"""Explicit plan management for the Plan-Execute runtime."""

from typing import Any

from pydantic import BaseModel, Field, field_validator

from patchloop.domain import Plan, PlanItem, StepStatus
from patchloop.tools.base import Tool, ToolContext, ToolInputModel


class PlanItemInput(ToolInputModel):
    description: str = Field(min_length=1)
    status: StepStatus = StepStatus.PENDING
    evidence: list[str] = Field(default_factory=list)

    @field_validator("status", mode="before")
    @classmethod
    def normalize_in_progress(cls, value: Any) -> Any:
        return StepStatus.RUNNING if value == "in_progress" else value


class UpdatePlanInput(ToolInputModel):
    items: list[PlanItemInput] = Field(min_length=1, max_length=50)


class UpdatePlanTool(Tool):
    name = "update_plan"
    description = (
        "Create or replace the execution plan. Keep at most one item running and attach "
        "evidence when completing an item."
    )
    input_model = UpdatePlanInput

    def run(self, arguments: BaseModel, context: ToolContext) -> str:
        request = UpdatePlanInput.model_validate(arguments)
        revision = 1 if context.plan is None else context.plan.revision + 1
        if context.requires_replan:
            context.replan_count += 1
        context.plan = Plan(
            items=[
                PlanItem(
                    description=item.description,
                    status=item.status,
                    evidence=item.evidence,
                )
                for item in request.items
            ],
            revision=revision,
        )
        context.requires_replan = False
        return context.plan.model_dump_json()
