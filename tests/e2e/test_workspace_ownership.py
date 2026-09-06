"""SRF-03 step 5 cross-process workspace single-writer acceptance tests."""

from __future__ import annotations

import multiprocessing
from pathlib import Path

import pytest
from pydantic import BaseModel

from patchloop.domain import Task, ToolCall
from patchloop.execution.ownership import ExecutionOwnershipManager
from patchloop.persistence import SQLiteStore
from patchloop.persistence_contracts import LeaseConflict, LeaseLost
from patchloop.providers import FakeProvider, ModelResponse
from patchloop.runtime import AgentRuntime
from patchloop.tools.base import PermissionLevel, Tool, ToolContext, ToolInputModel
from patchloop.tools.gateway import ToolGateway, ToolPolicy


def _workspace_claim_worker(
    database: str,
    repository: str,
    task_id: str,
    owner_id: str,
    start: multiprocessing.synchronize.Event,
    release: multiprocessing.synchronize.Event,
    outcomes: multiprocessing.queues.Queue,
) -> None:
    store = SQLiteStore(Path(database))
    task = store.get_task(task_id)
    manager = ExecutionOwnershipManager(store)
    if not start.wait(10):
        outcomes.put(("start_timeout", owner_id))
        return
    try:
        ownership = manager.acquire(
            session_id=task.session_id or "",
            task_id=task.id,
            owner_id=owner_id,
            repository=Path(repository),
            expected_version=task.version,
            workspace_writer=True,
        )
    except LeaseConflict as exc:
        outcomes.put(("conflict", owner_id, exc.resource_id))
        return
    outcomes.put(("acquired", owner_id, ownership.workspace_lease.workspace_id))
    if not release.wait(10):
        outcomes.put(("release_timeout", owner_id))
        return
    manager.release(ownership)


def _seed_task(store: SQLiteStore, repository: Path, task_id: str) -> Task:
    return store.prepare_task_execution(
        Task(id=task_id, goal="Own the workspace", repository=str(repository))
    )


def _run_claimers(
    database: Path,
    claims: list[tuple[Path, str, str]],
) -> list[tuple[str, str, str]]:
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    release = context.Event()
    outcomes = context.Queue()
    processes = [
        context.Process(
            target=_workspace_claim_worker,
            args=(
                str(database),
                str(repository),
                task_id,
                owner_id,
                start,
                release,
                outcomes,
            ),
        )
        for repository, task_id, owner_id in claims
    ]
    for process in processes:
        process.start()
    start.set()
    try:
        first_outcomes = [outcomes.get(timeout=15) for _ in processes]
    finally:
        release.set()
        for process in processes:
            process.join(timeout=15)
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
    assert [process.exitcode for process in processes] == [0] * len(processes)
    return first_outcomes


def test_two_processes_have_only_one_writer_for_the_same_workspace(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    database = tmp_path / "state.db"
    store = SQLiteStore(database)
    _seed_task(store, repository, "task-1")
    _seed_task(store, repository, "task-2")

    outcomes = _run_claimers(
        database,
        [
            (repository, "task-1", "worker-1"),
            (repository, "task-2", "worker-2"),
        ],
    )

    assert sorted(outcome[0] for outcome in outcomes) == ["acquired", "conflict"]
    assert outcomes[0][2] == outcomes[1][2]


def test_processes_can_write_different_workspaces_concurrently(tmp_path: Path) -> None:
    first_repository = tmp_path / "repository-1"
    second_repository = tmp_path / "repository-2"
    first_repository.mkdir()
    second_repository.mkdir()
    database = tmp_path / "state.db"
    store = SQLiteStore(database)
    _seed_task(store, first_repository, "task-1")
    _seed_task(store, second_repository, "task-2")

    outcomes = _run_claimers(
        database,
        [
            (first_repository, "task-1", "worker-1"),
            (second_repository, "task-2", "worker-2"),
        ],
    )

    assert [outcome[0] for outcome in outcomes] == ["acquired", "acquired"]
    assert outcomes[0][2] != outcomes[1][2]


class _NoInput(ToolInputModel):
    pass


class _MutationProbe(Tool):
    description = "Record that a mutation tool started."
    input_model = _NoInput

    def __init__(self, permission: PermissionLevel) -> None:
        self.name = f"probe_{permission.value}"
        self.permission = permission
        self.started = False

    def run(self, arguments: BaseModel, context: ToolContext) -> str:
        self.started = True
        return "started"


class _TaskLeaseOnlyManager(ExecutionOwnershipManager):
    def acquire(self, **kwargs):
        kwargs["workspace_writer"] = False
        return super().acquire(**kwargs)


@pytest.mark.parametrize("permission", [PermissionLevel.WRITE, PermissionLevel.EXECUTE])
def test_mutation_tool_cannot_run_with_only_a_task_lease(
    tmp_path: Path, permission: PermissionLevel
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    store = SQLiteStore(tmp_path / "state.db")
    tool = _MutationProbe(permission)
    gateway = ToolGateway(
        ToolContext(repository),
        [tool],
        policy=ToolPolicy(
            frozenset({permission}),
            require_plan_for_mutations=False,
        ),
    )
    provider = FakeProvider([ModelResponse(tool_calls=[ToolCall(id="mutation-1", name=tool.name)])])

    with pytest.raises(LeaseLost):
        AgentRuntime(
            provider,
            gateway,
            state_store=store,
            ownership_manager=_TaskLeaseOnlyManager(store),
            owner_id="task-only-worker",
        ).run(Task(id="task-1", goal="Mutate", repository=str(repository)))

    assert not tool.started
    assert store.get_tool_result("task-1", "mutation-1") is None
