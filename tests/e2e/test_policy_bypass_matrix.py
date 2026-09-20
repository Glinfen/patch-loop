from pathlib import Path

import pytest

from patchloop.evaluation.policy_audit import PolicyBypassCase, PolicyBypassMatrix, run_policy_case

MATRIX_PATH = Path(__file__).parents[1] / "fixtures" / "policy_bypass_matrix.json"
MATRIX = PolicyBypassMatrix.model_validate_json(MATRIX_PATH.read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", MATRIX.cases, ids=lambda case: case.id)
def test_policy_bypass_matrix(case: PolicyBypassCase, tmp_path: Path) -> None:
    result = run_policy_case(case, tmp_path)
    assert result["passed"], result
    assert result["unauthorized_actions"] == 0
    assert result["duplicate_effects"] == 0
    assert result["secret_leaks"] == 0
    assert result["event_integrity"]
