"""
Test suite: the Convoy host-app client (Embody/convoy/convoy_client.py).

Dual-runner, like test_envoy_bridge.py: the module under test is pure
stdlib Python with no TouchDesigner imports, so this whole file runs
under plain pytest on the windows+macos CI matrix AND inside TD via
TestRunnerExt. Nothing here needs a live session.

The two load-bearing tests are the ones that cannot be written anywhere
else:

  - PARITY against dev/convoy/convoy_hostprobe.py. convoy_client exists
    only because ConvoyExt cannot import that module (dev/convoy/ is not
    in a released .tox), which means the decision tree now lives in two
    files. Two copies drift. This test is the thing that stops it, so it
    drives BOTH modules with one set of kwargs and demands they agree.

  - INTEGRATION against a real in-process HostApp over real loopback
    HTTP: register -> the host reports the port -> unregister -> the
    port is cleared -> re-register restores it. No mocks, no TD.

SAFETY: no test may call probe() / read_token() / read_live_portfile()
without an explicit data_dir. The defaults resolve the REAL per-user
Convoy state directory, and a test that reads (or worse, writes) it
would reach into the machine's live host app.
"""

import ast
import importlib.util
import json
import os
import re
import sys
import threading
import time
import types
import unittest
import urllib.error
import urllib.request

# unit_tests/ -> embody/ -> dev/ -> the repo root. __file__ is the real
# path under BOTH runners (TestRunnerExt loads these modules with
# spec_from_file_location off disk), so this resolves the checkout even
# though the pytest shim points project.folder at a sandbox copy.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
_CLIENT_PATH = os.path.join(_REPO_ROOT, 'dev', 'embody', 'Embody',
                            'convoy', 'convoy_client.py')
_CONVOY_DIR = os.path.join(_REPO_ROOT, 'dev', 'convoy')

_spec = importlib.util.spec_from_file_location('convoy_client', _CLIENT_PATH)
client = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(client)
sys.modules[_spec.name] = client

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase

HOST_ID = 'h' * 32
OTHER_HOST_ID = 'x' * 32


def _in_touchdesigner():
    """True in a live TD session, False under plain pytest.

    CONSERVATIVE BY CONSTRUCTION -- anything not provably plain pytest
    counts as TD, because guessing wrong means binding a listening
    socket and making multi-second blocking HTTP calls on TD's MAIN
    thread (the closed-port probe alone measures ~2.7 s here, roughly
    160 dropped frames).

    TestRunnerExt injects the real `project` into each test module's
    globals; the pytest shim injects a bare SimpleNamespace into
    builtins instead, which never appears in globals() at all.
    """
    proj = globals().get('project')
    if proj is not None and not isinstance(proj, types.SimpleNamespace):
        return True
    return 'pytest' not in sys.modules


def _load_convoy_module(name):
    """Import a dev/convoy module, or None when this checkout has none.

    dev/convoy/ is tracked and pushed, so on CI and on a dev box it is
    always there -- but a user project that only ships the Embody COMP
    has no such directory, and the suite must skip rather than error.
    """
    path = os.path.join(_CONVOY_DIR, name + '.py')
    if not os.path.isfile(path):
        return None
    if _CONVOY_DIR not in sys.path:
        sys.path.insert(0, _CONVOY_DIR)
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _live_reader(port=8080, pid=4242, host_id=HOST_ID):
    return lambda d: {'port': port, 'pid': pid, 'host_id': host_id,
                      'protocol': 'convoy-host/1'}


class _Resp:
    """Minimal stand-in for the urlopen context manager."""

    def __init__(self, payload):
        self._payload = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._payload


class _FakeBody:
    def __init__(self, text):
        self._text = text.encode()

    def read(self):
        return self._text

    def close(self):
        # urllib closes real response bodies; a double without close()
        # raises AttributeError from teardown paths on some runners.
        pass


class _FakeKernel32:
    """Just enough of kernel32 to drive both OpenProcess outcomes on any
    platform -- a branch you cannot exercise off-Windows is a branch
    nobody reviews. Mirrors dev/convoy/test_convoy_platform.py's copy."""

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


def _refusing_opener(code, payload):
    def opener(req, timeout=None):
        raise urllib.error.HTTPError(
            req.full_url, code, 'refused', {},
            _FakeBody(json.dumps(payload)))
    return opener


# =====================================================================
# The probe decision tree
# =====================================================================

class TestConvoyClientProbeTree(EmbodyTestCase):
    """Exactly three outcomes, and absence is never a failure."""

    def test_running_when_a_live_host_app_is_found(self):
        r = client.probe(data_dir='/x',
                         portfile_reader=_live_reader(),
                         token_reader=lambda d: 'tok',
                         health_check=lambda handle: HOST_ID)
        self.assertEqual(r.status, client.STATUS_RUNNING)
        self.assertTrue(r.use_convoy)
        self.assertEqual(r.handle.port, 8080)
        self.assertEqual(r.handle.base_url, 'http://127.0.0.1:8080')
        self.assertEqual(r.handle.token, 'tok')
        self.assertEqual(r.handle.host_id, HOST_ID)

    def test_absent_when_there_is_no_portfile_at_all(self):
        r = client.probe(data_dir='/x',
                         portfile_reader=lambda d: None,
                         raw_portfile_reader=lambda d: None,
                         token_reader=lambda d: 'tok')
        self.assertEqual(r.status, client.STATUS_ABSENT)
        self.assertFalse(r.use_convoy)

    def test_stale_when_a_portfile_exists_but_its_writer_is_dead(self):
        """Same action as absent, distinguished for the log line: 'the
        host app went away' is not 'never present'."""
        r = client.probe(data_dir='/x',
                         portfile_reader=lambda d: None,
                         raw_portfile_reader=lambda d: {'port': 1,
                                                        'pid': 999999},
                         token_reader=lambda d: 'tok')
        self.assertEqual(r.status, client.STATUS_STALE)
        self.assertFalse(r.use_convoy)
        self.assertIn('stale', r.detail)

    def test_a_live_host_with_no_readable_token_falls_back(self):
        """Unauthenticated traffic would just 401 -- stay quiet instead
        of guaranteeing a failed call."""
        r = client.probe(data_dir='/x',
                         portfile_reader=_live_reader(),
                         token_reader=lambda d: None,
                         health_check=lambda handle: HOST_ID)
        self.assertEqual(r.status, client.STATUS_ABSENT)
        self.assertFalse(r.use_convoy)

    def test_a_port_that_does_not_answer_health_is_stale(self):
        r = client.probe(data_dir='/x',
                         portfile_reader=_live_reader(),
                         token_reader=lambda d: 'tok',
                         health_check=lambda handle: None)
        self.assertEqual(r.status, client.STATUS_STALE)
        self.assertFalse(r.use_convoy)

    def test_a_recycled_port_with_a_different_host_id_is_stale(self):
        """pid-liveness is NOT identity. A different host_id on /health
        means something else holds the port -- fall back, and above all
        do not hand it the token."""
        r = client.probe(data_dir='/x',
                         portfile_reader=_live_reader(host_id=HOST_ID),
                         token_reader=lambda d: 'tok',
                         health_check=lambda handle: OTHER_HOST_ID)
        self.assertEqual(r.status, client.STATUS_STALE)
        self.assertFalse(r.use_convoy)

    def test_identity_is_confirmed_before_the_token_is_transmitted(self):
        """The whole point of the /health step: the token must not be in
        flight until the process has identified itself."""
        seen = {}

        def check(handle):
            seen['ran'] = True
            seen['token_at_check'] = handle.token
            return HOST_ID

        r = client.probe(data_dir='/x', portfile_reader=_live_reader(),
                         token_reader=lambda d: 'tok', health_check=check)
        self.assertTrue(seen.get('ran'), 'the health check must run')
        self.assertEqual(r.status, client.STATUS_RUNNING)

    def test_the_default_health_check_sends_no_token(self):
        """GET /health is the one unauthenticated route, by design."""
        seen = {}

        def opener(req, timeout=None):
            seen['url'] = req.full_url
            seen['token'] = req.get_header(client.TOKEN_HEADER.lower()
                                           .capitalize())
            seen['headers'] = dict(req.header_items())
            return _Resp({'ok': True, 'host_id': HOST_ID})

        handle = client.HostHandle(port=8080, host_id=HOST_ID,
                                   token='super-secret', data_dir='/x')
        got = client._default_health_check(handle, opener=opener)
        self.assertEqual(got, HOST_ID)
        self.assertEqual(seen['url'], 'http://127.0.0.1:8080/health')
        blob = json.dumps(seen['headers'])
        self.assertNotIn('super-secret', blob,
                         'the IPC token must never ride on /health')

    def test_an_unreachable_health_endpoint_is_not_an_exception(self):
        handle = client.HostHandle(port=8080, host_id=HOST_ID,
                                   token='tok', data_dir='/x')

        def opener(req, timeout=None):
            raise urllib.error.URLError('connection refused')

        self.assertIsNone(client._default_health_check(handle,
                                                       opener=opener))

    def test_probe_never_raises_on_a_corrupt_portfile(self):
        r = client.probe(data_dir='/x',
                         portfile_reader=lambda d: None,
                         raw_portfile_reader=lambda d: None,
                         token_reader=lambda d: 'tok')
        self.assertEqual(r.status, client.STATUS_ABSENT)


# =====================================================================
# pid liveness -- a dead port must never be handed out as live
# =====================================================================

class TestConvoyClientPidLiveness(EmbodyTestCase):

    def test_a_dead_pid_reads_as_dead(self):
        def dead(pid, sig):
            raise ProcessLookupError()
        self.assertFalse(client.pid_is_alive(4242, platform='linux',
                                             kill=dead))

    def test_a_live_pid_reads_as_alive(self):
        self.assertTrue(client.pid_is_alive(4242, platform='linux',
                                            kill=lambda p, s: None))

    def test_a_foreign_owned_process_reads_as_alive(self):
        """EPERM proves the process EXISTS. Reading it as dead would let
        a stale portfile read as free."""
        def eperm(pid, sig):
            raise PermissionError()
        self.assertTrue(client.pid_is_alive(4242, platform='linux',
                                            kill=eperm))

    def test_a_nonsense_pid_reads_as_dead(self):
        for pid in (0, -1, None):
            self.assertFalse(client.pid_is_alive(pid, platform='linux',
                                                 kill=lambda p, s: None))

    def test_the_posix_branch_is_refused_on_windows_without_an_injected_kill(self):
        """os.kill(pid, 0) on Windows calls TerminateProcess -- it KILLS
        the pid it was asked to inspect. This project has lost a TD to
        exactly that, so the refusal is code, not a comment."""
        if sys.platform != 'win32':
            raise unittest.SkipTest('the guard only fires on a win32 host')
        with self.assertRaises(RuntimeError):
            client.pid_is_alive(4242, platform='linux', kill=None)

    def test_the_windows_branch_never_calls_kill(self):
        """Belt and braces on the same hazard: even if a caller hands in
        a kill, the win32 path must not reach it."""
        if sys.platform != 'win32':
            raise unittest.SkipTest('exercises the win32 OpenProcess path')

        def forbidden(pid, sig):
            raise AssertionError('os.kill must never run on win32')

        # Our own pid is certainly alive, and never touched by kill.
        self.assertTrue(client.pid_is_alive(os.getpid(), kill=forbidden))

    def test_a_dead_pid_portfile_never_yields_a_port(self):
        """The safety property that makes probe() trustworthy: reading
        goes through read_live_portfile, so the raw file's port is never
        handed out when its writer is gone."""
        directory = self._temp_dir()
        self._write_portfile(directory, {'port': 8080, 'pid': 999999,
                                         'host_id': HOST_ID})

        def dead(pid, sig):
            raise ProcessLookupError()

        self.assertIsNotNone(client.read_portfile(directory),
                             'the raw file is readable')
        self.assertIsNone(
            client.read_live_portfile(directory, platform='linux',
                                      kill=dead),
            'but a dead writer must yield no port')

        r = client.probe(
            data_dir=directory,
            portfile_reader=lambda d: client.read_live_portfile(
                d, platform='linux', kill=dead),
            token_reader=lambda d: 'tok')
        self.assertEqual(r.status, client.STATUS_STALE)
        self.assertIsNone(r.handle, 'no handle means no port to misuse')

    def test_a_corrupt_portfile_reads_as_absent(self):
        directory = self._temp_dir()
        with open(os.path.join(directory, client.PORT_FILE), 'w') as f:
            f.write('{ half-written')
        self.assertIsNone(client.read_portfile(directory))
        self.assertIsNone(client.read_live_portfile(directory,
                                                    platform='linux',
                                                    kill=lambda p, s: None))

    def test_a_portfile_with_a_non_numeric_pid_reads_as_absent(self):
        directory = self._temp_dir()
        self._write_portfile(directory, {'port': 8080, 'pid': 'nope'})
        self.assertIsNone(client.read_live_portfile(directory,
                                                    platform='linux',
                                                    kill=lambda p, s: None))

    def test_an_unreadable_token_is_none_not_an_exception(self):
        self.assertIsNone(client.read_token(self._temp_dir()))

    def test_a_non_utf8_token_reads_as_absent_not_an_exception(self):
        """UnicodeDecodeError IS a ValueError, and read_token used to
        catch only OSError -- so a host.token saved as UTF-16 (Notepad's
        "Unicode") raised straight out of probe(), breaking its
        documented "never raises" contract and killing the D4 worker."""
        directory = self._temp_dir()
        with open(os.path.join(directory, client.TOKEN_FILE), 'wb') as f:
            f.write(b'\xff\xfe\x00s\x00e\x00c\x00r\x00e\x00t\x00')
        self.assertIsNone(client.read_token(directory))

    def test_probe_survives_a_non_utf8_token_file(self):
        """The contract that matters: the tree still ANSWERS, and
        answers ABSENT -- a token we cannot read is a host we cannot
        authenticate to."""
        directory = self._temp_dir()
        with open(os.path.join(directory, client.TOKEN_FILE), 'wb') as f:
            f.write(b'\xff\xfe\x00secret')
        result = client.probe(data_dir=directory,
                              portfile_reader=_live_reader(),
                              health_check=lambda handle: HOST_ID)
        self.assertEqual(result.status, client.STATUS_ABSENT)
        self.assertFalse(result.use_convoy)

    def test_an_empty_token_reads_as_absent(self):
        directory = self._temp_dir()
        with open(os.path.join(directory, client.TOKEN_FILE), 'w') as f:
            f.write('   \n')
        self.assertIsNone(client.read_token(directory))

    # -- the win32 branch, driven through an injected kernel32 --------

    def test_win32_access_denied_means_the_process_EXISTS(self):
        """DEMONSTRATED REGRESSION: this branch inverted, for Windows,
        the rule its own POSIX half documents. Measured on Windows 11:
        pid 4 (System, alive) -> OpenProcess NULL, GetLastError 5, and
        pid_is_alive(4) returned False. A host app at another integrity
        level would read as gone, so probe() would return STALE forever
        while /health answered fine."""
        k = _FakeKernel32(handle=0, last_error=5)
        self.assertTrue(client.pid_is_alive(4, platform='win32',
                                            kernel32=k))

    def test_win32_invalid_parameter_means_gone(self):
        """An unused pid, measured on Windows 11 -> GetLastError 87."""
        k = _FakeKernel32(handle=0, last_error=87)
        self.assertFalse(client.pid_is_alive(999999, platform='win32',
                                             kernel32=k))

    def test_win32_a_signaled_process_object_means_exited(self):
        k = _FakeKernel32(handle=1234, wait=0)
        self.assertFalse(client.pid_is_alive(4242, platform='win32',
                                             kernel32=k))
        self.assertEqual(k.closed, [1234], 'the handle must be closed')

    def test_win32_an_unsignaled_process_object_means_running(self):
        k = _FakeKernel32(handle=1234, wait=0x102)      # WAIT_TIMEOUT
        self.assertTrue(client.pid_is_alive(4242, platform='win32',
                                            kernel32=k))
        self.assertEqual(k.closed, [1234])

    def test_win32_asks_for_synchronize_only(self):
        """SYNCHRONIZE is what makes WaitForSingleObject meaningful;
        opening with a weaker right would make the wait fail and
        reintroduce 'openable after death reads alive forever'."""
        k = _FakeKernel32(handle=1234, wait=0x102)
        client.pid_is_alive(4242, platform='win32', kernel32=k)
        self.assertEqual(k.opened, [(0x00100000, 4242)])

    def test_a_broken_kernel32_reads_as_dead_not_an_exception(self):
        class Exploding:
            def OpenProcess(self, *a):
                raise OSError('ctypes went wrong')
        self.assertFalse(client.pid_is_alive(4242, platform='win32',
                                             kernel32=Exploding()))

    def test_the_real_win32_branch_sees_a_protected_but_live_process(self):
        """The un-injected path on the box where it matters: pid 4
        (System) is alive and unopenable -- the exact case that read as
        dead before the fix."""
        if sys.platform != 'win32':
            raise unittest.SkipTest('exercises the real OpenProcess')
        self.assertTrue(client.pid_is_alive(4))

    # -- helpers ------------------------------------------------------

    def _temp_dir(self):
        import tempfile
        directory = tempfile.mkdtemp(prefix='convoy_client_test_')
        self._temp_dirs.append(directory)
        return directory

    def _write_portfile(self, directory, payload):
        with open(os.path.join(directory, client.PORT_FILE), 'w') as f:
            json.dump(payload, f)

    def setUp(self):
        super().setUp()
        self._temp_dirs = []

    def tearDown(self):
        import shutil
        for directory in getattr(self, '_temp_dirs', []):
            shutil.rmtree(directory, ignore_errors=True)
        super().tearDown()


