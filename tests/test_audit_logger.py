import json
import os
from pathlib import Path

import pytest

from security.audit_logger import AuditLogger, sanitize_arguments


def _events(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_tool_event_records_required_fields_and_redacts_sensitive_values(
    tmp_path: Path,
) -> None:
    path = tmp_path / "audit.jsonl"
    logger = AuditLogger(path, session_id="session-1")
    try:
        logger.log_tool(
            task_id="task-1",
            tool_name="read_text",
            arguments={
                "path": "/home/user/private.txt",
                "content": "document contents",
                "password": "top secret",
                "query": "token=abc123",
            },
            permission_decision="allow",
            execution_status="success",
            duration_seconds=0.25,
        )
    finally:
        logger.close()

    event = _events(path)[0]
    assert event["session_id"] == "session-1"
    assert event["task_id"] == "task-1"
    assert event["tool_name"] == "read_text"
    assert event["permission_decision"] == "allow"
    assert event["execution_status"] == "success"
    assert event["duration_seconds"] == 0.25
    arguments = event["sanitized_arguments"]
    assert arguments == {
        "path": "[PATH OMITTED]",
        "content": "[CONTENT OMITTED: 17 units]",
        "password": "[REDACTED]",
        "query": "token=[REDACTED]",
    }
    assert "document contents" not in path.read_text()
    assert "top secret" not in path.read_text()


def test_denied_and_exception_events_are_structured(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    logger = AuditLogger(path)
    try:
        logger.log_tool(
            task_id="task",
            tool_name="blocked",
            arguments={},
            permission_decision="deny",
            execution_status="denied",
            duration_seconds=0,
            error_code="PERMISSION_DENIED",
        )
        logger.log_exception(
            task_id="task",
            component="OllamaClient",
            error_code="LLM_ERROR",
        )
    finally:
        logger.close()

    events = _events(path)
    assert events[0]["execution_status"] == "denied"
    assert events[0]["error_code"] == "PERMISSION_DENIED"
    assert events[1]["event_type"] == "exception"
    assert events[1]["error_code"] == "LLM_ERROR"


def test_sanitizer_redacts_nested_secrets_and_omits_file_content() -> None:
    sanitized = sanitize_arguments(
        {"metadata": {"authorization": "Bearer secret", "text": ["private"]}}
    )

    assert sanitized == {
        "metadata": {
            "authorization": "[REDACTED]",
            "text": "[CONTENT OMITTED: 1 units]",
        }
    }


def test_rotation_keeps_active_and_backup_logs_private(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    logger = AuditLogger(path, max_bytes=300, backup_count=2)
    try:
        for index in range(10):
            logger.log_tool(
                task_id=f"task-{index}",
                tool_name="mock",
                arguments={"value": "x" * 100},
                permission_decision="allow",
                execution_status="success",
                duration_seconds=0.01,
            )
    finally:
        logger.close()

    rotated = list(tmp_path.glob("audit.jsonl*"))
    assert len(rotated) >= 2
    assert all(os.stat(file).st_mode & 0o777 == 0o600 for file in rotated)


def test_expired_rotated_logs_are_removed(tmp_path: Path) -> None:
    expired = tmp_path / "audit.jsonl.1"
    expired.write_text("old")
    os.utime(expired, (1, 1))
    logger = AuditLogger(tmp_path / "audit.jsonl", retention_days=1)
    try:
        assert not expired.exists()
    finally:
        logger.close()


def test_existing_symlink_log_path_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "target.jsonl"
    target.write_text("untouched")
    link = tmp_path / "audit.jsonl"
    link.symlink_to(target)

    with pytest.raises(ValueError, match="symbolic link"):
        AuditLogger(link)
    assert target.read_text() == "untouched"


def test_sanitizer_redacts_bearer_and_credential_values() -> None:
    sanitized = sanitize_arguments(
        {
            "private_key": "private key material",
            "query": "Bearer abc.def-123",
            "settings": "cookie=session-value; credential=private-value",
        }
    )

    assert sanitized == {
        "private_key": "[REDACTED]",
        "query": "Bearer [REDACTED]",
        "settings": "cookie=[REDACTED]; credential=[REDACTED]",
    }


def test_audit_file_creation_rejects_symlink_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "audit.jsonl"
    original_open = os.open
    target = tmp_path / "target.jsonl"

    def swap_then_open(file: object, flags: int, mode: int = 0o777) -> int:
        if Path(file) == path and not path.exists():
            path.symlink_to(target)
        return original_open(file, flags, mode)

    import security.audit_logger as audit_module

    monkeypatch.setattr(audit_module.os, "open", swap_then_open)
    with pytest.raises(OSError):
        AuditLogger(path)
    assert not target.exists()
