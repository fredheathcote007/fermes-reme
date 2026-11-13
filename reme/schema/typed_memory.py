"""Typed memory records and Option B integrity envelopes.

This module is deliberately schema-only for FHM007-B.1.  It defines the
application-owned contracts and pure structural URI validation, but does not
activate filesystem admission, typed-batch writes, retrieval, or authority runtime.
"""

from __future__ import annotations

import datetime
import hashlib
import re
import urllib.parse
from pathlib import Path
from typing import Annotated, Any, Dict, List, Literal, Optional, Union

import rfc8785
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationInfo, field_validator, model_validator


SafeId = Annotated[
    str,
    StringConstraints(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_\-]+$"),
]

SafePath = Annotated[
    str,
    StringConstraints(min_length=1, max_length=256, pattern=r"^[a-zA-Z0-9_\-\./]+$"),
]

TypedNotePathRegex = re.compile(
    r"^memory/typed/(semantic|episodic|procedural|failure_lesson)/[a-zA-Z0-9_\-]+\.md$"
)

StatusType = Literal["active", "superseded", "retracted", "archived"]
ConfidenceLevel = Literal["high", "medium", "low"]
SourceClass = Literal[
    "authoritative_record",
    "agent_observation",
    "derived_summary",
    "model_claim",
    "unverified_output",
    "owner_preference",
    "constitutional_rule",
]
TrustTier = Literal["untrusted", "verified", "owner_approved"]
RecordType = Literal["semantic", "episodic", "procedural", "failure_lesson"]

ALLOWED_URI_SCHEMES = ("file", "session")
WINDOWS_DRIVE_RE = re.compile(r"^(?:[A-Za-z]:[\\/]|/[A-Za-z]:[\\/])")


def to_canonical_json_dict(obj: Any) -> Any:
    """Convert supported values to JSON primitives without dropping nulls."""

    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json", exclude_none=False)
    if isinstance(obj, (datetime.date, datetime.datetime)):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {key: to_canonical_json_dict(value) for key, value in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_canonical_json_dict(value) for value in obj]
    return obj


def canonical_jcs_bytes(data: Any) -> bytes:
    """Serialize *data* using RFC 8785 JSON Canonicalization Scheme."""

    encoded = rfc8785.dumps(to_canonical_json_dict(data))
    return encoded if isinstance(encoded, bytes) else encoded.encode("utf-8")


def sha256_canonical(data: Any) -> str:
    """Return the SHA-256 digest of canonical JSON bytes."""

    return hashlib.sha256(canonical_jcs_bytes(data)).hexdigest()


def _reject_windows_or_unsafe_uri_path(path: str) -> None:
    """Reject encoded traversal, empty components, and Windows path syntax."""

    decoded = urllib.parse.unquote(path)
    if "\\" in decoded or WINDOWS_DRIVE_RE.match(decoded):
        raise ValueError("URI path contains unsupported Windows syntax")
    parts = decoded.split("/")
    if parts and parts[0] == "":
        parts = parts[1:]
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("URI path contains an unsafe component")


def _validate_uri_syntax(uri: str) -> None:
    """Validate URI syntax only; filesystem admission belongs to later tasks."""

    if not uri or not isinstance(uri, str):
        raise ValueError("URI must be a non-empty string")
    parsed = urllib.parse.urlsplit(uri)
    if parsed.scheme not in ALLOWED_URI_SCHEMES:
        raise ValueError(f"Invalid scheme: {uri!r}. Permitted: {ALLOWED_URI_SCHEMES}")
    if parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise ValueError(f"URI may not contain query, fragment, or credentials: {uri!r}")
    decoded = urllib.parse.unquote(parsed.path)
    _reject_windows_or_unsafe_uri_path(parsed.path)
    if parsed.scheme == "file" and parsed.netloc not in {"", "localhost"}:
        raise ValueError("file URI must not name a remote host")
    if parsed.scheme == "session":
        if not re.fullmatch(r"[a-f0-9]{64}", parsed.netloc or ""):
            raise ValueError("session URI authority must be a lowercase SHA-256")
        if decoded != "/dialog.jsonl":
            raise ValueError("session URI path must be /dialog.jsonl")


