"""Platform layer: every branch runs on every machine (D-5)."""

import os
import stat
import sys

import pytest

import convoy_platform as cp


# -- data dir, all three platforms, from any host ----------------------

def test_win32_uses_local_appdata_not_roaming():
    """Roaming would carry identity onto a second machine and silently
    break A-12's clone-uniqueness."""
    got = cp.data_dir(platform="win32",
                      env={"LOCALAPPDATA": r"C:\Users\x\AppData\Local"},
                      home=r"C:\Users\x")
    # LITERAL expectation, never os.path.join: this asserts a WINDOWS
    # path, and os.path.join yields forward slashes on the macOS
    # runner. The identical mistake failed the first-ever macOS CI run
    # of the bridge suite -- it must not be reintroduced here.
    assert got == r"C:\Users\x\AppData\Local\EmbodyConvoy"
    assert "Roaming" not in got


def test_win32_falls_back_when_localappdata_unset():
    got = cp.data_dir(platform="win32", env={}, home=r"C:\Users\x")
    assert got == r"C:\Users\x\AppData\Local\EmbodyConvoy"


def test_darwin_uses_application_support():
    got = cp.data_dir(platform="darwin", env={}, home="/Users/x")
    assert got == "/Users/x/Library/Application Support/EmbodyConvoy"


def test_linux_prefers_xdg_state_home():
    got = cp.data_dir(platform="linux",
                      env={"XDG_STATE_HOME": "/custom/state"},
                      home="/home/x")
    assert got == "/custom/state/embody-convoy"


def test_linux_falls_back_to_local_state():
    got = cp.data_dir(platform="linux", env={}, home="/home/x")
    assert got == "/home/x/.local/state/embody-convoy"


def test_data_dir_defaults_to_the_running_platform():
    assert cp.data_dir()


# -- token -------------------------------------------------------------

def test_token_is_256_bits_of_hex(tmp_path):
    token = cp.ensure_ipc_token(str(tmp_path))
    assert len(token) == 64
    int(token, 16)


def test_token_is_minted_once_and_reused(tmp_path):
    first = cp.ensure_ipc_token(str(tmp_path))
    assert cp.ensure_ipc_token(str(tmp_path)) == first


def test_distinct_installs_get_distinct_tokens(tmp_path):
    a = cp.ensure_ipc_token(str(tmp_path / "a"))
    b = cp.ensure_ipc_token(str(tmp_path / "b"))
    assert a != b


def test_an_empty_token_file_is_reminted(tmp_path):
    """A truncated write must not yield an empty shared secret."""
    directory = str(tmp_path)
    path = os.path.join(directory, cp.TOKEN_FILE)
    os.makedirs(directory, exist_ok=True)
    open(path, "w").close()
    token = cp.ensure_ipc_token(directory)
    assert len(token) == 64


@pytest.mark.skipif(sys.platform == "win32",
                    reason="POSIX mode bits; NTFS ACLs govern on Windows")
def test_token_file_is_owner_only_on_posix(tmp_path):
    directory = str(tmp_path)
    cp.ensure_ipc_token(directory)
    mode = os.stat(os.path.join(directory, cp.TOKEN_FILE)).st_mode
    assert not (mode & (stat.S_IRGRP | stat.S_IROTH)), (
        "the IPC token must not be group/world readable")


def test_no_temp_file_is_left_behind(tmp_path):
    directory = str(tmp_path)
    cp.ensure_ipc_token(directory)
    cp.write_portfile(directory, 9999, 1234, "h" * 32)
    leftovers = [n for n in os.listdir(directory) if n.endswith(".tmp")]
    assert leftovers == []


# -- _write_private: the rename retry, and the sync ----------------------
#
# THE SEAMS ARE THE POINT. This loop is a deadline, and a deadline
# asserted against a real clock on a shared CI runner measures the
# runner, not the code -- so `replace` and `sleep` are injected here
# exactly as convoy_lifecycle._atomic_replace injects them, and not one
# assertion below waits on wall time.


class _HeldOpen:
    """A destination a reader is holding open, as Windows reports it.

    os.replace onto a file another process has open raises
    PermissionError (WinError 32) -- the sharing violation the retry loop
    exists for. Each destination refuses a FIXED number of attempts and
    then succeeds, so a whole population's contention is deterministic
    and replayable rather than a race with a real reader.
    """

    def __init__(self, holds=None):
        self.holds = dict(holds or {})
        self.attempts = {}

    def __call__(self, source, destination):
        seen = self.attempts.get(destination, 0)
        self.attempts[destination] = seen + 1
        if seen < self.holds.get(destination, 0):
            raise PermissionError(
                32, "The process cannot access the file because it is "
                    "being used by another process")
        os.replace(source, destination)


