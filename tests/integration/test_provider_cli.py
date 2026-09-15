import json
from pathlib import Path

from typer.testing import CliRunner

from patchloop.cli import _ProviderEventWriter, app
from patchloop.providers import ProviderEvent, ProviderEventType

runner = CliRunner()


def provider_config(path: Path) -> Path:
    config = path / "providers.toml"
    config.write_text(
        """
schema_version = 1
default_profile = "local"

[profiles.local]
protocol = "responses"
base_url = "http://127.0.0.1:8080"
auth = "none"
default_model = "test-model"

[profiles.local.models.test-model.capabilities]
tools = true
multiple_tool_calls = true
streaming = false
reasoning_transport = "none"
structured_output = false
context_window_tokens = 4096
max_output_tokens = 256
usage_supported = true
cache_usage_supported = true

[profiles.local.models.test-model.generation]
max_output_tokens = 128

[profiles.local.models.test-model.pricing]
version = "local-zero-v1"
input_per_million = 0
output_per_million = 0
""".strip(),
        encoding="utf-8",
    )
    return config


def test_provider_list_show_and_check_are_local_by_default(tmp_path: Path) -> None:
    config = provider_config(tmp_path)

    listed = runner.invoke(app, ["provider", "list", "--provider-config", str(config)])
    shown = runner.invoke(
        app,
        ["provider", "show", "local", "--provider-config", str(config)],
    )
    checked = runner.invoke(
        app,
        ["provider", "check", "local", "--provider-config", str(config)],
    )

    assert listed.exit_code == 0, listed.output
    assert {item["profile_id"] for item in json.loads(listed.output)["items"]} == {
        "deepseek",
        "local",
    }
    assert shown.exit_code == 0, shown.output
    assert json.loads(shown.output)["provider"]["pricing_status"] == "configured"
    assert checked.exit_code == 0, checked.output
    assert json.loads(checked.output)["connected"] is False


def test_run_rejects_json_and_events_jsonl_together(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["run", "inspect", "--repo", str(tmp_path), "--json", "--events-jsonl"],
    )

    assert result.exit_code == 2
    assert "mutually exclusive" in result.output


def test_events_jsonl_redacts_a_secret_split_across_deltas(capsys: object) -> None:
    writer = _ProviderEventWriter()
    for sequence, delta in enumerate(("sk-proj-", "abcdefghijklmnopqrstuvwxyz123456")):
        writer(
            ProviderEvent(
                type=ProviderEventType.TEXT_DELTA,
                request_id="request-1",
                attempt_id="attempt-1",
                sequence=sequence,
                delta=delta,
            )
        )
    writer.flush()

    output = capsys.readouterr().out  # type: ignore[attr-defined]
    assert "sk-proj-abcdefghijklmnopqrstuvwxyz123456" not in output
    assert "[REDACTED]" in output
