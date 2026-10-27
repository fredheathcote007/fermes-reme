"""Completed automatic-memory requests can be replayed after a client timeout."""

# pylint: disable=protected-access,missing-function-docstring

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import frontmatter
import pytest
from agentscope.message import Msg

from reme.application import Application
from reme.components.agent_wrapper.base_agent_wrapper import BaseAgentWrapper
from reme.components.prompt_handler import PromptHandler
from reme.components.runtime_context import RuntimeContext
from reme.steps.evolve.auto_memory import AutoMemoryStep


class _SecurityTestWrapper(BaseAgentWrapper):
    async def reply(self, _inputs, **_kwargs):
        pytest.fail("protected validation must happen before a model call")


class _AutoMemorySubclass(AutoMemoryStep):
    pass


def _step(tmp_path, request_id="batch-1", **kwargs):
    step = AutoMemoryStep()
    step.file_store = SimpleNamespace(workspace_path=tmp_path)
    step.context = RuntimeContext(
        request_id=request_id,
        session_id="session-1",
        messages=[{"id": "m1", "content": "fact"}],
        date="2026-10-21",
        **kwargs,
    )
    return step


@pytest.mark.parametrize("body", ["hello", "hello\n", "hello\nworld", "hello\nworld\n"])
def test_option_b_body_digest_matches_canonical_stored_body(body):
    canonical = frontmatter.loads(frontmatter.dumps(frontmatter.Post(body))).content
    expected = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    assert AutoMemoryStep._body_digest(body) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("first_is_msg", [False, True])
@pytest.mark.parametrize("replay_is_msg", [False, True])
async def test_message_objects_and_dicts_share_a_stable_request_fingerprint(tmp_path, first_is_msg, replay_is_msg):
    message = Msg.model_validate(
        {
            "id": "stable-message",
            "name": "user",
            "role": "user",
            "content": [{"type": "text", "text": "fact"}],
            "created_at": "2026-10-21T12:00:00+08:00",
        },
    )
    original = message.model_dump(mode="json")
    first = _step(tmp_path)
    first.context["messages"] = [message if first_is_msg else original]

    async def execute():
        first.context.response.answer = "recorded"

    first._execute = execute
    await first.execute()
    assert first.context.response.success
    assert message.model_dump(mode="json") == original
    replay = _step(tmp_path)
    replay.context["messages"] = [Msg.model_validate(original) if replay_is_msg else original]

    async def unexpected():
        pytest.fail("replaying a supported message form must not invoke the agent")

    replay._execute = unexpected
    await replay.execute()
    assert replay.context.response == first.context.response


@pytest.mark.asyncio
async def test_completed_request_survives_restart_without_repeating_agent(tmp_path):
    first = _step(tmp_path)

    async def execute():
        first.context.response.answer = "recorded"
        # This unit test exercises receipt replay, not physical-note provenance.
        first.context.response.metadata = {"synthetic": "no-durable-note"}
        first.context["changes"] = [{"change": "modified", "path": "daily/note.md"}]

    first._execute = execute
    await first.execute()
    replay = _step(tmp_path)

    async def unexpected():
        pytest.fail("completed request must not invoke the memory agent")

    replay._execute = unexpected
    await replay.execute()
    assert replay.context.response == first.context.response
    assert replay.context["changes"] == first.context["changes"]


@pytest.mark.asyncio
async def test_application_run_job_replays_real_receipt_without_repeating_side_effect(tmp_path, monkeypatch):
    calls = []

    async def synthetic_execute(step):
        calls.append(step.context.get("request_id"))
        step.context.response.answer = "synthetic receipt"
        step.context.response.metadata = {"side_effect": "once"}
        step.context["changes"] = [{"change": "modified", "path": "daily/note.md"}]

    monkeypatch.setattr(AutoMemoryStep, "_execute", synthetic_execute)
    app = Application(
        workspace_dir=str(tmp_path),
        enable_logo=False,
        log_to_console=False,
        log_to_file=False,
        service={"backend": "cli"},
        components={
            "tokenizer": {"default": {"backend": "regex"}},
            "file_graph": {"default": {"backend": "local"}},
            "keyword_index": {"default": {"backend": "bm25", "tokenizer": "default"}},
            "file_store": {
                "default": {
                    "backend": "local",
                    "embedding_store": "",
                    "keyword_index": "default",
                    "file_graph": "default",
                },
            },
        },
        jobs={
            "auto_memory": {
                "backend": "base",
                "steps": [{"backend": "auto_memory_step"}],
            },
        },
    )

    async with app:
        first = await app.run_job(
            "auto_memory",
            request_id="application-receipt-1",
            session_id="session-1",
            messages=[{"id": "m1", "content": "fact"}],
            date="2026-10-21",
        )
        replay = await app.run_job(
            "auto_memory",
            request_id="application-receipt-1",
            session_id="session-1",
            messages=[{"id": "m1", "content": "fact"}],
            date="2026-10-21",
        )
        conflict = await app.run_job(
            "auto_memory",
            request_id="application-receipt-1",
            session_id="session-1",
            messages=[{"id": "m2", "content": "different fact"}],
            date="2026-10-21",
        )

    assert calls == ["application-receipt-1"]
    assert replay == first
    assert replay.metadata == {"side_effect": "once"}
    assert conflict.success is False
    assert "different input" in conflict.answer


