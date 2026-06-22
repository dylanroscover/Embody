"""
Regression guard for the Envoy onboarding-dialog leak during testing.

The "Enable Envoy?" onboarding modal (EmbodyExt._promptEnvoy -> _messageBox ->
ui.messageBox) used to escape as a real modal during automated suites and freeze
TD. The original suppression keyed off the consumable _smoke_test_responses dict:
once a test drained/unstored it, a later prompt (from an Envoy restart / reinit)
hit the empty store and opened a real modal -- the "drain-then-fire" leak.

The fix gates _messageBox on the test runner's _running flag (via
EmbodyExt._testRunnerActive()), which is decoupled from _smoke_test_responses and
stays True for the WHOLE run. These tests force the exact leak scenario INSIDE a
live run and assert no modal opens, and that the seeded-answer path still works.
"""


class TestDialogSuppression(EmbodyTestCase):

    def test_onboarding_modal_suppressed_during_run_with_no_seed(self):
        """The drain-then-fire leak guard: with a suite running and NO seeded
        response, the onboarding _messageBox returns -1 (suppressed) instead of
        opening a real modal -- because _testRunnerActive() keeps test_mode True
        even when the response dict is gone."""
        emb = self.embody_ext

        # We are inside a live RunTests* call, so the runner's _running is True.
        self.assertTrue(
            emb._testRunnerActive(),
            '_testRunnerActive() must report True while a suite is running')

        # Remove any seeded responses so ONLY the _running gate can suppress --
        # this reproduces the exact post-drain state that used to open a modal.
        saved = op.Embody.fetch('_smoke_test_responses', None, search=False)
        op.Embody.unstore('_smoke_test_responses')
        try:
            ret = emb._messageBox(
                'Embody - AI Coding Assistant Integration',
                'GUARD: this must return -1 and never open a modal',
                ['Skip', 'Enable Envoy'])
            self.assertEqual(
                ret, -1,
                'onboarding _messageBox must return -1 (no modal) during a test '
                'run even with no seeded response (drain-then-fire leak)')
        finally:
            if saved is not None:
                op.Embody.store('_smoke_test_responses', saved)

    def test_seeded_response_still_consumed_during_run(self):
        """The seeded-answer path must still work while the runner is active, so
        existing tests that assert exact button outcomes keep passing -- the gate
        must not short-circuit a genuinely seeded response."""
        emb = self.embody_ext
        saved = op.Embody.fetch('_smoke_test_responses', None, search=False)
        op.Embody.store('_smoke_test_responses', {'GUARD-SEEDED-TITLE': 1})
        try:
            ret = emb._messageBox('GUARD-SEEDED-TITLE', 'x', ['No', 'Yes'])
            self.assertEqual(
                ret, 1, 'a seeded button must still be consumed and returned')
        finally:
            if saved is not None:
                op.Embody.store('_smoke_test_responses', saved)
            else:
                op.Embody.unstore('_smoke_test_responses')

    def test_runner_active_helper_is_truthy_in_run(self):
        """Sanity on the gate's signal source: _testRunnerActive() is True during
        a run. (Inversely guarantees that a genuine user .tox -- which has no
        op.unit_tests COMP -- returns False, so real onboarding still prompts.)"""
        self.assertTrue(self.embody_ext._testRunnerActive())

    # ----- Save-window suppression (the 12-dialogs-on-save leak) -------------
    # A project save sets op.Embody.store('_suppress_dialogs', True) in
    # onProjectPreSave and clears it after the post-save window. These tests
    # isolate that FLAG from the test-runner signal (by forcing _running False)
    # so they prove the flag itself suppresses -- not just the ambient run.

    ONBOARD_TITLE = 'Embody - AI Coding Assistant Integration'

    def test_save_flag_suppresses_messagebox(self):
        """While _suppress_dialogs is set (a save in progress) and NO test run is
        active and NO response seeded, the onboarding _messageBox returns -1 --
        never reaching ui.messageBox. This is the save-burst leak the fix closes."""
        emb = self.embody_ext
        runner = op.unit_tests.ext.TestRunnerExt
        saved_running = getattr(runner, '_running', False)
        saved_resp = op.Embody.fetch('_smoke_test_responses', None, search=False)
        try:
            runner._running = False                       # isolate: pretend no run
            op.Embody.unstore('_smoke_test_responses')    # no seed -> flag only
            op.Embody.store('_suppress_dialogs', True)
            self.assertTrue(
                emb._suppressDialogs(),
                'the _suppress_dialogs flag alone must make _suppressDialogs() True')
            self.assertEqual(
                emb._messageBox(self.ONBOARD_TITLE, 'guard', ['Skip', 'Enable']),
                -1,
                'onboarding _messageBox must return -1 while a save is in progress')
        finally:
            op.Embody.unstore('_suppress_dialogs')
            runner._running = saved_running
            if saved_resp is not None:
                op.Embody.store('_smoke_test_responses', saved_resp)

    def test_save_flag_makes_promptenvoy_bail_without_disabling(self):
        """The deferred _promptEnvoy, if it fires during a save, must bail at its
        top-gate -- NOT fall through to the else-branch that sets Envoyenable=0.
        Guards the hidden second bug (save silently disabling Envoy)."""
        emb = self.embody_ext
        runner = op.unit_tests.ext.TestRunnerExt
        saved_running = getattr(runner, '_running', False)
        env_before = int(op.Embody.par.Envoyenable.eval())
        try:
            runner._running = False
            op.Embody.store('_suppress_dialogs', True)
            emb._promptEnvoy()                            # must return immediately
            self.assertEqual(
                int(op.Embody.par.Envoyenable.eval()), env_before,
                '_promptEnvoy must not touch Envoyenable while suppressed')
        finally:
            op.Embody.unstore('_suppress_dialogs')
            runner._running = saved_running

    def test_idle_allows_genuine_onboarding(self):
        """With no run AND no save flag, _suppressDialogs() is False so a real
        first-run prompt still shows. Guards against over-suppression. Checks the
        PREDICATE only -- never calls _messageBox in the unsuppressed state."""
        emb = self.embody_ext
        runner = op.unit_tests.ext.TestRunnerExt
        saved_running = getattr(runner, '_running', False)
        try:
            runner._running = False
            op.Embody.unstore('_suppress_dialogs')
            self.assertFalse(
                emb._suppressDialogs(),
                'idle (no run, no save) must allow dialogs -- predicate False')
        finally:
            runner._running = saved_running
