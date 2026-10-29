"""Offline executor/worker lifecycle tests; challenges never reach a real endpoint."""
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase, TransactionTestCase, override_settings

from api_keys.models import APIKey
from fingerprints import runner, services
from fingerprints.models import FingerprintCoordinator, FingerprintRun
from fingerprints.views import serialize
from fingerprints.management.commands import run_fingerprint_worker as worker

MODEL = "test-text-model"
CHALLENGES = [{"id": f"offline-{i}", "expected_count": 100, "prompt": "Offline fixed challenge"} for i in range(3)]
TEXT = ", ".join(str(i % 355 + 1) for i in range(90))


def result(valid=True, error=""):
    return {"status": "complete" if valid else "failed", "valid": valid,
            "number_count": 90 if valid else 0, "finish_reason": "stop" if valid else "",
            "duration_ms": 20, "response_model": "declared-model", "response_id": "response-id",
            "usage": {"input_tokens": 10, "output_tokens": 20, "complete": True},
            "error_code": error, "error_message": "Stopped" if error else "",
            "text": TEXT if valid else "", "expected_count": 100}


def analysis(decision="scored"):
    return {"decision": decision, "prediction_name": "Reference A", "family_prediction_name": "Family A",
            "probability_status": "reference_calibrated", "probability_scope": "reference_bank",
            "method": "offline", "evidence": {},
            "results": [{"display_name": f"Reference {i}", "score": .9 - i * .1, "probability": .6} for i in range(7)]}


