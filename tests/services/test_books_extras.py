"""
audibleExtras and extrasWithheld: what reaches the caller and what is
recorded when something cannot.

The blob exists so that a key Audible invents next month surfaces on its
own rather than disappearing between the fetch and the response, with
nobody in a position to know it ever arrived. Everything here is a check on
that one promise from a different side:

  the split         every top-level key Audible sent is either reproduced as
                    a first-class field or present in the blob, and never
                    both by accident.
  the sanitizer     three values Python's json round-trips happily and
                    Postgres jsonb refuses. Any one of them in a product
                    fails that book's write permanently, so what gets
                    removed is counted and reported rather than quietly
                    dropped.
  the strip         a podcast show's episode list is the one thing removed
                    for size alone, and the count of what went is part of
                    the response.
  the caps          past either cap the blob is dropped WHOLE, not pruned.
                    A pruned blob looks complete and is not, which is the
                    exact failure the blob was built to end.

Every assertion here reads the values, not just the key. "audibleExtras is
populated" is satisfied by a blob holding the wrong half of the product;
what is asserted is which keys it holds and what they contain.
"""

# Standard library
import importlib.util
import json
import pathlib

# Third party
import pytest

# Local
from app.services.audible import books
from app.services.audible.books import (
    _EXTRAS_MAX_BYTES,
    _EXTRAS_MAX_DEPTH,
    _REPRODUCED_KEYS,
    _WITHHELD_DEPTH,
    _WITHHELD_SANITIZED,
    _WITHHELD_SIZE,
    _normalize_product,
    _reproduce,
)
from libex_core.audible.client import VALID_REGIONS


# Written out rather than read from REGION_MAP, so a region disappearing
# from the map shrinks this list's coverage visibly instead of silently.
# test_every_region_is_covered_here holds the two together.
_ALL_REGIONS = ["us", "uk", "ca", "au", "de", "fr", "it", "es", "jp", "in", "br"]

# A product shaped like Audible's, carrying one of each category: keys that
# are reproduced as first-class fields, keys that are transformed and pass
# through as well, and keys nothing reads at all.
_PRODUCT = {
    "asin": "B0EXTRAS001",
    "title": "An Extras Title",
    "publisher_name": "A Publisher",
    "content_type": "Product",
    "rating": {"overall_distribution": {"average_rating": 4.5, "num_ratings": 12}, "num_reviews": 3},
    "product_images": {"500": "https://example.com/i._SX500_.jpg"},
    "plans": [{"plan_name": "US Minerva"}],
    "category_ladders": [{"root": "Genres", "ladder": [{"id": "1", "name": "Fantasy"}]}],
    "relationships": [{"asin": "B0SERIES01", "relationship_type": "series", "sort": "1"}],
    "release_date": "2020-01-01",
    "episode_number": 4,
    "episode_type": "full",
    "publication_datetime": "2020-01-01T00:00:00Z",
    "authors": [{"asin": "B0AUTHOR01", "name": "An Author"}],
    "narrators": [{"name": "A Narrator"}],
    "social_media_images": {"facebook": "https://example.com/fb.jpg"},
    "platinum_keywords": ["fantasy", "epic"],
    "is_world_rights": True,
}


def _extras(product, region="us"):
    return _normalize_product(product, region)["audibleExtras"]


def _withheld(product, region="us"):
    return _normalize_product(product, region).get("extrasWithheld", {})


# ============================================================
# THE SPLIT: REPRODUCED OR PASSED THROUGH, NEVER LOST
# ============================================================

def test_no_reproduced_key_is_also_in_the_blob():
    """The two sides of the split are disjoint on a real product shape.

    A key on both sides is the same value shipped twice under two names,
    which costs response size on every book and invites the two copies to
    disagree the day one of them starts being transformed.
    """
    duplicated = sorted(_REPRODUCED_KEYS & set(_extras(_PRODUCT)))

    assert duplicated == [], (
        f"{duplicated} are reproduced as first-class fields AND carried in "
        "audibleExtras."
    )


def test_every_upstream_key_is_reproduced_or_carried():
    """Nothing Audible sent leaves without appearing somewhere.

    Stated over the whole product rather than over a list of fields,
    because the loss this guards against is by definition a key nobody
    thought to list.
    """
    book = _normalize_product(_PRODUCT, "us")
    carried = set(book["audibleExtras"])

    lost = sorted(set(_PRODUCT) - _REPRODUCED_KEYS - carried)
    assert lost == [], (
        f"{lost} arrived from Audible and appear neither as a first-class "
        "field nor in audibleExtras."
    )


