"""
Fetching and normalizing a book's chapter listing from Audible's content
metadata endpoint.

The listing is a different record from the book itself, so it has its own
shape: fetch_chapter_metadata returns Audible's response raw, and
normalize_chapters turns one that carries chapter_info into the response
shape Libex serves, derived from AudiMeta's TrackContentDto. Whether a
response carries chapter_info at all is a separate question, answered by
has_chapter_info, because the caller decides what an absent listing means.

Everything Audible sends that is carried verbatim -- contentReference,
contentUrl, the audibleExtras groups and each chapter's own audibleExtras --
goes through the same sanitizing and size and depth caps a book's extras do
(libex_core.audible.extras.bound_extras), and whatever that withholds is
recorded in the response's extrasWithheld rather than dropped silently.
"""

# Standard library
import math
from typing import Any

# Core
from libex_core.audible.client import AudibleGet, validate_region, validated_asin
from libex_core.audible.extras import MAX_NESTING_DEPTH, bound_extras

CHAPTERS_PATH = "/1.0/content/{asin}/metadata"

CHAPTERS_RESPONSE_GROUPS = "chapter_info, always-returned, content_reference, content_url"

CHAPTERS_QUALITY = "High"


async def fetch_chapter_metadata(get: AudibleGet, asin: str, region: str) -> Any:
    """
    Fetches a book's content metadata from Audible, through `get`, and returns
    the response as Audible sent it.

    A 404 is terminal and surfaces as NotFoundException from `get`: Audible has
    no chapter metadata for that book in that region, and will not on a retry
    (the ISBN-keyed records are the large population that 404 here). A
    transient failure surfaces as AudibleAPIException, and is the caller's to
    retry. A 200 that carries no chapter_info is returned as-is rather than
    raised on -- see has_chapter_info.

    Raises RegionException for a region that is not one of the eleven, and
    ValueError for a value that is not an ASIN -- before anything is sent.
    """
    region = validate_region(region)
    path = CHAPTERS_PATH.format(asin=validated_asin(asin))
    params = {
        "response_groups": CHAPTERS_RESPONSE_GROUPS,
        "quality": CHAPTERS_QUALITY,
    }
    return await get(region, path, params)


def has_chapter_info(data: Any) -> bool:
    """True when a content metadata response carries a chapter_info listing."""
    return bool(data.get("content_metadata", {}).get("chapter_info"))


# Keys normalize_chapters reproduces as first-class fields; everything else on
# the same object goes through verbatim in audibleExtras, so a key Audible
# adds later surfaces without a code change.
_CHAPTER_INFO_CONSUMED = frozenset({
    "brandIntroDurationMs", "brandOutroDurationMs", "is_accurate",
    "runtime_length_ms", "runtime_length_sec", "chapters",
})
_CHAPTER_CONSUMED = frozenset({
    "length_ms", "start_offset_ms", "start_offset_sec", "title", "chapters",
})
_CONTENT_METADATA_CONSUMED = frozenset({"chapter_info", "content_reference", "content_url"})

# The response's own top-level keys other than content_metadata. response_groups
# is the one recorded drop: Audible echoes back the groups that were requested,
# which is the request's own constant and carries nothing about the book. Any
# other top-level key rides in audibleExtras.
_RESPONSE_NOISE = frozenset({"response_groups"})


# The widest value a typed scalar is accepted at: a signed 64-bit integer,
# which is what a Postgres bigint holds and far past any duration in
# milliseconds. A wider int is not a duration, and one wider than about 4,300
# digits cannot even be rendered by json.dumps, which is what would fail the
# write and the response. Narrower than bound_extras' own ceiling on purpose:
# these fields are numbers the model types as int, not verbatim passthrough.
_SCALAR_MAX = 2**63 - 1


