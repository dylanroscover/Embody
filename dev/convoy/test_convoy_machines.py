"""A machine is the permanent thing; a node is one .toe lineage on it.

The owner's model (2026-10-31): Convoy stays on once a machine has joined, a
version-up of a .toe is not a new node, and the directory lists machines with
the projects open on them. See docs/reports/convoy-version-up-loses-node-2026-10-31.md.
"""
import hashlib
import socket
import time

import pytest

import convoy_identity as identity
from conftest import approve_td_python
from test_convoy_hostapp import Server

ROOT = "/Work/repo"
COMP = "/project1/Embody"


@pytest.fixture
def server(tmp_path):
    instance = Server(str(tmp_path / "state"))
    yield instance
    instance.stop()


def _exact(path):
    """convoy_client.stable_node_discriminator's exact-path hash."""
    return "nd_" + hashlib.sha256(path.encode()).hexdigest()[:32]


def _register(server, toe, runtime, port=None, folder=ROOT, pid=None,
              expect=200):
    """Register as ConvoyExt does: an exact-path hash plus the toe's metadata."""
    path = folder + "/" + toe
    metadata = {"toe_path": path, "toe_name": toe, "hostname": "BOX"}
    if pid is not None:
        metadata["process_id"] = pid
    body = {
        "project_root": ROOT, "convoy_id": "cv", "comp_path": COMP,
        "runtime_id": runtime, "metadata": metadata,
        "node_discriminator": _exact(path)}
    if port is not None:
        body["envoy_port"] = port
    code, answer = server.call("/register", body)
    assert code == expect, answer
    return answer


def _rows(server):
    _, listing = server.call("/nodes")
    return {n["node_id"]: n for n in listing["nodes"]}


def _grant(server, node):
    return server.app.policy.allow_td_python(node["node_id"])


# -- lineage -----------------------------------------------------------

@pytest.mark.parametrize("a,b", [
    ("/w/e1.toe", "/w/e1.5.toe"),
    ("/w/e1.5.toe", "/w/e1.6.toe"),
    ("/w/Foo-6.toe", "/w/Foo-6.399.toe"),
    ("/w/show.15.toe", "/w/show.13.toe"),
    ("/w/moonshine.toe", "/w/CrashAutoSave.moonshine.10.toe"),
])
def test_every_save_of_one_project_shares_a_lineage(a, b):
    assert (identity.toe_lineage_discriminator(a, platform="linux")
            == identity.toe_lineage_discriminator(b, platform="linux"))


@pytest.mark.parametrize("a,b", [
    ("/w/show.toe", "/w/rig.toe"),
    ("/w/a/show.toe", "/w/b/show.2.toe"),
    ("/w/show.lanes.toe", "/w/show.toe"),
    ("/w/show2025.toe", "/w/show2024.toe"),
    ("/w/Foo-6.toe", "/w/Foo-7.toe"),
])
def test_different_projects_keep_different_lineages(a, b):
    assert (identity.toe_lineage_discriminator(a, platform="linux")
            != identity.toe_lineage_discriminator(b, platform="linux"))


def test_lineage_folds_case_only_where_the_filesystem_does():
    upper = identity.toe_lineage_discriminator("C:/Work/Show.2.toe",
                                               platform="win32")
    assert upper == identity.toe_lineage_discriminator("c:\\work\\show.toe",
                                                       platform="win32")
    assert (identity.toe_lineage_discriminator("/Work/Show.toe", platform="linux")
            != identity.toe_lineage_discriminator("/work/show.toe",
                                                  platform="linux"))


@pytest.mark.parametrize("bad", [None, "", "relative/show.toe", "/w/show.tox"])
def test_a_path_that_is_not_a_saved_toe_has_no_lineage(bad):
    assert identity.toe_lineage_discriminator(bad, platform="linux") is None


# -- a version-up is not a new node ------------------------------------

def test_a_live_version_up_keeps_the_node_and_its_grant(server):
    old = _register(server, "e1.5.toe", "rt_live", port=9981)
    approve_td_python(server.app, old["node_id"])
    new = _register(server, "e1.6.toe", "rt_live", port=9981)
    assert new["node_id"] == old["node_id"]
    assert new["td_python_approved"] is True
    rows = _rows(server)
    assert list(rows) == [old["node_id"]]
    assert rows[old["node_id"]]["metadata"]["toe_name"] == "e1.6.toe"


def test_reopening_the_next_version_keeps_the_node_and_its_grant(server):
    old = _register(server, "e1.5.toe", "rt_one", port=9981)
    approve_td_python(server.app, old["node_id"])
    server.call("/unregister", {"node_id": old["node_id"]})
    new = _register(server, "e1.6.toe", "rt_two", port=9982)
    assert new["node_id"] == old["node_id"]
    assert new["td_python_approved"] is True


def test_reopening_an_older_version_keeps_the_node(server):
    old = _register(server, "show.15.toe", "rt_one", port=9981)
    approve_td_python(server.app, old["node_id"])
    server.call("/unregister", {"node_id": old["node_id"]})
    new = _register(server, "show.13.toe", "rt_two", port=9982)
    assert new["node_id"] == old["node_id"]
    assert new["td_python_approved"] is True


