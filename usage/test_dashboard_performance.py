"""Phase-1 dashboard aggregation tests: sections, token aggregation, pagination.

The old ``aggregate_dashboard`` always computed every panel and re-read the whole
``raw_payload`` column in Python. These tests pin down the optimized contract:

* the default call still returns the legacy payload, in the legacy key order,
  with the legacy numbers;
* ``sections`` computes only the requested panels, and the summary counts come
  from a single aggregate query;
* cached/thinking tokens are aggregated in SQL while still matching
  ``_payload_int`` path precedence for every historical payload layout, and rows
  only Python can read (Unicode digits, 19+ digit integers, huge numbers) are
  corrected row by row instead of scanning the whole column;
* ``key_page`` paginates ``key_rank`` before the correlated top-model subquery;
* ``trend_bucket="month"`` buckets by calendar month.
"""

import json
import os
import unittest
from datetime import datetime, timedelta
from unittest import mock

from django.db import connection
from django.db.utils import OperationalError
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from usage import services
from usage.models import UsageLog
from usage.services import (
    CACHED_TOKEN_PATHS,
    DASHBOARD_KEY_PAGE_SIZE_LIMIT,
    DASHBOARD_SECTIONS,
    THINKING_TOKEN_PATHS,
    _build_json_token_anomaly_sql,
    _build_safe_json_int_sql,
    _payload_int,
    _trend_label,
    aggregate_dashboard,
)
from usage.tests import DashboardTopModelMixin

# Every raw_payload layout the portal has ever imported, plus the values SQL and
# Python can disagree about. ``_payload_int`` is the oracle for all of them.
TOKEN_PAYLOADS = [
    {"tokens": {"cached_tokens": 5, "reasoning_tokens": 7}},
    {"raw_payload": {"tokens": {"cached_tokens": 11}}, "tokens": {"cached_tokens": 3}},
    {"cached_tokens": 13, "thinking_tokens": 2},
    {"usage": {"cached_tokens": 17, "reasoning_tokens": 19}},
    {"tokens": {"cached_tokens": "23", "thinking_tokens": " 29 "}},
    {"tokens": {"cached_tokens": "1_000", "reasoning_tokens": "2_500"}},
    {"tokens": {"cached_tokens": "3.7", "reasoning_tokens": 3.7}},
    {"tokens": {"cached_tokens": None, "thinking_tokens": True, "reasoning_tokens": False}},
    {"tokens": {"cached_tokens": "٥"}},
    {"cached_tokens": "1234567890123456789012345"},
    {"cached_tokens": 2.5e18},
    {"cached_tokens": "abc"},
    {"cached_tokens": ""},
    {},
    {"completion_tokens_details": {"reasoning_tokens": 41}},
    {"raw_payload": {"tokens": {"thinking_tokens": 43}}},
    {"tokens": {"reasoning_tokens": 47}, "thinking_tokens": 53},
    {"raw_payload": {"tokens": {"cached_tokens": "0_5"}}},
    {"raw_payload": {"tokens": {"cached_tokens": "5_"}}},
    {"raw_payload": {"tokens": {"cached_tokens": "_5"}}},
    {"tokens": {"cached_tokens": -9, "reasoning_tokens": -4}},
    {"tokens": ["not", "a", "dict"], "cached_tokens": 61},
    # Signed *strings*: int("-7") is negative, so the SQL has to re-attach the sign it
    # strips while checking the digits (a sign-less cast returned +7 for these).
    {"tokens": {"cached_tokens": "-7", "thinking_tokens": "-1_000"}},
    {"usage": {"cached_tokens": "+12", "reasoning_tokens": " -42 "}},
    # A JSON *number* too large for a 64-bit integer: SQLite hands it back as a real
    # (1.2345678901234568e+24) and PostgreSQL as an out-of-range numeric, so both
    # vendors must flag the row rather than truncate it.
    {"tokens": {"cached_tokens": 1234567890123456789012345}},
]
# Payloads whose token value SQL cannot reproduce exactly: they must be reported
# by the anomaly expression so the Python fix-up keeps the totals exact.
MUST_CORRECT_PAYLOAD_INDEXES = (8, 9, 10, 24)
# Payloads the SQL expression reproduces exactly (so they must not be flagged and
# must not pay for the Python fix-up). Payloads 6, 11, 18 and 19 hold values that
# are invalid for int() as well, so they may be reported either way: Python
# returns the same number.
SQL_EXACT_PAYLOAD_INDEXES = (0, 1, 2, 3, 4, 5, 7, 12, 13, 14, 15, 16, 17, 20, 21, 22, 23)

