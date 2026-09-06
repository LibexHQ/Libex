"""
Integration tests for the blank-answer merge on the book row's text columns,
against real Postgres.

The rule is the same one test_db_longer_wins.py defends from the other end:
Libex never accepts less than it already holds. That file covers the two
columns merged by length. These cover the columns merged by presence, where
the loss is easier to miss because the merge LOOKS right -- coalesce is the
canonical "keep what you have" idiom, and it is, but only against NULL.
Audible does not always say nothing by sending nothing: a response group that
carries a field with no content sends an empty string, coalesce('', stored)
is '', and a book that had a publisher quietly stops having one on its next
ordinary refresh.

Nothing about that is visible to a mocked session, which can show that an
UPDATE was issued but not what Postgres resolved it to -- the same blind spot
that let the length merge's NULL trap survive a green suite. So the whole
truth table runs here, through the public writer, in the sequence production
writes it: one write to establish what is stored, a second to try to change
it.

Every assertion distinguishes '' from NULL explicitly. A check that only asked
whether the column was falsy would pass just as happily on the bug as on the
fix, because '' and NULL are both falsy in Python and the entire question is
which one is in the column.
"""

# Third party
import pytest
from sqlalchemy import text

# Local
from app.services.db.writer import upsert_author_profile, upsert_book

ASIN = "B0BLANKANS"
TITLE = "A Book With A Title"

# (payload key, column, stored value, replacement value)
#
# The values stay short on purpose: isbn is String(16) and four more of these
# columns are String(20) or String(50), so a generic filler long enough to
# read nicely would fail on length rather than on the merge.
FIELDS = [
    pytest.param("subtitle", "subtitle", "Stored Sub", "Fresh Sub", id="subtitle"),
    pytest.param("publisher", "publisher", "Stored Pub", "Fresh Pub", id="publisher"),
    pytest.param("copyright", "copyright", "(c) Stored", "(c) Fresh", id="copyright"),
    pytest.param("isbn", "isbn", "9780000000001", "9780000000002", id="isbn"),
    pytest.param("language", "language", "english", "german", id="language"),
    pytest.param("sku", "sku", "SKU-STORED", "SKU-FRESH", id="sku"),
    pytest.param("skuGroup", "sku_group", "GRP-STORED", "GRP-FRESH", id="sku_group"),
    pytest.param("imageUrl", "image", "https://i/a.jpg", "https://i/b.jpg", id="image"),
    pytest.param("bookFormat", "book_format", "unabridged", "abridged", id="book_format"),
    pytest.param("contentType", "content_type", "Product", "Podcast", id="content_type"),
    pytest.param(
        "contentDeliveryType", "content_delivery_type",
        "SinglePartBook", "MultiPartBook", id="content_delivery_type",
    ),
    pytest.param("episodeType", "episode_type", "full", "trailer", id="episode_type"),
    pytest.param("episodeNumber", "episode_number", "3", "4", id="episode_number"),
]

# Everything Audible can send that means it did not answer. '' is the case
# this guard exists for; whitespace carries exactly as much as '' does and is
# treated the same; None is the case coalesce already handled and must keep
# handling.
#
# The list is spelled out one character at a time because a run of spaces is
# the single whitespace case that proves nothing. postgresql's btrim(x) with
# no second argument trims U+0020 alone, so a suite whose only whitespace was
# '   ' passed identically on a merge that read a lone tab as a real answer
# and on one that did not. Each entry below is a character the catalogue
# actually carries: tabs and CRLF survive copy-paste into a publisher field,
# U+00A0 comes off a web page, and U+3000 is the ordinary space of Japanese
# text, which the jp marketplace is full of. Written as escapes so none of
# them can be mistaken for the plain space at the top of the list.
BLANKS = ["", "   ", "\t", "\n", "\r", "\r\n", "\u00a0", "\u3000", None]


async def _write(session, **fields):
    """One production write of the book, with the fields under test bound."""
    await upsert_book(session, {"asin": ASIN, "title": TITLE, "region": "us", **fields})
    await session.commit()


async def _stored(session, column):
    result = await session.execute(
        text(f"SELECT {column} FROM books WHERE asin = :asin"), {"asin": ASIN}
    )
    return result.scalar_one()


# ============================================================
# STORED VALUE + INCOMING BLANK — THE COLUMN KEEPS WHAT IT HAS
# ============================================================

@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("key, column, stored, replacement", FIELDS)
@pytest.mark.parametrize("blank", BLANKS)
async def test_a_blank_answer_does_not_displace_a_stored_value(
    db_session, key, column, stored, replacement, blank
):
    """The case the guard was written for. Audible has no way to say that a
    book no longer has a publisher, so a blank in that field is Audible
    declining to answer and the stored answer stands."""
    await _write(db_session, **{key: stored})
    assert await _stored(db_session, column) == stored

    await _write(db_session, **{key: blank})

    assert await _stored(db_session, column) == stored


