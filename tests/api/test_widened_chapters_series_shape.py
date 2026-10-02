"""
Route-level pins for the widened chapter and series bodies.

The service runs for real behind the route with only Audible's `get` and the
persistence/cache edges replaced, so the body asserted on is what a caller
receives. The pre-existing keys are compared against literals captured from
the bodies these same fixtures produced before the widening; the new keys are
asserted separately, so an additive change cannot be mistaken for a changed
one.
"""

# Standard library
import json
from unittest.mock import AsyncMock, MagicMock, patch

# Third party
import pytest
from httpx import AsyncClient, ASGITransport

# Local
from app.db.session import get_session
from app.main import app

CHAPTER_ASIN = "B0CHAPTR01"
SERIES_ASIN = "B0SERIES1X"

CHAPTERS_RAW = {
    "content_metadata": {
        "chapter_info": {
            "brandIntroDurationMs": 2043,
            "brandOutroDurationMs": 5062,
            "is_accurate": True,
            "runtime_length_ms": 36000000,
            "runtime_length_sec": 36000,
            "chapters": [
                {"length_ms": 1000, "start_offset_ms": 0, "start_offset_sec": 0, "title": "Opening Credits"},
                {"length_ms": 2500, "start_offset_ms": 1000, "start_offset_sec": 1, "title": "Chapter 1"},
            ],
        }
    }
}

# Bytes the body had before the widening, for CHAPTERS_RAW.
CHAPTERS_BEFORE = {
    "brandIntroDurationMs": 2043,
    "brandOutroDurationMs": 5062,
    "isAccurate": True,
    "runtimeLengthMs": 36000000,
    "runtimeLengthSec": 36000,
    "chapters": [
        {"lengthMs": 1000, "startOffsetMs": 0, "startOffsetSec": 0, "title": "Opening Credits"},
        {"lengthMs": 2500, "startOffsetMs": 1000, "startOffsetSec": 1, "title": "Chapter 1"},
    ],
}

SERIES_RAW = {
    "response_groups": ["product_attrs", "product_desc"],
    "product": {"asin": SERIES_ASIN, "title": "A Series", "publisher_summary": "<p>Sum &amp; more.</p>"},
}
SERIES_BEFORE = {
    "asin": SERIES_ASIN,
    "name": "A Series",
    "description": "Sum &amp; more.",
    "region": "us",
    "position": None,
    "updatedAt": None,
}

NEW_CHAPTER_KEYS = ("contentReference", "contentUrl", "audibleExtras", "extrasWithheld")
NEW_SERIES_KEYS = ("audibleExtras", "extrasWithheld")


@pytest.fixture
async def client():
    app.dependency_overrides[get_session] = lambda: MagicMock()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_session, None)


async def _get_chapters(client, raw):
    with (
        patch("app.services.audible.books.audible_get", AsyncMock(return_value=raw)),
        patch("app.services.audible.books.persist_track_background"),
    ):
        return await client.get(f"/book/{CHAPTER_ASIN}/chapters")


async def _get_series(client, raw):
    with (
        patch("app.services.audible.series.audible_get", AsyncMock(return_value=raw)),
        patch("app.services.audible.series.persist_series_background"),
        patch("app.services.audible.series.cache.get", AsyncMock(return_value=None)),
    ):
        return await client.get(f"/series/{SERIES_ASIN}")


def _without(body, keys):
    return {k: v for k, v in body.items() if k not in keys}


def _strip_chapter_extras(body):
    out = _without(body, NEW_CHAPTER_KEYS)
    out["chapters"] = [_without(c, ("chapters", "audibleExtras")) for c in body["chapters"]]
    return out


# ============================================================
# /book/{asin}/chapters
# ============================================================

async def test_chapters_body_keeps_every_pre_existing_key_and_value_and_their_order(client):
    response = await _get_chapters(client, CHAPTERS_RAW)
    assert response.status_code == 200
    body = response.json()
    assert _strip_chapter_extras(body) == CHAPTERS_BEFORE
    assert list(body)[:6] == list(CHAPTERS_BEFORE)
    for c in body["chapters"]:
        assert list(c)[:4] == ["lengthMs", "startOffsetMs", "startOffsetSec", "title"]


