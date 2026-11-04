"""Focused recovery and error-handling tests for the external Hermes provider."""

# pylint: disable=missing-class-docstring,missing-function-docstring,protected-access,redefined-outer-name

from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from .test_hermes_agent_integration import (
    CLIENT_MODULE,
    PLUGIN_MODULE,
    ReMeBackendError,
    ReMeConfig,
    ReMeHttpClient,
    ReMeMemoryProvider,
    ReMeServiceError,
    FakeBackend as _FakeBackend,
    config_path,
)


@pytest.mark.parametrize(
    "detail",
    [
        "connection reset by foreign host",
        "model alias superseded, please retry",
        "source provenance unavailable: backend is temporarily unavailable",
    ],
)
def test_transient_text_is_not_a_permanent_write_refusal(detail):
    assert PLUGIN_MODULE._is_permanent_write_failure(ReMeBackendError(detail)) is False


@pytest.mark.parametrize(
    "detail",
    [
        "existing protected note belongs to a different source session",
        "protected marker source provenance is not an append-only extension",
        "existing protected note is unreadable",
        "protected daily note is unreadable",
        "protected receipt is unreadable",
    ],
)
def test_protected_identity_mismatch_remains_permanent(detail):
    assert PLUGIN_MODULE._is_permanent_write_failure(ReMeBackendError(detail)) is True


@pytest.mark.parametrize(
    ("detail", "permanent"),
    [
        ("protected receipt is malformed", True),
        ("protected receipt response is malformed", True),
        ("protected receipt does not contain a successful commit", True),
        ("protected no-op receipt contains commit evidence", True),
        ("protected no-op receipt response is malformed", True),
        ("protected prior commit receipt is malformed", True),
        ("protected receipt storage error: [Errno 13] Permission denied", False),
        ("protected receipt storage error: [Errno 24] Too many open files", False),
        ("protected prior commit receipt storage error", False),
    ],
)
def test_receipt_corruption_is_permanent_but_storage_errors_retry(detail, permanent):
    assert PLUGIN_MODULE._is_permanent_write_failure(ReMeBackendError(detail)) is permanent


@pytest.mark.parametrize(
    ("response", "message"),
    [
        ({"success": False, "error": "memory extraction exploded"}, "memory extraction exploded"),
        (
            {"success": False, "error": {"message": "memory extraction exploded"}},
            "memory extraction exploded",
        ),
        ({"success": False, "error": {}}, "ReMe action did not report success"),
        ({"success": False, "error": ""}, "ReMe action did not report success"),
        (
            {"success": False, "error": {"secret": "must not leak"}},
            "ReMe action did not report success",
        ),
        ({"status": "error"}, "ReMe action did not report success"),
    ],
)
def test_http_client_propagates_error_only_result_detail(monkeypatch, response, message):
    class ErrorResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return False

        @staticmethod
        def read():
            return json.dumps(response).encode()

    monkeypatch.setattr(CLIENT_MODULE.urllib.request, "urlopen", lambda *args, **kwargs: ErrorResponse())

    with pytest.raises(ReMeServiceError, match=message) as exc_info:
        ReMeHttpClient("http://127.0.0.1:2333", timeout=1).call("auto_memory")
    assert "must not leak" not in str(exc_info.value)


