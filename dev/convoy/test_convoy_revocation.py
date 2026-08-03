"""Peers wired into the live host app: THE ORDER, the loopback routes,
the `refused` delivery terminal, and the four in-flight revocation cases.

STILL NO NETWORK. Every "peer" here arrives through submit_envelope's
`origin` seam -- the identity slice 3's TLS layer will establish locally
from the certificate it actually saw. What is under test is the DECISION
and its ORDER, which is exactly the part that must already be right
before a socket exists.
"""

import json
import tempfile
import time

import pytest

import convoy_controllers as controllers
import convoy_hostapp as ha
import convoy_hostkeys as hk
import convoy_hoststore as hs
import convoy_peers as cp
import convoy_protocol as protocol
from test_convoy_hostapp import Server

CONVOY = "studio"

# SLICE 3: a peer is now identified by a REAL pinned Ed25519 key, not a
# fictional fingerprint. The two test peers get actual identities (minted
# once, in throwaway temp dirs) so their fingerprints are real, their
# envelopes carry real Ed25519 signatures, and submit_envelope's peer
# path -- which builds the verifier from the PINNED public key and refuses
# a source that is not the authenticated peer -- exercises the production
# contract instead of a PSK simulation. host_id stays independent of the
# key (the pin is the binding (host_id, fingerprint)).
_PEER_KEYS = hk.load_or_create(tempfile.mkdtemp(prefix="cv_peer_"))
_OTHER_KEYS = hk.load_or_create(tempfile.mkdtemp(prefix="cv_other_"))

PEER = "ab" * 16
PEER_FP = _PEER_KEYS.fingerprint
OTHER = "cd" * 16
OTHER_FP = _OTHER_KEYS.fingerprint

# Look up a peer's key material by the fingerprint being pinned/presented,
# so a re-pin (admit PEER under OTHER_FP) or a mismatch test picks the key
# that matches the fingerprint, not the host_id.
_KEYS_BY_FP = {PEER_FP: _PEER_KEYS, OTHER_FP: _OTHER_KEYS}


@pytest.fixture
def server(tmp_path):
    s = Server(str(tmp_path / "state"))
    yield s
    s.stop()


def register(server, envoy_port=9800, root="/Work/p", comp="/Embody"):
    code, node = server.call("/register", {
        "project_root": root, "convoy_id": CONVOY, "comp_path": comp,
        "envoy_port": envoy_port})
    assert code == 200
    return node


def psk_for(server, convoy=CONVOY):
    code, body = server.call("/psk", {"convoy_id": convoy})
    assert code == 200
    return body["psk"]


def envelope_for(server, node, psk, operation="query_network",
                 controller_id="ctl-peer", origin_host_id=None, **kw):
    signer = protocol.HmacSigner(psk)
    return protocol.build_envelope(
        CONVOY, origin_host_id or server.app.host_id, controller_id,
        node["node_id"], operation, signer, **kw)


def admit(server, host_id=PEER, fingerprint=PEER_FP, **kw):
    # Carry the certificate that matches the fingerprint being pinned, so
    # the admission is realistic (the LAN listener's trust store needs it);
    # authorize_peer only checks the fingerprint, so tests that pin a
    # deliberate mismatch still behave as before.
    if "cert_pem" not in kw and fingerprint in _KEYS_BY_FP:
        kw["cert_pem"] = _KEYS_BY_FP[fingerprint].certificate_pem
    # Namespace membership is explicit and fail-closed.  Most revocation
    # tests predate that invariant and exercise admission lineage rather
    # than namespace selection, so give their shared fixture the Convoy it
    # actually submits work for.  Individual namespace tests can still
    # override this value.
    kw.setdefault("convoy_ids", [CONVOY])
    code, body = server.call("/peers/admit",
                             {"host_id": host_id,
                              "fingerprint": fingerprint, **kw})
    assert code == 200, body
    return body["peer"]


def submit_as_peer(server, envelope, host_id=PEER, fingerprint=PEER_FP):
    """The slice-3 seam: an envelope arriving FROM a peer whose identity
    the TLS layer established LOCALLY from the certificate it presented.

    This stands in for a correct peer client: it stamps the envelope's
    origin AND source to the authenticated host (the LAN listener refuses
    any other -- source_mismatch, tested in test_convoy_peerserver) and
    re-signs with the peer's PINNED Ed25519 key, then hands submit_envelope
    the public key the way the real listener recomputes it from the cert.
    A fingerprint with no matching key (a deliberate mismatch/unknown
    test) is left unsigned with no public_der -- authorize_peer refuses it
    before verification, so the signature never matters.
    """
    envelope = dict(envelope)
    envelope["origin_host_id"] = host_id
    envelope["source_host_id"] = host_id
    keys = _KEYS_BY_FP.get(fingerprint)
    public_der = None
    if keys is not None:
        envelope["sig_alg"] = keys.alg
        envelope["signature"] = keys.signer().sign(
            protocol._signing_payload(envelope))
        public_der = keys.public_der
    with server.app.lock:
        return server.app.submit_envelope(
            {"envelope": envelope},
            origin={"host_id": host_id, "fingerprint": fingerprint,
                    "public_der": public_der})


def write_denylist(server, payload, age_s=5.0):
    """Hand-edit denylist.json, back-dated so the mtime cache is really
    exercised rather than bypassed by the fresh-file rule."""
    import os
    path = server.app.peers.denylist.path
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(payload))
    old = time.time() - age_s
    os.utime(path, (old, old))


def peer_job(server, node, operation="query_network", key="k",
             controller_id="ctl-peer", host_id=PEER, fingerprint=PEER_FP,
             psk=None):
    psk = psk or psk_for(server)
    envelope = envelope_for(server, node, psk, operation=operation,
                            controller_id=controller_id,
                            idempotency_key=key)
    code, body = submit_as_peer(server, envelope, host_id, fingerprint)
    assert code == 200, body
    return body["job"]


