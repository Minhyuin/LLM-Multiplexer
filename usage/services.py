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
from django.db import DatabaseError, NotSupportedError, connection, connections, transaction
from django.db.models import BooleanField, Count, F, Func, IntegerField, Max, OuterRef, Q, Subquery, Sum
from django.db.models.functions import TruncDate, TruncHour, TruncMonth
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from api_keys.models import APIKey, APIKeyAlias
from dashboard.models import ModelPricing
from usage.models import UsageImportJob, UsageLog


logger = logging.getLogger(__name__)

SYNC_LOCK = threading.Lock()
LAST_SYNC_AT = 0.0
SYNC_INTERVAL_SECONDS = 60
DASHBOARD_RANK_LIMIT = 8
# Dashboard panels, in the order aggregate_dashboard() has always returned them.
# ``sections=None`` computes every panel.
DASHBOARD_SECTIONS = ("summary", "models", "users", "keys", "trend", "errors", "active")
DASHBOARD_KEY_PAGE_SIZE_DEFAULT = 25
DASHBOARD_KEY_PAGE_SIZE_LIMIT = 50

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
        if not api_key:
            # Rotated keys: the identifier no longer matches any APIKey row,
            # but an APIKeyAlias still maps it back to its owner. Attribute to
            # the owner's current key so per-key panels keep one row per user.
            alias = APIKeyAlias.objects.filter(
                Q(token_hash=APIKey.hash_token(token_value))
                | Q(token_prefix__iexact=token_value)
                | Q(token_masked__iexact=token_value)
            ).select_related("user").first()
            if alias:
                user = alias.user
                api_key = alias.user.api_keys.order_by("-created_at").first()
    return user, api_key


def _refresh_api_keys_last_used(api_key_ids):
    """Backfill ``APIKey.last_used_at`` for the keys an import job touched.

    One aggregate query plus at most one UPDATE per key (never per usage row);
    the value is the max request_time across that key's logs, so backfills and
    live imports agree and the timestamp never moves backwards.
    """
    ids = {key_id for key_id in api_key_ids if key_id}
    if not ids:
        return
    latest_by_key = {
        row["api_key_id"]: row["latest"]
        for row in UsageLog.objects.filter(api_key_id__in=ids)
        .values("api_key_id")
        .annotate(latest=Max("request_time"))
    }
    for key_id, latest in latest_by_key.items():
        if latest is None:
            continue
        APIKey.objects.filter(
            Q(pk=key_id) & (Q(last_used_at__isnull=True) | Q(last_used_at__lt=latest))
        ).update(last_used_at=latest)


def import_usage_records(records, source_type, source_name):
    job = UsageImportJob.objects.create(source_type=source_type, source_name=source_name)
    imported = skipped = failed = 0
    errors = []
    touched_api_key_ids = set()

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
                        if api_key:
                            touched_api_key_ids.add(api_key.pk)
                skipped += 1
                continue
            # Only do expensive normalization for new records
            data = normalize_usage_record(raw)
            user, api_key = resolve_related_objects(data)
            UsageLog.objects.create(**data, user=user, api_key=api_key)
            if api_key:
                touched_api_key_ids.add(api_key.pk)
            existing_ids.add(rid)  # Prevent duplicates within same batch
            imported += 1
        except Exception as exc:
            failed += 1
            errors.append(f"第 {index} 行导入失败: {exc}")

    _refresh_api_keys_last_used(touched_api_key_ids)
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
        # Rotated keys authenticate with a plaintext that no APIKey row carries
        # any more; an APIKeyAlias still owns it, so attribute to the owner's
        # current key (same fallback resolve_related_objects uses).
        alias = APIKeyAlias.objects.filter(
            Q(token_plaintext=full_key) | Q(token_hash=APIKey.hash_token(full_key))
        ).select_related("user").first()
        if alias:
            return alias.user.api_keys.order_by("-created_at").first()

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
    touched_api_key_ids = set()

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
            if api_key:
                touched_api_key_ids.add(api_key.pk)
            imported += 1
        except Exception as exc:
            failed += 1
            errors.append(f"第 {index} 条 keeper 事件导入失败: {exc}")

    _refresh_api_keys_last_used(touched_api_key_ids)
    job.imported_count = imported
    job.skipped_count = skipped
    job.failed_count = failed
    job.summary = f"unmatched={unmatched}" if unmatched else ""
    if errors:
        job.summary = "\n".join([job.summary, *errors[:20]]).strip()
    job.save(update_fields=["imported_count", "skipped_count", "failed_count", "summary"])
    return job


