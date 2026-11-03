"""
What the endpoint does to the database: dry run and commit, the cache, the
stored and entity refusals, the induced failures, and a live GET staying 404.
"""

# Standard library
import json
import time

# Third party
import pytest

ZERO = (0, 0, 0, 0, 0, 0)


def _full_body(sbx, asin=None, **over):
    """A book with every link and chapters, named uniquely to its ASIN."""
    asin = asin or sbx.fake_asin()
    body = sbx.book(
        asin=asin,
        narrators=[{"name": f"SBX Narrator {asin}"}],
        series=[{"asin": sbx.fake_asin(), "name": f"SBX Series {asin}", "position": "1"}],
        genres=[{"asin": "SBXG" + asin[5:], "name": f"SBX Genre {asin}", "type": "Genres"}],
        **over,
    )
    body["chapters"] = sbx.chapters()
    return body


def test_dry_run_stores_nothing_and_commit_stores_everything(sbx):
    body = _full_body(sbx)
    asin = body["book"]["asin"]
    before = sbx.state([asin])

    dry = sbx.post(body)
    assert dry.status_code == 200
    assert dry.json()["dryRun"] is True and dry.json()["committed"] is False
    assert dry.json()["book"]["manualEntry"] is True
    assert sbx.state([asin]) == before
    assert sbx.rows_of(asin) == ZERO
    assert sbx.db_book(asin).status_code == 404  # a dry run is not served

    commit = sbx.post(body, dry_run=False)
    assert commit.status_code == 200
    assert commit.json()["committed"] is True
    flags = sbx.psql(
        "SELECT manual_entry_at IS NOT NULL, confirmed_at IS NULL, chapters_checked_at IS NOT NULL, "
        f"chapters_confirmed_at IS NULL FROM books WHERE asin = '{asin}' AND region = 'us'"
    )
    assert flags == "t|t|t|t"
    assert sbx.rows_of(asin) == (1, 1, 1, 1, 1, 1)

    served = sbx.db_book(asin)
    assert served.status_code == 200
    assert served.json()["manualEntry"] is True
    assert set(served.json()) == set(dry.json()["book"]) == set(commit.json()["book"])
    chapters = sbx.http.get(f"/db/book/{asin}/chapters", params={"region": "us"})
    assert chapters.status_code == 200
    assert [c["title"] for c in chapters.json()["chapters"]] == ["One", "Two"]
    # A different region's record is a different book.
    assert sbx.db_book(asin, "uk").status_code == 404


def test_commit_clears_exactly_the_cache_entries_derived_from_the_book(sbx):
    asin, author, series = sbx.fake_asin(), sbx.fake_asin(), sbx.fake_asin()
    body = sbx.book(
        asin=asin,
        authors=[{"name": f"SBX Author {asin}", "asin": author}],
        series=[{"asin": series, "name": f"SBX Series {asin}"}],
    )
    # The keys come from the image's own functions, so a change to their format is caught here.
    script = (
        "import json, sys; from app.services.cache import manager as c; a, u, s = sys.argv[1:4]; "
        "print(json.dumps([c.book_key(a, 'us'), c.chapters_key(a, 'us'), "
        "c.author_books_key(u, 'us'), c.series_books_key(s, 'us')]))"
    )
    derived = json.loads(sbx.compose("exec", "-T", "api", "python", "-c", script, asin, author, series).stdout)
    unrelated = f"unrelated:us:{asin}"
    for key in [*derived, unrelated]:
        sbx.psql(
            "INSERT INTO cache (key, value, created_at, expires_at) "
            f"VALUES ('{key}', '{{\"sbx\": 1}}'::jsonb, now(), now() + interval '1 day')"
        )

    def present():
        quoted = ",".join(f"'{k}'" for k in [*derived, unrelated])
        return set(sbx.psql(f"SELECT key FROM cache WHERE key IN ({quoted})").split())

    assert sbx.post(body).status_code == 200
    assert present() == {*derived, unrelated}  # a dry run leaves every key
    assert sbx.post(body, dry_run=False).status_code == 200
    assert present() == {unrelated}  # a commit removes the four and only the four


def test_the_same_body_twice_is_200_then_409_stored(sbx):
    body = _full_body(sbx)
    asin = body["book"]["asin"]
    assert sbx.post(body, dry_run=False).status_code == 200
    state = sbx.table_state("books", f"x.asin = '{asin}'")
    for dry in (False, True):
        again = sbx.post(body, dry_run=dry)
        assert again.status_code == 409
        assert again.json() == {"reason": "stored"}
    assert sbx.rows_of(asin) == (1, 1, 1, 1, 1, 1)
    assert sbx.table_state("books", f"x.asin = '{asin}'") == state


