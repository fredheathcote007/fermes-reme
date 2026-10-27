"""Focused tests for server-owned daily-note provenance."""

# These tests intentionally exercise private trust-boundary contracts.
# pylint: disable=missing-function-docstring,protected-access

import hashlib
from pathlib import Path

import frontmatter
import pytest

from reme.components import ApplicationContext
from reme.components.file_store import LocalFileStore
from reme.enumeration import ComponentEnum
from reme.steps.file_io._provenance import trusted_provenance
from reme.steps.file_io.daily_write import DailyWriteStep
from reme.steps.file_io.frontmatter_update import FrontmatterUpdateStep
from reme.steps.file_io.write import WriteStep

SESSION = "trusted-session"


def _seed_source(workspace: Path, content: bytes = b'{"role":"user"}\n') -> Path:
    source = workspace / "session" / "dialog" / f"{SESSION}.jsonl"
    source.parent.mkdir(parents=True)
    source.write_bytes(content)
    return source


async def _store() -> LocalFileStore:
    store = LocalFileStore(name="provenance", embedding_store="")
    await store.start()
    return store


def test_source_hash_is_exact_server_owned_transcript_hash(tmp_path):
    source = _seed_source(tmp_path, b"exact bytes\n")

    provenance = trusted_provenance(tmp_path, "session", SESSION)

    assert provenance["source_hash"] == {
        "algorithm": "sha256",
        "format": "hex",
        "value": hashlib.sha256(source.read_bytes()).hexdigest(),
    }


