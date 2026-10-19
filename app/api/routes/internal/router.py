"""
Internal seed endpoint.
Completely undocumented. Requires SEED_SECRET env var to be set.
Used to upload enrichment data from local scrapers to the production DB.

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
from sqlalchemy import select, update, func

# Database
from app.db.models import Narrator
from app.db.session import AsyncSessionFactory

# Core
from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger()
settings = get_settings()

router = APIRouter(prefix="/internal", include_in_schema=False)

TOKEN_BYTES = 32
TOKEN_LENGTH = 43  # urlsafe base64 of 32 bytes, unpadded
MAX_BODY_BYTES = 32 * 1024 * 1024

_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{43}")
_SECRET_RE = re.compile(r"[0-9a-f]{64}")


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


def _seed_authorized(authorization: str | None) -> bool:
    """Shape check first, hash only for a well-formed token."""
    stored = settings.seed_secret
    if not stored:
        return False
    if not _secret_is_usable(stored):
        return False
    token = _extract_token(authorization)
    if token is None:
        return False
    return verify_secret(token, stored)


if settings.seed_secret and not _secret_is_usable(settings.seed_secret):
    logger.warning(
        "SEED_SECRET is not a SHA-256 hex digest (an old PBKDF2 value?); "
        "the seed endpoint is disabled until it is regenerated with "
        "python -m app.api.routes.internal.router"
    )


async def _read_capped_body(request: Request) -> bytes | None:
    """Reads the body, returning None once it passes MAX_BODY_BYTES."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        return None
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_BODY_BYTES:
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
        name = entry.get("name")
        if not name:
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
    print("  Usage:")
    print('  curl -X POST https://libexdb.com/internal/seed/narrators \\')
    print(f'    -H "Authorization: Bearer {token}" \\')
    print('    -H "Content-Type: application/json" \\')
    print('    -d @scrapers/narratorlist/output/narrators.json')
    print()
