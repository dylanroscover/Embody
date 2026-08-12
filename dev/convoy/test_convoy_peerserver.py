"""The LAN peer listener, end to end over REAL mutual TLS on loopback.

This is slice 3's exit evidence in test form: a peer admitted on the
target, a signed Ed25519 envelope carried over a pinned mutual-TLS
connection, a durable job created with the right origin -- and, around
it, the A-5 boundary rows the transport can already prove (24.7's rule:
every refusal is AFFIRMATIVE and OBSERVED ON THE TARGET, never merely a
timeout at the caller).
"""

import json
import socket
import ssl
import threading
import time

import pytest

import convoy_hostapp as ha
import convoy_hostkeys as hk
import convoy_peerclient as pc
import convoy_peers as peers_mod
import convoy_peerserver as ps
import convoy_protocol as protocol


# -- the mesh: two hosts, B serving TLS, A admitted on B ---------------

class Mesh:
    """Host A (the connecting controller-host) and host B (the serving
    node-owner), with B's LAN listener live and A admitted on B."""

    def __init__(self, tmp_path, admit_a=True, audit=None):
        self.a = ha.HostApp(
            str(tmp_path / "a"),
            artifact_cache_path=str(tmp_path / "a-artifacts"))
        self.b = ha.HostApp(
            str(tmp_path / "b"),
            artifact_cache_path=str(tmp_path / "b-artifacts"))
        self.audit_events = [] if audit is None else audit
        if admit_a:
            self.admit_a_on_b()
        sink = lambda event, detail: self.audit_events.append((event, detail))
        self.server, self.port = ps.serve_lan(
            self.b, "127.0.0.1", 0, audit_sink=sink, now=self.b._now)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()
        self.b.set_lan_server(self.server, self.thread, "127.0.0.1", self.port)

    def admit_a_on_b(self):
        with self.b.lock:
            self.b.peers.admit(self.a.host_id, self.a.hostkeys.fingerprint,
                               cert_pem=self.a.hostkeys.certificate_pem,
                               convoy_ids=["studio"])

    def target(self, host_id=None, cert_pem=None, fingerprint=None):
        return pc.PeerTarget(
            host_id or self.b.host_id, "127.0.0.1", self.port,
            cert_pem or self.b.hostkeys.certificate_pem,
            fingerprint or self.b.hostkeys.fingerprint)

    runtime_id = "rt-1"

    def register_node(self, convoy_id="studio"):
        code, body = self.b.register_node({
            "project_root": "/Work/proj", "comp_path": "/Embody",
            "convoy_id": convoy_id, "runtime_id": self.runtime_id})
        assert code == 200, body
        return body["node_id"], convoy_id

    def envelope(self, node_id, convoy_id, operation="convoy_ping",
                 signer=None, origin_host_id=None, source_host_id=None,
                 arguments=None, timeout_s=60.0, expected_runtime_id=None,
                 idempotency_key=None):
        signer = signer or self.a.hostkeys.signer()
        return protocol.build_envelope(
            convoy_id, origin_host_id or self.a.host_id, "ctl-a", node_id,
            operation, signer, arguments=arguments or {},
            timeout_s=timeout_s, expected_runtime_id=expected_runtime_id,
            idempotency_key=idempotency_key, source_host_id=source_host_id)

    def raw(self, method, path, body=None, target=None, hostkeys=None):
        """A pinned-TLS request to an ARBITRARY path -- for the
        loopback-route-leakage sweep. Returns the parsed JSON body."""
        return pc._request(target or self.target(),
                            hostkeys or self.a.hostkeys, method, path,
                            body if body is None else json.dumps(body).encode(),
                            5.0)

    def stop(self):
        self.b.stop_lan_server()
        self.a.db.close()
        self.b.db.close()


@pytest.fixture
def mesh(tmp_path):
    m = Mesh(tmp_path)
    yield m
    m.stop()


# -- the happy path: an envelope becomes a job over mutual TLS ----------

def test_a_signed_envelope_over_mtls_creates_a_job(mesh):
    node_id, convoy_id = mesh.register_node()
    env = mesh.envelope(node_id, convoy_id, "convoy_ping")
    result = pc.send_envelope(mesh.target(), mesh.a.hostkeys, env)
    assert isinstance(result, dict), result
    assert result.get("ok") is True, result
    assert result["created"] is True
    # The delivery record's origin is the AUTHENTICATED peer, taken from
    # the certificate, never from the body.
    assert result["job"]["origin_host_id"] == mesh.a.host_id


