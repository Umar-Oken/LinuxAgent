"""Terminal release checks for the Typer/Rich command surface."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

from rich.console import Console
from typer.testing import CliRunner

import cli
from security.approval import ApprovalRequest


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
    for expected in ("Bajarilish rejasi", "Tanlangan tool", "calculate", "Tool natijasi", "Yakuniy javob"):
        assert expected in rendered


def test_interrupting_an_approval_cancels_it(monkeypatch: Any) -> None:
    monkeypatch.setattr(cli, "console", Console(file=io.StringIO(), force_terminal=False))
    monkeypatch.setattr(cli.Prompt, "ask", lambda *_args, **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt()))
    result = cli._RichApproval().request_approval(
        ApprovalRequest(approval_id="test-approval", action="write", target_resource="file", potential_impact="changes data")
    )
    assert result.decision == "cancel"
