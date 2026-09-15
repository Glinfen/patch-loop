"""Resolve provider profiles into immutable, non-secret task bindings."""

from __future__ import annotations

import ipaddress
import os
import tomllib
from pathlib import Path
from typing import Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from patchloop.providers.contracts import (
    ChatDialect,
    ProviderAuth,
    ProviderBinding,
    ProviderCapabilities,
    ProviderError,
    ProviderErrorKind,
    ProviderGeneration,
    ProviderPricing,
    ProviderProtocol,
    ProviderTransportConfig,
    ReasoningTransport,
)

DEFAULT_PROVIDER_CONFIG = Path.home() / ".patchloop" / "providers.toml"


class _ModelDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capabilities: ProviderCapabilities
    generation: ProviderGeneration
    pricing: ProviderPricing | None = None


class _ProfileDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    protocol: ProviderProtocol
    dialect: ChatDialect = ChatDialect.STANDARD
    base_url: str
    auth: ProviderAuth = ProviderAuth.BEARER
    credential_env: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z_][A-Za-z0-9_]*$",
    )
    default_model: str
    transport: ProviderTransportConfig = Field(default_factory=ProviderTransportConfig)
    models: dict[str, _ModelDefinition]

    @model_validator(mode="after")
    def validate_default_model(self) -> Self:
        if self.default_model not in self.models:
            raise ValueError(f"default_model {self.default_model!r} is not configured")
        return self


class _ProviderFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1, le=1)
    default_profile: str | None = None
    profiles: dict[str, _ProfileDefinition] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_default_profile(self) -> Self:
        if self.default_profile is not None and self.default_profile not in self.profiles:
            raise ValueError(f"default_profile {self.default_profile!r} is not configured")
        return self


def _deepseek_flash_definition() -> _ModelDefinition:
    return _ModelDefinition(
        capabilities=ProviderCapabilities(
            tools=True,
            multiple_tool_calls=True,
            streaming=True,
            reasoning_transport=ReasoningTransport.DEEPSEEK_TEXT,
            structured_output=False,
            context_window_tokens=32_000,
            max_output_tokens=16_384,
            usage_supported=True,
            cache_usage_supported=True,
        ),
        generation=ProviderGeneration(
            max_output_tokens=16_384,
            temperature=1.0,
            reasoning_enabled=True,
            reasoning_effort="high",
        ),
        pricing=ProviderPricing(
            version="legacy-deepseek-default",
            input_per_million=0.14,
            output_per_million=0.28,
            cached_input_per_million=0.0028,
        ),
    )


def _builtin_deepseek() -> _ProfileDefinition:
    current = _deepseek_flash_definition()
    return _ProfileDefinition(
        protocol=ProviderProtocol.CHAT_COMPLETIONS,
        dialect=ChatDialect.DEEPSEEK,
        base_url="https://api.deepseek.com",
        credential_env="DEEPSEEK_API_KEY",
        default_model="deepseek-flash",
        models={
            "deepseek-flash": current,
            "deepseek-v4-flash": current.model_copy(deep=True),
        },
    )


