"""
The seam between the one set of SQL Libex writes and the two databases that
run it.

Postgres is the hosted backend, and the statements the hosted writer emits must
not move by a byte. SQLite is the embedded one and lacks a few of the things
those statements lean on: a Unicode `lower`, jsonb's containment and
concatenation operators, and an enforced foreign key. (Postgres's `btrim` is
not among what is supplied: SQLite's own `trim` takes the same set argument,
and the merge builders spell it that way on SQLite.) This module supplies what
SQLite is missing, as functions registered on every connection, and
`DialectVariant`, which lets one expression carry a Postgres spelling and a
SQLite spelling and chooses between them when the statement is compiled.

Nothing here talks to Postgres. The Postgres spelling of every expression is
the hosted one, untouched, and it is the default: a dialect that is not SQLite
gets it.
"""

# Standard library
import json
import sqlite3
import weakref
from collections.abc import Callable
from decimal import Decimal
from typing import Any

# Third party
from sqlalchemy import event
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql import ColumnElement
from sqlalchemy.sql.visitors import InternalTraversal

# SQLite 3.35 is the first release with RETURNING and the last this code
# reasons about; older ones lack features the writer's statements use.
SQLITE_MINIMUM = (3, 35, 0)

# How long a connection waits for another writer before giving up, in
# milliseconds. Two processes sharing one file (a CLI run beside a long-lived
# embedder) queue rather than fail on the first overlap.
BUSY_TIMEOUT_MS = 5000

# Execution option that asks for a write transaction. A connection that
# carries it opens with BEGIN IMMEDIATE, taking SQLite's write lock up front,
# which is the SQLite spelling of the row lock Postgres takes on the first
# write. Without it a transaction that read and then wrote could find the
# lock already taken and fail instead of waiting. Reads open with plain BEGIN
# so they never queue behind each other.
WRITE_OPTION = "libex_sqlite_write"

JSON_CONTAINS = "libex_json_contains"
JSON_MERGE = "libex_json_merge"


class SQLiteTooOld(RuntimeError):
    """The SQLite library linked into Python is older than SQLITE_MINIMUM."""


def require_sqlite_version(version: str | None = None) -> None:
    """Raise SQLiteTooOld, naming both versions, if SQLite is below the floor.
    Checks the library Python is linked against unless a version is given."""
    version = version or sqlite3.sqlite_version
    parsed = tuple(int(part) for part in version.split(".")[:3])
    if parsed < SQLITE_MINIMUM:
        floor = ".".join(str(part) for part in SQLITE_MINIMUM)
        raise SQLiteTooOld(
            f"local storage needs SQLite {floor} or newer, but this Python is "
            f"linked against SQLite {version}"
        )


# ------------------------------------------------------------
# Registered SQL functions
# ------------------------------------------------------------

