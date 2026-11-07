"""Behavioural red matrix for FHM-007 Stage 2A.

These tests intentionally target the real Hermes provider, transport adapters,
ReMe ``Application``/``BaseJob`` path, and the durable JSONL files.  The source
tree is the untouched pre-implementation baseline, so tests for the approved
new contract are expected to fail until the separate implementation gate.
"""

from __future__ import annotations

# Deliberately inspect private state and exact empty collections in adversarial tests.
# pylint: disable=C0116,W0212,C1803

import json
import hashlib
import importlib
import os
import subprocess
import time
import urllib.error
from pathlib import Path
from threading import Lock, Thread
from threading import Event

import pytest

from .test_hermes_agent_integration import (
    CLIENT_MODULE,
    EMBEDDED_MODULE,
    HTTP_BACKEND_MODULE,
    PLUGIN_MODULE,
    ReMeBackendError,
    ReMeConfig,
    ReMeMemoryProvider,
    config_path,
)
from .hermes_agent_test_support import FakeBackend

REFUSAL_CODE = "PROTECTED_OPTION_B_PROPOSAL_INVALID"
REFUSAL_REASONS = (
    "tool_result_invalid",
    "not_json_revision",
    "invalid_json",
    "schema_mismatch",
    "non_string_field",
    "invalid_name_description",
    "proposal_too_large",
    "empty_content",
    "changed_update_path",
    "unexpected_daily_path",
)


