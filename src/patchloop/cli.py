"""PatchLoop command-line interface."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from patchloop.domain import Task, TaskBudget, TaskExecutionConfig, TaskStatus
from patchloop.events import EventLogger
from patchloop.intelligence import (
    RepositoryIndexer,
    RepositorySearch,
    RepositorySnapshot,
    evaluate_retrieval,
    load_retrieval_tasks,
)
from patchloop.persistence import SQLiteStore
from patchloop.providers import DeepSeekProvider
from patchloop.runtime import AgentRuntime
from patchloop.storage import ArtifactStore, TaskNotFoundError
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
)
from patchloop.tools.base import Tool

app = typer.Typer(help="PatchLoop local-first coding agent runtime.", no_args_is_help=True)
task_app = typer.Typer(help="Create and inspect local tasks.", no_args_is_help=True)
app.add_typer(task_app, name="task")


def _state_dir(repository: Path) -> Path:
    return repository.resolve() / ".patchloop"


def _sqlite_store(repository: Path) -> SQLiteStore:
    return SQLiteStore(_state_dir(repository) / "patchloop.db")


def _all_tools() -> list[Tool]:
    return [
        ListFilesTool(),
        ReadFileTool(),
        SearchTextTool(),
        SearchCodeTool(),
        UpdatePlanTool(),
        CreateFileTool(),
        ApplyPatchTool(),
        ReplaceTextTool(),
        RunCommandTool(),
        RunTestsTool(),
        GetDiffTool(),
    ]


@task_app.command("create")
def create_task(
    goal: Annotated[str, typer.Argument(help="Natural-language development goal.")],
    repo: Annotated[
        Path,
        typer.Option(exists=True, file_okay=False, resolve_path=True),
    ] = Path("."),
) -> None:
    task = Task(goal=goal, repository=str(repo.resolve()))
    _sqlite_store(repo).save_task(task)
    typer.echo(task.model_dump_json(indent=2))


@task_app.command("show")
def show_task(
    task_id: Annotated[str, typer.Argument()],
    repo: Annotated[
        Path,
        typer.Option(exists=True, file_okay=False, resolve_path=True),
    ] = Path("."),
) -> None:
    try:
        task = _sqlite_store(repo).get_task(task_id)
    except TaskNotFoundError:
        typer.echo(f"task not found: {task_id}", err=True)
        raise typer.Exit(code=1) from None
    typer.echo(task.model_dump_json(indent=2))


@app.command("tools")
def list_tools(
    repo: Annotated[
        Path,
        typer.Option(exists=True, file_okay=False, resolve_path=True),
    ] = Path("."),
) -> None:
    gateway = ToolGateway(
        ToolContext(repo),
        _all_tools(),
    )
    typer.echo(json.dumps([item.model_dump() for item in gateway.specifications()], indent=2))


def _index_path(repository: Path) -> Path:
    return _state_dir(repository) / "repository-index.json"


def _load_or_build_index(repository: Path) -> tuple[RepositorySnapshot, bool]:
    indexer = RepositoryIndexer(repository)
    path = _index_path(repository)
    if path.is_file():
        try:
            snapshot = RepositorySnapshot.load(path)
            if indexer.is_current(snapshot):
                return snapshot, False
        except (OSError, ValueError):
            pass
    snapshot = indexer.build()
    snapshot.save(path)
    return snapshot, True


@app.command("index")
def index_repository(
    repo: Annotated[
        Path,
        typer.Option(exists=True, file_okay=False, resolve_path=True),
    ] = Path("."),
) -> None:
    snapshot = RepositoryIndexer(repo).build()
    path = snapshot.save(_index_path(repo))
    typer.echo(
        json.dumps(
            {
                "path": str(path),
                "files": len(snapshot.files),
                "symbols": snapshot.symbol_count,
                "references": snapshot.reference_count,
                "test_mappings": len(snapshot.test_mappings),
            },
            indent=2,
        )
    )


@app.command("search")
def search_repository(
    query: Annotated[str, typer.Argument(help="Natural-language code location query.")],
    repo: Annotated[
        Path,
        typer.Option(exists=True, file_okay=False, resolve_path=True),
    ] = Path("."),
    limit: Annotated[int, typer.Option(min=1, max=50)] = 10,
) -> None:
    snapshot, rebuilt = _load_or_build_index(repo)
    hits = RepositorySearch(repo, snapshot).search(query, limit=limit)
    typer.echo(
        json.dumps(
            {
                "index_rebuilt": rebuilt,
                "results": [hit.model_dump(mode="json") for hit in hits],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


@app.command("benchmark-search")
def benchmark_repository_search(
    tasks: Annotated[
        Path,
        typer.Option(exists=True, dir_okay=False, resolve_path=True),
    ] = Path("benchmarks/retrieval_tasks.json"),
    root: Annotated[
        Path,
        typer.Option(exists=True, file_okay=False, resolve_path=True),
    ] = Path("."),
    output: Annotated[str, typer.Option(help="Optional JSON report path.")] = "",
) -> None:
    report = evaluate_retrieval(load_retrieval_tasks(tasks), root)
    if output:
        output_path = Path(output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    typer.echo(report.model_dump_json(indent=2))


@app.command("trace")
def show_trace(
    task_id: Annotated[str, typer.Argument()],
    repo: Annotated[
        Path,
        typer.Option(exists=True, file_okay=False, resolve_path=True),
    ] = Path("."),
) -> None:
    logger = EventLogger(_state_dir(repo) / "traces" / f"{task_id}.jsonl")
    for event in logger.read():
        typer.echo(event.model_dump_json())


@app.command("status")
def task_status(
    task_id: Annotated[str, typer.Argument()],
    repo: Annotated[
        Path,
        typer.Option(exists=True, file_okay=False, resolve_path=True),
    ] = Path("."),
) -> None:
    try:
        task = _sqlite_store(repo).get_task(task_id)
    except TaskNotFoundError:
        typer.echo(f"task not found: {task_id}", err=True)
        raise typer.Exit(code=1) from None
    typer.echo(task.model_dump_json(indent=2))


@app.command("cancel")
def cancel_task(
    task_id: Annotated[str, typer.Argument()],
    repo: Annotated[
        Path,
        typer.Option(exists=True, file_okay=False, resolve_path=True),
    ] = Path("."),
) -> None:
    try:
        task = _sqlite_store(repo).cancel_task(task_id)
    except TaskNotFoundError:
        typer.echo(f"task not found: {task_id}", err=True)
        raise typer.Exit(code=1) from None
    typer.echo(task.model_dump_json(indent=2))


@app.command("diff")
def task_diff(
    task_id: Annotated[str, typer.Argument()],
    repo: Annotated[
        Path,
        typer.Option(exists=True, file_okay=False, resolve_path=True),
    ] = Path("."),
) -> None:
    store = _sqlite_store(repo)
    try:
        task = store.get_task(task_id)
    except TaskNotFoundError:
        typer.echo(f"task not found: {task_id}", err=True)
        raise typer.Exit(code=1) from None
    if task.report is not None:
        typer.echo(task.report.diff or "No changes.")
        return
    try:
        checkpoint = store.get_checkpoint(task_id)
    except TaskNotFoundError:
        typer.echo("No changes.")
        return
    context = ToolContext(repo)
    context.changes.restore(checkpoint.change_snapshot)
    typer.echo(context.changes.diff() or "No changes.")


@app.command("run")
def run_task(
    goal: Annotated[str, typer.Argument(help="Natural-language development goal.")],
    repo: Annotated[
        Path,
        typer.Option(exists=True, file_okay=False, resolve_path=True),
    ] = Path("."),
    allow_write: Annotated[
        bool,
        typer.Option(help="Allow atomic file creation and exact text replacement."),
    ] = False,
    allow_execute: Annotated[
        bool,
        typer.Option(help="Allow restricted pytest or unittest execution."),
    ] = False,
    max_steps: Annotated[int, typer.Option(min=1, max=1_000)] = 20,
    max_input_tokens: Annotated[int, typer.Option(min=1)] = 500_000,
    max_output_tokens: Annotated[int, typer.Option(min=1)] = 100_000,
    max_cost_usd: Annotated[float, typer.Option(min=0.0001)] = 5.0,
    max_tool_failures: Annotated[int, typer.Option(min=0, max=1_000)] = 10,
    non_interactive: Annotated[
        bool,
        typer.Option(help="Run without approval prompts; suitable for CI."),
    ] = True,
) -> None:
    try:
        provider = DeepSeekProvider.from_env()
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from None

    repository = repo.resolve()
    permissions = {PermissionLevel.READ}
    if allow_write:
        permissions.add(PermissionLevel.WRITE)
    if allow_execute:
        permissions.add(PermissionLevel.EXECUTE)
    task = Task(
        goal=goal,
        repository=str(repository),
        budget=TaskBudget(
            max_steps=max_steps,
            max_input_tokens=max_input_tokens,
            max_output_tokens=max_output_tokens,
            max_cost_usd=max_cost_usd,
            max_tool_failures=max_tool_failures,
        ),
        execution=TaskExecutionConfig(
            allowed_permissions=sorted(permission.value for permission in permissions),
            non_interactive=non_interactive,
        ),
    )
    state = _state_dir(repository)
    store = _sqlite_store(repository)
    store.save_task(task)
    trace = EventLogger(state / "traces" / f"{task.id}.jsonl")
    context = ToolContext(repository)
    gateway = ToolGateway(
        context,
        _all_tools(),
        trace,
        ToolPolicy(frozenset(permissions)),
    )
    result = AgentRuntime(provider, gateway, trace, store).run(task)
    paths = ArtifactStore(state / "artifacts").save_report(result)
    for path in paths:
        store.record_artifact(task.id, path)
    typer.echo(result.model_dump_json(indent=2))
    diff = context.changes.diff()
    if diff:
        typer.echo("\n--- diff ---\n")
        typer.echo(diff)
    if result.status is not TaskStatus.COMPLETED:
        raise typer.Exit(code=1)


@app.command("resume")
def resume_task(
    task_id: Annotated[str, typer.Argument()],
    repo: Annotated[
        Path,
        typer.Option(exists=True, file_okay=False, resolve_path=True),
    ] = Path("."),
) -> None:
    try:
        provider = DeepSeekProvider.from_env()
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from None
    repository = repo.resolve()
    store = _sqlite_store(repository)
    try:
        task = store.get_task(task_id)
        checkpoint = store.get_checkpoint(task_id)
    except TaskNotFoundError:
        typer.echo(f"task or checkpoint not found: {task_id}", err=True)
        raise typer.Exit(code=1) from None
    if task.status is not TaskStatus.RUNNING:
        typer.echo(f"task cannot resume from status: {task.status}", err=True)
        raise typer.Exit(code=1)
    try:
        permissions = frozenset(
            PermissionLevel(value) for value in task.execution.allowed_permissions
        )
    except ValueError:
        typer.echo("task contains an invalid permission checkpoint", err=True)
        raise typer.Exit(code=1) from None
    trace = EventLogger(_state_dir(repository) / "traces" / f"{task.id}.jsonl")
    context = ToolContext(repository)
    gateway = ToolGateway(
        context,
        _all_tools(),
        trace,
        ToolPolicy(permissions),
    )
    result = AgentRuntime(provider, gateway, trace, store).resume(task, checkpoint)
    paths = ArtifactStore(_state_dir(repository) / "artifacts").save_report(result)
    for path in paths:
        store.record_artifact(task.id, path)
    typer.echo(result.model_dump_json(indent=2))
    if result.status is not TaskStatus.COMPLETED:
        raise typer.Exit(code=1)
