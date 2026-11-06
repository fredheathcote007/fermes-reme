"""Completed automatic-memory requests can be replayed after a client timeout."""

# pylint: disable=protected-access,missing-function-docstring,too-many-lines

import asyncio
import errno
import hashlib
import json
import os
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import frontmatter
import pytest
import yaml
from agentscope.message import Msg

from reme.application import Application
from reme.components.agent_wrapper.base_agent_wrapper import BaseAgentWrapper
from reme.components.prompt_handler import PromptHandler
from reme.components.runtime_context import RuntimeContext
from reme.steps.evolve import auto_memory as auto_memory_module
from reme.steps.evolve.auto_memory import AutoMemoryStep

from .test_hermes_agent_integration import PLUGIN_MODULE, ReMeBackendError

_POSIX_DURABLE_COMMIT = pytest.mark.skipif(
    os.name != "posix",
    reason="requires POSIX durable no-overwrite commit semantics",
)


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


@pytest.mark.skipif(os.name != "posix", reason="direct os.link publication test requires POSIX semantics")
@pytest.mark.asyncio
async def test_option_b_new_file_does_not_overwrite_existing_target_or_leave_temp(tmp_path, monkeypatch):
    target = tmp_path / "existing.md"
    original = b"original note bytes"
    target.write_bytes(original)

    async def durable_stub(path, content, _encoding="utf-8"):
        path.write_bytes(content if isinstance(content, bytes) else content.encode(_encoding))

    async def inline(func, *args):
        return func(*args)

    monkeypatch.setattr(auto_memory_module, "write_file_durable", durable_stub)
    monkeypatch.setattr(auto_memory_module, "complete_in_thread", inline)
    published = await auto_memory_module._write_new_file_durable(target, b"replacement note bytes")

    assert published is False
    assert target.read_bytes() == original
    assert not list(tmp_path.glob(".reme-*.create.tmp"))


@_POSIX_DURABLE_COMMIT
@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["write", "scheduler"])
async def test_option_b_new_file_cleans_temp_when_staging_fails(tmp_path, monkeypatch, failure):
    """All failures after temp allocation must remove the private staging file."""
    target = tmp_path / "new.md"

    async def durable_stub(path, content, _encoding="utf-8"):
        path.write_bytes(content if isinstance(content, bytes) else content.encode(_encoding))
        if failure == "write":
            raise OSError("synthetic staging failure")

    async def scheduler_stub(_func, *_args):
        raise RuntimeError("synthetic scheduler failure")

    monkeypatch.setattr(auto_memory_module, "write_file_durable", durable_stub)
    if failure == "scheduler":
        monkeypatch.setattr(auto_memory_module, "complete_in_thread", scheduler_stub)

    with pytest.raises((OSError, RuntimeError)):
        await auto_memory_module._write_new_file_durable(target, b"new note bytes")

    assert not list(tmp_path.glob(".reme-*.create.tmp"))


@pytest.mark.asyncio
async def test_option_b_new_file_rejects_unsupported_platform_before_staging(tmp_path, monkeypatch):
    """Unsupported durable commits fail before the private staging write."""
    target = tmp_path / "new.md"
    staging_calls = []

    async def durable_stub(*_args, **_kwargs):
        staging_calls.append(True)

    monkeypatch.setattr(auto_memory_module, "_durable_local_posix_commits_available", lambda: False)
    monkeypatch.setattr(auto_memory_module, "write_file_durable", durable_stub)

    with pytest.raises(OSError, match="durable local POSIX file commits are unavailable"):
        await auto_memory_module._write_new_file_durable(target, b"new note bytes")

    assert not staging_calls
    assert not list(tmp_path.glob(".reme-*.create.tmp"))


@pytest.mark.skipif(os.name != "nt", reason="requires the native Windows durable-commit predicate")
@pytest.mark.asyncio
async def test_windows_protected_commit_fails_closed_before_staging(tmp_path, monkeypatch):
    """Native Windows rejects protected session-save before publication, temp, or receipt."""
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
                    "path": "daily/2026-10-21/windows-protected.md",
                    "name": "windows-protected",
                    "description": "A deterministic protected fact.",
                    "content": "A protected fact.",
                },
            ),
            "tool_execution": [],
        }

    assert not auto_memory_module._durable_local_posix_commits_available()
    monkeypatch.setattr(AsAgentWrapper, "reply", proposal_reply)
    request = {
        "session_id": "windows-protected-session",
        "messages": [{"id": "m1", "name": "user", "role": "user", "content": "fact"}],
        "date": "2026-10-21",
    }
    async with Application(**config) as app:
        response = await app.run_job("auto_memory", request_id="windows-protected-request", **request)

    assert not response.success
    assert "durable local POSIX file commits are unavailable" in response.answer
    assert not calls
    assert not list((workspace / "session/dialog").glob("*.jsonl"))
    assert not list(workspace.rglob(".reme-*.create.tmp"))
    assert not list((workspace / "metadata/auto_memory_requests").rglob("*.json"))
    assert not list((workspace / "daily").rglob("*.md"))


@_POSIX_DURABLE_COMMIT
@pytest.mark.asyncio
async def test_option_b_new_file_does_not_reallocate_after_post_commit_cleanup_failure(tmp_path, monkeypatch):
    """A post-commit long-path cleanup failure cannot turn one note into two."""
    target = tmp_path / "new.md"
    real_unlink = Path.unlink

    def fail_long_create_temp(path, missing_ok=False):
        if path.name.startswith(".reme-") and path.name.endswith(".create.tmp"):
            raise OSError(errno.ENAMETOOLONG, "synthetic absolute cleanup path too long")
        return real_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", fail_long_create_temp)
    result = await auto_memory_module._write_new_file_durable(target, b"new note bytes")

    assert result is True
    assert target.read_bytes() == b"new note bytes"
    assert not list(tmp_path.glob(".reme-*.create.tmp"))


