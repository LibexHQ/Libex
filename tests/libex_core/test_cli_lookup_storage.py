"""
The lookup commands with LIBEX_CORE_STORAGE set: each keeps what Audible
answered in the store (one case per family, read back through the db commands),
prints what it printed without a store, and when Audible cannot be reached
answers from what is kept. With storage off a lookup loads no database library
at all.
"""

# Standard library
import json
import sqlite3
import textwrap
from unittest.mock import AsyncMock

# Third party
import pytest

# Local
from libex_core.cli.environment import STORAGE_VARIABLE
from libex_core.exceptions import AudibleAPIException
from tests.libex_core._cli_lookup_support import (
    CASE_IDS,
    CASES,
    EGRESS,
    install_session,
    unclocked,
)
from tests.libex_core._cli_support import clean_env, run_python
from tests.libex_core._db_support import loads

CASE = {case.name: case for case in CASES}


@pytest.fixture
def lookup(run_cli, monkeypatch, empty_store):
    """lookup(name, **env) runs the named case against its stand-in Audible
    with the store on, and returns the result; read(*argv) then asks the store
    through the db commands."""

    def go(name, store=empty_store, get=None):
        case = CASE[name]
        install_session(monkeypatch, get or case.make_get())
        return run_cli(list(case.argv), env={**EGRESS, STORAGE_VARIABLE: store})

    return go


@pytest.fixture
def read(run_cli, empty_store):
    def ask(*argv, store=empty_store):
        result = run_cli(list(argv), env={STORAGE_VARIABLE: store})
        return result.code, (loads(result.out) if result.out else None), result.err

    return ask


def books_in(value):
    """Every book in what a command printed: the dicts that carry an asin and a title."""
    if isinstance(value, list):
        return [b for item in value for b in books_in(item)]
    if isinstance(value, dict):
        if "asin" in value and "title" in value:
            return [value]
        return [b for item in value.values() for b in books_in(item)]
    return []


# ============================================================
# A STORE CHANGES NOTHING THE CALLER SEES
# ============================================================

# The author's profile is fetched in the same lookup and its image is stored
# with the author, so the books of those lookups come back with it when a
# store is on and without it when none is.
ENRICHED = {"author books", "author books shaped"}


def without_row_ids(value, image=False):
    """An author carries the id of the row it is stored in, which a store
    makes and a bare lookup has none of; everything else is compared."""
    if isinstance(value, list):
        return [without_row_ids(item, image) for item in value]
    if isinstance(value, dict):
        blank = {"id"} | ({"image"} if image and "asin" in value and "name" in value else set())
        return {k: None if k in blank else without_row_ids(v, image) for k, v in value.items()}
    return value


@pytest.mark.parametrize("name", CASE_IDS)
def test_the_answer_with_a_store_is_the_answer_without_one(run_cli, monkeypatch, empty_store, name):
    case = CASE[name]
    install_session(monkeypatch, case.make_get())
    bare = run_cli(list(case.argv), env=EGRESS)
    install_session(monkeypatch, case.make_get())
    stored = run_cli(list(case.argv), env={**EGRESS, STORAGE_VARIABLE: empty_store})
    assert bare.code == stored.code == 0, (bare.err, stored.err)
    image = name in ENRICHED
    assert without_row_ids(unclocked(json.loads(stored.out)), image) == without_row_ids(
        unclocked(json.loads(bare.out)), image
    )
    assert json.loads(bare.out), "an empty answer would equal an empty answer"
    if image:
        assert {a["image"] for b in json.loads(stored.out) for a in b["authors"]} == {"http://i"}
        assert {a["image"] for b in json.loads(bare.out) for a in b["authors"]} == {None}


# ============================================================
# WRITE-THROUGH -- one command per family, read back from the store
# ============================================================

@pytest.mark.parametrize("name", [
    "book get", "book bulk", "series books", "search", "quick-search", "abs search",
    "narrator books", "author books", "author books-by-name", "releases new",
    "releases coming-soon",
])
def test_the_books_a_lookup_returned_are_in_the_store_as_returned(lookup, read, name):
    result = lookup(name)
    assert result.code == 0, result.err
    printed = books_in(json.loads(result.out))
    assert printed, name
    for book in printed:
        code, stored, err = read("db", "book", book["asin"])
        assert code == 0, (book["asin"], err)
        assert stored["asin"] == book["asin"]
        assert stored["title"] == book["title"]
        if "authors" in book:
            assert stored["authors"] == book["authors"]


