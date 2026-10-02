"""
libex_core.lookup authors: the profile and the search by name, and the books
an author is credited with by ASIN and by name. Checks the published models,
the not-found / outage split, what makes an author-books list incomplete and
in which words and order, that a deadline's abandoned requests count as not
fetched, and that filtering and sorting come after the completeness judgement.
Nothing touches a network.
"""

# Standard library
import asyncio
import logging
import time
from unittest.mock import AsyncMock

# Third party
import pytest

# Local
from libex_core.audible.books import UNRELEASED_PLACEHOLDER
from libex_core.exceptions import AudibleAPIException, ErrorCode, NotFoundException
from libex_core.lookup import (
    INCOMPLETE_REASONS,
    AuthorBooks,
    get_author,
    get_author_books,
    get_author_books_by_name,
    search_authors,
)
from libex_core.lookup.books import hydrate_books
from libex_core.models import AuthorResponse, BookResponse
from tests.libex_core._lookup_support import (
    AUTHOR,
    AUTHOR_NAME,
    NO_NAME_AUTHOR,
    asins,
    fake_get,
    outage_get,
    product,
)

WALK = "libex_core.lookup.author_books._walk_author_books"
BUDGET = "libex_core.lookup.author_books.AUTHOR_BOOKS_TIME_BUDGET_SECONDS"


def _hydrating_get(stubs=(), placeholders=(), fail=(), hang=(), calls=None):
    """A get that serves batch requests for the hydration that follows a walk:
    ASINs in `stubs` come back as hollow stubs, `placeholders` as placeholder
    records; a request containing any of `fail` raises, and one containing
    any of `hang` never answers."""
    async def get(region, path, params=None, extra_headers=None):
        if calls is not None:
            calls.append((region, path, params))
        wanted = params["asins"].split(",")
        if any(a in fail for a in wanted):
            raise AudibleAPIException("boom", upstream_status=503)
        if any(a in hang for a in wanted):
            await asyncio.sleep(20)
        out = []
        for a in wanted:
            if a in stubs:
                out.append({"asin": a})
            elif a in placeholders:
                out.append(product(a, publication_datetime=UNRELEASED_PLACEHOLDER))
            else:
                out.append(product(a))
        return {"products": out}
    return get


@pytest.fixture
def walk(monkeypatch):
    """Sets what discovery found on the ASIN route, so a test can be about
    what hydration and the shaping made of it."""
    def set_walk(found, complete=True):
        monkeypatch.setattr(WALK, AsyncMock(return_value=(list(found), complete)))
    return set_walk


# Section: profile


async def test_get_author_returns_the_published_model():
    result = await get_author(fake_get, AUTHOR)
    assert isinstance(result, AuthorResponse)
    assert result.asin == AUTHOR
    assert result.name == AUTHOR_NAME


async def test_get_author_uppercases_the_asin_it_asks_for():
    seen = []

    async def get(region, path, params=None, extra_headers=None):
        seen.append(path)
        return await fake_get(region, path, params, extra_headers)

    await get_author(get, AUTHOR.lower())
    assert seen and all(path.endswith(AUTHOR) for path in seen)


@pytest.mark.parametrize("data", [
    {"contributor": {"name": None, "bio": "x"}},
    {"contributor": {}},
    {},
])
async def test_get_author_with_no_name_on_record_is_not_found(data):
    async def get(region, path, params=None, extra_headers=None):
        return data

    with pytest.raises(NotFoundException):
        await get_author(get, AUTHOR)


async def test_get_author_unknown_to_audible_is_not_found_and_outage_is_not():
    with pytest.raises(NotFoundException):
        await get_author(fake_get, NO_NAME_AUTHOR)
    with pytest.raises(AudibleAPIException) as caught:
        await get_author(outage_get, AUTHOR)
    assert caught.value.upstream_status == 503


# Section: search by name


async def test_search_authors_returns_the_profiles_in_the_order_suggested():
    async def get(region, path, params=None, extra_headers=None):
        if "contributors/" in path:
            return {"contributor": {"name": path[-2:], "bio": ""}}
        return {"model": {"items": [
            {"view": {"template": "AuthorItemV2"},
             "model": {"person_metadata": {"asin": a}}}
            for a in ("B0AUTHORZZ", "B0AUTHORAA", "B0AUTHORMM")
        ]}}

    result = await search_authors(get, "anything")
    assert [a.name for a in result] == ["ZZ", "AA", "MM"]
    assert all(isinstance(a, AuthorResponse) for a in result)


