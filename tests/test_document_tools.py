from __future__ import annotations

import asyncio
import io
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from docx import Document
from openpyxl import Workbook
from pypdf import PdfWriter

from agent import create_tool_registry, run_task
import core.document_tools as document_tools
from core.document_tools import read_document
from core.llm_client import (
    ChatMessage,
    LLMResponse,
    ModelCapabilities,
    ToolCall,
    ToolDefinition as LLMToolDefinition,
)
from core.tool_dispatcher import ToolDispatcher, ToolExecutionError
from core.tool_registry import ToolDefinition
from security.path_guard import PathGuard


def _document_bytes(document_format: str) -> bytes:
    output = io.BytesIO()
    if document_format == "pdf":
        objects = [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            (
                b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>"
            ),
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        ]
        stream = b"BT /F1 12 Tf 72 720 Td (Hello PDF) Tj ET"
        objects.append(
            b"<< /Length "
            + str(len(stream)).encode()
            + b" >>\nstream\n"
            + stream
            + b"\nendstream"
        )
        objects.append(b"<< /Title (Sample PDF) >>")
        output.write(b"%PDF-1.4\n")
        offsets = [0]
        for index, content in enumerate(objects, start=1):
            offsets.append(output.tell())
            output.write(f"{index} 0 obj\n".encode())
            output.write(content)
            output.write(b"\nendobj\n")
        xref_offset = output.tell()
        output.write(f"xref\n0 {len(offsets)}\n".encode())
        output.write(b"0000000000 65535 f \n")
        for offset in offsets[1:]:
            output.write(f"{offset:010d} 00000 n \n".encode())
        output.write(
            (
                f"trailer\n<< /Size {len(offsets)} /Root 1 0 R /Info 6 0 R >>\n"
                f"startxref\n{xref_offset}\n%%EOF\n"
            ).encode()
        )
    elif document_format == "docx":
        document = Document()
        document.add_paragraph("Hujjat matni")
        table = document.add_table(rows=1, cols=2)
        table.cell(0, 0).text = "Mahsulot"
        table.cell(0, 1).text = "Narx"
        document.core_properties.title = "Sample DOCX"
        document.save(output)
    elif document_format == "csv":
        output.write("name,value\nolma,3\n".encode())
    elif document_format == "xlsx":
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Jadval"
        sheet.append(["name", "value"])
        sheet.append(["olma", 3])
        workbook.save(output)
    else:
        raise AssertionError(f"Unknown test document format: {document_format}")
    return output.getvalue()


@pytest.mark.parametrize("document_format", ["pdf", "docx", "csv", "xlsx"])
def test_reads_valid_documents_with_metadata_and_bounded_content(
    tmp_path: Path,
    document_format: str,
) -> None:
    path = tmp_path / f"sample.{document_format}"
    path.write_bytes(_document_bytes(document_format))
    guard = PathGuard([tmp_path])
    try:
        result = read_document(path, document_format, guard)
    finally:
        guard.close()

    assert result["format"] == document_format
    assert result["file_name"] == path.name
    assert result["size_bytes"] == path.stat().st_size
    assert result["untrusted_content"] is True
    assert isinstance(result["metadata"], dict)
    if document_format == "pdf":
        assert result["page_count"] == 1
        assert "Hello PDF" in result["pages"][0]["text"]
        assert result["metadata"]["/Title"] == "Sample PDF"
    elif document_format == "docx":
        assert "Hujjat matni" in result["paragraphs"]
        assert result["metadata"]["title"] == "Sample DOCX"
    elif document_format == "csv":
        assert result["headers"] == ["name", "value"]
        assert result["rows"] == [["olma", "3"]]
    else:
        assert result["sheets"][0]["name"] == "Jadval"
        assert result["sheets"][0]["rows"][1] == ["olma", 3]


@pytest.mark.parametrize("document_format", ["pdf", "docx", "csv", "xlsx"])
def test_empty_documents_are_handled_without_leaking_parser_errors(
    tmp_path: Path,
    document_format: str,
) -> None:
    path = tmp_path / f"empty.{document_format}"
    if document_format == "pdf":
        writer = PdfWriter()
        writer.write(path)
    elif document_format == "docx":
        Document().save(path)
    elif document_format == "csv":
        path.write_text("", encoding="utf-8")
    else:
        Workbook().save(path)
    guard = PathGuard([tmp_path])
    try:
        result = read_document(path, document_format, guard)
    finally:
        guard.close()

    assert result["format"] == document_format
    assert result["untrusted_content"] is True


@pytest.mark.parametrize("document_format", ["pdf", "docx", "csv", "xlsx"])
def test_corrupt_documents_return_safe_errors(
    tmp_path: Path,
    document_format: str,
) -> None:
    path = tmp_path / f"bad.{document_format}"
    path.write_bytes(b"\xff")
    guard = PathGuard([tmp_path])
    try:
        with pytest.raises(ToolExecutionError) as error:
            read_document(path, document_format, guard)
    finally:
        guard.close()

    assert error.value.code in {
        "INVALID_DOCUMENT",
        "INVALID_CSV",
        "UNSUPPORTED_ENCODING",
    }
    assert "Traceback" not in error.value.safe_message
    assert str(tmp_path) not in error.value.safe_message


