"""Tests for the TDN clipboard auto-paste watcher (TDNExt._clipboardWatchPoll).

The watcher polls ui.clipboard; when a NEW _embody_tdn envelope appears it offers
(via the Embody message box) to "embody it" into the current network as a new
COMP. No keyboard shortcut -- TD's native Cmd/Ctrl+V paste can't be suppressed.

These drive the poll directly with a monkeypatched message box (no real modal)
and a controlled clipboard, restoring both. Each test body is a single atomic
main-thread call, and the live watcher param is disabled in setUp, so the
background loop can never race the clipboard. Extensions referenced inline.
"""

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase


class TestClipboardWatch(EmbodyTestCase):

    def setUp(self):
        super().setUp()
        self._orig_clip = ui.clipboard
        self._orig_param = int(op.Embody.par.Clipboardautopaste.eval())
        op.Embody.par.Clipboardautopaste = 0          # quiet the live loop
        # The watcher only prompts while TD is the active window; headless tests have
        # no rollover, so force the gate open. The gate test overrides this.
        op.Embody.ext.TDN._tdWindowActive = lambda: True

    def tearDown(self):
        ui.clipboard = self._orig_clip
        op.Embody.par.Clipboardautopaste = self._orig_param
        op.Embody.ext.TDN._clip_last_sig = (len(self._orig_clip or ''),
                                            hash(self._orig_clip or ''))
        try:
            del op.Embody.ext.Embody._messageBox
        except Exception:
            pass
        try:
            del op.Embody.ext.TDN._tdWindowActive
        except Exception:
            pass
        if self.sandbox.op('cw_probe'):
            self.sandbox.op('cw_probe').destroy()
        super().tearDown()

    def _put_envelope(self):
        probe = self.sandbox.create(baseCOMP, 'cw_probe')
        probe.create(constantCHOP, 'c1')
        op.Embody.ext.TDN.CopyNetworkToClipboard(probe)

    def test_param_exists(self):
        # The Clipboardautopaste toggle must be a real (persisted) custom par.
        self.assertTrue(hasattr(op.Embody.par, 'Clipboardautopaste'))

    def test_detects_envelope(self):
        self._put_envelope()
        self.assertTrue(op.Embody.ext.TDN.ClipboardHasNetwork())

    def test_offswitch_no_prompt(self):
        self._put_envelope()
        calls = []
        op.Embody.ext.Embody._messageBox = lambda *a, **k: (calls.append(1), 1)[1]
        op.Embody.par.Clipboardautopaste = 0
        op.Embody.ext.TDN._clip_last_sig = None
        op.Embody.ext.TDN._clipboardWatchPoll()
        self.assertEqual(len(calls), 0, 'param off -> no prompt')

    def test_prompts_then_debounces(self):
        self._put_envelope()
        calls = []
        op.Embody.ext.Embody._messageBox = lambda *a, **k: (calls.append(a[0]), 1)[1]
        op.Embody.par.Clipboardautopaste = 1
        op.Embody.ext.TDN._clip_last_sig = None
        op.Embody.ext.TDN._clipboardWatchPoll()
        self.assertEqual(len(calls), 1, 'new envelope -> one prompt')
        self.assertIn('TDN', calls[0])
        op.Embody.ext.TDN._clipboardWatchPoll()          # same clipboard
        self.assertEqual(len(calls), 1, 'dismiss debounce -> no re-prompt')

    def test_non_envelope_no_prompt(self):
        ui.clipboard = 'just some random text, not a TDN at all'
        calls = []
        op.Embody.ext.Embody._messageBox = lambda *a, **k: (calls.append(1), 1)[1]
        op.Embody.par.Clipboardautopaste = 1
        op.Embody.ext.TDN._clip_last_sig = None
        op.Embody.ext.TDN._clipboardWatchPoll()
        self.assertEqual(len(calls), 0, 'non-envelope clipboard -> no prompt')

    def test_inactive_window_suppresses_then_prompts_on_return(self):
        # While TD is not the active window the prompt is withheld AND the clipboard
        # signature is left unrecorded, so when the user returns to TD the CURRENT
        # clipboard prompts (if they copied a different specimen, the newer one wins).
        self._put_envelope()
        calls = []
        op.Embody.ext.Embody._messageBox = lambda *a, **k: (calls.append(1), 1)[1]
        op.Embody.par.Clipboardautopaste = 1
        op.Embody.ext.TDN._clip_last_sig = None
        op.Embody.ext.TDN._tdWindowActive = lambda: False      # TD in the background
        op.Embody.ext.TDN._clipboardWatchPoll()
        self.assertEqual(len(calls), 0, 'inactive window -> no prompt')
        self.assertIsNone(op.Embody.ext.TDN._clip_last_sig, 'inactive -> sig left unrecorded')
        op.Embody.ext.TDN._tdWindowActive = lambda: True       # user returns to TD
        op.Embody.ext.TDN._clipboardWatchPoll()
        self.assertEqual(len(calls), 1, 'back in TD -> prompts the current clipboard')
