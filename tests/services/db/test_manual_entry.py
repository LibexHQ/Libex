"""
The one writer for a hand-entered book, on SQLite: one transaction that stores
the book, its links and its chapters or nothing at all, and that never changes
an entity another book already supplied. Postgres (locks, dry run, the real
chain) is covered in tests/integration/test_manual_entry_endpoint.py.
"""

# Third party
import pytest
import pytest_asyncio
from sqlalchemy import select, text
from unittest.mock import AsyncMock, patch

# Local
from app.api.routes.internal.router import _writer_dict
from app.core.ingest_models import IngestBody
from app.services.db import manual_entry
from app.services.db.manual_entry import (
    ENTITY,
    STORED,
    WRITTEN,
    ManualEntryReadbackError,
    enter_book,
)
from libex_core.storage.models import Author, Book, Series
from libex_core.storage.store import LocalStore
from libex_core.storage.write import write_books
from tests.fixtures.manual_entry import (
    ASIN,
    FULL_BOOK,
    FULL_CHAPTERS,
    assert_full_round_trip,
    AUTHOR_ASIN,
    GENRE_ASIN,
    SERIES_ASIN,
    chapters_dict,
    counts,
    snapshot,
    stable,
    writer_dict,
)

pytest.importorskip("aiosqlite")


@pytest_asyncio.fixture
async def store():
    s = LocalStore("sqlite+aiosqlite://")
    await s.upgrade()
    await s.open()
    yield s
    await s.close()


@pytest_asyncio.fixture
async def session(store):
    async with store.session() as s:
        yield s


async def _enter(session, book=None, chapters="default", *, dry_run=False, region="us"):
    chapters = chapters_dict() if chapters == "default" else chapters
    return await enter_book(session, book or writer_dict(), chapters, region=region, dry_run=dry_run)


async def _live_book(session, **author):
    """Another book that already carries the shared author, series, narrator and genre."""
    await write_books(session, [{
        "asin": "B0OTHER001", "region": "us", "title": "Other", "language": "english",
        "authors": [{"name": "First Author", "asin": AUTHOR_ASIN, "region": "us",
                     "description": "stored bio", "image": "https://img.example/stored.jpg", **author}],
        "narrators": [{"name": "Amy Voice"}],
        "series": [{"asin": SERIES_ASIN, "name": "The Series", "position": "1",
                    "description": "stored about"}],
        "genres": [{"asin": GENRE_ASIN, "name": "Fantasy", "type": "Genres"}],
    }], confirm=True)
    await session.commit()


# ============================================================
# A COMMIT STORES EVERYTHING
# ============================================================


async def test_a_commit_fills_the_book_every_pivot_and_the_track(session):
    result = await _enter(session)
    assert result.status == WRITTEN and result.conflicts == []

    book = (await session.execute(select(Book).filter_by(asin=ASIN, region="us"))).scalar_one()
    assert book.manual_entry_at is not None
    assert book.confirmed_at is None
    assert book.chapters_checked_at is not None
    assert book.chapters_confirmed_at is None
    assert (book.is_listenable, book.is_buyable, book.is_vvab) == (False, False, False)
    assert book.description == "typed in by hand"

    served = result.book
    assert [a["name"] for a in served["authors"]] == ["First Author", "Second Author"]
    assert sorted(n["name"] for n in served["narrators"]) == ["Amy Voice", "Zed Voice"]
    assert [s["asin"] for s in served["series"]] == [SERIES_ASIN]
    assert [g["asin"] for g in served["genres"]] == [GENRE_ASIN]
    assert served["manualEntry"] is True
    assert [c["title"] for c in result.chapters["chapters"]] == ["One", "Two"]
    assert (await counts(session))["tracks"] == 1


async def test_a_book_without_chapters_stores_no_track_and_no_checked_stamp(session):
    result = await _enter(session, chapters=None)
    assert result.status == WRITTEN and result.chapters is None
    assert (await counts(session))["tracks"] == 0
    book = (await session.execute(select(Book).filter_by(asin=ASIN))).scalar_one()
    assert book.chapters_checked_at is None


