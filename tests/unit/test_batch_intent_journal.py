"""FHM007-B.3 contract tests for the typed batch intent journal.

These tests intentionally exercise the production journal module through its
normal package imports so the suite cannot pollute the interpreter module registry.
"""

# pylint: disable=missing-function-docstring,redefined-outer-name,inconsistent-return-statements,protected-access

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import rfc8785

try:
    import fcntl
except ImportError:  # Native Windows does not support POSIX flock.
    fcntl = None

from reme.components.store import typed_journal as journal_module
from reme.components.store.typed_journal import (
    IntentJournal,
    JournalConflictError,
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
from reme.application import Application
from reme import application as application_module
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
    return _method(
        obj,
        (
            "recover",
            "reconcile",
            "reconcile_sync",
            "scan_and_recover",
            "startup_recover",
        ),
    )()


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
        "payload_digest": "0" * 64,
        "receipt_digest": "1" * 64,
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
        key: value for key, value in draft.model_dump(mode="json", exclude_none=False).items() if key != "record_digest"
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


def test_application_start_reconciles_configured_workspace_for_partial_mock(monkeypatch, tmp_path: Path) -> None:
    configured_workspace = tmp_path / "configured-workspace"
    observed: list[Path] = []

    def reconcile(path: Path) -> None:
        observed.append(Path(path).absolute())

    async def complete_direct(func, *args):
        return func(*args)

    async def run() -> None:
        app = object.__new__(Application)
        context = SimpleNamespace(
            app_config=SimpleNamespace(
                thread_pool_max_workers=0,
                workspace_dir=str(configured_workspace),
            ),
            jobs={},
            thread_pool=None,
        )
        app.context = context
        app.app_context = context
        app._started_components = []
        app._component_mutation_lock = asyncio.Lock()
        app._topological_order = lambda: []

        await Application._start(app)

    monkeypatch.setattr(application_module, "complete_in_thread", complete_direct)
    monkeypatch.setattr(application_module, "reconcile_workspace", reconcile)
    asyncio.run(run())

    assert configured_workspace.absolute() != Path.cwd()
    assert observed == [configured_workspace.absolute()]


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
        indexer=lambda commits: None,
    )
    assert note_write_seen


def test_update_intent_is_durable_before_history_backup_write(journal, tmp_path: Path, monkeypatch) -> None:
    record = _record(tmp_path)
    note = tmp_path / record.note_path
    note.parent.mkdir(parents=True)
    previous = b"previous note revision\n"
    note.write_bytes(previous)
    journal_path = tmp_path / ".reme" / "intent_journal.jsonl"
    history_write_seen = False
    real_write = journal_module.write_file_durable

    async def observe_write(path, content, encoding="utf-8"):
        nonlocal history_write_seen
        if str(path).endswith(".bak"):
            history_write_seen = True
            pending = [json.loads(line) for line in journal_path.read_text().splitlines()]
            assert any(entry.get("status") == "pending" for entry in pending)
        return await real_write(path, content, encoding)

    monkeypatch.setattr(journal_module, "write_file_durable", observe_write)
    journal.commit_batch_sync(
        "request-1",
        "batch-1",
        "session-1",
        "workspace-1",
        "profile-1",
        [
            {
                "record": record,
                "action": "update",
                "expected_old_digest": hashlib.sha256(previous).hexdigest(),
            },
        ],
        indexer=lambda commits: None,
    )

    assert history_write_seen


def _hold_lock(workspace: str, ready, release) -> None:
    journal = IntentJournal(Path(workspace))
    with journal._locked():
        ready.set()
        release.wait(10)


@pytest.mark.skipif(fcntl is None or os.name != "posix", reason="requires POSIX flock/fork")
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
            indexer=lambda commits: None,
        )


def test_missing_authoritative_indexer_fails_before_publication(journal, tmp_path: Path) -> None:
    record = _record(tmp_path)
    for status in ("ok", "degraded", None):
        with pytest.raises(journal_module.TypedJournalError, match="indexing"):
            journal.commit_batch_sync(
                "request-1",
                "batch-1",
                "session-1",
                "workspace-1",
                "profile-1",
                [record],
                index_status=status,
            )
        assert not (tmp_path / record.note_path).exists()
        assert not (tmp_path / ".reme/receipts/request-1.json").exists()