@_POSIX_DURABLE_COMMIT
@pytest.mark.asyncio
async def test_option_b_new_file_swallows_post_commit_relative_cleanup_failure(tmp_path, monkeypatch):
    """A failure from relative cleanup after fsync still returns the committed result."""
    target = tmp_path / "new.md"
    real_os = auto_memory_module.os
    cleanup_calls = []

    class CleanupFailureOS:
        """Proxy ``os`` while forcing the relative temporary-file cleanup to fail."""

        def __getattr__(self, name):
            return getattr(real_os, name)

        def unlink(self, path, *args, **kwargs):
            if kwargs.get("dir_fd") is not None and str(path).startswith(".reme-"):
                cleanup_calls.append(path)
                raise OSError(errno.ENAMETOOLONG, "synthetic relative cleanup failure")
            return real_os.unlink(path, *args, **kwargs)

    async def durable_stub(path, content, _encoding="utf-8"):
        path.write_bytes(content if isinstance(content, bytes) else content.encode(_encoding))

    async def inline(func, *args):
        return func(*args)

    monkeypatch.setattr(auto_memory_module, "os", CleanupFailureOS())
    monkeypatch.setattr(auto_memory_module, "write_file_durable", durable_stub)
    monkeypatch.setattr(auto_memory_module, "complete_in_thread", inline)

    result = await auto_memory_module._write_new_file_durable(target, b"new note bytes")

    assert result is True
    assert target.read_bytes() == b"new note bytes"
    assert len(cleanup_calls) == 1


@_POSIX_DURABLE_COMMIT
@pytest.mark.asyncio
async def test_option_b_new_file_preserves_post_link_fsync_failure(tmp_path, monkeypatch):
    """Cleanup errors after linking cannot mask a failed directory fsync."""
    target = tmp_path / "new.md"
    real_os = auto_memory_module.os

    class FsyncFailureOS:
        """Proxy ``os`` while failing publication durability and cleanup."""

        def __getattr__(self, name):
            return getattr(real_os, name)

        def fsync(self, _fd):
            raise OSError(errno.EIO, "synthetic directory fsync failure")

        def unlink(self, path, *args, **kwargs):
            if kwargs.get("dir_fd") is not None and str(path).startswith(".reme-"):
                raise OSError(errno.ENAMETOOLONG, "synthetic relative cleanup failure")
            return real_os.unlink(path, *args, **kwargs)

    async def durable_stub(path, content, _encoding="utf-8"):
        path.write_bytes(content if isinstance(content, bytes) else content.encode(_encoding))

    async def inline(func, *args):
        return func(*args)

    monkeypatch.setattr(auto_memory_module, "os", FsyncFailureOS())
    monkeypatch.setattr(auto_memory_module, "write_file_durable", durable_stub)
    monkeypatch.setattr(auto_memory_module, "complete_in_thread", inline)

    with pytest.raises(OSError) as raised:
        await auto_memory_module._write_new_file_durable(target, b"new note bytes")

    assert raised.value.errno == errno.EIO
    assert target.read_bytes() == b"new note bytes"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "note_path",
    [
        "../x.md",
        "daily/2026-10-21/x.txt",
        "daily/2026-10-21/invalid\ud800.md",
        f"daily/{'x' * 256}.md",
    ],
)
async def test_option_b_receipt_note_path_is_rejected_before_storage_lookup(tmp_path, monkeypatch, note_path):
    step = _step(tmp_path, request_id="replay-request")
    step.kwargs["agent_scope"] = "test-scope"
    monkeypatch.setattr(
        auto_memory_module,
        "trusted_provenance",
        lambda *_args, **_kwargs: pytest.fail("invalid receipt path reached filesystem lookup"),
    )

    with pytest.raises(ValueError, match="invalid protected note path"):
        await step._replay_option_b_receipt(
            {
                "version": 1,
                "fingerprint": "fingerprint",
                "outcome": "commit",
                "commit": {"note_path": note_path},
            },
            fingerprint="fingerprint",
            session_id="session-1",
        )


@_POSIX_DURABLE_COMMIT
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("note_path", "permanent", "filesystem_setup"),
    [
        ("../x.md", True, None),
        ("daily/2026-10-21/x.txt", True, None),
        ("daily/2026-10-21/invalid\ud800.md", True, None),
        (f"daily/2026-10-21/{'x' * 256}.md", True, None),
        ("daily/2026-10-21/gone.md", False, None),
        ("daily/2026-10-21/path component cleanup.md", False, None),
        ("daily/2026-10-21/protected note marker.md", False, None),
        ("daily/2026-10-21/protected note body digest.md", False, None),
        ("daily/2026-10-21/frontmatter key collision.md", False, None),
        ("daily/2026-10-21/unreadable.md", False, "directory"),
        ("daily/2026-10-21/symlink.md", False, "symlink"),
    ],
)
async def test_tampered_receipt_replay_answer_drives_retry_classifier(
    tmp_path,
    monkeypatch,
    note_path,
    permanent,
    filesystem_setup,
):
    """Classify the real replay answer, including structural and transient path failures."""
    from reme.components.agent_wrapper.as_agent_wrapper import AsAgentWrapper
    from reme.config import resolve_app_config

    workspace = tmp_path / "workspace"
    config_path = Path(__file__).resolve().parents[2] / "reme/config/fermes-hermes-stage1.yaml"
    config = resolve_app_config(config=str(config_path), workspace_dir=str(workspace), log_config=False)

    async def proposal_reply(_self, _inputs, **_kwargs):
        return {
            "result": json.dumps(
                {
                    "path": "daily/2026-10-21/tamper-proof.md",
                    "name": "tamper-proof",
                    "description": "A deterministic protected fact.",
                    "content": "A protected fact.",
                },
            ),
            "tool_execution": [],
        }

    monkeypatch.setattr(AsAgentWrapper, "reply", proposal_reply)
    request = {
        "session_id": "tamper-path-session",
        "messages": [{"id": "m1", "name": "user", "role": "user", "content": "fact"}],
        "date": "2026-10-21",
    }
    async with Application(**config) as app:
        first = await app.run_job("auto_memory", request_id="tamper-path-request", **request)
        assert first.success
        receipt_path = next((workspace / "metadata/auto_memory_requests").rglob("*.json"))
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt["commit"]["note_path"] = note_path
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

        if filesystem_setup == "directory":
            (workspace / note_path).mkdir()
        elif filesystem_setup == "symlink":
            (workspace / note_path).symlink_to(workspace / first.metadata["path"])

        replay = await app.run_job("auto_memory", request_id="tamper-path-request", **request)

    assert not replay.success
    assert PLUGIN_MODULE._is_permanent_write_failure(ReMeBackendError(replay.answer)) is permanent
    if permanent:
        assert replay.answer == "Error: protected receipt replay refused: invalid protected note path"


def test_option_b_invalid_yaml_marker_is_a_protected_note_refusal(tmp_path):
    raw = "---\n_reme_commit: [\nrequest_id: marker-request\n---\nprotected body\n"
    with pytest.raises(yaml.YAMLError):
        frontmatter.loads(raw)
    note = tmp_path / "daily/2026-10-21/broken.md"
    note.parent.mkdir(parents=True)
    note.write_text(raw, encoding="utf-8")

    step = _step(tmp_path)
    with pytest.raises(ValueError, match="protected note is unreadable"):
        step._read_commit_note(
            "daily/2026-10-21/broken.md",
            request_id="marker-request",
            fingerprint="fingerprint",
            session_id="session-1",
            source_hash=None,
            agent_scope="test-scope",
        )


