"""Daily token quota: limits, aggregation, admin editing and enforcement."""

from datetime import timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from api_keys.models import APIKey
from api_keys.rotation import RotationError, rotate_member_key
from core.models import SiteSettings
from quota import enforcement
from quota.models import DailyQuotaUsage
from quota.services import (
    default_daily_limit,
    is_suspended,
    member_quota,
    resolve_limit,
    set_all_daily_quota,
    today_usage,
)
from usage.models import UsageLog
from usage.services import get_model_pricing
from dashboard.models import ModelPricing

User = get_user_model()

MANAGEMENT = {
    "CLIPROXY_MANAGEMENT_BASE_URL": "http://127.0.0.1:8317",
    "CLIPROXY_MANAGEMENT_KEY": "test-key",
}


def make_key(user, token="labsk-aaaa", status=APIKey.STATUS_ACTIVE):
    return APIKey.objects.create(
        user=user,
        name="default",
        status=status,
        token_plaintext=token,
        **APIKey.build_token_metadata(token),
    )


_seq = {"n": 0}


def log_usage(user, tokens, when=None):
    _seq["n"] += 1
    return UsageLog.objects.create(
        request_id=f"req-{_seq['n']}-{user.id}",
        user=user,
        model_name="deepseek/deepseek-v4.1-flash",
        prompt_tokens=tokens,
        completion_tokens=0,
        total_tokens=tokens,
        request_time=when or timezone.now(),
        estimated_cost=Decimal("0"),
    )


class QuotaServiceTests(TestCase):
    def setUp(self):
        self.member = User.objects.create_user("member", password="pw")

    def test_member_without_own_value_follows_the_site_default(self):
        self.assertIsNone(self.member.profile.daily_token_quota)
        self.assertEqual(resolve_limit(self.member.profile), 200_000_000)
        self.assertEqual(default_daily_limit(), 200_000_000)

    def test_own_value_overrides_the_default(self):
        self.member.profile.daily_token_quota = 5_000_000
        self.member.profile.save()
        self.assertEqual(resolve_limit(self.member.profile), 5_000_000)

    def test_today_usage_sums_only_today_and_per_user(self):
        other = User.objects.create_user("other", password="pw")
        log_usage(self.member, 100)
        log_usage(self.member, 50)
        log_usage(other, 7)
        log_usage(self.member, 999, when=timezone.now() - timedelta(days=2))
        usage = today_usage()
        self.assertEqual(usage[self.member.id], 150)
        self.assertEqual(usage[other.id], 7)

    def test_member_quota_reports_over_and_remaining(self):
        self.member.profile.daily_token_quota = 100
        self.member.profile.save()
        log_usage(self.member, 120)
        data = member_quota(self.member)
        self.assertEqual(data["limit"], 100)
        self.assertEqual(data["used"], 120)
        self.assertEqual(data["remaining"], 0)
        self.assertTrue(data["over"])
        self.assertFalse(data["blocked"])

    def test_set_all_daily_quota_overwrites_every_profile(self):
        other = User.objects.create_user("other2", password="pw")
        other.profile.daily_token_quota = 3
        other.profile.save()
        updated = set_all_daily_quota(50_000_000)
        self.assertGreaterEqual(updated, 2)
        self.member.profile.refresh_from_db()
        other.profile.refresh_from_db()
        self.assertEqual(self.member.profile.daily_token_quota, 50_000_000)
        self.assertEqual(other.profile.daily_token_quota, 50_000_000)
        self.assertEqual(default_daily_limit(), 50_000_000)


class AdminQuotaEditTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user("admin", password="pw", is_staff=True)
        self.member = User.objects.create_user("member", password="pw")
        self.member.profile.display_name = "万翔"
        self.member.profile.save()

    def test_member_list_shows_quota_inputs(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse("admin-user-list"))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn(f'name="quota_{self.member.id}"', html)
        self.assertIn("今日 / 每日额度", html)
        self.assertIn("跟随默认", html)

    def test_admin_can_set_one_members_quota_in_millions(self):
        self.client.force_login(self.admin)
        response = self.client.post(
            reverse("admin-user-quota", args=[self.member.id]), {f"quota_{self.member.id}": "12.5"}
        )
        self.assertEqual(response.status_code, 302)
        self.member.profile.refresh_from_db()
        self.assertEqual(self.member.profile.daily_token_quota, 12_500_000)

    def test_blank_submission_clears_the_override(self):
        self.member.profile.daily_token_quota = 1
        self.member.profile.save()
        self.client.force_login(self.admin)
        self.client.post(reverse("admin-user-quota", args=[self.member.id]), {f"quota_{self.member.id}": ""})
        self.member.profile.refresh_from_db()
        self.assertIsNone(self.member.profile.daily_token_quota)

    def test_invalid_number_keeps_the_old_value(self):
        self.member.profile.daily_token_quota = 9_000_000
        self.member.profile.save()
        self.client.force_login(self.admin)
        response = self.client.post(
            reverse("admin-user-quota", args=[self.member.id]), {f"quota_{self.member.id}": "abc"}
        )
        self.assertEqual(response.status_code, 302)
        self.member.profile.refresh_from_db()
        self.assertEqual(self.member.profile.daily_token_quota, 9_000_000)

    def test_non_admin_cannot_change_quotas(self):
        self.client.force_login(self.member)
        response = self.client.post(
            reverse("admin-user-quota", args=[self.member.id]), {f"quota_{self.member.id}": "1"}
        )
        self.assertEqual(response.status_code, 403)
        self.member.profile.refresh_from_db()
        self.assertIsNone(self.member.profile.daily_token_quota)


class SiteSettingsQuotaTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user("admin2", password="pw", is_staff=True)
        self.a = User.objects.create_user("a", password="pw")
        self.b = User.objects.create_user("b", password="pw")
        SiteSettings.load()

    def _post(self, **overrides):
        data = {
            "public_api_base_url": "",
            "query_trace_enabled": "",
            "fingerprint_enabled": "",
            "fingerprint_daily_limit": "",
            "fingerprint_admin_daily_limit": "",
            "fingerprint_cooldown_seconds": "",
            "fingerprint_concurrency": "",
            "lab_groups": "",
            "daily_quota_m": "200",
            "quota_enforcement": "block",
        }
        data.update(overrides)
        return self.client.post(reverse("admin-site-settings"), data)

    def test_default_is_two_hundred_million_tokens(self):
        self.assertEqual(SiteSettings.load().daily_token_quota_default, 200_000_000)

    def test_saving_stores_millions_as_tokens(self):
        self.client.force_login(self.admin)
        response = self._post(daily_quota_m="150")
        self.assertEqual(response.status_code, 302)
        settings_obj = SiteSettings.load()
        self.assertEqual(settings_obj.daily_token_quota_default, 150_000_000)
        # without the checkbox, per-member values are untouched
        self.a.profile.refresh_from_db()
        self.assertIsNone(self.a.profile.daily_token_quota)

    def test_apply_to_all_overwrites_member_values(self):
        self.a.profile.daily_token_quota = 1_000
        self.a.profile.save()
        self.client.force_login(self.admin)
        self._post(daily_quota_m="80", apply_to_all="on")
        self.a.profile.refresh_from_db()
        self.b.profile.refresh_from_db()
        self.assertEqual(self.a.profile.daily_token_quota, 80_000_000)
        self.assertEqual(self.b.profile.daily_token_quota, 80_000_000)

    def test_enforcement_mode_is_saved(self):
        self.client.force_login(self.admin)
        self._post(quota_enforcement="warn")
        self.assertEqual(SiteSettings.load().quota_enforcement, "warn")


