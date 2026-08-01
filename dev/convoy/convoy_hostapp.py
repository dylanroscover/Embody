"""embody-convoy host app -- Phase 1 local skeleton.

One per machine, outside TD, LOOPBACK ONLY in Phase 1: LAN transport is
Phase 3 (with real identity/TLS); nothing here binds off-box, so no
firewall rule is needed yet (D-6). Development entry point per 12.2
("may start as a Python entry point"); packaging/signing is the Phase 1
spike, supervision is A-36's exactly-one-supervisor.

What it owns TODAY (the Phase 1 exit slice):
  - host identity (host_id minted once, stored host-private),
  - the node registry: TD runtimes register with their project-side
    anchor and get their host-minted node_id back (A-12),
  - durable jobs: persist-before-acknowledge, idempotent create, state
    survives host restart,
  - authenticated local IPC: every request presents the per-install
    token; a wrong/missing token is refused before ANY state is touched,
  - the audit trail (A-40),
  - THE GUARDED REQUEST PATH: a signed Convoy/1 envelope submitted to
    /envelope is verified (convoy_protocol), gated by the operation
    registry (A-1 executability), authorized against leases
    (convoy_controllers), and only then becomes a durable job. The
    capability manifest (convoy_capabilities, A-23) is served on
    /manifest so a controller can refuse-before-send.

What it deliberately does NOT do yet: LAN anything, discovery, peers,
relay, TD launch, artifacts. Those phases build ON this file's contracts
rather than amending them.
"""

import argparse
import copy
import hmac
import json
import math
import os
import re
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import convoy_capabilities as capabilities
import convoy_controllers as controllers
import convoy_hoststore as hoststore
import convoy_identity as identity
import convoy_mcpclient as mcpclient
import convoy_platform as platform_mod
import convoy_protocol as protocol

MAX_BODY_BYTES = 1 * 1024 * 1024
TOKEN_HEADER = "X-Convoy-Host-Token"

# -- async node jobs (the polling slice) ------------------------------
#
# Some node operations do not RETURN a result -- they mint a node-side
# job and return its HANDLE (run_tests background=True, save_project).
# The host mirrors that handle as 'running' and then POLLS the node for
# the outcome, so a 20-minute test run never sits inside a 30s forward.
#
# The one operation the poller calls, hardcoded: read-only, worker-side
# on the node (it answers while TD's main thread is blocked), and the
# only argument it takes is an id the node itself minted.
POLL_OPERATION = "get_job_status"
# Mirrors EnvoyExt's own job-id validation (_job_path). A node-supplied
# id is UNTRUSTED input that we hand straight back to the node and store
# in a durable record -- validate its shape before either.
NODE_JOB_ID_RE = re.compile(r"^job_[0-9a-f]{8}\Z")
# The node's status vocabulary, read from the store's mapping so the two
# can never drift apart (the store is what translates them to states).
NODE_JOB_STATUSES = tuple(hoststore._NODE_STATUS_TO_STATE)
# A node result rides into a durable job file and back out of every
# /jobs response. A test-run summary is small; a pathological one is
# not, and nothing else bounds it.
MAX_RESULT_BYTES = 64 * 1024
# Before a poll may conclude the node FORGOT a job (which terminalises
# it as indeterminate), the unknown answer must repeat and outlast a
# grace window. A node restarting between flushes, the 24h retention,
# and the transient dark-job layer all produce one-off unknowns.
POLL_UNKNOWN_MIN_OBSERVATIONS = 3
POLL_UNKNOWN_GRACE_S = 60.0
# The node's "I have no such job" answer, specifically (EnvoyExt
# get_job_status: {'error': "no job with id 'job_x'", 'jobs': [...]}).
# The unknown-job path is the ONE place a read turns into a terminal,
# precious record, so it may not fire on just any error payload: a
# node answering {'error': 'Job records unavailable'} is reporting its
# OWN trouble, not the job's absence, and terminalising on it would put
# a claim in the record the node never made.
_NODE_UNKNOWN_JOB_RE = re.compile(r"no job with id", re.IGNORECASE)
# A running mirror is rewritten at most this often. Every successful
# running-poll used to rewrite the whole job file for a record whose
# state and provenance had not changed -- measured at 6-38 ms each, 200
# atomic rewrites per backoff window at 200 running jobs, forever. The
# durable record may lag its last observation by this much; nothing
# reads observed_at for a decision, and a real change (state, handle,
# stale, terminal) always writes immediately.
POLL_MIRROR_REFRESH_S = 300.0

# The Phase 1 SEED of the operation registry. The canonical registry --
# every Envoy operation audited for executability -- is later work (A-1);
# until it lands, this host app relays NOTHING that is not explicitly
# entered here. The seed is the three operations the Phase 0.5 tracer
# proved end to end, plus one benign mutation so the lease gate is
# exercised on the live path.
#
# EVERY gating field is read with a STRICT default (see _GATING_DEFAULTS):
# an entry that omits one is treated as unaudited, which A-1 defines as
# executes_arbitrary_code=True -- i.e. refused. Absence from the registry
# and absence of a field must fail the same way; reading a missing field
# as False would invert A-1 for exactly the entries nobody audited.
#
# Field meanings:
#   schema       -- digest material for A-23 (shape documentation; not
#                   yet enforced argument validation),
#   mutating     -- drives the lease reader/writer gate (A-17),
#   executes_arbitrary_code -- A-1; True is REFUSED outright in Phase 1
#                   because the TD-Python gate does not exist yet,
#   runtime_required -- A-22's expected_runtime_id-mandatory classes
#                   (restart, read-modify-write, exclusive batch, wake).
PHASE1_OPERATIONS = {
    "convoy_ping": {
        "schema": {},
        "mutating": False,
        "executes_arbitrary_code": False,
        "remote_exposed": True,    # X0 liveness
        "runtime_required": False,
        "side_effects": {},
    },
    "query_network": {
        "schema": {"parent_path": "string", "recursive": "bool?"},
        "mutating": False,
        "executes_arbitrary_code": False,
        "remote_exposed": True,    # X0 read
        "runtime_required": False,
        "side_effects": {},
    },
    "capture_top": {
        "schema": {"op_path": "string", "format": "string?"},
        "mutating": False,
        "executes_arbitrary_code": False,
        "remote_exposed": True,    # X0 read (confidentiality, not executability)
        "runtime_required": False,
        "side_effects": {"cooks": True},
    },
    "set_op_position": {
        "schema": {"op_path": "string", "x": "number?", "y": "number?"},
        "mutating": True,
        "executes_arbitrary_code": False,
        "remote_exposed": True,    # X5 layout nudge -- refused under observe-only
        "runtime_required": False,
        "side_effects": {"layout": True},
    },
    # The two ASYNC entries. `async_job` is what tells the dispatcher
    # this operation answers with a HANDLE rather than a result:
    #   kind     -- the node job kind, for the audit trail,
    #   key_arg  -- the argument carrying our idempotency key, which is
    #               the anchor the node's own idempotency index (16.5)
    #               uses to hand a retry back the ORIGINAL run,
    #   inject   -- arguments host policy forces, overriding the caller.
    "run_tests": {
        "schema": {"suite_name": "string?", "test_name": "string?"},
        # Not "runs a pure read": a run flips Embody's Status and
        # Filecleanup, creates and destroys sandbox operators, writes
        # dev/logs, and can restart the Envoy server under itself.
        "mutating": True,
        # A-1, stated precisely, because the earlier wording here was
        # WRONG and a LAN listener would have made it dangerous: this is
        # not caller-supplied code, but it is not "the code already in
        # the project" either. TestRunnerExt._discoverTestSuites scans
        # unit_tests/ and exec_module's every test_*.py AND test_*.txt it
        # FINDS ON DISK. That is only equivalent to project-own code
        # while nothing can put a file there -- a LOOPBACK assumption.
        # It holds today (loopback + a 0600 IPC token means "remote
        # caller" does not exist) and it dissolves the moment a socket
        # binds off-box. So the flag stays False (the LOCAL path must
        # keep working -- it is the owner running their own tests), and
        # the boundary is drawn by remote_exposed below instead.
        "executes_arbitrary_code": False,
        # A-1 / R-2: NEVER relayable to a remote peer. Phase 3 reads
        # this; nothing reads it today, which is exactly why it is being
        # written now -- the data is correct before the code that could
        # arm it exists.
        "remote_exposed": False,
        "runtime_required": True,       # A-22 exclusive-batch class
        "side_effects": {"runs_tests": True, "writes_logs": True,
                         "may_restart_server": True},
        "async_job": {"kind": "run_tests", "key_arg": "idempotency_key",
                      # The caller fields run_tests actually accepts;
                      # anything else is dropped rather than forwarded
                      # for the node to reject.
                      "caller_args": ("suite_name", "test_name"),
                      # background True is not optional: False would
                      # block the forward for the whole run and
                      # manufacture an indeterminate out of a healthy
                      # test pass. override False is fail-closed -- the
                      # host must never bypass the NODE's own
                      # multi-session destructive gate.
                      "inject": {"background": True, "override": False}},
    },
    "save_project": {
        "schema": {},
        "mutating": True,               # writes the .toe + release .tox
        "executes_arbitrary_code": False,
        # NOT relayable to a remote peer: it blocks TD's main thread for
        # 15+ seconds and restarts the Envoy server under itself. A-30's
        # performance guard and A-31's per-node remote-work policy -- the
        # machinery whose whole job is to stop a remote peer wrecking a
        # live output -- are Phase 4. Letting a peer freeze a show
        # machine before show protection exists is precisely what A-30
        # was written to prevent. Returns to the remote surface in Phase
        # 4, gated, never ungated.
        "remote_exposed": False,
        "runtime_required": True,       # read-modify-write
        "side_effects": {"writes_toe": True, "blocks_main_thread": True,
                         "restarts_server": True},
        # save_project's MCP signature accepts idempotency_key ONLY, so
        # any extra argument is a validation error at the node -- a
        # failure the host would have invented. An empty `inject`
        # overrides nothing, so the empty `caller_args` is what actually
        # delivers that: NO caller argument rides, and the key is the
        # only thing on the wire.
        "async_job": {"kind": "save_project", "key_arg": "idempotency_key",
                      "caller_args": (), "inject": {}},
    },
}

# The strict reading of a registry entry: anything a registry entry does
# not say is assumed to be the most dangerous answer. `executes_arbitrary
# _code` True is A-1 verbatim; `mutating` True demands the exclusive
# lease; `runtime_required` True demands the A-22 precondition;
# `remote_exposed` FALSE means an operation nobody audited for the LAN is
# not reachable from it -- absence is a refusal, exactly like absence
# from the registry itself.
_GATING_DEFAULTS = {
    "executes_arbitrary_code": True,
    "mutating": True,
    "runtime_required": True,
    "remote_exposed": False,
}


def gating_of(entry):
    """The gating triple actually enforced for a registry entry, strict
    defaults applied. ONE reader, so the enforced value and the value in
    the capability digest can never drift apart."""
    return {field: bool(entry.get(field, default))
            for field, default in _GATING_DEFAULTS.items()}


# HTTP status per EnvelopeRejected/LeaseError reason. The STRUCTURED
# reason is the contract; the HTTP code is a coarse class: 4xx malformed,
# 403 authentication/namespace, 409 state conflict, 410 expired.
_REFUSAL_HTTP = {
    "bad_signature": 403,
    "arguments_tampered": 403,
    "namespace_mismatch": 403,
    "algorithm_mismatch": 403,
    "malformed": 400,
    "runtime_id_required": 400,
    "wrong_target": 409,
    "runtime_changed": 409,
    "runtime_unverifiable": 409,
    "hop_limit_exceeded": 409,
    "node_leased": 409,
    "shared_lease_no_mutation": 409,
    "deadline_exceeded": 410,
}

# Bounds for caller-supplied text. Ids are host-minted 32-hex or
# controller-chosen labels; nothing legitimate approaches these.
MAX_ID_CHARS = 128
MAX_OPERATION_CHARS = 128

# Which summary bucket a per-job refusal reason falls in. A DICT, not a
# chain of `in (...)` tuples: as tuples, a refusal reason added later
# silently landed in `errors` and the pass summary quietly lied about
# what happened. Anything not named here is counted as an error on
# purpose -- an unbucketed reason is a bug, and it must be visible.
_DRAIN_BUCKET = {
    "node_unreachable": "unreachable",
    "claim_lost": "unreachable",
    "no_node_job_handle": "no_handle",
    # A stored-arguments defect: permanent, not paced -- an anomaly the
    # summary must show rather than bury in 'deferred'.
    "malformed_arguments": "errors",
    "node_endpoint_unknown": "deferred",
    "runtime_changed": "deferred",
    "operation_not_exposed": "deferred",
    "operation_not_relayable": "deferred",
    "store_unavailable": "errors",
}

# The same, for a poll pass. 'unreachable' covers BOTH ways a poll can
# fail to observe the node (refused connection, no usable answer): each
# leaves the job running and teaches nothing, so they are one bucket.
# A running job with no node provenance is an anomaly, not a pacing
# state -- it counts as an error so it cannot hide.
_POLL_BUCKET = {
    "node_unreachable": "unreachable",
    "poll_no_response": "unreachable",
    # The node answered but does not (yet) know the job: still an
    # unobserved job, and the grace window has not elapsed.
    "node_forgot_job_pending": "unreachable",
    "node_endpoint_unknown": "deferred",
    "poll_in_flight": "skipped",
    "no_node_provenance": "errors",
    "poll_id_mismatch": "errors",
    "unknown_job": "errors",
    "unknown_node": "errors",
    "poll_recording_failed": "errors",
    "malformed": "errors",
}

# What a COMPLETED poll's resulting job state counts as.
_POLL_STATE_BUCKET = {"succeeded": "finished", "failed": "failed",
                      "indeterminate": "indeterminate",
                      "running": "running"}


class Malformed(Exception):
    """A caller-supplied field is the wrong type/shape. Raised by the
    field readers so every handler answers with ONE named 400 instead of
    letting a TypeError become an unaudited 500."""

    def __init__(self, detail):
        super().__init__(detail)
        self.detail = detail


def text_field(body, name, required=True, limit=MAX_ID_CHARS):
    """Read a string field from a request body, or raise Malformed.

    Every id, operation name, and convoy id arrives from an untrusted
    caller and is then used as a DICT KEY. An unhashable value (a list
    or dict from JSON) raises TypeError at the lookup, which the
    handler's catch-all turns into an unaudited 500 -- a refusal the
    audit trail cannot classify (A-39). Reading fields through here
    makes the refusal named, audited, and a 400.
    """
    value = body.get(name)
    if value is None or value == "":
        if required:
            raise Malformed(f"{name} is required")
        return ""
    if not isinstance(value, str):
        raise Malformed(f"{name} must be a string, got "
                        f"{type(value).__name__}")
    if len(value) > limit:
        raise Malformed(f"{name} exceeds {limit} characters")
    return value


