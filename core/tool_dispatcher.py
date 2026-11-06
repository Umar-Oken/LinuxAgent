"""Permission-gated execution for explicitly registered tools.

Async tool calls can be cancelled by timeout. Synchronous callables run inline to
avoid abandoned worker threads; their timeout is detected after they return,
so it cannot prevent a blocking synchronous implementation from delaying the
dispatcher. Use cooperative async tools when a hard execution deadline matters.
"""

from __future__ import annotations

import asyncio
import copy
import inspect
import json
import time
from collections.abc import Awaitable, Mapping
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from core.llm_client import ToolCall
from core.tool_registry import (
    DisabledToolError,
    ToolDefinition,
    ToolRegistry,
    ToolRegistryError,
    UnknownToolError,
)

ToolStatus = Literal["success", "error", "timeout", "denied"]


class ToolCallRequest(BaseModel):
    """Validated shape of a model-originated tool request."""

    model_config = ConfigDict(extra="forbid", strict=True)

    name: str = Field(min_length=1, max_length=64)
    arguments: dict[str, Any]


class ToolResult(BaseModel):
    """Stable tool result shape safe to return to the agent/model."""

    model_config = ConfigDict(extra="forbid")

    tool_name: str = Field(max_length=64)
    status: ToolStatus
    result_data: Any = None
    error_code: str | None = None
    safe_error_message: str | None = None
    execution_duration: float = Field(ge=0)


class PermissionChecker(Protocol):
    """Authorization boundary that must approve every tool call."""

    def is_allowed(
        self,
        tool: ToolDefinition,
        arguments: Mapping[str, Any],
    ) -> bool | Awaitable[bool]:
        """Return whether this specific invocation is authorized."""


class DenyByDefaultPermissionChecker:
    """Safe default that blocks all tool execution until policy is configured."""

    def is_allowed(
        self,
        tool: ToolDefinition,
        arguments: Mapping[str, Any],
    ) -> bool:
        return False


class _ToolTimedOut(Exception):
    """Internal marker for a completed synchronous call exceeding its limit."""


class ToolDispatcher:
    """Validate, authorize, and execute calls against a fixed registry."""

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        permission_checker: PermissionChecker | None = None,
        max_result_bytes: int = 16_384,
    ) -> None:
        if max_result_bytes < 1:
            raise ValueError("max_result_bytes must be greater than zero")
        self._registry = registry
        self._permission_checker = (
            permission_checker
            if permission_checker is not None
            else DenyByDefaultPermissionChecker()
        )
        self._max_result_bytes = max_result_bytes

    async def dispatch(self, request: object) -> ToolResult:
        """Run one validated, authorized tool call and return a safe result."""
        started = time.monotonic()
        tool_name = _extract_tool_name(request)
        try:
            call = _validate_request(request)
        except ValidationError:
            return self._error(
                tool_name,
                "INVALID_REQUEST",
                "Tool request is malformed.",
                started,
            )

        tool_name = call.name
        try:
            definition = self._registry.validate_tool_selection(
                call.name,
                call.arguments,
            )
        except UnknownToolError:
            return self._error(
                tool_name,
                "UNKNOWN_TOOL",
                "Requested tool is not registered.",
                started,
            )
        except DisabledToolError:
            return self._error(
                tool_name,
                "TOOL_DISABLED",
                "Requested tool is disabled.",
                started,
            )
        except ToolRegistryError:
            return self._error(
                tool_name,
                "INVALID_ARGUMENTS",
                "Tool arguments do not match the registered schema.",
                started,
            )

        try:
            validated_model = definition.argument_model.model_validate(
                call.arguments,
                strict=True,
            )
        except ValidationError:
            return self._error(
                tool_name,
                "INVALID_ARGUMENTS",
                "Tool arguments failed validation.",
                started,
            )
        arguments = validated_model.model_dump(mode="python")

        try:
            authorization_arguments = copy.deepcopy(arguments)
            allowed = self._permission_checker.is_allowed(
                definition,
                authorization_arguments,
            )
            if inspect.isawaitable(allowed):
                allowed = await allowed
        except Exception:
            return self._error(
                tool_name,
                "AUTHORIZATION_ERROR",
                "Tool authorization could not be completed.",
                started,
                status="denied",
            )
        if allowed is not True:
            return self._error(
                tool_name,
                "PERMISSION_DENIED",
                "Tool execution was not authorized.",
                started,
                status="denied",
            )

        try:
            result = await self._execute(definition, arguments)
        except (asyncio.TimeoutError, _ToolTimedOut):
            return self._error(
                tool_name,
                "TIMEOUT",
                "Tool execution exceeded its time limit.",
                started,
                status="timeout",
            )
        except Exception:
            return self._error(
                tool_name,
                "EXECUTION_ERROR",
                "Tool execution failed.",
                started,
            )

        try:
            result_size = _serialized_size(result, self._max_result_bytes)
        except (TypeError, ValueError, UnicodeError):
            return self._error(
                tool_name,
                "INVALID_RESULT",
                "Tool returned data that cannot be safely represented.",
                started,
            )
        if result_size > self._max_result_bytes:
            return self._error(
                tool_name,
                "RESULT_TOO_LARGE",
                "Tool result exceeded the permitted size.",
                started,
            )

        return ToolResult(
            tool_name=tool_name,
            status="success",
            result_data=result,
            execution_duration=max(0.0, time.monotonic() - started),
        )

    async def _execute(
        self,
        definition: ToolDefinition,
        arguments: dict[str, Any],
    ) -> Any:
        started = time.monotonic()
        result = definition.implementation(**arguments)
        if inspect.isawaitable(result):
            return await asyncio.wait_for(
                result,
                timeout=definition.execution_timeout,
            )
        if time.monotonic() - started > definition.execution_timeout:
            raise _ToolTimedOut
        return result

    @staticmethod
    def _error(
        tool_name: str,
        error_code: str,
        safe_message: str,
        started: float,
        *,
        status: ToolStatus = "error",
    ) -> ToolResult:
        return ToolResult(
            tool_name=tool_name,
            status=status,
            error_code=error_code,
            safe_error_message=safe_message,
            execution_duration=max(0.0, time.monotonic() - started),
        )


def _validate_request(
    request: object,
) -> ToolCallRequest:
    if isinstance(request, ToolCall):
        payload = {"name": request.name, "arguments": request.arguments}
    elif isinstance(request, ToolCallRequest):
        payload = request.model_dump()
    else:
        payload = request
    return ToolCallRequest.model_validate(payload)


def _extract_tool_name(
    request: object,
) -> str:
    if isinstance(request, (ToolCall, ToolCallRequest)):
        name = request.name
    elif isinstance(request, Mapping):
        name = request.get("name")
    else:
        name = None
    return name if isinstance(name, str) and len(name) <= 64 else "<invalid>"


def _serialized_size(value: Any, maximum: int) -> int:
    encoder = json.JSONEncoder(
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )
    size = 0
    for chunk in encoder.iterencode(value):
        size += len(chunk.encode("utf-8"))
        if size > maximum:
            return size
    return size
