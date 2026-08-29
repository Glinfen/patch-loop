"""PatchLoop command-line interface."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from patchloop.domain import Task, TaskBudget, TaskStatus
from patchloop.events import EventLogger
from patchloop.providers import DeepSeekProvider
from patchloop.storage import JsonTaskStore, TaskNotFoundError
from patchloop.tools import (
    CreateFileTool,
    GetDiffTool,
    ListFilesTool,
    PermissionLevel,
    ReadFileTool,
    ReplaceTextTool,
    RunTestsTool,
    SearchTextTool,
    ToolContext,
    ToolGateway,
    ToolPolicy,
)
from patchloop.tools.base import Tool

app = typer.Typer(help="PatchLoop local-first coding agent runtime.", no_args_is_help=True)
task_app = typer.Typer(help="Create and inspect local tasks.", no_args_is_help=True)
app.add_typer(task_app, name="task")


def _state_dir(repository: Path) -> Path:
    return repository.resolve() / ".patchloop"


def _all_tools() -> list[Tool]:
    return [
        ListFilesTool(),
        ReadFileTool(),
        SearchTextTool(),
        CreateFileTool(),
        ReplaceTextTool(),
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
    JsonTaskStore(_state_dir(repo) / "tasks").save(task)
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
        task = JsonTaskStore(_state_dir(repo) / "tasks").get(task_id)
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
) -> None:
    try:
        provider = DeepSeekProvider.from_env()
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from None

    repository = repo.resolve()
    task = Task(
        goal=goal,
        repository=str(repository),
        budget=TaskBudget(max_steps=max_steps),
    )
    state = _state_dir(repository)
    store = JsonTaskStore(state / "tasks")
    store.save(task)
    trace = EventLogger(state / "traces" / f"{task.id}.jsonl")
    permissions = {PermissionLevel.READ}
    if allow_write:
        permissions.add(PermissionLevel.WRITE)
    if allow_execute:
        permissions.add(PermissionLevel.EXECUTE)
    context = ToolContext(repository)
    gateway = ToolGateway(
        context,
        _all_tools(),
        trace,
        ToolPolicy(frozenset(permissions)),
    )
    from patchloop.runtime import AgentRuntime

    result = AgentRuntime(provider, gateway, trace).run(task)
    store.save(result)
    typer.echo(result.model_dump_json(indent=2))
    diff = context.changes.diff()
    if diff:
        typer.echo("\n--- diff ---\n")
        typer.echo(diff)
    if result.status is not TaskStatus.COMPLETED:
        raise typer.Exit(code=1)
