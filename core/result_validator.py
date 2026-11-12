"""Tool natijalarini tekshirish va xavfsiz qayta urinish siyosati."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import zipfile
import zlib
from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from core.tool_dispatcher import ToolResult
from security.path_guard import PathAccessError, PathGuard

ValidationStatus = Literal["valid", "invalid", "partial", "uncertain"]
RetryDisposition = Literal["retry", "do_not_retry"]

_PARTIAL_ERRORS = {"PARTIAL_FAILURE", "AUDIT_LOG_ERROR", "DOCUMENT_CLEANUP_FAILED"}
_UNCERTAIN_ERRORS = {"TIMEOUT", "INTERNAL_ERROR", "DISPATCH_ERROR"}
_READ_ONLY_RETRY_ERRORS = {
    "TIMEOUT",
    "NETWORK_TIMEOUT",
    "NETWORK_ERROR",
    "TEMPORARY_UNAVAILABLE",
    "SYSTEM_METRICS_UNAVAILABLE",
}
_DOCUMENT_FORMATS = {
    "read_pdf": "pdf",
    "read_docx": "docx",
    "read_csv": "csv",
    "read_xlsx": "xlsx",
}
_DOCUMENT_FIELDS = {
    "pdf": {"metadata", "page_count", "pages", "pages_truncated"},
    "docx": {"metadata", "paragraphs", "tables"},
    "csv": {"metadata", "headers", "rows", "rows_read"},
    "xlsx": {"metadata", "sheet_count", "sheets"},
}
_CREATE_DOCUMENT_TOOLS = {
    "create_markdown_document",
    "create_docx_document",
    "create_csv_document",
    "create_xlsx_document",
}
_MAX_ARCHIVE_VALIDATION_BYTES = 110 * 1024 * 1024
_MAX_ARCHIVE_VALIDATION_FILES = 1000
_MAX_ARCHIVE_VALIDATION_CONTENT = 100 * 1024 * 1024


class ResultValidation(BaseModel):
    """Tekshiruv xulosasi va ushbu amal uchun deterministik idempotency key."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: ValidationStatus
    message: str
    idempotency_key: str = Field(min_length=64, max_length=64)
    retry: RetryDisposition = "do_not_retry"


