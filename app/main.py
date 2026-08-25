# Standard library
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
import asyncio

# Third party
from fastapi import FastAPI, Request
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles


# Core
from app.core.config import get_settings, check_retired_env_vars
from app.core.logging import setup_logging, stop_axiom_listener
from app.core.exceptions import LibexException
from app.core.middleware import setup_middleware
from app.core.migration_notice import build_migration_notice, is_new_host_request, MIGRATION_HEADER_NAMES
from app.core.response_headers import HEADER_REQUEST_ID, EXPOSED_HEADER_NAMES


# Database
from app.db.session import engine

# Services
from app.services.seeder import run_seeder, run_new_releases_seeder, SessionFactory
from app.services.cache.manager import purge_expired

# Routes
from app.api.routes.books import router as books_router
from app.api.routes.authors import router as authors_router
from app.api.routes.narrators import router as narrators_router
from app.api.routes.series import router as series_router
from app.api.routes.search import router as search_router
from app.api.routes.releases import router as releases_router
from app.api.routes.db import router as db_router
from app.api.routes.internal import router as internal_router

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
    {"name": "Releases", "description": "New releases and upcoming books, scanned live from Audible"},
    {"name": "Database", "description": "Query the local indexed book library without hitting Audible"},
]

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup. Migrations are not run here: the container entrypoint applies
    # them once, before it execs uvicorn. Lifespan runs once per worker
    # process, and alembic takes no lock of its own, so running the upgrade
    # from here would have every worker race the same DDL — with the losers
    # failing on already-applied statements. Nothing below assumes the schema
    # is current: a start against an unmigrated or unreachable database still
    # boots and serves, and the failure surfaces per request rather than
    # aborting startup.
    logger.info(f"Libex {settings.app_version} starting up")

    # Warn about any retired env vars still set (never crashes)
    check_retired_env_vars()

    # Start background tasks
    seeder_task = asyncio.create_task(run_seeder())
    new_releases_task = asyncio.create_task(run_new_releases_seeder())
    purge_task = asyncio.create_task(_cache_purge_loop())

    yield

    # Shutdown
    seeder_task.cancel()
    new_releases_task.cancel()
    purge_task.cancel()
    await engine.dispose()
    logger.info("Libex shutting down")
    # Last, so the line above is still drained: stop() flushes the queue before
    # the shipping thread exits. Deterministic here in a way atexit is not — a
    # container stop runs lifespan shutdown, and buffered lines survive it.
    stop_axiom_listener()


# Off by default for self-hosters (migration_notice_enabled is False, migration_new_host
# is empty) — only the public instance's env sets these. Built exactly once, here, and
# threaded to every other consumer (setup_middleware, /health, the generic exception
# handler) so none of them can derive a looser condition and render a notice the others
# stayed silent on.
migration_notice = build_migration_notice(settings)

