"""
normalize_chapters: nothing Audible sends may make the response unstorable or
unservable, and nothing held back may go unrecorded.

Every verbatim part of a chapter response (contentReference, contentUrl, the
response-level audibleExtras, each chapter's own audibleExtras) goes through
the same bounds a book's extras do, through bound_extras. What cost something
is named in extrasWithheld, which is absent from the normalized output when
nothing did. Inputs that are the wrong shape are carried in audibleExtras
rather than raised on, because a raise here is a failed request and a failed
store for a listing Audible answered.
"""

# Standard library
import json
from unittest.mock import patch

# Third party
import pytest

# Local
from libex_core.audible.chapters import normalize_chapters
from libex_core.audible.extras import MAX_NESTING_DEPTH, bound_extras, build_extras
from libex_core.models import ChapterResponse

ASIN = "B0CHAPTR01"
OVER_CAP = "x" * (65 * 1024)


def _ch(title="C", ms=100, **extra):
    return {"length_ms": ms, "start_offset_ms": 0, "start_offset_sec": 0, "title": title, **extra}


def _payload(chapters=None, **content_metadata):
    return {
        "content_metadata": {
            "chapter_info": {
                "is_accurate": True,
                "runtime_length_ms": 1000,
                "runtime_length_sec": 1,
                "chapters": chapters if chapters is not None else [_ch()],
            },
            **content_metadata,
        }
    }


def _deep(levels):
    value = "leaf"
    for _ in range(levels):
        value = {"d": value}
    return value


def _chain(levels):
    """Sub-chapters nested `levels` deep, titled L1..L<levels>, L1 outermost."""
    node = _ch(f"L{levels}")
    for n in range(levels - 1, 0, -1):
        node = _ch(f"L{n}", chapters=[node])
    return node


def _valid(out):
    ChapterResponse(**out)
    json.dumps(out, allow_nan=False)


# ============================================================
# NUL, non-finite and oversized values
# ============================================================

def test_a_nul_in_a_chapter_title_is_stripped_counted_and_storable():
    out = normalize_chapters(_payload([_ch("Chap\x00ter\x00 1")]), ASIN, "us")
    assert out["chapters"][0]["title"] == "Chapter 1"
    assert out["extrasWithheld"] == {"sanitized": {"nulCharacters": 2}}
    assert "\\u0000" not in json.dumps(out) and "\x00" not in json.dumps(out)
    _valid(out)


def test_non_finite_and_oversized_numbers_in_a_chapter_extra_become_null_and_are_counted():
    out = normalize_chapters(
        _payload([_ch(a=float("nan"), b=float("inf"), c=-float("inf"), d=1 << 20000, e=7)]), ASIN, "us"
    )
    assert out["chapters"][0]["audibleExtras"] == {"a": None, "b": None, "c": None, "d": None, "e": 7}
    assert out["extrasWithheld"] == {"sanitized": {"nonFiniteNumbers": 3, "oversizedNumbers": 1}}
    _valid(out)


def test_nul_in_keys_and_values_of_every_verbatim_group_is_stripped_and_summed():
    payload = _payload(
        content_reference={"a\x00b": "c\x00d"},
        content_url={"u": "x\x00"},
        top_level={"k\x00": "v"},
    )
    payload["resp\x00key"] = "r\x00"
    out = normalize_chapters(payload, ASIN, "us")
    assert out["contentReference"] == {"ab": "cd"}
    assert out["contentUrl"] == {"u": "x"}
    assert out["audibleExtras"]["contentMetadata"] == {"top_level": {"k": "v"}}
    assert out["audibleExtras"]["response"] == {"respkey": "r"}
    # contentReference 2, contentUrl 1, contentMetadata 1, response 2
    assert out["extrasWithheld"] == {"sanitized": {"nulCharacters": 6}}
    assert "\x00" not in json.dumps(out)
    _valid(out)


# ============================================================
# Withheld whole, with the reason
# ============================================================

@pytest.mark.parametrize("field, source", [("contentReference", "content_reference"), ("contentUrl", "content_url")])
def test_a_group_over_64kb_is_absent_and_the_reason_is_size(field, source):
    out = normalize_chapters(_payload(**{source: {"blob": OVER_CAP}}), ASIN, "us")
    assert field not in out
    assert out["extrasWithheld"] == {field: "size"}
    assert OVER_CAP not in json.dumps(out)
    _valid(out)


@pytest.mark.parametrize("field, source", [("contentReference", "content_reference"), ("contentUrl", "content_url")])
def test_a_group_nested_past_32_is_absent_and_the_reason_is_depth(field, source):
    out = normalize_chapters(_payload(**{source: _deep(40)}), ASIN, "us")
    assert field not in out
    assert out["extrasWithheld"] == {field: "depth"}
    _valid(out)


def test_a_response_level_blob_over_the_cap_is_withheld_and_recorded():
    payload = _payload()
    payload["big"] = OVER_CAP
    out = normalize_chapters(payload, ASIN, "us")
    assert "audibleExtras" not in out
    assert out["extrasWithheld"] == {"audibleExtras": "size"}
    _valid(out)


