"""Bounded structural plans for tool calls selected by the language model."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from core.llm_client import ToolCall
from core.tool_registry import ToolRegistry, ToolRegistryError

ToolCategoryName = Literal[
    "filesystem",
    "system",
    "network",
    "calculation",
    "unknown",
]


class StepStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"


class PlanStep(BaseModel):
    """One bounded planned tool invocation."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    step_id: str = Field(min_length=1, max_length=32)
    description: str = Field(min_length=1, max_length=512)
    required_tool_category: ToolCategoryName = Field(min_length=1, max_length=32)
    dependencies: tuple[str, ...] = ()
    status: StepStatus = StepStatus.PENDING
    tool_call: ToolCall


class TaskPlan(BaseModel):
    """Structural task plan; it never grants permission to execute a tool."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    task: str = Field(min_length=1, max_length=20_000)
    steps: list[PlanStep] = Field(min_length=1, max_length=100)
    replan_count: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def require_valid_dependencies(self) -> TaskPlan:
        self.validate_dependencies()
        return self

    def validate_dependencies(self) -> None:
        identifiers = [step.step_id for step in self.steps]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("plan step identifiers must be unique")
        known = set(identifiers)
        for step in self.steps:
            if step.step_id in step.dependencies or not set(step.dependencies) <= known:
                raise ValueError("plan contains an invalid dependency")
        completed: set[str] = set()
        remaining = {step.step_id: set(step.dependencies) for step in self.steps}
        while remaining:
            ready = {
                step_id
                for step_id, dependencies in remaining.items()
                if dependencies <= completed
            }
            if not ready:
                raise ValueError("plan dependencies contain a cycle")
            completed.update(ready)
            for step_id in ready:
                remaining.pop(step_id)

    def ready_steps(self) -> list[PlanStep]:
        completed = {
            step.step_id for step in self.steps if step.status == StepStatus.COMPLETED
        }
        return [
            step
            for step in self.steps
            if step.status == StepStatus.PENDING
            and set(step.dependencies) <= completed
        ]

    def update_status(self, step_id: str, status: StepStatus) -> None:
        step = next((item for item in self.steps if item.step_id == step_id), None)
        if step is None:
            raise ValueError("unknown plan step")
        allowed_transitions = {
            StepStatus.PENDING: {StepStatus.RUNNING, StepStatus.BLOCKED},
            StepStatus.RUNNING: {StepStatus.COMPLETED, StepStatus.FAILED},
        }
        if status not in allowed_transitions.get(step.status, set()):
            raise ValueError("invalid plan step status transition")
        if status == StepStatus.RUNNING and not set(step.dependencies) <= {
            item.step_id
            for item in self.steps
            if item.status == StepStatus.COMPLETED
        }:
            raise ValueError("plan step dependencies have not completed")
        step.status = status
        if status in {StepStatus.FAILED, StepStatus.BLOCKED}:
            failed = {step_id}
            changed = True
            while changed:
                changed = False
                for dependent in self.steps:
                    if (
                        dependent.status == StepStatus.PENDING
                        and failed.intersection(dependent.dependencies)
                    ):
                        dependent.status = StepStatus.BLOCKED
                        failed.add(dependent.step_id)
                        changed = True


class TaskPlanner:
    """Build execution plans exclusively from registry-described tool calls."""

    def __init__(
        self,
        *,
        max_steps: int = 8,
        max_tool_calls: int = 8,
        max_replans: int = 2,
    ) -> None:
        if max_steps < 1 or max_tool_calls < 1 or max_replans < 0:
            raise ValueError("planner limits are invalid")
        self._max_steps = max_steps
        self._max_tool_calls = max_tool_calls
        self._max_replans = max_replans

    def create_plan(
        self,
        task: str,
        tool_calls: list[ToolCall],
        registry: ToolRegistry,
        *,
        replan_count: int = 0,
    ) -> TaskPlan:
        if not task.strip() or len(task) > 20_000:
            raise ValueError("task must contain 1 to 20000 characters")
        if not tool_calls:
            raise ValueError("a plan requires at least one tool call")
        if len(tool_calls) > self._max_steps or len(tool_calls) > self._max_tool_calls:
            raise PlanLimitError("task plan exceeds configured step or tool-call limits")
        if not 0 <= replan_count <= self._max_replans:
            raise PlanLimitError("task plan exceeds configured replanning limit")
        steps: list[PlanStep] = []
        for index, tool_call in enumerate(tool_calls):
            try:
                definition = registry.get_tool(tool_call.name)
                category = definition.category
                description = definition.description
            except ToolRegistryError:
                category = "unknown"
                description = f"Validate and handle the requested {tool_call.name} tool call."
            dependencies = (f"step-{index}",) if index else ()
            steps.append(
                PlanStep(
                    step_id=f"step-{index + 1}",
                    description=description,
                    required_tool_category=category,
                    dependencies=dependencies,
                    tool_call=tool_call,
                )
            )
        return TaskPlan(task=task, steps=steps, replan_count=replan_count)


class PlanLimitError(ValueError):
    """Raised when a proposed plan exceeds the agent's execution budget."""
