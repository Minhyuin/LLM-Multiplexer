from collections import Counter
from datetime import timedelta
from urllib.parse import urlencode

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count, Q, Sum
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.template.loader import render_to_string
from django.utils import timezone

from core.key_identifiers import key_index, key_suffix
from core.utils import admin_required
from dashboard.forms import ModelPricingForm
from dashboard.model_catalog import collect_available_models
from dashboard.models import ModelPricing
from usage.models import QueryTrace, UsageImportJob, UsageLog
from usage.services import aggregate_dashboard


def _get_range_start(range_name, request=None):
    now = timezone.localtime()
    if range_name == "today":
        return now.replace(hour=0, minute=0, second=0, microsecond=0), None
    if range_name == "this_month":
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0), None
    if range_name == "last_30_days":
        return now - timedelta(days=30), None
    if range_name == "all":
        return None, None
    if range_name == "custom" and request:
        from django.utils.dateparse import parse_date
        start_str = request.GET.get("start", "")
        end_str = request.GET.get("end", "")
        start_date = parse_date(start_str)
        end_date = parse_date(end_str)
        if start_date and end_date:
            start_dt = timezone.make_aware(timezone.datetime(start_date.year, start_date.month, start_date.day))
            end_dt = timezone.make_aware(timezone.datetime(end_date.year, end_date.month, end_date.day, 23, 59, 59))
            return start_dt, end_dt
    # default: last_7_days
    return now - timedelta(days=7), None


def _build_personal_dashboard_summary(user, visible_logs, personal_logs):
    user_rank_rows = list(
        visible_logs.exclude(user=None)
        .values("user_id", "user__username", "user__profile__display_name")
        .annotate(total_tokens=Sum("total_tokens"))
        .order_by("-total_tokens")
    )
    user_rank = len(user_rank_rows)
    total_users = len(user_rank_rows)
    for index, row in enumerate(user_rank_rows, start=1):
        if row["user_id"] == user.id:
            user_rank = index
            break

    model_usage = list(
        personal_logs.values("model_name")
        .annotate(total_tokens=Sum("total_tokens"))
        .order_by("-total_tokens")
    )

    return {
        "total_tokens": sum(item["total_tokens"] or 0 for item in model_usage),
        "models": model_usage,
        "rank": user_rank,
        "total_users": total_users,
        "model_count": len(model_usage),
        "top_model": model_usage[0]["model_name"] if model_usage else "暂无",
    }


def _display_name_strict(display_name, username):
    value = (display_name or "").strip()
    if value:
        return value
    return "未设置显示名"


def _get_filter_options():
    """Get group and grade filter options from Profile (all registered, not just those with usage)."""
    from users.models import Profile
    groups = sorted({
        group for group in
        Profile.objects.exclude(lab_group="")
        .values_list("lab_group", flat=True).distinct()
        if group in {"电控", "算法", "机械", "宣传"}
    })
    grades = sorted({
        grade for grade in
        Profile.objects.exclude(grade="")
        .values_list("grade", flat=True).distinct()
        if grade
    })
    return groups, grades


def _enrich_display_names(data):
    """Add display_name to user_rank, recent_errors, recent_active_users."""
    for row in data.get("user_rank", []):
        row["display_name"] = _display_name_strict(row.get("user__profile__display_name"), row.get("user__username"))
    for item in data.get("recent_errors", []):
        if item.user_id and hasattr(item.user, "profile"):
            item.display_name = _display_name_strict(item.user.profile.display_name, item.user.username)
        else:
            item.display_name = "-"
    for row in data.get("recent_active_users", []):
        row["display_name"] = _display_name_strict(row.get("user__profile__display_name"), row.get("user__username"))


def _dashboard_scope(request, admin=False):
    range_name = request.GET.get("range", "today")
    group_name = request.GET.get("group", "all")
    grade_name = request.GET.get("grade", "all")
    start, end = _get_range_start(range_name, request)
    all_logs = UsageLog.objects.exclude(user=None)
    if start:
        all_logs = all_logs.filter(request_time__gte=start)
    if end:
        all_logs = all_logs.filter(request_time__lte=end)
    visible = all_logs
    if not admin and not request.user.is_staff:
        visible = visible.filter(user__profile__is_dashboard_visible=True)
    if group_name != "all":
        visible = visible.filter(user__profile__lab_group=group_name)
    if grade_name != "all":
        visible = visible.filter(user__profile__grade=grade_name)
    return all_logs, visible, {
        "range_name": range_name, "group_name": group_name, "grade_name": grade_name,
        "custom_start": request.GET.get("start", ""), "custom_end": request.GET.get("end", ""),
    }


