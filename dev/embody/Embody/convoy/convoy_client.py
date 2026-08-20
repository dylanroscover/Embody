"""Finding and talking to the local Convoy host app -- the TD side.

Lives INSIDE the Embody COMP (reached as mod.convoy_client) because
dev/convoy/ exists only in this checkout: a released .tox has no such
path, so ConvoyExt cannot import convoy_hostprobe. This module is that
library's TD-side twin, and the parity test in
dev/embody/unit_tests/test_convoy_client.py is what keeps the two from
drifting -- both must agree on all four probe outcomes.

Stdlib only, TD-import-free, every seam injectable. It is imported by
plain pytest with no TouchDesigner present (that is what puts it on the
windows+macos CI matrix), and it is read from a WORKER THREAD, so it
must never touch an operator, a parameter, or any other main-thread TD
object. Nothing here does.

THE FALLBACK CONTRACT (12.3), inherited verbatim from convoy_hostprobe:
probe() answers with exactly one of three outcomes.

  running   -- a live, identity-confirmed host app, with its port and
               token.
  absent    -- no host app (no portfile, its writer is dead, or its
               token is unreadable). NOT an error: Convoy off is a
               normal, supported state, and it must read as
               'No Convoy host app', never as a failure.
  stale     -- a portfile exists but nothing usable answers on it. Same
               action as absent (do nothing), distinguished so a log can
               say "the host app went away" rather than "never present".

The critical safety property: probe() NEVER returns a port whose writer
is not alive, because it reads through read_live_portfile, which
verifies the pid. A dead port handed out as live was a real bug in the
host app's own portfile handling; a client must not reintroduce it by
reading the raw file.

THE TRUST STORY, stated honestly. The boundary is loopback plus a
per-install IPC token (X-Convoy-Host-Token, read from host.token in the
per-user data dir, created 0600). That authenticates the OS USER, not
the caller: any process running as this user can read the same file and
speak to the same host app. It is a boundary against other users on a
shared machine, not against the machine's owner or anything running as
them. Two consequences are load-bearing here:

  - Identity is confirmed through the UNAUTHENTICATED GET /health before
    the token is ever transmitted. pid-liveness is not identity -- a
    recycled pid or an unrelated process could hold that port, and we
    are about to hand it a credential.
  - This module never fetches, holds, or stores the convoy PSK. The
    group-signing key is host-private; the TD side has no business
    holding it.
"""

import hashlib
import json
import math
import ntpath
import os
import posixpath
import random
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# Must match convoy_platform: the same files, in the same place.
APP_DIR_NAME = "EmbodyConvoy"
TOKEN_FILE = "host.token"
PORT_FILE = "host.portfile.json"
TOKEN_HEADER = "X-Convoy-Host-Token"

STATUS_RUNNING = "running"
STATUS_ABSENT = "absent"
STATUS_STALE = "stale"

# Registration outcomes, as returned by register()/unregister() and
# consumed by status_text(). Named so the extension is not stringly
# typed and a typo cannot silently fall through to a blank status.
STATE_DISABLED = "disabled"
STATE_UNSAVED = "unsaved"
STATE_ABSENT = "absent"
STATE_STALE = "stale"
STATE_REGISTERING = "registering"
STATE_REGISTERED = "registered"
STATE_UNREGISTERED = "unregistered"
STATE_REFUSED = "refused"
STATE_UNREACHABLE = "unreachable"
# The host answered, but with a 5xx: it is BROKEN, not refusing. Kept
# apart from STATE_REFUSED because a transient host-side fault must be
# retried and a policy refusal must not.
STATE_HOST_ERROR = "host_error"
STATE_ERROR = "error"
# Result discriminator for the directory read.  It is intentionally not a
# STATE_* value: status_text() owns the scalar registration-state vocabulary,
# while this payload is projected into the separate Convoy Status sequence.
NETWORK_NODES_RESULT = "nodes"
POLICY_RESULT = "policy"

# Transport failures on /register back off 5 s -> 60 s, jittered.
BACKOFF_BASE_S = 5.0
BACKOFF_CAP_S = 60.0
BACKOFF_JITTER = 0.25

# Reasons that are NEVER a policy decision by the host, whatever status
# they arrive with: the request never left this process, or the answer
# was not an answer. Both are Errors, not Refusals -- "Refused:
# host_bad_response" would read as a decision the host made about us.
NOT_A_REFUSAL = ("unserializable_request", "host_bad_response",
                 "host_response_too_large")

# -- the Convoy Host readout's vocabulary ------------------------------
#
# convoy_install.host_state() owns the DECISION; this module owns the
# WORDS. That split is stated in convoy_install's own constants block
# and it is the reason a host status can never be computed in two places
# and drift. These eight names MIRROR convoy_install.STATE_*; they are
# literals rather than an import because convoy_install is a sibling DAT
# in TD, not an importable package, and an in-TD test drives
# host_status_text with convoy_install's OWN constants so a rename over
# there fails here instead of silently falling through to the default.
HOST_NOT_INSTALLED = "not_installed"
HOST_RUNNING = "running"
HOST_NOT_RUNNING = "not_running"
HOST_STOPPED = "stopped"
HOST_NO_SUPERVISOR = "no_supervisor"
HOST_NEEDS_REPAIR_PYTHON = "needs_repair_python"
HOST_NEWER_INSTALL = "newer_install"
HOST_EXTERNAL_SUPERVISOR = "external_supervisor"

# The five TRANSIENT states. convoy_install never computes these -- they
# describe what the EXTENSION is doing right now, not what is on disk,
# so they are owned here outright.
HOST_CHECKING = "checking"
HOST_INSTALLING = "installing"
HOST_STARTING = "starting"
HOST_INSTALL_FAILED = "install_failed"
# An install that landed on disk but whose DAEMON is still serving the
# previous payload after the settle wait AND one automatic restart. Owned
# here, not by convoy_install.host_state: only the install call knows what
# the daemon answered, and host_state reads disk. The lead words are
# 'Needs repair' on purpose -- that prefix already outranks the node line on
# Status and already classifies FAILED in startup_progress, so a defect the
# user must act on cannot hide behind a green 'Running'.
HOST_STALE_PAYLOAD = "stale_payload"
# The runtime-only repair (plan_install's repair_runtime action) rode
# HOST_INSTALLING and so read 'Installing...' -- which is false in the
# one way this field exists to prevent. A repair writes NO payload: it
# re-resolves the interpreter and rewrites the supervisor definition,
# leaving the installed version and files exactly as they were. Telling
# the user it is installing invites them to expect a version change that
# is never going to happen, on the one path they reached BECAUSE the
# version may not be replaced.
HOST_REPAIRING = "repairing"

HEALTH_TIMEOUT_S = 3.0
REGISTER_TIMEOUT_S = 10.0
# Unregister is best-effort on the way out the door: one attempt, and a
# short one. A shutting-down session must never block on a host app that
# is itself going away.
UNREGISTER_TIMEOUT_S = 1.0
NETWORK_TIMEOUT_S = 5.0
MAX_HOST_RESPONSE_BYTES = 1024 * 1024
MAX_SIBLING_REQUEST_BYTES = MAX_HOST_RESPONSE_BYTES - 64 * 1024
# A Convoy is expected to contain dozens of nodes, not thousands.  Keep the
# TD sequence projection bounded even if a damaged or future host answers
# with a much larger directory.
MAX_STATUS_NODES = 256

# TouchDesigner-originated sibling calls.  These names deliberately do not
# use the STATE_* prefix: status_text's registration vocabulary is pinned and
# independent from an asynchronous relay request's lifecycle.
SIBLING_ACCEPTED = "accepted"
SIBLING_JOB = "job"
SIBLING_CANCEL = "cancel"
SIBLING_TERMINAL_STATES = (
    "succeeded", "failed", "indeterminate", "refused")
SIBLING_TIMEOUT_MAX_S = 3600.0
SIBLING_TEXT_MAX = 256
SIBLING_OPERATION_MAX = 128
SIBLING_POLL_INITIAL_S = 0.10
SIBLING_POLL_MAX_S = 0.75

