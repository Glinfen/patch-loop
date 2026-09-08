"""SRF-07 multi-process ownership contention matrix."""

from __future__ import annotations

import argparse
import multiprocessing
from enum import StrEnum
from pathlib import Path
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from patchloop.domain import Task, utc_now
from patchloop.execution.ownership import ExecutionOwnershipManager
from patchloop.persistence import SQLiteStore
from patchloop.persistence_contracts import LeaseConflict
from patchloop.session.models import Session


class OwnershipScope(StrEnum):
    SESSION = "session"
    TASK = "task"
    WORKSPACE = "workspace"


class OwnershipRoundResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    round: int = Field(ge=1)
    acquired_count: int = Field(ge=0)
    conflict_count: int = Field(ge=0)
    error_count: int = Field(ge=0)
    winner_owner_id: str | None = None
    all_contenders_returned_before_release: bool
    errors: list[str] = Field(default_factory=list)


class OwnershipScopeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: OwnershipScope
    contenders: int
    rounds_expected: int
    passed: bool
    reacquired_after_release: bool
    rounds: list[OwnershipRoundResult]

    @model_validator(mode="after")
    def retains_every_round(self) -> Self:
        if len(self.rounds) != self.rounds_expected:
            raise ValueError("ownership result must retain every configured round")
        if [item.round for item in self.rounds] != list(range(1, self.rounds_expected + 1)):
            raise ValueError("ownership rounds must be complete and ordered")
        return self


class ParallelWorkspaceResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contenders: int
    acquired_count: int
    conflict_count: int
    error_count: int
    passed: bool
    errors: list[str] = Field(default_factory=list)


class OwnershipContentionReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    contenders: int
    rounds_per_scope: int
    passed: bool
    scopes: list[OwnershipScopeResult]
    different_workspaces: ParallelWorkspaceResult
    generated_at: str = Field(default_factory=lambda: utc_now().isoformat())

    @model_validator(mode="after")
    def aggregate_is_consistent(self) -> Self:
        if {result.scope for result in self.scopes} != set(OwnershipScope):
            raise ValueError("ownership report must contain all three contention scopes")
        if any(result.contenders != self.contenders for result in self.scopes):
            raise ValueError("scope contender count does not match report")
        if any(result.rounds_expected != self.rounds_per_scope for result in self.scopes):
            raise ValueError("scope round count does not match report")
        expected = all(result.passed for result in self.scopes) and self.different_workspaces.passed
        if self.passed != expected:
            raise ValueError("ownership report pass aggregate is inconsistent")
        return self


def _contention_worker(
    database: str,
    repository: str,
    task_id: str,
    owner_id: str,
    rounds: int,
    workspace_writer: bool,
    start_barrier: Any,
    outcomes_barrier: Any,
    release_barrier: Any,
    released_barrier: Any,
    outcomes: Any,
) -> None:
    store = SQLiteStore(Path(database))
    manager = ExecutionOwnershipManager(store)
    for round_number in range(1, rounds + 1):
        ownership = None
        start_barrier.wait(timeout=60)
        try:
            task = store.get_task(task_id)
            ownership = manager.acquire(
                session_id=task.session_id or "",
                task_id=task.id,
                owner_id=owner_id,
                repository=Path(repository),
                expected_version=task.version,
                workspace_writer=workspace_writer,
            )
            outcomes.put((round_number, "acquired", owner_id, ""))
        except LeaseConflict as exc:
            outcomes.put((round_number, "conflict", owner_id, exc.resource_id))
        except BaseException as exc:
            outcomes.put(
                (
                    round_number,
                    "error",
                    owner_id,
                    f"{type(exc).__name__}: {exc}",
                )
            )
        outcomes_barrier.wait(timeout=60)
        release_barrier.wait(timeout=60)
        if ownership is not None:
            try:
                manager.release(ownership)
                outcomes.put((round_number, "released", owner_id, ""))
            except BaseException as exc:
                outcomes.put(
                    (
                        round_number,
                        "release_error",
                        owner_id,
                        f"{type(exc).__name__}: {exc}",
                    )
                )
        else:
            outcomes.put((round_number, "not_owner", owner_id, ""))
        released_barrier.wait(timeout=60)


