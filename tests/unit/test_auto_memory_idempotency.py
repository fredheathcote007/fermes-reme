"""Completed automatic-memory requests can be replayed after a client timeout."""

# pylint: disable=protected-access,missing-function-docstring

import asyncio
from types import SimpleNamespace

import pytest
from agentscope.message import Msg

from reme.application import Application
from reme.components.runtime_context import RuntimeContext
from reme.steps.evolve.auto_memory import AutoMemoryStep


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
        first.context.response.metadata = {"path": "daily/note.md"}
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
    ("tool_execution", "expected_success"),
    [
        (None, False),
        ([{"type": "tool_call", "name": "daily_write", "call_id": "c1"}], False),
        (
            [
                {"type": "tool_call", "name": "daily_write", "call_id": "c1"},
                {"type": "tool_result", "name": "daily_write", "call_id": "wrong", "state": "success"},
            ],
            False,
        ),
        (
            [
                {"type": "tool_call", "name": "daily_write", "call_id": "c1"},
                {"type": "tool_call", "name": "daily_write", "call_id": "c2"},
                {"type": "tool_result", "name": "daily_write", "call_id": "c1", "state": "success"},
                {"type": "tool_result", "name": "daily_write", "call_id": "c2", "state": "error"},
            ],
            False,
        ),
        ([], True),
    ],
)
async def test_auto_memory_receipt_requires_current_turn_evidence(
    tmp_path, monkeypatch, tool_execution, expected_success
):
    """Real job execution must not acknowledge an unverified AgentScope turn."""
    from pathlib import Path

    from reme.components.agent_wrapper.as_agent_wrapper import AsAgentWrapper
    from reme.config import resolve_app_config

    config_path = Path(__file__).resolve().parents[2] / "reme/config/fermes-hermes-stage1.yaml"
    config = resolve_app_config(
        config=str(config_path), workspace_dir=str(tmp_path / "workspace"), log_config=False
    )

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
        )
    assert response.success is expected_success
    receipts = list((tmp_path / "workspace" / "metadata" / "auto_memory_requests").rglob("*.json"))
    assert bool(receipts) is expected_success
