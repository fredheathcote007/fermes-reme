"""B.2 typed-batch receipt and Hermes ACK boundary tests."""

# pylint: disable=missing-function-docstring,protected-access,redefined-outer-name,wrong-import-position

from __future__ import annotations

import copy
import importlib
import importlib.util
import inspect
import queue
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

_SCHEMA_SPEC = importlib.util.spec_from_file_location(
    "reme.schema.typed_memory",
    Path(__file__).resolve().parents[2] / "reme" / "schema" / "typed_memory.py",
)
assert _SCHEMA_SPEC is not None and _SCHEMA_SPEC.loader is not None
_SCHEMA = importlib.util.module_from_spec(_SCHEMA_SPEC)
sys.modules["reme.schema.typed_memory"] = _SCHEMA
_SCHEMA_SPEC.loader.exec_module(_SCHEMA)

from reme.schema.typed_memory import TypedOptionBReceipt, sha256_canonical  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
_integration = importlib.import_module("test_hermes_agent_integration")
PLUGIN_MODULE = _integration.PLUGIN_MODULE
FakeBackend = importlib.import_module("hermes_agent_test_support").FakeBackend
ReMeBackendError = _integration.ReMeBackendError
ReMeConfig = _integration.ReMeConfig
ReMeMemoryProvider = _integration.ReMeMemoryProvider


def _payload(session_id: str = "session-1") -> dict[str, Any]:
    return {
        "session_id": session_id,
        "messages": [{"role": "user", "content": "remember this"}],
    }


def _receipt_commit(memory_id: str, record_type: str = "semantic") -> dict[str, Any]:
    return {
        "memory_id": memory_id,
        "record_type": record_type,
        "record_digest": "a" * 64,
        "body_sha256": "b" * 64,
        "note_path": f"memory/typed/{record_type}/{memory_id}.md",
        "status": "active",
        "revision_seq": 1,
        "created": True,
    }


def _typed_batch_receipt(
    *,
    request_id: str = "request-1",
    session_id: str = "session-1",
    workspace_id: str = "workspace-1",
    profile_id: str = "profile-1",
    commits: list[dict[str, Any]] | None = None,
    index_status: Any = None,
) -> dict[str, Any]:
    if commits is None:
        commits = [
            _receipt_commit("mem_semantic_1", "semantic"),
            _receipt_commit("mem_episodic_2", "episodic"),
        ]
    values: dict[str, Any] = {
        "outcome": "typed_batch",
        "batch_id": "batch-1",
        "request_id": request_id,
        "session_id": session_id,
        "workspace_id": workspace_id,
        "profile_id": profile_id,
        "n_records": len(commits),
        "commits": commits,
        "index_status": index_status,
        "committed_at_utc": "2026-11-13T01:02:03Z",
        "receipt_digest": "0" * 64,
    }
    draft = TypedOptionBReceipt.model_construct(**values)
    values["receipt_digest"] = sha256_canonical(
        {
            key: value
            for key, value in draft.model_dump(mode="json", exclude_none=False).items()
            if key != "receipt_digest"
        },
    )
    values["schema_version"] = 2
    return values


def _model_validate(envelope: dict[str, Any]) -> TypedOptionBReceipt:
    """Validate the B.2 envelope using the B.1 model after removing its wrapper field."""
    without_wrapper = {key: value for key, value in envelope.items() if key != "schema_version"}
    return TypedOptionBReceipt(**without_wrapper)


def _parser_payload() -> dict[str, Any]:
    """Include the scope values the pure parser must bind, in addition to messages."""
    return {
        **_payload(),
        "workspace_id": "workspace-1",
        "profile_id": "profile-1",
    }


def _call_parser(
    envelope: dict[str, Any],
    payload: dict[str, Any] | None = None,
    *,
    workspace_id: str | None = "workspace-1",
    profile_id: str | None = "profile-1",
) -> None:
    provider = ReMeMemoryProvider()
    provider._profile_id = profile_id
    if workspace_id is not None:
        provider._workspace_id = workspace_id
    provider._validate_typed_batch_receipt(
        envelope,
        _parser_payload() if payload is None else payload,
        "request-1",
    )


