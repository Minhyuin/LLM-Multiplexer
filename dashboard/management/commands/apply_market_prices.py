"""Overwrite the price table with market list prices, in CNY per 1M tokens.

Rates follow the public list price of the closest real-world model, converted at
``1 USD = 7.1 CNY``.  The three columns are input / output / cached-input, which
is what :func:`usage.services.calculate_estimated_cost` consumes.

Run after deploying: ``manage.py apply_market_prices``.
"""

from decimal import Decimal

from django.core.management.base import BaseCommand

from dashboard.models import ModelPricing

USD_TO_CNY = Decimal("7.1")

# model name -> (input, output, cached input) in USD per 1M tokens
MARKET_PRICES_USD = {
    "claude-opus-5-5": ("15", "75", "1.5"),          # Claude Opus 4.x
    "claude-sonnet-5-5": ("3", "15", "0.3"),         # Claude Sonnet 4.x
    "claude-fable-5-2": ("1", "5", "0.1"),           # Claude Haiku 4.5
    "gpt-6.2-astra": ("1.25", "10", "0.125"),        # GPT-5.1
    "gpt-6.1-terra": ("1.25", "10", "0.125"),        # GPT-5.1
    "gpt-6.2-sol": ("0.25", "2", "0.025"),           # GPT-5.1 mini
    "gpt-6.1-sol": ("0.25", "2", "0.025"),           # GPT-5.1 mini
    "gemini-4-argon": ("2", "12", "0.2"),            # Gemini 3 Pro
    "gemini-4-hydrogen": ("0.3", "2.5", "0.03"),     # Gemini 3 Flash
    "gemini-4-carbon": ("2", "12", "0.2"),           # same tier as argon
    "glm-5.3-flash": ("0.6", "2.2", "0.11"),         # GLM-4.6
    "kimi-k3.1-pro": ("0.6", "2.5", "0.15"),         # Kimi K2
    "deepseek-v4.1-flash": ("0.28", "0.42", "0.028"),  # DeepSeek V3.2
    "grok-4.7": ("3", "15", "0.75"),                 # Grok 4
}


def cny(usd: str) -> Decimal:
    return (Decimal(usd) * USD_TO_CNY).quantize(Decimal("0.000001"))


class Command(BaseCommand):
    help = "按市场价（￥/1M tokens）覆盖模型价格表"

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="只打印，不写库")

    def handle(self, *args, **options):
        rows = []
        for name, (prompt, completion, cached) in sorted(MARKET_PRICES_USD.items()):
            rows.append((name, cny(prompt), cny(completion), cny(cached)))

        for name, prompt, completion, cached in rows:
            self.stdout.write(f"{name:22s} 输入 {prompt}  输出 {completion}  缓存 {cached}")
            if options["dry_run"]:
                continue
            ModelPricing.objects.update_or_create(
                model_name=name,
                defaults={
                    "prompt_price_per_million": prompt,
                    "completion_price_per_million": completion,
                    "cached_price_per_million": cached,
                },
            )

        if options["dry_run"]:
            self.stdout.write(self.style.WARNING("dry-run：未写库"))
            return
        self.stdout.write(self.style.SUCCESS(f"已更新 {len(rows)} 行价格（￥ / 1M tokens，汇率 {USD_TO_CNY}）"))
