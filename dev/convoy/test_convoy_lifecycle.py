"""Fail-closed exact-node lifecycle tests.

Both OS branches are dependency-injected so Windows/macOS safety invariants run
on every CI host.  A separate hardware matrix remains required before release.
"""

import os
import signal
import threading
import time

import pytest

import convoy_lifecycle as cl


NODE = "a" * 32
NODE_2 = "c" * 32
HOST = "b" * 32
CONVOY = "convoy-test"
TOKEN = "T" * 43


class StepClock:
    def __init__(self, value=1000.0, step=0.02):
        self.value = value
        self.step = step

    def __call__(self):
        self.value += self.step
        return self.value


class ManualMonotonic:
    def __init__(self, value=500.0):
        self.value = value

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += float(seconds)


def fake_time(manager):
    """Run a thread-free manager on injected time: deadlines and sleeps
    both on the fake clock, so deadline SEMANTICS are deterministic on
    any runner speed (a stalled CI runner converts real-clock budgets
    into deadline_exceeded wherever the stall lands -- three rounds of
    windows-latest flakes). Only for tests with NO real threads: a real
    thread runs on wall time and would race the instantly-advancing
    fake clock."""
    mm = ManualMonotonic()
    manager._monotonic = mm
    manager._sleep = mm.advance
    return mm


class FakeBackend:
    def __init__(self):
        self.processes = {}
        self.session = {"user_id": "user:1", "session_id": "session:1",
                        "interactive": True}
        self.terminated = []

    def inspect(self, pid):
        value = self.processes.get(pid)
        return dict(value) if value else None

    def inspect_status(self, pid):
        value = self.processes.get(pid)
        return ({"status": "alive", "process": dict(value)} if value
                else {"status": "dead"})

    def current_session(self):
        return dict(self.session) if self.session else None

    def terminate(self, pid, *, force):
        self.terminated.append((pid, force))
        self.processes.pop(pid, None)
        return True


class FakePopen:
    def __init__(self, pid):
        self.pid = pid
        self.returncode = None
        self.terminate_calls = 0

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminate_calls += 1
        self.returncode = -1


class FakeLauncher:
    def __init__(self, inspector, pid=200, callback=None, cancel_event=None):
        self.inspector = inspector
        self.pid = pid
        self.callback = callback
        self.cancel_event = cancel_event
        self.spawns = []
        self.cancels = []

    def spawn(self, profile, token, reservation_id):
        snapshot = {
            "pid": self.pid, "executable_path": profile["executable_path"],
            "birth_id": "birth:%d" % self.pid,
            "user_id": profile["session"]["user_id"],
            "session_id": profile["session"]["session_id"],
        }
        self.inspector.backend.processes[self.pid] = snapshot
        spawned = cl.SpawnedProcess(
            FakePopen(self.pid), snapshot, reservation_id)
        self.spawns.append((profile["node_id"], token, spawned))
        if self.cancel_event is not None:
            self.cancel_event.set()
        if self.callback is not None:
            # Registration is an independent HostApp request and therefore
            # cannot run synchronously inside Popen.  The short timer models
            # that thread boundary and lets the durable spawned-PID fence land.
            threading.Timer(.001, self.callback,
                            args=(profile, token, spawned)).start()
        return spawned

    def cancel(self, spawned, *, timeout_s):
        self.cancels.append((spawned.pid, timeout_s))
        self.inspector.backend.processes.pop(spawned.pid, None)
        return True


class Runtime:
    def __init__(self):
        self.value = None
        self.dirty_values = [{"ok": True, "dirty": False}]
        self.saved = []
        self.quit_calls = []
        self.inspector = None
        self.quit_event = None
        self.reservations = {}

    def current(self, node_id):
        return dict(self.value) if self.value else None

    def dirty(self, node_id, runtime_id, timeout_s, cancel_event):
        value = self.dirty_values.pop(0) if len(self.dirty_values) > 1 \
            else self.dirty_values[0]
        value = dict(value)
        if value.get("ok") is True:
            value.setdefault("unsaved", False)
            value.setdefault("revision", "revision:1")
        return value

    def save(self, node_id, runtime_id, timeout_s, cancel_event):
        self.saved.append((node_id, runtime_id))
        return {"ok": True}

    def quit(self, node_id, runtime_id, timeout_s, cancel_event, *, discard,
             expected_dirty_revision=None):
        self.quit_calls.append((node_id, runtime_id, discard,
                                expected_dirty_revision))
        if self.inspector and self.value:
            self.inspector.backend.processes.pop(self.value["process_id"], None)
        self.value = None
        if self.quit_event:
            self.quit_event.set()
        return {"ok": True}

    def reserve_launch(self, node_id, launch_unit_id, operation_id,
                       timeout_s, cancel_event):
        existing = self.reservations.get(launch_unit_id)
        if existing is not None and existing["operation_id"] != operation_id:
            return {"ok": False, "code": "busy"}
        reservation_id = (existing["reservation_id"] if existing else
                          "reservation:" + operation_id)
        self.reservations[launch_unit_id] = {
            "operation_id": operation_id,
            "reservation_id": reservation_id}
        return {"ok": True, "reservation_id": reservation_id}

    def confirm_launch_reservation(self, node_id, launch_unit_id, operation_id,
                                   reservation_id, runtime_id):
        existing = self.reservations.get(launch_unit_id)
        if existing != {"operation_id": operation_id,
                        "reservation_id": reservation_id}:
            return {"ok": False}
        return {"ok": True}

    def restore_launch_reservations(self, reservations):
        restored = {}
        for value in reservations:
            restored[value["launch_unit_id"]] = {
                "operation_id": value["operation_id"],
                "reservation_id": value["reservation_id"],
            }
        self.reservations = restored
        return {"ok": True, "restored": len(restored)}

    def release_launch_reservation(self, node_id, launch_unit_id, operation_id,
                                   reservation_id, outcome):
        existing = self.reservations.get(launch_unit_id)
        if existing == {"operation_id": operation_id,
                        "reservation_id": reservation_id}:
            self.reservations.pop(launch_unit_id, None)
        return {"ok": True}


def make_files(tmp_path, stem="project"):
    root = tmp_path / stem
    root.mkdir()
    toe = root / (stem + ".toe")
    exe = root / ("TouchDesigner.exe" if os.name == "nt" else "TouchDesigner")
    toe.write_bytes(b"toe-v1")
    exe.write_bytes(b"exe-v1")
    return root, toe, exe


def live_record(profile, runtime_id, pid, reservation_id):
    return {"node_id": profile["node_id"], "host_id": profile["host_id"],
            "convoy_id": profile["convoy_id"],
            "comp_path": profile["comp_path"], "runtime_id": runtime_id,
            "process_id": pid, "launch_reservation_id": reservation_id,
            "metadata": {"toe_path": profile["toe_path"],
                         "touchdesigner_version": profile["td_build"]}}


def make_system(tmp_path, *, node=NODE, platform="win32", launcher=None,
                local_policy=None, step_clock=True):
    root, toe, exe = make_files(tmp_path, node[:4])
    backend = FakeBackend()
    inspector = cl.LocalProcessInspector(platform=platform, backend=backend,
                                         sleep=lambda _: None)
    runtime = Runtime()
    runtime.inspector = inspector
    runtime.value = {"node_id": node, "host_id": HOST,
                     "convoy_id": CONVOY, "comp_path": "/project1/Embody",
                     "runtime_id": "runtime-1", "process_id": 100,
                     "metadata": {"toe_path": str(toe),
                                  "touchdesigner_version": "2025.30000"}}
    backend.processes[100] = {
        "pid": 100, "executable_path": str(exe), "birth_id": "birth:100",
        "user_id": "user:1", "session_id": "session:1",
    }
    clock = StepClock()
    store = cl.LaunchProfileStore(str(tmp_path / "state"), clock=clock)
    launcher = launcher or FakeLauncher(inspector)
    manager = cl.LifecycleManager(
        store, runtime, process_inspector=inspector, launcher=launcher,
        local_policy=local_policy, clock=clock,
        monotonic=time.monotonic, sleep=time.sleep,
        token_factory=lambda: TOKEN)
    record = {
        "node_id": node, "host_id": HOST, "convoy_id": CONVOY,
        "project_root": str(root), "comp_path": "/project1/Embody",
        "runtime_id": "runtime-1", "process_id": 100, "enabled": True,
        "metadata": {"toe_path": str(toe),
                     "touchdesigner_version": "2025.30000"},
    }
    manager.record_registration(record, str(exe), launch_eligible=True)
    return manager, store, runtime, inspector, launcher, root, toe, exe


def offline(system):
    manager, _, runtime, inspector, _, _, _, _ = system
    runtime.value = None
    inspector.backend.processes.pop(100, None)
    return manager


def auto_confirm(manager, runtime_id="runtime-new"):
    def callback(profile, token, spawned):
        manager.confirm_registration(
            profile["node_id"], profile["convoy_id"], token,
            live_record(profile, runtime_id, spawned.pid,
                        spawned.reservation_id))
    return callback


def stage_awaiting_registration(manager, store, runtime, inspector,
                                operation_id):
    """Create the durable post-Popen/pre-registration crash boundary."""
    profile = store.get_profile(NODE)
    content = {"operation": "start", "node_id": NODE,
               "convoy_id": CONVOY, "timeout_s": .1}
    attempt, _ = store.begin_attempt(operation_id, content)
    launch_unit_id = cl._launch_unit_id(profile)
    attempt, _ = store.update_attempt(
        operation_id, expected_states={"created"}, state="launching",
        token_hash=cl._token_hash(TOKEN), token_consumed=False,
        profile_digest=cl._launch_profile_digest(profile),
        launch_unit_id=launch_unit_id,
        launch_started_at=manager._clock())
    reserved = runtime.reserve_launch(
        NODE, launch_unit_id, operation_id, .1, None)
    attempt, _ = store.update_attempt(
        operation_id, expected_states={"launching"},
        reservation_id=reserved["reservation_id"])
    process = {
        "pid": 200, "executable_path": profile["executable_path"],
        "birth_id": "birth:200", "user_id": profile["session"]["user_id"],
        "session_id": profile["session"]["session_id"],
    }
    inspector.backend.processes[200] = dict(process)
    attempt, _ = store.update_attempt(
        operation_id, expected_states={"launching"},
        state="awaiting_registration", spawned_pid=200,
        spawned_process=process, spawned_at=manager._clock())
    record = live_record(
        profile, "runtime-restored", 200, reserved["reservation_id"])
    return attempt, profile, record


# -- profile registration and persistent proof -----------------------


def test_registration_pins_exact_executable_toe_process_and_session(tmp_path):
    manager, store, _, _, _, root, toe, exe = make_system(tmp_path)
    profile = store.get_profile(NODE)
    assert profile["project_root"] == os.path.realpath(root)
    assert profile["toe_path"] == os.path.realpath(toe)
    assert profile["executable_path"] == os.path.realpath(exe)
    assert profile["last_runtime"]["process"]["birth_id"] == "birth:100"
    assert profile["session"] == {"user_id": "user:1",
                                  "session_id": "session:1",
                                  "interactive": True}
    assert profile["td_build"] == "2025.30000"


def test_registration_rejects_claimed_executable_not_matching_live_process(tmp_path):
    manager, _, runtime, inspector, _, root, toe, exe = make_system(tmp_path)
    wrong = root / "Other.exe"
    wrong.write_bytes(b"other")
    record = {"node_id": NODE, "host_id": HOST, "convoy_id": CONVOY,
              "project_root": str(root), "comp_path": "/x",
              "runtime_id": "runtime-1", "process_id": 100,
              "metadata": {"toe_path": str(toe)}}
    with pytest.raises(cl.LifecycleError) as caught:
        manager.record_registration(record, str(wrong))
    assert caught.value.code == "runtime_unverifiable"


