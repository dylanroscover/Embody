"""Unit tests for ExportPortableTox release hooks (issue #74).

Copy-mode (default): the export stages a throwaway copy of the target
in /sys/quiet; pre_release runs ON THE COPY (shaping the artifact, live
comp untouched); both hook DATs are deleted from the copy so hook code
never ships; post_release runs on the ORIGINAL after the save -- even
when the save failed -- with (save_path, success). A pre_release raise
aborts and keeps the staged copy (renamed *_release_failed) for
inspection. hook_mode='live' restores in-place semantics (hooks mutate
the live comp and ship in the artifact). run_hooks=False skips hooks
AND ships them as-is.

Hook markers are written to op.unit_tests storage (key 'rht_hook_log')
because copy-mode destroys the comp the pre hook runs in -- markers
must survive the candidate. Each marker records parent().path at fire
time, which is how tests assert WHERE a hook ran (staged copy vs live
original). All targets use the 'rht_' name prefix so tearDown can sweep
stray staged copies out of /sys/quiet.

All exports use an explicit save_path inside a temp dir (the default
save_path writes into the real release/ directory and must never be
used from a test).
"""

import os
import shutil
import tempfile
from pathlib import Path

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase


PRE_BODY = (
    "import os\n"
    "log = op.unit_tests.fetch('rht_hook_log', [], search=False)\n"
    "log.append(['pre', parent().path, args[0], os.path.isfile(args[0])])\n"
    "op.unit_tests.store('rht_hook_log', log)\n"
)
POST_BODY = (
    "import os\n"
    "log = op.unit_tests.fetch('rht_hook_log', [], search=False)\n"
    "log.append(['post', parent().path, args[0], os.path.isfile(args[0]), args[1]])\n"
    "op.unit_tests.store('rht_hook_log', log)\n"
)

STAGING = '/sys/quiet'