@override_settings(**MANAGEMENT)
class EnforcementTests(TestCase):
    def setUp(self):
        self.member = User.objects.create_user("heavy", password="pw")
        self.key = make_key(self.member, token="labsk-heavy")
        self.member.profile.daily_token_quota = 100
        self.member.profile.save()

    def test_off_mode_touches_nothing(self):
        SiteSettings.objects.update_or_create(pk=1, defaults={"quota_enforcement": "off"})
        log_usage(self.member, 500)
        with patch("quota.enforcement.requests") as requests:
            stats = enforcement.enforce_daily_quota()
        self.assertEqual(stats["blocked"], 0)
        requests.get.assert_not_called()
        self.assertFalse(DailyQuotaUsage.objects.exists())

    def test_warn_mode_does_not_remove_the_key(self):
        SiteSettings.objects.update_or_create(pk=1, defaults={"quota_enforcement": "warn"})
        log_usage(self.member, 500)
        with patch("quota.enforcement.requests") as requests:
            stats = enforcement.enforce_daily_quota()
        self.assertEqual(stats["mode"], "warn")
        requests.get.assert_not_called()
        self.assertFalse(DailyQuotaUsage.objects.exists())

    def test_block_mode_removes_the_key_and_records_it(self):
        SiteSettings.objects.update_or_create(pk=1, defaults={"quota_enforcement": "block"})
        log_usage(self.member, 500)

        def fake_get(url, **kwargs):
            response = MagicMock()
            response.json.return_value = {"api-keys": ["other-key", "labsk-heavy"]}
            response.raise_for_status.return_value = None
            return response

        with patch("quota.enforcement.requests.get", side_effect=fake_get), patch(
            "quota.enforcement.requests.delete"
        ) as delete, patch("quota.enforcement.requests.patch") as patch_call:
            delete.return_value = MagicMock()
            stats = enforcement.enforce_daily_quota()

        self.assertEqual(stats["blocked"], 1)
        delete.assert_called_once()
        self.assertIn("labsk-heavy", delete.call_args[0][0])
        state = DailyQuotaUsage.objects.get(user=self.member)
        self.assertTrue(state.is_blocked)
        self.assertEqual(state.blocked_key, "labsk-heavy")
        self.assertEqual(state.tokens_used, 500)
        patch_call.assert_not_called()

    def test_raising_the_quota_restores_the_key(self):
        SiteSettings.objects.update_or_create(pk=1, defaults={"quota_enforcement": "block"})
        log_usage(self.member, 500)
        state = DailyQuotaUsage.objects.create(
            user=self.member, day=timezone.localdate(), tokens_used=500, limit=100,
            api_key=self.key, blocked_key="labsk-heavy", blocked_at=timezone.now(),
        )
        self.member.profile.daily_token_quota = 10_000
        self.member.profile.save()

        with patch("quota.enforcement.requests.get") as get, patch(
            "quota.enforcement.requests.patch"
        ) as patch_call:
            get.return_value = MagicMock(json=lambda: {"api-keys": []}, raise_for_status=lambda: None)
            patch_call.return_value = MagicMock()
            stats = enforcement.enforce_daily_quota()

        self.assertEqual(stats["restored"], 1)
        patch_call.assert_called_once()
        body = patch_call.call_args[1]["json"]
        self.assertEqual(body, {"old": "", "new": "labsk-heavy"})
        state.refresh_from_db()
        self.assertIsNotNone(state.restored_at)

    def test_already_present_key_is_not_added_twice(self):
        SiteSettings.objects.update_or_create(pk=1, defaults={"quota_enforcement": "block"})
        DailyQuotaUsage.objects.create(
            user=self.member, day=timezone.localdate(), tokens_used=500, limit=100,
            api_key=self.key, blocked_key="labsk-heavy", blocked_at=timezone.now(),
        )
        with patch("quota.enforcement.requests.get") as get, patch(
            "quota.enforcement.requests.patch"
        ) as patch_call:
            get.return_value = MagicMock(json=lambda: {"api-keys": ["labsk-heavy"]}, raise_for_status=lambda: None)
            enforcement.enforce_daily_quota()
        patch_call.assert_not_called()

    def test_transport_failure_does_not_raise(self):
        SiteSettings.objects.update_or_create(pk=1, defaults={"quota_enforcement": "block"})
        log_usage(self.member, 500)
        with patch("quota.enforcement.requests.get", side_effect=RuntimeError("boom")):
            stats = enforcement.enforce_daily_quota()
        self.assertEqual(stats["errors"], 1)
        self.assertFalse(DailyQuotaUsage.objects.exists())


