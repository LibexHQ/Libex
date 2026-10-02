"""
One seeded catalog and one fixed list of reader calls, run unchanged against
SQLite and Postgres so the two can be compared answer for answer.
"""

# Standard library
from datetime import datetime, timedelta, timezone

# Third party
from sqlalchemy import insert, select

# Local
from libex_core.storage import read
from libex_core.storage.base import Base
from libex_core.storage.models import (
    CORE_TABLES,
    Author,
    Book,
    Genre,
    Narrator,
    Series,
    Track,
    author_book,
    author_genre,
    book_genre,
    book_narrator,
    book_series,
)
from libex_core.storage.read import books, people, series, stats
from libex_core.storage.read._compat import NumericPosition

assert read  # the package docstring is the contract; the import keeps it loaded

STAMP = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

# Release dates are offsets from one instant fixed when the module loads, so a
# second seeding in the same run stores exactly the same values.
NOW = datetime.now(timezone.utc).replace(microsecond=0)


def core_tables():
    return [t for t in Base.metadata.sorted_tables if t.name in CORE_TABLES]


def _book(asin, title, **kw):
    fields = dict(
        asin=asin, title=title, region="us", created_at=STAMP, updated_at=STAMP,
        explicit=False, whisper_sync=False, has_pdf=False, is_listenable=True,
        is_buyable=True, is_vvab=False,
    )
    fields.update(kw)
    return Book(**fields)


