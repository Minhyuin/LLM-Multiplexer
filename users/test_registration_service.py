"""Backend unit tests for invitation-code registration.

Run with the project's own test settings (PostgreSQL in CI):

    python manage.py test users.test_registration_service

The tests never touch CLIProxyAPI: ``users.registration.push_key_rotation_to_cliproxy``
is patched wherever the sync service is exercised.
"""
from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError
from django.test import TestCase
from django.utils import timezone

from api_keys.models import APIKey
from users.models import InvitationCode, Profile, RegistrationProvision
from users.registration import (
    REGISTRATION_KEY_PREFIX,
    REGISTRATION_KEY_RANDOM_LENGTH,
    generate_invitation_code,
    generate_registration_key,
    register_member,
    sync_registration_key,
    unique_invitation_code,
    username_from_display_name,
    username_matches_display_name,
)
from users.registration_forms import InvitationCodeForm, RegistrationForm

User = get_user_model()

STRONG_PASSWORD = "Str0ng-Pass-2026"
INVITATION = "TEST-TEAM-INVITE"


class RegistrationTestBase(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username="admin",
            password="AdminPass-2026",
            is_staff=True,
        )
        self.invitation = InvitationCode.objects.create(
            code=INVITATION,
            valid_days=7,
            created_by=self.admin,
        )

    def form_data(self, **overrides):
        data = {
            "display_name": "张三",
            "username": "ZhangSan",
            "invitation_code": INVITATION,
            "password1": STRONG_PASSWORD,
            "password2": STRONG_PASSWORD,
        }
        data.update(overrides)
        return data

    def service_data(self, **overrides):
        return self.form_data(**overrides)

    def register(self, **overrides):
        return register_member(self.service_data(**overrides))

    def subscribe_state(self):
        return (
            User.objects.count(),
            APIKey.objects.count(),
            RegistrationProvision.objects.count(),
            Profile.objects.count(),
        )


class InvitationCodeModelTests(RegistrationTestBase):
    def test_expiry_is_creation_time_plus_valid_days(self):
        for days in range(1, 8):
            before = timezone.now()
            invitation = InvitationCode.objects.create(
                code=f"DAY{days}",
                valid_days=days,
                created_by=self.admin,
            )
            after = timezone.now()
            self.assertGreaterEqual(invitation.expires_at, before + timedelta(days=days))
            self.assertLessEqual(invitation.expires_at, after + timedelta(days=days))

    def test_valid_days_outside_1_to_7_rejected(self):
        for days in (0, 8, -1):
            invitation = InvitationCode(code=f"BAD{days}", valid_days=days, created_by=self.admin)
            with self.assertRaises(ValidationError):
                invitation.full_clean(exclude=["expires_at"])

    def test_code_charset_and_length_validation(self):
        for code in ("bad code", "邀请码", "bad@code", "a" * 65, ""):
            invitation = InvitationCode(code=code, valid_days=7, created_by=self.admin)
            with self.assertRaises(ValidationError, msg=code):
                invitation.full_clean(exclude=["expires_at"])

        for code in ("ABC-123_x", "a", "a" * 64):
            invitation = InvitationCode(code=code, valid_days=7, created_by=self.admin)
            invitation.full_clean(exclude=["expires_at"])

    def test_code_is_unique(self):
        duplicate = InvitationCode(code=INVITATION, valid_days=7, created_by=self.admin)
        with self.assertRaises(ValidationError):
            duplicate.full_clean(exclude=["expires_at"])

    def test_expiry_cannot_be_extended(self):
        invitation = InvitationCode.objects.create(code="EXTEND", valid_days=2, created_by=self.admin)
        original = invitation.expires_at
        invitation.expires_at = original + timedelta(seconds=1)
        with self.assertRaises(ValidationError):
            invitation.save()
        invitation.refresh_from_db()
        self.assertEqual(invitation.expires_at, original)

    def test_valid_days_cannot_change_after_creation(self):
        invitation = InvitationCode.objects.create(code="FROZEN", valid_days=2, created_by=self.admin)
        invitation.valid_days = 7
        with self.assertRaises(ValidationError):
            invitation.save()
        invitation.refresh_from_db()
        self.assertEqual(invitation.valid_days, 2)

    def test_early_stop_and_expiry_determine_valid(self):
        invitation = InvitationCode.objects.create(code="STOPME", valid_days=7, created_by=self.admin)
        self.assertTrue(invitation.is_valid)
        self.assertTrue(invitation.is_usable)
        invitation.is_active = False
        invitation.save(update_fields=["is_active"])
        self.assertFalse(invitation.is_valid)

        expired = InvitationCode.objects.create(code="EXPIREME", valid_days=1, created_by=self.admin)
        InvitationCode.objects.filter(pk=expired.pk).update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )
        expired.refresh_from_db()
        self.assertFalse(expired.is_valid)

    def test_creation_never_accepts_caller_supplied_past_expiry(self):
        invitation = InvitationCode.objects.create(
            code="PASTEXP",
            valid_days=1,
            expires_at=timezone.now() - timedelta(days=30),
            created_by=self.admin,
        )
        self.assertGreater(invitation.expires_at, timezone.now())

    def test_is_valid_is_the_same_property_as_is_usable(self):
        self.assertIs(InvitationCode.is_valid, InvitationCode.is_usable)


