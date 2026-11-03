"""
POST /internal/books: auth, size caps, strict parsing, the Audible probe, the
transactional writer's outcomes and what is and is not logged or returned.
The probe, the writer and the session are mocked at the router's import path;
the real writer runs in tests/integration/test_manual_entry_endpoint.py.
"""

# Standard library
import json
import logging
import secrets
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

# Third party
import pytest
from sqlalchemy.exc import IntegrityError

# Local
from app.api.routes.internal.router import MAX_INGEST_BODY_BYTES, _ingest_authorized, hash_secret
from app.services.db.manual_entry import ENTITY, STORED, WRITTEN, ManualEntryReadbackError, ManualEntryResult
from libex_core.exceptions import AudibleAPIException
from tests.fixtures.manual_entry import ASIN, AUTHOR_ASIN, SERIES_ASIN, body

MOD = "app.api.routes.internal.router"
INGEST_TOKEN = secrets.token_urlsafe(32)
INGEST_HASH = hash_secret(INGEST_TOKEN)
SEED_TOKEN = secrets.token_urlsafe(32)
SEED_HASH = hash_secret(SEED_TOKEN)
URL = "/internal/books?region=us"

STORED_BOOK = {"asin": ASIN, "region": "us", "title": "Delisted", "manualEntry": True}
ENTITIES = {
    "authors": [{"asin": AUTHOR_ASIN, "name": "First Author", "status": "new"}],
    "narrators": [], "series": [], "genres": [],
}


def _bearer(token=INGEST_TOKEN):
    return {"Authorization": f"Bearer {token}"}


def _written(**over):
    return ManualEntryResult(WRITTEN, [], ENTITIES, over.pop("book", STORED_BOOK), over.pop("chapters", None))


@pytest.fixture
def env():
    """Settings with both secrets, the probe answering absent, a writer that stores, a cache that clears."""
    session = MagicMock()

    @asynccontextmanager
    async def factory():
        yield session

    with patch(f"{MOD}.settings") as settings, \
         patch(f"{MOD}.probe_listing", new_callable=AsyncMock, return_value="absent") as probe, \
         patch(f"{MOD}.enter_book", new_callable=AsyncMock, return_value=_written()) as enter, \
         patch(f"{MOD}.AsyncSessionFactory", factory), \
         patch(f"{MOD}.cache.invalidate", new_callable=AsyncMock) as invalidate:
        settings.ingest_secret = INGEST_HASH
        settings.seed_secret = SEED_HASH
        yield MagicMock(settings=settings, probe=probe, enter=enter, invalidate=invalidate)


def _logged(caplog) -> str:
    """Every message and every structured field of every record."""
    return "\n".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)


async def _post(client, payload=None, *, url=URL, headers=None, **kw):
    content = json.dumps(body() if payload is None else payload).encode()
    return await client.post(url, content=content, headers=_bearer() if headers is None else headers, **kw)


# ============================================================
# AUTH
# ============================================================


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": INGEST_TOKEN},
        {"Authorization": f"Basic {INGEST_TOKEN}"},
        {"Authorization": "Bearer wrong-token"},
        {"Authorization": "Bearer " + INGEST_TOKEN[:-1]},
        {"Authorization": "Bearer " + INGEST_TOKEN + "A"},
        {"Authorization": "Bearer " + "A" * 5000},
        {"Authorization": "Bearer " + secrets.token_urlsafe(32)},
        {"Authorization": f"Bearer {SEED_TOKEN}"},
    ],
    ids=["missing", "no-scheme", "basic", "short", "one-short", "one-long", "huge", "wrong-valid-shape", "seed-token"],
)
async def test_a_bad_or_wrong_credential_is_the_same_401(async_client, env, headers):
    response = await _post(async_client, headers=headers)
    assert response.status_code == 401
    assert response.json() == {"error": "Unauthorized"}
    env.probe.assert_not_called()
    env.enter.assert_not_called()


async def test_the_right_token_is_not_a_401(async_client, env):
    assert (await _post(async_client)).status_code == 200


