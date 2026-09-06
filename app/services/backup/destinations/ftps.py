"""
The FTPS destination: explicit FTP over TLS, ftplib and ssl, no dependency.

Everything ftplib does is blocking, so every FTP call in this module runs
inside asyncio.to_thread and nothing here is awaited on the event loop.

FOUR TRAPS LIVE IN THIS FILE. Each is a real behaviour of the standard
library or of common servers, each was measured rather than inferred, and
each is guarded below:

1. FTP_TLS ENCRYPTS THE CONTROL CHANNEL AND LEAVES THE DATA CHANNEL IN THE
   CLEAR UNTIL prot_p() IS CALLED. __init__ sets _prot_p = False and
   ntransfercmd wraps the data socket only when that flag is true. So a
   login that negotiates TLS, reports a certificate and looks entirely
   encrypted is followed by 2.38 GB of database crossing the network as
   plaintext. _connect() calls prot_p() as part of establishing a session,
   never as an optional step at a call site.

2. FTP_TLS'S OWN DEFAULT CONTEXT IS ssl._create_stdlib_context(): verify
   mode 0, check_hostname False, empty trust store. The unverified mode is
   what you get by NOT passing a context, not by asking for one -- which
   makes the realistic mistake forgetting the argument rather than an
   operator choosing insecurity. _build_context() is the only way a session
   is made here and it always produces a verifying context.

3. PYTHON PASSES NO session= TO wrap_socket ON THE DATA CONNECTION, so the
   data channel negotiates a fresh TLS session. vsftpd ships
   require_ssl_reuse=YES by default, so a *correct* prot_p() upload is
   rejected by a default server with "450 TLS session ... has not resumed".
   The tempting fix is deleting prot_p(), which is trap 1. The actual fix is
   _SessionReusingFTPTLS below, resuming the control connection's session on
   the data connection.

4. A SOCKET TIMEOUT IS NOT A TRANSFER DEADLINE. A per-socket timeout only
   fires when nothing arrives for that long; a connection trickling one byte
   a second never trips it and never finishes either. So there are two
   bounds: a socket timeout on every operation, and an overall deadline
   checked in the transfer callback, which is the only place code of ours
   runs during a multi-gigabyte STOR.

The hostname override is order-dependent and has no parameter. Both auth()
and ntransfercmd() read self.host, and FTP.connect() is what assigns it, so
overriding it means constructing FTP_TLS with NO host (passing one connects
and logs in in the constructor, closing the window), then connect(), then
assigning self.host, then login(). Data connections are unaffected:
makepasv() takes the peer address from self.sock.getpeername(), not from
self.host -- read from ftplib in 3.12, not assumed.
"""

# Standard library
import asyncio
import ftplib
import os
import re
import ssl
import threading
import time

# Core
from app.core.logging import get_logger

# Services
from app.services.backup import artifact as artifact_names
from app.services.backup.artifact import BackupArtifact, RemoteArtifact
from app.services.backup.destinations.base import DestinationError, failure_fields


logger = get_logger()

# One megabyte per block. The default 8 KiB would mean roughly 290,000
# round trips through the callback for a 2.38 GB artefact; this is a few
# thousand, still frequent enough that the deadline and the abort flag are
# checked within a second of becoming true.
_BLOCK_SIZE = 1024 * 1024

# Per-socket inactivity timeout. Explicit because ftplib's default is the
# global default socket timeout, which is None -- a connect or a recv
# against a black-holed server blocks forever, inside a thread that nothing
# can cancel.
_SOCKET_TIMEOUT_SECONDS = 60.0

# Leading three digits of an FTP reply, and nothing else. ftplib puts the
# server's whole reply line into the exception message, and that text is
# remote input: it must not reach a log line or an exception message. The
# numeric code is a value from a fixed set defined by the protocol, which is
# what makes extracting it safe when quoting the line is not.
_REPLY_CODE_PATTERN = re.compile(r"^(\d{3})")

# The Destination seam names a method list(), so inside the class body below
# every annotation evaluated AFTER that def sees the method rather than the
# builtin, and `-> list[RemoteArtifact]` on any later method raises
# "TypeError: 'function' object is not subscriptable" at import. Found by
# import, not by review. This alias is bound here at module scope, where no
# shadowing exists, and is what those later annotations use. Method bodies
# are unaffected -- a class namespace is not in the lexical scope of the
# methods defined in it -- so this is only ever about annotations on def
# statements.
_RemoteArtifacts = list[RemoteArtifact]


class _TransferAborted(Exception):
    """
    Raised from inside the transfer callback to stop a STOR that has run
    past its deadline or been asked to stop.

    Raising is the mechanism, not a signal of a bug: storbinary's callback
    is the only code of ours that runs during a transfer, so an exception is
    the only way to interrupt one. ftplib's `with transfercmd(...)` closes
    the data connection on the way out, which is what actually ends the
    transfer.
    """


