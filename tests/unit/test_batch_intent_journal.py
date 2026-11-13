"""FHM007-B.3 contract tests for the typed batch intent journal.

These tests intentionally exercise the production journal module through its
normal package imports so the suite cannot pollute the interpreter module registry.
"""

# pylint: disable=missing-function-docstring,redefined-outer-name

from __future__ import annotations

import datetime as dt
import fcntl
import json
import multiprocessing
import os
from pathlib import Path
from typing import Any

import pytest
import rfc8785

from reme.components.store import typed_journal as journal_module
from reme.components.store.typed_journal import (
    IntentJournal,
    JournalCorruptionError,
    TypedBatchIntentJournal,
    reconcile_workspace,
)
from reme.schema.typed_memory import (
    ProvenanceCitation,
    SemanticContent,
    TypedMemoryRecord,
    compute_body_sha256,
    sha256_canonical,
)
from reme.steps.file_io import _file_io as file_io_module


def _method(obj, names: tuple[str, ...]):
    for name in names:
        method = getattr(obj, name, None)
        if callable(method):
            return method
    pytest.fail(f"journal object does not expose any of: {', '.join(names)}")


def _call_entry(obj, entry: dict[str, Any]) -> Any:
    append = getattr(obj, "_append_locked", None)
    locked = getattr(obj, "_locked", None)
    if callable(append) and callable(locked):
        with locked():
            return append(entry)
    method = _method(obj, ("append_pending", "append_intent", "append"))
    try:
        return method(entry)
    except TypeError:
        return method(intent=entry)


def _recover(obj) -> Any:
    return _method(obj, ("recover", "reconcile", "reconcile_sync", "scan_and_recover", "startup_recover"))()


def _canonical(value: Any) -> bytes:
    encoded = rfc8785.dumps(value)
    return encoded if isinstance(encoded, bytes) else encoded.encode("utf-8")


def _intent(*, batch_id: str = "batch-1", request_id: str = "request-1", records=None) -> dict[str, Any]:
    if records is None:
        records = [
            {
                "memory_id": "mem-semantic-1",
                "record_type": "semantic",
                "note_path": "memory/typed/semantic/mem-semantic-1.md",
                "action": "create",
                "record_digest": "a" * 64,
                "body_sha256": "b" * 64,
            },
        ]
    return {
        "intent_id": f"intent-{batch_id}",
        "batch_id": batch_id,
        "request_id": request_id,
        "session_id": "session-1",
        "workspace_id": "workspace-1",
        "profile_id": "profile-1",
        "timestamp_utc": "2026-11-13T01:02:03Z",
        "status": "pending",
        "n_records": len(records),
        "records": records,
    }


def _record(workspace: Path, *, fact: str = "The café is open"):
    content = SemanticContent(
        fact=fact,
        entity="cafe",
        attribute="status",
        value="open",
        valid_from=dt.date(2026, 1, 1),
    )
    content_hash = sha256_canonical(content.model_dump(mode="json", exclude_none=False))
    values = {
        "memory_id": f"mem_semantic_{content_hash[:16]}",
        "record_type": "semantic",
        "workspace_id": "workspace-1",
        "profile_id": "profile-1",
        "project_id": "project-1",
        "typed_content": content,
        "provenance": [
            ProvenanceCitation(
                source_uri=f"file://{workspace}/source.txt",
                session_id="session-1",
                verbatim_quote="quoted evidence",
                line_start=1,
                line_end=1,
            ),
        ],
        "content_hash": content_hash,
        "body_sha256": "0" * 64,
        "record_digest": "0" * 64,
        "note_path": f"memory/typed/semantic/mem_semantic_{content_hash[:16]}.md",
        "created_at_utc": "2026-11-13T01:02:03Z",
        "updated_at_utc": "2026-11-13T01:02:03Z",
    }
    draft = TypedMemoryRecord.model_construct(**values)
    values["body_sha256"] = compute_body_sha256(draft)
    draft = TypedMemoryRecord.model_construct(**values)
    digest_input = {
        key: value
        for key, value in draft.model_dump(mode="json", exclude_none=False).items()
        if key != "record_digest"
    }
    values["record_digest"] = sha256_canonical(digest_input)
    return TypedMemoryRecord(**values)


@pytest.fixture
def journal(tmp_path: Path):
    return IntentJournal(tmp_path)


@pytest.fixture(autouse=True)
def direct_durable_file_execution(monkeypatch: pytest.MonkeyPatch):
    async def complete_direct(func, *args):
        return func(*args)

    monkeypatch.setattr(file_io_module, "complete_in_thread", complete_direct)


def test_pending_line_is_rfc8785_canonical_and_newline_terminated(journal, tmp_path: Path) -> None:
    entry = _intent()
    _call_entry(journal, entry)

    journal_path = tmp_path / ".reme" / "intent_journal.jsonl"
    lines = journal_path.read_bytes().splitlines(keepends=True)
    assert lines == [_canonical(entry) + b"\n"]
    assert json.loads(lines[0]) == entry


def test_public_journal_exports_are_the_production_contract() -> None:
    assert TypedBatchIntentJournal is IntentJournal
    assert reconcile_workspace is journal_module.reconcile_workspace
    assert JournalCorruptionError is journal_module.JournalCorruptionError


def test_journal_fsync_precedes_any_note_write(journal, tmp_path: Path, monkeypatch) -> None:
    note_write_seen = False
    real_write = journal_module.write_file_durable
    journal_path = tmp_path / ".reme" / "intent_journal.jsonl"

    async def observe_write(path, content, encoding="utf-8"):
        nonlocal note_write_seen
        if str(path).endswith(".md"):
            note_write_seen = True
            pending = [json.loads(line) for line in journal_path.read_text().splitlines()]
            assert pending[0]["status"] == "pending"
        return await real_write(path, content, encoding)

    monkeypatch.setattr(journal_module, "write_file_durable", observe_write)
    journal.commit_batch_sync(
        "request-1",
        "batch-1",
        "session-1",
        "workspace-1",
        "profile-1",
        [_record(tmp_path)],
    )
    assert note_write_seen


def _hold_lock(workspace: str, ready, release) -> None:
    journal = IntentJournal(Path(workspace))
    with journal._locked():
        ready.set()
        release.wait(10)


def test_multiple_writers_are_serialized_by_advisory_lock(tmp_path: Path) -> None:
    journal = IntentJournal(tmp_path)
    context = multiprocessing.get_context("fork")
    ready = context.Event()
    release = context.Event()
    process = context.Process(target=_hold_lock, args=(str(tmp_path), ready, release))
    process.start()
    try:
        assert ready.wait(5)
        fd = os.open(journal.lock_path, os.O_RDWR | os.O_CREAT)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)
    finally:
        release.set()
        process.join(5)
        assert process.exitcode == 0


def test_failed_durable_primitives_fail_closed(journal, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "fsync", lambda _fd: (_ for _ in ()).throw(OSError("fsync failed")))
    with pytest.raises((OSError, RuntimeError)):
        journal.commit_batch_sync(
            "request-1",
            "batch-1",
            "session-1",
            "workspace-1",
            "profile-1",
            [_record(journal.workspace)],
        )
