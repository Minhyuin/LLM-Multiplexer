"""Read the proxy's available model list (``GET /v1/models``) for the portal UI.

The portal never persists a model catalogue and never fetches it while a page is
being rendered: the list is read only when somebody presses refresh, because the
request is made with that member's own key.  Requests happen server-side so a
member API key never reaches the browser.
"""

import logging
from datetime import datetime

import requests
from django.conf import settings
from django.utils import timezone

from api_keys.models import APIKey

logger = logging.getLogger(__name__)

MODEL_LIST_PATH = "/v1/models"
REQUEST_TIMEOUT = 6
MAX_MODELS = 500

#: Where the key used for the model request came from.
SOURCE_CONFIGURED = "configured"
SOURCE_MEMBER = "member"
SOURCE_PORTAL = "portal"

SOURCE_LABELS = {
    SOURCE_CONFIGURED: "门户专用 Key",
    SOURCE_MEMBER: "我的 API Key",
    SOURCE_PORTAL: "门户系统 Key",
}

NO_KEY_MESSAGE = "门户没有可用于读取模型列表的 API Key，请联系管理员配置。"

HINT_TEMPLATE = (
    "点击右上角「刷新模型」，将以「{source}」读取一次当前可用模型。"
    "不同的 Key 可能看到不同的模型列表。"
)

#: Provider presentation: name, badge letter and brand colour for the mark.
#: Model families the panel renders with their flagship logo.  Anything that
#: cannot be attributed to one of these families is grouped under ``other``.
PROVIDER_STYLES = {
    "gpt": {"label": "GPT", "logo": "brand/providers/gpt.svg", "accent": "#0B0B0B"},
    "claude": {"label": "Claude", "logo": "brand/providers/claude.svg", "accent": "#D97757"},
    "gemini": {"label": "Gemini", "logo": "brand/providers/gemini.svg", "accent": "#3186FF"},
    "deepseek": {"label": "DeepSeek", "logo": "brand/providers/deepseek.svg", "accent": "#4D6BFE"},
    "kimi": {"label": "Kimi", "logo": "brand/providers/kimi.svg", "accent": "#1783FF"},
    "glm": {"label": "GLM", "logo": "brand/providers/glm.svg", "accent": "#3859FF"},
    "grok": {"label": "Grok", "logo": "brand/providers/grok.svg", "accent": "#0B0B0B"},
    "qwen": {"label": "Qwen", "logo": "brand/providers/qwen.svg", "accent": "#082DFF"},
    "other": {"label": "其他", "logo": "", "accent": "#94A3B8"},
}

PROVIDER_ORDER = [
    "gpt", "claude", "deepseek", "gemini", "kimi", "glm", "grok", "qwen", "other",
]

PROVIDER_ALIASES = {
    "openai": "gpt", "open-ai": "gpt", "azure": "gpt", "azure-openai": "gpt", "chatgpt": "gpt",
    "anthropic": "claude", "claude": "claude",
    "google": "gemini", "google-ai": "gemini", "gemini": "gemini", "vertex": "gemini",
    "vertexai": "gemini", "pa": "gemini",
    "deepseek": "deepseek",
    "moonshot": "kimi", "moonshotai": "kimi", "moonshot-ai": "kimi", "kimi": "kimi",
    "zhipu": "glm", "z-ai": "glm", "zai": "glm", "z.ai": "glm", "bigmodel": "glm",
    "chatglm": "glm", "glm": "glm",
    "xai": "grok", "x-ai": "grok", "x.ai": "grok", "grok": "grok",
    "qwen": "qwen", "alibaba": "qwen", "aliyun": "qwen", "dashscope": "qwen",
    "tongyi": "qwen", "qwenlm": "qwen",
}

#: Model-id prefixes used when the proxy does not report a known owner.
PROVIDER_PREFIXES = [
    ("gpt", "gpt"), ("chatgpt", "gpt"), ("o1", "gpt"), ("o3", "gpt"), ("o4", "gpt"),
    ("claude", "claude"),
    ("gemini", "gemini"),
    ("deepseek", "deepseek"),
    ("kimi", "kimi"), ("moonshot", "kimi"),
    ("glm", "glm"), ("chatglm", "glm"),
    ("grok", "grok"),
    ("qwen", "qwen"), ("qwq", "qwen"), ("tongyi", "qwen"),
]


class ModelCatalogError(Exception):
    """The model list could not be read; the message is shown to the user."""


def _endpoint():
    base = (getattr(settings, "CLIPROXY_BASE_URL", "") or "").rstrip("/")
    if not base:
        raise ModelCatalogError("未配置 CLIPROXY_BASE_URL，无法读取模型列表。")
    return f"{base}{MODEL_LIST_PATH}"


