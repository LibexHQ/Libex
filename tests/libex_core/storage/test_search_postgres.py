"""
The ranked search on a real Postgres: accents fold both ways, an exact match is
the whole name, every supplied field has to match, an omitted region answers
with each ASIN's primary record exactly as the single lookup does, a given
region answers with that marketplace's, and Japanese text is matched as a
substring. Every case also pins what must NOT come back. Needs Docker, through
the container fixture in conftest.
"""

# Standard library
from datetime import timedelta
from unittest.mock import patch

# Third party
import pytest
import pytest_asyncio
from sqlalchemy import insert, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

# Local
from libex_core.storage.base import Base
from libex_core.storage.models import (
    Author,
    Genre,
    Narrator,
    Series,
    author_book,
    author_genre,
    book_narrator,
    book_series,
)
from libex_core.storage.read import books as stored_books
from libex_core.storage.read import search
from libex_core.storage.read.people import get_author
from tests.libex_core.storage._support import STAMP, _book, core_tables, create_extensions

pytestmark = pytest.mark.integration


def _author(id, asin, name, region="us", **kw):
    return Author(id=id, asin=asin, name=name, region=region, created_at=kw.pop("created_at", STAMP),
                  updated_at=STAMP, fetched_description=False, **kw)


def _series(asin, title, region="us", **kw):
    return Series(asin=asin, title=title, region=region, created_at=STAMP, updated_at=STAMP,
                  fetched_description=False, **kw)


async def _link(session, asin, region, *, authors=(), narrators=(), series=()):
    for author_id in authors:
        await session.execute(insert(author_book).values(author_id=author_id, book_asin=asin, book_region=region))
    for name in narrators:
        await session.execute(insert(book_narrator).values(narrator_name=name, book_asin=asin, book_region=region))
    for series_asin, series_region, position in series:
        await session.execute(insert(book_series).values(
            book_asin=asin, book_region=region, series_asin=series_asin,
            series_region=series_region, position=position))


async def _catalog(session):
    session.add_all([
        # Accents, stored with and without.
        _book("B0BREA0001", "Bréanainn's Tale", num_ratings=50),
        _book("B0BREA0002", "Breanainn Plain Story", num_ratings=40),
        _book("B0BREA0003", "Brennan Unrelated", num_ratings=30),
        # The same title twice, and a near miss that is far more popular.
        _book("B0DRAG0001", "Dragon Quest", num_ratings=10),
        _book("B0DRAG0002", "Dragon Quest", num_ratings=90),
        _book("B0DRAG0003", "Dragon Quests", num_ratings=1000),
        _book("B0GHOST001", "Ghost Book"),
        _book("B0USONLY01", "Lonely Americana"),
        _book("0008433844", "The Hobbit"),
        # One ASIN in two marketplaces, the primary being uk.
        _book("B0MULTI001", "Cobalt Sky", subtitle="US cut", region="us", is_primary=False, num_ratings=5),
        _book("B0MULTI001", "Cobalt Sky", subtitle="UK cut", region="uk", is_primary=True, num_ratings=7),
        # Text that exists only on the non-primary row.
        _book("B0MULTI002", "Zephyr Moon", region="us", is_primary=False),
        _book("B0MULTI002", "Lune d'Or", region="uk", is_primary=True),
        # Japanese.
        _book("B0JPHARRY1", "ハリー・ポッターと賢者の石", region="jp", num_ratings=5),
        _book("B0JPSTONE1", "賢者の石", region="jp", num_ratings=1),
        _book("B0JPOTHER1", "魔法使いの弟子", region="jp"),
        # A series of three, and a companion.
        _book("B0STORM001", "Way of Kings"),
        _book("B0STORM002", "Words of Radiance"),
        _book("B0STORM003", "Oathbringer"),
        _book("B0STORM004", "Edgedancer"),
    ])
    session.add_all([
        _author(10, "A0BREA0001", "Bréanainn Ó Dálaigh"),
        _author(11, "A0BL000001", "Brian Lee"),
        _author(12, "A0BLD00001", "Brian Lee Durfee"),
        _author(13, "A0ZED00001", "Zed Quux"),
        _author(14, "A0ANN00001", "Ann Author"),
        _author(15, "A0BOB00001", "Bob Writer"),
        _author(16, None, "Ghost Penname"),
        # Stored first in uk, then in us: uk is the answer without a region.
        _author(17, "A0MULTIAU1", "Sam Multi", region="uk", created_at=STAMP),
        _author(18, "A0MULTIAU1", "Sam Multi", region="us", created_at=STAMP + timedelta(days=1)),
        _author(19, "A0JP000001", "宮本 武蔵", region="jp"),
    ])
    session.add_all([Narrator(name="Nina Voice", created_at=STAMP, updated_at=STAMP, fetched_description=False),
                     Narrator(name="Tom Reader", created_at=STAMP, updated_at=STAMP, fetched_description=False)])
    session.add_all([
        _series("S0DRAG0001", "Dragon Cycle"),
        _series("S0STORM001", "The Stormlight Archive"),
        _series("S0STORM002", "Stormlight Archive Companion"),
        _series("S0COBALT01", "Cobalt Saga", region="us", is_primary=False),
        _series("S0COBALT01", "Cobalt Saga", region="uk", is_primary=True),
    ])
    await session.flush()
    await _link(session, "B0BREA0001", "us", authors=[10], narrators=["Nina Voice"])
    await _link(session, "B0DRAG0001", "us", authors=[14], narrators=["Nina Voice"], series=[("S0DRAG0001", "us", "1")])
    await _link(session, "B0DRAG0002", "us", authors=[15], narrators=["Tom Reader"])
    await _link(session, "B0GHOST001", "us", authors=[16])
    await _link(session, "B0MULTI001", "us", authors=[18], series=[("S0COBALT01", "us", "1")])
    await _link(session, "B0MULTI001", "uk", authors=[17], series=[("S0COBALT01", "uk", "2")])
    for asin in ("B0STORM001", "B0STORM002", "B0STORM003"):
        await _link(session, asin, "us", series=[("S0STORM001", "us", None)])
    await _link(session, "B0STORM004", "us", series=[("S0STORM002", "us", None)])
    await _link(session, "B0JPHARRY1", "jp", authors=[19])
    await session.commit()


