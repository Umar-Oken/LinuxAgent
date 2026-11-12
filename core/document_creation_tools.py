"""Atomic, guarded creation of plain-text and tabular documents."""

from __future__ import annotations

import csv
import io
import math
import os
import re
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from core.tool_dispatcher import ToolExecutionError
from security.path_guard import PathAccessError, PathGuard

CreationFormat = Literal["markdown", "docx", "csv", "xlsx"]
MAX_CREATED_DOCUMENT_BYTES = 1024 * 1024
MAX_CELL_CHARS = 10_000
MAX_ROWS = 10_000
MAX_COLUMNS = 100
MAX_SHEETS = 20
_FORMULA_PREFIX = re.compile(r"^[\s\x00-\x1f]*[=+\-@]")
_SUFFIXES: dict[CreationFormat, frozenset[str]] = {
    "markdown": frozenset({".md", ".markdown"}),
    "docx": frozenset({".docx"}),
    "csv": frozenset({".csv"}),
    "xlsx": frozenset({".xlsx"}),
}


def create_document(
    path: str | Path,
    document_format: CreationFormat,
    content: dict[str, Any],
    path_guard: PathGuard,
) -> dict[str, object]:
    """Create and verify a document without following links or overwriting files."""
    if document_format not in {"markdown", "docx", "csv", "xlsx"}:
        raise ToolExecutionError("UNSUPPORTED_DOCUMENT", "Hujjat formati qo'llab-quvvatlanmaydi.")
    if not isinstance(content, dict):
        raise ToolExecutionError("INVALID_DOCUMENT_CONTENT", "Hujjat mazmuni noto'g'ri formatda.")
    target = Path(path).expanduser()
    if not target.is_absolute():
        target = Path.cwd() / target
    if target.suffix.casefold() not in _SUFFIXES[document_format]:
        raise ToolExecutionError(
            "INVALID_DESTINATION",
            "Fayl kengaytmasi tanlangan hujjat formatiga mos emas.",
        )
    if target.is_symlink():
        raise ToolExecutionError(
            "SYMLINK_NOT_ALLOWED",
            "Hujjat manzilida symlink ishlatishga ruxsat yo'q.",
        )
    try:
        payload = _serialize_document(document_format, content)
    except ToolExecutionError:
        raise
    except (TypeError, ValueError, UnicodeError):
        raise ToolExecutionError(
            "INVALID_DOCUMENT_CONTENT",
            "Hujjat mazmunini xavfsiz tayyorlab bo'lmadi.",
        ) from None
    if not payload:
        raise ToolExecutionError(
            "INVALID_DOCUMENT_CONTENT",
            "Bo'sh hujjat yaratish uchun mazmun kiriting.",
        )
    if len(payload) > MAX_CREATED_DOCUMENT_BYTES:
        raise ToolExecutionError(
            "DOCUMENT_TOO_LARGE",
            "Yaratiladigan hujjat 1 MiB hajm limitidan oshdi.",
        )

    try:
        canonical_target = path_guard.authorize_path(
            target,
            operation="write",
            allow_missing=True,
        )
        if Path(os.path.abspath(target)) != canonical_target:
            raise ToolExecutionError(
                "SYMLINK_NOT_ALLOWED",
                "Hujjat manzilida symlink ishlatishga ruxsat yo'q.",
            )
        parent_fd = path_guard.open_directory(canonical_target.parent)
    except PathAccessError as exc:
        raise ToolExecutionError(exc.code, exc.safe_message) from None

    temporary_name = f".agent-document-{uuid4().hex}.tmp"
    temporary_fd = -1
    created_identity: tuple[int, int] | None = None
    published = False
    keep_published = False
    try:
        try:
            temporary_fd = os.open(
                temporary_name,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600,
                dir_fd=parent_fd,
            )
            temporary_stat = os.fstat(temporary_fd)
            created_identity = (temporary_stat.st_dev, temporary_stat.st_ino)
            _write_all(temporary_fd, payload)
            os.fsync(temporary_fd)
            _verify_fd(temporary_fd, payload)
            os.close(temporary_fd)
            temporary_fd = -1

            try:
                os.stat(canonical_target.name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise ToolExecutionError(
                    "DESTINATION_EXISTS",
                    "Manzil allaqachon mavjud; mavjud fayllar ustidan yozilmaydi.",
                )

            os.link(
                temporary_name,
                canonical_target.name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
                follow_symlinks=False,
            )
            published = True
            os.unlink(temporary_name, dir_fd=parent_fd)
            os.fsync(parent_fd)

            result_fd = os.open(
                canonical_target.name,
                os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=parent_fd,
            )
            try:
                result_stat = os.fstat(result_fd)
                if created_identity != (result_stat.st_dev, result_stat.st_ino):
                    raise ToolExecutionError(
                        "DOCUMENT_VERIFY_FAILED",
                        "Yaratilgan hujjatni xavfsiz tekshirib bo'lmadi.",
                    )
                _verify_fd(result_fd, payload)
                _verify_document_bytes(document_format, payload)
            finally:
                os.close(result_fd)
            keep_published = True
            return {
                "operation": f"create_{document_format}_document",
                "status": "success",
                "path": path_guard.public_path(canonical_target),
                "size_bytes": len(payload),
                "verified": True,
            }
        except FileExistsError:
            raise ToolExecutionError(
                "DESTINATION_EXISTS",
                "Manzil allaqachon mavjud; mavjud fayllar ustidan yozilmaydi.",
            ) from None
        except ToolExecutionError:
            raise
        except OSError as exc:
            if isinstance(exc, PathAccessError):
                raise ToolExecutionError(exc.code, exc.safe_message) from None
            raise ToolExecutionError(
                "DOCUMENT_CREATE_FAILED",
                "Hujjatni xavfsiz yaratib yoki tekshirib bo'lmadi.",
            ) from None
        except (TypeError, ValueError, KeyError):
            raise ToolExecutionError(
                "DOCUMENT_CREATE_FAILED",
                "Hujjatni xavfsiz yaratib yoki tekshirib bo'lmadi.",
            ) from None
    finally:
        if temporary_fd >= 0:
            os.close(temporary_fd)
        cleanup_failed = False
        try:
            os.unlink(temporary_name, dir_fd=parent_fd)
        except FileNotFoundError:
            pass
        except OSError:
            cleanup_failed = True
        if published and not keep_published and created_identity is not None:
            try:
                current = os.stat(
                    canonical_target.name,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
                if (current.st_dev, current.st_ino) == created_identity:
                    os.unlink(canonical_target.name, dir_fd=parent_fd)
                    os.fsync(parent_fd)
                else:
                    cleanup_failed = True
            except FileNotFoundError:
                pass
            except OSError:
                cleanup_failed = True
        os.close(parent_fd)
        if cleanup_failed:
            raise ToolExecutionError(
                "DOCUMENT_CLEANUP_FAILED",
                "Vaqtinchalik faylni tozalab bo'lmadi; manzil katalogini tekshiring.",
            ) from None


def _serialize_document(document_format: CreationFormat, content: dict[str, Any]) -> bytes:
    if document_format == "markdown":
        title = content.get("title")
        body = content.get("content")
        if title is not None and (not isinstance(title, str) or len(title) > 300):
            raise _invalid_content()
        if not isinstance(body, str):
            raise _invalid_content()
        if len(body.encode("utf-8")) > MAX_CREATED_DOCUMENT_BYTES:
            raise ToolExecutionError(
                "DOCUMENT_TOO_LARGE",
                "Yaratiladigan hujjat 1 MiB hajm limitidan oshdi.",
            )
        text = f"# {title}\n\n{body}" if title else body
        return text.encode("utf-8")

    if document_format == "docx":
        from docx import Document

        paragraphs = content.get("paragraphs")
        tables = content.get("tables", [])
        title = content.get("title", "")
        if (
            not _string_list(paragraphs, maximum=1000)
            or not isinstance(tables, list)
            or len(tables) > MAX_ROWS
            or not isinstance(title, str)
            or len(title) > MAX_CELL_CHARS
        ):
            raise _invalid_content()
        assert isinstance(paragraphs, list)
        assert isinstance(tables, list)
        if not paragraphs and not tables:
            raise _invalid_content()
        document = Document()
        document.core_properties.title = title
        for paragraph in paragraphs:
            document.add_paragraph(paragraph)
        for table_data in tables:
            if (
                not isinstance(table_data, list)
                or not table_data
                or len(table_data) > MAX_ROWS
                or not all(_string_list(row, maximum=MAX_COLUMNS) for row in table_data)
            ):
                raise _invalid_content()
            width = len(table_data[0])
            if width < 1 or any(len(row) != width for row in table_data):
                raise _invalid_content()
            table = document.add_table(rows=0, cols=width)
            for row_data in table_data:
                cells = table.add_row().cells
                for cell, value in zip(cells, row_data, strict=True):
                    cell.text = value
        output = io.BytesIO()
        document.save(output)
        return output.getvalue()

    if document_format == "csv":
        headers = content.get("headers")
        rows = content.get("rows")
        if (
            not _string_list(headers, maximum=MAX_COLUMNS)
            or not isinstance(rows, list)
            or len(rows) > MAX_ROWS
        ):
            raise _invalid_content()
        assert isinstance(headers, list)
        assert isinstance(rows, list)
        if any(
            not _string_list(row, maximum=MAX_COLUMNS)
            or len(row) != len(headers)
            for row in rows
        ):
            raise _invalid_content()
        csv_output = io.StringIO(newline="")
        writer = csv.writer(csv_output, lineterminator="\n")
        if headers:
            writer.writerow([_spreadsheet_safe(value) for value in headers])
            writer.writerows(
                [[_spreadsheet_safe(value) for value in row] for row in rows]
            )
        elif rows:
            raise _invalid_content()
        return csv_output.getvalue().encode("utf-8")

    from openpyxl import Workbook, load_workbook

    sheets = content.get("sheets")
    if not isinstance(sheets, list) or not 1 <= len(sheets) <= MAX_SHEETS:
        raise _invalid_content()
    assert isinstance(sheets, list)
    names: set[str] = set()
    workbook = Workbook()
    workbook.remove(workbook.active)
    try:
        for sheet_data in sheets:
            if not isinstance(sheet_data, dict):
                raise _invalid_content()
            name, rows = sheet_data.get("name"), sheet_data.get("rows")
            if (
                not isinstance(name, str)
                or not name
                or len(name) > 31
                or any(char in name for char in "[]:*?/\\")
                or name in names
                or not isinstance(rows, list)
                or len(rows) > MAX_ROWS
            ):
                raise _invalid_content()
            names.add(name)
            sheet = workbook.create_sheet(name)
            for row in rows:
                if not isinstance(row, list) or len(row) > MAX_COLUMNS:
                    raise _invalid_content()
                for value in row:
                    if not _valid_cell(value):
                        raise _invalid_content()
                sheet.append(
                    [
                        _spreadsheet_safe(value) if isinstance(value, str) else value
                        for value in row
                    ]
                )
        output = io.BytesIO()
        workbook.save(output)
        payload = output.getvalue()
    finally:
        workbook.close()
    if len(payload) > MAX_CREATED_DOCUMENT_BYTES:
        raise ToolExecutionError(
            "DOCUMENT_TOO_LARGE",
            "Yaratiladigan hujjat 1 MiB hajm limitidan oshdi.",
        )
    check = load_workbook(io.BytesIO(payload), read_only=True, data_only=False, keep_links=False)
    try:
        if check.sheetnames != [sheet["name"] for sheet in sheets]:
            raise _invalid_content()
    finally:
        check.close()
    return payload


def _verify_document_bytes(document_format: CreationFormat, payload: bytes) -> None:
    if document_format == "markdown":
        payload.decode("utf-8")
    elif document_format == "csv":
        list(csv.reader(io.StringIO(payload.decode("utf-8"), newline=""), strict=True))
    elif document_format == "docx":
        from docx import Document

        Document(io.BytesIO(payload))
    else:
        from openpyxl import load_workbook

        workbook = load_workbook(io.BytesIO(payload), read_only=True, keep_links=False)
        workbook.close()


def _verify_fd(fd: int, expected: bytes) -> None:
    details = os.fstat(fd)
    if details.st_size != len(expected) or details.st_size > MAX_CREATED_DOCUMENT_BYTES:
        raise ToolExecutionError(
            "DOCUMENT_VERIFY_FAILED",
            "Yaratilgan hujjat hajmi tekshiruvdan o'tmadi.",
        )
    os.lseek(fd, 0, os.SEEK_SET)
    actual = bytearray()
    while len(actual) <= MAX_CREATED_DOCUMENT_BYTES:
        chunk = os.read(fd, min(64 * 1024, MAX_CREATED_DOCUMENT_BYTES + 1 - len(actual)))
        if not chunk:
            break
        actual.extend(chunk)
    if bytes(actual) != expected:
        raise ToolExecutionError(
            "DOCUMENT_VERIFY_FAILED",
            "Yaratilgan hujjat mazmuni tekshiruvdan o'tmadi.",
        )


def _write_all(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short write")
        view = view[written:]


def _string_list(value: object, *, maximum: int) -> bool:
    return (
        isinstance(value, list)
        and len(value) <= maximum
        and all(isinstance(item, str) and len(item) <= MAX_CELL_CHARS for item in value)
    )


def _valid_cell(value: object) -> bool:
    if value is None or isinstance(value, (str, bool, int)):
        return not isinstance(value, str) or len(value) <= MAX_CELL_CHARS
    if isinstance(value, float):
        return math.isfinite(value)
    return False


def _spreadsheet_safe(value: str) -> str:
    return f"'{value}" if _FORMULA_PREFIX.match(value) else value


def _invalid_content() -> ToolExecutionError:
    return ToolExecutionError(
        "INVALID_DOCUMENT_CONTENT",
        "Hujjat mazmuni strukturaviy talabga mos emas.",
    )