# ============================================================
# STORED VALUE + INCOMING VALUE — AUDIBLE STILL WINS
# ============================================================

@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("key, column, stored, replacement", FIELDS)
async def test_a_real_answer_replaces_a_stored_value(
    db_session, key, column, stored, replacement
):
    """The half of the rule that is easy to break while fixing the other half.
    Audible is the source of truth in every case except shrinkage, so a
    present value must still overwrite -- a guard that kept the incumbent
    whatever arrived would freeze every book at its first write."""
    await _write(db_session, **{key: stored})
    await _write(db_session, **{key: replacement})

    assert await _stored(db_session, column) == replacement


# ============================================================
# STORED NULL + INCOMING BLANK — THE COLUMN STAYS NULL
# ============================================================

@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("key, column, stored, replacement", FIELDS)
@pytest.mark.parametrize("blank", BLANKS)
async def test_a_blank_answer_does_not_fill_a_null_column(
    db_session, key, column, stored, replacement, blank
):
    """A column Audible has never answered must stay distinguishable from one
    it answered blank. Writing the '' would make the two identical and lose
    the only record that the question is still open -- and `is None` is the
    assertion that says so, since '' would satisfy any falsy check."""
    await _write(db_session)
    assert await _stored(db_session, column) is None

    await _write(db_session, **{key: blank})

    assert await _stored(db_session, column) is None


# ============================================================
# STORED NULL + INCOMING VALUE — THE COLUMN FILLS
# ============================================================

@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("key, column, stored, replacement", FIELDS)
async def test_a_real_answer_fills_a_null_column(
    db_session, key, column, stored, replacement
):
    """The trap the length merge fell into once already: a guard written
    against NULL that also pins NULL, so a column first written empty can
    never be filled by any later response."""
    await _write(db_session)
    await _write(db_session, **{key: stored})

    assert await _stored(db_session, column) == stored


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("key, column, stored, replacement", FIELDS)
async def test_a_column_stays_fillable_after_a_run_of_blank_answers(
    db_session, key, column, stored, replacement
):
    """A book can sit through many refreshes whose response group comes back
    thin before one finally carries the field. Every one of those writes has
    to leave the column fillable, not merely the first."""
    for blank in BLANKS * 2:
        await _write(db_session, **{key: blank})

    await _write(db_session, **{key: stored})

    assert await _stored(db_session, column) == stored


# ============================================================
# TITLE — NOT NULL, SO ITS TABLE IS ITS OWN
# ============================================================
# title never holds NULL: the insert coalesces it to '' because the column
# forbids one. Its two blank rows are therefore stored-'' rather than
# stored-NULL, and the incoming side is bound directly rather than through
# excluded, which makes it a separate call site as well as a separate table.

@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("blank", BLANKS)
async def test_a_blank_answer_does_not_blank_a_stored_title(db_session, blank):
    """The worst loss on the row, and the one nothing downstream can recover:
    every response Libex serves for this book, and every title search that
    would have found it, goes with the title."""
    await _write(db_session)
    assert await _stored(db_session, "title") == TITLE

    await upsert_book(db_session, {"asin": ASIN, "title": blank, "region": "us"})
    await db_session.commit()

    assert await _stored(db_session, "title") == TITLE


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_real_title_replaces_a_stored_one(db_session):
    """A retitled edition is a real Audible answer and must land."""
    await _write(db_session)
    await upsert_book(db_session, {"asin": ASIN, "title": "Retitled", "region": "us"})
    await db_session.commit()

    assert await _stored(db_session, "title") == "Retitled"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_title_first_written_blank_can_still_be_filled(db_session):
    """The insert's '' fallback is not a floor the row can never rise off.
    Stored '' is what a book written from a response with no usable title
    holds, and the next response that carries one has to be able to set it."""
    await upsert_book(db_session, {"asin": ASIN, "title": "", "region": "us"})
    await db_session.commit()
    assert await _stored(db_session, "title") == ""

    await _write(db_session)

    assert await _stored(db_session, "title") == TITLE


# ============================================================
# A REAL ANSWER IS STORED EXACTLY AS IT ARRIVED
# ============================================================

