"""Tests for the admin view-as (read-only account perspective) feature."""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from quota.models import UserQuotaSnapshot
from usage.models import UsageLog
from users.middleware import VIEW_AS_SESSION_KEY

User = get_user_model()


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class ViewAsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff_admin = User.objects.create_user("staff-admin", password="test-password", is_staff=True)
        cls.role_admin = User.objects.create_user("role-admin", password="test-password")
        cls.role_admin.profile.role = "admin"
        cls.role_admin.profile.display_name = "Role Admin"
        cls.role_admin.profile.save()
        cls.member = User.objects.create_user("member", password="test-password")
        cls.member.profile.display_name = "Member One"
        cls.member.profile.lab_group = "算法"
        cls.member.profile.save()
        cls.disabled_user = User.objects.create_user("disabled-user", password="test-password", is_active=False)

    # --- helpers -----------------------------------------------------------------
    def start_url(self, target):
        return reverse("view-as-start", args=[target.pk])

    def stop_url(self):
        return reverse("view-as-stop")

    def active_view_as(self, target=None):
        """Turn self.client into a staff admin already borrowing `target`."""
        target = target or self.member
        self.client.force_login(self.staff_admin)
        response = self.client.post(self.start_url(target))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.session[VIEW_AS_SESSION_KEY], target.pk)
        return self.client

    def csrf_client(self, user):
        client = Client(enforce_csrf_checks=True)
        client.force_login(user)
        client.get("/")  # rendering {% csrf_token %} seeds the csrftoken cookie
        return client

    @staticmethod
    def csrf_token(client):
        return client.cookies["csrftoken"].value

    # --- starting a perspective --------------------------------------------------
    def test_start_requires_post(self):
        self.client.force_login(self.staff_admin)
        response = self.client.get(self.start_url(self.member))
        self.assertEqual(response.status_code, 405)
        self.assertNotIn(VIEW_AS_SESSION_KEY, self.client.session)

    def test_start_requires_csrf_token(self):
        client = self.csrf_client(self.staff_admin)
        response = client.post(self.start_url(self.member))
        self.assertEqual(response.status_code, 403)
        self.assertNotIn(VIEW_AS_SESSION_KEY, client.session)

    def test_member_and_anonymous_cannot_start(self):
        self.client.force_login(self.member)
        response = self.client.post(self.start_url(self.staff_admin))
        self.assertEqual(response.status_code, 403)
        self.assertNotIn(VIEW_AS_SESSION_KEY, self.client.session)

        anonymous = Client()
        response = anonymous.post(self.start_url(self.member))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith("/accounts/login/"))
        self.assertNotIn(VIEW_AS_SESSION_KEY, anonymous.session)

    def test_start_rejects_self_inactive_and_missing_targets(self):
        self.client.force_login(self.staff_admin)
        for target in (self.staff_admin, self.disabled_user):
            with self.subTest(target=target.username):
                response = self.client.post(self.start_url(target))
                self.assertEqual(response.status_code, 302)
                self.assertNotIn(VIEW_AS_SESSION_KEY, self.client.session)

        response = self.client.post(reverse("view-as-start", args=[999999]))
        self.assertEqual(response.status_code, 404)

    def test_staff_admin_switches_effective_user_but_keeps_real_principal(self):
        client = self.active_view_as()
        self.assertEqual(int(client.session["_auth_user_id"]), self.staff_admin.pk)

        response = client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.wsgi_request.user.pk, self.member.pk)
        self.assertEqual(response.wsgi_request.view_as.admin_user.pk, self.staff_admin.pk)
        self.assertContains(response, "Member One")
        self.assertContains(response, "账户视角")
        self.assertContains(response, "返回管理员视角")

    def test_profile_role_admin_can_start_and_stop(self):
        client = Client()
        client.force_login(self.role_admin)
        response = client.post(self.start_url(self.member))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(client.session[VIEW_AS_SESSION_KEY], self.member.pk)
        self.assertContains(client.get("/"), "账户视角")

        response = client.post(self.stop_url())
        self.assertEqual(response.status_code, 302)
        self.assertNotIn(VIEW_AS_SESSION_KEY, client.session)

    # --- banner and navigation ---------------------------------------------------
    def test_banner_navigation_and_member_pages(self):
        client = self.active_view_as()
        response = client.get("/")
        self.assertContains(response, self.stop_url())  # banner offers the way back
        self.assertContains(response, "只读")
        self.assertNotContains(response, "/users/admin/list/")
        self.assertContains(response, "/usage/my/")

        self.assertEqual(client.get(reverse("profile")).status_code, 200)
        self.assertEqual(client.get(reverse("my-usage-logs")).status_code, 200)

    # --- target-account data isolation -------------------------------------------
    def test_preview_sees_the_targets_data_only(self):
        """Every page rendered inside a preview belongs to the target account."""
        self.staff_admin.profile.display_name = "Staff Admin Name"
        self.staff_admin.profile.lab_group = "运营"
        self.staff_admin.profile.save()
        now = timezone.localtime()
        UsageLog.objects.create(
            request_id="member-req",
            user=self.member,
            model_name="member-only-model",
            request_time=now,
            total_tokens=11,
        )
        UsageLog.objects.create(
            request_id="admin-req",
            user=self.staff_admin,
            model_name="admin-only-model",
            request_time=now,
            total_tokens=22,
        )
        UserQuotaSnapshot.objects.create(
            user=self.member, year=now.year, month=now.month, token_limit=1000, token_used=11
        )
        UserQuotaSnapshot.objects.create(
            user=self.staff_admin, year=now.year, month=now.month, token_limit=5000, token_used=22
        )

        client = self.active_view_as(self.member)

        page = client.get(reverse("profile"))
        self.assertContains(page, "Member One")
        self.assertNotContains(page, "Staff Admin Name")

        usage_page = client.get(reverse("my-usage-logs"))
        self.assertEqual({log.user_id for log in usage_page.context["logs"]}, {self.member.pk})
        self.assertEqual(
            [row["model_name"] for row in usage_page.context["model_usage"]], ["member-only-model"]
        )
        self.assertNotContains(usage_page, "admin-only-model")

        quota_page = client.get(reverse("my-quota"))
        self.assertEqual(quota_page.context["snapshot"].user_id, self.member.pk)
        self.assertEqual({row.user_id for row in quota_page.context["snapshots"]}, {self.member.pk})

    # --- read-only enforcement ---------------------------------------------------
    def test_read_only_blocks_unsafe_methods(self):
        client = self.active_view_as()
        original_name = self.member.profile.display_name
        response = client.post(
            reverse("profile"),
            {"display_name": "Hacked", "lab_group": "算法", "is_dashboard_visible": "on"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertContains(response, "只读预览", status_code=403)
        self.assertContains(response, self.stop_url(), status_code=403)
        self.member.profile.refresh_from_db()
        self.assertEqual(self.member.profile.display_name, original_name)

        response = client.post(reverse("admin-user-toggle", args=[self.member.pk]))
        self.assertEqual(response.status_code, 403)
        self.member.refresh_from_db()
        self.assertTrue(self.member.is_active)

        response = client.post(reverse("request-events-sync"))
        self.assertEqual(response.status_code, 403)

    def test_read_only_blocks_get_side_effect_endpoints(self):
        client = self.active_view_as()
        with patch("usage.views.auto_sync_cliproxy_usage_records") as sync, patch(
            "usage.views.auto_sync_usage_queue"
        ) as queue, patch("dashboard.views._auto_refresh_codex_quotas") as refresh, patch(
            "dashboard.views._fetch_auth_files"
        ) as fetch:
            for name in ("auto-sync-status", "request-events", "admin-credentials"):
                with self.subTest(url_name=name):
                    response = client.get(reverse(name))
                    self.assertEqual(response.status_code, 403)
            sync.assert_not_called()
            queue.assert_not_called()
            refresh.assert_not_called()
            fetch.assert_not_called()

    def test_side_effect_endpoints_work_again_after_stop(self):
        client = self.active_view_as()
        client.post(self.stop_url())

        with patch("usage.views.auto_sync_cliproxy_usage_records", return_value=(None, False)) as sync, patch(
            "usage.views.auto_sync_usage_queue", return_value=({}, False)
        ) as queue:
            response = client.get(reverse("auto-sync-status"))
            self.assertEqual(response.status_code, 200)
            sync.assert_called_once()
            queue.assert_called_once()

        with patch("usage.views.auto_sync_usage_queue", return_value=({}, False)) as queue:
            response = client.get(reverse("request-events"))
            self.assertEqual(response.status_code, 200)
            queue.assert_called_once()

        with patch("dashboard.views._auto_refresh_codex_quotas") as refresh, patch(
            "dashboard.views._fetch_auth_files", return_value=[]
        ) as fetch:
            response = client.get(reverse("admin-credentials"))
            self.assertEqual(response.status_code, 200)
            refresh.assert_called_once()
            fetch.assert_called_once()

    def test_viewing_staff_target_keeps_staff_pages_but_read_only(self):
        client = self.active_view_as(self.role_admin)
        response = client.get(reverse("admin-user-list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "账户视角")

        response = client.post(reverse("admin-user-toggle", args=[self.member.pk]))
        self.assertEqual(response.status_code, 403)
        self.member.refresh_from_db()
        self.assertTrue(self.member.is_active)

    # --- leaving a perspective ---------------------------------------------------
    def test_stop_requires_post_and_csrf(self):
        client = self.active_view_as()
        self.assertEqual(client.get(self.stop_url()).status_code, 405)
        self.assertIn(VIEW_AS_SESSION_KEY, client.session)

        csrf_client = self.csrf_client(self.staff_admin)
        csrf_client.post(self.start_url(self.member), {"csrfmiddlewaretoken": self.csrf_token(csrf_client)})
        self.assertEqual(csrf_client.session[VIEW_AS_SESSION_KEY], self.member.pk)
        self.assertEqual(csrf_client.post(self.stop_url()).status_code, 403)
        self.assertIn(VIEW_AS_SESSION_KEY, csrf_client.session)
        response = csrf_client.post(self.stop_url(), {"csrfmiddlewaretoken": self.csrf_token(csrf_client)})
        self.assertEqual(response.status_code, 302)
        self.assertNotIn(VIEW_AS_SESSION_KEY, csrf_client.session)

    def test_stop_returns_to_admin_perspective(self):
        client = self.active_view_as()
        response = client.post(self.stop_url())
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("admin-user-list"))
        self.assertNotIn(VIEW_AS_SESSION_KEY, client.session)
        self.assertEqual(int(client.session["_auth_user_id"]), self.staff_admin.pk)

        page = client.get("/")
        self.assertEqual(page.wsgi_request.user.pk, self.staff_admin.pk)
        self.assertNotContains(page, "账户视角")
        self.assertContains(page, "/users/admin/list/")
        self.assertContains(page, "已返回管理员视角")
        self.assertEqual(client.get(response.url).status_code, 200)

    def test_logout_still_available_while_view_as_active(self):
        client = self.active_view_as()
        response = client.post("/accounts/logout/")
        self.assertEqual(response.status_code, 302)
        self.assertNotIn("_auth_user_id", client.session)
        self.assertNotIn(VIEW_AS_SESSION_KEY, client.session)
        self.assertEqual(client.get("/").status_code, 302)

    def test_member_cannot_stop_view_as(self):
        self.client.force_login(self.member)
        response = self.client.post(self.stop_url())
        self.assertEqual(response.status_code, 403)

    # --- stale / revoked state ---------------------------------------------------
    def test_target_deleted_returns_to_admin_perspective(self):
        client = self.active_view_as()
        User.objects.filter(pk=self.member.pk).delete()

        response = client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(VIEW_AS_SESSION_KEY, client.session)
        self.assertEqual(response.wsgi_request.user.pk, self.staff_admin.pk)
        self.assertContains(response, "被预览的账号已被删除")

        self.assertEqual(client.post(self.stop_url()).status_code, 302)

    def test_target_disabled_returns_to_admin_perspective(self):
        client = self.active_view_as()
        User.objects.filter(pk=self.member.pk).update(is_active=False)

        response = client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(VIEW_AS_SESSION_KEY, client.session)
        self.assertEqual(response.wsgi_request.user.pk, self.staff_admin.pk)
        self.assertContains(response, "已自动返回管理员视角")

    def test_revoked_admin_loses_borrowed_privileges(self):
        client = self.active_view_as()
        # Server-side demotion while the session still claims a borrowed identity.
        self.staff_admin.profile.role = "user"
        self.staff_admin.profile.save(update_fields=["role"])
        User.objects.filter(pk=self.staff_admin.pk).update(is_staff=False)

        response = client.get(reverse("admin-user-list"))
        self.assertEqual(response.status_code, 403)
        self.assertNotIn(VIEW_AS_SESSION_KEY, client.session)

        page = client.get("/")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(page.wsgi_request.user.pk, self.staff_admin.pk)
        self.assertContains(page, "已不再具有管理员权限")
        self.assertNotContains(page, "/users/admin/list/")

    def test_forged_session_view_as_does_not_escalate(self):
        target_admin = User.objects.create_user("target-admin", password="test-password", is_staff=True)
        client = Client()
        client.force_login(self.member)
        session = client.session
        session[VIEW_AS_SESSION_KEY] = target_admin.pk
        session.save()

        response = client.get(reverse("admin-user-list"))
        self.assertEqual(response.status_code, 403)
        self.assertNotIn(VIEW_AS_SESSION_KEY, client.session)

        page = client.get("/")
        self.assertEqual(page.wsgi_request.user.pk, self.member.pk)
        self.assertNotContains(page, "/users/admin/list/")

    def test_malformed_session_target_is_discarded(self):
        client = Client()
        client.force_login(self.staff_admin)
        session = client.session
        session[VIEW_AS_SESSION_KEY] = "not-a-pk"
        session.save()

        response = client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(VIEW_AS_SESSION_KEY, client.session)
        self.assertEqual(response.wsgi_request.user.pk, self.staff_admin.pk)
        self.assertContains(response, "预览目标无效")
        self.assertIn("no-store", response["Cache-Control"])

    def test_stale_exit_response_is_not_cacheable(self):
        client = self.active_view_as()
        User.objects.filter(pk=self.member.pk).delete()

        response = client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("no-store", response["Cache-Control"])
        self.assertIn("private", response["Cache-Control"])

    def test_admin_logout_get_does_not_mutate_session_for_non_staff_target(self):
        """GET /admin/logout/ never reaches LogoutView while previewing a member.

        ``AdminSite.admin_view`` denies the non-staff effective user and
        short-circuits the logout path to the admin index
        (django/contrib/admin/sites.py:231), so the session - real principal
        plus preview - stays untouched.  No GET can quietly change privilege
        state while a preview is active.
        """
        client = self.active_view_as(self.member)
        response = client.get("/admin/logout/")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/admin/")
        self.assertEqual(client.session[VIEW_AS_SESSION_KEY], self.member.pk)
        self.assertIn("_auth_user_id", client.session)
        self.assertEqual(client.get("/admin/").status_code, 302)

    def test_logout_always_ends_the_session_and_the_preview(self):
        """Logging out is allowed and flushes the session: nothing stale survives.

        Django 4.2 still answers GET on ``LogoutView`` (deprecated, removed in
        5.0), so an admin-site logout while previewing a *staff* account ends
        both the preview and the real admin login.
        """
        staff_target = User.objects.create_user("staff-target", password="test-password", is_staff=True)
        client = self.active_view_as(staff_target)
        response = client.get("/admin/logout/")
        self.assertIn(response.status_code, (200, 302))
        self.assertNotIn(VIEW_AS_SESSION_KEY, client.session)
        self.assertNotIn("_auth_user_id", client.session)
        self.assertEqual(client.get("/").status_code, 302)

        client = self.active_view_as(self.member)
        self.assertEqual(client.post(reverse("logout")).status_code, 302)
        self.assertNotIn(VIEW_AS_SESSION_KEY, client.session)
        self.assertNotIn("_auth_user_id", client.session)

    # --- session isolation -------------------------------------------------------
    def test_view_as_does_not_affect_other_sessions(self):
        admin_client = self.active_view_as()
        other_client = Client()
        other_client.force_login(self.member)

        response = other_client.get("/")
        self.assertFalse(response.wsgi_request.view_as.active)
        self.assertEqual(response.wsgi_request.user.pk, self.member.pk)
        self.assertNotContains(response, "账户视角")

        response = other_client.post(
            reverse("profile"),
            {"display_name": "Member Self", "lab_group": "算法", "is_dashboard_visible": "on"},
        )
        self.assertEqual(response.status_code, 302)
        self.member.profile.refresh_from_db()
        self.assertEqual(self.member.profile.display_name, "Member Self")

        self.assertEqual(admin_client.session[VIEW_AS_SESSION_KEY], self.member.pk)
