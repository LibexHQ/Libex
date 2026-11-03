"""Parallel requests across the six workers."""

# Standard library
from concurrent.futures import ThreadPoolExecutor

ZERO = (0, 0, 0, 0, 0, 0)


def _parallel(sbx, bodies, dry_run):
    # One check for the whole burst: ten probes at once, accepted once and recorded.
    sbx.audible("probe", planned=len(bodies))
    with ThreadPoolExecutor(len(bodies)) as pool:
        return list(pool.map(lambda b: sbx.post(b, dry_run=dry_run, audible=False), bodies))


def test_ten_parallel_commits_of_one_body_give_one_200_and_nine_409(sbx):
    body = sbx.book(narrators=[{"name": "SBX Race Narrator"}])
    body["chapters"] = sbx.chapters()
    asin = body["book"]["asin"]
    results = _parallel(sbx, [body] * 10, dry_run=False)
    assert sorted(r.status_code for r in results) == [200] + [409] * 9
    assert all(r.json() == {"reason": "stored"} for r in results if r.status_code == 409)
    assert sbx.rows_of(asin) == (1, 1, 1, 0, 0, 1)
    assert sbx.psql(f"SELECT count(*) FROM authors WHERE name = 'SBX Author {asin}'") == "1"


def test_ten_parallel_dry_runs_all_succeed_and_store_nothing(sbx):
    body = sbx.book()
    asin = body["book"]["asin"]
    before = sbx.state([asin])
    results = _parallel(sbx, [body] * 10, dry_run=True)
    assert [r.status_code for r in results] == [200] * 10
    assert all(r.json()["committed"] is False for r in results)
    assert sbx.state([asin]) == before
    assert sbx.rows_of(asin) == ZERO


def test_two_books_sharing_a_new_author_both_land_with_one_author_row(sbx):
    name = f"SBX Shared Author {sbx.fake_asin()}"
    bodies = [sbx.book(authors=[{"name": name}]) for _ in range(2)]
    results = _parallel(sbx, bodies, dry_run=False)
    assert [r.status_code for r in results] == [200, 200]
    assert sbx.psql(f"SELECT count(*) FROM authors WHERE name = '{name}'") == "1"
    for b in bodies:
        assert sbx.rows_of(b["book"]["asin"])[:2] == (1, 1)
