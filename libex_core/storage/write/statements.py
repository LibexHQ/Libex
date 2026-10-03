"""
The statements the batched writers execute, built once per dialect and
executed with bound rows.

Every value a statement writes arrives as a named bind parameter rather than
being compiled into it, so one book and fifty cost one compile between them.
The Postgres build is the hosted writer's statements to the character; the
SQLite build differs only where `libex_core.storage.dialect` says SQLite
spells something another way, and in the INSERT construct itself.
"""

# Standard library
from dataclasses import dataclass
from functools import lru_cache

# Third party
from sqlalchemy import bindparam, exists
from sqlalchemy.orm import aliased

# Local
from libex_core.storage import merge
from libex_core.storage.models import (
    Book,
    Genre,
    Narrator,
    Series,
    author_book,
    book_genre,
    book_narrator,
    book_series,
    series_author,
)
from libex_core.storage.write.support import insert_for


def _first_of_its_asin(model):
    """
    The value a new row's is_primary takes: true unless another region already
    holds the ASIN. It is evaluated when the row is inserted and the update
    below never sets it, so a row keeps the answer it was born with.
    """
    other = aliased(model)
    return ~exists().where(other.asin == bindparam("asin"), other.region != bindparam("region"))


@dataclass(frozen=True)
class Statements:
    """Every prebuilt statement for one dialect."""

    book_upsert: object
    series_upsert: object
    genre_insert: object
    narrator_insert: object
    book_genre_insert: object
    book_narrator_insert: object
    author_book_insert: object
    series_author_insert: object
    book_series_upsert: object


def _build_series_upsert(insert):
    """
    The one series upsert.

    Every value it writes arrives as a named bind parameter rather than being
    compiled into the statement, so one series and fifty cost one compile
    between them. That is the whole reason the statement is shaped this way:
    the postgresql Insert construct sets inherit_cache = False, so a statement
    carrying literals is recompiled on every execution no matter how many times
    the identical object has been executed before.
    """
    stmt = insert(Series).values(
        asin=bindparam("asin"),
        title=bindparam("title"),
        description=bindparam("description"),
        region=bindparam("region"),
        is_primary=_first_of_its_asin(Series),
        fetched_description=bindparam("fetched_description"),
        # none_as_null so an absent blob binds SQL NULL rather than the JSON
        # null scalar, which the NULL arms of extras_union could not tell
        # from a real answer. Same binding the books upsert uses.
        audible_extras=merge.json_bind("audible_extras"),
        extras_withheld=merge.json_bind("extras_withheld"),
        created_at=bindparam("created_at"),
        updated_at=bindparam("updated_at"),
    )
    # The conflict target is (asin, region): a series ASIN returned by another
    # marketplace is its own row, never a merge into this one. Region is in the
    # key, so it is never in the update either.
    return stmt.on_conflict_do_update(
        index_elements=["asin", "region"],
        set_={
            "title": merge.coalesce(bindparam("title"), Series.title),
            "description": merge.longer_wins(bindparam("description"), Series.description),
            "fetched_description": Series.fetched_description | stmt.excluded.fetched_description,
            # Merged exactly as a book's are; extras_union carries why. A
            # series that arrives through a book's relationships binds both
            # as NULL and leaves what a profile fetch stored untouched.
            "audible_extras": merge.extras_union(stmt.excluded.audible_extras, Series.audible_extras),
            "extras_withheld": merge.extras_union(stmt.excluded.extras_withheld, Series.extras_withheld),
            "updated_at": stmt.excluded.updated_at,
        },
    )