@pytest_asyncio.fixture
async def empty(postgres_url):
    engine = create_async_engine(postgres_url, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all, tables=core_tables())
        await create_extensions(conn)
        await conn.run_sync(Base.metadata.create_all, tables=core_tables())
    async with AsyncSession(engine, expire_on_commit=False) as session:
        yield session
    await engine.dispose()


@pytest_asyncio.fixture
async def pg(empty):
    await _catalog(empty)
    return empty


def _asins(records):
    return [r["asin"] for r in records]


def _keys(records):
    return [(r["asin"], r["region"]) for r in records]


# ============================================================
# BOOKS: FOLDING, FIELDS, RANKING
# ============================================================

async def test_accents_fold_in_both_directions(pg):
    plain_query = await search.search_books_ranked(pg, title="Breanainn")
    accented_query = await search.search_books_ranked(pg, title="Bréanainn")
    # Stored with the accent and stored without, found by either spelling.
    assert set(_asins(plain_query)) == set(_asins(accented_query)) == {"B0BREA0001", "B0BREA0002"}
    # A different name is not folded into a match.
    assert "B0BREA0003" not in _asins(plain_query)


async def test_case_is_ignored_and_a_stopword_only_prefix_is_dropped(pg):
    assert _asins(await search.search_books_ranked(pg, title="the HOBBIT")) == ["0008433844"]
    assert await search.search_books_ranked(pg, title="completely unrelated words") == []


async def test_an_isbn_keyed_asin_is_found(pg):
    found = await search.search_books_ranked(pg, title="The Hobbit")
    assert _asins(found) == ["0008433844"]
    assert found[0]["region"] == "us"