class OwnershipContentionRunner:
    def __init__(self, root: Path, *, contenders: int = 8, rounds: int = 100) -> None:
        if contenders < 2:
            raise ValueError("ownership contention requires at least two contenders")
        if rounds < 1:
            raise ValueError("ownership contention rounds must be positive")
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.contenders = contenders
        self.rounds = rounds

    def run(self) -> OwnershipContentionReport:
        scopes = [self._run_scope(scope) for scope in OwnershipScope]
        different_workspaces = self._run_different_workspaces()
        return OwnershipContentionReport(
            contenders=self.contenders,
            rounds_per_scope=self.rounds,
            passed=all(result.passed for result in scopes) and different_workspaces.passed,
            scopes=scopes,
            different_workspaces=different_workspaces,
        )

    def _run_scope(self, scope: OwnershipScope) -> OwnershipScopeResult:
        scope_root = self.root / scope.value
        scope_root.mkdir(parents=True, exist_ok=True)
        database = scope_root / "state.db"
        claims = self._seed_claims(scope, database, scope_root)
        outcomes = self._run_claims(database, claims, rounds=self.rounds)
        round_results = [
            self._summarize_round(round_number, outcomes[round_number])
            for round_number in range(1, self.rounds + 1)
        ]
        reacquired = self._verify_reacquisition(database, claims[0])
        passed = reacquired and all(
            item.acquired_count == 1
            and item.conflict_count == self.contenders - 1
            and item.error_count == 0
            and item.all_contenders_returned_before_release
            for item in round_results
        )
        return OwnershipScopeResult(
            scope=scope,
            contenders=self.contenders,
            rounds_expected=self.rounds,
            passed=passed,
            reacquired_after_release=reacquired,
            rounds=round_results,
        )

    def _seed_claims(
        self, scope: OwnershipScope, database: Path, scope_root: Path
    ) -> list[tuple[Path, str, str, bool]]:
        store = SQLiteStore(database)
        repository = scope_root / "repository"
        repository.mkdir()
        if scope is OwnershipScope.SESSION:
            session = store.create_session(
                Session(id="shared-session", workspace_ref=str(repository))
            )
            claims = []
            for index in range(self.contenders):
                task = store.create_task(
                    Task(
                        id=f"task-{index}",
                        session_id=session.id,
                        goal="Compete for one Session",
                        repository=str(repository),
                    )
                )
                claims.append((repository, task.id, f"worker-{index}", False))
            return claims
        if scope is OwnershipScope.TASK:
            task = store.prepare_task_execution(
                Task(id="shared-task", goal="Compete for one Task", repository=str(repository))
            )
            return [
                (repository, task.id, f"worker-{index}", False) for index in range(self.contenders)
            ]
        claims = []
        for index in range(self.contenders):
            task = store.prepare_task_execution(
                Task(
                    id=f"task-{index}",
                    goal="Compete for one Workspace",
                    repository=str(repository),
                )
            )
            claims.append((repository, task.id, f"worker-{index}", True))
        return claims

    def _run_claims(
        self,
        database: Path,
        claims: list[tuple[Path, str, str, bool]],
        *,
        rounds: int,
    ) -> dict[int, list[tuple[str, str, str]]]:
        context = multiprocessing.get_context("spawn")
        parties = len(claims) + 1
        start_barrier = context.Barrier(parties)
        outcomes_barrier = context.Barrier(parties)
        release_barrier = context.Barrier(parties)
        released_barrier = context.Barrier(parties)
        outcome_queue = context.Queue()
        processes = [
            context.Process(
                target=_contention_worker,
                args=(
                    str(database),
                    str(repository),
                    task_id,
                    owner_id,
                    rounds,
                    workspace_writer,
                    start_barrier,
                    outcomes_barrier,
                    release_barrier,
                    released_barrier,
                    outcome_queue,
                ),
            )
            for repository, task_id, owner_id, workspace_writer in claims
        ]
        for process in processes:
            process.start()
        collected: dict[int, list[tuple[str, str, str]]] = {
            round_number: [] for round_number in range(1, rounds + 1)
        }
        try:
            for _round_number in range(1, rounds + 1):
                start_barrier.wait(timeout=60)
                outcomes_barrier.wait(timeout=60)
                for _ in claims:
                    observed_round, status, owner_id, detail = outcome_queue.get(timeout=60)
                    collected[int(observed_round)].append((status, owner_id, detail))
                release_barrier.wait(timeout=60)
                released_barrier.wait(timeout=60)
                for _ in claims:
                    observed_round, status, owner_id, detail = outcome_queue.get(timeout=60)
                    if status == "release_error":
                        collected[int(observed_round)].append((status, owner_id, detail))
        finally:
            for process in processes:
                process.join(timeout=10)
            for process in processes:
                if process.is_alive():
                    process.terminate()
            for process in processes:
                process.join(timeout=5)
                if process.is_alive():
                    process.kill()
                    process.join(timeout=2)
        exit_errors = [
            f"worker process exited with {process.exitcode}"
            for process in processes
            if process.exitcode != 0
        ]
        if exit_errors:
            collected[rounds].extend(("error", "process", error) for error in exit_errors)
        return collected

    def _summarize_round(
        self, round_number: int, outcomes: list[tuple[str, str, str]]
    ) -> OwnershipRoundResult:
        acquired = [item for item in outcomes if item[0] == "acquired"]
        conflicts = [item for item in outcomes if item[0] == "conflict"]
        errors = [item for item in outcomes if item[0] not in {"acquired", "conflict"}]
        return OwnershipRoundResult(
            round=round_number,
            acquired_count=len(acquired),
            conflict_count=len(conflicts),
            error_count=len(errors),
            winner_owner_id=acquired[0][1] if len(acquired) == 1 else None,
            all_contenders_returned_before_release=len(acquired) + len(conflicts)
            == self.contenders,
            errors=[f"{status}:{owner}:{detail}" for status, owner, detail in errors],
        )

    @staticmethod
    def _verify_reacquisition(database: Path, claim: tuple[Path, str, str, bool]) -> bool:
        repository, task_id, _, workspace_writer = claim
        store = SQLiteStore(database)
        task = store.get_task(task_id)
        manager = ExecutionOwnershipManager(store)
        ownership = manager.acquire(
            session_id=task.session_id or "",
            task_id=task.id,
            owner_id="post-release-verifier",
            repository=repository,
            expected_version=task.version,
            workspace_writer=workspace_writer,
        )
        manager.release(ownership)
        return True

    def _run_different_workspaces(self) -> ParallelWorkspaceResult:
        root = self.root / "different-workspaces"
        root.mkdir(parents=True, exist_ok=True)
        database = root / "state.db"
        store = SQLiteStore(database)
        claims = []
        for index in range(self.contenders):
            repository = root / f"repository-{index}"
            repository.mkdir()
            task = store.prepare_task_execution(
                Task(
                    id=f"task-{index}",
                    goal="Own an independent Workspace",
                    repository=str(repository),
                )
            )
            claims.append((repository, task.id, f"worker-{index}", True))
        outcomes = self._run_claims(database, claims, rounds=1)[1]
        acquired = [item for item in outcomes if item[0] == "acquired"]
        conflicts = [item for item in outcomes if item[0] == "conflict"]
        errors = [item for item in outcomes if item[0] not in {"acquired", "conflict"}]
        return ParallelWorkspaceResult(
            contenders=self.contenders,
            acquired_count=len(acquired),
            conflict_count=len(conflicts),
            error_count=len(errors),
            passed=len(acquired) == self.contenders and not conflicts and not errors,
            errors=[f"{status}:{owner}:{detail}" for status, owner, detail in errors],
        )


def write_ownership_contention_report(report: OwnershipContentionReport, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the SRF-07 ownership contention matrix")
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--contenders", type=int, default=8)
    parser.add_argument("--rounds", type=int, default=100)
    arguments = parser.parse_args()
    report = OwnershipContentionRunner(
        arguments.work_root,
        contenders=arguments.contenders,
        rounds=arguments.rounds,
    ).run()
    write_ownership_contention_report(report, arguments.output)
    print(report.model_dump_json(indent=2))
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
