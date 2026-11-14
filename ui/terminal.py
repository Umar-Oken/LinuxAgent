"""Presentation facade for the terminal agent."""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Iterable

from rich.console import Console

from core.config import get_data_dir
from core.tool_registry import ToolRegistry, UnknownToolError
from ui.banner import render_banner
from ui.prompt import ChatPrompt, PROMPT_TOOLKIT_AVAILABLE
from ui.render import TerminalRenderer
from ui.theme import THEMES, Theme
from ui.tools_view import render_tool_detail, render_tools


class TerminalUI:
    """Keep terminal presentation separate from agent behavior."""

    def __init__(self, console: Console, *, theme: str = "dark") -> None:
        self.console = console
        self.theme: Theme = THEMES.get(theme, THEMES["dark"])
        self.model = "—"
        self.messages: list[tuple[str, str]] = []
        self._registry: ToolRegistry | None = None
        self._task_tools: Counter[str] = Counter()
        self._prompt = ChatPrompt()
        self._renderer = TerminalRenderer(console, self.theme)

    @property
    def theme_name(self) -> str:
        return self.theme.name

    def set_theme(self, name: str) -> bool:
        selected = THEMES.get(name.casefold())
        if selected is None:
            return False
        self.theme = selected
        self._renderer.set_theme(selected)
        return True

    def banner(self, *, model: str, version: str = "v2.0.0") -> None:
        self.model = model
        render_banner(self.console, self.theme, model=model, version=version)

    def status_line(self, *, elapsed: str = "—", tokens: str | None = None) -> None:
        active_tools = (
            len(self._registry.list_tools(enabled_only=True))
            if self._registry is not None
            else 0
        )
        self._renderer.status(
            model=self.model,
            theme=self.theme_name,
            messages=len(self.messages),
            elapsed=elapsed,
            active_tools=active_tools,
        )

    def set_tool_registry(self, registry: ToolRegistry) -> None:
        self._registry = registry

    def prompt(self, *, model: str, elapsed: str = "—") -> str:
        self.model = model
        tool_names = (
            [tool.name for tool in self._registry.list_tools()]
            if self._registry is not None
            else []
        )
        return self._prompt.prompt(
            model=model,
            theme=self.theme_name,
            messages=len(self.messages),
            elapsed=elapsed,
            interactive=self.console.is_terminal,
            tool_names=tool_names,
        )

    def render_event(self, message: str) -> None:
        if message.startswith("[step] Vosita: "):
            self._task_tools[message.removeprefix("[step] Vosita: ").strip()] += 1
        self._renderer.event(message)

    def task(self, task: str) -> None:
        self._renderer.panel(task, "Joriy vazifa", self.theme.primary)

    def completion(self, *, status: str, steps: int, calls: int, task_id: str, elapsed: str = "—") -> None:
        color = self.theme.success if status == "completed" else self.theme.warning
        labels = {
            "completed": "bajarildi",
            "cancelled": "bekor qilindi",
            "llm_error": "model xatosi",
            "invalid_response": "yaroqsiz javob",
            "time_limit": "vaqt limiti tugadi",
            "max_steps": "bosqich limiti tugadi",
        }
        used_tools = ", ".join(
            f"{name} ×{count}"
            for name, count in sorted(self._task_tools.items())
        ) or "yo'q"
        self._renderer.panel(
            f"Holat: {labels.get(status, status)}\nBosqichlar: {steps}\nVosita chaqiruvlari: {calls}\n"
            f"Vositalar: {used_tools}\nVazifa ID: {task_id}\nJavob vaqti: {elapsed}",
            "Bajarilish yakuni",
            color,
        )
        self._task_tools.clear()
        self.status_line(elapsed=elapsed)

    def approval(self, action: str, target: str, impact: str) -> str:
        self._renderer.panel(
            f"Amal: {action}\nMaqsad: {target}\nTa'sir: {impact}",
            "Tasdiqlash so'rovi",
            self.theme.warning,
        )
        if PROMPT_TOOLKIT_AVAILABLE and self.console.is_terminal:
            choice = self._renderer.menu(
                "Tasdiqlash so'rovi",
                [
                    ("ha", "Ha, tasdiqlayman"),
                    ("yoq", "Yo'q, rad etaman"),
                    ("bekor", "Bekor qilish"),
                ],
            )
            return choice or "bekor"
        try:
            answer = input("Tasdiqlaysizmi? [ha/yoq/bekor] (yoq): ").strip().casefold()
        except (EOFError, KeyboardInterrupt):
            return "bekor"
        return answer if answer in {"ha", "yoq", "bekor"} else "yoq"

    def menu(self) -> str | None:
        return self._renderer.menu(
            "Terminal Agent menyusi",
            [
                ("/help", "Yordam"),
                ("/tools", "Vositalar ro'yxati"),
                ("/model", "Modelni tanlash"),
                ("/theme", "Mavzuni almashtirish"),
                ("/history", "Suhbat tarixi"),
                ("/clear", "Ekranni tozalash"),
                ("/exit", "Chiqish"),
            ],
        )

    def choose_model(self, models: Iterable[str]) -> str | None:
        choices = list(dict.fromkeys(models))
        if not choices:
            return None
        return self._renderer.menu(
            "Ollama modellari",
            [(model, model) for model in choices],
        )

    def show_models(self, models: Iterable[str]) -> None:
        rows = [(model,) for model in models]
        self._renderer.table("Ollama modellari", ("Model",), rows)

    def choose_theme(self) -> str | None:
        return self._renderer.menu(
            "Mavzu",
            [
                ("dark", "Dark — tungi kontrast"),
                ("light", "Light — yorug' kontrast"),
                ("ocean", "Ocean — ko'k tuslar"),
                ("hacker", "Hacker — yashil tuslar"),
            ],
        )

    def begin_task(self) -> None:
        self._task_tools.clear()

    def tools(self) -> None:
        if self._registry is None:
            self._renderer.error("Vositalar ro'yxati mavjud emas.", "Agent registrini tekshiring.")
            return
        definitions = self._registry.list_tools()
        enabled = {
            tool.name for tool in self._registry.list_tools(enabled_only=True)
        }
        render_tools(self.console, self.theme, definitions, enabled)

    def tool_detail(self, name: str) -> None:
        if self._registry is None:
            self._renderer.error("Vositalar ro'yxati mavjud emas.", "Agent registrini tekshiring.")
            return
        try:
            definition = self._registry.get_tool(name, require_enabled=False)
        except UnknownToolError:
            self._renderer.error(f"'{name}' nomli vosita topilmadi.", "/tools buyrug'i bilan ro'yxatni ko'ring.")
            return
        enabled = name in {
            tool.name for tool in self._registry.list_tools(enabled_only=True)
        }
        render_tool_detail(self.console, self.theme, definition, enabled=enabled)

    def set_tool_enabled(self, name: str, *, enabled: bool) -> bool:
        if self._registry is None:
            self._renderer.error("Vositalar ro'yxati mavjud emas.", "Agent registrini tekshiring.")
            return False
        try:
            if enabled:
                self._registry.enable(name)
            else:
                self._registry.disable(name)
        except UnknownToolError:
            self._renderer.error(f"'{name}' nomli vosita topilmadi.", "/tools buyrug'i bilan ro'yxatni ko'ring.")
            return False
        state = "yoqildi" if enabled else "o'chirildi"
        self.console.print(f"'{name}' vositasi {state}.")
        return True

    def help(self) -> None:
        rows = [
            ("/help", "Mavjud buyruqlarni ko'rsatish"),
            ("/tools [nom|on|off <nom>]", "Vositalarni ko'rish, tafsilotlari va holatini boshqarish"),
            ("/clear", "Ekranni tozalash"),
            ("/model", "Ollama modelini tanlash"),
            ("/theme [nom]", "dark, light, ocean yoki hacker mavzusi"),
            ("/history", "Joriy suhbat tarixini ko'rsatish"),
            ("/save [fayl]", "Suhbatni Markdown faylga saqlash"),
            ("/exit", "Chiqish"),
        ]
        self._renderer.table("Yordam", ("Buyruq", "Vazifasi"), rows)
        self.console.print("Ko'p qatorli matn: Esc+Enter yoki Ctrl+Enter bilan yuboring.")

    def error(self, message: str, suggestion: str) -> None:
        self._renderer.error(message, suggestion)

    def add_message(self, role: str, content: str) -> None:
        self.messages.append((role, content))

    def show_history(self) -> None:
        if not self.messages:
            self.console.print("Suhbat tarixi hozircha bo'sh.")
            return
        rows = [
            (str(index), "Siz" if role == "user" else "Agent", content)
            for index, (role, content) in enumerate(self.messages, start=1)
        ]
        self._renderer.table("Suhbat tarixi", ("#", "Kim", "Xabar"), rows)

    def save_history(self, filename: str | None = None) -> Path:
        name = (filename or "").strip()
        if not name:
            name = f"suhbat-{datetime.now().astimezone():%Y%m%d-%H%M%S}.md"
        if Path(name).name != name or name in {".", ".."}:
            raise ValueError("Fayl nomi faqat oddiy nom bo'lishi kerak.")
        if not name.casefold().endswith(".md"):
            name += ".md"

        directory = get_data_dir() / "conversations"
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / name
        lines = ["# Suhbat tarixi", ""]
        for role, content in self.messages:
            title = "Siz" if role == "user" else "Agent"
            lines.extend((f"## {title}", "", content, ""))
        with target.open("x", encoding="utf-8") as saved:
            saved.write("\n".join(lines))
        return target
