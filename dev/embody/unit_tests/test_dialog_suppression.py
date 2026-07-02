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

    # ----- The misleading-"[test]"-on-save regression --------------------
    # A save suppressed the dialog AND mislabeled it as a test context, so
    # every Ctrl+S logged '[test] No response seeded for "TDN Content at
    # Risk" ...'. The fix splits the test gate from the save gate: a save
    # returns the safe default QUIETLY (DEBUG), only a real run still warns.

    def test_save_suppress_does_not_log_test_warning(self):
        """While a save is in progress (NOT a test run), a gated dialog must
        return -1 WITHOUT emitting the '[test] No response seeded' WARNING
        that used to hit the textport on every save."""
        emb = self.embody_ext
        runner = op.unit_tests.ext.TestRunnerExt
        saved_running = getattr(runner, '_running', False)
        saved_resp = op.Embody.fetch('_smoke_test_responses', None, search=False)
        try:
            runner._running = False                       # isolate: not a test
            op.Embody.unstore('_smoke_test_responses')    # no seed
            op.Embody.store('_suppress_dialogs', True)    # a save is mid-flight
            before_id = max((e['id'] for e in emb._log_buffer), default=0)
            ret = emb._messageBox('TDN Content at Risk', 'guard',
                                  ['Externalize', 'Skip'])
            # Diff by entry id, not list index: _log_buffer is a bounded
            # deque(maxlen=200); once saturated, appends evict from the left, so a
            # positional [before:] tail slice is always empty (false negative).
            new_entries = [e for e in emb._log_buffer if e['id'] > before_id]
            self.assertEqual(ret, -1, 'must return the safe default during save')
            offending = [e for e in new_entries
                         if e['level'] == 'WARNING'
                         and 'No response seeded' in e['message']]
            self.assertEqual(
                offending, [],
                'a save-suppressed dialog must NOT log a "[test] No response '
                f'seeded" WARNING; got: {offending}')
        finally:
            op.Embody.unstore('_suppress_dialogs')
            runner._running = saved_running
            if saved_resp is not None:
                op.Embody.store('_smoke_test_responses', saved_resp)

    def test_active_test_run_still_warns_on_unseeded_dialog(self):
        """Counterpart: a genuine test run with an unseeded dialog MUST still
        emit the '[test] No response seeded' WARNING so test authors notice
        the gap. The real runner is active here, so _testRunnerActive() is
        True and the test gate -- not the save gate -- fires."""
        emb = self.embody_ext
        saved_resp = op.Embody.fetch('_smoke_test_responses', None, search=False)
        try:
            op.Embody.unstore('_smoke_test_responses')    # no seed, run active
            self.assertTrue(emb._testRunnerActive())
            before_id = max((e['id'] for e in emb._log_buffer), default=0)
            ret = emb._messageBox('UNSEEDED DURING RUN', 'guard', ['A', 'B'])
            # Diff by entry id, not list index (bounded deque -- see sibling test).
            new_entries = [e for e in emb._log_buffer if e['id'] > before_id]
            self.assertEqual(ret, -1)
            warned = [e for e in new_entries
                      if e['level'] == 'WARNING'
                      and 'No response seeded' in e['message']]
            self.assertTrue(
                warned, 'an unseeded dialog during a real run must still warn')
        finally:
            if saved_resp is not None:
                op.Embody.store('_smoke_test_responses', saved_resp)