def audit_events(server):
    with server.app.lock:
        return [e["event"] for e in server.app.db.audit_tail(limit=400)]


# =====================================================================
# THE `refused` TERMINAL (proposed A-54)
# =====================================================================

def test_refused_is_a_host_originable_delivery_terminal():
    """A-15 forbids the host originating running/succeeded/failed --
    claims about EXECUTION. `refused` is none of those: it is a DELIVERY
    verdict, and delivery is what the host is the authority on.

    SHAPE ONLY. This asserts the vocabulary, and on its own that is a
    tautology over a documentation constant -- which is exactly what it
    was until _apply_state started enforcing it. The ENFORCEMENT is
    test_new7_the_host_originable_states_constant_is_ENFORCED, and the
    A-15 guarantee proper is record_node_verdict demanding provenance.
    """
    assert "refused" in hs.JOB_STATES
    assert "refused" in hs.TERMINAL_STATES
    assert "refused" in hs._HOST_ORIGINABLE_STATES
    for verdict in ("running", "succeeded", "failed"):
        assert verdict not in hs._HOST_ORIGINABLE_STATES


def test_mark_refused_requires_evidence(tmp_path):
    """Exactly like mark_indeterminate: a terminal the host wrote on its
    own authority must carry what the host saw."""
    db = hs.HostStore(str(tmp_path / "s"))
    job, _ = db.create_job("k", "n", "query_network", {}, "cv")
    with pytest.raises(ValueError) as e:
        db.mark_refused(job["delivery_id"], None)
    assert "evidence" in str(e.value)
    refused = db.mark_refused(job["delivery_id"], {"reason": "peer_revoked"})
    assert refused["state"] == "refused"
    assert refused["result"] == {"reason": "peer_revoked"}
    assert refused["verdict_source"] == "host"
    db.close()


def test_mark_refused_only_ever_touches_a_queued_job(tmp_path):
    """The narrowness IS the safety argument: `refused` is only honest
    while the operation provably never left this host."""
    db = hs.HostStore(str(tmp_path / "s"))
    claimed, _ = db.create_job("k1", "n", "query_network", {}, "cv")
    db.claim_for_dispatch(claimed["delivery_id"])
    assert db.mark_refused(claimed["delivery_id"], {"r": 1}) is None

    running, _ = db.create_job("k2", "n", "run_tests", {}, "cv")
    db.record_node_verdict(running["delivery_id"], "running",
                           node_job_id="job_0000abcd", observed_at=1.0)
    assert db.mark_refused(running["delivery_id"], {"r": 1}) is None

    done, _ = db.create_job("k3", "n", "query_network", {}, "cv")
    db.record_sync_result(done["delivery_id"], True, observed_at=1.0)
    assert db.mark_refused(done["delivery_id"], {"r": 1}) is None

    assert db.mark_refused("cj_nope", {"r": 1}) is None
    db.close()


def test_a_refused_job_is_settled_and_stays_settled(tmp_path):
    db = hs.HostStore(str(tmp_path / "s"))
    job, _ = db.create_job("k", "n", "query_network", {}, "cv")
    did = job["delivery_id"]
    db.mark_refused(did, {"reason": "peer_revoked"})
    assert db.claim_for_dispatch(did) is None, "a settled job never re-runs"
    # a late 'running' answer must not drag it back open
    mirrored = db.record_node_verdict(did, "running",
                                      node_job_id="job_0000abcd",
                                      observed_at=2.0)
    assert mirrored["state"] == "refused"
    db.close()


def test_a_refused_job_survives_a_host_restart(tmp_path):
    directory = str(tmp_path / "s")
    db = hs.HostStore(directory)
    job, _ = db.create_job("k", "n", "query_network", {}, "cv")
    db.mark_refused(job["delivery_id"], {"reason": "peer_revoked"})
    db.close()
    again = hs.HostStore(directory)
    assert again.get_job(job["delivery_id"])["state"] == "refused"
    again.close()


# =====================================================================
# WHO ASKED -- origin_host_id + controller_id on the delivery record
# =====================================================================

def test_the_delivery_record_names_who_asked(tmp_path):
    db = hs.HostStore(str(tmp_path / "s"))
    job, _ = db.create_job("k", "n", "query_network", {}, "cv",
                           origin_host_id=PEER, controller_id="ctl-7")
    assert job["origin_host_id"] == PEER
    assert job["controller_id"] == "ctl-7"
    assert db.get_job(job["delivery_id"])["origin_host_id"] == PEER
    db.close()


def test_a_record_with_no_origin_reads_as_local(tmp_path):
    """Every record written before this field existed has no origin, and
    only this host could have created one -- so absent means local, and
    the drain must never refuse those."""
    db = hs.HostStore(str(tmp_path / "s"))
    job, _ = db.create_job("k", "n", "query_network", {}, "cv")
    assert job["origin_host_id"] is None and job["controller_id"] is None
    db.close()


@pytest.mark.parametrize("bad", [5, [], {}, "", None, True])
def test_a_malformed_origin_never_masquerades_as_one(tmp_path, bad):
    db = hs.HostStore(str(tmp_path / "s"))
    job, _ = db.create_job("k", "n", "query_network", {}, "cv",
                           origin_host_id=bad, controller_id=bad)
    assert job["origin_host_id"] is None and job["controller_id"] is None
    db.close()


def test_a_local_job_records_this_host_as_the_origin(server):
    node = register(server)
    code, body = server.call("/jobs", {
        "idempotency_key": "k", "node_id": node["node_id"],
        "operation": "query_network", "controller_id": "ctl-local"})
    assert code == 200
    assert body["job"]["origin_host_id"] == server.app.host_id
    assert body["job"]["controller_id"] == "ctl-local"


def test_a_peer_job_records_the_peer_as_the_origin(server):
    node = register(server)
    admit(server)
    job = peer_job(server, node)
    assert job["origin_host_id"] == PEER
    # NAMESPACED by origin: a peer must not be able to name (and so
    # inherit, or revoke) another controller's lease identity.
    assert job["controller_id"] == cp.namespaced_controller(PEER, "ctl-peer")