class _Deadline:
    """
    An overall bound on one operation, plus the runner's stop signal.

    Both live together because both answer the same question at the same
    moment -- should this transfer keep going -- and because asyncio.wait_for
    can answer neither. wait_for cancels the await, not the thread: the
    upload carries on in the background against a socket nobody is watching,
    and the interpreter then waits for that thread at exit. This is checked
    inside the thread, which is where the work actually is.
    """

    def __init__(self, seconds: float, abort: threading.Event):
        self._expires_at = time.monotonic() + seconds
        self._abort = abort

    def check(self) -> None:
        if self._abort.is_set():
            raise _TransferAborted("aborted")
        if time.monotonic() > self._expires_at:
            raise _TransferAborted("deadline")


class _SessionReusingFTPTLS(ftplib.FTP_TLS):
    """
    FTP_TLS that resumes the control connection's TLS session on the data
    connection.

    ftplib's ntransfercmd calls context.wrap_socket without session=, so the
    data connection performs a full, independent handshake. RFC 4217 servers
    commonly require the data connection to resume the control connection's
    session as proof that the same client opened both -- vsftpd's
    require_ssl_reuse defaults to YES -- and refuse it otherwise with a 450.

    Overriding this method is the smallest correct fix. The alternative that
    presents itself when an upload starts failing is removing prot_p(),
    which makes the symptom disappear by sending the database in plaintext.
    """

    def ntransfercmd(self, cmd, rest=None):
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:
            conn = self.context.wrap_socket(
                conn,
                server_hostname=self.host,
                session=self.sock.session,
            )
        return conn, size


def build_context(ca_bundle: str) -> ssl.SSLContext:
    """
    The TLS context for this destination. There is no path through this
    module that produces an unverifying one.

    With a bundle, create_default_context loads that file and only that
    file: it calls load_default_certs() only when cafile, capath and cadata
    are all None. That is a private trust anchor -- measured, one CA in the
    store against 121 system roots without it -- which is to say certificate
    pinning, and against a NAS with a self-signed certificate it is a
    stronger position than trusting every public CA on earth to vouch for a
    box on a LAN. Anyone adding load_default_certs() to make a verification
    failure go away would be converting that pin back into ordinary web PKI
    and should be fixing the bundle instead.

    Without a bundle, the system roots are used and verification still
    happens; the caller warns, because that is a weaker position than the
    pin and the operator should know which one they have.

    minimum_version is set explicitly because the default is
    MINIMUM_SUPPORTED rather than TLS 1.2, and MINIMUM_SUPPORTED is a
    property of the OpenSSL build rather than a decision anyone here made.
    """
    context = ssl.create_default_context(cafile=ca_bundle or None)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


