"""D16 bounded backend ownership and cleanup tests."""

# These tests intentionally inspect private lifecycle seams and use compact helpers.
# pylint: disable=missing-class-docstring,missing-function-docstring,protected-access

from __future__ import annotations

import os
import threading
import time

import pytest

from .test_hermes_agent_integration import (
    EMBEDDED_MODULE,
    HTTP_BACKEND_MODULE,
    PLUGIN_MODULE,
    ReMeBackendError,
    ReMeConfig,
    ReMeConfigError,
    ReMeMemoryProvider,
)
from .hermes_agent_test_support import FakeBackend


class _CloseFailureBackend(FakeBackend):
    def __init__(self, failures: int = 1, error_type: type[Exception] = ReMeBackendError):
        super().__init__()
        self.close_calls = 0
        self._failures = failures
        self._error_type = error_type

    def close(self, *, timeout):
        del timeout
        self.close_calls += 1
        if self.close_calls <= self._failures:
            raise self._error_type("cleanup is still pending")
        self.closed = True


class _StartupFailureBackend(_CloseFailureBackend):
    def start(self, *, deadline=None):
        del deadline
        raise RuntimeError("startup failed")


def _live_embedded_backend(tmp_path):
    backend = EMBEDDED_MODULE.EmbeddedReMeBackend(str(tmp_path), start_timeout=1)
    backend._state = EMBEDDED_MODULE._State.STARTING
    thread = threading.Thread(target=backend._run_loop, name="d16-test-loop", daemon=True)
    backend._thread = thread
    thread.start()
    assert backend._loop_ready.wait(timeout=1)
    with backend._state_lock:
        backend._state = EMBEDDED_MODULE._State.RUNNING
    return backend, thread


def _stop_test_backend(backend, thread):
    try:
        backend.close(timeout=1)
    except Exception:
        pass
    with backend._state_lock:
        loop = backend._loop
    if loop is not None and loop.is_running():
        loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=2)
    assert not thread.is_alive()


def test_d16_r01_real_blocked_close_retains_owner_and_explicit_retry_stops_loop(tmp_path):
    backend, thread = _live_embedded_backend(tmp_path)
    provider = ReMeMemoryProvider()
    provider._backend = backend
    provider._backend_available = True
    provider._config = ReMeConfig()
    provider._shutdown_timeout = 0.02
    try:
        backend._operation_lock.acquire()
        try:
            provider.shutdown()
            assert provider._backend is backend
            assert provider._deferred_backend_close is True
            assert thread.is_alive()
        finally:
            backend._operation_lock.release()
        provider.shutdown()
        assert provider._backend is None
        assert provider._deferred_backend_close is False
        assert not thread.is_alive()
    finally:
        if backend._operation_lock.locked():
            backend._operation_lock.release()
        _stop_test_backend(backend, thread)


@pytest.mark.parametrize("error_type", [ReMeBackendError, OSError, RuntimeError])
def test_d16_r02_r03_failed_start_retains_identity_until_close_succeeds(monkeypatch, error_type):
    failed = _StartupFailureBackend(failures=2, error_type=error_type)
    created = []

    def factory(_config):
        if not created:
            created.append(failed)
            return failed
        backend = FakeBackend()
        created.append(backend)
        return backend

    monkeypatch.setattr(PLUGIN_MODULE, "_backend_for", factory)
    provider = ReMeMemoryProvider()
    provider._config = ReMeConfig()
    provider._shutdown_timeout = 0

    try:
        assert provider._ensure_backend(force=True) is False
        assert provider._backend is failed
        assert failed.close_calls == 1
        assert provider._ensure_backend(force=True) is False
        assert provider._backend is failed
        assert len(created) == 1
        provider._shutdown_timeout = 1
        assert provider._ensure_backend(force=True) is True
        assert provider._backend is created[1]
    finally:
        provider._shutdown_timeout = 1
        provider.shutdown()


def test_d16_r04_profile_switch_and_invalid_config_keep_profile_a_until_cleanup(monkeypatch, tmp_path):
    backend = _CloseFailureBackend()
    config = ReMeConfig()
    outcomes = iter([config, ReMeConfigError("broken profile config"), ReMeConfig()])
    monkeypatch.setattr(PLUGIN_MODULE, "_backend_for", lambda _config: backend)

    def load_test_config(_home=None):
        value = next(outcomes)
        if isinstance(value, ReMeConfigError):
            raise value
        return value

    monkeypatch.setattr(PLUGIN_MODULE, "load_config", load_test_config)
    provider = ReMeMemoryProvider()
    profile_a = tmp_path / "profile-a"
    profile_b = tmp_path / "profile-b"
    provider.initialize("a", hermes_home=profile_a, agent_identity="A")
    original_lock = provider._profile_lock

    provider.initialize("b", hermes_home=profile_b, agent_identity="B")
    assert provider._profile_root == profile_a.resolve()
    assert provider._profile_id == "A"
    assert provider._profile_lock is original_lock
    assert provider._backend is backend
    assert backend.close_calls == 1

    provider.initialize("b", hermes_home=profile_b, agent_identity="B")
    assert provider._profile_root == profile_b.resolve()
    assert provider._profile_id == "B"
    assert provider._backend is backend
    provider.shutdown()


