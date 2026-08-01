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

import json
import ntpath
import os
import posixpath
import random
import secrets
import sys
import urllib.error
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

# Transport failures on /register back off 5 s -> 60 s, jittered.
BACKOFF_BASE_S = 5.0
BACKOFF_CAP_S = 60.0
BACKOFF_JITTER = 0.25

# Reasons that are NEVER a policy decision by the host, whatever status
# they arrive with: the request never left this process, or the answer
# was not an answer. Both are Errors, not Refusals -- "Refused:
# host_bad_response" would read as a decision the host made about us.
NOT_A_REFUSAL = ("unserializable_request", "host_bad_response")

HEALTH_TIMEOUT_S = 3.0
REGISTER_TIMEOUT_S = 10.0
# Unregister is best-effort on the way out the door: one attempt, and a
# short one. A shutting-down session must never block on a host app that
# is itself going away.
UNREGISTER_TIMEOUT_S = 1.0


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
        data = None if body is None else json.dumps(body).encode("utf-8")
    except (TypeError, ValueError) as e:
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
            raw = resp.read()
    except urllib.error.HTTPError as e:
        # A structured refusal (400/401/404/409) is a real answer, not a
        # transport failure -- surface its body so the caller can act.
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except (ValueError, OSError):
            return e.code, {"ok": False, "reason": "host_http_error",
                            "detail": "HTTP %s" % (e.code,)}
    except (urllib.error.URLError, OSError):
        return None, None       # transport gone -> caller backs off
    try:
        return status, json.loads(raw.decode("utf-8"))
    except (ValueError, AttributeError):
        # A host that answered 200 with a body we cannot parse IS up and
        # reachable. Reporting that as absence would hide a live, broken
        # host behind "No Convoy host app" and hand it the transport
        # backoff instead of a diagnosable error.
        return status, {"ok": False, "reason": "host_bad_response",
                        "detail": "unparseable body from %s" % (path,)}


def registration_payload(project_root, comp_path, convoy_id,
                         runtime_id, envoy_port=None):
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

    The three identity fields are str()-coerced because they arrive from
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
    if envoy_port:
        payload["envoy_port"] = int(envoy_port)
    return payload


def register(handle, payload, opener=None, timeout=REGISTER_TIMEOUT_S):
    """POST /register. Returns a result dict for status_text; never
    raises, so the worker cannot die on a network hiccup."""
    status, answer = host_post(handle, "/register", payload,
                               timeout=timeout, opener=opener)
    result = _answer_state(status, answer)
    if result is not None:
        return result
    return {"state": STATE_REGISTERED,
            "http_status": status,
            "node_id": answer.get("node_id"),
            "host_id": answer.get("host_id") or handle.host_id,
            "runtime_id": answer.get("runtime_id"),
            "envoy_port": answer.get("envoy_port"),
            "td_python_approved": bool(answer.get("td_python_approved"))}


def unregister(handle, node_id, runtime_id=None, opener=None,
               timeout=UNREGISTER_TIMEOUT_S):
    """POST /unregister -- clear this node's Envoy port on the way out.

    Best-effort by contract: one attempt, short timeout, every outcome a
    result dict. A hard kill leaves the port stale anyway (the
    dispatcher's backoff handles that); this makes the COMMON exit
    clean, which is all a client can promise.

    runtime_id is the ownership proof and should always be passed: two
    TD sessions on ONE project folder share a node_id, so without it a
    departing instance zeroes a SURVIVING instance's live port. The host
    no-ops when it does not match.
    """
    body = {"node_id": node_id}
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
        return "Registered %s (host %s)" % (_short(result.get("node_id")),
                                            _short(result.get("host_id")))
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