def dict_field(body, name):
    """Read an OBJECT field from a request body, or raise Malformed.

    `arguments` is the one caller-supplied field that is neither an id
    nor a scalar, and it becomes the node tool's KEYWORD ARGUMENTS -- a
    list, string or number was never meaningful there. It was type-
    checked nowhere: the store wrote `arguments or {}` verbatim, the
    envelope digest json-dumps any JSON value, and the dispatcher then
    built the async call with `dict(arguments)`, which RAISES on a list.
    That raise landed between a durable claim and its cleanup and wedged
    the delivery permanently (panel probe, 2026-08-01). Refuse it at the
    door, on both create paths, like every other wrong-typed field.
    """
    value = body.get(name)
    if value is None:
        return None
    if not isinstance(value, dict):
        raise Malformed(f"{name} must be an object, got "
                        f"{type(value).__name__}")
    return value


def _json_safe(value):
    """The value itself if it survives strict JSON, else an honest
    substitute. A node result rides into the durable job file and back
    out through every /jobs response; an unserializable payload (or a
    bare NaN, which json.dumps emits but strict parsers and our own
    _send refuse) would otherwise blow up the RECORDING of a verdict the
    node really produced -- the verdict must survive even when its
    payload cannot."""
    try:
        # EXACTLY the store's dumps options (sort_keys included): a
        # value that only fails under sort_keys (mixed-type dict keys)
        # passed a laxer check here and then blew up the store write,
        # downgrading a real verdict to indeterminate.
        json.dumps(value, indent=1, sort_keys=True, allow_nan=False)
        return value
    except (TypeError, ValueError):
        return {"detail": "node result was not JSON-serializable; "
                          "sanitized to its repr",
                "repr": repr(value)[:512]}


def _bounded_result(value):
    """_json_safe, plus a size cap. A terminal node payload is COPIED
    into the delivery record (the node's own fetch-by-id window closes at
    24h, so the host record is what survives), and nothing else bounds
    what a node may return. An oversized payload is replaced by an HONEST
    note carrying its head -- the verdict is what matters, and it is
    recorded either way."""
    value = _json_safe(value)
    try:
        blob = json.dumps(value, indent=1, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError):     # cannot happen after _json_safe
        return value
    if len(blob.encode("utf-8")) <= MAX_RESULT_BYTES:
        return value
    return {"detail": f"node result exceeded {MAX_RESULT_BYTES} bytes and "
                      f"was truncated; the verdict is unaffected",
            "truncated": True,
            "bytes": len(blob.encode("utf-8")),
            "head": blob[:2048]}


def _is_unknown_job_answer(payload):
    """Whether an ok=True payload is the node saying it has no such job.

    Two independent signals, either of which is enough: the companion
    `jobs` listing the node ships with that answer and nothing else, or
    the message itself. Deliberately narrow -- see
    _NODE_UNKNOWN_JOB_RE."""
    if not isinstance(payload, dict):
        return False
    message = payload.get("error")
    if not isinstance(message, str):
        return False
    return ("jobs" in payload
            or bool(_NODE_UNKNOWN_JOB_RE.search(message)))


def _node_job_handle(payload):
    """(node_job_id, node_status) when a node payload is a JOB HANDLE,
    else None.

    A handle is what an async operation returns INSTEAD of a result: the
    node minted a job and this names it. Both fields are required and the
    id is shape-validated -- an unvalidated node-supplied id would be
    handed straight back to the node as a poll argument and written into
    a durable record. `job_id` is the starting tool's field, `id` the
    poll record's; one reader serves both legs.
    """
    if not isinstance(payload, dict):
        return None
    job_id = payload.get("job_id")
    if not isinstance(job_id, str):
        job_id = payload.get("id")
    status = payload.get("status")
    if (not isinstance(job_id, str) or not NODE_JOB_ID_RE.match(job_id)
            or status not in NODE_JOB_STATUSES):
        return None
    return job_id, status


