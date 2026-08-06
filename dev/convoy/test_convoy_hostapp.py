"""Host app end to end: real HTTP over loopback, real DB, real token.

Includes the PHASE 1 EXIT criterion:
  "two local fake nodes register; durable jobs survive host restart."
"""

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

import convoy_hostapp as ha
import convoy_platform as cp
from conftest import approve_td_python


class Server:
    """A running host app on a throwaway data dir."""

    def __init__(self, data_dir):
        self.app = ha.HostApp(
            data_dir,
            artifact_cache_path=os.path.join(data_dir, "test-artifacts"))
        self.server, self.port = ha.serve(self.app, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"

    def call(self, path, body=None, token=..., method=None):
        token = self.app.token if token is ... else token
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers[ha.TOKEN_HEADER] = token
        data = None if body is None else json.dumps(body).encode()
        # Retry ONLY a transient transport failure -- a loopback connection
        # aborted/reset under CI load (WinError 10053 flaked the matrix on
        # test_every_new_route_requires_the_token). A real HTTP response
        # (HTTPError, any 4xx/5xx) is caught FIRST and returned immediately,
        # never retried, so a genuine refusal can never be masked; and the
        # retry path is reached only when NO response was received.
        last = None
        for attempt in range(4):
            req = urllib.request.Request(self.url + path, data=data,
                                         headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    return r.status, json.loads(r.read().decode())
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read().decode())
            except (urllib.error.URLError, ConnectionError, OSError) as e:
                last = e
                time.sleep(0.1 * (attempt + 1))
        raise last

    def stop(self):
        # Defensive: a test that started the drain loop and failed before
        # its own stop must not leak a live daemon thread into the rest
        # of the session (it would wake later against a deleted tmp_path).
        self.app.stop_drain_loop()
        self.server.shutdown()
        self.server.server_close()
        self.app.db.close()


@pytest.fixture
def server(tmp_path):
    s = Server(str(tmp_path / "state"))
    yield s
    s.stop()


# -- authenticated IPC ------------------------------------------------

def test_health_is_the_only_unauthenticated_route(server):
    code, body = server.call("/health", token=None)
    assert code == 200 and body["ok"] is True


@pytest.mark.parametrize("path", ["/status", "/nodes", "/jobs/cj_x"])
def test_reads_require_the_token(server, path):
    code, body = server.call(path, token=None)
    assert code == 401 and body["reason"] == "unauthenticated"


@pytest.mark.parametrize("path,payload", [
    ("/register", {"project_root": "/Work/a", "convoy_id": "c", "comp_path": "/Embody"}),
    ("/jobs", {"idempotency_key": "k", "operation": "x"}),
    ("/remint", {"node_id": "n"}),
])
def test_writes_require_the_token(server, path, payload):
    code, body = server.call(path, payload, token=None)
    assert code == 401 and body["reason"] == "unauthenticated"


def test_wrong_token_refused(server):
    code, _ = server.call("/status", token="0" * 64)
    assert code == 401


def test_unauthenticated_post_never_reaches_the_parser(server):
    """Auth precedes body parsing: a bad token on malformed JSON must
    still read 401, not 400 -- no parser surface for a stranger."""
    req = urllib.request.Request(
        server.url + "/register", data=b"{not json",
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        urllib.request.urlopen(req, timeout=10)
        assert False, "should have refused"
    except urllib.error.HTTPError as e:
        assert e.code == 401


def test_token_is_persisted_and_stable(tmp_path):
    directory = str(tmp_path / "state")
    first = cp.ensure_ipc_token(directory)
    assert first == cp.ensure_ipc_token(directory)
    assert len(first) == 64


# -- registration (A-12 over the wire) ---------------------------------

def test_two_fake_nodes_register_and_get_distinct_ids(server):
    """PHASE 1 EXIT CLAUSE: two local fake nodes register."""
    code_a, a = server.call("/register", {
        "project_root": "/Work/anchor-alpha", "convoy_id": "studio",
        "comp_path": "/projA/Embody"})
    code_b, b = server.call("/register", {
        "project_root": "/Work/anchor-beta", "convoy_id": "studio",
        "comp_path": "/projB/Embody"})

    assert code_a == 200 and code_b == 200
    assert a["node_id"] != b["node_id"]
    assert a["host_id"] == b["host_id"], "one host, two nodes"
    assert a["td_python_approved"] is False

    code, listing = server.call("/nodes")
    assert code == 200 and len(listing["nodes"]) == 2


def test_re_registration_is_stable(server):
    _, first = server.call("/register", {"project_root": "/Work/x", "convoy_id": "c",
                                         "comp_path": "/Embody"})
    _, second = server.call("/register", {"project_root": "/Work/x", "convoy_id": "c",
                                          "comp_path": "/Embody"})
    assert first["node_id"] == second["node_id"]

    # A DIFFERENT comp_path is a different node (section 11).
    _, other = server.call("/register", {"project_root": "/Work/x", "convoy_id": "c",
                                         "comp_path": "/scenes/Embody"})
    assert other["node_id"] != first["node_id"]


def test_anchor_switching_convoy_is_refused(server):
    server.call("/register", {"project_root": "/Work/x", "convoy_id": "c1", "comp_path": "/Embody"})
    code, body = server.call("/register", {"project_root": "/Work/x", "convoy_id": "c2", "comp_path": "/Embody"})
    assert code == 409 and body["reason"] == "node_identity_conflict"


def test_remint_over_the_wire_resets_approval(server):
    _, node = server.call("/register", {"project_root": "/Work/x", "convoy_id": "c", "comp_path": "/Embody"})
    approve_td_python(server.app, node["node_id"])

    code, fresh = server.call("/remint", {"node_id": node["node_id"]})
    assert code == 200
    assert fresh["node_id"] != node["node_id"]
    assert fresh["td_python_approved"] is False


def test_remint_of_unknown_node_is_404(server):
    code, body = server.call("/remint", {"node_id": "nope"})
    assert code == 404 and body["reason"] == "unknown_node"


# -- jobs ---------------------------------------------------------------

def test_job_for_unknown_node_refused(server):
    code, body = server.call("/jobs", {"idempotency_key": "k",
                                       "node_id": "ghost",
                                       "operation": "query_network"})
    assert code == 404 and body["reason"] == "unknown_node"


def test_job_create_is_idempotent_over_the_wire(server):
    _, node = server.call("/register", {"project_root": "/Work/x", "convoy_id": "c", "comp_path": "/Embody"})
    payload = {"idempotency_key": "retry-key", "node_id": node["node_id"],
               "operation": "query_network", "arguments": {"parent_path": "/"}}
    _, first = server.call("/jobs", payload)
    _, second = server.call("/jobs", payload)
    assert first["created"] is True and second["created"] is False
    assert first["job"]["delivery_id"] == second["job"]["delivery_id"]


def test_malformed_job_refused(server):
    code, body = server.call("/jobs", {"node_id": "x"})
    assert code == 400 and body["reason"] == "malformed"


def test_unknown_routes_are_404(server):
    assert server.call("/nope")[0] == 404
    assert server.call("/nope", {"a": 1})[0] == 404


# -- restart durability (the headline exit criterion) -------------------

def test_nodes_and_jobs_survive_a_full_host_restart(tmp_path):
    """PHASE 1 EXIT: durable jobs survive host restart -- proven by
    stopping the process object entirely and starting a NEW one on the
    same data dir, as a supervisor restart would."""
    directory = str(tmp_path / "state")
    first = Server(directory)
    _, node = first.call("/register", {"project_root": "/Work/anchor-1",
                                       "convoy_id": "studio",
                                       "comp_path": "/p/Embody"})
    _, created = first.call("/jobs", {"idempotency_key": "survive-me",
                                      "node_id": node["node_id"],
                                      "operation": "capture_top"})
    delivery_id = created["job"]["delivery_id"]
    host_id = first.app.host_id
    first.stop()

    second = Server(directory)
    try:
        assert second.app.host_id == host_id, "host identity is stable"

        code, body = second.call(f"/jobs/{delivery_id}")
        assert code == 200, "the acknowledged job must still exist"
        assert body["job"]["idempotency_key"] == "survive-me"

        _, listing = second.call("/nodes")
        assert [n["node_id"] for n in listing["nodes"]] == [node["node_id"]]

        # Re-registering after the restart returns the SAME node_id.
        _, again = second.call("/register", {"project_root": "/Work/anchor-1",
                                             "convoy_id": "studio",
                                             "comp_path": "/p/Embody"})
        assert again["node_id"] == node["node_id"]

        # And the idempotency key still suppresses a duplicate.
        _, retry = second.call("/jobs", {"idempotency_key": "survive-me",
                                         "node_id": node["node_id"],
                                         "operation": "capture_top"})
        assert retry["created"] is False
        assert retry["job"]["delivery_id"] == delivery_id
    finally:
        second.stop()


def test_a_fresh_token_is_not_minted_on_restart(tmp_path):
    directory = str(tmp_path / "state")
    first = Server(directory)
    token = first.app.token
    first.stop()
    second = Server(directory)
    try:
        assert second.app.token == token, (
            "clients hold this token across host restarts")
    finally:
        second.stop()


# -- portfile ------------------------------------------------------------

def test_portfile_points_at_the_running_host(server):
    data = cp.read_portfile(server.app.data_dir)
    assert data["port"] == server.port
    assert data["host_id"] == server.app.host_id
    assert data["pid"] == os.getpid()


def test_corrupt_portfile_reads_as_absent(tmp_path):
    directory = str(tmp_path / "state")
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, cp.PORT_FILE), "w") as f:
        f.write("{ half-written")
    assert cp.read_portfile(directory) is None


