"""Automatic trusted-LAN discovery: wire, TOFU and transport lifecycle."""

import json
import os
import socket

import pytest

import convoy_discovery as cd
import convoy_hostkeys as hk
import convoy_peers as peers


LOCAL_HOST = "1" * 32
REMOTE_HOST = "2" * 32
OTHER_HOST = "3" * 32
LOCAL_IP = "192.168.50.10"
REMOTE_IP = "192.168.50.20"
NOW = 1_800_000_000.0
GEN_A = "a" * 32
GEN_B = "b" * 32
NONCE_A = "c" * 32
NONCE_B = "d" * 32


class Clock:
    def __init__(self, wall=NOW, mono=100.0):
        self.wall = wall
        self.mono = mono

    def time(self):
        return self.wall

    def monotonic(self):
        return self.mono

    def advance(self, seconds):
        self.wall += seconds
        self.mono += seconds


@pytest.fixture
def local_keys(tmp_path):
    path = tmp_path / "local-keys"
    path.mkdir()
    return hk.load_or_create(str(path))


@pytest.fixture
def remote_keys(tmp_path):
    path = tmp_path / "remote-keys"
    path.mkdir()
    return hk.load_or_create(str(path))


@pytest.fixture
def other_keys(tmp_path):
    path = tmp_path / "other-keys"
    path.mkdir()
    return hk.load_or_create(str(path))


@pytest.fixture
def store(tmp_path):
    path = tmp_path / "peers"
    path.mkdir()
    return peers.PeerStore(str(path), now=lambda: NOW)


def packet(keys, host_id=REMOTE_HOST, ip=REMOTE_IP, namespaces=("studio",),
           generation=GEN_A, nonce=NONCE_A, now=NOW, port=47600,
           realm_states=None):
    return cd.build_announcement(
        host_id, keys, ip, port, namespaces, generation,
        now=now, nonce=nonce, realm_states=realm_states)


def resign(raw, keys, mutate):
    value = json.loads(raw.decode("utf-8"))
    mutate(value["body"])
    value["signature"] = keys.signer().sign(
        cd.DISCOVERY_DOMAIN_TAG + cd._canonical_json(value["body"]))
    return cd._canonical_json(value)


def coordinator(store, active, clock=None, host_id=LOCAL_HOST,
                ttl=cd.CANDIDATE_TTL_S):
    clock = clock or Clock()
    return cd.DiscoveryCoordinator(
        host_id, store, lambda: tuple(active), now=clock.time,
        candidate_ttl_s=ttl)


def test_round_trip_authenticates_every_required_field(remote_keys):
    raw = packet(remote_keys, namespaces=("backup", "studio"))
    result = cd.parse_announcement(raw, (REMOTE_IP, 55123), now=NOW)
    assert result["kind"] == cd.DISCOVERY_KIND
    assert result["version"] == 1
    assert result["protocol"] == "convoy/1"
    assert result["host_id"] == REMOTE_HOST
    assert result["fingerprint"] == remote_keys.fingerprint
    assert result["endpoint"] == {"address": REMOTE_IP, "port": 47600}
    assert result["convoy_ids"] == ["backup", "studio"]
    assert result["realm_states"] == {
        "backup": "established", "studio": "established"}
    assert result["generation"] == GEN_A
    assert result["nonce"] == NONCE_A
    assert result["timestamp_ms"] == int(NOW * 1000)


def test_wire_is_compact_bounded_json_and_contains_no_private_key(remote_keys):
    raw = packet(remote_keys)
    assert len(raw) < cd.MAX_DATAGRAM_BYTES
    assert b"PRIVATE KEY" not in raw
    value = json.loads(raw)
    assert set(value) == {"body", "signature"}
    assert "certificate_pem" in value["body"]


def test_wire_classifies_uncommitted_candidate_realms(remote_keys):
    raw = packet(remote_keys, realm_states={"studio": "candidate"})
    parsed = cd.parse_announcement(raw, (REMOTE_IP, 1), now=NOW)
    assert parsed["realm_states"] == {"studio": "candidate"}


