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
                thinking="private model reasoning",
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
    assert "private model reasoning" not in output
    assert client.calls[0]["system_prompt"]
    assert {tool.name for tool in client.calls[0]["tools"]} == {
        "find_files",
        "read_text",
        "save_note",
        "calculate",
        "system_status",
        "get_weather",
    }
    next_history = client.calls[1]["history"]
    assert all(
        "private model reasoning" not in item.content
        for item in next_history
    )
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
        "find_files",
        "read_text",
        "save_note",
        "calculate",
        "system_status",
        "get_weather",
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
                        name="read_text",
                        arguments={"path": str(file_path)},
                    )
                ],
                assistant_message=ChatMessage(
                    role="assistant",
                    content="",
                    tool_calls=[
                        ToolCall(
                            name="read_text",
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