@pytest.mark.asyncio
async def test_write_replaces_model_provenance_with_trusted_values(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _seed_source(tmp_path)
    store = await _store()

    step = WriteStep(file_store=store)
    await step(
        path="daily/note.md",
        content="body",
        metadata={
            "session_id": "model-session",
            "source_conversation": "[[model]]",
            "source_hash": {"value": "model-hash"},
            "tag": "kept",
        },
        _provenance_session_id=SESSION,
    )

    post = frontmatter.loads((tmp_path / "daily/note.md").read_text(encoding="utf-8"))
    expected = trusted_provenance(tmp_path, "session", SESSION)
    assert step.context.response.success is True
    assert post.metadata["session_id"] == expected["session_id"]
    assert post.metadata["source_conversation"] == expected["source_conversation"]
    assert post.metadata["source_hash"] == expected["source_hash"]
    assert post.metadata["tag"] == "kept"
    await store.close()


@pytest.mark.asyncio
async def test_frontmatter_update_replaces_model_provenance_with_trusted_values(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _seed_source(tmp_path)
    note = tmp_path / "daily/note.md"
    note.parent.mkdir(parents=True)
    note.write_text(
        f"---\nname: note\nsession_id: {SESSION}\n"
        f"source_conversation: '[[session/dialog/{SESSION}.jsonl]]'\n---\nbody\n",
        encoding="utf-8",
    )
    store = await _store()

    step = FrontmatterUpdateStep(file_store=store)
    await step(
        path="daily/note.md",
        metadata={
            "source_conversation": "[[model]]",
            "source_hash": {"value": "model-hash"},
            "tag": "kept",
        },
        _provenance_session_id=SESSION,
    )

    post = frontmatter.loads(note.read_text(encoding="utf-8"))
    expected = trusted_provenance(tmp_path, "session", SESSION)
    assert step.context.response.success is True
    assert post.metadata["source_conversation"] == expected["source_conversation"]
    assert post.metadata["source_hash"] == expected["source_hash"]
    assert post.metadata["tag"] == "kept"
    await store.close()


@pytest.mark.asyncio
async def test_daily_write_does_not_overwrite_foreign_note(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _seed_source(tmp_path)
    foreign = tmp_path / "daily/2026-10-27/note.md"
    foreign.parent.mkdir(parents=True)
    original = "---\nsession_id: foreign\n---\nprotected\n"
    foreign.write_text(original, encoding="utf-8")
    store = await _store()
    app_context = ApplicationContext(workspace_dir=str(tmp_path))
    app_context.components[ComponentEnum.FILE_STORE] = {"default": store}

    step = DailyWriteStep(app_context=app_context)
    await step(
        name="note",
        description="updated",
        session_id=SESSION,
        content="model content",
        date="2026-10-27",
    )

    assert step.context.response.success is False
    assert "different source session" in step.context.response.answer
    assert foreign.read_text(encoding="utf-8") == original
    await store.close()


def test_verify_detects_transcript_changes_and_note_metadata_spoof(tmp_path):
    from reme.steps.file_io._provenance import verify_note_provenance

    source = _seed_source(tmp_path, b'{"role":"user","content":"first"}\n')
    note = tmp_path / "daily" / "2026-10-27" / "fact.md"
    note.parent.mkdir(parents=True)
    trusted = trusted_provenance(tmp_path, "session", SESSION)
    note.write_text(frontmatter.dumps(frontmatter.Post("fact", **trusted)), encoding="utf-8")
    assert verify_note_provenance(tmp_path, note, "session", SESSION)
    source.write_bytes(source.read_bytes() + b'{"role":"user","content":"second"}\n')
    assert not verify_note_provenance(tmp_path, note, "session", SESSION)
    note.write_text(
        frontmatter.dumps(frontmatter.Post("fact", **trusted_provenance(tmp_path, "session", SESSION))),
        encoding="utf-8",
    )
    assert verify_note_provenance(tmp_path, note, "session", SESSION)
    post = frontmatter.loads(note.read_text(encoding="utf-8"))
    post.metadata["session_id"] = "foreign"
    note.write_text(frontmatter.dumps(post), encoding="utf-8")
    assert not verify_note_provenance(tmp_path, note, "session", SESSION)


def test_provenance_rejects_symlinked_session_transcript(tmp_path):
    from reme.steps.file_io._provenance import ProvenanceError

    real = tmp_path / "outside"
    real.write_bytes(b"hostile")
    link = tmp_path / "session" / "dialog" / f"{SESSION}.jsonl"
    link.parent.mkdir(parents=True)
    link.symlink_to(real)
    with pytest.raises(ProvenanceError, match="symlink"):
        trusted_provenance(tmp_path, "session", SESSION)


@pytest.mark.asyncio
async def test_trusted_write_rejects_foreign_note_inside_path_lock(tmp_path, monkeypatch):
    """A note appearing after any outer ownership check cannot be overwritten."""
    monkeypatch.chdir(tmp_path)
    _seed_source(tmp_path)
    target = tmp_path / "daily" / "foreign.md"
    target.parent.mkdir(parents=True)
    original = "---\nsession_id: foreign\nsource_conversation: '[[session/dialog/foreign.jsonl]]'\n---\nprivate\n"
    target.write_text(original, encoding="utf-8")
    store = await _store()
    try:
        step = WriteStep(file_store=store)
        await step(
            path="daily/foreign.md",
            content="replacement",
            _provenance_session_id=SESSION,
        )
        assert not step.context.response.success
        assert "different source session" in step.context.response.answer
        assert target.read_text(encoding="utf-8") == original
    finally:
        await store.close()


def test_public_http_request_cannot_supply_trusted_provenance():
    from fastapi import HTTPException

    from reme.components.service.http_service import HttpService
    from reme.schema.request import Request

    with pytest.raises(HTTPException) as raised:
        HttpService._public_job_kwargs(
            Request.model_validate({"path": "daily/fact.md", "_provenance_session_id": SESSION}),
        )
    assert raised.value.status_code == 422
    assert HttpService._public_job_kwargs(Request.model_validate({"path": "daily/fact.md"})) == {
        "path": "daily/fact.md",
    }


@pytest.mark.asyncio
async def test_protected_frontmatter_update_cannot_claim_foreign_note(tmp_path, monkeypatch):
    """Protected tagging or metadata repair must not take over another session's file."""
    monkeypatch.chdir(tmp_path)
    _seed_source(tmp_path)
    note = tmp_path / "daily" / "foreign.md"
    note.parent.mkdir(parents=True)
    original = (
        "---\n"
        "session_id: different-session\n"
        "source_conversation: '[[session/dialog/different-session.jsonl]]'\n"
        "---\n"
        "private content\n"
    )
    note.write_text(original, encoding="utf-8")
    store = await _store()
    try:
        step = FrontmatterUpdateStep(file_store=store)
        await step(path="daily/foreign.md", metadata={"memory_tags": ["tag"]}, _provenance_session_id=SESSION)
        assert not step.context.response.success
        assert "different source session" in step.context.response.answer
        assert note.read_text(encoding="utf-8") == original
    finally:
        await store.close()
