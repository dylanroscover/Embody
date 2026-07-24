"""
Test suite: chunked TDN export progress + cancellation (TDNExt).

Covers the progress/cancel additions to the async export state machine
(added 2026-07-24): the CancelExport request path, the progress-dialog
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
        self.tdn = self.embody.ext.TDN

    def tearDown(self):
        # Never leave a fake export state behind -- it would make the next
        # real ExportNetworkAsync refuse ("export already in progress").
        self.tdn._export_state = None
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

    # --- CancelExport ---

    def test_cancel_export_noop_when_idle(self):
        """CancelExport with nothing running must not raise."""
        self.tdn._export_state = None
        self.tdn.CancelExport()  # must be a safe no-op

    def test_cancel_export_sets_flag(self):
        """CancelExport flips the running export's cancel flag."""
        self.tdn._export_state = self._fakeState()
        self.tdn.CancelExport()
        self.assertTrue(self.tdn._export_state['cancel'],
            'CancelExport must set cancel=True so the next batch aborts')

    def test_cancel_export_ignores_done_export(self):
        """A finished export is not re-cancelled."""
        state = self._fakeState()
        state['done'] = True
        self.tdn._export_state = state
        self.tdn.CancelExport()
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
        thr = self.tdn.EXPORT_PROGRESS_THRESHOLD
        self.assertIsInstance(thr, int)
        self.assertGreater(thr, 0)
