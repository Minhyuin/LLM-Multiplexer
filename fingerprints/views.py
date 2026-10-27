"""Owner-only JSON endpoints. GET never admits, expires or requeues a task."""
import json
import logging
from datetime import timedelta
from functools import wraps

from django.http import JsonResponse
from django.urls import reverse
from django.utils import timezone
from django.utils.cache import add_never_cache_headers

from users.middleware import get_real_user
from .config import FingerprintError, enabled, limit, model_policy
from .models import FingerprintRun
from .services import cancel_run, create_run, quota_for, usable_keys

logger = logging.getLogger(__name__)


def context(request):
    return {"fingerprint_enabled": enabled(), "fingerprint_read_only": bool(getattr(request, "view_as", None)),
            "fingerprint_state_url": reverse("fingerprint-state"), "fingerprint_start_url": reverse("fingerprint-start")}


def annotate_catalog(catalog, request):
    catalog.update(context(request))
    if enabled():
        for group in catalog.get("model_groups", []):
            for model in group["models"]:
                try:
                    model_policy(model["id"])
                    model["fingerprint_supported"] = True
                    model["fingerprint_reason"] = ""
                except FingerprintError as exc:
                    model["fingerprint_supported"] = False
                    model["fingerprint_reason"] = exc.message
    return catalog


def json_view(method):
    def decorate(func):
        @wraps(func)
        def wrapped(request, *args, **kwargs):
            try:
                if not get_real_user(request).is_authenticated:
                    raise FingerprintError("login_required", "登录已过期，请重新登录后查看检测。", 401)
                if request.method != method:
                    raise FingerprintError("method_not_allowed", f"请使用 {method} 请求。", 405)
                if not get_real_user(request).is_active:
                    raise FingerprintError("account_disabled", "账号已停用。", 403)
                if method != "GET" and getattr(request, "view_as", None):
                    raise FingerprintError("read_only", "只读预览不能发起或取消收费检测。", 403)
                payload = func(request, *args, **kwargs)
                response = JsonResponse(payload, json_dumps_params={"ensure_ascii": False, "allow_nan": False})
            except FingerprintError as exc:
                response = JsonResponse({"error": {"code": exc.code, "message": exc.message}}, status=exc.status)
            except Exception as exc:
                # Exception messages/traceback locals can contain an upstream key
                # or response. Log only the class and fixed endpoint name.
                logger.error("Fingerprint endpoint %s failed (%s)", func.__name__, type(exc).__name__)
                response = JsonResponse({"error": {"code": "server_error", "message": "检测服务暂时不可用，请稍后刷新查看任务状态。"}}, status=500)
            add_never_cache_headers(response)
            response["Cache-Control"] = "private, no-store, no-cache, max-age=0, must-revalidate"
            return response
        wrapped.portal_json_auth = True
        return wrapped
    return decorate


def serialize(run):
    samples = list(run.samples.all())
    iso = lambda value: value.isoformat() if value else None
    return {"id": str(run.pk), "model_id": run.model_id, "status": run.status, "status_label": run.get_status_display(),
            "created_at": iso(run.created_at), "started_at": iso(run.started_at), "finished_at": iso(run.finished_at),
            "cancel_requested": run.cancel_requested, "completed_samples": sum(s.status in ("complete", "truncated", "failed") for s in samples),
            "valid_samples": sum(s.valid for s in samples), "total_samples": 3,
            "error_code": run.error_code, "error_message": run.error_message, "key_label": run.key_label,
            "protocol": run.protocol, "parameters": run.parameters, "upstream_commit": run.upstream_commit,
            "bank_built_at": run.bank_built_at, "result": run.result, "usage": run.usage,
            "samples": [{"ordinal": s.ordinal, "status": s.status, "valid": s.valid, "number_count": s.number_count,
                         "finish_reason": s.finish_reason, "duration_ms": s.duration_ms, "response_model": s.response_model,
                         "error_message": s.error_message} for s in samples],
            "status_url": reverse("fingerprint-detail", kwargs={"run_id": run.pk}),
            "cancel_url": reverse("fingerprint-cancel", kwargs={"run_id": run.pk})}


def visible_runs(user):
    from django.db.models import Q
    return FingerprintRun.objects.filter(user=user).filter(
        Q(status__in=FingerprintRun.ACTIVE) | Q(finished_at__gte=timezone.now() - timedelta(hours=limit("HISTORY_HOURS", 24))))


@json_view("GET")
def state(request):
    preview = bool(getattr(request, "view_as", None))
    if not enabled():
        return {"enabled": False, "read_only": preview, "keys": [], "quota": None, "active": None, "history": []}
    # A preview must not expose the target member's private fingerprint history.
    user = get_real_user(request)
    runs = visible_runs(user).prefetch_related("samples")
    active = runs.filter(status__in=FingerprintRun.ACTIVE).first()
    history = runs.exclude(status__in=FingerprintRun.ACTIVE)[:limit("HISTORY_LIMIT", 200)]
    return {"enabled": True, "read_only": preview,
            "keys": [{"id": k.pk, "name": k.name, "masked": k.token_masked} for k in usable_keys(user)] if not preview else [],
            "quota": quota_for(user), "active": serialize(active) if active else None, "history": [serialize(r) for r in history]}


@json_view("POST")
def start(request):
    if len(request.body) > 4096:
        raise FingerprintError("invalid_request", "检测参数超过大小上限。")
    try:
        data = json.loads(request.body)
        if not isinstance(data, dict) or set(data) - {"key_id", "model_id", "idempotency_key"}:
            raise ValueError()
    except (ValueError, UnicodeError):
        raise FingerprintError("invalid_request", "检测参数无效。") from None
    run, reused = create_run(get_real_user(request), data.get("key_id"), data.get("model_id"), data.get("idempotency_key"))
    return {"run": serialize(run), "quota": quota_for(get_real_user(request)), "reused": reused}


@json_view("GET")
def detail(request, run_id):
    run = visible_runs(get_real_user(request)).filter(pk=run_id).prefetch_related("samples").first()
    if run is None:
        raise FingerprintError("not_found", "未找到检测任务，或历史记录已过期。", 404)
    return {"run": serialize(run), "quota": quota_for(get_real_user(request))}


@json_view("POST")
def cancel(request, run_id):
    run = cancel_run(get_real_user(request), run_id)
    return {"run": serialize(run), "quota": quota_for(get_real_user(request))}
