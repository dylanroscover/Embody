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
  - the audit trail (A-40).

What it deliberately does NOT do yet: LAN anything, discovery, peers,
relay, TD launch, artifacts. Those phases build ON this file's contracts
rather than amending them.
"""

import argparse
import hmac
import json
import os
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import convoy_hoststore as hoststore
import convoy_identity as identity
import convoy_platform as platform_mod

MAX_BODY_BYTES = 1 * 1024 * 1024
TOKEN_HEADER = "X-Convoy-Host-Token"


class HostApp:
    """All state behind one lock: a host app is coordination, not
    throughput. Every handler acquires it around the whole request."""

    def __init__(self, directory_path, now=None):
        self.data_dir = directory_path
        self.started = (now or time.time)()
        self.token = platform_mod.ensure_ipc_token(directory_path)
        self.db = hoststore.HostStore(directory_path, now=now)
        self.host_id = self.db.host_id()
        self.directory, self.quarantined = self.db.load_directory()
        self.lock = threading.Lock()
        self.db.audit("hostapp", "started", {"host_id": self.host_id})

    # -- request handlers (all called WITH self.lock held) -------------

    def status(self):
        return {
            "ok": True,
            "protocol": "convoy-host/1",
            "host_id": self.host_id,
            "nodes": len(self.directory.nodes()),
            "jobs_queued": len(self.db.jobs(state="queued")),
            "quarantined_nodes": len(self.quarantined),
            "uptime_s": round(time.time() - self.started, 1),
        }

    def register_node(self, body):
        project_root = body.get("project_root")
        convoy_id = body.get("convoy_id")
        comp_path = body.get("comp_path") or ""
        # Per-launch, supplied by the TD side and never stored. Absent is
        # fine (the host mints one); what matters is that it CHANGES on
        # every TD start so a stale request can be caught.
        runtime_id = body.get("runtime_id")
        if (not comp_path or not isinstance(comp_path, str)
                or len(comp_path) > 512):
            self.db.audit("hostapp", "register_refused",
                          {"reason": "malformed", "detail": "comp_path"})
            return 400, {"ok": False, "reason": "malformed",
                         "detail": "comp_path is required (1..512 chars) "
                                   "-- omitting it would mint a new identity"}
        try:
            record = self.directory.register(
                project_root, comp_path, convoy_id, runtime_id=runtime_id)
        except identity.IdentityError as e:
            # A-39: refusals are AUDITED, not silent -- with no admission
            # control yet, visibility is the compensating control.
            self.db.audit("hostapp", "register_refused",
                          {"reason": e.reason, "detail": e.detail})
            code = 400 if e.reason.startswith("malformed") else 409
            return code, {"ok": False, "reason": e.reason,
                          "detail": e.detail}
        # PERSIST FIRST, then keep the in-memory directory. The reverse
        # order left a node that existed in memory (and accepted jobs)
        # but vanished on restart if the write failed.
        try:
            self.db.save_node(record)
        except Exception as e:
            self.directory.forget(record["node_id"])
            self.db.audit("hostapp", "register_failed",
                          {"error": f"{type(e).__name__}: {e}"})
            return 500, {"ok": False, "reason": "persist_failed",
                         "detail": f"{type(e).__name__}: {e}"}
        self.db.audit("hostapp", "node_registered",
                      {"node_id": record["node_id"],
                       "comp_path": comp_path})
        return 200, {"ok": True,
                     "node_id": record["node_id"],
                     "runtime_id": record["runtime_id"],
                     "host_id": self.host_id,
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
        idempotency_key = body.get("idempotency_key")
        node_id = body.get("node_id") or ""
        operation = body.get("operation") or ""
        if not idempotency_key or not operation:
            return 400, {"ok": False, "reason": "malformed",
                         "detail": "idempotency_key and operation required"}
        node = self.directory.lookup(node_id)
        if node is None:
            return 404, {"ok": False, "reason": "unknown_node",
                         "detail": node_id}
        # convoy_id comes from the REGISTERED node, never from the
        # request: a caller must not be able to choose which namespace
        # its idempotency key lands in.
        job, created = self.db.create_job(
            idempotency_key, node_id, operation, body.get("arguments"),
            convoy_id=node["convoy_id"])
        return 200, {"ok": True, "created": created, "job": job}

    def get_job(self, job_id):
        job = self.db.get_job(job_id)
        if job is None:
            return 404, {"ok": False, "reason": "unknown_job",
                         "detail": job_id}
        return 200, {"ok": True, "job": job}


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            pass

        def _send(self, code, payload):
            body = json.dumps(payload).encode("utf-8")
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
                return json.loads(self.rfile.read(length).decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                return None

        def do_GET(self):
            if self.path == "/health":
                # The ONLY unauthenticated route: liveness, no state.
                self._send(200, {"ok": True, "protocol": "convoy-host/1"})
                return
            if not self._authenticated():
                self._send(401, {"ok": False, "reason": "unauthenticated"})
                return
            with app.lock:
                if self.path == "/status":
                    self._send(200, app.status())
                elif self.path == "/nodes":
                    self._send(200, app.list_nodes())
                elif self.path.startswith("/jobs/"):
                    code, payload = app.get_job(self.path[len("/jobs/"):])
                    self._send(code, payload)
                else:
                    self._send(404, {"ok": False, "reason": "not_found"})

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
            with app.lock:
                if self.path == "/register":
                    code, payload = app.register_node(body)
                elif self.path == "/remint":
                    code, payload = app.remint_node(body)
                elif self.path == "/jobs":
                    code, payload = app.create_job(body)
                else:
                    code, payload = 404, {"ok": False,
                                          "reason": "not_found"}
            self._send(code, payload)

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
    args = parser.parse_args(argv)

    directory = args.data_dir or platform_mod.data_dir()
    app = HostApp(directory)
    server, port = serve(app, args.port)
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
        platform_mod.clear_portfile(directory)
        app.db.audit("hostapp", "stopped", {})
        app.db.close()


if __name__ == "__main__":
    main()
