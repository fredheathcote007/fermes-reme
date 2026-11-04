"""Focused recovery tests for the external Hermes memory-provider plugin."""

# pylint: disable=missing-function-docstring,protected-access,wrong-import-position

from __future__ import annotations

import importlib
import json
import sys
import threading
import time
import types
from dataclasses import dataclass
from pathlib import Path

from tests.unit.hermes_agent_test_support import FakeBackend

_PLUGIN_PARENT = Path(__file__).resolve().parents[2] / "integrations"
if str(_PLUGIN_PARENT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_PARENT))

try:
    importlib.import_module("agent.memory_provider")
except ImportError:
    agent_module = types.ModuleType("agent")
    memory_provider_module = types.ModuleType("agent.memory_provider")

    class MemoryProvider:
        """Minimal Hermes contract used when hermes-agent is not installed."""

    @dataclass(frozen=True)
    class RecallStatus:
        """Fallback recall status used without a Hermes installation."""

        provider_label: str
        count: int
        glyph: str = "🧠"

    memory_provider_module.MemoryProvider = MemoryProvider
    memory_provider_module.RecallStatus = RecallStatus
    agent_module.memory_provider = memory_provider_module
    sys.modules["agent"] = agent_module
    sys.modules["agent.memory_provider"] = memory_provider_module

PLUGIN_MODULE = importlib.import_module("hermes_agent")
CONFIG_MODULE = importlib.import_module("hermes_agent.config")
ReMeMemoryProvider = PLUGIN_MODULE.ReMeMemoryProvider
ReMeBackendError = importlib.import_module("hermes_agent.backend").ReMeBackendError
ReMeConfig = CONFIG_MODULE.ReMeConfig
config_path = CONFIG_MODULE.config_path


def test_permanent_refusal_is_quarantined_instead_of_retried_forever(monkeypatch, tmp_path):
    backend = FakeBackend()
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


def test_retryable_error_prefix_remains_durable_and_is_not_quarantined(monkeypatch, tmp_path):
    backend = FakeBackend()
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
