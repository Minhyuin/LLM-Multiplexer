"""Offline behaviour tests for the fingerprints queue / quota state machine.

Everything here runs without a network: the owner-scoped model list lookup is
mocked (``fingerprints.transport.available_models`` only) and no challenge is
ever sent.  SQLite builds the schema, so these tests prove functional
behaviour; genuine row-lock behaviour needs PostgreSQL and lives in
``fingerprints/test_concurrency.py``.
"""
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from datetime import timezone as dt_timezone
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.utils import timezone

from api_keys.models import APIKey
from fingerprints import services
from fingerprints.config import UPSTREAM_COMMIT, FingerprintError
from fingerprints.models import (
    FingerprintCoordinator,
    FingerprintQuotaDay,
    FingerprintRun,
    FingerprintSample,
)

User = get_user_model()

MODEL = "gpt-text-1"
EMBEDDING_MODEL = "text-embedding-3-large"
DISABLED_MODEL = "legacy-text"
MODEL_POLICIES = {
    MODEL: {"enabled": True, "api": "chat_completions"},
    DISABLED_MODEL: {"enabled": False},
}

#: 2026-10-27 09:00 Asia/Shanghai.
NOW = datetime(2026, 10, 27, 1, 0, tzinfo=dt_timezone.utc)
TODAY = date(2026, 10, 27)


@contextmanager
def frozen(now):
    """Freeze ``timezone.now`` so quota days, cooldowns and history are exact."""
    with patch("django.utils.timezone.now", return_value=now):
        yield


def utc(*args):
    return datetime(*args, tzinfo=dt_timezone.utc)


_UNSET = object()


class FingerprintServiceTestCase(TestCase):
    """Shared fixtures: enabled feature, a configured model, owner keys."""

    def setUp(self):
        super().setUp()
        # Passwords are irrelevant here; the default PBKDF2 hasher dominates the
        # runtime of a suite that builds a few hundred users.
        hasher = override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
        hasher.enable()
        self.addCleanup(hasher.disable)
        # The singleton coordinator row is the admission/lease mutex.  A real
        # deployment seeds it during the controlled migration; tests must not
        # depend on that having happened yet.
        FingerprintCoordinator.objects.get_or_create(pk=1)
        self.model_calls = []
        self.model_grants = {}
        models = patch("fingerprints.transport.available_models", side_effect=self._available_models)
        models.start()
        self.addCleanup(models.stop)

    def _available_models(self, token):
        self.model_calls.append(token)
        return set(self.model_grants.get(token, {MODEL}))

    # --- fixture builders -------------------------------------------------------
    def make_user(self, username, *, is_staff=False, role=None, is_active=True):
        user = User.objects.create_user(username=username, password="test-password", is_staff=is_staff, is_active=is_active)
        if role is not None:
            user.profile.role = role
            user.profile.save()
        return user

    def make_key(self, user, *, name="主 Key", status=APIKey.STATUS_ACTIVE, expires_at=None, plaintext=None):
        raw = plaintext or APIKey.generate_token()
        meta = APIKey.build_token_metadata(raw)
        return APIKey.objects.create(user=user, name=name, token_hash=meta["token_hash"],
                                     token_prefix=meta["token_prefix"], token_masked=meta["token_masked"],
                                     token_plaintext=raw, status=status, expires_at=expires_at)

    def owner(self, username="member", **kwargs):
        """A member with one usable key; returns ``(user, key)``."""
        user = self.make_user(username, **kwargs)
        return user, self.make_key(user)

    def seed_quota(self, user, day=TODAY, *, used=_UNSET, reserved=_UNSET, last=_UNSET):
        defaults = {}
        if used is not _UNSET:
            defaults["used"] = used
        if reserved is not _UNSET:
            defaults["reserved"] = reserved
        if last is not _UNSET:
            defaults["last_submitted_at"] = last
        row, _ = FingerprintQuotaDay.objects.update_or_create(user=user, day=day, defaults=defaults)
        return row

    def counter(self, user, day=TODAY):
        return FingerprintQuotaDay.objects.get(user=user, day=day)

    def make_run(self, user, *, key=None, status=FingerprintRun.QUEUED, created=None, finished=None,
                 consumed=False, model=MODEL, quota_day=None, idempotency=None, queue_deadline=None,
                 lease=None, started=None, heartbeat=None, cancel_requested=False):
        """Fabricate a row without going through admission (controlled timestamps)."""
        key = key or self.make_key(user)
        created = created or timezone.now()
        run = FingerprintRun.objects.create(
            user=user, api_key=key, key_fingerprint=key.token_hash, key_label=f"{key.name} · {key.token_masked}",
            model_id=model, protocol="chat_completions", parameters={"api": "chat_completions"},
            upstream_commit=UPSTREAM_COMMIT, idempotency_key=idempotency or uuid.uuid4(), status=status,
            quota_day=quota_day or services.day_at(created), quota_consumed=consumed,
            queue_deadline=queue_deadline or created + timedelta(seconds=180), finished_at=finished,
            lease_token=lease, started_at=started, heartbeat_at=heartbeat,
            cancel_requested=cancel_requested)
        if created != timezone.now():
            FingerprintRun.objects.filter(pk=run.pk).update(created_at=created)
            run.refresh_from_db()
        return run

    def add_sample(self, run, ordinal=1, *, status="pending"):
        return FingerprintSample.objects.create(run=run, ordinal=ordinal, challenge_id=f"challenge-{ordinal}",
                                               expected_count=300, status=status)

    def dispatch(self, user, key, *, model=MODEL, idempotency=None):
        return services.create_run(user, key.pk, model, idempotency or uuid.uuid4())[0]

    def sample_result(self, **overrides):
        """Exactly the dict shape ``fingerprints.transport.sample()`` returns."""
        result = {"status": "complete", "valid": True, "number_count": 300, "finish_reason": "stop",
                  "duration_ms": 1200, "response_model": MODEL, "response_id": "resp-1",
                  "usage": {"input_tokens": 10, "output_tokens": 20, "complete": True},
                  "error_code": "", "error_message": ""}
        result.update(overrides)
        return result

    # --- assertions -------------------------------------------------------------
    def assertRejected(self, code, status, func, *args, **kwargs):
        with self.assertRaises(FingerprintError) as caught:
            func(*args, **kwargs)
        self.assertEqual(caught.exception.code, code, str(caught.exception))
        self.assertEqual(caught.exception.status, status, str(caught.exception))
        return caught.exception


