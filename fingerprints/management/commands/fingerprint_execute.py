"""Internal one-run child. stdin is a parent keepalive pipe, never credentials."""
import os
import sys
import threading
import time
import uuid

from django.core.management.base import BaseCommand

from fingerprints.config import limit
from fingerprints.runner import execute_run


class Command(BaseCommand):
    help = "Internal fingerprint child process (run via run_fingerprint_worker)."

    def add_arguments(self, parser):
        parser.add_argument("run_id", type=uuid.UUID)
        parser.add_argument("lease", type=uuid.UUID)

    def handle(self, *args, **options):
        last_pulse = [time.monotonic()]
        started = last_pulse[0]

        def pipe_reader():
            while sys.stdin.buffer.read(1):
                last_pulse[0] = time.monotonic()
            os._exit(75)  # Parent gone; close sockets, never resume the request.

        def watchdog():
            while True:
                now = time.monotonic()
                if now - last_pulse[0] > 10 or now - started > limit("RUN_TIMEOUT", 420):
                    os._exit(75)
                time.sleep(0.2)

        threading.Thread(target=pipe_reader, daemon=True).start()
        threading.Thread(target=watchdog, daemon=True).start()
        execute_run(options["run_id"], options["lease"])
