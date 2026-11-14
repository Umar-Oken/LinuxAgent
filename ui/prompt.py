"""Interactive prompt, input history, and slash-command completion."""

from __future__ import annotations

from typing import Any

from ui.theme import colors_enabled

try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.completion import NestedCompleter
    from prompt_toolkit.formatted_text import HTML
    from prompt_toolkit.history import InMemoryHistory
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.styles import Style

    PROMPT_TOOLKIT_AVAILABLE = True
except ImportError:  # pragma: no cover - minimal installations use input().
    PROMPT_TOOLKIT_AVAILABLE = False


SLASH_COMPLETIONS: dict[str, Any] = {
    "/help": None,
    "/clear": None,
    "/model": None,
    "/theme": {"dark": None, "light": None, "ocean": None, "hacker": None},
    "/history": None,
    "/save": None,
    "/exit": None,
}


class ChatPrompt:
    def __init__(self) -> None:
        self._session: Any = None

    def prompt(
        self,
        *,
        model: str,
        theme: str,
        messages: int,
        elapsed: str,
        interactive: bool,
        tool_names: list[str] | None = None,
    ) -> str:
        if not PROMPT_TOOLKIT_AVAILABLE or not interactive:
            return input("❯ ")
        if self._session is None:
            bindings = KeyBindings()

            @bindings.add("c-m")
            @bindings.add("escape", "enter")
            def submit(event: Any) -> None:
                event.current_buffer.validate_and_handle()

            style = Style.from_dict(
                {"prompt": "bold ansicyan" if colors_enabled() else ""}
            )
            self._session = PromptSession(
                history=InMemoryHistory(),
                completer=NestedCompleter.from_nested_dict(
                    self._completions(tool_names or [])
                ),
                complete_while_typing=True,
                multiline=True,
                key_bindings=bindings,
                style=style,
            )
        else:
            self._session.completer = NestedCompleter.from_nested_dict(
                self._completions(tool_names or [])
            )
        toolbar = HTML(
            f"<b>{model}</b> | mavzu: {theme} | xabar: {messages} | "
            f"vaqt: {elapsed} | Esc+Enter yuborish | /help"
        )
        return self._session.prompt(
            [("class:prompt", "❯ ")],
            bottom_toolbar=toolbar,
        )

    @staticmethod
    def _completions(tool_names: list[str]) -> dict[str, Any]:
        names = {name: None for name in tool_names}
        return {
            **SLASH_COMPLETIONS,
            "/tools": {
                **names,
                "on": names,
                "off": names,
            },
        }
