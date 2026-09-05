"""
Background /db/stats refresher tests.

The module keeps every stored stats entry from ever being read cold, and
every property below is one of the rulings that makes that work:
refreshing on a remaining-life THRESHOLD rather than on the tick cadence,
one worker of six doing it, a soft budget that stops a slow pass cleanly
under a hard ceiling that stops a hung one at all, a lock release that
cannot itself raise or leak, a failed entry leaving the stored value alone,
and a due order that decides which entries a truncated pass defers.

That last one is only observable across passes, which is why there is a
section for the sustained-degraded case as well as the healthy one: a fixed
order looks correct in every single-pass test and still starves the tail
forever.

No real database and no real engine -- the advisory-lock connection, the
session factory and get_db_stats are all replaced at this module's import
locations, so what is under test is the ordering and the decisions, not
SQLAlchemy. The two timing tests scale the module's own budget and ceiling
by a common factor rather than substituting invented ones, so the RATIO
between them is what is being exercised; see _SCALE.
"""

# Standard library
import asyncio
import inspect
import logging
import re
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import NamedTuple
from unittest.mock import AsyncMock, patch

# Third party
import pytest
from fastapi.testclient import TestClient

# Local
from app.main import app
from app.db import session as db_session
from app.db.models import Cache
from app.services.audible.client import VALID_REGIONS
from app.services.cache import manager as cache
from app.services.db.reader import STATS_CACHE_TTL_SECONDS, DbStatsResult
from app.services.db import stats_refresh
from app.services.db.stats_refresh import (
    _STATS_REFRESH_AHEAD_SECONDS,
    _STATS_REFRESH_LOCK_ID,
    _STATS_REFRESH_PASS_BUDGET_SECONDS,
    _STATS_REFRESH_PASS_CEILING_SECONDS,
    _STATS_REFRESH_ROTATION_SECONDS,
    STATS_REFRESH_INTERVAL_SECONDS,
    _due_stats_entries,
    _refresh_due_stats,
    _rotation_pivot,
    _release_refresh_lock,
    _stats_refresh_targets,
    run_stats_refresh_pass,
    stats_refresh_loop,
)


# The three cold entries the module has measured, and what a twelve-entry
# sweep costs when it is reproduced from those three and nothing else. Cited
# from the module so its ledger arithmetic can be asserted; nothing in this
# suite runs a real count.
#
# The unscoped entry is the term that decides the shape, and it went
# unmeasured until 2026-09-05: at ~15.3s it is nowhere near bounded by the
# 4.7s us figure, because every region-scoped count is a filtered subset of a
# count the unscoped entry does in full. Read it as an upper bound on database
# time rather than as database time -- it is end-to-end wall clock against the
# live instance, so TLS, Cloudflare, network and origin are inside it.
#
# A sweep is a range and not a point: the two measured regions differ by 3x
# and the nine between them are unmeasured. Both ends are carried because
# every correction this figure has needed came from collapsing the range to
# whichever point kept an inequality true -- 1.5s an entry, then 40s, then
# 35.1s, each presented as derived and none reproducible from the endpoints
# beside it.
_MEASURED_GLOBAL_ENTRY_SECONDS = 15.3
_MEASURED_REGION_SECONDS = (1.6, 4.7)
_CHEAPEST_SWEEP_SECONDS = 32.9
_DEAREST_SWEEP_SECONDS = 67.0

# What the ledger has left once the worst wait (one ceiling plus one interval,
# 110s) and one sweep are paid for, against the 150s an entry has when it
# becomes due. The dear end is negative, and that is the state of the
# arithmetic rather than a state of the module: simulated at the dear end with
# every pass truncating, all twelve entries were still refreshed 39-40 times
# over two hours and none spent any time expired, because truncation defers
# whichever entry has the most life left. Pinned at both ends so neither can
# be dropped for the other reading better.
_LEDGER_MARGIN_CHEAP_SECONDS = 7.1
_LEDGER_MARGIN_DEAR_SECONDS = -27.0


def _statement_timeout_seconds() -> float:
    """
    The statement_timeout every connection carries, read out of
    app/db/session.py rather than repeated here.

    The gap between the pass budget and the pass ceiling is sized as one
    statement_timeout -- what an entry admitted just under the soft deadline
    can plausibly still need. Hardcoding 30 here would let that setting
    change without anything noticing that the reason for the gap had moved.
    """
    source = inspect.getsource(db_session)
    match = re.search(r'"statement_timeout"\s*:\s*"(\d+)"', source)
    assert match, "app/db/session.py no longer sets statement_timeout"
    return int(match.group(1)) / 1000


_ORDER_BY_TAIL = re.compile(r"\bORDER BY\b(.*)", re.DOTALL)
_SQL_TOKEN = re.compile(r"[A-Za-z_][\w.]*|:\w+|<=|<|\(|\)")


def _bind_roles(statement):
    """
    Every bound parameter of the due query as {parameter name: the role it
    plays}, recovered from the values rather than from the names.

    The compiler numbers binds positionally -- :expires_at_1, :expires_at_2
    -- so a name is a fact about SQLAlchemy's counter and not about the
    query: adding one clause renumbers the rest. The four values are
    unambiguous on their own. Two instants are bound and they point opposite
    ways, the due cutoff AHEAD seconds into the future and the demotion
    threshold one whole cache lifetime behind now, so the earlier of the two
    is always the threshold. The list is the target enumeration and the
    remaining string is the rotation pivot.

    Getting the two instants the wrong way round is the specific mistake
    worth naming: they are 450 seconds apart, so a reader that took the
    first one the compiler happened to name would filter on a moment three
    quarters of a cache lifetime off and find nothing due at all.
    """
    bound = statement.compile().params
    instants = sorted((value, name) for name, value in bound.items()
                      if isinstance(value, datetime))
    assert len(instants) == 2, f"expected two bound instants, found {instants}"
    roles = {instants[0][1]: "unwritten_since", instants[1][1]: "cutoff"}
    for name, value in bound.items():
        if isinstance(value, list):
            roles[name] = "target_keys"
        elif isinstance(value, str):
            roles[name] = "pivot"
    return roles


def _due_cutoff(statement):
    """The instant the due query filters on, taken by role rather than by
    bind name; see _bind_roles for why the name is not safe to read."""
    bound = statement.compile().params
    return next(bound[name] for name, role in _bind_roles(statement).items()
                if role == "cutoff")


def _split_order_terms(tail):
    """
    The text after ORDER BY, split into one string per term.

    Split at depth zero rather than on every comma: the order now carries a
    CASE and a parenthesised conjunction, and a plain str.split would cut
    the first term that ever puts a comma inside brackets into two
    unparseable halves and blame the module for it.
    """
    terms = []
    depth = 0
    start = 0
    for position, character in enumerate(tail):
        if character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
        elif character == "," and depth == 0:
            terms.append(tail[start:position])
            start = position + 1
    terms.append(tail[start:])
    return [term.strip() for term in terms if term.strip()]


def _compile_order_expression(tokens, bound):
    """
    One ORDER BY term's tokens as a function from a row to the value the
    database would sort it on.

    A deliberately tiny grammar -- a column, a `<` comparison against a
    bound parameter, `AND`, brackets, and the one CASE shape the module
    renders -- and it asserts rather than guesses on anything else. That
    loudness is the feature: when the order gained its rotation terms this
    reader stopped parsing and three tests said so by name, which is how the
    change was noticed at all. A reader that shrugged and returned the terms
    it understood would have gone on replaying the previous order and
    reporting fairness the module had stopped producing.

    Evaluating the rendered SQL is what keeps the simulation honest. The
    alternative -- restating the four terms in Python -- is a second copy of
    the ordering that agrees with the first only until somebody edits one of
    them, and the whole point of the simulation is to catch an ordering that
    has changed.
    """
    def take(expected):
        found = tokens.pop(0)
        assert found.upper() == expected, f"expected {expected} in ORDER BY, found {found!r}"

    def primary():
        token = tokens.pop(0)
        word = token.upper()
        if word == "(":
            inner = conjunction()
            take(")")
            return inner
        if word == "CASE":
            take("WHEN")
            condition = conjunction()
            take("THEN")
            consequent = primary()
            take("ELSE")
            alternative = conjunction()
            take("END")
            return lambda row: consequent(row) if condition(row) else alternative(row)
        if word == "NULL":
            return lambda row: None
        if token.startswith(":"):
            value = bound[token[1:]]
            return lambda row: value
        column = token.split(".")[-1]
        assert column in ("key", "expires_at"), f"unknown column in ORDER BY: {token!r}"
        return lambda row: row[column]

    def comparison():
        left = primary()
        if tokens and tokens[0] == "<":
            tokens.pop(0)
            right = primary()
            return lambda row: left(row) < right(row)
        return left

    def conjunction():
        expression = comparison()
        while tokens and tokens[0].upper() == "AND":
            tokens.pop(0)
            right = comparison()
            expression = (lambda a, b: lambda row: bool(a(row) and b(row)))(expression, right)
        return expression

    return conjunction()


class _OrderTerm(NamedTuple):
    """
    One ORDER BY term: what it says, which way it sorts, and what it
    evaluates to for a given row.

    `sql` has every bound parameter replaced by the role it plays, so an
    assertion on it survives the compiler renumbering its binds and still
    says which instant the tier is measured against.
    """

    sql: str
    direction: str
    value: object

    def sort_key(self, row):
        """
        This term's value for `row`, wrapped so NULL sorts where Postgres
        sorts it.

        Postgres puts NULLs LAST under ASC by default, and reversing that
        key for DESC puts them first, which is the DESC default too -- so the
        leading flag reproduces both without a nulls_last clause anywhere.
        Nothing in the current order depends on it: the tier term is NULL
        across the whole demoted tier and non-NULL across all of it, so the
        term above has already separated them. It is here so that stops
        being true loudly rather than by producing a different order.
        """
        computed = self.value(row)
        return (computed is None, 0 if computed is None else computed)


def _order_terms(statement):
    """
    A statement's ORDER BY as [_OrderTerm], read off the rendered SQL rather
    than off SQLAlchemy's private clause list.

    Two things need the same reading of it: the assertion that names the four
    terms the due query sorts by, and the fake cache table that replays them
    over many passes. Reading it two ways would let them make two different
    claims about one statement, and the one that matters is the one the
    database would act on.
    """
    match = _ORDER_BY_TAIL.search(str(statement))
    if match is None:
        return []
    bound = statement.compile().params
    roles = _bind_roles(statement)
    terms = []
    for text_term in _split_order_terms(match.group(1)):
        tokens = _SQL_TOKEN.findall(text_term)
        assert tokens, f"unreadable ORDER BY term: {text_term!r}"
        direction = "ASC"
        if tokens[-1].upper() in ("ASC", "DESC"):
            direction = tokens.pop().upper()
        value = _compile_order_expression(tokens, bound)
        assert not tokens, f"unread tokens in ORDER BY term {text_term!r}: {tokens}"
        expression = re.sub(r"\s+(ASC|DESC)$", "", text_term.strip())
        for name, role in roles.items():
            expression = expression.replace(f":{name}", f":{role}")
        terms.append(_OrderTerm(expression, direction, value))
    return terms


# The timing tests run against the module's real budget and ceiling scaled by
# one common factor, so their RATIO is preserved: a source change that made
# the two equal makes the scaled pair equal too, and the graceful path stops
# being reachable here exactly as it stops being reachable in production.
# Substituting two invented numbers would test the harness instead.
_SCALE = 0.4 / _STATS_REFRESH_PASS_BUDGET_SECONDS
_SCALED_BUDGET = _STATS_REFRESH_PASS_BUDGET_SECONDS * _SCALE
_SCALED_CEILING = _STATS_REFRESH_PASS_CEILING_SECONDS * _SCALE

# One entry's cost, placed above the scaled budget and below the scaled
# ceiling: the first entry therefore finishes having overrun the soft
# deadline, which is the only arrangement in which the between-entries check
# can ever take effect.
_SCALED_ENTRY_SECONDS = _SCALED_BUDGET * 1.2


def _stats_result(seconds=300):
    """A successful get_db_stats return: an entry was written, so it carries
    an expiry. The refresher reads only that field."""
    return DbStatsResult(
        {"books": 1, "authors": 1, "narrators": 1, "series": 1, "booksWithChapters": 1},
        datetime.now(timezone.utc) + timedelta(seconds=seconds),
    )


def _failed_stats_result():
    """What get_db_stats returns when the live query fell over: the all-zeros
    fallback and no expiry, because nothing was written and the previously
    stored value is left in the table untouched. Left there is not the same
    as still serving: cache.get_entry filters on expires_at > now, so the row
    serves readers only for whatever is left of its expiry, and past that the
    edge's stale-if-error window covers them instead."""
    return DbStatsResult(
        {"books": 0, "authors": 0, "narrators": 0, "series": 0, "booksWithChapters": 0},
        None,
    )