# The keys _normalize_product transforms or only partly consumes. Each is
# read with a plain product.get rather than through _reproduce, so the
# parent object still goes into the blob whole -- imageUrl is one URL out
# of a dict of sizes, genres flattens category_ladders, series reads only
# the series entries out of relationships, and so on. The field is the
# convenient answer; the blob is the complete one.
_TRANSFORMED_BUT_CARRIED = [
    "rating", "product_images", "plans", "category_ladders", "relationships",
    "release_date", "episode_number", "episode_type", "publication_datetime",
    "authors", "narrators",
]


@pytest.mark.parametrize("key", _TRANSFORMED_BUT_CARRIED)
def test_a_transformed_key_is_still_carried_whole(key):
    """Present AND byte-identical to what arrived, not merely present.

    "the key is in the blob" is satisfied by a blob that carries a
    normalized, lossy copy -- the flattened genre list under the name
    category_ladders would pass it while destroying the ladder structure
    that made carrying it worth anything.
    """
    extras = _extras(_PRODUCT)

    assert key in extras, f"{key} is transformed into a field and then lost."
    assert extras[key] == _PRODUCT[key], (
        f"{key} is carried but not verbatim: the blob holds {extras[key]!r} "
        f"where Audible sent {_PRODUCT[key]!r}."
    )


def test_an_upstream_key_cannot_collide_with_a_derived_field():
    """link is Libex's own, built from the ASIN and region.

    An upstream key of the same name stays nested in the blob rather than
    being hoisted over it. Nothing splats the blob into the response, and
    this is the assertion that keeps it that way.
    """
    book = _normalize_product({**_PRODUCT, "link": "https://elsewhere.invalid/"}, "us")

    assert book["link"] == "https://audible.com/pd/B0EXTRAS001"
    assert book["audibleExtras"]["link"] == "https://elsewhere.invalid/"


def test_reproduce_refuses_a_key_that_is_not_declared():
    """The mechanism that makes _REPRODUCED_KEYS a requirement rather than
    a convention.

    Without this, adding a first-class field and forgetting its upstream
    key leaves the value in the response twice -- as the field and in the
    blob -- and nothing anywhere says so.
    """
    with pytest.raises(RuntimeError, match="missing from _REPRODUCED_KEYS"):
        _reproduce(_PRODUCT, "platinum_keywords")


# The anchor the copy below is edited at: the opening of the set literal,
# so an extra member can be added without parsing Python. Matched exactly
# rather than by regex, so a reformatting of that declaration fails this
# test loudly instead of silently editing nothing and asserting against a
# pristine module.
_REPRODUCED_KEYS_DECLARATION = "_REPRODUCED_KEYS: frozenset[str] = frozenset({"

_A_KEY_NOTHING_READS = "a_key_no_first_class_field_reads"


def test_a_declared_key_that_no_field_reads_stops_the_module_importing(tmp_path):
    """The reverse direction, which fails silently rather than loudly and
    so is the one worth the machinery.

    A key named in _REPRODUCED_KEYS is withheld from the blob because it is
    named there. If no first-class field actually reads it, it is also
    reproduced nowhere, and the value disappears between the fetch and the
    response with nothing raising -- the exact silent loss the blob exists
    to end, happening inside the mechanism built to prevent it. Renaming a
    field and leaving its old key in the set is one line away at any time.

    The check runs at import, so this has to import a module rather than
    call a function: a copy of the real source with one unread key added,
    loaded from a path of its own. Nothing is patched, because what is
    being asserted is that the raise happens during import itself.

    The copy is loaded under a name of its own and never put in
    sys.modules, so the real module every other test in the suite holds is
    untouched by it.
    """
    source = pathlib.Path(books.__file__).read_text(encoding="utf-8")
    assert source.count(_REPRODUCED_KEYS_DECLARATION) == 1, (
        "The declaration this test edits is no longer written the way it "
        f"expects: {_REPRODUCED_KEYS_DECLARATION!r}."
    )
    broken = source.replace(
        _REPRODUCED_KEYS_DECLARATION,
        f'{_REPRODUCED_KEYS_DECLARATION}\n    "{_A_KEY_NOTHING_READS}",',
    )

    copied = tmp_path / "books_with_an_unread_key.py"
    copied.write_text(broken, encoding="utf-8")

    spec = importlib.util.spec_from_file_location(copied.stem, copied)
    module = importlib.util.module_from_spec(spec)

    with pytest.raises(RuntimeError, match=_A_KEY_NOTHING_READS):
        spec.loader.exec_module(module)


