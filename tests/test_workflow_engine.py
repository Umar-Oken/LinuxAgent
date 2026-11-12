"""Deklarativ workflow bajarilishi va ruxsat chegaralari sinovlari."""

import asyncio
from pathlib import Path
from threading import Event
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

from core.tool_dispatcher import ToolDispatcher
from core.tool_registry import ToolDefinition, ToolRegistry
from core.workflow_engine import (
    InputArgument,
    LiteralArgument,
    ResultArgument,
    WorkflowCondition,
    WorkflowDefinition,
    WorkflowEngine,
    WorkflowError,
    WorkflowStep,
    WorkflowStepStatus,
    register_sample_workflows,
    sample_workflows,
)
from security.path_guard import PathGuard


class FirstArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    text: str


class SecondArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    text: str


class InputArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    value: str


class EmptyArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class OrganizeArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    source_directory: str
    apply: bool = False


class CpuArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    interval: float = 0.1


class DiskArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    limit: int = 50


class DocumentArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    path: str


class AllowPermission:
    def is_allowed(self, _tool: ToolDefinition, _arguments: dict[str, Any]) -> bool:
        return True


class DenyPermission:
    def is_allowed(self, _tool: ToolDefinition, _arguments: dict[str, Any]) -> bool:
        return False


def _tool(
    name: str,
    argument_model: type[BaseModel],
    implementation: Any,
    *,
    permission_level: str = "READ_ONLY",
) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=f"{name} test tool",
        category="system",
        argument_schema=argument_model.model_json_schema(),
        argument_model=argument_model,
        permission_level=permission_level,  # type: ignore[arg-type]
        execution_timeout=1,
        implementation=implementation,
    )


def _engine(
    tmp_path: Path,
    *,
    denied: bool = False,
) -> tuple[WorkflowEngine, PathGuard, list[tuple[str, dict[str, object]]]]:
    called: list[tuple[str, dict[str, object]]] = []
    registry = ToolRegistry()

    def produce(text: str) -> dict[str, object]:
        called.append(("produce", {"text": text}))
        return {"accepted": True, "text": text}

    def consume(text: str) -> dict[str, object]:
        called.append(("consume", {"text": text}))
        return {"summary": f"summary:{text}"}

    def report(value: str) -> dict[str, object]:
        called.append(("report", {"value": value}))
        return {"reported": value}

    registry.register(_tool("produce", FirstArguments, produce))
    registry.register(_tool("consume", SecondArguments, consume))
    registry.register(_tool("report", InputArguments, report, permission_level="CONTROLLED"))
    guard = PathGuard([tmp_path])
    dispatcher = ToolDispatcher(
        registry,
        permission_checker=DenyPermission() if denied else AllowPermission(),
    )
    return WorkflowEngine(registry, dispatcher), guard, called


def _sequential_workflow(*, condition_value: bool = True) -> WorkflowDefinition:
    return WorkflowDefinition(
        name="sequential_test",
        description="Natijani keyingi tool'ga uzatish.",
        input_schema={
            "type": "object",
            "properties": {"text": {"type": "string", "minLength": 1}},
            "required": ["text"],
            "additionalProperties": False,
        },
        steps=[
            WorkflowStep(
                step_id="produce",
                tool_name="produce",
                description="Ma'lumot tayyorlash.",
                arguments={"text": InputArgument(source="input", name="text")},
            ),
            WorkflowStep(
                step_id="consume",
                tool_name="consume",
                description="Oldingi natijani qayta ishlash.",
                arguments={
                    "text": ResultArgument(
                        source="result",
                        step_id="produce",
                        path=("text",),
                    )
                },
                depends_on=("produce",),
                condition=WorkflowCondition(
                    source="result",
                    step_id="produce",
                    path=("accepted",),
                    operator="equals",
                    value=condition_value,
                ),
            ),
        ],
    )


def test_preview_is_declarative_and_does_not_execute_tools(tmp_path: Path) -> None:
    engine, guard, called = _engine(tmp_path)
    try:
        engine.register(_sequential_workflow())
        preview = engine.preview("sequential_test", {"text": "maxfiy matn"})
        assert [step.tool_name for step in preview.steps] == ["produce", "consume"]
        assert preview.steps[0].arguments == {"text": "maxfiy matn"}
        assert preview.steps[1].arguments == {"text": "<produce natijasidan olinadi>"}
        assert all(step.conditional for step in preview.steps[1:])
        assert called == []
        assert engine.history() == ()
    finally:
        guard.close()