# =====================================================================
# THE ORDER INVARIANT -- the verifier spy that MUST NOT be called
# =====================================================================

class VerifierSpy:
    """Stands in for convoy_protocol.verify_envelope and RECORDS whether
    authentication was reached at all."""

    def __init__(self, real):
        self.real = real
        self.calls = 0

    def __call__(self, *a, **kw):
        self.calls += 1
        return self.real(*a, **kw)


@pytest.fixture
def spy(monkeypatch):
    s = VerifierSpy(protocol.verify_envelope)
    monkeypatch.setattr(ha.protocol, "verify_envelope", s)
    return s


def test_the_verifier_IS_reached_for_an_admitted_peer(server, spy):
    """POSITIVE CONTROL. Without this, every 'spy not called' assertion
    below would also pass on a build where the envelope path is simply
    broken."""
    node = register(server)
    admit(server)
    envelope = envelope_for(server, node, psk_for(server))
    code, body = submit_as_peer(server, envelope)
    assert code == 200 and body["ok"] is True
    assert spy.calls == 1


def test_the_verifier_is_NEVER_called_for_a_blocked_peer(server, spy):
    """THE order test. Not "a blocked peer is refused" -- that could be
    true with the check bolted on AFTER authentication, which would mean
    a revoked peer still gets its signature examined and its arguments
    parsed. The assertion is about the ORDER: nothing downstream of the
    denylist runs at all."""
    node = register(server)
    admit(server)
    envelope = envelope_for(server, node, psk_for(server))
    code, body = server.call("/peers/block", {"host_id": PEER})
    assert code == 200

    code, body = submit_as_peer(server, envelope)
    assert code == 403 and body["reason"] == cp.REASON_BLOCKED
    assert spy.calls == 0, (
        "a blocked peer must be refused BEFORE authentication -- the "
        "signature verifier must never have been reached")


def test_a_denylisted_peer_is_refused_before_authentication(server, spy):
    """The same, driven by a HAND EDIT rather than an API call: the file
    an operator touches at 2am, with no restart and no route."""
    node = register(server)
    admit(server)
    envelope = envelope_for(server, node, psk_for(server))
    write_denylist(server, {"host_ids": [PEER]})

    code, body = submit_as_peer(server, envelope)
    assert code == 403 and body["reason"] == cp.REASON_BLOCKED
    assert spy.calls == 0


def test_an_unparseable_denylist_refuses_before_authentication(server, spy):
    node = register(server)
    admit(server)
    envelope = envelope_for(server, node, psk_for(server))
    import os
    path = server.app.peers.denylist.path
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("{ truncated")
    old = time.time() - 5
    os.utime(path, (old, old))

    code, body = submit_as_peer(server, envelope)
    assert code == 403 and body["reason"] == cp.REASON_BLOCKED
    assert spy.calls == 0


@pytest.mark.parametrize("host_id,fingerprint,reason", [
    (OTHER, OTHER_FP, cp.REASON_UNKNOWN),
    (PEER, OTHER_FP, cp.REASON_PIN_MISMATCH),
    (OTHER, PEER_FP, cp.REASON_PIN_MISMATCH),
    ("nonsense", PEER_FP, cp.REASON_UNKNOWN),
])
def test_every_peer_refusal_precedes_authentication(server, spy, host_id,
                                                    fingerprint, reason):
    node = register(server)
    admit(server)
    envelope = envelope_for(server, node, psk_for(server))
    code, body = submit_as_peer(server, envelope, host_id, fingerprint)
    assert code == 403 and body["reason"] == reason
    assert spy.calls == 0


def test_a_killswitched_host_refuses_before_authentication(server, spy):
    node = register(server)
    admit(server)
    envelope = envelope_for(server, node, psk_for(server))
    assert server.call("/lan/killswitch", {"engaged": True})[0] == 200
    code, body = submit_as_peer(server, envelope)
    assert code == 403 and body["reason"] == cp.REASON_BLOCKED
    assert spy.calls == 0


def test_a_loopback_envelope_is_unaffected_by_peer_state(server, spy):
    """The local path has no peer origin at all: the IPC token is its
    credential, and blocking every peer must not stop the owner working
    on their own machine."""
    node = register(server)
    write_denylist(server, {"host_ids": [PEER]})
    server.call("/lan/killswitch", {"engaged": True})
    envelope = envelope_for(server, node, psk_for(server))
    code, body = server.call("/envelope", {"envelope": envelope})
    assert code == 200 and body["ok"] is True
    assert spy.calls == 1


def test_an_observe_only_peer_may_read_but_not_mutate(server):
    node = register(server)
    admit(server)
    assert server.call("/peers/observe", {"host_id": PEER})[0] == 200

    read = envelope_for(server, node, psk_for(server),
                        operation="query_network", idempotency_key="r1")
    code, body = submit_as_peer(server, read)
    assert code == 200 and body["ok"] is True

    mutate = envelope_for(server, node, psk_for(server),
                          operation="set_op_position", idempotency_key="m1")
    code, body = submit_as_peer(server, mutate)
    assert code == 403
    assert body["reason"] == cp.REASON_OBSERVE_ONLY
    assert "observe-only" in body["detail"]


def test_observe_only_refuses_regardless_of_local_gate_state(server):
    """24.6: the narrowing is not a preference the local registry can
    override -- an operation the host would happily relay locally is
    still refused for an observe-only peer."""
    node = register(server)
    admit(server)
    server.call("/peers/observe", {"host_id": PEER})
    # prove the local path relays exactly this operation right now
    code, _ = server.call("/jobs", {"idempotency_key": "local",
                                    "node_id": node["node_id"],
                                    "operation": "set_op_position"})
    assert code == 200
    envelope = envelope_for(server, node, psk_for(server),
                            operation="set_op_position",
                            idempotency_key="peer")
    assert submit_as_peer(server, envelope)[0] == 403