def test_the_withheld_group_does_not_take_the_others_with_it():
    out = normalize_chapters(_payload(content_reference={"blob": OVER_CAP}, content_url={"ok": 1}), ASIN, "us")
    assert out["contentUrl"] == {"ok": 1} and "contentReference" not in out
    assert out["extrasWithheld"] == {"contentReference": "size"}


def test_a_chapter_blob_over_the_cap_is_omitted_and_tallied_by_reason():
    chapters = [_ch("a", blob=OVER_CAP), _ch("b", blob=OVER_CAP), _ch("c", deep=_deep(40)), _ch("d", ok=1)]
    out = normalize_chapters(_payload(chapters), ASIN, "us")
    got = out["chapters"]
    assert all("audibleExtras" not in c for c in got[:3])
    assert got[3]["audibleExtras"] == {"ok": 1}
    assert [c["title"] for c in got] == ["a", "b", "c", "d"]
    assert out["extrasWithheld"] == {"chapterExtras": {"size": 2, "depth": 1}}
    _valid(out)


# ============================================================
# Sub-chapter recursion is bounded
# ============================================================

def test_sub_chapters_nested_past_the_cap_move_into_that_chapters_extras_and_are_counted():
    out = normalize_chapters(_payload([_chain(MAX_NESTING_DEPTH + 3)]), ASIN, "us")
    node, depth = out["chapters"][0], 1
    while "chapters" in node:
        node, depth = node["chapters"][0], depth + 1
    assert depth == MAX_NESTING_DEPTH
    assert node["title"] == f"L{MAX_NESTING_DEPTH}"
    remainder = node["audibleExtras"]["chapters"]
    assert remainder[0]["title"] == f"L{MAX_NESTING_DEPTH + 1}"
    assert remainder[0]["chapters"][0]["chapters"][0]["title"] == f"L{MAX_NESTING_DEPTH + 3}"
    # verbatim: the Audible keys, not the normalized ones
    assert "length_ms" in remainder[0] and "lengthMs" not in remainder[0]
    assert out["extrasWithheld"] == {"subChapters": {"depth": 1}}
    _valid(out)


def test_sub_chapters_exactly_at_the_cap_are_all_kept():
    out = normalize_chapters(_payload([_chain(MAX_NESTING_DEPTH)]), ASIN, "us")
    node, depth = out["chapters"][0], 1
    while "chapters" in node:
        node, depth = node["chapters"][0], depth + 1
    assert depth == MAX_NESTING_DEPTH
    assert "extrasWithheld" not in out and "audibleExtras" not in node


def test_a_very_deep_chain_is_bounded_not_recursed_into():
    out = normalize_chapters(_payload([_chain(5000)]), ASIN, "us")
    assert out["extrasWithheld"]["subChapters"] == {"depth": 1}
    # The remainder is itself too deep to carry, and that is recorded too.
    assert out["extrasWithheld"]["chapterExtras"] == {"depth": 1}
    _valid(out)


# ============================================================
# Wrong shapes are carried, never raised on
# ============================================================

@pytest.mark.parametrize("bad", ["a string", {"0": "x"}, 7, [1, "a"], [_ch(), 3], True])
def test_a_chapters_value_that_is_not_a_list_of_objects_is_carried_verbatim_with_no_raise(bad):
    out = normalize_chapters(_payload(bad), ASIN, "us")
    assert out["chapters"] == []
    assert out["audibleExtras"]["chapterInfo"]["chapters"] == bad
    _valid(out)


@pytest.mark.parametrize("bad", ["a string", {"0": "x"}, 7, [1, "a"], [_ch(), 3]])
def test_sub_chapters_that_are_not_a_list_of_objects_ride_in_that_chapters_extras(bad):
    out = normalize_chapters(_payload([{**_ch("Top"), "chapters": bad}]), ASIN, "us")
    chapter = out["chapters"][0]
    assert "chapters" not in chapter
    assert chapter["audibleExtras"] == {"chapters": bad}
    _valid(out)


@pytest.mark.parametrize("bad", [["a", "list"], "a string", 12])
@pytest.mark.parametrize("field, source", [("contentReference", "content_reference"), ("contentUrl", "content_url")])
def test_a_non_object_group_goes_to_content_metadata_extras_and_the_response_validates(bad, field, source):
    out = normalize_chapters(_payload(**{source: bad}), ASIN, "us")
    assert field not in out
    assert out["audibleExtras"]["contentMetadata"] == {source: bad}
    _valid(out)


def test_a_null_group_is_carried_as_null():
    out = normalize_chapters(_payload(content_reference=None), ASIN, "us")
    assert "contentReference" in out and out["contentReference"] is None
    _valid(out)