def test_the_job_is_durable_on_the_target(mesh):
    node_id, convoy_id = mesh.register_node()
    env = mesh.envelope(node_id, convoy_id, "convoy_ping")
    result = pc.send_envelope(mesh.target(), mesh.a.hostkeys, env)
    delivery_id = result["job"]["delivery_id"]
    stored = mesh.b.db.get_job(delivery_id)
    assert stored is not None
    assert stored["origin_host_id"] == mesh.a.host_id
    assert stored["state"] == "queued"


def test_peer_health_confirms_the_reached_host(mesh):
    body = pc.get_peer_health(mesh.target(), mesh.a.hostkeys)
    assert body["ok"] is True
    assert body["host_id"] == mesh.b.host_id


# -- L-01: nothing before admission ------------------------------------

def test_an_unadmitted_peer_cannot_hand_off_work(tmp_path):
    m = Mesh(tmp_path, admit_a=False)
    try:
        node_id, convoy_id = m.register_node()
        env = m.envelope(node_id, convoy_id, "convoy_ping")
        result = pc.send_envelope(m.target(), m.a.hostkeys, env)
        # A's client cert is NOT in B's trust store, so B rejects it at
        # the handshake. The client outcome is UNREACHABLE or None (in
        # TLS 1.3 a mutual-auth rejection can surface post-handshake, so
        # the exact sentinel is timing-dependent) -- but NEVER a success.
        assert not (isinstance(result, dict) and result.get("ok"))
        # THE property, AFFIRMATIVE and OBSERVED ON THE TARGET (24.7): B
        # audited the handshake refusal, and nothing was enqueued. POLLED,
        # not asserted immediately: the client's refusal returns before
        # B's accept thread finishes writing the audit record on a loaded
        # runner (flaked on CI 2026-08-04).
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if any(e == "peer_handshake_refused" for e, _ in m.audit_events):
                break
            time.sleep(0.02)
        assert any(e == "peer_handshake_refused" for e, _ in m.audit_events)
        with m.b.lock:
            assert m.b.db.state_counts().get("queued", 0) == 0
    finally:
        m.stop()


# -- L-03: no loopback route is reachable from the LAN handler ----------

_LOOPBACK_GET = ["/status", "/nodes", "/manifest", "/manifest/n", "/identity",
                 "/lan/status", "/peers", "/leases", "/jobs/cj_x", "/health"]
_LOOPBACK_POST = ["/register", "/unregister", "/remint", "/jobs", "/envelope",
                  "/psk", "/identity/rotate", "/peers/admit", "/peers/block",
                  "/peers/forget", "/peers/observe", "/peers/quarantine",
                  "/peers/denylist", "/realm/reset",
                  "/lan/killswitch", "/leases", "/leases/release",
                  "/heartbeat", "/dispatch", "/drain", "/poll", "/shutdown"]
_LOOPBACK_POST.append("/relay/artifact")
_LOOPBACK_POST.append("/relay/artifact/release")
_LOOPBACK_POST.append("/artifact/export")

# Artifact content routes are loopback-token authenticated too.  Name all
# three shapes in the structural LAN-leak sweep; their bytes must never become
# reachable merely because the peer listener learned its own artifact routes.
_LOOPBACK_GET.append("/artifacts/c3R1ZGlv/art_" + "0" * 64)
_LOOPBACK_POST.extend([
    "/artifacts/c3R1ZGlv",
    "/artifacts/c3R1ZGlv/art_" + "0" * 64 + "/capability",
])


@pytest.mark.parametrize("path", _LOOPBACK_GET)
def test_no_loopback_get_route_answers_on_the_lan(mesh, path):
    body = mesh.raw("GET", path)
    assert isinstance(body, dict)
    assert body.get("reason") == "not_found", (path, body)


@pytest.mark.parametrize("path", _LOOPBACK_POST)
def test_no_loopback_post_route_answers_on_the_lan(mesh, path):
    body = mesh.raw("POST", path, body={})
    assert isinstance(body, dict)
    assert body.get("reason") == "not_found", (path, body)


def test_psk_is_specifically_unreachable_from_the_lan(mesh):
    """/psk hands out the group signing key (S2). Named on its own."""
    assert mesh.raw("POST", "/psk", body={"convoy_id": "studio"}
                    ).get("reason") == "not_found"


# -- L-06: authorize runs BEFORE the signature verifier ----------------

