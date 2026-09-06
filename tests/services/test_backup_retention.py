"""
Retention keep-set tests.

This is the one function in the backup package where being wrong destroys
data rather than costing a cycle, so the assertions here are by NAME and
never by count. "Six were kept" is true of the keep set that threw away the
aged artefact.

The rule is three tiers -- the most recent N, PLUS the newest artefact at
least M days old, PLUS the oldest artefact not yet M days old -- and the
second tier is the fragile one. It is by construction the oldest thing on
the server, which makes it the first casualty of any "delete the oldest"
shortcut, and six daily copies of a database that was already corrupt a week
ago are six copies of the damage.

The third tier is what keeps the second one supplied, and its absence is
invisible in any single-listing test. Handed one seeded listing that already
contains a 45-day artefact, a two-tier rule looks perfect: it keeps that
artefact and deletes the 200-day one, which is the right answer to the
question it was asked. What it cannot be asked that way is whether the
listing it was handed is one the policy could ever have produced. With the
shipped defaults the recent tier deletes everything past six days old, so
nothing survives to become thirty days old, so the aged tier never gets a
first artefact -- and against an archive that already holds one it is worse,
because that single file is pinned forever while every possible replacement
dies between day six and day thirty. Only iterating the function over
successive cycles asks that question, which is why the steady-state tests at
the bottom of this file exist and why they are not a duplicate of the ones
above.

Three properties run through everything below:

  - The aged survivor is kept. The seeded listing reproduces the measured
    case: six daily artefacts, one 45 days old, one 200 days old. The 45-day
    one survives and the 200-day one goes.
  - The successor is reserved before it qualifies, so the incumbent has a
    replacement the moment it is released, and the oldest artefact held
    stays bounded instead of climbing forever.
  - Anything uncertain raises, and the caller's answer to that is to delete
    nothing. Every hazard case here asserts the raise; that a raise really
    does mean no deletions is asserted against the runner in
    test_backup_runner.py, because it is the runner that holds the answer.

No clock and no I/O anywhere in this file. That is the point of the module
being pure: every hazard below -- skew, a duplicate name, a misconfigured
tier -- is reachable with a datetime and a list, and so is four hundred days
of operation.
"""

# Standard library
from datetime import datetime, timedelta, timezone

# Third party
import pytest

# Local
from app.services.backup.artifact import RemoteArtifact, build_name, remote_artifact
from app.services.backup.retention import RetentionUnsafe, keep_set


NOW = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)

RECENT = 6
AGED_DAYS = 30


def _at(days_ago, hours=0):
    """An artefact named for `days_ago` days before NOW."""
    return remote_artifact(build_name(NOW - timedelta(days=days_ago, hours=hours)))


# The measured listing: six daily artefacts, plus one 45 days old and one
# 200 days old. Both older ones qualify for the aged tier; only the newer of
# them is the aged survivor.
DAILY = [_at(days) for days in (1, 2, 3, 4, 5, 6)]
AGED_45 = _at(45)
AGED_200 = _at(200)
SEEDED = [*DAILY, AGED_45, AGED_200]


def _names(artifacts):
    return {artifact.name for artifact in artifacts}


# ============================================================
# THE THREE-TIER RULE
# ============================================================

def test_keeps_the_recent_tier_plus_the_aged_survivor():
    """
    The measured case, asserted by name. The 45-day artefact is not one of
    the six most recent and is kept anyway; the 200-day one is the only
    thing deleted.
    """
    keep, delete = keep_set(SEEDED, NOW, recent=RECENT, aged_days=AGED_DAYS)

    assert _names(keep) == _names(DAILY) | {AGED_45.name}
    assert _names(delete) == {AGED_200.name}


def test_the_aged_tier_keeps_the_newest_artefact_old_enough_not_the_oldest():
    """
    "At least 30 days old" is satisfied by both the 45-day and the 200-day
    artefact. The newest of those is the one worth holding: it is the oldest
    copy that is still recent enough to be useful, and keeping the 200-day
    one instead would be a year of drift away from the database anyone would
    want to restore.
    """
    keep, delete = keep_set(SEEDED, NOW, recent=RECENT, aged_days=AGED_DAYS)

    aged_kept = _names(keep) - _names(DAILY)
    assert aged_kept == {AGED_45.name}
    assert AGED_200.name not in _names(keep)


