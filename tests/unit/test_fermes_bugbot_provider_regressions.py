"""Synthetic regressions for the FHM-007 Hermes provider security lane."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import types
from dataclasses import dataclass
from pathlib import Path

import pytest

# These tests intentionally inspect private lifecycle state to verify the provider boundary contract.
# pylint: disable=protected-access

PLUGIN_PARENT = Path(__file__).resolve().parents[2] / "integrations"
if str(PLUGIN_PARENT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_PARENT))

try:
    from agent.memory_provider import (  # type: ignore[import-not-found]
        MemoryProvider,
        RecallStatus,
    )
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

# These imports follow the optional Hermes dependency and repository plugin-path bootstrap above, so
# their execution order is part of the test contract. Keep the E402 suppressions local to these two
# imports; moving them before bootstrap would weaken the loading regression coverage.
# pylint: disable=wrong-import-position
from hermes_agent import (  # noqa: E402
    _WORKSPACE_OWNER_TEMP_PREFIX,
    ReMeMemoryProvider,
    _ProfileWriterLock,
    _WorkspaceOwnerMarker,
)
from hermes_agent.config import ReMeConfig  # noqa: E402

# pylint: enable=wrong-import-position


@dataclass
class FakeBackend:
    """Small synchronous backend that records lifecycle calls."""

    closed: bool = False
    close_calls: int = 0

    def start(self, *, deadline=None):
        """Record the minimal backend start contract used by the provider."""
        del deadline

    def health(self, *, timeout):
        """Return a healthy status for provider initialization."""
        del timeout
        return {"success": True, "metadata": {"health": {"healthy": True}}}

    def search(self, query, *, limit, timeout):
        """Return an empty successful search result."""
        del query, limit, timeout
        return {"success": True, "answer": "", "metadata": {}}

    def auto_memory(self, session_id, messages, *, timeout, request_id=None):
        """Accept the minimal auto-memory contract without persisting data."""
        del session_id, messages, timeout, request_id
        return {"success": True}

    def close(self, *, timeout):
        """Record backend shutdown for lifecycle assertions."""
        del timeout
        self.close_calls += 1
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


def test_writer_shutdown_gate_covers_backend_finally_before_journal_ack(monkeypatch, tmp_path):
    """A deferred backend close cannot release ownership before the writer's journal ACK."""
    backend = FakeBackend()
    auto_memory_entered = threading.Event()
    allow_auto_memory = threading.Event()

    def blocked_auto_memory(session_id, messages, *, timeout, request_id=None):
        del session_id, messages, timeout, request_id
        auto_memory_entered.set()
        assert allow_auto_memory.wait(timeout=2)
        return {"success": True}

    backend.auto_memory = blocked_auto_memory
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
    assert auto_memory_entered.wait(timeout=1)

    provider.shutdown()
    writer = provider._write_thread
    assert writer is not None and writer.is_alive()
    allow_auto_memory.set()
    assert ack_entered.wait(timeout=1)

    contender = _ProfileWriterLock(tmp_path / "reme" / ".writer.lock")
    with pytest.raises(OSError, match="already owned|temporarily unavailable|resource busy"):
        contender.acquire()

    allow_ack.set()
    writer.join(timeout=2)
    assert writer.is_alive() is False
    assert provider._deferred_backend_close is False
    assert backend.closed is True
    assert provider._profile_lock is None
    contender.acquire()
    contender.release()


def test_deferred_shutdown_closes_backend_and_releases_profile_lock_once(monkeypatch, tmp_path):
    """A deferred close owns the profile until the later operation finishes it."""

    class BlockingBackend(FakeBackend):
        """Backend double that blocks search and close independently."""

        def __init__(self):
            super().__init__()
            self.search_entered = threading.Event()
            self.allow_search = threading.Event()
            self.close_entered = threading.Event()
            self.allow_close = threading.Event()

        def search(self, query, *, limit, timeout):
            del query, limit, timeout
            self.search_entered.set()
            assert self.allow_search.wait(timeout=2)
            return {"success": True, "answer": "", "metadata": {}}

        def close(self, *, timeout):
            del timeout
            self.close_entered.set()
            assert self.allow_close.wait(timeout=2)
            super().close(timeout=0)

    backend = BlockingBackend()
    _use_fake_http_provider(monkeypatch, backend)
    provider = ReMeMemoryProvider()
    provider.initialize("session", hermes_home=tmp_path, agent_identity="profile")
    provider._shutdown_timeout = 0.01

    recall = threading.Thread(target=provider.prefetch, args=("query",))
    recall.start()
    assert backend.search_entered.wait(timeout=1)

    provider.shutdown()
    assert provider._deferred_backend_close is True
    assert provider._profile_lock is not None

    backend.allow_search.set()
    assert backend.close_entered.wait(timeout=1)
    contender = _ProfileWriterLock(tmp_path / "reme" / ".writer.lock")
    with pytest.raises(OSError, match="already owned|temporarily unavailable|resource busy"):
        contender.acquire()

    backend.allow_close.set()
    recall.join(timeout=2)
    assert recall.is_alive() is False
    assert provider._deferred_backend_close is False
    assert provider._profile_lock is None
    assert backend.close_calls == 1

    contender.acquire()
    contender.release()


