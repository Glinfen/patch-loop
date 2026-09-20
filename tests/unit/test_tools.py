import json
from pathlib import Path

import pytest

from patchloop.domain import ErrorKind, ToolCall
from patchloop.intelligence import RepositoryIndexer
from patchloop.runtime import SYSTEM_PROMPT
from patchloop.tools import (
    ApplyPatchTool,
    CreateFileTool,
    GetDiffTool,
    ListFilesTool,
    PermissionLevel,
    ReadFileTool,
    ReplaceTextTool,
    RunCommandTool,
    RunTestsTool,
    SearchCodeTool,
    SearchTextTool,
    ToolContext,
    ToolGateway,
    ToolPolicy,
    UpdatePlanTool,
    WriteFileTool,
)


def make_repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "src").mkdir()
    (repository / "src" / "calculator.py").write_text(
        "def add(left, right):\n    return left + right\n",
        encoding="utf-8",
    )
    (repository / "README.md").write_text("# Calculator\n", encoding="utf-8")
    return repository


def make_gateway(repository: Path) -> ToolGateway:
    return ToolGateway(
        ToolContext(repository),
        [ListFilesTool(), ReadFileTool(), SearchTextTool()],
    )


def test_read_only_tools_return_repository_evidence(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)
    (repository / ".patchloop").mkdir()
    (repository / ".patchloop" / "repository-index.json").write_text(
        "internal state",
        encoding="utf-8",
    )
    gateway = make_gateway(repository)

    listed = gateway.execute("task-1", ToolCall(name="list_files", arguments={}))
    read = gateway.execute(
        "task-1",
        ToolCall(name="read_file", arguments={"path": "src/calculator.py"}),
    )
    searched = gateway.execute(
        "task-1",
        ToolCall(name="search_text", arguments={"query": "return left"}),
    )

    assert listed.success and "src/calculator.py" in listed.output
    assert ".patchloop" not in listed.output
    assert read.success and "2:     return left + right" in read.output
    assert searched.success and "src/calculator.py:2" in searched.output


def test_list_files_ignores_only_repository_relative_cache_directories(
    tmp_path: Path,
) -> None:
    repository = tmp_path / ".patchloop" / "benchmark-run"
    repository.mkdir(parents=True)
    (repository / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repository / ".patchloop").mkdir()
    (repository / ".patchloop" / "trace.jsonl").write_text("{}\n", encoding="utf-8")

    result = make_gateway(repository).execute("task-1", ToolCall(name="list_files", arguments={}))

    assert result.success
    assert result.output == "app.py"


def test_update_plan_accepts_common_in_progress_alias(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)
    gateway = ToolGateway(ToolContext(repository), [UpdatePlanTool()])

    result = gateway.execute(
        "task-1",
        ToolCall(
            name="update_plan",
            arguments={"items": [{"description": "Implement fix", "status": "in_progress"}]},
        ),
    )

    assert result.success
    assert gateway.context.plan is not None
    assert gateway.context.plan.items[0].status.value == "running"


def test_search_code_returns_ranked_provenance_and_uses_recent_access(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)
    index_path = repository / ".patchloop" / "repository-index.json"
    RepositoryIndexer(repository).build().save(index_path)
    original_index = index_path.read_text(encoding="utf-8")
    context = ToolContext(repository)
    gateway = ToolGateway(context, [ReadFileTool(), SearchCodeTool()])
    gateway.execute(
        "task-1",
        ToolCall(name="read_file", arguments={"path": "src/calculator.py"}),
    )

    result = gateway.execute(
        "task-1",
        ToolCall(
            name="search_code",
            arguments={"query": "calculation function", "max_results": 2},
        ),
    )

    payload = json.loads(result.output)
    assert result.success
    assert payload[0]["path"] == "src/calculator.py"
    assert payload[0]["source"].startswith("ast+text+")
    assert payload[0]["features"]["recent_access"] == 1.0
    assert payload[0]["reasons"]
    assert index_path.read_text(encoding="utf-8") == original_index


def test_gateway_rejects_repository_escape(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)
    (tmp_path / "secret.txt").write_text("secret", encoding="utf-8")
    gateway = make_gateway(repository)

    result = gateway.execute(
        "task-1",
        ToolCall(name="read_file", arguments={"path": "../secret.txt"}),
    )

    assert not result.success
    assert result.error_kind is ErrorKind.PATH_DENIED


def test_gateway_classifies_invalid_arguments(tmp_path: Path) -> None:
    gateway = make_gateway(make_repository(tmp_path))

    result = gateway.execute(
        "task-1",
        ToolCall(name="read_file", arguments={}),
    )

    assert not result.success
    assert result.error_kind is ErrorKind.INVALID_ARGUMENTS