def test_registration_rejects_toe_outside_project_root(tmp_path):
    manager, _, _, _, _, root, _, exe = make_system(tmp_path)
    outside = tmp_path / "outside.toe"
    outside.write_bytes(b"x")
    record = {"node_id": NODE, "host_id": HOST, "convoy_id": CONVOY,
              "project_root": str(root), "comp_path": "/x",
              "runtime_id": "runtime-1", "process_id": 100,
              "metadata": {"toe_path": str(outside)}}
    with pytest.raises(cl.LifecycleError) as caught:
        manager.record_registration(record, str(exe))
    assert caught.value.code == "invalid_arguments"


@pytest.mark.parametrize("session", [None,
    {"user_id": "user:1", "session_id": "session:1", "interactive": False},
    {"user_id": "user:2", "session_id": "session:1", "interactive": True},
    {"user_id": "user:1", "session_id": "session:2", "interactive": True}])
def test_registration_refuses_unavailable_or_mismatched_session(tmp_path, session):
    manager, _, _, inspector, _, root, toe, exe = make_system(tmp_path)
    inspector.backend.session = session
    record = {"node_id": NODE, "host_id": HOST, "convoy_id": CONVOY,
              "project_root": str(root), "comp_path": "/x",
              "runtime_id": "runtime-1", "process_id": 100,
              "metadata": {"toe_path": str(toe)}}
    with pytest.raises(cl.LifecycleError) as caught:
        manager.record_registration(record, str(exe))
    assert caught.value.code == "session_unavailable"


def test_store_round_trips_and_rejects_corruption(tmp_path):
    _, store, _, _, _, _, _, _ = make_system(tmp_path)
    reloaded = cl.LaunchProfileStore(store.data_dir)
    assert reloaded.get_profile(NODE)["node_id"] == NODE
    with open(store.path, "w", encoding="utf-8") as handle:
        handle.write('{"schema":1,"profiles":[],"attempts":{}}')
    with pytest.raises(cl.LifecycleError) as caught:
        cl.LaunchProfileStore(store.data_dir)
    assert caught.value.code == "store_unavailable"


def test_atomic_replace_retries_windows_style_sharing_violation():
    calls = []
    sleeps = []

    def replace(source, destination):
        calls.append((source, destination))
        if len(calls) < 4:
            raise PermissionError("sharing violation")

    cl._atomic_replace("source", "destination", replace=replace,
                       sleep=sleeps.append)
    assert len(calls) == 4
    assert sleeps == [.005, .01]


def test_store_refuses_symlink_state_file_where_supported(tmp_path):
    directory = tmp_path / "state"
    directory.mkdir()
    target = tmp_path / "target"
    target.write_text("{}", encoding="utf-8")
    try:
        os.symlink(target, directory / cl.STATE_FILE)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation unavailable")
    with pytest.raises(cl.LifecycleError):
        cl.LaunchProfileStore(str(directory))


def test_indeterminate_safety_fence_is_never_history_pruned(tmp_path, monkeypatch):
    monkeypatch.setattr(cl, "MAX_ATTEMPTS", 4)
    _, store, _, _, _, _, _, _ = make_system(tmp_path)
    quarantined, _ = store.begin_attempt(
        "quarantine", {"operation": "start", "node_id": NODE,
                       "convoy_id": CONVOY})
    store.update_attempt(quarantined["operation_id"], state="indeterminate",
                         launch_started_at=1000.0,
                         result={"ok": False, "code": "indeterminate"})
    for index in range(6):
        attempt, _ = store.begin_attempt(
            "history-%d" % index,
            {"operation": "start", "node_id": NODE,
             "convoy_id": CONVOY, "slot": index})
        store.update_attempt(attempt["operation_id"], state="failed",
                             result={"ok": False, "code": "launch_failed"})
    assert store.get_attempt("quarantine")["state"] == "indeterminate"
    assert len(store.attempts_for_node(NODE)) == 4


def test_operation_id_is_content_bound_and_retries_are_idempotent(tmp_path):
    _, store, _, _, _, _, _, _ = make_system(tmp_path)
    content = {"operation": "start", "node_id": NODE,
               "convoy_id": CONVOY, "timeout_s": 10.0}
    first, created = store.begin_attempt("op-1", content)
    second, created_again = store.begin_attempt("op-1", content)
    assert created is True and created_again is False and first == second
    content["timeout_s"] = 11.0
    with pytest.raises(cl.LifecycleError) as caught:
        store.begin_attempt("op-1", content)
    assert caught.value.code == "idempotency_conflict"


# -- exact start and launch confirmation -----------------------------


@pytest.mark.parametrize("mutate,code", [
    (lambda toe, exe: toe.unlink(), "project_missing"),
    (lambda toe, exe: toe.write_bytes(b"changed"), "project_changed"),
    (lambda toe, exe: exe.unlink(), "executable_missing"),
    (lambda toe, exe: exe.write_bytes(b"changed"), "executable_changed"),
])
def test_start_refuses_missing_or_changed_exact_files(tmp_path, mutate, code):
    system = make_system(tmp_path)
    manager, _, _, _, launcher, _, toe, exe = system
    offline(system)
    mutate(toe, exe)
    result = manager.start_node(NODE, CONVOY, "start-files", timeout_s=5)
    assert result["code"] == code
    assert launcher.spawns == []


def test_start_refuses_stale_recorded_session(tmp_path):
    system = make_system(tmp_path)
    manager, _, _, inspector, launcher, _, _, _ = system
    offline(system)
    inspector.backend.session["session_id"] = "session:new"
    result = manager.start_node(NODE, CONVOY, "start-session", timeout_s=5)
    assert result["code"] == "session_unavailable"
    assert launcher.spawns == []


def test_start_returns_already_running_only_for_verified_process(tmp_path):
    manager, _, _, _, launcher, _, _, _ = make_system(tmp_path)
    result = manager.start_node(NODE, CONVOY, "start-live", timeout_s=5)
    assert result["ok"] is True and result["code"] == "already_running"
    assert launcher.spawns == []


def test_offline_directory_with_last_exact_pid_alive_is_orphan_not_duplicate(tmp_path):
    manager, _, runtime, _, launcher, _, _, _ = make_system(tmp_path)
    runtime.value = None
    result = manager.start_node(NODE, CONVOY, "start-orphan", timeout_s=5)
    assert result["code"] == "orphan_runtime"
    assert launcher.spawns == []


def test_birth_mismatch_means_old_pid_is_not_treated_as_exact_orphan(tmp_path):
    system = make_system(tmp_path)
    manager, _, runtime, inspector, launcher, _, _, _ = system
    runtime.value = None
    inspector.backend.processes[100]["birth_id"] = "birth:reused"
    launcher.callback = auto_confirm(manager)
    # Success path with a threaded confirm: the window is a ceiling the
    # manager never sits out, but a loaded CI runner can miss 100ms on
    # thread scheduling alone (flaked 2026-08-04).
    result = manager.start_node(NODE, CONVOY, "start-reused", timeout_s=5)
    assert result["ok"] is True
    assert len(launcher.spawns) == 1


def test_one_time_token_confirms_exact_node_then_replay_is_rejected(tmp_path):
    system = make_system(tmp_path)
    manager, _, _, _, launcher, _, _, _ = system
    offline(system)
    captured = {}

    def confirm(profile, token, spawned):
        captured.update(token=token, spawned=spawned, profile=profile)
        assert manager.confirm_registration(
            NODE, CONVOY, token, live_record(
                profile, "runtime-new", spawned.pid,
                spawned.reservation_id))["ok"]

    launcher.callback = confirm
    result = manager.start_node(NODE, CONVOY, "start-token", timeout_s=5)
    assert result["ok"] is True
    replay = manager.confirm_registration(
        NODE, CONVOY, captured["token"], live_record(
            captured["profile"], "runtime-new", captured["spawned"].pid,
            captured["spawned"].reservation_id))
    assert replay["code"] == "launch_token_replayed"


def test_wrong_token_and_wrong_process_cannot_consume_confirmation(tmp_path):
    system = make_system(tmp_path)
    manager, _, _, inspector, launcher, _, _, _ = system
    offline(system)
    seen = {}

    def attempts(profile, token, spawned):
        seen["bad_token"] = manager.confirm_registration(
            NODE, CONVOY, "X" * 43,
            live_record(profile, "runtime-new", spawned.pid,
                        spawned.reservation_id))
        inspector.backend.processes[201] = dict(spawned.snapshot, pid=201,
                                                birth_id="birth:201")
        seen["bad_process"] = manager.confirm_registration(
            NODE, CONVOY, token, live_record(
                profile, "runtime-new", 201, spawned.reservation_id))
        seen["good"] = manager.confirm_registration(
            NODE, CONVOY, token,
            live_record(profile, "runtime-new", spawned.pid,
                        spawned.reservation_id))

    launcher.callback = attempts
    result = manager.start_node(NODE, CONVOY, "start-proof", timeout_s=5)
    assert seen["bad_token"]["code"] == "launch_token_invalid"
    assert seen["bad_process"]["code"] == "registration_mismatch"
    assert seen["good"]["ok"] is True and result["ok"] is True


def test_unconfirmed_launch_is_indeterminate_and_retry_does_not_respawn(tmp_path):
    system = make_system(tmp_path)
    manager, _, _, _, launcher, _, _, _ = system
    offline(system)
    fake_time(manager)
    first = manager.start_node(NODE, CONVOY, "start-timeout", timeout_s=.1)
    second = manager.start_node(NODE, CONVOY, "start-timeout", timeout_s=.1)
    assert first["code"] == "launch_unconfirmed"
    assert first["ok"] is False and second == first
    assert len(launcher.spawns) == 1


def test_new_operation_cannot_duplicate_live_unconfirmed_child(tmp_path):
    system = make_system(tmp_path)
    manager, _, _, _, launcher, _, _, _ = system
    offline(system)
    fake_time(manager)
    first = manager.start_node(NODE, CONVOY, "unconfirmed-1", timeout_s=.1)
    second = manager.start_node(NODE, CONVOY, "unconfirmed-2", timeout_s=.1)
    assert first["code"] == "launch_unconfirmed"
    assert second["code"] == "orphan_runtime"
    assert len(launcher.spawns) == 1


def test_popen_to_pid_persistence_crash_gap_requires_local_reconciliation(tmp_path):
    system = make_system(tmp_path)
    manager, store, _, _, launcher, _, _, _ = system
    offline(system)
    fake_time(manager)
    content = {"operation": "start", "node_id": NODE,
               "convoy_id": CONVOY, "timeout_s": .1}
    attempt, _ = store.begin_attempt("crash-gap-1", content)
    store.update_attempt(attempt["operation_id"], state="launching",
                         launch_started_at=manager._clock(),
                         token_hash="f" * 64, token_consumed=False)
    recovered = manager.start_node(NODE, CONVOY, "crash-gap-1", timeout_s=.1)
    refused = manager.start_node(NODE, CONVOY, "crash-gap-2", timeout_s=.1)
    assert recovered["code"] == "launch_unconfirmed"
    assert refused["code"] == "indeterminate"
    assert launcher.spawns == []


def test_unknown_old_pid_state_cannot_be_treated_as_dead_and_duplicated(tmp_path):
    system = make_system(tmp_path)
    manager, _, runtime, inspector, launcher, _, _, _ = system
    runtime.value = None
    original = inspector.backend.inspect_status

    def unknown(pid):
        return {"status": "unknown"} if pid == 100 else original(pid)

    inspector.backend.inspect_status = unknown
    result = manager.start_node(NODE, CONVOY, "unknown-old-pid", timeout_s=5)
    assert result["code"] == "runtime_unverifiable"
    assert launcher.spawns == []


def test_runtime_directory_error_cannot_be_treated_as_offline(tmp_path):
    system = make_system(tmp_path)
    manager, _, runtime, _, launcher, _, _, _ = system

    def broken_current(node_id):
        raise OSError("directory unavailable")

    runtime.current = broken_current
    result = manager.start_node(NODE, CONVOY, "directory-error", timeout_s=5)
    assert result["code"] == "runtime_unverifiable"
    assert launcher.spawns == []


