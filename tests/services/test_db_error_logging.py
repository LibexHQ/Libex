"""
Guards the privacy contract on the DB reader's failure log lines.

Three reader handlers -- narrator search, narrator books and series search --
log a warning naming the operation when their query fails, and deliberately
leave the caller's search text out of the message. Taking it out of the message
is only half the guard: that text travels as a bound parameter of the statement,
and SQLAlchemy renders a statement's bound parameters into str() on any
StatementError. Every DBAPIError, OperationalError and DataError is one, so an
"{e}" on those lines puts back exactly what the message removed.

app/db/session.py closes that engine-wide with hide_parameters=True. These tests
hold both ends of it:

  * the failure path is driven with an error that really CARRIES bound
    parameters. A bare Exception passes here whether the flag is set or not,
    which is why nothing caught this the first time round; and
  * the engine setting those errors are rendered under is asserted directly, so
    deleting the flag fails a test rather than silently reopening the leak.

The middle test is the control: with the flag off, the identical path prints the
caller's text. It is what stops the other assertions passing vacuously if a
future SQLAlchemy stops rendering parameters into the exception at all.
"""

# Standard library
import logging
from unittest.mock import AsyncMock, MagicMock, patch

# Third party
import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import OperationalError, StatementError

# Local
from app.db.models import Narrator, Series
from app.db.session import engine
from app.services.db.reader import (
    get_narrator_books_from_db,
    search_narrators_from_db,
    search_series_from_db,
)


# Distinctive enough that a substring check cannot pass by accident, and with no
# word short enough to appear inside an unrelated one ("q" would match "query").
SECRET = "Zorbleflux Quennathrix"


def _records(caplog):
    return [r for r in caplog.records if r.levelno >= logging.WARNING]


def _serialised(caplog):
    return " ".join(str(r.__dict__) for r in _records(caplog))


def _statement_error(column, *, hide_parameters):
    """
    The error a failing reader query actually raises.

    Compiled from the same ilike the reader builds, against the Libex engine's
    own dialect, so the parameter dict is the real one -- {'name_1': '%...%'}
    carrying the caller's text -- rather than a stand-in that has nothing to
    leak.
    """
    compiled = select(column).where(column.ilike(f"%{SECRET}%")).compile(
        dialect=engine.sync_engine.dialect
    )
    assert SECRET in str(compiled.params), "the fixture carries no bound text to leak"
    return OperationalError(
        str(compiled),
        compiled.params,
        Exception("server closed the connection unexpectedly"),
        hide_parameters=hide_parameters,
    )


def _session_raising(error):
    session = MagicMock()
    session.execute = AsyncMock(side_effect=error)
    return session


@pytest.mark.asyncio
@pytest.mark.parametrize("reader,column,prefix", [
    (search_narrators_from_db, Narrator.name, "DB read failed for narrator search: "),
    (get_narrator_books_from_db, Narrator.name, "DB read failed for narrator books: "),
    (search_series_from_db, Series.title, "DB search failed for series: "),
])
async def test_reader_failure_never_logs_the_bound_search_text(caplog, reader, column, prefix):
    session = _session_raising(_statement_error(
        column, hide_parameters=engine.sync_engine.hide_parameters
    ))

    with caplog.at_level(logging.WARNING):
        result = await reader(session, SECRET)

    assert result == []

    records = _records(caplog)
    assert len(records) == 1
    message = records[0].getMessage()

    # The operation is still named and the statement still printed: suppressing
    # the values must not cost the operator the ability to tell which query
    # failed, which is the whole reason the exception is logged at all.
    assert message.startswith(prefix)
    assert "[SQL:" in message
    # Suppressed, not merely absent -- the marker is SQLAlchemy reporting that
    # it had parameters and withheld them.
    assert "[SQL parameters hidden" in message

    serialised = _serialised(caplog)
    assert SECRET not in serialised
    for word in SECRET.split():
        assert word not in serialised


@pytest.mark.asyncio
async def test_the_same_failure_leaks_the_search_text_without_the_engine_setting(caplog):
    """
    The control. Everything above passes for free against an exception with
    nothing in it, so this proves the exception used up there is one that
    genuinely carries the caller's text, and that the engine setting is what
    withholds it.
    """
    session = _session_raising(_statement_error(Narrator.name, hide_parameters=False))

    with caplog.at_level(logging.WARNING):
        await search_narrators_from_db(session, SECRET)

    assert SECRET in _serialised(caplog)


def test_the_engine_hides_bound_parameters():
    """
    Set on the engine rather than at each log site, so it covers the queries
    nobody has written yet as well as the three handlers above.
    """
    assert engine.sync_engine.hide_parameters is True


def test_a_real_driver_error_hides_its_parameters_under_the_engines_setting():
    """
    Closes the gap between "the flag is set" and "the flag still does anything".

    A real DBAPI failure wrapped by SQLAlchemy's own exception handling rather
    than by this file, with hide_parameters read from the Libex engine, so this
    fails if the setting stops being honoured as well as if it stops being set.
    In-memory SQLite because the check needs a driver that can fail without a
    server; the setting is dialect-independent and the statement is never run
    against anything Libex owns.
    """
    probe = create_engine("sqlite://", hide_parameters=engine.sync_engine.hide_parameters)

    with pytest.raises(StatementError) as caught:
        with probe.connect() as conn:
            conn.execute(
                text("select * from narrators where name like :name"),
                {"name": f"%{SECRET}%"},
            )

    rendered = str(caught.value)
    assert "no such table" in rendered, "not a real driver error"
    assert SECRET not in rendered
    assert "[SQL parameters hidden" in rendered


@pytest.mark.asyncio
async def test_the_reader_still_reports_the_failure_to_the_caller_as_empty(caplog):
    """
    The privacy work must not have changed what a failed read returns: these
    handlers swallow and return empty, and a route counting on that is what
    keeps a DB blip from becoming a 500.
    """
    with patch("app.services.db.reader.logger"):
        session = _session_raising(_statement_error(
            Narrator.name, hide_parameters=engine.sync_engine.hide_parameters
        ))
        assert await search_series_from_db(session, SECRET) == []
