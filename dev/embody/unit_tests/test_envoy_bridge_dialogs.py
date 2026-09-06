"""
Test suite: list_dialogs / dismiss_dialog bridge meta-tools.

TouchDesigner's blocking dialogs are TD-drawn windows owned by the main
window; the bridge finds them by ownership (or the #32770 class), dismisses
them with a close -> escape -> enter ladder and verifies each rung by the
window disappearing, and never targets the main window. The logic runs
against an injected fake backend here; one Windows-only test drives a REAL
Win32 message box in a child python process (never TouchDesigner).
"""

import importlib.util
import os
import struct
import subprocess
import sys
import tempfile
import time
import zlib
from unittest.mock import patch

_bridge_path = os.path.join(project.folder, 'embody', 'envoy_bridge.py')
_spec = importlib.util.spec_from_file_location('envoy_bridge_dialogs', _bridge_path)
bridge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bridge)
bridge.start_orphan_watchdog = lambda *args, **kwargs: None
if hasattr(bridge, 'start_reconciler'):
    bridge.start_reconciler = lambda *args, **kwargs: None

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase

MAIN = {'id': 100, 'title': 'TouchDesigner 2025.33070: C:/x/Show.toe', 'class': 'TouchDesigner Window', 'owner': 0, 'visible': True, 'enabled': True}
BOX = {'id': 200, 'title': 'Missing file', 'class': 'TouchDesigner Window', 'owner': 100, 'visible': True, 'enabled': True}
STD = {'id': 300, 'title': 'Open', 'class': '#32770', 'owner': 0, 'visible': True, 'enabled': True}
HIDDEN = {'id': 400, 'title': 'ghost', 'class': 'TouchDesigner Window', 'owner': 100, 'visible': False, 'enabled': True}


def _pid_of_window_titled(title):
    """Owning pid of the first visible top-level window with this exact title."""
    import ctypes
    import ctypes.wintypes as wt
    u = ctypes.windll.user32
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def cb(h, _):
        if u.IsWindowVisible(h):
            n = u.GetWindowTextLengthW(h) + 1
            buf = ctypes.create_unicode_buffer(n)
            u.GetWindowTextW(h, buf, n)
            if buf.value == title:
                pid = wt.DWORD()
                u.GetWindowThreadProcessId(h, ctypes.byref(pid))
                found.append(int(pid.value))
                return False
        return True
    u.EnumWindows(cb, 0)
    return found[0] if found else None


def _state(pid=4242, instances=None):
    st = bridge.BridgeState(url='http://127.0.0.1:9870/mcp', td_pid=pid)
    st.pinned_instance = 'Dev'
    st.config = {'active': 'Dev', 'instances': dict(instances or {'Dev': {'port': 9870, 'td_pid': pid}})}
    return st


class TestDialogClassification(EmbodyTestCase):

    def test_owned_and_standard_windows_are_dialogs(self):
        dialogs, main = bridge.classify_windows([MAIN, BOX, STD, HIDDEN])
        self.assertEqual([d['id'] for d in dialogs], [200, 300])
        self.assertEqual(main['id'], 100)

    def test_no_windows(self):
        self.assertEqual(bridge.classify_windows([]), ([], None))
        self.assertEqual(bridge.classify_windows(None), ([], None))

    def test_schemas_registered(self):
        names = {t['name'] for t in bridge.BRIDGE_TOOLS}
        self.assertIn('list_dialogs', names)
        self.assertIn('dismiss_dialog', names)
        self.assertIn('list_dialogs', bridge.BRIDGE_TOOL_NAMES)
        dismiss = next(t for t in bridge.BRIDGE_TOOLS if t['name'] == 'dismiss_dialog')
        self.assertEqual(dismiss['inputSchema']['properties']['action']['enum'],
                         ['auto', 'close', 'escape', 'enter'])


