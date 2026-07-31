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


def test_missing_portfile_is_not_live(tmp_path):
    assert cp.read_live_portfile(str(tmp_path)) is None
