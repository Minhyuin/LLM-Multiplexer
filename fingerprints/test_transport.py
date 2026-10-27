"""Protocol acceptance parity and bounded transport. All HTTP uses in-memory mocks."""
import asyncio
import json
import time
from unittest.mock import patch

import httpx
import lmfpd
from django.test import SimpleTestCase, override_settings

from . import transport
from .config import FingerprintError, endpoint_base, model_policy

TOKEN = "labsk-offline-only-secret-value"
CHALLENGE = {"id": "offline", "expected_count": 100, "prompt": "Fixed offline challenge"}
TEXT = ", ".join(str(i % 355 + 1) for i in range(90))


def config(api):
    return {"base_url": "http://127.0.0.1:8317/v1", "api_key": TOKEN, "model": "test-model",
            "api": api, "stream": True, "effort": "", "service_tier": "default", "count": 3}


def policy(api):
    with override_settings(FINGERPRINT_MODEL_POLICIES={"test-model": {"api": api}}):
        return model_policy("test-model")


def document(api, text=TEXT, finish=None):
    common = {"id": "resp-fixture", "model": "declared-model"}
    if api == "chat_completions":
        return {**common, "choices": [{"index": 0, "message": {"content": text}, "finish_reason": finish or "stop"}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 30, "prompt_tokens_details": {"cached_tokens": 4}}}
    if api == "responses":
        return {**common, "status": finish or "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
                "usage": {"input_tokens": 20, "output_tokens": 30, "output_tokens_details": {"reasoning_tokens": 2}}}
    return {**common, "content": [{"type": "text", "text": text}], "stop_reason": finish or "end_turn",
            "usage": {"input_tokens": 20, "output_tokens": 30, "cache_read_input_tokens": 4, "cache_creation_input_tokens": 3}}


def events(api, text=TEXT):
    if api == "chat_completions":
        return [{"id": "resp-fixture", "model": "declared-model", "choices": [{"index": 0, "delta": {"content": text}}]},
                {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
                {"choices": [], "usage": {"prompt_tokens": 20, "completion_tokens": 30}}, "[DONE]"]
    if api == "responses":
        return [{"type": "response.output_text.delta", "delta": text},
                {"type": "response.completed", "response": document(api, text)}]
    return [{"type": "message_start", "message": {"id": "resp-fixture", "model": "declared-model", "usage": {"input_tokens": 20, "output_tokens": 0}}},
            {"type": "content_block_delta", "delta": {"type": "text_delta", "text": text}},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 30}},
            {"type": "message_stop"}]


def stream_bytes(api, text=TEXT, separator="\n"):
    return (separator * 2).join("data: " + (value if isinstance(value, str) else json.dumps(value))
                                for value in events(api, text)).encode() + (separator * 2).encode()


class AsyncChunks(httpx.AsyncByteStream):
    def __init__(self, chunks, stall=False):
        self.chunks, self.stall, self.closed = chunks, stall, False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
        if self.stall:
            await asyncio.Event().wait()

    async def aclose(self):
        self.closed = True


