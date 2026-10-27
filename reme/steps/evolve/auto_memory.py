"""auto_memory — record conversation facts into a daily note via an agent."""

# This security-sensitive step intentionally keeps its provenance and crash-recovery
# paths together; splitting it solely to satisfy a module-size threshold would make
# the fail-closed state transitions harder to audit.
# pylint: disable=too-many-lines

import base64
import binascii
import datetime
import hashlib
import json
import os
import re
import stat
import zoneinfo
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import aiofiles
import frontmatter
from agentscope.agent import ContextConfig
from agentscope.message import DataBlock, Msg, TextBlock, UserMsg

from ...components import R
from ...schema import Response
from ...utils.wikilink_handler import WikilinkHandler
from ..base_step import BaseStep
from ..file_io import (
    extract_daily_date,
    get_path_lock,
    parse_daily_date,
    refresh_day_index,
    validate_filename_component,
    validate_session_id,
    write_file_durable,
    write_file_safe,
)
from ..file_io._path import IMAGE_MIME_BY_EXT, _check_path_permission, resolve_path
from ..file_io._provenance import (
    PROVENANCE_SESSION_KEY,
    ProvenanceError,
    note_owned_by_session,
    resolve_session_source,
    session_source_link,
    session_source_path,
    trusted_provenance,
    verify_note_provenance,
)
from ..index import normalize_posix_path
from ._evolve import agent_reply_result_text, format_history, now

_SESSION_ID_KEY = "session_id"
_SOURCE_CONVERSATION_KEY = "source_conversation"
_MESSAGE_TIME_ALIASES = ("time_created", "timestamp", "createdAt", "timeCreated", "created_time")
_SUCCESSFUL_TOOL_STATES = {"success", "succeeded", "ok"}
_OPTION_B_MARKER = "_reme_commit"
_OPTION_B_VERSION = 1
_OPTION_B_COMMIT_OUTCOME = "commit"
_OPTION_B_NOOP_OUTCOME = "noop"
_OPTION_B_RESERVED_METADATA = frozenset(
    {
        "name",
        "description",
        "session_id",
        "source_conversation",
        "source_hash",
        _OPTION_B_MARKER,
    },
)


def _checked_write_path(workspace: Path, relative: str, allowed_paths) -> Path:
    """Use the file Jobs' path and permission checks for attachments and note metadata."""
    target, error = resolve_path(workspace, relative)
    if error or target is None:
        raise ValueError(error or "Invalid write path")
    if not _check_path_permission(workspace, target, allowed_paths):
        raise PermissionError(f"No permission to write {relative}")
    return target


def _check_existing_image(target: Path) -> bool:
    if target.is_file():
        return True
    if target.exists():
        raise ValueError(f"Image attachment path is not a file: {target.name}")
    return False


def _read_exact_owned_file(path: Path) -> bytes:
    """Read a regular file without following a replaced final symlink."""
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("path is not a regular file")
        chunks = []
        while chunk := os.read(fd, 1024 * 1024):
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _has_lexical_symlink(workspace: Path, path: Path) -> bool:
    """Reject symlink components, including a final component that resolves inside."""
    try:
        relative = path.relative_to(workspace)
    except ValueError:
        return True
    current = workspace
    for component in relative.parts:
        current /= component
        if current.is_symlink():
            return True
    return False


async def _save_session_images(
    workspace: Path,
    session_dir: str,
    session_id: str,
    messages: list[Msg],
    images: dict[str, DataBlock],
    allowed_paths=None,
) -> list[str]:
    """Save Base64 bytes and annotate prepared image markers; URLs remain remote.

    ``messages`` must be the invocation-owned copies from image preparation.
    Image data and caller-owned messages remain unchanged. Image identity uses
    the message ID and block position, not the image block's optional identity.
    """
    workspace = workspace.resolve()
    error = validate_filename_component(session_id, kind="session_id")
    if error:
        raise ValueError(error)
    if Path(session_dir).is_absolute():
        raise ValueError("session_dir must be workspace-relative")
    pending: dict[Path, tuple[str, bytes]] = {}
    replacements = []
    sources = []
    for message in messages:
        for index, block in enumerate(message.content):
            if not isinstance(block, TextBlock) or block.text not in images:
                continue
            image = images[block.text]
            source = image.source
            if source.type == "url":
                reference = str(source.url)
            else:
                # A reversible encoding, not a hash: also distinguish IDs on
                # case-insensitive filesystems without trusting IDs as paths.
                encoded_id = message.id.encode("utf-8").hex()
                suffix = next(
                    (ext for ext, mime in IMAGE_MIME_BY_EXT.items() if mime == source.media_type.lower()),
                    ".bin",
                )
                filename = f"msg-{encoded_id}-image-{index}{suffix}"
                if len(filename) > 255:
                    raise ValueError("Message ID is too long for an image attachment filename")
                relative = (Path(session_dir) / "images" / session_id / filename).as_posix()
                error = WikilinkHandler.validate_src_dst(relative, relative)
                if error:
                    raise ValueError(f"Image attachment path cannot be linked: {error}")
                target = _checked_write_path(workspace, relative, allowed_paths)
                # Wait for any in-process writer before reusing the attachment.
                # Stable message IDs and block positions identify an image.
                async with await get_path_lock(target):
                    target = _checked_write_path(workspace, relative, allowed_paths)
                    if not _check_existing_image(target):
                        try:
                            payload = base64.b64decode(source.data, validate=True)
                        except (ValueError, binascii.Error) as exc:
                            raise ValueError("Image source contains invalid Base64") from exc
                        if not payload:
                            raise ValueError("Image source contains empty Base64 data")
                        if target in pending and pending[target][1] != payload:
                            raise ValueError("Conflicting images share the same message ID and block position")
                        pending[target] = (relative, payload)
                reference = f"[[{relative}]]"
            replacements.append((message, index, block, reference))
            if reference not in sources:
                sources.append(reference)

    # Validate every input before creating files. Recheck each path under the
    # existing file-operation lock so concurrent saves cannot overwrite it.
    for relative, payload in pending.values():
        target = _checked_write_path(workspace, relative, allowed_paths)
        async with await get_path_lock(target):
            target = _checked_write_path(workspace, relative, allowed_paths)
            if not _check_existing_image(target):
                await write_file_safe(target, payload)
    for message, index, block, reference in replacements:
        message.content[index] = block.model_copy(update={"text": f"Image source: {reference}\n{block.text}"})
    return sources


def _sanitize_msg_for_save(msg: Msg) -> Msg:
    new_content = []
    changed = False
    for block in msg.content:
        # Tool results often contain recalled memory/search/read output. Keeping
        # them in saved conversation history lets retrieved facts masquerade as
        # user-provided context in future auto-memory runs.
        if block.type == "tool_result":
            changed = True
            continue
        if block.type == "data" and hasattr(block, "source") and getattr(block.source, "type", None) == "base64":
            changed = True
            continue
        new_content.append(block)
    if not changed:
        return msg
    return msg.model_copy(update={"content": new_content})


def _source_turn_key(msg: Msg) -> str:
    """Identify a persisted turn without ephemeral AgentScope identity fields."""
    payload = msg.model_dump(mode="json")
    payload.pop("id", None)
    payload.pop("created_at", None)
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _normalize_msg_timestamp(item: dict) -> dict:
    """Map common message timestamp aliases to AgentScope's ``created_at`` field."""
    if item.get("created_at"):
        return item

    for key in _MESSAGE_TIME_ALIASES:
        value = item.get(key)
        if value:
            return {**item, "created_at": value}

    metadata = item.get("metadata")
    if isinstance(metadata, dict):
        for key in _MESSAGE_TIME_ALIASES:
            value = metadata.get(key)
            if value:
                return {**item, "created_at": value}

    return item


