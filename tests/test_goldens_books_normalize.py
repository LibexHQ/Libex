"""
Golden pins for the books/chapters/series fetch-and-normalize surface.

The expected values in tests/goldens/*.json were captured from the code as it
stood before the move into libex_core, and compare as the exact
json.dumps(..., ensure_ascii=False) string with no sort_keys: key order is
part of identity, because this dict goes straight into the cache JSONB and a
reordered key is a different stored value. The inputs live in
tests/goldens/cases.py and every symbol is reached through
tests/goldens/seam.py, so a relocation repoints that one file.

A golden regenerated from relocated code proves nothing; these files change
only when behaviour is meant to change.
"""

# Standard library
import copy
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

# Third party
import pytest

# Local
from tests.goldens import cases, seam

GOLDEN_DIR = Path(__file__).parent / "goldens"


def _golden(name: str):
    return json.loads((GOLDEN_DIR / name).read_text(encoding="utf-8"))


def _dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)


PRODUCT_GOLDEN = _golden("normalize_product.json")
PRODUCT_CASES = cases.product_cases()


# ============================================================
# _normalize_product
# ============================================================

def test_product_golden_covers_every_case():
    assert list(PRODUCT_CASES) == list(PRODUCT_GOLDEN)
    assert len(PRODUCT_CASES) == 11 + 1 + 1 + 1 + 5 + 18 + 6 + 1 + 1 + 1


@pytest.mark.parametrize("name", list(PRODUCT_CASES))
def test_normalize_product_matches_golden(name):
    product, region = PRODUCT_CASES[name]
    assert _dump(seam.normalize_product(product, region)) == PRODUCT_GOLDEN[name]


def test_normalize_product_covers_all_eleven_regions():
    assert [n for n in PRODUCT_CASES if n.startswith("fixture_")] == [
        f"fixture_{r}" for r in cases.REGIONS
    ]
    assert len(cases.REGIONS) == 11


def test_normalize_product_has_43_keys_and_extras_withheld_only_when_withheld():
    plain = seam.normalize_product(*PRODUCT_CASES["fixture_us"])
    assert len(plain) == 43
    assert "extrasWithheld" not in plain
    assert list(plain)[-1] == "audibleExtras"

    withheld = seam.normalize_product(*PRODUCT_CASES["podcast_episodes"])
    assert len(withheld) == 44
    assert list(withheld)[-1] == "extrasWithheld"
    assert withheld["extrasWithheld"] == {"relationships": {"episode": 2, "season": 1}}
    # Episodes are stripped from the blob, the series entry is kept.
    assert [r["relationship_type"] for r in withheld["audibleExtras"]["relationships"]] == ["series"]
    assert withheld["episodeNumber"] == "7"
    assert withheld["episodeType"] == "full"


def test_normalize_product_does_not_mutate_its_input():
    product, region = cases.base(), "us"
    snapshot = copy.deepcopy(product)
    seam.normalize_product(product, region)
    assert product == snapshot


def test_normalize_product_tri_state_values_are_pinned():
    no_plans = seam.normalize_product(*PRODUCT_CASES["plans_absent"])
    assert no_plans["plans"] is None
    assert seam.normalize_product(*PRODUCT_CASES["plans_empty"])["plans"] == []
    assert seam.normalize_product(*PRODUCT_CASES["plans_unreadable"])["plans"] is None
    assert seam.normalize_product(*PRODUCT_CASES["plans_partial"])["plans"] == ["US Minerva"]
    for key, field in [
        ("is_adult_product", "explicit"),
        ("is_pdf_url_available", "hasPdf"),
        ("read_along_support", "whisperSync"),
        ("is_listenable", "isListenable"),
        ("is_vvab", "isVvab"),
    ]:
        assert seam.normalize_product(*PRODUCT_CASES[f"flag_{key}_absent"])[field] is None
        assert seam.normalize_product(*PRODUCT_CASES[f"flag_{key}_false"])[field] is False
        assert seam.normalize_product(*PRODUCT_CASES[f"flag_{key}_true"])[field] is True
    absent = seam.normalize_product(*PRODUCT_CASES["flag_is_buyable_absent"])
    assert absent["isBuyable"] is None and absent["isAvailable"] is None