def test_the_rename_retries_a_windows_style_sharing_violation(tmp_path):
    """Same contract as _atomic_replace, and the same schedule: the
    first retry is immediate (the hot path pays nothing), then a 5 ms
    linear backoff."""
    target = str(tmp_path / "host.json")
    replace = _HeldOpen({target: 3})
    sleeps = []

    cp._write_private(target, "payload\n", replace=replace,
                      sleep=sleeps.append)

    assert replace.attempts[target] == 4
    assert sleeps == [.005, .010]
    with open(target, encoding="utf-8") as handle:
        assert handle.read() == "payload\n"


def test_the_seams_are_KEYWORD_ONLY(tmp_path):
    """ARITY GUARD, same as _atomic_replace's `*`. Every one of the
    nineteen production call sites passes exactly (path, data)
    positionally, so a seam reachable positionally is a seam a future
    third parameter binds to silently -- and `replace` is the one
    parameter that decides whether the file is written at all."""
    import inspect
    params = inspect.signature(cp._write_private).parameters
    for name in ("replace", "sleep"):
        assert params[name].kind == inspect.Parameter.KEYWORD_ONLY, (
            "%s must be keyword-only" % (name,))
    with pytest.raises(TypeError):
        cp._write_private(str(tmp_path / "x.json"), "data", os.replace)


def test_a_reader_that_never_lets_go_raises_and_leaves_no_temp(tmp_path):
    """Exhaustion RAISES. The caller has to hear that the write did not
    land -- and the temp file it opened must not survive the failure."""
    directory = str(tmp_path)
    target = os.path.join(directory, "host.json")
    sleeps = []

    def never(source, destination):
        raise PermissionError(32, "still open")

    with pytest.raises(PermissionError):
        cp._write_private(target, "payload\n", replace=never,
                          sleep=sleeps.append)

    assert not os.path.exists(target)
    assert [n for n in os.listdir(directory) if n.endswith(".tmp")] == []
    assert len(sleeps) == cp._REPLACE_ATTEMPTS - 2, (
        "attempt 0 is immediate and the last attempt raises instead of "
        "sleeping, so there is one sleep for every attempt between")


def test_the_retry_budget_outlasts_a_runner_stall(tmp_path):
    """THE NUMBER, pinned. 8 attempts bought 105 ms, which is less than
    the 100 ms+ a shared runner stalls for at arbitrary points -- and
    that is what surfaced as per-record errors in a 250-record
    revocation on windows-latest. A silent trim back to that budget must
    fail here rather than on someone else's CI run."""
    sleeps = []

    def never(source, destination):
        raise PermissionError(32, "still open")

    with pytest.raises(PermissionError):
        cp._write_private(str(tmp_path / "host.json"), "x",
                          replace=never, sleep=sleeps.append)

    assert sum(sleeps) >= .25, (
        "the rename budget is %.3fs, which does not cover a runner stall"
        % (sum(sleeps),))


def test_the_content_is_SYNCED_before_the_rename(tmp_path, monkeypatch):
    """os.replace is atomic for the NAME and says nothing about the DATA
    reaching the disk. The sync therefore has to happen while the TEMP
    file is still open -- before the rename, never after it.

    The destination's existence at sync time is the proof of ordering:
    this is a first write, so a sync that ran before the rename cannot
    see it.
    """
    target = str(tmp_path / "state" / "host.json")
    order = []
    real_fsync = os.fsync

    def spy_fsync(fd):
        order.append(("fsync", os.path.exists(target)))
        return real_fsync(fd)

    def replace(source, destination):
        order.append(("replace", os.path.exists(target)))
        os.replace(source, destination)

    monkeypatch.setattr(cp.os, "fsync", spy_fsync)
    cp._write_private(target, "durable\n", replace=replace)

    assert order[0] == ("fsync", False), (
        "the data was not synced before the rename: %r" % (order,))
    assert order[1][0] == "replace"
    with open(target, encoding="utf-8") as handle:
        assert handle.read() == "durable\n"


