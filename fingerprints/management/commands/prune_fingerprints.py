from django.core.management.base import BaseCommand

from fingerprints.services import maintenance


class Command(BaseCommand):
    help = "Expire queued/stale tasks and prune retained fingerprint history."

    def handle(self, *args, **options):
        maintenance()
        self.stdout.write("Fingerprint maintenance complete.")