def _dashboard_shell(request, admin=False):
    from django.urls import reverse
    _, _, context = _dashboard_scope(request, admin)
    groups, grades = _get_filter_options()
    context.update(group_options=groups, grade_options=grades, dashboard_is_admin=admin or request.user.is_staff,
                   module_base_url=reverse("admin-dashboard" if admin else "public-dashboard") + "modules/",
                   model_usage_url=reverse("admin-model-usage" if admin else "public-model-usage"))
    # Statistics endpoints only read data; do not start an upstream import on page load.
    return render(request, "dashboard/admin_dashboard.html" if admin else "dashboard/public_dashboard.html", context)


@login_required
def public_dashboard(request):
    if not settings.PUBLIC_DASHBOARD_ENABLED and not request.user.is_staff:
        return render(request, "dashboard/dashboard_unavailable.html")
    return _dashboard_shell(request)


@admin_required
def admin_dashboard(request):
    return _dashboard_shell(request, admin=True)


DASHBOARD_MODULES = {
    # ``rankings`` carries both halves of the side-by-side ranking row: the model bar
    # chart needs ``models`` and the member bar chart needs ``users``.
    "summary": {"summary"}, "trend": {"trend", "models"},
    "rankings": {"models", "users"}, "keys": {"keys"}, "errors": {"errors"}, "active": {"active"},
}


def _dashboard_module(request, module, admin=False):
    from django.http import HttpResponseBadRequest, HttpResponseForbidden, HttpResponseNotFound
    from django.template.loader import render_to_string
    from django.http import JsonResponse
    if module not in {*DASHBOARD_MODULES, "personal"}:
        return HttpResponseNotFound()
    if module == "active" and not (admin or request.user.is_staff or request.user.profile.role == "admin"):
        return HttpResponseForbidden()
    all_logs, visible, context = _dashboard_scope(request, admin)
    context["dashboard_is_admin"] = admin or request.user.is_staff
    if module == "personal":
        if admin or request.user.is_staff:
            data = aggregate_dashboard(visible, sections={"models"})
            context["dashboard"] = data
            template = "dashboard/partials/module_error_models.html"
        else:
            context["personal_summary"] = _build_personal_dashboard_summary(request.user, visible, all_logs.filter(user=request.user))
            template = "dashboard/partials/module_personal.html"
    else:
        bucket = "hour" if context["range_name"] == "today" else "day"
        # Long histories use calendar months to bound chart payload size.
        if context["range_name"] == "all":
            bucket = "month"
        elif context["range_name"] == "custom":
            start, end = _get_range_start("custom", request)
            if start and end and (end - start).days > 180:
                bucket = "month"
        try:
            page = max(1, int(request.GET.get("page", "1")))
        except ValueError:
            return HttpResponseBadRequest("无效页码")
        data = aggregate_dashboard(visible, trend_bucket=bucket, sections=DASHBOARD_MODULES[module],
                                   key_page=page if module == "keys" else None, key_page_size=25)
        _enrich_display_names(data)
        context["dashboard"] = data
        template = "dashboard/partials/module_" + module + ".html"
    response = JsonResponse({"html": render_to_string(template, context, request=request), "module": module})
    response["Cache-Control"] = "private, no-store"
    return response


@login_required
def public_dashboard_module(request, module):
    if request.method not in {"GET", "HEAD"}:
        from django.http import HttpResponseNotAllowed
        return HttpResponseNotAllowed(["GET", "HEAD"])
    if not settings.PUBLIC_DASHBOARD_ENABLED and not request.user.is_staff:
        from django.http import HttpResponseForbidden
        return HttpResponseForbidden()
    return _dashboard_module(request, module)


@admin_required
def admin_dashboard_module(request, module):
    if request.method not in {"GET", "HEAD"}:
        from django.http import HttpResponseNotAllowed
        return HttpResponseNotAllowed(["GET", "HEAD"])
    return _dashboard_module(request, module, admin=True)


