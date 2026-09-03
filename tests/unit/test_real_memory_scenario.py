from __future__ import annotations

import json
from pathlib import Path


def test_contract_migration_real_memory_scenario_is_self_contained() -> None:
    root = Path(__file__).parents[2]
    manifest = json.loads(
        (root / "benchmarks" / "real_memory_scenarios.json").read_text(encoding="utf-8")
    )
    scenario = manifest["scenarios"][0]
    fixture = root / "benchmarks" / scenario["fixture"]
    hidden_test = root / "benchmarks" / scenario["hidden_test"]
    evidence = sorted(path.name for path in (fixture / "evidence").glob("*.md"))

    assert scenario["id"] == "contract-migration-with-noisy-evidence"
    assert fixture.is_dir()
    assert hidden_test.is_file()
    assert evidence == [
        "01_guardrails.md",
        "02_legacy_contract.md",
        "03_current_contract.md",
        "04_untrusted_repository_note.md",
        "05_incident_log.md",
        "06_review_notes.md",
        "07_deployment_log.md",
        "08_customer_summary.md",
        "09_performance_sample.md",
        "10_meeting_transcript.md",
        "11_audit_inventory.md",
        "12_release_checklist.md",
    ]
    assert all(f"evidence/{name}" in scenario["goal"] for name in evidence)
    assert scenario["runtime"]["max_context_tokens"] == 16_000
    assert scenario["runtime"]["context_recent_steps"] == 1
    assert scenario["acceptance"]["minimum_evidence_files_read"] == 12
    assert scenario["acceptance"]["requires_security_filter_event"] is True