class ProfileResolver:
    """Load a configured profile without resolving or touching its credential."""

    def resolve(
        self,
        profile_id: str | None = None,
        model: str | None = None,
        config_path: Path | None = None,
        env_file: Path | None = None,
    ) -> ProviderBinding:
        del env_file  # Credentials are deliberately resolved in a separate phase.
        selected_path = config_path if config_path is not None else DEFAULT_PROVIDER_CONFIG
        configured = self._load(selected_path, required=config_path is not None)
        profiles = {"deepseek": _builtin_deepseek(), **configured.profiles}
        selected_profile = profile_id or configured.default_profile or "deepseek"
        if selected_profile not in profiles:
            available = ", ".join(sorted(profiles))
            raise ProviderError(
                ProviderErrorKind.CONFIGURATION,
                f"unknown provider profile {selected_profile!r}; available profiles: {available}",
            )
        profile = profiles[selected_profile]
        selected_model = model or profile.default_model
        if selected_model not in profile.models:
            available = ", ".join(sorted(profile.models))
            raise ProviderError(
                ProviderErrorKind.CONFIGURATION,
                f"model {selected_model!r} is not configured for profile "
                f"{selected_profile!r}; available models: {available}",
            )
        _validate_url(profile.base_url, field="base_url", allow_http_loopback=True)
        if profile.transport.proxy_url is not None:
            _validate_url(
                profile.transport.proxy_url,
                field="transport.proxy_url",
                allow_http_loopback=True,
            )
        selected = profile.models[selected_model]
        return ProviderBinding(
            profile_id=selected_profile,
            protocol=profile.protocol,
            dialect=profile.dialect,
            model=selected_model,
            base_url=profile.base_url.rstrip("/"),
            auth=profile.auth,
            credential_env=profile.credential_env,
            capabilities=selected.capabilities,
            generation=selected.generation,
            transport=profile.transport,
            pricing=selected.pricing,
        )

    def list_bindings(self, config_path: Path | None = None) -> list[ProviderBinding]:
        """Return each locally configured profile's default binding."""

        selected_path = config_path if config_path is not None else DEFAULT_PROVIDER_CONFIG
        configured = self._load(selected_path, required=config_path is not None)
        profiles = {"deepseek": _builtin_deepseek(), **configured.profiles}
        return [
            self.resolve(profile_id, config_path=config_path) for profile_id in sorted(profiles)
        ]

    def resolve_legacy_environment(self) -> ProviderBinding:
        """Project the historical DeepSeek environment aliases onto the built-in profile."""

        binding = self.resolve("deepseek")
        payload = binding.model_dump(mode="json", exclude={"fingerprint"})
        base_url = os.environ.get("DEEPSEEK_BASE_URL") or os.environ.get("LLM_BASE_URL")
        model = os.environ.get("DEEPSEEK_MODEL") or os.environ.get("LLM_MODEL_ID")
        if base_url:
            _validate_url(base_url, field="base_url", allow_http_loopback=True)
            payload["base_url"] = base_url.rstrip("/")
        if model:
            payload["model"] = model
        return ProviderBinding.model_validate(payload)

    @staticmethod
    def _load(path: Path, *, required: bool) -> _ProviderFile:
        if not path.is_file():
            if required:
                raise ProviderError(
                    ProviderErrorKind.CONFIGURATION,
                    f"provider configuration file does not exist: {path}",
                )
            return _ProviderFile()
        try:
            with path.open("rb") as handle:
                raw = tomllib.load(handle)
            return _ProviderFile.model_validate(raw)
        except (OSError, tomllib.TOMLDecodeError, ValueError) as exc:
            raise ProviderError(
                ProviderErrorKind.CONFIGURATION,
                f"invalid provider configuration: {exc}",
            ) from exc


class CredentialResolver:
    def resolve(
        self,
        binding: ProviderBinding,
        *,
        env_file: Path | None = None,
    ) -> SecretStr | None:
        if binding.auth is ProviderAuth.NONE:
            return None
        assert binding.credential_env is not None
        names = [binding.credential_env]
        if binding.profile_id == "deepseek" and binding.credential_env == "DEEPSEEK_API_KEY":
            names.append("LLM_API_KEY")
        file_values = _load_env_file(env_file) if env_file is not None else {}
        for source in (os.environ, file_values):
            for name in names:
                value = source.get(name)
                if value:
                    return SecretStr(value)
        raise ProviderError(
            ProviderErrorKind.CONFIGURATION,
            f"{' or '.join(names)} is not set",
        )


def provider_endpoint(binding: ProviderBinding) -> str:
    suffix = (
        "/chat/completions"
        if binding.protocol is ProviderProtocol.CHAT_COMPLETIONS
        else "/responses"
    )
    return f"{binding.base_url.rstrip('/')}{suffix}"


def _load_env_file(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        name, separator, value = line.partition("=")
        name = name.strip()
        value = value.strip()
        if not separator or not name.isidentifier():
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[name] = value
    return values


def _validate_url(url: str, *, field: str, allow_http_loopback: bool) -> None:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ProviderError(
            ProviderErrorKind.CONFIGURATION,
            f"{field} must be an absolute HTTP(S) URL",
        )
    if parsed.username is not None or parsed.password is not None:
        raise ProviderError(
            ProviderErrorKind.CONFIGURATION,
            f"{field} cannot contain user information",
        )
    if parsed.query or parsed.fragment:
        raise ProviderError(
            ProviderErrorKind.CONFIGURATION,
            f"{field} cannot contain a query or fragment",
        )
    if parsed.scheme == "http" and (not allow_http_loopback or not _is_loopback(parsed.hostname)):
        raise ProviderError(
            ProviderErrorKind.CONFIGURATION,
            f"{field} may use HTTP only for a loopback address",
        )


def _is_loopback(hostname: str) -> bool:
    if hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False