async def test_every_supplied_field_has_to_match(pg):
    assert _asins(await search.search_books_ranked(pg, title="Dragon Quest", author="Ann Author")) == ["B0DRAG0001"]
    assert _asins(await search.search_books_ranked(pg, title="Dragon Quest", author="Bob Writer")) == ["B0DRAG0002"]
    # Each field alone has a match; together there is none.
    assert _asins(await search.search_books_ranked(pg, title="Dragon Quest", narrator="Tom Reader", author="Ann Author")) == []
    assert _asins(await search.search_books_ranked(pg, author="Ann Author", narrator="Tom Reader")) == []
    assert _asins(await search.search_books_ranked(pg, author="Ann Author", narrator="Nina Voice")) == ["B0DRAG0001"]
    assert _asins(await search.search_books_ranked(pg, title="Dragon Quest", series="Dragon Cycle")) == ["B0DRAG0001"]
    assert _asins(await search.search_books_ranked(pg, title="Dragon Quest", series="Stormlight Archive")) == []


async def test_the_score_orders_before_popularity_and_popularity_breaks_ties(pg):
    found = await search.search_books_ranked(pg, title="Dragon Quest")
    # The two exact titles tie on score, the more rated first; the near miss
    # has 1000 ratings and still comes last because its score is lower.
    assert _asins(found) == ["B0DRAG0002", "B0DRAG0001", "B0DRAG0003"]


async def test_a_book_with_an_author_that_has_no_asin_matches_by_that_author(pg):
    assert _asins(await search.search_books_ranked(pg, author="Ghost Penname")) == ["B0GHOST001"]
    assert _asins(await search.search_books_ranked(pg, author="Ghost Penname", title="Dragon")) == []


async def test_the_limit_caps_the_answer_and_a_bad_one_is_refused(pg):
    assert len(await search.search_books_ranked(pg, title="Dragon Quest", limit=2)) == 2
    for bad in (0, 51, -1):
        with pytest.raises(ValueError):
            await search.search_books_ranked(pg, title="Dragon Quest", limit=bad)


async def test_a_search_with_no_text_is_refused(pg):
    with pytest.raises(ValueError):
        await search.search_books_ranked(pg)
    with pytest.raises(ValueError):
        await search.search_books_ranked(pg, title=None, author=None)


@pytest.mark.parametrize("given", ["!!", "?-?", "  .. "])
async def test_text_with_no_letter_or_digit_matches_nothing(pg, given):
    assert await search.search_books_ranked(pg, title=given) == []
    assert await search.search_authors_ranked(pg, given) == []
    assert await search.search_series_ranked(pg, given) == []


async def test_text_with_a_letter_among_punctuation_still_searches(pg):
    assert _asins(await search.search_books_ranked(pg, title="!!Dragon Quest!!"))[:2] == ["B0DRAG0002", "B0DRAG0001"]


@pytest.mark.parametrize("pattern", ["%", "%%", "_", "\\"])
async def test_a_wildcard_or_escape_alone_matches_nothing(pg, pattern):
    # A LIKE pattern of a lone % or _ would match every title.
    assert await search.search_books_ranked(pg, title=pattern) == []


@pytest.mark.parametrize("given,expected", [("100%", "100\\%"), ("a_b", "a\\_b"), ("a\\b", "a\\\\b"), ("plain", "plain")])
def test_the_substring_pattern_escapes_what_like_treats_as_special(given, expected):
    assert search._like_literal(given) == expected


# ============================================================
# BOOKS: REGION
# ============================================================

async def test_without_a_region_one_record_per_asin_equals_the_single_lookup(pg):
    found = await search.search_books_ranked(pg, title="Cobalt Sky")
    assert _keys(found) == [("B0MULTI001", "uk")]
    # The primary row is uk, not us: the answer is the lookup's, field for field.
    assert found == [await stored_books.get_book(pg, "B0MULTI001")]
    assert found[0]["subtitle"] == "UK cut"
    assert found != [await stored_books.get_book(pg, "B0MULTI001", region="us")]


async def test_a_region_selects_that_regions_row(pg):
    us = await search.search_books_ranked(pg, title="Cobalt Sky", region="us")
    uk = await search.search_books_ranked(pg, title="Cobalt Sky", region="uk")
    assert _keys(us) == [("B0MULTI001", "us")] and us[0]["subtitle"] == "US cut"
    assert _keys(uk) == [("B0MULTI001", "uk")] and uk[0]["subtitle"] == "UK cut"
    assert us == [await stored_books.get_book(pg, "B0MULTI001", region="us")]


