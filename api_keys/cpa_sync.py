"""Push member key changes from the portal to CLIProxyAPI (CPA)."""
import requests
from django.conf import settings


def push_key_rotation_to_cliproxy(old_key: str, new_key: str) -> None:
    """Rotate (or first-time append) a member key in CPA's access key list.

    CPA v8 semantics of PATCH /v0/management/api-keys with {"old","new"}:
    replaces the first entry equal to old_key; when old_key is not present
    the new key is appended. Either way only this one entry is touched —
    no full-list replace, so concurrent edits elsewhere can't be clobbered.
    The change persists to config.yaml and hot-reloads in the running process.

    Raises requests exceptions on transport/HTTP failure; the caller decides
    how prominently to surface the error.
    """
    if not new_key:
        return
    url = settings.CLIPROXY_MANAGEMENT_BASE_URL.rstrip("/") + "/v0/management/api-keys"
    resp = requests.patch(
        url,
        json={"old": old_key or "", "new": new_key},
        headers={"Authorization": "Bearer " + settings.CLIPROXY_MANAGEMENT_KEY},
        timeout=10,
    )
    resp.raise_for_status()