def test_document_size_and_result_limits_are_enforced(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    large_path = tmp_path / "large.csv"
    with large_path.open("wb") as file:
        file.truncate(10 * 1024 * 1024 + 1)
    guard = PathGuard([tmp_path])
    try:
        with pytest.raises(ToolExecutionError) as large:
            read_document(large_path, "csv", guard)
        assert large.value.code == "DOCUMENT_TOO_LARGE"

        result_path = tmp_path / "result.csv"
        result_path.write_text("value\nx\n", encoding="utf-8")
        monkeypatch.setattr(document_tools, "MAX_RESULT_BYTES", 100)
        with pytest.raises(ToolExecutionError) as result:
            read_document(result_path, "csv", guard, row_limit=20)
        assert result.value.code == "DOCUMENT_RESULT_TOO_LARGE"
    finally:
        guard.close()


def test_document_worker_timeout_is_killed_and_reaped(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    path = tmp_path / "timeout.csv"
    path.write_text("value\nx\n", encoding="utf-8")
    guard = PathGuard([tmp_path])

    class TimedOutWorker:
        returncode: int | None = None
        killed = False
        reaped = False

        def communicate(
            self,
            input: bytes | None = None,
            timeout: float | None = None,
        ) -> tuple[bytes, bytes]:
            if timeout is not None:
                raise subprocess.TimeoutExpired("document_worker", timeout)
            self.reaped = True
            return b"", b""

        def kill(self) -> None:
            self.killed = True
            self.returncode = -9

    worker = TimedOutWorker()
    monkeypatch.setattr(document_tools.subprocess, "Popen", lambda *_args, **_kwargs: worker)
    try:
        with pytest.raises(ToolExecutionError) as error:
            read_document(path, "csv", guard)
    finally:
        guard.close()

    assert error.value.code == "DOCUMENT_TIMEOUT"
    assert worker.killed
    assert worker.reaped


def test_document_path_policy_rejects_outside_and_symlink_paths(
    tmp_path: Path,
) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside.csv"
    outside.write_text("x\n1\n", encoding="utf-8")
    linked = allowed / "linked.csv"
    linked.symlink_to(outside)
    guard = PathGuard([allowed])
    try:
        with pytest.raises(ToolExecutionError) as outside_error:
            read_document(outside, "csv", guard)
        assert outside_error.value.code == "OUTSIDE_ALLOWED_ROOT"

        with pytest.raises(ToolExecutionError) as link_error:
            read_document(linked, "csv", guard)
        assert link_error.value.code in {
            "OUTSIDE_ALLOWED_ROOT",
            "SYMLINK_NOT_ALLOWED",
            "INVALID_PATH",
        }
    finally:
        guard.close()


class _AllowAll:
    def is_allowed(
        self,
        tool: ToolDefinition,
        arguments: dict[str, Any],
    ) -> bool:
        return True


class _DocumentLLM:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.responses = [
            LLMResponse(
                content="",
                tool_calls=[ToolCall(name="read_csv", arguments={"path": "@root0/data.csv"})],
                assistant_message=ChatMessage(
                    role="assistant",
                    content="",
                    tool_calls=[ToolCall(name="read_csv", arguments={"path": "@root0/data.csv"})],
                ),
            ),
            LLMResponse(
                content="CSV faylida bitta olma qatori bor.",
                assistant_message=ChatMessage(
                    role="assistant",
                    content="CSV faylida bitta olma qatori bor.",
                ),
            ),
        ]

    def chat(
        self,
        history: Sequence[ChatMessage],
        *,
        system_prompt: str | None = None,
        tools: Sequence[LLMToolDefinition] = (),
    ) -> LLMResponse:
        self.calls.append(
            {"history": list(history), "system_prompt": system_prompt, "tools": tools}
        )
        return self.responses.pop(0)

    def get_capabilities(self) -> ModelCapabilities:
        raise NotImplementedError


def test_document_result_is_returned_to_agent_as_untrusted_for_summarization(
    tmp_path: Path,
    capsys: Any,
) -> None:
    document = tmp_path / "data.csv"
    document.write_text("name,count\nolma,1\n", encoding="utf-8")
    guard = PathGuard([tmp_path])
    registry = create_tool_registry(guard)
    dispatcher = ToolDispatcher(registry, permission_checker=_AllowAll())
    client = _DocumentLLM()
    try:
        run_task(
            "data.csv faylini o'qib, qisqacha ayt",
            client=client,
            registry=registry,
            dispatcher=dispatcher,
            path_guard=guard,
        )
    finally:
        guard.close()

    output = capsys.readouterr().out
    assert "[step] Vosita: read_csv" in output
    assert "Agent: CSV faylida bitta olma qatori bor." in output
    tool_message = client.calls[1]["history"][2]
    assert "UNTRUSTED TOOL DATA" in tool_message.content
    assert "olma" in tool_message.content
    assert "hujjat" in client.calls[0]["system_prompt"].lower()


def test_document_tools_are_registered_as_strict_read_only_tools(
    tmp_path: Path,
) -> None:
    guard = PathGuard([tmp_path])
    try:
        tools = {
            tool.name: tool
            for tool in create_tool_registry(guard).list_tools()
            if tool.name.startswith("read_") and tool.name != "read_text_file"
        }
    finally:
        guard.close()

    assert set(tools) == {"read_pdf", "read_docx", "read_csv", "read_xlsx"}
    for tool in tools.values():
        assert tool.permission_level == "READ_ONLY"
        assert tool.argument_model.model_config.get("extra") == "forbid"


def test_registry_rejects_unknown_document_arguments(tmp_path: Path) -> None:
    guard = PathGuard([tmp_path])
    dispatcher = ToolDispatcher(
        create_tool_registry(guard),
        permission_checker=_AllowAll(),
    )
    try:
        result = asyncio.run(
            dispatcher.dispatch(
                {
                    "name": "read_csv",
                    "arguments": {"path": "data.csv", "execute_macros": True},
                }
            )
        )
    finally:
        guard.close()

    assert result.status == "error"
    assert result.error_code == "INVALID_ARGUMENTS"
