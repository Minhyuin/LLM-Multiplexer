"""Atomic queue / quota lifecycle. No implicit retry of a dispatched challenge."""
import uuid
from contextlib import contextmanager
from datetime import timedelta
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from api_keys.models import APIKey
from core.utils import is_portal_admin
from .config import FingerprintError, UPSTREAM_COMMIT, enabled, limit, model_policy
from .models import FingerprintCoordinator, FingerprintQuotaDay, FingerprintRun, FingerprintSample

SHANGHAI = ZoneInfo("Asia/Shanghai")


@contextmanager
def queue_lock():
    # Every mutation uses this one lock, including admission and lease recovery.
    # SQLite is suitable only for functional tests; PostgreSQL supplies row locks.
    with transaction.atomic():
        FingerprintCoordinator.objects.select_for_update().get(pk=1)
        yield


def day_at(now):
    return now.astimezone(SHANGHAI).date()


def quota_for(user, now=None):
    now = now or timezone.now()
    row = FingerprintQuotaDay.objects.filter(user=user, day=day_at(now)).first()
    used, reserved = (row.used, row.reserved) if row else (0, 0)
    maximum = limit("ADMIN_DAILY_LIMIT", 20) if is_portal_admin(user) else limit("DAILY_LIMIT", 3)
    last = FingerprintQuotaDay.objects.filter(user=user).aggregate(last=Max("last_submitted_at"))["last"]
    return {"day": day_at(now).isoformat(), "limit": maximum, "used": used, "reserved": reserved,
            "remaining": max(0, maximum - used - reserved),
            "next_allowed_at": (last + timedelta(seconds=limit("COOLDOWN_SECONDS", 60))).isoformat() if last else None}


def usable_keys(user):
    now = timezone.now()
    return [key for key in APIKey.objects.filter(user=user, status=APIKey.STATUS_ACTIVE).exclude(token_plaintext="")
            if (not key.expires_at or key.expires_at > now) and APIKey.hash_token(key.token_plaintext) == key.token_hash]


def validate_key(user_id, key_id, fingerprint=None):
    user = get_user_model().objects.filter(pk=user_id, is_active=True).first()
    if user is None:
        raise FingerprintError("account_disabled", "账号已停用，检测已停止。", 403)
    key = APIKey.objects.filter(pk=key_id, user_id=user_id).first()
    if key is None or not key.is_usable or not key.token_plaintext:
        raise FingerprintError("key_unavailable", "本人所选 Key 已不可用，请检查状态后重新发起。", 403)
    if APIKey.hash_token(key.token_plaintext) != key.token_hash or (fingerprint and key.token_hash != fingerprint):
        raise FingerprintError("key_changed", "所选 Key 已轮换或变更，请重新发起检测。", 409)
    return key


def existing_run(user, idempotency_key):
    existing = FingerprintRun.objects.filter(user=user, idempotency_key=idempotency_key).first()
    return existing or FingerprintRun.objects.filter(user=user, status__in=FingerprintRun.ACTIVE).first()


