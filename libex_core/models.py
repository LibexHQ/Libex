"""
AudiMeta-shaped response models.

These are the DTOs the hosted API returns for books, chapters, and series --
shaped and named to match AudiMeta's own response bodies field for field, so
that anything built against AudiMeta works against Libex without changes.
Field names are camelCase because AudiMeta's are; optionality and defaults
mirror what AudiMeta actually returns rather than what the underlying data
would otherwise suggest. They carry no database, cache, or web-framework
dependency of their own so the shape of a response can be reused anywhere
this package is embedded, independent of how it is served.
"""

# Standard library
from typing import Any

# Third party
from pydantic import BaseModel, Field


# ============================================================
# NESTED OBJECT SCHEMAS
# ============================================================

class NarratorResponse(BaseModel):
    name: str
    updatedAt: str | None = None


class GenreResponse(BaseModel):
    asin: str | None = None
    name: str | None = None
    type: str | None = None
    betterType: str | None = None
    updatedAt: str | None = None


class SeriesRefResponse(BaseModel):
    asin: str | None = None
    name: str | None = None
    region: str | None = None
    position: str | None = None
    updatedAt: str | None = None


class AuthorRefResponse(BaseModel):
    id: int | None = None
    asin: str | None = None
    name: str | None = None
    region: str | None = None
    regions: list[str] = Field(default_factory=list)
    image: str | None = None
    updatedAt: str | None = None


# ============================================================
# BOOK RESPONSE
# ============================================================