@pytest.mark.parametrize("bad", ["x", [1], 5, None])
def test_a_non_object_chapter_info_does_not_raise_and_is_carried(bad):
    out = normalize_chapters({"content_metadata": {"chapter_info": bad}}, ASIN, "us")
    assert out["chapters"] == []
    assert out["audibleExtras"]["contentMetadata"]["chapter_info"] == bad
    _valid(out)


@pytest.mark.parametrize("bad", ["x", [1], 5, None])
def test_a_non_object_content_metadata_does_not_raise_and_is_carried(bad):
    out = normalize_chapters({"content_metadata": bad}, ASIN, "us")
    assert out["chapters"] == []
    assert out["audibleExtras"]["response"]["content_metadata"] == bad
    _valid(out)


# ============================================================
# extrasWithheld is absent unless something was withheld
# ============================================================

def test_extras_withheld_is_absent_when_nothing_was_withheld_even_with_extras_present():
    payload = _payload([_ch(tag="t", chapters=[_ch("sub")])], content_reference={"a": 1}, content_url={"b": 2}, more=1)
    payload["request_id"] = "r"
    out = normalize_chapters(payload, ASIN, "us")
    assert "extrasWithheld" not in out
    assert out["audibleExtras"] and out["contentReference"] == {"a": 1}
    assert "extrasWithheld" not in normalize_chapters(_payload(), ASIN, "us")


def test_asin_and_region_default_so_existing_callers_still_work():
    assert normalize_chapters(_payload())["chapters"][0]["title"] == "C"


# ============================================================
# bound_extras and its parity with build_extras
# ============================================================

BLOBS = {
    "clean": {"a": 1, "b": [1, 2, {"c": "d"}]},
    "nul": {"a\x00": "b\x00", "n": ["\x00"]},
    "nonfinite": {"a": float("nan"), "b": [float("inf")]},
    "oversized_int": {"a": 1 << 20000},
    "deep": {"a": _deep(40)},
    "big": {"a": OVER_CAP},
    "empty": {},
}


def test_bound_extras_returns_blob_counts_and_reason_in_that_order():
    blob, hits, reason = bound_extras({"a\x00": 1, "b": float("nan")}, ASIN, "us")
    assert blob == {"a": 1, "b": None}
    assert hits == {"nulCharacters": 1, "nonFiniteNumbers": 1}
    assert reason is None
    assert bound_extras({"a": OVER_CAP}, ASIN, "us") == (None, {}, "size")
    assert bound_extras({"a": _deep(40)}, ASIN, "us") == (None, {}, "depth")


def test_bound_extras_reports_only_nonzero_counts():
    assert bound_extras({"a": 1}, ASIN, "us") == ({"a": 1}, {}, None)


@pytest.mark.parametrize("name", list(BLOBS))
def test_build_extras_agrees_with_bound_extras_for_the_same_input(name):
    blob = BLOBS[name]
    bounded, hits, reason = bound_extras(blob, ASIN, "us")
    built, withheld = build_extras(blob, ASIN, "us")
    assert built == bounded
    expected = {}
    if hits:
        expected["sanitized"] = hits
    if reason is not None:
        expected["audibleExtras"] = reason
    assert withheld == expected


def test_bound_extras_does_not_mutate_its_input():
    blob = {"a\x00": ["x\x00", {"y": float("nan")}]}
    snapshot = json.dumps(blob, allow_nan=True)
    bound_extras(blob, ASIN, "us")
    assert json.dumps(blob, allow_nan=True) == snapshot


# ============================================================
# The windowed incident log fires for chapter withholdings
# ============================================================

EXTRAS_LOGGER = "libex_core.audible.extras.logger"


@pytest.fixture
def fresh_incident_state():
    with patch("libex_core.audible.extras._extras_incident_counts", {}), \
         patch("libex_core.audible.extras._extras_incident_last_logged", {}):
        yield


def _incidents(mock_logger):
    return [c.kwargs["extra"] for c in mock_logger.warning.call_args_list if c.args[0] == "Audible extras withheld"]


def test_chapter_sanitizing_and_withholding_each_log_one_windowed_incident(fresh_incident_state):
    with patch(EXTRAS_LOGGER) as mock_logger, patch("libex_core.audible.extras.time.monotonic", return_value=5.0):
        normalize_chapters(_payload([_ch("a\x00", blob=OVER_CAP)]), ASIN, "uk")
        normalize_chapters(_payload([_ch("b\x00", blob=OVER_CAP)]), ASIN, "uk")
    reasons = sorted((i["withheld_reason"], i["asin"], i["region"]) for i in _incidents(mock_logger))
    # One per reason in the window, not one per chapter or per call.
    assert reasons == [("sanitized", ASIN, "uk"), ("size", ASIN, "uk")]


def test_a_chapter_response_with_nothing_withheld_logs_no_incident(fresh_incident_state):
    with patch(EXTRAS_LOGGER) as mock_logger:
        normalize_chapters(_payload([_ch(tag="t")], content_reference={"a": 1}), ASIN, "us")
    assert _incidents(mock_logger) == []
