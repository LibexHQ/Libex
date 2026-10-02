"""
Every `libex-core db` command against a seeded SQLite store: the values it
prints (not that it printed something), the filters, sorts and paging, the
exit status of each way it can end, that a typed value never comes back on
standard error, and that `book sku` is `db sku`.

The hosted /db routes are the reference for these shapes; the comparison with
them is in storage/test_cli_db_hosted_parity.py, which needs Docker. The
catalog is the readers' seeded one, minus the book whose plans are not strings
(see seed()).
"""

# Third party
import pytest

# Local
from libex_core.cli.environment import STORAGE_VARIABLE
from tests.libex_core._cli_lookup_support import PLANTED
from tests.libex_core._db_support import loads


@pytest.fixture
def db(run_cli, seeded_store):
    """ask(*argv) -> (code, parsed stdout or None, stderr)."""

    def ask(*argv):
        result = run_cli(list(argv), env={STORAGE_VARIABLE: seeded_store})
        return result.code, (loads(result.out) if result.out else None), result.err

    return ask


def asins(books):
    return [b["asin"] for b in books]


def ok(answer):
    code, body, err = answer
    assert code == 0, err
    return body


# ============================================================
# ONE BOOK, ITS CHAPTERS, ITS SKU GROUP
# ============================================================

def test_book_prints_the_stored_book_whole(db):
    book = ok(db("db", "book", "B000000001"))
    assert book["asin"] == "B000000001"
    assert book["title"] == "the first quest"
    assert book["rating"] == 4.5
    assert book["lengthMinutes"] == 600
    assert book["plans"] == ["Plus", "Premium"]
    assert book["publisher"] == "Orbit"
    assert book["isbn"] == "9780000000001"
    assert book["audibleExtras"] == {"a": 1, "b": {"c": [1, 2]}}
    assert book["whisperSync"] is True


def test_book_takes_an_asin_in_any_case_and_prints_it_in_the_stored_one(db):
    assert ok(db("db", "book", "b000000001"))["asin"] == "B000000001"


def test_book_for_an_asin_the_store_does_not_hold_is_exit_3_with_fixed_text(db):
    code, body, err = db("db", "book", "B000000099")
    assert (code, body) == (3, None)
    assert err == "libex-core: error: book not in the local store (code: not_in_libex)\n"
    assert "B000000099" not in err


def test_book_with_something_that_is_not_an_asin_is_a_usage_error_that_does_not_echo_it(db):
    code, body, err = db("db", "book", PLANTED)
    assert (code, body) == (2, None)
    assert "must be an ASIN" in err
    assert PLANTED not in err


def test_chapters_prints_the_stored_chapters(db):
    chapters = ok(db("db", "chapters", "B000000001"))
    assert [c["title"] for c in chapters["chapters"]] == ["One"]


def test_chapters_for_a_book_with_none_stored_is_exit_3(db):
    code, body, err = db("db", "chapters", "B000000002")
    assert (code, body) == (3, None)
    assert err == "libex-core: error: no chapter data in the local store for this book (code: not_in_libex)\n"


def test_sku_prints_every_book_of_the_group(db):
    assert sorted(asins(ok(db("db", "sku", "SG1")))) == ["B000000001", "B000000002"]


def test_sku_of_a_group_that_does_not_exist_is_exit_3_and_does_not_echo_it(db):
    code, body, err = db("db", "sku", PLANTED)
    assert (code, body) == (3, None)
    assert PLANTED not in err


def test_book_sku_is_db_sku(db, seeded_store, run_cli):
    via_db = run_cli(["db", "sku", "SG1"], env={STORAGE_VARIABLE: seeded_store})
    via_book = run_cli(["book", "sku", "SG1"], env={STORAGE_VARIABLE: seeded_store})
    assert via_db.code == via_book.code == 0
    assert via_db.out == via_book.out
    assert via_book.out != ""


def test_book_sku_with_nothing_found_ends_as_db_sku_does(run_cli, seeded_store):
    one = run_cli(["db", "sku", "NOPE"], env={STORAGE_VARIABLE: seeded_store})
    two = run_cli(["book", "sku", "NOPE"], env={STORAGE_VARIABLE: seeded_store})
    assert (one.code, one.out, one.err) == (two.code, two.out, two.err)
    assert one.code == 3


# ============================================================
# BOOKS
# ============================================================