def test_live_commit_fails_closed_on_unknown_journal_status(journal, tmp_path: Path) -> None:
    journal_path = tmp_path / ".reme" / "intent_journal.jsonl"
    journal_path.parent.mkdir(parents=True)
    journal_path.write_bytes(_canonical({"batch_id": "b-bad", "status": "bogus"}) + b"\n")
    record = _record(tmp_path)

    with pytest.raises(JournalCorruptionError):
        journal.commit_batch_sync(
            "request-1",
            "batch-1",
            "session-1",
            "workspace-1",
            "profile-1",
            [record],
        )

    assert not (tmp_path / record.note_path).exists()
    assert not (tmp_path / ".reme/receipts/request-1.json").exists()


def test_indexer_called_after_note_before_receipt(journal, tmp_path: Path) -> None:
    record = _record(tmp_path)
    seen = []

    def indexer(commits):
        assert (tmp_path / record.note_path).is_file()
        assert not (tmp_path / ".reme/receipts/request-1.json").exists()
        seen.extend(commits)

    receipt = journal.commit_batch_sync(
        "request-1",
        "batch-1",
        "session-1",
        "workspace-1",
        "profile-1",
        [record],
        indexer=indexer,
    )
    assert receipt.index_status == "ok"
    assert len(seen) == 1
    assert (tmp_path / ".reme/receipts/request-1.json").exists()


def test_indexer_failure_rolls_back_without_receipt(journal, tmp_path: Path) -> None:
    record = _record(tmp_path)

    def fail_indexer(_commits):
        raise RuntimeError("injected index update failure")

    with pytest.raises(RuntimeError, match="injected"):
        journal.commit_batch_sync(
            "request-1",
            "batch-1",
            "session-1",
            "workspace-1",
            "profile-1",
            [record],
            indexer=fail_indexer,
        )
    assert not (tmp_path / record.note_path).exists()
    assert not (tmp_path / ".reme/receipts/request-1.json").exists()


def test_pending_update_preserves_newer_unowned_note(journal, tmp_path: Path) -> None:
    rel = "memory/typed/semantic/mem_semantic_aaaaaaaaaaaaaaaa.md"
    target = tmp_path / rel
    target.parent.mkdir(parents=True)
    previous = b"prior known note\n"
    unrelated = b"newer independently modified note\n"
    target.write_bytes(unrelated)
    prior_sha = hashlib.sha256(previous).hexdigest()
    hist_rel = f".reme/history/mem_semantic_aaaaaaaaaaaaaaaa.rev1.{prior_sha[:8]}.bak"
    history = tmp_path / hist_rel
    history.parent.mkdir(parents=True)
    history.write_bytes(previous)
    journal.append(
        {
            "status": "pending",
            "batch_id": "batch-1",
            "request_id": "request-1",
            "session_id": "session-1",
            "workspace_id": "workspace-1",
            "profile_id": "profile-1",
            "payload_digest": "0" * 64,
            "receipt_digest": "1" * 64,
            "records": [
                {
                    "action": "update",
                    "memory_id": "mem_semantic_aaaaaaaaaaaaaaaa",
                    "record_type": "semantic",
                    "note_path": rel,
                    "record_digest": "a" * 64,
                    "body_sha256": "b" * 64,
                    "expected_old_digest": prior_sha,
                    "prior_history_path": hist_rel,
                },
            ],
        },
    )
    with pytest.raises(journal_module.JournalConflictError):
        journal.reconcile_sync()
    assert target.read_bytes() == unrelated