def _known_key_identifier_sets():
    """(plaintexts, hashes, prefixes) of every key the portal manages.

    Current APIKeys plus rotated APIKeyAliases, so usage from an old key is not
    mistaken for an external key by the import filters.
    """
    known_plaintexts = set(
        APIKey.objects.exclude(token_plaintext="").values_list("token_plaintext", flat=True)
    )
    known_hashes = set(APIKey.objects.values_list("token_hash", flat=True))
    known_prefixes = set(
        APIKey.objects.exclude(token_prefix="").values_list("token_prefix", flat=True)
    )
    known_plaintexts.update(
        APIKeyAlias.objects.exclude(token_plaintext="").values_list("token_plaintext", flat=True)
    )
    known_prefixes.update(
        APIKeyAlias.objects.exclude(token_prefix="").values_list("token_prefix", flat=True)
    )
    return known_plaintexts, known_hashes, known_prefixes


def _is_known_key_identifier(key_id, known_plaintexts, known_hashes, known_prefixes):
    """Same known-key rule as the management-API sync: True when importable."""
    key_id = (key_id or "").strip()
    if not key_id:
        return True  # no identifier: keep, user_identifier may still attribute it
    if key_id in known_plaintexts:
        return True
    if APIKey.hash_token(key_id) in known_hashes:
        return True
    if key_id[:12] in known_prefixes:
        return True
    return False


def sync_query_trace_records(keeper_events=None, **kwargs):
    """Import proxy request-log files into QueryTrace.

    Tracing is an add-on: a broken log directory or an unreadable file must never
    abort the usage sync that keeps the portal totals correct, so failures are
    logged and reported as ``None``.
    """
    from usage.query_trace import sync_query_traces

    try:
        return sync_query_traces(keeper_events=keeper_events, **kwargs)
    except Exception as exc:  # noqa: BLE001 - see docstring
        logger.error("query trace sync failed: %s", exc)
        return None


def sync_keeper_usage_records(limit=KEEPER_DB_SYNC_BATCH, with_query_traces=True):
    """Sync from the local keeper database when present, else the HTTP API."""
    if keeper_db_available():
        events = fetch_keeper_events_sqlite(since_id=latest_keeper_event_id(), limit=limit)
        # Keeper events carry the caller's full key. Import only keys the portal
        # manages (current or rotated alias), aligned with the management-API
        # sync; external/outsourced keys are skipped instead of landing as
        # unattributed rows.
        known_plaintexts, known_hashes, known_prefixes = _known_key_identifier_sets()
        events = [
            event for event in events
            if _is_known_key_identifier(
                event.get("api_group_key") or event.get("api_key"),
                known_plaintexts, known_hashes, known_prefixes,
            )
        ]
        job = import_keeper_usage_events(events, source_name="cpa-usage-keeper-db")
        if with_query_traces:
            # The keeper events imported a moment ago are what let us say which
            # member sent the query, so trace sync rides along with this run.
            sync_query_trace_records(keeper_events=events)
        return job
    events = fetch_keeper_usage_events()
    job = import_keeper_usage_events(events)
    if with_query_traces:
        sync_query_trace_records()
    return job


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
    # Rotated aliases count as managed keys, so old-key records survive the filter.
    known_plaintexts, known_hashes, known_prefixes = _known_key_identifier_sets()

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
    touched_api_key_ids = set()

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
            if api_key:
                touched_api_key_ids.add(api_key.pk)
            existing_ids.add(rid)
            imported += 1
        except Exception as exc:
            failed += 1
            errors.append(f"第 {index} 条队列事件导入失败: {exc}")

    _refresh_api_keys_last_used(touched_api_key_ids)
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
    if trend_bucket == "month":
        return local_time.strftime("%Y-%m")
    return local_time.date().isoformat()


