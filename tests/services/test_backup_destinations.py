"""
Destination construction and FTPS transport tests.

No server and no socket. Every FTP call goes through a recording stand-in
for ftplib's session object, which is what makes the ORDER of the session
setup assertable -- and the order is the whole of trap 1: FTP_TLS encrypts
the control channel and leaves the data channel in the clear until prot_p()
is called, so a login that negotiates TLS, reports a certificate and looks
entirely encrypted is followed by the whole database crossing the network as
plaintext.

The regression this file exists for above all others: A FAILED TRANSFER MUST
NOT STRAND ITS .partial AT THE DESTINATION. Partials deliberately fail the
artefact pattern, which is what makes them invisible to listing and to
retention -- and therefore also what makes them permanent. Measured against
a live server: every failed upload left another couple of gigabytes on the
NAS with nothing in the system able to remove them. The cleanup is best
effort on the connection already in hand, and it must not replace the
failure the caller is about to raise.

Nothing raw escapes, either. The tests below use a server reply that
contains a password, because the thing being asserted is that the reply text
reaches no field, no message and no log line -- only the three-digit code,
which is a protocol constant rather than remote input.
"""

# Standard library
import ftplib
import ssl
import threading
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

# Third party
import pytest
from pydantic import SecretStr

# Local
from app.services.backup.artifact import BackupArtifact
from app.services.backup.destinations import build_destinations
from app.services.backup.destinations.base import DestinationError, failure_fields
from app.services.backup.destinations.ftps import (
    FTPSDestination,
    _Deadline,
    _reply_code,
    _SessionReusingFTPTLS,
    _TransferAborted,
    build_context,
)


CREATED_AT = datetime(2026, 9, 6, 3, 0, tzinfo=timezone.utc)
ARTIFACT_NAME = "libex-20260906T030000Z.dump"

# A reply line of the shape ftplib puts verbatim into an exception message.
# The credential is there on purpose: it is what the assertions look for the
# absence of.
LEAKY_REPLY = "530 Login incorrect for user libex with password hunter2"