class _FakeLockConnection:
    """
    The connection the advisory lock is taken on.

    Records what it was asked to do, in order, into a shared event list, so a
    test can assert not merely that the rollback, the unlock and the discard
    happened but that they happened at the right points around the pass. All
    three are invisible in a mock's call count and all three are what a later
    tidy-up would remove.
    """

    def __init__(self, events, elected=True, acquire_error=None,
                 unlock_error=None, unlock_result=True, invalidate_error=None,
                 rollback_error=None):
        self.events = events
        self.elected = elected
        self.acquire_error = acquire_error
        self.unlock_error = unlock_error
        self.unlock_result = unlock_result
        self.invalidate_error = invalidate_error
        self.rollback_error = rollback_error
        self.lock_ids = []
        self.unlock_ids = []

    async def scalar(self, statement, params=None):
        sql = str(statement)
        if "pg_try_advisory_lock" in sql:
            self.events.append("acquire")
            if self.acquire_error is not None:
                raise self.acquire_error
            self.lock_ids.append(params["lock_id"])
            return self.elected
        if "pg_advisory_unlock" in sql:
            self.events.append("unlock")
            if self.unlock_error is not None:
                raise self.unlock_error
            self.unlock_ids.append(params["lock_id"])
            return self.unlock_result
        raise AssertionError(f"unexpected statement on the lock connection: {sql}")

    async def rollback(self):
        self.events.append("rollback")
        if self.rollback_error is not None:
            raise self.rollback_error

    async def invalidate(self):
        self.events.append("invalidate")
        if self.invalidate_error is not None:
            raise self.invalidate_error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.events.append("close")
        return False


class _FakeEngine:
    """Hands out one recording connection and remembers how often it was
    asked for one -- the lock must be taken on a connection of its own, never
    on a session doing the counting, and a pass with nothing due must not ask
    for one at all."""

    def __init__(self, connection):
        self._connection = connection
        self.connect_calls = 0

    def connect(self):
        self.connect_calls += 1
        return self._connection


class _FakeSessionFactory:
    """AsyncSessionFactory() as an async context manager. Counts its calls:
    the due check and the pass itself each open one, and the lock is on
    neither of them."""

    def __init__(self, session):
        self.session = session
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *exc):
        return False


def _pass_environment(due=None, refresh_side_effect=None, **connection_kwargs):
    """The patches every run_stats_refresh_pass test needs, plus the shared
    event list they record into. `due` defaults to one entry, since a pass
    with nothing due never reaches the election at all."""
    events = []
    connection = _FakeLockConnection(events, **connection_kwargs)
    engine = _FakeEngine(connection)
    factory = _FakeSessionFactory(AsyncMock())
    due = {"db_stats": None} if due is None else due

    async def _record_pass(session, deadline):
        events.append("pass")
        if refresh_side_effect is not None:
            raise refresh_side_effect

    refresher = AsyncMock(side_effect=_record_pass)
    patches = (
        patch.object(stats_refresh, "engine", engine),
        patch.object(stats_refresh, "AsyncSessionFactory", factory),
        patch.object(stats_refresh, "_due_stats_entries", AsyncMock(return_value=due)),
        patch.object(stats_refresh, "_refresh_due_stats", refresher),
    )
    return events, connection, engine, factory, refresher, patches


# ============================================================
# THE REFRESH LEDGER
# ============================================================
# Threshold-driven, not cadence-driven. The interval bounds how long a due
# entry waits to be NOTICED; the AHEAD threshold alone decides how often one
# is actually recomputed. The properties below are the ones that hold under
# that design -- an earlier version of this file asserted
# `2 * interval + budget <= ttl`, which is not the gap between two refreshes
# of the same entry under a `pass(); sleep(interval)` loop and stayed green
# while the property it was named for failed.


def test_the_sweep_range_is_reproducible_from_the_entries_it_is_built_on():
    """
    The defect this file has had three times, asserted instead of described.
    Each previous sweep figure was presented as derived from the measured
    entries and was not reachable from them -- 40s needed 6.3s each for the
    three cheapest regions, above the stated maximum for any of them.

    Tied to the region count rather than to eleven, so adding a twelfth
    region fails here rather than leaving both ends quietly a sweep short.
    """
    cheapest_region, dearest_region = _MEASURED_REGION_SECONDS

    assert _CHEAPEST_SWEEP_SECONDS == pytest.approx(
        _MEASURED_GLOBAL_ENTRY_SECONDS + len(VALID_REGIONS) * cheapest_region
    )
    assert _DEAREST_SWEEP_SECONDS == pytest.approx(
        _MEASURED_GLOBAL_ENTRY_SECONDS + len(VALID_REGIONS) * dearest_region
    )


def test_the_ledger_reaches_a_due_entry_in_time_at_the_cheap_sweep_and_not_the_dear_one():
    """
    The ledger, in one line, at both ends. An entry becomes due with AHEAD
    seconds of life left. The worst wait before a pass that can even see it
    BEGINS is one pass CEILING plus one interval -- a pass that started just
    before it came due has to finish first, and the budget is not what bounds
    when a pass ends. The budget only stops a pass ADMITTING further entries;
    the one admitted just under it still gets its statement_timeout on top,
    which is what test_a_single_hung_statement_is_cut_off_by_the_hard_ceiling
    exercises. The worst time within that pass to reach the entry is then a
    full cold sweep of all twelve.

    110 + 32.9 fits inside the 150 and 110 + 67.0 does not, and both are
    asserted, because writing this inequality against the end that passes is
    exactly how a margin that does not exist came to be claimed here twice.
    An unmet dear end is not a module that lapses entries: the three terms
    are not simultaneously reachable, since a pass that runs to its ceiling
    is a degraded database while a 67.0s sweep is a healthy one, and a
    truncating pass defers whichever entry has the most life left.
    """
    worst_wait = _STATS_REFRESH_PASS_CEILING_SECONDS + STATS_REFRESH_INTERVAL_SECONDS

    assert worst_wait + _CHEAPEST_SWEEP_SECONDS <= _STATS_REFRESH_AHEAD_SECONDS
    assert worst_wait + _DEAREST_SWEEP_SECONDS > _STATS_REFRESH_AHEAD_SECONDS


def test_the_ledger_keeps_the_margins_it_claims_at_both_ends():
    """
    The same arithmetic read as two numbers rather than as an inequality,
    because an inequality alone goes on passing as the margin erodes to
    nothing -- and because a range stated as a single number is the thing
    this ledger has been corrected for three times. +7.1s at the cheap end
    and -27.0s at the dear one; any constant that moves takes the whole of
    the first.

    approx rather than == because the sweeps are not integers and the
    subtraction lands a few binary places off. The tolerance is float noise,
    not slack in the ledger.
    """
    worst_wait = _STATS_REFRESH_PASS_CEILING_SECONDS + STATS_REFRESH_INTERVAL_SECONDS

    cheap = _STATS_REFRESH_AHEAD_SECONDS - worst_wait - _CHEAPEST_SWEEP_SECONDS
    dear = _STATS_REFRESH_AHEAD_SECONDS - worst_wait - _DEAREST_SWEEP_SECONDS
    assert cheap == pytest.approx(_LEDGER_MARGIN_CHEAP_SECONDS)
    assert dear == pytest.approx(_LEDGER_MARGIN_DEAR_SECONDS)


def test_the_threshold_fires_inside_the_entrys_own_life():
    """An AHEAD at or past the TTL would mark every entry due the moment it
    was written, turning the threshold back into "refresh everything every
    tick" -- the cadence-driven behaviour the threshold replaced, with the
    load that came with it."""
    assert 0 < _STATS_REFRESH_AHEAD_SECONDS < STATS_CACHE_TTL_SECONDS


def test_the_recompute_rate_is_the_ttl_less_the_threshold():
    """What the threshold actually pins: one recompute per entry per
    (TTL - AHEAD), whatever the tick cadence, however many workers tick and
    however far they have drifted. 150 seconds, and the cost arithmetic in
    the module's comment is built on that number."""
    assert STATS_CACHE_TTL_SECONDS - _STATS_REFRESH_AHEAD_SECONDS == 150


def test_a_tick_is_much_cheaper_and_much_more_frequent_than_a_recompute():
    """The interval is a discovery cadence, not a work cadence: it is short
    precisely because finding nothing due costs one primary-key lookup. It
    must stay well inside the threshold it serves, or an entry can come due
    and go unnoticed."""
    assert STATS_REFRESH_INTERVAL_SECONDS < _STATS_REFRESH_AHEAD_SECONDS
    assert STATS_REFRESH_INTERVAL_SECONDS < _STATS_REFRESH_PASS_BUDGET_SECONDS


def test_the_hard_ceiling_is_strictly_above_the_soft_budget():
    """
    The subtle one. A soft deadline checked between entries and a hard
    asyncio.timeout are two mechanisms for two different failures -- a slow
    pass and a hung statement -- and they only both exist if the ceiling is
    strictly above the budget. Set equal, the deadline admits an entry at
    59.9s and the timeout cancels it 0.1s later, so every slow pass ends by
    cancellation and the graceful path is dead code: measured with the two
    equal, a two-entry pass at 0.5s per entry under a 0.6s budget was
    hard-cancelled instead of stopping cleanly. Nothing else in the module
    would report that; both versions "time out".
    """
    assert _STATS_REFRESH_PASS_CEILING_SECONDS > _STATS_REFRESH_PASS_BUDGET_SECONDS


def test_the_gap_between_them_is_a_full_statement_timeout():
    """And it is that size for a reason: an entry admitted just under the
    soft deadline gets one statement_timeout to finish before the hard
    ceiling cancels it mid-flight. The timeout is read out of
    app/db/session.py rather than repeated, so moving that setting without
    revisiting this gap fails here."""
    gap = _STATS_REFRESH_PASS_CEILING_SECONDS - _STATS_REFRESH_PASS_BUDGET_SECONDS
    assert gap >= _statement_timeout_seconds()


def test_the_pass_budget_straddles_the_sweep_range():
    """
    The budget has to be generous enough that an ordinary sweep is not cut
    short every time: under one, the graceful stop becomes the normal ending
    of a healthy pass rather than the degraded one, and the module runs
    permanently in the regime
    test_no_entry_is_starved_when_every_pass_truncates covers instead of
    entering it only when the database is unwell.

    It clears the cheap end of the sweep range and not the dear end, which is
    stated here rather than smoothed over: at the dear end every pass
    truncates, and whether 60s is the right size for a sweep whose measured
    upper end is 67.0s is an open question about the constants -- one that
    moves the ceiling and the whole 110s wait term with it, so it is a design
    change with its own measurements rather than something to fold into a
    correction.
    """
    assert _STATS_REFRESH_PASS_BUDGET_SECONDS > _CHEAPEST_SWEEP_SECONDS
    assert _STATS_REFRESH_PASS_BUDGET_SECONDS < _DEAREST_SWEEP_SECONDS


# ============================================================
# _stats_refresh_targets
# ============================================================


def test_targets_are_the_global_entry_plus_every_region():
    """Twelve: the unscoped entry plus one per region. A region missing here
    is a region whose four badges keep paying the cold recompute, which is
    invisible from a US-only reading of the README."""
    targets = _stats_refresh_targets()

    assert len(targets) == len(VALID_REGIONS) + 1
    assert targets[cache.stats_key(None)] is None
    for region in VALID_REGIONS:
        assert targets[cache.stats_key(region)] == region


def test_targets_map_every_key_to_the_region_that_produces_it():
    """The dict is what tells the pass which region to hand get_db_stats for
    a given key. A key paired with the wrong region would refresh one
    region's entry with another region's counts -- and would then write them
    to the cache under the first region's key, so the error would survive
    every later pass."""
    targets = _stats_refresh_targets()

    for key, region in targets.items():
        assert key == cache.stats_key(region)


def test_targets_are_enumerated_from_the_region_enum_not_hardcoded():
    """
    A region added to the enum has to be picked up here with no second
    edit. Proved by adding one: a hardcoded list keeps returning twelve
    entries and this fails, where the enumeration returns thirteen.
    """
    extended = set(VALID_REGIONS) | {"zz"}
    with patch.object(stats_refresh, "VALID_REGIONS", extended), \
         patch.object(cache, "VALID_REGIONS", extended):
        targets = _stats_refresh_targets()

    assert len(targets) == len(extended) + 1
    assert targets["db_stats:zz"] == "zz"


def test_targets_are_enumerated_in_a_stable_order():
    """
    VALID_REGIONS is a set, whose iteration order is not guaranteed to be the
    same in two processes. Sorting is what makes the twelve keys the same
    list in every worker, which is what lets one worker's due query, another
    worker's log line and this suite's assertions be about the same thing.

    It is no longer a visiting order and this no longer claims to be one.
    Which entries a truncated pass drops is decided against expires_at in
    _due_stats_entries; a fixed visiting order is what starved us and uk.
    """
    first = list(_stats_refresh_targets())
    second = list(_stats_refresh_targets())

    assert first == second
    assert first[0] == cache.stats_key(None)
    assert first[1:] == [cache.stats_key(r) for r in sorted(VALID_REGIONS)]