def test_books_without_a_filter_or_a_sort_is_a_usage_failure_naming_what_to_give(db):
    code, body, err = db("db", "books")
    assert (code, body) == (2, None)
    assert err == "libex-core: error: give at least one filter or --sort (code: invalid_request)\n"


@pytest.mark.parametrize("argv, expected", [
    (["--title", "quest"], {"B000000001", "B000000002"}),
    (["--title", "QUEST"], {"B000000001", "B000000002"}),
    (["--region", "de"], {"B000000003"}),
    (["--genre", "fantasy"], {"B000000001", "B000000002"}),
    (["--explicit", "true"], {"B000000002"}),
    (["--is-vvab", "true", "--title", "e"], {"B000000002", "B000000005"}),
    (["--language", "german"], {"B000000003"}),
    (["--publisher", "orbit"], {"B000000001"}),
    (["--rating-better-than", "4.5"], {"B000000001", "B000000004"}),
    (["--rating-worse-than", "1.2"], {"B000000006", "B000000007"}),
    (["--shorter-than", "90"], {"B000000002", "B000000005"}),
    (["--plan-name", "Free"], {"B000000008"}),
    (["--title", "100%"], {"B000000008"}),
    # A backslash escapes the next character, as on the hosted route, so this
    # is the pattern ACDC and not the title that holds a literal backslash.
    (["--title", "AC\\DC"], {"B000000006"}),
    (["--title", "émile"], {"B000000005"}),
    (["--isbn", "9780000000001"], {"B000000001"}),
    (["--is-buyable", "false"], {"B000000003"}),
    (["--is-listenable", "false"], {"B000000004"}),
    (["--has-pdf", "true"], {"B000000002"}),
    (["--whisper-sync", "true"], {"B000000001"}),
    (["--content-type", "Podcast"], {"B000000003"}),
])
def test_each_filter_selects_exactly_the_books_that_match(db, argv, expected):
    assert set(asins(ok(db("db", "books", *argv, "--limit", "100")))) == expected


def test_filters_combine_as_and(db):
    assert asins(ok(db("db", "books", "--title", "quest", "--explicit", "true"))) == ["B000000002"]


def test_a_search_that_finds_nothing_is_exit_3_not_an_empty_list(db):
    code, body, err = db("db", "books", "--title", PLANTED)
    assert (code, body) == (3, None)
    assert err == "libex-core: error: no matching books in the local store (code: not_in_libex)\n"


def test_books_sort_by_title_follows_the_binary_order_sqlite_gives(db):
    """The documented difference from the hosted route: Postgres sorts text by
    its locale's collation, so there 'Émile' sits among the Es and case is
    nearly ignored; here the order is by code point, capitals first and
    accented letters last."""
    titles = [b["title"] for b in ok(db("db", "books", "--sort", "title", "--limit", "100"))]
    assert titles[:3] == ["100% pure_gold", "ACDC live", "AC\\DC story"]
    assert titles[-4:] == ["The Second Quest", "the first quest", "third wind", "Émile et les Détectives"]
    assert titles == sorted(titles)


def test_books_sort_descending_reverses_it(db):
    up = asins(ok(db("db", "books", "--sort", "title", "--limit", "100")))
    down = asins(ok(db("db", "books", "--sort", "title", "--order", "desc", "--limit", "100")))
    assert down == up[::-1]


def test_books_sort_by_rating_and_length_is_numeric(db):
    by_rating = ok(db("db", "books", "--sort", "rating", "--order", "desc", "--limit", "3"))
    assert [b["rating"] for b in by_rating] == [5.0, 4.5, 3.0]
    by_length = ok(db("db", "books", "--sort", "lengthMinutes", "--limit", "3"))
    assert [b["lengthMinutes"] for b in by_length] == [45, 90, 100]


def test_paging_slices_one_ordering(db):
    everything = asins(ok(db("db", "books", "--sort", "title", "--limit", "100")))
    first = asins(ok(db("db", "books", "--sort", "title", "--limit", "4", "--page", "1")))
    second = asins(ok(db("db", "books", "--sort", "title", "--limit", "4", "--page", "2")))
    last = asins(ok(db("db", "books", "--sort", "title", "--limit", "4", "--page", "4")))
    assert first == everything[0:4]
    assert second == everything[4:8]
    assert last == everything[12:15]


def test_a_page_past_the_end_finds_nothing(db):
    code, body, _ = db("db", "books", "--sort", "title", "--page", "999")
    assert (code, body) == (3, None)


