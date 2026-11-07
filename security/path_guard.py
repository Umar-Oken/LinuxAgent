"""Default-deny filesystem authorization and descriptor-relative file access."""

from __future__ import annotations

import errno
import os
import stat
from pathlib import Path
from threading import RLock
from typing import Literal

PathOperation = Literal["read", "write"]

_SYSTEM_PATHS = (
    Path("/etc"),
    Path("/root"),
    Path("/boot"),
    Path("/sys"),
    Path("/proc"),
    Path("/dev"),
    Path("/usr"),
    Path("/bin"),
    Path("/sbin"),
    Path("/lib"),
    Path("/lib64"),
    Path("/var/lib"),
    Path("/var/log"),
)
_SENSITIVE_COMPONENTS = {
    ".ssh",
    ".gnupg",
    ".aws",
    ".azure",
    ".kube",
    ".docker",
    ".mozilla",
    ".config/google-chrome",
    ".config/chromium",
    ".config/microsoft-edge",
    ".config/BraveSoftware",
    ".config/opera",
    ".config/vivaldi",
    ".config/gcloud",
    ".config/gh",
    ".config/credentials",
    "credentials",
}
_SENSITIVE_FILENAMES = {
    ".netrc",
    ".git-credentials",
    ".pypirc",
    "credentials",
    "credentials.json",
    "logins.json",
    "key4.db",
    "cookies",
    "cookies.sqlite",
    "login data",
    "local state",
}
_ALLOWED_OPEN_FLAGS = (
    os.O_RDONLY
    | os.O_WRONLY
    | os.O_RDWR
    | os.O_CREAT
    | os.O_EXCL
    | os.O_TRUNC
    | os.O_APPEND
)


