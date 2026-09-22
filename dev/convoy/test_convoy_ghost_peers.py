"""A re-minted host's ghost record goes dormant at the dial seam.

Field 2026-09-22: TEC-A4D lost host.json on 09-18 and came back as a NEW
host_id with a new key at the same address. Its three peers TOFU-admitted
the new identity beside the old record and kept dialing the old one
(~18 refused handshakes a minute, forever), which the reborn host read as
"3 admitted peers reject this host's certificate: they must re-pin it" --
the wrong diagnosis, because the same peers accepted its outbound calls.

Dormant, never forgotten (review panel 2026-09-22): a forget would free
the old host_id for a trust-on-first-use takeover and launder an
observe-only narrowing into a full admission. Dormancy only stops the
dialing; the pin, state, lineage and work stay, and any contact from the
identity itself wakes it.
"""

import pytest

import convoy_discovery as discovery_mod
import convoy_hostapp as ha
import convoy_peerclient as peerclient
import convoy_sessions as sessions_mod
from test_convoy_hostapp import Server
from test_convoy_revocation import (CONVOY, PEER, PEER_FP, OTHER, OTHER_FP,
                                    _KEYS_BY_FP, admit, register)

# Dead loopback ports: the server's own dial workers try every configured
# endpoint for real, and a LAN address that answers (this machine's live
# daemon did, first cut) turns a background dial into a genuine pin
# mismatch that parks the ghost before the test looks.
ADDRESS = "127.0.0.1:9"
ENDPOINT = sessions_mod.PeerEndpoint("127.0.0.1", 9)
OWN_ADDRESS = "127.0.0.1:10"
GHOST, GHOST_FP = PEER, PEER_FP
REBORN, REBORN_FP = OTHER, OTHER_FP


@pytest.fixture
def server(tmp_path):
    s = Server(str(tmp_path / "state"))
    register(s)
    yield s
    s.stop()


def _events(server, event):
    with server.app.lock:
        return [e for e in server.app.db.audit_tail(limit=400)
                if isinstance(e, dict) and e.get("event") == event]


def _pin_mismatch_dial(monkeypatch):
    """Only the GHOST's pin fails at the address; every other dial is
    plainly unreachable. The patch is process-global and the server's
    own dial workers use it too: refusing EVERY target let a background
    dial of the reborn record park IT first (macOS CI, 2026-09-22)."""
    def refuse(target, keys, timeout):
        if target.host_id == GHOST:
            raise peerclient.PeerSocketPinMismatch(
                peerclient._pin_mismatch(target, offered=None, cause=None))
        raise peerclient.PeerSocketUnavailable("connection refused")
    monkeypatch.setattr(ha.peerclient, "open_authenticated_socket", refuse)


def _haunt(server, ghost_endpoints=(ADDRESS,), reborn_endpoints=(ADDRESS,),
           heard_after=False):
    """The ghost pinned first at ADDRESS, the reborn identity admitted
    later at the same address; the ghost last connected before that."""
    ghost = admit(server, GHOST, GHOST_FP, endpoints=list(ghost_endpoints))
    reborn = admit(server, REBORN, REBORN_FP,
                   endpoints=list(reborn_endpoints))
    when = reborn["pin_first_seen"] + (60 if heard_after else -60)
    with server.app.lock:
        assert server.app.peers.touch_seen(GHOST, when=when)
        # The reborn host talks to us (that is how the field case looks).
        assert server.app.peers.touch_seen(REBORN)
    return ghost, reborn


def _dial_ghost(server):
    with pytest.raises(peerclient.PeerSocketPinMismatch):
        server.app._dial_peer_session(GHOST, ENDPOINT, 1.0)


def _configured(server):
    _namespaces, configs = server.app._session_configs()
    return {host_id for host_id, _endpoints, _ctx in configs}


def test_a_ghost_whose_address_moved_to_the_reborn_host_goes_dormant(
        server, monkeypatch):
    _haunt(server)
    _pin_mismatch_dial(monkeypatch)
    assert GHOST in _configured(server)
    _dial_ghost(server)
    ghost = server.app.peers.get(GHOST)
    # Parked, not forgotten: pin, state and lineage are untouched.
    assert ghost["state"] == "admitted"
    assert ghost["fingerprint"] == GHOST_FP
    assert ghost["dormant"]["successor_host_id"] == REBORN
    assert ghost["dormant"]["address"] == ADDRESS
    reborn = server.app.peers.get(REBORN)
    assert reborn["state"] == "admitted" and reborn["dormant"] is None
    (event,) = _events(server, "peer_dormant")
    assert event["detail"]["host_id"] == GHOST
    assert event["detail"]["successor_host_id"] == REBORN
    assert not _events(server, "peer_forgotten")
    # No more dials: dropped from the session configs and the HTTP targets.
    assert GHOST not in _configured(server)
    with pytest.raises(peerclient.PeerSocketUnavailable, match="dormant"):
        server.app._dial_peer_session(GHOST, ENDPOINT, 1.0)
    status = server.app.status()
    assert status["peers_dormant"] == 1
    advisory = next(a for a in status["advisories"]
                    if a["kind"] == "dormant_peers")
    assert advisory["host_ids"] == [GHOST]
    assert advisory["text"].startswith("1 dormant peer(s)")


