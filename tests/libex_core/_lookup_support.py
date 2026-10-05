"""
Shared stand-ins for the libex_core.lookup tests: a fake Audible `get` that
answers every endpoint the lookups use from one small catalogue, and the
helpers that build products and batch answers. Nothing here touches a network.
"""

# Standard library
import itertools
from datetime import datetime, timedelta, timezone

# Local
from libex_core.audible.releases import flatten_genre_nodes
from libex_core.exceptions import AudibleAPIException, NotFoundException
from libex_core.lookup import author_books, series

AUTHOR = "B000AUTHOR"
AUTHOR_NAME = "Jane Test"
SERIES = "B0SERIES01"
NO_NAME_AUTHOR = "B0NONAME00"
PLANTED = "Zq9-distinctive-typed-text"

NOW = datetime.now(timezone.utc)

CATEGORY_TREE = {
    "categories": [
        {"id": "1", "name": "Zed", "children": [{"id": "3", "name": "Kid"}]},
        {"id": "2", "name": "Alpha"},
    ]
}
STORED_GENRES = flatten_genre_nodes(CATEGORY_TREE)


def product(asin, days=10, rating=4.0, length=100, **extra):
    """A catalog product released `days` ago (negative: still to come) by the
    test author, with a rating and a runtime to filter and sort on."""
    released = (NOW - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "asin": asin,
        "title": f"Title {asin}",
        "publication_datetime": released,
        "release_date": released[:10],
        "authors": [{"asin": AUTHOR, "name": AUTHOR_NAME}],
        "rating": {"overall_distribution": {"display_average_rating": str(rating)}},
        "runtime_length_min": length,
        "language": "english",
        **extra,
    }


# Four released books, spread over date, rating and length so each sort and
# filter tells them apart, and one still to come.
BOOKS = {
    f"B0SCR{i:05d}": product(
        f"B0SCR{i:05d}", days=10 + i * 7, rating=3 + i * 0.5, length=100 * (i + 1)
    )
    for i in range(4)
}
FUTURE = {"B0FUT00001": product("B0FUT00001", days=-5)}
CATALOGUE = {**FUTURE, **BOOKS}


def series_record():
    return {
        "response_groups": ["product_attrs", "product_desc", "relationships"],
        "product": {
            "asin": SERIES,
            "title": "The Series",
            "relationships": [
                {
                    "relationship_to_product": "child",
                    "relationship_type": "series",
                    "asin": asin,
                    "sort": str(i + 1),
                }
                for i, asin in enumerate(BOOKS)
            ],
        }
    }


async def fake_get(region, path, params=None, extra_headers=None):
    """Answers every endpoint the lookups use. The second series and the
    no-name author are the two a lookup finds by search and then fails or
    comes up empty on."""
    params = params or {}
    if "contributors/" in path:
        if path.endswith(NO_NAME_AUTHOR):
            raise NotFoundException("x")
        return {
            "contributor": {
                "name": AUTHOR_NAME,
                "bio": "<b>Bio</b>",
                "profile_image_url": "http://i",
            }
        }
    if "searchsuggestions" in path:
        return {"model": {"items": [
            {"view": {"template": "AuthorItemV2"},
             "model": {"person_metadata": {"asin": AUTHOR}}},
            {"view": {"template": "AuthorItemV2"},
             "model": {"person_metadata": {"asin": NO_NAME_AUTHOR}}},
        ]}}
    if "screens" in path:
        return {"nothing": True}
    if "categories" in path:
        return CATEGORY_TREE
    if "/series/" in path or path.endswith(SERIES):
        if path.endswith("B0SERIES02"):
            raise NotFoundException("gone")
        return series_record()
    if "asins" in params:
        return {"products": [
            CATALOGUE[a] if a in CATALOGUE else {"asin": a}
            for a in params["asins"].split(",")
        ]}
    if "relationships" in params.get("response_groups", "") and "title" in params \
            and "category_id" not in params:
        return {"products": [{"asin": "B0SRCH0001", "title": "t", "relationships": [
            {"relationship_type": "series", "asin": SERIES},
            {"relationship_type": "series", "asin": "B0SERIES02"},
        ]}]}
    if "author" in params:
        page = params.get("page", 0)
        return {
            "total_results": len(BOOKS),
            "products": [product(a) for a in BOOKS] if page == 0 else [],
        }
    if "products_sort_by" in params:
        page = int(params.get("page", 0))
        return {"products": list(CATALOGUE.values()) if page == 0 else []}
    return {"products": [product(a) for a in BOOKS]}


async def not_found_get(region, path, params=None, extra_headers=None):
    raise NotFoundException("nothing here")


async def outage_get(region, path, params=None, extra_headers=None):
    raise AudibleAPIException("boom", upstream_status=503)


async def empty_get(region, path, params=None, extra_headers=None):
    return {"products": [], "categories": [], "total_results": 0}


def asins(count, prefix="B0LOK"):
    return [f"{prefix}{i:05d}" for i in range(count)]


def tick_walk_clock(monkeypatch):
    """Makes the time a walk is stamped with strictly later on every call, so a
    second walk is always newer than the first and no test depends on the wall
    clock having moved between two back-to-back walks."""
    start = datetime.now(timezone.utc)
    ticks = itertools.count(1)

    class _Ticking(datetime):
        @classmethod
        def now(cls, tz=None):
            return start + timedelta(seconds=next(ticks))

    for module in (series, author_books):
        monkeypatch.setattr(module, "datetime", _Ticking)
