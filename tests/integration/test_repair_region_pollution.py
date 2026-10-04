"""
The region-pollution repair against real Postgres.

A polluted ISBN-10 book is seeded the way 2.1.x left it: the first-stored us
row carrying another marketplace's author and series links, narrators and
text, beside a de row of its own. What these prove is what a fake session
cannot: the delete cascades through every link table and the track, the
rewrite lands only what Audible answered, the original is_primary survives
the writer's own rule for a new row, the other region's row is not touched,
the backup restores to the identical prior state, and an outage leaves the
whole row as it was.
"""

# Standard library
import json
from datetime import datetime, timezone
from unittest.mock import patch

# Third party
import pytest
from sqlalchemy import insert, select, text

# Local
import app.db.session as db_session_module
import scripts.repair_region_pollution as repair
from app.db.models import (
    Author,
    Book,
    Cache,
    Genre,
    Narrator,
    Series,
    Track,
    author_book,
    book_genre,
    book_narrator,
    book_series,
    series_author,
)
from app.services.cache import manager as cache_manager
from app.services.cache.manager import book_key, chapters_key
from libex_core.exceptions import AudibleAPIException, NotFoundException
from scripts.repair_region_pollution import (
    BackupFile,
    RepairList,
    check_resume,
    committed_indexes,
    reconcile_commits,
    _Audible,
    _Run,
    _snapshot,
    _ThrottleSentinel,
    build_plan,
    process,
    read_backup,
    restore,
)
from tests.fixtures.audible_product import AUDIBLE_PRODUCT

pytestmark = pytest.mark.integration

ASIN = "0123456789"
OLD_CREATED = datetime(2025, 3, 4, 5, 6, 7, 891011, tzinfo=timezone.utc)


async def _seed(session):
    """The polluted us book, its de sibling, and a track and cache entries."""
    us_author = (await session.execute(
        insert(Author).values(name="An Author", asin="B0AUTHOR01", region="us").returning(Author.id)
    )).scalar_one()
    de_author = (await session.execute(
        insert(Author).values(name="Fremder Autor", asin="B0AUTHDE01", region="de").returning(Author.id)
    )).scalar_one()
    # One statement each: the two rows carry different columns.
    await session.execute(insert(Book.__table__).values(
        {"asin": ASIN, "region": "us", "title": "Old us title", "description": "from de",
         "publisher": "Verlag", "is_primary": True, "created_at": OLD_CREATED,
         "updated_at": OLD_CREATED, "chapters_checked_at": None,
         "audible_extras": {"leaked": "from de"}}
    ))
    await session.execute(insert(Book.__table__).values(
        {"asin": ASIN, "region": "de", "title": "de title", "is_primary": False,
         "created_at": OLD_CREATED, "updated_at": OLD_CREATED}
    ))
    await session.execute(insert(Series.__table__), [
        {"asin": "B0SERIES01", "region": "us", "title": "A Series", "created_at": OLD_CREATED,
         "updated_at": OLD_CREATED},
        {"asin": "B0SERIES02", "region": "de", "title": "Eine Reihe", "created_at": OLD_CREATED,
         "updated_at": OLD_CREATED},
    ])
    await session.execute(insert(Narrator.__table__), [
        {"name": "A Narrator", "created_at": OLD_CREATED, "updated_at": OLD_CREATED},
        {"name": "Old Narrator", "created_at": OLD_CREATED, "updated_at": OLD_CREATED},
    ])
    await session.execute(insert(Genre.__table__), [
        {"asin": "G0OLD", "name": "Old Genre", "type": "Tags", "created_at": OLD_CREATED,
         "updated_at": OLD_CREATED},
    ])
    await session.execute(insert(author_book), [
        {"author_id": us_author, "book_asin": ASIN, "book_region": "us"},
        {"author_id": de_author, "book_asin": ASIN, "book_region": "us"},
        {"author_id": de_author, "book_asin": ASIN, "book_region": "de"},
    ])
    await session.execute(insert(book_narrator), [
        {"narrator_name": "A Narrator", "book_asin": ASIN, "book_region": "us"},
        {"narrator_name": "Old Narrator", "book_asin": ASIN, "book_region": "us"},
    ])
    await session.execute(insert(book_genre), [
        {"genre_asin": "G0OLD", "book_asin": ASIN, "book_region": "us"},
    ])
    await session.execute(insert(book_series), [
        {"book_asin": ASIN, "book_region": "us", "series_asin": "B0SERIES01",
         "series_region": "us", "position": "1"},
        {"book_asin": ASIN, "book_region": "us", "series_asin": "B0SERIES02",
         "series_region": "de", "position": "9"},
    ])
    await session.execute(insert(series_author), [
        {"series_asin": "B0SERIES01", "series_region": "us", "author_id": de_author},
    ])
    await session.execute(insert(Track.__table__), [
        {"asin": ASIN, "region": "us", "created_at": OLD_CREATED, "updated_at": OLD_CREATED,
         "chapters": {"chapters": [{"title": f"Old {i}"} for i in range(5)]}},
    ])
    await session.commit()
    await cache_manager.set(session, book_key(ASIN, "us"), {"asin": ASIN, "title": "stale"})
    await cache_manager.set(session, chapters_key(ASIN, "us"), {"chapters": []})
    await cache_manager.set(session, book_key(ASIN, "de"), {"asin": ASIN, "title": "de cache"})


