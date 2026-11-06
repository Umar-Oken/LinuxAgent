"""Integration checks for the agent loop using its LLM client abstraction."""

from typing import Any
from collections.abc import Sequence

from core.llm_client import (
    ChatMessage,
    LLMResponse,
    ModelCapabilities,
    ToolCall,
    ToolDefinition,
)
from agent import MAX_STEPS, TOOL_DEFINITIONS, run_task


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

    run_task("6 * 7 ni hisobla", client=client)

    output = capsys.readouterr().out
    assert "[step] Vosita: calculate" in output
    assert "Agent: Javob: 42" in output
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
    assert next_history[1].role == "assistant"
    assert next_history[1].tool_calls is not None
    assert next_history[2].role == "tool"
    assert next_history[2].tool_name == "calculate"
    assert next_history[2].content == "Natija: 42"


def test_run_task_processes_structured_fallback_call(capsys: Any) -> None:
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

    run_task("3 + 4 ni hisobla", client=client)

    output = capsys.readouterr().out
    assert "Agent: Natija yetti." in output
    assert client.calls[1]["history"][2].role == "user"
    assert client.calls[1]["history"][2].content == "Vosita natijasi: Natija: 7"


def test_tool_schemas_cover_every_registered_tool() -> None:
    assert {definition.name for definition in TOOL_DEFINITIONS} == {
        "find_files",
        "read_text",
        "save_note",
        "calculate",
        "system_status",
        "get_weather",
    }
    assert MAX_STEPS == 8