SECTION_KEYS = {
    "summary": {"total_requests", "total_tokens", "total_cost", "error_rate", "active_users", "active_keys"},
    "models": {"model_rank", "error_model_rank"},
    "users": {"user_rank"},
    "keys": {"key_rank"},
    "trend": {"trend"},
    "errors": {"recent_errors"},
    "active": {"recent_active_users"},
}
# SELECT statements per section: summary is one merged aggregate, models needs a
# second pass for the error ranking, and the trend panel adds the anomaly probe
# and the token GROUP BY next to the counter GROUP BY.
SECTION_SELECT_COUNT = {"summary": 1, "models": 2, "users": 1, "keys": 1, "trend": 3, "errors": 1, "active": 1}
LEGACY_KEY_ORDER = [
    "total_requests",
    "total_tokens",
    "total_cost",
    "error_rate",
    "active_users",
    "active_keys",
    "model_rank",
    "error_model_rank",
    "user_rank",
    "key_rank",
    "trend",
    "recent_errors",
    "recent_active_users",
]


class DashboardTestCase(DashboardTopModelMixin, TestCase):
    """Shared helpers: query accounting plus raw_payload fixtures."""

    def select_statements(self, call):
        """(result, SELECT statements) -- transaction control is not a query."""
        with CaptureQueriesContext(connection) as captured:
            result = call()
        statements = [
            query["sql"]
            for query in captured.captured_queries
            if query["sql"].lstrip().upper().startswith(("SELECT", "WITH"))
        ]
        return result, statements

    def add_payload_log(self, key, payload, request_id, request_time=None, model="gpt-5.4", total_tokens=100):
        prompt = total_tokens // 2
        return UsageLog.objects.create(
            request_id=request_id,
            user=key.user,
            api_key=key,
            user_identifier=key.user.username,
            api_key_identifier=key.token_prefix,
            model_name=model,
            prompt_tokens=prompt,
            completion_tokens=total_tokens - prompt,
            total_tokens=total_tokens,
            request_time=request_time or timezone.now(),
            raw_payload=payload,
        )

    def expected_token_totals(self, rows, trend_bucket):
        """The Python oracle: ``_payload_int`` per row, bucketed like the trend."""
        totals = {}
        for row in rows:
            label = _trend_label(row.request_time, trend_bucket)
            bucket = totals.setdefault(label, {"cached_tokens": 0, "thinking_tokens": 0})
            bucket["cached_tokens"] += _payload_int(row.raw_payload, *CACHED_TOKEN_PATHS)
            bucket["thinking_tokens"] += _payload_int(row.raw_payload, *THINKING_TOKEN_PATHS)
        return totals

    def payload_queryset(self):
        return UsageLog.objects.exclude(user=None).order_by("-request_time")


