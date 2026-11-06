"""Tests for safe, permission-gated tool dispatch."""

import asyncio
import time
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict

from core.tool_dispatcher import (
    DenyByDefaultPermissionChecker,
    ToolDispatcher,
)
from core.tool_registry import ToolDefinition, ToolRegistry


class EchoArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    text: str


class AllowPermissionChecker:
    def is_allowed(
        self,
        tool: ToolDefinition,
        arguments: Mapping[str, Any],
    ) -> bool:
        return True


def _definition(
    implementation: Any,
    *,
    timeout: float = 1,
) -> ToolDefinition:
    return ToolDefinition(
        name="mock_echo",
        description="A test tool.",
        category="system",
        argument_schema=EchoArguments.model_json_schema(),
        argument_model=EchoArguments,
        permission_level="read",
        execution_timeout=timeout,
        implementation=implementation,
    )


def _dispatcher(
    implementation: Any,
    *,
    timeout: float = 1,
    max_result_bytes: int = 1024,
    permission_checker: Any = None,
) -> ToolDispatcher:
    registry = ToolRegistry()
    registry.register(_definition(implementation, timeout=timeout))
    return ToolDispatcher(
        registry,
        permission_checker=permission_checker or AllowPermissionChecker(),
        max_result_bytes=max_result_bytes,
    )


def test_dispatch_executes_valid_sync_tool() -> None:
    dispatcher = _dispatcher(lambda text: {"echo": text})

    result = asyncio.run(
        dispatcher.dispatch({"name": "mock_echo", "arguments": {"text": "hello"}})
    )

    assert result.tool_name == "mock_echo"
    assert result.status == "success"
    assert result.result_data == {"echo": "hello"}
    assert result.error_code is None
    assert result.execution_duration >= 0


def test_dispatch_rejects_invalid_arguments_before_invoking_tool() -> None:
    called = False

    def implementation(text: str) -> str:
        nonlocal called
        called = True
        return text

    result = asyncio.run(
        _dispatcher(implementation).dispatch(
            {"name": "mock_echo", "arguments": {"unexpected": True}}
        )
    )

    assert result.error_code == "INVALID_ARGUMENTS"
    assert not called


def test_dispatch_rejects_unknown_tool() -> None:
    dispatcher = _dispatcher(lambda text: text)

    result = asyncio.run(
        dispatcher.dispatch({"name": "not_registered", "arguments": {"text": "x"}})
    )

    assert result.error_code == "UNKNOWN_TOOL"
    assert result.result_data is None


def test_execution_exception_does_not_expose_exception_details() -> None:
    def fail(text: str) -> str:
        raise RuntimeError("secret traceback detail")

    result = asyncio.run(_dispatcher(fail).dispatch({"name": "mock_echo", "arguments": {"text": "x"}}))

    assert result.error_code == "EXECUTION_ERROR"
    assert result.safe_error_message == "Tool execution failed."
    assert "secret traceback detail" not in str(result.model_dump())


def test_async_tool_timeout_cancels_coroutine() -> None:
    cancelled = False

    async def slow(text: str) -> str:
        nonlocal cancelled
        try:
            await asyncio.sleep(1)
        except asyncio.CancelledError:
            cancelled = True
            raise
        return text

    started = time.monotonic()
    result = asyncio.run(
        _dispatcher(slow, timeout=0.01).dispatch(
            {"name": "mock_echo", "arguments": {"text": "x"}}
        )
    )

    assert result.status == "timeout"
    assert result.error_code == "TIMEOUT"
    assert cancelled
    assert time.monotonic() - started < 0.5


def test_sync_tool_timeout_is_reported_after_it_returns() -> None:
    result = asyncio.run(
        _dispatcher(
            lambda text: (time.sleep(0.02), text)[1],
            timeout=0.001,
        ).dispatch({"name": "mock_echo", "arguments": {"text": "x"}})
    )

    assert result.status == "timeout"
    assert result.error_code == "TIMEOUT"


def test_oversized_result_is_rejected() -> None:
    result = asyncio.run(
        _dispatcher(
            lambda text: "x" * 100,
            max_result_bytes=16,
        ).dispatch({"name": "mock_echo", "arguments": {"text": "x"}})
    )

    assert result.error_code == "RESULT_TOO_LARGE"
    assert result.result_data is None


def test_dispatch_denies_when_no_permission_checker_is_configured() -> None:
    registry = ToolRegistry()
    registry.register(_definition(lambda text: text))
    dispatcher = ToolDispatcher(registry)

    result = asyncio.run(
        dispatcher.dispatch({"name": "mock_echo", "arguments": {"text": "x"}})
    )

    assert isinstance(dispatcher._permission_checker, DenyByDefaultPermissionChecker)
    assert result.status == "denied"
    assert result.error_code == "PERMISSION_DENIED"


def test_async_permission_checker_is_awaited() -> None:
    class AsyncAllowChecker:
        async def is_allowed(
            self,
            tool: ToolDefinition,
            arguments: Mapping[str, Any],
        ) -> bool:
            await asyncio.sleep(0)
            return True

    result = asyncio.run(
        _dispatcher(
            lambda text: text,
            permission_checker=AsyncAllowChecker(),
        ).dispatch({"name": "mock_echo", "arguments": {"text": "x"}})
    )

    assert result.status == "success"


def test_permission_checker_cannot_mutate_execution_arguments() -> None:
    received: list[str] = []

    class MutatingChecker:
        def is_allowed(
            self,
            tool: ToolDefinition,
            arguments: Mapping[str, Any],
        ) -> bool:
            arguments["text"] = "changed"
            return True

    result = asyncio.run(
        _dispatcher(
            lambda text: received.append(text) or text,
            permission_checker=MutatingChecker(),
        ).dispatch({"name": "mock_echo", "arguments": {"text": "approved"}})
    )

    assert result.status == "success"
    assert received == ["approved"]


def test_malformed_request_is_returned_as_safe_error() -> None:
    result = asyncio.run(
        _dispatcher(lambda text: text).dispatch(
            {"name": "mock_echo", "arguments": {"text": "x"}, "extra": True}
        )
    )

    assert result.error_code == "INVALID_REQUEST"
    assert result.safe_error_message == "Tool request is malformed."