def _settings(**overrides):
    values = {
        "backup_ftps_host": "nas.local",
        "backup_ftps_port": 21,
        "backup_ftps_user": "libex",
        "backup_ftps_password": SecretStr("hunter2"),
        "backup_ftps_path": "/backups",
        "backup_ftps_ca_bundle": "",
        "backup_ftps_server_hostname": "",
        "backup_destination_timeout_seconds": 3600,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class _FakeSession:
    """
    Stands in for the logged-in ftplib session. Records the calls in order,
    because the order is what several of the guarantees below actually are.
    """

    def __init__(self, *, nlst=(), fail_on=None, fail_with=None, fail_delete=False):
        self.calls = []
        self.host = None
        self._nlst = list(nlst)
        self._fail_on = fail_on
        self._fail_with = fail_with or ftplib.error_perm(LEAKY_REPLY)
        self._fail_delete = fail_delete

    def _record(self, name, *args):
        self.calls.append((name, *args))
        if self._fail_on == name:
            raise self._fail_with

    def retrlines(self, command, callback):
        self._record("retrlines", command)
        for line in self._nlst:
            callback(line)

    def storbinary(self, command, handle, blocksize=None, callback=None):
        self._record("storbinary", command)
        if callback is not None:
            callback(b"block")

    def rename(self, source, target):
        self._record("rename", source, target)

    def delete(self, path):
        self.calls.append(("delete", path))
        if self._fail_delete or self._fail_on == "delete":
            raise self._fail_with

    def quit(self):
        self.calls.append(("quit",))

    def close(self):
        self.calls.append(("close",))

    def names(self):
        return [call[0] for call in self.calls]


def _destination(session=None, remote_dir="/backups", abort=None, deadline_seconds=3600.0, **kwargs):
    destination = FTPSDestination(
        host="nas.local",
        port=21,
        user="libex",
        password="hunter2",
        remote_dir=remote_dir,
        context=ssl.create_default_context(),
        server_hostname="",
        deadline_seconds=deadline_seconds,
        abort=abort or threading.Event(),
        **kwargs,
    )
    if session is not None:
        destination._connect = lambda: session
    return destination


def _artifact(tmp_path, payload=b"an archive"):
    path = tmp_path / ARTIFACT_NAME
    path.write_bytes(payload)
    return BackupArtifact(name=ARTIFACT_NAME, path=str(path), created_at=CREATED_AT, size_bytes=len(payload))


# ============================================================
# BUILDING DESTINATIONS FROM SETTINGS
# ============================================================

def test_nothing_configured_is_a_working_deployment_with_no_destination():
    """Silent, because there is nothing wrong to report -- exactly as an
    empty axiom_token means stdout only."""
    settings = _settings(backup_ftps_host="", backup_ftps_user="", backup_ftps_password=SecretStr(""), backup_ftps_path="")

    assert build_destinations(settings, threading.Event()) == []


def test_a_fully_configured_ftps_destination_is_built():
    destinations = build_destinations(_settings(), threading.Event())

    assert len(destinations) == 1
    assert (destinations[0].name, destinations[0].provider) == ("ftps", "ftps")


@pytest.mark.parametrize(
    ("missing_field", "override"),
    [
        ("BACKUP_FTPS_HOST", {"backup_ftps_host": ""}),
        ("BACKUP_FTPS_USER", {"backup_ftps_user": ""}),
        ("BACKUP_FTPS_PASSWORD", {"backup_ftps_password": SecretStr("")}),
    ],
)
def test_a_partly_configured_destination_is_inactive_and_names_the_field_it_lacks(caplog, missing_field, override):
    """
    Settings uses extra="ignore", so BACKUP_FTPS_HSOT is discarded in silence
    and the field keeps its empty default -- nothing in pydantic will ever
    mention it. This log line is the only place the difference between
    "configured" and "thought I configured" is visible.
    """
    with caplog.at_level("WARNING"):
        destinations = build_destinations(_settings(**override), threading.Event())

    assert destinations == []
    assert any(missing_field in record.__dict__.get("missing", "") for record in caplog.records)


def test_a_ca_bundle_that_cannot_be_loaded_makes_the_destination_inactive(caplog):
    """
    It does not fall back to the system trust store. The operator asked for
    a specific trust anchor, and quietly substituting a broader one turns a
    pin into ordinary web PKI at the moment the pin stops working -- which
    is precisely when it might be doing its job.
    """
    with caplog.at_level("ERROR"):
        destinations = build_destinations(_settings(backup_ftps_ca_bundle="/no/such/bundle.pem"), threading.Event())

    assert destinations == []
    assert any(record.__dict__.get("ca_bundle_exists") is False for record in caplog.records)


def test_no_ca_bundle_still_verifies_and_says_so(caplog):
    """Verification stays on -- there is no path through this module that
    turns it off -- but the anchor is now every public CA rather than one
    certificate, which is a materially weaker position for a box on a LAN."""
    with caplog.at_level("WARNING"):
        destinations = build_destinations(_settings(backup_ftps_ca_bundle=""), threading.Event())

    assert len(destinations) == 1
    assert any("no CA bundle" in record.getMessage() for record in caplog.records)


def test_port_990_is_flagged_without_refusing_the_destination(caplog):
    """990 is implicit FTPS, which expects TLS before the first command,
    while ftplib speaks explicit FTPS and sends AUTH TLS over a cleartext
    control connection. The failure looks like a hung connection."""
    with caplog.at_level("WARNING"):
        destinations = build_destinations(_settings(backup_ftps_port=990), threading.Event())

    assert len(destinations) == 1
    assert any("implicit FTPS" in record.getMessage() for record in caplog.records)


def test_building_destinations_never_raises():
    """A raise in a container with restart: unless-stopped is a restart
    loop, which is the one failure mode that produces no readable
    explanation anywhere."""
    settings = _settings(backup_ftps_host="  ", backup_ftps_ca_bundle="/no/such/bundle.pem", backup_ftps_port=990)

    assert build_destinations(settings, threading.Event()) == []


def test_the_abort_event_reaches_the_destination():
    """One event shared by the whole run: only a transport doing blocking
    work in a thread has any use for it, which is why it is a constructor
    argument rather than a fourth protocol method."""
    abort = threading.Event()

    destination = build_destinations(_settings(), abort)[0]

    assert destination._abort is abort


# ============================================================
# THE TLS CONTEXT
# ============================================================

def test_the_context_always_verifies():
    """
    FTP_TLS's own default is ssl._create_stdlib_context(): verify mode 0,
    check_hostname False, empty trust store. The unverified mode is what you
    get by NOT passing a context, which makes the realistic mistake
    forgetting the argument rather than an operator choosing insecurity.
    """
    context = build_context("")

    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


def test_the_context_pins_a_minimum_tls_version():
    """The default is MINIMUM_SUPPORTED, which is a property of the OpenSSL
    build rather than a decision anyone here made."""
    assert build_context("").minimum_version == ssl.TLSVersion.TLSv1_2


def test_a_configured_bundle_is_the_only_trust_anchor_loaded():
    """
    create_default_context calls load_default_certs() only when cafile,
    capath and cadata are all None, so passing a bundle is what makes the
    trust store that one file -- certificate pinning, and against a NAS with
    a self-signed certificate a stronger position than trusting every public
    CA on earth to vouch for a box on a LAN.
    """
    with patch("app.services.backup.destinations.ftps.ssl.create_default_context") as create:
        build_context("/etc/ssl/nas.pem")

    assert create.call_args.kwargs["cafile"] == "/etc/ssl/nas.pem"


def test_no_bundle_falls_back_to_the_system_roots_explicitly():
    with patch("app.services.backup.destinations.ftps.ssl.create_default_context") as create:
        build_context("")

    assert create.call_args.kwargs["cafile"] is None


# ============================================================
# SESSION SETUP: THE ORDER IS THE GUARANTEE
# ============================================================

def test_the_data_channel_is_encrypted_before_anything_is_transferred():
    """
    Trap 1. Without prot_p() the control channel is TLS and the data channel
    is plaintext, and nothing about the session looks wrong. prot_p() is
    part of establishing a session here, never an optional step at a call
    site.
    """
    recorded = []

    class _Recording:
        def __init__(self, *args, **kwargs):
            recorded.append(("construct", kwargs.get("host")))
            self.host = None

        def connect(self, host=None, port=None, timeout=None):
            recorded.append(("connect", host, port))

        def login(self, user=None, passwd=None):
            recorded.append(("login", user))

        def prot_p(self):
            recorded.append(("prot_p",))

        def set_pasv(self, value):
            recorded.append(("set_pasv", value))

    with patch("app.services.backup.destinations.ftps._SessionReusingFTPTLS", _Recording):
        _destination()._connect()

    names = [call[0] for call in recorded]
    assert names == ["construct", "connect", "login", "prot_p", "set_pasv"]


def test_the_constructor_is_given_no_host_so_the_override_window_stays_open():
    """
    Passing a host to FTP_TLS connects and logs in inside the constructor,
    before a hostname override could be applied -- and both auth() and
    ntransfercmd() read self.host to decide what the certificate is checked
    against.
    """
    recorded = []

    class _Recording:
        def __init__(self, *args, **kwargs):
            recorded.append(kwargs)
            self.host = None

        def connect(self, **kwargs):
            self.host = kwargs.get("host")

        def login(self, **kwargs):
            recorded.append({"login_host": self.host})

        def prot_p(self):
            ...

        def set_pasv(self, value):
            ...

    with patch("app.services.backup.destinations.ftps._SessionReusingFTPTLS", _Recording):
        _destination()._connect()

    assert "host" not in recorded[0]


def test_the_hostname_override_is_applied_after_connect_and_before_login():
    recorded = {}

    class _Recording:
        def __init__(self, *args, **kwargs):
            self.host = None

        def connect(self, **kwargs):
            self.host = kwargs.get("host")
            recorded["after_connect"] = self.host

        def login(self, **kwargs):
            recorded["at_login"] = self.host

        def prot_p(self):
            ...

        def set_pasv(self, value):
            ...

    destination = FTPSDestination(
        host="10.0.0.5",
        port=21,
        user="libex",
        password="hunter2",
        remote_dir="/backups",
        context=ssl.create_default_context(),
        server_hostname="nas.internal",
        deadline_seconds=60.0,
        abort=threading.Event(),
    )

    with patch("app.services.backup.destinations.ftps._SessionReusingFTPTLS", _Recording):
        destination._connect()

    assert recorded["after_connect"] == "10.0.0.5"
    assert recorded["at_login"] == "nas.internal"


def test_the_data_connection_resumes_the_control_connection_tls_session():
    """
    Trap 3. ftplib passes no session= to wrap_socket, so the data connection
    performs a fresh handshake -- and vsftpd ships require_ssl_reuse=YES, so
    a *correct* prot_p() upload is refused by a default server with a 450.
    The tempting fix is deleting prot_p(), which is trap 1.
    """
    session = _SessionReusingFTPTLS.__new__(_SessionReusingFTPTLS)
    session._prot_p = True
    session.host = "nas.internal"
    session.sock = SimpleNamespace(session="the-control-session")
    wrapped = object()
    session.context = SimpleNamespace(wrap_socket=lambda conn, **kwargs: (wrapped, kwargs))

    with patch("ftplib.FTP.ntransfercmd", return_value=(object(), 123)):
        conn, size = session.ntransfercmd("STOR whatever")

    assert size == 123
    assert conn[1] == {"server_hostname": "nas.internal", "session": "the-control-session"}


def test_the_data_connection_is_not_wrapped_when_protection_was_never_asked_for():
    """The override must not invent encryption on a session that never
    negotiated it -- that would be a TLS handshake against a plaintext
    socket, which fails in a way nobody can read."""
    session = _SessionReusingFTPTLS.__new__(_SessionReusingFTPTLS)
    session._prot_p = False
    raw = object()

    with patch("ftplib.FTP.ntransfercmd", return_value=(raw, 0)):
        conn, _ = session.ntransfercmd("STOR whatever")

    assert conn is raw


# ============================================================
# LISTING
# ============================================================

@pytest.mark.asyncio
async def test_listing_returns_only_our_artefacts():
    """A transport that pre-filters is a transport that has quietly
    implemented half a retention policy, so this filters on ownership and
    nothing else: entries that are not ours belong to somebody else and are
    never deletion candidates."""
    session = _FakeSession(
        nlst=[
            "libex-20260906T030000Z.dump",
            "libex-20260905T030000Z.dump",
            "somebody-elses-backup.tar.gz",
            "notes.txt",
        ]
    )

    listing = await _destination(session).list()

    assert {a.name for a in listing} == {"libex-20260906T030000Z.dump", "libex-20260905T030000Z.dump"}


@pytest.mark.asyncio
async def test_listing_normalises_the_full_paths_some_servers_return():
    session = _FakeSession(nlst=["/backups/libex-20260906T030000Z.dump", " libex-20260905T030000Z.dump "])

    listing = await _destination(session).list()

    assert {a.name for a in listing} == {"libex-20260906T030000Z.dump", "libex-20260905T030000Z.dump"}


@pytest.mark.asyncio
async def test_a_stranded_partial_is_invisible_to_listing():
    """Which is what makes it safe -- an incomplete file under a name nothing
    recognises can never be mistaken for a backup -- and also what makes it
    permanent, which is why the upload path cleans up after itself."""
    session = _FakeSession(nlst=["libex-20260906T030000Z.dump.partial", "libex-20260905T030000Z.dump"])

    listing = await _destination(session).list()

    assert [a.name for a in listing] == ["libex-20260905T030000Z.dump"]


@pytest.mark.asyncio
async def test_every_listed_instant_comes_from_the_name():
    """No transport may report a remote modification time, and
    RemoteArtifact has no field to put one in."""
    session = _FakeSession(nlst=["libex-20260906T030000Z.dump"])

    listing = await _destination(session).list()

    assert listing[0].created_at == CREATED_AT


@pytest.mark.asyncio
async def test_a_550_on_listing_means_an_empty_directory_rather_than_a_failure():
    """A server may answer an empty directory with 550, and so may a missing
    one. Both mean this destination holds none of our artefacts, and both
    lead retention to delete nothing."""
    session = _FakeSession(fail_on="retrlines", fail_with=ftplib.error_perm("550 No files found"))

    assert await _destination(session).list() == []


@pytest.mark.asyncio
async def test_any_other_permanent_error_on_listing_is_a_failure():
    session = _FakeSession(fail_on="retrlines", fail_with=ftplib.error_perm("530 Not logged in"))

    with pytest.raises(DestinationError) as raised:
        await _destination(session).list()

    assert raised.value.fields["phase"] == "list"
    assert raised.value.fields["status"] == 530


@pytest.mark.asyncio
async def test_the_session_is_closed_after_listing():
    session = _FakeSession(nlst=[])

    await _destination(session).list()

    assert "quit" in session.names()


# ============================================================
# UPLOAD: THE PARTIAL DANCE AND ITS CLEANUP
# ============================================================

@pytest.mark.asyncio
async def test_an_upload_stores_to_a_partial_name_and_renames_once_it_is_accepted(tmp_path):
    """
    STOR writes progressively at the path it is given, so an aborted
    transfer would otherwise leave a truncated file under the name of what
    should be a complete backup -- listed, counted toward the recent tier,
    and restored from one day by someone with no way to know.
    """
    session = _FakeSession()

    await _destination(session).upload(_artifact(tmp_path))

    assert session.calls[0] == ("storbinary", f"STOR /backups/{ARTIFACT_NAME}.partial")
    assert session.calls[1] == ("rename", f"/backups/{ARTIFACT_NAME}.partial", f"/backups/{ARTIFACT_NAME}")


@pytest.mark.asyncio
async def test_a_failed_transfer_removes_its_partial_from_the_destination(tmp_path):
    """
    The regression. A partial deliberately fails the artefact pattern, so
    listing never returns it and retention never sees it -- which is what
    makes it safe and also what makes it permanent. Measured against a live
    server: every failed upload left another couple of gigabytes on the NAS
    with nothing in the system able to remove them.
    """
    session = _FakeSession(fail_on="storbinary")

    with pytest.raises(DestinationError):
        await _destination(session).upload(_artifact(tmp_path))

    assert ("delete", f"/backups/{ARTIFACT_NAME}.partial") in session.calls


@pytest.mark.asyncio
async def test_a_failed_transfer_never_publishes_the_final_name(tmp_path):
    session = _FakeSession(fail_on="storbinary")

    with pytest.raises(DestinationError):
        await _destination(session).upload(_artifact(tmp_path))

    assert "rename" not in session.names()


@pytest.mark.asyncio
async def test_the_cleanup_deletes_the_partial_and_never_the_final_name(tmp_path):
    """The one call in the failure path that destroys something. Deleting
    the final name would remove the previous cycle's good artefact on the
    strength of this cycle's failure."""
    session = _FakeSession(fail_on="storbinary")

    with pytest.raises(DestinationError):
        await _destination(session).upload(_artifact(tmp_path))

    deleted = [call[1] for call in session.calls if call[0] == "delete"]
    assert deleted == [f"/backups/{ARTIFACT_NAME}.partial"]


@pytest.mark.asyncio
async def test_a_cleanup_that_cannot_run_does_not_replace_the_real_failure(tmp_path):
    """The control connection often goes with the transfer. The failure the
    caller is about to raise is the one worth reporting."""
    session = _FakeSession(fail_on="storbinary", fail_delete=True)

    with pytest.raises(DestinationError) as raised:
        await _destination(session).upload(_artifact(tmp_path))

    assert raised.value.fields["phase"] == "upload"


@pytest.mark.asyncio
async def test_the_session_is_closed_even_when_the_transfer_fails(tmp_path):
    session = _FakeSession(fail_on="storbinary")

    with pytest.raises(DestinationError):
        await _destination(session).upload(_artifact(tmp_path))

    assert "quit" in session.names()


@pytest.mark.asyncio
async def test_an_upload_with_no_remote_directory_uses_bare_names(tmp_path):
    session = _FakeSession()

    await _destination(session, remote_dir="").upload(_artifact(tmp_path))

    assert session.calls[0] == ("storbinary", f"STOR {ARTIFACT_NAME}.partial")


@pytest.mark.asyncio
async def test_a_stop_request_ends_the_transfer(tmp_path):
    """
    asyncio.wait_for cancels the await, not the thread: the upload would
    carry on in the background against a socket nobody is watching, and the
    interpreter would then wait for that thread at exit. The flag is checked
    inside the callback, which is the only code of ours that runs during a
    STOR.
    """
    abort = threading.Event()
    abort.set()
    session = _FakeSession()

    with pytest.raises(DestinationError) as raised:
        await _destination(session, abort=abort).upload(_artifact(tmp_path))

    assert raised.value.fields["error_type"] == "_TransferAborted"
    assert ("delete", f"/backups/{ARTIFACT_NAME}.partial") in session.calls


@pytest.mark.asyncio
async def test_a_transfer_past_its_deadline_ends(tmp_path):
    """A per-socket timeout only fires when nothing arrives for that long; a
    connection trickling one byte a second never trips it and never finishes
    either."""
    session = _FakeSession()

    with pytest.raises(DestinationError) as raised:
        await _destination(session, deadline_seconds=-1.0).upload(_artifact(tmp_path))

    assert raised.value.fields["error_type"] == "_TransferAborted"


def test_the_deadline_passes_while_there_is_time_and_no_stop_request():
    deadline = _Deadline(3600.0, threading.Event())

    deadline.check()


def test_the_deadline_raises_on_a_stop_request():
    abort = threading.Event()
    abort.set()

    with pytest.raises(_TransferAborted):
        _Deadline(3600.0, abort).check()


# ============================================================
# DELETING
# ============================================================

@pytest.mark.asyncio
async def test_deleting_an_artefact_removes_it_from_the_remote_directory():
    session = _FakeSession()

    await _destination(session).delete(ARTIFACT_NAME)

    assert ("delete", f"/backups/{ARTIFACT_NAME}") in session.calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name",
    [
        "../../etc/passwd",
        "/backups/../../etc/passwd",
        "libex-20260906T030000Z.dump.partial",
        "somebody-elses-backup.tar.gz",
    ],
)
async def test_deleting_refuses_a_name_that_is_not_one_of_our_artefacts(name):
    """
    Names reaching here come from our own listing, so this cannot currently
    fail. It is checked anyway because it is the one call in this module
    that destroys something, and a name with a path separator in it would
    delete a file in another directory.
    """
    session = _FakeSession()

    with pytest.raises(DestinationError) as raised:
        await _destination(session).delete(name)

    assert raised.value.fields["phase"] == "delete"
    assert session.calls == []


