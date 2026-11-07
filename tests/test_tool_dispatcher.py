"""Tests for safe, permission-gated tool dispatch."""

import asyncio
import json
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from core.tool_dispatcher import (
    DenyByDefaultPermissionChecker,
    ToolDispatcher,
)
from core.tool_registry import ToolDefinition, ToolRegistry
from security.approval import (
    ApprovalManager,
    ApprovalRequest,
    ApprovalResponse,
    TrustedApprovalInterface,
)
from security.audit_logger import AuditLogger
from security.permissions import PermissionEngine, PermissionPolicy, PolicyDecision


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
        permission_level="READ_ONLY",
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


def test_dispatch_requires_approval_and_rechecks_before_execution() -> None:
    called = False

    def implementation(text: str) -> str:
        nonlocal called
        called = True
        return text

    class ApproveInterface(TrustedApprovalInterface):
        def request_approval(self, request: ApprovalRequest) -> ApprovalResponse:
            assert request.action == 'mock_echo arguments={"text":"approved"}'
            assert request.target_resource == "mock_echo"
            assert request.potential_impact
            return ApprovalResponse(
                approval_id=request.approval_id,
                decision="approve",
            )

    registry = ToolRegistry()
    definition = _definition(implementation)
    definition = definition.model_copy(update={"permission_level": "CONTROLLED"})
    registry.register(definition)
    dispatcher = ToolDispatcher(
        registry,
        permission_checker=PermissionEngine(
            PermissionPolicy(),
            ApprovalManager(ApproveInterface()),
        ),
    )

    result = asyncio.run(
        dispatcher.dispatch(
            {"name": "mock_echo", "arguments": {"text": "approved"}}
        )
    )

    assert result.status == "success"
    assert called


def test_dispatch_denies_if_pre_execution_recheck_fails() -> None:
    called = False

    def implementation(text: str) -> str:
        nonlocal called
        called = True
        return text

    class RecheckingPermission:
        def __init__(self) -> None:
            self.checks = 0

        def prepare(
            self,
            tool: ToolDefinition,
            arguments: Mapping[str, Any],
        ) -> PolicyDecision:
            self.checks += 1
            return PolicyDecision.ALLOW

        def authorize(
            self,
            tool: ToolDefinition,
            arguments: Mapping[str, Any],
        ) -> bool:
            self.checks += 1
            return False

    result = asyncio.run(
        _dispatcher(
            implementation,
            permission_checker=RecheckingPermission(),
        ).dispatch({"name": "mock_echo", "arguments": {"text": "x"}})
    )

    assert result.status == "denied"
    assert result.error_code == "PERMISSION_DENIED"
    assert not called


def test_dispatch_audits_success_denial_and_execution_error(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    audit_logger = AuditLogger(path)
    try:
        success = _dispatcher(
            lambda text: text,
            permission_checker=AllowPermissionChecker(),
        )
        success._audit_logger = audit_logger
        denied = _dispatcher(
            lambda text: text,
            permission_checker=DenyByDefaultPermissionChecker(),
        )
        denied._audit_logger = audit_logger
        failed = _dispatcher(
            lambda text: (_ for _ in ()).throw(RuntimeError("internal detail")),
            permission_checker=AllowPermissionChecker(),
        )
        failed._audit_logger = audit_logger

        asyncio.run(success.dispatch(
            {"name": "mock_echo", "arguments": {"text": "safe"}},
            task_id="success-task",
        ))
        asyncio.run(denied.dispatch(
            {"name": "mock_echo", "arguments": {"text": "safe"}},
            task_id="denied-task",
        ))
        failure = asyncio.run(failed.dispatch(
            {"name": "mock_echo", "arguments": {"text": "safe"}},
            task_id="failed-task",
        ))
    finally:
        audit_logger.close()

    events = [json.loads(line) for line in path.read_text().splitlines()]
    assert [event["execution_status"] for event in events] == [
        "success",
        "denied",
        "error",
    ]
    assert [event["task_id"] for event in events] == [
        "success-task",
        "denied-task",
        "failed-task",
    ]
    assert "internal detail" not in path.read_text()
    assert failure.safe_error_message == "Tool execution failed."