@override_settings(FINGERPRINT_ENABLED=True, FINGERPRINT_MODEL_POLICIES=MODEL_POLICIES)
class OwnerKeyTests(FingerprintServiceTestCase):
    """Only a live plaintext key of the requesting account may be used."""

    def test_usable_keys_lists_only_owner_live_plaintext_keys(self):
        user, live = self.owner("member")
        self.make_key(user, name="revoked", status=APIKey.STATUS_REVOKED)
        self.make_key(user, name="expired", expires_at=timezone.now() - timedelta(minutes=1))
        no_plaintext = self.make_key(user, name="no-plaintext")
        APIKey.objects.filter(pk=no_plaintext.pk).update(token_plaintext="")
        # A key whose stored plaintext no longer hashes to token_hash is corrupt.
        corrupt = self.make_key(user, name="corrupt")
        APIKey.objects.filter(pk=corrupt.pk).update(token_hash=APIKey.hash_token("labsk-something-else"))
        other, other_key = self.owner("other")

        self.assertEqual([key.pk for key in services.usable_keys(user)], [live.pk])
        self.assertEqual([key.pk for key in services.usable_keys(other)], [other_key.pk])

    def test_validate_key_rejects_another_members_key(self):
        owner, _ = self.owner("member")
        _, foreign = self.owner("other")
        self.assertRejected("key_unavailable", 403, services.validate_key, owner.pk, foreign.pk)

    def test_validate_key_rejects_revoked_and_expired_keys(self):
        user, _ = self.owner("member")
        revoked = self.make_key(user, name="revoked", status=APIKey.STATUS_REVOKED)
        expired = self.make_key(user, name="expired", expires_at=timezone.now() - timedelta(seconds=1))
        self.assertRejected("key_unavailable", 403, services.validate_key, user.pk, revoked.pk)
        self.assertRejected("key_unavailable", 403, services.validate_key, user.pk, expired.pk)

    def test_validate_key_rejects_disabled_account(self):
        user, key = self.owner("member")
        User.objects.filter(pk=user.pk).update(is_active=False)
        self.assertRejected("account_disabled", 403, services.validate_key, user.pk, key.pk)

    def test_validate_key_detects_rotation_through_the_version_fingerprint(self):
        user, key = self.owner("member")
        stale = key.token_hash
        # A rotation keeps the row but replaces the secret and its hash.
        rotated = APIKey.generate_token()
        meta = APIKey.build_token_metadata(rotated)
        APIKey.objects.filter(pk=key.pk).update(token_plaintext=rotated, token_hash=meta["token_hash"])
        self.assertRejected("key_changed", 409, services.validate_key, user.pk, key.pk, stale)

        key.refresh_from_db()
        self.assertEqual(services.validate_key(user.pk, key.pk, key.token_hash).pk, key.pk)

    def test_validate_key_rejects_stored_plaintext_that_no_longer_matches_the_hash(self):
        user, key = self.owner("member")
        APIKey.objects.filter(pk=key.pk).update(token_plaintext="labsk-rewritten-without-rehash")
        # The check cannot tell a corrupt row from a rotation, so both surface as
        # key_changed.  What matters is that the key is refused rather than used.
        self.assertRejected("key_changed", 409, services.validate_key, user.pk, key.pk)

    def test_validate_key_rejects_missing_key(self):
        user, _ = self.owner("member")
        self.assertRejected("key_unavailable", 403, services.validate_key, user.pk, 999999)


@override_settings(FINGERPRINT_ENABLED=True, FINGERPRINT_MODEL_POLICIES=MODEL_POLICIES)
class AdminKeyChoiceTests(FingerprintServiceTestCase):
    """Admins own no key, so they may pin one active member key per round.  The
    round, its quota and its usage stay the admins; only the key (and therefore
    the upstream bill) belongs to the member."""

    def test_admin_lists_every_live_member_key_with_its_owner(self):
        admin = self.make_user("root-admin", is_staff=True)
        alice, alice_key = self.owner("alice")
        bob, bob_key = self.owner("bob")
        self.make_key(alice, name="revoked", status=APIKey.STATUS_REVOKED)
        disabled, disabled_key = self.owner("carol")
        User.objects.filter(pk=disabled.pk).update(is_active=False)

        keys = services.usable_keys(admin)
        self.assertEqual([(key.pk, key.user.username) for key in keys],
                         [(alice_key.pk, "alice"), (bob_key.pk, "bob")])
        self.assertNotIn(disabled_key.pk, [key.pk for key in keys])

    def test_a_member_still_lists_only_their_own_keys(self):
        alice, alice_key = self.owner("alice")
        self.owner("bob")
        self.assertEqual([key.pk for key in services.usable_keys(alice)], [alice_key.pk])

    def test_admin_choice_is_refused_for_a_revoked_member_key(self):
        self.make_user("root-admin", is_staff=True)
        alice, _ = self.owner("alice")
        revoked = self.make_key(alice, name="revoked", status=APIKey.STATUS_REVOKED)
        self.assertRejected("key_unavailable", 403, services.validate_key, None, revoked.pk)

    def test_admin_round_pins_the_members_key_and_still_charges_the_admin(self):
        admin = self.make_user("root-admin", is_staff=True)
        alice, key = self.owner("alice")

        with frozen(NOW):
            run, reused = services.create_run(admin, key.pk, MODEL, uuid.uuid4())
            self.assertFalse(reused)
            self.assertEqual((run.user_id, run.api_key_id), (admin.pk, key.pk))
            self.assertEqual(run.key_label, f"alice · {key.token_masked}")
            self.assertEqual(self.counter(admin).reserved, 1)
        self.assertFalse(FingerprintQuotaDay.objects.filter(user=alice).exists())

    def test_admin_sees_the_member_display_name_and_a_name_sorted_list(self):
        admin = self.make_user("root-admin", is_staff=True)
        alice, alice_key = self.owner("alice")
        alice.profile.display_name = "爱丽丝"
        alice.profile.save()
        bob, bob_key = self.owner("bob")
        bob.profile.display_name = "Bob Zhang"
        bob.profile.save()

        self.assertEqual([key.pk for key in services.usable_keys(admin)], [bob_key.pk, alice_key.pk])
        run, _ = services.create_run(admin, alice_key.pk, MODEL, uuid.uuid4())
        self.assertEqual(run.key_label, f"爱丽丝 · {alice_key.token_masked}")

    def test_a_member_without_a_display_name_falls_back_to_the_username(self):
        admin = self.make_user("root-admin", is_staff=True)
        carol, carol_key = self.owner("carol")

        self.assertEqual(services.owner_display_name(carol), "carol")
        run, _ = services.create_run(admin, carol_key.pk, MODEL, uuid.uuid4())
        self.assertEqual(run.key_label, f"carol · {carol_key.token_masked}")

    def test_worker_dispatches_with_the_pinned_member_key(self):
        admin = self.make_user("root-admin", is_staff=True)
        alice, key = self.owner("alice")
        run = self.make_run(admin, key=key, status=FingerprintRun.RUNNING, lease=str(uuid.uuid4()))
        self.add_sample(run, 1)
        self.seed_quota(admin, day=run.quota_day)

        self.assertEqual(services.dispatch_sample(run.pk, run.lease_token, 1), key.token_plaintext)
        run.refresh_from_db()
        self.assertTrue(run.quota_consumed)
        self.assertEqual(FingerprintQuotaDay.objects.get(user=admin, day=run.quota_day).used, 1)
        self.assertFalse(FingerprintQuotaDay.objects.filter(user=alice).exists())