# =====================================================================
# The data dir -- foreign platforms, foreign separators
# =====================================================================

class TestConvoyClientDataDir(EmbodyTestCase):
    """Must agree with convoy_platform.data_dir on every platform, and
    must join with the TARGET platform's separator (a seam that hands
    back host-flavoured separators is not exercising the foreign path)."""

    def test_win32_uses_localappdata_not_roaming(self):
        got = client.data_dir(platform='win32',
                              env={'LOCALAPPDATA': 'C:\\Users\\x\\AppData\\Local'},
                              home='C:\\Users\\x')
        self.assertEqual(got, 'C:\\Users\\x\\AppData\\Local\\EmbodyConvoy')
        self.assertNotIn('Roaming', got)

    def test_win32_falls_back_to_the_home_profile(self):
        got = client.data_dir(platform='win32', env={},
                              home='C:\\Users\\x')
        self.assertEqual(got,
                         'C:\\Users\\x\\AppData\\Local\\EmbodyConvoy')

    def test_darwin_uses_application_support_with_posix_separators(self):
        got = client.data_dir(platform='darwin', env={}, home='/Users/x')
        self.assertEqual(
            got, '/Users/x/Library/Application Support/EmbodyConvoy')
        self.assertNotIn('\\', got)

    def test_linux_prefers_xdg_state_home(self):
        got = client.data_dir(platform='linux',
                              env={'XDG_STATE_HOME': '/home/x/.local/state'},
                              home='/home/x')
        self.assertEqual(got, '/home/x/.local/state/embody-convoy')

    def test_linux_falls_back_to_local_state(self):
        got = client.data_dir(platform='linux', env={}, home='/home/x')
        self.assertEqual(got, '/home/x/.local/state/embody-convoy')


# =====================================================================
# The /register payload
# =====================================================================

class TestConvoyClientPayload(EmbodyTestCase):

    def test_the_payload_carries_every_field_the_host_keys_on(self):
        payload = client.registration_payload(
            project_root='C:/Work/Show', comp_path='/Embody',
            convoy_id='cv_abc', runtime_id='rt_1234', envoy_port=9981)
        self.assertEqual(payload, {
            'project_root': 'C:/Work/Show',
            'comp_path': '/Embody',
            'convoy_id': 'cv_abc',
            'runtime_id': 'rt_1234',
            'envoy_port': 9981,
        })

    def test_payload_carries_explicit_realm_binding_state(self):
        payload = client.registration_payload(
            '/r', '/Embody', 'cv', 'rt_1234',
            binding_state='candidate')
        self.assertEqual(payload['binding_state'], 'candidate')
        with self.assertRaises(ValueError):
            client.registration_payload(
                '/r', '/Embody', 'cv', 'rt_1234',
                binding_state='conflict')

    def test_payload_carries_exact_executable_and_paired_launch_proof(self):
        token = 'T' * 43
        reservation = 'lr_' + 'R' * 32
        payload = client.registration_payload(
            '/r', '/Embody', 'cv', 'rt_1234',
            td_executable='C:/Program Files/Derivative/TouchDesigner.exe',
            launch_token=token, launch_reservation_id=reservation)
        self.assertEqual(
            payload['td_executable'],
            'C:/Program Files/Derivative/TouchDesigner.exe')
        self.assertEqual(payload['launch_token'], token)
        self.assertEqual(payload['launch_reservation_id'], reservation)
        self.assertEqual(json.loads(json.dumps(payload)), payload)

    def test_process_executable_is_the_real_process_image(self):
        """Inside TD, sys.executable names the BUNDLED bin/python.exe --
        registering with it can never match the host inspector's probe,
        so every launch profile died runtime_unverifiable and the whole
        fleet read remotely_launchable:false (field 2026-08-19).
        process_executable must name the actual process image."""
        import os as _os
        import sys as _sys
        path = client.process_executable()
        self.assertTrue(path, 'a probe result (or fallback) is required')
        self.assertTrue(_os.path.isabs(path))
        self.assertTrue(_os.path.isfile(path))
        base = _os.path.basename(path).lower()
        # This suite runs on BOTH legs. Inside TouchDesigner the image is
        # TD itself, never the bundled python that sys.executable names;
        # under plain pytest the process genuinely IS python and the probe
        # must agree with that instead (CI red 2026-08-20: the first cut
        # asserted 'touchdesigner' unconditionally).
        if 'td' in _sys.modules and _sys.platform in ('win32', 'darwin'):
            self.assertIn('touchdesigner', base)
            self.assertNotIn('python', base)
        elif _sys.platform in ('win32', 'darwin'):
            self.assertIn('python', base)
        self.assertIs(client.process_executable(), path,
                      'cached per process -- the image cannot change')

    def test_launch_proof_and_executable_are_strictly_bounded(self):
        base = ('/r', '/Embody', 'cv', 'rt_1234')
        bad = (
            {'td_executable': ''},
            {'td_executable': 'C:/Touch\nDesigner.exe'},
            {'launch_token': 'T' * 43},
            {'launch_reservation_id': 'lr_' + 'R' * 32},
            {'launch_token': 'short',
             'launch_reservation_id': 'lr_' + 'R' * 32},
            {'launch_token': 'T' * 42 + '!',
             'launch_reservation_id': 'lr_' + 'R' * 32},
            {'launch_token': 'T' * 43,
             'launch_reservation_id': 'bad\nreservation'},
        )
        for kwargs in bad:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                client.registration_payload(*base, **kwargs)

    def test_runtime_id_is_always_present(self):
        """The host does `runtime_id or mint_runtime_id()`: omitting it
        re-mints the run identity on every heartbeat and invalidates
        every in-flight expected_runtime_id precondition."""
        payload = client.registration_payload('/r', '/Embody', 'cv',
                                              'rt_1234')
        self.assertEqual(payload['runtime_id'], 'rt_1234')

    def test_a_missing_port_is_omitted_not_sent_as_null(self):
        """A pre-Envoy heartbeat must not be able to wipe a known port.
        The host never clears on a re-register that OMITS the field --
        so omit it."""
        payload = client.registration_payload('/r', '/Embody', 'cv',
                                              'rt_1234', envoy_port=None)
        self.assertNotIn('envoy_port', payload)

    def test_port_zero_is_omitted_too(self):
        """DEMONSTRATED: `is not None` sent envoy_port=0, which the host
        refuses with a 400 malformed (its range is 1..65535) -- turning
        a transient pre-Envoy tick into 'Refused: malformed' instead of
        the pending path. Port 0 is not a port."""
        payload = client.registration_payload('/r', '/Embody', 'cv',
                                              'rt_1234', envoy_port=0)
        self.assertNotIn('envoy_port', payload)

    def test_identity_fields_are_coerced_to_str(self):
        """They arrive from the main thread as whatever TD handed over.
        A pathlib.Path project root is not JSON-serializable, and would
        otherwise become a request that never leaves the process."""
        import pathlib
        payload = client.registration_payload(
            pathlib.PurePosixPath('/Work/Show'), '/Embody', 'cv', 'rt_1')
        self.assertEqual(payload['project_root'], '/Work/Show')
        for key in ('project_root', 'comp_path', 'convoy_id', 'runtime_id'):
            self.assertIsInstance(payload[key], str, key)
        json.dumps(payload)     # must not raise

    def test_the_port_is_coerced_to_an_int(self):
        """The host refuses a non-int envoy_port with a named 400."""
        payload = client.registration_payload('/r', '/Embody', 'cv',
                                              'rt_1234', envoy_port='9981')
        self.assertEqual(payload['envoy_port'], 9981)
        self.assertIsInstance(payload['envoy_port'], int)

    def test_the_payload_is_json_serializable(self):
        payload = client.registration_payload('/r', '/Embody', 'cv',
                                              'rt_1234', envoy_port=9981)
        self.assertEqual(json.loads(json.dumps(payload)), payload)

    def test_two_toe_files_get_distinct_stable_discriminators(self):
        a = client.stable_node_discriminator(
            'C:/Work/Show/main.toe', platform='win32')
        b = client.stable_node_discriminator(
            'C:/Work/Show/backup.toe', platform='win32')
        self.assertNotEqual(a, b)
        self.assertTrue(re.match(r'^nd_[0-9a-f]{32}$', a), a)
        self.assertEqual(
            a,
            client.stable_node_discriminator(
                'c:\\work\\show\\main.toe', platform='win32'),
            'Windows case/separator variants are the same saved .toe')

    def test_posix_toe_discriminator_is_case_sensitive(self):
        upper = client.stable_node_discriminator(
            '/Work/Show/main.toe', platform='darwin')
        lower = client.stable_node_discriminator(
            '/work/show/main.toe', platform='darwin')
        self.assertNotEqual(upper, lower)

    def test_relative_or_missing_toe_path_is_refused(self):
        for bad in (None, '', 'show/main.toe'):
            with self.assertRaises(ValueError):
                client.stable_node_discriminator(bad, platform='linux')

    def test_payload_carries_discriminator_and_bounded_metadata(self):
        discriminator = client.stable_node_discriminator(
            '/Work/Show/main.toe', platform='darwin')
        metadata = {
            'toe_path': '/Work/Show/main.toe',
            'toe_name': 'main.toe',
            'node_name': 'studio-mac / main',
            'hostname': 'studio-mac',
            'process_id': 1234,
            'embody_version': '6.0.178',
            'touchdesigner_version': '2025.32460',
        }
        payload = client.registration_payload(
            '/Work/Show', '/Embody', 'cv', 'rt_1234',
            node_discriminator=discriminator, metadata=metadata)
        self.assertEqual(payload['node_discriminator'], discriminator)
        self.assertEqual(payload['metadata'], metadata)
        self.assertEqual(json.loads(json.dumps(payload)), payload)

    def test_unknown_metadata_is_refused_not_forwarded(self):
        with self.assertRaises(ValueError):
            client.registration_payload(
                '/Work/Show', '/Embody', 'cv', 'rt_1234',
                node_discriminator='nd_' + '1' * 32,
                metadata={'secret_td_object': 'no'})

    def test_malformed_discriminator_is_refused(self):
        for bad in ('', 'legacy', 'nd_' + 'A' * 32, 'nd_short'):
            with self.assertRaises(ValueError):
                client.registration_payload(
                    '/Work/Show', '/Embody', 'cv', 'rt_1234',
                    node_discriminator=bad)

    def test_runtime_wake_endpoint_is_explicit_and_json_safe(self):
        token = 'A' * 43
        payload = client.registration_payload(
            '/Work/Show', '/Embody', 'cv', 'rt_1234',
            envoy_port=None, envoy_ready=False,
            wake_port=47631, wake_token=token, remote_wake=True,
            perform_mode=True, wake_active=False, wake_grace_s=60)
        self.assertEqual(payload['wake_port'], 47631)
        self.assertEqual(payload['wake_token'], token)
        self.assertFalse(payload['envoy_ready'])
        self.assertTrue(payload['remote_wake'])
        self.assertTrue(payload['perform_mode'])
        self.assertFalse(payload['wake_active'])
        self.assertEqual(payload['wake_grace_s'], 60)
        self.assertEqual(json.loads(json.dumps(payload)), payload)

    def test_wake_intent_may_be_pending_while_listener_binds(self):
        payload = client.registration_payload(
            '/r', '/Embody', 'cv', 'rt_1', remote_wake=True,
            perform_mode=True, wake_active=False, wake_grace_s=60)
        self.assertTrue(payload['wake_pending'])
        self.assertNotIn('wake_port', payload)
        self.assertNotIn('wake_token', payload)

    def test_malformed_wake_registration_is_refused_locally(self):
        base = ('/r', '/Embody', 'cv', 'rt_1')
        bad_kwargs = (
            {'wake_port': 47631},
            {'wake_token': 'A' * 43},
            {'wake_port': 0, 'wake_token': 'A' * 43},
            {'wake_port': True, 'wake_token': 'A' * 43},
            {'wake_port': 47631, 'wake_token': 'short'},
            {'wake_port': 47631, 'wake_token': 'A' * 42 + '!'},
            {'envoy_ready': True},
            {'remote_wake': 1},
            {'perform_mode': 'yes'},
            {'wake_active': 0},
            {'wake_grace_s': -1},
            {'wake_grace_s': 3601},
        )
        for kwargs in bad_kwargs:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                client.registration_payload(*base, **kwargs)


