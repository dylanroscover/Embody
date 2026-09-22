"""Advisories (2026-09-21): the facts that lived only in audit.jsonl.

A latched realm conflict, a re-minted identity, peers refusing this
host's certificate, pinned peers whose identity changed, and a LAN
listener on a tunnel or public network now ride /status and the
register answer as {kind, text, ...}; genesis consults the admitted
peers on record instead of founding a realm of one beside them; and an
admitted peer advertising a realm this host recorded it in latches a
conflict instead of being logged as an "un-admitted" stranger.
"""

import io
import os
import re
import sys
import time

import pytest

import convoy_hostapp as ha
import convoy_hostkeys as hostkeys
import convoy_lan as lan
import convoy_peerserver as peerserver
import convoy_protocol as protocol
from test_convoy_network_nodes import (  # noqa: F401 -- pytest fixtures
    CONVOY_A, CONVOY_B, DISC_A, _admit_peer, _register, app, server)


_ETHERNET = {"name": "Ethernet 2",
             "description": "Intel(R) I211 Gigabit Network Connection",
             "kind": lan.ADAPTER_PHYSICAL, "addresses": ["192.168.88.10"],
             "up": True, "category": "Private", "gateway": True}
_TAP = {"name": "Local Area Connection",
        "description": "TAP-Windows Adapter V9",
        "kind": lan.ADAPTER_TUNNEL, "addresses": ["10.8.0.2"],
        "up": True, "category": "Public", "gateway": False}
FRESH = "cv_" + "d" * 16


def _register_candidate(app, convoy_id, discriminator=DISC_A):
    return app.register_node({
        "project_root": "/shows/fresh", "convoy_id": convoy_id,
        "binding_state": "candidate", "comp_path": "/Embody",
        "runtime_id": "rt_fresh", "node_discriminator": discriminator,
        "envoy_port": 9800})


def _audit_events(app, event):
    return [line for line in app.db.audit_tail(limit=200)
            if isinstance(line, dict) and line.get("event") == event]


def _advisory(app, kind):
    return next(a for a in app.status()["advisories"] if a["kind"] == kind)


# -- genesis consults the admitted peers on record ---------------------

class TestGenesisConsultsAdmittedPeers:

    def test_a_fresh_candidate_rejoins_the_realm_admitted_peers_hold(
            self, app, tmp_path):
        """TEC-A4D 2026-09-18: realm.json gone, three admitted peers of
        the real realm on record, and the host founded a realm of one."""
        _admit_peer(app, tmp_path, 1, convoy_ids=(CONVOY_A,))
        code, response = _register_candidate(app, FRESH)
        assert code == 200, response
        assert response["convoy_id"] == CONVOY_A
        assert response["realm_state"] == "established"
        assert response["realm"]["state"] == "established"
        assert response["realm_peer_count"] == 1
        assert response["peer_realms"] == []
        established = _audit_events(app, "realm_established")
        assert established
        assert established[-1]["detail"]["rejoined_from_peers"] == [CONVOY_A]

    def test_admitted_peers_in_two_realms_latch_a_conflict_instead(
            self, app, tmp_path):
        _admit_peer(app, tmp_path, 1, convoy_ids=(CONVOY_A,))
        _admit_peer(app, tmp_path, 2, convoy_ids=(CONVOY_B,))
        code, response = _register_candidate(app, FRESH)
        assert code == 409 and response["reason"] == "local_realm_conflict"
        snapshot = app.realm.snapshot()
        assert snapshot["state"] == "conflict"
        assert set(snapshot["conflict_ids"]) == {CONVOY_A, CONVOY_B}
        assert "Resolve Realm Conflict" in _advisory(app, "realm_conflict")[
            "text"]

    def test_no_peers_on_record_means_genesis_as_before(self, app):
        code, response = _register_candidate(app, FRESH)
        assert code == 200, response
        assert response["convoy_id"] == FRESH
        assert response["realm_state"] == "candidate"
        assert response["realm_peer_count"] == 0
        assert response["advisories"] == []

    def test_peer_realms_names_the_other_realms_admitted_peers_hold(
            self, app, tmp_path):
        _register(app, convoy_id=CONVOY_A)
        _admit_peer(app, tmp_path, 1, convoy_ids=(CONVOY_A,))
        _admit_peer(app, tmp_path, 2, convoy_ids=(CONVOY_B,))
        response = _register(app, convoy_id=CONVOY_A)
        assert response["realm_peer_count"] == 1
        assert response["peer_realms"] == [CONVOY_B]


