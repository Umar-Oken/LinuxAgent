"""Rich/Typer terminal interface for the guarded agent."""

from __future__ import annotations

import json
import os
import stat
import time
from pathlib import Path
from typing import Annotated, Callable

import ollama
import typer
from rich.console import Console
from rich.table import Table

from agent import SYSTEM_PROMPT, create_tool_registry
from core.agent_controller import AgentController
from core.config import ConfigurationError, get_data_dir, load_config
from core.llm_client import OllamaClient
from core.tool_registry import ToolRegistry
from core.tool_dispatcher import ToolDispatcher
from security.approval import ApprovalRequest, ApprovalResponse, TrustedApprovalInterface
from security.approval import ApprovalManager
from security.audit_logger import AuditLogger
from security.path_guard import PathAccessError, PathGuard
from security.permissions import PermissionEngine, PermissionPolicy
from ui import TerminalUI

app = typer.Typer(help="Xavfsiz terminal AI agenti.", no_args_is_help=True)
tools_app = typer.Typer(help="Ro'yxatdan o'tgan tool'lar.", no_args_is_help=True)
config_app = typer.Typer(help="Konfiguratsiya buyruqlari.", no_args_is_help=True)
app.add_typer(tools_app, name="tools")
app.add_typer(config_app, name="config")
console = Console(no_color=bool(os.environ.get("NO_COLOR")))


class _RichApproval(TrustedApprovalInterface):
    def __init__(self, terminal: TerminalUI | None = None) -> None:
        self._terminal = terminal or TerminalUI(console)

    def request_approval(self, request: ApprovalRequest) -> ApprovalResponse:
        answer = self._terminal.approval(
            request.action, request.target_resource, request.potential_impact
        )
        return ApprovalResponse(
            approval_id=request.approval_id,
            decision={"ha": "approve", "bekor": "cancel"}.get(answer, "deny"),
        )


def _agent_output(message: str) -> None:
    """Compatibility renderer used by direct callers and CLI tests."""
    TerminalUI(console).render_event(message)


def _runtime(terminal: TerminalUI | None = None) -> tuple[object, ToolRegistry, PathGuard, AuditLogger, ToolDispatcher]:
    settings = load_config()
    audit = AuditLogger(retention_days=settings.logging.retention)
    guard = PathGuard(settings.security.allowed_roots, protected_paths=settings.security.protected_paths)
    registry = create_tool_registry(
        guard,
        enabled_categories=settings.tools.enabled_categories,
        connectivity_endpoint=settings.network.connectivity_endpoint,
        exchange_rate_providers=settings.network.exchange_rate_providers,
        web_timeout_seconds=settings.network.web_timeout_seconds,
        web_max_content_bytes=settings.network.web_max_content_bytes,
    )
    dispatcher = ToolDispatcher(
        registry,
        permission_checker=PermissionEngine(PermissionPolicy(approval_policy=settings.security.approval_policy), ApprovalManager(_RichApproval(terminal), audit_logger=audit)),
        audit_logger=audit,
    )
    return settings, registry, guard, audit, dispatcher


def _registry_runtime() -> tuple[object, ToolRegistry, PathGuard]:
    """Build the read-only tool listing without creating an audit log."""
    settings = load_config()
    guard = PathGuard(settings.security.allowed_roots, protected_paths=settings.security.protected_paths)
    registry = create_tool_registry(
        guard,
        enabled_categories=settings.tools.enabled_categories,
        connectivity_endpoint=settings.network.connectivity_endpoint,
        exchange_rate_providers=settings.network.exchange_rate_providers,
        web_timeout_seconds=settings.network.web_timeout_seconds,
        web_max_content_bytes=settings.network.web_max_content_bytes,
    )
    return settings, registry, guard


