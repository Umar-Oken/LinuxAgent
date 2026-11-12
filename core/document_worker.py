"""Isolated parsers for untrusted, size-bounded document bytes."""

from __future__ import annotations

import csv
import io
import json
import os
import sys
import zipfile
from datetime import date, datetime, time
from typing import Any
from xml.etree import ElementTree

_MAX_INPUT_BYTES = 10 * 1024 * 1024
_MAX_ZIP_ENTRIES = 2000
_MAX_MEMBER_BYTES = 20 * 1024 * 1024
_MAX_UNPACKED_BYTES = 50 * 1024 * 1024
_MAX_COMPRESSION_RATIO = 100
_MAX_COLUMNS = 50
_MAX_CELL_CHARS = 500
_MAX_EXTRACTED_CHARS = 2_000
_MAX_OUTPUT_BYTES = 12 * 1024


class _DocumentFailure(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _validate_ooxml(data: bytes) -> None:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if len(entries) > _MAX_ZIP_ENTRIES:
                raise _DocumentFailure(
                    "DOCUMENT_LIMIT_EXCEEDED",
                    "Hujjat ichidagi qismlar soni belgilangan limitdan oshdi.",
                )
            unpacked = 0
            for entry in entries:
                if entry.file_size > _MAX_MEMBER_BYTES:
                    raise _DocumentFailure(
                        "DOCUMENT_LIMIT_EXCEEDED",
                        "Hujjatning ichki qismi hajm limitidan oshdi.",
                    )
                unpacked += entry.file_size
                if unpacked > _MAX_UNPACKED_BYTES:
                    raise _DocumentFailure(
                        "DOCUMENT_LIMIT_EXCEEDED",
                        "Hujjatning umumiy ochilgan hajmi limitdan oshdi.",
                    )
                if (
                    entry.compress_size
                    and entry.file_size / entry.compress_size > _MAX_COMPRESSION_RATIO
                ):
                    raise _DocumentFailure(
                        "DOCUMENT_LIMIT_EXCEEDED",
                        "Hujjatda haddan tashqari siqilgan qism mavjud.",
                    )
                name = entry.filename.casefold()
                if (
                    "vbaproject.bin" in name
                    or name.startswith("word/embeddings/")
                    or name.startswith("xl/embeddings/")
                    or name.startswith("xl/externallinks/")
                ):
                    raise _DocumentFailure(
                        "UNSUPPORTED_DOCUMENT_CONTENT",
                        "Makroslar, ichki obyektlar va tashqi havolalar bo'lgan hujjatlar qabul qilinmaydi.",
                    )
            for entry in entries:
                if entry.filename.casefold().endswith(".rels"):
                    try:
                        relationships = ElementTree.fromstring(archive.read(entry))
                    except (ElementTree.ParseError, OSError, RuntimeError, zipfile.BadZipFile):
                        raise _DocumentFailure(
                            "INVALID_DOCUMENT",
                            "Hujjatning ichki bog'lanishlari yaroqsiz.",
                        ) from None
                    if any(
                        relation.attrib.get("TargetMode", "").casefold() == "external"
                        for relation in relationships
                    ):
                        raise _DocumentFailure(
                            "UNSUPPORTED_DOCUMENT_CONTENT",
                            "Tashqi resurslarga havolasi bo'lgan hujjatlar qabul qilinmaydi.",
                        )
    except _DocumentFailure:
        raise
    except (OSError, zipfile.BadZipFile, ValueError):
        raise _DocumentFailure(
            "INVALID_DOCUMENT",
            "Hujjat arxivi buzilgan yoki yaroqsiz.",
        ) from None


def _cell(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, (date, datetime, time)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value[:64].hex()
    return str(value)[:_MAX_CELL_CHARS]


def _metadata(properties: Any, names: tuple[str, ...]) -> dict[str, str | None]:
    result: dict[str, str | None] = {}
    for name in names:
        if hasattr(properties, "get"):
            value = properties.get(name)
        else:
            value = getattr(properties, name, None)
        if value is None:
            result[name] = None
        elif isinstance(value, (datetime, date)):
            result[name] = value.isoformat()
        else:
            result[name] = str(value)[:_MAX_CELL_CHARS]
    return result


def _parse_pdf(data: bytes, page_limit: int) -> dict[str, Any]:
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(data), strict=False)
        if reader.is_encrypted:
            raise _DocumentFailure(
                "DOCUMENT_ENCRYPTED",
                "Shifrlangan PDF hujjatlarni o'qish qo'llab-quvvatlanmaydi.",
            )
        page_count = len(reader.pages)
        pages: list[dict[str, Any]] = []
        text_left = _MAX_EXTRACTED_CHARS
        for index, page in enumerate(reader.pages[:page_limit]):
            text = page.extract_text() or ""
            text = text[:text_left]
            text_left -= len(text)
            pages.append({"page": index + 1, "text": text})
            if text_left <= 0:
                break
        metadata = _metadata(
            reader.metadata,
            ("/Title", "/Author", "/Subject", "/Creator", "/Producer"),
        ) if reader.metadata else {}
        return {
            "format": "pdf",
            "metadata": metadata,
            "page_count": page_count,
            "pages": pages,
            "pages_truncated": page_count > len(pages),
            "text_truncated": text_left <= 0,
        }
    except _DocumentFailure:
        raise
    except Exception:
        raise _DocumentFailure(
            "INVALID_DOCUMENT",
            "PDF hujjat buzilgan yoki matnini ajratib bo'lmadi.",
        ) from None


def _parse_docx(data: bytes, paragraph_limit: int) -> dict[str, Any]:
    _validate_ooxml(data)
    from docx import Document

    try:
        document = Document(io.BytesIO(data))
        paragraphs = [
            paragraph.text[:_MAX_CELL_CHARS]
            for paragraph in document.paragraphs[:paragraph_limit]
        ]
        tables: list[dict[str, Any]] = []
        rows_left = paragraph_limit
        text_left = _MAX_EXTRACTED_CHARS - sum(map(len, paragraphs))
        for table in document.tables:
            if rows_left <= 0 or text_left <= 0:
                break
            rows: list[list[Any]] = []
            for row in table.rows[:rows_left]:
                values = [
                    cell.text[:_MAX_CELL_CHARS]
                    for cell in row.cells[:_MAX_COLUMNS]
                ]
                text_left -= sum(map(len, values))
                rows.append(values)
                rows_left -= 1
                if rows_left <= 0 or text_left <= 0:
                    break
            tables.append({"rows": rows})
        properties = document.core_properties
        return {
            "format": "docx",
            "metadata": _metadata(
                properties,
                ("title", "subject", "author", "created", "modified"),
            ),
            "paragraphs": paragraphs,
            "tables": tables,
            "content_truncated": (
                len(document.paragraphs) > paragraph_limit
                or any(len(table.rows) > paragraph_limit for table in document.tables)
                or text_left <= 0
            ),
        }
    except _DocumentFailure:
        raise
    except Exception:
        raise _DocumentFailure(
            "INVALID_DOCUMENT",
            "DOCX hujjat buzilgan yoki matnini ajratib bo'lmadi.",
        ) from None


def _parse_csv(data: bytes, row_limit: int) -> dict[str, Any]:
    try:
        text = data.decode("utf-8-sig")
        csv.field_size_limit(65_536)
        reader = csv.reader(io.StringIO(text, newline=""), strict=True)
        headers = next(reader, [])
        headers = [str(value)[:_MAX_CELL_CHARS] for value in headers[:_MAX_COLUMNS]]
        rows: list[list[Any]] = []
        text_left = _MAX_EXTRACTED_CHARS
        for row in reader:
            values = [str(value)[:_MAX_CELL_CHARS] for value in row[:_MAX_COLUMNS]]
            text_left -= sum(map(len, values))
            rows.append(values)
            if len(rows) >= row_limit or text_left <= 0:
                break
        return {
            "format": "csv",
            "metadata": {"encoding": "utf-8"},
            "headers": headers,
            "rows": rows,
            "rows_read": len(rows),
            "rows_truncated": len(rows) >= row_limit or text_left <= 0,
            "content_truncated": text_left <= 0,
        }
    except UnicodeDecodeError:
        raise _DocumentFailure(
            "UNSUPPORTED_ENCODING",
            "CSV fayli UTF-8 kodlashida bo'lishi kerak.",
        ) from None
    except (csv.Error, ValueError):
        raise _DocumentFailure(
            "INVALID_DOCUMENT",
            "CSV fayli buzilgan yoki noto'g'ri formatlangan.",
        ) from None


def _parse_xlsx(data: bytes, row_limit: int, sheet_limit: int) -> dict[str, Any]:
    _validate_ooxml(data)
    from openpyxl import load_workbook

    workbook = None
    try:
        workbook = load_workbook(
            io.BytesIO(data),
            read_only=True,
            data_only=True,
            keep_links=False,
        )
        sheets: list[dict[str, Any]] = []
        text_left = _MAX_EXTRACTED_CHARS
        for sheet in workbook.worksheets[:sheet_limit]:
            rows: list[list[Any]] = []
            for row in sheet.iter_rows(
                max_row=row_limit,
                max_col=_MAX_COLUMNS,
                values_only=True,
            ):
                values = [_cell(value) for value in row]
                while values and values[-1] is None:
                    values.pop()
                text_left -= sum(len(value) if isinstance(value, str) else 0 for value in values)
                rows.append(values)
                if text_left <= 0:
                    break
            sheets.append(
                {
                    "name": sheet.title[:_MAX_CELL_CHARS],
                    "rows": rows,
                    "rows_read": len(rows),
                    "rows_truncated": sheet.max_row > len(rows),
                }
            )
            if text_left <= 0:
                break
        result = {
            "format": "xlsx",
            "metadata": _metadata(
                workbook.properties,
                ("title", "subject", "creator", "created", "modified"),
            ),
            "sheet_count": len(workbook.worksheets),
            "sheets": sheets,
            "sheets_truncated": len(workbook.worksheets) > len(sheets),
            "content_truncated": text_left <= 0,
        }
        return result
    except _DocumentFailure:
        raise
    except Exception:
        raise _DocumentFailure(
            "INVALID_DOCUMENT",
            "XLSX hujjat buzilgan yoki jadval ma'lumotini ajratib bo'lmadi.",
        ) from None
    finally:
        if workbook is not None:
            workbook.close()


def _disable_network() -> None:
    import socket

    def deny(*_args: object, **_kwargs: object) -> None:
        raise OSError("Network access is disabled in the document parser.")

    socket.socket = deny  # type: ignore[assignment]
    socket.create_connection = deny  # type: ignore[assignment]
    socket.getaddrinfo = deny  # type: ignore[assignment]


def main() -> int:
    import resource

    resource.setrlimit(resource.RLIMIT_CPU, (6, 7))
    resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))

    if len(sys.argv) != 4:
        return 2
    document_format = sys.argv[1]
    try:
        first_limit = int(sys.argv[2])
        sheet_limit = int(sys.argv[3])
        data = sys.stdin.buffer.read(_MAX_INPUT_BYTES + 1)
        if len(data) > _MAX_INPUT_BYTES:
            raise _DocumentFailure(
                "DOCUMENT_TOO_LARGE",
                "Hujjat belgilangan 10 MiB hajm limitidan katta.",
            )
        _disable_network()
        if document_format == "pdf":
            result = _parse_pdf(data, first_limit)
        elif document_format == "docx":
            result = _parse_docx(data, first_limit)
        elif document_format == "csv":
            result = _parse_csv(data, first_limit)
        elif document_format == "xlsx":
            result = _parse_xlsx(data, first_limit, sheet_limit)
        else:
            raise _DocumentFailure(
                "UNSUPPORTED_DOCUMENT",
                "Hujjat formati qo'llab-quvvatlanmaydi.",
            )
        payload = {"ok": True, "data": result}
    except _DocumentFailure as error:
        payload = {
            "ok": False,
            "error_code": error.code,
            "safe_error_message": error.message,
        }
    except Exception:
        payload = {
            "ok": False,
            "error_code": "INVALID_DOCUMENT",
            "safe_error_message": "Hujjat buzilgan yoki qo'llab-quvvatlanmaydigan formatda.",
        }

    encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(encoded) > _MAX_OUTPUT_BYTES:
        encoded = json.dumps(
            {
                "ok": False,
                "error_code": "DOCUMENT_RESULT_TOO_LARGE",
                "safe_error_message": "Ajratilgan hujjat natijasi belgilangan hajm limitidan oshdi.",
            },
            ensure_ascii=False,
        ).encode("utf-8")
    sys.stdout.buffer.write(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
