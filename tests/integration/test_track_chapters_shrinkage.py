"""
Integration tests for the shrinkage guard on tracks.chapters, against real
Postgres, through both writers that touch the column.

Libex never accepts less data than it holds. On this column that rule had no
expression at all until now: both writers overwrote the payload
unconditionally. What makes it reachable is the fall-through in the chapter
paths, which tests Audible's chapter_info for truthiness rather than for
containing chapters — so a chapter_info of {"brandIntroDurationMs": 2000} is
not treated as "no chapters", reaches the normalizer, and comes out as a
perfectly well-formed payload whose chapters list is empty. Written straight
through, that erases a stored listing and nothing anywhere records that it
happened.

The empty payloads below are built by the real _normalize_chapters from
exactly that chapter_info, rather than hand-written, so these test the shape
production actually produces.

Two of the cases are deliberate design and are here to stop a later
"improvement":

  - a SHORTER non-empty listing is accepted. A reissue can genuinely re-cut a
    title into fewer, longer chapters, and a count floor would pin the first
    listing ever stored and refuse every correction after it.
  - a payload whose chapters is null, an object or a string is refused
    WITHOUT erroring. What makes that true is that the length function is
    handed a CASE whose arms are both arrays, so it never sees the malformed
    value at all. Its position inside that call is pinned structurally in
    tests/test_db_writer.py rather than here, and has to be: the equivalent
    AND short-circuits safely in both positions this code uses today, so
    every test in this file passes against it. Nothing behavioural can tell
    the two apart — see that test for the caller that would break.

Both writers are run over the same table because the rule is one rule; the
backfill imports the expression rather than restating it, and these prove the
two sites actually behave alike rather than merely sharing a helper name.
"""

# Standard library
import asyncio
import logging

# Third party
import pytest
from sqlalchemy import insert, select

# Local
import app.db.session as db_session_module
from app.db.models import Book, Track
from app.services.audible.books import _normalize_chapters
from app.services.db.writer import upsert_track
from scripts.backfill_chapters import _store_chapters

ASIN = "B0CHAPTERS"

WRITERS = [upsert_track, _store_chapters]
WRITER_IDS = ["service", "backfill"]

SUPPRESSED = "Kept stored chapters over an empty response"
WRITE_FAILED = "DB write failed for track"


def _listing(count, runtime_ms=3_600_000):
    """A payload that really lists chapters, in the normalizer's own shape."""
    return _normalize_chapters(
        {
            "content_metadata": {
                "chapter_info": {
                    "runtime_length_ms": runtime_ms,
                    "is_accurate": True,
                    "chapters": [
                        {
                            "length_ms": 60_000,
                            "start_offset_ms": index * 60_000,
                            "start_offset_sec": index * 60,
                            "title": f"Chapter {index + 1}",
                        }
                        for index in range(count)
                    ],
                }
            }
        },
        ASIN,
    )


def _chapterless():
    """The payload the reported failure actually produces: Audible answers
    with a chapter_info carrying only a brand-intro duration, that passes the
    truthiness fall-through, and the normalizer turns it into this."""
    return _normalize_chapters(
        {"content_metadata": {"chapter_info": {"brandIntroDurationMs": 2000}}}, ASIN
    )


# Payloads that are not a chapter listing and are not shaped like one either.
# Each has erased a listing on any writer that hands its chapters value
# straight to jsonb_array_length, or taken the statement down trying.
MALFORMED = [
    pytest.param({**_chapterless(), "chapters": None}, id="chapters-is-null"),
    pytest.param({**_chapterless(), "chapters": {"0": "Chapter 1"}}, id="chapters-is-an-object"),
    pytest.param({**_chapterless(), "chapters": "21 chapters"}, id="chapters-is-a-string"),
    pytest.param({k: v for k, v in _chapterless().items() if k != "chapters"}, id="no-chapters-key"),
]


async def _book(session):
    """tracks.asin is a foreign key, so the book has to exist first."""
    await session.execute(
        insert(Book).values(asin=ASIN, title="Chaptered", region="us")
    )
    await session.commit()