def test_option_b_cross_day_invalid_yaml_marker_fails_closed(tmp_path):
    raw = "---\n_reme_commit: [\nrequest_id: cross-day-request\n---\nprotected body\n"
    with pytest.raises(yaml.YAMLError):
        frontmatter.loads(raw)
    note = tmp_path / "daily/2026-10-20/broken.md"
    note.parent.mkdir(parents=True)
    note.write_text(raw, encoding="utf-8")

    step = _step(tmp_path)
    step.config_value = lambda key: "daily" if key == "daily_dir" else None
    with pytest.raises(RuntimeError, match="protected daily note is unreadable"):
        step._find_cross_day_option_b_marker(
            request_id="cross-day-request",
            fingerprint="fingerprint",
            session_id="session-1",
            agent_scope="test-scope",
        )


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
@pytest.mark.parametrize(
    ("messages", "tamper", "expected"),
    [
        pytest.param(
            [{"id": "m1", "name": "user", "role": "user", "content": "fact"}],
            "response-object",
            "response is malformed",
            marks=_POSIX_DURABLE_COMMIT,
        ),
        pytest.param(
            [{"id": "m1", "name": "user", "role": "user", "content": "fact"}],
            "commit-object",
            "does not contain a successful commit",
            marks=_POSIX_DURABLE_COMMIT,
        ),
        pytest.param(
            [{"id": "m1", "name": "user", "role": "user", "content": "fact"}],
            "response-false",
            "does not contain a matching successful commit",
            marks=_POSIX_DURABLE_COMMIT,
        ),
        ([], "noop-commit", "no-op receipt contains commit evidence"),
        ([], "noop-response", "no-op receipt response is malformed"),
        ([], "noop-false", "no-op receipt response is malformed"),
    ],
)
async def test_application_run_job_rejects_tampered_protected_receipt(
    tmp_path,
    monkeypatch,
    messages,
    tamper,
    expected,
):
    from reme.components.agent_wrapper.as_agent_wrapper import AsAgentWrapper
    from reme.config import resolve_app_config

    workspace = tmp_path / "workspace"
    config_path = Path(__file__).resolve().parents[2] / "reme/config/fermes-hermes-stage1.yaml"
    config = resolve_app_config(config=str(config_path), workspace_dir=str(workspace), log_config=False)

    async def proposal_reply(_self, _inputs, **_kwargs):
        return {
            "result": json.dumps(
                {
                    "path": "daily/2026-10-21/tamper-proof.md",
                    "name": "tamper-proof",
                    "description": "A deterministic protected fact.",
                    "content": "A protected fact.",
                },
            ),
            "tool_execution": [],
        }

    monkeypatch.setattr(AsAgentWrapper, "reply", proposal_reply)
    request = {"session_id": "tamper-session", "messages": messages, "date": "2026-10-21"}
    async with Application(**config) as app:
        first = await app.run_job("auto_memory", request_id="tamper-request", **request)
        assert first.success
        receipt_path = next((workspace / "metadata/auto_memory_requests").rglob("*.json"))
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        if tamper == "response-object":
            receipt["response"] = []
        elif tamper == "commit-object":
            receipt["commit"] = []
        elif tamper == "response-false":
            receipt["response"]["success"] = False
        elif tamper == "noop-commit":
            receipt["commit"] = {"forged": True}
        elif tamper == "noop-response":
            receipt["response"] = []
        else:
            receipt["response"]["success"] = False
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        replay = await app.run_job("auto_memory", request_id="tamper-request", **request)

    assert not replay.success
    assert expected in replay.answer


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
async def test_protected_empty_turn_has_idempotent_noop_receipt_without_commit_or_model_call(tmp_path):
    """An empty protected turn is replayable without a transcript or fake commit."""

    def make_step():
        step = AutoMemoryStep(durable_commit=True, agent_scope="test-scope")
        step.file_store = SimpleNamespace(workspace_path=tmp_path)
        step.agent_wrapper = _SecurityTestWrapper(backend="agentscope")
        step.context = RuntimeContext(
            request_id="protected-empty-turn",
            session_id="protected-empty-session",
            messages=[],
            date="2026-10-21",
        )
        return step

    first = make_step()
    await first.execute()
    receipts = list(tmp_path.rglob("*.json"))
    assert len(receipts) == 1
    record = json.loads(receipts[0].read_text(encoding="utf-8"))
    assert record["outcome"] == "noop"
    assert record["changes"] == []
    assert "commit" not in record
    assert "note_sha256" not in record
    assert first.context.response.metadata["outcome"] == "noop"
    assert "commit" not in first.context.response.metadata
    assert not list(tmp_path.rglob("dialog/*.jsonl"))

    replay = make_step()
    await replay.execute()
    assert replay.context.response == first.context.response
    assert replay.context["changes"] == []
    assert not list(tmp_path.rglob("dialog/*.jsonl"))


@pytest.mark.parametrize("marker", ["`", "~"])
def test_option_b_inline_fence_scan_remains_linear(marker):
    """Adversarial unmatched fence runs must not stall the event loop."""
    content = marker * 65536 + "a" + marker * 65536 + "b"
    start = time.perf_counter()
    assert AutoMemoryStep._is_empty_option_b_content(content) is False
    assert time.perf_counter() - start < 2.0


