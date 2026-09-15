import pytest

from patchloop.providers import (
    EncodedRequest,
    ProviderError,
    ProviderPricing,
    ProviderProtocol,
)
from patchloop.providers.usage import PriceSnapshot, UsageNormalizer


def pricing() -> ProviderPricing:
    return ProviderPricing(
        version="test-v1",
        input_per_million=2.0,
        output_per_million=8.0,
        cached_input_per_million=0.5,
    )


def test_chat_usage_preserves_reported_cache_fields_and_estimates_cost() -> None:
    usage = UsageNormalizer.normalize(
        ProviderProtocol.CHAT_COMPLETIONS,
        {
            "prompt_tokens": 100,
            "completion_tokens": 10,
            "prompt_cache_hit_tokens": 75,
            "prompt_cache_miss_tokens": 25,
        },
        pricing(),
    )

    assert usage.cache_hit_tokens_source == "reported"
    assert usage.cache_miss_tokens_source == "reported"
    assert usage.cost_status == "estimated"
    assert usage.cost_usd == pytest.approx((75 * 0.5 + 25 * 2 + 10 * 8) / 1_000_000)


def test_responses_usage_derives_miss_and_does_not_add_reasoning_twice() -> None:
    usage = UsageNormalizer.normalize(
        ProviderProtocol.RESPONSES,
        {
            "input_tokens": 40,
            "output_tokens": 12,
            "input_tokens_details": {"cached_tokens": 30},
            "output_tokens_details": {"reasoning_tokens": 7},
        },
        pricing(),
    )

    assert usage.cache_miss_tokens == 10
    assert usage.cache_miss_tokens_source == "derived"
    assert usage.reasoning_output_tokens == 7
    assert usage.output_tokens == 12
    assert usage.cost_usd == pytest.approx((30 * 0.5 + 10 * 2 + 12 * 8) / 1_000_000)


def test_missing_pricing_and_missing_usage_are_not_known_zero_cost() -> None:
    unpriced = UsageNormalizer.normalize(
        ProviderProtocol.RESPONSES,
        {"input_tokens": 5, "output_tokens": 1},
        None,
    )
    absent = UsageNormalizer.normalize(ProviderProtocol.CHAT_COMPLETIONS, None, pricing())

    assert unpriced.cost_usd == 0
    assert unpriced.cost_status == "unknown"
    assert absent.input_tokens_reported is False
    assert absent.output_tokens_reported is False
    assert absent.cost_status == "unknown"


@pytest.mark.parametrize("cached", [0, 75, 100])
def test_standard_chat_usage_derives_uncached_input_from_reported_totals(cached: int) -> None:
    usage = UsageNormalizer.normalize(
        ProviderProtocol.CHAT_COMPLETIONS,
        {
            "prompt_tokens": 100,
            "completion_tokens": 10,
            "prompt_tokens_details": {"cached_tokens": cached},
        },
        pricing(),
    )
    assert usage.cache_hit_tokens_source == "reported"
    assert usage.cache_miss_tokens == 100 - cached
    assert usage.cache_miss_tokens_source == "derived"
    assert usage.cost_usd == pytest.approx((cached * 0.5 + (100 - cached) * 2 + 10 * 8) / 1_000_000)


@pytest.mark.parametrize(
    "raw",
    [
        {"prompt_tokens": 100},
        {"prompt_tokens_details": {"cached_tokens": 75}},
        {"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 101}},
        {"prompt_tokens": 100, "prompt_cache_hit_tokens": 75},
        {
            "prompt_tokens": 100,
            "prompt_cache_hit_tokens": 70,
            "prompt_tokens_details": {"cached_tokens": 75},
        },
    ],
)
def test_chat_usage_does_not_derive_miss_from_missing_or_ambiguous_fields(raw) -> None:
    usage = UsageNormalizer.normalize(ProviderProtocol.CHAT_COMPLETIONS, raw, pricing())
    assert usage.cache_miss_tokens is None
    assert usage.cache_miss_tokens_source is None


def test_chat_usage_preserves_explicit_miss_even_when_it_disagrees_with_totals() -> None:
    usage = UsageNormalizer.normalize(
        ProviderProtocol.CHAT_COMPLETIONS,
        {
            "prompt_tokens": 100,
            "completion_tokens": 10,
            "prompt_tokens_details": {"cached_tokens": 75},
            "prompt_cache_miss_tokens": 10,
        },
        pricing(),
    )
    assert usage.cache_miss_tokens == 10
    assert usage.cache_miss_tokens_source == "reported"
    # Inconsistent reported counts cannot claim a cached-input discount.
    assert usage.cost_usd == pytest.approx((100 * 2 + 10 * 8) / 1_000_000)


def test_boolean_token_counts_are_rejected() -> None:
    with pytest.raises(ProviderError, match="invalid token usage"):
        UsageNormalizer.normalize(
            ProviderProtocol.CHAT_COMPLETIONS,
            {"prompt_tokens": True, "completion_tokens": 1},
            pricing(),
        )


def test_reservation_uses_utf8_bytes_framing_and_uncached_prices() -> None:
    snapshot = PriceSnapshot.from_pricing(pricing())
    encoded = EncodedRequest(
        path="/responses",
        body={
            "input": [{"role": "user", "content": "你好"}],
            "max_output_tokens": 10,
        },
    )

    reservation = UsageNormalizer.reserve(encoded, 10, snapshot)

    assert reservation > 10 * snapshot.output_per_million / 1_000_000
