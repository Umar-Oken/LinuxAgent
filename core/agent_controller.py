"""Bounded agent execution loop connecting the model and guarded tools."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from collections.abc import Callable
from pathlib import Path
from threading import Event
from typing import Literal
from uuid import uuid4

import psycopg
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from core.context_manager import ContextManager, ExecutionConstraints
from core.llm_client import LLMClient, LLMError, LLMResponse
from core.memory import MemoryRecorder, MemoryStorageError
from core.planner import PlanLimitError, StepStatus, TaskPlan, TaskPlanner
from core.result_validator import ToolResultValidator
from core.tool_dispatcher import ToolDispatcher, ToolResult
from core.tool_registry import ToolRegistry, ToolRegistryError
from security.error_handler import ApplicationErrorHandler
from security.path_guard import PathGuard

TaskStatus = Literal[
    "completed",
    "step_limit",
    "tool_call_limit",
    "retry_limit",
    "time_limit",
    "cancelled",
    "llm_error",
    "invalid_response",
]

_SECRET_PATTERN = re.compile(
    r"(?im)(\b(?:api[_-]?key|token|password|passwd|secret|authorization)\b"
    r"\s*[:=]\s*).+"
)
_ABSOLUTE_PATH_PATTERN = re.compile(
    r"(?<![\w:])/(?:[A-Za-z0-9._~-]+/)+[A-Za-z0-9._~-]+"
)


class AgentTaskResult(BaseModel):
    """Outcome and counters for one bounded user task."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: TaskStatus
    response: str | None = None
    steps: int = Field(ge=0)
    tool_calls: int = Field(ge=0)
    task_id: str