def _trend_trunc_function(trend_bucket):
    """DB truncation for a trend bucket; unknown buckets keep the legacy day view."""
    if trend_bucket == "hour":
        return TruncHour
    if trend_bucket == "month":
        return TruncMonth
    return TruncDate


def _local_bucket_time(bucket):
    if timezone.is_aware(bucket):
        return timezone.localtime(bucket)
    return bucket


def _bucket_label(bucket, trend_bucket):
    """Label for a truncated DB bucket, matching ``_trend_label``."""
    if bucket is None:
        return None
    if trend_bucket == "hour":
        return _local_bucket_time(bucket).strftime("%H:%M")
    if trend_bucket == "month":
        return _local_bucket_time(bucket).strftime("%Y-%m")
    return bucket.isoformat() if hasattr(bucket, "isoformat") else str(bucket)


# raw_payload token layouts in the legacy precedence order: the first path that
# holds a usable integer wins, later paths are only consulted when it does not.
CACHED_TOKEN_PATHS = (
    ("raw_payload", "tokens", "cached_tokens"),
    ("tokens", "cached_tokens"),
    ("cached_tokens",),
    ("usage", "cached_tokens"),
)
THINKING_TOKEN_PATHS = (
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

_DB_TOKEN_VENDORS = ("sqlite", "postgresql")
# JSON path keys are interpolated into a JSON path / SQL string literal, so keys
# that would need escaping are rejected (the legacy payload layouts never need it).
_PATH_KEY_FORBIDDEN = ('"', "\\")
# Python's int() accepts literals of any length; SQL integer arithmetic does not,
# so digit runs longer than 18 characters are left to the Python fallback.
_INT_LITERAL_MAX_DIGITS = 18
# Boundary used to decide that a JSON *number* still casts exactly the way
# int() would; anything beyond it goes to the Python fallback.
_INT_LITERAL_BOUND_SQL = "1e18"


def _valid_json_path_key(key):
    return bool(key) and not any(char in key for char in _PATH_KEY_FORBIDDEN)


def _json_path_sql(vendor, column_sql, path):
    """(value, json type, text form) SQL for one JSON path.

    ``column_sql`` is the compiled column reference (``"usage_usagelog"."raw_payload"``),
    ``path`` a tuple of object keys.
    """
    if not path or any(not _valid_json_path_key(key) for key in path):
        raise ValueError(f"unsupported JSON path: {path!r}")
    if vendor == "sqlite":
        json_path = "$" + "".join(f'."{key}"' for key in path)
        value_sql = f"JSON_EXTRACT({column_sql}, '{json_path}')"
        # json_extract() already yields SQL text for JSON strings and SQL numbers
        # for JSON numbers, so the value doubles as its own text form.
        return value_sql, f"TYPEOF({value_sql})", value_sql
    if vendor == "postgresql":
        array_path = "{" + ",".join(f'"{key}"' for key in path) + "}"
        value_sql = f"({column_sql} #> '{array_path}')"
        return value_sql, f"JSONB_TYPEOF({value_sql})", f"({column_sql} #>> '{array_path}')"
    raise NotSupportedError(f"no safe JSON int expression for database vendor {vendor!r}")


def _json_int_path_sql(vendor, column_sql, path):
    """Row-level (value SQL, SQL-inexpressible SQL) for one JSON path.

    ``value SQL`` reproduces ``_payload_int`` for one path: JSON null, missing
    keys, empty strings, objects and arrays yield NULL, JSON true/false yield
    1/0 (as ``int(True)``/``int(False)``), JSON numbers are truncated towards
    zero (as ``int(3.7)`` == 3) and JSON strings must be integer literals in the
    ``int()`` grammar (ASCII whitespace, optional sign, PEP 515 underscores
    between digits) -- so ``"3.7"`` is NULL, exactly like ``int("3.7")`` raises.

    ``SQL-inexpressible SQL`` is true for the rows only Python can interpret
    correctly (Unicode digits or whitespace, 19+ digit integers, numbers outside
    the int64 range); those rows are aggregated by the Python fallback instead.
    """
    value_sql, type_sql, text_sql = _json_path_sql(vendor, column_sql, path)
    if vendor == "sqlite":
        trimmed = f"TRIM({text_sql}, char(9) || char(10) || char(13) || ' ')"
        # The sign is stripped so the digit/underscore rules are checked on the
        # magnitude alone, then re-attached: int("-9") is negative, and casting the
        # sign-less digits on their own silently turned "-9" into 9.
        sign = f"CASE WHEN SUBSTR({trimmed}, 1, 1) = '-' THEN '-' ELSE '' END"
        body = f"CASE WHEN SUBSTR({trimmed}, 1, 1) IN ('+', '-') THEN SUBSTR({trimmed}, 2) ELSE {trimmed} END"
        digits = f"REPLACE({body}, '_', '')"
        signed = f"({sign} || {digits})"
        in_number_range = f"{value_sql} BETWEEN -{_INT_LITERAL_BOUND_SQL} AND {_INT_LITERAL_BOUND_SQL}"
        literal_ok = (
            f"({body} NOT GLOB '_*' AND {body} NOT GLOB '*_' AND {body} NOT GLOB '*__*'"
            f" AND LENGTH({digits}) BETWEEN 1 AND {_INT_LITERAL_MAX_DIGITS}"
            f" AND {digits} NOT GLOB '*[^0-9]*')"
        )
        # The casts live in their own CASE branch so that the literal rules and the
        # numeric range are validated before any conversion happens.
        value_expr = (
            "CASE"
            f" WHEN {value_sql} IS NULL THEN NULL"
            f" WHEN {type_sql} = 'integer' THEN {value_sql}"
            f" WHEN {type_sql} = 'real' THEN CASE WHEN {in_number_range}"
            f" THEN CAST({value_sql} AS INTEGER) ELSE NULL END"
            f" WHEN {type_sql} = 'text' THEN CASE WHEN {literal_ok}"
            f" THEN CAST({signed} AS INTEGER) ELSE NULL END"
            " ELSE NULL END"
        )
        inexpressible = (
            f"({value_sql} IS NOT NULL AND {text_sql} <> '' AND (CASE {type_sql}"
            f" WHEN 'text' THEN NOT {literal_ok}"
            f" WHEN 'real' THEN NOT ({in_number_range}) ELSE 0 END))"
        )
        return value_expr, inexpressible
    if vendor == "postgresql":
        trimmed = f"BTRIM({text_sql}, E' \\t\\n\\r')"
        # Same sign handling as the SQLite branch: int("-9") is negative.
        sign = f"CASE WHEN SUBSTR({trimmed}, 1, 1) = '-' THEN '-' ELSE '' END"
        body = f"CASE WHEN SUBSTR({trimmed}, 1, 1) IN ('+', '-') THEN SUBSTR({trimmed}, 2) ELSE {trimmed} END"
        digits = f"REPLACE({body}, '_', '')"
        signed = f"({sign} || {digits})"
        numeric = f"{text_sql}::numeric"
        literal_ok = (
            f"({body} !~ '^_' AND {body} !~ '_$' AND {body} !~ '__'"
            f" AND {digits} ~ '^[0-9]{{1,{_INT_LITERAL_MAX_DIGITS}}}$')"
        )
        in_range = f"{numeric} BETWEEN -{_INT_LITERAL_BOUND_SQL} AND {_INT_LITERAL_BOUND_SQL}"
        # PostgreSQL does not promise to short-circuit a flat AND chain, so a cast of
        # a non-numeric string could be evaluated for an unrelated row and abort the
        # query (silently degrading to the Python scan). Each cast therefore sits in
        # its own CASE branch, which is only evaluated after the type test matched.
        value_expr = (
            "CASE"
            f" WHEN {type_sql} = 'null' THEN NULL"
            f" WHEN {type_sql} = 'boolean' THEN CASE WHEN {text_sql} = 'true' THEN 1 ELSE 0 END"
            f" WHEN {type_sql} = 'number' THEN CASE WHEN {in_range}"
            f" THEN TRUNC({numeric})::bigint ELSE NULL END"
            f" WHEN {type_sql} = 'string' THEN CASE WHEN {literal_ok}"
            f" THEN ({signed})::bigint ELSE NULL END"
            " ELSE NULL END"
        )
        inexpressible = (
            f"({value_sql} IS NOT NULL AND {text_sql} <> '' AND (CASE {type_sql}"
            f" WHEN 'string' THEN NOT {literal_ok}"
            f" WHEN 'number' THEN NOT ({in_range}) ELSE FALSE END))"
        )
        return value_expr, inexpressible
    raise NotSupportedError(f"no safe JSON int expression for database vendor {vendor!r}")


def _build_safe_json_int_sql(vendor, column_sql, paths):
    """COALESCE over ``paths``: the legacy first-usable-int with a 0 fallback."""
    return "COALESCE(" + ", ".join(_json_int_path_sql(vendor, column_sql, path)[0] for path in paths) + ", 0)"


def _build_json_token_anomaly_sql(vendor, column_sql, paths):
    """Rows holding a token value that only Python's int() can interpret."""
    conditions = [_json_int_path_sql(vendor, column_sql, path)[1] for path in paths]
    return "(" + " OR ".join(conditions) + ")"


class _SafeJsonInt(Func):
    """Row-level safe int for the first usable value among ``paths``."""

    def __init__(self, paths, source="raw_payload", **kwargs):
        super().__init__(F(source), output_field=IntegerField(), **kwargs)
        self.token_paths = tuple(tuple(path) for path in paths)

    def as_sql(self, compiler, connection):
        column_sql, params = compiler.compile(self.source_expressions[0])
        return _build_safe_json_int_sql(connection.vendor, column_sql, self.token_paths), params


class _JsonTokenAnomaly(Func):
    """Boolean row expression: SQL cannot reproduce ``int()`` for this row."""

    filterable = True

    def __init__(self, paths, source="raw_payload", **kwargs):
        kwargs.setdefault("output_field", BooleanField())
        super().__init__(F(source), **kwargs)
        self.token_paths = tuple(tuple(path) for path in paths)

    def as_sql(self, compiler, connection):
        column_sql, params = compiler.compile(self.source_expressions[0])
        return _build_json_token_anomaly_sql(connection.vendor, column_sql, self.token_paths), params


def _token_totals_from_payload_scan(queryset, trend_bucket):
    """Legacy fallback: parse raw_payload row by row (unchanged semantics)."""
    totals = {}
    for row in queryset.only("request_time", "raw_payload").iterator(chunk_size=500):
        cached_tokens = _payload_int(row.raw_payload, *CACHED_TOKEN_PATHS)
        thinking_tokens = _payload_int(row.raw_payload, *THINKING_TOKEN_PATHS)
        if cached_tokens or thinking_tokens:
            label = _trend_label(row.request_time, trend_bucket)
            bucket = totals.setdefault(label, {"cached_tokens": 0, "thinking_tokens": 0})
            bucket["cached_tokens"] += cached_tokens
            bucket["thinking_tokens"] += thinking_tokens
    return totals


def _token_totals_by_label(queryset, trend_bucket):
    """{label: {"cached_tokens": n, "thinking_tokens": n}} from the database.

    Cached/thinking tokens are aggregated in SQL with an expression that mirrors
    ``_payload_int`` path precedence, so the whole payload column is never
    shipped to Python. Rows the SQL expression cannot represent exactly are
    fetched on their own and corrected in Python. Databases without a verified
    expression -- and any failure of the aggregate -- fall back to the legacy
    row-by-row scan.
    """
    vendor = connections[queryset.db].vendor if queryset.db else connection.vendor
    if vendor not in _DB_TOKEN_VENDORS:
        return _token_totals_from_payload_scan(queryset, trend_bucket)
    totals = {}
    try:
        with transaction.atomic(using=queryset.db):
            anomaly = _JsonTokenAnomaly(CACHED_TOKEN_PATHS + THINKING_TOKEN_PATHS)
            rows = (
                queryset.exclude(anomaly)
                .annotate(bucket=_trend_trunc_function(trend_bucket)("request_time"))
                .values("bucket")
                .annotate(
                    cached_tokens=Sum(_SafeJsonInt(CACHED_TOKEN_PATHS)),
                    thinking_tokens=Sum(_SafeJsonInt(THINKING_TOKEN_PATHS)),
                )
                .order_by("bucket")
            )
            for row in rows:
                label = _bucket_label(row["bucket"], trend_bucket)
                if label is None:
                    continue
                bucket = totals.setdefault(label, {"cached_tokens": 0, "thinking_tokens": 0})
                bucket["cached_tokens"] += int(row["cached_tokens"] or 0)
                bucket["thinking_tokens"] += int(row["thinking_tokens"] or 0)
            # Only the rows the expression cannot represent are streamed to Python
            # (usually none), so a malformed payload never materialises the column.
            for row in (
                queryset.filter(anomaly).only("request_time", "raw_payload").iterator(chunk_size=500)
            ):
                label = _trend_label(row.request_time, trend_bucket)
                bucket = totals.setdefault(label, {"cached_tokens": 0, "thinking_tokens": 0})
                bucket["cached_tokens"] += _payload_int(row.raw_payload, *CACHED_TOKEN_PATHS)
                bucket["thinking_tokens"] += _payload_int(row.raw_payload, *THINKING_TOKEN_PATHS)
            return totals
    except NotSupportedError:
        logger.warning("no JSON token aggregate for vendor %s; scanning raw_payload", vendor)
    except DatabaseError:
        logger.warning("JSON token aggregate failed on %s; scanning raw_payload", vendor, exc_info=True)
    return _token_totals_from_payload_scan(queryset, trend_bucket)


def _dashboard_summary_section(queryset):
    summary = queryset.aggregate(
        total_requests=Count("id"),
        total_tokens=Sum("total_tokens"),
        total_cost=Sum("estimated_cost"),
        error_count=Count("id", filter=Q(is_error=True)),
        active_users=Count("user", distinct=True),
        active_keys=Count("api_key", distinct=True),
    )
    total_requests = summary["total_requests"] or 0
    return {
        "total_requests": total_requests,
        "total_tokens": summary["total_tokens"] or 0,
        "total_cost": summary["total_cost"] or 0,
        "error_rate": round((summary["error_count"] / total_requests) * 100, 2) if total_requests else 0,
        "active_users": summary["active_users"] or 0,
        "active_keys": summary["active_keys"] or 0,
    }


# The legacy panels ranked ten models (the user and key panels ranked eight), and
# the dashboard templates read ``total_errors``/``total_tokens``, so the model
# panels keep the legacy row shape and slice exactly.
DASHBOARD_MODEL_RANK_LIMIT = 10


def _dashboard_models_section(queryset):
    model_rank = list(
        queryset.values("model_name")
        .annotate(total_requests=Count("id"), total_tokens=Sum("total_tokens"))
        .order_by("-total_tokens", "-total_requests")[:DASHBOARD_MODEL_RANK_LIMIT]
    )
    error_model_rank = list(
        queryset.filter(is_error=True)
        .values("model_name")
        .annotate(total_errors=Count("id"), total_tokens=Sum("total_tokens"))
        .order_by("-total_errors", "-total_tokens")[:DASHBOARD_MODEL_RANK_LIMIT]
    )
    return {"model_rank": model_rank, "error_model_rank": error_model_rank}


def _dashboard_user_rank(queryset):
    """Raw ``values()`` rows, exactly as the legacy panel returned them.

    The values keys are load-bearing: ``dashboard.views._enrich_display_names``
    reads ``user__username``/``user__profile__display_name`` and writes the
    ``display_name`` it shows, so pre-formatting the rows here would break the
    template for every name it has to fill in.
    """
    return list(
        queryset.exclude(user=None)
        .filter(user__profile__is_dashboard_visible=True)
        .values(
            "user",
            "user__username",
            "user__profile__display_name",
            "user__profile__lab_group",
            "user__profile__grade",
        )
        .annotate(total_tokens=Sum("total_tokens"))
        .order_by("-total_tokens")[:DASHBOARD_RANK_LIMIT]
    )


def _key_rank_queryset(queryset):
    """Per-key totals for the keys the dashboard may show."""
    return (
        queryset.exclude(api_key=None)
        .filter(api_key__user__profile__is_dashboard_visible=True)
        .values(
            "api_key",
            "api_key__user__username",
            "api_key__user__profile__display_name",
            "api_key__user__profile__lab_group",
            "api_key__user__profile__grade",
        )
        .annotate(total_tokens=Sum("total_tokens"), total_requests=Count("id"))
    )


def _top_model_subquery(queryset):
    """Highest-token model of a key, as a correlated subquery.

    The legacy panel used ``Max("model_name")``, which compares names instead of
    token totals, so a key whose busiest model sorted low (for example "gpt-5.4"
    against "zz-test") displayed the wrong top model. The subquery reuses the
    dashboard queryset, so time/group/grade/visibility filters still apply, and
    ties fall back to most requests, then model name ascending.
    """
    return (
        queryset.filter(api_key=OuterRef("api_key"))
        .values("model_name")
        .annotate(model_tokens=Sum("total_tokens"), model_requests=Count("id"))
        .order_by("-model_tokens", "-model_requests", "model_name")
        .values("model_name")[:1]
    )


def _key_rank_rows(items):
    return [
        {
            "display_name": _preferred_display_name(
                item["api_key__user__profile__display_name"], item["api_key__user__username"]
            ),
            "group_name": item["api_key__user__profile__lab_group"] or "未分组",
            "grade": item["api_key__user__profile__grade"] or "未设置",
            "top_model": item["top_model"],
            "total_tokens": item["total_tokens"],
            "total_requests": item["total_requests"],
        }
        for item in items
    ]


def _coerce_dashboard_page_int(value, name):
    if isinstance(value, bool) or (isinstance(value, float) and not value.is_integer()):
        raise ValueError(f"{name} 必须是整数")
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} 必须是整数") from None