# =====================================================================
# runtime_id: fresh per launch, stable across reinit
# =====================================================================

class TestConvoyClientRuntimeId(EmbodyTestCase):

    def test_minted_ids_match_the_host_side_format(self):
        value = client.mint_runtime_id()
        self.assertStartsWith(value, 'rt_')
        self.assertTrue(re.match(r'^rt_[0-9a-f]{16}$', value), value)

    def test_every_mint_is_unique(self):
        self.assertLen({client.mint_runtime_id() for _ in range(64)}, 64)

    def test_the_same_store_and_key_mint_once(self):
        """A reinit (Ctrl+S) is NOT a new run: the id must survive it,
        which is why the store outlives the extension instance."""
        store = {}
        first = client.ensure_runtime_id(store, '/embody/Embody')
        for _ in range(5):
            self.assertEqual(client.ensure_runtime_id(store,
                                                      '/embody/Embody'),
                             first)

    def test_a_fresh_store_mints_a_fresh_id(self):
        """A process restart really IS a new run -- which is why the
        store must not be persisted into the .toe."""
        first = client.ensure_runtime_id({}, '/embody/Embody')
        second = client.ensure_runtime_id({}, '/embody/Embody')
        self.assertNotEqual(first, second)

    def test_two_comps_get_two_runtime_ids(self):
        store = {}
        a = client.ensure_runtime_id(store, '/embody/Embody')
        b = client.ensure_runtime_id(store, '/scenes/Embody')
        self.assertNotEqual(a, b)
        self.assertLen(store, 2)

    def test_a_junk_stored_value_is_re_minted(self):
        for junk in (None, '', 0, [], {}):
            store = {'/embody/Embody': junk}
            got = client.ensure_runtime_id(store, '/embody/Embody')
            self.assertStartsWith(got, 'rt_')
            self.assertEqual(store['/embody/Embody'], got)

    def test_the_format_matches_convoy_identity(self):
        identity = _load_convoy_module('convoy_identity')
        if identity is None:
            raise unittest.SkipTest('dev/convoy is not in this checkout')
        theirs = identity.mint_runtime_id()
        ours = client.mint_runtime_id()
        self.assertEqual(len(ours), len(theirs))
        self.assertStartsWith(ours, 'rt_')


# =====================================================================
# Backoff: 5 s -> 60 s, jittered, never outside its bounds
# =====================================================================

class TestConvoyClientBackoff(EmbodyTestCase):

    def test_the_first_retry_is_around_the_base(self):
        low = client.BACKOFF_BASE_S * (1 - client.BACKOFF_JITTER)
        high = client.BACKOFF_BASE_S * (1 + client.BACKOFF_JITTER)
        for _ in range(50):
            delay = client.backoff_delay(0)
            self.assertGreaterEqual(delay, low)
            self.assertLessEqual(delay, high)

    def test_the_schedule_never_exceeds_the_cap(self):
        for attempt in range(0, 40):
            for _ in range(5):
                self.assertLessEqual(client.backoff_delay(attempt),
                                     client.BACKOFF_CAP_S)

    def test_the_schedule_never_busy_loops(self):
        floor = client.BACKOFF_BASE_S * (1 - client.BACKOFF_JITTER)
        for attempt in range(0, 40):
            self.assertGreaterEqual(client.backoff_delay(attempt), floor)

    def test_the_schedule_doubles_then_saturates(self):
        """Deterministic through the injected rng (0.5 = the midpoint of
        the jitter window), so the SHAPE itself is asserted: 5, 10, 20,
        40, then flat."""
        mid = lambda: 0.5
        delays = [client.backoff_delay(a, rng=mid) for a in range(0, 8)]
        self.assertEqual(delays[:4], [5.0, 10.0, 20.0, 40.0])
        # From here the un-jittered step is already at or past the cap,
        # so every further attempt lands on the same window.
        self.assertEqual(delays[4], delays[5])
        self.assertEqual(delays[5], delays[7])

    def test_at_the_cap_the_jitter_only_spreads_downward(self):
        """The cap is a hard ceiling, so a saturated retry jitters into
        [45, 60] rather than around 60. Nodes still de-synchronise; none
        waits longer than the cap promises."""
        self.assertEqual(client.backoff_delay(10, rng=lambda: 1.0),
                         client.BACKOFF_CAP_S)
        self.assertEqual(
            client.backoff_delay(10, rng=lambda: 0.0),
            client.BACKOFF_CAP_S * (1 - client.BACKOFF_JITTER))

    def test_the_jitter_bounds_are_reachable(self):
        self.assertAlmostEqual(client.backoff_delay(0, rng=lambda: 0.0),
                               3.75)
        self.assertAlmostEqual(client.backoff_delay(0, rng=lambda: 1.0),
                               6.25)

    def test_the_jitter_actually_varies(self):
        """A fleet recovering from ONE host restart must not retry in
        lockstep -- a constant delay would defeat the whole point."""
        samples = {client.backoff_delay(3) for _ in range(50)}
        self.assertGreater(len(samples), 1)

    def test_a_nonsense_attempt_does_not_raise(self):
        for attempt in (-5, None, 'seven', 10 ** 6):
            delay = client.backoff_delay(attempt)
            self.assertGreater(delay, 0)
            self.assertLessEqual(delay, client.BACKOFF_CAP_S)


# =====================================================================
# status_text: total, and absence is never an error
# =====================================================================

_VOCABULARY = (
    'Disabled',
    'Waiting for project save',
    'No Convoy host app',
    'Host app stale',
    'Host app found',
    'Registering...',
    'Registered -- Envoy port pending',
    'Connected',
)
_VOCABULARY_PATTERNS = (
    re.compile(r'^Refused: .+$'),
    re.compile(r'^Error: .+$'),
)

_ALL_STATES = ('disabled', 'unsaved', 'absent', 'stale', 'registering',
               'registered', 'unregistered', 'refused', 'unreachable',
               'host_error', 'error')

# probe() answers in a SECOND vocabulary, and Stage B's tick reports
# whichever it last computed -- so status_text has to be total over both.
_ALL_PROBE_STATUSES = ('running', 'absent', 'stale')

# Absence is a normal, supported state of the machine. If any of these
# ever reads as an error the user is being told a lie, and will learn to
# ignore the field.
_ABSENCE_STATES = ('absent', 'stale', 'unreachable', 'disabled')


class TestConvoyClientStatusText(EmbodyTestCase):

    def test_every_constant_of_BOTH_vocabularies_is_covered(self):
        """Total mapping over STATE_* AND STATUS_*.

        The two vocabularies are same-shaped and silently overlap
        ('absent' and 'stale' are literally equal across them), so a
        STATUS_* value that had no branch half-matched by coincidence
        and only STATUS_RUNNING fell through -- reporting the healthiest
        state in the system as "Error: unexpected state 'running'".
        Enumerating both prefixes from dir() is what stops a future
        constant doing the same.
        """
        declared_states = {getattr(client, name) for name in dir(client)
                           if name.startswith('STATE_')}
        declared_statuses = {getattr(client, name) for name in dir(client)
                             if name.startswith('STATUS_')}
        self.assertEqual(declared_states, set(_ALL_STATES))
        self.assertEqual(declared_statuses, set(_ALL_PROBE_STATUSES))

        for state in sorted(declared_states | declared_statuses):
            text = client.status_text({'state': state, 'reason': 'r',
                                       'detail': 'd', 'node_id': 'n' * 32,
                                       'host_id': 'h' * 32,
                                       'envoy_port': 9981})
            self.assertTrue(text, 'state %r produced no text' % (state,))
            self.assertIsInstance(text, str)
            self.assertNotIn(
                'unexpected state', text,
                'state %r fell through the mapping' % (state,))

    def test_a_running_probe_outcome_is_not_an_error(self):
        """DEMONSTRATED REGRESSION: status_text({'state': STATUS_RUNNING})
        returned "Error: unexpected state 'running'" -- Stage B's tick
        probes before it registers, so the healthiest possible outcome
        wrote an Error status."""
        text = client.status_text({'state': client.STATUS_RUNNING})
        self.assertEqual(text, 'Host app found')
        self.assertNotIn('Error', text)

    def test_a_real_probe_result_maps_without_an_error(self):
        """The mixing pattern Stage B will actually use: feed probe()'s
        status straight to status_text, for all three outcomes."""
        cases = (
            dict(data_dir='/x', portfile_reader=_live_reader(),
                 token_reader=lambda d: 'tok',
                 health_check=lambda handle: HOST_ID),
            dict(data_dir='/x', portfile_reader=lambda d: None,
                 raw_portfile_reader=lambda d: None,
                 token_reader=lambda d: 'tok'),
            dict(data_dir='/x', portfile_reader=lambda d: None,
                 raw_portfile_reader=lambda d: {'port': 1, 'pid': 9},
                 token_reader=lambda d: 'tok'),
        )
        for kwargs in cases:
            result = client.probe(**kwargs)
            text = client.status_text({'state': result.status})
            self.assertNotIn('Error', text, result.status)
            self.assertIn(text, _VOCABULARY, result.status)

    def test_every_produced_string_is_in_the_agreed_vocabulary(self):
        for state in _ALL_STATES:
            for port in (9981, None):
                text = client.status_text({
                    'state': state, 'reason': 'node_identity_conflict',
                    'detail': 'something', 'node_id': 'n' * 32,
                    'host_id': 'h' * 32, 'envoy_port': port})
                ok = text in _VOCABULARY or any(
                    p.match(text) for p in _VOCABULARY_PATTERNS)
                self.assertTrue(ok, 'off-vocabulary status %r' % (text,))

    def test_absence_is_never_an_error(self):
        for state in _ABSENCE_STATES:
            text = client.status_text({'state': state})
            self.assertNotIn('Error', text)
            self.assertNotIn('error', text)
            self.assertNotIn('fail', text.lower())

    def test_absent_and_unreachable_read_the_same_to_a_user(self):
        """A host app that was never there and one that vanished
        mid-call are the same fact: there is nothing to talk to."""
        self.assertEqual(client.status_text({'state': 'absent'}),
                         'No Convoy host app')
        self.assertEqual(client.status_text({'state': 'unreachable'}),
                         'No Convoy host app')

    def test_stale_is_distinguished_from_absent(self):
        self.assertEqual(client.status_text({'state': 'stale'}),
                         'Host app stale')

    def test_registered_shows_short_ids(self):
        text = client.status_text({'state': 'registered',
                                   'node_id': 'abcdef0123456789' * 2,
                                   'host_id': 'fedcba9876543210' * 2,
                                   'envoy_port': 9981})
        self.assertEqual(text, 'Connected')

    def test_registered_without_a_port_is_named_pending_not_broken(self):
        text = client.status_text({'state': 'registered',
                                   'node_id': 'n' * 32,
                                   'host_id': 'h' * 32,
                                   'envoy_port': None})
        self.assertEqual(text, 'Registered -- Envoy port pending')
        self.assertNotIn('Error', text)

    def test_a_refusal_names_its_reason(self):
        self.assertEqual(
            client.status_text({'state': 'refused',
                                'reason': 'node_identity_conflict'}),
            'Refused: node_identity_conflict')

    def test_a_refusal_without_a_reason_still_reads(self):
        self.assertEqual(client.status_text({'state': 'refused'}),
                         'Refused: unknown')

    def test_an_error_is_bounded_in_length(self):
        text = client.status_text({'state': 'error', 'detail': 'x' * 500})
        self.assertLessEqual(len(text), 90)
        self.assertStartsWith(text, 'Error: ')

    def test_garbage_input_never_raises(self):
        for bad in (None, '', 0, [], {}, {'state': 'nonsense'}):
            text = client.status_text(bad)
            self.assertIsInstance(text, str)
            self.assertTrue(text)


