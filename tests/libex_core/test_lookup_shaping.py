"""
libex_core.lookup shaping: check_shaping refuses every bad filter, sort and
order before any request and never repeats what it was given, shape_books
filters then sorts as the hosted routes do, and the books-shaping functions
(bulk, series, author, release windows) apply it where the routes do. Nothing
touches a network.
"""

# Standard library
from unittest.mock import AsyncMock

# Third party
import pytest

# Local
from libex_core.audible.books import UNRELEASED_PLACEHOLDER
from libex_core.exceptions import AudibleAPIException
from libex_core.lookup import (
    coming_soon,
    get_author_books,
    get_author_books_by_name,
    get_books,
    get_series_books,
    new_releases,
)
from libex_core.lookup._shaping import check_shaping, shape_books
from libex_core.shaping import BOOK_FILTER_FIELDS, BOOK_FILTER_SPECS, BOOK_SORT_FIELDS
from tests.libex_core._lookup_support import (
    AUTHOR,
    AUTHOR_NAME,
    PLANTED,
    SERIES,
    asins,
    fake_get,
    product,
)

# A value of the right type for each filter, by the type the spec declares.
GOOD = {str: "x", bool: True, int: 5, float: 4.5}

# Section: check_shaping accepts


def test_nothing_to_check_passes():
    check_shaping(None, None, "asc")
    check_shaping({}, None, "desc")


@pytest.mark.parametrize("spec", BOOK_FILTER_SPECS, ids=lambda s: s.name)
def test_each_filter_accepts_a_value_of_its_type(spec):
    check_shaping({spec.name: GOOD[spec.type]}, None, "asc")


def test_a_none_value_is_ignored_as_on_the_routes():
    check_shaping({name: None for name in BOOK_FILTER_FIELDS}, None, "asc")


def test_an_int_stands_in_for_a_float_filter():
    check_shaping({"rating_better_than": 4}, None, "asc")


@pytest.mark.parametrize("sort", sorted(BOOK_SORT_FIELDS))
def test_every_published_sort_field_is_accepted(sort):
    check_shaping(None, sort, "asc")


# Section: check_shaping refuses

BAD_INPUTS = [
    pytest.param({"nonsense": 1}, None, "asc", id="unknown filter"),
    pytest.param({"language": 5}, None, "asc", id="str given int"),
    pytest.param({"language": None, "nonsense": "x"}, None, "asc", id="unknown filter beside a none"),
    pytest.param({"explicit": "true"}, None, "asc", id="bool given str"),
    pytest.param({"explicit": 1}, None, "asc", id="bool given int"),
    pytest.param({"rating_better_than": "4"}, None, "asc", id="float given str"),
    pytest.param({"rating_worse_than": True}, None, "asc", id="float given bool"),
    pytest.param({"longer_than": 1.5}, None, "asc", id="int given float"),
    pytest.param({"shorter_than": True}, None, "asc", id="int given bool"),
    pytest.param({"longer_than": "10"}, None, "asc", id="int given str"),
    pytest.param(["language"], None, "asc", id="filters not a dict"),
    pytest.param(None, "nonsense", "asc", id="unknown sort"),
    pytest.param(None, "", "asc", id="empty sort"),
    pytest.param(None, None, "up", id="unknown order"),
    pytest.param(None, None, "ASC", id="order is case sensitive"),
    pytest.param(None, None, None, id="order none"),
]


@pytest.mark.parametrize("filters,sort,order", BAD_INPUTS)
def test_check_shaping_refuses(filters, sort, order):
    with pytest.raises(ValueError):
        check_shaping(filters, sort, order)


@pytest.mark.parametrize("filters,sort,order", [
    ({PLANTED: 1}, None, "asc"),
    ({"longer_than": PLANTED}, None, "asc"),
    ({"explicit": PLANTED}, None, "asc"),
    (None, PLANTED, "asc"),
    (None, None, PLANTED),
])
def test_check_shaping_never_repeats_what_it_was_given(filters, sort, order):
    with pytest.raises(ValueError) as caught:
        check_shaping(filters, sort, order)
    assert PLANTED not in str(caught.value)
    assert PLANTED not in repr(caught.value.args)


# Section: shape_books


def _b(asin, **fields):
    return {"asin": asin, **fields}


def test_shape_books_filters_then_sorts():
    books = [
        _b("A", lengthMinutes=300), _b("B", lengthMinutes=100),
        _b("C", lengthMinutes=200), _b("D", lengthMinutes=250),
    ]
    out = shape_books(books, {"longer_than": 150}, "lengthMinutes", "desc")
    assert [b["asin"] for b in out] == ["A", "D", "C"]


def test_shape_books_with_no_filter_and_no_sort_keeps_the_order_it_came_in():
    books = [_b("Z", lengthMinutes=1), _b("A", lengthMinutes=9), _b("M", lengthMinutes=5)]
    assert shape_books(books, None, None, "asc") == books
    assert shape_books(books, {}, None, "desc") == books


def test_shape_books_does_not_mutate_what_it_was_given():
    books = [_b("A", lengthMinutes=2), _b("B", lengthMinutes=1)]
    shape_books(books, None, "lengthMinutes", "asc")
    assert [b["asin"] for b in books] == ["A", "B"]


