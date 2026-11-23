"""Daily token quota: limits, aggregation and admin-side bulk updates."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from django.conf import settings
from django.db.models import Sum
from django.utils import timezone

from core.models import SiteSettings
from core.site_settings import get_override

DEFAULT_DAILY_TOKENS = 200_000_000
MODE_BLOCK = "block"
MODE_WARN = "warn"
MODE_OFF = "off"
MODE_CHOICES = (MODE_BLOCK, MODE_WARN, MODE_OFF)


def default_daily_limit() -> int:
    """The site-wide daily cap in tokens; falls back to 200M when unset."""
    value = get_override("daily_token_quota_default", None)
    if value is None:
        return DEFAULT_DAILY_TOKENS
    return int(value)


def enforcement_mode() -> str:
    value = get_override("quota_enforcement", None)
    if value not in MODE_CHOICES:
        return MODE_BLOCK
    return value


def enforcement_enabled() -> bool:
    return enforcement_mode() != MODE_OFF


def resolve_limit(profile) -> int:
    """A member's effective cap: their own value, else the site default."""
    if profile is None:
        return default_daily_limit()
    value = getattr(profile, "daily_token_quota", None)
    if value is None:
        return default_daily_limit()
    return int(value)


def local_day_bounds(day: date | None = None):
    """Return ``(start, end)`` aware datetimes for a local calendar day."""
    current = day or timezone.localdate()
    start = datetime.combine(current, datetime.min.time())
    start = timezone.make_aware(start, timezone.get_current_timezone())
    return start, start + timedelta(days=1)


def today_usage() -> dict[int, int]:
    """Map user id -> tokens used today, in one aggregate query."""
    from usage.models import UsageLog

    start, end = local_day_bounds()
    rows = (
        UsageLog.objects.filter(request_time__gte=start, request_time__lt=end, user__isnull=False)
        .values("user_id")
        .annotate(total=Sum("total_tokens"))
    )
    return {row["user_id"]: int(row["total"] or 0) for row in rows}


def month_usage(user_id: int) -> dict[str, int]:
    """Tokens and request count for the current local month."""
    from django.db.models import Count

    from usage.models import UsageLog

    now = timezone.localtime()
    start, _ = local_day_bounds(now.date().replace(day=1))
    agg = UsageLog.objects.filter(user_id=user_id, request_time__gte=start).aggregate(
        tokens=Sum("total_tokens"), requests=Count("id")
    )
    return {"tokens": int(agg.get("tokens") or 0), "requests": int(agg.get("requests") or 0)}


def set_all_daily_quota(value: int) -> int:
    """Overwrite every member's cap with ``value`` and return the row count.

    This is the "统一设定" action: it deliberately clobbers per-member overrides,
    because that is what the admin asked for.
    """
    from users.models import Profile

    value = max(int(value), 0)
    SiteSettings.load()  # make sure the singleton row exists
    SiteSettings.objects.filter(pk=1).update(daily_token_quota_default=value)
    return Profile.objects.update(daily_token_quota=value)


def quota_state(user, day: date | None = None):
    from quota.models import DailyQuotaUsage

    return DailyQuotaUsage.objects.filter(user=user, day=day or timezone.localdate()).first()


def members_over_quota() -> int:
    """How many active members have already reached their cap today."""
    from django.contrib.auth import get_user_model

    usage = today_usage()
    count = 0
    for user in get_user_model().objects.filter(is_active=True).select_related("profile"):
        limit = resolve_limit(getattr(user, "profile", None))
        if limit and usage.get(user.id, 0) >= limit:
            count += 1
    return count


def member_quota(user, usage: dict[int, int] | None = None) -> dict:
    """Everything a template needs to render one member's daily quota."""
    profile = getattr(user, "profile", None)
    limit = resolve_limit(profile)
    used = (usage if usage is not None else today_usage()).get(user.id, 0)
    state = quota_state(user)
    blocked = bool(state and state.is_blocked)
    return {
        "limit": limit,
        "used": used,
        "remaining": max(limit - used, 0),
        "percent": (used / limit * 100) if limit else 0.0,
        "over": bool(limit) and used >= limit,
        "blocked": blocked,
        "mode": enforcement_mode(),
    }
