"""Host app end to end: real HTTP over loopback, real DB, real token.

Includes the PHASE 1 EXIT criterion:
  "two local fake nodes register; durable jobs survive host restart."
"""

import json
import os
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


def test_supersession_spares_a_record_that_still_owns_work(server):
    """Retiring a node with an uncollected result would strand it."""
    _, old = server.call("/register", {
        "project_root": "/Work/show", "convoy_id": "cv", "comp_path": "/Embody",
        "envoy_port": 9981, "node_discriminator": "nd_" + "1" * 32})
    code, _job = server.call("/jobs", {
        "idempotency_key": "keep", "node_id": old["node_id"],
        "operation": "query_network", "arguments": {}})
    assert code == 200
    server.call("/unregister", {"node_id": old["node_id"]})
    server.call("/register", {
        "project_root": "/Work/show", "convoy_id": "cv", "comp_path": "/Embody",
        "envoy_port": 9982, "node_discriminator": "nd_" + "2" * 32})
    _, listing = server.call("/nodes")
    assert old["node_id"] in [n["node_id"] for n in listing["nodes"]],         "a node with unresolved work must not be auto-retired"


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
