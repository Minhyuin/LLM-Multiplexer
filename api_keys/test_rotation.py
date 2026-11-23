from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from api_keys.member_tokens import IMPORT_KEY_RANDOM_LENGTH, MEMBER_KEY_RANDOM_LENGTH, build_account_name, generate_member_key
from api_keys.models import APIKey, APIKeyAlias
from api_keys.rotation import RotationError, rotate_member_key


User = get_user_model()


def make_key(user, plaintext="sk-oldkeyZhangSan", status=APIKey.STATUS_ACTIVE):
    metadata = APIKey.build_token_metadata(plaintext)
    return APIKey.objects.create(
        user=user,
        name="API 密钥",
        token_hash=metadata["token_hash"],
        token_prefix=metadata["token_prefix"],
        token_masked=metadata["token_masked"],
        token_plaintext=plaintext,
        source=APIKey.SOURCE_CLIPROXY,
        status=status,
    )


class MemberTokenFormatTests(TestCase):
    def test_special_cases_and_pinyin_suffix(self):
        self.assertEqual(build_account_name("黄海"), "HuangHai")
        self.assertEqual(build_account_name("薄汉博"), "BoHanbo")
        self.assertEqual(build_account_name("黄沛玥"), "HuangPeiyue")
        self.assertEqual(build_account_name("ZhangSan"), "ZhangSan")
        self.assertEqual(build_account_name("张三"), "ZhangSan")

    def test_rotation_key_keeps_prefix_and_lengthens_random_segment(self):
        key = generate_member_key("ZhangSan", MEMBER_KEY_RANDOM_LENGTH)
        imported = generate_member_key("ZhangSan", IMPORT_KEY_RANDOM_LENGTH)
        self.assertRegex(key, r"^sk-[A-Za-z0-9]{16}ZhangSan$")
        self.assertRegex(imported, r"^sk-[A-Za-z0-9]{7}ZhangSan$")
        self.assertNotEqual(key, imported)


class MemberSelfRotateTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("ZhangSan", password="test-password")
        self.user.profile.display_name = "张三"
        self.user.profile.save()
        self.key = make_key(self.user)
        self.client.force_login(self.user)
        self.url = reverse("rotate-my-api-key", args=[self.key.id])

    def test_member_rotation_syncs_cpa_before_replacing_the_stored_key(self):
        with patch("api_keys.rotation.push_key_rotation_to_cliproxy") as push:
            response = self.client.post(self.url, {"next": "quota", "token_plaintext": "sk-memberTyped"}, follow=True)
        push.assert_called_once()
        old, new = push.call_args.args
        self.assertEqual(old, "sk-oldkeyZhangSan")
        self.assertRegex(new, r"^sk-[A-Za-z0-9]{16}ZhangSan$")
        self.assertNotEqual(new, "sk-memberTyped")
        self.key.refresh_from_db()
        self.assertEqual(self.key.token_plaintext, new)
        self.assertEqual(self.key.token_hash, APIKey.hash_token(new))
        self.assertIsNotNone(self.key.token_rotated_at)
        alias = APIKeyAlias.objects.get()
        self.assertEqual(alias.user, self.user)
        self.assertEqual(alias.token_plaintext, old)
        self.assertEqual(alias.token_hash, APIKey.hash_token(old))
        self.assertIn("成员自助轮换", alias.note)
        self.assertContains(response, "密钥已更换，旧密钥立即失效")
        self.assertContains(response, new)
        self.assertContains(response, "最近更新")
        self.assertNotContains(response, "尚未轮换")

    def test_cpa_failure_leaves_portal_key_unchanged(self):
        with patch("api_keys.rotation.push_key_rotation_to_cliproxy", side_effect=ConnectionError("down")):
            response = self.client.post(self.url, follow=True)
        self.assertContains(response, "同步代理失败，密钥未更换")
        self.key.refresh_from_db()
        self.assertEqual(self.key.token_plaintext, "sk-oldkeyZhangSan")
        self.assertIsNone(self.key.token_rotated_at)
        self.assertFalse(APIKeyAlias.objects.exists())

    def test_portal_save_failure_rolls_the_proxy_key_back(self):
        calls = []

        def record(old, new):
            calls.append((old, new))

        with patch("api_keys.rotation.push_key_rotation_to_cliproxy", side_effect=record):
            with patch("api_keys.rotation._write_rotation", side_effect=RuntimeError("db")):
                response = self.client.post(self.url, follow=True)
        self.assertContains(response, "门户保存失败，已恢复旧密钥")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1], (calls[0][1], calls[0][0]))
        self.key.refresh_from_db()
        self.assertEqual(self.key.token_plaintext, "sk-oldkeyZhangSan")
        self.assertFalse(APIKeyAlias.objects.exists())

    def test_proxy_rollback_failure_is_reported_and_portal_stays_on_old_key(self):
        def push(old, new):
            if old != "sk-oldkeyZhangSan":
                raise ConnectionError("rollback down")

        with patch("api_keys.rotation.push_key_rotation_to_cliproxy", side_effect=push):
            with patch("api_keys.rotation._write_rotation", side_effect=RuntimeError("db")):
                response = self.client.post(self.url, follow=True)
        self.assertContains(response, "门户保存失败，且代理回滚失败")
        self.key.refresh_from_db()
        self.assertEqual(self.key.token_plaintext, "sk-oldkeyZhangSan")

    def test_second_rotation_inside_ten_minutes_is_rejected(self):
        with patch("api_keys.rotation.push_key_rotation_to_cliproxy") as push:
            self.client.post(self.url)
            first = APIKey.objects.get(pk=self.key.id).token_plaintext
            response = self.client.post(self.url, follow=True)
        self.assertEqual(push.call_count, 1)
        self.assertContains(response, "10 分钟内只能更换一次密钥")
        self.assertEqual(APIKey.objects.get(pk=self.key.id).token_plaintext, first)
        self.assertContains(response, "disabled")

    def test_rotation_is_allowed_again_after_the_cooldown(self):
        self.key.token_rotated_at = timezone.now() - timedelta(minutes=11)
        self.key.save(update_fields=["token_rotated_at"])
        with patch("api_keys.rotation.push_key_rotation_to_cliproxy") as push:
            self.client.post(self.url)
        push.assert_called_once()
        self.key.refresh_from_db()
        self.assertNotEqual(self.key.token_plaintext, "sk-oldkeyZhangSan")

    def test_cannot_rotate_someone_elses_revoked_or_blank_key(self):
        other = User.objects.create_user("other-member")
        other_key = make_key(other, "sk-otherkeyOther")
        revoked = make_key(self.user, "sk-revokedZhangSan", status=APIKey.STATUS_REVOKED)
        # Only one live key per member is allowed, so retire the fixture key
        # before adding the blank one.
        self.key.status = APIKey.STATUS_REVOKED
        self.key.save(update_fields=["status"])
        blank = make_key(self.user, "sk-blankZhangSan")
        blank.token_plaintext = ""
        blank.save(update_fields=["token_plaintext"])
        with patch("api_keys.rotation.push_key_rotation_to_cliproxy") as push:
            missing = self.client.post(reverse("rotate-my-api-key", args=[other_key.id]))
            revoked_response = self.client.post(reverse("rotate-my-api-key", args=[revoked.id]), follow=True)
            blank_response = self.client.post(reverse("rotate-my-api-key", args=[blank.id]), follow=True)
        self.assertEqual(missing.status_code, 404)
        self.assertContains(revoked_response, "只能更换启用中的密钥")
        self.assertContains(blank_response, "没有可同步的明文")
        push.assert_not_called()

    def test_admins_are_forbidden_and_get_is_not_allowed(self):
        staff = User.objects.create_user("staff-rotator", is_staff=True)
        role_admin = User.objects.create_user("role-rotator")
        role_admin.profile.role = "admin"
        role_admin.profile.save()
        self.client.force_login(staff)
        self.assertEqual(self.client.post(self.url).status_code, 403)
        self.client.force_login(role_admin)
        self.assertEqual(self.client.post(self.url).status_code, 403)
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(self.url).status_code, 405)
        with self.assertRaises(RotationError):
            rotate_member_key(staff, self.key.id)

    def test_keys_page_shows_update_time_and_posts_back_to_itself(self):
        page = self.client.get(reverse("my-api-keys"))
        self.assertContains(page, "最近更新")
        self.assertContains(page, "尚未轮换")
        self.assertContains(page, 'name="next" value="keys"')
        with patch("api_keys.rotation.push_key_rotation_to_cliproxy"):
            response = self.client.post(self.url, {"next": "keys"}, follow=True)
        self.assertEqual(response.redirect_chain[-1][0], reverse("my-api-keys"))
        self.assertContains(response, "请立即保存新 Token")

    def test_quota_page_hides_the_button_from_admins(self):
        self.assertContains(self.client.get(reverse("my-quota")), "更换密钥")
        staff = User.objects.create_user("quota-staff", is_staff=True)
        make_key(staff, "sk-staffkeyStaff")
        self.client.force_login(staff)
        page = self.client.get(reverse("my-quota"))
        self.assertNotContains(page, "更换密钥")
        self.assertContains(page, "最近更新")


