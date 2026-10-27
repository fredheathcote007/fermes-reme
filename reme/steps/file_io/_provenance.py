"""Trusted provenance for daily notes and their session transcripts."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

import frontmatter

from ._daily_index import validate_session_id
from ._path import is_relative_to, resolve_path

SOURCE_HASH_ALGORITHM = "sha256"
SOURCE_HASH_FORMAT = "hex"
SOURCE_HASH_KEY = "source_hash"
PROVENANCE_SESSION_KEY = "_provenance_session_id"
_PROVENANCE_KEYS = frozenset({"session_id", "source_conversation", SOURCE_HASH_KEY})


class ProvenanceError(ValueError):
    """Raised when a trusted session source cannot be used safely."""


def session_source_path(session_dir: str, session_id: str) -> str:
    """Return the canonical workspace-relative transcript path."""
    return Path(session_dir, "dialog", f"{session_id}.jsonl").as_posix()


def session_source_link(session_dir: str, session_id: str) -> str:
    """Return the canonical wikilink for a session transcript."""
    return f"[[{session_source_path(session_dir, session_id)}]]"


def _has_symlink_component(workspace: Path, path: Path) -> bool:
    """Return whether a lexical path component below ``workspace`` is a symlink."""
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


def resolve_session_source(
    workspace: Path,
    session_dir: str,
    session_id: str,
    *,
    require_exists: bool = True,
) -> Path:
    """Resolve a transcript only when its complete path is workspace-owned."""
    if error := validate_session_id(str(session_id)):
        raise ProvenanceError(error)
    workspace = workspace.resolve()
    relative = session_source_path(session_dir, str(session_id))
    lexical = workspace / relative
    if _has_symlink_component(workspace, lexical):
        raise ProvenanceError("session transcript path cannot contain symlinks")
    target, error = resolve_path(workspace, relative)
    if error or target is None or not is_relative_to(target, workspace):
        raise ProvenanceError(error or "session transcript path must stay inside the workspace")
    if _has_symlink_component(workspace, target):
        raise ProvenanceError("session transcript does not exist")
    if require_exists and not target.is_file():
        raise ProvenanceError("session transcript does not exist")
    return target


def _read_exact_bytes(path: Path) -> bytes:
    """Read exact source bytes without following a replaced final symlink."""
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise ProvenanceError("session transcript is not a readable regular file") from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ProvenanceError("session transcript is not a readable regular file")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    except ProvenanceError:
        raise
    except OSError as exc:
        raise ProvenanceError("session transcript is not a readable regular file") from exc
    finally:
        os.close(fd)


def source_hash_metadata(source_path: Path) -> dict[str, str]:
    """Hash exact transcript bytes in the explicit, serialized hash format."""
    digest = hashlib.sha256(_read_exact_bytes(source_path)).hexdigest()
    return {"algorithm": SOURCE_HASH_ALGORITHM, "format": SOURCE_HASH_FORMAT, "value": digest}


def trusted_provenance(workspace: Path, session_dir: str, session_id: str) -> dict:
    """Build source metadata from the server-owned session, never caller metadata."""
    source_path = resolve_session_source(workspace, session_dir, session_id)
    return {
        "session_id": str(session_id),
        "source_conversation": session_source_link(session_dir, str(session_id)),
        SOURCE_HASH_KEY: source_hash_metadata(source_path),
    }


def verify_note_provenance(workspace: Path, note_path: Path, session_dir: str, session_id: str) -> bool:
    """Verify note ownership, transcript link, and exact source hash."""
    workspace = workspace.resolve()
    note_target, error = resolve_path(workspace, str(note_path))
    if error or note_target is None or _has_symlink_component(workspace, note_path):
        return False
    try:
        post = frontmatter.loads(note_target.read_text(encoding="utf-8"))
        metadata = dict(post.metadata or {})
        expected = trusted_provenance(workspace, session_dir, session_id)
    except (OSError, UnicodeError, ValueError, TypeError, ProvenanceError):
        return False
    return all(metadata.get(key) == value for key, value in expected.items())


def strip_model_provenance(metadata: object) -> dict:
    """Drop model-provided ownership fields before adding trusted values."""
    if not isinstance(metadata, dict):
        return {}
    return {str(key): value for key, value in metadata.items() if key not in _PROVENANCE_KEYS and value is not None}


def note_owned_by_session(metadata: object, trusted: dict) -> bool:
    """Accept an existing note only when its declared source identifies this session.

    A legacy note may contain one owner field, but neither field may conflict
    with the trusted session. An unowned note is never claimed implicitly.
    """
    if not isinstance(metadata, dict):
        return False
    keys = ("session_id", "source_conversation")
    present = [key for key in keys if key in metadata]
    return bool(present) and all(metadata.get(key) == trusted[key] for key in present)
