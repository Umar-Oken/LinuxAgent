"""Tests for the Ollama language-model adapter."""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from core.config import load_config
from core.llm_client import (
    ChatMessage,
    LLMResponseError,
    LLMTransportError,
    OllamaClient,
    ToolCall,
    ToolDefinition,
    UnsupportedCapabilityError,
)


class FakeTransport:
    def __init__(self, *, response: Any = None, capabilities: list[str] | None = None) -> None:
        self.response = response
        self.capabilities = capabilities or []
        self.chat_calls: list[dict[str, Any]] = []
        self.show_calls: list[str] = []
        self.chat_error: Exception | None = None

    def chat(self, **kwargs: Any) -> Any:
        self.chat_calls.append(kwargs)
        if self.chat_error is not None:
            raise self.chat_error
        if isinstance(self.response, list):
            response = self.response.pop(0)
            if isinstance(response, Exception):
                raise response
            return response
        return self.response

    def show(self, model: str) -> Any:
        self.show_calls.append(model)
        return {"capabilities": self.capabilities}


def _tool() -> ToolDefinition:
    return ToolDefinition(
        name="calculate",
        description="Evaluate arithmetic",
        parameters={
            "type": "object",
            "properties": {"expression": {"type": "string"}},
            "required": ["expression"],
            "additionalProperties": False,
        },
    )


def test_successful_chat_preserves_history_and_adds_system_prompt() -> None:
    transport = FakeTransport(
        response={"message": {"role": "assistant", "content": "Hello"}}
    )
    client = OllamaClient(model="qwen3:4b", transport=transport)

    response = client.chat(
        [ChatMessage(role="user", content="Hi")],
        system_prompt="Be concise",
    )

    assert response.content == "Hello"
    assert response.assistant_message.role == "assistant"
    assert transport.chat_calls[0]["model"] == "qwen3:4b"
    assert transport.chat_calls[0]["think"] is False
    assert transport.chat_calls[0]["messages"] == [
        {"role": "system", "content": "Be concise"},
        {"role": "user", "content": "Hi"},
    ]


def test_retries_timeout_then_returns_response() -> None:
    transport = FakeTransport(
        response=[
            httpx.ReadTimeout("slow"),
            {"message": {"role": "assistant", "content": "Recovered"}},
        ]
    )
    delays: list[float] = []
    client = OllamaClient(
        model="qwen3:4b",
        transport=transport,
        max_retries=1,
        retry_delay=0.25,
        sleep=delays.append,
    )

    response = client.chat([ChatMessage(role="user", content="Hi")])

    assert response.content == "Recovered"
    assert len(transport.chat_calls) == 2
    assert delays == [0.25]


def test_connection_failure_is_transport_error_after_bounded_retries() -> None:
    transport = FakeTransport()
    transport.chat_error = httpx.ConnectError("offline")
    client = OllamaClient(
        model="qwen3:4b",
        transport=transport,
        max_retries=2,
        sleep=lambda _: None,
    )

    with pytest.raises(LLMTransportError, match="after 3 attempts"):
        client.chat([ChatMessage(role="user", content="Hi")])

    assert len(transport.chat_calls) == 3


def test_invalid_structured_json_is_response_error() -> None:
    transport = FakeTransport(
        capabilities=[],
        response={"message": {"content": '{"tool":"calculate","args":{}} extra'}},
    )
    client = OllamaClient(model="qwen3:4b", transport=transport)

    with pytest.raises(LLMResponseError, match="invalid structured JSON"):
        client.chat(
            [ChatMessage(role="user", content="calculate")],
            tools=[_tool()],
        )


def test_structured_fallback_validates_tool_arguments() -> None:
    transport = FakeTransport(
        capabilities=[],
        response={
            "message": {
                "content": json.dumps(
                    {"tool": "calculate", "args": {"expression": 42}}
                )
            }
        },
    )
    client = OllamaClient(model="qwen3:4b", transport=transport)

    with pytest.raises(LLMResponseError, match="do not match its schema"):
        client.chat(
            [ChatMessage(role="user", content="calculate")],
            tools=[_tool()],
        )
    assert "format" in transport.chat_calls[0]
    assert transport.chat_calls[0]["think"] is False
    assert transport.show_calls == ["qwen3:4b"]


def test_native_tool_calling_uses_advertised_capability() -> None:
    transport = FakeTransport(
        capabilities=["completion", "tools"],
        response={
            "message": {
                "content": "",
                "tool_calls": [
                    {
                        "function": {
                            "name": "calculate",
                            "arguments": {"expression": "2 + 2"},
                        }
                    }
                ],
            }
        },
    )
    client = OllamaClient(model="qwen3:4b", transport=transport)

    result = client.chat(
        [ChatMessage(role="user", content="calculate")],
        tools=[_tool()],
    )

    assert result.tool_calls[0].name == "calculate"
    assert result.tool_calls[0].arguments == {"expression": "2 + 2"}
    assert "tools" in transport.chat_calls[0]
    assert "format" not in transport.chat_calls[0]
    assert transport.chat_calls[0]["think"] is False