def test_legacy_signed_announcement_is_safely_established(remote_keys):
    raw = packet(remote_keys)
    legacy = resign(raw, remote_keys,
                    lambda body: body.pop("realm_states"))
    parsed = cd.parse_announcement(legacy, (REMOTE_IP, 1), now=NOW)
    assert parsed["realm_states"] == {"studio": "established"}


def test_realm_classification_is_closed_and_signed(remote_keys):
    malformed = resign(
        packet(remote_keys), remote_keys,
        lambda body: body.__setitem__("realm_states",
                                      {"studio": "conflict"}))
    with pytest.raises(cd.DiscoveryError, match="malformed_realms"):
        cd.parse_announcement(malformed, (REMOTE_IP, 1), now=NOW)


def test_signature_is_domain_separated_from_ordinary_envelope_payloads(
        remote_keys):
    raw = packet(remote_keys)
    value = json.loads(raw)
    value["signature"] = remote_keys.signer().sign(
        cd._canonical_json(value["body"]))
    with pytest.raises(cd.DiscoveryError, match="bad_signature"):
        cd.parse_announcement(cd._canonical_json(value),
                              (REMOTE_IP, 1), now=NOW)


def test_tamper_is_rejected(remote_keys):
    raw = json.loads(packet(remote_keys))
    raw["body"]["endpoint"]["port"] += 1
    with pytest.raises(cd.DiscoveryError, match="bad_signature"):
        cd.parse_announcement(cd._canonical_json(raw),
                              (REMOTE_IP, 1), now=NOW)


def test_certificate_spki_must_equal_advertised_fingerprint(
        remote_keys, other_keys):
    raw = resign(packet(remote_keys), remote_keys,
                 lambda body: body.__setitem__(
                     "fingerprint", other_keys.fingerprint))
    with pytest.raises(cd.DiscoveryError, match="fingerprint_mismatch"):
        cd.parse_announcement(raw, (REMOTE_IP, 1), now=NOW)


def test_udp_source_must_equal_advertised_tls_address(remote_keys):
    with pytest.raises(cd.DiscoveryError, match="source_mismatch"):
        cd.parse_announcement(packet(remote_keys),
                              ("192.168.50.99", 1), now=NOW)


@pytest.mark.parametrize("ip", [
    "0.0.0.0", "127.0.0.1", "169.254.2.4", "239.1.2.3",
    "255.255.255.255",
])
def test_unsafe_listener_addresses_are_never_announced(remote_keys, ip):
    with pytest.raises(cd.DiscoveryError, match="unsafe_endpoint"):
        packet(remote_keys, ip=ip)


def test_packet_size_is_checked_before_json_parse():
    with pytest.raises(cd.DiscoveryError, match="packet_too_large"):
        cd.parse_announcement(b"{" * (cd.MAX_DATAGRAM_BYTES + 1),
                              (REMOTE_IP, 1), now=NOW)


def test_unknown_top_level_and_body_fields_are_refused(remote_keys):
    value = json.loads(packet(remote_keys))
    value["extra"] = True
    with pytest.raises(cd.DiscoveryError, match="malformed_packet"):
        cd.parse_announcement(cd._canonical_json(value),
                              (REMOTE_IP, 1), now=NOW)
    raw = resign(packet(remote_keys), remote_keys,
                 lambda body: body.__setitem__("extra", True))
    with pytest.raises(cd.DiscoveryError, match="malformed_body"):
        cd.parse_announcement(raw, (REMOTE_IP, 1), now=NOW)


@pytest.mark.parametrize("delta", [-121.0, 121.0])
def test_stale_and_future_announcements_are_refused(remote_keys, delta):
    raw = packet(remote_keys, now=NOW + delta)
    with pytest.raises(cd.DiscoveryError, match="timestamp_out_of_window"):
        cd.parse_announcement(raw, (REMOTE_IP, 1), now=NOW)


def test_namespace_list_must_be_nonempty_sorted_unique_and_canonical(
        remote_keys):
    for values in ([], ["z", "a"], ["studio", "studio"], [" bad"]):
        raw = resign(packet(remote_keys), remote_keys,
                     lambda body, values=values:
                     body.__setitem__("convoy_ids", values))
        with pytest.raises(cd.DiscoveryError, match="malformed_namespaces"):
            cd.parse_announcement(raw, (REMOTE_IP, 1), now=NOW)