@override_settings(FINGERPRINT_ENABLED=True, FINGERPRINT_MODEL_POLICIES=MODEL_POLICIES)
class AdmissionTests(FingerprintServiceTestCase):
    """create_run: feature switch, argument hygiene, owner-only model proof."""

    def test_disabled_feature_rejects_admission(self):
        user, key = self.owner()
        with override_settings(FINGERPRINT_ENABLED=False):
            self.assertRejected("disabled", 503, services.create_run, user, key.pk, MODEL, uuid.uuid4())
        self.assertFalse(FingerprintRun.objects.exists())

    def test_invalid_arguments_are_rejected_before_any_write(self):
        user, key = self.owner()
        for key_id in (True, "not-a-number", None, 1.5, 0, -1, 10**100):
            with self.subTest(key_id=key_id):
                self.assertRejected("invalid_request", 400, services.create_run, user, key_id, MODEL, uuid.uuid4())
        self.assertRejected("invalid_request", 400, services.create_run, user, key.pk, MODEL, "not-a-uuid")
        self.assertFalse(FingerprintRun.objects.exists())

    def test_model_policy_guards_the_request(self):
        user, key = self.owner()
        for model_id in ("", None, "bad\nmodel"):
            with self.subTest(model_id=model_id):
                self.assertRejected("invalid_model", 400, services.create_run, user, key.pk, model_id, uuid.uuid4())
        self.assertRejected("unsupported_model", 400, services.create_run, user, key.pk, EMBEDDING_MODEL, uuid.uuid4())
        self.assertRejected("unsupported_model", 400, services.create_run, user, key.pk, DISABLED_MODEL, uuid.uuid4())

    def test_model_must_be_available_to_the_owners_own_key(self):
        user, key = self.owner()
        self.model_grants[key.token_plaintext] = set()
        self.assertRejected("model_unavailable", 403, services.create_run, user, key.pk, MODEL, uuid.uuid4())
        # Exactly one lookup, with the requesting owner's key, and no fallback.
        self.assertEqual(self.model_calls, [key.token_plaintext])
        self.assertFalse(FingerprintRun.objects.exists())

    def test_another_members_key_catalog_is_never_used_as_a_fallback(self):
        owner, owner_key = self.owner("member")
        _, other_key = self.owner("other")
        # Only the *other* member's key is granted the model.
        self.model_grants[owner_key.token_plaintext] = set()
        self.assertRejected("model_unavailable", 403, services.create_run, owner, owner_key.pk, MODEL, uuid.uuid4())
        self.assertNotIn(other_key.token_plaintext, self.model_calls)
        self.assertRejected("key_unavailable", 403, services.create_run, owner, other_key.pk, MODEL, uuid.uuid4())

    def test_admission_records_the_owner_key_identity_without_copying_plaintext(self):
        user, key = self.owner()
        run = self.dispatch(user, key)
        self.assertEqual(run.api_key_id, key.pk)
        self.assertEqual(run.user_id, user.pk)
        self.assertEqual(run.key_fingerprint, key.token_hash)
        self.assertIn(key.token_masked, run.key_label)
        self.assertNotIn(key.token_plaintext, run.key_label)
        self.assertEqual(run.model_id, MODEL)
        self.assertEqual(run.protocol, "chat_completions")
        self.assertEqual(run.parameters["api"], "chat_completions")
        self.assertEqual(run.upstream_commit, UPSTREAM_COMMIT)
        self.assertEqual(run.status, FingerprintRun.QUEUED)
        self.assertFalse(run.quota_consumed)

    def test_repeated_click_with_the_same_idempotency_key_returns_the_same_run(self):
        user, key = self.owner()
        identity = uuid.uuid4()
        with frozen(NOW):
            first, first_reused = services.create_run(user, key.pk, MODEL, identity)
            second, second_reused = services.create_run(user, key.pk, MODEL, identity)

            self.assertFalse(first_reused)
            self.assertTrue(second_reused)
            self.assertEqual(first.pk, second.pk)
            self.assertEqual(FingerprintRun.objects.count(), 1)
            self.assertEqual(self.counter(user).reserved, 1)

    def test_one_active_run_per_member_short_circuits_a_second_start(self):
        user, key = self.owner()
        with frozen(NOW):
            first = self.dispatch(user, key)
            second, reused = services.create_run(user, key.pk, MODEL, uuid.uuid4())

            self.assertTrue(reused)
            self.assertEqual(first.pk, second.pk)
            self.assertEqual(FingerprintRun.objects.count(), 1)
            self.assertEqual(self.counter(user).reserved, 1)

    def test_database_constraints_enforce_idempotency_and_single_active_run(self):
        user, key = self.owner()
        identity = uuid.uuid4()
        first = self.make_run(user, key=key, idempotency=identity)
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.make_run(user, key=key, idempotency=identity)
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.make_run(user, key=key)
        # A terminal run never blocks the next round.
        FingerprintRun.objects.filter(pk=first.pk).update(status=FingerprintRun.COMPLETED,
                                                          finished_at=timezone.now())
        second = self.make_run(user, key=key)
        self.assertEqual(second.status, FingerprintRun.QUEUED)