@_POSIX_DURABLE_COMMIT
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "accepted"),
    [
        (
            "This is a placeholder for the daily entry, "
            "capturing any relevant information and observations for the day.",
            False,
        ),
        ("The user moved to Berlin and starts work on Monday.", True),
        ("Today's daily note captures relevant observations: the user moved to Berlin on Monday.", True),
        (
            "# Daily Note – 2026-11-06\n\nThis is a placeholder for the daily entry, "
            "capturing any relevant information and observations for the day.\n",
            False,
        ),
        ("# Daily Note – 2026-11-06", False),
        ("Placeholder for today's entry.", False),
        ("Placeholder for today's daily note.", False),
        (
            "# Daily Note – 2026-11-06\n\n---\n\nThis is a placeholder for the daily entry, "
            "capturing any relevant information and observations for the day.\n---",
            False,
        ),
        ("```\n# Daily Note – 2026-11-06\nPlaceholder for today's daily entry.\n```", False),
        ("---", False),
        ("-", False),
        ("#", False),
        ("🎯\n---", True),
        ("```markdown\n# Daily Note – 2026-11-06\nPlaceholder for today's daily entry.\n```", False),
        ("~~~md\nPlaceholder for the daily entry.\n~~~", False),
        ("```text\nUser moved to Berlin.\n```", True),
        ("Placeholder for today's daily note: the user moved to Berlin.", True),
        ("|:---|:---:|", False),
        ("- [ ]\nPlaceholder for today's daily note.", False),
        ("# Daily Notes – 2026-11-06\nPlaceholder for today's daily note.", False),
        ("Placeholder for today's daily notes.", False),
        ("``` markdown\nPlaceholder for today's daily note.\n```", False),
        ("```c#\nPlaceholder for today's daily note.\n```", False),
        ("```json {1-3}\nPlaceholder for today's daily note.\n```", False),
        ("~~~ md\nUser moved to Berlin.\n~~~", True),
        ("🎯\n|:---|:---:|", True),
        ("Placeholder for today's daily notes: final migration approved.", True),
        ("```User moved to Berlin```", True),
        ("~~~User moved to Berlin~~~", True),
        ("``` User prefers tea```", True),
        ("~~~~ moved to Berlin~~~~", True),
        ("```Placeholder for today's daily note.```", False),
        ("```npm``` is the user's package manager", True),
        ("```Berlin``` and Paris", True),
        ("~~~npm~~~ is the preferred tool", True),
        ("```markdown\nPlaceholder for today's daily note.\n```", False),
        ("No new information.", False),
        ("用户搬到了柏林，周一开始上班。", True),
        ("ユーザーは月曜日に仕事を始めます。", True),
        ("Пользователь переехал в Берлин и начинает работу в понедельник.", True),
        ("🎯", True),
    ],
)
async def test_option_b_rejects_template_only_proposal_but_commits_substantive_content(
    tmp_path,
    monkeypatch,
    content,
    accepted,
):
    from reme.components.agent_wrapper.as_agent_wrapper import AsAgentWrapper
    from reme.config import resolve_app_config

    workspace = tmp_path / "workspace"
    config_path = Path(__file__).resolve().parents[2] / "reme/config/fermes-hermes-stage1.yaml"
    config = resolve_app_config(config=str(config_path), workspace_dir=str(workspace), log_config=False)

    async def proposal_reply(_self, _inputs, **_kwargs):
        return {
            "result": json.dumps(
                {
                    "path": "daily/2026-10-21/protected-fact.md",
                    "name": "protected-fact",
                    "description": "A deterministic protected fact.",
                    "content": content,
                },
            ),
            "tool_execution": [],
        }

    monkeypatch.setattr(AsAgentWrapper, "reply", proposal_reply)
    request = {
        "session_id": "template-session" if not accepted else "substantive-session",
        "messages": [{"id": "m1", "name": "user", "role": "user", "content": "fact"}],
        "date": "2026-10-21",
    }
    async with Application(**config) as app:
        response = await app.run_job(
            "auto_memory",
            request_id="template-request" if not accepted else "substantive-request",
            **request,
        )

    assert response.success is accepted
    assert bool(list((workspace / "daily").rglob("*.md"))) is accepted
    assert bool(list((workspace / "metadata/auto_memory_requests").rglob("*.json"))) is accepted
    if not accepted:
        assert "protected proposal contained no substantive memory content" in response.answer
    assert list((workspace / "session/dialog").rglob("*.jsonl"))


@_POSIX_DURABLE_COMMIT
@pytest.mark.asyncio
async def test_option_b_checks_size_before_template_parser(tmp_path, monkeypatch):
    """Reject oversized proposals before scanning the model's content."""
    from reme.components.agent_wrapper.as_agent_wrapper import AsAgentWrapper
    from reme.config import resolve_app_config

    workspace = tmp_path / "workspace"
    config_path = Path(__file__).resolve().parents[2] / "reme/config/fermes-hermes-stage1.yaml"
    config = resolve_app_config(config=str(config_path), workspace_dir=str(workspace), log_config=False)

    async def proposal_reply(_self, _inputs, **_kwargs):
        return {
            "result": json.dumps(
                {
                    "path": "daily/2026-10-21/oversize.md",
                    "name": "oversize",
                    "description": "Oversized protected content",
                    "content": "x" * 8192,
                },
            ),
            "tool_execution": [],
        }

    def unexpected_parser(_content):
        raise AssertionError("size check must precede the template parser")

    monkeypatch.setattr(AsAgentWrapper, "reply", proposal_reply)
    monkeypatch.setattr(AutoMemoryStep, "max_file_bytes", lambda _self: 4096)
    monkeypatch.setattr(AutoMemoryStep, "_is_empty_option_b_content", staticmethod(unexpected_parser))
    async with Application(**config) as app:
        response = await app.run_job(
            "auto_memory",
            session_id="oversize-session",
            messages=[{"id": "m1", "name": "user", "role": "user", "content": "fact"}],
            date="2026-10-21",
            request_id="oversize-request",
        )
    assert not response.success
    assert "protected proposal exceeds the file byte limit" in response.answer
    assert not list((workspace / "daily").rglob("*.md"))
    assert not list((workspace / "metadata/auto_memory_requests").rglob("*.json"))


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


@_POSIX_DURABLE_COMMIT
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
    assert PLUGIN_MODULE._is_permanent_write_failure(ReMeBackendError(tampered.answer)) is True
    assert len(calls) == 2