def test_atomic_launch_reservation_failure_prevents_popen(tmp_path):
    system = make_system(tmp_path)
    manager, _, runtime, _, launcher, _, _, _ = system
    offline(system)
    runtime.reserve_launch = lambda *args: {"ok": False, "code": "occupied"}
    result = manager.start_node(NODE, CONVOY, "reserve-refused", timeout_s=5)
    assert result["code"] == "launch_reservation_failed"
    assert launcher.spawns == []


def test_launch_confirmation_requires_matching_directory_reservation(tmp_path):
    system = make_system(tmp_path)
    manager, _, _, _, launcher, _, _, _ = system
    offline(system)
    seen = {}

    def confirm(profile, token, spawned):
        seen["wrong"] = manager.confirm_registration(
            NODE, CONVOY, token,
            live_record(profile, "runtime-reserved", spawned.pid,
                        "reservation:wrong"))
        seen["right"] = manager.confirm_registration(
            NODE, CONVOY, token,
            live_record(profile, "runtime-reserved", spawned.pid,
                        spawned.reservation_id))

    launcher.callback = confirm
    result = manager.start_node(NODE, CONVOY, "reserve-confirm", timeout_s=5)
    assert seen["wrong"]["code"] == "registration_mismatch"
    assert seen["right"]["ok"] is True and result["ok"] is True


def test_launch_reservation_releases_only_after_durable_confirmation(tmp_path):
    manager, store, runtime, inspector, _, _, _, _ = make_system(tmp_path)
    offline((manager, store, runtime, inspector, None, None, None, None))
    attempt, _, record = stage_awaiting_registration(
        manager, store, runtime, inspector, "confirm-order")
    events = []
    original_validate = runtime.confirm_launch_reservation
    original_commit = store.confirm_attempt
    original_release = runtime.release_launch_reservation

    def validate(*args, **kwargs):
        events.append("validate")
        return original_validate(*args, **kwargs)

    def commit(*args, **kwargs):
        events.append("durable_commit")
        return original_commit(*args, **kwargs)

    def release(*args, **kwargs):
        assert store.get_attempt(attempt["operation_id"])["state"] == \
            "succeeded"
        events.append("release")
        return original_release(*args, **kwargs)

    runtime.confirm_launch_reservation = validate
    runtime.release_launch_reservation = release
    store.confirm_attempt = commit
    result = manager.confirm_registration(NODE, CONVOY, TOKEN, record)
    assert result["ok"] is True
    assert events == ["validate", "durable_commit", "release"]
    assert runtime.reservations == {}


def test_failed_durable_confirmation_keeps_reservation_for_safe_retry(tmp_path):
    manager, store, runtime, inspector, _, _, _, _ = make_system(tmp_path)
    offline((manager, store, runtime, inspector, None, None, None, None))
    attempt, _, record = stage_awaiting_registration(
        manager, store, runtime, inspector, "confirm-store-failure")
    original_commit = store.confirm_attempt

    def fail_commit(*args, **kwargs):
        raise cl.LifecycleError("store_unavailable")

    store.confirm_attempt = fail_commit
    with pytest.raises(cl.LifecycleError) as error:
        manager.confirm_registration(NODE, CONVOY, TOKEN, record)
    assert error.value.code == "store_unavailable"
    assert runtime.reservations
    assert store.get_attempt(attempt["operation_id"])["state"] == \
        "awaiting_registration"

    store.confirm_attempt = original_commit
    retried = manager.confirm_registration(NODE, CONVOY, TOKEN, record)
    assert retried["ok"] is True
    assert runtime.reservations == {}


def test_host_restart_restores_durable_launch_fence_and_can_confirm(tmp_path):
    manager, store, runtime, inspector, launcher, _, _, _ = make_system(
        tmp_path)
    offline((manager, store, runtime, inspector, launcher, None, None, None))
    _, _, record = stage_awaiting_registration(
        manager, store, runtime, inspector, "restore-after-host-restart")

    restarted_runtime = Runtime()
    restarted_runtime.inspector = inspector
    restarted_manager = cl.LifecycleManager(
        store, restarted_runtime, process_inspector=inspector,
        launcher=launcher, clock=manager._clock,
        token_factory=lambda: TOKEN)
    restored = restarted_manager.restore_launch_reservations()
    assert restored == {"ok": True, "code": "ok",
                        "detail": "operation completed",
                        "capability": cl.HOST_LIFECYCLE_CAPABILITY,
                        "restored": 1}
    assert restarted_runtime.reservations

    confirmed = restarted_manager.confirm_registration(
        NODE, CONVOY, TOKEN, record)
    assert confirmed["ok"] is True
    assert restarted_runtime.reservations == {}


def test_host_restart_skips_pre_reservation_launching_gap(tmp_path):
    manager, store, runtime, inspector, launcher, _, _, _ = make_system(
        tmp_path)
    offline((manager, store, runtime, inspector, launcher, None, None, None))
    profile = store.get_profile(NODE)
    attempt, _ = store.begin_attempt(
        "restore-before-reserve",
        {"operation": "start", "node_id": NODE,
         "convoy_id": CONVOY, "timeout_s": .1})
    store.update_attempt(
        attempt["operation_id"], expected_states={"created"},
        state="launching", token_hash=cl._token_hash(TOKEN),
        token_consumed=False,
        profile_digest=cl._launch_profile_digest(profile),
        launch_unit_id=cl._launch_unit_id(profile),
        launch_started_at=manager._clock())

    restarted_runtime = Runtime()
    restarted_manager = cl.LifecycleManager(
        store, restarted_runtime, process_inspector=inspector,
        launcher=launcher, clock=manager._clock,
        token_factory=lambda: TOKEN)
    restored = restarted_manager.restore_launch_reservations()
    assert restored["ok"] is True and restored["restored"] == 0
    assert restarted_runtime.reservations == {}


def test_indeterminate_launch_reservation_remains_a_restart_fence(tmp_path):
    manager, store, runtime, inspector, launcher, _, _, _ = make_system(
        tmp_path)
    offline((manager, store, runtime, inspector, launcher, None, None, None))
    attempt, _, _ = stage_awaiting_registration(
        manager, store, runtime, inspector, "restore-indeterminate")
    store.update_attempt(
        attempt["operation_id"], expected_states={"awaiting_registration"},
        state="indeterminate",
        result={"ok": False, "code": "indeterminate"})

    restarted_runtime = Runtime()
    restarted_manager = cl.LifecycleManager(
        store, restarted_runtime, process_inspector=inspector,
        launcher=launcher, clock=manager._clock,
        token_factory=lambda: TOKEN)
    restored = restarted_manager.restore_launch_reservations()
    assert restored["ok"] is True and restored["restored"] == 1
    assert restarted_runtime.reservations


def test_cancel_after_spawn_terminates_owned_exact_process(tmp_path):
    event = threading.Event()
    system = make_system(tmp_path)
    manager, _, _, inspector, _, _, _, _ = system
    offline(system)
    launcher = FakeLauncher(inspector, cancel_event=event)
    manager.launcher = launcher
    result = manager.start_node(NODE, CONVOY, "start-cancel", timeout_s=5,
                                cancel_event=event)
    assert result["code"] == "cancelled"
    assert launcher.cancels and launcher.cancels[0][0] == 200
    assert manager.runtime.reservations == {}


def test_confirmation_winning_cancel_cas_is_never_killed(tmp_path):
    event = threading.Event()
    system = make_system(tmp_path)
    manager, store, _, _, _, _, _, _ = system
    offline(system)
    launcher = FakeLauncher(manager.inspector, cancel_event=event)
    manager.launcher = launcher
    original_update = store.update_attempt
    raced = {"done": False}

    def racing_update(operation_id, **fields):
        if fields.get("state") == "cancelled" and not raced["done"]:
            raced["done"] = True
            profile = store.get_profile(NODE)
            token = launcher.spawns[0][1]
            spawned = launcher.spawns[0][2]
            confirmed = manager.confirm_registration(
                NODE, CONVOY, token,
                live_record(profile, "runtime-cancel-race", spawned.pid,
                            spawned.reservation_id))
            assert confirmed["ok"] is True
        return original_update(operation_id, **fields)

    store.update_attempt = racing_update
    result = manager.start_node(NODE, CONVOY, "cancel-race", timeout_s=5,
                                cancel_event=event)
    assert result["ok"] is True
    assert result["runtime_id"] == "runtime-cancel-race"
    assert launcher.cancels == []


def test_crash_loop_blocks_fourth_recent_spawn(tmp_path):
    system = make_system(tmp_path)
    manager, store, _, _, launcher, _, _, _ = system
    offline(system)
    for index in range(cl.CRASH_LOOP_MAX_LAUNCHES):
        content = {"operation": "start", "node_id": NODE,
                   "convoy_id": CONVOY, "slot": index}
        attempt, _ = store.begin_attempt("old-%d" % index, content)
        store.update_attempt(attempt["operation_id"], state="failed",
                             spawned_at=manager._clock(),
                             result={"ok": False, "code": "launch_failed"})
    result = manager.start_node(NODE, CONVOY, "start-loop", timeout_s=5)
    assert result["code"] == "crash_loop"
    assert launcher.spawns == []


def test_restart_crash_loop_refuses_before_quitting_healthy_runtime(tmp_path):
    manager, store, runtime, _, launcher, _, _, _ = make_system(tmp_path)
    for index in range(cl.CRASH_LOOP_MAX_LAUNCHES):
        attempt, _ = store.begin_attempt(
            "restart-old-%d" % index,
            {"operation": "start", "node_id": NODE,
             "convoy_id": CONVOY, "slot": index})
        store.update_attempt(attempt["operation_id"], state="failed",
                             launch_started_at=manager._clock(),
                             result={"ok": False, "code": "launch_failed"})
    result = manager.restart_node(NODE, CONVOY, "restart-loop",
                                  "runtime-1", timeout_s=5)
    assert result["code"] == "crash_loop"
    assert runtime.quit_calls == [] and launcher.spawns == []


def test_disabled_and_ineligible_profiles_fail_closed(tmp_path):
    for eligible, code in ((True, "profile_disabled"),
                           (False, "launch_not_eligible")):
        child = tmp_path / ("case-" + code)
        child.mkdir()
        system = make_system(child)
        manager, _, _, _, launcher, _, _, _ = system
        offline(system)
        manager.set_enabled(NODE, CONVOY, False if eligible else True,
                            launch_eligible=eligible)
        if not eligible:
            manager.set_enabled(NODE, CONVOY, True, launch_eligible=False)
        result = manager.start_node(NODE, CONVOY, "op-" + code, timeout_s=5)
        assert result["code"] == code and launcher.spawns == []


def test_delete_profile_drops_the_forgotten_nodes_launch_profile(tmp_path):
    """Forgetting a node (manual /nodes/forget or the eviction sweep)
    deletes its launch profile too -- an orphaned profile is unreachable
    by every start path and would sit in lifecycle.json for ever."""
    _manager, store, _, _, _, _, _, _ = make_system(tmp_path)
    assert store.get_profile(NODE) is not None
    assert store.delete_profile(NODE) is True
    assert store.get_profile(NODE) is None
    assert store.delete_profile(NODE) is False, "idempotent second delete"


def test_reregistration_cannot_undo_local_membership_or_launch_gates(tmp_path):
    manager, store, runtime, _, _, root, toe, exe = make_system(tmp_path)
    manager.set_enabled(NODE, CONVOY, False, launch_eligible=False)
    record = {"node_id": NODE, "host_id": HOST, "convoy_id": CONVOY,
              "project_root": str(root), "comp_path": "/project1/Embody",
              "runtime_id": "runtime-1", "process_id": 100,
              "enabled": True,
              "metadata": {"toe_path": str(toe),
                           "touchdesigner_version": "2025.30000"}}
    manager.record_registration(record, str(exe), launch_eligible=True)
    profile = store.get_profile(NODE)
    assert profile["enabled"] is False
    assert profile["launch_eligible"] is False


