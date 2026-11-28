"""Billing tests: the token breakdown decides the price, not the cached column."""

from decimal import Decimal

from django.test import TestCase

from dashboard.models import ModelPricing
from usage.services import calculate_estimated_cost, get_model_pricing, is_anthropic_model, resolve_rates


def breakdown(uncached, cache_read=0, cache_write=0, output=0):
    return {
        "token_breakdown": {
            "input": {
                "total_tokens": uncached + cache_read + cache_write,
                "uncached_tokens": uncached,
                "cache_read_tokens": cache_read,
                "cache_write_tokens": cache_write,
            },
            "output": {"total_tokens": output, "reasoning_tokens": 0, "non_reasoning_tokens": output},
            "total_tokens": uncached + cache_read + cache_write + output,
        }
    }


class NonAnthropicBillingTests(TestCase):
    def setUp(self):
        ModelPricing.objects.create(
            model_name="gpt-6.2-sol",
            prompt_price_per_million=Decimal("14.2"),
            completion_price_per_million=Decimal("71"),
            cached_price_per_million=Decimal("0.71"),
        )

    def test_uncached_read_and_output_are_priced_from_the_breakdown(self):
        payload = breakdown(uncached=1_000_000, cache_read=2_000_000, output=500_000)
        cost = calculate_estimated_cost("gpt-6.2-sol", 3_000_000, 500_000, payload)
        expected = Decimal("14.2") + Decimal("2") * Decimal("0.71") + Decimal("0.5") * Decimal("71")
        self.assertEqual(cost, expected)

    def test_flat_prompt_subtraction_is_right_for_inclusive_providers(self):
        # prompt_tokens already contains the cached read for these vendors.
        payload = breakdown(uncached=1_000_000, cache_read=2_000_000, output=0)
        cost = calculate_estimated_cost("gpt-6.2-sol", 3_000_000, 0, payload)
        self.assertEqual(cost, Decimal("14.2") + Decimal("2") * Decimal("0.71"))

    def test_cache_writes_are_billed_at_the_input_rate_when_the_row_says_so(self):
        # Moonshot bills a cache write at face value even though it reports no TTL.
        ModelPricing.objects.create(
            model_name="kimi-k3.1-pro",
            prompt_price_per_million=Decimal("13"),
            completion_price_per_million=Decimal("68"),
            cached_price_per_million=Decimal("1.3"),
            cache_write_price_per_million=Decimal("13"),
        )
        payload = breakdown(uncached=0, cache_read=0, cache_write=1_000_000, output=0)
        cost = calculate_estimated_cost("kimi-k3.1-pro", 1_000_000, 0, payload)
        self.assertEqual(cost, Decimal("13"))

    def test_provider_prefix_still_resolves(self):
        prompt, completion, cached = get_model_pricing("openai/gpt-6.2-sol")
        self.assertEqual((prompt, completion, cached), (Decimal("14.2"), Decimal("71"), Decimal("0.71")))


class AnthropicBillingTests(TestCase):
    def setUp(self):
        ModelPricing.objects.create(
            model_name="claude-opus-5-5",
            prompt_price_per_million=Decimal("28.4"),
            completion_price_per_million=Decimal("142"),
            cached_price_per_million=Decimal("2.84"),
        )

    def test_the_model_is_recognised_by_name(self):
        self.assertTrue(is_anthropic_model("claude-opus-5-5"))
        self.assertTrue(is_anthropic_model("anthropic/claude-sonnet-5-5"))
        self.assertFalse(is_anthropic_model("gpt-6.2-sol"))

    def test_prompt_tokens_are_already_uncached(self):
        # Anthropic reports prompt_tokens == uncached_tokens, so the historical
        # `prompt - cached` subtraction billed the new input at zero.
        payload = breakdown(uncached=1_000_000, cache_read=5_000_000, output=100_000)
        cost = calculate_estimated_cost("claude-opus-5-5", 1_000_000, 100_000, payload)
        expected = Decimal("28.4") + Decimal("5") * Decimal("2.84") + Decimal("0.1") * Decimal("142")
        self.assertEqual(cost, expected)

    def test_cache_writes_use_the_vendor_multipliers_when_the_table_leaves_them_blank(self):
        payload = breakdown(uncached=0, cache_read=0, cache_write=1_000_000, output=0)
        payload["cache_details"] = {"by_ttl": {"5m": 1_000_000, "1h": 0}}
        cost = calculate_estimated_cost("claude-opus-5-5", 0, 0, payload)
        self.assertEqual(cost, Decimal("28.4") * Decimal("1.25"))

    def test_the_one_hour_ttl_costs_double(self):
        payload = breakdown(uncached=0, cache_read=0, cache_write=1_000_000, output=0)
        payload["cache_details"] = {"by_ttl": {"5m": 0, "1h": 1_000_000}}
        cost = calculate_estimated_cost("claude-opus-5-5", 0, 0, payload)
        self.assertEqual(cost, Decimal("28.4") * Decimal("2"))

    def test_mixed_ttls_are_split(self):
        payload = breakdown(uncached=0, cache_read=0, cache_write=2_000_000, output=0)
        payload["cache_details"] = {"by_ttl": {"5m": 1_500_000, "1h": 500_000}}
        cost = calculate_estimated_cost("claude-opus-5-5", 0, 0, payload)
        expected = Decimal("1.5") * Decimal("28.4") * Decimal("1.25") + Decimal("0.5") * Decimal("28.4") * Decimal("2")
        self.assertEqual(cost, expected)

    def test_a_ttl_total_that_disagrees_with_the_write_count_is_clamped(self):
        payload = breakdown(uncached=0, cache_read=0, cache_write=1_000_000, output=0)
        payload["cache_details"] = {"by_ttl": {"5m": 0, "1h": 4_000_000}}
        cost = calculate_estimated_cost("claude-opus-5-5", 0, 0, payload)
        self.assertEqual(cost, Decimal("28.4") * Decimal("2"))