def test_an_observe_only_ghost_stays_observe_only(server, monkeypatch):
    _haunt(server)
    with server.app.lock:
        server.app.peers.observe(GHOST)
    _pin_mismatch_dial(monkeypatch)
    _dial_ghost(server)
    ghost = server.app.peers.get(GHOST)
    assert ghost["state"] == "observe_only"
    assert ghost["dormant"]["successor_host_id"] == REBORN


def test_an_inbound_contact_wakes_the_ghost(server, monkeypatch):
    _haunt(server)
    _pin_mismatch_dial(monkeypatch)
    _dial_ghost(server)
    assert server.app.peers.get(GHOST)["dormant"]
    # What the LAN listener does on connection accept for an allowed peer.
    with server.app.lock:
        assert server.app.peers.touch_seen(GHOST)
    assert server.app.peers.get(GHOST)["dormant"] is None
    (event,) = _events(server, "peer_woken")
    assert event["detail"]["cause"] == "inbound"
    assert GHOST in _configured(server)


def test_its_own_announcement_wakes_the_ghost(server, monkeypatch):
    _haunt(server)
    _pin_mismatch_dial(monkeypatch)
    _dial_ghost(server)
    announcement = {
        "host_id": GHOST, "fingerprint": GHOST_FP,
        "certificate_pem": _KEYS_BY_FP[GHOST_FP].certificate_pem,
        "endpoint": {"address": "127.0.0.1", "port": 9},
        "timestamp_ms": 0.0,
    }
    with server.app.lock:
        status, _detail = discovery_mod.apply_tofu(
            server.app.peers, announcement, [CONVOY])
    assert status == "admitted"
    assert server.app.peers.get(GHOST)["dormant"] is None
    (event,) = _events(server, "peer_woken")
    assert event["detail"]["cause"] == "announcement"


def test_a_re_admit_wakes_the_ghost(server, monkeypatch):
    _haunt(server)
    _pin_mismatch_dial(monkeypatch)
    _dial_ghost(server)
    admit(server, GHOST, GHOST_FP, endpoints=[OWN_ADDRESS])
    assert server.app.peers.get(GHOST)["dormant"] is None
    (event,) = _events(server, "peer_woken")
    assert event["detail"]["cause"] == "upsert"


def test_a_ghost_heard_after_the_reborn_host_appeared_is_kept_awake(
        server, monkeypatch):
    _haunt(server, heard_after=True)
    _pin_mismatch_dial(monkeypatch)
    _dial_ghost(server)
    assert server.app.peers.get(GHOST)["dormant"] is None
    assert not _events(server, "peer_dormant")


def test_an_unreachable_dial_parks_nothing(server, monkeypatch):
    _haunt(server)

    def down(target, keys, timeout):
        raise peerclient.PeerSocketUnavailable("connection refused")
    monkeypatch.setattr(ha.peerclient, "open_authenticated_socket", down)
    with pytest.raises(peerclient.PeerSocketUnavailable):
        server.app._dial_peer_session(GHOST, ENDPOINT, 1.0)
    assert server.app.peers.get(GHOST)["dormant"] is None


def test_an_older_pin_never_supersedes_a_newer_one(server, monkeypatch):
    # Reversed roles: the record pinned LAST is the address's owner, so a
    # mismatch dialing it must not park it in favour of the older pin.
    admit(server, REBORN, REBORN_FP, endpoints=[ADDRESS])
    admit(server, GHOST, GHOST_FP, endpoints=[ADDRESS])   # newer pin
    _pin_mismatch_dial(monkeypatch)
    _dial_ghost(server)
    assert server.app.peers.get(GHOST)["dormant"] is None
    assert not _events(server, "peer_dormant")


def test_a_pending_successor_parks_nothing(server, monkeypatch):
    # Only an ADMITTED (or observe-only) record can own the address.
    admit(server, GHOST, GHOST_FP, endpoints=[ADDRESS])
    with server.app.lock:
        server.app.peers.record_peer(
            REBORN, REBORN_FP, endpoints=[ADDRESS],
            cert_pem=_KEYS_BY_FP[REBORN_FP].certificate_pem)
    _pin_mismatch_dial(monkeypatch)
    _dial_ghost(server)
    assert server.app.peers.get(GHOST)["dormant"] is None


def test_a_bookkeeping_failure_never_masks_the_pin_mismatch(
        server, monkeypatch):
    _haunt(server)
    _pin_mismatch_dial(monkeypatch)

    def boom(*args, **kwargs):
        raise OSError("disk full")
    monkeypatch.setattr(server.app.peers, "set_dormant", boom)
    _dial_ghost(server)
    assert server.app.peers.get(GHOST)["dormant"] is None
    (event,) = _events(server, "peer_dormant_error")
    assert "OSError" in event["detail"]["error"]


def test_a_dormant_record_survives_a_reload(server, monkeypatch):
    _haunt(server)
    _pin_mismatch_dial(monkeypatch)
    _dial_ghost(server)
    with server.app.lock:
        server.app.peers._load()
        assert server.app.peers.unreadable is None
        assert server.app.peers.get(GHOST)["dormant"]["successor_host_id"] \
            == REBORN
