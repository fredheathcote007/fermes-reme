"""Focused tests for the external Hermes memory-provider plugin."""

# pylint: disable=missing-class-docstring,missing-function-docstring
# pylint: disable=protected-access,redefined-outer-name,wrong-import-position,unused-import

from __future__ import annotations

import sys
import types
import asyncio
import contextvars
import http.server
import importlib
import json
import os
import stat
import threading
import time

from dataclasses import dataclass
from pathlib import Path

import pytest

_PLUGIN_PARENT = Path(__file__).resolve().parents[2] / "integrations"
if str(_PLUGIN_PARENT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_PARENT))


try:
    import agent.memory_provider  # type: ignore[import-not-found]  # noqa: F401
except ImportError:
    agent_module = types.ModuleType("agent")
    memory_provider_module = types.ModuleType("agent.memory_provider")

    class MemoryProvider:
        """Minimal Hermes contract used when hermes-agent is not installed."""

    @dataclass(frozen=True)
    class RecallStatus:
        provider_label: str
        count: int
        glyph: str = "🧠"

    memory_provider_module.MemoryProvider = MemoryProvider
    memory_provider_module.RecallStatus = RecallStatus
    agent_module.memory_provider = memory_provider_module
    sys.modules["agent"] = agent_module
    sys.modules["agent.memory_provider"] = memory_provider_module

PLUGIN_MODULE = importlib.import_module("hermes_agent")
BACKEND_MODULE = importlib.import_module("hermes_agent.backend")
CONFIG_MODULE = importlib.import_module("hermes_agent.config")
EMBEDDED_MODULE = importlib.import_module("hermes_agent.embedded_backend")
HTTP_BACKEND_MODULE = importlib.import_module("hermes_agent.http_backend")
CLIENT_MODULE = importlib.import_module("hermes_agent.client")

ReMeMemoryProvider = PLUGIN_MODULE.ReMeMemoryProvider
ProfileWriterLock = PLUGIN_MODULE._ProfileWriterLock
WorkspaceOwnerMarker = PLUGIN_MODULE._WorkspaceOwnerMarker
ReMeBackendError = BACKEND_MODULE.ReMeBackendError
ReMeConfig = CONFIG_MODULE.ReMeConfig
ReMeConfigError = CONFIG_MODULE.ReMeConfigError
config_path = CONFIG_MODULE.config_path
load_config = CONFIG_MODULE.load_config
parse_config = CONFIG_MODULE.parse_config
save_config = CONFIG_MODULE.save_config
EmbeddedReMeBackend = EMBEDDED_MODULE.EmbeddedReMeBackend
HttpReMeBackend = HTTP_BACKEND_MODULE.HttpReMeBackend
ReMeHttpClient = CLIENT_MODULE.ReMeHttpClient
ReMeServiceError = CLIENT_MODULE.ReMeServiceError
scoped_session_id = PLUGIN_MODULE._scoped_session_id


