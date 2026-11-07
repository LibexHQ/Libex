"""
Ranked search over the stored catalog: books, authors and series.

Postgres only. The matching reads the folded expression indexes the search
migrations build (public.libex_fold: lowercased, accents removed) and pg_trgm's
word similarity, so every function and operator here is spelled with its schema
and nothing depends on search_path. Input is always a bound value, never part of
the SQL text and never a LIKE pattern unless it is escaped as one.

Every supplied field has to match. A book's score is the weighted mean of its
fields' scores (title 1, the rest one half) and the order is a total one: score,
then popularity, then asin and region, so a page is the same every time. Without
a region only each ASIN's primary row is searched, as /db/book answers with it;
with one, that marketplace's rows.

Three shapes keep the common cases fast. A title that matches a great many books
is answered from the popular end of the catalog first. A search by author,
narrator or series starts from the matching people or series and reaches their
books by primary key, instead of probing every book. Japanese, Chinese and
Korean text has no word boundaries, so word similarity is useless for it; it is
matched as a substring and ranked by length.

The record returned is the one the single lookups return: the same relations, the
same dict builder, the series positions read for all of the books together.
"""

# Standard library
import json
import re
import unicodedata
from typing import Any

# Third party
from sqlalchemy import func, select, text, tuple_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

# Local
from libex_core.storage.models import Author, Book, Series, book_series
from libex_core.storage.read._compat import dialect_name
from libex_core.storage.read.people import merge_author_rows
from libex_core.storage.read.series import series_to_dict
from libex_core.storage.read.shapes import (
    BOOK_RELATIONS,
    book_to_dict,
    series_positions_by_book,
)

MAX_LIMIT = 50

# Applied to each search with SET LOCAL. JIT compiles for tens of milliseconds
# and saves nothing on these statements; the larger work_mem keeps the sort of a
# wide candidate set in memory.
SEARCH_WORK_MEM = "64MB"

# word_similarity() at or above this counts as a match.
WORD_SIMILARITY_THRESHOLD = 0.6

# The popularity shortcut looks at this many of the most popular books, and is
# only tried when the planner expects at least POPULARITY_MIN_ESTIMATE matches.
POPULARITY_WINDOW = 20000
POPULARITY_MIN_ESTIMATE = 2000

WEIGHTS = {"title": 1.0, "author": 0.5, "narrator": 0.5, "series": 0.5}

# Which field a search starts from when the title cannot (or need not) drive it.
_ENTITY_FIELDS = ("author", "series", "narrator")

# Words that only dilute a title query. They are dropped when a real word is left.
_STOPWORDS = frozenset(
    "a an and at by for from in is of on or the to with".split()
)

# Hiragana, katakana, CJK ideographs and extensions, Hangul, halfwidth katakana.
_CJK = re.compile(
    "[぀-ヿ㐀-䶿一-鿿豈-﫿가-힯ｦ-ﾟ]"
)

_BOOK_FOLDED = "public.libex_fold(b.title || ' ' || coalesce(b.subtitle, ''))"

# Per field: the tables a book reaches it through, joined from the book (the
# lateral form) and the column holding its text.
_LATERAL = {
    "author": (
        "author_book ab JOIN authors au ON au.id = ab.author_id",
        "ab.book_asin = b.asin AND ab.book_region = b.region",
        "au.name",
    ),
    "narrator": (
        "book_narrator bn",
        "bn.book_asin = b.asin AND bn.book_region = b.region",
        "bn.narrator_name",
    ),
    "series": (
        "book_series bs JOIN series se ON se.asin = bs.series_asin AND se.region = bs.series_region",
        "bs.book_asin = b.asin AND bs.book_region = b.region",
        "se.title",
    ),
}

# Per field: the same tables entered from the person or series (the driven
# form), the column holding its text, and the book's asin and region.
_DRIVEN = {
    "author": (
        "authors au JOIN author_book ab ON ab.author_id = au.id",
        "au.name", "ab.book_asin", "ab.book_region",
    ),
    "narrator": (
        "narrators nr JOIN book_narrator bn ON bn.narrator_name = nr.name",
        "nr.name", "bn.book_asin", "bn.book_region",
    ),
    "series": (
        "series se JOIN book_series bs ON bs.series_asin = se.asin AND bs.series_region = se.region",
        "se.title", "bs.book_asin", "bs.book_region",
    ),
}


class _Sql:
    """Collects the bound values of one statement."""

    def __init__(self) -> None:
        self.params: dict[str, Any] = {}

    def bind(self, value: Any) -> str:
        name = f"p{len(self.params)}"
        self.params[name] = value
        return f":{name}"


