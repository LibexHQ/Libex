"""
Keeps the /db/stats cache entries warm.

get_db_stats is cache-aside: an entry lapses after STATS_CACHE_TTL_SECONDS
and the next caller pays the full cost of recomputing it. Measured against
the live instance, that cost is 0.12s warm; cold it is 1.6s for a cheap
region (de), 4.7s for the dearest measured region (us), and ~15.3s for the
unscoped global entry, which is not bounded by the region figures because
every region-scoped count is a filtered subset of one it does in full.
The reader who happens to arrive first after an entry lapses is the one who
pays that, in front of an <img>: the README's counters are Libex's own SVGs
(app/api/routes/db/badge.py) reading these same entries, and the 37 fetches
one README render sends arrive together, so it is a whole screen of them at
once. Nothing third-party bounds that wait any more. While the counters were
shields.io badges pointed at /db/stats, shields gave an upstream fetch about
3.5 seconds before it rendered "inaccessible"; they now go to libexdb.com in
one hop through GitHub's camo proxy, so what a reader sees is no longer a
grey plate but a badge that takes the whole recompute to draw, or the
broken-image icon if camo gives up first. No ceiling of camo's own has been
measured and none is assumed here -- 15.3 seconds is the defect whichever way
it ends. The five global badges at the top of the README are the ones drawing
the unscoped entry.

This module shrinks that window rather than making cold cheaper: a
background pass recomputes each stored stats entry once its remaining life
drops below _STATS_REFRESH_AHEAD_SECONDS, so in ordinary operation no
request finds one expired. It is NOT a guarantee, and the constants below
say exactly where it stops holding: a pass that fails outright at the wrong
moment can still let one entry lapse. The edge's stale-while-revalidate
(app/api/routes/db/stats_headers.py) covers part of that residue; the next
pass restores the entry within an interval.

Part of it, and the remainder is what earns this module rather than a
header. stale-while-revalidate can only serve a stale copy at an edge PoP
that already holds one. The counters are 37 distinct origin URLs -- one per
badge since they became Libex's own SVGs, where four region counts used to
share one `?region=xx` response and the README drew nine -- spread across
Cloudflare's whole PoP fleet, so a large share of first-requests at any given
PoP are a genuine MISS, where the directive does nothing at all and the
reader waits 15.3 seconds on the origin for the unscoped entry. Four times
the URLs over the same fleet makes more of those requests a MISS rather than
fewer, so that repoint widened the case this loop covers instead of
narrowing it. It did not widen what the origin recomputes: the 37 read the
same nine cache entries between them, so what changed is only how many
readers reach one cold. That is the case this loop covers and the header
cannot. Recorded here because stats_headers.py argues the stale window from
the gap between README renders instead, which is the weaker of the two
reasons and is not the one that justifies the loop.

WHAT IT COSTS, AND WHO PAYS FOR IT. The set of entries kept warm is
self-perpetuating. A key enters it the first time anybody queries that
scope, and from then on this loop renews it, so it never expires and
purge_expired can never collect it -- demonstrated against Postgres 16 with
an already-lapsed db_stats:jp entry that one pass revived and no number of
purges removed afterwards. Presence in the cache table is therefore a
demand signal exactly once, before the first pass touches it; after that it
means only that somebody asked, once, at some point. One scanner, or one
fork of the README sweeping all eleven regions, permanently commits that
instance to twelve count sweeps every 150 seconds -- eleven region-scoped
and one unscoped.

It is bounded at twelve entries, never more, and an operator shrinks it by
deleting the unwanted rows: this loop creates nothing, so a key it does not
find does not come back until a request re-creates it. One race qualifies
that. cache.set is an upsert, so a DELETE landing after a pass has read its
due set but before that entry is reached is undone by the pass's own write,
and the row is back with a fresh expiry. Re-running the delete against an
idle instance settles it. Deleting them takes two predicates, not one: the eleven region entries are `db_stats:xx`, but
the unscoped entry is plain `db_stats`, which a `db_stats:*` glob does not
match -- and the ledger below measures that unscoped entry as by far the
most expensive of the twelve, so it is the one an operator is most likely
to want gone. `DELETE FROM cache WHERE key = 'db_stats' OR key LIKE
'db_stats:%'` covers both.

That is a deliberate trade and not an oversight. Eviction was considered
and refused, because any eviction rule has to let an entry go cold to
discover whether anyone still wants it -- and an entry going cold is the
single defect this module exists to remove. A rule that periodically breaks
the public instance's badges in order to save a scanned self-hoster some
counting has the trade backwards. Nothing cheaper is available either:
cache.set records value, created_at and expires_at and nothing else, all
three identical whether a request or this loop wrote them, and the only way
to tell them apart would be a marker written on the hottest public read
Libex serves.
"""

# Standard library
from datetime import datetime, timedelta, timezone
import asyncio

# Third party
from sqlalchemy import and_, case, select, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

# Database
from app.db.models import Cache
from app.db.session import AsyncSessionFactory, engine

# Services
from app.services.audible.client import VALID_REGIONS
from app.services.cache import manager as cache
from app.services.db.reader import STATS_CACHE_TTL_SECONDS, get_db_stats
from app.services.db.writer import _failure_fields

# Core
from app.core.logging import get_logger

logger = get_logger()