class ToolResultValidator:
    """Tekshiriladigan natijalarni qayta bajarishdan himoya qiluvchi validator."""

    def __init__(self, path_guard: PathGuard, *, max_retries: int = 1) -> None:
        if max_retries < 0:
            raise ValueError("max_retries manfiy bo'lmasligi kerak")
        self._path_guard = path_guard
        self._max_retries = max_retries
        self._attempts: dict[str, int] = {}
        self._last_results: dict[str, ResultValidation] = {}

    def start_task(self) -> None:
        """Yangi vazifada avvalgi vazifaning idempotency holatini tozalash."""
        self._attempts.clear()
        self._last_results.clear()

    def begin_execution(
        self,
        task_id: str,
        tool_name: str,
        arguments: Mapping[str, object],
    ) -> tuple[bool, str]:
        """Takroriy yoki xavfli qayta bajarishni dispatcher'dan oldin to'xtatish."""
        key = _idempotency_key(task_id, tool_name, arguments)
        attempts = self._attempts.get(key, 0)
        if attempts == 0:
            self._attempts[key] = 1
            return True, key
        previous = self._last_results.get(key)
        retry_allowed = (
            previous is not None
            and previous.retry == "retry"
            and attempts <= self._max_retries
        )
        if retry_allowed:
            self._attempts[key] = attempts + 1
            return True, key
        return False, key

    def validate(
        self,
        task_id: str,
        tool_name: str,
        arguments: Mapping[str, object],
        result: ToolResult,
        *,
        permission_level: str,
    ) -> ResultValidation:
        """Natijani strategiyaga ko'ra tekshiradi va keyingi retry siyosatini qaytaradi."""
        key = _idempotency_key(task_id, tool_name, arguments)
        validation = self._validate_result(
            key,
            tool_name,
            arguments,
            result,
            permission_level=permission_level,
        )
        self._last_results[key] = validation
        return validation

    def _validate_result(
        self,
        key: str,
        tool_name: str,
        arguments: Mapping[str, object],
        result: ToolResult,
        *,
        permission_level: str,
    ) -> ResultValidation:
        if result.tool_name != tool_name:
            return ResultValidation(
                status="invalid",
                message="Tool natijasidagi nom so'ralgan tool bilan mos kelmadi.",
                idempotency_key=key,
            )
        if result.status != "success":
            code = result.error_code or "TOOL_ERROR"
            status: ValidationStatus = (
                "partial"
                if code in _PARTIAL_ERRORS
                else "uncertain"
                if code in _UNCERTAIN_ERRORS
                else "invalid"
            )
            retry: RetryDisposition = "do_not_retry"
            if (
                permission_level == "READ_ONLY"
                and code in _READ_ONLY_RETRY_ERRORS
                and self._attempts.get(key, 0) <= self._max_retries
            ):
                retry = "retry"
            return ResultValidation(
                status=status,
                message=(
                    "Amal qisman bajarilgan; ta'sirini tekshirmasdan qayta urinmang."
                    if status == "partial"
                    else "Amal bajarilgan-bajarilmagani noaniq; holatini tekshiring."
                    if status == "uncertain"
                    else "Tool amali muvaffaqiyatsiz tugadi."
                ),
                idempotency_key=key,
                retry=retry,
            )

        data = result.result_data
        try:
            if tool_name in {"copy_file", "move_file", "rename_file"}:
                _validate_transfer(tool_name, arguments, data, self._path_guard)
            elif tool_name == "create_directory":
                _validate_directory(arguments, data, self._path_guard)
            elif tool_name in _CREATE_DOCUMENT_TOOLS:
                _validate_created_document(
                    tool_name,
                    arguments,
                    data,
                    self._path_guard,
                )
            elif tool_name == "create_archive":
                _validate_created_archive(arguments, data, self._path_guard)
            elif tool_name == "extract_archive":
                _validate_extracted_archive(arguments, data, self._path_guard)
            elif tool_name in _DOCUMENT_FORMATS:
                _validate_read_document(tool_name, data)
            elif data is None:
                raise _InvalidResult
        except (
            PathAccessError,
            OSError,
            ValueError,
            TypeError,
            RuntimeError,
            NotImplementedError,
            EOFError,
            zlib.error,
            zipfile.BadZipFile,
            _InvalidResult,
        ):
            return ResultValidation(
                status="invalid",
                message="Tool natijasi kutilgan holat yoki fayl tizimi bilan mos kelmadi.",
                idempotency_key=key,
            )
        return ResultValidation(
            status="valid",
            message="Tool natijasi tekshiruvdan o'tdi.",
            idempotency_key=key,
        )


class _InvalidResult(Exception):
    pass


def _idempotency_key(
    task_id: str,
    tool_name: str,
    arguments: Mapping[str, object],
) -> str:
    try:
        payload = json.dumps(
            {"task_id": task_id, "tool": tool_name, "arguments": arguments},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError):
        payload = repr(
            (
                task_id,
                tool_name,
                sorted(arguments.items(), key=lambda item: str(item[0])),
            )
        )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise _InvalidResult
    return value


def _validate_transfer(
    tool_name: str,
    arguments: Mapping[str, object],
    data: object,
    guard: PathGuard,
) -> None:
    result = _mapping(data)
    source_arg, destination_arg = arguments.get("source"), arguments.get("destination")
    if not isinstance(source_arg, str) or not isinstance(destination_arg, str):
        raise _InvalidResult
    if (
        result.get("operation") != tool_name
        or result.get("status") != "success"
        or not isinstance(result.get("size_bytes"), int)
        or isinstance(result.get("size_bytes"), bool)
    ):
        raise _InvalidResult
    source = guard.authorize_path(
        source_arg,
        allow_missing=tool_name in {"move_file", "rename_file"},
    )
    destination = guard.authorize_path(destination_arg)
    if (
        result.get("source") != _public_path_for(source, guard)
        or result.get("destination") != _public_path_for(destination, guard)
    ):
        raise _InvalidResult
    if tool_name == "copy_file":
        source_fd = guard.open_file(source)
        try:
            source_size = os.fstat(source_fd).st_size
        finally:
            os.close(source_fd)
        if source_size != result["size_bytes"]:
            raise _InvalidResult
    elif source.exists():
        raise _InvalidResult
    destination_fd = guard.open_file(destination)
    try:
        if os.fstat(destination_fd).st_size != result["size_bytes"]:
            raise _InvalidResult
    finally:
        os.close(destination_fd)


