"""
POST /internal/books and GET /db/delisted against real Postgres, through the
hosted chain: the real writer, the real advisory locks, the real cache table.
Only Audible (the probe) and the secrets are stood in for.

The properties that need a real database: a dry run leaves no row anywhere, a
conflict leaves the shared entities byte-identical, two entries of one ASIN
racing settle to one 200 and one 409, a failed read-back commits nothing, and
the delisted list's query uses the partial index.
"""

# Standard library
import asyncio
import json
import logging
import secrets
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

# Third party
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine, event, insert, select, text
from sqlalchemy.exc import IntegrityError

# Local
import app.db.session as db_session_module
from app.api.routes.internal.router import hash_secret
from app.db.session import get_session
from app.main import app
from app.services.audible import books as books_mod
from app.services.cache.manager import book_key, chapters_key, get, set as cache_set
from app.services.db import manual_entry, persist_queue
from app.services.db.writer import write_books
from libex_core.exceptions import AudibleAPIException, NotFoundException
from libex_core.storage.models import Book
from libex_core.storage.read.books import get_delisted_books
from tests.fixtures.manual_entry import (
    ASIN,
    AUTHOR_ASIN,
    FULL_BOOK,
    FULL_CHAPTERS,
    GENRE_ASIN,
    SERIES_ASIN,
    assert_full_round_trip,
    body,
    counts,
    snapshot,
    stable,
)
from tests.integration._scratch import build_template, clone, drop_database, run_alembic

pytestmark = [pytest.mark.integration, pytest.mark.timeout(300)]

MOD = "app.api.routes.internal.router"
TOKEN = secrets.token_urlsafe(32)
COMMIT = "/internal/books?region=us&dry_run=false"
DRY = "/internal/books?region=us"
ROW_TABLES = ("books", "authors", "narrators", "series", "genres", "tracks",
              "author_book", "book_narrator", "book_series", "book_genre")


@pytest.fixture
def env():
    """Both secrets set, the probe answering absent, the router on the container."""
    with patch(f"{MOD}.settings") as settings, \
         patch(f"{MOD}.probe_listing", new_callable=AsyncMock, return_value="absent") as probe, \
         patch(f"{MOD}.AsyncSessionFactory", db_session_module.AsyncSessionFactory):
        settings.ingest_secret = hash_secret(TOKEN)
        settings.seed_secret = hash_secret(secrets.token_urlsafe(32))
        yield probe


@pytest.fixture
async def client(db_session):
    async def _session():
        yield db_session

    app.dependency_overrides[get_session] = _session
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_session, None)


async def _post(client, payload=None, url=DRY):
    return await client.post(
        url, content=json.dumps(body() if payload is None else payload).encode(),
        headers={"Authorization": f"Bearer {TOKEN}"},
    )


async def _store(books, *, confirm=True):
    """Writes Audible-sourced books on their own session and commits them."""
    async with db_session_module.AsyncSessionFactory() as session:
        await write_books(session, books, confirm=confirm)
        await session.commit()


async def _fresh(db_session):
    """Reads see what other sessions committed."""
    await db_session.rollback()


async def _stored_other_book(**author):
    """An Audible-sourced book carrying the shared author, narrator, series and genre."""
    await _store([{
            "asin": "B0OTHER001", "region": "us", "title": "Other", "language": "english",
            "authors": [{"name": "First Author", "asin": AUTHOR_ASIN, "region": "us",
                         "description": "stored bio", "image": "https://img.example/stored.jpg", **author}],
            "narrators": [{"name": "Amy Voice"}],
            "series": [{"asin": SERIES_ASIN, "name": "The Series", "position": "1",
                        "description": "stored about"}],
            "genres": [{"asin": GENRE_ASIN, "name": "Fantasy", "type": "Genres"}],
        }])


def _matching(**over):
    payload = body(**over)
    payload["book"]["authors"] = [{"name": "First Author", "asin": AUTHOR_ASIN, "description": "stored bio",
                                   "image": "https://img.example/stored.jpg"}]
    payload["book"]["series"] = [{"asin": SERIES_ASIN, "name": "The Series", "description": "stored about"}]
    return payload


# ============================================================
# COMMIT
# ============================================================