async def test_search_authors_leaves_out_an_unknown_author_and_serves_the_rest():
    result = await search_authors(fake_get, "jane")
    assert [a.asin for a in result] == [AUTHOR]


async def test_search_authors_leaves_out_an_author_that_failed_and_warns(caplog):
    async def get(region, path, params=None, extra_headers=None):
        if path.endswith(NO_NAME_AUTHOR):
            raise AudibleAPIException("boom", upstream_status=503)
        return await fake_get(region, path, params, extra_headers)

    with caplog.at_level(logging.WARNING, logger="libex"):
        result = await search_authors(get, "jane")
    assert [a.asin for a in result] == [AUTHOR]
    assert any("skipping" in r.getMessage() for r in caplog.records)


async def test_search_authors_with_nothing_suggested_is_not_found():
    async def get(region, path, params=None, extra_headers=None):
        return {"model": {"items": []}}

    with pytest.raises(NotFoundException):
        await search_authors(get, "jane")


async def test_search_authors_every_candidate_failing_is_an_outage_not_an_absence():
    async def get(region, path, params=None, extra_headers=None):
        if "contributors/" in path:
            raise AudibleAPIException("boom", upstream_status=503)
        return await fake_get(region, path, params, extra_headers)

    with pytest.raises(AudibleAPIException):
        await search_authors(get, "jane")


async def test_search_authors_suggestions_failing_is_an_outage():
    with pytest.raises(AudibleAPIException):
        await search_authors(outage_get, "jane")


# Section: books by ASIN, what makes the list incomplete


async def test_a_whole_list_is_complete_with_no_reasons(walk):
    walk(asins(3))
    result = await get_author_books(_hydrating_get(), AUTHOR)
    assert isinstance(result, AuthorBooks)
    assert all(isinstance(b, BookResponse) for b in result.books)
    assert len(result.books) == 3
    assert result.complete is True
    assert result.incomplete_reasons == ()


async def test_the_reason_words_are_the_hosted_header_words():
    assert INCOMPLETE_REASONS == (
        "discovery-incomplete",
        "hydration-deadline",
        "hydration-failed",
        "hydration-not-found",
    )


async def test_unfinished_discovery_is_discovery_incomplete(walk):
    walk(asins(3), complete=False)
    result = await get_author_books(_hydrating_get(), AUTHOR)
    assert result.complete is False
    assert result.incomplete_reasons == ("discovery-incomplete",)
    assert len(result.books) == 3, "an incomplete list is still returned"


async def test_a_book_audible_has_no_record_of_is_hydration_not_found(walk):
    found = asins(3)
    walk(found)
    result = await get_author_books(_hydrating_get(stubs={found[1]}), AUTHOR)
    assert result.complete is False
    assert result.incomplete_reasons == ("hydration-not-found",)
    assert len(result.books) == 2


async def test_a_placeholder_record_is_also_hydration_not_found(walk):
    found = asins(3)
    walk(found)
    result = await get_author_books(_hydrating_get(placeholders={found[0]}), AUTHOR)
    assert result.incomplete_reasons == ("hydration-not-found",)
    assert len(result.books) == 2


async def test_a_failed_request_is_hydration_failed_and_the_rest_is_served(walk):
    found = asins(120)
    walk(found)
    result = await get_author_books(_hydrating_get(fail={found[60]}), AUTHOR)
    assert result.complete is False
    assert result.incomplete_reasons == ("hydration-failed",)
    assert len(result.books) == 70


async def test_every_request_failing_is_an_outage_not_an_empty_list(walk):
    found = asins(3)
    walk(found)
    with pytest.raises(AudibleAPIException):
        await get_author_books(_hydrating_get(fail=set(found)), AUTHOR)


async def test_a_request_cut_off_by_the_deadline_is_hydration_deadline(walk, monkeypatch):
    found = asins(120)
    walk(found)
    monkeypatch.setattr(BUDGET, 0.2)
    result = await get_author_books(_hydrating_get(hang={found[60]}), AUTHOR)
    assert result.complete is False
    assert result.incomplete_reasons == ("hydration-deadline",), (
        "an abandoned request is the deadline's doing, not a failure"
    )
    assert len(result.books) == 70


async def test_a_failure_and_a_deadline_are_both_reported(walk, monkeypatch):
    found = asins(150)
    walk(found)
    monkeypatch.setattr(BUDGET, 0.2)
    get = _hydrating_get(fail={found[60]}, hang={found[120]})
    result = await get_author_books(get, AUTHOR)
    assert result.incomplete_reasons == ("hydration-deadline", "hydration-failed")
    assert len(result.books) == 50


