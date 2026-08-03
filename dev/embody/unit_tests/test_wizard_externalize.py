"""
Tests for the setup wizard's externalize step (logic.py wiring +
_applyWizardExternalize / _wizardRecoveryPoint / _projectLooksExternalized).

The step offers two things Embody already had but never surfaced: turning on
the Autoexternalize preference ('both') so NEW ops externalize as they are
created, and -- for the 'full' choice -- the project-wide sweep
(ExternalizeProject).

SAFETY -- 'full' is the one wizard choice that rewrites the WHOLE project (a
project-wide re-tag is what destroyed 18 specimen .tdn files on 2026-07-01,
see .claude/rules/destructive-tests.md). So NOTHING here ever lets the real
sweep run: _scheduleProjectExternalization is stubbed to record, and the
recovery-point probe is stubbed for both outcomes. As in test_setup_wizard,
_restoring_settings is set for the whole test so a param write fires NO parexec
side effects (no config migration at the live repo root), Convoy is held off so
assistant='none' leaves Envoy off, and every mutated parameter is
saved and restored. finish() is NEVER called -- it closes the window and kicks
a real setup. NOT destructive.
"""

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase


class TestWizardExternalize(EmbodyTestCase):

    _PARAMS = ('Autoexternalize', 'Aiclient', 'Envoyenable', 'Envoystatus',
               'Convoyenable')

    def setUp(self):
        self._emb = op.Embody
        self._ext = op.Embody.ext.Embody
        self._envoy = op.Embody.ext.Envoy

        self._saved = {n: getattr(self._emb.par, n).eval() for n in self._PARAMS}

        # Seal every parexec side effect (see the module docstring).
        self._prev_restoring = getattr(self._ext, '_restoring_settings', False)
        self._ext._restoring_settings = True

        # The whole-project sweep is NEVER allowed to run in a test: record the
        # schedule instead. Same for the surfaces the enable path would touch.
        self._sweeps = []
        self._extract_calls = []
        self._stop_calls = []
        self._git_calls = []
        self._ext._scheduleProjectExternalization = (
            lambda *a, **k: self._sweeps.append(1))
        self._ext._extractAIConfig = lambda *a, **k: self._extract_calls.append(1)
        self._envoy.Stop = lambda *a, **k: self._stop_calls.append(1)
        self._ext._applyWizardGitInit = lambda *a, **k: self._git_calls.append(1)

        self._prev_bulk = getattr(self._ext, '_consent_bulk', False)

        # Known starting point so 'unchanged' assertions mean something.
        self._emb.par.Autoexternalize = 'neither'
        self._emb.par.Envoyenable = False
        self._emb.par.Convoyenable = False

    def tearDown(self):
        for obj, name in ((self._ext, '_scheduleProjectExternalization'),
                          (self._ext, '_extractAIConfig'),
                          (self._ext, '_applyWizardGitInit'),
                          (self._ext, '_wizardRecoveryPoint'),
                          (self._envoy, 'Stop')):
            obj.__dict__.pop(name, None)
        self._ext._consent_bulk = self._prev_bulk
        for n, v in self._saved.items():
            try:
                setattr(self._emb.par, n, v)
            except Exception:
                pass
        self._ext._restoring_settings = self._prev_restoring

    # ----- helpers ---------------------------------------------------------

    @property
    def _logicDAT(self):
        """The wizard's logic DAT, or a skip when the panel is absent."""
        dat = op.Embody.op('wizard/logic')
        if dat is None:
            self.skipTest('wizard/logic DAT not present in this build')
        return dat

    @property
    def _logic(self):
        """The wizard's logic module, or a skip when the panel is absent."""
        return self._logicDAT.module

    # ----- logic.py wiring (pure dict/list checks) -------------------------

    def test_step_is_wired_into_defs_and_groups(self):
        logic = self._logic
        self.assertDictHasKey(logic.DEFS, 'externalize',
                              'the externalize step must exist in DEFS')
        d = logic.DEFS['externalize']
        self.assertEqual(d['g'], 'grp_externalize')
        self.assertEqual(d['sel'], 'sel_externalize')
        self.assertEqual(d['title'], 'Make your project AI-readable?')
        self.assertIn('grp_externalize', logic.GROUPS,
                      'the option group must be in GROUPS so render() hides it '
                      'on every other step')

    def test_hint_explains_what_externalizing_does(self):
        # Final design: the standard one-line hint (the tall .tdn-mentioning
        # copy was rolled back so the step matches every other step's frame
        # -- see the v6.0.165 changelog bullet). The hint's job is the
        # user-facing OUTCOME: files, diffable, readable by git + AI tools.
        # '.tdn' is a format name a first-run user has never seen.
        d = self._logic.DEFS['externalize']
        hint = d['hint'].lower()
        for token in ('diffable', 'git', 'ai'):
            self.assertIn(token, hint,
                          f'the hint must mention {token} -- it is the only '
                          f'place the user learns what this step does')

    def test_every_defs_group_is_listed_in_groups(self):
        # render() only hides the groups it finds in GROUPS; a DEFS entry
        # pointing at a group missing from that list would leave a stale panel
        # visible on top of the next step.
        logic = self._logic
        for step, d in logic.DEFS.items():
            g = d.get('g')
            if g:
                self.assertIn(g, logic.GROUPS,
                              f"step '{step}' points at group '{g}', which is "
                              f'not in GROUPS')

    def test_hint_stays_single_line_like_every_other_step(self):
        # Final design: externalize uses the standard 16px one-line hint. Its
        # first iteration used the tall 60px box, which pushed the footer off
        # the step's baseline (verified by rendered-panel capture against the
        # git step -- v6.0.165 changelog). Guard the rollback: externalize
        # must NOT be in the tall-hint tuple, and its hint copy must fit one
        # line alongside the other single-line steps.
        src = self._logicDAT.text.replace(' ', '')
        self.assertNotIn("'externalize'", src.split('par.h=60if', 1)[-1]
                         .split('else16', 1)[0],
                         'externalize must NOT be in the tall-hint tuple -- '
                         'the step uses the standard single-line frame')
        hint = self._logic.DEFS['externalize']['hint']
        self.assertNotIn('\n', hint, 'a single-line hint cannot contain '
                                     'newlines')

    # ----- spine() gating --------------------------------------------------

    def test_spine_omits_step_when_project_already_externalized(self):
        logic = self._logic
        wiz = op.Embody.op('wizard')
        if wiz is None:
            self.skipTest('wizard COMP not present in this build')
        prev = wiz.fetch('ext_needed', None)
        try:
            wiz.store('ext_needed', False)
            self.assertNotIn('externalize', logic.spine(),
                             'an already-externalized project must not be '
                             'offered the step')
        finally:
            if prev is None:
                wiz.unstore('ext_needed')
            else:
                wiz.store('ext_needed', prev)

    def test_spine_includes_step_when_needed_and_group_exists(self):
        # Two gates: the flag AND the option group actually existing (an older
        # panel without grp_externalize would strand Next, which stays disabled
        # until a group option is picked). Assert the exact contract.
        logic = self._logic
        wiz = op.Embody.op('wizard')
        if wiz is None:
            self.skipTest('wizard COMP not present in this build')
        prev = wiz.fetch('ext_needed', None)
        try:
            wiz.store('ext_needed', True)
            sp = logic.spine()
            has_group = wiz.op('grp_externalize') is not None
            self.assertEqual('externalize' in sp, has_group,
                             'the step must appear exactly when the option '
                             'group exists')
            if has_group:
                self.assertEqual(sp[sp.index('externalize') - 1], 'mode',
                                 'the step belongs directly after the mode step')
        finally:
            if prev is None:
                wiz.unstore('ext_needed')
            else:
                wiz.store('ext_needed', prev)

    # ----- _applyWizardExternalize token handling --------------------------

    def test_skip_and_empty_change_nothing(self):
        for token in ('', 'skip', None):
            self._emb.par.Autoexternalize = 'neither'
            self._ext._applyWizardExternalize(token)
            self.assertEqual(self._emb.par.Autoexternalize.eval(), 'neither',
                             f'token {token!r} must not touch the preference')
        self.assertEqual(self._sweeps, [],
                         'skip must never schedule a project-wide sweep')

    def test_unrecognized_token_is_treated_as_skip(self):
        self._ext._applyWizardExternalize('bogus-token')
        self.assertEqual(self._emb.par.Autoexternalize.eval(), 'neither',
                         'an unrecognized token must change nothing')
        self.assertEqual(self._sweeps, [],
                         'an unrecognized token must never sweep the project')

    def test_auto_turns_on_both_without_sweeping(self):
        self._ext._applyWizardExternalize('auto')
        self.assertEqual(self._emb.par.Autoexternalize.eval(), 'both',
                         "'auto' must externalize new DATs AND COMPs")
        self.assertEqual(self._sweeps, [],
                         "'auto' must never touch existing operators")

    def test_full_sweeps_when_a_recovery_point_exists(self):
        self._ext._wizardRecoveryPoint = lambda *a, **k: '/tmp/fake-project.toe'
        self._ext._applyWizardExternalize('full')
        self.assertEqual(self._emb.par.Autoexternalize.eval(), 'both',
                         "'full' also keeps new work externalized")
        self.assertEqual(self._sweeps, [1],
                         "'full' must offer the project-wide sweep exactly once")

    def test_full_refuses_without_a_saved_toe(self):
        # The gate that matters: no recovery point on disk -> the project-wide
        # re-tag must NOT run, while the harmless half still applies.
        self._ext._wizardRecoveryPoint = lambda *a, **k: None
        self._ext._applyWizardExternalize('full')
        self.assertEqual(self._sweeps, [],
                         'a project-wide re-tag must never run without a saved '
                         '.toe to fall back on')
        self.assertEqual(self._emb.par.Autoexternalize.eval(), 'both',
                         'the non-destructive half still applies')

    def test_token_is_case_and_space_tolerant(self):
        self._ext._applyWizardExternalize('  AUTO ')
        self.assertEqual(self._emb.par.Autoexternalize.eval(), 'both')
        self.assertEqual(self._sweeps, [])

    # ----- plumbing through _applyWizardSetup ------------------------------

    def test_setup_passes_token_through(self):
        # assistant='none' returns before the Envoy enable path, so this
        # exercises the plumbing without enabling anything.
        self._ext._applyWizardSetup(mode='auto', assistant='none',
                                    externalize='auto')
        self.assertEqual(self._emb.par.Autoexternalize.eval(), 'both',
                         "the wizard's token must reach the preference")
        self.assertEqual(self._sweeps, [])

    def test_setup_default_omits_the_step(self):
        # Existing callers (and older wizards) pass no externalize kwarg --
        # they must keep working AND change nothing.
        self._ext._applyWizardSetup(mode='auto', assistant='none')
        self.assertEqual(self._emb.par.Autoexternalize.eval(), 'neither',
                         'an omitted token must be a no-op')
        self.assertEqual(self._sweeps, [])

    def test_setup_rejects_a_bogus_token(self):
        self._ext._applyWizardSetup(mode='auto', assistant='none',
                                    externalize='externalize-everything!!')
        self.assertEqual(self._emb.par.Autoexternalize.eval(), 'neither',
                         'a garbled token must be read as skip, never as full')
        self.assertEqual(self._sweeps, [])

    def test_setup_full_still_gated_on_the_recovery_point(self):
        self._ext._wizardRecoveryPoint = lambda *a, **k: None
        self._ext._applyWizardSetup(mode='auto', assistant='none',
                                    externalize='full')
        self.assertEqual(self._sweeps, [],
                         'the recovery-point gate applies through the wizard '
                         'entry point too')

    # ----- probes never raise ---------------------------------------------

    def test_recovery_point_probe_is_safe(self):
        import os
        res = self._ext._wizardRecoveryPoint()
        self.assertTrue(res is None or os.path.isfile(res),
                        'the probe must return an existing file path or None')

    def test_project_probe_returns_a_bool_and_never_raises(self):
        res = self._ext._projectLooksExternalized()
        self.assertIsInstance(res, bool,
                              'spine() gating depends on a plain bool')

    def test_project_probe_defaults_to_showing_on_an_empty_table(self):
        # No tracked rows -> nothing has ever been externalized -> the step is
        # the whole point, so the probe must say "not externalized".
        class _EmptyTable:
            numRows = 1
        real = type(self._ext).Externalizations
        try:
            type(self._ext).Externalizations = property(
                lambda s: _EmptyTable())
            self.assertFalse(self._ext._projectLooksExternalized(),
                             'an empty externalizations table must always show '
                             'the offer')
        finally:
            type(self._ext).Externalizations = real

    def test_project_probe_defaults_to_showing_on_error(self):
        real = type(self._ext).Externalizations
        try:
            def _boom(s):
                raise RuntimeError('table unavailable')
            type(self._ext).Externalizations = property(_boom)
            self.assertFalse(self._ext._projectLooksExternalized(),
                             'any failure must degrade to SHOWING the step')
        finally:
            type(self._ext).Externalizations = real
