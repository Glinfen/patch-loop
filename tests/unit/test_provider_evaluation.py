from __future__ import annotations

from pathlib import Path

from patchloop.evaluation.provider import (
    ProviderAcceptanceAttempt,
    ProviderAcceptanceCase,
    ProviderAcceptanceManifest,
    ProviderAcceptanceProfile,
    ProviderAcceptanceProfileKind,
    ProviderAcceptanceRunner,
    ProviderAcceptanceScenario,
    ProviderAcceptanceStatus,
    ProviderExpectedOutcome,
)
from patchloop.providers.contracts import ProviderProtocol


def _fixture_profile(profile_id: str, protocol: ProviderProtocol) -> ProviderAcceptanceProfile:
    return ProviderAcceptanceProfile(
        id=profile_id,
        kind=ProviderAcceptanceProfileKind.FIXTURE,
        protocol=protocol,
        implementation="loopback-http",
        implementation_version="1",
        endpoint="http://127.0.0.1:0",
        model="fixture-model",
        pricing_version="fixture-zero",
        input_price_per_million=0,
        output_price_per_million=0,
        generation={"temperature": 0, "max_output_tokens": 1024},
    )


def _real_profile(
    profile_id: str,
    kind: ProviderAcceptanceProfileKind,
    protocol: ProviderProtocol,
    prefix: str,
) -> ProviderAcceptanceProfile:
    return ProviderAcceptanceProfile(
        id=profile_id,
        kind=kind,
        protocol=protocol,
        implementation=kind.value,
        implementation_version_environment=f"{prefix}_VERSION",
        endpoint_environment=f"{prefix}_ENDPOINT",
        model_environment=f"{prefix}_MODEL",
        pricing_version_environment=f"{prefix}_PRICING_VERSION",
        input_price_environment=f"{prefix}_INPUT_PRICE",
        output_price_environment=f"{prefix}_OUTPUT_PRICE",
        credential_environment=(
            [] if kind is ProviderAcceptanceProfileKind.LOCAL else [f"{prefix}_KEY"]
        ),
        generation={"temperature": 0, "max_output_tokens": 1024},
    )


def _manifest() -> ProviderAcceptanceManifest:
    all_scenarios = list(ProviderAcceptanceScenario)
    profiles = [
        _fixture_profile("fixture-chat", ProviderProtocol.CHAT_COMPLETIONS),
        _fixture_profile("fixture-responses", ProviderProtocol.RESPONSES),
        _real_profile(
            "deepseek-chat",
            ProviderAcceptanceProfileKind.DEEPSEEK,
            ProviderProtocol.CHAT_COMPLETIONS,
            "DEEPSEEK",
        ),
        _real_profile(
            "openai-responses",
            ProviderAcceptanceProfileKind.OPENAI_RESPONSES,
            ProviderProtocol.RESPONSES,
            "OPENAI",
        ),
        _real_profile(
            "local-chat",
            ProviderAcceptanceProfileKind.LOCAL,
            ProviderProtocol.CHAT_COMPLETIONS,
            "LOCAL",
        ),
    ]
    cases = [
        ProviderAcceptanceCase(
            id=f"{profile.id}-case",
            profile_id=profile.id,
            scenarios=(
                all_scenarios
                if profile.kind is ProviderAcceptanceProfileKind.FIXTURE
                else [ProviderAcceptanceScenario.TEXT]
            ),
            expected_outcomes=[
                ProviderExpectedOutcome.COMPLETED,
                ProviderExpectedOutcome.CORRECTLY_REJECTED,
                ProviderExpectedOutcome.CORRECTLY_PAUSED,
            ],
            test_nodeids=[
                "tests/e2e/test_provider_session.py::test_loopback_http_session_core[chat]"
            ],
            repetitions=3,
        )
        for profile in profiles
    ]
    return ProviderAcceptanceManifest(profiles=profiles, cases=cases)


