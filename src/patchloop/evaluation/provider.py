"""PGW-11 cross-protocol and real-provider acceptance orchestration."""

from __future__ import annotations

import argparse
import math
import os
import platform
import re
import socket
import subprocess
import sys
from collections.abc import Callable, Mapping
from enum import StrEnum
from pathlib import Path
from time import perf_counter
from typing import Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from patchloop.domain import utc_now
from patchloop.providers.contracts import ProviderProtocol
from patchloop.security import SecretRedactor


class ProviderAcceptanceStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    UNVERIFIED = "unverified"


class ProviderAcceptanceProfileKind(StrEnum):
    FIXTURE = "fixture"
    DEEPSEEK = "deepseek"
    OPENAI_RESPONSES = "openai_responses"
    LOCAL = "local"


class ProviderAcceptanceScenario(StrEnum):
    TEXT = "text"
    MULTIPLE_TOOLS = "multiple_tools"
    BAD_ARGUMENTS = "bad_arguments"
    PERMISSION_DENIAL = "permission_denial"
    APPROVAL_RESTART = "approval_restart"
    CONFIRMED_RESULT_REPLAY = "confirmed_result_replay"
    NEW_CONSTRAINT = "new_constraint"
    TRUNCATION = "truncation"
    DISCONNECT = "disconnect"
    CANCELLATION = "cancellation"
    LEASE_LOST = "lease_lost"
    COMPRESSION = "compression"
    REASONING_ROUND_TRIP = "reasoning_round_trip"
    UNKNOWN_USAGE = "unknown_usage"


class ProviderExpectedOutcome(StrEnum):
    COMPLETED = "completed"
    CORRECTLY_REJECTED = "correctly_rejected"
    CORRECTLY_PAUSED = "correctly_paused"