class DashboardSectionsTests(DashboardTestCase):
    """sections= computes only the requested panels and keeps the legacy payload."""

    def setUp(self):
        self.user = self.visible_user("SectionUser")
        self.key = self.api_key(self.user, "sk-section-token")
        self.add_usage_log(self.key, "gpt-5.4", 100, "sec-1")
        self.add_usage_log(self.key, "gpt-5.5", 300, "sec-2")
        UsageLog.objects.create(
            request_id="sec-error",
            user=self.user,
            api_key=self.key,
            user_identifier=self.user.username,
            model_name="gpt-5.4",
            prompt_tokens=5,
            completion_tokens=5,
            total_tokens=10,
            is_error=True,
            request_time=timezone.now(),
        )

    def test_default_call_returns_the_legacy_payload_in_the_legacy_key_order(self):
        data, statements = self.select_statements(lambda: aggregate_dashboard(self.payload_queryset()))

        self.assertEqual(list(data.keys()), LEGACY_KEY_ORDER)
        self.assertEqual(data["total_requests"], 3)
        self.assertEqual(data["total_tokens"], 410)
        self.assertEqual(data["error_rate"], 33.33)
        self.assertEqual(len(data["trend"]), 1)
        self.assertEqual(len(statements), 10)

    def test_each_section_computes_only_its_own_panel(self):
        queryset = self.payload_queryset()
        for section in DASHBOARD_SECTIONS:
            with self.subTest(section=section):
                data, statements = self.select_statements(
                    lambda section=section: aggregate_dashboard(queryset, sections=[section])
                )
                self.assertEqual(set(data), SECTION_KEYS[section])
                self.assertEqual(len(statements), SECTION_SELECT_COUNT[section])

    def test_summary_counts_come_from_one_merged_aggregate(self):
        data, statements = self.select_statements(
            lambda: aggregate_dashboard(self.payload_queryset(), sections=["summary"])
        )

        self.assertEqual(len(statements), 1)
        self.assertIn("COUNT(DISTINCT", statements[0])
        self.assertEqual(
            data,
            {
                "total_requests": 3,
                "total_tokens": 410,
                "total_cost": 0,
                "error_rate": 33.33,
                "active_users": 1,
                "active_keys": 1,
            },
        )

    def test_sections_subset_and_order_are_normalized(self):
        data = aggregate_dashboard(self.payload_queryset(), sections=("trend", "summary"))

        # Sections are returned in the canonical legacy order, not the requested one.
        self.assertEqual(list(data.keys()), ["total_requests", "total_tokens", "total_cost", "error_rate",
                                            "active_users", "active_keys", "trend"])

    def test_no_section_work_is_done_when_no_section_is_requested(self):
        data, statements = self.select_statements(
            lambda: aggregate_dashboard(self.payload_queryset(), sections=[])
        )

        self.assertEqual(data, {})
        self.assertEqual(statements, [])

    def test_unknown_sections_raise_and_a_bare_string_is_rejected(self):
        with self.assertRaises(ValueError):
            aggregate_dashboard(self.payload_queryset(), sections=["summary", "nope"])
        with self.assertRaises(TypeError):
            aggregate_dashboard(self.payload_queryset(), sections="summary")

    def test_empty_queryset_keeps_the_legacy_empty_values(self):
        empty = UsageLog.objects.none()

        full = aggregate_dashboard(empty)
        self.assertEqual(full["total_requests"], 0)
        self.assertEqual(full["total_tokens"], 0)
        self.assertEqual(full["total_cost"], 0)
        self.assertEqual(full["error_rate"], 0)
        self.assertEqual(full["key_rank"], [])
        self.assertEqual(full["trend"], [])
        self.assertEqual(full["recent_errors"], [])
        self.assertEqual(full["recent_active_users"], [])
        self.assertEqual(aggregate_dashboard(empty, sections=["keys", "trend"]), {"key_rank": [], "trend": []})

    def test_key_pagination_metadata_appears_only_when_requested(self):
        without_page, _ = self.select_statements(
            lambda: aggregate_dashboard(self.payload_queryset(), sections=["keys"])
        )
        with_page, statements = self.select_statements(
            lambda: aggregate_dashboard(self.payload_queryset(), sections=["keys"], key_page=1)
        )

        self.assertNotIn("key_pagination", without_page)
        self.assertEqual(len(statements), 2)
        self.assertEqual(
            set(with_page["key_pagination"]),
            {"page", "num_pages", "total", "has_next", "has_previous"},
        )


