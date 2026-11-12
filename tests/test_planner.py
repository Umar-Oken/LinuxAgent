from __future__ import annotations

import pytest
from pydantic import BaseModel, ConfigDict

from core.llm_client import ToolCall
from core.planner import PlanLimitError, PlanStep, StepStatus, TaskPlan, TaskPlanner
from core.tool_registry import ToolDefinition, ToolRegistry


class _Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    value: str


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="read_item",
            description="Read one item.",
            category="filesystem",
            argument_schema=_Arguments.model_json_schema(),
            argument_model=_Arguments,
            permission_level="READ_ONLY",
            execution_timeout=1,
            implementation=lambda value: value,
        )
    )
    registry.register(
        ToolDefinition(
            name="report_status",
            description="Report system status.",
            category="system",
            argument_schema=_Arguments.model_json_schema(),
            argument_model=_Arguments,
            permission_level="READ_ONLY",
            execution_timeout=1,
            implementation=lambda value: value,
        )
    )
    return registry


def test_creates_structural_plan_for_simple_request() -> None:
    plan = TaskPlanner().create_plan(
        "Read one item",
        [ToolCall(name="read_item", arguments={"value": "notes.txt"})],
        _registry(),
    )

    assert plan.task == "Read one item"
    assert plan.steps[0].step_id == "step-1"
    assert plan.steps[0].required_tool_category == "filesystem"
    assert plan.steps[0].dependencies == ()
    assert plan.steps[0].status == StepStatus.PENDING


def test_multistep_plan_preserves_tool_order_as_dependencies() -> None:
    plan = TaskPlanner().create_plan(
        "Read then report",
        [
            ToolCall(name="read_item", arguments={"value": "notes.txt"}),
            ToolCall(name="report_status", arguments={"value": "complete"}),
        ],
        _registry(),
    )

    assert [step.required_tool_category for step in plan.steps] == [
        "filesystem",
        "system",
    ]
    assert plan.steps[1].dependencies == ("step-1",)
    assert [step.step_id for step in plan.ready_steps()] == ["step-1"]
    plan.update_status("step-1", StepStatus.RUNNING)
    plan.update_status("step-1", StepStatus.COMPLETED)
    assert [step.step_id for step in plan.ready_steps()] == ["step-2"]


def test_failed_step_blocks_dependents_and_prevents_execution_state() -> None:
    plan = TaskPlanner().create_plan(
        "Read then report",
        [
            ToolCall(name="read_item", arguments={"value": "missing.txt"}),
            ToolCall(name="report_status", arguments={"value": "complete"}),
        ],
        _registry(),
    )
    plan.update_status("step-1", StepStatus.RUNNING)
    plan.update_status("step-1", StepStatus.FAILED)

    assert plan.steps[0].status == StepStatus.FAILED
    assert plan.steps[1].status == StepStatus.BLOCKED
    assert plan.ready_steps() == []
    with pytest.raises(ValueError, match="invalid plan step"):
        plan.update_status("step-1", StepStatus.COMPLETED)


def test_replan_count_and_plan_limits_are_enforced() -> None:
    planner = TaskPlanner(max_steps=1, max_tool_calls=1)
    with pytest.raises(PlanLimitError):
        planner.create_plan(
            "Read and report",
            [
                ToolCall(name="read_item", arguments={"value": "notes.txt"}),
                ToolCall(name="report_status", arguments={"value": "complete"}),
            ],
            _registry(),
        )

    plan = TaskPlanner().create_plan(
        "Retry reading",
        [ToolCall(name="read_item", arguments={"value": "notes.txt"})],
        _registry(),
        replan_count=1,
    )
    assert plan.replan_count == 1


def test_unknown_tool_plan_does_not_authorize_execution() -> None:
    plan = TaskPlanner().create_plan(
        "Run unavailable tool",
        [ToolCall(name="unknown_tool", arguments={})],
        _registry(),
    )

    assert plan.steps[0].required_tool_category == "unknown"


@pytest.mark.parametrize(
    "dependencies",
    [("missing-step",), ("step-1",)],
)
def test_task_plan_rejects_missing_or_cyclic_dependencies(
    dependencies: tuple[str, ...],
) -> None:
    with pytest.raises(ValueError, match="dependency"):
        TaskPlan(
            task="Invalid plan",
            steps=[
                PlanStep(
                    step_id="step-1",
                    description="Read one item.",
                    required_tool_category="filesystem",
                    dependencies=dependencies,
                    tool_call=ToolCall(
                        name="read_item",
                        arguments={"value": "notes.txt"},
                    ),
                )
            ],
        )


def test_task_plan_rejects_multi_step_dependency_cycle() -> None:
    call = ToolCall(name="read_item", arguments={"value": "notes.txt"})
    with pytest.raises(ValueError, match="cycle"):
        TaskPlan(
            task="Cyclic plan",
            steps=[
                PlanStep(
                    step_id="step-1",
                    description="First.",
                    required_tool_category="filesystem",
                    dependencies=("step-2",),
                    tool_call=call,
                ),
                PlanStep(
                    step_id="step-2",
                    description="Second.",
                    required_tool_category="filesystem",
                    dependencies=("step-1",),
                    tool_call=call,
                ),
            ],
        )
