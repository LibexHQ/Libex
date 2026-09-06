"""
Artefact naming and ordering tests.

The filename is the whole ordering contract for the backup package, so
these are the tests that stop it drifting: a name round-trips to the exact
UTC instant that produced it, anything that is not ours parses to None and
is therefore never a deletion candidate, and a listing sorts by the
timestamp in the name whatever order it arrived in.

That last one has a structural half as well as a behavioural one.
RemoteArtifact carries no mtime and no size, so ordering by upload time is
unavailable rather than discouraged, and the field-set assertion here is
what keeps it that way -- FTP's MDTM precision varies by server, object
stores report upload time rather than dump time, and any re-upload resets
either. An artefact re-uploaded today would read as the newest thing on the
server, fill the recent tier, and push out the aged copy the retention
scheme exists to preserve.
"""

# Standard library
import dataclasses
from datetime import datetime, timedelta, timezone

# Third party
import pytest

# Local
from app.services.backup.artifact import (
    ARTIFACT_PREFIX,
    ARTIFACT_SUFFIX,
    PARTIAL_SUFFIX,
    BackupArtifact,
    RemoteArtifact,
    build_name,
    parse,
    partial_name,
    remote_artifact,
)


# A fixed instant and the one name it may produce. Written out rather than
# formatted from the datetime, so a change to the format is a diff here
# instead of a test that agrees with whatever the code now does.
INSTANT = datetime(2026, 9, 6, 3, 0, 0, tzinfo=timezone.utc)
NAME = "libex-20260906T030000Z.dump"


# ============================================================
# BUILDING A NAME
# ============================================================

def test_build_name_is_the_documented_shape():
    assert build_name(INSTANT) == NAME


def test_build_name_converts_to_utc_rather_than_formatting_local_fields():
    """
    03:00 in a +05:30 zone is 21:30 UTC the previous day, and the name has
    to say so. Formatting the local fields would produce a well-formed name
    that sorts a whole offset away from where the artefact belongs.
    """
    local = datetime(2026, 9, 6, 3, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    assert build_name(local) == "libex-20260905T213000Z.dump"


def test_build_name_refuses_a_naive_datetime():
    with pytest.raises(ValueError):
        build_name(datetime(2026, 9, 6, 3, 0))


def test_build_name_and_parse_round_trip():
    assert parse(build_name(INSTANT)) == INSTANT


# ============================================================
# PARSING A NAME
# ============================================================

def test_parse_returns_the_encoded_utc_instant():
    parsed = parse(NAME)
    assert parsed == INSTANT
    assert parsed.tzinfo is timezone.utc


@pytest.mark.parametrize(
    "name",
    [
        "libex-20260906T030000Z.dump.bak",        # trailing junk
        "old-libex-20260906T030000Z.dump",        # leading junk
        "libex-20260906T030000Z.dump.partial",    # an upload in progress
        "libex-20260906T030000Z.sql",             # not custom format
        "backup-20260906T030000Z.dump",           # somebody else's prefix
        "libex-2026-09-06T03:00:00Z.dump",        # separators
        "libex-20260906T0300Z.dump",              # short timestamp
        "status.json",
        "",
    ],
)
def test_parse_returns_none_for_anything_that_is_not_ours(name):
    """None, never an exception: a remote directory holds whatever an
    operator put in it, and a listing walks every entry it finds."""
    assert parse(name) is None


def test_parse_rejects_a_well_shaped_but_impossible_timestamp():
    """The regex accepts month 13 and hour 99; strptime is what refuses."""
    assert parse("libex-20261332T990000Z.dump") is None


def test_remote_artifact_takes_its_instant_from_the_name():
    found = remote_artifact(NAME)
    assert found == RemoteArtifact(name=NAME, created_at=INSTANT)


def test_remote_artifact_returns_none_for_a_foreign_entry():
    assert remote_artifact("someone-elses-backup.tar.gz") is None


# ============================================================
# THE PARTIAL NAME
# ============================================================

def test_partial_name_appends_the_partial_suffix():
    assert partial_name(NAME) == f"{NAME}{PARTIAL_SUFFIX}"


def test_a_partial_is_invisible_to_listing_and_therefore_to_retention():
    """
    The suffix is chosen to fail the artefact pattern. That is what makes a
    half-transferred file impossible to count as one of the copies we hold
    -- and, on FTP, impossible to restore from by mistake.
    """
    assert parse(partial_name(NAME)) is None
    assert remote_artifact(partial_name(NAME)) is None


# ============================================================
# ORDERING COMES FROM THE NAME, NEVER FROM THE SERVER
# ============================================================

def test_remote_artifact_has_no_field_a_transport_could_fill():
    """
    The structural half of the ordering contract. No mtime, no size, no
    server-supplied anything -- a type that cannot hold the wrong answer,
    rather than a comment asking nobody to use it. If this set ever grows,
    the question to answer is which transport now gets to influence order.
    """
    assert {f.name for f in dataclasses.fields(RemoteArtifact)} == {"name", "created_at"}


def test_a_listing_sorts_by_name_whatever_order_it_arrived_in():
    """
    The behavioural half. This listing arrives in exactly the order a
    re-upload would produce -- the oldest artefact last, as though it were
    the freshest thing on the server -- and still orders by the instant in
    its name.
    """
    names = [
        "libex-20260901T030000Z.dump",
        "libex-20260906T030000Z.dump",
        "libex-20260903T030000Z.dump",
        "libex-20260220T030000Z.dump",
    ]
    listing = [remote_artifact(name) for name in names]

    newest_first = sorted(listing, key=lambda a: a.sort_key, reverse=True)

    assert [a.name for a in newest_first] == [
        "libex-20260906T030000Z.dump",
        "libex-20260903T030000Z.dump",
        "libex-20260901T030000Z.dump",
        "libex-20260220T030000Z.dump",
    ]


def test_sort_key_carries_the_name_so_ordering_is_total():
    """Two artefacts cannot share a name in one directory, so the tie-break
    never fires on real input -- it is there so that "the most recent N"
    means the same thing on every run rather than depending on input order."""
    artifact = remote_artifact(NAME)
    assert artifact.sort_key == (INSTANT, NAME)


# ============================================================
# THE LOCAL ARTEFACT
# ============================================================

def test_backup_artifact_is_frozen():
    """It is handed to every destination at once and they run concurrently."""
    artifact = BackupArtifact(name=NAME, path=f"/spool/{NAME}", created_at=INSTANT, size_bytes=10)
    with pytest.raises(dataclasses.FrozenInstanceError):
        artifact.path = "/somewhere/else"


def test_backup_artifact_carries_a_path_rather_than_an_open_handle():
    """Destinations upload concurrently and each opens its own handle: one
    shared file object has one shared read position, and two readers of it
    would each send half an archive and both report success."""
    fields = {f.name for f in dataclasses.fields(BackupArtifact)}
    assert fields == {"name", "path", "created_at", "size_bytes"}


def test_the_prefix_and_suffix_are_what_the_pattern_is_built_from():
    """Nothing without this exact shape is ever a deletion candidate, so the
    two constants and the parser must not drift apart."""
    assert NAME.startswith(ARTIFACT_PREFIX)
    assert NAME.endswith(ARTIFACT_SUFFIX)
