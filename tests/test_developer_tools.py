from __future__ import annotations

import asyncio
import json
import os
import subprocess
from collections.abc import Awaitable, Mapping
from pathlib import Path
from typing import Any

import pytest

from agent import create_tool_registry
from core.developer_tools import (
    git_diff,
    git_status,
    inspect_python_project,
    search_source_code,
)
from core.tool_dispatcher import ToolDispatcher, ToolExecutionError
from core.tool_registry import ToolDefinition
from security.path_guard import PathGuard


class _Allow:
    def is_allowed(
        self,
        tool: ToolDefinition,
        arguments: Mapping[str, Any],
    ) -> bool | Awaitable[bool]:
        return True


def _git(project: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", "-C", str(project), *arguments],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "Tests",
            "GIT_AUTHOR_EMAIL": "tests@example.invalid",
            "GIT_COMMITTER_NAME": "Tests",
            "GIT_COMMITTER_EMAIL": "tests@example.invalid",
        },
    )


def test_inspect_python_project_lists_tree_modules_and_dependencies(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    (project / "pkg").mkdir(parents=True)
    (project / "pkg" / "module.py").write_text("class Widget:\n    pass\n", encoding="utf-8")
    (project / "requirements.txt").write_text(
        "httpx>=0.27\npydantic==2.8\n# comment\n", encoding="utf-8"
    )
    (project / "pyproject.toml").write_text(
        '[project]\ndependencies = ["requests>=2.31"]\n', encoding="utf-8"
    )
    (project / "pkg" / "outside.py").symlink_to(tmp_path / "outside.py")
    guard = PathGuard([tmp_path])
    try:
        result = inspect_python_project(project, guard)
    finally:
        guard.close()

    assert result["python_module_count"] == 1
    assert "httpx" in result["dependencies"]
    assert "pydantic" in result["dependencies"]
    assert "requests" in result["dependencies"]
    assert result["skipped_symlinks"] == 1
    assert any(entry["path"] == "pkg/module.py" for entry in result["tree"])
    assert not any(entry["path"] == "pkg/outside.py" for entry in result["tree"])


def test_inspect_python_project_parses_setup_metadata_without_execution(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "setup.cfg").write_text(
        "[options]\ninstall_requires =\n    flask>=3\n    requests\n",
        encoding="utf-8",
    )
    marker = tmp_path / "setup-executed"
    (project / "setup.py").write_text(
        "from setuptools import setup\n"
        f"open({str(marker)!r}, 'w').write('executed')\n"
        "setup(install_requires=['click>=8', 'urllib3'])\n",
        encoding="utf-8",
    )
    guard = PathGuard([tmp_path])
    try:
        result = inspect_python_project(project, guard)
    finally:
        guard.close()

    assert {"flask", "requests", "click", "urllib3"} <= set(result["dependencies"])
    assert not marker.exists()


def test_source_search_finds_text_functions_and_classes_with_redaction(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "example.py").write_text(
        'TOKEN = "top-secret-value"\n\n'
        "def calculate_total():\n    return 3\n\n"
        "class Invoice:\n    pass\n",
        encoding="utf-8",
    )
    guard = PathGuard([tmp_path])
    try:
        text = search_source_code(project, "TOKEN", guard)
        function = search_source_code(project, "calculate_total", guard, kind="function")
        class_result = search_source_code(project, "Invoice", guard, kind="class")
    finally:
        guard.close()

    assert text["result_count"] == 1
    assert "top-secret-value" not in text["results"][0]["snippet"]
    assert "[REDACTED]" in text["results"][0]["snippet"]
    assert function["results"][0]["line"] == 3
    assert class_result["results"][0]["name"] == "Invoice"


def test_search_is_bounded_and_skips_large_source_files(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    for index in range(3):
        (project / f"{index}.py").write_text(f"needle = {index}\n", encoding="utf-8")
    (project / "large.py").write_bytes(b"needle\n" * (1024 * 1024 // 7 + 1))
    guard = PathGuard([tmp_path])
    try:
        result = search_source_code(project, "needle", guard)
        limited = search_source_code(project, "needle", guard, max_results=1)
    finally:
        guard.close()

    assert result["result_count"] == 3
    assert result["truncated"] is False
    assert result["skipped_large_files"] == 1
    assert result["bytes_examined"] < 1024 * 1024
    assert limited["result_count"] == 1
    assert limited["truncated"] is True


def test_project_tree_result_size_is_bounded(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    for index in range(250):
        (project / f"{index:03d}-{'x' * 150}").touch()
    guard = PathGuard([tmp_path])
    try:
        result = inspect_python_project(project, guard, max_entries=500)
    finally:
        guard.close()

    assert len(json.dumps(result, ensure_ascii=False).encode()) <= 24 * 1024
    assert result["truncated"] is True


def test_project_tools_reject_outside_and_symlink_paths(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = allowed / "link"
    link.symlink_to(outside, target_is_directory=True)
    guard = PathGuard([allowed])
    try:
        with pytest.raises(ToolExecutionError) as outside_error:
            inspect_python_project(outside, guard)
        with pytest.raises(ToolExecutionError) as link_error:
            search_source_code(link, "x", guard)
    finally:
        guard.close()

    assert outside_error.value.code == "OUTSIDE_ALLOWED_ROOT"
    assert link_error.value.code in {
        "INVALID_PROJECT_PATH",
        "SYMLINK_NOT_ALLOWED",
        "OUTSIDE_ALLOWED_ROOT",
    }


def test_git_status_and_diff_are_bounded_and_redact_secrets(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    _git(project, "init", "-q")
    source = project / "app.py"
    source.write_text('password = "old"\n', encoding="utf-8")
    _git(project, "add", "app.py")
    _git(project, "commit", "-q", "-m", "initial")
    source.write_text('password = "private-value"\n', encoding="utf-8")
    other_repo = tmp_path / "other-repo"
    other_repo.mkdir()
    _git(other_repo, "init", "-q")
    monkeypatch.setenv("GIT_DIR", str(other_repo / ".git"))
    guard = PathGuard([tmp_path])
    try:
        status = git_status(project, guard)
        diff = git_diff(project, guard, max_chars=500)
    finally:
        guard.close()

    assert "app.py" in status["status"]
    assert "app.py" in diff["diff"]
    assert "private-value" not in diff["diff"]
    assert "[REDACTED]" in diff["diff"]
    assert diff["truncated"] is False


def test_git_diagnostics_reject_non_repository_and_disable_external_diff(
    tmp_path: Path,
) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    guard = PathGuard([tmp_path])
    try:
        with pytest.raises(ToolExecutionError) as not_repo:
            git_status(project, guard)
        _git(project, "init", "-q")
        source = project / "file.txt"
        source.write_text("before\n", encoding="utf-8")
        _git(project, "add", "file.txt")
        _git(project, "-c", "user.name=Tests", "-c", "user.email=tests@example.invalid", "commit", "-q", "-m", "init")
        source.write_text("after\n", encoding="utf-8")
        marker = tmp_path / "external-diff-ran"
        external = tmp_path / "external-diff.sh"
        external.write_text(f"#!/bin/sh\ntouch '{marker}'\n", encoding="utf-8")
        external.chmod(0o700)
        _git(project, "config", "diff.external", str(external))
        result = git_diff(project, guard)
    finally:
        guard.close()

    assert not_repo.value.code == "GIT_REPOSITORY_NOT_FOUND"
    assert "after" in result["diff"]
    assert not marker.exists()


def test_git_diagnostics_reject_filters_without_executing_them(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    _git(project, "init", "-q")
    marker = tmp_path / "filter-ran"
    filter_script = tmp_path / "filter.sh"
    filter_script.write_text(f"#!/bin/sh\ntouch '{marker}'\n", encoding="utf-8")
    filter_script.chmod(0o700)
    _git(project, "config", "filter.unsafe.clean", str(filter_script))
    (project / ".gitattributes").write_text("* filter=unsafe\n", encoding="utf-8")
    guard = PathGuard([tmp_path])
    try:
        with pytest.raises(ToolExecutionError) as error:
            git_status(project, guard)
    finally:
        guard.close()

    assert error.value.code == "UNSAFE_GIT_REPOSITORY"
    assert not marker.exists()


def test_tools_are_registered_read_only_and_dispatchable(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "sample.py").write_text("def hello():\n    pass\n", encoding="utf-8")
    guard = PathGuard([tmp_path])
    dispatcher = ToolDispatcher(create_tool_registry(guard), permission_checker=_Allow())
    try:
        names = {
            "inspect_python_project",
            "search_source_code",
            "git_status",
            "git_diff",
        }
        definitions = {
            tool.name: tool
            for tool in create_tool_registry(guard).list_tools()
            if tool.name in names
        }
        assert set(definitions) == names
        assert all(tool.permission_level == "READ_ONLY" for tool in definitions.values())
        assert all(tool.argument_model.model_config.get("extra") == "forbid" for tool in definitions.values())
        result = asyncio.run(
            dispatcher.dispatch(
                {
                    "name": "search_source_code",
                    "arguments": {
                        "path": str(project),
                        "query": "hello",
                        "kind": "function",
                    },
                }
            )
        )
    finally:
        guard.close()

    assert result.status == "success"
    assert result.result_data["results"][0]["name"] == "hello"


def test_dispatch_rejects_null_and_unknown_search_arguments(tmp_path: Path) -> None:
    guard = PathGuard([tmp_path])
    dispatcher = ToolDispatcher(create_tool_registry(guard), permission_checker=_Allow())
    try:
        null_query = asyncio.run(
            dispatcher.dispatch(
                {
                    "name": "search_source_code",
                    "arguments": {"path": str(tmp_path), "query": None},
                }
            )
        )
        unknown_argument = asyncio.run(
            dispatcher.dispatch(
                {
                    "name": "git_diff",
                    "arguments": {"path": str(tmp_path), "shell": "true"},
                }
            )
        )
    finally:
        guard.close()

    assert null_query.error_code == "INVALID_ARGUMENTS"
    assert unknown_argument.error_code == "INVALID_ARGUMENTS"
