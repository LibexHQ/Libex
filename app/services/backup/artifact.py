"""
Artefact naming and parsing.

The filename is a contract, not a label. Everything downstream that has to
put two artefacts in order -- retention above all -- reads the timestamp out
of the name and never asks the remote server when the file arrived.

That is the whole reason this module exists as its own file. Remote
modification time is available from every transport and is wrong in three
separate ways: FTP's MDTM has server-dependent precision and is optional in
the first place, object stores report the time of the *upload* rather than
the time the dump was taken, and any re-upload of an older artefact resets
it. Sort by mtime and a re-uploaded artefact from March reads as the newest
thing on the server, fills the recent tier, and the aged copy that the whole
retention scheme exists to preserve is what gets pruned. The name cannot
drift, because nothing rewrites it.

So the name carries a UTC timestamp in a format that sorts correctly as
plain text -- fixed width, zero padded, most significant field first -- and
RemoteArtifact deliberately has no mtime field at all. A type that cannot
hold the wrong answer is a stronger guarantee than a comment asking nobody
to use it.
"""

# Standard library
import re
from dataclasses import dataclass
from datetime import datetime, timezone


# libex-20260906T030000Z.dump
#
# The prefix is what tells our own artefacts apart from anything else living
# in the same remote directory: an operator's notes, another system's
# backups, a stray archive. Nothing without this exact shape is ever a
# deletion candidate.
#
# The suffix says custom format (pg_dump -Fc), which is what pg_restore
# --list can read a table of contents out of. A plain .sql dump would verify
# only by being valid text.
ARTIFACT_PREFIX = "libex-"
ARTIFACT_SUFFIX = ".dump"

# The name an upload in progress occupies. See destinations/base.py for the
# invariant this serves: an artefact must not appear under its final name
# until it is complete, so a half-transferred file has to be visibly
# something else while it is being written.
PARTIAL_SUFFIX = ".partial"

# Basic ISO 8601 -- no separators, so it is filename-safe everywhere, and
# still lexicographically ordered because every field is fixed width and
# runs most significant first. The trailing Z is not decoration: it is the
# record that this instant is UTC, which is what makes two artefacts written
# either side of a daylight-saving change still comparable.
_TIMESTAMP_FORMAT = "%Y%m%dT%H%M%SZ"

# Anchored at both ends on purpose. A trailing-match pattern would accept
# "old-libex-20260906T030000Z.dump.bak" as ours and make it eligible for
# deletion.
_NAME_PATTERN = re.compile(
    r"^" + re.escape(ARTIFACT_PREFIX) + r"(\d{8}T\d{6}Z)" + re.escape(ARTIFACT_SUFFIX) + r"$"
)


@dataclass(frozen=True)
class BackupArtifact:
    """
    A dump that exists on the local spool: the thing being uploaded.

    Frozen because it is handed to every configured destination at once and
    each one runs concurrently. Nothing that fans out to several consumers
    should be mutable by any of them.

    path is the spool path and is never sent anywhere -- destinations take
    the name from `name` and read the bytes from `path`, each opening its
    own handle (a shared file object has a shared read position, and two
    concurrent uploads sharing one would each send half an archive).
    """

    name: str
    path: str
    created_at: datetime
    size_bytes: int


@dataclass(frozen=True)
class RemoteArtifact:
    """
    One of our artefacts as it exists at a destination.

    Note what is absent: there is no mtime, no size, and no server-supplied
    field of any kind. created_at is parsed from the name by this module,
    which means every destination reports the same instant for the same
    artefact regardless of what its transport could have told us. Retention
    cannot accidentally sort by upload time because upload time never
    reaches it.

    A destination returns these for artefacts matching our own naming
    contract and nothing else. Files it does not recognise are not ours, are
    never returned, and are therefore never deletion candidates.
    """

    name: str
    created_at: datetime

    @property
    def sort_key(self) -> tuple[datetime, str]:
        """
        Newest-last ordering. The name breaks a tie so that ordering is
        total and stable -- two artefacts cannot share a name in one
        directory, so this never leaves two items genuinely equal, and a
        stable order is what makes "the most recent N" mean the same thing
        on every run.
        """
        return (self.created_at, self.name)


def build_name(created_at: datetime) -> str:
    """
    The artefact name for a dump taken at `created_at`.

    Requires an aware datetime and converts to UTC itself rather than
    assuming the caller already did. A naive datetime here would be
    formatted as though it were UTC whatever it actually was, and the error
    would be invisible: the name would look perfectly well formed and would
    sort into the wrong place by exactly the local UTC offset. Once a
    year, in one direction, that is the difference between keeping and
    deleting the aged copy.
    """
    if created_at.tzinfo is None:
        raise ValueError("build_name requires a timezone-aware datetime")
    return f"{ARTIFACT_PREFIX}{created_at.astimezone(timezone.utc).strftime(_TIMESTAMP_FORMAT)}{ARTIFACT_SUFFIX}"


def parse(name: str) -> datetime | None:
    """
    The UTC instant encoded in an artefact name, or None if the name is not
    one of ours.

    None rather than an exception because "not ours" is the ordinary case --
    a remote directory holds whatever an operator put there -- and a
    listing walks every entry it finds. Callers treat None as "ignore this
    entry", never as an error.

    strptime is what rejects a well-shaped but impossible timestamp: the
    regex would accept 20261332T990000Z, and strptime will not.
    """
    match = _NAME_PATTERN.match(name)
    if not match:
        return None
    try:
        parsed = datetime.strptime(match.group(1), _TIMESTAMP_FORMAT)
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc)


def remote_artifact(name: str) -> RemoteArtifact | None:
    """
    A RemoteArtifact for a listing entry, or None if the entry is not one of
    ours. The one supported way for a destination to build one, so that no
    transport can construct a descriptor whose created_at came from
    anywhere but the name.
    """
    created_at = parse(name)
    if created_at is None:
        return None
    return RemoteArtifact(name=name, created_at=created_at)


def partial_name(name: str) -> str:
    """
    The temporary name an in-progress upload occupies.

    Deliberately fails the artefact pattern (the suffix moves off .dump), so
    a partial left behind by an interrupted transfer is invisible to
    listing, invisible to retention, and can never be counted as one of the
    copies we hold.
    """
    return f"{name}{PARTIAL_SUFFIX}"