class BookResponse(BaseModel):
    asin: str
    title: str | None = None
    subtitle: str | None = None
    description: str | None = None
    summary: str | None = None
    region: str
    regions: list[str] = Field(default_factory=list)
    publisher: str | None = None
    copyright: str | None = None
    isbn: str | None = None
    language: str | None = None
    rating: float | None = None
    bookFormat: str | None = None
    releaseDate: str | None = None
    explicit: bool = False
    hasPdf: bool = False
    whisperSync: bool = False
    imageUrl: str | None = None
    lengthMinutes: int | None = None
    link: str | None = None
    contentType: str | None = None
    contentDeliveryType: str | None = None
    episodeNumber: str | None = None
    episodeType: str | None = None
    sku: str | None = None
    skuGroup: str | None = None
    isListenable: bool = False
    isAvailable: bool = False
    isBuyable: bool = False
    isVvab: bool = False
    plans: list[str] = Field(default_factory=list)
    updatedAt: str | None = None
    authors: list[AuthorRefResponse] = Field(default_factory=list)
    narrators: list[NarratorResponse] = Field(default_factory=list)
    genres: list[GenreResponse] = Field(default_factory=list)
    series: list[SeriesRefResponse] = Field(default_factory=list)
    numRatings: int | None = Field(
        default=None,
        description=(
            "How many ratings Audible reported for this product. Which fetch "
            "that count comes from depends on how the response was served: a "
            "live fetch carries the count in that response, a cache hit "
            "carries the one in the response that was stored, and the stored "
            "row -- what the /db/* routes and the database fallbacks serve -- "
            "holds the count from the most recent fetch that actually "
            "reported one. A response omitting the rating group leaves the "
            "stored count standing rather than clearing it, so a row's count "
            "can be older than the fields beside it; a bound 0 is a value and "
            "would overwrite a stored thirty thousand with nothing. Null is "
            "not Audible reporting zero -- a reported zero is 0. On a row it "
            "means no fetch has ever supplied a count; on a live fetch or a "
            "cache hit it means only that this one response did not carry one."
        ),
    )
    numReviews: int | None = Field(
        default=None,
        description=(
            "How many written reviews Audible reported for this product. "
            "Served and merged exactly as numRatings is, over the same three "
            "paths: a live fetch carries that fetch's count, a cache hit the "
            "stored response's, and the stored row the most recent fetch that "
            "actually reported one, since a response omitting the rating "
            "group leaves the stored count standing. Null is not a reported "
            "zero; it means no count came down the path this response was "
            "served on, and on a row that no fetch has ever supplied one."
        ),
    )
    publicationName: str | None = Field(
        default=None,
        description=(
            "The publication a periodical or podcast episode belongs to. "
            "Null for a product Audible does not place in one, which is most "
            "of the catalog."
        ),
    )
    publicationDatetime: str | None = Field(
        default=None,
        description=(
            "Publication instant, UTC ISO 8601 with a literal trailing Z, "
            "exactly as Audible spells it. Distinct from releaseDate, which "
            "is a bare calendar date."
        ),
    )
    extendedProductDescription: str | None = Field(
        default=None,
        description=(
            "Audible's long-form description, carried through with its "
            "markup intact rather than flattened into plain text the way "
            "description and summary are -- paragraph structure is most of "
            "what this field is for, and removing the tags destroys it. "
            "It is upstream HTML that Libex neither validates nor rewrites: "
            "treat it as untrusted input and encode it on output."
        ),
    )
    productState: str | None = Field(
        default=None,
        description=(
            "Audible's own state string for the product, passed through as "
            "sent. The vocabulary is Audible's and can grow without notice, "
            "so match it as an opaque string rather than an enum."
        ),
    )
    audibleExtras: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Every top-level key of Audible's responses for this product that "
            "the fields above do not already reproduce, so a key Audible "
            "invents next month reaches the caller instead of disappearing "
            "between the fetch and the response. Nothing in it is ever "
            "hoisted to the top level -- no splat, no key added at runtime -- "
            "so an upstream key named asin cannot collide with the "
            "first-class field of that name; it stays nested and is read "
            "here. "
            "What arrives in it depends on how the book was served -- per "
            "book, not per response, since one bulk answer can carry books "
            "from all three of the following at once -- and there are three "
            "answers rather than one. A live fetch serves that fetch's blob "
            "alone, merged with nothing. A cache hit serves a past fetch's "
            "blob, whatever shape it had when it was stored: "
            "the key is book:<region>:<asin>, it carries nothing describing "
            "the blob's shape, and an entry lives 24 hours by default. Only a "
            "response read out of the stored row serves the accumulated "
            "union. The /db/* routes always do, and so does a book the "
            "database covers after Audible fails -- the per-ASIN backstop "
            "when a chunk fails transiently, and the whole-request fallback "
            "when Audible cannot be reached at all. That fallback is all or "
            "nothing rather than a gap-fill: it reads the row for everything "
            "it owes an answer for, and only when the row holds nothing at "
            "all does it fall back to cache entries, meaning past fetches' "
            "blobs again. A book the row misses is left out of the response "
            "rather than filled in from cache. In ordinary operation the live "
            "/book routes answer from the first two paths, not the third. "
            "Responses is plural for the union's sake, and the union is what "
            "the stored row holds: incoming keys are merged into what is "
            "already there, so a key Audible stops sending is kept, carried "
            "forward from the last response that had it. It spans "
            "marketplaces as well as time, because a book is stored by ASIN "
            "alone and the same ASIN can be sold in several of them -- a key "
            "seen only on de and jp, such as audible_editors_summary or "
            "voice_description, can therefore appear on a us response for a "
            "shared ASIN. Which caution that earns depends on the path once "
            "more. Accumulation is the row's alone: only there is the blob "
            "more than one response, and only there is it not what Audible "
            "said last. The staleness that follows covers the cache hit too, "
            "for its own reason -- on both, a key's presence in the blob is "
            "no evidence Audible still sends that key, the row because it "
            "keeps what it was sent once, a cache hit because the fetch it "
            "was stored from can be a day old. On a live fetch neither "
            "caution applies: every key in it came from that one response, "
            "so the blob is current by construction and a key's presence is "
            "evidence Audible sent that key in that fetch. "
            "Unvalidated upstream data: values are passed through without "
            "sanitisation and include URLs and markup. Treat every value as "
            "untrusted input and encode it on output. Libex does not fetch "
            "anything it contains, and neither should a consumer treat a URL "
            "in it as vetted. "
            "Verbatim means every key, value for value -- not byte for byte. "
            "The content is parsed JSON rather than the original bytes, so "
            "key order is not preserved, duplicate keys are already collapsed "
            "to one, and numeric spelling is normalized (1e3 arrives as "
            "1000.0). That happens when the response is parsed, upstream of "
            "anything Libex stores. "
            "In the row it accumulates the way authors, narrators, genres and "
            "series do, and the difference worth knowing is how far down. "
            "Those four accumulate an entry at a time and a relationship is "
            "never dropped, so they only ever grow. This blob accumulates a "
            "top-level key at a time: the key survives, but whatever is "
            "nested under it is replaced wholesale by the most recent "
            "response that carried that key, so a value one level down can "
            "shrink where a relationship cannot. A raw array in here is "
            "therefore one response's version of it, which is why the "
            "first-class fields tend over time to become supersets of their "
            "raw counterparts. "
            "Tri-state: null means no extras reached the caller on the path "
            "this response was served on, an empty object means Audible sent "
            "nothing beyond the fields above, and a populated object is "
            "content. What a null rules out differs by path. From the row it "
            "means no response has ever written extras for this book, because "
            "a fetch whose extras were dropped whole for being too large, too "
            "deeply nested or unserializable does not clear the column -- the "
            "stored blob stands and extrasWithheld records the drop. From a "
            "live fetch or a cache hit it means only that this one response "
            "carried none, with extrasWithheld saying why when a drop is the "
            "reason, however rich the stored row may be. "
            "So forcing a fetch is the wrong reflex for a null. cache=false "
            "skips the cache and reaches Audible, and what comes back is that "
            "single fetch's blob: thinner than the cached one it replaced "
            "wherever the response is thinner, null where the fetch's extras "
            "were dropped whole, and written over the cache entry either way. "
            "That last clause is the sharp end, and it is mostly not the "
            "forcing caller who pays it: a fetch whose extras were dropped "
            "whole writes null into the shared cache entry, so every caller "
            "asking for that book reads null for as long as the entry lives, "
            "24 hours by default, while the stored row holds its good blob "
            "throughout. The null is deliberate rather than a defect -- it is "
            "what makes the writer's merge leave a stored blob alone instead "
            "of replacing it with an empty one -- so the decision that "
            "protects the row is the same decision that poisons the cache. "
            "The place to read the accumulated union is the /db/* family."
        ),
    )
    extrasWithheld: dict[str, Any] | None = Field(
        default=None,
        description=(
            "What was left out of audibleExtras and why, so a caller is told "
            "an omission happened rather than left to infer it from an "
            "absence. Keyed by what was withheld, valued by the reason or by "
            "counts of the affected entries. "
            "It has the same three sources audibleExtras does, and says "
            "something different on each. A live fetch reports what that one "
            "fetch withheld, and reads null when it withheld nothing, since "
            "the key is omitted rather than sent empty. A cache hit reports "
            "what the stored fetch withheld, null on the same terms. Only the "
            "stored row carries the accumulated record: what the /db/* routes "
            "serve, and what the per-ASIN backstop and the outage fallback "
            "serve when they read the row -- that fallback's cache leg being "
            "a past fetch's record again. "
            "In the row it accumulates alongside audibleExtras and over the "
            "same span, which is what lets the two be read together: the "
            "record of what has been kept out of that blob across every "
            "response that has written this book, not of what one fetch left "
            "out. What they union over is the difference. The blob unions "
            "Audible's keys; this unions Libex's own findings, one entry per "
            "kind of withholding, so each kind carries what the most recent "
            "response to hit that kind left behind -- unless that response's "
            "account was already contained in the stored one, which is left "
            "standing rather than rewritten, so a fuller entry survives a "
            "thinner later one. That exception only ever leaves more here "
            "than the rule before it describes, so read it as precision "
            "rather than a hazard. A response that strips a stray character "
            "does not erase an earlier episode count. A response that "
            "withholds nothing leaves the whole record standing "
            "rather than clearing it -- the key is omitted when there is "
            "nothing to say, so a write reporting all-clear cannot be told "
            "from one that never looked. Neither side is a snapshot of now: "
            "this can name something a later response supplied and the blob "
            "now holds. Read it as evidence that something was dropped at "
            "some point, not as an inventory of what is missing from the blob "
            "as it stands. "
            "The same span is not the same event, and on the row that is the "
            "part to act on. The two columns merge independently, each taking "
            "its own arm of the merge, so they move apart in both directions. "
            "A fetch whose blob was dropped whole writes its reason here while "
            "leaving the stored blob untouched -- so this names a drop that "
            "the blob printed beside it does not show, and on a book with "
            "nothing stored yet audibleExtras stays null while this is "
            "populated. A fetch that withholds nothing adds its keys to the "
            "blob and leaves this record exactly as it was. Do not difference "
            "the two to work out what is missing, and do not read an entry "
            "here as a claim about the blob served with it. On a live fetch or "
            "a cache hit they do correspond, both being one response's "
            "account of itself: audibleExtras null with a reason filed here "
            "under audibleExtras is that response's blob having been dropped "
            "whole. "
            "Null means nothing was recorded as withheld on the path this "
            "response was served on. From a live fetch or a cache hit, that "
            "one response withheld nothing. From the row, that nothing has "
            "ever been recorded: either every response for this book came "
            "through complete, or none has been captured at all, in which "
            "case audibleExtras is null too. Not tri-state, unlike "
            "audibleExtras -- there is no distinction here between nothing "
            "withheld and nothing known. "
            "It sits alongside audibleExtras rather than inside it because "
            "the blob is documented as Audible's keys only; a Libex-invented "
            "key in there would collide with a real upstream one the day "
            "Audible ships a field by that name."
        ),
    )


# ============================================================
# BULK BOOK RESPONSE
# ============================================================

class BulkBookResponse(BaseModel):
    books: list[BookResponse]
    notFound: list[str] = Field(
        default_factory=list,
        description=(
            "Requested ASINs Audible could not resolve, computed before "
            "filtering is applied. A book that was found and then removed "
            "by a filter is not reported here -- it is simply absent from "
            "both books and notFound."
        ),
    )


# ============================================================
# CHAPTERS RESPONSE
# ============================================================

class ChapterItem(BaseModel):
    lengthMs: int = 0
    startOffsetMs: int = 0
    startOffsetSec: int = 0
    title: str = ""


class ChapterResponse(BaseModel):
    brandIntroDurationMs: int = 0
    brandOutroDurationMs: int = 0
    isAccurate: bool = False
    runtimeLengthMs: int = 0
    runtimeLengthSec: int = 0
    chapters: list[ChapterItem] = Field(default_factory=list)


# ============================================================
# SERIES RESPONSE
# ============================================================

class SeriesResponse(BaseModel):
    asin: str
    name: str | None = None
    description: str | None = None
    region: str
    position: str | None = None
    updatedAt: str | None = None
