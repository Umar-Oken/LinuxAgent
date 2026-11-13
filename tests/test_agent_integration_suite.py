"""End-to-end agent scenarios using real registered tools and deterministic fakes."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal

from pypdf import PdfWriter

import agent as agent_module
from agent import create_tool_registry
from core.agent_controller import AgentController
from core.llm_client import ChatMessage, LLMResponse, ModelCapabilities, ToolCall
from core.tool_dispatcher import ToolDispatcher
from core.workflow_engine import WorkflowEngine, register_sample_workflows
from security.approval import (
    ApprovalManager,
    ApprovalRequest,
    ApprovalResponse,
    TrustedApprovalInterface,
)
from security.audit_logger import AuditLogger
from security.path_guard import PathGuard
from security.permissions import PermissionEngine, PermissionPolicy


class FakeLLMClient:
    """A finite, deterministic model transcript for integration tests."""

    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = responses
        self.calls: list[tuple[ChatMessage, ...]] = []

    def chat(
        self,
        history: Sequence[ChatMessage],
        **_kwargs: object,
    ) -> LLMResponse:
        self.calls.append(tuple(history))
        return self._responses.pop(0)

    def get_capabilities(self) -> ModelCapabilities:
        return ModelCapabilities(native_tool_calling=True, structured_json=True)


class FixedApproval(TrustedApprovalInterface):
    def __init__(self, decision: Literal["approve", "deny"] = "approve") -> None:
        self.decision = decision
        self.requests: list[ApprovalRequest] = []

    def request_approval(self, request: ApprovalRequest) -> ApprovalResponse:
        self.requests.append(request)
        return ApprovalResponse(approval_id=request.approval_id, decision=self.decision)


class FakeResponse:
    def __init__(self, payload: object) -> None:
        self._payload = payload

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")


def _tool_response(name: str, arguments: dict[str, Any]) -> LLMResponse:
    call = ToolCall(name=name, arguments=arguments)
    return LLMResponse(
        content="",
        tool_calls=[call],
        assistant_message=ChatMessage(role="assistant", content="", tool_calls=[call]),
    )


def _final_response(content: str) -> LLMResponse:
    return LLMResponse(
        content=content,
        assistant_message=ChatMessage(role="assistant", content=content),
    )


def _audit_events(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_find_read_and_save_approved_python_summary_with_audit(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
    document = tmp_path / "python-guide.md"
    document.write_text("Python integration guide", encoding="utf-8")
    audit_path = tmp_path / "audit.jsonl"
    audit = AuditLogger(audit_path)
    guard = PathGuard([tmp_path])
    registry = create_tool_registry(guard)
    approval = FixedApproval()
    dispatcher = ToolDispatcher(
        registry,
        permission_checker=PermissionEngine(
            PermissionPolicy(), ApprovalManager(approval, audit_logger=audit)
        ),
        audit_logger=audit,
    )
    responses = [
        _tool_response("search_files", {"query": "python", "folder": str(tmp_path)}),
        _tool_response("read_text_file", {"path": str(document)}),
        _tool_response("create_note", {"title": "summary", "content": "Tasdiqlangan xulosa"}),
        _final_response("Xulosa saqlandi."),
    ]
    controller = AgentController(
        client=FakeLLMClient(responses), registry=registry, dispatcher=dispatcher,
        path_guard=guard, system_prompt="integration test", output=lambda _message: None,
    )
    monkeypatch.setattr("builtins.input", lambda _prompt: "ha")
    try:
        result = controller.run_task("Python hujjatini top, o'qi va xulosani saqla")
        assert result.status == "completed"
        assert result.tool_calls == 3
        notes = list((tmp_path / "agent_notes").glob("*-summary.md"))
        assert len(notes) == 1
        assert notes[0].read_text(encoding="utf-8") == "Tasdiqlangan xulosa"
        assert len(approval.requests) == 1
        assert approval.requests[0].action.startswith("create_note ")
        events = _audit_events(audit_path)
        assert any(event["tool_name"] == "create_note" and event["execution_status"] == "success" for event in events)
        assert any(event["event_type"] == "approval" and event["execution_status"] == "consumed" for event in events)
    finally:
        guard.close()
        audit.close()


def test_discount_and_resource_checks_run_through_the_agent(tmp_path: Path) -> None:
    guard = PathGuard([tmp_path])
    registry = create_tool_registry(guard)
    dispatcher = ToolDispatcher(registry, permission_checker=PermissionEngine(PermissionPolicy()))
    client = FakeLLMClient([
        _tool_response("calculate", {"expression": "200 * (1 - 0.15)"}),
        _tool_response("get_memory_details", {}),
        _tool_response("get_disk_details", {"limit": 5}),
        _final_response("Chegirma: 170; RAM va disk holati tekshirildi."),
    ])
    controller = AgentController(
        client=client, registry=registry, dispatcher=dispatcher, path_guard=guard,
        system_prompt="integration test", output=lambda _message: None,
    )
    try:
        result = controller.run_task("15 foiz chegirma va resurslarni tekshir")
        assert result.status == "completed"
        assert result.tool_calls == 3
        assert any(
            "Natija: 170" in message.content
            for history in client.calls
            for message in history
            if message.role == "tool"
        )
    finally:
        guard.close()


def test_weather_is_mocked_and_agent_returns_a_recommendation(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    responses = iter((
        FakeResponse({"results": [{"latitude": 41.3, "longitude": 69.2}]}),
        FakeResponse({"current": {"temperature_2m": 32, "weather_code": 0}}),
    ))
    monkeypatch.setattr(agent_module, "urlopen", lambda *_args, **_kwargs: next(responses))
    guard = PathGuard([tmp_path])
    registry = create_tool_registry(guard)
    dispatcher = ToolDispatcher(registry, permission_checker=PermissionEngine(PermissionPolicy()))
    client = FakeLLMClient([
        _tool_response("get_weather", {"city": "Toshkent"}),
        _final_response("Issiq; suv olib yuring."),
    ])
    controller = AgentController(
        client=client, registry=registry, dispatcher=dispatcher, path_guard=guard,
        system_prompt="integration test", output=lambda _message: None,
    )
    try:
        result = controller.run_task("Ob-havoni olib, tavsiya ber")
        assert result.status == "completed"
        assert result.response == "Issiq; suv olib yuring."
        assert len(client.calls) == 2
    finally:
        guard.close()


def test_organize_files_requires_approval_and_writes_auditable_result(tmp_path: Path) -> None:
    source = tmp_path / "downloads"
    source.mkdir()
    (source / "photo.jpg").write_bytes(b"image")
    audit_path = tmp_path / "audit.jsonl"
    audit = AuditLogger(audit_path)
    approval = FixedApproval()
    guard = PathGuard([tmp_path])
    registry = create_tool_registry(guard)
    dispatcher = ToolDispatcher(
        registry,
        permission_checker=PermissionEngine(PermissionPolicy(), ApprovalManager(approval, audit_logger=audit)),
        audit_logger=audit,
    )
    try:
        preview = asyncio.run(dispatcher.dispatch({"name": "organize_directory", "arguments": {"source_directory": str(source), "apply": False}}))
        assert preview.status == "success"
        applied = asyncio.run(dispatcher.dispatch({"name": "organize_directory", "arguments": {"source_directory": str(source), "apply": True, "plan_id": preview.result_data["plan_id"]}}))
        assert applied.status == "success"
        assert (source / "images" / "photo.jpg").is_file()
        assert len(approval.requests) == 2
        events = _audit_events(audit_path)
        assert sum(event["tool_name"] == "organize_directory" for event in events) >= 2
    finally:
        guard.close()
        audit.close()


def test_pdf_read_and_project_analysis_run_with_fake_llm(tmp_path: Path) -> None:
    pdf = tmp_path / "report.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.write(pdf)
    project = tmp_path / "project"
    project.mkdir()
    (project / "main.py").write_text("def hello():\n    return 'hi'\n", encoding="utf-8")
    guard = PathGuard([tmp_path])
    registry = create_tool_registry(guard)
    dispatcher = ToolDispatcher(registry, permission_checker=PermissionEngine(PermissionPolicy()))
    client = FakeLLMClient([
        _tool_response("read_pdf", {"path": str(pdf)}),
        _tool_response("inspect_python_project", {"path": str(project)}),
        _final_response("PDF xulosasi va loyiha tahlili tayyor."),
    ])
    controller = AgentController(
        client=client, registry=registry, dispatcher=dispatcher, path_guard=guard,
        system_prompt="integration test", output=lambda _message: None,
    )
    try:
        result = controller.run_task("PDFni o'qi, xulosa qil va Python loyihasini tahlil qil")
        assert result.status == "completed"
        assert result.tool_calls == 2
        assert result.response == "PDF xulosasi va loyiha tahlili tayyor."
    finally:
        guard.close()


def test_multi_step_workflow_runs_sequentially_with_registered_tools(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(
        agent_module,
        "get_network_interfaces",
        lambda: {"interfaces": [], "summary": "Mock tarmoq holati."},
    )
    guard = PathGuard([tmp_path])
    registry = create_tool_registry(guard)
    dispatcher = ToolDispatcher(registry, permission_checker=PermissionEngine(PermissionPolicy()))
    engine = WorkflowEngine(registry, dispatcher)
    try:
        register_sample_workflows(engine)
        result = asyncio.run(engine.run("daily_system_report"))
        assert result.history_entry.status == "completed"
        assert [record.step_id for record in result.history_entry.steps] == ["system", "cpu", "memory", "disk", "network"]
        assert all(record.status == "completed" for record in result.history_entry.steps)
    finally:
        guard.close()


def test_limits_denial_and_dispatch_errors_are_reported_and_audited(tmp_path: Path) -> None:
    audit_path = tmp_path / "audit.jsonl"
    audit = AuditLogger(audit_path)
    guard = PathGuard([tmp_path])
    registry = create_tool_registry(guard)
    approval = FixedApproval("deny")
    dispatcher = ToolDispatcher(
        registry,
        permission_checker=PermissionEngine(PermissionPolicy(), ApprovalManager(approval, audit_logger=audit)),
        audit_logger=audit,
    )
    client = FakeLLMClient([
        _tool_response("create_note", {"title": "blocked", "content": "must not write"}),
        _tool_response("calculate", {"expression": "1 + 1"}),
    ])
    controller = AgentController(
        client=client, registry=registry, dispatcher=dispatcher, path_guard=guard,
        system_prompt="integration test", max_steps=1, max_tool_calls=1,
        output=lambda _message: None,
    )
    try:
        result = controller.run_task("Rad etilgan yozish va limit tekshiruvi")
        assert result.status == "step_limit"
        assert result.tool_calls == 1
        assert not (tmp_path / "agent_notes").exists()
        events = _audit_events(audit_path)
        assert any(event["tool_name"] == "create_note" and event["execution_status"] == "denied" for event in events)
        assert approval.requests
    finally:
        guard.close()
        audit.close()
