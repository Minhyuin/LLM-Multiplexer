"""One process executes at most one run; partial network outcomes never replay."""
import logging

from django.db import close_old_connections
from django.utils import timezone

from . import sdk, transport
from .config import FingerprintError
from .models import FingerprintRun, FingerprintSample
from .services import dispatch_sample, finish_run, is_cancelled, save_sample

logger = logging.getLogger(__name__)


def checked_cancel(run_id, lease):
    # Called by the transport watcher in a thread. Never leave thread-local
    # database connections behind after a sample.
    close_old_connections()
    try:
        return is_cancelled(run_id, lease)
    finally:
        from django.db import connections
        connections.close_all()


def summarize_analysis(analysis):
    calibrated = analysis.get("probability_status") == "reference_calibrated" and analysis.get("decision") == "scored"
    return {"verdict": analysis.get("decision", "unscorable"), "prediction": analysis.get("prediction_name", ""),
            "family": analysis.get("family_prediction_name", ""),
            "probability_status": "reference_calibrated" if calibrated else "unavailable",
            "probability_scope": analysis.get("probability_scope") if calibrated else None,
            "evidence": analysis.get("evidence", {}), "method": analysis.get("method", ""),
            "candidates": [{"model": r.get("display_name") or r.get("model", ""), "score": r.get("score"),
                            "probability": r.get("probability") if calibrated else None} for r in analysis.get("results", [])[:5]]}


def execute_run(run_id, lease):
    run = FingerprintRun.objects.get(pk=run_id)
    if run.lease_token != lease or run.status != FingerprintRun.RUNNING:
        return
    try:
        detector = sdk.detector()
        challenges = sdk.generate_challenges()
        if len(challenges) != 3:
            raise FingerprintError("invalid_challenges", "本地挑战配置无效。")
        FingerprintRun.objects.filter(pk=run_id, lease_token=lease).update(bank_built_at=detector.built_at)
        # A restarted execution process is not allowed to resume an existing run.
        if run.samples.exists():
            raise FingerprintError("already_dispatched", "任务已进入执行阶段，未自动重试。")
        FingerprintSample.objects.bulk_create([FingerprintSample(run=run, ordinal=i + 1,
            challenge_id=c["id"], expected_count=c["expected_count"]) for i, c in enumerate(challenges)])
        outputs = []
        for ordinal, challenge in enumerate(challenges, 1):
            token = dispatch_sample(run_id, lease, ordinal)
            outcome = transport.sample(token, run.model_id, run.parameters, challenge,
                                       lambda: checked_cancel(run_id, lease))
            del token
            if not save_sample(run_id, lease, ordinal, outcome):
                return
            outputs.append({"text": outcome["text"], "expected_count": challenge["expected_count"]})
            if outcome["error_code"] == "cancelled":
                raise FingerprintError("cancelled", outcome["error_message"])
        if is_cancelled(run_id, lease):
            raise FingerprintError("cancelled", "检测已停止。")
        analysis = detector.analyze(outputs, strict=False)
        result = summarize_analysis(analysis)
        decision = result["verdict"]
        status = FingerprintRun.COMPLETED if decision == "scored" else FingerprintRun.PARTIAL if decision == "partial" else FingerprintRun.FAILED
        finish_run(run_id, lease, status, "" if decision in ("scored", "partial") else "unscorable",
                   "" if decision in ("scored", "partial") else "本轮响应无法给出可靠的参考排名。", result)
    except FingerprintError as exc:
        status = FingerprintRun.CANCELLED if exc.code == "cancelled" else FingerprintRun.INTERRUPTED if exc.code == "already_dispatched" else FingerprintRun.FAILED
        finish_run(run_id, lease, status, exc.code, exc.message)
    except Exception:
        # Neither arbitrary exception strings nor local variables (tokens/replies)
        # reach logs or persisted diagnostics.
        logger.error("Fingerprint execution failed for run %s", run_id)
        finish_run(run_id, lease, FingerprintRun.FAILED, "execution_error", "检测执行失败，未自动重试。")
