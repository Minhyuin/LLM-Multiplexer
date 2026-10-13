from pathlib import Path
import hashlib
import re
from functools import lru_cache

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


# Stylesheets referenced by templates/base.html; their contents define the token below.
STATIC_VERSION_SOURCES = (
    "css/site.css",
    "css/scandinavian.css",
    "css/attachments.css",
)


def _static_roots():
    roots = [Path(settings.BASE_DIR) / "static"]
    for entry in getattr(settings, "STATICFILES_DIRS", ()) or ():
        roots.append(Path(entry[1] if isinstance(entry, (list, tuple)) else entry))
    return roots


@lru_cache(maxsize=1)
def static_asset_version():
    """Cache-busting token for locally served stylesheets.

    Production serves /static/ with a 30-day immutable Cache-Control header, so a
    stylesheet URL that never changes keeps returning the previous release's CSS
    from the browser cache (this is how v1.5.1 shipped a 512px image cap that no
    returning visitor ever loaded). The token pairs the release tag with a digest
    of the stylesheet contents, so it changes on every release and on any CSS edit.
    """
    tag = portal_release_version()
    slug = re.sub(r"[^0-9A-Za-z.+-]", "", tag).strip(".-+") or "dev"
    digest = hashlib.sha256()
    for name in STATIC_VERSION_SOURCES:
        for root in _static_roots():
            try:
                digest.update((root / name).read_bytes())
            except OSError:
                continue
            break
    return f"{slug}-{digest.hexdigest()[:10]}"