def test_binds_loopback_only(server):
    """Phase 1 is local-only: nothing here may listen off-box (D-6)."""
    assert server.server.server_address[0] == "127.0.0.1"


# =====================================================================
# Panel regressions (2026-07-31)
# =====================================================================

def test_non_ascii_token_header_is_a_clean_401_not_a_crash(server):
    """PROVEN BUG: hmac.compare_digest raises TypeError on non-ASCII str,
    and http.client decodes headers as iso-8859-1 -- so one 0xFF byte
    from an UNAUTHENTICATED caller blew up the auth check itself: no
    response at all, traceback on stderr, dead handler thread."""
    import http.client
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
    conn.putrequest("GET", "/status")
    conn.putheader(ha.TOKEN_HEADER, "\u00ff\u00fe")
    conn.endheaders()
    response = conn.getresponse()
    body = json.loads(response.read().decode())
    conn.close()
    assert response.status == 401
    assert body["reason"] == "unauthenticated"


def test_jobs_for_two_nodes_do_not_collide_over_the_wire(server):
    _, a = server.call("/register", {"project_root": "/Work/a", "convoy_id": "cv",
                                     "comp_path": "/A"})
    _, b = server.call("/register", {"project_root": "/Work/b", "convoy_id": "cv",
                                     "comp_path": "/B"})
    _, ja = server.call("/jobs", {"idempotency_key": "deploy",
                                  "node_id": a["node_id"],
                                  "operation": "capture_top"})
    _, jb = server.call("/jobs", {"idempotency_key": "deploy",
                                  "node_id": b["node_id"],
                                  "operation": "query_network"})
    assert ja["created"] is True and jb["created"] is True
    assert ja["job"]["delivery_id"] != jb["job"]["delivery_id"]
    assert jb["job"]["node_id"] == b["node_id"]


def test_a_failed_persist_does_not_leave_a_phantom_node(server, monkeypatch):
    """PROVEN BUG: the in-memory directory was mutated BEFORE the write,
    so a failed save left a node that accepted jobs but vanished on
    restart."""
    def boom(record):
        raise OSError("disk full (test)")
    monkeypatch.setattr(server.app.db, "save_node", boom)

    code, body = server.call("/register", {"project_root": "/Work/ghost", "convoy_id": "cv", "comp_path": "/Embody"})
    assert code == 500 and body["reason"] == "persist_failed"

    _, listing = server.call("/nodes")
    assert listing["nodes"] == [], "no phantom node may remain in memory"


def test_oversized_comp_path_refused(server):
    code, body = server.call("/register", {"project_root": "/Work/a", "convoy_id": "cv",
                                           "comp_path": "/" + "x" * 600})
    assert code == 400 and body["reason"] == "malformed"


# =====================================================================
# POST /unregister -- the clean-exit counterpart to /register
# =====================================================================

def _register(server, port=None, root="/Work/x", comp="/Embody",
              runtime_id=None):
    body = {"project_root": root, "convoy_id": "cv", "comp_path": comp}
    if port is not None:
        body["envoy_port"] = port
    if runtime_id is not None:
        body["runtime_id"] = runtime_id
    return server.call("/register", body)


def test_unregister_clears_the_envoy_port(server):
    """The round trip: a node registers a live port, unregisters, and the
    directory no longer hands that port to the dispatcher."""
    _, node = _register(server, port=9981)
    _, listing = server.call("/nodes")
    assert listing["nodes"][0]["envoy_port"] == 9981

    code, body = server.call("/unregister", {"node_id": node["node_id"]})
    assert code == 200 and body["ok"] is True
    assert body["cleared"] is True
    assert body["envoy_port"] is None
    assert body["node_id"] == node["node_id"]

    _, listing = server.call("/nodes")
    assert listing["nodes"][0]["envoy_port"] is None


def test_unregister_keeps_the_node_record(server):
    """Only the per-launch port goes. node_id is the durable address an
    approval attaches to -- deleting it would silently revoke consent."""
    _, node = _register(server, port=9981)
    approve_td_python(server.app, node["node_id"])

    server.call("/unregister", {"node_id": node["node_id"]})

    _, listing = server.call("/nodes")
    assert [n["node_id"] for n in listing["nodes"]] == [node["node_id"]]
    assert listing["nodes"][0]["td_python_approved"] is True


def test_unregister_is_idempotent(server):
    """onExit() is best-effort and may double-fire; a second clear is a
    plain 200, never a 404 or a state change."""
    _, node = _register(server, port=9981)
    first = server.call("/unregister", {"node_id": node["node_id"]})
    second = server.call("/unregister", {"node_id": node["node_id"]})
    assert first[0] == 200 and second[0] == 200
    assert second[1]["envoy_port"] is None


def test_unregister_of_unknown_node_is_a_named_404(server):
    code, body = server.call("/unregister", {"node_id": "ghost"})
    assert code == 404 and body["reason"] == "unknown_node"


# -- Forget Stale Node (advanced local recovery, plan 7.5) --------------

# -- supersession: a re-identified node retires its own old record ------

def test_registering_retires_the_superseded_record_for_the_same_project(server):
    """Node identity includes the project file, so a Save As / rename mints a
    new node and the old one used to linger offline for ever -- the duplicate
    rows users see. Same host + project root + COMP path IS the same logical
    node, so registering the new one retires the idle old one."""
    # A Save As changes the node discriminator -- that is what mints a new
    # id for the same project+COMP, and what leaves the duplicate behind.
    _, old = server.call("/register", {
        "project_root": "/Work/show", "convoy_id": "cv", "comp_path": "/Embody",
        "envoy_port": 9981, "node_discriminator": "nd_" + "1" * 32})
    server.call("/unregister", {"node_id": old["node_id"]})   # goes idle
    _, new = server.call("/register", {
        "project_root": "/Work/show", "convoy_id": "cv", "comp_path": "/Embody",
        "envoy_port": 9982, "runtime_id": "rt_new",
        "node_discriminator": "nd_" + "2" * 32})
    assert new["node_id"] != old["node_id"]
    _, listing = server.call("/nodes")
    ids = [n["node_id"] for n in listing["nodes"]]
    assert new["node_id"] in ids
    assert old["node_id"] not in ids, "the superseded record must be retired"


