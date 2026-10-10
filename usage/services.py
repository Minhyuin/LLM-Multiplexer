import csv
import json
import logging
import os
import re
import sqlite3
import threading
import time
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from urllib.parse import urljoin

import requests

from django.conf import settings
from django.contrib.auth.models import User
from django.db.models import Count, Max, Q, Sum
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from api_keys.models import APIKey
from dashboard.models import ModelPricing
from usage.models import UsageImportJob, UsageLog


logger = logging.getLogger(__name__)

SYNC_LOCK = threading.Lock()
LAST_SYNC_AT = 0.0
SYNC_INTERVAL_SECONDS = 60
DASHBOARD_RANK_LIMIT = 8

DEFAULT_MODEL_PRICING = {
    "gpt-5.4": (Decimal("2.50"), Decimal("15.00"), Decimal("0.25")),
    "gpt-5.5": (Decimal("5.00"), Decimal("30.00"), Decimal("0.50")),
    "gpt-5.3-codex": (Decimal("1.75"), Decimal("14.00"), Decimal("0.175")),
    "gpt-5.4-mini": (Decimal("0.75"), Decimal("4.50"), Decimal("0.075")),
    "gemini-3.1-pro-preview": (Decimal("2.00"), Decimal("12.00"), Decimal("0.20")),
    "gemini-3.1-flash-lite-preview": (Decimal("0.25"), Decimal("1.50"), Decimal("0.03")),
}

CLIPROXY_SYNC_SOURCE_NAMES = [
    "cpa-usage-keeper-db",
    "cpa-usage-keeper",
    "cliproxy-management-api",
    "cliproxy-usage-queue",
]


def get_model_pricing(model_name):
    pricing = ModelPricing.objects.filter(model_name=model_name).first()
    if pricing:
        return pricing.prompt_price_per_million, pricing.completion_price_per_million, pricing.cached_price_per_million
    return DEFAULT_MODEL_PRICING.get(model_name, (Decimal("0"), Decimal("0"), Decimal("0")))


def calculate_estimated_cost(model_name, prompt_tokens, completion_tokens, raw_payload=None):
    prompt_rate, completion_rate, cached_rate = get_model_pricing(model_name)
    raw_payload = raw_payload or {}
    cached_tokens = int(((raw_payload.get("tokens") or {}).get("cached_tokens") or raw_payload.get("cached_tokens") or 0))
    normal_prompt_tokens = max(int(prompt_tokens) - cached_tokens, 0)
    cost = (
        Decimal(normal_prompt_tokens) * prompt_rate
        + Decimal(completion_tokens) * completion_rate
        + Decimal(cached_tokens) * cached_rate
    ) / Decimal("1000000")
    return cost.quantize(Decimal("0.000001"))


def parse_request_time(value):
    if not value:
        return timezone.now()
    if isinstance(value, datetime):
        return value
    parsed = parse_datetime(str(value))
    if not parsed:
        trimmed = re.sub(r"\.(\d{6})\d+([+-]\d{2}:\d{2}|Z)$", r".\1\2", str(value))
        parsed = parse_datetime(trimmed)
    if parsed:
        return parsed if timezone.is_aware(parsed) else timezone.make_aware(parsed)
    return timezone.now()


def normalize_usage_record(record):
    prompt_tokens = int(record.get("prompt_tokens") or 0)
    completion_tokens = int(record.get("completion_tokens") or 0)
    total_tokens = int(record.get("total_tokens") or prompt_tokens + completion_tokens)
    estimated_cost_raw = record.get("estimated_cost")
    if estimated_cost_raw in (None, "", 0, "0"):
        estimated_cost = calculate_estimated_cost(str(record.get("model_name") or "unknown").strip(), prompt_tokens, completion_tokens, record)
    else:
        estimated_cost = Decimal(str(estimated_cost_raw))
    status_code = int(record.get("status_code") or 200)
    error_message = record.get("error_message") or ""
    return {
        "request_id": str(record.get("request_id") or "").strip(),
        "user_identifier": str(record.get("user_identifier") or "").strip(),
        "api_key_identifier": str(record.get("api_key_identifier") or "").strip(),
        "model_name": str(record.get("model_name") or "unknown").strip(),
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "estimated_cost": estimated_cost,
        "status_code": status_code,
        "is_error": status_code >= 400 or bool(error_message),
        "error_message": error_message,
        "request_time": parse_request_time(record.get("request_time")),
        "raw_payload": record,
    }


