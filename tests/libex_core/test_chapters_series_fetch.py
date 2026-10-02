"""
libex_core.audible.chapters and libex_core.audible.series: what each fetch
promises before anything is sent (region and ASIN validated, the rejected
value never echoed), the request it then makes, and how it reads Audible's
answer.

Each fetch is handed `get` rather than owning a client, so every test here
passes a stand-in and asserts on whether it was called. Nothing touches a
network.
"""

# Standard library
import copy
import logging
from unittest.mock import AsyncMock

# Third party
import pytest

# Local
from libex_core.audible.chapters import (
    CHAPTERS_QUALITY,
    CHAPTERS_RESPONSE_GROUPS,
    fetch_chapter_metadata,
    normalize_chapters,
)
from libex_core.audible.series import (
    SERIES_BOOKS_RESPONSE_GROUPS,
    SERIES_RESPONSE_GROUPS,
    fetch_series,
    fetch_series_book_asins,
    fetch_series_search_asins,
    normalize_series,
)
from libex_core.exceptions import AudibleAPIException, NotFoundException, RegionException

BAD_ASINS = [
    "B0SHORT",
    "B0ELEVENCHR",
    "B0FETCH00!",
    "../etc/pw",
    "B0FETCH00/",
    "",
    None,
    12345,
]
BAD_REGIONS = ["xx", "", "usa", "mars"]

FETCHES = [
    pytest.param(fetch_chapter_metadata, id="chapters"),
    pytest.param(fetch_series, id="series"),
    pytest.param(fetch_series_book_asins, id="series_books"),
]


# ============================================================
# Validated before anything is sent
# ============================================================

@pytest.mark.parametrize("fetch", FETCHES)
@pytest.mark.parametrize("region", BAD_REGIONS)
async def test_fetch_rejects_an_unknown_region_before_any_request(fetch, region):
    get = AsyncMock()
    with pytest.raises(RegionException):
        await fetch(get, "B0FETCH001", region)
    get.assert_not_awaited()


@pytest.mark.parametrize("fetch", FETCHES)
@pytest.mark.parametrize("bad", BAD_ASINS)
async def test_fetch_rejects_a_non_asin_before_any_request(fetch, bad):
    get = AsyncMock()
    with pytest.raises(ValueError):
        await fetch(get, bad, "us")
    get.assert_not_awaited()


@pytest.mark.parametrize("fetch", FETCHES)
async def test_fetch_error_message_never_echoes_the_rejected_value(fetch):
    get = AsyncMock()
    with pytest.raises(ValueError) as exc:
        await fetch(get, "inject\nline", "us")
    assert "inject" not in str(exc.value)


@pytest.mark.parametrize("fetch", FETCHES)
async def test_fetch_takes_get_as_a_required_first_argument(fetch):
    with pytest.raises(TypeError):
        await fetch("B0FETCH001", "us")


@pytest.mark.parametrize("fetch", FETCHES)
async def test_fetch_normalises_the_region_it_passes_to_get(fetch):
    get = AsyncMock(return_value={})
    try:
        await fetch(get, "B0FETCH001", " US ")
    except NotFoundException:
        pass
    assert get.await_args.args[0] == "us"


# ============================================================
# fetch_chapter_metadata: request and answer
# ============================================================

async def test_fetch_chapter_metadata_requests_the_content_metadata_path_with_pinned_params():
    get = AsyncMock(return_value={"content_metadata": {"chapter_info": {"chapters": []}}})
    await fetch_chapter_metadata(get, "b0fetch001", "uk")
    get.assert_awaited_once_with(
        "uk",
        "/1.0/content/B0FETCH001/metadata",
        {
            "response_groups": CHAPTERS_RESPONSE_GROUPS,
            "quality": CHAPTERS_QUALITY,
        },
    )
    assert CHAPTERS_RESPONSE_GROUPS == "chapter_info, always-returned, content_reference, content_url"
    assert CHAPTERS_QUALITY == "High"


