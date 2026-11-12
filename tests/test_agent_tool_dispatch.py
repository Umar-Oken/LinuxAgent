"""Tests that the legacy tools remain reachable through the guarded dispatcher."""

import asyncio
import builtins
import json
import os
from pathlib import Path
from typing import Any

import pytest

from agent import create_tool_registry
from core.tool_dispatcher import ToolDispatcher, ToolResult
from core.tool_registry import ToolDefinition
from security.path_guard import PathGuard
from security.approval import (
    ApprovalManager,
    ApprovalRequest,
    ApprovalResponse,
    TrustedApprovalInterface,
)
from security.permissions import PermissionEngine, PermissionPolicy


class AllowPermissionChecker:
    def is_allowed(
        self,
        tool: ToolDefinition,
        arguments: dict[str, Any],
    ) -> bool:
        return True


def _dispatcher(guard: PathGuard) -> ToolDispatcher:
    return ToolDispatcher(
        create_tool_registry(guard),
        permission_checker=AllowPermissionChecker(),
    )


def _dispatch(
    dispatcher: ToolDispatcher,
    name: str,
    arguments: dict[str, Any],
) -> ToolResult:
    return asyncio.run(
        dispatcher.dispatch({"name": name, "arguments": arguments})
    )


def test_calculate_and_system_status_run_through_dispatcher(tmp_path: Path) -> None:
    guard = PathGuard([tmp_path])
    dispatcher = _dispatcher(guard)
    try:
        calculated = _dispatch(
            dispatcher,
            "calculate",
            {"expression": "9 * 5"},
        )
        status = _dispatch(dispatcher, "get_system_status", {})

        assert calculated.status == "success"
        assert calculated.result_data == "Natija: 45"
        assert status.status == "success"
        assert isinstance(status.result_data, str)
    finally:
        guard.close()


