"""Synthetic regressions for the FHM-007 Hermes provider security lane."""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass
from pathlib import Path
import threading

import pytest

PLUGIN_PARENT = Path(__file__).resolve().parents[2] / "integrations"
if str(PLUGIN_PARENT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_PARENT))

try:
    from agent.memory_provider import MemoryProvider, RecallStatus  # type: ignore[import-not-found]
except ImportError:
    agent_module = types.ModuleType("agent")
    memory_provider_module = sys.modules.get("agent.memory_provider", types.ModuleType("agent.memory_provider"))

    if not hasattr(memory_provider_module, "MemoryProvider"):

        class MemoryProvider:
            """Minimal Hermes contract for the isolated provider tests."""

        memory_provider_module.MemoryProvider = MemoryProvider

    if not hasattr(memory_provider_module, "RecallStatus"):

        @dataclass(frozen=True)
        class RecallStatus:
            """Minimal status value used by the provider contract."""

            provider_label: str
            count: int
            glyph: str = "🧠"

        memory_provider_module.RecallStatus = RecallStatus

    agent_module.memory_provider = memory_provider_module
    sys.modules["agent"] = agent_module
    sys.modules["agent.memory_provider"] = memory_provider_module

from hermes_agent import ReMeMemoryProvider, _ProfileWriterLock, _WorkspaceOwnerMarker
from hermes_agent.config import ReMeConfig


@dataclass
class FakeBackend:
    """Small synchronous backend that records lifecycle calls."""

    closed: bool = False

    def start(self, *, deadline=None):
        del deadline

    def health(self, *, timeout):
        del timeout
        return {"success": True, "metadata": {"health": {"healthy": True}}}

    def search(self, query, *, limit, timeout):
        del query, limit, timeout
        return {"success": True, "answer": "", "metadata": {}}

    def auto_memory(self, session_id, messages, *, timeout, request_id=None):
        del session_id, messages, timeout, request_id
        return {"success": True}

    def close(self, *, timeout):
        del timeout
        self.closed = True


def _use_fake_http_provider(monkeypatch, backend):
    monkeypatch.setattr("hermes_agent.load_config", lambda home=None: ReMeConfig())
    monkeypatch.setattr("hermes_agent._backend_for", lambda config: backend)


def test_shutdown_keeps_profile_lock_until_journal_ack_finishes(monkeypatch, tmp_path):
    """A slow journal acknowledgement cannot race profile lock release."""
    backend = FakeBackend()
    _use_fake_http_provider(monkeypatch, backend)
    provider = ReMeMemoryProvider()
    provider.initialize("session", hermes_home=tmp_path, agent_identity="profile")
    provider._shutdown_timeout = 0.01

    ack_entered = threading.Event()
    allow_ack = threading.Event()
    original_remove = provider._remove_pending_write

    def blocked_remove(record_id):
        ack_entered.set()
        assert allow_ack.wait(timeout=2)
        return original_remove(record_id)

    monkeypatch.setattr(provider, "_remove_pending_write", blocked_remove)
    provider.sync_turn("user", "assistant")
    assert ack_entered.wait(timeout=1)

    provider.shutdown()
    lock_path = tmp_path / "reme" / ".writer.lock"
    assert provider._profile_lock is not None
    contender = _ProfileWriterLock(lock_path)
    with pytest.raises(OSError, match="already owned|temporarily unavailable|resource busy"):
        contender.acquire()

    allow_ack.set()
    writer = provider._write_thread
    if writer is not None:
        writer.join(timeout=2)
    assert provider._write_thread is None or not provider._write_thread.is_alive()
    assert provider._profile_lock is None
    contender.acquire()
    contender.release()


def test_initialize_without_profile_releases_previous_profile_lock(monkeypatch, tmp_path):
    """Disabling a previously initialized provider must not strand its lock."""
    backend = FakeBackend()
    _use_fake_http_provider(monkeypatch, backend)
    provider = ReMeMemoryProvider()
    provider.initialize("profile-session", hermes_home=tmp_path, agent_identity="profile")
    assert provider._profile_lock is not None

    provider.initialize("unscoped-session")

    assert provider._profile_lock is None
    assert backend.closed is True
    contender = _ProfileWriterLock(tmp_path / "reme" / ".writer.lock")
    contender.acquire()
    contender.release()


def test_owner_marker_failure_does_not_publish_partial_final_file(monkeypatch, tmp_path):
    """A failed durable write leaves no final marker that can brick a workspace."""
    workspace = tmp_path / "workspace"
    marker = _WorkspaceOwnerMarker(workspace, tmp_path / "profile")

    def fail_fsync(_fd):
        raise OSError("synthetic fsync failure")

    monkeypatch.setattr("hermes_agent.os.fsync", fail_fsync)
    with pytest.raises(OSError, match="durable"):
        marker.acquire()

    assert not marker.path.exists()
    assert list(workspace.iterdir()) == []
