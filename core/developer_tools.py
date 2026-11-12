"""Read-only, bounded project inspection and allowlisted Git diagnostics."""

from __future__ import annotations

import ast
import configparser
import json
import os
import re
import selectors
import shutil
import signal
import stat
import subprocess
import time
import tomllib
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal

from core.tool_dispatcher import ToolExecutionError
from security.audit_logger import sanitize_text
from security.path_guard import PathAccessError, PathGuard

SearchKind = Literal["text", "function", "class"]
_SOURCE_EXTENSIONS = frozenset(
    {".py", ".pyi", ".md", ".rst", ".txt", ".toml", ".cfg", ".ini", ".yaml", ".yml", ".json"}
)
_DEPENDENCY_FILES = frozenset(
    {
        "requirements.txt",
        "pyproject.toml",
        "setup.cfg",
        "setup.py",
        "pipfile",
        "poetry.lock",
        "environment.yml",
    }
)
_IGNORED_DIRECTORIES = frozenset({".git", ".venv", "venv", "node_modules", "__pycache__"})
_MAX_FILE_BYTES = 1024 * 1024
_MAX_TOTAL_SCAN_BYTES = 20 * 1024 * 1024
_MAX_SCANNED_FILES = 500
_MAX_TOOL_RESULT_BYTES = 24 * 1024
_MAX_GIT_OUTPUT_BYTES = 256 * 1024
_GIT_TIMEOUT_SECONDS = 8.0


def inspect_python_project(
    path: str | Path,
    path_guard: PathGuard,
    *,
    max_depth: int = 4,
    max_entries: int = 200,
) -> dict[str, Any]:
    project = _authorized_project(path, path_guard)
    if not 0 <= max_depth <= 8 or not 1 <= max_entries <= 500:
        raise ToolExecutionError("INVALID_LIMIT", "Loyiha tekshirish limiti noto'g'ri.")
    entries, scanned, symlinks, truncated = _walk_project(
        project,
        path_guard,
        max_depth=max_depth,
        max_entries=max_entries,
    )
    python_files = [
        str(entry["path"])
        for entry in entries
        if entry["type"] == "file" and Path(str(entry["path"])).suffix.casefold() == ".py"
    ]
    dependency_files = [
        str(entry["path"])
        for entry in entries
        if entry["type"] == "file" and Path(str(entry["path"])).name.casefold() in _DEPENDENCY_FILES
    ]
    dependencies: set[str] = set()
    for relative in dependency_files:
        if len(dependencies) >= 100:
            break
        source = _read_project_file(project / relative, path_guard, max_bytes=128 * 1024)
        if source is None:
            continue
        dependencies.update(_parse_dependencies(Path(relative).name, source))
    result: dict[str, object] = {
        "project": path_guard.public_path(project),
        "tree": entries,
        "python_module_count": len(python_files),
        "dependency_files": dependency_files[:50],
        "dependencies": [
            sanitize_text(dependency, max_chars=128)
            for dependency in sorted(dependencies)[:100]
        ],
        "scanned_entries": scanned,
        "skipped_symlinks": symlinks,
        "truncated": truncated,
    }
    return _bound_result(result)


