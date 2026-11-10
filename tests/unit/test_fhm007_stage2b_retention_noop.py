"""Deterministic Stage 2B protected no-op and retention contract regressions."""

# pylint: disable=protected-access

from __future__ import annotations

import hashlib
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
HttpReMeBackend = _HERMES_INTEGRATION.HttpReMeBackend
ReMeBackendError = _HERMES_INTEGRATION.ReMeBackendError


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


def test_http_backend_rejects_application_authorized_noop():
    """HTTP transport cannot authenticate an application-owned no-op policy."""
    backend = HttpReMeBackend("http://127.0.0.1:1", request_timeout=0.1)

    with pytest.raises(ReMeBackendError, match="cannot authenticate protected no-op authority"):
        backend.auto_memory(
            "session",
            [],
            timeout=0.1,
            request_id="request",
            noop_policy=_noop_policy(request_id="request", session_id="session"),
        )


def test_quarantine_provenance_is_generated_and_validated():
    """Quarantine provenance binds profile, request, messages, digest, and policy."""
    record = {
        "id": "request-1",
        "profile_id": "profile-1",
        "payload": {
            "session_id": "session-1",
            "messages": [
                {"id": "message-1", "role": "user", "content": "fact"},
                {"id": "message-2", "role": "assistant", "content": "recorded"},
            ],
            "memory_hint": "retain this",
            "noop_policy": {"policy": "retention_required"},
        },
    }

    provenance = ReMeMemoryProvider._quarantine_provenance(record)

    assert provenance == {
        "profile_id": "profile-1",
        "request_id": "request-1",
        "session_id": "session-1",
        "message_ids": ["message-1", "message-2"],
        "input_sha256": hashlib.sha256(
            json.dumps(record["payload"], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"),
        ).hexdigest(),
        "memory_hint_present": True,
        "policy": "retention_required",
    }
    assert ReMeMemoryProvider._valid_quarantine_provenance({**record, "provenance": provenance})
    tampered = {**provenance, "message_ids": ["message-forged"]}
    assert not ReMeMemoryProvider._valid_quarantine_provenance({**record, "provenance": tampered})


@pytest.mark.asyncio
async def test_noop_receipt_replay_under_different_session_is_rejected(tmp_path):
    """A no-op receipt copied into another session scope must fail closed."""
    first = _protected_step(tmp_path, noop_policy=_noop_policy(session_id="session-1"))
    await first.execute()
    receipt_path, _ = _receipt(tmp_path)
    request_key = hashlib.sha256("request-1".encode()).hexdigest()
    session_key = hashlib.sha256("session-2".encode()).hexdigest()
    swapped_path = tmp_path / "metadata" / "auto_memory_requests" / session_key / f"{request_key}.json"
    swapped_path.parent.mkdir(parents=True)
    swapped_path.write_bytes(receipt_path.read_bytes())

    second = _protected_step(tmp_path, session_id="session-2", noop_policy=_noop_policy(session_id="session-2"))
    await second.execute()

    assert not second.context.response.success
    assert "different input" in second.context.response.answer


def test_fifo_advances_after_authorized_noop_head(tmp_path, monkeypatch):
    """A durable no-op head must complete before the following turn is processed."""
    completed = threading.Event()
    calls = []

    class QueueBackend:
        """Return a no-op for the head and a bound commit for its successor."""

        label = "fifo-noop-test"

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
            """Return the appropriate durable outcome for each FIFO turn."""
            del timeout, memory_hint
            calls.append((session_id, messages, request_id, noop_policy))
            if noop_policy is not None:
                metadata = {
                    "schema_version": 2,
                    "outcome": "noop",
                    "request_id": request_id,
                    "session_id": session_id,
                    "input_fingerprint": "noop-fingerprint",
                    "authority_id": noop_policy["authority_id"],
                    "policy": "application_authorized_noop",
                    "modified": False,
                    "n_messages": len(messages),
                }
            else:
                metadata = {"commit": {"request_id": request_id}}
            if len(calls) == 2:
                completed.set()
            return {"success": True, "metadata": metadata}

        def close(self, *, timeout):
            """Close without allocating resources."""
            del timeout

    backend = QueueBackend()
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
    provider.sync_turn("next", "assistant", session_id="session")

    assert completed.wait(timeout=2)
    provider.shutdown()
    assert provider._last_write_failure_kind == ""
    assert not provider._read_pending_writes()
    assert len(calls) == 2
    assert calls[0][3]["policy"] == "application_authorized_noop"
    assert calls[1][3] is None
    assert calls[0][2] != calls[1][2]


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
async def test_noop_policy_with_extra_fields_is_rejected(tmp_path):
    """Unexpected no-op policy fields must fail the exact-envelope check."""
    policy = {**_noop_policy(), "extra": "malicious"}
    step = _protected_step(tmp_path, noop_policy=policy)

    await step.execute()

    assert not step.context.response.success
    assert step.context.response.metadata["fermes_refusal_code"] == "PROTECTED_OPTION_B_POLICY_CONFLICT"
    assert not list(tmp_path.rglob("*.json"))


@pytest.mark.asyncio
async def test_noop_policy_with_missing_fields_is_rejected(tmp_path):
    """A no-op policy missing a required envelope field must be refused."""
    policy = _noop_policy()
    del policy["authority_id"]
    step = _protected_step(tmp_path, noop_policy=policy)

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
