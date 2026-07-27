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
        # Setting the par to 0 is NOT enough to quiet the watcher: the tests
        # below set it back to 1, which re-opens the gate for the LIVE
        # self-rescheduling tick (_clipboardWatchTick, every 1500ms) as well
        # as for the test's own explicit polls. A live tick landing between
        # two of a test's polls consumes the clipboard signature at whichever
        # gate rejects it, and the test's own poll then debounces and never
        # prompts -- surfacing as an intermittent
        # "0 != 1 : back in TD -> prompts the current clipboard" that only
        # ever reproduced in full runs (observed 2026-07-26 and again
        # 2026-07-27). Orphan the live loop for the duration of the suite by
        # bumping the generation it is guarded on; tearDown starts a fresh
        # one. Now the ONLY polls are the ones each test makes explicitly.
        self._saved_gen = op.Embody.fetch('_clip_watch_gen', 0)
        op.Embody.store('_clip_watch_gen', self._saved_gen + 1)
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
        # Restart the live watcher the suite orphaned in setUp, mirroring the
        # product's own kick in TDNExt.onInitTD. Without this the clipboard
        # auto-paste feature would stay dead in the running session until the
        # next extension reinit.
        try:
            gen = op.Embody.fetch('_clip_watch_gen', 0) + 1
            op.Embody.store('_clip_watch_gen', gen)
            run("o = op(%r)\nif o and o.valid: o.ext.TDN._clipboardWatchTick(%d)"
                % (op.Embody.path, gen),
                fromOP=op.Embody, delayMilliSeconds=1500)
        except Exception:
            pass
        super().tearDown()

    def _put_envelope(self):
        """Copy a probe COMP's TDN to the clipboard, VERIFYING it stuck.

        CopyNetworkToClipboard writes the real OS clipboard, which every
        process on the machine shares -- another app, clipboard history, or
        a developer's own tooling can clobber it between this write and the
        `ui.clipboard` read inside _clipboardWatchPoll. That race made this
        suite fail intermittently in full runs while passing in isolation
        (observed 2026-07-26 as a bare "0 != 1 : back in TD -> prompts the
        current clipboard", which looks like a watcher bug and says nothing
        about the real cause).

        Retry briefly, then fail with the ACTUAL reason. A narrow race
        remains between returning here and the read under test; if that ever
        fires, trust this message over the assertion that follows.

        The check is a RAW marker substring, deliberately not
        ClipboardHasNetwork() -- that is the function under test in
        test_detects_envelope, and verifying with it would report a genuine
        parser bug as an "environment problem".
        """
        probe = self.sandbox.op('cw_probe')
        if not probe:
            probe = self.sandbox.create(baseCOMP, 'cw_probe')
            probe.create(constantCHOP, 'c1')
        marker = op.Embody.op('TDNExt').module.EMBODY_TDN_MARKER
        op.Embody.ext.TDN.CopyNetworkToClipboard(probe)
        self.requireClipboardHolds(
            lambda raw: marker in raw,
            what='a TDN envelope',
            reseed=lambda: op.Embody.ext.TDN.CopyNetworkToClipboard(probe))

    def _set_clipboard(self, text):
        """Write the system clipboard and VERIFY the write stuck.

        Same contended-OS-resource problem as _put_envelope above; the
        shared helper retries with backoff and skips (never fails) when
        another process is holding the clipboard.
        """
        self.seedClipboard(text)

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
        self._set_clipboard('just some random text, not a TDN at all')
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
        # Report every gate _clipboardWatchPoll checks. A bare "0 != 1" here
        # says nothing about WHICH gate closed, and cost two wrong hypotheses
        # (a clipboard-seed race, then the pane gate) before the real cause --
        # the live watcher tick eating the signature -- was found.
        marker = op.Embody.op('TDNExt').module.EMBODY_TDN_MARKER
        pane = ui.panes.current
        owner = pane.owner if pane else None
        self.assertEqual(
            len(calls), 1,
            'back in TD -> prompts the current clipboard; gates at failure: '
            'autopaste=%s performmode=%s clip_has_marker=%s clip_len=%d '
            'last_sig=%r window_active=%s pane_owner=%r owner_isCOMP=%s'
            % (op.Embody.par.Clipboardautopaste.eval(),
               op.Embody.par.Performmode.eval(),
               marker in (ui.clipboard or ''), len(ui.clipboard or ''),
               op.Embody.ext.TDN._clip_last_sig,
               op.Embody.ext.TDN._tdWindowActive(),
               owner.path if owner else None,
               bool(owner and owner.isCOMP)))

    def test_outbound_copy_does_not_prompt(self):
        # Ctrl+Shift+C copies a COMP's TDN to the clipboard (OUTBOUND -- to share or
        # paste elsewhere). The watcher must NOT turn around and offer to paste our
        # own export back in: CopyNetworkToClipboard seeds _clip_last_sig with what it
        # just wrote, so the next poll sees no NEW (inbound) content. This is the
        # outbound-vs-inbound fix -- note the sig is left exactly as the copy set it.
        op.Embody.ext.TDN._clip_last_sig = None
        self._put_envelope()                                   # outbound copy
        self.assertIsNotNone(op.Embody.ext.TDN._clip_last_sig,
                             'outbound copy must seed the watcher signature')
        calls = []
        op.Embody.ext.Embody._messageBox = lambda *a, **k: (calls.append(1), 1)[1]
        op.Embody.par.Clipboardautopaste = 1
        op.Embody.ext.TDN._clipboardWatchPoll()                # sig NOT cleared
        self.assertEqual(len(calls), 0,
                         'outbound copy -> watcher must not prompt to re-import')

    def test_inbound_after_outbound_still_prompts(self):
        # Suppression is content-specific, not a blanket mute: after an outbound copy
        # (sig = our export), a DIFFERENT TDN landing on the clipboard (the web
        # "embody it" button, a foreign envelope) is genuinely inbound -- a different
        # string -> a different sig -> it still prompts.
        self._put_envelope()                                   # outbound copy of cw_probe
        m = op.Embody.op('TDNExt').module
        foreign = m.wrap_tdn(
            {'format': 'tdn', 'version': '2.0', 'network_path': '/x/foreign',
             'operators': [{'name': 'n', 'type': 'noiseTOP'}]},
            source='embody', slug='foreign')
        self._set_clipboard(m.to_clipboard_str(foreign))       # inbound -- NOT via CopyNetwork
        calls = []
        op.Embody.ext.Embody._messageBox = lambda *a, **k: (calls.append(a[0]), 1)[1]
        op.Embody.par.Clipboardautopaste = 1
        op.Embody.ext.TDN._clipboardWatchPoll()
        self.assertEqual(len(calls), 1,
                         'a different (inbound) TDN after an outbound copy still prompts')