def sqlite_lower(value: Any) -> Any:
    """Lower-cases text the way Postgres lower() does on a UTF-8 database.

    Postgres maps each character on its own through the simple (one-to-one)
    lower-case table; it applies no context rule and no full mapping. Python's
    str.lower() differs in exactly two places, both of which this undoes: it
    turns a final capital sigma into the final form (a context rule), and it
    expands U+0130 to two characters (a full mapping). It is neither casefold
    nor NFKC: the sharp s stays itself, and fullwidth forms stay distinct.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        # SQLite's own lower reads a number as its text; anything else is
        # not text and is returned as it came.
        return str(value).lower() if isinstance(value, (int, float)) else value
    if value.isascii():
        return value.lower()
    return "".join(_simple_lower(ch) for ch in value)


def _simple_lower(ch: str) -> str:
    lowered = ch.lower()
    # A character whose full mapping is longer than one character has the first
    # of them as its simple mapping (U+0130 is the only one).
    return lowered[0] if len(lowered) > 1 else lowered


def _reject_constant(name: str) -> None:
    raise ValueError(f"{name} is not valid JSON")


# How deep a stored JSON document may nest before it is refused. Far beyond
# anything the extras builder lets in (32), far inside the interpreter's own
# recursion limit, so a pathological document fails with a clear error.
MAX_JSON_DEPTH = 200


def _load(text: str | bytes | int | float) -> Any:
    # SQLite gives a JSON column numeric affinity, so a stored document that is
    # a bare number comes back as a number, not as the text it was written as.
    if isinstance(text, (int, float)):
        text = str(text)
    # Floats load as Decimal so a number compares and re-serialises with the
    # digits it arrived with, as jsonb's numeric does.
    try:
        document = json.loads(text, parse_float=Decimal, parse_constant=_reject_constant)
    except RecursionError:
        raise ValueError(f"JSON document nests deeper than {MAX_JSON_DEPTH} levels") from None
    _check_depth(document)
    return document


def _check_depth(document: Any) -> None:
    # Iterative, so the check itself cannot hit the limit it guards.
    stack = [(document, 1)]
    while stack:
        value, depth = stack.pop()
        if isinstance(value, (dict, list)):
            if depth > MAX_JSON_DEPTH:
                raise ValueError(f"JSON document nests deeper than {MAX_JSON_DEPTH} levels")
            children = value.values() if isinstance(value, dict) else value
            stack.extend((child, depth + 1) for child in children)


def _dump(value: Any) -> str:
    # Callers pass only documents _load has already depth-checked, and a merge
    # of two of them is at most as deep as the deeper one.
    if isinstance(value, dict):
        return "{" + ",".join(f"{json.dumps(k)}:{_dump(v)}" for k, v in value.items()) + "}"
    if isinstance(value, list):
        return "[" + ",".join(_dump(v) for v in value) + "]"
    if isinstance(value, Decimal):
        return str(value)
    return json.dumps(value)


def _is_container(value: Any) -> bool:
    return isinstance(value, (dict, list))


def _scalar_equal(left: Any, right: Any) -> bool:
    # jsonb keeps true, 1 and "1" apart; Python's == does not keep True and 1
    # apart, so the booleans are settled first.
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left == right
    if left is None or right is None:
        return left is None and right is None
    if isinstance(left, str) or isinstance(right, str):
        return isinstance(left, str) and isinstance(right, str) and left == right
    return left == right


def _deep_contains(left: Any, right: Any) -> bool:
    """jsonb's @>: every key, element and value of right is found in left."""
    if isinstance(right, dict):
        if not isinstance(left, dict):
            return False
        for key, wanted in right.items():
            if key not in left:
                return False
            held = left[key]
            if _is_container(wanted) or _is_container(held):
                if type(wanted) is not type(held) or not _deep_contains(held, wanted):
                    return False
            elif not _scalar_equal(held, wanted):
                return False
        return True
    if isinstance(right, list):
        if not isinstance(left, list):
            return False
        for wanted in right:
            if _is_container(wanted):
                if not any(
                    type(held) is type(wanted) and _deep_contains(held, wanted) for held in left
                ):
                    return False
            elif not any(not _is_container(held) and _scalar_equal(held, wanted) for held in left):
                return False
        return True
    # right is a scalar: an array contains it as an element, a scalar only
    # contains an equal scalar, an object never does.
    if isinstance(left, list):
        return any(not _is_container(held) and _scalar_equal(held, right) for held in left)
    return not _is_container(left) and _scalar_equal(left, right)


def sqlite_json_contains(existing: str | bytes | None, new: str | bytes | None) -> int | None:
    """jsonb's `existing @> new` over JSON text; NULL in, NULL out."""
    if existing is None or new is None:
        return None
    return int(_deep_contains(_load(existing), _load(new)))


def sqlite_json_merge(existing: str | bytes | None, new: str | bytes | None) -> str | None:
    """jsonb's `existing || new` over JSON text; NULL in, NULL out.

    Two objects merge key by key with new winning a clash and nothing below the
    top level combined. That is jsonb's behaviour, kept exactly so both
    backends give the same row, including the shallow-merge hole recorded in
    `libex_core.storage.merge.extras_union`; the containment guard there
    narrows it and does not close it. Anything else follows the
    same rules jsonb applies: an empty object or array yields the other side,
    and otherwise both sides are taken as arrays (a lone value as a one-element
    array) and joined.
    """
    if existing is None or new is None:
        return None
    left, right = _load(existing), _load(new)
    if isinstance(left, dict) and isinstance(right, dict):
        return _dump({**left, **right})
    return _dump((left if isinstance(left, list) else [left]) + (right if isinstance(right, list) else [right]))


# (name, argument count, function). All are deterministic, which lets SQLite
# use them inside indexes and lets its planner fold them.
SQLITE_FUNCTIONS: tuple[tuple[str, int, Callable[..., Any]], ...] = (
    ("lower", 1, sqlite_lower),
    (JSON_CONTAINS, 2, sqlite_json_contains),
    (JSON_MERGE, 2, sqlite_json_merge),
)


# ------------------------------------------------------------
# Engine setup
# ------------------------------------------------------------

_configured: "weakref.WeakSet[Any]" = weakref.WeakSet()