async def seed(session, *, odd_plans: bool = True) -> None:
    """Rows chosen so every filter, sort and fallback in the readers has
    something to select and something to leave out.

    B000000014 carries plans that are not all strings on purpose: the readers'
    plan listing renders each element as text on both backends, and that is
    pinned. A book like that cannot be put through BookResponse, whose plans
    are strings, so anything that returns books whole (the hosted routes, the
    db commands) must seed with odd_plans=False, which stores a clean list."""
    now = NOW
    day = timedelta(days=1)
    session.add_all([
        _book("B000000001", "the first quest", rating=4.5, length_minutes=600,
              release_date=now - 5 * day, language="english", publisher="Orbit",
              copyright="(c) 2020 Alpha", isbn="9780000000001", book_format="unabridged",
              content_type="Product", content_delivery_type="SinglePartBook",
              sku="SK1", sku_group="SG1", plans=["Plus", "Premium"],
              description="A long description", summary="Sum one",
              audible_extras={"a": 1, "b": {"c": [1, 2]}}, extras_withheld={"k": ["x"]},
              publication_datetime=datetime(2020, 5, 1, 7, 30, tzinfo=timezone.utc),
              num_ratings=10, num_reviews=3, product_state="AVAILABLE",
              image="http://img/1", whisper_sync=True),
        _book("B000000002", "The Second Quest", rating=3.0, length_minutes=90,
              release_date=now - 40 * day, language="english", publisher="Tor",
              plans=["Plus"], sku="SK2", sku_group="SG1", is_vvab=True,
              explicit=True, has_pdf=True, content_type="Product"),
        _book("B000000003", "third wind", rating=None, length_minutes=None,
              release_date=now + 10 * day, language="german", region="de",
              plans=[], is_buyable=False, content_type="Podcast",
              episode_number="4", episode_type="full"),
        _book("B000000004", "Fourth Light", rating=5.0, length_minutes=300,
              release_date=now + 60 * day, language="english",
              is_listenable=False, region="uk"),
        _book("B000000005", "Émile et les Détectives", rating=2.5, length_minutes=45,
              release_date=None, language="french", region="fr", plans=["Premium"],
              is_vvab=True, publisher="Éditions Rouge"),
        _book("B000000006", "ACDC live", length_minutes=100, plans=["Plus"],
              rating=1.1, release_date=now - 106 * day),
        _book("B000000007", "AC\\DC story", length_minutes=110, plans=["Plus"],
              rating=1.2, release_date=now - 107 * day),
        _book("B000000008", "100% pure_gold", length_minutes=120,
              release_date=now - 2 * day, plans=["Free"], rating=1.3),
        _book("B000000009", "Series Nine", length_minutes=130,
              rating=1.4, release_date=now - 108 * day),
        _book("B000000010", "Series Ten", length_minutes=140,
              rating=1.5, release_date=now - 109 * day),
        _book("B000000011", "Series Eleven", length_minutes=150,
              rating=1.6, release_date=now - 110 * day),
        _book("B000000012", "Series Half", length_minutes=160,
              rating=1.7, release_date=now - 111 * day),
        _book("B000000013", "Series Odd", length_minutes=170,
              rating=1.8, release_date=now - 112 * day),
        _book("B000000014", "Series Word", length_minutes=180,
              plans=["Premium", 1, True] if odd_plans else ["Premium"],
              rating=1.9, release_date=now - 113 * day),
        _book("B000000015", "Series Nothing", length_minutes=190,
              rating=2.0, release_date=now - 114 * day),
    ])
    session.add_all([
        Author(id=1, asin="A000000001", name="Ann Author", region="us", description="short",
               image=None, created_at=STAMP, updated_at=STAMP, fetched_description=False),
        Author(id=2, asin="A000000001", name="Ann Q. Author", region="us",
               description="a much longer description wins", image="http://a/2",
               created_at=STAMP, updated_at=STAMP + timedelta(days=3), fetched_description=True),
        Author(id=3, asin="A000000003", name="Bob Writer", region="us", description=None,
               image="http://b", created_at=STAMP, updated_at=STAMP, fetched_description=False),
        Author(id=4, asin="A000000004", name="Émilie Écrivain", region="fr", description=None,
               image=None, created_at=STAMP, updated_at=STAMP, fetched_description=False),
    ])
    session.add_all([
        Genre(asin="G1", name="Science Fiction & Fantasy", type="Genres", created_at=STAMP, updated_at=STAMP),
        Genre(asin="G2", name="Fantasy", type="Genres", created_at=STAMP, updated_at=STAMP),
        Genre(asin="G3", name="Epic", type="Tags", created_at=STAMP, updated_at=STAMP),
        Genre(asin="G4", name="Mystery", type="Genres", created_at=STAMP, updated_at=STAMP),
    ])
    session.add_all([
        Narrator(name="Nina Voice", gender="female", source="Wiki", cultural_heritage="Irish",
                 languages={"English": 5, "Irish": 1}, audiobooks_produced="1 to 10",
                 genres_narrated=["Fantasy"], social_links={"x": "y"},
                 source_updated_at=datetime(2025, 3, 4, tzinfo=timezone.utc),
                 created_at=STAMP, updated_at=STAMP),
        Narrator(name="Oscar Reader", gender="male", source="Other", languages={"French": 2},
                 audiobooks_produced="More than 100", created_at=STAMP, updated_at=STAMP),
        Narrator(name="Zed", gender=None, source="Zed Src", created_at=STAMP, updated_at=STAMP),
        Narrator(name="Émile Lecteur", gender="Male", source="Alpha", created_at=STAMP, updated_at=STAMP),
    ])
    session.add_all([
        Series(asin="S000000001", title="Quest Saga", region="us", description="saga",
               audible_extras={"x": 1}, created_at=STAMP, updated_at=STAMP),
        Series(asin="S000000002", title="Quest Spinoffs", region=None,
               created_at=STAMP, updated_at=STAMP),
        Series(asin="S000000003", title="Ordering Series", region="us",
               created_at=STAMP, updated_at=STAMP),
        Series(asin="S000000004", title="Awkward Positions", region="us",
               created_at=STAMP, updated_at=STAMP),
    ])
    await session.flush()
    session.add(Track(asin="B000000001", chapters={"chapters": [{"title": "One"}], "n": 1},
                      created_at=STAMP, updated_at=STAMP))
    await session.execute(insert(author_book), [
        {"author_id": 1, "book_asin": "B000000001"}, {"author_id": 2, "book_asin": "B000000001"},
        {"author_id": 2, "book_asin": "B000000002"}, {"author_id": 3, "book_asin": "B000000002"},
        {"author_id": 3, "book_asin": "B000000003"}, {"author_id": 4, "book_asin": "B000000005"},
    ])
    await session.execute(insert(author_genre), [
        {"author_id": 1, "genre_asin": "G1"}, {"author_id": 2, "genre_asin": "G1"},
        {"author_id": 2, "genre_asin": "G3"},
    ])
    await session.execute(insert(book_narrator), [
        {"narrator_name": "Nina Voice", "book_asin": "B000000001"},
        {"narrator_name": "Nina Voice", "book_asin": "B000000002"},
        {"narrator_name": "Oscar Reader", "book_asin": "B000000002"},
        {"narrator_name": "Émile Lecteur", "book_asin": "B000000005"},
    ])
    await session.execute(insert(book_genre), [
        {"book_asin": "B000000001", "genre_asin": "G1"}, {"book_asin": "B000000001", "genre_asin": "G3"},
        {"book_asin": "B000000002", "genre_asin": "G2"}, {"book_asin": "B000000003", "genre_asin": "G4"},
    ])
    positions = [
        ("B000000001", "S000000001", "1"), ("B000000002", "S000000001", "2"),
        ("B000000009", "S000000003", "2"), ("B000000010", "S000000003", "10"),
        ("B000000011", "S000000003", "11"), ("B000000012", "S000000003", "1.5"),
        ("B000000013", "S000000003", "1-3"), ("B000000014", "S000000003", "Book 1"),
        ("B000000015", "S000000003", None), ("B000000003", "S000000003", "007"),
        ("B000000008", "S000000003", "0"),
        ("B000000004", "S000000004", "3."), ("B000000005", "S000000004", ".5"),
        ("B000000006", "S000000004", "1.2.3"), ("B000000007", "S000000004", ""),
        ("B000000009", "S000000004", "1e3"), ("B000000010", "S000000004", "12abc"),
        ("B000000011", "S000000004", "٣"), ("B000000012", "S000000004", "1\n"),
        ("B000000003", "S000000002", "1"),
    ]
    await session.execute(insert(book_series), [
        {"book_asin": b, "series_asin": s, "position": p} for b, s, p in positions
    ])
    await session.commit()


