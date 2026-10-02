"""
The readers answer the same on SQLite as on Postgres for the same rows.

SQLite runs everywhere. The Postgres side needs Docker and runs with the
integration marker; it builds the same schema from the same metadata, seeds the
same rows and runs the same calls, and the answers must be equal.
"""

# Third party
import pytest

# Local
from libex_core.storage.read import people, series
from tests.libex_core.storage._support import calls, run_all

pytest.importorskip("aiosqlite")


@pytest.mark.asyncio
async def test_every_call_runs_on_sqlite(sqlite_session):
    answers = await run_all(sqlite_session)
    assert set(answers) == {name for name, _, _ in calls()}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sqlite_answers_match_postgres(sqlite_session, postgres_session):
    lite = await run_all(sqlite_session)
    pg = await run_all(postgres_session)
    mismatched = {
        name: _first_difference(lite[name], pg[name]) for name in lite if lite[name] != pg[name]
    }
    assert not mismatched, "\n".join(f"{name}: {where}" for name, where in mismatched.items())


def _first_difference(a, b, path="") -> str:
    """Where two answers first part ways, so a failure names the field."""
    if type(a) is not type(b):
        return f"{path}: {a!r} vs {b!r}"
    if isinstance(a, dict):
        for key in sorted(set(a) | set(b)):
            if key not in a or key not in b:
                return f"{path}.{key}: present on one side only"
            if a[key] != b[key]:
                return _first_difference(a[key], b[key], f"{path}.{key}")
    if isinstance(a, list):
        if len(a) != len(b):
            return f"{path}: {len(a)} rows vs {len(b)}"
        for i, (x, y) in enumerate(zip(a, b)):
            if x != y:
                return _first_difference(x, y, f"{path}[{i}]")
    return f"{path}: {a!r} vs {b!r}"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_text_ordering_is_the_one_known_difference(sqlite_session, postgres_session):
    """Text sorts follow each backend's collation: Postgres its database
    locale, SQLite byte order. Nothing here can make SQLite match a glibc
    locale, so the difference is pinned rather than hidden: if it ever closes,
    this fails and the note in the docs can go."""
    lite = await series.get_series_books(sqlite_session, "S000000004")
    pg = await series.get_series_books(postgres_session, "S000000004")
    assert sorted(b["asin"] for b in lite) == sorted(b["asin"] for b in pg)
    assert [b["asin"][-2:] for b in lite] != [b["asin"][-2:] for b in pg]

    lite = await people.search_narrators(sqlite_session, "", sort="name")
    pg = await people.search_narrators(postgres_session, "", sort="name")
    assert [n["name"] for n in lite][-1] == "Émile Lecteur"   # bytes: accented last
    assert [n["name"] for n in pg][0] == "Émile Lecteur"      # locale: E before N