def test_the_default_page_is_20_books(db):
    assert len(ok(db("db", "books", "--sort", "title"))) == 15
    assert len(ok(db("db", "books", "--sort", "title", "--limit", "5"))) == 5


@pytest.mark.parametrize("argv", [
    ["--limit", "0"], ["--limit", "101"], ["--limit", "x"], ["--page", "0"],
    ["--rating-better-than", "abc"], ["--longer-than", "-1"], ["--explicit", "maybe"],
    ["--region", "xx"], ["--sort", "nonsense"], ["--order", "up"],
])
def test_a_bad_option_is_a_usage_error(db, argv):
    code, body, _ = db("db", "books", "--title", "q", *argv)
    assert (code, body) == (2, None)


# ============================================================
# AUTHORS
# ============================================================

def test_author_merges_the_rows_of_one_asin_the_way_the_hosted_route_does(db):
    author = ok(db("db", "author", "A000000001"))
    assert author["asin"] == "A000000001"
    assert author["description"] == "a much longer description wins"
    assert author["image"] == "http://a/2"
    assert author["region"] == "us"
    assert {g["name"] for g in author["genres"]} == {"Science Fiction & Fantasy", "Epic"}


def test_author_is_looked_up_in_us_unless_told_otherwise(db):
    code, body, err = db("db", "author", "A000000004")
    assert (code, body) == (3, None)
    assert err == "libex-core: error: author not in the local store (code: not_in_libex)\n"
    assert ok(db("db", "author", "A000000004", "--region", "fr"))["name"] == "Émilie Écrivain"


def test_author_books_lists_the_authors_stored_books(db):
    assert sorted(asins(ok(db("db", "author-books", "A000000001")))) == ["B000000001", "B000000002"]


def test_author_books_for_the_other_marketplace_uses_region_for_the_author_and_book_region_for_books(db):
    assert asins(ok(db("db", "author-books", "A000000004", "--region", "fr"))) == ["B000000005"]
    code, body, _ = db("db", "author-books", "A000000004", "--region", "fr", "--book-region", "us")
    assert (code, body) == (3, None)
    code, body, _ = db("db", "author-books", "A000000004")
    assert (code, body) == (3, None)


def test_author_books_takes_the_book_filters_and_sort(db):
    assert asins(ok(db("db", "author-books", "A000000001", "--explicit", "true"))) == ["B000000002"]
    assert asins(ok(db("db", "author-books", "A000000001", "--sort", "rating", "--order", "desc"))) == [
        "B000000001", "B000000002"
    ]


# ============================================================
# SERIES
# ============================================================

def test_series_prints_the_stored_series(db):
    series = ok(db("db", "series", "S000000001"))
    assert (series["asin"], series["name"], series["region"]) == ("S000000001", "Quest Saga", "us")
    assert series["audibleExtras"] == {"x": 1}


def test_series_not_stored_is_exit_3(db):
    code, body, err = db("db", "series", "S000000099")
    assert (code, body) == (3, None)
    assert err == "libex-core: error: series not in the local store (code: not_in_libex)\n"


def test_series_books_come_in_series_order(db):
    assert asins(ok(db("db", "series-books", "S000000001"))) == ["B000000001", "B000000002"]
    assert asins(ok(db("db", "series-books", "S000000003"))) == [
        "B000000008", "B000000012", "B000000009", "B000000003", "B000000010",
        "B000000011", "B000000013", "B000000014", "B000000015",
    ]


def test_series_books_sort_replaces_series_order(db):
    sorted_asins = asins(ok(db("db", "series-books", "S000000003", "--sort", "lengthMinutes", "--order", "desc")))
    assert sorted_asins[0] == "B000000015"
    assert sorted_asins != asins(ok(db("db", "series-books", "S000000003")))


def test_series_books_take_the_book_filters(db):
    assert asins(ok(db("db", "series-books", "S000000003", "--region", "de"))) == ["B000000003"]


# ============================================================
# NARRATORS
# ============================================================

def test_narrators_prints_the_matching_profiles(db):
    found = ok(db("db", "narrators", "nina"))
    assert len(found) == 1
    nina = found[0]
    assert nina["name"] == "Nina Voice"
    assert nina["gender"] == "female"
    assert nina["languages"] == {"English": 5, "Irish": 1}
    assert nina["audiobooksProduced"] == "1 to 10"
    assert nina["attribution"] == "Profile data provided by Wiki, retrieved March 2025"