# Labels mirror the range links rendered by the filter bar so the popover can
# restate the active window without a second lookup table in the template.
MODEL_USAGE_RANGE_LABELS = {
    "today": "今日", "last_7_days": "近 7 天", "this_month": "本月",
    "last_30_days": "近 30 天", "all": "全部时间", "custom": "自定义区间",
}


def _model_usage_range_label(context):
    if context["range_name"] == "custom" and context["custom_start"] and context["custom_end"]:
        return f"{context['custom_start']} 至 {context['custom_end']}"
    return MODEL_USAGE_RANGE_LABELS.get(context["range_name"], "统计范围")


def _model_usage_name(row):
    """Popover names fall back to the login name: a column of placeholders is useless."""
    display_name = (row.get("user__profile__display_name") or "").strip()
    return display_name or row.get("user__username") or "未知成员"


def _model_usage_payload(request, admin=False):
    """Per-user breakdown for one model inside the dashboard's current filters."""
    from django.http import JsonResponse
    model_name = (request.GET.get("model") or "").strip()
    if not model_name:
        return JsonResponse({"error": "缺少模型参数"}, status=400)
    _, visible, context = _dashboard_scope(request, admin)
    logs = visible.filter(model_name=model_name)
    totals = logs.aggregate(total_tokens=Sum("total_tokens"), total_requests=Count("id"))
    total_tokens = int(totals["total_tokens"] or 0)
    total_requests = int(totals["total_requests"] or 0)
    rows = logs.values("user_id", "user__username", "user__profile__display_name").annotate(
        tokens=Sum("total_tokens"), requests=Count("id")
    ).order_by("-tokens", "-requests", "user_id")
    users = []
    for row in rows:
        tokens = int(row["tokens"] or 0)
        users.append({
            "name": _model_usage_name(row),
            "tokens": tokens,
            "requests": int(row["requests"] or 0),
            "share": round(tokens / total_tokens * 100, 1) if total_tokens else 0,
        })
    payload = {
        "model": model_name,
        "range_label": _model_usage_range_label(context),
        "total_tokens": total_tokens,
        "total_requests": total_requests,
        "user_count": len(users),
        "users": users,
    }
    response = JsonResponse(payload)
    response["Cache-Control"] = "private, no-store"
    return response


@login_required
def public_model_usage(request):
    if request.method not in {"GET", "HEAD"}:
        from django.http import HttpResponseNotAllowed
        return HttpResponseNotAllowed(["GET", "HEAD"])
    if not settings.PUBLIC_DASHBOARD_ENABLED and not request.user.is_staff:
        from django.http import HttpResponseForbidden
        return HttpResponseForbidden()
    return _model_usage_payload(request)


@admin_required
def admin_model_usage(request):
    if request.method not in {"GET", "HEAD"}:
        from django.http import HttpResponseNotAllowed
        return HttpResponseNotAllowed(["GET", "HEAD"])
    return _model_usage_payload(request, admin=True)


@admin_required
def model_pricing_admin(request):
    if request.method == "POST":
        form = ModelPricingForm(request.POST)
        if form.is_valid():
            ModelPricing.objects.update_or_create(
                model_name=form.cleaned_data["model_name"],
                defaults={
                    "prompt_price_per_million": form.cleaned_data["prompt_price_per_million"],
                    "completion_price_per_million": form.cleaned_data["completion_price_per_million"],
                    "cached_price_per_million": form.cleaned_data["cached_price_per_million"],
                },
            )
            form = ModelPricingForm()
    else:
        form = ModelPricingForm()
    pricings = ModelPricing.objects.all()
    return render(request, "dashboard/model_pricing_admin.html", {"form": form, "pricings": pricings})