@override_settings(FINGERPRINT_ENABLED=True, FINGERPRINT_MODEL_POLICIES=MODEL_POLICIES)
class QuotaTests(FingerprintServiceTestCase):
    """Per-member daily allowance on the Asia/Shanghai calendar day."""

    def test_default_allowance_is_three_rounds_for_members_and_twenty_for_admins(self):
        member, _ = self.owner("member")
        staff = self.make_user("staff", is_staff=True)
        role_admin = self.make_user("role-admin", role="admin")
        self.make_key(staff)
        self.make_key(role_admin)

        self.assertEqual(services.quota_for(member)["limit"], 3)
        self.assertEqual(services.quota_for(staff)["limit"], 20)
        self.assertEqual(services.quota_for(role_admin)["limit"], 20)

    def test_quota_day_follows_the_shanghai_calendar_across_utc_midnight(self):
        user, key = self.owner()
        late_utc = utc(2026, 10, 27, 15, 59)  # 23:59 in Shanghai
        early_utc = utc(2026, 10, 27, 16, 1)  # 00:01 next Shanghai day

        self.assertEqual(services.day_at(late_utc), date(2026, 10, 27))
        self.assertEqual(services.day_at(early_utc), date(2026, 10, 28))
        self.assertEqual(services.quota_for(user, late_utc)["day"], "2026-10-27")
        self.assertEqual(services.quota_for(user, early_utc)["day"], "2026-10-28")

        with frozen(early_utc):
            run = self.dispatch(user, key)
        self.assertEqual(run.quota_day, date(2026, 10, 28))

    def test_allowance_resets_on_the_next_shanghai_day(self):
        user, key = self.owner()
        with frozen(NOW):
            self.assertEqual(services.quota_for(user)["remaining"], 3)
            self.seed_quota(user, used=3, last=NOW - timedelta(seconds=120))

        next_day = NOW + timedelta(hours=24)
        with frozen(next_day):
            quota = services.quota_for(user)
            self.assertEqual(quota["day"], "2026-10-28")
            self.assertEqual((quota["used"], quota["reserved"], quota["remaining"]), (0, 0, 3))
            self.assertEqual(self.counter(user, TODAY).used, 3)  # history, not debt
            self.dispatch(user, key)
            self.assertEqual(services.quota_for(user)["remaining"], 2)

    def test_multiple_keys_share_the_members_allowance(self):
        user, first_key = self.owner()
        second_key = self.make_key(user, name="备用 Key")
        self.assertEqual(len(services.usable_keys(user)), 2)
        with frozen(NOW):
            self.seed_quota(user, used=1, last=NOW)
            self.assertEqual(services.quota_for(user)["remaining"], 2)
            # Switching keys neither resets the allowance nor the cooldown.
            self.assertRejected("cooldown", 429, services.create_run, user, second_key.pk, MODEL, uuid.uuid4())
            self.assertEqual(FingerprintRun.objects.count(), 0)
            self.assertEqual(self.counter(user).used, 1)

    def test_admission_reserves_without_consuming(self):
        user, key = self.owner()
        with frozen(NOW):
            run = self.dispatch(user, key)
            quota = services.quota_for(user)
        counter = self.counter(user)
        self.assertEqual((counter.used, counter.reserved), (0, 1))
        self.assertEqual((quota["used"], quota["reserved"], quota["remaining"]), (0, 1, 2))
        self.assertFalse(run.quota_consumed)

    def test_cancelling_before_any_dispatch_returns_the_reservation(self):
        user, key = self.owner()
        with frozen(NOW):
            run = self.dispatch(user, key)
            services.cancel_run(user, run.pk)
            quota = services.quota_for(user)
        counter = self.counter(user)
        self.assertEqual((counter.used, counter.reserved), (0, 0))
        self.assertEqual(quota["remaining"], 3)
        self.assertEqual(FingerprintRun.objects.get(pk=run.pk).status, FingerprintRun.CANCELLED)

    def test_only_a_dispatched_challenge_consumes_the_round(self):
        user, key = self.owner()
        with frozen(NOW):
            run = self.dispatch(user, key)
            claimed = services.claim_run()
            self.add_sample(run, 1)
            services.dispatch_sample(run.pk, claimed.lease_token, 1)
            counter = self.counter(user)
            self.assertEqual((counter.used, counter.reserved), (1, 0))
            self.assertTrue(FingerprintRun.objects.get(pk=run.pk).quota_consumed)

            # A failure after dispatch keeps the round charged.
            services.finish_run(run.pk, claimed.lease_token, FingerprintRun.FAILED, "upstream_error",
                                "上游返回错误，未自动重试。")
            counter = self.counter(user)
            self.assertEqual((counter.used, counter.reserved), (1, 0))
            self.assertEqual(services.quota_for(user)["remaining"], 2)

    def test_finishing_without_dispatch_refunds_the_reservation(self):
        user, key = self.owner()
        with frozen(NOW):
            run = self.dispatch(user, key)
            claimed = services.claim_run()
            services.finish_run(run.pk, claimed.lease_token, FingerprintRun.INTERRUPTED, "worker_lost", "执行进程中断。")
            counter = self.counter(user)
        self.assertEqual((counter.used, counter.reserved), (0, 0))
        self.assertEqual(services.quota_for(user)["remaining"], 3)

    def test_used_and_reserved_rounds_both_block_admission(self):
        user, key = self.owner()
        with frozen(NOW + timedelta(seconds=120)):
            self.seed_quota(user, used=3)
            self.assertRejected("daily_limit", 429, services.create_run, user, key.pk, MODEL, uuid.uuid4())

        other, other_key = self.owner("other")
        with frozen(NOW + timedelta(seconds=120)):
            self.seed_quota(other, reserved=3)
            self.assertRejected("daily_limit", 429, services.create_run, other, other_key.pk, MODEL, uuid.uuid4())
        self.assertFalse(FingerprintRun.objects.exists())

    def test_pruning_history_never_refunds_consumed_rounds(self):
        user, key = self.owner()
        yesterday = date(2026, 10, 26)
        with frozen(NOW - timedelta(hours=30)):
            run = self.dispatch(user, key)
            claimed = services.claim_run()
            self.add_sample(run, 1)
            services.dispatch_sample(run.pk, claimed.lease_token, 1)
            services.finish_run(run.pk, claimed.lease_token, FingerprintRun.COMPLETED)
            self.assertEqual(self.counter(user, yesterday).used, 1)

        with frozen(NOW):
            services.maintenance(NOW)
            self.assertFalse(FingerprintRun.objects.exists())
            self.assertEqual(self.counter(user, yesterday).used, 1)

    def test_next_allowed_at_reflects_the_cooldown(self):
        user, _ = self.owner()
        with frozen(NOW):
            self.assertIsNone(services.quota_for(user)["next_allowed_at"])
            self.seed_quota(user, last=NOW)
            self.assertEqual(services.quota_for(user)["next_allowed_at"], (NOW + timedelta(seconds=60)).isoformat())


