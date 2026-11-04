"""Queue ordering and durable retry tests for the external Hermes provider."""

# pylint: disable=missing-function-docstring,protected-access,redefined-outer-name

from __future__ import annotations

import importlib
import json
import sys
import threading
import time
from datetime import datetime as RealDateTime
from datetime import timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
FakeBackend = importlib.import_module("hermes_agent_test_support").FakeBackend
_integration = importlib.import_module("test_hermes_agent_integration")
PLUGIN_MODULE = _integration.PLUGIN_MODULE
ReMeConfig = _integration.ReMeConfig
ReMeMemoryProvider = _integration.ReMeMemoryProvider
config_path = _integration.config_path

TURN_PAYLOAD_MODULE = importlib.import_module("hermes_agent.turn_payload")


class ManualTimer:
    """Timer double whose wakeup is driven explicitly by the test."""

    instances = []

    def __init__(self, _delay, callback):
        self._callback = callback
        self.cancelled = False
        self.started = False
        self.instances.append(self)

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True

    def fire(self):
        if not self.cancelled:
            self._callback()


def test_spooled_turn_keeps_created_at_across_midnight_and_retry(monkeypatch, tmp_path):
    entered = threading.Event()
    allow_first = threading.Event()
    replayed = threading.Event()
    backend = FakeBackend()
    current = RealDateTime(2026, 10, 1, 23, 59, 59, tzinfo=timezone.utc)

    class FrozenDateTime(RealDateTime):
        """Control the wall clock while testing spool persistence."""

        @classmethod
        def now(cls, tz=None):
            return current.astimezone(tz) if tz else current.replace(tzinfo=None)

    def auto_memory(session_id, messages, *, timeout, request_id=None):
        del session_id, timeout
        backend.writes.append((request_id, messages))
        backend.request_ids.append(request_id)
        if len(backend.writes) == 1:
            entered.set()
            assert allow_first.wait(timeout=1)
            return {"success": False, "answer": "transient"}
        replayed.set()
        return {"success": True}

    backend.auto_memory = auto_memory
    monkeypatch.setattr(TURN_PAYLOAD_MODULE, "datetime", FrozenDateTime)
    monkeypatch.setattr(PLUGIN_MODULE, "load_config", lambda home=None: ReMeConfig(health_retry_seconds=0.01))
    monkeypatch.setattr(PLUGIN_MODULE, "_backend_for", lambda config: backend)

    provider = ReMeMemoryProvider()
    provider.initialize("session", hermes_home=tmp_path, agent_identity="profile")
    provider.sync_turn("before midnight", "assistant")
    assert entered.wait(timeout=1)
    current = RealDateTime(2026, 10, 2, 0, 0, 1, tzinfo=timezone.utc)
    allow_first.set()
    assert replayed.wait(timeout=2)
    provider.shutdown()

    assert len(backend.writes) >= 2
    first_messages = backend.writes[0][1]
    retry_messages = backend.writes[1][1]
    assert first_messages == retry_messages
    assert all(message["created_at"].startswith("2026-10-01T23:59:59") for message in retry_messages)


def test_retried_spool_record_reuses_record_id_after_ack_failure(monkeypatch, tmp_path):
    backend = FakeBackend()
    monkeypatch.setattr(PLUGIN_MODULE, "load_config", lambda home=None: ReMeConfig(health_retry_seconds=0.01))
    monkeypatch.setattr(PLUGIN_MODULE, "_backend_for", lambda config: backend)

    provider = ReMeMemoryProvider()
    provider.initialize("session", hermes_home=tmp_path, agent_identity="profile")
    original_remove = provider._remove_pending_write
    monkeypatch.setattr(provider, "_remove_pending_write", lambda _record_id: False)
    provider.sync_turn("same user", "same assistant")
    deadline = time.monotonic() + 1
    while len(backend.request_ids) < 1 and time.monotonic() < deadline:
        time.sleep(0.005)
    first_id = backend.request_ids[0]

    pending = config_path(tmp_path).parent / "pending-writes.jsonl"
    record = json.loads(pending.read_text(encoding="utf-8"))
    assert record["id"] == first_id

    provider._shutdown_started = True
    monkeypatch.setattr(provider, "_remove_pending_write", original_remove)
    provider._shutdown_started = False
    provider._accept_writes = True
    provider._health_retry_seconds = 0.01
    provider._queue_pending_records()
    provider._start_writer_if_needed()
    deadline = time.monotonic() + 1
    while len(backend.request_ids) < 2 and time.monotonic() < deadline:
        time.sleep(0.005)

    assert backend.request_ids[:2] == [first_id, first_id]
    provider.shutdown()


