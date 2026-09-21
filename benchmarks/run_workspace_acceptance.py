"""Run local Git acceptance and publish evidence, without providers or network access."""

from __future__ import annotations

import argparse
import json
import platform
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = (
    "tests/unit/test_workspace_git.py",
    "tests/unit/test_workspace_ownership.py",
    "tests/integration/test_workspace_service.py",
    "tests/integration/test_workspace_cli.py",
    "tests/e2e/test_workspace_workflow.py",
    "tests/e2e/test_workspace_ownership.py",
)


def command_text(*args: str) -> str:
    result = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, check=True)
    return result.stdout.strip()


def cases_from_junit(path: Path) -> list[dict[str, str]]:
    cases: list[dict[str, str]] = []
    for case in ET.parse(path).iter("testcase"):
        failure = case.find("failure")
        if failure is None:
            failure = case.find("error")
        skipped = case.find("skipped")
        status = "failed" if failure is not None else "skipped" if skipped is not None else "passed"
        detail = failure if failure is not None else skipped
        cases.append(
            {
                "case": case.get("classname", "") + "::" + case.get("name", ""),
                "expected": "all assertions pass against a temporary local repository",
                "observed": "all assertions passed"
                if detail is None
                else detail.get("message", ""),
                "result": status,
                "seconds": case.get("time", "0"),
            }
        )
    return cases


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=ROOT / "benchmarks/results/workspace_git_acceptance.json"
    )
    parser.add_argument("--regression-junit", type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="patchloop-workspace-acceptance-") as temporary:
        junit = Path(temporary) / "results.xml"
        command = [sys.executable, "-m", "pytest", "-q", *TESTS, f"--junitxml={junit}"]
        completed = subprocess.run(command, cwd=ROOT, check=False)
        cases = (
            cases_from_junit(junit)
            if junit.exists()
            else [
                {
                    "case": "test_runner",
                    "expected": "test collection and execution completes",
                    "observed": f"pytest exit {completed.returncode}; no JUnit report",
                    "result": "failed",
                }
            ]
        )
    cases.append(
        {
            "case": "docker_sandbox",
            "expected": "separate Sandbox acceptance",
            "observed": "not exercised by local Workspace/Git acceptance",
            "result": "unverified",
        }
    )
    report: dict[str, object] = {
        "schema_version": "workspace-git.v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "git_version": command_text("git", "--version"),
        "os": platform.platform(),
        "python": platform.python_version(),
        "repository_revision": command_text("git", "rev-parse", "HEAD"),
        "repository_has_uncommitted_changes": bool(command_text("git", "status", "--porcelain")),
        "test_repositories": (
            "each case creates its own temporary Git baseline; no production repository mutations"
        ),
        "cases": cases,
        "summary": {
            status: sum(case["result"] == status for case in cases)
            for status in ("passed", "failed", "skipped", "unverified")
        },
    }
    if args.regression_junit is not None:
        regression = cases_from_junit(args.regression_junit)
        report["regression"] = {
            "source": str(args.regression_junit),
            "summary": {
                status: sum(case["result"] == status for case in regression)
                for status in ("passed", "failed", "skipped")
            },
            "nonpassing": [case for case in regression if case["result"] != "passed"],
        }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
