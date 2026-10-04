"""
One-off repair for books polluted across regions before region-aware keys.

Before 2.2.0 books and series were keyed by ASIN alone, so another
marketplace's answer for an ISBN-10 ASIN that exists in several regions was
merged into whichever row was stored first. That row kept its own region and
gained the other region's authors, series, scalars, texts and extras. Nothing
in the merged row says which of its values came from where, so the only honest
repair is to throw the row away and take Audible's answer for the region the
row says it belongs to. Everything else Libex does is "less data is never
accepted"; this script is the scoped, recorded exception to that (decision
0f020f65): it replaces, never merges, and only for the books `plan` selects.

What it is and is not. It re-reads books already stored, one request per
fifty of them plus one chapters request each, from the marketplace each row
already names. It acquires nothing new and is not catalog scraping; the
target is roughly ten thousand ISBN-10-keyed books, and it is paced far below
the corpus walks (see PACING).

WHICH BOOKS. A book is polluted when, against the 2.2.0 schema, either

  * an author_book row of the book points at an authors row whose region
    differs from the link's book_region, or
  * a book_series row of the book has a series_region that differs from its
    book_region,

and its ASIN is ISBN-10-shaped (nine digits and a digit or X). Nothing else is
ever selected: the audit found no B0 ASIN affected, and the script refuses a
list containing one. `plan` also counts polluted books outside that shape and
reports them without touching them, so a widened problem is visible.

Foreign series_author rows (authors.region differs from series_region) are the
third attributable kind. They belong to a series, not to a book, so replacing
the book does not remove them: they are listed in the plan and deleted, backed
up, after the book pass has finished. The foreign author_book and book_series
links of a polluted book are NOT deleted up front. Deleting the book removes
every link row by cascade and the rewrite adds back only what Audible says,
so a separate delete would be a second write that changes nothing.

WHAT ONE BOOK GOES THROUGH. For each pair in the frozen list:

  1. Audible is asked first, outside any transaction: the product for the
     stored region (BOOK_RESPONSE_GROUPS, up to fifty consecutive same-region
     pairs per request, as the app's own fetch does) and then its chapters.
  2. Only a real product answer (the asin and a title, not a placeholder)
     goes on. An outage, a 404, a placeholder or an empty answer leaves the
     row exactly as it was and is logged. An outage also ends the run with
     the cursor held at that book, so nothing hammers a failing exit.
     A chapters 404, or a 200 with no chapter listing, is a legitimate empty
     answer: the old track is backed up anyway and none is written.
  3. In ONE transaction: lock the book row, append the whole prior state to
     the backup file and fsync it, delete the book (the cascade takes its
     links and its track), write the fresh answer through the normal writers
     (write_books, write_track), then put back the two things the rewrite
     cannot infer: is_primary and created_at. The backup is durable before
     the delete can commit, so a crash between the two leaves a backup line
     for a book that was not changed, which restores to the same state.
  4. The cache keys book:{region}:{asin} and chapters:{region}:{asin} are
     invalidated after the commit. Bulk keys cannot be enumerated and expire
     on their TTL.

is_primary. The writer decides it when it inserts a row: true unless another
region already holds the ASIN. The polluted rows were the first stored, so
they were primary, but a rewrite after the delete would see the other
region's row and insert as non-primary, silently demoting the row the reads
prefer. The original value is read in the transaction and written back
explicitly. created_at is restored with it because it is the tie-break
between primary rows. chapters_checked_at is stamped, since the chapters
were just asked about. Nothing else on the old row survives; that is the
point.

PACING AND EGRESS. Strictly sequential, concurrency 1, with a random
pause of REPAIR_DELAY_MIN..REPAIR_DELAY_MAX seconds (0.7..2.0, the chapter
backfill's slowest steady state) before every Audible request, and the
environment can only widen it, never go under those figures. There is no
ramp, so there is no process-limit rebinding to guard: ten thousand requests
at about 1.35 s is under four hours, and a ramp would be machinery that earns
nothing here. Any 429, or five 5xx within two minutes, ends the run.

The run refuses to start unless AUDIBLE_PROXY_URL names a proxy whose
hostname contains "repair" (_verify_dedicated_proxy), so its traffic never
shares the API's exit or another job's. Give the exit that name through a
container name or a network alias. `plan` and `restore` make no Audible
request and need no proxy.

RUNBOOK. From the API image, DATABASE_URL set, one step at a time:

  1. Plan. Read-only; writes the frozen list and prints the counts.

         python -m scripts.repair_region_pollution plan --out /data/repair.list.json

     Send Shane the counts before going further. The list never changes
     after this; `plan` refuses to overwrite a file.
  2. Take a fresh database backup (scripts/backup.py) and confirm it landed.
  3. Rehearse. Real Audible requests, no writes, no backup file touched:

         python -m scripts.repair_region_pollution run --list /data/repair.list.json \\
             --backup /data/repair.backup.jsonl --dry-run --limit 20

  4. Run in batches, reading the log between them:

         python -m scripts.repair_region_pollution run --list ... --backup ... --limit 500
         python -m scripts.repair_region_pollution run --list ... --backup ... --resume-from 500

     A stop (SIGTERM, SIGINT, docker stop) finishes the book in flight and
     exits 3 after printing `RESUME CURSOR: N`. The cursor is the index into
     the frozen list. A restart that would begin at or before a book the
     backup already holds is refused, naming the index to resume from: a lost
     cursor stops the run, it never silently repeats the work from zero.
     `--pair ASIN:region` repairs one listed pair; a pair not in the list is
     refused. When the whole list is done the foreign series_author rows go.
  5. Books left unrepaired (placeholder, 404, empty) are logged and exit
     code 2. Run `plan` again to a new file to list what is still polluted.
  6. Restore. Puts back the first backed-up state of each pair, in a
     transaction per book, deleting what the repair wrote:

         python -m scripts.repair_region_pollution restore --backup /data/repair.backup.jsonl
         python -m scripts.repair_region_pollution restore --backup ... --dry-run --limit 20

     It needs the authors, series, narrators and genres the backup names to
     still exist, and fails loudly, rolling the book back, if one does not.

ENVIRONMENT.

    DATABASE_URL        required.
    AUDIBLE_PROXY_URL   required for `run`. Hostname must contain "repair".
    REPAIR_DELAY_MIN    0.7     pause floor in seconds, raise only.
    REPAIR_DELAY_MAX    2.0     pause ceiling in seconds, raise only.
    LOG_LEVEL           INFO    WARNING+ drops the RESUME CURSOR line.
    AXIOM_TOKEN         (unset) set to also ship logs to Axiom.
    AXIOM_DATASET       libex
    LOG_RETENTION_DAYS  7       0 keeps everything.

Exit codes: 0 finished, 1 aborted (throttled, outage, database error) or the
proxy guard refused the start, 2 finished with books left unrepaired, 3
stopped by a signal, 4 unusable arguments, or a list or backup that was
refused.
"""