# =====================================================================
# register / unregister result mapping
# =====================================================================

def _handle(token='tok'):
    return client.HostHandle(port=8080, host_id=HOST_ID, token=token,
                             data_dir='/x')


class TestConvoyClientRegister(EmbodyTestCase):

    def test_a_successful_register_reports_the_node(self):
        answer = {'ok': True, 'node_id': 'n' * 32, 'host_id': HOST_ID,
                  'runtime_id': 'rt_1234', 'envoy_port': 9981,
                  'td_python_approved': False}
        result = client.register(
                                 _handle(),
                                 {'project_root': '/r',
                                  'convoy_id': 'cv_test',
                                  'binding_state': 'candidate'},
                                 opener=lambda req, timeout=None:
                                 _Resp(answer))
        self.assertEqual(result['state'], client.STATE_REGISTERED)
        self.assertEqual(result['node_id'], 'n' * 32)
        self.assertEqual(result['envoy_port'], 9981)
        self.assertEqual(result['convoy_id'], 'cv_test')
        self.assertEqual(result['realm_state'], 'candidate')
        self.assertIs(result['td_python_approved'], False)
        self.assertEqual(client.status_text(result),
                         'Connected')

    def test_register_sends_the_token_and_the_body(self):
        seen = {}

        def opener(req, timeout=None):
            seen['url'] = req.full_url
            seen['method'] = req.get_method()
            seen['data'] = req.data
            seen['headers'] = {k.lower(): v for k, v in req.header_items()}
            return _Resp({'ok': True})

        client.register(_handle(), {'project_root': '/r'}, opener=opener)
        self.assertEqual(seen['url'], 'http://127.0.0.1:8080/register')
        self.assertEqual(seen['method'], 'POST')
        self.assertEqual(json.loads(seen['data']), {'project_root': '/r'})
        self.assertEqual(seen['headers'][client.TOKEN_HEADER.lower()],
                         'tok')

    def test_a_structured_refusal_is_surfaced_not_swallowed(self):
        opener = _refusing_opener(409, {'ok': False,
                                        'reason': 'node_identity_conflict',
                                        'detail': 'already registered'})
        result = client.register(_handle(), {}, opener=opener)
        self.assertEqual(result['state'], client.STATE_REFUSED)
        self.assertEqual(result['reason'], 'node_identity_conflict')
        self.assertEqual(client.status_text(result),
                         'Refused: node_identity_conflict')

    def test_a_transport_failure_is_absence_not_an_error(self):
        """The host app going away mid-call is the same user-visible
        fact as it never having been there."""
        def opener(req, timeout=None):
            raise urllib.error.URLError('connection refused')

        result = client.register(_handle(), {}, opener=opener)
        self.assertEqual(result['state'], client.STATE_UNREACHABLE)
        self.assertEqual(client.status_text(result), 'No Convoy host app')

    def test_an_os_error_is_also_absence(self):
        def opener(req, timeout=None):
            raise OSError('socket exploded')
        result = client.register(_handle(), {}, opener=opener)
        self.assertEqual(result['state'], client.STATE_UNREACHABLE)

    def test_a_non_object_answer_is_an_error_not_a_crash(self):
        result = client.register(_handle(), {},
                                 opener=lambda req, timeout=None:
                                 _Resp(['not', 'an', 'object']))
        self.assertEqual(result['state'], client.STATE_ERROR)
        self.assertStartsWith(client.status_text(result), 'Error: ')

    def test_an_oversized_post_answer_is_bounded_before_json_decode(self):
        class HugeResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, amount=None):
                return b'x' * (client.MAX_HOST_RESPONSE_BYTES + 1)

        result = client.register(
            _handle(), {}, opener=lambda req, timeout=None: HugeResponse())
        self.assertEqual(result['state'], client.STATE_HOST_ERROR)
        self.assertEqual(result['reason'], 'host_response_too_large')

    def test_an_unparseable_refusal_body_still_names_the_http_code(self):
        def opener(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 500, 'boom', {},
                                         _FakeBody('<html>nope'))
        result = client.register(_handle(), {}, opener=opener)
        self.assertEqual(result['state'], client.STATE_HOST_ERROR)
        self.assertEqual(result['reason'], 'host_http_error')
        self.assertEqual(result['http_status'], 500)

    # -- 5xx is the host FAILING, not the host refusing ---------------

    def test_a_transient_5xx_is_an_error_not_a_policy_refusal(self):
        """persist_failed rolls the registration back host-side and MUST
        be retried; node_identity_conflict must NOT be. Collapsing both
        to 'Refused: ...' leaves a host with a transient disk problem
        permanently unregistered behind a permanent-sounding status."""
        opener = _refusing_opener(500, {'ok': False,
                                        'reason': 'persist_failed',
                                        'detail': 'OSError: disk full'})
        result = client.register(_handle(), {}, opener=opener)
        self.assertEqual(result['state'], client.STATE_HOST_ERROR)
        self.assertEqual(result['http_status'], 500)
        self.assertEqual(client.status_text(result), 'Error: persist_failed')

    def test_a_host_side_crash_reads_as_an_error(self):
        """do_POST's catch-all answers 500 internal_error with a
        parseable body; 'Refused: internal_error' would read as a
        decision the host made about us."""
        opener = _refusing_opener(500, {'ok': False,
                                        'reason': 'internal_error',
                                        'detail': 'RuntimeError'})
        result = client.register(_handle(), {}, opener=opener)
        self.assertEqual(result['state'], client.STATE_HOST_ERROR)
        self.assertStartsWith(client.status_text(result), 'Error: ')

    def test_a_4xx_stays_a_refusal(self):
        opener = _refusing_opener(409, {'ok': False,
                                        'reason': 'node_identity_conflict'})
        result = client.register(_handle(), {}, opener=opener)
        self.assertEqual(result['state'], client.STATE_REFUSED)
        self.assertEqual(result['http_status'], 409)

    def test_the_two_are_distinguishable_from_the_result_alone(self):
        """The point of carrying the code: a caller must be able to tell
        retry-me from obey-me without parsing prose."""
        transient = client.register(
            _handle(), {},
            opener=_refusing_opener(500, {'ok': False,
                                          'reason': 'persist_failed'}))
        permanent = client.register(
            _handle(), {},
            opener=_refusing_opener(409, {'ok': False,
                                          'reason': 'node_identity_conflict'}))
        self.assertNotEqual(transient['state'], permanent['state'])

    # -- a live host answering garbage is not an absent host ----------

    def test_a_200_with_an_unparseable_body_is_not_absence(self):
        """DEMONSTRATED: a 200 carrying '<html>not json' used to yield
        'No Convoy host app'. A host that is demonstrably up, reachable
        and answering must not read as absent, nor get the transport
        backoff instead of a diagnosable error."""
        class _RawResp:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b'<html>not json'

        result = client.register(_handle(), {},
                                 opener=lambda req, timeout=None: _RawResp())
        self.assertNotEqual(result['state'], client.STATE_UNREACHABLE)
        self.assertEqual(result['state'], client.STATE_HOST_ERROR)
        self.assertEqual(result['reason'], 'host_bad_response')
        self.assertNotEqual(client.status_text(result),
                            'No Convoy host app')

    # -- an unserializable payload must not kill the worker -----------

    def test_an_unserializable_payload_is_a_result_not_a_raise(self):
        """register() promises it never raises: the only caller is a
        worker thread whose death would be silent. json.dumps used to sit
        OUTSIDE the try, so any non-JSON value propagated a TypeError."""
        class Weird:
            pass

        result = client.register(_handle(), {'project_root': Weird()},
                                 opener=lambda req, timeout=None:
                                 _Resp({'ok': True}))
        self.assertEqual(result['state'], client.STATE_HOST_ERROR)
        self.assertEqual(result['reason'], 'unserializable_request')
        self.assertStartsWith(client.status_text(result), 'Error: ')

    def test_a_weird_payload_never_reaches_the_network(self):
        class Weird:
            pass

        def opener(req, timeout=None):
            raise AssertionError('must not attempt a request')

        client.register(_handle(), {'x': Weird()}, opener=opener)


class TestConvoyClientNetworkNodes(EmbodyTestCase):

    @staticmethod
    def _row(index=1, convoy_id='cv_studio'):
        return {
            'node_id': ('%032x' % index),
            'host_id': ('%032x' % (index + 100)),
            'convoy_id': convoy_id,
            'runtime_id': 'rt_%016x' % index,
            'node_name': 'render-%s / show' % index,
            'hostname': 'render-%s' % index,
            'toe_name': 'show.toe',
            'embody_version': '6.0.178',
            'touchdesigner_version': '2025.32180',
            'ip': '192.168.10.%s' % index,
            'status': 'online',
            'online': True,
            'enabled': True,
            'controller_count': 2,
            'last_seen_age_s': 3.25,
            # Host-private fields must never survive the TD-side projection.
            'project_root': 'C:/secret/project',
            'td_python_approved': True,
        }

    def test_fetch_is_authenticated_get_to_literal_loopback(self):
        seen = {}

        def opener(req, timeout=None):
            seen['url'] = req.full_url
            seen['method'] = req.get_method()
            seen['headers'] = {k.lower(): v for k, v in req.header_items()}
            seen['timeout'] = timeout
            return _Resp({'ok': True, 'nodes': [self._row()]})

        result = client.network_nodes(
            _handle(), 'cv_studio', opener=opener)
        self.assertEqual(result['state'], client.NETWORK_NODES_RESULT)
        self.assertStartsWith(
            seen['url'],
            'http://127.0.0.1:8080/network/nodes?')
        self.assertIn('convoy_id=cv_studio', seen['url'])
        self.assertEqual(seen['method'], 'GET')
        self.assertEqual(seen['headers'][client.TOKEN_HEADER.lower()], 'tok')
        self.assertEqual(seen['timeout'], client.NETWORK_TIMEOUT_S)


    def test_projection_is_bounded_and_drops_private_fields(self):
        row = self._row()
        row['node_name'] = 'n' * 2000
        result = client.network_nodes(
            _handle(), 'cv_studio',
            opener=lambda req, timeout=None: _Resp(
                {'ok': True, 'nodes': [row]}))
        self.assertEqual(len(result['nodes']), 1)
        projected = result['nodes'][0]
        self.assertLessEqual(len(projected['node_name']), 512)
        self.assertNotIn('project_root', projected)
        self.assertNotIn('td_python_approved', projected)
        self.assertEqual(projected['controller_count'], 2)
        self.assertEqual(projected['last_seen_age_s'], 3.25)

    def test_optional_status_metrics_fail_closed_without_an_extra_request(self):
        row = self._row()
        row['controller_count'] = True
        row['last_seen_age_s'] = float('nan')
        result = client.network_nodes(
            _handle(), 'cv_studio',
            opener=lambda req, timeout=None: _Resp(
                {'ok': True, 'nodes': [row]}))
        projected = result['nodes'][0]
        self.assertIsNone(projected['controller_count'])
        self.assertIsNone(projected['last_seen_age_s'])

    def test_other_namespace_and_identityless_rows_are_dropped(self):
        wrong = self._row(2, convoy_id='cv_other')
        identityless = self._row(3)
        identityless['node_id'] = ''
        result = client.network_nodes(
            _handle(), 'cv_studio',
            opener=lambda req, timeout=None: _Resp(
                {'ok': True,
                 'nodes': [self._row(1), wrong, identityless]}))
        self.assertEqual([r['node_name'] for r in result['nodes']],
                         ['render-1 / show'])

    def test_directory_count_is_bounded_for_the_td_sequence(self):
        rows = [self._row(i + 1) for i in range(
            client.MAX_STATUS_NODES + 20)]
        result = client.network_nodes(
            _handle(), 'cv_studio',
            opener=lambda req, timeout=None: _Resp(
                {'ok': True, 'nodes': rows}))
        self.assertLen(result['nodes'], client.MAX_STATUS_NODES)
        self.assertIs(result['truncated'], True)

    def test_oversized_response_is_a_named_host_error(self):
        class HugeResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, amount=None):
                return b'x' * (client.MAX_HOST_RESPONSE_BYTES + 1)

        result = client.network_nodes(
            _handle(), 'cv_studio',
            opener=lambda req, timeout=None: HugeResponse())
        self.assertEqual(result['state'], client.STATE_HOST_ERROR)
        self.assertEqual(result['reason'], 'host_response_too_large')

    def test_transport_loss_is_not_a_policy_refusal(self):
        def gone(req, timeout=None):
            raise urllib.error.URLError('gone')

        result = client.network_nodes(_handle(), 'cv_studio', opener=gone)
        self.assertEqual(result['state'], client.STATE_UNREACHABLE)

    def test_malformed_convoy_id_never_reaches_the_host(self):
        def forbidden(req, timeout=None):
            raise AssertionError('malformed id must not reach the host')

        for value in ('', ' cv_studio', 'cv\nstudio', 'x' * 129, None):
            result = client.network_nodes(_handle(), value, opener=forbidden)
            self.assertEqual(result['state'], client.STATE_ERROR)
            self.assertEqual(result['reason'], 'malformed_convoy_id')