def test_a_blocked_peer_is_refused_before_verification(mesh, monkeypatch):
    node_id, convoy_id = mesh.register_node()
    with mesh.b.lock:
        mesh.b.peers.block(mesh.a.host_id)
    calls = []
    real_verify = protocol.verify_envelope
    monkeypatch.setattr(
        ha.protocol, "verify_envelope",
        lambda *a, **k: (calls.append(1), real_verify(*a, **k))[1])
    env = mesh.envelope(node_id, convoy_id, "convoy_ping")
    result = pc.send_envelope(mesh.target(), mesh.a.hostkeys, env)
    assert result.get("reason") == peers_mod.REASON_BLOCKED
    # THE ORDER, not the outcome: a blocked peer never reaches the
    # signature verifier, even though it still holds a valid key.
    assert calls == [], "verify_envelope must not run for a blocked peer"


# -- channel binding: source_mismatch (L-09/L-10) ----------------------

def test_an_envelope_claiming_a_third_origin_is_refused(mesh):
    node_id, convoy_id = mesh.register_node()
    # A signs (with its own key) an envelope that NAMES a third host as
    # origin -- the L-10 "make a third machine execute" shape.
    env = mesh.envelope(node_id, convoy_id, "convoy_ping",
                        origin_host_id="c" * 32)
    result = pc.send_envelope(mesh.target(), mesh.a.hostkeys, env)
    assert result.get("reason") == "source_mismatch"


def test_a_relayed_source_is_refused(mesh):
    node_id, convoy_id = mesh.register_node()
    # origin == A (authenticated) but source names a relay -> the L-09
    # replay-a-third-party's-envelope shape. Channel binding refuses it.
    env = mesh.envelope(node_id, convoy_id, "convoy_ping",
                        source_host_id="d" * 32)
    result = pc.send_envelope(mesh.target(), mesh.a.hostkeys, env)
    assert result.get("reason") == "source_mismatch"


# -- L-08 / S5: the listener chose the signer (no PSK downgrade) --------

def test_a_psk_signed_envelope_on_the_lan_is_refused(mesh):
    node_id, convoy_id = mesh.register_node()
    # A tries to authenticate with an HMAC (group) signature. The LAN
    # listener verifies against A's Ed25519 key regardless of what the
    # envelope claims, so the alg tag mismatch refuses it -- a peer
    # cannot downgrade to the group PSK even if it had one.
    hmac_signer = protocol.HmacSigner("x" * 32)
    env = mesh.envelope(node_id, convoy_id, "convoy_ping", signer=hmac_signer)
    result = pc.send_envelope(mesh.target(), mesh.a.hostkeys, env)
    assert result.get("reason") == "algorithm_mismatch"


def test_a_forged_ed25519_signature_is_refused(mesh):
    node_id, convoy_id = mesh.register_node()
    # Signed by a DIFFERENT Ed25519 key than A's pinned one: the alg tag
    # matches, but the signature does not verify against A's public key.
    stranger = hk.load_or_create(str(mesh.a.data_dir) + "-stranger")
    env = mesh.envelope(node_id, convoy_id, "convoy_ping",
                        signer=stranger.signer())
    result = pc.send_envelope(mesh.target(), mesh.a.hostkeys, env)
    assert result.get("reason") == "bad_signature"


# -- L-13: a cross-namespace envelope is refused -----------------------

def test_a_cross_namespace_envelope_is_refused(mesh):
    node_id, _convoy_id = mesh.register_node(convoy_id="studio")
    env = mesh.envelope(node_id, "a-different-convoy", "convoy_ping")
    result = pc.send_envelope(mesh.target(), mesh.a.hostkeys, env)
    # Namespace authorization is checked before envelope verification:
    # this peer has a valid host pin but no grant for the other Convoy.
    assert result.get("reason") == "namespace_not_admitted"


# -- remote surface: full node tools, with the independent code gate ---

def test_remote_run_tests_is_refused(mesh):
    # run_tests is never relayed to a remote peer. Without TD-Python approval
    # the TD-Python gate refuses it first (td_python_not_approved); with
    # approval the remote-exposed gate refuses it (operation_not_remote_exposed
    # -- covered in test_convoy_peer_hardening). Either way it never executes
    # (review 2026-08-02, finding 544).
    node_id, convoy_id = mesh.register_node()
    env = mesh.envelope(node_id, convoy_id, "run_tests",
                        expected_runtime_id=mesh.runtime_id)
    result = pc.send_envelope(mesh.target(), mesh.a.hostkeys, env)
    assert result.get("reason") == "td_python_not_approved"


def test_remote_save_project_is_refused_as_not_remote_exposed(mesh):
    # save_project blocks TD's main thread and is off the remote surface
    # until A-30/A-31 show protection exists (finding 544).
    node_id, convoy_id = mesh.register_node()
    env = mesh.envelope(node_id, convoy_id, "save_project",
                        expected_runtime_id=mesh.runtime_id)
    result = pc.send_envelope(mesh.target(), mesh.a.hostkeys, env)
    assert result.get("reason") == "operation_not_remote_exposed"


