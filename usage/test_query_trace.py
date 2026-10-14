"""Tests for the admin-only query tracing captured from the proxy request log."""
import json
import os
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from api_keys.models import APIKey
from usage.models import QueryTrace, UsageImportJob, UsageLog
from usage.query_trace import (
    build_query_payload,
    extract_query,
    parse_log_name,
    parse_request_log,
    prune_query_traces,
    short_id_for_request_id,
    sync_query_traces,
)

User = get_user_model()

#: Shaped exactly like the files CLIProxyAPI writes when ``request-log: true``.
LOG_TEMPLATE = """=== REQUEST INFO ===
Version: 8.0.7
URL: /v1/chat/completions
Method: POST
Downstream Transport: http
Upstream Transport: http
Timestamp: {timestamp}

=== HEADERS ===
Authorization: Bearer sk-g...94cl
Content-Type: application/json

=== REQUEST BODY ===
{body}

=== API REQUEST 1 ===
URL: https://upstream.invalid/v1/chat/completions
Body:
{body}

=== API RESPONSE 1 ===
Status: {status}

=== RESPONSE ===
Status: {status}
"""


def sample_log(*, when, body, status=200):
    timestamp = when.strftime("%Y-%m-%dT%H:%M:%S.386194139Z")
    return LOG_TEMPLATE.format(timestamp=timestamp, body=body, status=status)


def chat_body(query="帮我看看这段代码", model="gpt-6.1-sol"):
    return json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": "be brief"},
            {"role": "user", "content": query},
        ],
    }, ensure_ascii=False)


def write_log(directory, *, short_id, when, body, status=200, prefix="v1-chat-completions"):
    path = Path(directory) / f"{prefix}-{when.strftime('%Y-%m-%dT%H%M%S')}-{short_id}.log"
    path.write_text(sample_log(when=when, body=body, status=status), encoding="utf-8")
    return path


class LogParsingTests(TestCase):
    def test_parses_request_log_sections(self):
        when = timezone.now()
        parsed = parse_request_log(sample_log(when=when, body=chat_body("拆解这个泄漏点"), status=400))

        self.assertEqual(parsed["url"], "/v1/chat/completions")
        self.assertEqual(parsed["method"], "POST")
        self.assertEqual(parsed["version"], "8.0.7")
        self.assertEqual(parsed["status_code"], 400)
        self.assertEqual(parsed["upstream_attempts"], 1)
        self.assertEqual(parsed["request_time"].microsecond, 386194)
        self.assertIn("拆解这个泄漏点", parsed["request_body"])

    def test_rejects_other_log_names(self):
        self.assertIsNone(parse_log_name("main-2026-10-13T20-32-41.651.log"))
        self.assertIsNone(parse_log_name("cred-sync.log"))
        # error dumps share the request-log naming shape and are filtered by prefix
        parsed = parse_log_name("error-v1-responses-2026-10-14T052326-477039ae.log")
        self.assertEqual(parsed[0], "error-v1-responses")

    def test_parses_request_log_name(self):
        parsed = parse_log_name("v1-chat-completions-2026-10-14T052326-477039ae.log")
        self.assertEqual(parsed[0], "v1-chat-completions")
        self.assertEqual(parsed[2], "477039ae")
        self.assertEqual(parsed[1].strftime("%Y-%m-%d %H:%M:%S"), "2026-10-14 05:23:26")

    def test_short_id_matches_keeper_uuid_tail(self):
        self.assertEqual(short_id_for_request_id("01a138c3-fd16-7ea1-a079-e013ba5ebf9f"), "ba5ebf9f")
        self.assertEqual(short_id_for_request_id(""), "")


