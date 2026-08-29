"""PatchLoop command-line interface."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from patchloop.domain import Task
from patchloop.events import EventLogger
from patchloop.storage import JsonTaskStore, TaskNotFoundError
from patchloop.tools import (
    CreateFileTool,
    GetDiffTool,
    ListFilesTool,
    ReadFileTool,
    ReplaceTextTool,
    RunTestsTool,
    SearchTextTool,
    ToolContext,
    ToolGateway,
)

app = typer.Typer(help="PatchLoop local-first coding agent runtime.", no_args_is_help=True)
task_app = typer.Typer(help="Create and inspect local tasks.", no_args_is_help=True)
app.add_typer(task_app, name="task")


def _state_dir(repository: Path) -> Path:
    return repository.resolve() / ".patchloop"


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
        [
            ListFilesTool(),
            ReadFileTool(),
            SearchTextTool(),
            CreateFileTool(),
            ReplaceTextTool(),
            RunTestsTool(),
            GetDiffTool(),
        ],
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
