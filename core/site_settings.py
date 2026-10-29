"""Resolve the public API address shown to members.

The address is intentionally not an environment variable: the same release may
be reached through several hostnames.  An admin-editable override lives in
``SiteSettings``; when it is blank the request's own host is the fallback.
"""

from django.conf import settings
from django.core.exceptions import DisallowedHost

from core.models import SiteSettings


def get_override(field: str, default=None):
    """Return a saved admin override, or ``default`` when the field is blank."""
    value = SiteSettings.objects.filter(pk=1).values_list(field, flat=True).first()
    return default if value is None else value


def query_trace_enabled() -> bool:
    default = bool(getattr(settings, "QUERY_TRACE_ENABLED", True))
    return bool(get_override("query_trace_enabled", default))


def lab_group_choices():
    """Return configured group choices, falling back to the built-in four."""
    from users.models import Profile

    raw = get_override("lab_groups", "") or ""
    names = [line.strip() for line in raw.splitlines() if line.strip()]
    if not names:
        return tuple(Profile.GROUP_CHOICES)
    seen = set()
    choices = []
    for name in names:
        if name not in seen:
            seen.add(name)
            choices.append((name, name))
    return tuple(choices)


def normalize_orphan_lab_groups() -> int:
    """Move profiles whose group disappeared into the universal bucket.

    This runs when an admin saves the group list, so a group rename/removal
    cannot leave members with a value that every form would reject.
    """
    from users.models import Profile

    valid = {value for value, _ in lab_group_choices()}
    return (
        Profile.objects.exclude(lab_group__in=valid)
        .exclude(lab_group=Profile.GROUP_UNASSIGNED)
        .update(lab_group=Profile.GROUP_UNASSIGNED)
    )


def configured_public_api_base_url() -> str:
    """Return the saved origin, or ``""`` when automatic detection is active."""
    value = (
        SiteSettings.objects.filter(pk=1)
        .values_list("public_api_base_url", flat=True)
        .first()
    )
    return (value or "").strip().rstrip("/")


def public_api_base_url(request=None) -> str:
    """Return the public origin without the API version suffix."""
    configured = configured_public_api_base_url()
    if configured:
        return configured
    if request is None:
        return ""
    try:
        host = request.get_host()
    except DisallowedHost:
        return ""
    return f"{request.scheme}://{host}".rstrip("/")


def public_api_v1_url(request=None) -> str:
    """Return the Base URL members paste into an OpenAI-compatible client."""
    base = public_api_base_url(request)
    return f"{base}/v1" if base else ""
