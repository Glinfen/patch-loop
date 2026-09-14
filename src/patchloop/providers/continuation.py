"""Safe storage, diagnostic projection, and replay validation for provider state."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel

from patchloop.providers.base import ModelMessage, ModelResponse
from patchloop.providers.contracts import (
    ChatDialect,
    ProviderBinding,
    ProviderContinuation,
    ProviderProtocol,
    ValidatedResponseItem,
)
from patchloop.security import SecretRedactor


class ContinuationUnavailable(ValueError):
    """Raised when persisted provider state cannot safely be replayed."""


class ContinuationIntegrityError(ContinuationUnavailable):
    """Raised when a stored provider response no longer matches its digest."""


class ContinuationCodec:
    """Apply the same redaction and replay rules to every continuation path.

    Opaque Responses ``encrypted_content`` is copied byte-for-byte. Every
    human-readable or executable field still passes through the regular secret
    redactor, and a mutation makes the continuation ineligible for replay.
    """

    def __init__(self, redactor: SecretRedactor | None = None) -> None:
        self.redactor = redactor or SecretRedactor()

    def to_storage(self, value: Any) -> Any:
        """Return a redacted storage projection, preserving opaque ciphertext."""

        if isinstance(value, ProviderContinuation):
            return self._continuation_to_storage(value)
        if isinstance(value, BaseModel):
            payload = self.to_storage(value.model_dump(mode="json"))
            return type(value).model_validate(payload)
        if isinstance(value, Mapping):
            output: dict[str, Any] = {}
            for key, item in value.items():
                if key == "continuation" and item is not None:
                    continuation = ProviderContinuation.model_validate(item)
                    output[str(key)] = self._continuation_to_storage(continuation).model_dump(
                        mode="json"
                    )
                else:
                    output[str(key)] = self.to_storage(item)
            return output
        if isinstance(value, list):
            return [self.to_storage(item) for item in value]
        if isinstance(value, tuple):
            return tuple(self.to_storage(item) for item in value)
        return self.redactor.redact(value)

    def to_public(self, value: Any) -> Any:
        """Return a secret-safe diagnostic projection with continuation digests."""

        if isinstance(value, ProviderContinuation):
            return self._continuation_to_public(value)
        if isinstance(value, BaseModel):
            return self.to_public(value.model_dump(mode="json"))
        if isinstance(value, Mapping):
            output: dict[str, Any] = {}
            for key, item in value.items():
                if key == "continuation" and item is not None:
                    output[str(key)] = self._continuation_to_public(
                        ProviderContinuation.model_validate(item)
                    )
                else:
                    output[str(key)] = self.to_public(item)
            return output
        if isinstance(value, list):
            return [self.to_public(item) for item in value]
        if isinstance(value, tuple):
            return [self.to_public(item) for item in value]
        return self.redactor.redact(value)

    def validate_for_replay(
        self,
        response: ModelResponse,
        *,
        binding: ProviderBinding,
        binding_fingerprint: str,
        response_sha256: str | None = None,
    ) -> ModelResponse:
        """Validate integrity and profile before a response enters a replay path."""

        if binding_fingerprint != binding.fingerprint:
            raise ContinuationUnavailable("provider response belongs to another profile")
        if response_sha256 is not None and self.response_digest(response) != response_sha256:
            raise ContinuationIntegrityError("stored provider response failed integrity check")
        continuation = response.continuation
        if continuation is None:
            return response
        if not continuation.replayable:
            raise ContinuationUnavailable("provider continuation was redacted and cannot replay")
        if continuation.deepseek_reasoning_content is None and not continuation.responses_items:
            raise ContinuationUnavailable("provider continuation contains no replayable state")
        if binding.protocol is ProviderProtocol.RESPONSES:
            if (
                continuation.deepseek_reasoning_content is not None
                or not continuation.responses_items
            ):
                raise ContinuationUnavailable("Chat continuation cannot replay with Responses")
        elif continuation.responses_items:
            raise ContinuationUnavailable("Responses continuation cannot replay with Chat")
        elif (
            continuation.deepseek_reasoning_content is not None
            and binding.dialect is not ChatDialect.DEEPSEEK
        ):
            raise ContinuationUnavailable(
                "DeepSeek continuation cannot replay with this Chat profile"
            )
        return response

    @staticmethod
    def input_digest(value: Any) -> str:
        """Hash canonical request input without retaining its contents."""

        payload = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
        return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()

    @staticmethod
    def response_digest(response: ModelResponse) -> str:
        return hashlib.sha256(
            _canonical_json(response.model_dump(mode="json")).encode()
        ).hexdigest()

    def _continuation_to_storage(self, continuation: ProviderContinuation) -> ProviderContinuation:
        replayable = continuation.replayable
        deepseek = continuation.deepseek_reasoning_content
        if deepseek is not None:
            safe = self.redactor.redact_text(deepseek)
            replayable = replayable and safe == deepseek
            deepseek = safe

        response_items: list[ValidatedResponseItem] = []
        for response_item in continuation.responses_items:
            original = response_item.model_dump(mode="json")
            safe = _redact_preserving_encrypted_content(original, self.redactor)
            replayable = replayable and safe == original
            response_items.append(ValidatedResponseItem.model_validate(safe))
        return ProviderContinuation(
            deepseek_reasoning_content=deepseek,
            responses_items=tuple(response_items),
            replayable=replayable,
        )

    def _continuation_to_public(self, continuation: ProviderContinuation) -> dict[str, Any]:
        safe = self._continuation_to_storage(continuation)
        if safe.deepseek_reasoning_content is not None:
            encoded = safe.deepseek_reasoning_content.encode("utf-8")
            return {
                "protocol": "chat_completions",
                "replayable": safe.replayable,
                "items": [
                    {
                        "type": "deepseek_reasoning",
                        "length": len(encoded),
                        "sha256": hashlib.sha256(encoded).hexdigest(),
                    }
                ],
            }
        return {
            "protocol": "responses",
            "replayable": safe.replayable,
            "items": [
                {
                    "type": item.type,
                    "length": len(_canonical_json(item.model_dump(mode="json")).encode("utf-8")),
                    "sha256": hashlib.sha256(
                        _canonical_json(item.model_dump(mode="json")).encode("utf-8")
                    ).hexdigest(),
                }
                for item in safe.responses_items
            ],
        }


def estimate_continuation_tokens(message: ModelMessage) -> int:
    """Conservatively account for text and opaque bytes carried by a message."""

    continuation = message.continuation
    if continuation is None:
        return 0
    total = 0
    if continuation.deepseek_reasoning_content is not None:
        total += (len(continuation.deepseek_reasoning_content.encode("utf-8")) + 2) // 3
    for item in continuation.responses_items:
        payload = item.model_dump(mode="json")
        opaque = payload.get("item", {}).get("encrypted_content")
        if isinstance(opaque, str):
            plain_payload = dict(payload)
            plain_item = dict(payload.get("item", {}))
            plain_item.pop("encrypted_content", None)
            plain_payload["item"] = plain_item
            total += (len(_canonical_json(plain_payload).encode("utf-8")) + 2) // 3
        # Ciphertext is not natural language; count every byte as a token.
        if isinstance(opaque, str):
            total += len(opaque.encode("utf-8"))
        else:
            total += (len(_canonical_json(payload).encode("utf-8")) + 2) // 3
    return total


def _redact_preserving_encrypted_content(value: Any, redactor: SecretRedactor) -> Any:
    if isinstance(value, dict):
        return {
            key: (
                item
                if key == "encrypted_content"
                else _redact_preserving_encrypted_content(item, redactor)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_preserving_encrypted_content(item, redactor) for item in value]
    return redactor.redact(value)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


__all__ = [
    "ContinuationCodec",
    "ContinuationIntegrityError",
    "ContinuationUnavailable",
    "estimate_continuation_tokens",
]
