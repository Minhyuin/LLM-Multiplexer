"""Bounded HTTP transport; no retries, redirects, external proxies or key fallbacks."""
import asyncio
import json
import math
import re
import time

import httpx

from . import sdk
from .config import FingerprintError, endpoint_base

FINISHED = {"stop", "end_turn", "completed"}
REFUSED = {"refusal", "content_filter"}
DELIMITER = re.compile(rb"\r\n\r\n|\n\n|\r\r")
TRAILING_DIGITS = re.compile(r"[0-9]+$")


def safe_text(value, secret="", maximum=255):
    text = str(value or "")
    if secret:
        text = text.replace(secret, "[REDACTED]")
    text = re.sub(r"(?i)(?:sk-|labsk-)[a-z0-9_\-]{6,}", "[REDACTED]", text)
    return re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", text)[:maximum]


def record(value):
    return value if isinstance(value, dict) else {}


def items(value):
    return [record(item) for item in value] if isinstance(value, list) else []


def integer(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def response_text(data):
    return "".join(str(part.get("text") or "") for item in items(data.get("output")) if item.get("type") == "message"
                   for part in items(item.get("content")) if part.get("type") == "output_text")


def extract_usage(data, api, previous=None):
    out = dict(previous or {})
    raw = record(data.get("usage"))
    if not raw:
        return out
    if api == "chat_completions":
        mapping = {"input_tokens": raw.get("prompt_tokens"), "output_tokens": raw.get("completion_tokens"),
                   "cached_tokens": record(raw.get("prompt_tokens_details")).get("cached_tokens"),
                   "reasoning_tokens": record(raw.get("completion_tokens_details")).get("reasoning_tokens")}
    else:
        mapping = {"input_tokens": raw.get("input_tokens"), "output_tokens": raw.get("output_tokens"),
                   "cached_tokens": raw.get("cache_read_input_tokens") if api == "messages" else record(raw.get("input_tokens_details")).get("cached_tokens"),
                   "reasoning_tokens": record(raw.get("output_tokens_details")).get("reasoning_tokens")}
        if api == "messages" and integer(raw.get("cache_creation_input_tokens")) is not None:
            out["cache_creation_input_tokens"] = raw["cache_creation_input_tokens"]
    for name, value in mapping.items():
        if integer(value) is not None:
            out[name] = value
    out["complete"] = integer(out.get("input_tokens")) is not None and integer(out.get("output_tokens")) is not None
    return out


class Reply:
    """Upstream non-strict sample semantics, with portal bounds and usage added."""
    def __init__(self, api, expected, secret):
        self.api, self.expected, self.secret = api, expected, secret
        self.text = self.finish = self.response_model = self.response_id = ""
        self.terminal = self.capped = False
        self.usage = {}

    def metadata(self, data):
        if isinstance(data.get("model"), str):
            self.response_model = safe_text(data["model"], self.secret)
        if isinstance(data.get("id"), str):
            self.response_id = safe_text(data["id"], self.secret)
        self.usage = extract_usage(data, self.api, self.usage)

    def accept(self, text, final=False):
        self.text = text
        numbers = sdk.parse_numbers(text if final else TRAILING_DIGITS.sub("", text))
        if len(numbers) >= self.expected:
            self.capped = not final or len(numbers) > self.expected
            self.text = ", ".join(map(str, numbers[:self.expected]))

    def append(self, value):
        if isinstance(value, str):
            self.accept(self.text + value)

    def check_error(self, data):
        if data.get("error") or data.get("type") == "error":
            # Do not expose arbitrary upstream bodies (they can echo credentials).
            raise FingerprintError("upstream_error", "上游返回错误，未自动重试。")

    def event(self, frame):
        payload = "\n".join(line[5:].lstrip(" ") for line in frame.splitlines() if line.startswith("data:"))
        if not payload:
            return
        if payload.strip() == "[DONE]":
            self.terminal = True
            return
        try:
            data = record(json.loads(payload))
        except ValueError:
            raise FingerprintError("invalid_response", "上游流式响应格式无效。") from None
        self.check_error(data)
        self.metadata(data)
        kind = data.get("type")
        if self.api == "chat_completions":
            choices = items(data.get("choices"))
            choice = next((c for c in choices if c.get("index") == 0), choices[0] if choices else {})
            delta = record(choice.get("delta"))
            if delta.get("refusal"):
                raise FingerprintError("refusal", "模型拒绝了挑战。")
            if choice.get("finish_reason"):
                self.finish = safe_text(choice["finish_reason"], self.secret, 80)
            self.append(delta.get("content"))
        elif self.api == "messages":
            if kind == "message_start":
                self.metadata(record(data.get("message")))
            block, delta = record(data.get("content_block")), record(data.get("delta"))
            if kind == "content_block_start" and block.get("type") == "text":
                self.append(block.get("text"))
            if kind == "content_block_delta" and delta.get("type") == "text_delta":
                self.append(delta.get("text"))
            if kind == "message_delta":
                self.finish = safe_text(delta.get("stop_reason") or self.finish, self.secret, 80)
            if kind == "message_stop":
                self.terminal = True
        else:
            if kind in ("response.created", "response.in_progress"):
                self.metadata(record(data.get("response")))
            if kind == "response.output_text.delta":
                self.append(data.get("delta"))
            if kind == "response.output_text.done" and not self.text:
                self.accept(str(data.get("text") or ""), True)
            if kind == "response.output_item.done" and not self.text:
                self.accept(response_text({"output": [data.get("item")]}), True)
            if kind == "response.refusal.delta":
                raise FingerprintError("refusal", "模型拒绝了挑战。")
            if kind == "response.completed":
                response = record(data.get("response"))
                self.metadata(response)
                self.terminal = True
                self.finish = safe_text(response.get("status") or "completed", self.secret, 80)
                full = response_text(response)
                if full and not self.capped:
                    self.accept(full, True)
            if kind in ("response.failed", "response.incomplete"):
                raise FingerprintError("incomplete", "上游响应未完成，可能达到输出上限。")
        if self.finish in REFUSED:
            raise FingerprintError("refusal", "模型拒绝了挑战。")

    def json(self, data):
        data = record(data)
        self.check_error(data)
        self.metadata(data)
        if self.api == "responses":
            text = response_text(data)
            self.finish = safe_text(data.get("status"), self.secret, 80)
            self.terminal = self.finish == "completed"
        elif self.api == "messages":
            text = "".join(str(p.get("text") or "") for p in items(data.get("content")) if p.get("type") == "text")
            self.finish = safe_text(data.get("stop_reason"), self.secret, 80)
            self.terminal = True
        else:
            choices = items(data.get("choices"))
            choice = choices[0] if choices else {}
            message = record(choice.get("message"))
            if message.get("refusal"):
                raise FingerprintError("refusal", "模型拒绝了挑战。")
            text = str(message.get("content") or "")
            self.finish = safe_text(choice.get("finish_reason"), self.secret, 80)
            self.terminal = True
        if self.finish in REFUSED:
            raise FingerprintError("refusal", "模型拒绝了挑战。")
        # Like the SDK, JSON completion is validated before truncating numbers.
        if not self.terminal or self.finish not in FINISHED:
            raise FingerprintError("incomplete", "上游响应缺少正常结束状态，或达到输出上限。")
        self.accept(text, True)

    def validate(self):
        if not self.capped and (not self.terminal or self.finish not in FINISHED):
            raise FingerprintError("incomplete", "上游响应缺少正常结束状态，或达到输出上限。")
        if not self.capped:
            self.accept(self.text, True)
        if len(sdk.parse_numbers(self.text)) < max(80, math.ceil(self.expected * 0.55)):
            raise FingerprintError("insufficient_numbers", "有效整数数量不足，无法使用这条样本。")


async def _request(request, reply, maximum, transport=None):
    async with httpx.AsyncClient(transport=transport, follow_redirects=False, trust_env=False, timeout=None) as client:
        async with client.stream("POST", request["url"], headers=request["headers"], json=request["body"]) as response:
            if not 200 <= response.status_code < 300:
                raise FingerprintError("http_error", f"上游返回 HTTP {response.status_code}，未重试或切换协议。")
            streaming = "text/event-stream" in response.headers.get("content-type", "").lower()
            buffer = bytearray()
            total = 0
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > maximum:
                    raise FingerprintError("response_too_large", "响应超过读取上限，连接已关闭。")
                buffer.extend(chunk)
                if streaming:
                    while not (reply.terminal or reply.capped):
                        boundary = DELIMITER.search(buffer)
                        if boundary is None:
                            break
                        reply.event(buffer[:boundary.start()].decode("utf-8", "replace"))
                        del buffer[:boundary.end()]
                    if reply.terminal or reply.capped:
                        break
            if streaming:
                if not (reply.terminal or reply.capped) and buffer.strip():
                    reply.event(buffer.decode("utf-8", "replace"))
            else:
                try:
                    reply.json(json.loads(buffer))
                except (ValueError, UnicodeError):
                    raise FingerprintError("invalid_response", "上游响应不是有效 JSON。") from None
            reply.validate()


async def _bounded(request, reply, policy, cancelled, transport):
    # The run may have been cancelled after its dispatch marker was committed.
    # Check before scheduling HTTP, otherwise a ready socket can win the race
    # against the watcher and send a request that was already cancelled.
    if await asyncio.to_thread(cancelled):
        raise FingerprintError("cancelled", "检测已取消；已发出的请求仍可能计费。")

    async def watch():
        while True:
            if await asyncio.to_thread(cancelled):
                return
            await asyncio.sleep(0.2)

    network = asyncio.create_task(_request(request, reply, policy["response_max_bytes"], transport))
    watcher = asyncio.create_task(watch())
    try:
        done, _ = await asyncio.wait((network, watcher), timeout=policy["request_timeout"], return_when=asyncio.FIRST_COMPLETED)
        if watcher in done:
            watcher.result()  # Fail closed if cancellation state cannot be read.
            raise FingerprintError("cancelled", "检测已取消；已发出的请求仍可能计费。")
        if network not in done:
            raise FingerprintError("request_timeout", "单条挑战超过总时限，连接已关闭。")
        await network
    finally:
        for task in (network, watcher):
            if not task.done():
                task.cancel()
        await asyncio.gather(network, watcher, return_exceptions=True)


def sample(token, model_id, policy, challenge, cancelled=lambda: False, *, transport=None):
    started = time.monotonic()
    reply = Reply(policy["api"], challenge["expected_count"], token)
    error_code = error_message = ""
    valid = False
    try:
        config = {"base_url": endpoint_base(), "api_key": token, "model": model_id, "api": policy["api"],
                  "effort": policy["effort"], "service_tier": "default", "stream": True, "count": 3}
        request = sdk.build_request(config, challenge["prompt"])
        request["body"][policy["output_field"]] = policy["max_output_tokens"]
        asyncio.run(_bounded(request, reply, policy, cancelled, transport))
        valid = True
    except FingerprintError as exc:
        error_code, error_message = exc.code, exc.message
    except (httpx.HTTPError, OSError):
        error_code, error_message = "connection_error", "上游连接中断，未自动重试。"
    except Exception:
        error_code, error_message = "transport_error", "无法完成检测请求，未自动重试。"
    return {"status": ("truncated" if reply.capped else "complete") if valid else "failed", "valid": valid,
            "number_count": len(sdk.parse_numbers(reply.text)), "finish_reason": reply.finish,
            "duration_ms": round((time.monotonic() - started) * 1000), "response_model": reply.response_model,
            "response_id": reply.response_id, "usage": reply.usage, "error_code": error_code, "error_message": error_message,
            # Memory-only scoring input; save_sample deliberately excludes it.
            "text": reply.text if valid else "", "expected_count": challenge["expected_count"]}


async def _models(token):
    async with httpx.AsyncClient(follow_redirects=False, trust_env=False, timeout=6) as client:
        async with client.stream("GET", endpoint_base() + "/models", headers={"Authorization": f"Bearer {token}"}) as response:
            if response.status_code != 200:
                raise FingerprintError("model_list_failed", f"本人 Key 的模型列表读取失败（HTTP {response.status_code}）。", 503)
            buffer = bytearray()
            async for chunk in response.aiter_bytes():
                buffer.extend(chunk)
                if len(buffer) > 1048576:
                    raise FingerprintError("model_list_failed", "模型列表超过读取上限。", 503)
            data = json.loads(buffer)
            rows = data.get("data") if isinstance(data, dict) else data
            if not isinstance(rows, list):
                raise ValueError()
            return {row if isinstance(row, str) else str(record(row).get("id") or "") for row in rows}


def available_models(token):
    try:
        return asyncio.run(asyncio.wait_for(_models(token), timeout=8))
    except FingerprintError:
        raise
    except Exception:
        raise FingerprintError("model_list_failed", "无法读取本人 Key 的模型列表，请稍后重试。", 503) from None
