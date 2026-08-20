"""Host state for the embody-convoy host app -- plain JSON files.

Replaces an earlier SQLite implementation. Measured on the real target
scenario (a few machines, a few hundred TD projects each, 1-6 open at a
time): 500 registered projects is 120 KB as SQLite and 109 KB as JSON.
SQLite bought nothing at that size, and its machinery cost four real
defects in review (non-atomic migration leaving the daemon unbootable, a
schema check that crash-loops under the supervisor, missing constraints).
Embody's own job store is already one JSON file per job -- this follows
the codebase instead of importing a database.

Three stores, each shaped by how it actually changes:

  host.json   -- identity + the node registry. Small, rewritten whole,
                 rarely (only on register / approve / remint). Atomic
                 replace, so a crash can never leave it half-written.
  jobs/*.json -- ONE FILE PER JOB, exactly like .embody/jobs/. A job
                 write never rewrites the others, so the file that grows
                 with usage is never rewritten as a blob.
  audit.jsonl -- append-only, one JSON object per line. Appending cannot
                 corrupt earlier lines, and pruning is a truncate.

Scale honesty: at ~250 bytes/node this stays trivial to tens of
thousands of projects. If a later phase needs indexed queries or
cross-machine reconciliation over very large histories, THAT is when a
database earns its place -- not before.
"""

import hashlib
import json
import math
import os
import secrets
import stat
import time

import convoy_identity as identity
import convoy_platform as platform_mod

STORE_VERSION = 1
HOST_FILE = "host.json"
JOBS_DIR = "jobs"
AUDIT_FILE = "audit.jsonl"

# A delivery record that cannot be PARSED is renamed aside rather than
# deleted: the suffix takes it out of _delivery_ids (which lists only
# *.json) so it stops blocking node cleanup, while leaving the bytes on
# disk for a human. Bounded per pass so one bad sweep -- or one global
# read failure misclassified as corruption -- can never empty the queue.
_QUARANTINE_SUFFIX = ".corrupt"
_MAX_QUARANTINE_PER_PASS = 5

# Audit rotation. A supervised host runs unattended for WEEKS, and the
# killswitch-backlog path can append at ~200 KB/s (measured), so an
# append-only audit with no ceiling fills the disk -- 118 GB/week in the
# pathological case. Rotate the current file aside at this size, keeping
# ONE generation for forensics; the rotated file is overwritten on the
# next rotation, so the on-disk audit is bounded at ~2x this.
AUDIT_MAX_BYTES = 8 * 1024 * 1024
AUDIT_ROTATED = AUDIT_FILE + ".1"

JOB_STATES = ("queued", "dispatching", "running", "succeeded", "failed",
              "indeterminate", "refused")

# The states ordinary coordination never reopens. A record here is DONE being
# decided by this store's normal delivery authority:
# succeeded/failed are node verdicts, indeterminate is the host's own
# may-have-run proof (16.4), refused is the host's own DELIVERY verdict
# (see mark_refused). The sole stronger-evidence exception is
# reconcile_lifecycle_result: exact-node lifecycle owns a separate durable
# executor ledger and may replace boot-sweep indeterminate with that bound
# committed verdict. Named once, so every ordinary guard that must not reopen
# a settled record reads the same list.
TERMINAL_STATES = ("succeeded", "failed", "indeterminate", "refused")

# A-15, "one authority, two records". For TD operations the NODE originates
# the execution verdict and the host record MIRRORS it.  Reviewed host-native
# operations are the one explicit second execution locus: the host subprocess
# facade itself is authoritative for those verdicts.  The coordination layer
# still may write only delivery/uncertainty states on its own authority:
#   queued        -- accepted, not yet dispatched.
#   dispatching   -- the host's transient CLAIM: a dispatcher owns this
#                    job while it forwards to the node. Not a verdict --
#                    no node was observed yet -- just mutual exclusion so
#                    two dispatchers (the drain loop and a manual
#                    /dispatch) can never double-run one job. It resolves
#                    to a node verdict, back to queued (never delivered),
#                    or indeterminate (host died mid-forward; see the
#                    load-time sweep).
#   indeterminate -- dispatched, outcome unobservable (partition, node
#                    gone, TD restarted mid-job). Only the host witnessed
#                    the dispatch-without-response, so only the host can
#                    record it -- and it is never deleted, it is the sole
#                    proof a consequential operation MAY have run (16.4).
#   refused       -- NOT DELIVERED, and never will be: the host declined
#                    to route this job (its origin peer was revoked while
#                    it sat queued). PROPOSED AS AMENDMENT A-54, recorded
#                    here rather than slipped in as a quiet edit.
#
#                    Why the host may originate it without breaking A-15:
#                    A-15 forbids the host originating running/succeeded/
#                    failed, because those are claims about EXECUTION and
#                    the node is the authority on execution. `refused`
#                    makes NO CLAIM ABOUT EXECUTION AT ALL. It is a
#                    DELIVERY verdict -- "this host did not and will not
#                    hand this to a node" -- and delivery is precisely
#                    what the host IS the authority on (it is the same
#                    authority that lets it write `queued`). It is only
#                    ever written to a job that never left `queued`, so
#                    it cannot overwrite anything a node said.
#
#                    Why it exists at all: the alternative is leaving a
#                    revoked peer's work queued forever pending the
#                    deferred reaper, which makes /jobs LIE -- it would
#                    report work as pending that the host has already
#                    decided never to do. Like mark_indeterminate it
#                    demands EVIDENCE, for the same reason: a terminal
#                    the host wrote on its own authority must carry what
#                    it saw.
# running / succeeded / failed are EXECUTOR verdicts: TD outcomes may only be
# recorded through record_node_verdict/record_sync_result, while a reviewed
# host subprocess uses record_host_result.  Every path requires observed_at
# plus a source naming the executor; the coordination layer cannot fabricate a
# success by merely passing host_originated=True.
_HOST_ORIGINABLE_STATES = ("queued", "dispatching", "indeterminate",
                           "refused")

# The shipped node vocabulary (EnvoyExt _job_public: running|done|error)
# mapped to the host mirror. Kept explicit so a new node status cannot
# silently map to a host state by coincidence.
_NODE_STATUS_TO_STATE = {"running": "running", "done": "succeeded",
                         "error": "failed"}

# The verdict_source values that name the actual executor as authority.
# _apply_state refuses an execution verdict carrying any other source --
# enforcement is BY STATE, never by trusting the caller to pass a flag.
_NODE_VERDICT_SOURCES = ("node_poll", "node_sync")
_EXECUTION_VERDICT_SOURCES = _NODE_VERDICT_SOURCES + (
    "host_operation", "host_operation_recovery")
_HOST_VERDICT_OPERATIONS = frozenset({
    "convoy_ping", "convoy_git", "convoy_gh", "convoy_shell",
    "convoy_start_node", "convoy_restart_node",
})

# A small backwards wall-clock adjustment is common during time sync.  A
# larger rollback after admission makes a persisted absolute expiry
# untrustworthy, so the safe interpretation is expired.  During a live
# process the monotonic anchor below catches *any* rollback without this
# tolerance; this constant is principally the restart fallback.
MAX_CLOCK_ROLLBACK_S = 1.0


class IdempotencyOriginConflict(Exception):
    """This idempotency key already names a record from ANOTHER origin.

    Never silently hand the caller the other origin's job: it would be
    told 200 for an operation that was quietly replaced by someone
    else's, and (worse, measured) a peer's revocation would then burn a
    LOCAL caller's acknowledged work.

    The key space is scoped per origin, so a live marker and its record
    agree by construction. What this catches is the case where they have
    STOPPED agreeing: a job file whose origin changed underneath its
    marker -- a hand edit, a restored backup, a partial copy of a state
    directory, tampering. The marker says "this key belongs to X" and the
    record says "I belong to Y"; handing X the record would attribute Y's
    work to X, and X's revocation would then terminalise it. Refuse and
    let the caller retry with a key that is not in dispute.
    """

    reason = "idempotency_origin_conflict"

    def __init__(self, delivery_id, existing_origin, requested_origin):
        super().__init__(
            f"idempotency key already names delivery {delivery_id}, which "
            f"was submitted by {existing_origin!r}, not {requested_origin!r}")
        self.delivery_id = delivery_id
        self.existing_origin = existing_origin
        self.requested_origin = requested_origin


class IdempotencyContentConflict(IdempotencyOriginConflict):
    """A key was reused for different semantic work in the same scope.

    Returning the old job is not idempotency in that case: it tells the
    caller its new command was accepted while silently substituting a
    different operation.  The structured attributes let HTTP/MCP layers
    return a stable 409 without parsing prose.
    """

    reason = "idempotency_content_conflict"

    def __init__(self, delivery_id, existing_digest, requested_digest):
        # Bypass IdempotencyOriginConflict.__init__: inheritance is solely
        # the compatibility category caught by older host-app layers.
        Exception.__init__(self,
            f"idempotency key already names delivery {delivery_id}, whose "
            f"request content differs from this submission")
        self.delivery_id = delivery_id
        self.existing_digest = existing_digest
        self.requested_digest = requested_digest


