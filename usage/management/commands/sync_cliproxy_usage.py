from django.core.management.base import BaseCommand, CommandError

from quota.enforcement import enforce_daily_quota
from usage.services import sync_cliproxy_usage_records


class Command(BaseCommand):
    help = "从 cliproxy 管理接口同步 usage 数据"

    def add_arguments(self, parser):
        parser.add_argument(
            "--skip-quota",
            action="store_true",
            help="本轮不检查每日额度（只同步用量）",
        )

    def handle(self, *args, **options):
        try:
            job = sync_cliproxy_usage_records()
        except Exception as exc:
            raise CommandError(f"同步 cliproxy usage 失败: {exc}")

        self.stdout.write(self.style.SUCCESS(
            f"同步完成：成功 {job.imported_count}，跳过 {job.skipped_count}，失败 {job.failed_count}"
        ))
        if job.summary:
            self.stdout.write(job.summary)

        if options.get("skip_quota"):
            return
        # Enforcement must never turn a successful sync into a failed unit.
        try:
            stats = enforce_daily_quota()
        except Exception as exc:  # pragma: no cover - defensive
            self.stderr.write(f"额度检查失败（不影响用量同步）：{exc}")
            return
        if stats["blocked"] or stats["restored"] or stats["errors"] or stats["skipped"]:
            self.stdout.write(
                "额度检查：阻断 {blocked}，恢复 {restored}，错误 {errors}".format(**stats)
            )
