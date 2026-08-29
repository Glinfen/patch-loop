from pathlib import Path

import pytest

from patchloop.evaluation import (
    ExperimentConfig,
    ExperimentFeatures,
    ExperimentRunner,
    load_evaluation_manifest,
)
from patchloop.evaluation.experiments import AblationExecutor


def test_planning_ablation_changes_plan_without_changing_candidate_paths() -> None:
    root = Path(__file__).parents[2]
    manifest = load_evaluation_manifest(root / "benchmarks" / "evaluation_manifest.json")
    task = manifest.tasks[0]
    repository = root / manifest.repositories[task.repository].path
    full = AblationExecutor(
        ExperimentConfig(name="full-test", role="test", features=ExperimentFeatures())
    )
    no_planning = AblationExecutor(
        ExperimentConfig(
            name="no-plan-test",
            role="test",
            features=ExperimentFeatures(planning=False),
        )
    )

    full_candidate = full.execute(task, repository)
    ablated_candidate = no_planning.execute(task, repository)

    assert full_candidate.selected_paths == ablated_candidate.selected_paths
    assert len(full_candidate.plan_steps) == 2
    assert ablated_candidate.plan_steps == []


def test_experiment_runner_rejects_zero_repeats(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="repeats must be positive"):
        ExperimentRunner(tmp_path, repeats=0)


def test_experiment_runner_requires_comparison_stages() -> None:
    root = Path(__file__).parents[2]
    manifest = load_evaluation_manifest(root / "benchmarks" / "evaluation_manifest.json")

    with pytest.raises(ValueError, match="missing required experiment configs"):
        ExperimentRunner(root, repeats=1).run(manifest, [])
