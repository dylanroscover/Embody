"""
Test suite: TDN master mode menu (`Tdnmode`).

Verifies the three-mode menu (Off / Export-on-Save (MCP) / Full Import/Export
(Experimental)) correctly gates:
  - Reconstruction on project open (off: skip; export: skip; full: run)
  - Pre-save strip (off: skip; export: skip; full: run)
  - TDN export from Update() and SaveTDN (off: skip; export: run; full: run)
  - UI gating on the TDN page (off: all greyed; export: strip/create greyed;
    full: all live)

Tracked .tdn files on disk survive mode flips (non-destructive transitions).
"""

import os

try:
    runner_mod = op.unit_tests.op('TestRunnerExt').module
    EmbodyTestCase = runner_mod.EmbodyTestCase
except (AttributeError, NameError):
    pass


class TestTdnMode(EmbodyTestCase):

    # ------------------------------------------------------------------
    # Lifecycle: snapshot the mode so each test can flip it safely.
    # ------------------------------------------------------------------

    def setUp(self):
        super().setUp()
        # Suppress the 'Off' confirmation dialog when flipping to off.
        self.embody.store('_smoke_test_responses', {
            'Embody - Disable TDN': 1,  # 1 = 'Keep .tdn files (disable only)'
        })
        self._mode_was = self.embody.par.Tdnmode.eval()

    def tearDown(self):
        # Restore mode without firing parexec side-effects.
        parexec = self.embody.op('parexec')
        was_active = parexec.par.active.eval()
        parexec.par.active = False
        try:
            self.embody.par.Tdnmode.val = self._mode_was
        finally:
            parexec.par.active = was_active
        try:
            self.embody.unstore('_smoke_test_responses')
        except Exception:
            pass
        super().tearDown()

    def _setMode(self, mode: str) -> None:
        """Set Tdnmode with parexec suppressed (no side-effects)."""
        parexec = self.embody.op('parexec')
        was_active = parexec.par.active.eval()
        parexec.par.active = False
        try:
            self.embody.par.Tdnmode.val = mode
        finally:
            parexec.par.active = was_active

    # ------------------------------------------------------------------
    # 1. Parameter shape
    # ------------------------------------------------------------------

    def test_tdnmode_parameter_exists_as_menu(self):
        par = getattr(self.embody.par, 'Tdnmode', None)
        self.assertIsNotNone(par, 'Tdnmode parameter must exist')
        self.assertEqual(par.style, 'Menu')

    def test_tdnmode_menu_has_three_values(self):
        par = self.embody.par.Tdnmode
        names = list(par.menuNames)
        self.assertEqual(sorted(names), ['export', 'full', 'off'])

    def test_tdnmode_default_is_export(self):
        self.assertEqual(self.embody.par.Tdnmode.default, 'export')

    def test_tdnmode_lives_on_tdn_page(self):
        found_page = None
        for page in self.embody.customPages:
            for p in page.pars:
                if p.name == 'Tdnmode':
                    found_page = page.name
                    break
            if found_page:
                break
        self.assertEqual(found_page, 'TDN')

    # ------------------------------------------------------------------
    # 2. Helper short-circuits
    # ------------------------------------------------------------------

    def test_tdnMode_helper_returns_menu_value(self):
        for mode in ('off', 'export', 'full'):
            self._setMode(mode)
            self.assertEqual(self.embody_ext._tdnMode(), mode)

    def test_tdnEnabled_false_only_when_off(self):
        self._setMode('off')
        self.assertFalse(self.embody_ext._tdnEnabled())
        self._setMode('export')
        self.assertTrue(self.embody_ext._tdnEnabled())
        self._setMode('full')
        self.assertTrue(self.embody_ext._tdnEnabled())

    # ------------------------------------------------------------------
    # 3. Reconstruction gating
    # ------------------------------------------------------------------

    def test_reconstruct_skips_in_off(self):
        self._setMode('off')
        log_before = self.embody_ext._log_counter
        self.embody_ext.ReconstructTDNComps()
        new_logs = [e for e in self.embody_ext._log_buffer
                    if e['id'] > log_before]
        messages = ' | '.join(e.get('message', '') for e in new_logs)
        self.assertIn('mode=off', messages)

    def test_reconstruct_skips_in_export(self):
        self._setMode('export')
        log_before = self.embody_ext._log_counter
        self.embody_ext.ReconstructTDNComps()
        new_logs = [e for e in self.embody_ext._log_buffer
                    if e['id'] > log_before]
        messages = ' | '.join(e.get('message', '') for e in new_logs)
        self.assertIn('mode=export', messages)

    # ------------------------------------------------------------------
    # 4. SaveTDN gating
    # ------------------------------------------------------------------

    def test_savetdn_skips_when_off(self):
        self._setMode('off')
        log_before = self.embody_ext._log_counter
        self.embody_ext.SaveTDN('/no_such_op')
        new_logs = [e for e in self.embody_ext._log_buffer
                    if e['id'] > log_before]
        messages = ' | '.join(e.get('message', '') for e in new_logs)
        self.assertIn('TDN disabled', messages)
        self.assertNotIn('Operator not found', messages)

    # ------------------------------------------------------------------
    # 5. Disk-side non-destructive mode flips
    # ------------------------------------------------------------------

    def test_existing_tdn_entries_preserved_across_mode_flips(self):
        table = self.embody_ext.Externalizations
        ext_folder = self.embody_ext.getProjectFolder()

        def snapshot():
            rows = []
            for i in range(1, table.numRows):
                if table[i, 'strategy'].val == 'tdn':
                    rows.append((
                        table[i, 'path'].val,
                        table[i, 'rel_file_path'].val,
                    ))
            files = set()
            if os.path.isdir(ext_folder):
                for root, _, filenames in os.walk(ext_folder):
                    for fn in filenames:
                        if fn.endswith('.tdn'):
                            files.add(os.path.relpath(
                                os.path.join(root, fn), ext_folder))
            return sorted(rows), files

        rows_before, files_before = snapshot()
        # Cycle through all three modes and back.
        for mode in ('off', 'export', 'full', self._mode_was):
            self._setMode(mode)
        rows_after, files_after = snapshot()
        self.assertEqual(rows_before, rows_after,
            'Mode flips must not mutate externalizations table')
        self.assertEqual(files_before, files_after,
            'Mode flips must not delete .tdn files on disk')

    # ------------------------------------------------------------------
    # 6. UI gating per mode
    # ------------------------------------------------------------------

    def _getTdnPage(self):
        for page in self.embody.customPages:
            if page.name == 'TDN':
                return page
        return None

    def test_gating_off_greys_all_except_mode(self):
        self._setMode('off')
        self.embody_ext._applyTdnModeGating()
        page = self._getTdnPage()
        self.assertIsNotNone(page)
        for p in page.pars:
            if p.name == 'Tdnmode':
                self.assertTrue(p.enable, 'Tdnmode itself must stay live')
            else:
                self.assertFalse(p.enable,
                    f'{p.name} should be greyed in Off mode')

    def test_gating_export_greys_strip_params_only(self):
        self._setMode('export')
        self.embody_ext._applyTdnModeGating()
        full_only = self.embody_ext._TDN_FULL_ONLY_PARAMS
        page = self._getTdnPage()
        for p in page.pars:
            if p.name == 'Tdnmode':
                self.assertTrue(p.enable)
            elif p.name in full_only:
                self.assertFalse(p.enable,
                    f'{p.name} should be greyed in Export mode')
            else:
                self.assertTrue(p.enable,
                    f'{p.name} should be live in Export mode')

    def test_gating_full_enables_all(self):
        self._setMode('full')
        self.embody_ext._applyTdnModeGating()
        page = self._getTdnPage()
        for p in page.pars:
            self.assertTrue(p.enable,
                f'{p.name} should be live in Full mode')