# ============================================================
# extrasWithheld IS ABSENT WHEN NOTHING WAS WITHHELD
# ============================================================

def test_a_clean_product_carries_no_withheld_record():
    """An empty record would read as "examined and found clean", asserted
    on every book in the corpus. Absent says nothing was withheld, which is
    the true statement."""
    book = _normalize_product(_PRODUCT, "us")

    assert "extrasWithheld" not in book
    assert book["audibleExtras"] != {}, (
        "The blob came back empty for a product carrying keys nothing "
        "reproduces, so nothing above is actually being exercised."
    )


# ============================================================
# THE SANITIZER: WHAT jsonb REFUSES AND json ACCEPTS
# ============================================================
#
# Three classes of value, all of which Python's json parses out of an
# Audible response and re-emits without complaint, and all of which
# Postgres rejects on the way into a jsonb column. Untreated, one of them
# anywhere in one product fails that book's write for good -- and because
# the same dict is also cached into a jsonb column, the two surfaces would
# then disagree about the same book.
#
# Written as real values, never as the strings "NaN" or "\\u0000". A test
# carrying the escape sequence rather than the byte checks that a
# seven-character string survives, which every implementation does, and
# says nothing about the one byte that breaks the write.

def test_a_nul_character_in_a_value_is_stripped_and_counted():
    """U+0000 is a legal JSON string character and an illegal jsonb one.

    Stripped rather than dropping the key or the book: losing an entire
    title over one invisible byte is the worse outcome. Counted because a
    sanitization nothing records is the silent drop this mechanism exists
    to end.
    """
    book = _normalize_product({**_PRODUCT, "odd_field": "before\x00after"}, "us")

    assert book["audibleExtras"]["odd_field"] == "beforeafter"
    assert book["extrasWithheld"][_WITHHELD_SANITIZED]["nulCharacters"] == 1


def test_a_nul_character_in_a_key_is_stripped_and_counted():
    """The same byte on the other side of the colon. A sanitizer that walks
    values only leaves the key it is nested under carrying the byte, and
    the write fails exactly as before."""
    book = _normalize_product({**_PRODUCT, "odd\x00field": "plain"}, "us")

    extras = book["audibleExtras"]
    assert "oddfield" in extras
    assert extras["oddfield"] == "plain"
    assert book["extrasWithheld"][_WITHHELD_SANITIZED]["nulCharacters"] == 1


@pytest.mark.parametrize(
    "label, value",
    [
        ("NaN", float("nan")),
        ("Infinity", float("inf")),
        ("-Infinity", float("-inf")),
        # 1e999 overflows to inf on the way in rather than raising, which is
        # how a perfectly ordinary-looking number in an Audible response
        # becomes a token no JSON parser accepts.
        ("1e999", 1e999),
    ],
)
def test_a_non_finite_number_becomes_null_and_is_counted(label, value):
    """json.dumps writes these back as the bare tokens NaN and Infinity,
    which are not valid JSON at all -- so the failure is not only Postgres
    refusing the value, it is Libex emitting something no caller can
    parse."""
    book = _normalize_product({**_PRODUCT, "odd_number": value}, "us")

    assert book["audibleExtras"]["odd_number"] is None, label
    assert book["extrasWithheld"][_WITHHELD_SANITIZED]["nonFiniteNumbers"] == 1


def test_an_int_too_wide_to_render_becomes_null_and_is_counted():
    """Python refuses to str() an int past sys.get_int_max_str_digits, so
    this one breaks inside json.dumps rather than inside Postgres -- and it
    breaks on the way to the cache too, not only on the way to the row."""
    book = _normalize_product({**_PRODUCT, "odd_number": 10 ** 5000}, "us")

    assert book["audibleExtras"]["odd_number"] is None
    assert book["extrasWithheld"][_WITHHELD_SANITIZED]["oversizedNumbers"] == 1


