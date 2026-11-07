"""Tests for default-deny path authorization and race-resistant file opening."""

import os
from pathlib import Path

import pytest

from security.path_guard import PathAccessError, PathGuard


@pytest.fixture
def allowed_root(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    return root


def test_authorizes_existing_path_inside_explicit_root(allowed_root: Path) -> None:
    target = allowed_root / "notes.txt"
    target.write_text("hello", encoding="utf-8")
    guard = PathGuard([allowed_root])

    try:
        assert guard.authorize_path(target) == target
        assert guard.check_path(target)
    finally:
        guard.close()


def test_denies_external_path(allowed_root: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    guard = PathGuard([allowed_root])

    try:
        with pytest.raises(PathAccessError) as error:
            guard.authorize_path(outside)
        assert error.value.code == "OUTSIDE_ALLOWED_ROOT"
    finally:
        guard.close()


def test_denies_path_traversal_even_if_normalization_would_remain_inside(
    allowed_root: Path,
) -> None:
    guard = PathGuard([allowed_root])

    try:
        with pytest.raises(PathAccessError) as error:
            guard.authorize_path(allowed_root / "subdir" / ".." / "notes.txt")
        assert error.value.code == "PATH_TRAVERSAL"
    finally:
        guard.close()


def test_denies_symlink_escape_from_allowed_root(
    allowed_root: Path,
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    link = allowed_root / "outside-link"
    link.symlink_to(outside)
    guard = PathGuard([allowed_root])

    try:
        with pytest.raises(PathAccessError) as error:
            guard.authorize_path(link)
        assert error.value.code == "OUTSIDE_ALLOWED_ROOT"
        with pytest.raises(PathAccessError):
            guard.open_file(link)
    finally:
        guard.close()


def test_rejects_nonexistent_target_by_default(allowed_root: Path) -> None:
    guard = PathGuard([allowed_root])

    try:
        with pytest.raises(PathAccessError) as error:
            guard.authorize_path(allowed_root / "missing.txt")
        assert error.value.code == "NOT_FOUND"
    finally:
        guard.close()


def test_allows_creation_target_only_when_explicitly_requested(
    allowed_root: Path,
) -> None:
    target = allowed_root / "new.txt"
    guard = PathGuard([allowed_root])

    try:
        assert guard.authorize_path(
            target,
            operation="write",
            allow_missing=True,
        ) == target
        fd = guard.open_file(
            target,
            flags=os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            operation="write",
        )
        try:
            os.write(fd, b"created safely")
        finally:
            os.close(fd)
        assert target.read_text(encoding="utf-8") == "created safely"
    finally:
        guard.close()


def test_denies_sensitive_file_even_inside_allowed_root(allowed_root: Path) -> None:
    target = allowed_root / ".env"
    target.write_text("TOKEN=value", encoding="utf-8")
    guard = PathGuard([allowed_root])

    try:
        with pytest.raises(PathAccessError) as error:
            guard.authorize_path(target)
        assert error.value.code == "PROTECTED_PATH"
        with pytest.raises(PathAccessError):
            guard.open_file(target)
    finally:
        guard.close()


@pytest.mark.parametrize(
    "relative_path",
    [
        ".ssh/id_ed25519",
        ".aws/credentials",
        ".config/credentials/account.json",
        ".config/chromium/Default/Login Data",
        "credentials.json",
    ],
)
def test_denies_sensitive_locations_inside_allowed_root(
    allowed_root: Path,
    relative_path: str,
) -> None:
    target = allowed_root / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("sensitive", encoding="utf-8")
    guard = PathGuard([allowed_root])

    try:
        with pytest.raises(PathAccessError) as error:
            guard.authorize_path(target)
        assert error.value.code == "PROTECTED_PATH"
        with pytest.raises(PathAccessError):
            guard.open_file(target)
    finally:
        guard.close()


def test_sensitive_path_requires_explicit_policy_and_root(
    allowed_root: Path,
) -> None:
    target = allowed_root / ".env"
    target.write_text("TOKEN=value", encoding="utf-8")
    guard = PathGuard([allowed_root], allow_sensitive=True)

    try:
        assert guard.authorize_path(target) == target
    finally:
        guard.close()


def test_denies_system_path_even_when_configured_as_root() -> None:
    system_root = Path("/etc")
    target = system_root / "hosts"
    if not target.is_file():
        pytest.skip("/etc/hosts is not available on this system")
    guard = PathGuard([system_root])

    try:
        with pytest.raises(PathAccessError) as error:
            guard.authorize_path(target)
        assert error.value.code == "PROTECTED_PATH"
    finally:
        guard.close()


def test_home_is_not_implicitly_authorized(tmp_path: Path) -> None:
    guard = PathGuard()

    try:
        with pytest.raises(PathAccessError) as error:
            guard.authorize_path(tmp_path)
        assert error.value.code == "NO_ALLOWED_ROOTS"
    finally:
        guard.close()


def test_filesystem_root_cannot_be_configured_as_allowed_root() -> None:
    with pytest.raises(PathAccessError) as error:
        PathGuard([Path("/")])
    assert error.value.code == "INVALID_ROOT"


def test_secure_open_rejects_symlink_swapped_after_authorization(
    allowed_root: Path,
    tmp_path: Path,
) -> None:
    target = allowed_root / "item.txt"
    target.write_text("safe", encoding="utf-8")
    outside = tmp_path / "secret.txt"
    outside.write_text("secret", encoding="utf-8")
    guard = PathGuard([allowed_root])
    original_authorize = guard.authorize_path

    def authorize_then_swap(
        path: str | Path,
        *,
        operation: str = "read",
        allow_missing: bool = False,
    ) -> Path:
        resolved = original_authorize(
            path,
            operation=operation,  # type: ignore[arg-type]
            allow_missing=allow_missing,
        )
        target.unlink()
        target.symlink_to(outside)
        return resolved

    guard.authorize_path = authorize_then_swap  # type: ignore[method-assign]
    try:
        with pytest.raises(PathAccessError) as error:
            guard.open_file(target)
        assert error.value.code == "SYMLINK_NOT_ALLOWED"
        assert outside.read_text(encoding="utf-8") == "secret"
    finally:
        guard.close()