# -- restart dirty gates, commit, cancellation -----------------------


# Refusal-path timeouts are CEILINGS the manager never sits out -- the
# canned probes refuse immediately. But the deadline is armed on entry
# against the real clock, so a loaded CI runner stalling ~100ms anywhere
# before the check turns the expected refusal into deadline_exceeded
# (flaked on windows-latest 2026-08-04). Such sites use timeout_s=5;
# tests whose SHORT window is the semantics (launch_unconfirmed
# sit-outs, deadline_exceeded contracts, deferred-until-replacement,
# idempotency content) keep .1 deliberately.
@pytest.mark.parametrize("dirty,code", [
    ({"ok": False}, "dirty_state_unknown"),
    ({"ok": True, "dirty": True}, "project_dirty"),
])
def test_require_clean_refuses_unknown_or_dirty_state(tmp_path, dirty, code):
    manager, _, runtime, _, launcher, _, _, _ = make_system(tmp_path)
    runtime.dirty_values = [dirty]
    result = manager.restart_node(NODE, CONVOY, "restart-dirty",
                                  "runtime-1", timeout_s=5)
    assert result["code"] == code
    assert runtime.quit_calls == [] and launcher.spawns == []


def test_save_then_restart_saves_rechecks_then_launches(tmp_path):
    manager, _, runtime, _, launcher, _, _, _ = make_system(tmp_path)
    runtime.dirty_values = [{"ok": True, "dirty": True},
                            {"ok": True, "dirty": False}]
    launcher.callback = auto_confirm(manager)
    result = manager.restart_node(
        NODE, CONVOY, "restart-save", "runtime-1",
        policy="save_then_restart", timeout_s=5)
    assert result["ok"] is True
    assert runtime.saved == [(NODE, "runtime-1")]
    assert runtime.quit_calls == [
        (NODE, "runtime-1", False, "revision:1")]


def test_cancelled_accepted_save_never_restarts_and_reports_uncertainty(
        tmp_path):
    manager, _, runtime, _, launcher, _, _, _ = make_system(tmp_path)
    runtime.dirty_values = [{"ok": True, "dirty": True}]
    runtime.save = lambda *args, **kwargs: {
        "ok": False, "code": "cancelled", "job_id": "job-save",
        "save_may_have_run": True}

    result = manager.restart_node(
        NODE, CONVOY, "restart-cancelled-save", "runtime-1",
        policy="save_then_restart", timeout_s=5)

    assert result["code"] == "cancelled"
    assert result["save_may_have_run"] is True
    assert runtime.quit_calls == [] and launcher.spawns == []


def test_save_then_restart_repins_toe_identity_after_real_save(tmp_path):
    manager, store, runtime, _, launcher, _, toe, _ = make_system(tmp_path)
    runtime.dirty_values = [
        {"ok": True, "dirty": True, "revision": "before"},
        {"ok": True, "dirty": False, "revision": "after"},
        {"ok": True, "dirty": False, "revision": "after"}]

    def save(node_id, runtime_id, timeout_s, cancel_event):
        runtime.saved.append((node_id, runtime_id))
        toe.write_bytes(b"toe-saved-by-touchdesigner")
        return {"ok": True}

    runtime.save = save
    launcher.callback = auto_confirm(manager)
    result = manager.restart_node(
        NODE, CONVOY, "restart-real-save", "runtime-1",
        policy="save_then_restart", timeout_s=5)
    assert result["ok"] is True
    assert store.get_profile(NODE)["toe_identity"] == cl._file_identity(str(toe))
    assert runtime.quit_calls[0][3] == "after"


def test_unsaved_flag_is_dirty_even_when_generic_dirty_flag_is_false(tmp_path):
    manager, _, runtime, _, launcher, _, _, _ = make_system(tmp_path)
    runtime.dirty_values = [{"ok": True, "dirty": False,
                             "unsaved": True, "revision": "unsaved"}]
    result = manager.restart_node(NODE, CONVOY, "restart-unsaved",
                                  "runtime-1", timeout_s=5)
    assert result["code"] == "project_dirty"
    assert runtime.quit_calls == [] and launcher.spawns == []


def test_quit_revision_cas_refusal_never_escalates_without_force(
        tmp_path, monkeypatch):
    # The post-commit quit phase is now sized from the restoration budget, not
    # the caller's timeout_s (finding 2385); shrink only the quit slice so this
    # failed-quit path does not really wait DEFAULT_QUIT_TIMEOUT_S.
    monkeypatch.setattr(cl, "DEFAULT_QUIT_TIMEOUT_S", .1)
    manager, _, runtime, _, launcher, _, _, _ = make_system(tmp_path)

    def state_changed(*args, **kwargs):
        assert kwargs["expected_dirty_revision"] == "revision:1"
        return {"ok": False, "code": "state_changed"}

    runtime.quit = state_changed
    result = manager.restart_node(NODE, CONVOY, "restart-cas-refusal",
                                  "runtime-1", timeout_s=5)
    assert result["code"] == "quit_failed"
    assert launcher.spawns == []


def test_lost_quit_ack_does_not_strand_node_when_exact_process_exited(tmp_path):
    manager, _, runtime, inspector, launcher, _, _, _ = make_system(tmp_path)

    def ack_lost(node_id, runtime_id, timeout_s, cancel_event, **kwargs):
        inspector.backend.processes.pop(100, None)
        runtime.value = None
        return {"ok": False, "code": "transport_lost"}

    runtime.quit = ack_lost
    launcher.callback = auto_confirm(manager)
    result = manager.restart_node(NODE, CONVOY, "restart-ack-lost",
                                  "runtime-1", timeout_s=5)
    assert result["ok"] is True


@pytest.mark.parametrize("policy", ["discard_and_restart", "force"])
def test_destructive_restart_requires_local_literal_true(tmp_path, policy):
    manager, _, runtime, _, launcher, _, _, _ = make_system(
        tmp_path, local_policy=lambda node, selected: 1)
    runtime.dirty_values = [{"ok": True, "dirty": True}]
    result = manager.restart_node(NODE, CONVOY, "restart-" + policy,
                                  "runtime-1", policy=policy, timeout_s=5)
    assert result["code"] == "destructive_policy_disabled"
    assert runtime.quit_calls == [] and launcher.spawns == []


def test_discard_policy_is_checked_twice_next_to_commit(tmp_path):
    checks = []

    def approve(node, policy):
        checks.append((node, policy))
        return True

    manager, _, runtime, _, launcher, _, _, _ = make_system(
        tmp_path, local_policy=approve)
    runtime.dirty_values = [{"ok": True, "dirty": True}]
    launcher.callback = auto_confirm(manager)
    result = manager.restart_node(
        NODE, CONVOY, "restart-discard", "runtime-1",
        policy="discard_and_restart", timeout_s=5)
    assert result["ok"] is True
    assert len(checks) >= 2
    assert set(checks) == {(NODE, "discard_and_restart")}
    assert runtime.quit_calls[0][2] is True


def test_process_birth_mismatch_refuses_restart_without_quit(tmp_path):
    manager, _, runtime, inspector, launcher, _, _, _ = make_system(tmp_path)
    inspector.backend.processes[100]["birth_id"] = "birth:pid-reused"
    result = manager.restart_node(NODE, CONVOY, "restart-birth",
                                  "runtime-1", timeout_s=5)
    assert result["code"] == "runtime_changed"
    assert runtime.quit_calls == [] and launcher.spawns == []


def test_shared_touchdesigner_process_refuses_single_node_restart(tmp_path):
    manager, _, runtime, _, launcher, root, toe, exe = make_system(tmp_path)
    second = {"node_id": NODE_2, "host_id": HOST, "convoy_id": CONVOY,
              "project_root": str(root), "comp_path": "/project1/Embody2",
              "runtime_id": "runtime-2", "process_id": 100,
              "enabled": True,
              "metadata": {"toe_path": str(toe),
                           "touchdesigner_version": "2025.30000"}}
    manager.record_registration(second, str(exe), launch_eligible=True)
    assert manager._lock_for(NODE) is manager._lock_for(NODE_2)
    result = manager.restart_node(NODE, CONVOY, "restart-shared-runtime",
                                  "runtime-1", timeout_s=5)
    assert result["code"] == "shared_runtime"
    assert result["impacted_node_ids"] == [NODE_2]
    assert runtime.quit_calls == [] and launcher.spawns == []


def test_expected_runtime_id_is_compare_and_swap_fence(tmp_path):
    manager, _, runtime, _, launcher, _, _, _ = make_system(tmp_path)
    result = manager.restart_node(NODE, CONVOY, "restart-cas",
                                  "runtime-other", timeout_s=5)
    assert result["code"] == "runtime_changed"
    assert runtime.quit_calls == [] and launcher.spawns == []


def test_cancellation_before_commit_never_quits(tmp_path):
    event = threading.Event()
    event.set()
    manager, _, runtime, _, launcher, _, _, _ = make_system(tmp_path)
    result = manager.restart_node(NODE, CONVOY, "restart-cancel-before",
                                  "runtime-1", timeout_s=5,
                                  cancel_event=event)
    assert result["code"] == "cancelled"
    assert runtime.quit_calls == [] and launcher.spawns == []


def test_lock_wait_consumes_the_single_precommit_deadline(tmp_path):
    manager, store, runtime, _, launcher, _, _, _ = make_system(tmp_path)
    held = manager._lock_for(NODE)
    held.acquire()
    try:
        started = time.monotonic()
        result = manager.restart_node(
            NODE, CONVOY, "restart-lock-deadline", "runtime-1",
            timeout_s=.1)
        elapsed = time.monotonic() - started
    finally:
        held.release()
    assert result["code"] == "deadline_exceeded"
    assert elapsed < .3
    assert store.get_attempt("restart-lock-deadline") is None
    assert runtime.quit_calls == [] and launcher.spawns == []


def test_dirty_round_trips_share_one_precommit_deadline(tmp_path):
    manager, store, runtime, _, launcher, _, _, _ = make_system(tmp_path)
    monotonic = ManualMonotonic()
    manager._monotonic = monotonic
    budgets = []

    def slow_dirty(node_id, runtime_id, timeout_s, cancel_event):
        budgets.append(timeout_s)
        monotonic.advance(.06)
        return {"ok": True, "dirty": False, "unsaved": False,
                "revision": "revision:slow"}

    runtime.dirty = slow_dirty
    result = manager.restart_node(
        NODE, CONVOY, "restart-dirty-deadline", "runtime-1",
        timeout_s=.1)
    assert result["code"] == "deadline_exceeded"
    assert len(budgets) == 2 and budgets[1] < budgets[0]
    assert runtime.quit_calls == [] and launcher.spawns == []
    assert store.get_attempt("restart-dirty-deadline")["state"] == "failed"


def test_requested_timeout_remains_idempotency_content_not_shrinking_budget(
        tmp_path):
    manager, _, runtime, _, _, _, _, _ = make_system(tmp_path)
    monotonic = ManualMonotonic()
    manager._monotonic = monotonic

    def consuming_dirty(node_id, runtime_id, timeout_s, cancel_event):
        monotonic.advance(.06)
        return {"ok": True, "dirty": False, "unsaved": False,
                "revision": "revision:idempotent"}

    runtime.dirty = consuming_dirty
    first = manager.restart_node(
        NODE, CONVOY, "restart-timeout-content", "runtime-1",
        timeout_s=.2, execution_timeout_s=.1)
    retry = manager.restart_node(
        NODE, CONVOY, "restart-timeout-content", "runtime-1",
        timeout_s=.2, execution_timeout_s=.15)
    conflict = manager.restart_node(
        NODE, CONVOY, "restart-timeout-content", "runtime-1",
        timeout_s=.3, execution_timeout_s=.15)

    assert first["code"] == "deadline_exceeded"
    assert retry == first
    assert conflict["code"] == "idempotency_conflict"