@pytest.mark.asyncio
async def test_concurrent_duplicate_waits_for_the_original_request(tmp_path):
    first, duplicate = _step(tmp_path), _step(tmp_path)
    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def execute():
        calls.append("agent")
        started.set()
        await release.wait()

    first._execute = duplicate._execute = execute
    original = asyncio.create_task(first.execute())
    await started.wait()
    retry = asyncio.create_task(duplicate.execute())
    await asyncio.sleep(0)
    assert calls == ["agent"]
    release.set()
    await asyncio.wait_for(asyncio.gather(original, retry), 2)
    assert calls == ["agent"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("request_id", "step_kwargs", "diagnostic"),
    [
        (None, {"durable_commit": True, "agent_scope": "test-scope"}, "protected memory commit requires request_id"),
        ("protected-scope-check", {"durable_commit": True}, "configured agent_scope"),
    ],
)
async def test_protected_auto_memory_rejects_missing_identity_before_legacy_model_write(
    tmp_path,
    monkeypatch,
    request_id,
    step_kwargs,
    diagnostic,
):
    """Protected invocations never fall through to legacy model write tools."""
    step = AutoMemoryStep(**step_kwargs)
    step.file_store = SimpleNamespace(workspace_path=tmp_path)
    step.context = RuntimeContext(
        session_id="protected-session",
        messages=[{"id": "m1", "content": "fact"}],
        date="2026-10-21",
    )
    if request_id is not None:
        step.context["request_id"] = request_id
    calls = []

    async def unexpected_legacy_execute():
        calls.append("legacy-model-write-tools")

    monkeypatch.setattr(step, "_execute", unexpected_legacy_execute)
    await step.execute()

    assert not step.context.response.success
    assert diagnostic in step.context.response.answer
    assert not calls
    assert not list(tmp_path.rglob("*.md"))
    assert not list(tmp_path.rglob("*.json"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("step_class", "backend"),
    [(AutoMemoryStep, "not-agentscope"), (_AutoMemorySubclass, "agentscope")],
)
async def test_protected_auto_memory_rejects_unsupported_wrapper_or_subclass_before_execute(
    tmp_path,
    monkeypatch,
    step_class,
    backend,
):
    wrapper = _SecurityTestWrapper(backend=backend)
    step = step_class(
        agent_wrapper=wrapper,
        durable_commit=True,
        agent_scope="test-scope",
    )
    step.file_store = SimpleNamespace(workspace_path=tmp_path)
    step.context = RuntimeContext(
        request_id="unsupported-protected-request",
        session_id="protected-session",
        messages=[{"id": "m1", "content": "fact"}],
        date="2026-10-21",
    )
    calls = []

    async def unexpected_execute():
        calls.append("execute")

    monkeypatch.setattr(step, "_execute", unexpected_execute)
    await step.execute()

    assert not step.context.response.success
    assert "exact AgentScope AutoMemoryStep" in step.context.response.answer
    assert not calls
    assert not list(tmp_path.rglob("*.md"))
    assert not list(tmp_path.rglob("*.json"))


@pytest.mark.asyncio
@pytest.mark.parametrize("durable_commit", [1, "true", "yes"])
async def test_malformed_durable_commit_is_rejected_before_execute(tmp_path, monkeypatch, durable_commit):
    step = AutoMemoryStep(durable_commit=durable_commit, agent_scope="test-scope")
    step.file_store = SimpleNamespace(workspace_path=tmp_path)
    step.context = RuntimeContext(
        request_id="malformed-durable-request",
        session_id="protected-session",
        messages=[{"id": "m1", "content": "fact"}],
        date="2026-10-21",
    )
    calls = []

    async def unexpected_execute():
        calls.append("execute")

    monkeypatch.setattr(step, "_execute", unexpected_execute)
    await step.execute()

    assert not step.context.response.success
    assert "durable_commit must be a boolean" in step.context.response.answer
    assert not calls
    assert not list(tmp_path.rglob("*.md"))
    assert not list(tmp_path.rglob("*.json"))


@pytest.mark.asyncio
async def test_option_b_marker_binds_body_and_allows_new_request_revision(tmp_path, monkeypatch):
    """Protected replay is receipt-free only with matching marker/body evidence."""
    from reme.components.agent_wrapper.as_agent_wrapper import AsAgentWrapper
    from reme.config import resolve_app_config

    config_path = Path(__file__).resolve().parents[2] / "reme/config/fermes-hermes-stage1.yaml"
    config = resolve_app_config(config=str(config_path), workspace_dir=str(tmp_path / "workspace"), log_config=False)
    calls = []
    proposal_body = "The first protected fact."
    note_path = tmp_path / "workspace" / "daily/2026-10-21/protected-fact.md"

    async def proposal_reply(_self, _inputs, **_kwargs):
        calls.append(1)
        body = proposal_body if len(calls) == 1 else "The first protected fact.\nThe second protected fact."
        return {
            "result": json.dumps(
                {
                    "path": "daily/2026-10-21/protected-fact.md",
                    "name": "protected-fact",
                    "description": "A deterministic protected fact.",
                    "content": body,
                },
            ),
            "tool_execution": [],
        }

    monkeypatch.setattr(AsAgentWrapper, "reply", proposal_reply)
    request = {
        "session_id": "optionb-session",
        "messages": [{"id": "m1", "name": "user", "role": "user", "content": "fact"}],
        "date": "2026-10-21",
    }
    revised_request = {
        **request,
        "messages": [
            *request["messages"],
            {"id": "m2", "name": "user", "role": "user", "content": "a new fact"},
        ],
    }
    async with Application(**config) as app:
        first = await app.run_job("auto_memory", request_id="optionb-1", **request)
        repeated = await app.run_job("auto_memory", request_id="optionb-1", **request)
        revised = await app.run_job("auto_memory", request_id="optionb-2", **revised_request)
        same_after_revision = await app.run_job("auto_memory", request_id="optionb-1", **request)
        assert first.success and repeated.success and revised.success
        assert not same_after_revision.success
        assert len(calls) == 2

        saved = frontmatter.loads(note_path.read_text(encoding="utf-8"))
        marker = saved.metadata["_reme_commit"]
        assert marker["request_id"] == "optionb-2"
        assert marker["note_path"] == "daily/2026-10-21/protected-fact.md"
        assert marker["body_sha256"]
        assert marker["source_hash"] != first.metadata["source_hash"]
        saved.content = "tampered\n"
        note_path.write_text(frontmatter.dumps(saved), encoding="utf-8")
        tampered = await app.run_job("auto_memory", request_id="optionb-2", **revised_request)

    assert not tampered.success
    assert "protected" in tampered.answer.lower()
    assert len(calls) == 2


def test_option_b_system_prompt_renders_literal_json_schema():
    prompt_file = Path(__file__).resolve().parents[2] / "reme/steps/evolve/auto_memory.yaml"
    prompt = (
        PromptHandler()
        .load_prompt_by_file(prompt_file)
        .prompt_format(
            "option_b_system_prompt",
            agent_scope="test-scope",
        )
    )
    assert '{"path": "workspace-relative target"' in prompt
    assert "{agent_scope}" not in prompt
    assert "test-scope" in prompt


@pytest.mark.asyncio
@pytest.mark.parametrize("forgery", ["marker", "receipt"])
async def test_option_b_refuses_forged_prior_revision_before_model(tmp_path, monkeypatch, forgery):
    from reme.components.agent_wrapper.as_agent_wrapper import AsAgentWrapper
    from reme.config import resolve_app_config

    workspace = tmp_path / "workspace"
    config_path = Path(__file__).resolve().parents[2] / "reme/config/fermes-hermes-stage1.yaml"
    config = resolve_app_config(config=str(config_path), workspace_dir=str(workspace), log_config=False)
    calls = []

    async def proposal_reply(_self, _inputs, **_kwargs):
        calls.append(1)
        return {
            "result": json.dumps(
                {
                    "path": "daily/2026-10-21/forged-transition.md",
                    "name": "forged-transition",
                    "description": "A deterministic protected fact.",
                    "content": "A protected fact.",
                },
            ),
            "tool_execution": [],
        }

    monkeypatch.setattr(AsAgentWrapper, "reply", proposal_reply)
    request = {
        "session_id": "forged-transition-session",
        "messages": [{"id": "m1", "name": "user", "role": "user", "content": "fact"}],
        "date": "2026-10-21",
    }
    note_path = workspace / "daily/2026-10-21/forged-transition.md"
    async with Application(**config) as app:
        first = await app.run_job("auto_memory", request_id="forged-a", **request)
        assert first.success
        receipt_path = next((workspace / "metadata/auto_memory_requests").rglob("*.json"))
        saved = frontmatter.loads(note_path.read_text(encoding="utf-8"))
        if forgery == "marker":
            saved.metadata["_reme_commit"]["agent_scope"] = "forged-scope"
            note_path.write_text(frontmatter.dumps(saved), encoding="utf-8")
        else:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            receipt["note_sha256"] = "0" * 64
            receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        before = note_path.read_bytes()
        second = await app.run_job("auto_memory", request_id="forged-b", **request)

    assert not second.success
    assert len(calls) == 1
    assert note_path.read_bytes() == before


@pytest.mark.asyncio
async def test_option_b_orphaned_marker_cannot_be_superseded_without_receipt(tmp_path, monkeypatch):
    from reme.components.agent_wrapper.as_agent_wrapper import AsAgentWrapper
    from reme.config import resolve_app_config

    workspace = tmp_path / "workspace"
    config_path = Path(__file__).resolve().parents[2] / "reme/config/fermes-hermes-stage1.yaml"
    config = resolve_app_config(config=str(config_path), workspace_dir=str(workspace), log_config=False)
    calls = []

    async def proposal_reply(_self, _inputs, **_kwargs):
        calls.append(1)
        return {
            "result": json.dumps(
                {
                    "path": "daily/2026-10-21/orphaned-transition.md",
                    "name": "orphaned-transition",
                    "description": "A deterministic protected fact.",
                    "content": "A protected fact.",
                },
            ),
            "tool_execution": [],
        }

    monkeypatch.setattr(AsAgentWrapper, "reply", proposal_reply)
    request = {
        "session_id": "orphaned-transition-session",
        "messages": [{"id": "m1", "name": "user", "role": "user", "content": "fact"}],
        "date": "2026-10-21",
    }
    note_path = workspace / "daily/2026-10-21/orphaned-transition.md"
    async with Application(**config) as app:
        first = await app.run_job("auto_memory", request_id="orphaned-a", **request)
        assert first.success
        receipt_path = next((workspace / "metadata/auto_memory_requests").rglob("*.json"))
        receipt_path.unlink()
        before = note_path.read_bytes()
        second = await app.run_job("auto_memory", request_id="orphaned-b", **request)

    assert not second.success
    assert len(calls) == 1
    assert note_path.read_bytes() == before


@pytest.mark.asyncio
async def test_failed_request_can_retry_but_reusing_a_completed_id_for_other_input_fails(tmp_path):
    step = _step(tmp_path)

    async def fail():
        step.context.response.success = False

    step._execute = fail
    await step.execute()
    assert not list(tmp_path.rglob("*.json"))

    retry = _step(tmp_path)

    async def succeed():
        return None

    retry._execute = succeed
    await retry.execute()
    assert retry.context.response.success
    conflicting = _step(tmp_path)
    conflicting.context["messages"] = [{"id": "m2", "content": "different fact"}]
    await conflicting.execute()
    assert not conflicting.context.response.success
    assert "different input" in conflicting.context.response.answer


def test_failed_durable_tool_calls_are_not_treated_as_a_noop():
    failed_reply = {
        "last_message": {
            "content": [
                {"type": "tool_call", "name": "daily_write", "input": "{}"},
                {"type": "tool_result", "name": "daily_write", "state": "error"},
                {"type": "text", "text": "I reached the maximum number of reasoning iterations."},
            ],
        },
    }

    assert AutoMemoryStep._all_requested_writes_failed(failed_reply, ["daily_write"])
    assert not AutoMemoryStep._all_requested_writes_failed(
        {"last_message": {"content": [{"type": "text", "text": "Nothing to remember."}]}},
        ["daily_write"],
    )
    assert not AutoMemoryStep._all_requested_writes_failed(
        {
            "last_message": {
                "content": [
                    {"type": "tool_call", "name": "daily_write", "id": "c1", "input": "{}"},
                    {"type": "tool_result", "name": "daily_write", "id": "c1", "state": "success"},
                ],
            },
        },
        ["daily_write"],
    )


@pytest.mark.parametrize(
    "execution",
    [
        [{"type": "tool_call", "name": "daily_write", "call_id": "c1"}],
        [
            {"type": "tool_call", "name": "daily_write", "call_id": "c1"},
            {"type": "tool_result", "name": "daily_write", "call_id": "unknown", "state": "error"},
        ],
        [
            {"type": "tool_call", "name": "daily_write", "call_id": "c1"},
            {"type": "tool_call", "name": "daily_write", "call_id": "c1"},
            {"type": "tool_result", "name": "daily_write", "call_id": "c1", "state": "error"},
        ],
        [
            {"type": "tool_call", "name": "daily_write", "call_id": "c1"},
            {"type": "tool_result", "name": "daily_write", "call_id": "c1", "state": "error"},
            {"type": "tool_result", "name": "daily_write", "call_id": "c1", "state": "error"},
        ],
        [
            {"type": "tool_call", "name": "daily_write", "call_id": "c1"},
            {"type": "tool_result", "name": "daily_write", "call_id": "c1", "state": "interrupted"},
        ],
        [
            {"type": "tool_call", "name": "daily_write", "call_id": "c1"},
            {"type": "tool_result", "name": "daily_write", "call_id": "c1", "state": "not-a-state"},
        ],
        [
            {"type": "tool_call", "name": "daily_write", "call_id": "c1"},
            {"type": "tool_result", "name": "other_tool", "call_id": "c1", "state": "error"},
        ],
    ],
)
def test_durable_tool_execution_fails_closed_for_invalid_pairing(execution):
    assert AutoMemoryStep._all_requested_writes_failed({"tool_execution": execution}, ["daily_write"])


def test_same_name_tool_calls_pair_by_call_id_and_mixed_results_fail_closed():
    execution = [
        {"type": "tool_call", "name": "daily_write", "call_id": "c1"},
        {"type": "tool_call", "name": "daily_write", "call_id": "c2"},
        {"type": "tool_result", "name": "daily_write", "call_id": "c1", "state": "success"},
        {"type": "tool_result", "name": "daily_write", "call_id": "c2", "state": "error"},
    ]
    assert AutoMemoryStep._all_requested_writes_failed({"tool_execution": execution}, ["daily_write"])


def test_current_turn_without_tools_does_not_reuse_previous_turn_execution():
    assert not AutoMemoryStep._all_requested_writes_failed(
        {
            "tool_execution": [],
            "last_message": {
                "content": [
                    {"type": "tool_call", "name": "daily_write", "call_id": "old"},
                    {"type": "tool_result", "name": "daily_write", "call_id": "old", "state": "error"},
                ],
            },
        },
        ["daily_write"],
    )


def test_same_name_tool_calls_with_distinct_success_ids_are_not_failures():
    execution = [
        {"type": "tool_call", "name": "daily_write", "call_id": "c1"},
        {"type": "tool_call", "name": "daily_write", "call_id": "c2"},
        {"type": "tool_result", "name": "daily_write", "call_id": "c2", "state": "success"},
        {"type": "tool_result", "name": "daily_write", "call_id": "c1", "state": "success"},
    ]
    assert not AutoMemoryStep._all_requested_writes_failed({"tool_execution": execution}, ["daily_write"])


@pytest.mark.asyncio
async def test_receipts_obey_workspace_permissions(tmp_path):
    step = _step(tmp_path, _allowed_paths=["daily"])
    with pytest.raises(PermissionError):
        await step.execute()
    assert not list(tmp_path.rglob("*.json"))


@pytest.mark.asyncio
@pytest.mark.parametrize("request_id", ["", "x" * 129, 123])
async def test_rejects_invalid_request_id_before_running_agent(tmp_path, request_id):
    step = _step(tmp_path, request_id)
    await step.execute()
    assert not step.context.response.success
    assert "request_id" in step.context.response.answer
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_execution", "expected_success", "memory_hint"),
    [
        (None, False, ""),
        ([{"type": "tool_call", "name": "daily_write", "call_id": "c1"}], False, ""),
        (
            [
                {"type": "tool_call", "name": "daily_write", "call_id": "c1"},
                {"type": "tool_result", "name": "daily_write", "call_id": "wrong", "state": "success"},
            ],
            False,
            "",
        ),
        (
            [
                {"type": "tool_call", "name": "daily_write", "call_id": "c1"},
                {"type": "tool_call", "name": "daily_write", "call_id": "c2"},
                {"type": "tool_result", "name": "daily_write", "call_id": "c1", "state": "success"},
                {"type": "tool_result", "name": "daily_write", "call_id": "c2", "state": "error"},
            ],
            False,
            "",
        ),
        (
            [
                {"type": "tool_call", "name": "daily_write", "call_id": "c1"},
                {"type": "tool_result", "name": "daily_write", "call_id": "c1", "state": "success"},
            ],
            False,
            "",
        ),
        ([], True, ""),
        ([], False, "must remember this"),
        ([], True, "   "),
    ],
)
async def test_auto_memory_receipt_requires_current_turn_evidence(
    tmp_path,
    monkeypatch,
    tool_execution,
    expected_success,
    memory_hint,
):
    """Real job execution must not acknowledge an unverified AgentScope turn."""
    from reme.components.agent_wrapper.as_agent_wrapper import AsAgentWrapper
    from reme.config import resolve_app_config

    config_path = Path(__file__).resolve().parents[2] / "reme/config/default.yaml"
    config = resolve_app_config(config=str(config_path), workspace_dir=str(tmp_path / "workspace"), log_config=False)

    async def simulated_reply(_self, _inputs, **_kwargs):
        result = {"result": "done", "last_message": {"content": [{"type": "text", "text": "done"}]}}
        if tool_execution is not None:
            result["tool_execution"] = tool_execution
        return result

    monkeypatch.setattr(AsAgentWrapper, "reply", simulated_reply)
    app = Application(**config)
    async with app:
        response = await app.run_job(
            "auto_memory",
            request_id="current-turn-contract-test",
            session_id="synthetic-session",
            messages=[{"id": "m1", "name": "user", "role": "user", "content": "Synthetic calibration record."}],
            date="2026-10-21",
            memory_hint=memory_hint,
        )
    assert response.success is expected_success
    receipts = list((tmp_path / "workspace" / "metadata" / "auto_memory_requests").rglob("*.json"))
    assert bool(receipts) is expected_success


