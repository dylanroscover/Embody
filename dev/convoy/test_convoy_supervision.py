"""Supervision prerequisites: the singleton lock and the clean stop.

These two are what make the host app SUPERVISABLE -- restarted every
minute by a Scheduled Task or a LaunchAgent without corrupting itself.

  - the SINGLETON LOCK stops a second daemon reaching HostStore at all.
    HostStore.__init__ runs _sweep_interrupted_dispatches(), which burns
    every job in `dispatching` to indeterminate. That is right for the
    process's OWN interrupted forwards and catastrophic against a live
    peer's claims -- and 16.4/A-15 make indeterminate records permanent.
    Nothing enforced "one host app per data dir" before this slice.

  - POST /shutdown gives stop/upgrade/uninstall an ORDERLY exit, so
    main()'s `finally` runs and the portfile is cleared. The alternative
    is a hard kill, which on Windows skips cleanup entirely and leaves a
    portfile naming a dead port.

SAFETY: every test drives an explicit data dir under tmp_path. Nothing
here may call acquire_singleton() or main() without one -- the defaults
resolve the REAL per-user Convoy state directory, and a test that locked
(or swept) it would reach into the machine's live host app.
"""

import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

import convoy_hostapp
import convoy_platform as cp


# -- 1a. the lock itself ------------------------------------------------

def test_a_handle_is_truthy_and_names_its_lock_file(tmp_path):
    handle = cp.acquire_singleton(str(tmp_path))
    try:
        assert handle
        assert handle.path == os.path.join(str(tmp_path), cp.LOCK_FILE)
        assert os.path.isfile(handle.path)
    finally:
        cp.release_singleton(handle)


def test_a_second_acquire_is_refused_while_the_first_is_held(tmp_path):
    """THE property the whole slice exists for."""
    first = cp.acquire_singleton(str(tmp_path))
    try:
        assert first
        assert cp.acquire_singleton(str(tmp_path)) is None
    finally:
        cp.release_singleton(first)


def test_the_slot_is_free_again_after_release(tmp_path):
    first = cp.acquire_singleton(str(tmp_path))
    cp.release_singleton(first)
    second = cp.acquire_singleton(str(tmp_path))
    try:
        assert second
    finally:
        cp.release_singleton(second)


def test_release_is_idempotent(tmp_path):
    handle = cp.acquire_singleton(str(tmp_path))
    cp.release_singleton(handle)
    cp.release_singleton(handle)         # must not raise
    cp.release_singleton(None)           # nor on nothing at all
    assert handle.released is True


def test_distinct_data_dirs_do_not_contend(tmp_path):
    a = cp.acquire_singleton(str(tmp_path / "a"))
    b = cp.acquire_singleton(str(tmp_path / "b"))
    try:
        assert a and b
    finally:
        cp.release_singleton(a)
        cp.release_singleton(b)


def test_the_lock_file_is_never_truncated_by_acquiring_it(tmp_path):
    """A truncating open would let a new process clobber a live
    holder's file -- on POSIX that is how a lock file gets replaced out
    from under its owner.

    Measured, not assumed: msvcrt's byte-range lock on Windows is
    MANDATORY, so while the lock is held the locked byte cannot even be
    READ by another handle (PermissionError). Size is checkable either
    way, and the content check moves after the release.
    """
    path = tmp_path / cp.LOCK_FILE
    path.write_bytes(b"pre-existing bytes")
    handle = cp.acquire_singleton(str(tmp_path))
    try:
        assert path.stat().st_size == len(b"pre-existing bytes")
    finally:
        cp.release_singleton(handle)
    assert path.read_bytes() == b"pre-existing bytes"


# -- 1a. a CRASH releases it (why this is a lock, not a pid file) -------

_HOLDER = """
import os, sys, time
sys.path.insert(0, sys.argv[1])
import convoy_platform as cp
handle = cp.acquire_singleton(sys.argv[2])
sys.stdout.write("held\\n" if handle else "refused\\n")
sys.stdout.flush()
time.sleep(120)
"""


