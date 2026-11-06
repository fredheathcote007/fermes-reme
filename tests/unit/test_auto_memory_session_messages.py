"""Session transcript saves stay consistent under concurrent writers."""

# pylint: disable=protected-access,missing-function-docstring

import asyncio
import os
from types import SimpleNamespace

from agentscope.message import Msg, TextBlock
import pytest

from reme.steps.evolve import auto_memory
from reme.steps.evolve.auto_memory import AutoMemoryStep

_SESSION = "race-session"


def _msg(message_id: str, timestamp: str) -> Msg:
    return Msg.model_validate(
        {
            "id": message_id,
            "name": "Alice",
            "role": "user",
            "created_at": timestamp,
            "content": [TextBlock(text=f"text of {message_id}")],
        },
    )


@pytest.fixture(name="step")
def memory_step(tmp_path):
    step = AutoMemoryStep()
    step.file_store = SimpleNamespace(workspace_path=tmp_path)
    return step


class _LoggedLock:
    """Record lock contention; writer B signals once it is queued on the lock."""

    def __init__(self, lock, tag, interleaving):
        self._lock = lock
        self._tag = tag
        self._interleaving = interleaving

    async def __aenter__(self):
        self._interleaving.log.append((self._tag, "lock-wait"))
        if self._tag == "B":
            self._interleaving.b_lock_waiting.set()
        await self._lock.acquire()
        self._interleaving.log.append((self._tag, "lock-acquired"))
        return self

    async def __aexit__(self, *exc_info):
        self._lock.release()
        return None


class _LoggedRead:
    """Record read progress; writer A pauses after reading until B awaits the lock."""

    def __init__(self, context_manager, tag, interleaving):
        self._context_manager = context_manager
        self._tag = tag
        self._interleaving = interleaving

    async def __aenter__(self):
        handle = await self._context_manager.__aenter__()
        interleaving, tag = self._interleaving, self._tag
        interleaving.log.append((tag, "read-start"))

        async def read():
            content = await handle.read()
            interleaving.log.append((tag, "read-done"))
            if tag == "A":
                await interleaving.b_lock_waiting.wait()
            return content

        return SimpleNamespace(read=read)

    async def __aexit__(self, *exc_info):
        return await self._context_manager.__aexit__(*exc_info)


class _LoggedWrite:
    """Record write completion once the file handle is closed and flushed."""

    def __init__(self, context_manager, tag, interleaving):
        self._context_manager = context_manager
        self._tag = tag
        self._interleaving = interleaving

    async def __aenter__(self):
        self._interleaving.log.append((self._tag, "write-start"))
        return await self._context_manager.__aenter__()

    async def __aexit__(self, *exc_info):
        result = await self._context_manager.__aexit__(*exc_info)
        self._interleaving.log.append((self._tag, "write-done"))
        return result


class _Interleaving:
    """Deterministic two-writer interleaving driven by events, not sleeps.

    Writer A snapshots the transcript, then pauses until writer B is queued on
    the transcript lock. Without mutual exclusion B would read and write against
    A's stale snapshot in between; with the lock B cannot start reading until A
    finished writing.
    """

    def __init__(self):
        self.log: list[tuple[str, str]] = []
        self.b_lock_waiting = asyncio.Event()
        self.task_a: asyncio.Task | None = None
        self.task_b: asyncio.Task | None = None

    def patch(self, monkeypatch):
        real_get_path_lock = auto_memory.get_path_lock
        real_open = auto_memory.aiofiles.open

        async def get_path_lock(target):
            return _LoggedLock(await real_get_path_lock(target), self._tag(), self)

        def open_(path, mode="r", **kwargs):
            context_manager = real_open(path, mode, **kwargs)
            tag = self._tag()
            if mode == "r":
                return _LoggedRead(context_manager, tag, self)
            if mode in ("a", "w"):
                return _LoggedWrite(context_manager, tag, self)
            return context_manager

        monkeypatch.setattr(auto_memory, "get_path_lock", get_path_lock)
        monkeypatch.setattr(auto_memory.aiofiles, "open", open_)

    def _tag(self):
        task = asyncio.current_task()
        if task is self.task_a:
            return "A"
        if task is self.task_b:
            return "B"
        return None

    async def run(self, step, a_messages, b_messages):
        self.task_a = asyncio.create_task(step._save_session_messages(_SESSION, a_messages))
        await asyncio.sleep(0)  # Let writer A acquire the transcript lock before B starts.
        self.task_b = asyncio.create_task(step._save_session_messages(_SESSION, b_messages))
        await asyncio.wait_for(asyncio.gather(self.task_a, self.task_b), timeout=5)