def test_write_ahead_pending_intent_precedes_immutable_update_snapshot(
    journal,
    tmp_path: Path,
    monkeypatch,
) -> None:
    record = _record(tmp_path)
    original = b"existing note before update\n"
    old_sha = hashlib.sha256(original).hexdigest()
    note = tmp_path / record.note_path
    note.parent.mkdir(parents=True)
    note.write_bytes(original)
    real_write = journal_module.write_file_durable
    observed: list[str] = []

    async def observe_write(path, content, *args, **kwargs):
        path = Path(path)
        if path.parent == tmp_path / ".reme" / "history":
            entries = [
                json.loads(line) for line in (tmp_path / ".reme" / "intent_journal.jsonl").read_text().splitlines()
            ]
            assert entries[0]["status"] == "pending"
            entry = entries[0]["records"][0]
            assert entry["prior_history_path"] == f".reme/history/{path.name}"
            assert entry["prior_body_sha256"] == old_sha
            observed.append("pending-before-history")
        return await real_write(path, content, *args, **kwargs)

    monkeypatch.setattr(journal_module, "write_file_durable", observe_write)
    journal.commit_batch_sync(
        "request1",
        "batch1",
        "session1",
        "workspace1",
        "profile1",
        [{"record": record, "action": "update", "expected_old_digest": old_sha}],
        indexer=lambda _: None,
    )
    assert observed == ["pending-before-history"]


def test_crash_after_pending_before_history_preserves_original_update(
    journal,
    tmp_path: Path,
    monkeypatch,
) -> None:
    record = _record(tmp_path)
    original = b"existing old note must survive pending-only crash\n"
    old_sha = hashlib.sha256(original).hexdigest()
    note = tmp_path / record.note_path
    note.parent.mkdir(parents=True)
    note.write_bytes(original)
    real_write = journal_module.write_file_durable

    async def fail_history_write(path, content, *args, **kwargs):
        if Path(path).parent == tmp_path / ".reme" / "history":
            raise OSError("simulated crash before immutable snapshot")
        return await real_write(path, content, *args, **kwargs)

    monkeypatch.setattr(journal_module, "write_file_durable", fail_history_write)
    with pytest.raises(OSError, match="simulated crash"):
        journal.commit_batch_sync(
            "request1",
            "batch1",
            "session1",
            "workspace1",
            "profile1",
            [{"record": record, "action": "update", "expected_old_digest": old_sha}],
            indexer=lambda _: None,
        )
    assert note.read_bytes() == original
    assert not (tmp_path / ".reme/receipts/request1.json").exists()
    statuses = [
        json.loads(line)["status"] for line in (tmp_path / ".reme" / "intent_journal.jsonl").read_text().splitlines()
    ]
    assert statuses == ["pending", "rolled_back"]


def test_live_update_race_after_snapshot_preserves_third_party_edit(
    journal,
    tmp_path: Path,
    monkeypatch,
) -> None:
    record = _record(tmp_path)
    original = b"original note before race\n"
    third_party = b"third-party edit after snapshot\n"
    old_sha = hashlib.sha256(original).hexdigest()
    note = tmp_path / record.note_path
    note.parent.mkdir(parents=True)
    note.write_bytes(original)
    real_write = journal_module.write_file_durable
    history_written = False

    async def race_after_snapshot(path, content, *args, **kwargs):
        nonlocal history_written
        path = Path(path)
        result = await real_write(path, content, *args, **kwargs)
        if path.parent == tmp_path / ".reme" / "history":
            history_written = True
            note.write_bytes(third_party)
        return result

    monkeypatch.setattr(journal_module, "write_file_durable", race_after_snapshot)
    with pytest.raises(JournalConflictError):
        journal.commit_batch_sync(
            "request-race-after",
            "batch-race-after",
            "session-race",
            "workspace-race",
            "profile-race",
            [{"record": record, "action": "update", "expected_old_digest": old_sha}],
            indexer=lambda _: None,
        )

    assert history_written
    assert note.read_bytes() == third_party
    statuses = [
        json.loads(line)["status"] for line in (tmp_path / ".reme" / "intent_journal.jsonl").read_text().splitlines()
    ]
    assert statuses == ["pending", "rolled_back"]