class TestExportPortableToxHooks(EmbodyTestCase):
    """Release-hook behavior of ExportPortableTox against sandbox COMPs."""

    def setUp(self):
        super().setUp()
        self._temp_dir = tempfile.mkdtemp(prefix='release_hooks_')
        op.unit_tests.store('rht_hook_log', [])
        self._sweepStaging()  # self-heal: stale candidates from a
        # crashed/interrupted prior run must not fail unrelated tests

    def tearDown(self):
        shutil.rmtree(self._temp_dir, ignore_errors=True)
        op.unit_tests.unstore('rht_hook_log')
        op.unit_tests.unstore('rht_table_ran')
        self._sweepStaging()
        super().tearDown()

    def _sweepStaging(self):
        quiet = op(STAGING)
        if quiet is None:
            return
        for child in list(quiet.children):
            if child.name.startswith('rht_'):
                child.destroy()

    # -- helpers ----------------------------------------------------------

    def _makeTarget(self, name='rht_target'):
        return self.sandbox.create(baseCOMP, name)

    def _addHook(self, comp, hook_name, body):
        hook = comp.create(textDAT, hook_name)
        hook.text = body
        return hook

    def _savePath(self, filename='hooked.tox'):
        return str(Path(self._temp_dir) / filename)

    def _hookLog(self):
        return list(op.unit_tests.fetch('rht_hook_log', [], search=False))

    def _stagedLeftovers(self):
        return [c.name for c in op(STAGING).children
                if c.name.startswith('rht_')]

    # -- backward compatibility -------------------------------------------

    def test_export_without_hooks_unchanged(self):
        """No hook DATs: plain export succeeds, refs intact, no staged
        leftovers."""
        comp = self._makeTarget()
        child = comp.create(textDAT, 'notes')
        child.par.file = 'notes.py'
        sp = self._savePath()

        ok = self.embody_ext.ExportPortableTox(target=comp, save_path=sp)

        self.assertTrue(ok)
        self.assertTrue(os.path.isfile(sp))
        self.assertEqual(child.par.file.eval(), 'notes.py')
        self.assertEqual(self._hookLog(), [])
        self.assertEqual(self._stagedLeftovers(), [])

    # -- copy-mode happy path ----------------------------------------------

    def test_pre_on_copy_post_on_original(self):
        """pre_release fires on the staged copy (before the save),
        post_release on the live original (after, with success=True)."""
        comp = self._makeTarget()
        self._addHook(comp, 'pre_release', PRE_BODY)
        self._addHook(comp, 'post_release', POST_BODY)
        sp = self._savePath()

        ok = self.embody_ext.ExportPortableTox(target=comp, save_path=sp)

        self.assertTrue(ok)
        log = self._hookLog()
        self.assertLen(log, 2)
        pre, post = log
        self.assertEqual(pre[0], 'pre')
        self.assertStartsWith(pre[1], STAGING + '/')
        self.assertEqual(pre[2], sp)
        self.assertFalse(pre[3])  # no tox yet at pre time
        self.assertEqual(post[0], 'post')
        self.assertEqual(post[1], comp.path)  # original, not the copy
        self.assertTrue(post[3])   # tox exists at post time
        self.assertTrue(post[4])   # success flag
        # The ORIGINAL keeps its hook DATs (only the copy's are deleted).
        self.assertIsNotNone(comp.op('pre_release'))
        self.assertIsNotNone(comp.op('post_release'))
        self.assertEqual(self._stagedLeftovers(), [])

    def test_hooks_absent_from_artifact(self):
        """Hook DATs never ship inside the exported .tox."""
        comp = self._makeTarget()
        comp.create(textDAT, 'payload').text = 'real content'
        self._addHook(comp, 'pre_release', PRE_BODY)
        self._addHook(comp, 'post_release', POST_BODY)
        sp = self._savePath()

        ok = self.embody_ext.ExportPortableTox(target=comp, save_path=sp)

        self.assertTrue(ok)
        loaded = self.sandbox.loadTox(sp)
        try:
            self.assertIsNone(loaded.op('pre_release'))
            self.assertIsNone(loaded.op('post_release'))
            self.assertEqual(loaded.op('payload').text, 'real content')
        finally:
            loaded.destroy()

    def test_pre_mutation_ships_but_does_not_persist(self):
        """A pre_release mutation lands in the artifact (it ran on the
        copy) and leaves the live original untouched."""
        comp = self._makeTarget()
        self._addHook(
            comp, 'pre_release',
            "parent().create(textDAT, 'stamp').text = 'stamped'\n")
        sp = self._savePath()

        ok = self.embody_ext.ExportPortableTox(target=comp, save_path=sp)

        self.assertTrue(ok)
        self.assertIsNone(comp.op('stamp'))  # live comp untouched
        loaded = self.sandbox.loadTox(sp)
        try:
            self.assertEqual(loaded.op('stamp').text, 'stamped')
        finally:
            loaded.destroy()

    def test_pre_destroy_absent_from_artifact_present_live(self):
        """The cleanup use case, now safe: pre_release destroys a scratch
        child on the COPY; the artifact lacks it, the live comp keeps it."""
        comp = self._makeTarget()
        comp.create(textDAT, 'scratch').text = 'dev scratchpad'
        self._addHook(comp, 'pre_release',
                      "parent().op('scratch').destroy()\n")
        sp = self._savePath()

        ok = self.embody_ext.ExportPortableTox(target=comp, save_path=sp)

        self.assertTrue(ok)
        self.assertIsNotNone(comp.op('scratch'))  # live comp keeps it
        loaded = self.sandbox.loadTox(sp)
        try:
            self.assertIsNone(loaded.op('scratch'))
        finally:
            loaded.destroy()

    def test_artifact_stripped_original_untouched(self):
        """File/externaltox refs are stripped in the artifact; the live
        original's refs read unchanged after the export."""
        comp = self._makeTarget()
        dat = comp.create(textDAT, 'notes')
        dat.par.file = 'notes.py'
        sub = comp.create(baseCOMP, 'sub')
        sub.par.externaltox = 'sub.tox'
        sub.par.enableexternaltox = True
        self._addHook(comp, 'pre_release', PRE_BODY)
        sp = self._savePath()

        ok = self.embody_ext.ExportPortableTox(target=comp, save_path=sp)

        self.assertTrue(ok)
        self.assertEqual(dat.par.file.eval(), 'notes.py')
        self.assertEqual(sub.par.externaltox.eval(), 'sub.tox')
        self.assertTrue(sub.par.enableexternaltox.eval())
        loaded = self.sandbox.loadTox(sp)
        try:
            self.assertEqual(loaded.op('notes').par.file.eval(), '')
            self.assertEqual(loaded.op('sub').par.externaltox.eval(), '')
            self.assertFalse(loaded.op('sub').par.enableexternaltox.eval())
        finally:
            loaded.destroy()

    def test_pre_created_file_ref_stripped_in_artifact(self):
        """Pins hook-before-strip ordering on the copy: a file ref the
        pre hook creates must still be stripped from the artifact."""
        comp = self._makeTarget()
        self._addHook(
            comp, 'pre_release',
            "d = parent().create(textDAT, 'gen')\n"
            "d.par.file = 'gen.py'\n")
        sp = self._savePath()

        ok = self.embody_ext.ExportPortableTox(target=comp, save_path=sp)

        self.assertTrue(ok)
        self.assertIsNone(comp.op('gen'))  # created on the copy only
        loaded = self.sandbox.loadTox(sp)
        try:
            self.assertEqual(loaded.op('gen').par.file.eval(), '')
        finally:
            loaded.destroy()

    def test_post_release_only_fires(self):
        """Post-only configuration: still copy-staged (hooks must not
        ship), post fires on the original."""
        comp = self._makeTarget()
        self._addHook(comp, 'post_release', POST_BODY)
        sp = self._savePath()

        ok = self.embody_ext.ExportPortableTox(target=comp, save_path=sp)

        self.assertTrue(ok)
        log = self._hookLog()
        self.assertLen(log, 1)
        self.assertEqual(log[0][0], 'post')
        self.assertEqual(log[0][1], comp.path)
        self.assertTrue(log[0][4])
        loaded = self.sandbox.loadTox(sp)
        try:
            self.assertIsNone(loaded.op('post_release'))
        finally:
            loaded.destroy()

    def test_shortcut_preserved(self):
        """A target with a global OP shortcut keeps it through staging,
        and the artifact retains the par value."""
        comp = self._makeTarget('rht_shortcut_owner')
        comp.par.opshortcut = 'RhtProbeSC'
        self._addHook(comp, 'pre_release', PRE_BODY)
        sp = self._savePath()

        ok = self.embody_ext.ExportPortableTox(target=comp, save_path=sp)

        self.assertTrue(ok)
        self.assertEqual(op.RhtProbeSC.path, comp.path)
        loaded = self.sandbox.loadTox(sp)
        try:
            self.assertEqual(loaded.par.opshortcut.eval(), 'RhtProbeSC')
        finally:
            loaded.destroy()
        self.assertEqual(op.RhtProbeSC.path, comp.path)

    # -- failure contracts ---------------------------------------------------

    def test_pre_release_failure_keeps_candidate(self):
        """A raising pre_release aborts the export, keeps the staged copy
        (renamed *_release_failed) for inspection, skips post_release,
        and leaves the original untouched. The kept candidate must be
        tag-neutralized -- pinned with REAL Embody tags on the original
        so a regression cannot pass vacuously."""
        comp = self._makeTarget()
        comp.par.opshortcut = 'RhtKeptSC'
        child = comp.create(textDAT, 'notes')
        child.par.file = 'notes.py'
        synced_file = Path(self._temp_dir) / 'kept_synced.py'
        synced_file.write_text('kept original', encoding='utf-8')
        synced = comp.create(textDAT, 'synced')
        synced.text = 'kept original'
        synced.par.file = str(synced_file)
        synced.par.syncfile = True
        tag = self.embody_ext.getTags()[0]
        comp.tags.add(tag)
        child.tags.add(tag)
        try:
            self._addHook(comp, 'pre_release',
                          PRE_BODY + "raise RuntimeError('not ready')\n")
            self._addHook(comp, 'post_release', POST_BODY)
            sp = self._savePath()

            ok = self.embody_ext.ExportPortableTox(
                target=comp, save_path=sp)

            self.assertFalse(ok)
            self.assertFalse(os.path.isfile(sp))
            self.assertEqual(child.par.file.eval(), 'notes.py')
            log = self._hookLog()
            self.assertLen(log, 1)  # pre marker only; post never fired
            self.assertEqual(log[0][0], 'pre')
            kept = self._stagedLeftovers()
            self.assertLen(kept, 1)
            self.assertIn('_release_failed', kept[0])
            # Kept candidate: fully inert -- tag-neutralized,
            # sync-disabled, and OP shortcut blanked.
            kept_op = op(STAGING).op(kept[0])
            embody_tags = set(self.embody_ext.getTags())
            for o in [kept_op] + kept_op.findChildren():
                self.assertEqual(set(o.tags) & embody_tags, set())
            self.assertFalse(kept_op.op('synced').par.syncfile.eval())
            self.assertEqual(kept_op.par.opshortcut.eval(), '')
            # Original keeps its tags, sync, and shortcut -- untouched.
            self.assertIn(tag, comp.tags)
            self.assertIn(tag, child.tags)
            self.assertTrue(synced.par.syncfile.eval())
            self.assertEqual(op.RhtKeptSC.path, comp.path)
        finally:
            comp.tags.discard(tag)
            child.tags.discard(tag)

    def test_post_release_runs_on_save_failure(self):
        """post_release still fires (on the original, success=False) when
        the save itself fails; no staged copy is left behind."""
        comp = self._makeTarget()
        self._addHook(comp, 'pre_release', PRE_BODY)
        self._addHook(comp, 'post_release', POST_BODY)
        sp = self._savePath('blocked.tox')
        os.makedirs(sp)  # a directory at save_path makes the write fail

        ok = self.embody_ext.ExportPortableTox(target=comp, save_path=sp)

        self.assertFalse(ok)
        log = self._hookLog()
        self.assertLen(log, 2)
        self.assertEqual(log[1][0], 'post')
        self.assertEqual(log[1][1], comp.path)
        self.assertFalse(log[1][4])  # success=False
        self.assertEqual(self._stagedLeftovers(), [])

    def test_post_release_failure_returns_false_tox_exists(self):
        comp = self._makeTarget()
        self._addHook(comp, 'pre_release', PRE_BODY)
        self._addHook(comp, 'post_release',
                      POST_BODY + "raise RuntimeError('upload failed')\n")
        sp = self._savePath()

        ok = self.embody_ext.ExportPortableTox(target=comp, save_path=sp)

        self.assertFalse(ok)
        self.assertTrue(os.path.isfile(sp))
        log = self._hookLog()
        self.assertLen(log, 2)
        self.assertTrue(log[1][4])  # success was True at post fire time

    def test_pre_destroys_candidate_aborts_gracefully(self):
        """A pre_release that destroys its own staged copy must not raise
        out of the export: False, no tox, original intact -- and since
        pre COMPLETED, post_release still runs (success=False)."""
        comp = self._makeTarget('rht_doomed')
        comp.create(textDAT, 'payload').text = 'still here'
        self._addHook(comp, 'pre_release', "parent().destroy()\n")
        self._addHook(comp, 'post_release', POST_BODY)
        sp = self._savePath('doomed.tox')

        ok = self.embody_ext.ExportPortableTox(target=comp, save_path=sp)

        self.assertFalse(ok)
        self.assertFalse(os.path.isfile(sp))
        self.assertTrue(comp.valid)
        self.assertEqual(comp.op('payload').text, 'still here')
        log = self._hookLog()
        self.assertLen(log, 1)
        self.assertEqual(log[0][0], 'post')
        self.assertEqual(log[0][1], comp.path)  # on the original
        self.assertFalse(log[0][4])             # success=False

    def test_hooks_recover_after_failed_hook(self):
        """Latch hygiene: a raising hook must not leave the re-entrancy
        latch stuck; the next export's hooks fire normally."""
        broken = self._makeTarget('rht_broken')
        self._addHook(broken, 'pre_release', "raise RuntimeError('boom')\n")
        self.assertFalse(self.embody_ext.ExportPortableTox(
            target=broken, save_path=self._savePath('broken.tox')))
        op.unit_tests.store('rht_hook_log', [])

        healthy = self._makeTarget('rht_healthy')
        self._addHook(healthy, 'pre_release', PRE_BODY)
        self._addHook(healthy, 'post_release', POST_BODY)
        sp = self._savePath('healthy.tox')

        ok = self.embody_ext.ExportPortableTox(target=healthy, save_path=sp)

        self.assertTrue(ok)
        self.assertLen(self._hookLog(), 2)

    # -- mode selection and discovery rules -----------------------------------

    def test_hook_mode_live_mutates_original(self):
        """hook_mode='live' restores in-place semantics: pre runs on the
        live comp, mutations persist, hook DATs ship in the artifact."""
        comp = self._makeTarget()
        self._addHook(comp, 'pre_release',
                      PRE_BODY +
                      "parent().create(textDAT, 'stamp').text = 'live'\n")
        sp = self._savePath()

        ok = self.embody_ext.ExportPortableTox(
            target=comp, save_path=sp, hook_mode='live')

        self.assertTrue(ok)
        log = self._hookLog()
        self.assertLen(log, 1)
        self.assertEqual(log[0][1], comp.path)  # ran on the ORIGINAL
        self.assertIsNotNone(comp.op('stamp'))  # mutation persisted
        loaded = self.sandbox.loadTox(sp)
        try:
            self.assertIsNotNone(loaded.op('pre_release'))  # hooks ship
        finally:
            loaded.destroy()
        self.assertEqual(self._stagedLeftovers(), [])

    def test_run_hooks_false_suppresses_and_ships(self):
        """run_hooks=False is the machinery flag: no hooks run and the
        hook DATs ship as-is in the artifact."""
        comp = self._makeTarget()
        self._addHook(comp, 'pre_release', PRE_BODY)
        self._addHook(comp, 'post_release', POST_BODY)
        sp = self._savePath()

        ok = self.embody_ext.ExportPortableTox(
            target=comp, save_path=sp, run_hooks=False)

        self.assertTrue(ok)
        self.assertEqual(self._hookLog(), [])
        self.assertEqual(self._stagedLeftovers(), [])
        loaded = self.sandbox.loadTox(sp)
        try:
            self.assertIsNotNone(loaded.op('pre_release'))
        finally:
            loaded.destroy()

    def test_non_dat_hook_ignored(self):
        """A COMP named pre_release is not a hook: never executed, and it
        ships in the artifact (it is user content, not a hook)."""
        comp = self._makeTarget()
        comp.create(baseCOMP, 'pre_release')
        sp = self._savePath()

        ok = self.embody_ext.ExportPortableTox(target=comp, save_path=sp)

        self.assertTrue(ok)
        self.assertTrue(os.path.isfile(sp))
        self.assertEqual(self._hookLog(), [])
        self.assertEqual(self._stagedLeftovers(), [])
        loaded = self.sandbox.loadTox(sp)
        try:
            imposter = loaded.op('pre_release')
            self.assertIsNotNone(imposter)
            self.assertTrue(imposter.isCOMP)
        finally:
            loaded.destroy()

    def test_unknown_hook_mode_aborts(self):
        """A bogus hook_mode fails loud: False, no tox, no hooks run."""
        comp = self._makeTarget()
        self._addHook(comp, 'pre_release', PRE_BODY)
        sp = self._savePath()

        ok = self.embody_ext.ExportPortableTox(
            target=comp, save_path=sp, hook_mode='oops')

        self.assertFalse(ok)
        self.assertFalse(os.path.isfile(sp))
        self.assertEqual(self._hookLog(), [])
        self.assertEqual(self._stagedLeftovers(), [])

    def test_hook_created_during_pre_still_stripped(self):
        """A hook-named text DAT CREATED by the pre hook (after ref
        capture) must still be stripped from the artifact."""
        comp = self._makeTarget()
        self._addHook(
            comp, 'pre_release',
            "parent().create(textDAT, 'post_release').text = 'smuggled'\n")
        sp = self._savePath()

        ok = self.embody_ext.ExportPortableTox(target=comp, save_path=sp)

        self.assertTrue(ok)
        loaded = self.sandbox.loadTox(sp)
        try:
            self.assertIsNone(loaded.op('pre_release'))
            self.assertIsNone(loaded.op('post_release'))
        finally:
            loaded.destroy()

    def test_table_dat_hook_never_executes(self):
        """A Table DAT named pre_release IS a DAT but not a Text DAT --
        its contents are never executed as Python."""
        comp = self._makeTarget()
        table = comp.create(tableDAT, 'pre_release')
        table.clear()
        table.appendRow(["op.unit_tests.store('rht_table_ran', True)"])
        sp = self._savePath()

        ok = self.embody_ext.ExportPortableTox(target=comp, save_path=sp)

        self.assertTrue(ok)
        self.assertFalse(
            op.unit_tests.fetch('rht_table_ran', False, search=False))
        self.assertEqual(self._stagedLeftovers(), [])

    def test_nested_hook_dat_ignored(self):
        """Only DIRECT children count: a pre_release buried one level
        deeper never fires."""
        comp = self._makeTarget()
        inner = comp.create(baseCOMP, 'inner')
        hook = inner.create(textDAT, 'pre_release')
        hook.text = PRE_BODY
        sp = self._savePath()

        ok = self.embody_ext.ExportPortableTox(target=comp, save_path=sp)

        self.assertTrue(ok)
        self.assertEqual(self._hookLog(), [])
        self.assertEqual(self._stagedLeftovers(), [])

    def test_nested_export_from_hook_suppresses_inner_hooks(self):
        """A pre_release may export a sub-component of the staged copy;
        the nested export runs plain (no hooks, no second copy)."""
        comp = self._makeTarget()
        inner = comp.create(baseCOMP, 'rht_inner_target')
        inner_hook = inner.create(textDAT, 'pre_release')
        inner_hook.text = PRE_BODY
        self._addHook(
            comp, 'pre_release',
            "op.Embody.ExportPortableTox(\n"
            "    target=parent().op('rht_inner_target'),\n"
            "    save_path=args[0] + '_inner.tox')\n")
        sp = self._savePath()

        ok = self.embody_ext.ExportPortableTox(target=comp, save_path=sp)

        self.assertTrue(ok)
        self.assertTrue(os.path.isfile(sp))
        self.assertTrue(os.path.isfile(sp + '_inner.tox'))
        # Inner comp's own pre_release must NOT have fired.
        self.assertEqual(self._hookLog(), [])
        self.assertEqual(self._stagedLeftovers(), [])

    def test_renamed_hook_still_stripped(self):
        """A pre_release that renames itself cannot smuggle hook code
        into the artifact -- captured refs are destroyed, not names."""
        comp = self._makeTarget()
        self._addHook(comp, 'pre_release', "me.name = 'pre_done'\n")
        sp = self._savePath()

        ok = self.embody_ext.ExportPortableTox(target=comp, save_path=sp)

        self.assertTrue(ok)
        loaded = self.sandbox.loadTox(sp)
        try:
            self.assertIsNone(loaded.op('pre_release'))
            self.assertIsNone(loaded.op('pre_done'))
        finally:
            loaded.destroy()

    def test_synced_dat_no_writethrough(self):
        """File-sync is disabled on the staged copy: a pre hook editing a
        synced DAT must not write through to the source file or mutate
        the live original."""
        src_file = Path(self._temp_dir) / 'synced_src.py'
        src_file.write_text('original content', encoding='utf-8')
        comp = self._makeTarget()
        dat = comp.create(textDAT, 'synced')
        dat.text = 'original content'
        dat.par.file = str(src_file)
        dat.par.syncfile = True
        self._addHook(
            comp, 'pre_release',
            "parent().op('synced').text = 'MUTATED BY HOOK'\n")
        sp = self._savePath()

        ok = self.embody_ext.ExportPortableTox(target=comp, save_path=sp)

        self.assertTrue(ok)
        self.assertEqual(src_file.read_text(encoding='utf-8'),
                         'original content')
        self.assertEqual(dat.text, 'original content')
        self.assertTrue(dat.par.syncfile.eval())  # original still synced

    def test_repeat_failure_same_comp(self):
        """Two failures of the same comp keep two distinct candidates;
        a subsequent fixed export succeeds without deleting them."""
        comp = self._makeTarget('rht_repeat')
        hook = self._addHook(comp, 'pre_release',
                             "raise RuntimeError('v1')\n")
        self.assertFalse(self.embody_ext.ExportPortableTox(
            target=comp, save_path=self._savePath('r1.tox')))
        self.assertFalse(self.embody_ext.ExportPortableTox(
            target=comp, save_path=self._savePath('r2.tox')))
        kept = self._stagedLeftovers()
        self.assertLen(kept, 2)
        self.assertLen(set(kept), 2)  # distinct auto-suffixed names

        hook.text = PRE_BODY  # fix the hook
        sp = self._savePath('r3.tox')
        self.assertTrue(self.embody_ext.ExportPortableTox(
            target=comp, save_path=sp))
        self.assertTrue(os.path.isfile(sp))
        # Kept inspection artifacts were NOT silently deleted.
        self.assertLen(self._stagedLeftovers(), 2)

    def test_live_mode_pre_abort(self):
        """hook_mode='live': a raising pre_release aborts before anything
        is stripped or saved; post_release does not run."""
        comp = self._makeTarget()
        child = comp.create(textDAT, 'notes')
        child.par.file = 'notes.py'
        self._addHook(comp, 'pre_release',
                      PRE_BODY + "raise RuntimeError('not ready')\n")
        self._addHook(comp, 'post_release', POST_BODY)
        sp = self._savePath()

        ok = self.embody_ext.ExportPortableTox(
            target=comp, save_path=sp, hook_mode='live')

        self.assertFalse(ok)
        self.assertFalse(os.path.isfile(sp))
        self.assertEqual(child.par.file.eval(), 'notes.py')
        log = self._hookLog()
        self.assertLen(log, 1)
        self.assertEqual(log[0][0], 'pre')
        self.assertEqual(log[0][1], comp.path)  # ran on the original
        self.assertEqual(self._stagedLeftovers(), [])

    def test_live_mode_post_failure_returns_false_tox_exists(self):
        """hook_mode='live': post_release fires on the original with the
        success flag; a post raise turns the result False with the tox
        already on disk."""
        comp = self._makeTarget()
        self._addHook(comp, 'pre_release', PRE_BODY)
        self._addHook(comp, 'post_release',
                      POST_BODY + "raise RuntimeError('upload failed')\n")
        sp = self._savePath()

        ok = self.embody_ext.ExportPortableTox(
            target=comp, save_path=sp, hook_mode='live')

        self.assertFalse(ok)
        self.assertTrue(os.path.isfile(sp))
        log = self._hookLog()
        self.assertLen(log, 2)
        self.assertEqual(log[0][1], comp.path)
        self.assertEqual(log[1][1], comp.path)
        self.assertTrue(log[1][4])  # success was True at post fire time
        self.assertEqual(self._stagedLeftovers(), [])

    def test_file_in_dat_without_syncfile(self):
        """A DAT with a 'file' par but no 'syncfile' (File In DAT) must
        not break the export core on the staged copy."""
        comp = self._makeTarget()
        src = comp.create(fileinDAT, 'src')
        src.par.file = 'data.txt'
        self._addHook(comp, 'pre_release', PRE_BODY)
        sp = self._savePath()

        ok = self.embody_ext.ExportPortableTox(target=comp, save_path=sp)

        self.assertTrue(ok)
        self.assertEqual(src.par.file.eval(), 'data.txt')
        loaded = self.sandbox.loadTox(sp)
        try:
            self.assertEqual(loaded.op('src').par.file.eval(), '')
        finally:
            loaded.destroy()


