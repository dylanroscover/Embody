"""Controllers and leases for Convoy (A-16, A-17). Pure policy, no I/O.

Vocabulary, plainly:

  CONTROLLER -- a session driving the mesh (a Claude Code session, a
    ConvoyExt UI). It has an id and a heartbeat; when the heartbeat goes
    stale the controller is presumed gone and its leases fall.

  LEASE -- a controller's claim on a node. This is the cross-machine
    analogue of Embody's shipped claim_scope (which coordinates SESSIONS
    on ONE machine, inside TD); a Convoy lease coordinates CONTROLLERS
    across machines, and lives in the host app -- a separate process with
    no TD, so it cannot reuse claim_scope's in-TD implementation, only
    its cooperative, time-bounded shape. It expires on TTL or when the
    controller stops heartbeating, so a crashed controller never wedges a
    node forever.

Two lease modes (A-17), as a proper reader/writer lock:
  exclusive -- one holder. No other controller may hold ANY lease, and no
    other controller may issue a mutating operation.
  shared    -- MANY holders at once (this is why the store keeps a SET of
    holders per node, not one slot). A shared holder may READ; it may not
    mutate. An exclusive lease cannot be taken while any shared holder is
    live. Reads coexist with everyone.

THREAD-SAFETY: this module is NOT internally locked. It is pure policy
and relies on its single caller -- the host app -- serializing every
method under one lock (the host app already does this for all state). No
method may be called concurrently. Stated as an invariant because the
correctness of the read/write accounting depends on it.

TRUST BOUNDARY -- leases are COORDINATION, not security. `controller_id`
is whatever the caller says it is: on the local IPC routes it is
unauthenticated, and on the signed envelope path it is covered only by
the group PSK (A-8), so any convoy member can sign any controller_id.
Nothing here stops a caller from naming another controller's id to take,
release, or act under its lease. That is the same bargain Embody's
shipped claim_scope makes between cooperating sessions, and it is why a
lease refusal is a coordination signal rather than an access-control
decision. Per-controller authentication is a Phase 3 keypair question,
not something this module claims today.
"""

LEASE_EXCLUSIVE = "exclusive"
LEASE_SHARED = "shared"

DEFAULT_LEASE_TTL_S = 120.0
MAX_LEASE_TTL_S = 3600.0          # A-17: leases are TTL-CLAMPED
DEFAULT_CONTROLLER_TIMEOUT_S = 60.0


class LeaseError(Exception):
    def __init__(self, reason, detail="", holder=None):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail
        self.holder = holder            # a blocking controller, if any


