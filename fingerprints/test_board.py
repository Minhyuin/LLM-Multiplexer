"""Shared fingerprint results board: visibility, retention and admin deletion.

The board is the one place where members see each other's results, so the tests
pin the three properties that make it safe: everyone signed in can read it,
only admins can delete, and nothing older than the retention window survives.
"""
import uuid
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from api_keys.models import APIKey
from fingerprints import services
from fingerprints.config import UPSTREAM_COMMIT
from fingerprints.models import FingerprintCoordinator, FingerprintRun

User = get_user_model()

MODEL = "gpt-text-1"
RESULT = {"verdict": "scored", "family": "Llama 家族", "prediction": "llama-3",
          "candidates": [{"model": "llama-3-70b", "score": 0.912, "probability": 0.71}]}


@override_settings(FINGERPRINT_ENABLED=True, FINGERPRINT_HISTORY_HOURS=168)
class FingerprintBoardTests(TestCase):
    def setUp(self):
        super().setUp()
        FingerprintCoordinator.objects.get_or_create(pk=1)
        self.url = reverse("fingerprint-board")

    # --- fixtures ---------------------------------------------------------------
    def make_user(self, username, *, is_staff=False, role=None):
        user = User.objects.create_user(username=username, password="test-password", is_staff=is_staff)
        if role is not None:
            user.profile.role = role
            user.profile.save()
        return user

    def make_run(self, user, *, status=FingerprintRun.COMPLETED, finished=None, result=None, model=MODEL):
        raw = APIKey.generate_token()
        meta = APIKey.build_token_metadata(raw)
        key = APIKey.objects.create(user=user, name="主 Key", token_hash=meta["token_hash"],
                                    token_prefix=meta["token_prefix"], token_masked=meta["token_masked"],
                                    token_plaintext=raw, status=APIKey.STATUS_ACTIVE)
        return FingerprintRun.objects.create(
            user=user, api_key=key, key_fingerprint=key.token_hash, key_label="主 Key",
            model_id=model, protocol="chat_completions", parameters={"api": "chat_completions"},
            upstream_commit=UPSTREAM_COMMIT, idempotency_key=uuid.uuid4(), status=status,
            quota_day=services.day_at(timezone.now()), queue_deadline=timezone.now(),
            finished_at=finished, result=result or {})

    # --- visibility -------------------------------------------------------------
    def test_board_requires_a_signed_in_user(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response["Location"])

    def test_board_lists_every_members_finished_result(self):
        first = self.make_user("first")
        second = self.make_user("second")
        self.make_run(first, finished=timezone.now(), result=RESULT)
        self.make_run(second, finished=timezone.now() - timedelta(hours=1), result={**RESULT, "family": "Qwen 家族"})
        self.client.force_login(self.make_user("reader"))

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "first")
        self.assertContains(response, "second")
        self.assertContains(response, "Llama 家族")
        self.assertContains(response, "Qwen 家族")
        self.assertContains(response, "参考排名可用")
        self.assertEqual([row["model_id"] for row in response.context["rows"]], [MODEL, MODEL])

    def test_board_skips_runs_that_are_still_running(self):
        # One member may hold only one active run, so the two states need two owners.
        running = self.make_user("running")
        queued = self.make_user("queued")
        self.make_run(running, status=FingerprintRun.RUNNING, finished=None)
        self.make_run(queued, status=FingerprintRun.QUEUED, finished=None)
        self.client.force_login(self.make_user("reader"))

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["total"], 0)

    def test_board_drops_results_older_than_the_retention_window(self):
        member = self.make_user("historic")
        self.make_run(member, finished=timezone.now() - timedelta(days=8), result=RESULT)
        self.make_run(member, finished=timezone.now() - timedelta(days=6), result=RESULT)
        self.client.force_login(self.make_user("reader"))

        response = self.client.get(self.url)

        self.assertEqual(response.context["total"], 1)

    def test_retention_window_is_seven_days(self):
        from django.conf import settings

        self.assertEqual(settings.FINGERPRINT_HISTORY_HOURS, 168)

    def test_maintenance_keeps_what_the_board_still_shows(self):
        member = self.make_user("historic")
        kept = self.make_run(member, finished=timezone.now() - timedelta(days=6), result=RESULT)
        dropped = self.make_run(member, finished=timezone.now() - timedelta(days=8), result=RESULT)

        services.maintenance(now=timezone.now())

        self.assertTrue(FingerprintRun.objects.filter(pk=kept.pk).exists())
        self.assertFalse(FingerprintRun.objects.filter(pk=dropped.pk).exists())

    def test_board_shows_the_three_closest_models_ranked_by_similarity(self):
        member = self.make_user("ranked")
        candidates = [
            {"model": "far-model", "score": 0.31, "probability": None},
            {"model": "closest-model", "score": 0.94, "probability": None},
            {"model": "middle-model", "score": 0.72, "probability": None},
            {"model": "fourth-model", "score": 0.18, "probability": None},
        ]
        self.make_run(member, finished=timezone.now(), result={**RESULT, "candidates": candidates})
        self.client.force_login(self.make_user("reader"))

        response = self.client.get(self.url)

        shown = response.context["rows"][0]["candidates"]
        self.assertEqual([row["model"] for row in shown], ["closest-model", "middle-model", "far-model"])
        self.assertEqual([row["similarity"] for row in shown], ["0.94", "0.72", "0.31"])
        self.assertContains(response, "相似度最高的 3 个模型")
        self.assertNotContains(response, "候选参考模型")

    def test_board_renders_a_missing_similarity_without_inventing_a_number(self):
        member = self.make_user("unscored")
        self.make_run(member, finished=timezone.now(),
                      result={**RESULT, "candidates": [{"model": "mystery-model", "score": None}]})
        self.client.force_login(self.make_user("reader"))

        response = self.client.get(self.url)

        self.assertEqual(response.context["rows"][0]["candidates"][0]["similarity"], "—")
        self.assertNotContains(response, "0.0%")

    def test_board_shows_a_ranking_score_above_one_verbatim(self):
        # Real runs score above 1.0, so a percentage would print "321.8%".
        member = self.make_user("highscore")
        self.make_run(member, finished=timezone.now(),
                      result={**RESULT, "candidates": [{"model": "glm-5.3-flash", "score": 3.218}]})
        self.client.force_login(self.make_user("reader"))

        response = self.client.get(self.url)

        self.assertEqual(response.context["rows"][0]["candidates"][0]["similarity"], "3.218")
        self.assertNotContains(response, "321.8%")

    # --- deletion ---------------------------------------------------------------
    def test_a_member_cannot_delete_a_result(self):
        member = self.make_user("member")
        run = self.make_run(member, finished=timezone.now(), result=RESULT)
        self.client.force_login(member)

        response = self.client.post(reverse("fingerprint-board-delete", args=[run.pk]))

        self.assertEqual(response.status_code, 403)
        self.assertTrue(FingerprintRun.objects.filter(pk=run.pk).exists())

    def test_a_member_page_offers_no_delete_control(self):
        run_owner = self.make_user("owner")
        run = self.make_run(run_owner, finished=timezone.now(), result=RESULT)
        self.client.force_login(self.make_user("member"))

        response = self.client.get(self.url)

        self.assertFalse(response.context["can_delete"])
        self.assertNotContains(response, reverse("fingerprint-board-delete", args=[run.pk]))

    def test_an_admin_deletes_a_result(self):
        member = self.make_user("member")
        run = self.make_run(member, finished=timezone.now(), result=RESULT)
        self.client.force_login(self.make_user("root-admin", is_staff=True))

        response = self.client.post(reverse("fingerprint-board-delete", args=[run.pk]), {"page": "1"})

        self.assertEqual(response.status_code, 302)
        self.assertFalse(FingerprintRun.objects.filter(pk=run.pk).exists())

    def test_an_admin_cannot_delete_a_run_in_flight(self):
        member = self.make_user("member")
        run = self.make_run(member, status=FingerprintRun.RUNNING, finished=None)
        self.client.force_login(self.make_user("root-admin", is_staff=True))

        response = self.client.post(reverse("fingerprint-board-delete", args=[run.pk]))

        self.assertEqual(response.status_code, 302)
        self.assertTrue(FingerprintRun.objects.filter(pk=run.pk).exists())

    def test_delete_accepts_post_only(self):
        member = self.make_user("member")
        run = self.make_run(member, finished=timezone.now(), result=RESULT)
        self.client.force_login(self.make_user("root-admin", is_staff=True))

        response = self.client.get(reverse("fingerprint-board-delete", args=[run.pk]))

        self.assertEqual(response.status_code, 405)
        self.assertTrue(FingerprintRun.objects.filter(pk=run.pk).exists())