async def test_series_positions_stay_with_the_row_of_their_region(pg):
    us = await search.search_books_ranked(pg, title="Cobalt Sky", region="us")
    uk = await search.search_books_ranked(pg, title="Cobalt Sky", region="uk")
    assert [s["position"] for s in us[0]["series"]] == ["1"]
    assert [s["position"] for s in uk[0]["series"]] == ["2"]


async def test_only_the_primary_rows_text_is_searched_without_a_region(pg):
    assert await search.search_books_ranked(pg, title="Zephyr Moon") == []
    found = await search.search_books_ranked(pg, title="Zephyr Moon", region="us")
    assert _keys(found) == [("B0MULTI002", "us")]
    assert await search.search_books_ranked(pg, title="Zephyr Moon", region="uk") == []


async def test_a_book_stored_in_one_region_is_absent_from_another(pg):
    assert _keys(await search.search_books_ranked(pg, title="Lonely Americana", region="us")) == [("B0USONLY01", "us")]
    assert await search.search_books_ranked(pg, title="Lonely Americana", region="uk") == []


async def test_an_author_search_inside_a_region_pins_the_books_region(pg):
    # Sam Multi wrote B0MULTI001 in both marketplaces.
    assert _keys(await search.search_books_ranked(pg, author="Sam Multi", region="us")) == [("B0MULTI001", "us")]
    assert _keys(await search.search_books_ranked(pg, author="Sam Multi", region="uk")) == [("B0MULTI001", "uk")]
    assert _keys(await search.search_books_ranked(pg, author="Sam Multi")) == [("B0MULTI001", "uk")]



# ============================================================
# BOOKS: LANGUAGE
# ============================================================

async def _two_languages(session):
    # The German one is the more rated, so a missing filter puts it first.
    session.add_all([
        _book("B0LANGEN01", "Tidewalker Chronicle", language="english", num_ratings=5),
        _book("B0LANGDE01", "Tidewalker Chronicle", language="german", num_ratings=500),
    ])
    await session.commit()


async def test_language_keeps_only_that_languages_books_in_the_full_statement(pg):
    await _two_languages(pg)
    both = await search.search_books_ranked(pg, title="Tidewalker Chronicle")
    assert _asins(both) == ["B0LANGDE01", "B0LANGEN01"]
    with patch.object(search, "POPULARITY_MIN_ESTIMATE", 10**9), \
            patch.object(search, "_popular_query", wraps=search._popular_query) as popular:
        english = await search.search_books_ranked(pg, title="Tidewalker Chronicle", language="english")
        german = await search.search_books_ranked(pg, title="Tidewalker Chronicle", language="german")
        none = await search.search_books_ranked(pg, title="Tidewalker Chronicle", language="french")
    assert not popular.called
    assert _asins(english) == ["B0LANGEN01"]
    assert _asins(german) == ["B0LANGDE01"]
    assert none == []


async def test_language_keeps_only_that_languages_books_in_the_popularity_shortcut(pg):
    await _two_languages(pg)
    # limit 1 is filled by the window, so the full statement never runs as a fallback.
    with patch.object(search, "POPULARITY_MIN_ESTIMATE", 0), \
            patch.object(search, "_popular_query", wraps=search._popular_query) as popular:
        english = await search.search_books_ranked(pg, title="Tidewalker Chronicle", language="english", limit=1)
        unfiltered = await search.search_books_ranked(pg, title="Tidewalker Chronicle", limit=1)
    assert popular.call_count == 2
    assert _asins(english) == ["B0LANGEN01"]
    assert _asins(unfiltered) == ["B0LANGDE01"]
    statement, params = search._popular_query("Tidewalker Chronicle", None, "english", 10)
    assert [r.asin for r in await search._rows(pg, statement, params)] == ["B0LANGEN01"]


# ============================================================
# BOOKS: JAPANESE
# ============================================================