@pytest.mark.parametrize(
    "secret",
    ["", "   ", "not-a-digest", "A" * 63, "z" * 64],
    ids=["empty", "blank", "garbage", "short-digest", "non-hex"],
)
async def test_a_disabled_or_unusable_secret_refuses_even_its_own_token(async_client, env, secret):
    env.settings.ingest_secret = secret
    response = await _post(async_client)
    assert response.status_code == 401
    env.enter.assert_not_called()


async def test_a_digest_equal_to_the_seed_digest_is_refused(async_client, env):
    env.settings.ingest_secret = SEED_HASH
    assert (await _post(async_client, headers=_bearer(SEED_TOKEN))).status_code == 401
    env.enter.assert_not_called()


async def test_an_uppercased_digest_equal_to_the_seed_digest_is_refused(async_client, env):
    env.settings.ingest_secret = SEED_HASH.upper()
    assert (await _post(async_client, headers=_bearer(SEED_TOKEN))).status_code == 401


async def test_a_digest_pasted_uppercase_with_whitespace_still_matches(async_client, env):
    env.settings.ingest_secret = f"  {INGEST_HASH.upper()}\n"
    assert (await _post(async_client)).status_code == 200


def test_a_blank_seed_secret_does_not_make_a_blank_ingest_secret_equal():
    with patch(f"{MOD}.settings") as settings:
        settings.ingest_secret, settings.seed_secret = INGEST_HASH, ""
        assert _ingest_authorized(f"Bearer {INGEST_TOKEN}") is True
        assert _ingest_authorized(None) is False


async def test_the_ingest_token_does_not_open_the_seed_endpoint(async_client, env):
    response = await async_client.post("/internal/seed/narrators", json=[], headers=_bearer())
    assert response.status_code == 401


async def test_a_malformed_header_is_never_hashed(async_client, env):
    with patch(f"{MOD}.verify_secret") as verifier:
        for headers in ({}, {"Authorization": "Bearer short"}, {"Authorization": "Bearer " + "!" * 43}):
            assert (await _post(async_client, headers=headers)).status_code == 401
    verifier.assert_not_called()


async def test_the_body_is_not_read_before_auth(async_client, env):
    with patch(f"{MOD}._read_capped_body", new_callable=AsyncMock) as reader, \
         patch("starlette.requests.Request.body", new_callable=AsyncMock) as whole, \
         patch(f"{MOD}.IngestBody.model_validate_json") as parser:
        response = await _post(async_client, headers={"Authorization": "Bearer bad"})
    assert response.status_code == 401
    reader.assert_not_called()
    whole.assert_not_called()
    parser.assert_not_called()


# ============================================================
# REGION AND SIZE
# ============================================================


@pytest.mark.parametrize("url", ["/internal/books", "/internal/books?region=xx", "/internal/books?region="])
async def test_a_missing_or_unknown_region_is_refused_before_anything_else(async_client, env, url):
    response = await _post(async_client, url=url)
    assert response.status_code == 400
    env.probe.assert_not_called()
    env.enter.assert_not_called()


async def test_an_uppercase_region_is_normalised(async_client, env):
    response = await _post(async_client, url="/internal/books?region=US")
    assert response.status_code == 200
    assert env.probe.await_args.args == (ASIN, "us")
    assert env.enter.await_args.kwargs["region"] == "us"


async def test_a_body_over_the_declared_cap_is_413_and_never_parsed(async_client, env):
    with patch(f"{MOD}.IngestBody.model_validate_json") as parser:
        response = await async_client.post(
            URL, content=b" " * (MAX_INGEST_BODY_BYTES + 1), headers=_bearer()
        )
    assert response.status_code == 413
    assert response.json() == {"error": "Payload too large"}
    parser.assert_not_called()
    env.probe.assert_not_called()


async def test_a_streamed_body_over_the_cap_is_413(async_client, env):
    async def stream():
        yield b"{"
        yield b" " * (MAX_INGEST_BODY_BYTES + 1)

    response = await async_client.post(URL, content=stream(), headers=_bearer())
    assert response.status_code == 413
    env.probe.assert_not_called()