def test_supersession_never_touches_a_different_project_on_the_same_host(server):
    """The tempting rule -- same IP and offline -- would delete a real node.
    Two projects on one machine share an IP; only the SAME project root and
    COMP path mean the same node."""
    _, other = server.call("/register", {
        "project_root": "/Work/other", "convoy_id": "cv", "comp_path": "/Embody",
        "envoy_port": 9981, "node_discriminator": "nd_" + "1" * 32})
    server.call("/unregister", {"node_id": other["node_id"]})
    server.call("/register", {
        "project_root": "/Work/show", "convoy_id": "cv", "comp_path": "/Embody",
        "envoy_port": 9982, "node_discriminator": "nd_" + "2" * 32})
    _, listing = server.call("/nodes")
    assert other["node_id"] in [n["node_id"] for n in listing["nodes"]],         "a different project must survive -- it is still remotely launchable"


def test_a_live_sessions_versioned_save_retires_its_own_old_row(server):
    """TouchDesigner's versioned save renames the .toe WITHOUT the process
    exiting: the same TD re-registers under a new identity and the old row
    keeps an envoy_port that now belongs to its successor -- so it read as
    'live' and ghosted forever (field report 2026-08-05, duplicate #3).
    Same port or same recorded process = the same node wearing its
    previous name; it must be retired on the successor's register."""
    _, old = server.call("/register", {
        "project_root": "/Work/show", "convoy_id": "cv", "comp_path": "/Embody",
        "envoy_port": 9981, "runtime_id": "rt_live",
        "metadata": {"process_id": 4242},
        "node_discriminator": "nd_" + "1" * 32})
    # No unregister: the process never exited, it just saved.
    _, new = server.call("/register", {
        "project_root": "/Work/show", "convoy_id": "cv", "comp_path": "/Embody",
        "envoy_port": 9981, "runtime_id": "rt_live",
        "metadata": {"process_id": 4242},
        "node_discriminator": "nd_" + "2" * 32})
    assert new["node_id"] != old["node_id"]
    _, listing = server.call("/nodes")
    ids = [n["node_id"] for n in listing["nodes"]]
    assert new["node_id"] in ids
    assert old["node_id"] not in ids, \
        "the pre-save row is the same process and must not ghost"


def test_versioned_save_retirement_matches_on_port_alone(server):
    """Successor and predecessor share only the PORT (the old row never
    recorded a pid): still the same server, still retired."""
    _, old = server.call("/register", {
        "project_root": "/Work/show", "convoy_id": "cv", "comp_path": "/Embody",
        "envoy_port": 9981, "runtime_id": "rt_live",
        "node_discriminator": "nd_" + "1" * 32})
    server.call("/register", {
        "project_root": "/Work/show", "convoy_id": "cv", "comp_path": "/Embody",
        "envoy_port": 9981, "runtime_id": "rt_live",
        "node_discriminator": "nd_" + "2" * 32})
    _, listing = server.call("/nodes")
    assert old["node_id"] not in [n["node_id"] for n in listing["nodes"]]


def test_descriptive_pid_alone_never_retires_a_port_bearing_row(server):
    """metadata process_id is client-supplied display data -- never
    authority for a retirement. A portless successor register spares the
    port-bearing old row; convergence comes one heartbeat later, when
    the successor's register carries the port and it matches."""
    _, old = server.call("/register", {
        "project_root": "/Work/show", "convoy_id": "cv", "comp_path": "/Embody",
        "envoy_port": 9981, "runtime_id": "rt_live",
        "metadata": {"process_id": 4242},
        "node_discriminator": "nd_" + "1" * 32})
    server.call("/register", {
        "project_root": "/Work/show", "convoy_id": "cv", "comp_path": "/Embody",
        "runtime_id": "rt_live", "metadata": {"process_id": 4242},
        "node_discriminator": "nd_" + "2" * 32})
    _, listing = server.call("/nodes")
    assert old["node_id"] in [n["node_id"] for n in listing["nodes"]], \
        "a pid match alone must not retire a row that still holds a port"
    # The next heartbeat carries the port: NOW it is provably the same
    # server, and the past self retires.
    server.call("/register", {
        "project_root": "/Work/show", "convoy_id": "cv", "comp_path": "/Embody",
        "envoy_port": 9981, "runtime_id": "rt_live",
        "metadata": {"process_id": 4242},
        "node_discriminator": "nd_" + "2" * 32})
    _, listing = server.call("/nodes")
    assert old["node_id"] not in [n["node_id"] for n in listing["nodes"]]


def test_supersession_spares_a_different_live_server_on_the_same_project(
        server):
    """Two genuinely separate TD processes can hold the same project root
    (one still running an older version file). A port-bearing old row
    whose port AND process differ from the new registration can still
    answer -- it must never be retired."""
    _, old = server.call("/register", {
        "project_root": "/Work/show", "convoy_id": "cv", "comp_path": "/Embody",
        "envoy_port": 9981, "runtime_id": "rt_old",
        "metadata": {"process_id": 1111},
        "node_discriminator": "nd_" + "1" * 32})
    _, new = server.call("/register", {
        "project_root": "/Work/show", "convoy_id": "cv", "comp_path": "/Embody",
        "envoy_port": 9982, "runtime_id": "rt_new",
        "metadata": {"process_id": 2222},
        "node_discriminator": "nd_" + "2" * 32})
    _, listing = server.call("/nodes")
    ids = [n["node_id"] for n in listing["nodes"]]
    assert old["node_id"] in ids and new["node_id"] in ids, \
        "a different live server is not a ghost"


def test_supersession_refuses_a_queued_delivery_and_retires_the_row(server):
    """A queued delivery provably never left this host, so a superseded
    row's queue is refused (not invented away) and the row retires.

    This test used to assert the opposite -- that ANY unresolved work
    spared the row -- which is what pinned the field's eight
    versioned-save duplicates for ever (2026-08-06). The guarantee it was
    written to protect is real but narrower, and now lives in the two
    tests below: work that crossed the dispatch boundary still spares,
    and an uncollected RESULT never needed its row at all.
    """
    _, old = server.call("/register", {
        "project_root": "/Work/show", "convoy_id": "cv", "comp_path": "/Embody",
        "envoy_port": 9981, "node_discriminator": "nd_" + "1" * 32})
    code, created = server.call("/jobs", {
        "idempotency_key": "keep", "node_id": old["node_id"],
        "operation": "query_network", "arguments": {}})
    assert code == 200
    delivery_id = created["job"]["delivery_id"]
    server.call("/unregister", {"node_id": old["node_id"]})
    server.call("/register", {
        "project_root": "/Work/show", "convoy_id": "cv", "comp_path": "/Embody",
        "envoy_port": 9982, "node_discriminator": "nd_" + "2" * 32})

    _, listing = server.call("/nodes")
    assert old["node_id"] not in [n["node_id"] for n in listing["nodes"]], \
        "a queued-only pin must not keep a superseded row alive"
    # The delivery is durably terminal with the host's own evidence -- it
    # is not silently dropped, and it is now reapable and ackable.
    job = server.app.db.get_job(delivery_id)
    assert job["state"] == "refused"
    assert job["result"]["reason"] == "node_superseded"