def search_source_code(
    path: str | Path,
    query: str,
    path_guard: PathGuard,
    *,
    kind: SearchKind = "text",
    file_extensions: list[str] | None = None,
    case_sensitive: bool = False,
    max_results: int = 30,
) -> dict[str, Any]:
    if not isinstance(query, str) or not query or len(query) > 128:
        raise ToolExecutionError("INVALID_QUERY", "Manba qidiruv so'rovi noto'g'ri.")
    if kind not in {"text", "function", "class"} or not 1 <= max_results <= 100:
        raise ToolExecutionError("INVALID_QUERY", "Manba qidiruv parametrlari noto'g'ri.")
    project = _authorized_project(path, path_guard)
    extensions = _validated_extensions(file_extensions)
    entries, _scanned, symlinks, was_truncated = _walk_project(
        project,
        path_guard,
        max_depth=12,
        max_entries=_MAX_SCANNED_FILES,
    )
    candidates = [
        str(entry["path"])
        for entry in entries
        if entry["type"] == "file"
        and Path(str(entry["path"])).suffix.casefold() in extensions
    ]
    needle = query if case_sensitive else query.casefold()
    results: list[dict[str, object]] = []
    scanned_bytes = 0
    skipped_large = 0
    files_examined = 0
    truncated = was_truncated

    for relative in candidates:
        if files_examined >= _MAX_SCANNED_FILES or scanned_bytes >= _MAX_TOTAL_SCAN_BYTES:
            truncated = True
            break
        file_path = project / relative
        try:
            fd = path_guard.open_file(file_path)
        except PathAccessError:
            continue
        try:
            size = os.fstat(fd).st_size
            if size > _MAX_FILE_BYTES or scanned_bytes + size > _MAX_TOTAL_SCAN_BYTES:
                skipped_large += 1
                continue
            source_bytes = _read_fd(fd, _MAX_FILE_BYTES)
        finally:
            os.close(fd)
        files_examined += 1
        scanned_bytes += len(source_bytes)
        source = source_bytes.decode("utf-8", errors="replace")

        if kind in {"function", "class"}:
            if Path(relative).suffix.casefold() not in {".py", ".pyi"}:
                continue
            try:
                tree = ast.parse(source, filename=relative)
            except (SyntaxError, ValueError, RecursionError):
                continue
            definitions = (
                (ast.FunctionDef, ast.AsyncFunctionDef)
                if kind == "function"
                else (ast.ClassDef,)
            )
            for node in ast.walk(tree):
                if isinstance(node, definitions) and node.name == query:
                    snippet = _source_line(source, node.lineno)
                    results.append(
                        {
                            "path": relative[:512],
                            "line": node.lineno,
                            "kind": kind,
                            "name": node.name,
                            "snippet": sanitize_text(snippet, max_chars=240),
                        }
                    )
        else:
            for line_number, line in enumerate(source.splitlines(), start=1):
                candidate_line = line if case_sensitive else line.casefold()
                if needle in candidate_line:
                    results.append(
                        {
                            "path": relative[:512],
                            "line": line_number,
                            "kind": "text",
                            "snippet": sanitize_text(line, max_chars=240),
                        }
                    )
                if len(results) >= max_results:
                    truncated = True
                    break
        if len(results) >= max_results:
            truncated = True
            break

    return _bound_result(
        {
            "query": query,
            "kind": kind,
            "results": results,
            "result_count": len(results),
            "files_examined": files_examined,
            "bytes_examined": scanned_bytes,
            "skipped_large_files": skipped_large,
            "skipped_symlinks": symlinks,
            "truncated": truncated,
        }
    )


def git_status(path: str | Path, path_guard: PathGuard) -> dict[str, Any]:
    project = _authorized_project(path, path_guard)
    _validate_git_repository(project, path_guard)
    output, returncode, truncated = _run_git(
        project,
        [
            "status",
            "--short",
            "--branch",
            "--untracked-files=normal",
            "--ignore-submodules=all",
        ],
    )
    if returncode != 0 and not truncated:
        raise ToolExecutionError(
            "GIT_REPOSITORY_NOT_FOUND",
            "Ko'rsatilgan loyiha Git repository emas yoki uning holatini o'qib bo'lmadi.",
        )
    safe_output = sanitize_text(output, max_chars=12_000)
    return _bound_result(
        {
            "project": path_guard.public_path(project),
            "status": safe_output,
            "truncated": truncated or len(output) > 12_000,
        }
    )