async def test_a_commit_fills_every_pivot_the_track_and_the_stamps(client, db_session, env):
    response = await _post(client, url=COMMIT)
    assert response.status_code == 200
    assert (response.json()["dryRun"], response.json()["committed"]) == (False, True)

    await _fresh(db_session)
    book = (await db_session.execute(select(Book).filter_by(asin=ASIN, region="us"))).scalar_one()
    assert book.manual_entry_at is not None
    assert book.confirmed_at is None
    assert book.chapters_checked_at is not None
    assert book.chapters_confirmed_at is None
    assert (book.is_listenable, book.is_buyable, book.is_vvab) == (False, False, False)
    assert await counts(db_session) == {
        "books": 1, "authors": 2, "narrators": 2, "series": 1, "genres": 1, "tracks": 1,
        "author_book": 2, "book_narrator": 2, "book_series": 1, "book_genre": 1,
    }


async def test_every_accepted_key_round_trips_on_postgres(client, db_session, env):
    response = await _post(client, {"book": FULL_BOOK, "chapters": FULL_CHAPTERS}, url=COMMIT)
    assert response.status_code == 200, response.text
    await _fresh(db_session)
    await assert_full_round_trip(db_session)
    served = response.json()["book"]
    assert served["manualEntry"] is True
    assert served["publisher"] == "p" and served["isVvab"] is True


async def test_the_committed_book_is_served_by_db_book_and_listed_with_the_same_keys(client, db_session, env):
    await _post(client, url=COMMIT)
    await _fresh(db_session)
    single = (await client.get(f"/db/book/{ASIN}")).json()
    listed = (await client.get("/db/delisted")).json()
    assert [b["asin"] for b in listed] == [ASIN]
    assert set(listed[0]) == set(single)
    assert listed[0]["manualEntry"] is True and single["manualEntry"] is True
    assert [a["name"] for a in listed[0]["authors"]] == ["First Author", "Second Author"]
    assert [n["name"] for n in listed[0]["narrators"]] == ["Zed Voice", "Amy Voice"]


async def test_credit_order_is_the_order_sent(client, db_session, env):
    await _post(client, url=COMMIT)
    await _fresh(db_session)
    served = (await client.get(f"/db/book/{ASIN}")).json()
    assert [n["name"] for n in served["narrators"]] == ["Zed Voice", "Amy Voice"]
    assert [a["name"] for a in served["authors"]] == ["First Author", "Second Author"]


async def test_a_committed_book_is_not_served_live_when_audible_says_404(client, db_session, env):
    await _post(client, url=COMMIT)
    await _fresh(db_session)
    missing = AsyncMock(side_effect=NotFoundException("gone"))
    with patch.object(books_mod, "_fetch_chunk", new=missing):
        manual = await client.get(f"/book/{ASIN}")
        unknown = await client.get("/book/B0NEVERSTD")
    assert manual.status_code == unknown.status_code == 404
    assert manual.json()["code"] == unknown.json()["code"]
    assert manual.json()["error"].replace(ASIN, "X") == unknown.json()["error"].replace("B0NEVERSTD", "X")


async def test_a_refresh_chunk_that_404s_leaves_the_manual_row_unchanged(client, db_session, env):
    await _post(client, url=COMMIT)
    await _fresh(db_session)
    before = await snapshot(db_session)
    async with db_session_module.AsyncSessionFactory() as session:
        for failure in (NotFoundException("gone"), None):
            fetch = AsyncMock(side_effect=failure) if failure else AsyncMock(return_value=[])
            with patch.object(books_mod, "_fetch_chunk", new=fetch):
                try:
                    await books_mod.get_books_by_asins([ASIN], "us", session)
                except NotFoundException:
                    pass
    await persist_queue.wait_for_inflight()
    await _fresh(db_session)
    assert await snapshot(db_session) == before


# ============================================================
# DRY RUN
# ============================================================


async def test_a_dry_run_leaves_no_row_in_any_table_and_the_cache_untouched(client, db_session, env):
    await cache_set(db_session, book_key(ASIN, "us"), {"marker": "cached"}, 3600)
    await cache_set(db_session, chapters_key(ASIN, "us"), {"marker": "cached"}, 3600)
    before = await snapshot(db_session)

    response = await _post(client)
    assert response.status_code == 200
    data = response.json()
    assert (data["dryRun"], data["committed"]) == (True, False)
    assert data["book"]["asin"] == ASIN and data["chapters"] is not None

    await _fresh(db_session)
    assert await snapshot(db_session) == before
    assert (await get(db_session, book_key(ASIN, "us")))["marker"] == "cached"
    assert (await get(db_session, chapters_key(ASIN, "us")))["marker"] == "cached"
    for table in ROW_TABLES:
        assert (await db_session.execute(text(f"SELECT count(*) FROM {table}"))).scalar() == (  # noqa: S608
            len(before[table]))