async def test_a_body_at_the_cap_is_read(async_client, env):
    pad = MAX_INGEST_BODY_BYTES - len(json.dumps(body()))
    raw = json.dumps(body()).encode() + b" " * pad
    assert len(raw) == MAX_INGEST_BODY_BYTES
    response = await async_client.post(URL, content=raw, headers=_bearer())
    assert response.status_code == 200


# ============================================================
# STRICT PARSING
# ============================================================


@pytest.mark.parametrize(
    "payload,loc,kind",
    [
        ({"book": {**body()["book"], "link": "x"}}, ["book", "link"], "extra_forbidden"),
        ({"book": {**body()["book"], "manualEntry": True}}, ["book", "manualEntry"], "extra_forbidden"),
        ({"book": {**body()["book"], "region": "xx"}}, ["book", "region"], "value_error"),
        ({"book": {**body()["book"], "imageUrl": "http://x/y"}}, ["book", "imageUrl"], "value_error"),
        ({"book": {**body()["book"], "rating": 9}}, ["book", "rating"], "less_than_equal"),
        ({"book": {k: v for k, v in body()["book"].items() if k != "title"}}, ["book", "title"], "missing"),
        ({"book": body()["book"], "extra": 1}, ["extra"], "extra_forbidden"),
        ({}, ["book"], "missing"),
    ],
)
async def test_a_refused_body_is_a_422_with_only_loc_and_type(async_client, env, payload, loc, kind):
    response = await _post(async_client, payload)
    assert response.status_code == 422
    assert response.json() == {"errors": [{"loc": loc, "type": kind}]}
    env.probe.assert_not_called()
    env.enter.assert_not_called()


async def test_malformed_json_is_a_422_not_a_500(async_client, env):
    response = await async_client.post(URL, content=b"{nope", headers=_bearer())
    assert response.status_code == 422
    assert set(response.json()) == {"errors"}
    assert all(set(e) == {"loc", "type"} for e in response.json()["errors"])


async def test_an_error_body_never_echoes_a_submitted_value(async_client, env):
    secret = "ECHO-ME-9f3a"
    payload = body()
    payload["book"].update(rating=secret, region=secret, unknownKey=secret)
    response = await _post(async_client, payload)
    assert response.status_code == 422
    assert secret not in response.text


async def test_a_body_region_that_differs_from_the_query_region_is_a_422(async_client, env):
    response = await _post(async_client, body(region="uk"))
    assert response.status_code == 422
    assert response.json() == {"errors": [{"loc": ["book", "region"], "type": "region_mismatch"}]}
    env.probe.assert_not_called()


# ============================================================
# THE AUDIBLE PROBE
# ============================================================


async def test_a_book_audible_serves_is_a_409_live_and_is_never_written(async_client, env):
    env.probe.return_value = "live"
    response = await _post(async_client)
    assert response.status_code == 409
    assert response.json() == {"reason": "live"}
    env.enter.assert_not_called()
    env.invalidate.assert_not_called()


@pytest.mark.parametrize("listing", ["absent", "placeholder"])
async def test_an_absent_or_placeholder_listing_proceeds_to_the_write(async_client, env, listing):
    env.probe.return_value = listing
    response = await _post(async_client)
    assert response.status_code == 200
    env.enter.assert_awaited_once()


@pytest.mark.parametrize(
    "failure",
    [AudibleAPIException("down"), AudibleAPIException("429", upstream_status=429)],
    ids=["outage", "rate-limited"],
)
async def test_an_audible_outage_is_a_503_and_is_never_a_delisting(async_client, env, failure):
    env.probe.side_effect = failure
    response = await _post(async_client)
    assert response.status_code == 503
    assert response.json() == {"reason": "audible_unavailable"}
    env.enter.assert_not_called()


async def test_the_probe_is_asked_about_the_books_asin_and_region(async_client, env):
    await _post(async_client, body(asin="B0OTHER002"), url="/internal/books?region=us")
    assert env.probe.await_args.args == ("B0OTHER002", "us")


async def test_the_probe_runs_before_the_writer_opens_a_session(async_client, env):
    order = []
    env.probe.side_effect = lambda *a: order.append("probe") or "absent"
    env.enter.side_effect = lambda *a, **k: order.append("write") or _written()
    await _post(async_client)
    assert order == ["probe", "write"]


