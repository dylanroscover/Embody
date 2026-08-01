"""The guarded request path: envelope verification (convoy_protocol),
the operation-registry gate (A-1), lease authorization
(convoy_controllers, A-17), and the capability manifest
(convoy_capabilities, A-23) -- wired into the LIVE host app and proven
over real loopback HTTP.

This is the Phase 1 completion suite: before it, those modules were
tested libraries guarding nothing; these tests pin the wiring.
"""

import http.client
import json
import os

import pytest

import convoy_capabilities as capabilities
import convoy_hostapp as ha
import convoy_protocol as protocol
from test_convoy_hostapp import Server


@pytest.fixture
def server(tmp_path):
    s = Server(str(tmp_path / "state"))
    yield s
    s.stop()


CONVOY = "studio"


def register(server, root="/Work/proj", comp="/Embody", convoy=CONVOY):
    code, node = server.call("/register", {
        "project_root": root, "convoy_id": convoy, "comp_path": comp})
    assert code == 200
    return node


def psk_for(server, convoy=CONVOY):
    code, body = server.call("/psk", {"convoy_id": convoy})
    assert code == 200
    return body["psk"]


def signed_envelope(server, node, psk, operation="query_network",
                    convoy=CONVOY, controller_id="ctl-test", **kw):
    signer = protocol.HmacSigner(psk)
    return protocol.build_envelope(
        convoy, server.app.host_id, controller_id, node["node_id"],
        operation, signer, **kw)


def audit_events(server):
    with server.app.lock:
        return server.app.db.audit_tail(limit=200)


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


# =====================================================================
# Authentication surface of the new routes
# =====================================================================

@pytest.mark.parametrize("path,payload", [
    ("/manifest", None),
    ("/leases", None),
    ("/envelope", {"envelope": {}}),
    ("/psk", {"convoy_id": CONVOY}),
    ("/leases", {"controller_id": "c", "node_id": "n"}),
    ("/leases/release", {"controller_id": "c", "node_id": "n"}),
    ("/heartbeat", {"controller_id": "c"}),
])
def test_every_new_route_requires_the_token(server, path, payload):
    code, body = server.call(path, payload, token=None)
    assert code == 401 and body["reason"] == "unauthenticated"


def test_unexpected_exception_is_a_clean_500_not_a_dead_thread(server,
                                                               monkeypatch):
    """The dead-handler-thread class: an unexpected exception must
    produce a named 500, never a connection with no response."""
    def boom():
        raise RuntimeError("wired wrong (test)")
    monkeypatch.setattr(server.app, "status", boom)
    code, body = server.call("/status")
    assert code == 500
    assert body["reason"] == "internal_error"
    assert body["detail"] == "RuntimeError"
    # And the server still answers afterwards.
    assert server.call("/health", token=None)[0] == 200


# =====================================================================
# Capability manifest (A-23)
# =====================================================================

def test_manifest_lists_the_phase1_registry(server):
    code, body = server.call("/manifest")
    assert code == 200
    manifest = body["manifest"]
    assert manifest["protocol"] == protocol.PROTOCOL
    assert set(manifest["operations"]) == set(ha.PHASE1_OPERATIONS)
    assert manifest["manifest_digest"].startswith("mf1-")
    assert all(d.startswith("op1-")
               for d in manifest["operations"].values())


def test_manifest_digest_material_is_the_registry_entry(server):
    """A-23: the digest covers name/schema/gating/side-effects of the
    REGISTRY ENTRY -- never source bytes or build numbers. Recomputing
    from the entry must reproduce the served digest exactly."""
    _, body = server.call("/manifest")
    entry = ha.PHASE1_OPERATIONS["set_op_position"]
    expected = capabilities.operation_digest(
        "set_op_position",
        schema=entry["schema"],
        gating={"mutating": True, "executes_arbitrary_code": False,
                "runtime_required": False, "remote_exposed": True},
        side_effects=entry["side_effects"])
    assert body["manifest"]["operations"]["set_op_position"] == expected


def test_the_remote_surface_is_a_strict_subset_and_excludes_the_exec_paths():
    """A-1 / R-2, pinned in the DATA before the code that reads it exists.

    remote_exposed is inert today -- nothing consults it, because nothing
    binds off-box. It is written now precisely so the boundary is already
    correct when Phase 3's LAN listener lands, rather than being drawn
    under time pressure next to a live socket.

    run_tests is the one that matters: its entry says
    executes_arbitrary_code False, and LOCALLY that is right -- it runs
    the owner's own suites. But TestRunnerExt discovers suites by
    SCANNING DISK (every test_*.py AND test_*.txt it finds), so "the code
    already in the project" is only true while nothing can put a file
    there. That is a loopback assumption, and this flag is where it stops
    being one.
    """
    remote = {name for name, entry in ha.PHASE1_OPERATIONS.items()
              if ha.gating_of(entry)["remote_exposed"]}
    assert remote == {"convoy_ping", "query_network", "capture_top",
                      "set_op_position"}
    assert "run_tests" not in remote, (
        "run_tests execs every test file it finds on disk -- never "
        "relayable to a remote peer (R-2)")
    assert "save_project" not in remote, (
        "save_project blocks TD's main thread 15+s; show protection "
        "(A-30/A-31) is Phase 4 -- not relayable before it exists")
    assert remote < set(ha.PHASE1_OPERATIONS), "must be a STRICT subset"