def git_diff(
    path: str | Path,
    path_guard: PathGuard,
    *,
    staged: bool = False,
    max_chars: int = 12_000,
) -> dict[str, Any]:
    if not 100 <= max_chars <= 16_000:
        raise ToolExecutionError("INVALID_LIMIT", "Git diff hajm limiti noto'g'ri.")
    project = _authorized_project(path, path_guard)
    _validate_git_repository(project, path_guard)
    arguments = [
        "diff",
        "--no-ext-diff",
        "--no-textconv",
        "--no-color",
        "--no-renames",
        "--ignore-submodules=all",
        "--unified=2",
    ]
    if staged:
        arguments.append("--cached")
    output, returncode, truncated = _run_git(project, arguments)
    if returncode != 0 and not truncated:
        raise ToolExecutionError(
            "GIT_DIFF_FAILED",
            "Git diff ma'lumotini xavfsiz olib bo'lmadi.",
        )
    safe_output = sanitize_text(output, max_chars=max_chars)
    return _bound_result(
        {
            "project": path_guard.public_path(project),
            "staged": staged,
            "diff": safe_output,
            "truncated": truncated or len(output) > max_chars,
        }
    )


def _authorized_project(path: str | Path, path_guard: PathGuard) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    try:
        project = path_guard.authorize_path(candidate)
        if Path(os.path.abspath(candidate)) != project or not project.is_dir():
            raise ToolExecutionError(
                "INVALID_PROJECT_PATH",
                "Loyiha yo'li mavjud, ruxsatli va symlinksiz katalog bo'lishi kerak.",
            )
        descriptor = path_guard.open_directory(project)
        os.close(descriptor)
        return project
    except PathAccessError as exc:
        raise ToolExecutionError(exc.code, exc.safe_message) from None


def _walk_project(
    project: Path,
    path_guard: PathGuard,
    *,
    max_depth: int,
    max_entries: int,
) -> tuple[list[dict[str, object]], int, int, bool]:
    entries: list[dict[str, object]] = []
    stack: list[tuple[Path, int]] = [(project, 0)]
    scanned = 0
    symlinks = 0
    truncated = False
    while stack and scanned < max_entries:
        current, depth = stack.pop()
        try:
            directory_fd = path_guard.open_directory(current)
        except PathAccessError:
            continue
        try:
            try:
                names: list[str] = []
                with os.scandir(directory_fd) as scanner:
                    for entry in scanner:
                        names.append(entry.name)
                        if len(names) > max_entries - scanned:
                            truncated = True
                            break
                names = sorted(names[: max_entries - scanned], key=str.casefold)
            except OSError:
                continue
            child_directories: list[tuple[Path, int]] = []
            for name in names:
                if scanned >= max_entries:
                    truncated = True
                    break
                try:
                    details = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                except OSError:
                    continue
                scanned += 1
                relative = (current / name).relative_to(project).as_posix()
                if stat.S_ISLNK(details.st_mode):
                    symlinks += 1
                    continue
                is_directory = stat.S_ISDIR(details.st_mode)
                if not is_directory and not stat.S_ISREG(details.st_mode):
                    continue
                if is_directory and name.casefold() in _IGNORED_DIRECTORIES:
                    continue
                entry_type = "directory" if is_directory else "file"
                entries.append(
                    {
                        "path": relative[:512],
                        "type": entry_type,
                        "size_bytes": 0 if is_directory else details.st_size,
                    }
                )
                if is_directory and depth < max_depth:
                    child_directories.append((current / name, depth + 1))
                elif is_directory:
                    truncated = True
            stack.extend(reversed(child_directories))
        finally:
            os.close(directory_fd)
    if stack:
        truncated = True
    return entries, scanned, symlinks, truncated


def _read_project_file(path: Path, path_guard: PathGuard, *, max_bytes: int) -> str | None:
    try:
        fd = path_guard.open_file(path)
    except PathAccessError:
        return None
    try:
        if os.fstat(fd).st_size > max_bytes:
            return None
        return _read_fd(fd, max_bytes).decode("utf-8", errors="replace")
    except OSError:
        return None
    finally:
        os.close(fd)


def _read_fd(fd: int, maximum: int) -> bytes:
    chunks = bytearray()
    while len(chunks) <= maximum:
        chunk = os.read(fd, min(64 * 1024, maximum + 1 - len(chunks)))
        if not chunk:
            break
        chunks.extend(chunk)
    if len(chunks) > maximum:
        raise ToolExecutionError("SOURCE_FILE_TOO_LARGE", "Manba fayli hajm limiti oshdi.")
    return bytes(chunks)


