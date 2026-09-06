"""
ASIN-vs-region validation precedence.

When a request is invalid in both ways at once -- a malformed ASIN path
segment alongside a malformed ?region= -- exactly one error reaches the
caller, decided by which FastAPI dependency is declared first in the route's
own function signature: `asin: Annotated[str, Depends(valid_asin(...))]`
ahead of `region: str = Depends(valid_region)`. That ordering is incidental,
not a designed precedence rule -- it falls out of parameter position alone,
so a refactor that reorders the parameters, hoists `response: Response`
above them, or inserts another dependency between them changes the answer
with nothing here announcing it and no code review signal beyond the diff
itself.

This file pins today's answer so a future reorder shows up as a failing
test rather than a silent behaviour change. It is documented current
behaviour, not a promise made to AudiMeta consumers: AudiMeta validates
ASIN and region as a single object and reports both problems in one 422
`errors` array, so it has no equivalent precedence for Libex to match either
way.

Three cases per route, not one: pinning only the both-invalid case cannot
tell a future reader whether a change moved the precedence or broke one of
the single-invalid cases instead.
"""

# Third party
import pytest

# ============================================================
# ROUTES THAT TAKE BOTH A valid_asin AND A valid_region DEPENDENCY
# ============================================================

# (label, url template, a well-formed ASIN this route accepts)
# One representative per router that carries this dependency pairing.
_PRECEDENCE_ROUTES = [
    ("/book/{asin}", "/book/{asin}?region={region}", "B08G9PRS1K"),
    ("/author/{asin}", "/author/{asin}?region={region}", "B000APF21M"),
    ("/series/{asin}", "/series/{asin}?region={region}", "B00SERIES1"),
]

_BAD_ASIN = "not-an-asin"
_BAD_REGION = "zz"


@pytest.mark.asyncio
@pytest.mark.parametrize("label,url_template,good_asin", _PRECEDENCE_ROUTES)
async def test_both_invalid_the_asin_dependency_wins(async_client, label, url_template, good_asin):
    """
    ASIN is declared before region on every one of these routes, so an
    ASIN-shaped 404 is what a caller sees when both are wrong -- not the
    region-shaped 400 an unordered reading of the two checks might expect.
    """
    url = url_template.format(asin=_BAD_ASIN, region=_BAD_REGION)
    response = await async_client.get(url)
    assert response.json() == {
        "error": f"Invalid ASIN format: {_BAD_ASIN}",
        "status_code": 404,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("label,url_template,good_asin", _PRECEDENCE_ROUTES)
async def test_bad_asin_alone_still_404s(async_client, label, url_template, good_asin):
    """A malformed ASIN with an otherwise-valid region is unaffected by the
    ordering question -- there is nothing for it to race against."""
    url = url_template.format(asin=_BAD_ASIN, region="us")
    response = await async_client.get(url)
    assert response.json() == {
        "error": f"Invalid ASIN format: {_BAD_ASIN}",
        "status_code": 404,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("label,url_template,good_asin", _PRECEDENCE_ROUTES)
async def test_bad_region_alone_still_400s(async_client, label, url_template, good_asin):
    """A well-formed ASIN with a malformed region is unaffected by the
    ordering question either -- region is the only thing wrong here."""
    url = url_template.format(asin=good_asin, region=_BAD_REGION)
    response = await async_client.get(url)
    assert response.json() == {
        "error": f"Invalid region: {_BAD_REGION}",
        "status_code": 400,
    }


# ============================================================
# /db/book/{asin} -- TAKES NO region DEPENDENCY, SO THERE IS NOTHING TO FLIP
# ============================================================

@pytest.mark.asyncio
async def test_db_book_has_no_region_dependency_to_race_against(async_client):
    """
    /db/book/{asin} never gained a region parameter, so it carries no
    precedence question at all -- a malformed ASIN 404s the same way
    whether or not a region query string rides along, because nothing on
    this route ever reads it. This is the other half of the current-behaviour
    picture: "unchanged" is as much a fact worth pinning as "flipped", since
    a later change could just as easily give this route a region dependency
    and introduce the same ordering question the routes above already have.
    """
    response = await async_client.get(f"/db/book/{_BAD_ASIN}?region={_BAD_REGION}")
    assert response.json() == {
        "error": f"Invalid ASIN format: {_BAD_ASIN}",
        "status_code": 404,
    }