class LegacyRowTests(TestCase):
    """Rows captured before the gateway emitted a breakdown must still price."""

    def setUp(self):
        ModelPricing.objects.create(
            model_name="gpt-6.2-sol",
            prompt_price_per_million=Decimal("14.2"),
            completion_price_per_million=Decimal("71"),
            cached_price_per_million=Decimal("0.71"),
        )

    def test_without_a_breakdown_the_old_subtraction_still_applies(self):
        payload = {"tokens": {"cached_tokens": 2_000_000}}
        cost = calculate_estimated_cost("gpt-6.2-sol", 3_000_000, 0, payload)
        self.assertEqual(cost, Decimal("14.2") + Decimal("2") * Decimal("0.71"))

    def test_unknown_models_cost_zero(self):
        self.assertEqual(resolve_rates("nope/nothing").prompt, Decimal("0"))


class RecalculateCommandTests(TestCase):
    def _log(self, model, payload, prompt, completion, cost, when):
        from usage.models import UsageLog

        return UsageLog.objects.create(
            request_id=f"req-{when.timestamp()}-{model}",
            model_name=model,
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=prompt + completion,
            estimated_cost=Decimal(cost),
            request_time=when,
            raw_payload=payload,
        )

    def test_command_rewrites_the_stored_cost(self):
        from django.core.management import call_command
        from django.utils import timezone

        ModelPricing.objects.create(
            model_name="gpt-6.2-sol",
            prompt_price_per_million=Decimal("14.2"),
            completion_price_per_million=Decimal("71"),
            cached_price_per_million=Decimal("0.71"),
        )
        row = self._log("gpt-6.2-sol", breakdown(1_000_000, output=0), 1_000_000, 0, "0", timezone.now())
        call_command("recalculate_usage_costs")
        row.refresh_from_db()
        self.assertEqual(row.estimated_cost, Decimal("14.2"))

    def test_dry_run_changes_nothing(self):
        from django.core.management import call_command
        from django.utils import timezone

        ModelPricing.objects.create(
            model_name="gpt-6.2-sol",
            prompt_price_per_million=Decimal("14.2"),
            completion_price_per_million=Decimal("71"),
            cached_price_per_million=Decimal("0.71"),
        )
        row = self._log("gpt-6.2-sol", breakdown(1_000_000), 1_000_000, 0, "0", timezone.now())
        call_command("recalculate_usage_costs", "--dry-run")
        row.refresh_from_db()
        self.assertEqual(row.estimated_cost, Decimal("0"))

    def test_rows_are_not_written_when_the_price_is_unchanged(self):
        from django.core.management import call_command
        from django.utils import timezone

        ModelPricing.objects.create(
            model_name="gpt-6.2-sol",
            prompt_price_per_million=Decimal("14.2"),
            completion_price_per_million=Decimal("71"),
            cached_price_per_million=Decimal("0.71"),
        )
        row = self._log("gpt-6.2-sol", breakdown(1_000_000), 1_000_000, 0, "14.2", timezone.now())
        call_command("recalculate_usage_costs")
        row.refresh_from_db()
        self.assertEqual(row.estimated_cost, Decimal("14.2"))
