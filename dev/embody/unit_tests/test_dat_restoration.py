"""
Test suite: DAT/TOX restoration on startup and continuity check hardening.

Tests RestoreDATs(), _getDATEntries(), and the file-existence guard in
checkOpsForContinuity() that protects recoverable entries from deletion.
Also tests the TOX load triggers (RestoreTOXComps / _reloadTox /
ReconcileMetadata): TD 2025 loads an external .tox mid-session ONLY via
enableexternaltoxpulse -- setting externaltox does not auto-load,
toggling enableexternaltox off->on does not re-read the file, and
reloadtoxpulse does not exist (verified empirically 2026-07-18 on
builds 2025.32820 and 2025.33070).
"""

from pathlib import Path

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase


class TestDATRestoration(EmbodyTestCase):

    def setUp(self):
        self._test_dir = Path(project.folder) / 'embody' / 'unit_tests' / '_test_temp'
        self._test_dir.mkdir(parents=True, exist_ok=True)
        # Create a root-level sandbox outside all TDN/TOX COMPs.
        # The normal sandbox is inside /embody/unit_tests (a TDN COMP),
        # so _getDATEntries() correctly filters it. Restoration tests
        # need a parent that's NOT inside any managed COMP.
        self._root_sandbox = op('/').create(baseCOMP, '_test_dat_restore')
        # Track table rows we add so tearDown can clean them up
        self._added_paths = []

    def tearDown(self):
        # Clean up root-level sandbox
        if op(self._root_sandbox.path):
            self._root_sandbox.destroy()
        # Clean up any table entries we injected
        table = self.embody_ext.Externalizations
        for path in self._added_paths:
            for i in range(table.numRows - 1, 0, -1):
                if table[i, 'path'].val == path:
                    table.deleteRow(i)
        # Clean up temp files
        for f in self._test_dir.glob('*'):
            try:
                f.unlink()
            except OSError:
                pass
        super().tearDown()

    # --- Helpers ---

    def _add_table_entry(self, path, dat_type, strategy, rel_file_path):
        """Add a row to the externalizations table directly."""
        table = self.embody_ext.Externalizations
        table.appendRow([
            path, dat_type, strategy, rel_file_path,
            '2026-01-01 00:00:00 UTC', '', '', '',
            '0', '0', ''
        ])
        self._added_paths.append(path)

    def _create_file(self, rel_path, content='# test'):
        """Create a file on disk at the given relative path."""
        abs_path = self.embody_ext.buildAbsolutePath(rel_path)
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_text(content, encoding='utf-8')
        return abs_path

    def _table_has_path(self, path):
        """Check if the externalizations table has an entry for the given path."""
        table = self.embody_ext.Externalizations
        for i in range(1, table.numRows):
            if table[i, 'path'].val == path:
                return True
        return False

    # =================================================================
    # RestoreDATs - basic restoration
    # =================================================================

    def test_restore_missing_text_dat(self):
        """Missing textDAT with .py file on disk should be recreated."""
        dat_path = self._root_sandbox.path + '/restored_text'
        rel_path = 'embody/unit_tests/_test_temp/restored_text.py'
        self._create_file(rel_path, '# restored content')
        self._add_table_entry(dat_path, 'text', 'py', rel_path)

        self.embody_ext.RestoreDATs()

        restored = op(dat_path)
        self.assertIsNotNone(restored, 'textDAT should be restored')
        self.assertEqual(restored.type, 'text')
        self.assertIn('restored_text.py', restored.par.file.eval())
        self.assertTrue(restored.par.syncfile.eval())

    def test_restore_missing_table_dat(self):
        """Missing tableDAT with .tsv file on disk should be recreated."""
        dat_path = self._root_sandbox.path + '/restored_table'
        rel_path = 'embody/unit_tests/_test_temp/restored_table.tsv'
        self._create_file(rel_path, 'col1\tcol2\nval1\tval2')
        self._add_table_entry(dat_path, 'table', 'tsv', rel_path)

        self.embody_ext.RestoreDATs()

        restored = op(dat_path)
        self.assertIsNotNone(restored, 'tableDAT should be restored')
        self.assertEqual(restored.type, 'table')

    def test_restore_applies_tag(self):
        """Restored DAT should have the strategy value as a tag."""
        dat_path = self._root_sandbox.path + '/tagged_dat'
        rel_path = 'embody/unit_tests/_test_temp/tagged_dat.py'
        self._create_file(rel_path)
        self._add_table_entry(dat_path, 'text', 'py', rel_path)

        self.embody_ext.RestoreDATs()

        restored = op(dat_path)
        self.assertIsNotNone(restored, 'DAT should be restored for tag check')
        self.assertIn('py', restored.tags)

    def test_restore_applies_color(self):
        """Restored DAT should have the DAT tag color."""
        dat_path = self._root_sandbox.path + '/colored_dat'
        rel_path = 'embody/unit_tests/_test_temp/colored_dat.py'
        self._create_file(rel_path)
        self._add_table_entry(dat_path, 'text', 'py', rel_path)

        self.embody_ext.RestoreDATs()

        restored = op(dat_path)
        self.assertIsNotNone(restored, 'DAT should be restored for color check')
        expected_r = self.embody.par.Dattagcolorr.eval()
        expected_g = self.embody.par.Dattagcolorg.eval()
        expected_b = self.embody.par.Dattagcolorb.eval()
        self.assertAlmostEqual(restored.color[0], expected_r)
        self.assertAlmostEqual(restored.color[1], expected_g)
        self.assertAlmostEqual(restored.color[2], expected_b)

    # =================================================================
    # RestoreDATs - skip conditions
    # =================================================================

    def test_restore_skips_existing_dat(self):
        """DAT that already exists should not be recreated."""
        existing = self._root_sandbox.create(textDAT, 'existing_dat')
        rel_path = 'embody/unit_tests/_test_temp/existing_dat.py'
        self._create_file(rel_path)
        self._add_table_entry(existing.path, 'text', 'py', rel_path)

        self.embody_ext.RestoreDATs()
        self.assertIs(op(existing.path), existing)

    def test_restore_skips_missing_file(self):
        """Entry with no file on disk should be skipped (not crash)."""
        dat_path = self._root_sandbox.path + '/no_file_dat'
        rel_path = 'embody/unit_tests/_test_temp/no_file_dat.py'
        # Do NOT create the file
        self._add_table_entry(dat_path, 'text', 'py', rel_path)

        self.embody_ext.RestoreDATs()

        self.assertIsNone(op(dat_path), 'DAT should not be created without file')

    def test_restore_skips_embody_descendants(self):
        """DATs inside Embody's own path should never be restored."""
        embody_path = self.embody.path
        dat_path = embody_path + '/internal_dat'
        rel_path = 'embody/unit_tests/_test_temp/internal_dat.py'
        self._create_file(rel_path)
        self._add_table_entry(dat_path, 'text', 'py', rel_path)

        entries = self.embody_ext._getDATEntries()
        paths = [e[0] for e in entries]
        self.assertNotIn(dat_path, paths, 'Embody descendants should be excluded')

    def test_restore_skips_dat_inside_tox_comp(self):
        """DATs inside a TOX-strategy COMP should be excluded."""
        comp_path = self._root_sandbox.path + '/tox_parent'
        self._add_table_entry(comp_path, 'container', 'tox',
                              'embody/unit_tests/_test_temp/tox_parent.tox')

        dat_path = comp_path + '/child_dat'
        rel_path = 'embody/unit_tests/_test_temp/child_dat.py'
        self._create_file(rel_path)
        self._add_table_entry(dat_path, 'text', 'py', rel_path)

        entries = self.embody_ext._getDATEntries()
        paths = [e[0] for e in entries]
        self.assertNotIn(dat_path, paths,
                         'DATs inside TOX COMPs should be excluded')

    def test_restore_skips_dat_inside_tdn_comp(self):
        """DATs inside a TDN-strategy COMP should be excluded."""
        comp_path = self._root_sandbox.path + '/tdn_parent'
        self._add_table_entry(comp_path, 'container', 'tdn',
                              'embody/unit_tests/_test_temp/tdn_parent.tdn')

        dat_path = comp_path + '/child_dat'
        rel_path = 'embody/unit_tests/_test_temp/child_dat2.py'
        self._create_file(rel_path)
        self._add_table_entry(dat_path, 'text', 'py', rel_path)

        entries = self.embody_ext._getDATEntries()
        paths = [e[0] for e in entries]
        self.assertNotIn(dat_path, paths,
                         'DATs inside TDN COMPs should be excluded')

    def test_restore_skips_when_parent_missing(self):
        """DAT whose parent doesn't exist should be skipped gracefully."""
        dat_path = '/nonexistent_parent/orphan_dat'
        rel_path = 'embody/unit_tests/_test_temp/orphan_dat.py'
        self._create_file(rel_path)
        self._add_table_entry(dat_path, 'text', 'py', rel_path)

        # Should not raise
        self.embody_ext.RestoreDATs()
        self.assertIsNone(op(dat_path))

    def test_restore_disabled_by_toggle(self):
        """RestoreDATs should be a no-op when Datrestoreonstart is off."""
        dat_path = self._root_sandbox.path + '/toggle_off_dat'
        rel_path = 'embody/unit_tests/_test_temp/toggle_off_dat.py'
        self._create_file(rel_path)
        self._add_table_entry(dat_path, 'text', 'py', rel_path)

        orig = self.embody.par.Datrestoreonstart.eval()
        self.embody.par.Datrestoreonstart = False
        try:
            self.embody_ext.RestoreDATs()
            self.assertIsNone(op(dat_path),
                              'DAT should not be restored when toggle is off')
        finally:
            self.embody.par.Datrestoreonstart = orig

    # =================================================================
    # Continuity check hardening
    # =================================================================

    def test_continuity_protects_recoverable_dat(self):
        """Missing DAT with file on disk should NOT be removed from table."""
        dat_path = self._root_sandbox.path + '/recoverable_dat'
        rel_path = 'embody/unit_tests/_test_temp/recoverable_dat.py'
        self._create_file(rel_path)
        self._add_table_entry(dat_path, 'text', 'py', rel_path)

        self.embody_ext.checkOpsForContinuity(
            self.embody_ext.ExternalizationsFolder)

        self.assertTrue(self._table_has_path(dat_path),
                        'Recoverable DAT entry should be preserved')

    def test_continuity_removes_unrecoverable_entry(self):
        """Missing DAT with NO file on disk should be removed from table."""
        dat_path = self._root_sandbox.path + '/unrecoverable_dat'
        rel_path = 'embody/unit_tests/_test_temp/unrecoverable_dat.py'
        # Do NOT create the file
        self._add_table_entry(dat_path, 'text', 'py', rel_path)

        self.embody_ext.checkOpsForContinuity(
            self.embody_ext.ExternalizationsFolder)

        self.assertFalse(self._table_has_path(dat_path),
                         'Unrecoverable entry should be removed')

    def test_continuity_protects_recoverable_tox(self):
        """Missing TOX COMP with .tox file on disk should NOT be removed."""
        comp_path = self._root_sandbox.path + '/recoverable_tox'
        rel_path = 'embody/unit_tests/_test_temp/recoverable_tox.tox'
        self._create_file(rel_path, content='dummy tox')
        self._add_table_entry(comp_path, 'container', 'tox', rel_path)

        self.embody_ext.checkOpsForContinuity(
            self.embody_ext.ExternalizationsFolder)

        self.assertTrue(self._table_has_path(comp_path),
                        'Recoverable TOX entry should be preserved')


