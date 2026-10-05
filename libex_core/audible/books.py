"""
Fetching and normalizing Audible catalog products.

fetch_products asks Audible for up to 50 products by ASIN through a caller-
supplied `get`. normalize_product turns one raw product into the response
shape Libex serves: a fixed set of first-class camelCase fields, plus
audibleExtras carrying every other top-level key Audible sent, verbatim, so
nothing Audible returns is silently dropped on the way through. The shapes
are derived from AudiMeta's, with additions.

Normalizing and settling are separate steps. normalize_product leaves the
seven tri-state flags and plans as None when Audible said nothing, because a
consumer that stores the result needs "said nothing" to stay distinguishable
from an explicit false; settle_flags fills them for whatever is about to leave
the service layer, and never mutates its input.

Nothing here touches a database, a cache, or the environment; the only
request made is the one fetch_products hands to `get`.
"""

# Standard library
import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any

# Core
from libex_core.asin import is_valid_asin
from libex_core.audible.client import AudibleGet, REGION_MAP, validate_region, validated_asin
from libex_core.audible.extras import _log_extras_incident, build_extras
from libex_core.exceptions import AudibleAPIException
from libex_core.log_safety import is_safe_log_value, safe_asin_for_log, window_elapsed
from libex_core.text import is_unreadable_text, strip_html, strip_image_size_suffix

logger = logging.getLogger("libex")

# ============================================================
# CONSTANTS
# ============================================================

CATALOG_PRODUCTS_PATH = "/1.0/catalog/products"

# The most ASINs one Audible catalog request carries.
MAX_ASINS_PER_REQUEST = 50

BOOK_RESPONSE_GROUPS = (
    "media, product_attrs, product_desc, product_details, "
    "product_extended_attrs, product_plans, rating, series, "
    "relationships, review_attrs, category_ladders, customer_rights"
)

IMAGE_SIZES = "500,1000,2400,3200"

# The publication_datetime Audible puts on a placeholder catalogue record --
# an entry that stands in for a title rather than being one, which Audible
# does send. See filter_products for the records seen carrying it, and for
# why one is dropped rather than served.
UNRELEASED_PLACEHOLDER = "2200-01-01T00:00:00Z"

# Below this many products, normalization runs inline on the event loop; at
# or above it, the whole batch is handed to a single asyncio.to_thread call.
#
# What one product costs is governed by how big that product is, so a single
# figure does not characterise it and the sizes are named alongside every
# number here. build_extras walks every node of the product and then renders
# the whole blob to JSON, and that walk and dump is 50-80% of the total cost
# of normalizing a product -- work that scales with the product rather than
# with the count of fields the DTO names.
#
# Measured (scratchpad benchmark, not part of this repo; 300 iterations per
# size, median of seven runs, on a development machine): a 3.5 KB product
# normalizes in ~0.4ms, 6.5 KB in ~0.55ms, 17 KB in ~1.2ms and 45 KB in ~3ms,
# while the to_thread hop itself costs ~0.2ms of fixed overhead regardless of
# batch size. Read the ratios and the size dependence rather than the absolute
# numbers, which a production worker's hardware will move.
#
# A 50-ASIN chunk -- the largest a single Audible catalog request ever
# returns, see MAX_ASINS_PER_REQUEST -- is therefore ~30ms of uninterruptible
# loop time for ordinary books and ~150ms for a page of large multipart
# titles, and that is what a caller under this threshold pays inline.
#
# What decides the side is the whole fetch list, not the chunk: a caller that
# accumulates every chunk's products and hands them to normalize_products in
# one call offloads whenever its ASIN list runs past this count. A single ASIN,
# a search page and a short series stay inline; a bulk lookup of hundreds of
# ASINs, a long series, and above all an author's whole catalogue cross it. The flagged ~1530-product case blocks the loop for ~1s inline at
# 6.5 KB a product, with nothing else able to run in that window, against the
# same ~1s threaded but with the loop free to service other requests
# throughout. The thread hop is a net wall-clock loss at every size tested;
# the point is solely to stop a single request from stalling every other
# connection the process is holding.
NORMALIZE_THREAD_THRESHOLD = 100

# The upstream product keys normalize_product already reproduces as
# first-class response fields. Every other top-level key Audible sends goes
# into audibleExtras verbatim, so this set is the whole of what decides
# which side of that line a key falls on.
#
# Membership is checked in both directions, because the two directions fail
# differently and only one of them is cheap. Each first-class field that
# reproduces an upstream key reads it through _reproduce, which refuses a key
# not named here, so adding a field without adding its key raises on the
# first product normalized -- in every test and every request alike -- and
# costs a duplicated value until it does. The reverse, a key named here that
# no field reads, is a silent drop rather than a duplicate: withheld from the
# blob by this set and reproduced nowhere. _verify_reproduced_keys_read
# establishes at import that every key below is genuinely read, so neither
# direction can drift unnoticed.
#
# merchandising_summary and publisher_summary are members even though
# strip_html runs over them. HTML-stripping is not a transformation of the
# content, only of its markup, and the two copies were measured at 26% of the
# raw product as pure duplication.
#
# Deliberately NOT members, and therefore passed through whole alongside the
# field parsed out of them: rating, product_images, plans, category_ladders,
# relationships, release_date, episode_number, episode_type,
# publication_datetime, authors, narrators. Each is transformed or only
# partly consumed -- imageUrl is one URL out of a dict of sizes, genres
# flattens category_ladders, series reads the series entries out of
# relationships and leaves the rest, publicationDatetime surfaces
# publication_datetime unchanged while filter_products reads it for its own
# purposes -- so the upstream key still goes into the blob as sent, whole
# object or bare scalar alike. That
# list is documentation and nothing reads it as a constant; a second
# frozenset the code never consults would drift from the normalizer within a
# release. A field read with a plain product.get rather than _reproduce is
# duplicated into the blob, never dropped, which is the direction a mistake
# here has to fail in.
_REPRODUCED_KEYS: frozenset[str] = frozenset({
    "asin",
    "title",
    "subtitle",
    "publisher_name",
    "copyright",
    "isbn",
    "language",
    "format_type",
    "is_adult_product",
    "is_pdf_url_available",
    "read_along_support",
    "runtime_length_min",
    "content_type",
    "content_delivery_type",
    "sku",
    "sku_lite",
    "is_listenable",
    "is_buyable",
    "is_vvab",
    "merchandising_summary",
    "publisher_summary",
    "publication_name",
    "product_state",
    "extended_product_description",
})


