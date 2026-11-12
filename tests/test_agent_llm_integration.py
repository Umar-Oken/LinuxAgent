"""Integration checks for the agent loop using its LLM client abstraction."""

from typing import Any
from collections.abc import Sequence
from pathlib import Path

from core.llm_client import (
    ChatMessage,
    LLMResponse,
    ModelCapabilities,
    ToolCall,
    ToolDefinition,
)
from core.tool_dispatcher import ToolDispatcher
from core.tool_registry import ToolDefinition as RegisteredTool
from security.path_guard import PathGuard
from agent import MAX_STEPS, create_tool_registry, run_task


class FakeLLMClient:
    def __init__(self, responses: list[LLMResponse]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    def chat(
        self,
        history: Sequence[ChatMessage],
        *,
        system_prompt: str | None = None,
        tools: Sequence[ToolDefinition] = (),
    ) -> LLMResponse:
        self.calls.append(
            {
                "history": list(history),
                "system_prompt": system_prompt,
                "tools": tools,
            }
        )
        return self.responses.pop(0)

    def get_capabilities(self) -> ModelCapabilities:
        raise NotImplementedError


def test_run_task_executes_native_tool_call_and_continues(capsys: Any) -> None:
    root = Path.cwd()
    path_guard = PathGuard([root])
    registry = create_tool_registry(path_guard)
    dispatcher = ToolDispatcher(registry, permission_checker=AllowAllPermissionChecker())
    client = FakeLLMClient(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        name="calculate",
                        arguments={"expression": "6 * 7"},
                    )
                ],
                assistant_message=ChatMessage(
                    role="assistant",
                    content="",
                    tool_calls=[
                        ToolCall(
                            name="calculate",
                            arguments={"expression": "6 * 7"},
                        )
                    ],
                ),
            ),
            LLMResponse(
                content="Javob: 42",
                assistant_message=ChatMessage(
                    role="assistant",
                    content="Javob: 42",
                ),
            ),
        ]
    )

    run_task(
        "6 * 7 ni hisobla",
        client=client,
        registry=registry,
        dispatcher=dispatcher,
        path_guard=path_guard,
    )

    output = capsys.readouterr().out
    assert "[step] Vosita: calculate" in output
    assert "Agent: Javob: 42" in output
    assert client.calls[0]["system_prompt"]
    assert {tool.name for tool in client.calls[0]["tools"]} == {
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
    next_history = client.calls[1]["history"]
    assert next_history[1].role == "assistant"
    assert next_history[1].tool_calls is not None
    assert next_history[2].role == "tool"
    assert next_history[2].tool_name == "calculate"
    assert "Natija: 42" in next_history[2].content
    assert "UNTRUSTED TOOL DATA" in next_history[2].content
    path_guard.close()


def test_run_task_processes_structured_fallback_call(capsys: Any) -> None:
    root = Path.cwd()
    path_guard = PathGuard([root])
    registry = create_tool_registry(path_guard)
    dispatcher = ToolDispatcher(registry, permission_checker=AllowAllPermissionChecker())
    client = FakeLLMClient(
        [
            LLMResponse(
                content='{"tool":"calculate","args":{"expression":"3 + 4"}}',
                tool_calls=[
                    ToolCall(name="calculate", arguments={"expression": "3 + 4"})
                ],
                assistant_message=ChatMessage(
                    role="assistant",
                    content='{"tool":"calculate","args":{"expression":"3 + 4"}}',
                ),
            ),
            LLMResponse(
                content="Natija yetti.",
                assistant_message=ChatMessage(
                    role="assistant",
                    content="Natija yetti.",
                ),
            ),
        ]
    )

    run_task(
        "3 + 4 ni hisobla",
        client=client,
        registry=registry,
        dispatcher=dispatcher,
        path_guard=path_guard,
    )

    output = capsys.readouterr().out
    assert "Agent: Natija yetti." in output
    assert client.calls[1]["history"][2].role == "user"
    assert "Natija: 7" in client.calls[1]["history"][2].content
    path_guard.close()


class AllowAllPermissionChecker:
    def is_allowed(
        self,
        tool: RegisteredTool,
        arguments: dict[str, Any],
    ) -> bool:
        return True


def test_schemas_cover_every_registered_tool() -> None:
    guard = PathGuard([Path.cwd()])
    registry = create_tool_registry(guard)
    assert {definition.name for definition in registry.llm_tool_definitions()} == {
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
    assert MAX_STEPS == 8
    guard.close()


def test_model_receives_path_redacted_untrusted_tool_data(
    tmp_path: Path,
    capsys: Any,
) -> None:
    file_path = tmp_path / "private.txt"
    file_path.write_text(
        "untrusted file contents\npassword: hidden phrase\nTOKEN=abc123",
        encoding="utf-8",
    )
    guard = PathGuard([tmp_path])
    registry = create_tool_registry(guard)
    dispatcher = ToolDispatcher(registry, permission_checker=AllowAllPermissionChecker())
    client = FakeLLMClient(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        name="read_text_file",
                        arguments={"path": str(file_path)},
                    )
                ],
                assistant_message=ChatMessage(
                    role="assistant",
                    content="",
                    tool_calls=[
                        ToolCall(
                            name="read_text_file",
                            arguments={"path": str(file_path)},
                        )
                    ],
                ),
            ),
            LLMResponse(
                content="Faylda matn bor.",
                assistant_message=ChatMessage(
                    role="assistant",
                    content="Faylda matn bor.",
                ),
            ),
        ]
    )

    run_task(
        "Faylni o'qi",
        client=client,
        registry=registry,
        dispatcher=dispatcher,
        path_guard=guard,
    )

    tool_data = client.calls[1]["history"][2].content
    assert "untrusted file contents" in tool_data
    assert "hidden phrase" not in tool_data
    assert "abc123" not in tool_data
    assert str(tmp_path) not in tool_data
    assert "UNTRUSTED TOOL DATA" in tool_data
    assert "Agent: Faylda matn bor." in capsys.readouterr().out
    guard.close()


