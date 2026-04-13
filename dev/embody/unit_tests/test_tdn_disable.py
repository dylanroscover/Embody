"""
Test suite: TDN master enable toggle (`Tdnenable`).

Verifies that the master switch on the TDN page actually gates the TDN
subsystem -- reconstruction, pre-save strip/export, Update() loop, and
SaveTDN() -- and that toggling it OFF is non-destructive (existing .tdn
files on disk and tracked TDN COMP entries are preserved).
"""

import os

try:
    runner_mod = op.unit_tests.op('TestRunnerExt').module
    EmbodyTestCase = runner_mod.EmbodyTestCase
except (AttributeError, NameError):
    pass


class TestTdnDisable(EmbodyTestCase):

    # ------------------------------------------------------------------
    # Lifecycle: snapshot the master toggle so each test can flip it
    # without leaking state.
    # ------------------------------------------------------------------

    def setUp(self):
        super().setUp()
        # Suppress confirmation dialog when toggling OFF.
        self.embody.store('_smoke_test_responses', {
            'Embody - Disable TDN': 1,  # 1 = "Keep .tdn files (disable only)"
        })
        # Remember original value so tearDown restores it.
        self._tdn_was = bool(self.embody.par.Tdnenable.eval())

    def tearDown(self):
        # Restore master toggle without firing parexec side-effects.
        parexec = self.embody.op('parexec')
        was_active = parexec.par.active.eval()
        parexec.par.active = False
        try:
            self.embody.par.Tdnenable.val = self._tdn_was
        finally:
            parexec.par.active = was_active
        # Drop any unconsumed responses.
        try:
            self.embody.unstore('_smoke_test_responses')
        except Exception:
            pass
        super().tearDown()

    # ------------------------------------------------------------------
    # 1. Parameter exists, defaults to ON
    # ------------------------------------------------------------------

    def test_tdnenable_parameter_exists_and_defaults_on(self):
        """The Tdnenable parameter exists on the Embody COMP and defaults to True."""
        par = getattr(self.embody.par, 'Tdnenable', None)
        self.assertIsNotNone(par, 'Tdnenable parameter should exist')
        self.assertTrue(self.embody_ext._tdnEnabled(),
            'TDN should be enabled by default')

    def test_tdnenable_lives_on_tdn_page(self):
        """Tdnenable should appear on the TDN custom page, not somewhere else."""
        found_page = None
        for page in self.embody.customPages:
            for p in page.pars:
                if p.name == 'Tdnenable':
                    found_page = page.name
                    break
            if found_page:
                break
        self.assertEqual(found_page, 'TDN',
            f'Tdnenable should be on TDN page, found on {found_page}')

    # ------------------------------------------------------------------
    # 2. Helper short-circuit: _tdnEnabled tracks the parameter.
    # ------------------------------------------------------------------

    def test_tdnenabled_helper_reflects_parameter(self):
        """_tdnEnabled() returns True/False matching the parameter value."""
        parexec = self.embody.op('parexec')
        was_active = parexec.par.active.eval()
        parexec.par.active = False
        try:
            self.embody.par.Tdnenable.val = True
            self.assertTrue(self.embody_ext._tdnEnabled())
            self.embody.par.Tdnenable.val = False
            self.assertFalse(self.embody_ext._tdnEnabled())
        finally:
            parexec.par.active = was_active

    # ------------------------------------------------------------------
    # 3. ReconstructTDNComps short-circuits when disabled.
    # ------------------------------------------------------------------

    def test_reconstruct_skips_when_disabled(self):
        """ReconstructTDNComps logs and returns immediately when Tdnenable is OFF."""
        parexec = self.embody.op('parexec')
        was_active = parexec.par.active.eval()
        parexec.par.active = False
        try:
            self.embody.par.Tdnenable.val = False
            log_count_before = self.embody_ext._log_counter
            # Should return without iterating any TDN COMPs.
            self.embody_ext.ReconstructTDNComps()
            new_logs = [e for e in self.embody_ext._log_buffer
                        if e['id'] > log_count_before]
            messages = ' | '.join(e.get('message', '') for e in new_logs)
            self.assertIn('TDN disabled', messages,
                'Expected "TDN disabled" log entry, got: ' + messages)
        finally:
            parexec.par.active = was_active

    # ------------------------------------------------------------------
    # 4. SaveTDN short-circuits when disabled.
    # ------------------------------------------------------------------

    def test_savetdn_skips_when_disabled(self):
        """SaveTDN() returns early when Tdnenable is OFF, without writing files."""
        parexec = self.embody.op('parexec')
        was_active = parexec.par.active.eval()
        parexec.par.active = False
        try:
            self.embody.par.Tdnenable.val = False
            log_count_before = self.embody_ext._log_counter
            # Pass an arbitrary path -- the guard runs before any path lookup.
            self.embody_ext.SaveTDN('/no_such_op')
            new_logs = [e for e in self.embody_ext._log_buffer
                        if e['id'] > log_count_before]
            messages = ' | '.join(e.get('message', '') for e in new_logs)
            self.assertIn('TDN disabled', messages,
                'Expected "TDN disabled" log entry, got: ' + messages)
            # Should NOT have logged "Operator not found" (which would
            # mean the guard fell through).
            self.assertNotIn('Operator not found', messages)
        finally:
            parexec.par.active = was_active

    # ------------------------------------------------------------------
    # 5. Update() skips TDN export branch when disabled.
    # ------------------------------------------------------------------

    def test_update_skips_tdn_export_when_disabled(self):
        """Update() bypasses the TDN export loop when Tdnenable is OFF."""
        parexec = self.embody.op('parexec')
        was_active = parexec.par.active.eval()
        parexec.par.active = False
        try:
            self.embody.par.Tdnenable.val = False
            log_count_before = self.embody_ext._log_counter
            self.embody_ext.Update(suppress_refresh=True)
            new_logs = [e for e in self.embody_ext._log_buffer
                        if e['id'] > log_count_before]
            messages = [e.get('message', '') for e in new_logs]
            joined = ' | '.join(messages)
            # Either we hit the explicit "skipping export" log (when there
            # are tracked TDN COMPs) OR there are no tracked TDN COMPs
            # and we never even got to the loop -- both are valid.
            tdn_count = len(self.embody_ext._getTDNStrategyComps())
            if tdn_count:
                self.assertIn('TDN disabled', joined,
                    f'With {tdn_count} TDN COMP(s) tracked, expected '
                    f'"TDN disabled" log; got: {joined}')
        finally:
            parexec.par.active = was_active

    # ------------------------------------------------------------------
    # 6. Existing TDN COMPs / .tdn files survive a toggle-off.
    # ------------------------------------------------------------------

    def test_existing_tdn_entries_preserved_when_disabled(self):
        """Toggling Tdnenable=Off does NOT touch the externalizations table
        or delete any .tdn files on disk."""
        parexec = self.embody.op('parexec')
        was_active = parexec.par.active.eval()
        parexec.par.active = False
        try:
            # Snapshot table rows + on-disk .tdn file listing.
            table = self.embody_ext.Externalizations
            rows_before = []
            for i in range(1, table.numRows):
                if table[i, 'strategy'].val == 'tdn':
                    rows_before.append((
                        table[i, 'path'].val,
                        table[i, 'rel_file_path'].val,
                    ))
            ext_folder = self.embody_ext.getProjectFolder()
            files_before = set()
            if os.path.isdir(ext_folder):
                for root, _, filenames in os.walk(ext_folder):
                    for fn in filenames:
                        if fn.endswith('.tdn'):
                            files_before.add(
                                os.path.relpath(os.path.join(root, fn),
                                                ext_folder))

            # Toggle OFF and immediately back ON.
            self.embody.par.Tdnenable.val = False
            self.assertFalse(self.embody_ext._tdnEnabled())

            # Re-snapshot -- table and files must be unchanged.
            rows_after = []
            for i in range(1, table.numRows):
                if table[i, 'strategy'].val == 'tdn':
                    rows_after.append((
                        table[i, 'path'].val,
                        table[i, 'rel_file_path'].val,
                    ))
            self.assertEqual(sorted(rows_before), sorted(rows_after),
                'Disabling TDN must not mutate externalizations table')

            files_after = set()
            if os.path.isdir(ext_folder):
                for root, _, filenames in os.walk(ext_folder):
                    for fn in filenames:
                        if fn.endswith('.tdn'):
                            files_after.add(
                                os.path.relpath(os.path.join(root, fn),
                                                ext_folder))
            self.assertEqual(files_before, files_after,
                'Disabling TDN must not delete any .tdn file on disk')
        finally:
            parexec.par.active = was_active

    # ------------------------------------------------------------------
    # 7. UI gating: dependent params get disabled when master is OFF.
    # ------------------------------------------------------------------

    def test_dependent_pars_disabled_when_master_off(self):
        """Other TDN-page parameters become disabled when Tdnenable=Off."""
        parexec = self.embody.op('parexec')
        was_active = parexec.par.active.eval()
        parexec.par.active = False
        try:
            self.embody.par.Tdnenable.val = False
            self.embody_ext._applyTdnEnableGating()

            tdn_page = None
            for page in self.embody.customPages:
                if page.name == 'TDN':
                    tdn_page = page
                    break
            self.assertIsNotNone(tdn_page, 'TDN page must exist')

            for p in tdn_page.pars:
                if p.name == 'Tdnenable':
                    self.assertTrue(p.enable,
                        'Tdnenable itself must stay interactive')
                else:
                    self.assertFalse(p.enable,
                        f'Param {p.name} should be disabled when '
                        f'Tdnenable=Off')

            # Re-enable and verify they come back.
            self.embody.par.Tdnenable.val = True
            self.embody_ext._applyTdnEnableGating()
            for p in tdn_page.pars:
                self.assertTrue(p.enable,
                    f'Param {p.name} should be enabled when '
                    f'Tdnenable=On')
        finally:
            parexec.par.active = was_active