def test_an_unaudited_operation_is_not_remotely_exposed():
    """Absence is a refusal: an entry that never names remote_exposed is
    read as False, exactly as an unregistered operation is refused."""
    assert ha.gating_of({})["remote_exposed"] is False
    assert ha.gating_of({"executes_arbitrary_code": False})[
        "remote_exposed"] is False


def test_per_node_manifest_binds_the_node_and_shares_the_digest(server):
    node = register(server)
    code, bound = server.call(f"/manifest/{node['node_id']}")
    assert code == 200
    assert bound["manifest"]["node_id"] == node["node_id"]
    _, bare = server.call("/manifest")
    # manifest_digest covers protocol+operations only, so the per-node
    # view shares it -- that is what makes controller-side caching by
    # digest possible across nodes.
    assert (bound["manifest"]["manifest_digest"]
            == bare["manifest"]["manifest_digest"])


def test_manifest_for_unknown_node_is_404(server):
    code, body = server.call("/manifest/nope")
    assert code == 404 and body["reason"] == "unknown_node"


def test_controller_side_check_compatible_round_trip(server):
    """The refuse-before-send loop: a controller holding the served
    manifest accepts a matching digest and refuses a foreign one."""
    _, body = server.call("/manifest")
    manifest = capabilities.CapabilityManifest.from_dict(body["manifest"])
    good = body["manifest"]["operations"]["query_network"]
    assert capabilities.check_compatible(good, manifest,
                                         "query_network") is None
    with pytest.raises(capabilities.CompatibilityError):
        capabilities.check_compatible("op1-" + "0" * 24, manifest,
                                      "query_network")
    with pytest.raises(capabilities.CompatibilityError):
        capabilities.check_compatible(good, manifest, "execute_python")


# =====================================================================
# Convoy PSK issuance
# =====================================================================

def test_psk_is_minted_on_register_and_stable(server):
    register(server)
    first = psk_for(server)
    assert len(first) == 64
    int(first, 16)                      # well-formed 256-bit hex
    assert psk_for(server) == first
    assert first != server.app.token, "group key and IPC token differ"


def test_each_convoy_gets_its_own_psk(server):
    register(server)
    register(server, root="/Work/other", convoy="other-convoy")
    assert psk_for(server) != psk_for(server, "other-convoy")


def test_psk_for_unknown_convoy_is_404(server):
    code, body = server.call("/psk", {"convoy_id": "never-registered"})
    assert code == 404 and body["reason"] == "unknown_convoy"


def test_psk_value_never_reaches_the_audit_trail(server):
    register(server)
    psk = psk_for(server)
    with server.app.lock:
        raw = open(os.path.join(server.app.data_dir, "audit.jsonl"),
                   encoding="utf-8").read()
    assert "convoy_psk_minted" in raw and "convoy_psk_issued" in raw
    assert psk not in raw, "the audit trail records THAT, never the key"


# =====================================================================
# The envelope path: verify -> registry -> leases -> durable job
# =====================================================================

def test_signed_envelope_becomes_a_durable_idempotent_job(server):
    node = register(server)
    env = signed_envelope(server, node, psk_for(server),
                          arguments={"parent_path": "/"})
    code, body = server.call("/envelope", {"envelope": env})
    assert code == 200 and body["created"] is True
    job = body["job"]
    assert job["state"] == "queued"
    assert job["operation"] == "query_network"
    assert job["node_id"] == node["node_id"]
    assert job["convoy_id"] == CONVOY
    assert job["arguments"] == {"parent_path": "/"}

    # Durable: readable back through the job route.
    code, fetched = server.call(f"/jobs/{job['delivery_id']}")
    assert code == 200 and fetched["job"]["delivery_id"] == job["delivery_id"]

    # Idempotent: the SAME envelope acknowledges the SAME job.
    code, again = server.call("/envelope", {"envelope": env})
    assert code == 200 and again["created"] is False
    assert again["job"]["delivery_id"] == job["delivery_id"]


def test_wrong_psk_is_bad_signature_and_no_job(server):
    node = register(server)
    psk_for(server)                     # real key exists
    env = signed_envelope(server, node, "f" * 64)
    code, body = server.call("/envelope", {"envelope": env})
    assert code == 403 and body["reason"] == "bad_signature"
    _, status = server.call("/status")
    assert status["jobs_queued"] == 0, "a refused envelope creates nothing"
    events = audit_events(server)
    refused = [e for e in events if e["event"] == "envelope_refused"]
    assert refused and refused[-1]["detail"]["reason"] == "bad_signature"


def test_foreign_convoy_is_namespace_mismatch(server):
    node = register(server)
    env = signed_envelope(server, node, psk_for(server),
                          convoy="other-convoy")
    code, body = server.call("/envelope", {"envelope": env})
    assert code == 403 and body["reason"] == "namespace_mismatch"