def test_the_aged_survivor_is_kept_even_when_the_recent_tier_is_full_of_newer_copies():
    """
    The failure this whole module exists to prevent, in its simplest form:
    a full recent tier and one old artefact. A keep-count of six would
    delete it.
    """
    listing = [*DAILY, AGED_200]

    keep, delete = keep_set(listing, NOW, recent=RECENT, aged_days=AGED_DAYS)

    assert AGED_200.name in _names(keep)
    assert delete == []


def test_the_recent_tier_is_the_most_recent_by_name_not_the_first_n_listed():
    listing = [_at(days) for days in (1, 2, 3, 4, 5, 6, 7, 8)]

    keep, delete = keep_set(listing, NOW, recent=3, aged_days=0)

    assert _names(keep) == {_at(1).name, _at(2).name, _at(3).name}
    assert _names(delete) == {_at(days).name for days in (4, 5, 6, 7, 8)}


def test_an_artefact_exactly_aged_days_old_qualifies_for_the_aged_tier():
    """
    The boundary is inclusive. An artefact one second short of it does not
    qualify, which is why the two cases are asserted together -- and each is
    put in a listing that already holds a 45-day artefact, because that is
    the only arrangement in which the two tiers are told apart. Without it
    both readings of the boundary keep the same set: whichever artefact
    fails to be the incumbent is immediately reserved as the successor
    instead, so a keep set alone cannot say which tier took it.

    With AGED_45 present the tiers separate. The incumbent is the newest
    artefact old enough, so an inclusive boundary makes it `exactly` and
    releases AGED_45; an exclusive one would push `exactly` down into the
    successor slot and leave AGED_45 the incumbent, kept. The two calls
    below therefore have opposite delete sets, which is the assertion.
    """
    exactly = _at(AGED_DAYS)

    keep, delete = keep_set([*DAILY, exactly, AGED_45], NOW, recent=RECENT, aged_days=AGED_DAYS)
    assert exactly.name in _names(keep)
    assert _names(delete) == {AGED_45.name}

    just_short = remote_artifact(build_name(NOW - timedelta(days=AGED_DAYS) + timedelta(seconds=1)))
    keep, delete = keep_set([*DAILY, just_short, AGED_45], NOW, recent=RECENT, aged_days=AGED_DAYS)
    assert _names(keep) == _names(DAILY) | {just_short.name, AGED_45.name}
    assert delete == []


def test_a_young_archive_keeps_everything_and_is_not_an_error():
    """Nothing is old enough yet. The aged tier keeps nothing, which can
    only ever subtract from what it would have added -- never from the
    recent tier."""
    listing = [_at(days) for days in (1, 2, 3)]

    keep, delete = keep_set(listing, NOW, recent=RECENT, aged_days=AGED_DAYS)

    assert _names(keep) == _names(listing)
    assert delete == []


def test_aged_days_zero_disables_the_aged_tier():
    """
    A supported choice, and distinct from a negative value, which is not.

    Both aged tiers go together. The keep set is asserted as exactly the
    recent six, which is what says the successor was disabled alongside the
    incumbent -- a successor still being reserved would leave a seventh
    name here and quietly cost a slot to a tier the operator switched off.
    """
    keep, delete = keep_set(SEEDED, NOW, recent=RECENT, aged_days=0)

    assert _names(keep) == _names(DAILY)
    assert _names(delete) == {AGED_45.name, AGED_200.name}


def test_recent_one_still_keeps_the_aged_survivor():
    """
    Neither aged tier is a slice off the end of the recent one: shrinking
    the recent tier to a single artefact does not take the aged survivor
    with it. AGED_45 is still the incumbent, and the six-day artefact --
    the oldest thing that is not yet thirty days old -- is still the
    reserved successor, which is what stops the recent tier from deleting
    every candidate before one can ever qualify.
    """
    keep, delete = keep_set(SEEDED, NOW, recent=1, aged_days=AGED_DAYS)

    assert _names(keep) == {_at(1).name, _at(6).name, AGED_45.name}
    assert _names(delete) == {_at(days).name for days in (2, 3, 4, 5)} | {AGED_200.name}


def test_an_empty_listing_deletes_nothing():
    assert keep_set([], NOW, recent=RECENT, aged_days=AGED_DAYS) == ([], [])