def _provider(backend: Any) -> ReMeMemoryProvider:
    from hermes_agent.config import STAGE1_CONFIG_NAME

    provider = ReMeMemoryProvider()
    provider._config = ReMeConfig(
        mode="embedded",
        reme_config=STAGE1_CONFIG_NAME,
        allow_custom_config=False,
    )
    provider._backend = backend
    provider._backend_available = True
    provider._defer_write_retry = lambda: None
    return provider


def test_typed_batch_receipt_parser_accepts_canonical_multi_commit_receipt() -> None:
    envelope = _typed_batch_receipt()

    validated = _model_validate(envelope)
    _call_parser(envelope)

    assert validated.n_records == 2
    assert [commit.note_path for commit in validated.commits] == [
        "memory/typed/semantic/mem_semantic_1.md",
        "memory/typed/episodic/mem_episodic_2.md",
    ]
    assert envelope["schema_version"] == 2
    assert validated.receipt_digest == sha256_canonical(
        {key: value for key, value in envelope.items() if key not in {"schema_version", "receipt_digest"}},
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("request_id", "other-request"),
        ("session_id", "other-session"),
        ("workspace_id", "other-workspace"),
        ("profile_id", "other-profile"),
    ],
)
def test_typed_batch_receipt_parser_rejects_unbound_scope(field: str, value: str) -> None:
    envelope = _typed_batch_receipt()
    envelope[field] = value

    with pytest.raises(ReMeBackendError):
        _call_parser(envelope)


@pytest.mark.parametrize(
    ("mutate", "model_rejects"),
    [
        pytest.param(
            lambda receipt: receipt["commits"].__setitem__(1, copy.deepcopy(receipt["commits"][0])),
            True,
            id="duplicate-memory-id",
        ),
        pytest.param(
            lambda receipt: receipt.update(n_records=1),
            True,
            id="truncated-commits",
        ),
        pytest.param(
            lambda receipt: receipt.update(committed_at_utc="2026-11-13T01:02:03+01:00"),
            True,
            id="non-utc-timestamp",
        ),
        pytest.param(
            lambda receipt: receipt.update(receipt_digest="f" * 64),
            True,
            id="tampered-receipt-digest",
        ),
        pytest.param(
            lambda receipt: receipt["commits"][0].update(body_sha256="c" * 64),
            True,
            id="tampered-commit-hash",
        ),
        pytest.param(
            lambda receipt: receipt.update(schema_version=1),
            False,
            id="wrong-schema-version",
        ),
        pytest.param(
            lambda receipt: receipt.pop("schema_version"),
            False,
            id="missing-schema-version",
        ),
        pytest.param(
            lambda receipt: receipt["commits"][0].pop("memory_id"),
            True,
            id="missing-memory-id",
        ),
        pytest.param(
            lambda receipt: receipt["commits"][0].pop("record_type"),
            True,
            id="missing-record-type",
        ),
        pytest.param(
            lambda receipt: receipt["commits"][0].pop("note_path"),
            True,
            id="missing-note-path",
        ),
    ],
)
def test_typed_batch_receipt_parser_fails_closed_on_tampering(mutate, model_rejects: bool) -> None:
    envelope = _typed_batch_receipt()
    mutate(envelope)

    if model_rejects:
        with pytest.raises((ReMeBackendError, ValueError, TypeError)):
            _model_validate(envelope)
    else:
        _model_validate(envelope)
    with pytest.raises(ReMeBackendError):
        _call_parser(envelope)


def test_provider_has_no_typed_ack_bypass_knob() -> None:
    provider = ReMeMemoryProvider()

    assert not hasattr(provider, "_allow_test_typed_ack")
    assert "_allow_test_typed_ack" not in inspect.getsource(ReMeMemoryProvider)


def test_valid_typed_batch_is_never_acknowledged_and_remains_retryable() -> None:
    backend = FakeBackend()
    backend.auto_memory = lambda *_args, **_kwargs: {
        "success": True,
        "answer": "typed batch committed",
        "metadata": _typed_batch_receipt(request_id="request-1"),
    }
    provider = _provider(backend)

    assert provider._record_payload(_payload(), request_id="request-1") is False
    assert provider._last_write_failure_kind == "retryable"


