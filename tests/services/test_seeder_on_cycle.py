"""
Tests for run_seeder's on_cycle callback: called once after each completed
cycle, never for a cycle that raised, and optional.

The three cycle steps and the sleeps are patched at the seeder module, so
no Audible call, database or real wait is involved.
"""

# Standard library
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

# Third party
import pytest

# Services
from app.services import seeder

_STATS = {"authors_processed": 1, "series_processed": 1, "books_discovered": 2, "errors": 0}
_NARRATORS = {"narrators_processed": 1, "books_discovered": 1, "errors": 0}


def _steps(authors=None):
    return patch.multiple(
        seeder,
        _expand_authors=authors or AsyncMock(return_value=_STATS),
        _expand_series=AsyncMock(return_value=_STATS),
        _expand_narrators=AsyncMock(return_value=_NARRATORS),
    )


@pytest.fixture(autouse=True)
def _one_region(monkeypatch):
    monkeypatch.setattr(seeder.settings, "seeder_regions", "us")


async def test_on_cycle_is_called_once_for_a_completed_cycle():
    on_cycle = MagicMock()
    with _steps(), patch.object(seeder.asyncio, "sleep", AsyncMock()):
        await seeder.run_seeder(once=True, on_cycle=on_cycle)
    on_cycle.assert_called_once_with()


async def test_on_cycle_is_called_once_per_cycle_when_looping():
    on_cycle = MagicMock()
    sleeps = [None, None, None, asyncio.CancelledError()]  # start-up wait, then one wait per cycle

    with _steps(), patch.object(seeder.asyncio, "sleep", AsyncMock(side_effect=sleeps)):
        with pytest.raises(asyncio.CancelledError):
            await seeder.run_seeder(on_cycle=on_cycle)
    assert on_cycle.call_count == 3


async def test_on_cycle_is_not_called_when_the_cycle_raises():
    on_cycle = MagicMock()
    boom = AsyncMock(side_effect=RuntimeError("audible down"))
    with _steps(authors=boom), patch.object(seeder.asyncio, "sleep", AsyncMock()):
        await seeder.run_seeder(once=True, on_cycle=on_cycle)  # the failure is logged, not raised
    boom.assert_awaited_once()
    on_cycle.assert_not_called()


async def test_only_the_completed_cycles_are_reported():
    on_cycle = MagicMock()
    authors = AsyncMock(side_effect=[_STATS, RuntimeError("x"), _STATS])
    sleeps = [None, None, None, asyncio.CancelledError()]
    with _steps(authors=authors), patch.object(seeder.asyncio, "sleep", AsyncMock(side_effect=sleeps)):
        with pytest.raises(asyncio.CancelledError):
            await seeder.run_seeder(on_cycle=on_cycle)
    assert authors.await_count == 3
    assert on_cycle.call_count == 2  # the middle cycle failed


async def test_no_callback_is_fine():
    with _steps(), patch.object(seeder.asyncio, "sleep", AsyncMock()):
        await seeder.run_seeder(once=True)
