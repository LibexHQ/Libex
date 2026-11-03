"""
Body size and shape on POST /internal/books. None of these reach Audible
except the two bodies that sit exactly on the size limit, which are valid.
"""

# Standard library
import json

# Third party
import pytest

MAX_BODY = 1024 * 1024


def padded(body: dict, size: int) -> bytes:
    raw = json.dumps(body).encode()
    return raw + b" " * (size - len(raw))


def chunked(raw: bytes, step: int = 65536):
    for i in range(0, len(raw), step):
        yield raw[i:i + step]


def test_body_at_the_limit_passes_and_one_byte_over_is_413(sbx):
    body = sbx.book()
    ok = sbx.post(None, content=padded(body, MAX_BODY))
    assert ok.status_code == 200
    over = sbx.post(None, content=padded(body, MAX_BODY + 1), audible=False)
    assert over.status_code == 413
    assert over.json()["error"] == "Payload too large"


def test_chunked_body_is_capped_the_same_way(sbx):
    body = sbx.book()
    ok = sbx.post(None, content=chunked(padded(body, MAX_BODY)))
    assert ok.status_code == 200
    over = sbx.post(None, content=chunked(padded(body, MAX_BODY + 1)), audible=False)
    assert over.status_code == 413


def test_the_limit_counts_bytes_not_characters(sbx):
    # 400k characters: far under 1 MiB as text, over it as UTF-8 (3 bytes each).
    wide = sbx.book(description="あ" * 400_000, subtitle=sbx.canary())
    assert sbx.post(wide, audible=False).status_code == 413
    # The same number of ASCII characters is under the byte cap, so it is read
    # and refused for what it is (a description past its own cap), not as too large.
    narrow = sbx.post(sbx.book(description="a" * 400_000, subtitle=sbx.canary()), audible=False)
    assert narrow.status_code == 422
    assert {"loc": ["book", "description"], "type": "string_too_long"} in narrow.json()["errors"]


def _book(field, value):
    def mutate(b, c):
        b["book"][field] = value(c) if callable(value) else value
    return mutate


def _nested(field, item):
    def mutate(b, c):
        b["book"][field] = [item(c)]
    return mutate


CASES = {
    "top_manual_entry": (lambda b, c: b.update(manualEntry=c), ["manualEntry"], "extra_forbidden"),
    "top_unknown": (lambda b, c: b.update(extra=c), ["extra"], "extra_forbidden"),
    "book_manual_entry": (lambda b, c: b["book"].update(manualEntry=True, subtitle=c),
                          ["book", "manualEntry"], "extra_forbidden"),
    "book_link": (lambda b, c: b["book"].update(link=c), ["book", "link"], "extra_forbidden"),
    "author_unknown": (lambda b, c: b["book"]["authors"][0].update(bogus=c),
                       ["book", "authors", 0, "bogus"], "extra_forbidden"),
    "narrator_unknown": (_nested("narrators", lambda c: {"name": "SBX N", "bogus": c}),
                         ["book", "narrators", 0, "bogus"], "extra_forbidden"),
    "series_unknown": (_nested("series", lambda c: {"asin": "B0SBXSER01", "name": "SBX S", "bogus": c}),
                       ["book", "series", 0, "bogus"], "extra_forbidden"),
    "genre_unknown": (_nested("genres", lambda c: {"asin": "SBXG1", "name": "SBX G", "type": "Tags", "bogus": c}),
                      ["book", "genres", 0, "bogus"], "extra_forbidden"),
    "chapters_unknown": (lambda b, c: b.update(chapters={**_chapters(), "bogus": c}),
                         ["chapters", "bogus"], "extra_forbidden"),
    "chapter_item_unknown": (lambda b, c: b.update(chapters={
        "runtimeLengthMs": 1, "chapters": [{"title": "t", "lengthMs": 1, "startOffsetMs": 0, "bogus": c}]}),
        ["chapters", "chapters", 0, "bogus"], "extra_forbidden"),
    "http_image": (_book("imageUrl", "http://example.com/a.jpg"), ["book", "imageUrl"], "value_error"),
    "image_with_userinfo": (_book("imageUrl", "https://user:pw@example.com/a.jpg"),
                            ["book", "imageUrl"], "value_error"),
    "author_http_image": (lambda b, c: b["book"]["authors"][0].update(image="http://example.com/a.jpg"),
                          ["book", "authors", 0, "image"], "value_error"),
    "title_over_cap": (_book("title", lambda c: c + "t" * 1000), ["book", "title"], "string_too_long"),
    "bad_asin": (_book("asin", "SHORT"), ["book", "asin"], "value_error"),
    "bad_date": (_book("releaseDate", "not-a-date"), ["book", "releaseDate"], "value_error"),
    "nul_in_text": (_book("publisher", "a\u0000b"), ["book", "publisher"], "value_error"),
    "too_many_genres": (lambda b, c: b["book"].update(genres=[
        {"asin": f"SBXG{i}", "name": f"g{i}", "type": "Tags"} for i in range(201)]),
        ["book", "genres"], "too_long"),
    "no_authors": (_book("authors", []), ["book", "authors"], "too_short"),
}


def _chapters():
    return {"runtimeLengthMs": 1, "chapters": [{"title": "t", "lengthMs": 1, "startOffsetMs": 0}]}


@pytest.mark.parametrize("case", list(CASES))
def test_refused_body_is_422_with_only_location_and_type(sbx, case):
    mutate, loc, kind = CASES[case]
    canary = sbx.canary()
    body = sbx.book()
    mutate(body, canary)
    before = sbx.probe_count()
    resp = sbx.post(body, audible=False)
    assert resp.status_code == 422
    errors = resp.json()["errors"]
    assert {"loc": loc, "type": kind} in errors
    assert all(set(e) == {"loc", "type"} for e in errors)
    assert canary not in resp.text
    assert sbx.probe_count() == before


def test_region_in_the_body_must_match_the_query(sbx):
    resp = sbx.post(sbx.book(region="uk"), region="us", audible=False)
    assert resp.status_code == 422
    assert resp.json()["errors"] == [{"loc": ["book", "region"], "type": "region_mismatch"}]