def test_interrupted_inflight_write_keeps_spool_record(monkeypatch, tmp_path):
    entered = threading.Event()
    release = threading.Event()

    class InterruptedBackend(_FakeBackend):
        def auto_memory(self, session_id, messages, *, timeout, request_id=None):
            del timeout
            self.writes.append((session_id, messages))
            self.request_ids.append(request_id)
            entered.set()
            release.wait(timeout=2)
            raise ReMeBackendError("interrupted write")

    backend = InterruptedBackend()
    monkeypatch.setattr(PLUGIN_MODULE, "load_config", lambda home=None: ReMeConfig())
    monkeypatch.setattr(PLUGIN_MODULE, "_backend_for", lambda config: backend)
    provider = ReMeMemoryProvider()
    provider.initialize("session", hermes_home=tmp_path, agent_identity="profile")
    provider._shutdown_timeout = 0.01
    provider.sync_turn("interrupted user", "assistant")
    assert entered.wait(timeout=1)

    provider.shutdown()
    pending = config_path(tmp_path).parent / "pending-writes.jsonl"
    assert pending.read_text(encoding="utf-8").strip()

    release.set()
    if provider._write_thread is not None:
        provider._write_thread.join(timeout=2)
    assert provider._write_thread is None or not provider._write_thread.is_alive()


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX process wait-status semantics")
def test_sigkill_during_backend_execution_replays_record(monkeypatch, tmp_path):
    ready_read, ready_write = os.pipe()
    recovered = _FakeBackend()

    monkeypatch.setattr(PLUGIN_MODULE, "load_config", lambda home=None: ReMeConfig())
    monkeypatch.setattr(PLUGIN_MODULE, "_backend_for", lambda config: recovered)

    child_code = r"""
import importlib.util
import os
import signal
import sys
import time

test_path, ready_fd, hermes_home = sys.argv[1], int(sys.argv[2]), sys.argv[3]
spec = importlib.util.spec_from_file_location("hermes_agent_integration_subprocess", test_path)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = module
spec.loader.exec_module(module)

class CrashBackend:
    label = "crash-test"

    def start(self):
        return None

    def health(self, *, timeout):
        del timeout
        return {"success": True, "metadata": {"health": {"healthy": True}}}

    def auto_memory(self, session_id, messages, *, timeout, request_id=None):
        del session_id, messages, timeout, request_id
        os.write(ready_fd, b"1")
        os.kill(os.getpid(), signal.SIGKILL)

    def close(self, *, timeout):
        del timeout

module.PLUGIN_MODULE.load_config = lambda home=None: module.ReMeConfig()
module.PLUGIN_MODULE._backend_for = lambda config: CrashBackend()
provider = module.ReMeMemoryProvider()
provider.initialize("crashed-session", hermes_home=hermes_home, agent_identity="profile")
provider.sync_turn("crashed user", "assistant")
time.sleep(3)
os._exit(124)
"""

    support_path = Path(__file__).with_name("test_hermes_agent_integration.py")
    try:
        try:
            with subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    child_code,
                    str(support_path),
                    str(ready_write),
                    str(tmp_path),
                ],
                cwd=str(Path(__file__).resolve().parents[2]),
                pass_fds=(ready_write,),
            ) as child:
                try:
                    readable, _, _ = select.select([ready_read], [], [], 3)
                    if not readable:
                        try:
                            child.kill()
                        except ProcessLookupError:
                            pass
                    ready = os.read(ready_read, 1) if readable else b""
                    _, status = os.waitpid(child.pid, 0)
                    child.returncode = os.waitstatus_to_exitcode(status)
                    if not readable:
                        pytest.fail("crash child did not reach the backend before its deadline")
                finally:
                    if child.poll() is None:
                        child.kill()
        finally:
            os.close(ready_write)
    finally:
        os.close(ready_read)

    assert ready == b"1"
    assert os.WIFSIGNALED(status)
    assert os.WTERMSIG(status) == signal.SIGKILL
    pending = config_path(tmp_path).parent / "pending-writes.jsonl"
    assert pending.is_file() and pending.read_text(encoding="utf-8").strip()
    record = json.loads(pending.read_text(encoding="utf-8").splitlines()[0])

    restarted = ReMeMemoryProvider()
    restarted.initialize("restarted-session", hermes_home=tmp_path, agent_identity="profile")
    try:
        deadline = time.monotonic() + 2
        while pending.read_text(encoding="utf-8").strip() and time.monotonic() < deadline:
            time.sleep(0.005)
    finally:
        restarted.shutdown()

    assert pending.read_text(encoding="utf-8") == ""
    assert recovered.request_ids == [record["id"]]
    assert recovered.writes[0][1][0]["content"] == "crashed user"


def test_parallel_profile_workers_keep_spools_and_sessions_isolated(monkeypatch, tmp_path):
    backends = {}
    backends_lock = threading.Lock()

    def load_worker_config(home=None):
        return ReMeConfig(endpoint=f"worker-{Path(home).name}")

    def backend_for(config):
        with backends_lock:
            return backends.setdefault(config.endpoint, _FakeBackend())

    monkeypatch.setattr(PLUGIN_MODULE, "load_config", load_worker_config)
    monkeypatch.setattr(PLUGIN_MODULE, "_backend_for", backend_for)

    def run_worker(index):
        home = tmp_path / f"worker-{index}"
        provider = ReMeMemoryProvider()
        provider.initialize(f"session-{index}", hermes_home=home, agent_identity=f"profile-{index}")
        provider.sync_turn(f"worker-{index} user", "assistant")
        deadline = time.monotonic() + 1
        backend = backends[f"worker-{home.name}"]
        while not backend.writes and time.monotonic() < deadline:
            time.sleep(0.005)
        pending = config_path(home).parent / "pending-writes.jsonl"
        provider.shutdown()
        return backend, pending

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(run_worker, (1, 2)))

    for index, (backend, pending) in zip((1, 2), results):
        assert len(backend.writes) == 1
        assert backend.writes[0][0].startswith(f"hermes-profile-{index}-session-{index}-")
        assert backend.writes[0][1][0]["content"] == f"worker-{index} user"
        assert backend.writes[0][1][0]["id"] != backend.writes[0][1][1]["id"]
        assert pending.read_text(encoding="utf-8") == ""