def _build_book_upsert(insert):
    """
    The one book upsert, built once and executed with bound rows.

    Nothing about a book appears in the statement -- every value it writes is a
    named bind parameter -- so a chunk of fifty books is one compile and fifty
    parameter sets rather than fifty compiles. That is the entire performance
    argument for this shape, and it does not come from statement reuse:
    postgresql's Insert sets inherit_cache = False and OnConflictDoUpdate
    defines no traversal internals, so this object is recompiled on every
    execute() no matter how long it has been held. What is saved is a compile
    per book, not a compile per statement.

    It deliberately carries NO returning(). Adding one turns SQLAlchemy's
    use_insertmanyvalues on, which rewrites the executemany into batched
    multi-row VALUES groups -- and one INSERT ... ON CONFLICT DO UPDATE may not
    affect the same row twice, so a chunk holding the same ASIN twice raises
    cardinality_violation (21000) and drops all fifty books to the per-book
    replay path this exists to avoid. Duplicate ASINs inside a chunk are
    ordinary, not hypothetical: an author walk pages a catalog whose sort
    windows shift under it. Without returning(), the statement stays one row
    per execution, a repeated ASIN simply upserts twice, and the second
    execution merges against the first exactly as a later request would.

    Measured on the pinned SQLAlchemy rather than assumed, and the measurement
    is worth keeping because the rewrite turns out to be conditional in a way
    that flatters this statement by accident. A set_ clause referencing a
    shared bind parameter cannot be renumbered across VALUES groups, so the
    compiler abandons the rewrite and falls back to one statement per row --
    the same statement with returning() and a set_ built only from excluded
    columns reproduced the 21000 on the first try, while this one merely lost
    its executemany and ran fifty times. The coalesce binds below are what put
    it on the safe side today, which makes them a coincidence rather than a
    guard: keeping returning() off is the part that holds no matter what the
    set_ is later rewritten to say.

    Four asymmetries in the merge are load-bearing and must survive any edit
    that regenerates this from the column list:

    - created_at is written on insert and absent from the update. Deriving the
      update from the insert's columns adds excluded.created_at and resets
      every book's real creation time on its next write, silently.
    - is_primary is written on insert and absent from the update, like
      created_at: it records whether the row was first of its ASIN when it was
      stored, and a later write must not reconsider that.
    - region is in the conflict target and never in the update. A book is
      identified by (asin, region), so a response fetched for another region
      conflicts with nothing and lands as that region's own row; it can never
      move an existing one.
    - title falls back to '' on insert (the column is NOT NULL) but to the
      stored title on update, so a response that omits it cannot blank one
      that is already stored. The update reads that bind through answered,
      which counts '' and whitespace as no answer alongside NULL. It used to
      read it through a plain coalesce and rely on the bind never carrying
      '', which nothing in this module was in a position to guarantee.
    """
    stmt = insert(Book).values(
        asin=bindparam("asin"),
        title=merge.coalesce(bindparam("title"), ""),
        subtitle=bindparam("subtitle"),
        region=bindparam("region"),
        is_primary=_first_of_its_asin(Book),
        description=bindparam("description"),
        summary=bindparam("summary"),
        publisher=bindparam("publisher"),
        copyright=bindparam("copyright"),
        isbn=bindparam("isbn"),
        language=bindparam("language"),
        rating=bindparam("rating"),
        release_date=bindparam("release_date"),
        length_minutes=bindparam("length_minutes"),
        explicit=merge.coalesce(bindparam("explicit"), False),
        whisper_sync=merge.coalesce(bindparam("whisper_sync"), False),
        has_pdf=merge.coalesce(bindparam("has_pdf"), False),
        image=bindparam("image"),
        book_format=bindparam("book_format"),
        content_type=bindparam("content_type"),
        content_delivery_type=bindparam("content_delivery_type"),
        episode_number=bindparam("episode_number"),
        episode_type=bindparam("episode_type"),
        sku=bindparam("sku"),
        sku_group=bindparam("sku_group"),
        is_listenable=merge.coalesce(bindparam("is_listenable"), True),
        is_buyable=merge.coalesce(bindparam("is_buyable"), True),
        is_vvab=merge.coalesce(bindparam("is_vvab"), False),
        # none_as_null is not optional here. JSONB's default is to serialize a
        # Python None to the JSON value null, which is a value and not SQL
        # NULL -- so coalesce below would take it in preference to the stored
        # array and empty the column on every response that carries no plans.
        # json_bind binds None as SQL NULL.
        plans=merge.json_bind("plans"),
        num_ratings=bindparam("num_ratings"),
        num_reviews=bindparam("num_reviews"),
        publication_name=bindparam("publication_name"),
        publication_datetime=bindparam("publication_datetime"),
        extended_product_description=bindparam("extended_product_description"),
        product_state=bindparam("product_state"),
        # none_as_null is not optional on either of these, for the reason
        # spelled out at plans above and with the same consequence: without
        # it a Python None serializes to the JSON value null, which is a
        # value rather than SQL NULL, and the merge would prefer it to the
        # stored blob and empty the column on every response whose extras
        # were dropped whole.
        #
        # It is also what makes the three-state column work rather than
        # merely what keeps it safe. A genuinely empty extras dict is {} in
        # Python, which is not None, so it serializes to a real written
        # empty object and records that Audible was asked and had nothing to
        # add -- distinct from the NULL of a row no response has touched
        # since the column was added.
        audible_extras=merge.json_bind("audible_extras"),
        extras_withheld=merge.json_bind("extras_withheld"),
        created_at=bindparam("created_at"),
        updated_at=bindparam("updated_at"),
    )
    return stmt.on_conflict_do_update(
        index_elements=["asin", "region"],
        set_={
            # Sixteen text columns merge on answered-versus-blank rather
            # than on NULL alone. Audible has no vocabulary for retracting
            # any of them -- no response means "this book no longer has a
            # publisher" -- so an empty string is Audible declining to answer,
            # never Audible asserting none, and answered keeps what is
            # already stored. Each was decided on its own grounds, not by
            # applying one rule across the row. Fourteen are argued here;
            # publication_name and product_state are the other two, argued
            # in the scalar block further down beside the columns they
            # arrived with:
            #
            #   title                   The edition's own name. A reissue
            #                           renames a book; nothing un-names one.
            #                           title is NOT NULL besides, so a blank
            #                           there is the worst loss on the row.
            #   subtitle                Goes with title but stands on weaker
            #                           ground, and the difference is worth
            #                           stating rather than borrowing: a
            #                           second edition genuinely dropping its
            #                           subtitle is a real thing, so a blank
            #                           here could be an answer in a way a
            #                           blank title could not. It is guarded
            #                           anyway because the tie-break for this
            #                           row is already settled -- stale but
            #                           rich beats fresh but empty -- not
            #                           because title's argument covers it.
            #   publisher, copyright,   Catalogue identity, fixed at
            #   isbn, language, sku,    publication. A blank is a response
            #   sku_group               group that came back thin, not a fact
            #                           that changed underneath us.
            #   image                   A cover is superseded by another URL,
            #                           never withdrawn to nothing.
            #   book_format,            Classification labels, guarded on the
            #   content_type,           same ground as the rest of the row.
            #   content_delivery_type,  The stronger argument -- that each
            #   episode_type            draws from a fixed vocabulary that
            #                           does not contain '', putting a blank
            #                           outside the answer set rather than in
            #                           it -- is unverified and should not be
            #                           relied on: nothing in this repo
            #                           enumerates any of the four, and no
            #                           live probe has established them.
            #   episode_number          Reaches this statement only through
            #                           the product normalizer, which already
            #                           turns a falsy episode number into
            #                           None, so what a guard adds today is
            #                           the whitespace-only case alone.
            #                           Guarded regardless: this merge cannot
            #                           see that upstream truthiness test,
            #                           and a column whose safety lives in
            #                           another module is one edit away from
            #                           the defect the other thirteen had.
            "title": merge.answered(bindparam("title"), Book.title),
            "subtitle": merge.answered(stmt.excluded.subtitle, Book.subtitle),
            "description": merge.longer_wins(bindparam("description"), Book.description),
            "summary": merge.longer_wins(bindparam("summary"), Book.summary),
            "publisher": merge.answered(stmt.excluded.publisher, Book.publisher),
            "copyright": merge.answered(stmt.excluded.copyright, Book.copyright),
            "isbn": merge.answered(stmt.excluded.isbn, Book.isbn),
            "language": merge.answered(stmt.excluded.language, Book.language),
            "rating": merge.coalesce(stmt.excluded.rating, Book.rating),
            "release_date": merge.coalesce(stmt.excluded.release_date, Book.release_date),
            "length_minutes": merge.coalesce(stmt.excluded.length_minutes, Book.length_minutes),
            # Same asserted-versus-silent merge as is_listenable/is_buyable/
            # is_vvab below, and for the same reason: the column is NOT NULL,
            # so a response that omits explicit/whisperSync/hasPdf cannot be
            # told apart from one asserting false unless the bind itself
            # carries that distinction. asserted_bool reads the raw payload
            # for these three exactly as it does for the other three; reading
            # through stmt.excluded here instead of the bindparam would take
            # the insert side's own coalesce-to-False rather than the bind
            # Audible actually sent, making a response that omits the field
            # indistinguishable from one asserting false and silently
            # discarding a stored true.
            "explicit": merge.coalesce(bindparam("explicit"), Book.explicit),
            "whisper_sync": merge.coalesce(bindparam("whisper_sync"), Book.whisper_sync),
            "has_pdf": merge.coalesce(bindparam("has_pdf"), Book.has_pdf),
            "image": merge.answered(stmt.excluded.image, Book.image),
            "book_format": merge.answered(stmt.excluded.book_format, Book.book_format),
            "content_type": merge.answered(stmt.excluded.content_type, Book.content_type),
            "content_delivery_type": merge.answered(
                stmt.excluded.content_delivery_type, Book.content_delivery_type
            ),
            "episode_number": merge.answered(stmt.excluded.episode_number, Book.episode_number),
            "episode_type": merge.answered(stmt.excluded.episode_type, Book.episode_type),
            "sku": merge.answered(stmt.excluded.sku, Book.sku),
            "sku_group": merge.answered(stmt.excluded.sku_group, Book.sku_group),
            # The other three NOT NULL booleans merge the same way, on
            # asserted-versus-silent rather than true-versus-false: excluded
            # here would carry the insert default and overwrite a stored
            # answer with one Audible never gave. See asserted_bool for why
            # the bind is tri-state.
            "is_listenable": merge.coalesce(bindparam("is_listenable"), Book.is_listenable),
            "is_buyable": merge.coalesce(bindparam("is_buyable"), Book.is_buyable),
            "is_vvab": merge.coalesce(bindparam("is_vvab"), Book.is_vvab),
            # plans stays on the NULL-only merge, and that is a decision
            # rather than an omission. It is the one column here where a
            # blank is a real answer: an empty plans array is how a book that
            # has left the Plus catalogue reports itself, and Audible is
            # entitled to assert exactly that. Guarding it would hold a book
            # in a catalogue it no longer belongs to -- trading a silent
            # shrink for a silent staleness, which is not self-evidently the
            # better bargain. Which way that trade should go is a product
            # question about the plans field, not a question about this
            # merge, and it is open.
            #
            # Open, but not unattended, and a reader deciding from this
            # statement alone would not know that. The plans parser
            # (libex_core.audible.books) has already ruled on the same column
            # from the other end: None for a response carrying no plans key,
            # [] only for an explicitly empty one, and -- the case that
            # matters here -- None again when entries are present but none of
            # them yields a readable plan_name. That third fold is what keeps
            # an upstream rename from emptying this column across the corpus,
            # which is the damage a guard here would otherwise be needed for.
            # What is left open is narrower than it looks: only whether Libex
            # should keep believing Audible when Audible says, clearly, none.
            "plans": merge.coalesce(stmt.excluded.plans, Book.plans),
            # The six scalar columns beside the blob each get the merge its
            # own field argues for, not the one its type suggests.
            #
            #   num_ratings,            Plain NULL merges. Both normalize to
            #   num_reviews             None rather than 0 when Audible does
            #                           not answer, which is what keeps this
            #                           coalesce honest -- a bound 0 is a
            #                           value, and would overwrite a stored
            #                           thirty thousand with nothing.
            #   publication_datetime    Plain NULL merge. A publication
            #                           instant is fixed at publication; a
            #                           later response either restates it or
            #                           omits it.
            #   publication_name        answered rather than coalesce: this
            #                           is one of the fields a thin response
            #                           group returns as '' rather than
            #                           omitting, and coalesce('', stored)
            #                           is '' -- SQL sees a value and takes
            #                           it.
            #   extended_product_       Same family as description and
            #   description             summary, and merged the same way. It
            #                           is the long-form text of the row, it
            #                           grows as response groups fill in, and
            #                           a plain coalesce would let a shorter
            #                           later response win.
            #   product_state           answered, which is the only one of
            #                           the three that clears both hazards
            #                           this column has. A thin response
            #                           group sends it as '' rather than
            #                           omitting it, and coalesce('', stored)
            #                           is '' -- a blank would blank a state
            #                           the row already knew. And a length
            #                           measure is wrong for a different
            #                           reason worth keeping in view: this is
            #                           a state that legitimately changes, a
            #                           book moving between AVAILABLE,
            #                           AVAILABLE_FOR_PREORDER and
            #                           NOT_AVAILABLE_FOR_PURCHASE over its
            #                           life, so longer_wins would pin
            #                           NOT_AVAILABLE_FOR_PURCHASE (26
            #                           characters) permanently over
            #                           AVAILABLE (9) and leave the row
            #                           asserting a book is unbuyable forever.
            "num_ratings": merge.coalesce(stmt.excluded.num_ratings, Book.num_ratings),
            "num_reviews": merge.coalesce(stmt.excluded.num_reviews, Book.num_reviews),
            "publication_name": merge.answered(
                stmt.excluded.publication_name, Book.publication_name
            ),
            "publication_datetime": merge.coalesce(
                stmt.excluded.publication_datetime, Book.publication_datetime
            ),
            "extended_product_description": merge.longer_wins(
                bindparam("extended_product_description"), Book.extended_product_description
            ),
            "product_state": merge.answered(stmt.excluded.product_state, Book.product_state),
            # Read through excluded rather than the bindparam deliberately:
            # excluded carries the insert side's cast to JSONB, and @> and ||
            # both need the operand to be typed jsonb to resolve at all.
            "audible_extras": merge.extras_union(stmt.excluded.audible_extras, Book.audible_extras),
            # The record of what was left out of the blob, merged the way the
            # blob itself is, because the two are read as one picture and a
            # pair covering different spans of time cannot be read that way.
            #
            # This was a plain coalesce, which made the column "whatever the
            # most recent fetch that withheld anything happened to withhold"
            # while audible_extras beside it accumulated key by key. A podcast
            # fetch records relationships {episode: 4412}; a later fetch that
            # strips one NUL character replaces the whole record, and the
            # episode count is gone while the relationships key it described
            # is still sitting in the blob. That is the shrinkage rule failing
            # inside a merge, for the identical reason extras_union exists:
            # the top-level keys are independently sourced. relationships
            # comes from the podcast strip, sanitized from the jsonb
            # sanitizer, audibleExtras from the depth, encode and size checks
            # -- three producers that fire independently, so one of them
            # firing must not erase another's finding.
            #
            # What the column means now: per kind of withholding, the record
            # left by the most recent fetch that withheld that kind -- unless
            # that fetch's account was already contained in the stored one,
            # which the containment arm below leaves standing rather than
            # rewriting, so the fuller entry survives a thinner later one. A
            # union over time, never cleared, exactly as the blob is -- so
            # "this key is in the blob" and "this was withheld from it" are
            # claims about the same span, and the caller can hold them
            # together.
            #
            # It is deliberately not a snapshot of what is missing from the
            # row as it stands, because no merge available here can make it
            # one: a key withheld once and supplied by a later fetch leaves a
            # note that outlives what it describes. Keeping that stale note is
            # the accepted side of the trade, unchanged from before -- the
            # alternative is a fetch that said nothing erasing the only record
            # that anything was ever dropped.
            #
            # Clearing on a clean fetch is not available either, and that is a
            # property of the input rather than a choice made here. The
            # normalizer omits extrasWithheld when nothing was withheld, so
            # "nothing withheld this time" and "this write has no opinion"
            # both arrive as NULL and are indistinguishable at this point.
            # Reading NULL as "clear it" would clear the record on every
            # ordinary write that never looked.
            "extras_withheld": merge.extras_union(
                stmt.excluded.extras_withheld, Book.extras_withheld
            ),
            "updated_at": stmt.excluded.updated_at,
        },
    )


