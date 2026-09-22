"""
One upstream Audible product, shared by the tests that describe a book's
whole shape.

Two files need the same thing and need it to be the same thing:
tests/test_book_shape_parity.py checks that the builder, the reader and the
response model name identical fields, and
tests/integration/test_book_widening_roundtrip.py checks that every one of
those fields survives a write and a read. Both want a product carrying every
top-level key the normalizer reads, and the second is only meaningful over
the keys the first compared.

They held two hand-written copies of it before, 34 of roughly 35 keys
identical. The copies did not stay identical, and the way they diverged is
the argument for this module: one of them carried num_reviews at the product
top level, where the normalizer does not look for it -- it reads
rating.num_reviews -- so numReviews was null through a whole file of
set-based assertions that pass on a null just as readily as on a value, and
the stray key flowed into audibleExtras instead. Nothing could fail. The
other copy had it nested correctly, and neither copy could see the other.

Values are shaped the way Audible shapes them rather than minimally: the
rating halves sit at two different depths, images are keyed by pixel width,
series arrives as a relationship rather than a field of its own, and the
long-form description carries its markup. A stand-in that flattened any of
those would exercise a product Audible never sends.

Three classes of key are present on purpose, because the split between them
is what the blob is for:

  reproduced    read as a first-class response field and therefore kept out
                of audibleExtras.
  transformed   read for a derived field and carried into the blob as well,
                because only part of it is consumed -- rating,
                product_images, category_ladders, relationships and the rest.
  unread        nothing looks at them; social_media_images and
                platinum_keywords are here so the blob has content that
                exists for no other reason than that Audible sent it.

Callers override what they need -- an asin of their own above all, since two
tests writing the same row against one database would read each other's
writes. Override by copying, never by mutating: `{**AUDIBLE_PRODUCT, ...}`.
A dict at module scope is shared by every test that imports it, and an
in-place edit in one of them is visible in all the others.
"""

AUDIBLE_PRODUCT = {
    "asin": "B0PRODUCT01",
    "title": "A Product Title",
    "subtitle": "A Product Subtitle",
    "merchandising_summary": "<p>A short description.</p>",
    "publisher_summary": "<p>A longer summary.</p>",
    "publisher_name": "A Publisher",
    "copyright": "(c) 2020 A Publisher",
    "isbn": "9781234567897",
    "language": "english",
    # Both halves of the rating, at the two different depths Audible puts
    # them at: num_ratings inside overall_distribution beside average_rating,
    # num_reviews a level up beside overall_distribution itself.
    "rating": {
        "overall_distribution": {"average_rating": 4.5, "num_ratings": 312915},
        "num_reviews": 48002,
    },
    "format_type": "unabridged",
    "release_date": "2020-01-01",
    "publication_datetime": "2020-06-01T00:00:00Z",
    "publication_name": "A Publication",
    # Markup rather than plain text: this is the one long-form field carried
    # through with its tags intact, so a copy stripped of them would leave
    # that difference untested.
    "extended_product_description": "<p>An extended description.</p><p>Second paragraph.</p>",
    # One of the three values observed from Audible so far -- the other
    # two being AVAILABLE_FOR_PREORDER and NOT_AVAILABLE_FOR_PURCHASE.
    # The vocabulary is Audible's and can grow, so nothing treats this
    # as an enum; what matters here is that it is a string Audible is
    # known to send rather than one invented for a test.
    "product_state": "AVAILABLE",
    "is_adult_product": True,
    "is_pdf_url_available": True,
    "read_along_support": True,
    "product_images": {"500": "https://example.com/i._SX500_.jpg"},
    "runtime_length_min": 600,
    "content_type": "Product",
    "content_delivery_type": "SinglePartBook",
    "sku": "SKU123",
    "sku_lite": "SG123",
    "is_listenable": True,
    "is_buyable": True,
    "is_vvab": True,
    "plans": [{"plan_name": "US Minerva"}],
    "authors": [{"asin": "B0AUTHOR01", "name": "An Author"}],
    "narrators": [{"name": "A Narrator"}],
    "category_ladders": [
        {"root": "Genres", "ladder": [{"id": "18580606011", "name": "Fantasy"}]}
    ],
    "relationships": [
        {"asin": "B0SERIES01", "relationship_type": "series", "sort": "1", "title": "A Series"}
    ],
    "social_media_images": {"facebook": "https://example.com/fb.jpg"},
    "platinum_keywords": ["fantasy", "epic"],
}
