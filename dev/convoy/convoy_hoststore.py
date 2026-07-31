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

JOB_STATES = ("queued", "running", "succeeded", "failed", "indeterminate")

# A-15, "one authority, two records". The NODE originates the execution
# verdict; the host record MIRRORS it. So the host may write two states
# on its own authority and no more:
#   queued        -- accepted, not yet dispatched (there is no dispatcher
#                    in Phase 1, so this is where a host job rests).
#   indeterminate -- dispatched, outcome unobservable (partition, node
#                    gone, TD restarted mid-job). Only the host witnessed
#                    the dispatch-without-response, so only the host can
#                    record it -- and it is never deleted, it is the sole
#                    proof a consequential operation MAY have run (16.4).
# running / succeeded / failed are NODE verdicts: they may only be
# recorded through record_node_verdict, which demands the provenance
# (node_job_id + observed_at) that proves a node produced them. The host
# cannot fabricate a success -- the method that would write it refuses
# without the evidence.
_HOST_ORIGINABLE_STATES = ("queued", "indeterminate")

# The shipped node vocabulary (EnvoyExt _job_public: running|done|error)
# mapped to the host mirror. Kept explicit so a new node status cannot
# silently map to a host state by coincidence.
_NODE_STATUS_TO_STATE = {"running": "running", "done": "succeeded",
                         "error": "failed"}


class StoreTooNew(Exception):
    """Written by a NEWER host app. Refuse to write, never scribble."""

    def __init__(self, disk_version, code_version):
        super().__init__(
            f"host store is v{disk_version}, this build understands "
            f"v{code_version} -- refusing to write; upgrade embody-convoy")
        self.disk_version = disk_version
        self.code_version = code_version


class HostStore:
    def __init__(self, directory, now=None):
        self._now = now or time.time
        self.dir = directory
        self.jobs_dir = os.path.join(directory, JOBS_DIR)
        os.makedirs(self.jobs_dir, exist_ok=True)
        self._host_path = os.path.join(directory, HOST_FILE)
        self._state = self._load_host_file()

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
        replayed: replaying them against the CURRENT host_id would make a
        COPIED state directory reproduce the same node_ids on a second
        machine, and could carry a TD-Python approval onto a different
        node. Returns (directory, quarantined).
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
                    row.get("convoy_id"), minted_id=node_id)
            except identity.IdentityError as e:
                # One bad entry must not make a SUPERVISED daemon
                # crash-loop -- the spike proved it restarts every 60s.
                quarantined.append({**row, "node_id": node_id,
                                    "load_error": e.reason})
                continue
            if row.get("td_python_approved"):
                directory.approve_td_python(record["node_id"])
        if quarantined:
            self.audit("hoststore", "nodes_quarantined_on_load",
                       {"count": len(quarantined),
                        "node_ids": [r["node_id"] for r in quarantined][:16]})
        return directory, quarantined

    def save_node(self, record):
        now = self._now()
        existing = self._state["nodes"].get(record["node_id"], {})
        self._state["nodes"][record["node_id"]] = {
            # project_root + comp_path IS the key. runtime_id is NOT
            # stored: it is per-launch by design.
            "project_root": record["project_root"],
            "host_id": record["host_id"],
            "convoy_id": record["convoy_id"],
            "comp_path": record["comp_path"],
            "td_python_approved": bool(record["td_python_approved"]),
            "first_seen": existing.get("first_seen", now),
            "last_seen": now,
        }
        self._write_host()

    def delete_node(self, node_id):
        if self._state["nodes"].pop(node_id, None) is not None:
            self._write_host()

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

    def _idem_path(self, convoy_id, node_id, idempotency_key):
        """Path of the per-key admission MARKER.

        Idempotency is scoped, never global: a single global key space
        meant a submission for node B returned node A's job, and let one
        convoy squat a guessable key across every other convoy. The three
        parts are NUL-joined (a NUL cannot appear in any of them, so no
        combination can forge another scope) and hashed, so arbitrary key
        text can neither escape the jobs dir nor collide with a cj_ job
        file.

        ONE FILE PER KEY, deliberately: the old shared _by_key.json blob
        turned a single unreadable read into "no keys exist" and re-minted
        a duplicate job for EVERY prior key (proven 2026-07-31). A per-key
        marker's blast radius is one key, and its read fails CLOSED.
        """
        scope = "\x00".join((convoy_id, node_id, idempotency_key))
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

    def create_job(self, idempotency_key, node_id, operation, arguments,
                   convoy_id):
        """Persist-before-acknowledge, idempotent per (convoy, node, key).
        Returns (job, created).

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

        marker = self._idem_path(convoy_id, node_id, idempotency_key)
        if not self._claim_marker(marker):
            # Seen before. The filled marker names the delivery record;
            # read it FAIL-CLOSED (an unreadable marker raises here rather
            # than re-minting).
            prior = self._read_marker(marker)
            if prior and prior.get("delivery_id"):
                existing = self.get_job(prior["delivery_id"])
                if existing is not None:
                    return existing, False
                # Marker names a delivery record whose file is gone -- heal
                # by minting a fresh one under the same marker.
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
            "created": now,
            "updated": now,
        }
        platform_mod._write_private(
            self._job_path(delivery_id),
            json.dumps(job, indent=1, sort_keys=True) + "\n")
        platform_mod._write_private(
            marker,
            json.dumps({"delivery_id": delivery_id, "created": now},
                       sort_keys=True) + "\n")
        self.audit("hoststore", "job_created",
                   {"delivery_id": delivery_id,
                    "idempotency_key": idempotency_key,
                    "operation": operation, "node_id": node_id})
        return job, True

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
                                 result=evidence, verdict_source="host")

    def _apply_state(self, delivery_id, state, result=None,
                     verdict_source=None, observed_at=None, node_job_id=None):
        if state not in JOB_STATES:
            raise ValueError(f"unknown job state {state!r}")
        job = self.get_job(delivery_id)
        if job is None:
            raise KeyError(delivery_id)
        job["state"] = state
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

    def jobs(self, state=None):
        out = []
        try:
            names = sorted(os.listdir(self.jobs_dir))
        except OSError:
            return out
        for name in names:
            # Skip the per-key idempotency markers and any underscore-
            # prefixed bookkeeping file -- only cj_ delivery records here.
            if (not name.endswith(".json") or name.startswith("_")
                    or name.startswith("idem_")):
                continue
            job = self.get_job(name[:-len(".json")])
            if job and (state is None or job.get("state") == state):
                out.append(job)
        out.sort(key=lambda j: (j.get("created", 0),
                                j.get("delivery_id", "")))
        return out

    # -- audit (A-40: host-side, never the Embody logger) ---------------

    def audit(self, actor, event, detail=None):
        line = json.dumps({"ts": self._now(), "actor": actor,
                           "event": event, "detail": detail or {}},
                          sort_keys=True)
        # Append-only: a partial append can damage at most the LAST line,
        # never an earlier record, and pruning is a truncate.
        with open(os.path.join(self.dir, AUDIT_FILE), "a",
                  encoding="utf-8", newline="\n") as f:
            f.write(line + "\n")

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
