"""Hermes Agent memory provider backed by HTTP or an embedded ReMe SDK."""

from __future__ import annotations

import atexit
import hashlib
import importlib.util
import json
import logging
import os
import queue
import re
import tempfile
import threading
import time
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider, RecallStatus

from .backend import ReMeBackend, ReMeBackendError, spawn_profile_thread
from .config import ReMeConfig, ReMeConfigError, load_config, save_config
from .embedded_backend import EmbeddedReMeBackend
from .http_backend import HttpReMeBackend

logger = logging.getLogger(__name__)
_NON_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._-]+")
_SDK_INSTALL_HINT = 'Embedded ReMe mode requires the SDK. Install it with: pip install "reme-ai[core]"'
_PENDING_QUEUE_DIRECTORY = "reme"
_PENDING_QUEUE_FILENAME = "pending-writes.jsonl"


def _fsync_directory(path: Path) -> None:
    """Flush a directory entry after creating or replacing the profile spool."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _slug(value: str, fallback: str, *, limit: int) -> str:
    value = _NON_FILENAME_CHARS.sub("-", str(value or "").strip()).strip("-._")
    return (value or fallback)[:limit]


def _scoped_session_id(profile_id: str, session_id: str) -> str:
    """Create a readable, filename-safe ID without allowing scope collisions."""
    profile = str(profile_id or "default")
    session = str(session_id or "session")
    digest = hashlib.sha256(f"{profile}\0{session}".encode()).hexdigest()[:12]
    return f"hermes-{_slug(profile, 'default', limit=32)}-{_slug(session, 'session', limit=64)}-{digest}"


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
        self._pending_path: Path | None = None
        self._backend_lock = threading.RLock()
        self._shutdown_started = False
        self._deferred_backend_close = False
        self._atexit_registered = False
        self._recall_status: RecallStatus | None = None
        self._unavailable_reason = ""

    @property
    def name(self) -> str:
        """Return the provider identifier used by Hermes configuration."""
        return "reme"

    def is_available(self) -> bool:
        """Check configuration and local dependencies without network or writes."""
        try:
            config = load_config()
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
                self._close_backend_locked()
            self._config = None
            self._backend_label = "invalid configuration"
            self._session_id = str(session_id or "")
            self._profile_id = str(kwargs.get("agent_identity") or "default")
            self._accept_writes = False
            self._pending_path = profile_home / _PENDING_QUEUE_DIRECTORY / _PENDING_QUEUE_FILENAME
            self._recall_status = None
            self._unavailable_reason = str(exc)
            logger.warning("ReMe provider configuration is invalid: %s", exc)
            return

        with self._backend_lock:
            self._close_backend_locked()
        self._config = config
        self._pending_path = profile_home / _PENDING_QUEUE_DIRECTORY / _PENDING_QUEUE_FILENAME
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
        self._accept_writes = True
        self._write_queue = queue.Queue()
        self._queued_record_ids = set()
        self._retry_timers = set()
        self._write_thread = None
        self._shutdown_started = False
        self._deferred_backend_close = False
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
        current = load_config(hermes_home)
        mode = str(candidate.get("mode", current.mode) or current.mode).strip().lower()
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
        payload = {
            "session_id": _scoped_session_id(self._profile_id, routed_session),
            "messages": [
                {"name": "user", "role": "user", "content": user},
                {"name": "assistant", "role": "assistant", "content": assistant},
            ],
        }
        if not self._enqueue_write(payload):
            logger.warning(
                "ReMe did not record session %s because the provider is shutting down",
                payload["session_id"],
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
            if self._shutdown_started:
                return
            self._shutdown_started = True
            self._accept_writes = False
            thread = self._write_thread
            if thread is not None:
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
                self._close_backend_locked(
                    timeout=max(0.0, deadline - time.monotonic()),
                )
                self._deferred_backend_close = False
            finally:
                self._backend_lock.release()
        else:
            logger.warning(
                "ReMe backend shutdown is deferred until the in-flight operation finishes",
            )
        self._backend_available = False
        self._next_health_probe = 0.0

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
            if not self._accept_writes:
                return False
            try:
                record = self._append_pending_write(payload)
            except (OSError, TypeError, ValueError) as exc:
                logger.error("ReMe could not durably accept a completed turn: %s", exc)
                return False
            self._queue_pending_records((record,))
            self._start_writer_if_needed()
            return True

    def _disable_without_profile(self, session_id: str, profile_id: Any) -> None:
        """Fail closed when Hermes does not provide its profile home."""
        with self._backend_lock:
            self._close_backend_locked()
        self._config = None
        self._backend_label = "missing Hermes profile"
        self._session_id = str(session_id or "")
        self._profile_id = str(profile_id or "default")
        self._pending_path = None
        self._accept_writes = False
        self._backend_available = False
        self._recall_status = None
        self._unavailable_reason = "hermes_home is required for profile-scoped ReMe state"
        logger.warning("ReMe provider is unavailable because Hermes supplied no hermes_home")

    def _start_writer_if_needed(self) -> None:
        with self._write_thread_lock:
            if self._write_thread is not None and self._write_thread.is_alive():
                return
            self._queue_pending_records()
            if self._write_queue.empty():
                return
            self._write_thread = spawn_profile_thread(
                self._write_loop,
                args=(self._write_queue,),
                name="reme-memory-writer",
            )
            self._write_thread.start()

    def _queue_pending_records(
        self,
        records: tuple[dict[str, Any], ...] | None = None,
    ) -> None:
        if records is None:
            records = tuple(self._read_pending_writes())
        for record in records:
            record_id = record["id"]
            if record_id in self._queued_record_ids:
                continue
            self._queued_record_ids.add(record_id)
            self._write_queue.put(record)

    def _append_pending_write(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self._pending_path is None:
            raise OSError("profile-local ReMe queue path is not configured")
        record = {"id": uuid.uuid4().hex, "payload": payload}
        path = self._pending_path
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise OSError(f"Refusing unsafe ReMe queue path: {path}")
        if path.parent.is_symlink():
            raise OSError(f"Refusing symlinked ReMe queue directory: {path.parent}")
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._pending_lock:
            if path.is_symlink() or (path.exists() and not path.is_file()):
                raise OSError(f"Refusing unsafe ReMe queue path: {path}")
            os.chmod(path.parent, 0o700)
            flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            fd = os.open(path, flags, 0o600)
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "a", encoding="utf-8") as handle:
                    fd = -1
                    handle.write(
                        json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n",
                    )
                    handle.flush()
                    os.fsync(handle.fileno())
            finally:
                if fd >= 0:
                    os.close(fd)
            _fsync_directory(path.parent)
        return record

    def _read_pending_writes(self) -> list[dict[str, Any]]:
        path = self._pending_path
        if path is None or not path.is_file():
            return []
        if path.is_symlink() or path.parent.is_symlink():
            logger.error("Refusing unsafe ReMe queue read path: %s", path)
            return []
        records = []
        with self._pending_lock:
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError as exc:
                logger.error(
                    "ReMe could not read its durable write queue %s: %s",
                    path,
                    exc,
                )
                return []
        for line_number, line in enumerate(lines, start=1):
            try:
                record = json.loads(line)
                if (
                    not isinstance(record, dict)
                    or not isinstance(record.get("id"), str)
                    or not isinstance(record.get("payload"), dict)
                ):
                    raise TypeError("record must contain string id and object payload")
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                logger.error(
                    "ReMe durable write queue %s line %d is retained but invalid: %s",
                    path,
                    line_number,
                    exc,
                )
                continue
            records.append(record)
        return records

    def _remove_pending_write(self, record_id: str) -> bool:
        path = self._pending_path
        if path is None or not path.is_file():
            logger.error("ReMe queue disappeared before acknowledgement: %s", path)
            return False
        if path.is_symlink() or path.parent.is_symlink():
            logger.error("Refusing unsafe ReMe queue acknowledgement path: %s", path)
            return False
        with self._pending_lock:
            try:
                lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
                kept = []
                removed = False
                for line in lines:
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        kept.append(line)
                        continue
                    if isinstance(record, dict) and record.get("id") == record_id:
                        removed = True
                    else:
                        kept.append(line)
                if not removed:
                    return True
                fd, temp_name = tempfile.mkstemp(
                    prefix=f".{path.name}.",
                    dir=path.parent,
                )
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as handle:
                        handle.writelines(kept)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.chmod(temp_name, 0o600)
                    os.replace(temp_name, path)
                    _fsync_directory(path.parent)
                finally:
                    if os.path.exists(temp_name):
                        os.unlink(temp_name)
            except OSError as exc:
                logger.error(
                    "ReMe could not acknowledge durable write %s: %s",
                    record_id,
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
                            respect_cooldown=False,
                        ) or not self._remove_pending_write(payload["id"]):
                            retry = True
                    except Exception:
                        logger.exception(
                            "Unexpected ReMe recording failure; durable write remains queued",
                        )
                        retry = True
                    if retry:
                        logger.warning(
                            "ReMe durable write %s remains queued for a later retry",
                            payload.get("id", "unknown"),
                        )
                        self._schedule_retry(payload)
                finally:
                    if payload is not None:
                        with self._write_thread_lock:
                            self._queued_record_ids.discard(payload.get("id", ""))
                    write_queue.task_done()
        finally:
            current = threading.current_thread()
            with self._write_thread_lock:
                if self._write_thread is current:
                    self._write_thread = None

    def _record_payload(
        self,
        payload: dict[str, Any],
        *,
        request_id: str | None = None,
        respect_cooldown: bool = True,
    ) -> bool:
        if respect_cooldown and time.monotonic() < self._next_write_attempt:
            logger.warning(
                "ReMe write for session %s skipped during cooldown",
                payload["session_id"],
            )
            return False
        with self._backend_lock:
            if not self._ensure_backend(allow_shutdown=True):
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
                    raise ReMeBackendError(
                        str(
                            (
                                response.get("answer")
                                if isinstance(response, dict)
                                else "auto_memory returned a non-object response"
                            ),
                        )
                        or "auto_memory did not report success",
                    )
                return True
            except ReMeBackendError as exc:
                self._next_write_attempt = time.monotonic() + self._health_retry_seconds
                logger.warning(
                    "ReMe recording failed at %s: %s",
                    self._backend_label,
                    exc,
                )
            finally:
                self._close_backend_if_shutdown_locked()
        return False

    def _schedule_retry(self, record: dict[str, Any]) -> None:
        """Retry one failed record later without blocking unrelated queued records."""
        delay = max(0.01, self._next_write_attempt - time.monotonic())
        timer: threading.Timer

        def enqueue() -> None:
            with self._write_thread_lock:
                self._retry_timers.discard(timer)
                if self._shutdown_started:
                    return
                self._queued_record_ids.discard(record["id"])
                self._queue_pending_records((record,))
                self._start_writer_if_needed()

        timer = threading.Timer(delay, enqueue)
        timer.daemon = True
        with self._write_thread_lock:
            if self._shutdown_started:
                return
            self._retry_timers.add(timer)
        timer.start()

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
                except (ReMeBackendError, TypeError, ValueError, OSError) as exc:
                    failed, self._backend = self._backend, None
                    if failed is not None:
                        try:
                            cleanup_timeout = self._shutdown_timeout
                            if deadline is not None:
                                cleanup_timeout = max(0.0, deadline - time.monotonic())
                            if deadline is None or cleanup_timeout > 0:
                                failed.close(timeout=cleanup_timeout)
                        except ReMeBackendError as close_exc:
                            logger.warning(
                                "Failed to clean up ReMe after startup error: %s",
                                close_exc,
                            )
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
            self._close_backend_locked()
            self._deferred_backend_close = False

    def _close_backend_locked(self, *, timeout: float | None = None) -> None:
        backend, self._backend = self._backend, None
        if backend is not None:
            try:
                backend.close(
                    timeout=self._shutdown_timeout if timeout is None else timeout,
                )
            except ReMeBackendError as exc:
                logger.warning(
                    "ReMe backend shutdown failed at %s: %s",
                    self._backend_label,
                    exc,
                )
        self._backend_available = False

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
