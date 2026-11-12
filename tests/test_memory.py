from __future__ import annotations

from contextlib import nullcontext
from typing import Any, Self
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr

from core.config import DatabaseSettings
from core.memory import PersistentMemory
from security.approval import (
    ApprovalManager,
    ApprovalRequest,
    ApprovalResponse,
    TrustedApprovalInterface,
)
from storage.database import UnsupportedSchemaVersionError, apply_migrations


class _Cursor:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection
        self.rowcount = 1
        self.last_query = ""

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, query: str, params: object = None) -> None:
        self.last_query = " ".join(query.split())
        self.connection.executed.append((self.last_query, params))
        if self.last_query.startswith("INSERT INTO agent_memory.schema_migrations"):
            assert isinstance(params, tuple)
            self.connection.versions.add(int(params[0]))

    def fetchall(self) -> list[tuple[Any, ...]]:
        if self.last_query.startswith("SELECT version FROM"):
            return [(version,) for version in sorted(self.connection.versions)]
        if "AS messages" in self.last_query:
            return [
                (
                    "conversation-id",
                    "task-id",
                    "completed",
                    "find files",
                    "Found them.",
                    "now",
                    [{"role": "user", "content": "find files"}],
                )
            ]
        if "to_tsvector" in self.last_query:
            return [("task-id", "conversation-id", "completed", "find files", "Found them.", "now")]
        if "FROM agent_memory.persistent_notes" in self.last_query:
            return [("note-id", "Remember my preference", "now")]
        return []


class _Connection:
    def __init__(self) -> None:
        self.executed: list[tuple[str, object]] = []
        self.versions: set[int] = set()
        self.cursor_instance = _Cursor(self)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def cursor(self) -> _Cursor:
        return self.cursor_instance

    def transaction(self):
        return nullcontext()


class _Approval(TrustedApprovalInterface):
    def __init__(self, decision: str = "approve") -> None:
        self.decision = decision
        self.requests: list[ApprovalRequest] = []

    def request_approval(self, request: ApprovalRequest) -> ApprovalResponse:
        self.requests.append(request)
        return ApprovalResponse(
            approval_id=request.approval_id,
            decision=self.decision,  # type: ignore[arg-type]
        )


def _settings() -> DatabaseSettings:
    return DatabaseSettings(
        name="agent_db",
        user="agent_user",
        password=SecretStr("secret"),
        host="localhost",
        port=5432,
    )


def test_migrations_are_versioned_and_idempotent() -> None:
    connection = _Connection()

    assert apply_migrations(connection) == [1]
    migration_ddl_count = len(connection.executed)
    assert apply_migrations(connection) == []
    assert len(connection.executed) == migration_ddl_count + 4
    executed_sql = "\n".join(query for query, _params in connection.executed)
    for table in (
        "conversations",
        "conversation_messages",
        "task_history",
        "tool_execution_summaries",
        "persistent_notes",
    ):
        assert f"CREATE TABLE agent_memory.{table}" in executed_sql


def test_migration_refuses_a_newer_unknown_schema_version() -> None:
    connection = _Connection()
    connection.versions.add(99)

    with pytest.raises(UnsupportedSchemaVersionError):
        apply_migrations(connection)


def test_task_and_tool_history_store_sanitized_minimal_data() -> None:
    connection = _Connection()
    memory = PersistentMemory(
        _settings(),
        connection_factory=lambda: connection,
        initialize=False,
    )
    task_id = str(uuid4())

    memory.start_task(task_id, "Find files; password=do-not-retain")
    memory.record_tool_execution(
        task_id,
        tool_name="search_files",
        status="success",
        error_code=None,
        duration_seconds=0.25,
    )
    memory.finish_task(
        task_id,
        status="completed",
        response="Found files.",
    )

    executed = connection.executed
    request_insert = next(params for sql, params in executed if "INSERT INTO agent_memory.task_history" in sql)
    assert isinstance(request_insert, tuple)
    assert "do-not-retain" not in request_insert[2]
    tool_insert = next(
        params for sql, params in executed
        if "INSERT INTO agent_memory.tool_execution_summaries" in sql
    )
    assert isinstance(tool_insert, tuple)
    assert isinstance(tool_insert[0], UUID)
    assert tool_insert[1:] == (
        "search_files",
        "success",
        "Tool completed successfully.",
        0.25,
    )
    assert "arguments" not in "\n".join(sql for sql, _params in executed)


def test_recent_conversations_and_task_search_are_bounded_and_parameterized() -> None:
    connection = _Connection()
    memory = PersistentMemory(
        _settings(),
        connection_factory=lambda: connection,
        initialize=False,
    )

    conversations = memory.recent_conversations(limit=3)
    history = memory.search_task_history("files & notes", limit=2)

    assert conversations[0]["messages"][0]["content"] == "find files"
    assert history[0]["status"] == "completed"
    assert connection.executed[-1][1] == ("files & notes", 2)
    with pytest.raises(ValueError):
        memory.search_task_history("   ")
    with pytest.raises(ValueError):
        memory.recent_conversations(limit=51)


def test_personal_note_requires_trusted_explicit_approval() -> None:
    connection = _Connection()
    memory = PersistentMemory(
        _settings(),
        connection_factory=lambda: connection,
        initialize=False,
    )
    denied_interface = _Approval("deny")
    denied = memory.save_persistent_note(
        "Remember my preference",
        approval_manager=ApprovalManager(denied_interface),
    )
    assert denied is None
    assert not any("INSERT INTO agent_memory.persistent_notes" in sql for sql, _ in connection.executed)

    approved_interface = _Approval()
    note_id = memory.save_persistent_note(
        "Remember my preference",
        approval_manager=ApprovalManager(approved_interface),
    )
    assert note_id is not None
    assert approved_interface.requests[0].action.endswith("Remember my preference")
    assert any("INSERT INTO agent_memory.persistent_notes" in sql for sql, _ in connection.executed)


def test_note_without_trusted_interface_is_denied_and_memory_deletion_is_available() -> None:
    connection = _Connection()
    memory = PersistentMemory(
        _settings(),
        connection_factory=lambda: connection,
        initialize=False,
    )

    assert memory.save_persistent_note("Keep this", approval_manager=None) is None
    assert memory.delete_note(str(uuid4())) is True
    assert memory.delete_conversation(str(uuid4())) is True
    memory.delete_all_memory()
    assert connection.executed[-2][0] == "DELETE FROM agent_memory.conversations"
    assert connection.executed[-1][0] == "DELETE FROM agent_memory.persistent_notes"


def test_retention_and_identifiers_are_validated() -> None:
    with pytest.raises(ValueError, match="retention_days"):
        PersistentMemory(_settings(), retention_days=0, initialize=False)
    memory = PersistentMemory(_settings(), initialize=False)
    with pytest.raises(ValueError, match="UUID"):
        memory.delete_conversation("not-a-uuid")