class DashboardTokenAggregateTests(DashboardTestCase):
    """The SQL token aggregate must equal the legacy ``_payload_int`` scan."""

    def setUp(self):
        self.user = self.visible_user("TokenUser")
        self.key = self.api_key(self.user, "sk-token-token")
        self.rows = [
            self.add_payload_log(self.key, payload, f"payload-{index}", total_tokens=index)
            for index, payload in enumerate(TOKEN_PAYLOADS)
        ]

    def test_sql_value_equals_payload_int_for_every_row_sql_can_express(self):
        rows = list(
            UsageLog.objects.order_by("id").annotate(
                db_cached=services._SafeJsonInt(CACHED_TOKEN_PATHS),
                db_thinking=services._SafeJsonInt(THINKING_TOKEN_PATHS),
                sql_inexpressible=services._JsonTokenAnomaly(CACHED_TOKEN_PATHS + THINKING_TOKEN_PATHS),
            )
        )

        for index, (row, payload) in enumerate(zip(rows, TOKEN_PAYLOADS)):
            with self.subTest(payload=payload):
                if index in MUST_CORRECT_PAYLOAD_INDEXES:
                    self.assertTrue(row.sql_inexpressible, f"payload {index} must be corrected in Python")
                elif index in SQL_EXACT_PAYLOAD_INDEXES:
                    self.assertFalse(row.sql_inexpressible, f"payload {index} needs no Python correction")
                if not row.sql_inexpressible:
                    self.assertEqual(row.db_cached, _payload_int(payload, *CACHED_TOKEN_PATHS))
                    self.assertEqual(row.db_thinking, _payload_int(payload, *THINKING_TOKEN_PATHS))

    def test_trend_tokens_match_the_python_oracle_for_every_historical_layout(self):
        base = UsageLog.objects.filter(request_id__startswith="payload-")
        corrected_ids = [f"payload-{index}" for index in MUST_CORRECT_PAYLOAD_INDEXES]
        cases = (
            ("every layout", base),
            ("sql-exact layouts only", base.exclude(request_id__in=corrected_ids)),
        )
        for trend_bucket in ("day", "hour", "month"):
            for label, queryset in cases:
                with self.subTest(bucket=trend_bucket, case=label):
                    ordered = queryset.order_by("-request_time")
                    data = aggregate_dashboard(ordered, trend_bucket=trend_bucket, sections=["trend"])
                    expected = self.expected_token_totals(list(ordered), trend_bucket)

                    self.assertEqual(len(data["trend"]), len(expected))
                    for entry in data["trend"]:
                        bucket = expected[entry["date"]]
                        self.assertEqual(entry["cached_tokens"], bucket["cached_tokens"])
                        self.assertEqual(entry["thinking_tokens"], bucket["thinking_tokens"])

    def test_anomalous_rows_are_corrected_instead_of_the_whole_column_being_scanned(self):
        queryset = UsageLog.objects.filter(request_id__startswith="payload-")
        oracle = self.expected_token_totals(list(queryset), "day")
        scanned = []

        def counting_fallback(inner_queryset, trend_bucket):
            scanned.append(inner_queryset)
            return services._token_totals_from_payload_scan(inner_queryset, trend_bucket)

        with mock.patch.object(services, "_token_totals_from_payload_scan", counting_fallback):
            data = aggregate_dashboard(queryset, sections=["trend"])

        # The Python scan is not used at all: the exotic rows are fetched by the
        # anomaly predicate and merged into the SQL totals.
        self.assertEqual(scanned, [])
        expected = oracle[next(iter(oracle))]
        self.assertEqual(data["trend"][0]["cached_tokens"], expected["cached_tokens"])
        self.assertEqual(data["trend"][0]["thinking_tokens"], expected["thinking_tokens"])
        # 1234567890123456789012345 cannot live in a SQL integer column, so it can
        # only show up in the totals if the Python correction really ran.
        self.assertGreater(expected["cached_tokens"], 1234567890123456789012345)

    def test_vendor_without_a_verified_expression_falls_back_to_the_scan(self):
        queryset = UsageLog.objects.filter(request_id__startswith="payload-")
        queryset_without_anomalies = queryset.exclude(
            services._JsonTokenAnomaly(CACHED_TOKEN_PATHS + THINKING_TOKEN_PATHS)
        ).filter(request_id__in=["payload-0", "payload-1", "payload-2", "payload-3", "payload-4"])

        with mock.patch.object(services, "_DB_TOKEN_VENDORS", ()):
            with mock.patch.object(services, "_token_totals_from_payload_scan") as scan:
                scan.return_value = {"sentinel": {"cached_tokens": 1, "thinking_tokens": 2}}
                data = aggregate_dashboard(queryset_without_anomalies, sections=["trend"])

        scan.assert_called_once()
        # Only the token totals come from the fallback; the counters still come
        # from the database GROUP BY.
        self.assertEqual([entry["tokens"] for entry in data["trend"]], [10])
        self.assertEqual([entry["cached_tokens"] for entry in data["trend"]], [0])

    def test_database_error_in_the_aggregate_falls_back_to_the_scan(self):
        queryset = UsageLog.objects.filter(request_id__startswith="payload-")
        oracle = self.expected_token_totals(list(queryset), "day")
        expected = oracle[next(iter(oracle))]

        with mock.patch.object(services, "_JsonTokenAnomaly", side_effect=OperationalError("boom")):
            data = aggregate_dashboard(queryset, sections=["trend"])

        entry = data["trend"][0]
        self.assertEqual(entry["cached_tokens"], expected["cached_tokens"])
        self.assertEqual(entry["thinking_tokens"], expected["thinking_tokens"])

    def test_trend_entries_keep_the_legacy_shape(self):
        data = aggregate_dashboard(self.payload_queryset(), sections=["trend"])

        self.assertEqual(
            list(data["trend"][0].keys()),
            ["date", "tokens", "prompt_tokens", "completion_tokens", "cached_tokens", "thinking_tokens",
             "requests", "errors", "error_rate"],
        )