# -- an admitted peer advertising a realm we shared is a split ---------

class TestAdmittedPeerAdvertisingASharedRealm:

    @staticmethod
    def _announcement(host_id, fingerprint, states, ip="10.20.30.1"):
        return {"host_id": host_id, "fingerprint": fingerprint,
                "realm_states": dict(states), "convoy_ids": sorted(states),
                "endpoint": {"address": ip, "port": 47600}}

    def test_a_split_with_an_admitted_peer_latches_a_conflict(
            self, app, tmp_path):
        """TEC-A4D 2026-09-21 16:22:38: three admitted peers of the real
        realm advertised it and were logged as un-admitted strangers."""
        _register(app, convoy_id=CONVOY_A)
        assert app.realm.snapshot()["state"] == "established"
        peer_host_id, identity = _admit_peer(app, tmp_path, 3,
                                             convoy_ids=(CONVOY_B,))
        app._observe_realm_announcement(self._announcement(
            peer_host_id, identity.fingerprint, {CONVOY_B: "established"}))
        snapshot = app.realm.snapshot()
        assert snapshot["state"] == "conflict"
        assert set(snapshot["conflict_ids"]) == {CONVOY_A, CONVOY_B}
        assert not _audit_events(app, "realm_foreign_advisory")
        assert "Resolve Realm Conflict" in _advisory(app, "realm_conflict")[
            "text"]

    def test_a_stranger_is_an_advisory_that_names_its_standing(
            self, app, tmp_path):
        _register(app, convoy_id=CONVOY_A)
        stranger = hostkeys.load_or_create(str(tmp_path / "stranger"))
        app._observe_realm_announcement(self._announcement(
            "9" * 32, stranger.fingerprint, {CONVOY_B: "established"}))
        assert app.realm.snapshot()["state"] == "established"
        advisories = _audit_events(app, "realm_foreign_advisory")
        assert advisories
        detail = advisories[-1]["detail"]
        assert detail["sender_standing"] == "peer unknown"
        assert "(peer unknown)" in detail["detail"]

    def test_a_tofu_peer_of_a_realm_never_shared_is_an_advisory(
            self, app, tmp_path):
        """A TOFU-admitted neighbour may not move a committed realm
        (existing rule), and it is not recorded in the realm it now
        advertises: an advisory, naming that standing."""
        _register(app, convoy_id=CONVOY_A)
        identity = hostkeys.load_or_create(str(tmp_path / "peer-4"))
        peer_host_id = "%032x" % 4
        with app.lock:
            app.peers.admit(peer_host_id, identity.fingerprint,
                            admitted_via="lan_tofu",
                            cert_pem=identity.certificate_pem,
                            endpoints=["10.20.30.4:7404"],
                            convoy_ids=[CONVOY_A])
        app._observe_realm_announcement(self._announcement(
            peer_host_id, identity.fingerprint,
            {"cv_" + "e" * 16: "established"}))
        assert app.realm.snapshot()["state"] == "established"
        advisories = _audit_events(app, "realm_foreign_advisory")
        assert advisories[-1]["detail"]["sender_standing"] == (
            "admitted, but not in that realm on record")

    def test_an_operator_admitted_peer_of_this_realm_may_still_split_it(
            self, app, tmp_path):
        """Unchanged rule: a MANUALLY admitted member of the local realm
        advertising another established realm latches the conflict."""
        _register(app, convoy_id=CONVOY_A)
        peer_host_id, identity = _admit_peer(app, tmp_path, 4,
                                             convoy_ids=(CONVOY_A,))
        app._observe_realm_announcement(self._announcement(
            peer_host_id, identity.fingerprint,
            {"cv_" + "e" * 16: "established"}))
        assert app.realm.snapshot()["state"] == "conflict"


