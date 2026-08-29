from pathlib import Path

import pytest

from patchloop.domain import ErrorKind, ToolCall
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
    SearchTextTool,
    ToolContext,
    ToolGateway,
    ToolPolicy,
    UpdatePlanTool,
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
    assert read.success and "2:     return left + right" in read.output
    assert searched.success and "src/calculator.py:2" in searched.output


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
    assert result.error_kind is ErrorKind.EXECUTION_ERROR


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


def test_run_command_uses_exact_allowlist(tmp_path: Path) -> None:
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

    assert allowed.success and '"exit_code": 0' in allowed.output
    assert not denied.success
    assert denied.error_kind is ErrorKind.EXECUTION_ERROR
