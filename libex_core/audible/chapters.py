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
    """
    title = c.get("title", "")
    if isinstance(title, str) and "\x00" in title:
        bounded_title, _ = withheld.bound({"title": title})
        title = bounded_title["title"] if bounded_title else title.replace("\x00", "")
    chapter: dict[str, Any] = {
        "lengthMs": c.get("length_ms", 0),
        "startOffsetMs": c.get("start_offset_ms", 0),
        "startOffsetSec": c.get("start_offset_sec", 0),
        "title": title,
    }
    extras = {k: v for k, v in c.items() if k not in _CHAPTER_CONSUMED}
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

    result: dict[str, Any] = {
        "brandIntroDurationMs": chapter_info.get("brandIntroDurationMs", 0),
        "brandOutroDurationMs": chapter_info.get("brandOutroDurationMs", 0),
        "isAccurate": chapter_info.get("is_accurate", False),
        "runtimeLengthMs": chapter_info.get("runtime_length_ms", 0),
        "runtimeLengthSec": chapter_info.get("runtime_length_sec", 0),
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