def test_save_overrunning_deadline_reports_may_have_run_and_never_quits(
        tmp_path):
    manager, _, runtime, _, launcher, _, _, _ = make_system(tmp_path)
    monotonic = ManualMonotonic()
    manager._monotonic = monotonic
    runtime.dirty_values = [{"ok": True, "dirty": True},
                            {"ok": True, "dirty": False}]

    def slow_save(node_id, runtime_id, timeout_s, cancel_event):
        monotonic.advance(.12)
        return {"ok": True}

    runtime.save = slow_save
    result = manager.restart_node(
        NODE, CONVOY, "restart-save-deadline", "runtime-1",
        policy="save_then_restart", timeout_s=.1)
    assert result["code"] == "deadline_exceeded"
    assert result["save_may_have_run"] is True
    assert runtime.quit_calls == [] and launcher.spawns == []


def test_cancellation_after_quit_commit_is_deferred_until_replacement(tmp_path):
    event = threading.Event()
    manager, _, runtime, _, launcher, _, _, _ = make_system(tmp_path)
    runtime.quit_event = event
    launcher.callback = auto_confirm(manager)
    result = manager.restart_node(NODE, CONVOY, "restart-cancel-after",
                                  "runtime-1", timeout_s=.1,
                                  cancel_event=event)
    assert result["ok"] is True
    assert result["cancel_deferred"] is True
    assert runtime.quit_calls and launcher.spawns
    retry = manager.restart_node(NODE, CONVOY, "restart-cancel-after",
                                 "runtime-1", timeout_s=.1,
                                 cancel_event=event)
    assert retry == result


def test_deadline_after_restart_commit_is_deferred_until_replacement(tmp_path):
    # Fake clock, like its deadline siblings: a real sleep raced the real
    # .1s budget, and a runner stall landing pre-commit aborted instead
    # of deferring (windows-latest, 2026-08-21). Advancing the clock
    # INSIDE the quit pins the expiry to the post-commit window.
    manager, _, runtime, _, launcher, _, _, _ = make_system(tmp_path)
    monotonic = ManualMonotonic()
    manager._monotonic = monotonic
    ordinary_quit = runtime.quit

    def slow_committed_quit(*args, **kwargs):
        monotonic.advance(.12)
        return ordinary_quit(*args, **kwargs)

    runtime.quit = slow_committed_quit
    launcher.callback = auto_confirm(manager)
    result = manager.restart_node(
        NODE, CONVOY, "restart-deadline-after", "runtime-1",
        timeout_s=.1)
    assert result["ok"] is True
    assert result["deadline_deferred"] is True
    assert runtime.quit_calls and launcher.spawns


def test_cancellation_arriving_during_replacement_launch_is_deferred(tmp_path):
    event = threading.Event()
    manager, _, runtime, inspector, _, _, _, _ = make_system(tmp_path)
    launcher = FakeLauncher(inspector, cancel_event=event)
    manager.launcher = launcher
    launcher.callback = auto_confirm(manager)
    result = manager.restart_node(NODE, CONVOY, "restart-cancel-launch",
                                  "runtime-1", timeout_s=5,
                                  cancel_event=event)
    assert result["ok"] is True and result["cancel_deferred"] is True
    assert runtime.quit_calls and launcher.cancels == []


def test_restart_without_cancellation_does_not_claim_it_was_deferred(tmp_path):
    manager, _, _, _, launcher, _, _, _ = make_system(tmp_path)
    launcher.callback = auto_confirm(manager)
    result = manager.restart_node(NODE, CONVOY, "restart-no-cancel",
                                  "runtime-1", timeout_s=5,
                                  cancel_event=threading.Event())
    assert result["ok"] is True and "cancel_deferred" not in result


@pytest.mark.parametrize("old_already_exited", [False, True])
def test_durable_restart_commit_reconciles_and_restores_after_host_crash(
        tmp_path, old_already_exited):
    manager, store, runtime, inspector, launcher, _, _, _ = make_system(tmp_path)
    content = {"operation": "restart", "node_id": NODE,
               "convoy_id": CONVOY, "expected_runtime_id": "runtime-1",
               "policy": "require_clean", "timeout_s": .1}
    attempt, _ = store.begin_attempt("restart-recover", content)
    old_process = store.get_profile(NODE)["last_runtime"]["process"]
    store.update_attempt(
        attempt["operation_id"], state="restart_committed",
        committed_at=manager._clock(), old_process=old_process,
        old_runtime_id="runtime-1", dirty_revision="revision:1",
        profile_fingerprint=cl._profile_fingerprint(store.get_profile(NODE)))
    if old_already_exited:
        inspector.backend.processes.pop(100, None)
        runtime.value = None
    launcher.callback = auto_confirm(manager, runtime_id="runtime-recovered")
    result = manager.restart_node(
        NODE, CONVOY, "restart-recover", "runtime-1", timeout_s=.1)
    assert result["ok"] is True
    assert len(launcher.spawns) == 1
    if old_already_exited:
        assert runtime.quit_calls == []
    else:
        assert runtime.quit_calls


def test_autonomous_recovery_restores_committed_restart_without_client_retry(
        tmp_path):
    manager, store, runtime, inspector, launcher, _, _, _ = make_system(
        tmp_path)
    content = {"operation": "restart", "node_id": NODE,
               "convoy_id": CONVOY, "expected_runtime_id": "runtime-1",
               "policy": "require_clean", "timeout_s": .1}
    attempt, _ = store.begin_attempt("restart-autonomous", content)
    profile = store.get_profile(NODE)
    old_process = profile["last_runtime"]["process"]
    store.update_attempt(
        attempt["operation_id"], state="restart_committed",
        committed_at=manager._clock(), old_process=old_process,
        old_runtime_id="runtime-1", dirty_revision="revision:1",
        profile_fingerprint=cl._profile_fingerprint(profile),
        restart_policy="require_clean", requested_timeout_s=.1)
    inspector.backend.processes.pop(100, None)
    runtime.value = None
    launcher.callback = auto_confirm(manager, runtime_id="runtime-autonomous")
    callbacks = []

    # A real-clock CEILING: recovery completes the moment the synchronous
    # confirm lands, so only a stalled CI runner ever nears this bound --
    # and the launch reservation's lifetime is sliced from it, so a tight
    # value starved the confirm on a stalled runner (the windows-latest
    # flake). Fake time cannot serve here: it advances instantly and
    # starves the slice deterministically. The deferred-deadline flag
    # comes from the durable record's own requested_timeout_s.
    summary = manager.recover_committed_restarts(
        result_callback=lambda durable, result: callbacks.append(
            (durable["operation_id"], result)),
        recovery_timeout_s=10)

    assert summary["recovered"] == 1
    assert callbacks[0][0] == "restart-autonomous"
    assert callbacks[0][1]["ok"] is True
    assert callbacks[0][1]["deadline_deferred"] is True
    assert len(launcher.spawns) == 1


def test_autonomous_recovery_rewinds_pre_reservation_launching_boundary(
        tmp_path):
    manager, store, runtime, inspector, launcher, _, _, _ = make_system(
        tmp_path)
    content = {"operation": "restart", "node_id": NODE,
               "convoy_id": CONVOY, "expected_runtime_id": "runtime-1",
               "policy": "require_clean", "timeout_s": 5}
    attempt, _ = store.begin_attempt("restart-rewind-launching", content)
    profile = store.get_profile(NODE)
    store.update_attempt(
        attempt["operation_id"], state="launching",
        committed_at=manager._clock(),
        old_process=profile["last_runtime"]["process"],
        old_runtime_id="runtime-1", dirty_revision="revision:1",
        profile_fingerprint=cl._profile_fingerprint(profile),
        restart_policy="require_clean", requested_timeout_s=5,
        token_hash=cl._token_hash(TOKEN), token_consumed=False,
        launch_unit_id=cl._launch_unit_id(profile),
        launch_started_at=manager._clock())
    inspector.backend.processes.pop(100, None)
    runtime.value = None
    launcher.callback = auto_confirm(manager, runtime_id="runtime-rewound")

    # A GENEROUS CEILING on a path that completes immediately (the
    # launcher auto-confirms), which is the one real-clock budget the
    # CI-flake doctrine allows -- but it must be seconds, not tenths.
    # Recovery does several durable writes, and _write_private fsyncs each
    # one (~1.5 ms apiece), so a 200 ms budget under full-matrix disk
    # contention had no margin. That sync was withdrawn once (16ca52c) and
    # reinstated with the rename budget it needed, so the cost is real
    # again -- but this ceiling is deliberately far above it either way,
    # and must never be tightened back toward the measurement.
    summary = manager.recover_committed_restarts(recovery_timeout_s=5)

    assert summary["recovered"] == 1
    assert store.get_attempt(attempt["operation_id"])["state"] == "succeeded"
    assert len(launcher.spawns) == 1


def test_autonomous_recovery_never_respawns_ambiguous_post_reservation_gap(
        tmp_path):
    manager, store, _, _, launcher, _, _, _ = make_system(tmp_path)
    content = {"operation": "restart", "node_id": NODE,
               "convoy_id": CONVOY, "expected_runtime_id": "runtime-1",
               "policy": "require_clean", "timeout_s": .1}
    attempt, _ = store.begin_attempt("restart-ambiguous-launching", content)
    profile = store.get_profile(NODE)
    reservation_id = "reservation:restart-ambiguous-launching"
    store.update_attempt(
        attempt["operation_id"], state="launching",
        committed_at=manager._clock(),
        old_process=profile["last_runtime"]["process"],
        old_runtime_id="runtime-1", dirty_revision="revision:1",
        profile_fingerprint=cl._profile_fingerprint(profile),
        restart_policy="require_clean", requested_timeout_s=.1,
        token_hash=cl._token_hash(TOKEN), token_consumed=False,
        launch_unit_id=cl._launch_unit_id(profile),
        launch_started_at=manager._clock(),
        reservation_id=reservation_id)

    summary = manager.recover_committed_restarts(recovery_timeout_s=.2)
    durable = store.get_attempt(attempt["operation_id"])

    assert summary["recovered"] == 1
    assert durable["state"] == "indeterminate"
    assert durable["result"]["code"] == "indeterminate"
    assert durable["deadline_deferred"] is True
    assert durable["reservation_id"] == reservation_id
    assert launcher.spawns == []


def test_autonomous_recovery_waits_for_durable_spawn_registration(tmp_path):
    manager, store, runtime, inspector, launcher, _, _, _ = make_system(
        tmp_path)
    content = {"operation": "restart", "node_id": NODE,
               "convoy_id": CONVOY, "expected_runtime_id": "runtime-1",
               "policy": "require_clean", "timeout_s": .1}
    attempt, _ = store.begin_attempt("restart-awaiting-recovery", content)
    profile = store.get_profile(NODE)
    launch_unit_id = cl._launch_unit_id(profile)
    attempt, _ = store.update_attempt(
        attempt["operation_id"], state="launching",
        committed_at=manager._clock(),
        old_process=profile["last_runtime"]["process"],
        old_runtime_id="runtime-1", dirty_revision="revision:1",
        profile_fingerprint=cl._profile_fingerprint(profile),
        restart_policy="require_clean", requested_timeout_s=.1,
        token_hash=cl._token_hash(TOKEN), token_consumed=False,
        profile_digest=cl._launch_profile_digest(
            profile, include_gates=False),
        launch_unit_id=launch_unit_id,
        launch_started_at=manager._clock())
    reserved = runtime.reserve_launch(
        NODE, launch_unit_id, attempt["operation_id"], .1, None)
    process = {
        "pid": 200, "executable_path": profile["executable_path"],
        "birth_id": "birth:200", "user_id": profile["session"]["user_id"],
        "session_id": profile["session"]["session_id"],
    }
    inspector.backend.processes[200] = dict(process)
    store.update_attempt(
        attempt["operation_id"], state="awaiting_registration",
        reservation_id=reserved["reservation_id"], spawned_pid=200,
        spawned_process=process, spawned_at=manager._clock())
    record = live_record(
        profile, "runtime-awaiting-recovered", 200,
        reserved["reservation_id"])
    timer = threading.Timer(
        .02, manager.confirm_registration,
        args=(NODE, CONVOY, TOKEN, record))
    timer.start()
    try:
        # A ceiling, not a sit-out: recovery returns the moment the
        # threaded confirm lands, so only a stalled runner ever nears it.
        summary = manager.recover_committed_restarts(
            recovery_timeout_s=10)
    finally:
        timer.join(1)

    durable = store.get_attempt(attempt["operation_id"])
    assert summary["recovered"] == 1
    assert durable["state"] == "succeeded"
    assert durable["result"]["deadline_deferred"] is True
    assert launcher.spawns == []