@pytest.fixture
def reme_http_server():
    requests = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            requests.append((self.path, json.loads(body)))
            action = self.path.rsplit("/", 1)[-1]

            if action == "slow":
                time.sleep(0.3)
            if action == "server_error":
                self.send_response(503)
                self.end_headers()
                self.wfile.write(b"temporarily unavailable")
                return
            if action == "invalid_json":
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"not-json")
                return

            responses = {
                "health_check": {"success": True, "metadata": {"health": {"healthy": True}}},
                "unhealthy": {"success": True, "metadata": {"health": {"healthy": False}}},
                "failed": {"success": False, "answer": "search failed"},
                "list": [],
                "search": {"success": True, "answer": "remembered", "metadata": {}},
                "auto_memory": {"success": True, "answer": "recorded", "metadata": {}},
                "slow": {"success": True, "answer": "late", "metadata": {}},
            }
            payload = json.dumps(responses[action]).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            try:
                self.wfile.write(payload)
            except BrokenPipeError:
                pass

        def log_message(self, _format, *_args):
            return

    class Server(http.server.ThreadingHTTPServer):
        daemon_threads = True

    server = Server(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_config_defaults_to_http(tmp_path):
    config = load_config(tmp_path)

    assert config.mode == "http"
    assert config.endpoint == "http://127.0.0.1:2333"


def test_embedded_config_defaults_to_stage1_and_rejects_default(tmp_path):
    config = parse_config(
        {"mode": "embedded", "workspace_dir": str(tmp_path / "workspace")},
        hermes_home=tmp_path,
    )

    assert config.reme_config == "fermes-hermes-stage1"
    with pytest.raises(ReMeConfigError, match="fermes-hermes-stage1"):
        parse_config(
            {
                "mode": "embedded",
                "workspace_dir": str(tmp_path / "other"),
                "reme_config": "default",
            },
            hermes_home=tmp_path,
        )


def test_embedded_workspace_rejects_traversal_and_symlink(tmp_path):
    with pytest.raises(ReMeConfigError, match="traversal"):
        parse_config(
            {"mode": "embedded", "workspace_dir": str(tmp_path / "a" / ".." / "b")},
            hermes_home=tmp_path,
        )

    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(ReMeConfigError, match="symlinks"):
        parse_config(
            {"mode": "embedded", "workspace_dir": str(link)},
            hermes_home=tmp_path,
        )


def test_provider_schema_exposes_mode_specific_and_advanced_fields():
    fields = {field["key"]: field for field in ReMeMemoryProvider().get_config_schema()}

    assert set(fields) == {
        "mode",
        "endpoint",
        "workspace_dir",
        "reme_config",
        "recall_limit",
        "recall_timeout",
        "request_timeout",
        "health_timeout",
        "health_retry_seconds",
        "shutdown_timeout",
    }
    assert fields["endpoint"]["when"] == {"mode": "http"}
    assert fields["workspace_dir"]["when"] == {"mode": "embedded"}


def test_current_config_precedes_legacy(tmp_path):
    (tmp_path / "reme.json").write_text(
        '{"endpoint": "http://legacy:1"}',
        encoding="utf-8",
    )
    current = config_path(tmp_path)
    current.parent.mkdir()
    current.write_text('{"endpoint": "http://current:2"}', encoding="utf-8")

    assert load_config(tmp_path).endpoint == "http://current:2"


def test_sparse_dashboard_config_inherits_legacy_values(tmp_path):
    (tmp_path / "reme.json").write_text(
        '{"endpoint": "http://legacy:2444", "recall_limit": 3}',
        encoding="utf-8",
    )
    current = config_path(tmp_path)
    current.parent.mkdir()
    current.write_text('{"recall_limit": 7}', encoding="utf-8")

    config = load_config(tmp_path)

    assert config.endpoint == "http://legacy:2444"
    assert config.recall_limit == 7


def test_embedded_config_normalizes_workspace(tmp_path):
    config = parse_config(
        {"mode": " EMBEDDED ", "workspace_dir": str(tmp_path / "workspace")},
        hermes_home=tmp_path,
    )

    assert config.mode == "embedded"
    assert config.workspace_dir == str((tmp_path / "workspace").absolute())


def test_embedded_config_requires_workspace(tmp_path):
    with pytest.raises(ReMeConfigError, match="workspace_dir"):
        parse_config({"mode": "embedded"}, hermes_home=tmp_path)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_config_rejects_non_finite_timeouts(tmp_path, value):
    with pytest.raises(ReMeConfigError, match="positive number"):
        parse_config({"request_timeout": value}, hermes_home=tmp_path)


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://user:secret@127.0.0.1:2333",
        "http://127.0.0.1:2333?token=secret",
        "http://127.0.0.1:2333#fragment",
        "http://127.0.0.1:invalid",
    ],
)
def test_config_and_client_reject_unsafe_endpoint_shapes(tmp_path, endpoint):
    with pytest.raises(ReMeConfigError, match="absolute http"):
        parse_config({"endpoint": endpoint}, hermes_home=tmp_path)
    with pytest.raises(ValueError, match="absolute http"):
        ReMeHttpClient(endpoint, timeout=1)


def test_http_client_round_trip_sends_json_payload(reme_http_server):
    endpoint, requests = reme_http_server

    result = ReMeHttpClient(endpoint, timeout=1).call(
        "search",
        {"query": "project decision", "limit": 3},
    )

    assert result["answer"] == "remembered"
    assert requests == [("/search", {"query": "project decision", "limit": 3})]


@pytest.mark.parametrize(
    ("action", "message"),
    [
        ("server_error", "HTTP 503"),
        ("invalid_json", "invalid JSON"),
        ("failed", "search failed"),
        ("list", "non-object"),
    ],
)
def test_http_client_rejects_invalid_service_responses(reme_http_server, action, message):
    endpoint, _ = reme_http_server

    with pytest.raises(ReMeServiceError, match=message):
        ReMeHttpClient(endpoint, timeout=1).call(action)


def test_http_client_enforces_request_timeout(reme_http_server):
    endpoint, _ = reme_http_server

    with pytest.raises(ReMeServiceError):
        ReMeHttpClient(endpoint, timeout=1).call("slow", timeout=0.1)


def test_http_backend_requires_semantically_healthy_response(reme_http_server):
    endpoint, _ = reme_http_server
    backend = HttpReMeBackend(endpoint, request_timeout=1)
    backend._client.call = lambda *args, **kwargs: {
        "success": True,
        "metadata": {"health": {"healthy": False}},
    }

    with pytest.raises(ReMeBackendError, match="healthy component snapshot"):
        backend.health(timeout=1)


def test_http_backend_forwards_stable_auto_memory_request_id(reme_http_server):
    endpoint, requests = reme_http_server
    backend = HttpReMeBackend(endpoint, request_timeout=1)

    backend.auto_memory(
        "session",
        [{"role": "user", "content": "hello"}],
        request_id="spool-record-1",
        timeout=1,
    )

    assert requests == [
        (
            "/auto_memory",
            {
                "session_id": "session",
                "messages": [{"role": "user", "content": "hello"}],
                "request_id": "spool-record-1",
            },
        ),
    ]