async def test_fetch_chapter_metadata_returns_the_response_as_audible_sent_it():
    payload = {"content_metadata": {"chapter_info": {"chapters": [1]}}, "extra": "kept"}
    get = AsyncMock(return_value=payload)
    assert await fetch_chapter_metadata(get, "B0FETCH001", "us") is payload


async def test_fetch_chapter_metadata_returns_a_response_without_chapter_info_unraised():
    get = AsyncMock(return_value={"content_metadata": {}})
    assert await fetch_chapter_metadata(get, "B0FETCH001", "us") == {"content_metadata": {}}


async def test_fetch_chapter_metadata_lets_a_404_from_get_through():
    get = AsyncMock(side_effect=NotFoundException("gone"))
    with pytest.raises(NotFoundException):
        await fetch_chapter_metadata(get, "B0FETCH001", "us")


# ============================================================
# fetch_series: request and not-found handling
# ============================================================

async def test_fetch_series_requests_the_product_path_with_pinned_params():
    product = {"asin": "B0SERIES01", "title": "Dune"}
    get = AsyncMock(return_value={"response_groups": ["a", "b"], "product": product})
    await fetch_series(get, "b0series01", "de")
    get.assert_awaited_once_with(
        "de",
        "/1.0/catalog/products/B0SERIES01",
        {"response_groups": SERIES_RESPONSE_GROUPS},
    )
    assert SERIES_RESPONSE_GROUPS == "product_attrs, product_desc, product_extended_attrs"


async def test_fetch_series_returns_the_product_raw():
    product = {"asin": "B0SERIES01", "title": "Dune", "unmodelled": {"k": 1}}
    get = AsyncMock(return_value={"response_groups": ["a", "b"], "product": product})
    assert await fetch_series(get, "B0SERIES01", "us") is product


@pytest.mark.parametrize("data", [
    None,
    {},
    {"product": {"asin": "B0SERIES01"}},
    {"response_groups": [], "product": {"asin": "B0SERIES01"}},
    {"response_groups": ["only_one"], "product": {"asin": "B0SERIES01"}},
    {"response_groups": ["a", "b"]},
    {"response_groups": ["a", "b"], "product": {}},
    {"response_groups": ["a", "b"], "product": None},
])
async def test_fetch_series_not_found_when_audible_answers_for_no_series(data):
    get = AsyncMock(return_value=data)
    with pytest.raises(NotFoundException) as exc:
        await fetch_series(get, "B0SERIES01", "us")
    assert "B0SERIES01" in exc.value.message


async def test_fetch_series_lets_a_failure_from_get_through():
    boom = RuntimeError("transient")
    get = AsyncMock(side_effect=boom)
    with pytest.raises(RuntimeError) as exc:
        await fetch_series(get, "B0SERIES01", "us")
    assert exc.value is boom


# ============================================================
# fetch_series_book_asins: request, ordering and not-found handling
# ============================================================

async def test_fetch_series_book_asins_requests_the_relationships_group():
    get = AsyncMock(return_value={"product": {"relationships": [{"asin": "B0BOOK0001", "sort": "1"}]}})
    await fetch_series_book_asins(get, "b0series01", "fr")
    get.assert_awaited_once_with(
        "fr",
        "/1.0/catalog/products/B0SERIES01",
        {"response_groups": SERIES_BOOKS_RESPONSE_GROUPS},
    )
    assert SERIES_BOOKS_RESPONSE_GROUPS == "relationships"


async def test_fetch_series_book_asins_sorts_numerically_and_drops_entries_missing_either_field():
    get = AsyncMock(return_value={"product": {"relationships": [
        {"asin": "B0BOOK0010", "sort": "10"},
        {"asin": "B0BOOK0002", "sort": "2"},
        {"asin": "B0BOOK0015", "sort": "1.5"},
        {"asin": "B0NOSORT01"},
        {"sort": "3"},
        {"asin": "B0EMPTYSRT", "sort": ""},
    ]}})
    assert await fetch_series_book_asins(get, "B0SERIES01", "us") == [
        "B0BOOK0015", "B0BOOK0002", "B0BOOK0010",
    ]