class TestListDialogs(EmbodyTestCase):

    def setUp(self):
        super().setUp()
        self.alive = patch.object(bridge, 'is_td_process_alive', return_value=True)
        self.alive.start()

    def tearDown(self):
        self.alive.stop()
        super().tearDown()

    def test_lists_dialogs_and_main(self):
        fake = bridge._FakeDialogBackend([MAIN, BOX])
        r = bridge.handle_list_dialogs({}, _state(), backend=fake)
        self.assertTrue(r['ok'])
        self.assertTrue(r['blocked'])
        self.assertEqual(r['pid'], 4242)
        self.assertEqual(r['instance'], 'Dev')
        self.assertEqual([d['title'] for d in r['dialogs']], ['Missing file'])
        self.assertStartsWith(r['main_window'], 'TouchDesigner 2025')

    def test_no_dialogs_is_not_blocked(self):
        r = bridge.handle_list_dialogs({}, _state(), backend=bridge._FakeDialogBackend([MAIN]))
        self.assertTrue(r['ok'])
        self.assertFalse(r['blocked'])
        self.assertEqual(r['dialogs'], [])

    def test_screenshot_paths_ride_along(self):
        fake = bridge._FakeDialogBackend([MAIN, BOX])
        r = bridge.handle_list_dialogs({'screenshot': True}, _state(), backend=fake)
        self.assertLen(fake.shots, 1)
        self.assertTrue(r['dialogs'][0]['screenshot'].endswith('.png'))
        self.assertIn('Read each screenshot', r['hint'])
        os.remove(r['dialogs'][0]['screenshot'])

    def test_instance_param_resolves_through_registry(self):
        fake = bridge._FakeDialogBackend([dict(BOX, pid=777)])
        st = _state(instances={'Dev': {'port': 9870, 'td_pid': 4242}, 'Show': {'port': 9871, 'td_pid': 777}})
        r = bridge.handle_list_dialogs({'instance': 'Show'}, st, backend=fake)
        self.assertEqual(r['pid'], 777)
        self.assertEqual(r['instance'], 'Show')
        self.assertTrue(r['blocked'])

    def test_unknown_instance(self):
        r = bridge.handle_list_dialogs({'instance': 'Nope'}, _state(), backend=bridge._FakeDialogBackend())
        self.assertFalse(r['ok'])
        self.assertEqual(r['error_code'], 'envoy.dialog.unknown_instance')
        self.assertEqual(r['available'], ['Dev'])

    def test_unsupported_platform_is_a_named_error(self):
        r = bridge.handle_list_dialogs({}, _state(), backend=bridge._UnsupportedDialogBackend())
        self.assertFalse(r['ok'])
        self.assertEqual(r['error_code'], 'envoy.dialog.unsupported')

    def test_no_live_process(self):
        with patch.object(bridge, 'is_td_process_alive', return_value=False):
            r = bridge.handle_list_dialogs({}, _state(), backend=bridge._FakeDialogBackend([BOX]))
        self.assertEqual(r['error_code'], 'envoy.dialog.no_process')

    def test_dispatched_through_handle_bridge_tool(self):
        fake = bridge._FakeDialogBackend([MAIN, BOX])
        with patch.object(bridge, 'dialog_backend', return_value=fake):
            content = bridge.handle_bridge_tool('list_dialogs', {}, _state())
        self.assertEqual(content[0]['type'], 'text')
        self.assertIn('"blocked": true', content[0]['text'])


class TestDismissDialog(EmbodyTestCase):

    def setUp(self):
        super().setUp()
        self.alive = patch.object(bridge, 'is_td_process_alive', return_value=True)
        self.alive.start()
        self.no_sleep = lambda s: None

    def tearDown(self):
        self.alive.stop()
        super().tearDown()

    def test_ladder_stops_at_the_rung_that_worked(self):
        fake = bridge._FakeDialogBackend([MAIN, BOX], gone_after='escape')
        r = bridge.handle_dismiss_dialog({}, _state(), backend=fake, sleep=self.no_sleep)
        self.assertTrue(r['ok'])
        self.assertEqual(r['dismissed_by'], 'escape')
        self.assertEqual(r['tried'], ['close', 'escape'])
        self.assertEqual([a for _, a in fake.actions], ['close', 'escape'])
        self.assertEqual(r['remaining'], [])
        self.assertEqual(r['dialog']['title'], 'Missing file')

    def test_close_is_the_first_rung(self):
        fake = bridge._FakeDialogBackend([MAIN, BOX], gone_after='close')
        r = bridge.handle_dismiss_dialog({}, _state(), backend=fake, sleep=self.no_sleep)
        self.assertEqual(r['tried'], ['close'])
        self.assertEqual(r['dismissed_by'], 'close')

    def test_stuck_dialog_reports_every_rung_and_fails_loud(self):
        fake = bridge._FakeDialogBackend([MAIN, BOX], gone_after=None)
        r = bridge.handle_dismiss_dialog({}, _state(), backend=fake, sleep=self.no_sleep)
        self.assertFalse(r['ok'])
        self.assertEqual(r['error_code'], 'envoy.dialog.stuck')
        self.assertEqual(r['tried'], ['close', 'escape', 'enter'])
        self.assertEqual([d['id'] for d in r['remaining']], [200])

    def test_single_action_tries_only_that_rung(self):
        fake = bridge._FakeDialogBackend([MAIN, BOX], gone_after='close')
        r = bridge.handle_dismiss_dialog({'action': 'enter'}, _state(), backend=fake, sleep=self.no_sleep)
        self.assertEqual(r['tried'], ['enter'])
        self.assertFalse(r['ok'])

    def test_bad_action(self):
        r = bridge.handle_dismiss_dialog({'action': 'nuke'}, _state(),
                                         backend=bridge._FakeDialogBackend([MAIN, BOX]), sleep=self.no_sleep)
        self.assertEqual(r['error_code'], 'envoy.dialog.bad_action')

    def test_select_by_title_substring_and_by_id(self):
        second = dict(BOX, id=201, title='Save changes?')
        fake = bridge._FakeDialogBackend([MAIN, BOX, second], gone_after='close')
        r = bridge.handle_dismiss_dialog({'dialog': 'save'}, _state(), backend=fake, sleep=self.no_sleep)
        self.assertEqual(r['dialog']['id'], 201)
        fake = bridge._FakeDialogBackend([MAIN, BOX, second], gone_after='close')
        r = bridge.handle_dismiss_dialog({'dialog': '200'}, _state(), backend=fake, sleep=self.no_sleep)
        self.assertEqual(r['dialog']['id'], 200)

    def test_main_window_is_refused(self):
        fake = bridge._FakeDialogBackend([MAIN, BOX])
        r = bridge.handle_dismiss_dialog({'dialog': 'TouchDesigner 2025'}, _state(), backend=fake, sleep=self.no_sleep)
        self.assertFalse(r['ok'])
        self.assertEqual(r['error_code'], 'envoy.dialog.main_window_refused')
        self.assertEqual(fake.actions, [], 'nothing may be posted to the main window')

    def test_unknown_title(self):
        fake = bridge._FakeDialogBackend([MAIN, BOX])
        r = bridge.handle_dismiss_dialog({'dialog': 'license'}, _state(), backend=fake, sleep=self.no_sleep)
        self.assertEqual(r['error_code'], 'envoy.dialog.not_found')
        self.assertEqual([d['title'] for d in r['dialogs']], ['Missing file'])

    def test_no_dialog_open(self):
        r = bridge.handle_dismiss_dialog({}, _state(), backend=bridge._FakeDialogBackend([MAIN]), sleep=self.no_sleep)
        self.assertEqual(r['error_code'], 'envoy.dialog.none')
        self.assertFalse(r['blocked'])