def resolve_related_objects(data):
    user = None
    api_key = None
    if data["user_identifier"]:
        user = User.objects.filter(Q(username=data["user_identifier"]) | Q(email=data["user_identifier"])).first()
    if data["api_key_identifier"]:
        token_value = data["api_key_identifier"].strip()
        api_key = APIKey.objects.filter(
            Q(token_hash=APIKey.hash_token(token_value))
            | Q(token_prefix__iexact=token_value)
            | Q(token_masked__iexact=token_value)
            | Q(name__iexact=token_value)
        ).select_related("user").first()
        if api_key and not user:
            user = api_key.user
    return user, api_key


def import_usage_records(records, source_type, source_name):
    job = UsageImportJob.objects.create(source_type=source_type, source_name=source_name)
    imported = skipped = failed = 0
    errors = []

    # Quick pre-filter: extract request_ids first (cheap) and batch check existence
    raw_request_ids = []
    for raw in records:
        rid = str(raw.get("request_id") or "").strip()
        if rid:
            raw_request_ids.append(rid)

    # Batch lookup in chunks of 500 to avoid overly large IN clauses
    existing_ids = set()
    for i in range(0, len(raw_request_ids), 500):
        chunk = raw_request_ids[i:i + 500]
        existing_ids.update(
            UsageLog.objects.filter(request_id__in=chunk).values_list("request_id", flat=True)
        )

    # Only normalize and import records that are actually new
    for index, raw in enumerate(records, start=1):
        try:
            rid = str(raw.get("request_id") or "").strip()
            if not rid:
                failed += 1
                errors.append(f"第 {index} 行缺少 request_id")
                continue
            if rid in existing_ids:
                existing = UsageLog.objects.filter(request_id=rid).first()
                if existing and (not existing.user_id or not existing.api_key_id):
                    data = normalize_usage_record(raw)
                    user, api_key = resolve_related_objects(data)
                    update_fields = []
                    if user and not existing.user_id:
                        existing.user = user
                        update_fields.append("user")
                    if api_key and not existing.api_key_id:
                        existing.api_key = api_key
                        update_fields.append("api_key")
                    if update_fields:
                        existing.save(update_fields=update_fields)
                skipped += 1
                continue
            # Only do expensive normalization for new records
            data = normalize_usage_record(raw)
            user, api_key = resolve_related_objects(data)
            UsageLog.objects.create(**data, user=user, api_key=api_key)
            existing_ids.add(rid)  # Prevent duplicates within same batch
            imported += 1
        except Exception as exc:
            failed += 1
            errors.append(f"第 {index} 行导入失败: {exc}")

    job.imported_count = imported
    job.skipped_count = skipped
    job.failed_count = failed
    job.summary = "\n".join(errors[:20])
    job.save(update_fields=["imported_count", "skipped_count", "failed_count", "summary"])
    return job


