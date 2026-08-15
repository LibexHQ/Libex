"""
Guards the privacy contract on the Audible search log lines.

Libex records nothing that reveals what a caller was looking for. The search
service logs which fields a consumer searched on, how long the query was and
how the compound parser split it -- never the text itself. Title, author,
narrator, publisher and keywords are all caller-authored, and one of them on a
log line is a record of what someone searched for.

Both paths are driven with distinctive strings and every record emitted during
the call is checked, not only the line the change touched: a leak on any line
is the same leak.
"""

# Standard library
import logging
from unittest.mock import AsyncMock, MagicMock, patch

# Third party
import pytest

# Local
from app.services.audible.search import quick_search, search


# Distinctive enough that a substring check cannot pass by accident.
SECRETS = ["Zorbleflux", "Quennathrix", "Vandermoot", "Prellingsby"]


def _records(caplog):
    return [r for r in caplog.records if r.levelno == logging.INFO]


def _serialised(caplog):
    return " ".join(str(r.__dict__) for r in _records(caplog))


def _named(caplog, message):
    return [r for r in _records(caplog) if r.getMessage() == message]


@pytest.mark.asyncio
async def test_search_logs_field_names_never_field_values(caplog):
    with (
        patch("app.services.audible.search.audible_get", new_callable=AsyncMock) as mock_get,
        patch("app.services.audible.search.persist_books_background"),
        caplog.at_level(logging.INFO),
    ):
        mock_get.return_value = {"products": []}
        await search(
            region="us",
            session=MagicMock(),
            title=SECRETS[0],
            author=SECRETS[1],
            narrator=SECRETS[2],
            publisher=SECRETS[3],
        )

    records = _named(caplog, "Requested Audible Search")
    assert len(records) == 1

    # The value, not merely the presence of the field: sorted keys of the
    # params actually sent, with nothing carrying a value alongside it.
    assert records[0].search_fields == [
        "author", "narrator", "num_results", "page", "publisher", "title",
    ]
    assert not hasattr(records[0], "search_params")

    for secret in SECRETS:
        assert secret not in _serialised(caplog)


@pytest.mark.asyncio
async def test_quick_search_logs_keyword_length_never_the_keywords(caplog):
    keywords = SECRETS[0]

    with (
        patch("app.services.audible.search.audible_get", new_callable=AsyncMock) as mock_get,
        patch("app.services.audible.search.get_books_by_asins", new_callable=AsyncMock),
        caplog.at_level(logging.INFO),
    ):
        mock_get.return_value = {"model": {"items": []}}
        await quick_search(keywords=keywords, region="us", session=MagicMock())

    records = _named(caplog, "Requested Audible Quick Search")
    assert len(records) == 1
    assert records[0].keywords_length == len(keywords)
    assert not hasattr(records[0], "keywords")
    assert keywords not in _serialised(caplog)


@pytest.mark.asyncio
async def test_compound_fallback_logs_segment_count_never_the_segments(caplog):
    """The parser's output is the caller's own words split up -- still theirs."""
    keywords = f"{SECRETS[0]} - {SECRETS[1]} - {SECRETS[2]}"

    with (
        patch("app.services.audible.search.audible_get", new_callable=AsyncMock) as mock_get,
        patch("app.services.audible.search.search_books_from_db", new_callable=AsyncMock) as mock_db,
        patch("app.services.audible.search.persist_books_background"),
        caplog.at_level(logging.INFO),
    ):
        mock_get.return_value = {"model": {"items": []}, "products": []}
        mock_db.return_value = []
        await quick_search(keywords=keywords, region="us", session=MagicMock())

    records = _named(caplog, "Quick search compound fallback")
    assert len(records) == 1
    assert records[0].segments_found == 3
    assert records[0].keywords_length == len(keywords)
    for field in ("keywords", "parsed_author", "parsed_title"):
        assert not hasattr(records[0], field)

    for secret in SECRETS[:3]:
        assert secret not in _serialised(caplog)
