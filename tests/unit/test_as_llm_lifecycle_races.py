"""Negative lifecycle tests for the OpenAI-owned client (upstream #725).

These intentionally exercise async cancellation and post-shutdown access that
the existing sequential lazy-provider tests cannot cover.
"""

# pylint: disable=protected-access

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from reme.components.as_llm import BaseAsLLM, OpenAIAsLLM


def _owned_model(llm):
    model = Mock()
    credential_cls = Mock()
    credential_cls.return_value.get_chat_model_class.return_value = Mock(return_value=model)
    llm.credential_cls = credential_cls
    return model, credential_cls


@pytest.mark.asyncio
async def test_cancelled_close_finishes_owned_client_cleanup_before_returning():
    """Cancellation cannot abandon an owned client before its close finishes."""
    llm = OpenAIAsLLM(backend="openai", model="synthetic")
    model, _ = _owned_model(llm)
    entered = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()

    async def blocked_client_close():
        entered.set()
        await release.wait()
        finished.set()

    model.client.close = AsyncMock(side_effect=blocked_client_close)
    await llm.start()
    assert llm.model is model
    closing = asyncio.create_task(llm.close())
    await asyncio.wait_for(entered.wait(), 2)
    closing.cancel()
    try:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(closing, 2)
        assert finished.is_set(), "Cancelled component close abandoned the owned client"
        model.client.close.assert_awaited_once_with()
    finally:
        release.set()
        await asyncio.gather(closing, return_exceptions=True)


@pytest.mark.asyncio
async def test_model_cannot_be_recreated_after_shutdown():
    """A closed OpenAI component must fence late lazy-model construction."""
    llm = OpenAIAsLLM(backend="openai", model="synthetic")
    model, credential = _owned_model(llm)
    model.client.close = AsyncMock()
    await llm.start()
    assert llm.model is model
    await llm.close()
    with pytest.raises(RuntimeError, match="Start the LLM component"):
        _ = llm.model
    credential.return_value.get_chat_model_class.return_value.assert_called_once()


@pytest.mark.asyncio
async def test_prestart_owned_client_is_closed_once():
    """Pre-start lazy model ownership must still be released on close."""
    llm = OpenAIAsLLM(backend="openai", model="synthetic")
    model, _ = _owned_model(llm)
    model.client.close = AsyncMock()
    assert llm.model is model
    await llm.close()
    model.client.close.assert_awaited_once_with()
    with pytest.raises(RuntimeError, match="Start the LLM component"):
        _ = llm.model


@pytest.mark.asyncio
async def test_borrowed_model_is_not_closed_by_component():
    """Injected models remain borrowed and available after owner close."""
    llm = OpenAIAsLLM(backend="openai", model="synthetic")
    injected = Mock()
    injected.client.close = AsyncMock()
    llm.model = injected
    await llm.start()
    await llm.close()
    injected.client.close.assert_not_awaited()
    assert llm.model is injected


@pytest.mark.asyncio
async def test_non_openai_lazy_wrapper_semantics_unchanged_after_close():
    """Non-OpenAI implementations retain their existing lazy getter policy."""

    class OtherLLM(BaseAsLLM):
        """Synthetic provider not subject to OpenAI client ownership rules."""

        credential_cls = Mock()

    llm = OtherLLM(backend="fake", model="synthetic")
    model, credential = _owned_model(llm)
    await llm.start()
    assert llm.model is model
    await llm.close()
    assert llm.model is model
    credential.return_value.get_chat_model_class.return_value.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ["resolve", "hook"])
async def test_startup_rollback_preserves_original_error_after_cleanup_cancellation(failure_stage):
    """BaseComponent preserves the original startup exception after rollback.

    Cancellation during best-effort rollback must not strand an owned client.
    A future change to the shared BaseComponent contract requires separate review.
    """

    class FailingOpenAIAsLLM(OpenAIAsLLM):
        """Fail before/after the _start hook without modifying the base class."""

        async def _resolve_bindings(self):
            if failure_stage == "resolve":
                raise RuntimeError("original start failure")
            await super()._resolve_bindings()

        async def _start(self):
            raise RuntimeError("original start failure")

    llm = FailingOpenAIAsLLM(backend="openai", model="synthetic")
    model, credential_cls = _owned_model(llm)
    replacement = Mock()
    model.client.close = AsyncMock()
    replacement.client.close = AsyncMock()
    credential_cls.return_value.get_chat_model_class.return_value.side_effect = [model, replacement]
    assert llm.model is model
    released = asyncio.Event()
    resume = asyncio.Event()
    release_original = llm._release_model

    async def gated_release():
        await release_original()
        released.set()
        await resume.wait()

    llm._release_model = gated_release
    starting = asyncio.create_task(llm.start())
    await asyncio.wait_for(released.wait(), 2)
    assert not starting.done()
    starting.cancel()
    resume.set()

    with pytest.raises(RuntimeError, match="original start failure"):
        await asyncio.wait_for(starting, 2)
    model.client.close.assert_awaited_once_with()
    assert not llm.is_started
    assert llm._closing_depth == 0
    assert not llm._closed
    assert llm._model is None
    assert llm.model is replacement
    assert credential_cls.return_value.get_chat_model_class.return_value.call_count == 2
    await llm.close()
    replacement.client.close.assert_awaited_once_with()
