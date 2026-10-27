"""Offline HTTP-contract tests for the fingerprints JSON endpoints.

Nothing here talks to the network: the owner-scoped model catalogue lookup is
mocked (``fingerprints.transport.available_models``) and no challenge is ever
sent.  These tests pin the transport-level contract — method, CSRF, owner
scoping, read-only previews, cache headers, payload shape and status codes.
"""
import json
import uuid
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from api_keys.models import APIKey
from fingerprints import services
from fingerprints.config import UPSTREAM_COMMIT
from fingerprints.models import (
    FingerprintCoordinator,
    FingerprintQuotaDay,
    FingerprintRun,
    FingerprintSample,
)
from fingerprints import views as fingerprint_views
from fingerprints.views import annotate_catalog, context
from users.middleware import ViewAsState

User = get_user_model()

MODEL = "gpt-text-1"
EMBEDDING_MODEL = "text-embedding-3-large"
DISABLED_MODEL = "legacy-text"
MODEL_POLICIES = {
    MODEL: {"enabled": True, "api": "chat_completions"},
    DISABLED_MODEL: {"enabled": False},
}
NO_STORE = "private, no-store, no-cache, max-age=0, must-revalidate"


@override_settings(FINGERPRINT_ENABLED=True, FINGERPRINT_MODEL_POLICIES=MODEL_POLICIES)
class FingerprintViewTestCase(TestCase):
    def setUp(self):
        super().setUp()
        hasher = override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
        hasher.enable()
        self.addCleanup(hasher.disable)
        # The singleton coordinator row is the admission/lease mutex.
        FingerprintCoordinator.objects.get_or_create(pk=1)
        self.model_calls = []
        models = patch("fingerprints.transport.available_models", side_effect=self._available_models)
        models.start()
        self.addCleanup(models.stop)
        self.state_url = reverse("fingerprint-state")
        self.start_url = reverse("fingerprint-start")

    def _available_models(self, token):
        self.model_calls.append(token)
        return {MODEL}

    # --- fixtures ---------------------------------------------------------------
    def make_user(self, username, *, is_staff=False, role=None):
        user = User.objects.create_user(username=username, password="test-password", is_staff=is_staff)
        if role is not None:
            user.profile.role = role
            user.profile.save()
        return user

    def make_key(self, user, name="主 Key"):
        raw = APIKey.generate_token()
        meta = APIKey.build_token_metadata(raw)
        return APIKey.objects.create(user=user, name=name, token_hash=meta["token_hash"],
                                     token_prefix=meta["token_prefix"], token_masked=meta["token_masked"],
                                     token_plaintext=raw, status=APIKey.STATUS_ACTIVE)

    def member(self, username="member"):
        user = self.make_user(username)
        return user, self.make_key(user)

    def make_run(self, user, key, *, status=FingerprintRun.QUEUED, created=None, finished=None,
                 queue_deadline=None, cancel_requested=False):
        created = created or timezone.now()
        run = FingerprintRun.objects.create(
            user=user, api_key=key, key_fingerprint=key.token_hash, key_label=f"{key.name} · {key.token_masked}",
            model_id=MODEL, protocol="chat_completions", parameters={"api": "chat_completions"},
            upstream_commit=UPSTREAM_COMMIT, idempotency_key=uuid.uuid4(), status=status,
            quota_day=services.day_at(created), queue_deadline=queue_deadline or created + timedelta(seconds=180),
            finished_at=finished, cancel_requested=cancel_requested)
        if created != timezone.now():
            # created_at is auto_now_add, so a controlled timestamp needs an UPDATE.
            FingerprintRun.objects.filter(pk=run.pk).update(created_at=created)
            run.refresh_from_db()
        return run

    def login(self, user, *, client=None):
        client = client or self.client
        client.force_login(user)
        return client

    def seed_quota(self, user, *, used=0, reserved=0, last=None, day=None):
        row, _ = FingerprintQuotaDay.objects.update_or_create(
            user=user, day=day or services.day_at(timezone.now()),
            defaults={"used": used, "reserved": reserved, "last_submitted_at": last})
        return row

    def post_json(self, url, payload, *, client=None):
        return (client or self.client).post(url, data=json.dumps(payload), content_type="application/json")

    def error_code(self, response):
        return json.loads(response.content)["error"]["code"]

    def assertNoStore(self, response):
        self.assertEqual(response["Cache-Control"], NO_STORE)

    def start_payload(self, key, **extra):
        payload = {"key_id": key.pk, "model_id": MODEL, "idempotency_key": str(uuid.uuid4())}
        payload.update(extra)
        return payload