class RegistrationFormTests(RegistrationTestBase):
    def test_valid_form_keeps_explicit_pinyin_username(self):
        form = RegistrationForm(self.form_data())
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["username"], "ZhangSan")

    def test_blank_username_is_derived_from_chinese_name(self):
        form = RegistrationForm(self.form_data(username=""))
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["username"], "ZhangSan")

    def test_each_syllable_is_capitalised_canonically(self):
        for name, expected in (
            ("薄汉博", "BoHanBo"),
            ("黄沛玥", "HuangPeiYue"),
            ("黄海", "HuangHai"),
            ("张三", "ZhangSan"),
        ):
            form = RegistrationForm(self.form_data(display_name=name, username=""))
            self.assertTrue(form.is_valid(), (name, form.errors))
            self.assertEqual(form.cleaned_data["username"], expected)

    def test_legacy_mixed_case_username_is_rejected(self):
        for name, username in (("薄汉博", "BoHanbo"), ("黄沛玥", "HuangPeiyue")):
            form = RegistrationForm(self.form_data(display_name=name, username=username))
            self.assertFalse(form.is_valid(), username)
            self.assertIn("username", form.errors)

    def test_alternate_polyphonic_reading_is_still_accepted(self):
        form = RegistrationForm(self.form_data(display_name="薄汉博", username="BaoHanBo"))
        self.assertTrue(form.is_valid(), form.errors)

    def test_display_name_must_be_chinese(self):
        for name in ("John Smith", "12345", "张", "张A三", ""):
            form = RegistrationForm(self.form_data(display_name=name))
            self.assertFalse(form.is_valid(), name)
            self.assertIn("display_name", form.errors)

    def test_unicode_middle_dot_name_is_allowed(self):
        form = RegistrationForm(
            self.form_data(display_name="买买提·买买提", username="MaiMaiTiMaiMaiTi")
        )
        self.assertTrue(form.is_valid(), form.errors)

    def test_username_format_is_enforced(self):
        for username in ("zhangsan", "张三", "Zhang San", "ZhangSan1", "A"):
            form = RegistrationForm(self.form_data(username=username))
            self.assertFalse(form.is_valid(), username)
            self.assertIn("username", form.errors)

    def test_username_must_match_display_name(self):
        form = RegistrationForm(self.form_data(display_name="张三", username="LiSi"))
        self.assertFalse(form.is_valid())
        self.assertIn("username", form.errors)

    @patch("users.registration._readings_for_char", return_value=())
    def test_unknown_reading_fails_closed_for_arbitrary_username(self, _readings):
        # Canonical derivation still succeeds through lazy_pinyin, but the
        # defensive per-character branch must reject a different username
        # instead of accepting anything when pypinyin has no heteronym data.
        self.assertTrue(username_matches_display_name("张三", "ZhangSan"))
        self.assertFalse(username_matches_display_name("张三", "LiSi"))
        form = RegistrationForm(self.form_data(display_name="张三", username="LiSi"))
        self.assertFalse(form.is_valid())
        self.assertIn("username", form.errors)

    def test_overlong_auto_derived_username_reports_username_error(self):
        form = RegistrationForm(self.form_data(display_name="张" * 50, username=""))
        self.assertFalse(form.is_valid())
        self.assertIn("username", form.errors)
        self.assertIn("64", str(form.errors["username"]))

    def test_polyphonic_reading_is_accepted(self):
        canonical = username_from_display_name("单雄信")
        for username in (canonical, "ShanXiongXin", "DanXiongXin"):
            self.assertTrue(username_matches_display_name("单雄信", username), username)
            form = RegistrationForm(
                self.form_data(display_name="单雄信", username=username)
            )
            self.assertTrue(form.is_valid(), (username, form.errors))

    def test_password_confirmation_and_validators(self):
        mismatch = RegistrationForm(
            self.form_data(password2=STRONG_PASSWORD + "x")
        )
        self.assertFalse(mismatch.is_valid())
        self.assertIn("password2", mismatch.errors)

        for weak in ("12345678", "abc"):
            form = RegistrationForm(self.form_data(password1=weak, password2=weak))
            self.assertFalse(form.is_valid(), weak)
            self.assertIn("password1", form.errors)

    def test_duplicate_username_is_case_insensitive(self):
        User.objects.create_user(username="lisi", password=STRONG_PASSWORD)
        form = RegistrationForm(self.form_data(display_name="李四", username="LiSi"))
        self.assertFalse(form.is_valid())
        self.assertIn("username", form.errors)

    def test_inactive_or_expired_invitation_rejected(self):
        self.invitation.is_active = False
        self.invitation.save(update_fields=["is_active"])
        self.assertFalse(RegistrationForm(self.form_data()).is_valid())

        InvitationCode.objects.filter(pk=self.invitation.pk).update(
            is_active=True,
            expires_at=timezone.now() - timedelta(seconds=1),
        )
        form = RegistrationForm(self.form_data())
        self.assertFalse(form.is_valid())
        self.assertIn("invitation_code", form.errors)

    def test_unknown_invitation_rejected(self):
        form = RegistrationForm(self.form_data(invitation_code="NOPE"))
        self.assertFalse(form.is_valid())
        self.assertIn("invitation_code", form.errors)

    def test_extra_privilege_fields_are_ignored(self):
        data = self.form_data(is_staff="True", is_superuser="True", role=Profile.ROLE_ADMIN)
        form = RegistrationForm(data)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertNotIn("is_staff", form.cleaned_data)
        self.assertNotIn("role", form.cleaned_data)
        user = register_member(form.cleaned_data)
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)
        self.assertEqual(user.profile.role, Profile.ROLE_USER)

    def test_invitation_form_generates_random_code_when_blank(self):
        form = InvitationCodeForm({"code": "", "valid_days": "3"})
        self.assertTrue(form.is_valid(), form.errors)
        invitation = form.save(commit=False)
        self.assertIsNone(invitation.expires_at)  # computed in save()
        invitation.created_by = self.admin
        invitation.save()
        self.assertRegex(invitation.code, r"^[A-Z0-9]{24}$")
        self.assertEqual(invitation.valid_days, 3)
        delta = invitation.expires_at - timezone.now()
        self.assertTrue(timedelta(days=2, hours=23) < delta <= timedelta(days=3))

    def test_invitation_form_rejects_days_outside_range(self):
        for days in ("0", "8", "1.5", "abc"):
            form = InvitationCodeForm({"code": "", "valid_days": days})
            self.assertFalse(form.is_valid(), days)
            self.assertIn("valid_days", form.errors)

    def test_invitation_form_fields_exclude_creator_and_expiry(self):
        self.assertEqual(list(InvitationCodeForm().fields), ["code", "valid_days"])
        for name in ("display_name", "username", "invitation_code", "password1", "password2"):
            widget = RegistrationForm().fields[name].widget
            self.assertIn("form-control", widget.attrs.get("class", ""), name)
            self.assertIn("autocomplete", widget.attrs, name)

    def test_random_invitation_codes_are_unique_uppercase_alphanumeric(self):
        first = unique_invitation_code()
        second = unique_invitation_code()
        self.assertNotEqual(first, second)
        for code in (first, second):
            self.assertRegex(code, r"^[A-Z0-9]{24}$")
            self.assertRegex(generate_invitation_code(), r"^[A-Z0-9]{24}$")