def test_stage1_config_is_local_only_and_has_no_unattended_jobs(tmp_path):
    from reme.config import resolve_app_config
    from reme.schema import ApplicationConfig

    config = resolve_app_config(
        config="fermes-hermes-stage1",
        workspace_dir=str(tmp_path / "workspace"),
        log_config=False,
    )
    ApplicationConfig(**config)

    assert set(config["jobs"]) == {
        "index_update_loop",
        "health_check",
        "search",
        "auto_memory",
        "daily_list",
        "daily_write",
        "read",
        "write",
        "edit",
        "move",
        "frontmatter_read",
        "frontmatter_update",
        "list_tags",
    }
    assert config["service"]["backend"] == "cli"
    assert config["components"]["file_store"]["default"]["embedding_store"] == ""
    llm = config["components"]["as_llm"]["default"]
    assert llm["backend"] == "openai"
    assert llm["credential"]["base_url"] == "http://127.0.0.1:1234/v1"
    assert llm["model"] == "local-placeholder"
    assert not any(job.get("backend") == "cron" for job in config["jobs"].values())
    assert not any("proactive" in name or "dream" in name for name in config["jobs"])

    # AgentScope's FunctionTool is built from this schema. An omitted schema
    # advertises {} to an OpenAI-compatible model and causes empty tool calls.
    expected_required = {
        "daily_write": {"name", "description", "session_id", "content"},
        "read": {"path"},
        "write": {"path", "name", "description", "content"},
        "edit": {"path", "old", "new"},
        "frontmatter_update": {"path", "metadata"},
        "frontmatter_read": {"path"},
    }
    for name, fields in expected_required.items():
        spec = config["jobs"][name]["parameters"]
        assert spec["type"] == "object"
        assert set(spec["required"]) == fields
        assert fields <= set(spec["properties"])
    optional_tags = config["jobs"]["list_tags"]["parameters"]
    assert optional_tags["type"] == "object"
    assert {"page", "order_by", "order", "page_size"} <= set(optional_tags["properties"])
    assert not optional_tags.get("required")
    upstream = resolve_app_config(config="default", workspace_dir=str(tmp_path / "upstream"), log_config=False)
    for tool_name in ("frontmatter_read", "list_tags"):
        assert config["jobs"][tool_name]["parameters"] == upstream["jobs"][tool_name]["parameters"]


def test_save_config_uses_dashboard_layout_and_private_permissions(tmp_path):
    saved = save_config({"mode": "http", "recall_limit": 7}, tmp_path)
    path = config_path(tmp_path)

    assert saved.recall_limit == 7
    assert path.is_file()
    assert path.stat().st_mode & 0o777 == 0o600


class _FakeResponse:
    def __init__(self, answer="", metadata=None, success=True):
        self.answer = answer
        self.metadata = metadata or {}
        self.success = success

    def model_dump(self):
        return {
            "answer": self.answer,
            "metadata": self.metadata,
            "success": self.success,
        }


class _FakeApplication:
    instances = []

    def __init__(self, **config):
        self.config = config
        self.started = False
        self.closed = False
        self.calls = []
        self.__class__.instances.append(self)

    async def start(self):
        self.started = True

    async def close(self):
        self.closed = True

    async def run_job(self, name, **kwargs):
        self.calls.append((name, kwargs))
        if name == "health_check":
            return _FakeResponse(metadata={"health": {"healthy": True}})
        return _FakeResponse(answer=name, metadata={"counts": {"returned": 1}})


def test_embedded_backend_owns_application_lifecycle(monkeypatch, tmp_path):
    import reme
    import reme.config

    _FakeApplication.instances.clear()
    monkeypatch.setattr(reme, "Application", _FakeApplication)
    monkeypatch.setattr(
        reme.config,
        "resolve_app_config",
        lambda **kwargs: {
            "workspace_dir": kwargs["workspace_dir"],
            "marker": kwargs["config"],
        },
    )
    backend = EmbeddedReMeBackend(str(tmp_path), start_timeout=2)

    backend.start()
    assert backend.health(timeout=2)["success"] is True
    assert backend.search("needle", limit=3, timeout=2)["answer"] == "search"
    backend.auto_memory(
        "session",
        [{"role": "user", "content": "hello"}],
        request_id="record-1",
        timeout=2,
    )
    backend.close(timeout=2)

    app = _FakeApplication.instances[0]
    assert app.started is True
    assert app.closed is True
    assert app.config == {
        "workspace_dir": str(tmp_path),
        "marker": "default",
    }
    assert [name for name, _ in app.calls] == ["health_check", "search", "auto_memory"]
    assert app.calls[-1][1]["request_id"] == "record-1"
    assert backend.state == "closed"


class _FakeBackend:
    label = "fake"

    def __init__(self):
        self.writes = []
        self.request_ids = []
        self.closed = False

    def start(self):
        return None

    def health(self, *, timeout):
        del timeout
        return {"success": True, "metadata": {"health": {"healthy": True}}}

    def search(self, query, *, limit, timeout):
        del query, limit, timeout
        return {
            "success": True,
            "answer": " remembered ",
            "metadata": {"counts": {"returned": 2}},
        }

    def auto_memory(self, session_id, messages, *, timeout, request_id=None):
        del timeout
        self.writes.append((session_id, messages))
        self.request_ids.append(request_id)
        return {"success": True}

    def close(self, *, timeout):
        del timeout
        self.closed = True


def test_provider_selects_backend_recalls_and_writes(monkeypatch, tmp_path):
    backend = _FakeBackend()
    monkeypatch.setattr(PLUGIN_MODULE, "load_config", lambda home=None: ReMeConfig())
    monkeypatch.setattr(PLUGIN_MODULE, "_backend_for", lambda config: backend)
    provider = ReMeMemoryProvider()
    provider.initialize("session/one", hermes_home=tmp_path, agent_identity="work")

    assert provider.prefetch("project decision") == "remembered"
    assert provider.recall_status().count == 2
    provider.sync_turn("hello", "hi")
    provider.shutdown()

    assert len(backend.writes) == 1
    assert backend.writes[0][0].startswith("hermes-work-session-one-")
    assert backend.closed is True