@override_settings(FINGERPRINT_ENABLED=True, FINGERPRINT_MODEL_POLICIES=MODEL_POLICIES)
class AuthContractTests(FingerprintViewTestCase):
    def test_anonymous_callers_get_a_json_login_error(self):
        for url in (self.state_url, self.start_url):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 401)
                self.assertEqual(self.error_code(response), "login_required")
                self.assertNoStore(response)

    def test_each_endpoint_refuses_the_wrong_method(self):
        user, key = self.member()
        run = self.make_run(user, key)
        self.login(user)
        cases = [(self.state_url, "post"), (self.start_url, "get"),
                 (reverse("fingerprint-detail", kwargs={"run_id": run.pk}), "post"),
                 (reverse("fingerprint-cancel", kwargs={"run_id": run.pk}), "get")]
        for url, method in cases:
            with self.subTest(url=url, method=method):
                response = getattr(self.client, method)(url)
                self.assertEqual(response.status_code, 405)
                self.assertEqual(self.error_code(response), "method_not_allowed")
                self.assertNoStore(response)

    def test_a_disabled_account_is_refused_even_with_a_live_session(self):
        user, _ = self.member()
        user.is_active = False
        request = RequestFactory().get(self.state_url)
        request.user = user
        response = fingerprint_views.state(request)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.error_code(response), "account_disabled")


@override_settings(FINGERPRINT_ENABLED=True, FINGERPRINT_MODEL_POLICIES=MODEL_POLICIES)
class CsrfTests(FingerprintViewTestCase):
    def test_start_without_a_csrf_token_is_rejected_and_admits_nothing(self):
        user, key = self.member()
        client = self.login(user, client=Client(enforce_csrf_checks=True))
        response = self.post_json(self.start_url, self.start_payload(key), client=client)
        self.assertEqual(response.status_code, 403)
        self.assertFalse(FingerprintRun.objects.exists())

    def test_start_with_a_csrf_token_is_accepted(self):
        user, key = self.member()
        client = self.login(user, client=Client(enforce_csrf_checks=True))
        client.get("/")  # seeds the csrftoken cookie from the rendered form
        token = client.cookies["csrftoken"].value
        response = client.post(self.start_url, data=json.dumps(self.start_payload(key)),
                               content_type="application/json", HTTP_X_CSRFTOKEN=token)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(FingerprintRun.objects.count(), 1)

    def test_cancel_requires_a_post_with_a_csrf_token(self):
        user, key = self.member()
        run = self.make_run(user, key)
        client = self.login(user, client=Client(enforce_csrf_checks=True))
        url = reverse("fingerprint-cancel", kwargs={"run_id": run.pk})
        self.assertEqual(client.post(url).status_code, 403)
        self.assertEqual(FingerprintRun.objects.get(pk=run.pk).status, FingerprintRun.QUEUED)


