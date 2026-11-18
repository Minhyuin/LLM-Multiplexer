from django.conf import settings

from .version import static_asset_version


def site_settings(request):
    # Imported lazily: fingerprints reads SiteSettings rows, so it must not be
    # pulled in while the app registry is still being populated.
    from fingerprints.config import enabled as fingerprint_enabled

    return {
        "PUBLIC_DASHBOARD_ENABLED": settings.PUBLIC_DASHBOARD_ENABLED,
        "PUBLIC_DASHBOARD_MASK_USERNAMES": settings.PUBLIC_DASHBOARD_MASK_USERNAMES,
        "PORTAL_GITHUB_RELEASES_URL": settings.PORTAL_GITHUB_RELEASES_URL,
        "FINGERPRINT_ENABLED": fingerprint_enabled(),
        "static_asset_version": static_asset_version(),
    }