def _assert_transcript(path, expected_ids):
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    messages = [Msg.model_validate_json(line) for line in lines]
    assert [message.id for message in messages] == expected_ids
    assert len({message.id for message in messages}) == len(messages)
    assert [message.created_at for message in messages] == sorted(message.created_at for message in messages)


def _assert_serialized(interleaving):
    log = interleaving.log
    # Writer B queued on the transcript lock while writer A held it: A's
    # post-read pause only ends once B attempted the lock, so B's lock-wait
    # must precede A's write.
    assert log.index(("B", "lock-wait")) < log.index(("A", "write-start"))
    # Mutual exclusion: B acquires the lock and re-reads only after A's write
    # completed, so B always merges against A's fresh transcript state.
    assert log.index(("A", "write-done")) < log.index(("B", "lock-acquired"))
    assert log.index(("B", "lock-acquired")) < log.index(("B", "read-start"))


@pytest.mark.asyncio
async def test_concurrent_appends_merge_disjoint_messages_in_order(step, monkeypatch):
    await step._save_session_messages(_SESSION, [_msg("m0", "2026-10-01T10:00:00")])

    interleaving = _Interleaving()
    interleaving.patch(monkeypatch)
    # Writer A snapshots [m0]; writer B appends m2 while A is paused.
    await interleaving.run(step, [_msg("m1", "2026-10-01T10:01:00")], [_msg("m2", "2026-10-01T10:02:00")])

    _assert_transcript(step._session_path(_SESSION), ["m0", "m1", "m2"])
    _assert_serialized(interleaving)


@pytest.mark.asyncio
async def test_concurrent_append_survives_rewrite_race(step, monkeypatch):
    await step._save_session_messages(_SESSION, [_msg("m0", "2026-10-01T10:00:00")])
    await step._save_session_messages(_SESSION, [_msg("m1", "2026-10-01T10:01:00")])

    interleaving = _Interleaving()
    interleaving.patch(monkeypatch)
    # Writer A inserts m_mid before m1, forcing the full-rewrite branch against
    # its stale snapshot; writer B appends m2 while A is paused.
    await interleaving.run(
        step,
        [_msg("m_mid", "2026-10-01T10:00:30")],
        [_msg("m2", "2026-10-01T10:02:00")],
    )

    _assert_transcript(step._session_path(_SESSION), ["m0", "m_mid", "m1", "m2"])
    _assert_serialized(interleaving)


def test_default_note_match_prefers_session_id_over_source_link():
    first = {"path": "daily/2026-10-01/first.md", "session_id": _SESSION, "source_conversation": "[[other]]"}
    second = {"path": "daily/2026-10-01/second.md", "source_conversation": AutoMemoryStep()._session_link(_SESSION)}

    assert AutoMemoryStep()._find_session_note([first, second], _SESSION) is first