def test_a_book_lookup_stores_the_book_and_nothing_it_did_not_receive(lookup, read):
    assert lookup("book get").code == 0
    code, stats, _ = read("db", "stats")
    assert stats["books"] == 1
    assert (stats["authors"], stats["narrators"], stats["series"], stats["booksWithChapters"]) == (0, 0, 0, 0)


def test_a_chapters_lookup_stores_the_chapters_of_a_book_the_store_holds(lookup, read):
    assert lookup("book get").code == 0
    result = lookup("book chapters")
    assert result.code == 0, result.err
    printed = json.loads(result.out)
    code, stored, err = read("db", "chapters", "B0LOOK0001")
    assert code == 0, err
    assert [c["title"] for c in stored["chapters"]] == ["One", "Two"]
    assert [c["title"] for c in printed["chapters"]] == ["One", "Two"]
    assert stored["runtimeLengthMs"] == printed["runtimeLengthMs"] == 9000


def test_chapters_for_a_book_the_store_does_not_hold_are_printed_and_not_kept(lookup, read):
    """A chapter listing hangs off its book's row, as on the hosted service."""
    result = lookup("book chapters")
    assert result.code == 0, result.err
    assert [c["title"] for c in json.loads(result.out)["chapters"]] == ["One", "Two"]
    assert read("db", "chapters", "B0LOOK0001")[0] == 3


def test_a_series_lookup_stores_the_series(lookup, read):
    result = lookup("series get")
    assert result.code == 0, result.err
    printed = json.loads(result.out)
    code, stored, err = read("db", "series", "B0SERIES01")
    assert code == 0, err
    assert (stored["asin"], stored["name"]) == (printed["asin"], printed["name"])


def test_a_series_books_lookup_stores_the_members_in_series_order(lookup, read):
    from tests.libex_core._lookup_support import BOOKS, SERIES, product
    from tests.libex_core.test_lookup_store import SERIES_RELATION, batch_get

    members = {
        a: product(a, relationships=[{**SERIES_RELATION, "sequence": str(len(BOOKS) - i)}])
        for i, a in enumerate(BOOKS)
    }
    assert lookup("series get", get=batch_get(**members)).code == 0
    result = lookup("series books", get=batch_get(**members))
    assert result.code == 0, result.err
    code, stored, err = read("db", "series-books", SERIES)
    assert code == 0, err
    # sequences run backwards through the list, so series order is not the
    # order the books were fetched in
    assert [b["asin"] for b in stored] == sorted(BOOKS, reverse=True)


@pytest.mark.parametrize("name", ["author get", "author search"])
def test_an_author_lookup_stores_the_author(lookup, read, name):
    result = lookup(name)
    assert result.code == 0, result.err
    printed = json.loads(result.out)
    printed = printed[0] if isinstance(printed, list) else printed
    code, stored, err = read("db", "author", printed["asin"])
    assert code == 0, err
    assert stored["name"] == printed["name"]


def test_the_category_taxonomy_is_not_stored(lookup, read):
    """The hosted service keeps it in a table this schema does not have."""
    result = lookup("releases categories")
    assert result.code == 0, result.err
    assert json.loads(result.out)
    assert read("db", "genres")[0] == 3


def test_a_second_lookup_does_not_shrink_what_the_first_stored(lookup, read, empty_store):
    from tests.libex_core._lookup_support import product
    from tests.libex_core.test_lookup_store import batch_get

    full = product("B0LOOK0001", publisher_summary="a summary that is long enough to win")
    first = lookup("book get", get=batch_get(B0LOOK0001=full))
    assert first.code == 0, first.err
    code, stored, _ = read("db", "book", "B0LOOK0001")
    assert stored["summary"] == "a summary that is long enough to win"
    second = lookup("book get", get=batch_get(B0LOOK0001=product("B0LOOK0001")))
    assert second.code == 0, second.err
    code, stored, _ = read("db", "book", "B0LOOK0001")
    assert stored["summary"] == "a summary that is long enough to win"
    assert json.loads(second.out)["summary"] == "a summary that is long enough to win"


