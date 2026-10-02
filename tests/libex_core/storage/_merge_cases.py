"""
The old-value / new-value table both backends are run through. Each row names
what the stored column must hold afterwards, so a pair of backends agreeing on
the wrong answer fails too.

JSON rows use Python values; NULL stands for SQL NULL and JNULL for the JSON
value null, which are different things in a jsonb column.
"""

from libex_core.storage.merge import BLANK_CHARS


class _Marker:
    def __init__(self, label):
        self.label = label

    def __repr__(self):
        return self.label


NULL = _Marker("NULL")
JNULL = _Marker("JNULL")

# (id, stored, incoming, expected)
COALESCE = [
    ("null-null", None, None, None),
    ("stored-null", "a", None, "a"),
    ("null-new", None, "b", "b"),
    ("new-wins", "a", "b", "b"),
    ("empty-is-an-answer", "a", "", ""),
    ("space-is-an-answer", "a", " ", " "),
]

ANSWERED = [
    ("null-incoming", "keep", None, "keep"),
    ("empty-incoming", "keep", "", "keep"),
    ("null-stays-null-on-empty", None, "", None),
    ("null-stays-null-on-tab", None, "\t", None),
    ("null-takes-text", None, "x", "x"),
    ("empty-stored-takes-text", "", "x", "x"),
    ("text-replaces-text", "keep", "new", "new"),
    ("padded-text-written-verbatim", "keep", "  x  ", "  x  "),
    ("nbsp-padded-text-verbatim", "keep", " x ", " x "),
    ("zero-width-space-is-an-answer", "keep", "​", "​"),
    ("bom-is-an-answer", "keep", "﻿", "﻿"),
    ("file-separator-is-an-answer", "keep", "\x1c", "\x1c"),
    ("fullwidth-text", "keep", "ｘ", "ｘ"),
    ("cjk-text", "keep", "日本語", "日本語"),
    *[
        (f"blank-U+{ord(ch):04X}", "keep", ch * 2, "keep")
        for ch in BLANK_CHARS
    ],
    ("every-blank-at-once", "keep", BLANK_CHARS, "keep"),
]

LONGER_WINS = [
    ("null-null", None, None, None),
    ("null-empty", None, "", None),
    ("null-blank", None, " \t　", None),
    ("null-takes-text", None, "a", "a"),
    ("longer-stored-kept", "abc", "ab", "abc"),
    ("tie-keeps-stored", "abc", "xyz", "abc"),
    ("longer-incoming-wins", "ab", "abc", "abc"),
    ("padding-not-counted", "abc", "  ab  ", "abc"),
    ("padded-winner-verbatim", "a", "  ab  ", "  ab  "),
    ("all-blank-never-wins", "abc", "　　　　", "abc"),
    ("empty-stored-replaced", "", "a", "a"),
    ("empty-empty", "", "", ""),
    ("non-bmp-counted-by-character", "abc", "\U0001f600\U0001f600\U0001f600\U0001f600", "\U0001f600\U0001f600\U0001f600\U0001f600"),
    ("non-bmp-stored-longer", "\U0001f600\U0001f600\U0001f600", "abcd", "abcd"),
    ("non-bmp-tie", "\U0001f600\U0001f600", "ab", "\U0001f600\U0001f600"),
    ("combining-counts-two", "ab", "é", "ab"),
    ("combining-beats-one", "a", "é", "é"),
    ("cjk", "日本", "日本語", "日本語"),
    ("zero-width-counts", "ab", "​​​", "​​​"),
]