@override_settings(FINGERPRINT_ENABLED=True, FINGERPRINT_MODEL_POLICIES=MODEL_POLICIES)
class CooldownTests(FingerprintServiceTestCase):
    def test_second_start_inside_sixty_seconds_is_refused(self):
        user, key = self.owner()
        with frozen(NOW):
            first = self.dispatch(user, key)
            services.finish_run(first.pk, None, FingerprintRun.FAILED)
            self.assertRejected("cooldown", 429, services.create_run, user, key.pk, MODEL, uuid.uuid4())
        self.assertEqual(FingerprintRun.objects.count(), 1)
        self.assertEqual(self.counter(user).reserved, 0)

    def test_start_is_allowed_again_after_the_cooldown(self):
        user, key = self.owner()
        with frozen(NOW):
            self.seed_quota(user, last=NOW - timedelta(seconds=61))
            self.dispatch(user, key)
        self.assertEqual(FingerprintRun.objects.count(), 1)
        self.assertEqual(self.counter(user).reserved, 1)

    def test_cooldown_is_independent_of_the_daily_reset(self):
        user, key = self.owner()
        last = utc(2026, 10, 27, 15, 59, 30)  # 23:59:30 Asia/Shanghai
        with frozen(utc(2026, 10, 27, 15, 59, 45)):
            self.seed_quota(user, day=date(2026, 10, 27), last=last)
        with frozen(utc(2026, 10, 27, 16, 0)):  # a new Shanghai day, 30s later
            self.assertEqual(services.quota_for(user)["remaining"], 3)
            self.assertRejected("cooldown", 429, services.create_run, user, key.pk, MODEL, uuid.uuid4())


@override_settings(FINGERPRINT_ENABLED=True, FINGERPRINT_MODEL_POLICIES=MODEL_POLICIES,
                   FINGERPRINT_QUEUE_LIMIT=20, FINGERPRINT_QUEUE_TIMEOUT=180)
class QueueTests(FingerprintServiceTestCase):
    def test_queue_limit_blocks_further_admission(self):
        with frozen(NOW):
            for index in range(20):
                user, key = self.owner(f"member-{index}")
                self.dispatch(user, key)
            extra, extra_key = self.owner("member-20")
            self.assertRejected("queue_full", 429, services.create_run, extra, extra_key.pk, MODEL, uuid.uuid4())
        self.assertEqual(FingerprintRun.objects.filter(status=FingerprintRun.QUEUED).count(), 20)

    def test_queue_deadline_is_three_minutes(self):
        user, key = self.owner()
        with frozen(NOW):
            run = self.dispatch(user, key)
        self.assertEqual(run.queue_deadline, NOW + timedelta(seconds=180))

    def test_expired_queue_entry_is_cancelled_and_refunded_without_dispatch(self):
        user, key = self.owner()
        with frozen(NOW):
            run = self.dispatch(user, key)
            self.add_sample(run, 1)

        with frozen(NOW + timedelta(seconds=181)):
            self.assertIsNone(services.claim_run())
            stored = FingerprintRun.objects.get(pk=run.pk)
            self.assertEqual(stored.status, FingerprintRun.CANCELLED)
            self.assertEqual(stored.error_code, "queue_timeout")
            self.assertFalse(stored.quota_consumed)
            self.assertEqual((self.counter(user).used, self.counter(user).reserved), (0, 0))
            # A never-dispatched sample is closed as cancelled, never replayed.
            self.assertEqual(stored.samples.get().status, "cancelled")

    def test_maintenance_expires_a_stale_queue_entry(self):
        user, key = self.owner()
        with frozen(NOW):
            run = self.dispatch(user, key)
        later = NOW + timedelta(seconds=400)
        with frozen(later):
            services.maintenance(later)
        self.assertEqual(FingerprintRun.objects.get(pk=run.pk).status, FingerprintRun.CANCELLED)
        self.assertEqual(self.counter(user).reserved, 0)


