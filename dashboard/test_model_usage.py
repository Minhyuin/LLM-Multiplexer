from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from usage.models import UsageLog


class ModelUsagePopoverTests(TestCase):
    def setUp(self):
        self.member = User.objects.create_user(username="popover-member")
        self.member.profile.display_name = "普通成员"
        self.member.profile.save()
        self.client.force_login(self.member)

    def usage(self, model, tokens, user=None, request_id=None, request_time=None):
        return UsageLog.objects.create(
            request_id=request_id or f"popover-{UsageLog.objects.count() + 1}",
            user=user if user is not None else self.member,
            model_name=model,
            total_tokens=tokens,
            request_time=request_time or timezone.now(),
        )

    def popover(self, **params):
        return self.client.get(reverse("public-model-usage"), params)

    def admin_popover(self, **params):
        return self.client.get(reverse("admin-model-usage"), params)

    def test_requires_login_and_admin_route_is_protected(self):
        self.assertEqual(self.admin_popover(model="alpha").status_code, 403)
        self.client.logout()
        self.assertEqual(self.popover(model="alpha").status_code, 302)
        self.assertEqual(self.admin_popover(model="alpha").status_code, 302)

    @override_settings(PUBLIC_DASHBOARD_ENABLED=False)
    def test_disabled_public_dashboard_also_blocks_the_popover(self):
        self.assertEqual(self.popover(model="alpha").status_code, 403)
        self.member.is_staff = True
        self.member.save()
        self.assertEqual(self.popover(model="alpha").status_code, 200)

    def test_endpoints_are_read_only(self):
        self.assertEqual(self.client.post(reverse("public-model-usage"), {"model": "alpha"}).status_code, 405)
        self.member.is_staff = True
        self.member.save()
        self.assertEqual(self.client.post(reverse("admin-model-usage"), {"model": "alpha"}).status_code, 405)

    def test_missing_model_is_a_bad_request(self):
        response = self.popover()
        self.assertEqual(response.status_code, 400)
        self.assertIn("error", response.json())

    def test_unknown_model_returns_an_empty_payload(self):
        response = self.popover(model="never-called", range="all")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["users"], [])
        self.assertEqual(payload["total_tokens"], 0)
        self.assertEqual(payload["total_requests"], 0)
        self.assertEqual(payload["user_count"], 0)

    def test_share_is_token_based_and_lists_every_member(self):
        second = User.objects.create_user(username="popover-second")
        second.profile.display_name = "第二成员"
        second.profile.save()
        third = User.objects.create_user(username="popover-third")
        self.usage("alpha", 600)
        self.usage("alpha", 300, user=second)
        self.usage("alpha", 100, user=third)

        payload = self.popover(model="alpha", range="all").json()

        self.assertEqual(payload["total_tokens"], 1000)
        self.assertEqual(payload["total_requests"], 3)
        self.assertEqual(payload["user_count"], 3)
        self.assertEqual([user["tokens"] for user in payload["users"]], [600, 300, 100])
        self.assertEqual([user["share"] for user in payload["users"]], [60.0, 30.0, 10.0])
        self.assertEqual(sum(user["share"] for user in payload["users"]), 100.0)
        self.assertEqual(payload["users"][0]["name"], "普通成员")
        # A member without a display name falls back to the login name.
        self.assertEqual(payload["users"][2]["name"], "popover-third")

    def test_other_models_and_unattributed_logs_are_not_counted(self):
        self.usage("alpha", 400)
        self.usage("beta", 900)
        UsageLog.objects.create(request_id="popover-orphan", user=None, model_name="alpha",
                                total_tokens=5000, request_time=timezone.now())

        payload = self.popover(model="alpha", range="all").json()

        self.assertEqual(payload["total_tokens"], 400)
        self.assertEqual(payload["total_requests"], 1)

    def test_range_filter_applies_to_the_breakdown(self):
        self.usage("alpha", 250, request_time=timezone.now() - timezone.timedelta(days=10))

        self.assertEqual(self.popover(model="alpha", range="last_7_days").json()["total_tokens"], 0)
        self.assertEqual(self.popover(model="alpha", range="all").json()["total_tokens"], 250)

    def test_group_filter_applies_to_the_breakdown(self):
        self.member.profile.lab_group = "alpha"
        self.member.profile.save()
        self.usage("alpha", 120)

        self.assertEqual(self.popover(model="alpha", range="all", group="beta").json()["total_tokens"], 0)
        self.assertEqual(self.popover(model="alpha", range="all", group="alpha").json()["total_tokens"], 120)

    def test_hidden_members_are_invisible_to_their_peers_but_visible_to_staff(self):
        hidden = User.objects.create_user(username="popover-hidden")
        hidden.profile.display_name = "隐身成员"
        hidden.profile.is_dashboard_visible = False
        hidden.profile.save()
        self.usage("alpha", 100)
        self.usage("alpha", 900, user=hidden)

        peer_payload = self.popover(model="alpha", range="all").json()
        self.assertEqual(peer_payload["total_tokens"], 100)
        self.assertEqual([user["name"] for user in peer_payload["users"]], ["普通成员"])

        self.member.is_staff = True
        self.member.save()
        staff_payload = self.admin_popover(model="alpha", range="all").json()
        self.assertEqual(staff_payload["total_tokens"], 1000)
        self.assertIn("隐身成员", [user["name"] for user in staff_payload["users"]])

    def test_perspective_view_uses_the_effective_account(self):
        target = User.objects.create_user(username="popover-target")
        self.member.is_staff = True
        self.member.save()
        self.assertEqual(self.client.post(reverse("view-as-start", args=[target.id])).status_code, 302)

        self.assertEqual(self.admin_popover(model="alpha").status_code, 403)
        self.assertEqual(self.popover(model="alpha").status_code, 200)

    def test_payload_is_not_cached(self):
        response = self.popover(model="alpha", range="all")
        self.assertIn("private", response["Cache-Control"])
        self.assertIn("no-store", response["Cache-Control"])

    def test_dashboard_shell_exposes_the_endpoint(self):
        html = self.client.get(reverse("public-dashboard")).content.decode()
        self.assertIn('data-model-usage="/dashboard/public/model-usage/"', html)
        self.member.is_staff = True
        self.member.save()
        html = self.client.get(reverse("admin-dashboard")).content.decode()
        self.assertIn('data-model-usage="/dashboard/admin/model-usage/"', html)

    def test_trend_module_renders_clickable_chips(self):
        self.usage("alpha", 300)
        html = self.client.get(reverse("public-dashboard-module", args=["trend"]), {"range": "all"}).json()["html"]
        self.assertIn('class="model-chip" data-model="alpha"', html)
        self.assertIn('aria-expanded="false"', html)