async def test_the_dry_run_report_equals_what_the_commit_then_stores(client, db_session, env):
    dry = (await _post(client)).json()
    real = (await _post(client, url=COMMIT)).json()
    assert dry["entities"] == real["entities"]
    assert dry["chapters"] == real["chapters"]
    assert stable(dry["book"]) == stable(real["book"])


async def test_a_commit_clears_the_cache_a_dry_run_does_not(client, db_session, env):
    for key in (book_key(ASIN, "us"), chapters_key(ASIN, "us")):
        await cache_set(db_session, key, {"marker": "cached"}, 3600)
    await _post(client)
    await _fresh(db_session)
    assert await get(db_session, book_key(ASIN, "us")) is not None

    await _post(client, url=COMMIT)
    await _fresh(db_session)
    assert await get(db_session, book_key(ASIN, "us")) is None
    assert await get(db_session, chapters_key(ASIN, "us")) is None


# ============================================================
# ALREADY STORED, AND THE LOCK
# ============================================================


async def test_a_book_already_stored_is_a_409_stored_and_unchanged(client, db_session, env):
    await _post(client, url=COMMIT)
    await _fresh(db_session)
    before = await snapshot(db_session)
    again = await _post(client, body(title="Changed"), url=COMMIT)
    assert again.status_code == 409 and again.json() == {"reason": "stored"}
    await _fresh(db_session)
    assert await snapshot(db_session) == before


async def test_a_row_inserted_between_the_probe_and_the_transaction_is_a_409_stored(client, db_session, env):
    async def probe_then_race(asin, region):
        await _store([{"asin": asin, "region": region, "title": "Audible's"}])
        return "absent"

    env.side_effect = probe_then_race
    response = await _post(client, url=COMMIT)
    assert response.status_code == 409 and response.json() == {"reason": "stored"}
    await _fresh(db_session)
    row = (await db_session.execute(select(Book).filter_by(asin=ASIN))).scalar_one()
    assert row.title == "Audible's" and row.manual_entry_at is None


async def test_two_simultaneous_commits_of_one_asin_store_it_once(client, db_session, env):
    first, second = await asyncio.gather(_post(client, url=COMMIT), _post(client, url=COMMIT))
    assert sorted([first.status_code, second.status_code]) == [200, 409]
    await _fresh(db_session)
    assert (await counts(db_session))["books"] == 1
    assert (await counts(db_session))["tracks"] == 1


async def test_a_book_audible_serves_is_never_written(client, db_session, env):
    env.return_value = "live"
    before = await snapshot(db_session)
    response = await _post(client, url=COMMIT)
    assert response.status_code == 409 and response.json() == {"reason": "live"}
    await _fresh(db_session)
    assert await snapshot(db_session) == before


async def test_an_audible_outage_writes_nothing(client, db_session, env):
    env.side_effect = AudibleAPIException("down")
    response = await _post(client, url=COMMIT)
    assert response.status_code == 503
    await _fresh(db_session)
    assert (await counts(db_session))["books"] == 0


async def test_a_placeholder_listing_proceeds_and_a_placeholder_is_never_a_stored_row(client, db_session, env):
    env.return_value = "placeholder"
    assert (await _post(client, url=COMMIT)).status_code == 200
    # Audible's own persist drops placeholders, so a stored row can only be a manual one.
    await _fresh(db_session)
    rows = (await db_session.execute(select(Book.manual_entry_at))).scalars().all()
    assert len(rows) == 1 and rows[0] is not None


# ============================================================
# SHARED ENTITIES
# ============================================================

