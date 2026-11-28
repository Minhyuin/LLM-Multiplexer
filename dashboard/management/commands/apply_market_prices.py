"""Overwrite the model price table with real public list prices (CNY / 1M tokens).

Vendors that publish in CNY (Zhipu, Moonshot, DeepSeek) are stored verbatim;
USD-denominated vendors are converted at ``1 USD = 7.1 CNY``.

The cached column is the vendor's prompt-cache *read* price where it is
published; where it is not (Gemini 4 Argon/Carbon), 10% of the input price is
used as the conventional approximation.

Run after deploying: ``manage.py apply_market_prices``.
"""

from decimal import Decimal

from django.core.management.base import BaseCommand

from dashboard.models import ModelPricing

USD_TO_CNY = Decimal("7.1")

# model name -> (input, output, cached input), plus the unit the vendor quotes in
# and a one-line note on where the number comes from.
MARKET_PRICES = {
    # Anthropic (USD): Opus 5.5 $4/$20, Sonnet 5.5 $2/$10, Fable 5.2 $8/$40.
    "claude-opus-5-5": ("4", "20", "0.20", "USD", "Anthropic list, cache read 5%"),
    "claude-sonnet-5-5": ("2", "10", "0.10", "USD", "Anthropic list, cache read 5%"),
    "claude-fable-5-2": ("8", "40", "0.20", "USD", "Anthropic list, cache read 2.5%"),
    # OpenAI (USD) standard short-context rates.
    "gpt-6.2-astra": ("8", "40", "0.40", "USD", "OpenAI standard"),
    "gpt-6-astra": ("10", "50", "1.00", "USD", "OpenAI standard, previous flagship"),
    "gpt-6.1-terra": ("0.5", "2.5", "0.025", "USD", "OpenAI standard"),
    "gpt-6.2-sol": ("2", "10", "0.10", "USD", "OpenAI standard"),
    "gpt-6.1-sol": ("2", "10", "0.10", "USD", "OpenAI standard"),
    # Google (USD). Argon/Carbon are on a promo until 2026-11-30 and double after.
    "gemini-4-argon": ("2", "10", "0.20", "USD", "promo to 2026-11-30, then 4/20"),
    "gemini-4-carbon": ("2", "10", "0.20", "USD", "promo to 2026-11-30, then 4/20"),
    "gemini-4-hydrogen": ("1.5", "7.5", "0.15", "USD", "Google paid tier"),
    "gemini-4-neon": ("3", "15", "0.15", "USD", "Google list, cached input 95% off"),
    # xAI (USD) short-context rates.
    "grok-4.7": ("2", "6", "0.5", "USD", "xAI short context <200k"),
    # Vendors that already quote in CNY.
    "glm-5.3-flash": ("0.8", "2.8", "0.23", "CNY", "Zhipu bigmodel list"),
    "kimi-k3.1-pro": ("13", "68", "1.3", "CNY", "Moonshot list, cache hit price"),
    "deepseek-v4.1-flash": ("2", "8", "0.04", "CNY", "DeepSeek peak hours"),
}


def to_cny(value: str, unit: str) -> Decimal:
    amount = Decimal(value)
    if unit == "USD":
        amount *= USD_TO_CNY
    return amount.quantize(Decimal("0.000001"))


class Command(BaseCommand):
    help = "按市价（￥/1M tokens）覆盖模型价格表"

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="只打印，不写库")

    def handle(self, *args, **options):
        rows = []
        for name, (prompt, completion, cached, unit, note) in sorted(MARKET_PRICES.items()):
            rows.append((name, to_cny(prompt, unit), to_cny(completion, unit), to_cny(cached, unit), note))

        for name, prompt, completion, cached, note in rows:
            self.stdout.write(
                f"{name:22s} 输入 {prompt:>10}  输出 {completion:>10}  缓存 {cached:>10}   {note}"
            )
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
        self.stdout.write(self.style.SUCCESS(f"已更新 {len(rows)} 行价格（￥ / 1M tokens）"))