# How often a worker looks, and how much life an entry has to have left
# before it is recomputed. These two do different jobs and that separation
# is the whole design: the INTERVAL bounds how long a due entry waits to be
# noticed, the AHEAD threshold alone decides how often one is actually
# recomputed. Tying the recompute rate to the tick rate -- an earlier
# version of this module did exactly that, refreshing everything stored on
# every pass -- makes the query load a function of how many workers happen
# to be ticking and how far apart they have drifted, which is not something
# this module controls. WEB_CONCURRENCY is 6, uvicorn respawns a worker
# whenever it feels like it, and pass durations vary, so those ticks
# scatter: simulated over 24h with duration varying by +/-8s, an
# unthresholded loop ran ~2,000 passes a day at ~55% duty cycle on the most
# expensive query in the service. No paper figure is quoted beside that
# one, because every estimate this comment has carried for it was
# single-ticker arithmetic set against a six-worker measurement, which is
# the mistake the 55% exists to show rather than a comparison. With the
# threshold, a tick that finds nothing due costs one indexed lookup --
# measured at 0.196ms against 500k cache rows -- and the recompute rate is
# pinned at one per entry per
# (STATS_CACHE_TTL_SECONDS - _STATS_REFRESH_AHEAD_SECONDS) = 150s no matter
# how many ticks it takes to get there.
#
# What that pins the cost AT. Three entries have been measured cold, and
# they do not scale with one another: db_stats:de at 1.6s, db_stats:us at
# 4.7s, and the unscoped db_stats at ~15.3s. That last figure is the whole
# shape of this module's cost and it went unmeasured until 2026-09-05,
# every aggregate here before then having quietly assumed the unscoped
# entry was bounded by the region-scoped ones. It is not, and not by a
# little: every region-scoped count is a filtered subset of a count the
# unscoped entry does in full. Read it as an UPPER bound on database time
# rather than as database time -- it is end-to-end wall clock against the
# live instance (delete the row, time one request), so TLS, Cloudflare,
# network and origin are all inside it. What it settles beyond argument is
# the direction and the order of magnitude.
#
# A twelve-entry sweep reproduced from those three endpoints and nothing
# else therefore spans:
#   - 15.3 + (11 x 1.6) = 32.9s, every region at the cheapest measured;
#   - 15.3 + (11 x 4.7) = 67.0s, every region at the dearest measured.
# Once per 150s in one worker, that is a 22% to 45% duty cycle on a single
# pooled connection. No single number is quoted for it, because none is
# derivable: the two regions that have been measured differ by 3x and the
# nine between them have not been measured at all.
#
# THREE aggregates here have now had to be corrected, and it has been the
# same defect every time -- "roughly 1.5s" per entry, then a 40s sweep,
# then 35.1s. The 35.1 is the instructive one: the round that produced it
# was itself a correction, and it fixed the number instead of the class. It
# kept an unsupported ~21s term for nine entries, added a defensible 14.1s
# for the other three, and presented the sum as derived -- so the defect
# survived inside its own fix, and was then promoted from prose into a
# pinned test constant. An aggregate presented as derived has to be
# reproducible from the endpoints named beside it, or it has to say which
# end of a range it is and why. The range above is the reproducible
# statement, and it is deliberately not collapsed to a point.
#
# The cheapest further reduction available is hoisting the narrator count:
# narrators has no region column, so every one of the eleven region-scoped
# entries pays a full unscoped count of it, and one per sweep would do.
# Refused, and recorded so it is not re-proposed as an oversight. It is
# worth perhaps a tenth to a fifth of a sweep, and buying it means widening
# get_db_stats with a caller-supplied count -- putting a parameter on a
# public, unauthenticated response's numbers whose only correct value is
# the one the function would have computed anyway. A background loop's
# duty cycle is not worth that seam. What would change the ruling: a
# measured contention problem from this loop, at which point the fix is on
# the query rather than the signature -- an index-only-scan-friendly path,
# or the single multi-subquery statement already weighed and rejected for
# the request path in the caching slice.
#
# THE LEDGER THE THRESHOLD IS SIZED BY, worst case, single worker:
#   - an entry becomes due with 150s of life left;
#   - worst wait until a pass that can see it even BEGINS is one pass
#     CEILING plus one interval, because a pass that started just before it
#     came due has to finish first and the ceiling is what bounds when a
#     pass ends. The budget only stops a pass ADMITTING further entries; the
#     one admitted just under it still gets its statement_timeout on top,
#     and covering that overrun is what the 30s between the two constants
#     below buys. The wait itself is a different sum from that gap: the
#     ceiling plus one interval, 90 + 20 = 110s;
#   - worst time within that pass to reach it is a full cold sweep of all
#     twelve possible entries, which the range above puts between 32.9s and
#     67.0s;
#   - 110 + 32.9 = 142.9s against 150s of life, a margin of +7.1s at the
#     cheap end -- and 110 + 67.0 = 177.0s, a margin of -27.0s at the dear
#     end.
#
# The entry that last term is about is the LEAST urgent one in the due set,
# because _due_stats_entries hands them over in expiry order, among the
# entries something is writing. That is what makes this the right
# inequality to check: whatever is nearest to lapsing is recomputed first,
# so an entry waits behind a whole sweep only when it is the one with the
# most life left, which is the only case where a whole sweep still fits
# inside what it has left.
#
# SO THE MARGIN IS NOT ESTABLISHED. That is stated rather than resolved by
# adopting whichever end of the range keeps the inequality true, which is
# exactly how a 4.9s margin came to be claimed here. Two consequences
# follow, and they are different claims that have to be kept apart:
#
# The arithmetic worst case is not proven, and the dear end also exceeds
# _STATS_REFRESH_PASS_BUDGET_SECONDS outright -- a 67.0s sweep does not fit
# a 60s budget, so at those costs every pass truncates.
#
# But truncating is not lapsing, and simulating the loop separates them.
# Run at the dear end, a 67.0s sweep against the 60s budget with every pass
# truncating, over two hours: all twelve entries were still refreshed 39 or
# 40 times each, and no entry spent any time expired at all. The ledger's
# three terms are not simultaneously reachable -- a pass that runs to its
# 90s ceiling is a degraded database and a 67.0s sweep is a healthy one --
# and what truncation actually costs is that the entry with the most life
# left waits for the next pass, which by construction it can afford. The
# only lever that widens the ledger is the threshold, which shortens the
# effective refresh period and raises the duty cycle above on every instance
# permanently.
#
# What that leaves is a real question about the CONSTANTS rather than about
# this comment, and it is deliberately left open here: whether a 60s budget
# is the right size for a sweep whose measured upper end is 67.0s. Resizing
# it moves the ceiling, which moves the 110s wait term, which moves the
# ledger again, so it is a design change with its own measurements and not
# something to fold into a correction. The measurement it wants first is an
# EXPLAIN (ANALYZE, BUFFERS) on the unscoped counts, to replace the 15.3s
# wall-clock upper bound with actual database time.
#
# WHERE IT STOPS HOLDING, stated rather than designed away: if that pass
# fails outright, the next one lands up to 110s later, past the 150, and the
# entry lapses until a pass lands. One failed pass at the wrong moment is
# the documented residue, not an impossibility. Sustained per-entry cost
# above the measured range is the other, and with nothing failing it is
# survivable rather than fatal: once a sweep exceeds the budget every pass
# truncates, and expiry order is the only thing deciding that the deferral
# rotates instead of settling on the same entries. That last part stops
# being true once entries are ALSO failing -- from 22s per region upward,
# with two failing, one healthy entry does settle and refreshes no further.
# It is measured and owned on _due_stats_entries, where the tier that
# strands it lives. Neither of those is the case where an entry does not
# merely cost a lot but FAILS, which expiry order alone gets exactly
# backwards -- see _due_stats_entries.
#
# The interval is 20s rather than something nearer the threshold because an
# idle tick is now nearly free: six workers ticking every 20s is ~26,000
# discovery queries a day, ~5.1 seconds of total database time. Cheap ticks
# buy a tighter worst-case wait, which is the only term in the ledger above
# that is bought rather than measured.
STATS_REFRESH_INTERVAL_SECONDS = 20
_STATS_REFRESH_AHEAD_SECONDS = 150