def test_malformed_cert_signature_nonce_generation_and_port_are_named(
        remote_keys):
    mutations = [
        ("malformed_certificate",
         lambda body: body.__setitem__("certificate_pem", "not a cert")),
        ("malformed_nonce",
         lambda body: body.__setitem__("nonce", "x")),
        ("malformed_generation",
         lambda body: body.__setitem__("generation", "x")),
        ("malformed_endpoint",
         lambda body: body["endpoint"].__setitem__("port", True)),
    ]
    for reason, mutation in mutations:
        raw = resign(packet(remote_keys), remote_keys, mutation)
        with pytest.raises(cd.DiscoveryError, match=reason):
            cd.parse_announcement(raw, (REMOTE_IP, 1), now=NOW)

    value = json.loads(packet(remote_keys))
    value["signature"] = value["signature"].upper()
    with pytest.raises(cd.DiscoveryError, match="malformed_signature"):
        cd.parse_announcement(cd._canonical_json(value),
                              (REMOTE_IP, 1), now=NOW)


def test_replay_cache_rejects_duplicate_nonce(remote_keys):
    replay = cd.ReplayCache()
    raw = packet(remote_keys)
    cd.parse_announcement(raw, (REMOTE_IP, 1), now=NOW,
                          replay_cache=replay)
    with pytest.raises(cd.DiscoveryError, match="replay"):
        cd.parse_announcement(raw, (REMOTE_IP, 1), now=NOW,
                              replay_cache=replay)


def test_generation_rollover_retires_the_old_run(remote_keys):
    replay = cd.ReplayCache()
    first = packet(remote_keys, generation=GEN_A, nonce=NONCE_A, now=NOW)
    second = packet(remote_keys, generation=GEN_B, nonce=NONCE_B,
                    now=NOW + 1)
    delayed = packet(remote_keys, generation=GEN_A, nonce="e" * 32,
                     now=NOW + 2)
    cd.parse_announcement(first, (REMOTE_IP, 1), now=NOW,
                          replay_cache=replay)
    cd.parse_announcement(second, (REMOTE_IP, 1), now=NOW + 1,
                          replay_cache=replay)
    with pytest.raises(cd.DiscoveryError, match="retired_generation"):
        cd.parse_announcement(delayed, (REMOTE_IP, 1), now=NOW + 2,
                              replay_cache=replay)


def test_stranger_key_cannot_poison_pinned_keys_replay_stream(
        remote_keys, other_keys):
    replay = cd.ReplayCache()
    cd.parse_announcement(packet(remote_keys), (REMOTE_IP, 1), now=NOW,
                          replay_cache=replay)
    # Same claimed host id, different valid key/generation.  Replay accepts a
    # separate cryptographic stream so the durable pin layer can surface it.
    cd.parse_announcement(packet(other_keys, generation=GEN_B,
                                 nonce=NONCE_B),
                          (REMOTE_IP, 1), now=NOW, replay_cache=replay)
    later = packet(remote_keys, nonce="e" * 32, now=NOW + 1)
    cd.parse_announcement(later, (REMOTE_IP, 1), now=NOW + 1,
                          replay_cache=replay)


def test_no_enabled_namespace_means_no_parse_and_no_admission(
        store, remote_keys):
    active = []
    coord = coordinator(store, active)
    # Even malformed bytes are ignored while disabled; the gate precedes work.
    result = coord.handle_datagram(b"not-json", (REMOTE_IP, 1))
    assert result["status"] == "inactive"
    assert store.peers() == []


def test_disjoint_namespace_is_visible_but_not_admitted(store, remote_keys):
    coord = coordinator(store, ["local"])
    result = coord.handle_datagram(
        packet(remote_keys, namespaces=("remote",)), (REMOTE_IP, 1))
    assert result["ok"] is False
    assert result["status"] == "namespace_conflict"
    assert result["intersection"] == []
    assert store.get(REMOTE_HOST) is None
    assert coord.candidates()[0]["status"] == "namespace_conflict"