def admit_source_uri(
    source_uri: str,
    *,
    workspace_dir: str | Path,
    session_id: Optional[str] = None,
) -> Path:
    """Admit a source URI to an existing, confined workspace path."""

    if not source_uri or not isinstance(source_uri, str):
        raise ValueError("URI must be a non-empty string")
    if not isinstance(workspace_dir, (str, Path)) or not str(workspace_dir):
        raise ValueError("workspace_dir is mandatory application admission context")
    parsed = urllib.parse.urlsplit(source_uri)
    if parsed.scheme not in ALLOWED_URI_SCHEMES:
        raise ValueError(
            f"Invalid source_uri scheme: {source_uri!r}. Permitted schemes: {ALLOWED_URI_SCHEMES}. "
            "git://, http://, https://, and ftp:// are strictly prohibited."
        )
    if parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise ValueError(f"source URI may not contain query, fragment, or credentials: {source_uri!r}")
    _reject_windows_or_unsafe_uri_path(parsed.path)
    ws_path = Path(workspace_dir).resolve()
    if not ws_path.is_dir():
        raise ValueError(f"Application workspace_dir must be an existing directory: {workspace_dir!r}")

    if parsed.scheme == "file":
        if parsed.netloc not in {"", "localhost"}:
            raise ValueError(f"file URI must not name a remote host: {source_uri!r}")
        if not parsed.path.startswith("/"):
            raise ValueError(f"file URI must have an absolute local path: {source_uri!r}")
        raw_path = Path(urllib.parse.unquote(parsed.path))
        if not raw_path.is_relative_to(ws_path):
            raise ValueError(f"file URI escapes workspace_dir: {raw_path!r} outside {ws_path!r}")
        curr = raw_path
        while curr != ws_path and curr != curr.parent:
            if curr.is_symlink():
                raise ValueError(f"Symlink rejected in workspace path: {curr!r}")
            curr = curr.parent
        return raw_path

    if not re.fullmatch(r"[a-f0-9]{64}", parsed.netloc or ""):
        raise ValueError(f"session URI authority must be a lowercase SHA-256: {source_uri!r}")
    if urllib.parse.unquote(parsed.path) != "/dialog.jsonl":
        raise ValueError(f"session URI path must be /dialog.jsonl: {source_uri!r}")
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("session_id is mandatory for session URI admission")
    expected_authority = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
    if parsed.netloc != expected_authority:
        raise ValueError(f"Session URI authority mismatch: expected {expected_authority}, got {parsed.netloc}")
    raw_target = ws_path / ".reme" / "sessions" / parsed.netloc / "dialog.jsonl"
    if not raw_target.is_relative_to(ws_path):
        raise ValueError(f"session URI escapes workspace_dir: {raw_target!r}")
    curr = raw_target
    while curr != ws_path and curr != curr.parent:
        if curr.is_symlink():
            raise ValueError(f"Symlink rejected in workspace path: {curr!r}")
        curr = curr.parent
    return raw_target


def validate_workspace_containment(uri: str, info: Optional[ValidationInfo] = None) -> str:
    """Validate URI syntax and admit it when application context is supplied."""

    _validate_uri_syntax(uri)
    if info is not None and info.context and "workspace_dir" in info.context:
        admit_source_uri(uri, workspace_dir=info.context["workspace_dir"], session_id=info.context.get("session_id"))
    return uri


class SourceHash(BaseModel):
    """Bounded hash commitment for a source file or commit."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sha256: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    git_commit: Optional[str] = Field(None, pattern=r"^[0-9a-f]{7,40}$")
    line_count: Optional[int] = Field(None, ge=1, le=100_000)


class ProvenanceCitation(BaseModel):
    """Fine-grained provenance linking a record to a source event."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_uri: str = Field(..., max_length=1024)
    line_start: Optional[int] = Field(None, ge=1, le=1_000_000)
    line_end: Optional[int] = Field(None, ge=1, le=1_000_000)
    verbatim_quote: str = Field(..., min_length=1, max_length=4096)
    source_class: SourceClass = Field(default="agent_observation")
    session_id: str = Field(..., min_length=1, max_length=128)
    transcript_hash: Optional[str] = Field(None, pattern=r"^[0-9a-f]{64}$")
    source_hash: Optional[SourceHash] = None

    @field_validator("source_uri")
    @classmethod
    def check_uri(cls, value: str, info: ValidationInfo) -> str:
        return validate_workspace_containment(value, info)

    @model_validator(mode="after")
    def check_line_ordering(self) -> "ProvenanceCitation":
        if self.line_start is not None and self.line_end is not None and self.line_start > self.line_end:
            raise ValueError(f"line_start ({self.line_start}) > line_end ({self.line_end})")
        return self


