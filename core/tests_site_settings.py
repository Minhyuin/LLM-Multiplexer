from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse

from core.models import SiteSettings
from core.site_settings import (
    configured_public_api_base_url,
    lab_group_choices,
    public_api_base_url,
    public_api_v1_url,
    query_trace_enabled,
)
from fingerprints.config import enabled as fingerprint_enabled
from fingerprints.config import limit as fingerprint_limit


@override_settings(ALLOWED_HOSTS=["8.134.194.161"])
class PublicAPIBaseURLTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def _request(self, host="8.134.194.161", secure=False):
        return self.factory.get("/quota/my/", HTTP_HOST=host, secure=secure)

    def test_falls_back_to_request_origin_and_appends_v1(self):
        request = self._request()
        self.assertEqual(public_api_base_url(request), "http://8.134.194.161")
        self.assertEqual(public_api_v1_url(request), "http://8.134.194.161/v1")

    def test_configured_origin_overrides_request_and_normalizes_slash(self):
        SiteSettings.objects.create(public_api_base_url="https://api.example.com/")
        request = self._request()
        self.assertEqual(configured_public_api_base_url(), "https://api.example.com")
        self.assertEqual(public_api_v1_url(request), "https://api.example.com/v1")

    def test_blank_configuration_means_automatic(self):
        SiteSettings.objects.create(public_api_base_url="")
        self.assertEqual(configured_public_api_base_url(), "")
        self.assertEqual(public_api_base_url(self._request()), "http://8.134.194.161")

    def test_https_request_uses_https_origin(self):
        self.assertEqual(public_api_v1_url(self._request(secure=True)), "https://8.134.194.161/v1")


class SiteSettingsModelTests(TestCase):
    def test_load_is_a_singleton_and_save_forces_pk_one(self):
        first = SiteSettings.load()
        second = SiteSettings.load()
        self.assertEqual(first.pk, 1)
        self.assertEqual(second.pk, 1)
        second.public_api_base_url = "https://api.example.com"
        second.save()
        self.assertEqual(SiteSettings.objects.count(), 1)
        self.assertEqual(SiteSettings.objects.get(pk=1).public_api_base_url, "https://api.example.com")

    def test_accepts_root_or_v1_and_normalizes_to_origin(self):
        for value in ("https://api.example.com", "https://api.example.com/", "https://api.example.com/v1/"):
            settings_obj = SiteSettings(public_api_base_url=value)
            settings_obj.full_clean()
            self.assertEqual(settings_obj.public_api_base_url, "https://api.example.com")

    def test_rejects_non_http_or_unexpected_paths(self):
        for value in (
            "ftp://api.example.com",
            "https://api.example.com/foo",
            "https://api.example.com?x=1",
            "https://api.example.com#fragment",
            "https://user:pass@api.example.com",
        ):
            settings_obj = SiteSettings(public_api_base_url=value)
            with self.assertRaises(ValidationError, msg=value):
                settings_obj.full_clean()


class SiteSettingsAdminViewTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.admin = user_model.objects.create_user("settings-admin", password="test-pass", is_staff=True)
        self.member = user_model.objects.create_user("settings-member", password="test-pass")

    def test_member_is_forbidden(self):
        self.client.force_login(self.member)
        self.assertEqual(self.client.get(reverse("admin-site-settings")).status_code, 403)

    def test_admin_can_save_and_clear_the_override(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse("admin-site-settings"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "站点设置")

        response = self.client.post(
            reverse("admin-site-settings"),
            {"public_api_base_url": "https://api.example.com/v1/"},
            follow=True,
        )
        self.assertRedirects(response, reverse("admin-site-settings"))
        settings_obj = SiteSettings.objects.get(pk=1)
        self.assertEqual(settings_obj.public_api_base_url, "https://api.example.com")
        self.assertEqual(settings_obj.updated_by, self.admin)

        response = self.client.post(
            reverse("admin-site-settings"),
            {"public_api_base_url": ""},
            follow=True,
        )
        self.assertRedirects(response, reverse("admin-site-settings"))
        settings_obj.refresh_from_db()
        self.assertEqual(settings_obj.public_api_base_url, "")


@override_settings(
    QUERY_TRACE_ENABLED=False,
    FINGERPRINT_ENABLED=False,
    FINGERPRINT_DAILY_LIMIT=3,
    FINGERPRINT_ADMIN_DAILY_LIMIT=20,
    FINGERPRINT_COOLDOWN_SECONDS=60,
    FINGERPRINT_CONCURRENCY=2,
)
class FeatureOverrideTests(TestCase):
    def test_blank_overrides_follow_environment(self):
        SiteSettings.objects.create()
        self.assertFalse(query_trace_enabled())
        self.assertFalse(fingerprint_enabled())
        self.assertEqual(fingerprint_limit("DAILY_LIMIT", 3), 3)
        self.assertEqual(fingerprint_limit("CONCURRENCY", 2), 2)
        self.assertEqual(
            [value for value, _ in lab_group_choices()],
            ["电控", "算法", "机械", "宣传"],
        )

    def test_saved_overrides_take_precedence(self):
        SiteSettings.objects.create(
            query_trace_enabled=True,
            fingerprint_enabled=True,
            fingerprint_daily_limit=7,
            fingerprint_admin_daily_limit=9,
            fingerprint_cooldown_seconds=11,
            fingerprint_concurrency=1,
            lab_groups="电控\n算法\n算法\n视觉",
        )
        self.assertTrue(query_trace_enabled())
        self.assertTrue(fingerprint_enabled())
        self.assertEqual(fingerprint_limit("DAILY_LIMIT", 3), 7)
        self.assertEqual(fingerprint_limit("ADMIN_DAILY_LIMIT", 20), 9)
        self.assertEqual(fingerprint_limit("COOLDOWN_SECONDS", 60), 11)
        self.assertEqual(fingerprint_limit("CONCURRENCY", 2), 1)
        self.assertEqual(
            [value for value, _ in lab_group_choices()],
            ["电控", "算法", "视觉"],
        )

    def test_admin_can_save_feature_controls_and_groups(self):
        admin = get_user_model().objects.create_user("feature-admin", password="x", is_staff=True)
        self.client.force_login(admin)
        response = self.client.post(
            reverse("admin-site-settings"),
            {
                "public_api_base_url": "",
                "query_trace_enabled": "1",
                "fingerprint_enabled": "0",
                "fingerprint_daily_limit": "5",
                "fingerprint_admin_daily_limit": "12",
                "fingerprint_cooldown_seconds": "30",
                "fingerprint_concurrency": "1",
                "lab_groups": "电控\n算法\n视觉\n视觉",
            },
            follow=True,
        )
        self.assertRedirects(response, reverse("admin-site-settings"))
        saved = SiteSettings.objects.get(pk=1)
        self.assertTrue(saved.query_trace_enabled)
        self.assertFalse(saved.fingerprint_enabled)
        self.assertEqual(saved.fingerprint_daily_limit, 5)
        self.assertEqual(saved.lab_groups, "电控\n算法\n视觉")


class QuotaPublicAPIBaseURLTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("quota-base-url")

    @override_settings(ALLOWED_HOSTS=["8.134.194.161", "testserver"])
    def test_quota_uses_request_host_by_default(self):
        self.client.force_login(self.user)
        response = self.client.get("/quota/my/", HTTP_HOST="8.134.194.161")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["cliproxy_base_url"], "http://8.134.194.161/v1")

    @override_settings(ALLOWED_HOSTS=["8.134.194.161", "testserver"])
    def test_quota_uses_the_admin_override(self):
        SiteSettings.objects.create(public_api_base_url="https://api.example.com")
        self.client.force_login(self.user)
        response = self.client.get("/quota/my/", HTTP_HOST="8.134.194.161")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["cliproxy_base_url"], "https://api.example.com/v1")