@_POSIX_DURABLE_COMMIT
@pytest.mark.asyncio
async def test_option_b_create_collision_allocates_unique_note_without_replay(tmp_path, monkeypatch):
    """Two protected sessions sharing a proposed name retain both revisions."""
    from reme.components.agent_wrapper.as_agent_wrapper import AsAgentWrapper
    from reme.config import resolve_app_config

    workspace = tmp_path / "workspace"
    config_path = Path(__file__).resolve().parents[2] / "reme/config/fermes-hermes-stage1.yaml"
    config = resolve_app_config(config=str(config_path), workspace_dir=str(workspace), log_config=False)
    calls = []
    proposed_name = "shared-" + ("n" * 242)

    async def proposal_reply(_self, inputs, **_kwargs):
        calls.append(1)
        prompt = str(inputs)
        if "Session ID: collision-session-a" in prompt:
            body = "Protected fact 1."
        else:
            assert "Session ID: collision-session-b" in prompt
            body = "Protected fact 2."
        return {
            "result": json.dumps(
                {
                    "path": f"daily/2026-10-21/{proposed_name}.md",
                    "name": proposed_name,
                    "description": "A shared-name test note.",
                    "content": body,
                },
            ),
            "tool_execution": [],
        }

    monkeypatch.setattr(AsAgentWrapper, "reply", proposal_reply)
    request = {
        "messages": [{"id": "m1", "name": "user", "role": "user", "content": "fact"}],
        "date": "2026-10-21",
    }

    async with Application(**config) as app:
        first, second = await asyncio.gather(
            app.run_job("auto_memory", request_id="collision-a", session_id="collision-session-a", **request),
            app.run_job("auto_memory", request_id="collision-b", session_id="collision-session-b", **request),
        )
        replay_a, replay_b = await asyncio.gather(
            app.run_job("auto_memory", request_id="collision-a", session_id="collision-session-a", **request),
            app.run_job("auto_memory", request_id="collision-b", session_id="collision-session-b", **request),
        )

    assert first.success and second.success and replay_a.success and replay_b.success
    base_path = f"daily/2026-10-21/{proposed_name}.md"
    assert base_path in {first.metadata["path"], second.metadata["path"]}
    suffixed_path = next(path for path in (first.metadata["path"], second.metadata["path"]) if path != base_path)
    assert suffixed_path.startswith("daily/2026-10-21/shared-")
    assert second.metadata["path"] in {base_path, suffixed_path}
    assert second.metadata["path"] != first.metadata["path"]
    assert len(Path(suffixed_path).name.encode("utf-8")) <= 255
    assert replay_a.metadata["path"] == first.metadata["path"]
    assert replay_b.metadata["path"] == second.metadata["path"]
    assert len(calls) == 2
    assert frontmatter.loads((workspace / first.metadata["path"]).read_text(encoding="utf-8")).content.strip() == (
        "Protected fact 1."
    )
    assert frontmatter.loads((workspace / second.metadata["path"]).read_text(encoding="utf-8")).content.strip() == (
        "Protected fact 2."
    )


@_POSIX_DURABLE_COMMIT
@pytest.mark.asyncio
async def test_option_b_create_enametoolong_allocates_capped_suffix(tmp_path, monkeypatch):
    """An overlong original target advances to the bounded protected suffix."""
    from reme.components.agent_wrapper.as_agent_wrapper import AsAgentWrapper
    from reme.config import resolve_app_config

    workspace = tmp_path / "workspace"
    config_path = Path(__file__).resolve().parents[2] / "reme/config/fermes-hermes-stage1.yaml"
    config = resolve_app_config(config=str(config_path), workspace_dir=str(workspace), log_config=False)
    proposed_name = "o" * 253

    async def proposal_reply(_self, _inputs, **_kwargs):
        return {
            "result": json.dumps(
                {
                    "path": f"daily/2026-10-21/{proposed_name}.md",
                    "name": proposed_name,
                    "description": "An ENAMETOOLONG test note.",
                    "content": "A protected fact.",
                },
            ),
            "tool_execution": [],
        }

    monkeypatch.setattr(AsAgentWrapper, "reply", proposal_reply)

    async with Application(**config) as app:
        response = await app.run_job(
            "auto_memory",
            request_id="enametoolong-request",
            session_id="enametoolong-session",
            messages=[{"id": "m1", "name": "user", "role": "user", "content": "fact"}],
            date="2026-10-21",
        )

    assert response.success
    note_name = Path(response.metadata["path"]).name
    assert note_name != f"{proposed_name}.md"
    request_suffix = hashlib.sha256(b"enametoolong-session\0enametoolong-request").hexdigest()[:8]
    max_prefix_bytes = 255 - len(".md") - len(f"--{request_suffix}-100".encode("utf-8"))
    assert note_name == f"{'o' * max_prefix_bytes}--{request_suffix}.md"
    assert len(note_name.encode("utf-8")) <= 255


@_POSIX_DURABLE_COMMIT
@pytest.mark.asyncio
async def test_option_b_create_post_commit_cleanup_failure_does_not_allocate_again(tmp_path, monkeypatch):
    """A cleanup-only ENAMETOOLONG leaves one committed candidate and no suffix note."""
    from reme.components.agent_wrapper.as_agent_wrapper import AsAgentWrapper
    from reme.config import resolve_app_config

    workspace = tmp_path / "workspace"
    config_path = Path(__file__).resolve().parents[2] / "reme/config/fermes-hermes-stage1.yaml"
    config = resolve_app_config(config=str(config_path), workspace_dir=str(workspace), log_config=False)
    proposed_name = "cleanup-boundary"
    calls = []

    async def proposal_reply(_self, _inputs, **_kwargs):
        calls.append(1)
        return {
            "result": json.dumps(
                {
                    "path": f"daily/2026-10-21/{proposed_name}.md",
                    "name": proposed_name,
                    "description": "A cleanup-boundary test note.",
                    "content": "A protected fact.",
                },
            ),
            "tool_execution": [],
        }

    real_unlink = Path.unlink

    def fail_long_create_temp(path, missing_ok=False):
        if path.name.startswith(".reme-") and path.name.endswith(".create.tmp"):
            raise OSError(errno.ENAMETOOLONG, "synthetic absolute cleanup path too long")
        return real_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(AsAgentWrapper, "reply", proposal_reply)
    monkeypatch.setattr(Path, "unlink", fail_long_create_temp)

    async with Application(**config) as app:
        response = await app.run_job(
            "auto_memory",
            request_id="cleanup-boundary-request",
            session_id="cleanup-boundary-session",
            messages=[{"id": "m1", "name": "user", "role": "user", "content": "fact"}],
            date="2026-10-21",
        )

    note_dir = workspace / "daily/2026-10-21"
    assert response.success
    assert response.metadata["path"] == f"daily/2026-10-21/{proposed_name}.md"
    assert calls == [1]
    assert [path.name for path in note_dir.glob("*.md")] == [f"{proposed_name}.md"]
    assert not list(note_dir.glob(".reme-*.create.tmp"))