def test_keep_and_delete_account_for_the_whole_listing():
    keep, delete = keep_set(SEEDED, NOW, recent=RECENT, aged_days=AGED_DAYS)

    assert _names(keep) | _names(delete) == _names(SEEDED)
    assert _names(keep) & _names(delete) == set()
    assert len(keep) + len(delete) == len(SEEDED)


def test_keep_is_returned_newest_first():
    keep, _ = keep_set(SEEDED, NOW, recent=RECENT, aged_days=AGED_DAYS)

    assert [artifact.name for artifact in keep] == [
        *[artifact.name for artifact in DAILY],
        AGED_45.name,
    ]


# ============================================================
# ORDERING IS BY PARSED NAME, NEVER BY ARRIVAL
# ============================================================

def test_a_listing_whose_arrival_order_contradicts_its_names_gives_the_same_answer():
    """
    A destination returns entries in whatever order its server felt like.
    Sorted by arrival, this listing would put the 200-day artefact in the
    recent tier and delete a day-old copy; sorted by name it cannot.
    """
    scrambled = [AGED_200, DAILY[3], AGED_45, DAILY[0], DAILY[5], DAILY[1], DAILY[4], DAILY[2]]

    keep, delete = keep_set(scrambled, NOW, recent=RECENT, aged_days=AGED_DAYS)

    assert _names(keep) == _names(DAILY) | {AGED_45.name}
    assert _names(delete) == {AGED_200.name}


def test_reversing_the_listing_cannot_change_the_keep_set():
    forward = keep_set(SEEDED, NOW, recent=RECENT, aged_days=AGED_DAYS)
    backward = keep_set(list(reversed(SEEDED)), NOW, recent=RECENT, aged_days=AGED_DAYS)

    assert _names(forward[0]) == _names(backward[0])
    assert _names(forward[1]) == _names(backward[1])


# ============================================================
# EVERY UNCERTAINTY RAISES
# ============================================================

def test_a_naive_now_raises():
    with pytest.raises(RetentionUnsafe):
        keep_set(SEEDED, datetime(2026, 9, 6, 12, 0), recent=RECENT, aged_days=AGED_DAYS)


def test_a_naive_timestamp_in_the_listing_raises():
    """artifact.parse cannot produce one, so this is the case where a
    transport built a descriptor some other way. The offset would be silent
    rather than an error, which is what makes it worth refusing."""
    naive = RemoteArtifact(name="libex-20260901T030000Z.dump", created_at=datetime(2026, 9, 1, 3, 0))

    with pytest.raises(RetentionUnsafe):
        keep_set([*DAILY, naive], NOW, recent=RECENT, aged_days=AGED_DAYS)


def test_an_artefact_from_beyond_the_future_tolerance_raises():
    """One of the two clocks is wrong, which makes the age of every other
    artefact suspect -- and age is the entire input to the aged tier."""
    ahead = remote_artifact(build_name(NOW + timedelta(minutes=6)))

    with pytest.raises(RetentionUnsafe):
        keep_set([*DAILY, ahead], NOW, recent=RECENT, aged_days=AGED_DAYS)


def test_ordinary_clock_drift_inside_the_tolerance_is_accepted():
    """
    Five minutes absorbs NTP drift between two machines. Refusing at one
    second of skew would make pruning stop working for a reason nobody could
    see.

    Accepted means treated as an ordinary artefact, not merely not-refused,
    so both halves of that are asserted. The skewed artefact sorts newest
    and takes the first recent-tier slot -- which is what the second call
    shows by turning the successor tier off and watching it push the
    six-day artefact out of the recent tier and into the delete set. At the
    shipped defaults that same artefact is the reserved successor, so
    nothing is deleted at all; a delete set of nothing is the right answer
    here and is also the answer a refusal would give, which is why the
    aged_days=0 call is the one that carries the evidence.
    """
    slightly_ahead = remote_artifact(build_name(NOW + timedelta(minutes=4)))

    keep, delete = keep_set([*DAILY, slightly_ahead], NOW, recent=RECENT, aged_days=AGED_DAYS)

    assert _names(keep) == _names(DAILY) | {slightly_ahead.name}
    assert delete == []
    assert keep[0].name == slightly_ahead.name

    _, delete = keep_set([*DAILY, slightly_ahead], NOW, recent=RECENT, aged_days=0)

    assert _names(delete) == {DAILY[-1].name}


