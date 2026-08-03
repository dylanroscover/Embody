"""HostApp composition tests for exact-node Convoy lifecycle operations."""

import importlib.util
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import convoy_hostapp as hostapp


CONVOY = "studio"


class FakeStore:
    def __init__(self):
        self.profiles = {}

    def get_profile(self, node_id):
        return self.profiles.get(node_id)


class FakeLifecycle:
    def __init__(self):
        self.store = FakeStore()
        self.recorded = []
        self.enabled = []
        self.confirmed = []
        self.started = []
        self.restarted = []
        self.exits = []
        self.entered = None
        self.release = None

    def record_registration(self, record, executable, *, launch_eligible=None):
        self.recorded.append((dict(record), executable, launch_eligible))
        self.store.profiles[record["node_id"]] = {
            "enabled": True, "launch_eligible": bool(launch_eligible)}
        return self.store.profiles[record["node_id"]]

    def set_enabled(self, node_id, convoy_id, enabled, *, launch_eligible=None):
        self.enabled.append((node_id, convoy_id, enabled, launch_eligible))
        self.store.profiles.setdefault(node_id, {})
        self.store.profiles[node_id].update({
            "enabled": enabled,
            "launch_eligible": (enabled if launch_eligible is None
                                else launch_eligible),
        })
        return self.store.profiles[node_id]

    def confirm_registration(self, node_id, convoy_id, token, runtime_record):
        self.confirmed.append((node_id, convoy_id, token,
                               dict(runtime_record)))
        return {"ok": True, "code": "ok",
                "runtime_id": runtime_record["runtime_id"]}

    def start_node(self, node_id, convoy_id, operation_id, *, timeout_s=None,
                   execution_timeout_s=None, cancel_event=None):
        self.started.append((node_id, convoy_id, operation_id, timeout_s,
                             cancel_event, execution_timeout_s))
        if self.entered is not None:
            self.entered.set()
        if self.release is not None:
            while not self.release.wait(0.01):
                if cancel_event is not None and cancel_event.is_set():
                    return {"ok": False, "code": "cancelled"}
        return {"ok": True, "code": "ok", "node_id": node_id}

    def restart_node(self, node_id, convoy_id, operation_id,
                     expected_runtime_id, *, policy="require_clean",
                     timeout_s=None, execution_timeout_s=None,
                     cancel_event=None):
        self.restarted.append((node_id, convoy_id, operation_id,
                               expected_runtime_id, policy, timeout_s,
                               cancel_event, execution_timeout_s))
        return {"ok": True, "code": "ok", "node_id": node_id,
                "runtime_id": "runtime-new"}

    def record_runtime_exit(self, node_id, runtime_id):
        self.exits.append((node_id, runtime_id))
        return {"ok": True, "code": "ok"}


def make_app(tmp_path, lifecycle=None, **kwargs):
    lifecycle = lifecycle or FakeLifecycle()
    app = hostapp.HostApp(
        str(tmp_path / "state"), lifecycle_manager=lifecycle, **kwargs)
    return app, lifecycle


def register(app, *, runtime_id="runtime-1", envoy_port=9800,
             perform=False, wake_active=False, td_executable=None):
    body = {
        "project_root": str(app.data_dir),
        "convoy_id": CONVOY,
        "comp_path": "/Embody",
        "runtime_id": runtime_id,
        "metadata": {
            "toe_path": str(app.data_dir) + "/show.toe",
            "process_id": 1234,
            "touchdesigner_version": "2025.30000",
        },
        "perform_mode": perform,
        "wake_active": wake_active,
        "remote_wake": perform,
        "envoy_ready": bool(envoy_port),
    }
    if envoy_port:
        body["envoy_port"] = envoy_port
    if perform:
        body.update({"wake_port": 19000,
                     "wake_token": "w" * 32})
    if td_executable:
        body["td_executable"] = td_executable
    code, result = app.register_node(body)
    assert code == 200, result
    return result


def enqueue(app, node, operation, *, arguments=None,
            expected_runtime_id=None, key="key"):
    body = {"idempotency_key": key, "node_id": node["node_id"],
            "operation": operation, "arguments": arguments or {}}
    if expected_runtime_id is not None:
        body["expected_runtime_id"] = expected_runtime_id
    with app.lock:
        code, result = app.create_job(body)
    assert code == 200, result
    return result["job"]