class TestConvoyClientSiblingAPI(EmbodyTestCase):
    """Pure worker-side relay helpers for TouchDesigner-originated calls."""

    NODE_ID = 'n' * 32
    CONVOY_ID = 'cv_studio'

    @staticmethod
    def _job(state='queued', convoy_id=CONVOY_ID):
        return {
            'delivery_id': 'cj_123', 'state': state,
            'convoy_id': convoy_id, 'node_id': TestConvoyClientSiblingAPI.NODE_ID,
            'operation': 'convoy_ping', 'created': 1.0, 'updated': 2.0,
        }

    def test_local_submit_uses_atomic_same_host_relay(self):
        seen = []

        def opener(req, timeout=None):
            seen.append((req.get_method(), req.full_url,
                         json.loads(req.data) if req.data else None))
            return _Resp({'ok': True, 'created': True,
                          'job': self._job()})

        result = client.submit_sibling_call(
            _handle(), HOST_ID, self.CONVOY_ID, self.NODE_ID,
            'td:source', 'convoy_ping', {}, idempotency_key='stable',
            opener=opener)
        self.assertEqual(result['state'], client.SIBLING_ACCEPTED)
        self.assertTrue(result['local_target'])
        self.assertEqual(result['job']['delivery_id'], 'cj_123')
        self.assertLen(seen, 1)
        self.assertEqual(seen[0][0:2],
                         ('POST', 'http://127.0.0.1:8080/relay'))
        self.assertEqual(seen[0][2], {
            'target_host_id': HOST_ID,
            'convoy_id': self.CONVOY_ID,
            'target_node_id': self.NODE_ID,
            'controller_id': 'td:source', 'operation': 'convoy_ping',
            'arguments': {}, 'timeout_s': 30.0,
            'idempotency_key': 'stable',
        })

    def test_local_submit_obeys_atomic_namespace_refusal_without_fallback(self):
        calls = []

        def opener(req, timeout=None):
            calls.append((req.get_method(), req.full_url))
            return _Resp({'ok': False, 'reason': 'unknown_node',
                          'detail': 'not in requested Convoy'})

        result = client.submit_sibling_call(
            _handle(), HOST_ID, self.CONVOY_ID, self.NODE_ID,
            'td:source', 'convoy_ping', {}, opener=opener)
        self.assertEqual(result['reason'], 'unknown_node')
        self.assertEqual(calls, [
            ('POST', 'http://127.0.0.1:8080/relay')])

    def test_remote_submit_uses_relay_and_never_local_fallback(self):
        seen = []

        def opener(req, timeout=None):
            seen.append((req.get_method(), req.full_url,
                         json.loads(req.data)))
            return _Resp({'ok': True, 'created': True,
                          'request_id': 'rq_1', 'job': self._job()})

        result = client.submit_sibling_call(
            _handle(), OTHER_HOST_ID, self.CONVOY_ID, self.NODE_ID,
            'td:source', 'query_network', {'path': '/'},
            expected_runtime_id='rt_target', idempotency_key='stable',
            timeout_s=12, opener=opener)
        self.assertFalse(result['local_target'])
        self.assertLen(seen, 1)
        self.assertEqual(seen[0][0:2],
                         ('POST', 'http://127.0.0.1:8080/relay'))
        body = seen[0][2]
        self.assertEqual(body['target_host_id'], OTHER_HOST_ID)
        self.assertEqual(body['target_node_id'], self.NODE_ID)
        self.assertEqual(body['convoy_id'], self.CONVOY_ID)
        self.assertEqual(body['expected_runtime_id'], 'rt_target')
        self.assertEqual(body['controller_id'], 'td:source')

    def test_submit_passes_only_remaining_total_deadline_to_transport(self):
        seen = {}

        def opener(req, timeout=None):
            seen['timeout'] = timeout
            return _Resp({'ok': True, 'job': self._job()})

        clock = iter((10.0, 10.4)).__next__
        result = client.submit_sibling_call(
            _handle(), OTHER_HOST_ID, self.CONVOY_ID, self.NODE_ID,
            'td:source', 'convoy_ping', {}, timeout_s=1.0,
            opener=opener, monotonic=clock)
        self.assertEqual(result['state'], client.SIBLING_ACCEPTED)
        self.assertAlmostEqual(seen['timeout'], 0.6)

    def test_bad_sibling_call_shape_never_reaches_the_host(self):
        def forbidden(*args, **kwargs):
            raise AssertionError('network must not be reached')

        cases = (
            {'target_host_id': '', 'arguments': {}},
            {'target_host_id': OTHER_HOST_ID, 'arguments': []},
            {'target_host_id': OTHER_HOST_ID,
             'arguments': {'bad': object()}},
            {'target_host_id': OTHER_HOST_ID,
             'arguments': {'bad': float('nan')}},
            {'target_host_id': OTHER_HOST_ID, 'timeout_s': float('nan'),
             'arguments': {}},
        )
        for case in cases:
            with self.subTest(case=case):
                result = client.submit_sibling_call(
                    _handle(), case['target_host_id'], self.CONVOY_ID,
                    self.NODE_ID, 'td:source', 'query_network',
                    case['arguments'], timeout_s=case.get('timeout_s', 30),
                    opener=forbidden)
                self.assertEqual(result['reason'], 'invalid_arguments')

    def test_oversized_sibling_arguments_are_refused_before_network_io(self):
        def forbidden(*args, **kwargs):
            raise AssertionError('oversized arguments must stay local')

        result = client.submit_sibling_call(
            _handle(), OTHER_HOST_ID, self.CONVOY_ID, self.NODE_ID,
            'td:source', 'query_network',
            {'blob': 'x' * client.MAX_SIBLING_REQUEST_BYTES},
            opener=forbidden)
        self.assertEqual(result['state'], client.STATE_ERROR)
        self.assertEqual(result['reason'], 'invalid_arguments')

    def test_local_get_job_is_namespace_checked_and_redacted(self):
        raw = self._job(state='succeeded')
        raw.update({'arguments': {'token': 'must-not-return'},
                    'origin_host_id': 'private', 'result': {'pong': True}})
        result = client.get_sibling_job(
            _handle(), HOST_ID, self.CONVOY_ID, 'cj_123',
            opener=lambda req, timeout=None: _Resp(
                {'ok': True, 'job': raw}))
        self.assertEqual(result['state'], client.SIBLING_JOB)
        self.assertEqual(result['job']['result'], {'pong': True})
        self.assertNotIn('arguments', result['job'])
        self.assertNotIn('origin_host_id', result['job'])

        mismatch = client.get_sibling_job(
            _handle(), HOST_ID, 'cv_wrong', 'cj_123',
            opener=lambda req, timeout=None: _Resp(
                {'ok': True, 'job': raw}))
        self.assertEqual(mismatch['reason'], 'job_namespace_mismatch')

    def test_remote_get_job_uses_future_stable_owner_schema(self):
        seen = {}

        def opener(req, timeout=None):
            seen['url'] = req.full_url
            seen['body'] = json.loads(req.data)
            remote = self._job(state='running')
            remote.pop('convoy_id')
            return _Resp({'ok': True, 'changed': True,
                          'cursor': 3.0, 'job': remote})

        result = client.get_sibling_job(
            _handle(), OTHER_HOST_ID, self.CONVOY_ID, 'cj_123',
            since=2.0, opener=opener)
        self.assertEqual(seen['url'],
                         'http://127.0.0.1:8080/relay/job')
        self.assertEqual(seen['body'], {
            'target_host_id': OTHER_HOST_ID, 'convoy_id': self.CONVOY_ID,
            'delivery_id': 'cj_123', 'since': 2.0})
        self.assertEqual(result['job']['state'], 'running')

    def test_remote_cancel_uses_federated_owner_route_without_waking_td(self):
        seen = {}

        def opener(req, timeout=None):
            seen['url'] = req.full_url
            seen['body'] = json.loads(req.data)
            return _Resp({'ok': True, 'cancelled': True,
                          'definitive': True})

        result = client.cancel_sibling_job(
            _handle(), OTHER_HOST_ID, self.CONVOY_ID, 'cj_123',
            opener=opener)
        self.assertEqual(seen['url'],
                         'http://127.0.0.1:8080/relay/cancel')
        self.assertEqual(seen['body'], {
            'target_host_id': OTHER_HOST_ID,
            'convoy_id': self.CONVOY_ID,
            'delivery_id': 'cj_123',
        })
        self.assertEqual(result['state'], client.SIBLING_CANCEL)
        self.assertEqual(result['scope'], 'owner_host')
        self.assertTrue(result['remote_supported'])
        self.assertFalse(result['local_target'])
        self.assertFalse(result['wakes_touchdesigner'])

    def test_local_cancel_uses_same_federated_route_with_exact_namespace(self):
        seen = []

        def opener(req, timeout=None):
            seen.append((req.get_method(), req.full_url,
                         json.loads(req.data) if req.data else None))
            return _Resp({'ok': True, 'cancel_requested': True,
                          'definitive': False})

        result = client.cancel_sibling_job(
            _handle(), HOST_ID, self.CONVOY_ID, 'cj_123', opener=opener)
        self.assertEqual(seen, [
            ('POST', 'http://127.0.0.1:8080/relay/cancel', {
                'target_host_id': HOST_ID,
                'convoy_id': self.CONVOY_ID,
                'delivery_id': 'cj_123',
            }),
        ])
        self.assertTrue(result['cancel_requested'])
        self.assertEqual(result['state'], client.SIBLING_CANCEL)
        self.assertTrue(result['local_target'])

    def test_wait_job_emits_plain_progress_and_stops_on_terminal(self):
        progress = []
        terminal = self._job(state='succeeded')
        terminal.pop('convoy_id')
        result = client.wait_sibling_job(
            _handle(), OTHER_HOST_ID, self.CONVOY_ID, 'cj_123',
            initial={'ok': True, 'job': {'delivery_id': 'cj_123',
                                         'state': 'queued', 'updated': 1}},
            timeout_s=5, progress=lambda value: progress.append(value),
            opener=lambda req, timeout=None: _Resp({
                'ok': True, 'changed': True, 'cursor': 2,
                'job': terminal}), sleep=lambda _seconds: None,
            monotonic=iter((0, 0, 0, 0, 0)).__next__)
        self.assertEqual(result['job']['state'], 'succeeded')
        self.assertLen(progress, 1)
        self.assertEqual(progress[0]['job']['state'], 'succeeded')


class TestConvoyClientPolicy(EmbodyTestCase):

    POLICY = {
        'generation': 3,
        'allow_td_python': True,
        'allow_full_shell': False,
        'artifact_quota_mb': 512,
    }

    def test_get_policy_is_bounded_and_detached(self):
        seen = {}

        def opener(req, timeout=None):
            seen['url'] = req.full_url
            headers = {key.lower(): value
                       for key, value in req.header_items()}
            seen['token'] = headers.get(client.TOKEN_HEADER.lower())
            return _Resp({'ok': True, 'policy': dict(self.POLICY)})

        result = client.get_policy(_handle(), 'n' * 32, opener=opener)
        self.assertEqual(result['state'], client.POLICY_RESULT)
        self.assertEqual(result['policy'], self.POLICY)
        self.assertIn('node_id=' + 'n' * 32, seen['url'])
        self.assertEqual(seen['token'], _handle().token)

    def test_malformed_policy_is_a_host_error(self):
        result = client.get_policy(
            _handle(), opener=lambda req, timeout=None: _Resp({
                'ok': True,
                'policy': {**self.POLICY, 'allow_full_shell': 1},
            }))
        self.assertEqual(result['state'], client.STATE_HOST_ERROR)
        self.assertEqual(result['reason'], 'host_bad_response')

    def test_begin_challenge_preserves_the_exact_local_phrase(self):
        phrase = 'ENABLE TD PYTHON ' + 'n' * 32 + ' A1B2C3D4'
        result = client.begin_policy_challenge(
            _handle(), 'td_python', 3, node_id='n' * 32,
            opener=lambda req, timeout=None: _Resp({
                'ok': True,
                'challenge': {
                    'challenge_id': 'challenge',
                    'confirmation': phrase,
                    'setting': 'td_python',
                    'node_id': 'n' * 32,
                    'generation': 3,
                },
            }))
        self.assertEqual(result['state'], 'challenge')
        self.assertEqual(result['challenge']['confirmation'], phrase)

    def test_confirm_disable_and_quota_use_the_named_routes(self):
        paths = []

        def opener(req, timeout=None):
            paths.append(req.full_url)
            return _Resp({'ok': True, 'policy': dict(self.POLICY)})

        self.assertEqual(client.confirm_policy_challenge(
            _handle(), 'cid', 'phrase', 3, opener=opener)['state'],
            client.POLICY_RESULT)
        self.assertTrue(client.disable_policy(
            _handle(), 'td_python', node_id='n' * 32,
            opener=opener)['ok'])
        self.assertTrue(client.set_artifact_quota(
            _handle(), 512, 3, opener=opener)['ok'])
        self.assertTrue(paths[0].endswith('/policy/confirm'))
        self.assertTrue(paths[1].endswith('/policy/disable'))
        self.assertTrue(paths[2].endswith('/policy/artifact-quota'))

    def test_policy_refusal_is_not_misreported_as_transport_absence(self):
        result = client.begin_policy_challenge(
            _handle(), 'full_shell', 0,
            opener=_refusing_opener(409, {
                'ok': False,
                'reason': 'policy_generation_conflict',
                'detail': 'stale',
            }))
        self.assertEqual(result['state'], client.STATE_REFUSED)
        self.assertEqual(result['reason'], 'policy_generation_conflict')


