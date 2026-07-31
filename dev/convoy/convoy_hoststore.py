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

import json
import os
import time

import convoy_identity as identity
import convoy_platform as platform_mod

STORE_VERSION = 1
HOST_FILE = "host.json"
JOBS_DIR = "jobs"
AUDIT_FILE = "audit.jsonl"

JOB_STATES = ("queued", "running", "succeeded", "failed", "indeterminate")


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

    # -- jobs (one file each, like .embody/jobs/) -----------------------

    def _job_path(self, job_id):
        return os.path.join(self.jobs_dir, f"{job_id}.json")

    def _index_path(self):
        return os.path.join(self.jobs_dir, "_by_key.json")

    def _load_index(self):
        try:
            with open(self._index_path(), "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    @staticmethod
    def _scope_key(convoy_id, node_id, idempotency_key):
        """Idempotency is scoped, never global.

        A single global key space meant a submission for node B returned
        node A's job -- B's work silently never created -- and let one
        convoy squat a guessable key across every other convoy. The
        separator is a NUL, which cannot appear in any of the three
        parts, so no combination of ids can forge another scope's key.
        """
        return "\x00".join((convoy_id, node_id, idempotency_key))

    def create_job(self, idempotency_key, node_id, operation, arguments,
                   convoy_id):
        """Persist-before-acknowledge. Returns (job, created).

        The job file is written and replaced atomically BEFORE this
        returns, so an acknowledged job can never be lost to a crash.
        """
        if not idempotency_key or not isinstance(idempotency_key, str):
            raise ValueError("idempotency_key is required")
        if not convoy_id or not isinstance(convoy_id, str):
            raise ValueError("convoy_id is required to scope idempotency")

        index = self._load_index()
        scope = self._scope_key(convoy_id, node_id, idempotency_key)
        existing_id = index.get(scope)
        if existing_id:
            existing = self.get_job(existing_id)
            if existing is not None:
                return existing, False
            # Index pointed at a job whose file is gone -- heal it rather
            # than refusing forever.
            index.pop(scope, None)

        now = self._now()
        job = {
            "job_id": "cj_" + identity.mint_id()[:12],
            "idempotency_key": idempotency_key,
            "node_id": node_id,
            "convoy_id": convoy_id,
            "operation": operation,
            "arguments": arguments or {},
            "state": "queued",
            "result": None,
            "created": now,
            "updated": now,
        }
        platform_mod._write_private(
            self._job_path(job["job_id"]),
            json.dumps(job, indent=1, sort_keys=True) + "\n")
        index[scope] = job["job_id"]
        platform_mod._write_private(
            self._index_path(), json.dumps(index, sort_keys=True) + "\n")
        self.audit("hoststore", "job_created",
                   {"job_id": job["job_id"], "operation": operation,
                    "node_id": node_id})
        return job, True

    def get_job(self, job_id):
        if not job_id or "/" in job_id or "\\" in job_id or ".." in job_id:
            return None         # never let an id escape the jobs dir
        try:
            with open(self._job_path(job_id), "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def set_job_state(self, job_id, state, result=None):
        if state not in JOB_STATES:
            raise ValueError(f"unknown job state {state!r}")
        job = self.get_job(job_id)
        if job is None:
            raise KeyError(job_id)
        job["state"] = state
        if result is not None:
            job["result"] = result
        job["updated"] = self._now()
        platform_mod._write_private(
            self._job_path(job_id),
            json.dumps(job, indent=1, sort_keys=True) + "\n")
        return job

    def jobs(self, state=None):
        out = []
        try:
            names = sorted(os.listdir(self.jobs_dir))
        except OSError:
            return out
        for name in names:
            if not name.endswith(".json") or name.startswith("_"):
                continue
            job = self.get_job(name[:-len(".json")])
            if job and (state is None or job.get("state") == state):
                out.append(job)
        out.sort(key=lambda j: (j.get("created", 0), j.get("job_id", "")))
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
