"""End-to-end Option B daily-index degradation and truthful receipt status.

These are deterministic synthetic-workspace tests; no remote model is called.
"""

# pylint: disable=protected-access

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from reme.application import Application
from reme.components.agent_wrapper.as_agent_wrapper import AsAgentWrapper
from reme.config import resolve_app_config
from reme.steps.file_io._daily_index import refresh_day_index


@pytest.mark.asyncio
async def test_protected_receipt_exposes_index_degradation_and_recovers_without_model_replay(tmp_path, monkeypatch):
    """A durable note remains valid while derived-index health changes across replay."""
    workspace = tmp_path / "workspace"
    index = workspace / "daily/2026-10-21.md"
    index.parent.mkdir(parents=True)
    raw_index = b"---\ntags: [\n---\nprivate synthetic index body\n"
    index.write_bytes(raw_index)
    config_path = Path(__file__).resolve().parents[2] / "reme/config/fermes-hermes-stage1.yaml"
    config = resolve_app_config(config=str(config_path), workspace_dir=str(workspace), log_config=False)
    calls = []

    async def reply(_self, _inputs, **_kwargs):
        calls.append(1)
        return {
            "result": json.dumps(
                {
                    "path": "daily/2026-10-21/index-status-fact.md",
                    "name": "index-status-fact",
                    "description": "Synthetic model-generated fact.",
                    "content": "A protected note remains durable despite a damaged derived index.",
                },
            ),
            "tool_execution": [],
        }

    monkeypatch.setattr(AsAgentWrapper, "reply", reply)
    request = {
        "request_id": "fhm007-index-status",
        "session_id": "fhm007-index-status-session",
        "messages": [{"id": "m1", "name": "user", "role": "user", "content": "Retain a synthetic fact"}],
        "date": "2026-10-21",
    }
    async with Application(**config) as app:
        first = await app.run_job("auto_memory", **request)
        assert first.success
        assert first.metadata["index_status"] == "degraded"
        assert first.metadata["index"]["error_kind"] == "content"
        assert "private synthetic" not in first.metadata["index"]["error"]
        note = workspace / "daily/2026-10-21/index-status-fact.md"
        assert note.is_file()
        assert index.read_bytes() == raw_index
        receipt_paths = sorted((workspace / "metadata/auto_memory_requests").rglob("*.json"))
        assert len(receipt_paths) == 1
        receipt_bytes = receipt_paths[0].read_bytes()

        second = await app.run_job("auto_memory", **request)
        assert second.success
        assert second.metadata["index_status"] == "degraded"
        assert calls == [1]
        assert receipt_paths[0].read_bytes() == receipt_bytes

        index.write_text("---\nname: 2026-10-21\n---\nrepaired\n", encoding="utf-8")
        third = await app.run_job("auto_memory", **request)
        assert third.success
        # Receipt replay is an immutable commit-time snapshot, even after
        # external repair of the derived page.
        assert third == first
        assert third.metadata["index_status"] == "degraded"
        assert calls == [1]
        assert receipt_paths[0].read_bytes() == receipt_bytes
        health = await refresh_day_index(
            SimpleNamespace(workspace_path=workspace),
            "2026-10-21",
            "daily",
        )
        assert "error" not in health
        assert note.read_text(encoding="utf-8").count("A protected note remains durable") == 1


@pytest.mark.asyncio
async def test_unreadable_index_reports_only_sanitised_error_class(tmp_path):
    """Parse errors must not leak damaged page contents into an API response."""
    idx = tmp_path / "daily/2026-05-18.md"
    idx.parent.mkdir(parents=True)
    idx.write_bytes(b"---\nsecret_field: [UNSAFE-PAGE-CONTENT\n---\nprivate body\n")
    result = await refresh_day_index(SimpleNamespace(workspace_path=tmp_path), "2026-05-18", "daily")
    assert result["error_kind"] == "content"
    assert result["error"].startswith("daily index is unreadable: ")
    assert "UNSAFE-PAGE-CONTENT" not in result["error"]
    assert "private body" not in result["error"]
    assert idx.read_bytes() == b"---\nsecret_field: [UNSAFE-PAGE-CONTENT\n---\nprivate body\n"


