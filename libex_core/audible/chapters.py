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


def normalize_chapters(data: dict) -> dict[str, Any]:
    """Normalizes raw Audible chapter data into the chapter response shape."""
    chapter_info = data.get("content_metadata", {}).get("chapter_info", {})
    raw_chapters = chapter_info.get("chapters", [])

    chapters = [
        {
            "lengthMs": c.get("length_ms", 0),
            "startOffsetMs": c.get("start_offset_ms", 0),
            "startOffsetSec": c.get("start_offset_sec", 0),
            "title": c.get("title", ""),
        }
        for c in raw_chapters
    ]

    return {
        "brandIntroDurationMs": chapter_info.get("brandIntroDurationMs", 0),
        "brandOutroDurationMs": chapter_info.get("brandOutroDurationMs", 0),
        "isAccurate": chapter_info.get("is_accurate", False),
        "runtimeLengthMs": chapter_info.get("runtime_length_ms", 0),
        "runtimeLengthSec": chapter_info.get("runtime_length_sec", 0),
        "chapters": chapters,
    }