def _validate_directory(
    arguments: Mapping[str, object],
    data: object,
    guard: PathGuard,
) -> None:
    result = _mapping(data)
    path = arguments.get("path")
    if (
        not isinstance(path, str)
        or result.get("operation") != "create_directory"
        or result.get("status") != "success"
        or not isinstance(result.get("path"), str)
    ):
        raise _InvalidResult
    directory = guard.authorize_path(path)
    if result["path"] != guard.public_path(directory):
        raise _InvalidResult
    descriptor = guard.open_directory(directory)
    os.close(descriptor)


def _validate_created_document(
    tool_name: str,
    arguments: Mapping[str, object],
    data: object,
    guard: PathGuard,
) -> None:
    result = _mapping(data)
    path = arguments.get("path")
    size = result.get("size_bytes")
    if (
        not isinstance(path, str)
        or result.get("operation") != tool_name
        or result.get("status") != "success"
        or result.get("verified") is not True
        or not isinstance(size, int)
        or isinstance(size, bool)
        or size < 1
    ):
        raise _InvalidResult
    document = guard.authorize_path(path)
    if result.get("path") != guard.public_path(document):
        raise _InvalidResult
    descriptor = guard.open_file(document)
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode) or details.st_size != size:
            raise _InvalidResult
    finally:
        os.close(descriptor)


def _validate_created_archive(
    arguments: Mapping[str, object],
    data: object,
    guard: PathGuard,
) -> None:
    result = _mapping(data)
    archive_path = arguments.get("archive_path")
    files = result.get("files")
    source_bytes = result.get("source_bytes")
    if (
        not isinstance(archive_path, str)
        or result.get("operation") != "create_archive"
        or result.get("status") != "success"
        or not isinstance(files, int)
        or isinstance(files, bool)
        or files < 0
        or not isinstance(source_bytes, int)
        or isinstance(source_bytes, bool)
        or source_bytes < 0
    ):
        raise _InvalidResult
    path = guard.authorize_path(archive_path)
    if result.get("archive") != guard.public_path(path):
        raise _InvalidResult
    descriptor = guard.open_file(path)
    try:
        if os.fstat(descriptor).st_size > _MAX_ARCHIVE_VALIDATION_BYTES:
            raise _InvalidResult
        with (
            os.fdopen(os.dup(descriptor), "rb") as stream,
            zipfile.ZipFile(stream) as archive,
        ):
            infos = archive.infolist()
            if len(infos) > _MAX_ARCHIVE_VALIDATION_FILES:
                raise _InvalidResult
            entries = []
            total_content = 0
            for item in infos:
                member_path = PurePosixPath(item.filename)
                mode = (item.external_attr >> 16) & 0xFFFF
                kind = stat.S_IFMT(mode)
                if (
                    member_path.is_absolute()
                    or ".." in member_path.parts
                    or "\\" in item.filename
                    or kind not in {0, stat.S_IFREG, stat.S_IFDIR}
                ):
                    raise _InvalidResult
                if not item.is_dir():
                    entries.append(item)
                    total_content += item.file_size
                    if total_content > _MAX_ARCHIVE_VALIDATION_CONTENT:
                        raise _InvalidResult
            if (
                len(entries) != files
                or total_content != source_bytes
                or archive.testzip() is not None
            ):
                raise _InvalidResult
    finally:
        os.close(descriptor)


def _validate_extracted_archive(
    arguments: Mapping[str, object],
    data: object,
    guard: PathGuard,
) -> None:
    result = _mapping(data)
    destination = arguments.get("destination_directory")
    expected_files = result.get("files")
    if (
        not isinstance(destination, str)
        or result.get("operation") != "extract_archive"
        or result.get("status") != "success"
        or not isinstance(expected_files, int)
        or isinstance(expected_files, bool)
        or expected_files < 0
    ):
        raise _InvalidResult
    directory = guard.authorize_path(destination)
    if result.get("destination") != guard.public_path(directory):
        raise _InvalidResult
    descriptor = guard.open_directory(directory)
    try:
        actual_files = _count_regular_files(descriptor, expected_files)
    finally:
        os.close(descriptor)
    if actual_files < expected_files:
        raise _InvalidResult