@app.command()
def chat() -> None:
    """Interaktiv chatni ishga tushirish."""
    terminal = TerminalUI(console)
    try:
        settings, registry, guard, audit, dispatcher = _runtime(terminal)
    except (ConfigurationError, PathAccessError, OSError) as exc:
        terminal.error(f"XATO: {exc}", "Konfiguratsiya va ruxsat berilgan yo'llarni tekshiring.")
        raise typer.Exit(1)
    try:
        terminal.set_tool_registry(registry)
        active_model = settings.ollama.model

        def build_controller(model: str) -> AgentController:
            client = OllamaClient(model=model, host=settings.ollama.host, timeout=settings.ollama.timeout, max_retries=settings.agent.max_retries)
            return AgentController(client=client, registry=registry, dispatcher=dispatcher, path_guard=guard, system_prompt=SYSTEM_PROMPT, max_steps=settings.agent.max_steps, max_tool_calls=settings.agent.max_tool_calls, max_action_retries=settings.agent.max_action_retries, max_replans=settings.agent.max_replans, max_task_seconds=settings.agent.max_task_seconds, output=terminal.render_event)

        controller = build_controller(active_model)

        def change_model(model: str) -> None:
            nonlocal active_model, controller
            active_model = model
            controller = build_controller(model)

        terminal.banner(model=settings.ollama.model)
        terminal.status_line()
        while True:
            try:
                task = terminal.prompt(model=active_model).strip()
            except (EOFError, KeyboardInterrupt):
                console.print("\nXayr!")
                break

            if task.startswith("/"):
                if _handle_slash_command(
                    task,
                    terminal,
                    registry,
                    settings,
                    on_model_change=change_model,
                ):
                    console.print("Xayr!")
                    break
                continue
            if not task:
                continue

            if task.casefold() in {"exit", "quit", "chiqish"}:
                console.print("Xayr!")
                break
            terminal.begin_task()
            terminal.add_message("user", task)
            terminal.task(task)
            started = time.monotonic()
            with console.status("[bold cyan]O'ylayapman...[/]"):
                result = controller.run_task(task)
            elapsed = f"{time.monotonic() - started:.1f} s"
            if result.response:
                terminal.add_message("assistant", result.response)
            terminal.completion(status=result.status, steps=result.steps, calls=result.tool_calls, task_id=result.task_id, elapsed=elapsed)
            if result.status == "cancelled":
                console.print("\nXayr!")
                break
    finally:
        guard.close()
        audit.close()


def _handle_slash_command(
    task: str,
    terminal: TerminalUI,
    registry: object,
    settings: object,
    *,
    on_model_change: Callable[[str], None] | None = None,
) -> bool:
    """Handle presentation-only chat commands; return true only to exit chat."""
    if not task.startswith("/"):
        return False
    parts = task.split(maxsplit=1)
    command = parts[0]
    argument = parts[1] if len(parts) == 2 else ""
    command = command.casefold()
    argument = argument.strip()
    if command == "/exit":
        return True
    if command == "/help":
        terminal.help()
    elif command == "/menu":
        choice = terminal.menu()
        if choice == "/exit":
            return True
        if choice:
            return _handle_slash_command(
                choice, terminal, registry, settings, on_model_change=on_model_change
            )
    elif command == "/tools":
        if not argument:
            terminal.tools()
        else:
            tool_parts = argument.split(maxsplit=1)
            action = tool_parts[0]
            action = action.casefold()
            name = tool_parts[1].strip() if len(tool_parts) == 2 else ""
            if action in {"on", "off"}:
                if not name:
                    terminal.error(
                        f"/tools {action} buyrug'ida vosita nomi kerak.",
                        "/tools on <nom> yoki /tools off <nom> ko'rinishida yozing.",
                    )
                else:
                    terminal.set_tool_enabled(name, enabled=action == "on")
            else:
                terminal.tool_detail(argument)
    elif command == "/model":
        try:
            response = ollama.Client(
                host=settings.ollama.host, timeout=settings.ollama.timeout
            ).list()
            models = [
                name
                for item in getattr(response, "models", [])
                if isinstance(
                    name := getattr(item, "model", None) or getattr(item, "name", None),
                    str,
                )
            ]
        except Exception as exc:
            terminal.error(
                f"Ollama modellari olinmadi: {type(exc).__name__}.",
                "`ollama serve` ni ishga tushirib, Ollama bilan ulanishni tekshiring.",
            )
            return False
        if argument:
            selected = next(
                (name for name in models if name.casefold() == argument.casefold()),
                None,
            )
            if selected is None:
                terminal.error(
                    f"Model topilmadi: {argument}",
                    "Mavjud modellarni /model buyrug'i bilan tanlang.",
                )
                return False
        else:
            selected = terminal.choose_model(models)
        if selected and on_model_change is not None:
            on_model_change(selected)
            terminal.model = selected
            console.print(f"Model almashtirildi: {selected}")
        elif not models:
            terminal.error(
                "Ollama'da model topilmadi.",
                "`ollama pull gemma4:e2b-it-qat` buyrug'ini bajaring.",
            )
        elif selected is None:
            terminal.show_models(models)
    elif command == "/theme":
        selected = argument.casefold() or terminal.choose_theme()
        if selected and terminal.set_theme(selected):
            console.print(f"Mavzu o'zgartirildi: {selected}")
        else:
            terminal.error("Mavzu topilmadi.", "dark, light, ocean yoki hacker mavzusidan foydalaning.")
    elif command == "/clear":
        console.clear()
        terminal.banner(model=terminal.model)
    elif command == "/history":
        terminal.show_history()
    elif command == "/save":
        try:
            saved = terminal.save_history(argument or None)
        except (OSError, ValueError) as exc:
            terminal.error(f"Suhbat saqlanmadi: {exc}", "Fayl nomini tekshiring.")
        else:
            console.print(f"Suhbat saqlandi: {saved}")
    else:
        terminal.error(
            f"Noma'lum buyruq: {command}. /help ni ko'ring",
            "Buyruq agentga yuborilmadi.",
        )
    return False