# Hard ceiling on one pass, serving two different failures with one number.
#
# Nothing else bounds a pass: statement_timeout is 30s (app/db/session.py),
# an entry costs six or seven statements on the refresh path -- five counts,
# a sixth for seriesRegionUnknown when the entry is region-scoped, and the
# cache upsert, with the cache read skipped because refresh=True
# (services/db/reader.py) -- and up to twelve entries are possible, so a
# degraded-but-alive database yields a pass measured in
# minutes, holding the election the whole time and starving every other
# worker of the chance to do the work instead. That is precisely the
# condition this module exists for, so leaving it unbounded means the module
# is absent exactly when it is needed.
#
# Enforced twice, deliberately, because the two mechanisms fail differently.
# A deadline checked BETWEEN entries stops an ordinary slow pass cleanly:
# nothing is cancelled mid-statement, the entries already refreshed keep
# their new expiry, and the ones not reached are still due, so the next tick
# takes them 20 seconds later. That is graceful degradation, not loss --
# provided the ones not reached are not the same ones every time, which is
# _due_stats_entries' job. The asyncio.timeout around the whole pass is the
# guarantee underneath it, for the one case the between-entries check cannot
# catch -- a single statement that hangs past the deadline -- where
# cancelling a query mid-flight and risking the connection is plainly better
# than holding the election open indefinitely.
#
# THE CEILING IS STRICTLY ABOVE THE BUDGET, and that gap is the only thing
# that lets the soft check ever take effect. Set equal, the deadline admits
# an entry at 59.9s and the timeout cancels it 0.1s later, so every slow
# pass ends by cancellation and the graceful path is dead code. Measured
# with the two equal: a two-entry pass at 0.5s per entry under a 0.6s
# budget was hard-cancelled rather than stopping cleanly. The 30s of gap is
# one statement_timeout, which is what an entry admitted just under the
# deadline can plausibly need to finish -- and it is why the ledger's wait
# term is the ceiling and not the budget.
#
# 60s sits INSIDE the measured range for a full cold sweep rather than above
# it: ~1.8x the cheap end and 0.9x the dear end, so a legitimate cold sweep
# is not guaranteed to fit and at the dear end provably does not. That is
# the open question the ledger above records, not a property this number
# was chosen for -- 60s predates the unscoped entry ever being measured. It
# is left where it is because truncation rotates rather than starves and
# costs no entry any expired time in simulation, so moving it would trade a
# permanent duty-cycle increase for a bound nothing has yet shown to bind.
# A pass degraded enough that even one entry overruns the 30s of ceiling is
# the pathological case the ceiling exists for, and reaching it means the
# database is in no state to serve the endpoint anyway.
_STATS_REFRESH_PASS_BUDGET_SECONDS = 60
_STATS_REFRESH_PASS_CEILING_SECONDS = 90