CONFLICTS = {
    "author-description": (lambda p: p["book"]["authors"][0].update(description="different"),
                           ["book", "authors", 0, "description"], "author_differs"),
    "author-image": (lambda p: p["book"]["authors"][0].update(image="https://img.example/other.jpg"),
                     ["book", "authors", 0, "image"], "author_differs"),
    "author-name": (lambda p: p["book"]["authors"][0].update(name="Someone Else"),
                    ["book", "authors", 0, "name"], "author_differs"),
    "series-description": (lambda p: p["book"]["series"][0].update(description="different"),
                           ["book", "series", 0, "description"], "series_differs"),
    "series-extras": (lambda p: p["book"]["series"][0].update(audibleExtras={"x": 1}),
                      ["book", "series", 0, "audibleExtras"], "series_differs"),
    "series-name": (lambda p: p["book"]["series"][0].update(name="Renamed"),
                    ["book", "series", 0, "name"], "series_differs"),
    "genre-name": (lambda p: p["book"]["genres"][0].update(name="Renamed"),
                   ["book", "genres", 0], "genre_differs"),
    "genre-type": (lambda p: p["book"]["genres"][0].update(type="Tags"),
                   ["book", "genres", 0], "genre_differs"),
}


@pytest.mark.parametrize("case", sorted(CONFLICTS))
@pytest.mark.parametrize("url", [DRY, COMMIT], ids=["dry-run", "commit"])
async def test_a_differing_value_for_a_stored_entity_is_a_422_and_every_row_is_unchanged(
        client, db_session, env, case, url):
    await _stored_other_book()
    await _fresh(db_session)
    before = await snapshot(db_session)
    payload = _matching()
    mutate, loc, reason = CONFLICTS[case]
    mutate(payload)

    response = await _post(client, payload, url=url)
    assert response.status_code == 422
    assert {"loc": loc, "type": reason} in response.json()["errors"]
    assert all(set(e) == {"loc", "type"} for e in response.json()["errors"])
    await _fresh(db_session)
    assert await snapshot(db_session) == before


async def test_the_matching_payload_the_conflicts_start_from_commits_and_links_without_changing(
        client, db_session, env):
    await _stored_other_book()
    await _fresh(db_session)
    before = await snapshot(db_session)
    response = await _post(client, _matching(), url=COMMIT)
    assert response.status_code == 200, response.text
    statuses = {e.get("asin") or e["name"]: e["status"] for e in response.json()["entities"]["authors"]}
    assert statuses == {AUTHOR_ASIN: "existing"}
    await _fresh(db_session)
    after = await snapshot(db_session)
    for table in ("authors", "series", "genres", "narrators"):
        assert set(before[table]) <= set(after[table]), table


async def test_a_new_author_and_series_take_their_description_and_image_on_insert(client, db_session, env):
    await _post(client, url=COMMIT)
    await _fresh(db_session)
    row = (await db_session.execute(
        text("SELECT description, image FROM authors WHERE asin = :a"), {"a": AUTHOR_ASIN})).one()
    assert tuple(row) == ("bio", "https://img.example/p.jpg")
    desc = (await db_session.execute(
        text("SELECT description FROM series WHERE asin = :a"), {"a": SERIES_ASIN})).scalar()
    assert desc == "about"


# ============================================================
# FAILURES COMMIT NOTHING
# ============================================================


async def test_a_forced_read_back_failure_is_a_500_and_nothing_is_committed(client, db_session, env):
    with patch.object(manual_entry._read, "get_book", AsyncMock(return_value=None)):
        response = await _post(client, url=COMMIT)
    assert response.status_code == 500
    assert response.json() == {"error": "Internal Server Error"}
    await _fresh(db_session)
    assert all(n == 0 for n in (await counts(db_session)).values())


async def test_a_writer_integrity_error_is_a_500_and_the_log_holds_only_the_type(client, db_session, env, caplog):
    boom = IntegrityError("INSERT INTO books VALUES ('SECRET-ROW')", {"title": "SECRET-ROW"}, Exception("dup"))
    with patch.object(manual_entry, "write_books", AsyncMock(side_effect=boom)), \
         caplog.at_level(logging.DEBUG):
        response = await _post(client, body(title="SECRET-ROW"), url=COMMIT)
    assert response.status_code == 500
    logged = "\n".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    assert "IntegrityError" in logged
    assert "SECRET-ROW" not in logged and "SECRET-ROW" not in response.text
    await _fresh(db_session)
    assert all(n == 0 for n in (await counts(db_session)).values())


async def test_a_failed_chapter_write_rolls_the_whole_entry_back(client, db_session, env):
    # The chapter write fails after the book rows are written; the whole entry must roll back.
    with patch.object(manual_entry._entities, "write_track", AsyncMock(side_effect=RuntimeError("boom"))):
        response = await _post(client, url=COMMIT)
    assert response.status_code == 500
    await _fresh(db_session)
    assert all(n == 0 for n in (await counts(db_session)).values())