# ============================================================
# WHAT THE WRITER GETS
# ============================================================


async def test_the_writer_gets_the_stamp_and_the_three_flags_false(async_client, env):
    await _post(async_client)
    book = env.enter.await_args.args[1]
    assert book["manualEntry"] is True
    assert (book["isListenable"], book["isBuyable"], book["isVvab"]) == (False, False, False)
    assert book["asin"] == ASIN and book["title"] == "Delisted"


async def test_a_flag_the_input_sets_is_kept(async_client, env):
    await _post(async_client, body(isBuyable=True, isVvab=True))
    book = env.enter.await_args.args[1]
    assert (book["isListenable"], book["isBuyable"], book["isVvab"]) == (False, True, True)


async def test_unsent_keys_are_not_invented(async_client, env):
    await _post(async_client)
    book = env.enter.await_args.args[1]
    assert set(book) == set(body()["book"]) | {"isListenable", "isBuyable", "isVvab", "manualEntry"}


async def test_chapters_are_passed_dumped_and_none_when_absent(async_client, env):
    await _post(async_client)
    chapters = env.enter.await_args.args[2]
    assert chapters["runtimeLengthMs"] == 5400000
    assert [c["title"] for c in chapters["chapters"]] == ["One", "Two"]
    assert "audibleExtras" not in chapters

    without = body()
    del without["chapters"]
    await _post(async_client, without)
    assert env.enter.await_args.args[2] is None


async def test_dry_run_is_the_default_and_commit_must_be_asked_for(async_client, env):
    await _post(async_client)
    assert env.enter.await_args.kwargs["dry_run"] is True
    await _post(async_client, url="/internal/books?region=us&dry_run=false")
    assert env.enter.await_args.kwargs["dry_run"] is False
    await _post(async_client, url="/internal/books?region=us&dry_run=true")
    assert env.enter.await_args.kwargs["dry_run"] is True


async def test_an_unparseable_dry_run_value_never_reaches_the_writer(async_client, env):
    response = await _post(async_client, url="/internal/books?region=us&dry_run=maybe")
    assert response.status_code >= 400
    env.enter.assert_not_called()


# ============================================================
# OUTCOMES
# ============================================================


async def test_a_dry_run_response_is_the_full_report_and_clears_no_cache(async_client, env):
    env.enter.return_value = _written(chapters={"runtimeLengthMs": 1, "chapters": []})
    response = await _post(async_client)
    assert response.status_code == 200
    data = response.json()
    assert (data["dryRun"], data["committed"]) == (True, False)
    assert data["book"]["asin"] == ASIN and data["book"]["manualEntry"] is True
    assert data["chapters"]["runtimeLengthMs"] == 1
    assert data["entities"] == ENTITIES
    env.invalidate.assert_not_called()


async def test_a_commit_response_says_committed_and_chapters_are_null_without_any(async_client, env):
    response = await _post(async_client, url="/internal/books?region=us&dry_run=false")
    data = response.json()
    assert (data["dryRun"], data["committed"], data["chapters"]) == (False, True, None)


async def test_a_commit_clears_the_book_chapter_author_and_series_cache_keys(async_client, env):
    await _post(async_client, url="/internal/books?region=us&dry_run=false")
    keys = {c.args[1] for c in env.invalidate.await_args_list}
    assert keys == {
        f"book:us:{ASIN}", f"chapters:us:{ASIN}",
        f"author_books:us:{AUTHOR_ASIN}",
        f"series_books:us:{SERIES_ASIN}",
    }


async def test_a_cache_failure_after_a_commit_does_not_undo_the_commit(async_client, env, caplog):
    env.invalidate.side_effect = RuntimeError("cache down with secret-cache-detail")
    with caplog.at_level(logging.DEBUG):
        response = await _post(async_client, url="/internal/books?region=us&dry_run=false")
    assert response.status_code == 200 and response.json()["committed"] is True
    assert "secret-cache-detail" not in _logged(caplog)
    assert "RuntimeError" in _logged(caplog)


