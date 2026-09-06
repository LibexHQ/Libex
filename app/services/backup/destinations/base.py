"""
The Destination protocol and the contract behind it.

Three methods, and no fourth:

    async def list() -> list[RemoteArtifact]      names only, no policy
    async def upload(artifact) -> None
    async def delete(name) -> None

A prune(keep) method was considered and rejected. Retention is three tiers --
the most recent N artefacts, plus the newest one at least M days old, plus
the oldest one not yet M days old, which is what lets the second tier ever
have a candidate -- and behind a per-transport prune() that rule would be
hand-written once per transport. Add a second transport and there are two
implementations that will not agree, and the way they would disagree is by
deleting the aged artefact, because it is by construction the oldest thing
on the server and therefore the first casualty of any "delete the oldest"
shortcut. That is data loss, and it is silent until a restore is needed. So
the policy lives in retention.py as one pure function, and a transport does
exactly what it is told.

WHAT A DESTINATION MUST GUARANTEE. A Protocol enforces signatures and can
never enforce behaviour, so these are stated here and satisfied by each
implementation in its own way:

  1. NO ARTEFACT APPEARS UNDER ITS FINAL NAME UNTIL IT IS COMPLETE, and
     list() never returns an incomplete one. FTPS is the only transport
     implemented, and it is the awkward case: STOR writes progressively at
     the final path, so an aborted transfer would leave a truncated file
     sitting under the name of a good backup. ftps.py satisfies the
     invariant by storing to artifact.partial_name() and renaming with
     RNFR/RNTO once the transfer has been acknowledged; the partial name
     deliberately fails the artefact pattern, so it is invisible to listing
     and to retention either way. Stated as an invariant rather than as a
     note about FTP because a resumable-session transport -- Drive, Dropbox,
     OneDrive -- would satisfy it for free by committing the session, and
     the next implementer needs to know that is the bar rather than
     rediscover it.

  2. list() RETURNS NAMES THIS PACKAGE CAN PARSE, and nothing else. Build
     every entry with artifact.remote_artifact(), which returns None for
     anything that is not ours. Entries a transport does not recognise are
     skipped: they belong to somebody else and are never deletion
     candidates. No transport may report a remote modification time, and
     RemoteArtifact has no field to put one in -- ordering comes from the
     name, for the reasons in artifact.py.

  3. FAILURE IS AN EXCEPTION, NEVER A RETURN VALUE. A destination that
     cannot upload raises; it does not return quietly. The runner isolates
     each destination, so one failing does not stop the others, but a
     failure that reports itself as success would let pruning run against a
     server that did not receive this cycle's artefact.

  4. NOTHING RAW ESCAPES. Every exception a transport lets out carries only
     the allowlisted fields below. Nothing from the far side of the network
     -- no server banner, no response text, no path -- and no str() of an
     underlying exception may reach a log line, a status file field or an
     exception message.
"""

# Standard library
from typing import Protocol, runtime_checkable

# Services
from app.services.backup.artifact import BackupArtifact, RemoteArtifact


# The protocol below names a method list(), so every annotation evaluated
# after that def inside the class body sees the method rather than the
# builtin, and `-> list[...]` on any later one raises TypeError at import.
# Safe today only because upload() and delete() return None -- that is a
# property of the current three methods, not of the seam. Bound here at
# module scope, where nothing is shadowed, so the guard is already in place
# for the method that eventually needs it. Same pattern, same reason, as
# ftps.py's own alias. tests/services/test_backup_imports.py is what catches
# a regression, because only an import can.
_RemoteArtifacts = list[RemoteArtifact]


class DestinationError(Exception):
    """
    Anything a destination could not do.

    Not a LibexException subclass, and that is structural rather than
    stylistic. app/main.py's libex_exception_handler copies exc.message into
    the HTTP response body, and generic_exception_handler beside it str()s
    any escapee into a log line that is also shipped to Axiom. A backup
    exception carrying a server response or a connection string into either
    of those is a disclosure with no bug in this package at all -- only an
    import away. Staying off that hierarchy makes it unreachable.

    Both handlers are registered in app/main.py and not in
    app.core.middleware, where this note used to send the next reviewer.
    The described behaviour was right and only the address was wrong, which
    is the worse of the two failures: a pointer into the wrong file costs
    the person checking it more than no pointer at all.

    The message is always a fixed string written in this codebase. The
    detail travels in .fields, which is an allowlist by construction: the
    caller logs those fields and nothing else.
    """

    def __init__(self, message: str, **fields):
        super().__init__(message)
        self.fields = fields


def failure_fields(
    exc: BaseException,
    *,
    destination: str,
    provider: str,
    phase: str,
    attempt: int = 1,
    status: int | None = None,
) -> dict:
    """
    The only thing a failed destination operation may say about itself.

    The same shape as writer.py's _failure_fields and for the same reason,
    but deliberately its own function rather than an import of that one:
    that version reads sqlstate, table_name and constraint_name off a DBAPI
    exception's .orig, which no transport error has, and importing it here
    would pull the entire ORM writer -- every model, every SQLAlchemy
    dialect import -- into a process whose whole point is that it never
    opens a connection pool. error_type is spelled identically so that a
    dashboard filtering on it sees one field across both.

    status is an integer status where the transport has one -- an HTTP code,
    or an FTP reply code extracted as digits. Never the accompanying text: a
    server-supplied message is remote input, and the one place remote input
    must not be interpolated is the log line an operator reads and Axiom
    stores.
    """
    fields = {
        "destination": destination,
        "provider": provider,
        "phase": phase,
        "attempt": attempt,
        "error_type": type(exc).__name__,
    }
    if status is not None:
        fields["status"] = status
    return fields


@runtime_checkable
class Destination(Protocol):
    """
    One place a backup artefact is kept.

    name identifies this configured instance in logs and in the status file
    ("ftps"); provider identifies the kind. Both are labels this codebase
    chose -- never a hostname, a path or anything else an operator
    configured, because both end up in log lines.
    """

    name: str
    provider: str

    async def list(self) -> _RemoteArtifacts:
        """
        Our artefacts at this destination, in no guaranteed order.

        Names only. No filtering, no sorting by policy, no opinion about
        what should be kept -- retention.keep_set is handed the whole
        listing and decides. A transport that pre-filters is a transport
        that has quietly implemented half a retention policy.
        """
        ...

    async def upload(self, artifact: BackupArtifact) -> None:
        """
        Stores the artefact, and does not return until it is complete and
        visible under its final name. Raises DestinationError otherwise.

        Opens its own handle on artifact.path. Destinations run
        concurrently, and a file object shared between two of them has one
        shared read position -- each would send a different half of the
        archive, and both would report success.
        """
        ...

    async def delete(self, name: str) -> None:
        """
        Removes one artefact by name. Called only for names retention
        returned in the delete set, only after this destination's own
        upload succeeded this cycle, and never for anything list() did not
        return.
        """
        ...
