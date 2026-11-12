"""PostgreSQL schema migrations for persistent agent memory."""

from __future__ import annotations

from collections.abc import Sequence

import psycopg

MIGRATIONS: tuple[tuple[int, Sequence[str]], ...] = (
    (
        1,
        (
            """
            CREATE TABLE agent_memory.conversations (
                conversation_id UUID PRIMARY KEY,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """,
            """
            CREATE TABLE agent_memory.conversation_messages (
                message_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                conversation_id UUID NOT NULL
                    REFERENCES agent_memory.conversations(conversation_id)
                    ON DELETE CASCADE,
                role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
                content TEXT NOT NULL CHECK (length(content) <= 20000),
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """,
            """
            CREATE TABLE agent_memory.task_history (
                task_id UUID PRIMARY KEY,
                conversation_id UUID NOT NULL UNIQUE
                    REFERENCES agent_memory.conversations(conversation_id)
                    ON DELETE CASCADE,
                status TEXT NOT NULL CHECK (length(status) <= 32),
                request TEXT NOT NULL CHECK (length(request) <= 20000),
                response TEXT CHECK (response IS NULL OR length(response) <= 20000),
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """,
            """
            CREATE TABLE agent_memory.tool_execution_summaries (
                summary_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                task_id UUID NOT NULL
                    REFERENCES agent_memory.task_history(task_id)
                    ON DELETE CASCADE,
                tool_name TEXT NOT NULL CHECK (length(tool_name) <= 128),
                status TEXT NOT NULL CHECK (length(status) <= 32),
                summary TEXT NOT NULL CHECK (length(summary) <= 1000),
                duration_seconds DOUBLE PRECISION NOT NULL
                    CHECK (duration_seconds >= 0),
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """,
            """
            CREATE TABLE agent_memory.persistent_notes (
                note_id UUID PRIMARY KEY,
                content TEXT NOT NULL CHECK (length(content) <= 4000),
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """,
            """
            CREATE INDEX task_history_created_at_idx
            ON agent_memory.task_history (created_at DESC)
            """,
            """
            CREATE INDEX task_history_request_search_idx
            ON agent_memory.task_history USING GIN (
                to_tsvector('simple', coalesce(request, '') || ' ' || coalesce(response, ''))
            )
            """,
            """
            CREATE INDEX conversation_messages_recent_idx
            ON agent_memory.conversation_messages (conversation_id, created_at)
            """,
            "REVOKE ALL ON SCHEMA agent_memory FROM PUBLIC",
            "REVOKE ALL ON ALL TABLES IN SCHEMA agent_memory FROM PUBLIC",
            "REVOKE ALL ON ALL SEQUENCES IN SCHEMA agent_memory FROM PUBLIC",
        ),
    ),
)


class UnsupportedSchemaVersionError(RuntimeError):
    """Raised when the database schema is newer than this application."""


def apply_migrations(connection: psycopg.Connection) -> list[int]:
    """Apply unapplied, ordered schema migrations in one transaction."""
    applied: list[int] = []
    with connection.transaction(), connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(714233905620)")
        cursor.execute("CREATE SCHEMA IF NOT EXISTS agent_memory")
        cursor.execute(
            """
                CREATE TABLE IF NOT EXISTS agent_memory.schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
        )
        cursor.execute("SELECT version FROM agent_memory.schema_migrations")
        known_versions = {int(row[0]) for row in cursor.fetchall()}
        latest_supported = max((version for version, _ in MIGRATIONS), default=0)
        if any(version > latest_supported for version in known_versions):
            raise UnsupportedSchemaVersionError(
                "Database schema is newer than this application supports."
            )
        for version, statements in MIGRATIONS:
            if version in known_versions:
                continue
            for statement in statements:
                cursor.execute(statement)
            cursor.execute(
                "INSERT INTO agent_memory.schema_migrations (version) VALUES (%s)",
                (version,),
            )
            applied.append(version)
    return applied