def test_embedded_reme_receipt_covers_success_before_spool_ack_crash_window(monkeypatch, tmp_path):
    import yaml

    from reme.steps.evolve.auto_memory import AutoMemoryStep

    calls = []
    stage_config = Path(__file__).resolve().parents[2] / "reme/config/fermes-hermes-stage1.yaml"
    # This is deliberately a legacy receipt/ACK test. Use a disposable
    # unprotected job config rather than silently downgrading protected Option B.
    legacy_config = yaml.safe_load(stage_config.read_text(encoding="utf-8"))
    legacy_config["jobs"]["auto_memory"]["steps"][0]["durable_commit"] = False
    legacy_config_path = tmp_path / "legacy-memory-stage1.yaml"
    legacy_config_path.write_text(yaml.safe_dump(legacy_config, sort_keys=False), encoding="utf-8")
    config = ReMeConfig(
        mode="embedded",
        workspace_dir=str(tmp_path / "workspace"),
        reme_config=str(legacy_config_path),
        allow_custom_config=True,
        request_timeout=5,
        recall_timeout=1,
        health_timeout=1,
        health_retry_seconds=0.01,
        shutdown_timeout=5,
    )

    async def synthetic_execute(step):
        calls.append(step.context.get("request_id"))
        step.context.response.answer = "synthetic receipt"
        step.context["changes"] = []

    monkeypatch.setattr(AutoMemoryStep, "_execute", synthetic_execute)
    # This provider queue test exercises the pre-Option-B receipt/ACK boundary
    # with a synthetic step response; the dedicated capture tests cover the
    # protected commit contract.
    monkeypatch.setattr(AutoMemoryStep, "_option_b_enabled", lambda _step: False)
    monkeypatch.setattr(PLUGIN_MODULE, "load_config", lambda home=None: config)
    profile = tmp_path / "profile"
    provider = ReMeMemoryProvider()
    provider.initialize("session", hermes_home=profile, agent_identity="profile")
    monkeypatch.setattr(provider, "_remove_pending_write", lambda _record_id: False)
    provider.sync_turn("crash-window user", "assistant")

    deadline = time.monotonic() + 5
    while len(calls) < 1 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(calls) == 1
    pending = profile / "reme" / "pending-writes.jsonl"
    assert pending.read_text(encoding="utf-8").strip()
    provider.shutdown()

    replay = ReMeMemoryProvider()
    replay.initialize("new-session", hermes_home=profile, agent_identity="profile")
    deadline = time.monotonic() + 5
    while pending.read_text(encoding="utf-8").strip() and time.monotonic() < deadline:
        time.sleep(0.01)
    replay.shutdown()

    assert len(calls) == 1
    assert pending.read_text(encoding="utf-8") == ""


def test_failed_first_record_blocks_later_record_until_recovery(monkeypatch, tmp_path):
    backend = FakeBackend()
    calls = 0
    first_started = threading.Event()
    allow_first = threading.Event()
    recovered = threading.Event()

    def auto_memory(session_id, messages, *, timeout, request_id=None):
        nonlocal calls
        del session_id, messages, timeout
        calls += 1
        backend.request_ids.append(request_id)
        if calls == 1:
            first_started.set()
            assert allow_first.wait(timeout=1)
            return {"success": False, "answer": "transient"}
        if calls == 3:
            recovered.set()
        return {"success": True}

    backend.auto_memory = auto_memory
    monkeypatch.setattr(PLUGIN_MODULE, "load_config", lambda home=None: ReMeConfig())
    monkeypatch.setattr(PLUGIN_MODULE, "_backend_for", lambda config: backend)
    provider = ReMeMemoryProvider()
    provider.initialize("session", hermes_home=tmp_path)
    provider._health_retry_seconds = 0.01
    provider.sync_turn("first", "assistant")
    assert first_started.wait(timeout=1)
    provider.sync_turn("second", "assistant")
    allow_first.set()

    assert recovered.wait(timeout=2)
    provider.shutdown()

    assert calls >= 3
    assert backend.request_ids[0] == backend.request_ids[1]
    assert backend.request_ids[2] != backend.request_ids[1]
    quarantine = config_path(tmp_path).parent / "quarantined-writes.jsonl"
    assert not quarantine.exists()


