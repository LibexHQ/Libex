"""
Every hosted reader answers a failed read the same way: one warning naming the
operation, carrying the arguments it is allowed to name and the failure fields,
and the fallback value its callers rely on.

Table-driven over the whole list, so a dropped extra, a changed message or a
changed fallback fails here. The search and filter text a caller sends is never
in the table's extras; test_db_error_logging.py covers that side.
"""

# Standard library
from unittest.mock import AsyncMock, MagicMock, patch

# Third party
import pytest

# Local
import app.services.db.reader as reader

_PLAIN = {"error_type": "RuntimeError", "sqlstate": None, "schema_name": None,
          "table_name": None, "column_name": None, "constraint_name": None}
_AUTHOR = {"error_type": "RuntimeError", "error": "boom"}
_TYPE_ONLY = {"error_type": "RuntimeError"}

# (wrapper, positional args, message, expected extras, fallback)
CASES = [
    ("get_book_from_db", ("B1",), "DB read failed for book", {"asin": "B1", **_PLAIN}, None),
    ("get_books_from_db", (["B1", "B2"],), "DB read failed for books", {"asins": ["B1", "B2"], **_PLAIN}, []),
    ("search_books_from_db", (), "DB search failed for books", _PLAIN, []),
    ("get_books_by_sku_from_db", ("SG1",), "DB read failed for sku_group", {"sku_group": "SG1", **_PLAIN}, []),
    ("get_distinct_plans_from_db", (), "DB read failed for distinct plans", _PLAIN, []),
    ("get_distinct_genres_from_db", (), "DB read failed for distinct genres", _PLAIN, []),
    ("get_books_by_plan_from_db", ("Plus",), "DB read failed for plan", {"plan_name": "Plus", **_PLAIN}, []),
    ("get_vvab_books_from_db", (), "DB read failed for VVAB books", _PLAIN, []),
    ("get_new_releases_from_db", (), "DB read failed for new releases", _PLAIN, []),
    ("get_coming_soon_from_db", (), "DB read failed for coming soon", _PLAIN, []),
    ("get_track_from_db", ("B1",), "DB read failed for track", {"asin": "B1", **_PLAIN}, None),
    ("get_author_from_db", ("A1", "uk"), "DB read failed for author",
     {"asin": "A1", "region": "uk", **_AUTHOR}, None),
    ("get_author_book_asins_from_db", ("A1", "uk"), "DB read failed for author book asins",
     {"author_asin": "A1", "region": "uk", **_TYPE_ONLY}, None),
    ("get_author_books_from_db", ("A1", "uk"), "DB read failed for author books",
     {"author_asin": "A1", **_PLAIN}, []),
    ("search_narrators_from_db", ("secret",), "DB read failed for narrator search", _PLAIN, []),
    ("get_narrator_books_from_db", ("secret",), "DB read failed for narrator books", _PLAIN, []),
    ("get_series_from_db", ("S1",), "DB read failed for series", {"asin": "S1", **_PLAIN}, None),
    ("search_series_from_db", ("secret",), "DB search failed for series", _PLAIN, []),
    ("get_series_books_from_db", ("S1",), "DB read failed for series books",
     {"series_asin": "S1", **_PLAIN}, []),
]


def _failing_session():
    session = MagicMock()
    boom = RuntimeError("boom")
    for method in ("execute", "scalar", "scalars", "get", "stream"):
        setattr(session, method, AsyncMock(side_effect=boom))
    return session


def test_the_table_lists_every_guarded_reader():
    guarded = {
        name for name in reader.__all__
        if name.endswith("_from_db") and hasattr(getattr(reader, name), "__wrapped__")
    }
    assert {case[0] for case in CASES} == guarded


@pytest.mark.asyncio
@pytest.mark.parametrize("name,args,message,extra,fallback", CASES, ids=[c[0] for c in CASES])
async def test_a_failed_read_logs_its_operation_and_answers_the_fallback(
    name, args, message, extra, fallback
):
    with patch.object(reader, "logger") as log:
        result = await getattr(reader, name)(_failing_session(), *args)

    assert result == fallback
    assert type(result) is type(fallback)
    log.warning.assert_called_once_with(message, extra=extra)