# ============================================================
# FETCH
# ============================================================

async def fetch_products(get: AudibleGet, asins: list[str], region: str) -> list[dict[str, Any]]:
    """
    Fetches up to MAX_ASINS_PER_REQUEST products by ASIN from Audible, through
    `get`.

    Returns Audible's products raw. Filtering is the caller's job (see
    filter_products), because only the caller can tell a placeholder from a
    stub among the drops. An empty list of ASINs is an empty result with no
    request made.

    One ASIN is a single-product request, which Audible answers with a 404
    when it has no such record (NotFoundException out of `get`, terminal);
    several are one batch request, which Audible answers with a 200 whatever
    it knows. A single-product 200 whose product is an explicit null is not
    that answer: it is a malformed one, and raises AudibleAPIException, an
    outage, rather than reading as an absence. Transient failures surface as
    AudibleAPIException from `get` and are the caller's to retry.

    Raises RegionException for a region that is not one of the eleven, and
    ValueError for more than MAX_ASINS_PER_REQUEST ASINs or for any value that
    is not an ASIN -- before anything is sent.
    """
    region = validate_region(region)
    if len(asins) > MAX_ASINS_PER_REQUEST:
        raise ValueError(f"at most {MAX_ASINS_PER_REQUEST} ASINs per request")
    wanted = [validated_asin(a) for a in asins]
    if not wanted:
        return []

    if len(wanted) == 1:
        params: dict[str, Any] = {
            "response_groups": BOOK_RESPONSE_GROUPS,
            "image_sizes": IMAGE_SIZES,
        }
        data = await get(region, f"{CATALOG_PRODUCTS_PATH}/{wanted[0]}", params)
        if "product" in data and data["product"] is None:
            raise AudibleAPIException("Audible answered a product request with a null product")
        return [data.get("product", {})] if data.get("product") else []

    params = {
        "asins": ",".join(wanted),
        "response_groups": BOOK_RESPONSE_GROUPS,
        "image_sizes": IMAGE_SIZES,
    }
    data = await get(region, CATALOG_PRODUCTS_PATH, params)
    return data.get("products", [])


# ============================================================
# HELPERS
# ============================================================


def _entries(product: dict, key: str) -> list[dict]:
    """
    The dict entries of the upstream list at `key`, for the parsers below that
    read a list of objects out of a product.

    An entry that is not an object (a bare string, a number) has been seen
    alongside real ones and used to raise out of the parser and fail the
    whole book, so it is skipped and the entries around it are unaffected.
    The upstream list still reaches audibleExtras whole, so nothing is lost.

    The container itself is another matter. A key that is absent, or an
    empty string or object (which the loops this replaces iterated as
    nothing), is an empty list. A null or any other non-list raises TypeError
    and fails the book, as it always did. That raise is the less-data guard
    doing its job: audibleExtras carries the key as sent, a stored copy is
    merged by a shallow union, and a book that normalized would write the null
    over the arrays already stored. Failing here sends the caller to what it
    already holds.
    """
    value = product.get(key, [])
    if isinstance(value, list):
        return [e for e in value if isinstance(e, dict)]
    if not value and isinstance(value, (str, dict)):
        return []
    raise TypeError(f"{key} must be a list, got {type(value).__name__}")


# The reproduced keys whose value is read as text, and so can arrive as
# something that is not. Each is withheld from audibleExtras by
# _REPRODUCED_KEYS because the first-class field carries it, which is exactly
# what makes defaulting the field safe: when the value is unreadable it goes
# into the blob under its own key instead (see is_unreadable_text), and since a
# well-formed response never writes that key there, no stored blob entry is
# overwritten. The columns behind the fields are merged by `answered` and
# `longer_wins`, which leave the stored value for an empty one.
_TEXT_READ_KEYS: frozenset[str] = frozenset({
    "merchandising_summary",
    "publisher_summary",
    "content_type",
})

_UNREADABLE_TEXT_LOG_INTERVAL_SECONDS = 60

# The extrasWithheld value recorded for a product_images that cannot be read.
_WITHHELD_UNREADABLE = "unreadable"

_unreadable_text_counts: dict[str, int] = {}
_unreadable_text_last_logged: dict[str, float] = {}


def _log_unreadable_text(asin: str, key: str, region: str) -> None:
    """
    Reports that Audible sent a text field as something else, at most once
    per _UNREADABLE_TEXT_LOG_INTERVAL_SECONDS per key, through the shared
    window_elapsed gate: a changed upstream shape would otherwise log on
    every product in a page. The key is one of _TEXT_READ_KEYS, Libex's own
    vocabulary; nothing from the value is logged.
    """
    count = _unreadable_text_counts.get(key, 0) + 1
    _unreadable_text_counts[key] = count
    now = time.monotonic()
    if not window_elapsed(
        _unreadable_text_last_logged.get(key), now, _UNREADABLE_TEXT_LOG_INTERVAL_SECONDS
    ):
        return
    logger.warning("Audible sent a text field that is not text", extra={
        "asin": safe_asin_for_log(asin),
        "region": region,
        "text_field": key,
        "occurrences": count,
    })
    _unreadable_text_counts[key] = 0
    _unreadable_text_last_logged[key] = now


def _largest_size_key(product_images: dict) -> str | None:
    """The numeric size key of the largest image in an object of sizes, or
    None when it has none."""
    sizes = [k for k in product_images if isinstance(k, str) and k.isdecimal()]
    return str(max(int(k) for k in sizes)) if sizes else None


def _images_malformed(product_images: Any) -> bool:
    """
    True for a product_images value that cannot be read as sizes: truthy and
    not an object, an object with no numeric size key, or one whose largest
    size holds a truthy value that is not a string. Falsy values of any type
    are no images and are not malformed.
    """
    if not product_images:
        return False
    if not isinstance(product_images, dict):
        return True
    key = _largest_size_key(product_images)
    if key is None:
        return True
    url = product_images.get(key)
    return bool(url) and not isinstance(url, str)