def test_valid_typed_batch_with_trusted_context_triggers_exact_b3_disabled_message() -> None:
    backend = FakeBackend()
    backend.auto_memory = lambda *_args, **_kwargs: {
        "success": True,
        "answer": "typed batch committed",
        "metadata": _typed_batch_receipt(
            request_id="request-1",
            workspace_id="workspace-1",
            profile_id="profile-1",
        ),
    }
    provider = _provider(backend)
    provider._workspace_id = "workspace-1"
    provider._profile_id = "profile-1"

    captured_exceptions: list[str] = []
    orig_classify = PLUGIN_MODULE._classify_write_failure
    try:

        def capture_and_classify(exc: Exception) -> str:
            captured_exceptions.append(str(exc))
            return orig_classify(exc)

        PLUGIN_MODULE._classify_write_failure = capture_and_classify
        assert provider._record_payload(_payload(), request_id="request-1") is False
        assert provider._last_write_failure_kind == "retryable"
        assert captured_exceptions == [
            "typed_batch receipts require Task B.3 crash-consistent intent journal; live typed ACK is disabled",
        ]
    finally:
        PLUGIN_MODULE._classify_write_failure = orig_classify


@pytest.mark.parametrize("workspace_state", ["absent", "none"])
def test_runtime_rejects_self_asserted_workspace_when_trusted_binding_is_missing(
    workspace_state: str,
) -> None:
    backend = FakeBackend()
    backend.auto_memory = lambda *_args, **_kwargs: {
        "success": True,
        "answer": "typed batch committed",
        "metadata": _typed_batch_receipt(workspace_id="self-asserted-workspace"),
    }
    provider = _provider(backend)
    if workspace_state == "none":
        provider._workspace_id = None
    else:
        assert not hasattr(provider, "_workspace_id")

    assert provider._record_payload(_payload(), request_id="request-1") is False
    assert provider._last_write_failure_kind == "retryable"


def test_typed_batch_failure_preserves_fifo_turn_for_replay(monkeypatch, tmp_path) -> None:
    from hermes_agent.config import STAGE1_CONFIG_NAME

    first = threading.Event()
    backend = FakeBackend()
    responses = iter(
        [
            lambda session_id, request_id: {
                "success": True,
                "answer": "typed batch committed",
                "metadata": _typed_batch_receipt(
                    request_id=request_id,
                    session_id=session_id,
                    workspace_id="workspace-1",
                    profile_id="profile-1",
                ),
            },
            lambda _session_id, request_id: {
                "success": True,
                "answer": "legacy commit",
                "metadata": {"outcome": "commit", "commit": {"request_id": request_id}},
            },
        ],
    )

    def auto_memory(session_id, _messages, *, timeout, request_id=None):
        del timeout
        backend.request_ids.append(request_id)
        response = next(responses)
        result = response(session_id, request_id)
        if len(backend.request_ids) == 1:
            first.set()
        return result

    backend.auto_memory = auto_memory
    config = ReMeConfig(
        mode="embedded",
        workspace_dir=str(tmp_path / "workspace"),
        reme_config=STAGE1_CONFIG_NAME,
        allow_custom_config=False,
        health_retry_seconds=0.01,
    )
    provider = ReMeMemoryProvider()
    provider._config = config
    provider._profile_root = tmp_path
    provider._pending_path = tmp_path / "reme" / "pending-writes.jsonl"
    provider._profile_id = "profile-1"
    provider._session_id = "session-1"
    provider._backend = backend
    provider._backend_available = True
    provider._accept_writes = True
    provider._write_enabled = True
    monkeypatch.setattr(provider, "_defer_write_retry", lambda: None)
    monkeypatch.setattr(provider, "_schedule_retry", lambda _record: None)

    provider.sync_turn("user", "assistant", session_id="session-1")
    assert first.wait(timeout=2)
    deadline = time.monotonic() + 2
    while provider._write_thread is not None and time.monotonic() < deadline:
        time.sleep(0.005)

    pending = provider._read_pending_writes()
    assert pending and pending[0]["id"] == backend.request_ids[0]
    assert provider._last_write_failure_kind == "retryable"

    replay_queue: queue.Queue[dict[str, Any] | None] = queue.Queue()
    replay_queue.put(pending[0])
    replay_queue.put(None)
    provider._write_loop(replay_queue)

    assert provider._read_pending_writes() == []
    assert backend.request_ids == [backend.request_ids[0], backend.request_ids[0]]
    provider.shutdown()


