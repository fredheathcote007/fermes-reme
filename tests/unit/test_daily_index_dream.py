"""Regression tests for daily-index errors during dream extraction."""

from unittest.mock import AsyncMock, Mock, patch

import pytest

from reme.components.application_context import ApplicationContext
from reme.components.file_catalog import BaseFileCatalog
from reme.components.file_store import BaseFileStore
from reme.components.runtime_context import RuntimeContext
from reme.steps.evolve.dream.extract import DreamExtractStep


@pytest.mark.asyncio
async def test_dream_extract_reports_unreadable_index_without_mutating_source(tmp_path):
    """Index refresh failure must not be mistaken for successful extraction."""
    note = tmp_path / "daily/2026-05-28/session.md"
    note.parent.mkdir(parents=True)
    note.write_text("source input", encoding="utf-8")
    catalog = Mock(spec=BaseFileCatalog)
    catalog.get_nodes = AsyncMock(return_value=[])
    catalog.delete = AsyncMock()
    store = Mock(spec=BaseFileStore)
    store.workspace_path = tmp_path
    step = DreamExtractStep(scan_days=1, app_context=ApplicationContext(workspace_dir=str(tmp_path)))

    with (
        patch(
            "reme.steps.evolve.dream.extract.refresh_day_index",
            new=AsyncMock(return_value={"error": "daily index is unreadable: malformed YAML"}),
        ),
        patch("reme.steps.evolve.dream.extract.llm_available", return_value=False),
    ):
        response = await step(RuntimeContext(date="2026-05-28", file_catalog=catalog, file_store=store))

    assert response.success is False
    assert "daily index refresh failed" in response.answer
    assert "daily index is unreadable" in response.answer
    assert any("daily index refresh failed" in error for error in response.metadata["dream"]["errors"])
    assert note.read_text(encoding="utf-8") == "source input"


@pytest.mark.asyncio
@pytest.mark.parametrize("has_note", [False, True])
async def test_dream_index_error_does_not_scan_or_checkpoint_failed_day(tmp_path, has_note):
    """Broken derived navigation stops Dream before catalog/LLM side effects."""
    if has_note:
        note = tmp_path / "daily/2026-05-28/session.md"
        note.parent.mkdir(parents=True)
        note.write_text("synthetic source note", encoding="utf-8")
    catalog = Mock(spec=BaseFileCatalog)
    catalog.get_nodes = AsyncMock(return_value=[])
    catalog.delete = AsyncMock()
    store = Mock(spec=BaseFileStore)
    store.workspace_path = tmp_path
    step = DreamExtractStep(scan_days=1, app_context=ApplicationContext(workspace_dir=str(tmp_path)))

    with (
        patch(
            "reme.steps.evolve.dream.extract.refresh_day_index",
            new=AsyncMock(return_value={"error": "daily index is unreadable: malicious source text"}),
        ),
        patch("reme.steps.evolve.dream.extract.llm_available", return_value=False),
    ):
        response = await step(RuntimeContext(date="2026-05-28", file_catalog=catalog, file_store=store))

    assert response.success is False
    assert "daily index refresh failed" in response.answer
    catalog.get_nodes.assert_not_awaited()
    catalog.delete.assert_not_awaited()
    assert response.metadata["dream"]["checkpoint_paths"] == []
    assert response.metadata["dream"]["failed_paths"] == ["daily/2026-05-28.md"]


@pytest.mark.asyncio
async def test_dream_two_day_index_failure_never_checkpoints_source(tmp_path):
    """A mixed healthy/corrupt scan window cannot consume source on failed refresh."""
    from reme.steps.evolve.dream.finish import DreamFinishStep

    note = tmp_path / "daily/2026-05-28/session.md"
    note.parent.mkdir(parents=True)
    note.write_text("source still eligible for retry", encoding="utf-8")
    catalog = Mock(spec=BaseFileCatalog)
    catalog.get_nodes = AsyncMock(return_value=[])
    catalog.delete = AsyncMock()
    catalog.upsert = AsyncMock()
    catalog.dump = AsyncMock()
    store = Mock(spec=BaseFileStore)
    store.workspace_path = tmp_path
    ctx = ApplicationContext(workspace_dir=str(tmp_path))
    extract = DreamExtractStep(scan_days=2, app_context=ctx)

    async def refresh(_store, scan_day, _daily_dir):
        if scan_day == "2026-05-27":
            return {"error": "daily index is unreadable: PRIVATE-FILE-CONTENT"}
        return {"date": scan_day, "path": f"daily/{scan_day}.md", "notes": [], "created": False}

    with patch("reme.steps.evolve.dream.extract.refresh_day_index", new=refresh):
        response = await extract(RuntimeContext(date="2026-05-28", file_catalog=catalog, file_store=store))

    assert response.success is False
    assert "PRIVATE-FILE-CONTENT" not in response.answer
    state = response.metadata["dream"]
    assert state["failed_paths"] == ["daily/2026-05-27.md"]
    assert state["checkpoint_paths"] == []
    assert state["changed_paths"] == []

    finish = DreamFinishStep(app_context=ctx)
    completed = await finish(RuntimeContext(dream=state, file_catalog=catalog, file_store=store))
    assert completed.success is False
    assert completed.metadata["dream"]["checkpoint_paths"] == []
    catalog.upsert.assert_not_awaited()
    catalog.dump.assert_not_awaited()

    # After the derived index is repaired, the source is still uncheckpointed
    # and will be selected for extraction again.
    async def healthy_refresh(_store, scan_day, _daily_dir):
        return {"date": scan_day, "path": f"daily/{scan_day}.md", "notes": [], "created": False}

    with (
        patch("reme.steps.evolve.dream.extract.refresh_day_index", new=healthy_refresh),
        patch("reme.steps.evolve.dream.extract.llm_available", return_value=False),
    ):
        retried = await extract(RuntimeContext(date="2026-05-28", file_catalog=catalog, file_store=store))
    assert retried.metadata["dream"]["changed_paths"] == ["daily/2026-05-28/session.md"]
    assert retried.metadata["dream"]["failed_paths"] == ["daily/2026-05-28/session.md"]
    assert note.read_text(encoding="utf-8") == "source still eligible for retry"
