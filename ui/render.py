"""Rich-based rendering for chat events and responses."""

from __future__ import annotations

import time
import re

from rich.box import ROUNDED
from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from ui.theme import Theme, colors_enabled


class TerminalRenderer:
    def __init__(self, console: Console, theme: Theme) -> None:
        self.console = console
        self.theme = theme

    def set_theme(self, theme: Theme) -> None:
        self.theme = theme

    def event(self, message: str) -> None:
        if message.startswith("[reja] "):
            self.panel(message[7:], "Bajarilish rejasi", self.theme.accent)
        elif message.startswith("[step] Vosita: "):
            self.panel(message[15:], "Tanlangan vosita", self.theme.primary)
        elif message.startswith("[natija] "):
            self.panel(message[9:], "Vosita natijasi", self.theme.success)
        elif message.startswith("Agent: "):
            self.answer(message[7:])
        elif message.startswith("XATO:"):
            suggestion = (
                "`ollama serve` ni ishga tushiring va Ollama xizmatining manzilini tekshiring."
                if any(word in message.casefold() for word in ("ollama", "ulanish", "connection", "timeout"))
                else "So'rovni yoki sozlamalarni tekshiring."
            )
            self.error(message, suggestion)
        else:
            self.console.print(Text(message, style=self.theme.muted))

    def answer(self, message: str) -> None:
        def panel(text: str) -> Panel:
            return Panel(
                Markdown(text, code_theme="monokai"),
                title="[bold]Agent[/]",
                border_style=self.theme.success,
                box=ROUNDED,
                padding=(1, 2),
            )

        if not self.console.is_terminal:
            self.console.print(panel(message))
            return

        visible = ""
        words = re.findall(r"\S+\s*", message)
        with Live(panel(visible), console=self.console, refresh_per_second=20) as live:
            for word in words:
                visible += word
                live.update(panel(visible), refresh=True)
                time.sleep(0.012)

    def panel(self, text: str, title: str, color: str) -> None:
        self.console.print(
            Panel(Text(text), title=title, border_style=color, box=ROUNDED)
        )

    def error(self, message: str, suggestion: str) -> None:
        self.console.print(
            Panel(
                f"{message}\n\nYechim: {suggestion}",
                title="Xato",
                border_style=self.theme.error,
                box=ROUNDED,
            )
        )

    def status(
        self,
        *,
        model: str,
        theme: str,
        messages: int,
        elapsed: str,
        active_tools: int = 0,
    ) -> None:
        text = Text()
        text.append(" ● ", style=self.theme.success)
        text.append(f"{model}  ", style=f"bold {self.theme.primary}")
        text.append(f"mavzu: {theme}  ", style=self.theme.muted)
        text.append(f"vositalar: {active_tools}  ", style=self.theme.muted)
        text.append(f"xabarlar: {messages}  ", style=self.theme.muted)
        text.append(f"javob vaqti: {elapsed}", style=self.theme.muted)
        self.console.print(Rule(text, style=self.theme.muted))

    def menu(self, title: str, entries: list[tuple[str, str]]) -> str | None:
        try:
            from prompt_toolkit.shortcuts import radiolist_dialog
            from prompt_toolkit.styles import Style
        except ImportError:
            return None
        if not self.console.is_terminal:
            return None
        try:
            return radiolist_dialog(
                title=title,
                text="↑/↓ bilan tanlang, Enter bilan tasdiqlang.",
                values=entries,
                style=Style.from_dict({}) if not colors_enabled() else None,
            ).run()
        except (EOFError, KeyboardInterrupt):
            return None

    def table(self, title: str, headers: tuple[str, ...], rows: list[tuple[str, ...]]) -> None:
        table = Table(*headers, box=ROUNDED, border_style=self.theme.primary)
        for row in rows:
            table.add_row(*row)
        self.console.print(Panel(table, title=title, border_style=self.theme.primary))