def test_retry_rebuilds_fifo_when_new_turn_arrives_before_timer(monkeypatch, tmp_path):
    ManualTimer.instances = []
    monkeypatch.setattr(PLUGIN_MODULE.threading, "Timer", ManualTimer)
    backend = FakeBackend()
    first_failed = threading.Event()
    retry_scheduled = threading.Event()
    retry_started = threading.Event()
    all_writes = threading.Event()
    seen = []

    def auto_memory(session_id, messages, *, timeout, request_id=None):
        del session_id, timeout
        seen.append((request_id, messages[0]["content"]))
        if len(seen) == 1:
            first_failed.set()
            return {"success": False, "answer": "transient"}
        if len(seen) == 2:
            retry_started.set()
        if len(seen) == 4:
            all_writes.set()
        return {"success": True}

    backend.auto_memory = auto_memory
    monkeypatch.setattr(PLUGIN_MODULE, "load_config", lambda home=None: ReMeConfig(health_retry_seconds=60))
    monkeypatch.setattr(PLUGIN_MODULE, "_backend_for", lambda config: backend)
    provider = ReMeMemoryProvider()
    provider.initialize("session", hermes_home=tmp_path, agent_identity="profile")
    schedule_retry = provider._schedule_retry

    def schedule_and_signal(record):
        schedule_retry(record)
        retry_scheduled.set()

    monkeypatch.setattr(provider, "_schedule_retry", schedule_and_signal)
    provider.sync_turn("first", "assistant")
    assert first_failed.wait(timeout=1)
    assert retry_scheduled.wait(timeout=1)
    assert len(ManualTimer.instances) == 1

    # Simulate the cooldown expiring while its timer is still waiting to run.
    provider._next_write_attempt = time.monotonic() - 1
    provider.sync_turn("second", "assistant")
    assert retry_started.wait(timeout=1)
    provider.sync_turn("third", "assistant")
    assert all_writes.wait(timeout=1)
    provider.shutdown()

    assert [content for _request_id, content in seen] == [
        "first",
        "first",
        "second",
        "third",
    ]
    assert ManualTimer.instances[0].cancelled is True
    assert provider._read_pending_writes() == []


def test_early_retry_timer_rearms_once_and_shutdown_cancels_wakeup(monkeypatch, tmp_path):
    ManualTimer.instances = []
    monkeypatch.setattr(PLUGIN_MODULE.threading, "Timer", ManualTimer)
    backend = FakeBackend()
    backend.auto_memory = lambda *args, **kwargs: {"success": False, "answer": "transient"}
    monkeypatch.setattr(PLUGIN_MODULE, "load_config", lambda home=None: ReMeConfig(health_retry_seconds=60))
    monkeypatch.setattr(PLUGIN_MODULE, "_backend_for", lambda config: backend)
    provider = ReMeMemoryProvider()
    provider.initialize("session", hermes_home=tmp_path)
    provider.sync_turn("queued", "assistant")
    deadline = time.monotonic() + 1
    while len(ManualTimer.instances) < 1 and time.monotonic() < deadline:
        time.sleep(0.005)
    assert len(ManualTimer.instances) == 1

    provider._next_write_attempt = time.monotonic() + 60
    ManualTimer.instances[0].fire()
    assert len(ManualTimer.instances) == 2
    assert len(provider._retry_timers) == 1
    provider.shutdown()

    assert provider._retry_timers == set()
    assert ManualTimer.instances[1].cancelled is True
    assert provider._read_pending_writes()


def test_restart_replays_pending_records_in_spool_order(monkeypatch, tmp_path):
    failed = FakeBackend()
    first_failed = threading.Event()

    def fail_once(session_id, messages, *, timeout, request_id=None):
        del session_id, messages, timeout
        failed.request_ids.append(request_id)
        first_failed.set()
        return {"success": False, "answer": "transient"}

    failed.auto_memory = fail_once
    recovered = FakeBackend()
    replayed = threading.Event()

    def recover(session_id, messages, *, timeout, request_id=None):
        del timeout
        recovered.writes.append((session_id, messages))
        recovered.request_ids.append(request_id)
        if len(recovered.request_ids) == 2:
            replayed.set()
        return {"success": True}

    recovered.auto_memory = recover
    backends = iter([failed, recovered])
    monkeypatch.setattr(PLUGIN_MODULE, "load_config", lambda home=None: ReMeConfig(health_retry_seconds=60))
    monkeypatch.setattr(PLUGIN_MODULE, "_backend_for", lambda config: next(backends))

    provider = ReMeMemoryProvider()
    provider.initialize("session", hermes_home=tmp_path, agent_identity="profile")
    provider.sync_turn("first", "assistant")
    provider.sync_turn("second", "assistant")
    assert first_failed.wait(timeout=1)
    pending = provider._read_pending_writes()
    assert len(pending) == 2
    provider.shutdown()

    replay = ReMeMemoryProvider()
    replay.initialize("restarted", hermes_home=tmp_path, agent_identity="profile")
    pending_path = config_path(tmp_path).parent / "pending-writes.jsonl"
    assert replayed.wait(timeout=2)
    replay.shutdown()

    assert recovered.request_ids == [pending[0]["id"], pending[1]["id"]]
    assert pending_path.read_text(encoding="utf-8") == ""