@pytest.mark.parametrize("response", [{"error": "memory extraction exploded"}, {"status": "error"}])
def test_unstructured_auto_memory_error_keeps_spool_record(monkeypatch, tmp_path, response):
    backend = _FakeBackend()
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


def test_permanent_refusal_is_quarantined_instead_of_retried_forever(monkeypatch, tmp_path):
    backend = _FakeBackend()
    backend.auto_memory = lambda *args, **kwargs: (_ for _ in ()).throw(
        ReMeBackendError("Error: protected note marker is contradictory"),
    )
    monkeypatch.setattr(PLUGIN_MODULE, "load_config", lambda home=None: ReMeConfig(health_retry_seconds=0.01))
    monkeypatch.setattr(PLUGIN_MODULE, "_backend_for", lambda config: backend)

    provider = ReMeMemoryProvider()
    provider.initialize("session", hermes_home=tmp_path, agent_identity="profile")
    provider.sync_turn("refused user", "assistant")
    quarantine = config_path(tmp_path).parent / "quarantined-writes.jsonl"
    deadline = time.monotonic() + 2
    while not quarantine.exists() and time.monotonic() < deadline:
        time.sleep(0.005)
    provider.shutdown()

    pending = config_path(tmp_path).parent / "pending-writes.jsonl"
    assert quarantine.is_file()
    assert json.loads(quarantine.read_text(encoding="utf-8").splitlines()[0])["reason"] == "permanent_refusal"
    assert pending.read_text(encoding="utf-8") == ""


def test_permanent_refusal_does_not_cooldown_following_record(monkeypatch, tmp_path):
    backend = _FakeBackend()
    calls = []

    def auto_memory(session_id, messages, *, timeout, request_id=None):
        del session_id, timeout
        calls.append((request_id, messages[0]["content"]))
        if len(calls) == 1:
            raise ReMeBackendError("Error: protected note marker is contradictory")
        return {"success": True}

    backend.auto_memory = auto_memory
    monkeypatch.setattr(PLUGIN_MODULE, "load_config", lambda home=None: ReMeConfig(health_retry_seconds=60))
    monkeypatch.setattr(PLUGIN_MODULE, "_backend_for", lambda config: backend)

    provider = ReMeMemoryProvider()
    provider.initialize("session", hermes_home=tmp_path, agent_identity="profile")
    updated = []
    original_update = provider._update_pending_write

    def capture_update(record):
        updated.append((record["id"], record["attempts"]))
        return original_update(record)

    monkeypatch.setattr(provider, "_update_pending_write", capture_update)
    provider.sync_turn("refused", "assistant")
    provider.sync_turn("valid", "assistant")
    deadline = time.monotonic() + 1
    while len(calls) < 2 and time.monotonic() < deadline:
        time.sleep(0.005)
    provider.shutdown()

    assert [content for _request_id, content in calls] == ["refused", "valid"]
    assert not updated
    assert provider._next_write_attempt == 0.0
    assert provider._read_pending_writes() == []
    quarantine = config_path(tmp_path).parent / "quarantined-writes.jsonl"
    assert len(quarantine.read_text(encoding="utf-8").splitlines()) == 1


def test_retryable_error_prefix_remains_durable_and_is_not_quarantined(monkeypatch, tmp_path):
    backend = _FakeBackend()
    retry_invocations = []
    retry_started = threading.Event()

    def retryable_auto_memory(*args, **kwargs):
        del args, kwargs
        retry_invocations.append(None)
        retry_started.set()
        raise ReMeBackendError("Error: durable memory write failed; request remains eligible for retry")

    backend.auto_memory = retryable_auto_memory
    monkeypatch.setattr(PLUGIN_MODULE, "load_config", lambda home=None: ReMeConfig(health_retry_seconds=0.01))
    monkeypatch.setattr(PLUGIN_MODULE, "_backend_for", lambda config: backend)

    provider = ReMeMemoryProvider()
    provider.initialize("session", hermes_home=tmp_path, agent_identity="profile")
    provider.sync_turn("retryable user", "assistant")
    deadline = time.monotonic() + 0.3
    retry_started.wait(timeout=max(0, deadline - time.monotonic()))
    while len(retry_invocations) < 2 and time.monotonic() < deadline:
        time.sleep(0.005)
    provider.shutdown()

    pending = config_path(tmp_path).parent / "pending-writes.jsonl"
    quarantine = config_path(tmp_path).parent / "quarantined-writes.jsonl"
    assert len(retry_invocations) >= 2
    assert pending.read_text(encoding="utf-8").strip()
    assert not quarantine.exists()