def test_unknown_target_node_is_refused_before_any_crypto(server):
    register(server)
    env = signed_envelope(server, {"node_id": "0" * 32}, psk_for(server))
    code, body = server.call("/envelope", {"envelope": env})
    assert code == 404 and body["reason"] == "unknown_node"


def test_expired_deadline_is_refused(server):
    node = register(server)
    env = signed_envelope(server, node, psk_for(server), timeout_s=-1.0)
    code, body = server.call("/envelope", {"envelope": env})
    assert code == 410 and body["reason"] == "deadline_exceeded"


def test_tampered_arguments_are_refused(server):
    node = register(server)
    env = signed_envelope(server, node, psk_for(server),
                          arguments={"parent_path": "/"})
    env["arguments"] = {"parent_path": "/but/evil"}
    code, body = server.call("/envelope", {"envelope": env})
    assert code == 403 and body["reason"] == "arguments_tampered"


def test_algorithm_substitution_is_refused(server):
    node = register(server)
    env = signed_envelope(server, node, psk_for(server))
    env["sig_alg"] = "keypair-ed25519"
    code, body = server.call("/envelope", {"envelope": env})
    assert code == 403 and body["reason"] == "algorithm_mismatch"


def test_relayed_envelope_is_refused_in_v1(server):
    node = register(server)
    env = signed_envelope(server, node, psk_for(server),
                          source_host_id="some-relaying-host")
    code, body = server.call("/envelope", {"envelope": env})
    assert code == 409 and body["reason"] == "hop_limit_exceeded"


def test_signed_but_empty_idempotency_key_is_a_named_400(server):
    """verify_envelope does not require idempotency_key, but the job
    store does -- a signed-but-empty key must be a clean refusal, never
    an internal error."""
    node = register(server)
    psk = psk_for(server)
    signer = protocol.HmacSigner(psk)
    env = signed_envelope(server, node, psk)
    env["idempotency_key"] = ""
    # Private helper on purpose: the test needs a VALIDLY SIGNED envelope
    # with an empty key, which build_envelope refuses to make.
    env["signature"] = signer.sign(protocol._signing_payload(env))
    code, body = server.call("/envelope", {"envelope": env})
    assert code == 400 and body["reason"] == "malformed"
    assert "idempotency_key" in body["detail"]


def test_missing_envelope_object_is_malformed(server):
    assert server.call("/envelope", {})[0] == 400
    assert server.call("/envelope", {"envelope": "not-a-dict"})[0] == 400


# =====================================================================
# The registry gate (A-1)
# =====================================================================

def test_unregistered_operation_is_not_exposed(server):
    node = register(server)
    env = signed_envelope(server, node, psk_for(server),
                          operation="execute_python",
                          arguments={"code": "1+1"})
    code, body = server.call("/envelope", {"envelope": env})
    assert code == 403 and body["reason"] == "operation_not_exposed"
    _, status = server.call("/status")
    assert status["jobs_queued"] == 0


def test_code_executing_operation_is_refused_even_when_registered(server):
    """A-1: `executes_arbitrary_code: True` is refused outright until
    the TD-Python gate exists -- registration is not permission."""
    node = register(server)
    with server.app.lock:
        server.app.operations["dangerous_op"] = {
            "schema": {}, "mutating": True,
            "executes_arbitrary_code": True,
            "runtime_required": False, "side_effects": {}}
    env = signed_envelope(server, node, psk_for(server),
                          operation="dangerous_op")
    code, body = server.call("/envelope", {"envelope": env})
    assert code == 403 and body["reason"] == "operation_not_relayable"


def register_runtime_required_op(server, name="restart_node"):
    with server.app.lock:
        server.app.operations[name] = {
            "schema": {}, "mutating": True,
            "executes_arbitrary_code": False,
            "runtime_required": True, "side_effects": {}}


def test_runtime_required_operation_enforces_expected_runtime(server):
    """A-22: operations that may act on stale state MUST name the run
    they addressed; the registry flag drives the verifier."""
    node = register(server)
    psk = psk_for(server)
    register_runtime_required_op(server)

    env = signed_envelope(server, node, psk, operation="restart_node")
    code, body = server.call("/envelope", {"envelope": env})
    assert code == 400 and body["reason"] == "runtime_id_required"

    env = signed_envelope(server, node, psk, operation="restart_node",
                          expected_runtime_id="rt_" + "0" * 16)
    code, body = server.call("/envelope", {"envelope": env})
    assert code == 409 and body["reason"] == "runtime_changed"

    env = signed_envelope(server, node, psk, operation="restart_node",
                          expected_runtime_id=node["runtime_id"])
    code, body = server.call("/envelope", {"envelope": env})
    assert code == 200 and body["created"] is True


def test_local_jobs_path_enforces_the_runtime_precondition_too(server):
    """The two job-creating paths are ONE gate, not two authorities: the
    A-22 precondition lived only inside verify_envelope, so the local
    path would have enqueued a restart with no runtime check at all the
    moment such an operation was registered."""
    node = register(server)
    register_runtime_required_op(server)
    nid = node["node_id"]

    code, body = server.call("/jobs", {"idempotency_key": "r1",
                                       "node_id": nid,
                                       "operation": "restart_node"})
    assert code == 400 and body["reason"] == "runtime_id_required"

    code, body = server.call("/jobs", {"idempotency_key": "r2",
                                       "node_id": nid,
                                       "operation": "restart_node",
                                       "expected_runtime_id": "rt_stale"})
    assert code == 409 and body["reason"] == "runtime_changed"

    code, body = server.call("/jobs", {
        "idempotency_key": "r3", "node_id": nid,
        "operation": "restart_node",
        "expected_runtime_id": node["runtime_id"]})
    assert code == 200 and body["created"] is True