def _best_image(product_images: dict | None) -> str | None:
    """
    Returns the highest resolution image URL with size suffix stripped, or
    None when there is none to read, which includes a malformed
    product_images (see _images_malformed). normalize_product withholds a
    malformed one from audibleExtras and records that in extrasWithheld, so
    the book is served with no image and neither a stored size map nor a
    stored image is replaced.
    """
    if not product_images or _images_malformed(product_images):
        return None
    return strip_image_size_suffix(product_images.get(_largest_size_key(product_images)))


def _audible_link(asin: str, region: str) -> str:
    """Builds an Audible product page link."""
    tld = REGION_MAP.get(region, ".com")
    return f"https://audible{tld}/pd/{asin}"

def _parse_release_date(raw: str | None) -> str | None:
    """
    Converts a raw Audible release date string to ISO 8601 format.
    Audimeta stores dates as DateTime and outputs .toISO(), e.g. "2021-03-02T00:00:00.000+00:00".

    A truthy value that is not a string raises TypeError and fails the book,
    deliberately. release_date is not in _REPRODUCED_KEYS, so audibleExtras
    carries it as sent, and a stored copy is merged by a shallow union in
    which the incoming key wins: defaulting the field would let a book that
    normalized write the malformed value over the date already stored there.
    The failure sends the caller to what it already holds. A falsy value of
    any type is no date, as it always was.
    """
    if not raw:
        return None
    if not isinstance(raw, str):
        raise TypeError(f"release_date must be a string, got {type(raw).__name__}")
    try:
        dt = datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return dt.isoformat()
    except ValueError:
        return raw


def _log_value(value: str) -> str:
    """An upstream string for a log field: as-is if it passes
    is_safe_log_value, else the sentinel "REDACTED"."""
    return value if is_safe_log_value(value) else "REDACTED"


def _parse_authors(product: dict, region: str) -> list[dict]:
    """
    Extracts author objects shaped after AudiMeta's MinimalAuthorDto.

    An author entry's asin is checked for ASIN shape -- ten characters of
    A-Z0-9 -- and then written through unchanged whether it passes or not.
    The check only reports. That is deliberate, and the reason is what a
    consumer does with the value rather than the predicate.

    Audible's contributor entries are not always identifiers. Measured live
    2026-09-06 against /1.0/catalog/products, the authors array can carry
    the contributor's own name ({"asin": "Trinka Enell", "name": "Trinka
    Enell"}, us B0DKQBH3CR), a single stray character ({"asin": "v"}, jp
    B0H6ZCMBW5), or a fragment of a twice percent-encoded surname
    ({"asin": "25A7anha", "name": "Vitor Peçanha"} -- the tail of
    "Pe%25C3%25A7anha" -- us B09W33RNX7 and br B0CC8M5KXV).

    Nulling one here would cost more than it recovers. A consumer that
    already holds such a value as an identifier matches on it, so handing it
    a null instead -- or an uppercased form of a lowercased real ASIN, which
    is the same move -- resolves to a second identity for the same person
    rather than the one it already has, and the same contributor then
    appears twice for good.

    The warning is therefore the whole product of this pass. The affected
    population cannot be counted from stored data -- junk that is already ten
    uppercase characters is indistinguishable from a real ASIN, and only
    Audible sending it again reveals it -- so logging every rejected value
    with the product, the contributor name and the region is the only way to
    size it. Enforcement waits on that number and on a plan for the identities
    already recorded.

    A value longer than 12 characters is still nulled, as it was before any of
    this: the identifier column that stores it is 12 characters wide, and the
    ceiling keeps an over-long value from failing the insert. It is malformed
    by the same measure, so it is logged too.

    A non-object entry, a null or non-string name and a non-string asin do
    not fail the book. An entry with no usable name is
    dropped, as a blank one always was, and a non-string asin reads as no asin.
    The raw list still reaches audibleExtras whole, so the drop loses nothing.
    """
    authors = []
    for author in _entries(product, "authors"):
        raw_name = author.get("name")
        name = raw_name.replace("\t", "").strip() if isinstance(raw_name, str) else ""
        raw_asin = author.get("asin")
        asin = raw_asin.replace("\t", "").strip() if raw_asin and isinstance(raw_asin, str) else None
        if asin and not is_valid_asin(asin):
            logger.warning("Audible sent a malformed author ASIN", extra={
                "asin": safe_asin_for_log(product.get("asin", "")),
                "malformed_author_asin": _log_value(asin),
                "author_name": _log_value(name),
                "region": region,
            })
        if asin and len(asin) > 12:
            asin = None
        if name:
            authors.append({
                "id": None,
                "asin": asin,
                "name": name,
                "region": region,
                "regions": [region],
                "image": None,
                "updatedAt": None,
            })
    return authors


def _parse_narrators(product: dict) -> list[dict]:
    """Extracts narrator objects matching AudiMeta's NarratorDto.

    The name is stripped before it is tested, so a whitespace-only name is
    dropped like an empty or null one instead of publishing as "". A nameless
    entry is dropped on purpose: NarratorDto carries only a name, so there is
    nothing in the entry a caller could use. The drop loses no data, because
    narrators is not in _REPRODUCED_KEYS and the upstream list therefore still
    reaches audibleExtras whole, nameless entries included.

    A non-object entry and a name that is not a string
    are dropped the same way, and for the same reason, instead of failing the
    book.
    """
    narrators = []
    for n in _entries(product, "narrators"):
        raw_name = n.get("name")
        name = raw_name.strip() if isinstance(raw_name, str) else ""
        if name:
            narrators.append({"name": name, "updatedAt": None})
    return narrators


