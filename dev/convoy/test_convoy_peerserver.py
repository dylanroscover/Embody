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
        self.a = ha.HostApp(str(tmp_path / "a"))
        self.b = ha.HostApp(str(tmp_path / "b"))
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
                               cert_pem=self.a.hostkeys.certificate_pem)

    def target(self, host_id=None, cert_pem=None, fingerprint=None):
        return pc.PeerTarget(
            host_id or self.b.host_id, "127.0.0.1", self.port,
            cert_pem or self.b.hostkeys.certificate_pem,
            fingerprint or self.b.hostkeys.fingerprint)

    runtime_id = "rt-1"

    def register_node(self, convoy_id="studio"):
        with self.b.lock:
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
        # audited the handshake refusal, and nothing was enqueued.
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
                  "/lan/killswitch", "/leases", "/leases/release",
                  "/heartbeat", "/dispatch", "/drain", "/poll", "/shutdown"]


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
    assert result.get("reason") == "namespace_mismatch"


# -- remote surface: run_tests / save_project stay off the LAN ---------

@pytest.mark.parametrize("operation", ["run_tests", "save_project"])
def test_non_remote_exposed_operations_are_refused_for_a_peer(mesh, operation):
    node_id, convoy_id = mesh.register_node()
    # A VALID runtime, so verify_envelope's A-22 precondition passes and
    # the remote_exposed gate is what refuses -- otherwise the trap the
    # handoff names fires: runtime_id_required proves the wrong refusal.
    env = mesh.envelope(node_id, convoy_id, operation,
                        expected_runtime_id=mesh.runtime_id)
    result = pc.send_envelope(mesh.target(), mesh.a.hostkeys, env)
    assert result.get("reason") == "operation_not_remote_exposed"


# -- /peer/manifest is filtered to remote-exposed ----------------------

def test_peer_manifest_hides_non_exposed_operations(mesh):
    body = pc.get_peer_manifest(mesh.target(), mesh.a.hostkeys)
    assert body["ok"] is True
    names = set(body["manifest"]["operations"].keys())
    assert "convoy_ping" in names
    assert "query_network" in names
    assert "run_tests" not in names
    assert "save_project" not in names


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
        # SECOND request on the SAME connection -- must answer, not hang.
        conn.request("GET", ps.ROUTE_MANIFEST)
        r2 = conn.getresponse()
        r2.read()
        assert r2.status == 403
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