class SemanticContent(BaseModel):
    """Attributable facts, constraints, and world knowledge."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    record_type: Literal["semantic"] = "semantic"
    fact: str = Field(..., min_length=1, max_length=4096)
    entity: Optional[SafeId] = None
    attribute: Optional[SafeId] = None
    value: Optional[str] = Field(None, max_length=2048)
    confidence: ConfidenceLevel = Field(default="high")
    valid_from: Optional[datetime.date] = None
    valid_until: Optional[datetime.date] = None

    @model_validator(mode="after")
    def check_validity_dates(self) -> "SemanticContent":
        if self.valid_from is not None and self.valid_until is not None and self.valid_from > self.valid_until:
            raise ValueError(f"valid_from ({self.valid_from}) > valid_until ({self.valid_until})")
        return self


class EpisodicContent(BaseModel):
    """Time-ordered actions, observations, outcomes, and actors."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    record_type: Literal["episodic"] = "episodic"
    event_summary: str = Field(..., min_length=1, max_length=4096)
    occurred_at: datetime.datetime = Field(...)
    actors: List[SafeId] = Field(..., min_length=1, max_length=20)
    action: str = Field(..., min_length=1, max_length=2048)
    outcome: str = Field(..., min_length=1, max_length=2048)
    sequence_order: int = Field(default=0, ge=0, le=100_000)


class ProceduralContent(BaseModel):
    """Operational workflows, execution parameters, verification, and rollback."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    record_type: Literal["procedural"] = "procedural"
    task_name: SafeId = Field(...)
    prerequisites: List[str] = Field(default_factory=list, max_length=50)
    parameters: Dict[SafeId, str] = Field(default_factory=dict)
    steps: List[str] = Field(..., min_length=1, max_length=100)
    verification_command: Optional[str] = Field(None, max_length=1024)
    rollback_steps: List[str] = Field(default_factory=list, max_length=50)

    @field_validator("parameters")
    @classmethod
    def check_param_limits(cls, value: Dict[str, str]) -> Dict[str, str]:
        if len(value) > 50:
            raise ValueError(f"Parameters dict exceeds max keys (50): got {len(value)}")
        for key, item in value.items():
            if len(item) > 4096:
                raise ValueError(f"Parameter {key!r} value exceeds max chars (4096)")
        return value


class FailureLessonContent(BaseModel):
    """Observed failure symptoms, triggers, root causes, and prevention rules."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    record_type: Literal["failure_lesson"] = "failure_lesson"
    failure_symptom: str = Field(..., min_length=1, max_length=4096)
    trigger: str = Field(..., min_length=1, max_length=2048)
    root_cause: str = Field(..., min_length=1, max_length=4096)
    root_cause_status: Literal["hypothesized", "verified"] = Field(default="hypothesized")
    verification_receipt_id: Optional[SafeId] = None
    immediate_remediation: str = Field(..., min_length=1, max_length=4096)
    prevention_rule: str = Field(..., min_length=1, max_length=4096)

    @model_validator(mode="after")
    def check_verified_receipt(self) -> "FailureLessonContent":
        if self.root_cause_status == "verified" and not self.verification_receipt_id:
            raise ValueError("root_cause_status 'verified' requires a valid verification_receipt_id")
        return self


TypedContent = Annotated[
    Union[SemanticContent, EpisodicContent, ProceduralContent, FailureLessonContent],
    Field(discriminator="record_type"),
]

MARKDOWN_ESCAPED_CHARS = r"\`#[]()<>!*_~|+-"


def escape_markdown(text: str) -> str:
    """Escape Markdown punctuation and represent line breaks literally."""

    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", r"\n")
    return "".join(f"\\{char}" if char in MARKDOWN_ESCAPED_CHARS else char for char in text)