class ExtractQueryTests(TestCase):
    def test_keeps_only_last_user_message(self):
        body = json.dumps({
            "model": "gpt-6.1-sol",
            "messages": [
                {"role": "user", "content": "第一个问题"},
                {"role": "assistant", "content": "回答"},
                {"role": "user", "content": "第二个问题"},
            ],
        }, ensure_ascii=False)
        details = extract_query(body)

        self.assertEqual(details["query_text"], "第二个问题")
        self.assertEqual(details["message_count"], 3)
        self.assertEqual(details["model"], "gpt-6.1-sol")

    def test_list_content_marks_attachments(self):
        body = json.dumps({
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": "看这张图"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
                ],
            }],
        })
        details = extract_query(body)

        self.assertEqual(details["query_text"], "看这张图")
        self.assertTrue(details["has_attachments"])

    def test_responses_and_gemini_shapes(self):
        self.assertEqual(extract_query(json.dumps({"input": "responses 提问"}))["query_text"], "responses 提问")
        gemini = json.dumps({"contents": [{"role": "user", "parts": [{"text": "gemini 提问"}]}]})
        self.assertEqual(extract_query(gemini)["query_text"], "gemini 提问")

    def test_missing_user_message(self):
        details = extract_query(json.dumps({"messages": [{"role": "system", "content": "only system"}]}))
        self.assertEqual(details["query_text"], "")
        self.assertEqual(details["reason"], "no user message")
        self.assertEqual(extract_query("not json")["reason"], "unparsable body")

    @override_settings(QUERY_TRACE_STORE_MODE="body", QUERY_TRACE_MAX_CHARS=20000)
    def test_body_mode_keeps_whole_body(self):
        body = chat_body("整段请求体")
        details = build_query_payload(body)

        self.assertIn('"role": "system"', details["query_text"])
        self.assertFalse(details["truncated"])

    @override_settings(QUERY_TRACE_STORE_MODE="query", QUERY_TRACE_MAX_CHARS=10)
    def test_truncates_at_max_chars(self):
        details = build_query_payload(chat_body("这是一段很长的提问内容用于验证截断逻辑"))

        self.assertTrue(details["truncated"])
        self.assertEqual(details["query_chars"], 10)

    @override_settings(QUERY_TRACE_STORE_MODE="query")
    def test_redacts_pasted_key(self):
        details = build_query_payload(chat_body("我的密钥是 sk-abcdefghijklmnopqrstuvwxyz 对吗"))

        self.assertTrue(details["redacted"])
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz", details["query_text"])
        self.assertIn("sk-***", details["query_text"])