def test_a_duplicate_name_raises():
    """
    Two entries under one name cannot exist in one remote directory, so the
    listing is not what it appears to be -- concatenated directories, or a
    transport returning full paths where it promised names. Deleting "the
    duplicate" would delete both.

    The message is asserted, not just the type. The post-conditions at the
    end of keep_set raise RetentionUnsafe for this listing too, by way of
    the count not adding up, so a test that only checked the type would
    pass with the duplicate guard deleted.
    """
    with pytest.raises(RetentionUnsafe, match="duplicate"):
        keep_set([*SEEDED, DAILY[0]], NOW, recent=RECENT, aged_days=AGED_DAYS)


def test_a_duplicate_of_an_artefact_that_would_be_deleted_raises_too():
    """
    The case no other guard catches. When the repeated name is one the keep
    set does not want, the totals still add up -- seven kept, two deleted,
    nine listed -- so nothing downstream notices, and the delete list
    carries the same name twice. Only the duplicate check itself stands
    between that listing and a prune run against a directory nobody can
    describe.
    """
    with pytest.raises(RetentionUnsafe, match="duplicate"):
        keep_set([*SEEDED, AGED_200], NOW, recent=RECENT, aged_days=AGED_DAYS)


def test_a_recent_tier_below_one_raises():
    """recent=0 with nothing old enough for the aged tier is a
    configuration that empties the archive."""
    with pytest.raises(RetentionUnsafe):
        keep_set(SEEDED, NOW, recent=0, aged_days=AGED_DAYS)


def test_a_negative_aged_days_raises():
    """Read as a timedelta it would make every artefact old enough,
    including one taken a minute ago."""
    with pytest.raises(RetentionUnsafe):
        keep_set(SEEDED, NOW, recent=RECENT, aged_days=-1)


def test_every_refusal_happens_before_a_delete_list_exists():
    """
    The shape of the safety, not just its presence: keep_set raises instead
    of returning a partial answer, so there is no delete list for a caller
    to act on by mistake. There is deliberately no incremental form of this
    function -- a walk that fails partway has already deleted part of the
    archive on the strength of a decision it never finished making.
    """
    hazards = [
        {"listing": [*SEEDED, DAILY[0]], "recent": RECENT, "aged_days": AGED_DAYS},
        {"listing": SEEDED, "recent": 0, "aged_days": AGED_DAYS},
        {"listing": SEEDED, "recent": RECENT, "aged_days": -1},
    ]
    for hazard in hazards:
        with pytest.raises(RetentionUnsafe):
            keep_set(hazard["listing"], NOW, recent=hazard["recent"], aged_days=hazard["aged_days"])


def test_retention_unsafe_is_not_on_the_api_exception_hierarchy():
    """
    LibexException subclasses carry a .message the middleware copies into an
    HTTP response body. Nothing in the backup package may be able to reach
    that path, and inheritance is the only way it could.
    """
    from app.core.exceptions import LibexException

    assert not issubclass(RetentionUnsafe, LibexException)


# ============================================================
# THE STEADY STATE, ACROSS CYCLES
# ============================================================
#
# Everything above hands keep_set one listing and checks the answer. That
# cannot find the failure this policy actually had, because the failure is
# not in any single answer -- it is in which listings the policy can reach
# by applying its own answers over and over. The missing successor tier was
# found by iterating the function over four hundred simulated daily cycles
# and looking at the ages of what was left, and it is only cheap to do that
# because the module is pure: no server, no filesystem, no clock.

_START = datetime(2026, 1, 1, 3, 0, tzinfo=timezone.utc)


def _simulate(cycles, *, recent=RECENT, aged_days=AGED_DAYS, backlog=()):
    """
    Runs `cycles` daily backups against the policy and returns the age in
    days of everything still held after each one, oldest last.

    One artefact is added per cycle and the keep set becomes the whole of
    what survives into the next -- which is the loop the runner actually
    performs, with the destination and the upload taken out. `backlog`
    seeds the archive with artefacts already that many days old on day
    zero, for the case where the policy is switched on over history that
    already exists.
    """
    held = [remote_artifact(build_name(_START - timedelta(days=days))) for days in backlog]
    trace = []
    for cycle in range(cycles):
        now = _START + timedelta(days=cycle)
        held.append(remote_artifact(build_name(now)))
        keep, _ = keep_set(held, now, recent=recent, aged_days=aged_days)
        held = list(keep)
        trace.append(sorted(round((now - artifact.created_at).total_seconds() / 86400) for artifact in held))
    return trace