def test_the_sanitizer_reaches_values_nested_inside_the_blob():
    """A top-level-only sweep passes every test above and still ships the
    byte, because Audible's product is nested three and four deep
    everywhere that matters."""
    nested = {"level1": {"level2": [{"level3": "before\x00after"}]}}
    book = _normalize_product({**_PRODUCT, "odd_field": nested}, "us")

    assert book["audibleExtras"]["odd_field"]["level1"]["level2"][0]["level3"] == "beforeafter"
    assert book["extrasWithheld"][_WITHHELD_SANITIZED]["nulCharacters"] == 1


def test_a_sanitized_blob_encodes_as_valid_json_with_no_nul_left():
    """The end-to-end claim the four tests above are components of.

    allow_nan=False is what json.dumps needs to refuse the tokens it would
    otherwise happily emit, so this is the whole contract in one line: the
    blob that leaves the normalizer is something a strict parser and a
    jsonb column will both take.
    """
    product = {
        **_PRODUCT,
        "nul_value": "before\x00after",
        "nan_value": float("nan"),
        "inf_value": 1e999,
        "wide_int": 10 ** 5000,
    }

    encoded = json.dumps(_extras(product), allow_nan=False)

    assert "\x00" not in encoded
    assert json.loads(encoded)["nan_value"] is None


def test_every_sanitized_class_is_counted_separately():
    """One combined total would report "something was cleaned" and leave an
    operator unable to tell an encoding oddity from a number Audible sent
    that nothing can store."""
    product = {
        **_PRODUCT,
        "nul_value": "a\x00b\x00c",
        "nan_value": float("nan"),
        "wide_int": 10 ** 5000,
    }

    assert _withheld(product)[_WITHHELD_SANITIZED] == {
        "nulCharacters": 2,
        "nonFiniteNumbers": 1,
        "oversizedNumbers": 1,
    }


# ============================================================
# THE PODCAST STRIP: PER-TYPE COUNTS, NOT A TOTAL
# ============================================================

def test_the_podcast_strip_records_a_count_per_relationship_type():
    """Two types, deliberately different counts, so a bare total cannot
    satisfy this.

    A podcast show sends one child entry per episode -- measured in the
    thousands, hundreds of kilobytes of a product whose every other key
    together is a few -- so this is the one thing removed for size alone.
    The counts are what keeps that from being the silent drop the blob
    exists to prevent: the caller is told an episode list was there and how
    long it was.
    """
    relationships = (
        [{"asin": f"B0EP{n:06d}", "relationship_type": "episode"} for n in range(7)]
        + [{"asin": "B0SEASON01", "relationship_type": "season"}] * 2
        + [{"asin": "B0SERIES01", "relationship_type": "series", "sort": "1"}]
    )
    product = {**_PRODUCT, "content_type": "Podcast", "relationships": relationships}

    book = _normalize_product(product, "us")

    assert book["extrasWithheld"]["relationships"] == {"episode": 7, "season": 2}


def test_the_podcast_strip_keeps_every_other_relationship():
    """The strip is scoped to two named types. A series relationship
    removed alongside them would take the series out of the blob while the
    series field still shows it, leaving the blob no longer a complete
    record of what arrived."""
    relationships = [
        {"asin": "B0EPISODE1", "relationship_type": "episode"},
        {"asin": "B0SERIES01", "relationship_type": "series", "sort": "1"},
        {"asin": "B0COMPON01", "relationship_type": "component", "sort": "2"},
    ]
    product = {**_PRODUCT, "relationships": relationships}

    kept = _extras(product)["relationships"]

    assert kept == [relationships[1], relationships[2]]


def test_a_product_with_no_stripped_relationships_records_nothing():
    """No entry at all rather than a zero count, so an operator reading
    extrasWithheld sees only books where something actually happened."""
    book = _normalize_product(_PRODUCT, "us")

    assert "relationships" not in book.get("extrasWithheld", {})


# ============================================================
# THE CAPS: THE WHOLE BLOB GOES, OR NONE OF IT DOES
# ============================================================

def test_an_oversized_blob_is_dropped_whole_rather_than_pruned():
    """Pruning would hand back something that looks complete and is not.

    That is worse than dropping it: a caller can see an absent blob and a
    record saying why, and cannot see a key quietly removed from a blob
    that still arrived. None rather than {} for the same reason plans is
    tri-state -- {} would assert Audible sent nothing extra.
    """
    product = {**_PRODUCT, "huge_field": "x" * (_EXTRAS_MAX_BYTES + 1024)}

    book = _normalize_product(product, "us")

    assert book["audibleExtras"] is None
    assert book["extrasWithheld"]["audibleExtras"] == _WITHHELD_SIZE


