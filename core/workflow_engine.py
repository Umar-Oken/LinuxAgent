"""Ro'yxatdagi toollar uchun deklarativ va ruxsatlardan o'tadigan workflow'lar."""

from __future__ import annotations

import asyncio
import copy
import json
import time
from contextlib import suppress
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from enum import StrEnum
from threading import Event, RLock
from typing import Annotated, Any, Literal
from uuid import uuid4

from jsonschema import Draft202012Validator, SchemaError
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    ValidationError,
    model_validator,
)

from core.tool_dispatcher import ToolDispatcher, ToolResult
from core.tool_registry import ToolRegistry, ToolRegistryError

_MAX_WORKFLOW_INPUT_BYTES = 64 * 1024
_MAX_WORKFLOW_DEFINITION_BYTES = 256 * 1024
_DEFAULT_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}


class WorkflowError(ValueError):
    """Workflow ta'rifi yoki so'rovi xavfsiz bajarilmaganda beriladi."""


class WorkflowStepStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


class LiteralArgument(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source: Literal["literal"]
    value: Any


class InputArgument(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source: Literal["input"]
    name: str = Field(min_length=1, max_length=128)


class ResultArgument(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source: Literal["result"]
    step_id: str = Field(min_length=1, max_length=32)
    path: tuple[StrictStr | StrictInt, ...] = ()

    @model_validator(mode="after")
    def validate_path(self) -> ResultArgument:
        if any(isinstance(part, int) and part < 0 for part in self.path):
            raise ValueError("natija yo'li manfiy indeksdan foydalana olmaydi")
        return self


WorkflowArgument = Annotated[
    LiteralArgument | InputArgument | ResultArgument,
    Field(discriminator="source"),
]


class WorkflowCondition(BaseModel):
    """Oldingi natijadagi yoki inputdagi bitta qiymatga deklarativ shart."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source: Literal["input", "result"]
    input_name: str | None = Field(default=None, min_length=1, max_length=128)
    step_id: str | None = Field(default=None, min_length=1, max_length=32)
    path: tuple[StrictStr | StrictInt, ...] = ()
    operator: Literal["exists", "truthy", "equals", "not_equals"]
    value: Any = None

    @model_validator(mode="after")
    def validate_source(self) -> WorkflowCondition:
        if self.source == "input":
            if self.input_name is None or self.step_id is not None or self.path:
                raise ValueError("input sharti faqat input_name ishlatishi mumkin")
        elif self.step_id is None or self.input_name is not None:
            raise ValueError("result sharti step_id talab qiladi")
        if self.operator in {"equals", "not_equals"}:
            _ensure_json_value(self.value)
        if any(isinstance(part, int) and part < 0 for part in self.path):
            raise ValueError("natija yo'li manfiy indeksdan foydalana olmaydi")
        return self


class WorkflowStep(BaseModel):
    """Ixtiyoriy Python yoki shell kodisiz workflow bosqichi."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    step_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,31}$")
    tool_name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    description: str = Field(min_length=1, max_length=512)
    arguments: dict[str, WorkflowArgument] = Field(default_factory=dict, max_length=64)
    depends_on: tuple[str, ...] = Field(default=(), max_length=32)
    condition: WorkflowCondition | None = None

    @model_validator(mode="after")
    def require_unique_dependencies(self) -> WorkflowStep:
        if len(set(self.depends_on)) != len(self.depends_on):
            raise ValueError("workflow bog'liqliklari takrorlanmasligi kerak")
        return self


class WorkflowDefinition(BaseModel):
    """Tekshiriladigan deklarativ workflow sxemasi."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]{0,63}$")
    description: str = Field(min_length=1, max_length=1000)
    input_schema: dict[str, Any] = Field(default_factory=lambda: dict(_DEFAULT_INPUT_SCHEMA))
    steps: list[WorkflowStep] = Field(min_length=1, max_length=32)


class WorkflowPreviewStep(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    step_id: str
    tool_name: str
    description: str
    arguments: dict[str, object]
    conditional: bool


class WorkflowPreview(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    workflow_name: str
    description: str
    steps: tuple[WorkflowPreviewStep, ...]


class WorkflowStepRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    step_id: str
    tool_name: str
    status: WorkflowStepStatus
    error_code: str | None = None
    execution_duration: float = Field(ge=0)


class WorkflowHistoryEntry(BaseModel):
    """Natija mazmuni yoki maxfiy argumentlarni saqlamaydigan ish tarixi."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    workflow_name: str
    status: Literal["completed", "failed", "cancelled", "time_limit"]
    started_at: float
    duration: float = Field(ge=0)
    steps: tuple[WorkflowStepRecord, ...]


class WorkflowRunResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    history_entry: WorkflowHistoryEntry
    step_results: dict[str, ToolResult]


class WorkflowEngine:
    """Deklarativ workflow'larni validatsiya qiladi va ToolDispatcher orqali bajaradi."""

    def __init__(
        self,
        registry: ToolRegistry,
        dispatcher: ToolDispatcher,
        *,
        max_steps: int = 16,
        max_tool_calls: int = 16,
        max_run_seconds: float = 300,
        history_limit: int = 100,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_steps < 1 or max_tool_calls < 1 or max_run_seconds <= 0 or history_limit < 1:
            raise ValueError("workflow bajarilish limitlari noto'g'ri")
        self._registry = registry
        self._dispatcher = dispatcher
        self._max_steps = max_steps
        self._max_tool_calls = max_tool_calls
        self._max_run_seconds = max_run_seconds
        self._history: deque[WorkflowHistoryEntry] = deque(maxlen=history_limit)
        self._clock = clock
        self._lock = RLock()
        self._workflows: dict[str, WorkflowDefinition] = {}

    def register(self, workflow: WorkflowDefinition) -> None:
        """Faqat mavjud registry tool'lariga tayanadigan sxemani qo'shadi."""
        workflow = workflow.model_copy(deep=True)
        try:
            Draft202012Validator.check_schema(workflow.input_schema)
        except (SchemaError, TypeError, ValueError) as exc:
            raise WorkflowError("workflow input sxemasi yaroqsiz") from exc
        if workflow.input_schema.get("type") != "object":
            raise WorkflowError("workflow input sxemasi object bo'lishi kerak")
        _reject_remote_schema_references(workflow.input_schema)
        try:
            definition_data = json.dumps(
                workflow.model_dump(mode="python"),
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeError) as exc:
            raise WorkflowError("workflow ta'rifi faqat JSON qiymatlardan iborat bo'lishi kerak") from exc
        if len(definition_data) > _MAX_WORKFLOW_DEFINITION_BYTES:
            raise WorkflowError("workflow ta'rifi hajm limiti oshdi")
        if len(workflow.steps) > min(self._max_steps, self._max_tool_calls):
            raise WorkflowError("workflow bosqichlar limiti oshdi")

        known: set[str] = set()
        properties = workflow.input_schema.get("properties", {})
        if not isinstance(properties, Mapping):
            raise WorkflowError("workflow input properties yaroqsiz")
        for step in workflow.steps:
            if step.step_id in known:
                raise WorkflowError("workflow bosqich identifikatori takrorlanmasligi kerak")
            for dependency in step.depends_on:
                if dependency not in known:
                    raise WorkflowError("bog'liqlik oldin e'lon qilingan bosqichga tegishli bo'lishi kerak")
            if step.condition is not None:
                condition = step.condition
                if condition.source == "input":
                    if condition.input_name not in properties:
                        raise WorkflowError("shart noma'lum input maydoniga murojaat qildi")
                elif condition.step_id not in step.depends_on:
                    raise WorkflowError("natija sharti bog'liqlik sifatida e'lon qilinishi kerak")
            for argument in step.arguments.values():
                if isinstance(argument, InputArgument) and argument.name not in properties:
                    raise WorkflowError("bosqich noma'lum input maydoniga murojaat qildi")
                if isinstance(argument, ResultArgument) and argument.step_id not in step.depends_on:
                    raise WorkflowError("natija manbai bog'liqlik sifatida e'lon qilinishi kerak")
            try:
                definition = self._registry.get_tool(
                    step.tool_name,
                    require_enabled=False,
                )
            except ToolRegistryError as exc:
                raise WorkflowError("workflow faqat ro'yxatdan o'tgan tool'lardan foydalanishi mumkin") from exc
            argument_names = set(step.arguments)
            if not argument_names <= set(definition.argument_model.model_fields):
                raise WorkflowError("workflow tool argumentlari sxemaga mos kelmadi")
            if all(isinstance(value, LiteralArgument) for value in step.arguments.values()):
                static_arguments = {
                    name: value.value
                    for name, value in step.arguments.items()
                    if isinstance(value, LiteralArgument)
                }
                try:
                    definition.argument_model.model_validate(
                        static_arguments,
                        strict=True,
                    )
                except ValidationError as exc:
                    raise WorkflowError("workflow tool argumentlari sxemaga mos kelmadi") from exc
            known.add(step.step_id)

        with self._lock:
            if workflow.name in self._workflows:
                raise WorkflowError("workflow nomi allaqachon ro'yxatdan o'tgan")
            self._workflows[workflow.name] = workflow

    def list_workflows(self) -> tuple[WorkflowDefinition, ...]:
        """Ro'yxatdan o'tgan workflow'larning faqat deklarativ ta'riflarini qaytaradi.

        Nusxalar qaytariladi: chaqiruvchi ro'yxatdan o'tgan ta'rifni joyida
        o'zgartirib, keyingi bajarilishga ta'sir qila olmaydi.
        """
        with self._lock:
            return tuple(
                workflow.model_copy(deep=True)
                for workflow in self._workflows.values()
            )

    def register_many(self, workflows: Iterable[WorkflowDefinition]) -> None:
        """Bir nechta workflow'ni all-or-nothing tarzda ro'yxatdan o'tkazadi.

        Har bir ta'rif avval alohida validator engine'da tekshiriladi. Shuning
        uchun masalan, namunaviy workflow'lardan bittasi mavjud bo'lmagan toolga
        murojaat qilsa, avvalgilari qisman registrda qolmaydi.
        """
        candidates = tuple(workflow.model_copy(deep=True) for workflow in workflows)
        if not candidates:
            return
        validator = WorkflowEngine(
            self._registry,
            self._dispatcher,
            max_steps=self._max_steps,
            max_tool_calls=self._max_tool_calls,
            max_run_seconds=self._max_run_seconds,
            history_limit=1,
            clock=self._clock,
        )
        for workflow in candidates:
            validator.register(workflow)

        with self._lock:
            if any(workflow.name in self._workflows for workflow in candidates):
                raise WorkflowError("workflow nomi allaqachon ro'yxatdan o'tgan")
            self._workflows.update(
                {
                    workflow.name: workflow.model_copy(deep=True)
                    for workflow in candidates
                }
            )

    def preview(
        self,
        workflow_name: str,
        inputs: Mapping[str, object] | None = None,
    ) -> WorkflowPreview:
        """Workflow rejasini ko'rsatadi, tool'larni chaqirmaydi."""
        workflow = self._get_workflow(workflow_name)
        safe_inputs = self._validate_inputs(
            workflow,
            inputs if inputs is not None else {},
        )
        preview_steps: list[WorkflowPreviewStep] = []
        for step in workflow.steps:
            arguments: dict[str, object] = {}
            for name, value in step.arguments.items():
                if isinstance(value, LiteralArgument):
                    arguments[name] = copy.deepcopy(value.value)
                elif isinstance(value, InputArgument):
                    arguments[name] = safe_inputs.get(value.name, "<input qiymati kerak>")
                else:
                    arguments[name] = f"<{value.step_id} natijasidan olinadi>"
            preview_steps.append(
                WorkflowPreviewStep(
                    step_id=step.step_id,
                    tool_name=step.tool_name,
                    description=step.description,
                    arguments=arguments,
                    conditional=step.condition is not None,
                )
            )
        return WorkflowPreview(
            workflow_name=workflow.name,
            description=workflow.description,
            steps=tuple(preview_steps),
        )

    async def run(
        self,
        workflow_name: str,
        inputs: Mapping[str, object] | None = None,
        *,
        cancellation: Event | None = None,
    ) -> WorkflowRunResult:
        """Workflow bosqichlarini navbat bilan, har birini dispatcher'dan o'tkazib bajaradi."""
        workflow = self._get_workflow(workflow_name)
        safe_inputs = self._validate_inputs(
            workflow,
            inputs if inputs is not None else {},
        )
        run_id = str(uuid4())
        started = self._clock()
        started_at = time.time()
        deadline = started + self._max_run_seconds
        records: list[WorkflowStepRecord] = []
        results: dict[str, ToolResult] = {}
        states: dict[str, WorkflowStepStatus] = {}
        status: Literal["completed", "failed", "cancelled", "time_limit"] = "completed"

        for index, step in enumerate(workflow.steps):
            if cancellation is not None and cancellation.is_set():
                status = "cancelled"
                records.extend(_terminal_records(workflow.steps[index:], WorkflowStepStatus.CANCELLED))
                break
            if self._clock() >= deadline:
                status = "time_limit"
                records.extend(_terminal_records(workflow.steps[index:], WorkflowStepStatus.BLOCKED))
                break
            if any(states.get(dependency) != WorkflowStepStatus.COMPLETED for dependency in step.depends_on):
                states[step.step_id] = WorkflowStepStatus.BLOCKED
                records.append(_record(step, WorkflowStepStatus.BLOCKED))
                status = "failed"
                records.extend(
                    _terminal_records(workflow.steps[index + 1 :], WorkflowStepStatus.BLOCKED)
                )
                break
            if step.condition is not None and not _condition_matches(
                step.condition,
                safe_inputs,
                results,
            ):
                states[step.step_id] = WorkflowStepStatus.SKIPPED
                records.append(_record(step, WorkflowStepStatus.SKIPPED))
                continue

            states[step.step_id] = WorkflowStepStatus.RUNNING
            args_started = self._clock()
            try:
                arguments = _resolve_arguments(step, safe_inputs, results)
                definition = self._registry.get_tool(
                    step.tool_name,
                    require_enabled=False,
                )
                validated = definition.argument_model.model_validate(
                    arguments,
                    strict=True,
                )
                arguments = validated.model_dump(mode="python")
            except (WorkflowError, ToolRegistryError, ValidationError, TypeError, ValueError):
                failed_result = ToolResult(
                    tool_name=step.tool_name,
                    status="error",
                    error_code="INVALID_WORKFLOW_ARGUMENTS",
                    safe_error_message="Workflow bosqichining argumentlari yaroqsiz.",
                    execution_duration=max(0.0, self._clock() - args_started),
                )
            else:
                failed_result = await self._dispatch_with_cancellation(
                    {"name": step.tool_name, "arguments": arguments},
                    run_id=run_id,
                    cancellation=cancellation,
                )
            if failed_result is None:
                states[step.step_id] = WorkflowStepStatus.CANCELLED
                records.append(_record(step, WorkflowStepStatus.CANCELLED))
                status = "cancelled"
                records.extend(
                    _terminal_records(
                        workflow.steps[index + 1 :],
                        WorkflowStepStatus.CANCELLED,
                    )
                )
                break
            results[step.step_id] = failed_result
            step_status = (
                WorkflowStepStatus.COMPLETED
                if failed_result.status == "success"
                else WorkflowStepStatus.FAILED
            )
            states[step.step_id] = step_status
            records.append(
                _record(
                    step,
                    step_status,
                    error_code=failed_result.error_code,
                    execution_duration=failed_result.execution_duration,
                )
            )
            if step_status == WorkflowStepStatus.FAILED:
                status = "failed"
                records.extend(
                    _terminal_records(workflow.steps[index + 1 :], WorkflowStepStatus.BLOCKED)
                )
                break
            if self._clock() >= deadline:
                status = "time_limit"
                records.extend(
                    _terminal_records(workflow.steps[index + 1 :], WorkflowStepStatus.BLOCKED)
                )
                break

        entry = WorkflowHistoryEntry(
            run_id=run_id,
            workflow_name=workflow.name,
            status=status,
            started_at=started_at,
            duration=max(0.0, self._clock() - started),
            steps=tuple(records),
        )
        with self._lock:
            self._history.append(entry)
        return WorkflowRunResult(history_entry=entry, step_results=results)

    async def _dispatch_with_cancellation(
        self,
        request: dict[str, object],
        *,
        run_id: str,
        cancellation: Event | None,
    ) -> ToolResult | None:
        """Dispatch one call, permitting cooperative async tool cancellation.

        The dispatcher is still the sole authorization/execution boundary.  A
        synchronous tool cannot be interrupted safely while it is executing,
        but cancellation is observed before every next step; async tools are
        cancelled promptly.
        """
        if cancellation is None:
            return await self._dispatcher.dispatch(request, task_id=run_id)
        task = asyncio.create_task(
            self._dispatcher.dispatch(request, task_id=run_id)
        )
        while True:
            if cancellation.is_set():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
                return None
            try:
                return await asyncio.wait_for(
                    asyncio.shield(task),
                    timeout=0.05,
                )
            except asyncio.TimeoutError:
                continue

    def history(self, *, limit: int = 20) -> tuple[WorkflowHistoryEntry, ...]:
        """Eng oxirgi workflow bajarilishlarining maxfiy bo'lmagan tarixini qaytaradi."""
        if limit < 1:
            raise ValueError("history limiti kamida 1 bo'lishi kerak")
        with self._lock:
            return tuple(list(self._history)[-limit:])

    def _get_workflow(self, name: str) -> WorkflowDefinition:
        with self._lock:
            workflow = self._workflows.get(name)
        if workflow is None:
            raise WorkflowError("workflow ro'yxatdan o'tmagan")
        return workflow

    @staticmethod
    def _validate_inputs(
        workflow: WorkflowDefinition,
        inputs: Mapping[str, object],
    ) -> dict[str, object]:
        if not isinstance(inputs, Mapping) or any(
            not isinstance(key, str) for key in inputs
        ):
            raise WorkflowError("workflow input object yaroqsiz")
        try:
            encoded = json.dumps(
                inputs,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeError) as exc:
            raise WorkflowError("workflow input JSON ma'lumot bo'lishi kerak") from exc
        if len(encoded) > _MAX_WORKFLOW_INPUT_BYTES:
            raise WorkflowError("workflow input hajm limiti oshdi")
        errors = list(Draft202012Validator(workflow.input_schema).iter_errors(dict(inputs)))
        if errors:
            raise WorkflowError("workflow input sxemaga mos kelmadi")
        return dict(inputs)


def register_sample_workflows(engine: WorkflowEngine) -> None:
    """Uchta tayyor workflow'ni mavjud registry tool'lariga bog'laydi."""
    engine.register_many(sample_workflows())


def sample_workflows() -> tuple[WorkflowDefinition, ...]:
    """Tayyor, tool ruxsatlariga qo'shimcha bermaydigan workflow sxemalari."""
    organize = WorkflowDefinition(
        name="organize_downloads_preview",
        description="Downloads papkasini o'zgartirmasdan tartiblash rejasini ko'rsatish.",
        input_schema={
            "type": "object",
            "properties": {"source_directory": {"type": "string", "minLength": 1}},
            "required": ["source_directory"],
            "additionalProperties": False,
        },
        steps=[
            WorkflowStep(
                step_id="preview",
                tool_name="organize_directory",
                description="Fayllarni ko'chirmasdan tartiblash rejasini olish.",
                arguments={
                    "source_directory": InputArgument(source="input", name="source_directory"),
                    "apply": LiteralArgument(source="literal", value=False),
                },
            )
        ],
    )
    report = WorkflowDefinition(
        name="daily_system_report",
        description="Tizim, CPU, xotira, disk va tarmoq holatini ketma-ket yig'ish.",
        input_schema=dict(_DEFAULT_INPUT_SCHEMA),
        steps=[
            WorkflowStep(
                step_id="system",
                tool_name="get_system_status",
                description="Umumiy tizim holatini olish.",
            ),
            WorkflowStep(
                step_id="cpu",
                tool_name="get_cpu_details",
                description="CPU ko'rsatkichlarini olish.",
                arguments={"interval": LiteralArgument(source="literal", value=0.0)},
                depends_on=("system",),
            ),
            WorkflowStep(
                step_id="memory",
                tool_name="get_memory_details",
                description="RAM va swap ko'rsatkichlarini olish.",
                depends_on=("cpu",),
            ),
            WorkflowStep(
                step_id="disk",
                tool_name="get_disk_details",
                description="Disk ko'rsatkichlarini olish.",
                arguments={"limit": LiteralArgument(source="literal", value=20)},
                depends_on=("memory",),
            ),
            WorkflowStep(
                step_id="network",
                tool_name="get_network_interfaces",
                description="Tarmoq interfeysi ko'rsatkichlarini olish.",
                depends_on=("disk",),
            ),
        ],
    )
    document_properties = {
        "path": {"type": "string", "minLength": 1},
        "format": {"type": "string", "enum": ["pdf", "docx", "csv", "xlsx"]},
    }
    summarize = WorkflowDefinition(
        name="summarize_documents",
        description=(
            "Hujjat formatiga mos ro'yxatdagi reader tool orqali matn va "
            "strukturaviy ma'lumotni ajratib, agent xulosasiga tayyorlash."
        ),
        input_schema={
            "type": "object",
            "properties": document_properties,
            "required": ["path", "format"],
            "additionalProperties": False,
        },
        steps=[
            _document_read_step("pdf", "read_pdf"),
            _document_read_step("docx", "read_docx"),
            _document_read_step("csv", "read_csv"),
            _document_read_step("xlsx", "read_xlsx"),
        ],
    )
    return organize, report, summarize


def _document_read_step(document_format: str, tool_name: str) -> WorkflowStep:
    return WorkflowStep(
        step_id=f"read_{document_format}",
        tool_name=tool_name,
        description=f"{document_format.upper()} hujjat mazmunini xavfsiz ajratib olish.",
        arguments={"path": InputArgument(source="input", name="path")},
        condition=WorkflowCondition(
            source="input",
            input_name="format",
            operator="equals",
            value=document_format,
        ),
    )


def _resolve_arguments(
    step: WorkflowStep,
    inputs: Mapping[str, object],
    results: Mapping[str, ToolResult],
) -> dict[str, object]:
    resolved: dict[str, object] = {}
    for name, source in step.arguments.items():
        if isinstance(source, LiteralArgument):
            resolved[name] = copy.deepcopy(source.value)
        elif isinstance(source, InputArgument):
            if source.name not in inputs:
                raise WorkflowError("workflow input qiymati berilmagan")
            resolved[name] = inputs[source.name]
        else:
            previous = results.get(source.step_id)
            if previous is None or previous.status != "success":
                raise WorkflowError("workflow natija manbasi yaroqsiz")
            resolved[name] = _path_value(previous.result_data, source.path)
    return resolved


def _condition_matches(
    condition: WorkflowCondition,
    inputs: Mapping[str, object],
    results: Mapping[str, ToolResult],
) -> bool:
    if condition.source == "input":
        input_name = condition.input_name
        exists = input_name is not None and input_name in inputs
        value = inputs.get(input_name) if exists and input_name is not None else None
    else:
        result = results.get(condition.step_id or "")
        exists = result is not None and result.status == "success"
        value = result.result_data if exists and result is not None else None
        if exists:
            try:
                value = _path_value(value, condition.path)
            except WorkflowError:
                exists = False
    if condition.operator == "exists":
        return exists
    if not exists:
        return False
    if condition.operator == "truthy":
        return bool(value)
    if condition.operator == "equals":
        return value == condition.value
    return value != condition.value


def _path_value(value: object, path: tuple[str | int, ...]) -> object:
    current = value
    for part in path:
        if isinstance(current, Mapping):
            if not isinstance(part, str) or part not in current:
                raise WorkflowError("workflow natija maydoni topilmadi")
            current = current[part]
        elif isinstance(current, (list, tuple)):
            if (
                not isinstance(part, int)
                or isinstance(part, bool)
                or part < 0
                or part >= len(current)
            ):
                raise WorkflowError("workflow natija maydoni topilmadi")
            current = current[part]
        else:
            raise WorkflowError("workflow natija maydoni topilmadi")
    return current


def _terminal_records(
    steps: list[WorkflowStep],
    status: WorkflowStepStatus,
) -> list[WorkflowStepRecord]:
    return [_record(step, status) for step in steps]


def _record(
    step: WorkflowStep,
    status: WorkflowStepStatus,
    *,
    error_code: str | None = None,
    execution_duration: float = 0.0,
) -> WorkflowStepRecord:
    return WorkflowStepRecord(
        step_id=step.step_id,
        tool_name=step.tool_name,
        status=status,
        error_code=error_code,
        execution_duration=max(0.0, execution_duration),
    )


def _reject_remote_schema_references(value: object) -> None:
    if isinstance(value, Mapping):
        for keyword in ("$ref", "$dynamicRef", "$recursiveRef"):
            if keyword in value:
                reference = value[keyword]
                if not isinstance(reference, str) or not reference.startswith("#"):
                    raise WorkflowError(
                        "workflow sxemasida tashqi reference ishlatib bo'lmaydi"
                    )
        if "$id" in value:
            identifier = value["$id"]
            if not isinstance(identifier, str) or not identifier.startswith("#"):
                raise WorkflowError("workflow sxemasida tashqi reference ishlatib bo'lmaydi")
        for item in value.values():
            _reject_remote_schema_references(item)
    elif isinstance(value, list):
        for item in value:
            _reject_remote_schema_references(item)


def _ensure_json_value(value: object) -> None:
    try:
        json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, UnicodeError) as exc:
        raise WorkflowError("workflow ta'riflari faqat JSON qiymatlardan iborat bo'lishi kerak") from exc
