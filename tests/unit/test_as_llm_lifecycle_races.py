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
