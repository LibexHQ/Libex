"""
The keep-set policy. Pure: no network, no filesystem, no clock of its own.

The rule is three tiers, and it is not a keep-count:

    keep the most recent N artefacts, PLUS the newest artefact that is at
    least M days old, PLUS the oldest artefact that is not yet M days old.

The second tier is the one that matters and the one that is easy to lose.
Six daily backups are six copies of the same state if the damage landed a
week ago; the aged artefact is the only copy that predates a corruption
noticed late. It is also, by construction, always the oldest thing on the
server -- which is to say it is exactly what any ordinary "delete the
oldest" pruning deletes first.

THE THIRD TIER IS WHAT MAKES THE SECOND ONE POSSIBLE, and leaving it out is
a defect that looks exactly like correct operation. The aged tier can only
select from artefacts that survived the previous prune, and with the shipped
defaults -- six recent, thirty aged days -- the recent tier deletes
everything past about six days old. Nothing ever lives long enough to be
thirty days old, so the aged tier finds no candidate, ever, and the pruning
log reads `kept=6, deleted=1` every night while the copy the whole scheme
exists to preserve is never created. Against an archive that already
contains an old artefact it is worse than useless: the tier keeps that one
artefact and no other, so the same file is pinned forever and grows
arbitrarily old while every possible replacement dies between day six and
day thirty.

So one artefact is reserved BEFORE it is old enough, and the one reserved is
the oldest that is not yet old enough -- the next to cross the line. It ages
in place until it crosses M days, at which point it becomes the newest
artefact old enough and the second tier selects it; the previous incumbent
stops being the newest old-enough artefact at that moment and is released.
That is the rotation, and it is why the incumbent's age stays bounded
instead of climbing forever.

The cost is one slot. At most N + 2 artefacts are ever kept -- with the
shipped defaults, eight: six recent, the incumbent, and its successor in
transit. Spending a slot rather than a daily was a deliberate choice.

Which is why this lives here as one pure function rather than as a prune()
method on each destination. Behind a per-transport prune(keep) the tiered
rule gets hand-written once per transport, every implementation disagrees in
its own way, and the way they disagree is by deleting the aged artefact.
That is data loss, not untidiness, and it is silent: the pruning looks like
it worked, and nobody finds out until a restore is needed.

Pure has a second payoff. Every hazard below -- clock skew, an empty
listing, a truncated listing, a misconfigured tier -- is reachable in a test
with a datetime and a list, no server and no filesystem involved. So is the
steady state: iterating this function over four hundred simulated cycles is
how the missing third tier was found, and that is only cheap because there
is nothing to stand up first.

THE SAFE FAILURE IS ALWAYS TO DELETE NOTHING. Every uncertainty in this
module raises RetentionUnsafe, and the caller's answer to that is to prune
nothing at all, anywhere, this cycle. Backups accumulating for one extra day
costs disk. Getting it wrong in the other direction costs the artefact the
whole scheme exists to preserve.
"""

# Standard library
from datetime import datetime, timedelta

# Services
from app.services.backup.artifact import RemoteArtifact


# How far ahead of `now` an artefact may claim to have been created before
# this module refuses to reason about the listing at all.
#
# An artefact from the future is not a curiosity, it is a statement that one
# of the two clocks involved is wrong -- either the host that named it or
# the host judging it now. Either way the age of every other artefact in the
# listing is suspect, and age is the entire input to the aged tier. Five
# minutes absorbs ordinary NTP drift between two machines; anything past it
# is a real disagreement.
_FUTURE_TOLERANCE = timedelta(minutes=5)


class RetentionUnsafe(Exception):
    """
    Raised when the keep set cannot be computed with confidence.

    Deliberately not a subclass of LibexException. Those carry a .message
    that the API's handler copies into an HTTP response body, and they are
    logged by str()-ing the exception; nothing in the backup package may
    reach either path. This is a plain exception whose message is a fixed
    string written here, never interpolated from data.

    The caller catches this, logs it at ERROR, and prunes nothing.
    """