def fetch_cliproxy_usage_records():
    headers = {"Authorization": f"Bearer {settings.CLIPROXY_MANAGEMENT_KEY}"}
    response = requests.get(
        urljoin(settings.CLIPROXY_MANAGEMENT_BASE_URL, "/v0/management/usage"),
        headers=headers,
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def normalize_cliproxy_usage_payload(payload):
    if isinstance(payload, list):
        return payload

    if not isinstance(payload, dict):
        raise ValueError("cliproxy usage 返回格式无效")

    for key in ["items", "data", "usage", "records"]:
        if isinstance(payload.get(key), list):
            return payload[key]

    usage_block = payload.get("usage") or {}
    apis = usage_block.get("apis") or {}
    records = []

    for api_key_identifier, api_item in apis.items():
        models = api_item.get("models") or {}
        for model_name, model_item in models.items():
            for detail in model_item.get("details") or []:
                token_info = detail.get("tokens") or {}
                records.append(
                    {
                        "request_id": detail.get("request_id") or f"cliproxy-{api_key_identifier[:12]}-{model_name}-{detail.get('timestamp')}",
                        "user_identifier": detail.get("source") or "",
                        "api_key_identifier": api_key_identifier,
                        "model_name": model_name,
                        "prompt_tokens": token_info.get("input_tokens") or 0,
                        "completion_tokens": token_info.get("output_tokens") or 0,
                        "total_tokens": token_info.get("total_tokens") or 0,
                        "estimated_cost": detail.get("estimated_cost") or detail.get("cost") or 0,
                        "status_code": 500 if detail.get("failed") else 200,
                        "error_message": detail.get("error_message") or "",
                        "request_time": detail.get("timestamp"),
                        "raw_payload": detail,
                    }
                )

    return records


def keeper_sync_configured():
    return all(
        os.getenv(name)
        for name in ["CPA_USAGE_KEEPER_BASE_URL", "CPA_USAGE_KEEPER_USERNAME", "CPA_USAGE_KEEPER_PASSWORD"]
    )


def _extract_keeper_events_page(payload, current_page):
    if not isinstance(payload, dict):
        return [], current_page

    data = payload.get("data")
    candidates = [payload.get("events"), payload.get("items"), payload.get("records")]
    if isinstance(data, list):
        candidates.append(data)
    elif isinstance(data, dict):
        candidates.extend([data.get("events"), data.get("items"), data.get("records")])

    page_events = []
    for candidate in candidates:
        if isinstance(candidate, list):
            page_events = candidate
            break

    data_obj = data if isinstance(data, dict) else {}
    pagination = payload.get("pagination") or data_obj.get("pagination") or {}
    total_pages = (
        payload.get("total_pages")
        or payload.get("pages")
        or data_obj.get("total_pages")
        or data_obj.get("pages")
        or pagination.get("total_pages")
        or pagination.get("pages")
        or current_page
    )
    return page_events, int(total_pages)


def fetch_keeper_usage_events(range_name=None, page_size=None, max_pages=20):
    base_url = os.getenv("CPA_USAGE_KEEPER_BASE_URL", "").rstrip("/")
    username = os.getenv("CPA_USAGE_KEEPER_USERNAME", "")
    password = os.getenv("CPA_USAGE_KEEPER_PASSWORD", "")
    range_name = range_name or os.getenv("CPA_USAGE_KEEPER_RANGE", "7d")
    page_size = int(page_size or os.getenv("CPA_USAGE_KEEPER_PAGE_SIZE", "500"))

    session = requests.Session()
    login = session.post(
        f"{base_url}/api/v1/auth/login",
        json={"username": username, "password": password},
        timeout=15,
    )
    login.raise_for_status()

    events = []
    for page in range(1, max_pages + 1):
        params = {"range": range_name, "page": page, "page_size": page_size}
        response = session.get(f"{base_url}/api/v1/usage/events", params=params, timeout=30)
        response.raise_for_status()
        payload = response.json()
        page_events, total_pages = _extract_keeper_events_page(payload, page)
        if not page_events:
            break
        events.extend(page_events)
        if page >= total_pages:
            break
    return events


KEEPER_DB_DEFAULT_PATH = "/root/cpa-usage-keeper/data/app.db"

# Cap a single periodic sync so one run cannot pull the whole history; the
# backfill_keeper_usage command passes limit=None when that is what you want.
KEEPER_DB_SYNC_BATCH = 5000

KEEPER_DB_COLUMNS = (
    "id, api_group_key, provider, endpoint, request_id, model, model_alias, timestamp, "
    "source, auth_index, failed, latency_ms, input_tokens, output_tokens, reasoning_tokens, "
    "cached_tokens, cache_read_tokens, cache_creation_tokens, total_tokens, reasoning_effort, "
    "service_tier"
)


def keeper_db_path():
    return os.getenv("CPA_USAGE_KEEPER_DB", KEEPER_DB_DEFAULT_PATH)


def keeper_db_available():
    path = keeper_db_path()
    return bool(path) and os.path.exists(path)


def fetch_keeper_events_sqlite(since_id=0, limit=None):
    """Read usage events straight from the local cpa-usage-keeper SQLite database.

    The keeper HTTP API masks the client key as ``sk-*********<last6>``. Keys that
    share a 6-character suffix (e.g. ``...YuMingYuan`` and ``...ShiNingYuan``) are
    indistinguishable there, so events cannot be attributed to a user. The database
    keeps the full key in ``api_group_key``, which makes the match exact.
    """
    conn = sqlite3.connect(f"file:{keeper_db_path()}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        sql = f"SELECT {KEEPER_DB_COLUMNS} FROM usage_events WHERE id > ? ORDER BY id ASC"
        params = [int(since_id or 0)]
        if limit:
            sql += " LIMIT ?"
            params.append(int(limit))
        return [_keeper_row_to_event(row) for row in conn.execute(sql, params)]
    finally:
        conn.close()


def _keeper_row_to_event(row):
    """Shape a SQLite row like a keeper HTTP API event so one importer handles both."""
    return {
        "id": str(row["id"]),
        "api_group_key": row["api_group_key"] or "",
        "api_key": row["api_group_key"] or "",
        "timestamp": row["timestamp"],
        "model": row["model"],
        "model_alias": row["model_alias"],
        "request_id": row["request_id"],
        "endpoint": row["endpoint"],
        "source": row["source"],
        "source_type": row["provider"],
        "provider": row["provider"],
        "auth_index": row["auth_index"],
        "reasoning_effort": row["reasoning_effort"],
        "service_tier": row["service_tier"],
        "failed": bool(row["failed"]),
        "latency_ms": row["latency_ms"] or 0,
        "tokens": {
            "input_tokens": row["input_tokens"] or 0,
            "output_tokens": row["output_tokens"] or 0,
            "reasoning_tokens": row["reasoning_tokens"] or 0,
            "cached_tokens": row["cached_tokens"] or 0,
            "cache_read_tokens": row["cache_read_tokens"] or 0,
            "cache_creation_tokens": row["cache_creation_tokens"] or 0,
            "total_tokens": row["total_tokens"] or 0,
        },
    }


def latest_keeper_event_id():
    """Highest keeper event id already stored, so syncs stay incremental."""
    highest = 0
    for rid in UsageLog.objects.filter(request_id__startswith="keeper-").values_list("request_id", flat=True):
        try:
            highest = max(highest, int(rid.split("-", 1)[1]))
        except (IndexError, ValueError):
            continue
    return highest


def _resolve_keeper_api_key(event):
    """Match a keeper event to a portal APIKey.

    Prefers the full key from the SQLite source; falls back to the masked suffix
    that the HTTP API exposes. Returns None when no single key matches -- the
    caller still imports the event so totals stay correct.
    """
    full_key = (event.get("api_group_key") or "").strip()
    if full_key and not full_key.startswith("sk-*"):
        api_key = APIKey.objects.filter(
            Q(token_plaintext=full_key) | Q(token_hash=APIKey.hash_token(full_key))
        ).select_related("user").first()
        if api_key:
            return api_key

    key_hash = (event.get("api_key_hash") or "").strip()
    key_prefix = (event.get("api_key_prefix") or "").strip()
    masked_key = (event.get("api_key") or "").strip()

    query = Q()
    if key_hash:
        query |= Q(token_hash=key_hash)
    if key_prefix:
        query |= Q(token_prefix__iexact=key_prefix)

    suffix = ""
    if masked_key:
        suffix = masked_key.rsplit("*", 1)[-1].strip()
        if len(suffix) >= 4:
            query |= Q(token_plaintext__endswith=suffix) | Q(token_masked__endswith=suffix)

    if not query:
        return None

    matches = list(APIKey.objects.filter(query).select_related("user")[:2])
    if len(matches) == 1:
        return matches[0]
    if key_hash or key_prefix:
        return matches[0] if matches else None
    return None


def import_keeper_usage_events(events, source_name="cpa-usage-keeper"):
    job = UsageImportJob.objects.create(source_type=UsageImportJob.SOURCE_API, source_name=source_name)
    imported = skipped = failed = unmatched = 0
    errors = []

    raw_ids = [f"keeper-{event.get('id')}" for event in events if event.get("id")]
    existing_ids = set()
    for i in range(0, len(raw_ids), 500):
        chunk = raw_ids[i:i + 500]
        existing_ids.update(UsageLog.objects.filter(request_id__in=chunk).values_list("request_id", flat=True))

    for index, event in enumerate(events, start=1):
        try:
            event_id = str(event.get("id") or "").strip()
            if not event_id:
                failed += 1
                errors.append(f"第 {index} 条 keeper 事件缺少 id")
                continue
            rid = f"keeper-{event_id}"
            if rid in existing_ids:
                skipped += 1
                continue

            api_key = _resolve_keeper_api_key(event)
            if not api_key:
                # Keep the event: an unattributed row still makes portal totals
                # match CPA. Dropping it silently is what made them diverge.
                unmatched += 1

            tokens = event.get("tokens") or {}
            prompt_tokens = int(tokens.get("input_tokens") or event.get("input_tokens") or 0)
            completion_tokens = int(tokens.get("output_tokens") or event.get("output_tokens") or 0)
            total_tokens = int(tokens.get("total_tokens") or event.get("total_tokens") or prompt_tokens + completion_tokens)
            cached_tokens = int(tokens.get("cached_tokens") or event.get("cached_tokens") or 0)
            reasoning_tokens = int(tokens.get("reasoning_tokens") or event.get("reasoning_tokens") or 0)
            model_name = str(event.get("model_alias") or event.get("model") or "unknown").strip()
            failed_event = bool(event.get("failed"))
            raw_payload = {
                "source": "cpa-usage-keeper",
                "keeper_event_id": event_id,
                "upstream_request_id": event.get("request_id"),
                "endpoint": event.get("endpoint"),
                "source_type": event.get("source_type"),
                "auth_index": event.get("auth_index"),
                "reasoning_effort": event.get("reasoning_effort"),
                "service_tier": event.get("service_tier"),
                "tokens": {
                    "input_tokens": prompt_tokens,
                    "output_tokens": completion_tokens,
                    "reasoning_tokens": reasoning_tokens,
                    "cached_tokens": cached_tokens,
                    "cache_read_tokens": int(tokens.get("cache_read_tokens") or event.get("cache_read_tokens") or 0),
                    "cache_creation_tokens": int(tokens.get("cache_creation_tokens") or event.get("cache_creation_tokens") or 0),
                    "total_tokens": total_tokens,
                },
            }
            fallback_identifier = (event.get("api_group_key") or event.get("api_key") or "").strip()
            UsageLog.objects.create(
                request_id=rid,
                user=api_key.user if api_key else None,
                api_key=api_key,
                user_identifier=api_key.user.username if api_key else "",
                api_key_identifier=api_key.token_prefix if api_key else fallback_identifier[:150],
                model_name=model_name,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                estimated_cost=calculate_estimated_cost(model_name, prompt_tokens, completion_tokens, raw_payload),
                status_code=500 if failed_event else 200,
                is_error=failed_event,
                error_message="keeper failed event" if failed_event else "",
                request_time=parse_request_time(event.get("timestamp")),
                raw_payload=raw_payload,
                latency_ms=int(event.get("latency_ms") or 0),
                cached_tokens=cached_tokens,
                reasoning_tokens=reasoning_tokens,
                provider=str(event.get("provider") or event.get("source_type") or "").strip(),
            )
            existing_ids.add(rid)
            imported += 1
        except Exception as exc:
            failed += 1
            errors.append(f"第 {index} 条 keeper 事件导入失败: {exc}")

    job.imported_count = imported
    job.skipped_count = skipped
    job.failed_count = failed
    job.summary = f"unmatched={unmatched}" if unmatched else ""
    if errors:
        job.summary = "\n".join([job.summary, *errors[:20]]).strip()
    job.save(update_fields=["imported_count", "skipped_count", "failed_count", "summary"])
    return job


def sync_keeper_usage_records(limit=KEEPER_DB_SYNC_BATCH):
    """Sync from the local keeper database when present, else the HTTP API."""
    if keeper_db_available():
        events = fetch_keeper_events_sqlite(since_id=latest_keeper_event_id(), limit=limit)
        return import_keeper_usage_events(events, source_name="cpa-usage-keeper-db")
    events = fetch_keeper_usage_events()
    return import_keeper_usage_events(events)


def sync_cliproxy_usage_records():
    try:
        payload = fetch_cliproxy_usage_records()
    except requests.HTTPError as exc:
        response = getattr(exc, "response", None)
        if getattr(response, "status_code", None) == 404:
            if keeper_db_available() or keeper_sync_configured():
                try:
                    return sync_keeper_usage_records()
                except (requests.RequestException, sqlite3.Error) as keeper_exc:
                    # Never fail silently here: a dead keeper used to leave the
                    # portal quietly stuck on an always-empty queue for days.
                    logger.error("keeper sync failed, falling back to usage-queue: %s", keeper_exc)
            return sync_cliproxy_usage_queue_records(count=10000)
        raise

    records = normalize_cliproxy_usage_payload(payload)

    # Only import records whose api_key_identifier matches a key bound in the portal.
    # This filters out usage from external/outsourced keys not managed here.
    known_plaintexts = set(
        APIKey.objects.exclude(token_plaintext="").values_list("token_plaintext", flat=True)
    )
    known_hashes = set(
        APIKey.objects.values_list("token_hash", flat=True)
    )
    known_prefixes = set(
        APIKey.objects.exclude(token_prefix="").values_list("token_prefix", flat=True)
    )

    filtered = []
    for record in records:
        key_id = (record.get("api_key_identifier") or "").strip()
        if not key_id:
            # No key identifier — include it (might be matched by user_identifier later)
            filtered.append(record)
            continue
        if key_id in known_plaintexts:
            filtered.append(record)
        elif APIKey.hash_token(key_id) in known_hashes:
            filtered.append(record)
        elif key_id[:12] in known_prefixes:
            filtered.append(record)
        # else: external key, skip

    return import_usage_records(filtered, UsageImportJob.SOURCE_API, "cliproxy-management-api")


def sync_cliproxy_usage_queue_records(count=10000):
    events = fetch_usage_queue_events(count=count)
    if isinstance(events, dict):
        events = events.get("items") or events.get("data") or events.get("messages") or []
    if not isinstance(events, list):
        events = []

    job = UsageImportJob.objects.create(source_type=UsageImportJob.SOURCE_API, source_name="cliproxy-usage-queue")
    imported = skipped = failed = 0
    errors = []

    raw_ids = [str(event.get("request_id") or "").strip() for event in events if event.get("request_id")]
    existing_ids = set()
    for i in range(0, len(raw_ids), 500):
        chunk = raw_ids[i:i + 500]
        existing_ids.update(UsageLog.objects.filter(request_id__in=chunk).values_list("request_id", flat=True))

    for index, event in enumerate(events, start=1):
        try:
            rid = str(event.get("request_id") or "").strip()
            if not rid:
                failed += 1
                errors.append(f"第 {index} 条队列事件缺少 request_id")
                continue
            if rid in existing_ids:
                skipped += 1
                continue

            data = normalize_queue_event(event)
            user, api_key = resolve_related_objects(data)
            UsageLog.objects.create(**data, user=user, api_key=api_key)
            existing_ids.add(rid)
            imported += 1
        except Exception as exc:
            failed += 1
            errors.append(f"第 {index} 条队列事件导入失败: {exc}")

    job.imported_count = imported
    job.skipped_count = skipped
    job.failed_count = failed
    job.summary = "\n".join(errors[:20])
    job.save(update_fields=["imported_count", "skipped_count", "failed_count", "summary"])
    return job


def latest_cliproxy_sync_job():
    return UsageImportJob.objects.filter(source_name__in=CLIPROXY_SYNC_SOURCE_NAMES).first()


def auto_sync_cliproxy_usage_records(force=False):
    global LAST_SYNC_AT
    now = time.time()
    if not force and now - LAST_SYNC_AT < SYNC_INTERVAL_SECONDS:
        return latest_cliproxy_sync_job(), False

    if not SYNC_LOCK.acquire(blocking=False):
        return latest_cliproxy_sync_job(), False

    try:
        job = sync_cliproxy_usage_records()
        LAST_SYNC_AT = time.time()
        return job, True
    finally:
        SYNC_LOCK.release()


def fetch_usage_queue_events(count=100):
    """Consume real-time events from CPAP /v0/management/usage-queue endpoint."""
    headers = {"Authorization": f"Bearer {settings.CLIPROXY_MANAGEMENT_KEY}"}
    response = requests.get(
        urljoin(settings.CLIPROXY_MANAGEMENT_BASE_URL, f"/v0/management/usage-queue?count={count}"),
        headers=headers,
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def normalize_queue_event(event):
    """Normalize a single usage-queue event into UsageLog-compatible dict."""
    tokens = event.get("tokens") or {}
    input_tokens = int(tokens.get("input_tokens") or 0)
    output_tokens = int(tokens.get("output_tokens") or 0)
    cached_tokens = int(tokens.get("cached_tokens") or 0)
    reasoning_tokens = int(tokens.get("reasoning_tokens") or 0)
    total_tokens = int(tokens.get("total_tokens") or input_tokens + output_tokens)
    model_name = str(event.get("model") or event.get("alias") or "unknown").strip()
    failed = bool(event.get("failed"))

    estimated_cost = calculate_estimated_cost(model_name, input_tokens, output_tokens, event)

    return {
        "request_id": str(event.get("request_id") or "").strip(),
        "user_identifier": str(event.get("source") or "").strip(),
        "api_key_identifier": str(event.get("api_key") or "").strip(),
        "model_name": model_name,
        "prompt_tokens": input_tokens,
        "completion_tokens": output_tokens,
        "total_tokens": total_tokens,
        "estimated_cost": estimated_cost,
        "status_code": 500 if failed else 200,
        "is_error": failed,
        "error_message": "",
        "request_time": parse_request_time(event.get("timestamp")),
        "raw_payload": event,
        "latency_ms": int(event.get("latency_ms") or 0),
        "cached_tokens": cached_tokens,
        "reasoning_tokens": reasoning_tokens,
        "provider": str(event.get("provider") or "").strip(),
    }


QUEUE_SYNC_LOCK = threading.Lock()
LAST_QUEUE_SYNC_AT = 0.0
QUEUE_SYNC_INTERVAL_SECONDS = 30


def sync_usage_queue_events(count=100):
    """Fetch and persist events from the usage-queue, deduplicating by request_id."""
    payload = fetch_usage_queue_events(count)

    # The endpoint may return a list or a dict with a list inside
    if isinstance(payload, list):
        events = payload
    elif isinstance(payload, dict):
        events = payload.get("items") or payload.get("data") or payload.get("messages") or []
    else:
        events = []

    if not events:
        return 0, 0

    # Extract request_ids for batch dedup check
    raw_ids = [str(e.get("request_id") or "").strip() for e in events if e.get("request_id")]
    existing_ids = set()
    for i in range(0, len(raw_ids), 500):
        chunk = raw_ids[i:i + 500]
        existing_ids.update(
            UsageLog.objects.filter(request_id__in=chunk).values_list("request_id", flat=True)
        )

    imported = 0
    skipped = 0
    for event in events:
        rid = str(event.get("request_id") or "").strip()
        if not rid:
            skipped += 1
            continue
        if rid in existing_ids:
            skipped += 1
            continue

        data = normalize_queue_event(event)
        user, api_key = resolve_related_objects(data)
        UsageLog.objects.create(**data, user=user, api_key=api_key)
        existing_ids.add(rid)
        imported += 1

    return imported, skipped


def auto_sync_usage_queue(force=False):
    """Auto-sync usage queue events with rate limiting (every 30s)."""
    global LAST_QUEUE_SYNC_AT
    now = time.time()
    if not force and now - LAST_QUEUE_SYNC_AT < QUEUE_SYNC_INTERVAL_SECONDS:
        return None, False

    if not QUEUE_SYNC_LOCK.acquire(blocking=False):
        return None, False

    try:
        imported, skipped = sync_usage_queue_events()
        LAST_QUEUE_SYNC_AT = time.time()
        return {"imported": imported, "skipped": skipped}, True
    except Exception:
        return None, False
    finally:
        QUEUE_SYNC_LOCK.release()


def load_records_from_file(uploaded_file):
    name = uploaded_file.name.lower()
    content = uploaded_file.read().decode("utf-8")
    if name.endswith(".jsonl"):
        return [json.loads(line) for line in content.splitlines() if line.strip()]
    if name.endswith(".csv"):
        reader = csv.DictReader(content.splitlines())
        return list(reader)
    raise ValueError("仅支持 CSV 或 JSONL 文件")


def mask_username(name, enabled=True, index=None):
    if not enabled:
        return name
    if index is not None:
        return f"user_{index:02d}"
    if len(name) <= 1:
        return "*"
    return f"{name[0]}*"


def _preferred_display_name(display_name, username, fallback="未设置显示名"):
    value = (display_name or "").strip()
    username_value = (username or "").strip()
    if value and value.lower() != username_value.lower():
        return value
    return fallback


def _payload_int(payload, *paths):
    payload = payload or {}
    for path in paths:
        value = payload
        for key in path:
            if not isinstance(value, dict):
                value = None
                break
            value = value.get(key)
        if value not in (None, ""):
            try:
                return int(value)
            except (TypeError, ValueError):
                continue
    return 0


def _trend_label(request_time, trend_bucket):
    local_time = timezone.localtime(request_time)
    if trend_bucket == "hour":
        return local_time.replace(minute=0, second=0, microsecond=0).strftime("%H:%M")
    return local_time.date().isoformat()


def aggregate_dashboard(queryset, trend_bucket="day"):
    total_requests = queryset.count()
    summary = queryset.aggregate(total_tokens=Sum("total_tokens"), total_cost=Sum("estimated_cost"), error_count=Count("id", filter=Q(is_error=True)))
    total_tokens = summary["total_tokens"] or 0
    total_cost = summary["total_cost"] or Decimal("0")
    error_count = summary["error_count"] or 0
    error_rate = round((error_count / total_requests) * 100, 2) if total_requests else 0
    active_users = queryset.exclude(user=None).values("user").distinct().count()
    active_keys = queryset.exclude(api_key=None).values("api_key").distinct().count()
    model_rank = list(
        queryset.values("model_name")
        .annotate(total_requests=Count("id"), total_tokens=Sum("total_tokens"))
        .order_by("-total_tokens", "-total_requests")[:10]
    )
    error_model_rank = list(
        queryset.filter(is_error=True)
        .values("model_name")
        .annotate(total_errors=Count("id"), total_tokens=Sum("total_tokens"))
        .order_by("-total_errors", "-total_tokens")[:10]
    )
    user_rank = list(
        queryset.exclude(user=None)
        .filter(user__profile__is_dashboard_visible=True)
        .values("user", "user__username", "user__profile__display_name", "user__profile__lab_group", "user__profile__grade")
        .annotate(total_tokens=Sum("total_tokens"))
        .order_by("-total_tokens")[:DASHBOARD_RANK_LIMIT]
    )
    key_rank = []
    top_keys = list(
        queryset.exclude(api_key=None)
        .filter(api_key__user__profile__is_dashboard_visible=True)
        .values("api_key", "api_key__user__username", "api_key__user__profile__display_name", "api_key__user__profile__lab_group", "api_key__user__profile__grade")
        .annotate(total_tokens=Sum("total_tokens"), total_requests=Count("id"), top_model=Max("model_name"))
        .order_by("-total_tokens")
    )
    for item in top_keys:
        key_rank.append(
            {
                "display_name": _preferred_display_name(item["api_key__user__profile__display_name"], item["api_key__user__username"]),
                "group_name": item["api_key__user__profile__lab_group"] or "未分组",
                "grade": item["api_key__user__profile__grade"] or "未设置",
                "top_model": item["top_model"],
                "total_tokens": item["total_tokens"],
                "total_requests": item["total_requests"],
            }
        )
    # Use DB-level aggregation for trend data to avoid loading all rows into memory
    from django.db.models.functions import TruncHour, TruncDate
    trunc_fn = TruncHour if trend_bucket == "hour" else TruncDate
    trend_qs = (
        queryset
        .annotate(bucket=trunc_fn("request_time"))
        .values("bucket")
        .annotate(
            tokens=Sum("total_tokens"),
            prompt_tokens=Sum("prompt_tokens"),
            completion_tokens=Sum("completion_tokens"),
            requests=Count("id"),
            errors=Count("id", filter=Q(is_error=True)),
        )
        .order_by("bucket")
    )
    # For cached/thinking tokens we still need raw_payload parsing on a limited set
    trend_map = defaultdict(lambda: {"tokens": 0, "prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0, "thinking_tokens": 0, "requests": 0, "errors": 0})
    for row in trend_qs:
        if row["bucket"] is None:
            continue
        bucket = row["bucket"]
        if trend_bucket == "hour":
            local_time = timezone.localtime(bucket)
            label = local_time.strftime("%H:%M")
        else:
            # TruncDate returns a date object, not datetime
            label = bucket.isoformat() if hasattr(bucket, 'isoformat') else str(bucket)
        trend_map[label]["tokens"] += row["tokens"] or 0
        trend_map[label]["prompt_tokens"] += row["prompt_tokens"] or 0
        trend_map[label]["completion_tokens"] += row["completion_tokens"] or 0
        trend_map[label]["requests"] += row["requests"] or 0
        trend_map[label]["errors"] += row["errors"] or 0

    # Parse cached/thinking tokens from raw_payload (only fetch needed fields)
    for row in queryset.only("request_time", "raw_payload").iterator(chunk_size=500):
        label = _trend_label(row.request_time, trend_bucket)
        cached_tokens = _payload_int(row.raw_payload, ("raw_payload", "tokens", "cached_tokens"), ("tokens", "cached_tokens"), ("cached_tokens",), ("usage", "cached_tokens"))
        thinking_tokens = _payload_int(
            row.raw_payload,
            ("raw_payload", "tokens", "reasoning_tokens"),
            ("raw_payload", "tokens", "thinking_tokens"),
            ("tokens", "thinking_tokens"),
            ("tokens", "reasoning_tokens"),
            ("thinking_tokens",),
            ("reasoning_tokens",),
            ("usage", "thinking_tokens"),
            ("usage", "reasoning_tokens"),
            ("completion_tokens_details", "reasoning_tokens"),
        )
        if cached_tokens or thinking_tokens:
            trend_map[label]["cached_tokens"] += cached_tokens
            trend_map[label]["thinking_tokens"] += thinking_tokens

    trend = [{"date": label, **values, "error_rate": round((values['errors'] / values['requests']) * 100, 2) if values['requests'] else 0} for label, values in trend_map.items()]
    recent_errors = list(
        queryset.filter(is_error=True)
        .exclude(user=None)
        .select_related("user", "api_key", "user__profile")[:30]
    )
    recent_active_users = list(
        queryset.exclude(user=None)
        .values("user__username", "user__profile__display_name")
        .annotate(last_time=Sum("total_tokens"))
        .order_by("-last_time")[:10]
    )
    return {
        "total_requests": total_requests,
        "total_tokens": total_tokens,
        "total_cost": total_cost,
        "error_rate": error_rate,
        "active_users": active_users,
        "active_keys": active_keys,
        "model_rank": model_rank,
        "error_model_rank": error_model_rank,
        "user_rank": user_rank,
        "key_rank": key_rank,
        "trend": trend,
        "recent_errors": recent_errors,
        "recent_active_users": recent_active_users,
    }
