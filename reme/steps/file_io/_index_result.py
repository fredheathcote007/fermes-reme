"""Sanitised derived-index results for ordinary ReMe job responses.

Protected FHM-007 Option B manages its own commit and acknowledgement contract;
do not use this adapter for protected commits or immutable receipt replay.
"""

from collections.abc import Awaitable, Callable
from typing import Any
from pathlib import PurePosixPath, PureWindowsPath

from ...utils import get_logger

logger = get_logger(log_to_file=False)


async def refresh_index_result(
    refresh: Callable[..., Awaitable[dict]],
    file_store: Any,
    day: str,
    daily_dir: str,
) -> dict:
    """Refresh a derived index, returning an error without exposing file data.

    The original note operation is not rolled back: callers must report an
    unsuccessful index refresh while preserving accurate modified/path metadata.
    """
    try:
        result = await refresh(file_store, day, daily_dir)
    except OSError as exc:
        # The affected note may already be durable. Do not invent a zero-note
        # inventory or imply a partial derived write did not change the index.
        logger.warning(
            f"ordinary daily index I/O failure day={day} " f"kind={type(exc).__name__}",
        )
        result = {
            "date": day,
            "error": "daily index refresh failed",
            "error_kind": "io",
        }
    if "error" not in result:
        return result
    kind = result.get("error_kind")
    raw_error = result.get("error")
    if kind == "content" or (isinstance(raw_error, str) and raw_error.startswith("daily index is unreadable")):
        kind = "content"
        safe_error = "daily index is unreadable"
    else:
        kind = kind if kind in {"invalid_path", "io"} else "unknown"
        safe_error = "daily index refresh failed"
    # An error result is a status, not an inventory. Allow only stable
    # status fields; prevent accidental disclosure via future extra keys.
    public = {key: result[key] for key in ("date", "created", "changed") if key in result}
    path = result.get("path")
    if isinstance(path, str) and not (PurePosixPath(path).is_absolute() or PureWindowsPath(path).is_absolute()):
        public["path"] = path
    return {**public, "error": safe_error, "error_kind": kind}
