"""
Authentication, and the region check that follows it, on POST /internal/books.

A refused request must leave no trace of an Audible call: the probe count in
the app's logs is compared before and after every refusal.
"""

# Standard library
import hashlib
import os

# Third party
import pytest

BAD_SHAPES = ["none", "wrong", "basic_scheme", "no_scheme", "truncated", "extended", "bearer_alone", "seed"]


@pytest.mark.parametrize("shape", BAD_SHAPES)
def test_bad_credentials_are_401_and_never_reach_audible(sbx, shape):
    before = sbx.probe_count()
    resp = sbx.post(sbx.book(), auth=None if shape == "none" else shape, audible=False)
    assert resp.status_code == 401
    assert resp.json()["error"] == "Unauthorized"
    assert sbx.probe_count() == before


def test_the_ingest_token_passes_auth(sbx):
    # The right token with a bad region is a 400, so the 401s above are about
    # the credential and not about every request being refused.
    resp = sbx.post(sbx.book(), region="zz", audible=False)
    assert resp.status_code == 400


def test_a_5_mib_body_with_a_bad_token_is_401_not_413(sbx):
    big = b" " * (5 * 1024 * 1024)
    refused = sbx.post(None, auth="wrong", content=big, audible=False)
    assert refused.status_code == 401
    # With the right token the same body is over the cap.
    capped = sbx.post(None, content=big, audible=False)
    assert capped.status_code == 413


def test_seed_endpoint_takes_the_seed_token_only(sbx):
    seed = sbx.http.post("/internal/seed/narrators", content=b"[]", headers=sbx.auth("seed"))
    assert seed.status_code == 200
    ingest = sbx.http.post("/internal/seed/narrators", content=b"[]", headers=sbx.auth("ingest"))
    assert ingest.status_code == 401


@pytest.mark.parametrize("bad_region", [None, "zz"])
def test_missing_or_unknown_region_is_400(sbx, bad_region):
    before = sbx.probe_count()
    resp = sbx.post(sbx.book(), region=bad_region, audible=False)
    assert resp.status_code == 400
    assert "error" in resp.json()
    assert sbx.probe_count() == before


def test_a_bad_token_with_a_bad_region_is_401(sbx):
    # Auth first: an unauthenticated caller learns nothing about region handling.
    assert sbx.post(sbx.book(), region="zz", auth="wrong", audible=False).status_code == 401


def test_region_is_checked_before_the_body(sbx):
    resp = sbx.post(None, region="zz", content=b"not json", audible=False)
    assert resp.status_code == 400


def test_endpoint_is_disabled_when_the_secret_is_empty(sbx, tmp_path):
    with sbx.api_env(tmp_path, LIBEX_SANDBOX_INGEST_DIGEST=""):
        for kind in ("ingest", "seed"):
            resp = sbx.post(sbx.book(), auth=kind, audible=False)
            assert resp.status_code == 401
    # Restored: the same token works again.
    assert sbx.post(sbx.book(), region="zz", audible=False).status_code == 400


def test_endpoint_is_disabled_when_the_ingest_digest_equals_the_seed_digest(sbx, tmp_path):
    same = hashlib.sha256(os.environ["LIBEX_SANDBOX_SEED_TOKEN"].encode()).hexdigest()
    with sbx.api_env(tmp_path, LIBEX_SANDBOX_INGEST_DIGEST=same):
        for kind in ("ingest", "seed"):
            resp = sbx.post(sbx.book(), auth=kind, audible=False)
            assert resp.status_code == 401
        # Only the ingest endpoint is shut: the seed one still takes its token.
        seed = sbx.http.post("/internal/seed/narrators", content=b"[]", headers=sbx.auth("seed"))
        assert seed.status_code == 200
    assert sbx.post(sbx.book(), region="zz", audible=False).status_code == 400
