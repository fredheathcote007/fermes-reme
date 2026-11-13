"""FHM007-B.3 crash-window, replay, rollback, and receipt-integrity tests."""

# pylint: disable=missing-function-docstring,redefined-outer-name

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest
import rfc8785

from reme.components.store import typed_journal as journal_module
from reme.components.store.typed_journal import (
    IntentJournal,
    JournalConflictError,
    JournalCorruptionError,
    TypedBatchIntentJournal,
    reconcile_workspace,
)
from reme.schema.typed_memory import (
    OptionBReceiptCommit,
    ProvenanceCitation,
    SemanticContent,
    TypedMemoryRecord,
    TypedOptionBReceipt,
    canonical_jcs_bytes,
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


def _append(obj, entry: dict[str, Any]) -> None:
    with obj._locked():
        obj._append_locked(entry)


def _recover(obj) -> Any:
    return _method(obj, ("recover", "reconcile", "reconcile_sync", "scan_and_recover", "startup_recover"))()


def _intent(
    *,
    batch_id: str = "batch-1",
    request_id: str = "request-1",
    session_id: str = "session-1",
    workspace_id: str = "workspace-1",
    profile_id: str = "profile-1",
    records: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
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
        "batch_id": batch_id,
        "request_id": request_id,
        "session_id": session_id,
        "workspace_id": workspace_id,
        "profile_id": profile_id,
        "timestamp_utc": "2026-11-13T01:02:03Z",
        "status": "pending",
        "n_records": len(records),
        "records": records,
    }


def _update_record(old: bytes) -> dict[str, Any]:
    old_digest = hashlib.sha256(old).hexdigest()
    return {
        "memory_id": "mem-semantic-1",
        "record_type": "semantic",
        "note_path": "memory/typed/semantic/mem-semantic-1.md",
        "action": "update",
        "record_digest": "a" * 64,
        "body_sha256": hashlib.sha256(b"new revision").hexdigest(),
        "expected_old_digest": old_digest,
        "prior_history_path": f".reme/history/mem-semantic-1.rev1.{old_digest[:8]}.bak",
    }


def _create_record(body: bytes = b"uncommitted create") -> dict[str, Any]:
    return {
        "memory_id": "mem-semantic-created",
        "record_type": "semantic",
        "note_path": "memory/typed/semantic/mem-semantic-created.md",
        "action": "create",
        "record_digest": "d" * 64,
        "body_sha256": hashlib.sha256(body).hexdigest(),
    }


def _valid_receipt(entry: dict[str, Any]):
    commits = [
        OptionBReceiptCommit(
            memory_id=item["memory_id"],
            record_type=item["record_type"],
            record_digest=item["record_digest"],
            body_sha256=item["body_sha256"],
            note_path=item["note_path"],
            status="active",
            revision_seq=1,
            created=item["action"] == "create",
        )
        for item in entry["records"]
    ]
    values = {
        "outcome": "typed_batch",
        "batch_id": entry["batch_id"],
        "request_id": entry["request_id"],
        "session_id": entry["session_id"],
        "workspace_id": entry["workspace_id"],
        "profile_id": entry["profile_id"],
        "n_records": len(commits),
        "commits": commits,
        "index_status": "ok",
        "committed_at_utc": entry["timestamp_utc"],
    }
    values["receipt_digest"] = sha256_canonical(values)
    return TypedOptionBReceipt(**values)


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


def _write_journal(workspace: Path, entries: list[dict[str, Any]], trailing: bytes = b"") -> Path:
    path = workspace / ".reme" / "intent_journal.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for entry in entries:
        encoded = rfc8785.dumps(entry)
        lines.append((encoded if isinstance(encoded, bytes) else encoded.encode()) + b"\n")
    path.write_bytes(b"".join(lines) + trailing)
    return path


@pytest.fixture
def journal(tmp_path: Path):
    return IntentJournal(tmp_path)


@pytest.fixture(autouse=True)
def direct_durable_file_execution(monkeypatch: pytest.MonkeyPatch):
    async def complete_direct(func, *args):
        return func(*args)

    monkeypatch.setattr(file_io_module, "complete_in_thread", complete_direct)


def test_public_journal_exports_are_the_production_contract() -> None:
    assert TypedBatchIntentJournal is IntentJournal
    assert reconcile_workspace is journal_module.reconcile_workspace
    assert JournalCorruptionError is journal_module.JournalCorruptionError


def test_cw1_trailing_torn_line_is_truncated_without_losing_prior_history(journal, tmp_path: Path) -> None:
    history = {"batch_id": "batch-history", "status": "committed"}
    path = _write_journal(tmp_path, [history], trailing=b'{"status":"pending"')
    expected = rfc8785.dumps(history)
    expected = expected if isinstance(expected, bytes) else expected.encode()
    _recover(journal)
    assert path.read_bytes() == expected + b"\n"


def test_torn_suffix_also_reconciles_surviving_pending_intent(journal, tmp_path: Path) -> None:
    entry = _intent()
    path = _write_journal(tmp_path, [entry], trailing=b'{"interrupted":')

    _recover(journal)

    statuses = [json.loads(line)["status"] for line in path.read_text().splitlines()]
    assert statuses == ["pending", "rolled_back"]
    assert path.read_bytes().endswith(b"\n")


def test_interior_corruption_fails_closed_and_preserves_journal_bytes(journal, tmp_path: Path) -> None:
    first = _intent(batch_id="batch-1", request_id="request-1")
    second = _intent(batch_id="batch-2", request_id="request-2")
    path = _write_journal(tmp_path, [first], trailing=b"not-json\n")
    encoded = rfc8785.dumps(second)
    path.write_bytes(path.read_bytes() + (encoded if isinstance(encoded, bytes) else encoded.encode()) + b"\n")
    before = path.read_bytes()
    with pytest.raises(JournalCorruptionError):
        _recover(journal)
    assert path.read_bytes() == before


def test_complete_malformed_line_fails_closed_without_truncation(journal, tmp_path: Path) -> None:
    path = tmp_path / ".reme" / "intent_journal.jsonl"
    path.parent.mkdir(parents=True)
    raw = b'{"ok":true}\nnot-json\n'
    path.write_bytes(raw)
    with pytest.raises(JournalCorruptionError):
        _recover(journal)
    assert path.read_bytes() == raw


@pytest.mark.parametrize("window", ["CW-2", "CW-3", "CW-4a"])
def test_receiptless_recovery_removes_only_intended_creates_and_restores_updates(
    window: str,
    journal,
    tmp_path: Path,
) -> None:
    old = b"pre-existing note bytes\n"
    note = tmp_path / "memory" / "typed" / "semantic" / "mem-semantic-1.md"
    note.parent.mkdir(parents=True)
    note.write_bytes(old)
    old_digest = hashlib.sha256(old).hexdigest()
    history = tmp_path / ".reme" / "history" / f"mem-semantic-1.rev1.{old_digest[:8]}.bak"
    history.parent.mkdir(parents=True)
    history.write_bytes(old)
    create = tmp_path / "memory" / "typed" / "semantic" / "mem-semantic-created.md"
    create.write_bytes(b"uncommitted create")
    unrelated = tmp_path / "memory" / "typed" / "semantic" / "unrelated.md"
    unrelated.write_bytes(b"unrelated pre-existing note")
    records = [_update_record(old), _create_record()]
    _write_journal(tmp_path, [_intent(batch_id=f"batch-{window}", records=records)])

    _recover(journal)

    assert note.read_bytes() == old, f"{window} changed the pre-existing update target"
    assert not create.exists()
    assert unrelated.read_bytes() == b"unrelated pre-existing note"


def test_cw2_unlinks_dangling_tmp_files_and_preserves_prior_notes(journal, tmp_path: Path) -> None:
    old = b"prior note"
    note = tmp_path / "memory" / "typed" / "semantic" / "mem-semantic-1.md"
    note.parent.mkdir(parents=True)
    note.write_bytes(old)
    old_digest = hashlib.sha256(old).hexdigest()
    history = tmp_path / ".reme" / "history" / f"mem-semantic-1.rev1.{old_digest[:8]}.bak"
    history.parent.mkdir(parents=True)
    history.write_bytes(old)
    dangling = note.with_name(note.name + ".tmp.1234")
    dangling.write_bytes(b"partial replacement")
    _write_journal(tmp_path, [_intent(records=[_update_record(old)])])

    _recover(journal)

    assert note.read_bytes() == old
    assert not dangling.exists()


def test_cw4a_missing_receipt_rolls_back_creates_and_restores_updates(journal, tmp_path: Path) -> None:
    old = b"old revision"
    note = tmp_path / "memory" / "typed" / "semantic" / "mem-semantic-1.md"
    note.parent.mkdir(parents=True)
    note.write_bytes(old)
    old_digest = hashlib.sha256(old).hexdigest()
    history = tmp_path / ".reme" / "history" / f"mem-semantic-1.rev1.{old_digest[:8]}.bak"
    history.parent.mkdir(parents=True)
    history.write_bytes(old)
    create = tmp_path / "memory" / "typed" / "semantic" / "mem-semantic-created.md"
    create.write_bytes(b"published without receipt")
    _write_journal(tmp_path, [_intent(records=[_update_record(old), _create_record(b"published without receipt")])])

    _recover(journal)

    assert note.read_bytes() == old
    assert not create.exists()


def test_cw3_partial_publication_never_unlinks_update_target(journal, tmp_path: Path) -> None:
    old = b"old revision"
    note = tmp_path / "memory" / "typed" / "semantic" / "mem-semantic-1.md"
    note.parent.mkdir(parents=True)
    note.write_bytes(old)
    old_digest = hashlib.sha256(old).hexdigest()
    history = tmp_path / ".reme" / "history" / f"mem-semantic-1.rev1.{old_digest[:8]}.bak"
    history.parent.mkdir(parents=True)
    history.write_bytes(old)
    create = tmp_path / "memory" / "typed" / "semantic" / "mem-semantic-created.md"
    create.write_bytes(b"partial create")
    records = [_update_record(old), _create_record(b"partial create")]
    _write_journal(tmp_path, [_intent(batch_id="batch-cw3", records=records)])

    _recover(journal)

    assert note.exists()
    assert note.read_bytes() == old
    assert not create.exists()


def test_cw4b_reconciles_valid_receipt_to_committed_without_rewriting_notes(
    journal,
    tmp_path: Path,
) -> None:
    body = b"durable note"
    record = _create_record(body)
    entry = _intent(records=[record])
    note = tmp_path / record["note_path"]
    note.parent.mkdir(parents=True)
    note.write_bytes(body)
    receipt = _valid_receipt(entry)
    entry["receipt_digest"] = receipt.receipt_digest
    _append(journal, entry)
    receipt_path = tmp_path / ".reme" / "receipts" / "request-1.json"
    receipt_path.parent.mkdir(parents=True)
    receipt_path.write_bytes(canonical_jcs_bytes(receipt.model_dump(mode="json")))
    before = note.stat().st_mtime_ns

    _recover(journal)

    assert note.stat().st_mtime_ns == before
    journal_lines = (tmp_path / ".reme" / "intent_journal.jsonl").read_text().splitlines()
    statuses = [json.loads(line)["status"] for line in journal_lines]
    assert statuses[-1] == "committed"


def test_cw5_replay_returns_persisted_receipt_without_a_second_publication(journal, tmp_path: Path) -> None:
    record = _record(tmp_path)
    first = journal.commit_batch_sync("request-1", "batch-1", "session-1", "workspace-1", "profile-1", [record])
    note = tmp_path / record.note_path
    before = note.read_bytes()
    replay = journal.commit_batch_sync("request-1", "batch-1", "session-1", "workspace-1", "profile-1", [record])
    assert replay == first
    assert note.read_bytes() == before


def test_same_request_replays_valid_immutable_receipt_without_rewriting(journal, tmp_path: Path) -> None:
    record = _record(tmp_path)
    first = journal.commit_batch_sync("request-1", "batch-1", "session-1", "workspace-1", "profile-1", [record])
    note = tmp_path / record.note_path
    before = note.stat().st_mtime_ns
    replay = journal.commit_batch_sync("request-1", "batch-1", "session-1", "workspace-1", "profile-1", [record])
    assert replay.receipt_digest == first.receipt_digest
    assert note.stat().st_mtime_ns == before


def test_divergent_payload_is_409_with_zero_mutation(journal, tmp_path: Path) -> None:
    first_record = _record(tmp_path)
    journal.commit_batch_sync("request-1", "batch-1", "session-1", "workspace-1", "profile-1", [first_record])
    note = tmp_path / first_record.note_path
    before = note.read_bytes()
    with pytest.raises(JournalConflictError) as exc_info:
        journal.commit_batch_sync(
            "request-1",
            "batch-1",
            "session-1",
            "workspace-1",
            "profile-1",
            [_record(tmp_path, fact="The café is closed")],
        )
    assert exc_info.value.status_code == 409
    assert note.read_bytes() == before


@pytest.mark.parametrize(
    ("field", "value"),
    [("session_id", "session-other"), ("workspace_id", "workspace-other"), ("profile_id", "profile-other")],
)
def test_cross_scope_replay_is_rejected_without_mutation(
    field: str,
    value: str,
    journal,
    tmp_path: Path,
) -> None:
    record = _record(tmp_path)
    journal.commit_batch_sync("request-1", "batch-1", "session-1", "workspace-1", "profile-1", [record])
    note = tmp_path / record.note_path
    before = note.read_bytes()
    scope = {"session_id": "session-1", "workspace_id": "workspace-1", "profile_id": "profile-1"}
    scope[field] = value
    with pytest.raises(JournalConflictError):
        journal.commit_batch_sync(
            "request-1",
            "batch-1",
            scope["session_id"],
            scope["workspace_id"],
            scope["profile_id"],
            [record],
        )
    assert note.read_bytes() == before


def test_tampered_valid_receipt_rolls_back_pending_create(journal, tmp_path: Path) -> None:
    body = b"durable note"
    entry = _intent(records=[_create_record(b"different note")])
    note = tmp_path / entry["records"][0]["note_path"]
    note.parent.mkdir(parents=True)
    note.write_bytes(body)
    receipt = _valid_receipt(entry)
    entry["receipt_digest"] = receipt.receipt_digest
    _append(journal, entry)
    receipt_path = tmp_path / ".reme" / "receipts" / "request-1.json"
    receipt_path.parent.mkdir(parents=True)
    receipt_path.write_bytes(canonical_jcs_bytes(receipt.model_dump(mode="json")))

    _recover(journal)

    assert note.read_bytes() == body
    journal_lines = (tmp_path / ".reme" / "intent_journal.jsonl").read_text().splitlines()
    statuses = [json.loads(line)["status"] for line in journal_lines]
    assert statuses[-1] == "rolled_back"


@pytest.mark.parametrize("kind", ["embedded_request", "tampered_digest", "incomplete"])
def test_malformed_receipt_fails_closed_without_claiming_commit(
    kind: str,
    journal,
    tmp_path: Path,
) -> None:
    entry = _intent()
    _append(journal, entry)
    receipt_path = tmp_path / ".reme" / "receipts" / "request-1.json"
    receipt_path.parent.mkdir(parents=True)
    valid = _valid_receipt(entry).model_dump(mode="json")
    if kind == "embedded_request":
        valid["request_id"] = "request-other"
        valid["receipt_digest"] = sha256_canonical(
            {key: value for key, value in valid.items() if key != "receipt_digest"},
        )
        raw = canonical_jcs_bytes(valid)
    elif kind == "tampered_digest":
        valid["receipt_digest"] = "0" * 64
        raw = canonical_jcs_bytes(valid)
    else:
        raw = b'{"request_id":"request-1","batch_id":"batch-1"}'
    receipt_path.write_bytes(raw)

    if kind == "embedded_request":
        _recover(journal)
    else:
        with pytest.raises(JournalCorruptionError):
            _recover(journal)
    journal_lines = (tmp_path / ".reme" / "intent_journal.jsonl").read_text().splitlines()
    statuses = [json.loads(line)["status"] for line in journal_lines]
    assert statuses[-1] == "rolled_back"
    assert "committed" not in statuses


def test_windows_protected_commit_fails_closed(journal, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(os, "name", "nt")
    with pytest.raises(OSError, match="POSIX"):
        journal.commit_batch_sync(
            "request-1",
            "batch-1",
            "session-1",
            "workspace-1",
            "profile-1",
            [_record(tmp_path)],
        )


def test_shipping_provider_has_no_typed_ack_bypass_knob() -> None:
    source = Path(__file__).resolve().parents[2] / "integrations" / "hermes_agent" / "__init__.py"
    text = source.read_text(encoding="utf-8")
    assert "_allow_test_typed_ack" not in text
    assert "typed_batch receipts require Task B.3 crash-consistent intent journal" in text
    assert "live typed ACK is disabled" in text
