# Standard library
from collections.abc import AsyncGenerator

# Third party
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Local
from app.core.config import get_settings

settings = get_settings()

# Six uvicorn workers per deployment, and this module is imported once per
# worker process, so both limits below are PER PROCESS: the number that has to
# fit Postgres is the limit multiplied by the worker count. Within a process
# this pool is the only consumer of Postgres connections for the API, the
# seeder loop, and every background writer task.
#
# The budget it is sized against: Postgres runs with max_connections=200
# (docker-compose.yml sets it explicitly on the postgres command) and
# PostgreSQL 16 holds back superuser_reserved_connections (3) plus
# reserved_connections (0), leaving 197 for the libex role. pool_size +
# max_overflow caps each process at 20, so six workers hold 120 at full
# stretch, 77 clear of the role's share. The aggregate is the invariant and
# the per-process figure moves with WEB_CONCURRENCY (docker-compose.yml), the
# one place the worker count is set; raising that without lowering these
# spends another 20 connections per added process. What sets that count is
# CPU, not Postgres — one event loop per process against 12 cores on the host,
# with the measurements behind it recorded in docker-compose.yml. This pool is
# a constraint the count has to fit, never the reason it is what it is.
#
# What consumes a slot is an open transaction, not a request and not a
# session. get_session is a request-scoped FastAPI dependency, but an
# AsyncSession is lazy: created and untouched it checks out nothing, so the 41
# Depends(get_session) sites are that many potential borrowers rather than that
# many held connections. The checkout happens on the first statement and lasts
# until commit, rollback or close, and SQLAlchemy re-acquires transparently on
# the next statement.
#
# So a path that releases before a slow await costs the pool nothing while it
# waits, and every path that goes out to Audible releases first: the hydration
# fan-out (services/audible/books.py), the series-books fetch
# (services/audible/series.py), and both the profile fetch and the discovery
# walk (services/audible/authors) each roll back after their cache and DB
# reads and before the outbound work. No request holds a connection for the
# length of an Audible call — not the chunk fan-out an author-ASIN route
# drives with high_concurrency=True under the 25-permit author-books pool
# (services/audible/client.py), and not the walk that runs to
# AUTHOR_BOOKS_TIME_BUDGET_SECONDS (45s). A worker's 20 slots therefore serve
# far more than 20 concurrent requests.
#
# server_settings apply per-connection at the driver level: statement_timeout
# bounds any single query so a runaway statement can't pin a connection
# indefinitely, and idle_in_transaction_session_timeout is the net under those
# releases: a session left idle-in-transaction across a slow network await
# pins the xmin horizon and blocks autovacuum on a 1.1M-row, write-heavy
# table, so one that nothing has released ends at 60s instead of lasting as
# long as the await does.
#
# hide_parameters keeps caller-supplied search text out of the logs. Every
# search filter is a bound parameter, and SQLAlchemy renders a statement's
# bound parameters into str() on any StatementError -- which every DBAPIError,
# OperationalError and DataError is a subclass of. So a query that times out
# against the 30s statement_timeout above, or fails for any other reason, would
# otherwise print what the caller typed as part of the exception text, no matter
# how carefully the surrounding log message avoids it. This suppresses that
# rendering for every statement, including ones not yet written. The compiled
# SQL and the driver's own message are still logged, so a failure is still
# diagnosable down to the statement that caused it; only the literal values go.
engine = create_async_engine(
    settings.database_url,
    echo=settings.database_echo,
    hide_parameters=True,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=10,
    pool_timeout=30,
    connect_args={
        "server_settings": {
            "statement_timeout": "30000",
            "idle_in_transaction_session_timeout": "60000",
        }
    },
)

AsyncSessionFactory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that provides a database session."""
    async with AsyncSessionFactory() as session:
        yield session