def test_gateway_rejects_provider_argument_parse_error(tmp_path: Path) -> None:
    gateway = make_gateway(make_repository(tmp_path))

    result = gateway.execute(
        "task-1",
        ToolCall(
            name="list_files",
            arguments_error="invalid tool arguments JSON",
        ),
    )

    assert not result.success
    assert result.error_kind is ErrorKind.INVALID_ARGUMENTS
    assert result.output == "invalid tool arguments JSON"


def test_default_policy_denies_write_tools(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)
    gateway = ToolGateway(ToolContext(repository), [ReplaceTextTool()])

    result = gateway.execute(
        "task-1",
        ToolCall(
            name="replace_text",
            arguments={
                "path": "README.md",
                "old_text": "Calculator",
                "new_text": "Safe calculator",
            },
        ),
    )

    assert not result.success
    assert result.error_kind is ErrorKind.PERMISSION_DENIED
    assert (repository / "README.md").read_text(encoding="utf-8") == "# Calculator\n"


def test_write_tools_are_atomic_and_produce_diff(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)
    context = ToolContext(repository)
    policy = ToolPolicy(
        frozenset({PermissionLevel.READ, PermissionLevel.WRITE}),
        require_plan_for_mutations=False,
    )
    gateway = ToolGateway(
        context,
        [CreateFileTool(), ReplaceTextTool(), GetDiffTool()],
        policy=policy,
    )

    created = gateway.execute(
        "task-1",
        ToolCall(name="create_file", arguments={"path": "notes.txt", "content": "done\n"}),
    )
    replaced = gateway.execute(
        "task-1",
        ToolCall(
            name="replace_text",
            arguments={
                "path": "src/calculator.py",
                "old_text": "left + right",
                "new_text": "float(left + right)",
            },
        ),
    )
    diff = gateway.execute("task-1", ToolCall(name="get_diff"))

    assert created.success and replaced.success and diff.success
    assert "+++ b/notes.txt" in diff.output
    assert "+    return float(left + right)" in diff.output
    assert context.changes.changed_paths() == ["notes.txt", "src/calculator.py"]


def test_write_file_replaces_existing_file_but_never_creates_one(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)
    gateway = ToolGateway(
        ToolContext(repository),
        [WriteFileTool()],
        policy=ToolPolicy(
            frozenset({PermissionLevel.WRITE}),
            require_plan_for_mutations=False,
        ),
    )

    written = gateway.execute(
        "task-1",
        ToolCall(
            name="write_file",
            arguments={"path": "README.md", "content": "# Rewritten\n"},
        ),
    )
    missing = gateway.execute(
        "task-1",
        ToolCall(
            name="write_file",
            arguments={"path": "missing.py", "content": "VALUE = 1\n"},
        ),
    )

    assert written.success
    assert (repository / "README.md").read_text(encoding="utf-8") == "# Rewritten\n"
    assert not missing.success
    assert not (repository / "missing.py").exists()


def test_apply_patch_is_all_or_nothing(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)
    gateway = ToolGateway(
        ToolContext(repository),
        [ApplyPatchTool()],
        policy=ToolPolicy(
            frozenset({PermissionLevel.WRITE}),
            require_plan_for_mutations=False,
        ),
    )
    original = (repository / "src" / "calculator.py").read_text(encoding="utf-8")

    conflicted = gateway.execute(
        "task-1",
        ToolCall(
            name="apply_patch",
            arguments={
                "path": "src/calculator.py",
                "edits": [
                    {"old_text": "def add", "new_text": "def total"},
                    {"old_text": "missing text", "new_text": "never written"},
                ],
            },
        ),
    )
    assert not conflicted.success
    assert (repository / "src" / "calculator.py").read_text(encoding="utf-8") == original

    applied = gateway.execute(
        "task-1",
        ToolCall(
            name="apply_patch",
            arguments={
                "path": "src/calculator.py",
                "edits": [
                    {"old_text": "def add", "new_text": "def total"},
                    {"old_text": "left + right", "new_text": "float(left + right)"},
                ],
            },
        ),
    )

    assert applied.success
    updated = (repository / "src" / "calculator.py").read_text(encoding="utf-8")
    assert "def total" in updated
    assert "float(left + right)" in updated


def test_run_tests_rejects_arbitrary_python(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)
    gateway = ToolGateway(
        ToolContext(repository),
        [RunTestsTool()],
        policy=ToolPolicy(
            frozenset({PermissionLevel.EXECUTE}),
            require_plan_for_mutations=False,
        ),
    )

    result = gateway.execute(
        "task-1",
        ToolCall(name="run_tests", arguments={"command": ["python", "-c", "print(1)"]}),
    )

    assert not result.success
    assert result.error_kind is ErrorKind.PERMISSION_DENIED


