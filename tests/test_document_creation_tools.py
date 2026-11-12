from __future__ import annotations

import asyncio
import csv
import zipfile
from pathlib import Path
from typing import Any

import pytest
from docx import Document
from openpyxl import load_workbook

from agent import create_tool_registry
from core.document_creation_tools import create_document
from core.tool_dispatcher import ToolDispatcher, ToolExecutionError
from core.tool_registry import ToolDefinition
from security.approval import (
    ApprovalManager,
    ApprovalRequest,
    ApprovalResponse,
    TrustedApprovalInterface,
)
from security.path_guard import PathGuard
from security.permissions import PermissionEngine, PermissionPolicy


class _ApprovalInterface(TrustedApprovalInterface):
    def __init__(self, decision: str = "approve") -> None:
        self.decision = decision
        self.requests: list[ApprovalRequest] = []

    def request_approval(self, request: ApprovalRequest) -> ApprovalResponse:
        self.requests.append(request)
        return ApprovalResponse(
            approval_id=request.approval_id,
            decision=self.decision,  # type: ignore[arg-type]
        )


class _Allow:
    def is_allowed(
        self,
        tool: ToolDefinition,
        arguments: dict[str, Any],
    ) -> bool:
        return True


def _dispatch(
    dispatcher: ToolDispatcher,
    name: str,
    arguments: dict[str, Any],
) -> Any:
    return asyncio.run(dispatcher.dispatch({"name": name, "arguments": arguments}))


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        (
            "create_markdown_document",
            {"path": "report.md", "title": "Hisobot", "content": "Matn"},
        ),
        (
            "create_docx_document",
            {
                "path": "report.docx",
                "title": "Hisobot",
                "paragraphs": ["Birinchi qator", "Ikkinchi qator"],
                "tables": [[["nom", "son"], ["olma", "3"]]],
            },
        ),
        (
            "create_csv_document",
            {
                "path": "report.csv",
                "headers": ["nom", "qiymat"],
                "rows": [["olma", "3"], ["nok", "2"]],
            },
        ),
        (
            "create_xlsx_document",
            {
                "path": "report.xlsx",
                "sheets": [
                    {
                        "name": "Jadval",
                        "rows": [["nom", "son"], ["olma", 3], ["nok", 2]],
                    }
                ],
            },
        ),
    ],
)
def test_dispatch_creates_and_verifies_each_document_after_approval(
    tmp_path: Path,
    tool_name: str,
    arguments: dict[str, Any],
) -> None:
    guard = PathGuard([tmp_path])
    approval = _ApprovalInterface()
    dispatcher = ToolDispatcher(
        create_tool_registry(guard),
        permission_checker=PermissionEngine(
            PermissionPolicy(),
            ApprovalManager(approval),
        ),
    )
    arguments["path"] = f"@root0/{arguments['path']}"
    try:
        result = _dispatch(dispatcher, tool_name, arguments)
    finally:
        guard.close()

    assert result.status == "success"
    assert result.result_data["verified"] is True
    assert result.result_data["size_bytes"] > 0
    assert len(approval.requests) == 1
    assert approval.requests[0].target_resource == arguments["path"]
    assert (tmp_path / Path(arguments["path"]).name).is_file()
    assert (tmp_path / Path(arguments["path"]).name).stat().st_mode & 0o777 == 0o600


def test_created_formats_have_expected_structure_and_no_active_content(
    tmp_path: Path,
) -> None:
    guard = PathGuard([tmp_path])
    try:
        csv_result = create_document(
            tmp_path / "safe.csv",
            "csv",
            {"headers": ["value"], "rows": [["=1+1"], ["  @SUM(A1)"]]},
            guard,
        )
        xlsx_result = create_document(
            tmp_path / "safe.xlsx",
            "xlsx",
            {
                "sheets": [
                    {
                        "name": "Sheet",
                        "rows": [["=1+1", "-cmd"], ["  @SUM(A1)", 12]],
                    }
                ]
            },
            guard,
        )
    finally:
        guard.close()

    assert csv_result["verified"] and xlsx_result["verified"]
    with (tmp_path / "safe.csv").open(encoding="utf-8", newline="") as file:
        assert list(csv.reader(file)) == [
            ["value"],
            ["'=1+1"],
            ["'  @SUM(A1)"],
        ]
    workbook = load_workbook(tmp_path / "safe.xlsx", data_only=False)
    try:
        sheet = workbook["Sheet"]
        assert sheet["A1"].value == "'=1+1"
        assert sheet["A1"].data_type == "s"
        assert sheet["B1"].value == "'-cmd"
        assert sheet["A2"].value == "'  @SUM(A1)"
        assert sheet["B2"].value == 12
    finally:
        workbook.close()
def test_docx_has_plain_paragraphs_and_tables(tmp_path: Path) -> None:
    guard = PathGuard([tmp_path])
    try:
        result = create_document(
            tmp_path / "plain.docx",
            "docx",
            {
                "title": "Plain document",
                "paragraphs": ["Hello"],
                "tables": [[["a", "b"], ["1", "2"]]],
            },
            guard,
        )
    finally:
        guard.close()

    document = Document(tmp_path / "plain.docx")
    assert result["verified"] is True
    assert document.core_properties.title == "Plain document"
    assert document.paragraphs[0].text == "Hello"
    assert document.tables[0].cell(1, 1).text == "2"
    with zipfile.ZipFile(tmp_path / "plain.docx") as archive:
        assert not any(name.casefold().endswith("vbaproject.bin") for name in archive.namelist())