@pytest.mark.parametrize("response", [{"success": False}, [], None])
def test_false_or_malformed_auto_memory_ack_keeps_spool_record(monkeypatch, tmp_path, response):
    backend = FakeBackend()
    backend.auto_memory = lambda *args, **kwargs: response
    monkeypatch.setattr(PLUGIN_MODULE, "load_config", lambda home=None: ReMeConfig())
    monkeypatch.setattr(PLUGIN_MODULE, "_backend_for", lambda config: backend)
    provider = ReMeMemoryProvider()
    provider.initialize("session", hermes_home=tmp_path)
    provider.sync_turn("user", "assistant")
    deadline = time.monotonic() + 1
    while provider._write_thread is not None and time.monotonic() < deadline:
        time.sleep(0.005)

    pending = config_path(tmp_path).parent / "pending-writes.jsonl"
    assert pending.read_text(encoding="utf-8").strip()
    provider.shutdown()


def test_pending_queue_retains_poison_line_and_reads_following_valid_record(tmp_path):
    provider = ReMeMemoryProvider()
    provider._pending_path = tmp_path / "reme" / "pending-writes.jsonl"
    provider._pending_path.parent.mkdir()
    valid = {"id": "valid", "payload": {"session_id": "s", "messages": []}}
    binary_poison = b'{"id":"binary-poison","payload":\xff}\n'
    provider._pending_path.write_bytes(
        binary_poison
        + b"not-json\n"
        + b'{"id": "poison", "payload": "not-an-object"}\n'
        + json.dumps(valid).encode()
        + b"\n",
    )

    assert provider._read_pending_writes() == [valid]
    assert provider._update_pending_write({**valid, "attempts": 1, "payload": valid["payload"]}) is True
    assert provider._remove_pending_write("valid") is True
    remaining = provider._pending_path.read_bytes()
    assert binary_poison in remaining
    assert b"not-json" in remaining
    assert b'"poison"' in remaining
    assert b'"valid"' not in remaining


def test_startup_recovers_valid_record_after_invalid_utf8_poison_line(monkeypatch, tmp_path):
    backend = FakeBackend()
    monkeypatch.setattr(PLUGIN_MODULE, "load_config", lambda home=None: ReMeConfig(health_retry_seconds=0.01))
    monkeypatch.setattr(PLUGIN_MODULE, "_backend_for", lambda config: backend)
    pending = tmp_path / "reme" / "pending-writes.jsonl"
    pending.parent.mkdir()
    valid = {"id": "valid", "attempts": 0, "payload": {"session_id": "s", "messages": []}}
    binary_poison = b'{"id":"binary-poison","payload":\xff}\n'
    pending.write_bytes(binary_poison + json.dumps(valid).encode() + b"\n")

    provider = ReMeMemoryProvider()
    provider.initialize("session", hermes_home=tmp_path, agent_identity="profile")
    deadline = time.monotonic() + 1
    while len(backend.request_ids) < 1 and time.monotonic() < deadline:
        time.sleep(0.005)
    provider.shutdown()

    assert backend.request_ids == ["valid"]
    assert binary_poison in pending.read_bytes()
    assert b'"valid"' not in pending.read_bytes()
    assert provider._writer_starting is False


def test_disk_append_failure_does_not_acknowledge_or_queue_turn(monkeypatch, tmp_path, caplog):
    provider = ReMeMemoryProvider()
    provider._pending_path = tmp_path / "reme" / "pending-writes.jsonl"
    provider._accept_writes = True

    def fail(_payload):
        raise OSError("disk full")

    monkeypatch.setattr(provider, "_append_pending_write", fail)
    provider.sync_turn("user", "assistant", session_id="session")

    assert provider._write_queue.empty()
    assert not provider._pending_path.exists()
    assert "spool write failed" in caplog.text
    assert "shutting down" not in caplog.text
