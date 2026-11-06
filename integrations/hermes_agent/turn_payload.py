"""Build durable Hermes turn payloads."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any

_NON_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


def _slug(value: str, fallback: str, *, limit: int) -> str:
    value = _NON_FILENAME_CHARS.sub("-", str(value or "").strip()).strip("-._")
    return (value or fallback)[:limit]


def scoped_session_id(profile_id: str, session_id: str) -> str:
    """Create a readable, filename-safe ID without allowing scope collisions."""
    profile = str(profile_id or "default")
    session = str(session_id or "session")
    digest = hashlib.sha256(f"{profile}\0{session}".encode()).hexdigest()[:12]
    return f"hermes-{_slug(profile, 'default', limit=32)}-{_slug(session, 'session', limit=64)}-{digest}"


def build_turn_payload(profile_id: str, session_id: str, user: str, assistant: str) -> dict[str, Any]:
    """Build the two-message payload for one completed Hermes turn."""
    turn_id = uuid.uuid4().hex
    created_at = datetime.now(timezone.utc).isoformat()
    return {
        "session_id": scoped_session_id(profile_id, session_id),
        "messages": [
            {
                "id": f"{turn_id}:user",
                "name": "user",
                "role": "user",
                "content": user,
                "created_at": created_at,
            },
            {
                "id": f"{turn_id}:assistant",
                "name": "assistant",
                "role": "assistant",
                "content": assistant,
                "created_at": created_at,
            },
        ],
    }


_VALID_MESSAGE_ROLES = frozenset({"user", "assistant", "system"})
_MAX_SESSION_ID_BYTES = 249
_MAX_RECORD_ID_LENGTH = 128


def _is_utf8_text(value: str) -> bool:
    try:
        value.encode("utf-8")
    except UnicodeError:
        return False
    return True


def _is_json_safe(value: object) -> bool:
    try:
        json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, RecursionError, UnicodeError):
        return False
    return True


def _valid_session_id(session_id: object) -> bool:
    if not isinstance(session_id, str) or not session_id or session_id != session_id.strip():
        return False
    try:
        valid_utf8_length = len(session_id.encode("utf-8")) <= _MAX_SESSION_ID_BYTES
    except UnicodeError:
        return False
    return (
        valid_utf8_length
        and session_id not in {".", ".."}
        and not session_id.endswith(".")
        and not any(char in '<>:"/\\|?*' or ord(char) < 32 for char in session_id)
        and session_id.split(".", 1)[0].upper()
        not in {
            "CON",
            "PRN",
            "AUX",
            "NUL",
            *(f"COM{index}" for index in range(1, 10)),
            *(f"LPT{index}" for index in range(1, 10)),
        }
    )


def _valid_message(message: object) -> bool:
    required = ("id", "name", "role", "content")
    return (
        isinstance(message, dict)
        and message.get("role") in _VALID_MESSAGE_ROLES
        and all(isinstance(message.get(key), str) for key in required)
        and ("created_at" not in message or isinstance(message["created_at"], str))
        and ("metadata" not in message or isinstance(message["metadata"], dict))
        and all(_is_utf8_text(message[key]) for key in required)
        and ("created_at" not in message or _is_utf8_text(message["created_at"]))
    )


def valid_pending_payload(payload: object) -> bool:
    """Validate the provider's durable turn shape, including legacy timestamps."""
    if not isinstance(payload, dict):
        return False
    if not _valid_session_id(payload.get("session_id")):
        return False
    messages = payload.get("messages")
    if not isinstance(messages, list) or not all(_valid_message(message) for message in messages):
        return False
    return _is_json_safe(payload)


def valid_pending_record(record: object) -> bool:
    """Validate the durable record envelope before it enters the FIFO."""
    if not isinstance(record, dict):
        return False
    record_id = record.get("id")
    if not isinstance(record_id, str) or not record_id or len(record_id) > _MAX_RECORD_ID_LENGTH:
        return False
    if not _is_utf8_text(record_id):
        return False
    return valid_pending_payload(record.get("payload"))


def upgrade_legacy_payload(record: dict[str, Any], historical_created_at: str) -> dict[str, Any]:
    """Add missing message timestamps without changing existing durable values."""
    payload = record["payload"]
    messages = []
    changed = False
    for message in payload["messages"]:
        if "created_at" in message:
            messages.append(message)
            continue
        created_at = next(
            (
                message.get(key)
                for key in ("time_created", "timestamp", "createdAt", "timeCreated", "created_time")
                if isinstance(message.get(key), str) and message.get(key)
            ),
            payload.get("created_at") if isinstance(payload.get("created_at"), str) else historical_created_at,
        )
        messages.append({**message, "created_at": created_at})
        changed = True
    return {**record, "payload": {**payload, "messages": messages}} if changed else record