def test_a_MASS_rewrite_completes_under_intermittent_contention(tmp_path):
    """THE TEST 16ca52c OWED, and the condition it set for the sync
    coming back: a mass rewrite under contention.

    A peer revocation rewrites every affected delivery record through
    this function, back to back, inside the host lock. The withdrawal
    commit measured 250 records with 7 per-record errors on
    windows-latest and blamed the sync for holding the DESTINATION open
    longer -- but this function never opens the destination at all; the
    concurrent reader does. What the sync actually costs is elapsed
    time, and what a hold costs is retries. So the population here is
    held open for up to 10 attempts -- PAST the 8 the loop used to
    allow -- and every record must still land, with no exception
    reaching the caller.
    """
    directory = str(tmp_path / "jobs")
    os.makedirs(directory)
    records = [os.path.join(directory, "job-%03d.json" % index)
               for index in range(250)]

    # Clean first pass: this is a REwrite test, so there has to be
    # something to rewrite.
    for path in records:
        cp._write_private(path, '{"state": "queued"}\n')

    # Every fifth record is held, and the hold deepens 1..10 so the
    # population straddles the old budget instead of sitting under it.
    holds = {path: 1 + (index // 5) % 10
             for index, path in enumerate(records) if index % 5 == 0}
    assert max(holds.values()) > 8, (
        "the deepest hold must outlast the 8-attempt loop, or this "
        "proves nothing about the budget")
    replace = _HeldOpen(holds)
    sleeps = []

    errors = []
    for path in records:
        try:
            cp._write_private(path, '{"state": "refused"}\n',
                              replace=replace, sleep=sleeps.append)
        except Exception as e:                       # noqa: BLE001
            errors.append((path, repr(e)))

    assert errors == [], (
        "%d of %d records failed to rewrite under contention -- exactly "
        "the per-record errors a revocation reports as a failed "
        "containment: %r" % (len(errors), len(records), errors[:3]))
    for path in records:
        with open(path, encoding="utf-8") as handle:
            assert handle.read() == '{"state": "refused"}\n'
    assert [n for n in os.listdir(directory)
            if n.endswith(".tmp")] == []
    assert sleeps, "the held records must have gone round the retry loop"
    assert set(sleeps) <= {cp._REPLACE_BACKOFF_S * n
                           for n in range(1, cp._REPLACE_ATTEMPTS)}


# -- portfile ------------------------------------------------------------

def test_portfile_round_trips(tmp_path):
    directory = str(tmp_path)
    cp.write_portfile(directory, 9999, 4242, "h" * 32)
    data = cp.read_portfile(directory)
    assert data["port"] == 9999
    assert data["pid"] == 4242
    assert data["protocol"] == "convoy-host/1"


def test_missing_portfile_reads_as_none(tmp_path):
    assert cp.read_portfile(str(tmp_path)) is None


def test_portfile_without_a_port_reads_as_none(tmp_path):
    directory = str(tmp_path)
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, cp.PORT_FILE), "w") as f:
        f.write('{"pid": 1}')
    assert cp.read_portfile(directory) is None


def test_clear_portfile_is_safe_when_absent(tmp_path):
    cp.clear_portfile(str(tmp_path))    # must not raise


# -- liveness: a portfile is a hint, never a fact -----------------------

def test_live_portfile_hides_a_dead_writer(tmp_path):
    """The bug the first exit-proof run exposed: on Windows terminate()
    skips the cleanup handler, so the portfile outlives the process. A
    client reading it naively would dial a dead port."""
    directory = str(tmp_path)
    cp.write_portfile(directory, 9999, 4242, "h" * 32)
    assert cp.read_portfile(directory) is not None, "the raw read sees it"
    assert cp.read_live_portfile(
        directory, platform="linux",
        kill=_dead_kill) is None, "the live read must not"


def test_live_portfile_returns_a_live_writer(tmp_path):
    directory = str(tmp_path)
    cp.write_portfile(directory, 9999, os.getpid(), "h" * 32)
    assert cp.read_live_portfile(directory)["port"] == 9999


def _dead_kill(pid, sig):
    raise ProcessLookupError(pid)


def _alive_kill(pid, sig):
    return None


def test_pid_alive_posix_branch_runs_everywhere():
    assert cp.pid_is_alive(123, platform="linux", kill=_alive_kill) is True
    assert cp.pid_is_alive(123, platform="darwin", kill=_dead_kill) is False