# ============================================================
# AN OUTAGE IS ANSWERED FROM THE STORE
# ============================================================

def test_when_audible_is_down_a_stored_book_is_what_answers(lookup):
    first = lookup("book get")
    assert first.code == 0, first.err
    down = AsyncMock(side_effect=AudibleAPIException("boom", upstream_status=503))
    second = lookup("book get", get=down)
    assert second.code == 0, second.err
    assert unclocked(json.loads(second.out)) == unclocked(json.loads(first.out))


def test_when_audible_is_down_and_nothing_is_stored_the_outage_is_the_answer(lookup):
    down = AsyncMock(side_effect=AudibleAPIException("boom", upstream_status=503))
    result = lookup("book get", get=down)
    assert result.code == 4
    assert result.out == ""


# ============================================================
# STORAGE OFF
# ============================================================

@pytest.mark.parametrize("value", [None, "", "off"])
def test_a_lookup_with_storage_off_creates_nothing(run_cli, monkeypatch, tmp_path, value):
    case = CASE["book get"]
    install_session(monkeypatch, case.make_get())
    env = dict(EGRESS) if value is None else {**EGRESS, STORAGE_VARIABLE: value}
    monkeypatch.chdir(tmp_path)
    assert run_cli(list(case.argv), env=env).code == 0
    assert list(tmp_path.iterdir()) == []


_CHILD = textwrap.dedent(
    """
    import sys
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from libex_core.cli import main
    import libex_core.cli.session as session

    CHAPTERS = {"content_metadata": {"chapter_info": {
        "is_accurate": True, "runtime_length_ms": 9000,
        "chapters": [{"length_ms": 9000, "start_offset_ms": 0, "title": "One"}],
    }}}

    async def get(region, path, params=None, extra_headers=None):
        return CHAPTERS

    @asynccontextmanager
    async def client_session(config):
        yield SimpleNamespace(get=get)

    session.client_session = client_session
    code = main(sys.argv[1:])
    sys.stdout.flush()
    heavy = sorted(
        n for n in sys.modules
        if n.split(".")[0] in ("sqlalchemy", "aiosqlite", "alembic", "asyncpg")
    )
    sys.stderr.write("HEAVY:" + ",".join(heavy) + "\\n")
    sys.exit(code)
    """
)


def _child(argv, tmp_path, **env):
    script = tmp_path / "child.py"
    script.write_text(_CHILD)
    done = run_python(
        [str(script), *argv],
        env=clean_env(LIBEX_CORE_ALLOW_DIRECT_EGRESS="1", **env),
        cwd=tmp_path,
    )
    heavy = [
        line for line in done.stderr.decode().splitlines() if line.startswith("HEAVY:")
    ]
    assert heavy, done.stderr.decode()
    return done, heavy[-1][len("HEAVY:"):]


@pytest.mark.parametrize("value", [None, "", "off"])
def test_a_lookup_with_storage_off_loads_no_database_library(tmp_path, value):
    env = {} if value is None else {STORAGE_VARIABLE: value}
    done, heavy = _child(["book", "chapters", "B0LOOK0001"], tmp_path, **env)
    assert done.returncode == 0, done.stderr.decode()
    assert json.loads(done.stdout)["chapters"][0]["title"] == "One"
    assert heavy == ""


@pytest.mark.parametrize("argv", [
    ["db", "status"], ["db", "books", "--title", "x"], ["db", "stats"], ["book", "sku", "SG1"],
])
def test_a_db_command_with_storage_off_loads_no_database_library(tmp_path, argv):
    done, heavy = _child(argv, tmp_path)
    assert done.returncode == 5
    assert heavy == ""


def test_the_probe_does_see_the_libraries_when_storage_is_on(tmp_path):
    """Without this the two tests above could pass with a probe that sees
    nothing."""
    store = tmp_path / "s.db"
    sqlite3.connect(store).close()
    done, heavy = _child(["db", "upgrade"], tmp_path, **{STORAGE_VARIABLE: str(store)})
    assert done.returncode == 0, done.stderr.decode()
    assert "sqlalchemy" in heavy.split(",")
