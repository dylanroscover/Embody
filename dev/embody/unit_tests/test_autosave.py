"""
Test suite: Auto-save / crash checkpoint engine.

Covers the synchronous checkpoint path and its supporting machinery:
- ExportNetwork skip_cleanup (TDNExt) -- skips the rglob stale-scan
- EmbodyExt.Checkpoint() -- frame-cheap synchronous .tdn write + clean mark
- the touched-boundary recorder (NoteCheckpointTouch walk-up resolution)
- the idle-settle drain queue (_pending_checkpoint_roots)
- export-mode missing-only recovery (_recoverMissingTDNComps)
- the checkpoint-relevant mutating set (EnvoyExt)
- the pre-risky guard

Each test that externalizes a COMP cleans up its .tdn + tsv row in tearDown.
"""

import os
import inspect
from pathlib import Path

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase


class TestAutosave(EmbodyTestCase):

    def setUp(self):
        self._tdn_cleanup = []  # (comp_path, abs_tdn)
        ext = self.embody_ext
        ext._pending_checkpoint_roots.clear()
        ext._autosave_armed = False

    def tearDown(self):
        ext = self.embody_ext
        backup_dir = Path(project.folder) / '.tdn_backup'
        for comp_path, abs_tdn in self._tdn_cleanup:
            try:
                ext._removeTDNStrategy(comp_path, delete_file=True)
            except Exception:
                pass
            try:
                if abs_tdn and os.path.isfile(abs_tdn):
                    os.remove(abs_tdn)
            except OSError:
                pass
            try:
                # backups mirror the .tdn's relative path under .tdn_backup
                for bak in backup_dir.rglob(os.path.basename(abs_tdn) + '*'):
                    bak.unlink()
            except Exception:
                pass
        ext._pending_checkpoint_roots.clear()
        ext._autosave_armed = False
        super().tearDown()

    def _make_tdn(self, name):
        """Create + externalize a small TDN COMP in the sandbox; track for cleanup."""
        ext = self.embody_ext
        comp = self.sandbox.create(baseCOMP, name)
        comp.create(noiseTOP, 'n1')
        ext.applyTagToOperator(comp, 'tdn')
        ext.ExternalizeImmediate(comp)
        rel = ext._getStrategyFilePath(comp.path, 'tdn')
        abs_tdn = str(ext.buildAbsolutePath(rel)) if rel else None
        self._tdn_cleanup.append((comp.path, abs_tdn))
        return comp, abs_tdn

    # --- Stage 1: skip_cleanup ---

    def test_export_network_has_skip_cleanup_param(self):
        sig = inspect.signature(self.embody.ext.TDN.ExportNetwork)
        self.assertIn('skip_cleanup', sig.parameters)
        self.assertEqual(sig.parameters['skip_cleanup'].default, False)

    # --- Core: Checkpoint ---

    def test_checkpoint_writes_and_marks_clean(self):
        comp, abs_tdn = self._make_tdn('cp_writes')
        ok = self.embody_ext.Checkpoint(comp.path)
        self.assertTrue(ok)
        self.assertTrue(os.path.isfile(abs_tdn))
        self.assertEqual(self.embody_ext.Externalizations[comp.path, 'dirty'].val, '')

    def test_checkpoint_captures_current_state(self):
        comp, abs_tdn = self._make_tdn('cp_state')
        comp.op('n1').par.period = 12.5
        self.embody_ext.Checkpoint(comp.path)
        doc = self.embody.ext.TDN.tdn_load(open(abs_tdn).read())
        period = None
        for o in doc.get('operators', []):
            if o.get('name') == 'n1':
                period = o.get('parameters', {}).get('period')
        self.assertEqual(period, 12.5)

    def test_checkpoint_returns_false_for_untracked(self):
        comp = self.sandbox.create(baseCOMP, 'cp_untracked')
        self.assertFalse(self.embody_ext.Checkpoint(comp.path))

    # --- Stage 2: touched-boundary recorder ---

    def test_note_touch_resolves_child_to_boundary(self):
        comp, _ = self._make_tdn('touch_res')
        ext = self.embody_ext
        ext._pending_checkpoint_roots.clear()
        ext.NoteCheckpointTouch(comp.path + '/n1')
        self.assertIn(comp.path, ext._pending_checkpoint_roots)

    def test_note_touch_ignores_untracked_path(self):
        ext = self.embody_ext
        ext._pending_checkpoint_roots.clear()
        ext.NoteCheckpointTouch('/no/such/op')
        self.assertEqual(len(ext._pending_checkpoint_roots), 0)

    # --- Stage 6: export-mode missing-only recovery ---

    def test_recover_missing_rebuilds_crash_lost_comp(self):
        comp, abs_tdn = self._make_tdn('recov')
        comp.op('n1').par.period = 4.0
        self.embody_ext.Checkpoint(comp.path)
        comp_path = comp.path
        comp.destroy()  # simulate a crash: net loses it, .tdn + row persist
        self.assertIsNone(op(comp_path))
        self.embody_ext._recoverMissingTDNComps()
        rebuilt = op(comp_path)
        self.assertIsNotNone(rebuilt)
        self.assertIsNotNone(rebuilt.op('n1'))

    def test_delete_purges_tracking_no_resurrection(self):
        # Deleting a tracked TDN COMP must purge its row so recovery can't
        # resurrect it (the delete-undo guard). _delete_op calls _purgeTDNTracking.
        comp, abs_tdn = self._make_tdn('del_undo')
        self.embody_ext.Checkpoint(comp.path)
        comp_path = comp.path
        self.embody_ext._purgeTDNTracking(comp_path)
        comp.destroy()
        tracked = [cp for cp, _ in self.embody_ext._getTDNStrategyComps()]
        self.assertNotIn(comp_path, tracked)
        self.embody_ext._recoverMissingTDNComps()
        self.assertIsNone(op(comp_path))  # NOT resurrected

    # --- EnvoyExt: checkpoint-relevant mutating set ---

    def test_checkpoint_mutating_set_covers_destructive_ops(self):
        s = self.embody.ext.Envoy._CHECKPOINT_MUTATING_OPS
        for o in ('create_op', 'delete_op', 'disconnect_op', 'layout_children',
                  'set_annotation', 'set_parameter', 'import_network'):
            self.assertIn(o, s)
        # execute_python / exec_op_method are A1 skip+document -- excluded
        self.assertNotIn('execute_python', s)
        self.assertNotIn('exec_op_method', s)

    # --- pre-risky guard ---

    def test_prerisky_noop_on_nonclearing_import(self):
        # A non-clearing import destroys no state -> no checkpoint, no exception.
        before = set(self.embody_ext._pending_checkpoint_roots)
        self.embody_ext._preRiskyCheckpoint(
            'import_network', {'target_path': '/x', 'clear_first': False})
        self.assertEqual(set(self.embody_ext._pending_checkpoint_roots), before)

    # --- gates: save window + Perform Mode ---

    def test_save_window_gates_checkpoint(self):
        # Table mutation during the save window is fatal -- Checkpoint must bail.
        comp, abs_tdn = self._make_tdn('savewin')
        self.embody.store('_suppress_dialogs', True)
        try:
            ok = self.embody_ext.Checkpoint(comp.path)
            self.assertFalse(ok)
        finally:
            self.embody.unstore('_suppress_dialogs')

    def test_perform_mode_bypasses_engine(self):
        comp, _ = self._make_tdn('perf')
        ext = self.embody_ext
        par = self.embody.par.Performmode  # _performMode reads this par
        old = par.eval()
        par.val = True
        try:
            self.assertTrue(ext._performMode)
            self.assertFalse(ext.Checkpoint(comp.path))
            ext._pending_checkpoint_roots.clear()
            ext.NoteCheckpointTouch(comp.path + '/n1')
            self.assertEqual(len(ext._pending_checkpoint_roots), 0)
        finally:
            par.val = old

    # --- delete-undo prefix-sibling safety ---

    def test_purge_does_not_over_purge_prefix_sibling(self):
        comp, _ = self._make_tdn('cp')
        sib, _ = self._make_tdn('cp2')   # shares the 'cp' prefix
        self.embody_ext._purgeTDNTracking(comp.path)
        tracked = [p for p, _ in self.embody_ext._getTDNStrategyComps()]
        self.assertNotIn(comp.path, tracked)
        self.assertIn(sib.path, tracked)  # sibling must survive

    # --- recorder end-to-end (through _noteCheckpointActivity) ---

    def test_recorder_endtoend_resolves_and_queues(self):
        comp, _ = self._make_tdn('rec_e2e')
        ext = self.embody_ext
        ext._pending_checkpoint_roots.clear()
        # drive the EnvoyExt chokepoint recorder for a real op shape
        self.embody.ext.Envoy._noteCheckpointActivity(
            'set_parameter', {'op_path': comp.path + '/n1'}, {'success': True})
        self.assertIn(comp.path, ext._pending_checkpoint_roots)

    def test_recorder_ignores_readonly_ops(self):
        comp, _ = self._make_tdn('rec_ro')
        ext = self.embody_ext
        ext._pending_checkpoint_roots.clear()
        self.embody.ext.Envoy._noteCheckpointActivity(
            'get_op', {'op_path': comp.path + '/n1'}, {'success': True})
        self.assertEqual(len(ext._pending_checkpoint_roots), 0)

    # --- drain gen-token (superseded re-arm collapses) ---

    def test_drain_stale_gen_is_noop(self):
        ext = self.embody_ext
        comp, _ = self._make_tdn('gen')
        ext._pending_checkpoint_roots.clear()
        ext._pending_checkpoint_roots.add(comp.path)
        ext._autosave_gen = 5
        # a stale generation must NOT drain
        ext._autosaveDrain(3)
        self.assertIn(comp.path, ext._pending_checkpoint_roots)

    # --- nested TDN child recovery (the missing-at-start fix) ---

    def test_recover_nested_tdn_child(self):
        ext = self.embody_ext
        parent = self.sandbox.create(baseCOMP, 'np')
        parent.create(noiseTOP, 'pn')
        child = parent.create(baseCOMP, 'nc')
        child.create(rampTOP, 'cn')
        for c in (parent, child):
            ext.applyTagToOperator(c, 'tdn')
            ext.ExternalizeImmediate(c)
            rel = ext._getStrategyFilePath(c.path, 'tdn')
            self._tdn_cleanup.append((c.path, str(ext.buildAbsolutePath(rel)) if rel else None))
        ext.Checkpoint(parent.path)
        ext.Checkpoint(child.path)
        ppath, cpath = parent.path, child.path
        parent.destroy()  # destroys child too -- both missing
        self.assertIsNone(op(ppath))
        ext._recoverMissingTDNComps()
        # parent AND nested child must be rebuilt with their OWN content
        self.assertIsNotNone(op(ppath))
        self.assertIsNotNone(op(cpath), 'nested child not rebuilt')
        self.assertIsNotNone(op(cpath + '/cn'), 'nested child left an empty shell')

    # --- enabled toggle ---

    def test_autosave_enabled_reflects_toggle(self):
        p = getattr(self.embody.par, 'Autosave', None)
        self.assertIsNotNone(p)
        old = p.eval()
        try:
            p.val = True
            self.assertTrue(self.embody_ext._autosaveEnabled())
            p.val = False
            self.assertFalse(self.embody_ext._autosaveEnabled())
        finally:
            p.val = old