def _parse_dependencies(filename: str, source: str) -> set[str]:
    found: set[str] = set()
    if filename.casefold() == "requirements.txt":
        found.update(_requirements_names(source.splitlines()))
    elif filename.casefold() == "pyproject.toml":
        try:
            document = tomllib.loads(source)
        except (tomllib.TOMLDecodeError, ValueError):
            return found
        project = document.get("project")
        if isinstance(project, dict):
            dependencies = project.get("dependencies", [])
            if isinstance(dependencies, list):
                found.update(_requirements_names(dependencies))
            optional = project.get("optional-dependencies", {})
            if isinstance(optional, dict):
                for values in optional.values():
                    if isinstance(values, list):
                        found.update(_requirements_names(values))
        poetry = document.get("tool")
        if isinstance(poetry, dict):
            poetry = poetry.get("poetry")
        if isinstance(poetry, dict):
            dependencies = poetry.get("dependencies", {})
            if isinstance(dependencies, dict):
                found.update(
                    name
                    for name in dependencies
                    if name.casefold() != "python" and _valid_package_name(name)
                )
            groups = poetry.get("group", {})
            if isinstance(groups, dict):
                for group in groups.values():
                    if isinstance(group, dict):
                        dependencies = group.get("dependencies", {})
                        if isinstance(dependencies, dict):
                            found.update(
                                name
                                for name in dependencies
                                if _valid_package_name(name)
                            )
    elif filename.casefold() == "setup.cfg":
        config = configparser.ConfigParser(interpolation=None, strict=False)
        try:
            config.read_string(source)
        except configparser.Error:
            return found
        if config.has_section("options"):
            found.update(_requirements_names(config.get("options", "install_requires", fallback="").splitlines()))
        if config.has_section("options.extras_require"):
            for value in config["options.extras_require"].values():
                found.update(_requirements_names(value.splitlines()))
    elif filename.casefold() == "setup.py":
        try:
            module = ast.parse(source, filename=filename)
        except (SyntaxError, ValueError, RecursionError):
            return found
        for node in ast.walk(module):
            if not isinstance(node, ast.Call) or not _is_setup_call(node.func):
                continue
            for keyword in node.keywords:
                if keyword.arg not in {"install_requires", "setup_requires"}:
                    continue
                try:
                    requirements = ast.literal_eval(keyword.value)
                except (ValueError, TypeError, SyntaxError, RecursionError):
                    continue
                if isinstance(requirements, (list, tuple)):
                    found.update(_requirements_names(requirements))
    return found


def _requirements_names(requirements: Sequence[object]) -> set[str]:
    found: set[str] = set()
    for requirement in requirements:
        if not isinstance(requirement, str):
            continue
        package = requirement.strip()
        if not package or package.startswith(("#", "-", ".")):
            continue
        package = package.split(";", 1)[0].split("[", 1)[0].split("@", 1)[0].strip()
        for separator in ("==", ">=", "<=", "~=", "!=", ">", "<"):
            package = package.split(separator, 1)[0]
        package = package.strip()
        if _valid_package_name(package):
            found.add(package)
    return found


def _valid_package_name(name: str) -> bool:
    return bool(name and len(name) <= 128 and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name))


def _is_setup_call(function: ast.expr) -> bool:
    if isinstance(function, ast.Name):
        return function.id == "setup"
    return (
        isinstance(function, ast.Attribute)
        and function.attr == "setup"
        and isinstance(function.value, ast.Name)
        and function.value.id == "setuptools"
    )


def _validated_extensions(values: list[str] | None) -> frozenset[str]:
    if not values:
        return _SOURCE_EXTENSIONS
    if len(values) > 12 or any(
        not isinstance(value, str)
        or len(value) > 10
        or not value.startswith(".")
        or value.casefold() not in _SOURCE_EXTENSIONS
        for value in values
    ):
        raise ToolExecutionError(
            "INVALID_EXTENSION",
            "Manba qidiruvi uchun fayl kengaytmasi ruxsat etilmagan.",
        )
    return frozenset(value.casefold() for value in values)