def format_field_val(value: Any) -> str:
    """Format a rendered field, distinguishing null from an empty string."""

    if value is None:
        return "<null>"
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.isoformat()
    if isinstance(value, list):
        return "[" + ", ".join(format_field_val(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(
            f"{escape_markdown(str(key))}={format_field_val(item)}" for key, item in sorted(value.items())
        ) + "}"
    if isinstance(value, str):
        return f'"{escape_markdown(value)}"'
    return str(value)


def render_markdown_body(record: Any) -> str:
    """Render the deterministic Markdown body used by ``body_sha256``."""

    lines: List[str] = [
        f"# Typed Memory Record: {escape_markdown(str(getattr(record, 'memory_id', '')))}",
        "",
        "## Identity",
        "",
        f"- memory_id: {format_field_val(getattr(record, 'memory_id', None))}",
        f"- record_type: {format_field_val(getattr(record, 'record_type', None))}",
        f"- status: {format_field_val(getattr(record, 'status', 'active'))}",
        f"- trust_tier: {format_field_val(getattr(record, 'trust_tier', 'verified'))}",
        f"- revision_seq: {format_field_val(getattr(record, 'revision_seq', 1))}",
        f"- workspace_id: {format_field_val(getattr(record, 'workspace_id', None))}",
        f"- profile_id: {format_field_val(getattr(record, 'profile_id', None))}",
        f"- project_id: {format_field_val(getattr(record, 'project_id', None))}",
        f"- authority_entry_id: {format_field_val(getattr(record, 'authority_entry_id', None))}",
        "",
        "## Lifecycle",
        "",
        f"- created_at_utc: {format_field_val(getattr(record, 'created_at_utc', None))}",
        f"- updated_at_utc: {format_field_val(getattr(record, 'updated_at_utc', None))}",
        f"- supersedes_id: {format_field_val(getattr(record, 'supersedes_id', None))}",
        "",
        "## Content",
        "",
    ]
    content = getattr(record, "typed_content", None)
    if isinstance(content, SemanticContent):
        lines.extend(
            f"- {field}: {format_field_val(getattr(content, field))}"
            for field in ("fact", "entity", "attribute", "value", "confidence", "valid_from", "valid_until")
        )
    elif isinstance(content, EpisodicContent):
        lines.extend(
            f"- {field}: {format_field_val(getattr(content, field))}"
            for field in ("event_summary", "occurred_at", "actors", "action", "outcome", "sequence_order")
        )
    elif isinstance(content, ProceduralContent):
        lines.extend(
            f"- {field}: {format_field_val(getattr(content, field))}"
            for field in ("task_name", "prerequisites", "parameters", "steps", "verification_command", "rollback_steps")
        )
    elif isinstance(content, FailureLessonContent):
        lines.extend(
            f"- {field}: {format_field_val(getattr(content, field))}"
            for field in (
                "failure_symptom",
                "trigger",
                "root_cause_status",
                "root_cause",
                "verification_receipt_id",
                "immediate_remediation",
                "prevention_rule",
            )
        )
    lines.extend(["", "## Provenance", ""])
    for index, citation in enumerate(getattr(record, "provenance", []), start=1):
        lines.extend(
            [
                f"- citation_{index}_source_class: {format_field_val(citation.source_class)}",
                f"- citation_{index}_source_uri: {format_field_val(citation.source_uri)}",
                f"- citation_{index}_session_id: {format_field_val(citation.session_id)}",
                f"- citation_{index}_verbatim_quote: {format_field_val(citation.verbatim_quote)}",
                f"- citation_{index}_line_start: {format_field_val(citation.line_start)}",
                f"- citation_{index}_line_end: {format_field_val(citation.line_end)}",
                f"- citation_{index}_transcript_hash: {format_field_val(citation.transcript_hash)}",
                f"- citation_{index}_source_sha256: {format_field_val(citation.source_hash.sha256 if citation.source_hash else None)}",
                f"- citation_{index}_git_commit: {format_field_val(citation.source_hash.git_commit if citation.source_hash else None)}",
                f"- citation_{index}_line_count: {format_field_val(citation.source_hash.line_count if citation.source_hash else None)}",
            ]
        )
    lines.append("")
    return "\n".join(lines)


def compute_body_sha256(record: Any) -> str:
    """Return the digest of the exact UTF-8 rendered body."""

    return hashlib.sha256(render_markdown_body(record).encode("utf-8")).hexdigest()


class TypedMemoryRecord(BaseModel):
    """Immutable, application-owned typed memory record."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    memory_id: SafeId = Field(...)
    record_type: RecordType = Field(...)
    status: StatusType = Field(default="active")
    revision_seq: int = Field(default=1, ge=1, le=100_000)
    workspace_id: str = Field(..., min_length=1, max_length=128)
    profile_id: Optional[str] = Field(None, max_length=128)
    project_id: Optional[str] = Field(None, max_length=128, pattern=r"^[a-zA-Z0-9_\-\.]+$")
    supersedes_id: Optional[SafeId] = None
    typed_content: TypedContent = Field(...)
    provenance: List[ProvenanceCitation] = Field(..., min_length=1, max_length=100)
    trust_tier: TrustTier = Field(default="verified")
    authority_entry_id: Optional[SafeId] = Field(None)
    content_hash: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    body_sha256: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    record_digest: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    note_path: str = Field(..., pattern=TypedNotePathRegex.pattern)
    created_at_utc: str = Field(...)
    updated_at_utc: str = Field(...)

    @model_validator(mode="after")
    def validate_record_invariants(self) -> "TypedMemoryRecord":
        if self.typed_content.record_type != self.record_type:
            raise ValueError(f"Content record_type ({self.typed_content.record_type}) != record_type ({self.record_type})")
        if self.trust_tier == "owner_approved":
            if not self.authority_entry_id:
                raise ValueError("Records claiming trust_tier='owner_approved' must bind to authority_entry_id")
            if not self.profile_id:
                raise ValueError("Records claiming trust_tier='owner_approved' must have explicit profile_id")
        if self.authority_entry_id and self.trust_tier != "owner_approved":
            raise ValueError("authority_entry_id cannot be attached to non-owner records")

        expected_content_hash = sha256_canonical(self.typed_content.model_dump(mode="json", exclude_none=False))
        if self.content_hash != expected_content_hash:
            raise ValueError(f"content_hash mismatch: expected {expected_content_hash}, got {self.content_hash}")
        expected_memory_id = f"mem_{self.record_type}_{expected_content_hash[:16]}"
        if self.memory_id != expected_memory_id:
            raise ValueError(f"Deterministic memory_id mismatch: expected {expected_memory_id}, got {self.memory_id}")
        expected_note_path = f"memory/typed/{self.record_type}/{self.memory_id}.md"
        if self.note_path != expected_note_path:
            raise ValueError(f"Deterministic note_path mismatch: expected {expected_note_path}, got {self.note_path}")

        expected_body_sha256 = compute_body_sha256(self)
        if self.body_sha256 != expected_body_sha256:
            raise ValueError(f"body_sha256 mismatch: expected {expected_body_sha256}, got {self.body_sha256}")
        dumped = self.model_dump(mode="json", exclude_none=False)
        data_for_digest = {key: value for key, value in dumped.items() if key not in {"record_digest", "_reme_commit"}}
        expected_record_digest = sha256_canonical(data_for_digest)
        if self.record_digest != expected_record_digest:
            raise ValueError(f"record_digest mismatch: expected {expected_record_digest}, got {self.record_digest}")
        envelope_size = len(canonical_jcs_bytes(dumped))
        if envelope_size > 65_536:
            raise ValueError(f"Record envelope exceeds 64 KB limit: {envelope_size} bytes")
        return self


class TypedMemoryProposal(BaseModel):
    """Untrusted model proposal; OP and CR creation is forbidden in B.1."""

    model_config = ConfigDict(extra="forbid")

    record_type: RecordType = Field(...)
    source_class: SourceClass = Field(default="agent_observation")
    typed_content: TypedContent = Field(...)
    provenance: List[ProvenanceCitation] = Field(..., min_length=1, max_length=50)
    session_id: str = Field(..., min_length=1, max_length=128)
    supersedes_id: Optional[SafeId] = None
    project_id: Optional[str] = Field(None, max_length=128)

    @model_validator(mode="after")
    def validate_proposal(self) -> "TypedMemoryProposal":
        if self.source_class in ("owner_preference", "constitutional_rule"):
            raise ValueError(
                f"SourceClass {self.source_class!r} creation is strictly prohibited in Phase B (Owner Gate Q3)"
            )
        if self.typed_content.record_type != self.record_type:
            raise ValueError(f"Content record_type ({self.typed_content.record_type}) != proposal record_type ({self.record_type})")
        return self


class OptionBMarker(BaseModel):
    """Application-owned frontmatter marker for a persisted note."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str = Field(default="v2")
    request_id: SafeId = Field(...)
    session_id: str = Field(..., min_length=1, max_length=128)
    memory_id: SafeId = Field(...)
    record_type: RecordType = Field(...)
    content_hash: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    body_sha256: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    record_digest: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    created: bool = Field(default=True)
    provenance_incomplete: bool = Field(default=False)


class OptionBReceiptCommit(BaseModel):
    """Individual commit confirmation in an Option B transaction receipt."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    memory_id: SafeId = Field(...)
    record_type: RecordType = Field(...)
    record_digest: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    body_sha256: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    note_path: str = Field(..., pattern=TypedNotePathRegex.pattern)
    status: StatusType = Field(default="active")
    revision_seq: int = Field(default=1, ge=1)
    created: bool = Field(...)

    @model_validator(mode="after")
    def validate_commit(self) -> "OptionBReceiptCommit":
        expected_note_path = f"memory/typed/{self.record_type}/{self.memory_id}.md"
        if self.note_path != expected_note_path:
            raise ValueError(f"note_path mismatch: expected {expected_note_path}, got {self.note_path}")
        return self


class TypedOptionBReceipt(BaseModel):
    """Canonical typed-batch receipt scaffold; activation remains out of scope."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    outcome: Literal["typed_batch"] = Field(default="typed_batch")
    batch_id: SafeId = Field(...)
    request_id: SafeId = Field(...)
    session_id: str = Field(..., min_length=1, max_length=128)
    workspace_id: str = Field(..., min_length=1, max_length=128)
    profile_id: str = Field(..., min_length=1, max_length=128)
    n_records: int = Field(..., ge=1, le=100)
    commits: List[OptionBReceiptCommit] = Field(..., min_length=1, max_length=100)
    index_status: Optional[Literal["ok", "degraded"]] = Field(default=None)
    committed_at_utc: str = Field(...)
    receipt_digest: str = Field(..., pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_receipt(self) -> "TypedOptionBReceipt":
        try:
            timestamp = datetime.datetime.fromisoformat(self.committed_at_utc.replace("Z", "+00:00"))
            if timestamp.tzinfo is None:
                raise ValueError("committed_at_utc must include timezone")
            if timestamp.utcoffset() != datetime.timedelta(0):
                raise ValueError(
                    f"committed_at_utc must have UTC offset (+00:00 or Z), got {self.committed_at_utc!r}"
                )
        except Exception as exc:
            raise ValueError(f"Invalid committed_at_utc ISO-8601 timestamp: {self.committed_at_utc!r}") from exc
        if self.n_records != len(self.commits):
            raise ValueError(f"n_records ({self.n_records}) != len(commits) ({len(self.commits)})")
        commit_ids = [commit.memory_id for commit in self.commits]
        if len(set(commit_ids)) != len(commit_ids):
            raise ValueError("Duplicate memory_id detected in batch commits")
        dumped = self.model_dump(mode="json", exclude_none=False)
        if len(canonical_jcs_bytes(dumped)) > 524_288:
            raise ValueError("Batch receipt exceeds 512 KiB limit")
        data_to_hash = {key: value for key, value in dumped.items() if key != "receipt_digest"}
        expected = sha256_canonical(data_to_hash)
        if self.receipt_digest != expected:
            raise ValueError(f"receipt_digest mismatch: expected {expected}, got {self.receipt_digest}")
        return self


__all__ = [
    "ALLOWED_URI_SCHEMES",
    "ConfidenceLevel",
    "EpisodicContent",
    "FailureLessonContent",
    "OptionBMarker",
    "OptionBReceiptCommit",
    "ProceduralContent",
    "ProvenanceCitation",
    "RecordType",
    "SafeId",
    "SafePath",
    "SemanticContent",
    "SourceClass",
    "SourceHash",
    "StatusType",
    "TrustTier",
    "TypedContent",
    "TypedMemoryProposal",
    "TypedMemoryRecord",
    "TypedNotePathRegex",
    "TypedOptionBReceipt",
    "admit_source_uri",
    "canonical_jcs_bytes",
    "compute_body_sha256",
    "escape_markdown",
    "format_field_val",
    "render_markdown_body",
    "sha256_canonical",
    "to_canonical_json_dict",
    "validate_workspace_containment",
]