class PathAccessError(PermissionError):
    """A safe, stable path authorization failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.safe_message = message


class PathGuard:
    """Authorize paths only within explicitly configured, trusted roots.

    The guard never grants the home directory automatically. Descriptor-based
    opens reject symlinks in every path component, protecting operations from
    symlink swaps between a path check and the actual open. Directory renames
    by another process (especially a privileged process) cannot be fully
    constrained by portable Python APIs; Linux ``openat2`` would be needed for
    stronger kernel-enforced beneath-root resolution.
    """

    def __init__(
        self,
        allowed_roots: list[str | Path] | tuple[str | Path, ...] = (),
        *,
        protected_paths: list[str | Path] | tuple[str | Path, ...] = (),
        allow_sensitive: bool = False,
    ) -> None:
        self._lock = RLock()
        self._closed = False
        self._allow_sensitive = allow_sensitive
        roots: list[Path] = []
        for configured_root in allowed_roots:
            root = _resolve_configured_root(configured_root)
            if root not in roots:
                roots.append(root)
        self._roots = tuple(sorted(roots, key=lambda item: len(item.parts), reverse=True))

        configured_protected = [
            Path(path).expanduser() for path in protected_paths
        ]
        home = Path.home()
        configured_protected.extend(
            home / item for item in (".ssh", ".gnupg", ".aws", ".azure", ".kube", ".docker")
        )
        self._protected_paths = tuple(
            _resolve_protected_path(path) for path in configured_protected
        )

        self._root_fds: dict[Path, int] = {}
        try:
            for root in self._roots:
                self._root_fds[root] = os.open(
                    root,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                )
        except OSError as exc:
            self.close()
            raise PathAccessError(
                "INVALID_ROOT",
                "An allowed root is not a safely accessible directory.",
            ) from exc

    @property
    def allowed_roots(self) -> tuple[Path, ...]:
        """Return the canonical explicitly configured roots."""
        return self._roots

    def authorize_path(
        self,
        path: str | Path,
        *,
        operation: PathOperation = "read",
        allow_missing: bool = False,
    ) -> Path:
        """Validate a path and return its canonical absolute location.

        This method authorizes a path but does not pin it against later changes.
        For actual file access, prefer :meth:`open_file`.
        """
        if operation not in {"read", "write"}:
            raise PathAccessError("INVALID_OPERATION", "Requested file operation is invalid.")
        if not self._roots:
            raise PathAccessError("NO_ALLOWED_ROOTS", "No filesystem roots are authorized.")
        candidate = _candidate_path(path)
        try:
            if allow_missing:
                resolved = _resolve_destination(candidate)
            else:
                resolved = candidate.resolve(strict=True)
        except FileNotFoundError as exc:
            raise PathAccessError("NOT_FOUND", "Requested path does not exist.") from exc
        except (OSError, RuntimeError) as exc:
            raise PathAccessError("INVALID_PATH", "Requested path cannot be safely resolved.") from exc

        self._ensure_allowed(resolved)
        self._ensure_not_protected(candidate, resolved)
        return resolved

    def open_file(
        self,
        path: str | Path,
        *,
        flags: int = os.O_RDONLY,
        mode: int = 0o600,
        operation: PathOperation | None = None,
    ) -> int:
        """Safely open a regular file beneath an allowed root and return its fd.

        Callers own and must close the returned descriptor. All path components
        are opened relative to directory descriptors with ``O_NOFOLLOW``.
        """
        if flags & ~_ALLOWED_OPEN_FLAGS:
            raise PathAccessError("INVALID_FLAGS", "Requested file access flags are not allowed.")
        file_access = flags & (os.O_WRONLY | os.O_RDWR)
        writing = flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)
        if writing and not file_access:
            raise PathAccessError(
                "INVALID_FLAGS",
                "Write-related flags require a writable file descriptor.",
            )
        actual_operation: PathOperation = operation or ("write" if writing else "read")
        if (actual_operation == "read" and writing) or (
            actual_operation == "write" and not writing
        ):
            raise PathAccessError("INVALID_OPERATION", "File operation does not match access flags.")

        candidate = _candidate_path(path)
        allow_missing = bool(flags & os.O_CREAT)
        self.authorize_path(
            candidate,
            operation=actual_operation,
            allow_missing=allow_missing,
        )
        root = self._find_lexical_root(candidate)
        relative = candidate.relative_to(root)
        if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
            raise PathAccessError("INVALID_PATH", "Requested path is not a file beneath an allowed root.")

        with self._lock:
            if self._closed:
                raise PathAccessError("CLOSED", "Filesystem authorization is closed.")
            root_fd = self._root_fds[root]
            parent_fd = os.dup(root_fd)
            try:
                for component in relative.parts[:-1]:
                    next_fd = os.open(
                        component,
                        os.O_RDONLY
                        | os.O_DIRECTORY
                        | os.O_CLOEXEC
                        | os.O_NOFOLLOW,
                        dir_fd=parent_fd,
                    )
                    os.close(parent_fd)
                    parent_fd = next_fd
                try:
                    file_fd = os.open(
                        relative.parts[-1],
                        flags | os.O_CLOEXEC | os.O_NOFOLLOW,
                        mode,
                        dir_fd=parent_fd,
                    )
                except OSError as exc:
                    raise self._translate_open_error(exc) from exc
            except OSError as exc:
                if isinstance(exc, PathAccessError):
                    raise
                raise self._translate_open_error(exc) from exc
            finally:
                os.close(parent_fd)

        try:
            file_stat = os.fstat(file_fd)
            if not stat.S_ISREG(file_stat.st_mode):
                os.close(file_fd)
                raise PathAccessError("NOT_REGULAR_FILE", "Requested path is not a regular file.")
            return file_fd
        except OSError as exc:
            os.close(file_fd)
            raise PathAccessError("INVALID_PATH", "Requested file could not be safely opened.") from exc

    def check_path(
        self,
        path: str | Path,
        *,
        operation: PathOperation = "read",
        allow_missing: bool = False,
    ) -> bool:
        """Reusable fail-closed permission check for file tools."""
        self.authorize_path(path, operation=operation, allow_missing=allow_missing)
        return True

    def close(self) -> None:
        """Close pinned allowed-root descriptors; safe to call more than once."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            for fd in self._root_fds.values():
                try:
                    os.close(fd)
                except OSError:
                    pass
            self._root_fds.clear()

    def __enter__(self) -> PathGuard:
        if self._closed:
            raise PathAccessError("CLOSED", "Filesystem authorization is closed.")
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _ensure_allowed(self, resolved: Path) -> None:
        if not any(resolved.is_relative_to(root) for root in self._roots):
            raise PathAccessError(
                "OUTSIDE_ALLOWED_ROOT",
                "Requested path is outside the configured filesystem roots.",
            )

    def _ensure_not_protected(self, candidate: Path, resolved: Path) -> None:
        if self._allow_sensitive:
            return
        paths_to_check = (candidate, resolved)
        if any(
            path.is_relative_to(protected)
            for path in paths_to_check
            for protected in self._protected_paths
        ):
            raise PathAccessError(
                "PROTECTED_PATH",
                "Requested path is protected by filesystem policy.",
            )
        if any(_is_sensitive_path(path) for path in paths_to_check):
            raise PathAccessError(
                "PROTECTED_PATH",
                "Requested path is protected by filesystem policy.",
            )
        if any(
            path.is_relative_to(system_path)
            for path in paths_to_check
            for system_path in _SYSTEM_PATHS
        ):
            raise PathAccessError(
                "PROTECTED_PATH",
                "System directories are protected by filesystem policy.",
            )

    def _find_lexical_root(self, candidate: Path) -> Path:
        for root in self._roots:
            if candidate.is_relative_to(root):
                return root
        raise PathAccessError(
            "OUTSIDE_ALLOWED_ROOT",
            "Requested path is not lexically beneath an allowed root.",
        )

    @staticmethod
    def _translate_open_error(exc: OSError) -> PathAccessError:
        if exc.errno in {errno.ELOOP, errno.EMLINK}:
            return PathAccessError(
                "SYMLINK_NOT_ALLOWED",
                "Symlinks are not allowed in securely opened paths.",
            )
        if exc.errno == errno.ENOENT:
            return PathAccessError("NOT_FOUND", "Requested path does not exist.")
        if exc.errno in {errno.EACCES, errno.EPERM}:
            return PathAccessError("PERMISSION_DENIED", "Filesystem access was denied.")
        if exc.errno == errno.ENOTDIR:
            return PathAccessError("INVALID_PATH", "A path component is not a directory.")
        return PathAccessError("OPEN_FAILED", "Requested file could not be safely opened.")