@override_settings(FINGERPRINT_ENABLED=True, FINGERPRINT_MODEL_POLICIES=MODEL_POLICIES, FINGERPRINT_CONCURRENCY=2)
class ClaimTests(FingerprintServiceTestCase):
    def test_claim_takes_the_oldest_queued_run_and_issues_a_lease(self):
        older_user, older_key = self.owner("older")
        newer_user, newer_key = self.owner("newer")
        with frozen(NOW - timedelta(seconds=20)):
            older = self.dispatch(older_user, older_key)
        with frozen(NOW - timedelta(seconds=10)):
            newer = self.dispatch(newer_user, newer_key)

        with frozen(NOW):
            claimed = services.claim_run()
        self.assertEqual(claimed.pk, older.pk)
        self.assertEqual(claimed.status, FingerprintRun.RUNNING)
        self.assertIsNotNone(claimed.lease_token)
        self.assertIsNotNone(claimed.started_at)
        self.assertIsNotNone(claimed.heartbeat_at)
        self.assertEqual(FingerprintRun.objects.get(pk=newer.pk).status, FingerprintRun.QUEUED)

    def test_only_two_runs_are_leased_at_once_across_the_whole_portal(self):
        with frozen(NOW):
            for index in range(3):
                user, key = self.owner(f"member-{index}")
                self.dispatch(user, key)

        with frozen(NOW + timedelta(seconds=1)):
            first = services.claim_run()
            second = services.claim_run()
            third = services.claim_run()

        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertIsNone(third)
        self.assertNotEqual(first.lease_token, second.lease_token)
        self.assertEqual(FingerprintRun.objects.filter(status=FingerprintRun.RUNNING).count(), 2)

    def test_claim_returns_nothing_when_nothing_is_queued_or_the_feature_is_off(self):
        self.assertIsNone(services.claim_run())
        user, key = self.owner()
        with frozen(NOW):
            self.dispatch(user, key)
        with override_settings(FINGERPRINT_ENABLED=False):
            self.assertIsNone(services.claim_run())
        self.assertEqual(FingerprintRun.objects.get().status, FingerprintRun.QUEUED)

    def test_claim_never_picks_an_entry_past_its_queue_deadline(self):
        user, key = self.owner()
        with frozen(NOW):
            run = self.dispatch(user, key)
        later = NOW + timedelta(seconds=181)
        with frozen(later):
            self.assertIsNone(services.claim_run())
        self.assertEqual(FingerprintRun.objects.get(pk=run.pk).status, FingerprintRun.CANCELLED)


@override_settings(FINGERPRINT_ENABLED=True, FINGERPRINT_MODEL_POLICIES=MODEL_POLICIES)
class CancelTests(FingerprintServiceTestCase):
    def test_cancel_of_a_queued_run_closes_pending_samples_and_refunds(self):
        user, key = self.owner()
        with frozen(NOW):
            run = self.dispatch(user, key)
            self.add_sample(run, 1)
            self.add_sample(run, 2)
            cancelled = services.cancel_run(user, run.pk)

        self.assertEqual(cancelled.status, FingerprintRun.CANCELLED)
        self.assertTrue(cancelled.cancel_requested)
        self.assertEqual(set(run.samples.values_list("status", flat=True)), {"cancelled"})
        self.assertEqual(self.counter(user).reserved, 0)

    def test_cancel_of_a_running_run_asks_the_worker_to_stop(self):
        user, key = self.owner()
        with frozen(NOW):
            run = self.dispatch(user, key)
            claimed = services.claim_run()
            self.add_sample(run, 1, status="dispatching")
            services.cancel_run(user, run.pk)
            stored = FingerprintRun.objects.get(pk=run.pk)
            self.assertEqual(stored.status, FingerprintRun.RUNNING)
            self.assertTrue(stored.cancel_requested)
            self.assertTrue(services.is_cancelled(stored.pk, claimed.lease_token))

            finished = services.finish_run(run.pk, claimed.lease_token, FingerprintRun.COMPLETED)
        self.assertEqual(finished.status, FingerprintRun.CANCELLED)
        self.assertEqual(finished.error_code, "cancelled")
        self.assertIn("计费", finished.error_message)
        # A dispatched challenge stays interrupted, never replayed.
        self.assertEqual(finished.samples.get().status, "interrupted")

    def test_cancel_is_owner_scoped(self):
        owner, key = self.owner("member")
        intruder, _ = self.owner("intruder")
        with frozen(NOW):
            run = self.dispatch(owner, key)
            self.assertRejected("not_found", 404, services.cancel_run, intruder, run.pk)
            self.assertRejected("not_found", 404, services.cancel_run, owner, uuid.uuid4())
        stored = FingerprintRun.objects.get(pk=run.pk)
        self.assertEqual(stored.status, FingerprintRun.QUEUED)
        self.assertFalse(stored.cancel_requested)

    def test_cancel_of_a_finished_run_changes_nothing(self):
        user, key = self.owner()
        with frozen(NOW):
            run = self.dispatch(user, key)
            services.finish_run(run.pk, None, FingerprintRun.COMPLETED)
            cancel_after = services.cancel_run(user, run.pk)
            again = services.finish_run(run.pk, None, FingerprintRun.FAILED, "late", "late")
        self.assertEqual(cancel_after.status, FingerprintRun.COMPLETED)
        self.assertEqual(again.status, FingerprintRun.COMPLETED)
        self.assertEqual(again.error_code, "")
        self.assertFalse(again.cancel_requested)

    def test_is_cancelled_detects_leases_cancel_flags_and_the_feature_switch(self):
        user, key = self.owner()
        with frozen(NOW):
            run = self.dispatch(user, key)
            claimed = services.claim_run()
        lease = claimed.lease_token
        self.assertFalse(services.is_cancelled(run.pk, lease))
        self.assertTrue(services.is_cancelled(run.pk, uuid.uuid4()))
        self.assertTrue(services.is_cancelled(uuid.uuid4(), lease))
        FingerprintRun.objects.filter(pk=run.pk).update(cancel_requested=True)
        self.assertTrue(services.is_cancelled(run.pk, lease))
        FingerprintRun.objects.filter(pk=run.pk).update(cancel_requested=False)
        with override_settings(FINGERPRINT_ENABLED=False):
            self.assertTrue(services.is_cancelled(run.pk, lease))


@override_settings(FINGERPRINT_ENABLED=True, FINGERPRINT_MODEL_POLICIES=MODEL_POLICIES,
                   FINGERPRINT_LEASE_SECONDS=30, FINGERPRINT_RUN_TIMEOUT=420)