def test_matching_candidates_are_visible_but_not_admitted(store, remote_keys):
    coord = cd.DiscoveryCoordinator(
        LOCAL_HOST, store, lambda: ("studio",), now=lambda: NOW,
        active_realm_states=lambda: {"studio": "candidate"})
    result = coord.handle_datagram(
        packet(remote_keys, realm_states={"studio": "candidate"}),
        (REMOTE_IP, 1))
    assert result["status"] == "genesis_pending"
    assert store.get(REMOTE_HOST) is None


def test_realm_observer_can_adopt_established_before_tofu(
        store, remote_keys):
    local = {"id": "fresh", "state": "candidate"}
    observations = []

    def observe(announcement):
        observations.append(dict(announcement["realm_states"]))
        local["id"] = "studio"
        local["state"] = "established"

    coord = cd.DiscoveryCoordinator(
        LOCAL_HOST, store, lambda: (local["id"],), now=lambda: NOW,
        active_realm_states=lambda: {local["id"]: local["state"]},
        realm_observer=observe)
    result = coord.handle_datagram(packet(remote_keys), (REMOTE_IP, 1))
    assert observations == [{"studio": "established"}]
    assert result["status"] == "admitted"
    assert store.get(REMOTE_HOST)["convoy_ids"] == ["studio"]


def test_established_host_does_not_admit_later_candidate(store, remote_keys):
    coord = cd.DiscoveryCoordinator(
        LOCAL_HOST, store, lambda: ("studio",), now=lambda: NOW,
        active_realm_states=lambda: {"studio": "established"})
    result = coord.handle_datagram(
        packet(remote_keys, realm_states={"studio": "candidate"}),
        (REMOTE_IP, 1))
    assert result["status"] == "genesis_pending"
    assert store.get(REMOTE_HOST) is None


def test_first_intersection_performs_durable_tofu_with_only_shared_grants(
        store, remote_keys):
    coord = coordinator(store, ["private-local", "studio"])
    result = coord.handle_datagram(
        packet(remote_keys, namespaces=("remote-only", "studio")),
        (REMOTE_IP, 1))
    assert result["ok"] is True
    assert result["status"] == "admitted"
    record = store.get(REMOTE_HOST)
    assert record["state"] == "admitted"
    assert record["fingerprint"] == remote_keys.fingerprint
    assert record["convoy_ids"] == ["studio"]
    assert record["endpoints"] == [f"{REMOTE_IP}:47600"]
    assert record["cert_pem"] == remote_keys.certificate_pem.strip()
    assert record["admitted_via"] == "lan_tofu"


def test_same_pinned_peer_refreshes_endpoint_and_unions_new_intersection(
        store, remote_keys):
    active = ["a", "b"]
    coord = coordinator(store, active)
    coord.handle_datagram(
        packet(remote_keys, namespaces=("a",), nonce=NONCE_A),
        (REMOTE_IP, 1))
    raw = packet(remote_keys, ip="192.168.50.21", namespaces=("b",),
                 nonce=NONCE_B, now=NOW + 1)
    result = coord.handle_datagram(raw, ("192.168.50.21", 1))
    assert result["status"] == "admitted"
    record = store.get(REMOTE_HOST)
    assert record["convoy_ids"] == ["a", "b"]
    assert record["endpoints"] == [
        "192.168.50.21:47600", f"{REMOTE_IP}:47600"]


def test_unchanged_heartbeat_does_not_rewrite_durable_peer_store(
        store, remote_keys):
    active = ["studio"]
    coord = coordinator(store, active)
    calls = []
    original_admit = store.admit

    def counted_admit(*args, **kwargs):
        calls.append((args, kwargs))
        return original_admit(*args, **kwargs)

    store.admit = counted_admit
    coord.handle_datagram(packet(remote_keys), (REMOTE_IP, 1))
    coord.handle_datagram(
        packet(remote_keys, nonce=NONCE_B, now=NOW + 1),
        (REMOTE_IP, 1))
    assert len(calls) == 1


def test_full_endpoint_list_is_never_silently_truncated(store, remote_keys):
    existing = [f"192.168.60.{number}:47600" for number in range(1, 17)]
    store.admit(REMOTE_HOST, remote_keys.fingerprint,
                endpoints=existing, convoy_ids=["studio"],
                cert_pem=remote_keys.certificate_pem)
    coord = coordinator(store, ["studio"])
    result = coord.handle_datagram(packet(remote_keys), (REMOTE_IP, 1))
    assert result["status"] == "endpoint_conflict"
    assert store.get(REMOTE_HOST)["endpoints"] == existing