async def test_chapters_body_carries_the_new_keys_as_null_when_audible_sent_none(client):
    body = (await _get_chapters(client, CHAPTERS_RAW)).json()
    for key in NEW_CHAPTER_KEYS:
        assert key in body and body[key] is None
    assert body["extrasWithheld"] is None
    for c in body["chapters"]:
        assert c["chapters"] is None and c["audibleExtras"] is None


async def test_chapters_body_surfaces_what_audible_sent(client):
    raw = json.loads(json.dumps(CHAPTERS_RAW))
    cm = raw["content_metadata"]
    cm["content_reference"] = {"acr": "CR!ABC"}
    cm["content_url"] = {"offline_url": "https://example.com/a"}
    cm["extra_cm"] = 1
    cm["chapter_info"]["extra_ci"] = 2
    cm["chapter_info"]["chapters"][0]["chapters"] = [
        {"length_ms": 400, "start_offset_ms": 0, "start_offset_sec": 0, "title": "Sub A", "tag": "t"},
    ]
    raw["request_id"] = "r-1"
    raw["response_groups"] = ["chapter_info"]
    body = (await _get_chapters(client, raw)).json()
    assert body["contentReference"] == {"acr": "CR!ABC"}
    assert body["contentUrl"] == {"offline_url": "https://example.com/a"}
    assert body["audibleExtras"] == {
        "response": {"request_id": "r-1"},
        "contentMetadata": {"extra_cm": 1},
        "chapterInfo": {"extra_ci": 2},
    }
    sub = body["chapters"][0]["chapters"][0]
    assert sub["title"] == "Sub A" and sub["audibleExtras"] == {"tag": "t"}
    assert sub["chapters"] is None
    assert _without(body, NEW_CHAPTER_KEYS)["isAccurate"] is True


async def test_chapters_body_with_a_nul_payload_is_200_cleaned_and_recorded(client):
    raw = json.loads(json.dumps(CHAPTERS_RAW))
    raw["content_metadata"]["chapter_info"]["chapters"][0]["title"] = "Open\x00ing"
    raw["content_metadata"]["content_reference"] = {"ac\x00r": "CR\x00!"}
    persisted = MagicMock()
    with (
        patch("app.services.audible.books.audible_get", AsyncMock(return_value=raw)),
        patch("app.services.audible.books.persist_track_background", persisted),
    ):
        response = await client.get(f"/book/{CHAPTER_ASIN}/chapters")
    assert response.status_code == 200
    body = response.json()
    assert body["chapters"][0]["title"] == "Opening"
    assert body["contentReference"] == {"acr": "CR!"}
    assert body["extrasWithheld"] == {"sanitized": {"nulCharacters": 3}}
    stored_payload = persisted.call_args.args[1]
    assert "\\u0000" not in json.dumps(stored_payload)


# ============================================================
# /series/{asin}
# ============================================================

async def test_series_body_keeps_every_pre_existing_key_and_value_and_their_order(client):
    response = await _get_series(client, SERIES_RAW)
    assert response.status_code == 200
    body = response.json()
    assert _without(body, NEW_SERIES_KEYS) == SERIES_BEFORE
    assert list(body)[:6] == list(SERIES_BEFORE)


async def test_series_body_carries_the_new_keys_as_null_when_audible_sent_nothing_extra(client):
    body = (await _get_series(client, SERIES_RAW)).json()
    assert body["audibleExtras"] is None and body["extrasWithheld"] is None


async def test_series_body_surfaces_audible_extras_and_withheld(client):
    raw = json.loads(json.dumps(SERIES_RAW))
    raw["product"]["language"] = "english"
    raw["product"]["relationships"] = [
        {"relationship_type": "series", "asin": SERIES_ASIN},
        {"relationship_type": "episode", "asin": "B0EPISODE1"},
    ]
    body = (await _get_series(client, raw)).json()
    assert _without(body, NEW_SERIES_KEYS) == SERIES_BEFORE
    assert body["audibleExtras"]["language"] == "english"
    assert body["audibleExtras"]["relationships"] == [{"relationship_type": "series", "asin": SERIES_ASIN}]
    assert body["extrasWithheld"] == {"relationships": {"episode": 1}}