class PricingAliasTests(TestCase):
    def test_provider_prefix_falls_back_to_the_bare_model_name(self):
        ModelPricing.objects.create(
            model_name="deepseek-v4.1-flash",
            prompt_price_per_million=Decimal("1.988"),
            completion_price_per_million=Decimal("2.982"),
            cached_price_per_million=Decimal("0.1988"),
        )
        prompt, completion, cached = get_model_pricing("deepseek/deepseek-v4.1-flash")
        self.assertEqual(prompt, Decimal("1.988"))
        self.assertEqual(completion, Decimal("2.982"))
        self.assertEqual(cached, Decimal("0.1988"))

    def test_unknown_model_still_costs_zero(self):
        self.assertEqual(get_model_pricing("nope/nothing"), (Decimal("0"), Decimal("0"), Decimal("0")))


class ApplyMarketPricesTests(TestCase):
    def test_command_writes_the_market_table(self):
        from django.core.management import call_command

        call_command("apply_market_prices")
        row = ModelPricing.objects.get(model_name="claude-opus-5-5")
        self.assertEqual(row.prompt_price_per_million, Decimal("106.500000"))
        self.assertEqual(row.completion_price_per_million, Decimal("532.500000"))
        self.assertEqual(ModelPricing.objects.filter(model_name="deepseek-v4.1-flash").count(), 1)

    def test_dry_run_writes_nothing(self):
        from django.core.management import call_command

        call_command("apply_market_prices", "--dry-run")
        self.assertFalse(ModelPricing.objects.exists())

@override_settings(**MANAGEMENT)
class SuspendedKeyGuardTests(TestCase):
    """A suspended member must not be able to get a working key back."""

    def setUp(self):
        self.member = User.objects.create_user("blocked", password="pw")
        self.key = make_key(self.member, token="labsk-blocked")
        self.member.profile.daily_token_quota = 100
        self.member.profile.save()
        DailyQuotaUsage.objects.create(
            user=self.member,
            day=timezone.localdate(),
            tokens_used=500,
            limit=100,
            api_key=self.key,
            blocked_key="labsk-blocked",
            blocked_at=timezone.now(),
        )

    def test_suspension_is_visible_to_callers(self):
        self.assertTrue(is_suspended(self.member))

    def test_member_cannot_rotate_while_suspended(self):
        with patch("api_keys.rotation.push_key_rotation_to_cliproxy") as push:
            with self.assertRaises(RotationError):
                rotate_member_key(self.member, self.key.pk)
        push.assert_not_called()
        self.key.refresh_from_db()
        self.assertEqual(self.key.token_plaintext, "labsk-blocked")

    def test_restore_publishes_the_key_the_member_currently_holds(self):
        # A portal-side rebind during the suspension replaces the row in place
        # and is deliberately kept away from CPA; the restore must follow it.
        metadata = APIKey.build_token_metadata("labsk-rebound")
        self.key.token_plaintext = "labsk-rebound"
        self.key.token_hash = metadata["token_hash"]
        self.key.token_prefix = metadata["token_prefix"]
        self.key.token_masked = metadata["token_masked"]
        self.key.save()
        self.member.profile.daily_token_quota = 10_000
        self.member.profile.save()

        with patch("quota.enforcement.requests.get") as get, patch(
            "quota.enforcement.requests.patch"
        ) as patch_call:
            get.return_value = MagicMock(json=lambda: {"api-keys": []}, raise_for_status=lambda: None)
            patch_call.return_value = MagicMock()
            stats = enforcement.enforce_daily_quota()

        self.assertEqual(stats["restored"], 1)
        self.assertEqual(patch_call.call_args.kwargs["json"], {"old": "", "new": "labsk-rebound"})