async def _numeric_positions(session):
    """What the position classifier makes of each stored position, which is
    the part of the series ordering that can be compared exactly."""
    rows = await session.execute(
        select(book_series.c.position, NumericPosition(book_series.c.position))
    )
    return sorted(
        ((p, None if v is None else float(v)) for p, v in rows.all()),
        key=lambda pair: (pair[0] is None, pair[0] or ""),
    )


def normalize(value):
    """Order-insensitive where the database gives no order: the relationship
    lists inside a book come back in whatever order the backend scans."""
    if isinstance(value, list):
        return [normalize(v) for v in value]
    if isinstance(value, dict):
        out = {k: normalize(v) for k, v in value.items()}
        for key in ("authors", "narrators", "genres", "series"):
            if isinstance(out.get(key), list):
                out[key] = sorted(out[key], key=repr)
        return out
    return value


# (name, call, ordered). ordered=False compares the rows as a set by asin: the
# query has no ORDER BY, so each backend is free to return them its own way.
def calls():
    c = []

    def add(name, fn, ordered=False):
        c.append((name, fn, ordered))

    add("get_book", lambda s: books.get_book(s, "B000000001"), True)
    add("get_book_podcast", lambda s: books.get_book(s, "B000000003"), True)
    add("get_book_missing", lambda s: books.get_book(s, "NOPE"), True)
    add("get_books", lambda s: books.get_books(s, ["B000000002", "B000000001", "NOPE"]))
    add("get_books_empty", lambda s: books.get_books(s, []))
    add("by_sku", lambda s: books.get_books_by_sku(s, "SG1"))
    add("by_sku_missing", lambda s: books.get_books_by_sku(s, "NO"))
    add("distinct_plans", lambda s: books.distinct_plans(s), True)
    add("distinct_genres", lambda s: books.distinct_genres(s), True)
    add("distinct_genres_search", lambda s: books.distinct_genres(s, "fanta"), True)
    add("track", lambda s: books.get_track(s, "B000000001"), True)
    add("track_missing", lambda s: books.get_track(s, "B000000002"), True)

    for label, kw in {
        "title": {"title": "QUEST"},
        "title_percent": {"title": "100%"},
        "title_underscore": {"title": "pure_g"},
        "title_backslash": {"title": "AC\\DC"},
        "title_accent": {"title": "émile"},
        "publisher_accent": {"publisher": "éditions"},
        "region": {"region": "de"},
        "language": {"language": "english"},
        "ratings": {"rating_better_than": 3.0, "rating_worse_than": 4.5},
        "lengths": {"longer_than": 90, "shorter_than": 600},
        "booleans": {"explicit": True, "has_pdf": True},
        "whisper": {"whisper_sync": True},
        "listenable": {"is_listenable": False},
        "buyable": {"is_buyable": False},
        "vvab": {"is_vvab": True},
        "format": {"book_format": "unabridged", "content_type": "Product"},
        "delivery": {"content_delivery_type": "SinglePartBook"},
        "isbn": {"isbn": "0000001"},
        "copyright": {"copyright": "alpha"},
        "description": {"description": "LONG desc"},
        "summary": {"summary": "sum"},
        "subtitle_none": {"subtitle": "x"},
        "author_name": {"author_name": "ann"},
        "author_accent": {"author_name": "émilie"},
        "series_name": {"series_name": "quest"},
        "genre": {"genre": "fantasy"},
        "category": {"category": "G3, G4"},
        "plan": {"plan_name": "Plus"},
        "plan_absent": {"plan_name": "Nope"},
        "combined": {"title": "quest", "language": "english", "rating_better_than": 3.0},
    }.items():
        add(f"search_{label}", lambda s, kw=kw: books.search_books(s, **kw))

    for field in ("title", "releaseDate", "rating", "lengthMinutes", "language", "publisher", "updatedAt"):
        for order in ("asc", "desc"):
            if field in ("title", "language", "publisher", "updatedAt"):
                continue
            add(f"search_sort_{field}_{order}",
                lambda s, f=field, o=order: books.search_books(s, sort=f, order=o, limit=50), True)
    add("search_page", lambda s: books.search_books(s, sort="lengthMinutes", order="asc", limit=3, page=2), True)
    add("search_page_past_end", lambda s: books.search_books(s, sort="lengthMinutes", limit=3, page=99), True)

    add("by_plan", lambda s: books.get_books_by_plan(s, "Plus"))
    add("by_plan_number_is_not_its_digits", lambda s: books.get_books_by_plan(s, "1"))
    add("by_plan_bool_is_not_its_name", lambda s: books.get_books_by_plan(s, "true"))
    add("by_plan_filtered", lambda s: books.get_books_by_plan(s, "Plus", language="english"))
    add("by_plan_sorted", lambda s: books.get_books_by_plan(s, "Plus", sort="lengthMinutes", order="desc"), True)
    add("vvab", lambda s: books.get_vvab_books(s))
    add("vvab_sorted", lambda s: books.get_vvab_books(s, sort="rating", order="desc"), True)
    add("new_releases", lambda s: books.get_new_releases(s, days=30), True)
    add("new_releases_wide", lambda s: books.get_new_releases(s, days=90), True)
    add("new_releases_sorted", lambda s: books.get_new_releases(s, days=90, sort="lengthMinutes", order="asc"), True)
    add("coming_soon", lambda s: books.get_coming_soon(s, days=30), True)
    add("coming_soon_wide", lambda s: books.get_coming_soon(s, days=90), True)

    add("author_merged", lambda s: people.get_author(s, "A000000001", "us"), True)
    add("author_single", lambda s: people.get_author(s, "A000000003", "us"), True)
    add("author_wrong_region", lambda s: people.get_author(s, "A000000003", "de"), True)
    add("author_asins", lambda s: people.get_author_book_asins(s, "A000000001", "us"))
    add("author_books", lambda s: people.get_author_books(s, "A000000001", "us", sort="lengthMinutes", order="asc"), True)
    add("author_books_filtered", lambda s: people.get_author_books(s, "A000000003", "us", language="english"))
    add("search_narrators", lambda s: people.search_narrators(s, "nina"), True)
    add("search_narrators_accent", lambda s: people.search_narrators(s, "émile"), True)
    add("search_narrators_all", lambda s: people.search_narrators(s, "", sort="source", order="asc"), True)
    add("narrators_gender", lambda s: people.search_narrators(s, "", gender="male", sort="source"), True)
    add("narrators_language_key", lambda s: people.search_narrators(s, "", language="English", sort="source"), True)
    add("narrators_language_missing", lambda s: people.search_narrators(s, "", language="Klingon"), True)
    add("narrators_bucket", lambda s: people.search_narrators(s, "", audiobooks_produced="1 to 10"), True)
    add("narrators_source_heritage", lambda s: people.search_narrators(s, "", source="wiki", cultural_heritage="irish"), True)
    add("narrators_sorted_nulls", lambda s: people.search_narrators(s, "", sort="source", order="desc"), True)
    add("narrator_books", lambda s: people.get_narrator_books(s, "Nina Voice"))
    add("narrator_books_filtered", lambda s: people.get_narrator_books(s, "Nina Voice", language="english", sort="rating", order="desc"), True)

    add("series", lambda s: series.get_series(s, "S000000001"), True)
    add("series_missing", lambda s: series.get_series(s, "NOPE"), True)
    add("search_series", lambda s: series.search_series(s, "quest"))
    add("series_books_position_order", lambda s: series.get_series_books(s, "S000000003"), True)
    # Every awkward position must be read as non-numeric; their order among
    # themselves is a text sort and follows each backend's collation, so only
    # the set is compared here.
    add("series_books_awkward", lambda s: series.get_series_books(s, "S000000004"))
    add("numeric_positions", _numeric_positions)
    add("series_books_sorted", lambda s: series.get_series_books(s, "S000000003", sort="lengthMinutes", order="desc"), True)
    add("series_books_filtered", lambda s: series.get_series_books(s, "S000000001", language="english"), True)

    add("count_global", lambda s: stats.count_stored(s), True)
    add("count_us", lambda s: stats.count_stored(s, "us"), True)
    add("count_fr", lambda s: stats.count_stored(s, "fr"), True)
    return c


async def run_all(session):
    """Every call's answer, keyed by name, in a form two backends can be
    compared on."""
    out = {}
    for name, fn, ordered in calls():
        result = normalize(await fn(session))
        if not ordered and isinstance(result, list):
            result = sorted(result, key=_identity)
        out[name] = result
    return out


def _identity(row):
    if isinstance(row, (list, tuple)):
        return repr(row)
    if isinstance(row, dict):
        return row.get("asin") or row.get("name") or repr(row)
    return row
