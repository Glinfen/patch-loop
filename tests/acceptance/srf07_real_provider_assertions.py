"""Runtime-invisible assertions for the SRF-07 locked real-repository issue."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 3:
        raise SystemExit("usage: srf07_real_provider_assertions.py WORKSPACE PYTHON")
    workspace = Path(sys.argv[1]).resolve(strict=True)
    python = Path(sys.argv[2]).resolve(strict=True)
    cli_path = workspace / "src" / "patchloop" / "cli.py"
    tree = ast.parse(cli_path.read_text(encoding="utf-8"))

    exposed_optional = {"client_submission_id", "effect_id", "approved"}
    annotations: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for argument in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]:
            if argument.arg in exposed_optional and argument.annotation is not None:
                annotations[argument.arg] = ast.unparse(argument.annotation)
    if set(annotations) != exposed_optional:
        raise AssertionError(
            f"missing CLI annotations: {sorted(exposed_optional - set(annotations))}"
        )
    pep604 = {
        name: annotation for name, annotation in annotations.items() if "| None" in annotation
    }
    if pep604:
        raise AssertionError(f"Typer-exposed PEP 604 optional annotations remain: {pep604}")

    for arguments in (["session", "--help"], ["approval", "decide", "--help"]):
        completed = subprocess.run(
            [str(python), "-m", "patchloop", *arguments],
            cwd=workspace,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        if completed.returncode != 0:
            raise AssertionError(
                f"{' '.join(arguments)} failed ({completed.returncode}): "
                f"{completed.stdout}\n{completed.stderr}"
            )

    changed = subprocess.run(
        ["git", "diff", "--name-only", "--"],
        cwd=workspace,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    ).stdout.splitlines()
    if not changed:
        raise AssertionError("the trial produced no patch")
    allowed = {
        "src/patchloop/cli.py",
        "tests/integration/test_cli.py",
        "tests/integration/test_session_cli.py",
    }
    unexpected = set(changed) - allowed
    if unexpected:
        raise AssertionError(f"trial changed paths outside the issue scope: {sorted(unexpected)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