def test_runs_steps_sequentially_and_records_history(tmp_path: Path) -> None:
    engine, guard, called = _engine(tmp_path)
    try:
        engine.register(_sequential_workflow())
        result = asyncio.run(engine.run("sequential_test", {"text": "hello"}))
        assert result.history_entry.status == "completed"
        assert [step.status for step in result.history_entry.steps] == [
            WorkflowStepStatus.COMPLETED,
            WorkflowStepStatus.COMPLETED,
        ]
        assert called == [
            ("produce", {"text": "hello"}),
            ("consume", {"text": "hello"}),
        ]
        assert result.step_results["consume"].result_data == {"summary": "summary:hello"}
        assert engine.history() == (result.history_entry,)
        assert "hello" not in str(engine.history()[0].model_dump())
    finally:
        guard.close()


def test_structured_result_condition_skips_nonmatching_step(tmp_path: Path) -> None:
    engine, guard, called = _engine(tmp_path)
    try:
        engine.register(_sequential_workflow(condition_value=False))
        result = asyncio.run(engine.run("sequential_test", {"text": "hello"}))
        assert result.history_entry.status == "completed"
        assert [step.status for step in result.history_entry.steps] == [
            WorkflowStepStatus.COMPLETED,
            WorkflowStepStatus.SKIPPED,
        ]
        assert called == [("produce", {"text": "hello"})]
        assert "consume" not in result.step_results
    finally:
        guard.close()


def test_invalid_workflow_and_unknown_tool_are_rejected(tmp_path: Path) -> None:
    engine, guard, _called = _engine(tmp_path)
    try:
        unknown = WorkflowDefinition(
            name="unknown_tool",
            description="Noma'lum tool.",
            steps=[
                WorkflowStep(
                    step_id="one",
                    tool_name="not_registered",
                    description="Noma'lum tool.",
                )
            ],
        )
        with pytest.raises(WorkflowError, match="ro'yxatdan o'tgan"):
            engine.register(unknown)

        external_schema = WorkflowDefinition(
            name="external_schema",
            description="Tashqi reference ishlatmasligi kerak.",
            input_schema={
                "type": "object",
                "properties": {"value": {"$ref": "https://example.invalid/schema"}},
            },
            steps=[
                WorkflowStep(
                    step_id="one",
                    tool_name="produce",
                    description="Ishlatilmaydi.",
                )
            ],
        )
        with pytest.raises(WorkflowError, match="reference"):
            engine.register(external_schema)

        forward_dependency = WorkflowDefinition(
            name="forward_dependency",
            description="Oldinga bog'liqlik yaroqsiz.",
            steps=[
                WorkflowStep(
                    step_id="one",
                    tool_name="produce",
                    description="Oldingi bosqichga bog'lanmagan.",
                    depends_on=("later",),
                ),
                WorkflowStep(
                    step_id="later",
                    tool_name="consume",
                    description="Keyingi bosqich.",
                ),
            ],
        )
        with pytest.raises(WorkflowError, match="oldin e'lon"):
            engine.register(forward_dependency)

        duplicate_step = WorkflowDefinition(
            name="duplicate_step",
            description="Takrorlangan identifikator.",
            steps=[
                WorkflowStep(
                    step_id="one",
                    tool_name="produce",
                    description="Birinchi bosqich.",
                    arguments={"text": LiteralArgument(source="literal", value="one")},
                ),
                WorkflowStep(
                    step_id="one",
                    tool_name="consume",
                    description="Ikkinchi bosqich.",
                    arguments={"text": LiteralArgument(source="literal", value="two")},
                ),
            ],
        )
        with pytest.raises(WorkflowError, match="takrorlanmasligi"):
            engine.register(duplicate_step)
    finally:
        guard.close()


def test_invalid_input_and_expression_like_argument_are_not_run(tmp_path: Path) -> None:
    engine, guard, called = _engine(tmp_path)
    try:
        engine.register(_sequential_workflow())
        with pytest.raises(WorkflowError, match="input sxemaga"):
            asyncio.run(engine.run("sequential_test", {"text": 12}))
        with pytest.raises(WorkflowError, match="ro'yxatdan o'tmagan"):
            engine.preview("missing_workflow")
        assert called == []
    finally:
        guard.close()


