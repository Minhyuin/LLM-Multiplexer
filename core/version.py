from pathlib import Path
import re

from django.conf import settings


RELEASE_TAG = re.compile(r"v?\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?")


def portal_release_version():
    """Read the running release's tag, including after a deployment rollback."""
    try:
        marker = (Path(settings.BASE_DIR) / ".release").read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        marker = ""
    for line in marker.splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip() == "tag":
            tag = value.strip()
            if RELEASE_TAG.fullmatch(tag):
                return tag
            break
    # Container/manual deployments can inject the release tag when no marker exists.
    tag = getattr(settings, "PORTAL_RELEASE_VERSION", "").strip()
    return tag if RELEASE_TAG.fullmatch(tag) else "开发版本"