@pytest.mark.parametrize("data", [
    {},
    {"product": {}},
    {"product": {"relationships": []}},
    {"product": {"relationships": [{"asin": "B0NOSORT01"}, {"sort": "1"}]}},
])
async def test_fetch_series_book_asins_not_found_when_no_member_qualifies(data):
    get = AsyncMock(return_value=data)
    with pytest.raises(NotFoundException) as exc:
        await fetch_series_book_asins(get, "B0SERIES01", "us")
    assert "B0SERIES01" in exc.value.message


# ============================================================
# fetch_series_search_asins
# ============================================================

# Distinctive enough that a substring check cannot pass by accident.
TYPED_NAME = "Zorbleflux Quennathrix Saga"


def _search_answer(*products):
    return {"products": [{"relationships": list(rels)} for rels in products]}


def _series_rel(asin):
    return {"relationship_type": "series", "asin": asin}


async def test_fetch_series_search_asins_makes_the_pinned_request():
    get = AsyncMock(return_value={"products": []})
    await fetch_series_search_asins(get, TYPED_NAME, "uk")
    get.assert_awaited_once_with(
        "uk",
        "/1.0/catalog/products",
        {"title": TYPED_NAME, "response_groups": "relationships", "num_results": 10},
    )


@pytest.mark.parametrize("region", BAD_REGIONS)
async def test_fetch_series_search_asins_rejects_an_unknown_region_before_any_request(region):
    get = AsyncMock()
    with pytest.raises(RegionException) as raised:
        await fetch_series_search_asins(get, TYPED_NAME, region)
    get.assert_not_called()
    assert TYPED_NAME not in str(raised.value)


async def test_fetch_series_search_asins_normalises_the_region_it_passes_to_get():
    get = AsyncMock(return_value={"products": []})
    await fetch_series_search_asins(get, "x", "UK")
    assert get.await_args.args[0] == "uk"


async def test_fetch_series_search_asins_dedups_keeping_first_seen_order():
    get = AsyncMock(return_value=_search_answer(
        [_series_rel("B0SERIES2X"), {"relationship_type": "episode", "asin": "B0EPISODE1"}],
        [_series_rel("B0SERIES1X"), _series_rel("B0SERIES2X")],
        [_series_rel("B0SERIES3X"), _series_rel("B0SERIES1X")],
    ))
    assert await fetch_series_search_asins(get, "x", "us") == ["B0SERIES2X", "B0SERIES1X", "B0SERIES3X"]


async def test_fetch_series_search_asins_skips_series_entries_without_an_asin():
    get = AsyncMock(return_value=_search_answer(
        [{"relationship_type": "series"}, {"relationship_type": "series", "asin": ""}, _series_rel("B0SERIES1X")],
    ))
    assert await fetch_series_search_asins(get, "x", "us") == ["B0SERIES1X"]


@pytest.mark.parametrize("answer", [
    {},
    {"products": []},
    {"products": [{}]},
    {"products": [{"relationships": []}]},
    {"products": [{"relationships": [{"relationship_type": "episode", "asin": "B0EPISODE1"}]}]},
])
async def test_fetch_series_search_asins_returns_an_empty_list_for_no_series_and_never_raises_not_found(answer):
    assert await fetch_series_search_asins(AsyncMock(return_value=answer), "x", "us") == []


async def test_fetch_series_search_asins_lets_a_failure_from_get_through():
    get = AsyncMock(side_effect=AudibleAPIException("down"))
    with pytest.raises(AudibleAPIException):
        await fetch_series_search_asins(get, "x", "us")