def _source_line(source: str, line_number: int) -> str:
    lines = source.splitlines()
    if 1 <= line_number <= len(lines):
        return lines[line_number - 1]
    return ""


def _validate_git_repository(project: Path, path_guard: PathGuard) -> None:
    try:
        project_fd = path_guard.open_directory(project)
        try:
            try:
                metadata = os.stat(".git", dir_fd=project_fd, follow_symlinks=False)
            except FileNotFoundError:
                raise ToolExecutionError(
                    "GIT_REPOSITORY_NOT_FOUND",
                    "Ko'rsatilgan loyiha Git repository emas.",
                ) from None
            if not stat.S_ISDIR(metadata.st_mode):
                raise ToolExecutionError(
                    "UNSAFE_GIT_REPOSITORY",
                    "Faqat ichki .git katalogiga ega repository'lar tekshiriladi.",
                )
        finally:
            os.close(project_fd)
        git_directory = path_guard.open_directory(project / ".git")
    except PathAccessError as exc:
        if exc.code == "NOT_FOUND":
            raise ToolExecutionError(
                "GIT_REPOSITORY_NOT_FOUND",
                "Ko'rsatilgan loyiha Git repository emas.",
            ) from None
        raise ToolExecutionError("UNSAFE_GIT_REPOSITORY", exc.safe_message) from None
    try:
        config_path = project / ".git" / "config"
        config_text = _read_project_file(config_path, path_guard, max_bytes=128 * 1024)
        if config_text is None:
            raise ToolExecutionError(
                "UNSAFE_GIT_REPOSITORY",
                "Git repository konfiguratsiyasini xavfsiz tekshirib bo'lmadi.",
            )
        config = configparser.ConfigParser(interpolation=None, strict=False)
        try:
            config.read_string(config_text)
        except configparser.Error:
            raise ToolExecutionError(
                "UNSAFE_GIT_REPOSITORY",
                "Git repository konfiguratsiyasi yaroqsiz.",
            ) from None
        if any(section.casefold().startswith("include") for section in config.sections()):
            raise ToolExecutionError(
                "UNSAFE_GIT_REPOSITORY",
                "Tashqi Git konfiguratsiyasini o'z ichiga olgan repository tekshirilmaydi.",
            )
        core = config["core"] if config.has_section("core") else {}
        extensions = (
            config["extensions"] if config.has_section("extensions") else {}
        )
        if core.get("worktree") or core.get("bare", "false").casefold() == "true":
            raise ToolExecutionError(
                "UNSAFE_GIT_REPOSITORY",
                "Tashqi worktree yoki bare repository tekshirilmaydi.",
            )
        if extensions.get("worktreeconfig", "false").casefold() == "true":
            raise ToolExecutionError(
                "UNSAFE_GIT_REPOSITORY",
                "Linked worktree konfiguratsiyasi tekshirilmaydi.",
            )
        for metadata_path in (
            project / ".git" / "commondir",
            project / ".git" / "objects" / "info" / "alternates",
        ):
            try:
                fd = path_guard.open_file(metadata_path)
            except PathAccessError as exc:
                if exc.code == "NOT_FOUND":
                    continue
                raise ToolExecutionError(
                    "UNSAFE_GIT_REPOSITORY",
                    "Git repository tashqi metadata'ga murojaat qilishi mumkin.",
                ) from None
            else:
                os.close(fd)
                raise ToolExecutionError(
                    "UNSAFE_GIT_REPOSITORY",
                    "Git repository tashqi metadata'ga murojaat qilishi mumkin.",
                )

        entries, _scanned, symlinks, truncated = _walk_project(
            project,
            path_guard,
            max_depth=16,
            max_entries=2000,
        )
        if symlinks:
            raise ToolExecutionError(
                "UNSAFE_GIT_REPOSITORY",
                "Symlink mavjud repository Git diagnostikasi uchun qabul qilinmaydi.",
            )
        if truncated:
            raise ToolExecutionError(
                "UNSAFE_GIT_REPOSITORY",
                "Repository atributlarini to'liq tekshirish limiti oshdi.",
            )
        attribute_files = [
            str(entry["path"])
            for entry in entries
            if entry["type"] == "file"
            and Path(str(entry["path"])).name == ".gitattributes"
        ]
        attribute_files.append(".git/info/attributes")
        for relative in attribute_files:
            text = _read_project_file(project / relative, path_guard, max_bytes=64 * 1024)
            if text is None:
                if relative == ".git/info/attributes":
                    continue
                raise ToolExecutionError(
                    "UNSAFE_GIT_REPOSITORY",
                    "Git atribut faylini xavfsiz tekshirib bo'lmadi.",
                )
            if re.search(r"(?i)(?:^|\s)filter\s*=", text):
                raise ToolExecutionError(
                    "UNSAFE_GIT_REPOSITORY",
                    "Git filter konfiguratsiyasi mavjud repository tekshirilmaydi.",
                )
    finally:
        os.close(git_directory)