def test_fingerprint_change_fails_closed_and_never_updates_the_pin(
        store, remote_keys, other_keys):
    coord = coordinator(store, ["studio"])
    coord.handle_datagram(packet(remote_keys), (REMOTE_IP, 1))
    result = coord.handle_datagram(
        packet(other_keys, generation=GEN_B, nonce=NONCE_B),
        (REMOTE_IP, 1))
    assert result["ok"] is False
    assert result["status"] == "pin_mismatch"
    assert store.get(REMOTE_HOST)["fingerprint"] == remote_keys.fingerprint
    statuses = {item["status"] for item in coord.candidates()}
    assert statuses == {"admitted", "pin_mismatch"}


@pytest.mark.parametrize("narrow,expected", [
    ("block", "blocked"),
    ("observe", "observe_only"),
])
def test_discovery_never_widens_an_operator_block_or_narrowing(
        store, remote_keys, narrow, expected):
    coord = coordinator(store, ["studio"])
    coord.handle_datagram(packet(remote_keys), (REMOTE_IP, 1))
    getattr(store, narrow)(REMOTE_HOST)
    result = coord.handle_datagram(
        packet(remote_keys, nonce=NONCE_B, now=NOW + 1),
        (REMOTE_IP, 1))
    assert result["status"] == expected
    assert store.get(REMOTE_HOST)["state"] == expected


def test_killswitch_prevents_quiet_membership_building(store, remote_keys):
    store.set_killswitch(True, "incident")
    coord = coordinator(store, ["studio"])
    result = coord.handle_datagram(packet(remote_keys), (REMOTE_IP, 1))
    assert result["status"] == "killswitch"
    assert store.get(REMOTE_HOST) is None


def test_human_denylist_precedes_automatic_tofu(store, remote_keys):
    with open(store.denylist.path, "w", encoding="utf-8") as handle:
        json.dump({"version": 1, "host_ids": [REMOTE_HOST]}, handle)
    coord = coordinator(store, ["studio"])
    result = coord.handle_datagram(packet(remote_keys), (REMOTE_IP, 1))
    assert result["status"] == "blocked"
    assert store.get(REMOTE_HOST) is None


def test_malformed_denylist_fails_closed_before_automatic_tofu(
        store, remote_keys):
    with open(store.denylist.path, "w", encoding="utf-8") as handle:
        handle.write('{"host_id": "typo"}')
    coord = coordinator(store, ["studio"])
    result = coord.handle_datagram(packet(remote_keys), (REMOTE_IP, 1))
    assert result["status"] == "blocked"
    assert store.get(REMOTE_HOST) is None


def test_fingerprint_already_owned_by_another_host_fails_closed(
        store, remote_keys):
    store.admit(OTHER_HOST, remote_keys.fingerprint,
                endpoints=["192.168.50.30:47600"], convoy_ids=["studio"],
                cert_pem=remote_keys.certificate_pem)
    coord = coordinator(store, ["studio"])
    result = coord.handle_datagram(packet(remote_keys), (REMOTE_IP, 1))
    assert result["status"] == "fingerprint_conflict"
    assert store.get(REMOTE_HOST) is None


def test_self_announcement_is_ignored(store, remote_keys):
    coord = coordinator(store, ["studio"], host_id=REMOTE_HOST)
    result = coord.handle_datagram(packet(remote_keys), (REMOTE_IP, 1))
    assert result["status"] == "self"
    assert store.peers() == []


def test_candidates_are_sanitized_and_expire(store, remote_keys):
    clock = Clock()
    coord = coordinator(store, ["studio"], clock=clock, ttl=5.0)
    coord.handle_datagram(packet(remote_keys), (REMOTE_IP, 1))
    candidate = coord.candidates()[0]
    assert "certificate_pem" not in candidate
    assert "signature" not in candidate
    clock.advance(5.1)
    assert coord.candidates() == []


