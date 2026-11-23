import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("api_keys", "0001_initial"),
        ("quota", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="DailyQuotaUsage",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("day", models.DateField()),
                ("tokens_used", models.PositiveBigIntegerField(default=0)),
                ("limit", models.PositiveBigIntegerField(default=0)),
                ("blocked_key", models.CharField(blank=True, max_length=255)),
                ("blocked_at", models.DateTimeField(blank=True, null=True)),
                ("restored_at", models.DateTimeField(blank=True, null=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "api_key",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="daily_quota_usage",
                        to="api_keys.apikey",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="daily_quota_usage",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ["-day", "user__username"],
            },
        ),
        migrations.AlterUniqueTogether(
            name="dailyquotausage",
            unique_together={("user", "day")},
        ),
    ]