@override_settings(FINGERPRINT_ENABLED=True,
                   PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class RunnerTests(TestCase):
    def setUp(self):
        FingerprintCoordinator.objects.get_or_create(pk=1)
        self.user = get_user_model().objects.create_user("runner-owner")
        token = APIKey.generate_token()
        self.key = APIKey.objects.create(user=self.user, name="Owner", token_plaintext=token,
            status=APIKey.STATUS_ACTIVE, **APIKey.build_token_metadata(token))
        with patch("fingerprints.transport.available_models", return_value={MODEL}):
            self.run, _ = services.create_run(self.user, self.key.pk, MODEL, uuid.uuid4())
        self.run = services.claim_run()
        self.detector = Mock(built_at="offline-bank")
        self.detector.analyze.return_value = analysis()
        for target, value in (("fingerprints.runner.sdk.detector", self.detector),
                              ("fingerprints.runner.sdk.generate_challenges", CHALLENGES)):
            patcher = patch(target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch("fingerprints.runner.transport.sample", return_value=result())
        self.sample = patcher.start()
        self.addCleanup(patcher.stop)

    def execute(self):
        runner.execute_run(self.run.pk, self.run.lease_token)
        self.run.refresh_from_db()
        return self.run

    def test_complete_round_uses_only_owner_key_and_persists_no_reply_or_secret(self):
        run = self.execute()
        self.assertEqual(run.status, FingerprintRun.COMPLETED)
        self.assertEqual(self.sample.call_count, 3)
        self.assertTrue(all(c.args[0] == self.key.token_plaintext for c in self.sample.call_args_list))
        self.assertEqual([c.args[3] for c in self.sample.call_args_list], CHALLENGES)
        self.detector.analyze.assert_called_once_with(
            [{"text": TEXT, "expected_count": 100}] * 3, strict=False)
        self.assertEqual(len(run.result["candidates"]), 5)
        self.assertEqual((run.usage["input_tokens"], run.usage["output_tokens"]), (30, 60))
        self.assertEqual((services.quota_for(self.user)["used"], services.quota_for(self.user)["reserved"]), (1, 0))
        payload = json.dumps(serialize(run))
        self.assertNotIn(self.key.token_plaintext, payload)
        self.assertNotIn(TEXT, payload)
        self.assertEqual(serialize(run)["completed_samples"], 3)

    def test_partial_round_keeps_failed_sample_and_never_invents_probability(self):
        self.sample.side_effect = [result(), result(False, "request_timeout"), result()]
        self.detector.analyze.return_value = analysis("partial")
        run = self.execute()
        self.assertEqual(run.status, FingerprintRun.PARTIAL)
        self.assertEqual(self.sample.call_count, 3)
        self.assertEqual(self.detector.analyze.call_args.args[0][1]["text"], "")
        self.assertEqual(run.result["probability_status"], "unavailable")
        self.assertTrue(all(c["probability"] is None for c in run.result["candidates"]))
        self.assertEqual(serialize(run)["valid_samples"], 2)

    def test_key_rotation_after_first_response_stops_remaining_challenges(self):
        def rotate(*args):
            new_token = APIKey.generate_token()
            APIKey.objects.filter(pk=self.key.pk).update(token_plaintext=new_token,
                                                        **APIKey.build_token_metadata(new_token))
            return result()
        self.sample.side_effect = rotate
        run = self.execute()
        self.assertEqual(run.error_code, "key_changed")
        self.assertEqual(self.sample.call_count, 1)
        self.assertEqual(list(run.samples.values_list("status", flat=True)), ["complete", "cancelled", "cancelled"])
        self.assertEqual(serialize(run)["completed_samples"], 1)
        self.detector.analyze.assert_not_called()

    def test_cancel_after_first_response_stops_remaining_challenges(self):
        def cancel(*args):
            services.cancel_run(self.user, self.run.pk)
            return result()
        self.sample.side_effect = cancel
        run = self.execute()
        self.assertEqual(run.status, FingerprintRun.CANCELLED)
        self.assertEqual(self.sample.call_count, 1)
        self.assertEqual(services.quota_for(self.user)["used"], 1)
        self.detector.analyze.assert_not_called()

    def test_cancel_before_dispatch_refunds_reservation_without_any_request(self):
        services.cancel_run(self.user, self.run.pk)
        run = self.execute()
        self.sample.assert_not_called()
        self.assertEqual(run.status, FingerprintRun.CANCELLED)
        self.assertEqual(services.quota_for(self.user)["remaining"], 3)

    def test_wrong_lease_cannot_execute(self):
        runner.execute_run(self.run.pk, uuid.uuid4())
        self.sample.assert_not_called()
        self.detector.analyze.assert_not_called()
        self.assertFalse(self.run.samples.exists())

    def test_reentry_never_replays_existing_dispatch(self):
        self.run.samples.create(ordinal=1, challenge_id="existing", expected_count=100)
        services.dispatch_sample(self.run.pk, self.run.lease_token, 1)
        run = self.execute()
        self.sample.assert_not_called()
        self.assertEqual(run.status, FingerprintRun.INTERRUPTED)
        self.assertEqual(run.samples.get().status, "interrupted")
        self.assertEqual(services.quota_for(self.user)["used"], 1)

    def test_scoring_error_is_generic_and_consumed_round_stays_consumed(self):
        self.detector.analyze.side_effect = ValueError(self.key.token_plaintext)
        with self.assertLogs("fingerprints.runner", level="ERROR") as logs:
            run = self.execute()
        self.assertEqual(run.error_code, "execution_error")
        self.assertNotIn(self.key.token_plaintext, str(logs.output) + run.error_message)
        self.assertEqual(self.sample.call_count, 3)
        self.assertEqual(services.quota_for(self.user)["used"], 1)

    def test_missing_local_bank_fails_before_dispatch_and_refunds(self):
        with patch("fingerprints.runner.sdk.detector", side_effect=ValueError("bank unavailable")):
            with self.assertLogs("fingerprints.runner", level="ERROR"):
                run = self.execute()
        self.sample.assert_not_called()
        self.assertEqual(run.status, FingerprintRun.FAILED)
        self.assertEqual(services.quota_for(self.user)["remaining"], 3)


class WorkerLifecycleTests(TransactionTestCase):
    def test_stop_terminates_then_kills_and_reaps_a_stuck_child(self):
        process = Mock()
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired("child", 2), 0]
        worker.stop_process(process)
        process.terminate.assert_called_once()
        process.kill.assert_called_once()
        self.assertEqual(process.wait.call_count, 2)
        process.stdin.close.assert_called_once()

    def test_failed_initial_keepalive_reaps_process_before_releasing_slot(self):
        run = SimpleNamespace(pk=uuid.uuid4(), lease_token=uuid.uuid4())
        process = Mock()
        process.stdin.write.side_effect = BrokenPipeError()
        process.poll.return_value = 1
        with patch.object(worker, "maintenance"), patch.object(worker, "enabled", return_value=True), \
             patch.object(worker, "claim_run", side_effect=[run, None]), \
             patch.object(worker.subprocess, "Popen", return_value=process), \
             patch.object(worker, "stop_process") as stop, patch.object(worker, "finish_run") as finish:
            worker.Command().handle(once=True)
        stop.assert_called_once_with(process)
        self.assertEqual(finish.call_args.args[3], "worker_start")

    def test_supervisor_cancels_running_child_and_finishes_without_requeue(self):
        run = SimpleNamespace(pk=uuid.uuid4(), lease_token=uuid.uuid4())
        process = Mock()
        process.poll.return_value = None
        with patch.object(worker, "maintenance"), patch.object(worker, "enabled", return_value=True), \
             patch.object(worker, "claim_run", side_effect=[run, None, None]), \
             patch.object(worker.subprocess, "Popen", return_value=process), \
             patch.object(worker, "is_cancelled", return_value=True), \
             patch.object(worker, "stop_process") as stop, patch.object(worker, "finish_run") as finish, \
             patch.object(worker.time, "sleep"):
            worker.Command().handle(once=True)
        stop.assert_called_once_with(process)
        self.assertEqual(finish.call_args.args[2:4], (FingerprintRun.CANCELLED, "cancelled"))

    def run_watchdog(self, hard_limit):
        code = '''import os, sys, time, uuid
os.environ['DJANGO_SETTINGS_MODULE'] = 'lab_portal.settings_test'
import django
django.setup()
from fingerprints.management.commands import fingerprint_execute as child
child.limit = lambda name, default: int(sys.argv[1])
def idle(*args):
    print('READY', flush=True)
    while True:
        time.sleep(.05)
child.execute_run = idle
child.Command().handle(run_id=uuid.uuid4(), lease=uuid.uuid4())
'''
        process = subprocess.Popen([sys.executable, "-c", code, str(hard_limit)], cwd=settings.BASE_DIR,
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            # communicate closes stdin: the EOF detector must exit promptly even
            # with a long per-run timeout and a blocked executor.
            if hard_limit > 1:
                output, errors = process.communicate(timeout=8)
            else:
                # Keep the parent pipe open; the hard deadline itself must exit.
                process.wait(timeout=8)
                output, errors = process.communicate(timeout=2)
            self.assertEqual(process.returncode, 75, (output, errors))
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=3)

    def test_actual_child_exits_on_parent_pipe_eof(self):
        self.run_watchdog(420)

    def test_actual_child_hard_deadline_exits_with_parent_alive(self):
        self.run_watchdog(1)