class RegisterMemberServiceTests(RegistrationTestBase):
    def test_registers_ordinary_active_member_with_pending_key(self):
        user = self.register()

        self.assertTrue(user.is_active)
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)
        self.assertTrue(user.check_password(STRONG_PASSWORD))
        self.assertNotEqual(user.password, STRONG_PASSWORD)
        self.assertEqual(user.profile.display_name, "张三")
        self.assertEqual(user.profile.role, Profile.ROLE_USER)

        key = user.api_keys.get()
        self.assertEqual(key.status, APIKey.STATUS_PENDING)
        self.assertEqual(key.source, APIKey.SOURCE_PORTAL)
        self.assertEqual(key.name, "API 密钥")
        self.assertEqual(key.token_hash, APIKey.hash_token(key.token_plaintext))
        self.assertRegex(
            key.token_plaintext,
            rf"^{REGISTRATION_KEY_PREFIX}[A-Za-z0-9]{{{REGISTRATION_KEY_RANDOM_LENGTH}}}ZhangSan$",
        )

        provision = RegistrationProvision.objects.get(user=user)
        self.assertEqual(provision.invitation, self.invitation)
        self.assertEqual(provision.api_key, key)
        self.assertIsNone(provision.synced_at)
        self.assertEqual(provision.attempts, 0)
        self.assertEqual(self.invitation.registrations.count(), 1)

    def test_key_random_part_is_exactly_sixteen_alphanumerics(self):
        user = self.register()
        token = user.api_keys.get().token_plaintext
        random_part = token[len(REGISTRATION_KEY_PREFIX):-len("ZhangSan")]
        self.assertEqual(len(random_part), 16)
        self.assertTrue(random_part.isalnum())

    def test_generated_keys_do_not_repeat(self):
        keys = {generate_registration_key("ZhangSan") for _ in range(50)}
        self.assertEqual(len(keys), 50)

    def test_one_invitation_can_be_reused_until_expiry(self):
        first = self.register(display_name="张三", username="ZhangSan")
        second = self.register(display_name="李四", username="LiSi")
        self.assertNotEqual(first.pk, second.pk)
        self.assertEqual(self.invitation.registrations.count(), 2)
        self.assertEqual(
            set(self.invitation.registrations.values_list("user_id", flat=True)),
            {first.pk, second.pk},
        )

    def test_invalid_invitation_raises_validation_error_without_writes(self):
        before = self.subscribe_state()
        with self.assertRaises(ValidationError):
            self.register(invitation_code="DOESNOTEXIST")
        self.assertEqual(self.subscribe_state(), before)

    def test_expired_invitation_raises_validation_error(self):
        InvitationCode.objects.filter(pk=self.invitation.pk).update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )
        with self.assertRaisesMessage(ValidationError, "邀请码已过期"):
            self.register()
        self.assertFalse(User.objects.filter(username="ZhangSan").exists())

    def test_inactive_invitation_raises_validation_error(self):
        self.invitation.is_active = False
        self.invitation.save(update_fields=["is_active"])
        with self.assertRaisesMessage(ValidationError, "邀请码已停用"):
            self.register()
        self.assertFalse(User.objects.filter(username="ZhangSan").exists())

    def test_duplicate_username_case_insensitive_rolls_back(self):
        self.register(display_name="张三", username="ZhangSan")
        before = self.subscribe_state()
        with self.assertRaisesMessage(ValidationError, "该用户名已被使用"):
            self.register(display_name="李四", username="zhangsan")
        self.assertEqual(self.subscribe_state(), before)

    def test_provision_failure_rolls_back_user_profile_and_key(self):
        before = self.subscribe_state()
        with patch(
            "users.registration.RegistrationProvision.objects.create",
            side_effect=IntegrityError("provision failed"),
        ):
            with self.assertRaises(IntegrityError):
                self.register()
        self.assertEqual(self.subscribe_state(), before)
        self.assertFalse(User.objects.filter(username="ZhangSan").exists())

    def test_key_creation_failure_rolls_back_user_and_profile(self):
        before = self.subscribe_state()
        with patch(
            "users.registration.APIKey.objects.create",
            side_effect=IntegrityError("key failed"),
        ):
            with self.assertRaises(IntegrityError):
                self.register()
        self.assertEqual(self.subscribe_state(), before)

    def test_privilege_flags_in_cleaned_data_are_ignored(self):
        data = self.service_data(
            is_staff=True,
            is_superuser=True,
            role=Profile.ROLE_ADMIN,
            profile={"role": Profile.ROLE_ADMIN},
        )
        user = register_member(data)
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)
        self.assertEqual(user.profile.role, Profile.ROLE_USER)

    def test_blank_username_is_derived(self):
        user = self.register(username="")
        self.assertEqual(user.username, "ZhangSan")

    def test_service_rejects_malformed_username_and_missing_fields(self):
        for username in ("Zhang San", "张三", "A"):
            with self.assertRaises(ValidationError):
                self.register(username=username)
        with self.assertRaises(ValidationError):
            self.register(display_name="")
        with self.assertRaises(ValidationError):
            self.register(password1="", password2="")

    def test_invitation_code_is_case_sensitive(self):
        with self.assertRaises(ValidationError):
            self.register(invitation_code=INVITATION.lower())