class TestConvoyClientUnregister(EmbodyTestCase):

    def test_a_successful_unregister_reports_the_node(self):
        result = client.unregister(
            _handle(), 'n' * 32,
            opener=lambda req, timeout=None: _Resp(
                {'ok': True, 'node_id': 'n' * 32, 'envoy_port': None}))
        self.assertEqual(result['state'], client.STATE_UNREGISTERED)
        self.assertEqual(result['node_id'], 'n' * 32)
        self.assertEqual(client.status_text(result), 'Disabled')

    def test_unregister_posts_the_node_id(self):
        seen = {}

        def opener(req, timeout=None):
            seen['url'] = req.full_url
            seen['data'] = req.data
            seen['timeout'] = timeout
            return _Resp({'ok': True})

        client.unregister(_handle(), 'n' * 32, opener=opener)
        self.assertEqual(seen['url'], 'http://127.0.0.1:8080/unregister')
        self.assertEqual(json.loads(seen['data']), {
            'node_id': 'n' * 32,
            'reason': 'disabled',
        })

    def test_shutdown_intent_rides_on_unregister(self):
        seen = {}

        def opener(req, timeout=None):
            seen['data'] = json.loads(req.data)
            return _Resp({'ok': True})

        client.unregister(_handle(), 'n' * 32, reason='shutdown',
                          opener=opener)
        self.assertEqual(seen['data']['reason'], 'shutdown')

    def test_invalid_unregister_reason_fails_closed_before_network(self):
        def opener(req, timeout=None):
            raise AssertionError('invalid intent must not reach the host')

        result = client.unregister(_handle(), 'n' * 32,
                                   reason='TD exit', opener=opener)
        self.assertEqual(result['state'], client.STATE_ERROR)
        self.assertEqual(result['reason'], 'invalid_unregister_reason')

    def test_unregister_uses_a_short_single_attempt_timeout(self):
        """Best-effort on the way out: a shutting-down session must never
        block on a host app that is itself going away."""
        seen = {}

        def opener(req, timeout=None):
            seen['timeout'] = timeout
            return _Resp({'ok': True})

        client.unregister(_handle(), 'n', opener=opener)
        self.assertEqual(seen['timeout'], 1.0)
        self.assertEqual(client.UNREGISTER_TIMEOUT_S, 1.0)

    def test_an_unknown_node_folds_to_a_clean_unregister(self):
        """'The node is already gone' is exactly what an unregister
        WANTS. A host that restarted with a fresh state dir since we
        registered answers 404 unknown_node, and surfacing that as
        'Refused: unknown_node' on a normal disable is alarming noise
        about a completed outcome."""
        opener = _refusing_opener(404, {'ok': False,
                                        'reason': 'unknown_node',
                                        'detail': 'ghost'})
        result = client.unregister(_handle(), 'ghost', opener=opener)
        self.assertEqual(result['state'], client.STATE_UNREGISTERED)
        self.assertIs(result['already_gone'], True)
        self.assertEqual(client.status_text(result), 'Disabled')

    def test_other_refusals_are_still_refusals(self):
        """The fold is scoped to unknown_node -- a 401 must not be
        laundered into a clean disable."""
        opener = _refusing_opener(401, {'ok': False,
                                        'reason': 'unauthenticated'})
        result = client.unregister(_handle(), 'n', opener=opener)
        self.assertEqual(result['state'], client.STATE_REFUSED)
        self.assertEqual(result['reason'], 'unauthenticated')

    # -- ownership: only clear the port YOU registered -----------------

    def test_the_runtime_id_rides_along_when_given(self):
        """Two TD sessions on one project folder share a node_id, so
        without this proof a departing instance zeroes a surviving
        instance's live port."""
        seen = {}

        def opener(req, timeout=None):
            seen['data'] = json.loads(req.data)
            return _Resp({'ok': True, 'cleared': True})

        client.unregister(_handle(), 'n' * 32, runtime_id='rt_abc',
                          opener=opener)
        self.assertEqual(seen['data'], {'node_id': 'n' * 32,
                                        'runtime_id': 'rt_abc',
                                        'reason': 'disabled'})

    def test_no_runtime_id_means_no_runtime_field(self):
        seen = {}

        def opener(req, timeout=None):
            seen['data'] = json.loads(req.data)
            return _Resp({'ok': True})

        client.unregister(_handle(), 'n' * 32, opener=opener)
        self.assertNotIn('runtime_id', seen['data'])

    def test_a_superseded_no_op_is_reported_as_such(self):
        """The host answers 200 cleared:false when a newer run owns the
        port. That is a success for the departing session, but the
        caller must be able to see it did nothing."""
        result = client.unregister(
            _handle(), 'n' * 32, runtime_id='rt_old',
            opener=lambda req, timeout=None: _Resp(
                {'ok': True, 'cleared': False,
                 'reason': 'runtime_superseded', 'node_id': 'n' * 32}))
        self.assertEqual(result['state'], client.STATE_UNREGISTERED)
        self.assertIs(result['cleared'], False)
        self.assertEqual(client.status_text(result), 'Disabled')

    def test_a_transport_failure_is_absence(self):
        def opener(req, timeout=None):
            raise urllib.error.URLError('gone')
        result = client.unregister(_handle(), 'n', opener=opener)
        self.assertEqual(result['state'], client.STATE_UNREACHABLE)
        self.assertEqual(client.status_text(result), 'No Convoy host app')


# =====================================================================
# PARITY -- convoy_client vs dev/convoy/convoy_hostprobe
# =====================================================================

class TestConvoyClientHostprobeParity(EmbodyTestCase):
    """The decision tree now lives in two files because ConvoyExt cannot
    import dev/convoy. Two copies drift; this is what stops it."""

    def setUp(self):
        super().setUp()
        self.hostprobe = _load_convoy_module('convoy_hostprobe')
        if self.hostprobe is None:
            raise unittest.SkipTest('dev/convoy is not in this checkout')

    def _cases(self):
        """Every probe outcome, as one set of kwargs per case. The
        SIGNATURES match argument for argument, which is what lets both
        modules be driven by the same dict."""
        return {
            'running': dict(
                data_dir='/x', portfile_reader=_live_reader(),
                token_reader=lambda d: 'tok',
                health_check=lambda handle: HOST_ID),
            'absent_no_portfile': dict(
                data_dir='/x', portfile_reader=lambda d: None,
                raw_portfile_reader=lambda d: None,
                token_reader=lambda d: 'tok'),
            'stale_dead_writer': dict(
                data_dir='/x', portfile_reader=lambda d: None,
                raw_portfile_reader=lambda d: {'port': 1, 'pid': 999999},
                token_reader=lambda d: 'tok'),
            'absent_no_token': dict(
                data_dir='/x', portfile_reader=_live_reader(),
                token_reader=lambda d: None,
                health_check=lambda handle: HOST_ID),
            'stale_no_health': dict(
                data_dir='/x', portfile_reader=_live_reader(),
                token_reader=lambda d: 'tok',
                health_check=lambda handle: None),
            'stale_identity_mismatch': dict(
                data_dir='/x', portfile_reader=_live_reader(),
                token_reader=lambda d: 'tok',
                health_check=lambda handle: OTHER_HOST_ID),
        }

    def test_the_status_constants_are_identical(self):
        self.assertEqual(client.STATUS_RUNNING,
                         self.hostprobe.STATUS_RUNNING)
        self.assertEqual(client.STATUS_ABSENT,
                         self.hostprobe.STATUS_ABSENT)
        self.assertEqual(client.STATUS_STALE, self.hostprobe.STATUS_STALE)

    def test_the_probe_signatures_are_identical(self):
        import inspect
        self.assertEqual(str(inspect.signature(client.probe)),
                         str(inspect.signature(self.hostprobe.probe)))

    def test_both_modules_agree_on_every_probe_outcome(self):
        for name, kwargs in self._cases().items():
            ours = client.probe(**kwargs)
            theirs = self.hostprobe.probe(**kwargs)
            self.assertEqual(
                ours.status, theirs.status,
                'DRIFT on %r: convoy_client says %r, convoy_hostprobe '
                'says %r' % (name, ours.status, theirs.status))
            self.assertEqual(ours.use_convoy, theirs.use_convoy, name)

    def test_both_modules_agree_on_all_four_documented_outcomes(self):
        """The contract as written in both docstrings: running / absent /
        stale, plus live-but-tokenless folding into absent."""
        cases = self._cases()
        expected = {
            'running': client.STATUS_RUNNING,
            'absent_no_portfile': client.STATUS_ABSENT,
            'stale_dead_writer': client.STATUS_STALE,
            'absent_no_token': client.STATUS_ABSENT,
        }
        for name, want in expected.items():
            self.assertEqual(client.probe(**cases[name]).status, want, name)
            self.assertEqual(self.hostprobe.probe(**cases[name]).status,
                             want, name)

    def test_both_modules_produce_an_equivalent_handle_when_running(self):
        kwargs = self._cases()['running']
        ours = client.probe(**kwargs).handle
        theirs = self.hostprobe.probe(**kwargs).handle
        self.assertEqual(ours.port, theirs.port)
        self.assertEqual(ours.token, theirs.token)
        self.assertEqual(ours.host_id, theirs.host_id)
        self.assertEqual(ours.base_url, theirs.base_url)

    def test_every_non_running_outcome_carries_a_detail_on_both_sides(self):
        for name, kwargs in self._cases().items():
            if name == 'running':
                continue
            self.assertTrue(client.probe(**kwargs).detail, name)
            self.assertTrue(self.hostprobe.probe(**kwargs).detail, name)

    def test_the_token_header_names_match(self):
        """A mismatched header name would 401 every call from TD while
        the bridge's calls sailed through."""
        hostapp = _load_convoy_module('convoy_hostapp')
        if hostapp is None:
            raise unittest.SkipTest('dev/convoy is not in this checkout')
        self.assertEqual(client.TOKEN_HEADER.lower(),
                         hostapp.TOKEN_HEADER.lower())

    def test_the_data_dir_and_filenames_match_convoy_platform(self):
        platform_mod = _load_convoy_module('convoy_platform')
        if platform_mod is None:
            raise unittest.SkipTest('dev/convoy is not in this checkout')
        self.assertEqual(client.APP_DIR_NAME, platform_mod.APP_DIR_NAME)
        self.assertEqual(client.TOKEN_FILE, platform_mod.TOKEN_FILE)
        self.assertEqual(client.PORT_FILE, platform_mod.PORT_FILE)
        for plat, home in (('win32', 'C:\\Users\\x'),
                           ('darwin', '/Users/x'),
                           ('linux', '/home/x')):
            self.assertEqual(
                client.data_dir(platform=plat, env={}, home=home),
                platform_mod.data_dir(platform=plat, env={}, home=home),
                plat)


# =====================================================================
# PARITY -- the DUPLICATED PRIMITIVES, not just the branch tree
# =====================================================================

def _dead_kill(pid, sig):
    raise ProcessLookupError(pid)


def _alive_kill(pid, sig):
    return None


def _eperm_kill(pid, sig):
    raise PermissionError(pid)


class TestConvoyClientPlatformPrimitiveParity(EmbodyTestCase):
    """convoy_client copies four functions VERBATIM from
    convoy_platform (pid_is_alive, read_portfile, read_live_portfile,
    data_dir) plus read_token from convoy_hostprobe.

    The probe-tree parity test below injects every reader, so it never
    drives these at all -- which means the exact property both module
    docstrings advertise ("probe() NEVER returns a port whose writer is
    not alive, because it reads through read_live_portfile, which
    verifies the pid") was the one thing parity did not pin. Two
    reviewers demonstrated the hole by mutating convoy_platform and
    watching every suite stay green. These cases drive both copies over
    the SAME fixtures so that can no longer happen.
    """

    def setUp(self):
        super().setUp()
        self.platform_mod = _load_convoy_module('convoy_platform')
        self.hostprobe = _load_convoy_module('convoy_hostprobe')
        if self.platform_mod is None or self.hostprobe is None:
            raise unittest.SkipTest('dev/convoy is not in this checkout')
        self._temp_dirs = []

    def tearDown(self):
        import shutil
        for directory in getattr(self, '_temp_dirs', []):
            shutil.rmtree(directory, ignore_errors=True)
        super().tearDown()

    def _temp_dir(self):
        import tempfile
        directory = tempfile.mkdtemp(prefix='convoy_parity_')
        self._temp_dirs.append(directory)
        return directory

    def _assertSame(self, label, ours, theirs):
        self.assertEqual(
            ours, theirs,
            'DRIFT in %s: convoy_client says %r, dev/convoy says %r'
            % (label, ours, theirs))

    # -- pid_is_alive --------------------------------------------------

    def test_pid_is_alive_agrees_on_the_posix_triple(self):
        cases = [
            ('dead', _dead_kill), ('alive', _alive_kill),
            ('eperm', _eperm_kill),
        ]
        for platform in ('linux', 'darwin'):
            for label, kill in cases:
                self._assertSame(
                    'pid_is_alive/%s/%s' % (platform, label),
                    client.pid_is_alive(4242, platform=platform, kill=kill),
                    self.platform_mod.pid_is_alive(4242, platform=platform,
                                                   kill=kill))

    def test_pid_is_alive_agrees_on_nonsense_pids(self):
        for bad in (0, -1, None):
            self._assertSame(
                'pid_is_alive/%r' % (bad,),
                client.pid_is_alive(bad, platform='linux', kill=_alive_kill),
                self.platform_mod.pid_is_alive(bad, platform='linux',
                                               kill=_alive_kill))

    def test_pid_is_alive_agrees_on_every_win32_outcome(self):
        """Including ACCESS_DENIED, the case that was WRONG in both
        copies -- a fix on one side that did not propagate is exactly
        the drift this test exists to catch."""
        cases = {
            'access_denied': dict(handle=0, last_error=5),
            'invalid_param': dict(handle=0, last_error=87),
            'signaled_exited': dict(handle=1234, wait=0),
            'running': dict(handle=1234, wait=0x102),
        }
        for label, kwargs in cases.items():
            self._assertSame(
                'pid_is_alive/win32/%s' % (label,),
                client.pid_is_alive(4242, platform='win32',
                                    kernel32=_FakeKernel32(**kwargs)),
                self.platform_mod.pid_is_alive(
                    4242, platform='win32',
                    kernel32=_FakeKernel32(**kwargs)))

    # -- read_portfile / read_live_portfile -----------------------------

    def _portfile_fixtures(self):
        """One temp dir per shape the reader must survive."""
        fixtures = {}

        empty = self._temp_dir()
        fixtures['no_portfile'] = empty

        for label, payload in (
                ('valid', '{"port": 9999, "pid": %d, "host_id": "h"}'
                          % (os.getpid(),)),
                ('corrupt', '{ half-written'),
                ('not_a_dict', '[1, 2, 3]'),
                ('missing_port', '{"pid": 1}'),
                ('port_zero', '{"port": 0, "pid": 1}'),
                ('non_numeric_pid', '{"port": 9999, "pid": "nope"}'),
                ('dead_pid', '{"port": 9999, "pid": 999999}'),
                ('empty_file', ''),
        ):
            directory = self._temp_dir()
            with open(os.path.join(directory, client.PORT_FILE), 'w') as f:
                f.write(payload)
            fixtures[label] = directory
        return fixtures

    def test_read_portfile_agrees_on_every_fixture(self):
        for label, directory in self._portfile_fixtures().items():
            self._assertSame('read_portfile/%s' % (label,),
                             client.read_portfile(directory),
                             self.platform_mod.read_portfile(directory))

    def test_read_live_portfile_agrees_on_every_fixture(self):
        """The safety property itself: same fixtures, same injected
        liveness, same answer -- and a dead writer yields None on both
        sides."""
        for label, directory in self._portfile_fixtures().items():
            for kill_label, kill in (('alive', _alive_kill),
                                     ('dead', _dead_kill)):
                self._assertSame(
                    'read_live_portfile/%s/%s' % (label, kill_label),
                    client.read_live_portfile(directory, platform='linux',
                                              kill=kill),
                    self.platform_mod.read_live_portfile(
                        directory, platform='linux', kill=kill))

    def test_neither_copy_hands_out_a_dead_writers_port(self):
        directory = self._portfile_fixtures()['valid']
        self.assertIsNone(client.read_live_portfile(
            directory, platform='linux', kill=_dead_kill))
        self.assertIsNone(self.platform_mod.read_live_portfile(
            directory, platform='linux', kill=_dead_kill))

    # -- read_token -----------------------------------------------------

    def test_read_token_agrees_on_every_fixture(self):
        """convoy_client.read_token mirrors convoy_hostprobe._read_token
        -- the pair where the UnicodeDecodeError hole was found on one
        side only."""
        fixtures = {'missing': self._temp_dir()}
        for label, raw in (
                ('valid', b'abc123\n'),
                ('empty', b''),
                ('whitespace', b'   \n'),
                ('non_utf8', b'\xff\xfe\x00secret'),
                ('trailing_space', b'  tok  \n'),
        ):
            directory = self._temp_dir()
            with open(os.path.join(directory, client.TOKEN_FILE), 'wb') as f:
                f.write(raw)
            fixtures[label] = directory

        for label, directory in fixtures.items():
            self._assertSame('read_token/%s' % (label,),
                             client.read_token(directory),
                             self.hostprobe._read_token(directory))

    # -- data_dir (kept from the original parity set) --------------------

    def test_data_dir_agrees_across_platforms_and_envs(self):
        cases = [
            ('win32', {'LOCALAPPDATA': 'C:\\Users\\x\\AppData\\Local'},
             'C:\\Users\\x'),
            ('win32', {}, 'C:\\Users\\x'),
            ('darwin', {}, '/Users/x'),
            ('linux', {'XDG_STATE_HOME': '/home/x/.local/state'}, '/home/x'),
            ('linux', {}, '/home/x'),
        ]
        for plat, env, home in cases:
            self._assertSame(
                'data_dir/%s/%s' % (plat, sorted(env)),
                client.data_dir(platform=plat, env=env, home=home),
                self.platform_mod.data_dir(platform=plat, env=env,
                                           home=home))

    # -- and the structural guard ---------------------------------------

    def test_the_copied_primitives_are_still_code_identical(self):
        """Behavioural parity above catches semantic drift over the
        fixtures it happens to cover; this catches a change to a path no
        fixture reaches. Docstrings are stripped -- only the code has to
        match."""
        ours = _function_bodies(_CLIENT_PATH)
        theirs = _function_bodies(os.path.join(_CONVOY_DIR,
                                               'convoy_platform.py'))
        for name in ('data_dir', 'read_portfile', 'read_live_portfile',
                     'pid_is_alive', '_win32_pid_is_alive'):
            self.assertIn(name, ours, 'convoy_client lost %r' % (name,))
            self.assertIn(name, theirs, 'convoy_platform lost %r' % (name,))
            self.assertEqual(
                ours[name], theirs[name],
                'convoy_client.%s has drifted from convoy_platform.%s -- '
                'these are deliberate verbatim copies; change both or '
                'document why they may differ' % (name, name))


