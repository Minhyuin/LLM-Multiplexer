from django.conf import settings

from .version import static_asset_version


def site_settings(request):
    return {
        "PUBLIC_DASHBOARD_ENABLED": settings.PUBLIC_DASHBOARD_ENABLED,
        "PUBLIC_DASHBOARD_MASK_USERNAMES": settings.PUBLIC_DASHBOARD_MASK_USERNAMES,
        "static_asset_version": static_asset_version(),
    }