# =====================================================================
# Leases over the wire (A-17)
# =====================================================================

def acquire(server, controller, node_id, mode, **kw):
    return server.call("/leases", {"controller_id": controller,
                                   "node_id": node_id, "mode": mode, **kw})


def test_shared_holders_coexist_and_exclusive_is_blocked(server):
    node = register(server)
    nid = node["node_id"]
    assert acquire(server, "ctl-a", nid, "shared")[0] == 200
    assert acquire(server, "ctl-b", nid, "shared")[0] == 200
    code, body = acquire(server, "ctl-c", nid, "exclusive")
    assert code == 409 and body["reason"] == "node_leased"
    assert body["holder"] in ("ctl-a", "ctl-b")

    _, listing = server.call("/leases")
    rows = {(r["controller_id"], r["mode"]) for r in listing["leases"]}
    assert rows == {("ctl-a", "shared"), ("ctl-b", "shared")}


def test_exclusive_lease_gates_mutations_but_not_reads(server):
    node = register(server)
    psk = psk_for(server)
    nid = node["node_id"]
    assert acquire(server, "ctl-a", nid, "exclusive")[0] == 200

    # Another controller's mutation: refused, naming the holder.
    env = signed_envelope(server, node, psk, operation="set_op_position",
                          controller_id="ctl-b",
                          arguments={"op_path": "/x", "x": 0, "y": 0})
    code, body = server.call("/envelope", {"envelope": env})
    assert code == 409 and body["reason"] == "node_leased"
    assert body["holder"] == "ctl-a"

    # The same refused envelope succeeds once the lease is released --
    # a refusal must not burn the idempotency key.
    server.call("/leases/release", {"controller_id": "ctl-a",
                                    "node_id": nid})
    code, body = server.call("/envelope", {"envelope": env})
    assert code == 200 and body["created"] is True

    # Reads coexist with any lease.
    assert acquire(server, "ctl-a", nid, "exclusive")[0] == 200
    env = signed_envelope(server, node, psk, controller_id="ctl-b")
    assert server.call("/envelope", {"envelope": env})[0] == 200

    # The exclusive holder itself may mutate.
    env = signed_envelope(server, node, psk, operation="set_op_position",
                          controller_id="ctl-a",
                          arguments={"op_path": "/x", "x": 1, "y": 1})
    assert server.call("/envelope", {"envelope": env})[0] == 200


def test_shared_lease_grants_no_write_rights(server):
    node = register(server)
    psk = psk_for(server)
    assert acquire(server, "ctl-a", node["node_id"], "shared")[0] == 200
    env = signed_envelope(server, node, psk, operation="set_op_position",
                          controller_id="ctl-a",
                          arguments={"op_path": "/x", "x": 0, "y": 0})
    code, body = server.call("/envelope", {"envelope": env})
    assert code == 409 and body["reason"] == "shared_lease_no_mutation"


def test_release_is_idempotent_over_the_wire(server):
    node = register(server)
    nid = node["node_id"]
    acquire(server, "ctl-a", nid, "shared")
    code, body = server.call("/leases/release",
                             {"controller_id": "ctl-a", "node_id": nid})
    assert code == 200 and body["released"] is True
    code, body = server.call("/leases/release",
                             {"controller_id": "ctl-a", "node_id": nid})
    assert code == 200 and body["released"] is False
    _, listing = server.call("/leases")
    assert listing["leases"] == []


def test_lease_validation_refusals(server):
    node = register(server)
    nid = node["node_id"]
    code, body = acquire(server, "ctl-a", "no-such-node", "shared")
    assert code == 404 and body["reason"] == "unknown_node"
    code, body = acquire(server, "ctl-a", nid, "sideways")
    assert code == 400 and body["reason"] == "bad_mode"
    code, body = acquire(server, "ctl-a", nid, "shared", ttl_s="soon")
    assert code == 400 and body["reason"] == "malformed"
    code, body = acquire(server, "ctl-a", nid, "shared", ttl_s=-5)
    assert code == 400 and body["reason"] == "malformed"
    # An empty controller_id is caught by the field reader (which names
    # the field) before the lease registry sees it.
    code, body = acquire(server, "", nid, "shared")
    assert code == 400 and body["reason"] == "malformed"
    assert "controller_id" in body["detail"]


def test_heartbeat_route(server):
    code, body = server.call("/heartbeat", {"controller_id": "ctl-a",
                                            "label": "claude @ dev"})
    assert code == 200 and body["ok"] is True
    code, body = server.call("/heartbeat", {"controller_id": ""})
    assert code == 400 and body["reason"] == "malformed"


