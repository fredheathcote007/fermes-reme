"""Portable parity tests for the frozen FHM-007 B.1 normative schema.

The design pack is available on the implementation host, but is intentionally
not required by CI.  The checked-in fixture is the exact 1,566-line Python
fence from the frozen design and is executed in an isolated module namespace.
"""

# pylint: disable=missing-function-docstring,protected-access,redefined-outer-name

from __future__ import annotations

import hashlib
import importlib
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


FIXTURE = Path(__file__).parents[1] / "fixtures" / "fhm007_b1_normative_excerpt.txt"
DESIGN = Path("/srv/scratch/fhm007-phase-b-design-20261112/plans/detailed-design-typed-memory.md")
FIXTURE_SHA256 = "224617442e0dd63ad27f6ef7a7e57af0d44ddcccd97708eef7288c1b65f07e2a"
DESIGN_SHA256 = "09f2cb9a5eb0094ba005dfabbb72362b7b5f57fad0822186603f3bd88cfc9bd0"
NORMATIVE_LINES = 1_566

MODEL_NAMES = (
    "SemanticContent",
    "EpisodicContent",
    "ProceduralContent",
    "FailureLessonContent",
    "TypedMemoryRecord",
    "TypedMemoryProposal",
    "OptionBMarker",
    "OptionBReceiptCommit",
    "TypedOptionBReceipt",
)


def _normative_source() -> str:
    source = FIXTURE.read_text(encoding="utf-8")
    assert len(source.splitlines()) == NORMATIVE_LINES
    assert hashlib.sha256(source.encode("utf-8")).hexdigest() == FIXTURE_SHA256
    if DESIGN.is_file():
        assert hashlib.sha256(DESIGN.read_bytes()).hexdigest() == DESIGN_SHA256
        design_lines = DESIGN.read_text(encoding="utf-8").splitlines()
        start = design_lines.index("```python") + 1
        end = design_lines.index("```", start)
        assert "\n".join(design_lines[start:end]) + "\n" == source
    return source


@pytest.fixture(scope="session")
def normative_module() -> types.ModuleType:
    """Compile the checked-in normative code without importing production code."""

    module_name = "_fhm007_b1_normative_extracted"
    module = types.ModuleType(module_name)
    module.__file__ = str(FIXTURE)
    sys.modules[module_name] = module
    try:
        exec(compile(_normative_source(), str(FIXTURE), "exec"), module.__dict__)
        yield module
    finally:
        sys.modules.pop(module_name, None)


@pytest.fixture(scope="session")
def runtime_module() -> types.ModuleType:
    module_name = "reme.schema.typed_memory"
    module_path = Path(__file__).parents[2] / "reme" / "schema" / "typed_memory.py"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _schema_without_documentation(model: type[Any]) -> dict[str, Any]:
    """Keep field/type/constraint schema while ignoring non-contract prose."""

    schema = model.model_json_schema()

    def clean(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: clean(item) for key, item in value.items() if key not in {"description", "title"}}
        if isinstance(value, list):
            return [clean(item) for item in value]
        return value

    return clean(schema)


def test_normative_fixture_is_frozen_and_compilable(normative_module: types.ModuleType) -> None:
    assert normative_module.__name__ == "_fhm007_b1_normative_extracted"
    assert all(hasattr(normative_module, name) for name in MODEL_NAMES)


def test_runtime_models_have_exact_normative_schema_parity(
    normative_module: types.ModuleType, runtime_module: types.ModuleType
) -> None:
    for name in MODEL_NAMES:
        normative_model = getattr(normative_module, name)
        runtime_model = getattr(runtime_module, name)
        assert set(normative_model.model_fields) == set(runtime_model.model_fields), name
        for field_name, normative_field in normative_model.model_fields.items():
            runtime_field = runtime_model.model_fields[field_name]
            assert normative_field.is_required() == runtime_field.is_required(), f"{name}.{field_name} required"
            assert repr(normative_field.annotation).replace("_fhm007_b1_normative_extracted", "") == repr(
                runtime_field.annotation
            ).replace("reme.schema.typed_memory", ""), f"{name}.{field_name} annotation"
        assert _schema_without_documentation(normative_model) == _schema_without_documentation(runtime_model), name