_BASE_DESCRIPTION = """Open, unrestricted Audible metadata API for the audiobook automation community.

## Response headers

Every response carries **`X-Request-Id`** — an id minted fresh for that response
alone, never taken from anything the caller sent. Quote it back when reporting
a problem.

The book, series and author routes also report on the data in the body:

- **`X-Libex-Source`** — where the elements in the body came from: `audible`,
  `cache`, or `db` alone, or `mixed; audible=2; cache=1` when more than one
  contributed. Not sent by the two `/author/books` routes, which carry
  `X-Libex-Complete` below but never this one.
- **`X-Libex-Complete`** — whether the body holds every element that was
  asked for. `true` means it does; `false` means at least one requested
  element is missing — not that something merely went wrong internally along
  the way.
- **`X-Libex-Incomplete-Reason`** — present only when `X-Libex-Complete` is
  `false`:

  | Reason | What happened | Worth retrying? |
  |---|---|---|
  | `hydration-not-found` | Audible has no record of the ASIN, or answered with a titleless placeholder instead of a book — also covers a title Audible returns in full but hasn't released yet | No for a genuinely missing ASIN; an unreleased one may appear once it's out |
  | `hydration-failed` | Libex couldn't reach Audible for part of the request, and neither its stored DB copy nor its cache covered the gap | Yes, once Audible is reachable again |
  | `hydration-deadline` | The request ran out of its time budget before every element was fetched | Not currently emitted by any route — the one caller that imposes such a deadline reports completeness through a separate, coarser check instead |
  | `discovery-incomplete` | Reserved for a catalogue walk that ends before it finishes enumerating what exists | Not currently emitted by any route |

All four headers are exposed through CORS, so browser JavaScript can read them
directly off the response.

### What a response actually tells you

- **200 with `X-Libex-Complete: true`** — the body has everything that was
  asked for.
- **200 with `X-Libex-Complete: false`** — still a real, usable answer, just
  short one or more of the requested elements.
- **404** is not a quieter version of an incomplete 200. On a single-entity
  route (`/book/{asin}`, `/series/{asin}`, `/author/{asin}`), a missing
  element fails the whole request rather than returning a partial body —
  which is why those routes always read `true` at 200.
- On the bulk `/book` route, ASINs Audible didn't have are listed in
  **`notFound`**, computed before filtering — a book that was found and then
  removed by a filter parameter is never reported as missing.
- The two series-books routes (`/series/books/{asin}`, `/series/{asin}/books`)
  carry **no `notFound` field at all** — `X-Libex-Complete` and
  `X-Libex-Incomplete-Reason` are the only signal that a book is missing.

## Caching

The book, series and author routes default to `cache=true`, serving Libex's
stored copy when one exists. Pass `cache=false` on any of them to force a
live Audible fetch instead."""

if migration_notice is not None:
    # Served from both hostnames off one description built at import time, so this can't
    # say "this host" or "this instance" — read from libexdb.com, "this" would name the
    # host the reader is already on instead of the one being retired.
    openapi_description = (
        "## Migration notice\n\n"
        f"**Libex's public instance has moved to {migration_notice.new_host}.**\n\n"
        f"The previous host remains available until {migration_notice.sunset_display}, after which "
        "it stops serving.\n"
        "If you are using the old address, update your configuration.\n"
    )
    if migration_notice.info_url:
        openapi_description += f"\n[Details and questions]({migration_notice.info_url})\n"
    openapi_description += f"\n---\n\n{_BASE_DESCRIPTION}"
else:
    openapi_description = _BASE_DESCRIPTION

app = FastAPI(
    title="Libex",
    description=openapi_description,
    version=settings.app_version,
    openapi_tags=openapi_tags,
    lifespan=lifespan,
    # Unset (the self-hosted default) leaves `servers` out entirely, so OpenAPI tooling
    # infers the base URL from wherever it fetched the spec — a self-hoster's own server.
    # Hardcoding the new host here would send a self-hoster's "Try it out" requests and
    # generated SDKs at the public instance instead of their own.
    servers=[{"url": migration_notice.new_host}] if migration_notice is not None else None,
    # The interactive docs are served from assets Libex ships, not from a CDN
    # -- see the custom /docs and /redoc routes below. Disabling the built-in
    # routes here is what frees those paths for them.
    docs_url=None,
    redoc_url=None,
)

# ============================================================
# DOCS (self-hosted assets)
# ============================================================

# FastAPI's defaults have the browser fetch Swagger UI and ReDoc from
# cdn.jsdelivr.net, a favicon from fastapi.tiangolo.com, and -- ReDoc's
# template only, and only while with_google_fonts is left at its default --
# a stylesheet from fonts.googleapis.com. Opening the docs therefore sent a
# visitor's real IP address to three third parties. Libex records nothing that
# identifies a caller, and that has to hold for the pages it serves and not
# only for the lines it writes, so the assets are served from here instead.
#
# scripts/fetch_docs_assets.sh pulls them at build time against pinned
# versions and checksums, and neuters the logo the ReDoc bundle would
# otherwise fetch from Redocly as it renders. If it has not been run, both
# pages come up completely blank rather than merely unstyled: each template is
# an empty container plus a script tag, so a missing bundle leaves nothing to
# mount. That failure is total and local, which is still preferable to a
# silent fallback to the CDN that looks like it worked.
_STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

