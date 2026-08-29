from pathlib import Path

from patchloop.sandbox import DockerSandbox, DockerSandboxConfig


def test_docker_sandbox_command_is_networkless_and_resource_bounded(tmp_path: Path) -> None:
    sandbox = DockerSandbox(
        DockerSandboxConfig(
            image="patchloop-sandbox:py313",
            cpus=0.5,
            memory_mb=256,
            pids_limit=64,
        )
    )

    command = sandbox.build_command(["python", "-m", "pytest", "-q"], tmp_path)

    assert command[:3] == ["docker", "run", "--rm"]
    assert command[command.index("--network") + 1] == "none"
    assert command[command.index("--cpus") + 1] == "0.5"
    assert command[command.index("--memory") + 1] == "256m"
    assert command[command.index("--pids-limit") + 1] == "64"
    assert "--read-only" in command
    assert command[command.index("--cap-drop") + 1] == "ALL"
    assert command[command.index("--security-opt") + 1] == "no-new-privileges"
    mount = command[command.index("--mount") + 1]
    assert f"source={tmp_path.resolve()}" in mount
    assert "target=/workspace" in mount
    assert command[-4:] == ["python", "-m", "pytest", "-q"]


def test_network_can_only_be_enabled_explicitly(tmp_path: Path) -> None:
    sandbox = DockerSandbox(DockerSandboxConfig(network_enabled=True))

    command = sandbox.build_command(["git", "status", "--short"], tmp_path)

    assert command[command.index("--network") + 1] == "bridge"
