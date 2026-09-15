"""Run bounded real PPS pairs in fresh repositories, preserving all trial evidence.

This sends real model requests. Invoke explicitly after configuring the provider.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from patchloop.persistence import SQLiteStore
from patchloop.providers.config import _load_env_file


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--repeats", type=int, choices=(1, 3), default=3)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    work = args.work_root.resolve()
    work.mkdir(parents=True, exist_ok=False)
    config = root / "providers.toml"
    scenario = json.loads((root / "benchmarks/real_memory_scenarios.json").read_text())[
        "scenarios"
    ][0]
    env = {**_load_env_file(args.env_file), **os.environ}
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
    manifest: dict = {"runs": []}
    results: list[dict] = []

    def save() -> None:
        (work / "manifest.json").write_text(json.dumps(manifest, indent=2))
        (work / "results.json").write_text(json.dumps(results, indent=2))

    for case in ("contract-migration", "long-tool-output"):
        for repeat in range(1, args.repeats + 1):
            layouts = ("legacy", "append_only") if repeat % 2 else ("append_only", "legacy")
            for layout in layouts:
                trial = work / f"{case}-{repeat}-{layout}"
                repo = trial / "workspace"
                shutil.copytree(root / "benchmarks" / scenario["fixture"], repo)
                if case == "long-tool-output":
                    for evidence in sorted((repo / "evidence").glob("*.md"))[4:]:
                        with evidence.open("a") as handle:
                            handle.write("\nHistorical log noise; no contract changes.\n" * 500)
                (repo / ".gitignore").write_text(".patchloop/\n__pycache__/\n.pytest_cache/\n")
                for command in (
                    ["git", "init", "-q"],
                    ["git", "add", "."],
                    [
                        "git",
                        "-c",
                        "user.name=PPS Fixture",
                        "-c",
                        "user.email=pps@localhost",
                        "commit",
                        "-qm",
                        "fixture:固定验收初始状态",
                    ],
                ):
                    subprocess.run(command, cwd=repo, env=env, check=True)
                goal = scenario["goal"] + (
                    " Use separate tool rounds to read each evidence file in order. "
                    "Record a plan before editing and before running tests."
                )
                command = [
                    sys.executable,
                    "-m",
                    "patchloop",
                    "run",
                    goal,
                    "--repo",
                    str(repo),
                    "--provider-config",
                    str(config),
                    "--env-file",
                    str(args.env_file),
                    "--prompt-cache-layout",
                    layout,
                    "--allow-write",
                    "--allow-execute",
                    "--sandbox",
                    "local",
                    "--max-steps",
                    "40",
                    "--max-context-tokens",
                    "24000",
                    "--max-input-tokens",
                    "500000",
                    "--max-output-tokens",
                    "80000",
                    "--max-tool-output-chars",
                    "12000" if case == "long-tool-output" else "1800",
                    "--max-cost-usd",
                    "1",
                    "--json",
                ]
                print(f"START {trial.name}", flush=True)
                pause_requested = False
                with (
                    (trial / "run.stdout").open("w") as stdout,
                    (trial / "run.stderr").open("w") as stderr,
                ):
                    process = subprocess.Popen(
                        command, cwd=repo, env=env, stdout=stdout, stderr=stderr
                    )
                    deadline = time.monotonic() + 900
                    while process.poll() is None:
                        if time.monotonic() >= deadline:
                            process.terminate()
                            try:
                                process.wait(timeout=20)
                            except subprocess.TimeoutExpired:
                                process.kill()
                                process.wait()
                            break
                        traces = list((repo / ".patchloop/traces").glob("*.jsonl"))
                        if (
                            layout == "append_only"
                            and repeat == 1
                            and case == "contract-migration"
                            and not pause_requested
                            and traces
                        ):
                            text = traces[0].read_text()
                            if text.count('"type": "model.completed"') >= 2:
                                process.send_signal(signal.SIGINT)
                                pause_requested = True
                        time.sleep(1)
                traces = list((repo / ".patchloop/traces").glob("*.jsonl"))
                resume_code = None
                if pause_requested and traces:
                    task = SQLiteStore(repo / ".patchloop/patchloop.db").get_task(traces[0].stem)
                    assert task.session_id is not None
                    with (
                        (trial / "resume.stdout").open("w") as stdout,
                        (trial / "resume.stderr").open("w") as stderr,
                    ):
                        resumed = subprocess.run(
                            [
                                sys.executable,
                                "-m",
                                "patchloop",
                                "session",
                                "--repo",
                                str(repo),
                                "--json",
                                "resume",
                                task.session_id,
                            ],
                            cwd=repo,
                            env=env,
                            stdout=stdout,
                            stderr=stderr,
                            timeout=900,
                        )
                        resume_code = resumed.returncode
                # Act as the operator only for the explicitly scoped fixture change
                # its standard test command, and read-only status inspection.
                # Keep all normal approval records.
                approved_ids = []
                if traces:
                    store = SQLiteStore(repo / ".patchloop/patchloop.db")
                    for approval_round in range(4):
                        task = store.get_task(traces[0].stem)
                        pending = [
                            a for a in store.list_approvals(task.id) if a.status.value == "pending"
                        ]
                        if not pending:
                            break
                        permitted = True
                        for approval in pending:
                            effect = store.get_effect(approval.effect_id)
                            permitted = permitted and (
                                (
                                    effect.tool_name == "apply_patch"
                                    and approval.resource_summary
                                    == f"workspace={repo}; paths=order_service.py"
                                )
                                or (
                                    effect.tool_name == "run_command"
                                    and approval.resource_summary == f"workspace={repo}"
                                    and effect.arguments_summary.get("command")
                                    == ["git", "status", "--short"]
                                )
                                or (
                                    effect.tool_name == "run_tests"
                                    and approval.resource_summary == f"workspace={repo}"
                                    and effect.arguments_summary.get("command")
                                    in (
                                        ["python", "-m", "pytest"],
                                        ["python", "-m", "pytest", "-q"],
                                        ["pytest"],
                                        ["pytest", "-q"],
                                    )
                                )
                            )
                        if not permitted:
                            break
                        for approval in pending:
                            with (trial / f"approval-{approval.id}.json").open("w") as stdout:
                                subprocess.run(
                                    [
                                        sys.executable,
                                        "-m",
                                        "patchloop",
                                        "approval",
                                        "--repo",
                                        str(repo),
                                        "decide",
                                        approval.id,
                                        "--approve",
                                        "--source",
                                        "pps-fixture-operator",
                                    ],
                                    cwd=repo,
                                    env=env,
                                    stdout=stdout,
                                    check=True,
                                )
                            approved_ids.append(approval.id)
                        with (
                            (trial / f"approved-{approval_round}.stdout").open("w") as stdout,
                            (trial / f"approved-{approval_round}.stderr").open("w") as stderr,
                        ):
                            resumed = subprocess.run(
                                [
                                    sys.executable,
                                    "-m",
                                    "patchloop",
                                    "session",
                                    "--repo",
                                    str(repo),
                                    "--json",
                                    "resume",
                                    task.session_id,
                                ],
                                cwd=repo,
                                env=env,
                                stdout=stdout,
                                stderr=stderr,
                                timeout=900,
                            )
                        resume_code = resumed.returncode
                validation = {}
                for name, test in (
                    ("public", str(repo)),
                    ("hidden", str(root / "benchmarks" / scenario["hidden_test"])),
                ):
                    outcome = subprocess.run(
                        [sys.executable, "-m", "pytest", test, "-q"],
                        cwd=repo,
                        env={**env, "PATCHLOOP_REAL_MEMORY_REPO": str(repo)},
                        text=True,
                        capture_output=True,
                        timeout=120,
                    )
                    (trial / f"{name}.txt").write_text(outcome.stdout + outcome.stderr)
                    validation[name] = outcome.returncode
                changed = subprocess.check_output(
                    ["git", "diff", "--name-only"], cwd=repo, text=True
                ).splitlines()
                (trial / "changes.diff").write_text(
                    subprocess.check_output(["git", "diff"], cwd=repo, text=True)
                )
                result = {
                    "approved_ids": approved_ids,
                    "trial": trial.name,
                    "exit_code": process.returncode,
                    "resume_code": resume_code,
                    "pause_requested": pause_requested,
                    "validation": validation,
                    "changed_files": changed,
                }
                results.append(result)
                if traces:
                    manifest["runs"].append(
                        {
                            "trace": str(traces[0].relative_to(work)),
                            "task_id": traces[0].stem,
                            "variant": "current_layout" if layout == "legacy" else "append_only",
                            "repeat": repeat,
                            "batch_id": work.name,
                            "task_case": case,
                            "pair_id": f"{case}-{repeat}",
                        }
                    )
                save()
                print("DONE " + json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