NODE_DISCRIMINATOR_PREFIX = "nd_"
UNREGISTER_REASONS = ("disabled", "shutdown")
WAKE_TOKEN_MIN_CHARS = 32
WAKE_TOKEN_MAX_CHARS = 128
WAKE_GRACE_MAX_S = 3600

# Registration metadata is descriptive, never identity or authority. Keep
# the allowlist small so a caller cannot accidentally relay an arbitrary TD
# object graph or private state merely because json.dumps happens to accept
# part of it.
REGISTRATION_METADATA_FIELDS = (
    "toe_path",
    "toe_name",
    "node_name",
    "hostname",
    "process_id",
    "embody_version",
    "touchdesigner_version",
)


# -- the three per-machine facts (mirrors convoy_platform) -------------

def data_dir(platform=None, env=None, home=None):
    """Per-user, per-machine state directory for the host app.

    win32:  %LOCALAPPDATA%\\EmbodyConvoy   (Local, NOT Roaming: identity
            must never follow a roaming profile onto a second machine)
    darwin: ~/Library/Application Support/EmbodyConvoy
    linux:  $XDG_STATE_HOME/embody-convoy or ~/.local/state/embody-convoy

    Joins with the TARGET platform's separator, not the host's -- a seam
    that hands back host-flavoured separators is not exercising the
    foreign path at all.
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


# Private alias so probe() can take a parameter NAMED data_dir -- an
# exact signature match with convoy_hostprobe.probe, which is what lets
# the parity test drive both with one set of kwargs.
_machine_data_dir = data_dir


def read_portfile(directory):
    """Parse the portfile. Returns the dict, or None if absent/corrupt.

    Corrupt is treated as absent: a half-written or stale portfile must
    make a client fall back, never crash it.
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
    """Is this pid running? platform/kill/kernel32 injected, so a
    foreign platform's branch is testable on any machine.

    On Windows os.kill(pid, 0) calls TerminateProcess and would KILL the
    target -- the documented TD-killing hazard -- so win32 goes through
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
        # Mapping it to "dead" would let a foreign-owned host app read
        # as gone, and a stale portfile read as free.
        return True
    except OSError:
        return False


def read_live_portfile(directory, platform=None, kill=None):
    """The portfile ONLY if the process that wrote it is still alive.

    A portfile is a hint, never a fact: no amount of graceful shutdown
    covers a hard kill, a power cut, or a supervisor SIGKILL -- and on
    Windows terminate() skips the cleanup handler entirely. Every client
    goes through this, not read_portfile, so a dead port can never be
    handed out as live.
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


def read_token(directory):
    """The per-install IPC token, or None if unreadable.

    Never mints one: minting is the host app's job. A TD side that
    minted a token would hand itself a credential no host app honours.

    ValueError is caught alongside OSError because UnicodeDecodeError IS
    a ValueError: a host.token that is not UTF-8 (saved as "Unicode" out
    of Notepad is enough) would otherwise raise straight through probe()
    and break its "never raises" contract -- killing the D4 worker
    thread instead of returning a result dict. read_portfile above and
    the host app's own ensure_ipc_token already catch both.
    """
    try:
        with open(os.path.join(directory, TOKEN_FILE),
                  "r", encoding="utf-8") as f:
            token = f.read().strip()
        return token or None
    except (OSError, ValueError):
        return None


# -- locating the host app ---------------------------------------------

class HostHandle:
    """A located, live host app: where it is and how to authenticate."""

    def __init__(self, port, host_id, token, data_dir):
        self.port = port
        self.host_id = host_id
        self.token = token
        self.data_dir = data_dir

    @property
    def base_url(self):
        return "http://127.0.0.1:%s" % (self.port,)


class ProbeResult:
    def __init__(self, status, handle=None, detail=""):
        self.status = status
        self.handle = handle
        self.detail = detail

    @property
    def use_convoy(self):
        """True only when a live host app is present. Every other
        outcome means: behave exactly as Embody does today."""
        return self.status == STATUS_RUNNING


def probe(data_dir=None, token_reader=None, portfile_reader=None,
          raw_portfile_reader=None, health_check=None):
    """Locate the local host app. Returns a ProbeResult, never raises.

    The decision tree is convoy_hostprobe.probe's, step for step, and the
    signature matches it argument for argument -- see the parity test.
    Every reader is injected so the whole tree is testable without a
    filesystem; the defaults read the real per-user data dir.
    """
    directory = data_dir or _machine_data_dir()
    read_live = portfile_reader or read_live_portfile
    read_tok = token_reader or read_token
    raw_reader = raw_portfile_reader or read_portfile

    # read_live_portfile verifies the writer is alive; a portfile whose
    # process is gone comes back as None here, exactly like no portfile.
    live = read_live(directory)
    if live is None:
        # Distinguish "there was a portfile but its writer is dead" from
        # "no portfile at all" -- same action, clearer logs.
        if raw_reader(directory) is not None:
            return ProbeResult(STATUS_STALE,
                               detail="host app portfile is stale (writer "
                                      "not alive)")
        return ProbeResult(STATUS_ABSENT,
                           detail="no Convoy host app on this machine")

    token = read_tok(directory)
    if not token:
        # A live host app we cannot authenticate to is unusable; stay
        # quiet rather than send traffic guaranteed to 401.
        return ProbeResult(STATUS_ABSENT,
                           detail="host app is up but its IPC token is "
                                  "unreadable")

    handle = HostHandle(port=live["port"], host_id=live.get("host_id"),
                        token=token, data_dir=directory)

    # pid-liveness is NOT identity: a recycled pid or an unrelated
    # process could hold this port, and we are about to send it the IPC
    # token. Confirm the process actually IS our host app -- it answers
    # /health with the host_id the portfile claims -- before trusting it.
    check = health_check or _default_health_check
    confirmed_id = check(handle)
    if confirmed_id is None:
        return ProbeResult(
            STATUS_STALE,
            detail="a process holds the host port but did not answer "
                   "/health")
    if handle.host_id and confirmed_id != handle.host_id:
        return ProbeResult(
            STATUS_STALE,
            detail="the process on the host port identifies as "
                   "%r, not the expected %r -- likely a recycled port"
                   % (confirmed_id, handle.host_id))
    handle.host_id = confirmed_id
    return ProbeResult(STATUS_RUNNING, handle=handle)


def _default_health_check(handle, opener=None):
    """GET /health (UNAUTHENTICATED -- it is the one open route) and
    return the reported host_id, or None if unreachable. Deliberately
    does NOT send the token: identity is confirmed BEFORE the token is
    ever transmitted."""
    opener = opener or urllib.request.urlopen
    try:
        req = urllib.request.Request(handle.base_url + "/health")
        with opener(req, timeout=HEALTH_TIMEOUT_S) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data.get("host_id")
    except Exception:
        return None


# -- authenticated calls, with fallback --------------------------------