# ============================================================
# /series/search
# ============================================================

def _search_get(requests):
    async def fake(region, path, params):
        requests.append((region, path, dict(params)))
        if path == "/1.0/catalog/products":
            return {"products": [
                {"relationships": [
                    {"relationship_type": "series", "asin": "B0SERIES1X"},
                    {"relationship_type": "series", "asin": "B0SERIES2X"},
                    {"relationship_type": "series", "asin": "B0SERIES1X"},
                    {"relationship_type": "episode", "asin": "B0EPISODE1"},
                ]},
                {"relationships": [{"relationship_type": "series", "asin": "B0SERIES3X"}]},
            ]}
        asin = path.rsplit("/", 1)[1]
        return {
            "response_groups": ["product_attrs", "product_desc"],
            "product": {"asin": asin, "title": f"Series {asin}", "publisher_summary": f"<p>S {asin}</p>"},
        }
    return fake


def _search_before(asin):
    return {
        "asin": asin, "name": f"Series {asin}", "description": f"S {asin}",
        "region": "uk", "position": None, "updatedAt": None,
    }


async def test_series_search_sends_the_same_request_and_returns_the_same_series_in_the_same_order(client):
    requests = []
    with (
        patch("app.services.audible.series.audible_get", AsyncMock(side_effect=_search_get(requests))),
        patch("app.services.audible.series.persist_series_background"),
        patch("app.services.audible.series.cache.get", AsyncMock(return_value=None)),
        patch("app.services.audible.series.search_series_from_db", AsyncMock(return_value=[])),
    ):
        response = await client.get("/series/search?name=dune&region=uk")
    assert response.status_code == 200
    assert requests[0] == (
        "uk", "/1.0/catalog/products",
        {"title": "dune", "response_groups": "relationships", "num_results": 10},
    )
    assert [r[1] for r in requests[1:]] == [f"/1.0/catalog/products/B0SERIES{n}X" for n in (1, 2, 3)]
    body = response.json()
    assert [_without(s, NEW_SERIES_KEYS) for s in body] == [
        _search_before(a) for a in ("B0SERIES1X", "B0SERIES2X", "B0SERIES3X")
    ]
    assert all(s["audibleExtras"] is None and s["extrasWithheld"] is None for s in body)


async def test_series_search_with_no_series_behind_the_hits_is_still_a_404(client):
    with (
        patch("app.services.audible.series.audible_get", AsyncMock(return_value={"products": []})),
        patch("app.services.audible.series.search_series_from_db", AsyncMock(return_value=[])),
    ):
        response = await client.get("/series/search?name=nothing")
    assert response.status_code == 404
    assert response.json()["error"] == "No series found for: nothing"


# ============================================================
# /series/{asin} when Audible is down: the store's extras are served
# ============================================================

async def test_series_outage_fallback_body_carries_the_stored_extras_and_withheld(client):
    stored = {
        **SERIES_BEFORE,
        "audibleExtras": {"language": "english"},
        "extrasWithheld": {"relationships": {"episode": 1}},
    }
    with (
        patch("app.services.audible.series.audible_get", AsyncMock(side_effect=Exception("down"))),
        patch("app.services.audible.series.get_series_from_db", AsyncMock(return_value=stored)),
        patch("app.services.audible.series.cache.get", AsyncMock(return_value=None)),
    ):
        response = await client.get(f"/series/{SERIES_ASIN}")
    assert response.status_code == 200
    body = response.json()
    assert body["audibleExtras"] == {"language": "english"}
    assert body["extrasWithheld"] == {"relationships": {"episode": 1}}
    assert _without(body, NEW_SERIES_KEYS) == SERIES_BEFORE
