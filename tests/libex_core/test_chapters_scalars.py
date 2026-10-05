"""
normalize_chapters: a typed scalar or title that is not what the model types
reads as the field's default, the raw value is kept in audibleExtras under
Audible's own key, and one bad field never costs the other chapters.
"""

# Standard library
import json
import math

# Third party
import pytest

# Local
from libex_core.audible.chapters import normalize_chapters
from libex_core.models import ChapterResponse

HUGE = 10**5000
BAD = ["twelve", "", "1.5", 1.5, float("nan"), float("inf"), HUGE, "9" * 5000, None, {"a": 1}, [1]]

CHAPTER_KEYS = [("length_ms", "lengthMs"), ("start_offset_ms", "startOffsetMs"), ("start_offset_sec", "startOffsetSec")]
INFO_KEYS = [
    ("brandIntroDurationMs", "brandIntroDurationMs"),
    ("brandOutroDurationMs", "brandOutroDurationMs"),
    ("runtime_length_ms", "runtimeLengthMs"),
    ("runtime_length_sec", "runtimeLengthSec"),
]


def _id(value):
    """A test id that never renders the huge int, which cannot be printed."""
    return type(value).__name__ + ("_big" if isinstance(value, int) and value.bit_length() > 100 else "")


def _good(title="ok"):
    return {"length_ms": 10, "start_offset_ms": 20, "start_offset_sec": 1, "title": title}


def _payload(chapters, **info):
    return {"content_metadata": {"chapter_info": {"chapters": chapters, **info}}}


def _roundtrip(out):
    """What the write and the response both do: render it and validate it."""
    json.dumps(out)
    ChapterResponse(**out)


@pytest.mark.parametrize("raw_key,field", CHAPTER_KEYS)
@pytest.mark.parametrize("bad", BAD, ids=_id)
def test_bad_chapter_scalar_defaults_and_is_kept(raw_key, field, bad):
    chapter = _good()
    chapter[raw_key] = bad
    out = normalize_chapters(_payload([_good("a"), chapter, _good("c")]))
    _roundtrip(out)
    assert [c["title"] for c in out["chapters"]] == ["a", "ok", "c"]
    assert out["chapters"][1][field] == 0
    assert out["chapters"][0][field] != 0 and out["chapters"][2][field] != 0
    kept = out["chapters"][1]["audibleExtras"]
    if bad is HUGE:
        assert kept == {raw_key: None}
        assert out["extrasWithheld"]["sanitized"]["oversizedNumbers"] == 1
    elif isinstance(bad, float) and not math.isfinite(bad):
        assert kept == {raw_key: None}
    else:
        assert kept == {raw_key: bad}


@pytest.mark.parametrize("raw_key,field", INFO_KEYS)
@pytest.mark.parametrize("bad", BAD, ids=_id)
def test_bad_chapter_info_scalar_defaults_and_is_kept(raw_key, field, bad):
    out = normalize_chapters(_payload([_good()], **{raw_key: bad}))
    _roundtrip(out)
    assert out[field] == 0
    assert len(out["chapters"]) == 1
    assert raw_key in out["audibleExtras"]["chapterInfo"]


def test_good_scalars_pass_through_unchanged():
    out = normalize_chapters(_payload([_good()], runtime_length_ms=5, brandIntroDurationMs=2**63 - 1, is_accurate=True))
    assert out["chapters"][0] == {"lengthMs": 10, "startOffsetMs": 20, "startOffsetSec": 1, "title": "ok"}
    assert out["runtimeLengthMs"] == 5
    assert out["brandIntroDurationMs"] == 2**63 - 1
    assert out["isAccurate"] is True
    assert "audibleExtras" not in out


# Everything the response model coerced before is published unchanged: only a
# value it refused (or json.dumps could not render) reads as the default.
INT_MATRIX = [
    "1500", " 1500 ", "1500.0", "-7", True, False, -5, 0, 1500.0, -3.0, 1e30,
    2**63, 2**70, 10**4000, "1_000", "٣", "1e3", "0x10", "twelve", "", "1.5",
    1.5, float("nan"), float("-inf"), None, {}, [], [1], 10**5000,
]
BOOL_MATRIX = [
    "true", "false", "True", "1", "0", "yes", "no", "on", "off", "t", "f", "y", "n",
    1, 0, True, False, 1.0, 0.0, 2, "2", "maybe", "", None, [], {}, 1.5,
]