def _run_git(
    project: Path,
    arguments: list[str],
) -> tuple[str, int, bool]:
    executable = shutil.which("git")
    if executable is None:
        raise ToolExecutionError("GIT_UNAVAILABLE", "Git dasturi topilmadi.")
    command = [
        executable,
        "--no-pager",
        "--no-optional-locks",
        "-c",
        "core.pager=cat",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "core.worktree=" + str(project),
        "-c",
        "core.bare=false",
        "-c",
        "core.attributesFile=/dev/null",
        "-c",
        "diff.external=",
        "-c",
        "color.ui=false",
        "-C",
        str(project),
        *arguments,
    ]
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("GIT_")
    }
    environment.update({
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_PAGER": "cat",
        "PAGER": "cat",
        "GIT_OPTIONAL_LOCKS": "0",
    })
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=project,
            env=environment,
            close_fds=True,
            start_new_session=True,
        )
    except OSError:
        raise ToolExecutionError("GIT_UNAVAILABLE", "Git buyruqini ishga tushirib bo'lmadi.") from None

    output = bytearray()
    truncated = False
    deadline = time.monotonic() + _GIT_TIMEOUT_SECONDS
    if process.stdout is None:
        _terminate_git(process)
        raise ToolExecutionError("GIT_FAILED", "Git natijasini o'qib bo'lmadi.")
    selector = selectors.DefaultSelector()
    try:
        selector.register(process.stdout, selectors.EVENT_READ)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _terminate_git(process)
                raise ToolExecutionError("GIT_TIMEOUT", "Git amali vaqt limitidan oshdi.")
            if not selector.select(min(remaining, 0.25)):
                if process.poll() is not None:
                    break
                continue
            chunk = os.read(process.stdout.fileno(), 16 * 1024)
            if not chunk:
                break
            room = _MAX_GIT_OUTPUT_BYTES - len(output)
            output.extend(chunk[:room])
            if len(chunk) > room:
                truncated = True
                _terminate_git(process)
                break
        returncode = process.wait(timeout=max(0.1, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        _terminate_git(process)
        raise ToolExecutionError("GIT_TIMEOUT", "Git amali vaqt limitidan oshdi.") from None
    finally:
        selector.close()
        process.stdout.close()
    return output.decode("utf-8", errors="replace"), returncode, truncated


def _terminate_git(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _bound_result(result: dict[str, Any]) -> dict[str, Any]:
    encoded = json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(encoded) <= _MAX_TOOL_RESULT_BYTES:
        return result
    keys = ("tree", "results", "dependencies", "dependency_files")
    while len(encoded) > _MAX_TOOL_RESULT_BYTES:
        reduced = False
        for key in keys:
            value = result.get(key)
            if isinstance(value, list) and len(value) > 1:
                result[key] = value[: len(value) // 2]
                result["truncated"] = True
                reduced = True
                encoded = json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8")
                if len(encoded) <= _MAX_TOOL_RESULT_BYTES:
                    return result
        if not reduced:
            break
    raise ToolExecutionError("RESULT_TOO_LARGE", "Loyiha tekshiruvi natijasi hajm limitidan oshdi.")
