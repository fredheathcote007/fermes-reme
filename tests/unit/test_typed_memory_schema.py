"""Focused contract tests for FHM007-B.1 typed memory schemas."""

from __future__ import annotations

import datetime as dt
import ast
import hashlib
import importlib.util
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

_SCHEMA_SPEC = importlib.util.spec_from_file_location(
    "reme.schema.typed_memory", Path(__file__).parents[2] / "reme" / "schema" / "typed_memory.py"
)
assert _SCHEMA_SPEC is not None and _SCHEMA_SPEC.loader is not None
_SCHEMA = importlib.util.module_from_spec(_SCHEMA_SPEC)
sys.modules["reme.schema.typed_memory"] = _SCHEMA
sys.modules["_fhm007_typed_memory_schema"] = _SCHEMA
_SCHEMA_SPEC.loader.exec_module(_SCHEMA)

from _fhm007_typed_memory_schema import (  # noqa: E402
    EpisodicContent,
    FailureLessonContent,
    OptionBReceiptCommit,
    ProceduralContent,
    ProvenanceCitation,
    SemanticContent,
    TypedMemoryProposal,
    TypedMemoryRecord,
    TypedOptionBReceipt,
    admit_source_uri,
    canonical_jcs_bytes,
    compute_body_sha256,
    format_field_val,
    render_markdown_body,
    sha256_canonical,
)


def _citation(workspace: Path, session_id: str = "Sess-ABC") -> ProvenanceCitation:
    return ProvenanceCitation(
        source_uri=f"file://{workspace}/source.txt",
        session_id=session_id,
        verbatim_quote="quoted evidence — café",
        line_start=1,
        line_end=2,
    )


def _record(workspace: Path, content) -> TypedMemoryRecord:
    content_hash = sha256_canonical(content)
    record_type = content.record_type
    memory_id = f"mem_{record_type}_{content_hash[:16]}"
    values = {
        "memory_id": memory_id,
        "record_type": record_type,
        "workspace_id": "workspace-1",
        "profile_id": None,
        "project_id": "project-1",
        "typed_content": content,
        "provenance": [_citation(workspace)],
        "content_hash": content_hash,
        "body_sha256": "0" * 64,
        "record_digest": "0" * 64,
        "note_path": f"memory/typed/{record_type}/{memory_id}.md",
        "created_at_utc": "2026-11-13T01:02:03Z",
        "updated_at_utc": "2026-11-13T01:02:03Z",
    }
    draft = TypedMemoryRecord.model_construct(**values)
    values["body_sha256"] = compute_body_sha256(draft)
    draft = TypedMemoryRecord.model_construct(**values)
    digest_input = {key: value for key, value in draft.model_dump(mode="json", exclude_none=False).items() if key != "record_digest"}
    values["record_digest"] = sha256_canonical(digest_input)
    return TypedMemoryRecord(**values)


@pytest.mark.parametrize(
    "content",
    [
        SemanticContent(fact="The café is open", entity="cafe", attribute="status", value="open", valid_from=dt.date(2026, 1, 1)),
        EpisodicContent(
            event_summary="A deployment completed",
            occurred_at=dt.datetime(2026, 11, 13, 1, 2, 3, tzinfo=dt.timezone.utc),
            actors=["agent", "operator"],
            action="deploy",
            outcome="success",
        ),
        ProceduralContent(
            task_name="deploy_app",
            prerequisites=["tests-green"],
            parameters={"environment": "staging"},
            steps=["build", "verify"],
            verification_command="curl https://example.invalid/health",
            rollback_steps=["restore"],
        ),
        FailureLessonContent(
            failure_symptom="The service stopped",
            trigger="A malformed config",
            root_cause="Parser rejected input",
            root_cause_status="verified",
            verification_receipt_id="receipt_1",
            immediate_remediation="Restore the prior config",
            prevention_rule="Validate before deploy",
        ),
    ],
)
def test_all_categories_round_trip_and_commitments(tmp_path: Path, content) -> None:
    record = _record(tmp_path, content)
    restored = TypedMemoryRecord.model_validate_json(record.model_dump_json())
    assert restored == record
    assert restored.memory_id == f"mem_{content.record_type}_{record.content_hash[:16]}"
    assert restored.note_path == f"memory/typed/{content.record_type}/{restored.memory_id}.md"
    assert restored.record_digest != record.content_hash