def _function_bodies(path):
    """{name: normalized AST dump} for every top-level function in a
    module, docstrings stripped so prose may differ freely."""
    with open(path, 'r', encoding='utf-8') as f:
        tree = ast.parse(f.read())
    bodies = {}
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        body = list(node.body)
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(getattr(body[0], 'value', None), ast.Constant)
                and isinstance(body[0].value.value, str)):
            body = body[1:]
        clone = ast.Module(body=body, type_ignores=[])
        bodies[node.name] = ast.dump(clone)
    return bodies


# =====================================================================
# INTEGRATION -- a real HostApp, real loopback HTTP, no TD
# =====================================================================

class TestConvoyClientAgainstARealHostApp(EmbodyTestCase):
    """register -> the host reports the port -> unregister -> the port is
    cleared -> re-register restores it. The Phase 2 registration
    lifecycle, end to end, with nothing mocked.

    PYTEST ONLY -- deliberately skipped inside TouchDesigner. This class
    binds a real listening socket and makes synchronous loopback HTTP
    calls; in TD the runner drives it on the MAIN thread, where the
    closed-port probe below alone costs ~2.7 s (this box refuses a dead
    loopback port in ~2.0 s) -- about 160 dropped frames, and up to the
    full 10 s register timeout if a socket wedges. No other in-TD suite
    binds a socket; test_envoy_bridge.py mocks urlopen instead. The
    coverage still runs on every CI leg, which is where it belongs.
    """

    def setUp(self):
        super().setUp()
        import tempfile
        if _in_touchdesigner():
            raise unittest.SkipTest(
                'binds a listening socket and blocks on loopback HTTP; '
                'runs under pytest, never on TD\'s main thread')
        self.hostapp = _load_convoy_module('convoy_hostapp')
        if self.hostapp is None:
            raise unittest.SkipTest('dev/convoy is not in this checkout')
        # Assigned BEFORE anything can fail, so a partial setUp cannot
        # leak a bound socket and a live daemon thread for the life of
        # the process -- unittest skips tearDown when setUp raises.
        self._root = None
        self.app = None
        self.server = None
        try:
            self._root = tempfile.mkdtemp(prefix='convoy_client_e2e_')
            self.data_dir = os.path.join(self._root, 'state')
            self.app = self.hostapp.HostApp(
                self.data_dir,
                artifact_cache_path=os.path.join(self._root, 'artifacts'))
            self.server, self.port = self.hostapp.serve(self.app, port=0)
            self.thread = threading.Thread(target=self.server.serve_forever,
                                           daemon=True)
            self.thread.start()
        except Exception:
            self._teardown_server()
            raise

    def _teardown_server(self):
        import shutil
        if self.app is not None:
            try:
                self.app.stop_drain_loop()
            except Exception:
                pass
        if self.server is not None:
            try:
                self.server.shutdown()
                self.server.server_close()
            except Exception:
                pass
        if self.app is not None:
            try:
                self.app.db.close()
            except Exception:
                pass
        if self._root is not None:
            shutil.rmtree(self._root, ignore_errors=True)

    def tearDown(self):
        self._teardown_server()
        super().tearDown()

    def _nodes(self):
        req = urllib.request.Request(
            'http://127.0.0.1:%s/nodes' % (self.port,),
            headers={client.TOKEN_HEADER: self.app.token})
        last = None
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    return json.loads(r.read().decode())['nodes']
            except urllib.error.HTTPError:
                raise           # a real answer, never retried
            except (urllib.error.URLError, OSError) as e:
                last = e
                time.sleep(0.1 * (attempt + 1))
        raise last

    def _payload(self, port=9981):
        return client.registration_payload(
            project_root='/Work/Show', comp_path='/embody/Embody',
            convoy_id='cv_integration', runtime_id='rt_0123456789abcdef',
            envoy_port=port)

    # -- transport-flake retries, and ONLY transport flakes ------------
    #
    # A loopback connection aborted/reset under load (WinError 10053) is
    # a documented flake on this matrix -- dev/convoy's own Server.call
    # carries the same retry for the same reason, and it bit this class
    # once on the 401 path during review. The retry fires ONLY when
    # NOTHING answered (STATE_UNREACHABLE); a real HTTP answer of any
    # status returns immediately, so a genuine refusal can never be
    # masked. Production code keeps its single-attempt contract -- the
    # retry lives here, in the test, deliberately.

    def _retry_transport(self, call):
        result = None
        for attempt in range(4):
            result = call()
            if result.get('state') != client.STATE_UNREACHABLE:
                return result
            time.sleep(0.1 * (attempt + 1))
        return result

    def _handle(self):
        """A confirmed-RUNNING handle, tolerating a flaky /health."""
        for attempt in range(4):
            result = client.probe(data_dir=self.data_dir)
            if result.status == client.STATUS_RUNNING:
                return result.handle
            time.sleep(0.1 * (attempt + 1))
        self.fail('the host app never probed RUNNING: %s' % (result.detail,))

    def _do_register(self, handle, payload):
        return self._retry_transport(
            lambda: client.register(handle, payload))

    def _do_unregister(self, handle, node_id, runtime_id=None):
        return self._retry_transport(
            lambda: client.unregister(handle, node_id,
                                      runtime_id=runtime_id))

    def test_probe_finds_the_real_host_app_through_the_real_portfile(self):
        """No injected readers at all: the real portfile, the real pid
        check, the real unauthenticated /health identity confirmation."""
        result = client.probe(data_dir=self.data_dir)
        self.assertEqual(result.status, client.STATUS_RUNNING)
        self.assertTrue(result.use_convoy)
        self.assertEqual(result.handle.port, self.port)
        self.assertEqual(result.handle.host_id, self.app.host_id)
        self.assertEqual(result.handle.token, self.app.token)

    def test_the_full_register_unregister_reregister_lifecycle(self):
        handle = self._handle()
        self.assertIsNotNone(handle)

        # 1. register -> the host reports the node and the port.
        registered = self._do_register(handle, self._payload(9981))
        self.assertEqual(registered['state'], client.STATE_REGISTERED)
        node_id = registered['node_id']
        self.assertEqual(registered['envoy_port'], 9981)
        self.assertEqual(registered['runtime_id'], 'rt_0123456789abcdef',
                         'the host must keep OUR runtime_id, not mint one')
        self.assertEqual(client.status_text(registered), 'Connected')

        # 2. /nodes shows exactly one node holding that port.
        nodes = self._nodes()
        self.assertLen(nodes, 1)
        self.assertEqual(nodes[0]['node_id'], node_id)
        self.assertEqual(nodes[0]['envoy_port'], 9981)

        # 3. unregister -> the port is cleared, the node survives.
        cleared = self._do_unregister(handle, node_id)
        self.assertEqual(cleared['state'], client.STATE_UNREGISTERED)
        nodes = self._nodes()
        self.assertLen(nodes, 1, 'the node record must survive')
        self.assertIsNone(nodes[0]['envoy_port'])

        # 4. re-register -> the SAME node_id gets its port back. This is
        #    the heartbeat's healing path after a host-app restart.
        again = self._do_register(handle, self._payload(9982))
        self.assertEqual(again['state'], client.STATE_REGISTERED)
        self.assertEqual(again['node_id'], node_id)
        self.assertEqual(again['envoy_port'], 9982)
        self.assertEqual(self._nodes()[0]['envoy_port'], 9982)

    def test_registering_twice_is_one_node_not_two(self):
        handle = self._handle()
        first = self._do_register(handle, self._payload())
        second = self._do_register(handle, self._payload())
        self.assertEqual(first['node_id'], second['node_id'])
        self.assertLen(self._nodes(), 1)

    def test_local_sibling_submit_get_and_federated_cancel_over_real_http(self):
        handle = self._handle()
        registered = self._do_register(handle, self._payload())
        node_id = registered['node_id']
        accepted = client.submit_sibling_call(
            handle, self.app.host_id, 'cv_integration', node_id,
            'td:%s:%s:rt_source' % (self.app.host_id, node_id),
            'convoy_ping', {}, idempotency_key='client-e2e-sibling')
        self.assertEqual(accepted['state'], client.SIBLING_ACCEPTED)
        self.assertTrue(accepted['local_target'])
        delivery_id = accepted['job']['delivery_id']

        view = client.get_sibling_job(
            handle, self.app.host_id, 'cv_integration', delivery_id)
        self.assertEqual(view['state'], client.SIBLING_JOB)
        self.assertEqual(view['job']['delivery_id'], delivery_id)

        cancelled = client.cancel_sibling_job(
            handle, self.app.host_id, 'cv_integration', delivery_id)
        self.assertEqual(cancelled['state'], client.SIBLING_CANCEL)
        self.assertTrue(cancelled['local_target'])
        self.assertTrue(cancelled['remote_supported'])
        self.assertFalse(cancelled['wakes_touchdesigner'])

    def test_the_runtime_id_we_send_is_the_one_the_host_records(self):
        """The A-22 precondition depends on it: a heartbeat that let the
        host re-mint would invalidate every in-flight expected_runtime_id."""
        handle = self._handle()
        store = {}
        runtime_id = client.ensure_runtime_id(store, '/embody/Embody')
        payload = client.registration_payload(
            '/Work/Show', '/embody/Embody', 'cv_integration', runtime_id,
            envoy_port=9981)
        for _ in range(3):      # three heartbeats, one run
            result = self._do_register(handle, payload)
            self.assertEqual(result['runtime_id'], runtime_id)

    def test_unregistering_an_unknown_node_folds_to_a_clean_disable(self):
        """Over real HTTP: the host really does answer 404 unknown_node,
        and the client really does fold it -- 'already gone' is the
        outcome an unregister wants, not an alarm."""
        handle = self._handle()
        result = self._do_unregister(handle, 'ghost')
        self.assertEqual(result['state'], client.STATE_UNREGISTERED)
        self.assertIs(result['already_gone'], True)
        self.assertEqual(client.status_text(result), 'Disabled')

    def test_a_superseded_run_cannot_clear_a_live_port(self):
        """THE OWNERSHIP REGRESSION, over real loopback HTTP. Two TD
        sessions on ONE project folder share a node_id (OQ-1), so the
        first session's CLEAN EXIT used to zero the second session's
        live port and leave the survivor undispatchable until its next
        ~30 s heartbeat -- a wrong-direction failure manufactured by an
        orderly shutdown."""
        handle = self._handle()
        first = self._do_register(handle, client.registration_payload(
            '/Work/Show', '/embody/Embody', 'cv_integration',
            'rt_aaaaaaaaaaaaaaaa', envoy_port=9981))
        second = self._do_register(handle, client.registration_payload(
            '/Work/Show', '/embody/Embody', 'cv_integration',
            'rt_bbbbbbbbbbbbbbbb', envoy_port=9990))
        self.assertEqual(first['node_id'], second['node_id'],
                         'shared identity is the precondition (OQ-1)')

        # The DEPARTING first instance unregisters, naming its own run.
        result = self._do_unregister(handle, first['node_id'],
                                   runtime_id='rt_aaaaaaaaaaaaaaaa')
        self.assertEqual(result['state'], client.STATE_UNREGISTERED)
        self.assertIs(result['cleared'], False, 'it must not have cleared')

        self.assertEqual(self._nodes()[0]['envoy_port'], 9990,
                         'the surviving instance keeps its live port')

    def test_the_owning_run_still_clears_its_own_port(self):
        handle = self._handle()
        node = self._do_register(handle, client.registration_payload(
            '/Work/Show', '/embody/Embody', 'cv_integration',
            'rt_aaaaaaaaaaaaaaaa', envoy_port=9981))
        result = self._do_unregister(handle, node['node_id'],
                                   runtime_id='rt_aaaaaaaaaaaaaaaa')
        self.assertIs(result['cleared'], True)
        self.assertIsNone(self._nodes()[0]['envoy_port'])

    def test_a_host_side_persist_failure_is_an_error_not_a_refusal(self):
        """Over real HTTP: /register really answers 500 persist_failed
        when the store cannot write, and that MUST be retried -- unlike
        a 409 policy refusal, which must not."""
        handle = self._handle()

        def boom(record):
            raise OSError('disk full (test)')
        original = self.app.db.save_node
        self.app.db.save_node = boom
        try:
            result = self._do_register(handle, self._payload())
        finally:
            self.app.db.save_node = original
        self.assertEqual(result['state'], client.STATE_HOST_ERROR)
        self.assertEqual(result['http_status'], 500)
        self.assertEqual(result['reason'], 'persist_failed')
        self.assertStartsWith(client.status_text(result), 'Error: ')

    def test_unregister_is_idempotent_over_real_http(self):
        handle = self._handle()
        node_id = self._do_register(handle, self._payload())['node_id']
        first = self._do_unregister(handle, node_id)
        second = self._do_unregister(handle, node_id)
        self.assertEqual(first['state'], client.STATE_UNREGISTERED)
        self.assertEqual(second['state'], client.STATE_UNREGISTERED)

    def test_a_bad_token_is_refused_not_silently_accepted(self):
        bad = client.HostHandle(port=self.port, host_id=self.app.host_id,
                                token='0' * 64, data_dir=self.data_dir)
        result = self._do_register(bad, self._payload())
        self.assertEqual(result['state'], client.STATE_REFUSED)
        self.assertEqual(result['reason'], 'unauthenticated')

    def test_switching_convoy_is_refused_with_its_reason(self):
        handle = self._handle()
        self._do_register(handle, self._payload())
        other = client.registration_payload(
            '/Work/Show', '/embody/Embody', 'cv_somewhere_else',
            'rt_0123456789abcdef')
        result = self._do_register(handle, other)
        self.assertEqual(result['state'], client.STATE_REFUSED)
        self.assertEqual(result['reason'], 'node_identity_conflict')
        self.assertStartsWith(client.status_text(result), 'Refused: ')

    def test_a_portless_heartbeat_never_clears_a_live_port(self):
        """registration_payload OMITS envoy_port rather than sending
        null, so a tick before Envoy binds cannot wipe a good port."""
        handle = self._handle()
        self._do_register(handle, self._payload(9981))
        portless = client.registration_payload(
            '/Work/Show', '/embody/Embody', 'cv_integration',
            'rt_0123456789abcdef', envoy_port=None)
        result = self._do_register(handle, portless)
        self.assertEqual(result['envoy_port'], 9981)

    def test_the_unregister_is_audited_host_side(self):
        handle = self._handle()
        node_id = self._do_register(handle, self._payload())['node_id']
        self._do_unregister(handle, node_id)
        with self.app.lock:
            events = [r['event'] for r in self.app.db.audit_tail()]
        self.assertIn('node_unregistered', events)

    def test_probe_reads_stale_once_the_host_app_stops(self):
        """The absence path, for real: stop the server, and the client
        must fall back rather than hand out a dead port."""
        self.server.shutdown()
        self.server.server_close()
        result = client.probe(data_dir=self.data_dir)
        # The portfile still names THIS live pytest process, so the pid
        # check passes and /health is what fails -- exactly the recycled
        # port case, and it must not read as running.
        self.assertNotEqual(result.status, client.STATUS_RUNNING)
        self.assertFalse(result.use_convoy)
        self.assertNotIn('Error', client.status_text(
            {'state': result.status}))


