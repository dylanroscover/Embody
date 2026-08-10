"""Platform layer for the embody-convoy host app.

Phase 1 (CONVOY_PLAN 5.2). Stdlib only, TD-import-free, every platform
branch injectable (D-5 Mac-by-construction: the darwin/linux paths are
exercised by tests on any machine, not by owning the hardware).

Owns the three per-machine facts everything else needs:

  - the DATA DIR: host-private state (host DB, identity, token, portfile).
    NEVER inside a git tree -- A-12 requires node_id to live host-private,
    never in a tracked file, so the entire state directory is per-machine.
  - the IPC TOKEN: the local-IPC credential (12.2: "local IPC
    authentication"). A per-install random secret; possession proves the
    caller is the same OS user, because the file is created 0600.
  - the PORTFILE: how local clients (bridge, ConvoyExt) find the host app
    without a fixed port -- the same pattern Embody's instance registry
    already uses.
"""

import json
import ntpath
import os
import posixpath
import secrets
import stat
import time
import sys

APP_DIR_NAME = "EmbodyConvoy"
TOKEN_FILE = "host.token"
PORT_FILE = "host.portfile.json"
DB_FILE = "host.db"
LOCK_FILE = "host.lock"


def data_dir(platform=None, env=None, home=None):
    """Per-user, per-machine state directory for the host app.

    win32:  %LOCALAPPDATA%\\EmbodyConvoy   (Local, NOT Roaming: identity
            must never follow a roaming profile onto a second machine --
            that would silently violate A-12's clone-uniqueness)
    darwin: ~/Library/Application Support/EmbodyConvoy
    linux:  $XDG_STATE_HOME/embody-convoy or ~/.local/state/embody-convoy

    Joins with the TARGET platform's separator, not the host's:
    os.path.join on Windows would hand back
    `/Users/x/Library\\Application Support\\EmbodyConvoy` for the darwin
    branch. Harmless while platform is always sys.platform -- but the
    seam exists so tests can drive foreign platforms, and a seam that
    yields host-flavoured separators is not exercising the foreign path
    at all. (Exactly the failure the first macOS CI run caught in the
    bridge suite, one layer down.)
    """
    platform = platform or sys.platform
    env = env if env is not None else os.environ
    home = home or os.path.expanduser("~")
    join = ntpath.join if platform == "win32" else posixpath.join
    if platform == "win32":
        base = env.get("LOCALAPPDATA") or join(home, "AppData", "Local")
        return join(base, APP_DIR_NAME)
    if platform == "darwin":
        return join(home, "Library", "Application Support", APP_DIR_NAME)
    base = env.get("XDG_STATE_HOME") or join(home, ".local", "state")
    return join(base, "embody-convoy")


def _fsync_directory(path):
    """Make a rename itself durable. No-op on Windows (no directory fd)
    and best-effort everywhere: a filesystem that refuses the sync must
    not turn an otherwise-successful write into a failure."""
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


# The replace retry schedule. SHAPE IDENTICAL to convoy_lifecycle's
# _atomic_replace (attempt 0 immediate, then a 5 ms linear backoff), and
# deliberately so -- one contract for "a Windows reader is holding the
# destination", written twice because these two modules do not import
# each other.
#
# The ATTEMPT COUNT is the one place they differ, and the divergence is
# the point. _atomic_replace rewrites one lifecycle file occasionally;
# this function is the daemon's sole writer for every job record, and a
# peer revocation drives it 250+ times back to back. At 8 attempts the
# budget was 105 ms, which is smaller than the 100 ms+ stall a shared CI
# runner takes at arbitrary points and smaller than a scanner's hold on
# a file it has just seen written -- and that is what surfaced as 7
# per-record errors in a 250-record revocation on windows-latest.
#
# The ceiling is just as real: _revoke_peer_work calls this INSIDE the
# host lock, one record at a time, and a revocation may not stall the
# host (test_new6_a_revocation_does_not_stall_the_whole_host holds the
# worst lock wait under 1 s). 12 attempts sleeps 275 ms in total --
# 2.6x the old budget, and still comfortably inside that bound.
_REPLACE_ATTEMPTS = 12
_REPLACE_BACKOFF_S = 0.005


