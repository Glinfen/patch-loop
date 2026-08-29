import json
from pathlib import Path

from typer.testing import CliRunner

from patchloop.cli import app


def test_one_command_runs_fixed_evaluation_suite(tmp_path: Path) -> None:
    root = Path(__file__).parents[2]
    output = tmp_path / "evaluation.json"

    result = CliRunner().invoke(
        app,
        [
            "benchmark",
            "--manifest",
            str(root / "benchmarks" / "evaluation_manifest.json"),
            "--root",
            str(root),
            "--output",
            str(output),
            "--variant",
            "all",
            "--jobs",
            "4",
            "--retries",
            "1",
        ],
    )

    assert result.exit_code == 0, result.output
    report = json.loads(output.read_text(encoding="utf-8"))
    summary = {
        item["variant"]: {
            "passed": item["aggregate"]["passed"],
            "total": item["aggregate"]["total"],
            "success_rate": item["aggregate"]["success_rate"],
        }
        for item in report["reports"]
    }
    assert summary == {
        "single_shot": {"passed": 21, "total": 30, "success_rate": 0.7},
        "no_plan": {"passed": 24, "total": 30, "success_rate": 0.8},
        "text_only": {"passed": 24, "total": 30, "success_rate": 0.8},
        "patchloop": {"passed": 30, "total": 30, "success_rate": 1.0},
    }
    assert json.loads(result.output)["suite_revision"] == "week08-v1"


def test_one_command_runs_ablation_optimization_and_stability_experiments(
    tmp_path: Path,
) -> None:
    root = Path(__file__).parents[2]
    output = tmp_path / "experiments.json"

    result = CliRunner().invoke(
        app,
        [
            "experiment",
            "--manifest",
            str(root / "benchmarks" / "evaluation_manifest.json"),
            "--root",
            str(root),
            "--output",
            str(output),
            "--repeats",
            "2",
            "--jobs",
            "4",
        ],
    )

    assert result.exit_code == 0, result.output
    report = json.loads(output.read_text(encoding="utf-8"))
    variants = {item["name"]: item for item in report["variants"]}
    assert {name: item["mean_success_rate"] for name, item in variants.items()} == {
        "single-shot-baseline": 0.7,
        "routed-text": 0.8,
        "full": 1.0,
        "no-planning": 1.0,
        "no-retrieval": 0.5,
        "no-reflection": 0.8,
    }
    assert all(item["deterministic_results"] for item in variants.values())
    assert all(item["success_rate_stddev"] == 0.0 for item in variants.values())
    assert all(item["total_cost_usd"] == 0.0 for item in variants.values())
    assert report["largest_baseline_failures"] == {
        "documentation_not_retrieved": 6,
        "test_source_confusion": 3,
    }
    assert [item["success_rate"] for item in report["optimization_stages"]] == [
        0.7,
        0.8,
        1.0,
    ]