@pytest.mark.asyncio
async def test_provenance_mismatch_does_not_create_success_receipt(tmp_path, monkeypatch):
    """A forged note after an apparently successful tool result remains retryable."""
    from reme.components.agent_wrapper.as_agent_wrapper import AsAgentWrapper
    from reme.config import resolve_app_config

    workspace = tmp_path / "workspace"
    config_path = Path(__file__).resolve().parents[2] / "reme/config/default.yaml"
    config = resolve_app_config(config=str(config_path), workspace_dir=str(workspace), log_config=False)
    note_path = "daily/2026-10-21/forged-note.md"
    note_file = workspace / note_path

    async def simulated_reply(_self, _inputs, **_kwargs):
        note_file.parent.mkdir(parents=True, exist_ok=True)
        note_file.write_text(
            "---\nname: forged-note\nsession_id: synthetic-session\n"
            "source_conversation: '[[session/dialog/synthetic-session.jsonl]]'\n"
            "source_hash: {algorithm: sha256, format: hex, value: deadbeef}\n"
            "---\nsynthetic fact\n",
            encoding="utf-8",
        )
        return {
            "result": "done",
            "tool_execution": [
                {"type": "tool_call", "name": "daily_write", "call_id": "c1"},
                {"type": "tool_result", "name": "daily_write", "call_id": "c1", "state": "success"},
            ],
        }

    async def find_note(_self, _day, _session_id):
        return {"path": note_path} if note_file.exists() else None

    monkeypatch.setattr(AsAgentWrapper, "reply", simulated_reply)
    monkeypatch.setattr(AutoMemoryStep, "_list_session_note", find_note)
    app = Application(**config)
    async with app:
        response = await app.run_job(
            "auto_memory",
            request_id="forged-source-hash",
            session_id="synthetic-session",
            messages=[{"id": "m1", "name": "user", "role": "user", "content": "Synthetic fact."}],
            date="2026-10-21",
        )
    assert not response.success
    assert "provenance verification failed" in response.answer
    assert not list((workspace / "metadata" / "auto_memory_requests").rglob("*.json"))


