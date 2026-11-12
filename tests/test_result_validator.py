"""Tool natijasi va takroriy bajarilish himoyasi sinovlari."""

import zipfile
from pathlib import Path

from core.result_validator import ToolResultValidator
from core.tool_dispatcher import ToolResult
from security.path_guard import PathGuard


def _result(data: object, tool_name: str) -> ToolResult:
    return ToolResult(
        tool_name=tool_name,
        status="success",
        result_data=data,
        execution_duration=0.01,
    )


def test_validates_file_copy_using_real_source_and_destination(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    destination = tmp_path / "destination.txt"
    source.write_text("ma'lumot")
    destination.write_text("ma'lumot")
    guard = PathGuard([tmp_path])
    validator = ToolResultValidator(guard)
    validator.start_task()
    try:
        allowed, _ = validator.begin_execution(
            "task-1",
            "copy_file",
            {"source": str(source), "destination": str(destination)},
        )
        assert allowed
        outcome = validator.validate(
            "task-1",
            "copy_file",
            {"source": str(source), "destination": str(destination)},
            _result(
                {
                    "operation": "copy_file",
                    "status": "success",
                    "source": "@root0/source.txt",
                    "destination": "@root0/destination.txt",
                    "size_bytes": source.stat().st_size,
                },
                "copy_file",
            ),
            permission_level="CONTROLLED",
        )
        assert outcome.status == "valid"
        assert len(outcome.idempotency_key) == 64
        assert outcome.retry == "do_not_retry"
    finally:
        guard.close()


def test_missing_created_file_invalidates_success_result(tmp_path: Path) -> None:
    guard = PathGuard([tmp_path])
    validator = ToolResultValidator(guard)
    validator.start_task()
    try:
        outcome = validator.validate(
            "task-1",
            "create_markdown_document",
            {"path": str(tmp_path / "missing.md")},
            _result(
                {
                    "status": "success",
                    "verified": True,
                    "size_bytes": 4,
                },
                "create_markdown_document",
            ),
            permission_level="CONTROLLED",
        )
        assert outcome.status == "invalid"
    finally:
        guard.close()


def test_validates_created_document_and_reported_size(tmp_path: Path) -> None:
    path = tmp_path / "created.md"
    content = "# Sarlavha\n"
    path.write_text(content)
    guard = PathGuard([tmp_path])
    validator = ToolResultValidator(guard)
    try:
        result = validator.validate(
            "task-1",
            "create_markdown_document",
            {"path": str(path)},
            _result(
                {
                    "operation": "create_markdown_document",
                    "status": "success",
                    "path": "@root0/created.md",
                    "size_bytes": path.stat().st_size,
                    "verified": True,
                },
                "create_markdown_document",
            ),
            permission_level="CONTROLLED",
        )
        assert result.status == "valid"
        invalid = validator.validate(
            "task-1",
            "create_markdown_document",
            {"path": str(path)},
            _result(
                {
                    "operation": "create_markdown_document",
                    "status": "success",
                    "path": "@root0/created.md",
                    "size_bytes": 1,
                    "verified": True,
                },
                "create_markdown_document",
            ),
            permission_level="CONTROLLED",
        )
        assert invalid.status == "invalid"
    finally:
        guard.close()


def test_validates_created_archive_contents(tmp_path: Path) -> None:
    archive_path = tmp_path / "bundle.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("a.txt", "a")
    guard = PathGuard([tmp_path])
    validator = ToolResultValidator(guard)
    try:
        outcome = validator.validate(
            "task-1",
            "create_archive",
            {"archive_path": str(archive_path)},
            _result(
                {
                    "operation": "create_archive",
                    "status": "success",
                    "archive": "@root0/bundle.zip",
                    "files": 1,
                    "source_bytes": 1,
                },
                "create_archive",
            ),
            permission_level="CONTROLLED",
        )
        assert outcome.status == "valid"

        invalid = validator.validate(
            "task-1",
            "create_archive",
            {"archive_path": str(archive_path)},
            _result(
                {
                    "operation": "create_archive",
                    "status": "success",
                    "archive": "@root0/bundle.zip",
                    "files": 2,
                    "source_bytes": 1,
                },
                "create_archive",
            ),
            permission_level="CONTROLLED",
        )
        assert invalid.status == "invalid"
    finally:
        guard.close()


def test_validates_extracted_archive_files(tmp_path: Path) -> None:
    destination = tmp_path / "extracted"
    destination.mkdir()
    (destination / "file.txt").write_text("archive data")
    guard = PathGuard([tmp_path])
    validator = ToolResultValidator(guard)
    try:
        valid = validator.validate(
            "task-1",
            "extract_archive",
            {"destination_directory": str(destination)},
            _result(
                {
                    "operation": "extract_archive",
                    "status": "success",
                    "destination": "@root0/extracted",
                    "files": 1,
                },
                "extract_archive",
            ),
            permission_level="CONTROLLED",
        )
        assert valid.status == "valid"
        incomplete = validator.validate(
            "task-1",
            "extract_archive",
            {"destination_directory": str(destination)},
            _result(
                {
                    "operation": "extract_archive",
                    "status": "success",
                    "destination": "@root0/extracted",
                    "files": 2,
                },
                "extract_archive",
            ),
            permission_level="CONTROLLED",
        )
        assert incomplete.status == "invalid"
    finally:
        guard.close()


def test_checks_expected_document_structure(tmp_path: Path) -> None:
    path = tmp_path / "table.csv"
    path.write_text("name\nali\n")
    guard = PathGuard([tmp_path])
    validator = ToolResultValidator(guard)
    try:
        valid = validator.validate(
            "task-1",
            "read_csv",
            {"path": str(path)},
            _result(
                {
                    "format": "csv",
                    "metadata": {"encoding": "utf-8"},
                    "file_name": "table.csv",
                    "size_bytes": path.stat().st_size,
                    "untrusted_content": True,
                    "headers": ["name"],
                    "rows": [["ali"]],
                    "rows_read": 1,
                },
                "read_csv",
            ),
            permission_level="READ_ONLY",
        )
        assert valid.status == "valid"
        invalid = validator.validate(
            "task-1",
            "read_csv",
            {"path": str(path)},
            _result({"format": "csv", "rows": []}, "read_csv"),
            permission_level="READ_ONLY",
        )
        assert invalid.status == "invalid"
    finally:
        guard.close()


def test_partial_and_uncertain_errors_are_explicit_and_not_retried(
    tmp_path: Path,
) -> None:
    guard = PathGuard([tmp_path])
    validator = ToolResultValidator(guard)
    validator.start_task()
    try:
        arguments = {"path": str(tmp_path / "folder")}
        validator.begin_execution("task-1", "create_directory", arguments)
        partial = validator.validate(
            "task-1",
            "create_directory",
            arguments,
            ToolResult(
                tool_name="create_directory",
                status="error",
                error_code="PARTIAL_FAILURE",
                safe_error_message="Tekshirish kerak.",
                execution_duration=0.01,
            ),
            permission_level="CONTROLLED",
        )
        assert partial.status == "partial"
        assert partial.retry == "do_not_retry"

        uncertain = validator.validate(
            "task-1",
            "create_directory",
            arguments,
            ToolResult(
                tool_name="create_directory",
                status="timeout",
                error_code="TIMEOUT",
                safe_error_message="Vaqt tugadi.",
                execution_duration=1,
            ),
            permission_level="CONTROLLED",
        )
        assert uncertain.status == "uncertain"
        assert uncertain.retry == "do_not_retry"
    finally:
        guard.close()


def test_read_only_retry_requires_known_transient_error(tmp_path: Path) -> None:
    guard = PathGuard([tmp_path])
    validator = ToolResultValidator(guard, max_retries=1)
    validator.start_task()
    args = {"path": str(tmp_path / "data.txt")}
    try:
        assert validator.begin_execution("task-1", "read_text_file", args)[0]
        retryable = validator.validate(
            "task-1",
            "read_text_file",
            args,
            ToolResult(
                tool_name="read_text_file",
                status="timeout",
                error_code="TIMEOUT",
                safe_error_message="Vaqt tugadi.",
                execution_duration=1,
            ),
            permission_level="READ_ONLY",
        )
        assert retryable.status == "uncertain"
        assert retryable.retry == "retry"
        assert validator.begin_execution("task-1", "read_text_file", args)[0]
        assert not validator.begin_execution("task-1", "read_text_file", args)[0]
    finally:
        guard.close()


def test_duplicate_successful_action_is_not_executed_twice(tmp_path: Path) -> None:
    guard = PathGuard([tmp_path])
    validator = ToolResultValidator(guard)
    validator.start_task()
    args = {"path": "notes.txt"}
    try:
        first, key = validator.begin_execution("task-1", "create_note", args)
        assert first
        validator.validate(
            "task-1",
            "create_note",
            args,
            _result("created", "create_note"),
            permission_level="CONTROLLED",
        )
        second, duplicate_key = validator.begin_execution(
            "task-1",
            "create_note",
            args,
        )
        assert not second
        assert duplicate_key == key
    finally:
        guard.close()
