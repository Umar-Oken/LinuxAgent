"""Bounded, guarded document readers for untrusted local files."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Literal

from core.tool_dispatcher import ToolExecutionError
from security.path_guard import PathAccessError, PathGuard

DocumentFormat = Literal["pdf", "docx", "csv", "xlsx"]
MAX_DOCUMENT_BYTES = 10 * 1024 * 1024
MAX_PROCESSING_SECONDS = 8.0
MAX_RESULT_BYTES = 12 * 1024
_WORKER_PATH = Path(__file__).with_name("document_worker.py")
_SUPPORTED_SUFFIXES: dict[DocumentFormat, frozenset[str]] = {
    "pdf": frozenset({".pdf"}),
    "docx": frozenset({".docx"}),
    "csv": frozenset({".csv"}),
    "xlsx": frozenset({".xlsx"}),
}


def read_document(
    path: str | Path,
    document_format: DocumentFormat,
    path_guard: PathGuard,
    *,
    page_limit: int = 10,
    row_limit: int = 100,
    paragraph_limit: int = 100,
    sheet_limit: int = 5,
) -> dict[str, object]:
    """Read a supported document through a bounded, isolated parser process."""
    started = time.monotonic()
    if document_format not in _SUPPORTED_SUFFIXES:
        raise ToolExecutionError(
            "UNSUPPORTED_DOCUMENT",
            "Hujjat formati qo'llab-quvvatlanmaydi.",
        )
    if (
        not 1 <= page_limit <= 50
        or not 1 <= row_limit <= 500
        or not 1 <= paragraph_limit <= 200
        or not 1 <= sheet_limit <= 20
    ):
        raise ToolExecutionError(
            "INVALID_DOCUMENT_LIMIT",
            "Hujjatni o'qish limiti ruxsat etilgan oraliqda emas.",
        )
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    if candidate.suffix.casefold() not in _SUPPORTED_SUFFIXES[document_format]:
        raise ToolExecutionError(
            "UNSUPPORTED_DOCUMENT",
            f"Faqat {', '.join(sorted(_SUPPORTED_SUFFIXES[document_format]))} fayllar qo'llab-quvvatlanadi.",
        )

    try:
        descriptor = path_guard.open_file(candidate)
    except PathAccessError as exc:
        raise ToolExecutionError(
            exc.code,
            exc.safe_message,
        ) from None

    try:
        file_size = os.fstat(descriptor).st_size
        if file_size > MAX_DOCUMENT_BYTES:
            raise ToolExecutionError(
                "DOCUMENT_TOO_LARGE",
                "Hujjat belgilangan 10 MiB hajm limitidan katta.",
            )
        content = bytearray()
        while True:
            if time.monotonic() - started >= MAX_PROCESSING_SECONDS:
                raise ToolExecutionError(
                    "DOCUMENT_TIMEOUT",
                    "Hujjatni o'qish uchun vaqt limiti tugadi.",
                )
            chunk = os.read(descriptor, min(64 * 1024, MAX_DOCUMENT_BYTES + 1 - len(content)))
            if not chunk:
                break
            content.extend(chunk)
            if len(content) > MAX_DOCUMENT_BYTES:
                raise ToolExecutionError(
                    "DOCUMENT_TOO_LARGE",
                    "Hujjat belgilangan 10 MiB hajm limitidan katta.",
                )
    except OSError:
        raise ToolExecutionError(
            "DOCUMENT_READ_ERROR",
            "Hujjatni xavfsiz o'qib bo'lmadi.",
        ) from None
    finally:
        os.close(descriptor)

    remaining = MAX_PROCESSING_SECONDS - (time.monotonic() - started)
    if remaining <= 0:
        raise ToolExecutionError(
            "DOCUMENT_TIMEOUT",
            "Hujjatni qayta ishlash vaqt limiti tugadi.",
        )
    limits = {
        "pdf": page_limit,
        "docx": paragraph_limit,
        "csv": row_limit,
        "xlsx": row_limit,
    }
    try:
        worker = subprocess.Popen(
            [
                sys.executable,
                "-I",
                str(_WORKER_PATH),
                document_format,
                str(limits[document_format]),
                str(sheet_limit),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    except OSError:
        raise ToolExecutionError(
            "DOCUMENT_PROCESS_ERROR",
            "Hujjatni xavfsiz qayta ishlash jarayonini ishga tushirib bo'lmadi.",
        ) from None
    try:
        stdout, _ = worker.communicate(
            input=bytes(content),
            timeout=min(remaining, MAX_PROCESSING_SECONDS),
        )
    except subprocess.TimeoutExpired:
        worker.kill()
        worker.communicate()
        raise ToolExecutionError(
            "DOCUMENT_TIMEOUT",
            "Hujjatni qayta ishlash vaqt limiti tugadi.",
        ) from None
    if worker.returncode == -signal.SIGXCPU:
        raise ToolExecutionError(
            "DOCUMENT_TIMEOUT",
            "Hujjatni qayta ishlash uchun vaqt limiti tugadi.",
        )
    if worker.returncode != 0:
        raise ToolExecutionError(
            "INVALID_DOCUMENT",
            "Hujjat buzilgan, shifrlangan yoki qo'llab-quvvatlanmaydigan formatda.",
        )
    if len(stdout) > MAX_RESULT_BYTES:
        raise ToolExecutionError(
            "DOCUMENT_RESULT_TOO_LARGE",
            "Ajratilgan hujjat natijasi belgilangan hajm limitidan oshdi.",
        )
    try:
        response = json.loads(stdout)
    except (UnicodeError, json.JSONDecodeError):
        raise ToolExecutionError(
            "INVALID_DOCUMENT",
            "Hujjatni qayta ishlash natijasi yaroqsiz.",
        ) from None
    if not isinstance(response, dict) or response.get("ok") is not True:
        code = response.get("error_code") if isinstance(response, dict) else None
        message = response.get("safe_error_message") if isinstance(response, dict) else None
        if not isinstance(code, str) or not isinstance(message, str):
            code, message = (
                "INVALID_DOCUMENT",
                "Hujjat buzilgan yoki qo'llab-quvvatlanmaydigan formatda.",
            )
        raise ToolExecutionError(code, message)

    data = response.get("data")
    if not isinstance(data, dict):
        raise ToolExecutionError(
            "INVALID_DOCUMENT",
            "Hujjatni qayta ishlash natijasi yaroqsiz.",
        )
    data["file_name"] = candidate.name[:255]
    data["size_bytes"] = len(content)
    data["untrusted_content"] = True
    if len(json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")) > MAX_RESULT_BYTES:
        raise ToolExecutionError(
            "DOCUMENT_RESULT_TOO_LARGE",
            "Ajratilgan hujjat natijasi belgilangan hajm limitidan oshdi.",
        )
    return data