@_POSIX_DURABLE_COMMIT
@pytest.mark.asyncio
async def test_option_b_recovers_after_malformed_day_index_is_repaired(tmp_path, monkeypatch):
    """A derived-index parse failure does not force protected model replay."""
    from reme.components.agent_wrapper.as_agent_wrapper import AsAgentWrapper
    from reme.config import resolve_app_config

    workspace = tmp_path / "workspace"
    config_path = Path(__file__).resolve().parents[2] / "reme/config/fermes-hermes-stage1.yaml"
    config = resolve_app_config(config=str(config_path), workspace_dir=str(workspace), log_config=False)
    index = workspace / "daily/2026-10-21.md"
    index.parent.mkdir(parents=True)
    malformed_index = b"---\ntags: [\n---\nmanual index bytes\n"
    index.write_bytes(malformed_index)
    calls = []

    async def proposal_reply(_self, _inputs, **_kwargs):
        calls.append(1)
        return {
            "result": json.dumps(
                {
                    "path": "daily/2026-10-21/repaired-index.md",
                    "name": "repaired-index",
                    "description": "A recovery test note.",
                    "content": "The protected recovery fact.",
                },
            ),
            "tool_execution": [],
        }

    original_receipt = AutoMemoryStep._write_option_b_receipt
    receipt_calls = []

    async def lose_first_receipt(step, path, fingerprint):
        receipt_calls.append(1)
        if len(receipt_calls) == 1:
            raise OSError("simulated receipt loss")
        await original_receipt(step, path, fingerprint)

    monkeypatch.setattr(AsAgentWrapper, "reply", proposal_reply)
    monkeypatch.setattr(AutoMemoryStep, "_write_option_b_receipt", lose_first_receipt)
    request = {
        "request_id": "malformed-index-recovery",
        "session_id": "malformed-index-session",
        "messages": [{"id": "m1", "name": "user", "role": "user", "content": "fact"}],
        "date": "2026-10-21",
    }

    async with Application(**config) as app:
        first = await app.run_job("auto_memory", **request)
        note = workspace / "daily/2026-10-21/repaired-index.md"
        assert not first.success
        assert note.is_file()
        assert index.read_bytes() == malformed_index
        assert not list((workspace / "metadata/auto_memory_requests").rglob("*.json"))

        index.write_text("---\nname: 2026-10-21\n---\nrepaired\n", encoding="utf-8")
        second = await app.run_job("auto_memory", **request)

    assert second.success
    assert calls == [1]
    assert receipt_calls == [1, 1]
    assert note.read_text(encoding="utf-8").count("The protected recovery fact.") == 1


@_POSIX_DURABLE_COMMIT
@pytest.mark.asyncio
async def test_option_b_recovery_scans_prior_daily_directory_before_appending(tmp_path, monkeypatch):
    """A receipt loss recovered after midnight does not invoke the model twice."""
    from reme.components.agent_wrapper.as_agent_wrapper import AsAgentWrapper
    from reme.config import resolve_app_config

    workspace = tmp_path / "workspace"
    config_path = Path(__file__).resolve().parents[2] / "reme/config/fermes-hermes-stage1.yaml"
    config = resolve_app_config(config=str(config_path), workspace_dir=str(workspace), log_config=False)
    calls = []
    clock = iter((datetime(2026, 10, 21), datetime(2026, 10, 22)))

    async def proposal_reply(_self, _inputs, **_kwargs):
        calls.append(1)
        return {
            "result": json.dumps(
                {
                    "path": "daily/2026-10-21/cross-midnight.md",
                    "name": "cross-midnight",
                    "description": "A protected fact.",
                    "content": "The protected fact.",
                },
            ),
            "tool_execution": [],
        }

    async def lost_receipt(_self, _path, _fingerprint):
        return None

    monkeypatch.setattr(AsAgentWrapper, "reply", proposal_reply)
    monkeypatch.setattr(auto_memory_module, "now", lambda _tz: next(clock))
    monkeypatch.setattr(AutoMemoryStep, "_messages_day", staticmethod(lambda _messages, _timezone=None: None))
    monkeypatch.setattr(AutoMemoryStep, "_write_option_b_receipt", lost_receipt)
    request = {
        "session_id": "cross-midnight-session",
        "messages": [{"id": "m1", "name": "user", "role": "user", "content": "fact"}],
    }

    async with Application(**config) as app:
        first = await app.run_job("auto_memory", request_id="cross-midnight-request", **request)
        source = workspace / "session/dialog/cross-midnight-session.jsonl"
        original = source.read_bytes()
        appended = json.loads(original.splitlines()[0])
        appended["id"] = "m2"
        appended["content"][0]["text"] = "later turn"
        source.write_bytes(original + json.dumps(appended, ensure_ascii=False, separators=(",", ":")).encode() + b"\n")
        unrelated = workspace / "daily/2026-10-20/unrelated.md"
        unrelated.parent.mkdir(parents=True)
        unrelated.write_bytes(b"---\nnot: [valid\xff\n---\nnot a candidate\n")
        second = await app.run_job("auto_memory", request_id="cross-midnight-request", **request)

    assert first.success and second.success
    assert calls == [1]
    assert second.metadata["path"] == "daily/2026-10-21/cross-midnight.md"
    assert len(list((workspace / "daily").glob("**/cross-midnight.md"))) == 1


@_POSIX_DURABLE_COMMIT
@pytest.mark.asyncio
async def test_option_b_receipt_replays_after_append_only_source_growth(tmp_path, monkeypatch):
    """A lost acknowledgement remains replayable after the owned transcript grows."""
    from reme.components.agent_wrapper.as_agent_wrapper import AsAgentWrapper
    from reme.config import resolve_app_config

    workspace = tmp_path / "workspace"
    config_path = Path(__file__).resolve().parents[2] / "reme/config/fermes-hermes-stage1.yaml"
    config = resolve_app_config(config=str(config_path), workspace_dir=str(workspace), log_config=False)
    note_path = workspace / "daily/2026-10-21/growth.md"
    calls = []

    async def proposal_reply(_self, _inputs, **_kwargs):
        calls.append(1)
        return {
            "result": json.dumps(
                {
                    "path": "daily/2026-10-21/growth.md",
                    "name": "growth",
                    "description": "A deterministic protected fact.",
                    "content": "The protected fact.",
                },
            ),
            "tool_execution": [],
        }

    monkeypatch.setattr(AsAgentWrapper, "reply", proposal_reply)
    request = {
        "session_id": "growth-session",
        "messages": [{"id": "m1", "name": "user", "role": "user", "content": "fact"}],
        "date": "2026-10-21",
    }
    async with Application(**config) as app:
        first = await app.run_job("auto_memory", request_id="growth-request", **request)
        source = workspace / "session/dialog/growth-session.jsonl"
        original = source.read_bytes()
        record = json.loads(original.splitlines()[0])
        record["id"] = "m2"
        record["content"][0]["text"] = "later turn"
        source.write_bytes(original + json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode() + b"\n")
        note_before_replay = note_path.read_bytes()

        replay = await app.run_job("auto_memory", request_id="growth-request", **request)

    assert first.success
    assert replay.success
    assert replay == first
    assert calls == [1]
    assert note_path.read_bytes() == note_before_replay
    assert len(list((workspace / "metadata/auto_memory_requests").rglob("*.json"))) == 1


