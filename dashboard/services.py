from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from dashboard.models import DailyUsageStat
from usage.models import UsageLog


def rebuild_daily_usage_stats(days=90):
    """Rebuild the per-day, per-model rollup from the request ledger.

    The rows are pulled as values of the seven columns the rollup needs rather
    than as model instances.  Selecting whole rows drags the JSONB raw_payload of
    every request through the connection, which is tens of gigabytes of traffic on
    a small host and was enough to saturate the disk and take the server down.
    """
    end_time = timezone.now()
    start_time = end_time - timedelta(days=days)
    DailyUsageStat.objects.filter(stat_date__gte=start_time.date()).delete()

    rows = (
        UsageLog.objects.filter(request_time__gte=start_time)
        .order_by("request_time")
        .values(
            "request_time",
            "model_name",
            "total_tokens",
            "estimated_cost",
            "is_error",
            "user_id",
            "api_key_id",
        )
    )

    bucket = {}
    for item in rows.iterator(chunk_size=5000):
        stat_date = timezone.localtime(item["request_time"]).date()
        key = (stat_date, item["model_name"])
        row = bucket.get(key)
        if row is None:
            row = {
                "total_requests": 0,
                "total_tokens": 0,
                "total_cost": 0.0,
                "error_count": 0,
                "users": set(),
                "keys": set(),
            }
            bucket[key] = row
        row["total_requests"] += 1
        row["total_tokens"] += item["total_tokens"]
        row["total_cost"] += float(item["estimated_cost"] or 0)
        row["error_count"] += 1 if item["is_error"] else 0
        if item["user_id"]:
            row["users"].add(item["user_id"])
        if item["api_key_id"]:
            row["keys"].add(item["api_key_id"])

    objects = [
        DailyUsageStat(
            stat_date=stat_date,
            model_name=model_name,
            total_requests=row["total_requests"],
            total_tokens=row["total_tokens"],
            total_cost=row["total_cost"],
            error_count=row["error_count"],
            active_users=len(row["users"]),
            active_keys=len(row["keys"]),
        )
        for (stat_date, model_name), row in bucket.items()
    ]

    with transaction.atomic():
        DailyUsageStat.objects.bulk_create(objects, batch_size=500)
    return len(objects)