# The logo mark, square-padded, at 32px rather than the conventional 16: the
# mark is three elements and 16px renders them as mush, while browsers take the
# 32 for retina and downscale it for standard displays.
_FAVICON = "/static/favicon.png"

# ReDoc renders the logo from `info.x-logo` in the OpenAPI document itself, and
# FastAPI exposes no constructor argument for it, so the document has to be
# reached after it is built.
#
# The usual recipe for that re-derives the whole document with get_openapi(),
# which means restating every argument the FastAPI() call above passes. Miss one
# and the served spec loses it with nothing to show for it: drop `servers` and
# the public instance's spec stops naming the new host, so during a migration
# "Try it out" and generated SDKs fire at whichever host served the spec —
# including the one being retired; drop `openapi_tags` and every tag
# description empties. Adding the key to the document FastAPI already built
# cannot lose anything, because it never re-derives it.
#
# Delegating to the original method also keeps FastAPI's cache: that method
# populates app.openapi_schema on first call and returns the same dict every
# time after, so the document is built once rather than per request, and the
# key is added to the object that is cached.
#
# The logo `info.x-logo` names is Libex's own: logo.png is the dark-on-light
# artwork, which is the one that reads against ReDoc's near-white (#fafafa)
# sidebar. The bundle has no prefers-color-scheme handling, so ReDoc cannot
# select between the two files the way the README's <picture> does — this one
# is pinned regardless of the visitor's theme.
_fastapi_openapi = app.openapi


def _openapi_with_logo() -> dict[str, Any]:
    schema = _fastapi_openapi()
    schema["info"]["x-logo"] = {"url": "/static/logo.png", "altText": "Libex"}
    return schema


app.openapi = _openapi_with_logo


@app.get("/docs", include_in_schema=False)
async def swagger_ui() -> HTMLResponse:
    # Swagger UI's own default for validatorUrl is validator.swagger.io, and
    # FastAPI does not override it; the OnlineValidatorBadge that reads it would
    # have the visitor's browser fetch that host. Nothing reaches it from the
    # pinned bundle -- the badge is registered in the component map but never
    # retrieved from it, so it never mounts and the default sits inert. Setting
    # it to null anyway closes the vector by construction, so a later version
    # bump that does mount the badge cannot quietly reintroduce the request.
    # Belt and braces rather than a fix: None serialises to JS null, which the
    # option accepts and the badge's own guard reads as "no validator".
    #
    # Swagger UI ignores the `info.x-logo` key ReDoc takes its logo from, so on
    # this page the logo is placed by CSS instead. The stylesheet is an argument
    # to this function, which is what lets Libex own one without forking the
    # page around it; it @imports the fetched swagger-ui.css, so the bundle is
    # still styled by the sheet that shipped with it.
    return get_swagger_ui_html(
        openapi_url=app.openapi_url,
        title=f"{app.title} - Swagger UI",
        swagger_js_url="/static/docs/swagger-ui-bundle.js",
        swagger_css_url="/static/swagger-libex.css",
        swagger_favicon_url=_FAVICON,
        swagger_ui_parameters={"validatorUrl": None},
    )


