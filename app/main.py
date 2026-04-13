# Standard library
from contextlib import asynccontextmanager
import asyncio

# Third party
from alembic import command
from alembic.config import Config
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import inspect
from sqlalchemy import create_engine as sync_create_engine


# Core
from app.core.config import get_settings
from app.core.logging import setup_logging
from app.core.exceptions import LibexException
from app.core.middleware import setup_middleware


# Database
from app.db.session import engine

# Routes
from app.api.routes.books import router as books_router
from app.api.routes.authors import router as authors_router
from app.api.routes.series import router as series_router
from app.api.routes.search import router as search_router

# ============================================================
# SETTINGS & LOGGING
# ============================================================

settings = get_settings()
logger = setup_logging()

# ============================================================
# APPLICATION
# ============================================================

openapi_tags = [
    {"name": "Books", "description": "Retrieve book metadata by ASIN, ISBN, or bulk ASINs"},
    {"name": "Authors", "description": "Retrieve author metadata and their books"},
    {"name": "Series", "description": "Retrieve series metadata and their books"},
    {"name": "Search", "description": "Search Audible catalog by title, author, or keyword"},
]

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    try:
        async def run_migrations():
            alembic_cfg = Config("alembic.ini")

            engine_sync = sync_create_engine(settings.database_url.replace("+asyncpg", "+psycopg2"))
            inspector = inspect(engine_sync)
            has_alembic = "alembic_version" in inspector.get_table_names()
            has_cache = "cache" in inspector.get_table_names()
            engine_sync.dispose()

            if has_cache and not has_alembic:
                command.stamp(alembic_cfg, "9203c248b749")

            command.upgrade(alembic_cfg, "head")

        await asyncio.to_thread(run_migrations)
        logger.info("Database migrations applied")
    except Exception as e:
        logger.warning(f"Database unavailable on startup: {e}")

    logger.info(f"Libex {settings.app_version} starting up")
    yield
    # Shutdown
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
app.include_router(series_router)
app.include_router(search_router)


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