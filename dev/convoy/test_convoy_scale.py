"""Deterministic few-dozen-host Convoy scale gates.

These tests use real host admission records and the production aggregation
scheduler while replacing only network I/O.  They deliberately make no LLM,
TouchDesigner, multicast, or physical-LAN assumptions, so they can run in the
ordinary unit matrix on Windows and macOS.
"""

import threading

import convoy_hostapp as hostapp
import convoy_hostkeys as hostkeys
import convoy_peerclient as peerclient


CONVOY = "scale-studio"


def _admit(app, root, number):
    identity = hostkeys.load_or_create(str(root / ("peer-%02d" % number)))
    host_id = "%032x" % number
    with app.lock:
        app.peers.admit(
            host_id, identity.fingerprint,
            cert_pem=identity.certificate_pem,
            endpoints=["10.44.0.%d:47600" % number],
            convoy_ids=[CONVOY])
    return host_id


def test_thirty_host_directory_fanout_is_one_concurrent_wave(
        tmp_path, monkeypatch):
    """One local + 29 siblings is the documented upper ordinary topology.

    A barrier proves every remote query can be in flight together.  Each peer
    returns several nodes to cover the independent host/node identities and
    same-host routing model rather than testing one node per IP only.
    """
    app = hostapp.HostApp(str(tmp_path / "local"))
    try:
        peer_ids = [_admit(app, tmp_path, number)
                    for number in range(1, 30)]
        rendezvous = threading.Barrier(len(peer_ids))
        guard = threading.Lock()
        active = 0
        maximum = 0

        def fetch(target, keys, convoy_id, timeout, pool=None):
            nonlocal active, maximum
            with guard:
                active += 1
                maximum = max(maximum, active)
            try:
                rendezvous.wait(timeout=5.0)
                number = int(target.host_id, 16)
                return {
                    "ok": True, "host_id": target.host_id,
                    "convoy_id": convoy_id,
                    "nodes": [
                        {"node_id": "%032x" % (10000 + number * 3 + index),
                         "node_name": "render-%02d-%d" % (number, index),
                         "status": "online", "online": True}
                        for index in range(3)
                    ],
                }
            finally:
                with guard:
                    active -= 1

        monkeypatch.setattr(peerclient, "get_peer_nodes", fetch)
        code, payload = app.network_nodes(CONVOY)

        assert code == 200
        assert maximum == 29
        assert len(payload["nodes"]) == 29 * 3
        assert len([row for row in payload["peers"]
                    if not row.get("local")]) == 29
        assert {row["status"] for row in payload["peers"]
                if not row.get("local")} == {"online"}
        assert {row["host_id"] for row in payload["nodes"]} == set(peer_ids)
    finally:
        app.shutdown_network_queries()
        app.db.close()


def test_thirty_host_partial_outage_does_not_hide_healthy_siblings(
        tmp_path, monkeypatch):
    app = hostapp.HostApp(str(tmp_path / "local"))
    try:
        peer_ids = [_admit(app, tmp_path, number)
                    for number in range(1, 30)]
        offline = set(peer_ids[::5])

        def fetch(target, keys, convoy_id, timeout, pool=None):
            if target.host_id in offline:
                return peerclient.UNREACHABLE
            return {
                "ok": True, "host_id": target.host_id,
                "convoy_id": convoy_id,
                "nodes": [{"node_id": target.host_id,
                           "node_name": target.host_id,
                           "status": "online", "online": True}],
            }

        monkeypatch.setattr(peerclient, "get_peer_nodes", fetch)
        code, payload = app.network_nodes(CONVOY)

        assert code == 200
        assert {row["host_id"] for row in payload["nodes"]} == \
            set(peer_ids) - offline
        status = {row["host_id"]: row for row in payload["peers"]
                  if not row.get("local")}
        assert all(status[host_id]["reason"] == "peer_unreachable"
                   for host_id in offline)
        assert all(status[host_id]["status"] == "online"
                   for host_id in set(peer_ids) - offline)
    finally:
        app.shutdown_network_queries()
        app.db.close()