class RecoveryTests(FingerprintServiceTestCase):
    """A crash ends a run; the unknown in-flight request is never replayed."""

    def _running_run(self, *, started=NOW, username="member"):
        user, key = self.owner(username)
        with frozen(started):
            run = self.dispatch(user, key)
            claimed = services.claim_run()
            self.add_sample(run, 1, status="dispatching")
            self.add_sample(run, 2)
        return user, key, run, claimed

    def test_stale_lease_is_interrupted_never_requeued(self):
        user, key, run, claimed = self._running_run()
        later = NOW + timedelta(seconds=31)
        with frozen(later):
            services.maintenance(later)
        stored = FingerprintRun.objects.get(pk=run.pk)
        self.assertEqual(stored.status, FingerprintRun.INTERRUPTED)
        self.assertEqual(stored.error_code, "worker_lost")
        self.assertIsNotNone(stored.finished_at)
        self.assertFalse(FingerprintRun.objects.filter(status=FingerprintRun.QUEUED).exists())
        self.assertEqual(dict(stored.samples.values_list("ordinal", "status")), {1: "interrupted", 2: "cancelled"})

    def test_heartbeat_keeps_a_live_run(self):
        user, key, run, claimed = self._running_run()
        later = NOW + timedelta(seconds=20)
        with frozen(later):
            services.heartbeat(run.pk, claimed.lease_token)
            services.maintenance(later)
        self.assertEqual(FingerprintRun.objects.get(pk=run.pk).status, FingerprintRun.RUNNING)

    def test_run_past_the_hard_execution_deadline_is_interrupted(self):
        user, key, run, claimed = self._running_run()
        later = NOW + timedelta(seconds=431)
        with frozen(later):
            services.heartbeat(run.pk, claimed.lease_token)
            services.maintenance(later)
        self.assertEqual(FingerprintRun.objects.get(pk=run.pk).status, FingerprintRun.INTERRUPTED)
        self.assertFalse(FingerprintRun.objects.filter(status=FingerprintRun.QUEUED).exists())

    def test_dispatch_marker_prevents_a_second_send(self):
        user, key, run, claimed = self._running_run()
        FingerprintSample.objects.filter(run=run, ordinal=1).update(status="pending")
        with frozen(NOW):
            services.dispatch_sample(run.pk, claimed.lease_token, 1)
            self.assertEqual((self.counter(user).used, self.counter(user).reserved), (1, 0))
            self.assertRejected("already_dispatched", 400, services.dispatch_sample, run.pk, claimed.lease_token, 1)
        self.assertEqual(self.counter(user).used, 1)
        self.assertEqual(FingerprintSample.objects.get(run=run, ordinal=1).status, "dispatching")

    def test_dispatch_stops_when_the_key_was_rotated_or_reassigned(self):
        user, key, run, claimed = self._running_run()

        rotated = APIKey.generate_token()
        meta = APIKey.build_token_metadata(rotated)
        APIKey.objects.filter(pk=key.pk).update(token_plaintext=rotated, token_hash=meta["token_hash"])
        self.assertRejected("key_changed", 409, services.dispatch_sample, run.pk, claimed.lease_token, 2)

        other, _ = self.owner("other")
        # Hand the intact key to another account: the round must not follow it.
        APIKey.objects.filter(pk=key.pk).update(user=other, token_plaintext=key.token_plaintext,
                                               token_hash=key.token_hash)
        self.assertRejected("key_unavailable", 403, services.dispatch_sample, run.pk, claimed.lease_token, 2)
        # No other key was substituted for the member's own key.
        self.assertEqual(FingerprintRun.objects.get(pk=run.pk).status, FingerprintRun.RUNNING)
        self.assertEqual(FingerprintSample.objects.get(run=run, ordinal=2).status, "pending")

    def test_dispatch_stops_when_the_feature_is_switched_off(self):
        user, key, run, claimed = self._running_run()
        with frozen(NOW), override_settings(FINGERPRINT_ENABLED=False):
            self.assertRejected("cancelled", 400, services.dispatch_sample, run.pk, claimed.lease_token, 2)

    def test_finishing_with_a_foreign_or_expired_lease_is_ignored(self):
        user, key, run, claimed = self._running_run()
        with frozen(NOW):
            unchanged = services.finish_run(run.pk, uuid.uuid4(), FingerprintRun.COMPLETED)
        self.assertEqual(unchanged.status, FingerprintRun.RUNNING)
        self.assertIsNone(unchanged.finished_at)
        self.assertFalse(services.save_sample(run.pk, uuid.uuid4(), 1, {"status": "complete"}))
        self.assertEqual(FingerprintSample.objects.get(run=run, ordinal=1).status, "dispatching")

    def test_sample_results_are_recorded_only_for_the_current_lease(self):
        user, key, run, claimed = self._running_run()
        FingerprintSample.objects.filter(run=run, ordinal=1).update(status="pending")
        with frozen(NOW):
            services.dispatch_sample(run.pk, claimed.lease_token, 1)
            stored = services.save_sample(run.pk, claimed.lease_token, 1, {
                "status": "complete", "valid": True, "number_count": 300, "finish_reason": "stop",
                "duration_ms": 1200, "response_model": "gpt-text-1", "response_id": "resp-1",
                "usage": {"input_tokens": 10, "output_tokens": 20, "complete": True}, "error_code": "", "error_message": ""})
        self.assertTrue(stored)
        sample = FingerprintSample.objects.get(run=run, ordinal=1)
        self.assertTrue(sample.valid)
        self.assertEqual(sample.number_count, 300)
        self.assertIsNotNone(sample.finished_at)
        run.refresh_from_db()
        self.assertEqual(run.usage["input_tokens"], 10)

    def test_finish_run_marks_remaining_samples_and_aggregates_usage(self):
        user, key, run, claimed = self._running_run()
        FingerprintSample.objects.filter(run=run, ordinal=1).update(status="pending")
        with frozen(NOW):
            services.dispatch_sample(run.pk, claimed.lease_token, 1)
            services.save_sample(run.pk, claimed.lease_token, 1, self.sample_result())
            services.dispatch_sample(run.pk, claimed.lease_token, 2)
            finished = services.finish_run(run.pk, claimed.lease_token, FingerprintRun.PARTIAL, "partial", "部分结果")
        self.assertEqual(finished.status, FingerprintRun.PARTIAL)
        self.assertEqual(dict(finished.samples.values_list("ordinal", "status")), {1: "complete", 2: "interrupted"})
        self.assertEqual(finished.usage["input_tokens"], 10)
        self.assertEqual(finished.usage["sent_samples"], 2)
        self.assertFalse(finished.usage["complete"])