def test_supersession_spares_work_past_the_dispatch_boundary(server):
    """The real guarantee: once a delivery has been claimed for dispatch
    its verdict belongs to the node, so the host must never terminalise
    it to tidy a row away."""
    _, old = server.call("/register", {
        "project_root": "/Work/show", "convoy_id": "cv", "comp_path": "/Embody",
        "envoy_port": 9981, "node_discriminator": "nd_" + "1" * 32})
    _, created = server.call("/jobs", {
        "idempotency_key": "keep", "node_id": old["node_id"],
        "operation": "query_network", "arguments": {}})
    claimed = server.app.db.claim_for_dispatch(created["job"]["delivery_id"])
    assert claimed and claimed["state"] == "dispatching"
    server.call("/unregister", {"node_id": old["node_id"]})
    server.call("/register", {
        "project_root": "/Work/show", "convoy_id": "cv", "comp_path": "/Embody",
        "envoy_port": 9982, "node_discriminator": "nd_" + "2" * 32})
    _, listing = server.call("/nodes")
    assert old["node_id"] in [n["node_id"] for n in listing["nodes"]], \
        "a delivery past the dispatch boundary must spare its row"


def test_supersession_is_not_blocked_by_an_uncollected_result(server):
    """A finished-but-unacknowledged outcome never needed its row: it is
    fetched by delivery_id, and forgetting the row deletes no record.

    115 of the 123 job records pinning the field's duplicate rows were
    exactly this class.
    """
    _, old = server.call("/register", {
        "project_root": "/Work/show", "convoy_id": "cv", "comp_path": "/Embody",
        "envoy_port": 9981, "node_discriminator": "nd_" + "1" * 32})
    _, created = server.call("/jobs", {
        "idempotency_key": "keep", "node_id": old["node_id"],
        "operation": "query_network", "arguments": {}})
    delivery_id = created["job"]["delivery_id"]
    # queued -> refused: TERMINAL, and nobody has acknowledged it.
    server.call("/jobs/cancel", {"delivery_id": delivery_id})
    server.call("/unregister", {"node_id": old["node_id"]})
    server.call("/register", {
        "project_root": "/Work/show", "convoy_id": "cv", "comp_path": "/Embody",
        "envoy_port": 9982, "node_discriminator": "nd_" + "2" * 32})

    _, listing = server.call("/nodes")
    assert old["node_id"] not in [n["node_id"] for n in listing["nodes"]], \
        "an uncollected result must not pin a superseded row"
    # ...and the result is still there, still fetchable by delivery_id.
    code, body = server.call("/jobs/%s" % delivery_id, method="GET")
    assert code == 200 and body["job"]["delivery_id"] == delivery_id, \
        "the result must outlive the row it was addressed to"


# -- cross-project supersession: Save As mints a new project root -------
#
# Field report 2026-08-05 (the FOURTH duplicate report): a Save As
# writes the .toe into a NEW folder, so the same live TD re-registers
# under a new project root. The old row keeps a port nobody can clear
# and is immune to EVERY cleanup path -- the root-scoped supersede
# misses it, the eviction sweep skips port-bearing rows, the stale-
# runtime reconcile sees a live process, and Forget Offline Nodes
# refuses port-bearing rows. Only daemon-owned live state (runtime_id,
# the port) may claim a cross-project row.

def test_save_as_into_a_new_folder_retires_the_same_processes_old_row(server):
    """runtime_id is minted once per TD launch and survives every save:
    a cross-root re-register with the same runtime and COMP is the same
    process wearing a new project identity, and its old row must go."""
    _, old = server.call("/register", {
        "project_root": "/Work/scratch", "convoy_id": "cv",
        "comp_path": "/Embody", "envoy_port": 9981,
        "runtime_id": "rt_live", "node_discriminator": "nd_" + "1" * 32})
    # No unregister: the process never exited, it just saved elsewhere.
    _, new = server.call("/register", {
        "project_root": "/Work/show", "convoy_id": "cv",
        "comp_path": "/Embody", "envoy_port": 9981,
        "runtime_id": "rt_live", "node_discriminator": "nd_" + "2" * 32})
    assert new["node_id"] != old["node_id"]
    _, listing = server.call("/nodes")
    ids = [n["node_id"] for n in listing["nodes"]]
    assert new["node_id"] in ids
    assert old["node_id"] not in ids, \
        "the pre-Save-As row is this process's own past self"


def test_save_as_retirement_works_even_before_the_port_arrives(server):
    """The successor's FIRST register may carry no port yet; the runtime
    match alone is authority (it is daemon-owned live state)."""
    _, old = server.call("/register", {
        "project_root": "/Work/scratch", "convoy_id": "cv",
        "comp_path": "/Embody", "envoy_port": 9981,
        "runtime_id": "rt_live", "node_discriminator": "nd_" + "1" * 32})
    server.call("/register", {
        "project_root": "/Work/show", "convoy_id": "cv",
        "comp_path": "/Embody",
        "runtime_id": "rt_live", "node_discriminator": "nd_" + "2" * 32})
    _, listing = server.call("/nodes")
    assert old["node_id"] not in [n["node_id"] for n in listing["nodes"]]


def test_two_convoy_comps_in_one_project_survive_each_others_registers(server):
    """Two Convoy COMPs in ONE .toe share a runtime and a port: each
    register must spare the sibling (same root, same discriminator,
    different COMP path) -- both rows are real."""
    _, a = server.call("/register", {
        "project_root": "/Work/show", "convoy_id": "cv",
        "comp_path": "/Embody", "envoy_port": 9981,
        "runtime_id": "rt_live", "node_discriminator": "nd_" + "1" * 32})
    _, b = server.call("/register", {
        "project_root": "/Work/show", "convoy_id": "cv",
        "comp_path": "/Second", "envoy_port": 9981,
        "runtime_id": "rt_live", "node_discriminator": "nd_" + "1" * 32})
    # A heartbeat re-register of the first must not evict the second.
    server.call("/register", {
        "project_root": "/Work/show", "convoy_id": "cv",
        "comp_path": "/Embody", "envoy_port": 9981,
        "runtime_id": "rt_live", "node_discriminator": "nd_" + "1" * 32})
    _, listing = server.call("/nodes")
    ids = [n["node_id"] for n in listing["nodes"]]
    assert a["node_id"] in ids and b["node_id"] in ids, \
        "sibling COMP registrations of one live project are both real"


def test_sibling_save_as_converges_in_either_order(server):
    """The panel-caught order dependence: two sibling COMPs sharing one
    runtime and port Save-As together. The first sibling's re-register
    must NOT strip the other old row's port/runtime (clear_envoy_port
    wipes runtime_id too, which would orphan it as a permanent ghost) --
    each old row is retired by its OWN successor's register."""
    _, a_old = server.call("/register", {
        "project_root": "/Work/scratch", "convoy_id": "cv",
        "comp_path": "/Embody", "envoy_port": 9981,
        "runtime_id": "rt_live", "node_discriminator": "nd_" + "1" * 32})
    _, b_old = server.call("/register", {
        "project_root": "/Work/scratch", "convoy_id": "cv",
        "comp_path": "/Second", "envoy_port": 9981,
        "runtime_id": "rt_live", "node_discriminator": "nd_" + "1" * 32})
    # Save As: sibling A re-registers under the new root FIRST.
    _, a_new = server.call("/register", {
        "project_root": "/Work/show", "convoy_id": "cv",
        "comp_path": "/Embody", "envoy_port": 9981,
        "runtime_id": "rt_live", "node_discriminator": "nd_" + "2" * 32})
    _, listing = server.call("/nodes")
    rows = {n["node_id"]: n for n in listing["nodes"]}
    assert a_old["node_id"] not in rows, "A's past self retires at once"
    assert b_old["node_id"] in rows, \
        "B's old row is A's SIBLING mid-transition, not A's ghost"
    assert rows[b_old["node_id"]]["envoy_port"] == 9981, \
        "and its port/runtime evidence must survive for B's own register"
    # Sibling B follows.
    _, b_new = server.call("/register", {
        "project_root": "/Work/show", "convoy_id": "cv",
        "comp_path": "/Second", "envoy_port": 9981,
        "runtime_id": "rt_live", "node_discriminator": "nd_" + "2" * 32})
    _, listing = server.call("/nodes")
    ids = [n["node_id"] for n in listing["nodes"]]
    assert b_old["node_id"] not in ids, "B's past self retires on B's turn"
    assert a_new["node_id"] in ids and b_new["node_id"] in ids
    assert len([i for i in ids if i in (a_new["node_id"],
                                        b_new["node_id"])]) == 2


