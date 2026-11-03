"""
GET /db/delisted: empty, its parameters, and a full run of paging, sorting and
filtering over twelve committed entries. This file runs before any other that
commits, so the list starts empty and the counts below are exact.
"""

# Standard library
import json

# Third party
import pytest

LETTERS = "ABCDEFGHIJKL"
# Headers that differ per response by design; everything else is policy.
PER_RESPONSE = {"date", "content-length", "x-request-id", "etag", "last-modified"}


def test_empty_list_is_200_and_empty(sbx):
    assert sbx.psql("SELECT count(*) FROM books WHERE manual_entry_at IS NOT NULL") == "0"
    for params in ({}, {"region": "us"}, {"region": "jp", "sort": "title"}):
        resp = sbx.delisted(**params)
        assert resp.status_code == 200
        assert resp.json() == []
    if sbx.mode == "snapshot":
        # Recorded, not asserted: at zero manual rows the planner may reasonably
        # scan; the plan after the commits is the one test_90 checks.
        plan = sbx.psql("EXPLAIN SELECT * FROM books WHERE manual_entry_at IS NOT NULL "
                        "ORDER BY manual_entry_at DESC LIMIT 20")
        print("plan at zero manual rows:", plan.replace("\n", " | "))


@pytest.mark.parametrize("params,status", [
    ({"region": "zz"}, 400),
    ({"limit": 0}, 422),
    ({"limit": 101}, 422),
    ({"page": 0}, 422),
    ({"limit": 100, "page": 1, "region": "us"}, 200),
])
def test_parameters_are_validated(sbx, params, status):
    assert sbx.delisted(**params).status_code == status


def _commit(sbx, **over):
    body = sbx.book(**over)
    resp = sbx.post(body, region=body["book"]["region"], dry_run=False)
    assert resp.status_code == 200
    assert resp.json()["committed"] is True
    return body["book"]["asin"]


def test_paging_order_sort_and_region_filter(sbx):
    asins = [_commit(sbx, title=f"SBX List {letter}") for letter in LETTERS]
    uk = _commit(sbx, region="uk", title="SBX List UK")

    pages = [sbx.delisted(region="us", limit=5, page=p).json() for p in (1, 2, 3, 4)]
    assert [len(p) for p in pages] == [5, 5, 2, 0]
    got = [b["asin"] for p in pages for b in p]
    assert len(set(got)) == len(got)
    assert got == list(reversed(asins))  # newest entry first, not insertion or ASIN order
    assert all(b["manualEntry"] is True for p in pages for b in p)

    # Sorted pages are the sorted whole: no entry repeated or skipped at a boundary.
    asc = [b["title"] for b in sbx.delisted(region="us", sort="title", order="asc", limit=100).json()]
    assert asc == [f"SBX List {x}" for x in LETTERS]
    desc = [b["title"] for b in sbx.delisted(region="us", sort="title", order="desc", limit=100).json()]
    assert desc == list(reversed(asc))
    paged = [b["title"] for p in (1, 2, 3)
             for b in sbx.delisted(region="us", sort="title", order="asc", limit=5, page=p).json()]
    assert paged == asc

    # The region filter returns that region only, and no filter returns both.
    assert [(b["asin"], b["region"]) for b in sbx.delisted(region="uk").json()] == [(uk, "uk")]
    assert uk not in got
    everything = {a for a, _ in sbx.delisted_asins()}
    assert {uk, *asins} <= everything


def test_headers_match_the_other_db_list_routes(sbx):
    mine = sbx.delisted(region="us", limit=1)
    other = sbx.http.get("/db/book", params={"region": "us", "limit": 1})
    assert mine.status_code == other.status_code == 200

    def policy(resp):
        return {k.lower(): v for k, v in resp.headers.items() if k.lower() not in PER_RESPONSE}

    assert policy(mine) == policy(other)


def test_a_new_commit_shows_on_the_next_read(sbx):
    before = sbx.delisted(region="us", limit=100).json()
    asin = _commit(sbx, title="SBX List M")
    after = sbx.delisted(region="us", limit=100).json()
    assert asin not in [b["asin"] for b in before]
    assert after[0]["asin"] == asin  # no server-side staleness
    assert len(after) == len(before) + 1


def test_the_item_is_the_full_record(sbx):
    asin = _commit(sbx, title="SBX List N", publisher="SBX Pub")
    listed = sbx.delisted(region="us", limit=1).json()[0]
    stored = sbx.db_book(asin).json()
    assert listed == stored
    assert json.dumps(listed).count("SBX Pub") == 1
