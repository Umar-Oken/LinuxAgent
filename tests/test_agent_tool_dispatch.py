"""Tests that the legacy tools remain reachable through the guarded dispatcher."""

import asyncio
import builtins
import json
from pathlib import Path
from typing import Any

from agent import _CliPermissionChecker, create_tool_registry
from core.tool_dispatcher import ToolDispatcher, ToolResult
from core.tool_registry import ToolDefinition
from security.path_guard import PathGuard


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
        status = _dispatch(dispatcher, "system_status", {})

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
            "read_text",
            {"path": str(source)},
        )
        find_result = _dispatch(
            dispatcher,
            "find_files",
            {"folder": str(allowed), "query": "sample", "ext": ".txt"},
        )
        denied = _dispatch(
            dispatcher,
            "read_text",
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
    dispatcher = ToolDispatcher(
        create_tool_registry(guard),
        permission_checker=_CliPermissionChecker("writes"),
    )
    confirmations = iter(("ha",))
    monkeypatch.setattr(builtins, "input", lambda _prompt: next(confirmations))
    try:
        result = _dispatch(
            dispatcher,
            "save_note",
            {"title": "test", "content": "remember"},
        )

        assert result.status == "success"
        note_files = list((tmp_path / "agent_notes").glob("*.md"))
        assert len(note_files) == 1
        assert note_files[0].read_text(encoding="utf-8") == "remember"
    finally:
        guard.close()


def test_weather_tool_uses_registered_arguments(monkeypatch: Any, tmp_path: Path) -> None:
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
    monkeypatch.setattr("agent.urlopen", lambda *_args, **_kwargs: next(responses))
    guard = PathGuard([tmp_path])
    dispatcher = _dispatcher(guard)
    try:
        result = _dispatch(dispatcher, "get_weather", {"city": "London"})
        assert result.status == "success"
        assert "London" in str(result.result_data)
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
