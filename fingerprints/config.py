"""Server-only policy; browser input cannot select endpoints or sampling options."""
import fnmatch
import ipaddress
import re
from urllib.parse import urlsplit

from django.conf import settings

from core.site_settings import get_override

UPSTREAM_COMMIT = "a4ef212df06817e8071acd05f00219f4c9eb2f9a"
NON_TEXT = re.compile(r"(?:^|[-_/])(embedding|embeddings|rerank|reranker|whisper|tts|dall-e|imagen|veo|sora)(?:[-_/\d]|$)", re.I)


class FingerprintError(Exception):
    def __init__(self, code, message, status=400):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


LIMIT_FIELDS = {
    "DAILY_LIMIT": "fingerprint_daily_limit",
    "ADMIN_DAILY_LIMIT": "fingerprint_admin_daily_limit",
    "COOLDOWN_SECONDS": "fingerprint_cooldown_seconds",
    "CONCURRENCY": "fingerprint_concurrency",
}


def limit(name, default):
    field = LIMIT_FIELDS.get(name)
    if field:
        value = get_override(field, None)
        if value is not None:
            try:
                return max(1, int(value))
            except (TypeError, ValueError):
                pass
    return max(1, int(getattr(settings, "FINGERPRINT_" + name, default)))


def enabled():
    return bool(get_override("fingerprint_enabled", getattr(settings, "FINGERPRINT_ENABLED", False)))


def endpoint_base():
    base = str(getattr(settings, "FINGERPRINT_BASE_URL", "http://127.0.0.1:8317")).rstrip("/")
    parts = urlsplit("")
    try:
        parts = urlsplit(base)
        port = parts.port
        host = parts.hostname or ""
        local = host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        local = False
    if (not local or parts.scheme not in ("http", "https") or parts.username or parts.password
            or parts.query or parts.fragment or parts.path not in ("", "/v1")):
        raise FingerprintError("configuration", "检测接口必须配置为本机 CPA 数据接口。", 503)
    return base + ("" if parts.path else "/v1")


def model_policy(model_id):
    if not isinstance(model_id, str) or not model_id or len(model_id) > 255 or any(ord(c) < 32 for c in model_id):
        raise FingerprintError("invalid_model", "请选择有效的模型。")
    if NON_TEXT.search(model_id):
        raise FingerprintError("unsupported_model", "该模型不是受支持的文本生成模型。")
    mappings = getattr(settings, "FINGERPRINT_MODEL_POLICIES", {})
    if not isinstance(mappings, dict):
        raise FingerprintError("configuration", "检测协议配置无效。", 503)
    selected = mappings.get(model_id)
    if selected is None:
        selected = next((v for k, v in mappings.items() if fnmatch.fnmatchcase(model_id, k)), {})
    if not isinstance(selected, dict) or selected.get("enabled") is False:
        raise FingerprintError("unsupported_model", "当前模型尚未配置可用的检测协议。")
    api = selected.get("api", "chat_completions")
    fields = {"chat_completions": ("max_completion_tokens", "max_tokens"), "responses": ("max_output_tokens",), "messages": ("max_tokens",)}
    if api not in fields:
        raise FingerprintError("configuration", "检测协议配置无效。", 503)
    output_field = selected.get("output_field", fields[api][0])
    effort = selected.get("effort", "")
    if output_field not in fields[api] or not isinstance(effort, str) or len(effort) > 32:
        raise FingerprintError("configuration", "检测输出参数配置无效。", 503)
    return {"api": api, "output_field": output_field, "effort": effort, "service_tier": "default",
            "stream": True, "max_output_tokens": limit("MAX_OUTPUT_TOKENS", 8192), "count": 3,
            "request_timeout": limit("REQUEST_TIMEOUT", 120), "response_max_bytes": limit("RESPONSE_MAX_BYTES", 1048576)}