async def _stored(session):
    result = await session.execute(select(Track.chapters).where(Track.asin == ASIN))
    return result.scalar_one_or_none()


async def _stored_at(session):
    result = await session.execute(select(Track.updated_at).where(Track.asin == ASIN))
    return result.scalar_one_or_none()


def _count(payload):
    chapters = payload.get("chapters") if payload else None
    return len(chapters) if isinstance(chapters, list) else None


# (stored payload, incoming payload, chapters held afterwards)
TRUTH_TABLE = [
    pytest.param(_listing(21), _chapterless(), 21, id="empty-response-cannot-erase-a-listing"),
    pytest.param(_chapterless(), _listing(21), 21, id="a-listing-lands-over-an-empty-row"),
    pytest.param(_listing(21), _listing(3), 3, id="a-shorter-recut-is-accepted"),
    pytest.param(_listing(3), _listing(21), 21, id="a-longer-listing-is-accepted"),
    pytest.param(_chapterless(), _chapterless(), 0, id="two-empty-payloads-leave-it-empty"),
    pytest.param(_listing(21), _listing(21), 21, id="the-same-listing-again-is-harmless"),
]


# ============================================================
# The rule, on both writers
# ============================================================

@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("write", WRITERS, ids=WRITER_IDS)
@pytest.mark.parametrize("stored, incoming, expected", TRUTH_TABLE)
async def test_the_richer_chapter_payload_survives(db_session, write, stored, incoming, expected):
    """Written in the sequence production writes them: one call to establish
    the stored payload, a second to try to change it."""
    await _book(db_session)
    await write(db_session, ASIN, stored)
    await write(db_session, ASIN, incoming)

    assert _count(await _stored(db_session)) == expected


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("write", WRITERS, ids=WRITER_IDS)
@pytest.mark.parametrize("incoming", MALFORMED)
async def test_a_malformed_chapters_value_is_refused_without_erroring(
    db_session, write, incoming, caplog
):
    """Refused AND survivable. Proved by three things together, because any
    one of them alone passes against a statement that blew up: the listing is
    still there, the row's updated_at moved (so the statement really ran and
    committed rather than being rolled back), and the service writer — which
    swallows its own failures — logged no write failure. Without the second
    and third, a guard that errors on every one of these would look identical
    to one that refuses them."""
    await _book(db_session)
    await write(db_session, ASIN, _listing(21))
    before = await _stored_at(db_session)

    with caplog.at_level(logging.WARNING, logger="libex"):
        await write(db_session, ASIN, incoming)

    assert _count(await _stored(db_session)) == 21
    assert await _stored_at(db_session) > before
    assert [r for r in caplog.records if WRITE_FAILED in r.getMessage()] == []


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("write", WRITERS, ids=WRITER_IDS)
async def test_the_whole_payload_is_kept_together_when_a_write_is_refused(db_session, write):
    """The retained value is the stored payload entire, not its list spliced
    into the incoming one. A chapterless response also carries runtimeLengthMs
    0 and its own brand-intro duration, and writing those beside a retained
    listing would leave the row disagreeing with itself about a book nothing
    ever re-measured."""
    await _book(db_session)
    await write(db_session, ASIN, _listing(21, runtime_ms=4_200_000))
    await write(db_session, ASIN, _chapterless())

    held = await _stored(db_session)
    assert _count(held) == 21
    assert held["runtimeLengthMs"] == 4_200_000
    assert held["isAccurate"] is True
    assert held["brandIntroDurationMs"] == 0


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("write", WRITERS, ids=WRITER_IDS)
async def test_an_empty_payload_still_lands_on_a_row_that_has_nothing(db_session, write):
    """The guard stops a loss; it does not refuse a first answer. A book whose
    chapters Audible genuinely has none of still gets its row, so the fetch is
    recorded rather than retried forever."""
    await _book(db_session)
    await write(db_session, ASIN, _chapterless())

    held = await _stored(db_session)
    assert held is not None
    assert held["chapters"] == []
    assert held["brandIntroDurationMs"] == 2000


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("write", WRITERS, ids=WRITER_IDS)
async def test_an_empty_payload_still_refreshes_another_empty_one(db_session, write):
    """Neither side lists chapters, so there is nothing to lose and the newer
    answer lands whole — the fallback is the incoming payload, not the stored
    one. Frozen the other way, a row once written empty would hold its first
    runtime and brand durations against every later response, and the guard
    meant to stop a loss would be causing one."""
    await _book(db_session)
    await write(db_session, ASIN, _chapterless())
    await write(
        db_session,
        ASIN,
        {**_chapterless(), "brandIntroDurationMs": 5000, "runtimeLengthMs": 99},
    )

    held = await _stored(db_session)
    assert held["chapters"] == []
    assert held["brandIntroDurationMs"] == 5000
    assert held["runtimeLengthMs"] == 99


