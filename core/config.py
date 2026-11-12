"""Typed application configuration with YAML and environment overrides."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal, get_origin
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

ApprovalPolicy = Literal["writes", "always"]
ToolCategory = Literal["filesystem", "system", "network", "calculation"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
DEFAULT_CONNECTIVITY_ENDPOINT = "https://connectivitycheck.gstatic.com/generate_204"
DEFAULT_EXCHANGE_RATE_PROVIDERS = ["https://api.frankfurter.app/latest"]


class ConfigurationError(ValueError):
    """Raised when a configuration file or environment override is invalid."""


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OllamaSettings(StrictModel):
    host: str = "http://127.0.0.1:11434"
    model: str = "gemma4:e2b-it-qat"
    timeout: float = Field(default=60.0, gt=0, le=600)

    @field_validator("host")
    @classmethod
    def validate_host(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "host must be an HTTP(S) URL without credentials, path, query, or fragment"
            )
        try:
            parsed.port
        except ValueError as exc:
            raise ValueError("host contains an invalid port") from exc
        return value.rstrip("/")

    @field_validator("model")
    @classmethod
    def validate_model(cls, value: str) -> str:
        if not value.strip() or len(value) > 200:
            raise ValueError("model must contain 1 to 200 non-whitespace characters")
        return value.strip()


class AgentSettings(StrictModel):
    max_steps: int = Field(default=8, ge=1, le=100)
    max_tool_calls: int = Field(default=8, ge=1, le=100)
    max_retries: int = Field(default=2, ge=0, le=10)
    max_action_retries: int = Field(default=1, ge=0, le=10)
    max_task_seconds: float = Field(default=300.0, gt=0, le=86_400)


def _validate_paths(value: object) -> list[Path]:
    if not isinstance(value, list):
        raise ValueError("paths must be a list of absolute paths")

    paths: list[Path] = []
    for item in value:
        if not isinstance(item, (str, Path)):
            raise ValueError("each path must be a string")
        raw_path = Path(os.path.expanduser(str(item)))
        if not raw_path.is_absolute() or ".." in raw_path.parts:
            raise ValueError("paths must be absolute and must not contain '..'")
        normalized = Path(os.path.normpath(str(raw_path)))
        if normalized == Path(normalized.anchor):
            raise ValueError("filesystem root cannot be configured as a tool path")
        paths.append(normalized)
    return paths


class SecuritySettings(StrictModel):
    allowed_roots: list[Path] = Field(default_factory=list)
    protected_paths: list[Path] = Field(
        default_factory=lambda: [
            Path.home() / ".ssh",
            Path.home() / ".gnupg",
        ]
    )
    approval_policy: ApprovalPolicy = "writes"

    @field_validator("allowed_roots", "protected_paths", mode="before")
    @classmethod
    def validate_paths(cls, value: object) -> list[Path]:
        return _validate_paths(value)


class ToolSettings(StrictModel):
    enabled_categories: list[ToolCategory] = Field(default_factory=list)


class NetworkSettings(StrictModel):
    connectivity_endpoint: str = DEFAULT_CONNECTIVITY_ENDPOINT
    exchange_rate_providers: list[str] = Field(
        default_factory=lambda: list(DEFAULT_EXCHANGE_RATE_PROVIDERS),
        min_length=1,
        max_length=5,
    )
    web_timeout_seconds: float = Field(default=10.0, gt=0, le=30)
    web_max_content_bytes: int = Field(default=1_000_000, ge=1024, le=5_000_000)

    @field_validator("connectivity_endpoint")
    @classmethod
    def validate_connectivity_endpoint(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "connectivity_endpoint must be an HTTPS URL without credentials, query, or fragment"
            )
        try:
            parsed.port
        except ValueError as exc:
            raise ValueError("connectivity_endpoint contains an invalid port") from exc
        return value

    @field_validator("exchange_rate_providers")
    @classmethod
    def validate_exchange_rate_providers(cls, values: list[str]) -> list[str]:
        for value in values:
            parsed = urlsplit(value)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError(
                    "exchange rate providers must be HTTPS URLs without credentials, query, or fragment"
                )
            try:
                port = parsed.port
            except ValueError as exc:
                raise ValueError("exchange rate provider contains an invalid port") from exc
            if port not in {None, 443}:
                raise ValueError("exchange rate providers must use the standard HTTPS port")
        return values


class LoggingSettings(StrictModel):
    level: LogLevel = "INFO"
    retention: int = Field(default=30, ge=1, le=3650)


class AppSettings(StrictModel):
    ollama: OllamaSettings = Field(default_factory=OllamaSettings)
    agent: AgentSettings = Field(default_factory=AgentSettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    tools: ToolSettings = Field(default_factory=ToolSettings)
    network: NetworkSettings = Field(default_factory=NetworkSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)


def get_config_dir() -> Path:
    """Return the XDG configuration directory for this application."""
    configured = os.environ.get("XDG_CONFIG_HOME")
    base = Path(os.path.expanduser(configured)) if configured else Path.home() / ".config"
    if not base.is_absolute():
        base = Path.home() / ".config"
    return base / "personal-ai-agent"


def get_config_path() -> Path:
    """Return the default YAML configuration file path."""
    return get_config_dir() / "config.yaml"


def get_data_dir() -> Path:
    """Return the XDG-compatible application data directory."""
    configured = os.environ.get("XDG_DATA_HOME")
    base = Path(os.path.expanduser(configured)) if configured else Path.home() / ".local/share"
    if not base.is_absolute():
        base = Path.home() / ".local/share"
    return base / "personal-ai-agent"


def _validation_message(error: ValidationError) -> str:
    details = []
    for item in error.errors(
        include_url=False,
        include_context=False,
        include_input=False,
    ):
        location = ".".join(str(part) for part in item["loc"]) or "configuration"
        details.append(f"{location}: {item['msg']}")
    return "Invalid configuration:\n- " + "\n- ".join(details)


def _load_yaml(path: Path) -> dict[str, object]:
    try:
        with path.open("r", encoding="utf-8") as file:
            data = yaml.safe_load(file)
    except FileNotFoundError:
        return {}
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        if mark is not None:
            raise ConfigurationError(
                f"Invalid YAML in {path} at line {mark.line + 1}, column {mark.column + 1}."
            ) from None
        raise ConfigurationError(f"Invalid YAML in {path}.") from None
    except (OSError, UnicodeError) as exc:
        raise ConfigurationError(
            f"Could not read configuration file {path} ({type(exc).__name__})."
        ) from None

    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigurationError(f"Configuration in {path} must be a YAML mapping.")
    return data


def _environment_overrides(data: dict[str, object]) -> None:
    env_prefix = "AGENT_"
    for section_name, section_model in (
        ("ollama", OllamaSettings),
        ("agent", AgentSettings),
        ("security", SecuritySettings),
        ("tools", ToolSettings),
        ("network", NetworkSettings),
        ("logging", LoggingSettings),
    ):
        section_fields = section_model.model_fields
        for field_name, field_info in section_fields.items():
            variable = f"{env_prefix}{section_name}_{field_name}".upper()
            if variable not in os.environ:
                continue
            section = data.get(section_name)
            if section is None:
                section = {}
                data[section_name] = section
            if not isinstance(section, dict):
                raise ConfigurationError(
                    f"Configuration section '{section_name}' must be a YAML mapping."
                )
            value: object = os.environ[variable]
            if get_origin(field_info.annotation) is list:
                try:
                    value = json.loads(str(value))
                except json.JSONDecodeError:
                    raise ConfigurationError(
                        f"Environment variable {variable} must contain a JSON array."
                    ) from None
                if not isinstance(value, list):
                    raise ConfigurationError(
                        f"Environment variable {variable} must contain a JSON array."
                    )
            section[field_name] = value


def load_config(config_path: str | Path | None = None) -> AppSettings:
    """Load defaults, optional YAML, and ``AGENT_<SECTION>_<FIELD>`` overrides."""
    path = Path(config_path).expanduser() if config_path is not None else get_config_path()
    data = _load_yaml(path)
    _environment_overrides(data)
    try:
        return AppSettings.model_validate(data)
    except ValidationError as exc:
        raise ConfigurationError(_validation_message(exc)) from None