def test_normal_registration_records_only_host_verified_launch_inputs(tmp_path):
    app, lifecycle = make_app(tmp_path)
    try:
        node = register(app, td_executable="C:/Touch/TouchDesigner.exe")
        assert len(lifecycle.recorded) == 1
        record, executable, eligible = lifecycle.recorded[0]
        assert record["node_id"] == node["node_id"]
        assert executable == "C:/Touch/TouchDesigner.exe"
        assert eligible is True
        assert lifecycle.enabled[-1] == (
            node["node_id"], CONVOY, True, True)
    finally:
        app.db.close()


def test_start_node_dispatches_host_native_while_td_is_offline(tmp_path):
    app, lifecycle = make_app(tmp_path)
    try:
        node = register(app)
        with app.lock:
            code, _ = app.unregister_node({
                "node_id": node["node_id"],
                "runtime_id": "runtime-1", "reason": "shutdown"})
        assert code == 200
        job = enqueue(app, node, "convoy_start_node",
                      arguments={"timeout_s": 4}, key="start")
        app.forwarder = lambda *_: (_ for _ in ()).throw(
            AssertionError("lifecycle start must not forward through Envoy"))
        code, result = app.dispatch_job(job["delivery_id"])
        assert code == 200 and result["job"]["state"] == "succeeded", result
        assert lifecycle.started[0][:4] == (
            node["node_id"], CONVOY, job["delivery_id"], 4)
    finally:
        app.db.close()


def test_restart_passes_exact_runtime_and_policy_to_lifecycle(tmp_path):
    app, lifecycle = make_app(tmp_path)
    try:
        node = register(app)
        job = enqueue(
            app, node, "convoy_restart_node",
            arguments={"timeout_s": 5, "policy": "save_then_restart"},
            expected_runtime_id="runtime-1", key="restart")
        code, result = app.dispatch_job(job["delivery_id"])
        assert code == 200 and result["job"]["state"] == "succeeded", result
        assert lifecycle.restarted[0][:6] == (
            node["node_id"], CONVOY, job["delivery_id"], "runtime-1",
            "save_then_restart", 5)
    finally:
        app.db.close()


def test_lifecycle_keeps_requested_timeout_but_uses_remaining_delivery_budget(
        tmp_path):
    app, lifecycle = make_app(tmp_path)
    try:
        node = register(app)
        job = enqueue(
            app, node, "convoy_restart_node",
            arguments={"timeout_s": 5, "policy": "require_clean"},
            expected_runtime_id="runtime-1", key="restart-budget")
        app.db.job_timing = lambda *_args, **_kwargs: {
            "bounded": True, "expired": False, "remaining_s": .25,
            "reason": None}

        code, result = app.dispatch_job(job["delivery_id"])

        assert code == 200 and result["job"]["state"] == "succeeded"
        assert lifecycle.restarted[0][5] == 5
        assert lifecycle.restarted[0][7] == .25
    finally:
        app.db.close()


def test_delivery_expiring_after_claim_never_enters_lifecycle_manager(tmp_path):
    app, lifecycle = make_app(tmp_path)
    try:
        node = register(app)
        job = enqueue(
            app, node, "convoy_restart_node", arguments={"timeout_s": 5},
            expected_runtime_id="runtime-1", key="restart-expired")
        calls = []

        def timing(*_args, **_kwargs):
            calls.append(True)
            if len(calls) == 1:
                return {"bounded": True, "expired": False,
                        "remaining_s": .01, "reason": None}
            return {"bounded": True, "expired": True,
                    "remaining_s": 0.0, "reason": "deadline_exceeded"}

        app.db.job_timing = timing
        code, response = app.dispatch_job(job["delivery_id"])

        assert code == 200
        assert response["job"]["state"] == "failed"
        assert response["job"]["result"]["code"] == "deadline_exceeded"
        assert lifecycle.restarted == []
    finally:
        app.db.close()


def test_sleeping_perform_restart_wakes_before_lifecycle_dispatch(tmp_path):
    wakes = []

    def waker(port, token, action, lease_id, ttl_s):
        wakes.append((port, token, action, lease_id, ttl_s))
        return {"ok": True}

    app, lifecycle = make_app(tmp_path, waker=waker)
    try:
        node = register(app, envoy_port=None, perform=True,
                        wake_active=False)
        job = enqueue(app, node, "convoy_restart_node",
                      expected_runtime_id="runtime-1", key="wake-restart")
        code, result = app.dispatch_job(job["delivery_id"])
        assert code == 409 and result["reason"] == "node_waking"
        assert lifecycle.restarted == []
        assert wakes and wakes[0][2] == "acquire"
    finally:
        app.db.close()