def test_lease_refusals_are_audited(server):
    node = register(server)
    acquire(server, "ctl-a", node["node_id"], "exclusive")
    acquire(server, "ctl-b", node["node_id"], "exclusive")
    events = [e["event"] for e in audit_events(server)]
    assert "lease_acquired" in events and "lease_refused" in events


# =====================================================================
# The local /jobs path shares the same gates -- one authority, not two
# =====================================================================

def test_local_jobs_route_enforces_the_registry(server):
    node = register(server)
    code, body = server.call("/jobs", {"idempotency_key": "k",
                                       "node_id": node["node_id"],
                                       "operation": "execute_python"})
    assert code == 403 and body["reason"] == "operation_not_exposed"


def test_local_jobs_route_respects_leases(server):
    node = register(server)
    nid = node["node_id"]
    acquire(server, "ctl-a", nid, "exclusive")
    # An anonymous local mutation is refused while the node is held...
    code, body = server.call("/jobs", {"idempotency_key": "k1",
                                       "node_id": nid,
                                       "operation": "set_op_position"})
    assert code == 409 and body["reason"] == "node_leased"
    # ...a read passes...
    code, _ = server.call("/jobs", {"idempotency_key": "k2",
                                    "node_id": nid,
                                    "operation": "query_network"})
    assert code == 200
    # ...and the holder itself may mutate.
    code, _ = server.call("/jobs", {"idempotency_key": "k3",
                                    "node_id": nid,
                                    "operation": "set_op_position",
                                    "controller_id": "ctl-a"})
    assert code == 200


# =====================================================================
# Clock-driven policy (direct app calls, injected time)
# =====================================================================

def make_app(tmp_path, clock):
    return ha.HostApp(str(tmp_path / "state"), now=clock)


def register_direct(app, root="/Work/p", comp="/Embody", convoy=CONVOY):
    code, node = app.register_node({"project_root": root,
                                    "convoy_id": convoy,
                                    "comp_path": comp})
    assert code == 200
    return node


def test_lease_ttl_expiry_frees_the_node(tmp_path):
    clock = Clock(1000.0)
    app = make_app(tmp_path, clock)
    node = register_direct(app)
    nid = node["node_id"]
    code, _ = app.acquire_lease({"controller_id": "ctl-a", "node_id": nid,
                                 "mode": "exclusive", "ttl_s": 10})
    assert code == 200
    code, body = app.create_job({"idempotency_key": "k1", "node_id": nid,
                                 "operation": "set_op_position",
                                 "controller_id": "ctl-b"})
    assert code == 409 and body["reason"] == "node_leased"

    clock.t += 11                      # the lease TTL has passed
    code, _ = app.create_job({"idempotency_key": "k2", "node_id": nid,
                              "operation": "set_op_position",
                              "controller_id": "ctl-b"})
    assert code == 200, "an expired lease must not gate anything"


def test_dead_controller_loses_its_lease(tmp_path):
    """A crashed controller must never wedge a node: when its heartbeat
    goes stale the lease falls, TTL notwithstanding."""
    clock = Clock(1000.0)
    app = make_app(tmp_path, clock)
    node = register_direct(app)
    nid = node["node_id"]
    code, _ = app.acquire_lease({"controller_id": "ctl-gone",
                                 "node_id": nid, "mode": "exclusive",
                                 "ttl_s": 3600})
    assert code == 200
    clock.t += 61            # past DEFAULT_CONTROLLER_TIMEOUT_S, no beats
    code, _ = app.create_job({"idempotency_key": "k", "node_id": nid,
                              "operation": "set_op_position",
                              "controller_id": "ctl-b"})
    assert code == 200
    assert app.list_leases()[1]["leases"] == []


def test_deadline_check_uses_the_injected_clock(tmp_path):
    """Both directions, because only the ACCEPT direction discriminates:
    an envelope minted at the injected now (t=1000) is decades expired
    against the real wall clock, so accepting it proves submit_envelope
    passes self._now() to the verifier rather than falling back to
    time.time(). The refusal direction alone would pass either way."""
    clock = Clock(1000.0)
    app = make_app(tmp_path, clock)
    node = register_direct(app)
    signer = protocol.HmacSigner(app.db.convoy_psk(CONVOY))

    def envelope_at(now, key):
        return protocol.build_envelope(
            CONVOY, app.host_id, "ctl-a", node["node_id"], "query_network",
            signer, timeout_s=30.0, now=now, idempotency_key=key)

    code, body = app.submit_envelope({"envelope": envelope_at(clock.t, "a")})
    assert code == 200 and body["created"] is True, (
        "an envelope live on the INJECTED clock must be accepted; a "
        "time.time() fallback would call it decades expired")

    env = envelope_at(clock.t, "b")
    clock.t += 31
    code, body = app.submit_envelope({"envelope": env})
    assert code == 410 and body["reason"] == "deadline_exceeded"


# =====================================================================
# Panel regressions (2026-07-31, five-lens review of the wiring)
# =====================================================================