class DashboardTrendBucketTests(DashboardTestCase):
    """trend_bucket hour/day/month labels and per-bucket token sums."""

    def setUp(self):
        self.user = self.visible_user("BucketUser")
        self.key = self.api_key(self.user, "sk-bucket-token")
        self.moments = [timezone.now(), timezone.now() - timedelta(days=1), timezone.now() - timedelta(days=40)]

    def add_bucket_logs(self, moment, request_id, cached_tokens):
        return self.add_payload_log(
            self.key,
            {"tokens": {"cached_tokens": cached_tokens, "reasoning_tokens": cached_tokens * 2}},
            request_id,
            request_time=moment,
        )

    def test_day_trend_buckets_by_local_date(self):
        for index, moment in enumerate(self.moments):
            self.add_bucket_logs(moment, f"bucket-day-{index}", 10)

        data = aggregate_dashboard(self.payload_queryset(), trend_bucket="day", sections=["trend"])

        self.assertEqual(
            [entry["date"] for entry in data["trend"]],
            sorted({_trend_label(moment, "day") for moment in self.moments}),
        )
        for entry in data["trend"]:
            self.assertEqual(entry["cached_tokens"], 10)
            self.assertEqual(entry["thinking_tokens"], 20)

    def test_hour_trend_buckets_by_local_hour(self):
        for index, moment in enumerate(self.moments):
            self.add_bucket_logs(moment, f"bucket-hour-{index}", 1)

        data = aggregate_dashboard(self.payload_queryset(), trend_bucket="hour", sections=["trend"])

        self.assertEqual(
            [entry["date"] for entry in data["trend"]],
            sorted({_trend_label(moment, "hour") for moment in self.moments}),
        )
        for entry in data["trend"]:
            self.assertRegex(entry["date"], r"^\d{2}:\d{2}$")

    def test_month_trend_buckets_by_calendar_month(self):
        local_zone = timezone.get_current_timezone()
        august = datetime(2026, 8, 15, 9, 0, tzinfo=local_zone)
        september = datetime(2026, 9, 2, 23, 30, tzinfo=local_zone)
        self.add_bucket_logs(august, "bucket-month-aug", 3)
        self.add_bucket_logs(september, "bucket-month-sep-1", 5)
        self.add_bucket_logs(september, "bucket-month-sep-2", 7)

        data = aggregate_dashboard(self.payload_queryset(), trend_bucket="month", sections=["trend"])

        self.assertEqual([entry["date"] for entry in data["trend"]], ["2026-08", "2026-09"])
        august_entry, september_entry = data["trend"]
        self.assertEqual(august_entry["cached_tokens"], 3)
        self.assertEqual(august_entry["requests"], 1)
        self.assertEqual(september_entry["cached_tokens"], 12)
        self.assertEqual(september_entry["thinking_tokens"], 24)
        self.assertEqual(september_entry["requests"], 2)

    def test_month_bucket_tokens_match_the_python_oracle(self):
        self.add_bucket_logs(self.moments[0], "bucket-mixed-today", 2)
        self.add_bucket_logs(self.moments[1], "bucket-mixed-yesterday", 4)
        queryset = UsageLog.objects.filter(user=self.user).order_by("-request_time")

        data = aggregate_dashboard(queryset, trend_bucket="month", sections=["trend"])
        expected = self.expected_token_totals(list(queryset), "month")

        self.assertEqual(len(data["trend"]), len(expected))
        for entry in data["trend"]:
            self.assertRegex(entry["date"], r"^\d{4}-\d{2}$")
            self.assertEqual(entry["cached_tokens"], expected[entry["date"]]["cached_tokens"])
            self.assertEqual(entry["thinking_tokens"], expected[entry["date"]]["thinking_tokens"])

    def test_unknown_bucket_keeps_the_legacy_day_view(self):
        self.add_bucket_logs(timezone.now(), "bucket-unknown", 8)

        data = aggregate_dashboard(self.payload_queryset(), trend_bucket="week", sections=["trend"])

        self.assertEqual([entry["date"] for entry in data["trend"]], [_trend_label(timezone.now(), "day")])