def test_a_reclaimed_port_is_cleared_but_the_row_survives(server):
    """A crashed TD leaves its row holding a port the OS later hands to a
    DIFFERENT project's TD. The row is a real node (offline, launchable)
    and must survive -- but a loopback port is exclusive per host, so its
    stale claim on the port is cleared, unblocking the eviction sweep and
    Forget Offline Nodes."""
    _, old = server.call("/register", {
        "project_root": "/Work/crashed", "convoy_id": "cv",
        "comp_path": "/Embody", "envoy_port": 9981,
        "runtime_id": "rt_old", "node_discriminator": "nd_" + "1" * 32})
    # No unregister: a hard kill never says goodbye.
    server.call("/register", {
        "project_root": "/Work/other", "convoy_id": "cv",
        "comp_path": "/Embody", "envoy_port": 9981,
        "runtime_id": "rt_new", "node_discriminator": "nd_" + "2" * 32})
    _, listing = server.call("/nodes")
    rows = {n["node_id"]: n for n in listing["nodes"]}
    assert old["node_id"] in rows, \
        "a different project's row is a real node, not a ghost"
    assert rows[old["node_id"]]["envoy_port"] is None, \
        "the reclaimed port's stale claim must be cleared"


def test_cross_project_rows_are_never_claimed_by_metadata_alone(server):
    """A matching client-supplied process_id with a DIFFERENT runtime and
    no port overlap proves nothing: the cross-project row keeps its port
    and its place."""
    _, old = server.call("/register", {
        "project_root": "/Work/one", "convoy_id": "cv",
        "comp_path": "/Embody", "envoy_port": 9981,
        "runtime_id": "rt_one", "metadata": {"process_id": 4242},
        "node_discriminator": "nd_" + "1" * 32})
    server.call("/register", {
        "project_root": "/Work/two", "convoy_id": "cv",
        "comp_path": "/Embody", "envoy_port": 9982,
        "runtime_id": "rt_two", "metadata": {"process_id": 4242},
        "node_discriminator": "nd_" + "2" * 32})
    _, listing = server.call("/nodes")
    rows = {n["node_id"]: n for n in listing["nodes"]}
    assert old["node_id"] in rows
    assert rows[old["node_id"]]["envoy_port"] == 9981


# -- THE FIELD SHAPE (2026-08-06) --------------------------------------
#
# A user's node list held ELEVEN rows across two projects: eight
# versioned saves of one .toe and three of another, every one of them
# pinned by job records the old guard read as "unresolved work". 115 of
# the 123 pins were finished-but-unacknowledged results; the other 8
# were queued deliveries. Nothing was running. "Forget Offline Nodes"
# answered "forgot 0, kept 8 with unresolved jobs" and the rows came
# straight back. Every prior supersede test used exactly two rows and
# one job, which is why the shape survived a large suite.

def _versioned_save_session(server, disc, runtime, port, root, pin):
    """One TD session on `root`: register, take a job, close the project.

    pin='queued'   -> leaves an unfinished delivery
    pin='unacked'  -> leaves a finished, uncollected result
    """
    _, node = server.call("/register", {
        "project_root": root, "convoy_id": "cv", "comp_path": "/project1/Embody",
        "envoy_port": port, "runtime_id": runtime,
        "node_discriminator": "nd_" + disc * 32})
    _, created = server.call("/jobs", {
        "idempotency_key": "job_" + disc, "node_id": node["node_id"],
        "operation": "query_network", "arguments": {}})
    if pin == "unacked":
        server.call("/jobs/cancel",
                    {"delivery_id": created["job"]["delivery_id"]})
    server.call("/unregister", {"node_id": node["node_id"]})
    return node["node_id"]


def test_eight_versioned_saves_of_one_project_collapse_to_one_row(server):
    """The reported shape, end to end: eight siblings, mixed pin classes,
    all gone the moment the ninth registers."""
    stale = [
        _versioned_save_session(
            server, str(i), "rt_%d" % i, 9980 + i, "/Work/100g",
            "queued" if i % 3 == 0 else "unacked")
        for i in range(1, 9)]

    _, live = server.call("/register", {
        "project_root": "/Work/100g", "convoy_id": "cv",
        "comp_path": "/project1/Embody", "envoy_port": 9999,
        "runtime_id": "rt_live", "node_discriminator": "nd_" + "9" * 32})

    _, listing = server.call("/nodes")
    ids = [n["node_id"] for n in listing["nodes"]]
    assert ids == [live["node_id"]], (
        "%d sibling row(s) survived: %s"
        % (len([n for n in stale if n in ids]), [n[:8] for n in stale
                                                 if n in ids]))


def test_two_projects_collapse_independently_of_each_other(server):
    """The field machine held two projects. Collapsing one must never
    touch the other -- same host, same COMP path, different root.

    Note each project self-collapses AS IT GOES: session 2's register
    retires session 1's row, so the steady state is one row per project
    even before the final registration.
    """
    a = [_versioned_save_session(server, str(i), "rt_a%d" % i, 9980 + i,
                                 "/Work/100g", "unacked") for i in (1, 2)]
    b = [_versioned_save_session(server, str(i + 4), "rt_b%d" % i, 9990 + i,
                                 "/Work/st2110", "unacked") for i in (1, 2)]

    _, listing = server.call("/nodes")
    assert sorted(n["node_id"] for n in listing["nodes"]) == sorted(
        [a[-1], b[-1]]), "one row per project while they accumulate"

    _, live_a = server.call("/register", {
        "project_root": "/Work/100g", "convoy_id": "cv",
        "comp_path": "/project1/Embody", "envoy_port": 9870,
        "runtime_id": "rt_live_a", "node_discriminator": "nd_" + "8" * 32})

    _, listing = server.call("/nodes")
    ids = [n["node_id"] for n in listing["nodes"]]
    assert live_a["node_id"] in ids
    assert not [n for n in a if n in ids], "the registered project collapsed"
    assert b[-1] in ids, "the OTHER project's row must be untouched"


def test_collapsed_rows_stay_gone_across_a_daemon_restart(tmp_path):
    """'Disappear AND stay gone.' A row forgotten in memory but still on
    disk resurrects on the next daemon start -- the 2026-08-05 defect
    class -- so the durable delete is what this proves."""
    directory = str(tmp_path / "state")
    first = Server(directory)
    try:
        stale = [_versioned_save_session(
            first, str(i), "rt_%d" % i, 9980 + i, "/Work/100g", "unacked")
            for i in range(1, 5)]
        _, live = first.call("/register", {
            "project_root": "/Work/100g", "convoy_id": "cv",
            "comp_path": "/project1/Embody", "envoy_port": 9999,
            "runtime_id": "rt_live", "node_discriminator": "nd_" + "9" * 32})
        _, listing = first.call("/nodes")
        assert [n["node_id"] for n in listing["nodes"]] == [live["node_id"]]
    finally:
        first.stop()

    second = Server(directory)
    try:
        _, listing = second.call("/nodes")
        ids = [n["node_id"] for n in listing["nodes"]]
        assert ids == [live["node_id"]], (
            "retired rows came back from host.json after a restart: %s"
            % [n[:8] for n in stale if n in ids])
    finally:
        second.stop()