def _parse_genres(product: dict) -> list[dict]:
    """
    Extracts genre objects matching AudiMeta's GenreDto with type and betterType.

    A name that is only whitespace is dropped like an empty one instead of
    publishing as a blank genre. The name that is kept is published as sent,
    padding and all: only whether to keep it is decided on the stripped value.
    A null inner ladder, a non-object ladder or rung and a name
    that is not a string are dropped rather than failing the book, and a
    dropped rung still counts toward the position of the ones after it, so a
    later rung keeps its Genres or Tags type. category_ladders is not in
    _REPRODUCED_KEYS, so everything dropped here still reaches audibleExtras.
    """
    genres = []
    seen = set()
    for ladder in _entries(product, "category_ladders"):
        ladder_rungs = ladder.get("ladder")
        if not isinstance(ladder_rungs, list):
            continue
        for rung_index, rung in enumerate(ladder_rungs):
            if not isinstance(rung, dict):
                continue
            name = rung.get("name")
            asin = rung.get("id")
            if isinstance(name, str) and name.strip() and name not in seen:
                seen.add(name)
                genre_type = "Genres" if rung_index == 0 else "Tags"
                genres.append({
                    "asin": asin,
                    "name": name,
                    "type": genre_type,
                    "betterType": genre_type.lower().rstrip("s"),
                    "updatedAt": None,
                })
    return genres


# How often the "plans entries present but unreadable" warning below actually
# logs, once it starts firing repeatedly. An upstream rename of plan_name is
# exactly the scenario that trips this on every product in a page, or a whole
# search response, at once -- a line per product is the flood that would bury
# the one signal worth keeping: that it happened at all, and how much.
_UNREADABLE_PLANS_LOG_INTERVAL_SECONDS = 60

_unreadable_plans_count = 0
_unreadable_plans_last_logged: float | None = None


def _log_unreadable_plans(asin: str) -> None:
    """
    Reports "Audible sent plans entries this parser can't read" at most once
    per _UNREADABLE_PLANS_LOG_INTERVAL_SECONDS, through the shared
    window_elapsed gate -- this can fire on every product at once if
    plan_name itself is what changed shape, and an uncapped line per product
    would flood out the report of the one incident causing all of them. asin
    is the most recently affected product in the window, not every one of
    them -- enough to start looking without paying for a line per
    occurrence.
    """
    global _unreadable_plans_count, _unreadable_plans_last_logged
    _unreadable_plans_count += 1
    now = time.monotonic()
    if not window_elapsed(
        _unreadable_plans_last_logged, now, _UNREADABLE_PLANS_LOG_INTERVAL_SECONDS
    ):
        return
    logger.warning("Audible plans entries present but unreadable", extra={
        "asin": safe_asin_for_log(asin),
        "occurrences": _unreadable_plans_count,
    })
    _unreadable_plans_count = 0
    _unreadable_plans_last_logged = now


def _parse_plans(product: dict) -> list[str] | None:
    """
    Extracts plan_names from the plans array.

    None when the response carries no `plans` key at all; [] when it carries
    an explicitly empty one. The two mean different things to a consumer that
    keeps what it already holds -- None says Audible was silent and the stored
    value should stand, [] says Audible asserted there are none and replaces
    it -- so this field can surface null where a list is otherwise expected.

    A third case folds into the None side rather than the [] side: entries
    are present but not one of them yields a readable plan_name (the key
    renamed, an entry carrying only plan_id, a plan_name that is itself
    null). plan_name appears nowhere else in the codebase but this line, so
    an upstream shape change here is invisible until it's read back --
    and unlike an explicitly empty array, which is Audible asserting "there
    are no plans," this is Libex failing to read whatever Audible actually
    sent. Treating it as silence (None: leave whatever is held alone) rather
    than as an assertion ([]: overwrite) is what stops a rename from silently
    emptying stored plans across the whole corpus the next time every book
    gets re-read. Logged, because otherwise nobody would find out until the
    plans were already gone.

    A partial miss -- some entries readable, some not -- does NOT take this
    branch: it returns whatever it could read, same as always, because a
    partial answer is a real answer, not a total parse failure.

    A non-object entry and a plan_name that is not a string are unreadable by
    the same measure: they are skipped, and when nothing readable is left the
    result is None, not a failure of the book. A plans value that is not a
    list still fails it (see _entries).
    """
    raw = product.get("plans")
    if raw is None:
        return None
    names = [
        p["plan_name"] for p in _entries(product, "plans")
        if p.get("plan_name") and isinstance(p["plan_name"], str)
    ]
    if raw and not names:
        _log_unreadable_plans(product.get("asin", ""))
        return None
    return names


def _parse_series(product: dict, region: str) -> list[dict]:
    """
    Extracts series objects matching AudiMeta's MinimalSeriesDto.

    A non-object entry is skipped rather than
    failing the book; relationships is not in _REPRODUCED_KEYS, so the raw
    list still reaches audibleExtras.
    """
    series_list = []
    for item in _entries(product, "relationships"):
        if item.get("relationship_type") == "series":
            series_list.append({
                "asin": item.get("asin"),
                "name": item.get("title"),
                "position": item.get("sequence"),
                "region": region,
                "updatedAt": None,
            })
    return series_list


# The keys _reproduce has been asked for, while _verify_reproduced_keys_read
# is normalizing its one probe product, and None at every other moment --
# including the whole of normal service, where the check below costs a single
# is-None comparison per read.
#
# Recorded here rather than by watching product.get, because the two do not
# mean the same thing. A key read with a plain product.get is transformed or
# only partly consumed and therefore must stay OUT of _REPRODUCED_KEYS, so a
# probe that counted those reads as coverage would wave through exactly the
# mistake it exists to catch: relationships added to the set, its series
# entries parsed out, and every other entry in it gone from the blob with
# nothing raising.
_reproduced_keys_read: set[str] | None = None


def _reproduce(product: dict, key: str) -> Any:
    """
    Reads an upstream key that normalize_product reproduces as a first-class
    response field, refusing any key that is not in _REPRODUCED_KEYS.

    This is what turns that set from a convention into a requirement. A
    first-class field added to the normalizer without its upstream key added
    to the set raises here on the first product normalized -- every test and
    every request hits this path -- so the pair cannot fall out of step
    quietly and leave the same value appearing twice in the response.

    That is the cheap direction. The expensive one -- a key the set names
    that no field reads, which is excluded from the blob and reproduced
    nowhere -- is caught by _verify_reproduced_keys_read, which watches this
    function to establish it.

    Not used for a field that is transformed or only partly consumed; those
    are read with a plain product.get and appear in the blob as well, which
    is why reaching for the wrong one of the two costs a duplicated value
    rather than a lost one.
    """
    if key not in _REPRODUCED_KEYS:
        raise RuntimeError(
            f"{key} is read as a first-class response field but is missing from _REPRODUCED_KEYS"
        )
    if _reproduced_keys_read is not None:
        _reproduced_keys_read.add(key)
    return product.get(key)