@override_settings(FINGERPRINT_ENABLED=True, FINGERPRINT_MODEL_POLICIES=MODEL_POLICIES)
class StateViewTests(FingerprintViewTestCase):
    def test_disabled_feature_reports_itself_without_leaking_anything(self):
        user, _ = self.member()
        self.login(user)
        with override_settings(FINGERPRINT_ENABLED=False):
            response = self.client.get(self.state_url)
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload, {"enabled": False, "read_only": False, "keys": [], "quota": None,
                                  "active": None, "history": []})
        self.assertNoStore(response)

    def test_state_reports_masked_keys_quota_and_history(self):
        user, key = self.member()
        done = self.make_run(user, key, status=FingerprintRun.COMPLETED,
                             created=timezone.now() - timedelta(minutes=30),
                             finished=timezone.now() - timedelta(minutes=29))
        FingerprintSample.objects.create(run=done, ordinal=1, challenge_id="challenge-1", expected_count=300,
                                         status="complete", valid=True, number_count=300)
        self.login(user)

        payload = self.client.get(self.state_url).json()
        self.assertTrue(payload["enabled"])
        self.assertFalse(payload["read_only"])
        self.assertEqual(payload["keys"], [{"id": key.pk, "name": key.name, "masked": key.token_masked}])
        self.assertNotIn(key.token_plaintext, json.dumps(payload))
        self.assertEqual(payload["quota"]["remaining"], 3)
        self.assertIsNone(payload["active"])
        self.assertEqual([run["id"] for run in payload["history"]], [str(done.pk)])
        serialized = payload["history"][0]
        self.assertEqual(serialized["status"], FingerprintRun.COMPLETED)
        self.assertEqual(serialized["total_samples"], 3)
        self.assertEqual(serialized["completed_samples"], 1)
        self.assertEqual(serialized["valid_samples"], 1)
        self.assertEqual(serialized["status_url"], reverse("fingerprint-detail", kwargs={"run_id": done.pk}))

    def test_state_shows_the_active_run_and_never_mutates_it(self):
        user, key = self.member()
        run = self.make_run(user, key, queue_deadline=timezone.now() - timedelta(seconds=5))
        sample = FingerprintSample.objects.create(run=run, ordinal=1, challenge_id="challenge-1",
                                                  expected_count=300, status="dispatching")
        self.login(user)

        payload = self.client.get(self.state_url).json()
        self.assertEqual(payload["active"]["id"], str(run.pk))
        self.assertEqual(payload["active"]["status"], FingerprintRun.QUEUED)
        self.assertEqual(payload["history"], [])
        # GET is read-only: an overdue queue entry is not expired, requeued or refunded by a read.
        stored = FingerprintRun.objects.get(pk=run.pk)
        self.assertEqual(stored.status, FingerprintRun.QUEUED)
        self.assertEqual(FingerprintSample.objects.get(pk=sample.pk).status, "dispatching")

    def test_reading_the_state_has_no_side_effects(self):
        user, key = self.member()
        self.make_run(user, key, status=FingerprintRun.COMPLETED,
                      finished=timezone.now() - timedelta(minutes=5))
        self.login(user)

        self.client.get(self.state_url)
        self.client.get(self.state_url)
        self.assertEqual(FingerprintQuotaDay.objects.count(), 0, "a read created a quota row")
        self.assertEqual(FingerprintRun.objects.count(), 1)
        self.assertEqual(self.model_calls, [], "a read asked upstream for the model list")

    def test_history_rolls_off_after_twenty_four_hours_and_never_crosses_members(self):
        user, key = self.member()
        recent = self.make_run(user, key, status=FingerprintRun.COMPLETED,
                              created=timezone.now() - timedelta(hours=23),
                              finished=timezone.now() - timedelta(hours=23))
        self.make_run(user, key, status=FingerprintRun.COMPLETED,
                      created=timezone.now() - timedelta(hours=25),
                      finished=timezone.now() - timedelta(hours=25))
        other, other_key = self.member("other")
        self.make_run(other, other_key, status=FingerprintRun.FAILED,
                      created=timezone.now() - timedelta(minutes=5),
                      finished=timezone.now() - timedelta(minutes=5))
        self.login(user)

        payload = self.client.get(self.state_url).json()
        self.assertEqual([run["id"] for run in payload["history"]], [str(recent.pk)])

    def test_history_listing_honours_the_configured_limit(self):
        user, key = self.member()
        made = [self.make_run(user, key, status=FingerprintRun.COMPLETED,
                              created=timezone.now() - timedelta(minutes=index + 1),
                              finished=timezone.now() - timedelta(minutes=index + 1))
                for index in range(3)]
        self.login(user)

        with override_settings(FINGERPRINT_HISTORY_LIMIT=2):
            payload = self.client.get(self.state_url).json()
        self.assertEqual([run["id"] for run in payload["history"]], [str(made[0].pk), str(made[1].pk)])


