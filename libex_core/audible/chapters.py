"""
Fetching and normalizing a book's chapter listing from Audible's content
metadata endpoint.

The listing is a different record from the book itself, so it has its own
shape: fetch_chapter_metadata returns Audible's response raw, and
normalize_chapters turns one that carries chapter_info into the response
shape Libex serves, derived from AudiMeta's TrackContentDto. Whether a
response carries chapter_info at all is a separate question, answered by
has_chapter_info, because the caller decides what an absent listing means.
"""

# Standard library
from typing import Any

# Core
from libex_core.audible.client import AudibleGet, validate_region, validated_asin

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


def _normalize_chapter(c: dict) -> dict[str, Any]:
    """
    One chapter in the response shape. Sub-chapters, which Audible nests under
    a chapter's own chapters key, are normalized the same way and carried as
    chapters on the chapter; keys this does not reproduce ride in the
    chapter's audibleExtras. Both appear only when Audible sent them, so a
    chapter with neither is byte-identical to one from before they were kept.
    """
    chapter: dict[str, Any] = {
        "lengthMs": c.get("length_ms", 0),
        "startOffsetMs": c.get("start_offset_ms", 0),
        "startOffsetSec": c.get("start_offset_sec", 0),
        "title": c.get("title", ""),
    }
    children = c.get("chapters")
    if children:
        chapter["chapters"] = [_normalize_chapter(child) for child in children]
    extras = {k: v for k, v in c.items() if k not in _CHAPTER_CONSUMED}
    if extras:
        chapter["audibleExtras"] = extras
    return chapter


def normalize_chapters(data: dict) -> dict[str, Any]:
    """
    Normalizes raw Audible chapter data into the chapter response shape.

    The content_reference and content_url groups the fetch requests arrive
    beside chapter_info and are carried as contentReference and contentUrl,
    verbatim. Every other key Audible sends that no field above reproduces
    is gathered under audibleExtras by level -- response, contentMetadata,
    chapterInfo -- and each of those keys, like contentReference and
    contentUrl, appears only when Audible sent something for it.
    """
    content_metadata = data.get("content_metadata", {})
    chapter_info = content_metadata.get("chapter_info", {})
    raw_chapters = chapter_info.get("chapters", [])

    chapters = [_normalize_chapter(c) for c in raw_chapters]

    result: dict[str, Any] = {
        "brandIntroDurationMs": chapter_info.get("brandIntroDurationMs", 0),
        "brandOutroDurationMs": chapter_info.get("brandOutroDurationMs", 0),
        "isAccurate": chapter_info.get("is_accurate", False),
        "runtimeLengthMs": chapter_info.get("runtime_length_ms", 0),
        "runtimeLengthSec": chapter_info.get("runtime_length_sec", 0),
        "chapters": chapters,
    }

    if "content_reference" in content_metadata:
        result["contentReference"] = content_metadata["content_reference"]
    if "content_url" in content_metadata:
        result["contentUrl"] = content_metadata["content_url"]

    extras = {
        "response": {
            k: v for k, v in data.items()
            if k != "content_metadata" and k not in _RESPONSE_NOISE
        },
        "contentMetadata": {
            k: v for k, v in content_metadata.items() if k not in _CONTENT_METADATA_CONSUMED
        },
        "chapterInfo": {
            k: v for k, v in chapter_info.items() if k not in _CHAPTER_INFO_CONSUMED
        },
    }
    extras = {level: values for level, values in extras.items() if values}
    if extras:
        result["audibleExtras"] = extras

    return result