async def test_the_entities_report_says_new_for_everything_on_a_first_entry(session):
    result = await _enter(session)
    assert {k: {e["status"] for e in v} for k, v in result.entities.items()} == {
        "authors": {"new"}, "narrators": {"new"}, "series": {"new"}, "genres": {"new"},
    }
    assert [e["name"] for e in result.entities["authors"]] == ["First Author", "Second Author"]


# ============================================================
# A DRY RUN LEAVES NOTHING
# ============================================================


async def test_a_dry_run_returns_what_a_commit_would_store_and_writes_nothing(session):
    before = await snapshot(session)
    dry = await _enter(session, dry_run=True)
    assert dry.status == WRITTEN
    assert dry.book["title"] == "Delisted" and dry.chapters is not None
    assert await snapshot(session) == before

    real = await _enter(session)
    assert stable(real.book) == stable(dry.book)
    assert real.chapters == dry.chapters
    assert real.entities == dry.entities
    assert await snapshot(session) != before


# ============================================================
# ALREADY STORED
# ============================================================


async def test_an_asin_already_stored_for_the_region_is_refused_and_untouched(session):
    await _enter(session)
    before = await snapshot(session)
    again = await _enter(session, writer_dict(title="Changed", description="other"))
    assert again.status == STORED
    assert again.conflicts == [{"loc": ["book", "asin"], "reason": "stored"}]
    assert again.book is None
    assert await snapshot(session) == before


async def test_a_live_book_blocks_a_manual_entry_for_the_same_region(session):
    await write_books(session, [{"asin": ASIN, "region": "us", "title": "Live"}], confirm=True)
    await session.commit()
    before = await snapshot(session)
    assert (await _enter(session)).status == STORED
    assert await snapshot(session) == before


async def test_the_same_asin_in_another_region_is_a_separate_record(session):
    await write_books(session, [{"asin": ASIN, "region": "us", "title": "Live"}], confirm=True)
    await session.commit()
    uk = writer_dict(region="uk")
    uk["authors"] = [{"name": "UK Author"}]
    uk["series"], uk["genres"] = [], []
    result = await _enter(session, uk, chapters=None, region="uk")
    assert result.status == WRITTEN
    rows = (await session.execute(select(Book.region, Book.manual_entry_at).filter_by(asin=ASIN))).all()
    assert {r for r, _ in rows} == {"us", "uk"}
    assert {r: stamp is not None for r, stamp in rows} == {"us": False, "uk": True}


async def test_a_region_other_than_the_books_is_refused_and_rolls_back(session):
    before = await snapshot(session)
    with pytest.raises(ValueError):
        await _enter(session, region="uk")
    assert await snapshot(session) == before


# ============================================================
# SHARED ENTITIES: LINK, NEVER CHANGE
# ============================================================


async def test_a_matching_stored_entity_is_linked_and_reported_existing(session):
    await _live_book(session)
    before = await snapshot(session)
    book = writer_dict()
    book["authors"] = [{"name": "First Author", "asin": AUTHOR_ASIN, "description": "stored bio",
                        "image": "https://img.example/stored.jpg"}, {"name": "Second Author"}]
    book["series"] = [{"asin": SERIES_ASIN, "name": "The Series", "position": "2"}]
    result = await _enter(session, book)

    assert result.status == WRITTEN
    status = {k: {e.get("asin") or e["name"]: e["status"] for e in v} for k, v in result.entities.items()}
    assert status["authors"] == {AUTHOR_ASIN: "existing", "Second Author": "new"}
    assert status["series"] == {SERIES_ASIN: "existing"}
    assert status["genres"] == {GENRE_ASIN: "existing"}
    assert status["narrators"] == {"Amy Voice": "existing", "Zed Voice": "new"}

    after = await snapshot(session)
    for table in ("authors", "series", "genres", "narrators"):
        assert set(before[table]) <= set(after[table]), table
    assert (await counts(session))["book_series"] == 2


async def test_an_existing_entity_with_no_description_supplied_is_untouched(session):
    await _live_book(session)
    before = await snapshot(session)
    book = writer_dict()
    book["authors"] = [{"name": "First Author", "asin": AUTHOR_ASIN}]
    book["series"] = [{"asin": SERIES_ASIN, "name": "The Series"}]
    assert (await _enter(session, book)).status == WRITTEN
    after = await snapshot(session)
    for table in ("authors", "series", "genres", "narrators"):
        assert set(before[table]) <= set(after[table]), table