# =====================================================================
# THE LOOPBACK ROUTES
# =====================================================================

@pytest.mark.parametrize("path,payload", [
    ("/peers", None),
    ("/peers/admit", {"host_id": PEER, "fingerprint": PEER_FP}),
    ("/peers/block", {"host_id": PEER}),
    ("/peers/forget", {"host_id": PEER}),
    ("/peers/observe", {"host_id": PEER}),
    ("/lan/killswitch", {"engaged": True}),
])
def test_every_peer_route_requires_the_token(server, path, payload):
    code, body = server.call(path, payload, token=None)
    assert code == 401 and body["reason"] == "unauthenticated"


def test_admit_then_list(server):
    admit(server, display_name="booth laptop",
          endpoints=["192.168.88.30:47600"], convoy_ids=[CONVOY])
    code, body = server.call("/peers")
    assert code == 200
    assert [p["host_id"] for p in body["peers"]] == [PEER]
    assert body["peers"][0]["display_name"] == "booth laptop"
    assert body["killswitch"]["engaged"] is False
    assert body["denylist"]["fail_closed"] is False
    assert body["peers_unreadable"] is None


def test_the_peer_lifecycle_over_http(server):
    admit(server)
    assert server.call("/peers/observe", {"host_id": PEER})[1][
        "peer"]["state"] == cp.PEER_OBSERVE_ONLY
    assert server.call("/peers/block", {"host_id": PEER})[1][
        "peer"]["state"] == cp.PEER_BLOCKED
    code, body = server.call("/peers/forget", {"host_id": PEER})
    assert code == 200 and body["outcome"] == "forgotten"
    assert server.call("/peers")[1]["peers"] == []
    code, body = server.call("/peers/block", {"host_id": PEER})
    assert code == 404 and body["reason"] == "unknown_peer"


@pytest.mark.parametrize("path,payload,reason", [
    ("/peers/admit", {"fingerprint": PEER_FP}, "malformed"),
    ("/peers/admit", {"host_id": PEER}, "malformed"),
    ("/peers/admit", {"host_id": "nope", "fingerprint": PEER_FP},
     "malformed_host_id"),
    ("/peers/admit", {"host_id": PEER, "fingerprint": "nope"},
     "malformed_fingerprint"),
    ("/peers/block", {}, "malformed"),
    ("/lan/killswitch", {}, "malformed"),
    ("/lan/killswitch", {"engaged": "yes"}, "malformed"),
])
def test_malformed_peer_requests_are_named_400s(server, path, payload,
                                                reason):
    code, body = server.call(path, payload)
    assert code == 400 and body["reason"] == reason


def test_status_reports_the_peer_picture(server):
    admit(server)
    admit(server, host_id=OTHER, fingerprint=OTHER_FP)
    server.call("/peers/observe", {"host_id": OTHER})
    code, body = server.call("/status")
    assert code == 200
    assert body["peers_total"] == 2 and body["peers_admitted"] == 1
    assert body["lan_killswitch"] is False
    assert body["denylist_fail_closed"] is False
    write_denylist(server, {"host_ids": ["oops"]})
    assert server.call("/status")[1]["denylist_fail_closed"] is True


# =====================================================================
# THE FOUR IN-FLIGHT CASES
# =====================================================================

def test_revocation_terminalises_QUEUED_work_as_refused(server):
    """CASE 1. It provably never ran -- it never left this host -- so
    mark_indeterminate would be a LIE (indeterminate means MAY have
    run). Leaving it queued would make /jobs lie the other way."""
    node = register(server)
    admit(server)
    job = peer_job(server, node, key="q1")
    assert job["state"] == "queued"

    code, body = server.call("/peers/block", {"host_id": PEER})
    assert code == 200 and body["revocation"]["refused"] == 1

    code, body = server.call("/jobs/" + job["delivery_id"])
    assert body["job"]["state"] == "refused"
    assert body["job"]["verdict_source"] == "host"
    assert body["job"]["result"]["reason"] == "peer_revoked"
    assert body["job"]["result"]["origin_host_id"] == PEER
    assert "never ran" in body["job"]["result"]["detail"]


def test_revocation_LEAVES_A_DISPATCHING_JOB_ALONE(server):
    """CASE 2. The forward is in flight: you cannot un-run something.
    Revocation stops NEW work; it does not rewrite history, and the
    dispatcher's own resolution is the honest record."""
    node = register(server)
    admit(server)
    job = peer_job(server, node, key="d1")
    with server.app.lock:
        claimed = server.app.db.claim_for_dispatch(job["delivery_id"])
    assert claimed["state"] == "dispatching"

    code, body = server.call("/peers/block", {"host_id": PEER})
    assert body["revocation"]["left_in_flight"] == 1
    assert body["revocation"]["refused"] == 0

    after = server.call("/jobs/" + job["delivery_id"])[1]["job"]
    assert after["state"] == "dispatching", (
        "an in-flight forward must be left to finish and be recorded "
        "honestly -- terminalising it would invent an outcome")
    assert "peer_revocation_left_in_flight" in audit_events(server)


def test_revocation_KEEPS_POLLING_A_RUNNING_JOB(server):
    """CASE 3. The node owns this job and holds its verdict for 24h.
    Abandoning it manufactures a false indeterminate and destroys a real
    answer that still exists."""
    node = register(server)
    admit(server)
    # Built through the store: an ASYNC operation demands A-22's
    # expected_runtime_id on the submission path, and what is under test
    # here is the revocation of a job already RUNNING on the node.
    with server.app.lock:
        job, _ = server.app.db.create_job(
            "r1", node["node_id"], "run_tests", {}, CONVOY,
            origin_host_id=PEER, controller_id="ctl-peer")
        server.app.db.record_node_verdict(
            job["delivery_id"], "running", node_job_id="job_0000abcd",
            observed_at=100.0)

    code, body = server.call("/peers/block", {"host_id": PEER})
    assert body["revocation"]["left_running"] == 1
    assert body["revocation"]["refused"] == 0

    after = server.call("/jobs/" + job["delivery_id"])[1]["job"]
    assert after["state"] == "running"
    assert after["node_job_id"] == "job_0000abcd"
    assert "peer_revocation_keeps_polling" in audit_events(server)

    # ... and the poll pass still owns it: the node's verdict lands
    server.app.forwarder = lambda p, o, a: {
        "ok": True, "result": {"job_id": "job_0000abcd", "status": "done",
                               "result": {"passed": 12}}}
    server.app.poll_job(job["delivery_id"])
    settled = server.call("/jobs/" + job["delivery_id"])[1]["job"]
    assert settled["state"] == "succeeded", (
        "a revoked peer's RUNNING job must still be polled to a real "
        "terminal -- the verdict belongs to the node, not to the peer")