# ============================================================
# The suppression is visible
# ============================================================

@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("write", WRITERS, ids=WRITER_IDS)
async def test_a_refused_overwrite_says_so_with_the_asin_and_the_count(db_session, write, caplog):
    """A write that silently declines is no easier to diagnose than the silent
    overwrite it replaced, and nothing watches this path.

    The message is asserted verbatim, and deliberately carries no "Backfill:"
    prefix even from the script — one string across both writers is what lets
    a search find every suppressed overwrite rather than half of them."""
    await _book(db_session)
    await write(db_session, ASIN, _listing(21))

    with caplog.at_level(logging.WARNING, logger="libex"):
        await write(db_session, ASIN, _chapterless())

    suppressed = [r for r in caplog.records if r.getMessage() == SUPPRESSED]
    assert len(suppressed) == 1
    assert suppressed[0].asin == ASIN
    assert suppressed[0].stored_chapters == 21
    assert suppressed[0].levelno == logging.WARNING


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("write", WRITERS, ids=WRITER_IDS)
async def test_an_accepted_write_reports_no_suppression(db_session, write, caplog):
    """The line is gated on a refusal actually having happened, so an ordinary
    write does not cry wolf and the count stays worth reading."""
    await _book(db_session)
    await write(db_session, ASIN, _chapterless())

    with caplog.at_level(logging.WARNING, logger="libex"):
        await write(db_session, ASIN, _listing(21))

    assert [r for r in caplog.records if r.getMessage() == SUPPRESSED] == []


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("write", WRITERS, ids=WRITER_IDS)
async def test_a_shorter_listing_is_accepted_quietly(db_session, write, caplog):
    """The re-cut case is not a suppression and must not report as one — it is
    an accepted correction, and logging it as a refusal would train whoever
    reads these to ignore the real ones."""
    await _book(db_session)
    await write(db_session, ASIN, _listing(21))

    with caplog.at_level(logging.WARNING, logger="libex"):
        await write(db_session, ASIN, _listing(3))

    assert _count(await _stored(db_session)) == 3
    assert [r for r in caplog.records if r.getMessage() == SUPPRESSED] == []


# ============================================================
# Concurrency — the reason the rule is in the SET clause
# ============================================================

@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_race_of_mixed_payloads_ends_with_the_listing(db_session):
    """The merge is decided inside the statement, against the row as Postgres
    has it locked, rather than by reading first and writing back.

    Several paths can refresh one ASIN at once — the on-demand fetch, the
    seeder's chapter pass, the backfill walk — and a read-compare-write would
    let two of them agree the stored row was empty before either had written,
    at which point the last writer wins and the listing is gone. Here the
    empty payloads outnumber the listings and are written last, so a
    last-write-wins implementation ends empty essentially every run."""
    await _book(db_session)

    factory = db_session_module.AsyncSessionFactory
    payloads = [_listing(21), _chapterless(), _chapterless(), _listing(21)] + [
        _chapterless() for _ in range(8)
    ]

    async def _write(payload):
        async with factory() as session:
            await upsert_track(session, ASIN, payload)

    await asyncio.gather(*[_write(payload) for payload in payloads])

    assert _count(await _stored(db_session)) == 21