def _fake_get(*, product=None, chapters=None, product_error=None, chapters_error=None):
    async def get(region, path, params=None, extra_headers=None):
        if path.startswith("/1.0/catalog/products"):
            if product_error:
                raise product_error
            return {"product": product} if product else {}
        if chapters_error:
            raise chapters_error
        return chapters
    return get


def _fresh_product():
    return {**AUDIBLE_PRODUCT, "asin": ASIN, "title": "Fresh us title"}


async def _repair(tmp_path, get, *, dry_run=False):
    listed = RepairList([(ASIN, "us")], [], {}, "now")
    run = _Run()
    backup = None if dry_run else BackupFile(tmp_path / "backup.jsonl")
    try:
        with patch.object(repair, "DELAY_MIN", 0.0), patch.object(repair, "DELAY_MAX", 0.0):
            await process(
                db_session_module.AsyncSessionFactory, backup, listed, _Audible(run, get=get),
                run, _ThrottleSentinel(), start=0, end=1, dry_run=dry_run,
            )
    finally:
        if backup is not None:
            backup.close()
    return run


async def _state(session):
    await session.rollback()
    return await _snapshot(session, ASIN, "us", lock=False)


async def _foreign_link_counts(session):
    authors = (await session.execute(text(
        "SELECT count(*) FROM author_book ab JOIN authors a ON a.id = ab.author_id "
        "WHERE ab.book_asin = :a AND a.region <> ab.book_region"
    ), {"a": ASIN})).scalar_one()
    series = (await session.execute(text(
        "SELECT count(*) FROM book_series WHERE book_asin = :a AND series_region <> book_region"
    ), {"a": ASIN})).scalar_one()
    return authors, series


@pytest.mark.asyncio
async def test_plan_selects_the_polluted_isbn10_book_and_the_foreign_series_author(db_session):
    await _seed(db_session)
    # A polluted book that is not ISBN-10-shaped is counted and never listed.
    await db_session.execute(insert(Book.__table__).values(
        asin="B0POLLUTED", region="us", title="x", created_at=OLD_CREATED, updated_at=OLD_CREATED
    ))
    de_author = (await db_session.execute(select(Author.id).where(Author.region == "de"))).scalars().first()
    await db_session.execute(insert(author_book), [
        {"author_id": de_author, "book_asin": "B0POLLUTED", "book_region": "us"},
    ])
    await db_session.commit()

    plan = await build_plan(db_session)

    assert plan.pairs == [(ASIN, "us")]
    assert plan.counts["books_to_repair"] == 1
    assert plan.counts["by_region"] == {"us": 1}
    assert plan.counts["books_with_foreign_authors"] == 1
    assert plan.counts["books_with_foreign_series"] == 1
    assert plan.counts["books_with_both"] == 1
    assert plan.counts["foreign_author_links"] == 1
    assert plan.counts["foreign_series_links"] == 1
    assert plan.counts["foreign_series_author_rows"] == 1
    assert plan.counts["polluted_books_not_isbn10_untouched"] == 1
    assert len(plan.series_authors) == 1 and plan.series_authors[0][:2] == ("B0SERIES01", "us")


