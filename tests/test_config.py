"""Tests for typed configuration loading and validation."""

import os
from pathlib import Path

import pytest

import agent
from core.config import (
    AppSettings,
    ConfigurationError,
    get_config_path,
    get_data_dir,
    load_config,
)


@pytest.fixture(autouse=True)
def clear_agent_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for variable in tuple(os.environ):
        if variable.startswith("AGENT_"):
            monkeypatch.delenv(variable)


def test_loads_valid_yaml_configuration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        """
ollama:
  model: local-model:latest
  timeout: 15
agent:
  max_steps: 12
  max_tool_calls: 9
  max_retries: 1
security:
  allowed_roots:
    - /tmp/agent-work
  protected_paths:
    - /tmp/agent-work/private
  approval_policy: always
tools:
  enabled_categories: [filesystem, calculation]
logging:
  level: DEBUG
  retention: 14
""",
        encoding="utf-8",
    )
    monkeypatch.delenv("AGENT_OLLAMA_MODEL", raising=False)

    settings = load_config(config_file)

    assert settings.ollama.model == "local-model:latest"
    assert settings.ollama.host == "http://127.0.0.1:11434"
    assert settings.agent.max_steps == 12
    assert settings.security.allowed_roots == [Path("/tmp/agent-work")]
    assert settings.tools.enabled_categories == ["filesystem", "calculation"]
    assert settings.logging.level == "DEBUG"


def test_rejects_invalid_values_without_echoing_them(
    tmp_path: Path,
) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "ollama:\n  timeout: 0\n  api_key: do-not-print-this\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError) as error:
        load_config(config_file)

    assert "ollama.timeout" in str(error.value)
    assert "do-not-print-this" not in str(error.value)


def test_environment_variables_override_yaml(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "ollama:\n  model: yaml-model\nagent:\n  max_steps: 3\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENT_OLLAMA_MODEL", "environment-model")
    monkeypatch.setenv("AGENT_AGENT_MAX_STEPS", "7")
    monkeypatch.setenv(
        "AGENT_SECURITY_ALLOWED_ROOTS",
        '["/tmp/work", "/var/tmp/work"]',
    )

    settings = load_config(config_file)

    assert settings.ollama.model == "environment-model"
    assert settings.agent.max_steps == 7
    assert settings.security.allowed_roots == [
        Path("/tmp/work"),
        Path("/var/tmp/work"),
    ]


def test_missing_optional_file_uses_safe_defaults(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AGENT_OLLAMA_MODEL", raising=False)

    settings = load_config(tmp_path / "not-created.yaml")

    assert isinstance(settings, AppSettings)
    assert settings.ollama.host == "http://127.0.0.1:11434"
    assert settings.ollama.model == "gemma4:e2b"
    assert settings.agent.max_steps == 8
    assert settings.security.allowed_roots == []
    assert settings.security.approval_policy == "writes"
    assert settings.tools.enabled_categories == []


@pytest.mark.parametrize("unsafe_path", ["relative/path", "../outside", "/"])
def test_rejects_unsafe_allowed_root_paths(
    tmp_path: Path,
    unsafe_path: str,
) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        f"security:\n  allowed_roots:\n    - {unsafe_path!r}\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="absolute|filesystem root"):
        load_config(config_file)


def test_xdg_paths_are_respected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))

    assert get_config_path() == tmp_path / "config/personal-ai-agent/config.yaml"
    assert get_data_dir() == tmp_path / "data/personal-ai-agent"


def test_rejects_malformed_environment_list_without_echoing_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENT_SECURITY_ALLOWED_ROOTS", "not-json-secret")

    with pytest.raises(ConfigurationError) as error:
        load_config()

    assert "AGENT_SECURITY_ALLOWED_ROOTS" in str(error.value)
    assert "not-json-secret" not in str(error.value)


def test_invalid_config_prevents_agent_startup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_dir = tmp_path / "config/personal-ai-agent"
    config_dir.mkdir(parents=True)
    (config_dir / "config.yaml").write_text(
        "agent:\n  max_steps: 0\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    agent.main()

    assert "Konfiguratsiya noto'g'ri" in str(capsys.readouterr().out)