def create_run(user, key_id, model_id, idempotency_key):
    if not enabled():
        raise FingerprintError("disabled", "模型指纹检测暂未开启。", 503)
    try:
        identity = uuid.UUID(str(idempotency_key))
        if isinstance(key_id, bool) or not isinstance(key_id, (int, str)):
            raise ValueError()
        key_id = int(key_id)
        if not 0 < key_id <= 9223372036854775807:
            raise ValueError()
    except (ValueError, TypeError, OverflowError):
        raise FingerprintError("invalid_request", "任务标识或 Key 无效。") from None
    previous = existing_run(user, identity)
    if previous:
        return previous, True
    policy = model_policy(model_id)
    key = validate_key(user.pk, key_id)
    # No configured/system-key fallback. Model availability is always scoped to
    # the selected owner key. This GET is not a paid challenge and is not cached.
    from .transport import available_models
    if model_id not in available_models(key.token_plaintext):
        raise FingerprintError("model_unavailable", "本人所选 Key 当前无法使用这个模型。", 403)
    with queue_lock():
        now = timezone.now()
        _expire_queued(now)
        previous = existing_run(user, identity)
        if previous:
            return previous, True
        validate_key(user.pk, key_id, key.token_hash)
        fresh_user = get_user_model().objects.get(pk=user.pk)
        quota = quota_for(fresh_user, now)
        if quota["remaining"] <= 0:
            raise FingerprintError("daily_limit", "今日检测次数已用完。", 429)
        last = FingerprintQuotaDay.objects.filter(user=user).aggregate(last=Max("last_submitted_at"))["last"]
        if last and now < last + timedelta(seconds=limit("COOLDOWN_SECONDS", 60)):
            raise FingerprintError("cooldown", "两次检测需间隔 60 秒，请稍后重试。", 429)
        if FingerprintRun.objects.filter(status=FingerprintRun.QUEUED).count() >= limit("QUEUE_LIMIT", 20):
            raise FingerprintError("queue_full", "检测队列已满，请稍后重试。", 429)
        counter, _ = FingerprintQuotaDay.objects.get_or_create(user=user, day=day_at(now))
        counter.reserved += 1
        counter.last_submitted_at = now
        counter.save()
        run = FingerprintRun.objects.create(user=user, api_key=key, key_fingerprint=key.token_hash,
            key_label=f"{key.name} · {key.token_masked}"[:160], model_id=model_id, protocol=policy["api"],
            parameters=policy, upstream_commit=UPSTREAM_COMMIT, idempotency_key=identity, quota_day=counter.day,
            queue_deadline=now + timedelta(seconds=limit("QUEUE_TIMEOUT", 180)))
        return run, False


def aggregate_usage(run):
    rows = list(run.samples.exclude(dispatched_at=None).values_list("usage", flat=True))
    result = {}
    for name in ("input_tokens", "output_tokens", "cached_tokens", "reasoning_tokens"):
        values = [row.get(name) for row in rows if isinstance(row.get(name), int)]
        result[name] = sum(values) if values else None
    result["complete"] = bool(rows) and all(row.get("complete") is True for row in rows)
    result["reported_samples"] = sum(bool(row) for row in rows)
    result["sent_samples"] = len(rows)
    return result


def _finish(run, status, code="", message="", result=None):
    if run.status not in FingerprintRun.ACTIVE:
        return run
    now = timezone.now()
    if not run.quota_consumed:
        counter = FingerprintQuotaDay.objects.get(user_id=run.user_id, day=run.quota_day)
        counter.reserved = max(0, counter.reserved - 1)
        counter.save(update_fields=["reserved"])
    run.samples.filter(status="dispatching").update(status="interrupted", finished_at=now,
        error_code=code or "interrupted", error_message=message or "请求结果未知，未自动重试。")
    run.samples.filter(status="pending").update(status="cancelled", finished_at=now)
    run.status, run.error_code, run.error_message = status, code, message
    run.finished_at = now
    run.usage = aggregate_usage(run)
    if result is not None:
        run.result = result
    run.save()
    return run


def finish_run(run_id, lease, status, code="", message="", result=None):
    with queue_lock():
        run = FingerprintRun.objects.get(pk=run_id)
        if run.lease_token != lease:
            return run
        if run.cancel_requested:
            status, code, message = FingerprintRun.CANCELLED, "cancelled", "检测已取消；已发出的请求仍可能计费。"
        return _finish(run, status, code, message, result)


def cancel_run(user, run_id):
    with queue_lock():
        run = FingerprintRun.objects.filter(pk=run_id, user=user).first()
        if run is None:
            raise FingerprintError("not_found", "未找到检测任务。", 404)
        if run.status == FingerprintRun.QUEUED:
            run.cancel_requested = True
            _finish(run, FingerprintRun.CANCELLED, "cancelled", "已取消排队，预约次数已归还。")
        elif run.status == FingerprintRun.RUNNING:
            run.cancel_requested = True
            run.save(update_fields=["cancel_requested"])
        return run


def _expire_queued(now):
    for run in FingerprintRun.objects.filter(status=FingerprintRun.QUEUED, queue_deadline__lte=now):
        _finish(run, FingerprintRun.CANCELLED, "queue_timeout", "排队超过 3 分钟，预约次数已归还。")


