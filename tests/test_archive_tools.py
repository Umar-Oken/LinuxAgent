"""Security and behavior tests for archive and directory organization tools."""

import asyncio
import io
import tarfile
import zipfile
from pathlib import Path
from typing import Any

import pytest

from agent import create_tool_registry
from core.tool_dispatcher import ToolDispatcher
from core.tool_registry import ToolDefinition
from security.approval import (
    ApprovalManager,
    ApprovalRequest,
    ApprovalResponse,
    TrustedApprovalInterface,
)
from security.path_guard import PathGuard
from security.permissions import PermissionEngine, PermissionPolicy


class AllowPermission:
    def is_allowed(
        self,
        tool: ToolDefinition,
        arguments: dict[str, Any],
    ) -> bool:
        return True


class ApproveInterface(TrustedApprovalInterface):
    def __init__(self) -> None:
        self.requests: list[ApprovalRequest] = []

    def request_approval(self, request: ApprovalRequest) -> ApprovalResponse:
        self.requests.append(request)
        return ApprovalResponse(
            approval_id=request.approval_id,
            decision="approve",
        )


def _dispatcher(
    root: Path,
    *,
    permission: Any = None,
) -> tuple[ToolDispatcher, PathGuard]:
    guard = PathGuard([root])
    return (
        ToolDispatcher(
            create_tool_registry(guard),
            permission_checker=permission or AllowPermission(),
        ),
        guard,
    )


def _dispatch(
    dispatcher: ToolDispatcher,
    tool: str,
    arguments: dict[str, Any],
) -> Any:
    return asyncio.run(
        dispatcher.dispatch({"name": tool, "arguments": arguments})
    )