class TestDialogScreenshotEncoder(EmbodyTestCase):

    def test_png_roundtrip_shape(self):
        w, h = 3, 2
        rows = [bytes([10, 20, 30, 255] * w), bytes([40, 50, 60, 255] * w)]
        png = bridge._png_encode(w, h, rows)
        self.assertTrue(png.startswith(b'\x89PNG\r\n\x1a\n'))
        # IHDR: width, height, bit depth 8, colour type 6 (RGBA)
        self.assertEqual(struct.unpack('>IIBB', png[16:26]), (w, h, 8, 6))
        idat_at = png.index(b'IDAT')
        length = struct.unpack('>I', png[idat_at - 4:idat_at])[0]
        raw = zlib.decompress(png[idat_at + 4:idat_at + 4 + length])
        self.assertEqual(len(raw), h * (1 + w * 4))
        # BGRA in -> RGBA out, filter byte 0 per row
        self.assertEqual(raw[:5], bytes([0, 30, 20, 10, 255]))


class TestDialogsLiveWin32(EmbodyTestCase):
    """A REAL Win32 message box in a child python process (never
    TouchDesigner): the backend must find it by class and close it."""

    def setUp(self):
        super().setUp()
        if not sys.platform.startswith('win'):
            self.skipTest('Win32 backend only')
        if 'td' in sys.modules:
            # Inside TouchDesigner sys.executable IS TouchDesigner.exe, so the
            # child would be a second TD instance. The pytest tier owns this.
            self.skipTest('runs under the pytest tier, not inside TouchDesigner')

    def test_real_message_box_is_listed_and_dismissed(self):
        child = subprocess.Popen(
            [sys.executable, '-c',
             "import ctypes; ctypes.windll.user32.MessageBoxW(0, 'from a test', 'Envoy dialog test', 0)"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            backend = bridge._Win32DialogBackend()
            # A venv python.exe is a launcher that runs the real interpreter as
            # a child, so the box belongs to a grandchild pid: find it by its
            # unique title and take the owning pid from the window itself.
            box_pid = None
            deadline = time.monotonic() + 8.0
            while time.monotonic() < deadline and box_pid is None:
                box_pid = _pid_of_window_titled('Envoy dialog test')
                if box_pid is None:
                    time.sleep(0.1)
            self.assertIsNotNone(box_pid, 'the child process never showed its message box')
            dialogs, _ = bridge.classify_windows(backend.enumerate(box_pid))
            self.assertTrue(dialogs)
            self.assertEqual(dialogs[0]['class'], '#32770')
            self.assertEqual(dialogs[0]['title'], 'Envoy dialog test')

            st = _state(pid=box_pid)
            with patch.object(bridge, 'is_td_process_alive', return_value=True):
                listed = bridge.handle_list_dialogs({'screenshot': True}, st, backend=backend)
                self.assertTrue(listed['blocked'])
                shot = listed['dialogs'][0].get('screenshot')
                self.assertTrue(shot and os.path.getsize(shot) > 100, 'screenshot PNG written')
                os.remove(shot)
                r = bridge.handle_dismiss_dialog({}, st, backend=backend)
            self.assertTrue(r['ok'], r)
            self.assertEqual(r['dismissed_by'], 'close')
            self.assertEqual(child.wait(timeout=5), 0)
        finally:
            if child.poll() is None:
                child.kill()
