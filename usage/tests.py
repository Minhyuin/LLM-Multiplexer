import requests
import os
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.db.models import Sum
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from api_keys.models import APIKey
from users.models import Profile
from usage.models import UsageImportJob, UsageLog
from usage.services import (
    _extract_keeper_events_page,
    aggregate_dashboard,
    auto_sync_cliproxy_usage_records,
    fetch_keeper_usage_events,
    import_keeper_usage_events,
    import_usage_records,
    sync_cliproxy_usage_records,
)

class MyUsageLogsPageTests(TestCase):
    def test_page_uses_plain_usage_record_copy_without_key_column(self):
        user = User.objects.create_user(username="member", password="password")
        self.client.force_login(user)

        response = self.client.get(reverse("my-usage-logs"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "我的使用记录")
        self.assertNotContains(response, "我的 Usage 记录")
        self.assertNotContains(response, "<th>Key</th>", html=True)


class UsageImportAssociationTests(TestCase):
    def test_existing_unmatched_log_gets_association_when_key_is_bound_later(self):
        user = User.objects.create_user(username="member", password="password")
        raw_token = "sk-member-token"
        metadata = APIKey.build_token_metadata(raw_token)
        key = APIKey.objects.create(
            user=user,
            name="API 密钥",
            token_hash=metadata["token_hash"],
            token_prefix=metadata["token_prefix"],
            token_masked=metadata["token_masked"],
            token_plaintext=raw_token,
            source=APIKey.SOURCE_CLIPROXY,
            status=APIKey.STATUS_ACTIVE,
        )
        UsageLog.objects.create(
            request_id="request-1",
            api_key_identifier=raw_token,
            model_name="gpt-5.4",
            total_tokens=100,
            request_time=timezone.now(),
        )

        import_usage_records(
            [
                {
                    "request_id": "request-1",
                    "api_key_identifier": raw_token,
                    "model_name": "gpt-5.4",
                    "total_tokens": 100,
                    "request_time": timezone.now().isoformat(),
                }
            ],
            UsageImportJob.SOURCE_COMMAND,
            "repair-test",
        )

        log = UsageLog.objects.get(request_id="request-1")
        self.assertEqual(log.user, user)
        self.assertEqual(log.api_key, key)


class CliProxySyncFallbackTests(TestCase):
    def test_auto_sync_throttle_returns_latest_keeper_job(self):
        job = UsageImportJob.objects.create(
            source_type=UsageImportJob.SOURCE_API,
            source_name="cpa-usage-keeper",
            imported_count=3,
        )

        with (
            patch("usage.services.LAST_SYNC_AT", 9999999999.0),
            patch("usage.services.time.time", return_value=9999999999.0),
        ):
            latest_job, triggered = auto_sync_cliproxy_usage_records()

        self.assertFalse(triggered)
        self.assertEqual(latest_job, job)

    def test_sync_uses_usage_queue_when_legacy_usage_endpoint_is_missing(self):
        user = User.objects.create_user(username="member", password="password")
        raw_token = "sk-queue-token"
        metadata = APIKey.build_token_metadata(raw_token)
        APIKey.objects.create(
            user=user,
            name="API 密钥",
            token_hash=metadata["token_hash"],
            token_prefix=metadata["token_prefix"],
            token_masked=metadata["token_masked"],
            token_plaintext=raw_token,
            source=APIKey.SOURCE_CLIPROXY,
            status=APIKey.STATUS_ACTIVE,
        )
        response = requests.Response()
        response.status_code = 404
        missing_usage_endpoint = requests.HTTPError(response=response)
        queue_event = {
            "request_id": "queue-request-1",
            "api_key": raw_token,
            "source": "",
            "model": "gpt-5.5",
            "provider": "codex",
            "failed": False,
            "latency_ms": 1234,
            "timestamp": timezone.now().isoformat(),
            "tokens": {
                "input_tokens": 12,
                "output_tokens": 8,
                "reasoning_tokens": 3,
                "cached_tokens": 4,
                "total_tokens": 20,
            },
        }

        with (
            patch.dict(os.environ, {"CPA_USAGE_KEEPER_BASE_URL": "", "CPA_USAGE_KEEPER_USERNAME": "", "CPA_USAGE_KEEPER_PASSWORD": "", "CPA_USAGE_KEEPER_DB": ""}),
            patch("usage.services.fetch_cliproxy_usage_records", side_effect=missing_usage_endpoint),
            patch("usage.services.fetch_usage_queue_events", return_value=[queue_event]),
        ):
            job = sync_cliproxy_usage_records()

        self.assertEqual(job.source_name, "cliproxy-usage-queue")
        self.assertEqual(job.imported_count, 1)
        log = UsageLog.objects.get(request_id="queue-request-1")
        self.assertEqual(log.user, user)
        self.assertEqual(log.model_name, "gpt-5.5")
        self.assertEqual(log.total_tokens, 20)
        self.assertEqual(log.cached_tokens, 4)
        self.assertEqual(log.reasoning_tokens, 3)

    def test_sync_prefers_keeper_when_legacy_usage_endpoint_is_missing_and_keeper_is_configured(self):
        user = User.objects.create_user(username="member", password="password")
        raw_token = "sk-5VnRiyWZhangYanMing"
        metadata = APIKey.build_token_metadata(raw_token)
        APIKey.objects.create(
            user=user,
            name="API 密钥",
            token_hash=metadata["token_hash"],
            token_prefix=metadata["token_prefix"],
            token_masked=metadata["token_masked"],
            token_plaintext=raw_token,
            source=APIKey.SOURCE_CLIPROXY,
            status=APIKey.STATUS_ACTIVE,
        )
        response = requests.Response()
        response.status_code = 404
        keeper_event = {
            "id": "96886",
            "timestamp": timezone.now().isoformat(),
            "api_key": "sk-*********YanMing",
            "model": "gpt-5.5",
            "source_type": "codex",
            "failed": False,
            "latency_ms": 38266,
            "tokens": {
                "input_tokens": 19317,
                "output_tokens": 2005,
                "reasoning_tokens": 516,
                "cached_tokens": 3840,
                "total_tokens": 21322,
            },
        }

        with (
            patch.dict(os.environ, {"CPA_USAGE_KEEPER_BASE_URL": "http://keeper", "CPA_USAGE_KEEPER_USERNAME": "admin", "CPA_USAGE_KEEPER_PASSWORD": "secret", "CPA_USAGE_KEEPER_DB": ""}),
            patch("usage.services.fetch_cliproxy_usage_records", side_effect=requests.HTTPError(response=response)),
            patch("usage.services.fetch_keeper_usage_events", return_value=[keeper_event]),
            patch("usage.services.fetch_usage_queue_events") as queue_fetch,
        ):
            job = sync_cliproxy_usage_records()

        queue_fetch.assert_not_called()
        self.assertEqual(job.source_name, "cpa-usage-keeper")
        self.assertEqual(job.imported_count, 1)
        log = UsageLog.objects.get(request_id="keeper-96886")
        self.assertEqual(log.user, user)
        self.assertEqual(log.total_tokens, 21322)


class KeeperUsageImportTests(TestCase):
    def test_keeper_fetch_defaults_to_supported_7d_range(self):
        class DummyResponse:
            def __init__(self, payload):
                self.payload = payload

            def raise_for_status(self):
                return None

            def json(self):
                return self.payload

        class DummySession:
            def __init__(self):
                self.params = None

            def post(self, *args, **kwargs):
                return DummyResponse({})

            def get(self, *args, **kwargs):
                self.params = kwargs["params"]
                return DummyResponse({"events": [{"id": "1"}], "total_pages": 1})

        session = DummySession()

        with (
            patch.dict(os.environ, {"CPA_USAGE_KEEPER_BASE_URL": "http://keeper", "CPA_USAGE_KEEPER_USERNAME": "admin", "CPA_USAGE_KEEPER_PASSWORD": "secret", "CPA_USAGE_KEEPER_DB": ""}),
            patch("usage.services.requests.Session", return_value=session),
        ):
            events = fetch_keeper_usage_events(page_size=20, max_pages=1)

        self.assertEqual(session.params["range"], "7d")
        self.assertEqual(events, [{"id": "1"}])

    def test_keeper_payload_extracts_nested_data_events(self):
        events, total_pages = _extract_keeper_events_page(
            {
                "data": {
                    "events": [{"id": "96884"}],
                    "pagination": {"total_pages": 3},
                }
            },
            current_page=1,
        )

        self.assertEqual(events, [{"id": "96884"}])
        self.assertEqual(total_pages, 3)

    def test_keeper_events_match_portal_key_by_masked_suffix(self):
        user = User.objects.create_user(username="member", password="password")
        raw_token = "sk-5VnRiyWZhangYanMing"
        metadata = APIKey.build_token_metadata(raw_token)
        APIKey.objects.create(
            user=user,
            name="API 密钥",
            token_hash=metadata["token_hash"],
            token_prefix=metadata["token_prefix"],
            token_masked=metadata["token_masked"],
            token_plaintext=raw_token,
            source=APIKey.SOURCE_CLIPROXY,
            status=APIKey.STATUS_ACTIVE,
        )

        job = import_keeper_usage_events([
            {
                "id": "96884",
                "timestamp": "2026-07-09T08:23:51.017088953+08:00",
                "api_key": "sk-*********YanMing",
                "model": "gpt-5.5",
                "source_type": "codex",
                "failed": False,
                "latency_ms": 6769,
                "tokens": {
                    "input_tokens": 73366,
                    "output_tokens": 144,
                    "reasoning_tokens": 0,
                    "cached_tokens": 70144,
                    "total_tokens": 73510,
                },
            }
        ])

        self.assertEqual(job.imported_count, 1)
        log = UsageLog.objects.get(request_id="keeper-96884")
        self.assertEqual(log.user, user)
        self.assertEqual(log.prompt_tokens, 73366)
        self.assertEqual(log.completion_tokens, 144)
        self.assertEqual(log.cached_tokens, 70144)


class KeeperSqliteImportTests(TestCase):
    """The masked key from the HTTP API cannot separate two keys that share a
    6-character suffix, so events are matched on the full key from SQLite."""

    def _make_key(self, username, raw_token):
        user = User.objects.create_user(username=username, password="password")
        metadata = APIKey.build_token_metadata(raw_token)
        return user, APIKey.objects.create(
            user=user,
            name="API",
            token_hash=metadata["token_hash"],
            token_prefix=metadata["token_prefix"],
            token_masked=metadata["token_masked"],
            token_plaintext=raw_token,
            source=APIKey.SOURCE_CLIPROXY,
            status=APIKey.STATUS_ACTIVE,
        )

    def _event(self, event_id, full_key):
        return {
            "id": event_id,
            "api_group_key": full_key,
            "api_key": full_key,
            "timestamp": timezone.now().isoformat(),
            "model": "gpt-5.5",
            "model_alias": "gpt-5.5",
            "failed": False,
            "latency_ms": 10,
            "tokens": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        }

    def test_colliding_suffix_keys_are_attributed_to_the_right_user(self):
        yu, _ = self._make_key("YuMingYuan", "sk-wTob3GcYuMingYuan")
        shi, _ = self._make_key("ShiNingYuan", "sk-b5NOFPVShiNingYuan")

        import_keeper_usage_events([
            self._event("1", "sk-wTob3GcYuMingYuan"),
            self._event("2", "sk-b5NOFPVShiNingYuan"),
        ], source_name="cpa-usage-keeper-db")

        self.assertEqual(UsageLog.objects.get(request_id="keeper-1").user, yu)
        self.assertEqual(UsageLog.objects.get(request_id="keeper-2").user, shi)

    def test_unmatched_event_is_still_imported_so_totals_match(self):
        job = import_keeper_usage_events(
            [self._event("3", "sk-nobodyKnowsThisKey")],
            source_name="cpa-usage-keeper-db",
        )

        self.assertEqual(job.imported_count, 1)
        self.assertIn("unmatched=1", job.summary)
        log = UsageLog.objects.get(request_id="keeper-3")
        self.assertIsNone(log.user)
        self.assertEqual(log.total_tokens, 15)


class DashboardTopModelMixin:
    """Shared fixtures for the 队员 Token 使用情况 panel (``key_rank``)."""

    # aggregate_dashboard runs a fixed set of aggregate queries; the top-model
    # lookup rides inside the key_rank SELECT as a correlated subquery.
    DASHBOARD_QUERY_COUNT = 12

    def visible_user(self, username, display_name=None, lab_group="算法", visible=True):
        user = User.objects.create_user(username=username, password="password")
        Profile.objects.update_or_create(
            user=user,
            defaults={
                # _preferred_display_name() shows the profile name only when it
                # differs from the username, so defaulting to the username keeps
                # key_rank rows keyed by the username the test chose.
                "display_name": display_name or f"{username} 队员",
                "lab_group": lab_group,
                "grade": "2024",
                "is_dashboard_visible": visible,
            },
        )
        return user

    def api_key(self, user, raw_token, name="API"):
        metadata = APIKey.build_token_metadata(raw_token)
        return APIKey.objects.create(
            user=user,
            name=name,
            token_hash=metadata["token_hash"],
            token_prefix=metadata["token_prefix"],
            token_masked=metadata["token_masked"],
            token_plaintext=raw_token,
            source=APIKey.SOURCE_CLIPROXY,
            status=APIKey.STATUS_ACTIVE,
        )

    def add_usage_log(self, key, model, total_tokens, request_id, hours_ago=0):
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
            request_time=timezone.now() - timedelta(hours=hours_ago),
        )

    def dashboard(self, queryset=None):
        """Aggregate the same queryset shape the public/admin dashboard passes in."""
        if queryset is None:
            queryset = UsageLog.objects.exclude(user=None).order_by("-request_time")
        return aggregate_dashboard(queryset)

    def key_rows(self, data):
        return {row["display_name"]: row for row in data["key_rank"]}


