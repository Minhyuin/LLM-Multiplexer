"""Retry pending registration-key CPA syncs.

Designed for a one-minute systemd timer.  Only ``RegistrationProvision`` rows
with ``synced_at IS NULL`` for active users and pending/active keys are selected;
the SQL filter also runs before ``--limit`` so ineligible rows cannot starve the
retry queue.  The service re-checks eligibility under a row lock, so a retry can
never re-authorise a member an administrator has just disabled or revoked.
"""
from django.core.management.base import BaseCommand

from api_keys.models import APIKey
from users.models import RegistrationProvision
from users.registration import sync_registration_key


class Command(BaseCommand):
    help = "重试未完成的注册密钥 CPA 同步（供每分钟 systemd timer 调用）"

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit",
            type=int,
            default=200,
            help="单次最多处理多少条，0 表示不限制（默认 200）",
        )

    def handle(self, *args, **options):
        limit = max(0, options["limit"])
        # Filter in SQL *before* slicing: disabled members and revoked/expired
        # keys must not consume the batch budget and starve later pending tasks.
        pending = (
            RegistrationProvision.objects.filter(
                synced_at__isnull=True,
                user__is_active=True,
                api_key__status__in=(APIKey.STATUS_PENDING, APIKey.STATUS_ACTIVE),
            )
            .select_related("user", "api_key")
            .order_by("created_at")
        )
        if limit:
            pending = pending[:limit]

        synced = failed = skipped = 0
        for provision in pending:
            # The service re-checks both conditions under a row lock; these
            # pre-filters are just cheap guards for rows that changed after the
            # queryset was evaluated.
            if not provision.user.is_active:
                skipped += 1
                continue
            if provision.api_key.status not in (APIKey.STATUS_PENDING, APIKey.STATUS_ACTIVE):
                skipped += 1
                continue
            if sync_registration_key(provision.user_id):
                synced += 1
            else:
                failed += 1

        message = f"注册密钥同步完成：成功 {synced}，失败 {failed}，跳过 {skipped}"
        if failed:
            self.stdout.write(self.style.WARNING(message))
        else:
            self.stdout.write(self.style.SUCCESS(message))