def _write_private(path, data, *, replace=None, sleep=None):
    """Create/replace a file readable only by the owner, atomically.

    0600 before content lands: open with O_CREAT|O_EXCL on a temp name,
    fchmod-equivalent via the mode argument, then os.replace. On Windows
    the mode bits are advisory (NTFS ACLs inherit from the profile dir,
    which is already per-user), so this is belt-and-braces there and the
    real protection on POSIX.

    `replace` and `sleep` are INJECTION SEAMS, mirroring _atomic_replace
    down to being keyword-only: the retry loop below is a deadline, and a
    deadline tested against a real clock on a shared runner measures the
    runner. Both default to the real thing and no production caller
    passes either -- all nineteen call this with exactly (path, data),
    which is also why a third POSITIONAL parameter must never be added
    here: it would bind silently at every one of them.
    """
    replace = replace or os.replace
    sleep = sleep or time.sleep
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    # UNPREDICTABLE name + O_EXCL: with a fixed temp path and O_TRUNC the
    # mode argument is ignored whenever the file already exists, so a
    # pre-created temp (another local user, or a crash leftover) kept its
    # old permissions and this function's 0600 promise was false.
    # O_NOFOLLOW where available refuses a symlink swap.
    tmp = f"{path}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(tmp, flags, stat.S_IRUSR | stat.S_IWUSR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(data)
            # FLUSH AND SYNC BEFORE THE RENAME. os.replace is atomic for
            # the NAME; it says nothing about the DATA reaching the disk.
            # Without this, a power loss or hard kill can leave the new
            # name pointing at unwritten (zero-filled) content -- and this
            # is the sole writer for host.json, every delivery record and
            # marker, policy.json, realm.json, peers.json, the TLS
            # material, the portfile and the IPC token. Three sibling
            # modules in this daemon already do it (convoy_lifecycle,
            # convoy_artifacts, convoy_peerclient), so its absence here
            # was an inconsistency rather than a decision.
            #
            # THIS FD IS THE TEMP FILE'S, NOT THE DESTINATION'S. Syncing
            # it cannot lengthen the window in which the destination is
            # held open -- this function never opens the destination at
            # all; the concurrent READER does. That is why withdrawing
            # the sync was not the fix for the sharing violations it was
            # blamed for (16ca52c): the real cost is elapsed time, which
            # is paid for above in _REPLACE_ATTEMPTS, and proved by
            # test_convoy_platform's mass rewrite under injected
            # contention -- the test that commit said it was owed.
            f.flush()
            os.fsync(f.fileno())
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    # os.replace is atomic, but on Windows it raises a sharing violation
    # (PermissionError) when a concurrent reader holds the destination
    # open -- exactly what happens when the host app rewrites a job record
    # a status poll or a LOCK-FREE drain snapshot is reading. The reader's
    # window used to be sub-millisecond, but drain_once's snapshots scan
    # job files outside the app lock and hold each open for a whole
    # json.load -- immediate retries alone provably lost that race under
    # load (review probe, 2026-07-31). Short growing sleeps cover the
    # reader's full window; the first retry stays immediate so the hot
    # path is not slowed. The budget itself is _REPLACE_ATTEMPTS above.
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            replace(tmp, path)
            _fsync_directory(directory)
            return
        except PermissionError:
            if attempt == _REPLACE_ATTEMPTS - 1:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            if attempt:
                sleep(_REPLACE_BACKOFF_S * attempt)


def ensure_ipc_token(directory):
    """Return the per-install IPC token, minting it on first run.

    The token authenticates LOCAL clients to the host app (12.2). It is
    an authorization boundary against other-user processes on a shared
    machine, not against the machine owner -- stated honestly, per the
    plan's A-8 style. 256 bits, hex.
    """
    path = os.path.join(directory, TOKEN_FILE)
    try:
        with open(path, "r", encoding="utf-8") as f:
            token = f.read().strip()
        # A truncated/tampered file must not become a low-entropy
        # credential: only a well-formed 256-bit hex token is accepted,
        # anything else is re-minted.
        if len(token) == 64:
            int(token, 16)
            return token
    except (OSError, ValueError):
        pass
    token = secrets.token_hex(32)
    _write_private(path, token + "\n")
    return token


def write_portfile(directory, port, pid, host_id):
    """Record where the running host app listens, atomically."""
    payload = json.dumps({
        "port": int(port),
        "pid": int(pid),
        "host_id": host_id,
        "protocol": "convoy-host/1",
    }, sort_keys=True)
    _write_private(os.path.join(directory, PORT_FILE), payload + "\n")


