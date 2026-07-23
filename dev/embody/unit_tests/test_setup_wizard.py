"""
Tests for the setup-wizard backend (_applyWizardSetup / _enableEnvoyResolved /
_openSetupWizard).

_applyWizardSetup is the single entry point the wizard's finish() calls. It maps
the collected selections (mode / assistant / client / root) onto Embody's params
and then either enables Envoy (modal-free, via _enableEnvoyResolved) or -- for
assistant='none' / an unrecognized token -- leaves it off. _enableEnvoyResolved
enables on first run and RESTARTS on a re-run (Envoy already on).
_openSetupWizard opens the wizard window but must NEVER do so while dialogs are
suppressed (a test run or a save).

SAFETY -- this is the critical part (see .claude/rules/destructive-tests.md):
assigning Aiprojectroot fires parexec's _migrateRootFiles UNCONDITIONALLY (it is
NOT gated on Envoyenable), which would move/delete Embody + AI config at the LIVE
repo root. So setUp sets `_restoring_settings = True` for the whole test -- the
exact guard settings-restore uses -- which makes parexec.onValueChange return
early and fire NO side effects (migration, InitEnvoy, _extractAIConfig, Stop,
persistence). Param writes then only set values. The two surfaces
_enableEnvoyResolved touches DIRECTLY (not via parexec) -- _extractAIConfig and
Envoy.Stop -- are monkeypatched to record instead of run. Every mutated param is
saved and restored. NOT destructive.
"""

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase


class TestSetupWizard(EmbodyTestCase):

    _PARAMS = ('Embodymode', 'Aiprojectroot', 'Aiprojectrootcustom',
               'Aiclient', 'Envoyenable', 'Envoystatus', 'Toolpermissions')

    def setUp(self):
        self._emb = op.Embody
        self._ext = op.Embody.ext.Embody
        self._envoy = op.Embody.ext.Envoy

        self._saved = {n: getattr(self._emb.par, n).eval() for n in self._PARAMS}

        # Seal ALL parexec side effects so a param write cannot migrate/delete
        # config at the live repo root (the destructive-tests hazard).
        self._prev_restoring = getattr(self._ext, '_restoring_settings', False)
        self._ext._restoring_settings = True

        # Record the config-write + restart surfaces _enableEnvoyResolved calls
        # DIRECTLY (these bypass the parexec guard).
        self._extract_calls = []
        self._stop_calls = []
        self._git_calls = []
        self._ext._extractAIConfig = lambda *a, **k: self._extract_calls.append(1)
        self._envoy.Stop = lambda *a, **k: self._stop_calls.append(1)
        # The git step's worker would run a REAL `git init` at project.folder --
        # record instead (the worker itself is tested against a temp dir below).
        self._ext._applyWizardGitInit = lambda *a, **k: self._git_calls.append(1)

        # _enableEnvoyResolved sets _consent_bulk (cleared in production by
        # _continueStart + a timer, neither of which fires in a sync test) -- so
        # snapshot it and clear on teardown, or it leaks True into the session.
        self._prev_bulk = getattr(self._ext, '_consent_bulk', False)
        self._prev_pass = getattr(self._ext, '_startup_config_pass', False)

        # Default posture: first run (Envoy off). Suppressed, so no real Stop.
        self._emb.par.Envoyenable = False

    def tearDown(self):
        for obj, name in ((self._ext, '_extractAIConfig'),
                          (self._ext, '_applyWizardGitInit'),
                          (self._envoy, 'Stop')):
            obj.__dict__.pop(name, None)
        self._ext._consent_bulk = self._prev_bulk
        self._ext._startup_config_pass = self._prev_pass
        # Restore params while side effects are still sealed, then lift the seal.
        for n, v in self._saved.items():
            try:
                setattr(self._emb.par, n, v)
            except Exception:
                pass
        self._ext._restoring_settings = self._prev_restoring

    # ----- assistant = claudecode (first run) -----------------------------

    def test_claudecode_sets_params_and_enables(self):
        self._ext._applyWizardSetup(mode='advanced', assistant='claudecode',
                                    root='projectfolder')
        self.assertEqual(self._emb.par.Embodymode.eval(), 'advanced')
        self.assertEqual(self._emb.par.Aiprojectroot.eval(), 'projectfolder')
        self.assertEqual(self._emb.par.Aiclient.eval(), 'claudecode')
        self.assertTrue(bool(self._emb.par.Envoyenable.eval()),
                        'first-run claudecode must enable Envoy')
        self.assertEqual(self._extract_calls, [1],
                         'first run must write AI config exactly once')

    # ----- tool-permissions posture (settings.local.json) -----------------

    def test_permissions_plumbs_to_param(self):
        # The wizard's permissions step passes a posture token; _applyWizardSetup
        # must persist it on Toolpermissions (read later by _deploySettingsLocal).
        self._ext._applyWizardSetup(mode='auto', assistant='claudecode',
                                    permissions='some')
        self.assertEqual(self._emb.par.Toolpermissions.eval(), 'some',
                         'the chosen posture must land on the Toolpermissions param')

    def test_permissions_defaults_to_all(self):
        self._emb.par.Toolpermissions = 'prompt'   # prove the default overrides it
        self._ext._applyWizardSetup(mode='auto', assistant='claudecode')
        self.assertEqual(self._emb.par.Toolpermissions.eval(), 'all',
                         "an omitted posture must default to 'all'")

    def test_unknown_permissions_falls_back_to_all(self):
        self._ext._applyWizardSetup(mode='auto', assistant='claudecode',
                                    permissions='bogus')
        self.assertEqual(self._emb.par.Toolpermissions.eval(), 'all',
                         "an unrecognized posture token must fall back to 'all'")

    # ----- assistant = other ----------------------------------------------

    def test_other_maps_client_token(self):
        self._ext._applyWizardSetup(mode='auto', assistant='other',
                                    client='cursor', root='gitroot')
        self.assertEqual(self._emb.par.Aiclient.eval(), 'cursor')
        self.assertEqual(self._emb.par.Aiprojectroot.eval(), 'gitroot')
        self.assertTrue(bool(self._emb.par.Envoyenable.eval()))

    def test_other_blank_client_keeps_selection_but_still_enables(self):
        self._emb.par.Aiclient = 'windsurf'
        self._ext._applyWizardSetup(assistant='other', client='')
        self.assertEqual(self._emb.par.Aiclient.eval(), 'windsurf',
                         'a blank client must not clobber the current selection')
        self.assertTrue(bool(self._emb.par.Envoyenable.eval()))

    # ----- assistant = none -----------------------------------------------

    def test_none_disables_and_skips_enable(self):
        self._emb.par.Envoyenable = True   # prove the branch flips it back off
        self._ext._applyWizardSetup(mode='auto', assistant='none')
        self.assertEqual(self._emb.par.Aiclient.eval(), 'none')
        self.assertFalse(bool(self._emb.par.Envoyenable.eval()),
                         "assistant='none' must leave Envoy disabled")
        self.assertEqual(self._extract_calls, [],
                         "assistant='none' must NOT write AI config")

    # ----- unrecognized assistant (defensive) -----------------------------

    def test_unknown_assistant_is_noop_not_enable(self):
        self._emb.par.Envoyenable = False
        before_client = self._emb.par.Aiclient.eval()
        self._ext._applyWizardSetup(mode='auto', assistant='bogus')
        self.assertFalse(bool(self._emb.par.Envoyenable.eval()),
                         'an unknown assistant token must NOT enable Envoy')
        self.assertEqual(self._extract_calls, [],
                         'an unknown assistant token must not write config')
        self.assertEqual(self._emb.par.Aiclient.eval(), before_client,
                         'an unknown assistant token must leave client unchanged')

    # ----- root handling --------------------------------------------------

    def test_custom_root_sets_path_and_mode(self):
        self._ext._applyWizardSetup(mode='advanced', assistant='claudecode',
                                    root='custom', custom_root='/tmp/embody-cfg')
        self.assertEqual(self._emb.par.Aiprojectroot.eval(), 'custom')
        self.assertEqual(self._emb.par.Aiprojectrootcustom.eval(),
                         '/tmp/embody-cfg')

    def test_bogus_mode_left_unchanged(self):
        self._emb.par.Embodymode = 'auto'
        self._ext._applyWizardSetup(mode='nonsense', assistant='claudecode')
        self.assertEqual(self._emb.par.Embodymode.eval(), 'auto',
                         'an invalid mode token must be ignored, not applied')

    # ----- re-run path (Envoy already enabled) ----------------------------

    def test_rerun_restarts_instead_of_reenabling(self):
        # Envoy already ON -> _enableEnvoyResolved must RESTART (Stop + deferred
        # Start) rather than a no-op Envoyenable flip, and must NOT re-write AI
        # config (parexec already did on the param change in production).
        self._emb.par.Envoyenable = True
        self._ext._enableEnvoyResolved()
        self.assertEqual(self._stop_calls, [1],
                         'a re-run must Stop() to force a restart')
        self.assertEqual(self._extract_calls, [],
                         'a re-run must not re-extract AI config')
        self.assertTrue(bool(self._emb.par.Envoyenable.eval()),
                        'Envoy must stay enabled across a re-run')

    # ----- consented, atomic first-run enable (fix #4) --------------------

    def test_first_run_enable_consents_the_config_batch(self):
        # The wizard's footprint step IS the consent, so _enableEnvoyResolved
        # sets _consent_bulk -> the config writes (this sync one + the git/MCP
        # writes in the deferred Start) apply silently, without a second modal,
        # and the enable stays atomic (config can't be declined mid-flip).
        self._emb.par.Envoyenable = False
        self._ext._applyWizardSetup(mode='advanced', assistant='claudecode')
        self.assertTrue(self._ext._consent_bulk,
                        'first-run enable must consent the config batch so the '
                        'deferred Start writes apply without re-prompting')
        self.assertTrue(bool(self._emb.par.Envoyenable.eval()))
        self.assertEqual(self._extract_calls, [1],
                         'AI config is written once under the consent')

    # ----- git step (the wizard-owned git decision) ------------------------

    def test_gitinit_runs_the_init_worker(self):
        self._ext._applyWizardSetup(mode='auto', assistant='claudecode',
                                    git='gitinit')
        self.assertEqual(self._git_calls, [1],
                         "git='gitinit' must initialize a repo exactly once")
        self.assertTrue(bool(self._emb.par.Envoyenable.eval()),
                        'the enable path continues after the git action')

    def test_gitskip_and_default_do_not_init(self):
        self._ext._applyWizardSetup(mode='auto', assistant='claudecode',
                                    git='gitskip')
        self._ext._applyWizardSetup(mode='auto', assistant='claudecode')
        self.assertEqual(self._git_calls, [],
                         "'gitskip' and an omitted git token must never init")

    def test_bogus_git_token_never_inits(self):
        self._ext._applyWizardSetup(mode='auto', assistant='claudecode',
                                    git='bogus-token')
        self.assertEqual(self._git_calls, [],
                         'an unrecognized git token must never init a repo')

    def test_none_assistant_still_honors_gitinit(self):
        # The git decision is about the PROJECT, not the AI: externalization-
        # only users still get their choice (the action runs before the
        # assistant early-return).
        self._ext._applyWizardSetup(mode='auto', assistant='none',
                                    git='gitinit')
        self.assertEqual(self._git_calls, [1],
                         "assistant='none' must still honor the git choice")
        self.assertFalse(bool(self._emb.par.Envoyenable.eval()),
                         "and Envoy must stay off for assistant='none'")

    def test_init_git_repo_worker_in_temp_dir(self):
        # The real worker (envoy_setup.init_git_repo) against a throwaway
        # dir: creates .git, verifies, and writes .gitignore/.gitattributes.
        # Isolation: the worker's _write callbacks record uninstall-manifest
        # entries against the LIVE project root and route through
        # _guardFileWrite (Advanced mode would decline under test
        # suppression) -- stub the recorder and force bulk consent so the
        # test neither pollutes the live manifest nor depends on the
        # project's Embodymode value.
        import tempfile
        import shutil
        from unittest.mock import patch
        from pathlib import Path
        tmp = Path(tempfile.mkdtemp(prefix='wizard_git_'))
        try:
            setup_mod = op.Embody.op('envoy_setup').module
            emb_ext = op.Embody.ext.Embody
            with patch.object(emb_ext, '_manifestRecordAppendedFile'), \
                 patch.object(type(emb_ext), '_consent_bulk', True):
                res = setup_mod.init_git_repo(self._envoy, tmp)
            self.assertEqual(res, tmp,
                             'init must return the target dir on success')
            self.assertTrue((tmp / '.git').exists(), 'git init must have run')
            self.assertTrue((tmp / '.gitignore').is_file(),
                            'git config files belong with git init (issue #8)')
            self.assertTrue((tmp / '.gitattributes').is_file())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_init_git_repo_worker_failure_returns_none(self):
        from pathlib import Path
        setup_mod = op.Embody.op('envoy_setup').module
        bogus = Path(r'\\?\definitely-not-a-host\no\such\dir')
        self.assertIsNone(setup_mod.init_git_repo(self._envoy, bogus),
                          'a failed init must return None, never raise')

    # ----- _openSetupWizard suppression -----------------------------------

    def test_open_wizard_suppressed_during_test_run(self):
        # A live suite is running, so _suppressDialogs() is True. _openSetupWizard
        # must bail WITHOUT opening the window -- the guarantee that it never
        # surprise-pops during automation.
        self.assertTrue(self._ext._suppressDialogs(),
                        'a running suite must suppress dialogs')
        win = self._emb.op('window_wizard')
        was_open = win.isOpen if win else False
        self._ext._openSetupWizard()   # must be a no-op while suppressed
        now_open = win.isOpen if win else False
        self.assertEqual(was_open, now_open,
                         '_openSetupWizard must not open the window while suppressed')