def keep_set(
    listing: list[RemoteArtifact],
    now: datetime,
    *,
    recent: int,
    aged_days: int,
) -> tuple[list[RemoteArtifact], list[RemoteArtifact]]:
    """
    Splits a destination's listing into (keep, delete).

    The keep set is computed in full before the caller deletes anything.
    There is no incremental "walk the list and drop the old ones" form of
    this function on purpose: a walk that fails partway has already deleted
    part of the archive on the strength of a decision it never finished
    making.

    now must be timezone-aware, as must every artefact's created_at --
    artifact.parse guarantees the latter, and this checks anyway, because
    the failure of a naive datetime is a silent offset rather than an
    error.

    Raises RetentionUnsafe rather than returning a best guess whenever the
    inputs cannot support a confident answer.
    """
    if now.tzinfo is None:
        raise RetentionUnsafe("retention requires a timezone-aware now")

    # A recent tier of zero would put every artefact on the server into the
    # delete list the moment the aged tier also declined to keep one, which
    # is a configuration that empties the archive. There is no legitimate
    # value below 1: an operator who wants no backups stops running the
    # container.
    if recent < 1:
        raise RetentionUnsafe("retention recent tier must keep at least one artefact")

    # 0 disables the aged tier and its successor together, which is a
    # supported choice. Negative is a value nobody meant, and reading it as
    # a timedelta would make every artefact "old enough", including the one
    # taken a minute ago.
    if aged_days < 0:
        raise RetentionUnsafe("retention aged-days must not be negative")

    for artifact in listing:
        if artifact.created_at.tzinfo is None:
            raise RetentionUnsafe("listing contains an artefact with a naive timestamp")
        if artifact.created_at > now + _FUTURE_TOLERANCE:
            raise RetentionUnsafe("listing contains an artefact timestamped in the future")

    # Two entries under one name cannot exist in a single remote directory,
    # so this means the listing itself is not what it appears to be -- a
    # destination that concatenated two directories, or a transport
    # returning full paths where it promised names. Deleting "the duplicate"
    # would delete both.
    names = [artifact.name for artifact in listing]
    if len(set(names)) != len(names):
        raise RetentionUnsafe("listing contains duplicate artefact names")

    # Newest first. Ordering comes from RemoteArtifact.sort_key, which is
    # built from the parsed name -- never from a server-reported time.
    ordered = sorted(listing, key=lambda artifact: artifact.sort_key, reverse=True)

    keep: dict[str, RemoteArtifact] = {}

    # Tier one: the most recent N.
    for artifact in ordered[:recent]:
        keep[artifact.name] = artifact

    if aged_days > 0:
        cutoff = _cutoff(now, aged_days)

        # Tier two, the incumbent: the newest artefact at least aged_days
        # old. `ordered` is newest first, so the first one old enough is the
        # newest one old enough. If nothing is old enough yet -- a young
        # archive -- this tier keeps nothing, which is correct and is not an
        # error: it can only ever add to the keep set, never take from it.
        for artifact in ordered:
            if artifact.created_at <= cutoff:
                keep[artifact.name] = artifact
                break

        # Tier three, the successor: the oldest artefact NOT yet old enough,
        # reserved now so that it is still here when it crosses the cutoff
        # and becomes the incumbent. Without this the recent tier deletes
        # every candidate years before it qualifies and tier two never gets
        # a first artefact, or never gets a second one. `ordered` is newest
        # first, so the last entry above the cutoff is the oldest one below
        # aged_days.
        younger = [artifact for artifact in ordered if artifact.created_at > cutoff]
        if younger:
            successor = younger[-1]
            keep[successor.name] = successor

    delete = [artifact for artifact in ordered if artifact.name not in keep]

    # A self-check, not a formality. Everything above is a small amount of
    # obvious code today, and this function is the one place in the package
    # where being wrong destroys data rather than costing a cycle. The
    # invariants are cheap to state and cheap to verify, and a future edit
    # that breaks one of them fails loudly here instead of quietly on the
    # server.
    if set(keep) & {artifact.name for artifact in delete}:
        raise RetentionUnsafe("keep and delete sets overlap")
    if len(keep) + len(delete) != len(listing):
        raise RetentionUnsafe("keep and delete do not account for the whole listing")
    if listing and not keep:
        raise RetentionUnsafe("a non-empty listing produced an empty keep set")

    return sorted(keep.values(), key=lambda artifact: artifact.sort_key, reverse=True), delete


def _cutoff(now: datetime, aged_days: int) -> datetime:
    """
    The instant an artefact has to predate to count as aged.

    Wrapped because the arithmetic is the one line in this module that can
    raise something other than RetentionUnsafe: timedelta refuses more than
    999,999,999 days, and subtracting a merely enormous one from `now` walks
    off the bottom of the datetime range. Either way the answer is
    OverflowError, which is not a RetentionUnsafe, which means it escapes
    the caller's prune-nothing handler entirely -- surfacing as "destination
    raised an unexpected error" and marking a destination whose upload
    SUCCEEDED as failed. This module's contract is that every uncertainty
    leaves by the same door.
    """
    try:
        return now - timedelta(days=aged_days)
    except OverflowError as exc:
        raise RetentionUnsafe("retention aged-days is too large to compute an age against") from exc
