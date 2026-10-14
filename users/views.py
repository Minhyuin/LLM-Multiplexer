from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

import logging

from api_keys.cpa_sync import push_key_rotation_to_cliproxy
from api_keys.models import APIKey, APIKeyAlias
from core.utils import admin_required
from quota.models import UserQuotaSnapshot
from django.db.models import Sum
from users.models import Profile

logger = logging.getLogger(__name__)

from users.forms import AdminProfileQuotaForm, AdminUserBoundKeyForm, AdminUserKeyOnlyForm, AdminUserPasswordForm, AdminUserWithCliproxyKeyForm, ProfileForm
from users.middleware import get_real_user, is_portal_admin, start_view_as, stop_view_as


@login_required
def profile_detail(request):
    profile = request.user.profile
    if request.method == "POST":
        form = ProfileForm(request.POST, instance=profile)
        if form.is_valid():
            form.save()
            messages.success(request, "个人资料已更新")
            return redirect("profile")
    else:
        form = ProfileForm(instance=profile)

    snapshots = UserQuotaSnapshot.objects.filter(user=request.user)[:6]
    return render(request, "users/profile.html", {"form": form, "snapshots": snapshots})


@admin_required
def admin_user_list(request):
    users = User.objects.select_related("profile").order_by("username")

    # Filters
    group_filter = request.GET.get("group", "all")
    grade_filter = request.GET.get("grade", "all")
    status_filter = request.GET.get("status", "all")

    if group_filter != "all":
        users = users.filter(profile__lab_group=group_filter)
    if grade_filter != "all":
        users = users.filter(profile__grade=grade_filter)
    if status_filter == "active":
        users = users.filter(is_active=True)
    elif status_filter == "disabled":
        users = users.filter(is_active=False)

    # Get filter options
    all_groups = sorted(set(
        Profile.objects.exclude(lab_group="").values_list("lab_group", flat=True).distinct()
    ))
    all_grades = sorted(set(
        Profile.objects.exclude(grade="").values_list("grade", flat=True).distinct()
    ))

    monthly_usage = {
        row["usage_logs__user"]: row["total_tokens"]
        for row in User.objects.filter(usage_logs__isnull=False)
        .values("usage_logs__user")
        .annotate(total_tokens=Sum("usage_logs__total_tokens"))
        .values("usage_logs__user", "total_tokens")
    }
    return render(request, "users/admin_user_list.html", {
        "users": users,
        "monthly_usage": monthly_usage,
        "group_filter": group_filter,
        "grade_filter": grade_filter,
        "status_filter": status_filter,
        "all_groups": all_groups,
        "all_grades": all_grades,
    })


@admin_required
def admin_user_edit(request, user_id):
    target_user = get_object_or_404(User.objects.select_related("profile"), pk=user_id)
    bound_key = target_user.api_keys.order_by("-created_at").first()
    # ModelForm.is_valid() (via _post_clean -> construct_instance) copies the
    # submitted plaintext onto bound_key, so the old identifiers must be
    # captured before any form validation runs.
    if bound_key:
        old_plaintext = bound_key.token_plaintext or ""
        old_token_hash = bound_key.token_hash
        old_token_prefix = bound_key.token_prefix
        old_token_masked = bound_key.token_masked
    else:
        old_plaintext = old_token_hash = old_token_prefix = old_token_masked = ""
    if request.method == "POST":
        form = AdminProfileQuotaForm(request.POST, instance=target_user.profile, prefix="profile", user_instance=target_user)
        password_form = AdminUserPasswordForm(request.POST, prefix="password")
        key_form = AdminUserBoundKeyForm(request.POST, instance=bound_key, prefix="key") if bound_key else AdminUserKeyOnlyForm(request.POST, prefix="key")
        if "save_profile" in request.POST and form.is_valid():
            form.save()
            messages.success(request, "用户信息已更新")
            return redirect("admin-user-list")
        if "save_key" in request.POST and key_form.is_valid() and bound_key:
            new_plaintext = key_form.cleaned_data["token_plaintext"]
            with transaction.atomic():
                if old_plaintext and old_plaintext != new_plaintext:
                    # Rotation: keep the outgoing key attributable as an alias.
                    APIKeyAlias.objects.get_or_create(
                        token_hash=old_token_hash,
                        defaults={
                            "user": target_user,
                            "token_prefix": old_token_prefix,
                            "token_masked": old_token_masked,
                            "token_plaintext": old_plaintext,
                            "note": "轮换前密钥，轮换于 " + timezone.now().strftime("%Y-%m-%d %H:%M"),
                        },
                    )
                key = key_form.save(commit=False)
                metadata = APIKey.build_token_metadata(new_plaintext)
                key.token_hash = metadata["token_hash"]
                key.token_prefix = metadata["token_prefix"]
                key.token_masked = metadata["token_masked"]
                key.save()
            messages.success(request, "绑定密钥已更新")
            if new_plaintext != old_plaintext:
                # Best-effort: the panel save must never fail because CPA is
                # unreachable — surface the sync failure as a warning instead.
                try:
                    push_key_rotation_to_cliproxy(old_plaintext, new_plaintext)
                    messages.success(request, "CPA 密钥已同步")
                except Exception as exc:
                    logger.error("CPA key sync failed for user %s: %s", target_user.username, exc)
                    messages.warning(request, f"面板已保存，但同步 CPA 失败，需要手动处理：{exc}")
            return redirect("admin-user-list")
        if "save_password" in request.POST and password_form.is_valid():
            target_user.set_password(password_form.cleaned_data["new_password"])
            target_user.save()
            messages.success(request, "用户密码已更新")
            return redirect("admin-user-list")
        if "delete_user" in request.POST:
            target_user.delete()
            messages.success(request, "用户账号已注销")
            return redirect("admin-user-list")
    else:
        form = AdminProfileQuotaForm(instance=target_user.profile, prefix="profile", user_instance=target_user)
        password_form = AdminUserPasswordForm(prefix="password")
        key_form = AdminUserBoundKeyForm(instance=bound_key, prefix="key") if bound_key else None
    return render(request, "users/admin_user_edit.html", {"target_user": target_user, "form": form, "password_form": password_form, "key_form": key_form, "bound_key": bound_key})