def test_recovery_reports_terminal_committed_result_for_host_reconciliation(
        tmp_path):
    manager, store, _, _, _, _, _, _ = make_system(tmp_path)
    content = {"operation": "restart", "node_id": NODE,
               "convoy_id": CONVOY, "expected_runtime_id": "runtime-1",
               "policy": "require_clean", "timeout_s": .1}
    attempt, _ = store.begin_attempt("restart-terminal-recovery", content)
    profile = store.get_profile(NODE)
    result = cl._result(
        True, "ok", node_id=NODE,
        operation_id="restart-terminal-recovery",
        runtime_id="runtime-new")
    store.update_attempt(
        attempt["operation_id"], state="succeeded", result=result,
        committed_at=manager._clock(),
        old_process=profile["last_runtime"]["process"],
        old_runtime_id="runtime-1", dirty_revision="revision:1",
        profile_fingerprint=cl._profile_fingerprint(profile),
        restart_policy="require_clean", requested_timeout_s=.1)
    callbacks = []

    summary = manager.recover_committed_restarts(
        result_callback=lambda durable, observed: callbacks.append(observed),
        recovery_timeout_s=.2)

    assert summary["reconciled"] == 1
    assert callbacks == [result]


def test_committed_restart_restores_even_if_membership_disabled_midflight(tmp_path):
    manager, store, runtime, inspector, launcher, _, _, _ = make_system(tmp_path)
    content = {"operation": "restart", "node_id": NODE,
               "convoy_id": CONVOY, "expected_runtime_id": "runtime-1",
               "policy": "require_clean", "timeout_s": .1}
    attempt, _ = store.begin_attempt("restart-disabled-recover", content)
    old_process = store.get_profile(NODE)["last_runtime"]["process"]
    store.update_attempt(
        attempt["operation_id"], state="restart_committed",
        committed_at=manager._clock(), old_process=old_process,
        old_runtime_id="runtime-1", dirty_revision="revision:1",
        profile_fingerprint=cl._profile_fingerprint(store.get_profile(NODE)))
    manager.set_enabled(NODE, CONVOY, False)
    inspector.backend.processes.pop(100, None)
    runtime.value = None
    launcher.callback = auto_confirm(manager, runtime_id="runtime-restored")
    result = manager.restart_node(
        NODE, CONVOY, "restart-disabled-recover", "runtime-1", timeout_s=.1)
    assert result["ok"] is True


def test_already_cancelled_retry_cannot_abort_durable_restart_commit(tmp_path):
    manager, store, runtime, inspector, launcher, _, _, _ = make_system(
        tmp_path)
    content = {"operation": "restart", "node_id": NODE,
               "convoy_id": CONVOY, "expected_runtime_id": "runtime-1",
               "policy": "require_clean", "timeout_s": .1}
    attempt, _ = store.begin_attempt("restart-cancelled-recovery", content)
    profile = store.get_profile(NODE)
    store.update_attempt(
        attempt["operation_id"], state="restart_committed",
        committed_at=manager._clock(),
        old_process=profile["last_runtime"]["process"],
        old_runtime_id="runtime-1", dirty_revision="revision:1",
        profile_fingerprint=cl._profile_fingerprint(profile),
        restart_policy="require_clean", requested_timeout_s=.1)
    inspector.backend.processes.pop(100, None)
    runtime.value = None
    launcher.callback = auto_confirm(manager, runtime_id="runtime-restored")
    cancel = threading.Event()
    cancel.set()

    result = manager.restart_node(
        NODE, CONVOY, attempt["operation_id"], "runtime-1",
        timeout_s=.1, cancel_event=cancel)

    assert result["ok"] is True
    assert result["cancel_deferred"] is True
    assert len(launcher.spawns) == 1


def test_committed_destructive_restart_restores_after_exit_even_if_gate_revoked(
        tmp_path):
    manager, store, runtime, inspector, launcher, _, _, _ = make_system(
        tmp_path, local_policy=lambda node, policy: False)
    content = {"operation": "restart", "node_id": NODE,
               "convoy_id": CONVOY, "expected_runtime_id": "runtime-1",
               "policy": "force", "timeout_s": .1}
    attempt, _ = store.begin_attempt("force-recover-revoked", content)
    profile = store.get_profile(NODE)
    old_process = profile["last_runtime"]["process"]
    store.update_attempt(
        attempt["operation_id"], state="restart_committed",
        committed_at=manager._clock(), old_process=old_process,
        old_runtime_id="runtime-1", dirty_revision=None,
        profile_fingerprint=cl._profile_fingerprint(profile))
    inspector.backend.processes.pop(100, None)
    runtime.value = None
    launcher.callback = auto_confirm(manager, runtime_id="force-restored")
    result = manager.restart_node(
        NODE, CONVOY, "force-recover-revoked", "runtime-1",
        policy="force", timeout_s=.1)
    assert result["ok"] is True


def test_force_escalates_only_exact_old_process_after_structured_quit_fails(
        tmp_path, monkeypatch):
    # The post-commit quit wait is sized from the restoration budget now
    # (finding 2385); shrink only the quit slice so the pre-force wait for the
    # (deliberately still-alive) process does not really take 30s.
    monkeypatch.setattr(cl, "DEFAULT_QUIT_TIMEOUT_S", .1)
    manager, _, runtime, inspector, launcher, _, _, _ = make_system(
        tmp_path, local_policy=lambda node, policy: True)

    def failed_quit(*args, **kwargs):
        runtime.quit_calls.append(args)
        return {"ok": False}

    runtime.quit = failed_quit
    launcher.callback = auto_confirm(manager)
    result = manager.restart_node(NODE, CONVOY, "restart-force",
                                  "runtime-1", policy="force", timeout_s=5)
    assert result["ok"] is True
    assert inspector.backend.terminated == [(100, True)]


def test_per_profile_lock_serializes_and_cancelled_waiter_never_spawns(tmp_path):
    system = make_system(tmp_path, step_clock=False)
    manager, _, _, inspector, _, _, _, _ = system
    offline(system)
    entered = threading.Event()
    release = threading.Event()

    class BlockingLauncher(FakeLauncher):
        def spawn(self, profile, token, reservation_id):
            entered.set()
            release.wait(2)
            return super().spawn(profile, token, reservation_id)

    launcher = BlockingLauncher(inspector)
    manager.launcher = launcher
    results = {}
    first = threading.Thread(target=lambda: results.setdefault(
        "first", manager.start_node(NODE, CONVOY, "serial-1", timeout_s=30)))
    first.start()
    assert entered.wait(10)
    cancel = threading.Event()
    second = threading.Thread(target=lambda: results.setdefault(
        "second", manager.start_node(NODE, CONVOY, "serial-2", timeout_s=5,
                                     cancel_event=cancel)))
    second.start()
    time.sleep(.08)
    cancel.set()
    second.join(30)
    release.set()
    first.join(10)
    assert results["second"]["code"] == "cancelled"
    assert len(launcher.spawns) == 1


# -- launcher argv/environment and platform adapters -----------------


@pytest.mark.parametrize("platform", ["win32", "darwin"])
def test_launcher_direct_spawns_exact_binary_and_toe_without_shell(
        tmp_path, platform):
    manager, store, _, inspector, _, _, _, _ = make_system(
        tmp_path, platform=platform)
    calls = []

    def popen(argv, **kwargs):
        calls.append((argv, kwargs))
        return FakePopen(300)

    profile = store.get_profile(NODE)
    inspector.backend.processes[300] = {
        "pid": 300, "executable_path": profile["executable_path"],
        "birth_id": "birth:300", "user_id": "user:1",
        "session_id": "session:1"}
    launcher = cl.ExactProcessLauncher(
        inspector, platform=platform, popen_factory=popen,
        environment={"PATH": "safe", "PYTHONPATH": "bad", "DYLD_X": "bad",
                     "CONVOY_TOKEN": "bad"})
    launcher.spawn(profile, TOKEN, "reservation:test")
    argv, kwargs = calls[0]
    assert argv == [profile["executable_path"], profile["toe_path"]]
    assert kwargs["shell"] is False and kwargs["cwd"] == profile["project_root"]
    if platform == "win32":
        assert "creationflags" in kwargs
    else:
        assert "creationflags" not in kwargs
        assert "start_new_session" not in kwargs
    assert kwargs["env"]["PATH"] == "safe"
    assert kwargs["env"]["EMBODY_CONVOY_LAUNCH_TOKEN"] == TOKEN
    assert kwargs["env"]["EMBODY_CONVOY_LAUNCH_RESERVATION"] \
        == "reservation:test"
    assert "PYTHONPATH" not in kwargs["env"]
    assert "DYLD_X" not in kwargs["env"]
    assert "CONVOY_TOKEN" not in kwargs["env"]
    assert all("open" != os.path.basename(item).lower() for item in argv)


def test_local_inspector_revalidates_birth_before_exact_termination():
    backend = FakeBackend()
    backend.processes[10] = {"pid": 10, "executable_path": os.path.abspath("td"),
                             "birth_id": "new", "user_id": "u",
                             "session_id": "s"}
    inspector = cl.LocalProcessInspector(platform="win32", backend=backend)
    expected = dict(backend.processes[10], birth_id="old")
    assert inspector.terminate_exact(expected, force=True, timeout_s=.1) is False
    assert backend.terminated == []


def test_mac_backend_uses_libproc_birth_and_signals_only_exact_pid():
    import ctypes
    calls = {"kill": []}

    class LibProc:
        @staticmethod
        def proc_pidpath(pid, buffer, length):
            assert pid == 4321
            buffer.value = (b"/Applications/TouchDesigner.app/Contents/"
                            b"MacOS/TouchDesigner")
            return len(buffer.value)

        @staticmethod
        def proc_pidinfo(pid, flavor, arg, info_pointer, size):
            assert (pid, flavor, arg) == (4321, 3, 0)
            info = info_pointer._obj
            info.pbi_pid = pid
            info.pbi_uid = 501
            info.pbi_start_tvsec = 1_722_623_400
            info.pbi_start_tvusec = 123456
            return size

    backend = cl._MacNativeBackend(
        ctypes_module=ctypes, libproc=LibProc(),
        kill=lambda pid, sig: calls["kill"].append((pid, sig)),
        getsid=lambda pid: 77,
        stat_fn=lambda path: type("S", (), {"st_uid": 501})(),
        geteuid=lambda: 501)
    snapshot = backend.inspect(4321)
    assert snapshot["birth_id"] == "mac-time:1722623400.123456"
    assert snapshot["user_id"] == "uid:501"
    assert backend.current_session()["interactive"] is True
    assert backend.terminate(4321, force=False)
    assert backend.terminate(4321, force=True)
    assert calls["kill"] == [(4321, getattr(signal, "SIGTERM", 15)),
                              (4321, getattr(signal, "SIGKILL", 9))]


def test_mac_adapter_source_contains_no_app_wide_termination_primitive():
    import inspect
    source = inspect.getsource(cl._MacNativeBackend).lower()
    forbidden = ("osascript", "killall", 'open", "-a', "open -a")
    assert not any(item in source for item in forbidden)


