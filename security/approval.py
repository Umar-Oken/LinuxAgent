"""One-time, argument-bound approvals provided by trusted user interfaces."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from abc import ABC, abstractmethod
from enum import StrEnum
from threading import RLock
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from security.audit_logger import AuditLogger

class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    CONSUMED = "consumed"


class ApprovalRequest(BaseModel):
    """Human-readable description of the exact action requiring approval."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    approval_id: str = Field(min_length=1)
    action: str = Field(min_length=1)
    target_resource: str = Field(min_length=1)
    potential_impact: str = Field(min_length=1)


class ApprovalResponse(BaseModel):
    """Trusted interface response tied to the approval currently displayed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    approval_id: str = Field(min_length=1)
    decision: Literal["approve", "deny", "cancel"]


class TrustedApprovalInterface(ABC):
    """Explicit trust boundary for collecting approval from a user."""

    @abstractmethod
    def request_approval(self, request: ApprovalRequest) -> ApprovalResponse:
        """Return a decision for this exact request, not model-generated text."""


@dataclass(frozen=True)
class _ApprovalGrant:
    fingerprint: str
    expires_at: float
    tool_name: str
    arguments: dict[str, object]
    task_id: str


class ApprovalManager:
    """Track approval state and issue one-use grants with a bounded lifetime."""

    def __init__(
        self,
        interface: TrustedApprovalInterface | None = None,
        *,
        ttl_seconds: float = 120.0,
        audit_logger: AuditLogger | None = None,
        clock=time.monotonic,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be greater than zero")
        if interface is not None and not isinstance(interface, TrustedApprovalInterface):
            raise TypeError("approval interface must be a TrustedApprovalInterface")
        self._interface = interface
        self._ttl_seconds = ttl_seconds
        self._audit_logger = audit_logger
        self._clock = clock
        self._lock = RLock()
        self._states: dict[str, ApprovalStatus] = {}
        self._grants: dict[str, _ApprovalGrant] = {}

    def request(
        self,
        *,
        tool_name: str,
        arguments: dict[str, object],
        action: str,
        target_resource: str,
        potential_impact: str,
        task_id: str = "unassigned",
    ) -> tuple[str | None, ApprovalStatus]:
        """Ask the trusted interface and retain an approval only for this call."""
        approval_id = str(uuid4())
        if self._interface is None:
            logged = self._log_approval(
                approval_id=approval_id,
                tool_name=tool_name,
                arguments=arguments,
                task_id=task_id,
                decision="deny",
                status=ApprovalStatus.DENIED,
                duration=0,
            )
            return (approval_id if logged else None), ApprovalStatus.DENIED
        request = ApprovalRequest(
            approval_id=approval_id,
            action=action,
            target_resource=target_resource,
            potential_impact=potential_impact,
        )
        with self._lock:
            self._states[approval_id] = ApprovalStatus.PENDING

        started = self._clock()
        try:
            response = self._interface.request_approval(request)
        except Exception:
            with self._lock:
                self._states[approval_id] = ApprovalStatus.DENIED
            self._log_approval(
                approval_id=approval_id,
                tool_name=tool_name,
                arguments=arguments,
                task_id=task_id,
                decision="deny",
                status=ApprovalStatus.DENIED,
                duration=max(0.0, self._clock() - started),
            )
            return approval_id, ApprovalStatus.DENIED

        if not isinstance(response, ApprovalResponse) or response.approval_id != approval_id:
            decision = ApprovalStatus.DENIED
        elif response.decision == "approve":
            decision = ApprovalStatus.APPROVED
        elif response.decision == "cancel":
            decision = ApprovalStatus.CANCELLED
        else:
            decision = ApprovalStatus.DENIED

        with self._lock:
            if self._states.get(approval_id) == ApprovalStatus.CANCELLED:
                decision = ApprovalStatus.CANCELLED
            else:
                self._states[approval_id] = decision
            if decision == ApprovalStatus.APPROVED:
                self._grants[approval_id] = _ApprovalGrant(
                    fingerprint=self._fingerprint(tool_name, arguments),
                    expires_at=self._clock() + self._ttl_seconds,
                    tool_name=tool_name,
                    arguments=dict(arguments),
                    task_id=task_id,
                )
        if not self._log_approval(
            approval_id=approval_id,
            tool_name=tool_name,
            arguments=arguments,
            task_id=task_id,
            decision=decision,
            duration=max(0.0, self._clock() - started),
        ):
            self.cancel(approval_id)
            return approval_id, ApprovalStatus.DENIED
        return approval_id, decision

    def consume(
        self,
        approval_id: str,
        *,
        tool_name: str,
        arguments: dict[str, object],
        task_id: str = "unassigned",
    ) -> bool:
        """Consume a matching, unexpired approval exactly once."""
        with self._lock:
            grant = self._grants.pop(approval_id, None)
            if grant is None:
                return False
            if self._clock() >= grant.expires_at:
                self._states[approval_id] = ApprovalStatus.EXPIRED
                result = False
                status = ApprovalStatus.EXPIRED
            elif grant.fingerprint != self._fingerprint(tool_name, arguments):
                self._states[approval_id] = ApprovalStatus.CANCELLED
                result = False
                status = ApprovalStatus.CANCELLED
            else:
                self._states[approval_id] = ApprovalStatus.CONSUMED
                result = True
                status = ApprovalStatus.CONSUMED
        return self._log_approval(
            approval_id=approval_id,
            tool_name=grant.tool_name,
            arguments=grant.arguments,
            task_id=grant.task_id if task_id == "unassigned" else task_id,
            decision="approved" if result else "cancelled",
            status=status,
            duration=0,
        ) and result

    def cancel(self, approval_id: str) -> bool:
        """Revoke an unconsumed grant."""
        with self._lock:
            if self._states.get(approval_id) not in {
                ApprovalStatus.PENDING,
                ApprovalStatus.APPROVED,
            }:
                return False
            grant = self._grants.pop(approval_id, None)
            self._states[approval_id] = ApprovalStatus.CANCELLED
        if grant is not None:
            return self._log_approval(
                approval_id=approval_id,
                tool_name=grant.tool_name,
                arguments=grant.arguments,
                task_id=grant.task_id,
                decision="cancelled",
                status=ApprovalStatus.CANCELLED,
                duration=0,
            )
        return True

    def status(self, approval_id: str) -> ApprovalStatus | None:
        """Return the current state, expiring idle approvals on inspection."""
        with self._lock:
            grant = self._grants.get(approval_id)
            if grant is not None and self._clock() >= grant.expires_at:
                self._grants.pop(approval_id, None)
                self._states[approval_id] = ApprovalStatus.EXPIRED
                expired = grant
            else:
                expired = None
            status = self._states.get(approval_id)
        if expired is not None:
            self._log_approval(
                approval_id=approval_id,
                tool_name=expired.tool_name,
                arguments=expired.arguments,
                task_id=expired.task_id,
                decision="cancelled",
                status=ApprovalStatus.EXPIRED,
                duration=0,
            )
        return status

    def _log_approval(
        self,
        *,
        approval_id: str,
        tool_name: str,
        arguments: dict[str, object],
        task_id: str,
        decision: str,
        status: ApprovalStatus | None = None,
        duration: float,
    ) -> bool:
        if self._audit_logger is None:
            return True
        try:
            if isinstance(decision, ApprovalStatus):
                decision_value = {
                    ApprovalStatus.APPROVED: "approved",
                    ApprovalStatus.DENIED: "deny",
                    ApprovalStatus.CANCELLED: "cancelled",
                    ApprovalStatus.EXPIRED: "cancelled",
                    ApprovalStatus.CONSUMED: "approved",
                }.get(decision, "deny")
            else:
                decision_value = decision
            self._audit_logger.log_approval(
                task_id=task_id,
                tool_name=tool_name,
                arguments=arguments,
                decision=decision_value,  # type: ignore[arg-type]
                status=(status or _status_for_decision(decision)).value,
                duration_seconds=duration,
                approval_id=approval_id,
            )
            return True
        except Exception:
            return False

    @staticmethod
    def _fingerprint(tool_name: str, arguments: dict[str, object]) -> str:
        payload = json.dumps(
            {"tool_name": tool_name, "arguments": arguments},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _status_for_decision(decision: str) -> ApprovalStatus:
    return {
        "approved": ApprovalStatus.APPROVED,
        "rejected": ApprovalStatus.DENIED,
        "cancelled": ApprovalStatus.CANCELLED,
    }.get(decision, ApprovalStatus.DENIED)
