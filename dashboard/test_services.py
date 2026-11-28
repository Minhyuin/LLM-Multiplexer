"""The rollup must not drag raw_payload through the connection."""

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from dashboard.models import DailyUsageStat
from dashboard.services import rebuild_daily_usage_stats
from usage.models import UsageLog


class RebuildDailyUsageStatsTests(TestCase):
    def _log(self, request_id, model, tokens, cost, payload):
        return UsageLog.objects.create(
            request_id=request_id,
            model_name=model,
            prompt_tokens=tokens,
            completion_tokens=0,
            total_tokens=tokens,
            estimated_cost=cost,
            request_time=timezone.now(),
            raw_payload=payload,
        )

    def test_rollup_totals_and_grouping(self):
        self._log("a-1", "alpha", 1000, "1.5", {"blob": "x" * 5000})
        self._log("a-2", "alpha", 2000, "2.5", {"blob": "y" * 5000})
        self._log("b-1", "beta", 500, "0.5", {"blob": "z" * 5000})

        count = rebuild_daily_usage_stats(days=1)

        self.assertEqual(count, 2)
        alpha = DailyUsageStat.objects.get(model_name="alpha")
        self.assertEqual(alpha.total_requests, 2)
        self.assertEqual(alpha.total_tokens, 3000)
        self.assertEqual(float(alpha.total_cost), 4.0)
        beta = DailyUsageStat.objects.get(model_name="beta")
        self.assertEqual(beta.total_requests, 1)
        self.assertEqual(float(beta.total_cost), 0.5)

    def test_the_rollup_never_selects_raw_payload(self):
        self._log("a-1", "alpha", 1000, "1.5", {"blob": "x" * 5000})

        with CaptureQueriesContext(connection) as captured:
            rebuild_daily_usage_stats(days=1)

        selects = [q["sql"] for q in captured.captured_queries if q["sql"].lstrip().upper().startswith("SELECT")]
        self.assertTrue(selects, "the rollup issued no SELECT")
        for sql in selects:
            self.assertNotIn("raw_payload", sql)

    def test_rebuild_is_idempotent(self):
        self._log("a-1", "alpha", 1000, "1.5", {})
        rebuild_daily_usage_stats(days=1)
        rebuild_daily_usage_stats(days=1)
        self.assertEqual(DailyUsageStat.objects.filter(model_name="alpha").count(), 1)
        self.assertEqual(DailyUsageStat.objects.get(model_name="alpha").total_requests, 1)