async def test_japanese_text_is_a_substring_ranked_shortest_title_first(pg):
    found = await search.search_books_ranked(pg, title="賢者")
    assert _asins(found) == ["B0JPSTONE1", "B0JPHARRY1"]
    assert "B0JPOTHER1" not in _asins(found)
    assert _asins(await search.search_books_ranked(pg, title="魔法")) == ["B0JPOTHER1"]
    assert _keys(await search.search_books_ranked(pg, title="賢者", region="jp")) == [("B0JPSTONE1", "jp"), ("B0JPHARRY1", "jp")]
    assert await search.search_books_ranked(pg, title="賢者", region="us") == []


async def test_japanese_score_divides_by_the_field_that_matched(pg):
    pg.add_all([
        # Matches in the 4-character subtitle; its 20-character title does not.
        _book("B0JPSUB001", "全く関係のない長いだけの題名をここに置く", subtitle="星屑の旅", region="jp", num_ratings=1),
        # Matches in its 8-character title.
        _book("B0JPTITLE1", "星屑の旅路を行く者", region="jp", num_ratings=100),
        # Matches in both: the shorter of the two counts.
        _book("B0JPBOTH01", "星屑の長い長い物語", subtitle="星屑", region="jp", num_ratings=50),
        _book("B0JPNONE01", "月の砂", subtitle="海", region="jp", num_ratings=900),
    ])
    await pg.commit()
    found = await search.search_books_ranked(pg, title="星屑")
    # 2/2, then 2/4 though the subtitle book is the least rated, then 2/9.
    assert _asins(found) == ["B0JPBOTH01", "B0JPSUB001", "B0JPTITLE1"]
    assert "B0JPNONE01" not in _asins(found)


async def test_a_percent_in_japanese_text_is_not_a_wildcard(pg):
    assert await search.search_books_ranked(pg, title="賢%") == []
    assert await search.search_books_ranked(pg, title="賢_") == []


async def test_a_japanese_author_name_is_found_by_substring_and_by_the_book_search(pg):
    assert _asins(await search.search_authors_ranked(pg, "武蔵")) == ["A0JP000001"]
    assert _asins(await search.search_books_ranked(pg, author="武蔵")) == ["B0JPHARRY1"]


# ============================================================
# AUTHORS
# ============================================================

@pytest.mark.parametrize("exact", [False, True])
@pytest.mark.parametrize("limit", [1, 2])
async def test_an_author_with_no_asin_never_takes_a_place_in_the_limit(empty, limit, exact):
    # The unkeyed row is the closest name, so it leads the fuzzy ranking and
    # would eat a slot of the limit if it were not filtered ahead of it.
    empty.add_all([
        _author(1, None, "Quill Marsh"),
        _author(2, "A0QUILL001", "Quill Marsh" if exact else "Quill Marsh Jr"),
        _author(3, "A0QUILL002", "Quill Marsh" if exact else "Quill Marsh Sr"),
    ])
    await empty.flush()
    found = await search.search_authors_ranked(empty, "Quill Marsh", limit=limit, exact=exact)
    assert _asins(found) == ["A0QUILL001", "A0QUILL002"][:limit]


async def test_author_names_fold_accents_both_ways(pg):
    assert _asins(await search.search_authors_ranked(pg, "Breanainn O Dalaigh")) == ["A0BREA0001"]
    assert _asins(await search.search_authors_ranked(pg, "Bréanainn Ó Dálaigh")) == ["A0BREA0001"]
    assert await search.search_authors_ranked(pg, "Brennan Dalton") == []


async def test_exact_excludes_a_longer_name_and_fuzzy_ranks_the_right_one_first(pg):
    exact = await search.search_authors_ranked(pg, "Brian Lee", exact=True)
    assert _asins(exact) == ["A0BL000001"]
    fuzzy = await search.search_authors_ranked(pg, "Brian Lee")
    # Both contain the name; the one that is exactly it comes first.
    assert _asins(fuzzy) == ["A0BL000001", "A0BLD00001"]
    assert "A0ZED00001" not in _asins(fuzzy)


async def test_exact_ignores_case_and_accents_but_not_a_missing_word(pg):
    assert _asins(await search.search_authors_ranked(pg, "BRIAN LEE", exact=True)) == ["A0BL000001"]
    assert _asins(await search.search_authors_ranked(pg, "breanainn o dalaigh", exact=True)) == ["A0BREA0001"]
    assert await search.search_authors_ranked(pg, "Brian", exact=True) == []
    assert await search.search_authors_ranked(pg, "Lee Durfee", exact=True) == []