@pytest.mark.asyncio
async def test_default_transcript_collapses_same_batch_duplicate_ids(step):
    await step._save_session_messages(
        _SESSION,
        [_msg("duplicate", "2026-10-01T10:00:00"), _msg("duplicate", "2026-10-01T10:01:00")],
    )

    messages = [
        Msg.model_validate_json(line)
        for line in step._session_path(_SESSION).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [(message.id, message.created_at) for message in messages] == [("duplicate", "2026-10-01T10:01:00")]


@pytest.mark.asyncio
async def test_default_redelivery_reorders_and_sanitizes_rewrite(step):
    await step._save_session_messages(
        _SESSION,
        [_msg("first", "2026-10-01T10:00:00"), _msg("second", "2026-10-01T10:01:00")],
    )
    redelivery = Msg.model_validate(
        {
            "id": "second",
            "name": "Alice",
            "role": "assistant",
            "created_at": "2026-10-01T09:00:00",
            "content": [
                {"type": "text", "text": "updated"},
                {"type": "tool_result", "id": "tool-1", "name": "read", "output": "private"},
                {
                    "type": "data",
                    "source": {"type": "base64", "media_type": "image/png", "data": "aGVsbG8="},
                },
            ],
        },
    )

    await step._save_session_messages(_SESSION, [redelivery])

    saved = [
        Msg.model_validate_json(line)
        for line in step._session_path(_SESSION).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [message.id for message in saved] == ["second", "first"]
    assert saved[0].created_at == "2026-10-01T09:00:00"
    assert [block.type for block in saved[0].content] == ["text"]


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX directory symlink support")
@pytest.mark.asyncio
async def test_default_transcript_save_follows_symlinked_session_directory(step):
    real_session = step.file_store.workspace_path / "real-session"
    real_session.mkdir()
    (step.file_store.workspace_path / "session").symlink_to(real_session, target_is_directory=True)

    await step._save_session_messages(_SESSION, [_msg("linked", "2026-10-01T10:00:00")])

    assert (real_session / "dialog" / f"{_SESSION}.jsonl").is_file()


@pytest.mark.asyncio
async def test_protected_transcript_keeps_identical_distinct_turns(tmp_path, monkeypatch):
    """Repeated identical text is not mistaken for a retry of an earlier turn."""
    durable_calls = []

    async def durable_write(path, content):
        durable_calls.append(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    monkeypatch.setattr(auto_memory, "write_file_durable", durable_write)
    step = AutoMemoryStep(durable_commit=True, agent_scope="test-scope")
    step.file_store = SimpleNamespace(workspace_path=tmp_path)
    step.agent_wrapper = SimpleNamespace(backend="agentscope")
    step.context = SimpleNamespace(
        data={"request_id": "request-1"},
        get=lambda key, default=None: {"request_id": "request-1"}.get(key, default),
    )

    first = _msg("turn-1", "2026-10-01T10:00:00")
    second = _msg("turn-2", "2026-10-01T10:01:00")
    await step._save_session_messages(_SESSION, [first])
    await step._save_session_messages(_SESSION, [second])

    _assert_transcript(step._session_path(_SESSION), ["turn-1", "turn-2"])
    assert len(durable_calls) == 2


@pytest.mark.asyncio
async def test_protected_redelivery_preserves_original_created_at_and_bytes(tmp_path, monkeypatch):
    durable_calls = []

    async def durable_write(path, content):
        durable_calls.append(content)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    monkeypatch.setattr(auto_memory, "write_file_durable", durable_write)
    step = AutoMemoryStep(durable_commit=True, agent_scope="test-scope")
    step.file_store = SimpleNamespace(workspace_path=tmp_path)
    step.agent_wrapper = SimpleNamespace(backend="agentscope")
    step.context = SimpleNamespace(
        data={"request_id": "request-1"},
        get=lambda key, default=None: {"request_id": "request-1"}.get(key, default),
    )

    await step._save_session_messages(_SESSION, [_msg("stable", "2026-10-01T10:00:00")])
    before = step._session_path(_SESSION).read_bytes()
    await step._save_session_messages(_SESSION, [_msg("stable", "2026-10-01T11:00:00")])

    assert step._session_path(_SESSION).read_bytes() == before
    saved = [
        Msg.model_validate_json(line)
        for line in step._session_path(_SESSION).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(saved) == 1
    assert saved[0].created_at == "2026-10-01T10:00:00"
    assert len(durable_calls) == 2


@pytest.mark.asyncio
async def test_default_duplicate_message_id_without_reordering_keeps_existing_bytes(step):
    await step._save_session_messages(_SESSION, [_msg("stable", "2026-10-01T23:59:59")])
    before = step._session_path(_SESSION).read_bytes()
    await step._save_session_messages(_SESSION, [_msg("stable", "2026-10-02T00:00:01")])

    assert step._session_path(_SESSION).read_bytes() == before
    messages = [
        Msg.model_validate_json(line)
        for line in step._session_path(_SESSION).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(messages) == 1
    assert messages[0].id == "stable"
    assert messages[0].created_at == "2026-10-01T23:59:59"
