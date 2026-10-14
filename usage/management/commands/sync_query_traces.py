from django.core.management.base import BaseCommand, CommandError

from usage.query_trace import prune_query_traces, sync_query_traces


class Command(BaseCommand):
    help = "从 cliproxy 请求日志导入 query 追溯记录（滚动 FIFO 存储）"

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=None, help="本次最多导入多少个日志文件")
        parser.add_argument("--dry-run", action="store_true", help="只解析不写库")
        parser.add_argument("--no-prune", action="store_true", help="跳过 FIFO 清理")
        parser.add_argument("--prune-only", action="store_true", help="只执行 FIFO 清理")

    def handle(self, *args, **options):
        if options["prune_only"]:
            expired, overflow = prune_query_traces()
            self.stdout.write(self.style.SUCCESS(f"清理完成：过期 {expired}，超量 {overflow}"))
            return

        try:
            job = sync_query_traces(
                limit=options["limit"],
                dry_run=options["dry_run"],
                prune=not options["no_prune"],
            )
        except Exception as exc:  # noqa: BLE001 - surface the reason to the operator
            raise CommandError(f"同步 query 追溯失败: {exc}")

        self.stdout.write(self.style.SUCCESS(
            f"同步完成：新增 {job.imported_count}，跳过 {job.skipped_count}，失败 {job.failed_count}"
        ))
        if job.summary:
            self.stdout.write(job.summary)
