"""Tests for the "models and credentials" page: the proxy model list."""

from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from api_keys.models import APIKey
from dashboard import model_catalog

User = get_user_model()

MEMBER_KEY = "labsk-model-panel-member"


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class ModelPanelTestMixin(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            "model-panel-admin", password="pw-model-panel", is_staff=True
        )
        APIKey.objects.create(
            user=self.user,
            name="自用 Key",
            status=APIKey.STATUS_ACTIVE,
            token_plaintext=MEMBER_KEY,
            **APIKey.build_token_metadata(MEMBER_KEY),
        )
        # The credentials page also reads the management API and may kick off a
        # background quota refresh; neither belongs in these tests.
        for target, value in (
            ("dashboard.views._auto_refresh_codex_quotas", lambda *a, **k: None),
            ("dashboard.views._fetch_auth_files", lambda *a, **k: []),
        ):
            patcher = mock.patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client.force_login(self.user)

    def page(self):
        response = self.client.get(reverse("admin-credentials"))
        self.assertEqual(response.status_code, 200)
        return response

    def refresh(self):
        response = self.client.post(reverse("admin-credentials-models"))
        self.assertEqual(response.status_code, 200)
        return response.json()


class ModelPageDoesNotFetchTests(ModelPanelTestMixin):
    """Opening the page must not spend the member's key on a model request."""

    @mock.patch("dashboard.model_catalog.requests.get")
    def test_page_asks_the_member_to_refresh_instead_of_fetching(self, get):
        response = self.page()

        self.assertFalse(get.called)
        self.assertFalse(response.context["models_loaded"])
        self.assertEqual(response.context["models_count"], 0)
        self.assertEqual(response.context["model_groups"], [])
        self.assertContains(response, "尚未读取模型列表")
        self.assertContains(response, "刷新模型")
        self.assertContains(response, "我的 API Key")
        self.assertNotContains(response, 'class="model-chip"')
        self.assertNotContains(response, 'class="provider-mark"')

    @mock.patch("dashboard.model_catalog.requests.get")
    def test_page_reports_a_missing_key_without_fetching(self, get):
        APIKey.objects.all().delete()

        response = self.page()

        self.assertFalse(get.called)
        self.assertEqual(response.context["models_error"], model_catalog.NO_KEY_MESSAGE)
        self.assertContains(response, "没有可用于读取模型列表的 API Key")
        self.assertContains(response, "model-catalog-alert")

    @mock.patch("dashboard.model_catalog.requests.get")
    def test_refresh_button_is_the_only_fetch_trigger(self, get):
        get.return_value = FakeResponse(200, {"data": [{"id": "gpt-6.1-sol"}]})

        self.page()
        self.assertFalse(get.called)

        self.refresh()

        self.assertTrue(get.called)