def _usable_portal_key(user):
    """Pick the key used to ask the proxy, preferring the least surprising one."""
    configured = (getattr(settings, "CLIPROXY_MODELS_API_KEY", "") or "").strip()
    if configured:
        return configured, SOURCE_CONFIGURED

    if user is not None and getattr(user, "is_authenticated", False):
        own = (
            APIKey.objects.filter(user=user, status=APIKey.STATUS_ACTIVE)
            .exclude(token_plaintext="")
            .order_by("-created_at")
            .first()
        )
        if own is not None and own.is_usable:
            return own.token_plaintext, SOURCE_MEMBER

    portal = (
        APIKey.objects.filter(status=APIKey.STATUS_ACTIVE)
        .exclude(token_plaintext="")
        .order_by("created_at")
        .first()
    )
    if portal is not None:
        return portal.token_plaintext, SOURCE_PORTAL

    return "", ""


def _normalise(payload):
    rows = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise ModelCatalogError("模型接口返回的数据结构无法识别。")

    models = {}
    for row in rows:
        if isinstance(row, str):
            model_id, owner = row.strip(), ""
        elif isinstance(row, dict):
            model_id = str(row.get("id") or "").strip()
            owner = str(row.get("owned_by") or row.get("provider") or "").strip()
        else:
            continue
        if model_id:
            models.setdefault(model_id, {"id": model_id, "owner": owner})

    if not models:
        raise ModelCatalogError("代理当前没有返回任何可用模型。")

    return [models[model_id] for model_id in sorted(models)][:MAX_MODELS]


def provider_key(owner, model_id):
    """Resolve the provider bucket for one model."""
    for value in (owner, model_id):
        text = str(value or "").strip().lower()
        if not text:
            continue
        bucket = PROVIDER_ALIASES.get(text)
        if bucket:
            return bucket
    model_lower = str(model_id or "").strip().lower()
    for prefix, bucket in PROVIDER_PREFIXES:
        if model_lower.startswith(prefix):
            return bucket
    return "other"


def _group_by_provider(models):
    grouped = {}
    for model in models:
        bucket = provider_key(model["owner"], model["id"])
        grouped.setdefault(bucket, []).append(model)

    ordered = [key for key in PROVIDER_ORDER if key in grouped]
    ordered += sorted(key for key in grouped if key not in PROVIDER_ORDER)

    groups = []
    for bucket in ordered:
        style = PROVIDER_STYLES.get(bucket, PROVIDER_STYLES["other"])
        group_models = sorted(grouped[bucket], key=lambda item: item["id"])
        groups.append({
            "key": bucket,
            "label": style["label"],
            "logo": style["logo"],
            "accent": style["accent"],
            "count": len(group_models),
            "models": group_models,
        })
    return groups


def _request_models(token):
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        response = requests.get(_endpoint(), headers=headers, timeout=REQUEST_TIMEOUT)
    except requests.Timeout:
        raise ModelCatalogError(f"读取模型列表超时（{REQUEST_TIMEOUT} 秒），请稍后重试。")
    except requests.RequestException as exc:
        logger.warning("Model list request failed: %s", exc)
        raise ModelCatalogError("无法连接代理的模型接口，请稍后重试。")

    if response.status_code in (401, 403):
        raise ModelCatalogError(
            f"模型接口拒绝了当前 Key（HTTP {response.status_code}），请在代理侧确认该 Key 仍有效。"
        )
    if response.status_code != 200:
        raise ModelCatalogError(f"模型接口返回 HTTP {response.status_code}。")

    try:
        payload = response.json()
    except ValueError:
        raise ModelCatalogError("模型接口返回的内容不是有效 JSON。")

    return _normalise(payload)


def available_models(token):
    """Ask the proxy for the model list the given key can use."""
    return _request_models(token)


def collect_available_models(user, *, fetch=False):
    """Build the template context for the model section.

    ``fetch=False`` (page render) only describes what a refresh would do; the
    proxy is contacted exclusively when the member presses the refresh button.
    """
    token, source = _usable_portal_key(user)
    source_label = SOURCE_LABELS.get(source, "")
    context = {
        "models_loaded": False,
        "models_error": "",
        "models_count": 0,
        "models_provider_count": 0,
        "model_groups": [],
        "models_source": source,
        "models_source_label": source_label,
        "models_fetched_at": None,
        "models_hint": "",
    }

    if not token:
        context["models_error"] = NO_KEY_MESSAGE
        return context

    if not fetch:
        context["models_hint"] = HINT_TEMPLATE.format(source=source_label)
        return context

    try:
        models = available_models(token)
    except ModelCatalogError as exc:
        context["models_error"] = str(exc)
        return context

    groups = _group_by_provider(models)
    context.update({
        "models_loaded": True,
        "models_count": len(models),
        "models_provider_count": len(groups),
        "model_groups": groups,
        "models_fetched_at": datetime.now(tz=timezone.get_current_timezone()),
    })
    return context
