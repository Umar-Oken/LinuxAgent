"""Reusable Ollama adapter and typed language-model interfaces."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Literal, Protocol

import httpx
import ollama
from jsonschema import Draft202012Validator, SchemaError
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


class LLMError(Exception):
    """Base class for errors raised by a language-model adapter."""


class LLMTransportError(LLMError):
    """Ollama could not complete a request because of a transport/service error."""


class LLMResponseError(LLMError):
    """Ollama returned a malformed or incomplete response."""


class UnsupportedCapabilityError(LLMError):
    """The configured model/server cannot provide the requested response mode."""


class ChatMessage(BaseModel):
    """A message that can be sent in model conversation history."""

    model_config = ConfigDict(extra="forbid")

    role: Literal["system", "user", "assistant", "tool"]
    content: str
    tool_calls: list[ToolCall] | None = None
    tool_name: str | None = None


class ToolCall(BaseModel):
    """A validated structured request to invoke a named application tool."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    arguments: dict[str, Any]


class LLMResponse(BaseModel):
    """Normalized response; thinking remains separate from conversation history."""

    model_config = ConfigDict(extra="forbid")

    content: str
    thinking: str = Field(default="", exclude=True, repr=False)
    tool_calls: list[ToolCall] = Field(default_factory=list)
    assistant_message: ChatMessage

    @model_validator(mode="after")
    def require_content_or_tool_calls(self) -> LLMResponse:
        if not self.content.strip() and not self.tool_calls:
            raise ValueError("response must contain text or at least one tool call")
        return self


class ToolDefinition(BaseModel):
    """Tool declaration and JSON Schema used to validate its arguments."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    description: str = ""
    parameters: dict[str, Any]

    @model_validator(mode="after")
    def validate_parameters_schema(self) -> ToolDefinition:
        try:
            Draft202012Validator.check_schema(self.parameters)
        except SchemaError as exc:
            raise ValueError(f"invalid parameter schema for tool '{self.name}'") from exc
        if self.parameters.get("type") != "object":
            raise ValueError("tool parameter schema must describe an object")
        return self


class ModelCapabilities(BaseModel):
    """Capabilities advertised by the model and configured Ollama adapter."""

    native_tool_calling: bool
    structured_json: bool


class LLMClient(Protocol):
    """Model-independent interface consumed by future agent orchestration."""

    def chat(
        self,
        history: Sequence[ChatMessage],
        *,
        system_prompt: str | None = None,
        tools: Sequence[ToolDefinition] = (),
    ) -> LLMResponse:
        """Return one normalized response while preserving conversation history."""

    def get_capabilities(self) -> ModelCapabilities:
        """Inspect the configured model's response capabilities."""


class OllamaTransport(Protocol):
    """Small subset of the Ollama SDK used by this adapter."""

    def chat(self, **kwargs: Any) -> Any: ...

    def show(self, model: str) -> Any: ...


class _StructuredOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool: str | None = None
    args: dict[str, Any] | None = None
    final: str | None = None

    @model_validator(mode="after")
    def validate_output_shape(self) -> _StructuredOutput:
        is_tool_call = self.tool is not None and self.args is not None and self.final is None
        is_final = self.final is not None and self.tool is None and self.args is None
        if not (is_tool_call or is_final):
            raise ValueError("response must contain either a tool/args pair or final text")
        if self.tool is not None and not self.tool.strip():
            raise ValueError("tool name cannot be empty")
        if self.final is not None and not self.final.strip():
            raise ValueError("final response cannot be empty")
        return self


