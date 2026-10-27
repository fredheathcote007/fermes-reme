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
    _FakeBackend,
    config_path,
)


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
spec = importlib.util.spec_from_file_location("hermes_agent_test_support", test_path)
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
