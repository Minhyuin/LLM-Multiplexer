"""HTTP acceptance tests for invite signup and administrator-only publishing."""
from datetime import timedelta
from unittest.mock import Mock, patch

import requests
from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from api_keys.models import APIKey
from users.models import InvitationCode, RegistrationProvision


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class RegistrationViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user("admin", password="test-only-password", is_staff=True)
        cls.invitation = InvitationCode.objects.create(code="TEAM-TEST-INVITE", valid_days=7, created_by=cls.admin)

    def payload(self, **overrides):
        data = {
            "display_name": "张三", "username": "ZhangSan", "invitation_code": self.invitation.code,
            "lab_group": "算法",
            "password1": "Test-Only-Strong-Pass!93", "password2": "Test-Only-Strong-Pass!93",
        }
        data.update(overrides)
        return data

    def test_public_registration_and_login_link(self):
        response = self.client.get(reverse("register"))
        self.assertContains(response, "邀请码")
        self.assertContains(response, "ZhangSan")
        self.assertContains(response, "中文姓名")
        self.assertContains(response, "实验室分组")
        self.assertIn("no-store", response["Cache-Control"])
        self.assertContains(self.client.get(reverse("login")), reverse("register"))
        self.assertNotContains(response, self.invitation.code)
        self.assertEqual(self.client.put(reverse("register")).status_code, 405)

    def test_registration_requires_a_known_lab_group(self):
        missing = self.client.post(reverse("register"), self.payload(lab_group=""))
        self.assertEqual(missing.status_code, 200)
        self.assertContains(missing, "这个字段是必填项")
        unknown = self.client.post(reverse("register"), self.payload(lab_group="不存在"))
        self.assertEqual(unknown.status_code, 200)
        self.assertContains(unknown, "选择一个有效的选项")
        self.assertFalse(User.objects.filter(username="ZhangSan").exists())

    def test_registration_requires_csrf(self):
        client = Client(enforce_csrf_checks=True)
        self.assertEqual(client.post(reverse("register"), self.payload()).status_code, 403)
        self.assertFalse(User.objects.filter(username="ZhangSan").exists())

    @patch("api_keys.cpa_sync.requests.patch")
    def test_signup_logs_in_creates_key_and_ignores_forged_privileges(self, push):
        response = self.client.post(reverse("register"), self.payload(is_staff="1", is_superuser="1", role="admin"))
        self.assertRedirects(response, reverse("my-quota"), fetch_redirect_response=False)
        user = User.objects.get(username="ZhangSan")
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)
        self.assertEqual(user.profile.role, "user")
        self.assertEqual(user.profile.display_name, "张三")
        self.assertTrue(user.check_password(self.payload()["password1"]))
        self.assertEqual(int(self.client.session["_auth_user_id"]), user.pk)
        key = user.api_keys.get()
        self.assertRegex(key.token_plaintext, r"^pioneer_sk_[A-Za-z0-9]{16}ZhangSan$")
        self.assertEqual(key.status, APIKey.STATUS_ACTIVE)
        self.assertEqual(push.call_args.kwargs["json"], {"old": key.token_plaintext, "new": key.token_plaintext})
        self.assertIsNotNone(RegistrationProvision.objects.get(user=user).synced_at)
        self.assertContains(self.client.get(reverse("my-quota")), key.token_plaintext)
        self.assertEqual(Client().get(reverse("my-quota")).status_code, 302)
        # A refresh/double click after successful signup only redirects the signed-in user.
        self.client.post(reverse("register"), self.payload())
        self.assertEqual(user.api_keys.count(), 1)
        self.assertEqual(push.call_count, 1)

    @patch("api_keys.cpa_sync.requests.patch")
    def test_invalid_and_expired_invites_create_nothing(self, push):
        response = self.client.post(reverse("register"), self.payload(invitation_code="NOT-AN-INVITE"))
        self.assertEqual(response.status_code, 200)
        InvitationCode.objects.filter(pk=self.invitation.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
        response = self.client.post(reverse("register"), self.payload())
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(username="ZhangSan").exists())
        self.assertFalse(APIKey.objects.exists())
        self.assertFalse(RegistrationProvision.objects.exists())
        push.assert_not_called()

    @patch("api_keys.cpa_sync.requests.patch", side_effect=requests.ConnectionError("upstream-private-secret"))
    def test_failed_sync_is_durable_pending_and_does_not_leak_key_or_error(self, push):
        response = self.client.post(reverse("register"), self.payload())
        self.assertEqual(response.status_code, 302)
        key = APIKey.objects.get(user__username="ZhangSan")
        self.assertEqual(key.status, APIKey.STATUS_PENDING)
        self.assertIsNone(RegistrationProvision.objects.get(api_key=key).synced_at)
        quota = self.client.get(reverse("my-quota"))
        self.assertContains(quota, "自动重试")
        self.assertNotContains(quota, key.token_plaintext)
        self.assertNotContains(quota, "upstream-private-secret")
        keys = self.client.get(reverse("my-api-keys"))
        self.assertContains(keys, "正在同步")
        self.assertNotContains(keys, "待审批")
        self.assertNotContains(keys, key.token_plaintext)

    @patch("api_keys.cpa_sync.requests.patch")
    def test_invite_is_multi_use_and_duplicate_user_gets_no_extra_key(self, push):
        self.client.post(reverse("register"), self.payload())
        other = Client()
        self.assertEqual(other.post(reverse("register"), self.payload(display_name="李四", username="LiSi")).status_code, 302)
        self.assertEqual(RegistrationProvision.objects.filter(invitation=self.invitation).count(), 2)
        duplicate = Client().post(reverse("register"), self.payload())
        self.assertEqual(duplicate.status_code, 200)
        self.assertEqual(APIKey.objects.count(), 2)
        self.assertEqual(push.call_count, 2)


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class InvitationManagementTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user("admin", is_staff=True)
        cls.member = User.objects.create_user("member")
        cls.role_admin = User.objects.create_user("role-admin")
        cls.role_admin.profile.role = "admin"
        cls.role_admin.profile.save()
        cls.invitation = InvitationCode.objects.create(code="EXISTING-CODE", valid_days=3, created_by=cls.admin)

    def test_anonymous_and_members_cannot_read_or_publish_codes(self):
        url = reverse("admin-invitation-list")
        self.assertEqual(self.client.get(url).status_code, 302)
        self.client.force_login(self.member)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.client.post(url, {"code": "FORGED", "valid_days": 7}).status_code, 403)
        self.assertEqual(InvitationCode.objects.count(), 1)

    def test_admin_publishes_a_code_with_server_controlled_creator_and_expiry(self):
        self.client.force_login(self.admin)
        before = timezone.now()
        response = self.client.post(reverse("admin-invitation-list"), {
            "code": "NEW-CODE", "valid_days": "7", "created_by": self.member.pk,
            "expires_at": "2099-01-01", "is_active": False,
        })
        self.assertRedirects(response, reverse("admin-invitation-list"), fetch_redirect_response=False)
        code = InvitationCode.objects.get(code="NEW-CODE")
        self.assertEqual(code.created_by, self.admin)
        self.assertEqual(code.valid_days, 7)
        self.assertGreaterEqual(code.expires_at, before + timedelta(days=7))
        self.assertLessEqual(code.expires_at, timezone.now() + timedelta(days=7))
        self.assertTrue(code.is_active)
        page = self.client.get(reverse("admin-invitation-list"))
        self.assertContains(page, "NEW-CODE")
        self.assertContains(page, "停用")

    def test_whole_days_only_and_seven_day_ceiling(self):
        self.client.force_login(self.admin)
        for days in [0, -1, 8, "1.5", "abc"]:
            with self.subTest(days=days):
                response = self.client.post(reverse("admin-invitation-list"), {"code": "BAD-DURATION", "valid_days": days})
                self.assertEqual(response.status_code, 200)
                self.assertFalse(InvitationCode.objects.filter(code="BAD-DURATION").exists())

    def test_role_admin_can_generate_a_random_code(self):
        self.client.force_login(self.role_admin)
        response = self.client.post(reverse("admin-invitation-list"), {"code": "", "valid_days": 1})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(InvitationCode.objects.filter(created_by=self.role_admin).count(), 1)

    def test_republishing_same_code_does_not_extend_it(self):
        self.client.force_login(self.admin)
        expiry = self.invitation.expires_at
        self.assertEqual(self.client.post(reverse("admin-invitation-list"), {"code": self.invitation.code, "valid_days": 7}).status_code, 200)
        self.invitation.refresh_from_db()
        self.assertEqual(self.invitation.expires_at, expiry)

    def test_revoke_requires_admin_csrf_and_post(self):
        url = reverse("admin-invitation-revoke", args=[self.invitation.pk])
        self.client.force_login(self.member)
        self.assertEqual(self.client.post(url).status_code, 403)
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(url).status_code, 405)
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.admin)
        self.assertEqual(csrf_client.post(url).status_code, 403)
        self.assertEqual(csrf_client.post(reverse("admin-invitation-list"), {"code": "CSRF", "valid_days": 7}).status_code, 403)
        self.assertEqual(self.client.post(url).status_code, 302)
        self.invitation.refresh_from_db()
        self.assertFalse(self.invitation.is_active)
        self.assertFalse(self.invitation.is_valid)

    def test_readonly_preview_cannot_publish_invites(self):
        self.client.force_login(self.admin)
        self.client.post(reverse("view-as-start", args=[self.member.pk]))
        response = self.client.post(reverse("admin-invitation-list"), {"code": "PREVIEW-CODE", "valid_days": 7})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(InvitationCode.objects.filter(code="PREVIEW-CODE").exists())
