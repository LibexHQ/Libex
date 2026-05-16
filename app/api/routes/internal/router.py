"""
Internal seed endpoint.
Completely undocumented. Requires SEED_SECRET env var to be set.
Used to upload enrichment data from local scrapers to the production DB.

SECURITY:
- SEED_SECRET stores a PBKDF2-SHA256 hash, never the plaintext token.
- The plaintext token is sent via Authorization header and verified
  against the stored hash using constant-time comparison.
- Empty SEED_SECRET = endpoint fully disabled (returns 401).
- include_in_schema=False = invisible in OpenAPI docs.
- Generate a secret with: python -m app.api.routes.internal.router
"""

# Standard library
import base64
import hashlib
import hmac
import secrets
from datetime import datetime, timezone
from typing import Any

# Third party
from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select, update, func
from sqlalchemy.ext.asyncio import AsyncSession

# Database
from app.db.models import Narrator
from app.db.session import get_session

# Core
from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger()
settings = get_settings()

router = APIRouter(prefix="/internal", include_in_schema=False)

PBKDF2_ITERATIONS = 600_000


# ============================================================
# CRYPTO HELPERS
# ============================================================

def hash_secret(plaintext: str) -> str:
    """
    Hashes a plaintext secret using PBKDF2-SHA256 with a random salt.
    Returns base64-encoded salt+key for storage in env var.
    """
    salt = secrets.token_bytes(32)
    key = hashlib.pbkdf2_hmac("sha256", plaintext.encode(), salt, PBKDF2_ITERATIONS)
    return base64.b64encode(salt + key).decode()


def verify_secret(plaintext: str, stored_hash: str) -> bool:
    """
    Verifies a plaintext secret against a stored PBKDF2 hash.
    Uses constant-time comparison to prevent timing attacks.
    """
    try:
        decoded = base64.b64decode(stored_hash)
        if len(decoded) < 64:
            return False
        salt = decoded[:32]
        stored_key = decoded[32:]
        key = hashlib.pbkdf2_hmac("sha256", plaintext.encode(), salt, PBKDF2_ITERATIONS)
        return hmac.compare_digest(key, stored_key)
    except Exception:
        return False


def _verify_seed_auth(authorization: str = Header(None)) -> bool:
    """Verifies the bearer token against the stored PBKDF2 hash."""
    if not settings.seed_secret:
        return False
    if not authorization:
        return False
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return False
    return verify_secret(parts[1], settings.seed_secret)


@router.post("/seed/narrators")
async def seed_narrators(
    request: Request,
    session: AsyncSession = Depends(get_session),
    authorization: str = Header(None),
) -> JSONResponse:
    """
    Accepts a JSON array of narrator enrichment data.
    Matches by name against existing narrators in the DB.
    Updates profile fields only — never creates new narrators.
    """
    if not _verify_seed_auth(authorization):
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})

    try:
        data = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "Invalid JSON"})

    if not isinstance(data, list):
        return JSONResponse(status_code=400, content={"error": "Expected JSON array"})

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

    logger.info("Seed: narrator enrichment complete", extra=stats)

    return JSONResponse(
        status_code=200,
        content={"status": "ok", **stats},
    )


# ============================================================
# CLI: Generate a seed secret
# ============================================================
# Usage: python -m app.api.routes.internal.router

if __name__ == "__main__":
    token = secrets.token_urlsafe(48)
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
    print('  curl -X POST https://libex.lostcartographer.xyz/internal/seed/narrators \\')
    print(f'    -H "Authorization: Bearer {token}" \\')
    print('    -H "Content-Type: application/json" \\')
    print('    -d @scrapers/narratorlist/output/narrators.json')
    print()