def maintenance(now=None):
    now = now or timezone.now()
    with queue_lock():
        _expire_queued(now)
        for run in FingerprintRun.objects.filter(status=FingerprintRun.RUNNING):
            # A stale task is ended, never requeued. Hard process deadlines are
            # shorter than stale leases, so another worker cannot overlap it.
            stale = not run.heartbeat_at or run.heartbeat_at < now - timedelta(seconds=limit("LEASE_SECONDS", 30))
            expired = run.started_at and run.started_at < now - timedelta(seconds=limit("RUN_TIMEOUT", 420) + 10)
            if stale or expired:
                _finish(run, FingerprintRun.INTERRUPTED, "worker_lost", "执行进程中断，未知请求未自动重试。")
        terminal = FingerprintRun.objects.exclude(status__in=FingerprintRun.ACTIVE)
        terminal.filter(finished_at__lt=now - timedelta(hours=limit("HISTORY_HOURS", 24))).delete()
        for user_id in terminal.order_by().values_list("user_id", flat=True).distinct():
            old = list(terminal.filter(user_id=user_id).order_by("-finished_at").values_list("pk", flat=True)[limit("HISTORY_LIMIT", 200):])
            if old:
                FingerprintRun.objects.filter(pk__in=old).delete()
        # Keep daily quota accounting independent of history, and retain the
        # preceding day for cooldown at midnight. Never discard reservations.
        FingerprintQuotaDay.objects.filter(day__lt=day_at(now) - timedelta(days=2), reserved=0).delete()


def claim_run():
    if not enabled():
        return None
    with queue_lock():
        now = timezone.now()
        _expire_queued(now)
        if FingerprintRun.objects.filter(status=FingerprintRun.RUNNING).count() >= limit("CONCURRENCY", 2):
            return None
        run = FingerprintRun.objects.filter(status=FingerprintRun.QUEUED).order_by("created_at").first()
        if run is None:
            return None
        run.status, run.started_at, run.heartbeat_at, run.lease_token = FingerprintRun.RUNNING, now, now, uuid.uuid4()
        run.save()
        return run


def is_cancelled(run_id, lease):
    if not enabled():
        return True
    row = FingerprintRun.objects.filter(pk=run_id).values("status", "lease_token", "cancel_requested").first()
    return not row or row["status"] != FingerprintRun.RUNNING or row["lease_token"] != lease or row["cancel_requested"]


def heartbeat(run_id, lease):
    return FingerprintRun.objects.filter(pk=run_id, lease_token=lease, status=FingerprintRun.RUNNING).update(heartbeat_at=timezone.now())


def dispatch_sample(run_id, lease, ordinal):
    with queue_lock():
        run = FingerprintRun.objects.get(pk=run_id)
        if run.lease_token != lease or run.status != FingerprintRun.RUNNING or run.cancel_requested or not enabled():
            raise FingerprintError("cancelled", "检测已停止。")
        key = validate_key(run.user_id, run.api_key_id, run.key_fingerprint)
        sample = run.samples.get(ordinal=ordinal)
        if sample.status != "pending":
            raise FingerprintError("already_dispatched", "挑战已进入发送阶段，不会重复发送。")
        now = timezone.now()
        if not run.quota_consumed:
            counter = FingerprintQuotaDay.objects.get(user_id=run.user_id, day=run.quota_day)
            counter.reserved = max(0, counter.reserved - 1)
            counter.used += 1
            counter.save()
            run.quota_consumed, run.first_dispatched_at = True, now
        run.heartbeat_at = now
        run.save()
        sample.status, sample.dispatched_at = "dispatching", now
        sample.save(update_fields=["status", "dispatched_at"])
        # Commit the dispatch marker before sending. A crash in the gap is
        # conservatively charged one round, because delivery is unknowable.
        return key.token_plaintext


def save_sample(run_id, lease, ordinal, sample_result):
    with queue_lock():
        run = FingerprintRun.objects.get(pk=run_id)
        if run.status != FingerprintRun.RUNNING or run.lease_token != lease:
            return False
        fields = {name: sample_result.get(name) for name in (
            "status", "valid", "number_count", "finish_reason", "duration_ms", "response_model", "response_id", "usage", "error_code", "error_message")}
        run.samples.filter(ordinal=ordinal, status="dispatching").update(**fields, finished_at=timezone.now())
        run.heartbeat_at, run.usage = timezone.now(), aggregate_usage(run)
        run.save(update_fields=["heartbeat_at", "usage"])
        return True