class FakeSocket:
    def __init__(self, recv=None, fail_send=None, fail_recv=None):
        self.options = []
        self.binds = []
        self.sent = []
        self.recv = list(recv or [])
        self.fail_send = fail_send
        self.fail_recv = fail_recv
        self.blocking = None
        self.closed = False

    def setsockopt(self, *args):
        self.options.append(args)

    def bind(self, address):
        self.binds.append(address)

    def setblocking(self, value):
        self.blocking = value

    def sendto(self, data, target):
        if self.fail_send:
            raise self.fail_send
        self.sent.append((data, target))
        return len(data)

    def recvfrom(self, _size):
        if self.fail_recv:
            raise self.fail_recv
        if not self.recv:
            raise BlockingIOError()
        return self.recv.pop(0)

    def close(self):
        self.closed = True


class FakeFactory:
    def __init__(self, receiver=None, sender=None, failures=0):
        self.receiver = receiver or FakeSocket()
        self.sender = sender or FakeSocket()
        self.failures = failures
        self.calls = []
        self.created = 0

    def __call__(self, family, kind, protocol):
        self.calls.append((family, kind, protocol))
        if self.failures:
            self.failures -= 1
            raise OSError("injected socket failure")
        self.created += 1
        return self.receiver if self.created % 2 == 1 else self.sender


def service(store, local_keys, active, clock, factory, platform_name="linux",
            endpoint=None):
    coord = coordinator(store, active, clock=clock)
    endpoint = endpoint or (lambda: (LOCAL_IP, 47600))
    return cd.DiscoveryService(
        coord, local_keys, endpoint, LOCAL_IP, socket_factory=factory,
        wall_clock=clock.time, monotonic=clock.monotonic,
        random_float=lambda: 0.5, nonce_factory=lambda: NONCE_A,
        generation=GEN_A, platform_name=platform_name)


def test_disabled_service_opens_no_socket_or_packet(store, local_keys):
    clock = Clock()
    factory = FakeFactory()
    svc = service(store, local_keys, [], clock, factory)
    assert svc.step() == cd.INACTIVE_POLL_S
    assert factory.calls == []
    assert svc.status()["active"] is False


@pytest.mark.parametrize("platform_name,expected_receive_bind", [
    ("linux", cd.MULTICAST_GROUP),
    ("darwin", cd.MULTICAST_GROUP),
    ("win32", LOCAL_IP),
])
def test_activation_binds_only_concrete_addresses_and_announces(
        store, local_keys, platform_name, expected_receive_bind):
    clock = Clock()
    factory = FakeFactory()
    svc = service(store, local_keys, ["studio"], clock, factory,
                  platform_name=platform_name)
    delay = svc.step()
    assert 0 < delay <= cd.MAX_STEP_SLEEP_S
    assert factory.receiver.binds == [
        (expected_receive_bind, cd.MULTICAST_PORT)]
    assert factory.sender.binds == [(LOCAL_IP, 0)]
    assert all(bind[0] != "0.0.0.0"
               for bind in factory.receiver.binds + factory.sender.binds)
    assert factory.receiver.blocking is False
    assert factory.sender.sent[0][1] == (
        cd.MULTICAST_GROUP, cd.MULTICAST_PORT)
    parsed = cd.parse_announcement(
        factory.sender.sent[0][0], (LOCAL_IP, 9999), now=NOW)
    assert parsed["host_id"] == LOCAL_HOST
    assert parsed["convoy_ids"] == ["studio"]


def test_disable_closes_both_sockets_immediately_on_next_step(
        store, local_keys):
    active = ["studio"]
    clock = Clock()
    factory = FakeFactory()
    svc = service(store, local_keys, active, clock, factory)
    svc.step()
    active.clear()
    svc.step()
    assert factory.receiver.closed is True
    assert factory.sender.closed is True
    assert svc.active is False


def test_socket_failure_uses_exponential_backoff_then_reconnects(
        store, local_keys):
    clock = Clock()
    factory = FakeFactory(failures=1)
    svc = service(store, local_keys, ["studio"], clock, factory)
    assert svc.step() == pytest.approx(cd.BACKOFF_INITIAL_S)
    assert len(factory.calls) == 1
    clock.advance(cd.BACKOFF_INITIAL_S - 0.01)
    svc.step()
    assert len(factory.calls) == 1
    clock.advance(0.01)
    svc.step()
    assert len(factory.calls) == 3  # failed receiver + receiver + sender
    assert svc.active is True
    assert svc.status()["failures"] == 0