def test_targets_are_ascending_as_keys_and_not_merely_as_regions():
    """
    This function sorts REGIONS. Everything downstream of it is about KEYS.

    _rotation_pivot walks this list one step per period on the stated grounds
    that "keys arrives ascending from _stats_refresh_targets", and the ORDER
    BY it feeds expresses the tier in keys too -- `Cache.key < pivot` to split
    it and `Cache.key.asc()` to order it. The pivot walk and the order it is a
    pivot into are the same sequence for exactly one reason, which nothing
    here states: cache.stats_key appends the region after a constant prefix,
    so sorting the regions happens to sort the keys.

    The test above cannot see that reason go away. It recomputes its
    expectation through stats_key, so it agrees with any key format.

    What this adds, measured against two format mutants rather than assumed.
    Move the region in front of the prefix ("us:db_stats") and the unscoped
    key lands in the MIDDLE of the sorted order; the list is then not a
    rotation of key order at all, consecutive pivots stop being adjacent in
    the order the tier is actually sorted in, and
    test_the_demoted_tier_is_visited_in_key_order_from_the_moving_pivot
    catches it. Lengthen the unscoped key instead ("db_stats_global") and it
    sorts strictly LAST, which makes this list ascending key order rotated by
    one -- the pivot still walks the whole cycle in order, nothing
    misbehaves, and every other test in the suite passes. That second one is
    what this line is for: the documented invariant has stopped being true
    while the behaviour it was invoked to justify happens to survive, and an
    invariant nobody checks is one the next reader is entitled to rely on and
    wrong to.

    So it pins the contract where the contract is produced. It is not a
    second copy of the rotation test, and it is deliberately stronger than
    current behaviour strictly needs.
    """
    targets = list(_stats_refresh_targets())

    assert targets == sorted(targets)


# ============================================================
# _due_stats_entries
# ============================================================


@pytest.mark.asyncio
async def test_due_entries_are_the_stored_keys_that_are_close_to_expiry():
    """Two filters at once, returned as {key: region}: stored (somebody
    asked for this scope) and due (its remaining life is under the
    threshold). What comes back is what the pass will recompute."""
    session = AsyncMock()
    session.execute = AsyncMock(return_value=[("db_stats",), ("db_stats:us",)])

    due = await _due_stats_entries(session)

    assert due == {"db_stats": None, "db_stats:us": "us"}


@pytest.mark.asyncio
async def test_due_entries_creates_nothing_when_nothing_is_stored():
    """A key that has never been queried is not warmed into existence, so an
    instance that never serves /db/stats never pays for this module -- the
    tick costs one primary-key lookup and stops."""
    session = AsyncMock()
    session.execute = AsyncMock(return_value=[])

    assert await _due_stats_entries(session) == {}


@pytest.mark.asyncio
async def test_due_entries_filters_on_remaining_life_not_on_presence_alone():
    """
    The filter that pins the cost. Without it every stored entry is
    recomputed on every tick, and the query load becomes a function of how
    many workers are ticking and how far they have drifted apart -- measured
    at ~2,000 passes a day and ~55% duty cycle on the most expensive query
    in the service. With it, a surplus pass costs this one lookup.
    """
    session = AsyncMock()
    session.execute = AsyncMock(return_value=[])

    await _due_stats_entries(session)

    statement = str(session.execute.await_args[0][0])
    assert "expires_at <=" in statement
    assert Cache.__tablename__ in statement


@pytest.mark.asyncio
async def test_due_entries_cutoff_is_the_threshold_into_the_future():
    """
    The cutoff is now + AHEAD, not now: the point is to recompute an entry
    while it is still serving, not once it has already lapsed. Comparing
    against now would make this module a slightly faster version of the
    cold-read it exists to remove.

    The statement carries a second instant, and the two point opposite ways:
    the cutoff is AHEAD seconds into the future and decides what is DUE, the
    demotion threshold is one whole TTL into the past and decides what is
    taken LAST. Both are asserted here because they are the same column
    compared against two different times, and a sign or an offset copied from
    one to the other is a change no rendered SQL assertion would show.
    """
    session = AsyncMock()
    session.execute = AsyncMock(return_value=[])

    before = datetime.now(timezone.utc)
    await _due_stats_entries(session)
    after = datetime.now(timezone.utc)

    elapsed = (after - before).total_seconds()
    bound = session.execute.await_args[0][0].compile().params
    instants = sorted(v for v in bound.values() if isinstance(v, datetime))
    assert len(instants) == 2
    unwritten_since, cutoff = instants

    ahead = (cutoff - before).total_seconds()
    assert _STATS_REFRESH_AHEAD_SECONDS <= ahead <= _STATS_REFRESH_AHEAD_SECONDS + elapsed + 1
    behind = (after - unwritten_since).total_seconds()
    assert STATS_CACHE_TTL_SECONDS <= behind <= STATS_CACHE_TTL_SECONDS + elapsed + 1


@pytest.mark.asyncio
async def test_due_entries_still_catches_an_entry_that_already_lapsed():
    """
    `<=` against a cutoff in the future, so an entry that expired while this
    loop was down is due rather than invisible -- it is exactly the one that
    most needs recomputing, and an expired row survives in the table until
    the hourly purge collects it. A strict window (`between now and cutoff`)
    would skip precisely the entries the module exists for.
    """
    session = AsyncMock()
    session.execute = AsyncMock(return_value=[])

    await _due_stats_entries(session)

    statement = session.execute.await_args[0][0]
    cutoff = _due_cutoff(statement)
    assert cutoff > datetime.now(timezone.utc)
    assert ">=" not in str(statement)
    assert "expires_at >" not in str(statement)


@pytest.mark.asyncio
async def test_due_entries_asks_only_about_the_twelve_possible_keys():
    """A primary-key lookup on the twelve stats keys, not a scan of the
    cache table -- which holds every book, author and chapter response Libex
    has cached."""
    session = AsyncMock()
    session.execute = AsyncMock(return_value=[])

    await _due_stats_entries(session)

    bound = session.execute.await_args[0][0].compile().params
    key_lists = [v for v in bound.values() if isinstance(v, list)]
    assert key_lists == [list(_stats_refresh_targets())]


@pytest.mark.asyncio
async def test_due_entries_order_healthy_by_expiry_and_rotate_the_demoted_tier():
    """
    The order a truncated pass drops from the end of, so it decides who is
    deferred -- pinned as the exact four terms the database is handed, with
    every bind named for the role it plays rather than for the number the
    compiler gave it.

    Each term is load-bearing and each fails differently if it moves.

    The tier has to come FIRST or it is not a tier: ranked after expiry it
    could only break ties between entries lapsing in the same instant.

    The rotation is ANDed with the tier rather than standing alone, and that
    conjunction is the whole of its safety. `cache.key < :pivot` on its own
    is a term with two values across the UNDEMOTED entries too, so it would
    sort a healthy set by which side of an arbitrary moving key each entry
    falls on and only then by expiry -- silently replacing least-life-first
    for the entries that are actually being written. ANDed, it is constantly
    False everywhere outside the tier and cannot reorder anything there.

    Expiry is wrapped in a CASE that goes NULL across the tier, so it orders
    the healthy entries and leaves the tier to the term after it. Unwrapped
    -- which is what shipped first -- it orders the tier by how long each
    failure has lasted, over rows nothing ever writes, so that order never
    changes again and the entry behind a failing one stays behind it for the
    life of the process.

    The bare key last does two jobs: the rotation's second half inside the
    tier, and outside it the tiebreak that stops two entries written in the
    same instant from trading places between passes.

    Roles, not bind names. :expires_at_1 and :expires_at_2 are positions in
    SQLAlchemy's counter, so an assertion written against them breaks when an
    unrelated clause is added and says nothing about which instant the tier
    is measured against; see _bind_roles.
    """
    session = AsyncMock()
    session.execute = AsyncMock(return_value=[])

    await _due_stats_entries(session)

    statement = session.execute.await_args[0][0]
    assert "ORDER BY" in str(statement)
    assert [(term.sql, term.direction) for term in _order_terms(statement)] == [
        ("cache.expires_at < :unwritten_since", "ASC"),
        ("(cache.expires_at < :unwritten_since AND cache.key < :pivot)", "ASC"),
        (
            "CASE WHEN (cache.expires_at < :unwritten_since) THEN NULL"
            " ELSE cache.expires_at END",
            "ASC",
        ),
        ("cache.key", "ASC"),
    ]


@pytest.mark.asyncio
async def test_the_demotion_threshold_and_the_rotation_pivot_are_bound_from_one_instant():
    """
    Both are read off `now` inside a single call, so a pass cannot demote
    against one moment and rotate against another.

    Worth pinning because they are computed four lines apart and neither
    would look wrong on its own: a pivot taken from a second datetime.now()
    is correct at every instant and merely samples the clock twice, which is
    invisible until a test freezes one of them and finds the other still
    moving.
    """
    session = AsyncMock()
    session.execute = AsyncMock(return_value=[])

    with _clocked_stats_table({}) as table:
        await _due_stats_entries(session)
        frozen = table.clock.now(timezone.utc)

    bound = session.execute.await_args[0][0].compile().params
    roles = _bind_roles(session.execute.await_args[0][0])
    named = {role: bound[name] for name, role in roles.items()}

    assert named["cutoff"] == frozen + timedelta(seconds=_STATS_REFRESH_AHEAD_SECONDS)
    assert named["unwritten_since"] == frozen - timedelta(seconds=STATS_CACHE_TTL_SECONDS)
    assert named["pivot"] == stats_refresh._rotation_pivot(
        frozen, list(_stats_refresh_targets())
    )


@pytest.mark.asyncio
async def test_due_entries_keep_the_order_the_database_returned():
    """
    Where the first attempt at this fix died. The ordering was added to the
    statement and the mapping was still rebuilt by walking the alphabetical
    target dict, so the pass went on visiting entries in the order that
    starved us and uk while the SQL said otherwise -- a fix that changes the
    query and nothing a caller can observe.

    Rows arrive here in an order no target dict can produce, so anything that
    reads the order off the targets rather than off the result fails.
    """
    session = AsyncMock()
    session.execute = AsyncMock(return_value=[("db_stats:us",), ("db_stats",), ("db_stats:de",)])

    due = await _due_stats_entries(session)

    assert list(due) == ["db_stats:us", "db_stats", "db_stats:de"]
    assert due == {"db_stats:us": "us", "db_stats": None, "db_stats:de": "de"}


# ============================================================
# _rotation_pivot
# ============================================================
# What gives the demoted tier a front that moves. Every row in that tier is
# a full cache lifetime past expiry and none of them is ever written, so
# every column of every one is frozen and the clock is the only monotonic
# value this module can read -- it stores nothing of its own by design.


def test_the_rotation_pivot_is_always_one_of_the_keys_it_was_given():
    """It names the key the tier is visited FROM, so a value outside the
    list is not a rotation of anything -- every key would fall on the same
    side of it and the tier would be back to plain key order."""
    keys = list(_stats_refresh_targets())
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)

    for step in range(len(keys) * 3):
        pivot = _rotation_pivot(start + timedelta(seconds=step * 7), keys)
        assert pivot in keys


def test_the_rotation_pivot_advances_one_key_per_rotation_period_and_wraps():
    """
    One step per _STATS_REFRESH_ROTATION_SECONDS, and a full cycle names
    every key exactly once before repeating.

    A cycle that skipped a key would leave that key permanently unable to
    lead the tier, which is the starvation the rotation exists to remove
    wearing a smaller hat -- and it would be invisible in any test that only
    checked the pivot moves.
    """
    keys = list(_stats_refresh_targets())
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)

    cycle = [
        _rotation_pivot(start + timedelta(seconds=step * _STATS_REFRESH_ROTATION_SECONDS), keys)
        for step in range(len(keys))
    ]

    assert sorted(cycle) == sorted(keys)
    assert _rotation_pivot(
        start + timedelta(seconds=len(keys) * _STATS_REFRESH_ROTATION_SECONDS), keys
    ) == cycle[0]


def test_the_rotation_pivot_holds_still_inside_one_period():
    """
    The property that makes a multi-pass test easy to write and worthless.

    The pivot is a function of the wall clock and nothing else, so every pass
    inside one _STATS_REFRESH_ROTATION_SECONDS window draws the same one --
    and a simulation that runs twelve passes in a few milliseconds of real
    time runs all twelve against a single pivot, sees a demoted tier entered
    from the same key every pass, and reports the frozen behaviour of the
    order this one replaced as though it were the new one's.

    Pinned here so the constraint is a stated property rather than folk
    knowledge: anything driving more than one pass has to move the clock the
    module reads, which is what _clocked_stats_table exists to make
    unavoidable.
    """
    keys = list(_stats_refresh_targets())
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    inside = _STATS_REFRESH_ROTATION_SECONDS - 0.001

    assert _rotation_pivot(start, keys) == _rotation_pivot(
        start + timedelta(seconds=inside), keys
    )
    assert _rotation_pivot(start, keys) != _rotation_pivot(
        start + timedelta(seconds=_STATS_REFRESH_ROTATION_SECONDS), keys
    )


def test_the_rotation_period_is_no_longer_than_the_shortest_gap_between_passes():
    """
    Sized to the closest together two passes ever land. A pass that finds
    nothing due costs one indexed lookup and is followed by one interval, so
    a step longer than the interval lets two consecutive passes draw the same
    pivot and wastes one of them -- measured at 240s, the worst healthy entry
    sat 1,669s to 1,929s expired against 577s to 1,024s here.

    It is a constant of its own rather than a reuse of the interval, and the
    two holding the same number today is exactly why that matters: retuning
    how often a worker looks must not silently retune the rotation with it.
    """
    assert _STATS_REFRESH_ROTATION_SECONDS <= STATS_REFRESH_INTERVAL_SECONDS
    # Its own literal, not an alias of the interval. The two agree on a
    # value and not on a purpose, and an alias is how the agreement would
    # stop being a decision anybody made.
    source = inspect.getsource(stats_refresh)
    assert re.search(r"^_STATS_REFRESH_ROTATION_SECONDS = \d+$", source, re.MULTILINE)