# ============================================================
# NOTHING RAW ESCAPES
# ============================================================

@pytest.mark.asyncio
async def test_a_server_reply_never_reaches_the_exception_or_its_fields(tmp_path):
    """
    ftplib puts the server's entire reply line into the exception message,
    which makes str(exc) remote input. app.core.middleware str()s an escapee
    into a log line that is also shipped to Axiom, so a transport error
    carrying a server response there is a disclosure with no bug in this
    package at all -- only an import away.
    """
    session = _FakeSession(fail_on="storbinary", fail_with=ftplib.error_perm(LEAKY_REPLY))

    with pytest.raises(DestinationError) as raised:
        await _destination(session).upload(_artifact(tmp_path))

    assert "hunter2" not in str(raised.value)
    assert not any("hunter2" in str(value) for value in raised.value.fields.values())


@pytest.mark.asyncio
async def test_a_failure_carries_the_allowlisted_fields_and_nothing_else(tmp_path):
    session = _FakeSession(fail_on="storbinary", fail_with=ftplib.error_perm(LEAKY_REPLY))

    with pytest.raises(DestinationError) as raised:
        await _destination(session).upload(_artifact(tmp_path))

    assert set(raised.value.fields) == {"destination", "provider", "phase", "attempt", "error_type", "status"}
    assert raised.value.fields["status"] == 530


