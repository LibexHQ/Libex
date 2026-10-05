"""
normalize_chapters: a typed scalar or title that is not what the model types
reads as the field's default, the raw value is kept in audibleExtras under
Audible's own key, and one bad field never costs the other chapters.
"""

# Standard library
import json

# Third party
import pytest

# Local
from libex_core.audible.chapters import normalize_chapters
from libex_core.models import ChapterResponse

HUGE = 10**5000
BAD = ["12", 1.5, float("nan"), True, HUGE, -1, None, {"a": 1}, [1]]

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
    elif isinstance(bad, float) and bad != bad:
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


def test_whole_valued_float_is_the_int():
    chapter = _good()
    chapter["length_ms"] = 1500.0
    out = normalize_chapters(_payload([chapter]))
    assert out["chapters"][0]["lengthMs"] == 1500
    assert "audibleExtras" not in out["chapters"][0]


def test_just_past_the_int64_bound_is_bad():
    chapter = _good()
    chapter["length_ms"] = 2**63
    out = normalize_chapters(_payload([chapter]))
    assert out["chapters"][0]["lengthMs"] == 0
    assert out["chapters"][0]["audibleExtras"] == {"length_ms": 2**63}


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


@pytest.mark.parametrize("bad", ["true", 1, None, {}], ids=_id)
def test_non_bool_is_accurate_defaults_and_is_kept(bad):
    out = normalize_chapters(_payload([_good()], is_accurate=bad))
    _roundtrip(out)
    assert out["isAccurate"] is False
    assert out["audibleExtras"]["chapterInfo"]["is_accurate"] == bad
