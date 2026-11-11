"""
The search statements against a catalog big enough for the planner to choose:
each folded index is the one used under a generic plan (what a prepared
statement gets), the popularity shortcut answers exactly what the full
statement does, and a bound separator in the indexed expression is shown to
lose the index. Needs Docker, through the container fixture in conftest.
"""

# Standard library
import hashlib
import re
from unittest.mock import patch

# Third party
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

# Local
from libex_core.storage.base import Base
from libex_core.storage.read import search
from tests.libex_core.storage._support import core_tables, create_extensions

# The function-scoped big fixture rebuilds and indexes 30000 rows per test, over the 30s default under load.
pytestmark = [pytest.mark.integration, pytest.mark.timeout(300)]

ROWS = 30000
NOT_NULL = "explicit, whisper_sync, has_pdf, is_listenable, is_buyable, is_vvab, created_at, updated_at"
NOT_NULL_VALUES = "false, false, false, true, true, false, now(), now()"


def token(n: int, length: int = 10) -> str:
    return hashlib.md5(str(n).encode()).hexdigest()[:length]


SQL_TOKEN = "substr(md5(g::text), 1, 10)"


@pytest_asyncio.fixture
async def big(postgres_url):
    engine = create_async_engine(postgres_url, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all, tables=core_tables())
        await create_extensions(conn)
        await conn.run_sync(Base.metadata.create_all, tables=core_tables())
        await conn.execute(text(
            f"INSERT INTO books (asin, region, title, num_ratings, {NOT_NULL}) "
            f"SELECT 'BULK' || lpad(g::text, 6, '0'), 'us', 'Bulk ' || {SQL_TOKEN} || CASE WHEN g % 10 = 0 THEN ' common saga' ELSE ' plain tale' END, "
            f"CASE WHEN g % 7 = 0 THEN NULL ELSE g % 1000 END, {NOT_NULL_VALUES} FROM generate_series(1, {ROWS}) g"
        ))
        # Far more rated than any of the above, and a worse match for "common saga".
        await conn.execute(text(
            f"INSERT INTO books (asin, region, title, num_ratings, {NOT_NULL}) "
            f"SELECT 'NEAR' || lpad(g::text, 6, '0'), 'us', 'Common Sagas ' || g, 500000 + g, {NOT_NULL_VALUES} "
            "FROM generate_series(1, 20) g"
        ))
        await conn.execute(text(
            f"INSERT INTO authors (asin, name, region, fetched_description, created_at, updated_at) "
            f"SELECT 'AUTH' || lpad(g::text, 6, '0'), {SQL_TOKEN} || ' Author', 'us', false, now(), now() "
            f"FROM generate_series(1, {ROWS}) g"
        ))
        await conn.execute(text(
            f"INSERT INTO series (asin, title, region, fetched_description, created_at, updated_at) "
            f"SELECT 'SER' || lpad(g::text, 7, '0'), {SQL_TOKEN} || ' Saga', 'us', false, now(), now() "
            f"FROM generate_series(1, {ROWS}) g"
        ))
        await conn.execute(text(
            f"INSERT INTO narrators (name, fetched_description, created_at, updated_at) "
            f"SELECT {SQL_TOKEN} || ' Reader', false, now(), now() FROM generate_series(1, {ROWS}) g"
        ))
        await conn.execute(text(
            "INSERT INTO book_narrator (narrator_name, book_asin, book_region) "
            f"SELECT {SQL_TOKEN} || ' Reader', 'BULK' || lpad(g::text, 6, '0'), 'us' FROM generate_series(1, {ROWS}) g"
        ))
        for table in ("books", "authors", "series", "narrators", "book_narrator"):
            await conn.execute(text(f"ANALYZE {table}"))
    async with AsyncSession(engine, expire_on_commit=False) as session:
        yield session
    await engine.dispose()


def _numbered(statement: str, params: dict) -> tuple[str, list]:
    """The statement with $n placeholders and the values in order."""
    names = sorted(params, key=lambda n: int(n[1:]))
    sql = re.sub(r":p(\d+)", lambda m: f"${int(m.group(1)) + 1}", statement)
    return sql, [params[n] for n in names]


async def _generic_plan(session, statement: str, params: dict) -> str:
    sql, values = _numbered(statement, params)
    await session.execute(text("SET LOCAL plan_cache_mode = force_generic_plan"))
    await session.execute(text("SET LOCAL jit = off"))
    literals = ", ".join("'" + str(v).replace("'", "''") + "'" for v in values)
    await session.execute(text(f"PREPARE q AS {sql}"))
    try:
        rows = (await session.execute(text(f"EXPLAIN EXECUTE q({literals})"))).scalars().all()
    finally:
        await session.execute(text("DEALLOCATE q"))
    return "\n".join(rows)


async def test_a_selective_title_search_uses_the_folded_title_index_under_a_generic_plan(big):
    statement, params = search._book_query({"title": (token(77), False)}, None, None, 10, None)
    plan = await _generic_plan(big, statement, params)
    assert "ix_books_fold_title_gin" in plan, plan
    assert "Seq Scan on books" not in plan, plan


