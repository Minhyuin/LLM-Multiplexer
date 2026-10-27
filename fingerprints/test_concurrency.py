"""Concurrency tests for the fingerprints queue — PostgreSQL only.

``services.queue_lock()`` serialises every admission, lease and dispatch
decision with ``SELECT ... FOR UPDATE`` on the ``FingerprintCoordinator``
singleton.  On SQLite that statement is a no-op inside a single writer, so a
test running there would "pass" without proving anything: this whole module is
therefore skipped unless the configured database is PostgreSQL.

Run it with a PostgreSQL ``DATABASES`` (the role needs CREATEDB so Django can
create and drop ``test_<name>``)::

    python manage.py test fingerprints.test_concurrency --settings=<pg settings>

On SQLite the module is reported as skipped, and it never runs as part of a
sqlite test session.
"""
import threading
import uuid
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import connections
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from api_keys.models import APIKey
from fingerprints import services
from fingerprints.config import FingerprintError
from fingerprints.models import FingerprintCoordinator, FingerprintQuotaDay, FingerprintRun, FingerprintSample

User = get_user_model()

MODEL = "gpt-text-1"
MODEL_POLICIES = {MODEL: {"enabled": True, "api": "chat_completions"}}

#: ``services.queue_lock()`` relies on row locks that SQLite does not provide.
PG_ONLY = "row-level locking is Postgres-only; a SQLite run would prove nothing"


def _pg_only(test):
    vendor = connections["default"].vendor
    test.__unittest_skip__ = vendor != "postgresql"
    test.__unittest_skip_why__ = PG_ONLY
    return test