class OllamaClient:
    """Synchronous Ollama client with bounded transport retries and validation."""

    MAX_RETRIES = 10

    def __init__(
        self,
        *,
        model: str,
        host: str = "http://127.0.0.1:11434",
        timeout: float = 60.0,
        think: bool = False,
        max_retries: int = 2,
        structured_json_supported: bool = True,
        transport: OllamaTransport | None = None,
        retry_delay: float = 0.1,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not model.strip():
            raise ValueError("model must not be empty")
        if timeout <= 0:
            raise ValueError("timeout must be greater than zero")
        if not 0 <= max_retries <= self.MAX_RETRIES:
            raise ValueError(f"max_retries must be between 0 and {self.MAX_RETRIES}")
        if retry_delay < 0:
            raise ValueError("retry_delay must not be negative")

        self.model = model
        self.think = think
        self.max_retries = max_retries
        self.structured_json_supported = structured_json_supported
        self._transport = (
            transport if transport is not None else ollama.Client(host=host, timeout=timeout)
        )
        self._retry_delay = retry_delay
        self._sleep = sleep
        self._capabilities: ModelCapabilities | None = None
        self._tool_validators: dict[str, Draft202012Validator] = {}

    def get_capabilities(self) -> ModelCapabilities:
        """Read advertised model capabilities; do not change the configured model."""
        if self._capabilities is not None:
            return self._capabilities

        response = self._request(lambda: self._transport.show(self.model))
        raw_capabilities = _read_field(response, "capabilities")
        if not isinstance(raw_capabilities, Sequence) or isinstance(
            raw_capabilities, (str, bytes)
        ):
            raise LLMResponseError("Ollama model metadata has no valid capabilities list.")

        capabilities = {item.casefold() for item in raw_capabilities if isinstance(item, str)}
        detected = ModelCapabilities(
            native_tool_calling="tools" in capabilities,
            structured_json=self.structured_json_supported,
        )
        self._capabilities = detected
        return detected

    def chat(
        self,
        history: Sequence[ChatMessage],
        *,
        system_prompt: str | None = None,
        tools: Sequence[ToolDefinition] = (),
    ) -> LLMResponse:
        """Send a request and normalize native or schema-constrained tool output."""
        messages = self._prepare_messages(history, system_prompt)
        if not tools:
            raw = self._request(
                lambda: self._transport.chat(
                    model=self.model,
                    messages=messages,
                    think=self.think,
                )
            )
            return self._normalize_response(raw)

        self._set_tool_validators(tools)
        capabilities = self.get_capabilities()
        if capabilities.native_tool_calling:
            raw = self._request(
                lambda: self._transport.chat(
                    model=self.model,
                    messages=messages,
                    tools=[self._ollama_tool(tool) for tool in tools],
                    think=self.think,
                )
            )
            return self._normalize_response(raw, tools)
        if not capabilities.structured_json:
            raise UnsupportedCapabilityError(
                f"Model '{self.model}' does not advertise tool calling and "
                "structured JSON fallback is disabled."
            )

        output_schema = self._structured_output_schema(tools)
        raw = self._request(
            lambda: self._transport.chat(
                model=self.model,
                messages=messages,
                format=output_schema,
                think=self.think,
            )
        )
        return self._normalize_structured_response(raw)

    @staticmethod
    def _prepare_messages(
        history: Sequence[ChatMessage],
        system_prompt: str | None,
    ) -> list[dict[str, Any]]:
        messages = []
        for message in history:
            payload: dict[str, Any] = {
                "role": message.role,
                "content": message.content,
            }
            if message.tool_calls:
                payload["tool_calls"] = [
                    {
                        "function": {
                            "name": call.name,
                            "arguments": call.arguments,
                        }
                    }
                    for call in message.tool_calls
                ]
            if message.tool_name:
                payload["tool_name"] = message.tool_name
            messages.append(payload)
        if system_prompt:
            messages.insert(0, {"role": "system", "content": system_prompt})
        return messages

    @staticmethod
    def _ollama_tool(tool: ToolDefinition) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
            },
        }

    @staticmethod
    def _structured_output_schema(tools: Sequence[ToolDefinition]) -> dict[str, Any]:
        tool_variants = [
            {
                "type": "object",
                "properties": {
                    "tool": {"const": tool.name},
                    "args": tool.parameters,
                },
                "required": ["tool", "args"],
                "additionalProperties": False,
            }
            for tool in tools
        ]
        return {
            "anyOf": [
                *tool_variants,
                {
                    "type": "object",
                    "properties": {"final": {"type": "string", "minLength": 1}},
                    "required": ["final"],
                    "additionalProperties": False,
                },
            ]
        }

    def _set_tool_validators(self, tools: Sequence[ToolDefinition]) -> None:
        names = [tool.name for tool in tools]
        if len(set(names)) != len(names):
            raise LLMResponseError("Tool definitions contain duplicate names.")
        self._tool_validators = {
            tool.name: Draft202012Validator(tool.parameters) for tool in tools
        }

    def _normalize_response(
        self,
        response: Any,
        tools: Sequence[ToolDefinition] = (),
    ) -> LLMResponse:
        message = _read_field(response, "message")
        if message is None:
            raise LLMResponseError("Ollama response is missing its message.")
        content = _read_field(message, "content", "")
        if not isinstance(content, str):
            raise LLMResponseError("Ollama response content must be text.")
        content, thinking = self._separate_thinking(
            content,
            _read_field(message, "thinking", ""),
        )

        raw_calls = _read_field(message, "tool_calls", []) or []
        if not isinstance(raw_calls, Sequence) or isinstance(raw_calls, (str, bytes)):
            raise LLMResponseError("Ollama tool_calls must be a list.")
        tool_calls = [self._parse_native_tool_call(call) for call in raw_calls]
        if tool_calls and not tools:
            raise LLMResponseError("Ollama returned tool calls when no tools were supplied.")
        for call in tool_calls:
            self._validate_tool_call(call)

        try:
            assistant_message = ChatMessage(
                role="assistant",
                content=content,
                tool_calls=tool_calls or None,
            )
            return LLMResponse(
                content=content,
                thinking=thinking,
                tool_calls=tool_calls,
                assistant_message=assistant_message,
            )
        except ValidationError as exc:
            raise LLMResponseError("Ollama returned an empty or invalid response.") from exc

    @staticmethod
    def _parse_native_tool_call(raw_call: Any) -> ToolCall:
        function = _read_field(raw_call, "function")
        name = _read_field(function, "name")
        arguments = _read_field(function, "arguments")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError as exc:
                raise LLMResponseError("Ollama returned malformed tool arguments.") from exc
        try:
            return ToolCall(name=name, arguments=arguments)
        except ValidationError as exc:
            raise LLMResponseError("Ollama returned an invalid tool call.") from exc

    def _normalize_structured_response(
        self,
        response: Any,
    ) -> LLMResponse:
        message = _read_field(response, "message")
        content = _read_field(message, "content")
        if not isinstance(content, str):
            raise LLMResponseError("Ollama structured response content must be text.")
        content, thinking = self._separate_thinking(
            content,
            _read_field(message, "thinking", ""),
        )
        try:
            structured = _StructuredOutput.model_validate_json(content)
        except (ValidationError, ValueError) as exc:
            raise LLMResponseError("Ollama returned invalid structured JSON.") from exc

        if structured.final is not None:
            assistant_message = ChatMessage(role="assistant", content=structured.final)
            return LLMResponse(
                content=structured.final,
                thinking=thinking,
                assistant_message=assistant_message,
            )

        if structured.tool is None or structured.args is None:
            raise LLMResponseError("Ollama returned an incomplete structured tool call.")
        call = ToolCall(name=structured.tool, arguments=structured.args)
        self._validate_tool_call(call)
        assistant_message = ChatMessage(role="assistant", content=content)
        return LLMResponse(
            content=content,
            thinking=thinking,
            tool_calls=[call],
            assistant_message=assistant_message,
        )

    @staticmethod
    def _separate_thinking(content: str, explicit_thinking: Any) -> tuple[str, str]:
        if explicit_thinking is None:
            explicit_thinking = ""
        if not isinstance(explicit_thinking, str):
            raise LLMResponseError("Ollama thinking field must be text.")

        thinking_parts = [explicit_thinking] if explicit_thinking.strip() else []
        answer_parts: list[str] = []
        cursor = 0
        while cursor < len(content):
            opening = content.find("<think>", cursor)
            closing = content.find("</think>", cursor)
            if closing >= 0 and (opening < 0 or closing < opening):
                thinking_parts.append(content[cursor:closing])
                cursor = closing + len("</think>")
                continue
            if opening < 0:
                answer_parts.append(content[cursor:])
                break

            answer_parts.append(content[cursor:opening])
            body_start = opening + len("<think>")
            closing = content.find("</think>", body_start)
            if closing < 0:
                thinking_parts.append(content[body_start:])
                break
            thinking_parts.append(content[body_start:closing])
            cursor = closing + len("</think>")

        answer = "".join(answer_parts).strip()
        thinking = "\n".join(part.strip() for part in thinking_parts if part.strip())
        return answer, thinking

    def _validate_tool_call(self, call: ToolCall) -> None:
        validator = self._tool_validators.get(call.name)
        if validator is None:
            raise LLMResponseError(f"Ollama requested unknown tool '{call.name}'.")
        errors = list(validator.iter_errors(call.arguments))
        if errors:
            path = ".".join(str(part) for part in errors[0].absolute_path)
            location = f" at '{path}'" if path else ""
            raise LLMResponseError(
                f"Arguments for tool '{call.name}' do not match its schema{location}."
            )

    def _request(self, operation: Callable[[], Any]) -> Any:
        attempts = self.max_retries + 1
        for attempt in range(attempts):
            try:
                return operation()
            except Exception as exc:
                retryable = _is_retryable_transport_error(exc)
                if not retryable:
                    if _is_transport_error(exc):
                        raise LLMTransportError(
                            f"Ollama request failed ({type(exc).__name__})."
                        ) from exc
                    raise
                if attempt + 1 >= attempts:
                    raise LLMTransportError(
                        f"Ollama request failed after {attempts} attempts "
                        f"({type(exc).__name__})."
                    ) from exc
                self._sleep(self._retry_delay * (2**attempt))
        raise AssertionError("unreachable")


def _read_field(value: Any, field: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(field, default)
    return getattr(value, field, default)


def _is_retryable_transport_error(error: Exception) -> bool:
    if isinstance(error, (httpx.TimeoutException, httpx.NetworkError, ConnectionError, OSError)):
        return True
    if isinstance(error, ollama.ResponseError):
        status_code = error.status_code
        return isinstance(status_code, int) and (
            status_code == 429 or status_code >= 500
        )
    return False


def _is_transport_error(error: Exception) -> bool:
    return _is_retryable_transport_error(error) or isinstance(
        error,
        (httpx.HTTPStatusError, ollama.ResponseError),
    )
