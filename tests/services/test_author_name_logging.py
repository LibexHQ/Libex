"""
Guards the privacy contract on the author-name lookup log lines.

/author/books?name= takes verbatim caller-authored text. Libex records nothing
that reveals what a caller was looking for, so that text must not reach a log
record from any layer of the call -- not the service line that reports how the
lookup went, not the warning the catalog walk emits when a page fails, and not
the request line the middleware writes.

Every record emitted during the call is checked, not only the field that was
removed. Asserting one named field is absent is what let the leak exist: it
passes just as happily while the same string rides in on a field nobody
thought to name.

Not every author_name field is a leak. The ASIN-scoped path resolves the name
from Audible's catalogue rather than from a caller, and keeps its field
deliberately -- these tests are scoped to the by-name path and say nothing
about that one.
"""

# Standard library
import logging
from unittest.mock import AsyncMock, MagicMock, patch

# Third party
import pytest

# Local
from app.services.audible.authors import get_author_books_by_name
from app.services.audible.authors.catalog import fetch_author_books_by_name


# Distinctive enough that a substring check cannot pass by accident, and
# shaped like a name so nothing along the path can reject it as malformed.
TYPED_NAME = "Zorbleflux Quennathrix"


# The test client's own loggers echo the URL the test itself just built, name
# and all. Excluding them by name rather than allowlisting Libex's logger keeps
# the check as wide as possible: a Libex record written under some other logger
# is still examined, and a harness logger nobody anticipated fails the test
# rather than slipping past it.
_HARNESS_LOGGERS = ("httpx", "httpcore", "asyncio", "urllib3")


def _libex_records(caplog):
    return [r for r in caplog.records if not r.name.startswith(_HARNESS_LOGGERS)]


def _serialised(caplog):
    return " ".join(str(r.__dict__) for r in _libex_records(caplog))


def _named(caplog, message):
    return [r for r in _libex_records(caplog) if r.getMessage() == message]


@pytest.mark.asyncio
async def test_author_books_by_name_logs_the_lookup_never_the_name(caplog):
    with (
        patch(
            "app.services.audible.authors.fetch_author_books_by_name",
            new_callable=AsyncMock,
        ) as mock_fetch,
        caplog.at_level(logging.INFO),
    ):
        mock_fetch.return_value = (["B08G9PRS1K", "B000APF21M"], 3)
        asins = await get_author_books_by_name(TYPED_NAME, "de", MagicMock())

    assert asins == ["B08G9PRS1K", "B000APF21M"]

    records = _named(caplog, "Requested Audible Author Books By Name")
    assert len(records) == 1
    record = records[0]

    # The values, not merely the presence of the fields: what the line reports
    # has to stay true, or removing the name has cost the operator the answer.
    assert record.author_book_num == 2
    assert record.pages_fetched == 3
    assert record.region == "de"
    assert isinstance(record.author_book_took, float)

    assert not hasattr(record, "author_name")
    assert TYPED_NAME not in _serialised(caplog)
    for word in TYPED_NAME.split():
        assert word not in _serialised(caplog)


@pytest.mark.asyncio
async def test_catalog_partial_harvest_warning_reports_where_not_who(caplog):
    """The by-name walk's failure path: the name is in scope there too."""
    with (
        patch(
            "app.services.audible.authors.catalog._fetch_name_search_page",
            new_callable=AsyncMock,
        ) as mock_page,
        caplog.at_level(logging.INFO),
    ):
        mock_page.side_effect = RuntimeError("upstream exploded")
        await fetch_author_books_by_name(TYPED_NAME, "jp")

    records = _named(
        caplog,
        "Audible Author Books by-name page fetch failed, keeping partial harvest",
    )
    assert len(records) == 1
    record = records[0]

    assert record.region == "jp"
    assert record.page == 0
    assert record.asins_collected == 0
    assert record.error == "RuntimeError: upstream exploded"

    assert not hasattr(record, "author_name")
    assert TYPED_NAME not in _serialised(caplog)
    for word in TYPED_NAME.split():
        assert word not in _serialised(caplog)


@pytest.mark.asyncio
async def test_no_log_record_of_the_whole_request_carries_the_typed_name(caplog, async_client):
    """
    The form that would have caught the leak: drive the real endpoint and
    check every record the request produced, from every layer, for the string
    the caller typed. A per-field assertion passes while the same text sits on
    a neighbouring field; this does not.
    """
    with (
        patch(
            "app.services.audible.authors.fetch_author_books_by_name",
            new_callable=AsyncMock,
        ) as mock_fetch,
        patch(
            "app.api.routes.authors.router.get_books_by_asins",
            new_callable=AsyncMock,
        ) as mock_books,
        caplog.at_level(logging.INFO),
    ):
        mock_fetch.return_value = (["B08G9PRS1K"], 1)
        mock_books.return_value = []
        response = await async_client.get(f"/author/books?name={TYPED_NAME}&region=us")

    assert response.status_code == 200
    assert mock_fetch.await_args.args[0] == TYPED_NAME  # the real name did travel

    assert _libex_records(caplog), "the request produced no log records to check"
    assert TYPED_NAME not in _serialised(caplog)
    for word in TYPED_NAME.split():
        assert word not in _serialised(caplog)

    request_lines = _named(caplog, "Request completed")
    assert len(request_lines) == 1
    assert request_lines[0].query == "name=REDACTED&region=us"