def test_save_as_ghost_past_the_dispatch_boundary_is_spared(server):
    """A same-process past self still keeps its row while a delivery it
    already handed to the node is unfinished -- that verdict is the
    node's to give.

    (Its queued-delivery sibling case is the test below: queued work is
    refused with evidence and the row goes, because a queued delivery
    provably never left this host.)
    """
    _, old = server.call("/register", {
        "project_root": "/Work/scratch", "convoy_id": "cv",
        "comp_path": "/Embody", "envoy_port": 9981,
        "runtime_id": "rt_live", "node_discriminator": "nd_" + "1" * 32})
    _, created = server.call("/jobs", {
        "idempotency_key": "keep", "node_id": old["node_id"],
        "operation": "query_network", "arguments": {}})
    assert server.app.db.claim_for_dispatch(created["job"]["delivery_id"])
    server.call("/register", {
        "project_root": "/Work/show", "convoy_id": "cv",
        "comp_path": "/Embody", "envoy_port": 9981,
        "runtime_id": "rt_live", "node_discriminator": "nd_" + "2" * 32})
    _, listing = server.call("/nodes")
    assert old["node_id"] in [n["node_id"] for n in listing["nodes"]], \
        "work past the dispatch boundary spares even a same-process past self"


def test_save_as_ghost_with_only_queued_work_is_retired(server):
    """The same past self, pinned only by a queued delivery, retires --
    and the delivery is refused with the host's evidence, never dropped."""
    _, old = server.call("/register", {
        "project_root": "/Work/scratch", "convoy_id": "cv",
        "comp_path": "/Embody", "envoy_port": 9981,
        "runtime_id": "rt_live", "node_discriminator": "nd_" + "1" * 32})
    _, created = server.call("/jobs", {
        "idempotency_key": "keep", "node_id": old["node_id"],
        "operation": "query_network", "arguments": {}})
    server.call("/register", {
        "project_root": "/Work/show", "convoy_id": "cv",
        "comp_path": "/Embody", "envoy_port": 9981,
        "runtime_id": "rt_live", "node_discriminator": "nd_" + "2" * 32})
    _, listing = server.call("/nodes")
    assert old["node_id"] not in [n["node_id"] for n in listing["nodes"]]
    job = server.app.db.get_job(created["job"]["delivery_id"])
    assert job["state"] == "refused"
    assert job["result"]["reason"] == "node_superseded"


# -- the daemon knows what code it is running ---------------------------
#
# installed.json can lie (a supervisor rewrite whose restart silently
# failed leaves it claiming the new version while the OLD process keeps
# serving -- the hole that let nine releases ship with no deployed
# daemon updating). The RUNNING code's own app-dir version cannot.

def test_status_reports_the_running_app_version_and_health_does_not(server):
    code, body = server.call("/status")
    assert code == 200
    assert body["app_version"] == "source", \
        "a source-tree daemon says so explicitly -- absence of the key " \
        "must remain the unique signature of a pre-6.0.213 daemon"
    code, health = server.call("/health", token=None)
    assert code == 200 and "app_version" not in health, \
        "/health is the one pre-token route: the running version is a " \
        "fingerprint that must stay behind authentication"


def test_running_app_version_reads_the_installed_dir_segment():
    assert ha._running_app_version(
        os.path.join("d", "app", "6.0.213", "convoy_hostapp.py")) == "6.0.213"
    assert ha._running_app_version(
        os.path.join("d", "dev", "convoy", "convoy_hostapp.py")) == "source"
    assert ha._running_app_version(
        os.path.join("d", "app", "6.0.213-rc1", "x.py")) == "source", \
        "only a plain dotted-numeric segment is a version"


def test_forget_node_deletes_a_stale_record(server):
    """/unregister keeps the node on purpose; forgetting is the explicit
    recovery for debris (e.g. a renamed .toe mints a new node_id and the
    old row lingers offline forever)."""
    _, node = _register(server, port=9981)
    code, body = server.call("/nodes/forget", {"node_id": node["node_id"]})
    assert code == 200 and body["forgotten"] is True
    _, listing = server.call("/nodes")
    assert node["node_id"] not in [n["node_id"] for n in listing["nodes"]]


def test_forget_node_refuses_while_the_node_still_has_work(server):
    """Forgetting a node with live work would strand jobs whose results
    nobody can collect."""
    _, node = _register(server, port=9981)
    code, _job = server.call("/jobs", {
        "idempotency_key": "keep-me", "node_id": node["node_id"],
        "operation": "query_network", "arguments": {}})
    assert code == 200
    code, body = server.call("/nodes/forget", {"node_id": node["node_id"]})
    assert code == 409 and body["reason"] == "node_has_work"
    _, listing = server.call("/nodes")
    assert node["node_id"] in [n["node_id"] for n in listing["nodes"]]


def test_forget_of_unknown_node_is_a_named_404(server):
    code, body = server.call("/nodes/forget", {"node_id": "ghost"})
    assert code == 404 and body["reason"] == "unknown_node"


# -- automatic stale-node eviction (the retention sweep) ----------------
#
# Field report 2026-08-04: abandoned test projects lingered as Offline
# rows forever -- only a clean unregister ever removed anything, and
# /nodes/forget had no caller. The sweep forgets a row only when it is
# silent AND either its .toe is provably deleted or the retention
# horizon has passed; offline alone is never stale (a closed TD stays
# remotely launchable, the documented contract).

def _register_with_toe(server, tmp_path, name, port=None):
    toe = tmp_path / name / (name + ".toe")
    toe.parent.mkdir(parents=True, exist_ok=True)
    toe.write_bytes(b"toe")
    body = {"project_root": str(toe.parent), "convoy_id": "cv",
            "comp_path": "/Embody", "metadata": {"toe_path": str(toe)}}
    if port is not None:
        body["envoy_port"] = port
    _, node = server.call("/register", body)
    return node, toe


def _sweep(server, ahead_s):
    return server.app._evict_stale_nodes(time.time() + ahead_s)


def test_eviction_forgets_a_deleted_projects_node(server, tmp_path):
    node, toe = _register_with_toe(server, tmp_path, "scratch", port=9981)
    server.call("/unregister", {"node_id": node["node_id"]})
    toe.unlink()                       # project deleted, parent remains
    evicted = _sweep(server, server.app.node_dead_grace_s + 60)
    assert [e["node_id"] for e in evicted] == [node["node_id"]]
    assert evicted[0]["cause"] == "dead_project"
    _, listing = server.call("/nodes")
    assert node["node_id"] not in [n["node_id"] for n in listing["nodes"]]


def test_eviction_spares_a_project_that_still_exists(server, tmp_path):
    node, _toe = _register_with_toe(server, tmp_path, "keeper", port=9981)
    server.call("/unregister", {"node_id": node["node_id"]})
    assert _sweep(server, server.app.node_dead_grace_s + 60) == []
    _, listing = server.call("/nodes")
    assert node["node_id"] in [n["node_id"] for n in listing["nodes"]], \
        "an offline node whose project exists is remotely launchable, not stale"


def test_eviction_waits_out_the_dead_grace(server, tmp_path):
    node, toe = _register_with_toe(server, tmp_path, "fresh", port=9981)
    server.call("/unregister", {"node_id": node["node_id"]})
    toe.unlink()
    assert _sweep(server, 5) == [], \
        "a just-silent node must ride out the grace before eviction"


def test_eviction_spares_unresolved_work_even_when_the_project_is_gone(
        server, tmp_path):
    node, toe = _register_with_toe(server, tmp_path, "busy", port=9981)
    code, _job = server.call("/jobs", {
        "idempotency_key": "hold", "node_id": node["node_id"],
        "operation": "query_network", "arguments": {}})
    assert code == 200
    server.call("/unregister", {"node_id": node["node_id"]})
    toe.unlink()
    assert _sweep(server, server.app.node_dead_grace_s + 60) == []


def test_eviction_never_touches_a_live_node(server, tmp_path):
    node, toe = _register_with_toe(server, tmp_path, "live", port=9981)
    toe.unlink()                       # even with the file gone
    assert _sweep(server, server.app.node_dead_grace_s + 60) == []
    _, listing = server.call("/nodes")
    assert node["node_id"] in [n["node_id"] for n in listing["nodes"]]