@pytest.mark.parametrize("argv, expected", [
    (["--gender", "female"], ["Nina Voice"]),
    (["--language", "French"], ["Oscar Reader"]),
    (["--audiobooks-produced", "1-10"], ["Nina Voice"]),
    (["--audiobooks-produced", "more-than-100"], ["Oscar Reader"]),
    (["--source", "wiki"], ["Nina Voice"]),
    (["--cultural-heritage", "irish"], ["Nina Voice"]),
])
def test_each_narrator_filter_selects_the_matching_profiles(db, argv, expected):
    assert [n["name"] for n in ok(db("db", "narrators", "", *argv))] == expected


def test_narrators_sort_and_order(db):
    names = [n["name"] for n in ok(db("db", "narrators", "", "--sort", "name"))]
    assert names == ["Nina Voice", "Oscar Reader", "Zed", "Émile Lecteur"]
    assert [n["name"] for n in ok(db("db", "narrators", "", "--sort", "name", "--order", "desc"))] == names[::-1]


def test_narrators_page_and_limit(db):
    assert [n["name"] for n in ok(db("db", "narrators", "", "--sort", "name", "--limit", "2", "--page", "2"))] == [
        "Zed", "Émile Lecteur"
    ]


def test_narrators_that_match_nothing_are_exit_3_and_the_name_is_not_echoed(db):
    code, body, err = db("db", "narrators", PLANTED)
    assert (code, body) == (3, None)
    assert err == "libex-core: error: no matching narrators in the local store (code: not_in_libex)\n"


def test_narrator_books_matches_the_exact_name(db):
    assert sorted(asins(ok(db("db", "narrator-books", "Nina Voice")))) == ["B000000001", "B000000002"]
    code, body, _ = db("db", "narrator-books", "Nina")
    assert (code, body) == (3, None)


def test_narrator_books_take_filters_and_paging(db):
    assert asins(ok(db("db", "narrator-books", "Nina Voice", "--explicit", "true"))) == ["B000000002"]
    assert len(ok(db("db", "narrator-books", "Nina Voice", "--limit", "1"))) == 1


# ============================================================
# GENRES, PLANS, VVAB
# ============================================================

def test_genres_lists_every_distinct_name(db):
    assert ok(db("db", "genres")) == ["Epic", "Fantasy", "Mystery", "Science Fiction & Fantasy"]


def test_genres_search_narrows_the_list_and_nothing_found_is_exit_3(db):
    assert ok(db("db", "genres", "--search", "FAN")) == ["Fantasy", "Science Fiction & Fantasy"]
    code, body, err = db("db", "genres", "--search", PLANTED)
    assert (code, body) == (3, None)
    assert PLANTED not in err


def test_plans_lists_every_distinct_plan(db):
    assert ok(db("db", "plans")) == ["Free", "Plus", "Premium"]


def test_plan_lists_the_books_under_one_plan_by_whole_name(db):
    assert sorted(asins(ok(db("db", "plan", "Plus")))) == ["B000000001", "B000000002", "B000000006", "B000000007"]
    code, body, err = db("db", "plan", "Plu")
    assert (code, body) == (3, None)
    code, body, err = db("db", "plan", PLANTED)
    assert PLANTED not in err


def test_plan_takes_the_other_filters_but_not_a_second_plan(db):
    assert asins(ok(db("db", "plan", "Plus", "--explicit", "true"))) == ["B000000002"]
    code, _, _ = db("db", "plan", "Plus", "--plan-name", "Free")
    assert code == 2


def test_vvab_lists_the_virtual_voice_books_and_takes_the_other_filters(db):
    assert sorted(asins(ok(db("db", "vvab")))) == ["B000000002", "B000000005"]
    assert asins(ok(db("db", "vvab", "--region", "fr"))) == ["B000000005"]
    code, _, _ = db("db", "vvab", "--is-vvab", "false")
    assert code == 2


# ============================================================
# RELEASE WINDOWS
# ============================================================

def test_new_releases_default_to_30_days_newest_first(db):
    assert asins(ok(db("db", "new-releases"))) == ["B000000008", "B000000001"]


def test_new_releases_widen_with_days_and_leave_out_what_is_not_out_yet(db):
    wide = asins(ok(db("db", "new-releases", "--days", "365", "--limit", "100")))
    assert "B000000003" not in wide and "B000000004" not in wide
    assert "B000000005" not in wide  # no date yet
    assert wide[:2] == ["B000000008", "B000000001"]
    assert len(wide) == 12