def test_active_lifecycle_job_receives_cancellation_event(tmp_path):
    lifecycle = FakeLifecycle()
    lifecycle.entered = threading.Event()
    lifecycle.release = threading.Event()
    app, _ = make_app(tmp_path, lifecycle=lifecycle)
    try:
        node = register(app)
        with app.lock:
            app.directory.clear_envoy_port(node["node_id"])
        job = enqueue(app, node, "convoy_start_node", key="cancel-start")
        result = {}

        def dispatch():
            result["value"] = app.dispatch_job(job["delivery_id"])

        thread = threading.Thread(target=dispatch)
        thread.start()
        assert lifecycle.entered.wait(2)
        code, cancellation = app.cancel_host_job({
            "delivery_id": job["delivery_id"]})
        assert code == 202 and cancellation["cancel_requested"] is True
        thread.join(2)
        assert not thread.is_alive()
        assert result["value"][1]["job"]["state"] == "failed"
        assert lifecycle.started[0][4].is_set()
    finally:
        lifecycle.release.set()
        app.db.close()


def test_live_launch_reservation_rejects_manual_claim_and_accepts_match(tmp_path):
    app, lifecycle = make_app(tmp_path)
    try:
        node = register(app)
        with app.lock:
            app.directory.clear_envoy_port(node["node_id"])
        reservation = app.lifecycle_runtime.reserve_launch(
            node["node_id"], "unit-1", "operation-1", 5, None)
        assert reservation["ok"] is True
        base = {
            "project_root": str(app.data_dir), "convoy_id": CONVOY,
            "comp_path": "/Embody", "runtime_id": "runtime-2",
            "metadata": {"toe_path": str(app.data_dir) + "/show.toe",
                         "process_id": 2345,
                         "touchdesigner_version": "2025.30000"},
        }
        code, refused = app.register_node(dict(base))
        assert code == 409
        assert refused["reason"] == "launch_reservation_mismatch"

        token = "t" * 43
        accepted = dict(base, launch_token=token,
                        launch_reservation_id=reservation["reservation_id"])
        code, result = app.register_node(accepted)
        assert code == 200, result
        assert lifecycle.confirmed[0][0:3] == (
            node["node_id"], CONVOY, token)
        assert lifecycle.confirmed[0][3]["launch_reservation_id"] == \
            reservation["reservation_id"]
    finally:
        app.db.close()


def test_restored_launch_reservation_fences_manual_registration(tmp_path):
    app, lifecycle = make_app(tmp_path)
    try:
        node = register(app)
        with app.lock:
            app.directory.clear_envoy_port(node["node_id"])
        restored = app.lifecycle_runtime.restore_launch_reservations([{
            "node_id": node["node_id"],
            "launch_unit_id": "a" * 64,
            "operation_id": "operation-restored",
            "reservation_id": "lr_" + "r" * 32,
        }])
        assert restored == {"ok": True, "restored": 1}
        base = {
            "project_root": str(app.data_dir), "convoy_id": CONVOY,
            "comp_path": "/Embody", "runtime_id": "runtime-restored",
            "metadata": {"toe_path": str(app.data_dir) + "/show.toe",
                         "process_id": 3456,
                         "touchdesigner_version": "2025.30000"},
        }
        code, refused = app.register_node(dict(base))
        assert code == 409
        assert refused["reason"] == "launch_reservation_mismatch"

        accepted = dict(
            base, launch_token="t" * 43,
            launch_reservation_id="lr_" + "r" * 32)
        code, result = app.register_node(accepted)
        assert code == 200, result
        assert lifecycle.confirmed[-1][0:3] == (
            node["node_id"], CONVOY, "t" * 43)
        validated = app.lifecycle_runtime.confirm_launch_reservation(
            node["node_id"], "a" * 64, "operation-restored",
            "lr_" + "r" * 32, "runtime-restored")
        assert validated["ok"] is True
        assert app.lifecycle_runtime.reservation_for_node(node["node_id"])
        released = app.lifecycle_runtime.release_launch_reservation(
            node["node_id"], "a" * 64, "operation-restored",
            "lr_" + "r" * 32, "confirmed")
        assert released["ok"] is True
        assert app.lifecycle_runtime.reservation_for_node(
            node["node_id"]) is None
    finally:
        app.db.close()


def test_default_hostapp_restores_durable_fences_during_construction(
        tmp_path, monkeypatch):
    calls = []

    class StartupLifecycle(FakeLifecycle):
        def __init__(self, store, runtime, **kwargs):
            super().__init__()
            self.runtime = runtime

        def restore_launch_reservations(self):
            calls.append("restored")
            return {"ok": True, "restored": 0}

    monkeypatch.setattr(
        hostapp.lifecycle_mod, "LifecycleManager", StartupLifecycle)
    app = hostapp.HostApp(str(tmp_path / "state"))
    try:
        assert calls == ["restored"]
        assert isinstance(app.lifecycle, StartupLifecycle)
    finally:
        app.db.close()