# ============================================================
# EXPLICIT NULLS
# ============================================================

# The scalar upstream keys whose explicit null explicit_null_fields reports,
# each with the published fields it feeds, in the order normalize_product
# builds them. Scalars only. A key Audible normally sends as an object or a
# list (authors, narrators, category_ladders, relationships, plans,
# product_images, rating, and the rating's overall_distribution) is left out
# on purpose: a null there is Audible failing to answer rather than Audible
# asserting an empty value. normalize_product raises on most of them, which
# is the outage the caller sees, and the rest (plans, product_images) already
# normalize to "said nothing"; reporting any of them as a null would offer the
# caller a clear where there is none.
_NULLABLE_SCALAR_FIELDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("title", ("title",)),
    ("subtitle", ("subtitle",)),
    ("merchandising_summary", ("description",)),
    ("publisher_summary", ("summary",)),
    ("publisher_name", ("publisher",)),
    ("copyright", ("copyright",)),
    ("isbn", ("isbn",)),
    ("language", ("language",)),
    ("format_type", ("bookFormat",)),
    ("release_date", ("releaseDate",)),
    ("is_adult_product", ("explicit",)),
    ("is_pdf_url_available", ("hasPdf",)),
    ("read_along_support", ("whisperSync",)),
    ("runtime_length_min", ("lengthMinutes",)),
    ("content_type", ("contentType",)),
    ("content_delivery_type", ("contentDeliveryType",)),
    ("sku", ("sku",)),
    ("sku_lite", ("skuGroup",)),
    ("is_listenable", ("isListenable",)),
    ("is_buyable", ("isAvailable", "isBuyable")),
    ("is_vvab", ("isVvab",)),
    ("publication_name", ("publicationName",)),
    ("publication_datetime", ("publicationDatetime",)),
    ("extended_product_description", ("extendedProductDescription",)),
    ("product_state", ("productState",)),
)

# The two podcast-only fields: normalize_product reads these upstream keys only
# for a podcast and publishes None for anything else whatever Audible sent, so
# a null on a non-podcast is not what the published value reflects.
_PODCAST_ONLY_FIELDS: tuple[tuple[str, str], ...] = (
    ("episode_number", "episodeNumber"),
    ("episode_type", "episodeType"),
)


def explicit_null_fields(product: dict) -> tuple[str, ...]:
    """
    The published fields whose Audible source key was present in `product`
    with the value None, as opposed to omitted.

    The two read differently to a consumer. An omitted key is Audible saying
    nothing and a stored value should stand; an explicit null is Audible
    saying the value is empty, which a consumer that wants to tell the two
    apart can now do. Only the signal is produced here: normalize_product
    still publishes None for both, the scalar coalescing that keeps stored data
    from being cleared is unchanged, and nothing about what is stored or
    served depends on this.

    Presence is the whole test, so a key from a response group that was not
    requested, or a key Audible left out, is never reported. Container nulls
    are never reported either (see _NULLABLE_SCALAR_FIELDS); the rating's three
    scalars are, when the rating object itself is present. Each name appears
    once. A product with no asin has nothing to key a report on and the asin
    itself is never reported, since a product without one is not served.
    """
    found: list[str] = []
    for key, fields in _NULLABLE_SCALAR_FIELDS:
        if key in product and product[key] is None:
            found.extend(fields)

    rating = product.get("rating")
    if isinstance(rating, dict):
        distribution = rating.get("overall_distribution")
        if isinstance(distribution, dict):
            if "average_rating" in distribution and distribution["average_rating"] is None:
                found.append("rating")
            if "num_ratings" in distribution and distribution["num_ratings"] is None:
                found.append("numRatings")
        if "num_reviews" in rating and rating["num_reviews"] is None:
            found.append("numReviews")

    content_type = product.get("content_type")
    if isinstance(content_type, str) and content_type.lower() == "podcast":
        for key, name in _PODCAST_ONLY_FIELDS:
            if key in product and product[key] is None:
                found.append(name)

    return tuple(dict.fromkeys(found))


def explicit_nulls_by_asin(products: list[dict]) -> dict[str, tuple[str, ...]]:
    """
    explicit_null_fields for each raw product, keyed by the product's asin. A
    product with no nulls maps to (), which says "looked and found none";
    a product that was never looked at has no entry at all, which is how a
    caller tells that apart.
    """
    return {p["asin"]: explicit_null_fields(p) for p in products if p.get("asin")}


# ============================================================
# NORMALIZATION
# ============================================================

