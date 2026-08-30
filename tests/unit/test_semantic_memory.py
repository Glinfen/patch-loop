from __future__ import annotations

from patchloop.domain import ErrorKind, ToolCall, ToolResult
from patchloop.memory.models import MemoryScope, MemoryStatus
from patchloop.memory.semantic import (
    DeterministicSemanticExtractor,
    FactEpistemicStatus,
    SemanticExtractionEvent,
    SemanticFactDraft,
    SemanticFactType,
    SemanticMemoryManager,
)


def _result(
    call: ToolCall,
    output: str,
    *,
    success: bool = True,
    error_kind: ErrorKind | None = None,
) -> ToolResult:
    return ToolResult(
        call_id=call.id,
        tool_name=call.name,
        success=success,
        output=output,
        error_kind=error_kind,
    )


def test_initial_user_constraints_are_explicit_authoritative_facts() -> None:
    memory = SemanticMemoryManager(
        "task-constraints",
        "Only edit src. Do not expose .env. 必须保持 API 兼容。",
        "repo",
    )

    batch = memory.initial_facts()

    assert len(batch.records) == 3
    assert all(record.content["epistemic_status"] == "user_asserted" for record in batch.records)
    assert all(record.content["authority"] == 80 for record in batch.records)
    assert {record.content["fact_type"] for record in batch.records} == {
        "constraint",
        "prohibition",
    }
    assert len(batch.sources) == 1


def test_later_observation_supersedes_stale_value_in_the_same_slot() -> None:
    memory = SemanticMemoryManager("task-code", "Inspect mode", "repo")
    read = ToolCall(id="read-old", name="read_file", arguments={"path": "src/config.py"})
    observed = memory.observe_tool(
        read,
        _result(read, '1: MODE = "old"'),
        plan=None,
        changed_paths=[],
        diff="",
    )
    old = observed.records[0]
    patch = ToolCall(id="patch-new", name="apply_patch", arguments={"path": "src/config.py"})
    replacement = memory.observe_tool(
        patch,
        _result(patch, "patched"),
        plan=None,
        changed_paths=["src/config.py"],
        diff=(
            '--- a/src/config.py\n+++ b/src/config.py\n@@ -1 +1 @@\n-MODE = "old"\n+MODE = "new"\n'
        ),
    )

    assert replacement.superseded_count == 1
    superseded = next(record for record in replacement.records if record.id == old.id)
    current = next(record for record in replacement.records if record.id != old.id)
    assert superseded.status is MemoryStatus.SUPERSEDED
    assert superseded.superseded_by_id == current.id
    assert current.supersedes_id == superseded.id
    assert current.content["value"] == '"new"'
    assert [record.id for record in memory.active_records()] == [current.id]


class SequenceExtractor(DeterministicSemanticExtractor):
    def __init__(self, drafts: list[list[SemanticFactDraft]]) -> None:
        self.drafts = iter(drafts)

    def initial_facts(self, task_id: str, goal: str) -> list[SemanticFactDraft]:
        del task_id, goal
        return []

    def extract(self, event: SemanticExtractionEvent) -> list[SemanticFactDraft]:
        del event
        return next(self.drafts)


class ClaimingModelExtractor:
    def __init__(self, draft: SemanticFactDraft) -> None:
        self.draft = draft

    def extract(self, event: SemanticExtractionEvent) -> list[SemanticFactDraft]:
        del event
        return [self.draft]


def _draft(value: str, status: FactEpistemicStatus) -> SemanticFactDraft:
    return SemanticFactDraft(
        fact_type=SemanticFactType.CODE_SYMBOL,
        subject="src/config.py:MODE",
        predicate="value",
        value=value,
        epistemic_status=status,
        scope=MemoryScope.REPOSITORY,
        scope_id="repo",
        path="src/config.py",
        confidence=1.0,
    )