@override_settings(FINGERPRINT_ENABLED=True, FINGERPRINT_MODEL_POLICIES=MODEL_POLICIES)
class StartViewTests(FingerprintViewTestCase):
    def test_start_is_refused_while_the_feature_is_switched_off(self):
        user, key = self.member()
        self.login(user)
        with override_settings(FINGERPRINT_ENABLED=False):
            response = self.post_json(self.start_url, self.start_payload(key))
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.error_code(response), "disabled")
        self.assertFalse(FingerprintRun.objects.exists())
        self.assertEqual(self.model_calls, [])

    def test_start_returns_the_run_and_the_remaining_allowance(self):
        user, key = self.member()
        self.login(user)

        response = self.post_json(self.start_url, self.start_payload(key))
        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertFalse(payload["reused"])
        self.assertEqual(payload["run"]["status"], FingerprintRun.QUEUED)
        self.assertEqual(payload["run"]["key_label"], f"{key.name} · {key.token_masked}")
        self.assertEqual(payload["run"]["total_samples"], 3)
        self.assertEqual(payload["run"]["cancel_url"],
                         reverse("fingerprint-cancel", kwargs={"run_id": payload["run"]["id"]}))
        self.assertEqual((payload["quota"]["used"], payload["quota"]["reserved"], payload["quota"]["remaining"]),
                         (0, 1, 2))
        self.assertNoStore(response)
        self.assertEqual(self.model_calls, [key.token_plaintext])

    def test_a_repeated_submission_is_idempotent(self):
        user, key = self.member()
        self.login(user)
        payload = self.start_payload(key)

        first = self.post_json(self.start_url, payload).json()
        second = self.post_json(self.start_url, payload).json()
        self.assertFalse(first["reused"])
        self.assertTrue(second["reused"])
        self.assertEqual(first["run"]["id"], second["run"]["id"])
        self.assertEqual(FingerprintRun.objects.count(), 1)
        self.assertEqual(second["quota"]["reserved"], 1)

    def test_a_second_round_inside_the_cooldown_is_refused_over_http(self):
        user, key = self.member()
        self.login(user)
        first = self.post_json(self.start_url, self.start_payload(key))
        self.assertEqual(first.status_code, 200, first.content)
        # End the first round, so the refusal cannot come from the active-run rule.
        FingerprintRun.objects.filter(pk=json.loads(first.content)["run"]["id"]).update(
            status=FingerprintRun.CANCELLED, finished_at=timezone.now())

        response = self.post_json(self.start_url, self.start_payload(key))
        self.assertEqual(response.status_code, 429)
        self.assertEqual(self.error_code(response), "cooldown")
        self.assertNoStore(response)
        self.assertEqual(FingerprintRun.objects.count(), 1)

    def test_exhausted_allowance_is_refused_over_http(self):
        user, key = self.member()
        self.seed_quota(user, used=3)
        self.login(user)

        response = self.post_json(self.start_url, self.start_payload(key))
        self.assertEqual(response.status_code, 429)
        self.assertEqual(self.error_code(response), "daily_limit")
        self.assertFalse(FingerprintRun.objects.exists())

    def test_another_members_key_is_refused_over_http(self):
        user, _ = self.member()
        _, foreign = self.member("other")
        self.login(user)

        response = self.post_json(self.start_url, self.start_payload(foreign))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.error_code(response), "key_unavailable")
        self.assertFalse(FingerprintRun.objects.exists())

    def test_a_model_missing_from_the_owners_catalogue_is_refused_over_http(self):
        user, key = self.member()
        self.login(user)
        with patch("fingerprints.transport.available_models", return_value=set()):
            response = self.post_json(self.start_url, self.start_payload(key))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.error_code(response), "model_unavailable")

    def test_unsupported_models_are_refused_over_http(self):
        user, key = self.member()
        self.login(user)
        for model_id, expected in ((EMBEDDING_MODEL, "unsupported_model"), (DISABLED_MODEL, "unsupported_model"),
                                   ("", "invalid_model")):
            with self.subTest(model_id=model_id):
                response = self.post_json(self.start_url, self.start_payload(key, model_id=model_id))
                self.assertEqual(response.status_code, 400)
                self.assertEqual(self.error_code(response), expected)
        self.assertFalse(FingerprintRun.objects.exists())

    def test_malformed_bodies_are_rejected_before_any_admission(self):
        user, key = self.member()
        self.login(user)
        bodies = [
            (b"{not json", 400, "invalid_request"),
            (b"[]", 400, "invalid_request"),
            (json.dumps({"key_id": key.pk, "model_id": MODEL, "idempotency_key": str(uuid.uuid4()),
                         "cost_limit": 10}).encode(), 400, "invalid_request"),
            (json.dumps(self.start_payload(key)).encode() + b" " * 4096, 400, "invalid_request"),
        ]
        for body, status, code in bodies:
            with self.subTest(body=body[:24]):
                response = self.client.post(self.start_url, data=body, content_type="application/json")
                self.assertEqual(response.status_code, status)
                self.assertEqual(self.error_code(response), code)
        self.assertFalse(FingerprintRun.objects.exists())
        self.assertEqual(self.model_calls, [])

    def test_missing_arguments_are_rejected(self):
        user, key = self.member()
        self.login(user)
        cases = [
            ({"key_id": key.pk, "idempotency_key": str(uuid.uuid4())}, "invalid_model"),
            ({"key_id": key.pk, "model_id": MODEL}, "invalid_request"),
            ({"model_id": MODEL, "idempotency_key": str(uuid.uuid4())}, "invalid_request"),
        ]
        for payload, code in cases:
            with self.subTest(payload=sorted(payload)):
                response = self.post_json(self.start_url, payload)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(self.error_code(response), code)
        self.assertFalse(FingerprintRun.objects.exists())