async def test_fetch_series_search_asins_never_logs_or_echoes_the_name(caplog):
    answers = [
        AsyncMock(return_value=_search_answer([_series_rel("B0SERIES1X")])),
        AsyncMock(return_value={"products": []}),
    ]
    with caplog.at_level(logging.DEBUG):
        for get in answers:
            await fetch_series_search_asins(get, TYPED_NAME, "us")
        with pytest.raises(RegionException) as raised:
            await fetch_series_search_asins(AsyncMock(), TYPED_NAME, "xx")
    rendered = " ".join(str(r.__dict__) for r in caplog.records)
    assert TYPED_NAME not in rendered
    assert "Zorbleflux" not in rendered
    assert TYPED_NAME not in str(raised.value)


# ============================================================
# normalize_chapters: what Audible sends beyond the first-class fields
# ============================================================

def _sub(title, ms=100):
    return {"length_ms": ms, "start_offset_ms": 0, "start_offset_sec": 0, "title": title}


def _chapters_payload(**overrides):
    payload = {
        "content_metadata": {
            "chapter_info": {
                "brandIntroDurationMs": 2043,
                "brandOutroDurationMs": 5062,
                "is_accurate": True,
                "runtime_length_ms": 36000000,
                "runtime_length_sec": 36000,
                "chapters": [_sub("One", 1000)],
            }
        }
    }
    for key, value in overrides.items():
        payload[key] = value
    return payload


def test_normalize_chapters_omits_every_widened_key_when_audible_sent_none():
    out = normalize_chapters(_chapters_payload())
    assert list(out) == [
        "brandIntroDurationMs", "brandOutroDurationMs", "isAccurate",
        "runtimeLengthMs", "runtimeLengthSec", "chapters",
    ]
    assert list(out["chapters"][0]) == ["lengthMs", "startOffsetMs", "startOffsetSec", "title"]


def test_normalize_chapters_carries_content_reference_verbatim_when_sent():
    ref = {"acr": "CR!ABC", "content_format": "MPEG", "nested": {"k": [1, 2]}}
    payload = _chapters_payload()
    payload["content_metadata"]["content_reference"] = ref
    out = normalize_chapters(payload)
    assert out["contentReference"] == ref
    assert "contentUrl" not in out
    assert "audibleExtras" not in out, "a consumed key must not also ride in the extras"


def test_normalize_chapters_carries_content_url_verbatim_when_sent():
    url = {"offline_url": "https://example.com/a", "playback_url": None}
    payload = _chapters_payload()
    payload["content_metadata"]["content_url"] = url
    out = normalize_chapters(payload)
    assert out["contentUrl"] == url
    assert "contentReference" not in out
    assert "audibleExtras" not in out


def test_normalize_chapters_keeps_an_empty_content_reference_that_audible_sent():
    payload = _chapters_payload()
    payload["content_metadata"]["content_reference"] = {}
    assert normalize_chapters(payload)["contentReference"] == {}


def test_normalize_chapters_groups_leftovers_by_the_level_they_arrived_at():
    payload = _chapters_payload(request_id="r-1", status="ok")
    payload["content_metadata"]["extra_cm"] = {"a": 1}
    payload["content_metadata"]["chapter_info"]["extra_ci"] = [1, 2]
    out = normalize_chapters(payload)
    assert out["audibleExtras"] == {
        "response": {"request_id": "r-1", "status": "ok"},
        "contentMetadata": {"extra_cm": {"a": 1}},
        "chapterInfo": {"extra_ci": [1, 2]},
    }


def test_normalize_chapters_omits_a_level_with_nothing_to_carry():
    payload = _chapters_payload()
    payload["content_metadata"]["chapter_info"]["extra_ci"] = "x"
    assert normalize_chapters(payload)["audibleExtras"] == {"chapterInfo": {"extra_ci": "x"}}


def test_normalize_chapters_does_not_echo_response_groups_anywhere():
    payload = _chapters_payload(response_groups=["chapter_info", "content_reference"])
    out = normalize_chapters(payload)
    assert "audibleExtras" not in out
    assert "response_groups" not in str(out)
    # Dropped only at the response level; the same key one level down is data.
    payload["content_metadata"]["response_groups"] = ["kept"]
    assert normalize_chapters(payload)["audibleExtras"] == {"contentMetadata": {"response_groups": ["kept"]}}