@pytest.mark.asyncio
async def test_a_polluted_book_is_replaced_and_the_foreign_links_are_gone(db_session, tmp_path):
    await _seed(db_session)
    de_before = await _snapshot(db_session, ASIN, "de", lock=False)
    assert await _foreign_link_counts(db_session) == (1, 1)

    run = await _repair(tmp_path, _fake_get(product=_fresh_product(), chapters_error=NotFoundException("none")))

    assert run.abort_reason is None and run.repaired == 1 and run.cursor == 1
    after = await _state(db_session)
    assert after["book"]["title"] == "Fresh us title"
    assert after["book"]["description"] != "from de"
    assert after["book"]["publisher"] != "Verlag"
    assert "leaked" not in json.dumps(after["book"]["audible_extras"])
    assert await _foreign_link_counts(db_session) == (0, 0)
    assert after["foreign"] == {"author_ids": [], "series": []}
    assert [r["book_asin"] for r in after["links"]["author_book"]] == [ASIN]
    assert [r["narrator_name"] for r in after["links"]["book_narrator"]] == ["A Narrator"]
    assert [r["series_asin"] for r in after["links"]["book_series"]] == ["B0SERIES01"]
    assert [r["genre_asin"] for r in after["links"]["book_genre"]] == ["18580606011"]
    # A chapters 404 is an empty answer: the old track is gone and none written.
    assert after["track"] is None
    assert after["book"]["chapters_checked_at"] is not None
    # The other marketplace's row is exactly what it was.
    assert await _snapshot(db_session, ASIN, "de", lock=False) == de_before

    cached = await db_session.execute(select(Cache.key).where(Cache.key.like(f"%{ASIN}%")))
    assert {k for (k,) in cached.all()} == {book_key(ASIN, "de")}


@pytest.mark.asyncio
async def test_the_original_is_primary_and_created_at_survive_the_writers_rule(db_session, tmp_path):
    await _seed(db_session)

    await _repair(tmp_path, _fake_get(product=_fresh_product(), chapters_error=NotFoundException("none")))

    after = await _state(db_session)
    # The de row exists, so the writer alone would have inserted this one as
    # non-primary; the deleted row had been the primary.
    assert after["book"]["is_primary"] is True
    assert datetime.fromisoformat(after["book"]["created_at"]) == OLD_CREATED


@pytest.mark.asyncio
async def test_a_non_primary_row_stays_non_primary(db_session, tmp_path):
    await _seed(db_session)
    await db_session.execute(text(
        "UPDATE books SET is_primary = (region = 'de') WHERE asin = :a"
    ), {"a": ASIN})
    await db_session.commit()

    await _repair(tmp_path, _fake_get(product=_fresh_product(), chapters_error=NotFoundException("none")))

    assert (await _state(db_session))["book"]["is_primary"] is False


@pytest.mark.asyncio
async def test_a_chapters_listing_is_written_when_audible_has_one(db_session, tmp_path):
    await _seed(db_session)
    chapters = {"content_metadata": {"chapter_info": {"runtime_length_ms": 120000, "chapters": [
        {"length_ms": 60000, "start_offset_ms": 0, "start_offset_sec": 0, "title": "Fresh 1"},
        {"length_ms": 60000, "start_offset_ms": 60000, "start_offset_sec": 60, "title": "Fresh 2"},
    ]}}}

    await _repair(tmp_path, _fake_get(product=_fresh_product(), chapters=chapters))

    after = await _state(db_session)
    titles = [c["title"] for c in after["track"]["chapters"]["chapters"]]
    assert titles == ["Fresh 1", "Fresh 2"]


