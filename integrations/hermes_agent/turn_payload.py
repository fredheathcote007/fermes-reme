"""Build durable Hermes turn payloads."""

from __future__ import annotations

import hashlib
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
