"""Provider factory boundary used by the CLI and runtime."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from patchloop.providers.base import ProviderGateway
from patchloop.providers.contracts import (
    ChatDialect,
    ProviderBinding,
    ProviderError,
    ProviderErrorKind,
    ProviderProtocol,
)

type GatewayBuilder = Callable[[ProviderBinding, Any | None], ProviderGateway]
type ProviderRoute = tuple[ProviderProtocol, ChatDialect]


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
        self._builders = dict(builders or {})

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

    def create(self, binding: ProviderBinding, transport: Any | None = None) -> ProviderGateway:
        route = (binding.protocol, binding.dialect)
        builder = self._builders.get(route)
        if builder is None:
            raise ProviderError(
                ProviderErrorKind.CAPABILITY,
                f"provider adapter {binding.protocol.value}/{binding.dialect.value} "
                "is not implemented yet",
            )
        return builder(binding, transport)