@override_settings(FINGERPRINT_ENABLED=True, FINGERPRINT_MODEL_POLICIES=MODEL_POLICIES)
class DetailViewTests(FingerprintViewTestCase):
    def test_the_owner_can_read_a_run_and_its_samples(self):
        user, key = self.member()
        run = self.make_run(user, key, status=FingerprintRun.COMPLETED,
                            finished=timezone.now() - timedelta(minutes=1))
        FingerprintSample.objects.create(run=run, ordinal=1, challenge_id="challenge-1", expected_count=300,
                                         status="complete", valid=True, number_count=300,
                                         finish_reason="stop", duration_ms=900, response_model=MODEL,
                                         error_message="")
        self.login(user)

        response = self.client.get(reverse("fingerprint-detail", kwargs={"run_id": run.pk}))
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["run"]["id"], str(run.pk))
        self.assertEqual(payload["quota"]["remaining"], 3)
        self.assertEqual([sample["number_count"] for sample in payload["run"]["samples"]], [300])
        self.assertEqual(payload["run"]["samples"][0]["response_model"], MODEL)
        self.assertNoStore(response)

    def test_other_members_and_unknown_ids_get_the_same_404(self):
        user, key = self.member()
        intruder, _ = self.member("intruder")
        run = self.make_run(user, key)
        self.login(intruder)

        foreign = self.client.get(reverse("fingerprint-detail", kwargs={"run_id": run.pk}))
        unknown = self.client.get(reverse("fingerprint-detail", kwargs={"run_id": uuid.uuid4()}))
        for response in (foreign, unknown):
            self.assertEqual(response.status_code, 404)
            self.assertEqual(self.error_code(response), "not_found")
            self.assertNoStore(response)
        self.assertNotIn(str(run.pk), foreign.content.decode())

    def test_expired_history_is_not_readable(self):
        user, key = self.member()
        expired = self.make_run(user, key, status=FingerprintRun.COMPLETED,
                               created=timezone.now() - timedelta(hours=25),
                               finished=timezone.now() - timedelta(hours=25))
        self.login(user)

        response = self.client.get(reverse("fingerprint-detail", kwargs={"run_id": expired.pk}))
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.error_code(response), "not_found")
        self.assertTrue(FingerprintRun.objects.filter(pk=expired.pk).exists())


@override_settings(FINGERPRINT_ENABLED=True, FINGERPRINT_MODEL_POLICIES=MODEL_POLICIES)
class CancelViewTests(FingerprintViewTestCase):
    def test_cancelling_a_queued_run_refunds_the_allowance(self):
        user, key = self.member()
        run = self.make_run(user, key)
        self.seed_quota(user, reserved=1, last=timezone.now())
        self.login(user)

        response = self.client.post(reverse("fingerprint-cancel", kwargs={"run_id": run.pk}))
        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertEqual(payload["run"]["status"], FingerprintRun.CANCELLED)
        self.assertEqual(payload["quota"]["remaining"], 3)
        self.assertNoStore(response)

    def test_cancelling_a_running_run_only_asks_the_worker_to_stop(self):
        user, key = self.member()
        run = self.make_run(user, key)
        claimed = services.claim_run()
        self.login(user)

        payload = self.client.post(reverse("fingerprint-cancel", kwargs={"run_id": run.pk})).json()
        self.assertEqual(payload["run"]["status"], FingerprintRun.RUNNING)
        self.assertTrue(payload["run"]["cancel_requested"])
        self.assertIsNotNone(claimed)

    def test_cancel_is_owner_scoped(self):
        user, key = self.member()
        intruder, _ = self.member("intruder")
        run = self.make_run(user, key)
        self.login(intruder)

        response = self.client.post(reverse("fingerprint-cancel", kwargs={"run_id": run.pk}))
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.error_code(response), "not_found")
        self.assertEqual(FingerprintRun.objects.get(pk=run.pk).status, FingerprintRun.QUEUED)