def test_entity_conflicts_are_refused_and_shared_rows_stay_identical(sbx):
    tag = sbx.fake_asin()
    author, asinless = sbx.fake_asin(), f"SBX Asinless {tag}"
    series, genre = sbx.fake_asin(), "SBXG" + tag[5:]
    au_name, ser_name, gen_name, narr = f"SBX Au {tag}", f"SBX Ser {tag}", f"SBX Gen {tag}", f"SBX Narr {tag}"
    shared = dict(authors=[author], author_names=[asinless], series=[series], genres=[genre], narrators=[narr])

    base = sbx.book(
        authors=[{"name": au_name, "asin": author, "description": "SBX author text"}, {"name": asinless}],
        narrators=[{"name": narr}],
        series=[{"asin": series, "name": ser_name, "position": "1"}],
        genres=[{"asin": genre, "name": gen_name, "type": "Genres"}],
    )
    assert sbx.post(base, dry_run=False).status_code == 200
    before = sbx.entity_state(**shared)
    assert before["authors"].startswith("1|")  # the guard below compares real rows

    refused = {
        "author_differs": ({"authors": [{"name": "Another Name", "asin": author}]},
                           ["book", "authors", 0, "name"]),
        "series_differs": ({"series": [{"asin": series, "name": "Another Series"}]},
                           ["book", "series", 0, "name"]),
        "genre_name_differs": ({"genres": [{"asin": genre, "name": "Another Genre", "type": "Genres"}]},
                               ["book", "genres", 0]),
        "genre_type_differs": ({"genres": [{"asin": genre, "name": gen_name, "type": "Tags"}]},
                               ["book", "genres", 0]),
        "asinless_upgrade": ({"authors": [{"name": asinless, "asin": sbx.fake_asin()}]},
                             ["book", "authors", 0, "asin"]),
    }
    for name, (over, loc) in refused.items():
        body = sbx.book(**over)
        asin = body["book"]["asin"]
        resp = sbx.post(body, dry_run=False)
        assert resp.status_code == 422, name
        errors = resp.json()["errors"]
        assert [e["loc"] for e in errors] == [loc], name
        assert errors[0]["type"].endswith(("_differs", "_asinless_row")), name
        assert sbx.rows_of(asin) == ZERO, name
        assert sbx.entity_state(**shared) == before, name

    # The same entities, named exactly, link and report "existing".
    good = sbx.book(
        authors=[{"name": au_name, "asin": author}, {"name": asinless}],
        narrators=[{"name": narr}],
        series=[{"asin": series, "name": ser_name, "position": "2"}],
        genres=[{"asin": genre, "name": gen_name, "type": "Genres"}],
    )
    dry = sbx.post(good)
    assert dry.status_code == 200
    entities = dry.json()["entities"]
    assert {e["status"] for kind in entities.values() for e in kind} == {"existing"}
    assert sbx.post(good, dry_run=False).status_code == 200
    assert sbx.entity_state(**shared) == before
    assert sbx.rows_of(good["book"]["asin"]) == (1, 2, 1, 1, 1, 0)


def test_an_induced_database_failure_is_a_generic_500_with_nothing_stored(sbx):
    body = sbx.book(asin="B0SBXFAIL0")
    with sbx.trigger("sbx_fail", "B0SBXFAIL0", sbx.FAIL_BODY):
        resp = sbx.post(body, dry_run=False)
        assert resp.status_code == 500
        assert resp.json() == {"error": "Internal Server Error"}
        assert sbx.rows_of("B0SBXFAIL0") == ZERO
    time.sleep(1)
    logs = sbx.app_logs()
    failed = [line for line in logs.splitlines() if "Book entry failed" in line]
    assert failed and all("error_type" in line for line in failed)
    assert "sbx forced failure" not in logs  # the exception text is never logged
    # With the trigger gone, the same body goes in.
    assert sbx.post(body, dry_run=False).status_code == 200


@pytest.mark.timeout(900)
@pytest.mark.parametrize("proxy", ["http://127.0.0.1:9", "http://192.0.2.1:8888"], ids=["refused", "blackhole"])
def test_audible_outage_is_503_and_stores_nothing(sbx, tmp_path, capsys, proxy):
    body = sbx.book()
    asin = body["book"]["asin"]
    with sbx.api_env(tmp_path, LIBEX_SANDBOX_PROXY=proxy):
        start = time.monotonic()
        resp = sbx.post(body, dry_run=False, audible=False)
        elapsed = time.monotonic() - start
    with capsys.disabled():
        print(f"\n503 after {elapsed:.1f}s via {'refused' if '127' in proxy else 'blackhole'} proxy")
    assert resp.status_code == 503
    assert resp.json() == {"reason": "audible_unavailable"}
    assert sbx.rows_of(asin) == ZERO
    # Reachable again, the same body is accepted: the 503 was the outage, not the body.
    assert sbx.post(body).status_code == 200


def test_a_live_books_get_stays_404_for_a_manual_entry(sbx):
    body = sbx.book()
    asin = body["book"]["asin"]
    before = sbx.get_live(asin)
    assert before.status_code == 404
    assert sbx.post(body, dry_run=False).status_code == 200
    row = sbx.table_state("books", f"x.asin = '{asin}'")
    after = sbx.get_live(asin)
    assert after.status_code == 404
    assert after.content == before.content
    assert sbx.table_state("books", f"x.asin = '{asin}'") == row
    assert sbx.db_book(asin).json()["manualEntry"] is True