# -- a re-minted identity says so for days -----------------------------

class TestReMintedIdentity:

    def test_a_reminted_identity_is_an_advisory_naming_the_pinned_peers(
            self, tmp_path):
        """2026-09-18: host.json and identity.* vanished, peers.json
        survived; every peer refused the new certificate for days with
        no line on any panel."""
        data_dir = str(tmp_path / "host")
        first = ha.HostApp(data_dir)
        try:
            _admit_peer(first, tmp_path, 5, convoy_ids=(CONVOY_A,))
            old_fp = first.hostkeys.fingerprint
        finally:
            first.db.close()
        for name in ("host.json", "identity.key", "identity.cert.pem"):
            os.remove(os.path.join(data_dir, name))
        reborn = ha.HostApp(data_dir)
        try:
            assert reborn.hostkeys.fingerprint != old_fp
            status = reborn.status()
            assert status["identity_reminted"]["pinned_peers"] == 1
            assert "1 pinned peer" in _advisory(reborn, "identity_reminted")[
                "text"]
            assert _audit_events(reborn, "identity_reminted")
            # It ages out: peers re-pin within days, or never.
            assert reborn.db.identity_remint(
                now=time.time() + 15 * 86400) is None
        finally:
            reborn.db.close()

    def test_a_first_run_is_not_a_remint(self, app):
        assert app.status()["identity_reminted"] is None
        assert not _audit_events(app, "identity_reminted")


# -- refused handshakes are counted and mapped to admitted peers -------

class TestHandshakeRefusals:

    def test_refusals_are_counted_per_source_within_the_window(self, app):
        clock = [1000.0]
        lan_server, _port = peerserver.serve_lan(
            app, "127.0.0.1", 0, now=lambda: clock[0])
        try:
            for source in ("192.168.88.36", "192.168.88.36",
                           "192.168.88.20"):
                lan_server.note_refusal(source)
            summary = lan_server.refusal_summary(600.0)
            assert summary["count"] == 3
            assert summary["sources"] == {"192.168.88.36": 2,
                                          "192.168.88.20": 1}
            clock[0] += 601
            assert lan_server.refusal_summary(600.0)["count"] == 0
        finally:
            lan_server.server_close()

    def test_the_advisory_names_the_admitted_peers_behind_the_sources(
            self, app, tmp_path):
        peer_host_id, _identity = _admit_peer(app, tmp_path, 6,
                                              convoy_ids=(CONVOY_A,))

        class _Server:
            def refusal_summary(self, window_s):
                return {"count": 146, "window_s": window_s,
                        "sources": {"10.20.30.6": 140, "192.168.88.99": 6}}

        app.lan_server = _Server()
        try:
            status = app.status()
            assert status["inbound_refusals"]["admitted_peers"] == [
                peer_host_id]
            text = _advisory(app, "handshake_refusals")["text"]
            assert text.startswith("1 admitted peer(s) reject this host's "
                                   "certificate (146 refusals")
        finally:
            app.lan_server = None

    def test_strangers_alone_are_reported_as_sources(self, app):
        class _Server:
            def refusal_summary(self, window_s):
                return {"count": 9, "window_s": window_s,
                        "sources": {"192.168.88.99": 9}}

        app.lan_server = _Server()
        try:
            text = _advisory(app, "handshake_refusals")["text"]
            assert text == "9 refused LAN handshakes/10 min from 1 source(s)"
        finally:
            app.lan_server = None


# -- a pinned peer that changed identity, and the deliberate re-pin ----

