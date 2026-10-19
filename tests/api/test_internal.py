"""
Internal seed endpoint tests.
Tests auth, validation, and narrator matching.
"""

# Standard library
import base64
import hashlib
import logging
import secrets
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

# Third party
import pytest

# Local
from app.api.routes.internal.router import (
    MAX_BODY_BYTES,
    _read_capped_body,
    _seed_authorized,
    hash_secret,
    verify_secret,
)

MOD = "app.api.routes.internal.router"
SEED_TOKEN = secrets.token_urlsafe(32)
SEED_HASH = hash_secret(SEED_TOKEN)
URL = "/internal/seed/narrators"


def _factory(session=None):
    session = session or AsyncMock()

    @asynccontextmanager
    async def factory():
        yield session

    return factory


def _bearer(token=SEED_TOKEN):
    return {"Authorization": f"Bearer {token}"}


# ============================================================
# AUTH TESTS
# ============================================================


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": SEED_TOKEN},
        {"Authorization": f"Basic {SEED_TOKEN}"},
        {"Authorization": "Bearer wrong-token"},
        {"Authorization": "Bearer " + SEED_TOKEN[:-1]},
        {"Authorization": "Bearer " + SEED_TOKEN + "A"},
        {"Authorization": "Bearer " + "A" * 5000},
        {"Authorization": "Bearer " + SEED_TOKEN[:-1] + "!"},
        {"Authorization": "Bearer " + secrets.token_urlsafe(32)},
    ],
    ids=["missing", "no-scheme", "basic", "short", "one-short", "one-long", "huge", "bad-charset", "wrong-valid-shape"],
)
async def test_bad_credentials_return_401(async_client, headers):
    """Every bad or wrong credential is the same 401."""
    with patch(f"{MOD}.settings") as mock_settings:
        mock_settings.seed_secret = SEED_HASH
        response = await async_client.post(URL, json=[], headers=headers)
    assert response.status_code == 401
    assert response.json() == {"error": "Unauthorized"}


@pytest.mark.asyncio
async def test_malformed_headers_never_hash(async_client):
    """Missing, long, short and wrong-charset tokens are rejected before hashing."""
    bad = [
        {},
        {"Authorization": "Bearer short"},
        {"Authorization": "Bearer " + "A" * 5000},
        {"Authorization": "Bearer " + "!" * 43},
    ]
    with patch(f"{MOD}.settings") as mock_settings, \
         patch(f"{MOD}.hash_secret") as hasher, \
         patch(f"{MOD}.verify_secret") as verifier:
        mock_settings.seed_secret = SEED_HASH
        for headers in bad:
            response = await async_client.post(URL, json=[], headers=headers)
            assert response.status_code == 401
    hasher.assert_not_called()
    verifier.assert_not_called()


@pytest.mark.asyncio
async def test_returns_401_when_secret_not_set(async_client):
    """Returns 401 when SEED_SECRET env var is empty."""
    with patch(f"{MOD}.settings") as mock_settings:
        mock_settings.seed_secret = ""
        response = await async_client.post(URL, json=[], headers=_bearer())
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_old_pbkdf2_secret_is_refused(async_client):
    """A PBKDF2-format SEED_SECRET leaves the endpoint disabled, even for its own token."""
    salt = secrets.token_bytes(32)
    key = hashlib.pbkdf2_hmac("sha256", SEED_TOKEN.encode(), salt, 1000)
    old = base64.b64encode(salt + key).decode()
    with patch(f"{MOD}.settings") as mock_settings, \
         patch(f"{MOD}.verify_secret", return_value=True) as verifier, \
         patch(f"{MOD}.AsyncSessionFactory", _factory()):
        mock_settings.seed_secret = old
        response = await async_client.post(URL, json=[], headers=_bearer())
    assert response.status_code == 401
    verifier.assert_not_called()


@pytest.mark.asyncio
async def test_body_not_read_before_auth(async_client):
    """An unauthorized request never has its body read or parsed."""
    with patch(f"{MOD}.settings") as mock_settings, \
         patch(f"{MOD}._read_capped_body", new_callable=AsyncMock) as reader, \
         patch(f"{MOD}.json.loads") as parser, \
         patch(f"{MOD}.AsyncSessionFactory") as factory:
        mock_settings.seed_secret = SEED_HASH
        response = await async_client.post(URL, content=b"not json", headers={"Authorization": "Bearer bad"})
    assert response.status_code == 401
    reader.assert_not_called()
    parser.assert_not_called()
    factory.assert_not_called()


@pytest.mark.asyncio
async def test_oversized_declared_body_rejected_after_auth(async_client):
    """An authorized body past the cap is a 413 and is never parsed."""
    with patch(f"{MOD}.settings") as mock_settings, \
         patch(f"{MOD}.json.loads") as parser:
        mock_settings.seed_secret = SEED_HASH
        response = await async_client.post(
            URL,
            content=b"[" + b" " * (MAX_BODY_BYTES + 1) + b"]",
            headers=_bearer(),
        )
    assert response.status_code == 413
    parser.assert_not_called()


