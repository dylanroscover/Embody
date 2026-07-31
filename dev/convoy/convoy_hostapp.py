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
        "runtime_required": False,
        "side_effects": {},
    },
    "query_network": {
        "schema": {"parent_path": "string", "recursive": "bool?"},
        "mutating": False,
        "executes_arbitrary_code": False,
        "runtime_required": False,
        "side_effects": {},
    },
    "capture_top": {
        "schema": {"op_path": "string", "format": "string?"},
        "mutating": False,
        "executes_arbitrary_code": False,
        "runtime_required": False,
        "side_effects": {"cooks": True},
    },
    "set_op_position": {
        "schema": {"op_path": "string", "x": "number?", "y": "number?"},
        "mutating": True,
        "executes_arbitrary_code": False,
        "runtime_required": False,
        "side_effects": {"layout": True},
    },
}

# The strict reading of a registry entry: anything a registry entry does
# not say is assumed to be the most dangerous answer. `executes_arbitrary
# _code` True is A-1 verbatim; `mutating` True demands the exclusive
# lease; `runtime_required` True demands the A-22 precondition.
_GATING_DEFAULTS = {
    "executes_arbitrary_code": True,
    "mutating": True,
    "runtime_required": True,
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


class HostApp:
    """All state behind one lock: a host app is coordination, not
    throughput. Every handler acquires it around the whole request --
    EXCEPT dispatch_job and drain/drain_once, which SELF-lock in phases
    so the forward I/O runs outside the lock. Never call those from
    inside `with app.lock:` -- threading.Lock is not reentrant, and the
    double-acquire deadlocks the handler thread."""

    def __init__(self, directory_path, now=None, forwarder=None):
        self.data_dir = directory_path
        self._now = now or time.time
        self.started = self._now()
        self.token = platform_mod.ensure_ipc_token(directory_path)
        self.db = hoststore.HostStore(directory_path, now=now)
        self.host_id = self.db.host_id()
        self.directory, self.quarantined = self.db.load_directory()
        # The dispatch SEAM: how the host executes a queued job against a
        # node's Envoy. Signature (port, operation, arguments) -> a dict
        # {"ok": bool, "result"/"error": ...} for an observed node result,
        # or None on a transport failure (-> indeterminate). The default
        # is the minimal MCP client (convoy_mcpclient.forward), which
        # handles the synchronous request/response tool call the dispatcher
        # needs today; tests inject their own. The robust transport
        # (streaming, long-running node jobs, reconnection) is the A-46
        # rework, and this seam is exactly where it plugs in.
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
        # delivery_ids whose UNREACHABLE requeue write FAILED: the job is
        # still claimed on disk, its flight marker is deliberately kept
        # (the reaper must not resolve a never-delivered job), and the
        # drain pass retries the release until the disk heals.
        self._pending_release = {}
        self._last_drain_summary = None
        self.drain_backoff_s = 30.0
        self.db.audit("hostapp", "started", {"host_id": self.host_id})

    # -- request handlers (called WITH self.lock held -- except the
    #    self-locking dispatch_job / drain, see their docstrings) -------

    def status(self):
        return {
            "ok": True,
            "protocol": "convoy-host/1",
            "host_id": self.host_id,
            "nodes": len(self.directory.nodes()),
            "jobs_queued": len(self.db.jobs(state="queued")),
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
            idempotency_key, node_id, operation, body.get("arguments"),
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
                self._drain_backoff[delivery_id] = (self._now()
                                                    + self.drain_backoff_s)
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
                self._drain_backoff[delivery_id] = (self._now()
                                                    + self.drain_backoff_s)
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
                    self._drain_backoff[delivery_id] = (
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
                self._drain_backoff[delivery_id] = (self._now()
                                                    + self.drain_backoff_s)
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
            self._flight_counter += 1
            attempt = self._flight_counter
            self._in_flight[delivery_id] = attempt
            try:
                self.db.audit("hostapp", "dispatch_claimed",
                              {"delivery_id": delivery_id,
                               "operation": job["operation"], "port": port})
            except Exception:
                # An audit failure must never divert or strand a
                # dispatch: the claim is on disk and the in-flight
                # marker is set -- the forward proceeds.
                pass
            operation = job["operation"]
            arguments = job.get("arguments") or {}

        try:
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
                                                  detail)
            except Exception as e:
                return self._downgrade_failed_recording(delivery_id,
                                                        operation, e)
        finally:
            with self.lock:
                # Remove ONLY this attempt's marker. After a release back
                # to queued, another attempt may already have re-claimed
                # this job and own a newer marker -- erasing it would let
                # the reaper mark a live forward stranded. And a PARKED
                # release (failed requeue write) keeps its marker on
                # purpose: the claim is still on disk for a job that was
                # never delivered, and the reaper must not resolve it
                # before the drain pass retries the release.
                if (delivery_id not in self._pending_release
                        and self._in_flight.get(delivery_id) == attempt):
                    del self._in_flight[delivery_id]

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

    def _resolve_dispatch(self, delivery_id, operation, outcome, observed,
                          detail):
        """Phase c of dispatch_job -- called WITH self.lock held."""
        if outcome is mcpclient.UNREACHABLE:
            # The node refused the connection: the request was never
            # delivered, so the op did NOT run. Release the claim -- the
            # job returns to QUEUED to retry. A transient node-down must
            # never burn a job to indeterminate (or, worse, a fabricated
            # verdict).
            try:
                released = self.db.release_claim(delivery_id)
            except Exception as e:
                # The REQUEUE write failed. The op was never delivered,
                # so this must NOT decay to indeterminate (the forbidden
                # collapse -- round-3 panel reproduced it under natural
                # file contention). Park the release: this attempt's
                # flight marker is kept (the finally skips parked ids)
                # so the reaper cannot resolve the claim, and the drain
                # pass retries the release until the disk heals. Named
                # residual: a host that DIES before the retry lands
                # leaves a claim the boot sweep resolves to
                # indeterminate -- never-delivered knowledge cannot
                # outlive the process without a write, which is exactly
                # what is failing.
                detail = f"{type(e).__name__}: {e}"
                self._pending_release[delivery_id] = detail
                try:
                    self.db.audit("hostapp", "dispatch_release_failed",
                                  {"delivery_id": delivery_id,
                                   "detail": detail[:256]})
                except Exception:
                    pass
                return 503, {"ok": False, "reason": "store_unavailable",
                             "detail": f"the node refused the connection "
                                       f"but the requeue write failed "
                                       f"({detail}); the drain loop will "
                                       f"retry the requeue"}
            if released is None:
                # The CAS did not release. Distinguish UNREADABLE from
                # GONE before concluding anything: get_job swallows read
                # errors into None, and treating a transiently
                # unreadable file as a lost claim handed a
                # never-delivered job to the reaper (round-4 panel).
                current = self.db.get_job(delivery_id)
                state = current.get("state") if current else None
                if state == "dispatching" or (
                        current is None
                        and self.db.job_file_exists(delivery_id)):
                    # Still claimed on disk (or unreadable-but-present,
                    # conservatively the same): the CAS's READ failed,
                    # not the claim. Park it exactly like a failed
                    # release write.
                    self._pending_release[delivery_id] = (
                        "release CAS could not read the record")
                    try:
                        self.db.audit("hostapp", "dispatch_release_failed",
                                      {"delivery_id": delivery_id,
                                       "detail": "release CAS read failure"})
                    except Exception:
                        pass
                    return 503, {"ok": False,
                                 "reason": "store_unavailable",
                                 "detail": "the node refused the "
                                           "connection but the requeue "
                                           "could not read the record; "
                                           "the drain loop will retry"}
                state = state if current else "missing"
                try:
                    self.db.audit("hostapp", "dispatch_claim_lost",
                                  {"delivery_id": delivery_id,
                                   "state": state})
                except Exception:
                    pass
                return 409, {"ok": False, "reason": "claim_lost",
                             "detail": f"the node refused the connection, "
                                       f"but this dispatcher no longer "
                                       f"held the claim; the job is now "
                                       f"{state!r}"}
            self._note_dispatch_event(delivery_id, "dispatch_unreachable",
                                      {"operation": operation})
            self._drain_backoff[delivery_id] = (observed
                                                + self.drain_backoff_s)
            return 409, {"ok": False, "reason": "node_unreachable",
                         "detail": "the node's Envoy refused the "
                                   "connection; the job stays queued "
                                   "to retry"}
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
        summary = {"examined": len(queued), "dispatched": 0,
                   "indeterminate": 0, "unreachable": 0, "deferred": 0,
                   "skipped": 0, "errors": 0, "backoff": 0,
                   "stranded": stranded, "requeued": requeued,
                   "aborted": False}
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
                if payload.get("job", {}).get("state") == "indeterminate":
                    # Surfaced separately: 16.4 says a may-have-run must
                    # never be lost from view, a plain 'dispatched' count
                    # would hide it.
                    summary["indeterminate"] += 1
            elif payload.get("reason") in ("node_unreachable",
                                           "claim_lost"):
                summary["unreachable"] += 1
            elif payload.get("reason") in ("node_endpoint_unknown",
                                           "runtime_changed",
                                           "operation_not_exposed",
                                           "operation_not_relayable"):
                summary["deferred"] += 1
            elif code == 200:
                summary["skipped"] += 1    # raced: claimed or terminal
            else:
                summary["errors"] += 1     # unknown_job / unknown_node
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

    def _audit_drain_pass(self, summary):
        """One audit line when a pass's shape CHANGES and either side of
        the change shows trouble -- so a steady state (healthy or stuck)
        costs nothing, but every degradation and every recovery leaves a
        trace (A-40)."""
        def troubled(s):
            return bool(s and (s["errors"] or s["unreachable"]
                               or s["stranded"] or s["indeterminate"]))
        with self.lock:
            previous = self._last_drain_summary
            self._last_drain_summary = summary
            if summary != previous and (troubled(summary)
                                        or troubled(previous)):
                self.db.audit("hostapp", "drain_pass", summary)

    def _drain_loop(self, stop, interval_s):
        while not stop.wait(interval_s):
            try:
                self._audit_drain_pass(self.drain_once(stop=stop))
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
            timeout_s = mcpclient.DEFAULT_TIMEOUT_S + 5.0
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
        except Malformed as e:
            return self._refuse("envelope", "malformed", e.detail, 400, node)
        refusal = self._gate_operation(
            node, operation, controller_id, source="envelope",
            expected_runtime_id=envelope.get("expected_runtime_id"))
        if refusal is not None:
            return refusal
        job, created = self.db.create_job(
            idempotency_key, node["node_id"], operation,
            envelope.get("arguments"), convoy_id=node["convoy_id"],
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
            # gating_of, not entry.get: the digest must describe the
            # gating that is actually ENFORCED, defaults included, or a
            # controller could match digests with a host that treats the
            # same entry more permissively.
            manifest.add(name, capabilities.operation_digest(
                name,
                schema=entry.get("schema"),
                gating=gating_of(entry),
                side_effects=entry.get("side_effects")))
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
                # /dispatch and /drain are handled OUTSIDE the lock
                # block: dispatch_job self-locks in phases so the forward
                # I/O runs lock-free, and threading.Lock is not reentrant
                # -- wrapping these in `with app.lock:` would deadlock on
                # the first claim. They still flow into the SINGLE _send
                # below, like every other route.
                if self.path == "/dispatch":
                    code, payload = app.dispatch_job(
                        body.get("delivery_id"))
                elif self.path == "/drain":
                    code, payload = app.drain()
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
                        help="seconds between autonomous queue drains "
                             "(0 = off; dispatch is per-call only)")
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
