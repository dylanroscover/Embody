"""
Test suite: Auto-save / crash checkpoint engine.

Covers the synchronous checkpoint path and its supporting machinery:
- ExportNetwork skip_cleanup (TDXNExt) -- skips the rglob stale-scan
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
        ext._coarse_checkpoint_due = False
        # Several tests below reach code that early-returns when auto-save is
        # off, so they would pass or fail on a USER PREFERENCE rather than on
        # the behaviour under test. Force it on for the suite and put the
        # user's setting back in tearDown.
        self._autosave_par_was = None
        par = getattr(self.embody.par, 'Autosave', None)
        if par is not None:
            self._autosave_par_was = par.eval()
            par.val = True

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
        # DISARM, do not merely un-flag. Arming schedules a real run() drain
        # against the LIVE project, and clearing the flags does not cancel it:
        # a drain that fires after the suite has moved on sweeps every tracked
        # root and can write .tdn files mid-run -- with Filecleanup forced to
        # 'delete' by the runner, which is the amplifier behind the 2026-07-01
        # data loss. Bumping the generation is the invalidation the code itself
        # provides (_autosaveDrain returns immediately on a stale gen).
        ext._coarse_checkpoint_due = False
        ext._autosave_gen += 1
        if getattr(self, '_autosave_par_was', None) is not None:
            par = getattr(self.embody.par, 'Autosave', None)
            if par is not None:
                par.val = self._autosave_par_was
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
        sig = inspect.signature(self.embody.ext.TDXN.ExportNetwork)
        self.assertIn('skip_cleanup', sig.parameters)
        self.assertEqual(sig.parameters['skip_cleanup'].default, False)

    # --- Core: Checkpoint ---

    def test_checkpoint_writes_and_marks_clean(self):
        comp, abs_tdn = self._make_tdn('cp_writes')
        ok = self.embody_ext.Checkpoint(comp.path)
        self.assertTrue(ok)
        self.assertTrue(os.path.isfile(abs_tdn))
        self.assertEqual(self.embody_ext.Externalizations[comp.path, 'dirty'].val, '',
                         'tsv dirty cell stays blank (runtime-only, 2026-08-20)')
        self.assertEqual(self.embody_ext.DirtyState(comp.path), '',
                         'checkpoint clears the runtime dirty flag')

    def test_checkpoint_captures_current_state(self):
        comp, abs_tdn = self._make_tdn('cp_state')
        comp.op('n1').par.period = 12.5
        self.embody_ext.Checkpoint(comp.path)
        doc = self.embody.ext.TDXN.tdn_load(open(abs_tdn).read())
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

    # --- Stage 2b: the COARSE arm (execute_python has no path to resolve) ---

    def test_coarse_arm_sets_the_flag_and_arms_the_drain(self):
        """execute_python cannot name what it touched, so it arms a FLAG rather
        than a root -- the drain does the discovery once, after the burst."""
        ext = self.embody_ext
        ext._coarse_checkpoint_due = False
        ext._autosave_armed = False
        ext.NoteCoarseCheckpointTouch()
        self.assertTrue(ext._coarse_checkpoint_due,
                        'coarse touch must mark a sweep due')
        self.assertTrue(ext._autosave_armed,
                        'coarse touch must arm the settle-drain')
        self.assertEqual(len(ext._pending_checkpoint_roots), 0,
                         'coarse touch queues NO root -- that is the point')

    def test_coarse_expansion_queues_only_dirty_roots(self):
        """The sweep must not enqueue every tracked root: writing a .tdn per
        agent call is the churn this design exists to avoid."""
        comp, _ = self._make_tdn('coarse_dirty')
        ext = self.embody_ext
        # Scoped to OUR sandbox root, never a project-wide count: the live
        # project legitimately carries dirty roots from other work, and an
        # assertion of global cleanliness fails for reasons that have nothing
        # to do with the sweep (observed: 3 unrelated dirty roots).
        ext._pending_checkpoint_roots.clear()
        ext._queueDirtyTDNRoots()
        self.assertNotIn(comp.path, ext._pending_checkpoint_roots,
                         'a freshly externalized (clean) root must NOT be queued')
        comp.op('n1').par.period = 9.0          # now genuinely dirty
        ext._pending_checkpoint_roots.clear()
        ext._queueDirtyTDNRoots()
        self.assertIn(comp.path, ext._pending_checkpoint_roots,
                      'a changed root must be discovered by the sweep')

    def test_coarse_sweep_is_bounded(self):
        """One sweep can never become the frame cost it exists to avoid.

        Asserting the constant exists proves nothing -- the property is that
        the sweep RESPECTS it. Two genuinely dirty roots against a cap of one:
        drop the slice and both are queued.
        """
        ext = self.embody_ext
        a, _ = self._make_tdn('cap_a')
        b, _ = self._make_tdn('cap_b')
        a.op('n1').par.period = 4.0
        b.op('n1').par.period = 5.0
        ext._pending_checkpoint_roots.clear()
        ext._COARSE_SWEEP_CAP = 1        # instance shadow; removed below
        try:
            queued = ext._queueDirtyTDNRoots()
        finally:
            del ext._COARSE_SWEEP_CAP    # back to the class attribute
        self.assertLessEqual(queued, 1,
                             'the sweep examined more roots than its cap allows')
        self.assertGreater(type(ext)._COARSE_SWEEP_CAP, 0,
                           'the shipped cap must still bound a real sweep')

    # --- Stage 2d: the WIRING (the chokepoint must actually call all this) ---

    def test_arbitrary_code_tools_arm_the_coarse_sweep(self):
        """The wiring, not the helper: the MCP chokepoint must arm coarsely for
        BOTH tools that run arbitrary code. Deleting the branch leaves an agent
        session checkpointing nothing, which is the bug this whole stage fixes."""
        ext = self.embody_ext
        for operation in ('execute_python', 'exec_op_method'):
            ext._coarse_checkpoint_due = False
            ext._autosave_armed = False
            op.Embody.ext.Envoy._noteCheckpointActivity(operation, {}, None)
            self.assertTrue(ext._coarse_checkpoint_due,
                            f'{operation} must arm the coarse sweep')
            self.assertTrue(ext._autosave_armed,
                            f'{operation} must arm the settle-drain')
            self.assertEqual(len(ext._pending_checkpoint_roots), 0,
                             f'{operation} must queue NO root -- it has no path')

    def test_a_typed_op_still_arms_by_path_not_coarsely(self):
        """The coarse arm must not swallow the path-resolving branch: a tool
        that CAN name its root still queues that root and sweeps nothing."""
        comp, _ = self._make_tdn('wired_typed')
        ext = self.embody_ext
        ext._coarse_checkpoint_due = False
        ext._pending_checkpoint_roots.clear()
        op.Embody.ext.Envoy._noteCheckpointActivity(
            'set_parameter', {'op_path': comp.op('n1').path}, None)
        self.assertIn(comp.path, ext._pending_checkpoint_roots,
                      'a typed op must queue the root it touched')
        self.assertFalse(ext._coarse_checkpoint_due,
                         'a typed op must NOT trigger the project-wide sweep')

    def test_both_arbitrary_code_tools_share_one_guard_set(self):
        """The arm and the pre-flush read the SAME set. Two literals would drift
        into 'one tool is guarded, the other is not' -- which is how
        exec_op_method sat unwatched while execute_python was fixed."""
        coarse = op.Embody.ext.Envoy._COARSE_CHECKPOINT_OPS
        self.assertIn('execute_python', coarse)
        self.assertIn('exec_op_method', coarse)
        # Read the DAT, not inspect.getsource: an extension class backed by a
        # DAT has no file the inspect module can find ("source code not
        # available"), which is why this assertion has to go to the source of
        # truth TD actually loaded.
        src = op.Embody.op('EnvoyExt').text
        self.assertIn('elif operation in self._COARSE_CHECKPOINT_OPS:', src,
                      'the pre-flush must gate on the shared set, not a literal')

    # --- Stage 2c: the pre-flush (ordering guard before arbitrary code) ---

    def test_flush_writes_queued_roots_and_clears_them(self):
        """A root queued by an earlier tool sits unwritten for up to the settle
        window; execute_python can crash TD inside it. Flush first."""
        comp, abs_tdn = self._make_tdn('flush_q')
        ext = self.embody_ext
        comp.op('n1').par.period = 3.0
        ext._pending_checkpoint_roots.clear()
        ext._pending_checkpoint_roots.add(comp.path)
        written = ext.FlushPendingCheckpoints()
        self.assertGreaterEqual(written, 1, 'a queued root must be written')
        self.assertNotIn(comp.path, ext._pending_checkpoint_roots,
                         'a written root must leave the queue')

    def test_flush_is_a_no_op_when_nothing_is_queued(self):
        """The steady state: this runs before EVERY execute_python, so it must
        cost an empty-set check and never sweep for new dirt."""
        ext = self.embody_ext
        ext._pending_checkpoint_roots.clear()
        self.assertEqual(ext.FlushPendingCheckpoints(), 0)
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
        # resurrect it (the delete-undo guard). _delete_op calls
        # _purgeExternalizationTracking.
        comp, abs_tdn = self._make_tdn('del_undo')
        self.embody_ext.Checkpoint(comp.path)
        comp_path = comp.path
        self.embody_ext._purgeExternalizationTracking(comp_path)
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
        # The two arbitrary-code tools stay out of THIS set because it resolves
        # a path and they have none to resolve -- they are armed coarsely
        # instead (see the Stage 2d wiring tests), not left unwatched.
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
        self.embody_ext._purgeExternalizationTracking(comp.path)
        tracked = [p for p, _ in self.embody_ext._getTDNStrategyComps()]
        self.assertNotIn(comp.path, tracked)
        self.assertIn(sib.path, tracked)  # sibling must survive

    def test_purge_removes_non_tdn_rows(self):
        # delete_op on an externalized DAT must remove its table row --
        # previously only TDN rows were purged, leaving an orphan row + file
        # behind until a Refresh sweep (issue #57 follow-up, 2026-07-16).
        # The row removal is synchronous (asserted here); the file deletion
        # is deferred via run(..., delayFrames=5) like the TDN path, so it
        # cannot be observed inside a synchronous test -- clean it manually.
        dat = self.sandbox.create(textDAT, 'purge_dat')
        dat.text = '# purge me'
        ext = self.embody_ext
        ext.applyTagToOperator(dat, self.embody.par.Pytag.eval())
        ext.ExternalizeImmediate(dat)
        dat_path = dat.path
        table = ext.Externalizations
        rows = [i for i in range(1, table.numRows)
                if table[i, 'path'].val == dat_path]
        self.assertEqual(len(rows), 1, 'externalize should add one row')
        rel = table[rows[0], 'rel_file_path'].val
        abs_file = ext.buildAbsolutePath(ext.normalizePath(rel)).resolve()
        try:
            self.assertTrue(abs_file.is_file(),
                            'externalized file should exist')
            ext._purgeExternalizationTracking(dat_path)
            dat.destroy()
            rows_after = [i for i in range(1, table.numRows)
                          if table[i, 'path'].val == dat_path]
            self.assertEqual(rows_after, [],
                             'row must be purged with the op (any strategy)')
        finally:
            # The deferred _delete fires ~5 frames later and no-ops if the
            # file is already gone; remove it now so no orphan outlives the
            # test regardless of assertion outcome. Same for the table row:
            # a failed assertion must not leave an orphan row for a destroyed
            # sandbox op in the LIVE externalizations table.
            try:
                abs_file.unlink()
            except OSError:
                pass
            for i in range(table.numRows - 1, 0, -1):
                if table[i, 'path'].val == dat_path:
                    table.deleteRow(i)

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