# -- /peer/manifest is filtered to remote-exposed ----------------------

def test_peer_manifest_excludes_the_non_remote_operations(mesh):
    # The peer manifest advertises only the remote-exposed surface; the two
    # worker-only operations must not appear on it (finding 544).
    body = pc.get_peer_manifest(mesh.target(), mesh.a.hostkeys)
    assert body["ok"] is True
    names = set(body["manifest"]["operations"].keys())
    assert "convoy_ping" in names
    assert "query_network" in names
    assert "run_tests" not in names
    assert "save_project" not in names


# -- /peer/nodes: namespace-bound discovery ---------------------------

def test_peer_nodes_route_delegates_only_after_namespace_authorization(mesh):
    calls = []

    def view(host_id, convoy_id, authenticated_fingerprint):
        calls.append((host_id, convoy_id, authenticated_fingerprint))
        return 200, {"ok": True, "convoy_id": convoy_id, "nodes": []}

    mesh.b.peer_nodes_view = view
    body = pc.get_peer_nodes(mesh.target(), mesh.a.hostkeys, "studio")
    assert body["ok"] is True
    assert calls == [(mesh.a.host_id, "studio",
                      mesh.a.hostkeys.fingerprint)]


def test_peer_nodes_route_rejects_noncanonical_or_escaping_segments(mesh):
    mesh.b.peer_nodes_view = lambda *_: pytest.fail(
        "malformed route reached HostApp")
    encoded_limit = ((ps.MAX_CONVOY_ID_BYTES * 4 + 2) // 3)
    for segment in ("!!!", "abc=", "abc/def", "x" * (encoded_limit + 1)):
        body = mesh.raw("GET", ps.ROUTE_NODES_PREFIX + segment)
        assert body["reason"] == "malformed", (segment, body)


def test_convoy_segment_encoding_round_trips_unicode_canonically():
    value = "studio-舞台"
    encoded = pc._encode_convoy_segment(value)
    assert "/" not in encoded and "=" not in encoded
    assert ps._decode_convoy_segment(encoded) == value
    assert ps._decode_convoy_segment(encoded + "=") is None


def test_oversize_hostapp_response_becomes_a_small_named_failure(mesh):
    mesh.b.peer_nodes_view = lambda *_: (
        200, {"ok": True, "convoy_id": "studio",
              "nodes": [{"node_id": "x" * ps.MAX_PEER_RESPONSE_BYTES}]})
    body = pc.get_peer_nodes(mesh.target(), mesh.a.hostkeys, "studio")
    assert body["ok"] is False
    assert body["reason"] == "response_too_large"


@pytest.mark.parametrize("payload", [
    {"ok": True, "convoy_id": "other", "nodes": []},
    {"ok": True, "convoy_id": "studio",
     "nodes": [{"node_id": "n", "convoy_id": "other"}]},
    {"ok": True, "convoy_id": "studio", "nodes": ["not-an-object"]},
])
def test_peer_nodes_route_fails_closed_on_an_unbound_hostapp_view(mesh,
                                                                  payload):
    mesh.b.peer_nodes_view = lambda *_: (200, payload)
    body = pc.get_peer_nodes(mesh.target(), mesh.a.hostkeys, "studio")
    assert body["reason"] == "invalid_peer_nodes_view"


def test_peer_nodes_route_caps_the_number_of_rows(mesh):
    payload = {"ok": True, "convoy_id": "studio",
               "nodes": [{} for _ in range(ps.MAX_PEER_NODES + 1)]}
    mesh.b.peer_nodes_view = lambda *_: (200, payload)
    body = pc.get_peer_nodes(mesh.target(), mesh.a.hostkeys, "studio")
    assert body["reason"] == "invalid_peer_nodes_view"


# -- /peer/controllers: namespace-bound, non-waking status -------------

def test_peer_controllers_route_delegates_after_namespace_authorization(mesh):
    calls = []

    def view(host_id, convoy_id, authenticated_fingerprint):
        calls.append((host_id, convoy_id, authenticated_fingerprint))
        return 200, {"ok": True, "convoy_id": convoy_id,
                     "controllers": [], "wakes_touchdesigner": False}

    mesh.b.peer_controllers_view = view
    body = pc.get_peer_controllers(mesh.target(), mesh.a.hostkeys, "studio")
    assert body["ok"] is True
    assert body["wakes_touchdesigner"] is False
    assert calls == [(mesh.a.host_id, "studio",
                      mesh.a.hostkeys.fingerprint)]


def test_peer_controllers_route_rejects_invalid_hostapp_shape(mesh):
    mesh.b.peer_controllers_view = lambda *_: (
        200, {"ok": True, "convoy_id": "studio",
              "controllers": [{"controller_id": ""}]})
    body = pc.get_peer_controllers(mesh.target(), mesh.a.hostkeys, "studio")
    assert body["reason"] == "invalid_peer_controllers_view"


def test_peer_controllers_route_rejects_escaping_namespace(mesh):
    mesh.b.peer_controllers_view = lambda *_: pytest.fail(
        "malformed controller route reached HostApp")
    body = mesh.raw("GET", ps.ROUTE_CONTROLLERS_PREFIX + "abc/def")
    assert body["reason"] == "malformed"


# -- /peer/jobs: per-peer authorization + the cursor -------------------

def test_a_peer_reads_its_own_delivery_record(mesh):
    node_id, convoy_id = mesh.register_node()
    env = mesh.envelope(node_id, convoy_id, "convoy_ping")
    created = pc.send_envelope(mesh.target(), mesh.a.hostkeys, env)
    delivery_id = created["job"]["delivery_id"]
    view = pc.get_peer_job(mesh.target(), mesh.a.hostkeys, delivery_id)
    assert view["ok"] is True
    assert view["changed"] is True
    assert view["job"]["delivery_id"] == delivery_id
    assert view["job"]["state"] == "queued"


def test_a_peer_cannot_read_another_origins_job(mesh):
    # A LOCAL job on B (origin None) must be invisible to peer A.
    with mesh.b.lock:
        job, _created = mesh.b.db.create_job(
            "local-key", "n" * 32, "convoy_ping", {}, convoy_id="studio")
    view = pc.get_peer_job(mesh.target(), mesh.a.hostkeys, job["delivery_id"])
    # NOT FOUND, never a 'forbidden' that would confirm the id exists.
    assert view.get("reason") == "not_found"


def test_an_unknown_delivery_id_is_not_found(mesh):
    view = pc.get_peer_job(mesh.target(), mesh.a.hostkeys, "cj_deadbeef")
    assert view.get("reason") == "not_found"


def test_the_cursor_reports_unchanged_when_since_is_current(mesh):
    node_id, convoy_id = mesh.register_node()
    created = pc.send_envelope(mesh.target(), mesh.a.hostkeys,
                               mesh.envelope(node_id, convoy_id))
    delivery_id = created["job"]["delivery_id"]
    first = pc.get_peer_job(mesh.target(), mesh.a.hostkeys, delivery_id)
    cursor = first["cursor"]
    again = pc.get_peer_job(mesh.target(), mesh.a.hostkeys, delivery_id,
                            since=cursor)
    assert again["changed"] is False
    assert again["cursor"] == cursor


def test_a_peer_cancels_its_own_queued_delivery(mesh):
    node_id, convoy_id = mesh.register_node()
    created = pc.send_envelope(mesh.target(), mesh.a.hostkeys,
                               mesh.envelope(node_id, convoy_id))
    delivery_id = created["job"]["delivery_id"]
    cancelled = pc.cancel_peer_job(
        mesh.target(), mesh.a.hostkeys, convoy_id, delivery_id)
    assert cancelled["ok"] is True
    assert cancelled["cancelled"] is True
    assert cancelled["definitive"] is True
    assert cancelled["job"]["state"] == "refused"


def test_a_peer_cannot_cancel_another_origins_delivery(mesh):
    with mesh.b.lock:
        job, _ = mesh.b.db.create_job(
            "local-cancel-key", "n" * 32, "convoy_ping", {},
            convoy_id="studio", origin_host_id=mesh.b.host_id)
    result = pc.cancel_peer_job(
        mesh.target(), mesh.a.hostkeys, "studio", job["delivery_id"])
    assert result["reason"] == "unknown_job"


def test_peer_cancel_is_namespace_bound_before_job_lookup(mesh):
    result = pc.cancel_peer_job(
        mesh.target(), mesh.a.hostkeys, "not-admitted", "cj_deadbeef")
    assert result["reason"] == peers_mod.REASON_NAMESPACE


def test_peer_terminal_outcome_requires_and_accepts_explicit_ack(mesh):
    node_id, convoy_id = mesh.register_node()
    created = pc.send_envelope(mesh.target(), mesh.a.hostkeys,
                               mesh.envelope(node_id, convoy_id))
    delivery_id = created["job"]["delivery_id"]
    code, dispatched = mesh.b.dispatch_job(delivery_id)
    assert code == 200 and dispatched["job"]["state"] == "succeeded"

    # Polling is observation only; it must not release retained terminal
    # evidence as a hidden side effect.
    viewed = pc.get_peer_job(mesh.target(), mesh.a.hostkeys, delivery_id)
    assert viewed["job"]["state"] == "succeeded"
    assert mesh.b.db.get_job(delivery_id)["outcome_acknowledged_at"] is None

    acknowledged = pc.acknowledge_peer_job(
        mesh.target(), mesh.a.hostkeys, convoy_id, delivery_id)
    assert acknowledged["ok"] is True
    assert acknowledged["already_acknowledged"] is False
    assert acknowledged["wakes_touchdesigner"] is False
    assert mesh.b.db.get_job(delivery_id)["outcome_acknowledged_at"] \
        == acknowledged["acknowledged_at"]


def test_peer_ack_is_owner_and_namespace_bound(mesh):
    with mesh.b.lock:
        job, _ = mesh.b.db.create_job(
            "local-ack-key", "n" * 32, "convoy_ping", {},
            convoy_id="studio", origin_host_id=mesh.b.host_id)
        mesh.b.db.mark_refused(job["delivery_id"], {"reason": "test"})
    other_owner = pc.acknowledge_peer_job(
        mesh.target(), mesh.a.hostkeys, "studio", job["delivery_id"])
    assert other_owner["reason"] == "unknown_job"
    other_namespace = pc.acknowledge_peer_job(
        mesh.target(), mesh.a.hostkeys, "not-admitted", job["delivery_id"])
    assert other_namespace["reason"] == peers_mod.REASON_NAMESPACE


# -- caps: the handshake-audit rate limiter ----------------------------

def test_token_bucket_suppresses_and_carries_the_count():
    clock = {"t": 1000.0}
    bucket = ps._TokenBucket(capacity=2, refill_per_s=0.0,
                             now=lambda: clock["t"])
    assert bucket.take() == (True, 0)
    assert bucket.take() == (True, 0)
    # Exhausted: suppressed, count accrues.
    assert bucket.take() == (False, 0)
    assert bucket.take() == (False, 0)
    # A refill lets one through, carrying the suppressed count.
    clock["t"] = 1000.0
    bucket._tokens = 1.0            # simulate a refill deterministically
    allowed, carried = bucket.take()
    assert allowed is True
    assert carried == 2


def test_the_context_is_rebuilt_when_the_peer_set_changes(mesh):
    provider = ps._ContextProvider(mesh.b)
    ctx1 = provider.context()
    # A second, distinct admitted peer changes the trust signature.
    other = hk.load_or_create(str(mesh.b.data_dir) + "-other")
    with mesh.b.lock:
        mesh.b.peers.admit("e" * 32, other.fingerprint,
                           cert_pem=other.certificate_pem)
    ctx2 = provider.context()
    assert ctx1 is not ctx2, "an admission must rebuild the trust store"
    # Stable when nothing changed.
    assert provider.context() is ctx2


# -- the server binds a NAMED address, never the wildcard --------------

def test_serve_lan_binds_the_named_address(tmp_path):
    app = ha.HostApp(str(tmp_path / "solo"))
    try:
        server, port = ps.serve_lan(app, "127.0.0.1", 0)
        try:
            assert server.server_address[0] == "127.0.0.1"
            assert port == server.server_address[1]
        finally:
            server.server_close()
    finally:
        app.db.close()


# -- PANEL FINDING A: reads are authorized too (X0 containment) --------

@pytest.mark.parametrize("route", [ps.ROUTE_HEALTH, ps.ROUTE_MANIFEST])
def test_a_blocked_peer_is_refused_on_read_routes(mesh, route):
    """A blocked peer keeps its pin (so it still handshakes), and the
    contract says blocked is 'refused for every class INCLUDING X0'.
    Reads must be authorized, not just /peer/envelope."""
    with mesh.b.lock:
        mesh.b.peers.block(mesh.a.host_id)
    body = mesh.raw("GET", route)
    assert body.get("reason") == peers_mod.REASON_BLOCKED, (route, body)


def test_a_blocked_peer_cannot_read_its_own_job(mesh):
    node_id, convoy_id = mesh.register_node()
    created = pc.send_envelope(mesh.target(), mesh.a.hostkeys,
                               mesh.envelope(node_id, convoy_id))
    delivery_id = created["job"]["delivery_id"]
    with mesh.b.lock:
        mesh.b.peers.block(mesh.a.host_id)
    view = pc.get_peer_job(mesh.target(), mesh.a.hostkeys, delivery_id)
    assert view.get("reason") == peers_mod.REASON_BLOCKED


def test_the_killswitch_contains_reads(mesh):
    """A-32: the killswitch refuses EVERY peer. It is consulted inside
    authorize_peer, so the read routes must run authorize_peer or the
    emergency stop would leak reads."""
    with mesh.b.lock:
        mesh.b.peers.set_killswitch(True, "incident")
    assert mesh.raw("GET", ps.ROUTE_MANIFEST).get("reason") == \
        peers_mod.REASON_BLOCKED
    assert mesh.raw("GET", ps.ROUTE_HEALTH).get("reason") == \
        peers_mod.REASON_BLOCKED


def test_a_pending_peer_cannot_read(tmp_path):
    """A peer RECORDED (with a cert) but never admitted is a stranger --
    'refused like a stranger'. Its recorded cert lets it handshake, so
    the read routes must still refuse it."""
    m = Mesh(tmp_path, admit_a=False)
    try:
        with m.b.lock:
            m.b.peers.record_peer(m.a.host_id, m.a.hostkeys.fingerprint,
                                  cert_pem=m.a.hostkeys.certificate_pem)
        body = m.raw("GET", ps.ROUTE_MANIFEST)
        # pending -> authorize_peer refuses REASON_UNKNOWN (not admitted).
        assert body.get("reason") == peers_mod.REASON_UNKNOWN, body
    finally:
        m.stop()


def test_an_observe_only_peer_may_still_read(mesh):
    """Observe-only is ALLOWED (X0 permitted); only mutations are refused.
    So the read routes must NOT refuse it."""
    with mesh.b.lock:
        mesh.b.peers.observe(mesh.a.host_id)
    body = pc.get_peer_manifest(mesh.target(), mesh.a.hostkeys)
    assert body["ok"] is True


# -- PANEL FINDING D: a refusal does not hang a keep-alive connection ---

def test_a_second_request_after_a_refusal_still_answers(mesh):
    """A blocked peer's first request is refused; a SECOND request on the
    same keep-alive connection must also get a response, never hang until
    the I/O timeout (the cached-None-returns-no-response bug)."""
    import http.client
    with mesh.b.lock:
        mesh.b.peers.block(mesh.a.host_id)
    ctx = pc.build_client_ssl_context(mesh.a.hostkeys,
                                      mesh.b.hostkeys.certificate_pem)
    conn = http.client.HTTPSConnection("127.0.0.1", mesh.port,
                                       context=ctx, timeout=10)
    try:
        conn.request("GET", ps.ROUTE_HEALTH)
        r1 = conn.getresponse()
        r1.read()
        assert r1.status == 403
        assert r1.getheader("Connection") == "close"
        # http.client transparently opens a NEW TLS connection for the
        # second request. It must answer, while the refused connection's
        # bounded server slot has already been released.
        conn.request("GET", ps.ROUTE_MANIFEST)
        r2 = conn.getresponse()
        r2.read()
        assert r2.status == 403
        assert r2.getheader("Connection") == "close"
    finally:
        conn.close()


def test_a_normal_response_keeps_its_authenticated_control_connection(mesh):
    import http.client
    ctx = pc.build_client_ssl_context(mesh.a.hostkeys,
                                      mesh.b.hostkeys.certificate_pem)
    conn = http.client.HTTPSConnection("127.0.0.1", mesh.port,
                                       context=ctx, timeout=10)
    try:
        conn.request("GET", ps.ROUTE_HEALTH)
        response = conn.getresponse()
        response.read()
        assert response.status == 200
        assert response.getheader("Connection") == "keep-alive"
        assert response.will_close is False
        # The same TLS connection remains usable and is authorized again on
        # every request.
        sock = conn.sock
        conn.request("GET", ps.ROUTE_MANIFEST)
        again = conn.getresponse()
        again.read()
        assert again.status == 200
        assert conn.sock is sock
    finally:
        conn.close()


def test_connection_caps_apply_before_worker_thread_creation(tmp_path):
    """A slow handshake from one IP cannot create overflow threads."""
    app = ha.HostApp(str(tmp_path / "bounded"))
    overflow = threading.Event()
    audit = []

    def sink(event, detail):
        audit.append((event, detail))
        if event == "peer_connection_overflow":
            overflow.set()

    server, port = ps.serve_lan(
        app, "127.0.0.1", 0, audit_sink=sink,
        max_connections=2, max_connections_per_ip=1,
        handshake_timeout=5.0)
    worker_started = threading.Event()
    workers = []
    original = server.process_request_thread

    def counted(request, address):
        workers.append(address)
        worker_started.set()
        return original(request, address)

    server.process_request_thread = counted
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    first = second = None
    try:
        first = socket.create_connection(("127.0.0.1", port), timeout=2)
        assert worker_started.wait(2), "first connection got no worker"
        # It sends no TLS ClientHello, so it occupies the single per-IP
        # handshake slot. The next accept must close before spawning.
        second = socket.create_connection(("127.0.0.1", port), timeout=2)
        assert overflow.wait(2), "overflow connection was not refused"
        assert len(workers) == 1
        assert audit[-1][1]["max_connections_per_ip"] == 1
    finally:
        if second is not None:
            second.close()
        if first is not None:
            first.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        app.db.close()


def test_default_connection_budget_covers_the_thirty_host_mesh():
    assert ps.DEFAULT_MAX_CONNECTIONS >= 29
    assert ps.PeerHTTPSServer.request_queue_size >= 29


def test_per_ip_cap_covers_one_peers_session_control_and_transfers():
    """A single well-behaved peer legitimately holds, from its own IP, one
    long-lived /peer/session WSS upgrade + one persistent pooled control
    connection + up to DEFAULT_MAX_TRANSFERS one-shot artifact streams. The
    per-IP cap must clear that sum (1 + 1 + 4 = 6) or a peer starves its own
    transfers behind its own session/control connections."""
    import convoy_artifact_http as ah
    assert (ps.DEFAULT_MAX_CONNECTIONS_PER_IP
            >= ah.DEFAULT_MAX_TRANSFERS + 2)


def test_a_bad_artifact_route_post_drains_body_and_stays_usable(mesh):
    """do_POST that raises ArtifactHTTPError from parse_route (here, a query
    string) leaves the declared request body unread. It MUST DRAIN that body
    and deliver the error on a still-usable keep-alive connection. Closing
    without draining RSTs the socket on Windows and discards the response we
    just wrote (review 2026-08-02). Two consecutive POSTs on the SAME
    connection both getting a clean 400 proves the response was delivered
    (no RST) AND the body was drained (else the second would mis-parse)."""
    import http.client
    ctx = pc.build_client_ssl_context(mesh.a.hostkeys,
                                      mesh.b.hostkeys.certificate_pem)
    conn = http.client.HTTPSConnection("127.0.0.1", mesh.port,
                                       context=ctx, timeout=10)
    try:
        for _ in range(2):
            conn.request("POST", ps.ROUTE_ARTIFACTS_PREFIX + "c3R1ZGlv?x=1",
                         body=b'{"unread": "body"}')
            response = conn.getresponse()
            response.read()
            assert response.status == 400
    finally:
        conn.close()


# -- PANEL FINDING C: one bad cert cannot brick the whole trust store ---

def test_a_malformed_peer_cert_does_not_brick_the_trust_store(tmp_path):
    """A single unparseable stored cert_pem must not take down every peer
    handshake -- the affected peer is unreachable, the rest of the mesh
    is fine."""
    m = Mesh(tmp_path)                       # A admitted with a good cert
    try:
        junk = hk.load_or_create(str(tmp_path / "junk"))   # a valid fp shape
        with m.b.lock:
            # A second peer with a MALFORMED cert enters the trust store.
            m.b.peers.admit("f" * 32, junk.fingerprint,
                            cert_pem="-----BEGIN CERTIFICATE-----\nnope\n"
                                     "-----END CERTIFICATE-----\n")
        # A (good cert) can STILL connect and read.
        body = pc.get_peer_health(m.target(), m.a.hostkeys)
        assert body["ok"] is True and body["host_id"] == m.b.host_id
    finally:
        m.stop()


def test_build_context_isolates_a_bad_anchor(tmp_path):
    """Unit-level: build_server_ssl_context loads the good anchors and
    silently drops a malformed one instead of raising."""
    good = hk.load_or_create(str(tmp_path / "good"))
    ctx = ps.build_server_ssl_context(
        good.cert_path, good.key_path,
        [good.certificate_pem, "not a certificate at all"])
    # One valid anchor loaded; the junk was dropped, no exception.
    assert ctx.cert_store_stats()["x509"] >= 1


def test_a_taken_port_refuses_named(tmp_path):
    app = ha.HostApp(str(tmp_path / "solo"))
    try:
        first, port = ps.serve_lan(app, "127.0.0.1", 0)
        try:
            with pytest.raises(ps.LanBindError) as e:
                ps.serve_lan(app, "127.0.0.1", port)
            assert e.value.reason == "lan_port_in_use"
        finally:
            first.server_close()
    finally:
        app.db.close()