@pytest.mark.asyncio
async def test_receipt_replay_rejects_tampered_note_provenance(tmp_path, monkeypatch):
    """A cached success cannot acknowledge a deleted or altered durable note."""
    from reme.components.agent_wrapper.as_agent_wrapper import AsAgentWrapper
    from reme.config import resolve_app_config

    workspace = tmp_path / "workspace"
    note_path = "daily/2026-10-21/existing.md"
    note = workspace / note_path
    note.parent.mkdir(parents=True)
    note.write_text(
        "---\nname: existing\nsession_id: synthetic-session\n"
        "source_conversation: '[[session/dialog/synthetic-session.jsonl]]'\n"
        "---\nSynthetic fact.\n",
        encoding="utf-8",
    )
    config_path = Path(__file__).resolve().parents[2] / "reme/config/default.yaml"
    config = resolve_app_config(config=str(config_path), workspace_dir=str(workspace), log_config=False)
    calls = []

    async def no_tool_reply(_self, _inputs, **_kwargs):
        calls.append(1)
        return {
            "result": "no change",
            "tool_execution": [],
            "last_message": {"content": [{"type": "text", "text": "no change"}]},
        }

    async def find_existing(_self, _day, _session_id):
        return {"path": note_path}

    monkeypatch.setattr(AsAgentWrapper, "reply", no_tool_reply)
    monkeypatch.setattr(AutoMemoryStep, "_list_session_note", find_existing)
    job_kwargs = {
        "request_id": "replay-tamper-proof",
        "session_id": "synthetic-session",
        "messages": [{"id": "m1", "name": "user", "role": "user", "content": "Synthetic fact."}],
        "date": "2026-10-21",
    }
    app = Application(**config)
    async with app:
        first = await app.run_job("auto_memory", **job_kwargs)
        assert first.success
        assert list((workspace / "metadata" / "auto_memory_requests").rglob("*.json"))
        initial_model_calls = len(calls)
        saved = frontmatter.loads(note.read_text(encoding="utf-8"))
        assert saved.metadata.get("source_hash")
        saved.metadata["source_hash"]["value"] = "0" * 64
        note.write_text(frontmatter.dumps(saved), encoding="utf-8")
        repeated = await app.run_job("auto_memory", **job_kwargs)
        assert not repeated.success
        assert "provenance" in repeated.answer
        assert len(calls) == initial_model_calls


