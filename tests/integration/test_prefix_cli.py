import json

import pytest
from typer.testing import CliRunner

from patchloop.cli import app
from patchloop.domain import DEFAULT_PROMPT_CACHE_LAYOUT, PromptCacheLayout
from patchloop.persistence import SQLiteStore
from patchloop.providers import FakeProvider, ModelResponse


@pytest.mark.parametrize("layout", [None, "legacy", "stable", "append_only"])
@pytest.mark.parametrize("entry", ["run", "session"])
def test_cli_task_entries_share_layout_selection(tmp_path, monkeypatch, layout, entry):
    provider = FakeProvider([ModelResponse(content="done")])
    monkeypatch.setattr("patchloop.cli._provider_from_env", lambda: provider)
    runner = CliRunner()
    if entry == "run":
        arguments = ["run", "Inspect the repository", "--repo", str(tmp_path)]
    else:
        prefix = ["session", "--repo", str(tmp_path)]
        created = runner.invoke(app, [*prefix, "create"])
        assert created.exit_code == 0, created.output
        session_id = json.loads(created.stdout)["id"]
        arguments = [*prefix, "start", session_id, "Inspect the repository"]
    if layout is not None:
        arguments.extend(["--prompt-cache-layout", layout])
    result = runner.invoke(app, arguments)
    assert result.exit_code == 0, result.output
    task_id = json.loads(result.stdout)["id"]
    task = SQLiteStore(tmp_path / ".patchloop" / "patchloop.db").get_task(task_id)
    expected = DEFAULT_PROMPT_CACHE_LAYOUT if layout is None else PromptCacheLayout(layout)
    assert task.execution.prompt_cache_layout is expected
    if expected is PromptCacheLayout.APPEND_ONLY:
        assert any("PATCHLOOP_MEMORY_SNAPSHOT_V2" in m.content for m in provider.requests[0][0])


def test_pps_gate_cli_writes_unverified_report_and_cannot_enable_simulated_rollout(tmp_path):
    from tests.unit.test_prefix_acceptance import _evidence

    _, local, _ = _evidence()
    report_path = tmp_path / "local.json"
    report_path.write_text(local.model_dump_json(), encoding="utf-8")
    output = tmp_path / "acceptance.json"
    result = CliRunner().invoke(
        app,
        [
            "validate-cache-gates",
            "--profile",
            "pps",
            "--report",
            str(report_path),
            "--output",
            str(output),
            "--allow-simulated",
            "--enable-append-only",
        ],
    )
    assert result.exit_code == 1
    saved = json.loads(output.read_text(encoding="utf-8"))
    assert saved["schema_version"] == "pps.v1"
    assert saved["rollout"]["enabled"] is False
    assert any(c["status"] == "unverified" for c in saved["checks"])


def test_paused_compression_report_does_not_write_artifacts_after_lease_release(
    tmp_path, monkeypatch
):
    from tests.integration.test_prompt_prefix_recovery import _CompressingProvider, _Observe

    class InvalidSummaryProvider(_CompressingProvider):
        def complete(self, messages, tools):
            response = super().complete(messages, tools)
            if "PATCHLOOP_EPOCH_COMPRESSION_V1" in messages[-1].content:
                return response.model_copy(update={"content": "invalid JSON"})
            return response

    provider = InvalidSummaryProvider()
    monkeypatch.setattr("patchloop.cli._provider_from_env", lambda: provider)
    monkeypatch.setattr("patchloop.cli._all_tools", lambda: [_Observe([], long=True)])
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "run",
            "Read observations.",
            "--repo",
            str(tmp_path),
            "--prompt-cache-layout",
            "append_only",
            "--max-context-tokens",
            "4500",
            "--max-tool-output-chars",
            "3000",
            "--max-steps",
            "20",
        ],
    )
    assert result.exit_code == 11, result.output
    task = json.loads(result.stdout)
    assert task["runtime_condition"] == "paused"
    assert "invalid_summary" in task["report"]["summary"]
    resumed = runner.invoke(app, ["resume", task["id"], "--repo", str(tmp_path)])
    assert resumed.exit_code == 11, resumed.output
    assert json.loads(resumed.stdout)["runtime_condition"] == "paused"