def test_disable_without_profile_keeps_profile_lock_during_backend_close(monkeypatch, tmp_path):
    """Disabling a provider cannot expose its profile while close is pending."""

    class BlockingBackend(FakeBackend):
        """Backend double that blocks close until the lock assertion completes."""

        def __init__(self):
            super().__init__()
            self.close_entered = threading.Event()
            self.allow_close = threading.Event()

        def close(self, *, timeout):
            del timeout
            self.close_entered.set()
            assert self.allow_close.wait(timeout=2)
            super().close(timeout=0)

    backend = BlockingBackend()
    _use_fake_http_provider(monkeypatch, backend)
    provider = ReMeMemoryProvider()
    provider.initialize("session", hermes_home=tmp_path, agent_identity="profile")

    disable = threading.Thread(target=provider.initialize, args=("unscoped-session",))
    disable.start()
    assert backend.close_entered.wait(timeout=1)

    contender = _ProfileWriterLock(tmp_path / "reme" / ".writer.lock")
    with pytest.raises(OSError, match="already owned|temporarily unavailable|resource busy"):
        contender.acquire()

    backend.allow_close.set()
    disable.join(timeout=2)
    assert disable.is_alive() is False
    assert provider._profile_lock is None
    assert backend.close_calls == 1
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
    assert not list(workspace.iterdir())