def test_reopening_after_a_crash_keeps_the_node_inside_the_dead_grace(server):
    """No /unregister and a new port: the old port is closed, so its runtime
    is provably gone, with no half-hour wait for the row to read dead."""
    old = _register(server, "e1.5.toe", "rt_one", port=_closed_port())
    approve_td_python(server.app, old["node_id"])
    new = _register(server, "e1.6.toe", "rt_two", port=_closed_port())
    assert new["node_id"] == old["node_id"]
    assert new["td_python_approved"] is True


def test_queued_work_survives_a_version_up(server):
    old = _register(server, "e1.5.toe", "rt_live", port=_closed_port())
    server.app.stop_drain_loop()
    _, created = server.call("/jobs", {
        "idempotency_key": "keep-me", "node_id": old["node_id"],
        "operation": "query_network", "arguments": {}})
    _register(server, "e1.6.toe", "rt_live", port=_closed_port())
    code, job = server.call("/jobs/" + created["job"]["delivery_id"],
                            method="GET")
    assert code == 200, job
    assert job["job"].get("state") != "refused", job


def test_two_live_versions_stay_two_nodes_and_the_second_inherits_nothing(
        server):
    with _listening_port() as port:
        first = _register(server, "e1.5.toe", "rt_one", port=port)
        approve_td_python(server.app, first["node_id"])
        second = _register(server, "e1.6.toe", "rt_two", port=_closed_port())
        assert second["node_id"] != first["node_id"]
        assert second["td_python_approved"] is False
        again = _register(server, "e1.6.toe", "rt_two", port=_closed_port())
        assert again["node_id"] == second["node_id"], "a heartbeat sticks"
        rows = _rows(server)
        assert first["node_id"] in rows and second["node_id"] in rows
        assert _grant(server, first) is True


def test_the_same_file_opened_twice_is_still_refused(server):
    with _listening_port() as port:
        _register(server, "e1.toe", "rt_one", port=port)
        answer = _register(server, "e1.toe", "rt_two", port=_closed_port(),
                           expect=409)
        assert answer["reason"] == "node_runtime_conflict"


# -- nothing but an explicit act removes another project ----------------

def test_opening_another_project_in_the_repo_leaves_the_first_alone(server):
    show = _register(server, "show.toe", "rt_one", port=9981)
    approve_td_python(server.app, show["node_id"])
    server.call("/unregister", {"node_id": show["node_id"]})
    rig = _register(server, "rig.toe", "rt_two", port=9982)
    rows = _rows(server)
    assert show["node_id"] in rows and rig["node_id"] in rows
    assert _grant(server, show) is True


def test_same_folder_series_in_two_subfolders_stay_two_nodes(server):
    a = _register(server, "show.toe", "rt_one", port=9981, folder=ROOT + "/a")
    server.call("/unregister", {"node_id": a["node_id"]})
    b = _register(server, "show.2.toe", "rt_two", port=9982,
                  folder=ROOT + "/b")
    assert a["node_id"] != b["node_id"]
    assert a["node_id"] in _rows(server)


def test_file_open_in_one_process_never_moves_a_grant(server):
    """runtime_id is per process, so it proves nothing about the project."""
    show = _register(server, "show.toe", "rt_live", port=9981)
    approve_td_python(server.app, show["node_id"])
    rig = _register(server, "rig.toe", "rt_live", port=9981)
    assert rig["node_id"] != show["node_id"]
    assert rig["td_python_approved"] is False
    rows = _rows(server)
    assert _grant(server, show) is True
    assert rows[show["node_id"]]["envoy_port"] is None, "its port moved on"


def test_a_row_survives_until_its_toe_is_deleted(server, tmp_path):
    toe = tmp_path / "show.toe"
    toe.write_bytes(b"x")
    code, node = server.call("/register", {
        "project_root": str(tmp_path), "convoy_id": "cv", "comp_path": COMP,
        "runtime_id": "rt_one", "envoy_port": 9981,
        "metadata": {"toe_path": str(toe), "toe_name": "show.toe"},
        "node_discriminator": _exact(str(toe))})
    assert code == 200, node
    approve_td_python(server.app, node["node_id"])
    server.call("/unregister", {"node_id": node["node_id"]})
    a_year = time.time() + 365 * 86400
    server.app.started = 0
    assert server.app._evict_stale_nodes(a_year) == []
    assert _grant(server, node) is True
    toe.unlink()
    evicted = server.app._evict_stale_nodes(a_year)
    assert [e["cause"] for e in evicted] == ["dead_project"]


# -- rows written before lineage identity ------------------------------

def _seed_exact_row(server, toe, runtime, node_id=None):
    answer = _register(server, toe, runtime, port=None)
    record = server.app.directory.lookup(answer["node_id"])
    exact = _exact(ROOT + "/" + toe)
    server.app.directory.rekey(answer["node_id"], exact)
    server.app.db.save_node(record)
    return answer