def _wait_for(predicate, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return bool(predicate())


def _wait_until(deadline: float, predicate) -> bool:
    """Poll a condition without extending its caller-owned monotonic budget."""
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(min(0.005, max(0.0, deadline - time.monotonic())))
    return bool(predicate())


def _a34_diagnostics(provider, threads, seen, started_at: float) -> str:
    path = provider._pending_path
    try:
        pending_bytes = path.stat().st_size if path is not None and path.exists() else 0
        pending_records = (
            sum(bool(line.strip()) for line in path.read_bytes().splitlines())
            if path is not None and path.exists()
            else 0
        )
    except OSError:
        pending_bytes, pending_records = -1, -1
    writer = provider._write_thread
    return (
        f"elapsed={time.monotonic() - started_at:.3f}s, "
        f"alive_producers={sum(thread.is_alive() for thread in threads)}, "
        f"backend_count={len(seen)}, backend_unique={len(set(seen))}, "
        f"pending_bytes={pending_bytes}, pending_records={pending_records}, "
        f"journal_state={provider._journal_state}, "
        f"writer_alive={bool(writer and writer.is_alive())}, "
        f"retry_timers={len(provider._retry_timers)}"
    )


def _typed_refusal(reason: str = "invalid_json") -> ReMeBackendError:
    """Return a backend failure carrying the server-owned boundary contract.

    The exception is injected at the backend boundary only to make the writer
    deterministic.  A03 proves that the metadata originates in real Option B
    validation rather than in this helper.
    """
    error = ReMeBackendError("model supplied wording must not be trusted")
    error.refusal_code = REFUSAL_CODE
    error.refusal_reason = reason
    return error


def _payload(content: str = "turn") -> dict:
    return {
        "session_id": "hermes-profile-session-1234567890",
        "messages": [
            {
                "id": "message-1",
                "name": "user",
                "role": "user",
                "content": content,
                "created_at": "2026-11-06T00:00:00+00:00",
            },
        ],
    }


def _record(record_id: str, *, profile: str = "profile", payload: dict | None = None, **extra) -> dict:
    record = {
        "id": record_id,
        "profile_id": profile,
        "attempts": 0,
        "payload": payload or _payload(),
    }
    record.update(extra)
    return record


def _write_jsonl(path: Path, *records: dict, final_newline: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = b"\n".join(json.dumps(record, sort_keys=True).encode("utf-8") for record in records)
    path.write_bytes(raw + (b"\n" if final_newline else b""))


def _pending_path(home: Path) -> Path:
    return config_path(home).parent / "pending-writes.jsonl"


def _quarantine_path(home: Path) -> Path:
    return config_path(home).parent / "quarantined-writes.jsonl"


def _provider_with_backend(monkeypatch, home: Path, backend: FakeBackend, *, retry: float = 0.01) -> ReMeMemoryProvider:
    config = ReMeConfig(
        health_retry_seconds=retry,
        shutdown_timeout=0.5,
        request_timeout=1,
        recall_timeout=1,
        health_timeout=1,
    )
    monkeypatch.setattr(PLUGIN_MODULE, "load_config", lambda home=None: config)
    monkeypatch.setattr(PLUGIN_MODULE, "_backend_for", lambda _config: backend)
    provider = ReMeMemoryProvider()
    provider.initialize("session", hermes_home=home, agent_identity="profile")
    return provider


def _protected_provider_with_backend(monkeypatch, home: Path, backend: FakeBackend) -> ReMeMemoryProvider:
    config = ReMeConfig(
        mode="embedded",
        workspace_dir=str(home / "workspace"),
        reme_config="fermes-hermes-stage1",
        request_timeout=1,
        recall_timeout=1,
        health_timeout=1,
        health_retry_seconds=60,
        shutdown_timeout=0.5,
    )
    monkeypatch.setattr(PLUGIN_MODULE, "load_config", lambda home=None: config)
    monkeypatch.setattr(PLUGIN_MODULE, "_backend_for", lambda _config: backend)
    provider = ReMeMemoryProvider()
    provider.initialize("session", hermes_home=home, agent_identity="profile")
    return provider


def _failed_response(*, metadata=None, answer="untrusted model wording") -> dict:
    return {
        "success": False,
        "answer": answer,
        "metadata": {} if metadata is None else metadata,
    }


class _HttpResponse:
    def __init__(self, body: bytes):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.body


def test_a01_actual_writer_counts_three_typed_refusals_and_releases_fifo_successor(monkeypatch, tmp_path):
    backend = FakeBackend()
    calls = []

    def auto_memory(session_id, messages, *, timeout, request_id=None):
        del session_id, messages, timeout
        calls.append(request_id)
        if len(calls) <= 3:
            raise _typed_refusal()
        return {"success": True, "metadata": {"commit": {"request_id": request_id}}}

    backend.auto_memory = auto_memory
    provider = _provider_with_backend(monkeypatch, tmp_path, backend)
    try:
        provider.sync_turn("head", "assistant")
        provider.sync_turn("successor", "assistant")
        assert _wait_for(lambda: len(calls) >= 4)
    finally:
        provider.shutdown()

    assert calls[:3] == [calls[0]] * 3
    assert calls[3] != calls[0]
    quarantined = json.loads(_quarantine_path(tmp_path).read_text(encoding="utf-8").splitlines()[0])
    assert quarantined["outcome"] == "QUARANTINED/NOT COMMITTED"
    assert quarantined["id"] == calls[0]
    assert _pending_path(tmp_path).read_text(encoding="utf-8") == ""


def test_a02_server_owned_refusal_metadata_crosses_embedded_and_http_failure_boundaries(monkeypatch):
    metadata = {"fermes_refusal_code": REFUSAL_CODE, "fermes_refusal_reason": "invalid_json"}

    with pytest.raises(ReMeBackendError) as embedded_error:
        EMBEDDED_MODULE.EmbeddedReMeBackend._response_dict(_failed_response(metadata=metadata))
    assert getattr(embedded_error.value, "refusal_code", None) == REFUSAL_CODE

    monkeypatch.setattr(
        CLIENT_MODULE.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: _HttpResponse(json.dumps(_failed_response(metadata=metadata)).encode()),
    )
    backend = HTTP_BACKEND_MODULE.HttpReMeBackend("http://127.0.0.1:2333", request_timeout=1)
    with pytest.raises(ReMeBackendError) as http_error:
        backend.auto_memory("session", [{"role": "user", "content": "turn"}], timeout=1)
    assert getattr(http_error.value, "refusal_code", None) == REFUSAL_CODE

    with pytest.raises(ReMeBackendError) as old_error:
        EMBEDDED_MODULE.EmbeddedReMeBackend._response_dict(_failed_response(metadata={"legacy": True}))
    assert getattr(old_error.value, "refusal_code", None) is None


@pytest.mark.skipif(
    os.environ.get("FHM007_REME_HOST_TESTS") != "1",
    reason="run real ReMe Application/Option B qualification on the authorised host",
)
@pytest.mark.asyncio
async def test_a03_all_real_option_b_precommit_rejections_preserve_server_owned_metadata(monkeypatch, tmp_path):
    from reme.application import Application
    from reme.config import resolve_app_config
    from reme.components.agent_wrapper.as_agent_wrapper import AsAgentWrapper
    from reme.steps.evolve.auto_memory import AutoMemoryStep

    workspace = tmp_path / "workspace"
    config_file = Path(__file__).resolve().parents[2] / "reme/config/fermes-hermes-stage1.yaml"
    config = resolve_app_config(config=str(config_file), workspace_dir=str(workspace), log_config=False)
    config["jobs"]["index_update_loop"]["enabled"] = False
    scenarios = {}
    for index, reason in enumerate(REFUSAL_REASONS):
        name = f"proposal-{index}"
        proposal = {
            "path": f"daily/2026-11-06/{name}.md",
            "name": name,
            "description": "A synthetic protected proposal",
            "content": "A substantive synthetic fact.",
        }
        if reason == "tool_result_invalid":
            scenarios[reason] = {
                "result": json.dumps(proposal),
                "tool_execution": [{"type": "tool_call", "name": "write"}],
            }
        elif reason == "not_json_revision":
            scenarios[reason] = {"result": "", "tool_execution": []}
        elif reason == "invalid_json":
            scenarios[reason] = {"result": "not-json", "tool_execution": []}
        elif reason == "schema_mismatch":
            scenarios[reason] = {"result": json.dumps({"path": proposal["path"]}), "tool_execution": []}
        elif reason == "non_string_field":
            proposal["content"] = 7
            scenarios[reason] = {"result": json.dumps(proposal), "tool_execution": []}
        elif reason == "invalid_name_description":
            proposal["name"] = "../escape"
            scenarios[reason] = {"result": json.dumps(proposal), "tool_execution": []}
        elif reason == "proposal_too_large":
            proposal["content"] = "x" * 2048
            scenarios[reason] = {"result": json.dumps(proposal), "tool_execution": []}
        elif reason == "empty_content":
            proposal["content"] = "Placeholder for today's daily note."
            scenarios[reason] = {"result": json.dumps(proposal), "tool_execution": []}
        elif reason == "changed_update_path":
            proposal["path"] = "daily/2026-11-06/a-different-note.md"
            scenarios[reason] = {"result": json.dumps(proposal), "tool_execution": []}
        else:
            proposal["path"] = "daily/2026-11-05/wrong-day.md"
            scenarios[reason] = {"result": json.dumps(proposal), "tool_execution": []}

    reply_index = 0

    async def synthetic_reply(_self, _inputs, **_kwargs):
        nonlocal reply_index
        marker = REFUSAL_REASONS[reply_index]
        reply_index += 1
        return dict(scenarios[marker])

    original_list = AutoMemoryStep._list_session_note

    async def list_note(step, day, session_id):
        if session_id in {"session-changed_update_path", "session_changed_update_path"}:
            return {"path": "daily/2026-11-06/existing.md"}
        return await original_list(step, day, session_id)

    monkeypatch.setattr(AsAgentWrapper, "reply", synthetic_reply)
    monkeypatch.setattr(AutoMemoryStep, "_list_session_note", list_note)
    monkeypatch.setattr(AutoMemoryStep, "max_file_bytes", lambda _self: 1024)

    async with Application(**config) as app:
        responses = []
        for index, reason in enumerate(REFUSAL_REASONS):
            responses.append(
                await app.run_job(
                    "auto_memory",
                    request_id=f"request-{reason}",
                    session_id=f"session-{reason}",
                    messages=[{"id": f"message-{index}", "name": "user", "role": "user", "content": "fact"}],
                    date="2026-11-06",
                ),
            )

    for reason, response in zip(REFUSAL_REASONS, responses):
        assert response.success is False
        assert response.metadata["fermes_refusal_code"] == REFUSAL_CODE
        assert response.metadata["fermes_refusal_reason"] == reason
    assert not list((workspace / "daily").rglob("*.md"))
    assert not list((workspace / "metadata" / "auto_memory_requests").rglob("*.json"))


def test_a04_model_prose_and_wrong_action_metadata_cannot_authorise_quarantine(monkeypatch):
    forged = {"fermes_refusal_code": REFUSAL_CODE, "fermes_refusal_reason": "invalid_json"}
    monkeypatch.setattr(
        CLIENT_MODULE.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: _HttpResponse(
            json.dumps(_failed_response(metadata=forged, answer=f"{REFUSAL_CODE} in model prose")).encode(),
        ),
    )
    backend = HTTP_BACKEND_MODULE.HttpReMeBackend("http://127.0.0.1:2333", request_timeout=1)
    with pytest.raises(ReMeBackendError) as error:
        backend._call("search", {}, timeout=1)
    assert getattr(error.value, "refusal_code", None) is None
    assert PLUGIN_MODULE._is_permanent_write_failure(error.value) is False


@pytest.mark.parametrize(
    "error",
    [
        OSError("[Errno 28] No space left on device"),
        ReMeBackendError(OSError("protected receipt is malformed")),
        ReMeBackendError("protected receipt storage error: protected note marker.md"),
    ],
)
def test_a05_nested_and_serialised_storage_faults_remain_retryable(error):
    assert PLUGIN_MODULE._is_permanent_write_failure(error) is False


def test_a06_actual_http_failures_are_retryable_and_do_not_receive_refusal_code(monkeypatch):
    def fail(*_args, **_kwargs):
        raise urllib.error.HTTPError("http://127.0.0.1/auto_memory", 503, "Service Unavailable", {}, None)

    monkeypatch.setattr(CLIENT_MODULE.urllib.request, "urlopen", fail)
    backend = HTTP_BACKEND_MODULE.HttpReMeBackend("http://127.0.0.1:2333", request_timeout=1)
    with pytest.raises(ReMeBackendError) as error:
        backend.auto_memory("session", [{"role": "user", "content": "turn"}], timeout=1)
    assert getattr(error.value, "refusal_code", None) is None
    assert PLUGIN_MODULE._is_permanent_write_failure(error.value) is False


def test_a07_refusal_counter_is_cumulative_across_real_transient_writer_failures(monkeypatch, tmp_path):
    backend = FakeBackend()
    calls = []

    def auto_memory(session_id, messages, *, timeout, request_id=None):
        del session_id, messages, timeout
        calls.append(request_id)
        if len(calls) in {1, 3, 5}:
            raise _typed_refusal()
        if len(calls) in {2, 4}:
            raise TimeoutError("transient model service timeout")
        return {"success": True}

    backend.auto_memory = auto_memory
    provider = _provider_with_backend(monkeypatch, tmp_path, backend)
    try:
        provider.sync_turn("alternating failures", "assistant")
        assert _wait_for(lambda: _quarantine_path(tmp_path).exists())
    finally:
        provider.shutdown()
    record = json.loads(_quarantine_path(tmp_path).read_text(encoding="utf-8").splitlines()[0])
    assert record["semantic_refusals"] == 3
    assert len(set(calls[:6])) == 1


def test_a08_more_than_three_transient_failures_recover_without_quarantine(monkeypatch, tmp_path):
    backend = FakeBackend()
    calls = []

    def auto_memory(session_id, messages, *, timeout, request_id=None):
        del session_id, messages, timeout
        calls.append(request_id)
        if len(calls) <= 4:
            return {"success": False, "answer": "temporary outage"}
        return {"success": True}

    backend.auto_memory = auto_memory
    provider = _provider_with_backend(monkeypatch, tmp_path, backend)
    try:
        provider.sync_turn("temporary", "assistant")
        assert _wait_for(lambda: len(calls) >= 5)
    finally:
        provider.shutdown()
    assert not _quarantine_path(tmp_path).exists()
    assert _pending_path(tmp_path).read_text(encoding="utf-8") == ""


def test_a09_actual_receipt_contradiction_remains_permanent_but_marker_storage_error_does_not(
    monkeypatch,
    tmp_path,
):
    backend = FakeBackend()
    calls = []

    def auto_memory(session_id, messages, *, timeout, request_id=None):
        del session_id, messages, timeout
        calls.append(request_id)
        if len(calls) == 1:
            raise ReMeBackendError("Error: protected receipt replay refused: protected receipt is malformed")
        return {"success": True}

    backend.auto_memory = auto_memory
    provider = _provider_with_backend(monkeypatch, tmp_path, backend, retry=60)
    try:
        provider.sync_turn("receipt", "assistant")
        assert _wait_for(lambda: _quarantine_path(tmp_path).exists())
    finally:
        provider.shutdown()
    assert calls == [calls[0]]
    assert (
        PLUGIN_MODULE._is_permanent_write_failure(
            ReMeBackendError("Error: protected receipt replay refused: [Errno 13] protected note marker.md"),
        )
        is False
    )


def test_stage2a_d01_real_structural_marker_with_missing_phrase_is_permanent():
    error = ReMeBackendError(
        "Error: protected receipt replay refused: protected note marker is missing or contradictory",
    )
    assert PLUGIN_MODULE._is_permanent_write_failure(error) is True
    assert PLUGIN_MODULE._classify_write_failure(error) == "permanent"


def test_stage2a_r1_clean_legacy_timestamp_upgrade_is_durable_and_restart_stable(tmp_path):
    provider = ReMeMemoryProvider()
    provider._profile_id = "profile"
    provider._pending_path = _pending_path(tmp_path)
    legacy = _record("legacy-clean")
    legacy["payload"]["messages"][0].pop("created_at")
    _write_jsonl(provider._pending_path, legacy)

    upgraded = provider._read_pending_writes()
    assert upgraded is not None
    durable = provider._pending_path.read_bytes()
    persisted = json.loads(durable)
    created_at = persisted["payload"]["messages"][0]["created_at"]
    assert created_at == upgraded[0]["payload"]["messages"][0]["created_at"]
    assert hashlib.sha256(durable).hexdigest() == hashlib.sha256(provider._pending_path.read_bytes()).hexdigest()

    restarted = ReMeMemoryProvider()
    restarted._pending_path = provider._pending_path
    restarted._profile_id = "profile"
    assert restarted._read_pending_writes() == [persisted]
    assert restarted._pending_path.read_bytes() == durable


def test_a10_failed_retry_state_update_preserves_bytes_and_uses_bounded_cooldown(monkeypatch, tmp_path):
    backend = FakeBackend()
    provider = _provider_with_backend(monkeypatch, tmp_path, backend, retry=60)
    try:
        provider.shutdown()
        record = _record("retry-state")
        _write_jsonl(_pending_path(tmp_path), record)
        before = _pending_path(tmp_path).read_bytes()
        monkeypatch.setattr(
            PLUGIN_MODULE.os,
            "replace",
            lambda *_args: (_ for _ in ()).throw(OSError("replace failed")),
        )
        assert provider._update_pending_write({**record, "attempts": 1}) is False
        assert _pending_path(tmp_path).read_bytes() == before
        assert provider._next_write_attempt - time.monotonic() > 1
    finally:
        provider.shutdown()


@pytest.mark.parametrize("value", [-1, True, 4, 50_001, "1"])
def test_a11_semantic_counter_validation_rejects_invalid_values(value):
    from hermes_agent.turn_payload import valid_pending_record

    assert valid_pending_record({**_record("counter"), "semantic_refusals": value}) is False


def test_a11_legacy_missing_counter_and_valid_zero_to_three_are_admissible():
    from hermes_agent.turn_payload import valid_pending_record

    assert valid_pending_record(_record("legacy")) is True
    for count in range(4):
        assert valid_pending_record({**_record(f"count-{count}"), "semantic_refusals": count}) is True


@pytest.mark.parametrize("failure", ["file", "directory"])
def test_a12_quarantine_durability_failures_keep_pending_head_and_no_ack(monkeypatch, tmp_path, failure):
    backend = FakeBackend()
    provider = _provider_with_backend(monkeypatch, tmp_path, backend, retry=60)
    try:
        provider.shutdown()
        record = _record("durability")
        _write_jsonl(_pending_path(tmp_path), record)
        before = _pending_path(tmp_path).read_bytes()
        if failure == "file":
            monkeypatch.setattr(PLUGIN_MODULE, "_fsync_file", lambda *_args: (_ for _ in ()).throw(OSError("ENOSPC")))
        else:
            monkeypatch.setattr(
                PLUGIN_MODULE,
                "_fsync_directory",
                lambda *_args: (_ for _ in ()).throw(OSError("directory fsync failed")),
            )
        assert provider._quarantine_pending_write(record, "semantic_refusal") is False
        assert _pending_path(tmp_path).read_bytes() == before
        assert backend.request_ids == []
    finally:
        provider.shutdown()


def test_a13_restart_reconciles_durable_quarantine_without_backend_replay(monkeypatch, tmp_path):
    backend = FakeBackend()
    record = _record(
        "crash-quarantine",
        semantic_refusals=3,
        reason="semantic_refusal",
        outcome="QUARANTINED/NOT COMMITTED",
    )
    _write_jsonl(_pending_path(tmp_path), record)
    _write_jsonl(_quarantine_path(tmp_path), record)
    provider = _provider_with_backend(monkeypatch, tmp_path, backend)
    try:
        assert _wait_for(lambda: _pending_path(tmp_path).read_text(encoding="utf-8") == "")
    finally:
        provider.shutdown()
    assert backend.request_ids == []


def test_a14_live_cleanup_failure_does_not_release_fifo_successor(monkeypatch, tmp_path):
    backend = FakeBackend()
    calls = []

    def auto_memory(session_id, messages, *, timeout, request_id=None):
        del session_id, messages, timeout
        calls.append(request_id)
        if len(calls) == 1:
            raise ReMeBackendError("Error: protected receipt replay refused: protected receipt is malformed")
        return {"success": True}

    backend.auto_memory = auto_memory
    provider = _provider_with_backend(monkeypatch, tmp_path, backend, retry=0.01)
    _write_jsonl(_pending_path(tmp_path), _record("head"), _record("successor"))
    original_remove = provider._remove_pending_write
    monkeypatch.setattr(provider, "_remove_pending_write", lambda _record: False)
    provider._start_writer_if_needed()
    try:
        assert _wait_for(lambda: provider._journal_state == "QUARANTINE_CLEANUP")
        assert calls == [calls[0]]
        time.sleep(0.05)
        assert calls == [calls[0]]
        monkeypatch.setattr(provider, "_remove_pending_write", original_remove)
        assert _wait_for(lambda: len(calls) == 2)
    finally:
        monkeypatch.setattr(provider, "_remove_pending_write", original_remove)
        provider.shutdown()
    assert calls[1] == "successor"


def test_a15_replace_or_fsync_failure_preserves_exact_pending_record_for_restart(monkeypatch, tmp_path):
    backend = FakeBackend()
    provider = _provider_with_backend(monkeypatch, tmp_path, backend, retry=60)
    try:
        provider.shutdown()
        record = _record("replace-crash")
        _write_jsonl(_pending_path(tmp_path), record)
        before = _pending_path(tmp_path).read_bytes()
        monkeypatch.setattr(PLUGIN_MODULE.os, "replace", lambda *_args: (_ for _ in ()).throw(OSError("replace crash")))
        assert provider._remove_pending_write(record) is False
        assert _pending_path(tmp_path).read_bytes() == before
    finally:
        provider.shutdown()


@pytest.mark.parametrize(
    "response",
    [
        {"success": True},
        {"success": True, "metadata": {}},
        {"success": True, "metadata": {"commit": {"request_id": "other"}}},
    ],
)
def test_stage2a_d06_protected_success_without_bound_receipt_keeps_writer_pending(monkeypatch, tmp_path, response):
    backend = FakeBackend()

    def auto_memory(session_id, messages, *, timeout, request_id=None):
        del session_id, messages, timeout
        backend.request_ids.append(request_id)
        return response

    backend.auto_memory = auto_memory
    provider = _protected_provider_with_backend(monkeypatch, tmp_path, backend)
    try:
        provider.sync_turn("protected receipt", "assistant")
        assert _wait_for(lambda: bool(backend.request_ids))
        time.sleep(0.05)
        assert _pending_path(tmp_path).read_text(encoding="utf-8").strip()
        assert not _quarantine_path(tmp_path).exists()
    finally:
        provider.shutdown()


def test_a16_same_id_different_payload_is_not_mutated_by_update_or_remove(monkeypatch, tmp_path):
    backend = FakeBackend()
    provider = _provider_with_backend(monkeypatch, tmp_path, backend, retry=60)
    try:
        provider.shutdown()
        first = _record("same-id", payload=_payload("original"))
        conflict = _record("same-id", payload=_payload("different"))
        _write_jsonl(_pending_path(tmp_path), first, conflict)
        before = _pending_path(tmp_path).read_bytes()
        assert provider._update_pending_write({**first, "attempts": 1}) is False
        assert provider._remove_pending_write(first) is False
        assert _pending_path(tmp_path).read_bytes() == before
    finally:
        provider.shutdown()


def test_a16_cross_profile_same_id_conflict_halts_reconciliation(monkeypatch, tmp_path):
    backend = FakeBackend()
    _write_jsonl(_pending_path(tmp_path), _record("same-id", profile="profile-one"))
    provider = _provider_with_backend(monkeypatch, tmp_path, backend)
    try:
        provider._profile_id = "profile-two"
        provider._start_writer_if_needed()
        assert provider._write_thread is None
    finally:
        provider.shutdown()
    assert backend.request_ids == []


def test_a17_conflicting_quarantine_duplicates_halt_before_backend(monkeypatch, tmp_path):
    backend = FakeBackend()
    record = _record(
        "duplicate",
        semantic_refusals=3,
        reason="semantic_refusal",
        outcome="QUARANTINED/NOT COMMITTED",
    )
    conflict = {**record, "payload": _payload("conflicting")}
    _write_jsonl(_pending_path(tmp_path), record)
    _write_jsonl(_quarantine_path(tmp_path), record, conflict)
    provider = _provider_with_backend(monkeypatch, tmp_path, backend)
    try:
        time.sleep(0.1)
    finally:
        provider.shutdown()
    assert backend.request_ids == []


def test_stage2a_d10_invalid_quarantine_outcome_preserves_pending_and_halts(monkeypatch, tmp_path):
    backend = FakeBackend()
    pending = _record("invalid-outcome", semantic_refusals=3)
    invalid = {
        "id": pending["id"],
        "profile_id": pending["profile_id"],
        "attempts": pending["attempts"],
        "semantic_refusals": 3,
        "reason": "semantic_refusal",
        "outcome": "COMMITTED",
        "payload": pending["payload"],
    }
    _write_jsonl(_pending_path(tmp_path), pending)
    _write_jsonl(_quarantine_path(tmp_path), invalid)
    before = _pending_path(tmp_path).read_bytes()
    provider = _provider_with_backend(monkeypatch, tmp_path, backend)
    try:
        time.sleep(0.1)
        assert provider._journal_state == "HALTED_CORRUPT"
    finally:
        provider.shutdown()
    assert _pending_path(tmp_path).read_bytes() == before
    assert backend.request_ids == []


def test_stage2a_d11_conflicting_quarantine_evidence_halts_without_pending_deletion(monkeypatch, tmp_path):
    backend = FakeBackend()
    pending = _record("conflicting-outcome", semantic_refusals=3)
    first = {
        **pending,
        "reason": "semantic_refusal",
        "outcome": "QUARANTINED/NOT COMMITTED",
    }
    second = {**first, "outcome": "COMMITTED"}
    _write_jsonl(_pending_path(tmp_path), pending)
    _write_jsonl(_quarantine_path(tmp_path), first, second)
    before = _pending_path(tmp_path).read_bytes()
    provider = _provider_with_backend(monkeypatch, tmp_path, backend)
    try:
        time.sleep(0.1)
        assert provider._journal_state == "HALTED_CORRUPT"
    finally:
        provider.shutdown()
    assert _pending_path(tmp_path).read_bytes() == before
    assert backend.request_ids == []


@pytest.mark.parametrize("raw", [b"not-json\n", b'{"id":"bad"\n', b'{"id":"bad"}\n\xff'])
def test_a18_corrupt_pending_bytes_halt_admission_without_repair_or_backend(monkeypatch, tmp_path, raw):
    backend = FakeBackend()
    _pending_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    _pending_path(tmp_path).write_bytes(raw)
    provider = _provider_with_backend(monkeypatch, tmp_path, backend)
    try:
        before = _pending_path(tmp_path).read_bytes()
        provider.sync_turn("must not admit", "assistant")
        time.sleep(0.1)
    finally:
        provider.shutdown()
    assert _pending_path(tmp_path).read_bytes() == before
    assert backend.request_ids == []


def test_a19_corrupt_pending_head_blocks_valid_follower_and_backend_work(monkeypatch, tmp_path):
    backend = FakeBackend()
    _pending_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    _pending_path(tmp_path).write_bytes(b"not-json\n" + (json.dumps(_record("follower")).encode() + b"\n"))
    provider = _provider_with_backend(monkeypatch, tmp_path, backend)
    try:
        time.sleep(0.1)
    finally:
        provider.shutdown()
    assert backend.request_ids == []


def test_a20_read_failure_is_not_reported_as_empty_healthy_queue(monkeypatch, tmp_path):
    backend = FakeBackend()
    provider = _provider_with_backend(monkeypatch, tmp_path, backend, retry=60)
    try:
        provider.shutdown()
        _write_jsonl(_pending_path(tmp_path), _record("read-failure"))
        monkeypatch.setattr(provider, "_scan_journal", lambda *_args, **_kwargs: None)
        provider._start_writer_if_needed()
        assert provider._write_thread is None
        assert provider._accept_writes is False
    finally:
        provider.shutdown()


def test_stage2a_r3_broken_pending_symlink_is_not_an_empty_healthy_journal(monkeypatch, tmp_path):
    backend = FakeBackend()
    provider = _provider_with_backend(monkeypatch, tmp_path, backend, retry=60)
    try:
        path = _pending_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.symlink_to(tmp_path / "missing-target.jsonl")
        assert provider._read_pending_writes() is None
        assert provider._journal_state == "HALTED_CORRUPT"
    finally:
        provider.shutdown()


def test_stage2a_r5_shutdown_state_is_terminal_for_reconciliation(monkeypatch, tmp_path):
    provider = _provider_with_backend(monkeypatch, tmp_path, FakeBackend(), retry=60)
    provider.shutdown()
    assert provider._journal_state == "SHUTDOWN"
    assert provider._reconcile_durable_state() is False


@pytest.mark.skipif(
    os.environ.get("FHM007_REME_HOST_TESTS") != "1",
    reason="run real ReMe Application/Option B qualification on the authorised host",
)
@pytest.mark.asyncio
async def test_a21_direct_reme_memory_hint_refusal_has_no_noop_receipt_and_hermes_payload_remains_distinct(
    monkeypatch,
    tmp_path,
):
    from reme.application import Application
    from reme.config import resolve_app_config
    from reme.components.agent_wrapper.as_agent_wrapper import AsAgentWrapper

    workspace = tmp_path / "workspace"
    config_file = Path(__file__).resolve().parents[2] / "reme/config/fermes-hermes-stage1.yaml"
    config = resolve_app_config(config=str(config_file), workspace_dir=str(workspace), log_config=False)

    async def invalid_reply(_self, _inputs, **_kwargs):
        return {"result": "not-json", "tool_execution": []}

    monkeypatch.setattr(AsAgentWrapper, "reply", invalid_reply)
    async with Application(**config) as app:
        response = await app.run_job(
            "auto_memory",
            request_id="hint-refusal",
            session_id="hint-session",
            memory_hint="retain this exact user intent",
            messages=[{"id": "hint-message", "name": "user", "role": "user", "content": "fact"}],
            date="2026-11-06",
        )
    assert response.success is False
    assert response.metadata.get("outcome") != "noop"
    assert not list((workspace / "metadata" / "auto_memory_requests").rglob("*.json"))
    assert not list((workspace / "daily").rglob("*.md"))

    payload = PLUGIN_MODULE._build_turn_payload("profile", "session", "original user", "original assistant")
    assert [message["content"] for message in payload["messages"]] == ["original user", "original assistant"]


@pytest.mark.skipif(
    os.environ.get("FHM007_REME_HOST_TESTS") != "1",
    reason="run real ReMe Application compatibility qualification on the authorised host",
)
@pytest.mark.asyncio
async def test_a22_default_reme_job_remains_ordinary_and_non_opt_in(monkeypatch, tmp_path):
    from reme.application import Application
    from reme.config import resolve_app_config
    from reme.components.agent_wrapper.as_agent_wrapper import AsAgentWrapper

    workspace = tmp_path / "workspace"
    config_file = Path(__file__).resolve().parents[2] / "reme/config/default.yaml"
    config = resolve_app_config(config=str(config_file), workspace_dir=str(workspace), log_config=False)

    async def ordinary_reply(_self, _inputs, **_kwargs):
        return {"result": "ordinary result", "tool_execution": []}

    monkeypatch.setattr(AsAgentWrapper, "reply", ordinary_reply)
    async with Application(**config) as app:
        response = await app.run_job(
            "auto_memory",
            request_id="ordinary-job",
            session_id="ordinary-session",
            messages=[{"id": "ordinary-message", "name": "user", "role": "user", "content": "fact"}],
            date="2026-11-06",
        )
    assert response.success is True
    assert "fermes_refusal_code" not in response.metadata
    assert list((workspace / "metadata" / "auto_memory_requests").rglob("*.json"))


@pytest.mark.skipif(os.name == "nt", reason="POSIX link safety is qualified on a POSIX host")
def test_a23_posix_hardlink_and_symlink_queue_targets_fail_closed(monkeypatch, tmp_path):
    backend = FakeBackend()
    provider = _provider_with_backend(monkeypatch, tmp_path, backend, retry=60)
    try:
        provider.shutdown()
        path = _pending_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        outside = tmp_path / "outside.jsonl"
        outside.write_bytes(b"original\n")
        path.hardlink_to(outside)
        with pytest.raises(OSError):
            provider._append_pending_write(_payload("hardlink"))
        assert outside.read_bytes() == b"original\n"
        path.unlink()
        path.symlink_to(outside)
        provider.sync_turn("symlink", "must fail")
        time.sleep(0.05)
        assert outside.read_bytes() == b"original\n"
    finally:
        provider.shutdown()


def test_a24_shutdown_cancels_retry_timer_and_does_not_replay_after_ownership_release(monkeypatch, tmp_path):
    backend = FakeBackend()
    backend.auto_memory = lambda *args, **kwargs: {"success": False, "answer": "temporary"}
    provider = _provider_with_backend(monkeypatch, tmp_path, backend, retry=60)
    provider.sync_turn("pending", "assistant")
    assert _wait_for(lambda: bool(provider._retry_timers))
    provider.shutdown()
    count = len(backend.request_ids)
    time.sleep(0.1)
    assert len(backend.request_ids) == count
    assert provider._retry_timers == set()


def test_a25_actual_writer_uses_append_order_when_timestamps_are_equal(monkeypatch, tmp_path):
    backend = FakeBackend()
    records = [_record("first"), _record("second"), _record("third")]
    _write_jsonl(_pending_path(tmp_path), *records)
    provider = _provider_with_backend(monkeypatch, tmp_path, backend)
    try:
        assert _wait_for(lambda: len(backend.request_ids) == 3)
    finally:
        provider.shutdown()
    assert backend.request_ids == ["first", "second", "third"]


def test_a26_new_record_and_oversized_line_limits_apply_backpressure_without_deletion(monkeypatch, tmp_path):
    backend = FakeBackend()
    provider = _provider_with_backend(monkeypatch, tmp_path, backend, retry=60)
    try:
        provider.shutdown()
        oversized_payload = _payload("x" * (1024 * 1024))
        before = _pending_path(tmp_path).read_bytes() if _pending_path(tmp_path).exists() else b""
        with pytest.raises((OSError, ValueError)):
            provider._append_pending_write(oversized_payload)
        assert (_pending_path(tmp_path).read_bytes() if _pending_path(tmp_path).exists() else b"") == before
        _pending_path(tmp_path).write_bytes(b"x" * (2 * 1024 * 1024 + 1) + b"\n")
        assert provider._read_pending_writes() is None
    finally:
        provider.shutdown()


def test_a27_success_without_verified_receipt_does_not_ack_or_claim_noop(monkeypatch, tmp_path):
    backend = FakeBackend()

    def no_receipt(session_id, messages, *, timeout, request_id=None):
        del session_id, messages, timeout
        backend.request_ids.append(request_id)
        return {"success": True, "answer": "saved without receipt"}

    backend.auto_memory = no_receipt
    provider = _provider_with_backend(monkeypatch, tmp_path, backend, retry=60)
    try:
        provider.sync_turn("receipt required", "assistant")
        assert _wait_for(lambda: bool(backend.request_ids))
        time.sleep(0.05)
    finally:
        provider.shutdown()
    assert _pending_path(tmp_path).read_text(encoding="utf-8").strip()


def test_a28_ack_failure_replays_same_idempotent_request_without_duplicate_note(monkeypatch, tmp_path):
    backend = FakeBackend()
    provider = _provider_with_backend(monkeypatch, tmp_path, backend, retry=0.01)
    original_remove = provider._remove_pending_write
    monkeypatch.setattr(provider, "_remove_pending_write", lambda _record_id: False)
    provider.sync_turn("same durable turn", "assistant")
    assert _wait_for(lambda: len(backend.request_ids) == 1)
    first_id = backend.request_ids[0]
    provider._shutdown_started = True
    monkeypatch.setattr(provider, "_remove_pending_write", original_remove)
    provider._shutdown_started = False
    provider._accept_writes = True
    provider._next_write_attempt = 0.0
    provider._queue_pending_records()
    provider._start_writer_if_needed()
    assert _wait_for(lambda: len(backend.request_ids) >= 2)
    provider.shutdown()
    assert backend.request_ids[:2] == [first_id, first_id]


def test_a29_corrupt_journal_rejects_new_admission_instead_of_repairing_newline(monkeypatch, tmp_path):
    backend = FakeBackend()
    _pending_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    _pending_path(tmp_path).write_bytes(b"not-json")
    provider = _provider_with_backend(monkeypatch, tmp_path, backend)
    try:
        provider.sync_turn("new turn", "assistant")
        time.sleep(0.1)
    finally:
        provider.shutdown()
    assert _pending_path(tmp_path).read_bytes() == b"not-json"
    assert backend.request_ids == []


def test_a30_only_exact_server_owned_code_from_protected_auto_memory_is_classifiable():
    retry_policy = importlib.import_module("hermes_agent.retry_policy")
    classify_write_failure = getattr(retry_policy, "classify_write_failure", None)
    assert callable(classify_write_failure), "typed refusal classifier is not implemented"

    exact = ReMeBackendError("arbitrary answer")
    exact.refusal_code = REFUSAL_CODE
    forged = ReMeBackendError(REFUSAL_CODE)
    wrong = ReMeBackendError("arbitrary answer")
    wrong.refusal_code = "user-supplied"
    assert classify_write_failure(exact) == "semantic_refusal"
    assert classify_write_failure(forged) != "semantic_refusal"
    assert classify_write_failure(wrong) != "semantic_refusal"


@pytest.mark.skipif(os.name != "nt", reason="native Windows reparse/replace qualification")
def test_a31_native_windows_reparse_guard_and_atomic_replace_are_real(monkeypatch, tmp_path):
    backend = FakeBackend()
    provider = _provider_with_backend(monkeypatch, tmp_path, backend, retry=60)
    try:
        provider.shutdown()
        path = _pending_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_jsonl(path, _record("windows"))
        monkeypatch.setattr(PLUGIN_MODULE, "_is_reparse_point", lambda *_args: True)
        provider.sync_turn("reparse must fail", "assistant")
        time.sleep(0.05)
        assert _pending_path(tmp_path).read_text(encoding="utf-8") == path.read_text(encoding="utf-8")
        monkeypatch.setattr(PLUGIN_MODULE, "_is_reparse_point", lambda *_args: False)
        assert provider._remove_pending_write(_record("windows")) is True
        assert path.read_text(encoding="utf-8") == ""
    finally:
        provider.shutdown()


def _make_native_windows_junction(link: Path, target: Path) -> None:
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows NTFS junctions")
def test_f_d14_native_regular_profile_success_and_ack(monkeypatch, tmp_path):
    backend = FakeBackend()
    provider = _provider_with_backend(monkeypatch, tmp_path, backend)
    try:
        provider.sync_turn("native regular profile", "assistant")
        assert _wait_for(
            lambda: len(backend.request_ids) == 1
            and not provider._queued_record_ids
            and _pending_path(tmp_path).read_text() == "",
        )
        assert provider._pending_path.resolve().is_relative_to(tmp_path.resolve())
        assert not _quarantine_path(tmp_path).exists()
    finally:
        provider.shutdown()


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows NTFS junctions")
def test_f_d14_native_nested_junction_and_broken_junction_fail_closed(monkeypatch, tmp_path):
    external = tmp_path / "external-target"
    external.mkdir()
    sentinel = external / "sentinel.txt"
    sentinel.write_bytes(b"outside-before")

    profile = tmp_path / "profile"
    profile.mkdir()
    _make_native_windows_junction(profile / "reme", external)
    provider = _provider_with_backend(monkeypatch, profile, FakeBackend())
    try:
        assert provider._accept_writes is False
        provider.sync_turn("must not escape", "assistant")
        assert sentinel.read_bytes() == b"outside-before"
        assert not (external / "pending-writes.jsonl").exists()
    finally:
        provider.shutdown()

    broken_profile = tmp_path / "broken-profile"
    broken_profile.mkdir()
    _make_native_windows_junction(broken_profile / "reme", tmp_path / "missing-target")
    broken_provider = _provider_with_backend(monkeypatch, broken_profile, FakeBackend())
    try:
        assert broken_provider._accept_writes is False
        assert not (tmp_path / "missing-target" / "pending-writes.jsonl").exists()
    finally:
        broken_provider.shutdown()


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows NTFS junctions")
def test_f_d14_native_owner_configured_home_alias_resolves_to_canonical_root(monkeypatch, tmp_path):
    canonical = tmp_path / "canonical-home"
    canonical.mkdir()
    alias = tmp_path / "configured-home-alias"
    _make_native_windows_junction(alias, canonical)

    backend = FakeBackend()
    provider = _provider_with_backend(monkeypatch, alias, backend)
    try:
        assert alias.is_junction()
        assert alias.resolve() == canonical.resolve()
        assert provider._pending_path == canonical / "reme" / "pending-writes.jsonl"
        provider.sync_turn("configured alias remains supported", "assistant")
        assert _wait_for(
            lambda: len(backend.request_ids) == 1
            and not provider._queued_record_ids
            and provider._pending_path.read_text() == "",
        )
        assert (canonical / "reme" / "pending-writes.jsonl").exists()
        assert not (alias / "reme" / "pending-writes.jsonl").is_symlink()
    finally:
        provider.shutdown()


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows NTFS junctions")
def test_f_d14_native_rewrite_pre_replace_swap_fails_closed(monkeypatch, tmp_path):
    profile = tmp_path / "profile"
    journal_dir = profile / "reme"
    journal_dir.mkdir(parents=True)
    external = tmp_path / "external-target"
    external.mkdir()
    path = journal_dir / "pending-writes.jsonl"
    record = _record("swap", payload=_payload("canonical"))
    _write_jsonl(path, record)
    original_bytes = path.read_bytes()
    external_before = b"external-before"
    (external / "sentinel.txt").write_bytes(external_before)

    provider = ReMeMemoryProvider()
    provider._pending_path = path
    provider._profile_root = profile.resolve()
    original_safe = provider._journal_path_safe
    checks = 0

    def swap_parent(candidate):
        nonlocal checks
        checks += 1
        if checks == 2:
            moved = tmp_path / "canonical-reme-after-swap"
            journal_dir.rename(moved)
            _make_native_windows_junction(journal_dir, external)
        return original_safe(candidate)

    monkeypatch.setattr(provider, "_journal_path_safe", swap_parent)
    try:
        assert provider._rewrite_pending_record(path, record) is False
        assert (tmp_path / "canonical-reme-after-swap" / path.name).read_bytes() == original_bytes
        assert (external / "sentinel.txt").read_bytes() == external_before
        assert not (external / path.name).exists()
    finally:
        provider.shutdown()


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows NTFS junctions")
def test_f_d14_native_update_remove_quarantine_refuse_unsafe_child(tmp_path):
    profile = tmp_path / "profile"
    journal_dir = profile / "reme"
    journal_dir.mkdir(parents=True)
    external = tmp_path / "external-target"
    external.mkdir()
    path = journal_dir / "pending-writes.jsonl"
    record = _record("unsafe-child")
    _write_jsonl(path, record)
    original_bytes = path.read_bytes()
    sentinel = external / "sentinel.txt"
    sentinel.write_bytes(b"external-before")

    provider = ReMeMemoryProvider()
    provider._pending_path = path
    provider._profile_root = profile.resolve()
    provider._profile_id = "profile"
    moved = tmp_path / "canonical-reme-before-swap"
    journal_dir.rename(moved)
    _make_native_windows_junction(journal_dir, external)
    try:
        updated = dict(record, attempts=1)
        assert provider._update_pending_write(updated) is False
        assert provider._remove_pending_write(record) is False
        assert provider._quarantine_pending_write(record, "semantic_refusal") is False
        assert (moved / path.name).read_bytes() == original_bytes
        assert sentinel.read_bytes() == b"external-before"
        assert not (external / "quarantined-writes.jsonl").exists()
    finally:
        provider.shutdown()


def test_a32_large_backend_error_is_bounded_in_diagnostics_and_never_leaks_payload(monkeypatch, tmp_path, caplog):
    secret = "PRIVATE-MODEL-CONTENT-" + ("x" * (8 * 1024 * 1024))
    backend = FakeBackend()

    def large_error(session_id, messages, *, timeout, request_id=None):
        del session_id, messages, timeout
        backend.request_ids.append(request_id)
        return {"success": False, "answer": secret}

    backend.auto_memory = large_error
    provider = _provider_with_backend(monkeypatch, tmp_path, backend, retry=60)
    try:
        provider.sync_turn("large error", "assistant")
        assert _wait_for(lambda: bool(backend.request_ids))
    finally:
        provider.shutdown()
    assert "PRIVATE-MODEL-CONTENT" not in caplog.text
    assert len(caplog.text) < 100_000


def test_a33_legacy_quarantine_reconciliation_requires_exact_profile_and_payload(monkeypatch, tmp_path):
    backend = FakeBackend()
    pending = _record("legacy", payload=_payload("same"))
    legacy = {
        "id": pending["id"],
        "payload": pending["payload"],
        "reason": "semantic_refusal",
        "outcome": "QUARANTINED/NOT COMMITTED",
    }
    _write_jsonl(_pending_path(tmp_path), pending)
    _write_jsonl(_quarantine_path(tmp_path), legacy)
    provider = _provider_with_backend(monkeypatch, tmp_path, backend)
    try:
        assert _wait_for(lambda: _pending_path(tmp_path).read_text(encoding="utf-8") == "")
    finally:
        provider.shutdown()
    assert backend.request_ids == []


def test_a34_fifty_concurrent_producers_have_one_durable_fifo_record_each(monkeypatch, tmp_path):
    backend = FakeBackend()
    backend_lock = Lock()
    first_backend_entered = Event()
    release_first_backend = Event()
    seen = []

    def auto_memory(session_id, messages, *, timeout, request_id=None):
        del session_id, messages, timeout
        with backend_lock:
            first = not seen
        if first:
            first_backend_entered.set()
            release_first_backend.wait(timeout=20)
        with backend_lock:
            seen.append(request_id)
        return {"success": True}

    backend.auto_memory = auto_memory
    provider = _provider_with_backend(monkeypatch, tmp_path, backend)
    threads = [Thread(target=provider.sync_turn, args=(f"user-{index}", "assistant")) for index in range(50)]
    started_at = time.monotonic()
    deadline = started_at + 20.0
    try:
        threads[0].start()
        assert first_backend_entered.wait(timeout=max(0.0, deadline - time.monotonic()))
        for thread in threads[1:]:
            thread.start()
        for thread in threads:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        assert all(not thread.is_alive() for thread in threads), _a34_diagnostics(
            provider,
            threads,
            seen,
            started_at,
        )
        pending_snapshot = [
            json.loads(line)["id"]
            for line in _pending_path(tmp_path).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert len(pending_snapshot) == 50, _a34_diagnostics(provider, threads, seen, started_at)
        release_first_backend.set()
        assert _wait_until(
            deadline,
            lambda: len(seen) == 50
            and _pending_path(tmp_path).exists()
            and _pending_path(tmp_path).read_text(encoding="utf-8") == "",
        ), _a34_diagnostics(provider, threads, seen, started_at)
    finally:
        release_first_backend.set()
        provider.shutdown()
    assert len(seen) == 50
    assert len(set(seen)) == 50
    assert seen == pending_snapshot
    assert _pending_path(tmp_path).read_text(encoding="utf-8") == ""


def test_a34_blocked_backend_deadline_preserves_pending_then_drains_once(monkeypatch, tmp_path):
    backend = FakeBackend()
    entered = Event()
    release = Event()
    request_ids = []

    def blocked_auto_memory(session_id, messages, *, timeout, request_id=None):
        del session_id, messages, timeout
        entered.set()
        release.wait(timeout=5)
        request_ids.append(request_id)
        return {"success": True}

    backend.auto_memory = blocked_auto_memory
    provider = _provider_with_backend(monkeypatch, tmp_path, backend, retry=0.01)
    try:
        provider.sync_turn("blocked", "assistant")
        assert entered.wait(timeout=1)
        before = _pending_path(tmp_path).read_bytes()
        short_deadline = time.monotonic() + 0.25
        assert not _wait_until(
            short_deadline,
            lambda: _pending_path(tmp_path).exists() and _pending_path(tmp_path).read_bytes() == b"",
        )
        assert _pending_path(tmp_path).read_bytes() == before
        assert provider._write_thread is not None and provider._write_thread.is_alive()
    finally:
        release.set()
        assert _wait_for(
            lambda: len(request_ids) == 1
            and not provider._queued_record_ids
            and _pending_path(tmp_path).read_text(encoding="utf-8") == "",
            5,
        )
        provider.shutdown()
    assert len(request_ids) == 1
    assert len(set(request_ids)) == 1


def test_a35_volatile_orphan_cannot_be_reintroduced_without_durable_admission(monkeypatch, tmp_path):
    from hermes_agent.write_queue import rebuild_write_queue_locked

    backend = FakeBackend()
    provider = _provider_with_backend(monkeypatch, tmp_path, backend, retry=60)
    try:
        provider.shutdown()
        orphan = _record("volatile-orphan")
        provider._write_queue.put(orphan)
        rebuild_write_queue_locked(provider, [])
        assert provider._write_queue.empty()
        assert provider._queued_record_ids == set()
    finally:
        provider.shutdown()
    assert backend.request_ids == []
