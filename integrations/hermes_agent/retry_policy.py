"""Retry classification for durable Hermes provider writes."""

from __future__ import annotations


def is_permanent_write_failure(error: BaseException) -> bool:
    """Recognize application refusals that cannot improve by replaying unchanged input."""
    detail = str(error).strip().lower()
    return any(
        marker in detail
        for marker in (
            "request_id was already used for different input",
            "protected note marker",
            "protected note belongs to",
            "source provenance",
            "superseded",
            "foreign",
            "frontmatter key collision",
            "protected receipt is malformed",
            "protected receipt is not a ",
            "protected receipt fingerprint does not match",
            "protected receipt has no note path",
            "protected receipt has an unknown outcome",
            "protected receipt does not match",
            "protected receipt response is not bound",
            "protected receipt request does not match",
            "protected prior commit has no durable receipt",
            "protected note body digest",
        )
    )
