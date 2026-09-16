"""Cross-protocol provider usage normalization and conservative cost estimates."""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from patchloop.providers.base import EncodedRequest, ModelUsage
from patchloop.providers.contracts import (
    ProviderError,
    ProviderErrorKind,
    ProviderPricing,
    ProviderProtocol,
)


class PriceSnapshot(BaseModel):
    """Immutable prices used for one task's accounting decisions."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = Field(min_length=1, max_length=128)
    input_per_million: float = Field(ge=0)
    output_per_million: float = Field(ge=0)
    cached_input_per_million: float | None = Field(default=None, ge=0)
    cache_write_input_per_million: float | None = Field(default=None, ge=0)
    legacy_default: bool = False

    @classmethod
    def from_pricing(
        cls, pricing: ProviderPricing, *, legacy_default: bool = False
    ) -> PriceSnapshot:
        return cls(**pricing.model_dump(), legacy_default=legacy_default)

    def estimate(
        self,
        *,
        input_tokens: int,
        output_tokens: int,
        cache_hit_tokens: int | None = None,
        cache_miss_tokens: int | None = None,
        cache_write_tokens: int | None = None,
    ) -> float:
        if (
            cache_hit_tokens is not None
            and cache_miss_tokens is not None
            and cache_hit_tokens + cache_miss_tokens == input_tokens
        ):
            cached_price = (
                self.input_per_million
                if self.cached_input_per_million is None
                else self.cached_input_per_million
            )
            write_tokens = cache_write_tokens or 0
            if write_tokens > cache_miss_tokens:
                return (
                    input_tokens * self.input_per_million
                    + output_tokens * self.output_per_million
                ) / 1_000_000
            write_price = (
                self.input_per_million
                if self.cache_write_input_per_million is None
                else self.cache_write_input_per_million
            )
            input_cost = (
                cache_hit_tokens * cached_price
                + (cache_miss_tokens - write_tokens) * self.input_per_million
                + write_tokens * write_price
            )
        else:
            input_cost = input_tokens * self.input_per_million
        return (input_cost + output_tokens * self.output_per_million) / 1_000_000


class UsageNormalizer:
    """Map Chat Completions and Responses usage into the stable model."""

    @classmethod
    def normalize(
        cls,
        protocol: ProviderProtocol,
        raw_usage: Any,
        pricing: ProviderPricing | PriceSnapshot | None,
        *,
        legacy_pricing: bool = False,
    ) -> ModelUsage:
        if raw_usage is None:
            return cls.unknown()
        if not isinstance(raw_usage, dict):
            raise cls._invalid("provider returned invalid usage data")
        prices = cls._prices(pricing, legacy_default=legacy_pricing)
        if protocol is ProviderProtocol.CHAT_COMPLETIONS:
            return cls._chat(raw_usage, prices)
        return cls._responses(raw_usage, prices)

    @staticmethod
    def unknown() -> ModelUsage:
        return ModelUsage(
            input_tokens_reported=False,
            output_tokens_reported=False,
            cost_status="unknown",
        )

    @classmethod
    def reserve(
        cls,
        encoded: EncodedRequest,
        max_output_tokens: int,
        pricing: ProviderPricing | PriceSnapshot,
    ) -> float:
        """Reserve an intentionally conservative amount before a network attempt."""

        prices = cls._prices(pricing)
        assert prices is not None
        body = json.dumps(
            encoded.body,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        messages = encoded.body.get("messages", encoded.body.get("input", []))
        message_count = len(messages) if isinstance(messages, list) else 1
        input_proxy = len(body) + 64 * message_count
        if not cls._is_token_count(max_output_tokens):
            raise ValueError("max_output_tokens must be a non-negative integer")
        input_price = max(
            prices.input_per_million,
            prices.cache_write_input_per_million or prices.input_per_million,
        )
        return (
            input_proxy * input_price + max_output_tokens * prices.output_per_million
        ) / 1_000_000

    @classmethod
    def _chat(cls, raw: dict[str, Any], prices: PriceSnapshot | None) -> ModelUsage:
        details = raw.get("prompt_tokens_details", {})
        if details is None:
            details = {}
        if not isinstance(details, dict):
            raise cls._invalid("provider returned invalid prompt usage details")
        input_tokens = cls._integer(raw, "prompt_tokens")
        output_tokens = cls._integer(raw, "completion_tokens")
        standard_cache_hit = cls._integer(details, "cached_tokens")
        cache_hit = cls._integer(raw, "prompt_cache_hit_tokens")
        if cache_hit is None:
            cache_hit = standard_cache_hit
        cache_miss = cls._integer(raw, "prompt_cache_miss_tokens")
        cache_miss_source: Literal["reported", "derived"] | None = (
            "reported" if cache_miss is not None else None
        )
        # Standard Chat usage includes cached input in prompt_tokens. Custom
        # top-level cache fields alone do not establish that same accounting.
        if (
            cache_miss is None
            and input_tokens is not None
            and standard_cache_hit is not None
            and cache_hit == standard_cache_hit
            and standard_cache_hit <= input_tokens
        ):
            cache_miss = input_tokens - standard_cache_hit
            cache_miss_source = "derived"
        cache_write = cls._integer(details, "cache_write_tokens")
        if cache_write is None:
            cache_write = cls._integer(raw, "cache_write_tokens")
        if cache_write is None:
            cache_write = cls._integer(raw, "prompt_cache_write_tokens")
        return cls._usage(
            input_tokens,
            output_tokens,
            cache_hit,
            cache_miss,
            cache_write,
            prices,
            cache_hit_source="reported" if cache_hit is not None else None,
            cache_miss_source=cache_miss_source,
        )

    @classmethod
    def _responses(cls, raw: dict[str, Any], prices: PriceSnapshot | None) -> ModelUsage:
        input_tokens = cls._integer(raw, "input_tokens")
        output_tokens = cls._integer(raw, "output_tokens")
        input_details = raw.get("input_tokens_details", {})
        output_details = raw.get("output_tokens_details", {})
        if input_details is None:
            input_details = {}
        if output_details is None:
            output_details = {}
        if not isinstance(input_details, dict) or not isinstance(output_details, dict):
            raise cls._invalid("provider returned invalid usage details")
        cache_hit = cls._integer(input_details, "cached_tokens")
        cache_write = cls._integer(input_details, "cache_write_tokens")
        reasoning = cls._integer(output_details, "reasoning_tokens")
        cache_miss = None
        cache_miss_source: Literal["derived"] | None = None
        if input_tokens is not None and cache_hit is not None and cache_hit <= input_tokens:
            cache_miss = input_tokens - cache_hit
            cache_miss_source = "derived"
        return cls._usage(
            input_tokens,
            output_tokens,
            cache_hit,
            cache_miss,
            cache_write,
            prices,
            reasoning_output_tokens=reasoning,
            cache_hit_source="reported" if cache_hit is not None else None,
            cache_miss_source=cache_miss_source,
        )

    @classmethod
    def _usage(
        cls,
        input_tokens: int | None,
        output_tokens: int | None,
        cache_hit: int | None,
        cache_miss: int | None,
        cache_write: int | None,
        prices: PriceSnapshot | None,
        *,
        reasoning_output_tokens: int | None = None,
        cache_hit_source: Literal["reported", "derived"] | None = None,
        cache_miss_source: Literal["reported", "derived"] | None = None,
    ) -> ModelUsage:
        cost = 0.0
        status: Literal["estimated", "unknown", "legacy"] = "unknown"
        cache_write_usage_complete = (
            prices is None
            or prices.cache_write_input_per_million is None
            or (
                cache_hit is not None
                and cache_miss is not None
                and cache_write is not None
                and input_tokens is not None
                and cache_hit + cache_miss == input_tokens
                and cache_write <= cache_miss
            )
        )
        if (
            prices is not None
            and input_tokens is not None
            and output_tokens is not None
            and cache_write_usage_complete
        ):
            cost = prices.estimate(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_hit_tokens=cache_hit,
                cache_miss_tokens=cache_miss,
                cache_write_tokens=cache_write,
            )
            status = "legacy" if prices.legacy_default else "estimated"
        return ModelUsage(
            input_tokens=input_tokens or 0,
            output_tokens=output_tokens or 0,
            cost_usd=cost,
            cache_hit_tokens=cache_hit,
            cache_miss_tokens=cache_miss,
            cache_write_tokens=cache_write,
            reasoning_output_tokens=reasoning_output_tokens,
            input_tokens_reported=input_tokens is not None,
            output_tokens_reported=output_tokens is not None,
            cache_hit_tokens_source=cache_hit_source,
            cache_miss_tokens_source=cache_miss_source,
            cost_status=status,
            pricing_version=None if prices is None else prices.version,
        )

    @classmethod
    def _integer(cls, usage: dict[str, Any], field: str) -> int | None:
        if field not in usage or usage[field] is None:
            return None
        value = usage[field]
        if not cls._is_token_count(value):
            raise cls._invalid("provider returned invalid token usage")
        return int(value)

    @staticmethod
    def _is_token_count(value: Any) -> bool:
        return isinstance(value, int) and not isinstance(value, bool) and value >= 0

    @staticmethod
    def _prices(
        pricing: ProviderPricing | PriceSnapshot | None, *, legacy_default: bool = False
    ) -> PriceSnapshot | None:
        if pricing is None:
            return None
        if isinstance(pricing, PriceSnapshot):
            return pricing
        return PriceSnapshot.from_pricing(pricing, legacy_default=legacy_default)

    @staticmethod
    def _invalid(message: str) -> ProviderError:
        return ProviderError(
            ProviderErrorKind.PROTOCOL,
            message,
            request_sent=True,
            usage_unknown=True,
        )
