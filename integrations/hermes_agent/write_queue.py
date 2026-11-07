"""Helpers for durable FIFO writer handoff and retry wakeups."""

from __future__ import annotations

import queue
import threading
import time
from typing import Any


def _private(provider: Any, name: str) -> Any:
    """Read the provider's deliberately private queue state."""
    return getattr(provider, name)


def rebuild_write_queue_locked(provider: Any, records: list[dict[str, Any]]) -> None:
    """Rebuild a stopped provider queue in durable journal order."""
    write_queue = _private(provider, "_write_queue")
    queued_without_journal: list[dict[str, Any]] = []
    while True:
        try:
            queued_record = write_queue.get_nowait()
        except queue.Empty:
            break
        try:
            if isinstance(queued_record, dict):
                queued_without_journal.append(queued_record)
        finally:
            write_queue.task_done()

    _private(provider, "_queued_record_ids").clear()
    ordered = list(records)
    # A volatile queue entry has no durable admission proof.  It may be stale
    # after an acknowledgement, quarantine, crash, or profile switch, so it
    # must never be resurrected into the FIFO.
    durable_by_id = {record["id"]: record for record in records}
    for queued_record in queued_without_journal:
        durable_record = durable_by_id.get(queued_record.get("id"))
        if durable_record is not None and durable_record == queued_record:
            ordered.append(durable_record)
    _private(provider, "_queue_pending_records")(tuple(ordered))


class RetryWakeup:
    """Own the provider's single cancellable retry timer."""

    def __init__(self, provider: Any):
        self._provider = provider

    def arm_locked(self) -> None:
        """Arm one wakeup while the provider writer lock is held."""
        provider = self._provider
        if _private(provider, "_shutdown_started") or _private(provider, "_retry_timers"):
            return
        delay = max(0.01, _private(provider, "_next_write_attempt") - time.monotonic())
        timer_ref: list[threading.Timer | None] = [None]

        def wake() -> None:
            timer = timer_ref[0]
            with _private(provider, "_write_thread_lock"):
                if timer is not None:
                    _private(provider, "_retry_timers").discard(timer)
                if _private(provider, "_shutdown_started"):
                    return
                if time.monotonic() < _private(provider, "_next_write_attempt"):
                    self.arm_locked()
                    return
                if _private(provider, "_write_thread") is not None:
                    return
            _private(provider, "_start_writer_if_needed")()

        timer = threading.Timer(delay, wake)
        timer.daemon = True
        timer_ref[0] = timer
        _private(provider, "_retry_timers").add(timer)
        timer.start()

    def cancel_locked(self) -> None:
        """Cancel and forget all retry timers while the writer lock is held."""
        provider = self._provider
        timers = tuple(_private(provider, "_retry_timers"))
        _private(provider, "_retry_timers").clear()
        for timer in timers:
            timer.cancel()