def test_a_hard_killed_holder_releases_the_lock(tmp_path):
    """The whole reason this is an OS lock and not a pid file: no
    graceful shutdown covers SIGKILL, a power cut, or Windows
    terminate(). The kernel drops the lock when the process dies,
    however it dies -- a pid file would survive and block forever."""
    directory = str(tmp_path)
    here = os.path.dirname(os.path.abspath(__file__))
    child = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, here, directory],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "held"
        # While it lives, WE are refused -- proves the lock is real
        # across processes, not merely within one.
        assert cp.acquire_singleton(directory) is None
    finally:
        child.kill()
        child.wait(timeout=30)

    # Windows can take a moment to tear the handle down after kill().
    deadline = time.time() + 15.0
    handle = None
    while time.time() < deadline:
        handle = cp.acquire_singleton(directory)
        if handle:
            break
        time.sleep(0.1)
    try:
        assert handle, "a killed holder must not keep the slot"
    finally:
        cp.release_singleton(handle)


# -- 1a. both platform branches, on any machine (D-5) -------------------

def test_default_locker_selects_msvcrt_on_win32_and_flock_on_posix():
    """Named selection, so the branch a given machine never runs is
    still asserted on it."""
    assert cp.default_locker("win32") is cp._win32_lock
    assert cp.default_locker("darwin") is cp._posix_lock
    assert cp.default_locker("linux") is cp._posix_lock


class _FakeFile:
    def __init__(self):
        self.closed = False
        self.position = None

    def seek(self, offset):
        self.position = offset

    def fileno(self):
        return 999

    def close(self):
        self.closed = True


def test_a_locker_that_raises_oserror_reads_as_held(tmp_path):
    """The FOREIGN branch's refusal, without owning that platform: both
    msvcrt.locking and fcntl.flock signal contention by raising
    OSError, so one injected raiser proves both."""
    fake = _FakeFile()

    def refuse(_fileobj):
        raise BlockingIOError(11, "Resource temporarily unavailable")

    got = cp.acquire_singleton(str(tmp_path), opener=lambda p: fake,
                               locker=refuse)
    assert got is None
    assert fake.closed, "a refused acquire must not leak its file handle"


def test_an_injected_locker_supplies_the_unlocker_release_calls(tmp_path):
    calls = []
    fake = _FakeFile()

    def lock(_fileobj):
        calls.append("lock")
        return lambda: calls.append("unlock")

    handle = cp.acquire_singleton(str(tmp_path), opener=lambda p: fake,
                                  locker=lock)
    assert handle
    cp.release_singleton(handle)
    assert calls == ["lock", "unlock"]
    assert fake.closed


def test_a_locker_may_return_no_unlocker_at_all(tmp_path):
    """Closing the file releases the lock on both platforms, so an
    unlocker is optional. release_singleton must not assume one."""
    fake = _FakeFile()
    handle = cp.acquire_singleton(str(tmp_path), opener=lambda p: fake,
                                  locker=lambda f: None)
    assert handle
    cp.release_singleton(handle)        # must not raise
    assert fake.closed


def test_an_opener_failure_propagates_and_is_never_read_as_held(tmp_path):
    """An unwritable data dir is a BROKEN INSTALL, not a busy one.
    Reporting it as 'already running' would exit 0 every minute forever
    with a message naming the wrong cause -- the silent death loop this
    slice exists to avoid."""
    def explode(_path):
        raise PermissionError(13, "Permission denied")

    with pytest.raises(PermissionError):
        cp.acquire_singleton(str(tmp_path), opener=explode,
                             locker=lambda f: None)


# -- 1b. main() --singleton --------------------------------------------

def test_main_exits_zero_and_touches_nothing_when_the_lock_is_held(
        tmp_path, capsys):
    """THE load-bearing ordering test. The lock must be taken BEFORE
    HostApp(), because HostStore.__init__ sweeps interrupted dispatches
    the moment it opens the data dir -- a second daemon that got as far
    as constructing the app has already burned the first one's live
    claims to indeterminate, permanently."""
    directory = str(tmp_path)
    holder = cp.acquire_singleton(directory)
    try:
        assert holder
        assert convoy_hostapp.main(["--data-dir", directory]) == 0
    finally:
        cp.release_singleton(holder)

    # Nothing HostStore or HostApp creates may exist: no registry, no
    # jobs dir, not even the IPC token.
    for name in ("host.json", "jobs", "host.token", "audit.jsonl",
                 cp.PORT_FILE):
        assert not os.path.exists(os.path.join(directory, name)), (
            f"{name} proves HostApp was constructed behind a held lock")