def test_coming_soon_defaults_to_30_days_ahead_soonest_first(db):
    assert asins(ok(db("db", "coming-soon"))) == ["B000000003"]
    assert asins(ok(db("db", "coming-soon", "--days", "90"))) == ["B000000003", "B000000004"]


def test_coming_soon_sorts_when_asked(db):
    assert asins(ok(db("db", "coming-soon", "--days", "90", "--sort", "title", "--order", "desc"))) == [
        "B000000003", "B000000004"
    ]


def test_a_window_with_nothing_in_it_is_exit_3(db):
    code, body, _ = db("db", "new-releases", "--days", "30", "--region", "de")
    assert (code, body) == (3, None)


@pytest.mark.parametrize("days", ["0", "7", "31", "1000", "x"])
def test_days_outside_the_offered_windows_is_a_usage_error(db, days):
    code, _, _ = db("db", "new-releases", "--days", days)
    assert code == 2


# ============================================================
# STATS
# ============================================================

def test_stats_counts_the_whole_store(db):
    assert ok(db("db", "stats")) == {
        "books": 15, "authors": 4, "narrators": 4, "series": 4,
        "booksWithChapters": 1, "region": None, "seriesRegionUnknown": None,
    }


def test_stats_scoped_to_a_region_counts_that_region_and_no_series_without_one(db):
    assert ok(db("db", "stats", "--region", "us")) == {
        "books": 12, "authors": 3, "narrators": 4, "series": 4,
        "booksWithChapters": 1, "region": "us", "seriesRegionUnknown": 0,
    }
    assert ok(db("db", "stats", "--region", "de"))["books"] == 1


def test_stats_of_an_upgraded_empty_store_is_zeros_not_a_failure(run_cli, empty_store):
    result = run_cli(["db", "stats"], env={STORAGE_VARIABLE: empty_store})
    assert result.code == 0
    assert loads(result.out) == {
        "books": 0, "authors": 0, "narrators": 0, "series": 0,
        "booksWithChapters": 0, "region": None, "seriesRegionUnknown": None,
    }


# ============================================================
# NOTHING TYPED COMES BACK, AND NOTHING IS WRITTEN
# ============================================================

@pytest.mark.parametrize("argv", [
    ["db", "sku", PLANTED],
    ["db", "books", "--title", PLANTED],
    ["db", "books", "--publisher", PLANTED, "--sort", "title"],
    ["db", "author-books", "A000000001", "--title", PLANTED],
    ["db", "series-books", "S000000001", "--title", PLANTED],
    ["db", "narrators", PLANTED],
    ["db", "narrators", "", "--gender", PLANTED],
    ["db", "narrator-books", PLANTED],
    ["db", "genres", "--search", PLANTED],
    ["db", "plan", PLANTED],
    ["db", "vvab", "--title", PLANTED],
    ["db", "new-releases", "--title", PLANTED],
    ["db", "coming-soon", "--title", PLANTED],
    ["book", "sku", PLANTED],
], ids=lambda argv: " ".join(argv[:2]))
@pytest.mark.parametrize("verbosity", [[], ["-vv"]])
def test_a_value_the_caller_typed_is_not_on_either_stream(run_cli, seeded_store, argv, verbosity):
    result = run_cli([*verbosity, *argv], env={STORAGE_VARIABLE: seeded_store})
    assert result.code == 3
    assert PLANTED not in result.out
    assert PLANTED not in result.err


@pytest.mark.parametrize("command", [
    ["db", "book", "B000000001"], ["db", "books", "--title", "quest"], ["db", "stats"],
    ["db", "plan", "Plus"], ["db", "narrators", "nina"], ["book", "sku", "SG1"],
])
def test_a_read_command_changes_nothing_in_the_store(run_cli, seeded_store, command):
    import hashlib

    def digest():
        import sqlite3

        rows = sqlite3.connect(seeded_store).iterdump()
        return hashlib.sha256("\n".join(rows).encode()).hexdigest()

    before = digest()
    assert run_cli(command, env={STORAGE_VARIABLE: seeded_store}).code == 0
    assert digest() == before


def test_a_read_command_makes_no_request_and_needs_no_proxy(run_cli, seeded_store, monkeypatch):
    import libex_core.cli.session as session

    def refuse(config):
        raise AssertionError("a db command built a client")

    monkeypatch.setattr(session, "build_client", refuse)
    assert run_cli(["db", "books", "--title", "quest"], env={STORAGE_VARIABLE: seeded_store}).code == 0
