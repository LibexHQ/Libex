"""
Internal operator endpoints.
Completely undocumented. The seed endpoint requires SEED_SECRET and uploads
enrichment data from local scrapers to the production DB. The book-entry
endpoint requires INGEST_SECRET and stores a book Audible no longer lists.

SECURITY:
- The bearer token is 32 random bytes, urlsafe-base64 (43 characters).
  SEED_SECRET stores its SHA-256 hex digest, never the plaintext. A random
  token of that size needs no slow password hash, so the check is one
  SHA-256 and a constant-time compare.
- The header is shape-checked (Bearer scheme, exact length and charset)
  before anything is hashed, and the body is not read until the token has
  verified. An authorized body is capped at MAX_BODY_BYTES.
- Empty SEED_SECRET = endpoint fully disabled (returns 401).
- A SEED_SECRET that is not a SHA-256 hex digest (the old PBKDF2 value
  included) is refused: the endpoint stays disabled and says why in the log.
- include_in_schema=False = invisible in OpenAPI docs.
- INGEST_SECRET follows the same rules under its own name. It is refused,
  and the endpoint stays disabled, when its digest equals SEED_SECRET's, so
  one leaked token can never open both.
- Generate a secret with: python -m app.api.routes.internal.router
"""

# Standard library
import hashlib
import hmac
import json
import re
import secrets
from datetime import datetime, timezone
from typing import Any

# Third party
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy import select, update, func

# Database
from app.db.models import Narrator
from app.db.session import AsyncSessionFactory

# Core
from app.core.config import get_settings
from app.core.ingest_models import IngestBody, error_list
from app.core.logging import get_logger
from libex_core.audible.client import validate_region
from libex_core.exceptions import AudibleAPIException
from libex_core.models import BookResponse, ChapterResponse

# Services
from app.services.audible.listing import probe_listing
from app.services.cache import manager as cache
from app.services.db.manual_entry import ENTITY, STORED, enter_book

logger = get_logger()
settings = get_settings()

router = APIRouter(prefix="/internal", include_in_schema=False)

TOKEN_BYTES = 32
TOKEN_LENGTH = 43  # urlsafe base64 of 32 bytes, unpadded
MAX_BODY_BYTES = 32 * 1024 * 1024
MAX_INGEST_BODY_BYTES = 1024 * 1024

_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{43}")
_SECRET_RE = re.compile(r"[0-9a-f]{64}")
_warned_unusable = False
_warned_ingest_unusable = False


# ============================================================
# AUTH HELPERS
# ============================================================

def hash_secret(plaintext: str) -> str:
    """SHA-256 hex digest of a token, the form SEED_SECRET stores."""
    return hashlib.sha256(plaintext.encode()).hexdigest()


def verify_secret(plaintext: str, stored_digest: str) -> bool:
    """Constant-time check of a token against a stored SHA-256 hex digest."""
    return hmac.compare_digest(hash_secret(plaintext), stored_digest)


def _secret_is_usable(stored: str) -> bool:
    return bool(_SECRET_RE.fullmatch(stored))


def _extract_token(authorization: str | None) -> str | None:
    """Returns the token only if the header has exactly the expected shape."""
    if not authorization or len(authorization) != len("Bearer ") + TOKEN_LENGTH:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not _TOKEN_RE.fullmatch(token):
        return None
    return token


def _warn_unusable_secret_once() -> None:
    """Logged on first use rather than at import, which runs before logging is set up."""
    global _warned_unusable
    if not _warned_unusable:
        _warned_unusable = True
        logger.warning(
            "SEED_SECRET is not a SHA-256 hex digest (an old PBKDF2 value?); "
            "the seed endpoint is disabled until it is regenerated with "
            "python -m app.api.routes.internal.router"
        )


def _warn_unusable_ingest_secret_once(reason: str) -> None:
    global _warned_ingest_unusable
    if not _warned_ingest_unusable:
        _warned_ingest_unusable = True
        logger.warning(
            f"INGEST_SECRET {reason}; the book-entry endpoint is disabled until it is "
            "regenerated with python -m app.api.routes.internal.router"
        )


def _token_matches(authorization: str | None, stored: str) -> bool:
    """Shape check first, hash only for a well-formed token."""
    token = _extract_token(authorization)
    if token is None:
        return False
    return verify_secret(token, stored)


def _seed_authorized(authorization: str | None) -> bool:
    # Lowered and stripped so a digest pasted in uppercase or with stray whitespace still matches.
    stored = (settings.seed_secret or "").strip().lower()
    if not stored:
        return False
    if not _secret_is_usable(stored):
        _warn_unusable_secret_once()
        return False
    return _token_matches(authorization, stored)


def _ingest_authorized(authorization: str | None) -> bool:
    stored = (settings.ingest_secret or "").strip().lower()
    if not stored:
        return False
    if not _secret_is_usable(stored):
        _warn_unusable_ingest_secret_once("is not a SHA-256 hex digest")
        return False
    seed = (settings.seed_secret or "").strip().lower()
    # compare_digest raises on non-ASCII, and an unusable seed can never equal a usable digest.
    if _secret_is_usable(seed) and hmac.compare_digest(stored, seed):
        _warn_unusable_ingest_secret_once("equals SEED_SECRET")
        return False
    return _token_matches(authorization, stored)