def test_session_scope_distinguishes_profiles_and_ambiguous_names():
    assert scoped_session_id("profile/a", "session") != scoped_session_id(
        "profile-a",
        "session",
    )
    assert scoped_session_id("profile", "session/a") != scoped_session_id(
        "profile",
        "session-a",
    )


def test_profile_writer_lock_is_exclusive_and_releases_without_stale_marker(tmp_path):
    path = tmp_path / "reme" / ".writer.lock"
    first = ProfileWriterLock(path)
    second = ProfileWriterLock(path)

    first.acquire()
    assert path.is_file()
    with pytest.raises(OSError, match="already owned|temporarily unavailable|resource busy"):
        second.acquire()

    first.release()
    second.acquire()
    second.release()
    assert path.is_file()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork for cross-process lock probe")
def test_profile_writer_lock_rejects_other_process(tmp_path):
    path = tmp_path / "reme" / ".writer.lock"
    ready_read, ready_write = os.pipe()
    release_read, release_write = os.pipe()
    pid = os.fork()
    if pid == 0:  # pragma: no cover - child assertions are reported by the parent exit status
        try:
            os.close(ready_read)
            os.close(release_write)
            lock = ProfileWriterLock(path)
            lock.acquire()
            os.write(ready_write, b"1")
            os.read(release_read, 1)
            lock.release()
            os._exit(0)
        except Exception:
            os.write(ready_write, b"0")
            os._exit(1)

    os.close(ready_write)
    os.close(release_read)
    try:
        assert os.read(ready_read, 1) == b"1"
        contender = ProfileWriterLock(path)
        with pytest.raises(OSError):
            contender.acquire()
        os.write(release_write, b"1")
    finally:
        os.close(ready_read)
        os.close(release_write)
        _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 0


def test_workspace_owner_marker_is_private_durable_and_non_rewriting(tmp_path):
    workspace = tmp_path / "workspace"
    profile = tmp_path / "profile"
    marker = WorkspaceOwnerMarker(workspace, profile)
    marker.acquire()
    marker.release()

    owner_path = workspace / PLUGIN_MODULE._WORKSPACE_OWNER_FILENAME
    before = owner_path.read_bytes()
    before_stat = owner_path.stat()
    assert stat.S_IMODE(before_stat.st_mode) == 0o600

    time.sleep(0.01)
    restarted = WorkspaceOwnerMarker(workspace, profile)
    restarted.acquire()
    assert owner_path.read_bytes() == before
    assert owner_path.stat().st_mtime_ns == before_stat.st_mtime_ns
    restarted.release()


def test_workspace_owner_marker_rejects_mixed_owner_and_symlink(tmp_path):
    workspace = tmp_path / "workspace"
    first = WorkspaceOwnerMarker(workspace, tmp_path / "profile-one")
    first.acquire()

    with pytest.raises(OSError, match="another Hermes profile"):
        WorkspaceOwnerMarker(workspace, tmp_path / "profile-two").acquire()

    marker = workspace / PLUGIN_MODULE._WORKSPACE_OWNER_FILENAME
    marker.unlink()
    target = tmp_path / "outside"
    target.write_text("marker-secret", encoding="utf-8")
    marker.symlink_to(target)
    with pytest.raises(OSError, match="regular file|created"):
        WorkspaceOwnerMarker(workspace, tmp_path / "profile-one").acquire()
    assert target.read_text(encoding="utf-8") == "marker-secret"


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork for crash persistence probe")
def test_workspace_owner_marker_survives_crash_and_blocks_other_profile(tmp_path):
    workspace = tmp_path / "workspace"
    ready_read, ready_write = os.pipe()
    pid = os.fork()
    if pid == 0:  # pragma: no cover - child result is reported by the parent
        try:
            os.close(ready_read)
            WorkspaceOwnerMarker(workspace, tmp_path / "profile-one").acquire()
            os.write(ready_write, b"1")
            os._exit(0)
        except Exception:
            os.write(ready_write, b"0")
            os._exit(1)

    os.close(ready_write)
    try:
        assert os.read(ready_read, 1) == b"1"
    finally:
        os.close(ready_read)
        _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 0

    WorkspaceOwnerMarker(workspace, tmp_path / "profile-one").acquire()
    with pytest.raises(OSError, match="another Hermes profile"):
        WorkspaceOwnerMarker(workspace, tmp_path / "profile-two").acquire()


def test_embedded_provider_rejects_reused_workspace_across_profiles(monkeypatch, tmp_path, caplog):
    workspace = tmp_path / "shared-workspace"
    stage = Path(__file__).resolve().parents[2] / "reme/config/fermes-hermes-stage1.yaml"
    config = ReMeConfig(mode="embedded", workspace_dir=str(workspace), reme_config=str(stage))
    backend = _FakeBackend()
    monkeypatch.setattr(PLUGIN_MODULE, "load_config", lambda home=None: config)
    monkeypatch.setattr(PLUGIN_MODULE, "_backend_for", lambda _config: backend)

    owner = ReMeMemoryProvider()
    blocked = ReMeMemoryProvider()
    owner.initialize("owner", hermes_home=tmp_path / "profile-one", agent_identity="one")
    caplog.clear()
    blocked.initialize("blocked", hermes_home=tmp_path / "profile-two", agent_identity="two")

    assert owner._backend is not None
    assert blocked._backend is None
    assert blocked._accept_writes is False
    assert blocked.prefetch("must not cross profiles") == ""
    assert "marker-secret" not in caplog.text
    owner.shutdown()
    blocked.shutdown()


