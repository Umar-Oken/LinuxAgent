from __future__ import annotations

from typing import Any

import pytest
from pydantic import SecretStr

from core.config import DatabaseSettings
from storage import postgres
from storage.postgres import DatabaseNotConfiguredError, connect_database


def test_connect_database_passes_validated_settings_to_psycopg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected_connection = object()
    captured: dict[str, Any] = {}

    def fake_connect(**kwargs: Any) -> object:
        captured.update(kwargs)
        return expected_connection

    monkeypatch.setattr(postgres.psycopg, "connect", fake_connect)
    settings = DatabaseSettings(
        name="agent_db",
        user="agent_user",
        password=SecretStr("secret"),
        host="localhost",
        port=5432,
    )

    connection = connect_database(settings)

    assert connection is expected_connection
    assert captured == {
        "dbname": "agent_db",
        "user": "agent_user",
        "password": "secret",
        "host": "localhost",
        "port": 5432,
        "connect_timeout": 5,
    }


def test_connect_database_requires_configuration() -> None:
    with pytest.raises(DatabaseNotConfiguredError, match="DB_NAME"):
        connect_database(DatabaseSettings())


@pytest.mark.parametrize("timeout", [0, 61])
def test_connect_database_rejects_unsafe_timeout(timeout: int) -> None:
    settings = DatabaseSettings(
        name="agent_db",
        user="agent_user",
        password=SecretStr("secret"),
        host="localhost",
        port=5432,
    )

    with pytest.raises(ValueError, match="connect_timeout"):
        connect_database(settings, connect_timeout=timeout)


def test_database_settings_reject_partial_credentials() -> None:
    with pytest.raises(ValueError, match="configured together"):
        DatabaseSettings(name="agent_db", password="do-not-print")