def test_permission_denial_is_honored_without_running_tool(tmp_path: Path) -> None:
    engine, guard, called = _engine(tmp_path, denied=True)
    workflow = WorkflowDefinition(
        name="controlled",
        description="Ruxsat talab qiladigan tool.",
        steps=[
            WorkflowStep(
                step_id="report",
                tool_name="report",
                description="Nazorat qilinadigan hisobot.",
                arguments={
                    "value": LiteralArgument(source="literal", value="data"),
                },
            )
        ],
    )
    try:
        engine.register(workflow)
        result = asyncio.run(engine.run("controlled"))
        assert result.history_entry.status == "failed"
        assert result.history_entry.steps[0].error_code == "PERMISSION_DENIED"
        assert called == []
    finally:
        guard.close()


def test_registration_does_not_enable_a_disabled_tool(tmp_path: Path) -> None:
    registry = ToolRegistry()
    called = False

    def disabled_tool() -> dict[str, bool]:
        nonlocal called
        called = True
        return {"ran": True}

    registry.register(_tool("disabled_tool", EmptyArguments, disabled_tool), enabled=False)
    engine = WorkflowEngine(
        registry,
        ToolDispatcher(registry, permission_checker=AllowPermission()),
    )
    engine.register(
        WorkflowDefinition(
            name="disabled_tool_workflow",
            description="Disabled tool ruxsatini oshirmaydi.",
            steps=[
                WorkflowStep(
                    step_id="disabled",
                    tool_name="disabled_tool",
                    description="Bajarilmasligi kerak.",
                )
            ],
        )
    )
    result = asyncio.run(engine.run("disabled_tool_workflow"))
    assert result.history_entry.status == "failed"
    assert result.history_entry.steps[0].error_code == "TOOL_DISABLED"
    assert called is False


def test_cancellation_stops_before_first_tool_and_is_recorded(tmp_path: Path) -> None:
    engine, guard, called = _engine(tmp_path)
    cancellation = Event()
    cancellation.set()
    try:
        engine.register(_sequential_workflow())
        result = asyncio.run(
            engine.run(
                "sequential_test",
                {"text": "hello"},
                cancellation=cancellation,
            )
        )
        assert result.history_entry.status == "cancelled"
        assert all(
            step.status == WorkflowStepStatus.CANCELLED
            for step in result.history_entry.steps
        )
        assert called == []
        assert engine.history()[0] == result.history_entry
    finally:
        guard.close()


def test_cancellation_interrupts_a_running_async_tool(tmp_path: Path) -> None:
    registry = ToolRegistry()
    started = asyncio.Event()

    async def wait_forever() -> dict[str, bool]:
        started.set()
        await asyncio.Event().wait()
        return {"finished": True}

    registry.register(_tool("wait_forever", EmptyArguments, wait_forever))
    engine = WorkflowEngine(
        registry,
        ToolDispatcher(registry, permission_checker=AllowPermission()),
    )
    engine.register(
        WorkflowDefinition(
            name="interruptible",
            description="Bekor qilinadigan async bosqich.",
            steps=[
                WorkflowStep(
                    step_id="wait",
                    tool_name="wait_forever",
                    description="Kutish.",
                ),
                WorkflowStep(
                    step_id="after",
                    tool_name="wait_forever",
                    description="Bajarilmasligi kerak.",
                ),
            ],
        )
    )
    cancellation = Event()

    async def run_and_cancel() -> object:
        run = asyncio.create_task(engine.run("interruptible", cancellation=cancellation))
        await asyncio.wait_for(started.wait(), timeout=1)
        cancellation.set()
        return await asyncio.wait_for(run, timeout=1)

    result = asyncio.run(run_and_cancel())
    assert result.history_entry.status == "cancelled"
    assert [step.status for step in result.history_entry.steps] == [
        WorkflowStepStatus.CANCELLED,
        WorkflowStepStatus.CANCELLED,
    ]


def test_registered_workflow_list_returns_safe_definition_copies(tmp_path: Path) -> None:
    engine, guard, _called = _engine(tmp_path)
    try:
        engine.register(_sequential_workflow())
        listed = engine.list_workflows()
        assert [workflow.name for workflow in listed] == ["sequential_test"]
        listed[0].input_schema["properties"]["text"]["minLength"] = 999
        assert engine.preview("sequential_test", {"text": "hello"}).workflow_name == "sequential_test"
    finally:
        guard.close()


def test_register_many_is_atomic_when_a_definition_is_invalid(tmp_path: Path) -> None:
    engine, guard, _called = _engine(tmp_path)
    try:
        valid = _sequential_workflow()
        invalid = WorkflowDefinition(
            name="missing_tool",
            description="Mavjud bo'lmagan tool.",
            steps=[
                WorkflowStep(
                    step_id="missing",
                    tool_name="not_registered",
                    description="Bajarilmasligi kerak.",
                )
            ],
        )
        with pytest.raises(WorkflowError, match="ro'yxatdan o'tgan"):
            engine.register_many((valid, invalid))
        assert engine.list_workflows() == ()
    finally:
        guard.close()