def test_agent_contract_explains_test_and_efficiency_boundaries() -> None:
    assert "python -c" in RunTestsTool.description
    assert "do not repeat" in RunTestsTool.description
    assert "two to four short items" in UpdatePlanTool.description
    assert "never pass python -c" in SYSTEM_PROMPT
    assert "do not paste full source files" in SYSTEM_PROMPT
    assert "Avoid duplicate discovery" in SYSTEM_PROMPT
    assert "smallest focused tests" in SYSTEM_PROMPT


def test_run_tests_rejects_external_config_path(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)
    gateway = ToolGateway(
        ToolContext(repository),
        [RunTestsTool()],
        policy=ToolPolicy(
            frozenset({PermissionLevel.EXECUTE}),
            require_plan_for_mutations=False,
        ),
    )

    result = gateway.execute(
        "task-1",
        ToolCall(
            name="run_tests",
            arguments={"command": ["pytest", "--rootdir=C:\\external"]},
        ),
    )

    assert not result.success
    assert result.error_kind is ErrorKind.EXECUTION_ERROR


def test_run_tests_classifies_syntax_failure() -> None:
    result = RunTestsTool().classify_output(
        '{"exit_code": 1, "output": "SyntaxError: invalid syntax"}'
    )

    assert result is ErrorKind.SYNTAX_ERROR


def test_file_enumeration_does_not_follow_external_symlink(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)
    secret = tmp_path / "secret.txt"
    secret.write_text("outside-secret", encoding="utf-8")
    link = repository / "linked-secret.txt"
    try:
        link.symlink_to(secret)
    except OSError:
        pytest.skip("symbolic links are unavailable on this Windows host")
    gateway = make_gateway(repository)

    listed = gateway.execute("task-1", ToolCall(name="list_files"))
    searched = gateway.execute(
        "task-1",
        ToolCall(name="search_text", arguments={"query": "outside-secret"}),
    )
    read = gateway.execute(
        "task-1",
        ToolCall(name="read_file", arguments={"path": "linked-secret.txt"}),
    )

    assert "linked-secret.txt" not in listed.output
    assert "outside-secret" not in searched.output
    assert read.error_kind is ErrorKind.PATH_DENIED


def test_mutating_tool_requires_explicit_plan(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)
    gateway = ToolGateway(
        ToolContext(repository),
        [UpdatePlanTool(), ReplaceTextTool()],
        policy=ToolPolicy(frozenset({PermissionLevel.READ, PermissionLevel.WRITE})),
    )
    replacement = ToolCall(
        name="replace_text",
        arguments={
            "path": "README.md",
            "old_text": "Calculator",
            "new_text": "Planned calculator",
        },
    )

    denied = gateway.execute("task-1", replacement)
    planned = gateway.execute(
        "task-1",
        ToolCall(
            name="update_plan",
            arguments={
                "items": [
                    {"description": "Update README", "status": "running"},
                    {"description": "Verify change", "status": "pending"},
                ]
            },
        ),
    )
    updated = gateway.execute("task-1", replacement)

    assert denied.error_kind is ErrorKind.PERMISSION_DENIED
    assert planned.success and updated.success
    assert gateway.context.plan is not None
    assert gateway.context.plan.revision == 1


def test_run_command_allows_safe_diagnostic_variants(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)
    gateway = ToolGateway(
        ToolContext(repository),
        [RunCommandTool()],
        policy=ToolPolicy(
            frozenset({PermissionLevel.EXECUTE}),
            require_plan_for_mutations=False,
        ),
    )

    allowed = gateway.execute(
        "task-1",
        ToolCall(
            name="run_command",
            arguments={"command": ["python", "-m", "compileall", "-q", "."]},
        ),
    )
    denied = gateway.execute(
        "task-1",
        ToolCall(name="run_command", arguments={"command": ["python", "-c", "print(1)"]}),
    )
    scoped_compile = gateway.execute(
        "task-1",
        ToolCall(
            name="run_command",
            arguments={"command": ["python", "-m", "compileall", "src/calculator.py"]},
        ),
    )

    assert allowed.success and '"exit_code": 0' in allowed.output
    assert scoped_compile.success and '"exit_code": 0' in scoped_compile.output
    assert RunCommandTool._normalize_command(["git", "status"]) == ["git", "status"]
    assert not denied.success
    assert denied.error_kind is ErrorKind.PERMISSION_DENIED
    with pytest.raises(ValueError, match="escapes repository"):
        RunCommandTool._normalize_command(["python", "-m", "compileall", "../outside.py"])
