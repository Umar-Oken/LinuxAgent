"""Theme-aware rendering for registered tool metadata."""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from rich.box import ROUNDED
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from core.tool_registry import ToolDefinition
from ui.theme import Theme

_COMPACT_WIDTH = 72


def render_tools(
    console: Console,
    theme: Theme,
    tools: Iterable[ToolDefinition],
    enabled_names: set[str],
) -> None:
    definitions = list(tools)
    if not definitions:
        console.print(
            Panel(
                "Hozircha vositalar ulanmagan",
                title="Vositalar",
                border_style=theme.warning,
                box=ROUNDED,
            )
        )
        return

    if console.size.width < _COMPACT_WIDTH:
        compact = Table(box=ROUNDED, border_style=theme.primary, expand=True)
        compact.add_column("Vosita", overflow="fold")
        for index, tool in enumerate(definitions, start=1):
            enabled = tool.name in enabled_names
            state = "yoqilgan" if enabled else "o'chirilgan"
            compact.add_row(
                Text(
                    f"{index}. {tool.name} — {state}\n"
                    f"Tavsif: {tool.description}\n"
                    f"Parametrlar: {_parameter_summary(tool.argument_schema)}"
                )
            )
        console.print(
            Panel(
                compact,
                title="Vositalar",
                subtitle=f"Jami: {len(definitions)} ta vosita",
                border_style=theme.primary,
                box=ROUNDED,
                padding=(0, 1),
            )
        )
        return

    table = Table(box=ROUNDED, border_style=theme.primary, expand=True)
    for heading in ("№", "Nomi", "Tavsif", "Parametrlar", "Holat"):
        table.add_column(heading, no_wrap=False, overflow="fold")
    table.columns[0].width = 3
    table.columns[1].max_width = 20
    table.columns[2].max_width = 42
    table.columns[3].max_width = 32
    table.columns[4].max_width = 14
    for index, tool in enumerate(definitions, start=1):
        state = "yoqilgan" if tool.name in enabled_names else "o'chirilgan"
        table.add_row(
            str(index),
            Text(tool.name),
            Text(tool.description),
            Text(_parameter_summary(tool.argument_schema)),
            Text(state, style=theme.success if tool.name in enabled_names else theme.warning),
        )
    console.print(
        Panel(
            table,
            title="Vositalar",
            subtitle=f"Jami: {len(definitions)} ta vosita",
            border_style=theme.primary,
            box=ROUNDED,
            padding=(0, 1),
        )
    )


def render_tool_detail(console: Console, theme: Theme, tool: ToolDefinition, *, enabled: bool) -> None:
    console.print(
        Panel(
            Text(tool.description),
            title=f"Vosita: {tool.name}",
            subtitle="yoqilgan" if enabled else "o'chirilgan",
            border_style=theme.primary,
            box=ROUNDED,
        )
    )

    schema = tool.argument_schema
    properties = schema.get("properties", {})
    required = set(schema.get("required", []))
    if properties and console.size.width < _COMPACT_WIDTH:
        table = Table(box=ROUNDED, border_style=theme.accent, expand=True)
        table.add_column("Parametr", overflow="fold")
        for name, metadata in properties.items():
            table.add_row(
                Text(
                    f"{name} — turi: {_schema_type(metadata, schema)} — "
                    f"{'majburiy' if name in required else 'ixtiyoriy'}\n"
                    f"Tavsif: {metadata.get('description', '—')}"
                )
            )
    elif properties:
        table = Table(box=ROUNDED, border_style=theme.accent, expand=True)
        for heading in ("Parametr", "Tur", "Talab", "Tavsif"):
            table.add_column(heading, no_wrap=False, overflow="fold")
        table.columns[0].max_width = 24
        table.columns[1].max_width = 20
        table.columns[2].max_width = 14
        table.columns[3].max_width = 42
        for name, metadata in properties.items():
            table.add_row(
                Text(name),
                Text(_schema_type(metadata, schema)),
                Text("majburiy" if name in required else "ixtiyoriy"),
                Text(str(metadata.get("description", "—"))),
            )
    if properties:
        console.print(
            Panel(
                table,
                title="Parametrlar",
                border_style=theme.accent,
                box=ROUNDED,
            )
        )
    else:
        console.print("Parametr talab qilinmaydi.")

    example = {
        name: _example_value(metadata, schema)
        for name, metadata in properties.items()
        if name in required
    }
    console.print(
        Panel(
            Text(json.dumps({"tool": tool.name, "arguments": example}, ensure_ascii=False, indent=2)),
            title="Misol chaqiruvi",
            border_style=theme.success,
            box=ROUNDED,
        )
    )


def _parameter_summary(schema: dict[str, Any]) -> str:
    properties = schema.get("properties", {})
    if not properties:
        return "—"
    required = set(schema.get("required", []))
    return ", ".join(
        f"{name}{'*' if name in required else ''}"
        for name in properties
    )


def _schema_type(metadata: dict[str, Any], schema: dict[str, Any]) -> str:
    reference = metadata.get("$ref")
    if isinstance(reference, str) and reference.startswith("#/$defs/"):
        target: Any = schema.get("$defs", {})
        for key in reference.removeprefix("#/$defs/").split("/"):
            target = target.get(key, {}) if isinstance(target, dict) else {}
        if isinstance(target, dict):
            return _schema_type(target, schema)
    value = metadata.get("type", "any")
    if isinstance(value, list):
        return " | ".join(str(item) for item in value)
    if value == "array":
        return f"ro'yxat[{_schema_type(metadata.get('items', {}), schema)}]"
    if value == "object":
        return "obyekt"
    return str(value)


def _example_value(metadata: dict[str, Any], schema: dict[str, Any]) -> Any:
    if "default" in metadata:
        return metadata["default"]
    if "enum" in metadata and metadata["enum"]:
        return metadata["enum"][0]
    reference = metadata.get("$ref")
    if isinstance(reference, str) and reference.startswith("#/$defs/"):
        target: Any = schema.get("$defs", {})
        for key in reference.removeprefix("#/$defs/").split("/"):
            target = target.get(key, {}) if isinstance(target, dict) else {}
        if isinstance(target, dict):
            return _example_value(target, schema)
    kind = metadata.get("type")
    if isinstance(kind, list):
        kind = next((item for item in kind if item != "null"), kind[0] if kind else None)
    if kind == "string":
        return "<matn>"
    if kind in {"integer", "number"}:
        return 1
    if kind == "boolean":
        return True
    if kind == "array":
        return []
    if kind == "object":
        return {}
    return "<qiymat>"