def normalize_product(product: dict, region: str) -> dict[str, Any]:
    """
    Normalizes a raw Audible product into Libex's book response shape: the
    first-class camelCase fields named below, derived from AudiMeta's BookDto,
    and audibleExtras and extrasWithheld past them.

    audibleExtras carries every top-level key Audible sent that the fields
    above it do not already reproduce, verbatim (see _REPRODUCED_KEYS and
    build_extras). Two things about it never relax. Nothing out of it is
    ever hoisted into this dict -- no splat, no key added at runtime -- which
    is what makes an upstream key called "asin" or "__proto__" structurally
    unable to collide with a first-class field: it stays nested, and the
    caller reads it there. And nothing in it is ever fetched, not to validate
    it, not to check an image is still there, not from a script; it carries
    product_images, social_media_images and relationships[].url, and those
    are data, not inputs to a request.

    extrasWithheld is a top-level key here, deliberately not one inside
    audibleExtras, and is absent altogether from THIS dict when nothing was
    withheld -- the response model carries it to the wire as null either way,
    so a book object always has the key. Inside the blob it would be Libex's
    own invention sitting in a namespace documented as verbatim Audible, and
    it would collide with a real upstream key of that name the day Audible
    ships one.

    audibleExtras is None rather than {} when the blob was dropped whole: {}
    would assert Audible sent nothing extra, where None says Libex has
    nothing to offer and lets a consumer that keeps what it already holds
    leave a stored blob alone. Same tri-state and same reasoning as plans (see
    _parse_plans), though the two are not merged the same way at the other end.
    """
    asin = _reproduce(product, "asin") or ""
    series_list = _parse_series(product, region)

    # A text field that is not text is published as no value and rides into
    # the blob under its own key, as sent, so the field is defaulted and
    # nothing Audible sent is lost. See _TEXT_READ_KEYS for why that cannot
    # overwrite anything stored.
    unreadable = {
        k for k in _TEXT_READ_KEYS if is_unreadable_text(product.get(k))
    }
    for k in sorted(unreadable):
        _log_unreadable_text(product.get("asin") or "", k, region)

    def _text(key: str) -> Any:
        value = _reproduce(product, key)
        return None if key in unreadable else value

    content_type = _text("content_type")
    is_podcast = content_type and content_type.lower() == "podcast"

    # Every top-level key no first-class field reproduces rides into the blob
    # as sent, in the order Audible sent it.
    #
    # product_images stays out of it when it cannot be read as sizes. It is
    # carried in the blob as sent and the stored blob is merged by a shallow
    # union in which the incoming key wins, so a malformed value there would
    # replace the sizes already stored. Left out, the union finds the key
    # absent and keeps them, and the omission is recorded in extrasWithheld.
    images_malformed = _images_malformed(product.get("product_images"))
    passthrough = {
        k: v for k, v in product.items()
        if (k not in _REPRODUCED_KEYS or k in unreadable)
        and not (k == "product_images" and images_malformed)
    }
    extras, withheld = build_extras(passthrough, asin, region)
    if images_malformed:
        withheld["product_images"] = _WITHHELD_UNREADABLE
        _log_extras_incident(asin, region, "product_images")

    book: dict[str, Any] = {
        "asin": asin,
        "title": _reproduce(product, "title"),
        "subtitle": _reproduce(product, "subtitle"),
        "description": strip_html(_text("merchandising_summary")),
        "summary": strip_html(_text("publisher_summary")),
        "region": region,
        "regions": [region],
        "publisher": _reproduce(product, "publisher_name"),
        "copyright": _reproduce(product, "copyright"),
        "isbn": _reproduce(product, "isbn"),
        "language": _reproduce(product, "language"),
        # All three rating reads walk the same unguarded chain, and the two
        # halves of it are at different depths: num_ratings sits inside
        # overall_distribution beside average_rating, num_reviews sits
        # directly on rating. Both confirmed against live products rather
        # than taken from the fixtures, which carry neither -- us B08G9PRS1K
        # returns rating.overall_distribution.num_ratings 312,915 and
        # rating.num_reviews 48,002.
        #
        # A rating key present and explicitly null raises AttributeError out
        # of this chain, and that raise is the less-data guard doing its job:
        # it reaches the caller, which can fall back to whatever it already
        # holds for the book. A defensive .get on each step would turn a
        # shrinkage signal into three silent Nones handed over real values.
        "rating": product.get("rating", {}).get("overall_distribution", {}).get("average_rating"),
        "numRatings": product.get("rating", {}).get("overall_distribution", {}).get("num_ratings"),
        "numReviews": product.get("rating", {}).get("num_reviews"),
        "bookFormat": _reproduce(product, "format_type"),
        "releaseDate": _parse_release_date(product.get("release_date")),
        # Tri-state like isListenable/isBuyable/isVvab below -- see the
        # comment there for the full contract; a hard False here on a
        # missing key would be Libex asserting an answer Audible never
        # gave, and a consumer taking it unguarded would overwrite a stored
        # True with a fetch that said nothing at all.
        #
        # is_adult_product and is_pdf_url_available are present on every
        # product Audible sends, so this guard is precautionary rather than
        # a fix for a hole seen in the wild. read_along_support is
        # genuinely absent for a real, common slice of the catalog --
        # podcasts and some anthology titles -- and its absence there reads
        # as "does not apply to this content" rather than Audible declining
        # to answer. The effect a storing consumer needs to guard against is
        # identical either way: a response that omits the key is not a
        # negative assertion, which is why the flag is tri-state rather
        # than defaulted at normalization time.
        "explicit": _reproduce(product, "is_adult_product"),
        "hasPdf": _reproduce(product, "is_pdf_url_available"),
        "whisperSync": _reproduce(product, "read_along_support"),
        "imageUrl": _best_image(product.get("product_images", {})),
        "lengthMinutes": _reproduce(product, "runtime_length_min"),
        "link": _audible_link(asin, region),
        "contentType": content_type,
        "contentDeliveryType": _reproduce(product, "content_delivery_type"),
        "episodeNumber": str(product.get("episode_number")) if is_podcast and product.get("episode_number") else None,
        "episodeType": product.get("episode_type") if is_podcast else None,
        "sku": _reproduce(product, "sku"),
        "skuGroup": _reproduce(product, "sku_lite"),
        # No default: True/False here would be Libex asserting an answer
        # Audible never gave. None means "Audible said nothing" and is what
        # lets a consumer that stores these tell that apart from an explicit
        # false and leave a stored value alone. isAvailable and isBuyable are both is_buyable -- AudiMeta's
        # own DTO derives them the same way (see settle_flags below for
        # where None stops being a valid outward value).
        "isListenable": _reproduce(product, "is_listenable"),
        "isAvailable": _reproduce(product, "is_buyable"),
        "isBuyable": _reproduce(product, "is_buyable"),
        "isVvab": _reproduce(product, "is_vvab"),
        "plans": _parse_plans(product),
        "updatedAt": None,
        "authors": _parse_authors(product, region),
        "narrators": _parse_narrators(product),
        "genres": _parse_genres(product),
        "series": series_list,
        "publicationName": _reproduce(product, "publication_name"),
        # Read straight through rather than parsed. Audible already sends an
        # ISO-8601 instant here ("2026-03-02T00:00:00Z"), unlike release_date,
        # which is a bare date _parse_release_date has to give a timezone to.
        #
        # This one is both a field and a blob key on purpose: filter_products
        # reads publication_datetime for its own decision, which makes it
        # partly consumed rather than reproduced, so it is not in
        # _REPRODUCED_KEYS and appears in audibleExtras as well.
        "publicationDatetime": product.get("publication_datetime"),
        # Stored exactly as Audible sent it, markup and all. strip_html
        # replaces a tag with nothing, so a paragraph break becomes no break
        # at all and "...end.</p><p>Next..." reads as "...end.Next..." --
        # paragraph structure destroyed in the one field whose whole job is
        # the long description. This field is excluded from the blob, so this
        # is the only copy of it there is.
        "extendedProductDescription": _reproduce(product, "extended_product_description"),
        "productState": _reproduce(product, "product_state"),
        "audibleExtras": extras,
    }
    if withheld:
        book["extrasWithheld"] = withheld
    return book