def _fetch_auth_files():
    """Fetch auth files from CPAP management API."""
    import requests as http_requests
    from urllib.parse import urljoin
    headers = {"Authorization": f"Bearer {settings.CLIPROXY_MANAGEMENT_KEY}"}
    resp = http_requests.get(
        urljoin(settings.CLIPROXY_MANAGEMENT_BASE_URL, "/v0/management/auth-files"),
        headers=headers,
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    return data.get("files", []) if isinstance(data, dict) else data


def _fetch_kiro_quota(auth_index):
    """Fetch Kiro quota for a specific auth index."""
    import requests as http_requests
    from urllib.parse import urljoin
    headers = {"Authorization": f"Bearer {settings.CLIPROXY_MANAGEMENT_KEY}"}
    resp = http_requests.get(
        urljoin(settings.CLIPROXY_MANAGEMENT_BASE_URL, f"/v0/management/kiro-quota?auth_index={auth_index}"),
        headers=headers,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def _cpap_api_call(auth_index, method, url, extra_headers=None):
    """Call an external API through CPAP using a specific credential's token."""
    import requests as http_requests
    from urllib.parse import urljoin
    headers = {"Authorization": f"Bearer {settings.CLIPROXY_MANAGEMENT_KEY}"}
    payload = {
        "authIndex": auth_index,
        "method": method,
        "url": url,
    }
    if extra_headers:
        payload["header"] = extra_headers
    resp = http_requests.post(
        urljoin(settings.CLIPROXY_MANAGEMENT_BASE_URL, "/v0/management/api-call"),
        headers=headers,
        json=payload,
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


# Quota API configs per provider (same as cpa-usage-keeper)
QUOTA_CONFIGS = {
    "codex": {
        "method": "GET",
        "url": "https://chatgpt.com/backend-api/wham/usage",
        "headers": {"Authorization": "Bearer $TOKEN$", "Content-Type": "application/json", "User-Agent": "codex_cli_rs/0.76.0 (Debian 13.0.0; x86_64) WindowsTerminal"},
    },
    "gemini-cli": {
        "method": "POST",
        "url": "https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuota",
        "headers": {"Authorization": "Bearer $TOKEN$", "Content-Type": "application/json"},
    },
    "antigravity": {
        "method": "POST",
        "url": "https://daily-cloudcode-pa.googleapis.com/v1internal:fetchAvailableModels",
        "headers": {"Authorization": "Bearer $TOKEN$", "Content-Type": "application/json", "User-Agent": "antigravity/1.11.5 windows/amd64"},
    },
}


def _parse_codex_quota(data):
    """Parse Codex quota response into normalized format."""
    # CPAP returns: {"status_code": 200, "body": "{...json string...}", "header": {...}}
    status_code = data.get("status_code", data.get("statusCode", 0))
    if status_code != 200:
        return None

    body = data.get("body", data.get("bodyText", ""))
    if isinstance(body, str):
        import json as json_mod
        try:
            body = json_mod.loads(body)
        except (ValueError, TypeError):
            return None
    if not isinstance(body, dict):
        return None

    rate_limit = body.get("rate_limit") or body.get("rateLimit") or {}
    result = {"provider": "codex", "plan_type": body.get("plan_type", ""), "windows": []}

    for key, label in [("primary_window", "5h"), ("secondary_window", "Weekly"), ("primaryWindow", "5h"), ("secondaryWindow", "Weekly")]:
        window = rate_limit.get(key)
        if window:
            result["windows"].append({
                "label": label,
                "used_percent": window.get("used_percent", window.get("usedPercent", 0)),
                "reset_after_seconds": window.get("reset_after_seconds", window.get("resetAfterSeconds", 0)),
                "limit_reached": window.get("limit_reached", window.get("limitReached", False)),
            })

    return result if result["windows"] else None


def _parse_gemini_quota(data):
    """Parse Gemini CLI quota response."""
    body = data.get("body", "")
    if isinstance(body, str):
        import json as json_mod
        try:
            body = json_mod.loads(body)
        except (ValueError, TypeError):
            return None
    if not isinstance(body, dict):
        return None

    result = {"provider": "gemini-cli", "windows": []}
    for bucket in body.get("buckets", []):
        remaining = bucket.get("remainingFraction", bucket.get("remaining_fraction", 0))
        result["windows"].append({
            "label": bucket.get("modelId", bucket.get("model_id", "unknown")),
            "used_percent": round((1 - remaining) * 100, 1) if remaining else 0,
            "reset_after_seconds": 0,
            "remaining_fraction": remaining,
        })
    return result if result["windows"] else None


# --- Quota cache and auto-refresh for codex ---
import threading
import time as _time

_quota_cache = {}  # {auth_index: {"data": parsed_quota, "timestamp": epoch_float}}
_quota_refresh_lock = threading.Lock()
_last_auto_refresh = 0.0
_AUTO_REFRESH_INTERVAL = 60 * 60  # 60 minutes


def _auto_refresh_codex_quotas():
    """Background auto-refresh of all codex credential quotas every 30 minutes.
    Only runs between 10:00-22:00 local time, only for enabled credentials."""
    global _last_auto_refresh
    now = _time.time()
    if now - _last_auto_refresh < _AUTO_REFRESH_INTERVAL:
        return

    # Only run between 10:00 and 22:00 local time
    from django.utils import timezone as tz
    local_hour = tz.localtime().hour
    if local_hour < 10 or local_hour >= 22:
        return

    if not _quota_refresh_lock.acquire(blocking=False):
        return

    try:
        _last_auto_refresh = now
        t = threading.Thread(target=_do_refresh_all_codex, daemon=True)
        t.start()
    finally:
        _quota_refresh_lock.release()


def _do_refresh_all_codex():
    """Fetch quota for all enabled codex credentials and cache results."""
    import logging
    logger = logging.getLogger(__name__)
    try:
        files = _fetch_auth_files()
    except Exception:
        return

    # Only refresh enabled (not disabled) codex credentials
    codex_files = [f for f in files if f.get("provider") == "codex" and not f.get("disabled")]
    config = QUOTA_CONFIGS.get("codex")
    if not config:
        return

    for f in codex_files:
        auth_index = f.get("auth_index", "")
        if not auth_index:
            continue
        try:
            data = _cpap_api_call(auth_index, config["method"], config["url"], config.get("headers"))
            parsed = _parse_codex_quota(data)
            if parsed:
                _quota_cache[auth_index] = {"data": parsed, "timestamp": _time.time()}
        except Exception as exc:
            logger.debug(f"Auto-refresh quota failed for {auth_index}: {exc}")
        # 5 second delay between requests to avoid rate limiting and memory pressure
        _time.sleep(5)


@login_required
def admin_credentials(request):
    """Display all CPAP auth credentials grouped by provider."""
    from collections import defaultdict
    from datetime import timedelta
    from django.utils import timezone as tz

    # Trigger background auto-refresh of codex quotas
    _auto_refresh_codex_quotas()

    try:
        files = _fetch_auth_files()
    except Exception as e:
        files = []
        from django.contrib import messages
        messages.error(request, f"获取凭证失败: {e}")

    now = tz.now()

    # Group by provider
    grouped = defaultdict(list)
    for f in files:
        provider = f.get("provider", "unknown")
        credential = {
            "auth_index": f.get("auth_index", ""),
            "email": f.get("email", f.get("account", "")) or f.get("name", f.get("id", "")).replace(".json", ""),
            "provider": provider,
            "disabled": f.get("disabled", False),
            "failed": f.get("failed", 0),
            "success": f.get("success", 0),
            "recent_requests": f.get("recent_requests", []),
            "kiro_quota": f.get("kiro_quota"),
        }
        # Codex: extract plan info and calculate remaining subscription days
        id_token = f.get("id_token") or {}
        if id_token:
            credential["plan_type"] = id_token.get("plan_type", "")
            sub_until = id_token.get("chatgpt_subscription_active_until", "")
            credential["subscription_until"] = sub_until
            if sub_until:
                from django.utils.dateparse import parse_datetime as pd
                end = pd(sub_until)
                if end:
                    days_left = (end - now).days
                    credential["days_left"] = max(days_left, 0)

        # Calculate 5h and weekly usage from recent_requests
        recent = f.get("recent_requests") or []
        # Each slot is 10 min, 5h = 30 slots, weekly ~= all available slots
        five_h_slots = recent[-30:] if len(recent) >= 30 else recent
        weekly_slots = recent

        five_h_success = sum(s.get("success", 0) for s in five_h_slots)
        five_h_failed = sum(s.get("failed", 0) for s in five_h_slots)
        weekly_success = sum(s.get("success", 0) for s in weekly_slots)
        weekly_failed = sum(s.get("failed", 0) for s in weekly_slots)

        credential["five_h_total"] = five_h_success + five_h_failed
        credential["weekly_total"] = weekly_success + weekly_failed

        # Build sparkline data (last 20 slots for visual display)
        sparkline_slots = recent[-20:] if len(recent) >= 20 else recent
        sparkline = []
        for slot in sparkline_slots:
            s = slot.get("success", 0)
            f_count = slot.get("failed", 0)
            if s == 0 and f_count == 0:
                sparkline.append("empty")
            elif f_count > 0 and s == 0:
                sparkline.append("fail")
            elif f_count > 0:
                sparkline.append("warn")
            else:
                sparkline.append("ok")
        credential["sparkline"] = sparkline
        credential["updated_at"] = f.get("updated_at", "")

        # Email masking for non-admin users
        email = credential["email"]
        if email and not request.user.is_staff:
            parts = email.split("@")
            if len(parts) == 2:
                # Real email - mask the local part
                name = parts[0]
                masked_name = name[:3] + "***" if len(name) > 3 else name[0] + "***"
                credential["email_masked"] = f"{masked_name}@{parts[1]}"
            else:
                # Not an email (e.g. file name like kiro-google-pro-1) - show as-is
                credential["email_masked"] = email
        else:
            credential["email_masked"] = email

        # Determine health status
        if f.get("disabled"):
            credential["health"] = "disabled"
        elif f.get("failed", 0) > 0 and f.get("success", 0) == 0:
            credential["health"] = "error"
        elif f.get("failed", 0) > 0:
            credential["health"] = "warning"
        else:
            credential["health"] = "healthy"

        grouped[provider].append(credential)

    # Sort providers
    provider_order = ["codex", "kiro", "gemini-cli", "antigravity"]
    sorted_groups = []
    for p in provider_order:
        if p in grouped:
            # Sort within group: enabled first, then disabled
            creds = grouped.pop(p)
            creds.sort(key=lambda c: (c["disabled"], -(c.get("five_h_total", 0) + c.get("weekly_total", 0))))
            sorted_groups.append((p, creds))
    for p, creds in sorted(grouped.items()):
        creds.sort(key=lambda c: (c["disabled"], -(c.get("five_h_total", 0) + c.get("weekly_total", 0))))
        sorted_groups.append((p, creds))

    catalog = collect_available_models(request.user, fetch=False)

    return render(request, "dashboard/credentials.html", {
        "credential_groups": sorted_groups,
        "total_count": len(files),
        **catalog,
    })


@login_required
def admin_credentials_models_refresh(request):
    """Re-read the proxy model list and return the refreshed section markup."""
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "POST required"}, status=405)

    catalog = collect_available_models(request.user, fetch=True)
    html = render_to_string("dashboard/partials/model_catalog.html", catalog, request=request)
    return JsonResponse({
        "ok": not catalog["models_error"],
        "html": html,
        "error": catalog["models_error"],
        "count": catalog["models_count"],
        "provider_count": catalog["models_provider_count"],
        "source_label": catalog["models_source_label"],
    })


@login_required
def admin_credentials_refresh(request, auth_index):
    """AJAX endpoint to refresh quota for a credential."""
    from django.http import JsonResponse
    if request.method != "POST":
        return JsonResponse({"ok": False, "error": "POST required"}, status=405)

    import json as json_mod
    import time as time_mod
    provider = request.POST.get("provider", request.GET.get("provider", ""))

    # Cooldown check for codex: 15 minutes between refreshes per auth_index
    COOLDOWN_SECONDS = 15 * 60
    cache_only = request.POST.get("cache_only", "") == "1"
    if provider == "codex":
        cache_entry = _quota_cache.get(auth_index)
        if cache_entry:
            elapsed = time_mod.time() - cache_entry["timestamp"]
            minutes_ago = int(elapsed // 60)
            # Return cache if: within cooldown OR cache_only mode (page load)
            if elapsed < COOLDOWN_SECONDS or cache_only:
                return JsonResponse({
                    "ok": True,
                    "quota": cache_entry["data"],
                    "provider": provider,
                    "cached": True,
                    "minutes_ago": minutes_ago,
                    "message": f"该配额刷新时间为 {minutes_ago} 分钟前" if not cache_only else "",
                })
        elif cache_only:
            # No cache and cache_only mode - return empty
            return JsonResponse({"ok": True, "quota": None, "provider": provider, "cached": True, "minutes_ago": None})

    try:
        if provider == "kiro":
            # Use dedicated kiro-quota endpoint
            import requests as http_requests
            from urllib.parse import urljoin
            headers = {"Authorization": f"Bearer {settings.CLIPROXY_MANAGEMENT_KEY}"}
            resp = http_requests.get(
                urljoin(settings.CLIPROXY_MANAGEMENT_BASE_URL, f"/v0/management/kiro-quota?auth_index={auth_index}"),
                headers=headers,
                timeout=30,
            )
            resp.raise_for_status()
            return JsonResponse({"ok": True, "quota": resp.json(), "provider": "kiro"})

        elif provider in QUOTA_CONFIGS:
            config = QUOTA_CONFIGS[provider]
            data = _cpap_api_call(auth_index, config["method"], config["url"], config.get("headers"))

            if provider == "codex":
                parsed = _parse_codex_quota(data)
            elif provider == "gemini-cli":
                parsed = _parse_gemini_quota(data)
            else:
                parsed = {"provider": provider, "raw": data}

            if parsed:
                # Cache the result for codex
                if provider == "codex":
                    _quota_cache[auth_index] = {"data": parsed, "timestamp": time_mod.time()}
                return JsonResponse({"ok": True, "quota": parsed, "provider": provider, "minutes_ago": 0})
            else:
                return JsonResponse({"ok": False, "error": "无法解析配额数据", "raw_status": data.get("status_code")})
        else:
            return JsonResponse({"ok": False, "error": f"不支持的 provider: {provider}"})

    except Exception as e:
        return JsonResponse({"ok": False, "error": str(e)}, status=500)


@login_required
def admin_credentials_quota_cache(request):
    """Read-only endpoint returning cached quota data. Never triggers real API calls."""
    from django.http import JsonResponse

    quotas = {}
    for auth_index, cache_entry in _quota_cache.items():
        elapsed = _time.time() - cache_entry["timestamp"]
        quotas[auth_index] = {
            "data": cache_entry["data"],
            "minutes_ago": int(elapsed // 60),
        }

    return JsonResponse({"ok": True, "quotas": quotas})


@login_required
def admin_credentials_health(request):
    """AJAX endpoint returning live health status for all credentials."""
    from django.http import JsonResponse

    try:
        files = _fetch_auth_files()
    except Exception as e:
        return JsonResponse({"ok": False, "error": str(e)}, status=500)

    credentials = []
    for f in files:
        recent = f.get("recent_requests") or []
        sparkline_slots = recent[-20:] if len(recent) >= 20 else recent
        sparkline = []
        for slot in sparkline_slots:
            s = slot.get("success", 0)
            f_count = slot.get("failed", 0)
            if s == 0 and f_count == 0:
                sparkline.append("empty")
            elif f_count > 0 and s == 0:
                sparkline.append("fail")
            elif f_count > 0:
                sparkline.append("warn")
            else:
                sparkline.append("ok")

        credentials.append({
            "auth_index": f.get("auth_index", ""),
            "success": f.get("success", 0),
            "failed": f.get("failed", 0),
            "sparkline": sparkline,
        })

    return JsonResponse({"ok": True, "credentials": credentials})


QUERY_TRACE_PAGE_SIZE = 25
QUERY_TRACE_SOURCE_NAME = "cliproxy-request-log"


def _query_trace_key_index():
    # Historical identifiers survive deleted/revoked key rows. Clear model ordering
    # before DISTINCT so timestamps do not create duplicate options.
    identifiers = set()
    for model in (QueryTrace, UsageLog):
        identifiers.update(
            model.objects.exclude(api_key_identifier="")
            .order_by().values_list("api_key_identifier", flat=True).distinct()
        )
    return {key_index(identifier): identifier for identifier in sorted(identifiers)}


def _query_trace_filters(request, key_identifiers):
    range_name = request.GET.get("range", "last_7_days")
    start, end = _get_range_start(range_name, request)
    queryset = QueryTrace.objects.select_related("user")
    if start:
        queryset = queryset.filter(request_time__gte=start)
    if end:
        queryset = queryset.filter(request_time__lte=end)

    user_id = (request.GET.get("user") or "").strip()
    if user_id.isdigit():
        queryset = queryset.filter(user_id=int(user_id))
    model_name = (request.GET.get("model") or "").strip()
    if model_name:
        queryset = queryset.filter(model_name=model_name)
    selected_key = (request.GET.get("key") or "").strip()
    if selected_key:
        identifier = key_identifiers.get(selected_key)
        queryset = queryset.filter(api_key_identifier=identifier) if identifier is not None else queryset.none()
    keyword = (request.GET.get("q") or "").strip()
    if keyword:
        queryset = queryset.filter(Q(query_text__icontains=keyword) | Q(user_identifier__icontains=keyword))

    return queryset, {
        "range_name": range_name,
        "selected_user": user_id,
        "selected_model": model_name,
        "selected_key": selected_key,
        "key_filter_unknown": bool(selected_key and selected_key not in key_identifiers),
        "keyword": keyword,
        "start": request.GET.get("start", ""),
        "end": request.GET.get("end", ""),
    }


@admin_required
def query_trace_list(request):
    """Admin view: the concrete query behind each recent request (leak tracing)."""
    key_identifiers = _query_trace_key_index()
    # Suffix collisions get a separate ordinal, while the key itself stays <=5 characters.
    labels = Counter(key_suffix(identifier) for identifier in key_identifiers.values())
    key_options = [
        {"value": value, "label": key_suffix(identifier), "ordinal": position if labels[key_suffix(identifier)] > 1 else None}
        for position, (value, identifier) in enumerate(key_identifiers.items(), start=1)
    ]
    queryset, filters = _query_trace_filters(request, key_identifiers)
    paginator = Paginator(queryset, QUERY_TRACE_PAGE_SIZE)
    page = paginator.get_page(request.GET.get("page"))

    user_options = (
        QueryTrace.objects.exclude(user=None)
        .values_list("user_id", "user__username")
        .distinct()
        .order_by("user__username")
    )
    model_options = (
        QueryTrace.objects.exclude(model_name="")
        .values_list("model_name", flat=True)
        .distinct()
        .order_by("model_name")
    )

    filter_params = []
    if filters["selected_user"]:
        filter_params.append(("user", filters["selected_user"]))
    if filters["selected_model"]:
        filter_params.append(("model", filters["selected_model"]))
    if filters["selected_key"]:
        filter_params.append(("key", filters["selected_key"]))
    if filters["keyword"]:
        filter_params.append(("q", filters["keyword"]))
    if filters["range_name"] == "custom":
        filter_params.extend([("start", filters["start"]), ("end", filters["end"])])
    list_params = [("range", filters["range_name"])] + filter_params

    context = {
        "page_title": "请求 Query 追溯",
        "traces": page.object_list,
        "page_obj": page,
        "paginator": paginator,
        "total_count": paginator.count,
        "user_options": list(user_options),
        "model_options": list(model_options),
        "key_options": key_options,
        "last_job": UsageImportJob.objects.filter(source_name=QUERY_TRACE_SOURCE_NAME).first(),
        "trace_enabled": settings.QUERY_TRACE_ENABLED,
        "store_mode": settings.QUERY_TRACE_STORE_MODE,
        "retention_rows": settings.QUERY_TRACE_MAX_ROWS,
        "retention_days": settings.QUERY_TRACE_MAX_AGE_DAYS,
        "max_chars": settings.QUERY_TRACE_MAX_CHARS,
        # range buttons keep the current filters; pagination links keep range + filters
        "filter_suffix": f"&{urlencode(filter_params)}" if filter_params else "",
        "list_query": f"&{urlencode(list_params)}",
        **filters,
    }
    return render(request, "dashboard/query_trace_list.html", context)


@admin_required
def query_trace_detail(request, short_id):
    """Admin view: one captured query with its usage row and log metadata."""
    trace = get_object_or_404(QueryTrace.objects.select_related("user", "api_key"), short_id=short_id)
    usage_log = None
    if trace.request_id:
        usage_log = UsageLog.objects.filter(request_id=trace.request_id).first()
    return render(request, "dashboard/query_trace_detail.html", {
        "page_title": f"Query 详情 · {trace.short_id}",
        "trace": trace,
        "usage_log": usage_log,
    })

