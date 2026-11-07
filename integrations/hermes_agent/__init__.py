"""Hermes Agent memory provider backed by HTTP or an embedded ReMe SDK."""

# Keep the existing security-sensitive journal state machine intact in Stage 2A.
# Follow-up modularisation is separate from this source-only safety correction.
# pylint: disable=C0302,R0911

from __future__ import annotations

import atexit
import importlib.util
import json
import logging
import os
import queue
import stat
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider, RecallStatus

from .backend import ReMeBackend, ReMeBackendError, spawn_profile_thread
from .config import (
    ReMeConfig,
    ReMeConfigError,
    STAGE1_CONFIG_NAME,
    _validate_embedded_policy,
    load_config,
    save_config,
)
from .embedded_backend import EmbeddedReMeBackend
from .http_backend import HttpReMeBackend
from .retry_policy import classify_write_failure as _classify_write_failure
from .retry_policy import is_permanent_write_failure as _is_permanent_write_failure  # noqa: F401
from .turn_payload import build_turn_payload as _build_turn_payload
from .turn_payload import upgrade_legacy_payload as _upgrade_legacy_payload
from .turn_payload import valid_pending_record as _valid_pending_record
from .turn_payload import (
    MAX_JOURNAL_BYTES as _MAX_JOURNAL_BYTES,
    MAX_JOURNAL_LINE_BYTES as _MAX_JOURNAL_LINE_BYTES,
    MAX_JOURNAL_RECORDS as _MAX_JOURNAL_RECORDS,
    MAX_RECORD_JSON_BYTES as _MAX_RECORD_JSON_BYTES,
)
from .turn_payload import scoped_session_id
from .write_queue import RetryWakeup, rebuild_write_queue_locked
from .durability import (
    ProfileWriterLock as _ProfileWriterLock,
    _fsync_directory as _durability_fsync_directory,
    _fsync_file as _durability_fsync_file,
    _is_reparse_point as _durability_is_reparse_point,
    _safe_existing_path_components as _durability_safe_existing_path_components,
    _is_unsafe_link as _durability_is_unsafe_link,
    _owned_by_current_user as _durability_owned_by_current_user,
    _safe_staging_mode as _durability_safe_staging_mode,
    _same_file_identity as _durability_same_file_identity,
    _set_private_file_mode as _durability_set_private_file_mode,
    _verify_private_file_identity as _durability_verify_private_file_identity,
    configure_runtime as _configure_durability_runtime,
)

_scoped_session_id = scoped_session_id

logger = logging.getLogger(__name__)
_SDK_INSTALL_HINT = 'Embedded ReMe mode requires the SDK. Install it with: pip install "reme-ai[core]"'
_PENDING_QUEUE_DIRECTORY = "reme"
_PENDING_QUEUE_FILENAME = "pending-writes.jsonl"
_QUARANTINE_QUEUE_FILENAME = "quarantined-writes.jsonl"
_PROFILE_LOCK_FILENAME = ".writer.lock"
_WORKSPACE_OWNER_FILENAME = ".hermes-reme-owner.json"
_WORKSPACE_OWNER_TEMP_PREFIX = f".{_WORKSPACE_OWNER_FILENAME}."


def _is_windows_platform() -> bool:
    """Return the platform family used for filesystem mode interpretation."""
    return sys.platform == "win32"


_fsync_directory = _durability_fsync_directory
_fsync_file = _durability_fsync_file
_is_reparse_point = _durability_is_reparse_point
_safe_existing_path_components = _durability_safe_existing_path_components
_is_unsafe_link = _durability_is_unsafe_link
_owned_by_current_user = _durability_owned_by_current_user
_safe_staging_mode = _durability_safe_staging_mode
_same_file_identity = _durability_same_file_identity
_set_private_file_mode = _durability_set_private_file_mode
_verify_private_file_identity = _durability_verify_private_file_identity
_configure_durability_runtime(globals())


class _WorkspaceOwnerConflict(OSError):
    """The workspace marker belongs to a different Hermes profile."""


class _WorkspaceOwnerMarker:
    """Durably bind an embedded workspace to one canonical Hermes profile.

    The marker is intentionally not removed on shutdown.  It records the
    workspace owner across crashes and restarts; the existing profile writer
    lock still handles concurrent processes of the same profile.
    """

    def __init__(self, workspace: str | Path, profile_home: str | Path):
        self.workspace = Path(workspace)
        self.profile_home = str(Path(profile_home).expanduser().resolve(strict=False))
        self.path = self.workspace / _WORKSPACE_OWNER_FILENAME
        self._owned = False

    @staticmethod
    def _reject_symlink_components(path: Path) -> None:
        if not path.is_absolute() or ".." in path.parts:
            raise OSError("unsafe workspace path")
        current = Path(path.anchor)
        for part in path.parts[1:]:
            current /= part
            try:
                current_stat = current.lstat()
                if _is_unsafe_link(current, current_stat):
                    raise OSError("unsafe workspace path")
            except FileNotFoundError:
                continue
            except OSError as exc:
                if str(exc) == "unsafe workspace path":
                    raise
                raise OSError("workspace path could not be inspected") from exc

    def _prepare_workspace(self) -> None:
        self._reject_symlink_components(self.workspace)
        try:
            self.workspace.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise OSError("workspace is not writable") from exc
        self._reject_symlink_components(self.workspace)
        try:
            workspace_stat = self.workspace.stat()
        except OSError as exc:
            raise OSError("workspace could not be inspected") from exc
        if not stat.S_ISDIR(workspace_stat.st_mode) or not self.workspace.name:
            raise OSError("workspace must be a named directory")

    def _read_existing(self) -> None:
        try:
            marker_stat = self.path.lstat()
        except OSError as exc:
            raise OSError("workspace owner marker could not be inspected") from exc
        if stat.S_ISLNK(marker_stat.st_mode) or not stat.S_ISREG(marker_stat.st_mode):
            raise OSError("workspace owner marker is not a regular file")
        if _is_unsafe_link(self.path, marker_stat) or not _safe_staging_mode(marker_stat.st_mode):
            raise OSError("workspace owner marker is not private")

        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(self.path, flags)
        except OSError as exc:
            raise OSError("workspace owner marker could not be opened") from exc
        try:
            current_stat = os.fstat(fd)
            if (
                _is_unsafe_link(self.path, current_stat)
                or not stat.S_ISREG(current_stat.st_mode)
                or not _safe_staging_mode(current_stat.st_mode)
                or current_stat.st_dev != marker_stat.st_dev
                or current_stat.st_ino != marker_stat.st_ino
            ):
                raise OSError("workspace owner marker is ambiguous")
            raw = os.read(fd, 4097)
        finally:
            os.close(fd)
        if len(raw) > 4096:
            raise OSError("workspace owner marker is invalid")
        try:
            marker = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise OSError("workspace owner marker is invalid") from exc
        if not isinstance(marker, dict):
            raise OSError("workspace owner marker is invalid")
        if (
            marker.get("format") != 1
            or marker.get("owner") != self.profile_home
            or marker.get("workspace") != str(self.workspace)
        ):
            raise _WorkspaceOwnerConflict("workspace belongs to another Hermes profile")

    def acquire(self) -> None:
        """Create the owner marker atomically or validate its existing owner."""
        self._prepare_workspace()
        marker = {
            "format": 1,
            "owner": self.profile_home,
            "workspace": str(self.workspace),
        }
        encoded = (json.dumps(marker, ensure_ascii=True, sort_keys=True) + "\n").encode("utf-8")
        # Do not silently claim a workspace containing another identity's
        # historical data. Existing workspaces require an explicit migration.
        try:
            self.path.lstat()
        except FileNotFoundError:
            marker_exists = False
        except OSError as exc:
            raise OSError("workspace owner marker could not be inspected") from exc
        else:
            marker_exists = True
        if not marker_exists:
            self._remove_safe_staging_files(encoded)
            if any(self.workspace.iterdir()):
                raise OSError("existing workspace is unowned; explicit migration required")

        # Publishing the final name with O_EXCL before writing it leaves a
        # corrupt marker behind if the process dies during the write. A
        # private, durable temporary file plus a hard link publishes the
        # completed bytes atomically and cannot overwrite a competing marker.
        temp_name: str | None = None
        try:
            fd, temp_name = tempfile.mkstemp(
                prefix=_WORKSPACE_OWNER_TEMP_PREFIX,
                dir=self.workspace,
            )
            try:
                _set_private_file_mode(Path(temp_name), fd)
                with os.fdopen(fd, "wb") as handle:
                    fd = -1
                    handle.write(encoded)
                    handle.flush()
                    _fsync_file(handle.fileno())
                    temp_path = Path(temp_name)
                    before_link = _verify_private_file_identity(
                        temp_path,
                        handle.fileno(),
                        require_single_link=True,
                    )
                    try:
                        if _is_windows_platform():
                            # Windows Python 3.11 exposes CreateHardLink through
                            # os.link but rejects the follow_symlinks keyword.
                            os.link(temp_path, self.path)
                        else:
                            os.link(temp_path, self.path, follow_symlinks=False)
                    except FileExistsError:
                        self._read_existing()
                        self._owned = True
                        return
                    _verify_private_file_identity(temp_path, handle.fileno(), expected_stat=before_link)
                    _verify_private_file_identity(self.path, handle.fileno(), expected_stat=before_link)
                    _fsync_directory(self.workspace)
            finally:
                if fd >= 0:
                    os.close(fd)
        except _WorkspaceOwnerConflict:
            raise
        except OSError as exc:
            if str(exc).startswith("workspace owner marker"):
                raise
            raise OSError("workspace owner marker could not be made durable") from exc
        finally:
            if temp_name is not None:
                try:
                    os.unlink(temp_name)
                except FileNotFoundError:
                    pass
        self._owned = True

    def _remove_safe_staging_files(self, encoded: bytes) -> None:
        """Remove only verified leftovers from an interrupted marker publish."""
        staging_paths: list[Path] = []
        try:
            entries = tuple(self.workspace.iterdir())
        except OSError as exc:
            raise OSError("workspace contents could not be inspected") from exc
        current_uid = os.getuid() if hasattr(os, "getuid") else None
        for entry in entries:
            if not entry.name.startswith(_WORKSPACE_OWNER_TEMP_PREFIX):
                raise OSError("existing workspace is unowned; explicit migration required")
            try:
                entry_stat = entry.lstat()
            except OSError as exc:
                raise OSError("workspace owner staging file could not be inspected") from exc
            # Keep every fail-closed identity check explicit; these checks jointly prove that
            # the staging file is safe to remove after an interrupted marker publish.
            if (  # pylint: disable=too-many-boolean-expressions
                _is_unsafe_link(entry, entry_stat)
                or not stat.S_ISREG(entry_stat.st_mode)
                or not _safe_staging_mode(entry_stat.st_mode)
                or entry_stat.st_nlink != 1
                or (current_uid is not None and entry_stat.st_uid != current_uid)
            ):
                raise OSError("workspace owner staging file is ambiguous")
            flags = os.O_RDONLY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            try:
                fd = os.open(entry, flags)
            except OSError as exc:
                raise OSError("workspace owner staging file could not be opened") from exc
            try:
                opened_stat = os.fstat(fd)
                if (
                    _is_unsafe_link(entry, opened_stat)
                    or not stat.S_ISREG(opened_stat.st_mode)
                    or opened_stat.st_dev != entry_stat.st_dev
                    or opened_stat.st_ino != entry_stat.st_ino
                    or opened_stat.st_size != len(encoded)
                ):
                    raise OSError("workspace owner staging file is ambiguous")
                if os.read(fd, len(encoded) + 1) != encoded:
                    raise OSError("workspace owner staging file is ambiguous")
            finally:
                os.close(fd)
            staging_paths.append(entry)
        for entry in staging_paths:
            try:
                entry.unlink()
            except OSError as exc:
                raise OSError("workspace owner staging file could not be removed") from exc
        if staging_paths:
            try:
                _fsync_directory(self.workspace)
            except OSError as exc:
                raise OSError("workspace owner staging cleanup could not be made durable") from exc

    def release(self) -> None:
        """Release this provider's in-memory reference without deleting ownership."""
        self._owned = False


