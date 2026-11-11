"""Negative-path tests for ordinary ReMe index status and error sanitisation."""

from contextlib import chdir
import json

import pytest

from reme.components.file_store import LocalFileStore
from reme.steps.file_io._index_result import refresh_index_result
from reme.steps.file_io.daily_reindex import DailyReindexStep


@pytest.mark.asyncio
async def test_refresh_index_result_redacts_raw_yaml_excerpt():
    """A malformed derived page must not leak its original text into responses."""

    async def parse_error(*_args):
        return {
            "date": "2026-05-18",
            "path": "daily/2026-05-18.md",
            "notes": [],
            "created": False,
            "changed": False,
            "error": "daily index is unreadable: PRIVATE-PAGE-CONTENTS at line 5",
        }

    result = await refresh_index_result(parse_error, object(), "2026-05-18", "daily")
    assert result["error"] == "daily index is unreadable"
    assert result["error_kind"] == "content"
    assert "PRIVATE-PAGE-CONTENTS" not in json.dumps(result)


@pytest.mark.asyncio
async def test_refresh_index_result_redacts_oserror_and_preserves_error_kind():
    """A failed filesystem read is an error without exposing its host path."""

    async def no_access(*_args):
        raise PermissionError("/srv/host-private/fhm007-index")

    result = await refresh_index_result(no_access, object(), "2026-05-18", "daily")
    assert result["error"] == "daily index refresh failed"
    assert result["error_kind"] == "io"
    assert "host-private" not in json.dumps(result)


@pytest.mark.asyncio
async def test_daily_reindex_reports_sanitised_error_preserving_corrupt_bytes(tmp_path):
    """Standalone reindex returns failure without replacing damaged derived data."""
    idx = tmp_path / "daily/2026-05-18.md"
    idx.parent.mkdir(parents=True)
    raw = b"---\nprivate_field: [VERY_PRIVATE_MARKER\n---\nunchanged body\n"
    idx.write_bytes(raw)

    with chdir(tmp_path):
        store = LocalFileStore(name="test_store", embedding_store="")
        await store.start()
        try:
            step = DailyReindexStep(file_store=store)
            await step(date="2026-05-18")
            response = step.context.response
        finally:
            await store.close()

    assert response.success is False
    assert response.metadata["error_kind"] == "content"
    assert response.metadata["error"] == "daily index is unreadable"
    assert "VERY_PRIVATE_MARKER" not in json.dumps(response.model_dump(mode="json"))
    assert idx.read_bytes() == raw


@pytest.mark.asyncio
async def test_refresh_index_result_drops_untrusted_extra_fields():
    """Only validated status fields may escape a failed index operation."""

    async def injected_error(*_args):
        return {
            "date": "2026-05-18",
            "path": "/absolute/PRIVATE-HOST-PATH.md",
            "changed": False,
            "notes": [{"description": "PRIVATE-PAGE-CONTENTS"}],
            "debug": "PRIVATE-STACKTRACE",
            "error": "daily index is unreadable: PRIVATE-PAGE-CONTENTS",
        }

    result = await refresh_index_result(injected_error, object(), "2026-05-18", "daily")
    assert result["error_kind"] == "content"
    assert result["changed"] is False
    assert "path" not in result
    assert "notes" not in result
    assert "debug" not in result
    assert "PRIVATE" not in json.dumps(result)


@pytest.mark.asyncio
async def test_unexpected_refresh_programming_error_is_not_silently_reclassified():
    """Unexpected programmer faults remain exceptions, not ordinary I/O status."""

    async def broken_implementation(*_args):
        raise TypeError("synthetic contract violation")

    with pytest.raises(TypeError, match="synthetic contract violation"):
        await refresh_index_result(broken_implementation, object(), "2026-05-18", "daily")