async def test_an_author_without_an_asin_counts_for_books_but_is_not_listed(pg):
    assert await search.search_authors_ranked(pg, "Ghost Penname") == []
    assert await search.search_authors_ranked(pg, "Ghost Penname", exact=True) == []
    assert _asins(await search.search_authors_ranked(pg, "Ann Author")) == ["A0ANN00001"]


async def test_a_multi_region_author_collapses_to_the_first_stored_row(pg):
    found = await search.search_authors_ranked(pg, "Sam Multi")
    assert [(a["asin"], a["region"], a["regions"]) for a in found] == [("A0MULTIAU1", "uk", ["uk"])]
    assert [a["id"] for a in found] == [17]
    exact = await search.search_authors_ranked(pg, "Sam Multi", exact=True)
    assert [a["id"] for a in exact] == [17]


async def test_an_authors_region_selects_that_regions_row(pg):
    us = await search.search_authors_ranked(pg, "Sam Multi", region="us")
    assert [(a["id"], a["region"]) for a in us] == [(18, "us")]
    assert await search.search_authors_ranked(pg, "Sam Multi", region="fr") == []
    assert await search.search_authors_ranked(pg, "Sam Multi", region="us", exact=True) == us


async def _two_spellings(session):
    """One author, two stored spellings in uk and one row in us, stored later."""
    def row(id, name, region, created, updated, **kw):
        return Author(id=id, asin="A0MERGE001", name=name, region=region, created_at=created,
                      updated_at=updated, fetched_description=False, **kw)

    session.add_all([
        Genre(asin="G0MERGE001", name="Fantasy", type="Genres", created_at=STAMP, updated_at=STAMP),
        Genre(asin="G0MERGE002", name="Epic", type="Genres", created_at=STAMP, updated_at=STAMP),
        row(31, "Merge Person", "uk", STAMP, STAMP + timedelta(days=1),
            description="A fairly long biography of the person, stored first."),
        row(32, "Merge Persons", "uk", STAMP + timedelta(hours=1), STAMP + timedelta(days=5),
            description="Short.", image="https://example.test/merge.jpg"),
        row(33, "Merge Person", "us", STAMP + timedelta(days=2), STAMP, description="US only."),
    ])
    await session.flush()
    await session.execute(insert(author_genre).values([
        {"author_id": 31, "genre_asin": "G0MERGE001"},
        {"author_id": 32, "genre_asin": "G0MERGE001"},
        {"author_id": 32, "genre_asin": "G0MERGE002"},
    ]))
    await session.commit()


async def test_two_spellings_of_one_author_come_back_as_the_single_lookups_merged_record(pg):
    await _two_spellings(pg)
    # Both spellings match the query; they are one result, not two.
    found = await search.search_authors_ranked(pg, "Merge Person", region="uk")
    assert len(found) == 1
    merged = await get_author(pg, "A0MERGE001", "uk")
    assert found == [merged]
    [author] = found
    assert (author["id"], author["name"], author["region"]) == (31, "Merge Person", "uk")
    assert author["description"] == "A fairly long biography of the person, stored first."
    assert author["image"] == "https://example.test/merge.jpg"
    assert sorted(g["asin"] for g in author["genres"]) == ["G0MERGE001", "G0MERGE002"]
    assert author["updatedAt"] == (STAMP + timedelta(days=5)).isoformat()
    # Neither stored row alone is that record.
    assert author["updatedAt"] != (STAMP + timedelta(days=1)).isoformat()
    assert author["description"] != "Short."


async def test_without_a_region_the_merge_is_of_the_first_stored_region(pg):
    await _two_spellings(pg)
    found = await search.search_authors_ranked(pg, "Merge Person")
    assert len(found) == 1
    assert found == [await get_author(pg, "A0MERGE001", "uk")]
    assert found != [await get_author(pg, "A0MERGE001", "us")]
    assert (found[0]["region"], found[0]["description"], found[0]["image"]) == (
        "uk", "A fairly long biography of the person, stored first.", "https://example.test/merge.jpg")
    # The later region is its own record when asked for.
    [us] = await search.search_authors_ranked(pg, "Merge Person", region="us")
    assert (us["id"], us["description"], us["image"], us["genres"]) == (33, "US only.", None, [])
    assert us == await get_author(pg, "A0MERGE001", "us")