# How long one rotation step of the demoted tier lasts -- what gives that
# tier a front that moves. _due_stats_entries carries the reasoning; this is
# the sizing.
#
# Sized to the SHORTEST cadence two consecutive passes can have. A pass that
# finds nothing due costs one indexed lookup and is followed by one interval,
# so 20s is the closest together two passes ever land, and a step no longer
# than that is what guarantees the pivot has moved at least once between any
# two of them. Longer periods let consecutive passes draw the same pivot and
# waste one: at 240s, a full twelve-key cycle per four minutes, the worst
# healthy entry sat 1,669s to 1,929s expired against 577s to 1,024s at 20s
# -- four seeds, per-entry cost varying 15%, worst of a 55-pair sweep at both
# measured cost ends each time. Anything from 20s to 60s was within the
# seed-to-seed spread of that measurement; 240s was outside it.
#
# Shorter is not automatically safer, which is the part worth keeping. At 5s
# with per-entry costs held perfectly constant the step aligns with the pass
# cadence and leaves a healthy entry 9,235s expired somewhere in the same
# sweep, against 1,244s at 20s. Real costs vary and 15% of variation removes
# it entirely (1,073s), so this is a property of the measurement rather than
# a hazard in production -- but it is why the period is not simply minimised.
#
# It gets a constant of its own rather than reusing
# STATS_REFRESH_INTERVAL_SECONDS, which today holds the same number for the
# reason above: the two agree on a value, not on a purpose, and retuning how
# often a worker looks should not move the rotation with it.
_STATS_REFRESH_ROTATION_SECONDS = 20

# Postgres advisory lock id, elected once per pass that has work to do. The
# cache is a Postgres table shared by all six workers, so the storage was
# never per-process; only the WORK needs electing, and without an election
# six workers would multiply the very query load this exists to bound.
#
# Session-scoped (pg_try_advisory_lock), not transaction-scoped: a pass
# commits once per entry via cache.set, and a pg_try_advisory_xact_lock
# would be released by the first of those commits. It is therefore taken on
# a connection of its own, never on the session doing the counting.
#
# THAT PUTS A CONSTRAINT ON DEPLOYMENT, so it is written here rather than
# left to be discovered: a session-scoped advisory lock belongs to a
# backend, and a connection pooler in transaction-pooling mode hands a
# different backend to the next statement. Under PgBouncer in transaction
# mode the election would be taken on one backend and released on another,
# which both loses the election mid-pass and leaks the lock on the first.
# Libex connects to Postgres directly over asyncpg (app/db/session.py), so
# it is correct as deployed; a self-hoster who puts a transaction-mode
# pooler in front of it inherits this. Session or statement pooling is fine.
#
# LEAKING IT IS UNRECOVERABLE, which is why acquire and release are welded
# into one try/finally below and why _release_refresh_lock discards the
# connection rather than trusting it. A session-level lock survives the
# pool's reset-on-return, because that reset is a ROLLBACK and a ROLLBACK is
# exactly what does not release one -- the same property this module relies
# on to hold the lock across a pass. The backend stays alive in the pool
# still holding it, every worker is refused for the life of the process, and
# the only trace is a debug line. Advisory locks are also re-entrant, so if
# the pool hands that same physical connection out again a later single
# unlock decrements two to one and it is still stuck.
#
# Arbitrary but fixed, and the only advisory lock Libex takes anywhere.
_STATS_REFRESH_LOCK_ID = 4718201


def _stats_refresh_targets() -> dict[str, str | None]:
    """
    Every stats cache key that could exist, as {key: region}.

    Twelve: the unscoped global entry plus one per region. Enumerated from
    VALID_REGIONS through cache.stats_key rather than hardcoded, so a region
    added to the enum is picked up here with no second edit -- and sorted,
    because VALID_REGIONS is a set whose iteration order is not guaranteed
    to be the same in two processes, and an enumeration that differs between
    workers is one nobody can reason about afterwards.

    This is the candidate set, not a visiting order. Which of the twelve are
    actually refreshed, and in what order, is decided against the cache
    table; see _due_stats_entries.
    """
    targets: dict[str, str | None] = {cache.stats_key(None): None}
    for region in sorted(VALID_REGIONS):
        targets[cache.stats_key(region)] = region
    return targets