@override_settings(FINGERPRINT_ENABLED=True, FINGERPRINT_MODEL_POLICIES=MODEL_POLICIES)
class UsageAggregationTests(FingerprintServiceTestCase):
    def test_usage_is_never_reported_as_zero_when_upstream_omitted_it(self):
        user, key = self.owner()
        with frozen(NOW):
            run = self.dispatch(user, key)
            claimed = services.claim_run()
            self.add_sample(run, 1)
            self.add_sample(run, 2)
            self.add_sample(run, 3)  # never dispatched
            services.dispatch_sample(run.pk, claimed.lease_token, 1)
            services.dispatch_sample(run.pk, claimed.lease_token, 2)
            services.save_sample(run.pk, claimed.lease_token, 1, self.sample_result(usage={}))
            services.save_sample(run.pk, claimed.lease_token, 2, self.sample_result(
                usage={"input_tokens": 7, "output_tokens": 9, "complete": True}))
            usage = services.aggregate_usage(FingerprintRun.objects.get(pk=run.pk))

        self.assertEqual(usage["input_tokens"], 7)
        self.assertEqual(usage["output_tokens"], 9)
        self.assertIsNone(usage["cached_tokens"])
        self.assertIsNone(usage["reasoning_tokens"])
        self.assertFalse(usage["complete"])
        self.assertEqual(usage["sent_samples"], 2)
        self.assertEqual(usage["reported_samples"], 1)

    def test_all_samples_missing_usage_yields_unknown_rather_than_zero(self):
        user, key = self.owner()
        with frozen(NOW):
            run = self.dispatch(user, key)
            claimed = services.claim_run()
            self.add_sample(run, 1)
            services.dispatch_sample(run.pk, claimed.lease_token, 1)
            services.save_sample(run.pk, claimed.lease_token, 1, self.sample_result(
                status="failed", valid=False, number_count=0, usage={}))
            usage = services.aggregate_usage(FingerprintRun.objects.get(pk=run.pk))
        self.assertIsNone(usage["input_tokens"])
        self.assertIsNone(usage["output_tokens"])
        self.assertFalse(usage["complete"])
        self.assertEqual(usage["reported_samples"], 0)
        self.assertEqual(usage["sent_samples"], 1)


@override_settings(FINGERPRINT_ENABLED=True, FINGERPRINT_MODEL_POLICIES=MODEL_POLICIES,
                   FINGERPRINT_HISTORY_HOURS=24, FINGERPRINT_HISTORY_LIMIT=200)
class HistoryRetentionTests(FingerprintServiceTestCase):
    def test_finished_runs_are_pruned_after_twenty_four_hours(self):
        user, key = self.owner()
        old = self.make_run(user, key=key, status=FingerprintRun.COMPLETED,
                            created=NOW - timedelta(hours=25), finished=NOW - timedelta(hours=25))
        recent = self.make_run(user, key=key, status=FingerprintRun.COMPLETED,
                               created=NOW - timedelta(hours=23), finished=NOW - timedelta(hours=23))
        with frozen(NOW):
            services.maintenance(NOW)
        remaining = set(FingerprintRun.objects.values_list("pk", flat=True))
        self.assertEqual(remaining, {recent.pk})
        self.assertNotIn(old.pk, remaining)

    def test_each_member_keeps_at_most_the_history_limit(self):
        user, key = self.owner()
        for index in range(7):
            self.make_run(user, key=key, status=FingerprintRun.COMPLETED,
                          created=NOW - timedelta(minutes=index + 1),
                          finished=NOW - timedelta(minutes=index + 1))
        with override_settings(FINGERPRINT_HISTORY_LIMIT=5), frozen(NOW):
            services.maintenance(NOW)
        kept = list(FingerprintRun.objects.order_by("-finished_at").values_list("finished_at", flat=True))
        self.assertEqual(len(kept), 5)
        self.assertEqual(kept[0], NOW - timedelta(minutes=1))
        self.assertEqual(kept[-1], NOW - timedelta(minutes=5))

    def test_the_default_limit_is_two_hundred_runs_per_member(self):
        user, key = self.owner()
        FingerprintRun.objects.bulk_create([
            FingerprintRun(user=user, api_key=key, key_fingerprint=key.token_hash, key_label=key.name,
                           model_id=MODEL, protocol="chat_completions", upstream_commit=UPSTREAM_COMMIT,
                           idempotency_key=uuid.uuid4(), status=FingerprintRun.COMPLETED,
                           quota_day=TODAY, queue_deadline=NOW, finished_at=NOW - timedelta(minutes=index + 1))
            for index in range(205)])
        with frozen(NOW):
            services.maintenance(NOW)
        self.assertEqual(FingerprintRun.objects.count(), 200)

    def test_pruning_never_touches_active_or_expired_queue_entries(self):
        user, key = self.owner()
        waiting = self.make_run(user, key=key, created=NOW - timedelta(hours=48),
                                queue_deadline=NOW + timedelta(hours=1))
        with frozen(NOW):
            services.maintenance(NOW)
        self.assertTrue(FingerprintRun.objects.filter(pk=waiting.pk, status=FingerprintRun.QUEUED).exists())

    def test_history_cleanup_keeps_quota_rows_for_the_current_and_previous_day(self):
        user, key = self.owner()
        with frozen(NOW):
            self.seed_quota(user, day=date(2026, 10, 27), used=2)
            self.seed_quota(user, day=date(2026, 10, 26), used=1)
            self.seed_quota(user, day=date(2026, 10, 20), used=1)
            services.maintenance(NOW)
        days = set(FingerprintQuotaDay.objects.filter(user=user).values_list("day", flat=True))
        self.assertEqual(days, {date(2026, 10, 27), date(2026, 10, 26)})

    def test_sample_ordinals_are_unique_per_run(self):
        user, key = self.owner()
        run = self.make_run(user, key=key)
        self.add_sample(run, 1)
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.add_sample(run, 1)


@override_settings(FINGERPRINT_ENABLED=True, FINGERPRINT_MODEL_POLICIES=MODEL_POLICIES)
class CoordinatorTests(FingerprintServiceTestCase):
    def test_queue_lock_requires_the_singleton_row(self):
        FingerprintCoordinator.objects.all().delete()
        with self.assertRaises(FingerprintCoordinator.DoesNotExist):
            with services.queue_lock():
                pass

    def test_limits_come_from_server_settings(self):
        user, _ = self.owner()
        with override_settings(FINGERPRINT_DAILY_LIMIT=7):
            self.assertEqual(services.quota_for(user)["limit"], 7)
        with override_settings(FINGERPRINT_DAILY_LIMIT=0):
            # A misconfiguration may not silently disable the guard.
            self.assertEqual(services.quota_for(user)["limit"], 1)