async def test_a_new_author_and_series_take_their_description_image_and_extras(session):
    book = writer_dict()
    book["series"] = [{"asin": SERIES_ASIN, "name": "The Series", "description": "about",
                       "audibleExtras": {"k": 1}, "extrasWithheld": {"w": 2}}]
    await _enter(session, book)
    author = (await session.execute(select(Author).filter_by(asin=AUTHOR_ASIN))).scalar_one()
    series = (await session.execute(select(Series).filter_by(asin=SERIES_ASIN))).scalar_one()
    assert (author.description, author.image) == ("bio", "https://img.example/p.jpg")
    assert (series.description, series.audible_extras, series.extras_withheld) == (
        "about", {"k": 1}, {"w": 2})


# (what the entry says about the stored entity) -> (conflict loc, reason)
CONFLICTS = {
    "author-description": (
        lambda b: b["authors"][0].update(description="a different bio"),
        ["book", "authors", 0, "description"], "author_differs"),
    "author-image": (
        lambda b: b["authors"][0].update(image="https://img.example/other.jpg"),
        ["book", "authors", 0, "image"], "author_differs"),
    "author-name": (
        lambda b: b["authors"][0].update(name="Someone Else"),
        ["book", "authors", 0, "name"], "author_differs"),
    "series-name": (
        lambda b: b["series"][0].update(name="Another Name"),
        ["book", "series", 0, "name"], "series_differs"),
    "series-description": (
        lambda b: b["series"][0].update(description="different"),
        ["book", "series", 0, "description"], "series_differs"),
    "series-extras": (
        lambda b: b["series"][0].update(audibleExtras={"new": 1}),
        ["book", "series", 0, "audibleExtras"], "series_differs"),
    "genre-name": (
        lambda b: b["genres"][0].update(name="Renamed"),
        ["book", "genres", 0], "genre_differs"),
    "genre-type": (
        lambda b: b["genres"][0].update(type="Tags"),
        ["book", "genres", 0], "genre_differs"),
}


def _matching_book():
    book = writer_dict()
    book["authors"] = [{"name": "First Author", "asin": AUTHOR_ASIN, "description": "stored bio",
                        "image": "https://img.example/stored.jpg"}]
    book["series"] = [{"asin": SERIES_ASIN, "name": "The Series", "description": "stored about"}]
    return book


@pytest.mark.parametrize("case", sorted(CONFLICTS))
async def test_a_differing_value_for_a_stored_entity_is_a_conflict_that_writes_nothing(session, case):
    mutate, loc, reason = CONFLICTS[case]
    await _live_book(session)
    before = await snapshot(session)
    book = _matching_book()
    mutate(book)
    result = await _enter(session, book)

    assert result.status == ENTITY
    assert {"loc": loc, "reason": reason} in result.conflicts
    assert result.book is None and result.chapters is None
    assert await snapshot(session) == before


async def test_the_matching_book_the_conflicts_start_from_is_accepted(session):
    await _live_book(session)
    assert (await _enter(session, _matching_book())).status == WRITTEN


async def test_a_conflict_in_a_dry_run_is_the_same_conflict(session):
    await _live_book(session)
    book = _matching_book()
    book["authors"][0]["description"] = "different"
    commit = await _enter(session, book)
    dry = await _enter(session, book, dry_run=True)
    assert (dry.status, dry.conflicts) == (commit.status, commit.conflicts) == (ENTITY, commit.conflicts)


async def test_an_asinless_stored_author_is_not_upgraded_in_place(session):
    await write_books(session, [{
        "asin": "B0OTHER001", "region": "us", "title": "Other",
        "authors": [{"name": "First Author", "region": "us"}],
    }], confirm=True)
    await session.commit()
    before = await snapshot(session)
    book = writer_dict()
    book["authors"] = [{"name": "First Author", "asin": AUTHOR_ASIN}]
    result = await _enter(session, book)
    assert result.status == ENTITY
    assert {"loc": ["book", "authors", 0, "asin"], "reason": "author_asinless_row"} in result.conflicts
    assert await snapshot(session) == before