class DashboardTopModelTests(DashboardTopModelMixin, TestCase):
    """Regression tests for 最高使用模型 in 队员 Token 使用情况.

    The panel used to annotate ``Max("model_name")``, i.e. the lexicographically
    largest model name, not the model that actually consumed the most tokens.
    """

    def test_top_model_is_highest_summed_tokens_not_lexicographic_max(self):
        user = self.visible_user("YuMingYuan")
        key = self.api_key(user, "sk-wTob3GcYuMingYuan")

        # gpt-5.4 wins on total tokens but sorts BELOW the test-only name.
        for index in range(5):
            self.add_usage_log(key, "gpt-5.4", 200, f"a-{index}")
        # The lexicographic maximum: same call count, far fewer tokens.
        for index in range(5):
            self.add_usage_log(key, "测试专用-zz-模型", 20, f"b-{index}")

        row = self.key_rows(self.dashboard())["YuMingYuan 队员"]

        self.assertEqual(row["top_model"], "gpt-5.4")
        self.assertEqual(row["total_tokens"], 1100)
        self.assertEqual(row["total_requests"], 10)
        # Sanity-check against the raw sums, so the expectation is data-driven.
        tokens_by_model = {
            entry["model_name"]: entry["total_tokens"]
            for entry in UsageLog.objects.filter(api_key=key)
            .values("model_name")
            .annotate(total_tokens=Sum("total_tokens"))
        }
        self.assertEqual(tokens_by_model["gpt-5.4"], 1000)
        self.assertEqual(tokens_by_model["测试专用-zz-模型"], 100)
        self.assertEqual(row["top_model"], max(tokens_by_model, key=tokens_by_model.get))

    def test_top_model_is_summed_across_requests_not_the_largest_single_request(self):
        user = self.visible_user("ShiNingYuan")
        key = self.api_key(user, "sk-b5NOFPVShiNingYuan")

        # One huge request on a model that sorts LAST alphabetically.
        self.add_usage_log(key, "z-model", 4000, "huge-1")
        # Many small requests on a model that sorts FIRST alphabetically.
        for index in range(10):
            self.add_usage_log(key, "gpt-5.4-mini", 500, f"small-{index}")

        row = self.key_rows(self.dashboard())["ShiNingYuan 队员"]

        # 10 x 500 = 5000 > 4000, so the busiest model wins even though the other
        # model owns the single largest request.
        self.assertEqual(row["top_model"], "gpt-5.4-mini")
        self.assertEqual(row["total_tokens"], 9000)
        self.assertEqual(row["total_requests"], 11)
        largest_single = UsageLog.objects.filter(api_key=key).order_by("-total_tokens").first()
        self.assertEqual(largest_single.model_name, "z-model")
        self.assertNotEqual(row["top_model"], largest_single.model_name)

    def test_top_model_prefers_tokens_over_call_count(self):
        user = self.visible_user("CallCounter")
        key = self.api_key(user, "sk-call-counter-token")

        # Few calls, massive tokens.
        for index in range(3):
            self.add_usage_log(key, "heavy-model", 3000, f"heavy-{index}")
        # Many calls, tiny tokens.
        for index in range(30):
            self.add_usage_log(key, "chatty-model", 5, f"chatty-{index}")

        row = self.key_rows(self.dashboard())["CallCounter 队员"]

        # 3 x 3000 tokens beats 30 x 5 tokens: weighting is by tokens, not calls.
        self.assertEqual(row["top_model"], "heavy-model")
        self.assertEqual(row["total_requests"], 33)
        self.assertEqual(row["total_tokens"], 9150)

    def test_top_model_is_isolated_per_api_key(self):
        # Two keys whose model names are adversarial for each other.
        alpha_user = self.visible_user("AlphaUser")
        alpha_key = self.api_key(alpha_user, "sk-alpha-key-token")
        beta_user = self.visible_user("BetaUser")
        beta_key = self.api_key(beta_user, "sk-beta-key-token")

        self.add_usage_log(alpha_key, "qwen-max", 3000, "alpha-1")
        self.add_usage_log(alpha_key, "zz-lexically-largest", 100, "alpha-2")
        self.add_usage_log(beta_key, "zz-lexically-largest", 3000, "beta-1")
        self.add_usage_log(beta_key, "qwen-max", 100, "beta-2")

        rows = self.key_rows(self.dashboard())

        self.assertEqual(rows["AlphaUser 队员"]["top_model"], "qwen-max")
        self.assertEqual(rows["BetaUser 队员"]["top_model"], "zz-lexically-largest")

    def test_top_model_respects_filters_of_the_passed_queryset(self):
        user = self.visible_user("WindowUser")
        key = self.api_key(user, "sk-window-filter-token")

        # Outside the window.
        self.add_usage_log(key, "fable-model", 9000, "old-1", hours_ago=72)
        # Inside the window.
        self.add_usage_log(key, "gpt-5.4", 4000, "new-1", hours_ago=1)

        base = UsageLog.objects.exclude(user=None).order_by("-request_time")
        recent = base.filter(request_time__gte=timezone.now() - timedelta(days=1))

        self.assertEqual(self.key_rows(self.dashboard(recent))["WindowUser 队员"]["top_model"], "gpt-5.4")
        self.assertEqual(self.key_rows(self.dashboard(base))["WindowUser 队员"]["top_model"], "fable-model")

    def test_top_model_tie_breaks_by_request_count_then_name(self):
        tie_user = self.visible_user("TieUser")
        tie_key = self.api_key(tie_user, "sk-tie-break-token")

        # Equal tokens; "aaa-model" has more requests, so it must win even though
        # "zzz-model" has the larger lexicographic name.
        self.add_usage_log(tie_key, "aaa-model", 100, "tie-1")
        self.add_usage_log(tie_key, "aaa-model", 100, "tie-2")
        self.add_usage_log(tie_key, "zzz-model", 200, "tie-3")

        self.assertEqual(self.key_rows(self.dashboard())["TieUser 队员"]["top_model"], "aaa-model")

        name_user = self.visible_user("NameTieUser")
        name_key = self.api_key(name_user, "sk-name-tie-token")

        # Identical tokens AND identical request counts -> deterministic name order.
        self.add_usage_log(name_key, "aaa-model", 100, "name-1")
        self.add_usage_log(name_key, "zzz-model", 100, "name-2")

        rows = self.key_rows(self.dashboard())
        self.assertEqual(rows["NameTieUser 队员"]["top_model"], "aaa-model")
        self.assertEqual(rows["NameTieUser 队员"]["top_model"], min("aaa-model", "zzz-model"))

    def test_top_model_selection_stays_deterministic_across_calls(self):
        user = self.visible_user("StableUser")
        key = self.api_key(user, "sk-stable-token-xyz")

        self.add_usage_log(key, "model-b", 700, "stable-1")
        self.add_usage_log(key, "model-c", 700, "stable-2")
        self.add_usage_log(key, "model-a", 700, "stable-3")

        first = self.key_rows(self.dashboard())
        second = self.key_rows(self.dashboard())
        third = self.key_rows(self.dashboard(UsageLog.objects.exclude(user=None).order_by("-request_time")))

        # Equal tokens and equal request counts for all three models, so the name
        # ordering decides and repeated calls agree.
        self.assertEqual(first["StableUser 队员"]["top_model"], "model-a")
        self.assertEqual(second["StableUser 队员"]["top_model"], "model-a")
        self.assertEqual(third["StableUser 队员"]["top_model"], "model-a")

    def test_top_model_lookup_keeps_query_count_fixed(self):
        one_user = self.visible_user("SoloUser")
        one_key = self.api_key(one_user, "sk-solo-query-token")
        self.add_usage_log(one_key, "gpt-5.4", 10, "solo-1")

        self.dashboard()
        with self.assertNumQueries(self.DASHBOARD_QUERY_COUNT):
            self.dashboard()

        for index in range(6):
            user = self.visible_user(f"BulkUser{index}")
            key = self.api_key(user, f"sk-bulk-query-{index}")
            self.add_usage_log(key, "gpt-5.4", 10, f"bulk-1-{index}")
            self.add_usage_log(key, "gpt-5.5", 20, f"bulk-2-{index}")

        # A fresh aggregate over seven keys still costs the same fixed set of
        # queries: the top-model lookup is a correlated subquery inside the same
        # key_rank SELECT, not one query per key.
        self.dashboard()
        with self.assertNumQueries(self.DASHBOARD_QUERY_COUNT):
            data = self.dashboard()

        self.assertEqual(len(data["key_rank"]), 7)

    def test_top_model_is_none_for_empty_queryset_and_rows_without_keys(self):
        empty = aggregate_dashboard(UsageLog.objects.none())

        self.assertEqual(empty["key_rank"], [])
        self.assertEqual(empty["total_requests"], 0)
        self.assertEqual(empty["total_tokens"], 0)
        self.assertEqual(empty["total_cost"], 0)

        # A user with logs but no bound key never appears in this panel.
        keyless = self.visible_user("KeylessUser", display_name="KeylessUser 队员")
        UsageLog.objects.create(
            request_id="keyless-1",
            user=keyless,
            api_key=None,
            user_identifier="KeylessUser",
            model_name="gpt-5.4",
            prompt_tokens=10,
            completion_tokens=10,
            total_tokens=20,
            request_time=timezone.now(),
        )

        data = self.dashboard()
        self.assertEqual(data["key_rank"], [])
        self.assertEqual(data["total_requests"], 1)
        self.assertEqual(data["total_tokens"], 20)

    def test_top_model_respects_key_visibility_filter(self):
        visible_user = self.visible_user("VisibleUser")
        visible_key = self.api_key(visible_user, "sk-visible-key-token")
        self.add_usage_log(visible_key, "gpt-5.4", 500, "visible-1")

        hidden_user = self.visible_user("HiddenUser", visible=False)
        hidden_key = self.api_key(hidden_user, "sk-hidden-key-token")
        self.add_usage_log(hidden_key, "zz-hidden-model", 5000, "hidden-1")

        rows = self.key_rows(self.dashboard())

        self.assertIn("VisibleUser 队员", rows)
        self.assertNotIn("HiddenUser 队员", rows)
        self.assertEqual(rows["VisibleUser 队员"]["top_model"], "gpt-5.4")
