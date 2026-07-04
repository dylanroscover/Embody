"""
Test suite: TDN stability hardening (2026-07-03 audit regressions).

Pins the fixes from the TDN stability/data-resiliency audit:

  A. _isDATEditable uses DAT.isEditable -- the old write-probe
     (dat.text = dat.text) corrupted live table cells containing
     embedded newlines/tabs on every export.
  B. _exportFlags compares against per-OPType CREATION flag values --
     the global DEFAULT_FLAGS table lost "render off"/"display off"
     on object COMPs (geometryCOMP creates with render/display ON).
  C. ImportNetwork validates document structure BEFORE clear_first --
     a malformed hand-edited .tdn could destroy children and then
     raise, leaving the COMP empty with no error result.
  D. _trackTDNExport only APPENDS a table row for TDN-tagged COMPs --
     an ad-hoc file export no longer silently enrolls an untagged COMP
     in the save-strip/reconstruction lifecycle.
  E. Stale-file cleanup deletion candidates are restricted to files the
     externalizations table tracks -- untracked strays are never
     Embody's to delete.
  F. RecoverOrphanShells restores TDN-tagged empty COMPs whose table
     row was lost, via the _tdn_rel_path storage pointer or the
     mirror-path convention.
"""

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase


class TestTDNStabilityHardening(EmbodyTestCase):

    def setUp(self):
        super().setUp()
        self.tdn = self.embody.ext.TDN
        self._temp_files = []
        self._temp_rows = []

    def tearDown(self):
        # Remove any table rows this test enrolled (API path, never the
        # tsv directly), keeping their files for our own unlink below.
        for path in self._temp_rows:
            try:
                self.embody_ext._removeTDNStrategy(path, delete_file=False)
            except Exception:
                pass
        from pathlib import Path
        for f in self._temp_files:
            try:
                p = Path(f)
                if p.is_file():
                    p.unlink()
            except Exception:
                pass
        super().tearDown()

    # -----------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------

    def _tagged_comp_with_child(self, name):
        """Create a TDN-tagged COMP holding one child, inside the sandbox."""
        comp = self.sandbox.create(baseCOMP, name)
        comp.tags.add(self.embody.par.Tdntag.val)
        comp.create(textDAT, 'payload').text = 'payload text'
        return comp

    def _mirror_tdn_path(self, comp):
        """The convention path <project>/<comp path>.tdn as a Path."""
        from pathlib import Path
        return Path(project.folder) / (comp.path.lstrip('/') + '.tdn')

    # =================================================================
    # A. Non-mutating DAT editability (table-cell corruption regression)
    # =================================================================

    def test_A01_export_preserves_live_table_cells(self):
        """Export must NOT strip embedded newlines/tabs from live tables."""
        tbl = self.sandbox.create(tableDAT, 'cells_tbl')
        tbl.clear()
        tbl.appendRow(['key', 'value'])
        tbl.appendRow(['multiline', 'line1\nline2'])
        tbl.appendRow(['tabbed', 'a\tb'])
        result = self.tdn.ExportNetwork(
            root_path=self.sandbox.path, include_dat_content=True)
        self.assertTrue(result.get('success'))
        # The LIVE table is untouched by the export.
        self.assertEqual(tbl[1, 1].val, 'line1\nline2',
                         'export corrupted an embedded newline in a live cell')
        self.assertEqual(tbl[2, 1].val, 'a\tb',
                         'export corrupted an embedded tab in a live cell')
        # And the exported document carries the exact cell values.
        entry = next(o for o in result['tdn']['operators']
                     if o['name'] == 'cells_tbl')
        self.assertEqual(entry['dat_content'][1][1], 'line1\nline2')
        self.assertEqual(entry['dat_content'][2][1], 'a\tb')

    def test_A02_table_cell_special_chars_round_trip(self):
        tbl = self.sandbox.create(tableDAT, 'rt_tbl')
        tbl.clear()
        tbl.appendRow(['multiline', 'line1\nline2'])
        tbl.appendRow(['tabbed', 'a\tb'])
        exported = self.tdn.ExportNetwork(
            root_path=self.sandbox.path, include_dat_content=True)['tdn']
        dest = self.sandbox.create(baseCOMP, 'rt_dest')
        res = self.tdn.ImportNetwork(dest.path, exported)
        self.assertTrue(res.get('success'))
        clone = dest.op('rt_tbl')
        self.assertIsNotNone(clone)
        self.assertEqual(clone[0, 1].val, 'line1\nline2')
        self.assertEqual(clone[1, 1].val, 'a\tb')

    def test_A03_read_only_companion_still_marked(self):
        """glsl info DATs must still export as dat_read_only, not content."""
        self.sandbox.create(glslTOP, 'glsl_ro')
        info = self.sandbox.op('glsl_ro_info')
        self.assertIsNotNone(info, 'glsl info companion not found')
        result = self.tdn.ExportNetwork(
            root_path=self.sandbox.path, include_dat_content=True)
        entry = next((o for o in result['tdn']['operators']
                      if o['name'] == 'glsl_ro_info'), None)
        self.assertIsNotNone(entry, 'info DAT missing from export')
        self.assertTrue(entry.get('dat_read_only'),
                        'read-only companion no longer flagged dat_read_only')
        self.assertNotIn('dat_content', entry)

    def test_A04_locked_dat_content_still_exports(self):
        """Locked DATs remain editable per TD -- content must round-trip."""
        dat = self.sandbox.create(textDAT, 'locked_txt')
        dat.text = 'frozen payload'
        dat.lock = True
        try:
            result = self.tdn.ExportNetwork(
                root_path=self.sandbox.path, include_dat_content=True)
            entry = next(o for o in result['tdn']['operators']
                         if o['name'] == 'locked_txt')
            self.assertEqual(entry.get('dat_content'), 'frozen payload')
            self.assertIn('lock', entry.get('flags', []))
        finally:
            dat.lock = False

    # =================================================================
    # B. Per-type creation flag defaults (object-COMP render/display)
    # =================================================================

    def test_B01_geo_render_off_exports_negative_flags(self):
        geo = self.sandbox.create(geometryCOMP, 'geo_off')
        geo.render = False
        geo.display = False
        result = self.tdn.ExportNetwork(root_path=self.sandbox.path)
        entry = next(o for o in result['tdn']['operators']
                     if o['name'] == 'geo_off')
        flags = entry.get('flags', [])
        self.assertIn('-render', flags,
                      'geo render-off not captured (creation default is ON)')
        self.assertIn('-display', flags,
                      'geo display-off not captured (creation default is ON)')

    def test_B02_geo_defaults_export_no_render_flags(self):
        self.sandbox.create(geometryCOMP, 'geo_def')
        result = self.tdn.ExportNetwork(root_path=self.sandbox.path)
        entry = next(o for o in result['tdn']['operators']
                     if o['name'] == 'geo_def')
        flags = entry.get('flags', [])
        for noisy in ('render', 'display', '-render', '-display'):
            self.assertNotIn(noisy, flags,
                             f'default geo exported spurious flag {noisy}')

    def test_B03_geo_render_off_round_trips(self):
        """The production bug: hidden geo reappearing after strip/restore."""
        geo = self.sandbox.create(geometryCOMP, 'geo_rt')
        geo.render = False
        geo.display = False
        exported = self.tdn.ExportNetwork(root_path=self.sandbox.path)['tdn']
        res = self.tdn.ImportNetwork(
            self.sandbox.path, exported, clear_first=True)
        self.assertTrue(res.get('success'))
        rebuilt = self.sandbox.op('geo_rt')
        self.assertIsNotNone(rebuilt)
        self.assertFalse(rebuilt.render,
                         'render=False lost through TDN round-trip')
        self.assertFalse(rebuilt.display,
                         'display=False lost through TDN round-trip')

    def test_B04_common_types_flag_export_unchanged(self):
        """noiseTOP/textDAT creation flags match DEFAULT_FLAGS -- viewer=True
        must still export exactly as before the per-type baseline."""
        n = self.sandbox.create(noiseTOP, 'noise_v')
        n.viewer = True
        result = self.tdn.ExportNetwork(root_path=self.sandbox.path)
        entry = next(o for o in result['tdn']['operators']
                     if o['name'] == 'noise_v')
        self.assertIn('viewer', entry.get('flags', []))

    # =================================================================
    # C. Validate-before-destroy (malformed hand-edited .tdn)
    # =================================================================

    def test_C01_malformed_operator_entry_never_destroys_children(self):
        keeper = self.sandbox.create(textDAT, 'keeper')
        keeper.text = 'must survive'
        bad_doc = {
            'format': 'tdn', 'version': '2.0',
            'operators': [
                {'name': 'ok', 'type': 'noiseTOP'},
                'stray scalar from a hand edit',
            ],
        }
        result = self.tdn.ImportNetwork(
            self.sandbox.path, bad_doc, clear_first=True)
        self.assertDictHasKey(result, 'error')
        self.assertIsNotNone(self.sandbox.op('keeper'),
                             'clear_first destroyed children before '
                             'structural validation rejected the document')
        self.assertEqual(self.sandbox.op('keeper').text, 'must survive')

    def test_C02_malformed_children_never_destroys_children(self):
        keeper = self.sandbox.create(textDAT, 'keeper2')
        bad_doc = {
            'format': 'tdn', 'version': '2.0',
            'operators': [
                {'name': 'parent', 'type': 'baseCOMP',
                 'children': 'not a list'},
            ],
        }
        result = self.tdn.ImportNetwork(
            self.sandbox.path, bad_doc, clear_first=True)
        self.assertDictHasKey(result, 'error')
        self.assertIsNotNone(self.sandbox.op('keeper2'))

    def test_C03_malformed_par_templates_degrades_gracefully(self):
        """Non-dict par_templates/type_defaults are ignored with a warning;
        the rest of the document still imports."""
        doc = {
            'format': 'tdn', 'version': '2.0',
            'par_templates': 'garbage',
            'type_defaults': ['also', 'garbage'],
            'operators': [{'name': 'n1', 'type': 'noiseTOP'}],
        }
        result = self.tdn.ImportNetwork(self.sandbox.path, doc)
        self.assertTrue(result.get('success'),
                        f'graceful degradation failed: {result}')
        self.assertIsNotNone(self.sandbox.op('n1'))

    def test_C04_validate_op_defs_pure(self):
        v = self.tdn._validateOpDefs
        self.assertIsNone(v([]))
        self.assertIsNone(v([{'name': 'a', 'type': 'noiseTOP'}]))
        self.assertIsNone(v([{'name': 'a', 'type': 'baseCOMP',
                              'children': [{'name': 'b', 'type': 'nullTOP'}]}]))
        self.assertIsNotNone(v('nope'))
        self.assertIsNotNone(v(['scalar']))
        self.assertIsNotNone(v([{'name': 'a', 'children': 'bad'}]))
        self.assertIsNotNone(v([{'name': 'a',
                                 'children': [['nested', 'junk']]}]))

    # =================================================================
    # D. Deliberate enrollment (tracking gate)
    # =================================================================

    def test_D01_untagged_export_writes_file_but_no_row(self):
        comp = self.sandbox.create(baseCOMP, 'adhoc_untagged')
        comp.create(textDAT, 'x')
        out = self._mirror_tdn_path(comp)
        self._temp_files.append(str(out))
        result = self.tdn.ExportNetwork(
            root_path=comp.path, output_file=str(out))
        self.assertTrue(result.get('success'))
        self.assertTrue(out.is_file(), 'export did not write the file')
        table = self.embody_ext.Externalizations
        rows = [table[i, 'path'].val for i in range(1, table.numRows)]
        self.assertNotIn(comp.path, rows,
                         'ad-hoc export of an UNTAGGED COMP appended a table '
                         'row (silent lifecycle enrollment)')

    def test_D02_tagged_export_appends_row_and_pointer(self):
        comp = self._tagged_comp_with_child('adhoc_tagged')
        out = self._mirror_tdn_path(comp)
        self._temp_files.append(str(out))
        self._temp_rows.append(comp.path)
        result = self.tdn.ExportNetwork(
            root_path=comp.path, output_file=str(out))
        self.assertTrue(result.get('success'))
        table = self.embody_ext.Externalizations
        rows = [table[i, 'path'].val for i in range(1, table.numRows)]
        self.assertIn(comp.path, rows, 'tagged export did not append a row')
        pointer = comp.fetch('_tdn_rel_path', None, search=False)
        self.assertIsNotNone(pointer, 'recovery pointer not stamped')

    def test_D03_recovery_pointer_not_serialized(self):
        comp = self._tagged_comp_with_child('pointer_skip')
        comp.store('_tdn_rel_path', 'embody/fake.tdn')
        result = self.tdn.ExportNetwork(root_path=self.sandbox.path)
        entry = next(o for o in result['tdn']['operators']
                     if o['name'] == 'pointer_skip')
        self.assertNotIn('_tdn_rel_path', entry.get('storage', {}),
                         '_tdn_rel_path leaked into the .tdn document')

    # =================================================================
    # E. Tracked-only stale-cleanup candidates
    # =================================================================

    def test_E01_restrict_to_tracked_drops_strays(self):
        from pathlib import Path
        tracked_files = self.embody_ext._getAllTrackedTDNFiles()
        self.assertTrue(tracked_files, 'live project has no tracked .tdn')
        stray = str(Path(project.folder) / 'embody' / 'unit_tests'
                    / '_stray_never_tracked.tdn')
        kept = self.tdn._restrictToTrackedTDN({tracked_files[0], stray})
        self.assertIn(tracked_files[0], kept)
        self.assertNotIn(stray, kept,
                         'untracked stray survived into deletion candidates')

    def test_E02_export_does_not_delete_untracked_stray(self):
        from pathlib import Path
        comp = self._tagged_comp_with_child('stray_guard')
        out = self._mirror_tdn_path(comp)
        self._temp_files.append(str(out))
        self._temp_rows.append(comp.path)
        # Plant an untracked stray INSIDE the export root's mirror subtree --
        # exactly what the old sweep reclaimed as "stale".
        stray = Path(project.folder) / (comp.path.lstrip('/') + '/keep_me.tdn')
        stray.parent.mkdir(parents=True, exist_ok=True)
        stray.write_text('format: tdn\noperators: []\n', encoding='utf-8')
        self._temp_files.append(str(stray))
        result = self.tdn.ExportNetwork(
            root_path=comp.path, output_file=str(out))
        self.assertTrue(result.get('success'))
        self.assertTrue(stray.is_file(),
                        'stale cleanup deleted an untracked stray .tdn')

    # =================================================================
    # F. Orphan-shell recovery (lost table row)
    # =================================================================

    def test_F01_orphan_recovered_via_storage_pointer(self):
        comp = self._tagged_comp_with_child('orphan_ptr')
        out = self._mirror_tdn_path(comp)
        self._temp_files.append(str(out))
        result = self.tdn.ExportNetwork(
            root_path=comp.path, output_file=str(out))
        self.assertTrue(result.get('success'))
        # Simulate the tsv losing the row, then the shell opening empty.
        self.embody_ext._removeTDNStrategy(comp.path, delete_file=False)
        for child in list(comp.children):
            child.destroy()
        self.assertEqual(len(comp.children), 0)
        res = self.embody_ext.RecoverOrphanShells(auto=True)
        self.assertIn(comp.path, res.get('restored', []),
                      f'orphan not restored: {res}')
        self.assertIsNotNone(comp.op('payload'),
                             'children not rebuilt from the .tdn')
        table = self.embody_ext.Externalizations
        rows = [table[i, 'path'].val for i in range(1, table.numRows)]
        self.assertIn(comp.path, rows, 'orphan not re-tracked after restore')
        self._temp_rows.append(comp.path)

    def test_F02_orphan_recovered_via_convention_path(self):
        comp = self._tagged_comp_with_child('orphan_conv')
        out = self._mirror_tdn_path(comp)
        self._temp_files.append(str(out))
        result = self.tdn.ExportNetwork(
            root_path=comp.path, output_file=str(out))
        self.assertTrue(result.get('success'))
        self.embody_ext._removeTDNStrategy(comp.path, delete_file=False)
        comp.unstore('_tdn_rel_path')  # force the convention-path fallback
        for child in list(comp.children):
            child.destroy()
        res = self.embody_ext.RecoverOrphanShells(auto=True)
        self.assertIn(comp.path, res.get('restored', []),
                      f'convention-path orphan not restored: {res}')
        self.assertIsNotNone(comp.op('payload'))
        self._temp_rows.append(comp.path)

    def test_F03_tracked_empty_comp_is_not_an_orphan(self):
        """A rowed COMP that is merely empty (e.g. mid save-strip) must be
        left to normal reconstruction, never claimed by orphan recovery."""
        comp = self._tagged_comp_with_child('not_orphan')
        out = self._mirror_tdn_path(comp)
        self._temp_files.append(str(out))
        self._temp_rows.append(comp.path)
        result = self.tdn.ExportNetwork(
            root_path=comp.path, output_file=str(out))
        self.assertTrue(result.get('success'))
        for child in list(comp.children):
            child.destroy()
        res = self.embody_ext.RecoverOrphanShells(auto=True)
        self.assertNotIn(comp.path, res.get('found', []),
                         'tracked empty COMP wrongly flagged as an orphan')

    def test_F04_comp_with_content_is_not_an_orphan(self):
        comp = self._tagged_comp_with_child('busy_comp')
        # No row (untagged exports don't enroll; this comp IS tagged but was
        # never exported) and a matching convention file on disk:
        out = self._mirror_tdn_path(comp)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text('format: tdn\noperators: []\n', encoding='utf-8')
        self._temp_files.append(str(out))
        res = self.embody_ext.RecoverOrphanShells(auto=True)
        self.assertNotIn(comp.path, res.get('found', []),
                         'COMP with live content wrongly flagged as orphan')