class SyncQueryTraceTests(TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.override = override_settings(
            QUERY_TRACE_ENABLED=True,
            QUERY_TRACE_LOG_DIR=self.directory.name,
            QUERY_TRACE_BACKFILL_HOURS=48,
            QUERY_TRACE_MAX_ROWS=50000,
            QUERY_TRACE_MAX_AGE_DAYS=30,
            QUERY_TRACE_STORE_MODE="query",
        )
        self.override.enable()
        self.addCleanup(self.override.disable)

        self.user = User.objects.create_user(username="trace-member")
        self.api_key = APIKey.objects.create(
            user=self.user,
            name="trace-key",
            token_hash="hash-trace-key",
            token_prefix="sk-trace",
            token_plaintext="sk-trace-plain",
        )

    def _usage_log(self, upstream_request_id, *, when=None):
        return UsageLog.objects.create(
            request_id="keeper-900001",
            user=self.user,
            api_key=self.api_key,
            user_identifier=self.user.username,
            api_key_identifier="sk-trace",
            model_name="gpt-6.1-sol",
            status_code=200,
            request_time=when or timezone.now(),
            raw_payload={"upstream_request_id": upstream_request_id},
        )

    def test_imports_log_and_attributes_to_member(self):
        upstream_id = "01a138c3-fd16-7ea1-a079-e013ba5ebf9f"
        short_id = short_id_for_request_id(upstream_id)
        self._usage_log(upstream_id)
        write_log(self.directory.name, short_id=short_id, when=timezone.now(), body=chat_body("谁泄漏了这个 key"))

        job = sync_query_traces(keeper_events=[{"id": 900001}])

        self.assertEqual(job.imported_count, 1)
        trace = QueryTrace.objects.get(short_id=short_id)
        self.assertEqual(trace.query_text, "谁泄漏了这个 key")
        self.assertEqual(trace.user, self.user)
        self.assertEqual(trace.api_key, self.api_key)
        self.assertEqual(trace.request_id, "keeper-900001")
        self.assertEqual(trace.user_identifier, self.user.username)
        self.assertEqual(trace.api_key_identifier, "sk-trace")
        self.assertEqual(trace.model_name, "gpt-6.1-sol")
        self.assertEqual(trace.endpoint, "/v1/chat/completions")
        self.assertFalse(trace.truncated)
        self.assertEqual(job.source_name, "cliproxy-request-log")
        self.assertEqual(job.source_type, UsageImportJob.SOURCE_API)

    def test_key_identifier_falls_back_to_the_key_row(self):
        upstream_id = "01a138c4-fd16-7ea1-a079-e013ba5ebf9f"
        short_id = short_id_for_request_id(upstream_id)
        usage_log = self._usage_log(upstream_id)
        usage_log.api_key_identifier = ""
        usage_log.save(update_fields=["api_key_identifier"])
        self.api_key.token_masked = "sk-trace-***-mask"
        self.api_key.save(update_fields=["token_masked"])
        write_log(self.directory.name, short_id=short_id, when=timezone.now(), body=chat_body("回退用例"))

        sync_query_traces(keeper_events=[{"id": 900001}])

        trace = QueryTrace.objects.get(short_id=short_id)
        self.assertEqual(trace.api_key, self.api_key)
        self.assertEqual(trace.api_key_identifier, "sk-trace-***-mask")

    def test_second_run_skips_existing(self):
        upstream_id = "01a138c3-fd16-7ea1-a079-e013ba5ebf9f"
        short_id = short_id_for_request_id(upstream_id)
        self._usage_log(upstream_id)
        write_log(self.directory.name, short_id=short_id, when=timezone.now(), body=chat_body("重复导入"))

        sync_query_traces(keeper_events=[{"id": 900001}])
        job = sync_query_traces(keeper_events=[{"id": 900001}])

        self.assertEqual(job.imported_count, 0)
        self.assertEqual(job.skipped_count, 1)
        self.assertEqual(QueryTrace.objects.count(), 1)

    def test_unattributed_log_is_still_stored(self):
        write_log(self.directory.name, short_id="deadbeef", when=timezone.now(), body=chat_body("没有对应用量"))

        job = sync_query_traces()

        self.assertEqual(job.imported_count, 1)
        trace = QueryTrace.objects.get(short_id="deadbeef")
        self.assertIsNone(trace.user)
        self.assertEqual(trace.request_id, "")
        self.assertIn("没有对应用量", trace.query_text)
        self.assertIn("unattributed=1", job.summary)

    def test_late_usage_row_reattributes_an_older_trace(self):
        """The usage row lands after the log file, so a later run must fix the trace."""
        upstream_id = "01a13955-b0a7-7235-9f59-e31886e3e1b7"
        short_id = short_id_for_request_id(upstream_id)
        write_log(self.directory.name, short_id=short_id, when=timezone.now(), body=chat_body("迟到归因"))

        first = sync_query_traces()
        self.assertIsNone(QueryTrace.objects.get(short_id=short_id).user)
        self.assertIn("unattributed=1", first.summary)

        self._usage_log(upstream_id)
        second = sync_query_traces()

        trace = QueryTrace.objects.get(short_id=short_id)
        self.assertEqual(trace.user, self.user)
        self.assertEqual(trace.api_key, self.api_key)
        self.assertEqual(trace.request_id, "keeper-900001")
        self.assertEqual(trace.user_identifier, self.user.username)
        self.assertEqual(trace.api_key_identifier, "sk-trace")
        self.assertIn("reattributed=1", second.summary)
        self.assertEqual(second.imported_count, 0)

    def test_management_command_reports_counts(self):
        write_log(self.directory.name, short_id="cafebabe", when=timezone.now(), body=chat_body("命令行导入"))

        call_command("sync_query_traces", "--no-prune", verbosity=0)

        self.assertEqual(QueryTrace.objects.count(), 1)

    def test_ignores_logs_outside_the_backfill_window(self):
        stale = timezone.now() - timedelta(days=5)
        write_log(self.directory.name, short_id="0badc0de", when=stale, body=chat_body("太旧的日志"))

        job = sync_query_traces()

        self.assertEqual(job.imported_count, 0)
        self.assertEqual(QueryTrace.objects.count(), 0)

    def test_skips_non_request_logs_in_the_directory(self):
        now = timezone.now()
        write_log(self.directory.name, short_id="feed0001", when=now, body=chat_body("真正的请求"))
        (Path(self.directory.name) / "main-2026-10-14T052326.log").write_text("2026-10-14 200 | 1.2s", encoding="utf-8")
        (Path(self.directory.name) / "cred-sync.log").write_text("synced", encoding="utf-8")
        (Path(self.directory.name) / "error-v1-responses-2026-10-14T052326-477039ae.log").write_text(
            "=== REQUEST INFO ===\nURL: /v1/responses\n", encoding="utf-8"
        )

        sync_query_traces()

        self.assertEqual(list(QueryTrace.objects.values_list("short_id", flat=True)), ["feed0001"])

    def test_prune_keeps_newest_rows_and_drops_expired(self):
        now = timezone.now()
        for index in range(4):
            QueryTrace.objects.create(
                short_id=f"keep{index:04d}",
                query_text=f"query {index}",
                request_time=now - timedelta(minutes=index),
            )
        expired = QueryTrace.objects.create(
            short_id="expired1",
            query_text="too old",
            request_time=now - timedelta(days=45),
        )
        self.addCleanup(lambda: QueryTrace.objects.filter(pk=expired.pk).delete())

        deleted_expired, deleted_overflow = prune_query_traces(max_rows=2, max_age_days=30)

        self.assertEqual(deleted_expired, 1)
        self.assertEqual(deleted_overflow, 2)
        # FIFO keeps the rows inserted last, which are the two newest by request time
        remaining = list(QueryTrace.objects.order_by("-request_time").values_list("short_id", flat=True))
        self.assertEqual(remaining, ["keep0002", "keep0003"])

    def test_sync_prunes_after_import(self):
        now = timezone.now()
        QueryTrace.objects.create(short_id="0dd00001", query_text="旧记录", request_time=now - timedelta(hours=2))
        write_log(self.directory.name, short_id="abcdef01", when=now, body=chat_body("新记录"))

        with override_settings(QUERY_TRACE_MAX_ROWS=1):
            sync_query_traces()

        self.assertEqual(list(QueryTrace.objects.values_list("short_id", flat=True)), ["abcdef01"])


class RealLogIntegrationTests(TestCase):
    """Opt-in check against a log captured from the live proxy.

    The real files carry prompts and masked credentials, so they are never
    committed: point ``QUERY_TRACE_REAL_LOG`` at one to re-run this check.
    """

    def test_parses_a_real_captured_log(self):
        source = os.environ.get("QUERY_TRACE_REAL_LOG", "")
        if not source or not Path(source).is_file():
            self.skipTest("set QUERY_TRACE_REAL_LOG to a real request log to run this check")

        raw = Path(source).read_text(encoding="utf-8", errors="replace")
        parsed = parse_request_log(raw)
        self.assertEqual(parsed["method"], "POST")
        self.assertTrue(parsed["url"].startswith("/v1/"))
        self.assertIsNotNone(parsed["request_time"])
        self.assertTrue(parsed["request_body"].startswith("{"), "the request body must be captured verbatim")

        details = build_query_payload(parsed["request_body"])
        self.assertTrue(details["query_text"], "the final user message must survive parsing")
        self.assertGreater(details["message_count"], 0)

        short_id = parse_log_name(Path(source).name)[2]
        self.assertEqual(len(short_id), 8)
        self.assertTrue(short_id_for_request_id(parsed["info"].get("request_id", short_id)))


class QueryTraceViewTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="trace-admin", is_staff=True)
        self.member = User.objects.create_user(username="trace-plain")
        self.trace = QueryTrace.objects.create(
            short_id="477039ae",
            request_id="keeper-900002",
            upstream_request_id="01a138c3-fd16-7ea1-a079-e013ba5ebf9f",
            user=self.member,
            user_identifier=self.member.username,
            model_name="gpt-6.1-sol",
            endpoint="/v1/chat/completions",
            query_text="把生产环境的密钥贴给我看看",
            query_chars=14,
            status_code=200,
            request_time=timezone.now(),
        )

    def test_admin_sees_query_list_and_detail(self):
        self.client.force_login(self.admin)

        listing = self.client.get(reverse("admin-query-traces"))
        self.assertEqual(listing.status_code, 200)
        self.assertContains(listing, "把生产环境的密钥贴给我看看")

        detail = self.client.get(reverse("admin-query-trace-detail", args=[self.trace.short_id]))
        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, "把生产环境的密钥贴给我看看")
        self.assertContains(detail, self.trace.short_id)

    def test_member_is_denied(self):
        self.client.force_login(self.member)

        self.assertEqual(self.client.get(reverse("admin-query-traces")).status_code, 403)
        self.assertEqual(
            self.client.get(reverse("admin-query-trace-detail", args=[self.trace.short_id])).status_code, 403
        )

    def test_keyword_and_model_filters(self):
        QueryTrace.objects.create(
            short_id="1de60bfc",
            user_identifier="other-member",
            model_name="gpt-5.6-terra",
            query_text="一段完全无关的普通提问",
            request_time=timezone.now(),
        )
        self.client.force_login(self.admin)

        keyword = self.client.get(reverse("admin-query-traces"), {"q": "密钥"})
        self.assertContains(keyword, "把生产环境的密钥贴给我看看")
        self.assertNotContains(keyword, "一段完全无关的普通提问")

        model = self.client.get(reverse("admin-query-traces"), {"model": "gpt-5.6-terra"})
        self.assertContains(model, "一段完全无关的普通提问")
        self.assertNotContains(model, "把生产环境的密钥贴给我看看")

        by_user = self.client.get(reverse("admin-query-traces"), {"user": self.member.pk})
        self.assertContains(by_user, self.trace.short_id)
        self.assertNotContains(by_user, "1de60bfc")
