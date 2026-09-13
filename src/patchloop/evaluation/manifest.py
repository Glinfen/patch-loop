"""Evaluation manifest loading and deterministic repository fingerprints."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from patchloop.evaluation.models import EvaluationManifest

IGNORED_PARTS = frozenset({".git", ".patchloop", ".pytest_cache", "__pycache__"})


def repository_tree_sha256(repository: Path) -> str:
    repository = repository.resolve(strict=True)
    digest = hashlib.sha256()
    candidates = (
        path
        for path in repository.rglob("*")
        if path.is_file()
        and not path.is_symlink()
        and not any(part in IGNORED_PARTS for part in path.relative_to(repository).parts)
    )
    files = sorted(
        candidates,
        key=lambda path: path.relative_to(repository).as_posix().casefold(),
    )
    for path in files:
        relative = path.relative_to(repository).as_posix().encode("utf-8")
        content = path.read_bytes()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def load_evaluation_manifest(path: Path) -> EvaluationManifest:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return EvaluationManifest.model_validate(payload)