def _attempt(
    case: ProviderAcceptanceCase,
    profile: ProviderAcceptanceProfile,
    repetition: int,
    status: ProviderAcceptanceStatus,
    *,
    stdout: str = "",
) -> ProviderAcceptanceAttempt:
    checked = status is ProviderAcceptanceStatus.PASSED
    return ProviderAcceptanceAttempt(
        case_id=case.id,
        profile_id=profile.id,
        protocol=profile.protocol,
        scenarios=case.scenarios,
        expected_outcomes=case.expected_outcomes,
        repetition=repetition,
        status=status,
        duration_ms=1,
        stdout=stdout,
        detail="fixture executor",
        request_count_checked=checked,
        sqlite_checked=checked,
        trace_checked=checked,
        tool_audit_checked=checked,
        unauthorized_actions=0 if checked else None,
        duplicate_confirmed_effects=0 if checked else None,
        partial_stream_effects=0 if checked else None,
        secret_leaks=0 if checked else None,
    )


def test_offline_run_groups_profiles_and_keeps_real_attempts_unverified(tmp_path: Path) -> None:
    calls: list[tuple[str, int]] = []

    def execute(case, profile, repetition, environment):
        del environment
        calls.append((profile.id, repetition))
        return _attempt(case, profile, repetition, ProviderAcceptanceStatus.PASSED)

    report = ProviderAcceptanceRunner(tmp_path, environment={}, executor=execute).run(_manifest())

    assert report.offline_status is ProviderAcceptanceStatus.PASSED
    assert report.real_status is ProviderAcceptanceStatus.UNVERIFIED
    assert report.status is ProviderAcceptanceStatus.UNVERIFIED
    assert len(calls) == 6
    assert len(report.attempts) == 15
    assert {summary.profile_id for summary in report.profiles} == {
        "fixture-chat",
        "fixture-responses",
        "deepseek-chat",
        "openai-responses",
        "local-chat",
    }
    assert report.security_evidence_unverified == 9


def test_repetitions_preserve_a_failure_instead_of_selecting_best(tmp_path: Path) -> None:
    def execute(case, profile, repetition, environment):
        del environment
        status = (
            ProviderAcceptanceStatus.FAILED
            if profile.id == "fixture-chat" and repetition == 1
            else ProviderAcceptanceStatus.PASSED
        )
        return _attempt(case, profile, repetition, status)

    report = ProviderAcceptanceRunner(tmp_path, environment={}, executor=execute).run(_manifest())

    chat = next(summary for summary in report.profiles if summary.profile_id == "fixture-chat")
    assert chat.failed == 1
    assert chat.passed == 2
    assert chat.status is ProviderAcceptanceStatus.FAILED
    assert report.offline_status is ProviderAcceptanceStatus.FAILED
    assert [item.status for item in report.attempts if item.profile_id == "fixture-chat"] == [
        ProviderAcceptanceStatus.FAILED,
        ProviderAcceptanceStatus.PASSED,
        ProviderAcceptanceStatus.PASSED,
    ]


def test_missing_real_configuration_never_calls_executor_or_records_secret(tmp_path: Path) -> None:
    secret = "sk-this-secret-must-never-enter-the-report"
    calls: list[str] = []

    def execute(case, profile, repetition, environment):
        calls.append(profile.id)
        assert "DEEPSEEK_KEY" not in environment
        return _attempt(
            case,
            profile,
            repetition,
            ProviderAcceptanceStatus.PASSED,
            stdout=f"Authorization: Bearer {secret}",
        )

    report = ProviderAcceptanceRunner(
        tmp_path,
        environment={"UNRELATED_SECRET": secret},
        executor=execute,
    ).run(_manifest(), real=True)
    serialized = report.model_dump_json()

    assert calls == ["fixture-chat"] * 3 + ["fixture-responses"] * 3
    assert secret not in serialized
    assert "[REDACTED]" in serialized
    assert all(
        attempt.status is ProviderAcceptanceStatus.UNVERIFIED
        for attempt in report.attempts
        if attempt.profile_id in {"deepseek-chat", "openai-responses", "local-chat"}
    )