def test_owner_marker_recovers_verified_crash_staging_file(tmp_path):
    """A valid private marker staging file is safe to remove and republish."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    profile = tmp_path / "profile"
    encoded = (
        json.dumps(
            {
                "format": 1,
                "owner": str(profile.resolve()),
                "workspace": str(workspace),
            },
            ensure_ascii=True,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    fd, temp_name = tempfile.mkstemp(prefix=_WORKSPACE_OWNER_TEMP_PREFIX, dir=workspace)
    with os.fdopen(fd, "wb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())

    staging = Path(temp_name)
    assert staging.is_file()
    marker = _WorkspaceOwnerMarker(workspace, profile)
    marker.acquire()

    assert not staging.exists()
    assert marker.path.is_file()
    marker.release()


@pytest.mark.parametrize(
    ("mode", "accepted"),
    [(0o444, True), (0o600, True), (0o666, True), (0o644, False)],
)
@pytest.mark.skipif(os.name != "posix", reason="simulated Windows mode bits require POSIX chmod semantics")
def test_owner_marker_windows_staging_modes_remain_fail_closed(monkeypatch, tmp_path, mode, accepted):
    """Windows' normal/read-only modes are accepted while unknown modes fail closed."""
    monkeypatch.setattr("hermes_agent._is_windows_platform", lambda: True)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    profile = tmp_path / "profile"
    encoded = (
        json.dumps(
            {
                "format": 1,
                "owner": str(profile.resolve()),
                "workspace": str(workspace),
            },
            ensure_ascii=True,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    fd, temp_name = tempfile.mkstemp(prefix=_WORKSPACE_OWNER_TEMP_PREFIX, dir=workspace)
    with os.fdopen(fd, "wb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    staging = Path(temp_name)
    os.chmod(staging, mode)

    marker = _WorkspaceOwnerMarker(workspace, profile)
    if accepted:
        marker.acquire()
        assert not staging.exists()
        assert marker.path.is_file()
        marker.release()
    else:
        with pytest.raises(OSError, match="ambiguous"):
            marker.acquire()
        assert staging.exists()
        assert not marker.path.exists()


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows filesystem mode semantics")
def test_owner_marker_native_windows_staging_recovery(tmp_path):
    """Native Windows recovery publishes only an exact, durable marker staging file."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    profile = tmp_path / "profile"

    def write_staging(path, payload):
        fd, temp_name = tempfile.mkstemp(prefix=_WORKSPACE_OWNER_TEMP_PREFIX, dir=path)
        try:
            with os.fdopen(fd, "wb") as handle:
                fd = -1
                if hasattr(os, "fchmod"):
                    try:
                        os.fchmod(handle.fileno(), 0o600)
                    except (NotImplementedError, OSError):
                        pass
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            if fd >= 0:
                os.close(fd)
        return Path(temp_name)

    encoded = (
        json.dumps(
            {
                "format": 1,
                "owner": str(profile.resolve()),
                "workspace": str(workspace),
            },
            ensure_ascii=True,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    staging = write_staging(workspace, encoded)

    marker = _WorkspaceOwnerMarker(workspace, profile)
    marker.acquire()

    assert not staging.exists()
    assert marker.path.is_file()
    marker.release()

    corrupt_workspace = tmp_path / "corrupt-workspace"
    corrupt_workspace.mkdir()
    corrupt_staging = write_staging(corrupt_workspace, b"corrupt owner marker")
    with pytest.raises(OSError, match="ambiguous"):
        _WorkspaceOwnerMarker(corrupt_workspace, profile).acquire()
    assert corrupt_staging.exists()
    assert not (corrupt_workspace / ".hermes-reme-owner.json").exists()

    foreign_workspace = tmp_path / "foreign-workspace"
    foreign_workspace.mkdir()
    foreign_file = foreign_workspace / "user-data"
    foreign_file.write_bytes(b"do not remove")
    with pytest.raises(OSError, match="existing workspace"):
        _WorkspaceOwnerMarker(foreign_workspace, profile).acquire()
    assert foreign_file.exists()
    assert not (foreign_workspace / ".hermes-reme-owner.json").exists()


def test_owner_marker_keeps_posix_staging_mode_strict(monkeypatch, tmp_path):
    """POSIX recovery still rejects a staging file that is not exactly 0600."""
    monkeypatch.setattr("hermes_agent._is_windows_platform", lambda: False)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    profile = tmp_path / "profile"
    encoded = (
        json.dumps(
            {
                "format": 1,
                "owner": str(profile.resolve()),
                "workspace": str(workspace),
            },
            ensure_ascii=True,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    fd, temp_name = tempfile.mkstemp(prefix=_WORKSPACE_OWNER_TEMP_PREFIX, dir=workspace)
    with os.fdopen(fd, "wb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    staging = Path(temp_name)
    os.chmod(staging, 0o644)

    with pytest.raises(OSError, match="ambiguous"):
        _WorkspaceOwnerMarker(workspace, profile).acquire()

    assert staging.exists()
    assert not (workspace / ".hermes-reme-owner.json").exists()


@pytest.mark.parametrize(
    "kind",
    ["foreign-file", "foreign-symlink", "ambiguous-staging", "staging-symlink"],
)
def test_owner_marker_fails_closed_on_unknown_workspace_entries(tmp_path, kind):
    """Only verified crash staging files may be removed during an unowned claim."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    if kind == "foreign-file":
        entry = workspace / "user-data"
        entry.write_text("do not remove", encoding="utf-8")
    elif kind == "foreign-symlink":
        target = tmp_path / "outside"
        target.write_text("do not read", encoding="utf-8")
        entry = workspace / "user-link"
        entry.symlink_to(target)
    elif kind == "ambiguous-staging":
        entry = workspace / f"{_WORKSPACE_OWNER_TEMP_PREFIX}foreign"
        entry.write_bytes(b"not a marker")
    else:
        target = tmp_path / "staging-target"
        target.write_text("not a marker", encoding="utf-8")
        entry = workspace / f"{_WORKSPACE_OWNER_TEMP_PREFIX}foreign"
        entry.symlink_to(target)

    with pytest.raises(OSError, match="existing workspace|ambiguous"):
        _WorkspaceOwnerMarker(workspace, tmp_path / "profile").acquire()

    assert entry.exists() or entry.is_symlink()
    assert not (workspace / ".hermes-reme-owner.json").exists()