async def _read_capped_body(request: Request, limit: int = MAX_BODY_BYTES) -> bytes | None:
    """Reads the body, returning None once it passes limit."""
    declared = request.headers.get("content-length")
    if declared and declared.isascii() and declared.isdigit() and int(declared) > limit:
        return None
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("/seed/narrators")
async def seed_narrators(request: Request) -> JSONResponse:
    """
    Accepts a JSON array of narrator enrichment data.
    Matches by name against existing narrators in the DB.
    Updates profile fields only — never creates new narrators.

    Nothing is read, parsed or opened until the bearer token has verified,
    so the handler takes no body or database dependency.
    """
    if not _seed_authorized(request.headers.get("authorization")):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})

    raw = await _read_capped_body(request)
    if raw is None:
        return JSONResponse(status_code=413, content={"error": "Payload too large"})

    try:
        data = json.loads(raw)
    except Exception:
        return JSONResponse(status_code=400, content={"error": "Invalid JSON"})

    if not isinstance(data, list):
        return JSONResponse(status_code=400, content={"error": "Expected JSON array"})

    async with AsyncSessionFactory() as session:
        stats = await _apply_enrichment(session, data)

    logger.info("Seed: narrator enrichment complete", extra=stats)

    return JSONResponse(
        status_code=200,
        content={"status": "ok", **stats},
    )


async def _apply_enrichment(session: Any, data: list[Any]) -> dict[str, int]:
    """Matches each entry to a narrator, updates profile fields, commits."""
    stats = {"matched": 0, "updated": 0, "skipped": 0}

    for entry in data:
        # Counted, never logged: an entry that is not an object has no shape to trust.
        name = entry.get("name") if isinstance(entry, dict) else None
        if not isinstance(name, str) or not name:
            stats["skipped"] += 1
            continue

        # Exact match first (case-insensitive)
        result = await session.execute(
            select(Narrator).where(func.lower(Narrator.name) == name.lower())
        )
        narrator = result.scalars().first()

        # Normalized match: strip periods, collapse spaces
        if not narrator:
            normalized = name.lower().replace(".", "").replace("  ", " ").strip()
            result = await session.execute(
                select(Narrator).where(
                    func.lower(func.replace(func.replace(Narrator.name, '.', ''), '  ', ' ')) == normalized
                )
            )
            narrator = result.scalars().first()

        if not narrator:
            stats["skipped"] += 1
            logger.info(f"Seed: no match for narrator '{name}'")
            continue

        stats["matched"] += 1

        # Build update values — only set fields that are provided
        values: dict[str, Any] = {}
        if entry.get("description"):
            values["description"] = entry["description"]
        if entry.get("image"):
            values["image"] = entry["image"]
        if entry.get("website"):
            values["website"] = entry["website"]
        if entry.get("languages"):
            values["languages"] = entry["languages"]
        if entry.get("accents"):
            values["accents"] = entry["accents"]
        if entry.get("gender"):
            values["gender"] = entry["gender"]
        if entry.get("genres"):
            values["genres_narrated"] = entry["genres"]
        if entry.get("audiobooksProduced"):
            values["audiobooks_produced"] = entry["audiobooksProduced"]
        if entry.get("culturalHeritage"):
            values["cultural_heritage"] = entry["culturalHeritage"]
        if entry.get("publishers"):
            values["publishers"] = entry["publishers"]
        if entry.get("socialLinks"):
            values["social_links"] = entry["socialLinks"]
        if entry.get("audioSamples"):
            values["audio_samples"] = entry["audioSamples"]
        if entry.get("source"):
            values["source"] = entry["source"]
        if entry.get("sourceUrl"):
            values["source_url"] = entry["sourceUrl"]

        if values:
            values["source_updated_at"] = datetime.now(timezone.utc)
            values["fetched_description"] = True
            await session.execute(
                update(Narrator)
                .where(Narrator.name == narrator.name)
                .values(**values)
            )
            stats["updated"] += 1

    await session.commit()
    return stats


# ============================================================
# BOOK ENTRY
# ============================================================

def _errors(errors: list[dict[str, Any]], status: int = 422) -> JSONResponse:
    return JSONResponse(status_code=status, content={"errors": errors})


def _writer_dict(body: IngestBody) -> dict[str, Any]:
    """
    The parsed book as the writer takes it. exclude_unset drops the three flags
    the model defaults to False when the caller leaves them out, so they are
    put back here; the writer then stores False rather than an unknown. manualEntry is set server-side
    because the input model refuses it.
    """
    book = body.book.model_dump(exclude_unset=True)
    for flag in ("isListenable", "isBuyable", "isVvab"):
        book.setdefault(flag, False)
    book["manualEntry"] = True
    return book


