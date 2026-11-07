"""Centralized safe application-error classification for terminal output."""

from __future__ import annotations

from security.path_guard import PathAccessError


class ApplicationErrorHandler:
    """Map internal exceptions to concise messages without exposing details."""

    def user_message(self, error: BaseException) -> str:
        if isinstance(error, PathAccessError):
            return "Fayl tizimiga kirish siyosati so'rovni rad etdi."
        error_name = type(error).__name__
        known_messages = {
            "LLMTransportError": "Ollama xizmatiga ulanib bo'lmadi. Xizmat va modelni tekshiring.",
            "LLMResponseError": "Modeldan yaroqsiz javob keldi. Iltimos, qayta urinib ko'ring.",
            "UnsupportedCapabilityError": "Tanlangan model kerakli tool chaqiruvini qo'llamaydi.",
            "ConfigurationError": "Konfiguratsiya noto'g'ri.",
        }
        return known_messages.get(error_name, "Kutilmagan xatolik yuz berdi.")