class HostApp:
    """All state behind one lock: a host app is coordination, not
    throughput. Every handler acquires it around the whole request --
    EXCEPT dispatch_job, drain/drain_once, and poll_job/poll_once, which
    SELF-lock in phases so the forward I/O runs outside the lock. Never
    call those from inside `with app.lock:` -- threading.Lock is not
    reentrant, and the double-acquire deadlocks the handler thread."""

    def __init__(self, directory_path, now=None, forwarder=None):
        self.data_dir = directory_path
        self._now = now or time.time
        self.started = self._now()
        self.token = platform_mod.ensure_ipc_token(directory_path)
        self.db = hoststore.HostStore(directory_path, now=now)
        self.host_id = self.db.host_id()
        self.directory, self.quarantined = self.db.load_directory()
        # The node SEAM: how the host executes a queued job against a
        # node's Envoy. Signature (port, operation, arguments) -> a dict
        # {"ok": bool, "result"/"error": ...} for an observed node result,
        # or None on a transport failure (-> indeterminate). The default
        # is the minimal MCP client (convoy_mcpclient.forward), which
        # handles the synchronous request/response tool call the dispatcher
        # needs today; tests inject their own. The robust transport
        # (streaming, reconnection) is the A-46 rework, and this seam is
        # exactly where it plugs in.
        #
        # ONE seam, TWO callers: the poll pass forwards through the same
        # function with operation POLL_OPERATION ("get_job_status"). A
        # test fake therefore answers BOTH legs and must branch on the
        # operation -- a fake that always returns a dispatch result will
        # answer polls with nonsense. Existing fakes are unaffected:
        # only an async operation ever reaches 'running', and only a
        # running job is ever polled.
        self.forwarder = forwarder or mcpclient.forward
        # DEEP copy per instance: a shallow one shares the nested schema
        # and side_effects dicts with the module constant, so mutating
        # one in a test would silently change every other instance's
        # capability digest.
        self.operations = copy.deepcopy(PHASE1_OPERATIONS)
        # In-memory BY DESIGN: leases are cooperative and TTL-bounded, so
        # a host restart dropping them is safe (controllers re-acquire),
        # exactly like claim_scope's session-silence expiry. Durable
        # leases arrive with wake-leases (Phase 4) if at all.
        self.leases = controllers.LeaseRegistry()
        self.lock = threading.Lock()
        # The autonomous dispatcher (start_drain_loop). Off by default:
        # dispatch stays a per-call affair until someone opts in.
        self._drain_thread = None
        self._drain_stop = None
        # Dispatch bookkeeping, all guarded by self.lock:
        #   _in_flight     delivery_id -> ATTEMPT token for forwards in
        #                  progress in THIS process -- what separates a
        #                  live claim from a stranded one (drain_once's
        #                  reaper). Attempt-scoped, not job-scoped: a
        #                  finished attempt may only remove ITS OWN
        #                  marker, because a job released back to queued
        #                  can be re-claimed by a second attempt before
        #                  the first attempt's cleanup runs -- a
        #                  job-scoped discard there erased the live
        #                  marker and the reaper burned an undelivered
        #                  job to indeterminate (review probe,
        #                  2026-07-31).
        #   _drain_backoff delivery_id -> not-before timestamp; the drain
        #                  loop skips a refused/deferred job until it
        #                  passes. Manual /dispatch ignores it (an
        #                  explicit call is its own authority).
        #   _drain_noted   delivery_id -> last audited refusal event, so a
        #                  steady failing state audits ON TRANSITION, not
        #                  on every tick (unbounded audit growth).
        self._in_flight = {}
        self._flight_counter = 0
        self._drain_backoff = {}
        self._drain_noted = {}
        # Poll bookkeeping -- SEPARATE maps, not the drain ones. The
        # drain prune's notion of "live" is the QUEUED set, so a poll
        # entry parked in a drain map would be wiped by the next drain
        # pass (the sharpest trap in this slice). Same shapes:
        #   _polls_in_flight delivery_id -> ATTEMPT token, so a late
        #                    response cannot clear a newer poll's marker.
        #                    In-memory only and NOT a claim: a poll is a
        #                    READ, two concurrent polls are harmless, and
        #                    a host dying mid-poll must leave the job
        #                    running (the node still owns it).
        #   _poll_backoff    delivery_id -> not-before timestamp; paces
        #                    the node. Manual /poll ignores it.
        #   _poll_noted      delivery_id -> last audited (event, reason).
        #   _poll_unknown    delivery_id -> {first, count, node_job_id,
        #                    message}: the evidence behind RULE 2's
        #                    node_forgot_job terminalisation.
        self._polls_in_flight = {}
        self._poll_counter = 0
        self._poll_backoff = {}
        self._poll_noted = {}
        self._poll_unknown = {}
        # delivery_ids whose UNREACHABLE requeue write FAILED: the job is
        # still claimed on disk, its flight marker is deliberately kept
        # (the reaper must not resolve a never-delivered job), and the
        # drain pass retries the release until the disk heals.
        self._pending_release = {}
        # delivery_ids whose ASYNC HANDOFF write failed: the node IS
        # running the job and the host holds its handle, but the running
        # mirror could not be written. Parked exactly like a failed
        # requeue -- claim still on disk, flight marker deliberately
        # kept, drain pass retries the mirror until the disk heals --
        # because the alternative (falling into the indeterminate
        # downgrade) DESTROYS the handle and with it the only key to an
        # outcome the node can still answer for 24h.
        self._pending_handoff = {}
        # Last summary PER PASS KIND (drain, poll) -- the audit only
        # fires when a pass's shape changes, and the two passes must not
        # overwrite each other's baseline.
        self._last_pass_summary = {}
        self.drain_backoff_s = 30.0
        self.poll_backoff_s = 30.0
        self.db.audit("hostapp", "started", {"host_id": self.host_id})

    # -- request handlers (called WITH self.lock held -- except the
    #    self-locking dispatch_job / drain, see their docstrings) -------

    def status(self):
        # ONE scan for every job number reported here -- each filtered
        # jobs() call parses every job file on disk, under the lock.
        counts = self.db.state_counts()
        return {
            "ok": True,
            "protocol": "convoy-host/1",
            "host_id": self.host_id,
            "nodes": len(self.directory.nodes()),
            "jobs_queued": counts.get("queued", 0),
            # Node jobs the host handed off and is polling: work in
            # flight ON THE NODE, invisible in jobs_queued.
            "jobs_running": counts.get("running", 0),
            "polls_in_flight": len(self._polls_in_flight),
            "quarantined_nodes": len(self.quarantined),
            "drain_loop": bool(self._drain_thread is not None
                               and self._drain_thread.is_alive()),
            "uptime_s": round(self._now() - self.started, 1),
        }

    def register_node(self, body):
        project_root = body.get("project_root")
        convoy_id = body.get("convoy_id")
        comp_path = body.get("comp_path") or ""
        # Per-launch, supplied by the TD side and never stored. Absent is
        # fine (the host mints one); what matters is that it CHANGES on
        # every TD start so a stale request can be caught.
        runtime_id = body.get("runtime_id")
        # Where the node's local Envoy listens, so the host can dispatch a
        # job back to it (loopback, Phase 1). Optional and per-launch.
        envoy_port = body.get("envoy_port")
        if (not comp_path or not isinstance(comp_path, str)
                or len(comp_path) > 512):
            self.db.audit("hostapp", "register_refused",
                          {"reason": "malformed", "detail": "comp_path"})
            return 400, {"ok": False, "reason": "malformed",
                         "detail": "comp_path is required (1..512 chars) "
                                   "-- omitting it would mint a new identity"}
        if envoy_port is not None and (
                isinstance(envoy_port, bool)
                or not isinstance(envoy_port, int)
                or not (1 <= envoy_port <= 65535)):
            self.db.audit("hostapp", "register_refused",
                          {"reason": "malformed", "detail": "envoy_port"})
            return 400, {"ok": False, "reason": "malformed",
                         "detail": "envoy_port must be an integer 1..65535"}
        known_before = {r["node_id"] for r in self.directory.nodes()}
        try:
            record = self.directory.register(
                project_root, comp_path, convoy_id, runtime_id=runtime_id,
                envoy_port=envoy_port)
        except identity.IdentityError as e:
            # A-39: refusals are AUDITED, not silent -- with no admission
            # control yet, visibility is the compensating control.
            self.db.audit("hostapp", "register_refused",
                          {"reason": e.reason, "detail": e.detail})
            code = 400 if e.reason.startswith("malformed") else 409
            return code, {"ok": False, "reason": e.reason,
                          "detail": e.detail}
        newly_minted = record["node_id"] not in known_before
        # PERSIST FIRST, then keep the in-memory directory. The reverse
        # order left a node that existed in memory (and accepted jobs)
        # but vanished on restart if the write failed. The convoy PSK is
        # minted in the same failure domain: a node whose group key could
        # not persist must not register (the envelope path would refuse
        # every request against a key nobody was ever handed).
        try:
            self.db.ensure_convoy_psk(record["convoy_id"])
            self.db.save_node(record)
        except Exception as e:
            # Roll back ONLY a registration this call minted. On a
            # RE-registration the record was already persisted by an
            # earlier call, and forgetting it here would evict a healthy
            # node from memory -- leaving it unknown to every route while
            # it still sits on disk, until a restart reloaded it.
            if newly_minted:
                self.directory.forget(record["node_id"])
            self.db.audit("hostapp", "register_failed",
                          {"error": f"{type(e).__name__}: {e}",
                           "rolled_back": newly_minted})
            return 500, {"ok": False, "reason": "persist_failed",
                         "detail": f"{type(e).__name__}: {e}"}
        self.db.audit("hostapp", "node_registered",
                      {"node_id": record["node_id"],
                       "comp_path": comp_path})
        return 200, {"ok": True,
                     "node_id": record["node_id"],
                     "runtime_id": record["runtime_id"],
                     "host_id": self.host_id,
                     "envoy_port": record.get("envoy_port"),
                     "td_python_approved": record["td_python_approved"]}

    def unregister_node(self, body):
        """Clear a node's live Envoy port -- it is shutting down cleanly.

        The counterpart to /register, called best-effort from the TD side
        on exit and on disable. It does NOT delete the node: node_id is
        the durable address (approvals attach to it), and only envoy_port
        is per-launch. Without this, a closed TD leaves behind a port the
        dispatcher keeps forwarding into, retrying every 30 s, forever.

        A hard kill still leaves a stale port -- unavoidable, and handled
        elsewhere: an UNREACHABLE forward keeps the job queued and backs
        off. This route makes the COMMON case clean, exactly like the
        SIGTERM handler makes the common host stop clean.

        ONLY CLEAR THE PORT YOU REGISTERED. node_id is derived from
        (project_root, comp_path), so two TD sessions on one project
        folder share it -- the plan's OQ-1. Without a precondition, the
        first session's CLEAN EXIT zeroes the second session's live
        port, and the surviving node goes undispatchable until its next
        heartbeat: a wrong-direction failure manufactured by an orderly
        shutdown. runtime_id is the per-launch proof of which run is
        talking (the same field A-22's expected_runtime_id relies on),
        so a caller that supplies it and does not match is answered with
        a 200 no-op. That is the plan's shared-identity rule -- "do not
        fight" -- rather than a refusal, because the departing session
        genuinely has nothing left to do.
        """
        try:
            node_id = text_field(body, "node_id")
            runtime_id = text_field(body, "runtime_id", required=False)
        except Malformed as e:
            return self._refuse("unregister", "malformed", e.detail, 400)
        record = self.directory.lookup(node_id)
        if record is None:
            return self._refuse("unregister", "unknown_node", node_id, 404)
        current = record.get("runtime_id")
        if runtime_id and current and runtime_id != current:
            self._audit_best_effort("unregister_superseded",
                                    {"node_id": node_id,
                                     "claimed_runtime_id": runtime_id,
                                     "current_runtime_id": current})
            return 200, {"ok": True,
                         "cleared": False,
                         "reason": "runtime_superseded",
                         "node_id": record["node_id"],
                         "host_id": self.host_id,
                         "envoy_port": record.get("envoy_port"),
                         "td_python_approved":
                             record["td_python_approved"]}
        self.directory.clear_envoy_port(node_id)
        # Unlike register there is NOTHING to roll back: envoy_port is
        # per-launch and hoststore.save_node does not persist it, so the
        # clear is already complete in the only place it lives. This write
        # exists to stamp last_seen. Refusing a clear that has demonstrably
        # happened would be a lie, so a failed stamp is audited, not
        # escalated to a 500.
        try:
            self.db.save_node(record)
        except Exception as e:
            self._audit_best_effort("unregister_persist_failed",
                                    {"node_id": node_id,
                                     "error": f"{type(e).__name__}: {e}"})
        self._audit_best_effort("node_unregistered",
                                {"node_id": record["node_id"],
                                 "comp_path": record["comp_path"]})
        return 200, {"ok": True,
                     "cleared": True,
                     "node_id": record["node_id"],
                     "host_id": self.host_id,
                     "envoy_port": record.get("envoy_port"),
                     "td_python_approved": record["td_python_approved"]}

    def _audit_best_effort(self, event, detail):
        """Audit without letting the trail's failure fail the request.

        Same reasoning as _refuse's swallowed audit: an unregister that
        really cleared the port must report success even if the append
        could not be written.
        """
        try:
            self.db.audit("hostapp", event, detail)
        except Exception:
            pass

    def remint_node(self, body):
        node_id = body.get("node_id") or ""
        try:
            fresh = self.directory.remint(node_id)
        except identity.IdentityError as e:
            return 404, {"ok": False, "reason": e.reason, "detail": e.detail}
        self.db.delete_node(node_id)
        self.db.save_node(fresh)
        self.db.audit("hostapp", "node_reminted",
                      {"old_node_id": node_id,
                       "new_node_id": fresh["node_id"]})
        return 200, {"ok": True, "node_id": fresh["node_id"],
                     "td_python_approved": fresh["td_python_approved"]}

    def list_nodes(self):
        return {"ok": True, "host_id": self.host_id,
                "nodes": self.directory.nodes()}

    def create_job(self, body):
        """The LOCAL job path: token-authenticated, loopback-only, no
        envelope. It shares the registry and lease gates with /envelope
        -- ONE gate for what may be enqueued, never two authorities --
        and it never rides the LAN: remote submission is /envelope only.
        """
        try:
            idempotency_key = text_field(body, "idempotency_key")
            operation = text_field(body, "operation",
                                   limit=MAX_OPERATION_CHARS)
            node_id = text_field(body, "node_id")
            # controller_id is optional on the local path; without one
            # the caller simply has no lease rights of its own.
            controller_id = text_field(body, "controller_id",
                                       required=False)
            expected_runtime_id = text_field(body, "expected_runtime_id",
                                             required=False)
            arguments = dict_field(body, "arguments")
        except Malformed as e:
            return self._refuse("jobs", "malformed", e.detail, 400)
        node = self.directory.lookup(node_id)
        if node is None:
            return self._refuse("jobs", "unknown_node", node_id, 404)
        refusal = self._gate_operation(
            node, operation, controller_id, source="jobs",
            expected_runtime_id=expected_runtime_id)
        if refusal is not None:
            return refusal
        # convoy_id comes from the REGISTERED node, never from the
        # request: a caller must not be able to choose which namespace
        # its idempotency key lands in.
        job, created = self.db.create_job(
            idempotency_key, node_id, operation, arguments,
            convoy_id=node["convoy_id"],
            expected_runtime_id=expected_runtime_id)
        return 200, {"ok": True, "created": created, "job": job}

    def get_job(self, delivery_id):
        job = self.db.get_job(delivery_id)
        if job is None:
            return 404, {"ok": False, "reason": "unknown_job",
                         "detail": delivery_id}
        return 200, {"ok": True, "job": job}

    # -- dispatch: execute a queued job against the node (Phase 4 slice 1)

    def dispatch_job(self, delivery_id):
        """Execute one QUEUED job by forwarding its operation to the node's
        Envoy (loopback), then mirror the verdict.

        SELF-LOCKING, unlike every other handler: called WITHOUT
        self.lock held (the /dispatch and /drain routes sit outside
        do_POST's lock block; threading.Lock is not reentrant, so calling
        this from inside the lock would deadlock). Three phases:

          a) UNDER the lock: read the job and node, re-gate against the
             CURRENT registry and the runtime AS THE HOST LAST OBSERVED
             IT, then CLAIM the job (queued -> dispatching, the CAS in
             claim_for_dispatch) and mark it in-flight. The claim is what
             lets the drain loop and manual /dispatch race safely --
             exactly one caller wins a given job.
          b) OUTSIDE the lock: the forward, up to 30s of I/O. Holding the
             lock here would freeze every other route for the duration --
             the drain loop would make that permanent.
          c) UNDER the lock again: resolve the claim (_resolve_dispatch).
             The A-15 invariant holds: a node verdict (record_sync_result,
             demanding a real boolean ok), back to queued (UNREACHABLE =
             never delivered), or indeterminate (no response, a
             non-verdict response, or the recording itself failed =
             outcome unknown). The host never invents a verdict.

        ASYNC operations (registry entries carrying `async_job`) resolve
        differently in phase c: the node answers with a job HANDLE, not
        a result, so the job is recorded 'running' with that provenance
        and the POLL pass owns its outcome from there. An async answer
        with no handle goes back to queued -- the retry reconciles on
        the idempotency key rather than guessing a verdict.

        Idempotent: a job already claimed or past queued is returned
        unchanged (dispatched=False), so a double dispatch cannot re-run
        the work.
        """
        # -- phase a: read, gate, claim -- under the lock ---------------
        with self.lock:
            if (not delivery_id or not isinstance(delivery_id, str)
                    or len(delivery_id) > MAX_ID_CHARS):
                # Named, audited 400 -- not a TypeError-500 the audit
                # trail cannot classify (A-39), matching text_field's
                # treatment on every other route.
                return self._refuse("dispatch", "malformed",
                                    "delivery_id must be a string", 400)
            job = self.db.get_job(delivery_id)
            if job is None:
                # Audited directly, NOT through the dedupe map: the map
                # is pruned against jobs that exist, so ghost ids would
                # accumulate entries forever (the drain loop never sees
                # them). Caller-driven and token-gated, so unconditional
                # audit lines are the caller's own doing.
                try:
                    self.db.audit("hostapp", "dispatch_refused",
                                  {"reason": "unknown_job",
                                   "delivery_id": delivery_id})
                except Exception:
                    pass    # named 404 beats an unaudited 500
                return 404, {"ok": False, "reason": "unknown_job",
                             "detail": delivery_id}
            if job.get("state") != "queued":
                # Claimed by another dispatcher, or terminal -- never
                # re-run.
                return 200, {"ok": True, "dispatched": False, "job": job}
            node = self.directory.lookup(job["node_id"])
            if node is None:
                self._note_dispatch_event(delivery_id, "dispatch_refused",
                                          {"reason": "unknown_node",
                                           "node_id": job["node_id"]})
                self._set_drain_backoff(delivery_id,
                                        self._now() + self.drain_backoff_s)
                return 404, {"ok": False, "reason": "unknown_node",
                             "detail": job["node_id"]}
            # Re-gate against the registry AS OF NOW, not as of enqueue:
            # the entry may have been tightened or removed while the job
            # sat queued. The runtime re-check below is HONESTLY BOUNDED:
            # it compares against the runtime the host LAST OBSERVED (the
            # registry record), so it catches a restart the node has
            # re-registered -- and CANNOT catch a restart the host has
            # not seen yet, or one that lands mid-forward, because the
            # expectation does not ride the wire. Full A-22 closure needs
            # the node itself to verify expected_runtime_id (Phase 2
            # ConvoyExt + the A-46 transport); named deferral, not an
            # oversight.
            entry = self.operations.get(job["operation"])
            gating = gating_of(entry) if entry else None
            if gating is None or gating["executes_arbitrary_code"]:
                reason = ("operation_not_exposed" if gating is None
                          else "operation_not_relayable")
                self._note_dispatch_event(
                    delivery_id, "dispatch_refused",
                    {"reason": reason,
                     "operation": job["operation"][:MAX_OPERATION_CHARS]})
                self._set_drain_backoff(delivery_id,
                                        self._now() + self.drain_backoff_s)
                return 409, {"ok": False, "reason": reason,
                             "detail": "the operation is no longer "
                                       "relayable under the current "
                                       "registry; the job stays queued"}
            if gating["runtime_required"]:
                expected = job.get("expected_runtime_id")
                current = node.get("runtime_id")
                if not expected or not current or expected != current:
                    self._note_dispatch_event(
                        delivery_id, "dispatch_runtime_changed",
                        {"reason": f"expected {str(expected)[:64]!r}, "
                                   f"node {str(current)[:64]!r}",
                         "expected": str(expected)[:64],
                         "current": str(current)[:64]})
                    self._set_drain_backoff(delivery_id,
                        self._now() + self.drain_backoff_s)
                    return 409, {
                        "ok": False, "reason": "runtime_changed",
                        "detail": f"the job addressed runtime "
                                  f"{expected!r} but the node is now "
                                  f"{current!r}; refusing to dispatch "
                                  f"into a different runtime (A-22)"}
            port = node.get("envoy_port")
            if not port:
                # No endpoint yet -- the node has not registered its live
                # Envoy port. Leave the job queued (unclaimed) to dispatch
                # once it does; this is a not-yet, not a failure.
                self._note_dispatch_event(
                    delivery_id, "dispatch_deferred",
                    {"reason": "node_endpoint_unknown"})
                self._set_drain_backoff(delivery_id,
                                        self._now() + self.drain_backoff_s)
                return 409, {"ok": False, "reason": "node_endpoint_unknown",
                             "detail": "the node has not registered its "
                                       "Envoy port; the job stays queued"}
            try:
                claimed = self.db.claim_for_dispatch(delivery_id)
            except Exception as e:
                # The claim WRITE failed (a Windows sharing violation
                # that outlived the retries, disk trouble): os.replace
                # is atomic, so the job on disk is still queued. A named
                # refusal, never the unaudited 500 this used to become.
                detail = f"{type(e).__name__}: {e}"
                try:
                    self.db.audit("hostapp", "dispatch_store_unavailable",
                                  {"delivery_id": delivery_id,
                                   "detail": detail[:256]})
                except Exception:
                    pass
                return 503, {"ok": False, "reason": "store_unavailable",
                             "detail": f"could not claim the job "
                                       f"({detail}); it stays queued"}
            if claimed is None:
                # Should be unreachable (the state was read as queued
                # under this same lock hold) -- but a skip is always the
                # safe answer to a lost claim.
                job = self.db.get_job(delivery_id) or job
                return 200, {"ok": True, "dispatched": False, "job": job}
            # Capture everything the rest of the dispatch needs BEFORE
            # the marker goes up, so that nothing at all sits between
            # the marker and the try/finally below.
            operation = job["operation"]
            raw_arguments = job.get("arguments")
            idempotency_key = job.get("idempotency_key")
            async_spec = entry.get("async_job")
            self._flight_counter += 1
            attempt = self._flight_counter
            self._in_flight[delivery_id] = attempt

        # FROM HERE the claim is durable and the marker is up, so the
        # claim and its cleanup share ONE failure domain. They did not:
        # the async argument merge sat inside phase a, ABOVE this try,
        # and a raise there (a non-dict `arguments` -> `dict(list)`)
        # leaked the marker forever. That made drain_once's reaper skip
        # the job permanently (`if did in self._in_flight: continue`),
        # left it invisible to /dispatch and /poll alike, and ended in a
        # host restart writing a FALSE indeterminate for an operation
        # that never left phase a (panel probe, 2026-08-01). Structural
        # on purpose: no future edit to the post-claim tail can reopen
        # the class.
        try:
            # -- phase a tail: the arguments actually put on the wire ---
            with self.lock:
                try:
                    arguments, injected, dropped = self._merged_arguments(
                        raw_arguments, idempotency_key, async_spec)
                except Malformed as e:
                    # Belt to the door's braces: enqueue refuses a
                    # non-dict on both create paths, so only an older
                    # build or hand-edited state reaches this -- and it
                    # RELEASES the claim into a named refusal instead of
                    # raising. The job never goes on the wire malformed.
                    return self._requeue_claim(
                        delivery_id, operation, self._now(),
                        reason="malformed_arguments",
                        event="dispatch_malformed_arguments",
                        detail=f"the job's stored arguments are unusable "
                               f"({e.detail}); it stays queued and nothing "
                               f"was forwarded",
                        cause="the job's stored arguments are unusable")
                # Unconditional, and deliberately NOT deduped: this
                # records a real state transition on disk (queued ->
                # dispatching), not a refusal. Named cost (panel,
                # 2026-08-01): a delivery that requeues forever appends
                # one line per attempt, ~2 per minute at the default
                # backoff. Deduping it would hide genuine claims; the
                # bound belongs to the deferred reaper that stops the
                # retry loop itself, and until then `attempts` on the
                # record is what makes such a job findable without
                # reading the trail at all.
                try:
                    self.db.audit("hostapp", "dispatch_claimed",
                                  {"delivery_id": delivery_id,
                                   "operation": operation, "port": port,
                                   "injected": injected,
                                   "dropped": dropped})
                except Exception:
                    # An audit failure must never divert or strand a
                    # dispatch: the claim is on disk and the in-flight
                    # marker is set -- the forward proceeds.
                    pass

            # -- phase b: the forward, OUTSIDE the lock -----------------
            detail = ""
            try:
                outcome = self.forwarder(port, operation, arguments)
            except Exception as e:  # a forwarder must not crash dispatch
                outcome = None
                detail = f"{type(e).__name__}: {e}"
            if (outcome is not None
                    and outcome is not mcpclient.UNREACHABLE
                    and not (isinstance(outcome, dict)
                             and isinstance(outcome.get("ok"), bool))):
                # A broken forwarder must resolve like any transport
                # failure -- and 'broken' includes a DICT carrying no
                # real boolean verdict. Recording {} or {'error': ...} as
                # failed would fabricate a node verdict the node never
                # produced (A-15); the type check alone was half a guard.
                detail = (f"forwarder returned {type(outcome).__name__} "
                          f"without a boolean 'ok' verdict")
                outcome = None
            observed = self._now()

            # -- phase c: resolve the claim, under the lock -------------
            try:
                with self.lock:
                    return self._resolve_dispatch(delivery_id, operation,
                                                  outcome, observed,
                                                  detail, async_spec)
            except Exception as e:
                return self._downgrade_failed_recording(delivery_id,
                                                        operation, e)
        finally:
            with self.lock:
                # Remove ONLY this attempt's marker. After a release back
                # to queued, another attempt may already have re-claimed
                # this job and own a newer marker -- erasing it would let
                # the reaper mark a live forward stranded. And a PARKED
                # release (failed requeue write) or PARKED HANDOFF
                # (failed running-mirror write) keeps its marker on
                # purpose: the claim is still on disk, and the reaper
                # must not resolve it before the drain pass retries.
                if (delivery_id not in self._pending_release
                        and delivery_id not in self._pending_handoff
                        and self._in_flight.get(delivery_id) == attempt):
                    del self._in_flight[delivery_id]

    def _merged_arguments(self, raw_arguments, idempotency_key, async_spec):
        """The arguments actually put on the wire: (arguments, injected,
        dropped). Raises Malformed if the stored arguments are unusable.

        Pure and side-effect free, so a raise here can only ever be a
        refusal -- never a half-applied state.

        For a SYNC operation this is the caller's arguments verbatim.
        For an ASYNC one, host policy OVERRIDES the caller: `inject`
        forces the fields whose caller-supplied values would each break
        a different invariant (background False blocks the forward for
        the whole run, a caller idempotency_key breaks the node's 16.5
        anchor, override True bypasses the NODE's destructive gate), and
        `caller_args` -- when the entry declares one -- is the exhaustive
        list of caller fields the node's tool signature actually accepts.
        Anything else is DROPPED rather than forwarded: the host knows
        the operation's argument surface, and putting a field on the
        wire that the node must reject is a failure the host invented
        (panel finding, 2026-08-01: save_project's `inject: {}` overrode
        nothing, so junk rode through and killed the job at the node).
        This is not general schema validation (out of scope) -- it is
        the async injection contract being complete.

        The merged arguments are deliberately NOT persisted onto the job
        record: they are how THIS host relays it, not what was asked.
        """
        if raw_arguments is None:
            raw_arguments = {}
        if not isinstance(raw_arguments, dict):
            raise Malformed(f"arguments must be an object, got "
                            f"{type(raw_arguments).__name__}")
        if not async_spec:
            return dict(raw_arguments), [], []
        allowed = async_spec.get("caller_args")
        dropped = []
        base = dict(raw_arguments)
        if allowed is not None:
            dropped = sorted(k for k in base if k not in allowed)
            base = {k: v for k, v in base.items() if k in allowed}
        base.update(async_spec.get("inject") or {})
        key_arg = async_spec.get("key_arg", "idempotency_key")
        base[key_arg] = idempotency_key
        injected = sorted(set(async_spec.get("inject") or {}) | {key_arg})
        return base, injected, dropped

    def _note_dispatch_event(self, delivery_id, event, detail):
        """Audit a REPEATING per-job dispatch refusal once per transition.

        Called with self.lock held. The drain loop re-attempts refused
        and deferred jobs on a timer, so auditing every attempt grows
        audit.jsonl without bound on a steady failing state (a portless
        node is today's NORMAL state -- nothing in TD auto-registers
        yet). The noted event clears when the job resolves (see
        _resolve_dispatch) or falls out of the queue, so a recurrence
        after a recovery is audited afresh.

        The dedupe key is (event, reason), not the event name alone: a
        job whose REFUSAL REASON changes (deferred -> unknown_node, one
        registry refusal -> another) is a genuine transition the trail
        must show -- deduping on the event name swallowed it."""
        key = (event, (detail or {}).get("reason"))
        if self._drain_noted.get(delivery_id) == key:
            return
        try:
            self.db.audit("hostapp", event,
                          dict(detail, delivery_id=delivery_id))
        except Exception:
            # Audit trouble must neither break the dispatch (an escape
            # here surfaced as an unnamed 500 on a refusal path) nor
            # poison the dedupe: marking noted for a line never written
            # would swallow the NEXT attempt's audit too. Leave unnoted
            # so a later attempt retries the append.
            return
        self._drain_noted[delivery_id] = key
        if len(self._drain_noted) > 2048:
            # Bounded: on a loop-off host nothing prunes, and the map
            # must not grow one entry per refused delivery forever.
            self._drain_noted.pop(next(iter(self._drain_noted)))

    def _requeue_claim(self, delivery_id, operation, observed, reason,
                       event, detail, cause):
        """Release a claim back to QUEUED and refuse -- the shared
        never-terminalise path. Called WITH self.lock held.

        Two callers, one honesty contract: UNREACHABLE (the connection
        was refused, so the request was never delivered) and an async
        answer carrying no node-job handle (the node may have started
        work whose id we lost, and the retry's idempotency key provably
        recovers it). Both must return the job to the queue rather than
        burn it -- indeterminate is terminal and precious (16.4).

        `cause` is the human phrase naming why, spliced into the failure
        responses; `reason`/`event`/`detail` shape the normal refusal.
        Returns (code, payload).
        """
        try:
            released = self.db.release_claim(delivery_id)
        except Exception as e:
            # The REQUEUE write failed. The op was never delivered (or
            # is recoverable by key), so this must NOT decay to
            # indeterminate (the forbidden collapse -- round-3 panel
            # reproduced it under natural file contention). Park the
            # release: this attempt's flight marker is kept (the finally
            # skips parked ids) so the reaper cannot resolve the claim,
            # and the drain pass retries the release until the disk
            # heals. Named residual: a host that DIES before the retry
            # lands leaves a claim the boot sweep resolves to
            # indeterminate -- never-delivered knowledge cannot outlive
            # the process without a write, which is exactly what is
            # failing.
            failure = f"{type(e).__name__}: {e}"
            self._pending_release[delivery_id] = failure
            try:
                self.db.audit("hostapp", "dispatch_release_failed",
                              {"delivery_id": delivery_id,
                               "reason": reason,
                               "detail": failure[:256]})
            except Exception:
                pass
            return 503, {"ok": False, "reason": "store_unavailable",
                         "detail": f"{cause} but the requeue write failed "
                                   f"({failure}); the drain loop will "
                                   f"retry the requeue"}
        if released is None:
            # The CAS did not release. Distinguish UNREADABLE from GONE
            # before concluding anything: get_job swallows read errors
            # into None, and treating a transiently unreadable file as a
            # lost claim handed a never-delivered job to the reaper
            # (round-4 panel).
            current = self.db.get_job(delivery_id)
            state = current.get("state") if current else None
            if state == "dispatching" or (
                    current is None
                    and self.db.job_file_exists(delivery_id)):
                # Still claimed on disk (or unreadable-but-present,
                # conservatively the same): the CAS's READ failed, not
                # the claim. Park it exactly like a failed release write.
                self._pending_release[delivery_id] = (
                    "release CAS could not read the record")
                try:
                    self.db.audit("hostapp", "dispatch_release_failed",
                                  {"delivery_id": delivery_id,
                                   "reason": reason,
                                   "detail": "release CAS read failure"})
                except Exception:
                    pass
                return 503, {"ok": False,
                             "reason": "store_unavailable",
                             "detail": f"{cause} but the requeue could not "
                                       f"read the record; the drain loop "
                                       f"will retry"}
            state = state if current else "missing"
            try:
                self.db.audit("hostapp", "dispatch_claim_lost",
                              {"delivery_id": delivery_id,
                               "reason": reason, "state": state})
            except Exception:
                pass
            return 409, {"ok": False, "reason": "claim_lost",
                         "detail": f"{cause}, but this dispatcher no "
                                   f"longer held the claim; the job is "
                                   f"now {state!r}"}
        try:
            # The retry loop's ONLY on-record evidence. The audit line
            # is deduped to one per transition (that is what keeps
            # audit.jsonl bounded on a steady failing state), so without
            # this a job requeuing forever looks brand new in /jobs.
            # Best-effort and strictly after the release: a bookkeeping
            # write must never alter or divert dispatch state.
            self.db.record_dispatch_note(delivery_id, reason, observed)
        except Exception:
            pass
        self._note_dispatch_event(delivery_id, event,
                                  {"operation": operation})
        self._set_drain_backoff(delivery_id, observed + self.drain_backoff_s)
        return 409, {"ok": False, "reason": reason, "detail": detail}

    def _resolve_dispatch(self, delivery_id, operation, outcome, observed,
                          detail, async_spec=None):
        """Phase c of dispatch_job -- called WITH self.lock held."""
        if outcome is mcpclient.UNREACHABLE:
            # The node refused the connection: the request was never
            # delivered, so the op did NOT run. Release the claim -- the
            # job returns to QUEUED to retry. A transient node-down must
            # never burn a job to indeterminate (or, worse, a fabricated
            # verdict).
            return self._requeue_claim(
                delivery_id, operation, observed,
                reason="node_unreachable", event="dispatch_unreachable",
                detail="the node's Envoy refused the connection; the job "
                       "stays queued to retry",
                cause="the node refused the connection")
        handle = None
        if (async_spec is not None and isinstance(outcome, dict)
                and outcome.get("ok") is True):
            # An async operation must answer with a node-job HANDLE. The
            # payload arrives as ok=True even when it is a refusal --
            # Envoy's MCP layer sets isError only when a tool RAISES,
            # and these tools RETURN {'error': ...} dicts. So 'ok' means
            # the CALL completed, never that the operation succeeded:
            # the payload itself has to be inspected.
            handle = _node_job_handle(outcome.get("result"))
            if handle is None:
                # No handle. The node refused before minting -- OR it
                # started a run whose id we lost (the node's own 30s
                # main-thread timeout answers exactly like a refusal).
                # Recording either 'failed' or 'indeterminate' would be
                # a guess; requeue instead, because the retry carries
                # the same idempotency_key and the node's index hands
                # back the original handle (proven by
                # test_a_requeued_async_delivery_recovers_the_lost_handle).
                # Checked BEFORE the bookkeeping is cleared below, so
                # the repeat audits once per transition like every other
                # steady refusal.
                #
                # NAMED RESIDUAL (panel, 2026-08-01): the retry only
                # PROVABLY recovers when the node actually minted a job.
                # Part of the 'refused before minting' family is
                # permanent by construction -- an idempotency_key
                # already bound to a different operation is caused BY
                # the key the retry keeps sending; the multi-session
                # gate and 'Job records unavailable' persist as long as
                # their cause does. Those requeue at the backoff rate
                # forever. That is deliberate under A-15 (the host has
                # no node-originated evidence to terminalise on, and
                # inventing one is the forbidden collapse); bounding it
                # is the deferred host reaper's job (A-15 item b). What
                # this slice owes such a job is VISIBILITY, and that is
                # record_dispatch_note in _requeue_claim: attempts and
                # last_attempt on the delivery record, because the audit
                # line is deduped to one by design.
                return self._requeue_claim(
                    delivery_id, operation, observed,
                    reason="no_node_job_handle", event="dispatch_no_handle",
                    detail="the node answered an async operation without a "
                           "job handle; the job stays queued and the retry "
                           "reconciles on its idempotency key",
                    cause="the node returned no job handle")
        # The job is resolving -- clear its refusal bookkeeping so a
        # future recurrence is audited afresh.
        self._drain_noted.pop(delivery_id, None)
        self._drain_backoff.pop(delivery_id, None)
        if outcome is None:
            # Transport failure AFTER the request may have been sent:
            # the operation MAY have executed, and we have no result.
            # Indeterminate is the only honest terminal -- never a
            # silent retry (it might double-run) or a fake fail.
            updated = self.db.mark_indeterminate(delivery_id, {
                "reason": "no_response",
                "detail": detail or "no response from the node's Envoy",
                "operation": operation})
            try:
                self.db.audit("hostapp", "dispatch_indeterminate",
                              {"delivery_id": delivery_id,
                               "operation": operation})
            except Exception:
                pass    # the outcome is durably written; audits never
                        # alter dispatch state (round-3 panel)
            return 200, {"ok": True, "dispatched": True, "job": updated}
        if handle is not None:
            # THE HANDOFF. The node minted a job and named it; the host
            # mirrors that with the node's provenance and stops here --
            # the poller owns the outcome from now on.
            #
            # 'running' the instant the handle lands is load-bearing:
            # the job LEAVES 'dispatching' inside this phase, before any
            # drain pass can run, so the stranded-claim reaper (which
            # resolves dispatching claims with no forward in flight)
            # can never see it. Parking an awaited node job at
            # 'dispatching' would have it reaped to indeterminate on the
            # very next tick.
            node_job_id, node_status = handle
            # AUDIT FIRST, then write. The handle is the only key to an
            # outcome the node can still answer for 24h; audit.jsonl is
            # append-only and survives a process death that the parked
            # retry below would not. Auditing after the write meant a
            # failed write threw the handle away entirely (panel probe,
            # 2026-08-01).
            try:
                self.db.audit(
                    "hostapp", "dispatch_node_job_started",
                    {"delivery_id": delivery_id, "operation": operation,
                     "node_job_id": node_job_id, "status": node_status,
                     "kind": (async_spec or {}).get("kind")})
            except Exception:
                pass    # audits never alter dispatch state (round-3 panel)
            result = _bounded_result(self._handoff_result(outcome.get(
                "result"), node_status))
            try:
                updated = self.db.record_node_verdict(
                    delivery_id, node_status, node_job_id=node_job_id,
                    observed_at=observed, result=result)
            except Exception as e:
                # The mirror write failed -- transiently, most likely
                # (the sharing-violation class _requeue_claim parks
                # for). PARK it: the node is RUNNING this job and we
                # hold its handle, so letting this fall through to the
                # indeterminate downgrade would terminalise a run the
                # host provably observed AND destroy the only key to
                # its outcome. Claim stays on disk, flight marker stays
                # up (the finally skips parked ids, so the reaper cannot
                # touch it), and the drain pass retries the mirror.
                # Named residual: a host that DIES before the retry
                # lands leaves a claim the boot sweep resolves to
                # indeterminate -- but the handle is already in the
                # audit trail above, so the run stays findable by hand.
                failure = f"{type(e).__name__}: {e}"
                self._pending_handoff[delivery_id] = {
                    "node_job_id": node_job_id, "node_status": node_status,
                    "observed_at": observed, "result": result,
                    "detail": failure}
                try:
                    self.db.audit("hostapp", "dispatch_handoff_parked",
                                  {"delivery_id": delivery_id,
                                   "node_job_id": node_job_id,
                                   "status": node_status,
                                   "detail": failure[:256]})
                except Exception:
                    pass
                return 503, {"ok": False, "reason": "store_unavailable",
                             "detail": f"the node started {node_job_id!r} "
                                       f"but the running mirror could not "
                                       f"be written ({failure}); the drain "
                                       f"loop will retry the mirror"}
            started = updated.get("state") == "running"
            return 200, {"ok": True, "dispatched": True, "started": started,
                         "job": updated}
        ok = outcome.get("ok")      # a real bool -- phase b enforced it
        result = outcome.get("result") if ok else {
            "error": outcome.get("error")}
        result = _json_safe(result)
        updated = self.db.record_sync_result(delivery_id, ok, observed,
                                             result=result)
        try:
            self.db.audit("hostapp", "dispatched",
                          {"delivery_id": delivery_id, "ok": ok,
                           "operation": operation})
        except Exception:
            # The node's verdict is durably recorded. An audit-append
            # failure after it must never reach the downgrade path --
            # that DESTROYED a real node verdict (round-3 panel, A-15).
            pass
        return 200, {"ok": True, "dispatched": True, "job": updated}

    @staticmethod
    def _handoff_result(payload, node_status):
        """The handle payload as it is mirrored, with an honest note when
        a TERMINAL handle carries no outcome.

        A 16.5 idempotency RECONCILE hands back {'job_id', 'status':
        'done', 'hint'} -- a real node verdict, but with no result body,
        because the node is answering "you already asked for this" and
        not "here is what happened". Recording it terminal is right (the
        node authored the status), but the record would then hold no
        outcome at all, and the poller never revisits a terminal job. So
        say so, on the record, and name where the outcome still lives
        for the node's 24h window. Polling for the body instead would
        mean either a forward under the lock or writing a 'running'
        state the node never reported."""
        if node_status == "running" or not isinstance(payload, dict):
            return payload
        if payload.get("result") is not None or payload.get("error"):
            return payload      # the node did carry an outcome
        return dict(payload, detail=(
            "the node reconciled this delivery to a run that had already "
            "finished, so its answer carried no result body; fetch "
            "get_job_status(job_id) on the node within its 24h retention "
            "for the outcome"))

    def _downgrade_failed_recording(self, delivery_id, operation, error):
        """Phase c raised: the store could not write the honest outcome.
        The durable record can no longer reflect what was observed, so
        downgrade the claim to indeterminate with the failure as
        evidence. If even that write fails, leave the stranded claim for
        drain_once's reaper (or the load-time sweep) -- never let the
        exception escape as an unexplained 500 that strands the job
        silently."""
        detail = f"{type(error).__name__}: {error}"
        try:
            with self.lock:
                current = self.db.get_job(delivery_id)
                state = current.get("state") if current else None
                if state != "dispatching":
                    # The durable record already resolved -- a verdict
                    # landed, or the claim was released -- and the raise
                    # was something else. NEVER overwrite what is on
                    # disk with a host guess: doing so destroyed a real
                    # node verdict and re-terminalised a released job
                    # (round-3 panel, A-15 / 16.4).
                    try:
                        self.db.audit("hostapp",
                                      "dispatch_recording_glitch",
                                      {"delivery_id": delivery_id,
                                       "state": state,
                                       "detail": detail[:256]})
                    except Exception:
                        pass
                    if current is not None:
                        return 200, {"ok": True,
                                     "dispatched": state != "queued",
                                     "job": current}
                    return 500, {"ok": False,
                                 "reason": "recording_failed",
                                 "detail": detail}
                self.db.mark_indeterminate(delivery_id, {
                    "reason": "verdict_recording_failed",
                    "detail": detail, "operation": operation})
                try:
                    self.db.audit("hostapp", "dispatch_recording_failed",
                                  {"delivery_id": delivery_id,
                                   "detail": detail[:256]})
                except Exception:
                    pass    # the downgrade LANDED; the response below
                            # must not claim it is still pending
            return 500, {"ok": False, "reason": "recording_failed",
                         "detail": f"the node outcome could not be "
                                   f"recorded ({detail}); the job is "
                                   f"marked indeterminate"}
        except Exception:
            return 500, {"ok": False, "reason": "recording_failed",
                         "detail": f"the node outcome could not be "
                                   f"recorded and the downgrade write "
                                   f"also failed ({detail}); the job "
                                   f"remains claimed until reaped"}

    # -- poll: observe a node job the host handed off -------------------

    def poll_job(self, delivery_id):
        """Ask the node what became of ONE running node job, and mirror
        the answer.

        SELF-LOCKING, exactly like dispatch_job, and for the same reason
        (/poll sits outside do_POST's lock block; calling this from
        inside `with app.lock:` deadlocks). Three phases: read under the
        lock, forward outside it, resolve under it again.

        What is DELIBERATELY absent, versus dispatch:

          - no durable claim. A poll is a READ; two concurrent polls are
            harmless, and a host that dies mid-poll must leave the job
            running -- the node owns the execution and will still answer
            for it. _polls_in_flight is in-memory only: it avoids
            duplicate I/O and keeps a late answer from regressing state.
          - no re-gate. The gate decides whether work may START; this
            job already did. Re-gating would refuse to LOOK at a running
            node job and abandon it to limbo. The bound is that this
            calls exactly one hardcoded read-only operation, with a
            shape-validated id the node itself minted.
        """
        # -- phase a: read the job, node and port -- under the lock -----
        with self.lock:
            if (not delivery_id or not isinstance(delivery_id, str)
                    or len(delivery_id) > MAX_ID_CHARS):
                return self._refuse("poll", "malformed",
                                    "delivery_id must be a string", 400)
            job = self.db.get_job(delivery_id)
            if job is None:
                # Audited directly, not through the dedupe map -- ghost
                # ids would accumulate entries nothing prunes (the same
                # reasoning as dispatch_job's unknown_job).
                try:
                    self.db.audit("hostapp", "poll_refused",
                                  {"reason": "unknown_job",
                                   "delivery_id": delivery_id})
                except Exception:
                    pass    # named 404 beats an unaudited 500
                return 404, {"ok": False, "reason": "unknown_job",
                             "detail": delivery_id}
            if job.get("state") != "running":
                # Queued, claimed, or already settled -- not ours.
                return 200, {"ok": True, "polled": False, "job": job}
            node_job_id = job.get("node_job_id")
            if (not isinstance(node_job_id, str)
                    or not NODE_JOB_ID_RE.match(node_job_id)):
                # Unreachable through this code (record_node_verdict
                # demands the provenance), so this is hand-edited or
                # corrupt state. Refuse loudly rather than forward a
                # guessed id.
                self._note_poll_event(delivery_id, "poll_missing_provenance",
                                      {"reason": "no_node_provenance",
                                       "node_job_id": str(node_job_id)[:64]})
                self._set_poll_backoff(delivery_id,
                                       self._now() + self.poll_backoff_s)
                return 409, {"ok": False, "reason": "no_node_provenance",
                             "detail": "the job is running but carries no "
                                       "valid node job id; refusing to poll "
                                       "for an id the node never minted"}
            if delivery_id in self._polls_in_flight:
                return 200, {"ok": True, "polled": False,
                             "reason": "poll_in_flight", "job": job}
            node = self.directory.lookup(job["node_id"])
            if node is None:
                self._note_poll_event(delivery_id, "poll_refused",
                                      {"reason": "unknown_node",
                                       "node_id": job["node_id"]})
                self._set_poll_backoff(delivery_id,
                                       self._now() + self.poll_backoff_s)
                return 404, {"ok": False, "reason": "unknown_node",
                             "detail": job["node_id"]}
            port = node.get("envoy_port")
            if not port:
                # envoy_port is PER-LAUNCH and never persisted, so after
                # a host restart every poll defers here until the node
                # re-registers. The job stays running -- honest: the run
                # is still the node's, we simply cannot ask yet.
                self._note_poll_event(delivery_id, "poll_deferred",
                                      {"reason": "node_endpoint_unknown"})
                self._set_poll_backoff(delivery_id,
                                       self._now() + self.poll_backoff_s)
                return 409, {"ok": False, "reason": "node_endpoint_unknown",
                             "detail": "the node has not registered its "
                                       "Envoy port; the job stays running"}
            self._poll_counter += 1
            attempt = self._poll_counter
            self._polls_in_flight[delivery_id] = attempt

        try:
            # -- phase b: the poll forward, OUTSIDE the lock ------------
            detail = ""
            try:
                outcome = self.forwarder(port, POLL_OPERATION,
                                         {"job_id": node_job_id})
            except Exception as e:      # a forwarder must not crash the pass
                outcome = None
                detail = f"{type(e).__name__}: {e}"
            if (outcome is not None
                    and outcome is not mcpclient.UNREACHABLE
                    and not (isinstance(outcome, dict)
                             and isinstance(outcome.get("ok"), bool))):
                detail = (f"forwarder returned {type(outcome).__name__} "
                          f"without a boolean 'ok' verdict")
                outcome = None
            observed = self._now()

            # -- phase c: mirror the answer, under the lock -------------
            try:
                with self.lock:
                    return self._resolve_poll(delivery_id, node_job_id,
                                              outcome, observed, detail)
            except Exception as e:
                # Contrast _downgrade_failed_recording: a dispatch holds
                # a CLAIM, so a failed phase-c write must resolve it or
                # the job strands. A poll holds NOTHING -- the record is
                # exactly as the node last left it, so the honest answer
                # is to leave it running and let the next poll retry.
                failure = f"{type(e).__name__}: {e}"
                try:
                    with self.lock:
                        self.db.audit("hostapp", "poll_recording_failed",
                                      {"delivery_id": delivery_id,
                                       "detail": failure[:256]})
                except Exception:
                    pass
                return 500, {"ok": False, "reason": "poll_recording_failed",
                             "detail": f"the node's answer could not be "
                                       f"recorded ({failure}); the job is "
                                       f"unchanged and stays running"}
        finally:
            with self.lock:
                # ATTEMPT-scoped, like _in_flight: a slow poll finishing
                # after a newer one started must not clear the newer
                # one's marker.
                if self._polls_in_flight.get(delivery_id) == attempt:
                    del self._polls_in_flight[delivery_id]

    def _note_poll_event(self, delivery_id, event, detail):
        """Audit a REPEATING per-job poll event once per transition.

        Called with self.lock held. A clone of _note_dispatch_event
        against its OWN map: the poll pass re-visits the same running
        jobs on a timer, and an unreachable node (today's normal state
        between TD restarts) would otherwise append a line per tick
        forever. Same (event, reason) dedupe key, so a CHANGED failure
        is still a visible transition."""
        key = (event, (detail or {}).get("reason"))
        if self._poll_noted.get(delivery_id) == key:
            return
        try:
            self.db.audit("hostapp", event,
                          dict(detail, delivery_id=delivery_id))
        except Exception:
            # Never poison the dedupe with a line that was not written:
            # leave it unnoted so a later poll retries the append.
            return
        self._poll_noted[delivery_id] = key
        if len(self._poll_noted) > 2048:
            self._poll_noted.pop(next(iter(self._poll_noted)))

    def _resolve_poll(self, delivery_id, node_job_id, outcome, observed,
                      detail):
        """Phase c of poll_job -- called WITH self.lock held.

        THE ASYMMETRY WITH DISPATCH, stated once and deliberately:

            a dispatch that got no response may have EXECUTED the
            operation, so the only honest terminal is indeterminate. A
            poll is a READ. A read that failed teaches nothing at all --
            the node's own job record is durable and will answer when
            the node returns. So an unreachable or unanswered poll NEVER
            writes anything: the job stays running.

        This is not an oversight in the symmetry; inverting it would
        burn a healthy 20-minute test run every time the node blipped --
        and save_project restarts the Envoy server under itself, so the
        first poll after one is EXPECTED to be unreachable.

        Two answers do terminalise, and both carry the node's own
        evidence rather than a host guess: the node repeatedly and
        durably not knowing the job (node_forgot_job, gated by
        observations AND a grace window), and the node's own derived
        stale-running verdict.
        """
        if outcome is mcpclient.UNREACHABLE:
            return self._defer_poll(delivery_id, "poll_unreachable",
                                    "node_unreachable",
                                    {"node_job_id": node_job_id},
                                    observed,
                                    "the node's Envoy refused the "
                                    "connection; the job stays running "
                                    "and the next poll retries")
        if outcome is None:
            return self._defer_poll(delivery_id, "poll_no_response",
                                    "poll_no_response",
                                    {"node_job_id": node_job_id,
                                     "reason": "transport",
                                     "detail": (detail
                                                or "no response")[:256]},
                                    observed,
                                    "no usable answer about the node job; "
                                    "the job stays running and the next "
                                    "poll retries")
        if outcome.get("ok") is not True:
            # A protocol-level refusal (JSON-RPC error / isError): the
            # CALL failed, so we learned nothing about the job either.
            return self._defer_poll(delivery_id, "poll_no_response",
                                    "poll_no_response",
                                    {"node_job_id": node_job_id,
                                     "reason": "node_error",
                                     "detail": str(outcome.get("error")
                                                   )[:256]},
                                    observed,
                                    "the node refused the status call; the "
                                    "job stays running")
        payload = outcome.get("result")
        handle = _node_job_handle(payload)
        if handle is None:
            if _is_unknown_job_answer(payload):
                # "no job with id ..." -- RULE 2 territory. Gated on the
                # node's ACTUAL not-found answer, not on any error key:
                # this is the one path that turns a read into a terminal
                # record, and the host distrusts node-supplied data
                # everywhere else (NODE_JOB_ID_RE, the id-mismatch
                # check). An unrecognised node error defers below, like
                # every other answer we cannot read.
                return self._resolve_unknown_job(delivery_id, node_job_id,
                                                 payload, observed)
            if (isinstance(payload, dict)
                    and payload.get("error") is not None):
                return self._defer_poll(delivery_id, "poll_no_response",
                                        "poll_no_response",
                                        {"node_job_id": node_job_id,
                                         "reason": "node_error_payload",
                                         "detail": str(payload["error"]
                                                       )[:256]},
                                        observed,
                                        "the node reported its own trouble "
                                        "rather than this job's status; the "
                                        "job stays running")
            # Anything else is an answer we cannot read. Learning
            # nothing is not evidence of anything.
            return self._defer_poll(delivery_id, "poll_no_response",
                                    "poll_no_response",
                                    {"node_job_id": node_job_id,
                                     "reason": "unreadable"},
                                    observed,
                                    "the node's answer was not a job "
                                    "record; the job stays running")
        answered_id, node_status = handle
        if answered_id != node_job_id:
            # Debris (a confused node, a crossed response). Mirroring it
            # would file ANOTHER run's verdict against this delivery.
            return self._defer_poll(delivery_id, "poll_id_mismatch",
                                    "poll_id_mismatch",
                                    {"node_job_id": node_job_id,
                                     "answered": answered_id},
                                    observed,
                                    f"the node answered about "
                                    f"{answered_id!r}, not {node_job_id!r}; "
                                    f"ignored")
        if node_status == "running":
            if isinstance(payload, dict) and payload.get("stale") is True:
                # RULE 3: the NODE's own derived verdict -- its
                # completion poll chain died. The host mirrors that as
                # indeterminate carrying the node's payload; it does NOT
                # invent a second host-side staleness horizon.
                updated = self.db.mark_indeterminate(delivery_id, {
                    "reason": "node_reported_stale",
                    "detail": "the node's own record reports this run as "
                              "stale (running far past its expected "
                              "lifetime); the outcome is unobservable",
                    "node_job_id": node_job_id,
                    "node_record": _bounded_result(payload)})
                self._forget_poll(delivery_id)
                try:
                    self.db.audit("hostapp", "poll_stale_indeterminate",
                                  {"delivery_id": delivery_id,
                                   "node_job_id": node_job_id})
                except Exception:
                    pass    # the outcome is durably written; audits
                            # never alter poll state
                return 200, {"ok": True, "polled": True, "job": updated}
            # Still running: a clean observation, and nothing DECISION-
            # RELEVANT changed -- same state, same handle. The payload
            # itself always differs (the node stamps a fresh age_s, and
            # progress fields may move), so the skip deliberately trades
            # /jobs-view freshness for I/O: a running record's mirrored
            # payload can lag reality by up to POLL_MIRROR_REFRESH_S.
            # No reader decides on a running result or observed_at
            # (verified in round-2 review); stale:true and terminals
            # always write immediately. Rewriting the file every poll
            # cost 6-38 ms per poll per job, forever (measured, 200
            # running jobs). So write only when something real changed
            # or the mirror has gone stale past POLL_MIRROR_REFRESH_S.
            # The freshness comes off the RECORD, not an in-memory map:
            # no map to bound, and it survives a host restart. When we
            # do write, _apply_state reads the file anyway, so the skip
            # is strictly fewer I/O operations, never more.
            current = self.db.get_job(delivery_id)
            last = current.get("observed_at") if current else None
            unchanged = (current is not None
                         and current.get("state") == "running"
                         and current.get("node_job_id") == node_job_id
                         and isinstance(last, (int, float))
                         and (observed - last) < POLL_MIRROR_REFRESH_S)
            if unchanged:
                updated = current
            else:
                # Refresh the mirror FIRST -- if that write fails, the
                # bookkeeping must stay exactly as it was so the next
                # pass retries unpaced.
                updated = self.db.record_node_verdict(
                    delivery_id, "running", node_job_id=node_job_id,
                    observed_at=observed, result=_bounded_result(payload))
            # Clear the failure bookkeeping (a later failure is a fresh
            # transition) and pace the node.
            self._poll_noted.pop(delivery_id, None)
            self._poll_unknown.pop(delivery_id, None)
            self._set_poll_backoff(delivery_id, observed + self.poll_backoff_s)
            return 200, {"ok": True, "polled": True, "refreshed": not unchanged,
                         "job": updated}
        # done / error: the node's verdict on its own run. The terminal
        # payload is COPIED in -- the node's fetch-by-id window closes
        # at 24h, so the host's record is what survives.
        updated = self.db.record_node_verdict(
            delivery_id, node_status, node_job_id=node_job_id,
            observed_at=observed, result=_bounded_result(payload))
        self._forget_poll(delivery_id)
        try:
            self.db.audit("hostapp", "poll_finished",
                          {"delivery_id": delivery_id,
                           "node_job_id": node_job_id,
                           "status": node_status,
                           "state": updated.get("state")})
        except Exception:
            pass        # the verdict is durably written; an audit
                        # failure must never destroy it (A-15)
        return 200, {"ok": True, "polled": True, "job": updated}

    def _defer_poll(self, delivery_id, event, reason, note, observed,
                    detail):
        """Leave the job RUNNING, write nothing, audit once per
        transition, and pace the next attempt. Every non-terminal poll
        outcome funnels here, so there is exactly ONE place that could
        ever be mistakenly taught to terminalise.

        The note's own `reason` WINS over the response reason when it
        carries one: the dedupe key is (event, reason), so a
        poll_no_response that changes cause (transport -> the node
        refusing the status call) is a genuine transition the trail must
        show. Defaulting them all to the response reason collapsed
        exactly that distinction."""
        self._note_poll_event(delivery_id, event,
                              dict({"reason": reason}, **note))
        self._set_poll_backoff(delivery_id, observed + self.poll_backoff_s)
        return 409, {"ok": False, "reason": reason, "detail": detail}

    def _set_poll_backoff(self, delivery_id, not_before):
        """Pace the next poll of one job, BOUNDED. Called with the lock.

        The other two poll maps evict past 2048; this one was pruned
        only by poll_once's end-of-pass sweep -- and the drain loop is
        off by default, so a controller driving /poll by hand had
        nothing pruning it at all. Same cap, same eviction, one writer."""
        self._poll_backoff[delivery_id] = not_before
        if len(self._poll_backoff) > 2048:
            self._poll_backoff.pop(next(iter(self._poll_backoff)))

    def _set_drain_backoff(self, delivery_id, not_before):
        """Pace the next drain attempt of one job, BOUNDED. Called with
        the lock. Same rationale as _set_poll_backoff: the end-of-pass
        prune is the only other reclaim and the drain loop is off by
        default, so hand-driven /dispatch refusals grew the map one
        entry per distinct delivery forever (round-2 verify probe:
        3000 portless dispatches -> 3000 entries)."""
        self._drain_backoff[delivery_id] = not_before
        if len(self._drain_backoff) > 2048:
            self._drain_backoff.pop(next(iter(self._drain_backoff)))

    def _forget_poll(self, delivery_id):
        """Drop a settled job's poll bookkeeping. Called with the lock."""
        self._poll_backoff.pop(delivery_id, None)
        self._poll_noted.pop(delivery_id, None)
        self._poll_unknown.pop(delivery_id, None)

    def _resolve_unknown_job(self, delivery_id, node_job_id, payload,
                             observed):
        """RULE 2: the node says it has no such job.

        That is real evidence -- but not YET proof the run is lost. A
        node restarting between record flushes, the 24h retention, and
        the transient dark-job layer (a repo root the node had not
        resolved when it wrote the record) all produce one-off unknowns.
        So terminalisation needs BOTH a repeated observation and a grace
        window, and the evidence written down is the node's own message.

        A host restart resets the counter, which only ever DELAYS
        terminalisation -- safe in the direction that matters.
        """
        entry = self._poll_unknown.get(delivery_id)
        if entry is None or entry.get("node_job_id") != node_job_id:
            entry = {"first": observed, "count": 0,
                     "node_job_id": node_job_id}
        entry["count"] += 1
        entry["message"] = str(payload.get("error"))[:256]
        self._poll_unknown[delivery_id] = entry
        if len(self._poll_unknown) > 2048:
            self._poll_unknown.pop(next(iter(self._poll_unknown)))
        aged = (observed - entry["first"]) >= POLL_UNKNOWN_GRACE_S
        if entry["count"] >= POLL_UNKNOWN_MIN_OBSERVATIONS and aged:
            updated = self.db.mark_indeterminate(delivery_id, {
                "reason": "node_forgot_job",
                "detail": f"the node has no record of {node_job_id!r} "
                          f"({entry['message']}); the run may have "
                          f"completed unobserved",
                "node_job_id": node_job_id,
                "node_message": entry["message"],
                "first_unknown_at": entry["first"],
                "observations": entry["count"]})
            self._forget_poll(delivery_id)
            try:
                self.db.audit("hostapp", "poll_node_forgot_job",
                              {"delivery_id": delivery_id,
                               "node_job_id": node_job_id,
                               "observations": entry["count"]})
            except Exception:
                pass    # durably written; audits never alter poll state
            return 200, {"ok": True, "polled": True, "job": updated}
        return self._defer_poll(
            delivery_id, "poll_node_forgot_job_pending",
            "node_forgot_job_pending",
            {"node_job_id": node_job_id, "observations": entry["count"]},
            observed,
            f"the node does not know {node_job_id!r} yet "
            f"({entry['count']} of {POLL_UNKNOWN_MIN_OBSERVATIONS} "
            f"observations); the job stays running")

    def poll_once(self, stop=None):
        """Poll every currently-running node job once. Synchronous and
        directly testable; the background loop runs this before each
        dispatch pass.

        Called WITHOUT the lock (poll_job self-locks). The snapshot is
        taken lock-free for the same reason drain_once's is: jobs() is
        O(every job file on disk). A stale snapshot is safe -- poll_job
        re-reads under the lock, and a job that settled in the window is
        just a skip.

        stop: optional threading.Event checked between jobs, so a
        shutdown aborts the pass after the CURRENT poll.
        """
        running = [j["delivery_id"] for j in self.db.jobs(state="running")]
        summary = {"examined": len(running), "finished": 0, "failed": 0,
                   "running": 0, "indeterminate": 0, "unreachable": 0,
                   "deferred": 0, "skipped": 0, "errors": 0, "backoff": 0,
                   "aborted": False}
        now = self._now()
        for delivery_id in running:
            if stop is not None and stop.is_set():
                summary["aborted"] = True
                break
            with self.lock:
                held_until = self._poll_backoff.get(delivery_id)
            if held_until is not None and held_until > now:
                summary["backoff"] += 1
                continue
            try:
                code, payload = self.poll_job(delivery_id)
            except Exception:
                # One job's failure costs ONE job, never the rest of the
                # pass (drain_once learned this the hard way).
                summary["errors"] += 1
                continue
            if payload.get("polled"):
                bucket = _POLL_STATE_BUCKET.get(
                    payload.get("job", {}).get("state"))
                summary[bucket if bucket else "errors"] += 1
            else:
                bucket = _POLL_BUCKET.get(payload.get("reason"))
                if bucket is not None:
                    summary[bucket] += 1
                elif code == 200:
                    summary["skipped"] += 1   # settled in the window
                else:
                    summary["errors"] += 1
        # Prune against THIS pass's own snapshot -- never the drain
        # maps' queued view, which would wipe every poll entry. Same
        # conservative rule: keep anything still live, and keep an
        # unreadable-but-present job (get_job returns None for absent
        # AND unreadable alike).
        with self.lock:
            keep = set(running)
            for did in ((set(self._poll_backoff) | set(self._poll_noted)
                         | set(self._poll_unknown)) - keep):
                current = self.db.get_job(did)
                if current is not None and current.get("state") == "running":
                    keep.add(did)
                elif current is None and self.db.job_file_exists(did):
                    keep.add(did)
            self._poll_backoff = {k: v for k, v
                                  in self._poll_backoff.items()
                                  if k in keep}
            self._poll_noted = {k: v for k, v in self._poll_noted.items()
                                if k in keep}
            self._poll_unknown = {k: v for k, v
                                  in self._poll_unknown.items()
                                  if k in keep}
        return summary

    def drain_once(self, stop=None):
        """Dispatch every currently-queued job once. Synchronous and
        directly testable; the background loop is just this on a timer.

        Called WITHOUT the lock (dispatch_job self-locks). Both store
        snapshots are taken WITHOUT the lock, deliberately: jobs() is
        O(every job file on disk), and holding the lock across that scan
        froze every route for the duration (measured in review: tens of
        seconds cold at a few thousand files). A stale snapshot is safe
        because dispatch_job re-reads and CASes under the lock -- a job
        that resolved or got claimed in the window is just a skip.

        stop: optional threading.Event checked between jobs, so a
        shutdown aborts the pass after the CURRENT forward rather than
        after the whole queue.
        """
        # Reap claims stranded by a failed phase-c write: 'dispatching'
        # on disk with no forward in flight in THIS process can never
        # resolve on its own -- without this, such a job is invisible to
        # every route until a host restart.
        # Retry PARKED requeues first: a failed release write left a
        # never-delivered job claimed with its flight marker held. Until
        # the release lands, the reaper must skip it and the job cannot
        # retry -- so heal it before anything else in the pass.
        requeued = 0
        with self.lock:
            parked = list(self._pending_release)
        for did in parked:
            with self.lock:
                if did not in self._pending_release:
                    continue
                try:
                    released = self.db.release_claim(did)
                except Exception:
                    continue        # disk still failing; next pass
                if released is None:
                    current = self.db.get_job(did)
                    state = current.get("state") if current else None
                    if state == "dispatching" or (
                            current is None
                            and self.db.job_file_exists(did)):
                        # Unreadable, not resolved -- stay parked. An
                        # unconditional pop here handed the still-
                        # claimed job to the same-pass reaper.
                        continue
                self._pending_release.pop(did, None)
                self._in_flight.pop(did, None)
                if released is not None:
                    requeued += 1
                    try:
                        self.db.audit("hostapp",
                                      "dispatch_requeued_after_retry",
                                      {"delivery_id": did})
                    except Exception:
                        pass

        # Retry PARKED HANDOFFS on the same terms: a node job the host
        # observed START, whose running mirror could not be written.
        # Until it lands the claim is held (marker kept, reaper skipping
        # it) -- so heal it here, before the reaper runs, or a transient
        # write failure would end up terminalising a live node job.
        handoffs = 0
        with self.lock:
            pending = list(self._pending_handoff.items())
        for did, park in pending:
            with self.lock:
                if did not in self._pending_handoff:
                    continue
                try:
                    self.db.record_node_verdict(
                        did, park["node_status"],
                        node_job_id=park["node_job_id"],
                        observed_at=park["observed_at"],
                        result=park["result"])
                except Exception:
                    continue        # disk still failing; next pass
                self._pending_handoff.pop(did, None)
                self._in_flight.pop(did, None)
                handoffs += 1
                try:
                    self.db.audit("hostapp", "dispatch_handoff_recovered",
                                  {"delivery_id": did,
                                   "node_job_id": park["node_job_id"]})
                except Exception:
                    pass

        # ONE scan for both snapshots -- jobs() parses every job file on
        # disk, so two filtered calls doubled the pass's dominant cost.
        snapshot = self.db.jobs()
        stranded = 0
        for job in [j for j in snapshot if j.get("state") == "dispatching"]:
            did = job["delivery_id"]
            with self.lock:
                if did in self._in_flight:
                    continue        # a live dispatch owns it
                current = self.db.get_job(did)
                if (current is None
                        or current.get("state") != "dispatching"):
                    continue        # resolved in the window -- fine
                try:
                    self.db.mark_indeterminate(did, {
                        "reason": "claim_stranded",
                        "detail": "claimed for dispatch but no forward "
                                  "is in flight in this process; a "
                                  "recording failure likely orphaned it",
                        "operation": current.get("operation")})
                except Exception:
                    continue        # still unwritable; next pass retries
                stranded += 1       # the reap LANDED -- count it even
                try:                # if the audit append fails
                    self.db.audit("hostapp", "stranded_claim_reaped",
                                  {"delivery_id": did})
                except Exception:
                    pass

        queued = [j["delivery_id"] for j in snapshot
                  if j.get("state") == "queued"]
        summary = {"examined": len(queued), "dispatched": 0, "started": 0,
                   "indeterminate": 0, "unreachable": 0, "no_handle": 0,
                   "deferred": 0, "skipped": 0, "errors": 0, "backoff": 0,
                   "stranded": stranded, "requeued": requeued,
                   "handoffs": handoffs, "aborted": False}
        now = self._now()
        for delivery_id in queued:
            if stop is not None and stop.is_set():
                summary["aborted"] = True
                break
            with self.lock:
                held_until = self._drain_backoff.get(delivery_id)
            if held_until is not None and held_until > now:
                summary["backoff"] += 1
                continue
            try:
                code, payload = self.dispatch_job(delivery_id)
            except Exception:
                # One job's failure must cost ONE job, never the rest of
                # the pass (a leading bad job would wedge the whole
                # queue every pass -- round-4 panel).
                summary["errors"] += 1
                continue
            if payload.get("dispatched"):
                summary["dispatched"] += 1
                state = payload.get("job", {}).get("state")
                if state == "indeterminate":
                    # Surfaced separately: 16.4 says a may-have-run must
                    # never be lost from view, a plain 'dispatched' count
                    # would hide it.
                    summary["indeterminate"] += 1
                elif state == "running":
                    # An async HANDOFF, not a completed operation: the
                    # work is now running on the node and the poll pass
                    # owns its outcome.
                    summary["started"] += 1
            else:
                bucket = _DRAIN_BUCKET.get(payload.get("reason"))
                if bucket is not None:
                    summary[bucket] += 1
                elif code == 200:
                    summary["skipped"] += 1   # raced: claimed or terminal
                else:
                    summary["errors"] += 1    # unknown_job / unknown_node
        # Keep the per-job maps from outliving their jobs -- but never
        # drop an entry whose job is still LIVE. The snapshot is a
        # start-of-pass view: an entry set mid-pass (a manual /dispatch,
        # a job created after the snapshot, a file the lock-free scan
        # transiently failed to read) belongs to a real queued or
        # claimed job, and dropping it defeated the backoff and
        # re-audited the same refusal (review probe, 2026-07-31).
        with self.lock:
            keep = set(queued)
            for did in ((set(self._drain_backoff)
                         | set(self._drain_noted)) - keep):
                current = self.db.get_job(did)
                if current is not None and current.get("state") in (
                        "queued", "dispatching"):
                    keep.add(did)
                elif current is None and self.db.job_file_exists(did):
                    # UNREADABLE is not ABSENT: get_job swallows the
                    # same transient sharing violation that makes the
                    # lock-free scan miss files. Keep -- conservative.
                    keep.add(did)
            self._drain_backoff = {k: v for k, v
                                   in self._drain_backoff.items()
                                   if k in keep}
            self._drain_noted = {k: v for k, v
                                 in self._drain_noted.items()
                                 if k in keep}
        return summary

    def drain(self):
        """The /drain route: one synchronous pass over the queue."""
        return 200, {"ok": True, **self.drain_once()}

    def _audit_pass(self, kind, summary):
        """One audit line when a pass's shape CHANGES and either side of
        the change shows trouble -- so a steady state (healthy or stuck)
        costs nothing, but every degradation and every recovery leaves a
        trace (A-40). Keyed by pass kind: the drain and poll passes have
        different shapes and must not overwrite each other's baseline."""
        def troubled(s):
            # .get, not [...]: the two summaries share only some keys,
            # and a KeyError here would kill the loop thread.
            return bool(s and (s.get("errors") or s.get("unreachable")
                               or s.get("stranded") or s.get("indeterminate")
                               or s.get("no_handle")))
        with self.lock:
            previous = self._last_pass_summary.get(kind)
            self._last_pass_summary[kind] = summary
            if summary != previous and (troubled(summary)
                                        or troubled(previous)):
                self.db.audit("hostapp", f"{kind}_pass", summary)

    def _drain_loop(self, stop, interval_s):
        while not stop.wait(interval_s):
            # POLL FIRST, then dispatch. Learning what already-running
            # node jobs did before starting new work keeps a finished
            # job from being re-polled on the next tick, and keeps the
            # running set from growing across one tick. Separate
            # try/except per pass: a poll failure must not cost the
            # dispatch pass (or vice versa).
            try:
                self._audit_pass("poll", self.poll_once(stop=stop))
            except Exception as e:
                try:
                    with self.lock:
                        self.db.audit("hostapp", "poll_loop_error", {
                            "error": f"{type(e).__name__}: {e}"})
                except Exception:
                    pass
            if stop.is_set():
                continue        # stopping: do not open a new forward
            try:
                self._audit_pass("drain", self.drain_once(stop=stop))
            except Exception as e:
                # The loop must survive anything -- a dead drain thread
                # silently ends autonomous dispatch.
                try:
                    with self.lock:
                        self.db.audit("hostapp", "drain_loop_error", {
                            "error": f"{type(e).__name__}: {e}"})
                except Exception:
                    pass

    def start_drain_loop(self, interval_s=2.0):
        """Start the autonomous dispatcher: a daemon thread draining the
        queue every interval_s seconds. OPT-IN (nothing starts it unless
        asked -- main() wires it to --drain-interval). Returns True when
        started, False when a loop is already running -- including one a
        timed-out stop could not retire; the handle is kept alive
        precisely so this guard and status() stay honest."""
        with self.lock:
            if (self._drain_thread is not None
                    and self._drain_thread.is_alive()):
                return False
            # Audit BEFORE publishing any handle: if the append fails,
            # nothing was half-started. And start() INSIDE the lock:
            # publishing a not-yet-started thread opened a window where
            # a concurrent stop joined an unstarted thread
            # (RuntimeError) or a second start saw is_alive() False and
            # ran two loops (review probe, 2026-07-31).
            try:
                self.db.audit("hostapp", "drain_loop_started",
                              {"interval_s": interval_s})
            except Exception:
                pass    # an audit failure must not block the loop
            stop = threading.Event()
            thread = threading.Thread(
                target=self._drain_loop, args=(stop, interval_s),
                daemon=True, name="convoy-drain")
            # start() BEFORE publishing: a start() failure must not leave
            # handles pointing at a never-started thread (join on one
            # raises RuntimeError in every stop path).
            thread.start()
            self._drain_stop = stop
            self._drain_thread = thread
        return True

    def stop_drain_loop(self, timeout_s=None):
        """Stop the drain loop and wait for it to exit. Returns True when
        the loop is verifiably stopped (or none was running), False when
        it did not exit within the bound.

        On False the handles are KEPT: status() keeps reporting the live
        loop and start_drain_loop keeps refusing, instead of lying that
        the loop is gone and letting a second one start over it. The
        default bound covers one full forward (drain_once checks the
        stop event between jobs, so a stopping pass aborts after the
        CURRENT forward, never the whole queue). Safe to call when no
        loop is running."""
        with self.lock:
            thread = self._drain_thread
            if self._drain_stop is not None:
                self._drain_stop.set()
        if thread is None:
            return True
        if timeout_s is None:
            # TWO forwards, not one: a tick can be inside a poll forward
            # AND then a dispatch forward, and each pass only checks the
            # stop event BETWEEN jobs. Bounding to a single timeout made
            # a healthy stop report failure.
            timeout_s = 2 * mcpclient.DEFAULT_TIMEOUT_S + 5.0
        thread.join(timeout=timeout_s)
        if thread.is_alive():
            with self.lock:
                self.db.audit("hostapp", "drain_loop_stop_timeout",
                              {"timeout_s": timeout_s})
            return False
        with self.lock:
            if self._drain_thread is thread:
                self._drain_thread = None
                self._drain_stop = None
            self.db.audit("hostapp", "drain_loop_stopped", {})
        return True

    # -- the guarded request path (Phase 1 completion) ------------------

    def _refuse(self, source, reason, detail, code, node=None, extra=None):
        """Audit a gate denial and shape its response (A-39: denials, not
        just successes, leave a trace)."""
        record = {"reason": reason, "detail": str(detail)[:256]}
        if node is not None:
            record["node_id"] = node["node_id"]
        record.update(extra or {})
        try:
            self.db.audit("hostapp", f"{source}_refused", record)
        except Exception:
            # A refusal the trail could not record is still a refusal:
            # answering the named 4xx beats escalating to an unnamed,
            # equally-unaudited 500 (round-4 panel).
            pass
        payload = {"ok": False, "reason": reason, "detail": detail}
        return code, payload

    def _gate_operation(self, node, operation, controller_id, source,
                        expected_runtime_id=None):
        """Registry gate (A-1), runtime precondition (A-22), and lease
        gate (A-17) -- shared by BOTH job-creating paths, so /jobs and
        /envelope can never diverge into two authorities. Returns None
        when allowed, or an audited (code, payload) refusal.
        """
        entry = self.operations.get(operation)
        if entry is None:
            return self._refuse(
                source, "operation_not_exposed",
                f"{operation!r} is not in this host's operation registry",
                403, node, {"operation": operation[:MAX_OPERATION_CHARS]})
        gating = gating_of(entry)
        if gating["executes_arbitrary_code"]:
            return self._refuse(
                source, "operation_not_relayable",
                f"{operation!r} executes arbitrary code; refused until "
                f"the TD-Python gate exists (A-1)",
                403, node, {"operation": operation[:MAX_OPERATION_CHARS]})
        # A-22: an operation that may act on stale state must name the
        # run it addressed. On the envelope path verify_envelope has
        # already enforced this over the SIGNED field; here it also
        # covers the local path, whose body is unsigned -- one rule for
        # both, rather than a precondition the local path can skip.
        if gating["runtime_required"]:
            if not expected_runtime_id:
                return self._refuse(
                    source, "runtime_id_required",
                    f"{operation!r} may act on stale state and MUST carry "
                    f"expected_runtime_id (A-22)",
                    400, node, {"operation": operation[:MAX_OPERATION_CHARS]})
            current = node.get("runtime_id")
            if not current:
                return self._refuse(
                    source, "runtime_unverifiable",
                    "expected_runtime_id was set but this node has no "
                    "known runtime to check it against",
                    409, node)
            if expected_runtime_id != current:
                return self._refuse(
                    source, "runtime_changed",
                    f"addressed runtime {expected_runtime_id!r}, node is "
                    f"now {current!r}",
                    409, node)
        now = self._now()
        if controller_id and gating["mutating"]:
            # Issuing a MUTATION proves the controller is alive. Reads
            # deliberately do not: a read needs no lease, so heartbeating
            # on one would let any caller keep a dead controller's lease
            # standing by naming it (controller_id is self-asserted).
            # A read-only controller keeps itself alive via /heartbeat.
            self.leases.heartbeat(controller_id, now)
        try:
            self.leases.authorize(node["node_id"], controller_id,
                                  gating["mutating"], now)
        except controllers.LeaseError as e:
            code, payload = self._refuse(
                source, e.reason, e.detail,
                _REFUSAL_HTTP.get(e.reason, 409), node,
                {"operation": operation[:MAX_OPERATION_CHARS],
                 "controller_id": controller_id[:MAX_ID_CHARS]})
            payload["holder"] = e.holder
            return code, payload
        return None

    def submit_envelope(self, body):
        """Verify a signed Convoy/1 envelope and enqueue it as a durable
        job. THE guarded request path: signature -> registry -> leases ->
        persist-before-acknowledge, refusals structured and audited.
        """
        envelope = body.get("envelope")
        if not isinstance(envelope, dict):
            return self._refuse("envelope", "malformed",
                                "body must carry an 'envelope' object", 400)
        # PRE-VERIFICATION READS. Two fields are read before the
        # signature is checked, and both only SELECT, never decide:
        # target_node_id selects the node record (and so the convoy PSK
        # to verify against -- without it no verification is possible at
        # all), and operation selects the registry entry whose
        # runtime_required flag sets verification STRICTNESS. Both are
        # inside the signed subset, so a tampered value cannot survive
        # the signature check that follows; and an unknown operation
        # yields the LESS strict verify, then is refused outright by the
        # registry gate afterwards. Every other field is used only after
        # verification.
        try:
            target = text_field(envelope, "target_node_id")
            operation = text_field(envelope, "operation",
                                   limit=MAX_OPERATION_CHARS)
        except Malformed as e:
            return self._refuse("envelope", "malformed", e.detail, 400)
        node = self.directory.lookup(target)
        if node is None:
            return self._refuse("envelope", "unknown_node", target, 404)
        # ensure (not read): self-heals a register that predates PSK
        # minting; a fresh key can never validate an old signature, so
        # healing here can only produce a refusal, never an acceptance.
        signer = protocol.HmacSigner(
            self.db.ensure_convoy_psk(node["convoy_id"]))
        entry = self.operations.get(operation)
        gating = gating_of(entry) if entry else None
        # The A-22 precondition is asked of RELAYABLE operations only.
        # An unaudited or unknown operation is refused outright by the
        # registry gate below, and demanding expected_runtime_id first
        # would answer "you forgot a field" to a request whose real
        # problem is that it may never run here at all.
        runtime_required = bool(gating
                                and not gating["executes_arbitrary_code"]
                                and gating["runtime_required"])
        try:
            # my_node_id is the record we looked up BY the envelope's
            # target id, so wrong_target cannot fire here -- it becomes
            # meaningful when a node verifies for itself (Phase 2+). The
            # real unknown-target protection is the lookup refusal above.
            protocol.verify_envelope(
                envelope, signer, node["convoy_id"], node["node_id"],
                my_runtime_id=node.get("runtime_id"), now=self._now(),
                runtime_required=runtime_required)
        except protocol.EnvelopeRejected as e:
            return self._refuse(
                "envelope", e.reason, e.detail,
                _REFUSAL_HTTP.get(e.reason, 400), node,
                {"operation": operation[:MAX_OPERATION_CHARS],
                 "request_id": str(envelope.get("request_id"))[:MAX_ID_CHARS]})
        # verify_envelope requires controller_id/operation non-empty, but
        # NOT idempotency_key -- and the job store raises on a missing
        # one. A signed-but-malformed key must be a named refusal, not a
        # 500.
        try:
            idempotency_key = text_field(envelope, "idempotency_key")
            controller_id = text_field(envelope, "controller_id")
            # Read AFTER verification, like the two above: the signature
            # covers arguments_sha256, so a tampered value never reaches
            # here -- but a SIGNED non-dict does, and it is refused for
            # the same reason the local path refuses it. One rule for
            # both create paths, never two authorities.
            arguments = dict_field(envelope, "arguments")
        except Malformed as e:
            return self._refuse("envelope", "malformed", e.detail, 400, node)
        refusal = self._gate_operation(
            node, operation, controller_id, source="envelope",
            expected_runtime_id=envelope.get("expected_runtime_id"))
        if refusal is not None:
            return refusal
        job, created = self.db.create_job(
            idempotency_key, node["node_id"], operation,
            arguments, convoy_id=node["convoy_id"],
            expected_runtime_id=envelope.get("expected_runtime_id"))
        # The ack carries the HOST's delivery_id (cj_...), the id of the
        # routing record -- NOT A-22's target-minted job_id, which is the
        # node's own job_<8hex> and does not exist until a node accepts
        # and runs the work (job["node_job_id"], None here). Keeping the
        # two distinct is A-15's "two records": one for delivery, one for
        # execution.
        return 200, {"ok": True, "created": created, "job": job}

    # -- capability manifest (A-23) --------------------------------------

    def build_manifest(self, node_id=None):
        """The manifest of operations this host will accept -- identical
        for every node in Phase 1, bound to a node_id when serving a
        per-node view. Digests cover name/schema/gating/side-effects,
        never source bytes or build numbers (A-23)."""
        manifest = capabilities.CapabilityManifest(protocol.PROTOCOL, node_id)
        for name in sorted(self.operations):
            entry = self.operations[name]
            # Whether an operation is ASYNC is compatibility-relevant: a
            # controller that expects a result and gets a job handle is
            # talking to a host it does not understand. Folded in only
            # when set, so the operations that predate the async slice
            # keep their existing digests byte-identical.
            side_effects = dict(entry.get("side_effects") or {})
            if entry.get("async_job"):
                side_effects["async_job"] = True
            # gating_of, not entry.get: the digest must describe the
            # gating that is actually ENFORCED, defaults included, or a
            # controller could match digests with a host that treats the
            # same entry more permissively.
            manifest.add(name, capabilities.operation_digest(
                name,
                schema=entry.get("schema"),
                gating=gating_of(entry),
                side_effects=side_effects))
        return manifest

    def get_manifest(self, node_id=None):
        if node_id is not None and self.directory.lookup(node_id) is None:
            return 404, {"ok": False, "reason": "unknown_node",
                         "detail": str(node_id)[:64]}
        manifest = self.build_manifest(node_id)
        return 200, {"ok": True, "host_id": self.host_id,
                     "manifest": manifest.to_dict()}

    # -- convoy PSK issuance (Phase 1 local key distribution) ------------

    def issue_convoy_psk(self, body):
        """Hand the convoy's group signing key to a LOCAL caller.

        Phase 1 trust boundary, stated plainly: loopback + the per-install
        IPC token IS the admission control -- any process that can read
        the token file already owns this OS user. A convoy must have
        registered a node here before its key is issuable; Phase 3
        replaces PSK distribution with per-host keypairs + pinning.
        """
        try:
            convoy_id = text_field(body, "convoy_id")
        except Malformed as e:
            return self._refuse("psk", "malformed", e.detail, 400)
        psk = self.db.convoy_psk(convoy_id)
        if not psk:
            return self._refuse("psk", "unknown_convoy", convoy_id, 404)
        self.db.audit("hostapp", "convoy_psk_issued",
                      {"convoy_id": convoy_id})
        return 200, {"ok": True, "convoy_id": convoy_id, "psk": psk}

    # -- controllers and leases (A-16/A-17) -------------------------------

    def heartbeat_controller(self, body):
        now = self._now()
        try:
            controller_id = text_field(body, "controller_id")
            label = text_field(body, "label", required=False, limit=128)
        except Malformed as e:
            return self._refuse("heartbeat", "malformed", e.detail, 400)
        try:
            self.leases.heartbeat(controller_id, now, label=label)
        except controllers.LeaseError as e:
            return self._refuse("heartbeat", e.reason, e.detail, 400)
        # Reap on a WRITE path too. reap() ran only on GET /leases, so a
        # caller that never listed leases could grow the controller table
        # without bound; the cost is one pass over a handful of entries.
        self.leases.reap(now)
        return 200, {"ok": True}

    def acquire_lease(self, body):
        now = self._now()
        try:
            controller_id = text_field(body, "controller_id")
            node_id = text_field(body, "node_id")
            mode = (text_field(body, "mode", required=False)
                    or controllers.LEASE_SHARED)
        except Malformed as e:
            return self._refuse("lease", "malformed", e.detail, 400)
        ttl_s = body.get("ttl_s")
        if ttl_s is not None and (isinstance(ttl_s, bool)
                                  or not isinstance(ttl_s, (int, float))
                                  or not math.isfinite(ttl_s)
                                  or ttl_s <= 0):
            # NaN is the trap here: it IS a float and `nan <= 0` is
            # False, so it passed -- and `now + nan` is nan, which
            # `expires > now` then reads as ALREADY EXPIRED. The caller
            # got 200 and a lease that holds nothing: the phantom the
            # unknown_node check below exists to prevent.
            return self._refuse("lease", "malformed",
                                "ttl_s must be a positive finite number",
                                400)
        if self.directory.lookup(node_id) is None:
            # A lease names a real node -- a typo must not mint a
            # phantom lease that blocks nobody and reassures its holder.
            return self._refuse("lease", "unknown_node", node_id, 404)
        try:
            lease = self.leases.acquire(node_id, controller_id, mode, now,
                                        ttl_s=ttl_s)
        except controllers.LeaseError as e:
            code, payload = self._refuse(
                "lease", e.reason, e.detail,
                400 if e.reason in ("bad_mode",
                                    "malformed_controller") else 409,
                extra={"node_id": node_id,
                       "controller_id": controller_id[:MAX_ID_CHARS]})
            payload["holder"] = e.holder
            return code, payload
        self.db.audit("hostapp", "lease_acquired",
                      {"node_id": node_id, "mode": lease["mode"],
                       "controller_id": controller_id[:MAX_ID_CHARS]})
        self.leases.reap(now)
        return 200, {"ok": True, "lease": lease}

    def release_lease(self, body):
        try:
            controller_id = text_field(body, "controller_id")
            node_id = text_field(body, "node_id")
        except Malformed as e:
            return self._refuse("lease", "malformed", e.detail, 400)
        released = self.leases.release(node_id, controller_id)
        if released:
            self.db.audit("hostapp", "lease_released",
                          {"node_id": node_id,
                           "controller_id": str(controller_id)[:64]})
        # Idempotent by contract: releasing a hold you do not have is a
        # clean no-op, so cleanup paths can always fire it.
        return 200, {"ok": True, "released": released}

    def list_leases(self):
        now = self._now()
        self.leases.reap(now)          # opportunistic GC, not correctness
        return 200, {"ok": True, "leases": self.leases.live_leases(now)}