def test_the_refusal_prints_exactly_one_line_naming_the_situation(
        tmp_path, capsys):
    directory = str(tmp_path)
    holder = cp.acquire_singleton(directory)
    try:
        convoy_hostapp.main(["--data-dir", directory])
    finally:
        cp.release_singleton(holder)
    err = capsys.readouterr().err.strip()
    assert err.count("\n") == 0, "one line, so a log tail stays readable"
    assert "already running" in err
    assert directory in err


def test_singleton_is_on_by_default_in_the_real_parser():
    """DEFAULT ON is the safety property: a launcher that forgot the
    flag must still be protected. Asserted against the shipped parser,
    never a rebuilt copy of it."""
    parser = convoy_hostapp.build_parser()
    assert parser.parse_args([]).singleton is True
    assert parser.parse_args(["--singleton"]).singleton is True
    assert parser.parse_args(["--no-singleton"]).singleton is False


def test_no_singleton_starts_even_behind_a_held_lock(tmp_path):
    """The escape hatch exists for tests; it must actually bypass."""
    directory = str(tmp_path)
    holder = cp.acquire_singleton(directory)
    started = threading.Event()
    result = {}

    def run():
        result["rc"] = convoy_hostapp.main(
            ["--data-dir", directory, "--no-singleton", "--port", "0"])

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        port, token = _wait_for_host(directory, started)
        assert port                     # it really did start
    finally:
        _stop_host(directory, token)
        thread.join(timeout=30)
        cp.release_singleton(holder)


# -- 1c. POST /shutdown -------------------------------------------------

def _wait_for_host(directory, _started=None, timeout_s=30.0):
    """Block until the daemon has published a portfile. Returns
    (port, token)."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        data = cp.read_portfile(directory)
        if data and data.get("port"):
            token = cp.ensure_ipc_token(directory)
            return data["port"], token
        time.sleep(0.05)
    raise AssertionError("the host app never published a portfile")


def _post(port, path, token=None, body=None, timeout=10.0):
    """(status, parsed body). HTTPError codes ride back as values, so a
    401 is an assertion about a response and not an exception."""
    payload = json.dumps(body if body is not None else {}).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers[convoy_hostapp.TOKEN_HEADER] = token
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                 data=payload, method="POST",
                                 headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def _stop_host(directory, token):
    data = cp.read_portfile(directory)
    if data and token:
        try:
            _post(data["port"], "/shutdown", token=token)
        except OSError:
            pass


class _RunningHost:
    """A real host app on real loopback, in this process."""

    def __init__(self, directory, extra=None):
        self.directory = directory
        self.thread = threading.Thread(
            target=lambda: convoy_hostapp.main(
                ["--data-dir", directory, "--port", "0"] + (extra or [])),
            daemon=True)

    def __enter__(self):
        self.thread.start()
        self.port, self.token = _wait_for_host(self.directory)
        return self

    def __exit__(self, *exc):
        if self.thread.is_alive():
            _stop_host(self.directory, self.token)
            self.thread.join(timeout=30)


def test_shutdown_refuses_an_unauthenticated_caller(tmp_path):
    with _RunningHost(str(tmp_path)) as host:
        status, body = _post(host.port, "/shutdown")
        assert status == 401
        assert body["reason"] == "unauthenticated"
        # ...and it is STILL SERVING: an unauthenticated stop that
        # actually stopped the daemon would be a denial-of-service any
        # process on the box could fire without the token.
        assert _post(host.port, "/shutdown", token="0" * 64)[0] == 401
        assert cp.read_live_portfile(str(tmp_path)) is not None


def test_shutdown_stops_the_daemon_and_clears_the_portfile(tmp_path):
    """THE POINT of the route. A hard kill leaves a portfile naming a
    dead port; an orderly exit unwinds main()'s finally and clears it."""
    directory = str(tmp_path)
    host = _RunningHost(directory)
    with host:
        assert cp.read_portfile(directory) is not None
        status, body = _post(host.port, "/shutdown", token=host.token)
        assert status == 200
        assert body["ok"] is True and body["stopping"] is True
        host.thread.join(timeout=30)
        assert not host.thread.is_alive(), "the server did not stop"
    assert cp.read_portfile(directory) is None, (
        "the portfile must not outlive an orderly shutdown")


def test_shutdown_is_audited(tmp_path):
    directory = str(tmp_path)
    with _RunningHost(directory) as host:
        _post(host.port, "/shutdown", token=host.token)
        host.thread.join(timeout=30)
    with open(os.path.join(directory, "audit.jsonl"),
              "r", encoding="utf-8") as f:
        events = [json.loads(line)["event"] for line in f if line.strip()]
    assert "shutdown_requested" in events
    assert "stopped" in events