def read_portfile(directory):
    """Parse the portfile. Returns the dict, or None if absent/corrupt.

    Corrupt is treated as absent -- a half-written or stale portfile must
    make clients probe/relaunch, never crash them.
    """
    try:
        with open(os.path.join(directory, PORT_FILE),
                  "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and data.get("port"):
            return data
    except (OSError, ValueError):
        pass
    return None


def clear_portfile(directory):
    try:
        os.unlink(os.path.join(directory, PORT_FILE))
    except OSError:
        pass


# Windows access rights / error codes for the liveness probe.
_SYNCHRONIZE = 0x00100000
_ERROR_ACCESS_DENIED = 5
_WAIT_OBJECT_0 = 0


def _win32_pid_is_alive(pid, kernel32=None):
    """win32 liveness, applying the SAME rule the POSIX branch states:
    a process we are not ALLOWED to open is a process that EXISTS.

    OpenProcess failing is not evidence of death. ACCESS_DENIED means
    the kernel FOUND the process and refused us -- exactly what EPERM
    means on POSIX, where this module already maps to alive. Calling it
    dead lets a host app at another integrity level (elevated, or the
    service-mode supervisor A-47 implies) read as gone, so every client
    would probe STALE forever while /health answered fine.

    Measured on Windows 11: pid 4 (System, alive) -> OpenProcess NULL
    with GetLastError 5; an unused pid -> GetLastError 87
    (ERROR_INVALID_PARAMETER). Retrying with
    PROCESS_QUERY_LIMITED_INFORMATION was considered and rejected: it
    returns the SAME error code for both pids, so it decides nothing the
    ACCESS_DENIED rule has not already decided, and a handle opened
    without SYNCHRONIZE makes WaitForSingleObject fail -- reintroducing
    the "openable after death reads alive forever" bug this function
    exists to avoid.

    kernel32 is injected so both outcomes are testable off-Windows.
    """
    if kernel32 is None:
        import ctypes
        kernel32 = ctypes.WinDLL("kernel32")
    handle = kernel32.OpenProcess(_SYNCHRONIZE, False, int(pid))
    if not handle:
        return kernel32.GetLastError() == _ERROR_ACCESS_DENIED
    try:
        # WAIT_OBJECT_0 means the process object is signaled, which
        # happens exactly on exit -- a handle alone stays openable
        # after death and would read alive forever.
        return kernel32.WaitForSingleObject(handle, 0) != _WAIT_OBJECT_0
    finally:
        kernel32.CloseHandle(handle)


def pid_is_alive(pid, platform=None, kill=None, kernel32=None):
    """Is this pid running? platform/kill/kernel32 injected (D-5), never
    reached on a foreign-platform test.

    On Windows os.kill(pid, 0) calls TerminateProcess and would KILL the
    target -- the documented TD-killing hazard -- so win32 uses
    OpenProcess via ctypes and POSIX uses signal 0.
    """
    platform = platform or sys.platform
    if not pid or pid <= 0:
        return False
    if platform == "win32":
        try:
            return _win32_pid_is_alive(pid, kernel32=kernel32)
        except Exception:
            return False
    if kill is None:
        if sys.platform == "win32":
            # HARD REFUSAL, not a fallback. Reaching here means a caller
            # asked for the POSIX branch on a Windows host without
            # injecting kill -- and os.kill(pid, 0) on Windows calls
            # TerminateProcess, i.e. it KILLS the pid it was asked to
            # merely inspect. That exact hazard has killed TD in this
            # project before. A comment is not enforcement.
            raise RuntimeError(
                "refusing the POSIX liveness branch on a win32 host "
                "without an injected kill: os.kill would TERMINATE the "
                "target process, not probe it")
        kill = os.kill
    try:
        kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # EPERM proves the process EXISTS and belongs to another user.
        # Mapping it to "dead" would let a foreign-owned host app read as
        # gone, and a stale portfile read as free.
        return True
    except OSError:
        return False


def read_live_portfile(directory, platform=None, kill=None):
    """The portfile ONLY if the process that wrote it is still alive.

    A portfile is a hint, never a fact: no amount of graceful shutdown
    covers a hard kill, a power cut, or a supervisor SIGKILL -- and on
    Windows `terminate()` skips the cleanup handler entirely (observed
    on the first Phase 1 exit-proof run: the file outlived the process).
    Every client must go through this, not read_portfile, so a dead port
    can never be handed out as live.
    """
    data = read_portfile(directory)
    if not data:
        return None
    try:
        pid = int(data.get("pid"))
    except (TypeError, ValueError):
        return None         # corrupt is absent, never an exception
    if not pid_is_alive(pid, platform=platform, kill=kill):
        return None
    return data


# -- the singleton lock (A-36: exactly one host app per data dir) ------
#
# WHY A LOCK AND NOT A PID FILE. HostStore.__init__ runs
# _sweep_interrupted_dispatches(), which burns every job left in
# `dispatching` to indeterminate -- correct for THIS process's own
# interrupted forwards, catastrophic against a SECOND live daemon's
# claims on the same data dir. Nothing enforced "one host app per data
# dir" before this function existed; IgnoreNew only covers instances the
# supervisor itself launched, so a hand-started daemon beside a
# supervised one is entirely unpoliced.
#
# A pid file cannot do this job: it survives a crash, so every hard kill
# (SIGKILL, power loss, Windows terminate()) would leave a stale claim
# that either blocks the restart forever or has to be second-guessed by
# the same liveness probe that a recycled pid defeats. An OS-level
# exclusive lock on an open handle is released BY THE KERNEL when the
# process dies, however it dies. That is the whole reason for the shape.


class SingletonHandle:
    """A held singleton lock. Truthy; keep it alive for the process.

    The lock lives on the OPEN FILE, not on its contents, so this handle
    must outlive nothing less than the daemon itself -- letting it fall
    out of scope closes the file and releases the lock, and a second
    daemon would then walk straight into the sweep this guards.
    """

    def __init__(self, path, fileobj, unlocker=None):
        self.path = path
        self.file = fileobj
        self._unlocker = unlocker
        self.released = False

    def __repr__(self):
        return "<SingletonHandle %s%s>" % (
            self.path, " (released)" if self.released else "")


def _default_lock_opener(path):
    """Open (creating) the lock file for locking.

    "a+b" never truncates -- a truncating open would let a NEW process
    clobber the file a LIVE holder has locked, and on POSIX that is
    exactly how a lock file gets replaced out from under its owner.
    """
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    return open(path, "a+b")


def _win32_lock(fileobj):
    """msvcrt byte-range lock, non-blocking. Returns its unlocker.

    msvcrt.locking works from the CURRENT file position, so seek(0)
    first and lock exactly one byte -- locking a byte past EOF is legal
    and keeps the file itself empty. Raises OSError when another process
    holds it, which is the "someone else is running" signal.
    """
    import msvcrt
    fileobj.seek(0)
    msvcrt.locking(fileobj.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock():
        fileobj.seek(0)
        msvcrt.locking(fileobj.fileno(), msvcrt.LK_UNLCK, 1)

    return _unlock


def _posix_lock(fileobj):
    """flock LOCK_EX|LOCK_NB. Returns its unlocker.

    Raises BlockingIOError (an OSError) when held elsewhere.
    """
    import fcntl
    fcntl.flock(fileobj.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock():
        fcntl.flock(fileobj.fileno(), fcntl.LOCK_UN)

    return _unlock


def default_locker(platform=None):
    """The locking primitive for a platform: msvcrt on win32, flock on
    POSIX. Exposed so a test can ask for the FOREIGN branch by name and
    prove the selection, without owning that hardware (D-5)."""
    platform = platform or sys.platform
    return _win32_lock if platform == "win32" else _posix_lock


def acquire_singleton(data_dir, *, platform=None, opener=None, locker=None):
    """Claim "the one host app for this data dir". Handle, or None.

    None means ANOTHER PROCESS HOLDS IT -- a normal, expected outcome
    that main() reports and exits 0 on, never an error.

    opener and locker are both injected (D-5) so the foreign platform's
    branch is exercised on any machine: opener(path) -> a file object,
    locker(fileobj) -> an unlock callable (or None), raising OSError when
    the lock is already held.

    An OSError from the LOCKER is "held" -> None. An error from the
    OPENER is NOT: an unwritable data dir is a broken install, and
    reporting it as "already running" would make the daemon exit 0 every
    minute forever with a message naming the wrong cause -- the silent
    death loop this whole slice exists to avoid. It propagates.
    """
    path = os.path.join(data_dir, LOCK_FILE)
    open_lock = opener or _default_lock_opener
    lock = locker or default_locker(platform)
    fileobj = open_lock(path)
    try:
        unlocker = lock(fileobj)
    except OSError:
        # Held by a live daemon (or, on POSIX, by a process that has not
        # yet released it). Close OUR handle immediately -- an abandoned
        # open file on the same path is harmless, but leaking one per
        # supervisor tick is not.
        try:
            fileobj.close()
        except OSError:
            pass
        return None
    except Exception:
        try:
            fileobj.close()
        except OSError:
            pass
        raise
    return SingletonHandle(path, fileobj, unlocker)


def release_singleton(handle):
    """Release a handle from acquire_singleton. Idempotent, never raises.

    Only the ORDERLY exit path needs this -- the kernel releases the lock
    on process death regardless, which is the property that makes a
    crashed daemon replaceable a minute later. It exists so an in-process
    caller (a test, or a host app embedded in another program) can hand
    the slot back without exiting.
    """
    if handle is None or getattr(handle, "released", False):
        return
    unlocker = getattr(handle, "_unlocker", None)
    if unlocker is not None:
        try:
            unlocker()
        except OSError:
            pass
    fileobj = getattr(handle, "file", None)
    if fileobj is not None:
        try:
            fileobj.close()
        except OSError:
            pass
    handle.released = True