class ModelRefreshTests(ModelPanelTestMixin):
    @mock.patch("dashboard.model_catalog.requests.get")
    def test_refresh_lists_models_grouped_by_family(self, get):
        get.return_value = FakeResponse(200, {"data": [
            {"id": "gpt-6.1-sol", "owned_by": "openai"},
            {"id": "gpt-6.1-mini", "owned_by": "openai"},
            {"id": "claude-4-opus", "owned_by": "anthropic"},
            {"id": "gpt-6.1-sol", "owned_by": "duplicate"},
        ]})

        payload = self.refresh()

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["count"], 3)
        self.assertEqual(payload["provider_count"], 2)
        html = payload["html"]
        self.assertIn("model-provider-row", html)
        self.assertIn("GPT", html)
        self.assertIn("Claude", html)
        self.assertIn("provider-logo", html)
        self.assertIn("brand/providers/gpt.svg", html)
        self.assertIn("brand/providers/claude.svg", html)
        self.assertIn("claude-4-opus", html)
        self.assertEqual(html.count('class="model-provider-row"'), 2)

        args, kwargs = get.call_args
        self.assertTrue(args[0].endswith("/v1/models"), args[0])
        self.assertEqual(kwargs["headers"]["Authorization"], f"Bearer {MEMBER_KEY}")
        self.assertNotIn(MEMBER_KEY, html)

    @mock.patch("dashboard.model_catalog.requests.get")
    def test_refresh_groups_models_without_an_owner_but_with_known_prefixes(self, get):
        get.return_value = FakeResponse(200, {"data": [
            {"id": "gemini-3-pro"},
            {"id": "grok-5"},
            {"id": "house-model-1", "owned_by": "internal-lab"},
        ]})

        payload = self.refresh()

        html = payload["html"]
        for label in ("Gemini", "Grok", "其他"):
            self.assertIn(label, html)
        self.assertIn("brand/providers/gemini.svg", html)
        self.assertIn("brand/providers/grok.svg", html)
        self.assertEqual(payload["provider_count"], 3)
        # The catch-all family has no logo of its own and falls back to a mark.
        self.assertIn("provider-mark", html)

    @mock.patch("dashboard.model_catalog.requests.get")
    def test_refresh_reports_rejected_key_instead_of_models(self, get):
        get.return_value = FakeResponse(401, {"error": "unauthorized"})

        payload = self.refresh()

        self.assertFalse(payload["ok"])
        self.assertIn("401", payload["error"])
        self.assertIn("model-catalog-alert", payload["html"])
        self.assertEqual(payload["count"], 0)

    @mock.patch("dashboard.model_catalog.requests.get")
    def test_configured_key_wins_over_the_member_key(self, get):
        get.return_value = FakeResponse(200, {"data": [{"id": "gpt-6.1-sol"}]})

        with self.settings(CLIPROXY_MODELS_API_KEY="labsk-portal-dedicated"):
            payload = self.refresh()

        self.assertEqual(get.call_args.kwargs["headers"]["Authorization"], "Bearer labsk-portal-dedicated")
        self.assertEqual(payload["source_label"], model_catalog.SOURCE_LABELS[model_catalog.SOURCE_CONFIGURED])
        self.assertNotIn("labsk-portal-dedicated", payload["html"])

    @mock.patch("dashboard.model_catalog.requests.get")
    def test_rejected_configured_key_falls_back_to_member_key(self, get):
        get.side_effect = [
            FakeResponse(401, {"error": "Invalid API key"}),
            FakeResponse(200, {"data": [{"id": "claude-4-opus"}]}),
        ]

        with self.settings(CLIPROXY_MODELS_API_KEY="labsk-stale"):
            payload = self.refresh()

        self.assertTrue(payload["ok"])
        self.assertEqual(get.call_count, 2)
        self.assertEqual(get.call_args.kwargs["headers"]["Authorization"], f"Bearer {MEMBER_KEY}")
        self.assertEqual(payload["source_label"], model_catalog.SOURCE_LABELS[model_catalog.SOURCE_MEMBER])
        self.assertIn("claude-4-opus", payload["html"])

    def test_refresh_rejects_get(self):
        response = self.client.get(reverse("admin-credentials-models"))

        self.assertEqual(response.status_code, 405)
        self.assertFalse(response.json()["ok"])

    def test_refresh_requires_authentication(self):
        self.client.logout()

        response = self.client.post(reverse("admin-credentials-models"))

        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response["Location"])