# ============================================================
# _refresh_due_stats
# ============================================================


def _far_deadline():
    """A deadline no test pass can reach, for the cases that are not about
    the budget."""
    return asyncio.get_running_loop().time() + 3600


@pytest.mark.asyncio
async def test_a_pass_refreshes_each_due_entry_with_its_own_region():
    """Every due entry gets exactly one refresh, scoped to the region its
    key belongs to. A region handed the wrong key writes one region's counts
    into another's badge."""
    session = AsyncMock()
    due = {"db_stats": None, "db_stats:de": "de", "db_stats:us": "us"}
    with patch.object(stats_refresh, "_due_stats_entries", AsyncMock(return_value=due)), \
         patch.object(stats_refresh, "get_db_stats", AsyncMock(return_value=_stats_result())) as mock_stats:
        await _refresh_due_stats(session, _far_deadline())

    assert [call[0][1] for call in mock_stats.await_args_list] == [None, "de", "us"]


@pytest.mark.asyncio
async def test_a_pass_re_reads_the_due_set_rather_than_being_handed_it():
    """
    Between the pre-election check and here, another worker may have won and
    refreshed some of it. Re-reading costs one primary-key lookup and avoids
    recomputing what is already warm -- and it is why this function takes a
    deadline rather than a set of entries.
    """
    session = AsyncMock()
    with patch.object(stats_refresh, "_due_stats_entries", AsyncMock(return_value={})) as mock_due, \
         patch.object(stats_refresh, "get_db_stats", AsyncMock()) as mock_stats:
        await _refresh_due_stats(session, _far_deadline())

    mock_due.assert_awaited_once_with(session)
    mock_stats.assert_not_called()
    assert list(inspect.signature(_refresh_due_stats).parameters) == ["session", "deadline"]


@pytest.mark.asyncio
async def test_a_pass_forces_the_live_query_rather_than_reading_the_cache_back():
    """
    refresh=True is the whole mechanism. Without it every entry the pass
    visits is still live -- that is the point, it runs before they lapse --
    so a cache-aside read would hand back the very value the pass exists to
    supersede, and the refresher would run forever renewing nothing.
    """
    session = AsyncMock()
    with patch.object(stats_refresh, "_due_stats_entries", AsyncMock(return_value={"db_stats": None})), \
         patch.object(stats_refresh, "get_db_stats", AsyncMock(return_value=_stats_result())) as mock_stats:
        await _refresh_due_stats(session, _far_deadline())

    assert mock_stats.await_args.kwargs["refresh"] is True


@pytest.mark.asyncio
async def test_one_failed_entry_does_not_stop_the_rest_of_the_pass():
    """
    A failure is reported as a null expiry, not raised, and the loop moves
    on. If it stopped, one region whose count times out under a corpus
    refresh's write load would leave every entry after it in the pass cold
    -- and the same entry fails first every pass, so the ones behind it
    would never be renewed at all.
    """
    session = AsyncMock()
    due = {"db_stats": None, "db_stats:de": "de", "db_stats:us": "us"}
    results = [_stats_result(), _failed_stats_result(), _stats_result()]
    with patch.object(stats_refresh, "_due_stats_entries", AsyncMock(return_value=due)), \
         patch.object(stats_refresh, "get_db_stats", AsyncMock(side_effect=results)) as mock_stats:
        await _refresh_due_stats(session, _far_deadline())

    assert mock_stats.await_count == 3


