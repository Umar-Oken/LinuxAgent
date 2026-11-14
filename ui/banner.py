"""Startup banner rendering."""

from __future__ import annotations

import shutil

from rich.align import Align
from rich.box import ROUNDED
from rich.console import Console, Group
from rich.panel import Panel
from rich.text import Text

from ui.theme import Theme


def render_banner(
    console: Console,
    theme: Theme,
    *,
    model: str,
    version: str,
) -> None:
    width = shutil.get_terminal_size(fallback=(80, 24)).columns
    if width < 60:
        console.print(
            Panel(
                f"Terminal Agent  {version}\nModel: {model}",
                title="AI YORDAMCHI",
                border_style=theme.primary,
                box=ROUNDED,
            )
        )
        return

    logo = Text(
        "  _____ _____ ____  __  __ ___ _   _   _    \n"
        " |_   _| ____|  _ \\|  \\/  |_ _| \\ | | / \\   \n"
        "   | | |  _| | |_) | |\\/| || ||  \\| |/ _ \\  \n"
        "   | | | |___|  _ <| |  | || || |\\  / ___ \\ \n"
        "   |_| |_____|_| \\_\\_|  |_|___|_| \\_/_/   \\_\\",
        style=f"bold {theme.primary}",
    )
    body = Group(
        Align.center(logo),
        Align.center(Text("SHAXSIY AI AGENT", style=f"bold {theme.accent}")),
        Align.center(Text(f"{version}   •   {model}", style=theme.muted)),
        Align.center(Text("/help — buyruqlar   •   Ctrl+C — chiqish", style=theme.muted)),
    )
    console.print(Panel(body, box=ROUNDED, border_style=theme.primary, padding=(1, 2)))
