"""Explicit, schema-validated registry for application tools."""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from threading import RLock
from typing import Annotated, Any, Literal

from jsonschema import Draft202012Validator, SchemaError
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.json_schema import SkipJsonSchema

ToolCategory = Literal["filesystem", "system", "network", "calculation"]
PermissionLevel = Literal["read", "write", "network", "system"]
_TOOL_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class ToolRegistryError(Exception):
    """Base error for explicit tool registry operations."""


class DuplicateToolError(ToolRegistryError):
    """Raised when a tool name is already registered."""


class UnknownToolError(ToolRegistryError):
    """Raised when a requested tool is not registered."""


class DisabledToolError(ToolRegistryError):
    """Raised when a registered tool is disabled."""


class ToolDefinition(BaseModel):
    """Validated tool metadata and an explicitly supplied implementation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=64)
    description: str = Field(min_length=1, max_length=1000)
    category: ToolCategory
    argument_schema: dict[str, Any]
    argument_model: Annotated[
        type[BaseModel],
        SkipJsonSchema(),
    ] = Field(exclude=True, repr=False)
    permission_level: PermissionLevel
    execution_timeout: float = Field(gt=0, le=300)
    implementation: Annotated[
        Callable[..., Any],
        SkipJsonSchema(),
    ] = Field(exclude=True, repr=False)

    @model_validator(mode="after")
    def validate_definition(self) -> ToolDefinition:
        if not _TOOL_NAME_PATTERN.fullmatch(self.name):
            raise ValueError("name must use lowercase letters, digits, and underscores")
        if not callable(self.implementation):
            raise ValueError("implementation must be a callable explicitly supplied by the app")
        if self.argument_model.model_config.get("extra") != "forbid":
            raise ValueError("argument_model must forbid unknown fields")
        try:
            Draft202012Validator.check_schema(self.argument_schema)
        except SchemaError as exc:
            raise ValueError("argument_schema must be a valid JSON Schema") from exc
        if self.argument_schema.get("type") != "object":
            raise ValueError("argument_schema must describe an object")
        return self

    def model_tool_schema(self) -> dict[str, Any]:
        """Return the Ollama-compatible function schema without implementation data."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.argument_schema,
            },
        }


class _RegisteredTool(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    definition: ToolDefinition
    enabled: bool = True


class ToolRegistry:
    """Thread-safe in-memory registry with deny-by-default tool selection."""

    def __init__(self) -> None:
        self._tools: dict[str, _RegisteredTool] = {}
        self._lock = RLock()

    def register(self, definition: ToolDefinition, *, enabled: bool = True) -> None:
        """Register a supplied definition; no module discovery or imports occur."""
        with self._lock:
            if definition.name in self._tools:
                raise DuplicateToolError(f"Tool '{definition.name}' is already registered.")
            self._tools[definition.name] = _RegisteredTool(
                definition=definition,
                enabled=enabled,
            )

    def enable(self, name: str) -> None:
        """Enable a registered tool by its exact registered name."""
        self._set_enabled(name, True)

    def disable(self, name: str) -> None:
        """Disable a registered tool by its exact registered name."""
        self._set_enabled(name, False)

    def _set_enabled(self, name: str, enabled: bool) -> None:
        with self._lock:
            registered = self._tools.get(name)
            if registered is None:
                raise UnknownToolError(f"Tool '{name}' is not registered.")
            self._tools[name] = registered.model_copy(update={"enabled": enabled})

    def get_tool(self, name: str, *, require_enabled: bool = True) -> ToolDefinition:
        """Return a registered tool, rejecting disabled or unknown names by default."""
        with self._lock:
            registered = self._tools.get(name)
            if registered is None:
                raise UnknownToolError(f"Tool '{name}' is not registered.")
            if require_enabled and not registered.enabled:
                raise DisabledToolError(f"Tool '{name}' is disabled.")
            return registered.definition

    def list_tools(
        self,
        *,
        category: ToolCategory | None = None,
        enabled_only: bool = False,
    ) -> list[ToolDefinition]:
        """List registered definitions, optionally filtered by category/status."""
        with self._lock:
            return [
                registered.definition
                for registered in self._tools.values()
                if (category is None or registered.definition.category == category)
                and (not enabled_only or registered.enabled)
            ]

    def model_tool_schemas(
        self,
        *,
        category: ToolCategory | None = None,
    ) -> list[dict[str, Any]]:
        """Build model schemas only from registered and enabled tools."""
        return [
            definition.model_tool_schema()
            for definition in self.list_tools(category=category, enabled_only=True)
        ]

    def validate_tool_selection(self, name: str, arguments: object) -> ToolDefinition:
        """Authorize a model-selected name and validate arguments before use."""
        definition = self.get_tool(name, require_enabled=True)
        if not isinstance(arguments, dict):
            raise ToolRegistryError(f"Arguments for tool '{name}' must be an object.")
        errors = list(
            Draft202012Validator(definition.argument_schema).iter_errors(arguments)
        )
        if errors:
            path = ".".join(str(part) for part in errors[0].absolute_path)
            location = f" at '{path}'" if path else ""
            raise ToolRegistryError(
                f"Arguments for tool '{name}' do not match its schema{location}."
            )
        return definition

    def __len__(self) -> int:
        with self._lock:
            return len(self._tools)


def definitions_for_model(
    registry: ToolRegistry,
    *,
    category: ToolCategory | None = None,
) -> Sequence[dict[str, Any]]:
    """Return enabled model schemas as an immutable sequence."""
    return tuple(registry.model_tool_schemas(category=category))