@pytest.mark.asyncio
async def test_a_failed_entry_is_never_invalidated(caplog):
    """
    The guarantee that makes a failed refresh free: the stored value is left
    exactly where it is, keeping whatever life it has left, until a later
    pass succeeds. That life is the whole of what is preserved -- cache.get_entry
    filters on expires_at > now, so a lapsed row serves nobody and it is the
    edge's stale-if-error window that covers a reader from then on.
    Invalidating first -- the intuitive way to "force" a refresh -- throws
    away even the unexpired remainder, turning every transient failure into
    the cold state the module exists to remove, at the moment the database is
    already unwell.
    """
    session = AsyncMock()
    with patch.object(stats_refresh, "_due_stats_entries", AsyncMock(return_value={"db_stats": None})), \
         patch.object(stats_refresh, "get_db_stats", AsyncMock(return_value=_failed_stats_result())), \
         patch.object(cache, "invalidate", AsyncMock()) as mock_invalidate, \
         caplog.at_level(logging.WARNING):
        await _refresh_due_stats(session, _far_deadline())

    mock_invalidate.assert_not_called()
    assert any("Stats refresh failed" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_a_pass_past_its_deadline_stops_before_touching_anything(caplog):
    """
    The soft budget, at its simplest: a deadline already behind us means the
    pass recomputes nothing at all. Stopping loses nothing -- an entry not
    reached was never written, so it is still due and the next tick takes it
    twenty seconds later.
    """
    session = AsyncMock()
    due = {"db_stats": None, "db_stats:us": "us"}
    with patch.object(stats_refresh, "_due_stats_entries", AsyncMock(return_value=due)), \
         patch.object(stats_refresh, "get_db_stats", AsyncMock(return_value=_stats_result())) as mock_stats, \
         caplog.at_level(logging.WARNING):
        await _refresh_due_stats(session, asyncio.get_running_loop().time() - 1)

    mock_stats.assert_not_called()
    budget = [r for r in caplog.records if "hit its budget" in r.getMessage()]
    assert len(budget) == 1
    assert budget[0].skipped == 2


@pytest.mark.asyncio
async def test_a_pass_that_runs_long_stops_between_entries_and_says_what_it_left(caplog):
    """
    The graceful path, driven the way it happens in production: entries are
    refreshed one at a time and the deadline is checked between them, so the
    pass stops after whichever entry overran it rather than being cancelled
    inside one. The count it reports has to be right -- what it says it
    skipped is what the next tick has to pick up.
    """
    session = AsyncMock()
    due = {"db_stats": None, "db_stats:de": "de", "db_stats:us": "us"}

    async def _slow_refresh(*args, **kwargs):
        await asyncio.sleep(_SCALED_ENTRY_SECONDS)
        return _stats_result()

    deadline = asyncio.get_running_loop().time() + _SCALED_BUDGET
    with patch.object(stats_refresh, "_due_stats_entries", AsyncMock(return_value=due)), \
         patch.object(stats_refresh, "get_db_stats", AsyncMock(side_effect=_slow_refresh)) as mock_stats, \
         caplog.at_level(logging.WARNING):
        await _refresh_due_stats(session, deadline)

    assert mock_stats.await_count == 1
    budget = [r for r in caplog.records if "hit its budget" in r.getMessage()]
    assert len(budget) == 1
    assert budget[0].refreshed == 1
    assert budget[0].skipped == 2


@pytest.mark.asyncio
async def test_a_pass_that_finishes_in_time_reports_no_skips(caplog):
    """The control for the two above: with room to spare nothing is skipped
    and no budget warning is logged, so the warning means what it says."""
    session = AsyncMock()
    due = {"db_stats": None, "db_stats:us": "us"}
    with patch.object(stats_refresh, "_due_stats_entries", AsyncMock(return_value=due)), \
         patch.object(stats_refresh, "get_db_stats", AsyncMock(return_value=_stats_result())), \
         caplog.at_level(logging.DEBUG, logger="libex"):
        await _refresh_due_stats(session, _far_deadline())

    assert not any("hit its budget" in r.getMessage() for r in caplog.records)
    complete = [r for r in caplog.records if r.getMessage() == "Stats refresh pass complete"]
    assert len(complete) == 1
    assert complete[0].refreshed == 2
    assert complete[0].failed == 0
    assert complete[0].skipped == 0
    assert complete[0].due == 2


# ============================================================
# SUSTAINED PRESSURE ACROSS MANY PASSES
# ============================================================
# The regime every test above steps around: per-entry cost high enough that
# no pass can reach the whole due set, for longer than one pass. A single
# slow pass corrects itself -- what it skipped is still due and the next tick
# takes it. Sustained pressure does not, unless the order changes, because
# the entries served first come due again and re-enter ahead of the ones that
# were skipped.
#
# Nothing here is a second implementation of the module's decisions. The real
# _due_stats_entries runs against a fake cache table that replays its own
# filter and ORDER BY, and the real _refresh_due_stats truncates against a
# real deadline; the simulation supplies only the passage of time between
# passes.

# A UNIFORM per-entry cost, in production seconds, for the one simulation
# that models pressure without failure. It is a chosen pressure level and
# not a measurement of anything: what it is chosen for is that a 60s budget
# admits exactly three of the twelve, which
# test_the_degraded_pressure_level_is_the_one_that_truncates asserts rather
# than leaves to this comment. Any cost from 20s to 29s does that, so 21 is
# a point inside a band and nothing here depends on which point.
#
# ITS DIGITS COLLIDE WITH A DISCREDITED FIGURE AND THE TWO ARE UNRELATED,
# which is worth saying because the module comment names that figure four
# paragraphs from here. The ~21s the 35.1s aggregate rested on was a total
# for NINE entries -- about 2.3s each -- and it was unsupported. This is 21s
# for ONE entry, nine times that per entry, and it describes a database
# under sustained load rather than a healthy sweep. Nothing was relabelled;
# the numbers are the same two digits about two different quantities.
_DEGRADED_ENTRY_SECONDS = 21

# What one entry costs a pass when it is FAILING rather than merely slow:
# the statement_timeout, read out of app/db/session.py rather than repeated,
# because a failing entry runs until something stops it and that setting is
# the only thing that does.
_FAILING_ENTRY_SECONDS = _statement_timeout_seconds()

# Real seconds per production second. The budget and the per-entry cost are
# scaled by the same factor so the number of entries a pass admits is the one
# production would admit; scaling only one of them would choose that number
# by hand and test the harness.
_PRESSURE_SCALE = 1 / 1000
_PRESSURE_BUDGET = _STATS_REFRESH_PASS_BUDGET_SECONDS * _PRESSURE_SCALE

# Enough passes for the head of a fixed order to come due again several times
# over, which is when a deferral that settles becomes distinguishable from
# one that rotates. Fewer and both orders merely look slow.
_PRESSURE_PASSES = 14

# Passes for the outage simulation, which needs considerably more than the
# healthy one. A failing entry does not reach the demotion boundary until
# 450 production seconds after it first came due -- 150s of remaining life,
# then a whole cache lifetime past expiry -- and a pass plus its interval
# costs roughly 80 production seconds, so the first six passes happen before
# there is a demoted tier at all. Everything this file asserts about that
# tier is in the twenty-four after them.
_OUTAGE_PASSES = 30


def _measured_entry_seconds(key, failing, region_seconds):
    """
    What one entry costs a pass, from the module's own measured figures: the
    unscoped entry at 15.3s, a region at whichever end of the measured range
    the run is at, and a failing entry at the statement_timeout.

    Reproduced from the three measurements rather than flattened to one
    average per entry, because the unscoped entry is 3x to 10x a region and
    it is also the entry the five top-of-README badges read. An average
    would spread its cost over the eleven that do not carry it and make the
    budget reach further than it does.
    """
    if key in failing:
        return _FAILING_ENTRY_SECONDS
    if key == cache.stats_key(None):
        return _MEASURED_GLOBAL_ENTRY_SECONDS
    return region_seconds


class _SimulatedClock:
    """
    The clock a multi-pass simulation runs on, standing in for the module's
    `datetime`.

    Patched over app.services.db.stats_refresh.datetime AND handed to the
    fake cache table, so the two cannot drift: the module's due cutoff, its
    demotion threshold, its rotation pivot and the table's row expiries are
    all read from this one instant.

    THAT SINGLE SOURCE IS THE WHOLE REASON THIS EXISTS, and the trap it
    closes is specific. _rotation_pivot advances with the wall clock, one
    step per _STATS_REFRESH_ROTATION_SECONDS. Twelve passes inside a fast
    test all land in the same wall-clock second, so they all draw the SAME
    pivot, the demoted tier is visited from the same key every pass, and the
    rotation looks exactly as frozen as the ordering it replaced. A
    multi-pass test written against the real clock therefore asserts the
    behaviour of the broken order and passes -- which is why _advanced_by
    below refuses to let a simulation age its rows without moving the clock
    the same amount.
    """

    def __init__(self, instant):
        self.instant = instant

    def now(self, tz=None):
        return self.instant

    def advance(self, seconds):
        self.instant += timedelta(seconds=seconds)


# Where a simulated run starts. Arbitrary, and fixed rather than derived
# from today: a pivot is a function of the absolute instant, so a run anchored
# to now() would visit the demoted tier from a different key every day and a
# failure would be reproducible only on the date it happened.
_SIMULATED_START = datetime(2026, 1, 1, tzinfo=timezone.utc)


class _FakeStatsCacheTable:
    """
    The twelve stats rows across a run of passes, as {key: seconds of life
    left}, standing in for the session _due_stats_entries queries.

    execute() replays the statement's own IN filter, expiry cutoff and ORDER
    BY against those rows instead of answering in a fixed order. That is what
    makes the simulation sensitive to the module's ordering rather than to
    this class's: an ORDER BY dropped from the query is an order this table
    stops applying, and the rows come back in the insertion order the target
    enumeration gave them -- which is exactly the alphabetical behaviour that
    starved us and uk.

    The clock defaults to the real `datetime`, which is what a single-pass
    test wants and what the module itself is reading in that case. A
    multi-pass test hands a _SimulatedClock instead, and must patch the same
    object over the module; _clocked_stats_table does both together so it
    cannot be done by halves.
    """

    def __init__(self, life, clock=datetime):
        self.life = dict(life)
        self.clock = clock

    async def execute(self, statement):
        bound = statement.compile().params
        keys = next(v for v in bound.values() if isinstance(v, list))
        cutoff = _due_cutoff(statement)
        now = self.clock.now(timezone.utc)
        rows = [
            {"key": key, "expires_at": now + timedelta(seconds=left)}
            for key, left in self.life.items()
            if key in keys and now + timedelta(seconds=left) <= cutoff
        ]
        for term in reversed(_order_terms(statement)):
            rows.sort(key=term.sort_key, reverse=term.direction == "DESC")
        return [(row["key"],) for row in rows]

    def refresh(self, key):
        self.life[key] = STATS_CACHE_TTL_SECONDS

    def expired(self):
        """Every key whose stored row has already lapsed, which is every key
        the request path would currently recompute cold."""
        return {key for key, left in self.life.items() if left <= 0}

    def age(self, seconds):
        """
        Move `seconds` of production time past: every row loses that much
        life and the clock moves the same amount.

        One call, not two, and that is the point rather than a convenience.
        Aging the rows while leaving the clock where it was is the exact
        shape of a multi-pass test that measures nothing -- the pivot never
        moves, the demoted tier is entered from the same key every pass, and
        the rotation is invisible.
        """
        for key in self.life:
            self.life[key] -= seconds
        self.clock.advance(seconds)


@contextmanager
def _clocked_stats_table(life, start=_SIMULATED_START):
    """
    A fake cache table and the simulated clock that BOTH it and the module
    under test read, for the length of the block.

    Handed out together because there is no correct way to have one without
    the other: see _SimulatedClock for what a test measures when the module
    keeps the wall clock and only the rows move.
    """
    clock = _SimulatedClock(start)
    table = _FakeStatsCacheTable(life, clock)
    with patch.object(stats_refresh, "datetime", clock):
        yield table


@pytest.mark.asyncio
async def test_no_entry_is_starved_when_every_pass_truncates():
    """
    The property the alphabetical order lost, and the reason it went
    unnoticed: every test above runs one pass, and one pass cannot tell a
    deferral that rotates from a deferral that settles.

    Twelve entries, all due, at a per-entry cost that lets a pass reach three
    of them. Under a fixed order the same three are served every pass -- they
    come due again 150s later and re-enter at the front, ahead of the nine
    that were skipped -- so the tail is deferred for as long as the pressure
    lasts. Measured that way against Postgres 16: the first entry refreshed
    seven times while uk and us refreshed zero and sat expired. Ordering by
    remaining life inverts it, because an entry skipped is by construction
    one of the entries with the most life left, which is what puts it at the
    front of the next pass.

    This does not buy margin and is not asserted as if it did -- the ledger
    above is unchanged by the ordering. What it buys is that the entry served
    last is the one that can afford to wait.
    """
    keys = list(_stats_refresh_targets())
    life = {key: float(_STATS_REFRESH_AHEAD_SECONDS) for key in keys}
    refreshes = {key: 0 for key in keys}
    served_per_pass = []

    with _clocked_stats_table(life) as table:

        async def _slow_entry(session, region, refresh):
            await asyncio.sleep(_DEGRADED_ENTRY_SECONDS * _PRESSURE_SCALE)
            table.refresh(cache.stats_key(region))
            refreshes[cache.stats_key(region)] += 1
            return _stats_result()

        with patch.object(stats_refresh, "get_db_stats", AsyncMock(side_effect=_slow_entry)):
            for _ in range(_PRESSURE_PASSES):
                before = sum(refreshes.values())
                deadline = asyncio.get_running_loop().time() + _PRESSURE_BUDGET
                await _refresh_due_stats(table, deadline)
                served = sum(refreshes.values()) - before
                served_per_pass.append(served)
                # Production seconds the pass and the wait for the next tick
                # cost, taken from what the pass actually managed rather than
                # from what it was expected to, so timer jitter moves the
                # simulated clock with it instead of desynchronising from it.
                # One call moves the rows and the clock together; see
                # _FakeStatsCacheTable.age for why it may not be two.
                table.age(served * _DEGRADED_ENTRY_SECONDS + STATS_REFRESH_INTERVAL_SECONDS)

    # Without this the run could sweep everything every pass and the fairness
    # assertions below would hold for a reason that has nothing to do with
    # the order.
    assert served_per_pass and max(served_per_pass) < len(keys)

    assert min(refreshes.values()) >= 1, f"starved: {refreshes}"
    assert min(refreshes.values()) * 2 >= max(refreshes.values()), f"settled: {refreshes}"


@pytest.mark.asyncio
async def test_an_entry_nothing_has_written_for_a_full_ttl_is_taken_last():
    """
    Where expiry order alone gets it exactly backwards. An entry whose own
    counts fail costs a whole attempt and is written by none of them, and
    being unwritten it only falls further behind -- so pure expiry order
    hands it the front of every pass forever and the entries that would
    succeed are starved by the one that cannot. Measured over all 55 failing
    pairs at both measured cost ends, 110 runs of 200 passes: with no tier at
    all the worst healthy entry was refreshed zero times, and all 110 runs
    ended with a healthy entry still expired.

    One whole TTL past expiry is what separates late from unwritten. The
    ledger bounds late at 110s, so an entry a full cache lifetime past its
    own expiry is by definition one that nothing has successfully written for
    a complete lifetime -- which needs no stored state to detect, and is why
    this module still writes nothing of its own.

    ONE DEMOTED ENTRY IS ALL THIS SHOWS, deliberately, and it is why it is
    not the test that defends the tier. A tier of one cannot be ordered
    wrongly, so this order is the same under every candidate the module
    considered -- including the expiry-ordered tier that shipped first and
    starves. What the tier does to more than one demoted entry is
    test_the_demoted_tier_is_visited_in_key_order_from_the_moving_pivot, and
    what it is worth across passes is
    test_no_healthy_entry_stays_expired_while_two_entries_keep_failing.

    Driven through the fake table so the ordering under test is the SQL's
    own, replayed: an assertion on the rendered ORDER BY says the terms are
    there, and this says what they do to rows.
    """
    table = _FakeStatsCacheTable({
        "db_stats": 10.0,
        "db_stats:de": -30.0,
        "db_stats:us": -(STATS_CACHE_TTL_SECONDS + 60),
    })

    due = await _due_stats_entries(table)

    # de lapsed half a minute ago and us has not been written in over a cache
    # lifetime, so the healthy pair go first in expiry order and the entry
    # that is failing is still attempted, last, with whatever budget is left.
    assert list(due) == ["db_stats:de", "db_stats", "db_stats:us"]


# ============================================================
# A SUSTAINED OUTAGE OF TWO ENTRIES
# ============================================================
# The shape the two fairness tests above are structurally blind to, and the
# one the first version of the demoted tier passed while failing for 45 of
# the 55 possible region pairs. One of them runs many passes with NOTHING
# failing; the other is a single-pass ordering assertion over three rows. A
# tier that freezes is invisible to both: with nothing failing there is no
# tier, and one pass cannot show an order that never changes again.
#
# WHICH PAIR FAILS DECIDES WHAT THE RESULT MEANS. db_stats:us sorts last of
# the twelve keys, so a failure there consumes budget only after everything
# else has been swept, and the ten pairs containing us are the only ten an
# expiry-ordered tier survives -- on uk+us it looks perfect. The pairs below
# therefore exclude us on purpose. au+br is the sharpest of the 55: it sorts
# first, and under the expiry-ordered tier nine of the ten healthy entries
# were never refreshed again and were still expired when the run ended.
#
# Costs are the module's own measured figures rather than one flat number
# per entry, because the unscoped entry is 3x to 10x a region and is also the
# one the five top-of-README badges read; see _measured_entry_seconds.


@contextmanager
def _quiet_refresher():
    """
    Suppress the refresher's own log lines for the length of a simulated
    outage.

    Thirty passes across six parameterizations emit roughly six hundred
    records, and pytest replays every one of them under whichever assertion
    fails -- so the message naming which entry starved arrives at the bottom
    of a two-hundred-kilobyte wall of "Stats refresh pass complete". That is
    a real cost paid at exactly the moment the output matters most.

    Nothing is lost by silencing them HERE. The pass's logging lifecycle is
    asserted by name against caplog in its own tests further up -- what it
    reports on a budget stop, on a failed entry and on a clean finish -- and
    none of those run under this. A simulation asserts about ordering, not
    about lines.
    """
    logger = logging.getLogger("libex")
    previous = logger.level
    logger.setLevel(logging.ERROR)
    try:
        yield
    finally:
        logger.setLevel(previous)


async def _run_outage(failing, region_seconds, passes=_OUTAGE_PASSES):
    """
    Run `passes` refresh passes with `failing` never able to write, and
    report how often each entry was refreshed and which healthy entries were
    left expired at the end.

    The real _due_stats_entries and _refresh_due_stats do the work. What is
    simulated is the passage of time: a pass costs whatever the entries it
    actually admitted cost, and the wait for the next tick is one interval on
    top. Time spent is accumulated from the entries visited rather than from
    what the pass was expected to visit, so the simulated clock cannot drift
    away from the run it is meant to be describing.

    A failing entry returns the all-zeros fallback with no expiry -- what
    get_db_stats reports when the live query fell over -- and its row is
    left exactly where it was, which is the module's own behaviour and the
    entire reason the tier is needed.
    """
    keys = list(_stats_refresh_targets())
    life = {key: float(_STATS_REFRESH_AHEAD_SECONDS) for key in keys}
    refreshes = {key: 0 for key in keys}
    attempts = {key: 0 for key in keys}
    truncated = 0
    demoted_at_most = 0
    spent = 0.0

    with _clocked_stats_table(life) as table:

        async def _entry(session, region, refresh):
            nonlocal spent
            key = cache.stats_key(region)
            cost = _measured_entry_seconds(key, failing, region_seconds)
            spent += cost
            attempts[key] += 1
            await asyncio.sleep(cost * _PRESSURE_SCALE)
            if key in failing:
                return _failed_stats_result()
            table.refresh(key)
            refreshes[key] += 1
            return _stats_result()

        with patch.object(stats_refresh, "get_db_stats", AsyncMock(side_effect=_entry)), \
             _quiet_refresher():
            for _ in range(passes):
                spent = 0.0
                before = sum(attempts.values())
                due = len(await _due_stats_entries(table))
                deadline = asyncio.get_running_loop().time() + _PRESSURE_BUDGET
                await _refresh_due_stats(table, deadline)
                if sum(attempts.values()) - before < due:
                    truncated += 1
                table.age(spent + STATS_REFRESH_INTERVAL_SECONDS)
                demoted_at_most = max(demoted_at_most, sum(
                    1 for left in table.life.values()
                    if left < -STATS_CACHE_TTL_SECONDS
                ))

        left_expired = table.expired()

    healthy = [key for key in keys if key not in failing]
    return SimpleNamespace(
        healthy=healthy,
        refreshes=refreshes,
        attempts=attempts,
        truncated=truncated,
        demoted_at_most=demoted_at_most,
        expired_at_end={key for key in left_expired if key in healthy},
    )


# The two failing pairs the outage is run over, named by region. Neither
# contains us, for the reason the section comment gives; au+br is the
# sharpest of the 55 and in+jp is a mid-list pair carried so the result is
# not a property of sorting first.
_OUTAGE_PAIRS = [("au", "br"), ("in", "jp")]


@pytest.mark.asyncio
@pytest.mark.parametrize("first,second", _OUTAGE_PAIRS)
@pytest.mark.parametrize("region_seconds", _MEASURED_REGION_SECONDS)
async def test_no_healthy_entry_stays_expired_while_two_entries_keep_failing(
    first, second, region_seconds
):
    """
    The property the expiry-ordered tier lost, and the one thing that
    separates a demoted tier from no tier at all.

    Two entries fail every pass and never write, so they keep their old
    expires_at while everything that succeeds moves a whole TTL forward.
    Both eventually cross the demotion boundary -- and so do the healthy
    entries a truncated pass never reached, because expires_at cannot tell a
    failing entry from an unvisited one. Order that tier by expires_at and it
    is frozen: nothing in it is ever written, so the two that fail lead it
    for the life of the process and everything behind them stays behind
    them. Visit it as a rotation and the front moves every pass, so each of
    them leads once per cycle, succeeds, and leaves.

    Both ends of the measured region cost are run, because the cheap end
    fits a sweep inside the budget and the dear end does not, and only the
    dear end puts the module in the regime where every pass truncates.

    Asserted as an end state and not as a count. Across the 110-run sweep
    the rotation left no healthy entry expired at the end of any run and the
    expiry-ordered tier left one in 90 of 110; that is the column this
    reproduces at a smaller pass count. The 93-refresh figure from the same
    sweep is not asserted, here or anywhere, because it belongs to a 200-pass
    run over all 55 pairs that this suite does not perform -- quoting it
    against 30 passes would be a number that agreed with the docstring rather
    than with the run.
    """
    failing = {cache.stats_key(first), cache.stats_key(second)}

    outage = await _run_outage(failing, region_seconds)

    # Both preconditions, because without them the two assertions below hold
    # for reasons that have nothing to do with the order. A run in which no
    # pass ever truncated never had to defer anybody, and a run in which the
    # demoted tier never held more than one entry never had a tier to order.
    assert outage.truncated > 0, "no pass ever truncated"
    assert outage.demoted_at_most > 1, f"tier never held two: {outage.demoted_at_most}"

    assert outage.expired_at_end == set(), f"still expired: {outage.expired_at_end}"
    starved = {key: outage.refreshes[key] for key in outage.healthy
               if outage.refreshes[key] == 0}
    assert starved == {}, f"never refreshed again: {starved}"


@pytest.mark.asyncio
@pytest.mark.parametrize("first,second", _OUTAGE_PAIRS)
async def test_a_demoted_entry_is_still_attempted_rather_than_excluded(first, second):
    """
    Demotion is an ORDER BY and not a WHERE, and the difference is whether a
    failing entry can ever recover.

    Nothing in this module marks an entry bad, and nothing may: an entry
    fails because the database was unwell, and the only way to discover it
    is well again is to try it. An implementation that filtered the tier out
    of the due set would look identical in every fairness assertion above --
    better, even, since the healthy entries would never wait behind it -- and
    would leave the two failing entries permanently stale with nothing
    logging why.
    """
    failing = {cache.stats_key(first), cache.stats_key(second)}

    outage = await _run_outage(failing, _MEASURED_REGION_SECONDS[0])

    assert all(outage.attempts[key] > 0 for key in failing), outage.attempts
    assert all(outage.refreshes[key] == 0 for key in failing), outage.refreshes


def test_the_degraded_pressure_level_is_the_one_that_truncates():
    """
    What _DEGRADED_ENTRY_SECONDS is chosen for, asserted rather than left to
    its comment: at that cost one pass admits three of the twelve entries and
    the set cannot be swept, which is the sustained-truncation regime
    test_no_entry_is_starved_when_every_pass_truncates exists to run in.

    Both bounds. A cost that admitted the whole set would make that test
    pass for a reason that has nothing to do with the order, and a cost that
    admitted one entry would make every pass a single-entry pass, where a
    rotating deferral and a settling one are again indistinguishable.

    Deliberately NOT asserted as a measurement. The figure is a chosen
    pressure level -- anything from 20s to 29s produces the same three -- and
    the ~21s the module names as discredited is a different quantity
    entirely: a total for nine entries, about 2.3s each. The digits collide
    and nothing else does.
    """
    admitted = 0
    elapsed = 0.0
    while admitted < len(_stats_refresh_targets()) and elapsed < _STATS_REFRESH_PASS_BUDGET_SECONDS:
        elapsed += _DEGRADED_ENTRY_SECONDS
        admitted += 1

    assert admitted == 3
    assert admitted < len(_stats_refresh_targets())


# ============================================================
# THE DEMOTED TIER, WITHIN ONE PASS
# ============================================================
# expires_at does not order this tier and must not. Every row in it is a
# full cache lifetime past expiry, so the column has stopped measuring how
# near anything is to lapsing and measures only how long a failure has
# lasted -- and nothing in the tier is ever written, so an order taken from
# it never changes again. The three tests below are the single-pass half of
# that ruling; the outage simulation further down is the half that needs
# many passes.


def _all_demoted_life():
    """
    Twelve rows every one of which is more than a whole cache lifetime past
    expiry -- the state a sustained outage leaves, and the only state in
    which the tier holds more than one entry to order.

    Their expiries are all DIFFERENT and are laid out in the reverse of key
    order, which is the whole diagnostic value of this fixture. Real demoted
    rows lapsed at different moments and carry different expires_at, and the
    ruling under test is that the column no longer orders them -- so a
    fixture that gave them one shared expiry would let any surviving
    expires_at term break no ties and go unnoticed. Reversed rather than
    merely distinct so that an order taken from expiry is the exact opposite
    of the one taken from keys, and cannot coincide with a rotation at any
    pivot.
    """
    keys = list(_stats_refresh_targets())
    return {
        key: -float(STATS_CACHE_TTL_SECONDS + 60 + position * 30)
        for position, key in enumerate(keys)
    }


@pytest.mark.asyncio
async def test_the_demoted_tier_is_visited_in_key_order_from_the_moving_pivot():
    """
    The exact order, at three different instants, and it is the exact order
    that matters rather than the fact that it changed.

    Key order rotated to start at the pivot is a specific claim with a
    specific consequence: every key leads the tier once per cycle and no key
    can sit behind another for more than one cycle. "The order changed" is
    satisfied by a shuffle, which would also pass while leaving one entry at
    the back far more often than the rest.

    The rows carry twelve DIFFERENT expiries laid out in the reverse of key
    order, so any surviving expires_at term inside the tier produces the
    exact opposite of the answer asserted here. Under the expiry-ordered
    tier that shipped first, all three instants return that reverse order
    and never change; under a version that keeps the rotation split but
    orders within each half by expiry, the halves are internally backwards.
    Identical expiries would hide both, because a column that breaks no ties
    cannot be seen to be breaking them wrongly.
    """
    keys = list(_stats_refresh_targets())
    seen = []

    for step in (0, 1, 7):
        start = _SIMULATED_START + timedelta(seconds=step * _STATS_REFRESH_ROTATION_SECONDS)
        with _clocked_stats_table(_all_demoted_life(), start=start) as table:
            pivot = _rotation_pivot(table.clock.now(timezone.utc), keys)
            due = await _due_stats_entries(table)

        at = keys.index(pivot)
        assert list(due) == keys[at:] + keys[:at], f"pivot {pivot}"
        seen.append(list(due))

    # Three different instants, three different fronts. Without this the
    # assertion above is satisfied by an order that never rotates at all,
    # because a pivot that never moved would still equal keys[0].
    assert len({tuple(order) for order in seen}) == 3


@pytest.mark.asyncio
async def test_the_rotation_cannot_reorder_the_entries_that_are_not_demoted():
    """
    The conjunction in the second ORDER BY term, asserted by what it
    prevents.

    `cache.key < :pivot` standing alone is a term with two values across the
    healthy entries too, so it would sort them by which side of a moving key
    they fall on and only THEN by expiry -- quietly replacing least-life-first
    for exactly the entries something is still writing, which is the property
    the whole order was added for. ANDed with the tier it is constantly False
    outside the tier and can decide nothing there.

    Read at a FULL CYCLE of instants rather than at two, and the expiries
    are non-monotonic in key in BOTH directions. Both of those are things
    this test had to be corrected for, and both hid the mutant they were
    written to catch.

    Two instants is not enough: the pivot has to land somewhere that
    actually splits these four keys before an unfenced term can reorder
    anything, and two arbitrary steps can both land outside them and agree
    for no reason at all.

    Expiries merely "disagreeing with key order" is not enough either. An
    unfenced `cache.key < :pivot` sorts the keys at or after the pivot
    first, which is a partial DESCENDING key order -- so an expiry sequence
    that happens to run opposite to ascending key order runs WITH the mutant
    instead of against it, and every pivot in the cycle returns the expected
    answer. These four are ordered by expiry as au, db_stats, us, de, which
    matches neither direction anywhere.
    """
    life = {
        "db_stats": 40.0,
        "db_stats:au": 10.0,
        "db_stats:de": 120.0,
        "db_stats:us": 90.0,
    }
    expected = ["db_stats:au", "db_stats", "db_stats:us", "db_stats:de"]

    for step in range(len(_stats_refresh_targets())):
        start = _SIMULATED_START + timedelta(seconds=step * _STATS_REFRESH_ROTATION_SECONDS)
        with _clocked_stats_table(life, start=start) as table:
            pivot = _rotation_pivot(table.clock.now(timezone.utc), list(_stats_refresh_targets()))
            order = list(await _due_stats_entries(table))

        assert order == expected, f"pivot {pivot} reordered the healthy entries: {order}"


@pytest.mark.asyncio
async def test_a_demoted_entry_that_succeeds_is_back_in_expiry_order_at_once():
    """
    Leaving the tier costs nothing and needs no bookkeeping: the tier is a
    predicate over expires_at, so the write a successful refresh performs is
    what takes the entry out of it. Nothing has to remember that it recovered.

    Read at three points, because the interesting one is the third. The
    write buys a full cache lifetime, so immediately afterwards the entry is
    not merely undemoted, it is not DUE at all -- which on its own is
    consistent with a cooldown that has simply not expired yet. What settles
    it is letting the entry come due again and finding it at the front of an
    otherwise entirely demoted set, in ordinary expiry order, one refresh
    after it was at the back.
    """
    life = _all_demoted_life()

    with _clocked_stats_table(life) as table:
        while_failing = list(await _due_stats_entries(table))
        table.refresh("db_stats:jp")
        just_written = list(await _due_stats_entries(table))
        # Past the point it comes due again, with everything else still
        # unwritten and sinking further behind.
        table.age(STATS_CACHE_TTL_SECONDS - _STATS_REFRESH_AHEAD_SECONDS + 50)
        due_again = list(await _due_stats_entries(table))

    assert "db_stats:jp" in while_failing
    assert "db_stats:jp" not in just_written
    assert due_again[0] == "db_stats:jp"
    assert set(due_again) == set(while_failing)


# ============================================================
# _release_refresh_lock
# ============================================================


@pytest.mark.asyncio
async def test_releasing_the_lock_unlocks_the_same_id_it_took():
    """A release under a different id leaves the real lock held forever and
    looks entirely healthy in the logs."""
    events = []
    connection = _FakeLockConnection(events)

    await _release_refresh_lock(connection)

    assert connection.unlock_ids == [_STATS_REFRESH_LOCK_ID]
    assert events == ["unlock"]


@pytest.mark.asyncio
async def test_a_failed_unlock_discards_the_connection(caplog):
    """
    Not tidiness. A session-level advisory lock survives the pool's
    reset-on-return, because that reset is a ROLLBACK and a ROLLBACK is
    exactly what does not release one. An unlock that did not happen leaves
    a live pooled backend holding a lock nothing will ever release, every
    worker refused for the life of the process, and a debug line as the only
    trace. Discarding the physical connection ends that backend and Postgres
    releases everything it held.
    """
    events = []
    connection = _FakeLockConnection(events, unlock_error=Exception("connection reset"))

    with caplog.at_level(logging.WARNING):
        await _release_refresh_lock(connection)

    assert events == ["unlock", "invalidate"]
    assert any("connection discarded" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_releasing_the_lock_never_raises_even_when_everything_fails(caplog):
    """
    It runs in a finally. An exception raised there REPLACES whatever was
    already propagating, so a failure to clean up would cost the log line
    naming why the pass actually failed and hand back one about the cleanup
    instead -- the diagnostic swapped for the symptom. Both the unlock and
    the discard are given the chance to fail here; neither may escape.
    """
    events = []
    connection = _FakeLockConnection(
        events,
        unlock_error=Exception("connection reset"),
        invalidate_error=Exception("already gone"),
    )

    with caplog.at_level(logging.WARNING):
        await _release_refresh_lock(connection)

    assert events == ["unlock", "invalidate"]
    assert any("could not be discarded" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_a_lock_already_gone_is_logged_and_not_discarded(caplog):
    """A false return means the lock was not held at release time --
    something took this connection apart underneath the pass. The lock is
    gone either way, which is the outcome wanted, so it is worth a line and
    nothing more; discarding a healthy connection over it would be a cost
    for no gain."""
    events = []
    connection = _FakeLockConnection(events, unlock_result=False)

    with caplog.at_level(logging.WARNING):
        await _release_refresh_lock(connection)

    assert events == ["unlock"]
    assert any("already gone" in r.getMessage() for r in caplog.records)


# ============================================================
# run_stats_refresh_pass — due check, election, release
# ============================================================


@pytest.mark.asyncio
async def test_a_pass_with_nothing_due_never_touches_the_lock(caplog):
    """
    The due check comes BEFORE the election on purpose. Most passes have
    nothing to do -- six workers ticking every twenty seconds against a
    150-second recompute period -- and in that shape a pass costs one
    primary-key lookup and never opens a connection or contends the advisory
    lock at all, so the lock is contended only by workers that intend to
    work.
    """
    events, connection, engine, factory, refresher, patches = _pass_environment(due={})
    with patches[0], patches[1], patches[2], patches[3], caplog.at_level(logging.DEBUG, logger="libex"):
        await run_stats_refresh_pass()

    assert engine.connect_calls == 0
    assert events == []
    refresher.assert_not_called()
    assert any("no entry is due" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_a_pass_with_work_to_do_is_gated_on_the_advisory_lock():
    """WEB_CONCURRENCY is 6 and this loop runs in every worker, so without an
    election the fix multiplies the very query load it exists to bound. The
    lock is taken with the module's fixed id -- an id that varied per worker
    would elect all six."""
    events, connection, engine, factory, refresher, patches = _pass_environment()
    with patches[0], patches[1], patches[2], patches[3]:
        await run_stats_refresh_pass()

    assert connection.lock_ids == [_STATS_REFRESH_LOCK_ID]
    assert refresher.await_count == 1


@pytest.mark.asyncio
async def test_a_worker_that_loses_the_election_does_no_work_and_releases_nothing():
    """The ordinary case for a worker that did have something due. It never
    held the lock, so releasing anyway would decrement somebody else's --
    advisory locks are re-entrant, so an unlock that does not belong to this
    worker takes a lock the winner still needs from two down to one, or off
    it entirely."""
    events, connection, engine, factory, refresher, patches = _pass_environment(elected=False)
    with patches[0], patches[1], patches[2], patches[3]:
        await run_stats_refresh_pass()

    refresher.assert_not_called()
    assert connection.unlock_ids == []
    assert events == ["acquire", "close"]


@pytest.mark.asyncio
async def test_the_lock_connection_is_rolled_back_before_the_pass_begins():
    """
    The edge this module found and closed. pg_try_advisory_lock is a SELECT,
    so it opens a transaction, and a session-scoped advisory lock is held by
    the CONNECTION rather than by that transaction -- so the transaction can
    be ended immediately and must be. Left open, this connection sits
    idle-in-transaction for the length of the pass against the 60s
    idle_in_transaction_session_timeout every connection carries
    (app/db/session.py), and a pass that ever ran long enough to trip it
    would have its election dissolved underneath it at precisely the moment
    the database is slow enough for a second worker's duplicate pass to
    hurt.

    The order is the assertion. A rollback after the pass, or none at all,
    leaves exactly that window open and nothing else in the system would
    show it -- which is why a later reader would take the line for a no-op.
    """
    events, connection, engine, factory, refresher, patches = _pass_environment()
    with patches[0], patches[1], patches[2], patches[3]:
        await run_stats_refresh_pass()

    assert events == ["acquire", "rollback", "pass", "unlock", "close"]


@pytest.mark.asyncio
async def test_the_rollback_does_not_release_the_election():
    """The other half: the lock is still held while the pass runs, so the
    unlock at the end is a real release rather than a second attempt at one
    the rollback already did. A transaction-scoped pg_try_advisory_xact_lock
    would have been dropped by that rollback -- and, in production, by the
    first cache.set the pass commits."""
    events, connection, engine, factory, refresher, patches = _pass_environment()
    with patches[0], patches[1], patches[2], patches[3]:
        await run_stats_refresh_pass()

    assert events.index("rollback") < events.index("pass") < events.index("unlock")
    assert connection.unlock_ids == [_STATS_REFRESH_LOCK_ID]


@pytest.mark.asyncio
async def test_the_lock_is_taken_on_a_connection_of_its_own():
    """Never on a session doing the counting. That session commits once per
    entry through cache.set, and a lock riding on it would be released by
    the first of those commits -- so a second worker would start its own
    pass halfway through this one's. Two sessions are opened, the due check
    and the pass, and the lock is on neither."""
    events, connection, engine, factory, refresher, patches = _pass_environment()
    with patches[0], patches[1], patches[2], patches[3]:
        await run_stats_refresh_pass()

    assert engine.connect_calls == 1
    assert factory.calls == 2
    assert refresher.await_args[0][0] is factory.session
    assert refresher.await_args[0][0] is not connection


@pytest.mark.asyncio
async def test_the_lock_is_released_even_when_the_pass_raises():
    """A session-level lock left behind on a pooled connection outlives the
    code that took it and would silence every later pass in this process --
    a refresher that stops working with nothing in the logs to say so. The
    finally is what stops one failed pass costing the instance the feature.
    """
    events, connection, engine, factory, refresher, patches = _pass_environment(
        refresh_side_effect=RuntimeError("counting fell over")
    )
    with patches[0], patches[1], patches[2], patches[3]:
        with pytest.raises(RuntimeError):
            await run_stats_refresh_pass()

    assert connection.unlock_ids == [_STATS_REFRESH_LOCK_ID]
    assert events == ["acquire", "rollback", "pass", "unlock", "close"]


@pytest.mark.asyncio
async def test_the_lock_is_released_when_the_pass_is_cancelled():
    """
    CancelledError is a BaseException, so it slips past `except Exception`
    entirely -- and the pass has a cancellation of its own built in, the
    hard ceiling. A cleanup guarded by anything narrower than a finally
    would leak the lock in exactly the case the ceiling exists to produce.
    """
    events, connection, engine, factory, refresher, patches = _pass_environment(
        refresh_side_effect=asyncio.CancelledError()
    )
    with patches[0], patches[1], patches[2], patches[3]:
        with pytest.raises(asyncio.CancelledError):
            await run_stats_refresh_pass()

    assert connection.unlock_ids == [_STATS_REFRESH_LOCK_ID]


@pytest.mark.asyncio
async def test_an_acquire_that_fails_mid_flight_still_attempts_a_release():
    """
    Why `elected` starts as None rather than False. Three states have to be
    told apart in the finally: never asked, asked and refused, and asked
    without a usable answer -- a connection that died between the server
    taking the lock and the answer arriving. Only a definite refusal is safe
    to skip the release on; the unknown case must release, because the one
    outcome that cannot be recovered from is a lock left held.
    """
    events, connection, engine, factory, refresher, patches = _pass_environment(
        acquire_error=Exception("server closed the connection unexpectedly")
    )
    with patches[0], patches[1], patches[2], patches[3]:
        with pytest.raises(Exception, match="server closed"):
            await run_stats_refresh_pass()

    assert events == ["acquire", "unlock", "close"]
    refresher.assert_not_called()


@pytest.mark.asyncio
async def test_a_rollback_that_fails_after_election_still_releases_the_lock():
    """
    The weld: acquire and release are one try/finally with nothing
    unprotected between them. An earlier version put this rollback in that
    gap, where anything raised exited with the lock held and permanently
    dead -- and the rollback is a statement against a database this module
    only ever runs when that database is under load.
    """
    events, connection, engine, factory, refresher, patches = _pass_environment(
        rollback_error=Exception("connection lost during rollback")
    )
    with patches[0], patches[1], patches[2], patches[3]:
        with pytest.raises(Exception, match="connection lost"):
            await run_stats_refresh_pass()

    assert connection.unlock_ids == [_STATS_REFRESH_LOCK_ID]
    refresher.assert_not_called()


@pytest.mark.asyncio
async def test_a_slow_pass_stops_at_its_budget_rather_than_being_cancelled(caplog):
    """
    The two limits doing their two different jobs, exercised together
    through the real function. The budget and the ceiling are the module's
    own constants scaled by one common factor, so what is under test is the
    RATIO between them: with the ceiling above the budget the pass overruns
    the soft deadline, stops cleanly between entries and says what it left
    behind. Set the two equal -- which is how this was first written -- and
    the same pass is cancelled 0.1s after the deadline admits an entry, the
    graceful path never runs, and a test that only checked "the pass ended"
    would not have noticed.
    """
    events = []
    connection = _FakeLockConnection(events)
    engine = _FakeEngine(connection)
    factory = _FakeSessionFactory(AsyncMock())
    due = {"db_stats": None, "db_stats:de": "de", "db_stats:us": "us"}

    async def _slow_refresh(*args, **kwargs):
        await asyncio.sleep(_SCALED_ENTRY_SECONDS)
        return _stats_result()

    with patch.object(stats_refresh, "engine", engine), \
         patch.object(stats_refresh, "AsyncSessionFactory", factory), \
         patch.object(stats_refresh, "_due_stats_entries", AsyncMock(return_value=due)), \
         patch.object(stats_refresh, "get_db_stats", AsyncMock(side_effect=_slow_refresh)), \
         patch.object(stats_refresh, "_STATS_REFRESH_PASS_BUDGET_SECONDS", _SCALED_BUDGET), \
         patch.object(stats_refresh, "_STATS_REFRESH_PASS_CEILING_SECONDS", _SCALED_CEILING), \
         caplog.at_level(logging.WARNING):
        await run_stats_refresh_pass()

    budget = [r for r in caplog.records if "hit its budget" in r.getMessage()]
    assert len(budget) == 1
    assert budget[0].refreshed == 1
    assert budget[0].skipped == 2
    assert connection.unlock_ids == [_STATS_REFRESH_LOCK_ID]


@pytest.mark.asyncio
async def test_a_single_hung_statement_is_cut_off_by_the_hard_ceiling(caplog):
    """
    What the between-entries check cannot catch. A statement that never
    returns is never between entries, so only the asyncio.timeout ends it --
    and it has to, because a pass that hangs holds the election for as long
    as it hangs and starves every other worker of the chance to do the work
    instead. That is precisely the condition this module exists for, so
    leaving it unbounded means the module is absent exactly when it is
    needed.

    The pass reports the cut-off here and returns rather than raising it,
    because here is the only place the ceiling can be told apart from every
    other TimeoutError a pass can meet. The lock is still released on the way
    out, which is the half a caller-side report would be most likely to lose.
    """
    events = []
    connection = _FakeLockConnection(events)
    engine = _FakeEngine(connection)
    factory = _FakeSessionFactory(AsyncMock())

    async def _hang(*args, **kwargs):
        await asyncio.sleep(3600)

    with patch.object(stats_refresh, "engine", engine), \
         patch.object(stats_refresh, "AsyncSessionFactory", factory), \
         patch.object(stats_refresh, "_due_stats_entries", AsyncMock(return_value={"db_stats": None})), \
         patch.object(stats_refresh, "get_db_stats", AsyncMock(side_effect=_hang)), \
         patch.object(stats_refresh, "_STATS_REFRESH_PASS_BUDGET_SECONDS", _SCALED_BUDGET), \
         patch.object(stats_refresh, "_STATS_REFRESH_PASS_CEILING_SECONDS", _SCALED_CEILING), \
         caplog.at_level(logging.WARNING):
        await run_stats_refresh_pass()

    cut_off = [r for r in caplog.records
               if r.getMessage() == "Stats refresh pass was cut off by its hard ceiling"]
    assert len(cut_off) == 1
    assert cut_off[0].passCeilingSeconds == _SCALED_CEILING
    assert connection.unlock_ids == [_STATS_REFRESH_LOCK_ID]
    assert events[-2:] == ["unlock", "close"]


@pytest.mark.asyncio
async def test_a_timeout_from_inside_the_pass_is_not_reported_as_the_ceiling(caplog):
    """
    The attribution, which is the whole reason the ceiling is handled here
    and not around the loop. TimeoutError is reachable from several places a
    pass touches and only one of them is the ceiling: a blackholed host
    raises a real builtins.TimeoutError out of engine.connect() at 30.02s and
    SQLAlchemy's asyncpg dialect does not translate it. Reported as the
    ceiling, that connect failure came stamped with a ceiling figure the pass
    never came near -- and a wrong cause is worse than the blank one it
    replaced, because a number invites the reader to reason from it.

    So a TimeoutError raised from inside the ceiling's scope, with the
    ceiling itself unexpired, has to come back out untouched for the loop to
    report as what it is. Nothing else in the module distinguishes the two,
    and both versions "time out".
    """
    events = []
    connection = _FakeLockConnection(events)
    engine = _FakeEngine(connection)
    factory = _FakeSessionFactory(AsyncMock())

    with patch.object(stats_refresh, "engine", engine), \
         patch.object(stats_refresh, "AsyncSessionFactory", factory), \
         patch.object(stats_refresh, "_due_stats_entries", AsyncMock(return_value={"db_stats": None})), \
         patch.object(stats_refresh, "get_db_stats", AsyncMock(side_effect=TimeoutError())), \
         patch.object(stats_refresh, "_STATS_REFRESH_PASS_BUDGET_SECONDS", _SCALED_BUDGET), \
         patch.object(stats_refresh, "_STATS_REFRESH_PASS_CEILING_SECONDS", _SCALED_CEILING), \
         caplog.at_level(logging.WARNING):
        with pytest.raises(TimeoutError):
            await run_stats_refresh_pass()

    assert not any("hard ceiling" in r.getMessage() for r in caplog.records)
    assert connection.unlock_ids == [_STATS_REFRESH_LOCK_ID]


# ============================================================
# stats_refresh_loop
# ============================================================


@pytest.mark.asyncio
async def test_the_loop_runs_a_pass_before_its_first_sleep():
    """
    Deliberately unlike the cache purge loop. The cache lives in Postgres
    and survives a deploy, so a worker starting up inherits entries of any
    age -- one of them may be past the threshold already. Sleeping first
    would leave that gap open for a full interval.
    """
    events = []

    async def _pass():
        events.append("pass")

    async def _sleep(seconds):
        events.append(f"sleep:{seconds}")
        raise asyncio.CancelledError

    with patch.object(stats_refresh, "run_stats_refresh_pass", AsyncMock(side_effect=_pass)), \
         patch.object(stats_refresh.asyncio, "sleep", AsyncMock(side_effect=_sleep)):
        with pytest.raises(asyncio.CancelledError):
            await stats_refresh_loop()

    assert events == ["pass", f"sleep:{STATS_REFRESH_INTERVAL_SECONDS}"]


@pytest.mark.asyncio
async def test_the_loop_survives_a_failing_pass_and_keeps_going(caplog):
    """
    Never raises. Letting the exception out kills the task, and a cancelled
    background task is silent -- the endpoint drops back to its lazy path
    with nothing anywhere to say the refresher is gone, and the badges start
    failing again days later for a reason nobody can see.

    What the line carries is the exception's TYPE and the server's own
    fields, never the exception itself. That is the point rather than a
    limitation: Postgres puts the offending row into its message text, so
    interpolating a failure here would publish book data into the logs of a
    public service. The type is what an operator actually acts on.
    """
    passes = 0

    async def _pass():
        nonlocal passes
        passes += 1
        raise RuntimeError("database unreachable")

    sleeps = 0

    async def _sleep(seconds):
        nonlocal sleeps
        sleeps += 1
        if sleeps == 3:
            raise asyncio.CancelledError

    with patch.object(stats_refresh, "run_stats_refresh_pass", AsyncMock(side_effect=_pass)), \
         patch.object(stats_refresh.asyncio, "sleep", AsyncMock(side_effect=_sleep)), \
         caplog.at_level(logging.WARNING):
        with pytest.raises(asyncio.CancelledError):
            await stats_refresh_loop()

    assert passes == 3
    failures = [r for r in caplog.records if r.getMessage() == "Stats refresh pass failed"]
    assert len(failures) == 3
    assert failures[0].error_type == "RuntimeError"
    assert not any("database unreachable" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_a_timeout_reaching_the_loop_is_survived_and_named(caplog):
    """
    A TimeoutError is what the loop is likeliest to be handed and the hardest
    for it to say anything about: str() on one is the empty string, so the
    line that interpolated the exception read "Stats refresh pass failed: "
    and stopped, naming no cause at all, on the most pathological failure the
    module has. The structured fields are what make it legible, and error_type
    is the whole of what they can say about this one.

    The loop survives it at all only because asyncio.timeout raises
    TimeoutError rather than CancelledError at its boundary -- an Exception,
    so `except Exception` holds it. That is a property of the boundary and
    not an obvious one: a change to how a pass is bounded could start letting
    a BaseException out instead and kill the task silently.

    One handler, not two. A TimeoutError arriving here is by construction one
    the ceiling did not raise, since the ceiling names itself where it fires;
    a second branch here could not tell the two apart and mislabelled the
    other one when it tried.
    """
    sleeps = 0

    async def _sleep(seconds):
        nonlocal sleeps
        sleeps += 1
        if sleeps == 2:
            raise asyncio.CancelledError

    with patch.object(stats_refresh, "run_stats_refresh_pass", AsyncMock(side_effect=TimeoutError())), \
         patch.object(stats_refresh.asyncio, "sleep", AsyncMock(side_effect=_sleep)), \
         caplog.at_level(logging.WARNING):
        with pytest.raises(asyncio.CancelledError):
            await stats_refresh_loop()

    assert sleeps == 2
    failures = [r for r in caplog.records if r.getMessage() == "Stats refresh pass failed"]
    assert len(failures) == 2
    assert failures[0].error_type == "TimeoutError"
    assert not any("hard ceiling" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_the_loop_waits_the_interval_between_passes():
    """The discovery cadence itself: two passes an interval apart, not a
    tight loop that would turn cheap ticks into a continuous one."""
    intervals = []

    async def _sleep(seconds):
        intervals.append(seconds)
        if len(intervals) == 2:
            raise asyncio.CancelledError

    with patch.object(stats_refresh, "run_stats_refresh_pass", AsyncMock()) as mock_pass, \
         patch.object(stats_refresh.asyncio, "sleep", AsyncMock(side_effect=_sleep)):
        with pytest.raises(asyncio.CancelledError):
            await stats_refresh_loop()

    assert intervals == [STATS_REFRESH_INTERVAL_SECONDS, STATS_REFRESH_INTERVAL_SECONDS]
    assert mock_pass.await_count == 2


@pytest.mark.asyncio
async def test_the_loop_announces_every_number_it_runs_on(caplog):
    """All five on one line. The interval means nothing on its own -- it is
    a discovery cadence, and what decides the load is the threshold, while
    what decides how a bad pass ends is the budget and the ceiling. An
    operator reading a startup log should be able to reconstruct the ledger
    without reading the source."""
    async def _sleep(seconds):
        raise asyncio.CancelledError

    with patch.object(stats_refresh, "run_stats_refresh_pass", AsyncMock()), \
         patch.object(stats_refresh.asyncio, "sleep", AsyncMock(side_effect=_sleep)), \
         caplog.at_level(logging.INFO):
        with pytest.raises(asyncio.CancelledError):
            await stats_refresh_loop()

    started = [r for r in caplog.records if r.getMessage() == "Stats refresher started"]
    assert len(started) == 1
    assert started[0].intervalSeconds == STATS_REFRESH_INTERVAL_SECONDS
    assert started[0].refreshAheadSeconds == _STATS_REFRESH_AHEAD_SECONDS
    assert started[0].passBudgetSeconds == _STATS_REFRESH_PASS_BUDGET_SECONDS
    assert started[0].passCeilingSeconds == _STATS_REFRESH_PASS_CEILING_SECONDS
    assert started[0].ttlSeconds == STATS_CACHE_TTL_SECONDS


# ============================================================
# THE SELF-PERPETUATING WARM SET
# ============================================================
# Documented behaviour, not a fix. Eviction was considered and refused: any
# eviction rule has to let an entry go cold to find out whether anybody still
# wants it, and an entry going cold is the defect this module removes. What
# is pinned here is that nothing pretends otherwise.


@pytest.mark.asyncio
async def test_a_refreshed_entry_is_never_evicted_by_this_module():
    """
    A key enters the warm set the first time anybody queries that scope, and
    from then on this loop renews it, so it never expires and purge_expired
    can never collect it -- demonstrated against Postgres 16 with an
    already-lapsed db_stats:jp entry that one pass revived and no number of
    purges removed. One scanner sweeping all eleven regions commits the
    instance to twelve sweeps every 150 seconds -- one per region and the
    unscoped one -- permanently, and an operator's remedy is deleting the
    row rather than anything this module does.

    Pinned as the documented behaviour it is: this module invalidates
    nothing and deletes nothing, so a future "cleanup" that quietly starts
    evicting has to argue with the ruling instead of slipping past it.
    """
    session = AsyncMock()
    due = {"db_stats": None, "db_stats:jp": "jp"}
    with patch.object(stats_refresh, "_due_stats_entries", AsyncMock(return_value=due)), \
         patch.object(stats_refresh, "get_db_stats", AsyncMock(return_value=_stats_result())), \
         patch.object(cache, "invalidate", AsyncMock()) as mock_invalidate, \
         patch.object(cache, "purge_expired", AsyncMock()) as mock_purge:
        await _refresh_due_stats(session, _far_deadline())

    mock_invalidate.assert_not_called()
    mock_purge.assert_not_called()


@pytest.mark.asyncio
async def test_the_module_creates_no_entry_that_was_not_already_stored():
    """
    The other half of the trade, and the operator's remedy. Every write this
    module causes goes through get_db_stats, which renews a key that is
    already stored and creates none of its own -- so deleting a stats row
    takes it out of the warm set until a request re-creates it, and a
    version that seeded the twelve keys itself would make that remedy
    useless.

    Deleting them takes TWO predicates and this test drives both key shapes
    for that reason. The eleven region entries are `db_stats:xx` and the
    unscoped entry is plain `db_stats`, which a `db_stats:%` glob does not
    match -- and the module's own ledger measures that unscoped entry as by
    far the most expensive of the twelve, so it is the one an operator is
    most likely to be trying to remove. `WHERE key = 'db_stats' OR key LIKE
    'db_stats:%'` covers both.

    Driven over a due entry first, so a write on the path a pass actually
    takes fails here; the source is read afterwards because a write added to
    a branch this pass never reaches would otherwise be invisible until an
    operator found a deleted row back again.

    The session is asserted to have been handed no statement at all, rather
    than only the cache writer to have gone uncalled. Every write in this
    codebase goes through a session, so that holds whatever name a write is
    reached by -- where mocking cache.set catches only a write that reaches
    it through the module attribute, and an alias bound at import escapes
    both it and the source read.
    """
    session = AsyncMock()
    due = {"db_stats": None, "db_stats:jp": "jp"}
    with patch.object(stats_refresh, "_due_stats_entries", AsyncMock(return_value=due)), \
         patch.object(stats_refresh, "get_db_stats", AsyncMock(return_value=_stats_result())), \
         patch.object(cache, "set", AsyncMock()) as mock_set:
        await _refresh_due_stats(session, _far_deadline())

    mock_set.assert_not_called()
    session.execute.assert_not_awaited()

    source = inspect.getsource(stats_refresh)
    assert "cache.set(" not in source
    assert "cache.invalidate(" not in source


# ============================================================
# LIFESPAN WIRING
# ============================================================
# Everything above tests a refresher nothing has started. The three below
# hold the other end: the loop is only worth anything if the application
# actually runs it, and a task that is never created fails no test in this
# file. One holds the startup, and the two after it hold the shutdown --
# the refresher's own task, and the cache purge loop it shares two adjacent
# cancel lines with.


def test_startup_actually_starts_the_refresher():
    """
    The loop begins running as part of the lifespan, in this worker. Without
    this the whole module could be dead code -- imported, correct, and never
    scheduled -- and every entry would go back to being recomputed by
    whichever reader arrived first after it lapsed, which is exactly the
    state the badges failed in.

    A request is made inside the block to give the event loop a turn, so
    what is asserted is that the coroutine started, not merely that
    create_task was called on it.
    """
    started = []

    async def fake_loop():
        started.append(True)
        await asyncio.sleep(3600)

    with patch("app.main.stats_refresh_loop", fake_loop):
        with TestClient(app) as client:
            client.get("/health")

    assert started == [True]


def _task_states_at_dispose(refresher):
    """
    Run one lifespan and read every background task's cancellation state at
    the moment shutdown reaches engine.dispose(), as (qualname, cancelling,
    done) in creation order.

    Sampled there rather than after the client exits, because the portal
    TestClient runs on cancels whatever is still pending when it tears the
    event loop down -- so a task the lifespan never touched finishes
    indistinguishable from one it cancelled, and an assertion made
    afterwards passes whether the shutdown did anything or not. dispose is
    the first await after both cancels and before that teardown, and it is
    stubbed rather than allowed to run so the sample cannot depend on a real
    engine being reachable.
    """
    states = []
    tasks = []
    real_create_task = asyncio.create_task

    def tracking_create_task(coro, **kwargs):
        task = real_create_task(coro, **kwargs)
        tasks.append(task)
        return task

    def sample():
        states.extend(
            (t.get_coro().__qualname__, t.cancelling(), t.done()) for t in tasks
        )

    stub_engine = SimpleNamespace(dispose=AsyncMock(side_effect=sample))
    with patch("app.main.stats_refresh_loop", refresher), \
         patch("app.main.engine", stub_engine), \
         patch("app.main.asyncio.create_task", side_effect=tracking_create_task):
        with TestClient(app) as client:
            client.get("/health")

    stub_engine.dispose.assert_awaited_once()
    return states


async def _idle_loop():
    """A stand-in for a background loop that is still mid-pass when shutdown
    arrives: it never finishes on its own, so anything that ends it was the
    shutdown's doing."""
    await asyncio.sleep(3600)


def test_shutdown_asks_the_refresher_to_stop_without_waiting_for_it():
    """
    cancel() is requested on the refresher's own task, and the task is not
    awaited afterwards. Awaiting it would hold the shutdown open for the
    length of a pass -- up to the full pass ceiling of counting -- every time
    a worker restarts, and a deploy restarts six of them.

    Both halves are read at dispose: one pending cancellation request, and
    the task still unfinished at a step the shutdown had already moved on
    to. The pair is what separates this from the harness's own teardown,
    which cancels every surviving task and would report the same thing about
    a shutdown that did nothing.
    """
    states = _task_states_at_dispose(_idle_loop)

    assert [s for s in states if s[0] == _idle_loop.__qualname__] == [
        (_idle_loop.__qualname__, 1, False)
    ]


def test_shutdown_asks_the_cache_purge_loop_to_stop_the_same_way():
    """
    The lifespan's other background task, held to the same shutdown for the
    same reason: the two cancels are adjacent lines, and a tidy-up that
    drops one drops the other exactly as silently -- neither leaves a
    failing test behind on its own.

    Nothing about the purge loop is stubbed. What is being read is the
    lifespan's treatment of the task, and the real coroutine sleeps before
    its first purge, so it reaches shutdown having touched nothing.
    """
    states = _task_states_at_dispose(_idle_loop)

    assert [s for s in states if s[0] == "_cache_purge_loop"] == [
        ("_cache_purge_loop", 1, False)
    ]