@override_settings(FINGERPRINT_BASE_URL="http://127.0.0.1:8317")
class TransportTests(SimpleTestCase):
    def run_reply(self, api, body=None, *, data=None, content_type="application/json", status=200, overrides=None, cancelled=lambda: False, stream=None):
        seen = []
        def handler(request):
            seen.append(request)
            if stream is not None:
                return httpx.Response(status, stream=stream, headers={"content-type": content_type})
            return httpx.Response(status, content=body if body is not None else json.dumps(data or document(api)).encode(),
                                  headers={"content-type": content_type})
        result = transport.sample(TOKEN, "test-model", {**policy(api), **(overrides or {})}, CHALLENGE,
                                  cancelled, transport=httpx.MockTransport(handler))
        return result, seen

    def test_json_protocols_match_public_upstream_sample_validation(self):
        for api in ("chat_completions", "responses", "messages"):
            for text, finish in ((TEXT, None), ("1, 2, 3", None), (TEXT, "length"), (TEXT, "refusal"),
                                 (", ".join(map(str, range(1, 121))), None)):
                with self.subTest(api=api, finish=finish, length=len(text)):
                    data = document(api, text, finish)
                    result, seen = self.run_reply(api, data=data)
                    real_client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=data)))
                    with patch("lmfpd._sampling.client", return_value=real_client):
                        reference = lmfpd.request_sample(config(api), CHALLENGE)
                    self.assertEqual(result["valid"], reference["error"] is None)
                    if result["valid"]:
                        self.assertEqual(result["text"], reference["text"])
                        self.assertEqual(result["number_count"], reference["count"])
                        self.assertEqual(result["status"], reference["state"])
                    self.assertEqual(len(seen), 1)

    def test_stream_protocols_and_line_breaks_match_upstream(self):
        for api in ("chat_completions", "responses", "messages"):
            for separator in ("\n", "\r\n", "\r"):
                with self.subTest(api=api, separator=repr(separator)):
                    body = stream_bytes(api, separator=separator)
                    # Split across bytes to exercise delimiter/chunk boundaries.
                    source = AsyncChunks([body[i:i + 7] for i in range(0, len(body), 7)])
                    result, _ = self.run_reply(api, stream=source, content_type="text/event-stream")
                    reference_client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})))
                    with patch("lmfpd._sampling.client", return_value=reference_client):
                        reference = lmfpd.request_sample(config(api), CHALLENGE)
                    self.assertTrue(result["valid"], result)
                    self.assertEqual(result["text"], reference["text"])
                    self.assertEqual(result["number_count"], 90)
                    self.assertEqual(result["usage"]["output_tokens"], 30)
                    self.assertTrue(result["usage"]["complete"])
                    self.assertTrue(source.closed)

    def test_sse_multiline_json_and_comment(self):
        data = document("chat_completions")
        event = {"choices": [{"index": 0, "delta": {"content": TEXT}, "finish_reason": "stop"}]}
        pretty = json.dumps(event, indent=2)
        body = (": keepalive\n\n" + "\n".join("data: " + line for line in pretty.splitlines()) + "\n\ndata: [DONE]\n\n").encode()
        result, _ = self.run_reply("chat_completions", body=body, content_type="text/event-stream")
        self.assertTrue(result["valid"], result)
        self.assertEqual(result["usage"], {})

    def test_cap_closes_stream_without_waiting_for_usage(self):
        body = stream_bytes("chat_completions", ", ".join(map(str, range(1, 110))) + ", ")
        source = AsyncChunks([body], stall=True)
        result, _ = self.run_reply("chat_completions", stream=source, content_type="text/event-stream", overrides={"request_timeout": .1})
        self.assertEqual(result["status"], "truncated")
        self.assertTrue(result["valid"])
        self.assertEqual(result["number_count"], 100)
        self.assertFalse(result["usage"].get("complete", False))
        self.assertTrue(source.closed)

    def test_unfinished_number_is_not_counted_before_complete_token(self):
        reply = transport.Reply("chat_completions", 100, TOKEN)
        reply.append(", ".join(map(str, range(1, 100))) + ", 3")
        self.assertFalse(reply.capped)
        reply.append("55, ")
        self.assertTrue(reply.capped)
        self.assertEqual(lmfpd.parse_numbers(reply.text)[-1], 355)

    def test_missing_terminal_and_length_limit_are_not_scored(self):
        for events_list in ([{"choices": [{"delta": {"content": TEXT}}]}],
                            [{"choices": [{"delta": {"content": TEXT}, "finish_reason": "length"}]}, "[DONE]"]):
            body = b"".join(("data: " + (v if isinstance(v, str) else json.dumps(v)) + "\n\n").encode() for v in events_list)
            result, _ = self.run_reply("chat_completions", body=body, content_type="text/event-stream")
            self.assertFalse(result["valid"])
            self.assertEqual(result["error_code"], "incomplete")
            self.assertEqual(result["text"], "")

    def test_timeout_after_first_byte_closes_connection(self):
        source = AsyncChunks([b": first byte\n\n"], stall=True)
        started = time.monotonic()
        result, _ = self.run_reply("chat_completions", stream=source, content_type="text/event-stream", overrides={"request_timeout": .05})
        self.assertEqual(result["error_code"], "request_timeout")
        self.assertLess(time.monotonic() - started, 2)
        self.assertTrue(source.closed)

    def test_cancel_closes_stream_and_does_not_retry(self):
        source = AsyncChunks([b": first byte\n\n"], stall=True)
        probes = iter([False, True, True])
        result, seen = self.run_reply("chat_completions", stream=source, content_type="text/event-stream", cancelled=lambda: next(probes, True))
        self.assertEqual(result["error_code"], "cancelled")
        self.assertEqual(len(seen), 1)
        self.assertTrue(source.closed)

    def test_cancelled_before_send_makes_no_http_request(self):
        result, seen = self.run_reply("chat_completions", cancelled=lambda: True)
        self.assertEqual(result["error_code"], "cancelled")
        self.assertEqual(seen, [])

    def test_size_limit_and_disconnect_fail_without_retry(self):
        result, seen = self.run_reply("chat_completions", body=b"x" * 129, overrides={"response_max_bytes": 128})
        self.assertEqual(result["error_code"], "response_too_large")
        self.assertEqual(len(seen), 1)
        calls = []
        def disconnect(request):
            calls.append(request)
            raise httpx.ReadError("secret " + TOKEN)
        result = transport.sample(TOKEN, "test-model", policy("chat_completions"), CHALLENGE, transport=httpx.MockTransport(disconnect))
        self.assertEqual(result["error_code"], "connection_error")
        self.assertEqual(len(calls), 1)
        self.assertNotIn(TOKEN, json.dumps(result))

    def test_upstream_errors_and_metadata_never_expose_secret(self):
        for status in (302, 401, 429, 500):
            result, seen = self.run_reply("chat_completions", data={"error": {"message": TOKEN}}, status=status)
            self.assertEqual(result["error_code"], "http_error")
            self.assertEqual(len(seen), 1)
            self.assertNotIn(TOKEN, json.dumps(result))
        result, _ = self.run_reply("chat_completions", data={"error": {"message": TOKEN}})
        self.assertEqual(result["error_code"], "upstream_error")
        data = {**document("chat_completions"), "model": TOKEN + "\nmodel", "id": "sk-anotherfakekey123"}
        result, _ = self.run_reply("chat_completions", data=data)
        self.assertTrue(result["valid"])
        self.assertNotIn(TOKEN, json.dumps(result))
        self.assertEqual(result["response_model"], "[REDACTED] model")
        self.assertEqual(result["response_id"], "[REDACTED]")

    def test_only_owner_key_fixed_parameters_and_local_url_reach_http(self):
        for api, suffix, field in (("chat_completions", "/chat/completions", "max_completion_tokens"), ("responses", "/responses", "max_output_tokens"), ("messages", "/messages", "max_tokens")):
            result, seen = self.run_reply(api)
            request = seen[0]
            self.assertEqual(str(request.url), "http://127.0.0.1:8317/v1" + suffix)
            self.assertEqual(request.headers.get("x-api-key") if api == "messages" else request.headers["authorization"], TOKEN if api == "messages" else "Bearer " + TOKEN)
            body = json.loads(request.content)
            self.assertEqual(body[field], 8192)
            self.assertTrue(body["stream"])
            self.assertEqual(body.get("service_tier", "default"), "default")
            self.assertNotIn("reasoning_effort", body)
            self.assertEqual(body["model"], "test-model")

    def test_missing_usage_is_unknown_and_bad_token_counts_are_ignored(self):
        data = document("chat_completions")
        data.pop("usage")
        result, _ = self.run_reply("chat_completions", data=data)
        self.assertEqual(result["usage"], {})
        self.assertEqual(transport.extract_usage({"usage": {"prompt_tokens": True, "completion_tokens": -1}}, "chat_completions"), {"complete": False})

    def test_remote_endpoint_userinfo_query_and_wrong_protocol_are_rejected(self):
        for url in ("http://example.com", "http://127.0.0.1.evil.test", "http://user:pass@127.0.0.1", "http://127.0.0.1?x=y", "http://127.0.0.1/v0/management", "file:///tmp/test"):
            with self.subTest(url=url), override_settings(FINGERPRINT_BASE_URL=url), self.assertRaises(FingerprintError):
                endpoint_base()
        with override_settings(FINGERPRINT_MODEL_POLICIES={"test-model": {"api": "other"}}), self.assertRaises(FingerprintError):
            model_policy("test-model")

    def test_invalid_json_and_sse_are_failures(self):
        for body, content_type in ((b"not-json", "application/json"), (b"data: bad-json\n\n", "text/event-stream")):
            result, _ = self.run_reply("chat_completions", body=body, content_type=content_type)
            self.assertEqual(result["error_code"], "invalid_response")
            self.assertFalse(result["valid"])