def _dashboard_key_page_window(total, key_page, key_page_size):
    """(offset, limit, metadata) for key_rank pagination, following Django's Paginator."""
    page_size = max(1, min(_coerce_dashboard_page_int(key_page_size, "key_page_size"), DASHBOARD_KEY_PAGE_SIZE_LIMIT))
    num_pages = max(1, (total + page_size - 1) // page_size) if total else 1
    page = max(1, min(_coerce_dashboard_page_int(key_page, "key_page"), num_pages))
    metadata = {
        "page": page,
        "num_pages": num_pages,
        "total": total,
        "has_next": page < num_pages,
        "has_previous": page > 1,
    }
    return (page - 1) * page_size, page_size, metadata


def _dashboard_key_section(queryset, key_page, key_page_size):
    if key_page is None:
        items = (
            _key_rank_queryset(queryset)
            .annotate(top_model=Subquery(_top_model_subquery(queryset)))
            .order_by("-total_tokens")
        )
        return {"key_rank": _key_rank_rows(items)}
    # Paginate over the plain grouped query and only then attach the correlated
    # top-model subquery, so the expensive part runs for one page, not for every key.
    # ``api_key_id`` and not ``api_key``: ordering by a relation name follows the
    # related model's Meta.ordering instead of the foreign key column, which made
    # the tiebreak depend on APIKey's default ordering rather than on the key id.
    page_queryset = _key_rank_queryset(queryset).order_by("-total_tokens", "api_key_id")
    offset, limit, metadata = _dashboard_key_page_window(page_queryset.count(), key_page, key_page_size)
    items = page_queryset.annotate(top_model=Subquery(_top_model_subquery(queryset)))[offset:offset + limit]
    return {"key_rank": _key_rank_rows(items), "key_pagination": metadata}


def _dashboard_recent_errors(queryset):
    return list(
        queryset.filter(is_error=True)
        .exclude(user=None)
        .select_related("user", "api_key", "user__profile")
        .order_by("-request_time")[:30]
    )


def _dashboard_recent_active_users(queryset):
    return list(
        queryset.exclude(user=None)
        .values("user__username", "user__profile__display_name")
        .annotate(last_time=Sum("total_tokens"))
        .order_by("-last_time")[:10]
    )


def _normalize_dashboard_sections(sections):
    if sections is None:
        return DASHBOARD_SECTIONS
    if isinstance(sections, str):
        raise TypeError("sections 必须是可迭代的栏目名集合，而不是单个字符串")
    requested = set(sections)
    unknown = requested.difference(DASHBOARD_SECTIONS)
    if unknown:
        raise ValueError(
            "未知的看板栏目: " + ", ".join(sorted(str(name) for name in unknown))
            + "; 可用栏目: " + ", ".join(DASHBOARD_SECTIONS)
        )
    return tuple(name for name in DASHBOARD_SECTIONS if name in requested)


def aggregate_dashboard(queryset, trend_bucket="day", *, sections=None, key_page=None,
                        key_page_size=DASHBOARD_KEY_PAGE_SIZE_DEFAULT):
    """聚合看板数据。

    ``sections=None`` 按旧版顺序返回全部栏目；传入栏目名集合时只计算所请求的栏目
    （可用栏目见 ``DASHBOARD_SECTIONS``），未知栏目名会抛出 ``ValueError``。汇总栏目
    的计数与排名共用一次聚合查询，因此默认路径的查询次数也比旧版更少。

    ``key_page`` 传入页码时，``key_rank`` 按 ``-total_tokens, api_key`` 稳定排序并
    分页，同时返回 ``key_pagination``；``key_page_size`` 会被夹在
    ``1..DASHBOARD_KEY_PAGE_SIZE_LIMIT`` 之间。

    ``trend_bucket`` 支持 ``hour``、``day``（默认）与 ``month``（``YYYY-MM`` 标签，
    ``TruncMonth``），其它取值保持旧版的按天行为。

    cached/thinking tokens 仍按 ``raw_payload``/``_payload_int`` 的路径优先级取值，
    但由数据库聚合完成；数据库无法精确表达的行（Unicode 数字、超长整数等）改用
    Python 逐行修正。数据库不支持或聚合失败时，回退到旧版整表扫描。
    """
    data = {}
    for name in _normalize_dashboard_sections(sections):
        if name == "summary":
            data.update(_dashboard_summary_section(queryset))
        elif name == "models":
            data.update(_dashboard_models_section(queryset))
        elif name == "users":
            data["user_rank"] = _dashboard_user_rank(queryset)
        elif name == "keys":
            data.update(_dashboard_key_section(queryset, key_page, key_page_size))
        elif name == "trend":
            data["trend"] = _dashboard_trend_section(queryset, trend_bucket)
        elif name == "errors":
            data["recent_errors"] = _dashboard_recent_errors(queryset)
        elif name == "active":
            data["recent_active_users"] = _dashboard_recent_active_users(queryset)
    return data


def _dashboard_trend_section(queryset, trend_bucket):
    """Trend series: DB-grouped counters merged with cached/thinking token totals."""
    trend_map = {}
    rows = (
        queryset.annotate(bucket=_trend_trunc_function(trend_bucket)("request_time"))
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
    for row in rows:
        label = _bucket_label(row["bucket"], trend_bucket)
        if label is None:
            continue
        trend_map[label] = {
            "tokens": row["tokens"] or 0,
            "prompt_tokens": row["prompt_tokens"] or 0,
            "completion_tokens": row["completion_tokens"] or 0,
            "cached_tokens": 0,
            "thinking_tokens": 0,
            "requests": row["requests"] or 0,
            "errors": row["errors"] or 0,
        }
    for label, values in _token_totals_by_label(queryset, trend_bucket).items():
        bucket = trend_map.get(label)
        if bucket is not None:
            bucket["cached_tokens"] += values["cached_tokens"]
            bucket["thinking_tokens"] += values["thinking_tokens"]
    return [
        {
            "date": label,
            **values,
            "error_rate": round((values["errors"] / values["requests"]) * 100, 2) if values["requests"] else 0,
        }
        for label, values in trend_map.items()
    ]