def test_registry_entry_missing_a_gating_field_is_refused(server):
    """A-1: `executes_arbitrary_code` is REQUIRED and defaults TRUE for
    anything unaudited. Reading it as falsy-by-default inverted that --
    an entry present but unaudited was silently relayable."""
    node = register(server)
    with server.app.lock:
        server.app.operations["unaudited_op"] = {"schema": {}}

    code, body = server.call("/jobs", {"idempotency_key": "k",
                                       "node_id": node["node_id"],
                                       "operation": "unaudited_op"})
    assert code == 403 and body["reason"] == "operation_not_relayable"

    env = signed_envelope(server, node, psk_for(server),
                          operation="unaudited_op")
    code, body = server.call("/envelope", {"envelope": env})
    assert code == 403 and body["reason"] == "operation_not_relayable"


def test_gating_defaults_are_strict_for_every_field(server):
    """mutating and runtime_required default strict too: an entry that
    declares only that it does not execute code still needs the
    exclusive lease and the A-22 runtime precondition."""
    node = register(server)
    with server.app.lock:
        server.app.operations["half_declared"] = {
            "schema": {}, "executes_arbitrary_code": False}
    assert ha.gating_of({"executes_arbitrary_code": False}) == {
        "executes_arbitrary_code": False, "mutating": True,
        "runtime_required": True, "remote_exposed": False}

    # runtime_required defaults True -> refused without the precondition
    code, body = server.call("/jobs", {"idempotency_key": "k",
                                       "node_id": node["node_id"],
                                       "operation": "half_declared"})
    assert code == 400 and body["reason"] == "runtime_id_required"


def test_manifest_digest_reflects_enforced_gating_not_raw_fields(server):
    """The digest must describe the gating actually ENFORCED. If it
    digested the raw (missing) fields while the gate applied strict
    defaults, two hosts could agree on a digest and disagree on
    behaviour -- the exact drift A-23 exists to prevent."""
    with server.app.lock:
        server.app.operations["sparse"] = {"schema": {}}
    _, body = server.call("/manifest")
    assert body["manifest"]["operations"]["sparse"] == (
        capabilities.operation_digest(
            "sparse", schema={},
            gating={"executes_arbitrary_code": True, "mutating": True,
                    "runtime_required": True, "remote_exposed": False},
            side_effects=None))


def test_registry_entries_are_deep_copied_per_instance(tmp_path):
    """A shallow copy shared the nested schema dict with the module
    constant, so one instance's mutation changed every other
    instance's capability digest."""
    app = make_app(tmp_path, Clock())
    app.operations["query_network"]["schema"]["injected"] = "yes"
    assert "injected" not in ha.PHASE1_OPERATIONS["query_network"]["schema"]


def test_nan_ttl_cannot_mint_a_phantom_lease(server):
    """PROVEN BUG: NaN is a float and `nan <= 0` is False, so it passed
    validation; `now + nan` is nan, and `expires > now` then reads as
    expired. The caller got 200 and an 'exclusive' lease that held
    nothing while another controller mutated the node freely."""
    node = register(server)
    nid = node["node_id"]
    raw = ('{"controller_id": "ctl-a", "node_id": "%s", '
           '"mode": "exclusive", "ttl_s": NaN}' % nid)
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
    conn.request("POST", "/leases", body=raw.encode(),
                 headers={ha.TOKEN_HEADER: server.app.token,
                          "Content-Type": "application/json"})
    response = conn.getresponse()
    body = json.loads(response.read().decode())
    conn.close()
    assert response.status == 400 and body["reason"] == "malformed"

    _, listing = server.call("/leases")
    assert listing["leases"] == [], "no phantom lease may exist"


@pytest.mark.parametrize("token", ["NaN", "Infinity", "-Infinity"])
def test_json_non_finite_tokens_are_refused_at_the_door(server, token):
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
    conn.request("POST", "/jobs",
                 body=('{"idempotency_key": "k", "node_id": "n", '
                       '"operation": "query_network", "ttl_s": %s}'
                       % token).encode(),
                 headers={ha.TOKEN_HEADER: server.app.token,
                          "Content-Type": "application/json"})
    response = conn.getresponse()
    body = json.loads(response.read().decode())
    conn.close()
    assert response.status == 400 and body["reason"] == "malformed"


def test_signed_non_finite_deadline_is_refused(server):
    """PROVEN BUG: every comparison against NaN is False, so
    `deadline <= now` never fired and a signed NaN deadline became an
    immortal job. Only a PSK holder can sign one -- and a group member
    is exactly who the deadline is supposed to bound.

    TWO layers, both asserted: the transport refuses JSON's NaN token at
    the door, and the verifier refuses a non-finite deadline that
    reaches it by any other route (a future in-process caller, a
    different codec).
    """
    node = register(server)
    psk = psk_for(server)
    signer = protocol.HmacSigner(psk)
    for bad in (float("nan"), float("inf")):
        env = signed_envelope(server, node, psk)
        env["deadline_unix"] = bad
        env["signature"] = signer.sign(protocol._signing_payload(env))

        code, body = server.call("/envelope", {"envelope": env})
        assert code == 400 and body["reason"] == "malformed", bad

        with server.app.lock:
            code, body = server.app.submit_envelope({"envelope": env})
        assert code == 400 and body["reason"] == "malformed", bad
        assert "finite" in body["detail"], bad
    _, status = server.call("/status")
    assert status["jobs_queued"] == 0


