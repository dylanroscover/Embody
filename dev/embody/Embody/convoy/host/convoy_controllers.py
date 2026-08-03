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
        # node_id -> {delivery_id -> {"controller_id", "expires"}}.
        # These are operation-scoped writer claims.  They are deliberately
        # separate from explicit user leases so completing one job can never
        # release a lease the operator acquired independently.
        self._claims = {}

    # -- controllers ---------------------------------------------------

    def heartbeat(self, controller_id, now, label="", selected_node_id=None,
                  clear_selection=False):
        if not controller_id:
            raise LeaseError("malformed_controller", "empty controller_id")
        entry = self._controllers.get(controller_id) or {}
        entry["last_seen"] = now
        if label:
            entry["label"] = label
        entry.setdefault("label", label)
        if clear_selection:
            entry.pop("selected_node_id", None)
        elif selected_node_id is not None:
            if not isinstance(selected_node_id, str) or not selected_node_id:
                raise LeaseError("malformed_node", "empty selected node id")
            entry["selected_node_id"] = selected_node_id
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

    def _live_claims(self, node_id, now):
        """Operation claims follow the durable job, not controller presence.

        A controller heartbeat is required to *start* queued work, but once a
        mutation has crossed the dispatch boundary, dropping the client must
        not make a second controller's mutation concurrent with it.  Queued
        claims still require a live controller; HostApp detaches and renews a
        claim once durable dispatch begins.
        """
        claims = self._claims.get(node_id) or {}
        return {
            delivery_id: record for delivery_id, record in claims.items()
            if record["expires"] > now
            and (record.get("requires_controller", True) is False
                 or self.controller_alive(record["controller_id"], now))
        }

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
        claim_others = {
            record["controller_id"]
            for record in self._live_claims(node_id, now).values()
            if record["controller_id"] != controller_id
        }
        if claim_others:
            holder = sorted(claim_others)[0]
            raise LeaseError(
                "node_leased",
                f"held by active operation from {holder!r}",
                holder=holder)
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

    def claim_operation(self, node_id, controller_id, delivery_id, now,
                        ttl_s=None):
        """Acquire/renew one operation-scoped exclusive writer claim.

        Multiple jobs from the same controller may coexist; a different
        controller is refused until every live claim is released, expires,
        or loses its controller heartbeat.  The claim id is the durable
        delivery id, making cleanup exact and idempotent.
        """
        if not controller_id:
            raise LeaseError("malformed_controller", "empty controller_id")
        if not isinstance(delivery_id, str) or not delivery_id:
            raise LeaseError("malformed_claim", "empty delivery_id")
        self.heartbeat(controller_id, now)
        ttl = min(ttl_s or self.max_ttl_s, self.max_ttl_s)
        explicit = self._live_holders(node_id, now)
        explicit_others = {cid for cid in explicit if cid != controller_id}
        if explicit_others:
            holder = sorted(explicit_others)[0]
            raise LeaseError(
                "node_leased",
                f"{node_id} is held by {holder!r}", holder=holder)
        entry = self._nodes.get(node_id)
        if (entry and entry.get("mode") == LEASE_SHARED
                and controller_id in explicit):
            raise LeaseError(
                "shared_lease_no_mutation",
                f"you hold {node_id} SHARED (read-only); take it exclusive "
                f"to mutate")
        claim_others = {
            record["controller_id"]
            for record in self._live_claims(node_id, now).values()
            if record["controller_id"] != controller_id
        }
        if claim_others:
            holder = sorted(claim_others)[0]
            raise LeaseError(
                "node_leased",
                f"{node_id} has active work from {holder!r}", holder=holder)
        claims = self._claims.setdefault(node_id, {})
        existing = claims.get(delivery_id)
        requires_controller = not (
            isinstance(existing, dict)
            and existing.get("controller_id") == controller_id
            and existing.get("requires_controller", True) is False)
        claims[delivery_id] = {
            "controller_id": controller_id, "expires": now + ttl,
            "requires_controller": requires_controller}
        return {
            "node_id": node_id, "controller_id": controller_id,
            "delivery_id": delivery_id, "mode": LEASE_EXCLUSIVE,
            "implicit": True, "expires": now + ttl,
        }

    def restore_operation(self, node_id, controller_id, delivery_id, now,
                          ttl_s=None):
        """Restore a durable in-flight claim without reviving its controller.

        Used only during HostApp boot.  A running job is still a writer even
        when the originating client is gone, so recovery must rebuild that
        exclusion while leaving controller liveness truthful.
        """
        if not controller_id:
            raise LeaseError("malformed_controller", "empty controller_id")
        if not isinstance(delivery_id, str) or not delivery_id:
            raise LeaseError("malformed_claim", "empty delivery_id")
        ttl = min(ttl_s or self.max_ttl_s, self.max_ttl_s)
        explicit = self._live_holders(node_id, now)
        explicit_others = {cid for cid in explicit if cid != controller_id}
        entry = self._nodes.get(node_id)
        # Recovery describes work that has ALREADY crossed the dispatch
        # boundary. A shared/read-only holder may coexist with it (reads
        # coexist with writers); an exclusive holder would represent a second
        # writer authority and therefore keeps the global recovery fence up.
        if (explicit_others and entry
                and entry.get("mode") == LEASE_EXCLUSIVE):
            holder = sorted(explicit_others)[0]
            raise LeaseError(
                "node_leased", f"{node_id} is held by {holder!r}",
                holder=holder)
        claim_others = {
            record["controller_id"]
            for record in self._live_claims(node_id, now).values()
            if record["controller_id"] != controller_id
        }
        if claim_others:
            holder = sorted(claim_others)[0]
            raise LeaseError(
                "node_leased",
                f"{node_id} has active work from {holder!r}", holder=holder)
        expires = now + ttl
        self._claims.setdefault(node_id, {})[delivery_id] = {
            "controller_id": controller_id, "expires": expires,
            "requires_controller": False}
        return {
            "node_id": node_id, "controller_id": controller_id,
            "delivery_id": delivery_id, "mode": LEASE_EXCLUSIVE,
            "implicit": True, "expires": expires,
        }

    def renew_operation(self, node_id, delivery_id, now, ttl_s=None,
                        detach=False):
        """Renew one existing job claim without changing controller liveness."""
        claims = self._claims.get(node_id)
        record = claims.get(delivery_id) if claims else None
        if record is None:
            return False
        ttl = min(ttl_s or self.max_ttl_s, self.max_ttl_s)
        record["expires"] = now + ttl
        if detach:
            record["requires_controller"] = False
        return True

    def release_operation(self, node_id, delivery_id):
        """Release exactly one operation claim; never an explicit lease."""
        claims = self._claims.get(node_id)
        if not claims or claims.pop(delivery_id, None) is None:
            return False
        if not claims:
            self._claims.pop(node_id, None)
        return True

    def operation_claims(self):
        """Return detached claim rows, including expired ones, for recovery.

        HostApp uses this before ``reap`` so an in-flight durable job can
        renew its exclusion after a long OS sleep instead of briefly becoming
        writable by another controller.
        """
        return [
            {
                "node_id": node_id,
                "delivery_id": delivery_id,
                "controller_id": record["controller_id"],
                "expires": record["expires"],
                "requires_controller": record.get(
                    "requires_controller", True),
            }
            for node_id, claims in self._claims.items()
            for delivery_id, record in claims.items()
        ]

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

    def release_controller(self, controller_id, modes=None,
                           preserve_claims=None):
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
        preserve_claims = set(preserve_claims or ())
        released = 0
        for node_id in list(self._nodes):
            entry = self._nodes[node_id]
            if modes is not None and entry["mode"] not in modes:
                continue
            if entry["holders"].pop(controller_id, None) is not None:
                released += 1
            if not entry["holders"]:
                del self._nodes[node_id]
        if modes is None or LEASE_EXCLUSIVE in modes:
            for node_id in list(self._claims):
                claims = self._claims[node_id]
                for delivery_id in list(claims):
                    if (claims[delivery_id]["controller_id"] == controller_id
                            and delivery_id not in preserve_claims):
                        del claims[delivery_id]
                        released += 1
                if not claims:
                    del self._claims[node_id]
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
        claims = self._live_claims(node_id, now)
        if claims:
            claim_controllers = {
                record["controller_id"] for record in claims.values()}
            other = next((cid for cid in sorted(claim_controllers)
                          if cid != controller_id), None)
            if other is not None:
                raise LeaseError(
                    "node_leased",
                    f"{node_id} has active work from {other!r}; your "
                    f"mutating operation is refused", holder=other)
            # Every live claim belongs to this controller.  It already owns
            # the operation-scoped writer right, even when no explicit lease
            # exists. Recovery can, however, discover a shared reader that
            # was acquired while the durable job file was unreadable; that
            # reader does not stop the existing job, but it does block a NEW
            # mutation until released.
            live = self._live_holders(node_id, now)
            explicit_other = next(
                (cid for cid in live if cid != controller_id), None)
            if explicit_other is not None:
                raise LeaseError(
                    "node_leased",
                    f"{node_id} is held by {explicit_other!r}; your "
                    f"mutating operation is refused",
                    holder=explicit_other)
            entry = self._nodes.get(node_id)
            if (entry and entry.get("mode") == LEASE_SHARED
                    and controller_id in live):
                raise LeaseError(
                    "shared_lease_no_mutation",
                    f"you hold {node_id} SHARED (read-only); take it "
                    f"exclusive to mutate")
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
        for node_id in list(self._claims):
            for delivery_id, record in self._live_claims(
                    node_id, now).items():
                out.append({
                    "node_id": node_id,
                    "controller_id": record["controller_id"],
                    "mode": LEASE_EXCLUSIVE,
                    "expires": record["expires"],
                    "implicit": True,
                    "delivery_id": delivery_id,
                })
        return out

    def live_controllers(self, now):
        """One bounded plain-data row per live controller.

        Controllers remain visible between operations even when they hold no
        explicit lease.  The selected node is the last target observed by the
        host gate; it is display/coordination state, never authorization.
        """
        leases_by_controller = {}
        for lease in self.live_leases(now):
            leases_by_controller.setdefault(
                lease["controller_id"], []).append(dict(lease))
        out = []
        for controller_id, entry in self._controllers.items():
            if not self.controller_alive(controller_id, now):
                continue
            leases = sorted(
                leases_by_controller.get(controller_id, []),
                key=lambda row: (row.get("node_id") or "",
                                 row.get("mode") or ""))
            row = {
                "controller_id": controller_id,
                "label": entry.get("label") or "",
                "last_seen": entry.get("last_seen"),
                "selected_node_id": entry.get("selected_node_id"),
                "leases": leases,
                "node_ids": sorted({lease["node_id"] for lease in leases}),
            }
            out.append(row)
        return sorted(out, key=lambda row: row["controller_id"])

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
        for node_id in list(self._claims):
            claims = self._claims[node_id]
            dead = [delivery_id for delivery_id, record in claims.items()
                    if record["expires"] <= now
                    or (record.get("requires_controller", True)
                        and not self.controller_alive(
                            record["controller_id"], now))]
            for delivery_id in dead:
                del claims[delivery_id]
                reaped += 1
            if not claims:
                del self._claims[node_id]
        gone = [cid for cid, e in self._controllers.items()
                if (now - e["last_seen"]) > self.controller_timeout_s]
        for cid in gone:
            del self._controllers[cid]
        return reaped
