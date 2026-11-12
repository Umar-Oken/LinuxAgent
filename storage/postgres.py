"""PostgreSQL connection helper."""

from __future__ import annotations

import psycopg

from core.config import DatabaseSettings


class DatabaseNotConfiguredError(RuntimeError):
    """Raised when a database connection is requested without complete settings."""


def connect_database(
    settings: DatabaseSettings,
    *,
    connect_timeout: int = 5,
) -> psycopg.Connection:
    """Open a PostgreSQL connection using validated application settings."""
    if not 1 <= connect_timeout <= 60:
        raise ValueError("connect_timeout must be between 1 and 60 seconds")
    if (
        settings.name is None
        or settings.user is None
        or settings.password is None
        or settings.host is None
        or settings.port is None
    ):
        raise DatabaseNotConfiguredError(
            "PostgreSQL is not configured; set DB_NAME, DB_USER, DB_PASSWORD, DB_HOST, and DB_PORT."
        )

    return psycopg.connect(
        dbname=settings.name,
        user=settings.user,
        password=settings.password.get_secret_value(),
        host=settings.host,
        port=settings.port,
        connect_timeout=connect_timeout,
    )
