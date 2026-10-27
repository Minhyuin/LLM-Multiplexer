# Generated from the Django 4.2.16 model state; seeds the queue mutex.
import uuid

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


def seed_coordinator(apps, schema_editor):
    apps.get_model("fingerprints", "FingerprintCoordinator").objects.using(schema_editor.connection.alias).get_or_create(pk=1)


class Migration(migrations.Migration):
    initial = True
    dependencies = [
        ("api_keys", "0005_apikey_token_rotated_at"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]
    operations = [
        migrations.CreateModel(
            name="FingerprintCoordinator",
            fields=[("id", models.PositiveSmallIntegerField(default=1, editable=False, primary_key=True, serialize=False))],
        ),
        migrations.CreateModel(
            name="FingerprintRun",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("key_fingerprint", models.CharField(max_length=64)),
                ("key_label", models.CharField(max_length=160)),
                ("model_id", models.CharField(max_length=255)),
                ("protocol", models.CharField(max_length=32)),
                ("parameters", models.JSONField(default=dict)),
                ("upstream_commit", models.CharField(max_length=40)),
                ("bank_built_at", models.CharField(blank=True, max_length=100)),
                ("idempotency_key", models.UUIDField()),
                ("status", models.CharField(choices=[("queued", "排队中"), ("running", "检测中"), ("completed", "已完成"), ("partial", "部分成功"), ("failed", "无法完成"), ("cancelled", "已取消"), ("interrupted", "执行中断")], default="queued", max_length=16)),
                ("quota_day", models.DateField()),
                ("quota_consumed", models.BooleanField(default=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("queue_deadline", models.DateTimeField()),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("first_dispatched_at", models.DateTimeField(blank=True, null=True)),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
                ("heartbeat_at", models.DateTimeField(blank=True, null=True)),
                ("lease_token", models.UUIDField(blank=True, null=True)),
                ("cancel_requested", models.BooleanField(default=False)),
                ("result", models.JSONField(default=dict)),
                ("usage", models.JSONField(default=dict)),
                ("error_code", models.CharField(blank=True, max_length=64)),
                ("error_message", models.CharField(blank=True, max_length=300)),
                ("api_key", models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="fingerprint_runs", to="api_keys.apikey")),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="fingerprint_runs", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.CreateModel(
            name="FingerprintSample",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("ordinal", models.PositiveSmallIntegerField()),
                ("challenge_id", models.CharField(max_length=100)),
                ("expected_count", models.PositiveIntegerField()),
                ("status", models.CharField(default="pending", max_length=16)),
                ("dispatched_at", models.DateTimeField(blank=True, null=True)),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
                ("valid", models.BooleanField(default=False)),
                ("number_count", models.PositiveIntegerField(default=0)),
                ("finish_reason", models.CharField(blank=True, max_length=80)),
                ("duration_ms", models.PositiveIntegerField(blank=True, null=True)),
                ("response_model", models.CharField(blank=True, max_length=255)),
                ("response_id", models.CharField(blank=True, max_length=255)),
                ("usage", models.JSONField(default=dict)),
                ("error_code", models.CharField(blank=True, max_length=64)),
                ("error_message", models.CharField(blank=True, max_length=300)),
                ("run", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="samples", to="fingerprints.fingerprintrun")),
            ],
            options={"ordering": ["ordinal"]},
        ),
        migrations.CreateModel(
            name="FingerprintQuotaDay",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("day", models.DateField()),
                ("used", models.PositiveIntegerField(default=0)),
                ("reserved", models.PositiveIntegerField(default=0)),
                ("last_submitted_at", models.DateTimeField(blank=True, null=True)),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to=settings.AUTH_USER_MODEL)),
            ],
        ),
        migrations.AddConstraint(model_name="fingerprintsample", constraint=models.UniqueConstraint(fields=("run", "ordinal"), name="fp_sample_ordinal")),
        migrations.AddIndex(model_name="fingerprintrun", index=models.Index(fields=["status", "created_at"], name="fp_queue_idx")),
        migrations.AddIndex(model_name="fingerprintrun", index=models.Index(fields=["user", "finished_at"], name="fp_history_idx")),
        migrations.AddConstraint(model_name="fingerprintrun", constraint=models.UniqueConstraint(fields=("user", "idempotency_key"), name="fp_user_idempotency")),
        migrations.AddConstraint(model_name="fingerprintrun", constraint=models.UniqueConstraint(condition=models.Q(status__in=["queued", "running"]), fields=("user",), name="fp_one_active_user")),
        migrations.AddConstraint(model_name="fingerprintquotaday", constraint=models.UniqueConstraint(fields=("user", "day"), name="fp_quota_user_day")),
        migrations.RunPython(seed_coordinator, migrations.RunPython.noop),
    ]