def test_native_tool_history_uses_ollama_wire_format() -> None:
    transport = FakeTransport(
        capabilities=["tools"],
        response={"message": {"content": "Done"}},
    )
    client = OllamaClient(model="qwen3:4b", transport=transport)
    history = [
        ChatMessage(
            role="assistant",
            content="",
            tool_calls=[
                ToolCall(name="calculate", arguments={"expression": "2 + 2"})
            ],
        ),
        ChatMessage(role="tool", tool_name="calculate", content="4"),
    ]

    client.chat(history, tools=[_tool()])

    assert transport.chat_calls[0]["messages"] == [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "function": {
                        "name": "calculate",
                        "arguments": {"expression": "2 + 2"},
                    }
                }
            ],
        },
        {"role": "tool", "content": "4", "tool_name": "calculate"},
    ]


def test_structured_fallback_supports_final_response() -> None:
    transport = FakeTransport(
        capabilities=[],
        response={"message": {"content": '{"final":"Finished"}'}},
    )
    client = OllamaClient(model="qwen3:4b", transport=transport)

    response = client.chat(
        [ChatMessage(role="user", content="finish")],
        tools=[_tool()],
    )

    assert response.content == "Finished"
    assert response.tool_calls == []
    assert response.assistant_message.content == "Finished"


def test_unsupported_tool_calling_raises_without_fallback() -> None:
    transport = FakeTransport(capabilities=[])
    client = OllamaClient(
        model="qwen3:4b",
        transport=transport,
        structured_json_supported=False,
    )

    with pytest.raises(UnsupportedCapabilityError):
        client.chat(
            [ChatMessage(role="user", content="calculate")],
            tools=[_tool()],
        )

    assert not transport.chat_calls


def test_model_show_connection_failure_is_transport_error() -> None:
    class FailedShowTransport(FakeTransport):
        def show(self, model: str) -> Any:
            raise httpx.ConnectError("offline")

    client = OllamaClient(
        model="qwen3:4b",
        transport=FailedShowTransport(),
        max_retries=0,
    )

    with pytest.raises(LLMTransportError):
        client.get_capabilities()


def test_retry_limit_is_bounded() -> None:
    with pytest.raises(ValueError, match="between 0 and 10"):
        OllamaClient(model="qwen3:4b", max_retries=11)


def test_thinking_mode_is_configurable() -> None:
    transport = FakeTransport(
        response={"message": {"role": "assistant", "content": "Short answer"}}
    )
    client = OllamaClient(
        model="qwen3:4b",
        transport=transport,
        think=True,
    )

    client.chat([ChatMessage(role="user", content="Hi")])

    assert transport.chat_calls[0]["think"] is True


def test_yaml_thinking_setting_is_forwarded_to_ollama(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text("ollama:\n  think: true\n", encoding="utf-8")
    settings = load_config(config_file)
    transport = FakeTransport(
        response={"message": {"role": "assistant", "content": "Answer"}}
    )
    client = OllamaClient(
        model=settings.ollama.model,
        think=settings.ollama.think,
        transport=transport,
    )

    client.chat([ChatMessage(role="user", content="Hi")])

    assert transport.chat_calls[0]["think"] is True


@pytest.mark.parametrize(
    ("content", "expected", "think"),
    [
        ("<think>private reasoning</think>\nOK", "OK", True),
        ("private reasoning</think>\nOK", "OK", False),
        ("Answer: <think>private reasoning</think>OK", "Answer: OK", True),
    ],
)
def test_inline_thinking_is_separated_from_content(
    content: str,
    expected: str,
    think: bool,
) -> None:
    transport = FakeTransport(
        response={"message": {"role": "assistant", "content": content}}
    )
    client = OllamaClient(model="qwen3:4b", transport=transport, think=think)

    response = client.chat([ChatMessage(role="user", content="Hi")])

    assert response.content == expected
    assert "private reasoning" not in response.content
    assert "private reasoning" in response.thinking
    assert "private reasoning" not in response.assistant_message.content
    assert transport.chat_calls[0]["think"] is think


def test_unclosed_thinking_is_separated_and_not_exposed() -> None:
    transport = FakeTransport(
        response={
            "message": {
                "role": "assistant",
                "content": "<think>unfinished reasoning",
            }
        }
    )
    client = OllamaClient(model="qwen3:4b", transport=transport)

    with pytest.raises(LLMResponseError, match="empty or invalid response") as error:
        client.chat([ChatMessage(role="user", content="Hi")])
    assert "unfinished reasoning" not in str(error.value)


def test_explicit_thinking_field_is_separate_and_not_added_to_history() -> None:
    transport = FakeTransport(
        response={
            "message": {
                "role": "assistant",
                "content": "Clean answer",
                "thinking": "private reasoning",
            }
        }
    )
    client = OllamaClient(model="qwen3:4b", transport=transport)
    response = client.chat([ChatMessage(role="user", content="Hi")])

    assert response.content == "Clean answer"
    assert response.thinking == "private reasoning"
    assert response.assistant_message.content == "Clean answer"
    assert "thinking" not in transport.chat_calls[0]["messages"][-1]


def test_thinking_is_separated_before_structured_tool_json_parsing() -> None:
    transport = FakeTransport(
        capabilities=[],
        response={
            "message": {
                "content": (
                    "<think>private thoughts with misleading JSON {}</think>"
                    '{"tool":"calculate","args":{"expression":"2 + 2"}}'
                ),
                "thinking": "explicit thought",
            }
        },
    )
    client = OllamaClient(model="qwen3:4b", transport=transport)

    response = client.chat(
        [ChatMessage(role="user", content="calculate")],
        tools=[_tool()],
    )

    assert response.tool_calls[0].arguments == {"expression": "2 + 2"}
    assert response.thinking == "explicit thought\nprivate thoughts with misleading JSON {}"
    assert "private thought" not in response.assistant_message.content