class _MaliciousUnhashableStatus:
    __hash__ = None

    def __eq__(self, _other):
        raise AssertionError("index_status comparison must not execute user equality")

    def __repr__(self) -> str:
        return "<malicious-unhashable-status>"


@pytest.mark.parametrize("outcome", ["commit", "typed_batch"])
@pytest.mark.parametrize(
    "index_status",
    [
        "corrupted",
        "unknown",
        123,
        True,
        3.14,
        ["ok"],
        {"status": "ok"},
        {"ok"},
        _MaliciousUnhashableStatus(),
    ],
    ids=["string-corrupted", "string-unknown", "int", "bool", "float", "list", "dict", "set", "malicious"],
)
def test_invalid_index_status_fails_closed_for_commit_and_typed_batch(outcome, index_status) -> None:
    backend = FakeBackend()
    if outcome == "commit":
        metadata = {
            "outcome": "commit",
            "commit": {"request_id": "request-1"},
            "index_status": index_status,
        }
    else:
        metadata = _typed_batch_receipt()
        metadata["index_status"] = index_status
    backend.auto_memory = lambda *_args, **_kwargs: {"success": True, "metadata": metadata}
    provider = _provider(backend)

    assert provider._record_payload(_payload(), request_id="request-1") is False
    assert provider._last_write_failure_kind == "retryable"


@pytest.mark.parametrize("outcome", ["commit", "typed_batch"])
@pytest.mark.parametrize("index_status", [None, "ok", "degraded"])
def test_valid_index_statuses_are_not_rejected(outcome, index_status, caplog) -> None:
    backend = FakeBackend()
    metadata = (
        {
            "outcome": "commit",
            "commit": {"request_id": "request-1"},
            "index_status": index_status,
        }
        if outcome == "commit"
        else _typed_batch_receipt(index_status=index_status)
    )
    backend.auto_memory = lambda *_args, **_kwargs: {"success": True, "metadata": metadata}
    provider = _provider(backend)

    result = provider._record_payload(_payload(), request_id="request-1")
    assert result is (outcome == "commit")
    assert "unrecognized index_status" not in caplog.text


def test_existing_commit_outcome_remains_backward_compatible() -> None:
    backend = FakeBackend()
    backend.auto_memory = lambda *_args, **_kwargs: {
        "success": True,
        "answer": "legacy commit",
        "metadata": {"outcome": "commit", "commit": {"request_id": "request-1"}},
    }
    provider = _provider(backend)

    assert provider._record_payload(_payload(), request_id="request-1") is True
    assert provider._last_write_failure_kind == ""


def test_existing_noop_outcome_remains_backward_compatible() -> None:
    backend = FakeBackend()
    policy = {
        "policy": "application_authorized_noop",
        "authority_id": "test-authority",
        "request_id": "request-1",
        "session_id": "session-1",
    }
    backend.auto_memory = lambda *_args, **_kwargs: {
        "success": True,
        "answer": "Skipped: application-authorized no-op",
        "metadata": {
            "schema_version": 2,
            "outcome": "noop",
            "request_id": "request-1",
            "session_id": "session-1",
            "input_fingerprint": "fingerprint",
            "authority_id": "test-authority",
            "policy": "application_authorized_noop",
            "modified": False,
            "n_messages": 1,
        },
    }
    provider = _provider(backend)

    assert provider._record_payload(_payload(), request_id="request-1", noop_policy=policy) is True
    assert provider._last_write_failure_kind == ""