def test_revocation_RELEASES_LEASES_IMMEDIATELY(server):
    """CASE 4. A revoked peer's exclusive hold must not keep blocking
    every local mutation for the rest of its TTL."""
    node = register(server)
    admit(server)
    peer_job(server, node, key="l1", controller_id="ctl-peer")
    held_as = cp.namespaced_controller(PEER, "ctl-peer")
    code, _ = server.call("/leases", {"controller_id": held_as,
                                      "node_id": node["node_id"],
                                      "mode": "exclusive"})
    assert code == 200
    assert len(server.call("/leases")[1]["leases"]) == 1

    code, body = server.call("/peers/block", {"host_id": PEER})
    assert body["revocation"]["leases_released"] == 1
    assert server.call("/leases")[1]["leases"] == [], (
        "the lease must fall at revocation, not at TTL expiry")
    # and the local operator can mutate again at once
    code, _ = server.call("/jobs", {"idempotency_key": "local-after",
                                    "node_id": node["node_id"],
                                    "operation": "set_op_position",
                                    "controller_id": "ctl-local"})
    assert code == 200


def test_revocation_touches_nobody_elses_work(server):
    node = register(server)
    admit(server)
    admit(server, host_id=OTHER, fingerprint=OTHER_FP)
    mine = server.call("/jobs", {"idempotency_key": "local",
                                 "node_id": node["node_id"],
                                 "operation": "query_network"})[1]["job"]
    theirs = peer_job(server, node, key="other", host_id=OTHER,
                      fingerprint=OTHER_FP, controller_id="ctl-other")
    ours = peer_job(server, node, key="mine")

    server.call("/peers/block", {"host_id": PEER})

    assert server.call("/jobs/" + ours["delivery_id"])[1][
        "job"]["state"] == "refused"
    for untouched in (mine, theirs):
        assert server.call("/jobs/" + untouched["delivery_id"])[1][
            "job"]["state"] == "queued"


def test_forget_revokes_exactly_like_block(server):
    node = register(server)
    admit(server)
    job = peer_job(server, node, key="f1")
    code, body = server.call("/peers/forget", {"host_id": PEER})
    assert code == 200 and body["revocation"]["refused"] == 1
    assert server.call("/jobs/" + job["delivery_id"])[1][
        "job"]["state"] == "refused"


def test_observe_terminalises_only_the_MUTATIONS(server):
    """Narrowing, not revocation: reads keep working, so their queued
    work must survive. Only what can never be dispatched again is
    terminalised."""
    node = register(server)
    admit(server)
    read = peer_job(server, node, operation="query_network", key="o-read")
    write = peer_job(server, node, operation="set_op_position", key="o-write")

    code, body = server.call("/peers/observe", {"host_id": PEER})
    assert code == 200 and body["revocation"]["refused"] == 1

    assert server.call("/jobs/" + write["delivery_id"])[1][
        "job"]["state"] == "refused"
    assert server.call("/jobs/" + read["delivery_id"])[1][
        "job"]["state"] == "queued"


def test_observe_drops_writer_leases_and_keeps_reader_leases(server):
    node = register(server)
    admit(server)
    peer_job(server, node, key="ol", controller_id="ctl-peer")
    held_as = cp.namespaced_controller(PEER, "ctl-peer")
    server.call("/leases", {"controller_id": held_as,
                            "node_id": node["node_id"],
                            "mode": "exclusive"})
    code, body = server.call("/peers/observe", {"host_id": PEER})
    assert body["revocation"]["leases_released"] == 1
    assert server.call("/leases")[1]["leases"] == []

    # a SHARED (read) hold survives the same narrowing
    server.call("/peers/admit", {"host_id": PEER, "fingerprint": PEER_FP,
                                  "convoy_ids": [CONVOY]})
    server.call("/leases", {"controller_id": held_as,
                            "node_id": node["node_id"], "mode": "shared"})
    server.call("/peers/observe", {"host_id": PEER})
    assert len(server.call("/leases")[1]["leases"]) == 1, (
        "an observe-only peer may still read, so its reader hold stands")


# =====================================================================
# DRAIN INTEGRATION
# =====================================================================

def test_drain_skips_a_job_whose_origin_was_denylisted(server):
    """The reversible case: a hand edit, no API call, no restart. The
    job is SKIPPED and paced -- never burnt, because lifting the block
    must bring the work back."""
    node = register(server)
    admit(server)
    job = peer_job(server, node, key="dl")
    server.app.forwarder = lambda p, o, a: {"ok": True, "result": {}}

    write_denylist(server, {"host_ids": [PEER]})
    summary = server.app.drain_once()
    assert summary["deferred"] == 1 and summary["dispatched"] == 0
    assert server.call("/jobs/" + job["delivery_id"])[1][
        "job"]["state"] == "queued", "reversible means NOT terminalised"

    code, body = server.call("/dispatch",
                             {"delivery_id": job["delivery_id"]})
    assert code == 403 and body["reason"] == "origin_not_admitted"
    assert body["peer_reason"] == cp.REASON_BLOCKED

    # lift it, and the same job dispatches
    write_denylist(server, {"host_ids": []})
    with server.app.lock:
        server.app._drain_backoff.clear()
    summary = server.app.drain_once()
    assert summary["dispatched"] == 1
    assert server.call("/jobs/" + job["delivery_id"])[1][
        "job"]["state"] == "succeeded"


