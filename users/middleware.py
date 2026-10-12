"""Admin "view as" (read-only account perspective / preview) support.

The real authentication principal never changes: the session keeps pointing at
the admin account that started the preview.  While such a session is active this
middleware swaps ``request.user`` for the target account so an administrator can
see exactly what that member sees, and it enforces a strict read-only mode so
nothing can be mutated from inside the borrowed perspective.

Placement matters: it must run after ``AuthenticationMiddleware`` (it needs the
real principal) and before ``core.middleware.PortalLoginRequiredMiddleware``
(the effective user must already be resolved when that middleware inspects
``request.user``).
"""

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.shortcuts import render
from django.utils import timezone
from django.utils.cache import add_never_cache_headers

from users.models import Profile

User = get_user_model()

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})

#: Session keys describing the previewed account.  ``_auth_user_id`` keeps
#: pointing at the administrator, so the real principal survives untouched.
VIEW_AS_SESSION_KEY = "portal_view_as_user_id"
VIEW_AS_STARTED_AT_KEY = "portal_view_as_started_at"

#: Unsafe requests that stay available while a preview is active: the button
#: that returns to the administrator and a way to log out.  (Django's own
#: logout views are POST-only in 4.2, and logging out only ends this session.)
ALLOWED_UNSAFE_VIEW_NAMES = frozenset({"view-as-stop", "logout", "admin:logout"})

#: Safe (GET/HEAD) endpoints that still mutate state server-side.  A read-only
#: preview must not trigger them just by loading a page.  See
#: usage.views.auto_sync_status (usage/views.py:118), usage.views.request_events
#: (usage/views.py:154) and dashboard.views.admin_credentials
#: (dashboard/views.py:412).
READ_ONLY_BLOCKED_VIEW_NAMES = frozenset(
    {"auto-sync-status", "request-events", "admin-credentials"}
)

STALE_REASONS = {
    "admin-revoked": "当前账号已不再具有管理员权限，已自动退出只读预览。",
    "target-deleted": "被预览的账号已被删除，已自动返回管理员视角。",
    "target-disabled": "被预览的账号已停用，已自动返回管理员视角。",
    "target-invalid": "预览目标无效，已自动返回管理员视角。",
}


def is_portal_admin(user):
    """True when ``user`` is a portal administrator (staff or ``role=admin``).

    Mirrors ``core.utils.admin_required`` but also requires an active account so
    a disabled/revoked account can never keep borrowed privileges.
    """
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    if not getattr(user, "is_active", False):
        return False
    if user.is_staff:
        return True
    profile = getattr(user, "profile", None)
    return bool(profile is not None and profile.role == Profile.ROLE_ADMIN)


class ViewAsState:
    """Request-scoped snapshot of the view-as (preview) state."""

    def __init__(self):
        self.active = False
        self.admin_user = None
        self.target_user = None
        self.started_at = None
        # Set when a previously stored preview had to be dropped.
        self.stale_reason = None
        self.notice_sent = False

    def __bool__(self):
        return self.active


def get_real_user(request):
    """The authenticated principal behind the request, ignoring any preview."""
    state = getattr(request, "view_as", None)
    if state is not None and state.active and state.admin_user is not None:
        return state.admin_user
    return getattr(request, "user", None)


def start_view_as(request, target_user):
    """Store ``target_user`` as the previewed account of this session."""
    request.session[VIEW_AS_SESSION_KEY] = target_user.pk
    request.session[VIEW_AS_STARTED_AT_KEY] = timezone.now().isoformat()


def stop_view_as(request):
    """Drop the previewed account (idempotent)."""
    request.session.pop(VIEW_AS_SESSION_KEY, None)
    request.session.pop(VIEW_AS_STARTED_AT_KEY, None)


def _coerce_pk(raw):
    """Normalise a stored session identifier; ``None`` when it is unusable."""
    if isinstance(raw, bool) or raw is None or isinstance(raw, (dict, list, tuple)):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


class ViewAsMiddleware:
    """Resolve the effective user for a preview session and keep it read-only."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.view_as = ViewAsState()

        session = getattr(request, "session", None)
        if session is None:
            return self.get_response(request)

        raw_target_id = session.get(VIEW_AS_SESSION_KEY)
        if not raw_target_id:
            return self.get_response(request)

        admin_user = getattr(request, "user", None)
        if not is_portal_admin(admin_user):
            # Privileges revoked (or forged session data): never honour a
            # preview on the strength of what the session claims.
            return self._drop(request, "admin-revoked")

        target_id = _coerce_pk(raw_target_id)
        if target_id is None:
            # Malformed/stale session data must not reach the ORM.
            return self._drop(request, "target-invalid")

        target_user = User.objects.select_related("profile").filter(pk=target_id).first()
        if target_user is None:
            # Target account is gone; fall back to the real administrator so the
            # session can still be recovered/ended normally.
            return self._drop(request, "target-deleted")
        if not target_user.is_active:
            return self._drop(request, "target-disabled")
        if target_user.pk == admin_user.pk:
            return self._drop(request, "target-invalid")

        request.view_as.active = True
        request.view_as.admin_user = admin_user
        request.view_as.target_user = target_user
        request.view_as.started_at = session.get(VIEW_AS_STARTED_AT_KEY)
        # Effective identity for downstream middleware, views and templates.
        request.user = target_user
        return self.get_response(request)

    def _drop(self, request, reason):
        """Abandon a preview that can no longer be honoured (never cache it)."""
        stop_view_as(request)
        request.view_as.stale_reason = reason
        response = self.get_response(request)
        add_never_cache_headers(response)
        return response

    def process_view(self, request, view_func, view_args, view_kwargs):
        state = getattr(request, "view_as", None)
        if state is None:
            return None
        if state.stale_reason:
            self._notify_stale(request, state)
            return None
        if not state.active:
            return None

        view_name = request.resolver_match.view_name if request.resolver_match else ""
        if request.method not in SAFE_METHODS:
            if view_name not in ALLOWED_UNSAFE_VIEW_NAMES:
                return self._read_only_response(request, view_name)
            return None
        if view_name in READ_ONLY_BLOCKED_VIEW_NAMES:
            return self._read_only_response(request, view_name)
        return None

    @staticmethod
    def _notify_stale(request, state):
        if state.notice_sent:
            return
        state.notice_sent = True
        text = STALE_REASONS.get(state.stale_reason)
        if text:
            messages.warning(request, text)

    @staticmethod
    def _read_only_response(request, view_name=""):
        return render(request, "users/view_as_blocked.html", {"blocked_view_name": view_name}, status=403)