async def test_all_four_reasons_come_in_the_published_order(walk, monkeypatch):
    found = asins(150)
    walk(found, complete=False)
    monkeypatch.setattr(BUDGET, 0.2)
    get = _hydrating_get(stubs={found[3]}, fail={found[60]}, hang={found[120]})
    result = await get_author_books(get, AUTHOR)
    assert result.complete is False
    assert result.incomplete_reasons == INCOMPLETE_REASONS
    assert len(result.books) == 49


async def test_the_deadline_is_absolute_and_shared_with_discovery(monkeypatch):
    """Discovery is handed the deadline hydration is held to, so the two
    cannot add up past one budget."""
    captured = {}

    async def fake_walk(get, asin, region, deadline):
        captured["deadline"] = deadline
        return asins(2), True

    monkeypatch.setattr(WALK, fake_walk)
    monkeypatch.setattr(BUDGET, 7.0)
    await get_author_books(_hydrating_get(), AUTHOR)
    assert 0 < captured["deadline"] - time.monotonic() <= 7.0


# Section: hydrate_books deadline


async def test_hydrate_books_counts_a_deadline_abandoned_request_as_not_fetched():
    found = asins(120)
    hydration = await hydrate_books(
        _hydrating_get(hang={found[60]}), found, "us", deadline=time.monotonic() + 0.2
    )
    abandoned = found[50:100]
    assert hydration.deadline_abandoned == abandoned
    assert hydration.not_fetched == abandoned
    assert len(hydration.books) == 70


async def test_hydrate_books_a_failure_is_not_fetched_but_not_abandoned():
    found = asins(120)
    hydration = await hydrate_books(_hydrating_get(fail={found[60]}), found, "us")
    assert hydration.not_fetched == found[50:100]
    assert hydration.deadline_abandoned == []


async def test_hydrate_books_with_no_deadline_never_abandons():
    found = asins(120)
    hydration = await hydrate_books(_hydrating_get(), found, "us")
    assert hydration.deadline_abandoned == []
    assert hydration.not_fetched == []


async def test_hydrate_books_an_already_passed_deadline_serves_nothing_and_raises():
    with pytest.raises(AudibleAPIException):
        await hydrate_books(
            _hydrating_get(hang=set(asins(3))), asins(3), "us",
            deadline=time.monotonic() - 1,
        )


async def test_hydrate_books_takes_deadline_and_high_concurrency_by_keyword_only():
    with pytest.raises(TypeError):
        await hydrate_books(_hydrating_get(), asins(2), "us", 1.0)  # type: ignore[misc]
    hydration = await hydrate_books(_hydrating_get(), asins(2), "us", high_concurrency=True)
    assert len(hydration.books) == 2


# Section: completeness is judged before filtering


async def test_a_filter_that_leaves_nothing_does_not_make_the_list_incomplete(walk):
    walk(asins(3))
    result = await get_author_books(
        _hydrating_get(), AUTHOR, filters={"language": "klingon"}
    )
    assert result.books == []
    assert result.complete is True
    assert result.incomplete_reasons == ()


async def test_a_filter_does_not_hide_a_real_shortfall(walk):
    found = asins(3)
    walk(found, complete=False)
    result = await get_author_books(
        _hydrating_get(stubs={found[0]}), AUTHOR, filters={"language": "klingon"}
    )
    assert result.books == []
    assert result.incomplete_reasons == ("discovery-incomplete", "hydration-not-found")


async def test_filters_and_sort_shape_the_books_after_the_judgement(walk):
    found = asins(4)
    walk(found)

    async def get(region, path, params=None, extra_headers=None):
        return {"products": [
            product(a, length=100 * (i + 1)) for i, a in enumerate(params["asins"].split(","))
        ]}

    result = await get_author_books(
        get, AUTHOR, filters={"longer_than": 150}, sort="lengthMinutes", order="desc"
    )
    assert [b.lengthMinutes for b in result.books] == [400, 300, 200]
    assert result.complete is True


# Section: books by ASIN, discovery


async def test_a_value_that_is_not_an_asin_is_refused_without_a_request():
    get = AsyncMock()
    with pytest.raises(NotFoundException) as caught:
        await get_author_books(get, "not an asin")
    assert caught.value.code == ErrorCode.INVALID_REQUEST
    get.assert_not_called()


