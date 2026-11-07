"""Private filesystem primitives used by the Hermes ReMe integration.

These helpers keep the provider's durable markers and journals fail-closed
when filesystem identity, permissions, or durability guarantees are unclear.
The runtime mapping is only a compatibility seam for the provider module's
existing test hooks; production calls use this module's normal bindings.
"""

from __future__ import annotations

import os
import stat
import sys
import threading
from pathlib import Path
from typing import Any, MutableMapping

try:
    import fcntl
except ImportError:  # pragma: no cover - exercised on Windows
    fcntl = None

try:
    import msvcrt
except ImportError:  # pragma: no cover - exercised on POSIX
    msvcrt = None

_RUNTIME: MutableMapping[str, Any] | None = None
_HELD_PROFILE_LOCKS: set[str] = set()
_HELD_PROFILE_LOCKS_LOCK = threading.Lock()


def configure_runtime(runtime: MutableMapping[str, Any]) -> None:
    """Expose the provider module's bindings for its established test hooks."""
    global _RUNTIME
    _RUNTIME = runtime


def _runtime_os() -> Any:
    if _RUNTIME is not None:
        return _RUNTIME.get("os", os)
    return os


def _platform_is_windows() -> bool:
    if _RUNTIME is not None:
        override = _RUNTIME.get("_is_windows_platform")
        if override is not None and override is not _is_windows_platform:
            return bool(override())
    return sys.platform == "win32"


def _is_windows_platform() -> bool:
    """Return the platform family used for filesystem mode interpretation."""
    return _platform_is_windows()


def _fsync_directory(path: Path) -> None:
    """Flush a directory entry after creating or replacing the profile spool."""
    runtime_os = _runtime_os()
    if runtime_os.name == "nt":
        # Windows does not expose a portable fsync-able directory handle.
        # The file itself is flushed before publication; never substitute an
        # unsafe rename when directory metadata cannot be flushed.
        return
    fsync = getattr(runtime_os, "fsync", None)
    if fsync is None:
        raise OSError("directory fsync is unsupported")
    fd = runtime_os.open(path, runtime_os.O_RDONLY)
    try:
        fsync(fd)
    finally:
        runtime_os.close(fd)


def _safe_staging_mode(mode: int) -> bool:
    """Accept only private marker modes, including Windows' reduced mode semantics."""
    mode_bits = stat.S_IMODE(mode)
    if not _platform_is_windows():
        return mode_bits == 0o600
    # Windows may report the mkstemp file as normal/read-only (0666/0444),
    # while 0600 is also produced by Python builds that expose POSIX modes.
    return mode_bits in {0o444, 0o600, 0o666}


def _set_private_file_mode(path: Path, fd: int) -> None:
    """Apply the strongest portable private-file mode for the current platform."""
    runtime_os = _runtime_os()
    if _platform_is_windows():
        chmod = getattr(runtime_os, "chmod", None)
        if chmod is None:
            raise OSError("private file mode is unsupported on Windows")
        try:
            # Python 3.11's Windows chmod does not implement follow_symlinks.
            # The descriptor is retained from mkstemp/exclusive creation while
            # the path-based operation is guarded by identity and reparse checks.
            before = _verify_private_file_identity(path, fd, require_single_link=True)
            chmod(path, stat.S_IREAD | stat.S_IWRITE)
            _verify_private_file_identity(path, fd, expected_stat=before)
        except (NotImplementedError, OSError, TypeError) as exc:
            raise OSError("private file mode is unsupported on Windows") from exc
        return
    fchmod = getattr(runtime_os, "fchmod", None)
    if fchmod is None:
        raise OSError("private file mode is unsupported")
    try:
        fchmod(fd, 0o600)
    except (NotImplementedError, OSError) as exc:
        raise OSError("private file mode could not be applied") from exc


def _fsync_file(fd: int) -> None:
    """Flush an open file or fail closed when file durability is unavailable."""
    fsync = getattr(_runtime_os(), "fsync", None)
    if fsync is None:
        raise OSError("file fsync is unsupported")
    fsync(fd)


def _is_reparse_point(path: Path, file_stat: os.stat_result) -> bool:
    """Detect Windows reparse points even on Python versions without is_junction."""
    if not _platform_is_windows():
        return False
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if getattr(file_stat, "st_file_attributes", 0) & reparse_flag:
        return True
    is_junction = getattr(path, "is_junction", None)
    if is_junction is None:
        return False
    try:
        return bool(is_junction())
    except OSError:
        return True


def _runtime_reparse_point(path: Path, file_stat: os.stat_result) -> bool:
    if _RUNTIME is not None:
        override = _RUNTIME.get("_is_reparse_point")
        if override is not None and override is not _is_reparse_point:
            return bool(override(path, file_stat))
    return _is_reparse_point(path, file_stat)


def _is_unsafe_link(path: Path, file_stat: os.stat_result) -> bool:
    """Return whether a path entry is a symlink or Windows reparse point."""
    return stat.S_ISLNK(file_stat.st_mode) or _runtime_reparse_point(path, file_stat)