async def test_conflicts_carry_a_location_and_a_fixed_code_never_an_input_value(session):
    await _live_book(session)
    book = _matching_book()
    book["authors"][0]["description"] = "SECRET-INPUT-VALUE"
    result = await _enter(session, book)
    assert "SECRET-INPUT-VALUE" not in repr(result)
    assert all(set(c) == {"loc", "reason"} for c in result.conflicts)


# ============================================================
# FAILURE PATHS ROLL BACK
# ============================================================


async def test_a_failed_read_back_raises_and_commits_nothing(session):
    before = await snapshot(session)
    with patch.object(manual_entry._read, "get_book", AsyncMock(return_value=None)):
        with pytest.raises(ManualEntryReadbackError):
            await _enter(session)
    assert await snapshot(session) == before


async def test_a_failed_chapter_read_back_raises_and_commits_nothing(session):
    before = await snapshot(session)
    with patch.object(manual_entry._read, "get_track", AsyncMock(return_value=None)):
        with pytest.raises(ManualEntryReadbackError):
            await _enter(session)
    assert await snapshot(session) == before


async def test_a_track_that_is_not_stored_raises_and_commits_nothing(session):
    before = await snapshot(session)
    with patch.object(manual_entry._entities, "write_track", AsyncMock(return_value=None)):
        with pytest.raises(ManualEntryReadbackError):
            await _enter(session)
    assert await snapshot(session) == before


async def test_a_writer_error_propagates_after_a_rollback(session):
    before = await snapshot(session)
    boom = AsyncMock(side_effect=RuntimeError("writer exploded"))
    with patch.object(manual_entry, "write_books", boom):
        with pytest.raises(RuntimeError, match="writer exploded"):
            await _enter(session)
    assert await snapshot(session) == before


async def test_commit_is_called_once_on_success_and_never_on_a_dry_run_or_a_conflict():
    session = AsyncMock()
    ok = manual_entry.ManualEntryResult(WRITTEN)
    with patch.object(manual_entry, "_enter", AsyncMock(return_value=ok)), \
         patch.object(manual_entry, "dialect_of", return_value="sqlite"):
        await enter_book(session, {}, None, region="us", dry_run=False)
        assert session.commit.await_count == 1
        await enter_book(session, {}, None, region="us", dry_run=True)
        assert session.commit.await_count == 1
        for status in (STORED, ENTITY):
            with patch.object(manual_entry, "_enter", AsyncMock(
                    return_value=manual_entry.ManualEntryResult(status))):
                await enter_book(session, {}, None, region="us", dry_run=False)
        assert session.commit.await_count == 1


async def test_the_stamp_is_the_writers_and_the_input_cannot_clear_it(session):
    await _enter(session, writer_dict(manualEntry=False))
    stamp = (await session.execute(text("SELECT manual_entry_at FROM books WHERE asin = :a"), {"a": ASIN})).scalar()
    assert stamp is not None


# ============================================================
# EVERY ACCEPTED KEY LANDS WHERE THE MAP SAYS
# ============================================================


async def test_every_accepted_key_round_trips_to_its_column_or_pivot(session):
    parsed = IngestBody.model_validate({"book": FULL_BOOK, "chapters": FULL_CHAPTERS})
    book = _writer_dict(parsed)
    chapters = parsed.chapters.model_dump(exclude_none=True, exclude_unset=True)
    result = await enter_book(session, book, chapters, region="us", dry_run=False)
    assert result.status == WRITTEN

    await assert_full_round_trip(session)
    served = result.book
    assert [a["name"] for a in served["authors"]] == ["A"]
    assert [n["name"] for n in served["narrators"]] == ["N"]
    assert [(g["asin"], g["name"], g["type"]) for g in served["genres"]] == [("18574426011", "G", "Genres")]
    assert [(s["asin"], s["position"]) for s in served["series"]] == [("B00SERIES1", "1")]


async def test_a_dropped_key_would_be_seen_by_the_round_trip(session):
    parsed = IngestBody.model_validate({"book": FULL_BOOK, "chapters": FULL_CHAPTERS})
    book = _writer_dict(parsed)
    del book["publisher"]
    await enter_book(session, book, parsed.chapters.model_dump(exclude_none=True, exclude_unset=True),
                     region="us", dry_run=False)
    with pytest.raises(AssertionError):
        await assert_full_round_trip(session)