@_POSIX_DURABLE_COMMIT
@pytest.mark.asyncio
async def test_option_b_receipt_rejects_changed_historical_source_prefix(tmp_path, monkeypatch):
    """A changed historical record is not accepted merely because the file also grew."""
    from reme.components.agent_wrapper.as_agent_wrapper import AsAgentWrapper
    from reme.config import resolve_app_config

    workspace = tmp_path / "workspace"
    config_path = Path(__file__).resolve().parents[2] / "reme/config/fermes-hermes-stage1.yaml"
    config = resolve_app_config(config=str(config_path), workspace_dir=str(workspace), log_config=False)
    note_path = workspace / "daily/2026-10-21/prefix.md"
    calls = []

    async def proposal_reply(_self, _inputs, **_kwargs):
        calls.append(1)
        return {
            "result": json.dumps(
                {
                    "path": "daily/2026-10-21/prefix.md",
                    "name": "prefix",
                    "description": "A deterministic protected fact.",
                    "content": "The protected fact.",
                },
            ),
            "tool_execution": [],
        }

    monkeypatch.setattr(AsAgentWrapper, "reply", proposal_reply)
    request = {
        "session_id": "prefix-session",
        "messages": [{"id": "m1", "name": "user", "role": "user", "content": "fact"}],
        "date": "2026-10-21",
    }
    async with Application(**config) as app:
        first = await app.run_job("auto_memory", request_id="prefix-request", **request)
        source = workspace / "session/dialog/prefix-session.jsonl"
        original = source.read_bytes()
        changed = json.loads(original.splitlines()[0])
        changed["content"][0]["text"] = "tampered historical turn"
        appended = dict(changed)
        appended["id"] = "m2"
        appended["content"] = [{"type": "text", "text": "later turn"}]
        source.write_bytes(
            json.dumps(changed, ensure_ascii=False, separators=(",", ":")).encode()
            + b"\n"
            + json.dumps(appended, ensure_ascii=False, separators=(",", ":")).encode()
            + b"\n",
        )
        note_before_replay = note_path.read_bytes()

        replay = await app.run_job("auto_memory", request_id="prefix-request", **request)

    assert first.success
    assert not replay.success
    assert "replay refused" in replay.answer
    assert calls == [1]
    assert note_path.read_bytes() == note_before_replay


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


@_POSIX_DURABLE_COMMIT
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


@_POSIX_DURABLE_COMMIT
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
async def test_option_b_rejects_unmarked_foreign_note_with_matching_source_before_model(tmp_path, monkeypatch):
    """A source link cannot claim an unmarked note whose session owner differs."""
    from reme.components.agent_wrapper.as_agent_wrapper import AsAgentWrapper
    from reme.config import resolve_app_config

    workspace = tmp_path / "workspace"
    config_path = Path(__file__).resolve().parents[2] / "reme/config/fermes-hermes-stage1.yaml"
    config = resolve_app_config(config=str(config_path), workspace_dir=str(workspace), log_config=False)
    note_path = workspace / "daily/2026-10-21/planted.md"
    note_path.parent.mkdir(parents=True)
    original = (
        "---\nname: planted\n"
        "session_id: foreign-session\n"
        "source_conversation: '[[session/dialog/protected-session.jsonl]]'\n"
        "---\nForeign content.\n"
    )
    note_path.write_text(original, encoding="utf-8")
    calls = []

    async def unexpected_model_call(_self, _inputs, **_kwargs):
        calls.append(1)
        pytest.fail("foreign unmarked note must be rejected before the model call")

    monkeypatch.setattr(AsAgentWrapper, "reply", unexpected_model_call)
    async with Application(**config) as app:
        response = await app.run_job(
            "auto_memory",
            request_id="foreign-unmarked-note",
            session_id="protected-session",
            messages=[{"id": "m1", "name": "user", "role": "user", "content": "Synthetic fact."}],
            date="2026-10-21",
        )

    assert not response.success
    assert "different source session" in response.answer or "protected note" in response.answer
    assert not calls
    assert note_path.read_text(encoding="utf-8") == original
    assert not list((workspace / "metadata" / "auto_memory_requests").rglob("*.json"))


@pytest.mark.asyncio
async def test_option_b_updates_unmarked_note_with_matching_owner_and_source(tmp_path, monkeypatch):
    """An unmarked legacy note remains updateable only for its exact owner."""
    from reme.components.agent_wrapper.as_agent_wrapper import AsAgentWrapper
    from reme.config import resolve_app_config

    workspace = tmp_path / "workspace"
    config_path = Path(__file__).resolve().parents[2] / "reme/config/fermes-hermes-stage1.yaml"
    config = resolve_app_config(config=str(config_path), workspace_dir=str(workspace), log_config=False)
    note_path = workspace / "daily/2026-10-21/legacy.md"
    note_path.parent.mkdir(parents=True)
    note_path.write_text(
        "---\nname: legacy\ndescription: Legacy note\n"
        "session_id: protected-session\n"
        "source_conversation: '[[session/dialog/protected-session.jsonl]]'\n"
        "---\nLegacy content.\n",
        encoding="utf-8",
    )
    calls = []

    async def proposal_reply(_self, _inputs, **_kwargs):
        calls.append(1)
        return {
            "result": json.dumps(
                {
                    "path": "daily/2026-10-21/legacy.md",
                    "name": "legacy",
                    "description": "Updated legacy note",
                    "content": "Updated content.",
                },
            ),
            "tool_execution": [],
        }

    monkeypatch.setattr(AsAgentWrapper, "reply", proposal_reply)
    async with Application(**config) as app:
        response = await app.run_job(
            "auto_memory",
            request_id="matching-unmarked-note",
            session_id="protected-session",
            messages=[{"id": "m1", "name": "user", "role": "user", "content": "Synthetic fact."}],
            date="2026-10-21",
        )

    assert response.success
    assert calls == [1]
    saved = frontmatter.loads(note_path.read_text(encoding="utf-8"))
    # python-frontmatter canonicalizes the serialized body without a trailing newline.
    assert saved.content == "Updated content."
    assert saved.metadata["session_id"] == "protected-session"
    assert "_reme_commit" in saved.metadata


