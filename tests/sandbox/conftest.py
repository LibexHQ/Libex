"""
Harness for the sandbox suite that tests/sandbox/run.sh drives.

Everything here talks to the throwaway stack run.sh built: the API over HTTP,
Postgres and docker through the compose invocation run.sh exported. Nothing
touches the real libex project. The suite is collect-ignored by the root
conftest unless LIBEX_SANDBOX_URL is set, so the unit and integration runs
never see it.

Rules the helpers keep, because the log greps and the leak checks depend on
them:
- Tokens are read from os.environ at the moment a header is built and are
  never stored, parametrized on, or put in an assert.
- Every canary starts SBXCANARY- and is written into a request body, never a
  log line; the user agent starts SBXUA- and must appear in the logs.
- Docker and psql go through shlex.split(LIBEX_SANDBOX_COMPOSE) only.
- Audible calls are paced one second apart and counted from the app's own log
  lines, which all six workers write; a 429 or 403 aborts the whole run.
"""

# Standard library
import contextlib
import json
import os
import re
import secrets
import shlex
import subprocess
import time
import uuid

# Third party
import httpx
import pytest

MODE = os.environ.get("LIBEX_SANDBOX_MODE", "empty")
BUDGET = int(os.environ.get("LIBEX_SANDBOX_AUDIBLE_BUDGET", "120"))
REGIONS = ("us", "uk", "ca", "au", "de", "fr", "it", "es", "jp", "in", "br")
CANARY = "SBXCANARY-"

# Snapshot-only cases live in their own file so a run in the other mode never
# collects them: a skip would fail run.sh.
collect_ignore = [] if MODE == "snapshot" else ["test_90_snapshot.py"]

_THROTTLED = re.compile(r"status_code: (429|403)|returned (429|403)")
# One title per marketplace that Audible lists there, for finding a live ASIN.
_TITLES = {"us": "the hobbit", "uk": "the hobbit", "ca": "the hobbit", "au": "the hobbit",
           "de": "der hobbit", "fr": "le hobbit", "it": "lo hobbit", "es": "el hobbit",
           "jp": "ハリー・ポッター", "in": "the alchemist", "br": "o hobbit"}


def pytest_collection_modifyitems(items):
    here = os.path.dirname(__file__)
    for item in items:
        if not str(item.fspath).startswith(here):
            continue
        item.add_marker(pytest.mark.sandbox)
        # pytest.ini's 30s is for in-process tests; these cross a network and a container.
        if item.get_closest_marker("timeout") is None:
            item.add_marker(pytest.mark.timeout(300))


@pytest.fixture(autouse=True)
def block_network_sockets():
    """Overrides the root guard, which refuses the real sockets this suite exists to use."""
    yield