# The region the probe below normalizes under. Deliberately not one of the
# eleven: the probe's output is discarded, region never decides which keys
# normalize_product reads, and a real region sitting here would read as a
# default that some later caller could inherit.
_PROBE_REGION = "probe"


def _verify_reproduced_keys_read() -> None:
    """
    Raises unless every key in _REPRODUCED_KEYS is actually read as a
    first-class response field, established by normalizing one probe product
    and recording which keys normalize_product asked _reproduce for.

    _reproduce guards the other direction, and that is the cheap mistake: a
    field reading a key the set does not name leaves the value in the
    response twice. This is the expensive one. A key the set names that
    nothing reads is excluded from audibleExtras precisely because the set
    names it, and reproduced in no field because nothing asks for it, so it
    is dropped with nothing raising -- the silent loss the blob exists to end,
    happening inside the mechanism built to prevent it. It is one forgotten
    line away at all times: remove or rename a first-class field, leave its
    key in the set, and the field and its blob entry disappear together.

    Proved by running the real normalizer rather than by scanning this file
    for _reproduce calls. A source scan is only ever as good as the spellings
    it anticipates: the call shape it fails to recognise reads as an unused
    key and raises against a normalizer that is in fact correct, and a key
    genuinely read through a shape the scan does not model reads as covered
    when it is not. Running the thing removes the question -- nothing about
    how a call is written can fool it.

    Two limits, stated rather than papered over. It establishes that a key is
    read, not that the value reaches the response, so a read whose result was
    then discarded would still pass. And the probe product is empty, so a key
    reproduced only inside a branch an empty product does not take is
    reported as unread -- deliberately, because a conditionally reproduced
    key is dropped for every product that misses that branch, which is the
    same defect in a narrower window. An empty product is also the weakest
    input this normalizer will ever be handed, Audible omitting keys being
    ordinary; if normalizing one raises, that raise is a defect in its own
    right and is left to propagate rather than caught here.

    Runs at import, so the mismatch stops a process instead of waiting for a
    request: this module cannot be imported, in a test run or a worker start
    alike, while a key in the set is reproduced by nothing.
    """
    global _reproduced_keys_read
    _reproduced_keys_read = set()
    try:
        normalize_product({}, _PROBE_REGION)
        read = _reproduced_keys_read
    finally:
        _reproduced_keys_read = None

    unread = sorted(_REPRODUCED_KEYS - read)
    if unread:
        raise RuntimeError(
            "_REPRODUCED_KEYS names upstream keys that no first-class response "
            "field reads, so each is withheld from audibleExtras and reproduced "
            f"nowhere: {', '.join(unread)}"
        )


_verify_reproduced_keys_read()


# Settled values for the seven tri-state flags above, matched to the defaults
# a stored book carries for the same fields, so a brand-new record and a live
# fetch settle to the same value: there is nothing stored yet to disagree
# with. Once a consumer holds a real, asserted value and a later fetch is
# silent on that same key, it keeps the asserted value while a fresh
# normalization still settles to the fixed default here -- the two can
# disagree at that point, and that is the accepted trade: an asserted answer
# already held outranks a default standing in for silence on the wire.
# isListenable/isAvailable/isBuyable settle to True, mirroring AudiMeta's
# own ingest for the three it defines that way. explicit/hasPdf/whisperSync
# settle to False, mirroring AudiMeta's own ingest the other way -- AudiMeta
# applies `?? false` to exactly these three -- which is also the
# stored default for each (False, never null). isVvab -- which AudiMeta
# doesn't have -- settles to Libex's own prior emitted value rather
# than inventing a new one.
_FLAG_SETTLE_DEFAULTS: dict[str, bool] = {
    "isListenable": True,
    "isAvailable": True,
    "isBuyable": True,
    "isVvab": False,
    "explicit": False,
    "hasPdf": False,
    "whisperSync": False,
}


def settle_flags(book: dict[str, Any]) -> dict[str, Any]:
    """
    Fills a None tri-state flag with its settled default, for anything that
    is about to leave the service layer: every BookResponse field is a plain
    bool with no null variant, and a filter matching on these keys compares
    against real values, so None can reach neither. Storing is the one use
    this must NOT run before -- a consumer needs the None to tell "Audible
    said nothing" apart from an explicit false (see normalize_product above)
    -- so a caller settles only the value it hands back, never what it hands
    to storage.

    plans rides along in the same step for the same reason, even though it
    isn't a bool: _parse_plans is deliberately tri-state too (None for "no
    plans key at all", so a stored array is left alone, vs. [] for "Audible
    said explicitly empty," which overwrites -- see _parse_plans), and
    BookResponse.plans is `list[str]` with no null variant, same as the seven
    flags below. pydantic does not fall back to a field's default_factory on
    an explicit None, it raises, and every route that serves this fell over on
    exactly that until it was settled here rather than in _parse_plans.

    Returns a new dict; the input is never mutated, since some callers still
    need the unsettled version afterwards -- they store the tri-state and
    serve the settled result of the same call.

    Only settles a key that is PRESENT and None -- never adds a key a dict
    never carried. normalize_product always carries all seven flags plus
    plans, so nothing that went through it is affected by the distinction;
    what it protects is a dict from a source outside that contract, whose
    rows already carry real, non-null booleans and an already-listed plans
    and need no settling in the first place.
    """
    settled = dict(book)
    for key, default in _FLAG_SETTLE_DEFAULTS.items():
        if key in settled and settled[key] is None:
            settled[key] = default
    if "plans" in settled and settled["plans"] is None:
        settled["plans"] = []
    return settled


