import requests
import os
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from api_keys.models import APIKey
from usage.models import UsageImportJob, UsageLog
from usage.services import (
    _extract_keeper_events_page,
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