def test_hostapp_reconciles_boot_indeterminate_from_lifecycle_ledger(tmp_path):
    first, _ = make_app(tmp_path)
    node = register(first)
    job = enqueue(
        first, node, "convoy_restart_node",
        expected_runtime_id="runtime-1", key="restart-crash")
    with first.lock:
        first.db.claim_for_dispatch(job["delivery_id"])
    first.db.close()

    result = {
        "ok": True, "code": "ok", "detail": "operation completed",
        "capability": "host.td-lifecycle/v1",
        "node_id": node["node_id"],
        "operation_id": job["delivery_id"],
        "runtime_id": "runtime-restored",
        "deadline_deferred": True,
    }

    class RecoveryLifecycle(FakeLifecycle):
        def recover_committed_restarts(self, result_callback=None, **kwargs):
            result_callback(
                {"operation_id": job["delivery_id"],
                 "node_id": node["node_id"], "convoy_id": CONVOY,
                 "old_runtime_id": "runtime-1"}, result)
            return {"examined": 1, "recovered": 0, "reconciled": 1,
                    "busy": 0, "errors": 0, "results": [{
                        "operation_id": job["delivery_id"],
                        "result": result}]}

    recovered, _ = make_app(tmp_path, lifecycle=RecoveryLifecycle())
    try:
        assert recovered.db.get_job(job["delivery_id"])["state"] == \
            "indeterminate"
        summary = recovered.recover_lifecycle_once()
        durable = recovered.db.get_job(job["delivery_id"])
        assert summary["available"] is True
        assert durable["state"] == "succeeded"
        assert durable["verdict_source"] == "host_operation_recovery"
        assert durable["result"] == result
    finally:
        recovered.db.close()


def test_lifecycle_recovery_loop_runs_immediately_and_on_interval(tmp_path):
    observed = threading.Event()

    class PeriodicLifecycle(FakeLifecycle):
        def __init__(self):
            super().__init__()
            self.passes = 0

        def recover_committed_restarts(self, result_callback=None, **kwargs):
            self.passes += 1
            if self.passes >= 2:
                observed.set()
            return {"examined": 0, "recovered": 0, "reconciled": 0,
                    "busy": 0, "errors": 0, "results": []}

    lifecycle = PeriodicLifecycle()
    app, _ = make_app(tmp_path, lifecycle=lifecycle)
    try:
        assert app.start_lifecycle_recovery_loop(.02) is True
        assert observed.wait(2)
        assert app.stop_lifecycle_recovery_loop(timeout_s=2) is True
        assert lifecycle.passes >= 2
    finally:
        app.stop_lifecycle_recovery_loop(timeout_s=2)
        app.db.close()


def test_lifecycle_arguments_refuse_remote_launch_inputs(tmp_path):
    app, _ = make_app(tmp_path)
    try:
        node = register(app)
        for operation, arguments, runtime in (
                ("convoy_start_node", {"toe_path": "C:/other.toe"}, None),
                ("convoy_start_node", {"operation_id": "caller"}, None),
                ("convoy_restart_node", {"policy": "guess"}, "runtime-1"),
                ("convoy_restart_node", {"policy": "discard_and_restart"},
                 "runtime-1"),
                ("convoy_restart_node", {"policy": "force"}, "runtime-1"),
                ("convoy_restart_node", {"timeout_s": 901}, "runtime-1")):
            body = {"idempotency_key": operation + str(arguments),
                    "node_id": node["node_id"], "operation": operation,
                    "arguments": arguments}
            if runtime:
                body["expected_runtime_id"] = runtime
            with app.lock:
                code, result = app.create_job(body)
            assert code == 400, result
            assert result["reason"] == "malformed"
    finally:
        app.db.close()


def test_dead_registered_runtime_is_atomically_reconciled_offline(tmp_path):
    app, lifecycle = make_app(tmp_path)
    try:
        node = register(app, td_executable="C:/Touch/TouchDesigner.exe")
        process = {
            "pid": 1234, "executable_path": "C:/Touch/TouchDesigner.exe",
            "birth_id": "birth:1234", "user_id": "user:1",
            "session_id": "session:1"}
        lifecycle.store.profiles[node["node_id"]].update({
            "last_runtime": {"runtime_id": "runtime-1",
                             "process": process}})
        lifecycle.inspector = SimpleNamespace(
            inspect_status=lambda pid: {"status": "dead"})

        assert app.lifecycle_runtime.current(node["node_id"]) is None
        record = app.directory.lookup(node["node_id"])
        assert record["runtime_id"] is None
        assert record["envoy_port"] is None
    finally:
        app.db.close()