def _reject_json_constant(name):
    raise ValueError(f"{name} is not permitted in a request body")


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            pass

        def _send(self, code, payload):
            try:
                # allow_nan=False: Python's json emits BARE NaN /
                # Infinity tokens, which are not JSON and which strict
                # parsers reject -- a response no client can read is a
                # failure, so it becomes a named 500 instead.
                body = json.dumps(payload, allow_nan=False).encode("utf-8")
            except ValueError:
                code = 500
                body = json.dumps({"ok": False, "reason": "internal_error",
                                   "detail": "unserializable response"
                                   }).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authenticated(self):
            provided = self.headers.get(TOKEN_HEADER) or ""
            # compare_digest raises TypeError on non-ASCII str, and
            # http.client decodes headers as iso-8859-1 -- so a single
            # 0xFF byte from an UNAUTHENTICATED caller crashed the auth
            # check itself (no 401, dead handler thread). Compare bytes.
            try:
                provided_bytes = provided.encode("ascii")
            except UnicodeEncodeError:
                return False
            return hmac.compare_digest(provided_bytes,
                                       app.token.encode("ascii"))

        def _read_body(self):
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return None
            if length <= 0 or length > MAX_BODY_BYTES:
                return None
            try:
                # parse_constant rejects JSON's NaN / Infinity /
                # -Infinity tokens, which Python accepts by default.
                # They are the fail-open vector for every numeric guard
                # downstream: `nan <= 0` and `nan > now` are BOTH False,
                # so a NaN slips past minimum checks and expiry checks
                # alike. Refuse them at the door, once.
                return json.loads(self.rfile.read(length).decode("utf-8"),
                                  parse_constant=_reject_json_constant)
            except ValueError:      # JSONDecodeError and UnicodeDecodeError
                return None

        def do_GET(self):
            if self.path == "/health":
                # The ONLY unauthenticated route: liveness + IDENTITY, no
                # secrets. host_id lets a client confirm it reached the
                # right host app (not a recycled pid) BEFORE it sends the
                # IPC token -- see convoy_hostprobe identity confirmation.
                self._send(200, {"ok": True, "protocol": "convoy-host/1",
                                 "host_id": app.host_id})
                return
            if not self._authenticated():
                self._send(401, {"ok": False, "reason": "unauthenticated"})
                return
            try:
                with app.lock:
                    if self.path == "/status":
                        code, payload = 200, app.status()
                    elif self.path == "/nodes":
                        code, payload = 200, app.list_nodes()
                    elif self.path == "/manifest":
                        code, payload = app.get_manifest()
                    elif self.path.startswith("/manifest/"):
                        code, payload = app.get_manifest(
                            self.path[len("/manifest/"):])
                    elif self.path == "/leases":
                        code, payload = app.list_leases()
                    elif self.path.startswith("/jobs/"):
                        code, payload = app.get_job(
                            self.path[len("/jobs/"):])
                    else:
                        code, payload = 404, {"ok": False,
                                              "reason": "not_found"}
            except Exception as e:
                # LAST RESort, not the refusal mechanism: every expected
                # bad input is a named, audited 4xx before it reaches
                # here. This exists so an unforeseen bug cannot kill the
                # handler with NO response at all (the dead-thread
                # failure class the panel proved on the auth path).
                code, payload = 500, {"ok": False,
                                      "reason": "internal_error",
                                      "detail": type(e).__name__}
            self._send(code, payload)

        def do_POST(self):
            # Authenticate BEFORE parsing the body: an unauthenticated
            # caller gets no parser surface at all.
            if not self._authenticated():
                self._send(401, {"ok": False, "reason": "unauthenticated"})
                return
            body = self._read_body()
            if not isinstance(body, dict):
                self._send(400, {"ok": False, "reason": "malformed"})
                return
            try:
                # /dispatch, /drain and /poll are handled OUTSIDE the
                # lock block: they self-lock in phases so the forward
                # I/O runs lock-free, and threading.Lock is not reentrant
                # -- wrapping these in `with app.lock:` would deadlock on
                # the first claim. They still flow into the SINGLE _send
                # below, like every other route.
                if self.path == "/dispatch":
                    code, payload = app.dispatch_job(
                        body.get("delivery_id"))
                elif self.path == "/drain":
                    code, payload = app.drain()
                elif self.path == "/poll":
                    code, payload = app.poll_job(body.get("delivery_id"))
                else:
                    code, payload = self._post_locked(body)
            except Exception as e:      # same last-resort contract
                code, payload = 500, {"ok": False,
                                      "reason": "internal_error",
                                      "detail": type(e).__name__}
            self._send(code, payload)

        def _post_locked(self, body):
            with app.lock:
                if self.path == "/register":
                    return app.register_node(body)
                if self.path == "/unregister":
                    return app.unregister_node(body)
                if self.path == "/remint":
                    return app.remint_node(body)
                if self.path == "/jobs":
                    return app.create_job(body)
                if self.path == "/envelope":
                    return app.submit_envelope(body)
                if self.path == "/psk":
                    return app.issue_convoy_psk(body)
                if self.path == "/leases":
                    return app.acquire_lease(body)
                if self.path == "/leases/release":
                    return app.release_lease(body)
                if self.path == "/heartbeat":
                    return app.heartbeat_controller(body)
                return 404, {"ok": False, "reason": "not_found"}

    return Handler