def run_concurrently(count, work):
    """Run ``work(index)`` on ``count`` threads that all start together.

    Each thread closes its own database connection when it finishes, so the
    main thread never inherits a stale one.
    """
    barrier = threading.Barrier(count)
    results = [None] * count
    errors = [None] * count

    def worker(index):
        try:
            barrier.wait(timeout=10)
            results[index] = work(index)
        except BaseException as exc:  # noqa: BLE001 - captured and re-raised by the caller
            errors[index] = exc
        finally:
            connections.close_all()

    threads = [threading.Thread(target=worker, args=(index,), name=f"fingerprint-{index}") for index in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    return results, [error for error in errors if error is not None]


@override_settings(FINGERPRINT_ENABLED=True, FINGERPRINT_MODEL_POLICIES=MODEL_POLICIES,
                   FINGERPRINT_DAILY_LIMIT=3, FINGERPRINT_QUEUE_LIMIT=20, FINGERPRINT_CONCURRENCY=2)
class FingerprintConcurrencyTestCase(TransactionTestCase):
    """Real transactions, real threads, real row locks."""

    reset_sequences = False
    available_apps = None

    def setUp(self):
        super().setUp()
        hasher = override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
        hasher.enable()
        self.addCleanup(hasher.disable)
        FingerprintCoordinator.objects.get_or_create(pk=1)
        models = patch("fingerprints.transport.available_models", return_value={MODEL})
        models.start()
        self.addCleanup(models.stop)

    # --- fixtures ---------------------------------------------------------------
    def member(self, username):
        user = User.objects.create_user(username=username, password="test-password")
        raw = APIKey.generate_token()
        meta = APIKey.build_token_metadata(raw)
        key = APIKey.objects.create(user=user, name="主 Key", token_hash=meta["token_hash"],
                                    token_prefix=meta["token_prefix"], token_masked=meta["token_masked"],
                                    token_plaintext=raw, status=APIKey.STATUS_ACTIVE)
        return user, key

    def counter(self, user):
        row, _ = FingerprintQuotaDay.objects.get_or_create(user=user, day=services.day_at(timezone.now()))
        return row

    def queued_run(self, user, key):
        return services.create_run(user, key.pk, MODEL, uuid.uuid4())[0]

    def add_sample(self, run, ordinal=1):
        return FingerprintSample.objects.create(run=run, ordinal=ordinal, challenge_id=f"challenge-{ordinal}",
                                               expected_count=300, status="pending")


@_pg_only
class AdmissionConcurrencyTests(FingerprintConcurrencyTestCase):
    def test_simultaneous_starts_by_one_member_admit_exactly_one_round(self):
        user, key = self.member("member")

        results, errors = run_concurrently(
            4, lambda index: services.create_run(user, key.pk, MODEL, uuid.uuid4()))

        self.assertEqual(errors, [], "concurrent admission raised instead of serialising")
        self.assertEqual(FingerprintRun.objects.filter(user=user).count(), 1)
        self.assertEqual(sum(1 for _run, reused in results if not reused), 1)
        self.assertEqual(sum(1 for _run, reused in results if reused), 3)
        counter = self.counter(user)
        self.assertEqual((counter.used, counter.reserved), (0, 1))

    def test_the_queue_limit_is_never_overshot(self):
        users = [self.member(f"member-{index}") for index in range(6)]

        def work(index):
            user, key = users[index]
            try:
                services.create_run(user, key.pk, MODEL, uuid.uuid4())
                return "admitted"
            except FingerprintError as exc:
                return exc.code

        with override_settings(FINGERPRINT_QUEUE_LIMIT=2):
            results, errors = run_concurrently(6, work)

        self.assertEqual(errors, [])
        self.assertEqual(results.count("admitted"), 2)
        self.assertEqual(results.count("queue_full"), 4)
        self.assertEqual(FingerprintRun.objects.filter(status=FingerprintRun.QUEUED).count(), 2)


@_pg_only
class LeaseConcurrencyTests(FingerprintConcurrencyTestCase):
    def test_four_workers_lease_exactly_two_distinct_runs(self):
        for index in range(4):
            user, key = self.member(f"member-{index}")
            self.queued_run(user, key)

        with override_settings(FINGERPRINT_CONCURRENCY=2):
            results, errors = run_concurrently(4, lambda index: services.claim_run())

        self.assertEqual(errors, [])
        leased = [run for run in results if run is not None]
        self.assertEqual(len(leased), 2)
        self.assertEqual(len({run.pk for run in leased}), 2)
        self.assertEqual(len({run.lease_token for run in leased}), 2)
        self.assertEqual(FingerprintRun.objects.filter(status=FingerprintRun.RUNNING).count(), 2)

    def test_two_workers_cannot_dispatch_the_same_challenge(self):
        user, key = self.member("member")
        run = self.queued_run(user, key)
        claimed = services.claim_run()
        self.add_sample(run, 1)

        def work(index):
            try:
                return services.dispatch_sample(run.pk, claimed.lease_token, 1)
            except FingerprintError as exc:
                return exc.code

        _results, errors = run_concurrently(2, work)
        self.assertEqual(errors, [])
        charged = self.counter(user)
        self.assertEqual((charged.used, charged.reserved), (1, 0))
        self.assertEqual(FingerprintSample.objects.get(run=run, ordinal=1).status, "dispatching")

    def test_the_round_is_never_consumed_twice(self):
        user, key = self.member("member")
        run = self.queued_run(user, key)
        claimed = services.claim_run()
        self.add_sample(run, 1)

        def work(index):
            try:
                services.dispatch_sample(run.pk, claimed.lease_token, 1)
                return "dispatched"
            except FingerprintError as exc:
                return exc.code

        results, errors = run_concurrently(2, work)
        self.assertEqual(errors, [])
        self.assertEqual(sorted(results), ["already_dispatched", "dispatched"])
        self.assertEqual(self.counter(user).used, 1)


@_pg_only
class TerminalConcurrencyTests(FingerprintConcurrencyTestCase):
    def test_only_one_terminal_transition_and_one_refund(self):
        user, key = self.member("member")
        run = self.queued_run(user, key)
        claimed = services.claim_run()

        def worker(index):
            services.finish_run(run.pk, claimed.lease_token, FingerprintRun.FAILED, "upstream_error", "上游返回错误。")
            return FingerprintRun.objects.get(pk=run.pk).status

        _results, errors = run_concurrently(2, worker)
        self.assertEqual(errors, [])
        stored = FingerprintRun.objects.get(pk=run.pk)
        self.assertEqual(stored.status, FingerprintRun.FAILED)
        counter = self.counter(user)
        self.assertEqual((counter.used, counter.reserved), (0, 0), "the reservation was refunded more than once")

    def test_a_cancel_racing_a_dispatch_leaves_a_consistent_ledger(self):
        user, key = self.member("member")
        run = self.queued_run(user, key)
        claimed = services.claim_run()
        self.add_sample(run, 1)

        def dispatch(index):
            try:
                services.dispatch_sample(run.pk, claimed.lease_token, 1)
                return "dispatched"
            except FingerprintError as exc:
                return exc.code

        def cancel(index):
            services.cancel_run(user, run.pk)
            return "cancelled"

        _results, errors = run_concurrently(2, lambda index: dispatch(index) if index == 0 else cancel(index))
        self.assertEqual(errors, [])
        counter = self.counter(user)
        stored = FingerprintRun.objects.get(pk=run.pk)
        # Cancel requests stop the executor first. Until the supervisor confirms
        # that exit, a running but undispatched task still owns its reservation.
        self.assertTrue(stored.cancel_requested)
        self.assertIn((counter.used, counter.reserved), {(1, 0), (0, 1)})
        self.assertEqual(stored.quota_consumed, counter.used == 1)
        used = counter.used
        # Exercise the same terminal transition as the supervisor, twice: only
        # an undispatched round is refunded and the second finish is a no-op.
        services.finish_run(run.pk, claimed.lease_token, FingerprintRun.CANCELLED)
        services.finish_run(run.pk, claimed.lease_token, FingerprintRun.CANCELLED)
        counter = self.counter(user)
        self.assertEqual((counter.used, counter.reserved), (used, 0))
        self.assertEqual(FingerprintRun.objects.get(pk=run.pk).status, FingerprintRun.CANCELLED)