def test_catalog_requires_no_full_shell_and_has_exact_structured_ops():
    catalog = cl.lifecycle_catalog()
    assert catalog["capability"] == "host.td-lifecycle/v1"
    assert set(catalog["operations"]) == {
        "convoy_start_node", "convoy_restart_node"}
    assert all(op["full_shell_required"] is False
               for op in catalog["operations"].values())


def test_registration_requires_touchdesigner_build(tmp_path):
    manager, _, _, _, _, root, toe, exe = make_system(tmp_path)
    record = {"node_id": NODE, "host_id": HOST, "convoy_id": CONVOY,
              "project_root": str(root), "comp_path": "/project1/Embody",
              "runtime_id": "runtime-1", "process_id": 100,
              "metadata": {"toe_path": str(toe)}}
    with pytest.raises(cl.LifecycleError) as caught:
        manager.record_registration(record, str(exe))
    assert caught.value.code == "invalid_arguments"


@pytest.mark.parametrize("field,value", [
    ("host_id", "d" * 32), ("comp_path", "/wrong/Embody")])
def test_runtime_identity_fields_are_exact_restart_fences(tmp_path, field, value):
    manager, _, runtime, _, launcher, _, _, _ = make_system(tmp_path)
    runtime.value[field] = value
    result = manager.restart_node(NODE, CONVOY, "restart-field-" + field,
                                  "runtime-1", timeout_s=5)
    assert result["code"] == "runtime_changed"
    assert runtime.quit_calls == [] and launcher.spawns == []


@pytest.mark.parametrize("name,value", [
    ("toe_path", "C:/wrong/project.toe"),
    ("touchdesigner_version", "2024.10000")])
def test_runtime_metadata_is_exact_restart_fence(tmp_path, name, value):
    manager, _, runtime, _, launcher, _, _, _ = make_system(tmp_path)
    runtime.value["metadata"][name] = value
    result = manager.restart_node(NODE, CONVOY, "restart-meta-" + name,
                                  "runtime-1", timeout_s=5)
    assert result["code"] == "runtime_changed"
    assert runtime.quit_calls == [] and launcher.spawns == []


@pytest.mark.parametrize("policy", ["discard_and_restart", "force"])
def test_locally_approved_destructive_policy_can_proceed_when_dirty_unknown(
        tmp_path, policy):
    manager, _, runtime, _, launcher, _, _, _ = make_system(
        tmp_path, local_policy=lambda node, selected: True)
    runtime.dirty_values = [{"ok": False}]
    launcher.callback = auto_confirm(manager)
    result = manager.restart_node(NODE, CONVOY, "unknown-" + policy,
                                  "runtime-1", policy=policy, timeout_s=5)
    assert result["ok"] is True


def test_confirm_is_one_atomic_profile_and_attempt_commit(tmp_path):
    system = make_system(tmp_path)
    manager, store, _, _, launcher, _, _, _ = system
    offline(system)
    launcher.callback = auto_confirm(manager, runtime_id="runtime-atomic")
    # Success path with a threaded confirm; 100ms flaked on CI (2026-08-04).
    result = manager.start_node(NODE, CONVOY, "atomic-confirm", timeout_s=5)
    assert result["ok"] is True
    reloaded = cl.LaunchProfileStore(store.data_dir)
    assert reloaded.get_attempt("atomic-confirm")["state"] == "succeeded"
    assert reloaded.get_attempt("atomic-confirm")["token_hash"] == ""
    assert reloaded.get_profile(NODE)["last_runtime"]["runtime_id"] \
        == "runtime-atomic"


def test_disable_between_spawn_and_confirmation_cancels_owned_child(tmp_path):
    system = make_system(tmp_path)
    manager, _, _, _, launcher, _, _, _ = system
    offline(system)
    seen = {}
    callback_done = threading.Event()

    def change_then_confirm(profile, token, spawned):
        manager.set_enabled(NODE, CONVOY, False)
        seen["result"] = manager.confirm_registration(
            NODE, CONVOY, token,
            live_record(profile, "runtime-changed-profile", spawned.pid,
                        spawned.reservation_id))
        callback_done.set()

    launcher.callback = change_then_confirm
    # The callback disables mid-flight and must still get scheduled; a
    # loaded CI runner missed both the 100ms window and the 1s wait
    # (flaked 2026-08-04).
    result = manager.start_node(NODE, CONVOY, "profile-race", timeout_s=5)
    assert callback_done.wait(5)
    assert seen["result"]["code"] == "launch_token_replayed"
    assert result["code"] == "cancelled"
    assert launcher.cancels and launcher.cancels[0][0] == 200


def test_confirmed_launches_do_not_trip_crash_loop(tmp_path):
    _, store, _, _, _, _, _, _ = make_system(tmp_path)
    for index in range(cl.CRASH_LOOP_MAX_LAUNCHES + 2):
        attempt, _ = store.begin_attempt(
            "success-%d" % index,
            {"operation": "start", "node_id": NODE,
             "convoy_id": CONVOY, "slot": index})
        store.update_attempt(attempt["operation_id"], state="succeeded",
                             launch_started_at=1001.0 + index,
                             result={"ok": True, "code": "ok"})
    assert store.recent_launch_count(NODE, now=1010.0) == 0


def test_short_lived_confirmed_runtime_is_reclassified_for_crash_loop(tmp_path):
    system = make_system(tmp_path)
    manager, store, _, inspector, launcher, _, _, _ = system
    offline(system)
    launcher.callback = auto_confirm(manager, runtime_id="runtime-unstable")
    result = manager.start_node(NODE, CONVOY, "unstable-launch", timeout_s=5)
    assert result["ok"] is True
    inspector.backend.processes.pop(200, None)
    exit_result = manager.record_runtime_exit(NODE, "runtime-unstable")
    assert exit_result["ok"] is True and exit_result["unstable_exit"] is True
    attempt = store.get_attempt("unstable-launch")
    assert attempt["state"] == "succeeded"
    assert attempt["unstable_exit"] is True
    assert store.recent_launch_count(
        NODE, launch_unit_id=cl._launch_unit_id(store.get_profile(NODE)),
        now=manager._clock()) == 1


def test_windows_backend_force_terminates_only_opened_exact_pid():
    import ctypes

    class Kernel:
        def __init__(self):
            self.opened = []
            self.terminated = []
            self.closed = []

        def OpenProcess(self, access, inherit, pid):
            self.opened.append((access, inherit, pid))
            return 55

        def TerminateProcess(self, handle, code):
            self.terminated.append((handle, code))
            return 1

        def CloseHandle(self, handle):
            self.closed.append(handle)

    backend = cl._WindowsNativeBackend.__new__(cl._WindowsNativeBackend)
    backend.ctypes = ctypes
    backend.kernel32 = Kernel()
    backend.user32 = None
    assert backend.terminate(4321, force=True) is True
    assert backend.kernel32.opened == [(backend.PROCESS_TERMINATE, False, 4321)]
    assert backend.kernel32.terminated == [(55, 1)]
    assert backend.kernel32.closed == [55]


def test_windows_exact_force_keeps_process_bound_handle_through_termination():
    import ctypes
    expected = {"pid": 4321, "executable_path": os.path.abspath("td.exe"),
                "birth_id": "birth:exact", "user_id": "user:1",
                "session_id": "session:1"}

    class Kernel:
        def __init__(self):
            self.opened = []
            self.terminated = []
            self.closed = []

        def OpenProcess(self, access, inherit, pid):
            self.opened.append((access, pid))
            return 77

        @staticmethod
        def WaitForSingleObject(handle, timeout):
            return 0x102

        def TerminateProcess(self, handle, code):
            self.terminated.append((handle, code))
            return 1

        def CloseHandle(self, handle):
            self.closed.append(handle)

    backend = cl._WindowsNativeBackend.__new__(cl._WindowsNativeBackend)
    backend.ctypes = ctypes
    backend.kernel32 = Kernel()
    backend.inspect_status = lambda pid: {
        "status": "alive", "process": dict(expected)}
    assert backend.terminate_exact(expected, force=True) is True
    assert backend.kernel32.opened == [(
        backend.PROCESS_QUERY_LIMITED_INFORMATION | backend.SYNCHRONIZE
        | backend.PROCESS_TERMINATE, 4321)]
    assert backend.kernel32.terminated == [(77, 1)]
    assert backend.kernel32.closed == [77]


def test_mac_exact_signal_revalidates_full_birth_immediately_before_kill():
    expected = {"pid": 4321, "executable_path": os.path.abspath("td"),
                "birth_id": "birth:old", "user_id": "user:1",
                "session_id": "session:1"}
    backend = cl._MacNativeBackend.__new__(cl._MacNativeBackend)
    signals = []
    backend.inspect_status = lambda pid: {
        "status": "alive", "process": dict(expected, birth_id="birth:reused")}
    backend.terminate = lambda pid, force: signals.append((pid, force)) or True
    assert backend.terminate_exact(expected, force=True) is False
    assert signals == []


def test_windows_graceful_close_posts_only_to_exact_pid_windows():
    import ctypes

    class User:
        def __init__(self):
            self.posts = []

        def EnumWindows(self, callback, value):
            assert callback(11, value)
            assert callback(22, value)
            return 1

        @staticmethod
        def GetWindowThreadProcessId(hwnd, pointer):
            pointer._obj.value = 4321 if hwnd == 22 else 9999
            return 1

        def PostMessageW(self, hwnd, message, wparam, lparam):
            self.posts.append((hwnd, message, wparam, lparam))
            return 1

    backend = cl._WindowsNativeBackend.__new__(cl._WindowsNativeBackend)
    backend.ctypes = ctypes
    backend.kernel32 = None
    backend.user32 = User()
    assert backend.terminate(4321, force=False) is True
    assert backend.user32.posts == [(22, backend.WM_CLOSE, 0, 0)]


@pytest.mark.parametrize("error,status", [(87, "dead"), (5, "unknown"),
                                           (1234, "unknown")])
def test_windows_open_failure_distinguishes_dead_from_unverifiable(error, status):
    import ctypes

    class Kernel:
        @staticmethod
        def OpenProcess(access, inherit, pid):
            return 0

        @staticmethod
        def GetLastError():
            return error

    backend = cl._WindowsNativeBackend.__new__(cl._WindowsNativeBackend)
    backend.ctypes = ctypes
    backend.kernel32 = Kernel()
    assert backend.inspect_status(4321) == {"status": status}


def test_windows_native_success_uses_query_plus_synchronize_and_full_birth_proof():
    import ctypes

    class Kernel:
        def __init__(self):
            self.access = None
            self.closed = []

        def OpenProcess(self, access, inherit, pid):
            self.access = access
            return 55

        @staticmethod
        def WaitForSingleObject(handle, timeout):
            return 0x102

        @staticmethod
        def QueryFullProcessImageNameW(handle, flags, path, size_pointer):
            path.value = os.path.abspath("TouchDesigner.exe")
            return 1

        @staticmethod
        def GetProcessTimes(handle, created, exited, kernel, user):
            created._obj.high = 2
            created._obj.low = 3
            return 1

        @staticmethod
        def ProcessIdToSessionId(pid, pointer):
            pointer._obj.value = 7
            return 1

        def CloseHandle(self, handle):
            self.closed.append(handle)

    backend = cl._WindowsNativeBackend.__new__(cl._WindowsNativeBackend)
    backend.ctypes = ctypes
    backend.kernel32 = Kernel()
    backend._user_sid = lambda handle: "S-1-test"
    result = backend.inspect_status(4321)
    assert result["status"] == "alive"
    assert result["process"]["birth_id"] == "winfiletime:%d" % ((2 << 32) | 3)
    assert result["process"]["session_id"] == "windows:7"
    assert backend.kernel32.access == (
        backend.PROCESS_QUERY_LIMITED_INFORMATION | backend.SYNCHRONIZE)
    assert backend.kernel32.closed == [55]