# Standard library
import argparse
import asyncio
import fcntl
import hashlib
import json
import logging
import os
import random
import re
import signal
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Third party
from sqlalchemy import JSON, delete, insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

# Database
import app.db.session as db_session
from app.db.models import (
    Author,
    Book,
    Track,
    author_book,
    book_genre,
    book_narrator,
    book_series,
    series_author,
)

# Core
from app.core.logging import get_logger, setup_logging
from libex_core.audible.books import (
    MAX_ASINS_PER_REQUEST,
    fetch_products,
    filter_products,
    is_placeholder_record,
    normalize_products,
)
from libex_core.audible.chapters import (
    fetch_chapter_metadata,
    has_chapter_info,
    normalize_chapters,
)
from libex_core.exceptions import NotFoundException
from libex_core.storage.types import UTCDateTime
from libex_core.storage.write import entities

# Services
import app.services.audible as audible_service
from app.services.audible import audible_get
from app.services.cache import manager as cache_manager
from app.services.cache.manager import book_key, chapters_key
from app.services.db import writer

logger = get_logger()

Pair = tuple[str, str]

EXIT_OK = 0
EXIT_ABORTED = 1
EXIT_UNREPAIRED = 2
EXIT_STOPPED = 3
EXIT_REFUSED = 4

LIST_FORMAT = 1
BACKUP_FORMAT = 1

# Nine digits and a digit or X: the shape of an ISBN-10 used as an ASIN.
ISBN10 = re.compile(r"^[0-9]{9}[0-9X]$")
REGIONS = frozenset({"us", "uk", "ca", "au", "de", "fr", "it", "es", "jp", "in", "br"})

# The dialect the hosted writers are fixed to; write_track takes it by name.
_DIALECT = "postgresql"

# The link tables keyed by the book, in the order they are backed up and put
# back. Every one carries book_asin and book_region.
_LINK_TABLES = (
    ("author_book", author_book),
    ("book_narrator", book_narrator),
    ("book_series", book_series),
    ("book_genre", book_genre),
)


# ============================================================
# TUNABLES
# ============================================================

def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


# One request is fifty books at most, the endpoint's own ceiling; a larger
# value would be silently cut to fifty.
BATCH_SIZE = MAX_ASINS_PER_REQUEST

# The slowest steady state the chapter backfill runs at. The environment can
# raise either figure and never lower it below these: a rate under this is
# not a tuning choice for a script that exists to be gentle.
DELAY_FLOOR_MIN = 0.7
DELAY_FLOOR_MAX = 2.0
DELAY_MIN = max(DELAY_FLOOR_MIN, _env_float("REPAIR_DELAY_MIN", DELAY_FLOOR_MIN))
DELAY_MAX = max(DELAY_MIN, DELAY_FLOOR_MAX, _env_float("REPAIR_DELAY_MAX", DELAY_FLOOR_MAX))

# Any 429 ends the run. 5xx may be noise, but not this much of it.
ABORT_5XX_WITHIN = 5
ABORT_5XX_WINDOW_SECONDS = 120.0

PROGRESS_EVERY = 25


# ============================================================
# THE FROZEN LIST
# ============================================================

