from pathlib import Path

import pytest

from patchloop.providers import (
    ChatCompletionsAdapter,
    ModelMessage,
    ProfileResolver,
    ProviderBinding,
    ProviderError,
    ProviderRequest,
    ProviderRequestPurpose,
)

PROFILE = Path(__file__).parents[2] / "providers.toml"


def test_local_profile_resolves_env_and_encodes_high(tmp_path, monkeypatch):
    for name in ("OPENAI_BASE_URL", "OPENAI_MODEL", "OPENAI_REASONING_EFFORT"):
        monkeypatch.delenv(name, raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "OPENAI_BASE_URL=http://localhost:12345/v1\n"
        "OPENAI_MODEL=gpt-5.6-luna\nOPENAI_REASONING_EFFORT=high\n"
        "OPENAI_API_KEY=private-test-key\n",
        encoding="utf-8",
    )
    resolver = ProfileResolver()
    binding = resolver.resolve(config_path=PROFILE, env_file=env_file)
    assert binding.base_url == "http://localhost:12345/v1"
    assert binding.model == "gpt-5.6-luna"
    assert binding.pricing is not None
    assert binding.pricing.input_per_million == binding.pricing.output_per_million == 0
    assert "private-test-key" not in binding.model_dump_json()
    request = ProviderRequest(
        request_id="request",
        task_id="task",
        step_index=0,
        purpose=ProviderRequestPurpose.AGENT_STEP,
        messages=(ModelMessage(role="user", content="hello"),),
    )
    body = ChatCompletionsAdapter().encode(request, binding).body
    assert body["reasoning_effort"] == "high"
    assert body["max_completion_tokens"] == 8192
    assert "max_tokens" not in body
    assert "thinking" not in body
    saved = binding.model_dump_json()
    monkeypatch.setenv("OPENAI_REASONING_EFFORT", "low")
    changed = resolver.resolve(config_path=PROFILE, env_file=env_file)
    assert changed.generation.reasoning_effort == "low"
    assert ProviderBinding.model_validate_json(saved).generation.reasoning_effort == "high"


def test_env_cannot_reuse_capabilities_for_an_unconfigured_model(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_MODEL", "unconfigured-model")
    with pytest.raises(ProviderError, match="is not configured"):
        ProfileResolver().resolve(config_path=PROFILE)
    binding = ProfileResolver().resolve(model="gpt-5.6-luna", config_path=PROFILE)
    assert binding.model == "gpt-5.6-luna"


def test_env_url_still_uses_loopback_restriction(monkeypatch):
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    monkeypatch.setenv("OPENAI_BASE_URL", "http://remote.example/v1")
    with pytest.raises(ProviderError, match="loopback"):
        ProfileResolver().resolve(config_path=PROFILE)


def test_cli_uses_config_default_without_explicit_profile(tmp_path, monkeypatch):
    from patchloop.cli import _selected_provider

    for name in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL", "OPENAI_REASONING_EFFORT"):
        monkeypatch.delenv(name, raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("OPENAI_API_KEY=local-test-key\n", encoding="utf-8")
    _, binding = _selected_provider(None, config_path=PROFILE, env_file=env_file)
    assert binding is not None
    assert binding.profile_id == "local-openai"
