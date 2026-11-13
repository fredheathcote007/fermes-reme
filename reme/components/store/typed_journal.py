"""Crash-consistent typed-memory batch commits.

The journal is intentionally a small storage adapter rather than a component.
It owns the workspace lock, the write-ahead JSONL record, immutable update
snapshots, durable receipts, and restart reconciliation.  The public API
accepts either ``TypedMemoryRecord`` instances or dictionaries containing a
record and the optional ``action``/``expected_old_digest`` fields.  Keeping
that boundary here prevents the model-facing step from acquiring filesystem
privileges or inventing its own commit ordering.
"""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import inspect
import json
import os
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping

try:
    import fcntl
except ImportError:  # pragma: no cover - exercised on native Windows
    fcntl = None

from ...schema.typed_memory import (
    OptionBReceiptCommit,
    TypedMemoryRecord,
    TypedOptionBReceipt,
    canonical_jcs_bytes,
    render_markdown_body,
    sha256_canonical,
)
from ...steps.file_io._file_io import unlink_file_durable, write_file_durable

_TYPED_NOTE_RE = re.compile(r"^memory/typed/(semantic|episodic|procedural|failure_lesson)/[A-Za-z0-9_-]+\.md$")
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_HISTORY_RE = re.compile(r"\.rev([0-9]+)\.")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")


class TypedJournalError(RuntimeError):
    """Base class for fail-closed journal and transaction errors."""


class JournalConflictError(TypedJournalError):
    """The request or batch identity was reused with another payload."""

    status_code = 409


class JournalCorruptionError(TypedJournalError):
    """The journal contains non-trailing corruption or an unsafe intent."""


class _PendingItem:
    """Normalized, validated input used by the transaction and recovery code."""

    __slots__ = (
        "action",
        "body",
        "body_sha256",
        "created_before",
        "expected_old_digest",
        "history_path",
        "memory_id",
        "note_path",
        "record_digest",
        "record_type",
        "revision_seq",
    )

    def __init__(
        self,
        *,
        action: str,
        body: bytes,
        body_sha256: str,
        created_before: bool,
        expected_old_digest: str | None,
        history_path: str | None,
        memory_id: str,
        note_path: str,
        record_digest: str,
        record_type: str,
        revision_seq: int,
    ) -> None:
        self.action = action
        self.body = body
        self.body_sha256 = body_sha256
        self.created_before = created_before
        self.expected_old_digest = expected_old_digest
        self.history_path = history_path
        self.memory_id = memory_id
        self.note_path = note_path
        self.record_digest = record_digest
        self.record_type = record_type
        self.revision_seq = revision_seq

    def journal_dict(self) -> dict[str, Any]:
        """Return the immutable intent summary for this typed record."""
        result: dict[str, Any] = {
            "memory_id": self.memory_id,
            "record_type": self.record_type,
            "note_path": self.note_path,
            "body_sha256": self.body_sha256,
            "record_digest": self.record_digest,
            "revision_seq": self.revision_seq,
            "action": self.action,
        }
        if self.expected_old_digest is not None:
            result["expected_old_digest"] = self.expected_old_digest
        if self.history_path is not None:
            result["prior_history_path"] = self.history_path
        return result


def _utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_line(entry: Mapping[str, Any]) -> bytes:
    return canonical_jcs_bytes(dict(entry)) + b"\n"


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _safe_scope_id(value: str, field: str) -> str:
    if not isinstance(value, str) or not _SAFE_ID_RE.fullmatch(value):
        raise ValueError(f"{field} must contain only ASCII letters, digits, '_' or '-' and be 1..64 characters")
    return value


def _safe_relative(relative: str) -> None:
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise ValueError("journal path must be a non-empty POSIX relative path")
    path = Path(relative)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in relative.split("/")):
        raise ValueError(f"unsafe journal path: {relative!r}")


