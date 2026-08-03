"""End-to-end HostApp contracts for Perform-safe wake leases."""

from __future__ import annotations

import threading
import time

import convoy_hostapp as hostapp


CONVOY = "studio"
TOKEN = "A" * 43
RUNTIME = "rt_sleeping"


def _register(app, *, awake=False, remote_wake=True):
    body = {
        "project_root": "/Work/p",
        "convoy_id": CONVOY,
        "comp_path": "/Embody",
        "runtime_id": RUNTIME,
        "envoy_ready": bool(awake),
        "remote_wake": bool(remote_wake),
        "perform_mode": True,
        "wake_active": bool(awake),
        "wake_grace_s": 60,
    }
    if remote_wake:
        body.update(wake_port=47631, wake_token=TOKEN)
    if awake:
        body["envoy_port"] = 9800
    code, response = app.register_node(body)
    assert code == 200, response
    return response


def _enqueue(app, node, operation="query_network", key="wake-key"):
    with app.lock:
        code, response = app.create_job({
            "idempotency_key": key,
            "node_id": node["node_id"],
            "operation": operation,
            "arguments": ({"parent_path": "/"}
                          if operation == "query_network" else {}),
            "expected_runtime_id": RUNTIME,
        })
    assert code == 200, response
    return response["job"]


def test_sleeping_dispatch_wakes_then_runs_and_releases_after_grace(tmp_path):
    app = hostapp.HostApp(str(tmp_path / "state"))
    app.drain_backoff_s = 30.0
    calls = []
    app.waker = lambda port, token, action, lease, ttl: (
        calls.append((port, token, action, lease, ttl))
        or {"ok": True})
    try:
        node = _register(app)
        job = _enqueue(app, node)
        code, response = app.dispatch_job(job["delivery_id"])
        assert code == 409 and response["reason"] == "node_waking"
        assert calls[0][2] == "acquire"
        assert app.db.get_job(job["delivery_id"])["state"] == "queued"
        assert app._drain_backoff[job["delivery_id"]] \
            <= app._now() + hostapp.WAKE_RETRY_S + 0.1

        awake = _register(app, awake=True)
        assert awake["node_id"] == node["node_id"]
        app.forwarder = lambda port, operation, arguments: {
            "ok": True, "result": {"ops": ["/a"]}}
        code, response = app.dispatch_job(job["delivery_id"])
        assert code == 200
        assert response["job"]["state"] == "succeeded"
        assert [call[2] for call in calls] == ["acquire", "touch", "release"]
        assert all(call[1] == TOKEN for call in calls)
    finally:
        app.db.close()


def test_convoy_ping_never_wakes_a_sleeping_node(tmp_path):
    app = hostapp.HostApp(str(tmp_path / "state"))
    calls = []
    app.waker = lambda *args: calls.append(args) or {"ok": True}
    try:
        node = _register(app)
        job = _enqueue(app, node, operation="convoy_ping", key="ping")
        code, response = app.dispatch_job(job["delivery_id"])
        assert code == 200
        assert response["job"]["result"]["pong"] is True
        assert response["job"]["result"]["online"] is True
        assert calls == []
    finally:
        app.db.close()


def test_disabled_remote_wake_keeps_real_work_queued(tmp_path):
    app = hostapp.HostApp(str(tmp_path / "state"))
    calls = []
    app.waker = lambda *args: calls.append(args) or {"ok": True}
    try:
        node = _register(app, remote_wake=False)
        job = _enqueue(app, node)
        code, response = app.dispatch_job(job["delivery_id"])
        assert code == 409
        assert response["reason"] == "remote_wake_disabled"
        assert app.db.get_job(job["delivery_id"])["state"] == "queued"
        assert calls == []
    finally:
        app.db.close()


def test_wake_registration_is_closed_schema_and_never_returns_the_token(
        tmp_path):
    app = hostapp.HostApp(str(tmp_path / "state"))
    base = {
        "project_root": "/Work/p", "convoy_id": CONVOY,
        "comp_path": "/Embody", "runtime_id": RUNTIME,
        "envoy_ready": False, "remote_wake": True,
        "perform_mode": True, "wake_active": False,
        "wake_grace_s": 60,
    }
    try:
        bad = (
            {**base, "wake_port": 47631},
            {**base, "wake_port": 47631, "wake_token": "short"},
            {**base, "wake_port": 47631, "wake_token": TOKEN,
             "remote_wake": False},
            {**base, "wake_active": True, "perform_mode": False},
            {**base, "envoy_ready": True},
            {**base, "wake_grace_s": 3601},
        )
        for body in bad:
            code, response = app.register_node(body)
            assert code == 400, (body, response)
            assert response["reason"] == "malformed"
        code, response = app.register_node({
            **base, "wake_port": 47631, "wake_token": TOKEN})
        assert code == 200
        assert response["wake_ready"] is True
        assert "wake_token" not in response
        assert TOKEN not in repr(response)
    finally:
        app.db.close()


def test_sleeping_node_is_online_without_disclosing_its_wake_credential(
        tmp_path):
    app = hostapp.HostApp(str(tmp_path / "state"))
    try:
        node = _register(app)
        record = app.directory.lookup(node["node_id"])
        assert app._node_is_online(record) is True
        row = app._public_node_row(record, address="10.0.0.2")
        assert row["online"] is True
        assert row["sleeping"] is True
        assert row["wake_active"] is False
        assert "wake_token" not in row and TOKEN not in repr(row)
    finally:
        app.db.close()


def test_queued_wake_is_released_when_job_is_cancelled(tmp_path):
    app = hostapp.HostApp(str(tmp_path / "state"))
    calls = []
    app.waker = lambda *args: calls.append(args) or {"ok": True}
    try:
        node = _register(app)
        job = _enqueue(app, node)
        code, response = app.dispatch_job(job["delivery_id"])
        assert code == 409 and response["reason"] == "node_waking"

        code, response = app.cancel_host_job({
            "delivery_id": job["delivery_id"]})
        assert code == 200 and response["cancelled"] is True
        assert [call[2] for call in calls] == ["acquire", "release"]
        assert calls[0][3] == calls[1][3] == job["delivery_id"]
    finally:
        app.db.close()


def test_long_awake_dispatch_refreshes_lease_until_terminal_release(
        tmp_path, monkeypatch):
    monkeypatch.setattr(hostapp, "WAKE_REFRESH_MAX_S", 0.05)
    app = hostapp.HostApp(str(tmp_path / "state"))
    calls = []
    lock = threading.Lock()
    release = threading.Event()

    def waker(*args):
        with lock:
            calls.append(args)
        return {"ok": True}

    def forwarder(*_args):
        assert release.wait(2)
        return {"ok": True, "result": {"ops": []}}

    app.waker = waker
    app.forwarder = forwarder
    try:
        node = _register(app, awake=True)
        job = _enqueue(app, node)
        result = {}
        thread = threading.Thread(
            target=lambda: result.setdefault(
                "response", app.dispatch_job(job["delivery_id"])))
        thread.start()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            with lock:
                touches = [call for call in calls if call[2] == "touch"]
            if len(touches) >= 2:
                break
            time.sleep(.01)
        release.set()
        thread.join(2)

        assert not thread.is_alive()
        assert result["response"][1]["job"]["state"] == "succeeded"
        with lock:
            actions = [call[2] for call in calls]
        assert actions[0] == "touch"
        assert actions.count("touch") >= 2
        assert actions[-1] == "release"
    finally:
        release.set()
        app.db.close()