def host_post(handle, path, body, timeout=REGISTER_TIMEOUT_S, opener=None):
    """Authenticated POST. Returns (http_status, parsed_body).

    (None, None) means the TRANSPORT failed -- nothing answered at all,
    and the caller falls back or backs off. Anything else is a real HTTP
    answer, and THE STATUS IS PART OF THAT ANSWER: a 500 is the host
    crashing (retry it), a 409 is the host refusing on policy (obey it).
    Collapsing the two into one "refused" hides a difference the caller
    has to act on, so the code rides back with the body.

    Never raises. Every failure mode -- an unserializable payload, a
    dead socket, a 200 carrying garbage -- comes back as a value,
    because the only caller is a worker thread whose death would be
    silent.
    """
    opener = opener or urllib.request.urlopen
    try:
        data = (None if body is None else
                json.dumps(body, allow_nan=False,
                           ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError, OverflowError, UnicodeError) as e:
        # An unserializable payload is OUR bug, not a transport failure,
        # and it must not read as absence. Status 0 = "never left this
        # process".
        return 0, {"ok": False, "reason": "unserializable_request",
                   "detail": "%s: %s" % (type(e).__name__, e)}
    req = urllib.request.Request(
        handle.base_url + path, data=data, method="POST",
        headers={"Content-Type": "application/json",
                 TOKEN_HEADER: handle.token})
    try:
        with opener(req, timeout=timeout) as resp:
            status = getattr(resp, "status", None) or 200
            try:
                raw = resp.read(MAX_HOST_RESPONSE_BYTES + 1)
            except TypeError:
                raw = resp.read()
    except urllib.error.HTTPError as e:
        # A structured refusal (400/401/404/409) is a real answer, not a
        # transport failure -- surface its body so the caller can act.
        try:
            try:
                raw = e.read(MAX_HOST_RESPONSE_BYTES + 1)
            except TypeError:
                raw = e.read()
            if len(raw) > MAX_HOST_RESPONSE_BYTES:
                return e.code, {
                    "ok": False, "reason": "host_response_too_large",
                    "detail": "response from %s exceeded %d bytes"
                              % (path, MAX_HOST_RESPONSE_BYTES),
                }
            answer = json.loads(raw.decode("utf-8"))
            return e.code, answer
        except (ValueError, OSError, AttributeError, TypeError):
            return e.code, {"ok": False, "reason": "host_http_error",
                            "detail": "HTTP %s" % (e.code,)}
    except (urllib.error.URLError, OSError):
        return None, None       # transport gone -> caller backs off
    if len(raw) > MAX_HOST_RESPONSE_BYTES:
        return status, {
            "ok": False,
            "reason": "host_response_too_large",
            "detail": "response from %s exceeded %d bytes"
                      % (path, MAX_HOST_RESPONSE_BYTES),
        }
    try:
        answer = json.loads(raw.decode("utf-8"))
        return status, answer
    except (ValueError, AttributeError, UnicodeError):
        # A host that answered 200 with a body we cannot parse IS up and
        # reachable. Reporting that as absence would hide a live, broken
        # host behind "No Convoy host app" and hand it the transport
        # backoff instead of a diagnosable error.
        return status, {"ok": False, "reason": "host_bad_response",
                        "detail": "unparseable body from %s" % (path,)}


def host_get(handle, path, query=None, timeout=NETWORK_TIMEOUT_S,
             opener=None):
    """Authenticated bounded GET. Returns ``(status, object)``; never raises.

    This is intentionally separate from the unauthenticated health probe.
    The handle has already been identity-confirmed before it reaches this
    function, so the per-user IPC token is sent only to literal loopback.
    """
    opener = opener or urllib.request.urlopen
    url = handle.base_url + path
    if query:
        try:
            encoded = urllib.parse.urlencode(query)
        except (TypeError, ValueError) as e:
            return 0, {"ok": False, "reason": "unserializable_request",
                       "detail": "%s: %s" % (type(e).__name__, e)}
        url += "?" + encoded
    req = urllib.request.Request(
        url, method="GET", headers={TOKEN_HEADER: handle.token})
    try:
        with opener(req, timeout=timeout) as resp:
            status = getattr(resp, "status", None) or 200
            try:
                raw = resp.read(MAX_HOST_RESPONSE_BYTES + 1)
            except TypeError:
                # Minimal test doubles and some file-like adapters expose
                # read() without a size argument.  The post-read bound still
                # applies, so this does not weaken the production contract.
                raw = resp.read()
    except urllib.error.HTTPError as e:
        try:
            try:
                raw = e.read(MAX_HOST_RESPONSE_BYTES + 1)
            except TypeError:
                raw = e.read()
            if len(raw) > MAX_HOST_RESPONSE_BYTES:
                raise ValueError("oversized")
            answer = json.loads(raw.decode("utf-8"))
            return e.code, answer
        except (ValueError, OSError, AttributeError):
            return e.code, {"ok": False, "reason": "host_http_error",
                            "detail": "HTTP %s" % (e.code,)}
    except (urllib.error.URLError, OSError):
        return None, None
    if len(raw) > MAX_HOST_RESPONSE_BYTES:
        return status, {
            "ok": False,
            "reason": "host_response_too_large",
            "detail": "response from %s exceeded %d bytes"
                      % (path, MAX_HOST_RESPONSE_BYTES),
        }
    try:
        answer = json.loads(raw.decode("utf-8"))
    except (ValueError, AttributeError, UnicodeError):
        return status, {"ok": False, "reason": "host_bad_response",
                        "detail": "unparseable body from %s" % (path,)}
    if not isinstance(answer, dict):
        return status, {"ok": False, "reason": "host_bad_response",
                        "detail": "non-object body from %s" % (path,)}
    return status, answer


def _status_text_field(value, limit):
    if value is None:
        return ""
    text = str(value).strip()
    if any(ord(char) < 32 or ord(char) == 127 for char in text):
        return ""
    return text[:limit]


def _clean_status_node(row):
    """Detach and bound one host-authored status row for TD projection."""
    if not isinstance(row, dict):
        return None
    controller_count = row.get("controller_count")
    if (isinstance(controller_count, bool)
            or not isinstance(controller_count, int)
            or controller_count < 0):
        controller_count = None
    last_seen_age_s = row.get("last_seen_age_s")
    if isinstance(last_seen_age_s, bool):
        last_seen_age_s = None
    else:
        try:
            last_seen_age_s = float(last_seen_age_s)
            if (not math.isfinite(last_seen_age_s)
                    or last_seen_age_s < 0):
                last_seen_age_s = None
        except (TypeError, ValueError, OverflowError):
            last_seen_age_s = None
    clean = {
        "node_id": _status_text_field(row.get("node_id"), 128),
        "host_id": _status_text_field(row.get("host_id"), 128),
        "convoy_id": _status_text_field(row.get("convoy_id"), 128),
        "runtime_id": _status_text_field(row.get("runtime_id"), 128),
        "node_name": _status_text_field(row.get("node_name"), 512),
        "hostname": _status_text_field(row.get("hostname"), 255),
        "toe_name": _status_text_field(row.get("toe_name"), 512),
        "embody_version": _status_text_field(
            row.get("embody_version"), 128),
        "touchdesigner_version": _status_text_field(
            row.get("touchdesigner_version"), 128),
        "ip": _status_text_field(row.get("ip"), 255),
        "status": _status_text_field(row.get("status"), 64),
        "online": bool(row.get("online")),
        "enabled": bool(row.get("enabled", True)),
        "perform_mode": row.get("perform_mode") is True,
        "wake_active": row.get("wake_active") is True,
        "sleeping": row.get("sleeping") is True,
        # Optional coalesced display data. Older hosts omit both; retaining
        # them here costs no extra request and avoids per-node mesh fan-out.
        "controller_count": controller_count,
        "last_seen_age_s": last_seen_age_s,
    }
    # Identity is required for deterministic sequence rows and routing.
    if not clean["node_id"] or not clean["host_id"]:
        return None
    return clean


def network_nodes(handle, convoy_id, opener=None,
                  timeout=NETWORK_TIMEOUT_S):
    """Fetch this node's Convoy directory from the local host app.

    Returns a total result dict suitable for a worker thread.  The result is
    deliberately compact and detached: no host-private paths, approvals, or
    unbounded peer payloads can cross into TouchDesigner.
    """
    if not isinstance(convoy_id, str) or not convoy_id \
            or convoy_id != convoy_id.strip():
        return {"state": STATE_ERROR, "reason": "malformed_convoy_id",
                "detail": "convoy_id must be non-empty canonical text"}
    try:
        raw_id = convoy_id.encode("utf-8")
    except UnicodeEncodeError:
        raw_id = b""
    if (not raw_id or len(raw_id) > 128
            or any(byte < 0x20 or byte == 0x7f for byte in raw_id)):
        return {"state": STATE_ERROR, "reason": "malformed_convoy_id",
                "detail": "convoy_id must be bounded printable text"}
    status, answer = host_get(
        handle, "/network/nodes", {"convoy_id": convoy_id},
        timeout=timeout, opener=opener)
    result = _answer_state(status, answer)
    if result is not None:
        return result
    raw_nodes = answer.get("nodes")
    if not isinstance(raw_nodes, list):
        return {"state": STATE_HOST_ERROR, "http_status": status,
                "reason": "host_bad_response",
                "detail": "network node response omitted its node list"}
    nodes = []
    for row in raw_nodes[:MAX_STATUS_NODES]:
        clean = _clean_status_node(row)
        if clean is not None and clean["convoy_id"] == convoy_id:
            nodes.append(clean)
    return {
        "state": NETWORK_NODES_RESULT,
        "http_status": status,
        "convoy_id": convoy_id,
        "nodes": nodes,
        "truncated": bool(answer.get("truncated"))
                     or len(raw_nodes) > MAX_STATUS_NODES,
        "remote_nodes_available": bool(answer.get(
            "remote_nodes_available", True)),
    }


# -- TouchDesigner-originated sibling relay --------------------------

def _sibling_error(reason, detail):
    return {"state": STATE_ERROR, "ok": False,
            "reason": reason, "detail": detail}


def _sibling_text(value, name, limit=SIBLING_TEXT_MAX):
    if (not isinstance(value, str) or not value
            or value != value.strip() or len(value) > limit
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ValueError("%s must be bounded non-empty printable text" % name)
    return value


def _sibling_timeout(value):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not 0.1 <= float(value) <= SIBLING_TIMEOUT_MAX_S):
        raise ValueError("timeout_s must be within [0.1, 3600]")
    return float(value)


def _sibling_object(value, name):
    if not isinstance(value, dict):
        raise ValueError("%s must be an object" % name)
    try:
        encoded = json.dumps(
            value, allow_nan=False, ensure_ascii=False,
            separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, OverflowError, UnicodeError):
        raise ValueError("%s must contain only JSON data" % name)
    if len(encoded) > MAX_SIBLING_REQUEST_BYTES:
        raise ValueError("%s exceeds the %d-byte request limit"
                         % (name, MAX_SIBLING_REQUEST_BYTES))
    return json.loads(encoded.decode("utf-8"))


def _sibling_target(target_host_id, convoy_id, target_node_id):
    host_id = _sibling_text(target_host_id, "target_host_id")
    node_id = _sibling_text(target_node_id, "target_node_id")
    convoy_id = _sibling_text(convoy_id, "convoy_id", 128)
    try:
        encoded = convoy_id.encode("utf-8")
    except UnicodeEncodeError:
        encoded = b""
    if not encoded or len(encoded) > 128:
        raise ValueError("convoy_id must fit within 128 UTF-8 bytes")
    return host_id, convoy_id, node_id


def _clean_sibling_job(value):
    """Bounded public job projection; never echo stored request arguments."""
    if not isinstance(value, dict):
        return None
    delivery_id = value.get("delivery_id")
    state = value.get("state")
    if not isinstance(delivery_id, str) or not delivery_id \
            or not isinstance(state, str) or not state:
        return None
    names = (
        "delivery_id", "state", "operation", "node_id", "node_job_id",
        "verdict_source", "result", "created", "updated", "terminal_at",
        "outcome_acknowledged_at", "attempts", "last_attempt",
    )
    return {name: value.get(name) for name in names if name in value}


def submit_sibling_call(handle, target_host_id, convoy_id, target_node_id,
                        controller_id, operation, arguments=None,
                        expected_runtime_id=None, idempotency_key=None,
                        timeout_s=30.0, opener=None, monotonic=None):
    """Submit one exact local-or-remote durable call through the host.

    Both same-host siblings and remote peers use /relay. The host handles the
    local case without a LAN round trip and atomically verifies the requested
    Convoy before creating the job; using /nodes then /jobs would leave a
    namespace-change race between those two requests. There is never a
    fallback route. The result is the durable acknowledgement; callers that
    want a terminal verdict use ``wait_sibling_job`` or ``get_sibling_job``.
    """
    try:
        target_host_id, convoy_id, target_node_id = _sibling_target(
            target_host_id, convoy_id, target_node_id)
        controller_id = _sibling_text(controller_id, "controller_id")
        operation = _sibling_text(
            operation, "operation", SIBLING_OPERATION_MAX)
        timeout_s = _sibling_timeout(timeout_s)
        if arguments is None:
            arguments = {}
        arguments = _sibling_object(arguments, "arguments")
        if expected_runtime_id is not None:
            expected_runtime_id = _sibling_text(
                expected_runtime_id, "expected_runtime_id")
        if idempotency_key is None:
            idempotency_key = secrets.token_hex(16)
        else:
            idempotency_key = _sibling_text(
                idempotency_key, "idempotency_key")
    except (TypeError, ValueError) as exc:
        return _sibling_error("invalid_arguments", str(exc))

    local = target_host_id == handle.host_id
    path = "/relay"
    body = {
        "target_host_id": target_host_id,
        "convoy_id": convoy_id,
        "target_node_id": target_node_id,
        "controller_id": controller_id,
        "operation": operation,
        "arguments": arguments,
        "timeout_s": timeout_s,
        "idempotency_key": idempotency_key,
    }
    if expected_runtime_id is not None:
        body["expected_runtime_id"] = expected_runtime_id

    clock = monotonic or time.monotonic
    deadline = clock() + timeout_s
    remaining = deadline - clock()
    if remaining < 0.1:
        return {
            "state": STATE_UNREACHABLE, "ok": False,
            "reason": "request_timeout",
            "detail": "the sibling submission deadline elapsed locally",
            "target_host_id": target_host_id, "convoy_id": convoy_id,
            "target_node_id": target_node_id, "operation": operation,
            "idempotency_key": idempotency_key,
        }
    status, answer = host_post(
        handle, path, body, timeout=remaining, opener=opener)
    result = _answer_state(status, answer)
    if result is not None:
        result.update({"target_host_id": target_host_id,
                       "convoy_id": convoy_id,
                       "target_node_id": target_node_id,
                       "operation": operation,
                       "idempotency_key": idempotency_key})
        return result
    job = _clean_sibling_job(answer.get("job"))
    if job is None:
        return {
            "state": STATE_HOST_ERROR, "ok": False,
            "http_status": status, "reason": "host_bad_response",
            "detail": "call acknowledgement omitted a durable job",
            "target_host_id": target_host_id, "convoy_id": convoy_id,
            "target_node_id": target_node_id, "operation": operation,
            "idempotency_key": idempotency_key,
        }
    return {
        "state": SIBLING_ACCEPTED, "ok": True, "http_status": status,
        "target_host_id": target_host_id, "convoy_id": convoy_id,
        "target_node_id": target_node_id, "operation": operation,
        "idempotency_key": (answer.get("idempotency_key")
                            or idempotency_key),
        "request_id": answer.get("request_id"),
        "created": bool(answer.get("created", True)),
        "local_target": local,
        "job": job,
    }


def get_sibling_job(handle, target_host_id, convoy_id, delivery_id,
                    since=None, opener=None, timeout=NETWORK_TIMEOUT_S):
    """Read one durable job from its explicit owner host without waking TD."""
    try:
        target_host_id = _sibling_text(target_host_id, "target_host_id")
        convoy_id = _sibling_text(convoy_id, "convoy_id", 128)
        delivery_id = _sibling_text(delivery_id, "delivery_id")
        timeout = _sibling_timeout(timeout)
        if since is not None and (
                isinstance(since, bool) or not isinstance(since, (int, float))):
            raise ValueError("since must be a finite number")
        if since is not None and not float("-inf") < float(since) < float("inf"):
            raise ValueError("since must be a finite number")
    except (TypeError, ValueError) as exc:
        return _sibling_error("invalid_arguments", str(exc))

    local = target_host_id == handle.host_id
    if local:
        status, answer = host_get(
            handle, "/jobs/" + urllib.parse.quote(delivery_id, safe=""),
            timeout=timeout, opener=opener)
    else:
        body = {"target_host_id": target_host_id,
                "convoy_id": convoy_id, "delivery_id": delivery_id}
        if since is not None:
            body["since"] = float(since)
        status, answer = host_post(
            handle, "/relay/job", body, timeout=timeout, opener=opener)
    result = _answer_state(status, answer)
    if result is not None:
        result.update({"target_host_id": target_host_id,
                       "convoy_id": convoy_id,
                       "delivery_id": delivery_id})
        return result
    if answer.get("changed") is False:
        return {"state": SIBLING_JOB, "ok": True,
                "http_status": status, "changed": False,
                "cursor": answer.get("cursor"),
                "target_host_id": target_host_id,
                "convoy_id": convoy_id, "delivery_id": delivery_id,
                "local_target": local}
    job = _clean_sibling_job(answer.get("job"))
    if job is None:
        return {"state": STATE_HOST_ERROR, "ok": False,
                "http_status": status, "reason": "host_bad_response",
                "detail": "job response omitted a durable job",
                "target_host_id": target_host_id,
                "convoy_id": convoy_id, "delivery_id": delivery_id}
    # A local job view is host-private and includes its namespace; enforce it
    # before returning anything. The peer-safe remote view is already scoped
    # by the signed/mTLS request and intentionally omits convoy_id.
    raw_job = answer.get("job")
    if local and raw_job.get("convoy_id") != convoy_id:
        return {"state": STATE_REFUSED, "ok": False,
                "reason": "job_namespace_mismatch",
                "detail": "the local delivery belongs to another Convoy",
                "target_host_id": target_host_id,
                "convoy_id": convoy_id, "delivery_id": delivery_id}
    return {"state": SIBLING_JOB, "ok": True,
            "http_status": status, "changed": True,
            "cursor": answer.get("cursor", job.get("updated")),
            "target_host_id": target_host_id,
            "convoy_id": convoy_id, "delivery_id": delivery_id,
            "local_target": local, "job": job}


def cancel_sibling_job(handle, target_host_id, convoy_id, delivery_id,
                       opener=None, timeout=NETWORK_TIMEOUT_S):
    """Cancel a delivery through its exact local-or-remote owner host.

    The federated host route enforces namespace and origin ownership on both
    same-host and peer-host deliveries. It is a management-plane operation
    and never wakes TouchDesigner.
    """
    try:
        target_host_id = _sibling_text(target_host_id, "target_host_id")
        convoy_id = _sibling_text(convoy_id, "convoy_id", 128)
        delivery_id = _sibling_text(delivery_id, "delivery_id")
        timeout = _sibling_timeout(timeout)
    except (TypeError, ValueError) as exc:
        return _sibling_error("invalid_arguments", str(exc))
    base = {"target_host_id": target_host_id, "convoy_id": convoy_id,
            "delivery_id": delivery_id, "scope": "owner_host",
            "remote_supported": True,
            "local_target": target_host_id == handle.host_id,
            "wakes_touchdesigner": False}
    status, answer = host_post(
        handle, "/relay/cancel", {
            "target_host_id": target_host_id,
            "convoy_id": convoy_id,
            "delivery_id": delivery_id,
        },
        timeout=timeout, opener=opener)
    result = _answer_state(status, answer)
    if result is not None:
        result.update(base)
        return result
    out = dict(answer)
    out.update(base)
    out["state"] = SIBLING_CANCEL
    out["http_status"] = status
    return out


def wait_sibling_job(handle, target_host_id, convoy_id, delivery_id,
                     initial=None, timeout_s=30.0, progress=None,
                     opener=None, sleep=None, monotonic=None):
    """Worker-only bounded poll loop with optional plain progress sink."""
    try:
        timeout_s = _sibling_timeout(timeout_s)
    except ValueError as exc:
        return _sibling_error("invalid_arguments", str(exc))
    sleep = sleep or time.sleep
    monotonic = monotonic or time.monotonic
    deadline = monotonic() + timeout_s
    latest = dict(initial) if isinstance(initial, dict) else None
    interval = SIBLING_POLL_INITIAL_S
    cursor = None
    if latest:
        job = latest.get("job")
        if isinstance(job, dict):
            if job.get("state") in SIBLING_TERMINAL_STATES:
                return latest
            cursor = job.get("updated")
    while monotonic() < deadline:
        current = get_sibling_job(
            handle, target_host_id, convoy_id, delivery_id,
            since=cursor, opener=opener,
            timeout=max(0.1, min(NETWORK_TIMEOUT_S,
                                 deadline - monotonic())))
        if current.get("ok"):
            if current.get("changed") is not False:
                latest = current
                job = current.get("job")
                if callable(progress):
                    progress(dict(current))
                if isinstance(job, dict):
                    cursor = current.get("cursor", job.get("updated"))
                    if job.get("state") in SIBLING_TERMINAL_STATES:
                        return current
            interval = min(SIBLING_POLL_MAX_S, interval * 1.5)
        else:
            # An explicit refusal is definitive. Transport loss is returned
            # with the durable identity so the caller can reconcile later;
            # never resubmit from this loop.
            current["delivery_id"] = delivery_id
            return current
        remaining = deadline - monotonic()
        if remaining <= 0:
            break
        sleep(min(interval, remaining))
    out = dict(latest or {})
    out.setdefault("state", SIBLING_JOB)
    out.setdefault("ok", True)
    out.update({"wait_timed_out": True,
                "target_host_id": target_host_id,
                "convoy_id": convoy_id, "delivery_id": delivery_id})
    return out


def _clean_policy(value):
    if not isinstance(value, dict):
        return None
    generation = value.get("generation")
    quota = value.get("artifact_quota_mb")
    if (isinstance(generation, bool) or not isinstance(generation, int)
            or generation < 0 or isinstance(quota, bool)
            or not isinstance(quota, int) or not 0 <= quota <= 1024 * 1024
            or type(value.get("allow_td_python")) is not bool
            or type(value.get("allow_full_shell")) is not bool):
        return None
    return {
        "generation": generation,
        "allow_td_python": value["allow_td_python"],
        "allow_full_shell": value["allow_full_shell"],
        "artifact_quota_mb": quota,
    }


def get_policy(handle, node_id=None, opener=None,
               timeout=NETWORK_TIMEOUT_S):
    query = {"node_id": node_id} if node_id else None
    status, answer = host_get(
        handle, "/policy", query, timeout=timeout, opener=opener)
    result = _answer_state(status, answer)
    if result is not None:
        return result
    clean = _clean_policy(answer.get("policy"))
    if clean is None:
        return {"state": STATE_HOST_ERROR, "http_status": status,
                "reason": "host_bad_response",
                "detail": "policy response was missing or malformed"}
    return {"state": POLICY_RESULT, "http_status": status,
            "policy": clean}


def begin_policy_challenge(handle, setting, expected_generation,
                           node_id=None, opener=None,
                           timeout=REGISTER_TIMEOUT_S):
    body = {"setting": setting,
            "expected_generation": expected_generation}
    if node_id:
        body["node_id"] = node_id
    status, answer = host_post(
        handle, "/policy/challenge", body, timeout=timeout, opener=opener)
    result = _answer_state(status, answer)
    if result is not None:
        return result
    challenge = answer.get("challenge")
    if not isinstance(challenge, dict):
        return {"state": STATE_HOST_ERROR, "http_status": status,
                "reason": "host_bad_response",
                "detail": "policy challenge response was malformed"}
    required = ("challenge_id", "confirmation", "setting", "generation")
    if (any(key not in challenge for key in required)
            or not isinstance(challenge.get("challenge_id"), str)
            or not isinstance(challenge.get("confirmation"), str)
            or not isinstance(challenge.get("setting"), str)
            or isinstance(challenge.get("generation"), bool)
            or not isinstance(challenge.get("generation"), int)):
        return {"state": STATE_HOST_ERROR, "http_status": status,
                "reason": "host_bad_response",
                "detail": "policy challenge fields were malformed"}
    return {"state": "challenge", "http_status": status,
            "challenge": dict(challenge)}


def confirm_policy_challenge(handle, challenge_id, confirmation,
                             expected_generation, opener=None,
                             timeout=REGISTER_TIMEOUT_S):
    status, answer = host_post(
        handle, "/policy/confirm", {
            "challenge_id": challenge_id,
            "confirmation": confirmation,
            "expected_generation": expected_generation,
        }, timeout=timeout, opener=opener)
    result = _answer_state(status, answer)
    if result is not None:
        return result
    return {"state": POLICY_RESULT, "http_status": status,
            "ok": True}


def decline_policy_challenge(handle, challenge_id, opener=None,
                             timeout=REGISTER_TIMEOUT_S):
    status, answer = host_post(
        handle, "/policy/decline", {"challenge_id": challenge_id},
        timeout=timeout, opener=opener)
    result = _answer_state(status, answer)
    return result or {"state": POLICY_RESULT, "http_status": status,
                      "ok": True}


def disable_policy(handle, setting, node_id=None, opener=None,
                   timeout=REGISTER_TIMEOUT_S):
    body = {"setting": setting}
    if node_id:
        body["node_id"] = node_id
    status, answer = host_post(
        handle, "/policy/disable", body, timeout=timeout, opener=opener)
    result = _answer_state(status, answer)
    if result is not None:
        return result
    clean = _clean_policy(answer.get("policy"))
    return {"state": POLICY_RESULT, "http_status": status,
            "policy": clean, "ok": clean is not None}


def set_artifact_quota(handle, quota_mb, expected_generation, opener=None,
                       timeout=REGISTER_TIMEOUT_S):
    status, answer = host_post(
        handle, "/policy/artifact-quota", {
            "artifact_quota_mb": quota_mb,
            "expected_generation": expected_generation,
        }, timeout=timeout, opener=opener)
    result = _answer_state(status, answer)
    if result is not None:
        return result
    clean = _clean_policy(answer.get("policy"))
    return {"state": POLICY_RESULT, "http_status": status,
            "policy": clean, "ok": clean is not None}


def normalize_toe_path(toe_path, platform=None):
    """Canonical saved-.toe path used only to derive stable node identity.

    Different .toe files inside one repository must not collapse into one
    host-side node merely because their Embody COMPs have the same operator
    path. Windows spelling and case variants collapse; POSIX case remains
    significant. The caller supplies the already-saved absolute path from
    TD's main thread.
    """
    if not isinstance(toe_path, str) or not toe_path.strip():
        raise ValueError("toe_path must be a non-empty absolute path")
    platform = platform or sys.platform
    if platform == "win32":
        text = ntpath.normpath(toe_path.replace("/", "\\"))
        if not ntpath.isabs(text):
            raise ValueError("toe_path must be absolute")
        return ntpath.normcase(text)
    text = posixpath.normpath(toe_path.replace("\\", "/"))
    if not posixpath.isabs(text):
        raise ValueError("toe_path must be absolute")
    return text


def stable_node_discriminator(toe_path, platform=None):
    """Opaque stable discriminator for one saved .toe launch profile.

    It is deterministic across restarts and deliberately independent of
    runtime_id, which remains fresh for every TD process. The host combines
    it with canonical project_root and comp_path; this value alone is not a
    node identity and carries no authority.
    """
    canonical = normalize_toe_path(toe_path, platform=platform)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]
    return NODE_DISCRIMINATOR_PREFIX + digest