TYPE_CONFUSED = [
    ("/jobs", {"idempotency_key": "k", "node_id": {"a": 1},
               "operation": "query_network"}),
    ("/jobs", {"idempotency_key": "k", "node_id": "n",
               "operation": ["query_network"]}),
    ("/jobs", {"idempotency_key": 7, "node_id": "n",
               "operation": "query_network"}),
    ("/envelope", {"envelope": {"target_node_id": {"a": 1},
                                "operation": "query_network"}}),
    ("/envelope", {"envelope": {"target_node_id": "n",
                                "operation": ["x"]}}),
    ("/psk", {"convoy_id": {"a": 1}}),
    ("/leases", {"controller_id": "c", "node_id": ["n"]}),
    ("/leases/release", {"controller_id": {"c": 1}, "node_id": "n"}),
    ("/heartbeat", {"controller_id": ["c"]}),
    ("/dispatch", {"delivery_id": {"a": 1}}),
    ("/dispatch", {"delivery_id": 5}),
]


@pytest.mark.parametrize("path,payload", TYPE_CONFUSED)
def test_type_confused_fields_are_named_400s_not_500s(server, path, payload):
    """PROVEN BUG: these values are used as DICT KEYS, so an unhashable
    one raised TypeError and surfaced as an UNAUDITED 500 -- a refusal
    the audit trail could not classify (A-39)."""
    code, body = server.call(path, payload)
    assert code == 400, (path, body)
    assert body["reason"] == "malformed"


def test_type_confused_refusals_are_audited(server):
    server.call("/psk", {"convoy_id": {"a": 1}})
    events = [e for e in audit_events(server)
              if e["event"] == "psk_refused"]
    assert events and events[-1]["detail"]["reason"] == "malformed"


def test_unknown_convoy_and_unknown_node_denials_are_audited(server):
    register(server)
    server.call("/psk", {"convoy_id": "never-registered"})
    server.call("/leases", {"controller_id": "c", "node_id": "ghost",
                            "mode": "shared"})
    detail_reasons = {(e["event"], e["detail"].get("reason"))
                      for e in audit_events(server)}
    assert ("psk_refused", "unknown_convoy") in detail_reasons
    assert ("lease_refused", "unknown_node") in detail_reasons


def test_failed_persist_on_re_registration_keeps_the_healthy_node(server,
                                                                  monkeypatch):
    """PROVEN BUG: the rollback forgot the record unconditionally, so a
    persist failure on a RE-register evicted an already-persisted node
    from memory -- unknown to every route while still on disk."""
    node = register(server)

    def boom(record):
        raise OSError("disk full (test)")
    monkeypatch.setattr(server.app.db, "save_node", boom)

    code, body = server.call("/register", {"project_root": "/Work/proj",
                                           "convoy_id": CONVOY,
                                           "comp_path": "/Embody"})
    assert code == 500 and body["reason"] == "persist_failed"

    _, listing = server.call("/nodes")
    assert [n["node_id"] for n in listing["nodes"]] == [node["node_id"]], (
        "the previously-persisted node must survive a failed re-register")
    # And it still works: the node is addressable.
    code, _ = server.call("/jobs", {"idempotency_key": "still-here",
                                    "node_id": node["node_id"],
                                    "operation": "query_network"})
    assert code == 200


def test_a_read_does_not_prop_up_a_dead_controllers_lease(tmp_path):
    """PROVEN BUG: every operation carrying a controller_id refreshed
    that controller's liveness -- including reads, which need no lease.
    Since controller_id is self-asserted, any caller could keep a dead
    controller's exclusive lease standing by naming it in harmless
    reads, defeating the heartbeat half of the dual expiry."""
    clock = Clock(1000.0)
    app = make_app(tmp_path, clock)
    node = register_direct(app)
    nid = node["node_id"]
    app.acquire_lease({"controller_id": "victim", "node_id": nid,
                       "mode": "exclusive", "ttl_s": 3600})

    clock.t += 61                       # victim stops heartbeating
    code, _ = app.create_job({"idempotency_key": "sneaky", "node_id": nid,
                              "operation": "query_network",
                              "controller_id": "victim"})
    assert code == 200                  # reads are always allowed
    assert not app.leases.controller_alive("victim", clock.t), (
        "a read must not refresh the named controller's liveness")
    assert app.list_leases()[1]["leases"] == []


def test_write_paths_reap_dead_controllers(tmp_path):
    """reap() ran only on GET /leases, so a caller that never listed
    leases grew the controller table without bound."""
    clock = Clock(1000.0)
    app = make_app(tmp_path, clock)
    for i in range(5):
        app.heartbeat_controller({"controller_id": f"ctl-{i}"})
    assert len(app.leases._controllers) == 5
    clock.t += 61
    app.heartbeat_controller({"controller_id": "ctl-fresh"})
    assert list(app.leases._controllers) == ["ctl-fresh"]


def test_post_catch_all_answers_instead_of_dying(server, monkeypatch):
    """The GET catch-all was covered; the POST one guards the new
    envelope/psk/lease routes and was not."""
    def boom(body):
        raise RuntimeError("wired wrong (test)")
    monkeypatch.setattr(server.app, "issue_convoy_psk", boom)
    code, body = server.call("/psk", {"convoy_id": CONVOY})
    assert code == 500 and body["reason"] == "internal_error"
    assert body["detail"] == "RuntimeError"
    assert server.call("/manifest")[0] == 200, "the server still serves"