def test_first_four_sample_workflows_use_migrated_tools(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    import json

    import agent as agent_module
    from core.agent_controller import AgentController

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    documentation = tmp_path / "python-guide.md"
    documentation.write_text("Python sample documentation", encoding="utf-8")
    guard = PathGuard([tmp_path])
    registry = create_tool_registry(guard)
    calls = [
        ("search_files", {"query": "python", "folder": str(tmp_path)}),
        ("read_text_file", {"path": str(documentation)}),
        ("create_note", {"title": "summary", "content": "Python summary"}),
        ("calculate", {"expression": "200 * (1 - 0.15)"}),
        ("get_system_status", {}),
        ("get_weather", {"city": "London"}),
    ]

    class FakeResponse:
        def __init__(self, value: object) -> None:
            self.value = value

        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(self.value).encode()

    weather_responses = iter(
        (
            FakeResponse({"results": [{"latitude": 51.5, "longitude": -0.1}]}),
            FakeResponse({"current": {"temperature_2m": 16}}),
        )
    )
    monkeypatch.setattr(
        agent_module,
        "urlopen",
        lambda *_args, **_kwargs: next(weather_responses),
    )
    monkeypatch.setattr("builtins.input", lambda _prompt: "ha")
    dispatcher = ToolDispatcher(registry, permission_checker=AllowAllPermissionChecker())

    def run_calls(selected: list[tuple[str, dict[str, Any]]], final: str) -> None:
        responses: list[LLMResponse] = []
        for name, arguments in selected:
            call = ToolCall(name=name, arguments=arguments)
            responses.append(
                LLMResponse(
                    content="",
                    tool_calls=[call],
                    assistant_message=ChatMessage(
                        role="assistant",
                        content="",
                        tool_calls=[call],
                    ),
                )
            )
        responses.append(
            LLMResponse(
                content=final,
                assistant_message=ChatMessage(role="assistant", content=final),
            )
        )
        controller = AgentController(
            client=FakeLLMClient(responses),
            registry=registry,
            dispatcher=dispatcher,
            path_guard=guard,
            system_prompt="test",
            output=lambda _text: None,
        )
        assert controller.run_task("sample request").status == "completed"

    try:
        # Sample 1: find documentation, read it, then create the approved summary.
        run_calls(calls[:3], "Hujjat topildi, o'qildi va xulosa saqlandi.")
        note_files = list((tmp_path / "agent_notes").glob("*.md"))
        assert len(note_files) == 1
        assert note_files[0].read_text(encoding="utf-8") == "Python summary"

        # Samples 2–4: calculate a discount, inspect system resources, and fetch weather.
        run_calls(calls[3:4], "Chegirmali narx: 170.")
        run_calls(calls[4:5], "RAM va disk holati tayyor.")
        run_calls(calls[5:], "London ob-havosi tayyor.")
    finally:
        guard.close()