class TestPinConflictsAndRepin:

    @staticmethod
    def _changed(host_id, identity, ip="10.20.30.7"):
        return {"host_id": host_id, "fingerprint": identity.fingerprint,
                "certificate_pem": identity.certificate_pem,
                "endpoint": {"address": ip, "port": 47600},
                "convoy_ids": [CONVOY_A],
                "realm_states": {CONVOY_A: "established"}}

    def test_a_changed_identity_is_listed_and_can_be_repinned(
            self, app, tmp_path):
        peer_host_id, old = _admit_peer(app, tmp_path, 7,
                                        convoy_ids=(CONVOY_A,))
        new = hostkeys.load_or_create(str(tmp_path / "peer-7-reborn"))
        app._note_pin_conflict(self._changed(peer_host_id, new))
        code, listing = app.peers_mismatched()
        assert code == 200
        assert [row["host_id"] for row in listing["peers"]] == [peer_host_id]
        row = listing["peers"][0]
        assert row["pinned_fingerprint"] == old.fingerprint
        assert row["fingerprint"] == new.fingerprint
        assert "cert_pem" not in row
        status = app.status()
        assert status["peers_mismatched"] == 1
        assert "Re-pin Changed Peers" in _advisory(
            app, "peer_identity_changed")["text"]
        assert _audit_events(app, "peer_identity_changed")

        code, payload = app.repin_peer({"host_id": peer_host_id})
        assert code == 200, payload
        assert payload["repinned"] is True
        assert payload["previous_fingerprint"] == old.fingerprint
        record = app.peers.get(peer_host_id)
        assert record["fingerprint"] == new.fingerprint
        assert record["cert_pem"].strip() == new.certificate_pem.strip()
        assert record["admitted_via"] == "operator_repin"
        assert record["convoy_ids"] == [CONVOY_A]
        assert record["endpoints"][0] == "10.20.30.7:47600"
        assert app.peers_mismatched()[1]["peers"] == []
        assert _audit_events(app, "peer_repinned")

    def test_a_matching_announcement_clears_a_stale_entry(
            self, app, tmp_path):
        peer_host_id, old = _admit_peer(app, tmp_path, 8,
                                        convoy_ids=(CONVOY_A,))
        new = hostkeys.load_or_create(str(tmp_path / "peer-8-reborn"))
        app._note_pin_conflict(self._changed(peer_host_id, new))
        assert len(app.peers_mismatched()[1]["peers"]) == 1
        app._note_pin_conflict(self._changed(peer_host_id, old))
        assert app.peers_mismatched()[1]["peers"] == []

    def test_unknown_or_unpinned_hosts_are_never_listed(self, app, tmp_path):
        stranger = hostkeys.load_or_create(str(tmp_path / "stranger"))
        app._note_pin_conflict(self._changed("8" * 32, stranger))
        assert app.peers_mismatched()[1]["peers"] == []
        code, payload = app.repin_peer({"host_id": "8" * 32})
        assert code == 404 and payload["reason"] == "peer_not_mismatched"

    def test_the_routes_require_the_token(self, server):
        code, _payload = server.call("/peers/mismatched", token=None,
                                     method="GET")
        assert code == 401
        code, payload = server.call("/peers/mismatched", method="GET")
        assert code == 200 and payload["peers"] == []
        code, payload = server.call("/peers/repin", {"host_id": "7" * 32})
        assert code == 404 and payload["reason"] == "peer_not_mismatched"


# -- the LAN posture rides /status and the advisories -------------------

