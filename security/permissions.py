"""Deny-by-default tool policy evaluation and approval coordination."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from threading import RLock
from typing import Any, Literal

from core.tool_registry import ToolDefinition
from security.approval import ApprovalManager, ApprovalStatus


class PermissionLevel(StrEnum):
    READ_ONLY = "READ_ONLY"
    CONTROLLED = "CONTROLLED"
    SENSITIVE = "SENSITIVE"
    PROHIBITED = "PROHIBITED"


class PolicyOutcome(StrEnum):
    ALLOW = "allow"
    REQUIRE_APPROVAL = "require_approval"
    DENY = "deny"


class PolicyDecision(StrEnum):
    ALLOW = "allow"
    DENY = "deny"


@dataclass(frozen=True)
class PermissionContext:
    tool_identifier: str
    requested_arguments: Mapping[str, Any]
    target_resource: str
    permission_level: PermissionLevel
    approval_policy: Literal["writes", "always"]


class PermissionPolicy:
    """Evaluate one registered tool invocation against immutable app policy."""

    def __init__(
        self,
        *,
        approval_policy: Literal["writes", "always"] = "writes",
        prohibited_tools: frozenset[str] = frozenset(),
        approval_required_tools: frozenset[str] = frozenset(),
    ) -> None:
        if approval_policy not in {"writes", "always"}:
            raise ValueError("unsupported approval policy")
        if not isinstance(prohibited_tools, (set, frozenset)) or not all(
            isinstance(name, str) and name for name in prohibited_tools
        ):
            raise ValueError("prohibited_tools must contain non-empty tool names")
        if not isinstance(approval_required_tools, (set, frozenset)) or not all(
            isinstance(name, str) and name for name in approval_required_tools
        ):
            raise ValueError("approval_required_tools must contain non-empty tool names")
        self._approval_policy = approval_policy
        self._prohibited_tools = frozenset(prohibited_tools)
        self._approval_required_tools = frozenset(approval_required_tools)

    @property
    def approval_policy(self) -> Literal["writes", "always"]:
        return self._approval_policy

    def evaluate(
        self,
        context: PermissionContext,
    ) -> PolicyOutcome:
        if (
            not isinstance(context.tool_identifier, str)
            or not context.tool_identifier
            or not isinstance(context.target_resource, str)
            or not context.target_resource
            or context.tool_identifier in self._prohibited_tools
            or context.approval_policy not in {"writes", "always"}
        ):
            return PolicyOutcome.DENY
        try:
            _invocation_key(
                context.tool_identifier,
                context.requested_arguments,
            )
        except (TypeError, ValueError, UnicodeError):
            return PolicyOutcome.DENY
        level = context.permission_level
        if level == PermissionLevel.PROHIBITED:
            return PolicyOutcome.DENY
        if (
            context.tool_identifier in self._approval_required_tools
            or level in {PermissionLevel.CONTROLLED, PermissionLevel.SENSITIVE}
        ):
            return PolicyOutcome.REQUIRE_APPROVAL
        if level == PermissionLevel.READ_ONLY:
            if context.approval_policy == "always":
                return PolicyOutcome.REQUIRE_APPROVAL
            return PolicyOutcome.ALLOW
        return PolicyOutcome.DENY


class PermissionEngine:
    """Two-phase authorization with immediate pre-execution revalidation."""

    def __init__(
        self,
        policy: PermissionPolicy,
        approval_manager: ApprovalManager | None = None,
    ) -> None:
        self._policy = policy
        self._approval_manager = approval_manager or ApprovalManager()
        self._prepared: dict[tuple[str, str], list[str]] = {}
        self._lock = RLock()

    def prepare(
        self,
        tool: ToolDefinition,
        arguments: Mapping[str, Any],
        *,
        task_id: str = "unassigned",
    ) -> PolicyDecision:
        """Obtain trusted approval when policy requires it; model text is ignored."""
        snapshot = dict(arguments)
        context = self._context(tool, snapshot)
        if context is None:
            return PolicyDecision.DENY
        outcome = self._policy.evaluate(context)
        if outcome == PolicyOutcome.DENY:
            return PolicyDecision.DENY
        if outcome == PolicyOutcome.ALLOW:
            return PolicyDecision.ALLOW

        approval_id, status = self._approval_manager.request(
            tool_name=tool.name,
            arguments=snapshot,
            action=_action_description(tool.name, snapshot),
            target_resource=context.target_resource,
            potential_impact=_potential_impact(context.permission_level),
            task_id=task_id,
        )
        if status != ApprovalStatus.APPROVED:
            return PolicyDecision.DENY
        if approval_id is None:
            return PolicyDecision.DENY
        with self._lock:
            self._prepared.setdefault(
                (tool.name, _invocation_key(tool.name, snapshot)),
                [],
            ).append(approval_id)
        return PolicyDecision.ALLOW

    def authorize(
        self,
        tool: ToolDefinition,
        arguments: Mapping[str, Any],
        *,
        task_id: str = "unassigned",
    ) -> bool:
        """Re-evaluate policy and consume approval immediately before execution."""
        snapshot = dict(arguments)
        context = self._context(tool, snapshot)
        if context is None:
            return False
        outcome = self._policy.evaluate(context)
        if outcome == PolicyOutcome.DENY:
            return False
        if outcome == PolicyOutcome.ALLOW:
            return True
        key = (tool.name, _invocation_key(tool.name, snapshot))
        approval_id: str | None = None
        with self._lock:
            approvals = self._prepared.get(key)
            if not approvals:
                stale_ids = [
                    pending_id
                    for (prepared_name, _), approval_ids in self._prepared.items()
                    if prepared_name == tool.name
                    for pending_id in approval_ids
                ]
                for prepared_key in tuple(self._prepared):
                    if prepared_key[0] == tool.name:
                        self._prepared.pop(prepared_key, None)
            else:
                approval_id = approvals.pop(0)
                if not approvals:
                    self._prepared.pop(key, None)
                stale_ids = []
        if approval_id is None:
            for stale_id in stale_ids:
                self._approval_manager.cancel(stale_id)
            return False
        return self._approval_manager.consume(
            approval_id,
            tool_name=tool.name,
            arguments=snapshot,
            task_id=task_id,
        )

    def _context(
        self,
        tool: ToolDefinition,
        arguments: Mapping[str, Any],
    ) -> PermissionContext | None:
        try:
            permission_level = PermissionLevel(tool.permission_level)
        except ValueError:
            return None
        return PermissionContext(
            tool_identifier=tool.name,
            requested_arguments=dict(arguments),
            target_resource=_target_resource(tool.name, arguments),
            permission_level=permission_level,
            approval_policy=self._policy.approval_policy,
        )

    def is_allowed(
        self,
        tool: ToolDefinition,
        arguments: Mapping[str, Any],
        *,
        task_id: str = "unassigned",
    ) -> bool:
        """Single-call compatibility interface for the ToolDispatcher protocol."""
        return self.prepare(
            tool,
            arguments,
            task_id=task_id,
        ) == PolicyDecision.ALLOW and self.authorize(
            tool,
            arguments,
            task_id=task_id,
        )


def _target_resource(tool_name: str, arguments: Mapping[str, Any]) -> str:
    for key in ("path", "folder", "target", "resource", "filename", "city"):
        value = arguments.get(key)
        if isinstance(value, str) and value:
            return value[:256]
    if tool_name == "save_note":
        title = arguments.get("title")
        if isinstance(title, str) and title:
            return f"agent_notes/{title[:200]}"
    return tool_name


def _action_description(tool_name: str, arguments: Mapping[str, Any]) -> str:
    serialized = json.dumps(
        arguments,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return f"{tool_name} arguments={serialized}"


def _potential_impact(level: PermissionLevel) -> str:
    if level == PermissionLevel.SENSITIVE:
        return "Sensitive data may be accessed or changed."
    return "This action may change data or have external effects."


def _invocation_key(tool_name: str, arguments: Mapping[str, Any]) -> str:
    payload = json.dumps(
        {"tool_name": tool_name, "arguments": arguments},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return payload
