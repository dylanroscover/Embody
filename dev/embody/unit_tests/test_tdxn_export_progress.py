"""
Test suite: chunked TDN export progress + cancellation (TDXNExt).

Covers the progress/cancel additions to the async export state machine
(added 2026-07-24): the cancelExport request path, the progress-dialog
helpers' graceful guards, and the auto-show threshold. These exercise the
pure state/guard logic WITHOUT spawning the Thread Manager worker (a real
ExportNetworkAsync run advances frames and mutates the live network, which
does not belong in the synchronous test harness).
"""

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase


class TestTDNExportProgress(EmbodyTestCase):

    def setUp(self):
        super().setUp()
        self.tdn = self.embody.ext.TDXN

    def tearDown(self):
        # Never leave a fake export state behind -- it would make the next
        # real ExportNetworkAsync refuse ("export already in progress").
        self.tdn._export_state = None
        # Reset the dialog's transient UI state. These are ordinary
        # parameters on COMPs INSIDE Embody, so anything left here is
        # captured by Embody's own .tdn export and committed:
        # test_update_progress_guarded writes a sandbox-named label, and
        # 'sandbox_test_tdn_export_progress -- 400 / 1,000 operators (40%)'
        # sat in the repository across several releases because of it.
        # (_closeExportProgress resets these too, but this suite drives
        # _updateExportProgress directly and never closes.)
        try:
            dlg = self.embody.op('tdn_export_progress')
            if dlg:
                status = dlg.op('dialog/status')
                if status:
                    status.par.text = status.par.text.default
                fill = dlg.op('dialog/bar_bg/bar_fill')
                if fill:
                    fill.par.w = fill.par.w.default
        except Exception:
            pass
        super().tearDown()

    def _fakeState(self, total=1000, index=400):
        return {
            'paths': ['/p%d' % i for i in range(total)],
            'index': index,
            'batch_size': 200,
            'cancel': False,
            'root_path': self.sandbox.path,
            'done': False,
        }

    # --- cancelExport ---

    def test_cancel_export_noop_when_idle(self):
        """cancelExport with nothing running must not raise."""
        self.tdn._export_state = None
        self.tdn.cancelExport()  # must be a safe no-op

    def test_cancel_export_sets_flag(self):
        """cancelExport flips the running export's cancel flag."""
        self.tdn._export_state = self._fakeState()
        self.tdn.cancelExport()
        self.assertTrue(self.tdn._export_state['cancel'],
            'cancelExport must set cancel=True so the next batch aborts')

    def test_cancel_export_ignores_done_export(self):
        """A finished export is not re-cancelled."""
        state = self._fakeState()
        state['done'] = True
        self.tdn._export_state = state
        self.tdn.cancelExport()
        self.assertFalse(state['cancel'],
            'A done export must not be flagged for cancellation')

    # --- Progress dialog guards (must never break an export) ---

    def test_update_progress_guarded(self):
        """_updateExportProgress tolerates being called with live state."""
        self.tdn._export_state = self._fakeState()
        # Must not raise whether or not the dialog COMP is present.
        self.tdn._updateExportProgress(self.tdn._export_state)

    def test_close_progress_guarded(self):
        """_closeExportProgress never raises when no export/dialog active."""
        self.tdn._closeExportProgress()

    # --- Auto-show threshold ---

    def test_progress_threshold_is_positive_int(self):
        thr = self.tdn._EXPORT_PROGRESS_THRESHOLD
        self.assertIsInstance(thr, int)
        self.assertGreater(thr, 0)
