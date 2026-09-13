from pathlib import Path

import pytest
from pydantic import ValidationError

from patchloop.domain import TaskExecutionConfig
from patchloop.providers import (
    CredentialResolver,
    ProfileResolver,
    ProviderAuth,
    ProviderError,
    ProviderFactory,
    ProviderProtocol,
    provider_endpoint,
)

CONFIG = """
schema_version = 1
default_profile = "local"

[profiles.local]
protocol = "chat_completions"
dialect = "standard"
base_url = "http://127.0.0.1:8000/v1"
auth = "none"
default_model = "local-model"

[profiles.local.models.local-model.capabilities]
tools = true
multiple_tool_calls = false
streaming = true
reasoning_transport = "none"
structured_output = false
context_window_tokens = 8192
max_output_tokens = 1024
usage_supported = true
cache_usage_supported = false

[profiles.local.models.local-model.generation]
max_output_tokens = 512
reasoning_enabled = false

[profiles.local.models.local-model.pricing]
version = "local-zero"
input_per_million = 0
output_per_million = 0

[profiles.responses]
protocol = "responses"
base_url = "https://api.openai.example/v1"
credential_env = "OPENAI_EXAMPLE_KEY"
default_model = "response-model"

[profiles.responses.models.response-model.capabilities]
tools = true
multiple_tool_calls = true
streaming = true
reasoning_transport = "responses_items"
structured_output = true
context_window_tokens = 16000
max_output_tokens = 2048
usage_supported = true
cache_usage_supported = true

[profiles.responses.models.response-model.generation]
max_output_tokens = 1024
reasoning_enabled = true
reasoning_effort = "medium"
"""


def write_config(tmp_path: Path, content: str = CONFIG) -> Path:
    path = tmp_path / "providers.toml"
    path.write_text(content, encoding="utf-8")
    return path


def test_resolver_uses_file_default_and_keeps_profiles_atomic(tmp_path: Path) -> None:
    resolver = ProfileResolver()
    path = write_config(tmp_path)

    local = resolver.resolve(config_path=path)
    responses = resolver.resolve("responses", config_path=path)

    assert local.profile_id == "local"
    assert local.auth is ProviderAuth.NONE
    assert local.model == "local-model"
    assert provider_endpoint(local) == "http://127.0.0.1:8000/v1/chat/completions"
    assert responses.protocol is ProviderProtocol.RESPONSES
    assert responses.credential_env == "OPENAI_EXAMPLE_KEY"
    assert provider_endpoint(responses) == "https://api.openai.example/v1/responses"


def test_explicit_profile_and_model_override_only_the_selected_profile(tmp_path: Path) -> None:
    path = write_config(tmp_path)

    with pytest.raises(ProviderError, match="not configured for profile 'local'"):
        ProfileResolver().resolve("local", "response-model", config_path=path)


def test_builtin_deepseek_is_available_without_a_file(tmp_path: Path) -> None:
    selected = ProfileResolver().resolve(config_path=None)

    assert selected.profile_id == "deepseek"
    assert selected.model == "deepseek-flash"
    assert selected.credential_env == "DEEPSEEK_API_KEY"


def test_builtin_deepseek_keeps_legacy_flash_model_available() -> None:
    selected = ProfileResolver().resolve("deepseek", "deepseek-v4-flash")

    assert selected.model == "deepseek-v4-flash"


def test_credential_resolution_is_separate_and_supports_legacy_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("LLM_API_KEY", "rotated-secret")
    binding = ProfileResolver().resolve("deepseek")
    original_fingerprint = binding.fingerprint

    credential = CredentialResolver().resolve(binding)
    monkeypatch.setenv("LLM_API_KEY", "new-secret")

    assert credential is not None
    assert credential.get_secret_value() == "rotated-secret"
    assert binding.fingerprint == original_fingerprint
    assert "rotated-secret" not in binding.model_dump_json()


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/v1",
        "https://user:password@example.com/v1",
        "https://example.com/v1?api_key=secret",
        "https://example.com/v1#fragment",
    ],
)
def test_resolver_rejects_unsafe_base_urls(tmp_path: Path, url: str) -> None:
    path = write_config(tmp_path, CONFIG.replace("http://127.0.0.1:8000/v1", url))

    with pytest.raises(ProviderError):
        ProfileResolver().resolve(config_path=path)


def test_unknown_fields_and_unsupported_generation_options_are_rejected(tmp_path: Path) -> None:
    path = write_config(tmp_path, CONFIG.replace("reasoning_enabled = false", "top_p = 0.5"))

    with pytest.raises(ProviderError, match="invalid provider configuration"):
        ProfileResolver().resolve(config_path=path)


def test_legacy_task_execution_config_has_no_binding() -> None:
    execution = TaskExecutionConfig.model_validate({"allowed_permissions": ["read"]})

    assert execution.provider is None


def test_factory_fails_closed_until_route_adapter_is_registered() -> None:
    binding = ProfileResolver().resolve("deepseek")

    with pytest.raises(ProviderError, match="not implemented yet"):
        ProviderFactory().create(binding)


def test_binding_still_rejects_auth_none_with_a_credential_reference(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        CONFIG.replace('auth = "none"', 'auth = "none"\ncredential_env = "SHOULD_NOT_EXIST"'),
    )

    with pytest.raises((ProviderError, ValidationError), match="credential_env"):
        ProfileResolver().resolve(config_path=path)