class ProviderAcceptanceProfile(BaseModel):
    """A secret-free profile lock; sensitive values are referenced only by env name."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]+$")
    kind: ProviderAcceptanceProfileKind
    protocol: ProviderProtocol
    implementation: str
    implementation_version: str | None = None
    implementation_version_environment: str | None = None
    endpoint: str | None = None
    endpoint_environment: str | None = None
    model: str | None = None
    model_environment: str | None = None
    pricing_version: str | None = None
    pricing_version_environment: str | None = None
    input_price_per_million: float | None = Field(default=None, ge=0.0)
    input_price_environment: str | None = None
    output_price_per_million: float | None = Field(default=None, ge=0.0)
    output_price_environment: str | None = None
    credential_environment: list[str] = Field(default_factory=list)
    generation: dict[str, int | float | bool | str] = Field(default_factory=dict)
    tools_capability: bool = True

    @model_validator(mode="after")
    def configuration_is_explicit(self) -> Self:
        for label, literal, environment in (
            ("endpoint", self.endpoint, self.endpoint_environment),
            ("model", self.model, self.model_environment),
            ("pricing_version", self.pricing_version, self.pricing_version_environment),
            (
                "input_price_per_million",
                self.input_price_per_million,
                self.input_price_environment,
            ),
            (
                "output_price_per_million",
                self.output_price_per_million,
                self.output_price_environment,
            ),
        ):
            if (literal is None) == (environment is None):
                raise ValueError(f"profile {label} must have exactly one literal or env reference")
        if self.kind is ProviderAcceptanceProfileKind.FIXTURE:
            references = (
                self.endpoint_environment,
                self.model_environment,
                self.pricing_version_environment,
                self.input_price_environment,
                self.output_price_environment,
            )
            if any(references) or self.credential_environment:
                raise ValueError("fixture profiles must be fully literal and credential-free")
        elif self.kind is not ProviderAcceptanceProfileKind.LOCAL:
            if not self.credential_environment:
                raise ValueError("remote real profiles must reference a credential environment")
        return self

    def required_environment(self) -> list[str]:
        values = [
            self.implementation_version_environment,
            self.endpoint_environment,
            self.model_environment,
            self.pricing_version_environment,
            self.input_price_environment,
            self.output_price_environment,
            *self.credential_environment,
        ]
        return [value for value in values if value is not None]


class ProviderAcceptanceCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]+$")
    profile_id: str
    scenarios: list[ProviderAcceptanceScenario] = Field(min_length=1)
    expected_outcomes: list[ProviderExpectedOutcome] = Field(min_length=1)
    test_nodeids: list[str] = Field(min_length=1)
    repetitions: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def test_target_and_repetitions_are_safe(self) -> Self:
        if any(
            not nodeid.startswith("tests/") or any(character.isspace() for character in nodeid)
            for nodeid in self.test_nodeids
        ):
            raise ValueError("acceptance test_nodeids must be whitespace-free tests/ targets")
        repeated = {
            ProviderAcceptanceScenario.TRUNCATION,
            ProviderAcceptanceScenario.DISCONNECT,
            ProviderAcceptanceScenario.CANCELLATION,
            ProviderAcceptanceScenario.LEASE_LOST,
        }
        if repeated.intersection(self.scenarios) and self.repetitions < 3:
            raise ValueError("recovery and cancellation cases require at least three repetitions")
        return self


class ProviderAcceptanceManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = 1
    profiles: list[ProviderAcceptanceProfile] = Field(min_length=5)
    cases: list[ProviderAcceptanceCase] = Field(min_length=1)

    @model_validator(mode="after")
    def coverage_is_complete(self) -> Self:
        profile_ids = [profile.id for profile in self.profiles]
        case_ids = [case.id for case in self.cases]
        if len(profile_ids) != len(set(profile_ids)):
            raise ValueError("provider acceptance profile ids must be unique")
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("provider acceptance case ids must be unique")
        if any(len(case.test_nodeids) != len(set(case.test_nodeids)) for case in self.cases):
            raise ValueError("provider acceptance test nodeids must be unique within a case")
        known = set(profile_ids)
        if unknown := sorted({case.profile_id for case in self.cases} - known):
            raise ValueError(f"acceptance cases reference unknown profiles: {unknown}")
        profiles = {profile.id: profile for profile in self.profiles}
        if missing_profiles := sorted(known - {case.profile_id for case in self.cases}):
            raise ValueError(f"acceptance profiles have no cases: {missing_profiles}")
        fixture_protocols = {
            profile.protocol
            for profile in self.profiles
            if profile.kind is ProviderAcceptanceProfileKind.FIXTURE
        }
        if fixture_protocols != {ProviderProtocol.CHAT_COMPLETIONS, ProviderProtocol.RESPONSES}:
            raise ValueError("fixture acceptance must cover Chat Completions and Responses")
        real_kinds = {
            profile.kind
            for profile in self.profiles
            if profile.kind is not ProviderAcceptanceProfileKind.FIXTURE
        }
        if real_kinds != {
            ProviderAcceptanceProfileKind.DEEPSEEK,
            ProviderAcceptanceProfileKind.OPENAI_RESPONSES,
            ProviderAcceptanceProfileKind.LOCAL,
        }:
            raise ValueError("real acceptance must define DeepSeek, OpenAI Responses, and local")
        for case in self.cases:
            if (
                profiles[case.profile_id].kind is not ProviderAcceptanceProfileKind.FIXTURE
                and case.repetitions < 3
            ):
                raise ValueError("every real-provider case requires at least three repetitions")
        required_scenarios = set(ProviderAcceptanceScenario)
        for protocol in ProviderProtocol:
            covered = {
                scenario
                for case in self.cases
                if profiles[case.profile_id].kind is ProviderAcceptanceProfileKind.FIXTURE
                and profiles[case.profile_id].protocol is protocol
                for scenario in case.scenarios
            }
            if covered != required_scenarios:
                missing = sorted(item.value for item in required_scenarios - covered)
                raise ValueError(f"{protocol.value} fixture scenarios missing: {missing}")
        return self


class ProviderProfileProbe(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile_id: str
    kind: ProviderAcceptanceProfileKind
    protocol: ProviderProtocol
    available: bool
    detail: str
    required_environment: list[str]
    configured_environment: list[str]
    implementation: str
    implementation_version: str | None = None
    endpoint: str | None = None
    model: str | None = None
    pricing_version: str | None = None
    input_price_per_million: float | None = None
    output_price_per_million: float | None = None
    generation: dict[str, int | float | bool | str]
    tools_capability: bool


class ProviderAcceptanceAttempt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str
    profile_id: str
    protocol: ProviderProtocol
    scenarios: list[ProviderAcceptanceScenario]
    expected_outcomes: list[ProviderExpectedOutcome]
    repetition: int = Field(ge=1)
    status: ProviderAcceptanceStatus
    duration_ms: float = Field(ge=0.0)
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    detail: str
    request_count_checked: bool = False
    sqlite_checked: bool = False
    trace_checked: bool = False
    tool_audit_checked: bool = False
    unauthorized_actions: int | None = Field(default=None, ge=0)
    duplicate_confirmed_effects: int | None = Field(default=None, ge=0)
    partial_stream_effects: int | None = Field(default=None, ge=0)
    secret_leaks: int | None = Field(default=None, ge=0)


class ProviderProfileSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile_id: str
    kind: ProviderAcceptanceProfileKind
    protocol: ProviderProtocol
    status: ProviderAcceptanceStatus
    passed: int = Field(ge=0)
    failed: int = Field(ge=0)
    unverified: int = Field(ge=0)


class ProviderAcceptanceReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    manifest_schema_version: int
    status: ProviderAcceptanceStatus
    offline_status: ProviderAcceptanceStatus
    real_status: ProviderAcceptanceStatus
    real_requested: bool
    platform: str
    python_version: str
    revision: str | None
    source_dirty: bool | None
    probes: list[ProviderProfileProbe]
    attempts: list[ProviderAcceptanceAttempt]
    profiles: list[ProviderProfileSummary]
    unauthorized_actions: int = Field(ge=0)
    duplicate_confirmed_effects: int = Field(ge=0)
    partial_stream_effects: int = Field(ge=0)
    secret_leaks: int = Field(ge=0)
    security_evidence_unverified: int = Field(ge=0)
    generated_at: str = Field(default_factory=lambda: utc_now().isoformat())

    @model_validator(mode="after")
    def aggregate_is_consistent(self) -> Self:
        kinds = {probe.profile_id: probe.kind for probe in self.probes}
        offline = [
            item.status
            for item in self.attempts
            if kinds.get(item.profile_id) is ProviderAcceptanceProfileKind.FIXTURE
        ]
        real = [
            item.status
            for item in self.attempts
            if kinds.get(item.profile_id) is not ProviderAcceptanceProfileKind.FIXTURE
        ]
        if self.offline_status is not _aggregate(offline):
            raise ValueError("offline Provider acceptance status is inconsistent")
        if self.real_status is not _aggregate(real):
            raise ValueError("real Provider acceptance status is inconsistent")
        if self.status is not _aggregate([self.offline_status, self.real_status]):
            raise ValueError("Provider acceptance aggregate status is inconsistent")
        fields = (
            "unauthorized_actions",
            "duplicate_confirmed_effects",
            "partial_stream_effects",
            "secret_leaks",
        )
        for field in fields:
            if getattr(self, field) != sum(getattr(item, field) or 0 for item in self.attempts):
                raise ValueError(f"Provider acceptance {field} total is inconsistent")
        expected_unverified = sum(
            any(getattr(item, field) is None for field in fields) for item in self.attempts
        )
        if self.security_evidence_unverified != expected_unverified:
            raise ValueError("Provider acceptance security evidence total is inconsistent")
        return self


type AcceptanceExecutor = Callable[
    [ProviderAcceptanceCase, ProviderAcceptanceProfile, int, Mapping[str, str]],
    ProviderAcceptanceAttempt,
]


class ProviderAcceptanceRunner:
    """Run every configured sample and retain failures instead of selecting a best run."""

    def __init__(
        self,
        root: Path | None = None,
        *,
        environment: Mapping[str, str] | None = None,
        timeout_seconds: float = 300.0,
        executor: AcceptanceExecutor | None = None,
    ) -> None:
        self.root = (root or Path.cwd()).resolve(strict=True)
        self.environment = dict(os.environ if environment is None else environment)
        self.timeout_seconds = timeout_seconds
        self.executor = executor
        self._secrets: list[str] = []
        self._credential_names: set[str] = set()

    def run(
        self,
        manifest: ProviderAcceptanceManifest,
        *,
        real: bool = False,
    ) -> ProviderAcceptanceReport:
        profiles = {profile.id: profile for profile in manifest.profiles}
        self._credential_names = {
            name for profile in manifest.profiles for name in profile.credential_environment
        }
        self._secrets = [
            value
            for profile in manifest.profiles
            for name in profile.credential_environment
            if (value := self.environment.get(name, "").strip())
        ]
        probes = {profile.id: self._probe(profile, real=real) for profile in manifest.profiles}
        attempts: list[ProviderAcceptanceAttempt] = []
        for case in manifest.cases:
            profile = profiles[case.profile_id]
            probe = probes[profile.id]
            for repetition in range(1, case.repetitions + 1):
                if not probe.available:
                    attempts.append(self._unverified(case, profile, repetition, probe.detail))
                    continue
                executor = self.executor or self._execute_pytest
                try:
                    attempt = executor(
                        case,
                        profile,
                        repetition,
                        self._case_environment(profile),
                    )
                except Exception as exc:
                    attempt = self._failed_executor_attempt(
                        case, profile, repetition, f"executor failed: {type(exc).__name__}: {exc}"
                    )
                if not self._attempt_matches(attempt, case, profile, repetition):
                    attempt = self._failed_executor_attempt(
                        case,
                        profile,
                        repetition,
                        "executor returned evidence for a different case, profile, or repetition",
                    )
                attempts.append(self._redact_attempt(attempt))

        summaries = [self._summarize(profile, attempts) for profile in manifest.profiles]
        fixture_ids = {
            profile.id
            for profile in manifest.profiles
            if profile.kind is ProviderAcceptanceProfileKind.FIXTURE
        }
        offline_status = _aggregate(
            [attempt.status for attempt in attempts if attempt.profile_id in fixture_ids]
        )
        real_status = _aggregate(
            [attempt.status for attempt in attempts if attempt.profile_id not in fixture_ids]
        )
        status = _aggregate([offline_status, real_status])
        security_fields = (
            "unauthorized_actions",
            "duplicate_confirmed_effects",
            "partial_stream_effects",
            "secret_leaks",
        )
        security_unverified = sum(
            any(getattr(attempt, field) is None for field in security_fields)
            for attempt in attempts
        )
        revision, source_dirty = self._revision()
        return ProviderAcceptanceReport(
            manifest_schema_version=manifest.schema_version,
            status=status,
            offline_status=offline_status,
            real_status=real_status,
            real_requested=real,
            platform=platform.platform(),
            python_version=platform.python_version(),
            revision=revision,
            source_dirty=source_dirty,
            probes=list(probes.values()),
            attempts=attempts,
            profiles=summaries,
            unauthorized_actions=sum(attempt.unauthorized_actions or 0 for attempt in attempts),
            duplicate_confirmed_effects=sum(
                attempt.duplicate_confirmed_effects or 0 for attempt in attempts
            ),
            partial_stream_effects=sum(attempt.partial_stream_effects or 0 for attempt in attempts),
            secret_leaks=sum(attempt.secret_leaks or 0 for attempt in attempts),
            security_evidence_unverified=security_unverified,
        )

    def _probe(self, profile: ProviderAcceptanceProfile, *, real: bool) -> ProviderProfileProbe:
        required = profile.required_environment()
        configured = [name for name in required if self.environment.get(name, "").strip()]
        available = True
        detail = "deterministic loopback fixture is available"
        if profile.kind is not ProviderAcceptanceProfileKind.FIXTURE:
            if not real:
                available = False
                detail = "real acceptance was not requested; environment remains unverified"
            elif missing := sorted(set(required) - set(configured)):
                available = False
                detail = f"required environment is not configured: {', '.join(missing)}"
            elif profile.kind is ProviderAcceptanceProfileKind.LOCAL:
                endpoint = self._text_setting(profile.endpoint, profile.endpoint_environment)
                available, detail = self._probe_local_service(endpoint)
            else:
                detail = "explicit real-provider configuration is available"
            prices = (
                self._float_setting(
                    profile.input_price_per_million, profile.input_price_environment
                ),
                self._float_setting(
                    profile.output_price_per_million, profile.output_price_environment
                ),
            )
            if available and any(price is None or price < 0 for price in prices):
                available = False
                detail = "Provider price environment must contain non-negative numbers"
        return ProviderProfileProbe(
            profile_id=profile.id,
            kind=profile.kind,
            protocol=profile.protocol,
            available=available,
            detail=detail,
            required_environment=required,
            configured_environment=configured,
            implementation=profile.implementation,
            implementation_version=self._redact_text(
                self._text_setting(
                    profile.implementation_version,
                    profile.implementation_version_environment,
                )
            ),
            endpoint=self._redact_text(
                self._text_setting(profile.endpoint, profile.endpoint_environment)
            ),
            model=self._redact_text(self._text_setting(profile.model, profile.model_environment)),
            pricing_version=self._redact_text(
                self._text_setting(profile.pricing_version, profile.pricing_version_environment)
            ),
            input_price_per_million=self._float_setting(
                profile.input_price_per_million, profile.input_price_environment
            ),
            output_price_per_million=self._float_setting(
                profile.output_price_per_million, profile.output_price_environment
            ),
            generation=profile.generation,
            tools_capability=profile.tools_capability,
        )

    def _execute_pytest(
        self,
        case: ProviderAcceptanceCase,
        profile: ProviderAcceptanceProfile,
        repetition: int,
        environment: Mapping[str, str],
    ) -> ProviderAcceptanceAttempt:
        started = perf_counter()
        try:
            completed = subprocess.run(
                [sys.executable, "-m", "pytest", "-q", "-ra", *case.test_nodeids],
                cwd=self.root,
                env=dict(environment),
                capture_output=True,
                text=True,
                check=False,
                timeout=self.timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return self._failed_executor_attempt(
                case,
                profile,
                repetition,
                f"acceptance test failed to run: {type(exc).__name__}: {exc}",
                duration_ms=(perf_counter() - started) * 1_000,
            )
        combined = f"{completed.stdout}\n{completed.stderr}"
        only_skipped = bool(re.search(r"\b\d+ skipped\b", combined)) and not bool(
            re.search(r"\b\d+ passed\b", combined)
        )
        status = (
            ProviderAcceptanceStatus.PASSED
            if completed.returncode == 0 and not only_skipped
            else ProviderAcceptanceStatus.UNVERIFIED
            if completed.returncode == 0
            else ProviderAcceptanceStatus.FAILED
        )
        checked = status is ProviderAcceptanceStatus.PASSED
        return ProviderAcceptanceAttempt(
            case_id=case.id,
            profile_id=profile.id,
            protocol=profile.protocol,
            scenarios=case.scenarios,
            expected_outcomes=case.expected_outcomes,
            repetition=repetition,
            status=status,
            duration_ms=(perf_counter() - started) * 1_000,
            exit_code=completed.returncode,
            stdout=completed.stdout[-4_000:],
            stderr=completed.stderr[-4_000:],
            detail=(
                "acceptance assertions passed"
                if checked
                else "acceptance target was skipped"
                if only_skipped
                else f"pytest exited with code {completed.returncode}"
            ),
            request_count_checked=checked,
            sqlite_checked=checked,
            trace_checked=checked,
            tool_audit_checked=checked,
            unauthorized_actions=0 if checked else None,
            duplicate_confirmed_effects=0 if checked else None,
            partial_stream_effects=0 if checked else None,
            secret_leaks=0 if checked else None,
        )

    def _case_environment(self, profile: ProviderAcceptanceProfile) -> dict[str, str]:
        environment = self.environment.copy()
        credential_names = set(profile.credential_environment)
        for name in self._credential_names - credential_names:
            environment.pop(name, None)
        environment.update(
            {
                "PATCHLOOP_ACCEPTANCE_ACTIVE": "1",
                "PATCHLOOP_ACCEPTANCE_PROFILE": profile.id,
                "PATCHLOOP_ACCEPTANCE_KIND": profile.kind.value,
                "PATCHLOOP_ACCEPTANCE_PROTOCOL": profile.protocol.value,
                "PATCHLOOP_ACCEPTANCE_ENDPOINT": self._text_setting(
                    profile.endpoint, profile.endpoint_environment
                )
                or "",
                "PATCHLOOP_ACCEPTANCE_MODEL": self._text_setting(
                    profile.model, profile.model_environment
                )
                or "",
                "PATCHLOOP_ACCEPTANCE_PRICING_VERSION": self._text_setting(
                    profile.pricing_version, profile.pricing_version_environment
                )
                or "",
                "PATCHLOOP_ACCEPTANCE_INPUT_PRICE": str(
                    self._float_setting(
                        profile.input_price_per_million, profile.input_price_environment
                    )
                    or 0
                ),
                "PATCHLOOP_ACCEPTANCE_OUTPUT_PRICE": str(
                    self._float_setting(
                        profile.output_price_per_million, profile.output_price_environment
                    )
                    or 0
                ),
                "PATCHLOOP_ACCEPTANCE_API_KEY": (
                    self.environment.get(profile.credential_environment[0], "")
                    if profile.credential_environment
                    else ""
                ),
            }
        )
        source = str(self.root / "src")
        environment["PYTHONPATH"] = os.pathsep.join(
            item for item in (source, environment.get("PYTHONPATH", "")) if item
        )
        return environment

    def _redact_attempt(self, attempt: ProviderAcceptanceAttempt) -> ProviderAcceptanceAttempt:
        payload = attempt.model_dump(mode="json")
        redactor = SecretRedactor()
        for field in ("stdout", "stderr", "detail"):
            text = str(payload[field])
            for secret in sorted(self._secrets, key=len, reverse=True):
                text = text.replace(secret, SecretRedactor.replacement)
            payload[field] = redactor.redact_text(text)
        return ProviderAcceptanceAttempt.model_validate(payload)

    def _redact_text(self, value: str | None) -> str | None:
        if value is None:
            return None
        for secret in sorted(self._secrets, key=len, reverse=True):
            value = value.replace(secret, SecretRedactor.replacement)
        return SecretRedactor().redact_text(value)

    @staticmethod
    def _attempt_matches(
        attempt: ProviderAcceptanceAttempt,
        case: ProviderAcceptanceCase,
        profile: ProviderAcceptanceProfile,
        repetition: int,
    ) -> bool:
        return (
            attempt.case_id == case.id
            and attempt.profile_id == profile.id
            and attempt.protocol is profile.protocol
            and attempt.repetition == repetition
            and attempt.scenarios == case.scenarios
            and attempt.expected_outcomes == case.expected_outcomes
        )

    @staticmethod
    def _unverified(
        case: ProviderAcceptanceCase,
        profile: ProviderAcceptanceProfile,
        repetition: int,
        detail: str,
    ) -> ProviderAcceptanceAttempt:
        return ProviderAcceptanceAttempt(
            case_id=case.id,
            profile_id=profile.id,
            protocol=profile.protocol,
            scenarios=case.scenarios,
            expected_outcomes=case.expected_outcomes,
            repetition=repetition,
            status=ProviderAcceptanceStatus.UNVERIFIED,
            duration_ms=0.0,
            detail=detail,
        )

    @staticmethod
    def _failed_executor_attempt(
        case: ProviderAcceptanceCase,
        profile: ProviderAcceptanceProfile,
        repetition: int,
        detail: str,
        *,
        duration_ms: float = 0.0,
    ) -> ProviderAcceptanceAttempt:
        return ProviderAcceptanceAttempt(
            case_id=case.id,
            profile_id=profile.id,
            protocol=profile.protocol,
            scenarios=case.scenarios,
            expected_outcomes=case.expected_outcomes,
            repetition=repetition,
            status=ProviderAcceptanceStatus.FAILED,
            duration_ms=duration_ms,
            detail=detail,
        )

    @staticmethod
    def _summarize(
        profile: ProviderAcceptanceProfile,
        attempts: list[ProviderAcceptanceAttempt],
    ) -> ProviderProfileSummary:
        statuses = [item.status for item in attempts if item.profile_id == profile.id]
        return ProviderProfileSummary(
            profile_id=profile.id,
            kind=profile.kind,
            protocol=profile.protocol,
            status=_aggregate(statuses),
            passed=statuses.count(ProviderAcceptanceStatus.PASSED),
            failed=statuses.count(ProviderAcceptanceStatus.FAILED),
            unverified=statuses.count(ProviderAcceptanceStatus.UNVERIFIED),
        )

    def _text_setting(self, literal: str | None, environment: str | None) -> str | None:
        if literal is not None:
            return literal
        if environment is None:
            return None
        return self.environment.get(environment) or None

    def _float_setting(self, literal: float | None, environment: str | None) -> float | None:
        if literal is not None:
            return literal
        if environment is None or not (value := self.environment.get(environment, "").strip()):
            return None
        try:
            parsed = float(value)
        except ValueError:
            return None
        return parsed if math.isfinite(parsed) and parsed >= 0 else None

    @staticmethod
    def _probe_local_service(endpoint: str | None) -> tuple[bool, str]:
        if endpoint is None:
            return False, "local endpoint is not configured"
        parsed = urlsplit(endpoint)
        if parsed.scheme not in {"http", "https"}:
            return False, "local acceptance endpoint must use http or https"
        if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            return False, "local acceptance endpoint must resolve to an explicit loopback host"
        try:
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            with socket.create_connection((parsed.hostname, port), timeout=2):
                pass
        except (OSError, ValueError) as exc:
            return False, f"local model service is unavailable: {type(exc).__name__}"
        return True, "local model service accepted a loopback connection"

    def _revision(self) -> tuple[str | None, bool | None]:
        try:
            revision = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=self.root,
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
            dirty = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=self.root,
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None, None
        if revision.returncode != 0 or dirty.returncode != 0:
            return None, None
        return revision.stdout.strip() or None, bool(dirty.stdout.strip())


def _aggregate(statuses: list[ProviderAcceptanceStatus]) -> ProviderAcceptanceStatus:
    if (
        not statuses or ProviderAcceptanceStatus.UNVERIFIED in statuses
    ) and ProviderAcceptanceStatus.FAILED not in statuses:
        return ProviderAcceptanceStatus.UNVERIFIED
    if ProviderAcceptanceStatus.FAILED in statuses:
        return ProviderAcceptanceStatus.FAILED
    return ProviderAcceptanceStatus.PASSED


def load_provider_acceptance_manifest(path: Path) -> ProviderAcceptanceManifest:
    return ProviderAcceptanceManifest.model_validate_json(path.read_text(encoding="utf-8"))


def write_provider_acceptance_report(report: ProviderAcceptanceReport, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run PGW-11 Provider acceptance")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--real", action="store_true")
    arguments = parser.parse_args()
    manifest = load_provider_acceptance_manifest(arguments.manifest)
    report = ProviderAcceptanceRunner(arguments.root, timeout_seconds=arguments.timeout).run(
        manifest, real=arguments.real
    )
    write_provider_acceptance_report(report, arguments.output)
    print(report.model_dump_json(indent=2))
    selected_status = report.status if arguments.real else report.offline_status
    if selected_status is ProviderAcceptanceStatus.PASSED:
        return 0
    if selected_status is ProviderAcceptanceStatus.FAILED:
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
