"""Deterministic FHM-007 protected-note publication and restart coverage."""

from __future__ import annotations

import pytest

from reme.application import Application
from reme.components.runtime_context import RuntimeContext
from reme.schema import Response
from reme.steps.evolve.auto_memory import AutoMemoryStep


def _app(workspace) -> Application:
    """Build the BM25-only application without an LLM or watcher job."""
    return Application(
        workspace_dir=str(workspace),
        enable_logo=False,
        log_to_console=False,
        log_to_file=False,
        service={"backend": "cli"},
        components={
            "tokenizer": {"default": {"backend": "regex"}},
            "file_graph": {"default": {"backend": "local"}},
            "file_chunker": {
                "markdown": {
                    "backend": "markdown",
                    "supported_extensions": ["md"],
                },
            },
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
            "search": {
                "backend": "base",
                "steps": [
                    {
                        "backend": "search_step",
                        "vector_weight": 0.0,
                        "expand_links": False,
                    },
                ],
            },
        },
    )


# These accesses intentionally validate the internal publication boundary.
# pylint: disable=protected-access
@pytest.mark.asyncio
async def test_option_b_publication_survives_immediate_restart(tmp_path):
    """A committed synthetic note is in BM25 before shutdown and on first restart query."""
    note = tmp_path / "daily" / "2026-10-27" / "amber-kite.md"
    note.parent.mkdir(parents=True)
    note.write_text(
        "---\nname: Amber Kite\ndescription: synthetic\n---\n"
        "The calibration case is teal and the reference is FHM007-readiness-2710.\n",
        encoding="utf-8",
    )
    changes = [{"change": "added", "path": "daily/2026-10-27/amber-kite.md"}]

    first = _app(tmp_path)
    await first.start()
    try:
        before = await first.run_job("search", query="FHM007-readiness-2710", limit=5)
        assert before.metadata["counts"]["returned"] == 0

        publisher = AutoMemoryStep(app_context=first.context)
        publisher.context = RuntimeContext(changes=changes)
        await publisher._publish_option_b_index()

        file_store = first.context.components["file_store"]["default"]
        assert file_store._mutation_generation == 1
        assert file_store.keyword_index.n_docs == 1
        assert set(file_store.keyword_index.document_ids) == set(file_store.file_chunks)
    finally:
        await first.close()

    restarted = _app(tmp_path)
    await restarted.start()
    try:
        after = await restarted.run_job("search", query="FHM007-readiness-2710", limit=5)
    finally:
        await restarted.close()

    assert after.success is True
    assert after.metadata["counts"] == {
        "vector": 0,
        "keyword": 1,
        "returned": 1,
        "hybrid": False,
    }
    assert "teal" in after.answer


@pytest.mark.asyncio
async def test_option_b_publication_fails_closed_without_false_readiness():
    """A failed publication is not treated as a successful protected commit."""
    step = AutoMemoryStep()
    step.context = RuntimeContext(changes=[{"change": "added", "path": "daily/note.md"}])

    async def failed_dispatch(*_args, **_kwargs):
        return [Response(success=False, answer="index failed")]

    step.dispatch_steps = failed_dispatch

    with pytest.raises(RuntimeError, match="index publication failed"):
        await step._publish_option_b_index()


# pylint: enable=protected-access
