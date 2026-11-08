"""Tests for trusted, argument-bound approvals and tool policy."""

from typing import Any
import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from core.tool_registry import ToolDefinition
from security.approval import (
    ApprovalManager,
    ApprovalRequest,
    ApprovalResponse,
    ApprovalStatus,
    TrustedApprovalInterface,
)
from security.audit_logger import AuditLogger
from security.permissions import (
    PermissionEngine,
    PermissionContext,
    PermissionLevel,
    PermissionPolicy,
    PolicyDecision,
    PolicyOutcome,
)


class EmptyArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ValueArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    value: int


class FixedInterface(TrustedApprovalInterface):
    def __init__(
        self,
        decision: str = "approve",
        *,
        clock_advance: Any = None,
        wrong_id: bool = False,
    ) -> None:
        self.decision = decision
        self.clock_advance = clock_advance
        self.wrong_id = wrong_id
        self.requests: list[ApprovalRequest] = []

    def request_approval(self, request: ApprovalRequest) -> ApprovalResponse:
        self.requests.append(request)
        if self.clock_advance is not None:
            self.clock_advance()
        return ApprovalResponse(
            approval_id="not-the-request" if self.wrong_id else request.approval_id,
            decision=self.decision,  # type: ignore[arg-type]
        )


def _tool(permission: PermissionLevel) -> ToolDefinition:
    return ToolDefinition(
        name="mock_action",
        description="A test action.",
        category="system",
        argument_schema=EmptyArguments.model_json_schema(),
        argument_model=EmptyArguments,
        permission_level=permission.value,
        execution_timeout=1,
        implementation=lambda: "done",
    )


def _context(
    tool: ToolDefinition,
    arguments: dict[str, Any],
    approval_policy: str = "writes",
) -> PermissionContext:
    level = PermissionLevel(tool.permission_level)
    return PermissionContext(
        tool_identifier=tool.name,
        requested_arguments=arguments,
        target_resource=tool.name,
        permission_level=level,
        approval_policy=approval_policy,  # type: ignore[arg-type]
    )


def test_read_only_is_allowed_under_writes_policy_without_approval() -> None:
    interface = FixedInterface()
    engine = PermissionEngine(
        PermissionPolicy(approval_policy="writes"),
        ApprovalManager(interface),
    )
    tool = _tool(PermissionLevel.READ_ONLY)

    assert engine.prepare(tool, {}) == PolicyDecision.ALLOW
    assert engine.authorize(tool, {})
    assert interface.requests == []


def test_controlled_action_requires_trusted_approval_and_consumes_it() -> None:
    interface = FixedInterface()
    manager = ApprovalManager(interface)
    engine = PermissionEngine(PermissionPolicy(), manager)
    tool = _tool(PermissionLevel.CONTROLLED)

    assert engine.prepare(tool, {}) == PolicyDecision.ALLOW
    request = interface.requests[0]
    assert request.action == "mock_action arguments={}"
    assert request.target_resource == "mock_action"
    assert request.potential_impact
    assert request.approval_id
    assert engine.authorize(tool, {})
    assert manager.status(request.approval_id) == ApprovalStatus.CONSUMED
    assert not engine.authorize(tool, {})


def test_denial_and_missing_trusted_interface_fail_closed() -> None:
    tool = _tool(PermissionLevel.CONTROLLED)
    denied = PermissionEngine(
        PermissionPolicy(),
        ApprovalManager(FixedInterface("deny")),
    )
    no_interface = PermissionEngine(PermissionPolicy(), ApprovalManager())

    assert denied.prepare(tool, {}) == PolicyDecision.DENY
    assert no_interface.prepare(tool, {}) == PolicyDecision.DENY


def test_approval_expires() -> None:
    now = [10.0]
    interface = FixedInterface()
    manager = ApprovalManager(interface, ttl_seconds=3, clock=lambda: now[0])
    approval_id, status = manager.request(
        tool_name="mock_action",
        arguments={"value": 1},
        action='mock_action arguments={"value":1}',
        target_resource="target",
        potential_impact="change",
    )
    assert status == ApprovalStatus.APPROVED
    assert approval_id is not None
    now[0] = 13.0

    assert not manager.consume(
        approval_id,
        tool_name="mock_action",
        arguments={"value": 1},
    )
    assert manager.status(approval_id) == ApprovalStatus.EXPIRED