def _count_regular_files(directory_fd: int, minimum: int) -> int:
    if minimum == 0:
        return 0
    count = 0
    pending = [os.dup(directory_fd)]
    while pending:
        current_fd = pending.pop()
        try:
            for name in os.listdir(current_fd):
                details = os.stat(name, dir_fd=current_fd, follow_symlinks=False)
                if stat.S_ISREG(details.st_mode):
                    count += 1
                    if count >= minimum:
                        for descriptor in pending:
                            os.close(descriptor)
                        return count
                elif stat.S_ISDIR(details.st_mode):
                    pending.append(
                        os.open(
                            name,
                            os.O_RDONLY
                            | os.O_DIRECTORY
                            | os.O_CLOEXEC
                            | os.O_NOFOLLOW,
                            dir_fd=current_fd,
                        )
                    )
                elif stat.S_ISLNK(details.st_mode):
                    raise _InvalidResult
        except Exception:
            for descriptor in pending:
                os.close(descriptor)
            raise
        finally:
            os.close(current_fd)
    return count


def _validate_read_document(tool_name: str, data: object) -> None:
    result = _mapping(data)
    document_format = _DOCUMENT_FORMATS[tool_name]
    required = _DOCUMENT_FIELDS[document_format] | {
        "file_name",
        "size_bytes",
        "untrusted_content",
    }
    if result.get("format") != document_format or not required.issubset(result):
        raise _InvalidResult
    size_bytes = result.get("size_bytes")
    if (
        not isinstance(result.get("metadata"), Mapping)
        or not isinstance(result.get("file_name"), str)
        or not isinstance(size_bytes, int)
        or isinstance(size_bytes, bool)
        or size_bytes < 0
        or result.get("untrusted_content") is not True
    ):
        raise _InvalidResult
    if document_format == "pdf":
        page_count = result.get("page_count")
        pages = result.get("pages")
        if (
            not isinstance(page_count, int)
            or isinstance(page_count, bool)
            or page_count < 0
            or not isinstance(pages, list)
            or not all(
                isinstance(page, Mapping)
                and isinstance(page.get("page"), int)
                and not isinstance(page.get("page"), bool)
                and isinstance(page.get("text"), str)
                for page in pages
            )
        ):
            raise _InvalidResult
    elif document_format == "docx":
        paragraphs = result.get("paragraphs")
        tables = result.get("tables")
        if (
            not isinstance(paragraphs, list)
            or not all(isinstance(item, str) for item in paragraphs)
            or not isinstance(tables, list)
            or not all(
                isinstance(table, Mapping) and isinstance(table.get("rows"), list)
                for table in tables
            )
        ):
            raise _InvalidResult
    elif document_format == "csv":
        headers = result.get("headers")
        rows = result.get("rows")
        rows_read = result.get("rows_read")
        if (
            not isinstance(headers, list)
            or not all(isinstance(item, str) for item in headers)
            or not isinstance(rows, list)
            or not all(
                isinstance(row, list)
                and all(
                    isinstance(cell, (str, int, float, bool)) or cell is None
                    for cell in row
                )
                for row in rows
            )
            or not isinstance(rows_read, int)
            or isinstance(rows_read, bool)
            or rows_read != len(rows)
        ):
            raise _InvalidResult
    else:
        sheet_count = result.get("sheet_count")
        sheets = result.get("sheets")
        if (
            not isinstance(sheet_count, int)
            or isinstance(sheet_count, bool)
            or sheet_count < 0
            or not isinstance(sheets, list)
            or not all(
                isinstance(sheet, Mapping)
                and isinstance(sheet.get("name"), str)
                and isinstance(sheet.get("rows"), list)
                for sheet in sheets
            )
        ):
            raise _InvalidResult


def _public_path_for(path: os.PathLike[str] | str, guard: PathGuard) -> str:
    resolved = guard.authorize_path(path, allow_missing=True)
    for index, root in enumerate(guard.allowed_roots):
        if resolved.is_relative_to(root):
            suffix = resolved.relative_to(root).as_posix()
            return f"@root{index}/{suffix}".rstrip("/")
    raise _InvalidResult
