from django.conf import settings
from django.db import models


class UserQuotaSnapshot(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="quota_snapshots")
    year = models.PositiveIntegerField()
    month = models.PositiveIntegerField()
    token_limit = models.PositiveBigIntegerField(default=0)
    token_used = models.PositiveBigIntegerField(default=0)
    cost_limit = models.DecimalField(max_digits=12, decimal_places=4, default=0)
    cost_used = models.DecimalField(max_digits=12, decimal_places=4, default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-year", "-month", "user__username"]
        unique_together = [("user", "year", "month")]

    def __str__(self):
        return f"{self.user.username} {self.year}-{self.month:02d}"


class DailyQuotaUsage(models.Model):
    """One row per member per local day: the cap in force and the enforcement state.

    ``tokens_used`` is a cached value refreshed by the sync tick; the authoritative
    number is always ``SUM(UsageLog.total_tokens)`` for the day.  ``blocked_key``
    holds the exact key text that was removed from the proxy so a restoration adds
    back only that one key and never rewrites the whole list.
    """

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="daily_quota_usage")
    day = models.DateField()
    tokens_used = models.PositiveBigIntegerField(default=0)
    limit = models.PositiveBigIntegerField(default=0)
    api_key = models.ForeignKey(
        "api_keys.APIKey", on_delete=models.SET_NULL, null=True, blank=True, related_name="daily_quota_usage"
    )
    blocked_key = models.CharField(max_length=255, blank=True)
    blocked_at = models.DateTimeField(null=True, blank=True)
    restored_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-day", "user__username"]
        unique_together = [("user", "day")]

    def __str__(self):
        return f"{self.user.username} {self.day} {self.tokens_used}/{self.limit}"

    @property
    def is_blocked(self) -> bool:
        return self.blocked_at is not None and self.restored_at is None
