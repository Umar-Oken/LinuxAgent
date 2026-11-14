"""Terminal release checks for the Typer/Rich command surface."""

from __future__ import annotations

import io
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from rich.console import Console
from typer.testing import CliRunner
from pydantic import BaseModel, ConfigDict

import cli
from core.tool_registry import ToolDefinition, ToolRegistry
from security.approval import ApprovalRequest
from ui import TerminalUI
from ui.prompt import ChatPrompt


def _audit_row() -> dict[str, object]:
    return {
        "timestamp": "2026-11-13T12:00:00+00:00",
        "event_type": "tool",
        "tool_name": "calculate",
        "execution_status": "success",
    }


def test_required_commands_are_registered() -> None:
    runner = CliRunner()
    result = runner.invoke(cli.app, ["--help"])
    assert result.exit_code == 0
    for command in ("chat", "tools", "config", "history", "doctor"):
        assert command in result.output


def test_tools_list_and_config_show_work_without_ollama(
    tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    runner = CliRunner()
    tools = runner.invoke(cli.app, ["tools", "list"])
    shown_config = runner.invoke(cli.app, ["config", "show"])
    assert tools.exit_code == 0
    assert "calculate" in tools.output
    assert shown_config.exit_code == 0
    assert '"ollama"' in shown_config.output


def test_history_reads_valid_audit_rows_and_rejects_symlinks(
    tmp_path: Path, monkeypatch: Any
) -> None:
    data_dir = tmp_path / "data"
    history_path = data_dir / "personal-ai-agent" / "audit.jsonl"
    history_path.parent.mkdir(parents=True)
    history_path.write_text(json.dumps(_audit_row()) + "\n", encoding="utf-8")
    monkeypatch.setenv("XDG_DATA_HOME", str(data_dir))
    runner = CliRunner()
    result = runner.invoke(cli.app, ["history"])
    assert result.exit_code == 0
    assert "calculate" in result.output

    history_path.unlink()
    history_path.symlink_to(tmp_path / "other.jsonl")
    blocked = runner.invoke(cli.app, ["history"])
    assert blocked.exit_code == 1
    assert "ramziy" in blocked.output


def test_rich_event_renderer_shows_tool_result_and_final_answer(monkeypatch: Any) -> None:
    buffer = io.StringIO()
    monkeypatch.setattr(cli, "console", Console(file=buffer, force_terminal=False, color_system=None))
    cli._agent_output("[reja] 1 bosqich")
    cli._agent_output("[step] Vosita: calculate")
    cli._agent_output("[natija] 4")
    cli._agent_output("Agent: Javob tayyor")
    rendered = buffer.getvalue()
    for expected in ("Bajarilish rejasi", "Tanlangan vosita", "calculate", "Vosita natijasi", "Agent"):
        assert expected in rendered


def test_interrupting_an_approval_cancels_it(monkeypatch: Any) -> None:
    terminal = TerminalUI(Console(file=io.StringIO(), force_terminal=False))
    monkeypatch.setattr(terminal, "approval", lambda *_args: "bekor")
    result = cli._RichApproval(terminal).request_approval(
        ApprovalRequest(approval_id="test-approval", action="write", target_resource="file", potential_impact="changes data")
    )
    assert result.decision == "cancel"


def test_terminal_ui_has_themes_and_renders_markdown() -> None:
    buffer = io.StringIO()
    terminal = TerminalUI(Console(file=buffer, force_terminal=False, color_system=None))
    assert terminal.set_theme("light")
    assert not terminal.set_theme("unknown")
    terminal.banner(model="test:model")
    terminal.render_event("Agent: **Tayyor**\n\n```python\nprint(1)\n```")
    terminal.error("Ulanish yo'q", "Ollama xizmatini tekshiring.")
    rendered = buffer.getvalue()
    for expected in ("SHAXSIY AI AGENT", "Agent", "print(1)", "Yechim"):
        assert expected in rendered


def test_terminal_ui_tracks_and_saves_conversation(
    tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    terminal = TerminalUI(Console(file=io.StringIO(), force_terminal=False))
    terminal.add_message("user", "Salom")
    terminal.add_message("assistant", "Assalomu alaykum")
    saved = terminal.save_history("sinov")
    assert saved == tmp_path / "personal-ai-agent" / "conversations" / "sinov.md"
    assert "Salom" in saved.read_text(encoding="utf-8")
    assert "Assalomu alaykum" in saved.read_text(encoding="utf-8")


def test_model_command_uses_ollama_model_list(
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(
        cli.ollama,
        "Client",
        lambda **_kwargs: SimpleNamespace(
            list=lambda: SimpleNamespace(
                models=[SimpleNamespace(model="gemma4:e2b-it-qat")]
            )
        ),
    )
    terminal = TerminalUI(Console(file=io.StringIO(), force_terminal=False))
    terminal.choose_model = lambda models: next(iter(models))
    selected: list[str] = []
    assert not cli._handle_slash_command(
        "/model",
        terminal,
        object(),
        SimpleNamespace(ollama=SimpleNamespace(host="http://localhost:11434", timeout=1)),
        on_model_change=selected.append,
    )
    assert selected == ["gemma4:e2b-it-qat"]


def test_theme_slash_command_supports_all_themes() -> None:
    terminal = TerminalUI(Console(file=io.StringIO(), force_terminal=False))
    assert not cli._handle_slash_command("/theme hacker", terminal, object(), object())
    assert terminal.theme_name == "hacker"


class _SearchArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str
    limit: int = 5


def _tool_registry(*, enabled: bool = True) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="web_search",
            description="Internetdan uzun tavsifli qidiruv natijalarini topadi.",
            category="network",
            argument_schema=_SearchArguments.model_json_schema(),
            argument_model=_SearchArguments,
            permission_level="READ_ONLY",
            execution_timeout=5,
            implementation=lambda **_kwargs: {},
        ),
        enabled=enabled,
    )
    return registry


def test_tools_command_lists_all_tools_and_status() -> None:
    buffer = io.StringIO()
    terminal = TerminalUI(Console(file=buffer, force_terminal=False, width=52))
    registry = _tool_registry()
    terminal.set_tool_registry(registry)

    assert not cli._handle_slash_command("/tools", terminal, registry, object())
    output = buffer.getvalue()
    for expected in ("Vositalar", "web_search", "yoqilgan", "Jami: 1 ta vosita"):
        assert expected in output


def test_tools_command_uses_requested_columns_on_regular_terminal() -> None:
    buffer = io.StringIO()
    terminal = TerminalUI(Console(file=buffer, force_terminal=False, width=110))
    registry = _tool_registry(enabled=False)
    terminal.set_tool_registry(registry)

    terminal.tools()
    output = buffer.getvalue()
    for expected in ("№", "Nomi", "Tavsif", "Parametrlar", "Holat", "o'chirilgan"):
        assert expected in output


def test_tools_detail_shows_schema_and_example() -> None:
    buffer = io.StringIO()
    terminal = TerminalUI(Console(file=buffer, force_terminal=False))
    registry = _tool_registry()
    terminal.set_tool_registry(registry)

    assert not cli._handle_slash_command(
        "/tools web_search", terminal, registry, object()
    )
    output = buffer.getvalue()
    for expected in ("Parametrlar", "query", "string", "majburiy", "Misol chaqiruvi"):
        assert expected in output


def test_tools_can_be_enabled_and_disabled_via_existing_registry_api() -> None:
    buffer = io.StringIO()
    terminal = TerminalUI(Console(file=buffer, force_terminal=False))
    registry = _tool_registry(enabled=False)
    terminal.set_tool_registry(registry)

    cli._handle_slash_command("/tools on web_search", terminal, registry, object())
    assert len(registry.list_tools(enabled_only=True)) == 1
    cli._handle_slash_command("/tools off web_search", terminal, registry, object())
    assert not registry.list_tools(enabled_only=True)
    assert "vositasi yoqildi" in buffer.getvalue()
    assert "vositasi o'chirildi" in buffer.getvalue()
    assert "javob vaqti" not in buffer.getvalue()


def test_tools_empty_registry_shows_friendly_panel() -> None:
    buffer = io.StringIO()
    terminal = TerminalUI(Console(file=buffer, force_terminal=False))
    terminal.set_tool_registry(ToolRegistry())

    terminal.tools()
    assert "Hozircha vositalar ulanmagan" in buffer.getvalue()


def test_tool_names_are_available_in_nested_completions() -> None:
    completions = ChatPrompt._completions(["web_search", "calculate"])
    assert "web_search" in completions["/tools"]
    assert "web_search" in completions["/tools"]["on"]
    assert "calculate" in completions["/tools"]["off"]


def test_chat_never_sends_slash_commands_or_empty_input_to_agent(
    tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    output = io.StringIO()
    test_console = Console(file=output, force_terminal=False, color_system=None)
    monkeypatch.setattr(cli, "console", test_console)

    commands = iter(
        [
            "/help",
            "/tools",
            "/tools web_search",
            "/model",
            "/theme dark",
            "/history",
            "/clear",
            "/save integration",
            "/abc",
            "",
            "Salom",
            "/exit",
        ]
    )
    terminals: list[TerminalUI] = []

    def create_terminal(console: Console) -> TerminalUI:
        terminal = TerminalUI(console)
        terminal.prompt = lambda **_kwargs: next(commands)  # type: ignore[method-assign]
        terminals.append(terminal)
        return terminal

    monkeypatch.setattr(cli, "TerminalUI", create_terminal)

    registry = _tool_registry()
    guard = SimpleNamespace(close=lambda: None)
    audit = SimpleNamespace(close=lambda: None)
    settings = SimpleNamespace(
        ollama=SimpleNamespace(
            model="gemma4:e2b-it-qat",
            host="http://localhost:11434",
            timeout=1,
        ),
        agent=SimpleNamespace(
            max_retries=0,
            max_steps=1,
            max_tool_calls=1,
            max_action_retries=0,
            max_replans=0,
            max_task_seconds=5,
        ),
    )
    monkeypatch.setattr(
        cli,
        "_runtime",
        lambda _terminal: (settings, registry, guard, audit, object()),
    )
    monkeypatch.setattr(cli, "OllamaClient", lambda **_kwargs: object())

    model_requests: list[str] = []

    class FakeController:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def run_task(self, task: str) -> SimpleNamespace:
            model_requests.append(task)
            return SimpleNamespace(
                status="completed",
                response="Salom!",
                steps=1,
                tool_calls=0,
                task_id="test",
            )

    monkeypatch.setattr(cli, "AgentController", FakeController)
    monkeypatch.setattr(
        cli.ollama,
        "Client",
        lambda **_kwargs: SimpleNamespace(
            list=lambda: SimpleNamespace(
                models=[SimpleNamespace(model="gemma4:e2b-it-qat")]
            )
        ),
    )

    cli.chat()

    assert model_requests == ["Salom"]
    assert terminals[0].messages == [("user", "Salom"), ("assistant", "Salom!")]
    assert output.getvalue().count("Joriy vazifa") == 1
    assert "O'ylayapman" not in output.getvalue()
    assert "Noma'lum buyruq: /abc. /help ni ko'ring" in output.getvalue()


def test_completion_summarizes_tool_call_counts_and_status() -> None:
    buffer = io.StringIO()
    terminal = TerminalUI(Console(file=buffer, force_terminal=False))
    terminal.set_tool_registry(_tool_registry())
    terminal.render_event("[step] Vosita: web_search")
    terminal.render_event("[step] Vosita: web_search")
    terminal.completion(
        status="completed", steps=2, calls=2, task_id="test", elapsed="0.5 s"
    )
    output = buffer.getvalue()
    assert "Vositalar: web_search ×2" in output
    assert "vositalar: 1" in output