@app.get("/redoc", include_in_schema=False)
async def redoc() -> HTMLResponse:
    # with_google_fonts=False is what makes every URL in the emitted page
    # same-origin; the other three are pointed at Libex by the arguments above
    # it. Upstream's template is used rather than a local copy so its
    # <noscript> notice keeps reaching visitors who would otherwise be handed a
    # blank page with no explanation.
    return get_redoc_html(
        openapi_url=app.openapi_url,
        title=f"{app.title} - ReDoc",
        redoc_js_url="/static/docs/redoc.standalone.js",
        redoc_favicon_url=_FAVICON,
        with_google_fonts=False,
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
    # Read once, up front, and reused below for both the log line and the
    # header rather than each computing its own -- an id on the header that
    # names no log line is worse than no header at all, since an operator or
    # a bug report goes looking for a line that was never written, and a
    # header naming a different id than the log line would be worse still.
    # This is the one and only read: request.state is shared across every
    # Request object built from the same ASGI scope, so what LoggingMiddleware
    # set on it before call_next survives even though this handler runs in
    # ServerErrorMiddleware, entirely outside that middleware. getattr rather
    # than a direct attribute read: a 500 raised before LoggingMiddleware's
    # dispatch ever ran reaches this handler with no request_id set, and that
    # is one log line and one response with a field missing, not a second
    # failure stacked on the first -- never a fresh uuid minted here to paper
    # over that gap, which would silently produce the exact mismatch this
    # comment exists to avoid.
    request_id = getattr(request.state, "request_id", None)
    logger.error(
        f"Unhandled exception: {exc}",
        exc_info=True,
        extra={"request_id": request_id} if request_id is not None else {},
    )
    response = JSONResponse(
        status_code=500,
        content={"error": "Internal server error", "status_code": 500},
    )
    # ServerErrorMiddleware, which invokes this handler, sits outside every
    # middleware added in setup_middleware — so MigrationNoticeMiddleware never
    # sees this response. Apply the same headers here instead of leaving a
    # 500 (exactly when a moving consumer most needs the pointer) silently
    # bypass the notice.
    if migration_notice is not None and not is_new_host_request(migration_notice, request.url.hostname):
        response.headers.update(migration_notice.headers)

    if request_id is not None:
        response.headers[HEADER_REQUEST_ID] = request_id

    # ServerErrorMiddleware sits outside CORSMiddleware too, so an unhandled
    # 500 otherwise carries no CORS headers at all — a browser discards the
    # entire response before any JS reaches the body, let alone a header.
    # Exposing headers without also allowing the origin would change
    # nothing, so both are set by hand here, the same way the migration
    # headers above already have to be.
    #
    # Restricted to requests that actually carried an Origin header: a
    # same-origin page and a non-browser caller (curl, a server-side fetch)
    # never send one and were never going to hit a CORS check in the first
    # place, so stamping this on every 500 regardless would be pure noise.
    # The exposed set mirrors what setup_middleware exposes on every other
    # response, so a browser sees the same allowlist whether the request
    # succeeded or failed.
    if request.headers.get("origin") is not None:
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Expose-Headers"] = ", ".join(
            dict.fromkeys((*EXPOSED_HEADER_NAMES, *MIGRATION_HEADER_NAMES))
        )
    return response

# ============================================================
# MIDDLEWARE
# ============================================================

setup_middleware(app, migration_notice)

# ============================================================
# ROUTERS
# ============================================================

app.include_router(books_router)
app.include_router(authors_router)
app.include_router(narrators_router)
app.include_router(series_router)
app.include_router(search_router)
app.include_router(releases_router)
app.include_router(db_router)
app.include_router(internal_router)

# ============================================================
# HEALTH CHECK
# ============================================================

@app.get("/health", tags=["System"])
async def health(request: Request):
    payload = {"status": "ok", "version": settings.app_version}
    # Not part of the AudiMeta DTO contract, so an additive field here is safe — unlike the
    # book/author/series/search responses, where an unknown key can hard-break a strict
    # (e.g. Jackson-based) consumer mid-migration. Suppressed on the new host itself, so
    # libexdb.com never tells its own consumers that it stops serving.
    if migration_notice is not None and not is_new_host_request(migration_notice, request.url.hostname):
        payload["notice"] = migration_notice.health_message
    return payload

# ============================================================
# DOCS REDIRECT
# ============================================================

@app.get("/", include_in_schema=False)
async def root_redirect():
    return RedirectResponse(url="/docs")


@app.get("/api-docs", include_in_schema=False)
async def api_docs_redirect():
    return RedirectResponse(url="/docs")