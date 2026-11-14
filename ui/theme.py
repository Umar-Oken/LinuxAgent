"""Terminal themes shared by the presentation layer."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Theme:
    name: str
    primary: str
    accent: str
    success: str
    warning: str
    error: str
    muted: str


THEMES: dict[str, Theme] = {
    "dark": Theme("dark", "bright_cyan", "magenta", "bright_green", "yellow", "bright_red", "grey62"),
    "light": Theme("light", "blue", "dark_magenta", "green", "dark_orange", "red", "grey42"),
    "ocean": Theme("ocean", "bright_blue", "bright_cyan", "bright_green", "yellow", "bright_red", "steel_blue"),
    "hacker": Theme("hacker", "bright_green", "green", "bright_green", "yellow", "bright_red", "green"),
}


def colors_enabled() -> bool:
    """Respect the conventional opt-out for colored terminal output."""
    return not bool(os.environ.get("NO_COLOR"))