@pytest.mark.asyncio
async def test_a_dropped_socket_is_a_destination_error_too():
    """ftplib.all_errors is (Error, OSError, EOFError, ssl.SSLError) --
    every failure mode of the transport, caught as one set so that "nothing
    raw escapes" has a single boundary to check."""
    session = _FakeSession(fail_on="retrlines", fail_with=OSError("connection reset"))

    with pytest.raises(DestinationError) as raised:
        await _destination(session).list()

    assert raised.value.fields["error_type"] == "OSError"
    assert raised.value.fields.get("status") is None


def test_failure_fields_names_the_operation_and_the_class_and_nothing_more():
    fields = failure_fields(ValueError("anything"), destination="ftps", provider="ftps", phase="upload")

    assert fields == {
        "destination": "ftps",
        "provider": "ftps",
        "phase": "upload",
        "attempt": 1,
        "error_type": "ValueError",
    }


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("530 Login incorrect", 530),
        ("450 TLS session of data connection has not resumed", 450),
        ("connection reset by peer", None),
        ("", None),
    ],
)
def test_the_reply_code_is_read_and_the_text_is_not(message, expected):
    """The code is a value from a fixed set defined by the protocol, which
    is what makes extracting it safe when quoting the line is not -- and it
    is the difference between 530 (credentials) and 450 (the TLS session
    reuse trap) at a glance."""
    assert _reply_code(ftplib.error_perm(message)) == expected


def test_destination_error_is_not_on_the_api_exception_hierarchy():
    from app.core.exceptions import LibexException

    assert not issubclass(DestinationError, LibexException)