def test_lower_authority_conflict_is_invalidated_not_returned_as_active() -> None:
    guess = _draft('"guess"', FactEpistemicStatus.INFERRED)
    extractor = SequenceExtractor(
        [
            [_draft('"verified"', FactEpistemicStatus.VERIFIED)],
            [guess],
            [guess],
        ]
    )
    memory = SemanticMemoryManager(
        "task-authority",
        "Inspect mode",
        "repo",
        extractor=extractor,
    )
    first = ToolCall(id="verified", name="custom")
    memory.observe_tool(
        first,
        _result(first, "verified"),
        plan=None,
        changed_paths=[],
        diff="",
    )
    second = ToolCall(id="guess", name="custom")
    rejected = memory.observe_tool(
        second,
        _result(second, "guess"),
        plan=None,
        changed_paths=[],
        diff="",
    )

    assert rejected.rejected_conflict_count == 1
    assert rejected.records[0].status is MemoryStatus.INVALIDATED
    assert rejected.records[0].content["conflicts_with_id"] == memory.active_records()[0].id
    assert memory.active_records()[0].content["value"] == '"verified"'
    replay = ToolCall(id="guess", name="custom")
    duplicate = memory.observe_tool(
        replay,
        _result(replay, "guess"),
        plan=None,
        changed_paths=[],
        diff="",
    )
    assert duplicate.records == ()
    assert duplicate.suppressed_duplicate_count == 1


def test_model_assisted_claims_are_forced_to_inferred_status() -> None:
    claimed = _draft('"claimed verified"', FactEpistemicStatus.VERIFIED)
    memory = SemanticMemoryManager(
        "task-model",
        "Inspect mode",
        "repo",
        extractor=SequenceExtractor([[]]),
        model_extractor=ClaimingModelExtractor(claimed),
    )
    call = ToolCall(id="model-claim", name="custom")

    batch = memory.observe_tool(
        call,
        _result(call, "claim"),
        plan=None,
        changed_paths=[],
        diff="",
    )

    assert len(batch.records) == 1
    assert batch.records[0].content["fact_type"] == "inference"
    assert batch.records[0].content["epistemic_status"] == "inferred"
    assert batch.records[0].content["authority"] == 20
    assert batch.records[0].confidence == 0.6


def test_successful_test_replaces_failed_result_with_verified_fact() -> None:
    memory = SemanticMemoryManager("task-tests", "Run tests", "repo")
    arguments = {"command": ["python", "-m", "pytest", "-q"]}
    failed_call = ToolCall(id="tests-failed", name="run_tests", arguments=arguments)
    failed = memory.observe_tool(
        failed_call,
        _result(
            failed_call,
            "1 failed",
            success=False,
            error_kind=ErrorKind.TEST_FAILURE,
        ),
        plan=None,
        changed_paths=[],
        diff="",
    )
    passed_call = ToolCall(id="tests-passed", name="run_tests", arguments=arguments)
    passed = memory.observe_tool(
        passed_call,
        _result(passed_call, "1 passed"),
        plan=None,
        changed_paths=[],
        diff="",
    )

    assert passed.superseded_count == 1
    assert failed.records[0].id == next(
        record.id for record in passed.records if record.status is MemoryStatus.SUPERSEDED
    )
    active = memory.active_records()[0]
    assert active.content["value"] == "passed"
    assert active.content["epistemic_status"] == "verified"


def test_oversized_observation_is_bounded_without_losing_stable_identity() -> None:
    memory = SemanticMemoryManager("task-large", "Inspect large value", "repo")
    call = ToolCall(id="read-large", name="read_file", arguments={"path": "large.py"})

    batch = memory.observe_tool(
        call,
        _result(call, "1: PAYLOAD = '" + "x" * 12_000 + "'"),
        plan=None,
        changed_paths=[],
        diff="",
    )

    assert len(batch.records) == 1
    value = batch.records[0].content["value"]
    assert isinstance(value, str)
    assert len(value) == 2_000
    assert "…#" in value