class DashboardKeyPaginationTests(DashboardTestCase):
    """key_rank pagination: stable order, bounded page size, top_model on the page."""

    def setUp(self):
        self.tokens = [500, 400, 300, 200, 100]
        for index, tokens in enumerate(self.tokens):
            user = self.visible_user(f"PageUser{index}")
            key = self.api_key(user, f"sk-page-token-{index}")
            self.add_usage_log(key, "gpt-5.4", tokens, f"page-{index}-a")
            self.add_usage_log(key, "gpt-5.5", tokens // 2, f"page-{index}-b")
        self.hidden_user = self.visible_user("HiddenPageUser", visible=False)
        hidden_key = self.api_key(self.hidden_user, "sk-page-hidden-token")
        self.add_usage_log(hidden_key, "gpt-5.4", 10 ** 6, "page-hidden")

    def page(self, **kwargs):
        return aggregate_dashboard(self.payload_queryset(), sections=["keys"], **kwargs)

    def test_first_middle_and_last_page_metadata(self):
        first = self.page(key_page=1, key_page_size=2)
        self.assertEqual(first["key_pagination"],
                         {"page": 1, "num_pages": 3, "total": 5, "has_next": True, "has_previous": False})
        self.assertEqual([row["total_tokens"] for row in first["key_rank"]], [750, 600])

        middle = self.page(key_page=2, key_page_size=2)
        self.assertEqual(middle["key_pagination"],
                         {"page": 2, "num_pages": 3, "total": 5, "has_next": True, "has_previous": True})
        self.assertEqual([row["total_tokens"] for row in middle["key_rank"]], [450, 300])

        last = self.page(key_page=3, key_page_size=2)
        self.assertEqual(last["key_pagination"],
                         {"page": 3, "num_pages": 3, "total": 5, "has_next": False, "has_previous": True})
        self.assertEqual([row["total_tokens"] for row in last["key_rank"]], [150])

    def test_hidden_keys_are_excluded_from_rows_and_from_the_total(self):
        data = self.page(key_page=1, key_page_size=25)

        self.assertEqual(data["key_pagination"]["total"], 5)
        self.assertNotIn("HiddenPageUser 队员", {row["display_name"] for row in data["key_rank"]})

    def test_out_of_range_pages_are_clamped_like_django_paginator(self):
        self.assertEqual(self.page(key_page=99, key_page_size=2)["key_pagination"]["page"], 3)
        self.assertEqual(self.page(key_page=0, key_page_size=2)["key_pagination"]["page"], 1)
        self.assertEqual(self.page(key_page=-4, key_page_size=2)["key_pagination"]["page"], 1)

    def test_page_size_is_bounded_and_validated(self):
        offset, limit, metadata = services._dashboard_key_page_window(120, 1, 1000)
        self.assertEqual((offset, limit), (0, DASHBOARD_KEY_PAGE_SIZE_LIMIT))
        self.assertEqual(metadata["num_pages"], 3)

        self.assertEqual(services._dashboard_key_page_window(120, 3, 50)[0], 100)
        self.assertEqual(len(self.page(key_page=1, key_page_size=50)["key_rank"]), 5)
        for bad_value in ("abc", 1.5, True):
            with self.subTest(value=bad_value):
                with self.assertRaises(ValueError):
                    self.page(key_page=bad_value)
                with self.assertRaises(ValueError):
                    self.page(key_page=1, key_page_size=bad_value)

        # None is the "no pagination" sentinel for key_page (it is the default), but it
        # is not a valid page size.
        self.assertNotIn("key_pagination", self.page(key_page=None))
        with self.assertRaises(ValueError):
            self.page(key_page=1, key_page_size=None)

    def test_pagination_runs_the_top_model_subquery_only_for_the_page(self):
        data, statements = self.select_statements(lambda: self.page(key_page=1, key_page_size=2))

        self.assertEqual(len(statements), 2)
        self.assertNotIn("LIMIT", statements[0].upper())  # the count query is the plain grouped one
        self.assertIn("DESC", statements[1])
        self.assertIn("api_key", statements[1])
        self.assertIn("LIMIT 2", statements[1].upper())
        for row in data["key_rank"]:
            self.assertIn(row["top_model"], {"gpt-5.4", "gpt-5.5"})
            self.assertIn("group_name", row)
            self.assertIn("grade", row)

    def test_equal_token_totals_keep_a_stable_order(self):
        for index in range(3):
            user = self.visible_user(f"TieUser{index}")
            key = self.api_key(user, f"sk-tie-token-{index}")
            self.add_usage_log(key, "gpt-5.4", 50, f"tie-{index}")

        first = [row["total_tokens"] for row in self.page(key_page=1, key_page_size=25)["key_rank"]]
        second = [row["total_tokens"] for row in self.page(key_page=1, key_page_size=25)["key_rank"]]
        ties = [row["display_name"] for row in self.page(key_page=1, key_page_size=25)["key_rank"]
                if row["total_tokens"] == 50]

        self.assertEqual(first, second)
        self.assertEqual(ties, sorted(ties))

    def test_top_model_on_a_page_matches_the_unpaginated_panel(self):
        unpaginated = {row["display_name"]: row for row in self.page()["key_rank"]}

        for page in (1, 2, 3):
            for row in self.page(key_page=page, key_page_size=2)["key_rank"]:
                self.assertEqual(row, unpaginated[row["display_name"]])


class DashboardSqlBuilderTests(TestCase):
    """The generated SQL for both supported vendors (no database needed)."""

    def test_sqlite_expression_uses_json_extract_and_a_bounded_literal_test(self):
        sql = _build_safe_json_int_sql("sqlite", '"usage_usagelog"."raw_payload"', CACHED_TOKEN_PATHS)

        self.assertEqual(sql.count("COALESCE("), 1)
        self.assertIn("JSON_EXTRACT(", sql)
        self.assertIn("$.\"raw_payload\".\"tokens\".\"cached_tokens\"", sql)
        self.assertIn("TYPEOF(", sql)
        self.assertIn("GLOB '*[^0-9]*'", sql)
        self.assertIn("REPLACE(", sql)
        self.assertIn("BETWEEN 1 AND 18", sql)
        # The sign stripped for validation must come back for the value itself.
        self.assertIn("THEN '-' ELSE '' END", sql)

    def test_postgresql_expression_uses_jsonb_operators(self):
        sql = _build_safe_json_int_sql("postgresql", '"usage_usagelog"."raw_payload"', CACHED_TOKEN_PATHS)

        self.assertEqual(sql.count("COALESCE("), 1)
        # The JSON path is rendered as a PostgreSQL text[] literal, whose elements
        # are quoted: #> '{"raw_payload","tokens","cached_tokens}'.
        self.assertIn('#> \'{"raw_payload","tokens","cached_tokens"}\'', sql)
        self.assertIn('#>> \'{"usage","cached_tokens"}\'', sql)
        self.assertIn("JSONB_TYPEOF(", sql)
        self.assertIn("~ '^[0-9]{1,18}$'", sql)
        self.assertIn("TRUNC(", sql)
        self.assertIn("::bigint", sql)
        self.assertIn("THEN '-' ELSE '' END", sql)

    def test_anomaly_predicate_lists_every_path(self):
        sqlite_sql = _build_json_token_anomaly_sql(
            "sqlite", '"usage_usagelog"."raw_payload"', CACHED_TOKEN_PATHS + THINKING_TOKEN_PATHS
        )
        postgres_sql = _build_json_token_anomaly_sql(
            "postgresql", '"usage_usagelog"."raw_payload"', CACHED_TOKEN_PATHS + THINKING_TOKEN_PATHS
        )

        for path in CACHED_TOKEN_PATHS + THINKING_TOKEN_PATHS:
            with self.subTest(path=path):
                self.assertIn(f'"tokens"."{path[-1]}"' if len(path) > 1 else f'."{path[-1]}"', sqlite_sql)
        self.assertIn("'text'", sqlite_sql)
        self.assertIn("'string'", postgres_sql)
        self.assertIn("'number'", postgres_sql)
        self.assertIn("NOT (", postgres_sql)

    def test_postgresql_sql_parses_as_a_postgresql_statement(self):
        try:
            import pglast
        except ImportError:  # pragma: no cover - optional tooling
            self.skipTest("pglast is not installed; PostgreSQL syntax check skipped")

        for paths in (CACHED_TOKEN_PATHS, THINKING_TOKEN_PATHS):
            statement = "SELECT " + _build_safe_json_int_sql("postgresql", "raw_payload", paths)
            conditional = "SELECT " + _build_json_token_anomaly_sql("postgresql", "raw_payload", paths)
            with self.subTest(paths=paths[:1]):
                pglast.parse_sql(statement)
                pglast.parse_sql(conditional)

    def test_unsupported_vendor_is_rejected(self):
        with self.assertRaises(services.NotSupportedError):
            _build_safe_json_int_sql("mysql", "raw_payload", CACHED_TOKEN_PATHS)

    def test_unsupported_json_path_is_rejected(self):
        with self.assertRaises(ValueError):
            _build_safe_json_int_sql("sqlite", "raw_payload", [("weird\"key",)])


POSTGRES_DSN = os.environ.get("PORTAL_TEST_POSTGRES_DSN")


@unittest.skipUnless(POSTGRES_DSN, "set PORTAL_TEST_POSTGRES_DSN to run the PostgreSQL checks")
class DashboardPostgresIntegrationTests(TestCase):
    """Runs the PostgreSQL expression against a real server.

    The expression is the only vendor-specific part of the token aggregate, so it
    is checked directly against ``_payload_int``: exact for every row the anomaly
    predicate does not flag, and flagged for the rows only Python can read -- the
    same two properties the SQLite tests assert through the ORM.
    """

    def setUp(self):
        try:
            import psycopg2
        except ImportError:  # pragma: no cover - driver may be absent
            self.skipTest("psycopg2 is not installed; PostgreSQL integration check skipped")
        self.connection = psycopg2.connect(POSTGRES_DSN)
        self.addCleanup(self.connection.close)
        self.cursor = self.connection.cursor()
        self.cursor.execute("CREATE TEMP TABLE dashboard_payloads (id serial primary key, raw_payload jsonb)")
        self.cursor.executemany(
            "INSERT INTO dashboard_payloads (raw_payload) VALUES (%s)",
            [(json.dumps(payload),) for payload in TOKEN_PAYLOADS],
        )

    def anomaly_sql(self):
        """The production predicate: one flag for both token families."""
        return _build_json_token_anomaly_sql(
            "postgresql", "raw_payload", CACHED_TOKEN_PATHS + THINKING_TOKEN_PATHS
        )

    def rows(self, paths):
        value_sql = _build_safe_json_int_sql("postgresql", "raw_payload", paths)
        anomaly_sql = self.anomaly_sql()
        self.cursor.execute(f"SELECT id, {value_sql}, {anomaly_sql} FROM dashboard_payloads ORDER BY id")
        return [(row[0], row[1], row[2]) for row in self.cursor.fetchall()]

    def test_postgresql_expression_matches_payload_int(self):
        for paths in (CACHED_TOKEN_PATHS, THINKING_TOKEN_PATHS):
            for index, value, inexpressible in self.rows(paths):
                payload = TOKEN_PAYLOADS[index - 1]
                with self.subTest(paths=paths[:1], payload=payload):
                    self.assertEqual(
                        inexpressible,
                        index - 1 in MUST_CORRECT_PAYLOAD_INDEXES,
                        "the anomaly predicate must select exactly the rows Python has to correct",
                    )
                    if not inexpressible:
                        self.assertEqual(value, _payload_int(payload, *paths))

    def test_postgresql_totals_match_the_python_oracle(self):
        for paths in (CACHED_TOKEN_PATHS, THINKING_TOKEN_PATHS):
            value_sql = _build_safe_json_int_sql("postgresql", "raw_payload", paths)
            anomaly_sql = self.anomaly_sql()
            self.cursor.execute(
                f"SELECT COALESCE(SUM({value_sql}), 0) FROM dashboard_payloads WHERE NOT ({anomaly_sql})"
            )
            sql_total = self.cursor.fetchone()[0]
            self.cursor.execute(f"SELECT id FROM dashboard_payloads WHERE {anomaly_sql} ORDER BY id")
            flagged = [row[0] - 1 for row in self.cursor.fetchall()]
            corrected = sql_total + sum(_payload_int(TOKEN_PAYLOADS[index], *paths) for index in flagged)

            self.assertEqual(corrected, sum(_payload_int(payload, *paths) for payload in TOKEN_PAYLOADS))