class TestTOXRestoration(EmbodyTestCase):
    """TOX load triggers: RestoreTOXComps, _reloadTox, ReconcileMetadata.

    TD 2025 loads an external .tox mid-session only via
    enableexternaltoxpulse; a restored COMP left empty means the trigger
    regressed (the stale-tox-restore investigation, 2026-07-18).
    """

    def setUp(self):
        self._test_dir = (Path(project.folder) / 'embody' / 'unit_tests'
                          / '_test_temp')
        self._test_dir.mkdir(parents=True, exist_ok=True)
        # Root-level sandbox outside all TDN/TOX COMPs (see
        # TestDATRestoration.setUp for why).
        self._root_sandbox = op('/').create(baseCOMP, '_test_tox_restore')
        self._added_paths = []
        self._orig_restore_pref = self.embody.par.Toxrestoreonstart.eval()
        self.embody.par.Toxrestoreonstart = True

    def tearDown(self):
        self.embody.par.Toxrestoreonstart = self._orig_restore_pref
        # Table rows FIRST -- a leaked fake tox row in the LIVE table
        # would drive real restore attempts at the next project open.
        table = self.embody_ext.Externalizations
        for path in self._added_paths:
            for i in range(table.numRows - 1, 0, -1):
                if table[i, 'path'].val == path:
                    table.deleteRow(i)
        if self._root_sandbox.valid:
            self._root_sandbox.destroy()
        for f in self._test_dir.glob('tox_restore_*'):
            try:
                f.unlink()
            except OSError:
                pass
        super().tearDown()

    # -----------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------

    def _add_tox_row(self, path, comp_type, rel_file_path):
        """Add a TOX-strategy row to the externalizations table."""
        table = self.embody_ext.Externalizations
        table.appendRow([
            path, comp_type, 'tox', rel_file_path,
            '2026-01-01 00:00:00 UTC', '', '', '',
            '0', '0', ''
        ])
        self._added_paths.append(path)

    def _export_donor_tox(self, name, rel_path):
        """Create a container with one child DAT and save it as a .tox."""
        donor = self._root_sandbox.create(containerCOMP, name)
        inner = donor.create(textDAT, 'inner')
        inner.text = 'tox restore payload'
        donor.par.externaltox = rel_path
        donor.par.enableexternaltox = True
        donor.saveExternalTox()
        return donor

    def _get_log_id(self):
        return self.embody_ext._log_counter

    def _has_log_message(self, since_id, substring):
        for entry in self.embody_ext._log_buffer:
            if entry['id'] > since_id and substring in entry.get('message', ''):
                return True
        return False

    # -----------------------------------------------------------------
    # Load-trigger contract
    # -----------------------------------------------------------------

    def test_comp_has_enableexternaltoxpulse_par(self):
        """The load trigger the restore/reload paths rely on must exist."""
        # Both COMP families the restore path commonly rebuilds
        for comp_type, name in ((containerCOMP, 'pulse_par_probe'),
                                (baseCOMP, 'pulse_par_probe_base')):
            c = self._root_sandbox.create(comp_type, name)
            self.assertLen(c.pars('enableexternaltoxpulse'), 1,
                           f'{c.type} lacks enableexternaltoxpulse -- '
                           'every Embody tox reload trigger is broken on '
                           'this build')

    def test_restore_missing_tox_comp_loads_content(self):
        """RestoreTOXComps must actually LOAD the .tox, not leave a shell."""
        rel = 'embody/unit_tests/_test_temp/tox_restore_full.tox'
        self._export_donor_tox('tox_restore_src', rel)
        comp_path = self._root_sandbox.path + '/tox_restore_full'
        self._add_tox_row(comp_path, 'container', rel)

        self.embody_ext.RestoreTOXComps()

        restored = op(comp_path)
        self.assertIsNotNone(restored, 'COMP should be restored')
        self.assertIsNotNone(restored.op('inner'),
                             'restored COMP must contain the .tox content '
                             '(empty shell = load-trigger regression)')
        self.assertTrue(restored.par.enableexternaltox.eval())
        self.assertIn('tox_restore_full.tox',
                      restored.par.externaltox.eval())

    def test_restore_unloadable_tox_fails_loud(self):
        """A .tox that cannot load must log ERROR and leave no empty shell."""
        rel = 'embody/unit_tests/_test_temp/tox_restore_bad.tox'
        bad = self.embody_ext.buildAbsolutePath(rel)
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_bytes(b'not a real tox file')
        comp_path = self._root_sandbox.path + '/tox_restore_bad'
        self._add_tox_row(comp_path, 'container', rel)

        log_id = self._get_log_id()
        self.embody_ext.RestoreTOXComps()

        self.assertTrue(
            self._has_log_message(log_id, f'Restore of {comp_path}'),
            'failed tox load must log an ERROR naming the COMP')
        shell = op(comp_path)
        # destroy() may leave a same-frame corpse; only inspect live ops
        if shell is not None and shell.valid:
            self.assertLen(list(shell.children), 0)

    def test_restore_empty_tox_comp_is_not_an_error(self):
        """A valid-but-EMPTY .tox must restore cleanly, not fail loud.

        externalTimeStamp (not child count) is the load-success signal,
        so a legitimately empty externalized container restores without
        an ERROR and without being destroyed.
        """
        rel = 'embody/unit_tests/_test_temp/tox_restore_empty.tox'
        donor = self._root_sandbox.create(containerCOMP, 'tox_restore_esrc')
        donor.par.externaltox = rel
        donor.par.enableexternaltox = True
        donor.saveExternalTox()
        comp_path = self._root_sandbox.path + '/tox_restore_empty'
        self._add_tox_row(comp_path, 'container', rel)

        log_id = self._get_log_id()
        self.embody_ext.RestoreTOXComps()

        restored = op(comp_path)
        self.assertIsNotNone(restored,
                             'empty-but-valid .tox must still restore')
        self.assertTrue(restored.valid)
        self.assertFalse(
            self._has_log_message(log_id, f'Restore of {comp_path}'),
            'a successful empty-tox restore must not log a load ERROR')

    def test_reload_tox_rereads_disk(self):
        """_reloadTox must replace live content with the .tox on disk."""
        rel = 'embody/unit_tests/_test_temp/tox_restore_reload.tox'
        donor = self._export_donor_tox('tox_restore_reload', rel)
        # Live-only change that is NOT in the saved .tox
        donor.create(textDAT, 'live_only')
        self.assertIsNotNone(donor.op('live_only'))

        self.embody_ext._reloadTox(donor)

        self.assertIsNotNone(donor.op('inner'),
                             'content from disk should be present')
        self.assertIsNone(donor.op('live_only'),
                          '_reloadTox did not re-read the .tox from disk '
                          '(live-only op survived the reload)')

    def test_reconcile_bad_row_does_not_abort_pass(self):
        """One broken row must not abort the whole ReconcileMetadata pass."""
        # Row 1 (earlier in the table): tox-strategy row pointing at a DAT.
        # The tox branch touches par.externaltox, which raises on a DAT --
        # the per-row guard must catch it and keep going.
        bad = self._root_sandbox.create(textDAT, 'reconcile_bad')
        self._add_tox_row(bad.path, 'container', 'fake/reconcile_bad.tox')
        # Row 2: valid tox COMP missing its tag -- must still be reconciled.
        rel = 'embody/unit_tests/_test_temp/tox_restore_reconcile.tox'
        good = self._export_donor_tox('tox_restore_reconcile', rel)
        tox_tag = self.embody.par.Toxtag.val
        good.tags.discard(tox_tag)
        self._add_tox_row(good.path, 'container', rel)

        self.embody_ext.ReconcileMetadata()

        self.assertIn(tox_tag, good.tags,
                      'row after a broken row was not reconciled -- the '
                      'per-row guard in ReconcileMetadata is missing')