@pytest.mark.parametrize(
    "value",
    [
        {"unicode": "café\u2028雪", "float": 1.25, "negative_zero": -0.0, "nested": {"null": None}},
        {"z": [3, 2, 1], "a": True, "empty": ""},
    ],
)
def test_runtime_jcs_bytes_and_digests_match_normative(
    value: dict[str, Any], normative_module: types.ModuleType, runtime_module: types.ModuleType
) -> None:
    assert runtime_module.canonical_jcs_bytes(value) == normative_module.canonical_jcs_bytes(value)
    assert runtime_module.sha256_canonical(value) == normative_module.sha256_canonical(value)


def _assert_both_reject(
    normative_module: types.ModuleType,
    runtime_module: types.ModuleType,
    operation: Any,
    message: str | None = None,
) -> None:
    for module in (normative_module, runtime_module):
        with pytest.raises((ValueError, TypeError), match=message):
            operation(module)


def _semantic(module: types.ModuleType, **overrides: Any) -> Any:
    values: dict[str, Any] = {"fact": "A durable fact", "value": None}
    values.update(overrides)
    return module.SemanticContent(**values)


def _provenance(module: types.ModuleType, *, quote: str = "observed") -> Any:
    return module.ProvenanceCitation(
        source_uri="file:///workspace/source.txt",
        verbatim_quote=quote,
        session_id="session-1",
    )


def _receipt(module: types.ModuleType, *, timestamp: str = "2026-11-13T01:00:00Z", duplicate: bool = False) -> Any:
    first = module.OptionBReceiptCommit(
        memory_id="mem_semantic_1",
        record_type="semantic",
        record_digest="a" * 64,
        body_sha256="b" * 64,
        note_path="memory/typed/semantic/mem_semantic_1.md",
        created=True,
    )
    commits = [first]
    if duplicate:
        commits.append(first)
    return module.TypedOptionBReceipt(
        batch_id="batch-1",
        request_id="request-1",
        session_id="session-1",
        workspace_id="workspace-1",
        profile_id="profile-1",
        n_records=len(commits),
        commits=commits,
        committed_at_utc=timestamp,
        receipt_digest="0" * 64,
    )


def test_deliberate_schema_negative_probes_fail_closed(
    normative_module: types.ModuleType, runtime_module: types.ModuleType
) -> None:
    _assert_both_reject(
        normative_module,
        runtime_module,
        lambda module: _semantic(module, valid_from="2026-11-14", valid_until="2026-11-13"),
        "valid_from",
    )
    _assert_both_reject(
        normative_module,
        runtime_module,
        lambda module: module.FailureLessonContent(
            failure_symptom="symptom",
            trigger="trigger",
            root_cause="cause",
            root_cause_status="verified",
            immediate_remediation="fix",
            prevention_rule="prevent",
        ),
        "verification_receipt_id",
    )
    for module in (normative_module, runtime_module):
        with pytest.raises(ValueError, match="authority"):
            module.admit_source_uri(
                "session://not-a-sha/dialog.jsonl",
                workspace_dir=Path(__file__).parents[2],
                session_id="session-1",
            )


def test_admit_source_uri_confinement_parity(
    normative_module: types.ModuleType, runtime_module: types.ModuleType, tmp_path: Path
) -> None:
    for index, module in enumerate((normative_module, runtime_module)):
        workspace = tmp_path / f"workspace-{index}"
        workspace.mkdir()
        inside = workspace / "source.txt"
        inside.write_text("source", encoding="utf-8")
        assert module.admit_source_uri(f"file://{inside}", workspace_dir=workspace) == inside

        session_id = "session-1"
        authority = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
        expected = workspace / ".reme" / "sessions" / authority / "dialog.jsonl"
        assert module.admit_source_uri(
            f"session://{authority}/dialog.jsonl", workspace_dir=workspace, session_id=session_id
        ) == expected

        with pytest.raises(ValueError, match="escapes workspace_dir"):
            module.admit_source_uri("file:///etc/passwd", workspace_dir=workspace)
        for uri in (f"file://{workspace}/../outside", f"file://{workspace}/%2e%2e/outside"):
            with pytest.raises(ValueError):
                module.admit_source_uri(uri, workspace_dir=workspace)
        with pytest.raises(ValueError, match="[Aa]uthority mismatch"):
            module.admit_source_uri(
                f"session://{'a' * 64}/dialog.jsonl", workspace_dir=workspace, session_id=session_id
            )
        with pytest.raises(ValueError, match="session_id"):
            module.admit_source_uri(f"session://{authority}/dialog.jsonl", workspace_dir=workspace, session_id="")
        with pytest.raises(ValueError, match="existing directory|does not exist"):
            module.admit_source_uri(f"file://{inside}", workspace_dir=workspace / "missing")

        outside = workspace.parent / f"outside-{index}.txt"
        outside.write_text("outside", encoding="utf-8")
        link = workspace / "link"
        link.symlink_to(outside)
        with pytest.raises(ValueError, match="[Ss]ymlink"):
            module.admit_source_uri(f"file://{link}", workspace_dir=workspace)


