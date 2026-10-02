"""
The package migration that keys books, series and tracks by (asin, region).

Built on SQLite, where it is a table rebuild and so the part most able to lose
a row, a constraint or a CHECK without anyone noticing. Each test starts from
a store at the revision before it, fills it with rows by hand, and runs the
chain forward the way the store does: foreign keys off for the rebuild, the
whole thing in one transaction.
"""

# Third party
import pytest
from alembic import command
from alembic.migration import MigrationContext
from alembic.autogenerate import compare_metadata
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import IntegrityError

# Local
from libex_core.storage.base import Base
from libex_core.storage.dialect import configure_sqlite
from libex_core.storage.models import CORE_TABLES
from libex_core.storage.upgrade import _config, set_foreign_keys, upgrade_to_head

BEFORE = "438dbe70d041"
AFTER = "a4d91f7c3b26"

STAMP = "2026-01-01 00:00:00"
BOOK_COLUMNS = (
    "explicit, whisper_sync, has_pdf, is_listenable, is_buyable, is_vvab, created_at, updated_at"
)
BOOK_VALUES = f"0, 0, 0, 1, 1, 0, '{STAMP}', '{STAMP}'"

# Every column that names a region and the table it lives in.
REGION_COLUMNS = (
    ("books", "region"),
    ("series", "region"),
    ("tracks", "region"),
    ("author_book", "book_region"),
    ("book_narrator", "book_region"),
    ("book_genre", "book_region"),
    ("book_series", "book_region"),
    ("book_series", "series_region"),
    ("series_author", "series_region"),
)

COUNTED = (
    "books", "series", "tracks", "author_book", "book_narrator", "book_genre",
    "book_series", "series_author", "authors", "narrators", "genres", "author_genre",
)


@pytest.fixture
def engine(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'store.db'}")
    # The store's own connection setup: without it the driver commits each DDL
    # statement as it goes, and a failed migration would leave its half behind.
    configure_sqlite(eng, wal=False)
    yield eng
    eng.dispose()


def _run(engine, revision, *, down=False):
    with engine.connect() as connection:
        set_foreign_keys(connection, False)
        try:
            with connection.begin():
                step = command.downgrade if down else command.upgrade
                step(_config(connection), revision)
        finally:
            set_foreign_keys(connection, True)


def _fill_old(engine):
    """A small catalogue as the previous revision stores it: one region per
    ASIN, links pointing at asin alone."""
    with engine.begin() as c:
        c.exec_driver_sql(
            f"INSERT INTO books (asin, title, region, {BOOK_COLUMNS}) VALUES "
            f"('B1', 'one', 'uk', {BOOK_VALUES}), ('B2', 'two', 'us', {BOOK_VALUES}), "
            f"('B3', 'three', 'de', {BOOK_VALUES})"
        )
        c.exec_driver_sql(
            "INSERT INTO series (asin, title, region, fetched_description, created_at, updated_at) "
            f"VALUES ('S1', 's one', 'uk', 0, '{STAMP}', '{STAMP}'), "
            f"('S2', 's two', 'us', 0, '{STAMP}', '{STAMP}')"
        )
        c.exec_driver_sql(
            "INSERT INTO authors (id, asin, name, region, fetched_description, created_at, updated_at) "
            f"VALUES (1, 'A1', 'ann', 'uk', 0, '{STAMP}', '{STAMP}')"
        )
        c.exec_driver_sql(
            "INSERT INTO narrators (name, fetched_description, created_at, updated_at) "
            f"VALUES ('nina', 0, '{STAMP}', '{STAMP}')"
        )
        c.exec_driver_sql(
            f"INSERT INTO genres (asin, name, type, created_at, updated_at) "
            f"VALUES ('G1', 'g', 'Genres', '{STAMP}', '{STAMP}')"
        )
        c.exec_driver_sql("INSERT INTO author_book VALUES (1, 'B1'), (1, 'B2')")
        c.exec_driver_sql("INSERT INTO book_narrator VALUES ('nina', 'B1'), ('nina', 'B3')")
        c.exec_driver_sql("INSERT INTO book_genre VALUES ('B1', 'G1'), ('B2', 'G1')")
        c.exec_driver_sql("INSERT INTO book_series VALUES ('B1', 'S1', '1'), ('B2', 'S2', '4')")
        c.exec_driver_sql("INSERT INTO series_author VALUES ('S1', 1)")
        c.exec_driver_sql("INSERT INTO author_genre VALUES (1, 'G1')")
        c.exec_driver_sql(
            f"INSERT INTO tracks (asin, chapters, created_at, updated_at) "
            f"VALUES ('B1', '[]', '{STAMP}', '{STAMP}'), ('B3', '[]', '{STAMP}', '{STAMP}')"
        )