def test_lifecycle_save_key_is_stable_per_operation_not_per_runtime(tmp_path):
    app, _ = make_app(tmp_path)
    try:
        node = register(app)
        keys = []

        def call(_node_id, _runtime_id, operation, arguments, _timeout,
                 _cancel):
            if operation == "save_project":
                keys.append(arguments["idempotency_key"])
                return {"job_id": "job_deadbeef"}
            return {"status": "done", "result": {}}

        app.lifecycle_runtime._call = call
        for operation_id in ("restart-one", "restart-one", "restart-two"):
            app.lifecycle_runtime.begin_operation(operation_id)
            try:
                result = app.lifecycle_runtime.save(
                    node["node_id"], "runtime-1", .2, threading.Event())
                assert result["ok"] is True
            finally:
                app.lifecycle_runtime.end_operation()

        assert keys[0] == keys[1]
        assert keys[2] != keys[0]
    finally:
        app.db.close()


def test_cancelling_after_save_acceptance_reports_save_may_have_run(tmp_path):
    app, _ = make_app(tmp_path)
    try:
        node = register(app)
        cancel = threading.Event()

        def call(_node_id, _runtime_id, operation, _arguments, _timeout,
                 _cancel):
            assert operation == "save_project"
            cancel.set()
            return {"job_id": "job_deadbeef"}

        app.lifecycle_runtime._call = call
        app.lifecycle_runtime.begin_operation("restart-cancel-save")
        try:
            result = app.lifecycle_runtime.save(
                node["node_id"], "runtime-1", .2, cancel)
        finally:
            app.lifecycle_runtime.end_operation()

        assert result == {
            "ok": False, "code": "cancelled", "job_id": "job_deadbeef",
            "save_may_have_run": True}
    finally:
        app.db.close()


def _load_envoy_ext_module():
    path = (Path(__file__).resolve().parents[1] / "embody" / "Embody" /
            "EnvoyExt.py")
    spec = importlib.util.spec_from_file_location(
        "convoy_lifecycle_envoy_contract", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_delayed_lifecycle_quit_rechecks_revision_at_commit_boundary():
    """An edit in the two response frames must never be force-discarded."""
    module = _load_envoy_ext_module()
    extension = object.__new__(module.EnvoyExt)
    extension.ownerComp = SimpleNamespace(path="/Embody")
    logs = []
    extension._log = lambda message, level: logs.append((message, level))
    scheduled = []
    module.run = lambda expression, **kwargs: scheduled.append(
        (expression, kwargs))
    quits = []
    module.project = SimpleNamespace(
        quit=lambda **kwargs: quits.append(kwargs))

    extension._convoy_lifecycle_state = lambda: {
        "ok": True, "dirty": False, "unsaved": False,
        "revision": "clean-revision"}
    accepted = extension._convoy_lifecycle_quit("clean-revision")
    assert accepted["ok"] is True
    assert len(scheduled) == 1
    assert "_convoy_lifecycle_commit_quit" in scheduled[0][0]
    assert quits == []

    # TouchDesigner can cook/edit between the acknowledgement and the
    # delayed callback.  The callback must compare again at that boundary.
    extension._convoy_lifecycle_state = lambda: {
        "ok": True, "dirty": True, "unsaved": False,
        "revision": "late-edit"}
    refused = extension._convoy_lifecycle_commit_quit("clean-revision")
    assert refused["code"] == "dirty_revision_changed"
    assert quits == []
    assert logs[-1][1] == "WARNING"


def test_delayed_lifecycle_quit_commits_only_unchanged_clean_revision():
    module = _load_envoy_ext_module()
    extension = object.__new__(module.EnvoyExt)
    extension.ownerComp = SimpleNamespace(path="/Embody")
    extension._log = lambda *_: None
    module.run = lambda *args, **kwargs: None
    quits = []
    module.project = SimpleNamespace(
        quit=lambda **kwargs: quits.append(kwargs))
    extension._convoy_lifecycle_state = lambda: {
        "ok": True, "dirty": False, "unsaved": False,
        "revision": "same-revision"}

    committed = extension._convoy_lifecycle_commit_quit("same-revision")
    assert committed["ok"] is True
    assert quits == [{"force": True}]
