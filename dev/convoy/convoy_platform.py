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


def _write_private(path, data):
    """Create/replace a file readable only by the owner, atomically.

    0600 before content lands: open with O_CREAT|O_EXCL on a temp name,
    fchmod-equivalent via the mode argument, then os.replace. On Windows
    the mode bits are advisory (NTFS ACLs inherit from the profile dir,
    which is already per-user), so this is belt-and-braces there and the
    real protection on POSIX.
    """
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
    # path is not slowed.
    for attempt in range(8):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 7:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            if attempt:
                time.sleep(0.005 * attempt)


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


def pid_is_alive(pid, platform=None, kill=None):
    """Is this pid running? platform/kill injected (D-5), never reached
    on a foreign-platform test.

    On Windows os.kill(pid, 0) calls TerminateProcess and would KILL the
    target -- the documented TD-killing hazard -- so win32 uses
    OpenProcess via ctypes and POSIX uses signal 0.
    """
    platform = platform or sys.platform
    if not pid or pid <= 0:
        return False
    if platform == "win32":
        try:
            import ctypes
            kernel32 = ctypes.WinDLL("kernel32")
            SYNCHRONIZE = 0x00100000
            handle = kernel32.OpenProcess(SYNCHRONIZE, False, int(pid))
            if not handle:
                return False
            try:
                # WAIT_OBJECT_0 means the process object is signaled,
                # which happens exactly on exit -- a handle alone stays
                # openable after death and would read alive forever.
                return kernel32.WaitForSingleObject(handle, 0) != 0
            finally:
                kernel32.CloseHandle(handle)
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