class StoreTooNew(Exception):
    """Written by a NEWER host app. Refuse to write, never scribble."""

    def __init__(self, disk_version, code_version):
        super().__init__(
            f"host store is v{disk_version}, this build understands "
            f"v{code_version} -- refusing to write; upgrade embody-convoy")
        self.disk_version = disk_version
        self.code_version = code_version


class HostStore:
    def __init__(self, directory, now=None, monotonic=None):
        self._now = now or time.time
        self._monotonic = monotonic or time.monotonic
        # delivery_id -> target-local monotonic deadline.  Never written
        # to disk: monotonic epochs are process/boot local.  Durable wall
        # expiry fields rebuild these anchors conservatively on startup.
        self._monotonic_deadlines = {}
        self.dir = directory
        self.jobs_dir = os.path.join(directory, JOBS_DIR)
        os.makedirs(self.jobs_dir, exist_ok=True)
        self._host_path = os.path.join(directory, HOST_FILE)
        self._state = self._load_host_file()
        self._restore_expiry_anchors()
        self._sweep_interrupted_dispatches()

    # -- host.json ------------------------------------------------------

    def _load_host_file(self):
        try:
            with open(self._host_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            data = None
        except (OSError, ValueError) as e:
            # A corrupt host file is NOT recoverable by guessing: it holds
            # identity. Refuse loudly rather than minting a new host_id
            # and silently orphaning every peer relationship.
            raise RuntimeError(
                f"host state at {self._host_path} is unreadable ({e}). "
                f"Refusing to start with a fresh identity -- restore the "
                f"file or delete it deliberately to re-register.")
        if data is None:
            data = {"version": STORE_VERSION,
                    "host_id": identity.mint_id(),
                    "nodes": {}}
            self._write_host(data)
            self.audit("hoststore", "host_id_minted",
                       {"host_id": data["host_id"]})
            return data
        version = data.get("version", 0)
        if version > STORE_VERSION:
            raise StoreTooNew(version, STORE_VERSION)
        return data

    def _write_host(self, data=None):
        platform_mod._write_private(
            self._host_path,
            json.dumps(data if data is not None else self._state,
                       indent=1, sort_keys=True) + "\n")

    def close(self):
        pass            # nothing to close; files are written as they change

    # -- identity -------------------------------------------------------

    def host_id(self):
        return self._state["host_id"]

    # -- node registry --------------------------------------------------

    def load_directory(self):
        """Rebuild the pure NodeDirectory from stored nodes.

        Rows whose stored host_id is not this host are quarantined, never
        replayed.  Legacy ``td_python_approved`` values are deliberately
        ignored: code-execution authority moved to host-private policy.json
        and must be reconfirmed locally rather than migrated fail-open.
        Returns (directory, quarantined).
        """
        host_id = self.host_id()
        directory = identity.NodeDirectory(host_id)
        quarantined = []
        for node_id, row in sorted(self._state["nodes"].items(),
                                   key=lambda kv: (kv[1].get("first_seen", 0),
                                                   kv[0])):
            if row.get("host_id") != host_id:
                quarantined.append({**row, "node_id": node_id})
                continue
            try:
                record = directory.register(
                    row.get("project_root"), row.get("comp_path"),
                    row.get("convoy_id"), minted_id=node_id,
                    node_discriminator=(
                        row.get("node_discriminator") or None),
                    replay=True,
                    # Rows written before automatic realm genesis existed
                    # were already authoritative.  Treating a missing field
                    # as candidate would let a rolling upgrade silently
                    # rewrite an established deployment.
                    binding_state=row.get("binding_state", "established"))
            except identity.IdentityError as e:
                # One bad entry must not make a SUPERVISED daemon
                # crash-loop -- the spike proved it restarts every 60s.
                quarantined.append({**row, "node_id": node_id,
                                    "load_error": e.reason})
                continue
            # Rows written before these additive fields existed remain
            # enabled and have no descriptive metadata.  Invalid metadata
            # cannot carry authority, so discard only that decoration rather
            # than quarantining an otherwise valid stable node identity.
            directory.set_enabled(record["node_id"],
                                  row.get("enabled", True) is True)
            try:
                directory.set_metadata(record["node_id"],
                                       row.get("metadata", {}))
            except identity.IdentityError as e:
                directory.set_metadata(record["node_id"], {})
                self.audit("hoststore", "node_metadata_dropped_on_load",
                           {"node_id": node_id, "reason": e.reason})
        if quarantined:
            self.audit("hoststore", "nodes_quarantined_on_load",
                       {"count": len(quarantined),
                        "node_ids": [r["node_id"] for r in quarantined][:16]})
        return directory, quarantined

    def _node_row(self, record, existing, now):
        """Validate and serialize the durable subset of one node record."""
        return {
            # Stable location and operator intent survive a host restart.
            # runtime_id and envoy_port are deliberately absent: both name
            # one live TD launch and must never be resurrected from disk.
            "project_root": record["project_root"],
            "node_discriminator": record.get("node_discriminator", ""),
            "host_id": record["host_id"],
            "convoy_id": record["convoy_id"],
            "binding_state": identity.normalize_binding_state(
                record.get("binding_state", "established")),
            "comp_path": record["comp_path"],
            "enabled": record.get("enabled", True) is True,
            "metadata": identity.sanitize_node_metadata(
                record.get("metadata", {})),
            "first_seen": existing.get("first_seen", now),
            "last_seen": now,
        }

    def save_nodes(self, records):
        """Persist several node rows with one atomic host.json replace.

        Every record is validated and a complete next state is built before
        either the in-memory store or disk changes.  This is the persistence
        primitive for realm convergence: a dozen local TD nodes cannot be
        left half on the candidate id and half on the authoritative id by a
        process failure between per-row writes.
        """
        records = list(records)
        if not records:
            return []
        now = self._now()
        nodes = dict(self._state["nodes"])
        seen = set()
        saved = []
        for record in records:
            node_id = record.get("node_id")
            if not identity.is_valid_id(node_id):
                raise identity.IdentityError("malformed_node_id",
                                             repr(node_id))
            if node_id in seen:
                raise identity.IdentityError(
                    "duplicate_node_id", repr(node_id))
            seen.add(node_id)
            row = self._node_row(record, nodes.get(node_id, {}), now)
            nodes[node_id] = row
            saved.append({"node_id": node_id, **row})

        next_state = dict(self._state)
        next_state["nodes"] = nodes
        self._write_host(next_state)
        self._state = next_state
        return saved

    def save_node(self, record):
        return self.save_nodes([record])[0]

    def rebind_candidates(self, directory, convoy_id,
                          binding_state="established"):
        """Persist and apply one authoritative binding to local candidates.

        This is the HostApp-facing transaction.  It snapshots only candidate
        rows, writes their projected durable forms in one atomic replace, and
        then performs the directory's exact compare-and-swap as one pure
        mutation.  Established rows -- matching or not -- are not selected
        and cannot be rewritten here.  HostApp serializes calls with its
        existing state lock, so the disk-first projection cannot race another
        directory mutation; disk-first also makes a crash restart converge to
        the authoritative state rather than resurrecting candidates.

        Returns detached changed directory records.
        """
        convoy_id = identity.normalize_convoy_id(convoy_id)
        binding_state = identity.normalize_binding_state(binding_state)
        candidates = directory.candidate_nodes()
        expected = {record["node_id"]: record["convoy_id"]
                    for record in candidates}
        projected = []
        for record in candidates:
            if (record["convoy_id"] == convoy_id
                    and record["binding_state"] == binding_state):
                continue
            target = dict(record)
            target["metadata"] = dict(record.get("metadata") or {})
            target["convoy_id"] = convoy_id
            target["binding_state"] = binding_state
            projected.append(target)
        if not projected:
            return []
        self.save_nodes(projected)
        return directory.rebind_candidates(
            convoy_id, binding_state=binding_state, expected=expected)

    def abandon_established(self, directory, adopted_convoy_id):
        """Persist and apply an operator-confirmed realm abandonment.

        The HostApp-facing transaction for Join Other Realm: every
        established row NOT bound to ``adopted_convoy_id`` moves to
        candidate AT the adopted id, DISK FIRST like
        ``rebind_candidates`` -- a crash restart must converge toward
        the adoption the operator confirmed, never resurrect the old
        realm's commitment and re-derive the very conflict being
        escaped. The caller commits the realm itself AFTER this returns
        (RealmStore.adopt), so a failed write here leaves the previous
        realm committed and the join simply retryable. HostApp
        serializes calls with its state lock; the ``expected`` map gives
        the directory the same exact compare-and-swap its sibling keeps.

        Returns detached changed directory records.
        """
        adopted_convoy_id = identity.normalize_convoy_id(adopted_convoy_id)
        expected = {}
        projected = []
        for record in directory.nodes():
            if (record["binding_state"] == "established"
                    and record["convoy_id"] != adopted_convoy_id):
                expected[record["node_id"]] = record["convoy_id"]
                target = dict(record)
                target["metadata"] = dict(record.get("metadata") or {})
                target["convoy_id"] = adopted_convoy_id
                target["binding_state"] = "candidate"
                projected.append(target)
        if not projected:
            return []
        self.save_nodes(projected)
        return directory.abandon_established(adopted_convoy_id,
                                             expected=expected)

    def delete_node(self, node_id):
        """Remove one node row, DISK FIRST -- the save_nodes contract.

        The old body popped the row out of self._state and only then
        wrote host.json, which inverted every caller's durability
        argument. A raised write (a Windows sharing violation surviving
        all of _write_private's retries) left the row absent from
        memory, present on disk and present in the directory; the
        caller's retry then found nothing to pop, wrote NOTHING, and
        returned success, so the next daemon start replayed the ghost
        from host.json. Build the next state, persist it, and only then
        commit in memory: a failed write raises with memory and disk
        still agreeing, and the retry does the whole thing again.
        """
        if node_id not in self._state["nodes"]:
            return
        nodes = dict(self._state["nodes"])
        nodes.pop(node_id, None)
        next_state = dict(self._state)
        next_state["nodes"] = nodes
        self._write_host(next_state)
        self._state = next_state

    def node_last_seen(self, node_id):
        """The durable last_seen stamp for a node row, or None.

        last_heartbeat_unix is process-local; this is the stamp that
        survives daemon restarts (written on every save_node), used by
        the stale-node eviction sweep to prove silence.
        """
        return self._node_stamp(node_id, "last_seen")

    def node_first_seen(self, node_id):
        """The durable first_seen stamp for a node row, or None.

        With last_seen it bounds how long a row ever LIVED -- the
        transient-ghost eviction tier's evidence (see hostapp
        _evict_stale_nodes)."""
        return self._node_stamp(node_id, "first_seen")

    def _node_stamp(self, node_id, key):
        row = self._state["nodes"].get(node_id)
        if not row:
            return None
        try:
            value = row.get(key)
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    # -- convoy pre-shared keys (Phase 1 group auth, A-8) ----------------

    def ensure_convoy_psk(self, convoy_id):
        """Return this convoy's pre-shared signing key, minting it on
        first sight. Idempotent.

        Phase 1's HMAC-over-PSK is GROUP/membership authentication (A-8):
        one key per convoy, held host-private in host.json (0600 via
        _write_private). Phase 3 replaces the PSK with per-host keypairs
        through the Signer seam; this store key then becomes legacy.

        `convoy_psks` is an ADDITIVE key: older builds load host.json
        whole and rewrite the whole dict, so the key round-trips through
        them intact -- no STORE_VERSION bump needed. The audit line
        records THAT a key was minted, never the key itself.
        """
        if not convoy_id or not isinstance(convoy_id, str):
            raise ValueError("convoy_id is required to mint a PSK")
        psks = self._state.setdefault("convoy_psks", {})
        psk = psks.get(convoy_id)
        if psk:
            return psk
        psk = secrets.token_hex(32)
        psks[convoy_id] = psk
        self._write_host()
        self.audit("hoststore", "convoy_psk_minted", {"convoy_id": convoy_id})
        return psk

    def convoy_psk(self, convoy_id):
        """The convoy's PSK, or None if never minted. Read-only."""
        return self._state.get("convoy_psks", {}).get(convoy_id)

    # -- host identity fingerprint (Phase 3 slice 1) --------------------

    def last_identity_fingerprint(self):
        """The host fingerprint recorded at the previous boot, or None.

        Exists so a CHANGE of identity between boots is detectable at
        all. Without it, "the key file was present" and "the key file is
        the same key as last time" are indistinguishable -- so restoring
        the wrong backup, an operator swapping in another machine's
        identity, or a half-completed rotation all come up silently as
        a normal start, and the single most audit-worthy event in the
        whole Phase 3 trust model goes unrecorded.

        `identity_fingerprint` is an ADDITIVE key, like `convoy_psks`
        above: older builds round-trip it untouched, so no
        STORE_VERSION bump. It is PUBLIC data -- a fingerprint is what
        peers compare out loud -- so unlike the PSKs beside it there is
        nothing secret here.
        """
        value = self._state.get("identity_fingerprint")
        return value if isinstance(value, str) and value else None

    def record_identity_fingerprint(self, value):
        """Remember this boot's fingerprint. Writes only on a change, so
        a steady host does not rewrite host.json on every start."""
        if not value or self._state.get("identity_fingerprint") == value:
            return False
        self._state["identity_fingerprint"] = value
        self._write_host()
        return True

    # -- jobs (one file each, like .embody/jobs/) -----------------------
    #
    # Two ids live here, and keeping them distinct is A-15's "two
    # records": delivery_id (cj_...) is the HOST's routing/delivery
    # record -- did the mesh accept and route this request; node_job_id
    # (job_<8hex>) is the NODE's execution record -- what TD actually
    # did. The host mirrors the node's verdict into its own record but
    # never originates one (see record_node_verdict / mark_indeterminate).

    def _job_path(self, delivery_id):
        return os.path.join(self.jobs_dir, f"{delivery_id}.json")

    def _idem_path(self, convoy_id, node_id, idempotency_key,
                   origin_host_id=None):
        """Path of the per-key admission MARKER.

        Idempotency is scoped, never global: a single global key space
        meant a submission for node B returned node A's job, and let one
        convoy squat a guessable key across every other convoy. THE
        ORIGIN IS THE FOURTH PART, by exactly the same argument one layer
        out -- without it a peer could squat a guessable key across every
        other origin, and both directions of that were reproduced: a peer
        bound to a local caller's record (its own operation silently
        replaced, still answered 200), and a local caller's acknowledged
        job was burned by that peer's revocation.

        origin_host_id=None reproduces the ORIGINAL three-part scope, and
        that is deliberate rather than vestigial: markers written before
        delivery records carried an origin used it, so a LOCAL retry
        still has to be able to find its own job (see create_job) instead
        of re-minting a duplicate of acknowledged work.

        The parts are NUL-joined (a NUL cannot appear in any of them, so
        no combination can forge another scope) and hashed, so arbitrary
        key text can neither escape the jobs dir nor collide with a cj_
        job file.

        ONE FILE PER KEY, deliberately: the old shared _by_key.json blob
        turned a single unreadable read into "no keys exist" and re-minted
        a duplicate job for EVERY prior key (proven 2026-07-31). A per-key
        marker's blast radius is one key, and its read fails CLOSED.
        """
        parts = [convoy_id, node_id, idempotency_key]
        if origin_host_id:
            parts.append(origin_host_id)
        scope = "\x00".join(parts)
        digest = hashlib.sha256(scope.encode("utf-8")).hexdigest()[:24]
        return os.path.join(self.jobs_dir, f"idem_{digest}.json")

    @staticmethod
    def _claim_marker(path):
        """O_EXCL-create the marker as the admission GATE. Returns True
        if we created it (we own this key's admission), False if it
        already existed. The atomic create IS the decision, so there is
        no write-body-then-index window in which a crash drops the
        mapping."""
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(path, flags, stat.S_IRUSR | stat.S_IWUSR)
        except FileExistsError:
            return False
        os.close(fd)
        return True

    def _fill_marker(self, path, delivery_id, request_sha256=None):
        """Point a claimed marker at the record it names. Atomic replace,
        so it heals an empty (crash- or inherit-left) marker in place."""
        marker = {"delivery_id": delivery_id, "created": self._now()}
        if request_sha256:
            marker["request_sha256"] = request_sha256
        platform_mod._write_private(
            path,
            json.dumps(marker, sort_keys=True) + "\n")

    @staticmethod
    def _read_marker(path):
        """Return the marker dict, None if it is absent or created-but-
        not-yet-filled, and RAISE on a persistent read error or corrupt
        content.

        Fail-closed is the whole point: treating an unreadable marker as
        "absent" would re-mint a job that already exists (the annihilation
        class). Empty is distinct from unreadable -- an empty marker is
        the O_EXCL-created-but-not-yet-filled window left by a crash mid
        accept, which the caller heals by minting; a locked/corrupt marker
        is refused so the caller retries rather than duplicates.
        """
        last = None
        for _attempt in range(4):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    text = f.read().strip()
            except FileNotFoundError:
                return None
            except OSError as e:        # locked / sharing violation: retry
                last = e
                continue
            if not text:
                return None             # created but not yet filled (crash)
            try:
                data = json.loads(text)
            except ValueError:
                # os.replace is atomic, so a NON-EMPTY marker we wrote is
                # always whole JSON. Unparseable-but-present means disk
                # corruption or tampering -- refuse, never heal-into-dup.
                raise OSError(
                    f"idempotency marker at {path} is present but corrupt")
            return data if isinstance(data, dict) else None
        raise OSError(f"idempotency marker at {path} unreadable: {last}")

    def _request_digest(self, node_id, operation, arguments, convoy_id,
                        expected_runtime_id=None, origin_host_id=None,
                        controller_id=None):
        """Digest the semantic request bound to an idempotency key.

        Delivery-only fields (request id, deadline, admission lineage)
        are intentionally excluded: a legitimate retry may have a fresh
        envelope/deadline and may cross a host restart, but it must still
        name exactly the same work.  Origin is part of the marker scope;
        including it here is defense in depth and maps legacy ``None``
        local origins to this host's identity for rolling upgrades.
        """
        origin = (origin_host_id if isinstance(origin_host_id, str)
                  and origin_host_id else self.host_id())
        payload = {
            "convoy_id": convoy_id,
            "node_id": node_id,
            "operation": operation,
            "arguments": arguments or {},
            "expected_runtime_id": (expected_runtime_id
                                    if isinstance(expected_runtime_id, str)
                                    and expected_runtime_id else None),
            "origin_host_id": origin,
            "controller_id": (controller_id
                              if isinstance(controller_id, str)
                              and controller_id else None),
        }
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=True, allow_nan=False)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def _job_request_digest(self, job):
        stored = job.get("request_sha256")
        if isinstance(stored, str) and stored:
            return stored
        # Rolling-upgrade compatibility: derive the binding for a record
        # written before request_sha256 existed.  We do not have to
        # rewrite it merely to answer a retry; every newly minted record
        # persists the digest.
        return self._request_digest(
            job.get("node_id"), job.get("operation"), job.get("arguments"),
            job.get("convoy_id"), job.get("expected_runtime_id"),
            job.get("origin_host_id"), job.get("controller_id"))

    @staticmethod
    def _accepted_timing(timing):
        """Validate and select the durable subset of verifier timing.

        The target-local monotonic timestamp is deliberately omitted from
        the returned dict because it is meaningless after process restart.
        It is installed into the in-memory anchor separately by
        create_job.  Malformed internal timing fails closed before a job
        is acknowledged.
        """
        if timing is None:
            return None
        if not isinstance(timing, dict):
            raise ValueError("accepted_timing must be an object")
        names = ("request_deadline_unix", "accepted_at_unix",
                 "accepted_remaining_s", "accepted_expires_unix")
        values = {}
        for name in names:
            try:
                value = float(timing.get(name))
            except (TypeError, ValueError):
                raise ValueError(f"accepted_timing.{name} is not numeric")
            if not math.isfinite(value):
                raise ValueError(f"accepted_timing.{name} is not finite")
            values[name] = value
        if values["accepted_remaining_s"] <= 0:
            raise ValueError("accepted_timing has no remaining budget")
        if values["accepted_expires_unix"] <= values["accepted_at_unix"]:
            raise ValueError("accepted_timing expires at or before admission")
        # The receiver-derived durable expiry may be earlier than the
        # signed deadline (a future protocol can clamp for clock skew),
        # but it may never extend the authenticated deadline.
        if (values["accepted_expires_unix"]
                > values["request_deadline_unix"] + 0.001):
            raise ValueError("accepted_timing extends the signed deadline")
        derived = (values["accepted_expires_unix"]
                   - values["accepted_at_unix"])
        if abs(derived - values["accepted_remaining_s"]) > 0.002:
            raise ValueError("accepted_timing remaining budget is inconsistent")
        return values

    def create_job(self, idempotency_key, node_id, operation, arguments,
                   convoy_id, expected_runtime_id=None,
                   origin_host_id=None, controller_id=None,
                   origin_admission_id=None, accepted_timing=None):
        """Persist-before-acknowledge, idempotent per (convoy, node, key).
        Returns (job, created).

        origin_host_id / controller_id record WHO ASKED. Three things
        need them and none of them can be reconstructed later:
        per-peer job-read authorization (a peer may read its own
        delivery records and no others), revocation-driven skip (the
        drain must be able to tell whose work to stop), and audit
        attribution. `None` means the job originated on THIS host --
        which is also how every record written before this field
        existed reads, and that is the correct reading of them.

        Order is crash-safe: claim the marker (atomic gate), write the
        JOB file (durable), then fill the marker to point at it. A crash
        before the job write leaves an empty marker the next retry heals;
        a crash before the marker fill leaves an unreferenced job (never
        acknowledged, so never dispatched) that retention reaps -- neither
        is a duplicate of ACKNOWLEDGED work, because the caller only
        receives the delivery_id after the marker names it.
        """
        if not idempotency_key or not isinstance(idempotency_key, str):
            raise ValueError("idempotency_key is required")
        if not convoy_id or not isinstance(convoy_id, str):
            raise ValueError("convoy_id is required to scope idempotency")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise ValueError("arguments must be an object")

        origin = (origin_host_id if isinstance(origin_host_id, str)
                  and origin_host_id else None)
        request_sha256 = self._request_digest(
            node_id, operation, arguments, convoy_id,
            expected_runtime_id=expected_runtime_id,
            origin_host_id=origin, controller_id=controller_id)
        durable_timing = self._accepted_timing(accepted_timing)
        marker = self._idem_path(convoy_id, node_id, idempotency_key, origin)
        prior = None
        claimed_fresh = self._claim_marker(marker)
        if not claimed_fresh:
            # Seen before. The filled marker names the delivery record;
            # read it FAIL-CLOSED (an unreadable marker raises here rather
            # than re-minting).
            prior = self._read_marker(marker)
        elif origin is None or origin == self.host_id():
            # We claimed a FRESH origin-scoped marker -- but a LOCAL
            # submission may have a pre-scoping marker under the old
            # three-part scope. Re-minting over one would duplicate
            # ACKNOWLEDGED work, which is the annihilation class this
            # whole mechanism exists to prevent. Only a local submission
            # may inherit one: a record with no origin is local by
            # definition, so a PEER must never bind to it.
            legacy = self._idem_path(convoy_id, node_id, idempotency_key)
            if legacy != marker:
                prior = self._read_marker(legacy)
        if prior and prior.get("delivery_id"):
            marker_digest = prior.get("request_sha256")
            if (isinstance(marker_digest, str) and marker_digest
                    and not secrets.compare_digest(marker_digest,
                                                   request_sha256)):
                # The record may have been manually removed or be
                # temporarily unreadable.  The atomic admission marker is
                # still evidence that this key was bound; fail closed
                # rather than heal it into DIFFERENT work.
                if claimed_fresh:
                    try:
                        os.unlink(marker)
                    except OSError:
                        pass
                raise IdempotencyContentConflict(
                    prior["delivery_id"], marker_digest, request_sha256)
            existing = self.get_job(prior["delivery_id"])
            if existing is not None:
                existing_origin = existing.get("origin_host_id")
                if (existing_origin or None) != origin and not (
                        existing_origin is None and origin == self.host_id()):
                    if claimed_fresh:
                        # This can only be the empty origin-scoped marker
                        # claimed before consulting a legacy marker.  Do
                        # not strand it on a refusal: an empty marker is a
                        # crash window that a later retry is allowed to
                        # heal without re-checking legacy state.
                        try:
                            os.unlink(marker)
                        except OSError:
                            pass
                    raise IdempotencyOriginConflict(
                        prior["delivery_id"], existing_origin, origin)
                existing_digest = self._job_request_digest(existing)
                if not secrets.compare_digest(existing_digest,
                                              request_sha256):
                    if claimed_fresh:
                        # Same rolling-upgrade case as above.  Leaving our
                        # fresh marker empty would let the *next* retry
                        # skip the legacy lookup and mint a duplicate.
                        try:
                            os.unlink(marker)
                        except OSError:
                            pass
                    raise IdempotencyContentConflict(
                        prior["delivery_id"], existing_digest,
                        request_sha256)
                if existing.get("state") != "refused":
                    if claimed_fresh and marker != self._idem_path(
                            convoy_id, node_id, idempotency_key,
                            existing.get("origin_host_id")
                            if isinstance(existing.get("origin_host_id"),
                                          str) else None):
                        # We O_EXCL-claimed the four-part marker as an
                        # EMPTY gate, then inherited the record through the
                        # LEGACY marker. Returning now would leave the
                        # four-part marker empty forever, and _read_marker
                        # reads empty as "unfilled" -- so the NEXT retry of
                        # this key re-mints a DUPLICATE of acknowledged
                        # work, the exact annihilation class this scoping
                        # exists to prevent. Fill it to name the inherited
                        # record first.
                        self._fill_marker(
                            marker, existing["delivery_id"], existing_digest)
                    return existing, False
                # A REFUSED record is a delivery this host declined and
                # never made. It must not wedge its key forever: after a
                # re-admission the same key has to be able to produce work
                # again, and answering 200 over a refused record would be
                # a lie. Nothing ran, so re-minting cannot double-run.
            # Marker names a delivery record whose file is gone, or a
            # refused one -- heal by minting a fresh one under it.
        # else: marker was created but never filled (a prior accept
        # crashed between the gate and the job write) -- heal.

        now = self._now()
        delivery_id = "cj_" + identity.mint_id()[:12]
        job = {
            "delivery_id": delivery_id,
            "idempotency_key": idempotency_key,
            "node_id": node_id,
            "convoy_id": convoy_id,
            "operation": operation,
            "arguments": arguments or {},
            # The canonical semantic request bound to this key. A retry
            # may refresh delivery metadata (request id/deadline) but may
            # never substitute different work and receive the old 200.
            "request_sha256": request_sha256,
            # The runtime the caller addressed (A-22), persisted so the
            # DISPATCHER can re-check it at execution time -- the queue
            # spans node restarts, which is exactly when it changes.
            "expected_runtime_id": (expected_runtime_id
                                    if isinstance(expected_runtime_id, str)
                                    and expected_runtime_id else None),
            # WHO ASKED (see the docstring). Stored verbatim only when a
            # non-empty string; anything else is None, so a malformed
            # value can never masquerade as an admitted origin.
            "origin_host_id": (origin_host_id
                               if isinstance(origin_host_id, str)
                               and origin_host_id else None),
            # The ADMISSION LINEAGE this delivery was authorized under
            # (peers admission_id at submission; None for local work).
            # The dispatch fence compares it against the origin's CURRENT
            # lineage, so work submitted before a revocation can never be
            # served after a re-admission -- even work the revocation
            # sweep could not reach (stale_admission).
            "origin_admission_id": (origin_admission_id
                                    if isinstance(origin_admission_id, str)
                                    and origin_admission_id else None),
            "controller_id": (controller_id
                              if isinstance(controller_id, str)
                              and controller_id else None),
            # Host-originated (A-15): the host may rest a record at queued;
            # the execution verdict is mirrored from the node later.
            "state": "queued",
            "result": None,
            # The node-minted execution id (job_<8hex>) and the provenance
            # of the current state. None until a node verdict is mirrored
            # in -- and in Phase 1 nothing dispatches yet, so they stay
            # None. Their presence is what distinguishes a real node
            # verdict from a host-originated state.
            "node_job_id": None,
            "verdict_source": None,
            "observed_at": None,
            # Terminal evidence is acknowledged explicitly by a future
            # controller/job API.  In particular, an indeterminate result
            # is the only proof an operation MAY have executed and is not
            # retention-eligible until that proof has been observed.
            "terminal_at": None,
            "outcome_acknowledged_at": None,
            "created": now,
            "updated": now,
        }
        if durable_timing is not None:
            job.update(durable_timing)
        platform_mod._write_private(
            self._job_path(delivery_id),
            json.dumps(job, indent=1, sort_keys=True) + "\n")
        self._fill_marker(marker, delivery_id, request_sha256)
        if durable_timing is not None:
            supplied_monotonic = accepted_timing.get(
                "accepted_deadline_monotonic")
            try:
                supplied_monotonic = float(supplied_monotonic)
            except (TypeError, ValueError):
                supplied_monotonic = None
            if (supplied_monotonic is not None
                    and math.isfinite(supplied_monotonic)):
                self._monotonic_deadlines[delivery_id] = supplied_monotonic
            else:
                self._monotonic_deadlines[delivery_id] = (
                    self._monotonic()
                    + durable_timing["accepted_remaining_s"])
        self.audit("hoststore", "job_created",
                   {"delivery_id": delivery_id,
                    "idempotency_key": idempotency_key,
                    "operation": operation, "node_id": node_id})
        return job, True

    def job_file_exists(self, delivery_id):
        """Whether the delivery record FILE is present -- distinct from
        get_job, which returns None for absent and unreadable alike. A
        pruner deciding whether bookkeeping may be dropped must treat
        unreadable as live (the conservative direction)."""
        if (not delivery_id or not isinstance(delivery_id, str)
                or "/" in delivery_id or "\\" in delivery_id
                or ".." in delivery_id):
            return False
        return os.path.exists(self._job_path(delivery_id))

    def get_job(self, delivery_id):
        if (not delivery_id or "/" in delivery_id or "\\" in delivery_id
                or ".." in delivery_id):
            return None         # never let an id escape the jobs dir
        try:
            with open(self._job_path(delivery_id), "r",
                      encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def _restore_expiry_anchors(self):
        """Anchor durable wall expiries to this process's monotonic clock.

        Monotonic timestamps cannot survive a restart because their epoch
        is local to a boot/process.  At startup we therefore compute the
        remaining duration from the durable target-accepted wall expiry,
        cap it at the originally accepted duration (so a clock rollback
        cannot mint *more* than the original budget), and start a fresh
        local monotonic countdown.  job_timing additionally treats a
        material wall rollback before accepted_at as expired.
        """
        try:
            wall_now = float(self._now())
            mono_now = float(self._monotonic())
        except (TypeError, ValueError):
            return
        if not math.isfinite(wall_now) or not math.isfinite(mono_now):
            return
        for job in self.jobs():
            delivery_id = job.get("delivery_id")
            try:
                expires = float(job.get("accepted_expires_unix"))
                accepted_remaining = float(job.get("accepted_remaining_s"))
            except (TypeError, ValueError):
                continue
            if (not delivery_id or not math.isfinite(expires)
                    or not math.isfinite(accepted_remaining)):
                continue
            remaining = min(expires - wall_now, accepted_remaining)
            self._monotonic_deadlines[delivery_id] = (
                mono_now + max(0.0, remaining))

    def job_timing(self, job_or_delivery_id, now=None, monotonic_now=None):
        """Return the target-local delivery budget for a durable job.

        ``bounded`` is False for local/legacy jobs with no accepted
        envelope timing.  A bounded job is expired when EITHER its
        durable wall deadline or its process-local monotonic deadline is
        exhausted.  This is the conservative direction under clock
        changes and provides a single integration point for dispatchers.
        """
        job = (job_or_delivery_id if isinstance(job_or_delivery_id, dict)
               else self.get_job(job_or_delivery_id))
        if job is None:
            return {"bounded": False, "expired": True, "remaining_s": 0.0,
                    "reason": "unknown_job"}
        if "accepted_expires_unix" not in job:
            return {"bounded": False, "expired": False,
                    "remaining_s": None, "reason": None}
        try:
            wall_now = float(self._now() if now is None else now)
            mono_now = float(self._monotonic() if monotonic_now is None
                             else monotonic_now)
            expires = float(job.get("accepted_expires_unix"))
            accepted_at = float(job.get("accepted_at_unix"))
            accepted_remaining = float(job.get("accepted_remaining_s"))
        except (TypeError, ValueError):
            return {"bounded": True, "expired": True, "remaining_s": 0.0,
                    "reason": "timing_corrupt"}
        if not all(math.isfinite(v) for v in (
                wall_now, mono_now, expires, accepted_at,
                accepted_remaining)):
            return {"bounded": True, "expired": True, "remaining_s": 0.0,
                    "reason": "timing_corrupt"}
        if wall_now < accepted_at - MAX_CLOCK_ROLLBACK_S:
            return {"bounded": True, "expired": True, "remaining_s": 0.0,
                    "reason": "clock_rollback"}

        delivery_id = job.get("delivery_id")
        mono_deadline = self._monotonic_deadlines.get(delivery_id)
        if mono_deadline is None:
            # Lazy fallback for a file that appeared after startup.  Cap
            # by the originally accepted duration for rollback safety.
            initial = min(expires - wall_now, accepted_remaining)
            mono_deadline = mono_now + max(0.0, initial)
            if delivery_id:
                self._monotonic_deadlines[delivery_id] = mono_deadline
        remaining = min(expires - wall_now, mono_deadline - mono_now,
                        accepted_remaining)
        remaining = max(0.0, remaining)
        return {"bounded": True, "expired": remaining <= 0.0,
                "remaining_s": remaining,
                "reason": "deadline_exceeded" if remaining <= 0.0 else None,
                "accepted_expires_unix": expires,
                "request_deadline_unix": job.get("request_deadline_unix")}

    def claim_for_dispatch(self, delivery_id):
        """Compare-and-set queued -> dispatching: the dispatcher's CLAIM.

        Returns the claimed job, or None when the job is not claimable --
        unknown, already claimed by another dispatcher, or past queued.
        None (not an exception) because the drain loop works from a
        SNAPSHOT of queued ids: by the time it reaches one, a concurrent
        /dispatch may already own or have finished it, and that is a
        skip, not an error.

        The read-then-write pair is NOT atomic at the store level; the
        host app serializes every claim under its lock. What the store
        guarantees is durability: the claim is on disk before any forward
        happens, so a host that dies mid-forward leaves the claim behind
        for the load-time sweep to resolve honestly.
        """
        job = self.get_job(delivery_id)
        if job is None or job.get("state") != "queued":
            return None
        timing = self.job_timing(job)
        if timing["expired"]:
            evidence = {
                "reason": timing.get("reason") or "deadline_exceeded",
                "detail": "the accepted delivery budget expired before "
                          "the operation reached the node",
                "accepted_expires_unix": timing.get(
                    "accepted_expires_unix",
                    job.get("accepted_expires_unix")),
                "request_deadline_unix": timing.get(
                    "request_deadline_unix",
                    job.get("request_deadline_unix")),
            }
            self.mark_refused(delivery_id, evidence)
            try:
                self.audit("hoststore", "job_expired_before_dispatch",
                           {"delivery_id": delivery_id,
                            "reason": evidence["reason"]})
            except OSError:
                pass
            return None
        return self._apply_state(delivery_id, "dispatching",
                                 host_originated=True)

    def release_claim(self, delivery_id):
        """Compare-and-set dispatching -> queued: the forward was REFUSED
        (connection refused = the request never reached the node, so the
        op did not run) and the job goes back to retry. Returns the
        released job, or None when the job is not currently claimed.

        Only for the never-delivered case. A forward that got no RESPONSE
        may still have executed -- that resolves to mark_indeterminate,
        never back to queued."""
        job = self.get_job(delivery_id)
        if job is None or job.get("state") != "dispatching":
            return None
        return self._apply_state(delivery_id, "queued",
                                 host_originated=True)

    def _sweep_interrupted_dispatches(self):
        """Resolve every job left 'dispatching' by a dead host.

        Runs at store load, so a loaded store NEVER holds a live claim --
        a claim can only belong to the running process (one host app per
        data dir; A-36's exactly-one-supervisor owns that invariant). A
        job found dispatching here was claimed by a host run that ended
        -- crash, kill, or a stop that could not wait out a forward --
        before recording an outcome; the forward MAY have happened, so
        the only honest resolution is indeterminate with that as
        evidence. Never back to queued (a mutation could double-run),
        never a verdict (A-15: no node was observed).

        It sweeps 'dispatching' ONLY, and that narrowness is load-bearing.
        A 'running' record is not a host claim: it is a mirror of a NODE
        job the node still owns and will still answer for after this host
        restarts (the poller resumes it). Widening this to "resolve
        everything non-terminal" would burn every in-flight node job to
        indeterminate on every host start -- pinned by
        test_the_load_sweep_touches_only_dispatching."""
        swept = []
        for job in self.jobs(state="dispatching"):
            self.mark_indeterminate(job["delivery_id"], {
                "reason": "host_exited_mid_dispatch",
                "detail": "a previous host app run left this job claimed "
                          "for dispatch (crash, kill, or a stop that "
                          "could not wait); the operation may have run",
                "operation": job.get("operation")})
            swept.append(job["delivery_id"])
        if swept:
            self.audit("hoststore", "interrupted_dispatches_swept",
                       {"count": len(swept), "delivery_ids": swept[:16]})

    def record_node_verdict(self, delivery_id, node_status, node_job_id,
                            observed_at, result=None):
        """Mirror a verdict OBSERVED on the node into the host record.

        A-15: the node originates the execution verdict; the host only
        caches it, and only with the provenance that proves a node
        produced it -- node_job_id (the node's own job_<8hex>) and
        observed_at. This is the strongest form of "one authority": the
        method that would write "succeeded" cannot be called without the
        evidence a node returned it, so the host can never fabricate a
        success. node_status is the shipped node vocabulary
        (running|done|error); it maps to the host mirror.

        ONE regression is refused: 'running' onto an already-TERMINAL
        record. A poll answer can land after the job settled (a slow
        response overtaken by a later poll, or by the stale-record
        terminalisation), and applying it would drag a finished job back
        to running and re-open it for polling forever. Only this
        direction is blocked -- a node correcting done -> error is a real
        verdict it authored, and release_claim's dispatching -> queued
        must keep working, so a blanket state machine here would break
        both.
        """
        state = _NODE_STATUS_TO_STATE.get(node_status)
        if state is None:
            raise ValueError(
                f"unknown node status {node_status!r} (expected one of "
                f"{sorted(_NODE_STATUS_TO_STATE)})")
        if not node_job_id or not observed_at:
            raise ValueError(
                "a cached node verdict MUST carry node_job_id and "
                "observed_at -- its provenance is what separates it from a "
                "host-fabricated result (A-15)")
        if state == "running":
            current = self.get_job(delivery_id)
            if current is not None and current.get("state") in TERMINAL_STATES:
                try:
                    self.audit("hoststore", "verdict_regression_ignored",
                               {"delivery_id": delivery_id,
                                "state": current.get("state"),
                                "node_job_id": node_job_id,
                                "observed_at": observed_at})
                except OSError:
                    pass        # the REFUSAL is the contract; the trail
                                # is best-effort, exactly as elsewhere
                return current
        return self._apply_state(delivery_id, state, result=result,
                                 verdict_source="node_poll",
                                 observed_at=observed_at,
                                 node_job_id=node_job_id)

    def record_sync_result(self, delivery_id, ok, observed_at, result=None):
        """Record the verdict of a SYNCHRONOUS relay: the host forwarded
        the operation to the node's Envoy and got the response inline.

        Still a NODE-originated verdict under A-15 -- the success/failure
        came FROM the node's execution, the host only mirrors it -- but
        tagged `node_sync` rather than `node_poll` to be honest about the
        mechanism: a synchronous op mints no node-side job_<8hex>, so
        there is no node_job_id, and observed_at (when the response
        arrived) is the provenance. The host still cannot reach here
        without an actual node response to mirror.
        """
        if not observed_at:
            raise ValueError(
                "a sync result must carry observed_at -- when the node's "
                "response was seen is its provenance")
        return self._apply_state(delivery_id, "succeeded" if ok else "failed",
                                 result=result, verdict_source="node_sync",
                                 observed_at=observed_at)

    def record_host_result(self, delivery_id, ok, observed_at, result=None):
        """Record a verdict authored by the reviewed host subprocess facade.

        This is deliberately narrower than a generic "host may succeed"
        escape hatch: only the reviewed host-native operation names may use
        it, and only while their durable delivery is in the dispatching claim
        state. Ordinary TD relays remain subject to A-15's node provenance.
        """
        if not isinstance(ok, bool):
            raise ValueError("a host operation verdict requires boolean ok")
        if not observed_at:
            raise ValueError(
                "a host operation result must carry observed_at")
        job = self.get_job(delivery_id)
        if job is None:
            raise KeyError(delivery_id)
        if (job.get("state") != "dispatching"
                or job.get("operation") not in _HOST_VERDICT_OPERATIONS):
            raise ValueError(
                "host operation verdict requires a claimed reviewed "
                "host-operation delivery")
        return self._apply_state(
            delivery_id, "succeeded" if ok else "failed", result=result,
            verdict_source="host_operation", observed_at=observed_at)

    def reconcile_lifecycle_result(self, delivery_id, ok, observed_at,
                                   result=None):
        """Repair only a lifecycle delivery proven by its private ledger.

        The load sweep must classify a prior process's ``dispatching`` claim
        as indeterminate: at that point HostStore alone cannot know whether
        execution ran.  Exact-node lifecycle has a second, crash-safe attempt
        ledger whose committed terminal result is stronger evidence.  This
        method is the deliberately narrow bridge between those authorities;
        it is not a generic terminal-state rewrite API.
        """
        if not isinstance(ok, bool) or not observed_at:
            raise ValueError(
                "a recovered lifecycle verdict requires boolean ok and "
                "observed_at")
        job = self.get_job(delivery_id)
        if job is None:
            raise KeyError(delivery_id)
        if job.get("operation") not in (
                "convoy_start_node", "convoy_restart_node"):
            raise ValueError(
                "only reviewed lifecycle deliveries may be reconciled")
        if (not isinstance(result, dict)
                or result.get("operation_id") != delivery_id
                or result.get("capability") != "host.td-lifecycle/v1"
                or result.get("ok") is not ok
                or result.get("node_id") != job.get("node_id")):
            raise ValueError(
                "recovered lifecycle result is not bound to this delivery")
        state = job.get("state")
        desired = "succeeded" if ok else "failed"
        if state == desired:
            if (job.get("verdict_source") in (
                    "host_operation", "host_operation_recovery")
                    and job.get("result") == result):
                return job
            raise ValueError("existing lifecycle terminal evidence differs")
        if state != "indeterminate":
            raise ValueError(
                "lifecycle recovery requires an indeterminate delivery")
        evidence = job.get("result")
        reason = evidence.get("reason") if isinstance(evidence, dict) else None
        if reason not in (
                "host_exited_mid_dispatch", "claim_stranded",
                "host_operation_result_recording_failed"):
            raise ValueError(
                "indeterminate evidence is not an interrupted host dispatch")
        updated = self._apply_state(
            delivery_id, desired, result=result,
            verdict_source="host_operation_recovery",
            observed_at=observed_at)
        try:
            self.audit("hoststore", "lifecycle_result_reconciled", {
                "delivery_id": delivery_id, "state": desired,
                "prior_reason": reason})
        except OSError:
            pass
        return updated

    def mark_indeterminate(self, delivery_id, evidence):
        """Host-ORIGINATED terminal outcome: the operation MAY have run
        and the node cannot be observed. The one execution-ambiguous
        state the host may write on its own authority, because only the
        host witnessed the dispatch-without-response. Never delete such a
        record -- it is the sole proof a consequential operation may have
        executed (16.4), so evidence is required."""
        if not evidence:
            raise ValueError(
                "mark_indeterminate requires evidence (what was seen, and "
                "why the outcome is unknown)")
        return self._apply_state(delivery_id, "indeterminate",
                                 result=evidence, verdict_source="host",
                                 host_originated=True)

    def mark_refused(self, delivery_id, evidence):
        """Host-ORIGINATED terminal DELIVERY verdict: this host declined
        to route the job, and it never ran (A-54, proposed).

        Requires evidence for exactly the reason mark_indeterminate does
        -- a terminal written on the host's own authority must carry what
        the host saw.

        CAS'd to `queued` on purpose, and the narrowness is the safety
        argument. `refused` is only honest while the operation provably
        never left this host; a `dispatching` job's forward may already
        be in flight (its honest resolution is the dispatcher's, and may
        be indeterminate), and a `running` job is executing on a node
        that still owns its verdict. Refusing to terminalise those is how
        revocation stops NEW work without rewriting history. Returns the
        refused job, or None when the job is absent or no longer queued.
        """
        if not evidence:
            raise ValueError(
                "mark_refused requires evidence (why delivery was "
                "declined) -- a host-originated terminal must carry it")
        job = self.get_job(delivery_id)
        if job is None or job.get("state") != "queued":
            return None
        return self._apply_state(delivery_id, "refused", result=evidence,
                                 verdict_source="host",
                                 host_originated=True)

    def record_dispatch_note(self, delivery_id, reason, at):
        """Count a dispatch ATTEMPT that ended in a refusal, on the
        delivery record itself. Never touches state, result, or verdict
        provenance -- it is bookkeeping, not an outcome.

        Why it has to be on the RECORD and not only in the audit trail:
        a job that requeues on every pass (a node refusal that will
        never resolve) audits ONCE, by design -- the dedupe that keeps
        audit.jsonl bounded. Without a counter, that job is byte-
        indistinguishable from one enqueued a second ago, and the retry
        loop is invisible to /jobs. The host still does not terminalise
        it (that stays the deferred reaper's call, A-15 item b), but the
        evidence a human needs to SEE it now exists.
        """
        job = self.get_job(delivery_id)
        if job is None:
            return None
        job["attempts"] = int(job.get("attempts") or 0) + 1
        job["last_attempt"] = {"at": at, "reason": str(reason)[:128]}
        job["updated"] = self._now()
        platform_mod._write_private(
            self._job_path(delivery_id),
            json.dumps(job, indent=1, sort_keys=True) + "\n")
        return job

    def acknowledge_outcome(self, delivery_id, acknowledged_at=None):
        """Record that a terminal outcome was observed by its controller.

        This is evidence metadata, not an execution verdict, and cannot
        alter state/result/provenance.  It gives retention a safe answer
        to "has anybody seen the only may-have-run proof?".  The host API
        must perform job-read authorization before calling this method.
        """
        job = self.get_job(delivery_id)
        if job is None:
            raise KeyError(delivery_id)
        if job.get("state") not in TERMINAL_STATES:
            raise ValueError("only a terminal outcome can be acknowledged")
        at = self._now() if acknowledged_at is None else acknowledged_at
        try:
            at = float(at)
        except (TypeError, ValueError):
            raise ValueError("acknowledged_at must be numeric")
        if not math.isfinite(at):
            raise ValueError("acknowledged_at must be finite")
        if job.get("outcome_acknowledged_at") is None:
            job["outcome_acknowledged_at"] = at
            job["updated"] = self._now()
            platform_mod._write_private(
                self._job_path(delivery_id),
                json.dumps(job, indent=1, sort_keys=True) + "\n")
        return job

    def _apply_state(self, delivery_id, state, result=None,
                     verdict_source=None, observed_at=None, node_job_id=None,
                     host_originated=False):
        if state not in JOB_STATES:
            raise ValueError(f"unknown job state {state!r}")
        if state not in _HOST_ORIGINABLE_STATES:
            # A-15 ENFORCED BY STATE, not by the caller's flag. The first
            # cut of this guard fired only when host_originated=True was
            # PASSED, so simply OMITTING the flag let a host-side writer
            # originate a terminal 'succeeded' with verdict_source=host
            # and no executor provenance (measured 2026-08-02). An execution
            # verdict (running/succeeded/failed) may land only with the
            # provenance that proves the node or reviewed host subprocess
            # produced it: an executor verdict_source, observed_at, and --
            # for the poll path -- the node's own job id.  The three public
            # record_* methods validate the same things at their own doors;
            # this is the belt at the ONLY write that can rest a record in one
            # of these states.
            if host_originated:
                raise ValueError(
                    f"A-15: the host may not originate {state!r} -- that "
                    f"is an EXECUTION verdict and only the executor-specific "
                    f"recording path can author one (coordination-originable: "
                    f"{list(_HOST_ORIGINABLE_STATES)})")
            if verdict_source not in _EXECUTION_VERDICT_SOURCES:
                raise ValueError(
                    f"A-15: {state!r} is an execution verdict and must "
                    f"carry an executor verdict_source "
                    f"({list(_EXECUTION_VERDICT_SOURCES)}); got "
                    f"{verdict_source!r}")
            if not observed_at:
                raise ValueError(
                    f"A-15: {state!r} must carry observed_at -- when the "
                    f"executor's answer was seen is its provenance")
            if verdict_source == "node_poll" and not node_job_id:
                raise ValueError(
                    f"A-15: a polled {state!r} must carry node_job_id -- "
                    f"the node-minted job id is its provenance")
        job = self.get_job(delivery_id)
        if job is None:
            raise KeyError(delivery_id)
        prior_state = job.get("state")
        prior_result = job.get("result")
        effective_result = result if result is not None else prior_result
        terminal_changed = (state in TERMINAL_STATES and (
            prior_state != state or effective_result != prior_result))
        job["state"] = state
        if state in TERMINAL_STATES and (
                job.get("terminal_at") is None or terminal_changed):
            job["terminal_at"] = self._now()
        if prior_state in TERMINAL_STATES and terminal_changed:
            # An acknowledgement belongs to the exact terminal evidence
            # the controller observed.  A later node-authored correction
            # (done -> error, or a changed result) is new evidence and
            # must be acknowledged again before retention may remove it.
            job["outcome_acknowledged_at"] = None
        if result is not None:
            job["result"] = result
        if verdict_source is not None:
            job["verdict_source"] = verdict_source
        if observed_at is not None:
            job["observed_at"] = observed_at
        if node_job_id is not None:
            job["node_job_id"] = node_job_id
        job["updated"] = self._now()
        platform_mod._write_private(
            self._job_path(delivery_id),
            json.dumps(job, indent=1, sort_keys=True) + "\n")
        return job

    def _delivery_ids(self):
        """Every delivery record id on disk. Raises on a listing failure.

        Skips the per-key idempotency markers and any underscore-prefixed
        bookkeeping file -- only cj_ delivery records.
        """
        return [name[:-len(".json")]
                for name in sorted(os.listdir(self.jobs_dir))
                if name.endswith(".json") and not name.startswith("_")
                and not name.startswith("idem_")]

    def scan_jobs(self):
        """(jobs, unreadable_ids) -- UNREADABLE IS NOT ABSENT.

        jobs() swallows both: get_job returns None for an absent file AND
        for one it could not read, and a failed listdir comes back as an
        empty list. That is right for the DRAIN (a record it cannot read
        this pass is simply not dispatched, and the next pass retries),
        and it is exactly wrong for a REVOCATION, where "read nothing"
        and "found nothing" have to be different answers -- otherwise a
        sweep that examined no records at all reports a clean containment.

        The listing failure RAISES rather than returning empty, for the
        same reason. Callers that must not proceed on a blind scan can
        then say so.
        """
        jobs, unreadable = [], []
        for delivery_id in self._delivery_ids():
            job = self.get_job(delivery_id)
            if job is None:
                if self.job_file_exists(delivery_id):
                    unreadable.append(delivery_id)
                continue
            jobs.append(job)
        jobs.sort(key=lambda j: (j.get("created", 0),
                                 j.get("delivery_id", "")))
        return jobs, unreadable

    def jobs(self, state=None):
        try:
            ids = self._delivery_ids()
        except OSError:
            # Unchanged, and deliberately so: the drain's snapshot treats
            # an unreadable listing as "nothing to do this pass" and
            # retries. Anything that needs the distinction uses scan_jobs.
            return []
        out = []
        for delivery_id in ids:
            job = self.get_job(delivery_id)
            if job and (state is None or job.get("state") == state):
                out.append(job)
        out.sort(key=lambda j: (j.get("created", 0),
                                j.get("delivery_id", "")))
        return out

    def state_counts(self):
        """{state: count} over every delivery record, in ONE scan.

        status() needs several of these numbers at once, and each
        jobs(state=...) call parses every job file on disk -- three
        filtered calls read the queue three times under the app lock.
        States with no jobs are simply absent (the caller reads with a
        default), so this never claims a state exists that does not.
        """
        counts = {}
        for job in self.jobs():
            state = job.get("state")
            counts[state] = counts.get(state, 0) + 1
        return counts

    def _markers_for(self, job):
        """Every idempotency marker path that could name THIS record.

        A job carries the four parts of its own scope, so the marker is
        recomputable -- there is no back-reference to store. A LOCAL job
        (origin None) uses the three-part legacy scope; a peer job uses
        the four-part scope; the origin-set case also checks the legacy
        marker, because a local retry that inherited one leaves it behind.
        """
        key = job.get("idempotency_key")
        convoy_id = job.get("convoy_id")
        node_id = job.get("node_id")
        if not (key and convoy_id and node_id):
            return []
        origin = (job.get("origin_host_id")
                  if isinstance(job.get("origin_host_id"), str) else None)
        paths = [self._idem_path(convoy_id, node_id, key, origin)]
        if origin:
            legacy = self._idem_path(convoy_id, node_id, key)
            if legacy not in paths:
                paths.append(legacy)
        return paths

    def _classify_unreadable(self, delivery_id, cutoff):
        """Why get_job answered None -- ('absent'|'young'|'transient'|
        'corrupt', mtime).

        Only 'corrupt' licenses touching the file, and it requires
        PROOF: the bytes were read successfully and json refused them.
        Every failure to read is 'transient', because at this layer a
        lock and a truncation are the same errno -- get_job collapses
        OSError and ValueError into one None, which is exactly how a
        valid record came to be deleted.

        The read is retried the way _read_marker's is: an antivirus or
        backup handle, or the os.replace of a concurrent writer (this
        pass holds no lock), is a window of milliseconds, and spending
        a few of them is much cheaper than being wrong.
        """
        path = self._job_path(delivery_id)
        if not self.job_file_exists(delivery_id):
            return "absent", 0.0
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return "transient", 0.0
        if mtime > cutoff:
            return "young", mtime
        raw = None
        for attempt in range(4):
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    raw = handle.read()
                break
            except OSError:
                if attempt == 3:
                    return "transient", mtime
                time.sleep(0.05 * (attempt + 1))
        try:
            json.loads(raw)
        except ValueError:
            return "corrupt", mtime
        except Exception:
            return "transient", mtime
        # It parses NOW, so whatever get_job hit has passed. Leave it:
        # the next pass reads it normally and reaps it on the ordinary
        # rules, with both safety gates applied.
        return "transient", mtime

    def reap(self, retention_s, now=None, max_delete=None, on_reap=None):
        """Delete retention-eligible TERMINAL records older than
        retention_s, and the idempotency markers that name them. An
        unacknowledged ``indeterminate`` record is never eligible because
        it is the sole may-have-run proof. Returns
        {jobs, markers, listing_failed}.

        NOTHING ELSE in this store ever deletes -- without this the jobs
        dir is an unbounded pile of files, and EVERY status()/revocation
        scan reads all of it, so a supervised host gets measurably slower
        every week it runs (0.17s at 1k jobs, 8.1s at 50k, all under the
        app lock on /status). A record is reapable only when its state is
        TERMINAL (a node verdict is in, or the host refused it) AND its
        last update predates the window, so in-flight work and the
        idempotency horizon are both preserved; retention is >> that
        horizon by design (a retry lands in seconds, retention in hours).

        LOCK-FREE off the drain loop: it only unlinks records already in
        a terminal state, and a concurrent reader that held the id finds
        it gone (get_job -> None), which every caller already tolerates.
        Markers are unlinked BEFORE the job so a crash mid-reap leaves an
        unreferenced job the next pass retries, never a filled marker
        pointing at a deleted record.
        """
        if on_reap is not None and not callable(on_reap):
            raise ValueError("on_reap must be callable")
        now = self._now() if now is None else now
        cutoff = now - retention_s
        reaped_jobs = reaped_markers = quarantined = 0
        try:
            ids = self._delivery_ids()
        except OSError:
            return {"jobs": 0, "markers": 0, "quarantined": 0,
                    "listing_failed": True}
        for delivery_id in ids:
            if max_delete is not None and reaped_jobs >= max_delete:
                break
            job = self.get_job(delivery_id)
            if job is None:
                # Gone, or UNREADABLE. An unreadable record used to live
                # here for ever: reap skipped it by definition, and it
                # blocks every node-cleanup sweep (a record nobody can
                # parse might be a pending delivery for the row being
                # retired), so one truncated file -- a crash mid-write --
                # froze all node cleanup on the host permanently.
                #
                # It is NEVER deleted, and never on one failed read.
                # get_job answers None for a torn file AND for a file it
                # merely could not open this instant (an antivirus or
                # backup handle, fd exhaustion, or -- since this pass is
                # LOCK-FREE while writers hold the app lock -- a
                # concurrent os.replace). Deleting on that evidence
                # destroyed valid records, including the one class this
                # store must never lose: an unacknowledged
                # `indeterminate`, the sole may-have-run proof. The
                # branch also sits above both of reap's safety gates, so
                # a misread bypassed them entirely (reproduced with a
                # transient EMFILE, 2026-08-07).
                #
                # So: prove corruption, then QUARANTINE. A renamed file
                # leaves _delivery_ids (which lists only *.json) and
                # stops blocking cleanup, while still being on disk for
                # a human -- the outcome is recoverable either way the
                # classification errs.
                if quarantined >= _MAX_QUARANTINE_PER_PASS:
                    continue
                verdict, mtime = self._classify_unreadable(
                    delivery_id, cutoff)
                if verdict != "corrupt":
                    continue
                path = self._job_path(delivery_id)
                try:
                    os.replace(path, path + _QUARANTINE_SUFFIX)
                except OSError:
                    continue
                quarantined += 1
                try:
                    self.audit("hoststore", "unreadable_job_quarantined",
                               {"delivery_id": delivery_id,
                                "age_s": round(now - mtime, 1),
                                "renamed_to": os.path.basename(
                                    path + _QUARANTINE_SUFFIX)})
                except Exception:
                    pass
                continue
            if job.get("state") not in TERMINAL_STATES:
                continue
            # `indeterminate` is the only durable proof that a command may
            # have executed.  Deleting it merely because its controller
            # was offline for the retention window makes a blind retry
            # possible.  It becomes retention-eligible only after the
            # controller explicitly acknowledges seeing that outcome.
            if (job.get("state") == "indeterminate"
                    and job.get("outcome_acknowledged_at") is None):
                continue
            # `x or fallback` is the falsy-zero trap on a timestamp: a
            # legitimately small `updated` would fall through to created.
            # Prefer updated, fall back only when it is truly absent.
            updated = job.get("updated")
            if updated is None:
                updated = job.get("created")
            if updated is None:
                updated = 0
            if updated > cutoff:
                continue
            for marker in self._markers_for(job):
                # Only unlink a marker that still names THIS record -- a
                # marker re-minted onto a newer job must survive.
                try:
                    m = self._read_marker(marker)
                except Exception:
                    m = None    # unreadable: leave it; reap the job anyway
                if m is not None and m.get("delivery_id") != delivery_id:
                    continue
                try:
                    os.unlink(marker)
                    reaped_markers += 1
                except FileNotFoundError:
                    pass
                except OSError:
                    pass
            try:
                os.unlink(self._job_path(delivery_id))
                reaped_jobs += 1
                if on_reap is not None:
                    try:
                        on_reap(dict(job))
                    except Exception:
                        # Retention already committed. Cleanup callbacks are
                        # best-effort and may be retried by their own index
                        # reconciliation; they must not kill the drain loop.
                        pass
            except FileNotFoundError:
                pass
            except OSError:
                pass
        if reaped_jobs or reaped_markers:
            try:
                self.audit("hoststore", "jobs_reaped",
                           {"jobs": reaped_jobs, "markers": reaped_markers,
                            "retention_s": retention_s})
            except Exception:
                pass
        return {"jobs": reaped_jobs, "markers": reaped_markers,
                "quarantined": quarantined, "listing_failed": False}

    # -- audit (A-40: host-side, never the Embody logger) ---------------

    def audit(self, actor, event, detail=None):
        line = json.dumps({"ts": self._now(), "actor": actor,
                           "event": event, "detail": detail or {}},
                          sort_keys=True)
        path = os.path.join(self.dir, AUDIT_FILE)
        # Bound the file BEFORE appending: an append-only log with no
        # ceiling is the disk-fill vector the killswitch backlog opens.
        # Best-effort -- a rotation that loses the race (Windows refuses
        # os.replace on a file another thread has open) just means one
        # slightly-oversized append and a retry next line, never a lost
        # record or a raised handler.
        try:
            if os.path.getsize(path) >= AUDIT_MAX_BYTES:
                self._rotate_audit(path)
        except OSError:
            pass
        # Append-only: a partial append can damage at most the LAST line,
        # never an earlier record, and pruning is a truncate.
        with open(path, "a", encoding="utf-8", newline="\n") as f:
            f.write(line + "\n")

    def _rotate_audit(self, path):
        """Rename the current audit aside, keeping ONE generation.

        A concurrent appender either committed just before the rename
        (its line is in the rotated file) or opens fresh just after (its
        line is in the new file) -- never lost. Best-effort throughout.
        """
        keep = os.path.join(self.dir, AUDIT_ROTATED)
        try:
            os.replace(path, keep)   # atomic; overwrites the prior .1
        except OSError:
            pass

    def audit_tail(self, limit=50):
        try:
            with open(os.path.join(self.dir, AUDIT_FILE), "r",
                      encoding="utf-8") as f:
                lines = f.readlines()
        except OSError:
            return []
        out = []
        for line in lines[-int(limit):]:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue        # a torn final line is skipped, not fatal
        return out