def test_shutdown_releases_the_singleton_for_the_next_launch(tmp_path):
    """Stop -> install -> start is the upgrade path; if the lock
    outlived the process the restart would refuse itself."""
    directory = str(tmp_path)
    with _RunningHost(directory) as host:
        assert cp.acquire_singleton(directory) is None      # held
        _post(host.port, "/shutdown", token=host.token)
        host.thread.join(timeout=30)
    handle = cp.acquire_singleton(directory)
    try:
        assert handle
    finally:
        cp.release_singleton(handle)


# -- the hard-kill path: a stale portfile must never be handed out -----

def test_a_portfile_naming_a_REALLY_DEAD_pid_is_rejected(tmp_path):
    """THE PROPERTY THE SUPERVISOR KILL PATH RESTS ON, measured
    end-to-end 2026-08-01 against a real Scheduled Task:

      `schtasks /End` stops the daemon in 1.3 s and the task returns to
      Status Ready -- but it is a KILL. main()'s `finally` never runs,
      so the portfile OUTLIVES the process naming a dead pid (observed:
      the file still read pid 72712 / port 11830 after the process was
      gone). POST /shutdown is what avoids that, and it is why it exists.

    Nothing can clean that up in general -- no handler covers SIGKILL or
    power loss -- so the whole path is survivable for exactly one
    reason: read_live_portfile verifies the writer is alive before
    handing the port out. Weaken that check and every client starts
    dialling dead ports.

    It IS already covered for the POSIX branch with an injected kill
    (test_convoy_platform.test_live_portfile_hides_a_dead_writer). This
    one closes the case that measurement actually exercised: a REAL
    portfile, a REAL dead pid, and the REAL un-injected liveness probe
    -- OpenProcess via ctypes on win32, signal 0 on POSIX. An injected
    kill cannot prove the ctypes branch, and the ctypes branch is the
    one that runs on the platform this was measured on.
    """
    directory = str(tmp_path)
    # A pid that is genuinely dead, not merely unused: spawn, reap, and
    # only then write the portfile that names it.
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait(timeout=30)
    dead_pid = child.pid

    deadline = time.time() + 15.0
    while cp.pid_is_alive(dead_pid) and time.time() < deadline:
        time.sleep(0.1)
    assert not cp.pid_is_alive(dead_pid), (
        "the probe still reports a reaped child as alive")

    cp.write_portfile(directory, 11830, dead_pid, "h" * 32)
    # The raw read still sees it -- that is the hazard, not the bug.
    assert cp.read_portfile(directory)["pid"] == dead_pid
    # The live read is what every client goes through, and it must not.
    assert cp.read_live_portfile(directory) is None, (
        "a portfile whose writer is dead was handed out as live -- this "
        "is exactly what survives a supervisor kill")


def test_an_embedded_host_app_refuses_shutdown_rather_than_lying(tmp_path):
    """A HostApp built without serve() has no server to stop. Reporting
    success would tell a caller the daemon is going away when nothing
    performed the stop."""
    app = convoy_hostapp.HostApp(str(tmp_path))
    try:
        code, payload = app.request_shutdown({})
        assert code == 409
        assert payload["reason"] == "shutdown_unavailable"
    finally:
        app.db.close()


def test_shutdown_fires_exactly_the_hook_it_was_given(tmp_path):
    """One shutdown path, two triggers. main() hands request_shutdown
    the SAME callable it installs for SIGTERM; if the route grew its own
    server-stopping code the two would drift and only one would ever be
    exercised."""
    app = convoy_hostapp.HostApp(str(tmp_path))
    fired = []
    try:
        app.set_shutdown_hook(lambda: fired.append(True))
        code, payload = app.request_shutdown({})
        assert (code, payload["ok"]) == (200, True)
        assert fired == [True], "the route must call the injected hook"
    finally:
        app.db.close()


def test_the_signal_handler_and_the_route_share_one_callable():
    """Read main() itself: the hook handed to set_shutdown_hook is the
    same `_stop` registered with signal.signal, not a second function
    that happens to look like it."""
    import inspect
    source = inspect.getsource(convoy_hostapp.main)
    assert "app.set_shutdown_hook(_stop)" in source
    assert "signal.signal(sig, _stop)" in source
