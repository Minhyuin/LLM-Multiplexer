"""Overwrite the model price table with real public list prices (￥ / 1M tokens).

Vendors that publish in CNY (Zhipu, Moonshot, DeepSeek) are stored verbatim;
USD-denominated vendors are converted at ``1 USD = 7.1 CNY``.

Four numbers are stored per model: the input price, the output price, the price
of reading a cached prefix, and what it costs to *write* a cache entry. The write
price is not universal:

* Anthropic charges 1.25x the input rate for the 5-minute TTL and 2x for the
  1-hour TTL, and reports the two buckets separately.
* OpenAI charges 1.25x for the 30-minute cache and 2.5x for the 12-hour one.
* Gemini's implicit caching has no write charge.
* DeepSeek, Zhipu and Moonshot bill a cache write at the plain input rate.

Run after deploying: ``manage.py apply_market_prices``.
"""

from decimal import Decimal

from django.core.management.base import BaseCommand

from dashboard.models import ModelPricing

USD_TO_CNY = Decimal("7.1")

# model name -> (input, output, cached read, cache write, 1h/12h cache write,
#                unit the vendor quotes in, where the number comes from)
MARKET_PRICES = {
    # Anthropic (USD): Opus 5.5 $4/$20, Sonnet 5.5 $2/$10, Fable 5.2 $8/$40.
    # Cache read is 10% of input; writes are 1.25x (5m) and 2x (1h).
    "claude-opus-5-5": ("4", "20", "0.40", "5", "8", "USD", "Anthropic list; write 1.25x/2x"),
    "claude-sonnet-5-5": ("2", "10", "0.20", "2.5", "4", "USD", "Anthropic list; write 1.25x/2x"),
    "claude-fable-5-2": ("8", "40", "0.80", "10", "16", "USD", "Anthropic list; write 1.25x/2x"),
    # OpenAI (USD) standard rates. Cached input is 10% of input; cache writes are
    # $10/$12.50/$2.50/$2.50/$2.50/$0.625 per 1M for 30m and 2x that for 12h.
    "gpt-6.2-astra": ("8", "40", "0.40", "10", "20", "USD", "OpenAI standard; write 1.25x/2.5x"),
    "gpt-6-astra": ("10", "50", "1.00", "12.5", "25", "USD", "OpenAI previous flagship; write 1.25x/2.5x"),
    "gpt-6.1-terra": ("0.5", "2.5", "0.025", "0.625", "1.25", "USD", "OpenAI standard; write 1.25x/2.5x"),
    "gpt-6.2-sol": ("2", "10", "0.10", "2.5", "5", "USD", "OpenAI standard; write 1.25x/2.5x"),
    "gpt-6.1-sol": ("2", "10", "0.10", "2.5", "5", "USD", "OpenAI standard; write 1.25x/2.5x"),
    # Google (USD). Argon/Carbon are on a promo until 2026-11-30 and double after.
    # Implicit caching costs nothing to write.
    "gemini-4-argon": ("2", "10", "0.20", "0", "0", "USD", "promo to 2026-11-30, then 4/20"),
    "gemini-4-carbon": ("2", "10", "0.20", "0", "0", "USD", "promo to 2026-11-30, then 4/20"),
    "gemini-4-hydrogen": ("1.5", "7.5", "0.15", "0", "0", "USD", "Google paid tier"),
    "gemini-4-neon": ("3", "15", "0.15", "0", "0", "USD", "Google list, cached input 95% off"),
    # xAI (USD) short-context rates; cache writes are not billed.
    "grok-4.7": ("2", "6", "0.5", "0", "0", "USD", "xAI short context <200k"),
    # Vendors that already quote in CNY and bill writes at the input rate.
    "glm-5.3-flash": ("0.8", "2.8", "0.23", "0.8", "0", "CNY", "Zhipu bigmodel list"),
    "kimi-k3.1-pro": ("13", "68", "1.3", "13", "0", "CNY", "Moonshot list, cache hit price"),
    "deepseek-v4.1-flash": ("2", "8", "0.04", "2", "0", "CNY", "DeepSeek peak hours"),
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
        for name, (prompt, completion, cached, write, write_1h, unit, note) in sorted(MARKET_PRICES.items()):
            rows.append(
                (
                    name,
                    to_cny(prompt, unit),
                    to_cny(completion, unit),
                    to_cny(cached, unit),
                    to_cny(write, unit),
                    to_cny(write_1h, unit),
                    note,
                )
            )

        for name, prompt, completion, cached, write, write_1h, note in rows:
            self.stdout.write(
                f"{name:22s} 输入 {prompt:>10}  输出 {completion:>10}  缓存 {cached:>10}  "
                f"写缓存 {write:>10}  长缓存写 {write_1h:>10}   {note}"
            )
            if options["dry_run"]:
                continue
            ModelPricing.objects.update_or_create(
                model_name=name,
                defaults={
                    "prompt_price_per_million": prompt,
                    "completion_price_per_million": completion,
                    "cached_price_per_million": cached,
                    "cache_write_price_per_million": write,
                    "cache_write_1h_price_per_million": write_1h,
                },
            )

        if options["dry_run"]:
            self.stdout.write(self.style.WARNING("dry-run：未写库"))
            return
        self.stdout.write(self.style.SUCCESS(f"已更新 {len(rows)} 行价格（￥ / 1M tokens）"))