def _counts(engine):
    with engine.connect() as c:
        return {t: c.exec_driver_sql(f"SELECT count(*) FROM {t}").scalar() for t in COUNTED}


def _revision(engine):
    with engine.connect() as c:
        return c.exec_driver_sql("SELECT version_num FROM libex_core_alembic_version").scalar()


@pytest.fixture
def old_store(engine):
    _run(engine, BEFORE)
    _fill_old(engine)
    return engine


# ============================================================
# UPGRADE WITH DATA
# ============================================================

def test_upgrade_keeps_every_row_and_gives_each_link_its_parents_region(old_store):
    before = _counts(old_store)
    _run(old_store, AFTER)

    assert _counts(old_store) == before
    with old_store.connect() as c:
        rows = lambda sql: c.exec_driver_sql(sql).all()  # noqa: E731
        assert rows("SELECT author_id, book_asin, book_region FROM author_book ORDER BY 2") == [
            (1, "B1", "uk"), (1, "B2", "us"),
        ]
        assert rows("SELECT book_asin, book_region, narrator_name FROM book_narrator ORDER BY 1") == [
            ("B1", "uk", "nina"), ("B3", "de", "nina"),
        ]
        assert rows("SELECT book_asin, book_region, genre_asin FROM book_genre ORDER BY 1") == [
            ("B1", "uk", "G1"), ("B2", "us", "G1"),
        ]
        assert rows(
            "SELECT book_asin, book_region, series_asin, series_region, position "
            "FROM book_series ORDER BY 1"
        ) == [("B1", "uk", "S1", "uk", "1"), ("B2", "us", "S2", "us", "4")]
        assert rows("SELECT series_asin, series_region, author_id FROM series_author") == [
            ("S1", "uk", 1),
        ]
        assert rows("SELECT asin, region FROM tracks ORDER BY 1") == [("B1", "uk"), ("B3", "de")]
        assert rows("PRAGMA foreign_key_check") == []


def test_upgrade_ends_at_the_new_head_and_leaves_the_new_columns_empty(old_store):
    _run(old_store, AFTER)
    assert _revision(old_store) == AFTER
    with old_store.connect() as c:
        for table, column in (
            ("books", "confirmed_at"), ("books", "chapters_confirmed_at"),
            ("series", "confirmed_at"), ("authors", "confirmed_at"),
        ):
            assert c.exec_driver_sql(f"SELECT count({column}) FROM {table}").scalar() == 0
        assert c.exec_driver_sql("SELECT count(*) FROM walk_results").scalar() == 0


def test_the_keys_are_the_composite_ones(old_store):
    _run(old_store, AFTER)
    inspector = inspect(old_store)
    for table in ("books", "series", "tracks"):
        assert inspector.get_pk_constraint(table)["constrained_columns"] == ["asin", "region"]
    assert inspector.get_pk_constraint("walk_results")["constrained_columns"] == [
        "kind", "asin", "region",
    ]
    uniques = {
        t: next(u for u in inspector.get_unique_constraints(t) if u["name"] == f"uq_{t}")
        for t in ("author_book", "book_narrator", "book_genre", "book_series", "series_author")
    }
    assert uniques["author_book"]["column_names"] == ["author_id", "book_asin", "book_region"]
    assert uniques["book_narrator"]["column_names"] == ["book_asin", "book_region", "narrator_name"]
    assert uniques["book_genre"]["column_names"] == ["book_asin", "book_region", "genre_asin"]
    assert uniques["book_series"]["column_names"] == [
        "book_asin", "book_region", "series_asin", "series_region",
    ]
    assert uniques["series_author"]["column_names"] == ["series_asin", "series_region", "author_id"]
    composite = {
        (t, tuple(fk["constrained_columns"]), fk["referred_table"], tuple(fk["referred_columns"]))
        for t in ("tracks", "author_book", "book_narrator", "book_genre", "book_series", "series_author")
        for fk in inspector.get_foreign_keys(t)
        if len(fk["constrained_columns"]) == 2
    }
    assert composite == {
        ("tracks", ("asin", "region"), "books", ("asin", "region")),
        ("author_book", ("book_asin", "book_region"), "books", ("asin", "region")),
        ("book_narrator", ("book_asin", "book_region"), "books", ("asin", "region")),
        ("book_genre", ("book_asin", "book_region"), "books", ("asin", "region")),
        ("book_series", ("book_asin", "book_region"), "books", ("asin", "region")),
        ("book_series", ("series_asin", "series_region"), "series", ("asin", "region")),
        ("series_author", ("series_asin", "series_region"), "series", ("asin", "region")),
    }
    # No single-column key onto books or series is left to fight the composite one.
    for table in ("tracks", "author_book", "book_narrator", "book_genre", "book_series", "series_author"):
        for fk in inspector.get_foreign_keys(table):
            if fk["referred_table"] in ("books", "series"):
                assert len(fk["constrained_columns"]) == 2