def _like_literal(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _clean(value: str | None) -> str | None:
    """The text compared and measured: NFKC, so a full-width space or letter
    counts as the plain one, and stripped. None stays None."""
    return None if value is None else unicodedata.normalize("NFKC", value).strip()


def _without_stopwords(value: str) -> str:
    kept = [w for w in value.split() if w.strip(".,:;!?\"'()[]").lower() not in _STOPWORDS]
    return " ".join(kept) if kept else value


def _match(sql: _Sql, value: str, folded: str, raw: tuple[str, ...], cjk: bool) -> tuple[str, str]:
    """(predicate, score) of `value` against a column. `folded` is the folded
    expression the trigram index covers, `raw` the plain columns for the
    substring form."""
    if cjk:
        pattern = sql.bind("%" + _like_literal(value) + "%")
        predicate = "(" + " OR ".join(f"{column} ILIKE {pattern}" for column in raw) + ")"
        length = sql.bind(float(len(value)))
        # Scored against the field that matched: the shorter one when both do.
        matched = ", ".join(f"CASE WHEN {column} ILIKE {pattern} THEN char_length({column}) END" for column in raw)
        return predicate, f"(CAST({length} AS real) / greatest(least({matched}), 1))"
    query = f"public.libex_fold(CAST({sql.bind(value)} AS text))"
    return f"{query} OPERATOR(public.<%) {folded}", f"public.word_similarity({query}, {folded})"


def _region_condition(sql: _Sql, region: str | None, alias: str) -> str:
    if region is None:
        return f"{alias}.is_primary"
    return f"{alias}.region = CAST({sql.bind(region)} AS region_enum)"


def _book_query(
    fields: dict[str, tuple[str, bool]], region: str | None, language: str | None,
    limit: int, drive: str | None,
) -> tuple[str, dict[str, Any]]:
    """The ranked statement. With `drive` the books are reached from that
    entity's matches; without it, from the books whose title matches."""
    sql = _Sql()
    joins: list[str] = []
    where: list[str] = []
    terms: list[str] = []
    weight = 0.0
    lateral_fields = [f for f in fields if f != "title" and f != drive]

    if drive is None:
        source = "books b"
    else:
        value, cjk = fields[drive]
        tables, column, book_asin, book_region = _DRIVEN[drive]
        predicate, score = _match(sql, value, f"public.libex_fold({column})", (column,), cjk)
        pinned = f" AND {book_region} = CAST({sql.bind(region)} AS region_enum)" if region else ""
        source = "m JOIN books b ON b.asin = m.asin AND b.region = m.region"
        cte = (
            f"WITH m AS MATERIALIZED (SELECT {book_asin} AS asin, {book_region} AS region, max({score}) AS s "
            f"FROM {tables} WHERE {predicate}{pinned} GROUP BY 1, 2) "
        )
        terms.append(f"m.s * {WEIGHTS[drive]}")
        weight += WEIGHTS[drive]

    if "title" in fields:
        value, cjk = fields["title"]
        predicate, score = _match(sql, value, _BOOK_FOLDED, ("b.title", "b.subtitle"), cjk)
        where.append(predicate)
        terms.append(f"{score} * {WEIGHTS['title']}")
        weight += WEIGHTS["title"]

    for field in lateral_fields:
        value, cjk = fields[field]
        tables, link, column = _LATERAL[field]
        predicate, score = _match(sql, value, f"public.libex_fold({column})", (column,), cjk)
        joins.append(
            f"JOIN LATERAL (SELECT max({score}) AS s FROM {tables} WHERE {link} AND {predicate}) "
            f"l_{field} ON l_{field}.s IS NOT NULL"
        )
        terms.append(f"l_{field}.s * {WEIGHTS[field]}")
        weight += WEIGHTS[field]

    where.append(_region_condition(sql, region, "b"))
    if language is not None:
        where.append(f"b.language = {sql.bind(language)}")
    prefix = cte if drive is not None else ""
    statement = (
        f"{prefix}SELECT b.asin AS asin, CAST(b.region AS text) AS region, "
        f"(({' + '.join(terms)}) / {weight}) AS score FROM {source} {' '.join(joins)} "
        f"WHERE {' AND '.join(where)} "
        f"ORDER BY score DESC, b.num_ratings DESC NULLS LAST, b.asin, b.region LIMIT {int(limit)}"
    )
    return statement, sql.params


def _popular_query(
    title: str, region: str | None, language: str | None, limit: int
) -> tuple[str, dict[str, Any]]:
    """Titles that hold every trigram of the query score 1, the highest a title
    can. The window is a prefix of the popularity order, so when it yields
    `limit` such titles they are the first `limit` of the full ranking."""
    sql = _Sql()
    query = f"public.libex_fold(CAST({sql.bind(title)} AS text))"
    where = [_region_condition(sql, region, "b")]
    if language is not None:
        where.append(f"b.language = {sql.bind(language)}")
    statement = (
        "SELECT w.asin AS asin, CAST(w.region AS text) AS region FROM ("
        f"SELECT b.asin, b.region, b.num_ratings, {_BOOK_FOLDED} AS f FROM books b "
        f"WHERE {' AND '.join(where)} "
        f"ORDER BY b.num_ratings DESC NULLS LAST, b.asin, b.region LIMIT {POPULARITY_WINDOW}) w "
        f"WHERE {query} OPERATOR(public.<%) w.f AND public.word_similarity({query}, w.f) >= 1.0 "
        f"ORDER BY w.num_ratings DESC NULLS LAST, w.asin, w.region LIMIT {int(limit)}"
    )
    return statement, sql.params


def _entity_query(
    table: str, alias: str, column: str, value: str, cjk: bool, exact: bool,
    region: str | None, limit: int, key: str,
) -> tuple[str, dict[str, Any]]:
    """Authors or series ranked by name. `key` is the column returned as k; the
    readers pass asin, the plan tests also pass id."""
    sql = _Sql()
    folded = f"public.libex_fold({alias}.{column})"
    if exact:
        query = f"public.libex_fold(CAST({sql.bind(value)} AS text))"
        where = [f"{folded} = {query}"]
        order = f"{alias}.asin, {alias}.region"
        score = "1.0"
    else:
        predicate, score = _match(sql, value, folded, (f"{alias}.{column}",), cjk)
        where = [predicate]
        order = f"score DESC, {alias}.asin, {alias}.region"
        if not cjk:
            similarity = f"public.similarity(public.libex_fold(CAST({sql.bind(value)} AS text)), {folded})"
            order = f"score DESC, {similarity} DESC, {alias}.asin, {alias}.region"
    if table == "authors":
        where.append(f"{alias}.asin IS NOT NULL")
        if region is None:
            # The first-stored row of each ASIN, ahead of the ranking and the limit.
            where.append(
                "NOT EXISTS (SELECT 1 FROM authors o WHERE o.asin = a.asin AND "
                "(o.created_at, CAST(o.region AS text), o.id) < (a.created_at, CAST(a.region AS text), a.id))"
            )
    if region is None:
        if table == "series":
            where.append(f"{alias}.is_primary")
    else:
        where.append(f"{alias}.region = CAST({sql.bind(region)} AS region_enum)")
    statement = (
        f"SELECT {alias}.{key} AS k, CAST({alias}.region AS text) AS region, {score} AS score "
        f"FROM {table} {alias} WHERE {' AND '.join(where)} ORDER BY {order} LIMIT {int(limit)}"
    )
    return statement, sql.params


def _plan_figures(plan: Any) -> tuple[float, float]:
    """(total cost, largest row estimate of a bitmap heap scan) from EXPLAIN (FORMAT JSON)."""
    if isinstance(plan, str):
        plan = json.loads(plan)
    root = plan[0]["Plan"]
    estimate = 0.0
    pending = [root]
    while pending:
        node = pending.pop()
        pending.extend(node.get("Plans", []))
        if node["Node Type"] == "Bitmap Heap Scan":
            estimate = max(estimate, node["Plan Rows"])
    return root["Total Cost"], estimate


async def _explain(session: AsyncSession, statement: str, params: dict[str, Any]) -> tuple[float, float]:
    result = await session.execute(text("EXPLAIN (FORMAT JSON) " + statement), params)
    return _plan_figures(result.scalar())


async def _rows(session: AsyncSession, statement: str, params: dict[str, Any]):
    return (await session.execute(text(statement), params)).all()


async def _prepare(session: AsyncSession, limit: int) -> None:
    if dialect_name(session) != "postgresql":
        raise NotImplementedError("ranked search needs PostgreSQL with the pg_trgm and unaccent extensions")
    if not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_LIMIT}")
    await session.execute(text("SET LOCAL jit = off"))
    await session.execute(text(f"SET LOCAL work_mem = '{SEARCH_WORK_MEM}'"))
    await session.execute(text(f"SET LOCAL pg_trgm.word_similarity_threshold = {WORD_SIMILARITY_THRESHOLD}"))