class TestLanPosture:

    def test_a_tunnel_bind_by_lan_json_is_an_advisory(self, app):
        app.lan_server = object()
        app.lan_address, app.lan_port = "10.8.0.2", 47600
        app.lan_posture = lan.describe_bind("10.8.0.2", [_TAP], explicit=True)
        try:
            status = app.status()
            assert status["lan"]["warning"] == "tunnel_adapter"
            assert status["lan"]["adapter"]["description"] == \
                "TAP-Windows Adapter V9"
            text = _advisory(app, "lan_bind")["text"]
            assert "10.8.0.2" in text and "lan.json" in text
        finally:
            app.lan_server = None
            app.lan_posture = None

    def test_a_refused_public_network_is_an_advisory(self, app):
        app.lan_server = None
        app.lan_reason = "lan_public_network"
        status = app.status()
        assert status["lan"]["bound"] is False
        assert status["lan"]["reason"] == "lan_public_network"
        assert _advisory(app, "lan_bind")["text"].startswith(
            "LAN listener refused: lan public network")

    def test_desired_lan_endpoint_moves_off_the_vpn(self, app, monkeypatch):
        """TEC-A4D: the route probe answered the OpenVPN TAP; the Intel
        NIC held the LAN and the default route."""
        monkeypatch.setattr(lan, "primary_ipv4",
                            lambda socket_factory=None: "10.8.0.2")
        app.adapter_inventory = lan.InventoryCache(
            loader=lambda: [_ETHERNET, _TAP])
        address, port = ha.desired_lan_endpoint(app)
        assert (address, port) == ("192.168.88.10", 47600)
        assert app.lan_posture["skipped"]["description"] == \
            "TAP-Windows Adapter V9"
        assert app.lan_posture["probe_address"] == "10.8.0.2"

    def test_an_inventory_that_cannot_place_the_address_is_refreshed(
            self, app, monkeypatch):
        monkeypatch.setattr(lan, "primary_ipv4",
                            lambda socket_factory=None: "10.8.0.2")
        loads = []

        def loader():
            loads.append(1)
            return [_ETHERNET] if len(loads) == 1 else [_ETHERNET, _TAP]

        app.adapter_inventory = lan.InventoryCache(loader=loader)
        address, _port = ha.desired_lan_endpoint(app)
        assert len(loads) == 2 and address == "192.168.88.10"

    def test_a_tunnel_only_machine_keeps_loopback_with_a_named_reason(
            self, app, monkeypatch):
        monkeypatch.setattr(lan, "primary_ipv4",
                            lambda socket_factory=None: "10.8.0.2")
        app.adapter_inventory = lan.InventoryCache(loader=lambda: [_TAP])
        with pytest.raises(lan.LanConfigError) as info:
            ha.desired_lan_endpoint(app)
        assert info.value.reason == "lan_tunnel_only"
        assert app.lan_posture is None

    def test_no_inventory_keeps_the_probe_answer(self, app, monkeypatch):
        monkeypatch.setattr(lan, "primary_ipv4",
                            lambda socket_factory=None: "10.8.0.2")
        app.adapter_inventory = lan.InventoryCache(loader=lambda: None)
        address, _port = ha.desired_lan_endpoint(app)
        assert address == "10.8.0.2"
        assert app.lan_posture["inventory"] == "unavailable"


# -- compatibility has a reason and both versions ----------------------

class TestCompatibilityHasAReasonAndVersions:

    class _Info:
        def __init__(self, summary, state="connected"):
            self.state = state
            self.remote_capability_summary = summary

    class _Manager:
        is_stopped = False

        def __init__(self, info):
            self._info = info

        def peer_info(self, host_id):
            return self._info

    def _summary(self, **over):
        summary = {"envelope_protocol": protocol.PROTOCOL,
                   "manifest_digest": "x", "host_app_version": "6.0.280"}
        summary.update(over)
        return summary

    def test_a_matching_manifest_is_compatible_with_the_peer_version(
            self, app):
        digest = app.build_remote_manifest().manifest_digest()
        app.session_manager = self._Manager(self._Info(
            self._summary(manifest_digest=digest)))
        verdict = app._peer_compatibility_projection("a" * 32)
        assert verdict["label"] == "compatible"
        assert verdict["peer_app_version"] == "6.0.280"
        assert verdict["local_app_version"] == ha.APP_VERSION

    def test_a_protocol_mismatch_names_both_protocols(self, app):
        app.session_manager = self._Manager(self._Info(
            self._summary(envelope_protocol="convoy/0")))
        verdict = app._peer_compatibility_projection("a" * 32)
        assert verdict["label"] == "incompatible"
        assert "convoy/0" in verdict["reason"]
        assert protocol.PROTOCOL in verdict["reason"]

    def test_a_partial_manifest_is_limited_with_counts_and_version(
            self, app, monkeypatch):
        local = app.build_remote_manifest()
        subset = dict(list(local.operations.items())[:1])

        class _Cached:
            protocol = protocol.PROTOCOL
            operations = subset

        monkeypatch.setattr(app, "_cached_peer_manifest",
                            lambda record, digest: _Cached())
        app.session_manager = self._Manager(self._Info(self._summary()))
        verdict = app._peer_compatibility_projection("a" * 32)
        assert verdict["label"] == "limited"
        assert verdict["reason"] == (
            "1 of %d operations shared with host app 6.0.280"
            % len(local.operations))

    def test_no_session_is_still_unknown(self, app):
        app.session_manager = None
        assert app._peer_compatibility_projection("a" * 32) is None

    def test_the_hello_profile_carries_the_host_app_version(self, app):
        summary = app._session_hello_profile().capability_summary
        assert summary["host_app_version"] == ha.APP_VERSION

    def test_local_rows_and_the_self_entry_carry_the_host_app_version(
            self, app):
        node = _register(app)
        code, directory = app.network_nodes(CONVOY_A)
        assert code == 200
        row = next(r for r in directory["nodes"]
                   if r["node_id"] == node["node_id"])
        assert row["host_app_version"] == ha.APP_VERSION
        assert directory["peers"][0]["host_app_version"] == ha.APP_VERSION


