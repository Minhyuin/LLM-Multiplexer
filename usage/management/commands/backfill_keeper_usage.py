"""Backfill historical usage from the local cpa-usage-keeper database.

The periodic sync only pulls a bounded batch. Use this once after fixing the
keeper connection to pull everything the portal missed.
"""
from django.core.management.base import BaseCommand

from usage.services import (
    fetch_keeper_events_sqlite,
    import_keeper_usage_events,
    keeper_db_available,
    keeper_db_path,
    latest_keeper_event_id,
)


class Command(BaseCommand):
    help = "Backfill usage records from the local cpa-usage-keeper SQLite database"

    def add_arguments(self, parser):
        parser.add_argument("--since-id", type=int, default=None,
                            help="Start after this keeper event id (default: highest already stored)")
        parser.add_argument("--batch", type=int, default=5000,
                            help="Rows per import batch")
        parser.add_argument("--dry-run", action="store_true",
                            help="Report what would be imported without writing")

    def handle(self, *args, **options):
        if not keeper_db_available():
            self.stderr.write(self.style.ERROR(f"keeper database not found: {keeper_db_path()}"))
            return

        since = options["since_id"]
        if since is None:
            since = latest_keeper_event_id()
        batch = options["batch"]

        self.stdout.write(f"keeper db : {keeper_db_path()}")
        self.stdout.write(f"since id  : {since}")

        total_imported = total_skipped = total_unmatched = 0
        while True:
            events = fetch_keeper_events_sqlite(since_id=since, limit=batch)
            if not events:
                break
            if options["dry_run"]:
                self.stdout.write(f"  would import {len(events)} events "
                                  f"(ids {events[0]['id']}..{events[-1]['id']})")
                since = int(events[-1]["id"])
                total_imported += len(events)
                continue

            job = import_keeper_usage_events(events, source_name="cpa-usage-keeper-db")
            total_imported += job.imported_count
            total_skipped += job.skipped_count
            if job.summary and "unmatched=" in job.summary:
                try:
                    total_unmatched += int(job.summary.split("unmatched=")[1].split()[0].split("\n")[0])
                except (IndexError, ValueError):
                    pass
            self.stdout.write(f"  ids {events[0]['id']}..{events[-1]['id']}  "
                              f"imported={job.imported_count} skipped={job.skipped_count}")
            since = int(events[-1]["id"])

        self.stdout.write(self.style.SUCCESS(
            f"done: imported={total_imported} skipped={total_skipped} unattributed={total_unmatched}"
        ))