# Section: every shaped function refuses before any request


def _calls():
    return {
        "get_books": lambda get, **kw: get_books(get, asins(2), **kw),
        "get_series_books": lambda get, **kw: get_series_books(get, SERIES, **kw),
        "get_author_books": lambda get, **kw: get_author_books(get, AUTHOR, **kw),
        "get_author_books_by_name": lambda get, **kw: get_author_books_by_name(
            get, AUTHOR_NAME, **kw),
        "new_releases": lambda get, **kw: new_releases(get, 30, **kw),
        "coming_soon": lambda get, **kw: coming_soon(get, 30, **kw),
    }


CALLS = _calls()


@pytest.mark.parametrize("fn", CALLS)
@pytest.mark.parametrize("filters,sort,order", BAD_INPUTS)
async def test_a_bad_shaping_input_is_refused_before_any_request(fn, filters, sort, order):
    get = AsyncMock()
    with pytest.raises(ValueError):
        await CALLS[fn](get, filters=filters, sort=sort, order=order)
    get.assert_not_called()


@pytest.mark.parametrize("fn", CALLS)
async def test_a_refusal_does_not_repeat_the_callers_input(fn, caplog):
    get = AsyncMock()
    for kwargs in ({"filters": {PLANTED: 1}}, {"sort": PLANTED}, {"order": PLANTED},
                   {"filters": {"longer_than": PLANTED}}):
        with pytest.raises(ValueError) as caught:
            await CALLS[fn](get, **kwargs)
        assert PLANTED not in str(caught.value)
    assert PLANTED not in caplog.text


# Section: bulk lookup, shaping touches the books only


def _bulk_get(known_len, stubs=(), placeholders=(), fail=()):
    async def get(region, path, params=None, extra_headers=None):
        wanted = params["asins"].split(",")
        if any(a in fail for a in wanted):
            raise AudibleAPIException("boom", upstream_status=503)
        out = []
        for a in wanted:
            if a in stubs:
                out.append({"asin": a})
            elif a in placeholders:
                out.append(product(a, publication_datetime=UNRELEASED_PLACEHOLDER))
            else:
                out.append(product(a, length=known_len(a)))
        return {"products": out}
    return get


BULK = asins(60)


def _bulk_get_for_buckets():
    return _bulk_get(
        lambda a: 100 * (int(a[-5:]) % 7 + 1),
        stubs={BULK[50]}, placeholders={BULK[51]}, fail={BULK[0]},
    )


@pytest.mark.parametrize("shaping", [
    {"filters": {"language": "klingon"}},
    {"filters": {"longer_than": 450}},
    {"sort": "lengthMinutes", "order": "desc"},
    {"filters": {"longer_than": 250}, "sort": "lengthMinutes"},
])
async def test_shaping_does_not_disturb_the_three_disjoint_lists(shaping):
    plain = await get_books(_bulk_get_for_buckets(), BULK)
    shaped = await get_books(_bulk_get_for_buckets(), BULK, **shaping)

    assert plain.notFound == [BULK[50]]
    assert plain.placeholderRecords == [BULK[51]]
    assert plain.notFetched == BULK[:50]
    assert len(plain.books) == 8

    assert shaped.notFound == plain.notFound
    assert shaped.placeholderRecords == plain.placeholderRecords
    assert shaped.notFetched == plain.notFetched


async def test_a_found_book_that_was_filtered_out_is_reported_nowhere():
    plain = await get_books(_bulk_get_for_buckets(), BULK)
    shaped = await get_books(
        _bulk_get_for_buckets(), BULK, filters={"longer_than": 450}
    )
    dropped = {b.asin for b in plain.books} - {b.asin for b in shaped.books}
    assert dropped
    reported = set(shaped.notFound) | set(shaped.placeholderRecords) | set(shaped.notFetched)
    assert not dropped & reported


async def test_bulk_shaping_filters_and_sorts_the_books():
    result = await get_books(
        _bulk_get_for_buckets(), BULK, filters={"longer_than": 250},
        sort="lengthMinutes", order="desc",
    )
    lengths = [b.lengthMinutes for b in result.books]
    assert lengths and lengths == sorted(lengths, reverse=True)
    assert min(lengths) >= 250


# Section: series books, a sort overrides series order


async def test_series_books_keep_series_order_without_a_sort():
    result = await get_series_books(fake_get, SERIES)
    assert [b.asin for b in result] == [
        "B0SCR00000", "B0SCR00001", "B0SCR00002", "B0SCR00003"
    ]


async def test_a_sort_overrides_series_order():
    result = await get_series_books(
        fake_get, SERIES, sort="lengthMinutes", order="desc"
    )
    assert [b.lengthMinutes for b in result] == [400, 300, 200, 100]
    assert [b.asin for b in result] == [
        "B0SCR00003", "B0SCR00002", "B0SCR00001", "B0SCR00000"
    ]


async def test_series_filters_apply():
    result = await get_series_books(fake_get, SERIES, filters={"longer_than": 250})
    assert [b.lengthMinutes for b in result] == [300, 400]