# -- host.log lines carry a timestamp ----------------------------------

def test_daemon_log_lines_are_timestamped(monkeypatch):
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stderr", buf)
    ha._stamp("convoy LAN: peer listener on 192.168.88.10:47600")
    assert re.match(r"^\[\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\] convoy LAN: "
                    r"peer listener on 192.168.88.10:47600\n$",
                    buf.getvalue())


# -- refusals from a peer that is CONNECTED come from a ghost record ----

class _ConnectedManager:
    """A session manager holding one live pair session with `host_id`."""
    is_stopped = False

    def __init__(self, host_id):
        self._host_id = host_id

    def peer_info(self, host_id):
        state = "connected" if host_id == self._host_id else "backoff"
        return type("Info", (), {"state": state})()


class TestGhostDialRefusals:

    def test_refusals_from_a_connected_peer_name_a_previous_identity(
            self, app, tmp_path):
        peer_host_id, _identity = _admit_peer(app, tmp_path, 6,
                                              convoy_ids=(CONVOY_A,))

        class _Server:
            def refusal_summary(self, window_s):
                return {"count": 179, "window_s": window_s,
                        "sources": {"10.20.30.6": 179}}

        app.lan_server = _Server()
        app.session_manager = _ConnectedManager(peer_host_id)
        try:
            status = app.status()
            assert status["inbound_refusals"]["connected_peers"] == [
                peer_host_id]
            text = _advisory(app, "handshake_refusals")["text"]
            assert text.startswith("1 admitted peer(s) still dial a previous "
                                   "identity of this host (179 refusals")
            assert "re-pin" not in text
        finally:
            app.lan_server = None
            app.session_manager = None

    def test_a_mix_names_both_counts(self, app, tmp_path):
        connected, _ = _admit_peer(app, tmp_path, 6, convoy_ids=(CONVOY_A,))
        rejecting, _ = _admit_peer(app, tmp_path, 7, convoy_ids=(CONVOY_A,))

        class _Server:
            def refusal_summary(self, window_s):
                return {"count": 20, "window_s": window_s,
                        "sources": {"10.20.30.6": 10, "10.20.30.7": 10}}

        app.lan_server = _Server()
        app.session_manager = _ConnectedManager(connected)
        try:
            status = app.status()
            assert status["inbound_refusals"]["connected_peers"] == [
                connected]
            assert set(status["inbound_refusals"]["admitted_peers"]) == {
                connected, rejecting}
            text = _advisory(app, "handshake_refusals")["text"]
            assert text.startswith("1 admitted peer(s) reject this host's "
                                   "certificate and 1 dial a previous "
                                   "identity of it (20 refusals")
        finally:
            app.lan_server = None
            app.session_manager = None