@pytest.mark.asyncio
async def test_the_backup_holds_the_whole_prior_state_and_restores_it_exactly(db_session, tmp_path):
    await _seed(db_session)
    before = await _state(db_session)

    await _repair(tmp_path, _fake_get(product=_fresh_product(), chapters_error=NotFoundException("none")))
    assert await _state(db_session) != before

    records = read_backup(tmp_path / "backup.jsonl")
    assert [r["type"] for r in records] == ["book", "commit"]
    assert records[0]["book"] == before["book"]
    assert records[0]["links"] == before["links"]
    assert len(records[0]["track"]["chapters"]["chapters"]) == 5

    run = _Run()
    restored = await restore(
        db_session_module.AsyncSessionFactory, records, run, dry_run=False, limit=None
    )

    assert restored == 1
    assert await _state(db_session) == before


@pytest.mark.asyncio
async def test_an_outage_leaves_the_row_as_it_was(db_session, tmp_path):
    await _seed(db_session)
    before = await _state(db_session)

    run = await _repair(tmp_path, _fake_get(product_error=AudibleAPIException("down")))

    assert run.abort_reason and run.cursor == 0 and run.repaired == 0
    assert await _state(db_session) == before
    assert read_backup(tmp_path / "backup.jsonl") == []
    assert (await db_session.execute(select(Cache.key).where(Cache.key == book_key(ASIN, "us")))).first()


@pytest.mark.asyncio
async def test_a_chapters_outage_leaves_the_row_as_it_was(db_session, tmp_path):
    await _seed(db_session)
    before = await _state(db_session)

    run = await _repair(
        tmp_path, _fake_get(product=_fresh_product(), chapters_error=AudibleAPIException("down"))
    )

    assert run.abort_reason and run.repaired == 0
    assert await _state(db_session) == before
    assert read_backup(tmp_path / "backup.jsonl") == []


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", [
    {},
    {"asin": ASIN},
    {**AUDIBLE_PRODUCT, "asin": ASIN, "publication_datetime": "2200-01-01T00:00:00Z"},
])
async def test_a_stub_or_placeholder_answer_leaves_the_row_as_it_was(db_session, tmp_path, answer):
    await _seed(db_session)
    before = await _state(db_session)

    run = await _repair(
        tmp_path, _fake_get(product=answer or None, chapters_error=NotFoundException("none"))
    )

    assert run.abort_reason is None and run.repaired == 0 and run.unrepaired
    assert await _state(db_session) == before
    assert read_backup(tmp_path / "backup.jsonl") == []


@pytest.mark.asyncio
async def test_a_dry_run_changes_nothing_and_writes_no_backup(db_session, tmp_path):
    await _seed(db_session)
    before = await _state(db_session)

    run = await _repair(
        tmp_path, _fake_get(product=_fresh_product(), chapters_error=NotFoundException("none")),
        dry_run=True,
    )

    assert run.dry_run_books == 1 and run.repaired == 0
    assert await _state(db_session) == before
    assert not (tmp_path / "backup.jsonl").exists()


@pytest.mark.asyncio
async def test_a_failure_inside_the_transaction_rolls_the_row_back(db_session, tmp_path):
    await _seed(db_session)
    before = await _state(db_session)

    with patch.object(repair.writer, "write_books", side_effect=RuntimeError("boom")):
        run = await _repair(
            tmp_path, _fake_get(product=_fresh_product(), chapters_error=NotFoundException("none"))
        )

    assert run.abort_reason and run.cursor == 0
    assert await _state(db_session) == before


@pytest.mark.asyncio
async def test_the_foreign_series_author_rows_are_backed_up_removed_and_restorable(db_session, tmp_path):
    await _seed(db_session)
    plan = await build_plan(db_session)
    backup = BackupFile(tmp_path / "backup.jsonl")
    try:
        removed = await repair._delete_foreign_series_authors(
            db_session_module.AsyncSessionFactory, backup, plan, False
        )
    finally:
        backup.close()

    assert removed == 1
    assert (await db_session.execute(select(series_author))).all() == []

    records = read_backup(tmp_path / "backup.jsonl")
    assert [r["type"] for r in records] == ["series_author"]
    await restore(
        db_session_module.AsyncSessionFactory, records, _Run(), dry_run=False, limit=None
    )
    assert len((await db_session.execute(select(series_author))).all()) == 1


