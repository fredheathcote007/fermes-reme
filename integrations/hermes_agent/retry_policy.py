"""Retry classification for durable Hermes provider writes."""

from __future__ import annotations

_PERMANENT_RECEIPT_ERRORS = frozenset(
    {
        "protected receipt is malformed",
        "protected receipt is unreadable",
        "protected receipt is not a commit",
        "protected receipt fingerprint does not match its marker",
        "protected receipt does not contain a matching successful commit",
        "protected receipt does not contain a successful commit",
        "protected receipt has no note path",
        "protected receipt has an unknown outcome",
        "protected receipt does not match the committed note bytes",
        "protected receipt response is malformed",
        "protected receipt response is not bound to its commit",
        "protected receipt request does not match its marker",
        "protected no-op receipt contains commit evidence",
        "protected no-op receipt response is malformed",
        "protected prior commit receipt is malformed",
    },
)


def is_permanent_write_failure(error: BaseException) -> bool:
    """Recognize application refusals that cannot improve by replaying unchanged input."""
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        if isinstance(current, OSError):
            return False
        seen.add(id(current))
        current = current.__cause__ or (None if current.__suppress_context__ else current.__context__)
    detail = str(error).strip().lower()
    if detail.startswith("error:"):
        detail = detail[6:].strip()
    if detail in _PERMANENT_RECEIPT_ERRORS:
        return True
    return any(
        marker in detail
        for marker in (
            "request_id was already used for different input",
            "protected note marker",
            "protected note belongs to",
            "existing protected note belongs to a different source session",
            "protected marker source provenance is not an append-only extension",
            "protected receipt source provenance is not an append-only extension",
            "frontmatter key collision",
            "protected receipt is malformed",
            "protected receipt is not a ",
            "protected receipt fingerprint does not match",
            "protected receipt has no note path",
            "protected receipt has an unknown outcome",
            "protected receipt does not match",
            "protected receipt does not contain a successful commit",
            "protected receipt response is malformed",
            "protected receipt response is not bound",
            "protected receipt request does not match",
            "protected prior commit has no durable receipt",
            "protected prior commit receipt is malformed",
            "protected no-op receipt",
            "protected note body digest",
            "protected note is unreadable",
            "protected daily note is unreadable",
        )
    )