def test_live_update_race_before_snapshot_preserves_third_party_edit(
    journal,
    tmp_path: Path,
    monkeypatch,
) -> None:
    record = _record(tmp_path)
    original = b"original note before pre-snapshot race\n"
    third_party = b"third-party edit before snapshot\n"
    old_sha = hashlib.sha256(original).hexdigest()
    note = tmp_path / record.note_path
    note.parent.mkdir(parents=True)
    note.write_bytes(original)
    edited = False

    real_persist = journal._persist_update_snapshots

    async def race_before_snapshot(planned):
        nonlocal edited
        if not edited:
            edited = True
            note.write_bytes(third_party)
        return await real_persist(planned)

    monkeypatch.setattr(journal, "_persist_update_snapshots", race_before_snapshot)
    with pytest.raises(JournalConflictError):
        journal.commit_batch_sync(
            "request-race-before",
            "batch-race-before",
            "session-race",
            "workspace-race",
            "profile-race",
            [{"record": record, "action": "update", "expected_old_digest": old_sha}],
            indexer=lambda _: None,
        )

    assert edited
    assert note.read_bytes() == third_party
    statuses = [
        json.loads(line)["status"] for line in (tmp_path / ".reme" / "intent_journal.jsonl").read_text().splitlines()
    ]
    assert statuses == ["pending", "rolled_back"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("session_id", "bad space"),
        ("session_id", "../../"),
        ("session_id", ""),
        ("workspace_id", "bad space"),
        ("workspace_id", "../../"),
        ("workspace_id", ""),
        ("profile_id", "bad space"),
        ("profile_id", "../../"),
        ("profile_id", ""),
    ],
)
def test_pending_entry_rejects_invalid_scope_without_truncation(
    journal,
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    entry = _intent()
    entry[field] = value
    journal.append(entry)
    path = tmp_path / ".reme/intent_journal.jsonl"
    before = path.read_bytes()

    with pytest.raises(JournalCorruptionError):
        journal.reconcile_sync()

    assert path.read_bytes() == before


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("payload_digest", "bad"),
        ("payload_digest", "0" * 63),
        ("payload_digest", "g" * 64),
        ("payload_digest", ""),
        ("receipt_digest", "bad"),
        ("receipt_digest", "0" * 63),
        ("receipt_digest", "g" * 64),
        ("receipt_digest", ""),
    ],
)
def test_pending_entry_rejects_invalid_digest_without_truncation(
    journal,
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    entry = _intent()
    entry[field] = value
    journal.append(entry)
    path = tmp_path / ".reme/intent_journal.jsonl"
    before = path.read_bytes()

    with pytest.raises(JournalCorruptionError):
        journal.reconcile_sync()

    assert path.read_bytes() == before


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("memory_id", "bad space"),
        ("memory_id", "../../"),
        ("memory_id", ""),
        ("record_type", "unknown"),
        ("note_path", "../escape.md"),
        ("note_path", "memory/typed/semantic/foo/../bar.md"),
        ("note_path", "untyped/note.md"),
        ("note_path", "/abs.md"),
        ("note_path", ""),
    ],
)
def test_pending_entry_rejects_invalid_record_without_truncation(
    journal,
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    entry = _intent()
    entry["records"][0][field] = value
    journal.append(entry)
    path = tmp_path / ".reme/intent_journal.jsonl"
    before = path.read_bytes()

    with pytest.raises(JournalCorruptionError):
        journal.reconcile_sync()

    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "entry",
    [
        {"batch_id": "batch1", "status": "bogus"},
        {"batch_id": "batch1", "status": "pending", "request_id": "request1"},
        {
            "batch_id": "batch1",
            "status": "pending",
            "request_id": "request1",
            "records": "invalid",
        },
        {"batch_id": "batch1", "status": "rolled_back"},
        {"batch_id": "batch1", "status": "committed", "receipt_digest": "a" * 64},
    ],
)
def test_semantically_invalid_canonical_journal_fails_closed_without_truncation(
    journal,
    tmp_path: Path,
    entry: dict[str, Any],
) -> None:
    journal.append(entry)
    path = tmp_path / ".reme/intent_journal.jsonl"
    before = path.read_bytes()
    with pytest.raises(JournalCorruptionError):
        journal.reconcile_sync()
    assert path.read_bytes() == before


def test_duplicate_pending_or_terminal_transition_fails_closed(
    journal,
    tmp_path: Path,
) -> None:
    first = _intent(batch_id="batch1", request_id="request1")
    journal.append(first)
    journal.append(first)
    path = tmp_path / ".reme/intent_journal.jsonl"
    before = path.read_bytes()
    with pytest.raises(JournalCorruptionError):
        journal.reconcile_sync()
    assert path.read_bytes() == before