class FTPSDestination:
    """
    One configured FTPS server.

    A fresh control connection per operation, rather than one held open
    across a cycle. The cycle spans a nine-minute dump and an upload that
    may run for an hour, and an FTP control connection idle across either is
    a connection the server has already dropped -- reconnecting costs a
    handshake once a day and removes a whole class of "421 Timeout" failure.
    """

    provider = "ftps"

    def __init__(
        self,
        *,
        host: str,
        port: int,
        user: str,
        password: str,
        remote_dir: str,
        context: ssl.SSLContext,
        server_hostname: str,
        deadline_seconds: float,
        abort: threading.Event,
        name: str = "ftps",
    ):
        self.name = name
        self._host = host
        self._port = port
        self._user = user
        self._password = password
        self._remote_dir = remote_dir.rstrip("/")
        self._context = context
        self._server_hostname = server_hostname
        self._deadline_seconds = deadline_seconds
        self._abort = abort

    # --- the protocol ----------------------------------------------------

    async def list(self) -> list[RemoteArtifact]:
        return await self._in_thread("list", self._list_blocking)

    async def upload(self, artifact: BackupArtifact) -> None:
        await self._in_thread("upload", self._upload_blocking, artifact)

    async def delete(self, name: str) -> None:
        # Names reaching here come from our own listing, which only returns
        # entries matching the artefact pattern -- so this cannot currently
        # fail. It is checked anyway because it is the one call in this
        # module that destroys something: a name with a path separator in it
        # would delete a file in another directory, and the pattern is what
        # rules that out.
        if artifact_names.parse(name) is None:
            raise DestinationError(
                "refusing to delete a name that is not one of our artefacts",
                destination=self.name,
                provider=self.provider,
                phase="delete",
            )
        await self._in_thread("delete", self._delete_blocking, name)

    # --- thread boundary --------------------------------------------------

    async def _in_thread(self, phase: str, func, *args):
        """
        Runs one blocking FTP operation off the event loop and converts
        everything it can raise into a DestinationError carrying allowlisted
        fields only.

        ftplib.all_errors is (Error, OSError, EOFError, ssl.SSLError) --
        every failure mode of the transport, including a TLS handshake
        rejection and a dropped socket. Catching it as one set here is what
        makes "nothing raw escapes" checkable: there is a single boundary,
        and the exception constructed at it never interpolates anything from
        the far side.
        """
        try:
            return await asyncio.to_thread(func, *args)
        except _TransferAborted as exc:
            raise DestinationError(
                "ftps transfer stopped before completing",
                **failure_fields(exc, destination=self.name, provider=self.provider, phase=phase),
            ) from exc
        except ftplib.all_errors as exc:
            raise DestinationError(
                "ftps operation failed",
                **failure_fields(
                    exc,
                    destination=self.name,
                    provider=self.provider,
                    phase=phase,
                    status=_reply_code(exc),
                ),
            ) from exc

    # --- blocking implementations -----------------------------------------

    def _connect(self) -> ftplib.FTP_TLS:
        """
        A logged-in session with both channels encrypted.

        The order is the whole point and cannot be rearranged:

          FTP_TLS(context=...)      no host -- a host here would connect and
                                    log in inside the constructor, before
                                    the hostname override could be applied
          connect(host, port)       assigns self.host
          self.host = override      what auth() and ntransfercmd() check the
                                    certificate against
          login()                   sends AUTH TLS, then USER/PASS
          prot_p()                  PBSZ 0 + PROT P -- without this the data
                                    channel is plaintext
        """
        session = _SessionReusingFTPTLS(context=self._context, timeout=_SOCKET_TIMEOUT_SECONDS)
        session.connect(host=self._host, port=self._port, timeout=_SOCKET_TIMEOUT_SECONDS)
        if self._server_hostname:
            session.host = self._server_hostname
        session.login(user=self._user, passwd=self._password)
        # Not optional, and not a call site's decision. See trap 1 above.
        session.prot_p()
        session.set_pasv(True)
        return session

    def _close(self, session: ftplib.FTP_TLS) -> None:
        """
        QUIT if the server will take it, close the socket regardless.

        quit() sends a command and waits for a reply, which raises on a
        connection that has already gone -- at which point the transfer is
        over and the only thing left to do is release the socket. Failing
        here must never turn a completed upload into a reported failure.
        """
        try:
            session.quit()
        except (*ftplib.all_errors, AttributeError):
            try:
                session.close()
            except (*ftplib.all_errors, AttributeError):
                pass

    def _list_blocking(self) -> _RemoteArtifacts:
        """
        NLST, deliberately, rather than MLSD or LIST.

        NLST returns names and nothing else, which is exactly what this
        package wants: a listing that cannot report a modification time
        cannot tempt anyone into ordering by one. MDTM precision varies by
        server and any re-upload resets it, so a facts-carrying listing here
        would be a listing carrying facts we must not use.

        A server may answer an empty directory with 550. That is treated as
        an empty listing rather than an error, and so is a missing
        directory: both mean this destination holds none of our artefacts,
        and both lead retention to delete nothing.
        """
        session = self._connect()
        deadline = _Deadline(self._deadline_seconds, self._abort)
        names: list[str] = []

        def collect(line: str) -> None:
            deadline.check()
            names.append(line)

        try:
            command = f"NLST {self._remote_dir}" if self._remote_dir else "NLST"
            try:
                session.retrlines(command, collect)
            except ftplib.error_perm as exc:
                code = _reply_code(exc)
                if code != 550:
                    raise
                logger.warning(
                    "Backup: FTPS listing returned no entries",
                    extra={"destination": self.name, "provider": self.provider, "status": code},
                )
                return []
        finally:
            self._close(session)

        artifacts: list[RemoteArtifact] = []
        skipped = 0
        for entry in names:
            # Some servers answer NLST with full paths, some with bare
            # names. basename normalises both, and the artefact pattern
            # rejects anything that is not ours -- including a .partial from
            # an interrupted upload, whose suffix is chosen to fail it.
            found = artifact_names.remote_artifact(os.path.basename(entry.strip()))
            if found is None:
                skipped += 1
                continue
            artifacts.append(found)

        if skipped:
            # A count, not the names. These strings come from the far side
            # of the network and belong to someone else; how many there were
            # is the only part of them that is ours to log.
            logger.info(
                "Backup: ignored entries at the destination that are not our artefacts",
                extra={"destination": self.name, "provider": self.provider, "ignored": skipped},
            )
        return artifacts

    def _upload_blocking(self, artifact: BackupArtifact) -> None:
        """
        STOR to a .partial name, then RNFR/RNTO onto the final name.

        FTP is the one transport where this dance is needed: STOR writes
        progressively at the path it is given, so an aborted transfer leaves
        a truncated file under the name of what should be a complete backup
        -- listed, counted toward the recent tier, and restored from one day
        by someone who has no way to know. The rename is atomic on every
        real server's filesystem, and the partial suffix fails the artefact
        pattern, so a leftover from a failed transfer is invisible to
        listing and to retention rather than being a landmine.

        The file handle is opened here, inside the thread that uses it.
        Destinations upload concurrently, and a shared file object has a
        shared read position: two destinations reading from one would each
        send part of the archive and both report success.
        """
        deadline = _Deadline(self._deadline_seconds, self._abort)
        final_path = self._remote_path(artifact.name)
        partial_path = self._remote_path(artifact_names.partial_name(artifact.name))

        session = self._connect()
        try:
            with open(artifact.path, "rb") as handle:
                session.storbinary(
                    f"STOR {partial_path}",
                    handle,
                    blocksize=_BLOCK_SIZE,
                    callback=lambda _block: deadline.check(),
                )
        except BaseException:
            # An aborted STOR leaves the partial sitting at the destination,
            # and since the name carries the artefact's timestamp, every
            # failed upload leaves another one -- a couple of gigabytes
            # each, on a NAS, accumulating with nothing to remove them.
            # Retention cannot: partials deliberately fail the artefact
            # pattern, so listing never returns them and the keep set never
            # sees them, which is what makes them safe and also what makes
            # them permanent.
            #
            # THE REMOVAL GETS ITS OWN CONNECTION, and the reason is in
            # ftplib's source rather than in anything a server does. When
            # the transfer callback raises -- the deadline, or the runner's
            # stop flag, which is every SIGTERM during an upload --
            # storbinary's `with transfercmd(...)` closes the data socket on
            # the way out and skips both conn.unwrap() and the final
            # voidresp(). The server's transfer-completion reply is then
            # still queued on the control connection, unread. A DELE issued
            # on that same session reads the stale reply as its own answer
            # and quit() reads DELE's, so every reply after the abort is off
            # by one and the outcome of the delete is indeterminate. A fresh
            # control connection has nothing queued on it, which makes this
            # deterministic without depending on where in the transfer the
            # abort landed.
            #
            # A process killed outright still leaves a partial behind, and
            # that is accepted: it costs disk at the destination, never
            # correctness, because an incomplete file under a name nothing
            # recognises can never be mistaken for a backup.
            self._close(session)
            self._remove_partial(partial_path)
            raise

        try:
            # Only after storbinary's own voidresp() has accepted the
            # transfer. A rename before that would publish a file the server
            # has not confirmed it finished writing.
            session.rename(partial_path, final_path)
        finally:
            self._close(session)

    def _remove_partial(self, path: str) -> None:
        """
        Deletes a partial we created, on a connection of its own, swallowing
        anything that goes wrong.

        Only ever called while an upload is already failing. The failure the
        caller is about to raise is the one worth reporting; a tidy-up that
        cannot run must not replace it with a less useful one, and that
        includes the connect itself failing.

        The cost is one extra handshake on a failed upload, and on a stop it
        is bounded by the socket timeout rather than by the transfer
        deadline -- a stop spends a few seconds here before the process
        exits. Paid deliberately: the alternative is either leaving a
        multi-gigabyte partial at every stop, or issuing the delete on a
        control connection whose next reply is known to be the wrong one.
        """
        try:
            session = self._connect()
        except (*ftplib.all_errors, AttributeError):
            return
        try:
            session.delete(path)
        except (*ftplib.all_errors, AttributeError):
            pass
        finally:
            self._close(session)

    def _delete_blocking(self, name: str) -> None:
        session = self._connect()
        try:
            session.delete(self._remote_path(name))
        finally:
            self._close(session)

    def _remote_path(self, name: str) -> str:
        return f"{self._remote_dir}/{name}" if self._remote_dir else name


def _reply_code(exc: BaseException) -> int | None:
    """
    The three-digit FTP reply code from an ftplib exception, or None.

    ftplib raises with the server's entire reply line as the message, so
    str(exc) is remote input and never goes anywhere. The leading code is a
    protocol constant, and reading only the digits is what makes it usable
    in a log field: it is the same allowlisted shape as an HTTP status, and
    it is the difference between "530" (credentials) and "450" (the TLS
    session reuse trap) at a glance.
    """
    match = _REPLY_CODE_PATTERN.match(str(exc))
    return int(match.group(1)) if match else None