async def test_the_same_search_with_a_bound_separator_loses_the_index(big):
    """The reason the separators in the expression are SQL literals: bound, the
    generic plan cannot match the expression to the index and reads every row."""
    statement = (
        "SELECT b.asin FROM books b WHERE public.libex_fold(CAST($1 AS text)) OPERATOR(public.<%) "
        "public.libex_fold(b.title || CAST($2 AS text) || coalesce(b.subtitle, CAST($3 AS text))) LIMIT 10"
    )
    await big.execute(text("SET LOCAL plan_cache_mode = force_generic_plan"))
    await big.execute(text("PREPARE q AS " + statement))
    plan = "\n".join((await big.execute(text(f"EXPLAIN EXECUTE q('{token(77)}', ' ', '')"))).scalars().all())
    await big.execute(text("DEALLOCATE q"))
    assert "ix_books_fold_title_gin" not in plan, plan
    assert "Seq Scan on books" in plan, plan


async def test_an_author_fuzzy_search_uses_the_folded_name_index(big):
    statement, params = search._entity_query("authors", "a", "name", token(88), False, False, None, 10, "id")
    plan = await _generic_plan(big, statement, params)
    assert "ix_authors_fold_name_gin" in plan, plan
    # Without a region the first-stored check reads authors a second time; the
    # candidates themselves come from the index.
    assert "Seq Scan on authors a" not in plan, plan
    pinned = await _generic_plan(big, *search._entity_query("authors", "a", "name", token(88), False, False, "us", 10, "id"))
    assert "ix_authors_fold_name_gin" in pinned and "Seq Scan" not in pinned, pinned


async def test_an_author_exact_search_uses_the_btree_not_the_trigram_index(big):
    statement, params = search._entity_query("authors", "a", "name", f"{token(88)} author", False, True, None, 10, "id")
    plan = await _generic_plan(big, statement, params)
    assert "ix_authors_fold_name_bt" in plan, plan
    assert "ix_authors_fold_name_gin" not in plan and "Seq Scan on authors a" not in plan, plan


async def test_series_searches_use_their_folded_indexes(big):
    fuzzy = await _generic_plan(big, *search._entity_query("series", "s", "title", token(99), False, False, None, 10, "asin"))
    exact = await _generic_plan(big, *search._entity_query("series", "s", "title", f"{token(99)} saga", False, True, None, 10, "asin"))
    assert "ix_series_fold_title_gin" in fuzzy and "Seq Scan on series" not in fuzzy, fuzzy
    assert "ix_series_fold_title_bt" in exact and "Seq Scan on series" not in exact, exact


async def test_a_narrator_search_starts_from_the_folded_narrator_index(big):
    statement, params = search._book_query({"narrator": (token(55), False)}, None, None, 10, "narrator")
    plan = await _generic_plan(big, statement, params)
    assert "ix_narrators_fold_name_gin" in plan, plan
    assert "Seq Scan on narrators" not in plan, plan


async def test_the_narrator_join_can_reach_the_books_by_index_not_a_scan(big):
    """At this size the planner hashes the join; with hash and merge joins
    off it shows the indexes it would otherwise walk."""
    statement, params = search._book_query({"narrator": (token(55), False)}, None, None, 10, "narrator")
    await big.execute(text("SET LOCAL enable_hashjoin = off"))
    await big.execute(text("SET LOCAL enable_mergejoin = off"))
    plan = await _generic_plan(big, statement, params)
    assert "ix_book_narrator_fold_narrator_name" in plan, plan
    assert "Seq Scan on books" not in plan and "Seq Scan on book_narrator" not in plan, plan


async def test_the_popularity_shortcut_answers_what_the_full_statement_does(big):
    full_statement, full_params = search._book_query({"title": ("common saga", False)}, None, None, 10, None)
    full = [(r.asin, r.region) for r in await search._rows(big, full_statement, full_params)]
    # The estimate gate has its own tests; zero here so the fixture's estimate cannot decide whether this runs.
    with patch.object(search, "POPULARITY_MIN_ESTIMATE", 0), \
            patch.object(search, "_popular_query", wraps=search._popular_query) as popular:
        quick = await search.search_books_ranked(big, title="common saga")
    assert popular.called, "the shortcut was not taken, so this compares the full statement with itself"
    assert [(r["asin"], r["region"]) for r in quick] == full
    assert len(full) == 10
    # The near misses are the most rated of all and must not lead.
    assert not any(a.startswith("NEAR") for a, _ in full)


async def test_without_the_shortcut_the_same_answer_comes_back(big):
    with patch.object(search, "POPULARITY_MIN_ESTIMATE", 10**9), \
            patch.object(search, "_popular_query", wraps=search._popular_query) as popular:
        slow = await search.search_books_ranked(big, title="common saga")
    assert not popular.called
    fast = await search.search_books_ranked(big, title="common saga")
    assert [(r["asin"], r["region"]) for r in slow] == [(r["asin"], r["region"]) for r in fast]


async def test_a_page_is_the_same_every_time(big):
    first = await search.search_books_ranked(big, title="common saga", limit=50)
    second = await search.search_books_ranked(big, title="common saga", limit=50)
    assert [r["asin"] for r in first] == [r["asin"] for r in second]
    # Equal scores, so the order is popularity (nulls last), then asin.
    ratings = [r["numRatings"] for r in first]
    assert ratings == sorted(ratings, key=lambda n: (n is None, -(n or 0)))


async def test_a_title_the_shortcut_cannot_fill_falls_back_to_the_full_statement(big):
    """Fewer than `limit` perfect matches in the window means the window proves
    nothing; the full ranking decides."""
    found = await search.search_books_ranked(big, title="common sagas", limit=50)
    assert found, "the near misses and the saga titles all match"
    assert len(found) == 50
