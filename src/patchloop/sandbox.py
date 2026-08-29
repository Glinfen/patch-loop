"""Command execution backends with a fail-closed Docker sandbox."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field


class SandboxError(RuntimeError):
    pass


class SandboxTimeoutError(TimeoutError):
    pass


class SandboxResult(BaseModel):
    exit_code: int
    output: str
    backend: str


class CommandSandbox(Protocol):
    name: str

    def execute(
        self,
        command: list[str],
        repository: Path,
        *,
        timeout_seconds: float,
        max_output_chars: int,
    ) -> SandboxResult: ...


def _minimal_environment() -> dict[str, str]:
    environment = {
        key: value
        for key in ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP")
        if (value := os.environ.get(key)) is not None
    }
    environment.update({"PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"})
    return environment


class LocalProcessSandbox:
    """Compatibility backend restricted by the command policy but not OS-isolated."""

    name = "local"

    def execute(
        self,
        command: list[str],
        repository: Path,
        *,
        timeout_seconds: float,
        max_output_chars: int,
    ) -> SandboxResult:
        try:
            completed = subprocess.run(
                command,
                cwd=repository,
                env=_minimal_environment(),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
                check=False,
                shell=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise SandboxTimeoutError(
                f"local command timed out after {timeout_seconds:g} seconds"
            ) from exc
        return SandboxResult(
            exit_code=completed.returncode,
            output=(completed.stdout + completed.stderr)[-max_output_chars:],
            backend=self.name,
        )


class DockerSandboxConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    image: str = Field(default="patchloop-sandbox:py313", pattern=r"^[A-Za-z0-9._/:@-]+$")
    cpus: float = Field(default=1.0, gt=0, le=8)
    memory_mb: int = Field(default=512, ge=64, le=16_384)
    pids_limit: int = Field(default=128, ge=16, le=4_096)
    network_enabled: bool = False


class DockerSandbox:
    name = "docker"

    def __init__(self, config: DockerSandboxConfig | None = None) -> None:
        self.config = config or DockerSandboxConfig()

    def build_command(self, command: list[str], repository: Path) -> list[str]:
        repository = repository.resolve(strict=True)
        network = "bridge" if self.config.network_enabled else "none"
        container_command = list(command)
        executable = Path(container_command[0]).name.casefold()
        if executable in {"python", "python.exe", "python3", "python3.exe", "py", "py.exe"}:
            container_command[0] = "python"
        return [
            "docker",
            "run",
            "--rm",
            "--network",
            network,
            "--cpus",
            f"{self.config.cpus:g}",
            "--memory",
            f"{self.config.memory_mb}m",
            "--pids-limit",
            str(self.config.pids_limit),
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=64m",
            "--mount",
            f"type=bind,source={repository},target=/workspace",
            "--workdir",
            "/workspace",
            self.config.image,
            *container_command,
        ]

    def execute(
        self,
        command: list[str],
        repository: Path,
        *,
        timeout_seconds: float,
        max_output_chars: int,
    ) -> SandboxResult:
        docker_command = self.build_command(command, repository)
        try:
            completed = subprocess.run(
                docker_command,
                env=_minimal_environment(),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
                check=False,
                shell=False,
            )
        except FileNotFoundError as exc:
            raise SandboxError("Docker is required but was not found; execution denied") from exc
        except subprocess.TimeoutExpired as exc:
            raise SandboxTimeoutError(
                f"Docker command timed out after {timeout_seconds:g} seconds"
            ) from exc
        output = (completed.stdout + completed.stderr)[-max_output_chars:]
        if completed.returncode in {125, 126, 127}:
            raise SandboxError(f"Docker sandbox failed to start: {output.strip()}")
        return SandboxResult(
            exit_code=completed.returncode,
            output=output,
            backend=self.name,
        )
