"""Tests for guarded advanced filesystem discovery tools."""

import asyncio
import json
from pathlib import Path
from typing import Any

from agent import create_tool_registry
from core.llm_client import (
    ChatMessage,
    LLMResponse,
    ModelCapabilities,
    ToolCall,
)
from core.tool_dispatcher import ToolDispatcher
from core.tool_registry import ToolDefinition
from security.path_guard import PathGuard


class AllowPermission:
    def is_allowed(
        self,
        tool: ToolDefinition,
        arguments: dict[str, Any],
    ) -> bool:
        return True


def _dispatcher(root: Path) -> tuple[ToolDispatcher, PathGuard]:
    guard = PathGuard([root])
    return (
        ToolDispatcher(
            create_tool_registry(guard),
            permission_checker=AllowPermission(),
        ),
        guard,
    )


def _dispatch(
    dispatcher: ToolDispatcher,
    name: str,
    arguments: dict[str, Any],
) -> Any:
    return asyncio.run(
        dispatcher.dispatch({"name": name, "arguments": arguments})
    )


def _entries(result: Any) -> list[dict[str, Any]]:
    assert result.status == "success", result.safe_error_message
    return json.loads(result.result_data)["entries"]


def test_advanced_search_filters_filename_extension_and_size(tmp_path: Path) -> None:
    (tmp_path / "report-one.txt").write_text("12345")
    (tmp_path / "report-two.txt").write_text("123456789")
    (tmp_path / "report-three.md").write_text("123456")
    dispatcher, guard = _dispatcher(tmp_path)
    try:
        result = _dispatch(
            dispatcher,
            "search_files_advanced",
            {
                "path": str(tmp_path),
                "pattern": "report-*.txt",
                "extensions": [".txt"],
                "min_size_bytes": 4,
                "max_size_bytes": 6,
                "recursive": False,
            },
        )
        entries = _entries(result)
        assert [entry["name"] for entry in entries] == ["report-one.txt"]
        assert entries[0]["size_bytes"] == 5
    finally:
        guard.close()


def test_recursive_search_observes_depth_limit(tmp_path: Path) -> None:
    shallow = tmp_path / "shallow.txt"
    shallow.write_text("a")
    level_one = tmp_path / "one"
    level_one.mkdir()
    (level_one / "middle.txt").write_text("b")
    level_two = level_one / "two"
    level_two.mkdir()
    (level_two / "deep.txt").write_text("c")
    dispatcher, guard = _dispatcher(tmp_path)
    try:
        result = _dispatch(
            dispatcher,
            "search_files_advanced",
            {
                "path": str(tmp_path),
                "pattern": "*.txt",
                "max_depth": 1,
            },
        )
        assert {entry["name"] for entry in _entries(result)} == {
            "shallow.txt",
            "middle.txt",
        }
    finally:
        guard.close()


def test_pagination_is_sorted_and_reports_more_pages(tmp_path: Path) -> None:
    for name in ("charlie.txt", "alpha.txt", "bravo.txt"):
        (tmp_path / name).write_text(name)
    dispatcher, guard = _dispatcher(tmp_path)
    try:
        first = _dispatch(
            dispatcher,
            "search_files_advanced",
            {
                "path": str(tmp_path),
                "pattern": "*.txt",
                "recursive": False,
                "page": 1,
                "page_size": 2,
            },
        )
        second = _dispatch(
            dispatcher,
            "search_files_advanced",
            {
                "path": str(tmp_path),
                "pattern": "*.txt",
                "recursive": False,
                "page": 2,
                "page_size": 2,
            },
        )
        first_page = json.loads(first.result_data)
        assert [item["name"] for item in first_page["entries"]] == [
            "alpha.txt",
            "bravo.txt",
        ]
        assert first_page["has_more"]
        assert [item["name"] for item in _entries(second)] == ["charlie.txt"]
    finally:
        guard.close()


def test_directory_listing_skips_symlinks_and_metadata_is_guarded(
    tmp_path: Path,
) -> None:
    target = tmp_path / "safe.txt"
    target.write_text("safe")
    outside = tmp_path.parent / f"{tmp_path.name}-outside.txt"
    outside.write_text("outside")
    (tmp_path / "escape.txt").symlink_to(outside)
    dispatcher, guard = _dispatcher(tmp_path)
    try:
        listing = _dispatch(
            dispatcher,
            "list_directory",
            {"path": str(tmp_path), "recursive": False},
        )
        assert {item["name"] for item in _entries(listing)} == {"safe.txt"}

        metadata = _dispatch(
            dispatcher,
            "get_file_metadata",
            {"path": str(target)},
        )
        assert metadata.status == "success"
        assert json.loads(metadata.result_data)["size_bytes"] == 4
        denied = _dispatch(
            dispatcher,
            "get_file_metadata",
            {"path": str(outside)},
        )
        assert denied.status == "error"
    finally:
        guard.close()
        outside.unlink()


def test_find_large_files_filters_and_orders_by_size(tmp_path: Path) -> None:
    (tmp_path / "small.bin").write_bytes(b"1" * 4)
    (tmp_path / "large.bin").write_bytes(b"1" * 12)
    (tmp_path / "medium.bin").write_bytes(b"1" * 8)
    dispatcher, guard = _dispatcher(tmp_path)
    try:
        result = _dispatch(
            dispatcher,
            "find_large_files",
            {
                "path": str(tmp_path),
                "min_size_bytes": 7,
                "recursive": False,
                "page_size": 1,
            },
        )
        data = json.loads(result.result_data)
        assert data["entries"][0]["name"] == "large.bin"
        assert data["has_more"]
        assert data["total"] == 2
    finally:
        guard.close()


def test_advanced_search_is_available_through_agent_controller(
    tmp_path: Path,
) -> None:
    from core.agent_controller import AgentController
    from core.llm_client import ChatMessage

    target = tmp_path / "match.txt"
    target.write_text("found")
    call = ToolCall(
        name="search_files_advanced",
        arguments={
            "path": str(tmp_path),
            "pattern": "*.txt",
            "recursive": False,
        },
    )
    tool_response = LLMResponse(
        content="",
        tool_calls=[call],
        assistant_message=ChatMessage(
            role="assistant",
            content="",
            tool_calls=[call],
        ),
    )
    final_response = LLMResponse(
        content="Topildi.",
        assistant_message=ChatMessage(role="assistant", content="Topildi."),
    )
    class SequentialClient:
        def __init__(self) -> None:
            self.responses = [tool_response, final_response]
            self.calls: list[dict[str, Any]] = []

        def chat(
            self,
            history: Any,
            *,
            system_prompt: str | None = None,
            tools: Any = (),
        ) -> LLMResponse:
            self.calls.append({"history": list(history), "tools": list(tools)})
            return self.responses.pop(0)

        def get_capabilities(self) -> ModelCapabilities:
            raise NotImplementedError

    guard = PathGuard([tmp_path])
    registry = create_tool_registry(guard)
    client = SequentialClient()
    dispatcher = ToolDispatcher(registry, permission_checker=AllowPermission())
    controller = AgentController(
        client=client,
        registry=registry,
        dispatcher=dispatcher,
        path_guard=guard,
        system_prompt="test",
        output=lambda _text: None,
    )
    try:
        result = controller.run_task("Find match.txt")
        assert result.status == "completed"
        assert "match.txt" in client.calls[1]["history"][-1].content
        assert "search_files_advanced" in {
            tool.name for tool in client.calls[0]["tools"]
        }
    finally:
        guard.close()