def test_retention_horizon_evicts_a_long_unseen_node(server, tmp_path):
    node, _toe = _register_with_toe(server, tmp_path, "ancient", port=9981)
    server.call("/unregister", {"node_id": node["node_id"]})
    evicted = _sweep(server, server.app.node_retention_s + 60)
    assert [e["node_id"] for e in evicted] == [node["node_id"]]
    assert evicted[0]["cause"] == "retired_unseen", \
        "even an intact project ages out past the retention horizon"


def test_the_reap_cadence_actually_runs_the_eviction_sweep(server, tmp_path):
    """The sweep's only production entry point is _maybe_reap on the drain
    cadence -- pin the chaining, not just the sweep in isolation."""
    node, toe = _register_with_toe(server, tmp_path, "chained", port=9981)
    server.call("/unregister", {"node_id": node["node_id"]})
    toe.unlink()
    server.app.node_dead_grace_s = 0.0     # collapse both grace windows
    server.app._last_reap = 0.0
    server.app._maybe_reap()
    _, listing = server.call("/nodes")
    assert node["node_id"] not in [n["node_id"] for n in listing["nodes"]], \
        "eviction must ride the reap cadence, not exist only as a method"


def test_eviction_still_works_from_the_durable_stamp_after_restart(
        server, tmp_path):
    """After a daemon restart the in-memory heartbeat is gone;
    load_directory replays rows without it, so eviction depends entirely
    on host.json's durable last_seen (the dominant real-world path)."""
    node, toe = _register_with_toe(server, tmp_path, "reborn", port=9981)
    server.call("/unregister", {"node_id": node["node_id"]})
    toe.unlink()
    reborn = ha.HostApp(
        server.app.data_dir,
        artifact_cache_path=os.path.join(server.app.data_dir,
                                         "test-artifacts-2"))
    assert node["node_id"] in [
        n.get("node_id") for n in reborn.directory.nodes()]
    reborn.started -= reborn.node_dead_grace_s + 1   # past the boot grace
    evicted = reborn._evict_stale_nodes(
        time.time() + reborn.node_dead_grace_s + 60)
    assert [e["node_id"] for e in evicted] == [node["node_id"]]
    assert evicted[0]["cause"] == "dead_project"


def test_no_eviction_during_the_daemon_boot_grace(server, tmp_path):
    """Seconds after boot, running TDs have not re-registered yet; the
    sweep must give them a full heartbeat cycle before judging silence."""
    node, toe = _register_with_toe(server, tmp_path, "booting", port=9981)
    server.call("/unregister", {"node_id": node["node_id"]})
    toe.unlink()
    future = time.time() + server.app.node_retention_s + 60
    original = server.app.started
    server.app.started = future - 30       # daemon "booted" 30s before now
    try:
        assert server.app._evict_stale_nodes(future) == [], \
            "a freshly booted daemon must not judge silence yet"
    finally:
        server.app.started = original


def test_an_unplugged_volume_is_never_a_deletion(tmp_path):
    """The mount-container rule is platform-shaped: an absent child of
    /Volumes (macOS) or /media (Linux) is an unplugged drive; on Windows
    an unplugged drive letter has no reachable ancestry at all. A deleted
    ordinary folder, by contrast, IS provably deleted."""
    gone = ha.HostApp._path_provably_deleted
    if sys.platform == "win32":
        assert gone("Q:\\nonexistent-drive\\show\\show.toe") is False
    elif sys.platform == "darwin":
        assert gone("/Volumes/UnpluggedDrive/show/show.toe") is False
    deleted = tmp_path / "was-here" / "show.toe"
    deleted.parent.mkdir()
    assert gone(str(deleted)) is True, \
        "a missing file under an existing folder is a deletion"


def test_unregister_requires_the_token(server):
    code, body = server.call("/unregister", {"node_id": "n"}, token=None)
    assert code == 401 and body["reason"] == "unauthenticated"


@pytest.mark.parametrize("body", [
    {},                             # missing
    {"node_id": ""},                # empty
    {"node_id": ["not", "a", "string"]},    # unhashable -> would be a 500
    {"node_id": "x" * 200},         # over MAX_ID_CHARS
])
def test_malformed_node_id_is_a_named_400(server, body):
    """text_field, not a bare dict lookup: a list node_id would raise
    TypeError at the directory lookup and become an unaudited 500."""
    code, payload = server.call("/unregister", body)
    assert code == 400 and payload["reason"] == "malformed"


def test_unregister_is_audited(server):
    _, node = _register(server, port=9981)
    server.call("/unregister", {"node_id": node["node_id"]})
    with server.app.lock:
        events = [r["event"] for r in server.app.db.audit_tail()]
    assert "node_unregistered" in events


def test_unregister_stamps_last_seen(server):
    """STRICTLY greater. `>=` is vacuously true when nothing is written,
    so it would still pass with the save_node call deleted -- and
    stamping last_seen is that call's ONLY effect (the port clear is
    memory-complete), i.e. the one behaviour it exists to protect."""
    _, node = _register(server, port=9981)
    with server.app.lock:
        before = server.app.db._state["nodes"][node["node_id"]]["last_seen"]
    time.sleep(0.05)
    server.call("/unregister", {"node_id": node["node_id"]})
    with server.app.lock:
        after = server.app.db._state["nodes"][node["node_id"]]["last_seen"]
    assert after > before


# -- ownership: only clear the port YOU registered ----------------------

def test_a_superseded_run_cannot_clear_a_live_port(server):
    """THE REGRESSION: two TD sessions on ONE project folder share a
    node_id (OQ-1), so without a precondition the first session's clean
    exit zeroes the SECOND session's live port and the survivor goes
    undispatchable -- a failure manufactured by an orderly shutdown."""
    _, first = _register(server, port=9981, runtime_id="rt_aaaaaaaaaaaaaaaa")
    _, second = _register(server, port=9990, runtime_id="rt_bbbbbbbbbbbbbbbb")
    assert first["node_id"] == second["node_id"], "shared identity (OQ-1)"

    # The DEPARTING first instance unregisters, naming its own run.
    code, body = server.call("/unregister",
                             {"node_id": first["node_id"],
                              "runtime_id": "rt_aaaaaaaaaaaaaaaa"})
    assert code == 200, "do not fight -- a no-op, not a refusal"
    assert body["cleared"] is False
    assert body["reason"] == "runtime_superseded"

    _, listing = server.call("/nodes")
    assert listing["nodes"][0]["envoy_port"] == 9990, (
        "the surviving instance keeps its live port")


def test_the_current_run_can_clear_its_own_port(server):
    _, node = _register(server, port=9981, runtime_id="rt_aaaaaaaaaaaaaaaa")
    code, body = server.call("/unregister",
                             {"node_id": node["node_id"],
                              "runtime_id": "rt_aaaaaaaaaaaaaaaa"})
    assert code == 200 and body["cleared"] is True
    assert body["envoy_port"] is None


def test_an_unregister_without_a_runtime_id_still_clears(server):
    """Best-effort by contract: a caller with no runtime_id to offer is
    not blocked, it just gets no ownership protection."""
    _, node = _register(server, port=9981, runtime_id="rt_aaaaaaaaaaaaaaaa")
    code, body = server.call("/unregister", {"node_id": node["node_id"]})
    assert code == 200 and body["cleared"] is True


def test_a_superseded_unregister_is_audited(server):
    _, node = _register(server, port=9981, runtime_id="rt_aaaaaaaaaaaaaaaa")
    _register(server, port=9990, runtime_id="rt_bbbbbbbbbbbbbbbb")
    server.call("/unregister", {"node_id": node["node_id"],
                                "runtime_id": "rt_aaaaaaaaaaaaaaaa"})
    with server.app.lock:
        events = [r["event"] for r in server.app.db.audit_tail()]
    assert "unregister_superseded" in events
    assert "node_unregistered" not in events, (
        "a no-op must not claim it unregistered anything")


