"""Phase 4 slice 1: the host dispatches a QUEUED job to the node and
mirrors the verdict. The A-15 invariant holds end to end -- a job leaves
'queued' only by an OBSERVED node result or, on transport failure,
mark_indeterminate; the host never invents a verdict. The forwarder (the
MCP transport, A-46) is injected here so the ORCHESTRATION is tested
without a live node.
"""

import pytest

import convoy_hostapp as ha
import convoy_hoststore as hs
from test_convoy_hostapp import Server


@pytest.fixture
def server(tmp_path):
    s = Server(str(tmp_path / "state"))
    yield s
    s.stop()


CONVOY = "studio"


def register(server, envoy_port=9800, root="/Work/p", comp="/Embody"):
    code, node = server.call("/register", {
        "project_root": root, "convoy_id": CONVOY, "comp_path": comp,
        "envoy_port": envoy_port})
    assert code == 200
    return node


def enqueue(server, node, operation="query_network", key="k",
            arguments=None):
    code, body = server.call("/jobs", {
        "idempotency_key": key, "node_id": node["node_id"],
        "operation": operation, "arguments": arguments or {}})
    assert code == 200
    return body["job"]


# -- envoy_port in registration ---------------------------------------

def test_registration_carries_the_envoy_port(server):
    code, node = server.call("/register", {
        "project_root": "/Work/p", "convoy_id": CONVOY,
        "comp_path": "/Embody", "envoy_port": 9872})
    assert code == 200 and node["envoy_port"] == 9872
    # and it is on the in-memory node record the dispatcher reads
    with server.app.lock:
        rec = server.app.directory.lookup(node["node_id"])
    assert rec["envoy_port"] == 9872


def test_envoy_port_is_optional(server):
    code, node = server.call("/register", {
        "project_root": "/Work/p", "convoy_id": CONVOY,
        "comp_path": "/Embody"})
    assert code == 200 and node["envoy_port"] is None


@pytest.mark.parametrize("bad", [0, 70000, -1, "9800", 98.6, True])
def test_malformed_envoy_port_refused(server, bad):
    code, body = server.call("/register", {
        "project_root": "/Work/p", "convoy_id": CONVOY,
        "comp_path": "/Embody", "envoy_port": bad})
    assert code == 400 and body["reason"] == "malformed"


def test_a_re_register_without_port_keeps_the_known_port(server):
    node = register(server, envoy_port=9801)
    # TD re-registers (restart) but omits the port this time
    server.call("/register", {"project_root": "/Work/p", "convoy_id": CONVOY,
                              "comp_path": "/Embody"})
    with server.app.lock:
        rec = server.app.directory.lookup(node["node_id"])
    assert rec["envoy_port"] == 9801, "a known port is never cleared"


# -- dispatch: the verdict mirror --------------------------------------

def test_dispatch_mirrors_a_successful_node_result(server):
    node = register(server)
    job = enqueue(server, node, operation="query_network",
                  arguments={"parent_path": "/"})
    seen = {}

    def forwarder(port, operation, arguments):
        seen.update(port=port, operation=operation, arguments=arguments)
        return {"ok": True, "result": {"ops": ["/a", "/b"]}}

    server.app.forwarder = forwarder
    code, body = server.call("/dispatch",
                             {"delivery_id": job["delivery_id"]})
    assert code == 200 and body["dispatched"] is True
    updated = body["job"]
    assert updated["state"] == "succeeded"
    assert updated["verdict_source"] == "node_sync"
    assert updated["result"] == {"ops": ["/a", "/b"]}
    assert updated["node_job_id"] is None, "a sync op mints no node job id"
    # the forwarder was handed the node's port + the job's operation
    assert seen == {"port": 9800, "operation": "query_network",
                    "arguments": {"parent_path": "/"}}


def test_dispatch_mirrors_a_node_error(server):
    node = register(server)
    job = enqueue(server, node)
    server.app.forwarder = lambda p, o, a: {"ok": False,
                                            "error": "op not found"}
    code, body = server.call("/dispatch",
                             {"delivery_id": job["delivery_id"]})
    assert code == 200 and body["job"]["state"] == "failed"
    assert body["job"]["result"] == {"error": "op not found"}
    assert body["job"]["verdict_source"] == "node_sync"


