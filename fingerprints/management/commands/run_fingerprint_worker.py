"""Small supervised process pool backed by the portal database."""
import os
import signal
import subprocess
import sys
import time

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import close_old_connections

from fingerprints.config import enabled, limit
from fingerprints.models import FingerprintRun
from fingerprints.services import claim_run, finish_run, heartbeat, is_cancelled, maintenance


def stop_process(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
    if process.stdin:
        try:
            process.stdin.close()
        except OSError:
            pass  # The child may have exited before buffered keepalive flushed.


class Command(BaseCommand):
    help = "Run the portal fingerprint queue with bounded child processes."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Recover/prune and process current work, then exit.")

    def handle(self, *args, **options):
        stopping = [False]
        children = {}
        last_maintenance = 0
        previous_handlers = {}

        def stop(signum, frame):
            stopping[0] = True

        for sig in (signal.SIGTERM, signal.SIGINT):
            previous_handlers[sig] = signal.signal(sig, stop)
        try:
            while not stopping[0]:
                close_old_connections()
                now = time.monotonic()
                if now - last_maintenance >= 5:
                    maintenance()
                    last_maintenance = now
                for run_id, (process, lease, started) in list(children.items()):
                    cancelled = is_cancelled(run_id, lease)
                    timed_out = now - started > limit("RUN_TIMEOUT", 420)
                    if cancelled or timed_out or process.poll() is not None:
                        stop_process(process)
                        finish_run(run_id, lease, FingerprintRun.CANCELLED if cancelled else FingerprintRun.INTERRUPTED,
                                   "cancelled" if cancelled else "run_timeout" if timed_out else "worker_exit",
                                   "检测已停止；已发出的请求仍可能计费。" if cancelled else "执行进程结束或超时，未知请求未自动重试。")
                        del children[run_id]
                        continue
                    heartbeat(run_id, lease)
                    try:
                        process.stdin.write(b".")
                        process.stdin.flush()
                    except (BrokenPipeError, OSError):
                        stop_process(process)
                        finish_run(run_id, lease, FingerprintRun.INTERRUPTED, "worker_exit", "执行进程中断，未知请求未自动重试。")
                        del children[run_id]
                if enabled():
                    while len(children) < limit("CONCURRENCY", 2):
                        run = claim_run()
                        if run is None:
                            break
                        # No key or prompt in arguments/environment/stdout. Only
                        # public task identity and random execution lease token.
                        env = os.environ.copy()
                        env.update(OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1", MKL_NUM_THREADS="1")
                        process = None
                        try:
                            process = subprocess.Popen([sys.executable, str(settings.BASE_DIR / "manage.py"),
                                "fingerprint_execute", str(run.pk), str(run.lease_token)],
                                cwd=settings.BASE_DIR, env=env, stdin=subprocess.PIPE)
                            process.stdin.write(b".")
                            process.stdin.flush()
                            children[run.pk] = (process, run.lease_token, time.monotonic())
                        except OSError:
                            # Popen can succeed while the initial keepalive write
                            # fails. Reap that child before releasing its DB slot.
                            if process is not None:
                                stop_process(process)
                            finish_run(run.pk, run.lease_token, FingerprintRun.INTERRUPTED, "worker_start", "执行进程无法启动，未发送时预约次数已归还。")
                if options["once"] and not children:
                    break
                time.sleep(1)
        finally:
            for run_id, (process, lease, started) in children.items():
                stop_process(process)
                finish_run(run_id, lease, FingerprintRun.INTERRUPTED, "worker_stopped", "检测服务停止，未知请求未自动重试。")
            for sig, handler in previous_handlers.items():
                signal.signal(sig, handler)