class Sandbox:
    """One handle on the stack: HTTP, SQL, docker, canaries and the Audible budget."""

    def __init__(self):
        self.url = os.environ["LIBEX_SANDBOX_URL"]
        self.mode = MODE
        self.ua = "SBXUA-" + uuid.uuid4().hex[:12]
        self.http = httpx.Client(
            base_url=self.url, timeout=httpx.Timeout(180.0), headers={"User-Agent": self.ua}
        )
        self.canaries: list[str] = []
        self.gets = 0
        self._last_audible = 0.0
        self._asins: set[str] = set()
        self._path_proven = False
        self._live: dict[tuple[str, int], str] = {}

    # ------------------------------------------------------------ docker / sql

    def compose(self, *args, env_file=None, timeout=420, check=True, stdin=None):
        cmd = shlex.split(os.environ["LIBEX_SANDBOX_COMPOSE"])
        if env_file is not None:
            cmd += ["--env-file", str(env_file)]
        done = subprocess.run(
            [*cmd, *args], capture_output=True, text=True, timeout=timeout, input=stdin
        )
        if check and done.returncode != 0:
            raise AssertionError(f"compose {args[0]} exited {done.returncode}: {done.stderr[-600:]}")
        return done

    def psql(self, sql: str, timeout=300) -> str:
        done = self.compose(
            "exec", "-T", "db", "psql", "-U", "libex", "-d", "libex", "-v", "ON_ERROR_STOP=1",
            "-Atq", "-c", sql, timeout=timeout,
        )
        return done.stdout.strip()

    def app_logs(self) -> str:
        return self.compose("exec", "-T", "api", "sh", "-c", "cat /app/logs/*").stdout

    def container_logs(self) -> str:
        done = self.compose("logs", "--no-color", "api", "db")
        return done.stdout + done.stderr

    def probe_count(self) -> int:
        return self.app_logs().count("Probed Audible listing")

    @contextlib.contextmanager
    def api_env(self, tmp_path, **env):
        """Recreates api with extra compose variables, and puts it back on exit."""
        path = tmp_path / "override.env"
        path.write_text("".join(f"{k}={v}\n" for k, v in env.items()))
        path.chmod(0o600)
        self.compose("up", "-d", "--wait", "--wait-timeout", "300", "api", env_file=path)
        try:
            yield
        finally:
            self.compose("up", "-d", "--wait", "--wait-timeout", "300", "api")

    # ----------------------------------------------------------------- audible

    def audible(self, kind: str = "probe", planned: int = 1):
        """
        Call before a request that reaches Audible: paces one second apart,
        stops the run on a 429 or 403 already in the logs, and stops it when
        this call would pass the budget. Probes are counted from the log
        lines; plain GETs from this handle.
        """
        gap = time.monotonic() - self._last_audible
        if gap < 1.0:
            time.sleep(1.0 - gap)
        logs = self.app_logs()
        if _THROTTLED.search(logs):
            pytest.exit("Audible throttled or refused (429/403); aborting the run", returncode=3)
        spent = logs.count("Probed Audible listing") + self.gets
        if spent + planned > BUDGET:
            pytest.exit(f"Audible budget {BUDGET} would be passed (spent {spent})", returncode=3)
        if kind == "get":
            self.gets += planned
        self._last_audible = time.monotonic()

    # ------------------------------------------------------------------- data

    def canary(self) -> str:
        value = CANARY + secrets.token_hex(8)
        self.canaries.append(value)
        return value

    def fake_asin(self) -> str:
        while True:
            asin = "B0SBX" + secrets.token_hex(3).upper()[:5]
            if asin not in self._asins:
                self._asins.add(asin)
                return asin

    def book(self, asin=None, region="us", **over) -> dict:
        """A valid book body, with a canary in the description and names unique to the ASIN."""
        asin = asin or self.fake_asin()
        book = {
            "asin": asin,
            "region": region,
            "title": f"SBX Title {asin}",
            "language": "english",
            "releaseDate": "2019-04-02",
            "description": self.canary(),
            "authors": [{"name": f"SBX Author {asin}"}],
        }
        book.update(over)
        return {"book": book}

    @staticmethod
    def chapters() -> dict:
        return {"runtimeLengthMs": 90000, "chapters": [
            {"title": "One", "lengthMs": 60000, "startOffsetMs": 0},
            {"title": "Two", "lengthMs": 30000, "startOffsetMs": 60000},
        ]}

    # ------------------------------------------------------------------- http

    @staticmethod
    def auth(kind: str | None = "ingest") -> dict:
        """Built here, per call, from the environment. kind names a shape, never a value."""
        ingest = os.environ["LIBEX_SANDBOX_TOKEN"]
        seed = os.environ["LIBEX_SANDBOX_SEED_TOKEN"]
        shapes = {
            None: None,
            "ingest": "Bearer " + ingest,
            "seed": "Bearer " + seed,
            "wrong": "Bearer " + secrets.token_urlsafe(32),
            "basic_scheme": "Basic " + ingest,
            "no_scheme": ingest,
            "truncated": "Bearer " + ingest[:-1],
            "extended": "Bearer " + ingest + "x",
            "bearer_alone": "Bearer",  # h11 refuses a trailing space client-side
        }
        value = shapes[kind]
        return {} if value is None else {"Authorization": value}

    def post(self, body, *, region="us", dry_run=True, auth="ingest", audible=True, params=None,
             content=None, headers=None) -> httpx.Response:
        """
        POST /internal/books. A body that gets past validation reaches the
        Audible probe, so audible=True (the default) paces and counts it; a
        case that is refused earlier passes audible=False.
        """
        q = {"dry_run": str(dry_run).lower()}
        if region is not None:
            q["region"] = region
        q.update(params or {})
        if audible:
            self.audible("probe")
        data = content if content is not None else json.dumps(body, ensure_ascii=False).encode()
        resp = self.http.post(
            "/internal/books", params=q, content=data,
            headers={"Content-Type": "application/json", **self.auth(auth), **(headers or {})},
        )
        if audible:
            self._last_audible = time.monotonic()
        return resp

    def get_live(self, asin, region="us", **params) -> httpx.Response:
        """A live GET /book/{asin} (singular: the router prefix), which asks Audible for the record."""
        if not self._path_proven:
            # A 404 from a path that does not exist reads the same as a 404
            # for a missing book, so prove the path with a listed ASIN first.
            self._path_proven = True
            listed = self.live_asin("us")
            self.audible("get")
            control = self.http.get(f"/book/{listed}", params={"region": "us"})
            assert control.status_code == 200, "GET /book/{asin} did not serve a listed ASIN"
            time.sleep(2)  # its background write must not land inside a later state comparison
        self.audible("get")
        resp = self.http.get(f"/book/{asin}", params={"region": region, **params})
        self._last_audible = time.monotonic()
        return resp

    def db_book(self, asin, region="us") -> httpx.Response:
        return self.http.get(f"/db/book/{asin}", params={"region": region})

    def delisted(self, **params) -> httpx.Response:
        return self.http.get("/db/delisted", params=params)

    def delisted_asins(self, region=None) -> list[tuple[str, str]]:
        """Every (asin, region) on /db/delisted, newest first, read a full page at a time."""
        out, page = [], 1
        while True:
            params = {"limit": 100, "page": page}
            if region:
                params["region"] = region
            items = self.delisted(**params).json()
            out += [(b["asin"], b["region"]) for b in items]
            if len(items) < 100:
                return out
            page += 1

    def live_asin(self, region: str, nth: int = 0) -> str:
        """
        The nth ASIN Audible lists in this region (the relist test takes its own, nth=1, so it never
        marks the one the live-409 cases share). From a restored row when there is
        one (no Audible call), else one Libex search. Never a fabricated ASIN.
        """
        if (region, nth) in self._live:
            return self._live[region, nth]
        asin = ""
        if self.mode == "snapshot":
            asin = self.psql(
                "SELECT asin FROM books WHERE region = '%s' AND confirmed_at IS NOT NULL "
                "AND manual_entry_at IS NULL ORDER BY num_ratings DESC NULLS LAST LIMIT 1 OFFSET %d" % (region, nth)
            )
        if not asin:
            self.audible("get")
            resp = self.http.get("/search", params={"region": region, "title": _TITLES[region], "limit": nth + 1})
            assert resp.status_code == 200, f"search for a live {region} ASIN answered {resp.status_code}"
            asin = resp.json()[nth]["asin"]
            # The search stores what it found in the background; let that land
            # before any test compares table state.
            self.wait_for(lambda: self.count("books", asin, region) == 1, 30)
            time.sleep(2)
        self._live[region, nth] = asin
        return asin

    # -------------------------------------------------------------------- sql

    def count(self, table: str, asin: str, region: str | None = None) -> int:
        col = {"books": "asin", "tracks": "asin"}.get(table, "book_asin")
        reg = {"books": "region", "tracks": "region"}.get(table, "book_region")
        where = f"{col} = '{asin}'" + (f" AND {reg} = '{region}'" if region else "")
        return int(self.psql(f"SELECT count(*) FROM {table} WHERE {where}"))

    def rows_of(self, asin: str, region: str | None = None) -> tuple[int, ...]:
        """Counts of the book, its four pivots and its chapters: all zero or the full set."""
        return tuple(
            self.count(t, asin, region)
            for t in ("books", "author_book", "book_narrator", "book_series", "book_genre", "tracks")
        )

    def table_state(self, table: str, where: str = "true") -> str:
        """Row count and an order-independent md5 over each row's JSON and xmin, so an UPDATE to the same values shows."""
        return self.psql(
            f"SELECT count(*), coalesce(md5(string_agg(h, '' ORDER BY h)), '') FROM "
            f"(SELECT md5(row_to_json(x)::text || x.xmin::text) AS h FROM {table} x WHERE {where}) s"
        )

    def whole_state(self) -> dict[str, str]:
        """
        Mode A only: every public table. The cache is read without the
        db_stats entries, which a background loop in the app rewrites on its own.
        """
        names = self.psql("SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY 1").split()
        return {
            t: self.table_state(t, "x.key NOT LIKE 'db\\_stats%'" if t == "cache" else "true")
            for t in names
        }

    def state(self, asins=()) -> object:
        """Mode A: whole-table identity. Mode B: the targeted counts for these ASINs, never a whole-table scan."""
        if self.mode == "empty":
            return self.whole_state()
        return {a: self.rows_of(a) for a in asins}

    def entity_state(self, *, authors=(), author_names=(), series=(), genres=(), narrators=()) -> dict[str, str]:
        """Identity of the named shared rows, for the byte-identical-before-and-after checks."""

        def q(xs):
            return ",".join("'%s'" % x.replace("'", "''") for x in xs) or "''"

        return {
            "authors": self.table_state("authors", f"x.asin IN ({q(authors)})"),
            "authors_by_name": self.table_state("authors", f"x.name IN ({q(author_names)})"),
            "series": self.table_state("series", f"x.asin IN ({q(series)})"),
            "genres": self.table_state("genres", f"x.asin IN ({q(genres)})"),
            "narrators": self.table_state("narrators", f"x.name IN ({q(narrators)})"),
        }

    def wait_for(self, check, seconds: float, step: float = 1.0) -> bool:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if check():
                return True
            time.sleep(step)
        return bool(check())

    # --------------------------------------------------------------- triggers

    @contextlib.contextmanager
    def trigger(self, name: str, asin: str, body: str):
        """
        A sandbox-only BEFORE INSERT trigger on books for one fabricated ASIN.
        sbx_ names, a fixed message, lock_timeout on create and on drop, and it
        is gone again on exit whatever the test did.
        """
        assert name.startswith("sbx_") and asin.startswith("B0SBX")
        self.psql(
            "SET lock_timeout = '5s'; "
            f"CREATE FUNCTION {name}_fn() RETURNS trigger LANGUAGE plpgsql AS $f$ BEGIN {body} END $f$; "
            f"CREATE TRIGGER {name} BEFORE INSERT ON books FOR EACH ROW "
            f"WHEN (NEW.asin = '{asin}') EXECUTE FUNCTION {name}_fn();"
        )
        try:
            yield
        finally:
            self.drop_triggers()

    FAIL_BODY = "RAISE EXCEPTION 'sbx forced failure'; RETURN NEW;"
    STALL_BODY = "PERFORM pg_sleep(15); RETURN NEW;"

    def drop_triggers(self):
        # CASCADE takes the trigger with its function.
        for name in self.psql("SELECT proname FROM pg_proc WHERE proname LIKE 'sbx\\_%'").split():
            self.psql(f"SET lock_timeout = '5s'; DROP FUNCTION IF EXISTS {name}() CASCADE;")

    def triggers_clean(self) -> bool:
        return (
            self.psql("SELECT count(*) FROM pg_trigger WHERE tgname LIKE 'sbx\\_%'") == "0"
            and self.psql("SELECT count(*) FROM pg_proc WHERE proname LIKE 'sbx\\_%'") == "0"
        )


@pytest.fixture(scope="session")
def sbx():
    box = Sandbox()
    yield box
    box.drop_triggers()
    clean = box.triggers_clean()
    throttled = bool(_THROTTLED.search(box.app_logs()))
    box.http.close()
    assert not throttled, "Audible answered 429 or 403 during the run"
    # Before run.sh's migration and rollback steps, which must not meet a trigger.
    assert clean, "sandbox triggers or functions were left in the database"