def _build_pivot_insert(insert, table, *columns):
    """
    An additive pivot insert: one row per execution, conflicts ignored.

    Every pivot Libex writes is a link that may already exist and must never
    be removed, so DO NOTHING is the whole merge rule and there is nothing to
    parameterise beyond the row itself. A link to a book or series carries
    that record's region, and the unique key includes it: without that, the
    second marketplace's link to the same ASIN would be the conflict this
    ignores, and would be dropped without a trace.
    """
    return insert(table).values(
        **{column: bindparam(column) for column in columns}
    ).on_conflict_do_nothing()


def _build_book_series_upsert(insert):
    """
    The book-to-series link, which unlike the other pivots carries a value:
    position moves as Audible restates it, so this one updates rather than
    ignoring the conflict -- but only from a non-null incoming position, so a
    response that omits it leaves the stored one standing.
    """
    stmt = insert(book_series).values(
        book_asin=bindparam("book_asin"),
        book_region=bindparam("book_region"),
        series_asin=bindparam("series_asin"),
        series_region=bindparam("series_region"),
        position=bindparam("position"),
    )
    return stmt.on_conflict_do_update(
        index_elements=["book_asin", "book_region", "series_asin", "series_region"],
        set_={"position": merge.coalesce(stmt.excluded.position, book_series.c.position)},
    )


@lru_cache(maxsize=None)
def statements_for(dialect: str) -> Statements:
    """The prebuilt statements for a dialect name, built on first use."""
    insert = insert_for(dialect)
    return Statements(
        book_upsert=_build_book_upsert(insert),
        series_upsert=_build_series_upsert(insert),
        genre_insert=_build_pivot_insert(insert, Genre, "asin", "name", "type", "created_at", "updated_at"),
        narrator_insert=_build_pivot_insert(insert, Narrator, "name", "created_at", "updated_at"),
        book_genre_insert=_build_pivot_insert(
            insert, book_genre, "book_asin", "book_region", "genre_asin"
        ),
        book_narrator_insert=_build_pivot_insert(
            insert, book_narrator, "book_asin", "book_region", "narrator_name"
        ),
        author_book_insert=_build_pivot_insert(
            insert, author_book, "author_id", "book_asin", "book_region"
        ),
        series_author_insert=_build_pivot_insert(
            insert, series_author, "series_asin", "series_region", "author_id"
        ),
        book_series_upsert=_build_book_series_upsert(insert),
    )