@pytest.mark.asyncio
async def test_auto_memory_repair_cannot_claim_foreign_session_note(tmp_path, monkeypatch):
    """Internal frontmatter repair must honour the same ownership boundary as tools."""
    from reme.components.agent_wrapper.as_agent_wrapper import AsAgentWrapper
    from reme.config import resolve_app_config

    workspace = tmp_path / "workspace"
    note_path = "daily/2026-10-21/existing.md"
    note = workspace / note_path
    note.parent.mkdir(parents=True)
    original = (
        "---\nname: existing\nsession_id: foreign-session\n"
        "source_conversation: '[[session/dialog/foreign-session.jsonl]]'\n"
        "---\nForeign content.\n"
    )
    note.write_text(original, encoding="utf-8")
    config_path = Path(__file__).resolve().parents[2] / "reme/config/default.yaml"
    config = resolve_app_config(config=str(config_path), workspace_dir=str(workspace), log_config=False)

    async def no_tool_reply(_self, _inputs, **_kwargs):
        return {"result": "done", "tool_execution": [], "last_message": {"content": [{"type": "text", "text": "done"}]}}

    async def find_foreign(_self, _day, _session_id):
        return {"path": note_path}

    monkeypatch.setattr(AsAgentWrapper, "reply", no_tool_reply)
    monkeypatch.setattr(AutoMemoryStep, "_list_session_note", find_foreign)
    app = Application(**config)
    async with app:
        response = await app.run_job(
            "auto_memory",
            request_id="foreign-note-repair",
            session_id="synthetic-session",
            messages=[{"id": "m1", "name": "user", "role": "user", "content": "Synthetic fact."}],
            date="2026-10-21",
        )
    assert not response.success
    assert "different source session" in response.answer
    assert note.read_text(encoding="utf-8") == original
    assert not list((workspace / "metadata" / "auto_memory_requests").rglob("*.json"))