class AdminCreateKeyTests(TestCase):
    """A key created from the admin page must reach the proxy, or it is dead."""

    def setUp(self):
        self.admin = User.objects.create_user("root-admin", password="pw", is_staff=True)
        self.member = User.objects.create_user("ZhangSan", password="pw")
        self.key = make_key(self.member, "sk-livekeyZhangSan")
        self.client.force_login(self.admin)

    def test_an_active_key_is_published_to_the_proxy(self):
        with patch("api_keys.views.push_key_rotation_to_cliproxy") as push:
            response = self.client.post(
                reverse("admin-api-key-create", args=[self.member.id]),
                {"name": "第二把", "status": APIKey.STATUS_ACTIVE, "note": ""},
                follow=True,
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(APIKey.objects.filter(user=self.member).count(), 2)
        push.assert_called_once()
        self.assertEqual(push.call_args[0][0], "")
        self.assertTrue(push.call_args[0][1].startswith("labsk-"))
        self.assertContains(response, "新密钥已写入 CPA")

    def test_a_key_that_is_not_live_is_not_published(self):
        with patch("api_keys.views.push_key_rotation_to_cliproxy") as push:
            self.client.post(
                reverse("admin-api-key-create", args=[self.member.id]),
                {"name": "备用", "status": APIKey.STATUS_PENDING, "note": ""},
            )
        push.assert_not_called()
        self.assertEqual(APIKey.objects.filter(user=self.member).count(), 2)

    def test_a_failed_publish_is_reported_but_keeps_the_key(self):
        with patch("api_keys.views.push_key_rotation_to_cliproxy", side_effect=RuntimeError("boom")):
            response = self.client.post(
                reverse("admin-api-key-create", args=[self.member.id]),
                {"name": "第三把", "status": APIKey.STATUS_ACTIVE, "note": ""},
                follow=True,
            )
        self.assertContains(response, "写入 CPA 失败")
        self.assertEqual(APIKey.objects.filter(user=self.member).count(), 2)
