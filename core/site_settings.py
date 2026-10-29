"""Resolve the public API address shown to members.

The address is intentionally not an environment variable: the same release may
be reached through several hostnames.  An admin-editable override lives in
``SiteSettings``; when it is blank the request's own host is the fallback.
"""

from django.core.exceptions import DisallowedHost

from core.models import SiteSettings


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
