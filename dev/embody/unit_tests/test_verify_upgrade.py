"""
Test suite: Upgrade-path validation (the removed Skip/Re-scan dialog).

Regression guard for the upgrade freeze: the old Verify() upgrade branch
showed a Skip/Re-scan dialog whose 'Re-scan' button called Reset() ->
Disable() -- synchronously unlinking EVERY tracked file (bypassing the
Filecleanup preference), clearing the externalizations table, then
re-exporting the whole project in one main-thread frame: a minutes-long
freeze and a crash window with zero files on disk. The upgrade path now
validates quietly via _validateTrackedOperators(), which must stay
non-destructive: no file deletion, no table clear, no Status flip, no
dialog.

The helper defers UpdateHandler() by 10 frames; these tests assert the
SYNCHRONOUS contract only (everything the old code broke synchronously in
the click frame). Embody's own externaltox par is snapshotted in setUp and
restored in tearDown.

ORDERING NOTE: the deferred UpdateHandler() outlives this suite and lands
~10 frames later. Today this suite sorts near the end of a full run, so
the deferred Update() fires on a quiescent project after Filecleanup is
restored (identical to what production reset() always scheduled). If a
future suite sorts alphabetically AFTER test_verify_upgrade and mutates
tags on live tracked ops, be aware that this deferred sweep may interleave
with it.
"""

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase


class TestVerifyUpgradePath(EmbodyTestCase):

    def setUp(self):
        self._prev_externaltox = str(self.embody.par.externaltox.eval())

    def tearDown(self):
        if self._prev_externaltox:
            self.embody.par.externaltox = self._prev_externaltox
        super().tearDown()

    def test_validation_is_non_destructive(self):
        """_validateTrackedOperators deletes nothing, clears nothing."""
        ext = self.embody_ext
        table = ext.Externalizations
        self.assertIsNotNone(
            table, 'dev project must have an externalizations table')
        rows_before = table.numRows
        self.assertGreater(
            rows_before, 1,
            'dev project must have tracked rows for this test to be meaningful')
        status_before = str(self.embody.par.Status.eval())
        existing_before = {
            p for p in ext.getTrackedFilePaths() if p.is_file()}
        self.assertGreater(
            len(existing_before), 0,
            'dev project must have tracked files on disk')

        ext._validateTrackedOperators()

        self.assertEqual(
            table.numRows, rows_before,
            'validation must not add or remove table rows synchronously')
        self.assertEqual(
            str(self.embody.par.Status.eval()), status_before,
            'validation must not change Status')
        still_existing = {p for p in existing_before if p.is_file()}
        self.assertEqual(
            still_existing, existing_before,
            'validation must not delete any tracked file')
        self.assertEqual(
            self.embody.par.externaltox.eval(), '',
            "validation must clear Embody's own externaltox (drag-source path)")

    def test_rescan_dialog_removed(self):
        """The Skip/Re-scan dialog must never come back to Verify()."""
        src = self.embody.op('EmbodyExt').text
        self.assertNotIn(
            "buttons=['Skip', 'Re-scan']", src,
            'the destructive one-click Re-scan dialog must stay removed')
        self.assertIn(
            '_validateTrackedOperators', src,
            'the quiet validation helper must exist in EmbodyExt')

    def test_validation_helper_stays_quiet(self):
        """The helper body must contain no dialog and no Reset call.

        Slices the helper's source out of the EmbodyExt DAT text (DAT-backed
        modules have no file, so inspect.getsource is unreliable) and asserts
        the destructive/dialog primitives are absent from the BODY -- the
        docstring is stripped first so prose mentioning them stays legal.
        """
        src = self.embody.op('EmbodyExt').text
        start = src.index('def _validateTrackedOperators')
        end = src.index('\n    def ', start + 1)
        body = src[start:end]
        doc_open = body.index('"""')
        doc_close = body.index('"""', doc_open + 3)
        body = body[:doc_open] + body[doc_close + 3:]
        for forbidden in ('_messageBox', 'ui.messageBox', 'self.Reset(',
                          'Disable('):
            self.assertNotIn(
                forbidden, body,
                f'{forbidden} must never appear in _validateTrackedOperators')