def test_windows_wait_failure_is_unknown_not_dead():
    import ctypes

    class Kernel:
        @staticmethod
        def OpenProcess(access, inherit, pid):
            return 55

        @staticmethod
        def WaitForSingleObject(handle, timeout):
            return 0xFFFFFFFF

        @staticmethod
        def CloseHandle(handle):
            return 1

    backend = cl._WindowsNativeBackend.__new__(cl._WindowsNativeBackend)
    backend.ctypes = ctypes
    backend.kernel32 = Kernel()
    assert backend.inspect_status(4321) == {"status": "unknown"}


def test_real_windows_process_inspector_has_pointer_safe_identity_proof():
    import sys
    if sys.platform != "win32":
        pytest.skip("real Windows ABI proof")
    inspector = cl.LocalProcessInspector(platform="win32")
    status = inspector.inspect_status(os.getpid())
    session = inspector.current_session()
    assert status["status"] == "alive"
    assert session and session["interactive"] is True
    assert status["process"]["user_id"] == session["user_id"]
    assert status["process"]["session_id"] == session["session_id"]


@pytest.mark.parametrize("failure,status", [
    (ProcessLookupError(), "dead"), (PermissionError(), "unknown"),
    (OSError(), "unknown")])
def test_mac_inspection_failure_distinguishes_dead_from_unverifiable(
        failure, status):
    import ctypes

    class LibProc:
        @staticmethod
        def proc_pidpath(pid, buffer, length):
            return 0

    def probe(pid, sent_signal):
        assert (pid, sent_signal) == (4321, 0)
        raise failure

    backend = cl._MacNativeBackend(
        ctypes_module=ctypes, libproc=LibProc(), kill=probe,
        getsid=lambda pid: 1,
        stat_fn=lambda path: type("S", (), {"st_uid": 501})(),
        geteuid=lambda: 501)
    assert backend.inspect_status(4321) == {"status": status}


# -- lock identity, budgets, save proof, stranded reconciliation ------
# Regressions for the 2026-08-02 review findings 1478/2385/2676/684/1981.


def test_lock_identity_is_stable_across_toe_identity_change(tmp_path):
    # finding 1478: the serialization lock must not change identity when a
    # save_then_restart re-pins toe_identity mid-operation, or the recovery
    # loop mints a different Lock object and races the live op.
    manager, store, _, _, _, _, toe, _ = make_system(tmp_path)
    before = manager._lock_for(NODE)
    unit_before = cl._launch_unit_id(store.get_profile(NODE))
    toe.write_bytes(b"toe-rewritten-by-a-save-with-a-different-size")
    manager._refresh_saved_toe(store.get_profile(NODE))
    after = manager._lock_for(NODE)
    assert before is after
    # The launch-unit id (reservation namespace) DID change with the file
    # identity, proving the lock is keyed on something more stable.
    assert cl._launch_unit_id(store.get_profile(NODE)) != unit_before


def test_recovery_skips_operation_owned_by_a_live_in_process_op(tmp_path):
    # finding 1478 (second clause): recovery never re-enters an attempt a live
    # in-process op still owns, independent of the launch-unit lock.
    manager, store, runtime, inspector, launcher, _, _, _ = make_system(
        tmp_path)
    content = {"operation": "restart", "node_id": NODE,
               "convoy_id": CONVOY, "expected_runtime_id": "runtime-1",
               "policy": "require_clean", "timeout_s": .1}
    attempt, _ = store.begin_attempt("live-owned", content)
    profile = store.get_profile(NODE)
    store.update_attempt(
        attempt["operation_id"], state="restart_committed",
        committed_at=manager._clock(),
        old_process=profile["last_runtime"]["process"],
        old_runtime_id="runtime-1", dirty_revision="revision:1",
        profile_fingerprint=cl._profile_fingerprint(profile),
        restart_policy="require_clean", requested_timeout_s=.1)
    inspector.backend.processes.pop(100, None)
    runtime.value = None
    manager._enter_operation("live-owned")
    try:
        summary = manager.recover_committed_restarts(recovery_timeout_s=.2)
    finally:
        manager._exit_operation("live-owned")
    assert summary["busy"] == 1 and summary["recovered"] == 0
    assert launcher.spawns == []
    assert store.get_attempt("live-owned")["state"] == "restart_committed"


def test_post_commit_quit_survives_caller_timeout_shorter_than_exit(tmp_path):
    # finding 2385: a short caller timeout_s must not abandon a node already
    # committed to exit. The old runtime takes longer than timeout_s to exit;
    # the restoration budget (not timeout_s) must cover the quit wait.
    manager, _, runtime, inspector, launcher, _, _, _ = make_system(tmp_path)

    def slow_exit_quit(node_id, runtime_id, timeout_s, cancel_event, **kwargs):
        def die():
            inspector.backend.processes.pop(100, None)
            runtime.value = None
        threading.Timer(.3, die).start()
        return {"ok": True}

    runtime.quit = slow_exit_quit
    launcher.callback = auto_confirm(manager, runtime_id="runtime-after-exit")
    result = manager.restart_node(NODE, CONVOY, "restart-slow-exit",
                                  "runtime-1", timeout_s=5)
    assert result["ok"] is True
    assert len(launcher.spawns) == 1


def test_save_then_restart_succeeds_when_project_redirties_after_landed_save(
        tmp_path):
    # finding 2676: Embody re-dirties project.modified within seconds of a good
    # save, so a still-dirty re-read is NOT a failed save once the .toe file
    # itself was rewritten.
    manager, store, runtime, _, launcher, _, toe, _ = make_system(tmp_path)
    runtime.dirty_values = [{"ok": True, "dirty": True, "revision": "r%d" % i}
                            for i in range(1, 6)]

    def save(node_id, runtime_id, timeout_s, cancel_event):
        runtime.saved.append((node_id, runtime_id))
        toe.write_bytes(b"toe-saved-by-touchdesigner-larger-payload")
        return {"ok": True}

    runtime.save = save
    launcher.callback = auto_confirm(manager)
    result = manager.restart_node(
        NODE, CONVOY, "restart-redirty", "runtime-1",
        policy="save_then_restart", timeout_s=5)
    assert result["ok"] is True
    assert runtime.saved == [(NODE, "runtime-1")]
    assert store.get_profile(NODE)["toe_identity"] == cl._file_identity(str(toe))
    assert runtime.quit_calls and launcher.spawns


def test_save_then_restart_still_fails_when_save_never_writes_toe(tmp_path):
    # finding 2676 (negative): a save that leaves the exact .toe unchanged AND
    # the project dirty is still a genuine save_failed.
    manager, _, runtime, _, launcher, _, _, _ = make_system(tmp_path)
    runtime.dirty_values = [{"ok": True, "dirty": True}]
    result = manager.restart_node(
        NODE, CONVOY, "restart-nosave", "runtime-1",
        policy="save_then_restart", timeout_s=5)
    assert result["code"] == "save_failed"
    assert runtime.quit_calls == [] and launcher.spawns == []


def test_indeterminate_fence_releases_once_spawned_child_proven_gone(tmp_path):
    # finding 1981 (release): an indeterminate quarantine keeps its durable
    # reservation only while the child may exist; once the exact child is
    # proven gone a fresh start releases the fence and relaunches.
    system = make_system(tmp_path)
    manager, store, runtime, inspector, launcher, _, _, _ = system
    offline(system)
    # A GENEROUS REAL ceiling, not .1s and not an injected clock. This
    # test is reservation-coupled: the launch reservation's lifetime is
    # sliced from the start budget, so fake time starves the SECOND
    # call's confirm exactly the way a stalled runner does (tried, and
    # it fails deterministically). Meanwhile .1s of real budget is what
    # a stalled windows-latest runner spends before the spawn even
    # lands, turning launch_unconfirmed into deadline_exceeded -- how
    # this failed on main at 8ed43b7 while the same sha passed on dev.
    # Seconds, not tenths: nothing here confirms, so the budget still
    # expires and the attempt still goes indeterminate; the wait just
    # cannot be consumed by a stall first.
    first = manager.start_node(NODE, CONVOY, "wedge-1", timeout_s=3)
    assert first["code"] == "launch_unconfirmed"
    assert store.get_attempt("wedge-1")["state"] == "indeterminate"
    assert runtime.reservations
    inspector.backend.processes.pop(200, None)  # exact child proven gone
    launcher.callback = auto_confirm(manager, runtime_id="runtime-recovered")
    second = manager.start_node(NODE, CONVOY, "wedge-2", timeout_s=30)
    assert second["ok"] is True
    assert len(launcher.spawns) == 2
    assert store.get_attempt("wedge-1")["state"] == "failed"


def test_quarantined_launch_is_confirmable_by_its_exact_child(tmp_path):
    # finding 1981 (confirm): a token-bearing registration whose reservation
    # and spawned snapshot match confirms a quarantined attempt -- the
    # strongest possible proof the launch actually succeeded.
    system = make_system(tmp_path)
    manager, store, runtime, inspector, launcher, _, _, _ = system
    offline(system)
    fake_time(manager)
    first = manager.start_node(NODE, CONVOY, "late-confirm", timeout_s=.1)
    assert first["code"] == "launch_unconfirmed"
    attempt = store.get_attempt("late-confirm")
    assert attempt["state"] == "indeterminate"
    assert attempt["token_hash"] and attempt["token_consumed"] is False
    profile = store.get_profile(NODE)
    token = launcher.spawns[0][1]
    spawned = launcher.spawns[0][2]
    confirmed = manager.confirm_registration(
        NODE, CONVOY, token,
        live_record(profile, "runtime-late", spawned.pid,
                    spawned.reservation_id))
    assert confirmed["ok"] is True
    assert store.get_attempt("late-confirm")["state"] == "succeeded"
    assert runtime.reservations == {}


def test_load_reconciliation_frees_stranded_created_start_attempt(tmp_path):
    # finding 684: a host crash mid start_node leaves a 'created' attempt that
    # would wedge every future lifecycle call at profile_busy forever.
    manager, store, _, _, _, _, _, _ = make_system(tmp_path)
    store.begin_attempt(
        "stranded-created",
        {"operation": "start", "node_id": NODE, "convoy_id": CONVOY})
    with pytest.raises(cl.LifecycleError) as busy:
        store.begin_attempt(
            "new-op",
            {"operation": "start", "node_id": NODE, "convoy_id": CONVOY})
    assert busy.value.code == "profile_busy"
    summary = manager.reconcile_stranded_attempts()
    assert summary["failed"] >= 1
    assert store.get_attempt("stranded-created")["state"] == "failed"
    _, created = store.begin_attempt(
        "new-op", {"operation": "start", "node_id": NODE, "convoy_id": CONVOY})
    assert created is True


def test_load_reconciliation_releases_start_attempt_whose_child_is_dead(
        tmp_path):
    # finding 684/1981: at load, an ACTIVE start attempt whose spawned child is
    # provably dead is failed and its fence released.
    manager, store, runtime, inspector, launcher, _, _, _ = make_system(
        tmp_path)
    offline((manager, store, runtime, inspector, launcher, None, None, None))
    stage_awaiting_registration(
        manager, store, runtime, inspector, "dead-child")
    inspector.backend.processes.pop(200, None)
    assert runtime.reservations
    summary = manager.reconcile_stranded_attempts()
    assert summary["failed"] == 1
    assert store.get_attempt("dead-child")["state"] == "failed"
    assert runtime.reservations == {}


def test_load_reconciliation_keeps_live_child_start_attempt_confirmable(
        tmp_path):
    # finding 684: an ACTIVE start attempt whose child is still alive is left
    # intact so that live child can still register and confirm.
    manager, store, runtime, inspector, launcher, _, _, _ = make_system(
        tmp_path)
    offline((manager, store, runtime, inspector, launcher, None, None, None))
    _, _, record = stage_awaiting_registration(
        manager, store, runtime, inspector, "live-child")
    summary = manager.reconcile_stranded_attempts()
    assert summary["kept"] == 1
    assert store.get_attempt("live-child")["state"] == "awaiting_registration"
    assert runtime.reservations
    confirmed = manager.confirm_registration(NODE, CONVOY, TOKEN, record)
    assert confirmed["ok"] is True