async def _clear_cache(book: dict[str, Any], region: str) -> None:
    """
    Drops every cached entry that can hold the entered book or a list derived
    from its links. Called after a commit only; a failure here cannot undo the
    commit, so it is logged by type and the response still reports the commit.
    """
    asin = book["asin"]
    keys = [cache.book_key(asin, region), cache.chapters_key(asin, region)]
    keys += [cache.author_books_key(a["asin"], region) for a in book.get("authors", []) if a.get("asin")]
    keys += [cache.series_books_key(s["asin"], region) for s in book.get("series", []) if s.get("asin")]
    try:
        async with AsyncSessionFactory() as session:
            for key in keys:
                await cache.invalidate(session, key)
    except Exception as e:
        logger.warning("Book entry: cache clear failed", extra={"error_type": type(e).__name__})


@router.post("/books")
async def enter_delisted_book(
    request: Request,
    region: str | None = None,
    dry_run: bool = True,
) -> JSONResponse:
    """
    Stores one book Audible no longer lists, with its links and chapters.

    Dry run by default: the whole write happens and is rolled back, so the
    response shows what a commit would store. Nothing is read from the body
    until the token has verified, and nothing in a response or a log line
    echoes a submitted value.
    """
    if not _ingest_authorized(request.headers.get("authorization")):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})

    region = validate_region(region)  # type: ignore[arg-type]  # None raises RegionException

    raw = await _read_capped_body(request, MAX_INGEST_BODY_BYTES)
    if raw is None:
        return JSONResponse(status_code=413, content={"error": "Payload too large"})

    try:
        body = IngestBody.model_validate_json(raw)
    except ValidationError as e:
        return _errors(error_list(e))
    if body.book.region != region:
        return _errors([{"loc": ["book", "region"], "type": "region_mismatch"}])

    asin = body.book.asin
    context = {"request_id": getattr(request.state, "request_id", None), "asin": asin,
              "region": region, "dry_run": dry_run}

    try:
        listing = await probe_listing(asin, region)
    except AudibleAPIException:
        logger.info("Book entry: Audible unavailable", extra={**context, "outcome": "audible_unavailable"})
        return JSONResponse(status_code=503, content={"reason": "audible_unavailable"})
    if listing == "live":
        logger.info("Book entry: listed on Audible", extra={**context, "outcome": "live"})
        return JSONResponse(status_code=409, content={"reason": "live"})

    book = _writer_dict(body)
    chapters = (
        body.chapters.model_dump(exclude_none=True, exclude_unset=True) if body.chapters else None
    )
    try:
        async with AsyncSessionFactory() as session:
            result = await enter_book(session, book, chapters, region=region, dry_run=dry_run)
    except Exception as e:
        # Never the exception itself: a database error carries the row's values.
        logger.error("Book entry failed", extra={**context, "outcome": "error", "error_type": type(e).__name__})
        return JSONResponse(status_code=500, content={"error": "Internal Server Error"})

    counts = {kind: len(items) for kind, items in result.entities.items()}
    if result.status == STORED:
        logger.info("Book entry: already stored", extra={**context, "outcome": "stored"})
        return JSONResponse(status_code=409, content={"reason": "stored"})
    if result.status == ENTITY:
        logger.info("Book entry: entity conflict", extra={**context, "outcome": "entity_conflict", "entities": counts})
        return _errors([{"loc": c["loc"], "type": c["reason"]} for c in result.conflicts])

    committed = not dry_run
    if committed:
        await _clear_cache(book, region)
    logger.info("Book entry complete", extra={**context, "outcome": "committed" if committed else "dry_run",
                                                "entities": counts})
    return JSONResponse(
        status_code=200,
        content={
            "dryRun": dry_run,
            "committed": committed,
            "book": BookResponse.model_validate(result.book).model_dump(mode="json"),
            "chapters": (
                ChapterResponse.model_validate(result.chapters).model_dump(mode="json")
                if result.chapters else None
            ),
            "entities": result.entities,
        },
    )


# ============================================================
# CLI: Generate a seed secret
# ============================================================
# Usage: python -m app.api.routes.internal.router

if __name__ == "__main__":
    token = secrets.token_urlsafe(TOKEN_BYTES)
    hashed = hash_secret(token)
    print()
    print("=== Libex Seed Secret Generator ===")
    print()
    print("  Your token (use in Authorization header — save this, it cannot be recovered):")
    print(f"  {token}")
    print()
    print("  SEED_SECRET (set this in Portainer/env):")
    print(f"  {hashed}")
    print()
    print("  INGEST_SECRET (a separate token and digest; never reuse the seed token):")
    ingest_token = secrets.token_urlsafe(TOKEN_BYTES)
    print(f"  token:  {ingest_token}")
    print(f"  digest: {hash_secret(ingest_token)}")
    print()
    print("  Usage:")
    print('  curl -X POST https://libexdb.com/internal/seed/narrators \\')
    print(f'    -H "Authorization: Bearer {token}" \\')
    print('    -H "Content-Type: application/json" \\')
    print('    -d @scrapers/narratorlist/output/narrators.json')
    print()
