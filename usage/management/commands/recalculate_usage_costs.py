"""Re-price historical usage rows with the current price table.

``UsageLog.estimated_cost`` is written once, when a request is imported, so every
price change leaves the past priced at whatever the table said that day. This
command rewrites those rows from the gateway's ``token_breakdown`` (which is
present on 100% of the rows collected so far) and the current rates.

It streams rows with ``.iterator()`` and bulk-updates in chunks; it never issues a
query per row, because doing that once saturated the production disk and made the
host unreachable.

    manage.py recalculate_usage_costs --dry-run
    manage.py recalculate_usage_costs --days 90 --chunk 2000
"""

from datetime import timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from usage.models import UsageLog
from usage.services import build_rate_table, calculate_estimated_cost, resolve_rates


class Command(BaseCommand):
    help = "按当前价格表重算历史用量的 estimated_cost"

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=0, help="只重算最近 N 天，0 表示全部")
        parser.add_argument("--chunk", type=int, default=2000, help="每批更新的行数")
        parser.add_argument("--dry-run", action="store_true", help="只统计，不写库")
        parser.add_argument("--force", action="store_true", help="连价格未变动的行也重写")

    def handle(self, *args, **options):
        rates = build_rate_table()
        if not rates:
            self.stderr.write(self.style.ERROR("价格表为空，先运行 apply_market_prices"))
            return

        queryset = UsageLog.objects.all()
        days = options["days"]
        if days:
            queryset = queryset.filter(request_time__gte=timezone.now() - timedelta(days=days))
        queryset = queryset.order_by("pk")

        total = queryset.count()
        self.stdout.write(f"待重算 {total} 行；价格表 {len(rates)} 个模型")

        changed = 0
        unchanged = 0
        missing_pricing = {}
        delta = Decimal("0")
        pending = []
        seen = 0

        for row in queryset.iterator(chunk_size=options["chunk"]):
            seen += 1
            model_rates = resolve_rates(row.model_name, table=rates)
            if not model_rates.prompt and not model_rates.completion:
                missing_pricing[row.model_name] = missing_pricing.get(row.model_name, 0) + 1
            new_cost = calculate_estimated_cost(
                row.model_name, row.prompt_tokens, row.completion_tokens, row.raw_payload, rates=model_rates
            )
            old_cost = row.estimated_cost or Decimal("0")
            if new_cost == old_cost and not options["force"]:
                unchanged += 1
                continue
            delta += new_cost - old_cost
            row.estimated_cost = new_cost
            pending.append(row)
            changed += 1
            if len(pending) >= options["chunk"]:
                self._flush(pending, options["dry_run"])
                pending.clear()

        if pending:
            self._flush(pending, options["dry_run"])

        self.stdout.write(
            f"扫描 {seen} 行：改写 {changed}，未变 {unchanged}，"
            f"金额变化 ￥{delta:.6f}"
        )
        if missing_pricing:
            self.stdout.write(self.style.WARNING("以下模型没有价格，成本按 0 计算："))
            for name, count in sorted(missing_pricing.items(), key=lambda kv: -kv[1]):
                self.stdout.write(f"  {name or '(空名)':30s} {count} 行")
        if options["dry_run"]:
            self.stdout.write(self.style.WARNING("dry-run：未写库"))
        else:
            self.stdout.write(self.style.SUCCESS("历史成本已按当前价格重算"))
            self.stdout.write("提示：DailyUsageStat 预聚合仍按旧值，需要的话运行 rebuild_daily_stats --days 90")

    def _flush(self, rows, dry_run):
        if dry_run:
            return
        with transaction.atomic():
            UsageLog.objects.bulk_update(rows, ["estimated_cost"], batch_size=len(rows))
