"""All eleven regions: a listed book is 409, and an entry lands in its own region."""

# Third party
import pytest

from tests.sandbox.conftest import REGIONS

SAME = "B0SBXREG01"


@pytest.mark.parametrize("region", REGIONS)
def test_a_book_audible_lists_is_409_live(sbx, region):
    asin = sbx.live_asin(region)
    resp = sbx.post(sbx.book(asin=asin, region=region), region=region, dry_run=False)
    assert resp.status_code == 409
    assert resp.json() == {"reason": "live"}
    assert sbx.psql(f"SELECT count(*) FROM books WHERE asin = '{asin}' AND region = '{region}' "
                    "AND manual_entry_at IS NOT NULL") == "0"


@pytest.mark.parametrize("region", REGIONS)
def test_an_entry_lands_in_its_own_region_only(sbx, region):
    jp = region == "jp"
    title = "日本語の題名" if jp else f"SBX Region {region}"
    body = sbx.book(asin=SAME, region=region, title=title)
    if jp:
        body["book"]["description"] += " 日本語のあらすじ、第一章"
        dry = sbx.post(body, region=region)
        assert dry.status_code == 200 and dry.json()["committed"] is False
        assert sbx.db_book(SAME, region).status_code == 404
    assert sbx.db_book(SAME, region).status_code == 404  # not served from another region's row

    resp = sbx.post(body, region=region, dry_run=False)
    assert resp.status_code == 200
    assert resp.json()["book"]["region"] == region

    served = sbx.db_book(SAME, region)
    assert served.status_code == 200
    assert served.json()["title"] == title
    assert served.json()["manualEntry"] is True
    assert (SAME, region) in sbx.delisted_asins(region)
    assert sbx.get_live(SAME, region).status_code == 404


def test_one_asin_in_eleven_regions_is_eleven_independent_rows(sbx):
    assert sbx.psql(f"SELECT count(*), count(DISTINCT region) FROM books WHERE asin = '{SAME}'") == "11|11"
    assert {r for a, r in sbx.delisted_asins() if a == SAME} == set(REGIONS)