class TestReleaseAll(EmbodyTestCase):
    """ReleaseAll: a component releases when it is BOTH Embody-tracked
    (externalization tag) AND hook-bearing. Third-party components ship
    with their authors' hook DATs baked in (PI-style tools; seen in the
    wild with AlphaMoonbase's tweener), so hooks alone must never
    qualify a component for a batch release."""

    def setUp(self):
        super().setUp()
        self._temp_dir = tempfile.mkdtemp(prefix='release_all_')
        op.unit_tests.store('rht_hook_log', [])
        self._tag = self.embody_ext.getTags()[0]
        self._tagged = []

    def tearDown(self):
        for c in self._tagged:
            if c.valid:
                c.tags.discard(self._tag)
        shutil.rmtree(self._temp_dir, ignore_errors=True)
        op.unit_tests.unstore('rht_hook_log')
        quiet = op(STAGING)
        if quiet:
            for child in list(quiet.children):
                if child.name.startswith('rht_'):
                    child.destroy()
        super().tearDown()

    def _comp(self, name, pre=None, post=None, tracked=True):
        comp = self.sandbox.create(baseCOMP, name)
        if pre is not None:
            comp.create(textDAT, 'pre_release').text = pre
        if post is not None:
            comp.create(textDAT, 'post_release').text = post
        if tracked:
            comp.tags.add(self._tag)
            self._tagged.append(comp)
        return comp

    def test_tracked_and_hooked_release(self):
        """Tracked+hooked comps release; hookless or untracked ones
        don't -- the tweener lesson."""
        a = self._comp('rht_alpha', pre=PRE_BODY, post=POST_BODY)
        b = self._comp('rht_beta', post=POST_BODY)          # post-only
        self._comp('rht_plain')                             # tracked, no hooks
        self._comp('rht_thirdparty', pre=PRE_BODY, tracked=False)

        res = self.embody_ext.ReleaseAll(root=self.sandbox,
                                         out_dir=self._temp_dir)

        self.assertEqual(res['targets'], [a.path, b.path])
        self.assertEqual(res['failed'], [])
        self.assertLen(res['released'], 2)
        for name in ('rht_alpha', 'rht_beta'):
            self.assertTrue(
                (Path(self._temp_dir) / f'{name}.tox').is_file())
        for name in ('rht_plain', 'rht_thirdparty'):
            self.assertFalse(
                (Path(self._temp_dir) / f'{name}.tox').exists(),
                f'{name} must not be released')

    def test_failure_does_not_halt_batch(self):
        """A pre-abort in one component never blocks the others."""
        bad = self._comp('rht_bad',
                         pre="raise RuntimeError('not ready')\n")
        self._comp('rht_good', pre=PRE_BODY)

        res = self.embody_ext.ReleaseAll(root=self.sandbox,
                                         out_dir=self._temp_dir)

        self.assertEqual(res['failed'], [bad.path])
        self.assertLen(res['released'], 1)
        self.assertTrue((Path(self._temp_dir) / 'rht_good.tox').is_file())
        self.assertFalse((Path(self._temp_dir) / 'rht_bad.tox').exists())

    def test_imposter_hooks_do_not_qualify(self):
        """A Table DAT named pre_release is not an opt-in, even on a
        tracked comp."""
        imp = self._comp('rht_imposter')
        table = imp.create(tableDAT, 'pre_release')
        table.clear()
        table.appendRow(['not a hook'])
        real = self._comp('rht_real', pre=PRE_BODY)

        res = self.embody_ext.ReleaseAll(root=self.sandbox,
                                         out_dir=self._temp_dir)

        self.assertEqual(res['targets'], [real.path])

    def test_duplicate_names_disambiguated(self):
        """Same-named comps under different parents both release, with
        distinct filenames."""
        p1 = self.sandbox.create(baseCOMP, 'rht_group1')
        p2 = self.sandbox.create(baseCOMP, 'rht_group2')
        for parent_comp in (p1, p2):
            dup = parent_comp.create(baseCOMP, 'rht_dup')
            dup.create(textDAT, 'pre_release').text = PRE_BODY
            dup.tags.add(self._tag)
            self._tagged.append(dup)

        res = self.embody_ext.ReleaseAll(root=self.sandbox,
                                         out_dir=self._temp_dir)

        self.assertLen(res['released'], 2)
        self.assertLen(set(res['released']), 2)
        self.assertTrue((Path(self._temp_dir) / 'rht_dup.tox').is_file())
        self.assertTrue((Path(self._temp_dir) / 'rht_dup_2.tox').is_file())

    def test_project_scan_is_discovery_only_and_excludes_system(self):
        """The whole-project targeting rules, pinned WITHOUT executing
        anything: _findReleaseTargets never returns /sys (staged copies
        carry hook DATs), /local, Embody's interior, or untracked
        hook-bearers like third-party palette/PI components."""
        mine = self._comp('rht_mine', pre=PRE_BODY)
        self._comp('rht_foreign', pre=PRE_BODY, tracked=False)

        targets = self.embody_ext._findReleaseTargets(None)

        paths = [c.path for c in targets]
        self.assertIn(mine.path, paths)
        for t in paths:
            self.assertFalse(t.startswith('/sys/'),
                             f'staging leaked into scan: {t}')
            self.assertFalse(t.startswith('/local/'),
                             f'/local leaked into scan: {t}')
            self.assertFalse(t.startswith(self.embody.path),
                             f'Embody interior leaked into scan: {t}')
            self.assertFalse(t.endswith('rht_foreign'),
                             'untracked hook-bearer must not qualify')

    def test_release_all_end_to_end_reimport(self):
        """Full batch E2E: two hooked comps -> ReleaseAll -> reimport both
        artifacts into TD and verify what shipped. Pins the whole chain:
        pre hooks shaped each artifact (stamp added, scratch removed),
        hook DATs and Embody tags stripped, payloads intact, live
        originals untouched, and post hooks ran on the originals writing
        per-component receipts with the right save path + success flag."""
        comps = {}
        for name in ('rht_e2e_a', 'rht_e2e_b'):
            comp = self._comp(name)  # tracked; hooks added below
            comp.create(textDAT, 'payload').text = f'payload of {name}'
            comp.create(textDAT, 'scratch').text = 'dev scratch'
            comp.create(textDAT, 'pre_release').text = (
                "parent().create(textDAT, 'stamp').text = "
                "'released ' + parent().name\n"
                "parent().op('scratch').destroy()\n")
            comp.create(textDAT, 'post_release').text = (
                "with open(args[0] + '.receipt', 'w') as f:\n"
                "    f.write(parent().name + ' ok=' + str(args[1]))\n")
            comps[name] = comp

        res = self.embody_ext.ReleaseAll(root=self.sandbox,
                                         out_dir=self._temp_dir)

        self.assertEqual(res['failed'], [])
        self.assertLen(res['released'], 2)
        for name, comp in comps.items():
            tox = Path(self._temp_dir) / f'{name}.tox'
            self.assertTrue(tox.is_file(), f'{name}.tox must exist')
            # post hook ran on the ORIGINAL with the right path + success.
            receipt = Path(str(tox) + '.receipt')
            self.assertTrue(receipt.is_file(),
                            f'{name} post_release must write its receipt')
            self.assertEqual(receipt.read_text(), f'{name} ok=True')
            # Live original untouched by the pre hook.
            self.assertIsNone(comp.op('stamp'))
            self.assertIsNotNone(comp.op('scratch'))
            self.assertIsNotNone(comp.op('pre_release'))
            self.assertIsNotNone(comp.op('post_release'))
            # Reimport the artifact and verify exactly what shipped.
            loaded = self.sandbox.loadTox(str(tox))
            try:
                self.assertEqual(loaded.op('payload').text,
                                 f'payload of {name}')
                self.assertEqual(loaded.op('stamp').text,
                                 f'released {name}')
                self.assertIsNone(loaded.op('scratch'))
                self.assertIsNone(loaded.op('pre_release'))
                self.assertIsNone(loaded.op('post_release'))
                self.assertEqual(
                    set(loaded.tags) & set(self.embody_ext.getTags()),
                    set(), 'artifact must carry no Embody tags')
            finally:
                loaded.destroy()