def _list_sha(pairs: list[Pair], series_authors: list[tuple[str, str, int]]) -> str:
    payload = json.dumps(
        {"pairs": [list(p) for p in pairs], "series_author": [list(r) for r in series_authors]},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class RepairList:
    """
    The books a run may touch, fixed when `plan` wrote them.

    The checksum covers the pairs and the series_author rows, so a list edited
    by hand, or truncated, is refused rather than run: a repair that replaces
    rows has to be exactly the audited set.
    """

    def __init__(
        self,
        pairs: list[Pair],
        series_authors: list[tuple[str, str, int]],
        counts: dict[str, Any],
        created_at: str,
    ) -> None:
        seen: set[Pair] = set()
        for asin, region in pairs:
            if not ISBN10.fullmatch(asin):
                raise ValueError(f"{asin!r} is not an ISBN-10-shaped ASIN; this script repairs nothing else")
            if region not in REGIONS:
                raise ValueError(f"{asin}:{region} names no known region")
            if (asin, region) in seen:
                raise ValueError(f"{asin}:{region} appears twice in the list")
            seen.add((asin, region))
        self.pairs = list(pairs)
        self.series_authors = list(series_authors)
        self.counts = counts
        self.created_at = created_at
        self.sha = _list_sha(self.pairs, self.series_authors)
        self._index = {pair: i for i, pair in enumerate(self.pairs)}

    def require(self, asin: str, region: str) -> int:
        """The pair's index in the list, or ValueError: nothing outside the
        list is ever touched, whoever asks."""
        try:
            return self._index[(asin, region)]
        except KeyError:
            raise ValueError(f"{asin}:{region} is not in the frozen list; refusing to touch it") from None

    def dump(self, path: Path) -> None:
        document = {
            "format": LIST_FORMAT,
            "created_at": self.created_at,
            "sha256": self.sha,
            "counts": self.counts,
            "pairs": [list(p) for p in self.pairs],
            "series_author": [list(r) for r in self.series_authors],
        }
        # "x" refuses to replace a list: the frozen set must not change under
        # a backup that was taken against it.
        with open(path, "x", encoding="utf-8") as handle:
            json.dump(document, handle, indent=1)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())

    @classmethod
    def load(cls, path: Path) -> "RepairList":
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
            if document["format"] != LIST_FORMAT:
                raise ValueError(f"list format {document['format']!r} is not {LIST_FORMAT}")
            loaded = cls(
                [(str(a), str(r)) for a, r in document["pairs"]],
                [(str(a), str(r), int(i)) for a, r, i in document["series_author"]],
                document["counts"],
                document["created_at"],
            )
            recorded = document["sha256"]
        except (OSError, KeyError, TypeError) as exc:
            raise ValueError(f"cannot read the list {path}: {type(exc).__name__}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise ValueError(f"the list {path} is not valid JSON: {exc}") from exc
        if loaded.sha != recorded:
            raise ValueError(f"the list {path} does not match its checksum; it was edited or truncated")
        return loaded


# ============================================================
# PLAN
# ============================================================

# Both predicates read the link tables directly and join nothing else but
# authors: a link's book_region already says which book it belongs to, and
# the foreign key guarantees that book exists.
_FOREIGN_AUTHOR_LINKS = text(
    "SELECT ab.book_asin AS asin, CAST(ab.book_region AS text) AS region, count(*) AS links "
    "FROM author_book ab JOIN authors a ON a.id = ab.author_id "
    "WHERE a.region <> ab.book_region GROUP BY ab.book_asin, ab.book_region"
)
_FOREIGN_SERIES_LINKS = text(
    "SELECT bs.book_asin AS asin, CAST(bs.book_region AS text) AS region, count(*) AS links "
    "FROM book_series bs WHERE bs.series_region <> bs.book_region "
    "GROUP BY bs.book_asin, bs.book_region"
)
_FOREIGN_SERIES_AUTHORS = text(
    "SELECT sa.series_asin, CAST(sa.series_region AS text) AS series_region, sa.author_id "
    "FROM series_author sa JOIN authors a ON a.id = sa.author_id "
    "WHERE a.region <> sa.series_region ORDER BY 1, 2, 3"
)


async def build_plan(session) -> RepairList:
    """
    Reads the polluted books and the foreign series_author rows and returns
    them as a RepairList. Read-only: the transaction is declared so, and given
    long enough to scan the link tables, which the app's 30 s statement
    timeout would not allow.
    """
    async with session.begin():
        await session.execute(text("SET TRANSACTION READ ONLY"))
        await session.execute(text("SET LOCAL statement_timeout = '900s'"))
        author_rows = (await session.execute(_FOREIGN_AUTHOR_LINKS)).all()
        series_rows = (await session.execute(_FOREIGN_SERIES_LINKS)).all()
        series_author_rows = (await session.execute(_FOREIGN_SERIES_AUTHORS)).all()

    with_author = {(r.asin, r.region) for r in author_rows}
    with_series = {(r.asin, r.region) for r in series_rows}
    polluted = with_author | with_series
    in_scope = sorted((p for p in polluted if ISBN10.fullmatch(p[0])), key=lambda p: (p[1], p[0]))
    out_of_scope = polluted - set(in_scope)

    by_region: dict[str, int] = {}
    for _, region in in_scope:
        by_region[region] = by_region.get(region, 0) + 1
    scoped = set(in_scope)

    counts = {
        "books_to_repair": len(in_scope),
        "by_region": dict(sorted(by_region.items())),
        "books_with_foreign_authors": len(with_author & scoped),
        "books_with_foreign_series": len(with_series & scoped),
        "books_with_both": len(with_author & with_series & scoped),
        "foreign_author_links": sum(r.links for r in author_rows if (r.asin, r.region) in scoped),
        "foreign_series_links": sum(r.links for r in series_rows if (r.asin, r.region) in scoped),
        "foreign_series_author_rows": len(series_author_rows),
        "polluted_books_not_isbn10_untouched": len(out_of_scope),
    }
    return RepairList(
        in_scope,
        [(r.series_asin, r.series_region, int(r.author_id)) for r in series_author_rows],
        counts,
        datetime.now(timezone.utc).isoformat(),
    )


# ============================================================
# ROW ENCODING
# ============================================================

def _encode_value(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime) else value


def _encode_row(row) -> dict[str, Any]:
    return {key: _encode_value(value) for key, value in dict(row).items()}


def _decode_row(table, row: dict[str, Any]) -> dict[str, Any]:
    """
    A backed-up row as the table takes it back.

    A JSON column whose value is None is left out so it inserts as SQL NULL:
    bound as None, a plain JSONB column would store the JSON value null, which
    is a different state, and the writers never produce it.
    """
    decoded: dict[str, Any] = {}
    for column in table.c:
        if column.key not in row:
            continue
        value = row[column.key]
        if isinstance(column.type, UTCDateTime) and isinstance(value, str):
            value = datetime.fromisoformat(value)
        if value is None and isinstance(column.type, JSON):
            continue
        decoded[column.key] = value
    return decoded


def _row_sort_key(row: dict[str, Any]) -> str:
    return json.dumps(row, sort_keys=True, default=str)


# ============================================================
# SNAPSHOT AND SUMMARY
# ============================================================

async def _snapshot(session, asin: str, region: str, *, lock: bool) -> dict[str, Any] | None:
    """
    Everything the store holds for one (asin, region): the book row, every
    link row, the track, and which of its links are foreign. None when the
    book is not stored. `lock` takes the row lock that keeps a concurrent
    writer from linking to the book between this read and the delete.
    """
    book_table = Book.__table__
    stmt = select(book_table).where(book_table.c.asin == asin, book_table.c.region == region)
    if lock:
        stmt = stmt.with_for_update()
    row = (await session.execute(stmt)).mappings().first()
    if row is None:
        return None

    links: dict[str, list[dict[str, Any]]] = {}
    for name, table in _LINK_TABLES:
        result = await session.execute(
            select(table).where(table.c.book_asin == asin, table.c.book_region == region)
        )
        links[name] = sorted((_encode_row(r) for r in result.mappings().all()), key=_row_sort_key)

    track_table = Track.__table__
    track = (await session.execute(
        select(track_table).where(track_table.c.asin == asin, track_table.c.region == region)
    )).mappings().first()

    foreign_authors = (await session.execute(
        select(author_book.c.author_id)
        .join(Author, Author.id == author_book.c.author_id)
        .where(
            author_book.c.book_asin == asin,
            author_book.c.book_region == region,
            Author.region != author_book.c.book_region,
        )
        .order_by(author_book.c.author_id)
    )).scalars().all()
    foreign_series = [
        [s["series_asin"], s["series_region"]]
        for s in links["book_series"] if s["series_region"] != region
    ]
    return {
        "book": _encode_row(row),
        "links": links,
        "track": None if track is None else _encode_row(track),
        "foreign": {"author_ids": list(foreign_authors), "series": foreign_series},
    }


_IDENTITY_FIELDS = frozenset({"asin", "region"})


def _summary(snapshot: dict[str, Any] | None) -> dict[str, int]:
    """The counts logged before and after a repair."""
    if snapshot is None:
        return {}
    chapters = (snapshot["track"] or {}).get("chapters")
    listing = chapters.get("chapters") if isinstance(chapters, dict) else None
    return {
        "authors": len(snapshot["links"]["author_book"]),
        "narrators": len(snapshot["links"]["book_narrator"]),
        "series": len(snapshot["links"]["book_series"]),
        "genres": len(snapshot["links"]["book_genre"]),
        "foreign_author_links": len(snapshot["foreign"]["author_ids"]),
        "foreign_series_links": len(snapshot["foreign"]["series"]),
        "chapters": len(listing) if isinstance(listing, list) else 0,
        "fields_filled": sum(
            1 for key, value in snapshot["book"].items()
            if key not in _IDENTITY_FIELDS and value is not None
        ),
    }


def _identity_values(before: dict[str, Any], checked_at: datetime) -> dict[str, Any]:
    """
    What is put back on the rewritten row: the original is_primary (the
    writer would set it from whichever other region now holds the ASIN) and
    created_at (the tie-break between primary rows), and the chapters stamp.
    """
    created = before["book"]["created_at"]
    return {
        "is_primary": bool(before["book"]["is_primary"]),
        "created_at": datetime.fromisoformat(created) if isinstance(created, str) else created,
        "chapters_checked_at": checked_at,
    }


def _backup_record(
    list_sha: str, index: int, asin: str, region: str, snapshot: dict[str, Any]
) -> dict[str, Any]:
    """One line of the backup: the whole prior state of one book."""
    return {
        "format": BACKUP_FORMAT,
        "type": "book",
        "list_sha": list_sha,
        "index": index,
        "asin": asin,
        "region": region,
        "backed_up_at": datetime.now(timezone.utc).isoformat(),
        "book": snapshot["book"],
        "links": snapshot["links"],
        "track": snapshot["track"],
        "foreign": snapshot["foreign"],
    }


# ============================================================
# THE BACKUP FILE
# ============================================================

class BackupFile:
    """
    The append-only backup. Every line is fsynced before append returns, and
    an exclusive lock on the file stops a second run from interleaving with
    this one.
    """

    def __init__(self, path: Path) -> None:
        created = not path.exists()
        self._handle = open(path, "ab")
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._handle.close()
            raise ValueError(f"{path} is locked by another run") from None
        # A crash can leave a final line cut short. Its delete cannot have
        # committed (the line is written first), so it is cut off here rather
        # than left to corrupt the record appended after it.
        if path.stat().st_size and not _ends_with_newline(path):
            self._handle.truncate(_last_newline_end(path))
        self._handle.flush()
        os.fsync(self._handle.fileno())
        if created:
            _fsync_directory(path.parent)

    def append(self, record: dict[str, Any]) -> None:
        line = json.dumps(record, sort_keys=True, separators=(",", ":"), default=str) + "\n"
        self._handle.write(line.encode("utf-8"))
        self._handle.flush()
        os.fsync(self._handle.fileno())

    def close(self) -> None:
        self._handle.close()


def _ends_with_newline(path: Path) -> bool:
    with open(path, "rb") as handle:
        handle.seek(-1, os.SEEK_END)
        return handle.read(1) == b"\n"


def _last_newline_end(path: Path) -> int:
    """The offset just past the last complete line, 0 if there is none."""
    return path.read_bytes().rfind(b"\n") + 1


def _fsync_directory(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def read_backup(path: Path) -> list[dict[str, Any]]:
    """
    Every record in a backup, in file order. A final line cut short by a crash
    is dropped, since its delete cannot have committed (the line is written
    first); any other unreadable line is an error, never skipped.
    """
    records: list[dict[str, Any]] = []
    try:
        raw = path.read_bytes().decode("utf-8")
    except OSError as exc:
        raise ValueError(f"cannot read the backup {path}: {exc}") from exc
    lines = raw.split("\n")
    for number, line in enumerate(lines, start=1):
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            if number == len(lines) and not raw.endswith("\n"):
                logger.warning("Repair: ignoring a torn final backup line", extra={"line": number})
                continue
            raise ValueError(f"backup line {number} is not valid JSON: {exc}") from exc
        if record.get("format") != BACKUP_FORMAT or record.get("type") not in ("book", "series_author"):
            raise ValueError(f"backup line {number} is not a recognised record")
        records.append(record)
    return records


def check_pair_unrepaired(records: list[dict[str, Any]], list_sha: str, index: int) -> None:
    """Refuses a single-pair run for a book the backup already holds, and a
    backup taken against a different list."""
    for record in records:
        if record["list_sha"] != list_sha:
            raise ValueError("the backup holds records from a different list; refusing to mix them")
        if record["type"] == "book" and record["index"] == index:
            raise ValueError(f"index {index} is already repaired in the backup; refusing to repeat it")


def check_resume(records: list[dict[str, Any]], list_sha: str, start: int) -> None:
    """
    Refuses a start that is not safe against what the backup already holds.

    The backup is the durable record of how far a run got, so it, not a
    remembered cursor, decides: a start at or before a repaired index would
    repeat finished work, which is what a lost cursor looks like, and a backup
    taken against a different list is the wrong file altogether.
    """
    highest = -1
    for record in records:
        if record["list_sha"] != list_sha:
            raise ValueError("the backup holds records from a different list; refusing to mix them")
        if record["type"] == "book":
            highest = max(highest, record["index"])
    if start <= highest:
        raise ValueError(
            f"the backup already holds repaired books up to index {highest}; starting at {start} "
            f"would repeat them. Pass --resume-from {highest + 1} (or later)"
        )


# ============================================================
# RUN STATE, PACING AND THE THROTTLE SIGNAL
# ============================================================

class _StopRequested(Exception):
    """A stop arrived before an Audible request that had not been sent."""


class _RunFailure(Exception):
    """A failure the run cannot step over: an outage, or a database error."""


class _Run:
    """Counters, the stop flag, and the abort reason."""

    def __init__(self) -> None:
        self.stopping = False
        self.abort_reason: str | None = None
        self.started = time.monotonic()
        self.repaired = 0
        self.dry_run_books = 0
        self.unrepaired: dict[str, int] = {}
        self.cursor = 0
        self._wake = asyncio.Event()

    def request_stop(self, *_args) -> None:
        if not self.stopping:
            logger.info("Repair: stop requested, finishing the book in flight")
        self.stopping = True
        self._wake.set()

    def abort(self, reason: str) -> None:
        if self.abort_reason is None:
            self.abort_reason = reason
        self.request_stop()

    def leave_alone(self, reason: str) -> None:
        self.unrepaired[reason] = self.unrepaired.get(reason, 0) + 1

    async def sleep(self, seconds: float) -> None:
        """Waits, waking at once if a stop arrives."""
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass


class _ThrottleSentinel(logging.Handler):
    """
    Watches the throttle line audible_get logs on every 429 and 5xx, retried
    ones included, keyed on its structured fields rather than its text.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.throttled = 0
        self.server_errors: deque[float] = deque()

    def emit(self, record: logging.LogRecord) -> None:
        status = getattr(record, "status_code", None)
        if status is None or not hasattr(record, "attempts_left"):
            return
        if status == 429:
            self.throttled += 1
        elif isinstance(status, int) and 500 <= status < 600:
            self.server_errors.append(time.monotonic())

    @property
    def sustained_server_errors(self) -> bool:
        now = time.monotonic()
        while self.server_errors and now - self.server_errors[0] > ABORT_5XX_WINDOW_SECONDS:
            self.server_errors.popleft()
        return len(self.server_errors) >= ABORT_5XX_WITHIN


def _check_abort(run: _Run, sentinel: _ThrottleSentinel) -> None:
    if sentinel.throttled:
        run.abort(f"Audible returned {sentinel.throttled} throttled response(s)")
    elif sentinel.sustained_server_errors:
        run.abort(f"{len(sentinel.server_errors)} upstream 5xx within {ABORT_5XX_WINDOW_SECONDS:.0f}s")


class _Audible:
    """
    The only place this script asks Audible anything. Every request is
    preceded by the pause, so no two ever go out closer together than
    DELAY_MIN, and a stop during the pause cancels the request unsent.
    """

    def __init__(self, run: _Run, get=audible_get) -> None:
        self._run = run
        self._get = get

    async def _pace(self) -> None:
        if self._run.stopping:
            raise _StopRequested()
        await self._run.sleep(random.uniform(DELAY_MIN, DELAY_MAX))
        if self._run.stopping:
            raise _StopRequested()

    async def products(self, asins: list[str], region: str) -> list[dict[str, Any]]:
        await self._pace()
        return await fetch_products(self._get, asins, region)

    async def chapters(self, asin: str, region: str) -> Any:
        await self._pace()
        return await fetch_chapter_metadata(self._get, asin, region)


# ============================================================
# THE REPAIR OF ONE BOOK
# ============================================================

async def _fetch_batch(
    audible: _Audible, asins: list[str], region: str
) -> tuple[dict[str, dict], set[str]]:
    """The batch's real products by ASIN, and which ASINs came back as
    placeholders. Anything but a 404 is an outage."""
    try:
        products = await audible.products(asins, region)
    except NotFoundException:
        products = []
    except _StopRequested:
        raise
    except Exception as exc:
        raise _RunFailure(f"product request failed: {type(exc).__name__}") from exc
    kept = {p["asin"]: p for p in filter_products(products) if p.get("asin")}
    placeholders = {p["asin"] for p in products if p.get("asin") and is_placeholder_record(p)}
    return kept, placeholders


async def _fetch_track(audible: _Audible, asin: str, region: str) -> dict[str, Any] | None:
    """The normalized chapter listing, or None for a legitimate empty answer
    (a 404, or a 200 with no listing). Anything else is an outage."""
    try:
        data = await audible.chapters(asin, region)
    except NotFoundException:
        return None
    except _StopRequested:
        raise
    except Exception as exc:
        raise _RunFailure(f"chapters request failed: {type(exc).__name__}") from exc
    if not has_chapter_info(data):
        return None
    return normalize_chapters(data, asin, region)


async def _replace_in_transaction(
    factory,
    backup: BackupFile,
    list_sha: str,
    index: int,
    asin: str,
    region: str,
    book: dict[str, Any],
    track: dict[str, Any] | None,
) -> tuple[dict[str, int], dict[str, int]] | None:
    """
    The one transaction. Returns the before and after summaries, or None when
    the book is no longer stored. Raises, rolled back, on any failure; the
    backup line written first is then for a book that did not change.
    """
    async with factory() as session:
        async with session.begin():
            before = await _snapshot(session, asin, region, lock=True)
            if before is None:
                return None
            backup.append(_backup_record(list_sha, index, asin, region, before))

            book_table = Book.__table__
            await session.execute(
                delete(book_table).where(book_table.c.asin == asin, book_table.c.region == region)
            )
            await writer.write_books(session, [book])
            if track is not None:
                stored = await entities.write_track(session, asin, track, region=region, dialect=_DIALECT)
                if stored is None:
                    raise RuntimeError("the rewritten book is not stored, so its chapters have nowhere to go")
            await session.execute(
                update(book_table)
                .where(book_table.c.asin == asin, book_table.c.region == region)
                .values(**_identity_values(before, datetime.now(timezone.utc)))
            )
            after = await _snapshot(session, asin, region, lock=False)
            if after is None:
                raise RuntimeError("the rewrite left no book stored")
    return _summary(before), _summary(after)


async def _invalidate(factory, asin: str, region: str) -> None:
    """Drops the two single-book cache entries. A failure is logged and left:
    the rows are right, and an entry expires on its TTL."""
    try:
        async with factory() as session:
            for key in (book_key(asin, region), chapters_key(asin, region)):
                await cache_manager.invalidate(session, key)
    except Exception as exc:
        logger.warning(
            "Repair: cache invalidation failed, the entries will expire on their TTL",
            extra={"asin": asin, "region": region, "error_type": type(exc).__name__},
        )


async def _repair_one(
    factory,
    backup: BackupFile | None,
    list_: RepairList,
    audible: _Audible,
    run: _Run,
    index: int,
    kept: dict[str, dict],
    placeholders: set[str],
    dry_run: bool,
) -> str:
    """One pair, start to finish. Returns its outcome. Raises _RunFailure or
    _StopRequested when the run cannot go on past this book."""
    asin, region = list_.pairs[index]
    list_.require(asin, region)

    product = kept.get(asin)
    if product is None:
        reason = "placeholder" if asin in placeholders else "no_product"
        logger.warning("Repair: no real product answer, row left as it was", extra={
            "asin": asin, "region": region, "index": index, "reason": reason,
        })
        run.leave_alone(reason)
        return reason

    try:
        book = (await normalize_products([product], region))[0]
    except Exception as exc:
        logger.warning("Repair: product could not be normalized, row left as it was", extra={
            "asin": asin, "region": region, "index": index, "error_type": type(exc).__name__,
        })
        run.leave_alone("unreadable")
        return "unreadable"
    if book.get("asin") != asin or not book.get("title"):
        logger.warning("Repair: answer is not this book, row left as it was", extra={
            "asin": asin, "region": region, "index": index,
        })
        run.leave_alone("no_product")
        return "no_product"

    track = await _fetch_track(audible, asin, region)
    offered = len(track["chapters"]) if track and isinstance(track.get("chapters"), list) else 0

    if dry_run:
        async with factory() as session:
            before = _summary(await _snapshot(session, asin, region, lock=False))
        if not before:
            logger.warning("Repair: book is not stored, nothing to replace", extra={
                "asin": asin, "region": region, "index": index,
            })
            run.leave_alone("not_stored")
            return "not_stored"
        run.dry_run_books += 1
        logger.info("Repair dry run: would replace", extra={
            "asin": asin, "region": region, "index": index, "before": before,
            "fresh_authors": len(book.get("authors") or []),
            "fresh_series": len(book.get("series") or []),
            "fresh_chapters": offered,
        })
        return "dry_run"

    if backup is None:
        raise RuntimeError("a real run has no backup file open")
    try:
        result = await _replace_in_transaction(
            factory, backup, list_.sha, index, asin, region, book, track
        )
    except Exception as exc:
        raise _RunFailure(
            f"the replacement of {asin}:{region} failed and was rolled back: {type(exc).__name__}"
        ) from exc
    if result is None:
        logger.warning("Repair: book is no longer stored, nothing to replace", extra={
            "asin": asin, "region": region, "index": index,
        })
        run.leave_alone("not_stored")
        return "not_stored"

    before, after = result
    run.repaired += 1
    logger.info("Repair: replaced", extra={
        "asin": asin, "region": region, "index": index, "before": before, "after": after,
        "chapters_written": offered,
    })
    await _invalidate(factory, asin, region)
    return "replaced"


# ============================================================
# THE RUN
# ============================================================

def _next_batch(pairs: list[Pair], start: int, end: int) -> int:
    """The end index of the batch beginning at `start`: consecutive pairs of
    one region, at most BATCH_SIZE of them. A book resolves only in its own
    marketplace, so a batch never mixes regions."""
    region = pairs[start][1]
    stop = start
    while stop < end and stop - start < BATCH_SIZE and pairs[stop][1] == region:
        stop += 1
    return stop


async def process(
    factory,
    backup: BackupFile | None,
    list_: RepairList,
    audible: _Audible,
    run: _Run,
    sentinel: _ThrottleSentinel,
    *,
    start: int,
    end: int,
    dry_run: bool,
) -> None:
    """
    Walks list_.pairs[start:end] in order, batch by batch. run.cursor is the
    index of the first book not yet finished, so it only ever moves past a
    book that was repaired or deliberately left alone.
    """
    run.cursor = start
    while run.cursor < end and not run.stopping:
        batch_end = _next_batch(list_.pairs, run.cursor, end)
        region = list_.pairs[run.cursor][1]
        asins = [list_.pairs[i][0] for i in range(run.cursor, batch_end)]
        try:
            kept, placeholders = await _fetch_batch(audible, asins, region)
        except _StopRequested:
            break
        except _RunFailure as failure:
            logger.error("Repair: Audible unavailable, cursor held", extra={
                "region": region, "cursor": run.cursor, "reason": str(failure),
            })
            run.abort(str(failure))
            break
        _check_abort(run, sentinel)
        if run.abort_reason:
            break

        while run.cursor < batch_end and not run.stopping:
            try:
                await _repair_one(
                    factory, backup, list_, audible, run, run.cursor, kept, placeholders, dry_run
                )
            except _StopRequested:
                break
            except _RunFailure as failure:
                logger.error("Repair: cannot continue, cursor held at this book", extra={
                    "cursor": run.cursor, "reason": str(failure),
                })
                run.abort(str(failure))
                break
            run.cursor += 1
            _check_abort(run, sentinel)
            if run.cursor % PROGRESS_EVERY == 0:
                logger.info("Repair: progress", extra={
                    "cursor": run.cursor, "of": len(list_.pairs), "repaired": run.repaired,
                    "left_alone": dict(run.unrepaired),
                    "elapsed_minutes": round((time.monotonic() - run.started) / 60, 1),
                })
        if run.abort_reason:
            break
        logger.info(f"RESUME CURSOR: {run.cursor}")


async def _delete_foreign_series_authors(
    factory, backup: BackupFile | None, list_: RepairList, dry_run: bool
) -> int:
    """
    Removes the listed series_author rows that still have a foreign author,
    each backed up first. They hang off a series, so replacing books does not
    touch them. The predicate is checked again here: a row that no longer
    matches is not deleted.
    """
    removed = 0
    for series_asin, series_region, author_id in list_.series_authors:
        async with factory() as session:
            async with session.begin():
                row = (await session.execute(
                    select(series_author)
                    .join(Author, Author.id == series_author.c.author_id)
                    .where(
                        series_author.c.series_asin == series_asin,
                        series_author.c.series_region == series_region,
                        series_author.c.author_id == author_id,
                        Author.region != series_author.c.series_region,
                    )
                    .with_for_update(of=series_author)
                )).mappings().first()
                if row is None:
                    continue
                if not dry_run:
                    if backup is None:
                        raise RuntimeError("a real run has no backup file open")
                    backup.append({
                        "format": BACKUP_FORMAT, "type": "series_author", "list_sha": list_.sha,
                        "backed_up_at": datetime.now(timezone.utc).isoformat(), "row": _encode_row(row),
                    })
                    await session.execute(delete(series_author).where(
                        series_author.c.series_asin == series_asin,
                        series_author.c.series_region == series_region,
                        series_author.c.author_id == author_id,
                    ))
        removed += 1
        logger.info("Repair: foreign series author link " + ("would go" if dry_run else "removed"), extra={
            "series_asin": series_asin, "series_region": series_region, "author_id": author_id,
        })
    return removed


def _verify_dedicated_proxy() -> None:
    """
    Refuses to start unless the configured transport is a proxy whose
    hostname contains "repair", so this run's requests never leave by the
    API's exit, another job's, or the container's own address. Reads the
    hosted client's transport_summary() rather than AUDIBLE_PROXY_URL: the
    value the client was actually built from, checked by hostname alone,
    because the real value may carry credentials and must never reach a log
    line or an exception. Logged before the SystemExit, which never reaches
    the log handlers by itself.

    Reached through the app.services.audible module object so a test that
    swaps the hosted instance is seen.
    """
    summary = audible_service._hosted_client.transport_summary()
    host = summary.host or ""
    if summary.mode != "proxy" or "repair" not in host:
        detail = f"host {host!r}" if summary.mode == "proxy" else summary.mode
        logger.error(
            "Repair: refusing to start, AUDIBLE_PROXY_URL does not name a repair-dedicated exit",
            extra={"proxy_host": host or "unset", "proxy_configured": summary.mode == "proxy"},
        )
        raise SystemExit(
            f"AUDIBLE_PROXY_URL ({detail}) does not name a repair-dedicated exit. Refusing to "
            "start against what may be a shared exit -- point this at an exit whose hostname "
            "contains 'repair' before starting."
        )


async def _run(args: argparse.Namespace) -> int:
    list_ = RepairList.load(args.list)
    pairs = list_.pairs
    start, end = 0, len(pairs)
    if args.pair is not None:
        start = list_.require(*args.pair)
        end = start + 1
    elif args.resume_from is not None:
        if not 0 <= args.resume_from <= len(pairs):
            raise ValueError(f"--resume-from {args.resume_from} is outside the list (0..{len(pairs)})")
        start = args.resume_from
    if args.limit is not None:
        end = min(end, start + args.limit)
    whole_list_done = args.pair is None and end == len(pairs)

    _verify_dedicated_proxy()

    backup: BackupFile | None = None
    if not args.dry_run:
        records = read_backup(args.backup) if args.backup.exists() else []
        if args.pair is None:
            check_resume(records, list_.sha, start)
        else:
            check_pair_unrepaired(records, list_.sha, start)
        backup = BackupFile(args.backup)

    run = _Run()
    sentinel = _ThrottleSentinel()
    logging.getLogger("libex").addHandler(sentinel)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, run.request_stop)

    logger.info("Repair: starting", extra={
        "books_in_list": len(pairs), "start": start, "end": end, "dry_run": args.dry_run,
        "delay_min": DELAY_MIN, "delay_max": DELAY_MAX, "batch_size": BATCH_SIZE,
        "proxy": audible_service._hosted_client.transport_summary().mode == "proxy",
    })
    factory = db_session.AsyncSessionFactory
    try:
        await process(
            factory, backup, list_, _Audible(run), run, sentinel,
            start=start, end=end, dry_run=args.dry_run,
        )
        if whole_list_done and not run.stopping:
            await _delete_foreign_series_authors(factory, backup, list_, args.dry_run)
    except Exception as exc:
        run.abort(f"unexpected {type(exc).__name__}")
        logger.exception("Repair: unexpected failure, stopping with the cursor held")
    finally:
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(sig)
        logging.getLogger("libex").removeHandler(sentinel)
        if backup is not None:
            backup.close()
        await db_session.engine.dispose()
        if run.abort_reason:
            logger.error("Repair: ABORTED", extra={"reason": run.abort_reason})
        logger.info("Repair: stopped", extra={
            "cursor": run.cursor, "of": len(pairs), "repaired": run.repaired,
            "dry_run_books": run.dry_run_books, "left_alone": dict(run.unrepaired),
            "throttled_responses": sentinel.throttled,
            "elapsed_minutes": round((time.monotonic() - run.started) / 60, 1),
        })
        logger.info(f"RESUME CURSOR: {run.cursor}")

    if run.abort_reason:
        return EXIT_ABORTED
    if run.stopping:
        return EXIT_STOPPED
    if run.unrepaired:
        return EXIT_UNREPAIRED
    return EXIT_OK


# ============================================================
# RESTORE
# ============================================================

async def _restore_book(session, record: dict[str, Any]) -> None:
    asin, region = record["asin"], record["region"]
    book_table = Book.__table__
    await session.execute(
        delete(book_table).where(book_table.c.asin == asin, book_table.c.region == region)
    )
    await session.execute(insert(book_table), [_decode_row(book_table, record["book"])])
    for name, table in _LINK_TABLES:
        rows = record["links"][name]
        if rows:
            await session.execute(insert(table), [_decode_row(table, r) for r in rows])
    if record["track"] is not None:
        await session.execute(insert(Track.__table__), [_decode_row(Track.__table__, record["track"])])


async def restore(
    factory, records: list[dict[str, Any]], run: _Run, *, dry_run: bool, limit: int | None
) -> int:
    """
    Puts back the first backed-up state of each pair (the polluted original,
    should a book have been repaired twice) and every backed-up series_author
    row. One transaction per book; a failure rolls that book back and stops.
    Returns how many books were restored.
    """
    first: dict[Pair, dict[str, Any]] = {}
    for record in records:
        if record["type"] == "book":
            first.setdefault((record["asin"], record["region"]), record)
    restored = 0
    for (asin, region), record in first.items():
        if run.stopping or (limit is not None and restored >= limit):
            break
        if dry_run:
            logger.info("Repair restore dry run: would restore", extra={
                "asin": asin, "region": region, "index": record["index"],
                "state": {"authors": len(record["links"]["author_book"]),
                          "series": len(record["links"]["book_series"])},
            })
            restored += 1
            continue
        try:
            async with factory() as session:
                async with session.begin():
                    await _restore_book(session, record)
        except Exception as exc:
            logger.error("Repair restore: failed, that book was rolled back", extra={
                "asin": asin, "region": region, "error_type": type(exc).__name__,
            })
            run.abort(f"restore of {asin}:{region} failed: {type(exc).__name__}")
            break
        restored += 1
        logger.info("Repair restore: restored", extra={"asin": asin, "region": region})
        await _invalidate(factory, asin, region)

    if limit is None and not run.stopping:
        for record in records:
            if record["type"] != "series_author":
                continue
            if dry_run:
                continue
            table_row = _decode_row(series_author, record["row"])
            async with factory() as session:
                async with session.begin():
                    await session.execute(pg_insert(series_author).values(table_row).on_conflict_do_nothing())
    return restored


async def _restore(args: argparse.Namespace) -> int:
    records = read_backup(args.backup)
    run = _Run()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, run.request_stop)
    try:
        count = await restore(
            db_session.AsyncSessionFactory, records, run, dry_run=args.dry_run, limit=args.limit
        )
    finally:
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(sig)
        await db_session.engine.dispose()
    logger.info("Repair restore: finished", extra={"books": count, "dry_run": args.dry_run})
    if run.abort_reason:
        return EXIT_ABORTED
    return EXIT_STOPPED if run.stopping else EXIT_OK


async def _plan(args: argparse.Namespace) -> int:
    try:
        async with db_session.AsyncSessionFactory() as session:
            plan = await build_plan(session)
    finally:
        await db_session.engine.dispose()
    plan.dump(args.out)
    logger.info("Repair plan written", extra={"out": str(args.out), "sha256": plan.sha, **plan.counts})
    print(json.dumps({"out": str(args.out), "sha256": plan.sha, **plan.counts}, indent=1))
    return EXIT_OK


# ============================================================
# ENTRY POINT
# ============================================================

def _pair_arg(text_value: str) -> Pair:
    asin, sep, region = text_value.strip().partition(":")
    if not sep or not asin or region not in REGIONS:
        raise argparse.ArgumentTypeError(f"{text_value!r} must be ASIN:region with a known region")
    return asin, region


def _positive(text_value: str) -> int:
    value = int(text_value)
    if value < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Repair books polluted across regions.")
    modes = parser.add_subparsers(dest="mode", required=True)

    plan = modes.add_parser("plan", help="Read-only. Write the frozen list and print the counts.")
    plan.add_argument("--out", type=Path, required=True, help="Where to write the list; must not exist.")

    run = modes.add_parser("run", help="Replace the listed books from Audible.")
    run.add_argument("--list", type=Path, required=True, help="The frozen list from `plan`.")
    run.add_argument("--backup", type=Path, help="Append-only backup file; required unless --dry-run.")
    run.add_argument("--dry-run", action="store_true", help="Fetch and report; write nothing.")
    run.add_argument("--limit", type=_positive, help="At most this many books this run.")
    run.add_argument("--resume-from", type=int, help="List index to start at, from RESUME CURSOR.")
    run.add_argument("--pair", type=_pair_arg, help="Repair only this listed ASIN:region.")

    back = modes.add_parser("restore", help="Put the backed-up state back.")
    back.add_argument("--backup", type=Path, required=True)
    back.add_argument("--dry-run", action="store_true")
    back.add_argument("--limit", type=_positive)
    return parser


def main() -> None:
    args = _parser().parse_args()
    # get_logger only fetches the logger. Without this no handler is attached
    # and a standalone script emits nothing at all.
    setup_logging()

    if args.mode == "run" and not args.dry_run and args.backup is None:
        raise SystemExit("run needs --backup unless --dry-run")

    handlers = {"plan": _plan, "run": _run, "restore": _restore}
    try:
        code = asyncio.run(handlers[args.mode](args))
    except FileExistsError as exc:
        logger.error("Repair: refused", extra={"reason": f"{exc.filename} already exists"})
        raise SystemExit(EXIT_REFUSED) from exc
    except ValueError as exc:
        logger.error("Repair: refused", extra={"reason": str(exc)})
        raise SystemExit(EXIT_REFUSED) from exc
    if code:
        raise SystemExit(code)


if __name__ == "__main__":
    main()