async def test_an_already_stored_book_is_a_409_stored_and_clears_nothing(async_client, env):
    env.enter.return_value = ManualEntryResult(STORED, [{"loc": ["book", "asin"], "reason": "stored"}])
    response = await _post(async_client, url="/internal/books?region=us&dry_run=false")
    assert response.status_code == 409
    assert response.json() == {"reason": "stored"}
    env.invalidate.assert_not_called()


async def test_an_entity_conflict_is_a_422_of_loc_and_type_and_clears_nothing(async_client, env):
    conflicts = [{"loc": ["book", "authors", 0, "description"], "reason": "author_differs"}]
    env.enter.return_value = ManualEntryResult(ENTITY, conflicts, ENTITIES)
    response = await _post(async_client, url="/internal/books?region=us&dry_run=false")
    assert response.status_code == 422
    assert response.json() == {"errors": [
        {"loc": ["book", "authors", 0, "description"], "type": "author_differs"}]}
    env.invalidate.assert_not_called()


@pytest.mark.parametrize(
    "failure",
    [RuntimeError("row values: SECRET-ROW"), ManualEntryReadbackError("could not be read back")],
    ids=["writer-error", "readback-failure"],
)
async def test_a_writer_or_readback_failure_is_a_generic_500_and_clears_nothing(async_client, env, failure):
    env.enter.side_effect = failure
    response = await _post(async_client, url="/internal/books?region=us&dry_run=false")
    assert response.status_code == 500
    assert response.json() == {"error": "Internal Server Error"}
    assert "SECRET-ROW" not in response.text
    env.invalidate.assert_not_called()


async def test_a_database_integrity_error_logs_only_the_type_name(async_client, env, caplog):
    env.enter.side_effect = IntegrityError("INSERT ... VALUES ('SECRET-ROW')", {"title": "SECRET-ROW"}, Exception("dup"))
    with caplog.at_level(logging.DEBUG):
        response = await _post(async_client)
    assert response.status_code == 500
    assert "IntegrityError" in _logged(caplog)
    assert "SECRET-ROW" not in _logged(caplog)
    assert "SECRET-ROW" not in response.text


# ============================================================
# LOGS
# ============================================================


async def test_no_body_value_and_no_token_reaches_a_log_line(async_client, env, caplog):
    payload = body(title="SECRET-TITLE-77", description="SECRET-DESC-77")
    payload["book"]["authors"][0]["name"] = "SECRET-AUTHOR-77"
    with caplog.at_level(logging.DEBUG):
        ok = await _post(async_client, payload, url="/internal/books?region=us&dry_run=false")
        env.probe.return_value = "live"
        live = await _post(async_client, payload)
        env.probe.side_effect = AudibleAPIException("down")
        down = await _post(async_client, payload)
        env.probe.side_effect = None
        env.probe.return_value = "absent"
        env.enter.return_value = ManualEntryResult(ENTITY, [{"loc": ["book"], "reason": "author_differs"}])
        conflict = await _post(async_client, payload)
        bad = await _post(async_client, {"book": {**payload["book"], "rating": "SECRET-TITLE-77"}})
    assert [r.status_code for r in (ok, live, down, conflict, bad)] == [200, 409, 503, 422, 422]
    for secret in ("SECRET-TITLE-77", "SECRET-DESC-77", "SECRET-AUTHOR-77", INGEST_TOKEN, INGEST_HASH):
        assert secret not in _logged(caplog)
    assert "Book entry" in _logged(caplog)


# ============================================================
# THE SURFACE
# ============================================================


async def test_the_route_and_its_models_are_absent_from_openapi(async_client):
    schema = (await async_client.get("/openapi.json")).json()
    dump = json.dumps(schema)
    assert "/internal/books" not in schema["paths"]
    assert not any(path.startswith("/internal") for path in schema["paths"])
    for name in ("IngestBody", "IngestBook", "IngestChapters", "IngestAuthor", "IngestSeries", "IngestGenre"):
        assert name not in dump


def test_a_non_ascii_seed_secret_is_a_refusal_not_a_crash(env):
    env.settings.seed_secret = "é" * 64
    assert _ingest_authorized(f"Bearer {INGEST_TOKEN}") is True
    assert _ingest_authorized("Bearer " + "x" * len(INGEST_TOKEN)) is False
