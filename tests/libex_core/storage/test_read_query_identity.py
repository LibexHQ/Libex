"""
The SQL the readers send to PostgreSQL has not changed.

Moving the readers into libex_core must not alter a single query. The golden
file holds the compiled text and bind parameters of every read entry point
under a spread of filters and sorts, captured from the readers before the move.
SQLite cannot catch a changed query that happens to return the same rows, so
this compares the statements themselves.
"""

# Standard library
import json

# Local
import app.services.db.reader as hosted_reader
from tests.libex_core.storage._pg_statements import GOLDEN, collect, pack


async def test_every_statement_matches_the_golden():
    golden = json.loads(GOLDEN.read_text())
    now = pack(await collect())

    assert set(now["statements"]) == set(golden["statements"])
    changed = []
    for tag, entry in now["statements"].items():
        want = golden["statements"][tag]
        if golden["sql"][want["sql"]] != now["sql"][entry["sql"]]:
            changed.append(f"{tag}: SQL text")
        elif entry["params"] != want["params"]:
            changed.append(f"{tag}: bind parameters {entry['params']} != {want['params']}")
    assert not changed, "\n".join(changed[:20])


async def test_the_golden_covers_every_hosted_entry_point_filtered_and_sorted():
    golden = json.loads(GOLDEN.read_text())
    tags = list(golden["statements"])
    hosted = {name for name in dir(hosted_reader) if name.endswith("_from_db")}
    assert {t.split("|")[0] for t in tags if "|plain|" in t} == hosted
    assert {t.split("|")[0] for t in tags if "|filtered|" in t} == hosted
    assert {"stats|None", "stats|us", "stored_genres", "positions", "positions_batch"} <= set(tags)
    assert golden["statements"]["search_books_from_db|filtered|rating|desc"]["sql"] != golden["statements"][
        "search_books_from_db|filtered|rating|asc"
    ]["sql"]
