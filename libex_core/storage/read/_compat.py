"""
Constructs whose SQL differs by backend, chosen when the statement compiles.

Each element wraps the expression the hosted app has always issued. Postgres,
and any dialect without a variant below, compiles that expression untouched,
so the Postgres SQL is byte for byte what it was before SQLite was supported.
SQLite compiles a different spelling that selects the same rows.

Where SQLite cannot reproduce Postgres exactly the difference is stated on the
element rather than approximated away.
"""

# Third party
from sqlalchemy import Float, String, bindparam, case, cast
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.sql.visitors import InternalTraversal


def dialect_name(session) -> str:
    """The backend a session talks to, "unknown" when that cannot be read."""
    bind = getattr(session, "bind", None)
    name = getattr(getattr(bind, "dialect", None), "name", None)
    return name if isinstance(name, str) else "unknown"


class _Wrapped(ColumnElement):
    """A boolean or scalar expression carried as-is until compile time."""

    inherit_cache = True
    _traverse_internals = [
        ("_expr", InternalTraversal.dp_clauseelement),
        ("type", InternalTraversal.dp_type),
    ]

    def __init__(self, expr: ColumnElement):
        self._expr = expr
        self.type = expr.type

    @property
    def _from_objects(self):
        return self._expr._from_objects

    def self_group(self, against=None):
        # Left bare so a backend without a native boolean does not append
        # "= 1" to the SQL written below; the variants parenthesise themselves.
        return self


@compiles(_Wrapped)
def _compile_wrapped(element, compiler, **kw):
    return compiler.process(element._expr, **kw)


class ILike(_Wrapped):
    """`column ILIKE pattern`, case-insensitive.

    SQLite has no ILIKE. The variant lowers both sides and states backslash as
    the escape character, because that is the one Postgres uses by default and
    SQLite has none unless told: without it a backslash in the search text is a
    literal on SQLite and an escape on Postgres. The lowering uses whatever
    lower() the connection has, so non-ASCII text folds as well as that
    function does; SQLite's own is ASCII only.

    A pattern ending in a lone backslash is an error on Postgres and simply
    matches nothing on SQLite.
    """

    inherit_cache = True

    def __init__(self, column, pattern: str):
        super().__init__(column.ilike(pattern))


@compiles(ILike, "sqlite")
def _compile_ilike_sqlite(element, compiler, **kw):
    expr = element._expr
    return "(lower(%s) LIKE lower(%s) ESCAPE '\\')" % (
        compiler.process(expr.left, **kw),
        compiler.process(expr.right, **kw),
    )


class JsonListContains(_Wrapped):
    """True when a JSON array column holds `value` as a string element.

    Postgres: `column @> '["value"]'`. SQLite: an EXISTS over json_each that
    matches text elements only, so a number is not equal to its digits, as
    with `@>`. A column that is not an array matches nothing on SQLite and
    nothing on Postgres either, except that Postgres also treats a bare string
    equal to `value` as containing it; the stored shape is always an array.
    """

    inherit_cache = True

    _traverse_internals = _Wrapped._traverse_internals + [
        ("_column", InternalTraversal.dp_clauseelement),
        ("_value", InternalTraversal.dp_clauseelement),
    ]

    def __init__(self, column, value: str):
        super().__init__(column.contains([value]))
        self._column = column
        self._value = bindparam(None, value, type_=String(), unique=True)


@compiles(JsonListContains, "sqlite")
def _compile_list_contains_sqlite(element, compiler, **kw):
    column = compiler.process(element._column, **kw)
    value = compiler.process(element._value, **kw)
    return "".join((
        "(EXISTS (SELECT 1 FROM json_each(", column, ") AS _je WHERE json_type(",
        column, ") = 'array' AND _je.type = 'text' AND _je.value = ", value, "))",
    ))


class JsonHasKey(_Wrapped):
    """True when a JSON column has `key` at its top level (`column ? key`).

    Mirrors the three shapes Postgres accepts: an object with that key, an
    array holding that string, or a bare string equal to it. SQLite compares
    through json_each and json_extract, so any key is matched literally with
    no path syntax to escape.
    """

    inherit_cache = True

    _traverse_internals = _Wrapped._traverse_internals + [
        ("_column", InternalTraversal.dp_clauseelement),
        ("_value", InternalTraversal.dp_clauseelement),
    ]

    def __init__(self, column, key: str):
        super().__init__(column.has_key(key))
        self._column = column
        self._value = bindparam(None, key, type_=String(), unique=True)


@compiles(JsonHasKey, "sqlite")
def _compile_has_key_sqlite(element, compiler, **kw):
    column = compiler.process(element._column, **kw)
    key = compiler.process(element._value, **kw)
    return "".join((
        "(EXISTS (SELECT 1 FROM json_each(", column, ") AS _je WHERE json_type(",
        column, ") = 'object' AND _je.key = ", key, ") OR EXISTS (SELECT 1 FROM json_each(",
        column, ") AS _je WHERE json_type(", column, ") = 'array' AND _je.type = 'text' AND _je.value = ",
        key, ") OR (json_type(", column, ") = 'text' AND json_extract(", column, ", '$') = ", key, "))",
    ))


class NumericPosition(_Wrapped):
    """A series position as a number when it is one, otherwise NULL.

    A numeric position is digits with an optional one-dot fraction, which is
    what `^\\d+(\\.\\d+)?$` accepts on Postgres. SQLite has no regular
    expression operator without a registered function, so the variant states
    the same shape with instr, substr and GLOB. Digits are ASCII 0-9 in both.
    """

    inherit_cache = True

    _traverse_internals = _Wrapped._traverse_internals + [
        ("_column", InternalTraversal.dp_clauseelement),
    ]

    def __init__(self, column):
        super().__init__(
            case(
                (column.op("~")(r"^\d+(\.\d+)?$"), cast(column, Float)),
                else_=None,
            )
        )
        self._column = column


@compiles(NumericPosition, "sqlite")
def _compile_numeric_position_sqlite(element, compiler, **kw):
    t = compiler.process(element._column, **kw)
    dot = f"instr({t}, '.')"
    whole = f"{t} NOT GLOB '*[^0-9]*'"
    head = f"substr({t}, 1, {dot} - 1) NOT GLOB '*[^0-9]*'"
    tail = f"substr({t}, {dot} + 1)"
    fraction = f"({dot} > 1 AND {head} AND {tail} <> '' AND {tail} NOT GLOB '*[^0-9]*')"
    return (
        f"CASE WHEN ({t} IS NOT NULL AND {t} <> '' AND ({whole} OR {fraction})) "
        f"THEN CAST({t} AS FLOAT) ELSE NULL END"
    )


class AscNullsLast(_Wrapped):
    """`column ASC`, with NULLs last.

    Postgres puts NULLs last on an ascending sort without being asked, so the
    hosted statement says only ASC. SQLite puts them first, so its variant
    says NULLS LAST outright; the rows then come back in the Postgres order.
    """

    inherit_cache = True

    def __init__(self, column):
        super().__init__(column.asc())


@compiles(AscNullsLast, "sqlite")
def _compile_asc_nulls_last_sqlite(element, compiler, **kw):
    return compiler.process(element._expr, **kw) + " NULLS LAST"


__all__ = [
    "AscNullsLast",
    "ILike",
    "JsonHasKey",
    "JsonListContains",
    "NumericPosition",
    "dialect_name",
]