def test_transport_failure_is_indeterminate_never_a_fake_verdict(server):
    node = register(server)
    job = enqueue(server, node)
    server.app.forwarder = lambda p, o, a: None   # no response
    code, body = server.call("/dispatch",
                             {"delivery_id": job["delivery_id"]})
    assert code == 200 and body["job"]["state"] == "indeterminate"
    assert body["job"]["result"]["reason"] == "node_unreachable"


def test_a_raising_forwarder_is_indeterminate_not_a_crash(server):
    node = register(server)
    job = enqueue(server, node)

    def boom(p, o, a):
        raise RuntimeError("socket exploded")

    server.app.forwarder = boom
    code, body = server.call("/dispatch",
                             {"delivery_id": job["delivery_id"]})
    assert code == 200 and body["job"]["state"] == "indeterminate"
    assert "RuntimeError" in body["job"]["result"]["detail"]


def test_dispatch_is_idempotent_never_reruns(server):
    node = register(server)
    job = enqueue(server, node)
    calls = {"n": 0}

    def once(p, o, a):
        calls["n"] += 1
        return {"ok": True, "result": {"n": calls["n"]}}

    server.app.forwarder = once
    first = server.call("/dispatch", {"delivery_id": job["delivery_id"]})[1]
    assert first["dispatched"] is True and first["job"]["state"] == "succeeded"
    # a second dispatch must NOT forward again -- the job is terminal
    second = server.call("/dispatch", {"delivery_id": job["delivery_id"]})[1]
    assert second["dispatched"] is False
    assert second["job"]["state"] == "succeeded"
    assert calls["n"] == 1, "a dispatched job is never re-executed"


def test_dispatch_without_a_known_port_leaves_the_job_queued(server):
    _, node = server.call("/register", {
        "project_root": "/Work/p", "convoy_id": CONVOY,
        "comp_path": "/Embody"})           # no envoy_port
    job = enqueue(server, node)
    server.app.forwarder = lambda p, o, a: {"ok": True, "result": 1}
    code, body = server.call("/dispatch",
                             {"delivery_id": job["delivery_id"]})
    assert code == 409 and body["reason"] == "node_endpoint_unknown"
    # the job is untouched -- still queued, ready to dispatch once known
    _, fetched = server.call(f"/jobs/{job['delivery_id']}")
    assert fetched["job"]["state"] == "queued"


def test_dispatch_unknown_job_is_404(server):
    code, body = server.call("/dispatch", {"delivery_id": "cj_nope"})
    assert code == 404 and body["reason"] == "unknown_job"


def test_dispatch_requires_the_token(server):
    code, body = server.call("/dispatch", {"delivery_id": "cj_x"},
                             token=None)
    assert code == 401 and body["reason"] == "unauthenticated"


def test_the_default_forwarder_is_the_mcp_client(server):
    """The host app dispatches through the real minimal MCP client by
    default; a node whose Envoy is unreachable fails SAFE to
    indeterminate (proven with an injected transport failure elsewhere)."""
    import convoy_mcpclient as mcpclient
    assert server.app.forwarder is mcpclient.forward


# -- record_sync_result unit contract ----------------------------------

def test_record_sync_result_contract(tmp_path):
    db = hs.HostStore(str(tmp_path / "s"))
    job, _ = db.create_job("k", "n", "query_network", {}, "cv")
    did = job["delivery_id"]

    ok = db.record_sync_result(did, True, observed_at=1000.0,
                               result={"v": 1})
    assert ok["state"] == "succeeded" and ok["verdict_source"] == "node_sync"
    assert ok["observed_at"] == 1000.0 and ok["node_job_id"] is None

    job2, _ = db.create_job("k2", "n", "x", {}, "cv")
    bad = db.record_sync_result(job2["delivery_id"], False, observed_at=1.0,
                                result={"error": "boom"})
    assert bad["state"] == "failed"

    with pytest.raises(ValueError):
        db.record_sync_result(did, True, observed_at=None)
    with pytest.raises(KeyError):
        db.record_sync_result("cj_missing", True, observed_at=1.0)
    db.close()