def test_unserializable_response_becomes_a_named_500(server, monkeypatch):
    """A response Python can emit but JSON cannot express (bare NaN) is
    a response no strict client can read -- so it is a failure, not a
    200 carrying invalid JSON."""
    monkeypatch.setattr(server.app, "status",
                        lambda: {"ok": True, "uptime_s": float("nan")})
    code, body = server.call("/status")
    assert code == 500 and body["detail"] == "unserializable response"


# =====================================================================
# Restart semantics
# =====================================================================

def test_psk_survives_restart_and_leases_do_not(tmp_path):
    """The group key is durable state (host.json); leases are
    deliberately ephemeral -- a supervisor restart drops them and
    controllers re-acquire, exactly like claim_scope expiry."""
    directory = str(tmp_path / "state")
    first = Server(directory)
    try:
        node = register(first)
        psk = psk_for(first)
        assert acquire(first, "ctl-a", node["node_id"],
                       "exclusive")[0] == 200
    finally:
        first.stop()

    second = Server(directory)
    try:
        assert psk_for(second) == psk
        _, listing = second.call("/leases")
        assert listing["leases"] == [], "leases are ephemeral by design"
        # The surviving key still verifies a fresh envelope end to end.
        env = signed_envelope(second, node, psk)
        code, body = second.call("/envelope", {"envelope": env})
        assert code == 200 and body["created"] is True
    finally:
        second.stop()


# =====================================================================
# The async registry entries (Phase 4 polling slice)
#
# run_tests and save_project answer with a node-job HANDLE rather than a
# result. That is a GATING question as much as a transport one: they are
# consequential mutations, and their manifest digest has to say so.
# =====================================================================

def test_the_async_entries_declare_async_in_their_digest(server):
    """A-23: a controller that expects a RESULT and gets a job handle is
    talking to a host it does not understand, so async-ness is digest
    material. Folded in only when set.

    NOTE: adding remote_exposed to the ENFORCED gating moved every digest
    once, deliberately -- taken now, while there are zero cross-version
    peers in the field. After the first release with peers it would cost
    a renegotiation across a live mesh (Phase 3 plan, risk S15)."""
    _, body = server.call("/manifest")
    entry = ha.PHASE1_OPERATIONS["run_tests"]
    assert body["manifest"]["operations"]["run_tests"] == (
        capabilities.operation_digest(
            "run_tests", schema=entry["schema"],
            gating={"executes_arbitrary_code": False, "mutating": True,
                    "runtime_required": True, "remote_exposed": False},
            side_effects={**entry["side_effects"], "async_job": True}))
    sync = ha.PHASE1_OPERATIONS["query_network"]
    assert body["manifest"]["operations"]["query_network"] == (
        capabilities.operation_digest(
            "query_network", schema=sync["schema"],
            gating={"executes_arbitrary_code": False, "mutating": False,
                    "runtime_required": False, "remote_exposed": True},
            side_effects=sync["side_effects"])), \
        "an existing operation's digest moved"


@pytest.mark.parametrize("bad", [["a", "b"], "suite=x", 5, True])
def test_a_signed_envelope_with_non_dict_arguments_is_malformed(server, bad):
    """The envelope path accepts a non-dict `arguments` all the way
    through verification -- canonical_arguments_digest json-dumps any
    JSON value happily -- so the SAME named refusal has to live here, or
    one create path admits what the other refuses. `arguments` becomes
    the node's tool kwargs; a list was never meaningful."""
    node = register(server)
    env = signed_envelope(server, node, psk_for(server), arguments=bad)
    code, body = server.call("/envelope", {"envelope": env})
    assert code == 400 and body["reason"] == "malformed"
    assert "arguments" in body["detail"]
    _, status = server.call("/status")
    assert status["jobs_queued"] == 0


def test_the_async_operations_are_lease_gated_on_both_create_paths(server):
    """They are MUTATING, so a read lease must not grant the right to
    start one -- on either job-creating path. (The gate applies at
    ENQUEUE; dispatch re-checks the registry, the code flag and the
    runtime, but NOT leases, which are cooperative and TTL-bounded --
    re-checking them at dispatch would strand accepted jobs.)"""
    node = register(server)
    psk = psk_for(server)
    nid = node["node_id"]
    assert acquire(server, "ctl-reader", nid, "shared")[0] == 200

    code, body = server.call("/jobs", {
        "idempotency_key": "async-local", "node_id": nid,
        "operation": "run_tests", "controller_id": "ctl-reader",
        "expected_runtime_id": node["runtime_id"]})
    assert code == 409 and body["reason"] == "shared_lease_no_mutation"

    env = signed_envelope(server, node, psk, operation="save_project",
                          controller_id="ctl-reader",
                          expected_runtime_id=node["runtime_id"])
    code, body = server.call("/envelope", {"envelope": env})
    assert code == 409 and body["reason"] == "shared_lease_no_mutation"