def test_workspace_owner_failure_does_not_log_marker_contents(monkeypatch, tmp_path, caplog):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / PLUGIN_MODULE._WORKSPACE_OWNER_FILENAME).write_text(
        json.dumps(
            {
                "format": 1,
                "owner": "marker-secret-not-for-logs",
                "workspace": str(workspace),
            },
        ),
        encoding="utf-8",
    )
    stage = Path(__file__).resolve().parents[2] / "reme/config/fermes-hermes-stage1.yaml"
    config = ReMeConfig(mode="embedded", workspace_dir=str(workspace), reme_config=str(stage))
    monkeypatch.setattr(PLUGIN_MODULE, "load_config", lambda home=None: config)
    monkeypatch.setattr(PLUGIN_MODULE, "_backend_for", lambda _config: _FakeBackend())

    provider = ReMeMemoryProvider()
    provider.initialize("session", hermes_home=tmp_path / "profile")

    assert provider._backend is None
    assert "marker-secret-not-for-logs" not in caplog.text
    provider.shutdown()


def test_embedded_provider_same_profile_restart_reuses_owner_marker(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    stage = Path(__file__).resolve().parents[2] / "reme/config/fermes-hermes-stage1.yaml"
    config = ReMeConfig(mode="embedded", workspace_dir=str(workspace), reme_config=str(stage))
    backends = [_FakeBackend(), _FakeBackend()]
    monkeypatch.setattr(PLUGIN_MODULE, "load_config", lambda home=None: config)
    monkeypatch.setattr(PLUGIN_MODULE, "_backend_for", lambda _config: backends.pop(0))

    first = ReMeMemoryProvider()
    first.initialize("first", hermes_home=tmp_path / "profile", agent_identity="profile")
    first.shutdown()
    second = ReMeMemoryProvider()
    second.initialize("second", hermes_home=tmp_path / "profile", agent_identity="profile")

    assert second._backend is not None
    second.shutdown()


def test_custom_embedded_config_requires_explicit_warning_opt_in(monkeypatch, tmp_path, caplog):
    custom = tmp_path / "custom.yaml"
    custom.write_text("service: {backend: http}\n", encoding="utf-8")
    with pytest.raises(ReMeConfigError, match="allow_custom_config"):
        parse_config(
            {
                "mode": "embedded",
                "workspace_dir": str(tmp_path / "workspace"),
                "reme_config": str(custom),
            },
            hermes_home=tmp_path,
        )

    config = ReMeConfig(
        mode="embedded",
        workspace_dir=str(tmp_path / "workspace"),
        reme_config=str(custom),
        allow_custom_config=True,
    )
    backend = _FakeBackend()
    monkeypatch.setattr(PLUGIN_MODULE, "load_config", lambda home=None: config)
    monkeypatch.setattr(PLUGIN_MODULE, "_backend_for", lambda _config: backend)
    provider = ReMeMemoryProvider()
    provider.initialize("session", hermes_home=tmp_path / "profile")
    assert "custom ReMe configuration" in caplog.text
    provider.shutdown()


def test_same_profile_provider_fails_closed_when_writer_is_owned(monkeypatch, tmp_path):
    backend = _FakeBackend()
    monkeypatch.setattr(PLUGIN_MODULE, "load_config", lambda home=None: ReMeConfig())
    monkeypatch.setattr(PLUGIN_MODULE, "_backend_for", lambda config: backend)
    owner = ReMeMemoryProvider()
    blocked = ReMeMemoryProvider()
    owner.initialize("owner", hermes_home=tmp_path, agent_identity="profile")

    blocked.initialize("blocked", hermes_home=tmp_path, agent_identity="profile")
    blocked.sync_turn("must not", "be appended")

    assert blocked._backend is None
    assert blocked._accept_writes is False
    assert blocked.prefetch("query") == ""
    assert "writer lock unavailable" in blocked.unavailable_reason()
    assert not (tmp_path / "reme" / "pending-writes.jsonl").exists()

    owner.shutdown()
    blocked.shutdown()


def test_reinitialize_closes_previous_backend(monkeypatch, tmp_path):
    backends = [_FakeBackend(), _FakeBackend()]
    monkeypatch.setattr(PLUGIN_MODULE, "load_config", lambda home=None: ReMeConfig())
    monkeypatch.setattr(PLUGIN_MODULE, "_backend_for", lambda config: backends.pop(0))
    provider = ReMeMemoryProvider()

    provider.initialize("first", hermes_home=tmp_path)
    first = provider._backend
    provider.initialize("second", hermes_home=tmp_path)

    assert first is not None and first.closed is True
    assert provider._backend is not first
    provider.shutdown()


def test_invalid_reinitialize_closes_previous_backend_and_disables_writes(monkeypatch, tmp_path):
    backend = _FakeBackend()
    outcomes = iter([ReMeConfig(), ReMeConfigError("broken profile config")])

    def load(_home=None):
        outcome = next(outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(PLUGIN_MODULE, "load_config", load)
    monkeypatch.setattr(PLUGIN_MODULE, "_backend_for", lambda config: backend)
    provider = ReMeMemoryProvider()
    provider.initialize("first", hermes_home=tmp_path, agent_identity="first-profile")

    provider.initialize("second", hermes_home=tmp_path, agent_identity="second-profile")
    provider.sync_turn("user", "assistant")

    assert backend.closed is True
    assert provider._backend is None
    assert provider._config is None
    assert provider._session_id == "second"
    assert provider._profile_id == "second-profile"
    assert provider._accept_writes is False
    assert provider.unavailable_reason() == "broken profile config"
    assert provider.prefetch("query") == ""
    assert provider._write_thread is None


def test_provider_failure_does_not_escape_model_path(monkeypatch, tmp_path):
    backend = _FakeBackend()
    backend.search = lambda *args, **kwargs: (_ for _ in ()).throw(
        ReMeBackendError("offline"),
    )
    monkeypatch.setattr(PLUGIN_MODULE, "load_config", lambda home=None: ReMeConfig())
    monkeypatch.setattr(PLUGIN_MODULE, "_backend_for", lambda config: backend)
    provider = ReMeMemoryProvider()
    provider.initialize("session", hermes_home=tmp_path)

    assert provider.prefetch("query") == ""
    assert provider.recall_status() is None
    provider.shutdown()


def test_provider_reports_embedded_workspace_for_hermes_backup(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    monkeypatch.setattr(
        PLUGIN_MODULE,
        "load_config",
        lambda home=None: ReMeConfig(mode="embedded", workspace_dir=str(workspace)),
    )

    assert ReMeMemoryProvider().backup_paths() == [str(workspace)]


def test_backend_creation_is_serialized(monkeypatch):
    provider = ReMeMemoryProvider()
    provider._config = ReMeConfig()
    created = []

    def factory(config):
        del config
        time.sleep(0.05)
        backend = _FakeBackend()
        created.append(backend)
        return backend

    monkeypatch.setattr(PLUGIN_MODULE, "_backend_for", factory)
    gate = threading.Barrier(3)
    results = []

    def ensure():
        gate.wait()
        results.append(provider._ensure_backend())

    threads = [threading.Thread(target=ensure) for _ in range(2)]
    for thread in threads:
        thread.start()
    gate.wait()
    for thread in threads:
        thread.join(timeout=2)

    assert results == [True, True]
    assert len(created) == 1
    provider.shutdown()


def test_shutdown_defers_close_until_inflight_recall_finishes():
    entered = threading.Event()
    release = threading.Event()

    class BlockingBackend(_FakeBackend):
        def search(self, query, *, limit, timeout):
            del query, limit, timeout
            entered.set()
            release.wait(timeout=2)
            return {"success": True, "answer": "remembered", "metadata": {}}

    backend = BlockingBackend()
    provider = ReMeMemoryProvider()
    provider._config = ReMeConfig()
    provider._backend = backend
    provider._backend_available = True
    provider._shutdown_timeout = 0.05
    recall = threading.Thread(target=provider.prefetch, args=("query",))
    recall.start()
    assert entered.wait(timeout=1)

    provider.shutdown()
    assert backend.closed is False
    release.set()
    recall.join(timeout=2)

    assert recall.is_alive() is False
    assert backend.closed is True


def test_shutdown_drains_all_accepted_writes_before_closing_backend(tmp_path):
    entered = threading.Event()
    release = threading.Event()

    class BlockingBackend(_FakeBackend):
        def auto_memory(self, session_id, messages, *, timeout, request_id=None):
            del timeout
            self.writes.append((session_id, messages))
            if len(self.writes) == 1:
                entered.set()
                release.wait(timeout=2)
            return {"success": True}

    backend = BlockingBackend()
    provider = ReMeMemoryProvider()
    provider._config = ReMeConfig()
    provider._pending_path = tmp_path / "pending-writes.jsonl"
    provider._backend = backend
    provider._backend_available = True
    provider._shutdown_timeout = 1
    for index in range(3):
        provider.sync_turn(f"user {index}", f"assistant {index}", session_id=f"session-{index}")
    assert entered.wait(timeout=1)

    shutdown = threading.Thread(target=provider.shutdown)
    shutdown.start()
    deadline = time.monotonic() + 1
    while not provider._shutdown_started and time.monotonic() < deadline:
        time.sleep(0.001)
    assert provider._shutdown_started is True
    release.set()
    shutdown.join(timeout=2)

    assert shutdown.is_alive() is False
    assert len(backend.writes) == 3
    assert backend.closed is True


def test_writer_inherits_hermes_profile_context(tmp_path):
    profile = contextvars.ContextVar("test_hermes_profile", default="default")

    class ContextBackend(_FakeBackend):
        def __init__(self):
            super().__init__()
            self.profile = None

        def auto_memory(self, session_id, messages, *, timeout, request_id=None):
            super().auto_memory(session_id, messages, timeout=timeout, request_id=request_id)
            self.profile = profile.get()

    backend = ContextBackend()
    provider = ReMeMemoryProvider()
    provider._config = ReMeConfig()
    provider._pending_path = tmp_path / "pending-writes.jsonl"
    provider._backend = backend
    provider._backend_available = True
    token = profile.set("work")
    try:
        provider.sync_turn("user", "assistant", session_id="session")
    finally:
        profile.reset(token)
    provider.shutdown()

    assert backend.profile == "work"


def test_recall_timeout_includes_waiting_for_background_write(tmp_path):
    write_entered = threading.Event()
    write_release = threading.Event()
    search_entered = threading.Event()

    class BlockingBackend(_FakeBackend):
        def auto_memory(self, session_id, messages, *, timeout, request_id=None):
            del session_id, messages, timeout
            write_entered.set()
            write_release.wait(timeout=2)
            return {"success": True}

        def search(self, query, *, limit, timeout):
            del query, limit, timeout
            search_entered.set()
            return {"success": True, "answer": "remembered", "metadata": {}}

    backend = BlockingBackend()
    provider = ReMeMemoryProvider()
    provider._config = ReMeConfig()
    provider._pending_path = tmp_path / "pending-writes.jsonl"
    provider._backend = backend
    provider._backend_available = True
    provider._recall_timeout = 0.05
    provider.sync_turn("user", "assistant", session_id="session")
    assert write_entered.wait(timeout=1)

    started = time.monotonic()
    assert provider.prefetch("query") == ""
    elapsed = time.monotonic() - started

    assert elapsed < 0.2
    assert search_entered.is_set() is False
    write_release.set()
    provider.shutdown()


def test_recall_timeout_includes_embedded_startup_failure_cleanup(monkeypatch, tmp_path):
    import reme
    import reme.config

    class SlowApplication:
        instances = []

        def __init__(self, **config):
            del config
            self.closed = threading.Event()
            self.thread_pool = object()
            self.__class__.instances.append(self)

        async def start(self):
            time.sleep(0.25)

        async def close(self):
            await asyncio.sleep(0.4)
            self.thread_pool = None
            self.closed.set()

    monkeypatch.setattr(reme, "Application", SlowApplication)
    monkeypatch.setattr(reme.config, "resolve_app_config", lambda **kwargs: kwargs)
    backend = EmbeddedReMeBackend(str(tmp_path), start_timeout=1)
    monkeypatch.setattr(PLUGIN_MODULE, "_backend_for", lambda config: backend)
    provider = ReMeMemoryProvider()
    provider._config = ReMeConfig(
        mode="embedded",
        workspace_dir=str(tmp_path),
        request_timeout=1,
        recall_timeout=0.15,
        shutdown_timeout=0.6,
    )
    provider._recall_timeout = 0.15
    provider._shutdown_timeout = 0.6

    started = time.monotonic()
    assert provider.prefetch("query") == ""
    elapsed = time.monotonic() - started

    assert elapsed < 0.25
    assert backend._thread is not None
    backend._thread.join(timeout=1)
    assert backend._thread.is_alive() is False
    app = SlowApplication.instances[0]
    assert app.closed.is_set()
    assert app.thread_pool is None


def test_embedded_start_timeout_closes_real_application_resources(monkeypatch, tmp_path):
    import reme
    import reme.config

    from reme.application import Application
    from reme.components import BaseComponent

    component_closed = threading.Event()

    class SlowComponent(BaseComponent):
        component_type = "slow_test"

        async def _start(self):
            time.sleep(0.25)

        async def _close(self):
            component_closed.set()

    app = Application(
        workspace_dir=str(tmp_path),
        service={"backend": "cli"},
        thread_pool_max_workers=1,
        enable_logo=False,
        log_to_console=False,
        log_to_file=False,
    )
    component = SlowComponent(name="slow", app_context=app.context)
    app.context.components["slow_test"] = {"slow": component}
    monkeypatch.setattr(reme, "Application", lambda **config: app)
    monkeypatch.setattr(reme.config, "resolve_app_config", lambda **kwargs: kwargs)
    backend = EmbeddedReMeBackend(str(tmp_path), start_timeout=1)

    started = time.monotonic()
    with pytest.raises(ReMeBackendError, match="timed out"):
        backend.start(deadline=started + 0.15)
    elapsed = time.monotonic() - started

    assert elapsed < 0.25
    assert backend._thread is not None
    backend._thread.join(timeout=1)
    assert backend._thread.is_alive() is False
    assert component_closed.is_set()
    assert component.is_started is False
    assert app.context.thread_pool is None


def test_shutdown_discard_keeps_sentinel_for_inflight_writer():
    provider = ReMeMemoryProvider()
    provider._write_queue.put({"session_id": "queued"})
    provider._write_queue.put(None)

    assert provider._discard_queued_writes() == 1
    assert provider._write_queue.get_nowait() is None


def test_accepted_write_is_replayed_after_backend_failure(monkeypatch, tmp_path):
    failed = _FakeBackend()
    failed.auto_memory = lambda *args, **kwargs: (_ for _ in ()).throw(
        ReMeBackendError("injected write failure"),
    )
    recovered = _FakeBackend()
    backends = iter([failed, recovered])
    monkeypatch.setattr(PLUGIN_MODULE, "load_config", lambda home=None: ReMeConfig())
    monkeypatch.setattr(PLUGIN_MODULE, "_backend_for", lambda config: next(backends))

    provider = ReMeMemoryProvider()
    provider.initialize("session", hermes_home=tmp_path, agent_identity="profile")
    provider.sync_turn("durable user", "durable assistant")
    deadline = time.monotonic() + 1
    while provider._write_thread is not None and time.monotonic() < deadline:
        time.sleep(0.001)

    pending = config_path(tmp_path).parent / "pending-writes.jsonl"
    assert pending.is_file()
    assert len(pending.read_text(encoding="utf-8").splitlines()) == 1
    provider.shutdown()

    replay = ReMeMemoryProvider()
    replay.initialize("new-session", hermes_home=tmp_path, agent_identity="profile")
    replay.shutdown()

    assert recovered.writes[0][1][0]["content"] == "durable user"
    assert pending.read_text(encoding="utf-8") == ""


def test_retried_spool_record_reuses_record_id_after_ack_failure(monkeypatch, tmp_path):
    backend = _FakeBackend()
    monkeypatch.setattr(PLUGIN_MODULE, "load_config", lambda home=None: ReMeConfig())
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
    from reme.steps.evolve.auto_memory import AutoMemoryStep

    calls = []
    stage_config = Path(__file__).resolve().parents[2] / "reme/config/fermes-hermes-stage1.yaml"
    config = ReMeConfig(
        mode="embedded",
        workspace_dir=str(tmp_path / "workspace"),
        reme_config=str(stage_config),
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


def test_failed_first_record_does_not_stall_later_record(monkeypatch, tmp_path):
    backend = _FakeBackend()
    calls = 0

    def auto_memory(session_id, messages, *, timeout, request_id=None):
        nonlocal calls
        del session_id, messages, timeout
        calls += 1
        backend.request_ids.append(request_id)
        if calls == 1:
            return {"success": False, "answer": "transient"}
        return {"success": True}

    backend.auto_memory = auto_memory
    monkeypatch.setattr(PLUGIN_MODULE, "load_config", lambda home=None: ReMeConfig())
    monkeypatch.setattr(PLUGIN_MODULE, "_backend_for", lambda config: backend)
    provider = ReMeMemoryProvider()
    provider.initialize("session", hermes_home=tmp_path)
    provider._health_retry_seconds = 60
    provider.sync_turn("first", "assistant")
    provider.sync_turn("second", "assistant")

    deadline = time.monotonic() + 1
    while calls < 2 and time.monotonic() < deadline:
        time.sleep(0.005)
    provider.shutdown()

    assert calls >= 2
    assert len(backend.request_ids) >= 2
    assert backend.request_ids[0] != backend.request_ids[1]


@pytest.mark.parametrize("response", [{"success": False}, [], None])
def test_false_or_malformed_auto_memory_ack_keeps_spool_record(monkeypatch, tmp_path, response):
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


def test_pending_queue_retains_poison_line_and_reads_following_valid_record(tmp_path):
    provider = ReMeMemoryProvider()
    provider._pending_path = tmp_path / "reme" / "pending-writes.jsonl"
    provider._pending_path.parent.mkdir()
    valid = {"id": "valid", "payload": {"session_id": "s", "messages": []}}
    provider._pending_path.write_text(
        "not-json\n" '{"id": "poison", "payload": "not-an-object"}\n' + json.dumps(valid) + "\n",
        encoding="utf-8",
    )

    assert provider._read_pending_writes() == [valid]
    assert provider._remove_pending_write("valid") is True
    remaining = provider._pending_path.read_text(encoding="utf-8")
    assert "not-json" in remaining
    assert '"poison"' in remaining
    assert '"valid"' not in remaining


def test_disk_append_failure_does_not_acknowledge_or_queue_turn(monkeypatch, tmp_path):
    provider = ReMeMemoryProvider()
    provider._pending_path = tmp_path / "reme" / "pending-writes.jsonl"
    provider._accept_writes = True

    def fail(_payload):
        raise OSError("disk full")

    monkeypatch.setattr(provider, "_append_pending_write", fail)
    provider.sync_turn("user", "assistant", session_id="session")

    assert provider._write_queue.empty()
    assert not provider._pending_path.exists()


def test_spool_rejects_symlink_and_sets_private_permissions(tmp_path):
    provider = ReMeMemoryProvider()
    provider._pending_path = tmp_path / "reme" / "pending-writes.jsonl"
    record = provider._append_pending_write({"session_id": "s", "messages": []})

    assert record["id"]
    assert provider._pending_path.stat().st_mode & 0o777 == 0o600
    assert provider._pending_path.parent.stat().st_mode & 0o777 == 0o700

    target = tmp_path / "outside.jsonl"
    target.write_text("outside", encoding="utf-8")
    provider._pending_path.unlink()
    provider._pending_path.symlink_to(target)
    with pytest.raises(OSError, match="unsafe"):
        provider._append_pending_write({"session_id": "s", "messages": []})
    assert target.read_text(encoding="utf-8") == "outside"


def test_initialize_without_hermes_home_fails_closed(monkeypatch):
    provider = ReMeMemoryProvider()
    monkeypatch.chdir("/")

    provider.initialize("session")

    assert provider._accept_writes is False
    assert provider._pending_path is None
    assert "hermes_home is required" in provider.unavailable_reason()


def test_provider_registers_with_hermes_memory_provider_contract():
    registered = []

    class Context:
        def register_memory_provider(self, provider):
            registered.append(provider)

    PLUGIN_MODULE.register(Context())

    assert len(registered) == 1
    assert isinstance(registered[0], ReMeMemoryProvider)
    assert registered[0].name == "reme"


def test_existing_unowned_workspace_cannot_be_silently_claimed(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    legacy_note = workspace / "daily-note.md"
    legacy_note.write_text("Pre-existing memory from unknown profile", encoding="utf-8")
    with pytest.raises(OSError, match="explicit migration required"):
        WorkspaceOwnerMarker(workspace, tmp_path / "new-profile").acquire()
    assert legacy_note.read_text(encoding="utf-8") == "Pre-existing memory from unknown profile"
    assert not (workspace / PLUGIN_MODULE._WORKSPACE_OWNER_FILENAME).exists()
