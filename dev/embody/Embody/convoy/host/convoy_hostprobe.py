"""Finding and talking to the local host app -- client side (Phase 1).

Used by the bridge and by ConvoyExt to answer one question: is a Convoy
host app running on this machine, and if so, where? Pure stdlib.

THE FALLBACK CONTRACT is the whole point (12.3: "Retain direct loopback
Envoy behavior when Convoy is disabled/unavailable"). A client calls
probe() and gets one of exactly three outcomes:

  running   -- a live host app, with its port and token. Use Convoy.
  absent    -- no host app (no portfile, or its writer is dead). Fall
               straight back to today's direct-loopback Envoy. This is
               NOT an error: Convoy off is a normal, supported state.
  stale     -- a portfile exists but points at a dead process. Same
               action as absent (fall back), but distinguished so a
               client can log "the host app went away" rather than
               "never present".

The critical safety property: probe() NEVER returns a port whose writer
is not alive, because it reads through read_live_portfile (which verifies
the pid). A dead port must never be handed out as live -- that was a real
bug in the host app's own portfile handling, fixed there; a client must
not reintroduce it by reading the raw file.
"""

import json
import urllib.error
import urllib.request

import convoy_platform as platform_mod

STATUS_RUNNING = "running"
STATUS_ABSENT = "absent"
STATUS_STALE = "stale"


class HostHandle:
    """A located, live host app: where it is and how to authenticate."""

    def __init__(self, port, host_id, token, data_dir):
        self.port = port
        self.host_id = host_id
        self.token = token
        self.data_dir = data_dir

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.port}"


class ProbeResult:
    def __init__(self, status, handle=None, detail=""):
        self.status = status
        self.handle = handle
        self.detail = detail

    @property
    def use_convoy(self):
        """True only when a live host app is present. Every other outcome
        means: behave exactly as Embody does today, direct to loopback
        Envoy."""
        return self.status == STATUS_RUNNING


def probe(data_dir=None, token_reader=None, portfile_reader=None,
          raw_portfile_reader=None, health_check=None):
    """Locate the local host app. Returns a ProbeResult, never raises.

    token_reader / portfile_reader are injected so the whole decision
    tree is testable without a filesystem; the defaults read the real
    per-user data dir.
    """
    data_dir = data_dir or platform_mod.data_dir()
    read_live = portfile_reader or platform_mod.read_live_portfile
    read_token = token_reader or _read_token

    # read_live_portfile verifies the writer is alive; a portfile whose
    # process is gone comes back as None here, exactly like no portfile.
    raw_reader = raw_portfile_reader or platform_mod.read_portfile
    live = read_live(data_dir)
    if live is None:
        # Distinguish "there was a portfile but its writer is dead" from
        # "no portfile at all" -- same action, clearer logs. Uses the
        # injected raw reader so the decision tree is fully isolated from
        # the filesystem, as the docstring promises.
        if raw_reader(data_dir) is not None:
            return ProbeResult(STATUS_STALE,
                               detail="host app portfile is stale (writer "
                                      "not alive); falling back to direct "
                                      "Envoy")
        return ProbeResult(STATUS_ABSENT,
                           detail="no Convoy host app on this machine; "
                                  "using direct Envoy")

    token = read_token(data_dir)
    if not token:
        # A live host app we cannot authenticate to is unusable; fall
        # back rather than send unauthenticated traffic that will 401.
        return ProbeResult(STATUS_ABSENT,
                           detail="host app is up but its IPC token is "
                                  "unreadable; using direct Envoy")

    handle = HostHandle(port=live["port"], host_id=live.get("host_id"),
                        token=token, data_dir=data_dir)

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
                   "/health; falling back to direct Envoy")
    if handle.host_id and confirmed_id != handle.host_id:
        return ProbeResult(
            STATUS_STALE,
            detail=f"the process on the host port identifies as "
                   f"{confirmed_id!r}, not the expected {handle.host_id!r} "
                   f"-- likely a recycled port; falling back")
    handle.host_id = confirmed_id
    return ProbeResult(STATUS_RUNNING, handle=handle)


def _default_health_check(handle):
    """GET /health (UNAUTHENTICATED -- it is the one open route) and
    return the reported host_id, or None if unreachable. Deliberately
    does NOT send the token: identity is confirmed BEFORE the token is
    ever transmitted."""
    import urllib.request
    try:
        req = urllib.request.Request(handle.base_url + "/health")
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data.get("host_id")
    except Exception:
        return None


def _read_token(data_dir):
    """The per-install IPC token, or None if unreadable.

    ValueError is caught alongside OSError because UnicodeDecodeError IS
    a ValueError: a host.token that is not UTF-8 would otherwise raise
    straight through probe() and break its "never raises" contract.
    read_portfile and ensure_ipc_token already catch both; this was the
    one reader that did not.
    """
    import os
    try:
        with open(os.path.join(data_dir, platform_mod.TOKEN_FILE),
                  "r", encoding="utf-8") as f:
            token = f.read().strip()
        return token or None
    except (OSError, ValueError):
        return None


def host_get(handle, path, timeout=5, opener=None):
    """Authenticated GET against the host app. Returns the parsed JSON,
    or None on any transport failure (the caller falls back)."""
    return _host_call(handle, "GET", path, None, timeout, opener)


def host_post(handle, path, body, timeout=10, opener=None):
    return _host_call(handle, "POST", path, body, timeout, opener)


def _host_call(handle, method, path, body, timeout, opener):
    opener = opener or urllib.request.urlopen
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        handle.base_url + path, data=data, method=method,
        headers={"Content-Type": "application/json",
                 "X-Convoy-Host-Token": handle.token})
    try:
        with opener(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # A structured refusal (401/404/409) is a real answer, not a
        # transport failure -- surface its body so the caller can act.
        try:
            return json.loads(e.read().decode("utf-8"))
        except (ValueError, OSError):
            return {"ok": False, "reason": "host_http_error",
                    "detail": f"HTTP {e.code}"}
    except (urllib.error.URLError, OSError, ValueError):
        return None         # transport gone -> caller falls back