@pytest.mark.asyncio
async def test_protected_daily_index_io_failure_retries_without_duplicate_model(tmp_path, monkeypatch):
    """A read error is not degraded-success; the durable note remains recoverable."""
    workspace = tmp_path / "workspace"
    index = workspace / "daily/2026-10-21.md"
    index.parent.mkdir(parents=True)
    index.write_text("---\nname: 2026-10-21\n---\noriginal\n", encoding="utf-8")
    config_path = Path(__file__).resolve().parents[2] / "reme/config/fermes-hermes-stage1.yaml"
    config = resolve_app_config(config=str(config_path), workspace_dir=str(workspace), log_config=False)
    calls = []

    async def reply(_self, _inputs, **_kwargs):
        calls.append(1)
        return {
            "result": json.dumps(
                {
                    "path": "daily/2026-10-21/io-error-fact.md",
                    "name": "io-error-fact",
                    "description": "Synthetic read error",
                    "content": "The durable note must be recovered without a second model call.",
                },
            ),
            "tool_execution": [],
        }

    monkeypatch.setattr(AsAgentWrapper, "reply", reply)
    original_read = Path.read_text

    def fail_index_read(path, *args, **kwargs):
        if path == index:
            raise PermissionError("synthetic-host-private-path")
        return original_read(path, *args, **kwargs)

    request = {
        "request_id": "fhm007-index-oserror",
        "session_id": "fhm007-index-oserror-session",
        "messages": [{"id": "m1", "name": "user", "role": "user", "content": "Retain synthetic proof"}],
        "date": "2026-10-21",
    }
    async with Application(**config) as app:
        with monkeypatch.context() as failure:
            failure.setattr(Path, "read_text", fail_index_read)
            first = await app.run_job("auto_memory", **request)
        note = workspace / "daily/2026-10-21/io-error-fact.md"
        assert not first.success
        assert note.is_file()
        assert "synthetic-host-private-path" not in first.answer
        assert not list((workspace / "metadata/auto_memory_requests").rglob("*.json"))
        second = await app.run_job("auto_memory", **request)
        assert second.success
        assert second.metadata["index_status"] == "ok"
        assert calls == [1]
        assert note.read_text(encoding="utf-8").count("The durable note must be recovered") == 1


@pytest.mark.asyncio
async def test_receipt_replay_keeps_stored_response_without_rebuilding_index(tmp_path, monkeypatch):
    """Receipt replay stays authoritative when the derived daily index is unreadable."""
    workspace = tmp_path / "workspace"
    config_path = Path(__file__).resolve().parents[2] / "reme/config/fermes-hermes-stage1.yaml"
    config = resolve_app_config(config=str(config_path), workspace_dir=str(workspace), log_config=False)
    calls = []

    async def reply(_self, _inputs, **_kwargs):
        calls.append(1)
        return {
            "result": json.dumps(
                {
                    "path": "daily/2026-10-21/replay-health-fact.md",
                    "name": "replay-health-fact",
                    "description": "Synthetic healthy first commit",
                    "content": "Previously committed fact does not require model replay.",
                },
            ),
            "tool_execution": [],
        }

    monkeypatch.setattr(AsAgentWrapper, "reply", reply)
    request = {
        "request_id": "fhm007-index-replay-oserror",
        "session_id": "fhm007-index-replay-oserror-session",
        "messages": [{"id": "m1", "name": "user", "role": "user", "content": "Retain a fact"}],
        "date": "2026-10-21",
    }
    index = workspace / "daily/2026-10-21.md"
    original_read = Path.read_text

    def fail_replay_index_read(path, *args, **kwargs):
        if path == index:
            raise PermissionError("private-replay-host-path")
        return original_read(path, *args, **kwargs)

    async with Application(**config) as app:
        first = await app.run_job("auto_memory", **request)
        assert first.success
        assert first.metadata["index_status"] == "ok"
        receipts = list((workspace / "metadata/auto_memory_requests").rglob("*.json"))
        assert len(receipts) == 1
        receipt_bytes = receipts[0].read_bytes()
        note = workspace / "daily/2026-10-21/replay-health-fact.md"
        note_bytes = note.read_bytes()
        assert index.exists()

        with monkeypatch.context() as failure:
            failure.setattr(Path, "read_text", fail_replay_index_read)
            second = await app.run_job("auto_memory", **request)

        assert second == first
        assert receipts[0].read_bytes() == receipt_bytes
        assert note.read_bytes() == note_bytes
        assert calls == [1]
        third = await app.run_job("auto_memory", **request)
        assert third == first
        assert calls == [1]
        assert receipts[0].read_bytes() == receipt_bytes