class AgentController:
    """Run one user request through the configured LLM and tool boundary."""

    def __init__(
        self,
        *,
        client: LLMClient,
        registry: ToolRegistry,
        dispatcher: ToolDispatcher,
        path_guard: PathGuard,
        system_prompt: str,
        max_steps: int = 8,
        max_tool_calls: int = 8,
        max_action_retries: int = 1,
        max_replans: int = 2,
        max_task_seconds: float = 300,
        max_context_tokens: int = 16_000,
        max_tool_result_chars: int = 12_000,
        memory: MemoryRecorder | None = None,
        output: Callable[[str], None] = print,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if (
            max_steps < 1
            or max_tool_calls < 1
            or max_action_retries < 0
            or max_replans < 0
        ):
            raise ValueError("execution limits must be positive (retries may be zero)")
        if max_task_seconds <= 0:
            raise ValueError("max_task_seconds must be greater than zero")
        self._client = client
        self._registry = registry
        self._dispatcher = dispatcher
        self._path_guard = path_guard
        self._system_prompt = system_prompt
        self._max_steps = max_steps
        self._max_tool_calls = max_tool_calls
        self._max_action_retries = max_action_retries
        self._max_replans = max_replans
        self._max_task_seconds = max_task_seconds
        self._planner = TaskPlanner(
            max_steps=max_steps,
            max_tool_calls=max_tool_calls,
            max_replans=max_replans,
        )
        self._last_plan: TaskPlan | None = None
        self._context = ContextManager(
            max_context_tokens=max_context_tokens,
            max_tool_result_chars=max_tool_result_chars,
        )
        self._result_validator = ToolResultValidator(
            path_guard,
            max_retries=max_action_retries,
        )
        self._memory = memory
        self._output = output
        self._clock = clock

    def run_task(
        self,
        task: str,
        *,
        cancellation: Event | None = None,
    ) -> AgentTaskResult:
        task_id = str(uuid4())
        memory_started = False
        if self._memory is not None:
            try:
                self._memory.start_task(task_id, task)
                memory_started = True
            except (psycopg.Error, MemoryStorageError, ValueError):
                self._output("XATO: Vazifa xotira bazasiga saqlanmadi.")

        result = self._run_task(task, task_id=task_id, cancellation=cancellation)
        if memory_started and self._memory is not None:
            try:
                self._memory.finish_task(
                    task_id,
                    status=result.status,
                    response=result.response,
                )
            except (psycopg.Error, MemoryStorageError, ValueError):
                self._output("XATO: Vazifa natijasi xotira bazasiga saqlanmadi.")
        return result

    @property
    def last_plan(self) -> TaskPlan | None:
        """Return the latest structural plan and its final step states."""
        return self._last_plan

    def _run_task(
        self,
        task: str,
        *,
        task_id: str,
        cancellation: Event | None,
    ) -> AgentTaskResult:
        """Run a bounded task; cancellation is observed between blocking calls.

        A synchronous model or tool callable cannot be forcibly interrupted.
        Its own transport/tool timeout must bound the call; cancellation and
        task deadlines are applied as soon as the call returns.
        """
        self._context.start_task(
            task,
            constraints=ExecutionConstraints(
                max_steps=self._max_steps,
                max_tool_calls=self._max_tool_calls,
                max_action_retries=self._max_action_retries,
                max_replans=self._max_replans,
            ),
        )
        self._result_validator.start_task()
        deadline = self._clock() + self._max_task_seconds
        steps = 0
        planned_steps = 0
        calls = 0
        failed_attempts: dict[str, int] = {}
        replan_count = 0
        needs_replan = False

        while steps < self._max_steps:
            if cancellation is not None and cancellation.is_set():
                return self._finish("cancelled", task_id, steps, calls, "Vazifa bekor qilindi.")
            if self._clock() >= deadline:
                return self._finish("time_limit", task_id, steps, calls, "Vazifa uchun vaqt limiti tugadi.")

            try:
                raw_response = self._client.chat(
                    self._context.history,
                    system_prompt=self._system_prompt,
                    tools=self._registry.llm_tool_definitions(),
                )
                response = LLMResponse.model_validate(raw_response)
                _validate_response_consistency(response)
            except KeyboardInterrupt:
                return self._finish(
                    "cancelled",
                    task_id,
                    steps,
                    calls,
                    "Vazifa bekor qilindi.",
                )
            except LLMError as error:
                self._report_error(task_id, error, "llm")
                return AgentTaskResult(
                    status="llm_error",
                    steps=steps + 1,
                    tool_calls=calls,
                    task_id=task_id,
                )
            except (ValidationError, TypeError, ValueError):
                self._output("XATO: Modeldan yaroqsiz javob keldi.")
                return AgentTaskResult(
                    status="invalid_response",
                    steps=steps + 1,
                    tool_calls=calls,
                    task_id=task_id,
                )
            except Exception as error:
                self._report_error(task_id, error, "llm")
                return AgentTaskResult(
                    status="llm_error",
                    steps=steps + 1,
                    tool_calls=calls,
                    task_id=task_id,
                )

            steps += 1
            if cancellation is not None and cancellation.is_set():
                return self._finish("cancelled", task_id, steps, calls, "Vazifa bekor qilindi.")
            if self._clock() >= deadline:
                return self._finish("time_limit", task_id, steps, calls, "Vazifa uchun vaqt limiti tugadi.")

            if not response.tool_calls:
                self._output(f"Agent: {response.content}")
                return AgentTaskResult(
                    status="completed",
                    response=response.content,
                    steps=steps,
                    tool_calls=calls,
                    task_id=task_id,
                )

            if len(response.tool_calls) > self._max_tool_calls - calls:
                return self._finish(
                    "tool_call_limit",
                    task_id,
                    steps,
                    calls,
                    "Vazifa tool chaqiruvlari limitiga yetdi.",
                )
            if len(response.tool_calls) > self._max_steps - planned_steps:
                return self._finish(
                    "step_limit",
                    task_id,
                    steps,
                    calls,
                    "Vazifa rejalashtirilgan bosqichlar limitiga yetdi.",
                )

            if needs_replan:
                if replan_count >= self._max_replans:
                    return self._finish(
                        "retry_limit",
                        task_id,
                        steps,
                        calls,
                        "Qayta rejalashtirish limiti tugadi.",
                    )
                replan_count += 1
                needs_replan = False
            try:
                plan = self._planner.create_plan(
                    task,
                    response.tool_calls,
                    self._registry,
                    replan_count=replan_count,
                )
            except PlanLimitError:
                return self._finish(
                    "tool_call_limit",
                    task_id,
                    steps,
                    calls,
                    "Reja belgilangan bosqich yoki tool chaqiruvi limitidan oshdi.",
                )
            except ValueError:
                self._output("XATO: Model yaratgan vazifa rejasi yaroqsiz.")
                return AgentTaskResult(
                    status="invalid_response",
                    steps=steps,
                    tool_calls=calls,
                    task_id=task_id,
                )
            self._last_plan = plan
            planned_steps += len(plan.steps)
            self._output(
                f"[reja] {len(plan.steps)} bosqich; qayta rejalashtirish: "
                f"{plan.replan_count}/{self._max_replans}"
            )
            self._context.append(response.assistant_message)
            native_tool_calls = response.assistant_message.tool_calls is not None
            for planned_step in plan.steps:
                tool_call = planned_step.tool_call
                if cancellation is not None and cancellation.is_set():
                    return self._finish("cancelled", task_id, steps, calls, "Vazifa bekor qilindi.")
                if self._clock() >= deadline:
                    return self._finish("time_limit", task_id, steps, calls, "Vazifa uchun vaqt limiti tugadi.")
                calls += 1
                self._output(f"[step] Vosita: {tool_call.name}")
                if planned_step.status == StepStatus.BLOCKED:
                    tool_result = ToolResult(
                        tool_name=tool_call.name,
                        status="error",
                        error_code="DEPENDENCY_FAILED",
                        safe_error_message="Tool step skipped because a required step failed.",
                        execution_duration=0,
                    )
                    self._record_tool_memory(task_id, tool_result)
                    self._context.record_permission(
                        _call_fingerprint(
                            tool_call.name,
                            tool_call.arguments,
                        ),
                        "denied_dependency",
                    )
                    self._context.append_tool_result(
                        tool_call.name,
                        _safe_tool_result(tool_result, self._path_guard),
                        native_tool_call=native_tool_calls,
                    )
                    continue
                plan.update_status(planned_step.step_id, StepStatus.RUNNING)

                fingerprint = _call_fingerprint(tool_call.name, tool_call.arguments)
                failures = failed_attempts.get(fingerprint, 0)
                if failures > self._max_action_retries:
                    plan.update_status(planned_step.step_id, StepStatus.FAILED)
                    needs_replan = True
                    return self._finish(
                        "retry_limit",
                        task_id,
                        steps,
                        calls,
                        "Bir xil muvaffaqiyatsiz amal uchun qayta urinish limiti tugadi.",
                    )

                permission_level = "PROHIBITED"
                try:
                    definition = self._registry.validate_tool_selection(
                        tool_call.name,
                        tool_call.arguments,
                    )
                    permission_level = definition.permission_level
                except ToolRegistryError:
                    # The dispatcher returns the stable, safe unknown/disabled/schema result.
                    definition = None
                if definition is not None:
                    may_execute, _idempotency_key = self._result_validator.begin_execution(
                        task_id,
                        tool_call.name,
                        tool_call.arguments,
                    )
                else:
                    may_execute = True
                if not may_execute:
                    tool_result = ToolResult(
                        tool_name=tool_call.name,
                        status="error",
                        error_code="DUPLICATE_EXECUTION",
                        safe_error_message=(
                            "Amal takrorlanmadi: oldingi bajarilish holati noaniq yoki "
                            "qayta bajarish xavfsiz emas; avval natijani tekshiring."
                        ),
                        execution_duration=0,
                    )
                else:
                    try:
                        tool_result = asyncio.run(
                            self._dispatcher.dispatch(tool_call, task_id=task_id)
                        )
                    except KeyboardInterrupt:
                        return self._finish(
                            "cancelled",
                            task_id,
                            steps,
                            calls,
                            "Vazifa bekor qilindi.",
                        )
                    except Exception:
                        tool_result = ToolResult(
                            tool_name=tool_call.name,
                            status="error",
                            error_code="DISPATCH_ERROR",
                            safe_error_message="Tool execution failed.",
                            execution_duration=0,
                        )
                    validation = self._result_validator.validate(
                        task_id,
                        tool_call.name,
                        tool_call.arguments,
                        tool_result,
                        permission_level=permission_level,
                    )
                    if validation.status == "invalid" and tool_result.status == "success":
                        tool_result = ToolResult(
                            tool_name=tool_call.name,
                            status="error",
                            error_code="RESULT_VALIDATION_FAILED",
                            safe_error_message=validation.message,
                            execution_duration=tool_result.execution_duration,
                        )
                    elif validation.status in {"partial", "uncertain"}:
                        tool_result = tool_result.model_copy(
                            update={"safe_error_message": validation.message}
                        )
                self._record_tool_memory(task_id, tool_result)
                self._context.record_permission(
                    fingerprint,
                    _permission_decision(tool_result),
                )
                plan.update_status(
                    planned_step.step_id,
                    StepStatus.COMPLETED
                    if tool_result.status == "success"
                    else StepStatus.FAILED,
                )
                if tool_result.status != "success":
                    needs_replan = True

                if cancellation is not None and cancellation.is_set():
                    return self._finish(
                        "cancelled",
                        task_id,
                        steps,
                        calls,
                        "Vazifa bekor qilindi; joriy amal yakunlandi.",
                    )
                if self._clock() >= deadline:
                    return self._finish(
                        "time_limit",
                        task_id,
                        steps,
                        calls,
                        "Vazifa uchun vaqt limiti tugadi.",
                    )

                if tool_result.status == "success":
                    failed_attempts.pop(fingerprint, None)
                else:
                    failed_attempts[fingerprint] = failures + 1
                safe_result = _safe_tool_result(tool_result, self._path_guard)
                preview = safe_result.get(
                    "result_data",
                    safe_result.get("message", ""),
                )
                self._show_preview(str(preview))
                self._context.append_tool_result(
                    tool_call.name,
                    safe_result,
                    native_tool_call=native_tool_calls,
                )

        return self._finish(
            "step_limit",
            task_id,
            steps,
            calls,
            f"Vazifa {self._max_steps} agent bosqichi limitiga yetdi.",
        )

    def _record_tool_memory(self, task_id: str, result: ToolResult) -> None:
        if self._memory is None:
            return
        try:
            self._memory.record_tool_execution(
                task_id,
                tool_name=result.tool_name,
                status=result.status,
                error_code=result.error_code,
                duration_seconds=result.execution_duration,
            )
        except (psycopg.Error, MemoryStorageError, ValueError):
            self._output("XATO: Vosita xotira tarixiga yozilmadi.")

    def _finish(
        self,
        status: TaskStatus,
        task_id: str,
        steps: int,
        calls: int,
        message: str,
    ) -> AgentTaskResult:
        self._output(message)
        return AgentTaskResult(
            status=status,
            steps=steps,
            tool_calls=calls,
            task_id=task_id,
        )

    def _show_preview(self, result: str) -> None:
        preview = " ".join(result.split())
        if len(preview) > 220:
            preview = preview[:217] + "..."
        self._output(f"[natija] {preview}")

    def _report_error(
        self,
        task_id: str,
        error: BaseException,
        component: str,
    ) -> None:
        self._output(f"XATO: {ApplicationErrorHandler().user_message(error)}")
        audit_logger = self._dispatcher.audit_logger
        if audit_logger is None:
            return
        try:
            audit_logger.log_exception(
                task_id=task_id,
                component=component,
                error_code=type(error).__name__,
            )
        except Exception:
            self._output("XATO: Xatolikni audit jurnaliga yozib bo'lmadi.")


def _safe_tool_result(
    result: ToolResult,
    path_guard: PathGuard,
) -> dict[str, object]:
    result_data = result.result_data
    if isinstance(result_data, str):
        for index, root in enumerate(path_guard.allowed_roots):
            result_data = result_data.replace(str(root), f"@root{index}")
        result_data = result_data.replace(str(Path.home()), "~")
        result_data = _ABSOLUTE_PATH_PATTERN.sub("<local-path>", result_data)
        result_data = _SECRET_PATTERN.sub(r"\1[REDACTED]", result_data)

    payload: dict[str, object] = {
        "tool_name": result.tool_name,
        "status": result.status,
    }
    if result.status == "success":
        payload["result_data"] = result_data
    else:
        payload["error_code"] = result.error_code or "TOOL_ERROR"
        payload["message"] = result.safe_error_message or "Tool execution failed."
    return payload


def _permission_decision(result: ToolResult) -> str:
    if result.status == "success":
        return "authorized"
    if result.error_code in {"PERMISSION_DENIED", "APPROVAL_DENIED"}:
        return "denied"
    if result.error_code in {"APPROVAL_REQUIRED", "APPROVAL_UNAVAILABLE"}:
        return "approval_required"
    return "not_authorized_or_failed"


def _call_fingerprint(name: str, arguments: dict[str, object]) -> str:
    payload = json.dumps(
        {"name": name, "arguments": arguments},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _validate_response_consistency(response: LLMResponse) -> None:
    message = response.assistant_message
    if message.role != "assistant":
        raise ValueError("model response assistant message has an invalid role")
    if message.tool_calls is not None and message.tool_calls != response.tool_calls:
        raise ValueError("model response tool calls are inconsistent")