def _clean_registration_metadata(metadata):
    if metadata is None:
        return None
    if not isinstance(metadata, dict):
        raise ValueError("registration metadata must be an object")
    unknown = set(metadata) - set(REGISTRATION_METADATA_FIELDS)
    if unknown:
        raise ValueError("unknown registration metadata field(s): %s"
                         % (", ".join(sorted(str(k) for k in unknown)),))
    clean = {}
    for key in REGISTRATION_METADATA_FIELDS:
        if key not in metadata or metadata[key] is None:
            continue
        value = metadata[key]
        if key == "process_id":
            if isinstance(value, bool):
                raise ValueError("process_id must be a positive integer")
            value = int(value)
            if value <= 0:
                raise ValueError("process_id must be a positive integer")
        else:
            value = str(value)
        clean[key] = value
    return clean


def registration_payload(project_root, comp_path, convoy_id,
                         runtime_id, envoy_port=None,
                         node_discriminator=None, metadata=None,
                         envoy_ready=None, wake_port=None, wake_token=None,
                         remote_wake=None, perform_mode=None,
                         wake_active=None, wake_grace_s=None,
                         binding_state=None, td_executable=None,
                         launch_token=None, launch_reservation_id=None):
    """The /register body, built from values ALREADY resolved on the
    main thread.

    runtime_id is not optional here even though the route accepts its
    absence, and that is the sharpest correctness trap in the whole
    registration path: the host does
    `existing["runtime_id"] = runtime_id or mint_runtime_id()`, so a
    heartbeat that omits it re-mints the run identity and invalidates
    every in-flight expected_runtime_id precondition. Making it a
    required argument is the enforcement.

    envoy_port is omitted for ANY falsy value, not just None: the host
    never CLEARS a known port on a re-register that omits one, so a
    pre-Envoy heartbeat cannot wipe a good port -- but port 0 is not a
    port either, and sending it earns a 400 malformed (the host's range
    is 1..65535), turning a transient pre-Envoy tick into a
    refusal-shaped status instead of the pending path. Clearing is
    /unregister's job alone.

    The identity fields are str()-coerced because they arrive from
    the main thread as whatever TD handed over -- a pathlib.Path project
    root, or any object with a __str__ -- and an unserializable value
    would otherwise surface as a request that never leaves the process.
    """
    payload = {
        "project_root": str(project_root),
        "comp_path": str(comp_path),
        "convoy_id": str(convoy_id),
        "runtime_id": str(runtime_id),
    }
    if binding_state is not None:
        if binding_state not in ("candidate", "established"):
            raise ValueError("binding_state must be candidate or established")
        payload["binding_state"] = binding_state
    if td_executable is not None:
        executable = str(td_executable).strip()
        if (not executable or len(executable.encode("utf-8")) > 4096
                or any(ord(char) < 32 or ord(char) == 127
                       for char in executable)):
            raise ValueError("td_executable must be a bounded path")
        payload["td_executable"] = executable
    launch_present = (launch_token is not None
                      or launch_reservation_id is not None)
    if launch_present:
        if launch_token is None or launch_reservation_id is None:
            raise ValueError(
                "launch_token and launch_reservation_id must be paired")
        token = str(launch_token)
        reservation = str(launch_reservation_id)
        alphabet = (
            "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
        if (not 32 <= len(token) <= 256
                or any(char not in alphabet for char in token)):
            raise ValueError("launch_token must be bounded URL-safe text")
        if (not reservation or len(reservation.encode("utf-8")) > 256
                or any(ord(char) < 32 or ord(char) == 127
                       for char in reservation)):
            raise ValueError("launch_reservation_id must be bounded text")
        payload["launch_token"] = token
        payload["launch_reservation_id"] = reservation
    if node_discriminator is not None:
        value = str(node_discriminator)
        if (not value.startswith(NODE_DISCRIMINATOR_PREFIX)
                or len(value) != len(NODE_DISCRIMINATOR_PREFIX) + 32
                or any(c not in "0123456789abcdef"
                       for c in value[len(NODE_DISCRIMINATOR_PREFIX):])):
            raise ValueError("malformed node_discriminator")
        payload["node_discriminator"] = value
    clean_metadata = _clean_registration_metadata(metadata)
    if clean_metadata:
        payload["metadata"] = clean_metadata
    if envoy_port:
        payload["envoy_port"] = int(envoy_port)
    # New runtimes send an explicit readiness bit.  Legacy callers omit it,
    # preserving the old "an absent port does not clear a known endpoint"
    # rule.  ConvoyExt always supplies it, so entering Perform Mode can
    # retire Envoy's old port without abusing port 0 as a sentinel.
    if envoy_ready is not None:
        if not isinstance(envoy_ready, bool):
            raise ValueError("envoy_ready must be boolean")
        if envoy_ready and not payload.get("envoy_port"):
            raise ValueError("envoy_ready requires envoy_port")
        payload["envoy_ready"] = envoy_ready

    pair_present = wake_port is not None or wake_token is not None
    if pair_present:
        if wake_port is None or wake_token is None:
            raise ValueError("wake_port and wake_token must be supplied together")
        if isinstance(wake_port, bool):
            raise ValueError("wake_port must be an integer 1..65535")
        try:
            clean_port = int(wake_port)
        except (TypeError, ValueError, OverflowError):
            raise ValueError("wake_port must be an integer 1..65535")
        if clean_port < 1 or clean_port > 65535:
            raise ValueError("wake_port must be an integer 1..65535")
        clean_token = str(wake_token)
        if (not WAKE_TOKEN_MIN_CHARS <= len(clean_token)
                <= WAKE_TOKEN_MAX_CHARS
                or any(ch not in
                       "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
                       for ch in clean_token)):
            raise ValueError("wake_token must be bounded URL-safe text")
        payload["wake_port"] = clean_port
        payload["wake_token"] = clean_token

    for name, value in (("remote_wake", remote_wake),
                        ("perform_mode", perform_mode),
                        ("wake_active", wake_active)):
        if value is None:
            continue
        if not isinstance(value, bool):
            raise ValueError("%s must be boolean" % name)
        payload[name] = value
    if wake_grace_s is not None:
        if isinstance(wake_grace_s, bool):
            raise ValueError("wake_grace_s must be an integer")
        try:
            clean_grace = int(wake_grace_s)
        except (TypeError, ValueError, OverflowError):
            raise ValueError("wake_grace_s must be an integer")
        if (clean_grace != wake_grace_s or clean_grace < 0
                or clean_grace > WAKE_GRACE_MAX_S):
            raise ValueError("wake_grace_s is outside the supported range")
        payload["wake_grace_s"] = clean_grace
    if remote_wake is True and not pair_present:
        # Listener startup is asynchronous; advertising intent before the
        # endpoint is ready is valid and lets status explain the transition.
        payload["wake_pending"] = True
    return payload


def register(handle, payload, opener=None, timeout=REGISTER_TIMEOUT_S):
    """POST /register. Returns a result dict for status_text; never
    raises, so the worker cannot die on a network hiccup."""
    status, answer = host_post(handle, "/register", payload,
                               timeout=timeout, opener=opener)
    result = _answer_state(status, answer)
    if result is not None:
        return result
    authoritative_id = answer.get("convoy_id", payload.get("convoy_id"))
    if (not isinstance(authoritative_id, str) or not authoritative_id
            or authoritative_id != authoritative_id.strip()):
        return {"state": STATE_HOST_ERROR, "http_status": status,
                "reason": "host_bad_response",
                "detail": "registration response has an invalid convoy_id"}
    try:
        raw_id = authoritative_id.encode("utf-8")
    except UnicodeEncodeError:
        raw_id = b""
    if (not raw_id or len(raw_id) > 128
            or any(byte < 0x20 or byte == 0x7f for byte in raw_id)):
        return {"state": STATE_HOST_ERROR, "http_status": status,
                "reason": "host_bad_response",
                "detail": "registration response has an invalid convoy_id"}
    realm_state = answer.get(
        "realm_state", payload.get("binding_state", "established"))
    if realm_state not in ("candidate", "established"):
        return {"state": STATE_HOST_ERROR, "http_status": status,
                "reason": "host_bad_response",
                "detail": "registration response has an invalid realm_state"}
    return {"state": STATE_REGISTERED,
            "http_status": status,
            "node_id": answer.get("node_id"),
            "host_id": answer.get("host_id") or handle.host_id,
            # The daemon's own account of the code it RUNS (never
            # installed.json, which lies exactly when an update failed to
            # restart the process). None means a pre-6.0.213 daemon that
            # cannot say -- which is itself the out-of-date signal.
            "host_app_version": (str(answer["app_version"])
                                 if answer.get("app_version") else None),
            "convoy_id": authoritative_id,
            "realm_state": realm_state,
            "runtime_id": answer.get("runtime_id"),
            "envoy_port": answer.get("envoy_port"),
            "perform_mode": answer.get("perform_mode") is True,
            "wake_active": answer.get("wake_active") is True,
            "wake_ready": answer.get("wake_ready") is True,
            "td_python_approved": bool(answer.get("td_python_approved")),
            "policy": _clean_policy(answer.get("policy"))}


def unregister(handle, node_id, runtime_id=None, reason="disabled",
               opener=None, timeout=UNREGISTER_TIMEOUT_S):
    """POST /unregister -- clear this node's Envoy port on the way out.

    Best-effort by contract: one attempt, short timeout, every outcome a
    result dict. A hard kill leaves the port stale anyway (the
    dispatcher's backoff handles that); this makes the COMMON exit
    clean, which is all a client can promise.

    runtime_id is the ownership proof and should always be passed: two
    TD sessions on ONE project folder share a node_id, so without it a
    departing instance zeroes a SURVIVING instance's live port. The host
    no-ops when it does not match.

    reason is an intent, not prose. ``disabled`` withdraws this node's
    membership; ``shutdown`` only clears the current runtime while keeping
    its enabled membership intent for normal offline/reconnect behavior.
    Anything else is refused locally before a token or request leaves TD.
    """
    if reason not in UNREGISTER_REASONS:
        return {
            "state": STATE_ERROR,
            "reason": "invalid_unregister_reason",
            "detail": "reason must be one of: %s"
                      % (", ".join(UNREGISTER_REASONS),),
        }
    body = {"node_id": node_id, "reason": reason}
    if runtime_id:
        body["runtime_id"] = runtime_id
    status, answer = host_post(handle, "/unregister", body,
                               timeout=timeout, opener=opener)
    result = _answer_state(status, answer)
    if result is not None:
        # "The node is already gone" is exactly what an unregister
        # WANTS. A host that restarted with a fresh state dir since we
        # registered answers 404 unknown_node, and reporting that as
        # 'Refused: unknown_node' on a normal disable would be alarming
        # noise about a completed outcome.
        if (result["state"] == STATE_REFUSED
                and result.get("reason") == "unknown_node"):
            return {"state": STATE_UNREGISTERED, "node_id": node_id,
                    "already_gone": True}
        return result
    return {"state": STATE_UNREGISTERED,
            "node_id": answer.get("node_id") or node_id,
            "cleared": answer.get("cleared", True)}


def _answer_state(status, answer):
    """The non-success readings of a host answer, or None when the
    answer is a clean success and the caller should shape its own."""
    if status is None:
        return {"state": STATE_UNREACHABLE,
                "detail": "the host app did not answer"}
    if not isinstance(answer, dict):
        return {"state": STATE_ERROR, "http_status": status,
                "detail": "host answered with %s, not an object"
                          % (type(answer).__name__,)}
    if answer.get("ok") is not True:
        reason = str(answer.get("reason") or "unknown")
        detail = str(answer.get("detail") or "")
        # 5xx is the host FAILING, not the host refusing, and the two
        # demand opposite handling: a transient persist_failed (which
        # rolls the registration back host-side) must be retried, while
        # a 409 node_identity_conflict must not be. Status 0 is our own
        # unserializable request -- equally a bug, not a policy answer.
        if status == 0 or status >= 500 or reason in NOT_A_REFUSAL:
            return {"state": STATE_HOST_ERROR, "http_status": status,
                    "reason": reason, "detail": detail}
        return {"state": STATE_REFUSED, "http_status": status,
                "reason": reason, "detail": detail}
    return None


# -- per-launch run identity -------------------------------------------

_PROCESS_EXECUTABLE = {"cached": False, "path": None}


def process_executable():
    """This process's REAL executable image, for td_executable claims.

    Inside TouchDesigner sys.executable names the bundled bin/python.exe,
    not the running TouchDesigner.exe, so a register that sent it could
    never match the host inspector's process probe: every launch profile
    died runtime_unverifiable and the whole fleet read
    remotely_launchable:false (field 2026-08-19). Mirror the inspector's
    own probes instead -- GetModuleFileNameW on win32, proc_pidpath on
    macOS -- falling back to sys.executable only when a probe fails.
    Cached per process: the image cannot change while we run.
    """
    cache = _PROCESS_EXECUTABLE
    if cache["cached"]:
        return cache["path"]
    path = None
    try:
        if sys.platform == "win32":
            import ctypes
            buf = ctypes.create_unicode_buffer(32768)
            if ctypes.windll.kernel32.GetModuleFileNameW(None, buf, 32768):
                path = buf.value or None
        elif sys.platform == "darwin":
            import ctypes
            libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
            buf = ctypes.create_string_buffer(4096)
            if libproc.proc_pidpath(os.getpid(), buf, len(buf)) > 0:
                path = os.fsdecode(buf.value) or None
    except Exception:
        path = None
    if not path:
        try:
            path = sys.executable or None
        except Exception:
            path = None
    cache["path"] = path
    cache["cached"] = True
    return path


def mint_runtime_id():
    """A fresh per-launch identifier, in convoy_identity's format.

    Never persisted, by design: its whole job is to change on every TD
    start so a request aimed at the previous run can be refused.
    """
    return "rt_" + secrets.token_hex(8)


def ensure_runtime_id(store, key):
    """The runtime_id for one COMP, minted AT MOST ONCE per process.

    `store` is that lifetime, handed in rather than chosen here, because
    both obvious choices are wrong in opposite directions: an instance
    attribute is re-minted on every extension reinit (Ctrl+S is not a
    new run, but it would mint a new identity), and COMP storage is
    saved into the .toe and would outlive the process (a restart really
    IS a new run and must mint). The caller passes a plain dict living
    on a `sys` attribute -- survives reinit, dies with the process.
    """
    existing = store.get(key)
    if isinstance(existing, str) and existing:
        return existing
    fresh = mint_runtime_id()
    store[key] = fresh
    return fresh


# -- backoff and status text -------------------------------------------

def backoff_delay(attempt, rng=None):
    """Seconds to wait before retry number `attempt` (0-based).

    Doubling from 5 s to a 60 s cap, then +/-25% jitter so a fleet of
    nodes recovering from ONE host-app restart does not retry in
    lockstep. Never returns more than the cap, and never less than
    three quarters of the base -- a retry storm and a busy-loop are both
    failures.
    """
    try:
        attempt = int(attempt)
    except (TypeError, ValueError):
        attempt = 0
    # Clamped before the shift: a runaway counter must not build a
    # thousand-bit integer just to be thrown away by min().
    attempt = max(0, min(attempt, 32))
    rng = rng or random.random
    step = min(BACKOFF_BASE_S * (2 ** attempt), BACKOFF_CAP_S)
    low = step * (1.0 - BACKOFF_JITTER)
    high = min(step * (1.0 + BACKOFF_JITTER), BACKOFF_CAP_S)
    return low + (high - low) * rng()


def _short(value, width=8):
    text = str(value or "")
    return text[:width] if text else "?"


def status_text(result):
    """The Convoystatus string for one outcome. TOTAL: every reachable
    state has a string, and it never raises.

    ABSENCE IS NOT AN ERROR. absent / stale / unreachable are the normal
    state of a machine with no host app running, and they must read as
    such -- an Error status there would train the user to ignore the
    field, and would be a lie besides.
    """
    if not isinstance(result, dict):
        return "Error: no result"
    state = result.get("state")

    # BOTH vocabularies land here. probe() answers in STATUS_* and
    # register() in STATE_*, and Stage B's tick reports whichever it
    # last computed -- so a probe outcome must map too. Two of the three
    # STATUS_* values happen to equal their STATE_* twins (absent,
    # stale), which is exactly why the third silently fell through to
    # "Error: unexpected state 'running'" -- the healthiest state in the
    # system reported as a failure.
    if state == STATUS_RUNNING:
        return "Host app found"

    if state == STATE_DISABLED:
        return "Disabled"
    if state == STATE_UNSAVED:
        return "Waiting for project save"
    if state in (STATE_ABSENT, STATE_UNREACHABLE):
        # A host app that was never there and one that vanished mid-call
        # are the same fact to a user: there is nothing to talk to.
        return "No Convoy host app"
    if state == STATE_STALE:
        return "Host app stale"
    if state == STATE_REGISTERING:
        return "Registering..."
    if state == STATE_REGISTERED:
        if not result.get("envoy_port"):
            # Registered, but the host cannot dispatch back yet. Named
            # separately because it is a real, temporary, non-error
            # state -- Envoy binds after the first tick.
            return "Registered -- Envoy port pending"
        # Plain language: two truncated hashes told a user nothing they
        # could act on. The ids live in the log and in convoy_list_nodes
        # for anyone debugging.
        return "Connected"
    if state == STATE_UNREGISTERED:
        # The only paths that unregister are disable and exit; the
        # resting state after either one is Disabled.
        return "Disabled"
    if state == STATE_REFUSED:
        return "Refused: %s" % (result.get("reason") or "unknown",)
    if state == STATE_HOST_ERROR:
        # A host-side crash is an Error, not a Refusal: 'Refused:
        # internal_error' reads as a decision the host made about us.
        return "Error: %s" % (str(result.get("reason") or "unknown")[:80],)
    if state == STATE_ERROR:
        return "Error: %s" % (str(result.get("detail") or "unknown")[:80],)
    return "Error: unexpected state %r" % (state,)


def host_status_text(state):
    """The Convoy Host string for one host-app state. TOTAL, never raises.

    THE SINGLE SOURCE of that field's vocabulary -- fourteen strings, and
    no fifteenth. `state` is either convoy_install.host_state()'s dict
    or a bare transient name ('checking', 'installing', 'repairing',
    'starting', 'install_failed'), because the extension needs to say
    what it is doing before there is anything on disk to describe.

    ASCII ONLY, deliberately: '--' and '...' are the repo rule, and this
    string is written into a TD parameter that a Windows textport may
    render through a legacy codepage.

    TWO PLACES THIS DEPARTS FROM A LITERAL READING OF THE PLAN'S LIST,
    both so the field cannot state something false:

      - newer_install reads 'Running X -- installed by a newer Embody'
        only when a host app ACTUALLY ANSWERED /health. When it is
        installed-but-silent the lead word is 'Installed', because
        printing 'Running' over a dead daemon is the one failure mode
        this whole status field exists to prevent.
      - an UNRECOGNISED state falls back to 'Install failed -- see log'.
        A total function needs a default, and that is the only string in
        the vocabulary that points the user somewhere useful; reaching
        it at all is a bug in this file, which the log will show.
    """
    if isinstance(state, str):
        state = {"state": state}
    if not isinstance(state, dict):
        return "Install failed -- see log"
    name = str(state.get("state") or "")
    version = str(state.get("installed_version") or "")

    if name == HOST_NOT_INSTALLED:
        return "Not installed"
    if name == HOST_CHECKING:
        return "Checking..."
    if name == HOST_INSTALLING:
        return "Installing..."
    if name == HOST_REPAIRING:
        return "Repairing runtime..."
    if name == HOST_STARTING:
        return "Installed -- starting..."
    if name == HOST_RUNNING:
        return _running_text(version, state.get("pid"))
    if name == HOST_NOT_RUNNING:
        return "Installed -- not running (restarts within a minute)"
    if name == HOST_STOPPED:
        return "Installed -- stopped"
    if name == HOST_NO_SUPERVISOR:
        return "Installed -- no supervisor (use Repair Convoy App)"
    if name == HOST_NEEDS_REPAIR_PYTHON:
        return "Needs repair -- Python not found (reinstall)"
    if name == HOST_NEWER_INSTALL:
        lead = "Running" if state.get("live") else "Installed"
        return "%s %s -- installed by a newer Embody" % (
            lead, version or "?")
    if name == HOST_EXTERNAL_SUPERVISOR:
        return "Managed by another supervisor"
    if name == HOST_STALE_PAYLOAD:
        running = str(state.get("reported_version") or "")
        return ("Needs repair -- still running %s, not %s (use Repair "
                "Convoy App)" % (running or "older code", version or "?"))
    return "Install failed -- see log"


def _running_text(version, pid):
    """'Running X (pid N)', degrading honestly when either is unknown.

    A missing pid is normal, not exceptional: /health does not report
    one and the supervisor query is the only source, so a status
    refreshed without a supervisor query must still be able to say
    'Running'.
    """
    try:
        pid = int(pid) if pid else None
    except (TypeError, ValueError):
        pid = None
    text = "Running %s" % (version,) if version else "Running"
    if pid:
        text += " (pid %s)" % (pid,)
    return text
