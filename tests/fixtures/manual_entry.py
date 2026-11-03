"""
One hand-entered book in the shapes the entry tests share: the request body,
the writer dict the route builds from it, and a snapshot of every table the
write can touch so "nothing changed" is a comparison and not a count.
"""

# Standard library
import copy
from datetime import datetime, timezone

# Third party
from sqlalchemy import select, text

# Local
from app.core.ingest_models import BOOK_FIELDS, CHAPTERS_FIELDS
from libex_core.storage.models import Author, Book, Series, Track

ASIN = "B0MANUAL01"
AUTHOR_ASIN = "B000APF21M"
SERIES_ASIN = "B00SERIES1"
GENRE_ASIN = "18574426011"

FULL_BOOK = {
    "asin": ASIN, "region": "us", "title": "Delisted", "subtitle": "Sub",
    "description": "d", "summary": "s", "extendedProductDescription": "e",
    "publisher": "p", "copyright": "c", "isbn": "9780000000001", "language": "english",
    "rating": 4.5, "numRatings": 10, "numReviews": 3, "releaseDate": "2020-01-02",
    "publicationName": "pn", "publicationDatetime": "2020-01-02T03:04:05Z",
    "lengthMinutes": 600, "explicit": True, "whisperSync": True, "hasPdf": True,
    "imageUrl": "https://img.example/a.jpg", "bookFormat": "unabridged",
    "contentType": "Product", "contentDeliveryType": "SinglePartBook",
    "episodeNumber": "1", "episodeType": "full", "sku": "SK1", "skuGroup": "SG1",
    "isListenable": True, "isBuyable": True, "isVvab": True, "plans": ["Plus"],
    "productState": "AVAILABLE", "audibleExtras": {"a": 1}, "extrasWithheld": {"b": 2},
    "authors": [{"name": "A", "asin": "B000APF21M", "description": "x", "image": "https://img.example/p.jpg"}],
    "narrators": [{"name": "N"}],
    "series": [{
        "asin": "B00SERIES1", "name": "S", "position": "1", "region": "us",
        "description": "sd", "audibleExtras": {"c": 1}, "extrasWithheld": {"d": 1},
    }],
    "genres": [{"asin": "18574426011", "name": "G", "type": "Genres"}],
}
FULL_CHAPTERS = {
    "runtimeLengthMs": 60000, "runtimeLengthSec": 60, "brandIntroDurationMs": 1,
    "brandOutroDurationMs": 2, "isAccurate": True, "contentReference": {"r": 1},
    "contentUrl": {"u": 1}, "audibleExtras": {"e": 1}, "extrasWithheld": {"w": 1},
    "chapters": [{
        "title": "One", "lengthMs": 1000, "startOffsetMs": 0, "startOffsetSec": 0,
        "audibleExtras": {"x": 1},
        "chapters": [{"title": "Sub", "lengthMs": 500, "startOffsetMs": 0}],
    }],
}

TABLES = (
    "books", "authors", "narrators", "series", "genres", "tracks",
    "author_book", "book_narrator", "book_series", "book_genre",
)

BODY = {
    "book": {
        "asin": ASIN, "region": "us", "title": "Delisted", "language": "english",
        "releaseDate": "2020-01-02", "description": "typed in by hand",
        "imageUrl": "https://img.example/a.jpg", "lengthMinutes": 90,
        "authors": [
            {"name": "First Author", "asin": AUTHOR_ASIN, "description": "bio", "image": "https://img.example/p.jpg"},
            {"name": "Second Author"},
        ],
        "narrators": [{"name": "Zed Voice"}, {"name": "Amy Voice"}],
        "series": [{"asin": SERIES_ASIN, "name": "The Series", "position": "2", "description": "about"}],
        "genres": [{"asin": GENRE_ASIN, "name": "Fantasy", "type": "Genres"}],
    },
    "chapters": {
        "runtimeLengthMs": 5400000,
        "chapters": [
            {"title": "One", "lengthMs": 2700000, "startOffsetMs": 0},
            {"title": "Two", "lengthMs": 2700000, "startOffsetMs": 2700000},
        ],
    },
}


def body(**book_over) -> dict:
    out = copy.deepcopy(BODY)
    out["book"].update(book_over)
    return out


def writer_dict(**over) -> dict:
    """The book as the route hands it to enter_book (flags defaulted, stamp set)."""
    book = {**copy.deepcopy(BODY["book"]), "isListenable": False, "isBuyable": False,
            "isVvab": False, "manualEntry": True}
    book.update(over)
    return book


def chapters_dict() -> dict:
    return copy.deepcopy(BODY["chapters"]) | {
        "runtimeLengthSec": 0, "brandIntroDurationMs": 0, "brandOutroDurationMs": 0, "isAccurate": False,
    }


async def snapshot(session) -> dict[str, list[str]]:
    """Every row of every table a book entry can write, as sorted text."""
    out = {}
    for table in TABLES:
        rows = (await session.execute(text(f"SELECT * FROM {table}"))).all()  # noqa: S608
        out[table] = sorted(repr(tuple(r)) for r in rows)
    return out


async def counts(session) -> dict[str, int]:
    return {t: len(rows) for t, rows in (await snapshot(session)).items()}


def _utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


# Values the writer parses on the way in; every other key is stored as sent.
PARSED = {
    "releaseDate": _utc(2020, 1, 2),
    "publicationDatetime": _utc(2020, 1, 2, 3, 4, 5),
}


async def assert_full_round_trip(session, asin: str = ASIN, region: str = "us") -> None:
    """
    Every accepted key of FULL_BOOK and FULL_CHAPTERS reads back from the
    column or pivot BOOK_FIELDS says it lands in, with the value sent.
    """
    book = (await session.execute(select(Book).filter_by(asin=asin, region=region))).scalar_one()
    for key, target in BOOK_FIELDS.items():
        if not target.startswith("books."):
            continue
        column = target.split(".", 1)[1]
        expected = PARSED.get(key, FULL_BOOK[key])
        assert getattr(book, column) == expected, key

    authors = (await session.execute(
        select(Author).where(Author.region == region).order_by(Author.id))).scalars().all()
    assert [(a.name, a.asin, a.description, a.image) for a in authors] == [
        ("A", "B000APF21M", "x", "https://img.example/p.jpg")]
    series = (await session.execute(select(Series).filter_by(region=region))).scalar_one()
    sent = FULL_BOOK["series"][0]
    assert (series.asin, series.title, series.description, series.audible_extras,
            series.extras_withheld) == (sent["asin"], sent["name"], sent["description"],
                                       sent["audibleExtras"], sent["extrasWithheld"])

    track = (await session.execute(select(Track).filter_by(asin=asin, region=region))).scalar_one()
    assert set(CHAPTERS_FIELDS) <= set(track.chapters)
    assert track.chapters == FULL_CHAPTERS


def stable(value):
    """A read-back without what a second write is bound to change: sequence ids and write times."""
    if isinstance(value, dict):
        return {k: stable(v) for k, v in value.items() if k not in ("id", "updatedAt", "createdAt")}
    if isinstance(value, list):
        return [stable(v) for v in value]
    return value