class SyncRegistrationKeyTests(RegistrationTestBase):
    def setUp(self):
        super().setUp()
        self.user = self.register()
        self.key = self.user.api_keys.get()
        self.provision = RegistrationProvision.objects.get(user=self.user)

    def test_success_marks_key_active_and_records_sync(self):
        token = self.key.token_plaintext
        with patch("users.registration.push_key_rotation_to_cliproxy") as push:
            self.assertTrue(sync_registration_key(self.user.pk))
        push.assert_called_once_with(token, token)

        self.key.refresh_from_db()
        self.provision.refresh_from_db()
        self.assertEqual(self.key.status, APIKey.STATUS_ACTIVE)
        self.assertIsNotNone(self.provision.synced_at)
        self.assertEqual(self.provision.attempts, 1)
        self.assertIsNotNone(self.provision.last_attempt_at)
        self.assertEqual(self.key.token_plaintext, token)

    def test_second_sync_is_idempotent_and_does_not_push_again(self):
        with patch("users.registration.push_key_rotation_to_cliproxy"):
            self.assertTrue(sync_registration_key(self.user.pk))
        with patch("users.registration.push_key_rotation_to_cliproxy") as push:
            self.assertTrue(sync_registration_key(self.user.pk))
        push.assert_not_called()
        self.provision.refresh_from_db()
        self.assertEqual(self.provision.attempts, 1)

    def test_failure_keeps_key_pending_and_is_retryable(self):
        with patch(
            "users.registration.push_key_rotation_to_cliproxy",
            side_effect=ConnectionError("cpa down"),
        ) as push:
            with self.assertLogs("users.registration", level="WARNING") as logs:
                self.assertFalse(sync_registration_key(self.user.pk))
        push.assert_called_once()
        self.key.refresh_from_db()
        self.provision.refresh_from_db()
        self.assertEqual(self.key.status, APIKey.STATUS_PENDING)
        self.assertIsNone(self.provision.synced_at)
        self.assertEqual(self.provision.attempts, 1)
        self.assertIsNotNone(self.provision.last_attempt_at)
        self.assertNotIn(self.key.token_plaintext, "\n".join(logs.output))

        with patch("users.registration.push_key_rotation_to_cliproxy") as retry:
            self.assertTrue(sync_registration_key(self.user.pk))
        retry.assert_called_once_with(self.key.token_plaintext, self.key.token_plaintext)
        self.key.refresh_from_db()
        self.provision.refresh_from_db()
        self.assertEqual(self.key.status, APIKey.STATUS_ACTIVE)
        self.assertIsNotNone(self.provision.synced_at)
        self.assertEqual(self.provision.attempts, 2)

    def test_exception_text_containing_key_is_not_logged(self):
        with patch(
            "users.registration.push_key_rotation_to_cliproxy",
            side_effect=RuntimeError(f"request body had {self.key.token_plaintext}"),
        ):
            with self.assertLogs("users.registration", level="WARNING") as logs:
                self.assertFalse(sync_registration_key(self.user.pk))
        self.assertNotIn(self.key.token_plaintext, "\n".join(logs.output))

    def test_disabled_user_is_never_activated(self):
        User.objects.filter(pk=self.user.pk).update(is_active=False)
        with patch("users.registration.push_key_rotation_to_cliproxy") as push:
            self.assertFalse(sync_registration_key(self.user.pk))
        push.assert_not_called()
        self.key.refresh_from_db()
        self.provision.refresh_from_db()
        self.assertEqual(self.key.status, APIKey.STATUS_PENDING)
        self.assertIsNone(self.provision.synced_at)
        self.assertEqual(self.provision.attempts, 0)

    def test_revoked_key_is_never_reactivated(self):
        self.key.status = APIKey.STATUS_REVOKED
        self.key.save(update_fields=["status"])
        with patch("users.registration.push_key_rotation_to_cliproxy") as push:
            self.assertFalse(sync_registration_key(self.user.pk))
        push.assert_not_called()
        self.key.refresh_from_db()
        self.assertEqual(self.key.status, APIKey.STATUS_REVOKED)
        self.provision.refresh_from_db()
        self.assertIsNone(self.provision.synced_at)

    def test_deleted_user_has_no_task(self):
        user_id = self.user.pk
        self.user.delete()
        self.assertFalse(RegistrationProvision.objects.filter(user_id=user_id).exists())
        self.assertFalse(sync_registration_key(user_id))

    def test_missing_provision_returns_false(self):
        self.assertFalse(sync_registration_key(999999))

    def test_syncing_one_user_does_not_touch_another(self):
        other = self.register(display_name="李四", username="LiSi")
        other_key = other.api_keys.get()

        with patch("users.registration.push_key_rotation_to_cliproxy") as push:
            self.assertTrue(sync_registration_key(self.user.pk))
        push.assert_called_once_with(self.key.token_plaintext, self.key.token_plaintext)
        other_key.refresh_from_db()
        self.assertEqual(other_key.status, APIKey.STATUS_PENDING)
        self.assertIsNone(RegistrationProvision.objects.get(user=other).synced_at)

        with patch("users.registration.push_key_rotation_to_cliproxy") as push:
            self.assertTrue(sync_registration_key(other.pk))
        push.assert_called_once_with(other_key.token_plaintext, other_key.token_plaintext)
        self.assertNotEqual(other_key.token_plaintext, self.key.token_plaintext)

    def test_success_preserves_key_secret_and_metadata(self):
        token = self.key.token_plaintext
        digest = self.key.token_hash
        prefix = self.key.token_prefix
        with patch("users.registration.push_key_rotation_to_cliproxy"):
            sync_registration_key(self.user.pk)
        self.key.refresh_from_db()
        self.assertEqual(self.key.token_plaintext, token)
        self.assertEqual(self.key.token_hash, digest)
        self.assertEqual(self.key.token_prefix, prefix)