def test_d16_r05_partial_cleanup_retry_is_same_backend_and_idempotent():
    backend = _CloseFailureBackend()
    provider = ReMeMemoryProvider()
    provider._backend = backend
    provider._backend_available = True

    assert provider._close_backend_locked(timeout=0) is False
    assert provider._backend is backend
    assert provider._close_backend_locked(timeout=0) is True
    assert provider._backend is None
    assert backend.close_calls == 2
    assert provider._close_backend_locked(timeout=0) is True
    assert backend.close_calls == 2


def test_d16_r06_health_failure_reuses_open_backend_and_http_has_no_local_loop(monkeypatch):
    backend = FakeBackend()
    calls = []

    def health(*, timeout):
        del timeout
        calls.append(1)
        if len(calls) == 1:
            raise ReMeBackendError("temporarily unhealthy")
        return {"success": True, "metadata": {"health": {"healthy": True}}}

    backend.health = health
    created = []
    monkeypatch.setattr(PLUGIN_MODULE, "_backend_for", lambda _config: created.append(backend) or backend)
    provider = ReMeMemoryProvider()
    provider._config = ReMeConfig()
    assert provider._ensure_backend(force=True) is False
    assert provider._ensure_backend(force=True) is True
    assert created == [backend]
    provider.shutdown()

    http = HTTP_BACKEND_MODULE.HttpReMeBackend("http://127.0.0.1:1", request_timeout=0.01)
    assert not hasattr(http, "_thread")
    http.close(timeout=0)


def test_d16_r07_shutdown_does_not_ack_or_release_profile_before_writer_and_close(monkeypatch, tmp_path):
    backend = _CloseFailureBackend()
    monkeypatch.setattr(PLUGIN_MODULE, "load_config", lambda _home=None: ReMeConfig())
    monkeypatch.setattr(PLUGIN_MODULE, "_backend_for", lambda _config: backend)
    provider = ReMeMemoryProvider()
    provider.initialize("session", hermes_home=tmp_path, agent_identity="profile")
    provider._shutdown_timeout = 0
    provider.sync_turn("durable", "turn")
    deadline = time.monotonic() + 1
    while provider._write_thread is not None and time.monotonic() < deadline:
        time.sleep(0.005)
    try:
        provider.shutdown()
        assert provider._backend is backend
        assert provider._profile_lock is not None
        assert provider._pending_path is not None
        assert provider._pending_path.exists()
        backend._failures = 0
        provider._shutdown_timeout = 1
        provider.shutdown()
        assert provider._backend is None
        assert provider._profile_lock is None
    finally:
        backend._failures = 0
        provider._shutdown_timeout = 1
        provider.shutdown()


def test_d16_r08_bounded_repeated_close_cycles_leave_no_loop_threads(tmp_path):
    baseline = {thread.ident for thread in threading.enumerate() if thread.ident}
    fd_path = "/proc/self/fd"
    baseline_fds = len(os.listdir(fd_path)) if os.path.isdir(fd_path) else None
    for index in range(50):
        backend = EMBEDDED_MODULE.EmbeddedReMeBackend(str(tmp_path / str(index)), start_timeout=1)
        backend._state = EMBEDDED_MODULE._State.STARTING
        thread = threading.Thread(target=backend._run_loop, name=f"d16-cycle-{index}", daemon=True)
        backend._thread = thread
        try:
            thread.start()
            assert backend._loop_ready.wait(timeout=1)
            backend._operation_lock.acquire()
            try:
                with pytest.raises(ReMeBackendError, match="Timed out waiting"):
                    backend.close(timeout=0)
                assert thread.is_alive()
            finally:
                backend._operation_lock.release()
            backend.close(timeout=1)
            thread.join(timeout=2)
            assert not thread.is_alive()
        finally:
            if backend._operation_lock.locked():
                backend._operation_lock.release()
            _stop_test_backend(backend, thread)
        if baseline_fds is not None:
            assert len(os.listdir(fd_path)) <= baseline_fds + 8
    assert {thread.ident for thread in threading.enumerate() if thread.ident} <= baseline