def _resolve_configured_root(configured_root: str | Path) -> Path:
    raw = Path(configured_root).expanduser()
    if not raw.is_absolute():
        raise PathAccessError("INVALID_ROOT", "Allowed roots must be absolute paths.")
    try:
        root = raw.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise PathAccessError("INVALID_ROOT", "An allowed root cannot be resolved.") from exc
    if not root.is_dir():
        raise PathAccessError("INVALID_ROOT", "An allowed root must be a directory.")
    if root == Path(root.anchor):
        raise PathAccessError("INVALID_ROOT", "Filesystem root cannot be an allowed root.")
    return root


def _resolve_protected_path(path: Path) -> Path:
    if not path.is_absolute():
        path = Path.home() / path
    try:
        return path.resolve(strict=False)
    except (OSError, RuntimeError):
        return path.absolute()


def _candidate_path(path: str | Path) -> Path:
    if not isinstance(path, (str, Path)) or not str(path).strip():
        raise PathAccessError("INVALID_PATH", "Requested path is empty or invalid.")
    raw = Path(path).expanduser()
    if ".." in raw.parts:
        raise PathAccessError("PATH_TRAVERSAL", "Path traversal is not allowed.")
    try:
        return Path(os.path.abspath(raw))
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise PathAccessError("INVALID_PATH", "Requested path is invalid.") from exc


def _resolve_destination(candidate: Path) -> Path:
    if candidate.exists() or candidate.is_symlink():
        return candidate.resolve(strict=True)
    parent = candidate.parent.resolve(strict=True)
    return parent / candidate.name


def _is_sensitive_path(path: Path) -> bool:
    parts = tuple(part.casefold() for part in path.parts)
    if any(part == component.casefold() for part in parts for component in _SENSITIVE_COMPONENTS):
        return True
    filename = path.name.casefold()
    if filename in _SENSITIVE_FILENAMES:
        return True
    if filename == ".env" or filename.startswith(".env."):
        return True
    if filename.startswith("id_") or filename.endswith(
        (".pem", ".key", ".p12", ".pfx", ".ppk", "_rsa", "_ed25519", "_ecdsa", "_dsa")
    ):
        return True
    browser_markers = {
        "google-chrome",
        "chromium",
        "microsoft-edge",
        "bravesoftware",
        "opera",
        "vivaldi",
        "firefox",
    }
    return any(part in browser_markers for part in parts)
