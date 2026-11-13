"""Rich/Typer terminal interface for the guarded agent."""

from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.text import Text
from rich.table import Table

from agent import SYSTEM_PROMPT, create_tool_registry
from core.agent_controller import AgentController
from core.config import ConfigurationError, get_data_dir, load_config
from core.llm_client import OllamaClient
from core.tool_dispatcher import ToolDispatcher
from security.approval import ApprovalRequest, ApprovalResponse, TrustedApprovalInterface
from security.approval import ApprovalManager
from security.audit_logger import AuditLogger
from security.path_guard import PathAccessError, PathGuard
from security.permissions import PermissionEngine, PermissionPolicy

app = typer.Typer(help="Xavfsiz terminal AI agenti.", no_args_is_help=True)
tools_app = typer.Typer(help="Ro'yxatdan o'tgan tool'lar.", no_args_is_help=True)
config_app = typer.Typer(help="Konfiguratsiya buyruqlari.", no_args_is_help=True)
app.add_typer(tools_app, name="tools")
app.add_typer(config_app, name="config")
console = Console()


class _RichApproval(TrustedApprovalInterface):
    def request_approval(self, request: ApprovalRequest) -> ApprovalResponse:
        details = (
            f"[bold]Amal:[/] {request.action}\n"
            f"[bold]Maqsad:[/] {request.target_resource}\n"
            f"[bold]Ta'sir:[/] {request.potential_impact}"
        )
        console.print(Panel(details, title="[yellow]Tasdiqlash so'rovi[/]", border_style="yellow"))
        try:
            answer = Prompt.ask("Tasdiqlaysizmi", choices=["ha", "yoq", "bekor"], default="yoq")
        except (EOFError, KeyboardInterrupt):
            console.print("\n[yellow]Tasdiqlash bekor qilindi.[/]")
            answer = "bekor"
        return ApprovalResponse(
            approval_id=request.approval_id,
            decision={"ha": "approve", "bekor": "cancel"}.get(answer, "deny"),
        )


def _agent_output(message: str) -> None:
    """Render controller events without treating controller text as Rich markup."""
    if message.startswith("[reja] "):
        console.print(Panel(Text(message[7:]), title="[cyan]Bajarilish rejasi[/]", border_style="cyan"))
    elif message.startswith("[step] Vosita: "):
        console.print(Panel(Text(message[15:]), title="[blue]Tanlangan tool[/]", border_style="blue"))
    elif message.startswith("[natija] "):
        console.print(Panel(Text(message[9:]), title="[green]Tool natijasi[/]", border_style="green"))
    elif message.startswith("Agent: "):
        console.print(Panel(Text(message[7:]), title="[bold green]Yakuniy javob[/]", border_style="green"))
    else:
        console.print(Text(message))


def _runtime() -> tuple[object, object, PathGuard, AuditLogger, ToolDispatcher]:
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
        permission_checker=PermissionEngine(PermissionPolicy(approval_policy=settings.security.approval_policy), ApprovalManager(_RichApproval(), audit_logger=audit)),
        audit_logger=audit,
    )
    return settings, registry, guard, audit, dispatcher


def _registry_runtime() -> tuple[object, object, PathGuard]:
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
    try:
        settings, registry, guard, audit, dispatcher = _runtime()
    except (ConfigurationError, PathAccessError, OSError) as exc:
        console.print(f"[red]XATO:[/] {exc}")
        raise typer.Exit(1)
    try:
        client = OllamaClient(model=settings.ollama.model, host=settings.ollama.host, timeout=settings.ollama.timeout, max_retries=settings.agent.max_retries)
        controller = AgentController(client=client, registry=registry, dispatcher=dispatcher, path_guard=guard, system_prompt=SYSTEM_PROMPT, max_steps=settings.agent.max_steps, max_tool_calls=settings.agent.max_tool_calls, max_action_retries=settings.agent.max_action_retries, max_replans=settings.agent.max_replans, max_task_seconds=settings.agent.max_task_seconds, output=_agent_output)
        console.print(Panel(
            f"[bold]Holat:[/] tayyor\n[bold]Model:[/] {settings.ollama.model}\n[bold]Limitlar:[/] {settings.agent.max_steps} bosqich, {settings.agent.max_tool_calls} tool\n[bold]Chiqish:[/] exit yoki Ctrl+C",
            title="[green]Terminal Agent v2.0[/]",
            border_style="green",
        ))
        while True:
            try:
                task = Prompt.ask("[bold cyan]Siz[/]").strip()
            except (EOFError, KeyboardInterrupt):
                console.print("\nXayr!")
                break
            if task.casefold() in {"exit", "quit", "chiqish"}:
                break
            if task:
                console.print(Panel(Text(task), title="[blue]Joriy vazifa[/]", border_style="blue"))
                with console.status("[bold cyan]Agent holati: ishlamoqda[/]"):
                    result = controller.run_task(task)
                color = "green" if result.status == "completed" else "yellow"
                console.print(
                    Panel(
                        f"[bold]Holat:[/] {result.status}\n[bold]Bosqichlar:[/] {result.steps}\n[bold]Tool chaqiruvlari:[/] {result.tool_calls}\n[bold]Vazifa ID:[/] {result.task_id}",
                        title=f"[bold {color}]Bajarilish yakuni[/]",
                        border_style=color,
                    )
                )
    finally:
        guard.close()
        audit.close()


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