def test_normalize_chapters_carries_unreproduced_chapter_keys_per_chapter():
    payload = _chapters_payload()
    payload["content_metadata"]["chapter_info"]["chapters"] = [
        {**_sub("One"), "mystery": {"a": 1}},
        _sub("Two"),
    ]
    out = normalize_chapters(payload)["chapters"]
    assert out[0]["audibleExtras"] == {"mystery": {"a": 1}}
    assert "audibleExtras" not in out[1]


def test_normalize_chapters_keeps_sub_chapters_two_levels_deep_with_their_extras():
    payload = _chapters_payload()
    deepest = {**_sub("Deepest", 7), "tag": "d"}
    payload["content_metadata"]["chapter_info"]["chapters"] = [
        {**_sub("Top", 30), "chapters": [{**_sub("Mid", 20), "chapters": [deepest, _sub("Deep2", 8)]}]},
    ]
    top = normalize_chapters(payload)["chapters"][0]
    mid = top["chapters"][0]
    assert (top["title"], mid["title"]) == ("Top", "Mid")
    assert mid["chapters"] == [
        {"lengthMs": 7, "startOffsetMs": 0, "startOffsetSec": 0, "title": "Deepest", "audibleExtras": {"tag": "d"}},
        {"lengthMs": 8, "startOffsetMs": 0, "startOffsetSec": 0, "title": "Deep2"},
    ]


def test_normalize_chapters_does_not_mutate_its_input():
    payload = _chapters_payload(request_id="r")
    payload["content_metadata"]["content_reference"] = {"a": 1}
    payload["content_metadata"]["chapter_info"]["chapters"][0]["chapters"] = [_sub("S")]
    before = copy.deepcopy(payload)
    normalize_chapters(payload)
    assert payload == before


# ============================================================
# normalize_series: audibleExtras and extrasWithheld
# ============================================================

def test_normalize_series_omits_both_keys_for_a_product_with_only_the_consumed_keys():
    out = normalize_series({"asin": "B0SERIES1X", "title": "T", "publisher_summary": "S"}, "us")
    assert list(out) == ["asin", "name", "description", "region", "position", "updatedAt"]


def test_normalize_series_carries_every_other_key_in_audible_extras_verbatim():
    product = {
        "asin": "B0SERIES1X", "title": "T", "publisher_summary": "S",
        "language": "english", "merchandising_summary": "<p>m</p>", "nested": {"k": [1]},
    }
    out = normalize_series(product, "us")
    assert out["audibleExtras"] == {"language": "english", "merchandising_summary": "<p>m</p>", "nested": {"k": [1]}}
    assert "extrasWithheld" not in out
    assert out["name"] == "T" and out["description"] == "S"
    for consumed in ("asin", "title", "publisher_summary"):
        assert consumed not in out["audibleExtras"]


def test_normalize_series_records_what_was_withheld():
    product = {
        "asin": "B0SERIES1X", "title": "T",
        "relationships": [
            {"relationship_type": "series", "asin": "B0SERIES1X"},
            {"relationship_type": "episode", "asin": "B0EPISODE1"},
        ],
    }
    out = normalize_series(product, "us")
    assert out["extrasWithheld"] == {"relationships": {"episode": 1}}
    assert out["audibleExtras"]["relationships"] == [{"relationship_type": "series", "asin": "B0SERIES1X"}]


def test_normalize_series_reports_a_dropped_blob_as_none_with_the_reason():
    deep = cur = {}
    for _ in range(40):
        cur["d"] = {}
        cur = cur["d"]
    out = normalize_series({"asin": "B0SERIES1X", "title": "T", "deep": deep}, "us")
    assert out["audibleExtras"] is None
    assert "audibleExtras" in out["extrasWithheld"]