def _rotation_pivot(now: datetime, keys: list[str]) -> str:
    """
    The key the demoted tier is visited from, as of `now`.

    `keys` arrives ascending from _stats_refresh_targets, so walking it one
    step per _STATS_REFRESH_ROTATION_SECONDS and wrapping is the rotation:
    each step names a different key, and the tier is ordered from there.

    The clock is the input because nothing else advances. Every row in the
    demoted tier is one nothing has written for a full cache lifetime, so
    every column of every one of them is frozen, and a module that stores no
    state of its own has no other monotonic value to read. It does mean two
    workers a few seconds apart can pick different pivots for the same tier;
    that is harmless, because either pivot is a legitimate visiting order and
    only one of them wins the election anyway.
    """
    step = int(now.timestamp() // _STATS_REFRESH_ROTATION_SECONDS)
    return keys[step % len(keys)]


async def _due_stats_entries(session: AsyncSession) -> dict[str, str | None]:
    """
    The stored stats entries close enough to expiry to be worth recomputing,
    as {key: region}, in the order a pass should visit them: nearest to
    lapsing first, with the entries nothing has written for a whole cache
    lifetime last and rotating among themselves.

    Two filters and an order, doing three different jobs.

    Presence in the table makes the pass demand-driven. The README alone
    drives 37 distinct origin URLs, one per badge, but only nine cache
    entries between them: five global badges reading the unscoped entry,
    plus eight regions whose four badges each read one region-scoped entry.
    Nine of the twelve possible keys are therefore the ones that matter --
    it is the entry count that decides this and not the URL count, which is
    four times larger and moves whenever the README's layout does -- and
    refreshing all twelve regardless would spend a full narrator count
    (unscoped even on a region-scoped call, since narrators has no region
    column) on regions nobody asks about. An entry exists precisely because
    somebody queried it.

    That signal only ever GAINS keys, and it stops being a demand signal at
    all the moment this loop first renews one: a renewed entry never
    expires, so purge_expired can never collect it, so a region queried
    once and then dropped from the badge table is kept warm forever. The
    module docstring carries the measurement that proves it, the ruling on
    why it is accepted rather than fixed, and what an operator does about
    it. Do not describe presence here as self-correcting -- it corrects in
    the gaining direction only.

    Remaining life is what pins the cost. An entry with more than
    _STATS_REFRESH_AHEAD_SECONDS left is not touched, so a surplus pass --
    and with six workers ticking independently most passes are surplus --
    costs this one query and nothing else. The comparison is `<=` against a
    cutoff in the FUTURE, so it deliberately also catches entries that have
    already expired: one that lapsed while this loop was down is exactly the
    one that most needs recomputing, and an expired row survives in the
    table until the hourly purge collects it.

    Remaining life ORDERS them too, and that is what decides who a truncated
    pass drops. _refresh_due_stats stops at its budget, so whatever sits at
    the end of the order is deferred; under any fixed order that is the same
    tail every pass, and because the head comes due again 150s after its own
    refresh it re-enters ahead of that tail indefinitely. Measured against
    Postgres 16 with the alphabetical order this used to return and a
    per-entry cost above the budget's reach: the first entry refreshed seven
    times while uk and us refreshed zero, expired, and stayed expired. Least
    life first inverts it -- the entry nearest to lapsing is always served,
    and the ones deferred are by construction the ones with the most left,
    which puts them at the front of the next pass. Same total work per pass,
    spread evenly. The key tiebreak stops two entries written in the same
    instant from trading places between passes.

    THE STALENESS TIER AHEAD OF IT exists because expiry order alone gets
    one case exactly backwards. A refresh that FAILS writes nothing -- that
    is deliberate, so the entry keeps its older real value instead of a
    zeroed one (see _refresh_due_stats) -- so a failing entry keeps its old
    expires_at while every entry that succeeded moves a full TTL forward.
    Its sort position does not merely persist, it improves monotonically
    against the entire healthy set: the ordering key rewards failure. Two
    entries failing at the 30s statement_timeout are 60s, the whole pass
    budget, spent before any healthy entry is admitted -- and they lead
    again next pass, and every pass after. That is the same starvation the
    ORDER BY was added to prevent, arriving through the mechanism that
    prevents it.

    So an entry more than one whole STATS_CACHE_TTL_SECONDS past its own
    expiry sorts behind every entry that is not, and needs no stored state
    to detect. What that threshold does NOT do is identify which entry is
    failing, and the first version of this tier was built on the belief that
    it did. expires_at cannot tell a failing entry from one a truncated pass
    never reached: neither was written, so both keep their old expires_at,
    both cross the threshold, and both are demoted. Demoting the healthy one
    is not the damage on its own -- being stuck there is. Nothing in the
    demoted tier is ever written, so an order taken from expires_at over
    rows nobody writes never changes again, and the entry behind the failing
    one stays behind it for the life of the process.

    WHICH IS WHY expires_at DOES NOT ORDER THE DEMOTED TIER. Inside it every
    entry is a full cache lifetime past expiry and none is nearer to lapsing
    than another, so the column has stopped measuring urgency and measures
    only how long a failure has lasted -- and sorting on it is exactly what
    freezes the order. The tier is visited as a rotation of key order
    instead, starting from whichever key _rotation_pivot names, so the entry
    at the front of it changes from pass to pass and no entry in the tier
    sits behind another one permanently. That claim stops at the tier
    boundary and is worth reading no wider: the rotation decides the order
    WITHIN the tier and nothing about whether a pass reaches the tier at
    all, which is a separate residue stated below. Healthy entries are
    untouched by this: they sort ahead of the whole tier and among
    themselves by expiry, as before. A demoted entry that succeeds is
    written, leaves the tier on that write, and is back in expiry order at
    once.

    MEASURED over all 55 failing-region pairs rather than one, at this
    module's own costs -- 15.3s unscoped, 1.6s and 4.7s for the cheap and
    dear ends of a region, a failure costing the 30s statement_timeout --
    with twelve entries each starting from _STATS_REFRESH_AHEAD_SECONDS of
    life, two of them failing, over 200 passes. 110 runs, both cost ends:

      expiry order, no tier   worst healthy entry refreshed 0 times; 110 of
                              110 runs ended with a healthy entry expired
      tier, ordered by expiry worst healthy entry refreshed 0 times; 90/110
      tier, rotated           worst healthy entry refreshed 93 times; 0/110

    WHICH PAIR FAILS IS THE WHOLE REASON THAT IS QUOTED OVER ALL 55.
    db_stats:us sorts last of the twelve keys, so a failure there consumes
    budget only after every other entry has been swept, and the ten pairs
    containing us are the only ten an expiry-ordered tier survives. On uk+us
    it looks perfect -- every healthy entry refreshed 98 times, worst 210s
    expired. On au+br, which sorts first, nine of the ten healthy entries
    were never refreshed again and were still expired when the run ended.
    Any future measurement of this order is worth nothing unless it names a
    failing pair that does not contain us.

    THE RESIDUE THE ROTATION DOES NOT REMOVE, stated rather than designed
    away. A failing entry only REACHES the demotion boundary 450s after it
    first comes due -- 150s of remaining life, then 300s past expiry -- and
    for that whole window it is an ordinary undemoted entry holding the
    smallest expires_at, so it leads every pass and can spend the budget
    before any healthy entry is admitted. Every healthy entry, the unscoped
    db_stats the five top-of-README badges read among them, therefore spends
    one contiguous window fully expired at the start of an outage: 210s on
    uk+us, 672s at the cheap end and 939s at the dear end on au+br. Every
    badge fetch landing in one of those windows finds its entry cold, so it
    is minutes of readers each waiting out a full recompute -- 15.3s of it
    for the unscoped entry. That is the breakage this module exists to
    remove, happening once per outage instead of continuously. It
    is a transient and not a cycle, which is the part worth trusting: across
    those same 110 runs, not one healthy entry lapsed again at any point
    after the first 1,500s, the failing pair still failing throughout.

    THOSE THREE FIGURES ARE ALL AT TWO FAILING ENTRIES, which is the whole
    of what the 110 runs cover, and they are not the ceiling. The same
    transient grows with the number of failures: 1,527s at three, 1,702s at
    four, 2,486s at six. What does not grow with it is when a lapse can
    BEGIN. Across every one of those configurations the latest point at
    which any healthy entry started a lapse was 419s to 438s -- an onset
    bound, and not a time by which anything has cleared, which are different
    quantities and easily read as one: the longest single lapse still runs
    the full 2,486s. The onset bound is what makes this a transient rather
    than a cycle, independently of how long any individual lapse runs, and
    it carries the 1,500s claim above with a 3.4x margin. Read 210s to 939s
    as the two-failure case they were measured in.

    A HEALTHY ENTRY CAN STILL BE STRANDED BEHIND THE TIER, which the
    rotation cannot reach because the tier sorts last unconditionally. If
    the undemoted entries alone fill the 60s budget, no pass ever gets as
    far as the tier: an entry demoted while healthy -- a truncated pass
    never reached it, so it went unwritten and crossed the threshold -- is
    then never written, never leaves, and refreshes zero times for as long
    as that lasts. Measured with two entries failing at the
    statement_timeout, a constant per-entry cost, over 400 passes: at 21s
    and below nothing is stranded, the worst healthy entry still refreshing
    129 to 196 times. At 22s, db_stats:it refreshed zero times while the
    other nine refreshed 146 to 149; at 23s the stranded key is db_stats:us
    instead; at 24s it is db_stats:it and db_stats:jp together. Steady state
    at 22s is three entries demoted -- the two failing plus the stranded
    healthy one -- with every pass spending its whole budget before the tier
    is reached.

    It is recorded rather than fixed, for two reasons. The rotation is
    better than either alternative in exactly this regime and not merely
    elsewhere: at those same costs an expiry-ordered tier strands NINE
    healthy keys at zero refreshes rather than one. And the regime is
    narrow -- it needs a database degraded to 22s or more per region while
    still finishing under the 30s statement_timeout, AND two entries already
    failing. With nothing failing, no cost from 5s to 24s stranded anything;
    and 15% variation in per-entry cost removes it at 22s and 23s. The lever
    for anyone who does revisit it is the tier sorting last unconditionally,
    not the rotation inside it.

    When EVERY entry is that stale -- a process down longer than a cache
    lifetime -- the tier is uniform and the rotation is the only thing
    ordering it, so recovery is no longer in expiry order. That was the
    reason to check it and it costs nothing measurable: with nothing
    failing, outages from 400s to 7,200s brought all twelve back inside a
    single sweep at the cheap end (32.9s) and inside one sweep plus one
    deferred entry at the dear end (87.0s), identical to what expiry order
    manages, because every entry is equally past a full lifetime and the
    whole set is swept either way.

    A BAND BETWEEN THOSE TWO STATES MIS-TIERS WITH NOTHING FAILING AT ALL.
    An outage of roughly 450s to 600s leaves some entries past the threshold
    and some short of it, so an entry expired 410s is visited behind one
    expired 260s. Both orders do it -- 32 to 36 inverted pairs out of 132
    for expiry order, 33 to 46 for the rotation -- and neither delays
    recovery by a second, the whole set still being back within one sweep in
    every case measured. Recorded rather than fixed: the ORDER BY that would
    prevent the inversion is the one that starves.

    Three alternatives were refused. A memo of which keys failed needs
    shared state, because six workers contend and the election winner
    changes between passes, which means writing it to the database -- and
    this loop writes nothing of its own by design (see the module docstring
    on eviction). Bumping a failed entry's expires_at to push it down the
    order was refused for a harder reason: it would extend the advertised
    life of a value that nothing recomputed, which misstates freshness to
    every reader of that entry. A per-entry allowance, cutting an entry off
    before it reaches the 30s statement_timeout, has to sit above the
    unscoped entry's 15.3s or it cuts off the one those badges read -- and
    15.3s is an end-to-end wall-clock upper bound rather than database time,
    so an allowance set just above it would cut that entry off at the same
    point on every pass and turn slow into never.

    Bounded at twelve binds by construction, so the IN list needs no
    chunking. It is an index scan on cache_pkey with expires_at filtered off
    the heap rather than an index-only scan, since expires_at is not in the
    primary key: measured at 0.196ms against 500k cache rows, against
    0.170ms for the same lookup without the expires_at predicate, 40 buffers
    and twelve heap fetches either way. That difference is noise, which is
    why expires_at gets no index of its own here -- the plan already has to
    visit the same twelve rows. Both figures predate the ORDER BY, which
    sorts those same twelve.
    """
    now = datetime.now(timezone.utc)
    targets = _stats_refresh_targets()
    cutoff = now + timedelta(seconds=_STATS_REFRESH_AHEAD_SECONDS)
    unwritten_since = now - timedelta(seconds=STATS_CACHE_TTL_SECONDS)
    pivot = _rotation_pivot(now, list(targets))
    demoted = Cache.expires_at < unwritten_since
    result = await session.execute(
        select(Cache.key)
        .where(
            Cache.key.in_(list(targets)),
            Cache.expires_at <= cutoff,
        )
        .order_by(
            # Demoted last. False sorts before True, so undemoted entries
            # lead whatever else the following terms say.
            demoted.asc(),
            # The rotation, and it is ANDed with the tier rather than
            # standing alone so that it is constantly False across every
            # undemoted row and cannot reorder them. Within the tier it
            # puts the keys at or after the pivot first and the ones before
            # it after, which is the key order rotated to start at the pivot.
            and_(demoted, Cache.key < pivot).asc(),
            # Expiry, for the undemoted entries only. NULL across the whole
            # demoted tier leaves that tier to the term below; it is not a
            # NULLS-ordering question, because every row it applies to
            # carries the same NULL.
            case((demoted, None), else_=Cache.expires_at).asc(),
            # The rotation order inside the tier, and the tiebreak outside
            # it that stops two entries written in the same instant from
            # trading places between passes.
            Cache.key.asc(),
        )
    )
    return {row[0]: targets[row[0]] for row in result}


async def _refresh_due_stats(session: AsyncSession, deadline: float) -> None:
    """
    Recomputes every due stats entry once, in place, stopping at `deadline`.

    Each entry is refreshed through get_db_stats(refresh=True) rather than
    by any query written here, so the counts, the region scoping and the
    shrinkage-free write are the one implementation the request path already
    uses -- there is no second version of the stats query to drift from it.

    The due set is re-read here rather than passed in from the pre-election
    check, because between the two another worker may have won the election
    and refreshed some of it; re-reading costs one indexed lookup and avoids
    recomputing what is already warm.

    A failed entry is reported by get_db_stats as a null cache_expires_at:
    the live query fell over, it returned the all-zeros fallback WITHOUT
    writing anything, and the previously stored value is left in the table
    untouched. That is why this loop neither invalidates first nor treats a
    failure as fatal -- it moves on, and the entry that failed keeps its
    older, real value until a later pass succeeds.

    Left in the table is not the same as still serving, and the difference
    matters once the entry is past its expiry. cache.get_entry filters on
    expires_at > now, so from that moment the request path treats the row as
    a miss and recomputes rather than handing the stored value back; what
    actually spares a reader during a failure is the edge's stale-if-error
    window (app/api/routes/db/stats_headers.py), not this row. Keeping the
    row is still right -- it is a real value that one successful pass turns
    back into a served one, where a zeroed row would have to be corrected --
    but it buys the reader nothing on its own. get_db_stats
    rolls this session back on every one of its own failure paths -- cache
    read, live query, and cache write -- so the session stays usable for the
    entries after it. That last one was only true from this change onward:
    on the request path a session is closed straight after, so an aborted
    transaction left by a failed cache write cost nothing and went unnoticed
    for as long as nothing reused the session.

    Stopping at the deadline defers rather than loses, and it does so only
    because of the order the due set arrives in. An entry not reached was
    never written, so it is still due; among the entries something is
    writing, it is also the one with the most life left, so expiry order
    puts it nearer the front of the next pass. Iterate a fixed order instead
    and the deferral is not self-correcting at all: the tail is dropped by
    every truncated pass while the head comes due again and preempts it, for
    as long as the pressure lasts.

    Expiry order cannot do that job for the entries nothing is writing,
    because their position in it never moves, and those are exactly the ones
    a truncated pass keeps dropping. What rotates their deferral instead is
    the demoted tier's rotation. Both halves of the order are load-bearing
    here and for the same reason; see _due_stats_entries.
    """
    started = datetime.now(timezone.utc)
    due = await _due_stats_entries(session)

    refreshed = 0
    failed = 0
    skipped = 0
    for key, region in due.items():
        if asyncio.get_running_loop().time() >= deadline:
            skipped = len(due) - refreshed - failed
            logger.warning(
                "Stats refresh pass hit its budget; the rest stay due for the next pass",
                extra={"refreshed": refreshed, "failed": failed, "skipped": skipped},
            )
            break
        result = await get_db_stats(session, region, refresh=True)
        if result.cache_expires_at is None:
            failed += 1
            logger.warning(
                "Stats refresh failed for an entry; the stored value is kept, and serves only while unexpired",
                extra={"cacheKey": key, "region": region},
            )
        else:
            refreshed += 1

    elapsed_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
    logger.info(
        "Stats refresh pass complete",
        extra={
            "refreshed": refreshed,
            "failed": failed,
            "skipped": skipped,
            "due": len(due),
            "durationMs": elapsed_ms,
        },
    )


async def _release_refresh_lock(lock_conn: AsyncConnection) -> None:
    """
    Releases the election lock, discarding the connection if it cannot.

    Never raises. It runs in a finally, and an exception raised there
    replaces whatever was already propagating -- which would cost the log
    line naming why the pass actually failed, in exchange for a second one
    about the cleanup.

    invalidate() on failure is not tidiness. An unlock that did not happen
    leaves a live pooled backend holding a lock nothing will ever release
    (see _STATS_REFRESH_LOCK_ID); discarding the physical connection ends
    that backend, and Postgres releases every advisory lock it held. Trading
    one connection for the refresher's continued existence in this process
    is not a close call.

    A false return is the other half of the same signal: the lock was not
    held at release time, which means something already took this connection
    apart underneath the pass. Worth a line, nothing more -- the lock is
    gone either way, which is the outcome wanted.

    Both failure lines carry _failure_fields rather than the exception, for
    the reason that function's docstring gives and for one specific to this
    module: the exceptions reachable here include ones whose str() is empty
    -- a bare TimeoutError from a partitioned host is the plain case -- so
    interpolating them produced a line that named no cause at all, on the
    single failure this module cannot recover from.
    """
    try:
        released = await lock_conn.scalar(
            text("SELECT pg_advisory_unlock(:lock_id)"),
            {"lock_id": _STATS_REFRESH_LOCK_ID},
        )
    except Exception as e:
        try:
            await lock_conn.invalidate()
        except Exception as invalidate_error:
            logger.warning(
                "Stats refresh lock connection could not be discarded",
                extra=_failure_fields(invalidate_error),
            )
        logger.warning(
            "Stats refresh lock could not be released, connection discarded",
            extra=_failure_fields(e),
        )
        return

    if not released:
        logger.warning("Stats refresh lock was already gone at release time")


async def run_stats_refresh_pass() -> None:
    """
    Runs one refresh pass in this worker, if anything is due and if it wins
    the election.

    The due check comes BEFORE the election on purpose. Most passes have
    nothing to do, and in that shape they cost one indexed lookup and never
    touch the advisory lock at all -- so the lock is contended only by
    workers that actually intend to work. Losing the election is the
    ordinary case for the rest and is not a failure of any kind, hence
    debug.

    Acquire and release are welded into one try/finally with no await
    between them. An earlier version put the transaction-ending rollback in
    that gap, where anything raised -- including CancelledError, which is a
    BaseException and slips past `except Exception` entirely -- exited with
    the lock held and permanently dead. `elected` starts as None so the
    finally can tell "never asked" from "asked and was refused" from "asked
    and it is unknown whether the server took it", and releases in every
    case except the one where the answer was a definite no.

    The hard ceiling is attributed HERE, at the asyncio.timeout boundary,
    and not around the loop that calls this. TimeoutError is reachable from
    several places a pass touches and only one of them is the ceiling: a
    blackholed host raises a real builtins.TimeoutError out of
    engine.connect(), measured at 30.02s, and SQLAlchemy's asyncpg dialect
    does not translate it. Caught at the loop, that connect failure was
    reported as the ceiling firing and stamped with a ceiling figure the
    pass never came near -- a wrong cause is worse than the blank one it
    replaced, because a number invites the reader to reason from it.
    ceiling.expired() is what makes the attribution exact rather than
    probable: asyncio.timeout sets it only when the deadline it owns is what
    fired, so a TimeoutError raised from inside is re-raised untouched and
    reported by the loop as what it is.
    """
    async with AsyncSessionFactory() as session:
        due = await _due_stats_entries(session)
    if not due:
        logger.debug("Stats refresh skipped; no entry is due")
        return

    async with engine.connect() as lock_conn:
        elected = None
        try:
            elected = await lock_conn.scalar(
                text("SELECT pg_try_advisory_lock(:lock_id)"),
                {"lock_id": _STATS_REFRESH_LOCK_ID},
            )
            if not elected:
                logger.debug("Stats refresh skipped; another worker holds the lock")
                return
            # Ends the transaction the SELECT above opened, WITHOUT releasing
            # the lock -- a session-level advisory lock is held by the
            # connection, not the transaction, so only pg_advisory_unlock or
            # the connection dying lets it go. Left open, this connection
            # would sit idle-in-transaction for the length of the pass
            # against the 60s idle_in_transaction_session_timeout every
            # connection carries (app/db/session.py), and a pass that ever
            # ran long enough to trip it would have the election dissolved
            # underneath it at precisely the moment the database is slow
            # enough for a second worker's duplicate pass to hurt.
            await lock_conn.rollback()
            deadline = asyncio.get_running_loop().time() + _STATS_REFRESH_PASS_BUDGET_SECONDS
            try:
                async with asyncio.timeout(_STATS_REFRESH_PASS_CEILING_SECONDS) as ceiling:
                    async with AsyncSessionFactory() as session:
                        await _refresh_due_stats(session, deadline)
            except TimeoutError:
                if not ceiling.expired():
                    raise
                logger.warning(
                    "Stats refresh pass was cut off by its hard ceiling",
                    extra={"passCeilingSeconds": _STATS_REFRESH_PASS_CEILING_SECONDS},
                )
        finally:
            if elected is not False:
                await _release_refresh_lock(lock_conn)


async def stats_refresh_loop() -> None:
    """
    Looks for due /db/stats entries every STATS_REFRESH_INTERVAL_SECONDS,
    for as long as the process runs.

    The pass comes BEFORE the sleep, unlike the cache purge loop, and that
    ordering is the point rather than an accident: a restart is exactly when
    an entry can be moments from expiring -- the cache lives in Postgres and
    survives a deploy, so a worker starting up inherits entries of any age
    -- and sleeping first would leave that gap open for a full interval.

    Never raises. A pass that fails is logged and the loop waits for the
    next one; letting the exception out would kill the task and leave the
    endpoint quietly back on its lazy path with nothing to say so.

    One handler, not two. A pass stopped by the hard ceiling names itself
    inside run_stats_refresh_pass, where the ceiling is the only thing that
    can have fired; a branch here could not tell that apart from a
    TimeoutError raised by anything else the pass touches, and mislabelled
    one when it tried. What is left for this handler is every other
    failure, and it reports through _failure_fields rather than by
    interpolating the exception -- which is what makes a bare TimeoutError
    legible here at all, since str() on one is empty and produced a line
    naming no cause whatsoever.
    """
    logger.info(
        "Stats refresher started",
        extra={
            "intervalSeconds": STATS_REFRESH_INTERVAL_SECONDS,
            "refreshAheadSeconds": _STATS_REFRESH_AHEAD_SECONDS,
            "passBudgetSeconds": _STATS_REFRESH_PASS_BUDGET_SECONDS,
            "passCeilingSeconds": _STATS_REFRESH_PASS_CEILING_SECONDS,
            "ttlSeconds": STATS_CACHE_TTL_SECONDS,
        },
    )
    while True:
        try:
            await run_stats_refresh_pass()
        except Exception as e:
            logger.warning("Stats refresh pass failed", extra=_failure_fields(e))
        await asyncio.sleep(STATS_REFRESH_INTERVAL_SECONDS)
