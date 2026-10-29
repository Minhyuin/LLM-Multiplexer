"""Public invite registration and the administrator's invitation desk."""
from django.contrib import messages
from django.contrib.auth import login
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import Count
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods, require_POST

from core.utils import admin_required
from users.models import InvitationCode
from users.registration import register_member, sync_registration_key
from users.registration_forms import InvitationCodeForm, RegistrationForm


@sensitive_post_parameters("password1", "password2", "invitation_code")
@never_cache
@require_http_methods(["GET", "POST"])
def register(request):
    if request.user.is_authenticated:
        return redirect("my-quota")
    form = RegistrationForm(request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        try:
            user = register_member(form.cleaned_data)
        except ValidationError as exc:
            form.add_error(None, "；".join(exc.messages))
        except IntegrityError:
            form.add_error("username", "该用户名已被注册，请检查后重试。")
        else:
            synced = sync_registration_key(user.pk)
            login(request, user)
            if synced:
                messages.success(request, "注册成功！API Key 已自动创建并启用，请在下方调用信息中复制。")
            else:
                messages.warning(request, "注册成功，API Key 正在同步，系统将自动重试。同步完成后可在我的额度中查看。")
            return redirect("my-quota")
    return render(request, "registration/register.html", {"form": form})


@admin_required
@require_http_methods(["GET", "POST"])
def invitation_list(request):
    form = InvitationCodeForm(request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        invitation = form.save(commit=False)
        invitation.created_by = request.user
        try:
            with transaction.atomic():
                invitation.save()
        except (IntegrityError, ValidationError):
            form.add_error("code", "邀请码已存在或设置无效，请检查后重新发布。")
        else:
            messages.success(request, "邀请码已发布。可在有效期内供多人注册，到期自动失效。")
            return redirect("admin-invitation-list")
    invitations = InvitationCode.objects.select_related("created_by").annotate(
        registration_count=Count("registrations")
    ).order_by("-created_at", "-pk")
    page = Paginator(invitations, 25).get_page(request.GET.get("page"))
    return render(request, "users/admin_invitation_list.html", {"form": form, "page_obj": page})


@admin_required
@require_POST
def invitation_revoke(request, invitation_id):
    with transaction.atomic():
        invitation = get_object_or_404(InvitationCode.objects.select_for_update(), pk=invitation_id)
        invitation.is_active = False
        invitation.save(update_fields=["is_active"])
    messages.success(request, "邀请码已停用。已注册的账户不受影响。")
    return redirect("admin-invitation-list")