class IntentJournal:
    """Write-ahead journal and durable transaction coordinator for typed notes."""

    def __init__(self, workspace_dir: str | Path) -> None:
        self.workspace = Path(workspace_dir).absolute()
        if not self.workspace.exists() or not self.workspace.is_dir():
            raise ValueError(f"workspace_dir must be an existing directory: {workspace_dir!r}")
        self.reme_dir = self.workspace / ".reme"
        self.journal_path = self.reme_dir / "intent_journal.jsonl"
        self.lock_path = self.reme_dir / "commit.lock"
        self.history_dir = self.reme_dir / "history"
        self.receipts_dir = self.reme_dir / "receipts"
        self._journal_was_truncated = False

    @contextmanager
    def _locked(self) -> Iterator[None]:
        with self.lock():
            yield

    @contextmanager
    def lock(self, *, blocking: bool = True) -> Iterator[None]:
        """Hold the workspace advisory lock.

        ``blocking=False`` is useful to callers proving that a second writer
        cannot enter a live commit.  Production commits always use the
        blocking form so retries serialize rather than spuriously fail.
        """
        if fcntl is None or os.name != "posix" or not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
            raise OSError("durable local POSIX file commits are unavailable on this platform")
        self._ensure_directory(self.reme_dir)
        flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW
        fd = os.open(self.lock_path, flags, 0o600)
        try:
            operation = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
            fcntl.flock(fd, operation)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def _ensure_metadata_dirs(self) -> None:
        self._ensure_directory(self.reme_dir)
        self._ensure_directory(self.history_dir)
        self._ensure_directory(self.receipts_dir)

    @staticmethod
    def _ensure_directory(path: Path) -> None:
        if path.exists() and path.is_symlink():
            raise JournalCorruptionError(f"journal directory contains a symlink: {path}")
        path.mkdir(parents=True, exist_ok=True)
        if not path.is_dir():
            raise JournalCorruptionError(f"journal path is not a directory: {path}")

    def _confined(self, relative: str, *, allow_reme: bool = False) -> Path:
        _safe_relative(relative)
        target = self.workspace / relative
        current = self.workspace
        for component in Path(relative).parts:
            current = current / component
            if current.is_symlink():
                raise JournalCorruptionError(f"symlink is not permitted in journal path: {relative!r}")
        resolved = target.absolute()
        if not resolved.is_relative_to(self.workspace):
            raise JournalCorruptionError(f"journal path escapes workspace: {relative!r}")
        if not allow_reme and relative.startswith(".reme/"):
            raise JournalCorruptionError("typed note path may not be under .reme")
        return target

    def _note_path(self, relative: str) -> Path:
        if not _TYPED_NOTE_RE.fullmatch(relative):
            raise ValueError(f"invalid typed note path: {relative!r}")
        return self._confined(relative)

    def _receipt_path(self, request_id: str) -> Path:
        _safe_scope_id(request_id, "request_id")
        return self._confined(f".reme/receipts/{request_id}.json", allow_reme=True)

    def _history_path(self, relative: str) -> Path:
        return self._confined(relative, allow_reme=True)

    def _read_entries_locked(self, *, repair_trailing: bool = True) -> list[dict[str, Any]]:
        self._journal_was_truncated = False
        if not self.journal_path.exists():
            return []
        if self.journal_path.is_symlink():
            raise JournalCorruptionError("intent journal is a symlink")
        raw = self.journal_path.read_bytes()
        entries: list[dict[str, Any]] = []
        seen_pending: set[str] = set()
        terminal_batches: set[str] = set()
        offset = 0
        lines = raw.splitlines(keepends=True)
        for line in lines:
            if not line.endswith(b"\n"):
                if repair_trailing:
                    self._truncate_journal(offset)
                    break
                raise JournalCorruptionError("intent journal has an unterminated line")
            candidate = line[:-1]
            try:
                value = json.loads(candidate.decode("utf-8"))
                if not isinstance(value, dict) or canonical_jcs_bytes(value) != candidate:
                    raise ValueError("line is not RFC 8785 canonical JSON")
            except (UnicodeError, RecursionError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise JournalCorruptionError(f"interior intent journal corruption at byte {offset}") from exc
            status = value.get("status")
            batch_id = value.get("batch_id")
            if not isinstance(status, str) or status not in {"pending", "committed", "rolled_back"}:
                raise JournalCorruptionError(f"unknown intent journal status at byte {offset}")
            if not isinstance(batch_id, str) or not _SAFE_ID_RE.fullmatch(batch_id):
                raise JournalCorruptionError(f"invalid journal batch_id at byte {offset}")
            if status == "pending":
                request = value.get("request_id")
                records = value.get("records")
                if batch_id in seen_pending or batch_id in terminal_batches:
                    raise JournalCorruptionError(f"duplicate pending journal batch at byte {offset}")
                if not isinstance(request, str) or not _SAFE_ID_RE.fullmatch(request):
                    raise JournalCorruptionError(f"invalid pending journal request at byte {offset}")
                if not isinstance(records, list) or not 1 <= len(records) <= 100:
                    raise JournalCorruptionError(f"invalid pending journal record count at byte {offset}")
                if value.get("n_records", len(records)) != len(records):
                    raise JournalCorruptionError(f"pending journal record count mismatch at byte {offset}")
                for item in records:
                    if not isinstance(item, dict):
                        raise JournalCorruptionError(f"invalid pending journal record at byte {offset}")
                    if item.get("action") not in {"create", "update"}:
                        raise JournalCorruptionError(f"invalid pending journal action at byte {offset}")
                    if not isinstance(item.get("note_path"), str):
                        raise JournalCorruptionError(f"invalid pending journal path at byte {offset}")
                    for field in ("body_sha256", "record_digest"):
                        digest = item.get(field)
                        if not isinstance(digest, str) or not _HEX64_RE.fullmatch(digest):
                            raise JournalCorruptionError(f"invalid pending journal {field} at byte {offset}")
                seen_pending.add(batch_id)
            else:
                if batch_id not in seen_pending or batch_id in terminal_batches:
                    raise JournalCorruptionError(f"invalid journal status transition at byte {offset}")
                if status == "committed" and not (
                    isinstance(value.get("receipt_digest"), str) and _HEX64_RE.fullmatch(value["receipt_digest"])
                ):
                    raise JournalCorruptionError(f"committed journal entry lacks receipt digest at byte {offset}")
                terminal_batches.add(batch_id)
            entries.append(value)
            offset += len(line)
        return entries

    def _truncate_journal(self, offset: int) -> None:
        self._journal_was_truncated = True
        flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW
        fd = os.open(self.journal_path, flags, 0o600)
        try:
            os.ftruncate(fd, offset)
            os.fsync(fd)
        finally:
            os.close(fd)
        self._fsync_directory(self.reme_dir)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        if os.name != "posix" or not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"):
            raise OSError("durable local POSIX file commits are unavailable on this platform")
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _append_locked(self, entry: Mapping[str, Any]) -> None:
        self._ensure_directory(self.reme_dir)
        line = _canonical_line(entry)
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW
        fd = os.open(self.journal_path, flags, 0o600)
        try:
            view = memoryview(line)
            while view:
                view = view[os.write(fd, view) :]
            os.fsync(fd)
        finally:
            os.close(fd)
        self._fsync_directory(self.reme_dir)

    @staticmethod
    def _disk_digest(path: Path) -> str:
        return _sha256_bytes(path.read_bytes())

    def _known_record_digest_matches(self, relative: str, expected: str, raw: bytes) -> bool:
        """Match a prior committed record commitment without trusting note text."""
        entries = self._read_entries_locked()
        statuses = {
            str(entry.get("batch_id")): entry.get("status")
            for entry in entries
            if entry.get("status") in {"committed", "rolled_back"}
        }
        body_digest = _sha256_bytes(raw)
        for entry in reversed(entries):
            if entry.get("status") != "pending" or statuses.get(str(entry.get("batch_id"))) != "committed":
                continue
            for item in entry.get("records", []):
                if (
                    isinstance(item, Mapping)
                    and item.get("note_path") == relative
                    and item.get("record_digest") == expected
                    and item.get("body_sha256") == body_digest
                ):
                    return True
        return False

    @staticmethod
    def _digest_matches(path: Path, expected: str) -> bool:
        if not _HEX64_RE.fullmatch(expected):
            return False
        raw = path.read_bytes()
        if _sha256_bytes(raw) == expected:
            return True
        # A typed note may carry the record commitment in an application-owned
        # marker.  Accepting it makes update admission compatible with both the
        # pure renderer and frontmatter-backed note fixtures.
        marker_match = re.search(rb"(?:record_digest|recordDigest)\s*[:=]\s*[\"']?([0-9a-f]{64})", raw)
        return bool(marker_match and marker_match.group(1).decode("ascii") == expected)

    def _normalize_item(self, item: Any) -> _PendingItem:
        item_data = dict(item) if isinstance(item, Mapping) else {}
        record_value = item_data.get("record", item)
        if isinstance(record_value, TypedMemoryRecord):
            record = record_value
        else:
            record = TypedMemoryRecord.model_validate(record_value)
        body_value = item_data.get("body")
        body = (
            render_markdown_body(record).encode("utf-8")
            if body_value is None
            else (body_value.encode("utf-8") if isinstance(body_value, str) else bytes(body_value))
        )
        if _sha256_bytes(body) != record.body_sha256:
            raise ValueError("typed note body does not match body_sha256")
        note_path = record.note_path
        self._note_path(note_path)
        target = self.workspace / note_path
        exists = target.exists()
        # A bare record is a create request.  This is important for retries:
        # the journal lookup must happen before the create precondition sees
        # the already-published note and mistakes an idempotent replay for an
        # update lacking an expected_old_digest.  Updates are explicit.
        action = item_data.get("action", "create")
        if action not in {"create", "update"}:
            raise ValueError("typed batch action must be 'create' or 'update'")
        expected = item_data.get("expected_old_digest")
        if action == "update" and (not exists or not isinstance(expected, str)):
            raise ValueError("update requires an existing note and expected_old_digest")
        if expected is not None and not _HEX64_RE.fullmatch(expected):
            raise ValueError("expected_old_digest must be a lowercase SHA-256")
        return _PendingItem(
            action=action,
            body=body,
            body_sha256=record.body_sha256,
            created_before=exists,
            expected_old_digest=expected,
            history_path=None,
            memory_id=record.memory_id,
            note_path=note_path,
            record_digest=record.record_digest,
            record_type=record.record_type,
            revision_seq=record.revision_seq,
        )

    @staticmethod
    def _payload_digest(scope: Mapping[str, str], items: Iterable[_PendingItem]) -> str:
        return sha256_canonical({"scope": dict(scope), "records": [item.journal_dict() for item in items]})

    def _history_sequence(self) -> int:
        highest = 0
        for path in self.history_dir.glob("*.bak"):
            match = _HISTORY_RE.search(path.name)
            if match:
                highest = max(highest, int(match.group(1)))
        return highest + 1

    def _plan_update_snapshots(
        self,
        items: list[_PendingItem],
        sequence: int,
    ) -> list[tuple[_PendingItem, Path, bytes]]:
        """Plan history paths without mutating storage before the pending intent."""
        planned: list[tuple[_PendingItem, Path, bytes]] = []
        for item in items:
            if item.action != "update":
                continue
            target = self._note_path(item.note_path)
            old = target.read_bytes()
            expected = item.expected_old_digest or ""
            if not self._digest_matches(target, expected) and not self._known_record_digest_matches(
                item.note_path,
                expected,
                old,
            ):
                raise JournalConflictError(f"expected_old_digest does not match {item.note_path}")
            digest = _sha256_bytes(old)
            backup = self.history_dir / f"{item.memory_id}.rev{sequence}.{digest[:8]}.bak"
            while backup.exists():
                sequence += 1
                backup = self.history_dir / f"{item.memory_id}.rev{sequence}.{digest[:8]}.bak"
            item.history_path = f".reme/history/{backup.name}"
            planned.append((item, backup, old))
            sequence += 1
        return planned

    async def _persist_update_snapshots(
        self,
        planned: list[tuple[_PendingItem, Path, bytes]],
    ) -> None:
        """Fsync immutable old bytes only after their pending intent is durable."""
        for item, backup, old in planned:
            target = self._note_path(item.note_path)
            if not target.is_file() or target.is_symlink() or target.read_bytes() != old:
                raise JournalConflictError("update target changed before history snapshot")
            if backup.exists():
                raise JournalConflictError("reserved immutable history path already exists")
            await write_file_durable(backup, old)

    def _validate_preconditions(self, items: list[_PendingItem]) -> None:
        """Re-check admission conditions after taking the workspace lock."""
        paths: set[str] = set()
        for item in items:
            if item.note_path in paths:
                raise ValueError(f"typed batch contains duplicate note_path: {item.note_path}")
            paths.add(item.note_path)
            target = self._note_path(item.note_path)
            exists = target.exists()
            if item.action == "create" and exists:
                raise JournalConflictError(f"typed note already exists: {item.note_path}")
            if item.action == "update":
                if not exists or target.is_symlink():
                    raise JournalConflictError(f"typed note disappeared before update: {item.note_path}")
                expected = item.expected_old_digest or ""
                if not self._digest_matches(target, expected) and not self._known_record_digest_matches(
                    item.note_path,
                    expected,
                    target.read_bytes(),
                ):
                    raise JournalConflictError(f"expected_old_digest does not match {item.note_path}")

    async def _restore_update(self, item: Mapping[str, Any]) -> None:
        history = item.get("prior_history_path")
        if not isinstance(history, str):
            raise JournalCorruptionError("pending update has no immutable history snapshot")
        backup = self._history_path(history)
        target = self._note_path(str(item["note_path"]))
        if not target.is_file() or target.is_symlink():
            raise JournalConflictError("pending update target is missing or unsafe")
        previous_sha = item.get("prior_body_sha256")
        if previous_sha is not None and (not isinstance(previous_sha, str) or not _HEX64_RE.fullmatch(previous_sha)):
            raise JournalCorruptionError("pending update history digest is malformed")
        if not backup.is_file() or backup.is_symlink():
            if previous_sha and _sha256_bytes(target.read_bytes()) == previous_sha:
                return  # Crash after pending, before immutable history was created.
            raise JournalCorruptionError(f"history snapshot is missing: {history}")
        original = backup.read_bytes()
        if previous_sha is not None and _sha256_bytes(original) != previous_sha:
            raise JournalCorruptionError("immutable history snapshot digest mismatch")
        expected_old = item.get("expected_old_digest")
        if isinstance(expected_old, str) and not (
            _sha256_bytes(original) == expected_old
            or self._digest_matches(backup, expected_old)
            or self._known_record_digest_matches(str(item["note_path"]), expected_old, original)
        ):
            raise JournalCorruptionError("immutable history snapshot does not match pending intent")
        current = target.read_bytes()
        if current == original:
            return
        if _sha256_bytes(current) != item.get("body_sha256"):
            raise JournalConflictError("pending update target changed independently; refusing rollback")
        await write_file_durable(target, original)

    async def _remove_create_if_owned(self, item: Mapping[str, Any]) -> None:
        target = self._note_path(str(item["note_path"]))
        if not target.exists():
            return
        if target.is_symlink():
            raise JournalCorruptionError(f"created typed note became a symlink: {target}")
        if not self._digest_matches(target, str(item["body_sha256"])):
            # Never remove a path whose bytes are no longer those published by
            # this transaction.  This preserves unrelated or externally
            # replaced notes; update targets take the opposite path below and
            # are restored, never unlinked.
            return
        await unlink_file_durable(target)

    async def _rollback_entry(self, entry: Mapping[str, Any]) -> None:
        for item in entry.get("records", []):
            if not isinstance(item, Mapping):
                raise JournalCorruptionError("intent record is malformed")
            if item.get("action") == "create":
                await self._remove_create_if_owned(item)
            elif item.get("action") == "update":
                await self._restore_update(item)
            else:
                raise JournalCorruptionError("intent action is malformed")
        self._append_locked({"batch_id": entry["batch_id"], "status": "rolled_back"})

    def _load_receipt(self, request_id: str) -> TypedOptionBReceipt | None:
        path = self._receipt_path(request_id)
        if not path.exists():
            return None
        if path.is_symlink():
            raise JournalCorruptionError("receipt is a symlink")
        try:
            raw = path.read_bytes()
            receipt = TypedOptionBReceipt.model_validate_json(raw)
            if canonical_jcs_bytes(receipt.model_dump(mode="json")) != raw:
                raise ValueError("receipt is not RFC 8785 canonical JSON")
            if receipt.request_id != request_id:
                raise ValueError("receipt request_id does not match its storage key")
            return receipt
        except ValueError as exc:
            if "receipt request_id does not match" in str(exc):
                raise JournalCorruptionError(str(exc)) from exc
            raise JournalCorruptionError("durable typed receipt is malformed") from exc
        except (OSError, RecursionError, TypeError) as exc:
            raise JournalCorruptionError("durable typed receipt is malformed") from exc

    @staticmethod
    def _receipt_matches_entry(receipt: TypedOptionBReceipt, entry: Mapping[str, Any]) -> bool:
        if "receipt_digest" in entry and receipt.receipt_digest != entry.get("receipt_digest"):
            return False
        if any(
            receipt.model_dump(mode="json").get(key) != entry.get(key)
            for key in ("batch_id", "request_id", "session_id", "workspace_id", "profile_id")
        ):
            return False
        if receipt.n_records != entry.get("n_records"):
            return False
        journal_records = entry.get("records")
        if not isinstance(journal_records, list):
            return False
        commits = [commit.model_dump(mode="json") for commit in receipt.commits]
        expected = [
            {
                "memory_id": item.get("memory_id"),
                "record_type": item.get("record_type"),
                "record_digest": item.get("record_digest"),
                "body_sha256": item.get("body_sha256"),
                "note_path": item.get("note_path"),
                "status": "active",
                "revision_seq": item.get("revision_seq", 1),
                "created": item.get("action") == "create",
            }
            for item in journal_records
        ]
        return commits == expected

    async def _notes_match_entry(self, entry: Mapping[str, Any]) -> bool:
        for item in entry.get("records", []):
            try:
                path = self._note_path(str(item["note_path"]))
                if not path.is_file() or path.is_symlink():
                    return False
                if _sha256_bytes(path.read_bytes()) != item.get("body_sha256"):
                    return False
            except (KeyError, OSError, TypeError, ValueError, JournalCorruptionError):
                return False
        return True

    async def _reconcile_locked(self) -> list[TypedOptionBReceipt]:
        entries = self._read_entries_locked()
        if self._journal_was_truncated:
            # A torn final line is an unsealed intent.  Preserve all complete
            # history and discard only the incomplete transaction fragment.
            # Complete pending entries before the torn suffix still require
            # normal CW-4 reconciliation.
            self._cleanup_temporary_files()
        states: dict[str, str] = {}
        pending: dict[str, dict[str, Any]] = {}
        for entry in entries:
            status = entry.get("status")
            batch_id = entry.get("batch_id")
            if not isinstance(batch_id, str):
                raise JournalCorruptionError("journal entry has no batch_id")
            if status == "pending":
                pending[batch_id] = entry
                states[batch_id] = status
            elif status in {"committed", "rolled_back"}:
                states[batch_id] = status
        for batch_id, entry in pending.items():
            if states.get(batch_id) != "pending":
                continue
            request_id = entry.get("request_id")
            if not isinstance(request_id, str):
                raise JournalCorruptionError("pending journal entry has no request_id")
            receipt_error: JournalCorruptionError | None = None
            try:
                receipt = self._load_receipt(request_id)
            except JournalCorruptionError as exc:
                # A receipt that is absent or malformed is the CW-4a path:
                # roll back the journaled notes rather than trusting it.
                receipt = None
                receipt_error = exc
            if (
                receipt is not None
                and self._receipt_matches_entry(receipt, entry)
                and await self._notes_match_entry(entry)
            ):
                self._append_locked(
                    {"batch_id": batch_id, "status": "committed", "receipt_digest": receipt.receipt_digest},
                )
                states[batch_id] = "committed"
            else:
                await self._rollback_entry(entry)
                states[batch_id] = "rolled_back"
                if receipt_error is not None and "request_id does not match" not in str(receipt_error):
                    raise receipt_error
        self._cleanup_temporary_files()
        committed_batches = {batch_id for batch_id, status in states.items() if status == "committed"}
        receipts: list[TypedOptionBReceipt] = []
        for batch_id in committed_batches:
            entry = pending.get(batch_id)
            if entry is None:
                continue
            receipt = self._load_receipt(str(entry["request_id"]))
            if receipt is not None:
                receipts.append(receipt)
        return receipts

    def _cleanup_temporary_files(self) -> None:
        roots = [root for root in (self.reme_dir, self.workspace / "memory" / "typed") if root.exists()]
        for scan_root in roots:
            for root, dirs, files in os.walk(scan_root, followlinks=False):
                dirs[:] = [name for name in dirs if not Path(root, name).is_symlink()]
                for name in files:
                    if not (name.endswith(".tmp") or ".tmp." in name):
                        continue
                    path = Path(root, name)
                    if path.is_symlink():
                        raise JournalCorruptionError(f"temporary journal path is a symlink: {path}")
                    path.unlink()
        self._fsync_directory(self.reme_dir)

    async def reconcile(self) -> list[TypedOptionBReceipt]:
        """Repair a trailing torn line and resolve every pending transaction."""
        if not self.journal_path.exists():
            return []
        async with asyncio.Lock():
            with self._locked():
                return await self._reconcile_locked()

    def reconcile_sync(self) -> None:
        """Synchronous startup entry point used by :class:`Application`."""
        if not self.journal_path.exists():
            return
        with self._locked():
            asyncio.run(self._reconcile_locked())

    def recover(self) -> None:
        """Synchronous compatibility name for startup reconciliation."""
        self.reconcile_sync()

    scan_and_recover = recover
    startup_recover = recover

    def append(self, entry: Mapping[str, Any]) -> None:
        """Append one already-validated canonical journal object.

        This low-level seam is intentionally available for recovery tools and
        crash-injection probes; normal typed commits use :meth:`commit_batch`.
        """
        if not isinstance(entry, Mapping):
            raise TypeError("journal entry must be a mapping")
        with self._locked():
            self._append_locked(entry)

    append_pending = append
    append_intent = append

    def replay(
        self,
        request_id: str,
        batch_id: str | None = None,
        payload: Any = None,
        *,
        session_id: str | None = None,
        workspace_id: str | None = None,
        profile_id: str | None = None,
    ) -> TypedOptionBReceipt:
        """Return an existing durable receipt without rewriting it.

        Receipts are always parsed through the B.1 Pydantic model.  A caller
        supplying ``payload`` must supply the exact journal payload digest (or
        the exact stored payload object for diagnostic callers); otherwise a
        request collision is rejected without any write.
        """
        with self._locked():
            receipt = self._load_receipt(request_id)
            if receipt is None:
                raise FileNotFoundError(self._receipt_path(request_id))
            if receipt.request_id != request_id:
                raise JournalCorruptionError("durable receipt request_id does not match the replay request")
            if batch_id is not None and receipt.batch_id != batch_id:
                raise JournalConflictError("Batch/Request collision with conflicting payload")
            for field, expected in (
                ("session_id", session_id),
                ("workspace_id", workspace_id),
                ("profile_id", profile_id),
            ):
                if expected is not None and getattr(receipt, field) != expected:
                    raise JournalConflictError("typed receipt scope does not match the replay scope")
            entries = self._read_entries_locked()
            statuses = {
                str(entry.get("batch_id")): entry.get("status")
                for entry in entries
                if entry.get("status") in {"committed", "rolled_back"}
            }
            pending = next(
                (
                    entry
                    for entry in entries
                    if entry.get("status") == "pending"
                    and entry.get("request_id") == request_id
                    and statuses.get(str(entry.get("batch_id"))) == "committed"
                ),
                None,
            )
            if pending is None or not self._receipt_matches_entry(receipt, pending):
                raise JournalCorruptionError("durable receipt has no matching committed intent")
            if payload is not None:
                matches = [
                    entry
                    for entry in entries
                    if entry.get("status") == "committed"
                    and entry.get("request_id") == request_id
                    and (batch_id is None or entry.get("batch_id") == batch_id)
                ]
                if not matches or not any(
                    payload == candidate
                    for candidate in (matches[-1].get("payload_digest"), matches[-1].get("payload"))
                ):
                    raise JournalConflictError("409 Conflict: Batch/Request collision with conflicting payload")
            return receipt

    replay_request = replay
    load_receipt = replay

    async def _find_replay_locked(
        self,
        *,
        request_id: str,
        batch_id: str,
        payload_digest: str,
        scope: Mapping[str, str],
    ) -> TypedOptionBReceipt | None:
        entries = self._read_entries_locked()
        committed_batches = {
            str(entry.get("batch_id")): entry for entry in entries if entry.get("status") == "committed"
        }
        for entry in entries:
            if entry.get("status") != "pending":
                continue
            if entry.get("request_id") == request_id or entry.get("batch_id") == batch_id:
                await self._reconcile_locked()
                entries = self._read_entries_locked()
                break
        status_by_batch = {
            str(entry.get("batch_id")): entry.get("status")
            for entry in entries
            if entry.get("status") in {"committed", "rolled_back"}
        }
        committed_batches = {
            str(entry.get("batch_id")): entry
            for entry in entries
            if entry.get("status") == "pending" and status_by_batch.get(str(entry.get("batch_id"))) == "committed"
        }
        for entry in committed_batches.values():
            if entry.get("request_id") != request_id and entry.get("batch_id") != batch_id:
                continue
            receipt = self._load_receipt(str(entry.get("request_id")))
            if receipt is None:
                raise JournalCorruptionError("committed intent has no durable receipt")
            if not self._receipt_matches_entry(receipt, entry):
                raise JournalCorruptionError("committed receipt does not match its intent")
            same_scope = all(entry.get(key) == scope.get(key) for key in scope)
            if same_scope and entry.get("payload_digest") == payload_digest:
                return receipt
            raise JournalConflictError("Batch/Request collision with conflicting payload")
        return None

    async def commit_batch(
        self,
        request_id: str,
        batch_id: str,
        session_id: str,
        workspace_id: str,
        profile_id: str,
        records: Iterable[Any],
        *,
        index_status: str | None = "ok",
        indexer: Callable[[list[dict[str, Any]]], Any] | None = None,
        committed_at_utc: str | None = None,
    ) -> TypedOptionBReceipt:
        """Durably publish one typed batch and return its immutable receipt.

        ``indexer`` is an application-owned synchronous or asynchronous hook.
        It runs after note publication and before receipt persistence.  A hook
        failure rolls the notes back, so the transaction cannot claim a
        successful authoritative index update.
        """
        return await self._commit_batch_async(
            request_id,
            batch_id,
            session_id,
            workspace_id,
            profile_id,
            records,
            index_status=index_status,
            indexer=indexer,
            committed_at_utc=committed_at_utc,
        )

    async def _commit_batch_async(
        self,
        request_id: str,
        batch_id: str,
        session_id: str,
        workspace_id: str,
        profile_id: str,
        records: Iterable[Any],
        *,
        index_status: str | None,
        indexer: Callable[[list[dict[str, Any]]], Any] | None,
        committed_at_utc: str | None,
    ) -> TypedOptionBReceipt:
        if os.name != "posix" or not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
            raise OSError("durable local POSIX file commits are unavailable on this platform")
        if index_status not in {None, "ok", "degraded"}:
            raise ValueError("index_status must be None, 'ok', or 'degraded'")
        scope = {
            "request_id": _safe_scope_id(request_id, "request_id"),
            "batch_id": _safe_scope_id(batch_id, "batch_id"),
            "session_id": session_id,
            "workspace_id": workspace_id,
            "profile_id": profile_id,
        }
        if not all(isinstance(scope[key], str) and scope[key] for key in ("session_id", "workspace_id", "profile_id")):
            raise ValueError("session_id, workspace_id, and profile_id are required")
        normalized = [self._normalize_item(item) for item in records]
        if not 1 <= len(normalized) <= 100:
            raise ValueError("typed batch must contain 1..100 records")
        if len({item.memory_id for item in normalized}) != len(normalized):
            raise ValueError("typed batch contains duplicate memory_id values")
        payload_digest = self._payload_digest(scope, normalized)

        with self._locked():
            self._ensure_metadata_dirs()
            replay = await self._find_replay_locked(
                request_id=request_id,
                batch_id=batch_id,
                payload_digest=payload_digest,
                scope=scope,
            )
            if replay is not None:
                return replay
            if indexer is None:
                raise TypedJournalError("authoritative typed indexing must be supplied before commit")
            sequence = self._history_sequence()
            self._validate_preconditions(normalized)
            planned = self._plan_update_snapshots(normalized, sequence)
            old_shas = {item.note_path: _sha256_bytes(old) for item, _path, old in planned}
            commits = [
                {
                    "memory_id": item.memory_id,
                    "record_type": item.record_type,
                    "record_digest": item.record_digest,
                    "body_sha256": item.body_sha256,
                    "note_path": item.note_path,
                    "status": "active",
                    "revision_seq": item.revision_seq,
                    "created": item.action == "create",
                }
                for item in normalized
            ]
            timestamp = committed_at_utc or _utc_now()
            receipt_values = {
                "outcome": "typed_batch",
                "batch_id": batch_id,
                "request_id": request_id,
                "session_id": session_id,
                "workspace_id": workspace_id,
                "profile_id": profile_id,
                "n_records": len(normalized),
                "commits": commits,
                "index_status": index_status,
                "committed_at_utc": timestamp,
            }
            receipt_values["receipt_digest"] = sha256_canonical(receipt_values)
            receipt = TypedOptionBReceipt.model_validate(receipt_values)
            entry = {
                **scope,
                "timestamp_utc": timestamp,
                "status": "pending",
                "n_records": len(normalized),
                "receipt_digest": receipt.receipt_digest,
                "payload_digest": payload_digest,
                "records": [
                    {
                        **item.journal_dict(),
                        **({"prior_body_sha256": old_shas[item.note_path]} if item.action == "update" else {}),
                    }
                    for item in normalized
                ],
            }
            self._append_locked(entry)
            try:
                await self._persist_update_snapshots(planned)
                for item in normalized:
                    await write_file_durable(self._note_path(item.note_path), item.body)
                if indexer is not None:
                    result = indexer(commits)
                    if inspect.isawaitable(result):
                        await result
                await write_file_durable(
                    self._receipt_path(request_id),
                    canonical_jcs_bytes(receipt.model_dump(mode="json")),
                )
            except BaseException:
                try:
                    await self._rollback_entry(entry)
                except BaseException as rollback_error:
                    raise TypedJournalError(
                        "typed batch failed and safe rollback could not complete",
                    ) from rollback_error
                raise
            # Once the receipt is durable, leave the pending intent in place
            # if this final journal fsync fails.  Startup reconciliation must
            # be able to roll the complete batch forward (CW-4b).
            self._append_locked(
                {"batch_id": batch_id, "status": "committed", "receipt_digest": receipt.receipt_digest},
            )
            return receipt

    def commit_batch_sync(self, *args, **kwargs) -> TypedOptionBReceipt:
        """Run :meth:`commit_batch` from a synchronous caller."""
        return asyncio.run(self.commit_batch(*args, **kwargs))

    commit = commit_batch
    commit_typed_batch = commit_batch


TypedJournal = IntentJournal
BatchIntentJournal = IntentJournal
TypedIntentJournal = IntentJournal
TypedBatchIntentJournal = IntentJournal


def reconcile_workspace(workspace_dir: str | Path) -> None:
    """Reconcile pending typed transactions before a service accepts work."""
    IntentJournal(workspace_dir).reconcile_sync()


reconcile_intent_journal = reconcile_workspace


__all__ = [
    "BatchIntentJournal",
    "IntentJournal",
    "JournalConflictError",
    "JournalCorruptionError",
    "OptionBReceiptCommit",
    "TypedJournal",
    "TypedIntentJournal",
    "TypedBatchIntentJournal",
    "TypedJournalError",
    "reconcile_intent_journal",
    "reconcile_workspace",
]
