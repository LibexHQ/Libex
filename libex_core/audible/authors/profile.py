"""
Fetching and normalizing an Audible author's profile, and finding authors by
name through Audible's search suggestions.

fetch_author_profile returns the contributor record raw, normalize_author
turns one into the response shape Libex serves, derived from AudiMeta's
AuthorDto, and fetch_author_suggestion_asins returns the author ASINs a
partial name resolves to.

An author ASIN is global: it resolves in all eleven regions, though what the
record carries (the localized name, the bio) is that marketplace's. Region is
still threaded through every call here for that reason.

Nothing here validates what a caller typed as a name. It goes to Audible as
given, and no message raised from this module repeats any of it.
"""

# Standard library
from datetime import datetime, timezone
from typing import Any

# Core
from libex_core.audible.client import AudibleGet, LOCALE_MAP, validate_region, validated_asin
from libex_core.audible.search import SEARCH_SUGGESTIONS_PATH, _generate_session_id
from libex_core.text import strip_html

AUTHOR_PROFILE_PATH = "/1.0/catalog/contributors/{asin}"


def normalize_author(data: dict, asin: str, region: str) -> dict[str, Any]:
    """
    Normalizes a raw Audible contributor response into the author response shape.

    A response with no contributor, or a contributor whose name is missing or
    null, is tolerated rather than raised on: the name comes out as an empty
    string. Deciding that such a response means "author not found" is the
    caller's; the hosted service does so before it normalizes.
    """
    contributor = data.get("contributor") or {}
    bio = contributor.get("bio")
    return {
        "id": None,
        "asin": asin,
        "name": (contributor.get("name") or "").replace("\t", "").strip(),
        "description": strip_html(bio),
        "image": contributor.get("profile_image_url"),
        "region": region,
        "regions": [region],
        "genres": [],
        "updatedAt": datetime.now(timezone.utc).isoformat(),
    }


async def fetch_author_profile(get: AudibleGet, asin: str, region: str) -> dict[str, Any]:
    """
    Fetches an author's contributor record from Audible, through `get`, and
    returns it as Audible sent it: the bio, the image and the name.

    A 404 is terminal and surfaces as NotFoundException from `get`; a
    transient failure surfaces as AudibleAPIException and is the caller's to
    retry. A 200 that carries no contributor name is returned as it is rather
    than raised on -- what that means is the caller's to decide.

    Raises RegionException for a region that is not one of the eleven, and
    ValueError for a value that is not an ASIN -- before anything is sent.
    """
    region = validate_region(region)
    path = AUTHOR_PROFILE_PATH.format(asin=validated_asin(asin))
    params = {
        "locale": LOCALE_MAP.get(region, "en-US"),
    }
    return await get(region, path, params)


async def fetch_author_suggestion_asins(
    get: AudibleGet, name: str, region: str
) -> list[str]:
    """
    Asks Audible's search suggestions which authors `name` resolves to in one
    region, through `get`, and returns the ASINs of the author rows, in the
    order Audible gave them.

    The ASINs are returned as Audible sent them, unvalidated. A
    NotFoundException or AudibleAPIException from `get` propagates as it is.

    Raises RegionException for a region that is not one of the eleven, before
    anything is sent.
    """
    region = validate_region(region)
    params = {
        "keywords": name,
        "key_strokes": name,
        "site_variant": "android-mshop",
        "session_id": _generate_session_id(),
        "local_time": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
        "surface": "Android",
    }
    data = await get(region, SEARCH_SUGGESTIONS_PATH, params)

    asins: list[str] = []
    for item in data.get("model", {}).get("items", []):
        if item.get("view", {}).get("template") == "AuthorItemV2":
            asin = item.get("model", {}).get("person_metadata", {}).get("asin")
            if asin:
                asins.append(asin)
    return asins