@pytest.mark.parametrize("table, column", REGION_COLUMNS)
def test_every_region_column_still_refuses_a_region_outside_the_eleven(old_store, table, column):
    _run(old_store, AFTER)
    with old_store.connect() as c:
        ddl = c.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE name = ?", (table,)
        ).scalar()
    assert f"CHECK ({column} IN (" in ddl
    assert ddl.count(f"CHECK ({column} IN (") == 1


def test_a_second_region_of_the_same_asin_is_its_own_book_with_its_own_links(old_store):
    _run(old_store, AFTER)
    with old_store.begin() as c:
        c.exec_driver_sql("PRAGMA foreign_keys=ON")
        c.exec_driver_sql(
            f"INSERT INTO books (asin, title, region, {BOOK_COLUMNS}) VALUES "
            f"('B1', 'one in the us', 'us', {BOOK_VALUES})"
        )
        c.exec_driver_sql(
            "INSERT INTO author_book (author_id, book_asin, book_region) VALUES (1, 'B1', 'us')"
        )
        c.exec_driver_sql(
            "INSERT INTO book_narrator (narrator_name, book_asin, book_region) "
            "VALUES ('nina', 'B1', 'us')"
        )
        c.exec_driver_sql(
            "INSERT INTO book_genre (book_asin, book_region, genre_asin) VALUES ('B1', 'us', 'G1')"
        )
        c.exec_driver_sql("INSERT INTO tracks (asin, region, chapters, created_at, updated_at) "
                          f"VALUES ('B1', 'us', '[]', '{STAMP}', '{STAMP}')")
        assert c.exec_driver_sql(
            "SELECT count(*) FROM author_book WHERE book_asin = 'B1'"
        ).scalar() == 2
        c.exec_driver_sql("DELETE FROM books WHERE asin = 'B1' AND region = 'uk'")
        # The uk record's links went with it; the us record's stayed.
        assert c.exec_driver_sql(
            "SELECT book_region FROM author_book WHERE book_asin = 'B1'"
        ).all() == [("us",)]
        assert c.exec_driver_sql("SELECT region FROM tracks WHERE asin = 'B1'").all() == [("us",)]


def test_a_link_to_a_region_the_book_does_not_have_is_refused(old_store):
    _run(old_store, AFTER)
    with pytest.raises(IntegrityError):
        with old_store.begin() as c:
            c.exec_driver_sql("PRAGMA foreign_keys=ON")
            c.exec_driver_sql(
                "INSERT INTO author_book (author_id, book_asin, book_region) VALUES (1, 'B1', 'us')"
            )


def test_the_new_unique_keys_refuse_a_duplicate_link_in_the_same_region(old_store):
    _run(old_store, AFTER)
    with pytest.raises(IntegrityError):
        with old_store.begin() as c:
            c.exec_driver_sql(
                "INSERT INTO author_book (author_id, book_asin, book_region) VALUES (1, 'B1', 'uk')"
            )


def test_walk_results_refuses_a_kind_it_does_not_know(old_store):
    _run(old_store, AFTER)
    insert = (
        "INSERT INTO walk_results (kind, asin, region, complete, incomplete_reasons, "
        f"book_asins, confirmed_at) VALUES ('{{}}', 'A1', 'us', 1, '[]', '[]', '{STAMP}')"
    )
    with old_store.begin() as c:
        c.exec_driver_sql(insert.format("author_books"))
        c.exec_driver_sql(insert.format("series_books").replace("'A1'", "'S1'"))
    with pytest.raises(IntegrityError):
        with old_store.begin() as c:
            c.exec_driver_sql(insert.format("narrator_books"))


# ============================================================
# PRECONDITIONS: REFUSE, NEVER REPAIR
# ============================================================