def settle_flags_list(books: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Maps settle_flags over a list. See that function for what and why."""
    return [settle_flags(b) for b in books]


async def normalize_products(products: list[dict], region: str) -> list[dict[str, Any]]:
    """
    Normalizes a batch of raw Audible products, offloading the whole batch
    to a worker thread when it's large enough to be worth the hop (see
    NORMALIZE_THREAD_THRESHOLD). One thread hop for the entire batch, never
    one per product -- a hop costs ~0.2ms whatever it carries, against
    ~0.4-3ms of work per product depending on that product's size, so paying
    it per product would add hundreds of milliseconds of pure overhead across
    the hundreds-to-low-thousands batches this exists for.

    normalize_product touches only its own arguments and pure helpers
    (strip_html, strip_image_size_suffix, datetime parsing, and the sanitizing
    walk in extras.py) -- no DB session, no cache, and no read of any
    ContextVar, so running it on another thread carries no correctness risk.
    Three pieces of shared mutable state are in reach and none changes that.
    The counters behind the windowed warnings (_log_unreadable_plans,
    _log_unreadable_text, _log_extras_incident) decide only how often a
    warning prints, never what any product normalizes to, so a lost increment across concurrent batches
    costs an off-by-a-few occurrence count and nothing else. The
    _reproduced_keys_read recorder every _reproduce call checks is written
    only by _verify_reproduced_keys_read, at import, long before any batch
    exists: every read from a worker thread sees the None it is left at, and
    no batch ever writes it. In particular
    it never reads the author_books_concurrency ContextVar in client.py,
    which wouldn't propagate into a to_thread worker the way a normal await
    does -- moot here since nothing in this path looks at it.

    Ordering matches the input list either way (a single list comprehension,
    run inline or inside one to_thread call), and a malformed product raises
    out of that comprehension exactly as it always has -- offloading doesn't
    change which products succeed or fail relative to each other, only which
    thread does the work.
    """
    if len(products) < NORMALIZE_THREAD_THRESHOLD:
        return [normalize_product(p, region) for p in products]
    return await asyncio.to_thread(lambda: [normalize_product(p, region) for p in products])


def is_placeholder_record(p: dict) -> bool:
    """True for a titled record carrying the UNRELEASED_PLACEHOLDER date.

    The title is part of the rule: a titleless hollow stub is a stub whatever
    date it carries, and stays "not found". Only a record Audible actually
    described and then marked as a stand-in is a placeholder.
    """
    return bool(p.get("title")) and p.get("publication_datetime") == UNRELEASED_PLACEHOLDER


def filter_products(products: list[dict]) -> list[dict]:
    """
    Drops two disjoint categories of product from a raw Audible list:
    anything with no title, and anything whose publication_datetime is the
    UNRELEASED_PLACEHOLDER sentinel. A caller that serves catalogue pages
    straight from a listing never hydrates afterwards, so what survives this
    filter is exactly what that response ships; a name built around
    "unreleased" placeholders alone would undersell what a new-release or
    search listing actually passes through.

    Also load-bearing for resolving a nonexistent-but-well-formed ASIN to
    "not found": probed live, Audible's catalog endpoints don't 404 for one
    of those in either fetch_products branch -- they return 200 with a
    hollow, titleless stub instead (a bogus single ASIN and a bogus ASIN
    in a batch request both came back that way; a real ASIN came back
    fully populated). Dropping anything with no title here is what turns
    that stub into an empty result rather than a phantom book with every
    field blank.

    The UNRELEASED_PLACEHOLDER clause carries no comparable confirmation
    from when it was written. Measured since: 1,100 products across all
    eleven regions, two sorts each (-ReleaseDate and -Title, 50 per call --
    11 regions x 2 sorts x 50), plus a further 500 in us across ten calls
    spanning nine query shapes (10 calls x 50 per call) -- ascending and
    descending release-date sorts, the descending sort run over two pages
    (which is why the call count exceeds the shape count), three prolific
    author catalogues, a narrator catalogue, "preorder" and "coming soon"
    keyword searches, a title sort -- carried zero products with a
    publication_datetime matching the sentinel, and zero hollow titleless
    stubs of the kind above. Only the -ReleaseDate calls carry a
    sort-order argument, and only on an assumption this sample cannot
    verify -- every observed product had already passed the filter, so the
    sample is silent on what it removed -- that Audible's -ReleaseDate sort
    keys on the same publication_datetime field the clause reads. On that
    assumption, a product carrying the sentinel would have ranked first in
    every one of those calls, and none did. The -Title and
    catalogue/keyword calls carry no such guarantee; their zero count is a
    plain absence, nothing more. Real pre-orders pass through untouched
    with real dates in both publication_datetime and release_date --
    furthest observed: jp 2029-01-01, br 2028-05-02, us/uk/ca/au/fr
    2027-12-10, de 2027-05-11 (the remaining three regions matched one of
    these dates rather than adding a new one).

    The constant and this clause appear in 74a79a3, the project's earliest
    substantive commit -- the two commits before it are a bare license/
    readme and empty scaffolding -- so where the clause came from is not
    recorded.

    What the clause catches is real, and asking Audible for one directly is
    what shows it. us B0182NWM9I and us B009CFOEGK each answer 200 carrying
    publication_datetime exactly 2200-01-01T00:00:00Z, a title borrowed from
    a real franchise ("Harry Potter", "The Lord of the Rings"), publisher_name
    "ZZZ - Series Advisor Placeholder", product_state
    NOT_AVAILABLE_FOR_PURCHASE, is_buyable and is_listenable both false, a
    zero runtime and no ISBN -- placeholder catalogue entries standing in for
    a series, not titles anyone can listen to. Dropping them is the right
    answer, so the clause stays and the records it removes stay removed.

    The 1,600-product sample above is not in tension with that. Every call in
    it went through a sort or a keyword search, and what those surface is the
    sellable catalogue; a zero count there says a placeholder does not show up
    in ordinary browse results, not that Audible has none to send. Both
    confirmations above came from asking for one ASIN by name, which is
    exactly the path a caller takes and the sample never did.
    """
    return [p for p in products if p.get("title") and not is_placeholder_record(p)]