def _backend_for(config: ReMeConfig) -> ReMeBackend:
    if config.mode == "embedded":
        return EmbeddedReMeBackend(
            config.workspace_dir,
            reme_config=config.reme_config,
            start_timeout=config.request_timeout,
        )
    return HttpReMeBackend(config.endpoint, request_timeout=config.request_timeout)


class ReMeMemoryProvider(MemoryProvider):
    """Use ReMe for automatic cross-session recall and recording in Hermes."""

    def __init__(self) -> None:
        defaults = ReMeConfig()
        self._backend: ReMeBackend | None = None
        self._config: ReMeConfig | None = None
        self._backend_label = defaults.endpoint
        self._recall_timeout = defaults.recall_timeout
        self._health_timeout = defaults.health_timeout
        self._health_retry_seconds = defaults.health_retry_seconds
        self._shutdown_timeout = defaults.shutdown_timeout
        self._request_timeout = defaults.request_timeout
        self._recall_limit = defaults.recall_limit
        self._backend_available = False
        self._next_health_probe = 0.0
        self._next_recall_attempt = 0.0
        self._next_write_attempt = 0.0
        self._last_write_failure_kind = ""
        self._session_id = ""
        self._profile_id = "default"
        self._write_enabled = True
        self._accept_writes = True
        self._write_queue: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self._queued_record_ids: set[str] = set()
        self._write_thread: threading.Thread | None = None
        self._write_thread_lock = threading.RLock()
        self._pending_lock = threading.Lock()
        self._retry_timers: set[threading.Timer] = set()
        self._retry_wakeup = RetryWakeup(self)
        self._retry_wakeup_pending, self._writer_starting, self._pending_generation = False, False, 0
        self._pending_path: Path | None = None
        self._profile_root: Path | None = None
        self._legacy_created_at_by_record: dict[tuple[Path, str], str] = {}
        self._profile_lock: _ProfileWriterLock | None = None
        self._workspace_owner: _WorkspaceOwnerMarker | None = None
        self._profile_lock_required = False
        self._backend_lock = threading.RLock()
        self._shutdown_started = False
        self._deferred_backend_close = False
        self._backend_cleanup_pending = False
        self._atexit_registered = False
        self._recall_status: RecallStatus | None = None
        self._unavailable_reason = ""
        self._journal_state = "READY"

    @property
    def name(self) -> str:
        """Return the provider identifier used by Hermes configuration."""
        return "reme"

    def is_available(self) -> bool:
        """Check configuration and local dependencies without network or writes."""
        try:
            config = load_config()
            if config.mode == "embedded" and _is_windows_platform():
                self._unavailable_reason = (
                    "embedded ReMe Option B requires POSIX durable filesystem primitives; Windows is unsupported"
                )
                return False
            if config.mode == "embedded" and importlib.util.find_spec("reme") is None:
                self._unavailable_reason = _SDK_INSTALL_HINT
                return False
            _backend_for(config)
        except (ReMeConfigError, TypeError, ValueError, OSError) as exc:
            self._unavailable_reason = str(exc)
            return False
        self._unavailable_reason = ""
        return True

    def unavailable_reason(self) -> str:
        """Return the last local availability failure as user-facing guidance."""
        return self._unavailable_reason

    # pylint: disable-next=too-many-statements
    def initialize(self, session_id: str, **kwargs: Any) -> None:
        """Load profile config and start the selected backend best-effort."""
        with self._write_thread_lock:
            if self._write_thread is not None and self._write_thread.is_alive():
                raise RuntimeError(
                    "Cannot reinitialize ReMe while its previous writer is still running",
                )
        hermes_home = str(kwargs.get("hermes_home") or "") or None
        if hermes_home is None:
            self._disable_without_profile(session_id, kwargs.get("agent_identity"))
            return
        profile_home = Path(hermes_home).expanduser().resolve()
        try:
            config = load_config(hermes_home)
        except ReMeConfigError as exc:
            with self._backend_lock:
                if not self._close_backend_locked():
                    self._accept_writes = False
                    self._unavailable_reason = "previous ReMe backend cleanup is still pending"
                    return
            self._release_profile_lock()
            self._release_workspace_owner()
            self._profile_lock_required = False
            self._profile_root = profile_home
            self._config = None
            self._backend_label = "invalid configuration"
            self._session_id = str(session_id or "")
            self._profile_id = str(kwargs.get("agent_identity") or "default")
            self._accept_writes = False
            self._profile_lock_required = True
            self._pending_path = profile_home / _PENDING_QUEUE_DIRECTORY / _PENDING_QUEUE_FILENAME
            self._recall_status = None
            self._unavailable_reason = str(exc)
            logger.warning("ReMe provider configuration is invalid: %s", exc)
            return

        with self._backend_lock:
            if not self._close_backend_locked():
                self._accept_writes = False
                self._unavailable_reason = "previous ReMe backend cleanup is still pending"
                return
        self._release_profile_lock()
        self._release_workspace_owner()
        self._profile_lock_required = False
        self._profile_root = profile_home
        try:
            if config.mode == "embedded":
                if _is_windows_platform():
                    raise ReMeConfigError(
                        "Embedded ReMe Option B requires POSIX durable filesystem primitives; Windows is unsupported",
                    )
                _validate_embedded_policy(
                    config.reme_config,
                    allow_custom=config.allow_custom_config,
                )
                if config.allow_custom_config:
                    logger.warning(
                        "Embedded Hermes custom ReMe configuration is explicitly enabled; "
                        "local-only Stage 1 safeguards are bypassed",
                    )
        except ReMeConfigError as exc:
            self._config = None
            self._backend_label = "invalid embedded configuration"
            self._session_id = str(session_id or "")
            self._profile_id = str(kwargs.get("agent_identity") or "default")
            self._write_enabled = False
            self._accept_writes = False
            self._backend_available = False
            self._recall_status = None
            self._unavailable_reason = str(exc)
            logger.warning("ReMe embedded configuration is invalid: %s", exc)
            return
        self._config = config
        pending_path = profile_home / _PENDING_QUEUE_DIRECTORY / _PENDING_QUEUE_FILENAME
        if self._pending_path is not None and self._pending_path != pending_path:
            self._legacy_created_at_by_record.clear()
        self._pending_path = pending_path
        self._profile_lock_required = True
        try:
            profile_lock = _ProfileWriterLock(
                profile_home / _PENDING_QUEUE_DIRECTORY / _PROFILE_LOCK_FILENAME,
                trusted_root=profile_home,
            )
            profile_lock.acquire()
        except OSError as exc:
            self._config = None
            self._backend_label = f"profile writer unavailable: {profile_home}"
            self._session_id = str(session_id or "")
            self._profile_id = str(kwargs.get("agent_identity") or "default")
            self._write_enabled = False
            self._accept_writes = False
            self._backend_available = False
            self._recall_status = None
            self._unavailable_reason = f"profile writer lock unavailable: {exc}"
            logger.warning("ReMe provider is unavailable for %s: %s", profile_home, self._unavailable_reason)
            return
        self._profile_lock = profile_lock
        if config.mode == "embedded":
            try:
                workspace_owner = _WorkspaceOwnerMarker(config.workspace_dir, profile_home)
                workspace_owner.acquire()
            except OSError as exc:
                self._release_profile_lock()
                self._config = None
                self._backend_label = "embedded workspace unavailable"
                self._session_id = str(session_id or "")
                self._profile_id = str(kwargs.get("agent_identity") or "default")
                self._write_enabled = False
                self._accept_writes = False
                self._backend_available = False
                self._recall_status = None
                self._unavailable_reason = "embedded workspace ownership is unavailable"
                logger.warning("ReMe embedded workspace ownership is unavailable: %s", exc)
                return
            self._workspace_owner = workspace_owner
        self._unavailable_reason = ""
        self._backend_label = config.endpoint if config.mode == "http" else f"embedded:{config.workspace_dir}"
        self._recall_timeout = config.recall_timeout
        self._health_timeout = config.health_timeout
        self._health_retry_seconds = config.health_retry_seconds
        self._shutdown_timeout = config.shutdown_timeout
        self._request_timeout = config.request_timeout
        self._recall_limit = config.recall_limit
        self._session_id = str(session_id or "")
        self._profile_id = str(kwargs.get("agent_identity") or "default")
        self._write_enabled = str(kwargs.get("agent_context") or "primary") not in {
            "cron",
            "flush",
            "subagent",
        }
        self._backend = None
        self._backend_available = False
        self._next_health_probe = 0.0
        self._next_recall_attempt = 0.0
        self._next_write_attempt = 0.0
        self._last_write_failure_kind = ""
        self._accept_writes = True
        self._journal_state = "READY"
        self._write_queue = queue.Queue()
        self._queued_record_ids = set()
        self._retry_timers = set()
        self._retry_wakeup_pending, self._writer_starting, self._pending_generation = False, False, 0
        self._write_thread = None
        self._shutdown_started = False
        self._deferred_backend_close = False
        self._backend_cleanup_pending = False
        self._recall_status = None
        if not self._atexit_registered:
            atexit.register(self._atexit_shutdown)
            self._atexit_registered = True
        if not self._ensure_backend(force=True):
            logger.warning(
                "ReMe is unavailable at %s; recall and recording will retry after cooldown",
                self._backend_label,
            )
        self._start_writer_if_needed()

    def get_config_schema(self) -> List[Dict[str, Any]]:
        """Describe fields used by the terminal setup wizard."""
        defaults = ReMeConfig()
        return [
            {
                "key": "mode",
                "label": "Mode",
                "kind": "select",
                "description": "How Hermes connects to ReMe",
                "default": defaults.mode,
                "choices": ["http", "embedded"],
                "required": True,
            },
            {
                "key": "endpoint",
                "label": "HTTP endpoint",
                "description": "ReMe service URL (HTTP mode only)",
                "default": defaults.endpoint,
                "required": True,
                "when": {"mode": "http"},
            },
            {
                "key": "workspace_dir",
                "label": "Workspace directory",
                "description": "ReMe workspace (embedded mode only)",
                "default": "",
                "required": True,
                "when": {"mode": "embedded"},
            },
            {
                "key": "reme_config",
                "label": "ReMe configuration",
                "description": "Built-in config name or YAML/JSON path (embedded mode only)",
                "default": defaults.reme_config,
                "required": True,
                "when": {"mode": "embedded"},
            },
            {
                "key": "recall_limit",
                "label": "Recall limit",
                "kind": "integer",
                "description": "Maximum search results injected before a model call",
                "default": defaults.recall_limit,
                "minimum": 1,
            },
            {
                "key": "recall_timeout",
                "label": "Recall timeout (seconds)",
                "kind": "number",
                "default": defaults.recall_timeout,
                "minimum": 0.1,
            },
            {
                "key": "request_timeout",
                "label": "Write/start timeout (seconds)",
                "kind": "number",
                "default": defaults.request_timeout,
                "minimum": 0.1,
            },
            {
                "key": "health_timeout",
                "label": "Health timeout (seconds)",
                "kind": "number",
                "default": defaults.health_timeout,
                "minimum": 0.1,
            },
            {
                "key": "health_retry_seconds",
                "label": "Health retry delay (seconds)",
                "kind": "number",
                "default": defaults.health_retry_seconds,
                "minimum": 0.1,
            },
            {
                "key": "shutdown_timeout",
                "label": "Shutdown timeout (seconds)",
                "kind": "number",
                "default": defaults.shutdown_timeout,
                "minimum": 0.1,
            },
        ]

    def save_config(self, values: Dict[str, Any], hermes_home: str) -> None:
        """Validate and save terminal-wizard settings."""
        candidate = dict(values or {})
        requested_mode = str(candidate.get("mode", "") or "").strip().lower()
        if requested_mode == "embedded" and _is_windows_platform():
            raise ReMeConfigError(
                "Embedded ReMe Option B requires POSIX durable filesystem primitives; Windows is unsupported",
            )
        current = load_config(hermes_home)
        mode = str(candidate.get("mode", current.mode) or current.mode).strip().lower()
        if mode == "embedded" and _is_windows_platform():
            raise ReMeConfigError(
                "Embedded ReMe Option B requires POSIX durable filesystem primitives; Windows is unsupported",
            )
        if mode == "http":
            endpoint = str(
                candidate.get("endpoint", current.endpoint) or current.endpoint,
            )
            probe = HttpReMeBackend(endpoint, request_timeout=current.request_timeout)
            probe.health(timeout=current.health_timeout)
        elif importlib.util.find_spec("reme") is None:
            raise ReMeConfigError(_SDK_INSTALL_HINT)
        save_config(candidate, hermes_home)

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        """Automatic recall and capture add no model-visible tools."""
        return []

    def backup_paths(self) -> List[str]:
        """Expose an embedded workspace to Hermes backup without starting ReMe."""
        try:
            config = load_config()
        except ReMeConfigError:
            return []
        return [config.workspace_dir] if config.mode == "embedded" and config.workspace_dir else []

    def recall_status(self) -> Optional[RecallStatus]:
        """Describe only the content injected by the latest prefetch call."""
        return self._recall_status

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Recall relevant memory before Hermes sends the turn to the model."""
        del session_id
        self._recall_status = None
        query = str(query or "").strip()
        if not query or time.monotonic() < self._next_recall_attempt:
            return ""
        deadline = time.monotonic() + self._recall_timeout
        if not self._backend_lock.acquire(  # pylint: disable=consider-using-with
            timeout=max(0.0, deadline - time.monotonic()),
        ):
            logger.warning(
                "ReMe retrieval at %s timed out waiting for the backend",
                self._backend_label,
            )
            return ""
        try:
            if not self._ensure_backend(deadline=deadline):
                return ""
            assert self._backend is not None
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    logger.warning(
                        "ReMe retrieval at %s exhausted its timeout before search",
                        self._backend_label,
                    )
                    return ""
                response = self._backend.search(
                    query,
                    limit=self._recall_limit,
                    timeout=remaining,
                )
            except ReMeBackendError as exc:
                self._next_recall_attempt = time.monotonic() + self._health_retry_seconds
                logger.warning(
                    "ReMe retrieval failed at %s: %s",
                    self._backend_label,
                    exc,
                )
                return ""
            finally:
                self._close_backend_if_shutdown_locked()
        finally:
            self._backend_lock.release()
        answer = response.get("answer")
        answer = answer.strip() if isinstance(answer, str) else ""
        if answer:
            self._recall_status = RecallStatus(
                provider_label="ReMe",
                count=self._result_count(response),
            )
        return answer

    @staticmethod
    def _result_count(response: dict[str, Any]) -> int:
        metadata = response.get("metadata")
        if not isinstance(metadata, dict):
            return 0
        counts = metadata.get("counts")
        if isinstance(counts, dict):
            returned = counts.get("returned")
            if isinstance(returned, int) and not isinstance(returned, bool) and returned >= 0:
                return returned
        results = metadata.get("results")
        return len(results) if isinstance(results, list) else 0

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """Durably spool one completed turn before background memory extraction."""
        del messages
        user = str(user_content or "").strip()
        assistant = str(assistant_content or "").strip()
        if not self._write_enabled or not (user or assistant):
            return
        routed_session = str(session_id or self._session_id)
        if not routed_session:
            logger.warning(
                "ReMe skipped a completed turn because Hermes supplied no session id",
            )
            return
        payload = _build_turn_payload(self._profile_id, routed_session, user, assistant)
        if not self._enqueue_write(payload):
            logger.warning(
                "ReMe did not record session %s because the provider is unavailable: %s",
                payload["session_id"],
                self._unavailable_reason or ("shutting down" if self._shutdown_started else "spool write failed"),
            )

    def on_session_switch(
        self,
        new_session_id: str,
        *,
        parent_session_id: str = "",
        reset: bool = False,
        rewound: bool = False,
        **kwargs: Any,
    ) -> None:
        """Route future writes to the newly active Hermes conversation."""
        del parent_session_id, reset, rewound, kwargs
        if new_session_id:
            self._session_id = str(new_session_id)

    def shutdown(self) -> None:
        """Drain queued writes, then close the backend within a bounded interval."""
        deadline = time.monotonic() + self._shutdown_timeout
        with self._write_thread_lock:
            already_started = self._shutdown_started
            if not already_started:
                self._shutdown_started = True
                self._accept_writes = False
                self._journal_state = "SHUTDOWN"
            thread = self._write_thread
            self._retry_wakeup.cancel_locked()
            self._retry_wakeup_pending = False
            if thread is not None and not already_started:
                self._write_queue.put(None)
        if thread is not None:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
            if thread.is_alive():
                abandoned = self._discard_queued_writes()
                logger.warning(
                    "ReMe shutdown timed out after %.1fs; %d queued write(s) remain durably spooled "
                    "and an in-flight write continues",
                    self._shutdown_timeout,
                    abandoned,
                )
        self._deferred_backend_close = True
        # A context manager cannot express the bounded wait required by shutdown.
        if self._backend_lock.acquire(  # pylint: disable=consider-using-with
            timeout=max(0.0, deadline - time.monotonic()),
        ):
            try:
                closed = self._close_backend_locked(
                    timeout=max(0.0, deadline - time.monotonic()),
                )
                if closed:
                    self._finish_deferred_backend_close_locked()
            finally:
                self._backend_lock.release()
        else:
            logger.warning(
                "ReMe backend shutdown is deferred until the in-flight operation finishes",
            )
        self._backend_available = False
        self._next_health_probe = 0.0

    def _release_profile_lock(self) -> None:
        profile_lock, self._profile_lock = self._profile_lock, None
        if profile_lock is not None:
            profile_lock.release()

    def _release_workspace_owner(self) -> None:
        owner, self._workspace_owner = self._workspace_owner, None
        if owner is not None:
            owner.release()

    def _release_profile_resources_if_writer_stopped(self) -> bool:
        """Release ownership only after the writer completed journal handling."""
        with self._write_thread_lock:
            # The writer clears this slot only after its backend call, durable
            # journal acknowledgement, queue bookkeeping, and task_done all
            # finish. In particular, do not release from the writer's own
            # _record_payload finally block before _remove_pending_write.
            if self._write_thread is not None or self._backend is not None or self._backend_cleanup_pending:
                return False
            self._release_profile_lock()
            self._release_workspace_owner()
            return True

    def _atexit_shutdown(self) -> None:
        try:
            self.shutdown()
        except Exception as exc:  # pragma: no cover
            logger.debug("ReMe atexit shutdown failed: %s", exc)

    def _discard_queued_writes(self) -> int:
        abandoned = 0
        while True:
            try:
                payload = self._write_queue.get_nowait()
            except queue.Empty:
                break
            try:
                if payload is not None:
                    abandoned += 1
                    if isinstance(payload, dict):
                        with self._write_thread_lock:
                            self._queued_record_ids.discard(payload.get("id", ""))
            finally:
                self._write_queue.task_done()
        self._write_queue.put(None)
        return abandoned

    def _enqueue_write(self, payload: dict[str, Any]) -> bool:
        with self._write_thread_lock:
            if not self._accept_writes or self._journal_state != "READY":
                return False
            try:
                record = self._append_pending_write(payload)
            except (OSError, TypeError, ValueError) as exc:
                logger.error("ReMe could not durably accept a completed turn: %s", exc)
                return False
            self._pending_generation += 1
            self._queue_pending_records((record,))
            self._start_writer_if_needed()
            return True

    def _disable_without_profile(self, session_id: str, profile_id: Any) -> None:
        """Fail closed when Hermes does not provide its profile home."""
        with self._write_thread_lock:
            if self._write_thread is not None and self._write_thread.is_alive():
                raise RuntimeError(
                    "Cannot disable ReMe while its previous writer is still running",
                )
            self._accept_writes = False
        with self._backend_lock:
            if not self._close_backend_locked():
                return
        self._release_profile_lock()
        self._profile_lock_required = False
        self._profile_root = None
        self._config = None
        self._backend_label = "missing Hermes profile"
        self._session_id = str(session_id or "")
        self._profile_id = str(profile_id or "default")
        self._pending_path = None
        self._release_workspace_owner()
        self._backend_available = False
        self._backend_cleanup_pending = False
        self._recall_status = None
        self._unavailable_reason = "hermes_home is required for profile-scoped ReMe state"
        logger.warning("ReMe provider is unavailable because Hermes supplied no hermes_home")

    def _start_writer_if_needed(self) -> None:
        """Start one writer after taking a durable, FIFO-ordered queue snapshot."""
        with self._write_thread_lock:
            if self._write_thread is not None or self._writer_starting:
                return
            if self._journal_state in {"HALTED_CORRUPT", "SHUTDOWN"}:
                return
            if time.monotonic() < self._next_write_attempt:
                if self._retry_wakeup_pending:
                    self._retry_wakeup.arm_locked()
                return
            self._writer_starting = True
            generation = self._pending_generation
        try:
            if not self._reconcile_durable_state():
                with self._write_thread_lock:
                    if self._journal_state in {"QUARANTINE_CLEANUP", "HALTED_IO"} and not self._shutdown_started:
                        self._retry_wakeup_pending = True
                        self._retry_wakeup.arm_locked()
                return
            records = self._read_pending_writes()
            if records is None:
                with self._write_thread_lock:
                    if self._journal_state == "HALTED_IO" and not self._shutdown_started:
                        self._retry_wakeup_pending = True
                        self._retry_wakeup.arm_locked()
                return
            with self._write_thread_lock:
                if self._shutdown_started:
                    return
                if generation != self._pending_generation:
                    self._writer_starting = False
                    self._start_writer_if_needed()
                    return
                rebuild_write_queue_locked(self, records)
                if self._write_queue.empty():
                    self._retry_wakeup_pending = False
                    self._retry_wakeup.cancel_locked()
                    return
                self._write_thread = spawn_profile_thread(
                    self._write_loop,
                    args=(self._write_queue,),
                    name="reme-memory-writer",
                )
                self._retry_wakeup_pending = False
                self._retry_wakeup.cancel_locked()
                self._write_thread.start()
        finally:
            with self._write_thread_lock:
                self._writer_starting = False

    def _queue_pending_records(
        self,
        records: tuple[dict[str, Any], ...] | None = None,
    ) -> None:
        if records is None:
            records = tuple(self._read_pending_writes() or ())
        for record in records:
            record_id = record["id"]
            if record_id in self._queued_record_ids:
                continue
            self._queued_record_ids.add(record_id)
            self._write_queue.put(record)

    def _journal_path_safe(self, path: Path) -> bool:
        """Reject links, hardlinks, non-files, and ambiguous journal paths."""
        try:
            if not _safe_existing_path_components(
                path.parent,
                trusted_root=self._profile_root,
            ):
                return False
            file_stat = path.lstat()
        except FileNotFoundError:
            return True
        except OSError:
            return False
        return stat.S_ISREG(file_stat.st_mode) and file_stat.st_nlink == 1 and not _is_unsafe_link(path, file_stat)

    def _set_journal_halted(self, state: str, detail: str = "") -> None:
        if self._journal_state in {"HALTED_CORRUPT", "SHUTDOWN"} and self._journal_state != state:
            return
        self._journal_state = state
        self._accept_writes = False
        self._unavailable_reason = detail or "durable ReMe journal requires operator recovery"
        if state != "HALTED_CORRUPT":
            self._defer_write_retry()
        logger.error("ReMe durable journal entered %s", state)

    @staticmethod
    def _record_payload_equal(first: dict[str, Any], second: dict[str, Any]) -> bool:
        profiles_match = (
            first.get("profile_id") is None
            or second.get("profile_id") is None
            or first.get("profile_id") == second.get("profile_id")
        )
        first_payload = first.get("payload")
        second_payload = second.get("payload")
        if first_payload == second_payload:
            return profiles_match
        if not isinstance(first_payload, dict) or not isinstance(second_payload, dict):
            return False
        if {key: value for key, value in first_payload.items() if key != "messages"} != {
            key: value for key, value in second_payload.items() if key != "messages"
        }:
            return False
        first_messages = first_payload.get("messages")
        second_messages = second_payload.get("messages")
        if not isinstance(first_messages, list) or not isinstance(second_messages, list):
            return False
        if len(first_messages) != len(second_messages):
            return False
        for first_message, second_message in zip(first_messages, second_messages):
            if not isinstance(first_message, dict) or not isinstance(second_message, dict):
                return False
            if {key: value for key, value in first_message.items() if key != "created_at"} != {
                key: value for key, value in second_message.items() if key != "created_at"
            }:
                return False
            if "created_at" in first_message and "created_at" in second_message:
                if first_message["created_at"] != second_message["created_at"]:
                    return False
        return profiles_match

    @staticmethod
    def _quarantine_record_equal(first: dict[str, Any], second: dict[str, Any]) -> bool:
        """Require every quarantine evidence field to agree for duplicate IDs."""
        return all(
            first.get(key) == second.get(key)
            for key in ("id", "profile_id", "attempts", "semantic_refusals", "reason", "outcome", "payload")
        )

    @staticmethod
    def _valid_quarantine_record(record: object) -> bool:
        if not isinstance(record, dict) or not _valid_pending_record(
            {
                key: value
                for key, value in record.items()
                if key in {"id", "profile_id", "attempts", "semantic_refusals", "payload"}
            },
        ):
            return False
        if record.get("reason") not in {"semantic_refusal", "permanent_refusal"}:
            return False
        if record.get("outcome") != "QUARANTINED/NOT COMMITTED":
            return False
        attempts = record.get("attempts", 0)
        return isinstance(attempts, int) and not isinstance(attempts, bool) and attempts >= 0

    def _iter_journal_lines(self, path: Path):
        """Yield bounded raw lines without a whole-file read race."""
        size = path.stat().st_size
        if size > _MAX_JOURNAL_BYTES:
            raise ValueError("durable journal exceeds its size limit")
        total = 0
        with path.open("rb") as stream:
            while True:
                raw_line = stream.readline(_MAX_JOURNAL_LINE_BYTES + 1)
                if not raw_line:
                    return
                if len(raw_line) > _MAX_JOURNAL_LINE_BYTES or not raw_line.endswith(b"\n"):
                    raise ValueError("durable journal line is oversized or incomplete")
                total += len(raw_line)
                if total > _MAX_JOURNAL_BYTES:
                    raise ValueError("durable journal exceeds its size limit")
                yield raw_line

    @staticmethod
    def _has_final_newline(path: Path) -> bool:
        """Check journal termination without allocating the journal contents."""
        if not path.exists() or path.stat().st_size == 0:
            return True
        with path.open("rb") as handle:
            handle.seek(-1, os.SEEK_END)
            return handle.read(1) == b"\n"

    def _scan_journal(self, path: Path, *, quarantine: bool = False) -> list[dict[str, Any]] | None:
        """Parse one private JSONL journal without treating failures as empty."""
        if not self._journal_path_safe(path):
            return None
        if not path.exists():
            return []
        records: list[dict[str, Any]] = []
        seen: dict[str, dict[str, Any]] = {}
        try:
            for raw_line in self._iter_journal_lines(path):
                record = json.loads(raw_line.decode("utf-8"))
                if not isinstance(record, dict):
                    raise ValueError("journal entry is not an object")
                record_id = record.get("id")
                if not isinstance(record_id, str) or not record_id:
                    raise ValueError("journal entry has no valid id")
                if quarantine:
                    if not self._valid_quarantine_record(record):
                        raise ValueError("quarantine payload is invalid")
                elif not _valid_pending_record(record):
                    raise ValueError("pending journal entry is invalid")
                profile_id = record.get("profile_id")
                if profile_id is not None and profile_id != self._profile_id:
                    raise ValueError("journal profile scope does not match")
                previous = seen.get(record_id)
                if previous is not None:
                    if not quarantine or not self._quarantine_record_equal(previous, record):
                        raise ValueError("journal contains an ambiguous duplicate id")
                    continue
                seen[record_id] = record
                if len(records) >= _MAX_JOURNAL_RECORDS:
                    raise ValueError("durable journal exceeds its record limit")
                records.append(record)
        except (OSError, UnicodeError, json.JSONDecodeError, RecursionError, TypeError, ValueError):
            return None
        return records

    def _reconcile_durable_state(self) -> bool:
        """Reconcile quarantine before every queue snapshot, never replaying it."""
        if self._journal_state in {"HALTED_CORRUPT", "SHUTDOWN"}:
            return False
        path = self._pending_path
        if path is None:
            return False
        quarantine_path = path.parent / _QUARANTINE_QUEUE_FILENAME
        with self._pending_lock:
            pending = self._scan_journal(path)
            quarantined = self._scan_journal(quarantine_path, quarantine=True)
        if pending is None or quarantined is None:
            self._set_journal_halted("HALTED_CORRUPT")
            return False
        by_id = {record["id"]: record for record in pending}
        for entry in quarantined:
            pending_record = by_id.get(entry["id"])
            if pending_record is None:
                continue
            if "payload" not in entry or not self._record_payload_equal(pending_record, entry):
                self._set_journal_halted("HALTED_CORRUPT")
                return False
            if not self._remove_pending_write(pending_record):
                self._journal_state = "QUARANTINE_CLEANUP"
                self._accept_writes = False
                self._defer_write_retry()
                return False
        if self._shutdown_started or self._journal_state in {"HALTED_CORRUPT", "SHUTDOWN"}:
            return False
        self._journal_state = "READY"
        self._accept_writes = True
        return True

    def _append_pending_write(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self._pending_path is None:
            raise OSError("profile-local ReMe queue path is not configured")
        if self._journal_state != "READY":
            raise OSError("durable ReMe journal is not accepting writes")
        record = {"id": uuid.uuid4().hex, "profile_id": self._profile_id, "attempts": 0, "payload": payload}
        encoded = (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        if len(encoded) > _MAX_RECORD_JSON_BYTES:
            raise ValueError("durable ReMe turn exceeds the 1 MiB record limit")
        path = self._pending_path
        if not self._journal_path_safe(path):
            raise OSError(f"Refusing unsafe ReMe queue path: {path}")
        parent_created = not path.parent.exists()
        path.parent.mkdir(parents=True, exist_ok=True)
        if not self._journal_path_safe(path):
            raise OSError(f"Refusing unsafe ReMe queue path: {path}")
        if parent_created:
            _fsync_directory(path.parent.parent)
        with self._pending_lock:
            if not self._journal_path_safe(path):
                raise OSError(f"Refusing unsafe ReMe queue path: {path}")
            os.chmod(path.parent, 0o700)
            if not self._journal_path_safe(path):
                raise OSError(f"Refusing unsafe ReMe queue path: {path}")
            existing = self._scan_journal(path)
            if existing is None:
                self._set_journal_halted("HALTED_CORRUPT")
                raise OSError("durable ReMe journal is corrupt")
            current_size = path.stat().st_size if path.exists() else 0
            if len(existing) >= _MAX_JOURNAL_RECORDS or current_size + len(encoded) > _MAX_JOURNAL_BYTES:
                raise OSError("durable ReMe journal capacity is exhausted")
            if path.exists() and path.stat().st_size and not self._has_final_newline(path):
                raise OSError("durable ReMe journal has an incomplete final line")
            flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            fd = os.open(path, flags, 0o600)
            try:
                _set_private_file_mode(path, fd)
                with os.fdopen(fd, "a", encoding="utf-8") as handle:
                    fd = -1
                    handle.write(
                        encoded.decode("utf-8"),
                    )
                    handle.flush()
                    _fsync_file(handle.fileno())
            finally:
                if fd >= 0:
                    os.close(fd)
            _fsync_directory(path.parent)
        return record

    def _update_pending_write(self, record: dict[str, Any]) -> bool:
        """Persist retry metadata without changing the record's queue position."""
        path = self._pending_path
        if path is None or not self._journal_path_safe(path) or not _valid_pending_record(record):
            return False
        with self._pending_lock:
            try:
                current_records = self._scan_journal(path)
                if current_records is None:
                    return False
                matches = [item for item in current_records if item.get("id") == record.get("id")]
                if len(matches) != 1 or not self._record_payload_equal(matches[0], record):
                    return False
                updated = self._rewrite_pending_record(path, matches[0], replacement=record)
                if not updated:
                    self._defer_write_retry()
                return updated
            except (OSError, RecursionError, TypeError, UnicodeError, ValueError) as exc:
                logger.error("ReMe could not persist durable write retry state: %s", exc)
                self._defer_write_retry()
                return False
        return True

    def _quarantine_pending_write(self, record: dict[str, Any], reason: str) -> bool:
        """Durably move a refused/exhausted record out of the active retry queue."""
        path = self._pending_path
        if path is None or not self._journal_path_safe(path):
            self._set_journal_halted("HALTED_CORRUPT", "durable quarantine path is unavailable")
            return False
        quarantine = path.parent / _QUARANTINE_QUEUE_FILENAME
        if not self._journal_path_safe(quarantine):
            self._set_journal_halted("HALTED_CORRUPT", "durable quarantine path is unsafe")
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        if not self._journal_path_safe(path) or not self._journal_path_safe(quarantine):
            self._set_journal_halted("HALTED_CORRUPT", "durable quarantine path became unsafe")
            return False
        entry = {
            "id": record.get("id"),
            "attempts": record.get("attempts", 0),
            "semantic_refusals": record.get("semantic_refusals", 0),
            "profile_id": record.get("profile_id", self._profile_id),
            "reason": reason,
            "outcome": "QUARANTINED/NOT COMMITTED",
            "payload": record.get("payload"),
        }
        if not self._valid_quarantine_record(entry):
            self._set_journal_halted("HALTED_CORRUPT")
            return False
        with self._pending_lock:
            try:
                existing_records = self._scan_journal(quarantine, quarantine=True)
                if existing_records is None:
                    self._set_journal_halted("HALTED_CORRUPT")
                    return False
                matching = [item for item in existing_records if item.get("id") == record.get("id")]
                if matching and not any(self._quarantine_record_equal(item, entry) for item in matching):
                    self._set_journal_halted("HALTED_CORRUPT")
                    return False
                if not matching:
                    encoded = (json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
                    if len(encoded) > _MAX_JOURNAL_LINE_BYTES:
                        self._set_journal_halted("HALTED_CORRUPT", "durable quarantine record exceeds its size limit")
                        return False
                    quarantine_size = quarantine.stat().st_size if quarantine.exists() else 0
                    if (
                        len(existing_records) >= _MAX_JOURNAL_RECORDS
                        or quarantine_size + len(encoded) > _MAX_JOURNAL_BYTES
                    ):
                        self._set_journal_halted("HALTED_CORRUPT", "durable quarantine capacity is exhausted")
                        return False
                    if quarantine.exists() and quarantine.stat().st_size and not self._has_final_newline(quarantine):
                        self._set_journal_halted("HALTED_CORRUPT", "durable quarantine has an incomplete final line")
                        return False
                    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
                    if hasattr(os, "O_NOFOLLOW"):
                        flags |= os.O_NOFOLLOW
                    fd = os.open(quarantine, flags, 0o600)
                    try:
                        _set_private_file_mode(quarantine, fd)
                        with os.fdopen(fd, "a", encoding="utf-8") as handle:
                            fd = -1
                            handle.write(encoded.decode("utf-8"))
                            handle.flush()
                            _fsync_file(handle.fileno())
                    finally:
                        if fd >= 0:
                            os.close(fd)
                    _fsync_directory(quarantine.parent)
            except (OSError, RecursionError, TypeError, UnicodeError, ValueError) as exc:
                logger.error("ReMe could not quarantine durable write: %s", exc)
                return False
        # The quarantine record is durable even if cleanup of the active queue
        # is temporarily unavailable.  The next provider start will retry only
        # that cleanup; do not issue another backend write or duplicate the
        # quarantine entry in this process.
        if self._remove_pending_write(record):
            return True
        self._journal_state = "QUARANTINE_CLEANUP"
        self._accept_writes = False
        self._defer_write_retry()
        return False

    def _read_pending_writes(self) -> list[dict[str, Any]] | None:
        path = self._pending_path
        if path is None:
            return []
        if not self._journal_path_safe(path):
            self._set_journal_halted("HALTED_CORRUPT")
            return None
        if not path.exists():
            return []
        with self._pending_lock:
            records = self._scan_journal(path)
        if records is None:
            self._set_journal_halted("HALTED_CORRUPT")
            return None
        default_historical_created_at = time.strftime(
            "%Y-%m-%dT%H:%M:%S+00:00",
            time.gmtime(path.stat().st_mtime),
        )
        legacy_records = []
        upgraded_records = []
        for record in records:
            try:
                record_key = (path, record["id"])
                historical_created_at = self._legacy_created_at_by_record.get(
                    record_key,
                    default_historical_created_at,
                )
                upgraded = _upgrade_legacy_payload(record, historical_created_at)
            except (RecursionError, TypeError, UnicodeError, ValueError):
                self._set_journal_halted("HALTED_CORRUPT")
                return None
            upgraded_records.append(upgraded)
            if upgraded is not record:
                self._legacy_created_at_by_record.setdefault(record_key, historical_created_at)
                legacy_records.append(upgraded)
        for record in legacy_records:
            if not self._update_pending_write(record):
                logger.error(
                    "ReMe could not persist legacy timestamp upgrade for durable write %s",
                    record.get("id", "unknown"),
                )
                self._set_journal_halted("HALTED_IO")
                return None
            else:
                self._legacy_created_at_by_record.pop((path, record["id"]), None)
        return upgraded_records

    def _rewrite_pending_record(
        self,
        path: Path,
        expected_record: dict[str, Any],
        *,
        replacement: dict[str, Any] | None = None,
    ) -> bool:
        """Rewrite only after a complete verified scan, preserving all other bytes."""
        if not self._journal_path_safe(path):
            return False
        fd = -1
        backup_fd = -1
        temp_name: str | None = None
        backup_name: str | None = None
        replaced = False
        found = 0
        try:
            fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
            backup_fd, backup_name = tempfile.mkstemp(prefix=f".{path.name}.original.", dir=path.parent)
            with os.fdopen(fd, "wb") as output, os.fdopen(backup_fd, "wb") as backup:
                fd = -1
                backup_fd = -1
                for raw_line in self._iter_journal_lines(path):
                    current = json.loads(raw_line.decode("utf-8"))
                    if isinstance(current, dict) and current.get("id") == expected_record.get("id"):
                        if not self._record_payload_equal(current, expected_record):
                            raise ValueError("durable record identity changed before mutation")
                        found += 1
                        backup.write(raw_line)
                        if replacement is None:
                            continue
                        raw_line = (json.dumps(replacement, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
                    else:
                        backup.write(raw_line)
                    output.write(raw_line)
                if found != 1:
                    return False
                output.flush()
                _fsync_file(output.fileno())
                backup.flush()
                _fsync_file(backup.fileno())
            if not self._journal_path_safe(path):
                return False
            assert temp_name is not None
            assert backup_name is not None
            os.chmod(temp_name, 0o600)
            os.chmod(backup_name, 0o600)
            if not self._journal_path_safe(path):
                return False
            os.replace(temp_name, path)
            replaced = True
            if not self._journal_path_safe(path):
                return False
            _fsync_directory(path.parent)
            if not self._journal_path_safe(path):
                return False
            os.unlink(backup_name)
            return True
        except (OSError, UnicodeError, json.JSONDecodeError, RecursionError, TypeError, ValueError) as exc:
            logger.error("ReMe could not complete a durable journal mutation: %s", exc)
            if replaced:
                try:
                    if self._journal_path_safe(path):
                        os.replace(backup_name, path)
                        _fsync_directory(path.parent)
                    else:
                        logger.error("ReMe refused to restore a journal after its parent became unsafe")
                except OSError:
                    logger.error("ReMe could not restore the original durable journal after mutation failure")
            return False
        finally:
            if fd >= 0:
                os.close(fd)
            if backup_fd >= 0:
                os.close(backup_fd)
            if temp_name is not None and self._journal_path_safe(path) and os.path.exists(temp_name):
                os.unlink(temp_name)
            if backup_name is not None and self._journal_path_safe(path) and os.path.exists(backup_name):
                os.unlink(backup_name)

    def _remove_pending_write(self, record: dict[str, Any]) -> bool:
        """Remove exactly one caller-identified durable record, never by ID alone."""
        if not isinstance(record, dict) or not isinstance(record.get("id"), str):
            return False
        path = self._pending_path
        if path is None or not self._journal_path_safe(path):
            logger.error("ReMe queue disappeared before acknowledgement: %s", path)
            return False
        if not self._journal_path_safe(path):
            logger.error("Refusing unsafe ReMe queue acknowledgement path: %s", path)
            return False
        with self._pending_lock:
            try:
                records = self._scan_journal(path)
                if records is None:
                    return False
                matches = [item for item in records if item.get("id") == record.get("id")]
                if len(matches) > 1:
                    return False
                if not matches:
                    return True
                if not self._record_payload_equal(matches[0], record):
                    return False
                return self._rewrite_pending_record(path, matches[0])
            except (OSError, RecursionError, TypeError, UnicodeError, ValueError) as exc:
                logger.error(
                    "ReMe could not acknowledge durable write %s: %s",
                    record.get("id", "unknown"),
                    exc,
                )
                return False
        return True

    def _write_loop(self, write_queue: queue.Queue[dict[str, Any] | None]) -> None:
        try:
            while True:
                payload = write_queue.get()
                try:
                    if payload is None:
                        return
                    retry = False
                    try:
                        if not self._record_payload(
                            payload["payload"],
                            request_id=payload["id"],
                            respect_cooldown=True,
                        ):
                            retry = True
                        elif not self._remove_pending_write(payload):
                            self._last_write_failure_kind = "ack_failure"
                            self._defer_write_retry()
                            retry = True
                    except Exception:
                        logger.exception(
                            "Unexpected ReMe recording failure; durable write remains queued",
                        )
                        self._last_write_failure_kind = "retryable"
                        self._defer_write_retry()
                        retry = True
                    if retry:
                        failure_kind = self._last_write_failure_kind or "retryable"
                        if failure_kind == "cooldown":
                            self._schedule_retry(payload)
                            return
                        try:
                            prior_attempts = int(payload.get("attempts", 0))
                        except (TypeError, ValueError):
                            prior_attempts = 0
                        payload["attempts"] = max(0, prior_attempts) + 1
                        if failure_kind in {"semantic_refusal", "permanent"}:
                            if failure_kind == "semantic_refusal":
                                prior_refusals = payload.get("semantic_refusals", 0)
                                if not isinstance(prior_refusals, int) or isinstance(prior_refusals, bool):
                                    self._set_journal_halted("HALTED_CORRUPT")
                                    return
                                next_refusals = prior_refusals + 1
                                if next_refusals < 3:
                                    payload["semantic_refusals"] = next_refusals
                                    if not self._update_pending_write(payload):
                                        self._set_journal_halted("HALTED_IO")
                                        return
                                    self._defer_write_retry()
                                    self._schedule_retry(payload)
                                    return
                                payload["semantic_refusals"] = 3
                                quarantine_reason = "semantic_refusal"
                            else:
                                quarantine_reason = "permanent_refusal"
                            if self._quarantine_pending_write(payload, quarantine_reason):
                                logger.error(
                                    "ReMe durable write %s was quarantined (%s)",
                                    payload.get("id", "unknown"),
                                    quarantine_reason,
                                )
                                continue
                            if self._journal_state == "HALTED_CORRUPT":
                                return
                            self._defer_write_retry()
                            self._schedule_retry(payload)
                            return
                        if not self._update_pending_write(payload):
                            self._set_journal_halted("HALTED_IO")
                            return
                        logger.warning(
                            "ReMe durable write %s remains queued for a later retry (attempt %d)",
                            payload.get("id", "unknown"),
                            payload["attempts"],
                        )
                        self._schedule_retry(payload)
                        return
                finally:
                    with self._write_thread_lock:
                        if payload is not None:
                            self._queued_record_ids.discard(payload.get("id", ""))
                        write_queue.task_done()
        finally:
            should_restart = False
            with self._write_thread_lock:
                if self._write_thread is threading.current_thread():
                    self._write_thread = None
                    should_restart = (
                        self._retry_wakeup_pending and not self._shutdown_started and not self._retry_timers
                    )
            if should_restart:
                self._start_writer_if_needed()
            if self._shutdown_started:
                # The writer slot is now clear: complete any deferred backend
                # teardown before releasing the profile and workspace locks.
                with self._backend_lock:
                    self._close_backend_if_shutdown_locked()
                self._release_profile_resources_if_writer_stopped()

    def _protected_option_b_enabled(self) -> bool:
        """Return whether this provider uses the fixed protected Stage 1 contract."""
        config = self._config
        return bool(
            config
            and config.mode == "embedded"
            and config.reme_config == STAGE1_CONFIG_NAME
            and not config.allow_custom_config,
        )

    def _record_payload(
        self,
        payload: dict[str, Any],
        *,
        request_id: str | None = None,
        respect_cooldown: bool = True,
    ) -> bool:
        self._last_write_failure_kind = ""
        if respect_cooldown and time.monotonic() < self._next_write_attempt:
            self._last_write_failure_kind = "cooldown"
            logger.warning(
                "ReMe write for session %s skipped during cooldown",
                payload["session_id"],
            )
            return False
        with self._backend_lock:
            if not self._ensure_backend(allow_shutdown=True):
                self._last_write_failure_kind = "unavailable"
                self._defer_write_retry()
                logger.warning(
                    "ReMe write for session %s skipped because backend is unavailable",
                    payload["session_id"],
                )
                return False
            assert self._backend is not None
            try:
                response = self._backend.auto_memory(
                    payload["session_id"],
                    payload["messages"],
                    timeout=self._request_timeout,
                    request_id=request_id,
                )
                if not isinstance(response, dict) or response.get("success") is not True:
                    metadata = response.get("metadata") if isinstance(response, dict) else None
                    refusal_code = metadata.get("fermes_refusal_code") if isinstance(metadata, dict) else None
                    refusal_reason = metadata.get("fermes_refusal_reason") if isinstance(metadata, dict) else None
                    raise ReMeBackendError(
                        str(
                            (
                                response.get("answer")
                                if isinstance(response, dict)
                                else "auto_memory returned a non-object response"
                            ),
                        )
                        or "auto_memory did not report success",
                        refusal_code=(
                            refusal_code
                            if self._protected_option_b_enabled()
                            and refusal_code == "PROTECTED_OPTION_B_PROPOSAL_INVALID"
                            else None
                        ),
                        refusal_reason=refusal_reason if isinstance(refusal_reason, str) else None,
                    )
                if self._protected_option_b_enabled():
                    metadata = response.get("metadata")
                    if not isinstance(metadata, dict):
                        raise ReMeBackendError("protected auto_memory returned no durable receipt")
                    outcome = metadata.get("outcome", "commit")
                    if outcome == "commit":
                        commit = metadata.get("commit")
                        if (
                            not isinstance(commit, dict)
                            or not isinstance(request_id, str)
                            or commit.get("request_id") != request_id
                        ):
                            raise ReMeBackendError("protected auto_memory returned an unbound commit receipt")
                    elif outcome == "noop":
                        if (
                            not isinstance(request_id, str)
                            or metadata.get("request_id") != request_id
                            or metadata.get("modified") is not False
                            or metadata.get("n_messages") != 0
                            or "commit" in metadata
                        ):
                            raise ReMeBackendError("protected auto_memory returned an invalid no-op receipt")
                    else:
                        raise ReMeBackendError("protected auto_memory returned an unknown receipt outcome")
                if response.get("answer") and not isinstance(response.get("metadata"), dict):
                    raise ReMeBackendError("auto_memory returned success without a verified commit receipt")
                return True
            except ReMeBackendError as exc:
                self._last_write_failure_kind = _classify_write_failure(exc)
                if self._last_write_failure_kind not in {"permanent", "semantic_refusal"}:
                    self._defer_write_retry()
                logger.warning(
                    "ReMe recording failed at %s (%s)",
                    self._backend_label,
                    self._last_write_failure_kind,
                )
            finally:
                self._close_backend_if_shutdown_locked()
        return False

    def _defer_write_retry(self) -> None:
        """Apply the backend health cooldown to every operational retry path."""
        retry_at = time.monotonic() + self._health_retry_seconds
        self._next_write_attempt = max(self._next_write_attempt, self._next_health_probe, retry_at)

    def _schedule_retry(self, record: dict[str, Any]) -> None:
        """Arrange one wakeup; the durable journal supplies the record later."""
        del record
        with self._write_thread_lock:
            if self._shutdown_started:
                return
            self._retry_wakeup_pending = True
            self._retry_wakeup.arm_locked()

    # pylint: disable-next=too-many-return-statements
    def _ensure_backend(
        self,
        *,
        force: bool = False,
        allow_shutdown: bool = False,
        deadline: float | None = None,
    ) -> bool:
        with self._backend_lock:
            if self._backend_available and self._backend is not None and not force:
                return True
            if self._config is None or (self._shutdown_started and not allow_shutdown):
                return False
            if self._profile_lock_required and self._profile_lock is None:
                return False
            if self._backend_cleanup_pending:
                cleanup_timeout = self._shutdown_timeout
                if deadline is not None:
                    cleanup_timeout = min(cleanup_timeout, max(0.0, deadline - time.monotonic()))
                if not self._close_backend_locked(timeout=cleanup_timeout):
                    self._mark_unavailable("backend cleanup", ReMeBackendError("cleanup remains pending"))
                    return False
                if self._deferred_backend_close:
                    self._finish_deferred_backend_close_locked()
                if self._shutdown_started:
                    return False
            now = time.monotonic()
            if not force and now < self._next_health_probe:
                return False
            if self._backend is None:
                try:
                    backend_config = self._config
                    if deadline is not None:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            return False
                        backend_config = replace(
                            backend_config,
                            request_timeout=min(
                                backend_config.request_timeout,
                                remaining,
                            ),
                        )
                    self._backend = _backend_for(backend_config)
                    if deadline is None:
                        self._backend.start()
                    else:
                        self._backend.start(deadline=deadline)
                except Exception as exc:
                    cleanup_timeout = self._shutdown_timeout
                    if deadline is not None:
                        cleanup_timeout = max(0.0, deadline - time.monotonic())
                    if self._backend is not None:
                        self._close_backend_locked(timeout=cleanup_timeout)
                    self._mark_unavailable("startup", exc)
                    return False
            try:
                health_timeout = self._health_timeout
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return False
                    health_timeout = min(health_timeout, remaining)
                self._backend.health(timeout=health_timeout)
            except ReMeBackendError as exc:
                self._mark_unavailable("health check", exc)
                return False
            self._backend_available = True
            self._next_health_probe = 0.0
            return True

    def _close_backend_if_shutdown_locked(self) -> None:
        if self._deferred_backend_close:
            if self._close_backend_locked():
                if self._deferred_backend_close:
                    self._finish_deferred_backend_close_locked()

    def _finish_deferred_backend_close_locked(self) -> bool:
        """Finish shutdown ownership release after backend close completes."""
        if self._backend is not None or self._backend_cleanup_pending:
            return False
        if not self._release_profile_resources_if_writer_stopped():
            return False
        self._deferred_backend_close = False
        return True

    def _close_backend_locked(self, *, timeout: float | None = None) -> bool:
        backend = self._backend
        if backend is None:
            self._backend_cleanup_pending = False
            self._backend_available = False
            return True
        if backend is not None:
            try:
                backend.close(
                    timeout=self._shutdown_timeout if timeout is None else timeout,
                )
            except Exception as exc:
                self._backend_cleanup_pending = True
                logger.warning(
                    "ReMe backend shutdown failed at %s: %s",
                    self._backend_label,
                    exc,
                )
                self._backend_available = False
                return False
        self._backend = None
        self._backend_cleanup_pending = False
        self._backend_available = False
        return True

    def _mark_unavailable(self, operation: str, error: Exception) -> None:
        self._backend_available = False
        self._next_health_probe = time.monotonic() + self._health_retry_seconds
        logger.warning(
            "ReMe %s failed at %s: %s",
            operation,
            self._backend_label,
            error,
        )


def register(ctx: Any) -> None:
    """Register with Hermes' memory-provider collector."""
    ctx.register_memory_provider(ReMeMemoryProvider())