def _field(name: str, value: str | None, strip: bool = True) -> tuple[str, bool] | None:
    """(text to match, whether it is matched as a substring), or None when the
    field was not supplied. Stopwords go only where `strip`: an exact match
    compares the whole input."""
    value = _clean(value)
    if value is None:
        return None
    cjk = _CJK.search(value) is not None
    if strip and name in ("title", "series") and not cjk:
        value = _without_stopwords(value)
    return value, cjk


def _matchable(*values: str | None) -> bool:
    """False when a supplied text has no letter or digit: it has no trigrams, so
    it cannot use an index and could only scan the table to find nothing useful."""
    return all(v is None or any(ch.isalnum() for ch in v) for v in values)


async def search_books_ranked(
    session: AsyncSession,
    *,
    title: str | None = None,
    author: str | None = None,
    narrator: str | None = None,
    series: str | None = None,
    region: str | None = None,
    language: str | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Books whose title, author, narrator and series all match what was
    given, best first, as full records. At least one text is required."""
    await _prepare(session, limit)
    given = {"title": title, "author": author, "narrator": narrator, "series": series}
    fields = {k: f for k, v in given.items() if (f := _field(k, v)) is not None}
    if not fields:
        raise ValueError("a title, author, narrator or series is required")
    if not _matchable(*(f[0] for f in fields.values())):
        return []

    drive = next((f for f in _ENTITY_FIELDS if f in fields), None)
    if "title" not in fields:
        statement, params = _book_query(fields, region, language, limit, drive)
        rows = await _rows(session, statement, params)
    elif drive is None:
        value, cjk = fields["title"]
        statement, params = _book_query(fields, region, language, limit, None)
        rows = None
        if not cjk:
            _, estimate = await _explain(session, statement, params)
            if estimate >= POPULARITY_MIN_ESTIMATE:
                quick, quick_params = _popular_query(value, region, language, limit)
                rows = await _rows(session, quick, quick_params)
                if len(rows) < limit:
                    rows = None
        if rows is None:
            rows = await _rows(session, statement, params)
    else:
        shapes = [
            _book_query(fields, region, language, limit, None),
            _book_query(fields, region, language, limit, drive),
        ]
        costs = [(await _explain(session, s, p))[0] for s, p in shapes]
        statement, params = shapes[costs.index(min(costs))]
        rows = await _rows(session, statement, params)

    return await _hydrate_books(session, [(r.asin, r.region) for r in rows])


async def _hydrate_books(session: AsyncSession, keys: list[tuple[str, str]]) -> list[dict[str, Any]]:
    if not keys:
        return []
    result = await session.execute(
        select(Book).where(tuple_(Book.asin, Book.region).in_(keys)).options(*BOOK_RELATIONS)
    )
    books = {(b.asin, b.region): b for b in result.scalars().all()}
    positions = await series_positions_by_book(session, list(books.values()))
    return [book_to_dict(books[k], positions.get(k, {})) for k in keys if k in books]


async def search_authors_ranked(
    session: AsyncSession,
    name: str,
    *,
    region: str | None = None,
    exact: bool = False,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Authors whose name matches, best first. Rows with no ASIN are left out:
    the record is keyed by it. Without a region, an ASIN is answered by its
    first-stored row, so only that row's name is searched. Every row of a
    result's (asin, region) is merged, as get_author does."""
    await _prepare(session, limit)
    value, cjk = _field("name", name) or ("", False)
    if not _matchable(value):
        return []
    statement, params = _entity_query("authors", "a", "name", value, cjk, exact, region, limit, "asin")
    # Best rank first, so the first sighting of an (asin, region) is its best.
    keys = list(dict.fromkeys((r.k, r.region) for r in await _rows(session, statement, params)))
    if not keys:
        return []
    result = await session.execute(
        select(Author)
        .where(tuple_(Author.asin, Author.region).in_(keys))
        .options(selectinload(Author.genres))
        .order_by(Author.id)
    )
    rows: dict[tuple[str, str], list[Author]] = {}
    for a in result.scalars().all():
        rows.setdefault((a.asin, a.region), []).append(a)
    return [merge_author_rows(rows[k]) for k in keys if k in rows]


async def search_series_ranked(
    session: AsyncSession,
    name: str,
    *,
    region: str | None = None,
    exact: bool = False,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Series whose title matches, best first, each with the number of stored
    books linked to it. Without a region, an ASIN is answered by its primary
    row, so only that row's title is searched."""
    await _prepare(session, limit)
    value, cjk = _field("series", name, strip=not exact) or ("", False)
    if not _matchable(value):
        return []
    statement, params = _entity_query("series", "s", "title", value, cjk, exact, region, limit, "asin")
    keys = [(r.k, r.region) for r in await _rows(session, statement, params)]
    if not keys:
        return []
    found = await session.execute(select(Series).where(tuple_(Series.asin, Series.region).in_(keys)))
    series = {(s.asin, s.region): s for s in found.scalars().all()}
    counted = await session.execute(
        select(book_series.c.series_asin, book_series.c.series_region, func.count())
        .where(tuple_(book_series.c.series_asin, book_series.c.series_region).in_(keys))
        .group_by(book_series.c.series_asin, book_series.c.series_region)
    )
    counts = {(asin, region): n for asin, region, n in counted.all()}
    return [
        {**series_to_dict(s), "bookCount": counts.get(k, 0)}
        for k, s in ((k, series[k]) for k in keys if k in series)
    ]