def test_repeated_send_failure_also_backs_off_exponentially(
        store, local_keys):
    clock = Clock()
    factory = FakeFactory(sender=FakeSocket(fail_send=OSError("no route")))
    svc = service(store, local_keys, ["studio"], clock, factory)
    assert svc.step() == pytest.approx(cd.BACKOFF_INITIAL_S)
    assert svc.status()["failures"] == 1
    clock.advance(cd.BACKOFF_INITIAL_S)
    assert svc.step() == pytest.approx(cd.BACKOFF_INITIAL_S * 2)
    assert svc.status()["failures"] == 2


def test_listener_interface_mismatch_never_opens_discovery(
        store, local_keys):
    clock = Clock()
    factory = FakeFactory()
    svc = service(store, local_keys, ["studio"], clock, factory,
                  endpoint=lambda: ("192.168.60.10", 47600))
    svc.step()
    assert factory.calls == []
    assert "listener_address_mismatch" in svc.status()["last_error"]


def test_live_identity_rotation_publishes_new_pin_and_generation(
        store, local_keys, other_keys):
    clock = Clock()
    factory = FakeFactory()
    svc = service(store, local_keys, ["studio"], clock, factory)
    svc.step()
    fresh_generation = svc.replace_identity(other_keys, generation=GEN_B)
    clock.advance(0.01)
    svc.step()
    assert fresh_generation == GEN_B
    second = cd.parse_announcement(
        factory.sender.sent[-1][0], (LOCAL_IP, 1), now=clock.wall)
    assert second["fingerprint"] == other_keys.fingerprint
    assert second["generation"] == GEN_B


def test_receive_path_verifies_and_admits_remote_peer(
        store, local_keys, remote_keys):
    incoming = packet(remote_keys)
    receiver = FakeSocket(recv=[(incoming, (REMOTE_IP, 55123))])
    factory = FakeFactory(receiver=receiver)
    clock = Clock()
    svc = service(store, local_keys, ["studio"], clock, factory)
    svc.step()
    assert store.get(REMOTE_HOST)["fingerprint"] == remote_keys.fingerprint
    assert svc.status()["last_result"]["status"] == "admitted"


def test_receive_failure_closes_transport_and_schedules_retry(
        store, local_keys):
    receiver = FakeSocket(fail_recv=OSError("interface disappeared"))
    factory = FakeFactory(receiver=receiver)
    clock = Clock()
    svc = service(store, local_keys, ["studio"], clock, factory)
    assert svc.step() == pytest.approx(cd.BACKOFF_INITIAL_S)
    assert receiver.closed is True
    assert factory.sender.closed is True
    assert svc.status()["failures"] == 1


def test_each_step_bounds_receive_work(store, local_keys):
    receiver = FakeSocket(recv=[(b"bad", (REMOTE_IP, 1))]
                          * (cd.MAX_RECEIVES_PER_STEP + 5))
    factory = FakeFactory(receiver=receiver)
    clock = Clock()
    svc = service(store, local_keys, ["studio"], clock, factory)
    svc.step()
    assert len(receiver.recv) == 5


def test_generation_churn_is_bounded_and_fails_closed():
    replay = cd.ReplayCache()
    for number in range(cd.MAX_RETIRED_GENERATIONS + 1):
        replay.accept(
            REMOTE_HOST, "fingerprint-stream", f"{number:032x}",
            f"{number:032x}", int((NOW + number / 1000.0) * 1000), NOW)
    with pytest.raises(cd.DiscoveryError, match="generation_churn"):
        replay.accept(
            REMOTE_HOST, "fingerprint-stream",
            f"{cd.MAX_RETIRED_GENERATIONS + 1:032x}", "f" * 32,
            int((NOW + 1) * 1000), NOW)


def test_start_stop_wrapper_is_idempotent_and_leaves_no_socket(
        store, local_keys):
    factory = FakeFactory()
    svc = service(store, local_keys, [], Clock(), factory)
    assert svc.start() is True
    assert svc.start() is False
    assert svc.stop() is True
    assert svc.active is False
