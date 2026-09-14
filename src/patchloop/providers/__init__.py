"""Public provider API with lazy imports to keep domain contracts acyclic."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from patchloop.providers.base import (
        ControlAction,
        EncodedRequest,
        ModelMessage,
        ModelProvider,
        ModelResponse,
        ModelUsage,
        ProviderAdapter,
        ProviderControl,
        ProviderEvent,
        ProviderEventObserver,
        ProviderEventType,
        ProviderRequest,
        ProviderRequestPurpose,
        StreamReducer,
        ToolSpec,
    )
    from patchloop.providers.chat import ChatCompletionsAdapter, ChatStreamReducer
    from patchloop.providers.config import CredentialResolver, ProfileResolver, provider_endpoint
    from patchloop.providers.continuation import (
        ContinuationCodec,
        ContinuationIntegrityError,
        ContinuationUnavailable,
    )
    from patchloop.providers.contracts import (
        ChatDialect,
        ProviderAuth,
        ProviderBinding,
        ProviderCapabilities,
        ProviderContinuation,
        ProviderError,
        ProviderErrorKind,
        ProviderGeneration,
        ProviderPricing,
        ProviderProtocol,
        ProviderTransportConfig,
        ReasoningTransport,
        ValidatedResponseItem,
        resolve_credential,
    )
    from patchloop.providers.deepseek import DeepSeekConfig, DeepSeekProvider
    from patchloop.providers.factory import ProviderFactory
    from patchloop.providers.fake import FakeProvider
    from patchloop.providers.gateway import LegacyProviderAdapter, ProviderGateway
    from patchloop.providers.responses import ResponsesAdapter, ResponsesStreamReducer

_MODULE_BY_NAME = {
    "ChatCompletionsAdapter": "chat",
    "ChatDialect": "contracts",
    "ChatStreamReducer": "chat",
    "ContinuationCodec": "continuation",
    "ContinuationIntegrityError": "continuation",
    "ContinuationUnavailable": "continuation",
    "ControlAction": "base",
    "CredentialResolver": "config",
    "DeepSeekConfig": "deepseek",
    "DeepSeekProvider": "deepseek",
    "EncodedRequest": "base",
    "FakeProvider": "fake",
    "ModelMessage": "base",
    "ModelProvider": "base",
    "ModelResponse": "base",
    "ModelUsage": "base",
    "ProfileResolver": "config",
    "ProviderAdapter": "base",
    "ProviderAuth": "contracts",
    "ProviderBinding": "contracts",
    "ProviderCapabilities": "contracts",
    "ProviderContinuation": "contracts",
    "ProviderControl": "base",
    "ProviderError": "contracts",
    "ProviderErrorKind": "contracts",
    "ProviderEvent": "base",
    "ProviderEventObserver": "base",
    "ProviderEventType": "base",
    "ProviderFactory": "factory",
    "LegacyProviderAdapter": "gateway",
    "ProviderGateway": "gateway",
    "ProviderGeneration": "contracts",
    "ProviderPricing": "contracts",
    "ProviderProtocol": "contracts",
    "ProviderRequest": "base",
    "ProviderRequestPurpose": "base",
    "ProviderTransportConfig": "contracts",
    "ResponsesAdapter": "responses",
    "ResponsesStreamReducer": "responses",
    "ReasoningTransport": "contracts",
    "StreamReducer": "base",
    "ToolSpec": "base",
    "ValidatedResponseItem": "contracts",
    "provider_endpoint": "config",
    "resolve_credential": "contracts",
}

__all__ = [
    "ChatCompletionsAdapter",
    "ChatDialect",
    "ChatStreamReducer",
    "ContinuationCodec",
    "ContinuationIntegrityError",
    "ContinuationUnavailable",
    "ControlAction",
    "CredentialResolver",
    "DeepSeekConfig",
    "DeepSeekProvider",
    "EncodedRequest",
    "FakeProvider",
    "LegacyProviderAdapter",
    "ModelMessage",
    "ModelProvider",
    "ModelResponse",
    "ModelUsage",
    "ProfileResolver",
    "ProviderAdapter",
    "ProviderAuth",
    "ProviderBinding",
    "ProviderCapabilities",
    "ProviderContinuation",
    "ProviderControl",
    "ProviderError",
    "ProviderErrorKind",
    "ProviderEvent",
    "ProviderEventObserver",
    "ProviderEventType",
    "ProviderFactory",
    "ProviderGateway",
    "ProviderGeneration",
    "ProviderPricing",
    "ProviderProtocol",
    "ProviderRequest",
    "ProviderRequestPurpose",
    "ProviderTransportConfig",
    "ReasoningTransport",
    "ResponsesAdapter",
    "ResponsesStreamReducer",
    "StreamReducer",
    "ToolSpec",
    "ValidatedResponseItem",
    "provider_endpoint",
    "resolve_credential",
]


def __getattr__(name: str) -> Any:
    module_name = _MODULE_BY_NAME.get(name)
    if module_name is None:
        raise AttributeError(name)
    value = getattr(import_module(f"patchloop.providers.{module_name}"), name)
    globals()[name] = value
    return value