def test_invalid_protected_index_path_is_not_a_degraded_success():
    """Invalid path/config policy is a hard failure rather than silently ACKed."""
    from reme.steps.evolve.auto_memory import AutoMemoryStep

    with pytest.raises(RuntimeError, match="protected daily index refresh failed"):
        AutoMemoryStep._option_b_index_status(
            {"error_kind": "invalid_path", "error": "invalid daily index path"},
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("cross_day", [False, True], ids=["same_day", "cross_day"])
async def test_option_b_orphaned_commit_recovers_degraded_index_without_model_replay(
    tmp_path,
    monkeypatch,
    cross_day,
):
    """A lost receipt and newly corrupt derived page cannot duplicate the note."""
    from datetime import datetime

    from reme.steps.evolve import auto_memory as auto_memory_module
    from reme.steps.evolve.auto_memory import AutoMemoryStep

    workspace = tmp_path / "workspace"
    config_path = Path(__file__).resolve().parents[2] / "reme/config/fermes-hermes-stage1.yaml"
    config = resolve_app_config(config=str(config_path), workspace_dir=str(workspace), log_config=False)
    times = iter(
        [
            datetime(2026, 10, 21),
            datetime(2026, 10, 22 if cross_day else 21),
        ],
    )
    model_calls = []
    receipt_calls = []
    write_receipt = AutoMemoryStep._write_option_b_receipt

    async def proposal(_self, _inputs, **_kwargs):
        model_calls.append(1)
        return {
            "result": json.dumps(
                {
                    "path": "daily/2026-10-21/recovered-degraded.md",
                    "name": "recovered-degraded",
                    "description": "Synthetic orphaned receipt recovery",
                    "content": "Durable recovery survives a damaged daily page.",
                },
            ),
            "tool_execution": [],
        }

    async def lose_first_receipt(step, path, fingerprint):
        receipt_calls.append(1)
        if len(receipt_calls) == 1:
            raise OSError("synthetic interrupted receipt write")
        await write_receipt(step, path, fingerprint)

    monkeypatch.setattr(AsAgentWrapper, "reply", proposal)
    monkeypatch.setattr(auto_memory_module, "now", lambda _tz: next(times))
    monkeypatch.setattr(AutoMemoryStep, "_messages_day", staticmethod(lambda _messages, _timezone=None: None))
    monkeypatch.setattr(AutoMemoryStep, "_write_option_b_receipt", lose_first_receipt)
    if not cross_day:
        monkeypatch.setattr(AutoMemoryStep, "_find_cross_day_option_b_marker", lambda *_args, **_kwargs: None)

    request = {
        "request_id": f"degraded-recovery-{cross_day}",
        "session_id": "degraded-recovery-session",
        "messages": [{"id": "m1", "name": "user", "role": "user", "content": "Retention proof"}],
    }
    note = workspace / "daily/2026-10-21/recovered-degraded.md"
    index = workspace / "daily/2026-10-21.md"
    damaged = b"---\nprivate: [BROKEN-RECOVERY-PAGE\n---\nunchanged\n"

    async with Application(**config) as app:
        first = await app.run_job("auto_memory", **request)
        assert first.success is False
        original_note = note.read_bytes()
        assert len(model_calls) == 1
        assert not list((workspace / "metadata/auto_memory_requests").rglob("*.json"))

        index.write_bytes(damaged)
        second = await app.run_job("auto_memory", **request)
        assert second.success
        assert second.metadata["index_status"] == "degraded"
        assert second.metadata["index"]["error_kind"] == "content"
        assert "BROKEN-RECOVERY-PAGE" not in second.metadata["index"]["error"]
        assert second.metadata["path"] == "daily/2026-10-21/recovered-degraded.md"
        assert note.read_bytes() == original_note
        assert index.read_bytes() == damaged
        assert model_calls == [1]
        assert receipt_calls == [1, 1]
        receipts = list((workspace / "metadata/auto_memory_requests").rglob("*.json"))
        assert len(receipts) == 1
        original_receipt = receipts[0].read_bytes()

        # Repeated request must use the frozen receipt, never a second model
        # operation, even if the derived page later gets repaired.
        index.write_text("---\nname: 2026-10-21\n---\nrepaired\n", encoding="utf-8")
        third = await app.run_job("auto_memory", **request)
        assert third == second
        assert receipts[0].read_bytes() == original_receipt
        assert model_calls == [1]
