"""
Shared pieces for the db command tests: a SQLite store file built by the
package's own upgrade and seeded with the readers' catalog, and the table of
db commands with the question each one asks of the store.
"""

# Standard library
import asyncio
import json
from pathlib import Path

# Local
from tests.libex_core.storage._support import seed


async def _build(path: Path, seeded: bool) -> None:
    from sqlalchemy.engine import URL

    from libex_core.storage import LocalStore

    store = LocalStore(URL.create("sqlite+aiosqlite", database=str(path)))
    try:
        await store.upgrade()
        if seeded:
            await store.open()
            async with store.write() as session:
                # Books go through BookResponse on every db command, so the
                # one with plans that are not strings is left out of this
                # catalog; the readers' own tests keep it.
                await seed(session, odd_plans=False)
    finally:
        await store.close()


def make_store(path: Path, *, seeded: bool = True) -> str:
    """Creates the store file at path, upgraded, and seeded unless told not
    to be. Returns the path as the text LIBEX_CORE_STORAGE takes."""
    asyncio.run(_build(path, seeded))
    return str(path)


def loads(text: str):
    """The one line of JSON a command printed."""
    assert text.endswith("\n") and text.count("\n") == 1, text
    return json.loads(text)


# Every command that reads the store, with arguments that find something in the
# seeded catalog. Used to hold the commands to the same failure behaviour.
READ_COMMANDS: tuple[tuple[str, ...], ...] = (
    ("db", "book", "B000000001"),
    ("db", "books", "--title", "quest"),
    ("db", "chapters", "B000000001"),
    ("db", "sku", "SG1"),
    ("db", "author", "A000000001"),
    ("db", "author-books", "A000000001"),
    ("db", "series", "S000000001"),
    ("db", "series-books", "S000000001"),
    ("db", "narrators", "nina"),
    ("db", "narrator-books", "Nina Voice"),
    ("db", "genres"),
    ("db", "plans"),
    ("db", "plan", "Plus"),
    ("db", "vvab"),
    ("db", "new-releases"),
    ("db", "coming-soon"),
    ("db", "stats"),
    ("book", "sku", "SG1"),
)

ALL_DB_COMMANDS: tuple[tuple[str, ...], ...] = (
    ("db", "upgrade"),
    ("db", "status"),
    *READ_COMMANDS,
)
