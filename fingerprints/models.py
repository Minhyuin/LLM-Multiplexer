"""Durable task state; no API secrets or model reply bodies are stored here."""
import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q


class FingerprintCoordinator(models.Model):
    """Singleton transaction mutex for queue admission, leases and quota changes."""
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)


class FingerprintQuotaDay(models.Model):
    # Independent of run retention: deleting history must never refund usage.
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    day = models.DateField()
    used = models.PositiveIntegerField(default=0)
    reserved = models.PositiveIntegerField(default=0)
    last_submitted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "day"], name="fp_quota_user_day")]


class FingerprintRun(models.Model):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"
    ACTIVE = (QUEUED, RUNNING)
    STATUS_CHOICES = [
        (QUEUED, "排队中"), (RUNNING, "检测中"), (COMPLETED, "已完成"),
        (PARTIAL, "部分成功"), (FAILED, "无法完成"), (CANCELLED, "已取消"),
        (INTERRUPTED, "执行中断"),
    ]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="fingerprint_runs")
    api_key = models.ForeignKey("api_keys.APIKey", on_delete=models.SET_NULL, null=True, related_name="fingerprint_runs")
    key_fingerprint = models.CharField(max_length=64)
    key_label = models.CharField(max_length=160)
    model_id = models.CharField(max_length=255)
    protocol = models.CharField(max_length=32)
    parameters = models.JSONField(default=dict)
    upstream_commit = models.CharField(max_length=40)
    bank_built_at = models.CharField(max_length=100, blank=True)
    idempotency_key = models.UUIDField()
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=QUEUED)
    quota_day = models.DateField()
    quota_consumed = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    queue_deadline = models.DateTimeField()
    started_at = models.DateTimeField(null=True, blank=True)
    first_dispatched_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    heartbeat_at = models.DateTimeField(null=True, blank=True)
    lease_token = models.UUIDField(null=True, blank=True)
    cancel_requested = models.BooleanField(default=False)
    result = models.JSONField(default=dict)
    usage = models.JSONField(default=dict)
    error_code = models.CharField(max_length=64, blank=True)
    error_message = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["user", "idempotency_key"], name="fp_user_idempotency"),
            models.UniqueConstraint(fields=["user"], condition=Q(status__in=["queued", "running"]), name="fp_one_active_user"),
        ]
        indexes = [
            models.Index(fields=["status", "created_at"], name="fp_queue_idx"),
            models.Index(fields=["user", "finished_at"], name="fp_history_idx"),
        ]


class FingerprintSample(models.Model):
    run = models.ForeignKey(FingerprintRun, on_delete=models.CASCADE, related_name="samples")
    ordinal = models.PositiveSmallIntegerField()
    challenge_id = models.CharField(max_length=100)
    expected_count = models.PositiveIntegerField()
    status = models.CharField(max_length=16, default="pending")
    dispatched_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    valid = models.BooleanField(default=False)
    number_count = models.PositiveIntegerField(default=0)
    finish_reason = models.CharField(max_length=80, blank=True)
    duration_ms = models.PositiveIntegerField(null=True, blank=True)
    response_model = models.CharField(max_length=255, blank=True)
    response_id = models.CharField(max_length=255, blank=True)
    usage = models.JSONField(default=dict)
    error_code = models.CharField(max_length=64, blank=True)
    error_message = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ["ordinal"]
        constraints = [models.UniqueConstraint(fields=["run", "ordinal"], name="fp_sample_ordinal")]