class LeaseRegistry:
    """Per-node lease state as pure policy driven by an injected clock.
    Every method takes `now` so tests are deterministic and the host app
    passes its own clock (never a hidden time.time)."""

    def __init__(self, ttl_s=DEFAULT_LEASE_TTL_S,
                 controller_timeout_s=DEFAULT_CONTROLLER_TIMEOUT_S,
                 max_ttl_s=MAX_LEASE_TTL_S):
        self.ttl_s = ttl_s
        self.max_ttl_s = max_ttl_s
        self.controller_timeout_s = controller_timeout_s
        self._controllers = {}   # controller_id -> {"last_seen", "label"}
        # node_id -> {"mode": exclusive|shared,
        #             "holders": {controller_id -> expires_unix}}
        # A SET of holders is the fix for the single-slot bug: a shared
        # lease legitimately has many simultaneous holders, and an
        # exclusive lease has exactly one.
        self._nodes = {}

    # -- controllers ---------------------------------------------------

    def heartbeat(self, controller_id, now, label=""):
        if not controller_id:
            raise LeaseError("malformed_controller", "empty controller_id")
        entry = self._controllers.get(controller_id) or {}
        entry["last_seen"] = now
        if label:
            entry["label"] = label
        entry.setdefault("label", label)
        self._controllers[controller_id] = entry

    def controller_alive(self, controller_id, now):
        entry = self._controllers.get(controller_id)
        if entry is None:
            return False
        return (now - entry["last_seen"]) <= self.controller_timeout_s

    # -- internal: live holders ----------------------------------------

    def _live_holders(self, node_id, now):
        """The controllers holding a live lease on this node right now: a
        holder counts only while its own entry is unexpired AND it is
        still heartbeating. Returns {controller_id -> expires}."""
        entry = self._nodes.get(node_id)
        if not entry:
            return {}
        live = {cid: exp for cid, exp in entry["holders"].items()
                if exp > now and self.controller_alive(cid, now)}
        return live

    # -- leases --------------------------------------------------------

    def acquire(self, node_id, controller_id, mode, now, ttl_s=None):
        """Take or renew a lease. Returns the lease dict, or raises
        LeaseError naming a blocking holder."""
        if mode not in (LEASE_EXCLUSIVE, LEASE_SHARED):
            raise LeaseError("bad_mode", repr(mode))
        # Acting proves you are alive.
        self.heartbeat(controller_id, now)
        # A-17: TTL is clamped, never unbounded.
        ttl = min(ttl_s or self.ttl_s, self.max_ttl_s)
        expires = now + ttl

        entry = self._nodes.get(node_id)
        live = self._live_holders(node_id, now)
        # Drop dead holders so a node with only-dead holders is free.
        others = {c: e for c, e in live.items() if c != controller_id}

        if mode == LEASE_EXCLUSIVE:
            if others:
                holder = next(iter(others))
                raise LeaseError(
                    "node_leased",
                    f"held by {holder!r}; cannot take exclusive",
                    holder=holder)
            # No other live holder: take (or convert to) exclusive.
            self._nodes[node_id] = {"mode": LEASE_EXCLUSIVE,
                                    "holders": {controller_id: expires}}
            return self._lease_view(node_id, controller_id)

        # mode == SHARED
        if entry and entry["mode"] == LEASE_EXCLUSIVE and others:
            holder = next(iter(others))
            raise LeaseError(
                "node_leased",
                f"held exclusively by {holder!r}; cannot take shared",
                holder=holder)
        # Add/refresh THIS holder without disturbing other shared holders.
        holders = dict(live)
        holders[controller_id] = expires
        self._nodes[node_id] = {"mode": LEASE_SHARED, "holders": holders}
        return self._lease_view(node_id, controller_id)

    def _lease_view(self, node_id, controller_id):
        entry = self._nodes[node_id]
        return {"node_id": node_id, "controller_id": controller_id,
                "mode": entry["mode"],
                "expires": entry["holders"][controller_id]}

    def release(self, node_id, controller_id):
        """Release YOUR hold on a node. Removing a hold you do not have is
        a no-op, never an error -- cleanup must be idempotent. Other
        shared holders are untouched."""
        entry = self._nodes.get(node_id)
        if not entry or controller_id not in entry["holders"]:
            return False
        del entry["holders"][controller_id]
        if not entry["holders"]:
            del self._nodes[node_id]
        return True

    def release_controller(self, controller_id, modes=None):
        """Drop EVERY hold this controller has, across every node, and
        forget the controller itself. Returns the number of holds dropped.

        The revocation path (A-7): when a peer is revoked, the leases its
        controllers hold must fall IMMEDIATELY rather than waiting out a
        TTL -- a revoked peer's exclusive lease would otherwise keep
        blocking every local mutation for up to an hour.

        `modes` optionally narrows it to given lease modes, which is how
        a narrowing to observe-only drops the peer's WRITER locks (it may
        no longer mutate, so holding one only blocks others) while
        leaving its reader holds alone.
        """
        if not controller_id:
            return 0
        released = 0
        for node_id in list(self._nodes):
            entry = self._nodes[node_id]
            if modes is not None and entry["mode"] not in modes:
                continue
            if entry["holders"].pop(controller_id, None) is not None:
                released += 1
            if not entry["holders"]:
                del self._nodes[node_id]
        if modes is None:
            # Forget the controller too, so a stale heartbeat cannot keep
            # a revoked peer's identity alive in the table.
            self._controllers.pop(controller_id, None)
        return released

    def authorize(self, node_id, controller_id, is_mutating, now):
        """May this controller issue this operation to this node NOW?
        Returns None if allowed, raises LeaseError if not.

        Reader/writer semantics:
          - a READ is always allowed (reads coexist with any lease);
          - a MUTATION is allowed only if the caller holds the EXCLUSIVE
            lease, or the node has no live holder at all. A shared holder
            may NOT mutate (that was the blocker: a read lease must never
            grant write rights), and a mutation is refused whenever any
            OTHER controller holds any live lease.
        """
        if not is_mutating:
            return None
        live = self._live_holders(node_id, now)
        if not live:
            return None                     # unleased: open to mutate
        entry = self._nodes.get(node_id)
        if entry["mode"] == LEASE_EXCLUSIVE and controller_id in live:
            return None                     # you hold exclusive
        # Either someone else holds it, or it is a shared lease (no writer
        # rights for anyone). Name a blocking holder.
        other = next((c for c in live if c != controller_id), None)
        if other is not None:
            raise LeaseError(
                "node_leased",
                f"{node_id} is held by {other!r}; your mutating operation "
                f"is refused", holder=other)
        # Only the caller holds it, but as SHARED -> still no write right.
        raise LeaseError(
            "shared_lease_no_mutation",
            f"you hold {node_id} SHARED (read-only); take it exclusive to "
            f"mutate")

    def live_leases(self, now):
        """One entry per (node, holder) that is live -- so N shared
        readers on a node show as N rows, which is what a federation view
        must report."""
        out = []
        for node_id in list(self._nodes):
            entry = self._nodes[node_id]
            for cid, exp in self._live_holders(node_id, now).items():
                out.append({"node_id": node_id, "controller_id": cid,
                            "mode": entry["mode"], "expires": exp})
        return out

    def reap(self, now):
        """Drop dead holders and forget timed-out controllers. Returns the
        number of holder-slots reaped."""
        reaped = 0
        for node_id in list(self._nodes):
            entry = self._nodes[node_id]
            dead = [c for c, e in entry["holders"].items()
                    if e <= now or not self.controller_alive(c, now)]
            for c in dead:
                del entry["holders"][c]
                reaped += 1
            if not entry["holders"]:
                del self._nodes[node_id]
        gone = [cid for cid, e in self._controllers.items()
                if (now - e["last_seen"]) > self.controller_timeout_s]
        for cid in gone:
            del self._controllers[cid]
        return reaped