@admin_required
def admin_user_create(request):
    if request.method == "POST":
        form = AdminUserWithCliproxyKeyForm(request.POST)
        if form.is_valid():
            with transaction.atomic():
                user = User.objects.create_user(
                    username=form.cleaned_data["username"],
                    password=form.cleaned_data["password"],
                )
                profile = user.profile
                profile.display_name = form.cleaned_data["display_name"]
                profile.lab_group = form.cleaned_data["lab_group"]
                profile.grade = form.cleaned_data["grade"]
                profile.member_type = form.cleaned_data["member_type"]
                profile.save()

                raw_token = form.cleaned_data["cliproxy_key_plaintext"]
                metadata = APIKey.build_token_metadata(raw_token)
                APIKey.objects.create(
                    user=user,
                    name="API 密钥",
                    token_hash=metadata["token_hash"],
                    token_prefix=metadata["token_prefix"],
                    token_masked=metadata["token_masked"],
                    token_plaintext=raw_token,
                    source=APIKey.SOURCE_CLIPROXY,
                    status=APIKey.STATUS_ACTIVE,
                    note=form.cleaned_data["member_type"],
                )

            messages.success(request, "用户和 CLIProxy Key 绑定已创建")
            return redirect("admin-user-list")
    else:
        form = AdminUserWithCliproxyKeyForm()
    return render(request, "users/admin_user_create.html", {"form": form})


@admin_required
def admin_user_toggle(request, user_id):
    """Toggle user active/disabled status."""
    target_user = get_object_or_404(User, pk=user_id)
    if request.method == "POST":
        target_user.is_active = not target_user.is_active
        target_user.save(update_fields=["is_active"])
        status_text = "启用" if target_user.is_active else "停用"
        messages.success(request, f"用户 {target_user.username} 已{status_text}")
    return redirect("admin-user-list")


@admin_required
def admin_user_delete(request, user_id):
    """Delete a user."""
    target_user = get_object_or_404(User, pk=user_id)
    if request.method == "POST":
        username = target_user.username
        target_user.delete()
        messages.success(request, f"用户 {username} 已删除")
    return redirect("admin-user-list")


@admin_required
def admin_user_batch(request):
    """Batch operations on multiple users."""
    if request.method == "POST":
        action = request.POST.get("batch_action", "")
        user_ids = request.POST.getlist("user_ids")
        if not user_ids:
            messages.warning(request, "未选择任何用户")
            return redirect("admin-user-list")

        users_qs = User.objects.filter(id__in=user_ids, is_staff=False)
        count = users_qs.count()

        if action == "disable":
            users_qs.update(is_active=False)
            messages.success(request, f"已停用 {count} 个用户")
        elif action == "enable":
            users_qs.update(is_active=True)
            messages.success(request, f"已启用 {count} 个用户")
        elif action == "delete":
            users_qs.delete()
            messages.success(request, f"已删除 {count} 个用户")
        else:
            messages.warning(request, "未知操作")

    return redirect("admin-user-list")


@admin_required
@require_POST
def admin_user_view_as_start(request, user_id):
    """Enter a read-only preview of another account (administrators only).

    The real authentication principal stays the administrator in the session;
    ``users.middleware.ViewAsMiddleware`` exposes the target as the effective
    ``request.user`` and blocks every mutating request while it is active.
    """
    target_user = get_object_or_404(User.objects.select_related("profile"), pk=user_id)
    if target_user.pk == request.user.pk:
        messages.warning(request, "无需预览自己的账户视角")
        return redirect("admin-user-list")
    if not target_user.is_active:
        messages.warning(request, f"用户 {target_user.username} 已停用，无法预览其账户视角")
        return redirect("admin-user-list")

    start_view_as(request, target_user)
    messages.success(request, f"已切换到 {target_user.username} 的只读预览视角，写操作已禁用")
    return redirect("home")


@require_POST
def admin_user_view_as_stop(request):
    """Leave view-as mode and return to the administrator perspective.

    Deliberately not wrapped in ``admin_required``: while a perspective is
    borrowed ``request.user`` is the target account, so the authorization check
    runs against the real principal instead.  The check also works when the
    target account was deleted, disabled or demoted mid-session.
    """
    if not is_portal_admin(get_real_user(request)):
        raise PermissionDenied("需要管理员权限")

    stop_view_as(request)
    messages.success(request, "已返回管理员视角")
    return redirect("admin-user-list")
