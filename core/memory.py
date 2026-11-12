"""Privacy-conscious PostgreSQL storage for agent conversations and notes."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol
from uuid import UUID, uuid4

import psycopg

from core.config import DatabaseSettings
from security.approval import ApprovalManager, ApprovalStatus
from security.audit_logger import sanitize_text
from storage.database import UnsupportedSchemaVersionError, apply_migrations
from storage.postgres import DatabaseNotConfiguredError, connect_database

_MAX_RECENT_ITEMS = 50
_MAX_SEARCH_LENGTH = 200
_MAX_MESSAGE_LENGTH = 20_000
_MAX_NOTE_LENGTH = 4_000


class MemoryRecorder(Protocol):
    """Controller-facing subset used to persist task outcomes."""

    def start_task(self, task_id: str, user_message: str) -> None: ...

    def record_tool_execution(
        self,
        task_id: str,
        *,
        tool_name: str,
        status: str,
        error_code: str | None,
        duration_seconds: float,
    ) -> None: ...

    def finish_task(
        self,
        task_id: str,
        *,
        status: str,
        response: str | None,
    ) -> None: ...


class MemoryStorageError(RuntimeError):
    """Raised when the configured PostgreSQL memory store cannot initialize."""


class PersistentMemory:
    """Persist bounded task history, conversation context, and approved notes."""

    def __init__(
        self,
        settings: DatabaseSettings,
        *,
        connection_factory: Callable[[], psycopg.Connection] | None = None,
        retention_days: int | None = None,
        initialize: bool = True,
    ) -> None:
        selected_retention = (
            settings.memory_retention_days
            if retention_days is None
            else retention_days
        )
        if not 1 <= selected_retention <= 3650:
            raise ValueError("retention_days must be between 1 and 3650")
        self._retention_days = selected_retention
        self._connect = connection_factory or (lambda: connect_database(settings))
        if initialize:
            try:
                with self._connect() as connection:
                    apply_migrations(connection)
                self.prune_expired()
            except (
                psycopg.Error,
                DatabaseNotConfiguredError,
                UnsupportedSchemaVersionError,
            ) as exc:
                raise MemoryStorageError(
                    "PostgreSQL memory storage could not be initialized."
                ) from exc

    def start_task(self, task_id: str, user_message: str) -> None:
        """Create a conversation and task record, retaining only sanitized text."""
        task_uuid = _parse_uuid(task_id)
        request = _bounded_sanitized_text(user_message, _MAX_MESSAGE_LENGTH)
        with self._connect() as connection, connection.cursor() as cursor:
            _prune_with_cursor(cursor, self._retention_days)
            cursor.execute(
                "INSERT INTO agent_memory.conversations (conversation_id) VALUES (%s)",
                (task_uuid,),
            )
            cursor.execute(
                """
                    INSERT INTO agent_memory.task_history
                        (task_id, conversation_id, status, request)
                    VALUES (%s, %s, 'running', %s)
                    """,
                (task_uuid, task_uuid, request),
            )
            cursor.execute(
                """
                    INSERT INTO agent_memory.conversation_messages
                        (conversation_id, role, content)
                    VALUES (%s, 'user', %s)
                    """,
                (task_uuid, request),
            )

    def record_tool_execution(
        self,
        task_id: str,
        *,
        tool_name: str,
        status: str,
        error_code: str | None,
        duration_seconds: float,
    ) -> None:
        """Store only tool identity and outcome metadata, never its arguments."""
        task_uuid = _parse_uuid(task_id)
        if not tool_name or len(tool_name) > 128:
            raise ValueError("tool_name must contain 1 to 128 characters")
        if not status or len(status) > 32:
            raise ValueError("status must contain 1 to 32 characters")
        if duration_seconds < 0:
            raise ValueError("duration_seconds must be non-negative")
        summary = (
            "Tool completed successfully."
            if status == "success"
            else f"Tool failed ({sanitize_text(error_code or 'TOOL_ERROR', max_chars=100)})."
        )
        with self._connect() as connection, connection.cursor() as cursor:
            _prune_with_cursor(cursor, self._retention_days)
            cursor.execute(
                """
                    INSERT INTO agent_memory.tool_execution_summaries
                        (task_id, tool_name, status, summary, duration_seconds)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                (task_uuid, tool_name, status, summary, duration_seconds),
            )

    def finish_task(
        self,
        task_id: str,
        *,
        status: str,
        response: str | None,
    ) -> None:
        """Set the outcome and append a sanitized final assistant message."""
        task_uuid = _parse_uuid(task_id)
        if not status or len(status) > 32:
            raise ValueError("status must contain 1 to 32 characters")
        safe_response = (
            _bounded_sanitized_text(response, _MAX_MESSAGE_LENGTH)
            if response is not None
            else None
        )
        with self._connect() as connection, connection.cursor() as cursor:
            _prune_with_cursor(cursor, self._retention_days)
            cursor.execute(
                """
                    UPDATE agent_memory.task_history
                    SET status = %s, response = %s, updated_at = now()
                    WHERE task_id = %s
                    """,
                (status, safe_response, task_uuid),
            )
            if cursor.rowcount != 1:
                raise ValueError("task history record does not exist")
            if safe_response:
                cursor.execute(
                    """
                        INSERT INTO agent_memory.conversation_messages
                            (conversation_id, role, content)
                        VALUES (%s, 'assistant', %s)
                        """,
                    (task_uuid, safe_response),
                )
            cursor.execute(
                """
                    UPDATE agent_memory.conversations
                    SET updated_at = now()
                    WHERE conversation_id = %s
                    """,
                (task_uuid,),
            )

    def recent_conversations(self, limit: int = 10) -> list[dict[str, Any]]:
        """Return the newest conversations and their sanitized messages."""
        _validate_limit(limit)
        with self._connect() as connection, connection.cursor() as cursor:
            _prune_with_cursor(cursor, self._retention_days)
            cursor.execute(
                """
                    SELECT c.conversation_id::text, t.task_id::text, t.status,
                           t.request, t.response, c.created_at,
                           COALESCE(
                               json_agg(
                                   json_build_object(
                                       'role', m.role,
                                       'content', m.content,
                                       'created_at', m.created_at
                                   ) ORDER BY m.created_at, m.message_id
                               ) FILTER (WHERE m.message_id IS NOT NULL),
                               '[]'::json
                           ) AS messages
                    FROM agent_memory.conversations AS c
                    JOIN agent_memory.task_history AS t
                      ON t.conversation_id = c.conversation_id
                    LEFT JOIN agent_memory.conversation_messages AS m
                      ON m.conversation_id = c.conversation_id
                    GROUP BY c.conversation_id, t.task_id, t.status, t.request,
                             t.response, c.created_at
                    ORDER BY c.updated_at DESC, c.conversation_id DESC
                    LIMIT %s
                    """,
                (limit,),
            )
            rows = cursor.fetchall()
        return [
            {
                "conversation_id": row[0],
                "task_id": row[1],
                "status": row[2],
                "request": row[3],
                "response": row[4],
                "created_at": row[5],
                "messages": row[6],
            }
            for row in rows
        ]

    def search_task_history(
        self,
        query: str,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Search task requests and responses with PostgreSQL full-text search."""
        _validate_limit(limit)
        if not isinstance(query, str) or not query.strip() or len(query) > _MAX_SEARCH_LENGTH:
            raise ValueError("query must contain 1 to 200 characters")
        with self._connect() as connection, connection.cursor() as cursor:
            _prune_with_cursor(cursor, self._retention_days)
            cursor.execute(
                """
                    SELECT task_id::text, conversation_id::text, status,
                           request, response, created_at
                    FROM agent_memory.task_history
                    WHERE to_tsvector('simple', coalesce(request, '') || ' ' ||
                                      coalesce(response, ''))
                          @@ plainto_tsquery('simple', %s)
                    ORDER BY created_at DESC
                    LIMIT %s
                    """,
                (query.strip(), limit),
            )
            rows = cursor.fetchall()
        return [
            {
                "task_id": row[0],
                "conversation_id": row[1],
                "status": row[2],
                "request": row[3],
                "response": row[4],
                "created_at": row[5],
            }
            for row in rows
        ]

    def save_persistent_note(
        self,
        content: str,
        *,
        approval_manager: ApprovalManager | None,
        task_id: str = "unassigned",
    ) -> str | None:
        """Save personal memory only after trusted, content-bound user approval."""
        note = _bounded_sanitized_text(content, _MAX_NOTE_LENGTH)
        if not note.strip() or approval_manager is None:
            return None
        arguments: dict[str, object] = {"content": note}
        approval_id, decision = approval_manager.request(
            tool_name="save_persistent_note",
            arguments=arguments,
            action=f"Save this persistent note: {note}",
            target_resource="PostgreSQL persistent user memory",
            potential_impact="This note will remain available across future agent sessions until deleted or expired.",
            task_id=task_id,
        )
        if (
            approval_id is None
            or decision != ApprovalStatus.APPROVED
            or not approval_manager.consume(
                approval_id,
                tool_name="save_persistent_note",
                arguments=arguments,
                task_id=task_id,
            )
        ):
            return None

        note_id = str(uuid4())
        with self._connect() as connection, connection.cursor() as cursor:
            _prune_with_cursor(cursor, self._retention_days)
            cursor.execute(
                """
                    INSERT INTO agent_memory.persistent_notes (note_id, content)
                    VALUES (%s, %s)
                    """,
                (UUID(note_id), note),
            )
        return note_id

    def list_persistent_notes(self, limit: int = 50) -> list[dict[str, Any]]:
        """List saved user-approved notes without exposing database credentials."""
        _validate_limit(limit)
        with self._connect() as connection, connection.cursor() as cursor:
            _prune_with_cursor(cursor, self._retention_days)
            cursor.execute(
                """
                    SELECT note_id::text, content, created_at
                    FROM agent_memory.persistent_notes
                    ORDER BY created_at DESC
                    LIMIT %s
                    """,
                (limit,),
            )
            rows = cursor.fetchall()
        return [
            {"note_id": row[0], "content": row[1], "created_at": row[2]}
            for row in rows
        ]

    def delete_note(self, note_id: str) -> bool:
        """Delete one persistent note by identifier."""
        identifier = _parse_uuid(note_id)
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM agent_memory.persistent_notes WHERE note_id = %s",
                (identifier,),
            )
            return cursor.rowcount == 1

    def delete_conversation(self, conversation_id: str) -> bool:
        """Delete a conversation and its task/tool history via foreign-key cascades."""
        identifier = _parse_uuid(conversation_id)
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM agent_memory.conversations WHERE conversation_id = %s",
                (identifier,),
            )
            return cursor.rowcount == 1

    def delete_all_memory(self) -> None:
        """Erase conversations, task/tool history, and persistent notes."""
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("DELETE FROM agent_memory.conversations")
            cursor.execute("DELETE FROM agent_memory.persistent_notes")

    def prune_expired(self) -> None:
        """Remove records older than the configured maximum retention period."""
        with self._connect() as connection, connection.cursor() as cursor:
            _prune_with_cursor(cursor, self._retention_days)


def _prune_with_cursor(cursor: Any, retention_days: int) -> None:
    cursor.execute(
        """
        DELETE FROM agent_memory.conversations
        WHERE created_at < now() - (%s * interval '1 day')
        """,
        (retention_days,),
    )
    cursor.execute(
        """
        DELETE FROM agent_memory.persistent_notes
        WHERE created_at < now() - (%s * interval '1 day')
        """,
        (retention_days,),
    )


def _bounded_sanitized_text(value: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"text must contain 1 to {maximum} characters")
    return sanitize_text(value, max_chars=maximum)


def _validate_limit(limit: int) -> None:
    if not 1 <= limit <= _MAX_RECENT_ITEMS:
        raise ValueError(f"limit must be between 1 and {_MAX_RECENT_ITEMS}")


def _parse_uuid(value: str) -> UUID:
    try:
        return UUID(value)
    except (ValueError, TypeError, AttributeError):
        raise ValueError("identifier must be a UUID") from None