def test_a_superseded_unregister_does_not_stamp_last_seen(server):
    """It did nothing, so it must not look like a visit."""
    _, node = _register(server, port=9981, runtime_id="rt_aaaaaaaaaaaaaaaa")
    _register(server, port=9990, runtime_id="rt_bbbbbbbbbbbbbbbb")
    with server.app.lock:
        before = server.app.db._state["nodes"][node["node_id"]]["last_seen"]
    time.sleep(0.05)
    server.call("/unregister", {"node_id": node["node_id"],
                                "runtime_id": "rt_aaaaaaaaaaaaaaaa"})
    with server.app.lock:
        after = server.app.db._state["nodes"][node["node_id"]]["last_seen"]
    assert after == before


@pytest.mark.parametrize("bad", [123, ["a"], {"a": 1}, "x" * 200])
def test_a_malformed_runtime_id_is_a_named_400(server, bad):
    _, node = _register(server, port=9981)
    code, body = server.call("/unregister", {"node_id": node["node_id"],
                                             "runtime_id": bad})
    assert code == 400 and body["reason"] == "malformed"


def test_registering_again_after_unregister_restores_the_port(server):
    """The heartbeat's healing path: a node that unregistered on exit and
    came back must get its port back, on the SAME node_id."""
    _, node = _register(server, port=9981)
    server.call("/unregister", {"node_id": node["node_id"]})

    _, again = _register(server, port=9982)
    assert again["node_id"] == node["node_id"]
    assert again["envoy_port"] == 9982

    _, listing = server.call("/nodes")
    assert listing["nodes"][0]["envoy_port"] == 9982


def test_a_portless_re_register_still_never_clears_a_known_port(server):
    """The rule /unregister exists to preserve: only the explicit route
    clears a port. A re-register that omits one must leave it alone."""
    _, node = _register(server, port=9981)
    _, again = _register(server)          # no envoy_port in the body
    assert again["envoy_port"] == 9981


def test_a_failed_last_seen_stamp_does_not_fail_the_clear(server,
                                                          monkeypatch):
    """The clear lives in memory and is already complete; save_node only
    stamps last_seen. Refusing a clear that demonstrably happened would
    be a lie, so the failure is audited, not escalated."""
    _, node = _register(server, port=9981)

    def boom(record):
        raise OSError("disk full (test)")
    monkeypatch.setattr(server.app.db, "save_node", boom)

    code, body = server.call("/unregister", {"node_id": node["node_id"]})
    assert code == 200 and body["envoy_port"] is None

    _, listing = server.call("/nodes")
    assert listing["nodes"][0]["envoy_port"] is None


def test_refusals_are_audited(server):
    server.call("/register", {"project_root": "/Work/x", "convoy_id": "c1", "comp_path": "/Embody"})
    server.call("/register", {"project_root": "/Work/x", "convoy_id": "c2"})
    with server.app.lock:
        events = [r["event"] for r in server.app.db.audit_tail()]
    assert "register_refused" in events, (
        "A-39: refusals must leave a trace -- visibility is the "
        "compensating control while there is no admission control")


# -- what a QUEUED delivery means for retirement -----------------------

def test_queued_host_native_work_tracks_the_dispatchers_own_rule(server):
    """A queued delivery may only be refused when retiring its row would
    actually strand it. The dispatcher decides that, at
    convoy_hostapp.py: needs_td_endpoint = (op not in HOST_NATIVE_OPERATIONS
    or op == 'convoy_restart_node'). Pin the two to each other so they
    cannot drift: refusing host-native work (which runs on the HOST and
    would have succeeded) destroys it, and sparing convoy_restart_node
    (which needs an endpoint that will never exist again) pins the row for
    ever.
    """
    app = server.app
    for operation in sorted(ha.HOST_NATIVE_OPERATIONS) + ["query_network"]:
        needs_td_endpoint = (operation not in ha.HOST_NATIVE_OPERATIONS
                             or operation == "convoy_restart_node")
        assert app._queued_delivery_survives_retirement(operation) is (
            not needs_td_endpoint), operation
    # The two cases that matter, stated outright.
    assert app._queued_delivery_survives_retirement("convoy_start_node")
    assert not app._queued_delivery_survives_retirement("convoy_restart_node")


def test_a_queue_larger_than_the_display_cap_still_retires(server, monkeypatch):
    """The census NAMES at most _CENSUS_NAMED_CAP deliveries to a human,
    but must COLLECT all of them: a gate that spared any row whose queue
    exceeded the cap would be a display constant deciding correctness, and
    would recreate the permanently-unretirable row this change exists to
    remove.

    The cap is monkeypatched DOWN so the test actually crosses it. An
    earlier version of this test queued 12 deliveries against a cap of
    512 and never entered the branch at all -- it would have passed with
    the guard present, absent or inverted.
    """
    monkeypatch.setattr(ha, "_CENSUS_NAMED_CAP", 2)
    _, old = server.call("/register", {
        "project_root": "/Work/show", "convoy_id": "cv", "comp_path": "/Embody",
        "envoy_port": 9981, "node_discriminator": "nd_" + "1" * 32})
    deliveries = []
    for i in range(5):
        code, created = server.call("/jobs", {
            "idempotency_key": "k%d" % i, "node_id": old["node_id"],
            "operation": "query_network", "arguments": {}})
        assert code == 200
        deliveries.append(created["job"]["delivery_id"])
    server.call("/unregister", {"node_id": old["node_id"]})

    # The refusal names only the cap, and says how many there really are.
    code, body = server.call("/nodes/forget", {"node_id": old["node_id"]})
    assert code == 409
    assert len(body["blocking"]) == 2, "only the cap is NAMED"
    assert body["pending_count"] == 5, "but the count is exact"

    server.call("/register", {
        "project_root": "/Work/show", "convoy_id": "cv", "comp_path": "/Embody",
        "envoy_port": 9982, "node_discriminator": "nd_" + "2" * 32})

    _, listing = server.call("/nodes")
    assert old["node_id"] not in [n["node_id"] for n in listing["nodes"]], \
        "a queue larger than the display cap must not pin the row"
    for delivery_id in deliveries:
        job = server.app.db.get_job(delivery_id)
        assert job["state"] == "refused", \
            "every queued delivery is resolved, not just the named ones"


def test_an_unreadable_job_record_spares_every_row_but_says_so(server):
    """Fail closed on an unreadable store -- a record nobody can parse
    might be a pending delivery for the row being retired. It must be
    LOUD (the ids are audited) and BOUNDED (reap clears it past
    retention), never a silent permanent freeze."""
    _, old = server.call("/register", {
        "project_root": "/Work/show", "convoy_id": "cv", "comp_path": "/Embody",
        "envoy_port": 9981, "node_discriminator": "nd_" + "1" * 32})
    server.call("/unregister", {"node_id": old["node_id"]})
    bad = os.path.join(server.app.db.jobs_dir, "cj_corrupt.json")
    with open(bad, "w", encoding="utf-8") as f:
        f.write("{ truncated")

    server.call("/register", {
        "project_root": "/Work/show", "convoy_id": "cv", "comp_path": "/Embody",
        "envoy_port": 9982, "node_discriminator": "nd_" + "2" * 32})
    _, listing = server.call("/nodes")
    assert old["node_id"] in [n["node_id"] for n in listing["nodes"]], \
        "an unreadable record must spare (fail closed)"

    # ...and the refusal names the file, so it can be found and removed.
    code, body = server.call("/nodes/forget", {"node_id": old["node_id"]})
    assert code == 503 and body["reason"] == "job_state_unreadable"

    # Removing it un-freezes cleanup on the very next register.
    os.unlink(bad)
    server.call("/register", {
        "project_root": "/Work/show", "convoy_id": "cv", "comp_path": "/Embody",
        "envoy_port": 9983, "node_discriminator": "nd_" + "3" * 32})
    _, listing = server.call("/nodes")
    assert old["node_id"] not in [n["node_id"] for n in listing["nodes"]]