def test_a_series_with_no_region_stops_the_upgrade_and_changes_nothing(old_store):
    with old_store.begin() as c:
        c.exec_driver_sql(
            "INSERT INTO series (asin, title, region, fetched_description, created_at, updated_at) "
            f"VALUES ('S9', 'orphan', NULL, 0, '{STAMP}', '{STAMP}')"
        )
    with pytest.raises(RuntimeError, match="1 series rows have no region"):
        _run(old_store, AFTER)
    assert _revision(old_store) == BEFORE
    columns = {c["name"] for c in inspect(old_store).get_columns("books")}
    assert "confirmed_at" not in columns
    assert "walk_results" not in inspect(old_store).get_table_names()
    assert inspect(old_store).get_pk_constraint("books")["constrained_columns"] == ["asin"]


def test_a_link_with_no_parent_stops_the_upgrade_and_changes_nothing(old_store):
    with old_store.connect() as c:
        set_foreign_keys(c, False)
        try:
            with c.begin():
                c.exec_driver_sql("INSERT INTO book_genre VALUES ('GHOST', 'G1')")
        finally:
            set_foreign_keys(c, True)
    with pytest.raises(RuntimeError, match="1 rows have no matching books row"):
        _run(old_store, AFTER)
    assert _revision(old_store) == BEFORE
    assert _counts(old_store)["book_genre"] == 3


# ============================================================
# DOWNGRADE
# ============================================================

def test_downgrade_restores_the_single_region_keys_and_keeps_every_row(old_store):
    _run(old_store, AFTER)
    before = _counts(old_store)
    _run(old_store, BEFORE, down=True)

    assert _revision(old_store) == BEFORE
    assert _counts(old_store) == before
    inspector = inspect(old_store)
    for table in ("books", "series", "tracks"):
        assert inspector.get_pk_constraint(table)["constrained_columns"] == ["asin"]
    assert "walk_results" not in inspector.get_table_names()
    assert "confirmed_at" not in {c["name"] for c in inspector.get_columns("books")}
    assert {i["name"] for i in inspector.get_indexes("books")} == {
        "books_asin_index", "books_region_asin_index",
    }
    with old_store.connect() as c:
        assert c.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    # And it can come back up again.
    _run(old_store, AFTER)
    assert _counts(old_store) == before


def test_downgrade_refuses_once_an_asin_has_two_regions_and_deletes_nothing(old_store):
    _run(old_store, AFTER)
    with old_store.begin() as c:
        c.exec_driver_sql(
            f"INSERT INTO books (asin, title, region, {BOOK_COLUMNS}) VALUES "
            f"('B1', 'one in the us', 'us', {BOOK_VALUES}), ('B2', 'two in fr', 'fr', {BOOK_VALUES})"
        )
        c.exec_driver_sql(
            "INSERT INTO series (asin, title, region, fetched_description, created_at, updated_at) "
            f"VALUES ('S1', 'again', 'us', 0, '{STAMP}', '{STAMP}')"
        )
    before = _counts(old_store)
    with pytest.raises(RuntimeError) as raised:
        _run(old_store, BEFORE, down=True)
    message = str(raised.value)
    assert "2 ASINs in books" in message and "1 ASINs in series" in message
    assert "Nothing was changed or deleted" in message
    assert _revision(old_store) == AFTER
    assert _counts(old_store) == before


# ============================================================
# SCHEMA AGAINST THE MODELS
# ============================================================

def _drift(engine):
    with engine.connect() as connection:
        context = MigrationContext.configure(
            connection,
            opts={
                "compare_type": True,
                "include_object": lambda obj, name, type_, reflected, compare_to: (
                    type_ != "table" or name in CORE_TABLES
                ),
            },
        )
        return compare_metadata(context, Base.metadata)


def test_the_migrated_schema_is_the_models_schema(engine):
    _run(engine, AFTER)
    assert _drift(engine) == []


def test_the_new_columns_are_nullable_and_have_no_default(engine):
    _run(engine, AFTER)
    inspector = inspect(engine)
    for table, column in (
        ("books", "confirmed_at"), ("books", "chapters_confirmed_at"),
        ("series", "confirmed_at"), ("authors", "confirmed_at"),
    ):
        found = next(c for c in inspector.get_columns(table) if c["name"] == column)
        assert found["nullable"] and found["default"] is None
    walk = {c["name"]: c for c in inspector.get_columns("walk_results")}
    assert not any(walk[c]["nullable"] for c in walk)


def test_upgrade_to_head_runs_the_revision_the_way_the_store_does(engine):
    with engine.connect() as connection:
        set_foreign_keys(connection, False)
        with connection.begin():
            upgrade_to_head(connection)
    assert _revision(engine) == AFTER