@R.register("auto_memory_step")
class AutoMemoryStep(BaseStep):
    """Record conversation facts into a daily note via an Agent."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.create_tools: list[str] = ["daily_write"]
        self.update_tools: list[str] = ["read", "edit", "frontmatter_update", "write"]

    def _session_dir(self) -> str:
        return normalize_posix_path(str(self.config_value("session_dir")))

    def _session_path(self, session_id: str) -> Path:
        return resolve_session_source(
            Path(self.file_store.workspace_path or "."),
            self._session_dir(),
            session_id,
            require_exists=False,
        )

    def _session_source_path(self, session_id: str) -> str:
        return normalize_posix_path(session_source_path(self._session_dir(), session_id))

    def _session_link(self, session_id: str) -> str:
        return session_source_link(self._session_dir(), session_id)

    def _daily_note_path(self, day: str, name: str) -> str:
        return f"{self.config_value('daily_dir')}/{day}/{name}.md"

    def _frontmatter(self, path: str) -> dict:
        post = frontmatter.loads((self.file_store.workspace_path / path).read_text(encoding="utf-8"))
        return dict(post.metadata or {})

    def _note_bytes(self, path: str) -> bytes | None:
        note_path = self.file_store.workspace_path / path
        if not note_path.is_file():
            return None
        return note_path.read_bytes()

    def _note_modified(self, before_path: str, before_bytes: bytes | None, after_path: str) -> bool:
        if not after_path:
            return False
        after_bytes = self._note_bytes(after_path)
        if after_bytes is None:
            return before_bytes is not None
        return after_path != before_path or before_bytes != after_bytes

    def _find_session_note(
        self,
        notes: list[dict],
        session_id: str,
        *,
        fail_closed: bool = False,
    ) -> dict | None:
        source = self._session_link(session_id)
        if not fail_closed:
            for note in notes:
                note_session = str(note.get(_SESSION_ID_KEY, "")).strip()
                note_source = str(note.get(_SOURCE_CONVERSATION_KEY, "")).strip()
                if note_session == session_id and (not note_source or note_source == source):
                    return note
            for note in notes:
                if str(note.get(_SOURCE_CONVERSATION_KEY, "")).strip() == source:
                    return note
            return None

        conflicting: dict | None = None
        for note in notes:
            note_session = str(note.get(_SESSION_ID_KEY, "")).strip()
            note_source = str(note.get(_SOURCE_CONVERSATION_KEY, "")).strip()
            if note_session == session_id and (not note_source or note_source == source):
                return note
            # Keep an identity contradiction attached to the update path so
            # the protected flow can fail closed before invoking the model or
            # treating the note as a new create target. Unrelated notes remain
            # eligible for the historical create flow.
            if conflicting is None and (note_session == session_id or note_source == source):
                conflicting = note
        return conflicting

    async def _list_session_note(self, day: str, session_id: str) -> dict | None:
        list_response = await self.run_job("daily_list", date=day)
        if not list_response.success:
            raise RuntimeError(f"daily_list failed: {list_response.answer}")
        notes = list_response.metadata.get("notes") or []
        return self._find_session_note(notes, session_id, fail_closed=self._option_b_enabled())

    def _provenance_enabled(self) -> bool:
        """Limit trusted transcript provenance to the Fermes AgentScope step."""
        return type(self) is AutoMemoryStep and getattr(self.agent_wrapper, "backend", None) == "agentscope"

    def _option_b_enabled(self) -> bool:
        """Return whether this invocation is the explicitly opted-in protected flow."""
        return (
            self.kwargs.get("durable_commit") is True
            and self._provenance_enabled()
            and isinstance(
                self.context.get("request_id") if self.context is not None else None,
                str,
            )
        )

    def _option_b_scope(self) -> str:
        scope = self.kwargs.get("agent_scope")
        if not isinstance(scope, str) or not scope.strip() or len(scope) > 128:
            raise ValueError("protected memory commit requires a configured agent_scope")
        return scope

    @staticmethod
    def _option_b_note_owned_by_session(metadata: object, trusted: dict) -> bool:
        """Require exact session ownership without binding prior notes to a new hash."""
        if not note_owned_by_session(metadata, trusted) or not isinstance(metadata, dict):
            return False
        if metadata.get(_SESSION_ID_KEY) != trusted.get(_SESSION_ID_KEY):
            return False
        for key in (_SOURCE_CONVERSATION_KEY,):
            if key in metadata and metadata.get(key) != trusted.get(key):
                return False
        return True

    def _owned_note_target(self, relative: str) -> Path:
        """Resolve a protected Markdown target and reject symlink components."""
        workspace = self.file_store.workspace_path.resolve()
        lexical = workspace / relative
        if _has_lexical_symlink(workspace, lexical):
            raise ValueError("protected note path contains a symlink")
        target, error = resolve_path(workspace, relative)
        if error or target is None:
            raise ValueError(error or "Invalid protected note path")
        if Path(relative).suffix.lower() != ".md" or _has_lexical_symlink(workspace, target):
            raise ValueError("protected note path contains a symlink or is not Markdown")
        if not _check_path_permission(workspace, target, self.context.get("_allowed_paths")):
            raise PermissionError(f"No permission to write {relative}")
        return target

    def _read_owned_note(self, relative: str, *, missing_ok: bool = False) -> bytes | None:
        target = self._owned_note_target(relative)
        try:
            return _read_exact_owned_file(target)
        except FileNotFoundError:
            if missing_ok:
                return None
            raise
        except (OSError, UnicodeError) as exc:
            raise ValueError(f"protected note is not a readable regular file: {relative}") from exc

    @staticmethod
    def _canonical_stored_body(body: str) -> str:
        """Return the body that the Markdown serializer stores and reloads."""
        serialized = frontmatter.dumps(frontmatter.Post(body))
        return frontmatter.loads(serialized).content

    @classmethod
    def _body_digest(cls, body: str) -> str:
        """Digest the canonical stored body, not the pre-serialization proposal."""
        canonical_body = cls._canonical_stored_body(body)
        return hashlib.sha256(canonical_body.encode("utf-8")).hexdigest()

    @staticmethod
    def _valid_marker_source_hash(marker: dict) -> bool:
        """Check the marker's source hash before any nested values are read."""
        source_hash = marker.get("source_hash")
        if not isinstance(source_hash, dict):
            return False
        if source_hash.get("algorithm") != "sha256":
            return False
        if source_hash.get("format") != "hex":
            return False
        value = source_hash.get("value")
        if not isinstance(value, str):
            return False
        return len(value) == 64 and all(char in "0123456789abcdef" for char in value.lower())

    def _validate_existing_marker(
        self,
        post: frontmatter.Post,
        *,
        relative: str,
        session_id: str,
        source_hash: dict | None,
        agent_scope: str,
    ) -> dict:
        """Validate a prior application marker before allowing a new request to update a note."""
        marker = post.metadata.get(_OPTION_B_MARKER)
        required = {
            "version": _OPTION_B_VERSION,
            "session_id": session_id,
            "agent_scope": agent_scope,
            "note_path": relative,
        }
        marker_keys = {*required, "source_hash", "request_id", "fingerprint", "body_sha256", "created"}
        if not isinstance(marker, dict):
            raise ValueError("protected note marker is missing or contradictory")
        if set(marker) != marker_keys or any(marker.get(key) != value for key, value in required.items()):
            raise ValueError("protected note marker is missing or contradictory")
        if not self._valid_marker_source_hash(marker):
            raise ValueError("protected note marker is missing or contradictory")
        if not all(isinstance(marker.get(key), str) for key in ("request_id", "fingerprint", "body_sha256")):
            raise ValueError("protected note marker is missing or contradictory")
        if not isinstance(marker.get("created"), bool):
            raise ValueError("protected note marker is missing or contradictory")
        if marker.get("body_sha256") != self._body_digest(post.content):
            raise ValueError("protected note marker is missing or contradictory")
        if source_hash is not None and marker.get("source_hash") != source_hash:
            raise ValueError("protected note marker is missing or contradictory")
        return marker

    def _read_commit_note(
        self,
        relative: str,
        *,
        request_id: str,
        fingerprint: str,
        session_id: str,
        source_hash: dict,
        agent_scope: str,
        expected_body_digest: str | None = None,
    ) -> tuple[frontmatter.Post, bytes, dict]:
        """Read and verify an application-owned marker and its exact body digest."""
        raw = self._read_owned_note(relative)
        assert raw is not None
        try:
            post = frontmatter.loads(raw.decode("utf-8"))
        except (UnicodeError, ValueError, TypeError) as exc:
            raise ValueError("protected note is unreadable") from exc
        marker = self._validate_existing_marker(
            post,
            relative=relative,
            session_id=session_id,
            source_hash=source_hash,
            agent_scope=agent_scope,
        )
        if marker.get("request_id") != request_id or marker.get("fingerprint") != fingerprint:
            raise ValueError("protected note marker is missing or contradictory")
        body_digest = self._body_digest(post.content)
        if marker.get("body_sha256") != body_digest or (
            expected_body_digest is not None and body_digest != expected_body_digest
        ):
            raise ValueError("protected note body digest does not match its marker")
        return post, raw, marker

    def _option_b_receipt_path(self, session_id: str, request_id: str) -> Path:
        """Return the protected receipt path for a prior request in this session."""
        session_key = hashlib.sha256(session_id.encode()).hexdigest()
        request_key = hashlib.sha256(request_id.encode()).hexdigest()
        relative = f"{self.config_value('metadata_dir')}/auto_memory_requests/{session_key}/{request_key}.json"
        return _checked_write_path(self.file_store.workspace_path, relative, self.context.get("_allowed_paths"))

    def _validate_option_b_receipt_record(
        self,
        record: dict,
        *,
        request_id: str,
        fingerprint: str,
        session_id: str,
        marker: dict,
        note_raw: bytes,
    ) -> Response:
        """Validate a receipt against the exact marker and note it claims to complete."""
        if not isinstance(record, dict) or record.get("version") != _OPTION_B_VERSION:
            raise RuntimeError("protected receipt is malformed")
        if record.get("outcome", _OPTION_B_COMMIT_OUTCOME) != _OPTION_B_COMMIT_OUTCOME:
            raise RuntimeError("protected receipt is not a commit")
        if record.get("fingerprint") != fingerprint:
            raise RuntimeError("protected receipt fingerprint does not match its marker")
        response = Response.model_validate(record.get("response"))
        if not response.success or record.get("commit") != marker:
            raise RuntimeError("protected receipt does not contain a matching successful commit")
        note_path = marker.get("note_path")
        if not isinstance(note_path, str) or not note_path:
            raise RuntimeError("protected receipt has no note path")
        expected_changes = [{"change": "added" if marker["created"] else "modified", "path": note_path}]
        if record.get("note_sha256") != hashlib.sha256(note_raw).hexdigest():
            raise RuntimeError("protected receipt does not match the committed note bytes")
        matches = (
            response.metadata.get("outcome", _OPTION_B_COMMIT_OUTCOME) != _OPTION_B_COMMIT_OUTCOME
            or response.metadata.get("commit") != marker
            or response.metadata.get("path") != note_path
            or response.metadata.get("source_hash") != marker["source_hash"]
            or response.metadata.get("source_conversation") != self._session_link(session_id)
            or record.get("changes") != expected_changes
        )
        if matches:
            raise RuntimeError("protected receipt response is not bound to its commit")
        if marker.get("request_id") != request_id:
            raise RuntimeError("protected receipt request does not match its marker")
        return response

    @staticmethod
    def _validate_option_b_noop_receipt_record(record: dict, *, fingerprint: str) -> Response:
        """Validate a durable no-op receipt without requiring commit evidence."""
        if not isinstance(record, dict) or record.get("version") != _OPTION_B_VERSION:
            raise RuntimeError("protected receipt is malformed")
        if record.get("outcome") != _OPTION_B_NOOP_OUTCOME:
            raise RuntimeError("protected receipt is not a no-op")
        if record.get("fingerprint") != fingerprint:
            raise RuntimeError("protected receipt fingerprint does not match its request")
        if any(key in record for key in ("commit", "note_sha256")) or record.get("changes") != []:
            raise RuntimeError("protected no-op receipt contains commit evidence")
        response = Response.model_validate(record.get("response"))
        metadata = response.metadata
        if (
            not response.success
            or metadata.get("outcome") != _OPTION_B_NOOP_OUTCOME
            or metadata.get("modified") is not False
            or metadata.get("n_messages") != 0
            or any(key in metadata for key in ("commit", "note_sha256", "path", "created"))
        ):
            raise RuntimeError("protected no-op receipt response is malformed")
        return response

    def _validate_superseded_option_b_revision(
        self,
        *,
        relative: str,
        note_raw: bytes,
        session_id: str,
        agent_scope: str,
    ) -> dict:
        """Prove a prior marked revision is complete before allowing supersession."""
        try:
            post = frontmatter.loads(note_raw.decode("utf-8"))
        except (UnicodeError, ValueError, TypeError) as exc:
            raise RuntimeError("existing protected note is unreadable") from exc
        marker = self._validate_existing_marker(
            post,
            relative=relative,
            session_id=session_id,
            # A completed prior request is bound to the transcript bytes that
            # existed for that request. B may append new messages, so validate
            # A's source hash shape here and bind B's current hash to B below.
            source_hash=None,
            agent_scope=agent_scope,
        )
        prior_request_id = marker.get("request_id")
        prior_fingerprint = marker.get("fingerprint")
        if not isinstance(prior_request_id, str) or not isinstance(prior_fingerprint, str):
            raise TypeError("protected note marker has no prior request identity")
        receipt_path = self._option_b_receipt_path(session_id, prior_request_id)
        try:
            receipt_raw = _read_exact_owned_file(receipt_path)
            record = json.loads(receipt_raw.decode("utf-8"))
        except FileNotFoundError as exc:
            raise RuntimeError("protected prior commit has no durable receipt") from exc
        except (OSError, UnicodeError, ValueError, TypeError) as exc:
            raise RuntimeError("protected prior commit receipt is unreadable") from exc
        self._validate_option_b_receipt_record(
            record,
            request_id=prior_request_id,
            fingerprint=prior_fingerprint,
            session_id=session_id,
            marker=marker,
            note_raw=note_raw,
        )
        return marker

    async def _recover_option_b_before_source_append(self, *, day: str, session_id: str, messages: list[Msg]) -> bool:
        """Recover a marked commit before replay messages can alter its source hash."""
        assert self.context is not None
        request_id = self.context.get("request_id")
        fingerprint = self.context.get("_option_b_fingerprint")
        if not isinstance(request_id, str) or not isinstance(fingerprint, str):
            raise TypeError("protected memory commit requires request_id fingerprint state")

        note = await self._list_session_note(day, session_id)
        if not note:
            return False
        relative = str(note.get("path") or "")
        if not relative:
            raise RuntimeError("protected session note has no path")
        raw = self._read_owned_note(relative, missing_ok=True)
        if raw is None:
            return False
        try:
            post = frontmatter.loads(raw.decode("utf-8"))
        except (UnicodeError, ValueError, TypeError) as exc:
            raise RuntimeError("existing protected note is unreadable") from exc
        marker = post.metadata.get(_OPTION_B_MARKER)
        if marker is None:
            return False
        if not isinstance(marker, dict):
            raise RuntimeError("protected note marker is malformed")

        if marker.get("request_id") == request_id:
            provenance = trusted_provenance(self.file_store.workspace_path, self._session_dir(), session_id)
            _, committed_raw, committed_marker = self._read_commit_note(
                relative,
                request_id=request_id,
                fingerprint=fingerprint,
                session_id=session_id,
                source_hash=provenance["source_hash"],
                agent_scope=self._option_b_scope(),
            )
            index_payload = await refresh_day_index(self.file_store, day, self.config_value("daily_dir"))
            self.context["changes"] = [
                {
                    "change": "added" if committed_marker["created"] else "modified",
                    "path": relative,
                },
            ]
            await self._publish_option_b_index()
            self._option_b_response(
                marker=committed_marker,
                day=day,
                session_id=session_id,
                n_messages=len(messages),
                answer="Recovered durable memory commit without model replay",
            )
            self.context.response.metadata["index"] = index_payload
            self.context.response.metadata["note_sha256"] = hashlib.sha256(committed_raw).hexdigest()
            return True

        # A later request may supersede this note only after the prior request's
        # own marker, body, scope, and receipt have been validated. Its source
        # hash is deliberately not compared with the current transcript hash:
        # this request may have appended new source turns.
        self._validate_superseded_option_b_revision(
            relative=relative,
            note_raw=raw,
            session_id=session_id,
            agent_scope=self._option_b_scope(),
        )
        return False

    async def _publish_option_b_index(self) -> None:
        """Publish protected note changes before reporting a successful commit.

        The background watcher is best-effort and may not run before an embedded
        provider is shut down. Protected commits therefore use the existing file
        store update step synchronously. A failed publication is raised so no
        receipt is written and a later retry can reconcile the note.
        """
        assert self.context is not None
        changes = self.context.get("changes") or []
        if not changes:
            raise RuntimeError("protected memory commit has no index changes")
        responses = await self.dispatch_steps(
            [{"backend": "update_index_step", "persist": True}],
            changes=changes,
        )
        if not responses or any(not response.success for response in responses):
            raise RuntimeError("protected memory commit index publication failed")

    def _option_b_response(
        self,
        *,
        marker: dict,
        day: str,
        session_id: str,
        n_messages: int,
        answer: str,
    ) -> None:
        assert self.context is not None
        path = str(marker["note_path"])
        created = bool(marker.get("created"))
        self.context["changes"] = [{"change": "added" if created else "modified", "path": path}]
        self.context.response.success = True
        self.context.response.answer = answer
        self.context.response.metadata.update(
            {
                "date": day,
                "path": path,
                "created": created,
                "modified": True,
                "n_messages": n_messages,
                "source_conversation": self._session_link(session_id),
                "source_hash": marker["source_hash"],
                "commit": marker,
            },
        )

    @staticmethod
    def _proposal_text(result: object) -> str | None:
        if not isinstance(result, dict):
            return None
        value = result.get("result")
        if not isinstance(value, str):
            return None
        text = value.strip()
        if text.startswith("```") and text.endswith("```"):
            lines = text.splitlines()
            if len(lines) >= 3:
                text = "\n".join(lines[1:-1]).strip()
        return text or None

    @staticmethod
    def _proposal_execution_valid(result: object) -> bool:
        """Accept only read evidence; protected mode exposes no mutating tools."""
        if not isinstance(result, dict) or not isinstance(result.get("tool_execution"), list):
            return True
        calls: dict[str, str] = {}
        results: dict[str, tuple[str, str]] = {}
        for block in result["tool_execution"]:
            if not isinstance(block, dict) or block.get("type") not in {"tool_call", "tool_result"}:
                continue
            name = block.get("name")
            call_id = block.get("call_id", block.get("id"))
            if name != "read" or not isinstance(call_id, str) or not call_id:
                return False
            if block["type"] == "tool_call":
                if call_id in calls:
                    return False
                calls[call_id] = name
            else:
                state = str(block.get("state") or "").lower().rsplit(".", 1)[-1]
                if call_id in results:
                    return False
                results[call_id] = (name, state)
        return set(calls) == set(results) and all(state in _SUCCESSFUL_TOOL_STATES for _, state in results.values())

    async def _execute_option_b(
        self,
        *,
        day: str,
        session_id: str,
        messages: list[Msg],
        memory_hint: str,
        created: bool,
        note_path: str,
        before_note_bytes: bytes | None,
        provenance: dict,
        image_sources: list[str],
        reply_kwargs: dict,
    ) -> bool:
        """Run the untrusted proposal phase and application-owned note commit."""
        assert self.context is not None
        request_id = self.context.get("request_id")
        fingerprint = self.context.get("_option_b_fingerprint")
        if not isinstance(request_id, str) or not isinstance(fingerprint, str):
            raise TypeError("protected memory commit requires request_id fingerprint state")
        agent_scope = self._option_b_scope()

        # A matching marker is the recovery path for SIGKILL after note commit.
        # Structural validation is independent of the current transcript hash:
        # a distinct request may have appended new source turns since this
        # marker was committed.
        if note_path:
            current = self._read_owned_note(note_path, missing_ok=True)
            existing_marker = None
            if current is not None:
                try:
                    existing = frontmatter.loads(current.decode("utf-8"))
                except (UnicodeError, ValueError, TypeError) as exc:
                    raise RuntimeError("existing protected note is unreadable") from exc
                if not self._option_b_note_owned_by_session(existing.metadata, provenance):
                    raise RuntimeError("existing protected note belongs to a different source session")
                existing_marker = existing.metadata.get(_OPTION_B_MARKER)
                if existing_marker is not None and not isinstance(existing_marker, dict):
                    raise RuntimeError("protected note marker is malformed")
            if existing_marker is not None and existing_marker.get("request_id") == request_id:
                # Same-ID mismatches are corruption or input reuse and must
                # never fall through to another model mutation.
                _, raw, marker = self._read_commit_note(
                    note_path,
                    request_id=request_id,
                    fingerprint=fingerprint,
                    session_id=session_id,
                    source_hash=provenance["source_hash"],
                    agent_scope=agent_scope,
                )
                index_payload = await refresh_day_index(self.file_store, day, self.config_value("daily_dir"))
                self.context["changes"] = [
                    {
                        "change": "added" if marker["created"] else "modified",
                        "path": note_path,
                    },
                ]
                await self._publish_option_b_index()
                self._option_b_response(
                    marker=marker,
                    day=day,
                    session_id=session_id,
                    n_messages=len(messages),
                    answer="Recovered durable memory commit without model replay",
                )
                self.context.response.metadata["index"] = index_payload
                self.context.response.metadata["note_sha256"] = hashlib.sha256(raw).hexdigest()
                return True
            if existing_marker is not None:
                self._validate_superseded_option_b_revision(
                    relative=note_path,
                    note_raw=current,
                    session_id=session_id,
                    agent_scope=agent_scope,
                )

        expected_path = note_path
        if created:
            # The model chooses only a filename component; the application
            # derives the daily path and never accepts a model path escape.
            expected_path = ""
        injected = dict(reply_kwargs.get("injected_job_kwargs") or {})
        if not created:
            injected["_allowed_paths"] = [note_path]
        reply_kwargs["injected_job_kwargs"] = injected
        result = await self.agent_wrapper.reply(
            self.prompt_format(
                "option_b_user_message",
                today=day,
                note=memory_hint or "(none)",
                note_path=note_path or "(new note; application derives the path)",
                session_id=session_id,
                session_file=self._session_source_path(session_id),
                history=self._format_history(messages),
            ),
            system_prompt=self.prompt_format("option_b_system_prompt", agent_scope=agent_scope),
            job_tools=[] if created else ["read"],
            **reply_kwargs,
        )
        if not self._proposal_execution_valid(result):
            raise RuntimeError("protected proposal contained failed, unknown, or ambiguous tool execution")
        text = self._proposal_text(result)
        if text is None:
            raise RuntimeError("protected proposal was not a JSON revision")
        try:
            proposal = json.loads(text)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("protected proposal was not valid JSON") from exc
        if not isinstance(proposal, dict) or set(proposal) != {"path", "name", "description", "content"}:
            raise RuntimeError("protected proposal must contain exactly path, name, description, and content")
        if any(not isinstance(proposal[key], str) for key in proposal):
            raise RuntimeError("protected proposal fields must be strings")
        name = proposal["name"]
        description = proposal["description"]
        content = proposal["content"]
        if validate_filename_component(name, kind="name") or not description.strip():
            raise RuntimeError("protected proposal has invalid name or description")
        body = content if content.endswith("\n") else content + "\n"
        if len(body.encode("utf-8")) > self.max_file_bytes():
            raise RuntimeError("protected proposal exceeds the file byte limit")
        if created:
            expected_path = self._daily_note_path(day, name)
        elif proposal["path"] != note_path:
            raise RuntimeError("protected update proposal changed the application-owned note path")
        if proposal["path"] != expected_path:
            raise RuntimeError("protected proposal path does not match the application-owned daily path")
        target = self._owned_note_target(expected_path)
        async with await get_path_lock(target):
            current = self._read_owned_note(expected_path, missing_ok=True)
            if current != before_note_bytes:
                raise RuntimeError("protected note changed while the model prepared its revision")
            if current is not None:
                try:
                    existing = frontmatter.loads(current.decode("utf-8"))
                except (UnicodeError, ValueError, TypeError) as exc:
                    raise RuntimeError("existing protected note is unreadable") from exc
                if not self._option_b_note_owned_by_session(existing.metadata, provenance):
                    raise RuntimeError("existing protected note belongs to a different source session")
                existing_marker = existing.metadata.get(_OPTION_B_MARKER)
                if existing_marker is not None:
                    if not isinstance(existing_marker, dict):
                        raise RuntimeError("protected note marker is malformed")
                    if existing_marker.get("request_id") == request_id:
                        raise RuntimeError("protected note marker already belongs to this request but did not recover")
                    self._validate_superseded_option_b_revision(
                        relative=expected_path,
                        note_raw=current,
                        session_id=session_id,
                        agent_scope=agent_scope,
                    )
                metadata = {
                    key: value
                    for key, value in dict(existing.metadata or {}).items()
                    if key not in _OPTION_B_RESERVED_METADATA
                }
            else:
                metadata = {}
            marker = {
                "version": _OPTION_B_VERSION,
                "request_id": request_id,
                "fingerprint": fingerprint,
                "session_id": session_id,
                "source_hash": provenance["source_hash"],
                "agent_scope": agent_scope,
                "note_path": expected_path,
                "body_sha256": self._body_digest(body),
                "created": created,
            }
            metadata.update(
                {
                    "name": name,
                    "description": description,
                    **provenance,
                    _OPTION_B_MARKER: marker,
                },
            )
            post = frontmatter.Post(body, **metadata)
            serialized = frontmatter.dumps(post)
            await write_file_durable(target, serialized)
            committed, raw, committed_marker = self._read_commit_note(
                expected_path,
                request_id=request_id,
                fingerprint=fingerprint,
                session_id=session_id,
                source_hash=provenance["source_hash"],
                agent_scope=agent_scope,
                expected_body_digest=marker["body_sha256"],
            )
            del committed
        self.context["changes"] = [{"change": "added" if created else "modified", "path": expected_path}]
        index_payload = await refresh_day_index(self.file_store, day, self.config_value("daily_dir"))
        await self._publish_option_b_index()
        self.context.response.success = True
        self.context.response.answer = "Committed one durable memory revision"
        self.context.response.metadata.update(
            {
                "date": day,
                "path": expected_path,
                "created": created,
                "modified": True,
                "n_messages": len(messages),
                "source_conversation": self._session_link(session_id),
                "source_hash": provenance["source_hash"],
                "index": index_payload,
                "commit": committed_marker,
                "note_sha256": hashlib.sha256(raw).hexdigest(),
                "image_sources": image_sources,
            },
        )
        return True

    async def _replay_option_b_receipt(self, record: dict, *, fingerprint: str, session_id: str) -> None:
        """Validate a protected receipt and its note before replaying its response."""
        assert self.context is not None
        if not isinstance(record, dict) or record.get("version") != _OPTION_B_VERSION:
            raise RuntimeError("protected receipt is malformed")
        if record.get("fingerprint") != fingerprint:
            self.context.response.success = False
            self.context.response.answer = "Error: request_id was already used for different input"
            return
        if record.get("outcome") == _OPTION_B_NOOP_OUTCOME:
            response = self._validate_option_b_noop_receipt_record(record, fingerprint=fingerprint)
            self.context.response.answer = response.answer
            self.context.response.success = response.success
            self.context.response.metadata.update(response.metadata)
            self.context["changes"] = []
            return
        if record.get("outcome", _OPTION_B_COMMIT_OUTCOME) != _OPTION_B_COMMIT_OUTCOME:
            raise RuntimeError("protected receipt has an unknown outcome")
        marker = record.get("commit")
        if not isinstance(marker, dict):
            raise TypeError("protected receipt does not contain a successful commit")
        provenance = trusted_provenance(self.file_store.workspace_path, self._session_dir(), session_id)
        scope = self._option_b_scope()
        note_path = marker.get("note_path")
        if not isinstance(note_path, str) or not note_path:
            raise RuntimeError("protected receipt has no note path")
        _, raw, note_marker = self._read_commit_note(
            note_path,
            request_id=str(self.context.get("request_id")),
            fingerprint=fingerprint,
            session_id=session_id,
            source_hash=provenance["source_hash"],
            agent_scope=scope,
        )
        if note_marker != marker:
            raise RuntimeError("protected receipt does not match the committed note marker")
        response = self._validate_option_b_receipt_record(
            record,
            request_id=str(self.context.get("request_id")),
            fingerprint=fingerprint,
            session_id=session_id,
            marker=marker,
            note_raw=raw,
        )
        expected_changes = [{"change": "added" if marker["created"] else "modified", "path": note_path}]
        self.context["changes"] = expected_changes
        await self._publish_option_b_index()
        self.context.response.answer = response.answer
        self.context.response.success = response.success
        self.context.response.metadata.update(response.metadata)

    async def _write_option_b_receipt(self, path: Path, fingerprint: str) -> None:
        """Persist the successful protected response after index refresh."""
        assert self.context is not None
        outcome = self.context.response.metadata.get("outcome", _OPTION_B_COMMIT_OUTCOME)
        if outcome == _OPTION_B_NOOP_OUTCOME:
            metadata = self.context.response.metadata
            if (
                not self.context.response.success
                or metadata.get("modified") is not False
                or metadata.get("n_messages") != 0
                or any(key in metadata for key in ("commit", "note_sha256", "path", "created"))
                or self.context.get("changes")
            ):
                raise TypeError("protected no-op has unexpected commit evidence")
            record = {
                "version": _OPTION_B_VERSION,
                "outcome": _OPTION_B_NOOP_OUTCOME,
                "fingerprint": fingerprint,
                "response": self.context.response.model_dump(mode="json"),
                "changes": [],
            }
            await write_file_durable(path, json.dumps(record, ensure_ascii=False, sort_keys=True))
            return
        if outcome != _OPTION_B_COMMIT_OUTCOME:
            raise TypeError("protected success has an unknown outcome")
        commit = self.context.response.metadata.get("commit")
        note_sha256 = self.context.response.metadata.get("note_sha256")
        if not isinstance(commit, dict) or not isinstance(note_sha256, str):
            raise TypeError("protected success has no complete commit evidence")
        record = {
            "version": _OPTION_B_VERSION,
            "outcome": _OPTION_B_COMMIT_OUTCOME,
            "fingerprint": fingerprint,
            "commit": commit,
            "note_sha256": note_sha256,
            "response": self.context.response.model_dump(mode="json"),
            "changes": self.context.get("changes") or [],
        }
        await write_file_durable(path, json.dumps(record, ensure_ascii=False, sort_keys=True))

    async def _ensure_session_frontmatter(
        self,
        path: str,
        session_id: str,
        image_sources: list[str] | None = None,
    ) -> None:
        metadata = (
            trusted_provenance(self.file_store.workspace_path, self._session_dir(), session_id)
            if self._provenance_enabled()
            else {"session_id": session_id, "source_conversation": self._session_link(session_id)}
        )
        if image_sources is not None and (
            not isinstance(image_sources, list) or any(not isinstance(source, str) for source in image_sources)
        ):
            raise ValueError("source_images must be a list of strings")
        if image_sources:
            workspace = self.file_store.workspace_path.resolve()
            allowed_paths = self.context.get("_allowed_paths")
            target = _checked_write_path(workspace, path, allowed_paths)
            # Read and merge under the same lock as native file Jobs so a
            # concurrent write cannot lose its source links to a stale list.
            async with await get_path_lock(target):
                target = _checked_write_path(workspace, path, allowed_paths)
                if not target.is_file():
                    raise ValueError(f"Memory note not found: {path}")
                post = frontmatter.loads(target.read_text(encoding="utf-8"))
                if self._provenance_enabled() and not note_owned_by_session(post.metadata, metadata):
                    raise RuntimeError("existing note belongs to a different source session")
                current_sources = post.metadata.get("source_images", [])
                if not isinstance(current_sources, list) or any(not isinstance(s, str) for s in current_sources):
                    raise ValueError("Existing source_images must be a list of strings")
                metadata["source_images"] = list(dict.fromkeys([*current_sources, *image_sources]))
                if not all(post.metadata.get(key) == value for key, value in metadata.items()):
                    post.metadata.update(metadata)
                    await write_file_safe(target, frontmatter.dumps(post))
            return
        current = self._frontmatter(path)
        if all(current.get(key) == value for key, value in metadata.items()):
            return
        response = await self.run_job(
            "frontmatter_update",
            path=path,
            metadata=metadata,
            **({PROVENANCE_SESSION_KEY: session_id} if self._provenance_enabled() else {}),
        )
        if not response.success:
            raise RuntimeError(f"frontmatter_update failed: {response.answer}")

    async def _rename_from_frontmatter_name(self, path: str, day: str) -> str:
        meta = self._frontmatter(path)
        name = str(meta.get("name", "")).strip()
        if not name:
            return path
        if err := validate_filename_component(name, kind="name"):
            raise RuntimeError(err)

        target_path = self._daily_note_path(day, name)
        if target_path == path:
            return path

        move_response = await self.run_job(
            "move",
            src_path=path,
            dst_path=target_path,
            overwrite=False,
            retarget=True,
        )
        if not move_response.success:
            raise RuntimeError(f"move failed: {move_response.answer}")
        return target_path

    async def _save_session_messages(self, session_id: str, messages: list[Msg]) -> None:
        if not session_id or not messages:
            return

        path = self._session_path(session_id)
        self.logger.info(
            f"[{self.name}] save session start session_id={session_id!r} messages={len(messages)} path={path}",
        )

        async with await get_path_lock(path):
            existing: list[Msg] = []
            if path.exists():
                async with aiofiles.open(path, encoding="utf-8") as f:
                    content = await f.read()
                for line in content.splitlines():
                    line = line.strip()
                    if line:
                        try:
                            existing.append(Msg.model_validate_json(line))
                        except Exception:
                            pass

            by_id: dict[str, Msg] = {}
            for msg in existing:
                by_id[msg.id] = msg
            incoming = [_sanitize_msg_for_save(msg) for msg in messages]
            existing_by_turn: dict[str, list[Msg]] = {}
            for msg in existing:
                existing_by_turn.setdefault(_source_turn_key(msg), []).append(msg)
            protected = self._option_b_enabled()
            new_messages = []
            for msg in incoming:
                if msg.id in by_id:
                    by_id[msg.id] = msg
                elif protected and (matches := existing_by_turn.get(_source_turn_key(msg))):
                    # Hermes may reconstruct the same turn with a fresh ID and
                    # timestamp on every retry. Preserve the durable row's
                    # identity instead of appending a duplicate.
                    matches.pop(0)
                else:
                    new_messages.append(msg)
            merged = sorted([*by_id.values(), *new_messages], key=lambda m: m.created_at)

            can_append = 0 < len(existing) <= len(merged) and all(
                merged[i].id == existing[i].id for i in range(len(existing))
            )

            path.parent.mkdir(parents=True, exist_ok=True)

            if can_append:
                new_msgs = merged[len(existing) :]
                if new_msgs:
                    async with aiofiles.open(path, "a", encoding="utf-8") as f:
                        for msg in new_msgs:
                            await f.write(msg.model_dump_json() + "\n")
                    self.logger.info(
                        f"[{self.name}] save session appended session_id={session_id!r} "
                        f"existing={len(existing)} appended={len(new_msgs)} total={len(merged)}",
                    )
                else:
                    self.logger.info(
                        f"[{self.name}] save session unchanged session_id={session_id!r} "
                        f"existing={len(existing)} total={len(merged)}",
                    )
            else:
                async with aiofiles.open(path, "w", encoding="utf-8") as f:
                    for msg in merged:
                        await f.write(msg.model_dump_json() + "\n")
                self.logger.info(
                    f"[{self.name}] save session rewrote session_id={session_id!r} "
                    f"existing={len(existing)} total={len(merged)}",
                )

    @staticmethod
    def _to_msg(item) -> Msg:
        if isinstance(item, Msg):
            return item
        if isinstance(item, dict):
            item = _normalize_msg_timestamp(item)
        if isinstance(item, dict) and isinstance(item.get("content"), str):
            item = {**item, "content": [{"type": "text", "text": item["content"]}]}
        return Msg.model_validate(item)

    @staticmethod
    def _messages_day(messages: list[Msg], timezone: str | None = None) -> str | None:
        days = [day for msg in messages if (day := AutoMemoryStep._message_day(msg.created_at, timezone))]
        return max(days) if days else None

    @staticmethod
    def _message_day(value, timezone: str | None) -> str | None:
        """Resolve an absolute timestamp to the workspace's calendar date."""
        fallback = extract_daily_date(value)
        text = str(value or "").strip()
        if not fallback or len(text) <= 10:
            return fallback
        try:
            timestamp = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return fallback
        if timestamp.tzinfo is None:
            return fallback
        if timezone:
            timestamp = timestamp.astimezone(zoneinfo.ZoneInfo(timezone))
        else:
            timestamp = timestamp.astimezone()
        return timestamp.date().isoformat()

    def _build_messages(self, raw_messages: list) -> list[Msg]:
        """Convert raw message payloads into ``Msg`` objects.

        Overridable hook: subclasses can preprocess ``raw_messages`` (e.g. fill
        in missing timestamps) before conversion.
        """
        return [self._to_msg(item) for item in raw_messages]

    def _reply_extra_kwargs(self, day: str) -> dict:  # pylint: disable=unused-argument
        """Extra keyword arguments for ``agent_wrapper.reply``.

        Overridable hook: subclasses can inject additional reply options such
        as per-tool defaults keyed on ``day``.
        """
        return {}

    @staticmethod
    def _all_requested_writes_failed(
        reply_result: dict,
        durable_tools: list[str],
        *,
        require_execution: bool = False,
    ) -> bool:
        """Reject durable execution unless every attempted call has one success.

        AgentScope can return a final text message after exhausting its
        iterations.  That text is not an acknowledgement that a file write
        happened.  Calls and results must be known, well-formed, uniquely
        identified, and paired by AgentScope's call ID.  A reply with no tool
        block is handled by the caller, which distinguishes an explicit
        retention request from a genuine no-op.
        """
        execution = reply_result.get("tool_execution") if isinstance(reply_result, dict) else None
        if isinstance(execution, list):
            content = execution
        elif require_execution:
            # AgentScope must provide an explicit current-turn record; final
            # assistant text cannot prove a durable write or a genuine no-op.
            return True
        else:
            last_message = reply_result.get("last_message") if isinstance(reply_result, dict) else None
            content = last_message.get("content") if isinstance(last_message, dict) else None
        if not isinstance(content, list):
            return False

        durable_tools = set(durable_tools)
        calls: dict[str, str] = {}
        results: dict[str, tuple[str, str]] = {}
        saw_tool_block = False
        invalid = False
        for block in content:
            if not isinstance(block, dict) or block.get("type") not in {"tool_call", "tool_result"}:
                continue
            saw_tool_block = True
            name = block.get("name")
            call_id = block.get("call_id", block.get("id"))
            if not isinstance(name, str) or not name:
                invalid = True
                continue
            if name not in durable_tools:
                # Other tools may occur in the same turn. A missing result for
                # a requested durable tool is still detected by call ID.
                continue
            if not isinstance(call_id, str) or not call_id:
                invalid = True
                continue
            if block["type"] == "tool_call":
                if call_id in calls:
                    invalid = True
                else:
                    calls[call_id] = name
            else:
                state = str(block.get("state") or "").lower().rsplit(".", 1)[-1]
                if call_id in results:
                    invalid = True
                else:
                    results[call_id] = (name, state)

        if not saw_tool_block:
            return False
        if invalid or not calls or set(calls) != set(results):
            return True
        if any(calls[call_id] != name for call_id, (name, _state) in results.items()):
            return True
        return any(state not in _SUCCESSFUL_TOOL_STATES for _name, state in results.values())

    @staticmethod
    def _requested_tool_call_observed(
        reply_result: dict,
        durable_tools: list[str],
        *,
        require_execution: bool = False,
    ) -> bool:
        """Return whether the current reply contains a call to a durable tool."""
        execution = reply_result.get("tool_execution") if isinstance(reply_result, dict) else None
        if isinstance(execution, list):
            content = execution
        elif require_execution:
            return False
        else:
            last_message = reply_result.get("last_message") if isinstance(reply_result, dict) else None
            content = last_message.get("content") if isinstance(last_message, dict) else None
        if not isinstance(content, list):
            return False
        durable_tools = set(durable_tools)
        return any(
            isinstance(block, dict) and block.get("type") == "tool_call" and block.get("name") in durable_tools
            for block in content
        )

    def _format_history(self, messages: list[Msg]) -> str:
        """Render the conversation history injected into the prompt.

        Overridable hook: subclasses can annotate messages (e.g. with their
        source line numbers in the session file) before rendering.
        """
        return format_history(messages)

    def _prepare_image_history(
        self,
        messages: list[Msg],
        day: str,
    ) -> tuple[list[Msg], dict[str, DataBlock], dict | None]:
        """Validate image inputs before saving, without reading or changing their sources."""
        include_images = self.context.get("include_images", False)
        if include_images is False:
            return messages, {}, None
        images = [
            (message_index, block_index, block)
            for message_index, message in enumerate(messages)
            for block_index, block in enumerate(message.content)
            if isinstance(block, DataBlock) and block.source.media_type.startswith("image/")
        ]
        if not images:
            return messages, {}, None
        wrapper = self.agent_wrapper
        if wrapper is None or wrapper.backend != "agentscope":
            raise NotImplementedError("Auto Memory image inputs require the AgentScope wrapper")
        for _, _, block in images:
            if block.source.type == "url" and urlsplit(str(block.source.url)).scheme not in {"http", "https"}:
                raise ValueError("Image URLs must use HTTP(S); convert local files to Base64Source before calling")
        reply_kwargs = dict(self._reply_extra_kwargs(day))
        context_config = reply_kwargs.get("context_config", wrapper.kwargs.get("context_config")) or {}
        limit = ContextConfig(**context_config).max_image_num
        if len(images) > limit:
            raise ValueError(
                f"Session has {len(images)} images, exceeding context_config.max_image_num={limit}; "
                "configure the AgentScope wrapper's image limit explicitly",
            )
        prepared = [message.model_copy(deep=True) for message in messages]
        image_blocks = {}
        prefix = f"__reme_image_{uuid4().hex}_"
        for number, (message_index, block_index, _) in enumerate(images):
            marker = f"{prefix}{number}__"
            image_blocks[marker] = prepared[message_index].content[block_index]
            prepared[message_index].content[block_index] = TextBlock(text=marker)
        return prepared, image_blocks, reply_kwargs

    def _image_user_message(self, prompt: str, images: dict[str, DataBlock]) -> UserMsg:
        """Add source instructions and restore images after rendering the memory prompt."""
        prompt += "\n\n" + self.prompt_format("image_sources_instructions")
        parts = re.split("(" + "|".join(map(re.escape, images)) + ")", prompt)
        if [part for part in parts if part in images] != list(images):
            raise ValueError("Memory prompt must preserve every image once in conversation order")
        return UserMsg(
            name="user",
            content=[images[part] if part in images else TextBlock(text=part) for part in parts if part],
        )

    # These returns reject malformed or unverifiable receipts before any write;
    # keeping the state machine linear makes the security boundary auditable.
    async def execute(self):  # pylint: disable=too-many-return-statements
        """Replay completed request IDs without repeating the memory agent."""
        assert self.context is not None
        request_id = self.context.get("request_id")
        durable_commit = self.kwargs.get("durable_commit", False)
        if type(durable_commit) is not bool:
            self.context.response.success = False
            self.context.response.answer = "Error: durable_commit must be a boolean"
            return
        protected_requested = durable_commit is True
        if request_id is None:
            if protected_requested:
                self.context.response.success = False
                self.context.response.answer = "Error: protected memory commit requires request_id"
                return
            return await self._execute()
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
            self.context.response.success = False
            self.context.response.answer = "Error: request_id must be a string of 1 to 128 characters"
            return
        if protected_requested:
            try:
                self._option_b_scope()
            except ValueError as exc:
                self.context.response.success = False
                self.context.response.answer = f"Error: {exc}"
                return
            if not self._option_b_enabled():
                self.context.response.success = False
                self.context.response.answer = (
                    "Error: protected memory commit requires the exact AgentScope AutoMemoryStep implementation"
                )
                return

        payload = {
            key: self.context.get(key) for key in ("session_id", "messages", "date", "memory_hint", "include_images")
        }
        if payload["messages"]:
            # Keep caller-provided IDs/timestamps; parsing dictionaries would generate defaults on every retry.
            payload["messages"] = [
                message.model_dump(mode="json") if isinstance(message, Msg) else message
                for message in payload["messages"]
            ]
        fingerprint = hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode(),
        ).hexdigest()
        option_b = self._option_b_enabled()
        self.context["_option_b_fingerprint"] = fingerprint
        session_key = hashlib.sha256(str(payload["session_id"]).encode()).hexdigest()
        request_key = hashlib.sha256(request_id.encode()).hexdigest()
        directory = f"{self.config_value('metadata_dir')}/auto_memory_requests/{session_key}"
        workspace = self.file_store.workspace_path
        allowed = self.context.get("_allowed_paths")
        lock_path = _checked_write_path(workspace, f"{directory}/.lock", allowed)
        async with await get_path_lock(lock_path):
            receipt_relative = f"{directory}/{request_key}.json"
            if option_b and _has_lexical_symlink(workspace.resolve(), workspace.resolve() / receipt_relative):
                self.context.response.success = False
                self.context.response.answer = "Error: protected receipt path contains a symlink"
                return
            path = _checked_write_path(workspace, receipt_relative, allowed)
            if path.exists():
                if option_b:
                    try:
                        record = json.loads(_read_exact_owned_file(path).decode("utf-8"))
                    except FileNotFoundError:
                        record = None
                    except (OSError, UnicodeError, ValueError, TypeError) as exc:
                        self.context.response.success = False
                        self.context.response.answer = f"Error: protected receipt is unreadable: {exc}"
                        return
                else:
                    record = json.loads(path.read_text(encoding="utf-8"))
                if record is None:
                    pass
                elif option_b:
                    try:
                        await self._replay_option_b_receipt(
                            record,
                            fingerprint=fingerprint,
                            session_id=str(payload["session_id"]),
                        )
                    except (KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
                        self.context.response.success = False
                        self.context.response.answer = f"Error: protected receipt replay refused: {exc}"
                    return
                if record is not None and record["fingerprint"] != fingerprint:
                    self.context.response.success = False
                    self.context.response.answer = "Error: request_id was already used for different input"
                    return
                response: Response | None = None
                if record is not None:
                    response = Response.model_validate(record["response"])
                if response is not None and response.success:
                    note_path = response.metadata.get("path")
                    if self._provenance_enabled() and isinstance(note_path, str) and note_path:
                        if not verify_note_provenance(
                            workspace,
                            workspace / note_path,
                            self._session_dir(),
                            str(payload["session_id"]),
                        ):
                            self.context.response.success = False
                            self.context.response.answer = (
                                "Error: recorded note provenance changed; receipt cannot be safely replayed"
                            )
                            return
                    elif (
                        self._provenance_enabled()
                        and isinstance(payload.get("memory_hint"), str)
                        and payload["memory_hint"].strip()
                    ):
                        self.context.response.success = False
                        self.context.response.answer = "Error: explicit retention receipt has no attributable note"
                        return
                    self.context.response.answer = response.answer
                    self.context.response.success = response.success
                    self.context.response.metadata.update(response.metadata)
                    self.context["changes"] = record["changes"]
                    return
                if response is not None:
                    self.context.response.answer = response.answer
                    self.context.response.success = response.success
                    self.context.response.metadata.update(response.metadata)
                    self.context["changes"] = record["changes"]
                    return
            await self._execute()
            if self.context.response.success:
                path = _checked_write_path(workspace, f"{directory}/{request_key}.json", allowed)
                if option_b:
                    await self._write_option_b_receipt(path, fingerprint)
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    temporary = _checked_write_path(workspace, f"{directory}/{request_key}.tmp", allowed)
                    await write_file_safe(
                        temporary,
                        json.dumps(
                            {
                                "fingerprint": fingerprint,
                                "response": self.context.response.model_dump(mode="json"),
                                "changes": self.context.get("changes") or [],
                            },
                        ),
                    )
                    temporary.replace(path)

    # pylint: disable=too-many-return-statements,too-many-statements
    async def _execute(self):
        assert self.context is not None
        self.context["changes"] = []
        raw_messages = self.context.get("messages") or []
        session_id: str = self.context.get("session_id", "")
        memory_hint: str = self.context.get("memory_hint", "")
        retention_requested = isinstance(memory_hint, str) and bool(memory_hint.strip())
        raw_date = self.context.get("date", "")
        tz = self.app_context.app_config.timezone if self.app_context is not None else None
        current = now(tz)

        messages: list[Msg] = self._build_messages(raw_messages)
        self.logger.info(
            f"[{self.name}] start session_id={session_id!r} raw_messages={len(raw_messages)} "
            f"messages={len(messages)} hint={bool(memory_hint)}",
        )

        if session_id and (err := validate_session_id(session_id)):
            self.context.response.success = False
            self.context.response.answer = f"Error: {err}"
            self.logger.warning(f"[{self.name}] invalid session_id={session_id!r} err={err}")
            return
        if not session_id:
            self.context.response.success = False
            self.context.response.answer = "Error: session_id is required"
            self.logger.warning(f"[{self.name}] missing session_id")
            return

        day = (
            parse_daily_date(raw_date) if raw_date else self._messages_day(messages, tz) or current.strftime("%Y-%m-%d")
        )
        if raw_date and day is None:
            self.context.response.success = False
            self.context.response.answer = "Error: date must be YYYY-MM-DD"
            self.context.response.metadata.update({"date": raw_date, "modified": False, "n_messages": len(messages)})
            self.logger.warning(f"[{self.name}] invalid date={raw_date!r}")
            return

        protected = self._provenance_enabled()
        option_b = self._option_b_enabled()
        history_messages, images, reply_kwargs = self._prepare_image_history(messages, day)
        if option_b and images:
            self.context.response.success = False
            self.context.response.answer = "Error: protected memory commit does not support image attachments yet"
            return
        if (
            option_b
            and messages
            and await self._recover_option_b_before_source_append(
                day=day,
                session_id=session_id,
                messages=messages,
            )
        ):
            return
        await self._save_session_messages(session_id, messages)
        if not messages:
            self.context.response.success = True
            self.context.response.answer = "Skipped: no messages"
            self.context.response.metadata.update(
                {
                    "date": day,
                    "modified": False,
                    "n_messages": 0,
                    "outcome": _OPTION_B_NOOP_OUTCOME if option_b else None,
                },
            )
            if not option_b:
                self.context.response.metadata.pop("outcome", None)
            self.logger.info(f"[{self.name}] Skipped: no messages session_id={session_id!r} modified=False")
            return

        provenance = {}
        if protected:
            try:
                provenance = trusted_provenance(self.file_store.workspace_path, self._session_dir(), session_id)
            except ProvenanceError as exc:
                self.context.response.success = False
                self.context.response.answer = f"Error: source provenance unavailable: {exc}"
                self.context.response.metadata.update({"date": day, "modified": False, "n_messages": len(messages)})
                return

        try:
            note = await self._list_session_note(day, session_id)
        except RuntimeError as exc:
            self.context.response.success = False
            self.context.response.answer = str(exc)
            self.context.response.metadata.update({"date": day, "modified": False, "n_messages": len(messages)})
            self.logger.info(f"[{self.name}] list failed session_id={session_id!r} answer={str(exc)!r}")
            return

        note_path = str(note["path"]) if note else ""
        created = note is None
        before_note_path = note_path
        before_note_bytes = (
            self._read_owned_note(note_path, missing_ok=True)
            if option_b and note_path
            else self._note_bytes(note_path) if note_path else None
        )
        # Keep attachment provenance even when the Agent uses a full rewrite,
        # or this call updates an image-backed note with images disabled.
        image_sources = []
        if before_note_bytes is not None:
            previous_sources = frontmatter.loads(before_note_bytes.decode("utf-8")).get("source_images", [])
            if isinstance(previous_sources, list) and all(isinstance(source, str) for source in previous_sources):
                image_sources = previous_sources
            elif images:
                raise ValueError("Existing source_images must be a list of strings")
        if images:
            image_sources += await _save_session_images(
                self.file_store.workspace_path,
                self._session_dir(),
                session_id,
                history_messages,
                images,
                self.context.get("_allowed_paths"),
            )
        self.logger.info(
            f"[{self.name}] note lookup session_id={session_id!r} path={note_path!r} "
            f"created={created} msgs={len(messages)} hint={bool(memory_hint)}",
        )
        template_key = "user_message_create" if created else "user_message_update"
        user_message = self.prompt_format(
            template_key,
            today=day,
            note=memory_hint or "(none)",
            note_path=note_path,
            session_id=session_id,
            session_file=self._session_source_path(session_id),
            history=self._format_history(history_messages),
        )
        if images:
            user_message = self._image_user_message(user_message, images)

        self.logger.info(f"[{self.name}] agent start path={note_path} template={template_key}")
        # Existing-note updates are restricted to the resolved note path. New
        # notes retain the upstream ``daily_write`` date behavior, where the
        # model supplies the date from the prompt.
        if reply_kwargs is None:
            reply_kwargs = self._reply_extra_kwargs(day)
        is_agentscope = getattr(self.agent_wrapper, "backend", None) == "agentscope"
        injected_job_kwargs = dict(reply_kwargs.get("injected_job_kwargs") or {})
        if protected:
            injected_job_kwargs[PROVENANCE_SESSION_KEY] = session_id
        if created and protected:
            # The source session is application-owned. Keep any other wrapper
            # constraints while hiding this value from the model's tool schema.
            injected_job_kwargs["session_id"] = session_id
        if not created:
            injected_job_kwargs["_allowed_paths"] = [note_path]
        # Provenance is always server-owned, including non-AgentScope create
        # flows where the model does not receive a session_id argument.
        if injected_job_kwargs:
            reply_kwargs["injected_job_kwargs"] = injected_job_kwargs
        else:
            reply_kwargs.pop("injected_job_kwargs", None)
        if option_b:
            await self._execute_option_b(
                day=day,
                session_id=session_id,
                messages=messages,
                memory_hint=memory_hint,
                created=created,
                note_path=note_path,
                before_note_bytes=before_note_bytes,
                provenance=provenance,
                image_sources=image_sources,
                reply_kwargs=reply_kwargs,
            )
            return
        result = await self.agent_wrapper.reply(
            user_message,
            system_prompt=self.prompt_format("system_prompt"),
            job_tools=self.create_tools if created else self.update_tools,
            **reply_kwargs,
        )
        self.logger.info(f"[{self.name}] agent done path={note_path} has_result={bool(result.get('result'))}")

        # Reject incomplete/malformed durable execution before checking the
        # note on disk: a partial write must not acknowledge the whole turn.
        durable_tools = self.create_tools if created else self.update_tools
        if self._all_requested_writes_failed(result, durable_tools, require_execution=is_agentscope):
            self.context.response.success = False
            self.context.response.answer = "Error: durable memory write failed; request remains eligible for retry"
            self.context.response.metadata.update(
                {
                    "date": day,
                    "path": None if created else note_path,
                    "created": False if created else created,
                    "modified": False,
                    "n_messages": len(messages),
                },
            )
            self.logger.warning(f"[{self.name}] durable tool result unverified; keeping request retryable")
            return

        if created:
            try:
                note = await self._list_session_note(day, session_id)
            except RuntimeError as exc:
                self.context.response.success = False
                self.context.response.answer = str(exc)
                self.context.response.metadata.update(
                    {"date": day, "path": None, "created": created, "modified": False, "n_messages": len(messages)},
                )
                self.logger.info(f"[{self.name}] post-create list failed session_id={session_id!r} answer={str(exc)!r}")
                return
            if note is None:
                if (is_agentscope and retention_requested) or self._requested_tool_call_observed(
                    result,
                    durable_tools,
                    require_execution=is_agentscope,
                ):
                    self.context.response.success = False
                    self.context.response.answer = (
                        "Error: durable memory write did not create a note for the requested session; "
                        "request remains eligible for retry"
                    )
                    self.context.response.metadata.update(
                        {"date": day, "path": None, "created": False, "modified": False, "n_messages": len(messages)},
                    )
                    self.logger.warning(
                        f"[{self.name}] durable tool succeeded without a note for session_id={session_id!r}; "
                        "keeping request retryable",
                    )
                    return
                self.context.response.success = True
                self.context.response.answer = agent_reply_result_text(result)
                self.context.response.metadata.update(
                    {"date": day, "path": None, "created": False, "modified": False, "n_messages": len(messages)},
                )
                self.logger.info(f"[{self.name}] done without note session_id={session_id!r} modified=False")
                return
            note_path = str(note["path"])
            if image_sources:
                await self._ensure_session_frontmatter(note_path, session_id, image_sources)
        else:
            try:
                await self._ensure_session_frontmatter(note_path, session_id, image_sources)
                note_path = await self._rename_from_frontmatter_name(note_path, day)
            except RuntimeError as exc:
                self.context.response.success = False
                self.context.response.answer = str(exc)
                self.context.response.metadata.update(
                    {
                        "date": day,
                        "path": note_path,
                        "created": created,
                        "modified": self._note_modified(before_note_path, before_note_bytes, note_path),
                        "n_messages": len(messages),
                    },
                )
                self.logger.info(f"[{self.name}] post-update failed path={note_path} answer={str(exc)!r}")
                return

        if protected and not verify_note_provenance(
            self.file_store.workspace_path,
            self.file_store.workspace_path / note_path,
            self._session_dir(),
            session_id,
        ):
            self.context.response.success = False
            self.context.response.answer = (
                "Error: durable memory note provenance verification failed; request remains eligible for retry"
            )
            self.context.response.metadata.update(
                {
                    "date": day,
                    "path": note_path,
                    "created": created,
                    "modified": self._note_modified(before_note_path, before_note_bytes, note_path),
                    "n_messages": len(messages),
                },
            )
            return

        modified = self._note_modified(before_note_path, before_note_bytes, note_path)
        if modified:
            self.context["changes"] = [{"change": "added" if created else "modified", "path": note_path}]
        daily_dir = self.config_value("daily_dir")
        self.logger.info(f"[{self.name}] refresh index start date={day} daily_dir={daily_dir}")
        index_payload = await refresh_day_index(self.file_store, day, daily_dir)
        self.logger.info(f"[{self.name}] refresh index done path={note_path}")

        source_conversation = self._session_link(session_id)
        self.context.response.success = True
        if retention_requested and not modified:
            # An attributed existing note may already contain the requested fact. Do not turn
            # an unchanged disk state into a false claim that this request stored it.
            self.context.response.answer = "No note change: existing session note retained; no new storage claimed"
        else:
            self.context.response.answer = agent_reply_result_text(result)
        self.context.response.metadata.update(
            {
                "date": day,
                "path": note_path,
                "created": created,
                "modified": modified,
                "n_messages": len(messages),
                "source_conversation": source_conversation,
                **({"source_hash": provenance["source_hash"]} if protected else {}),
                "index": index_payload,
            },
        )
        self.logger.info(f"[{self.name}] done {note_path} modified={modified}")
