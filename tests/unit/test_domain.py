import pytest
from pydantic import ValidationError

from patchloop.domain import Plan, PlanItem, StepStatus, Task, TaskStatus


def test_task_allows_valid_lifecycle(tmp_path: object) -> None:
    task = Task(goal="Fix pagination", repository=str(tmp_path))

    task.transition(TaskStatus.RUNNING)
    task.transition(TaskStatus.COMPLETED, message="Fixed and verified")

    assert task.status is TaskStatus.COMPLETED
    assert task.result == "Fixed and verified"


def test_task_rejects_invalid_transition(tmp_path: object) -> None:
    task = Task(goal="Fix pagination", repository=str(tmp_path))

    with pytest.raises(ValueError, match="invalid task transition"):
        task.transition(TaskStatus.COMPLETED)


def test_plan_rejects_multiple_running_items() -> None:
    with pytest.raises(ValidationError, match="at most one running item"):
        Plan(
            items=[
                PlanItem(description="First", status=StepStatus.RUNNING),
                PlanItem(description="Second", status=StepStatus.RUNNING),
            ]
        )