def test_pid_alive_posix_never_signals_for_real():
    """os.kill on Windows TERMINATES -- the injected kill must be the
    only thing a foreign-platform test ever calls."""
    seen = []
    cp.pid_is_alive(4242, platform="darwin",
                    kill=lambda pid, sig: seen.append((pid, sig)))
    assert seen == [(4242, 0)], "signal 0 only, exactly the given pid"


@pytest.mark.parametrize("bad", [0, -1, None])
def test_pid_alive_refuses_nonsense(bad):
    assert cp.pid_is_alive(bad, platform="linux", kill=_alive_kill) is False


def test_pid_alive_on_this_host_is_true_for_us():
    assert cp.pid_is_alive(os.getpid()) is True


# -- the win32 branch, driven through an injected kernel32 -------------

class _FakeKernel32:
    """Just enough of kernel32 to drive both OpenProcess outcomes on any
    platform (D-5: a branch you cannot exercise off-Windows is a branch
    nobody reviews)."""

    def __init__(self, handle=0, last_error=0, wait=0):
        self._handle = handle
        self._last_error = last_error
        self._wait = wait
        self.opened = []
        self.closed = []

    def OpenProcess(self, access, inherit, pid):
        self.opened.append((access, pid))
        return self._handle

    def GetLastError(self):
        return self._last_error

    def WaitForSingleObject(self, handle, ms):
        return self._wait

    def CloseHandle(self, handle):
        self.closed.append(handle)


def test_win32_access_denied_means_the_process_EXISTS():
    """The mirror of the POSIX EPERM rule three branches below. Mapping
    ACCESS_DENIED to dead lets a host app at another integrity level
    (elevated, or a service-mode supervisor) read as gone -- so every
    client probes STALE forever while /health answers fine.

    Measured on Windows 11: pid 4 (System, alive) -> OpenProcess NULL,
    GetLastError 5."""
    k = _FakeKernel32(handle=0, last_error=5)
    assert cp.pid_is_alive(4, platform="win32", kernel32=k) is True


def test_win32_invalid_parameter_means_gone():
    """An unused pid measured on Windows 11 -> GetLastError 87."""
    k = _FakeKernel32(handle=0, last_error=87)
    assert cp.pid_is_alive(999999, platform="win32", kernel32=k) is False


def test_win32_signaled_process_object_means_exited():
    """WAIT_OBJECT_0 happens exactly on exit -- a handle alone stays
    openable after death and would read alive forever."""
    k = _FakeKernel32(handle=1234, wait=0)
    assert cp.pid_is_alive(4242, platform="win32", kernel32=k) is False
    assert k.closed == [1234], "the handle must always be closed"


def test_win32_unsignaled_process_object_means_running():
    k = _FakeKernel32(handle=1234, wait=0x102)      # WAIT_TIMEOUT
    assert cp.pid_is_alive(4242, platform="win32", kernel32=k) is True
    assert k.closed == [1234]


def test_win32_branch_never_calls_kill():
    def forbidden(pid, sig):
        raise AssertionError("os.kill must never run on the win32 branch")
    k = _FakeKernel32(handle=1234, wait=0x102)
    assert cp.pid_is_alive(4242, platform="win32", kernel32=k,
                           kill=forbidden) is True


def test_win32_asks_for_synchronize_only():
    """SYNCHRONIZE is what makes WaitForSingleObject meaningful; opening
    with a weaker right would make the wait fail and reintroduce the
    'openable after death reads alive' bug."""
    k = _FakeKernel32(handle=1234, wait=0x102)
    cp.pid_is_alive(4242, platform="win32", kernel32=k)
    assert k.opened == [(0x00100000, 4242)]


def test_a_broken_kernel32_reads_as_dead_not_an_exception():
    class Exploding:
        def OpenProcess(self, *a):
            raise OSError("ctypes went wrong")
    assert cp.pid_is_alive(4242, platform="win32",
                           kernel32=Exploding()) is False


def test_the_real_win32_branch_sees_a_protected_but_live_process():
    """The un-injected path, on the box where it matters: pid 4 (System)
    is alive and unopenable. This is the exact case that read as dead."""
    if sys.platform != "win32":
        pytest.skip("exercises the real OpenProcess on Windows")
    assert cp.pid_is_alive(4) is True


def test_missing_portfile_is_not_live(tmp_path):
    assert cp.read_live_portfile(str(tmp_path)) is None
