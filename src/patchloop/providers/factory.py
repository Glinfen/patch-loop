"""Provider factory boundary used by the CLI and runtime."""

from __future__ import annotations

from collections.abc import Callable

from patchloop.providers.base import ProviderGateway as ProviderGatewayPort
from patchloop.providers.chat import ChatCompletionsAdapter
from patchloop.providers.config import CredentialResolver
from patchloop.providers.contracts import (
    ChatDialect,
    ProviderBinding,
    ProviderError,
    ProviderErrorKind,
    ProviderProtocol,
)
from patchloop.providers.gateway import ProviderGateway
from patchloop.providers.responses import ResponsesAdapter
from patchloop.providers.transport import AsyncTransport, HttpxTransport

type GatewayBuilder = Callable[[ProviderBinding, AsyncTransport | None], ProviderGatewayPort]
type ProviderRoute = tuple[ProviderProtocol, ChatDialect]


def _build_chat_gateway(
    binding: ProviderBinding,
    transport: AsyncTransport | None,
) -> ProviderGatewayPort:
    if transport is None:
        credential = CredentialResolver().resolve(binding)
        transport = HttpxTransport(
            binding.base_url,
            credential=credential,
            config=binding.transport,
        )
    return ProviderGateway(binding, ChatCompletionsAdapter(binding.dialect), transport)


def _build_responses_gateway(
    binding: ProviderBinding,
    transport: AsyncTransport | None,
) -> ProviderGatewayPort:
    if transport is None:
        credential = CredentialResolver().resolve(binding)
        transport = HttpxTransport(
            binding.base_url,
            credential=credential,
            config=binding.transport,
        )
    return ProviderGateway(binding, ResponsesAdapter(), transport)


class ProviderFactory:
    """Create gateways only for explicitly registered protocol/dialect routes."""

    known_routes: frozenset[ProviderRoute] = frozenset(
        {
            (ProviderProtocol.CHAT_COMPLETIONS, ChatDialect.STANDARD),
            (ProviderProtocol.CHAT_COMPLETIONS, ChatDialect.DEEPSEEK),
            (ProviderProtocol.RESPONSES, ChatDialect.STANDARD),
        }
    )

    def __init__(self, builders: dict[ProviderRoute, GatewayBuilder] | None = None) -> None:
        self._builders: dict[ProviderRoute, GatewayBuilder] = {
            (ProviderProtocol.CHAT_COMPLETIONS, ChatDialect.STANDARD): _build_chat_gateway,
            (ProviderProtocol.CHAT_COMPLETIONS, ChatDialect.DEEPSEEK): _build_chat_gateway,
            (ProviderProtocol.RESPONSES, ChatDialect.STANDARD): _build_responses_gateway,
        }
        self._builders.update(builders or {})

    def register(
        self, protocol: ProviderProtocol, dialect: ChatDialect, builder: GatewayBuilder
    ) -> None:
        route = (protocol, dialect)
        if route not in self.known_routes:
            raise ProviderError(
                ProviderErrorKind.CONFIGURATION,
                f"unsupported provider route {protocol.value}/{dialect.value}",
            )
        self._builders[route] = builder

    def create(
        self,
        binding: ProviderBinding,
        transport: AsyncTransport | None = None,
    ) -> ProviderGatewayPort:
        route = (binding.protocol, binding.dialect)
        builder = self._builders.get(route)
        if builder is None:
            raise ProviderError(
                ProviderErrorKind.CAPABILITY,
                f"provider adapter {binding.protocol.value}/{binding.dialect.value} "
                "is not implemented yet",
            )
        return builder(binding, transport)