def test_a_blob_just_under_the_size_cap_survives_intact():
    """The complement, so the test above cannot pass by the cap firing on
    everything. Without this, a cap accidentally set to zero reads as
    working."""
    product = {**_PRODUCT, "large_field": "x" * (_EXTRAS_MAX_BYTES // 2)}

    book = _normalize_product(product, "us")

    assert book["audibleExtras"]["large_field"] == "x" * (_EXTRAS_MAX_BYTES // 2)
    assert "audibleExtras" not in book.get("extrasWithheld", {})


def test_a_blob_nested_past_the_depth_cap_is_dropped_whole():
    """Recorded as depth rather than size, because the two are different
    problems with different causes and an operator reading the record needs
    to know which one fired."""
    deep = current = {}
    for _ in range(_EXTRAS_MAX_DEPTH + 8):
        current["nested"] = {}
        current = current["nested"]
    product = {**_PRODUCT, "deep_field": deep}

    book = _normalize_product(product, "us")

    assert book["audibleExtras"] is None
    assert book["extrasWithheld"]["audibleExtras"] == _WITHHELD_DEPTH


def test_a_blob_within_the_depth_cap_survives_intact():
    """The complement again. A depth walk that counts the root twice, or
    counts a list level and a dict level separately, fires several levels
    early and would drop ordinary products."""
    shallow = current = {}
    for _ in range(4):
        current["nested"] = {}
        current = current["nested"]
    product = {**_PRODUCT, "shallow_field": shallow}

    book = _normalize_product(product, "us")

    assert book["audibleExtras"]["shallow_field"] == shallow
    assert "audibleExtras" not in book.get("extrasWithheld", {})


def test_a_dropped_blob_still_reports_what_the_sanitizer_found():
    """The two records are independent. A product that is both dirty and
    oversized must report both, or the operator fixing the size problem
    never learns the values were there too."""
    product = {
        **_PRODUCT,
        "nul_value": "a\x00b",
        "huge_field": "x" * (_EXTRAS_MAX_BYTES + 1024),
    }

    withheld = _withheld(product)

    assert withheld["audibleExtras"] == _WITHHELD_SIZE
    assert withheld[_WITHHELD_SANITIZED] == {"nulCharacters": 1}


# ============================================================
# EVERY REGION, NOT JUST us
# ============================================================

def test_every_region_is_covered_here():
    """_ALL_REGIONS and the client's own set say the same thing.

    Parametrizing straight off VALID_REGIONS would make a region deleted
    from the map shrink this file's coverage with nothing failing, which is
    the shape of regression that passes US-only testing and breaks
    everyone else.
    """
    assert set(_ALL_REGIONS) == VALID_REGIONS
    assert len(_ALL_REGIONS) == len(set(_ALL_REGIONS))


@pytest.mark.parametrize("region", _ALL_REGIONS)
def test_the_blob_is_assembled_identically_in_every_region(region):
    """The blob is verbatim upstream data, so it cannot vary by region --
    only the fields derived around it can. A region-dependent blob would
    mean something in the assembly is reading a regional setting it has no
    business reading."""
    book = _normalize_product(_PRODUCT, region)

    assert book["audibleExtras"] == _extras(_PRODUCT, "us")
    assert book["region"] == region


@pytest.mark.parametrize("region", _ALL_REGIONS)
def test_the_podcast_strip_runs_in_every_region(region):
    """The strip is unconditional, so it must fire on a jp or br product
    exactly as on a us one. A strip gated on anything regional would leave
    ten marketplaces shipping 448 KB podcast products."""
    relationships = (
        [{"asin": f"B0EP{n:06d}", "relationship_type": "episode"} for n in range(3)]
        + [{"asin": "B0SEASON01", "relationship_type": "season"}]
        + [{"asin": "B0SERIES01", "relationship_type": "series", "sort": "1"}]
    )
    product = {**_PRODUCT, "content_type": "Podcast", "relationships": relationships}

    book = _normalize_product(product, region)

    assert book["extrasWithheld"]["relationships"] == {"episode": 3, "season": 1}
    assert book["audibleExtras"]["relationships"] == [relationships[4]]