@pytest.mark.asyncio
async def test_a_failure_after_the_backup_line_leaves_the_book_resumable_at_its_own_index(db_session, tmp_path):
    await _seed(db_session)
    before = await _state(db_session)
    path = tmp_path / "backup.jsonl"

    with patch.object(repair.writer, "write_books", side_effect=RuntimeError("boom")):
        run = await _repair(
            tmp_path, _fake_get(product=_fresh_product(), chapters_error=NotFoundException("none"))
        )
    assert run.abort_reason and run.cursor == 0
    records = read_backup(path)
    assert [r["type"] for r in records] == ["book"]
    assert committed_indexes(records) == set()
    assert await _state(db_session) == before

    # The record exists but marks nothing done: resuming at index 0 is allowed
    # and the retry repairs the book.
    check_resume(records, RepairList([(ASIN, "us")], [], {}, "now").sha, 0)
    run = await _repair(
        tmp_path, _fake_get(product=_fresh_product(), chapters_error=NotFoundException("none"))
    )
    assert run.repaired == 1
    assert (await _state(db_session))["foreign"] == {"author_ids": [], "series": []}
    assert committed_indexes(read_backup(path)) == {0}


@pytest.mark.asyncio
async def test_restore_ignores_a_record_that_never_committed(db_session, tmp_path):
    await _seed(db_session)
    before = await _state(db_session)
    with patch.object(repair.writer, "write_books", side_effect=RuntimeError("boom")):
        await _repair(tmp_path, _fake_get(product=_fresh_product(), chapters_error=NotFoundException("none")))

    restored = await restore(
        db_session_module.AsyncSessionFactory, read_backup(tmp_path / "backup.jsonl"),
        _Run(), dry_run=False, limit=None,
    )

    assert restored == 0
    assert await _state(db_session) == before


@pytest.mark.asyncio
async def test_a_commit_that_lost_its_marker_is_marked_and_a_still_polluted_book_is_not(db_session, tmp_path):
    await _seed(db_session)
    path = tmp_path / "backup.jsonl"
    sha = RepairList([(ASIN, "us")], [], {}, "now").sha

    # Still polluted: the record is for a book that did not change.
    with patch.object(repair.writer, "write_books", side_effect=RuntimeError("boom")):
        await _repair(tmp_path, _fake_get(product=_fresh_product(), chapters_error=NotFoundException("none")))
    backup = BackupFile(path)
    assert await reconcile_commits(
        db_session_module.AsyncSessionFactory, backup, read_backup(path), sha
    ) == 0
    backup.close()

    # Replaced, then the marker is lost as if the process died after the commit.
    await _repair(tmp_path, _fake_get(product=_fresh_product(), chapters_error=NotFoundException("none")))
    lines = path.read_text().splitlines()
    path.write_text("\n".join(line for line in lines if '"type":"commit"' not in line) + "\n")
    assert committed_indexes(read_backup(path)) == set()

    backup = BackupFile(path)
    assert await reconcile_commits(
        db_session_module.AsyncSessionFactory, backup, read_backup(path), sha
    ) == 1
    backup.close()
    assert committed_indexes(read_backup(path)) == {0}