@pytest.mark.asyncio
async def test_option_b_rejects_matching_owner_note_through_symlink_before_model(tmp_path, monkeypatch):
    """Protected lookup cannot read or write a symlinked note target."""
    from reme.components.agent_wrapper.as_agent_wrapper import AsAgentWrapper
    from reme.config import resolve_app_config

    workspace = tmp_path / "workspace"
    config_path = Path(__file__).resolve().parents[2] / "reme/config/fermes-hermes-stage1.yaml"
    config = resolve_app_config(config=str(config_path), workspace_dir=str(workspace), log_config=False)
    outside = tmp_path / "outside.md"
    outside.write_text(
        "---\nname: linked\nsession_id: protected-session\n"
        "source_conversation: '[[session/dialog/protected-session.jsonl]]'\n"
        "---\nOutside content.\n",
        encoding="utf-8",
    )
    note_path = workspace / "daily/2026-10-21/linked.md"
    note_path.parent.mkdir(parents=True)
    note_path.symlink_to(outside)
    original = outside.read_bytes()
    calls = []

    async def unexpected_model_call(_self, _inputs, **_kwargs):
        calls.append(1)
        pytest.fail("symlinked note must be rejected before the model call")

    monkeypatch.setattr(AsAgentWrapper, "reply", unexpected_model_call)

    async def find_linked_note(_self, _day, _session_id):
        return {"path": "daily/2026-10-21/linked.md"}

    monkeypatch.setattr(AutoMemoryStep, "_list_session_note", find_linked_note)
    async with Application(**config) as app:
        response = await app.run_job(
            "auto_memory",
            request_id="symlinked-note",
            session_id="protected-session",
            messages=[{"id": "m1", "name": "user", "role": "user", "content": "Synthetic fact."}],
            date="2026-10-21",
        )

    assert not response.success
    assert not calls
    assert outside.read_bytes() == original
    assert not list((workspace / "metadata" / "auto_memory_requests").rglob("*.json"))


@pytest.mark.asyncio
async def test_option_b_rejects_model_path_mismatch_without_mutation(tmp_path, monkeypatch):
    """The application-owned existing path remains bound across the proposal CAS."""
    from reme.components.agent_wrapper.as_agent_wrapper import AsAgentWrapper
    from reme.config import resolve_app_config

    workspace = tmp_path / "workspace"
    config_path = Path(__file__).resolve().parents[2] / "reme/config/fermes-hermes-stage1.yaml"
    config = resolve_app_config(config=str(config_path), workspace_dir=str(workspace), log_config=False)
    note_path = workspace / "daily/2026-10-21/bound.md"
    note_path.parent.mkdir(parents=True)
    note_path.write_text(
        "---\nname: bound\ndescription: Bound note\n"
        "session_id: protected-session\n"
        "source_conversation: '[[session/dialog/protected-session.jsonl]]'\n"
        "---\nBound content.\n",
        encoding="utf-8",
    )
    original = note_path.read_bytes()
    calls = []

    async def mismatched_proposal(_self, _inputs, **_kwargs):
        calls.append(1)
        return {
            "result": json.dumps(
                {
                    "path": "daily/2026-10-21/other.md",
                    "name": "bound",
                    "description": "Bound note",
                    "content": "Changed content.",
                },
            ),
            "tool_execution": [],
        }

    monkeypatch.setattr(AsAgentWrapper, "reply", mismatched_proposal)
    async with Application(**config) as app:
        response = await app.run_job(
            "auto_memory",
            request_id="mismatched-proposal-path",
            session_id="protected-session",
            messages=[{"id": "m1", "name": "user", "role": "user", "content": "Synthetic fact."}],
            date="2026-10-21",
        )

    assert not response.success
    assert calls == [1]
    assert note_path.read_bytes() == original
    assert not list((workspace / "metadata" / "auto_memory_requests").rglob("*.json"))


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
        (None, True, ""),
        ([{"type": "tool_call", "name": "daily_write", "call_id": "c1"}], True, ""),
        (
            [
                {"type": "tool_call", "name": "daily_write", "call_id": "c1"},
                {"type": "tool_result", "name": "daily_write", "call_id": "wrong", "state": "success"},
            ],
            True,
            "",
        ),
        (
            [
                {"type": "tool_call", "name": "daily_write", "call_id": "c1"},
                {"type": "tool_call", "name": "daily_write", "call_id": "c2"},
                {"type": "tool_result", "name": "daily_write", "call_id": "c1", "state": "success"},
                {"type": "tool_result", "name": "daily_write", "call_id": "c2", "state": "error"},
            ],
            True,
            "",
        ),
        (
            [
                {"type": "tool_call", "name": "daily_write", "call_id": "c1"},
                {"type": "tool_result", "name": "daily_write", "call_id": "c1", "state": "success"},
            ],
            True,
            "",
        ),
        ([], True, ""),
        ([], True, "must remember this"),
        ([], True, "   "),
    ],
)
async def test_default_auto_memory_receipt_keeps_upstream_reply_behavior(
    tmp_path,
    monkeypatch,
    tool_execution,
    expected_success,
    memory_hint,
):
    """The default job does not require Stage1 tool-evidence blocks."""
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
async def test_default_auto_memory_receipt_does_not_inject_provenance(tmp_path, monkeypatch):
    """Default notes remain on the upstream schema without a source hash."""
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
    assert response.success
    assert list((workspace / "metadata" / "auto_memory_requests").rglob("*.json"))
    saved = frontmatter.loads(note_file.read_text(encoding="utf-8"))
    assert "source_hash" not in saved.metadata


@pytest.mark.asyncio
async def test_default_receipt_replay_does_not_require_provenance(tmp_path, monkeypatch):
    """Default replay does not quarantine a valid completed note on Stage1 evidence."""
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
        assert "source_hash" not in saved.metadata
        saved.metadata["source_hash"] = {
            "algorithm": "sha256",
            "format": "hex",
            "value": "0" * 64,
        }
        note.write_text(frontmatter.dumps(saved), encoding="utf-8")
        repeated = await app.run_job("auto_memory", **job_kwargs)
        assert repeated.success
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
    config_path = Path(__file__).resolve().parents[2] / "reme/config/fermes-hermes-stage1.yaml"
    config = resolve_app_config(config=str(config_path), workspace_dir=str(workspace), log_config=False)

    async def no_tool_reply(_self, _inputs, **_kwargs):
        return {
            "result": json.dumps(
                {
                    "path": note_path,
                    "name": "existing",
                    "description": "updated protected fact",
                    "content": "Updated protected content.",
                },
            ),
            "tool_execution": [],
        }

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