@override_settings(FINGERPRINT_ENABLED=True, FINGERPRINT_MODEL_POLICIES=MODEL_POLICIES)
class PreviewReadOnlyTests(FingerprintViewTestCase):
    """A preview may look at its own history but may never start or cancel."""

    def _preview(self, admin, target):
        client = self.login(admin)
        response = client.post(reverse("view-as-start", args=[target.pk]))
        self.assertEqual(response.status_code, 302, "preview did not start")
        return client

    def test_preview_state_is_read_only_and_hides_the_targets_keys_and_history(self):
        admin = self.make_user("admin", is_staff=True)
        admin_key = self.make_key(admin)
        target, target_key = self.member("target")
        target_run = self.make_run(target, target_key, status=FingerprintRun.COMPLETED,
                                  finished=timezone.now() - timedelta(minutes=5))
        self.make_run(admin, admin_key, status=FingerprintRun.FAILED,
                      finished=timezone.now() - timedelta(minutes=5))
        client = self._preview(admin, target)

        payload = client.get(self.state_url).json()
        self.assertTrue(payload["read_only"])
        self.assertEqual(payload["keys"], [])
        ids = [run["id"] for run in payload["history"]]
        self.assertNotIn(str(target_run.pk), ids)
        self.assertEqual(len(ids), 1)

    def test_preview_cannot_start_a_paid_detection(self):
        admin = self.make_user("admin", is_staff=True)
        self.make_key(admin)
        target, target_key = self.member("target")
        client = self._preview(admin, target)

        response = self.post_json(self.start_url, self.start_payload(target_key), client=client)
        self.assertEqual(response.status_code, 403)
        self.assertFalse(FingerprintRun.objects.exists())

    def test_the_view_refuses_an_unsafe_method_even_when_it_reaches_the_view(self):
        # Defence in depth: the middleware blocks preview writes first, so the
        # view's own guard is checked directly.
        admin = self.make_user("admin", is_staff=True)
        target, target_key = self.member("target")
        state = ViewAsState()
        state.active = True
        state.admin_user = admin
        state.target_user = target

        request = RequestFactory().post(self.start_url, data=json.dumps(self.start_payload(target_key)),
                                        content_type="application/json")
        request.user = target
        request.view_as = state
        response = fingerprint_views.start(request)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.error_code(response), "read_only")
        self.assertNoStore(response)
        self.assertFalse(FingerprintRun.objects.exists())

    def test_read_only_get_still_works_in_preview(self):
        admin = self.make_user("admin", is_staff=True)
        self.make_key(admin)
        target, _ = self.member("target")
        client = self._preview(admin, target)
        self.assertEqual(client.get(self.state_url).status_code, 200)


@override_settings(FINGERPRINT_ENABLED=True, FINGERPRINT_MODEL_POLICIES=MODEL_POLICIES)
class CatalogAnnotationTests(FingerprintViewTestCase):
    def _catalog(self):
        return {"model_groups": [{"models": [{"id": MODEL}, {"id": EMBEDDING_MODEL}, {"id": DISABLED_MODEL}]}]}

    def test_supported_models_are_annotated_with_a_reason_when_they_are_not(self):
        request = RequestFactory().get("/")
        request.user = self.make_user("member")
        catalog = annotate_catalog(self._catalog(), request)
        models = {model["id"]: model for model in catalog["model_groups"][0]["models"]}

        self.assertTrue(models[MODEL]["fingerprint_supported"])
        self.assertEqual(models[MODEL]["fingerprint_reason"], "")
        self.assertFalse(models[EMBEDDING_MODEL]["fingerprint_supported"])
        self.assertTrue(models[EMBEDDING_MODEL]["fingerprint_reason"])
        self.assertFalse(models[DISABLED_MODEL]["fingerprint_supported"])
        self.assertTrue(models[DISABLED_MODEL]["fingerprint_reason"])
        self.assertTrue(catalog["fingerprint_enabled"])
        self.assertFalse(catalog["fingerprint_read_only"])
        self.assertEqual(catalog["fingerprint_state_url"], self.state_url)
        self.assertEqual(catalog["fingerprint_start_url"], self.start_url)

    def test_a_disabled_feature_adds_no_per_model_verdicts(self):
        request = RequestFactory().get("/")
        request.user = self.make_user("member")
        with override_settings(FINGERPRINT_ENABLED=False):
            catalog = annotate_catalog(self._catalog(), request)
            self.assertFalse(context(request)["fingerprint_enabled"])
        for group in catalog["model_groups"]:
            for model in group["models"]:
                self.assertNotIn("fingerprint_supported", model)