def test_direct_note_adapters_are_absent_from_runtime_exports(runtime_module: types.ModuleType) -> None:
    for name in ("safe_write_note", "admit_and_commit_record", "admit_and_load_record", "safe_open_source"):
        assert name not in runtime_module.__all__
        assert not hasattr(runtime_module, name)


def test_op_and_cr_proposals_are_rejected(normative_module: types.ModuleType, runtime_module: types.ModuleType) -> None:
    for module in (normative_module, runtime_module):
        for source_class in ("owner_preference", "constitutional_rule"):
            with pytest.raises(ValueError, match="prohibited|Q3"):
                module.TypedMemoryProposal(
                    record_type="semantic",
                    source_class=source_class,
                    typed_content=_semantic(module),
                    provenance=[_provenance(module)],
                    session_id="session-1",
                )


def _oversized_record_payload(module: types.ModuleType) -> dict[str, Any]:
    content = _semantic(module)
    provenance = [_provenance(module, quote="x" * 4096) for _ in range(20)]
    content_hash = module.sha256_canonical(content.model_dump(mode="json", exclude_none=False))
    memory_id = f"mem_semantic_{content_hash[:16]}"
    shell = SimpleNamespace(
        memory_id=memory_id,
        record_type="semantic",
        status="active",
        trust_tier="verified",
        revision_seq=1,
        workspace_id="workspace-1",
        profile_id=None,
        project_id=None,
        authority_entry_id=None,
        created_at_utc="2026-11-13T01:00:00Z",
        updated_at_utc="2026-11-13T01:00:00Z",
        supersedes_id=None,
        typed_content=content,
        provenance=provenance,
    )
    body_sha256 = module.compute_body_sha256(shell)
    payload = {
        **shell.__dict__,
        "content_hash": content_hash,
        "body_sha256": body_sha256,
        "record_digest": "0" * 64,
        "note_path": f"memory/typed/semantic/{memory_id}.md",
    }
    digest_payload = {key: value for key, value in payload.items() if key != "record_digest"}
    payload["record_digest"] = module.sha256_canonical(digest_payload)
    return payload


def test_record_envelope_over_64_kib_is_rejected(
    normative_module: types.ModuleType, runtime_module: types.ModuleType
) -> None:
    for module in (normative_module, runtime_module):
        with pytest.raises(ValueError, match="64 KB|64 KiB|65536"):
            module.TypedMemoryRecord(**_oversized_record_payload(module))


def test_receipt_batch_over_512_kib_is_rejected(
    normative_module: types.ModuleType, runtime_module: types.ModuleType
) -> None:
    for module in (normative_module, runtime_module):
        commit = module.OptionBReceiptCommit(
            memory_id="mem_semantic_1",
            record_type="semantic",
            record_digest="a" * 64,
            body_sha256="b" * 64,
            note_path="memory/typed/semantic/mem_semantic_1.md",
            created=True,
        )
        receipt = module.TypedOptionBReceipt.model_construct(
            batch_id="batch-1",
            request_id="request-1",
            session_id="session-1",
            workspace_id="workspace-1",
            profile_id="profile-1",
            n_records=1,
            commits=[commit],
            committed_at_utc="2026-11-13T01:00:00Z",
            receipt_digest="0" * 64,
        )
        original_model_dump = receipt.model_dump
        object.__setattr__(receipt, "model_dump", lambda **_: {"oversized": "x" * 524_289})
        try:
            with pytest.raises(ValueError, match="512 KiB|512 KB|524288"):
                receipt.validate_receipt()
        finally:
            object.__setattr__(receipt, "model_dump", original_model_dump)


def test_duplicate_memory_id_in_batch_receipt_is_rejected(
    normative_module: types.ModuleType, runtime_module: types.ModuleType
) -> None:
    for module in (normative_module, runtime_module):
        with pytest.raises(ValueError, match="Duplicate memory_id"):
            _receipt(module, duplicate=True)


def test_invalid_iso8601_utc_timestamp_is_rejected(
    normative_module: types.ModuleType, runtime_module: types.ModuleType
) -> None:
    for module in (normative_module, runtime_module):
        with pytest.raises(ValueError, match="Invalid committed_at_utc"):
            _receipt(module, timestamp="2026-11-13T01:00:00")