def test_existing_destination_is_never_overwritten(tmp_path: Path) -> None:
    destination = tmp_path / "existing.md"
    destination.write_text("original", encoding="utf-8")
    guard = PathGuard([tmp_path])
    try:
        with pytest.raises(ToolExecutionError) as error:
            create_document(
                destination,
                "markdown",
                {"content": "replacement"},
                guard,
            )
    finally:
        guard.close()

    assert error.value.code == "DESTINATION_EXISTS"
    assert destination.read_text(encoding="utf-8") == "original"
    assert not list(tmp_path.glob(".agent-document-*.tmp"))


@pytest.mark.parametrize(
    ("path", "document_format", "content", "error_code"),
    [
        ("bad.txt", "markdown", {"content": "text"}, "INVALID_DESTINATION"),
        ("bad.csv", "csv", {"headers": ["a"], "rows": [["1", "2"]]}, "INVALID_DOCUMENT_CONTENT"),
        ("bad.docx", "docx", {"paragraphs": [None]}, "INVALID_DOCUMENT_CONTENT"),
        ("empty.docx", "docx", {"paragraphs": [], "tables": []}, "INVALID_DOCUMENT_CONTENT"),
        ("bad.xlsx", "xlsx", {"sheets": [{"name": "Bad/Name", "rows": []}]}, "INVALID_DOCUMENT_CONTENT"),
        ("bad.md", "markdown", {"content": None}, "INVALID_DOCUMENT_CONTENT"),
    ],
)
def test_invalid_destination_or_content_is_rejected(
    tmp_path: Path,
    path: str,
    document_format: str,
    content: dict[str, Any],
    error_code: str,
) -> None:
    guard = PathGuard([tmp_path])
    try:
        result_path = tmp_path / path
        with pytest.raises(ToolExecutionError) as error:
            create_document(result_path, document_format, content, guard)  # type: ignore[arg-type]
    finally:
        guard.close()

    assert error.value.code == error_code
    assert not result_path.exists()


def test_rejects_outside_and_symlink_destinations(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside.md"
    linked = allowed / "linked.md"
    linked.symlink_to(outside)
    guard = PathGuard([allowed])
    try:
        for path, code in (
            (outside, "OUTSIDE_ALLOWED_ROOT"),
            (linked, "SYMLINK_NOT_ALLOWED"),
        ):
            with pytest.raises(ToolExecutionError) as error:
                create_document(path, "markdown", {"content": "text"}, guard)
            assert error.value.code == code
    finally:
        guard.close()
    assert not outside.exists()


def test_approval_denial_prevents_any_file_creation(tmp_path: Path) -> None:
    guard = PathGuard([tmp_path])
    approval = _ApprovalInterface("deny")
    dispatcher = ToolDispatcher(
        create_tool_registry(guard),
        permission_checker=PermissionEngine(
            PermissionPolicy(),
            ApprovalManager(approval),
        ),
    )
    try:
        result = _dispatch(
            dispatcher,
            "create_markdown_document",
            {"path": "@root0/rejected.md", "content": "no"},
        )
    finally:
        guard.close()

    assert result.status == "denied"
    assert not (tmp_path / "rejected.md").exists()
    assert len(approval.requests) == 1


def test_creation_registry_is_typed_controlled_and_rejects_unknown_fields(
    tmp_path: Path,
) -> None:
    guard = PathGuard([tmp_path])
    dispatcher = ToolDispatcher(create_tool_registry(guard), permission_checker=_Allow())
    try:
        definitions = {
            tool.name: tool
            for tool in create_tool_registry(guard).list_tools()
            if tool.name.startswith("create_") and tool.name.endswith("_document")
        }
        assert set(definitions) == {
            "create_markdown_document",
            "create_docx_document",
            "create_csv_document",
            "create_xlsx_document",
        }
        assert all(tool.permission_level == "CONTROLLED" for tool in definitions.values())
        assert all(tool.argument_model.model_config.get("extra") == "forbid" for tool in definitions.values())
        result = _dispatch(
            dispatcher,
            "create_csv_document",
            {
                "path": "@root0/invalid.csv",
                "headers": ["value"],
                "rows": [["ok"]],
                "run_script": True,
            },
        )
    finally:
        guard.close()
    assert result.status == "error"
    assert result.error_code == "INVALID_ARGUMENTS"
    assert not (tmp_path / "invalid.csv").exists()


def test_large_payload_is_limited_before_publishing(tmp_path: Path) -> None:
    guard = PathGuard([tmp_path])
    try:
        with pytest.raises(ToolExecutionError) as error:
            create_document(
                tmp_path / "large.md",
                "markdown",
                {"content": "x" * (1024 * 1024 + 1)},
                guard,
            )
    finally:
        guard.close()

    assert error.value.code == "DOCUMENT_TOO_LARGE"
    assert not (tmp_path / "large.md").exists()
    assert not list(tmp_path.glob(".agent-document-*.tmp"))
