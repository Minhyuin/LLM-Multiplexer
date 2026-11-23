"""Enforce the daily token quota by suspending the member's proxy key.

The portal never sits in the request path, so the only lever it has is the
proxy's access-key list: CLIProxyAPI accepts ``DELETE ?value=<key>`` (atomic,
removes just that string) and ``PATCH {"old": "", "new": key}`` (appends when
``old`` is absent).  Both are idempotent enough that a crashed run cannot leave
the list half rewritten — we never PUT the whole list.

State is recorded in :class:`quota.models.DailyQuotaUsage` so a restart does not
forget that a key was removed, and so a quota bump restores it on the next tick.
"""

from __future__ import annotations

import logging
from urllib.parse import quote

import requests
from django.conf import settings
from django.contrib.auth import get_user_model
from django.utils import timezone

from api_keys.models import APIKey
from quota.models import DailyQuotaUsage
from quota.services import (
    MODE_BLOCK,
    enforcement_mode,
    resolve_limit,
    today_usage,
)

logger = logging.getLogger(__name__)

TIMEOUT = 10


def _management_url(path: str) -> str:
    base = (getattr(settings, "CLIPROXY_MANAGEMENT_BASE_URL", "") or "").rstrip("/")
    return base + path


def _headers() -> dict:
    return {"Authorization": "Bearer " + (getattr(settings, "CLIPROXY_MANAGEMENT_KEY", "") or "")}


def _configured() -> bool:
    return bool(getattr(settings, "CLIPROXY_MANAGEMENT_BASE_URL", "") and getattr(settings, "CLIPROXY_MANAGEMENT_KEY", ""))


def fetch_proxy_keys() -> list[str]:
    resp = requests.get(_management_url("/v0/management/api-keys"), headers=_headers(), timeout=TIMEOUT)
    resp.raise_for_status()
    payload = resp.json() or {}
    return [str(item) for item in (payload.get("api-keys") or [])]


def remove_proxy_key(key: str) -> None:
    url = _management_url("/v0/management/api-keys?value=" + quote(key, safe=""))
    resp = requests.delete(url, headers=_headers(), timeout=TIMEOUT)
    resp.raise_for_status()


def append_proxy_key(key: str) -> None:
    resp = requests.patch(
        _management_url("/v0/management/api-keys"),
        json={"old": "", "new": key},
        headers=_headers(),
        timeout=TIMEOUT,
    )
    resp.raise_for_status()


def _member_key(user):
    return (
        APIKey.objects.filter(user=user, status=APIKey.STATUS_ACTIVE)
        .exclude(token_plaintext="")
        .order_by("-created_at")
        .first()
    )


def _record_block(user, api_key, used: int, limit: int) -> None:
    """Persist the block *after* the proxy call succeeded (or was unnecessary)."""
    today = timezone.localdate()
    state, _ = DailyQuotaUsage.objects.get_or_create(user=user, day=today)
    state.tokens_used = used
    state.limit = limit
    state.api_key = api_key
    state.blocked_key = api_key.token_plaintext
    state.blocked_at = state.blocked_at or timezone.now()
    state.restored_at = None
    state.save(update_fields=["tokens_used", "limit", "api_key", "blocked_key", "blocked_at", "restored_at", "updated_at"])


def _restore(state, proxy_keys: list[str]) -> bool:
    """Put the member's key back if it is missing. Returns True if it was re-added."""
    key = state.blocked_key or ""
    if not key:
        user_key = _member_key(state.user)
        key = user_key.token_plaintext if user_key else ""
    added = False
    if key and key in proxy_keys:
        added = False
    elif key:
        append_proxy_key(key)
        added = True
    state.restored_at = timezone.now()
    state.save(update_fields=["restored_at", "updated_at"])
    return added


def enforce_daily_quota(now=None) -> dict:
    """One enforcement tick. Never raises: the usage sync must not depend on it."""
    stats = {"mode": "off", "checked": 0, "blocked": 0, "restored": 0, "errors": 0, "skipped": False}
    mode = enforcement_mode()
    stats["mode"] = mode
    if mode != MODE_BLOCK:
        return stats
    if not _configured():
        stats["skipped"] = True
        logger.warning("quota enforcement skipped: cliproxy management api not configured")
        return stats

    usage = today_usage()
    open_states = {
        state.user_id: state
        for state in DailyQuotaUsage.objects.filter(blocked_at__isnull=False, restored_at__isnull=True).select_related("user", "api_key")
    }

    pending_block = []  # (user, api_key, used, limit)
    pending_restore = []  # state
    for user in get_user_model().objects.filter(is_active=True).select_related("profile"):
        stats["checked"] += 1
        limit = resolve_limit(getattr(user, "profile", None))
        used = usage.get(user.id, 0)
        should_block = bool(limit) and used >= limit
        state = open_states.get(user.id)
        is_blocked = bool(state and state.is_blocked)
        if should_block and not is_blocked:
            api_key = _member_key(user)
            if api_key is None:
                continue
            pending_block.append((user, api_key, used, limit))
        elif is_blocked and not should_block:
            pending_restore.append(state)

    if not pending_block and not pending_restore:
        return stats

    try:
        proxy_keys = fetch_proxy_keys()
    except Exception as exc:  # transport/auth failures must not break the sync
        stats["errors"] += 1
        logger.warning("quota enforcement: cannot read proxy keys: %s", exc)
        return stats

    for user, api_key, used, limit in pending_block:
        try:
            if api_key.token_plaintext in proxy_keys:
                remove_proxy_key(api_key.token_plaintext)
                proxy_keys = [k for k in proxy_keys if k != api_key.token_plaintext]
            _record_block(user, api_key, used, limit)
            stats["blocked"] += 1
            logger.info("quota: blocked %s at %s/%s tokens", user.username, used, limit)
        except Exception as exc:
            stats["errors"] += 1
            logger.warning("quota: failed to block %s: %s", user.username, exc)

    for state in pending_restore:
        try:
            if _restore(state, proxy_keys):
                proxy_keys.append(state.blocked_key)
            stats["restored"] += 1
            logger.info("quota: restored %s", state.user.username)
        except Exception as exc:
            stats["errors"] += 1
            logger.warning("quota: failed to restore %s: %s", state.user.username, exc)

    return stats
