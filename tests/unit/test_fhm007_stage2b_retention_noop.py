"""Deterministic Stage 2B protected no-op and retention contract regressions."""

# pylint: disable=protected-access

from __future__ import annotations

import importlib
import json
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from reme.components.runtime_context import RuntimeContext
from reme.schema import Response
from reme.steps.evolve.auto_memory import AutoMemoryStep

sys.path.insert(0, str(Path(__file__).resolve().parent))
_HERMES_INTEGRATION = importlib.import_module("test_hermes_agent_integration")
HERMES_MODULE = _HERMES_INTEGRATION.PLUGIN_MODULE
ReMeConfig = _HERMES_INTEGRATION.ReMeConfig
ReMeMemoryProvider = _HERMES_INTEGRATION.ReMeMemoryProvider


def _protected_step(
    tmp_path: Path,
    *,
    request_id: str = "request-1",
    session_id: str = "session-1",
    messages: list[dict] | None = None,
    **kwargs,
) -> AutoMemoryStep:
    step = AutoMemoryStep(durable_commit=True, agent_scope="test-scope")
    step.file_store = SimpleNamespace(workspace_path=tmp_path)
    step.agent_wrapper = SimpleNamespace(backend="agentscope")
    step.context = RuntimeContext(
        request_id=request_id,
        session_id=session_id,
        messages=[] if messages is None else messages,
        date="2026-11-10",
        **kwargs,
    )
    return step


def _noop_policy(request_id: str = "request-1", session_id: str = "session-1", authority_id: str = "test-harness"):
    return {
        "policy": "application_authorized_noop",
        "authority_id": authority_id,
        "request_id": request_id,
        "session_id": session_id,
    }


def _receipt(tmp_path: Path) -> tuple[Path, dict]:
    receipts = list(tmp_path.rglob("*.json"))
    assert len(receipts) == 1
    return receipts[0], json.loads(receipts[0].read_text(encoding="utf-8"))


def test_hermes_worker_accepts_application_authorized_noop(tmp_path, monkeypatch):
    """The Hermes worker must accept a valid bound no-op receipt without error."""
    completed = threading.Event()
    calls = []

    class NoopBackend:
        """Return a correctly bound protected no-op receipt to the worker."""

        label = "noop-test"

        def start(self, **kwargs):
            """Start without allocating resources."""
            del kwargs

        def health(self, *, timeout):
            """Report a healthy embedded backend."""
            del timeout
            return {"success": True, "metadata": {"health": {"healthy": True}}}

        def auto_memory(
            self,
            session_id,
            messages,
            *,
            timeout,
            request_id=None,
            memory_hint=None,
            noop_policy=None,
        ):
            """Return a bound version-two no-op receipt."""
            del timeout, memory_hint
            calls.append((session_id, messages, request_id, noop_policy))
            completed.set()
            return {
                "success": True,
                "answer": "Skipped: application-authorized no-op",
                "metadata": {
                    "schema_version": 2,
                    "outcome": "noop",
                    "request_id": request_id,
                    "session_id": session_id,
                    "input_fingerprint": "fingerprint",
                    "authority_id": noop_policy["authority_id"],
                    "policy": "application_authorized_noop",
                    "modified": False,
                    "n_messages": len(messages),
                },
            }

        def close(self, *, timeout):
            """Close without allocating resources."""
            del timeout

    backend = NoopBackend()
    config = ReMeConfig(mode="embedded", workspace_dir=str(tmp_path / "workspace"), health_retry_seconds=0.01)
    monkeypatch.setattr(HERMES_MODULE, "load_config", lambda home=None: config)
    monkeypatch.setattr(HERMES_MODULE, "_backend_for", lambda _config: backend)
    provider = ReMeMemoryProvider()
    provider.initialize("session", hermes_home=tmp_path / "profile", agent_identity="profile")
    provider.sync_turn(
        "",
        "",
        session_id="session",
        noop_policy=_noop_policy(request_id="caller-request", session_id="session"),
    )

    assert completed.wait(timeout=2)
    provider.shutdown()
    assert len(calls) == 1
    assert provider._last_write_failure_kind == ""
    assert not provider._read_pending_writes()
    session_id, messages, request_id, policy = calls[0]
    assert session_id == policy["session_id"]
    assert request_id == policy["request_id"]
    assert len(messages) == 2


@pytest.mark.asyncio
async def test_empty_protected_input_without_authenticated_policy_is_not_noop(tmp_path):
    """Empty protected input without authority must be refused, not acknowledged."""
    step = _protected_step(tmp_path)

    await step.execute()

    assert not step.context.response.success
    assert step.context.response.metadata["fermes_refusal_code"] == "PROTECTED_OPTION_B_POLICY_CONFLICT"
    assert not list(tmp_path.rglob("*.json"))


@pytest.mark.asyncio
async def test_memory_hint_is_retention_required_and_cannot_be_noop(tmp_path):
    """A nonblank retention hint must reject an application-authorized no-op."""
    step = _protected_step(
        tmp_path,
        memory_hint="retain this exact fact",
        noop_policy=_noop_policy(),
    )

    await step.execute()

    assert not step.context.response.success
    assert step.context.response.metadata["fermes_refusal_code"] == "PROTECTED_OPTION_B_POLICY_CONFLICT"
    assert not list(tmp_path.rglob("*.json"))


@pytest.mark.asyncio
async def test_noop_receipt_has_bound_schema_and_zero_commit_evidence(tmp_path):
    """An authorized no-op receipt is versioned and contains no commit evidence."""
    step = _protected_step(tmp_path, noop_policy=_noop_policy())

    await step.execute()

    assert step.context.response.success
    assert step.context.response.metadata["outcome"] == "noop"
    assert step.context.response.metadata["authority_id"] == "test-harness"
    _, record = _receipt(tmp_path)
    assert record["schema_version"] == 2
    assert record["request_id"] == "request-1"
    assert record["session_id"] == "session-1"
    assert record["policy"] == "application_authorized_noop"
    assert record["modified"] is False
    assert record["n_messages"] == 0
    assert record["changes"] == []
    assert "commit" not in record
    assert "note_sha256" not in record


@pytest.mark.asyncio
async def test_same_request_id_with_changed_noop_policy_is_rejected(tmp_path):
    """Changing authority under a reused request ID must not replay the old no-op."""
    first = _protected_step(tmp_path, noop_policy=_noop_policy(authority_id="first-authority"))
    await first.execute()
    second = _protected_step(tmp_path, noop_policy=_noop_policy(authority_id="different-authority"))

    await second.execute()

    assert not second.context.response.success
    assert "different input" in second.context.response.answer
    _, record = _receipt(tmp_path)
    assert record["authority_id"] == "first-authority"


def test_legacy_noop_receipt_without_stage2b_binding_is_rejected():
    """Legacy unbound no-op receipts must fail closed during replay validation."""
    response = Response(
        success=True,
        answer="Skipped: no messages",
        metadata={"outcome": "noop", "modified": False, "n_messages": 0, "request_id": "request-1"},
    )
    legacy = {
        "version": 1,
        "outcome": "noop",
        "fingerprint": "fingerprint",
        "response": response.model_dump(mode="json"),
        "changes": [],
    }

    with pytest.raises(RuntimeError, match="schema"):
        # The validator is intentionally exercised directly to prove legacy
        # receipts cannot enter the trusted replay path.
        # pylint: disable=protected-access
        AutoMemoryStep._validate_option_b_noop_receipt_record(
            legacy,
            request_id="request-1",
            session_id="session-1",
            fingerprint="fingerprint",
            authority_id="test-harness",
            n_messages=0,
        )
