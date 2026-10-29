from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from api_keys.forms import APIKeyAdminForm, APIKeyRequestForm, APIKeyReviewForm
from api_keys.models import APIKey, APIKeyRequest
from api_keys.rotation import RotationError, rotate_member_key, self_rotate_state
from core.utils import admin_required, is_portal_admin


@login_required
def api_key_list(request):
    keys = APIKey.objects.filter(user=request.user).select_related("registration_provision").order_by("-created_at")
    latest_plain_token = request.session.pop("latest_plain_token", None)
    return render(request, "api_keys/my_keys.html", {
        "keys": keys,
        "latest_plain_token": latest_plain_token,
        **self_rotate_state(request.user),
    })


@login_required
def api_key_request_create(request):
    if request.method == "POST":
        form = APIKeyRequestForm(request.POST)
        if form.is_valid():
            key_request = form.save(commit=False)
            key_request.user = request.user
            key_request.save()
            messages.success(request, "申请已提交，等待管理员审批")
            return redirect("my-api-keys")
    else:
        form = APIKeyRequestForm()
    return render(request, "api_keys/request_key.html", {"form": form})


@login_required
@require_POST
def rotate_my_api_key(request, key_id):
    if is_portal_admin(request.user):
        raise PermissionDenied("管理员请在用户编辑页更换密钥")
    next_name = "my-api-keys" if request.POST.get("next") == "keys" else "my-quota"
    try:
        new_token = rotate_member_key(request.user, key_id)
    except APIKey.DoesNotExist as exc:
        raise Http404("密钥不存在") from exc
    except RotationError as exc:
        messages.error(request, str(exc))
        return redirect(next_name)
    if next_name == "my-api-keys":
        request.session["latest_plain_token"] = new_token
    messages.success(request, "密钥已更换，旧密钥立即失效。请马上复制新密钥。")
    return redirect(next_name)


@admin_required
def admin_api_key_list(request):
    keys = APIKey.objects.select_related("user").all()
    return render(request, "api_keys/admin_key_list.html", {"keys": keys})


@admin_required
def admin_api_key_create(request, user_id):
    target_user = get_object_or_404(User, pk=user_id)
    if request.method == "POST":
        form = APIKeyAdminForm(request.POST)
        if form.is_valid():
            raw_token = APIKey.generate_token()
            key = form.save(commit=False)
            key.user = target_user
            key.status = form.cleaned_data["status"]
            metadata = APIKey.build_token_metadata(raw_token)
            key.token_hash = metadata["token_hash"]
            key.token_prefix = metadata["token_prefix"]
            key.token_masked = metadata["token_masked"]
            key.token_plaintext = raw_token
            key.save()
            request.session["latest_plain_token"] = raw_token
            messages.success(request, f"已为 {target_user.username} 创建 API Key")
            return redirect("admin-api-key-list")
    else:
        form = APIKeyAdminForm(initial={"status": APIKey.STATUS_ACTIVE})
    return render(request, "api_keys/admin_key_create.html", {"form": form, "target_user": target_user})


@admin_required
def admin_api_key_edit(request, key_id):
    key = get_object_or_404(APIKey.objects.select_related("user"), pk=key_id)
    if request.method == "POST":
        form = APIKeyAdminForm(request.POST, instance=key)
        if form.is_valid():
            form.save()
            messages.success(request, "API Key 已更新")
            return redirect("admin-api-key-list")
    else:
        form = APIKeyAdminForm(instance=key)
    return render(request, "api_keys/admin_key_edit.html", {"form": form, "key": key})


@admin_required
def admin_api_key_request_list(request):
    requests = APIKeyRequest.objects.select_related("user").all()
    return render(request, "api_keys/admin_request_list.html", {"requests": requests})


@admin_required
def admin_api_key_request_review(request, request_id):
    key_request = get_object_or_404(APIKeyRequest.objects.select_related("user"), pk=request_id)
    if request.method == "POST":
        form = APIKeyReviewForm(request.POST, instance=key_request)
        if form.is_valid():
            review = form.save(commit=False)
            review.reviewed_at = timezone.now()
            review.save()
            if review.status == APIKeyRequest.STATUS_APPROVED:
                raw_token = APIKey.generate_token()
                metadata = APIKey.build_token_metadata(raw_token)
                APIKey.objects.create(
                    user=review.user,
                    name=review.name,
                    token_hash=metadata["token_hash"],
                    token_prefix=metadata["token_prefix"],
                    token_masked=metadata["token_masked"],
                    token_plaintext=raw_token,
                    status=APIKey.STATUS_ACTIVE,
                    note=f"由申请 #{review.id} 自动创建",
                )
                request.session["latest_plain_token"] = raw_token
            messages.success(request, "申请审批已完成")
            return redirect("admin-api-key-request-list")
    else:
        form = APIKeyReviewForm(instance=key_request)
    return render(request, "api_keys/admin_request_review.html", {"form": form, "key_request": key_request})