def test_organize_returns_preview_before_moving_files(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    document = source / "report.pdf"
    document.write_bytes(b"pdf")
    dispatcher, guard = _dispatcher(tmp_path)
    try:
        result = _dispatch(
            dispatcher,
            "organize_directory",
            {"source_directory": str(source)},
        )
        assert result.status == "success"
        preview = result.result_data
        assert preview["status"] == "preview"
        assert preview["requires_approval"]
        assert preview["actions"][0]["category"] == "documents"
        assert document.exists()
        assert not (source / "documents" / "report.pdf").exists()
    finally:
        guard.close()


def test_duplicate_files_are_found_by_hash_without_removing_them(
    tmp_path: Path,
) -> None:
    (tmp_path / "first.bin").write_bytes(b"same contents")
    (tmp_path / "second.bin").write_bytes(b"same contents")
    (tmp_path / "different.bin").write_bytes(b"different")
    dispatcher, guard = _dispatcher(tmp_path)
    try:
        result = _dispatch(
            dispatcher,
            "find_duplicate_files",
            {"path": str(tmp_path)},
        )
        assert result.status == "success"
        groups = result.result_data["groups"]
        assert len(groups) == 1
        assert groups[0]["copies"] == 2
        assert all((tmp_path / name).exists() for name in (
            "first.bin",
            "second.bin",
            "different.bin",
        ))
    finally:
        guard.close()


def test_create_zip_archive_and_extract_it_safely(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "hello.txt").write_text("hello", encoding="utf-8")
    (source / "nested").mkdir()
    (source / "nested" / "world.txt").write_text("world", encoding="utf-8")
    output = tmp_path / "bundle.zip"
    target = tmp_path / "extracted"
    target.mkdir()
    dispatcher, guard = _dispatcher(tmp_path)
    try:
        created = _dispatch(
            dispatcher,
            "create_archive",
            {"source_directory": str(source), "archive_path": str(output)},
        )
        assert created.status == "success"
        assert created.result_data["files"] == 2
        with zipfile.ZipFile(output) as zipped:
            assert set(zipped.namelist()) == {"hello.txt", "nested/world.txt"}

        extracted = _dispatch(
            dispatcher,
            "extract_archive",
            {"archive_path": str(output), "destination_directory": str(target)},
        )
        assert extracted.status == "success"
        assert extracted.result_data["files"] == 2
        assert (target / "hello.txt").read_text(encoding="utf-8") == "hello"
        assert (target / "nested" / "world.txt").read_text(encoding="utf-8") == "world"
    finally:
        guard.close()


def test_extract_tar_archive_with_regular_file(tmp_path: Path) -> None:
    archive_path = tmp_path / "bundle.tar"
    payload = b"tar data"
    with tarfile.open(archive_path, "w") as archive:
        info = tarfile.TarInfo("folder/file.txt")
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
    target = tmp_path / "target"
    target.mkdir()
    dispatcher, guard = _dispatcher(tmp_path)
    try:
        result = _dispatch(
            dispatcher,
            "extract_archive",
            {"archive_path": str(archive_path), "destination_directory": str(target)},
        )
        assert result.status == "success"
        assert (target / "folder" / "file.txt").read_bytes() == payload
    finally:
        guard.close()


@pytest.mark.parametrize("member_name", ["../outside.txt", "/tmp/outside.txt", "a/../../outside.txt"])
def test_extract_rejects_path_traversal(tmp_path: Path, member_name: str) -> None:
    archive_path = tmp_path / "malicious.zip"
    with zipfile.ZipFile(archive_path, "w") as zipped:
        zipped.writestr(member_name, "malicious")
    target = tmp_path / "target"
    target.mkdir()
    outside = tmp_path.parent / "outside.txt"
    dispatcher, guard = _dispatcher(tmp_path)
    try:
        result = _dispatch(
            dispatcher,
            "extract_archive",
            {"archive_path": str(archive_path), "destination_directory": str(target)},
        )
        assert result.status == "error"
        assert result.error_code == "UNSAFE_ARCHIVE_ENTRY"
        assert not outside.exists()
        assert list(target.iterdir()) == []
    finally:
        guard.close()


def test_extract_rejects_tar_symlink_and_rolls_back(tmp_path: Path) -> None:
    archive_path = tmp_path / "link.tar"
    with tarfile.open(archive_path, "w") as archive:
        data = b"payload"
        info = tarfile.TarInfo("created.txt")
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
        link = tarfile.TarInfo("link")
        link.type = tarfile.SYMTYPE
        link.linkname = "/outside"
        archive.addfile(link)
    target = tmp_path / "target"
    target.mkdir()
    dispatcher, guard = _dispatcher(tmp_path)
    try:
        result = _dispatch(
            dispatcher,
            "extract_archive",
            {"archive_path": str(archive_path), "destination_directory": str(target)},
        )
        assert result.status == "error"
        assert result.error_code == "UNSAFE_ARCHIVE_ENTRY"
        assert list(target.iterdir()) == []
    finally:
        guard.close()


def test_extract_limits_file_count_and_existing_destinations(tmp_path: Path) -> None:
    archive_path = tmp_path / "many.zip"
    with zipfile.ZipFile(archive_path, "w") as zipped:
        zipped.writestr("a.txt", "a")
        zipped.writestr("b.txt", "b")
    target = tmp_path / "target"
    target.mkdir()
    dispatcher, guard = _dispatcher(tmp_path)
    try:
        limited = _dispatch(
            dispatcher,
            "extract_archive",
            {
                "archive_path": str(archive_path),
                "destination_directory": str(target),
                "max_files": 1,
            },
        )
        assert limited.status == "error"
        assert limited.error_code == "ARCHIVE_LIMIT"
        assert list(target.iterdir()) == []

        (target / "a.txt").write_text("keep")
        conflict = _dispatch(
            dispatcher,
            "extract_archive",
            {"archive_path": str(archive_path), "destination_directory": str(target)},
        )
        assert conflict.status == "error"
        assert (target / "a.txt").read_text() == "keep"
        assert not (target / "b.txt").exists()
    finally:
        guard.close()


def test_archive_changes_require_trusted_approval(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "file.txt").write_text("content")
    archive_path = tmp_path / "archive.zip"
    guard = PathGuard([tmp_path])
    registry = create_tool_registry(guard)
    no_approval = ToolDispatcher(
        registry,
        permission_checker=PermissionEngine(PermissionPolicy(), ApprovalManager()),
    )
    try:
        denied = _dispatch(
            no_approval,
            "create_archive",
            {"source_directory": str(source), "archive_path": str(archive_path)},
        )
        assert denied.status == "denied"
        assert not archive_path.exists()

        interface = ApproveInterface()
        approved_dispatcher = ToolDispatcher(
            registry,
            permission_checker=PermissionEngine(
                PermissionPolicy(),
                ApprovalManager(interface),
            ),
        )
        approved = _dispatch(
            approved_dispatcher,
            "create_archive",
            {"source_directory": str(source), "archive_path": str(archive_path)},
        )
        assert approved.status == "success"
        assert len(interface.requests) == 1
        assert "create_archive" in interface.requests[0].action
    finally:
        guard.close()


def test_organize_apply_requires_approval_and_moves_according_to_rules(
    tmp_path: Path,
) -> None:
    source = tmp_path / "organize"
    source.mkdir()
    (source / "photo.jpg").write_bytes(b"image")
    interface = ApproveInterface()
    guard = PathGuard([tmp_path])
    dispatcher = ToolDispatcher(
        create_tool_registry(guard),
        permission_checker=PermissionEngine(
            PermissionPolicy(),
            ApprovalManager(interface),
        ),
    )
    try:
        preview = _dispatch(
            dispatcher,
            "organize_directory",
            {"source_directory": str(source)},
        )
        assert preview.status == "success"
        assert preview.result_data["status"] == "preview"
        result = _dispatch(
            dispatcher,
            "organize_directory",
            {
                "source_directory": str(source),
                "apply": True,
                "plan_id": preview.result_data["plan_id"],
            },
        )
        assert result.status == "success"
        assert result.result_data["status"] == "success"
        assert (source / "images" / "photo.jpg").read_bytes() == b"image"
        assert '"apply":true' in interface.requests[-1].action
        assert preview.result_data["plan_id"] in interface.requests[-1].action
    finally:
        guard.close()


def test_organize_apply_rejects_stale_preview(tmp_path: Path) -> None:
    source = tmp_path / "organize"
    source.mkdir()
    (source / "photo.jpg").write_bytes(b"image")
    guard = PathGuard([tmp_path])
    dispatcher = ToolDispatcher(
        create_tool_registry(guard),
        permission_checker=PermissionEngine(
            PermissionPolicy(),
            ApprovalManager(ApproveInterface()),
        ),
    )
    try:
        preview = _dispatch(
            dispatcher,
            "organize_directory",
            {"source_directory": str(source)},
        )
        (source / "new-photo.jpg").write_bytes(b"new image")
        result = _dispatch(
            dispatcher,
            "organize_directory",
            {
                "source_directory": str(source),
                "apply": True,
                "plan_id": preview.result_data["plan_id"],
            },
        )
        assert result.status == "error"
        assert result.error_code == "STALE_PLAN"
        assert (source / "photo.jpg").exists()
        assert (source / "new-photo.jpg").exists()
    finally:
        guard.close()


def test_archive_tool_is_dispatched_through_agent_controller(
    tmp_path: Path,
) -> None:
    from collections.abc import Sequence

    from core.agent_controller import AgentController
    from core.llm_client import (
        ChatMessage,
        LLMResponse,
        ModelCapabilities,
        ToolCall,
        ToolDefinition as LLMToolDefinition,
    )

    source = tmp_path / "source"
    source.mkdir()
    (source / "file.txt").write_text("agent archive")
    archive_path = tmp_path / "agent.zip"
    call = ToolCall(
        name="create_archive",
        arguments={
            "source_directory": str(source),
            "archive_path": str(archive_path),
        },
    )

    class Client:
        def __init__(self) -> None:
            self.responses = [
                LLMResponse(
                    content="",
                    tool_calls=[call],
                    assistant_message=ChatMessage(
                        role="assistant",
                        content="",
                        tool_calls=[call],
                    ),
                ),
                LLMResponse(
                    content="Arxivlandi.",
                    assistant_message=ChatMessage(
                        role="assistant",
                        content="Arxivlandi.",
                    ),
                ),
            ]
            self.tool_names: set[str] = set()

        def chat(
            self,
            history: Sequence[ChatMessage],
            *,
            system_prompt: str | None = None,
            tools: Sequence[LLMToolDefinition] = (),
        ) -> LLMResponse:
            self.tool_names = {tool.name for tool in tools}
            return self.responses.pop(0)

        def get_capabilities(self) -> ModelCapabilities:
            raise NotImplementedError

    guard = PathGuard([tmp_path])
    registry = create_tool_registry(guard)
    client = Client()
    dispatcher = ToolDispatcher(
        registry,
        permission_checker=PermissionEngine(
            PermissionPolicy(),
            ApprovalManager(ApproveInterface()),
        ),
    )
    controller = AgentController(
        client=client,
        registry=registry,
        dispatcher=dispatcher,
        path_guard=guard,
        system_prompt="test",
        output=lambda _text: None,
    )
    try:
        result = controller.run_task("Create a ZIP archive")
        assert result.status == "completed"
        assert "create_archive" in client.tool_names
        assert archive_path.is_file()
    finally:
        guard.close()