def test_drain_skips_every_peer_job_under_the_killswitch(server):
    """A-32: the SAME predicate applied to all peers at once, reversible,
    unwinding no membership."""
    node = register(server)
    admit(server)
    admit(server, host_id=OTHER, fingerprint=OTHER_FP)
    a = peer_job(server, node, key="ks-a")
    b = peer_job(server, node, key="ks-b", host_id=OTHER,
                 fingerprint=OTHER_FP, controller_id="ctl-other")
    mine = server.call("/jobs", {"idempotency_key": "ks-local",
                                 "node_id": node["node_id"],
                                 "operation": "query_network"})[1]["job"]
    server.app.forwarder = lambda p, o, a_: {"ok": True, "result": {}}

    assert server.call("/lan/killswitch",
                       {"engaged": True, "reason": "hostile venue"})[0] == 200
    summary = server.app.drain_once()
    assert summary["deferred"] == 2, "both peers' work is skipped"
    assert summary["dispatched"] == 1, "local work is untouched"

    for job in (a, b):
        assert server.call("/jobs/" + job["delivery_id"])[1][
            "job"]["state"] == "queued", "the killswitch terminalises NOTHING"
    assert server.call("/jobs/" + mine["delivery_id"])[1][
        "job"]["state"] == "succeeded"

    # released: membership was never unwound, so the work simply resumes
    assert server.call("/lan/killswitch", {"engaged": False})[0] == 200
    with server.app.lock:
        server.app._drain_backoff.clear()
    summary = server.app.drain_once()
    assert summary["dispatched"] == 2


def test_a_local_job_is_never_skipped_for_its_origin(server):
    """Absent origin (a pre-origin record) and this host's own id both
    mean local. Neither may be refused, ever."""
    node = register(server)
    server.app.forwarder = lambda p, o, a: {"ok": True, "result": {}}
    with server.app.lock:
        legacy, _ = server.app.db.create_job(
            "legacy", node["node_id"], "query_network", {}, CONVOY)
    write_denylist(server, {"host_ids": [PEER]})
    server.call("/lan/killswitch", {"engaged": True})
    summary = server.app.drain_once()
    assert summary["dispatched"] == 1
    assert server.call("/jobs/" + legacy["delivery_id"])[1][
        "job"]["state"] == "succeeded"


def test_drain_terminalises_a_forgotten_peers_leftover_work(server):
    """A job whose origin has no record at all is never dispatched on the
    strength of having once been admitted -- and forgetting is a
    MEMBERSHIP decision, so the work is terminalised rather than left
    queued for a /jobs listing to lie about."""
    node = register(server)
    admit(server)
    job = peer_job(server, node, key="fg")
    with server.app.lock:
        server.app.db._apply_state(job["delivery_id"], "queued")
    server.call("/peers/forget", {"host_id": PEER})
    with server.app.lock:
        server.app.db._apply_state(job["delivery_id"], "queued")
        server.app._drain_backoff.clear()
    server.app.forwarder = lambda p, o, a: {"ok": True, "result": {}}
    summary = server.app.drain_once()
    assert summary["refused"] == 1 and summary["dispatched"] == 0
    after = server.call("/jobs/" + job["delivery_id"])[1]["job"]
    assert after["state"] == "refused"
    assert after["result"]["peer_reason"] == cp.REASON_UNKNOWN


def test_a_pin_change_stops_a_queued_peers_work(server):
    """The pin is the binding: a re-admission under a NEW key does not
    silently authorize work submitted under the old one."""
    node = register(server)
    admit(server)
    job = peer_job(server, node, key="pin")
    server.app.forwarder = lambda p, o, a: {"ok": True, "result": {}}
    write_denylist(server, {"fingerprints": [PEER_FP]})
    summary = server.app.drain_once()
    assert summary["deferred"] == 1
    assert server.call("/jobs/" + job["delivery_id"])[1][
        "job"]["state"] == "queued"


def test_an_unreadable_peers_file_stops_every_peer_job(server):
    """Fail-closed at the drain, too: a host that cannot read its own
    admission records dispatches no peer work."""
    import os
    node = register(server)
    admit(server)
    job = peer_job(server, node, key="ur")
    server.app.forwarder = lambda p, o, a: {"ok": True, "result": {}}
    with open(os.path.join(server.app.data_dir, cp.PEERS_FILE), "w",
              encoding="utf-8") as f:
        f.write("{ truncated")
    # a fresh store reads the damage, exactly as a restart would
    with server.app.lock:
        server.app.peers = cp.PeerStore(server.app.data_dir)
    summary = server.app.drain_once()
    assert summary["deferred"] == 1
    assert server.call("/jobs/" + job["delivery_id"])[1][
        "job"]["state"] == "queued"


# =====================================================================
# BOOKKEEPING AND AUDIT HYGIENE
# =====================================================================

def test_the_peer_controller_map_is_bounded(server, monkeypatch):
    monkeypatch.setattr(ha, "MAX_PEER_CONTROLLERS", 8)
    with server.app.lock:
        for i in range(200):
            server.app._note_peer_controller(PEER, f"ctl-{i}")
        total = sum(len(v) for v
                    in server.app._peer_controllers.values())
    assert total <= 8


def test_controllers_for_origin_unions_memory_and_the_job_records(server):
    node = register(server)
    admit(server)
    peer_job(server, node, key="c1", controller_id="ctl-from-job")
    with server.app.lock:
        server.app._peer_controllers.clear()      # simulate a restart
        from_disk = server.app._controllers_for_origin(PEER)
        server.app._note_peer_controller(PEER, "ctl-in-memory")
        both = server.app._controllers_for_origin(PEER)
    from_job = cp.namespaced_controller(PEER, "ctl-from-job")
    assert from_disk == {from_job}, (
        "the durable job records are what survives a restart")
    assert both == {from_job, "ctl-in-memory"}