def test_file_tools_are_guarded_and_preserve_existing_behavior(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    source = allowed / "sample.txt"
    source.write_text("hello from the guarded file", encoding="utf-8")
    guard = PathGuard([allowed])
    dispatcher = _dispatcher(guard)
    try:
        read_result = _dispatch(
            dispatcher,
            "read_text_file",
            {"path": str(source)},
        )
        find_result = _dispatch(
            dispatcher,
            "search_files",
            {"folder": str(allowed), "query": "sample", "ext": ".txt"},
        )
        denied = _dispatch(
            dispatcher,
            "read_text_file",
            {"path": str(tmp_path / "outside.txt")},
        )

        assert read_result.status == "success"
        assert read_result.result_data == "hello from the guarded file"
        assert find_result.status == "success"
        assert "sample.txt" in str(find_result.result_data)
        assert denied.status == "error"
        assert str(tmp_path) not in denied.safe_error_message
    finally:
        guard.close()


def test_save_note_requires_confirmation_and_writes_only_under_allowed_root(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    guard = PathGuard([tmp_path])
    dispatcher = _dispatcher(guard)
    confirmations = iter(("ha",))
    monkeypatch.setattr(builtins, "input", lambda _prompt: next(confirmations))
    try:
        result = _dispatch(
            dispatcher,
            "create_note",
            {"title": "test", "content": "remember"},
        )

        assert result.status == "success"
        note_files = list((tmp_path / "agent_notes").glob("*.md"))
        assert len(note_files) == 1
        assert note_files[0].read_text(encoding="utf-8") == "remember"
    finally:
        guard.close()


def test_weather_tool_uses_registered_arguments(monkeypatch: Any, tmp_path: Path) -> None:
    timeouts: list[float] = []

    class FakeResponse:
        def __init__(self, value: Any) -> None:
            self.value = value

        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(self.value).encode()

    responses = iter(
        (
            FakeResponse(
                {"results": [{"latitude": 51.5, "longitude": -0.1}]}
            ),
            FakeResponse({"current": {"temperature_2m": 16}}),
        )
    )
    def fake_urlopen(*_args: Any, **kwargs: Any) -> FakeResponse:
        timeouts.append(kwargs["timeout"])
        return next(responses)

    monkeypatch.setattr("agent.urlopen", fake_urlopen)
    guard = PathGuard([tmp_path])
    dispatcher = _dispatcher(guard)
    try:
        result = _dispatch(dispatcher, "get_weather", {"city": "London"})
        assert result.status == "success"
        assert "London" in str(result.result_data)
        assert timeouts == [10, 10]
    finally:
        guard.close()


def test_web_tool_dispatches_through_registry_without_approval(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        "agent.fetch_webpage",
        lambda url, **_kwargs: {
            "source_url": url,
            "text": "public page text",
            "summary": "page fetched",
        },
    )
    guard = PathGuard([tmp_path])
    dispatcher = _dispatcher(guard)
    try:
        result = _dispatch(
            dispatcher,
            "fetch_webpage",
            {"url": "https://public.example/page"},
        )
        assert result.status == "success"
        assert result.result_data["text"] == "public page text"
    finally:
        guard.close()


def test_registry_exposes_exact_six_typed_tools_and_permissions(
    tmp_path: Path,
) -> None:
    guard = PathGuard([tmp_path])
    try:
        definitions = {
            definition.name: definition
            for definition in create_tool_registry(guard).list_tools()
        }
        assert set(definitions) == {
            "search_files",
            "list_directory",
            "search_files_advanced",
            "get_file_metadata",
            "find_large_files",
            "copy_file",
            "move_file",
            "rename_file",
            "create_directory",
            "organize_directory",
            "find_duplicate_files",
            "create_archive",
            "extract_archive",
            "read_text_file",
            "read_pdf",
            "read_docx",
            "read_csv",
            "read_xlsx",
            "create_markdown_document",
            "create_docx_document",
            "create_csv_document",
            "create_xlsx_document",
            "inspect_python_project",
            "search_source_code",
            "git_status",
            "git_diff",
            "create_note",
            "calculate",
            "get_system_status",
            "get_cpu_details",
            "get_memory_details",
            "get_disk_details",
            "list_processes",
            "get_network_interfaces",
            "check_internet_connectivity",
            "inspect_process",
            "get_system_uptime",
            "get_weather",
            "fetch_webpage",
            "summarize_webpage",
            "get_exchange_rate",
            "check_website_status",
        }
        assert definitions["create_note"].permission_level == "CONTROLLED"
        assert all(
            definitions[name].permission_level == "CONTROLLED"
            for name in (
                "copy_file",
                "move_file",
                "rename_file",
                "create_directory",
                "organize_directory",
                "create_archive",
                "extract_archive",
                "create_markdown_document",
                "create_docx_document",
                "create_csv_document",
                "create_xlsx_document",
            )
        )
        assert all(
            definitions[name].permission_level == "READ_ONLY"
            for name in (
                "get_cpu_details",
                "get_memory_details",
                "get_disk_details",
                "list_processes",
                "get_network_interfaces",
                "check_internet_connectivity",
                "inspect_process",
                "get_system_uptime",
                "fetch_webpage",
                "summarize_webpage",
                "get_exchange_rate",
                "check_website_status",
                "read_pdf",
                "read_docx",
                "read_csv",
                "read_xlsx",
                "inspect_python_project",
                "search_source_code",
                "git_status",
                "git_diff",
            )
        )
        assert all(
            definitions[name].argument_model.model_config.get("extra") == "forbid"
            for name in definitions
        )
    finally:
        guard.close()


def test_create_note_requires_trusted_approval_before_writing(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    class Approve(TrustedApprovalInterface):
        def request_approval(self, request: ApprovalRequest) -> ApprovalResponse:
            return ApprovalResponse(
                approval_id=request.approval_id,
                decision="approve",
            )

    guard = PathGuard([tmp_path])
    registry = create_tool_registry(guard)
    dispatcher = ToolDispatcher(
        registry,
        permission_checker=PermissionEngine(
            PermissionPolicy(),
            ApprovalManager(Approve()),
        ),
    )
    confirmations = iter(("ha",))
    monkeypatch.setattr("builtins.input", lambda _prompt: next(confirmations))
    try:
        result = _dispatch(
            dispatcher,
            "create_note",
            {"title": "approved", "content": "remember"},
        )
        assert result.status == "success"
        assert len(list((tmp_path / "agent_notes").glob("*.md"))) == 1
    finally:
        guard.close()


def test_controlled_file_operations_copy_move_rename_and_create_directory(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.txt"
    source.write_text("important data", encoding="utf-8")
    guard = PathGuard([tmp_path])
    registry = create_tool_registry(guard)
    class Approve(TrustedApprovalInterface):
        def request_approval(self, request: ApprovalRequest) -> ApprovalResponse:
            return ApprovalResponse(
                approval_id=request.approval_id,
                decision="approve",
            )

    dispatcher = ToolDispatcher(
        registry,
        permission_checker=PermissionEngine(
            PermissionPolicy(),
            ApprovalManager(Approve()),
        ),
    )
    try:
        copied = _dispatch(
            dispatcher,
            "copy_file",
            {"source": str(source), "destination": str(tmp_path / "copy.txt")},
        )
        assert copied.status == "success"
        assert copied.result_data["status"] == "success"
        assert source.read_text(encoding="utf-8") == "important data"
        assert (tmp_path / "copy.txt").read_text(encoding="utf-8") == "important data"

        moved = _dispatch(
            dispatcher,
            "move_file",
            {"source": str(tmp_path / "copy.txt"), "destination": str(tmp_path / "moved.txt")},
        )
        assert moved.status == "success"
        assert not (tmp_path / "copy.txt").exists()
        assert (tmp_path / "moved.txt").read_text(encoding="utf-8") == "important data"

        renamed = _dispatch(
            dispatcher,
            "rename_file",
            {"source": str(tmp_path / "moved.txt"), "destination": str(tmp_path / "renamed.txt")},
        )
        assert renamed.status == "success"
        assert not (tmp_path / "moved.txt").exists()

        created = _dispatch(
            dispatcher,
            "create_directory",
            {"path": str(tmp_path / "new-folder")},
        )
        assert created.status == "success"
        assert (tmp_path / "new-folder").is_dir()
    finally:
        guard.close()


def test_file_operations_reject_existing_destination_without_overwrite(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.txt"
    destination = tmp_path / "destination.txt"
    source.write_text("source", encoding="utf-8")
    destination.write_text("keep this", encoding="utf-8")
    guard = PathGuard([tmp_path])
    dispatcher = _dispatcher(guard)
    try:
        result = _dispatch(
            dispatcher,
            "copy_file",
            {"source": str(source), "destination": str(destination)},
        )
        assert result.status == "error"
        assert result.error_code == "DESTINATION_EXISTS"
        assert source.read_text(encoding="utf-8") == "source"
        assert destination.read_text(encoding="utf-8") == "keep this"
    finally:
        guard.close()


def test_controlled_file_operation_denial_does_not_change_files(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.txt"
    source.write_text("source", encoding="utf-8")
    guard = PathGuard([tmp_path])
    dispatcher = ToolDispatcher(
        create_tool_registry(guard),
        permission_checker=PermissionEngine(
            PermissionPolicy(),
            ApprovalManager(),
        ),
    )
    try:
        result = _dispatch(
            dispatcher,
            "move_file",
            {"source": str(source), "destination": str(tmp_path / "moved.txt")},
        )
        assert result.status == "denied"
        assert source.read_text(encoding="utf-8") == "source"
        assert not (tmp_path / "moved.txt").exists()
    finally:
        guard.close()


@pytest.mark.parametrize("tool_name", ["copy_file", "move_file"])
def test_partial_file_operation_preserves_source_and_cleans_destination(
    tmp_path: Path,
    monkeypatch: Any,
    tool_name: str,
) -> None:
    source = tmp_path / "source.txt"
    destination = tmp_path / "destination.txt"
    source.write_text("source data", encoding="utf-8")
    guard = PathGuard([tmp_path])
    dispatcher = _dispatcher(guard)
    original_write = os.write

    def fail_write(fd: int, data: Any) -> int:
        raise OSError("private filesystem failure")

    monkeypatch.setattr("agent.os.write", fail_write)
    try:
        result = _dispatch(
            dispatcher,
            tool_name,
            {"source": str(source), "destination": str(destination)},
        )
        assert result.status == "error"
        assert result.error_code == "OPERATION_FAILED"
        assert source.read_text(encoding="utf-8") == "source data"
        assert not destination.exists()
    finally:
        monkeypatch.setattr("agent.os.write", original_write)
        guard.close()


def test_file_operation_rejects_destination_outside_allowed_roots(
    tmp_path: Path,
) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    source = allowed / "source.txt"
    source.write_text("source", encoding="utf-8")
    destination = tmp_path / "outside.txt"
    guard = PathGuard([allowed])
    dispatcher = _dispatcher(guard)
    try:
        result = _dispatch(
            dispatcher,
            "copy_file",
            {"source": str(source), "destination": str(destination)},
        )
        assert result.status == "error"
        assert source.read_text(encoding="utf-8") == "source"
        assert not destination.exists()
    finally:
        guard.close()


def test_controlled_operation_runs_through_agent_controller(
    tmp_path: Path,
) -> None:
    from collections.abc import Sequence

    from core.agent_controller import AgentController
    from core.llm_client import (
        ChatMessage,
        LLMResponse,
        ModelCapabilities,
        ToolCall,
        ToolDefinition as LLMToolDefinition,
    )

    class Client:
        def __init__(self) -> None:
            call = ToolCall(
                name="create_directory",
                arguments={"path": str(tmp_path / "created-by-agent")},
            )
            self.responses = [
                LLMResponse(
                    content="",
                    tool_calls=[call],
                    assistant_message=ChatMessage(
                        role="assistant",
                        content="",
                        tool_calls=[call],
                    ),
                ),
                LLMResponse(
                    content="Katalog yaratildi.",
                    assistant_message=ChatMessage(
                        role="assistant",
                        content="Katalog yaratildi.",
                    ),
                ),
            ]
            self.tools: list[str] = []

        def chat(
            self,
            history: Sequence[ChatMessage],
            *,
            system_prompt: str | None = None,
            tools: Sequence[LLMToolDefinition] = (),
        ) -> LLMResponse:
            self.tools = [tool.name for tool in tools]
            return self.responses.pop(0)

        def get_capabilities(self) -> ModelCapabilities:
            raise NotImplementedError

    class Approve(TrustedApprovalInterface):
        def request_approval(self, request: ApprovalRequest) -> ApprovalResponse:
            return ApprovalResponse(
                approval_id=request.approval_id,
                decision="approve",
            )

    guard = PathGuard([tmp_path])
    registry = create_tool_registry(guard)
    dispatcher = ToolDispatcher(
        registry,
        permission_checker=PermissionEngine(
            PermissionPolicy(),
            ApprovalManager(Approve()),
        ),
    )
    client = Client()
    controller = AgentController(
        client=client,
        registry=registry,
        dispatcher=dispatcher,
        path_guard=guard,
        system_prompt="test",
        output=lambda _text: None,
    )
    try:
        result = controller.run_task("Create a directory")
        assert result.status == "completed"
        assert "create_directory" in client.tools
        assert (tmp_path / "created-by-agent").is_dir()
    finally:
        guard.close()


def test_disabled_agent_tool_is_not_advertised_or_dispatched(tmp_path: Path) -> None:
    guard = PathGuard([tmp_path])
    registry = create_tool_registry(guard)
    registry.disable("calculate")
    dispatcher = ToolDispatcher(
        registry,
        permission_checker=AllowPermissionChecker(),
    )
    try:
        assert "calculate" not in {
            definition.name for definition in registry.llm_tool_definitions()
        }
        result = _dispatch(dispatcher, "calculate", {"expression": "1 + 1"})
        assert result.status == "error"
        assert result.error_code == "TOOL_DISABLED"
    finally:
        guard.close()
