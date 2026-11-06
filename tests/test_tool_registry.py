"""Tests for explicit tool registration and model-visible schemas."""

from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from core.tool_registry import (
    DisabledToolError,
    DuplicateToolError,
    ToolCategory,
    ToolDefinition,
    ToolRegistry,
    ToolRegistryError,
    UnknownToolError,
    definitions_for_model,
)


class MockLookupArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    query: str


def _mock_tool(
    *,
    name: str = "mock_lookup",
    category: ToolCategory = "filesystem",
    schema: dict[str, Any] | None = None,
) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description="A test-only mock tool.",
        category=category,
        argument_schema=schema
        or {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        },
        argument_model=MockLookupArguments,
        permission_level="read",
        execution_timeout=2,
        implementation=lambda query: f"found: {query}",
    )


def test_register_tool_and_list_by_category() -> None:
    registry = ToolRegistry()
    definition = _mock_tool()

    registry.register(definition)

    assert len(registry) == 1
    assert registry.get_tool("mock_lookup") is definition
    assert registry.list_tools(category="filesystem") == [definition]
    assert registry.list_tools(category="network") == []


def test_duplicate_registration_is_rejected() -> None:
    registry = ToolRegistry()
    registry.register(_mock_tool())

    with pytest.raises(DuplicateToolError, match="already registered"):
        registry.register(_mock_tool())


def test_invalid_argument_schema_is_rejected() -> None:
    with pytest.raises(ValidationError, match="valid JSON Schema"):
        _mock_tool(
            schema={
                "type": "unknown-type",
                "properties": {},
            }
        )


def test_disabled_tool_is_not_selectable_or_in_model_schemas() -> None:
    registry = ToolRegistry()
    registry.register(_mock_tool(), enabled=False)

    with pytest.raises(DisabledToolError, match="disabled"):
        registry.get_tool("mock_lookup")
    assert registry.list_tools(enabled_only=True) == []
    assert registry.model_tool_schemas() == []

    registry.enable("mock_lookup")
    assert registry.model_tool_schemas()[0]["function"]["name"] == "mock_lookup"


def test_unknown_tool_is_rejected() -> None:
    registry = ToolRegistry()

    with pytest.raises(UnknownToolError, match="not registered"):
        registry.get_tool("not_registered")
    with pytest.raises(UnknownToolError, match="not registered"):
        registry.disable("not_registered")


def test_model_schema_omits_implementation_and_includes_only_enabled_tools() -> None:
    registry = ToolRegistry()
    registry.register(_mock_tool())
    registry.register(_mock_tool(name="mock_network", category="network"))
    registry.disable("mock_network")

    schemas = definitions_for_model(registry, category="filesystem")

    assert len(schemas) == 1
    assert schemas[0] == {
        "type": "function",
        "function": {
            "name": "mock_lookup",
            "description": "A test-only mock tool.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    }


def test_model_selected_arguments_are_validated_before_tool_use() -> None:
    registry = ToolRegistry()
    registry.register(_mock_tool())

    assert registry.validate_tool_selection(
        "mock_lookup",
        {"query": "hello"},
    ).name == "mock_lookup"
    with pytest.raises(ToolRegistryError, match="do not match its schema"):
        registry.validate_tool_selection("mock_lookup", {"unexpected": True})


def test_model_schema_generation_does_not_expose_callable() -> None:
    definition = _mock_tool()

    assert "implementation" not in definition.model_dump()
    assert "implementation" not in definition.model_json_schema()["properties"]