async def test_an_author_record_carries_its_stored_fields(pg):
    [author] = await search.search_authors_ranked(pg, "Ann Author")
    assert set(author) == {"id", "asin", "name", "description", "image", "region", "regions", "genres", "updatedAt"}
    assert (author["id"], author["name"], author["genres"]) == (14, "Ann Author", [])


# ============================================================
# SERIES
# ============================================================

async def test_series_carry_how_many_books_are_linked(pg):
    found = await search.search_series_ranked(pg, "Stormlight Archive")
    assert [(s["asin"], s["name"], s["bookCount"]) for s in found] == [
        ("S0STORM001", "The Stormlight Archive", 3),
        ("S0STORM002", "Stormlight Archive Companion", 1),
    ]


async def test_a_series_with_no_books_counts_zero(pg):
    [found] = await search.search_series_ranked(pg, "Dragon Cycle")
    assert found["bookCount"] == 1
    await pg.execute(text("DELETE FROM book_series WHERE series_asin = 'S0DRAG0001'"))
    [found] = await search.search_series_ranked(pg, "Dragon Cycle")
    assert found["bookCount"] == 0


async def test_exact_series_is_the_whole_title_including_a_leading_the(pg):
    exact = await search.search_series_ranked(pg, "The Stormlight Archive", exact=True)
    assert [s["asin"] for s in exact] == ["S0STORM001"]
    assert await search.search_series_ranked(pg, "Stormlight Archive", exact=True) == []
    # Case is ignored, and the longer title is its own series, not a prefix match.
    for spelling in ("stormlight archive companion", "Stormlight Archive Companion"):
        found = await search.search_series_ranked(pg, spelling, exact=True)
        assert [s["asin"] for s in found] == ["S0STORM002"]


async def test_fuzzy_series_search_still_strips_stopwords(pg):
    # "of" and "the" are in neither title, so only stripping lets this match.
    found = await search.search_series_ranked(pg, "The Stormlight Archive of the and to by for")
    assert [s["asin"] for s in found][:2] == ["S0STORM001", "S0STORM002"]
    # Stripping does not loosen exact search: those words are no whole title.
    assert await search.search_series_ranked(pg, "The Stormlight Archive of the and to by for", exact=True) == []


async def test_series_without_a_region_answer_with_the_primary_row(pg):
    found = await search.search_series_ranked(pg, "Cobalt Saga")
    assert [(s["asin"], s["region"], s["bookCount"]) for s in found] == [("S0COBALT01", "uk", 1)]
    us = await search.search_series_ranked(pg, "Cobalt Saga", region="us")
    assert [(s["asin"], s["region"]) for s in us] == [("S0COBALT01", "us")]
    assert await search.search_series_ranked(pg, "Cobalt Saga", region="de") == []


async def test_a_series_record_carries_the_stored_fields(pg):
    [found] = await search.search_series_ranked(pg, "Dragon Cycle")
    assert set(found) == {"asin", "name", "description", "region", "position", "updatedAt",
                          "audibleExtras", "extrasWithheld", "bookCount"}
    assert found["position"] is None


# ============================================================
# THE REST OF THE CONTRACT
# ============================================================

async def test_the_search_sets_its_session_settings_for_the_transaction_only(pg):
    await search.search_books_ranked(pg, title="Dragon Quest")
    assert (await pg.execute(text("SHOW jit"))).scalar() == "off"
    assert (await pg.execute(text("SHOW work_mem"))).scalar() == search.SEARCH_WORK_MEM
    assert float((await pg.execute(text("SHOW pg_trgm.word_similarity_threshold"))).scalar()) == search.WORD_SIMILARITY_THRESHOLD
    await pg.rollback()
    assert (await pg.execute(text("SHOW jit"))).scalar() == "on"
    assert (await pg.execute(text("SHOW work_mem"))).scalar() != search.SEARCH_WORK_MEM