def test_sample_registration_is_atomic_when_a_required_tool_is_missing() -> None:
    registry = ToolRegistry()
    registry.register(_tool("organize_directory", OrganizeArguments, lambda **_kwargs: {}))
    engine = WorkflowEngine(
        registry,
        ToolDispatcher(registry, permission_checker=AllowPermission()),
    )
    with pytest.raises(WorkflowError, match="ro'yxatdan o'tgan"):
        register_sample_workflows(engine)
    assert engine.list_workflows() == ()


def test_sample_workflows_are_registered_and_previewable(tmp_path: Path) -> None:
    registry = ToolRegistry()
    sample_models: dict[str, type[BaseModel]] = {
        "organize_directory": OrganizeArguments,
        "get_system_status": EmptyArguments,
        "get_cpu_details": CpuArguments,
        "get_memory_details": EmptyArguments,
        "get_disk_details": DiskArguments,
        "get_network_interfaces": EmptyArguments,
        "read_pdf": DocumentArguments,
        "read_docx": DocumentArguments,
        "read_csv": DocumentArguments,
        "read_xlsx": DocumentArguments,
    }
    for name, argument_model in sample_models.items():
        registry.register(_tool(name, argument_model, lambda **_kwargs: "ok"))
    guard = PathGuard([tmp_path])
    engine = WorkflowEngine(registry, ToolDispatcher(registry, permission_checker=AllowPermission()))
    try:
        register_sample_workflows(engine)
        assert {workflow.name for workflow in sample_workflows()} == {
            "organize_downloads_preview",
            "daily_system_report",
            "summarize_documents",
        }
        organize = engine.preview(
            "organize_downloads_preview",
            {"source_directory": str(tmp_path)},
        )
        assert organize.steps[0].arguments["apply"] is False
        report = engine.preview("daily_system_report")
        assert [step.tool_name for step in report.steps] == [
            "get_system_status",
            "get_cpu_details",
            "get_memory_details",
            "get_disk_details",
            "get_network_interfaces",
        ]
        documents = engine.preview(
            "summarize_documents",
            {"path": "report.csv", "format": "csv"},
        )
        assert len(documents.steps) == 4
        assert all(step.conditional for step in documents.steps)
    finally:
        guard.close()


def test_organize_preview_sample_never_moves_files(tmp_path: Path) -> None:
    import agent as agent_module

    source = tmp_path / "Downloads"
    source.mkdir()
    photo = source / "photo.jpg"
    photo.write_bytes(b"image")
    guard = PathGuard([tmp_path])
    registry = agent_module.create_tool_registry(guard)
    engine = WorkflowEngine(
        registry,
        ToolDispatcher(registry, permission_checker=AllowPermission()),
    )
    try:
        register_sample_workflows(engine)
        result = asyncio.run(
            engine.run(
                "organize_downloads_preview",
                {"source_directory": str(source)},
            )
        )
        assert result.history_entry.status == "completed"
        assert result.step_results["preview"].result_data["status"] == "preview"
        assert photo.is_file()
        assert not (source / "images" / "photo.jpg").exists()
    finally:
        guard.close()


def test_summarize_documents_sample_reads_only_selected_format(
    tmp_path: Path,
) -> None:
    import agent as agent_module

    document = tmp_path / "records.csv"
    document.write_text("name\nali\n", encoding="utf-8")
    guard = PathGuard([tmp_path])
    registry = agent_module.create_tool_registry(guard)
    engine = WorkflowEngine(
        registry,
        ToolDispatcher(registry, permission_checker=AllowPermission()),
    )
    try:
        register_sample_workflows(engine)
        result = asyncio.run(
            engine.run(
                "summarize_documents",
                {"path": str(document), "format": "csv"},
            )
        )
        assert result.history_entry.status == "completed"
        assert [
            (step.tool_name, step.status)
            for step in result.history_entry.steps
        ] == [
            ("read_pdf", WorkflowStepStatus.SKIPPED),
            ("read_docx", WorkflowStepStatus.SKIPPED),
            ("read_csv", WorkflowStepStatus.COMPLETED),
            ("read_xlsx", WorkflowStepStatus.SKIPPED),
        ]
        assert result.step_results["read_csv"].result_data["untrusted_content"] is True
    finally:
        guard.close()