def test_four_hundred_daily_cycles_settle_on_eight_artefacts():
    """
    The shipped defaults, run for over a year. Eight artefacts: six recent,
    the incumbent, and the successor in transit -- and the exact ages are
    asserted rather than the count, because a count of eight is also true
    of eight consecutive dailies.

    The two aged ages are a snapshot of a rotation, not constants. The
    incumbent climbs from six days to twenty-nine and is then replaced, so
    the pair [15, 39] is what cycle four hundred happens to land on; the
    property that does not move is asserted separately below.
    """
    trace = _simulate(400)

    assert trace[-1] == [0, 1, 2, 3, 4, 5, 15, 39]


def test_the_incumbent_rotates_instead_of_being_pinned_forever():
    """
    The whole point of the successor tier, and the thing no single listing
    can show. Without it the aged tier keeps whichever artefact was already
    old enough and nothing ever replaces it, so after four hundred cycles
    the archive holds a 389-day-old file and six copies of this week.

    Asserted as a bound rather than a sequence: the oldest artefact held
    never gets past 53 days, and the second-oldest is released and replaced
    repeatedly rather than being the same age plus one every cycle.
    """
    trace = _simulate(400)

    oldest = [held[-1] for held in trace]
    # Read only from the cycles where both aged tiers are actually occupied.
    # A young archive has fewer than eight artefacts and its second-oldest
    # entry is an ordinary daily, which would read as a rotation every time
    # the recent tier shifted.
    incumbent = [held[-2] for held in trace if len(held) == RECENT + 2]

    assert max(oldest) == 53
    # A pinned incumbent ages by exactly one day every cycle and never
    # drops. Counting the drops is what tells rotation from ageing.
    releases = sum(1 for before, after in zip(incumbent, incumbent[1:]) if after < before)
    assert releases > 10


def test_the_archive_never_grows_past_the_recent_tier_plus_two():
    """N + 2 is the whole cost of both aged tiers. A policy that quietly
    accumulates is as much a defect as one that quietly deletes -- the spool
    and the destination are both sized for what this promises."""
    trace = _simulate(400)

    assert max(len(held) for held in trace) == RECENT + 2


def test_a_backlog_of_existing_artefacts_is_reduced_rather_than_pinned():
    """
    Switching the policy on over history that already exists. The 40-day
    artefact is old enough on day one and becomes the incumbent, which is
    exactly the case where a two-tier rule pins one file forever: every
    replacement it could ever have dies between day six and day thirty.

    It reaches the same bounded rotation as an archive that started empty.
    """
    trace = _simulate(400, backlog=range(1, 41))

    assert trace[-1] == [0, 1, 2, 3, 4, 5, 20, 44]
    assert max(held[-1] for held in trace) == 53


def test_across_cycles_aged_days_zero_holds_exactly_the_recent_tier():
    """Both aged tiers off, sustained. Six artefacts, none of them older
    than five days, forever -- which is the archive the operator asked for
    and not one slot more."""
    trace = _simulate(120, aged_days=0)

    assert trace[-1] == [0, 1, 2, 3, 4, 5]
    assert {len(held) for held in trace} == {1, 2, 3, 4, 5, 6}


def test_a_recent_tier_that_reaches_past_the_cutoff_collapses_to_one_tier():
    """
    recent >= aged_days is a configuration where the aged tiers can only
    ever name artefacts the recent tier already holds: the newest artefact
    thirty days old and the oldest one not yet thirty days old are both
    inside the most recent thirty. Nothing extra is kept, and the archive
    is the recent tier exactly -- so the cost of the aged tiers here is
    zero rather than two, which is worth pinning because the arithmetic
    that produces it is not obvious from the rule as stated.
    """
    listing = [_at(days) for days in range(1, 61)]

    keep, delete = keep_set(listing, NOW, recent=30, aged_days=AGED_DAYS)

    assert _names(keep) == {_at(days).name for days in range(1, 31)}
    assert _names(delete) == {_at(days).name for days in range(31, 61)}