@tools_app.command("list")
def tools_list() -> None:
    """Ro'yxatdan o'tgan tool'larni ko'rsatish."""
    try:
        _settings, registry, guard = _registry_runtime()
    except (ConfigurationError, PathAccessError, OSError) as exc:
        console.print(f"[red]XATO:[/] {exc}")
        raise typer.Exit(1)
    try:
        table = Table("Tool", "Kategoriya", "Ruxsat", "Tavsif")
        for tool in registry.list_tools(enabled_only=True):
            table.add_row(tool.name, tool.category, tool.permission_level, tool.description)
        console.print(table)
    finally:
        guard.close()


@config_app.command("show")
def config_show() -> None:
    """Tekshirilgan samarali konfiguratsiyani ko'rsatish."""
    try:
        console.print_json(json.dumps(load_config().model_dump(mode="json"), ensure_ascii=False))
    except ConfigurationError as exc:
        console.print(f"[red]XATO:[/] {exc}")
        raise typer.Exit(1)


@app.command()
def history(limit: Annotated[int, typer.Option(min=1, max=100)] = 20) -> None:
    """Redakt qilingan audit tarixini ko'rsatish."""
    path = get_data_dir() / "audit.jsonl"
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        console.print("Audit tarixi hali yo'q.")
        return
    except OSError as exc:
        console.print(f"[red]XATO:[/] audit tarixini o'qib bo'lmadi: {exc}")
        raise typer.Exit(1)
    if path.is_symlink() or not stat.S_ISREG(mode):
        console.print("[red]XATO:[/] audit tarixi oddiy, ramziy bo'lmagan fayl bo'lishi kerak.")
        raise typer.Exit(1)
    rows = path.read_text(encoding="utf-8").splitlines()[-limit:]
    table = Table("Vaqt", "Hodisa", "Tool", "Holat")
    for row in rows:
        try:
            event = json.loads(row)
            table.add_row(event["timestamp"], event["event_type"], event["tool_name"], event["execution_status"])
        except (json.JSONDecodeError, KeyError):
            continue
    console.print(table)


@app.command()
def doctor() -> None:
    """Konfiguratsiya va Ollama ulanishi bo'yicha tezkor tekshiruv."""
    try:
        settings = load_config()
        client = OllamaClient(model=settings.ollama.model, host=settings.ollama.host, timeout=min(settings.ollama.timeout, 5), max_retries=0)
        client.get_capabilities()
    except Exception as exc:
        console.print(f"[red]Doctor xatosi:[/] {type(exc).__name__}")
        raise typer.Exit(1)
    console.print("[green]OK:[/] konfiguratsiya va Ollama tool-calling tekshirildi.")


if __name__ == "__main__":
    app()
