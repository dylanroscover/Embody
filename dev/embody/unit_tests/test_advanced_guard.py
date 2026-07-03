"""
Tests for _guardFileWrite -- the Advanced-mode chokepoint that gates every
invasive write to the user's repo (git config, .mcp.json, AI config).

Behavior:
  auto (or a consented batch, _consent_bulk) -> apply_fn() silently.
  advanced + interactive                     -> Apply/Skip; apply only on Apply.
  advanced + suppressed (startup/save/test)  -> DECLINE (defer) -- never write
                                                to the user's files silently
                                                when they asked to be consulted.

These are PURE-LOGIC tests: apply_fn is a list.append, so NO real file is
written and the live repo is never touched. The advanced Apply/Skip is driven
through the real _messageBox seeding machinery (_smoke_test_responses keyed by
the dialog title). NOT destructive.
"""

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase


class TestAdvancedGuard(EmbodyTestCase):

    CATEGORY = 'GuardTest'
    TITLE = 'Embody - GuardTest'   # _guardFileWrite prefixes 'Embody - '

    def setUp(self):
        self._ext = op.Embody.ext.Embody
        self._saved = op.Embody.fetch('_smoke_test_responses', None, search=False)
        self._prev_bulk = getattr(self._ext, '_consent_bulk', False)
        self._prev_pass = getattr(self._ext, '_startup_config_pass', False)

    def tearDown(self):
        self._ext._consent_bulk = self._prev_bulk
        self._ext._startup_config_pass = self._prev_pass
        if self._saved is not None:
            op.Embody.store('_smoke_test_responses', self._saved)
        else:
            op.Embody.unstore('_smoke_test_responses')

    def _seed(self, idx):
        # _guard uses ['Apply', 'Skip']; Apply == 0.
        op.Embody.store('_smoke_test_responses', {self.TITLE: idx})

    def _call(self, mode):
        applied = []
        ok = self._ext._guardFileWrite(
            self.CATEGORY, 'edit test files', ['/tmp/a', '/tmp/b'],
            lambda: applied.append(1), mode=mode)
        return ok, applied

    # ----- auto: act silently ---------------------------------------------

    def test_auto_applies_without_prompt(self):
        op.Embody.unstore('_smoke_test_responses')   # no seed at all
        self._ext._consent_bulk = False
        ok, applied = self._call('auto')
        self.assertTrue(ok)
        self.assertEqual(applied, [1], 'auto must apply the write silently')

    # ----- advanced: ask first --------------------------------------------

    def test_advanced_applies_on_confirm(self):
        self._ext._consent_bulk = False
        self._seed(0)   # Apply
        ok, applied = self._call('advanced')
        self.assertTrue(ok)
        self.assertEqual(applied, [1])

    def test_advanced_skips_on_decline(self):
        self._ext._consent_bulk = False
        self._seed(1)   # Skip
        ok, applied = self._call('advanced')
        self.assertFalse(ok)
        self.assertEqual(applied, [], 'a declined write must not run')

    def test_advanced_defers_when_suppressed(self):
        # No seed -> _messageBox returns -1 (suppressed) -> decline. This is the
        # startup/save case: never write to the user's files silently.
        op.Embody.unstore('_smoke_test_responses')
        self._ext._consent_bulk = False
        ok, applied = self._call('advanced')
        self.assertFalse(ok)
        self.assertEqual(applied, [],
                         'a suppressed advanced write must DEFER, not apply')

    # ----- consented batch bypass -----------------------------------------

    def test_consent_bulk_bypasses_prompt_even_in_advanced(self):
        # An orchestrator/wizard that already showed ONE combined confirm sets
        # _consent_bulk so the sub-writes apply silently.
        op.Embody.unstore('_smoke_test_responses')   # no seed; would else defer
        self._ext._consent_bulk = True
        try:
            ok, applied = self._call('advanced')
        finally:
            self._ext._consent_bulk = False
        self.assertTrue(ok, 'a consented batch must apply even in advanced')
        self.assertEqual(applied, [1])

    # ----- startup Start (project open): defer, never a modal --------------

    def test_startup_pass_defers_even_with_seed(self):
        # On a startup Start (_startup_config_pass) a modal must NEVER pop -- it
        # would block the frame-30..80 restore chain. Advanced DEFERS with a
        # breadcrumb and applies NOTHING, even if a seed would say Apply.
        self._ext._consent_bulk = False
        self._seed(0)   # would be Apply if it reached the modal
        self._ext._startup_config_pass = True
        ok, applied = self._call('advanced')
        self.assertFalse(ok, 'a startup Start must defer (not apply) in advanced')
        self.assertEqual(applied, [], 'startup defer must write nothing')

    def test_startup_pass_auto_still_applies(self):
        # Auto on a startup Start applies silently as before (managed default).
        self._ext._consent_bulk = False
        op.Embody.unstore('_smoke_test_responses')
        self._ext._startup_config_pass = True
        ok, applied = self._call('auto')
        self.assertTrue(ok)
        self.assertEqual(applied, [1], 'auto must still restore silently on open')

    def test_consent_bulk_overrides_startup_pass(self):
        # A wizard-consented batch applies even during a startup Start, so a
        # consented first-run still writes its config.
        self._seed(1)   # Skip, ignored under bulk
        self._ext._consent_bulk = True
        self._ext._startup_config_pass = True
        try:
            ok, applied = self._call('advanced')
        finally:
            self._ext._consent_bulk = False
        self.assertTrue(ok, 'consent_bulk must apply even during a startup Start')
        self.assertEqual(applied, [1])

    # ----- default posture -------------------------------------------------

    def test_default_mode_is_auto_silent(self):
        # With mode omitted, _guardFileWrite consults _embodyMode(); default auto
        # (or the live param) -- here we force the param to auto for determinism.
        prev = op.Embody.par.Embodymode.eval()
        try:
            op.Embody.par.Embodymode = 'auto'
            op.Embody.unstore('_smoke_test_responses')
            self._ext._consent_bulk = False
            applied = []
            ok = self._ext._guardFileWrite(
                self.CATEGORY, 'edit test files', ['/tmp/a'],
                lambda: applied.append(1))   # no explicit mode
            self.assertTrue(ok)
            self.assertEqual(applied, [1])
        finally:
            op.Embody.par.Embodymode = prev