class ManagementCommandTests(RegistrationTestBase):
    def test_create_invitation_creates_code_with_creator(self):
        out = StringIO()
        call_command(
            "create_invitation",
            code="DEPLOYCODE",
            days=3,
            admin="admin",
            stdout=out,
        )
        output = out.getvalue()
        self.assertIn("DEPLOYCODE", output)
        self.assertIn("days=3", output)
        self.assertNotIn(REGISTRATION_KEY_PREFIX, output)

        invitation = InvitationCode.objects.get(code="DEPLOYCODE")
        self.assertEqual(invitation.created_by, self.admin)
        self.assertEqual(invitation.valid_days, 3)
        self.assertTrue(invitation.is_active)
        delta = invitation.expires_at - timezone.now()
        self.assertTrue(timedelta(days=2, hours=23) < delta <= timedelta(days=3))

    def test_create_invitation_is_idempotent_and_never_extends(self):
        call_command("create_invitation", code="KEEPWINDOW", days=5, admin="admin", stdout=StringIO())
        invitation = InvitationCode.objects.get(code="KEEPWINDOW")
        original_expiry = invitation.expires_at

        call_command("create_invitation", code="KEEPWINDOW", days=1, admin="admin", stdout=StringIO())
        invitation.refresh_from_db()
        self.assertEqual(invitation.valid_days, 5)
        self.assertEqual(invitation.expires_at, original_expiry)

    def test_create_invitation_does_not_reactivate_stopped_code(self):
        call_command(
            "create_invitation", code="STOPPEDCODE", days=5, admin="admin", stdout=StringIO()
        )
        InvitationCode.objects.filter(code="STOPPEDCODE").update(is_active=False)
        call_command(
            "create_invitation", code="STOPPEDCODE", days=5, admin="admin", stdout=StringIO()
        )
        self.assertFalse(InvitationCode.objects.get(code="STOPPEDCODE").is_active)

    def test_create_invitation_rejects_invalid_days_and_missing_admin(self):
        with self.assertRaises(CommandError):
            call_command("create_invitation", code="BAD", days=0, admin="admin", stdout=StringIO())
        with self.assertRaises(CommandError):
            call_command("create_invitation", code="BAD", days=8, admin="admin", stdout=StringIO())
        with self.assertRaises(CommandError):
            call_command("create_invitation", code="BAD", days=3, admin="ghost", stdout=StringIO())

        plain = User.objects.create_user(username="plain", password=STRONG_PASSWORD)
        self.assertFalse(plain.is_staff)
        with self.assertRaises(CommandError):
            call_command("create_invitation", code="BAD", days=3, admin="plain", stdout=StringIO())

    def test_create_invitation_generates_code_when_omitted(self):
        call_command("create_invitation", days=2, admin="admin", stdout=StringIO())
        invitation = InvitationCode.objects.exclude(code__in=[INVITATION]).get()
        self.assertRegex(invitation.code, r"^[A-Z0-9]{24}$")

    def test_sync_command_drains_pending_tasks_and_skips_disabled(self):
        active_user = self.register(display_name="张三", username="ZhangSan")
        disabled_user = self.register(display_name="李四", username="LiSi")
        User.objects.filter(pk=disabled_user.pk).update(is_active=False)

        out = StringIO()
        with patch("users.registration.push_key_rotation_to_cliproxy") as push:
            call_command("sync_registration_keys", stdout=out)
        output = out.getvalue()
        self.assertIn("成功 1", output)
        self.assertIn("跳过 0", output)
        self.assertNotIn(REGISTRATION_KEY_PREFIX, output)

        self.assertEqual(active_user.api_keys.get().status, APIKey.STATUS_ACTIVE)
        self.assertIsNotNone(RegistrationProvision.objects.get(user=active_user).synced_at)
        self.assertEqual(disabled_user.api_keys.get().status, APIKey.STATUS_PENDING)
        self.assertIsNone(RegistrationProvision.objects.get(user=disabled_user).synced_at)
        # Only the enabled user's key was pushed.
        pushed = [call.args[0] for call in push.call_args_list]
        self.assertEqual(pushed, [active_user.api_keys.get().token_plaintext])

    def test_sync_command_retries_after_transient_failure(self):
        user = self.register()
        out = StringIO()
        with patch(
            "users.registration.push_key_rotation_to_cliproxy",
            side_effect=ConnectionError("down"),
        ):
            call_command("sync_registration_keys", stdout=out)
        self.assertIn("失败 1", out.getvalue())
        self.assertEqual(user.api_keys.get().status, APIKey.STATUS_PENDING)

        out = StringIO()
        with patch("users.registration.push_key_rotation_to_cliproxy"):
            call_command("sync_registration_keys", stdout=out)
        self.assertIn("成功 1", out.getvalue())
        self.assertEqual(user.api_keys.get().status, APIKey.STATUS_ACTIVE)

    def test_sync_command_respects_limit(self):
        self.register(display_name="张三", username="ZhangSan")
        self.register(display_name="李四", username="LiSi")
        with patch("users.registration.push_key_rotation_to_cliproxy"):
            call_command("sync_registration_keys", limit=1, stdout=StringIO())
        self.assertEqual(
            RegistrationProvision.objects.filter(synced_at__isnull=False).count(), 1
        )
        self.assertEqual(
            RegistrationProvision.objects.filter(synced_at__isnull=True).count(), 1
        )

    def test_limit_is_applied_after_disabled_and_revoked_are_filtered_out(self):
        # These two older rows would consume a naive [:limit] budget forever.
        disabled_user = self.register(display_name="李四", username="LiSi")
        User.objects.filter(pk=disabled_user.pk).update(is_active=False)
        revoked_user = self.register(display_name="王五", username="WangWu")
        revoked_key = revoked_user.api_keys.get()
        revoked_key.status = APIKey.STATUS_REVOKED
        revoked_key.save(update_fields=["status"])

        first = self.register(display_name="张三", username="ZhangSan")
        second = self.register(display_name="赵六", username="ZhaoLiu")

        out = StringIO()
        with patch("users.registration.push_key_rotation_to_cliproxy") as push:
            call_command("sync_registration_keys", limit=2, stdout=out)

        self.assertIn("成功 2", out.getvalue())
        self.assertIn("跳过 0", out.getvalue())
        self.assertEqual(push.call_count, 2)
        for user in (first, second):
            self.assertEqual(user.api_keys.get().status, APIKey.STATUS_ACTIVE)
            self.assertIsNotNone(RegistrationProvision.objects.get(user=user).synced_at)
        self.assertEqual(disabled_user.api_keys.get().status, APIKey.STATUS_PENDING)
        self.assertIsNone(RegistrationProvision.objects.get(user=disabled_user).synced_at)
        self.assertEqual(revoked_key.status, APIKey.STATUS_REVOKED)
        self.assertIsNone(RegistrationProvision.objects.get(user=revoked_user).synced_at)