def test_jcs_is_stable_and_preserves_nulls_and_negative_zero() -> None:
    assert canonical_jcs_bytes({"b": 1, "a": None, "negative_zero": -0.0}) == b'{"a":null,"b":1,"negative_zero":0}'
    assert sha256_canonical({"a": 1}) == sha256_canonical({"a": 1})


def test_markdown_rendering_distinguishes_null_empty_and_unicode(tmp_path: Path) -> None:
    content = SemanticContent(fact="line 1\nline 2", value="")
    record = _record(tmp_path, content)
    body = render_markdown_body(record)
    assert '- value: ""' in body
    assert "- entity: <null>" in body
    assert r"line 1\\nline 2" in body
    assert body.endswith("\n")
    assert compute_body_sha256(record) == record.body_sha256
    assert format_field_val(dt.date(2026, 11, 13)) == "2026-11-13"
    assert format_field_val(dt.datetime(2026, 11, 13, 1, 2, tzinfo=dt.timezone.utc)) == "2026-11-13T01:02:00+00:00"


def test_record_is_frozen_and_tampering_is_rejected(tmp_path: Path) -> None:
    record = _record(tmp_path, SemanticContent(fact="stable"))
    with pytest.raises(ValidationError):
        record.status = "retracted"
    with pytest.raises(ValidationError, match="content_hash mismatch"):
        TypedMemoryRecord.model_validate({**record.model_dump(), "content_hash": "f" * 64})


def test_source_uri_admission_confines_workspace_and_session_context(tmp_path: Path) -> None:
    file_uri = f"file://{tmp_path}/source.txt"
    assert admit_source_uri(file_uri, workspace_dir=tmp_path) == tmp_path / "source.txt"
    session_id = "Sess-ABC"
    authority = hashlib.sha256(session_id.encode()).hexdigest()
    session_uri = f"session://{authority}/dialog.jsonl"
    assert admit_source_uri(session_uri, workspace_dir=tmp_path, session_id=session_id) == (
        tmp_path / ".reme" / "sessions" / authority / "dialog.jsonl"
    )
    outside = tmp_path.parent / "outside.txt"
    with pytest.raises(ValueError, match="escapes"):
        admit_source_uri(f"file://{outside}", workspace_dir=tmp_path)
    link = tmp_path / "link"
    link.symlink_to(outside)
    with pytest.raises(ValueError, match="Symlink rejected"):
        admit_source_uri(f"file://{link}/child.txt", workspace_dir=tmp_path)
    with pytest.raises(ValueError, match="authority mismatch"):
        admit_source_uri(f"session://{'a' * 64}/dialog.jsonl", workspace_dir=tmp_path, session_id=session_id)
    with pytest.raises(ValueError, match="escapes workspace_dir"):
        admit_source_uri("file:///etc/passwd", workspace_dir=tmp_path)
    for uri in (f"file://{tmp_path}/../outside", f"file://{tmp_path}/%2e%2e/outside"):
        with pytest.raises(ValueError):
            admit_source_uri(uri, workspace_dir=tmp_path)
    for missing_session_id in (None, ""):
        with pytest.raises(ValueError, match="session_id"):
            admit_source_uri(session_uri, workspace_dir=tmp_path, session_id=missing_session_id)
    with pytest.raises(ValueError, match="existing directory|does not exist"):
        admit_source_uri(file_uri, workspace_dir=tmp_path / "missing")


def test_uri_syntax_is_structural_only() -> None:
    assert _SCHEMA.validate_workspace_containment("file:///workspace/source.txt")
    assert _SCHEMA.validate_workspace_containment(f"session://{'a' * 64}/dialog.jsonl")
    for uri in (
        "http://example.invalid/a",
        "file:///workspace/../outside",
        "file:///workspace/%2e%2e/outside",
        "file://server/share/file",
        "file:///workspace/C:%5CWindows%5Csystem32",
    ):
        with pytest.raises(ValueError):
            _SCHEMA.validate_workspace_containment(uri)

def test_direct_note_adapters_are_not_exported_or_callable() -> None:
    schema_init = Path(__file__).parents[2] / "reme" / "schema" / "__init__.py"
    tree = ast.parse(schema_init.read_text(encoding="utf-8"))
    exported: set[str] = set()
    imported: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets):
            exported = set(ast.literal_eval(node.value))
        if isinstance(node, ast.ImportFrom) and node.module == "typed_memory":
            imported.update(alias.asname or alias.name for alias in node.names)
    for name in ("safe_write_note", "admit_and_commit_record", "admit_and_load_record", "safe_open_source"):
        assert name not in exported
        assert name not in imported
        assert not hasattr(_SCHEMA, name)


