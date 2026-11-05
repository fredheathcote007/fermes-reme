"""Retry classification for durable Hermes provider writes."""

from __future__ import annotations

from collections.abc import Mapping

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

_MAX_NESTED_ERROR_DEPTH = 16
_MAX_NESTED_ERROR_NODES = 128
_RECEIPT_REPLAY_PREFIX = "protected receipt replay refused:"
_PERMANENT_ERROR_MARKERS = (
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
_REPLAY_ONLY_PERMANENT_ERRORS = frozenset({"invalid protected note path"})


def _contains_os_error(error: BaseException) -> bool:
    """Find a typed storage error in a bounded exception/argument graph."""
    seen: set[int] = set()
    visited = 0

    def visit(value: object, depth: int) -> bool:
        nonlocal visited
        if isinstance(value, OSError):
            return True
        if depth > _MAX_NESTED_ERROR_DEPTH or visited >= _MAX_NESTED_ERROR_NODES:
            # A bounded walk cannot prove that a truncated graph is free of a
            # storage fault. Keep it retryable rather than quarantining it.
            return isinstance(value, (BaseException, Mapping, tuple, list, set, frozenset))
        found = False
        if isinstance(value, BaseException):
            identity = id(value)
            if identity not in seen:
                seen.add(identity)
                visited += 1
                found = visit(value.__cause__, depth + 1) or visit(value.__context__, depth + 1)
                if not found:
                    found = any(visit(argument, depth + 1) for argument in value.args)
        elif isinstance(value, Mapping):
            identity = id(value)
            if identity not in seen:
                seen.add(identity)
                visited += 1
                found = any(visit(nested, depth + 1) for item in value.items() for nested in item)
        elif isinstance(value, (tuple, list, set, frozenset)):
            identity = id(value)
            if identity not in seen:
                seen.add(identity)
                visited += 1
                found = any(visit(nested, depth + 1) for nested in value)
        return found

    return visit(error, 0)


def is_permanent_write_failure(error: BaseException) -> bool:
    """Recognize application refusals that cannot improve by replaying unchanged input."""
    if _contains_os_error(error):
        return False
    try:
        detail = str(error).strip().lower()
    except RecursionError:
        return False
    if detail.startswith("error:"):
        detail = detail[6:].strip()
    is_receipt_replay = detail.startswith(_RECEIPT_REPLAY_PREFIX)
    if is_receipt_replay:
        detail = detail[len(_RECEIPT_REPLAY_PREFIX) :].strip()
    if detail in _PERMANENT_RECEIPT_ERRORS:
        return True
    if is_receipt_replay:
        return detail in _REPLAY_ONLY_PERMANENT_ERRORS or any(
            detail.startswith(marker) for marker in _PERMANENT_ERROR_MARKERS
        )
    return any(marker in detail for marker in _PERMANENT_ERROR_MARKERS)