def _model_int(value):
    """What the response model published for this value before, or None if it refused."""
    try:
        json.dumps(value)
        return ChapterResponse(chapters=[{"lengthMs": value}]).chapters[0].lengthMs
    except Exception:
        return None


@pytest.mark.parametrize("value", INT_MATRIX, ids=_id)
def test_int_differential_against_the_model(value):
    expected = _model_int(value)
    chapter = {**_good(), "length_ms": value}
    out = normalize_chapters(_payload([chapter], runtime_length_ms=value))
    _roundtrip(out)
    if expected is None:
        assert out["chapters"][0]["lengthMs"] == 0
        assert out["runtimeLengthMs"] == 0
        assert "length_ms" in out["chapters"][0]["audibleExtras"]
    else:
        assert out["chapters"][0]["lengthMs"] == expected
        assert out["runtimeLengthMs"] == expected
        assert "audibleExtras" not in out["chapters"][0]


def test_int_matrix_covers_both_outcomes():
    outcomes = {_model_int(v) is None for v in INT_MATRIX}
    assert outcomes == {True, False}


@pytest.mark.parametrize("value", BOOL_MATRIX, ids=_id)
def test_is_accurate_differential_against_the_model(value):
    try:
        expected = ChapterResponse(isAccurate=value).isAccurate
    except Exception:
        expected = None
    out = normalize_chapters(_payload([_good()], is_accurate=value))
    _roundtrip(out)
    if expected is None:
        assert out["isAccurate"] is False
        assert out["audibleExtras"]["chapterInfo"]["is_accurate"] == value
    else:
        assert out["isAccurate"] is expected
        assert "audibleExtras" not in out


def test_title_matrix_matches_the_model():
    for value in ("t", "", 5, 1.5, True, None, {"t": 1}, ["x"]):
        try:
            expected = ChapterResponse(chapters=[{"title": value}]).chapters[0].title
        except Exception:
            expected = None
        out = normalize_chapters(_payload([_good(value)]))
        _roundtrip(out)
        if expected is None:
            assert out["chapters"][0]["title"] == ""
            assert out["chapters"][0]["audibleExtras"] == {"title": value}
        else:
            assert out["chapters"][0]["title"] == expected


def test_wide_but_renderable_int_is_published():
    chapter = {**_good(), "length_ms": 2**63}
    out = normalize_chapters(_payload([chapter]))
    assert out["chapters"][0]["lengthMs"] == 2**63


@pytest.mark.parametrize("bad", [5, 1.5, True, None, {"t": 1}, ["x"]], ids=_id)
def test_non_string_title_defaults_and_is_kept(bad):
    out = normalize_chapters(_payload([_good("a"), _good(bad), _good("c")]))
    _roundtrip(out)
    assert [c["title"] for c in out["chapters"]] == ["a", "", "c"]
    assert out["chapters"][1]["audibleExtras"] == {"title": bad}


def test_nul_in_string_title_still_stripped():
    out = normalize_chapters(_payload([_good("a\x00b")]))
    assert out["chapters"][0]["title"] == "ab"
    assert out["extrasWithheld"]["sanitized"]["nulCharacters"] == 1


def test_bad_scalar_on_sub_chapter_keeps_siblings():
    parent = _good("p")
    parent["chapters"] = [_good("s1"), {**_good("s2"), "length_ms": "x"}]
    out = normalize_chapters(_payload([parent, _good("q")]))
    _roundtrip(out)
    subs = out["chapters"][0]["chapters"]
    assert [s["title"] for s in subs] == ["s1", "s2"]
    assert subs[1]["lengthMs"] == 0 and subs[0]["lengthMs"] == 10
    assert out["chapters"][1]["title"] == "q"