def _is_memory(url_database: str | None) -> bool:
    return not url_database or url_database == ":memory:" or url_database.startswith("file::memory:")


def configure_sqlite(
    engine: Any,
    *,
    busy_timeout_ms: int = BUSY_TIMEOUT_MS,
    wal: bool | None = None,
) -> None:
    """Prepares a SQLite engine, sync or async, to run Libex's SQL.

    Registers, on every connection the engine opens: the functions in
    SQLITE_FUNCTIONS; foreign key enforcement, which SQLite leaves off; a busy
    timeout; synchronous=NORMAL; and, for a file database (`wal=None` decides
    from the URL, in-memory ones have no journal to switch), write-ahead
    logging so a reader does not block the writer.

    Also takes over transaction control, per SQLAlchemy's documented recipe for
    pysqlite and aiosqlite: the driver's own implicit BEGIN is switched off,
    and the engine emits it instead. Left to the driver, begin_nested's
    SAVEPOINT is not honoured, and a rollback to it undoes nothing. A
    connection carrying the WRITE_OPTION execution option begins IMMEDIATE.

    Raises SQLiteTooOld below SQLITE_MINIMUM and ValueError on any other
    dialect. Safe to call once per engine; a second call is a no-op.
    """
    require_sqlite_version()
    sync_engine = getattr(engine, "sync_engine", engine)
    if sync_engine.dialect.name != "sqlite":
        raise ValueError(f"configure_sqlite needs a SQLite engine, got {sync_engine.dialect.name!r}")
    if sync_engine in _configured:
        return
    _configured.add(sync_engine)

    use_wal = (not _is_memory(sync_engine.url.database)) if wal is None else wal
    timeout = int(busy_timeout_ms)

    @event.listens_for(sync_engine, "connect")
    def _on_connect(dbapi_connection, connection_record):
        # The driver's own implicit BEGIN is turned off; see the docstring.
        dbapi_connection.isolation_level = None
        for name, arity, function in SQLITE_FUNCTIONS:
            dbapi_connection.create_function(name, arity, function, deterministic=True)
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute(f"PRAGMA busy_timeout={timeout}")
            cursor.execute("PRAGMA synchronous=NORMAL")
            if use_wal:
                cursor.execute("PRAGMA journal_mode=WAL")
        finally:
            cursor.close()

    @event.listens_for(sync_engine, "begin")
    def _on_begin(connection):
        write = connection.get_execution_options().get(WRITE_OPTION)
        connection.exec_driver_sql("BEGIN IMMEDIATE" if write else "BEGIN")


# ------------------------------------------------------------
# One expression, two spellings
# ------------------------------------------------------------

class DialectVariant(ColumnElement):
    """An expression with a SQLite spelling and a default one.

    `default` is what every dialect but SQLite compiles, which for Libex is the
    exact expression the hosted writer has always emitted. `sqlite` replaces it
    on SQLite alone. Both stay in the statement's cache key, so a compiled
    statement is never reused across the two.
    """

    __visit_name__ = "libex_dialect_variant"
    inherit_cache = True
    _traverse_internals = [
        ("default", InternalTraversal.dp_clauseelement),
        ("sqlite", InternalTraversal.dp_clauseelement),
    ]

    def __init__(self, default: ColumnElement, sqlite: ColumnElement):
        self.default = default
        self.sqlite = sqlite
        self.type = default.type

    @property
    def _anon_name_label(self):
        # An unlabelled use of this expression (a RETURNING list, say) is
        # named, and numbered, as the bare default would have been, so the
        # Postgres statement stays identical to the one without the wrapper.
        return self.default._anon_name_label

    @property
    def _from_objects(self):
        # Without this a SELECT or WHERE using the expression would not see
        # the table its columns come from and leave it out of FROM.
        return [*self.default._from_objects, *self.sqlite._from_objects]

    def _bind_param(self, operator, obj, type_=None, expanding=False):
        # A literal compared against this expression is bound as it would be
        # against the default, name included, so the Postgres statement names
        # its binds exactly as the hosted one does.
        return self.default._bind_param(operator, obj, type_=type_, expanding=expanding)

    def self_group(self, against=None):
        return DialectVariant(self.default.self_group(against), self.sqlite.self_group(against))


@compiles(DialectVariant)
def _compile_dialect_variant(element: DialectVariant, compiler, **kw) -> str:
    branch = element.sqlite if compiler.dialect.name == "sqlite" else element.default
    return compiler.process(branch, **kw)
