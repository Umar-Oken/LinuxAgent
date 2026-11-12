"""Local, structured audit logging with redaction, rotation, and retention."""

from __future__ import annotations

import json
import logging
import math
import os
import re
import stat
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from threading import RLock
from typing import Any, Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from core.config import get_data_dir

PermissionDecision = Literal[
    "allow",
    "deny",
    "require_approval",
    "approved",
    "consumed",
    "rejected",
    "cancelled",
    "not_required",
    "error",
]
ExecutionStatus = Literal[
    "success",
    "error",
    "timeout",
    "denied",
    "approved",
    "consumed",
    "rejected",
    "cancelled",
    "expired",
]

_SECRET_KEY = re.compile(
    r"(?i)(password|passwd|secret|token|api[_-]?key|authorization|cookie|credential"
    r"|private[_-]?key|ssh[_-]?key|access[_-]?key|refresh[_-]?token|bearer)"
)
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)(\b(?:password|passwd|secret|token|api[_-]?key|authorization|cookie"
    r"|credential|private[_-]?key|ssh[_-]?key|access[_-]?key|refresh[_-]?token)\b"
    r"\s*[:=]\s*)([^\r\n,;]+)"
)
_BEARER_TOKEN = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*")
_PEM_BLOCK = re.compile(
    r"-----BEGIN [A-Z0-9 ]*(?:PRIVATE KEY|CERTIFICATE)[A-Z0-9 ]*-----"
    r".*?-----END [A-Z0-9 ]*(?:PRIVATE KEY|CERTIFICATE)[A-Z0-9 ]*-----",
    re.IGNORECASE | re.DOTALL,
)
_LOCAL_PATH = re.compile(r"(?<![\w:])/(?:[A-Za-z0-9._~-]+/)*[A-Za-z0-9._~-]+")
_CONTENT_KEYS = {
    "content",
    "text",
    "document",
    "body",
    "prompt",
    "paragraphs",
    "tables",
    "headers",
    "rows",
    "sheets",
}
_PATH_KEYS = {"path", "folder", "filename", "file", "target"}
_MAX_VALUE_CHARS = 256


class _StrictRotatingFileHandler(RotatingFileHandler):
    def _open(self):
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_CLOEXEC
        flags |= getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(self.baseFilename, flags, 0o600)
        try:
            os.fchmod(fd, 0o600)
            return os.fdopen(fd, self.mode, encoding=self.encoding)
        except Exception:
            os.close(fd)
            raise

    def handleError(self, record: logging.LogRecord) -> None:
        error = record.exc_info[1] if record.exc_info else None
        if error is not None:
            raise error
        raise OSError("audit log write failed")


class AuditEvent(BaseModel):
    """Validated structured audit record without tool result contents."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    timestamp: datetime
    session_id: str = Field(min_length=1, max_length=128)
    task_id: str = Field(min_length=1, max_length=128)
    event_type: Literal["tool", "approval", "exception"]
    tool_name: str = Field(min_length=1, max_length=128)
    sanitized_arguments: dict[str, Any] = Field(default_factory=dict)
    permission_decision: PermissionDecision
    execution_status: ExecutionStatus
    duration_seconds: float = Field(ge=0)
    error_code: str | None = Field(default=None, max_length=64)
    approval_id: str | None = Field(default=None, max_length=128)


class AuditLogger:
    """Append JSONL audit events to a private, locally rotated file."""

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        session_id: str | None = None,
        retention_days: int = 30,
        max_bytes: int = 5 * 1024 * 1024,
        backup_count: int = 5,
        clock=time.time,
    ) -> None:
        if retention_days < 1:
            raise ValueError("retention_days must be at least one")
        if max_bytes < 1 or backup_count < 1:
            raise ValueError("rotation limits must be positive")
        self.path = Path(path) if path is not None else get_data_dir() / "audit.jsonl"
        self.session_id = session_id or str(uuid4())
        self.retention_days = retention_days
        self._clock = clock
        self._lock = RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)
        if self.path.is_symlink():
            raise ValueError("audit log path must not be a symbolic link")
        if self.path.exists() and not self.path.is_file():
            raise ValueError("audit log path must be a regular file")

        self._logger = logging.Logger(f"personal_ai_audit_{uuid4()}")
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False
        previous_umask = os.umask(0o077)
        try:
            handler = _StrictRotatingFileHandler(
                self.path,
                maxBytes=max_bytes,
                backupCount=backup_count,
                encoding="utf-8",
                delay=False,
            )
        finally:
            os.umask(previous_umask)
        handler.setFormatter(logging.Formatter("%(message)s"))
        self._logger.addHandler(handler)
        self._secure_log_files()
        self._prune_expired()

    def log_tool(
        self,
        *,
        task_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        permission_decision: PermissionDecision,
        execution_status: ExecutionStatus,
        duration_seconds: float,
        error_code: str | None = None,
    ) -> None:
        self._write(
            AuditEvent(
                timestamp=datetime.now(timezone.utc),
                session_id=self.session_id,
                task_id=task_id,
                event_type="tool",
                tool_name=tool_name,
                sanitized_arguments=sanitize_arguments(arguments),
                permission_decision=permission_decision,
                execution_status=execution_status,
                duration_seconds=max(0.0, duration_seconds),
                error_code=error_code,
            )
        )

    def log_approval(
        self,
        *,
        task_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        decision: PermissionDecision,
        status: ExecutionStatus,
        duration_seconds: float,
        error_code: str | None = None,
        approval_id: str | None = None,
    ) -> None:
        self._write(
            AuditEvent(
                timestamp=datetime.now(timezone.utc),
                session_id=self.session_id,
                task_id=task_id,
                event_type="approval",
                tool_name=tool_name,
                sanitized_arguments=sanitize_arguments(arguments),
                permission_decision=decision,
                execution_status=status,
                duration_seconds=max(0.0, duration_seconds),
                error_code=error_code,
                approval_id=approval_id,
            )
        )

    def log_exception(
        self,
        *,
        task_id: str,
        component: str,
        error_code: str,
    ) -> None:
        self._write(
            AuditEvent(
                timestamp=datetime.now(timezone.utc),
                session_id=self.session_id,
                task_id=task_id,
                event_type="exception",
                tool_name=component,
                permission_decision="error",
                execution_status="error",
                duration_seconds=0,
                error_code=error_code,
            )
        )

    def close(self) -> None:
        with self._lock:
            for handler in self._logger.handlers[:]:
                handler.flush()
                handler.close()
                self._logger.removeHandler(handler)

    def _write(self, event: AuditEvent) -> None:
        serialized = json.dumps(
            event.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        with self._lock:
            self._logger.info(serialized)
            self._secure_log_files()
            self._prune_expired()

    def _log_files(self) -> list[Path]:
        candidates = [self.path]
        candidates.extend(self.path.parent.glob(f"{self.path.name}.[0-9]*"))
        return [
            candidate
            for candidate in candidates
            if candidate == self.path
            or candidate.name[len(self.path.name) + 1 :].isdigit()
        ]

    def _prune_expired(self) -> None:
        threshold = self._clock() - self.retention_days * 86400
        for candidate in self._log_files():
            try:
                details = candidate.lstat()
                if stat.S_ISREG(details.st_mode) and details.st_mtime < threshold:
                    candidate.unlink()
            except OSError:
                continue

    def _secure_log_files(self) -> None:
        for candidate in self._log_files():
            try:
                fd = os.open(
                    candidate,
                    os.O_RDONLY
                    | os.O_CLOEXEC
                    | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_NONBLOCK", 0),
                )
            except OSError:
                continue
            try:
                if stat.S_ISREG(os.fstat(fd).st_mode):
                    os.fchmod(fd, 0o600)
            except OSError:
                continue
            finally:
                os.close(fd)


def sanitize_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    """Redact secret values and omit the contents of documents and file text."""
    sanitized: dict[str, Any] = {}
    for key, value in arguments.items():
        normalized_key = key.casefold()
        if _SECRET_KEY.search(normalized_key):
            sanitized[key] = "[REDACTED]"
        elif normalized_key in _CONTENT_KEYS:
            length = len(value) if isinstance(value, (str, list, tuple, dict)) else 0
            sanitized[key] = f"[CONTENT OMITTED: {length} units]"
        elif normalized_key in _PATH_KEYS and isinstance(value, str):
            sanitized[key] = "[PATH OMITTED]"
        else:
            sanitized[key] = _sanitize_value(value)
    return sanitized


def _redact_url_query(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return value
    query = []
    for key, item in parse_qsl(parsed.query, keep_blank_values=True):
        normalized_key = key.casefold().replace("-", "_")
        sensitive = normalized_key in {
            "key",
            "api_key",
            "apikey",
            "auth",
            "authorization",
            "token",
            "access_token",
            "refresh_token",
            "client_secret",
            "password",
            "signature",
            "sig",
        } or normalized_key.endswith(
            ("_key", "_token", "_secret", "_password", "_signature", "_credential")
        )
        query.append((key, "[REDACTED]" if sensitive else item))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))


def sanitize_text(value: str, *, max_chars: int = _MAX_VALUE_CHARS) -> str:
    """Redact secrets and local paths from a bounded source or diff excerpt."""
    if max_chars < 1:
        raise ValueError("max_chars must be greater than zero")
    safe = _SECRET_ASSIGNMENT.sub(r"\1[REDACTED]", value)
    safe = _BEARER_TOKEN.sub("******", safe)
    safe = _PEM_BLOCK.sub("[REDACTED PEM BLOCK]", safe)
    if safe.startswith(("http://", "https://")):
        safe = _redact_url_query(safe)
    safe = _LOCAL_PATH.sub("[PATH OMITTED]", safe)
    if len(safe) > max_chars:
        return safe[:max_chars] + "…[TRUNCATED]"
    return safe


def _sanitize_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return sanitize_arguments(dict(value))
    if isinstance(value, (list, tuple)):
        return [_sanitize_value(item) for item in value[:100]]
    if isinstance(value, str):
        safe = _SECRET_ASSIGNMENT.sub(r"\1[REDACTED]", value)
        safe = _BEARER_TOKEN.sub("Bearer [REDACTED]", safe)
        if safe.startswith(("http://", "https://")):
            safe = _redact_url_query(safe)
            safe = _LOCAL_PATH.sub("[PATH OMITTED]", safe)
            if len(safe) > _MAX_VALUE_CHARS:
                return safe[:_MAX_VALUE_CHARS] + "…[TRUNCATED]"
            return safe
        safe = _LOCAL_PATH.sub("[PATH OMITTED]", safe)
        if len(safe) > _MAX_VALUE_CHARS:
            return safe[:_MAX_VALUE_CHARS] + "…[TRUNCATED]"
        return safe
    if isinstance(value, float) and not math.isfinite(value):
        return "[NON-FINITE VALUE OMITTED]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return f"<{type(value).__name__}>"