@pytest.mark.integration
@pytest.mark.asyncio
async def test_padding_around_a_real_answer_is_stored_verbatim(db_session):
    """The other half of measuring blankness after a trim: only the
    measurement is trimmed. Deciding what to keep is this module's job and
    cleaning what Audible sent is the fetch layer's, so a value that has real
    text in it lands byte for byte, padding included. A merge that tidied it
    on the way past would be a normalization rule hidden where nobody looks
    for one, and would leave the stored text quietly differing from what the
    normalizer produced."""
    padded = "\u3000 Stored Pub \t"

    await _write(db_session, publisher=padded)

    assert await _stored(db_session, "publisher") == padded


# ============================================================
# PLANS — THE CARVE-OUT, PINNED SO A CHANGE TO IT IS DELIBERATE
# ============================================================
# plans is the one column here left on the NULL-only merge, because an empty
# plans array is a real answer: it is how a book that has left the Plus
# catalogue reports itself. Guarding it would trade a silent shrink for a
# silent staleness. These record what the carve-out actually does, so that
# changing it has to be a decision rather than a side effect of someone
# extending the guard across the row.

@pytest.mark.integration
@pytest.mark.asyncio
async def test_an_empty_plans_list_replaces_a_stored_one(db_session):
    """An empty list is Audible asserting that the book is in no plans, and it
    lands. This is the behaviour the carve-out preserves."""
    await _write(db_session, plans=[{"plan": "US Minerva"}])
    await _write(db_session, plans=[])

    assert await _stored(db_session, "plans") == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_absent_plans_still_keep_a_stored_list(db_session):
    """Silence is not the same assertion and never was. A response carrying no
    plans key at all leaves the stored list alone, which is the half of the
    NULL merge that must survive the carve-out."""
    stored = [{"plan": "US Minerva"}]
    await _write(db_session, plans=stored)
    await _write(db_session)

    assert await _stored(db_session, "plans") == stored


# ============================================================
# THE AUTHOR PORTRAIT — THE SAME MERGE, A DIFFERENT TABLE
# ============================================================
# authors.image is the one column outside the book row where a blank arrives
# with nothing upstream to stop it. _normalize_author passes the contributors
# response's profile_image_url straight through, unfiltered and unstripped,
# so an empty image field on that endpoint reaches the merge as '' -- and the
# path it takes, upsert_author_profile, is the only one that refreshes an
# author's portrait once the row exists. A coalesce there blanks a stored
# portrait on an ordinary profile refresh.

AUTHOR_ASIN = "B0BLANKAUT"
AUTHOR_NAME = "An Author"
PORTRAIT = "https://images.example.com/author.jpg"


async def _write_author(session, **fields):
    """One production write of the author profile, as the contributors path
    makes it."""
    await upsert_author_profile(session, {
        "asin": AUTHOR_ASIN, "name": AUTHOR_NAME, "region": "us", **fields,
    })


async def _stored_author(session, column):
    result = await session.execute(
        text(f"SELECT {column} FROM authors WHERE asin = :asin"),
        {"asin": AUTHOR_ASIN},
    )
    return result.scalar_one()


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("blank", BLANKS)
async def test_a_blank_image_does_not_displace_a_stored_portrait(db_session, blank):
    """A portrait is superseded by another URL, never withdrawn to nothing, so
    a blank profile_image_url is the contributors endpoint answering thin and
    the stored portrait stands."""
    await _write_author(db_session, image=PORTRAIT)
    assert await _stored_author(db_session, "image") == PORTRAIT

    await _write_author(db_session, image=blank)

    assert await _stored_author(db_session, "image") == PORTRAIT


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_real_image_replaces_a_stored_portrait(db_session):
    """The other half of the rule. Audible is still the source of truth for
    everything but shrinkage, so a new URL has to land."""
    replacement = "https://images.example.com/author-v2.jpg"
    await _write_author(db_session, image=PORTRAIT)

    await _write_author(db_session, image=replacement)

    assert await _stored_author(db_session, "image") == replacement


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("blank", BLANKS)
async def test_a_blank_image_does_not_fill_a_null_portrait(db_session, blank):
    """An author whose portrait has never been answered stays distinguishable
    from one answered blank. `is None` rather than a falsy check, because ''
    would satisfy the falsy check on either behaviour."""
    await _write_author(db_session)
    assert await _stored_author(db_session, "image") is None

    await _write_author(db_session, image=blank)

    assert await _stored_author(db_session, "image") is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_portrait_stays_fillable_after_a_run_of_blank_images(db_session):
    """The trap the length merge fell into once already, in its other form: a
    guard written against blanks that also pins NULL would leave an author
    first seen without a portrait unable to ever gain one."""
    for blank in BLANKS * 2:
        await _write_author(db_session, image=blank)

    await _write_author(db_session, image=PORTRAIT)

    assert await _stored_author(db_session, "image") == PORTRAIT