@pytest.mark.asyncio
async def test_oversized_streamed_body_rejected_after_auth(async_client):
    """A chunked body declares no length, so the cap has to hold while it streams."""
    async def body():
        yield b"["
        yield b" " * (MAX_BODY_BYTES + 1)
        yield b"]"

    with patch(f"{MOD}.settings") as mock_settings, \
         patch(f"{MOD}.json.loads") as parser:
        mock_settings.seed_secret = SEED_HASH
        response = await async_client.post(URL, content=body(), headers=_bearer())
    assert response.status_code == 413
    parser.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("scheme", ["bearer", "BEARER", "Bearer"])
async def test_scheme_is_case_insensitive(async_client, scheme):
    with patch(f"{MOD}.settings") as mock_settings, \
         patch(f"{MOD}.AsyncSessionFactory", _factory()):
        mock_settings.seed_secret = SEED_HASH
        response = await async_client.post(
            URL, json=[], headers={"Authorization": f"{scheme} {SEED_TOKEN}"}
        )
    assert response.status_code == 200


# ============================================================
# VALIDATION TESTS
# ============================================================


@pytest.mark.asyncio
async def test_returns_400_for_non_array(async_client):
    """Returns 400 when body is not a JSON array."""
    with patch(f"{MOD}.settings") as mock_settings:
        mock_settings.seed_secret = SEED_HASH
        response = await async_client.post(URL, json={"name": "Jim Dale"}, headers=_bearer())
    assert response.status_code == 400
    assert "Expected JSON array" in response.json()["error"]


@pytest.mark.asyncio
async def test_returns_400_for_invalid_json(async_client):
    """Returns 400 when the body is not JSON."""
    with patch(f"{MOD}.settings") as mock_settings:
        mock_settings.seed_secret = SEED_HASH
        response = await async_client.post(URL, content=b"{nope", headers=_bearer())
    assert response.status_code == 400
    assert response.json()["error"] == "Invalid JSON"


@pytest.mark.asyncio
async def test_returns_200_with_empty_array(async_client):
    """Returns 200 with zero counts for empty input."""
    with patch(f"{MOD}.settings") as mock_settings, \
         patch(f"{MOD}.AsyncSessionFactory", _factory()):
        mock_settings.seed_secret = SEED_HASH
        response = await async_client.post(URL, json=[], headers=_bearer())
    assert response.status_code == 200
    data = response.json()
    assert data["matched"] == 0
    assert data["skipped"] == 0


@pytest.mark.asyncio
async def test_skips_entries_without_name(async_client):
    """Entries without a name field are skipped."""
    with patch(f"{MOD}.settings") as mock_settings, \
         patch(f"{MOD}.AsyncSessionFactory", _factory()):
        mock_settings.seed_secret = SEED_HASH
        response = await async_client.post(URL, json=[{"description": "No name here"}], headers=_bearer())
    assert response.status_code == 200
    assert response.json()["skipped"] == 1


# ============================================================
# CRYPTO TESTS
# ============================================================


def test_hash_secret_is_sha256_hex():
    """The stored form is a deterministic 64-character hex digest."""
    assert hash_secret("test") == hash_secret("test")
    assert len(hash_secret("test")) == 64


def test_verify_secret_validates_correct_token():
    assert verify_secret("my-secret", hash_secret("my-secret")) is True


def test_verify_secret_rejects_wrong_token():
    assert verify_secret("wrong-secret", hash_secret("my-secret")) is False


def test_verify_secret_rejects_garbage_and_empty_digest():
    assert verify_secret("anything", "not-a-valid-hash") is False
    assert verify_secret("anything", "") is False


# ============================================================
# ENDPOINT VISIBILITY
# ============================================================


def test_seed_endpoint_not_in_openapi(async_client):
    """Seed endpoint is not visible in the OpenAPI schema."""
    from app.main import app
    schema = app.openapi()
    paths = schema.get("paths", {})
    assert "/internal/seed/narrators" not in paths


# ============================================================
# SECRET HANDLING
# ============================================================

def test_unusable_secret_is_logged_once_on_use(caplog):
    """The warning goes out through the libex logger, once, when the secret is first used."""
    with patch(f"{MOD}.settings") as mock_settings, \
         patch(f"{MOD}._warned_unusable", False), \
         caplog.at_level(logging.WARNING, logger="libex"):
        mock_settings.seed_secret = "not-a-digest"
        assert _seed_authorized(f"Bearer {SEED_TOKEN}") is False
        assert _seed_authorized(f"Bearer {SEED_TOKEN}") is False
    assert [r.name for r in caplog.records if "SEED_SECRET" in r.getMessage()] == ["libex"]


def test_uppercase_hex_secret_still_works():
    """A digest pasted in uppercase, or with stray whitespace, is the same digest."""
    with patch(f"{MOD}.settings") as mock_settings:
        mock_settings.seed_secret = f" {SEED_HASH.upper()}\n"
        assert _seed_authorized(f"Bearer {SEED_TOKEN}") is True


@pytest.mark.asyncio
async def test_unicode_digit_content_length_is_not_a_500():
    """A superscript digit passes str.isdigit but not int(); it must be ignored, not raise."""
    request = MagicMock()
    request.headers = {"content-length": "²"}

    async def _stream():
        yield b"[]"

    request.stream = _stream
    assert await _read_capped_body(request) == b"[]"
