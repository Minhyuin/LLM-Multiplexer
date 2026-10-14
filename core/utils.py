from functools import wraps

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied


def is_portal_admin(user) -> bool:
    if not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_staff", False):
        return True
    profile = getattr(user, "profile", None)
    return bool(profile and profile.role == "admin")


def admin_required(view_func):
    @login_required
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if is_portal_admin(request.user):
            return view_func(request, *args, **kwargs)
        raise PermissionDenied("需要管理员权限")

    return _wrapped