@pytest.mark.asyncio
async def test_a_thin_answer_does_not_drop_the_clean_own_region_links(db_session, tmp_path):
    await _seed(db_session)
    thin = {**_fresh_product(), "authors": [], "relationships": [], "narrators": []}

    run = await _repair(tmp_path, _fake_get(product=thin, chapters_error=NotFoundException("none")))

    assert run.repaired == 1
    after = await _state(db_session)
    # The clean us author and us series come back; the foreign ones do not.
    assert [r["series_asin"] for r in after["links"]["book_series"]] == ["B0SERIES01"]
    assert after["links"]["book_series"][0]["position"] == "1"
    us_author = (await db_session.execute(select(Author.id).where(Author.region == "us"))).scalar_one()
    assert [r["author_id"] for r in after["links"]["author_book"]] == [us_author]
    assert after["foreign"] == {"author_ids": [], "series": []}
    # Narrators are not attributable, so the thin answer's none stands.
    assert after["links"]["book_narrator"] == []


@pytest.mark.asyncio
async def test_a_jp_book_is_repaired_from_its_own_region(db_session, tmp_path):
    asin = "4087203123"
    jp_author = (await db_session.execute(
        insert(Author).values(name="Nihon", asin="B0AUTHJP01", region="jp").returning(Author.id)
    )).scalar_one()
    us_author = (await db_session.execute(
        insert(Author).values(name="Gaijin", asin="B0AUTHUS01", region="us").returning(Author.id)
    )).scalar_one()
    await db_session.execute(insert(Book.__table__).values(
        asin=asin, region="jp", title="jp old", created_at=OLD_CREATED, updated_at=OLD_CREATED
    ))
    await db_session.execute(insert(author_book), [
        {"author_id": jp_author, "book_asin": asin, "book_region": "jp"},
        {"author_id": us_author, "book_asin": asin, "book_region": "jp"},
    ])
    await db_session.commit()

    plan = await build_plan(db_session)
    assert plan.pairs == [(asin, "jp")]

    requested = []

    async def get(region, path, params=None, extra_headers=None):
        requested.append(region)
        if path.startswith("/1.0/catalog/products"):
            return {"product": {**AUDIBLE_PRODUCT, "asin": asin, "title": "jp fresh",
                                "authors": [{"asin": "B0AUTHJP01", "name": "Nihon"}]}}
        raise NotFoundException("none")

    run = _Run()
    backup = BackupFile(tmp_path / "backup.jsonl")
    with patch.object(repair, "DELAY_MIN", 0.0), patch.object(repair, "DELAY_MAX", 0.0):
        await process(
            db_session_module.AsyncSessionFactory, backup, plan, _Audible(run, get=get),
            run, _ThrottleSentinel(), start=0, end=1, dry_run=False,
        )
    backup.close()

    assert requested == ["jp", "jp"]
    await db_session.rollback()
    after = await _snapshot(db_session, asin, "jp", lock=False)
    assert after["book"]["title"] == "jp fresh"
    assert [r["author_id"] for r in after["links"]["author_book"]] == [jp_author]
    assert after["foreign"]["author_ids"] == []


@pytest.mark.asyncio
async def test_the_series_author_pass_waits_until_every_listed_book_is_replaced(db_session, tmp_path):
    await _seed(db_session)
    plan = await build_plan(db_session)
    path = tmp_path / "backup.jsonl"
    factory = db_session_module.AsyncSessionFactory

    backup = BackupFile(path)
    assert await repair._series_author_pass(factory, backup, plan, path, _Run()) is None
    backup.close()
    assert len((await db_session.execute(select(series_author))).all()) == 1

    await _repair(tmp_path, _fake_get(product=_fresh_product(), chapters_error=NotFoundException("none")))

    # A stop is honoured between rows: nothing is removed.
    stopping = _Run()
    stopping.request_stop()
    backup = BackupFile(path)
    assert await repair._series_author_pass(factory, backup, plan, path, stopping) == 0
    backup.close()
    await db_session.rollback()
    assert len((await db_session.execute(select(series_author))).all()) == 2

    backup = BackupFile(path)
    assert await repair._series_author_pass(factory, backup, plan, path, _Run()) == 1
    backup.close()
    await db_session.rollback()
    # Only the foreign link went; the rewrite's own us author link stays.
    us_author = (await db_session.execute(select(Author.id).where(Author.region == "us"))).scalar_one()
    assert [r.author_id for r in (await db_session.execute(select(series_author))).all()] == [us_author]