EXTRAS = [
    ("null-null", NULL, NULL, NULL),
    ("null-takes-empty", NULL, {}, {}),
    ("null-takes-blob", NULL, {"a": 1}, {"a": 1}),
    ("incoming-null-keeps-blob", {"a": 1}, NULL, {"a": 1}),
    ("incoming-null-keeps-empty", {}, NULL, {}),
    ("empty-incoming-keeps-blob", {"a": 1}, {}, {"a": 1}),
    ("empty-stored-takes-blob", {}, {"a": 1}, {"a": 1}),
    ("subset-keeps-stored", {"a": 1, "b": 2}, {"a": 1}, {"a": 1, "b": 2}),
    ("superset-grows", {"a": 1}, {"a": 1, "b": 2}, {"a": 1, "b": 2}),
    ("changed-value-replaced", {"a": 1}, {"a": 2}, {"a": 2}),
    ("disjoint-keys-union", {"a": 1}, {"b": 2}, {"a": 1, "b": 2}),
    ("nested-shrink-refused", {"a": {"x": 1, "y": 2}}, {"a": {"x": 1}}, {"a": {"x": 1, "y": 2}}),
    ("nested-grow", {"a": {"x": 1}}, {"a": {"x": 1, "y": 2}}, {"a": {"x": 1, "y": 2}}),
    ("nested-disjoint-replaced-shallowly", {"a": {"x": 1}}, {"a": {"y": 2}}, {"a": {"y": 2}}),
    (
        "mixed-nested-and-new-key",
        {"a": {"x": 1, "y": 2}, "b": 1},
        {"a": {"x": 9}, "c": 3},
        {"a": {"x": 9}, "b": 1, "c": 3},
    ),
    ("deep-nested-shrink-refused", {"a": {"b": {"c": 1, "d": 2}}}, {"a": {"b": {"c": 1}}}, {"a": {"b": {"c": 1, "d": 2}}}),
    ("list-subset-refused", {"a": [1, 2, 3]}, {"a": [1, 2]}, {"a": [1, 2, 3]}),
    ("list-reordered-subset-refused", {"a": [1, 2, 3]}, {"a": [3, 1]}, {"a": [1, 2, 3]}),
    ("list-replaced", {"a": [1, 2]}, {"a": [3]}, {"a": [3]}),
    ("list-grows", {"a": [1]}, {"a": [1, 2]}, {"a": [1, 2]}),
    ("list-of-objects-subset-refused", {"a": [{"x": 1, "y": 2}]}, {"a": [{"x": 1}]}, {"a": [{"x": 1, "y": 2}]}),
    ("empty-list-contained", {"a": [1]}, {"a": []}, {"a": [1]}),
    ("empty-list-replaced-by-items", {"a": []}, {"a": [1]}, {"a": [1]}),
    ("equal-integer-and-float-contained", {"a": 1}, {"a": 1.0}, {"a": 1}),
    ("true-is-not-one", {"a": True}, {"a": 1}, {"a": 1}),
    ("one-is-not-true", {"a": 1}, {"a": True}, {"a": True}),
    ("string-one-is-not-one", {"a": "1"}, {"a": 1}, {"a": 1}),
    ("json-null-value-contained", {"a": None}, {"a": None}, {"a": None}),
    ("json-null-value-replaced", {"a": None}, {"a": 1}, {"a": 1}),
    ("value-replaced-by-json-null", {"a": 1}, {"a": None}, {"a": None}),
    ("decimal-trailing-zero-contained", {"a": 0.1}, {"a": 0.10}, {"a": 0.1}),
    (
        "big-integers-differ",
        {"n": 12345678901234567890123},
        {"n": 12345678901234567890124},
        {"n": 12345678901234567890124},
    ),
    ("unicode-keys-and-values", {"t": "日本語"}, {"t": "日本語", "é": "ß"}, {"t": "日本語", "é": "ß"}),
    ("top-level-array-subset", [1, 2], [2], [1, 2]),
    ("top-level-arrays-join", [1], [2], [1, 2]),
    ("top-level-object-and-array", {"a": 1}, [1], [{"a": 1}, 1]),
    ("top-level-scalars-join", 1, 2, [1, 2]),
    ("top-level-equal-scalars", 1, 1, 1),
    ("top-level-scalar-and-object", "x", {"a": 1}, ["x", {"a": 1}]),
    ("stored-json-null-and-object", JNULL, {"a": 1}, [None, {"a": 1}]),
    ("object-and-json-null", {"a": 1}, JNULL, [{"a": 1}, None]),
]

_C2 = {"chapters": [{"n": 1}, {"n": 2}], "runtimeLengthMs": 5}
_C1 = {"chapters": [{"n": 9}], "runtimeLengthMs": 7}
_E = {"chapters": [], "runtimeLengthMs": 0}
_E2 = {"chapters": [], "runtimeLengthMs": 3}
_NOKEY = {"runtimeLengthMs": 1}

CHAPTERED = [
    ("empty-incoming-keeps-stored", _C2, _E, _C2),
    ("empty-stored-takes-chapters", _E, _C2, _C2),
    ("fewer-chapters-replace", _C2, _C1, _C1),
    ("more-chapters-replace", _C1, _C2, _C2),
    ("neither-lists-takes-incoming", _E, _E2, _E2),
    ("no-chapters-key-incoming", _C2, _NOKEY, _C2),
    ("no-chapters-key-stored", _NOKEY, _C1, _C1),
    ("neither-has-key-takes-incoming", _NOKEY, _E2, _E2),
    ("string-chapters-incoming", _C2, {"chapters": "x"}, _C2),
    ("string-chapters-stored", {"chapters": "x"}, _C2, _C2),
    ("object-chapters-incoming", _C2, {"chapters": {"a": 1}}, _C2),
    ("object-chapters-both", {"chapters": {"a": 1}}, {"chapters": {"b": 2}}, {"chapters": {"b": 2}}),
    ("null-chapters-incoming", _C2, {"chapters": None}, _C2),
    ("json-null-payload-incoming", _C2, JNULL, _C2),
    ("json-null-payload-stored", JNULL, _C1, _C1),
    ("json-null-both", JNULL, JNULL, JNULL),
    ("array-payload-incoming", _C2, [1], _C2),
    ("scalar-payload-incoming", _C2, 3, _C2),
    ("string-payload-incoming", _C2, "x", _C2),
    ("null-element-counts-as-a-chapter", _E, {"chapters": [None]}, {"chapters": [None]}),
]

# (pattern, expected names) over NAMES
NAMES = ["Müller", "MÜLLER", "ÉLAN", "Ñandú", "São Paulo", "Œuvre", "ΣΊΣΥΦΟΣ", "straße", "ＡＢＣ", "İstanbul", "plain"]
ILIKE = [
    ("%müller%", ["Müller", "MÜLLER"]),
    ("%élan%", ["ÉLAN"]),
    ("%ñandú%", ["Ñandú"]),
    ("%são%", ["São Paulo"]),
    ("%œuvre%", ["Œuvre"]),
    ("%σίσυφοσ%", ["ΣΊΣΥΦΟΣ"]),
    ("%strasse%", []),
    ("%STRASSE%", []),
    ("%abc%", []),
    ("%PLAIN%", ["plain"]),
    ("%i̇stanbul%", []),
    ("%istanbul%", ["İstanbul"]),
]