class ProviderGroupingTests(TestCase):
    def test_owner_wins_then_model_prefix_then_other(self):
        self.assertEqual(model_catalog.provider_key("openai", "claude-x"), "gpt")
        self.assertEqual(model_catalog.provider_key("", "claude-4-opus"), "claude")
        self.assertEqual(model_catalog.provider_key("", "gpt-6.1-sol"), "gpt")
        self.assertEqual(model_catalog.provider_key("", "o4-mini"), "gpt")
        self.assertEqual(model_catalog.provider_key("", "qwen-3-max"), "qwen")
        self.assertEqual(model_catalog.provider_key("", "qwq-plus"), "qwen")
        self.assertEqual(model_catalog.provider_key("", "house-model-1"), "other")

    def test_the_collected_families_and_their_aliases(self):
        cases = {
            "openai": "gpt", "azure-openai": "gpt",
            "anthropic": "claude",
            "google": "gemini", "vertexai": "gemini",
            "deepseek": "deepseek",
            "moonshotai": "kimi", "moonshot": "kimi",
            "z-ai": "glm", "zhipu": "glm", "bigmodel": "glm",
            "x-ai": "grok", "xai": "grok",
            "qwen": "qwen", "alibaba": "qwen", "dashscope": "qwen", "tongyi": "qwen",
        }
        for owner, expected in cases.items():
            self.assertEqual(model_catalog.provider_key(owner, "anything"), expected, owner)

    def test_groups_are_ordered_counted_and_carry_their_logo(self):
        models = model_catalog._normalise({"data": [
            {"id": "claude-4-opus"},
            {"id": "gpt-6.1-sol"},
            {"id": "gpt-6.1-mini"},
            {"id": "kimi-k3"},
            {"id": "glm-5"},
            {"id": "qwen-3-max"},
            {"id": "house-model-1", "owned_by": "internal-lab"},
        ]})

        groups = model_catalog._group_by_provider(models)

        self.assertEqual(
            [group["key"] for group in groups],
            ["gpt", "claude", "kimi", "glm", "qwen", "other"],
        )
        gpt = groups[0]
        self.assertEqual(gpt["label"], "GPT")
        self.assertEqual(gpt["logo"], "brand/providers/gpt.svg")
        self.assertEqual(gpt["count"], 2)
        self.assertEqual([model["id"] for model in gpt["models"]], ["gpt-6.1-mini", "gpt-6.1-sol"])
        self.assertEqual(groups[2]["logo"], "brand/providers/kimi.svg")
        self.assertEqual(groups[3]["logo"], "brand/providers/glm.svg")
        self.assertEqual(groups[4]["label"], "Qwen")
        self.assertEqual(groups[4]["logo"], "brand/providers/qwen.svg")
        self.assertEqual(groups[5]["label"], "其他")
        self.assertEqual(groups[5]["logo"], "")

    def test_every_logo_file_exists_in_static(self):
        from pathlib import Path

        from django.conf import settings

        for key, style in model_catalog.PROVIDER_STYLES.items():
            if not style["logo"]:
                self.assertEqual(key, "other")
                continue
            path = Path(settings.STATICFILES_DIRS[0]) / style["logo"] if settings.STATICFILES_DIRS else None
            if path is None:
                path = Path(settings.BASE_DIR) / "static" / style["logo"]
            self.assertTrue(path.exists(), f"missing logo for {key}: {path}")
            self.assertIn("<svg", path.read_text(encoding="utf-8"))


class ModelPayloadTests(TestCase):
    def test_accepts_plain_list_payloads(self):
        models = model_catalog._normalise(["b-model", "a-model", ""])

        self.assertEqual([m["id"] for m in models], ["a-model", "b-model"])
        self.assertEqual(models[0]["owner"], "")

    def test_rejects_unusable_payloads(self):
        for payload in ({"object": "list"}, "nope", {"data": []}):
            with self.assertRaises(model_catalog.ModelCatalogError):
                model_catalog._normalise(payload)

    def test_timeout_and_connection_failures_are_reported(self):
        import requests

        with mock.patch("dashboard.model_catalog.requests.get", side_effect=requests.Timeout()):
            with self.assertRaises(model_catalog.ModelCatalogError) as caught:
                model_catalog._request_models("labsk-x")
        self.assertIn("超时", str(caught.exception))

        with mock.patch("dashboard.model_catalog.requests.get", side_effect=requests.ConnectionError()):
            with self.assertRaises(model_catalog.ModelCatalogError) as caught:
                model_catalog._request_models("labsk-x")
        self.assertIn("无法连接", str(caught.exception))

    def test_collect_without_fetch_describes_the_refresh(self):
        context = model_catalog.collect_available_models(None, fetch=False)

        self.assertFalse(context["models_loaded"])
        self.assertEqual(context["models_error"], model_catalog.NO_KEY_MESSAGE)

    def test_collect_without_fetch_never_calls_the_proxy(self):
        user = User.objects.create_user("plain-collector", password="pw-collector")
        token = "labsk-collector-key-0001"
        APIKey.objects.create(
            user=user, name="k", status=APIKey.STATUS_ACTIVE,
            token_plaintext=token, **APIKey.build_token_metadata(token),
        )

        with mock.patch("dashboard.model_catalog.requests.get") as get:
            context = model_catalog.collect_available_models(user, fetch=False)

        self.assertFalse(get.called)
        self.assertFalse(context["models_loaded"])
        self.assertEqual(context["models_source"], model_catalog.SOURCE_MEMBER)
        self.assertIn("我的 API Key", context["models_hint"])