def serve(app, port=0):
    """Bind loopback, write the portfile, serve until shutdown.

    port=0 lets the OS pick -- clients find us via the portfile, so a
    fixed port (and its collision/failure modes) is never needed locally.
    """
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))
    actual_port = server.server_address[1]
    platform_mod.write_portfile(app.data_dir, actual_port, os.getpid(),
                                app.host_id)
    return server, actual_port


def main(argv=None):
    parser = argparse.ArgumentParser(description="embody-convoy host app")
    parser.add_argument("--data-dir", default=None,
                        help="state directory (default: per-user app dir)")
    parser.add_argument("--port", type=int, default=0,
                        help="loopback port (default: OS-assigned)")
    parser.add_argument("--drain-interval", type=float, default=0.0,
                        help="seconds between autonomous ticks -- each "
                             "polls running node jobs, then dispatches "
                             "queued ones (0 = off; per-call only)")
    args = parser.parse_args(argv)

    directory = args.data_dir or platform_mod.data_dir()
    app = HostApp(directory)
    server, port = serve(app, args.port)
    if args.drain_interval > 0:
        app.start_drain_loop(args.drain_interval)
    sys.stderr.write(
        f"embody-convoy host {app.host_id[:8]} on 127.0.0.1:{port} "
        f"(data: {directory})\n")
    sys.stderr.flush()

    # A supervisor stops us with SIGTERM (Scheduled Task / LaunchAgent,
    # A-36). Without a handler, Python does not unwind -- the `finally`
    # below never runs and the portfile outlives the process, pointing
    # clients at a dead port. Handle it so the COMMON stop is clean;
    # clients still verify liveness, because SIGKILL/power-loss can
    # never be handled here.
    def _stop(signum, _frame):
        threading.Thread(target=server.shutdown, daemon=True).start()

    for signame in ("SIGTERM", "SIGINT", "SIGBREAK"):
        sig = getattr(signal, signame, None)
        if sig is not None:
            try:
                signal.signal(sig, _stop)
            except (ValueError, OSError):
                pass        # not the main thread, or unsupported here

    try:
        server.serve_forever()
    finally:
        try:
            app.stop_drain_loop()
        except Exception:
            # Shutdown hygiene must not depend on the loop stopping
            # cleanly: a raise here skipped clear_portfile and left a
            # portfile pointing at a dead port.
            pass
        platform_mod.clear_portfile(directory)
        app.db.audit("hostapp", "stopped", {})
        app.db.close()


if __name__ == "__main__":
    main()