# ============================================================
# REGIONS
# ============================================================


async def test_a_uk_manual_row_beside_a_us_live_row_is_its_own_record(client, db_session, env):
    await _store([{"asin": ASIN, "region": "us", "title": "Live us"}])
    await _fresh(db_session)
    uk = body(region="uk")
    uk["book"]["series"], uk["book"]["genres"] = [], []
    uk["book"]["authors"] = [{"name": "UK Author"}]

    response = await _post(client, uk, url="/internal/books?region=uk&dry_run=false")
    assert response.status_code == 200, response.text
    await _fresh(db_session)
    rows = (await db_session.execute(select(Book.region, Book.title, Book.manual_entry_at))).all()
    assert {r: (t, stamp is not None) for r, t, stamp in rows} == {"us": ("Live us", False), "uk": ("Delisted", True)}
    listed = (await client.get("/db/delisted")).json()
    assert [(b["asin"], b["region"]) for b in listed] == [(ASIN, "uk")]
    assert (await client.get("/db/delisted?region=us")).json() == []
    assert [b["region"] for b in (await client.get("/db/delisted?region=uk")).json()] == ["uk"]


async def test_a_jp_entry_is_stored_under_jp(client, db_session, env):
    jp = body(region="jp")
    response = await _post(client, jp, url="/internal/books?region=jp&dry_run=false")
    assert response.status_code == 200, response.text
    await _fresh(db_session)
    assert (await db_session.execute(select(Book.region))).scalars().all() == ["jp"]
    assert env.await_args.args == (ASIN, "jp")


async def test_the_same_asin_entered_in_two_regions_is_two_listed_entries(client, db_session, env):
    for region in ("us", "de"):
        payload = body(region=region)
        assert (await _post(client, payload, url=f"/internal/books?region={region}&dry_run=false")).status_code == 200
    await _fresh(db_session)
    listed = (await client.get("/db/delisted")).json()
    assert sorted(b["region"] for b in listed) == ["de", "us"]


# ============================================================
# GET /db/delisted
# ============================================================


async def _enter_many(client, count, region="us"):
    for i in range(count):
        payload = body(asin=f"B0LIST{i:04d}", title=f"Listed {i}")
        payload["book"]["series"], payload["book"]["genres"] = [], []
        payload["book"]["authors"] = [{"name": f"Author {i}"}]
        del payload["chapters"]
        r = await _post(client, payload, url=f"/internal/books?region={region}&dry_run=false")
        assert r.status_code == 200, r.text


async def test_an_empty_list_is_200_and_a_bad_region_is_400(client, db_session, env):
    empty = await client.get("/db/delisted")
    assert empty.status_code == 200 and empty.json() == []
    assert (await client.get("/db/delisted?region=xx")).status_code == 400


async def test_the_list_pages_newest_first_without_repeat_or_skip(client, db_session, env):
    await _enter_many(client, 5)
    await _fresh(db_session)
    everything = [b["asin"] for b in (await client.get("/db/delisted?limit=100")).json()]
    assert everything == [f"B0LIST{i:04d}" for i in (4, 3, 2, 1, 0)]
    paged = []
    for page in (1, 2, 3, 4):
        paged += [b["asin"] for b in (await client.get(f"/db/delisted?limit=2&page={page}")).json()]
    assert paged == everything
    assert (await client.get("/db/delisted?limit=2&page=4")).json() == []


async def test_the_list_sorts_by_a_field_in_both_directions(client, db_session, env):
    await _enter_many(client, 3)
    await _fresh(db_session)
    asc = [b["title"] for b in (await client.get("/db/delisted?sort=title&order=asc")).json()]
    desc = [b["title"] for b in (await client.get("/db/delisted?sort=title&order=desc")).json()]
    assert asc == ["Listed 0", "Listed 1", "Listed 2"] and desc == asc[::-1]


async def test_live_rows_are_never_listed_and_a_relisted_row_stays(client, db_session, env):
    await _enter_many(client, 1)
    await _store([{"asin": "B0LIVEONLY", "region": "us", "title": "Live"}])
    # Audible lists the manual book: the first confirmed answer replaces its fields
    await _store([{"asin": "B0LIST0000", "region": "us", "title": "Audible's title",
                   "description": "what Audible says"}])
    await _fresh(db_session)
    listed = (await client.get("/db/delisted")).json()
    assert [(b["asin"], b["manualEntry"]) for b in listed] == [("B0LIST0000", True)]
    assert listed[0]["description"] == "what Audible says"


