# Standard library
from contextlib import asynccontextmanager
import asyncio

# Third party
from alembic import command
from alembic.config import Config
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse


# Core
from app.core.config import get_settings
from app.core.logging import setup_logging
from app.core.exceptions import LibexException
from app.core.middleware import setup_middleware


# Database
from app.db.session import engine

# Services
from app.services.seeder import run_seeder, SessionFactory
from app.services.cache.manager import purge_expired

# Routes
from app.api.routes.books import router as books_router
from app.api.routes.authors import router as authors_router
from app.api.routes.narrators import router as narrators_router
from app.api.routes.series import router as series_router
from app.api.routes.search import router as search_router
from app.api.routes.db import router as db_router

# ============================================================
# SETTINGS & LOGGING
# ============================================================

settings = get_settings()
logger = setup_logging()


# ============================================================
# BACKGROUND TASKS
# ============================================================

async def _cache_purge_loop():
    """Purges expired cache entries every hour."""
    while True:
        await asyncio.sleep(3600)
        try:
            async with SessionFactory() as session:
                count = await purge_expired(session)
                if count:
                    logger.info(f"Cache purge: removed {count} expired entries")
        except Exception as e:
            logger.warning(f"Cache purge failed: {e}")

# ============================================================
# APPLICATION
# ============================================================

openapi_tags = [
    {"name": "Books", "description": "Retrieve book metadata by ASIN, ISBN, or bulk ASINs"},
    {"name": "Authors", "description": "Retrieve author metadata and their books"},
    {"name": "Narrators", "description": "Retrieve books by narrator name"},
    {"name": "Series", "description": "Retrieve series metadata and their books"},
    {"name": "Search", "description": "Search Audible catalog by title, author, or keyword"},
    {"name": "Database", "description": "Query the local indexed book library without hitting Audible"},
]

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    try:
        alembic_cfg = Config("alembic.ini")
        await asyncio.to_thread(command.upgrade, alembic_cfg, "head")
        logger.info("Database migrations applied")
    except Exception as e:
        logger.warning(f"Database unavailable on startup: {e}")
    logger.info(f"Libex {settings.app_version} starting up")

    # Start background tasks
    seeder_task = asyncio.create_task(run_seeder())
    purge_task = asyncio.create_task(_cache_purge_loop())

    yield

    # Shutdown
    seeder_task.cancel()
    purge_task.cancel()
    await engine.dispose()
    logger.info("Libex shutting down")


app = FastAPI(
    title="Libex",
    description="Open, unrestricted Audible metadata API for the audiobook automation community.",
    version=settings.app_version,
    openapi_tags=openapi_tags,
    lifespan=lifespan,
)

# ============================================================
# EXCEPTION HANDLERS
# ============================================================

@app.exception_handler(LibexException)
async def libex_exception_handler(request: Request, exc: LibexException) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.message, "status_code": exc.status_code},
    )

@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.error(f"Unhandled exception: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"error": "Internal server error", "status_code": 500},
    )

# ============================================================
# MIDDLEWARE
# ============================================================

setup_middleware(app)

# ============================================================
# ROUTERS
# ============================================================

app.include_router(books_router)
app.include_router(authors_router)
app.include_router(narrators_router)
app.include_router(series_router)
app.include_router(search_router)
app.include_router(db_router)

# ============================================================
# HEALTH CHECK
# ============================================================

@app.get("/health", tags=["System"])
async def health():
    return {"status": "ok", "version": settings.app_version}

# ============================================================
# DOCS REDIRECT
# ============================================================

@app.get("/", include_in_schema=False)
async def root_redirect():
    return RedirectResponse(url="/docs")


@app.get("/api-docs", include_in_schema=False)
async def api_docs_redirect():
    return RedirectResponse(url="/docs")