def _safe_existing_path_components(path: Path, *, trusted_root: Path | None = None) -> bool:
    """Reject unsafe existing components below the canonical profile root.

    Missing components are allowed so normal profile creation can proceed. The
    caller must run this check again after creating them and immediately before
    each filesystem operation that can follow the path.
    """
    candidate = Path(path)
    root = Path(trusted_root) if trusted_root is not None else None
    if not candidate.is_absolute() or (root is not None and not root.is_absolute()):
        return False
    if root is None:
        root = candidate.parent.parent
    try:
        relative_parts = candidate.relative_to(root).parts
    except ValueError:
        return False
    current = root
    for part in relative_parts:
        current /= part
        try:
            current_stat = current.lstat()
        except FileNotFoundError:
            return True
        except OSError:
            return False
        if _is_unsafe_link(current, current_stat):
            return False
    return True


def _same_file_identity(first: os.stat_result, second: os.stat_result) -> bool:
    """Return whether two stat results identify the same device and inode."""
    return first.st_dev == second.st_dev and first.st_ino == second.st_ino


def _owned_by_current_user(file_stat: os.stat_result, current_uid: int | None) -> bool:
    """Return whether a stat result belongs to the current user when supported."""
    return current_uid is None or file_stat.st_uid == current_uid


def _verify_private_file_identity(
    path: Path,
    fd: int,
    *,
    expected_stat: os.stat_result | None = None,
    require_single_link: bool = False,
) -> os.stat_result:
    """Verify that a path and open descriptor still name the same private file."""
    runtime_os = _runtime_os()
    path_stat = path.lstat()
    fd_stat = runtime_os.fstat(fd)
    current_uid = runtime_os.getuid() if hasattr(runtime_os, "getuid") else None
    if _is_unsafe_link(path, path_stat) or _is_unsafe_link(path, fd_stat):
        raise OSError("private file target is ambiguous")
    if not stat.S_ISREG(path_stat.st_mode) or not stat.S_ISREG(fd_stat.st_mode):
        raise OSError("private file target is ambiguous")
    if not _same_file_identity(path_stat, fd_stat):
        raise OSError("private file target is ambiguous")
    if expected_stat is not None and not _same_file_identity(path_stat, expected_stat):
        raise OSError("private file target is ambiguous")
    if expected_stat is not None and not _same_file_identity(fd_stat, expected_stat):
        raise OSError("private file target is ambiguous")
    if require_single_link and path_stat.st_nlink != 1:
        raise OSError("private file target is ambiguous")
    if not _owned_by_current_user(path_stat, current_uid) or not _owned_by_current_user(fd_stat, current_uid):
        raise OSError("private file target is ambiguous")
    if not _safe_staging_mode(path_stat.st_mode) or not _safe_staging_mode(fd_stat.st_mode):
        raise OSError("private file target is ambiguous")
    return path_stat


class ProfileWriterLock:
    """Own one Hermes profile's ReMe spool for the lifetime of a provider.

    The lock file is intentionally retained after release. Ownership is the OS
    lock, not the presence or age of this file, so a killed process cannot
    leave a stale marker that blocks future recovery.
    """

    def __init__(self, path: Path, *, trusted_root: Path | None = None):
        self.path = path
        self.trusted_root = trusted_root or path.parent.parent
        self._fd: int | None = None
        self._key = _runtime_os().path.normcase(str(path.resolve(strict=False)))

    def acquire(self) -> None:
        """Acquire exclusive process and OS ownership of this profile journal."""
        runtime_os = _runtime_os()
        with _HELD_PROFILE_LOCKS_LOCK:
            if self._key in _HELD_PROFILE_LOCKS:
                raise OSError("profile writer is already owned by this process")
            if not _safe_existing_path_components(self.path.parent, trusted_root=self.trusted_root):
                raise OSError(f"Refusing unsafe profile writer lock path: {self.path}")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if not _safe_existing_path_components(self.path.parent, trusted_root=self.trusted_root):
                raise OSError(f"Refusing unsafe profile writer lock path: {self.path}")
            try:
                lock_stat = self.path.lstat()
            except FileNotFoundError:
                lock_stat = None
            if lock_stat is not None and _is_unsafe_link(self.path, lock_stat):
                raise OSError(f"Refusing unsafe profile writer lock path: {self.path}")
            flags = runtime_os.O_RDWR | runtime_os.O_CREAT
            if hasattr(runtime_os, "O_NOFOLLOW"):
                flags |= runtime_os.O_NOFOLLOW
            fd = runtime_os.open(self.path, flags, 0o600)
            try:
                if not _safe_existing_path_components(self.path.parent, trusted_root=self.trusted_root):
                    raise OSError(f"Refusing unsafe profile writer lock path: {self.path}")
                _set_private_file_mode(self.path, fd)
                if fcntl is not None:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                elif msvcrt is not None:
                    if runtime_os.fstat(fd).st_size == 0:
                        runtime_os.write(fd, b"\0")
                    runtime_os.lseek(fd, 0, runtime_os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:  # pragma: no cover - no supported platform has neither
                    raise OSError("no supported OS file-lock implementation")
            except Exception:
                runtime_os.close(fd)
                raise
            self._fd = fd
            _HELD_PROFILE_LOCKS.add(self._key)

    def release(self) -> None:
        """Release the operating-system lock and local ownership registration."""
        runtime_os = _runtime_os()
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
            elif msvcrt is not None:
                runtime_os.lseek(fd, 0, runtime_os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        finally:
            runtime_os.close(fd)
            with _HELD_PROFILE_LOCKS_LOCK:
                _HELD_PROFILE_LOCKS.discard(self._key)