def test_arguments_cannot_be_changed_after_approval() -> None:
    interface = FixedInterface()
    manager = ApprovalManager(interface)
    policy = PermissionPolicy()
    tool = ToolDefinition(
        name="mock_action",
        description="A test action.",
        category="system",
        argument_schema={
            "type": "object",
            "properties": {"value": {"type": "integer"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        argument_model=ValueArguments,
        permission_level="CONTROLLED",
        execution_timeout=1,
        implementation=lambda value: value,
    )
    engine = PermissionEngine(policy, manager)

    assert engine.prepare(tool, {"value": 1}) == PolicyDecision.ALLOW
    assert not engine.authorize(tool, {"value": 2})
    approval_id = interface.requests[0].approval_id
    assert manager.status(approval_id) == ApprovalStatus.CANCELLED


def test_prohibited_action_is_never_sent_for_approval() -> None:
    interface = FixedInterface()
    tool = _tool(PermissionLevel.PROHIBITED)
    engine = PermissionEngine(PermissionPolicy(), ApprovalManager(interface))

    assert PermissionPolicy().evaluate(_context(tool, {})) == PolicyOutcome.DENY
    assert engine.prepare(tool, {}) == PolicyDecision.DENY
    assert interface.requests == []


def test_read_only_requires_approval_when_policy_is_always() -> None:
    interface = FixedInterface()
    engine = PermissionEngine(
        PermissionPolicy(approval_policy="always"),
        ApprovalManager(interface),
    )
    tool = _tool(PermissionLevel.READ_ONLY)

    assert engine.prepare(tool, {}) == PolicyDecision.ALLOW
    assert len(interface.requests) == 1
    assert engine.authorize(tool, {})


def test_sensitive_action_requires_trusted_approval() -> None:
    interface = FixedInterface()
    engine = PermissionEngine(
        PermissionPolicy(),
        ApprovalManager(interface),
    )
    tool = _tool(PermissionLevel.SENSITIVE)

    assert engine.prepare(tool, {}) == PolicyDecision.ALLOW
    assert len(interface.requests) == 1
    assert "Sensitive data" in interface.requests[0].potential_impact
    assert engine.authorize(tool, {})


def test_user_policy_can_prohibit_or_require_approval_for_specific_tool() -> None:
    read_tool = _tool(PermissionLevel.READ_ONLY)
    prohibited = PermissionPolicy(prohibited_tools=frozenset({"mock_action"}))
    approval_required = PermissionPolicy(
        approval_required_tools=frozenset({"mock_action"})
    )

    assert prohibited.evaluate(_context(read_tool, {})) == PolicyOutcome.DENY
    assert (
        approval_required.evaluate(_context(read_tool, {}))
        == PolicyOutcome.REQUIRE_APPROVAL
    )


def test_policy_configuration_is_snapshotted_and_invalid_context_is_denied() -> None:
    tool = _tool(PermissionLevel.READ_ONLY)
    prohibited_tools = {"mock_action"}
    policy = PermissionPolicy(prohibited_tools=prohibited_tools)  # type: ignore[arg-type]
    prohibited_tools.clear()

    assert policy.evaluate(_context(tool, {})) == PolicyOutcome.DENY
    invalid_context = _context(tool, {})
    invalid_context = PermissionContext(
        tool_identifier=invalid_context.tool_identifier,
        requested_arguments=invalid_context.requested_arguments,
        target_resource=invalid_context.target_resource,
        permission_level=invalid_context.permission_level,
        approval_policy="unexpected",  # type: ignore[arg-type]
    )
    assert policy.evaluate(invalid_context) == PolicyOutcome.DENY


def test_untrusted_or_mismatched_interface_response_is_denied() -> None:
    interface = FixedInterface(wrong_id=True)
    manager = ApprovalManager(interface)
    approval_id, status = manager.request(
        tool_name="mock_action",
        arguments={},
        action="mock_action arguments={}",
        target_resource="target",
        potential_impact="change",
    )

    assert approval_id is not None
    assert status == ApprovalStatus.DENIED
    assert not manager.consume(
        approval_id,
        tool_name="mock_action",
        arguments={},
    )


def test_cancel_revokes_an_approved_grant() -> None:
    manager = ApprovalManager(FixedInterface())
    approval_id, status = manager.request(
        tool_name="mock_action",
        arguments={},
        action="mock_action arguments={}",
        target_resource="target",
        potential_impact="change",
    )
    assert status == ApprovalStatus.APPROVED
    assert approval_id is not None

    assert manager.cancel(approval_id)
    assert manager.status(approval_id) == ApprovalStatus.CANCELLED
    assert not manager.consume(approval_id, tool_name="mock_action", arguments={})


def test_approval_decisions_and_consumption_are_audited(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    audit_logger = AuditLogger(path)
    manager = ApprovalManager(FixedInterface(), audit_logger=audit_logger)
    try:
        approval_id, decision = manager.request(
            tool_name="mock_action",
            arguments={"password": "do-not-log"},
            action="mock action",
            target_resource="mock_action",
            potential_impact="change",
            task_id="approval-task",
        )
        assert decision == ApprovalStatus.APPROVED
        assert approval_id is not None
        assert manager.consume(
            approval_id,
            tool_name="mock_action",
            arguments={"password": "do-not-log"},
            task_id="approval-task",
        )
    finally:
        audit_logger.close()

    events = [json.loads(line) for line in path.read_text().splitlines()]
    assert [event["event_type"] for event in events] == ["approval", "approval"]
    assert [event["execution_status"] for event in events] == [
        "approved",
        "consumed",
    ]
    assert all(event["approval_id"] == approval_id for event in events)
    assert "do-not-log" not in path.read_text()


def test_missing_approval_interface_is_audited_as_denied(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    audit_logger = AuditLogger(path)
    try:
        approval_id, status = ApprovalManager(
            audit_logger=audit_logger
        ).request(
            tool_name="mock_action",
            arguments={},
            action="mock action",
            target_resource="mock_action",
            potential_impact="change",
            task_id="approval-task",
        )
    finally:
        audit_logger.close()

    event = json.loads(path.read_text())
    assert approval_id is not None
    assert status == ApprovalStatus.DENIED
    assert event["permission_decision"] == "deny"
    assert event["execution_status"] == "denied"


def test_approval_is_denied_when_audit_write_fails(tmp_path: Path) -> None:
    audit_logger = AuditLogger(tmp_path / "audit.jsonl")
    audit_logger._logger.info = lambda *_args, **_kwargs: (_ for _ in ()).throw(  # type: ignore[method-assign]
        OSError("private filesystem detail")
    )
    manager = ApprovalManager(FixedInterface(), audit_logger=audit_logger)
    try:
        _, status = manager.request(
            tool_name="mock_action",
            arguments={},
            action="mock action",
            target_resource="mock_action",
            potential_impact="change",
        )
    finally:
        audit_logger.close()

    assert status == ApprovalStatus.DENIED