def test_existing_rows_migrate_to_lineage_on_start_keeping_the_grant(
        tmp_path):
    directory = str(tmp_path / "state")
    first = Server(directory)
    try:
        old = _seed_exact_row(first, "e1.5.toe", "rt_one")
        approve_td_python(first.app, old["node_id"])
        server_call = first.call("/unregister", {"node_id": old["node_id"]})
        assert server_call[0] == 200
    finally:
        first.stop()
    second = Server(directory)
    try:
        record = second.app.directory.lookup(old["node_id"])
        assert record["node_discriminator"] == \
            identity.toe_lineage_discriminator(ROOT + "/e1.5.toe")
        new = _register(second, "e1.6.toe", "rt_two", port=9982)
        assert new["node_id"] == old["node_id"]
        assert new["td_python_approved"] is True
    finally:
        second.stop()


def test_duplicate_rows_of_one_lineage_merge_to_the_one_holding_the_grant(
        tmp_path):
    directory = str(tmp_path / "state")
    first = Server(directory)
    try:
        ungranted = _seed_exact_row(first, "e1.5.toe", "rt_one")
        granted = _seed_exact_row(first, "e1.6.toe", "rt_two")
        approve_td_python(first.app, granted["node_id"])
        for row in (ungranted, granted):
            first.call("/unregister", {"node_id": row["node_id"]})
    finally:
        first.stop()
    second = Server(directory)
    try:
        ids = [r["node_id"] for r in second.app.directory.nodes()]
        assert ids == [granted["node_id"]]
        assert second.app.policy.allow_td_python(granted["node_id"])
    finally:
        second.stop()


def test_a_grant_on_the_exact_row_survives_a_lineage_row_without_one(tmp_path):
    directory = str(tmp_path / "state")
    first = Server(directory)
    try:
        canonical = _register(first, "e1.5.toe", "rt_one")
        sibling = _seed_exact_row(first, "e1.6.toe", "rt_two")
        assert canonical["node_id"] != sibling["node_id"]
        approve_td_python(first.app, sibling["node_id"])
    finally:
        first.stop()
    second = Server(directory)
    try:
        ids = [r["node_id"] for r in second.app.directory.nodes()]
        assert ids == [sibling["node_id"]]
        assert second.app.policy.allow_td_python(sibling["node_id"])
        record = second.app.directory.lookup(sibling["node_id"])
        assert record["node_discriminator"] ==             identity.toe_lineage_discriminator(ROOT + "/e1.6.toe")
    finally:
        second.stop()


# -- a machine stays on --------------------------------------------------

def _establish(server):
    node = _register(server, "show.toe", "rt_one", port=9981)
    server.app._tick_realm()
    return node


def test_a_machine_stays_on_the_lan_with_no_project_open(server):
    node = _establish(server)
    assert server.app.active_convoy_ids()
    server.call("/unregister", {"node_id": node["node_id"]})
    assert server.app.active_convoy_ids(), "closing TD is not leaving"


def test_a_machine_stays_on_the_lan_after_its_last_row_is_forgotten(server):
    node = _establish(server)
    server.call("/unregister", {"node_id": node["node_id"]})
    code, answer = server.call("/nodes/forget", {"node_id": node["node_id"]})
    assert code == 200, answer
    assert _rows(server) == {}
    assert server.app.active_convoy_ids()


def test_disabling_one_project_does_not_take_the_machine_off(server):
    node = _establish(server)
    server.call("/unregister",
                {"node_id": node["node_id"], "reason": "disabled"})
    assert server.app.active_convoy_ids()


def test_a_machine_that_never_joined_stays_off_the_lan(server):
    assert server.app.active_convoy_ids() == ()


# -- the machine directory ----------------------------------------------

def _machines(server):
    code, body = server.call("/network/nodes", method="GET")
    assert code == 200, body
    return body["machines"]


def test_the_local_machine_is_listed_with_no_project_open(server):
    node = _establish(server)
    server.call("/unregister", {"node_id": node["node_id"]})
    machines = _machines(server)
    assert len(machines) == 1
    assert machines[0]["local"] is True
    assert machines[0]["online"] is True
    assert machines[0]["hostname"] == socket.gethostname()
    assert machines[0]["toes"] == []


def test_only_active_toes_are_listed(server):
    _establish(server)
    closed = _register(server, "closed.toe", "rt_two", port=9982)
    server.call("/unregister", {"node_id": closed["node_id"]})
    assert _machines(server)[0]["toes"] == ["show.toe"]
    assert closed["node_id"] in _rows(server), "known, just not listed"


def test_a_version_up_lists_the_new_file_name_once(server):
    _register(server, "e1.5.toe", "rt_live", port=9981)
    _register(server, "e1.6.toe", "rt_live", port=9981)
    assert _machines(server)[0]["toes"] == ["e1.6.toe"]


# -- helpers -------------------------------------------------------------

def _closed_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class _listening_port:
    """A loopback port that answers, standing in for a live Envoy."""

    def __enter__(self):
        self._socket = socket.socket()
        self._socket.bind(("127.0.0.1", 0))
        self._socket.listen(1)
        return self._socket.getsockname()[1]

    def __exit__(self, *_exc):
        self._socket.close()