def test_normalize_product_withholding_records_are_pinned():
    expected = {
        "extras_nul_char": {"sanitized": {"nulCharacters": 3}},
        "extras_inf": {"sanitized": {"nonFiniteNumbers": 3}},
        "extras_oversized_int": {"sanitized": {"oversizedNumbers": 1}},
        "extras_depth_33": {"audibleExtras": "depth"},
        "extras_blob_over_64k": {"audibleExtras": "size"},
    }
    for name, record in expected.items():
        assert seam.normalize_product(*PRODUCT_CASES[name])["extrasWithheld"] == record
    for name in ("extras_depth_32", "extras_unicode_under_cap"):
        out = seam.normalize_product(*PRODUCT_CASES[name])
        assert "extrasWithheld" not in out and out["audibleExtras"] is not None
    for name in ("extras_depth_33", "extras_blob_over_64k"):
        assert seam.normalize_product(*PRODUCT_CASES[name])["audibleExtras"] is None


# ============================================================
# _normalize_products -- the worker-thread path
# ============================================================

BATCH_GOLDEN = _golden("normalize_products_batch.json")["batch_150"]


async def test_normalize_products_over_threshold_runs_threaded_and_matches_golden():
    batch = cases.thread_batch(150)
    assert len(batch) >= seam.thread_threshold()
    import asyncio
    with patch("asyncio.to_thread", wraps=asyncio.to_thread) as hop:
        out = await seam.normalize_products(batch, "us")
    assert hop.call_count == 1
    assert _dump(out) == BATCH_GOLDEN


async def test_normalize_products_threaded_equals_inline_for_same_products():
    threshold = seam.thread_threshold()
    batch = cases.thread_batch(threshold)
    threaded = await seam.normalize_products(batch, "us")
    inline = [seam.normalize_product(p, "us") for p in batch]
    assert _dump(threaded) == _dump(inline)


async def test_normalize_products_below_threshold_stays_inline():
    batch = cases.thread_batch(seam.thread_threshold() - 1)
    with patch("asyncio.to_thread", new=AsyncMock()) as hop:
        out = await seam.normalize_products(batch, "us")
    hop.assert_not_called()
    assert _dump(out) == _dump([seam.normalize_product(p, "us") for p in batch])


def test_thread_threshold_is_100():
    assert seam.thread_threshold() == 100


# ============================================================
# _normalize_chapters / _normalize_series
# ============================================================

CHAPTER_GOLDEN = _golden("normalize_chapters.json")
SERIES_GOLDEN = _golden("normalize_series.json")


@pytest.mark.parametrize("name", list(cases.chapter_cases()))
def test_normalize_chapters_matches_golden(name):
    data, asin = cases.chapter_cases()[name]
    assert _dump(seam.normalize_chapters(data, asin)) == CHAPTER_GOLDEN[name]


def test_normalize_chapters_keeps_nested_subchapters():
    data, asin = cases.chapter_cases()["nested_subchapters"]
    out = seam.normalize_chapters(data, asin)
    assert [c["title"] for c in out["chapters"]] == ["Part One", "Sparse"]
    assert out["chapters"][0]["chapters"] == [
        {"lengthMs": 4000, "startOffsetMs": 0, "startOffsetSec": 0, "title": "Sub A"},
        {"lengthMs": 5000, "startOffsetMs": 4000, "startOffsetSec": 4, "title": "Sub B"},
    ]
    # A chapter Audible sent no sub-chapters for carries no chapters key at all.
    assert out["chapters"][1] == {"lengthMs": 100, "startOffsetMs": 0, "startOffsetSec": 0, "title": "Sparse"}


def test_normalize_chapters_empty_and_brandless_defaults():
    data, asin = cases.chapter_cases()["empty"]
    out = seam.normalize_chapters(data, asin)
    assert out == {
        "brandIntroDurationMs": 0, "brandOutroDurationMs": 0, "isAccurate": False,
        "runtimeLengthMs": 0, "runtimeLengthSec": 0, "chapters": [],
    }
    data, asin = cases.chapter_cases()["brand_keys_missing"]
    out = seam.normalize_chapters(data, asin)
    assert out["brandIntroDurationMs"] == 0 and out["brandOutroDurationMs"] == 0
    assert out["isAccurate"] is True and len(out["chapters"]) == 2


@pytest.mark.parametrize("name", list(cases.series_cases()))
def test_normalize_series_matches_golden(name):
    product, region = cases.series_cases()[name]
    assert _dump(seam.normalize_series(product, region)) == SERIES_GOLDEN[name]


# ============================================================
# _settle_flags
# ============================================================

SETTLE_GOLDEN = _golden("settle_flags.json")


@pytest.mark.parametrize("name", list(cases.settle_cases()))
def test_settle_flags_matches_golden_and_does_not_mutate(name):
    book = cases.settle_cases()[name]
    snapshot = copy.deepcopy(book)
    out = seam.settle_flags(book)
    assert _dump(out) == SETTLE_GOLDEN[name]
    assert book == snapshot
    assert out is not book