def test_a_failing_audit_never_blocks_a_revocation(server, monkeypatch):
    """An audit is evidence, never a control path: a trail that cannot be
    written must not leave a revoked peer's work dispatchable."""
    node = register(server)
    admit(server)
    job = peer_job(server, node, key="a1")

    def explode(*a, **kw):
        raise OSError("audit.jsonl is on fire")

    monkeypatch.setattr(server.app.db, "audit", explode)
    code, body = server.call("/peers/block", {"host_id": PEER})
    assert code == 200 and body["revocation"]["refused"] == 1
    monkeypatch.undo()
    assert server.call("/jobs/" + job["delivery_id"])[1][
        "job"]["state"] == "refused"


def test_a_repeating_origin_refusal_audits_on_transition_only(server):
    """The drain re-checks every peer job on every pass. Auditing each
    one would let a single revoked peer grow audit.jsonl without bound."""
    node = register(server)
    admit(server)
    peer_job(server, node, key="dedupe")
    write_denylist(server, {"host_ids": [PEER]})
    for _ in range(5):
        with server.app.lock:
            server.app._drain_backoff.clear()
        server.app.drain_once()
    events = audit_events(server)
    assert events.count("dispatch_refused") == 1


def test_a_CHANGED_origin_refusal_reason_is_audited_afresh(server):
    """The other half of the dedupe contract: a steady refusal audits
    once, but a job whose refusal MOVES (denylisted -> forgotten) is a
    genuine transition the trail must show."""
    import os
    node = register(server)
    admit(server)
    peer_job(server, node, key="transition")

    # Two REVERSIBLE refusals in sequence, so the job stays queued under
    # both and the reason really does move: first the store cannot be
    # read at all (peer_unknown), then it is repaired and the peer is
    # hand-denylisted (peer_blocked).
    good = open(os.path.join(server.app.data_dir, cp.PEERS_FILE),
                encoding="utf-8").read()
    with open(os.path.join(server.app.data_dir, cp.PEERS_FILE), "w",
              encoding="utf-8") as f:
        f.write("{ truncated")
    server.app.drain_once()

    with open(os.path.join(server.app.data_dir, cp.PEERS_FILE), "w",
              encoding="utf-8", newline="\n") as f:
        f.write(good)
    old = time.time() - 5
    os.utime(os.path.join(server.app.data_dir, cp.PEERS_FILE), (old, old))
    write_denylist(server, {"host_ids": [PEER]})
    with server.app.lock:
        server.app._drain_backoff.clear()
    server.app.drain_once()

    reasons = []
    with server.app.lock:
        for entry in server.app.db.audit_tail(limit=400):
            if entry["event"] == "dispatch_refused":
                reasons.append(entry["detail"]["peer_reason"])
    assert reasons == [cp.REASON_UNKNOWN, cp.REASON_BLOCKED]


def test_the_peer_store_shares_the_host_data_dir(server):
    """Host-private, beside host.json and identity.key -- never in a git
    tree, a .tox, or op.Embody.Log."""
    import os
    admit(server)
    assert os.path.exists(os.path.join(server.app.data_dir, cp.PEERS_FILE))
    assert server.app.peers.denylist.path == os.path.join(
        server.app.data_dir, cp.DENYLIST_FILE)


def test_release_controller_is_idempotent_and_narrow():
    leases = controllers.LeaseRegistry()
    leases.acquire("n1", "ctl", controllers.LEASE_EXCLUSIVE, 0.0)
    leases.acquire("n2", "ctl", controllers.LEASE_EXCLUSIVE, 0.0)
    leases.acquire("n3", "other", controllers.LEASE_SHARED, 0.0)
    assert leases.release_controller("ctl") == 2
    assert leases.release_controller("ctl") == 0
    assert leases.release_controller("") == 0
    assert len(leases.live_leases(1.0)) == 1, "another holder is untouched"
    # mode-narrowed: a shared hold survives a writer-only release
    assert leases.release_controller("other",
                                     modes=(controllers.LEASE_EXCLUSIVE,)) == 0
    assert leases.release_controller("other",
                                     modes=(controllers.LEASE_SHARED,)) == 1


# =====================================================================
# THE ADMISSION LINEAGE FENCE (stale_admission) -- no resurrection, and
# an ABSENT store is not a membership decision
# =====================================================================

def hide_from_sweep_then_requeue(server, delivery_id, revoke):
    """The exact 'sweep could not reach it' shape: a claimed job is
    invisible to the revocation sweep (in-flight is left alone), and
    re-enters the queue after the sweep has run."""
    with server.app.lock:
        assert server.app.db.claim_for_dispatch(delivery_id) is not None
    revoke()
    with server.app.lock:
        assert server.app.db.release_claim(delivery_id) is not None


def test_pre_revocation_work_never_resurrects_after_readmission(server):
    """Work the sweep could not reach was re-authorized by a later
    re-admission: the record carried no lineage, so the per-dispatch
    fence could not tell pre-revocation work from fresh. Now it can --
    and the refusal REASON is the evidence, never the status class."""
    node = register(server)
    admit(server)
    job = peer_job(server, node, key="stale1")
    hide_from_sweep_then_requeue(
        server, job["delivery_id"],
        lambda: server.call("/peers/block", {"host_id": PEER}))
    admit(server)                        # operator consents to the PEER
    code, body = server.call("/dispatch", {"delivery_id": job["delivery_id"]})
    assert code == 403 and body["reason"] == "origin_revoked"
    assert body["peer_reason"] == "stale_admission"
    after = server.call("/jobs/" + job["delivery_id"])[1]["job"]
    assert after["state"] == "refused"
    assert after["result"]["cause"] == "stale_admission"