# =====================================================================
# Static: the module must stay TD-free and stdlib-only
# =====================================================================

_STDLIB_ALLOWED = {
    'hashlib', 'json', 'math', 'ntpath', 'os', 'posixpath', 'random', 'secrets', 'sys',
    'time', 'urllib', 'urllib.error', 'urllib.parse', 'urllib.request', 'ctypes',
}

def _code_only(source):
    """The source with every comment and string literal blanked out.

    The scan below must judge CODE, not prose: this module's docstrings
    legitimately discuss `mod.convoy_client` and "cannot import
    convoy_hostprobe", and a naive substring search reads those
    explanations as violations. Blanking preserves offsets, so a
    reported position still points at the real line.
    """
    import io
    import tokenize
    rows = [list(line) for line in source.splitlines(keepends=True)]
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, IndentationError):   # pragma: no cover
        return source
    for tok in tokens:
        if tok.type not in (tokenize.COMMENT, tokenize.STRING):
            continue
        (srow, scol), (erow, ecol) = tok.start, tok.end
        for row in range(srow, erow + 1):
            line = rows[row - 1]
            start = scol if row == srow else 0
            end = ecol if row == erow else len(line)
            for i in range(start, min(end, len(line))):
                if line[i] != '\n':
                    line[i] = ' '
    return ''.join(''.join(row) for row in rows)


_TD_TOKENS = (
    (re.compile(r'^\s*import\s+td\b', re.M), "'import td'"),
    (re.compile(r'^\s*from\s+td\b', re.M), "'from td import'"),
    (re.compile(r'\bop\('), "'op('"),
    (re.compile(r'\bopex\('), "'opex('"),
    (re.compile(r'ownerComp'), "'ownerComp'"),
    (re.compile(r'\bparent\('), "'parent()'"),
    (re.compile(r'\bproject\.'), "'project.'"),
    (re.compile(r'\btdu\.'), "'tdu.'"),
    (re.compile(r'\bme\.par\b'), "'me.par'"),
    (re.compile(r'\bui\.'), "'ui.'"),
    (re.compile(r'\bmod\.'), "'mod.'"),
)


class TestConvoyClientIsTouchDesignerFree(EmbodyTestCase):
    """convoy_client is read from a WORKER THREAD and imported by plain
    pytest with no TD present. One TD reference would be both a
    threading violation in production and a collection error on CI."""

    def setUp(self):
        super().setUp()
        with open(_CLIENT_PATH, 'r', encoding='utf-8') as f:
            self.source = f.read()
        self.code = _code_only(self.source)

    def test_the_source_names_nothing_td_flavoured(self):
        for pattern, label in _TD_TOKENS:
            match = pattern.search(self.code)
            self.assertIsNone(
                match,
                'convoy_client.py must not reference %s (found at offset '
                '%s)' % (label, match.start() if match else -1))

    def test_the_scan_would_actually_catch_a_violation(self):
        """A negative-assertion test is worthless if the scanner is
        broken -- prove it fires on planted code, and that it ignores
        the same text in a docstring."""
        planted = _code_only('x = 1\nnode = op("/embody/Embody")\n')
        self.assertTrue(any(p.search(planted) for p, _ in _TD_TOKENS))
        prose = _code_only('"""reached as mod.convoy_client via op()."""\n')
        self.assertFalse(any(p.search(prose) for p, _ in _TD_TOKENS))

    def test_every_import_is_stdlib(self):
        tree = ast.parse(self.source)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported.add(alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    raise AssertionError('no relative imports: a DAT has '
                                         'no package to be relative to')
                imported.add(node.module)
        unexpected = imported - _STDLIB_ALLOWED
        self.assertEqual(unexpected, set(),
                         'non-stdlib imports: %s' % (sorted(unexpected),))

    def test_it_imports_no_convoy_sibling(self):
        """It cannot: dev/convoy/ does not exist in a released .tox.
        That constraint is the entire reason this module is a copy --
        which is also why the docstrings SAY so, and why this checks the
        import graph rather than the prose."""
        for node in ast.walk(ast.parse(self.source)):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or '']
            for name in names:
                self.assertFalse(
                    name.startswith('convoy_'),
                    'convoy_client.py cannot import %r -- dev/convoy is '
                    'not on the path of a released .tox' % (name,))

    def test_the_source_is_ascii(self):
        """Repo rule: ASCII punctuation only -- a raw em-dash mojibakes
        in a TD textport."""
        try:
            self.source.encode('ascii')
        except UnicodeEncodeError as e:
            raise AssertionError('non-ASCII at offset %s: %r'
                                 % (e.start, self.source[e.start:e.end]))

    def test_it_loads_with_no_td_globals_present(self):
        """The real proof: exec the module in a namespace that has no op,
        no parent, no project."""
        spec = importlib.util.spec_from_file_location('convoy_client_probe',
                                                      _CLIENT_PATH)
        fresh = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fresh)
        self.assertTrue(callable(fresh.probe))
        self.assertTrue(callable(fresh.status_text))

    def test_the_public_surface_the_plan_names_exists(self):
        for name in ('data_dir', 'read_live_portfile', 'pid_is_alive',
                     'probe', 'register', 'unregister', 'mint_runtime_id',
                     'stable_node_discriminator', 'backoff_delay',
                     'status_text'):
            self.assertTrue(callable(getattr(client, name, None)),
                            'missing public function %r' % (name,))


class TestConvoyHostRepairVocabulary(EmbodyTestCase):
    """The runtime-only repair had no words of its own.

    plan_install's repair_runtime action writes NO payload -- it
    re-resolves the interpreter and rewrites the supervisor definition,
    leaving the installed version and file list verbatim -- but the
    extension reported HOST_INSTALLING while it ran, so the field read
    'Installing...'. On the one path reached BECAUSE the version may not
    be replaced, that promises a version change that cannot happen.
    """

    def test_repairing_has_its_own_string(self):
        self.assertEqual(client.host_status_text(client.HOST_REPAIRING),
                         'Repairing runtime...')

    def test_it_is_not_the_installing_string(self):
        self.assertNotEqual(
            client.host_status_text(client.HOST_REPAIRING),
            client.host_status_text(client.HOST_INSTALLING),
            'a repair that reads exactly like an install is the defect')

    def test_it_did_not_fall_through_to_the_default(self):
        """A transient name convoy_client does not know reads
        'Install failed -- see log'. That is the failure mode a new
        constant introduces if the vocabulary is not extended with it."""
        self.assertNotEqual(client.host_status_text(client.HOST_REPAIRING),
                            'Install failed -- see log')

    def test_every_transient_string_is_distinct_and_ascii(self):
        seen = {}
        for name in ('HOST_CHECKING', 'HOST_INSTALLING', 'HOST_REPAIRING',
                     'HOST_STARTING', 'HOST_INSTALL_FAILED',
                     'HOST_STALE_PAYLOAD'):
            text = client.host_status_text(getattr(client, name))
            text.encode('ascii')
            for glyph in ('\u2014', '\u2013', '\u2026', '\u2019'):
                self.assertNotIn(glyph, text)
            if name != 'HOST_INSTALL_FAILED':
                self.assertNotIn(
                    text, seen,
                    '%s and %s both read %r' % (name, seen.get(text), text))
            seen[text] = name


class TestConvoyStalePayloadVocabulary(EmbodyTestCase):
    """An install that landed on disk while the DAEMON kept serving the
    previous payload had no words at all -- only a textport WARNING naming
    a button, which is exactly how a user misses it (field report,
    2026-08-16). It now has a resting state, and the lead words matter as
    much as the rest.
    """

    def _text(self, **over):
        state = {'state': client.HOST_STALE_PAYLOAD,
                 'installed_version': '6.0.246',
                 'reported_version': '6.0.241', 'live': True}
        state.update(over)
        return client.host_status_text(state)

    def test_it_did_not_fall_through_to_the_default(self):
        self.assertNotEqual(self._text(), 'Install failed -- see log')

    def test_it_leads_with_needs_repair(self):
        """The prefix is load-bearing twice over: ConvoyExt's
        _BLOCKING_HOST_TEXTS promotes it onto Status, and
        startup_progress.convoy_step classifies it FAILED. Lead with
        'Running' and the defect hides behind a green mark."""
        self.assertTrue(self._text().startswith('Needs repair'), self._text())

    def test_it_names_both_versions_and_the_remedy(self):
        text = self._text()
        self.assertIn('6.0.241', text)
        self.assertIn('6.0.246', text)
        self.assertIn('Repair Convoy App', text)

    def test_it_degrades_honestly_when_a_version_is_unknown(self):
        text = self._text(reported_version=None, installed_version='')
        text.encode('ascii')
        self.assertTrue(text.startswith('Needs repair'), text)