def test_settle_flags_values():
    settled = seam.settle_flags(cases.settle_cases()["all_none"])
    assert settled["plans"] == []
    assert (settled["isListenable"], settled["isAvailable"], settled["isBuyable"]) == (True, True, True)
    assert (settled["isVvab"], settled["explicit"], settled["hasPdf"], settled["whisperSync"]) == (False,) * 4
    # A key a dict never carried is never added.
    assert seam.settle_flags({"asin": "X"}) == {"asin": "X"}


# ============================================================
# _REPRODUCED_KEYS
# ============================================================

def test_reproduced_keys_are_the_current_24():
    expected = _golden("reproduced_keys.json")
    assert len(expected) == 24
    assert seam.reproduced_keys() == frozenset(expected)


# ============================================================
# Persistence receives the UNSETTLED dicts
# ============================================================

def _bare_product(asin: str, **extra) -> dict:
    """No flags, no plans: every tri-state comes out of the normalizer as None."""
    return {
        "asin": asin, "title": f"T {asin}", "authors": [], "narrators": [],
        "relationships": [], "product_images": {}, "category_ladders": [],
        "rating": {"overall_distribution": {"average_rating": 4.0}},
        "release_date": "2020-01-01", "publication_datetime": "2020-01-01T00:00:00Z",
        **extra,
    }


FLAG_FIELDS = ("isListenable", "isAvailable", "isBuyable", "isVvab", "explicit", "hasPdf", "whisperSync")


def _assert_unsettled(persisted: list[dict]):
    assert persisted, "persist_books_background received nothing"
    for book in persisted:
        for field in FLAG_FIELDS:
            assert book[field] is None, field
        assert book["plans"] is None


def _assert_settled(returned: list[dict]):
    assert returned
    for book in returned:
        assert all(book[f] is not None for f in FLAG_FIELDS)
        assert book["plans"] == []


async def test_get_books_by_asins_persists_unsettled_and_returns_settled():
    with patch(seam.PATCH_BOOKS_AUDIBLE_GET, new=AsyncMock(return_value={"product": _bare_product("B0PERSIS01")})), \
         patch(seam.PATCH_BOOKS_PERSIST) as persist, \
         patch(seam.PATCH_BOOKS_CACHE_GET, new=AsyncMock(return_value=None)):
        returned = await seam.get_books_by_asins()(["B0PERSIS01"], "us", AsyncMock())
    persist.assert_called_once()
    _assert_unsettled(persist.call_args[0][0])
    _assert_settled(returned)


async def test_search_persists_unsettled_and_returns_settled():
    page = {"products": [_bare_product("B0PERSIS02"), _bare_product("B0PERSIS03")]}
    with patch(seam.PATCH_SEARCH_AUDIBLE_GET, new=AsyncMock(return_value=page)), \
         patch(seam.PATCH_SEARCH_PERSIST) as persist:
        returned = await seam.search()("us", AsyncMock(), title="x")
    persist.assert_called_once()
    _assert_unsettled(persist.call_args[0][0])
    _assert_settled(returned)


def _release_get(product):
    def _get(region, path, params=None):
        if (params or {}).get("page", 0) == 0 and "/categories" not in path:
            return {"products": [product]}
        return {"products": []}
    return AsyncMock(side_effect=_get)


async def _releases_run(getter, days_offset):
    from datetime import datetime, timedelta, timezone
    dt = datetime.now(timezone.utc) + timedelta(days=days_offset)
    product = _bare_product(
        "B0PERSIS04",
        release_date=dt.strftime("%Y-%m-%d"),
        publication_datetime=dt.isoformat(),
    )
    cache_set = AsyncMock()
    with patch(seam.PATCH_RELEASES_AUDIBLE_GET, new=_release_get(product)), \
         patch(seam.PATCH_RELEASES_PERSIST) as persist, \
         patch(seam.PATCH_RELEASES_CACHE_GET, new=AsyncMock(return_value=None)), \
         patch(seam.PATCH_RELEASES_CACHE_SET, new=cache_set):
        returned = await getter("us", AsyncMock(), days=30, category="C1")
    return persist, returned, cache_set


async def test_new_releases_persists_unsettled_and_returns_and_caches_settled():
    persist, returned, cache_set = await _releases_run(seam.get_new_releases(), -3)
    persist.assert_called_once()
    _assert_unsettled(persist.call_args[0][0])
    _assert_settled(returned)
    _assert_settled(cache_set.call_args[0][2])


async def test_coming_soon_persists_unsettled_and_returns_and_caches_settled():
    persist, returned, cache_set = await _releases_run(seam.get_coming_soon(), 3)
    persist.assert_called_once()
    _assert_unsettled(persist.call_args[0][0])
    _assert_settled(returned)
    _assert_settled(cache_set.call_args[0][2])