def test_fresh_work_after_readmission_still_dispatches(server):
    """The fence must burn ONLY the stale lineage: work submitted under
    the new admission carries the new lineage and dispatches."""
    node = register(server)
    admit(server)
    server.call("/peers/block", {"host_id": PEER})
    admit(server)
    job = peer_job(server, node, key="fresh1")
    server.app.forwarder = lambda p, o, a: {"ok": True, "result": {}}
    code, body = server.call("/dispatch", {"delivery_id": job["delivery_id"]})
    assert code == 200 and body["job"]["state"] == "succeeded", body


def test_repin_readmission_does_not_resurrect_missed_work(server):
    """Same fence, re-pin shape: the old key's work that the re-pin
    sweep could not reach must stay dead under the NEW key."""
    node = register(server)
    admit(server)
    job = peer_job(server, node, key="stale2")
    hide_from_sweep_then_requeue(
        server, job["delivery_id"],
        lambda: admit(server, fingerprint=OTHER_FP))   # the re-pin
    code, body = server.call("/dispatch", {"delivery_id": job["delivery_id"]})
    assert code == 403 and body["peer_reason"] == "stale_admission"


def test_observe_narrowing_does_not_strand_read_work(server):
    """Narrow -> widen is the same unbroken lineage: a READ submitted
    while admitted keeps dispatching under observe-only and after the
    widening -- the whole point of the observe state."""
    node = register(server)
    admit(server)
    job = peer_job(server, node, key="read1", operation="query_network")
    server.app.forwarder = lambda p, o, a: {"ok": True, "result": {}}
    server.call("/peers/observe", {"host_id": PEER})
    code, body = server.call("/dispatch", {"delivery_id": job["delivery_id"]})
    assert code == 200 and body["job"]["state"] == "succeeded", (
        "a read submitted while admitted must survive the narrowing")
    admit(server)                        # widen back
    job2 = peer_job(server, node, key="read2", operation="query_network")
    code, body = server.call("/dispatch", {"delivery_id": job2["delivery_id"]})
    assert code == 200 and body["job"]["state"] == "succeeded", body


def test_an_absent_peers_file_defers_queued_peer_work(server):
    """An ABSENT peers.json used to BURN queued peer work while an
    UNREADABLE one deferred it -- but a deleted or AV-quarantined file
    looks absent, and queued peer work proves an admission was once
    granted. Absence now defers (reversible), and restoring the file
    brings the work back."""
    import os
    node = register(server)
    admit(server)
    job = peer_job(server, node, key="abs1")
    saved = open(server.app.peers.path, encoding="utf-8").read()
    os.remove(server.app.peers.path)
    server.app.peers._signature = ("re-read",)
    code, body = server.call("/dispatch", {"delivery_id": job["delivery_id"]})
    assert code == 403 and body["reason"] == "origin_not_admitted", body
    after = server.call("/jobs/" + job["delivery_id"])[1]["job"]
    assert after["state"] == "queued", (
        "a deleted store must DEFER, not burn: %r" % after["state"])
    # restore -> the exact same job dispatches
    with open(server.app.peers.path, "w", encoding="utf-8",
              newline="\n") as f:
        f.write(saved)
    server.app.peers._signature = ("re-read",)
    server.app.forwarder = lambda p, o, a: {"ok": True, "result": {}}
    code, body = server.call("/dispatch", {"delivery_id": job["delivery_id"]})
    assert code == 200 and body["job"]["state"] == "succeeded", body


def test_block_observe_readmit_does_not_resurrect_unreachable_work(server):
    """Panel BLOCKER (spec-fidelity lens), end to end. The exact bypass:
    a job unreachable during the block sweep, then the block laundered
    through observe-only before a re-admit. The durable block epoch must
    keep that job dead at the dispatch fence."""
    node = register(server)
    admit(server)
    job = peer_job(server, node, key="laundered1")
    # Keep it CLAIMED (dispatching) through BOTH the block sweep and the
    # observe sweep -- the faithful "unreachable during the sweep" case
    # the fence exists for. Release to queued only after the re-admit, so
    # nothing but the lineage fence can decide its fate.
    with server.app.lock:
        assert server.app.db.claim_for_dispatch(job["delivery_id"]) is not None
    server.call("/peers/block", {"host_id": PEER})       # mints the epoch
    server.call("/peers/observe", {"host_id": PEER})     # launder the state
    server.call("/peers/admit", {"host_id": PEER, "fingerprint": PEER_FP,
                                  "convoy_ids": [CONVOY]})
    with server.app.lock:
        assert server.app.db.release_claim(job["delivery_id"]) is not None
    code, body = server.call("/dispatch", {"delivery_id": job["delivery_id"]})
    assert code == 403 and body["peer_reason"] == "stale_admission", (
        "block -> observe -> admit resurrected pre-block work: %s" % body)
    after = server.call("/jobs/" + job["delivery_id"])[1]["job"]
    assert after["state"] == "refused"


def test_a_legacy_lineage_peer_survives_a_benign_readmit(server):
    """Panel MAJOR (edge-cases lens), end to end. A peer whose record
    predates admission_id keeps dispatching its None-lineage work across
    a routine re-affirm -- only a real revocation burns it."""
    import json as _json
    node = register(server)
    admit(server)
    # Downgrade the live record to a pre-fix (None-lineage) one on disk.
    ppath = server.app.peers.path
    data = _json.load(open(ppath, encoding="utf-8"))
    data["peers"][PEER].pop("admission_id", None)
    _json.dump(data, open(ppath, "w", encoding="utf-8"))
    server.app.peers._signature = ("re-read",)
    job = peer_job(server, node, key="legacy1")
    assert server.app.db.get_job(job["delivery_id"])["origin_admission_id"] \
        is None
    # a routine re-affirm must NOT strand it
    server.call("/peers/admit", {"host_id": PEER, "fingerprint": PEER_FP,
                                  "convoy_ids": [CONVOY]})
    server.app.forwarder = lambda p, o, a: {"ok": True, "result": {}}
    code, body = server.call("/dispatch", {"delivery_id": job["delivery_id"]})
    assert code == 200 and body["job"]["state"] == "succeeded", (
        "a benign re-affirm burned a legacy peer's legitimate work: %s"
        % body)