def _scalar_int(value: Any) -> int | None:
    """
    The int a typed chapter scalar carries, or None when what Audible sent is
    not one: a non-number, a bool (which is an int in Python and not a
    duration), a negative, a number past _SCALAR_MAX, a fractional float, or a
    non-finite one. A whole-valued float is the number it spells.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            return None
        value = int(value)
    if not isinstance(value, int) or value < 0 or value > _SCALAR_MAX:
        return None
    return value


def _read_int(source: dict, key: str, rescued: dict[str, Any]) -> int:
    """
    Reads one typed scalar off a raw Audible object. An absent key is the
    field's default, 0, as it always was. A value that is not usable (see
    _scalar_int) also reads as 0 -- so one odd field cannot fail the response
    or the write and take the other chapters with it -- and the raw value is
    put in `rescued` under Audible's own key, which becomes part of the
    object's audibleExtras. The field no longer speaks for it, but nothing
    Audible sent is lost; a value too wide to store is nulled there and
    counted in extrasWithheld like any other.
    """
    if key not in source:
        return 0
    value = _scalar_int(source[key])
    if value is None:
        rescued[key] = source[key]
        return 0
    return value


def _read_title(source: dict, rescued: dict[str, Any]) -> str:
    """
    A chapter's title: the string Audible sent, "" when it sent none, and ""
    with the raw value kept in `rescued` when it sent something that is not a
    string. A NUL in a string title is the caller's to strip.
    """
    title = source.get("title", "")
    if isinstance(title, str):
        return title
    rescued["title"] = title
    return ""


class _Withheld:
    """
    What bounding the verbatim parts of one chapter response cost, collected
    as the response is built and turned into its extrasWithheld at the end.

    The sanitized counts are summed across every part, the same account a
    book gives. A part withheld whole is filed under the name of the field it
    would have filled; the chapters' own extras, of which there can be one per
    chapter, are tallied by reason instead of named one by one.
    """

    def __init__(self, asin: str, region: str) -> None:
        self.asin = asin
        self.region = region
        self.sanitized: dict[str, int] = {}
        self.whole: dict[str, str] = {}
        self.chapter_extras: dict[str, int] = {}
        self.sub_chapter_depth = 0

    def bound(self, blob: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
        """Runs one verbatim blob through the shared bounds; (blob or None, reason or None)."""
        bounded, hits, reason = bound_extras(blob, self.asin, self.region)
        for name, total in hits.items():
            self.sanitized[name] = self.sanitized.get(name, 0) + total
        return bounded, reason

    def record(self) -> dict[str, Any]:
        """The extrasWithheld value; empty when nothing was withheld."""
        record: dict[str, Any] = {}
        if self.sanitized:
            record["sanitized"] = self.sanitized
        record.update(self.whole)
        if self.chapter_extras:
            record["chapterExtras"] = self.chapter_extras
        if self.sub_chapter_depth:
            record["subChapters"] = {"depth": self.sub_chapter_depth}
        return record


def _is_chapter_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, dict) for item in value)


def _normalize_chapter(c: dict, depth: int, withheld: _Withheld) -> dict[str, Any]:
    """
    One chapter in the response shape. Sub-chapters, which Audible nests under
    a chapter's own chapters key, are normalized the same way and carried as
    chapters on the chapter; keys this does not reproduce ride in the
    chapter's audibleExtras. Both appear only when Audible sent them, so a
    chapter with neither is byte-identical to one from before they were kept.

    depth counts this chapter's level, top-level chapters being 1. Sub-chapters
    nested below MAX_NESTING_DEPTH are not normalized: they ride in this
    chapter's audibleExtras as Audible sent them, as does a chapters value that
    is not a list of objects, and the cut is counted in extrasWithheld. A NUL
    in the title is stripped, and counted.

    The three typed scalars and the title are read defensively: one that is
    not what the model types (see _read_int, _read_title) becomes its default
    and the raw value rides in audibleExtras under Audible's own key.
    """
    rescued: dict[str, Any] = {}
    title = _read_title(c, rescued)
    if "\x00" in title:
        bounded_title, _ = withheld.bound({"title": title})
        title = bounded_title["title"] if bounded_title else title.replace("\x00", "")
    chapter: dict[str, Any] = {
        "lengthMs": _read_int(c, "length_ms", rescued),
        "startOffsetMs": _read_int(c, "start_offset_ms", rescued),
        "startOffsetSec": _read_int(c, "start_offset_sec", rescued),
        "title": title,
    }
    extras = {k: v for k, v in c.items() if k not in _CHAPTER_CONSUMED}
    extras.update(rescued)
    children = c.get("chapters")
    if _is_chapter_list(children):
        if children and depth >= MAX_NESTING_DEPTH:
            extras["chapters"] = children
            withheld.sub_chapter_depth += 1
        elif children:
            chapter["chapters"] = [_normalize_chapter(child, depth + 1, withheld) for child in children]
    elif children is not None:
        extras["chapters"] = children
    if extras:
        bounded, reason = withheld.bound(extras)
        if reason is None:
            chapter["audibleExtras"] = bounded
        else:
            withheld.chapter_extras[reason] = withheld.chapter_extras.get(reason, 0) + 1
    return chapter


def normalize_chapters(data: dict, asin: str = "", region: str = "") -> dict[str, Any]:
    """
    Normalizes raw Audible chapter data into the chapter response shape.

    The content_reference and content_url groups the fetch requests arrive
    beside chapter_info and are carried as contentReference and contentUrl,
    verbatim. Every other key Audible sends that no field above reproduces
    is gathered under audibleExtras by level -- response, contentMetadata,
    chapterInfo -- and each of those keys, like contentReference and
    contentUrl, appears only when Audible sent something for it.

    Nothing is allowed to make the response unstorable. A group that is not
    an object where the response model needs one (a content_reference that
    is a list, a chapters value that is not a list of objects) is carried in
    audibleExtras under its own key instead, so it can neither be dropped nor
    fail validation. Each verbatim part is bounded like a book's extras; a
    part withheld whole is absent and named in extrasWithheld with its
    reason, which is itself absent when nothing was withheld. asin and region
    are only for the log line a withholding writes.

    A typed scalar that is not a usable non-negative int no wider than a
    64-bit one, a non-bool isAccurate, and a non-string title read as the
    field's default; the raw value is carried in audibleExtras, so the field
    is wrong-but-valid and nothing Audible sent is gone.
    """
    withheld = _Withheld(asin, region)

    content_metadata = data.get("content_metadata", {})
    response_leftovers = {
        k: v for k, v in data.items()
        if k != "content_metadata" and k not in _RESPONSE_NOISE
    }
    if not isinstance(content_metadata, dict):
        response_leftovers["content_metadata"] = content_metadata
        content_metadata = {}

    chapter_info = content_metadata.get("chapter_info", {})
    content_metadata_leftovers = {
        k: v for k, v in content_metadata.items() if k not in _CONTENT_METADATA_CONSUMED
    }
    if not isinstance(chapter_info, dict):
        content_metadata_leftovers["chapter_info"] = chapter_info
        chapter_info = {}
    chapter_info_leftovers = {
        k: v for k, v in chapter_info.items() if k not in _CHAPTER_INFO_CONSUMED
    }

    raw_chapters = chapter_info.get("chapters")
    if _is_chapter_list(raw_chapters):
        chapters = [_normalize_chapter(c, 1, withheld) for c in raw_chapters]
    else:
        chapters = []
        if raw_chapters is not None:
            chapter_info_leftovers["chapters"] = raw_chapters

    # Same rule as a chapter's own scalars: a value the model cannot type
    # reads as the default and the raw value is kept in chapterInfo's extras.
    is_accurate = chapter_info.get("is_accurate", False)
    if not isinstance(is_accurate, bool):
        chapter_info_leftovers["is_accurate"] = is_accurate
        is_accurate = False
    result: dict[str, Any] = {
        "brandIntroDurationMs": _read_int(chapter_info, "brandIntroDurationMs", chapter_info_leftovers),
        "brandOutroDurationMs": _read_int(chapter_info, "brandOutroDurationMs", chapter_info_leftovers),
        "isAccurate": is_accurate,
        "runtimeLengthMs": _read_int(chapter_info, "runtime_length_ms", chapter_info_leftovers),
        "runtimeLengthSec": _read_int(chapter_info, "runtime_length_sec", chapter_info_leftovers),
        "chapters": chapters,
    }

    # The response model types both groups as objects. An object is bounded
    # and carried; null is what Audible sent and is carried as null; anything
    # else goes to contentMetadata's extras under its own key.
    for source, field in (("content_reference", "contentReference"), ("content_url", "contentUrl")):
        if source not in content_metadata:
            continue
        value = content_metadata[source]
        if isinstance(value, dict):
            bounded, reason = withheld.bound(value)
            if reason is None:
                result[field] = bounded
            else:
                withheld.whole[field] = reason
        elif value is None:
            result[field] = None
        else:
            content_metadata_leftovers[source] = value

    extras = {
        "response": response_leftovers,
        "contentMetadata": content_metadata_leftovers,
        "chapterInfo": chapter_info_leftovers,
    }
    extras = {level: values for level, values in extras.items() if values}
    if extras:
        bounded, reason = withheld.bound(extras)
        if reason is None:
            result["audibleExtras"] = bounded
        else:
            withheld.whole["audibleExtras"] = reason

    record = withheld.record()
    if record:
        result["extrasWithheld"] = record

    return result