async def test_the_readers_own_query_walks_the_partial_index_in_order(db_session):
    """The statement get_delisted_books really sends, not a hand-written twin of it."""
    sent = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        sent.append((statement, parameters))

    engine = db_session.bind.sync_engine
    event.listen(engine, "before_cursor_execute", capture)
    try:
        await get_delisted_books(db_session, limit=20, page=1)
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    statement, parameters = next((s, p) for s, p in sent if "manual_entry_at IS NOT NULL" in s)

    # A table this small is planned as a scan; steer the planner to show which index serves the order.
    await db_session.execute(text("SET LOCAL enable_seqscan = off"))
    await db_session.execute(text("SET LOCAL enable_bitmapscan = off"))

    def explain(sync_session):
        return [row[0] for row in sync_session.connection().exec_driver_sql("EXPLAIN " + statement, parameters)]

    plan = await db_session.run_sync(explain)
    assert any("Index Scan using ix_books_manual_entry" in line for line in plan), plan
    assert not any("Sort" in line for line in plan), plan


async def test_equal_entry_times_page_in_asin_then_region_order_without_the_index(db_session):
    stamp = datetime(2026, 3, 1, tzinfo=timezone.utc)
    for asin, region in (("B0TIE00003", "us"), ("B0TIE00001", "uk"), ("B0TIE00002", "us"),
                         ("B0TIE00001", "us"), ("B0TIE00004", "de")):
        await db_session.execute(insert(Book).values(
            asin=asin, region=region, title=asin, explicit=False, whisper_sync=False, has_pdf=False,
            is_listenable=False, is_buyable=False, is_vvab=False, manual_entry_at=stamp,
            created_at=stamp, updated_at=stamp))
    await db_session.commit()
    # Scan and sort rather than walk the index, so only the ORDER BY decides ties.
    await db_session.execute(text("SET enable_indexscan = off"))
    await db_session.execute(text("SET enable_bitmapscan = off"))
    # region is an enum, so it orders as declared (us before uk), not alphabetically
    expected = [("B0TIE00001", "us"), ("B0TIE00001", "uk"), ("B0TIE00002", "us"),
                ("B0TIE00003", "us"), ("B0TIE00004", "de")]
    got = [(b["asin"], b["region"]) for b in await get_delisted_books(db_session)]
    assert got == expected
    paged = []
    for page in (1, 2, 3):
        paged += [(b["asin"], b["region"]) for b in await get_delisted_books(db_session, limit=2, page=page)]
    assert paged == expected


async def test_the_reader_returns_the_same_items_as_the_route(client, db_session, env):
    await _enter_many(client, 2)
    await _fresh(db_session)
    from_reader = await get_delisted_books(db_session)
    from_route = (await client.get("/db/delisted")).json()
    assert [b["asin"] for b in from_reader] == [b["asin"] for b in from_route]


# ============================================================
# THE HOSTED CHAIN'S INDEX REVISION
# ============================================================

BEFORE = "b7e2d94c1a68"
REVISION = "c4f7a2e9d1b3"


async def test_the_chain_at_head_has_the_partial_index_with_the_listed_order(db_session):
    definition = (await db_session.execute(text(
        "SELECT indexdef FROM pg_indexes WHERE tablename = 'books' AND indexname = 'ix_books_manual_entry'"
    ))).scalar()
    assert definition is not None
    assert "(manual_entry_at DESC, asin, region)" in definition
    assert "WHERE (manual_entry_at IS NOT NULL)" in definition


@pytest.fixture(scope="module")
def template():
    name = build_template(BEFORE)
    yield name
    drop_database(name)


def _index_exists(engine):
    with engine.connect() as c:
        return c.execute(text("SELECT count(*) FROM pg_indexes WHERE indexname = 'ix_books_manual_entry'")).scalar() == 1


def test_the_revision_builds_the_index_and_downgrades_cleanly(template):
    name, sync_url, async_url = clone(template)
    engine = create_engine(sync_url)
    try:
        assert not _index_exists(engine)
        run_alembic(async_url, "upgrade", REVISION)
        assert _index_exists(engine)
        run_alembic(async_url, "downgrade", BEFORE)
        assert not _index_exists(engine)
        run_alembic(async_url, "upgrade", REVISION)
        assert _index_exists(engine)
    finally:
        engine.dispose()
        drop_database(name)