async def test_with_no_source_answering_it_is_an_outage():
    with pytest.raises(AudibleAPIException):
        await get_author_books(outage_get, AUTHOR)


async def test_a_null_contributor_name_is_no_name_not_a_failure():
    """Audible confirming the author has no name means there is nothing to
    search the catalog with, which is not the same as name resolution failing:
    the first finds no books, the second establishes nothing."""
    calls = []

    async def no_name(region, path, params=None, extra_headers=None):
        calls.append(path)
        if "contributors/" in path:
            return {"contributor": {"name": None}}
        return {"nothing": True}

    with pytest.raises(NotFoundException):
        await get_author_books(no_name, AUTHOR)
    assert not any(p == "/1.0/catalog/products" for p in calls), (
        "no name, so no catalog search"
    )

    async def name_fails(region, path, params=None, extra_headers=None):
        if "contributors/" in path:
            raise AudibleAPIException("boom", upstream_status=503)
        return {"nothing": True}

    with pytest.raises(AudibleAPIException):
        await get_author_books(name_fails, AUTHOR)


async def test_the_catalog_is_searched_on_the_resolved_name_and_the_books_unioned():
    result = await get_author_books(fake_get, AUTHOR)
    assert [b.asin for b in result.books] == [
        "B0SCR00000", "B0SCR00001", "B0SCR00002", "B0SCR00003"
    ]


# Section: books by name


def _name_get(total=60, fail_page=None, fail=(), stubs=()):
    """A get for the by-name route: `total` matching books over 50-wide pages,
    page `fail_page` failing, and the batch requests that hydrate them."""
    names = [f"B0NAM{i:05d}" for i in range(total)]

    async def get(region, path, params=None, extra_headers=None):
        if "author" in params:
            page = params["page"]
            if page == fail_page:
                raise AudibleAPIException("boom", upstream_status=503)
            chunk = names[page * 50:(page + 1) * 50]
            return {"total_results": total, "products": [product(a) for a in chunk]}
        wanted = params["asins"].split(",")
        if any(a in fail for a in wanted):
            raise AudibleAPIException("boom", upstream_status=503)
        return {"products": [
            {"asin": a} if a in stubs else product(a) for a in wanted
        ]}

    get.names = names
    return get


async def test_by_name_a_confirmed_end_is_complete():
    result = await get_author_books_by_name(_name_get(total=60), AUTHOR_NAME)
    assert result.complete is True
    assert result.incomplete_reasons == ()
    assert len(result.books) == 60


async def test_by_name_a_later_page_failing_is_discovery_incomplete_with_what_was_gathered():
    result = await get_author_books_by_name(_name_get(total=60, fail_page=1), AUTHOR_NAME)
    assert result.complete is False
    assert result.incomplete_reasons == ("discovery-incomplete",)
    assert len(result.books) == 50


async def test_by_name_the_first_page_failing_is_an_outage_not_an_empty_author():
    with pytest.raises(AudibleAPIException):
        await get_author_books_by_name(_name_get(fail_page=0), AUTHOR_NAME)


async def test_by_name_no_exact_match_is_not_found():
    with pytest.raises(NotFoundException):
        await get_author_books_by_name(_name_get(total=60), "Somebody Else")


async def test_by_name_matches_the_name_exactly_ignoring_case():
    result = await get_author_books_by_name(_name_get(total=3), AUTHOR_NAME.upper())
    assert len(result.books) == 3


async def test_by_name_failed_and_missing_books_each_get_their_reason():
    names = [f"B0NAM{i:05d}" for i in range(120)]
    get = _name_get(total=120, fail={names[60]}, stubs={names[3]})
    result = await get_author_books_by_name(get, AUTHOR_NAME)
    assert result.complete is False
    assert result.incomplete_reasons == ("hydration-failed", "hydration-not-found")
    assert len(result.books) == 69


async def test_by_name_every_hydration_request_failing_is_an_outage():
    names = [f"B0NAM{i:05d}" for i in range(3)]
    with pytest.raises(AudibleAPIException):
        await get_author_books_by_name(_name_get(total=3, fail=set(names)), AUTHOR_NAME)


async def test_by_name_filters_and_sort_come_after_the_judgement():
    names = [f"B0NAM{i:05d}" for i in range(120)]
    get = _name_get(total=120, stubs={names[3]})
    result = await get_author_books_by_name(
        get, AUTHOR_NAME, filters={"language": "klingon"}
    )
    assert result.books == []
    assert result.incomplete_reasons == ("hydration-not-found",)
