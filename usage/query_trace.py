"""Read the concrete query behind each proxied request.

The portal never sees request bodies: they only exist inside the CLIProxy process.
CLIProxy can persist them (``request-log: true``), one file per request under its
log directory, named ``<endpoint>-<utc stamp>-<short id>.log``.  The client's own
body sits in the ``=== REQUEST BODY ===`` section, and the 8 hex characters in the
file name are the same short request id that appears as ``[477039ae]`` in
``main.log`` and as the tail of the keeper's ``usage_events.request_id`` UUID, which
is what makes attribution to a portal user possible.

This module scans those files, keeps only the final user message (the query), and
stores it in :class:`usage.models.QueryTrace` with FIFO retention.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from django.conf import settings
from django.utils import timezone as django_timezone

from api_keys.models import APIKey
from usage.models import QueryTrace, UsageImportJob, UsageLog

logger = logging.getLogger(__name__)

#: ``v1-chat-completions-2026-10-14T052326-477039ae.log``
FILENAME_RE = re.compile(
    r"^(?P<prefix>[a-z0-9][a-z0-9-]*?)-(?P<stamp>\d{4}-\d{2}-\d{2}T\d{6})-(?P<short>[0-9a-f]{8})\.log$"
)

#: Files that live in the same directory but never hold a request body.
SKIPPED_PREFIXES = ("main", "cred-sync", "error-", "request-log")

SECTION_RE = re.compile(r"^=== (?P<name>[A-Z][A-Z0-9 ]*?)(?: (?P<index>\d+))? ===$", re.MULTILINE)
INFO_FIELD_RE = re.compile(r"^(?P<key>[A-Za-z][A-Za-z ]*?):\s*(?P<value>.*)$")

REDACTIONS = (
    (re.compile(r"sk-[A-Za-z0-9_\-]{12,}"), "sk-***"),
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{12,}"), "Bearer ***"),
    (
        re.compile(
            r"(?i)(\"?(?:api[_-]?key|access[_-]?token|refresh[_-]?token|auth[_-]?token|secret|password)\"?"
            r"\s*[:=]\s*\")([^\"]{6,})(\")"
        ),
        r"\1***\3",
    ),
)
NANO_SECONDS_RE = re.compile(r"\.(\d{6})\d+")


def query_trace_log_dir() -> Path:
    return Path(getattr(settings, "QUERY_TRACE_LOG_DIR", "/root/cliproxyapi/logs"))


def query_trace_enabled() -> bool:
    return bool(getattr(settings, "QUERY_TRACE_ENABLED", True))


def _setting_int(name: str, default: int) -> int:
    try:
        return int(getattr(settings, name, default))
    except (TypeError, ValueError):
        return default


def parse_log_timestamp(value: str):
    """Parse the logger's RFC3339 timestamp (it carries nanoseconds)."""
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    text = NANO_SECONDS_RE.sub(r".\1", text)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def parse_log_name(name: str):
    """Return ``(prefix, request_time, short_id)`` for a request log file name."""
    match = FILENAME_RE.match(name)
    if not match:
        return None
    stamp = match.group("stamp")
    try:
        request_time = datetime.strptime(stamp, "%Y-%m-%dT%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return match.group("prefix"), request_time, match.group("short")


def parse_request_log(text: str) -> dict:
    """Split a request log into its sections and pull out the interesting fields."""
    matches = list(SECTION_RE.finditer(text))
    sections: dict[str, str] = {}
    ordered: list[tuple[str, str]] = []
    for position, match in enumerate(matches):
        start = match.end()
        end = matches[position + 1].start() if position + 1 < len(matches) else len(text)
        ordered.append((match.group("name"), text[start:end].strip("\n")))

    for name, body in ordered:
        sections.setdefault(name, body)

    info = {}
    for line in sections.get("REQUEST INFO", "").splitlines():
        field = INFO_FIELD_RE.match(line.strip())
        if field:
            info[field.group("key").strip().lower().replace(" ", "_")] = field.group("value").strip()

    status_code = 0
    for name, body in reversed(ordered):
        if name != "RESPONSE":
            continue
        status = re.search(r"^Status:\s*(\d+)", body, re.MULTILINE)
        if status:
            status_code = int(status.group(1))
        break

    upstream_attempts = sum(1 for name, _ in ordered if name.startswith("API REQUEST"))
    raw_body = sections.get("REQUEST BODY", "")

    return {
        "info": info,
        "url": info.get("url", ""),
        "method": info.get("method", ""),
        "version": info.get("version", ""),
        "request_time": parse_log_timestamp(info.get("timestamp", "")),
        "request_body": raw_body,
        "status_code": status_code,
        "upstream_attempts": upstream_attempts,
        "sections": sections,
    }


def _content_parts(content):
    """Yield ``(text, is_attachment)`` for a message content payload."""
    if isinstance(content, str):
        yield content, False
        return
    if not isinstance(content, list):
        return
    for part in content:
        if not isinstance(part, dict):
            if isinstance(part, str):
                yield part, False
            continue
        part_type = str(part.get("type") or "").lower()
        text = part.get("text")
        if isinstance(text, str) and text:
            yield text, False
            continue
        if part.get("content"):
            for nested, attachment in _content_parts(part["content"]):
                yield nested, attachment
            continue
        if part_type or part:
            yield "", True


def extract_query(body_text: str) -> dict:
    """Return the final user message of a request body.

    Handles the chat-completions shape (``messages``), the responses shape
    (``input``) and the gemini-ish shape (``contents``).  Every other role and
    every non-text part is counted but never stored.
    """
    result = {
        "query_text": "",
        "message_count": 0,
        "has_attachments": False,
        "model": "",
        "reason": "",
    }
    if not body_text:
        result["reason"] = "empty body"
        return result

    try:
        body = json.loads(body_text)
    except (json.JSONDecodeError, ValueError):
        result["reason"] = "unparsable body"
        return result
    if not isinstance(body, dict):
        result["reason"] = "non-object body"
        return result

    result["model"] = str(body.get("model") or "")

    candidates = []
    messages = body.get("messages")
    if isinstance(messages, list):
        result["message_count"] = len(messages)
        for message in messages:
            if isinstance(message, dict) and str(message.get("role") or "").lower() == "user":
                candidates.append(message.get("content"))

    if not candidates and isinstance(body.get("input"), list):
        items = body["input"]
        result["message_count"] = result["message_count"] or len(items)
        for item in items:
            if isinstance(item, dict) and str(item.get("role") or "").lower() == "user":
                candidates.append(item.get("content"))
    elif not candidates and isinstance(body.get("input"), str):
        candidates.append(body["input"])

    if not candidates and isinstance(body.get("contents"), list):
        items = body["contents"]
        result["message_count"] = result["message_count"] or len(items)
        for item in items:
            if isinstance(item, dict) and str(item.get("role") or "").lower() in {"user", ""}:
                if item.get("parts") is not None:
                    candidates.append(item.get("parts"))
                else:
                    candidates.append(item.get("content"))

    if not candidates:
        result["reason"] = "no user message"
        return result

    chunks = []
    # Only the final user message is stored: earlier turns are the agent's
    # context, not the prompt the member is accountable for.
    for content in candidates[-1:]:
        for text, is_attachment in _content_parts(content):
            if is_attachment:
                result["has_attachments"] = True
            elif text:
                chunks.append(text)

    query = "\n".join(chunk.strip() for chunk in chunks if chunk.strip()).strip()
    if not query and result["has_attachments"]:
        result["reason"] = "attachment only"
    elif not query:
        result["reason"] = "empty query"
    result["query_text"] = query
    return result


def redact_secrets(text: str):
    """Mask the obvious credentials a member might paste into a prompt."""
    redacted = text
    for pattern, replacement in REDACTIONS:
        redacted = pattern.sub(replacement, redacted)
    return redacted, redacted != text


def store_mode() -> str:
    mode = str(getattr(settings, "QUERY_TRACE_STORE_MODE", "query") or "query").strip().lower()
    return mode if mode in {"query", "body"} else "query"


def build_query_payload(body_text: str):
    """Turn a raw request body into the text we keep, honouring the store mode."""
    max_chars = _setting_int("QUERY_TRACE_MAX_CHARS", 20000)
    details = extract_query(body_text)

    if store_mode() == "body":
        text = body_text.strip()
        details["reason"] = ""
        details["message_count"] = details["message_count"] or 0
    else:
        text = details["query_text"]

    text, redacted = redact_secrets(text)
    truncated = len(text) > max_chars
    if truncated:
        text = text[:max_chars]
    details["query_text"] = text
    details["query_chars"] = len(text)
    details["truncated"] = truncated
    details["redacted"] = redacted
    return details


def short_id_for_request_id(request_id: str) -> str:
    """The logger's short request id is the tail of the keeper's request UUID."""
    compact = re.sub(r"[^0-9a-fA-F]", "", str(request_id or ""))
    return compact[-8:].lower() if len(compact) >= 8 else ""


def iter_request_logs(since=None):
    """Yield ``(path, stat, request_time, short_id)`` oldest first."""
    directory = query_trace_log_dir()
    if not directory.is_dir():
        return []
    limit = _setting_int("QUERY_TRACE_SCAN_LIMIT", 500)
    found = []
    for entry in os.scandir(directory):
        try:
            if not entry.is_file():
                continue
        except OSError:
            continue
        parsed = parse_log_name(entry.name)
        if not parsed:
            continue
        prefix, request_time, short_id = parsed
        if prefix.startswith(SKIPPED_PREFIXES):
            continue
        if since and request_time < since:
            continue
        try:
            stat = entry.stat()
        except OSError:
            continue
        found.append((request_time, entry.path, stat.st_size, short_id, prefix))
    found.sort(key=lambda item: item[0])
    return found[:limit]


def _attribution_from_keeper_events(keeper_events):
    """Map short request id -> UsageLog for the keeper events imported right now."""
    keeper_ids = [f"keeper-{event.get('id')}" for event in keeper_events or [] if event.get("id")]
    mapping = {}
    for index in range(0, len(keeper_ids), 500):
        chunk = keeper_ids[index:index + 500]
        for log in UsageLog.objects.filter(request_id__in=chunk).select_related("user", "api_key"):
            short_id = short_id_for_request_id((log.raw_payload or {}).get("upstream_request_id"))
            if short_id:
                mapping[short_id] = log
    return mapping


def _attribution_from_recent_logs(days=None, since=None):
    """Fallback attribution for logs whose keeper event was imported earlier."""
    days = days or _setting_int("QUERY_TRACE_ATTRIBUTION_DAYS", 3)
    window_start = since or (django_timezone.now() - timedelta(days=days))
    mapping = {}
    rows = (
        UsageLog.objects.filter(request_time__gte=window_start)
        .select_related("user", "api_key")
        .only("request_id", "user", "api_key", "model_name", "request_time", "raw_payload")
        .order_by("-request_time")[:20000]
    )
    for log in rows:
        short_id = short_id_for_request_id((log.raw_payload or {}).get("upstream_request_id"))
        if short_id and short_id not in mapping:
            mapping[short_id] = log
    return mapping


def _fallback_attribution(short_id: str, request_time):
    """Look a single short id up in the usage table when the batch map missed it."""
    if not short_id or not request_time:
        return None
    window_start = request_time - timedelta(days=2)
    window_end = request_time + timedelta(days=2)
    return (
        UsageLog.objects.filter(
            request_time__range=(window_start, window_end),
            raw_payload__upstream_request_id__endswith=f"-{short_id}",
        )
        .select_related("user", "api_key")
        .first()
    )


def reattribute_query_traces(since=None, limit=2000):
    """Give traces a second chance at attribution.

    A request log is written the moment the proxy answers, while the matching usage
    row only lands minutes later with the keeper sync, so a trace is often stored
    unattributed first. Re-checking the still-unattributed rows inside the
    attribution window is what keeps those requests traceable to a member.
    """
    window_start = since or (
        django_timezone.now() - timedelta(days=_setting_int("QUERY_TRACE_ATTRIBUTION_DAYS", 3))
    )
    pending = list(
        QueryTrace.objects.filter(user__isnull=True, request_time__gte=window_start)
        .order_by("-request_time")[: int(limit)]
    )
    if not pending:
        return 0

    attribution = _attribution_from_recent_logs(since=window_start)
    fixed = 0
    for trace in pending:
        usage_log = attribution.get(trace.short_id) or _fallback_attribution(trace.short_id, trace.request_time)
        if usage_log is None:
            continue
        api_key = getattr(usage_log, "api_key", None)
        if api_key is None and usage_log.user_id:
            api_key = APIKey.objects.filter(user=usage_log.user).order_by("id").first()
        trace.request_id = usage_log.request_id
        trace.upstream_request_id = str((usage_log.raw_payload or {}).get("upstream_request_id") or "")
        trace.user = usage_log.user
        trace.api_key = api_key
        trace.user_identifier = (
            usage_log.user.username if usage_log.user else usage_log.user_identifier
        )[:150]
        trace.api_key_identifier = (
            usage_log.api_key_identifier
            or getattr(api_key, "token_masked", "")
            or getattr(api_key, "token_prefix", "")
        )[:150]
        if not trace.model_name:
            trace.model_name = usage_log.model_name
        trace.save(update_fields=[
            "request_id", "upstream_request_id", "user", "api_key",
            "user_identifier", "api_key_identifier", "model_name",
        ])
        fixed += 1
    return fixed


def prune_query_traces(max_rows=None, max_age_days=None):
    """FIFO retention: keep the newest rows, drop anything past its age."""
    max_rows = max_rows or _setting_int("QUERY_TRACE_MAX_ROWS", 50000)
    max_age_days = max_age_days or _setting_int("QUERY_TRACE_MAX_AGE_DAYS", 30)
    deleted_expired = 0
    if max_age_days > 0:
        cutoff = django_timezone.now() - timedelta(days=max_age_days)
        deleted_expired = QueryTrace.objects.filter(request_time__lt=cutoff).delete()[0]
    deleted_overflow = 0
    if max_rows > 0:
        overflow_ids = list(
            QueryTrace.objects.order_by("-id").values_list("id", flat=True)[max_rows:]
        )
        while overflow_ids:
            chunk, overflow_ids = overflow_ids[:2000], overflow_ids[2000:]
            deleted_overflow += QueryTrace.objects.filter(id__in=chunk).delete()[0]
    return deleted_expired, deleted_overflow


def sync_query_traces(keeper_events=None, limit=None, dry_run=False, prune=True):
    """Import request-log files into ``QueryTrace`` and apply FIFO retention."""
    job = UsageImportJob.objects.create(
        source_type=UsageImportJob.SOURCE_API, source_name="cliproxy-request-log"
    )
    imported = skipped = failed = unattributed = 0
    errors: list[str] = []

    if not query_trace_enabled():
        job.summary = "query trace disabled"
        job.save(update_fields=["summary"])
        return job

    since = None
    newest = QueryTrace.objects.order_by("-request_time").values_list("request_time", flat=True).first()
    if newest:
        since = newest - timedelta(hours=6)
    else:
        since = django_timezone.now() - timedelta(hours=_setting_int("QUERY_TRACE_BACKFILL_HOURS", 24))

    candidates = iter_request_logs(since=since)
    if limit:
        candidates = candidates[-int(limit):]

    known = set()
    short_ids = [item[3] for item in candidates]
    for index in range(0, len(short_ids), 500):
        known.update(
            QueryTrace.objects.filter(short_id__in=short_ids[index:index + 500]).values_list("short_id", flat=True)
        )

    # the keeper events imported by this run are the freshest source, but a usage row
    # that arrived earlier still has to be usable: merge instead of picking one
    attribution = _attribution_from_recent_logs(since=since)
    attribution.update(_attribution_from_keeper_events(keeper_events))

    max_file_bytes = _setting_int("QUERY_TRACE_MAX_FILE_BYTES", 4 * 1024 * 1024)
    created = []
    for request_time, path, size, short_id, prefix in candidates:
        if short_id in known:
            skipped += 1
            continue
        try:
            if size and size > max_file_bytes:
                raw = Path(path).read_bytes()[:max_file_bytes].decode("utf-8", "replace")
            else:
                raw = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            failed += 1
            errors.append(f"{Path(path).name}: {exc}")
            continue

        try:
            parsed = parse_request_log(raw)
            details = build_query_payload(parsed["request_body"])
            usage_log = attribution.get(short_id) or _fallback_attribution(short_id, request_time)
            if usage_log is None:
                unattributed += 1
            api_key = getattr(usage_log, "api_key", None) if usage_log else None
            if api_key is None and usage_log is not None:
                api_key = (
                    APIKey.objects.filter(user=usage_log.user).order_by("id").first()
                    if usage_log.user_id
                    else None
                )
            # the keeper event usually carries the masked key; when it does not, fall back to
            # the key row itself so leak triage still sees which key produced the request
            api_key_identifier = (usage_log.api_key_identifier if usage_log else "") or getattr(
                api_key, "token_masked", ""
            ) or getattr(api_key, "token_prefix", "")
            trace = QueryTrace(
                short_id=short_id,
                request_id=usage_log.request_id if usage_log else "",
                upstream_request_id=str(((usage_log.raw_payload or {}) if usage_log else {}).get("upstream_request_id") or ""),
                user=usage_log.user if usage_log else None,
                api_key=api_key,
                user_identifier=(
                    usage_log.user.username
                    if usage_log and usage_log.user
                    else (usage_log.user_identifier if usage_log else "")
                ),
                api_key_identifier=api_key_identifier[:150],
                model_name=details.get("model") or (usage_log.model_name if usage_log else ""),
                endpoint=(parsed["url"] or f"/{prefix.replace('-', '/')}")[:120],
                log_name=Path(path).name[:200],
                log_bytes=int(size or 0),
                query_text=details["query_text"],
                query_chars=details["query_chars"],
                truncated=details["truncated"],
                redacted=details["redacted"],
                message_count=details["message_count"],
                has_attachments=details["has_attachments"],
                status_code=parsed["status_code"],
                request_time=parsed["request_time"] or request_time,
            )
            if dry_run:
                created.append(trace)
                imported += 1
                known.add(short_id)
                continue
            trace.save()
            created.append(trace)
            known.add(short_id)
            imported += 1
        except Exception as exc:  # noqa: BLE001 - one bad file must not stop the sync
            failed += 1
            errors.append(f"{Path(path).name}: {exc}")

    deleted_expired = deleted_overflow = 0
    if prune and not dry_run:
        deleted_expired, deleted_overflow = prune_query_traces()

    reattributed = 0
    if not dry_run:
        reattributed = reattribute_query_traces(since=since)

    job.imported_count = imported
    job.skipped_count = skipped
    job.failed_count = failed
    summary_lines = []
    if unattributed:
        summary_lines.append(f"unattributed={unattributed}")
    if reattributed:
        summary_lines.append(f"reattributed={reattributed}")
    if deleted_expired or deleted_overflow:
        summary_lines.append(f"pruned: expired={deleted_expired} overflow={deleted_overflow}")
    if dry_run:
        summary_lines.append("dry-run")
    if errors:
        summary_lines.extend(errors[:20])
    job.summary = "\n".join(summary_lines)
    job.save(update_fields=["imported_count", "skipped_count", "failed_count", "summary"])
    return job