def test_invalid_ranges_receipts_and_statuses_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        SemanticContent(fact="x", valid_from=dt.date(2026, 2, 1), valid_until=dt.date(2026, 1, 1))
    with pytest.raises(ValidationError):
        ProvenanceCitation(source_uri="file:///workspace/a", session_id="s", verbatim_quote="x", line_start=2, line_end=1)
    with pytest.raises(ValidationError):
        FailureLessonContent(
            failure_symptom="x",
            trigger="x",
            root_cause="x",
            root_cause_status="verified",
            immediate_remediation="x",
            prevention_rule="x",
        )
    with pytest.raises(ValidationError):
        OptionBReceiptCommit(
            memory_id="mem_semantic_a",
            record_type="semantic",
            record_digest="a" * 64,
            body_sha256="b" * 64,
            note_path="memory/typed/semantic/../../bad.md",
            created=True,
        )


def test_q3_rejects_owner_preference_and_constitutional_rule(tmp_path: Path) -> None:
    common = {
        "record_type": "semantic",
        "typed_content": SemanticContent(fact="proposal"),
        "provenance": [_citation(tmp_path)],
        "session_id": "host-session",
    }
    for source_class in ("owner_preference", "constitutional_rule"):
        with pytest.raises(ValidationError, match="strictly prohibited"):
            TypedMemoryProposal(**common, source_class=source_class)


def test_record_and_batch_bounds(tmp_path: Path) -> None:
    content = SemanticContent(fact="bounded")
    record = _record(tmp_path, content)
    many_citations = [
        ProvenanceCitation(
            source_uri=f"file://{tmp_path}/source.txt",
            session_id=f"session-{index}",
            verbatim_quote="q" * 4096,
        )
        for index in range(100)
    ]
    values = record.model_dump()
    values["provenance"] = many_citations
    values["typed_content"] = content
    draft = TypedMemoryRecord.model_construct(**values)
    values["body_sha256"] = compute_body_sha256(draft)
    draft = TypedMemoryRecord.model_construct(**values)
    values["record_digest"] = sha256_canonical(
        {key: value for key, value in draft.model_dump(mode="json", exclude_none=False).items() if key != "record_digest"}
    )
    with pytest.raises(ValidationError, match="Record envelope"):
        TypedMemoryRecord(**values)

    commit = OptionBReceiptCommit(
        memory_id="mem_semantic_a",
        record_type="semantic",
        record_digest="a" * 64,
        body_sha256="b" * 64,
        note_path="memory/typed/semantic/mem_semantic_a.md",
        created=True,
    )
    values = {
        "batch_id": "batch_1",
        "request_id": "request_1",
        "session_id": "session",
        "workspace_id": "workspace",
        "profile_id": "profile",
        "n_records": 1,
        "commits": [commit],
        "committed_at_utc": "2026-11-13T01:02:03Z",
        "receipt_digest": "0" * 64,
    }
    draft = TypedOptionBReceipt.model_construct(**values)
    values["receipt_digest"] = sha256_canonical(
        {key: value for key, value in draft.model_dump(mode="json", exclude_none=False).items() if key != "receipt_digest"}
    )
    assert TypedOptionBReceipt(**values).index_status is None
    with pytest.raises(ValidationError, match="Invalid committed_at_utc"):
        TypedOptionBReceipt(**{**values, "committed_at_utc": "2026-11-13T01:02:03+01:00"})
    with pytest.raises(ValidationError, match="Invalid committed_at_utc"):
        TypedOptionBReceipt(**{**values, "committed_at_utc": "2026-11-13T01:02:03"})
    utc_values = {**values, "committed_at_utc": "2026-11-13T01:02:03+00:00"}
    utc_draft = TypedOptionBReceipt.model_construct(**utc_values)
    utc_values["receipt_digest"] = sha256_canonical(
        {key: value for key, value in utc_draft.model_dump(mode="json", exclude_none=False).items() if key != "receipt_digest"}
    )
    assert TypedOptionBReceipt(**utc_values).committed_at_utc.endswith("+00:00")
    with pytest.raises(ValidationError):
        TypedOptionBReceipt(**{**values, "index_status": "invalid"})

