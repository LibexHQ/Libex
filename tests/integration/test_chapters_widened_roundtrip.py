"""
A chapter listing carrying what Audible really sends -- NUL characters,
non-finite numbers, groups beyond the first-class fields -- through the real
hosted write into Postgres and back out through the /db chapters reader.

Before the chapter data was bounded, a NUL anywhere in a chapter's own keys
or values made the jsonb write fail; the service writer swallows its own
failures and logs them, so the listing was simply never stored and nothing
told the caller. The control below proves this harness can see that failure,
so the passing case means the write really happened rather than that the
failure went unlooked-for.

The second half pins that riding extrasWithheld inside the payload leaves
the shrinkage rule exactly as it was: the payload is still replaced or kept
whole, judged only on its chapters list.
"""

# Standard library
import logging

# Third party
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import insert, select

# Local
from app.db.models import Book, Track
from app.main import app
from app.services.db.reader import get_track_from_db
from app.services.db.writer import upsert_track
from libex_core.audible.chapters import normalize_chapters

ASIN = "B0CHAPTERS"
WRITE_FAILED = "DB write failed for track"
KEPT = "Kept stored chapters over an empty response"


def _ch(title, index=0, **extra):
    return {
        "length_ms": 60_000, "start_offset_ms": index * 60_000,
        "start_offset_sec": index * 60, "title": title, **extra,
    }


def _hostile():
    return {
        "request_id": "r\x00-1",
        "content_metadata": {
            "content_reference": {"ac\x00r": "CR\x00!", "ratio": float("nan")},
            "content_url": {"offline_url": "https://example.com/a", "n": float("inf")},
            "chapter_info": {
                "runtime_length_ms": 120_000,
                "is_accurate": True,
                "chapters": [
                    _ch("Chap\x00ter 1", 0, odd=1 << 20000, nested=[{"k\x00": "v"}]),
                    _ch("Chapter 2", 1, chapters=[_ch("Sub\x00 A", 1)]),
                ],
            },
        },
    }


def _listing(count):
    data = {"content_metadata": {
        "chapter_info": {
            "runtime_length_ms": 3_600_000,
            "chapters": [_ch(f"Chapter {i + 1}", i) for i in range(count)],
        },
    }}
    return normalize_chapters(data, ASIN, "us")


async def _book(session):
    await session.execute(insert(Book).values(asin=ASIN, title="Chaptered", region="us"))
    await session.commit()


async def _stored(session):
    session.expire_all()
    return (await session.execute(select(Track.chapters).where(Track.asin == ASIN))).scalar_one_or_none()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_control_a_raw_nul_payload_does_fail_the_write(db_session, caplog):
    await _book(db_session)
    with caplog.at_level(logging.WARNING, logger="libex"):
        await upsert_track(db_session, ASIN, {"chapters": [{"title": "a\x00b"}]}, region="us")
    assert [r for r in caplog.records if WRITE_FAILED in r.getMessage()]
    assert await _stored(db_session) is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_nul_and_nan_bearing_listing_is_stored_and_served(db_session, caplog):
    normalized = normalize_chapters(_hostile(), ASIN, "us")
    assert normalized["extrasWithheld"]["sanitized"]["nulCharacters"] >= 6

    await _book(db_session)
    with caplog.at_level(logging.WARNING, logger="libex"):
        await upsert_track(db_session, ASIN, normalized, region="us")
    assert [r for r in caplog.records if WRITE_FAILED in r.getMessage()] == []

    db_session.expire_all()
    assert await get_track_from_db(db_session, ASIN) == normalized

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/db/book/{ASIN}/chapters")
    assert response.status_code == 200
    body = response.json()
    assert body["chapters"][0]["title"] == "Chapter 1"
    assert body["chapters"][1]["chapters"][0]["title"] == "Sub A"
    assert body["contentReference"] == {"acr": "CR!", "ratio": None}
    assert body["contentUrl"]["n"] is None
    assert body["chapters"][0]["audibleExtras"]["odd"] is None
    assert body["extrasWithheld"] == normalized["extrasWithheld"]


# ============================================================
# extrasWithheld inside the payload does not change the shrinkage rule
# ============================================================

def _withheld_empty():
    """A chapterless payload that also carries an extrasWithheld."""
    out = normalize_chapters(
        {"content_metadata": {"content_reference": {"blob": "x" * (65 * 1024)},
                              "chapter_info": {"brandIntroDurationMs": 2000}}},
        ASIN, "us",
    )
    assert out["chapters"] == [] and out["extrasWithheld"] == {"contentReference": "size"}
    return out


@pytest.mark.integration
@pytest.mark.asyncio
async def test_an_empty_response_carrying_extras_withheld_cannot_erase_a_stored_listing(db_session, caplog):
    await _book(db_session)
    stored = _listing(5)
    await upsert_track(db_session, ASIN, stored, region="us")
    with caplog.at_level(logging.WARNING, logger="libex"):
        await upsert_track(db_session, ASIN, _withheld_empty(), region="us")
    held = await _stored(db_session)
    assert held == stored
    assert "extrasWithheld" not in held, "the refused payload's record must not be spliced in"
    assert [r for r in caplog.records if KEPT in r.getMessage()]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_stored_payload_carrying_extras_withheld_is_replaced_whole_by_a_listing(db_session):
    await _book(db_session)
    await upsert_track(db_session, ASIN, _withheld_empty(), region="us")
    assert (await _stored(db_session))["extrasWithheld"] == {"contentReference": "size"}
    listing = _listing(3)
    await upsert_track(db_session, ASIN, listing, region="us")
    held = await _stored(db_session)
    assert held == listing and "extrasWithheld" not in held


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_shorter_listing_with_extras_withheld_still_replaces_a_longer_stored_one(db_session):
    await _book(db_session)
    await upsert_track(db_session, ASIN, _listing(5), region="us")
    shorter = normalize_chapters(
        {"content_metadata": {"content_url": {"b": "x" * (65 * 1024)},
                              "chapter_info": {"chapters": [_ch("Only", 0)]}}},
        ASIN, "us",
    )
    assert shorter["extrasWithheld"] == {"contentUrl": "size"}
    await upsert_track(db_session, ASIN, shorter, region="us")
    held = await _stored(db_session)
    assert len(held["chapters"]) == 1 and held["extrasWithheld"] == {"contentUrl": "size"}