def test_sixteen_directory_callers_share_one_bounded_peer_refresh(
        tmp_path, monkeypatch):
    app = hostapp.HostApp(str(tmp_path / "local"))
    try:
        peer_ids = [_admit(app, tmp_path, number)
                    for number in range(1, 30)]
        peer_wave = threading.Barrier(len(peer_ids))
        caller_wave = threading.Barrier(16)
        guard = threading.Lock()
        calls = 0
        active = 0
        maximum = 0

        def fetch(target, keys, convoy_id, timeout, pool=None):
            nonlocal calls, active, maximum
            with guard:
                calls += 1
                active += 1
                maximum = max(maximum, active)
            try:
                peer_wave.wait(timeout=5.0)
                return {
                    "ok": True,
                    "host_id": target.host_id,
                    "convoy_id": convoy_id,
                    "nodes": [{
                        "node_id": target.host_id,
                        "node_name": target.host_id,
                        "status": "online",
                        "online": True,
                    }],
                }
            finally:
                with guard:
                    active -= 1

        monkeypatch.setattr(peerclient, "get_peer_nodes", fetch)
        results = [None] * 16

        def query(index):
            caller_wave.wait(timeout=5.0)
            results[index] = app.network_nodes(CONVOY)

        callers = [threading.Thread(target=query, args=(index,))
                   for index in range(len(results))]
        for caller in callers:
            caller.start()
        for caller in callers:
            caller.join(timeout=10.0)

        assert all(not caller.is_alive() for caller in callers)
        assert all(result is not None and result[0] == 200
                   for result in results)
        assert calls == 29
        assert maximum <= hostapp.NETWORK_QUERY_WORKERS
        assert all(len(result[1]["nodes"]) == 29 for result in results)
        metrics = app._network_query_metrics
        assert metrics["refreshes"] == 1
        assert metrics["cache_hits"] + metrics["coalesced"] == 15
    finally:
        app.shutdown_network_queries()
        app.db.close()


def test_directory_result_cache_expires_and_membership_invalidates_it(
        tmp_path, monkeypatch):
    app = hostapp.HostApp(str(tmp_path / "local"))
    try:
        _admit(app, tmp_path, 1)
        clock = [100.0]
        app._network_nodes_cache_clock = lambda: clock[0]
        calls = 0

        def fetch(target, keys, convoy_id, timeout, pool=None):
            nonlocal calls
            calls += 1
            return {
                "ok": True,
                "host_id": target.host_id,
                "convoy_id": convoy_id,
                "nodes": [{
                    "node_id": target.host_id,
                    "node_name": target.host_id,
                    "status": "online",
                    "online": True,
                }],
            }

        monkeypatch.setattr(peerclient, "get_peer_nodes", fetch)
        assert app.network_nodes(CONVOY)[0] == 200
        assert app.network_nodes(CONVOY)[0] == 200
        assert calls == 1

        code, registered = app.register_node({
            "project_root": str(tmp_path / "project"),
            "comp_path": "/Embody",
            "convoy_id": CONVOY,
            "runtime_id": "scale-runtime",
        })
        assert code == 200, registered
        code, refreshed = app.network_nodes(CONVOY)
        assert code == 200
        assert registered["node_id"] in {
            row["node_id"] for row in refreshed["nodes"]}
        assert calls == 2

        clock[0] += hostapp.NETWORK_NODE_CACHE_TTL_S + 0.01
        assert app.network_nodes(CONVOY)[0] == 200
        assert calls == 3
    finally:
        app.shutdown_network_queries()
        app.db.close()


def test_controllers_fanout_uses_the_shared_bounded_pool(
        tmp_path, monkeypatch):
    """/network/controllers must funnel its peer fanout through the ONE
    host-wide query pool, not a fresh per-request 32-thread executor.

    Several concurrent controllers-plane callers at the 29-peer ceiling would
    otherwise stack N*29 threads and N*29 simultaneous peer TLS calls -- the
    exact amplification the sibling nodes route was fixed to prevent. The
    shared pool makes the NETWORK_QUERY_WORKERS bound true for the process, so
    peak concurrency never exceeds it however many callers overlap.
    """
    import time

    app = hostapp.HostApp(str(tmp_path / "local"))
    try:
        peer_ids = [_admit(app, tmp_path, number)
                    for number in range(1, 30)]
        guard = threading.Lock()
        active = 0
        maximum = 0

        def fetch(target, keys, convoy_id, timeout, pool=None):
            nonlocal active, maximum
            with guard:
                active += 1
                maximum = max(maximum, active)
            try:
                time.sleep(0.05)
                return {
                    "ok": True, "host_id": target.host_id,
                    "convoy_id": convoy_id,
                    "controllers": [],
                }
            finally:
                with guard:
                    active -= 1

        monkeypatch.setattr(peerclient, "get_peer_controllers", fetch)

        callers = 8
        start = threading.Barrier(callers)
        results = [None] * callers

        def query(index):
            start.wait(timeout=5.0)
            results[index] = app.network_controllers(CONVOY)

        threads = [threading.Thread(target=query, args=(index,))
                   for index in range(callers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15.0)

        assert all(not thread.is_alive() for thread in threads)
        assert all(result is not None and result[0] == 200
                   for result in results)
        # The proof: with a per-request executor this would reach callers*29;
        # the shared pool holds it at the process-wide worker bound.
        assert maximum <= hostapp.NETWORK_QUERY_WORKERS
        assert {row["host_id"] for row in results[0][1]["peers"]
                if not row.get("local")} == set(peer_ids)
    finally:
        app.shutdown_network_queries()
        app.db.close()
