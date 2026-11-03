"""
A manual entry that Audible later lists again: Audible's text wins once,
manual_entry_at stays, and the book stays on /db/delisted.
"""

# Standard library
import time


def test_relist_audible_description_wins_once_and_manual_entry_stays(sbx):
    asin = sbx.live_asin("us", nth=1)
    # Before the row is marked, so a 404 below is about the mark and not about
    # a search hit that /books cannot serve.
    plain = sbx.get_live(asin, cache="false")
    assert plain.status_code == 200, "this search hit is not fetchable through /books even unmarked"
    where = f"asin = '{asin}' AND region = 'us'"
    assert sbx.wait_for(lambda: sbx.psql(
        f"SELECT confirmed_at IS NOT NULL FROM books WHERE {where}") == "t", 30)
    time.sleep(2)  # the background write that follows the GET has finished
    sbx.psql(f"UPDATE books SET manual_entry_at = now(), confirmed_at = NULL, "
             f"description = repeat('x', 5000) WHERE {where}")
    marked = sbx.psql(f"SELECT manual_entry_at::text FROM books WHERE {where}")
    sbx.psql(f"DELETE FROM cache WHERE key = 'book:us:{asin}'")

    first = sbx.get_live(asin)
    assert first.status_code == 200, "a listed book 404s on /books once it carries the manual mark"
    assert first.json()["manualEntry"] is False  # a live fetch never reads manual
    assert sbx.wait_for(
        lambda: sbx.psql(f"SELECT confirmed_at IS NOT NULL FROM books WHERE {where}") == "t", 30
    )
    stored = sbx.psql(f"SELECT description FROM books WHERE {where}")
    assert stored != "x" * 5000
    assert stored == first.json()["description"]
    assert sbx.psql(f"SELECT manual_entry_at::text FROM books WHERE {where}") == marked

    # A cache hit is the stored row read back, so it reads manual (the live
    # fetch above did not): the two differ for the same ASIN.
    assert sbx.wait_for(lambda: sbx.psql(f"SELECT count(*) FROM cache WHERE key = 'book:us:{asin}'") == "1", 30)
    hit = sbx.http.get(f"/books/{asin}", params={"region": "us"})
    assert hit.status_code == 200
    assert hit.json()["manualEntry"] is True
    assert sbx.psql(f"SELECT value->>'manualEntry' FROM cache WHERE key = 'book:us:{asin}'") == "true"

    # One-shot: with the book confirmed, a longer stored text now beats Audible's.
    confirmed = sbx.psql(f"SELECT confirmed_at::text FROM books WHERE {where}")
    sbx.psql(f"UPDATE books SET description = repeat('y', 5000) WHERE {where}")
    second = sbx.get_live(asin, cache="false")
    assert second.status_code == 200
    sbx.wait_for(lambda: sbx.psql(f"SELECT confirmed_at::text FROM books WHERE {where}") != confirmed, 20)
    assert sbx.psql(f"SELECT description = repeat('y', 5000) FROM books WHERE {where}") == "t"
    assert sbx.psql(f"SELECT manual_entry_at::text FROM books WHERE {where}") == marked

    listed = [b for b in sbx.delisted(region="us", limit=100).json() if b["asin"] == asin]
    assert len(listed) == 1 and listed[0]["manualEntry"] is True
    refused = sbx.post(sbx.book(asin=asin), dry_run=False)
    assert refused.status_code == 409
    assert refused.json() == {"reason": "live"}
