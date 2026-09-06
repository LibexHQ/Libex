"""
Guards the privacy contract on the DB reader's failure log lines.

Three reader handlers -- narrator search, narrator books and series search --
log a warning naming the operation when their query fails, and never put the
caller's search text anywhere in that log line. The message is a fixed string
naming the operation, and the structured fields alongside it come from
_failure_fields(e) (app/services/db/writer.py), which reports classification
and schema metadata only -- error_type, sqlstate, schema_name, table_name,
column_name, constraint_name -- and never touches str(e), the compiled
statement, or the statement's bound parameters. That is a stronger guarantee
than the previous shape of these three lines, which interpolated the
exception directly into the message (an f"...: {e}") and relied on the
engine's hide_parameters=True (app/db/session.py) to keep the rendered
exception text from including the bound parameters. Both guards are exercised
here:

  * the failure path is driven with an error that really CARRIES bound
    parameters, so a fixture that could never leak in the first place proves
    nothing; and
  * the engine setting is still asserted directly (it still matters for any
    query not routed through _failure_fields), so deleting it fails a test
    rather than silently reopening a leak on some other path.

The test that used to be the control -- proving the same path leaks without
hide_parameters -- now proves the opposite, and is kept for that reason: since
the message no longer touches str(e) at all, disabling the engine setting no
longer reopens a leak on this specific path. That is a real, verified change
of behaviour, not an oversight in the test.
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

# The exact key set _failure_fields(e) reports -- pinned here as well as in
# tests/services/test_persist_queue.py, since both reader.py and
# persist_queue.py import the same function from writer.py and a change to it
# affects both call sites' log shape identically.
_FAILURE_FIELD_KEYS = {
    "error_type", "sqlstate", "schema_name", "table_name",
    "column_name", "constraint_name",
}


def _records(caplog):
    return [r for r in caplog.records if r.levelno >= logging.WARNING]


def _serialised(caplog):
    return " ".join(str(r.__dict__) for r in _records(caplog))


# Duplicates app/core/logging.py's own standard-field exclusion list rather
# than importing it, so this test does not reach into a module it has no
# reason to depend on just to compute which attributes on a LogRecord came in
# through extra=.
_STANDARD_RECORD_FIELDS = {
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "message", "taskName", "asctime",
}


def _extras(record):
    return {k: v for k, v in record.__dict__.items() if k not in _STANDARD_RECORD_FIELDS}


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
@pytest.mark.parametrize("reader,column,message", [
    (search_narrators_from_db, Narrator.name, "DB read failed for narrator search"),
    (get_narrator_books_from_db, Narrator.name, "DB read failed for narrator books"),
    (search_series_from_db, Series.title, "DB search failed for series"),
])
async def test_reader_failure_never_logs_the_bound_search_text(caplog, reader, column, message):
    session = _session_raising(_statement_error(
        column, hide_parameters=engine.sync_engine.hide_parameters
    ))

    with caplog.at_level(logging.WARNING):
        result = await reader(session, SECRET)

    assert result == []

    records = _records(caplog)
    assert len(records) == 1
    record = records[0]

    # The operation is still named, so the failure remains attributable to
    # this endpoint -- but the message itself is now static: nothing derived
    # from the exception is interpolated into it at all.
    assert record.getMessage() == message

    # The extra fields carry exactly _failure_fields(e)'s fixed key set --
    # never str(e), never the compiled statement, never a bound parameter --
    # so there is nothing here for hide_parameters to have to catch.
    assert set(_extras(record)) == _FAILURE_FIELD_KEYS

    serialised = _serialised(caplog)
    assert SECRET not in serialised
    for word in SECRET.split():
        assert word not in serialised


@pytest.mark.asyncio
async def test_the_reader_no_longer_depends_on_the_engine_setting_to_stay_safe(caplog):
    """
    Historically this was the control: with hide_parameters off, the same path
    used to interpolate str(e) into the log message and leak the caller's
    text, proving the engine setting was load-bearing for it. That path no
    longer exists -- the message is static and the extras carry only
    _failure_fields(e), which never touches str(e) or the statement's bound
    parameters -- so the secret stays out even with the engine setting
    disabled. hide_parameters is still asserted directly below, as a second,
    independent guard for any query not routed through _failure_fields, but
    this reader path no longer needs it to be correct.
    """
    session = _session_raising(_statement_error(Narrator.name, hide_parameters=False))

    with caplog.at_level(logging.WARNING):
        await search_narrators_from_db(session, SECRET)

    assert SECRET not in _serialised(caplog)


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
