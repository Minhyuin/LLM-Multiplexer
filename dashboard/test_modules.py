from unittest.mock import patch
from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from usage.models import UsageLog


class DashboardModuleTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="module-member")
        self.user.profile.display_name = "测试成员"
        self.user.profile.save()
        self.client.force_login(self.user)

    def module(self, name, **params):
        return self.client.get(reverse("public-dashboard-module", args=[name]), params)

    def test_shell_does_not_aggregate_logs(self):
        with patch("dashboard.views.aggregate_dashboard", side_effect=AssertionError("shell queried statistics")), patch("dashboard.views._build_personal_dashboard_summary", side_effect=AssertionError("shell queried personal")):
            response = self.client.get(reverse("public-dashboard"))
        self.assertContains(response, 'data-module="summary"')
        self.assertNotContains(response, 'id="trend-data"')

    def test_module_is_authenticated_and_admin_route_is_protected(self):
        self.assertEqual(self.client.get(reverse("admin-dashboard-module", args=["summary"])).status_code, 403)
        self.assertEqual(self.module("active").status_code, 403)
        self.client.logout()
        self.assertEqual(self.module("summary").status_code, 302)

    @override_settings(PUBLIC_DASHBOARD_ENABLED=False)
    def test_disabled_public_dashboard_also_blocks_modules(self):
        self.assertEqual(self.module("summary").status_code, 403)

    def test_summary_only_returns_requested_data(self):
        UsageLog.objects.create(request_id="mod-1", user=self.user, model_name="alpha", total_tokens=123, request_time=timezone.now())
        response = self.module("summary", range="all")
        self.assertEqual(response.status_code, 200)
        self.assertIn("private", response["Cache-Control"])
        self.assertIn("no-store", response["Cache-Control"])
        self.assertIn("123", response.json()["html"])
        self.assertNotIn("trendChart", response.json()["html"])
        self.assertEqual(self.module("nonexistent").status_code, 404)
        self.assertEqual(self.module("keys", page="nonsense").status_code, 400)

    def test_hidden_team_usage_does_not_hide_own_personal_summary(self):
        self.user.profile.is_dashboard_visible = False
        self.user.profile.save()
        UsageLog.objects.create(request_id="hidden-1", user=self.user, model_name="alpha", total_tokens=321, request_time=timezone.now())
        summary = self.module("summary", range="all").json()["html"]
        personal = self.module("personal", range="all").json()["html"]
        self.assertIn('"total_tokens": 321', personal)
        self.assertNotIn("321", summary)

    def test_every_member_module_renders_and_empty_keys_page_is_valid(self):
        for name in ("summary", "personal", "trend", "rankings", "keys", "errors"):
            with self.subTest(module=name):
                response = self.module(name, range="all")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["module"], name)
        self.assertIn("共 0 条", self.module("keys", page="9999").json()["html"])

    def test_filters_affect_team_but_not_own_usage(self):
        self.user.profile.lab_group = "alpha"
        self.user.profile.save()
        UsageLog.objects.create(request_id="filter-1", user=self.user, model_name="alpha", total_tokens=987, request_time=timezone.now())
        self.assertNotIn("987", self.module("summary", range="all", group="beta").json()["html"])
        self.assertIn('"total_tokens": 987', self.module("personal", range="all", group="beta").json()["html"])

    def test_admin_can_read_active_module(self):
        self.user.is_staff = True
        self.user.save()
        self.assertEqual(self.module("active").status_code, 200)
        self.assertEqual(self.client.get(reverse("admin-dashboard-module", args=["active"])).status_code, 200)

    def test_perspective_uses_effective_account_for_modules(self):
        target = User.objects.create_user(username="perspective-member")
        self.user.is_staff = True
        self.user.save()
        response = self.client.post(reverse("view-as-start", args=[target.id]))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.module("active").status_code, 403)
        self.assertEqual(self.client.get(reverse("admin-dashboard-module", args=["summary"])).status_code, 403)
        self.assertEqual(self.module("personal").status_code, 200)

    def test_modules_are_read_only(self):
        self.assertEqual(self.client.post(reverse("public-dashboard-module", args=["summary"])).status_code, 405)
