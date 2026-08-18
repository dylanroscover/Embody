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


class TestTransientParScrub(EmbodyTestCase):
    """A-50 (Convoy plan): the declarative runtime-status par registry and
    its consumers -- TDN custom-par export records RESTING values (never
    par.default: Status's default '' is a state the enable machinery
    cannot leave), the TDN value-omit companion drops machine-stamp values
    while definitions ship, and ExportPortableTox resets registered pars
    around the save in EVERY mode (the registry is the last word; hooks
    cannot ship a session value for a registered par).

    Behavioral tests run against sandbox comps given a throwaway global OP
    shortcut plus TEMPORARILY patched registry entries. Both class
    attributes are restored in tearDown even on failure; the live Embody
    comp's entries are only ever READ. Temp dirs are cleaned in tearDown
    (addCleanup does not run under this harness) and /sys/quiet is swept
    of rht_-prefixed staging leftovers.
    """

    _SHORTCUT = 'Rhtscrub'
    _RESTING = 'Testresting'

    def setUp(self):
        super().setUp()
        self._cls = type(self.embody_ext)
        self._orig_registry = self._cls._TRANSIENT_STATUS_PARS
        self._orig_omit = self._cls._TDN_VALUE_OMIT_PARS
        self._tmp_dirs = []

    def tearDown(self):
        self._cls._TRANSIENT_STATUS_PARS = self._orig_registry
        self._cls._TDN_VALUE_OMIT_PARS = self._orig_omit
        for d in self._tmp_dirs:
            shutil.rmtree(d, ignore_errors=True)
        quiet = op(STAGING)
        if quiet is not None:
            for child in list(quiet.children):
                if child.name.startswith('rht_'):
                    child.destroy()
        super().tearDown()

    def _tmpdir(self, prefix):
        d = tempfile.mkdtemp(prefix=prefix)
        self._tmp_dirs.append(d)
        return d

    def _make_scrub_comp(self, name='rht_scrub_target'):
        """Sandbox comp with a registered transient par ('Teststatus',
        runtime-looking value, non-default resting) and an unregistered
        authored par. The par's default is '' while the registered resting
        is 'Testresting', so a reset-to-default bug cannot pass as a
        reset-to-resting."""
        comp = self.sandbox.create(baseCOMP, name)
        comp.par.opshortcut = self._SHORTCUT
        page = comp.appendCustomPage('Info')
        page.appendStr('Teststatus')[0].val = 'Running on port 999'
        page.appendStr('Authored')[0].val = 'keep-me'
        patched = dict(self._orig_registry)
        patched[self._SHORTCUT] = {'Teststatus': self._RESTING}
        self._cls._TRANSIENT_STATUS_PARS = patched
        return comp

    # -- registry + scoping ------------------------------------------

    def test_registered_embody_names_exist_with_truthy_restings(self):
        """Typo/rename tripwire, plus the blocker tripwire: every
        registered Embody name must exist on the live comp, and every
        resting must be a truthy state string -- never '' (Status's
        default), which the enable state-machine cannot leave.

        None is the ONE other legal resting: the scrub reads it as
        'reset to the par's own default', which is what an IDENTIFIER
        par needs (Convoyid rests empty because a project has no convoy
        until the first explicit enable). It carries the same leak
        protection; what stays banned is a literal '' resting, which
        would claim an empty string is a state.
        """
        # A registered name is either a single status par or a custom
        # SEQUENCE (e.g. Convoynodes, the read-only network-status rows).
        # A sequence exists in comp.seq, not comp.par, and its None resting
        # means "reset every block value to its template default" (the
        # _scrubTransientPars sequence branch), so the empty-default check
        # for single identifier pars does not apply to it.
        seq_names = {s.name for s in self.embody.seq if s is not None}
        for name, resting in sorted(
                self._orig_registry['Embody'].items()):
            is_sequence = name in seq_names
            self.assertTrue(
                is_sequence or getattr(self.embody.par, name, None) is not None,
                'registered transient par %r does not exist on the '
                'Embody COMP -- registry is stale' % name)
            self.assertTrue(
                resting is None or (isinstance(resting, str) and resting),
                'resting for %r must be a truthy state string, or None '
                'for reset-to-default; got %r' % (name, resting))
            if resting is None and not is_sequence:
                par = getattr(self.embody.par, name, None)
                # None means "reset to the par's own default". That is the
                # right registration for an IDENTIFIER or display name (rests
                # empty) AND for a CONSENT toggle (rests at its AUTHORED
                # default -- Off for opt-ins like Convoyenable, On for
                # default-on consent like Clipboardautopaste, whose baked
                # Off shipped in v6.0.251 and killed the collection copy
                # flow on fresh installs). A toggle's default is always a
                # real state, so the empty-default restriction applies only
                # to non-toggle pars, where a '' default can be a non-state
                # the runtime cannot leave (Status's '' bricked the enable
                # path -- the Opus panel blocker).
                if par.style != 'Toggle':
                    self.assertIn(
                        par.default, ('', False, 0),
                        'reset-to-default resting only makes sense for a '
                        'par whose default IS its resting state (empty or '
                        'off); %r has default %r' % (name, par.default))
        for name in sorted(self._orig_omit['Embody']):
            self.assertIsNotNone(
                getattr(self.embody.par, name, None),
                'value-omit par %r does not exist on the Embody COMP'
                % name)

    def test_embody_about_page_names_are_consciously_registered(self):
        """Sync tripwire: adding a par to the Embody About page must be a
        conscious decision about churn -- either register it in
        _TDN_VALUE_OMIT_PARS (machine-written, per-save churn) or accept
        its value in version control. This assertion forces the look."""
        expected = {'Version', 'Touchbuild', 'Author', 'Build', 'Date',
                    'Github', 'Help', 'Autoupdate', 'Checkforupdate',
                    'Updatestatus'}
        live = set()
        for page in self.embody.customPages:
            if page.name == 'About':
                live = {p.name for p in page.pars}
        self.assertEqual(
            live, expected,
            'The Embody About page changed. Decide churn-handling for the '
            'new/renamed pars (EmbodyExt._TDN_VALUE_OMIT_PARS) and update '
            'this expected set.')

    def test_scoping_by_op_shortcut(self):
        self.assertEqual(
            self.embody_ext._transientParNames(self.embody),
            self._orig_registry['Embody'])
        self.assertEqual(
            self.embody_ext._tdnValueOmitNames(self.embody),
            self._orig_omit['Embody'])
        plain = self.sandbox.create(baseCOMP, 'rht_plain_scope')
        self.assertEqual(
            self.embody_ext._transientParNames(plain), {},
            'a comp with no registered shortcut must scrub nothing')
        self.assertEqual(
            self.embody_ext._tdnValueOmitNames(plain), frozenset())

    # -- scrub / restore ---------------------------------------------

    def test_scrub_sets_resting_and_restore_reapplies(self):
        comp = self._make_scrub_comp()
        snap = self.embody_ext._scrubTransientPars(comp)
        self.assertEqual(
            comp.par.Teststatus.eval(), self._RESTING,
            'a registered constant-mode par must reset to its RESTING '
            'value, not its default')
        self.assertEqual(
            comp.par.Authored.eval(), 'keep-me',
            'an unregistered par must never be touched')
        self.embody_ext._restoreTransientPars(snap)
        self.assertEqual(
            comp.par.Teststatus.eval(), 'Running on port 999',
            'restore must reapply the snapshotted session value')

    def test_scrub_walks_descendant_comps(self):
        comp = self._make_scrub_comp('rht_scrub_parent')
        child = comp.create(baseCOMP, 'rht_scrub_child')
        child.par.opshortcut = self._SHORTCUT
        page = child.appendCustomPage('Info')
        page.appendStr('Teststatus')[0].val = 'child-session-value'

        snap = self.embody_ext._scrubTransientPars(comp)

        self.assertEqual(
            child.par.Teststatus.eval(), self._RESTING,
            'the scrub must walk descendant COMPs with registered '
            'shortcuts, not just the root')
        self.embody_ext._restoreTransientPars(snap)
        self.assertEqual(child.par.Teststatus.eval(), 'child-session-value')

    def test_scrub_leaves_expression_mode_alone(self):
        comp = self._make_scrub_comp()
        comp.par.Teststatus.expr = "'live-' + 'value'"
        snap = self.embody_ext._scrubTransientPars(comp)
        self.assertEqual(
            comp.par.Teststatus.mode.name, 'EXPRESSION',
            'an expression-mode par carries no baked value -- scrubbing '
            'it would destroy the reference')
        self.assertFalse(
            any(p.name == 'Teststatus' for p, _val in snap),
            'expression-mode pars must not enter the snapshot')

    def test_scrub_on_live_embody_roundtrips_exactly(self):
        """Snapshot/reset/restore on the REAL Embody comp: values after
        must equal values before, and while scrubbed each registered par
        reads its RESTING value. Status is 'Enabled' in a live dev
        session while its resting is 'Disabled', so the snapshot is
        guaranteed non-empty -- this test cannot pass vacuously."""
        registry = self._orig_registry['Embody']
        # Sequence-registered names (Convoynodes) have no comp.par entry --
        # they roundtrip through the scrub snapshot per block, covered by
        # test_registered_sequence_count_kept_values_dropped. This live
        # roundtrip pins the single status readout pars.
        seq_names = {s.name for s in self.embody.seq if s is not None}
        names = [n for n in sorted(registry) if n not in seq_names]
        before = {n: getattr(self.embody.par, n).eval() for n in names}
        snap = self.embody_ext._scrubTransientPars(self.embody)
        try:
            self.assertTrue(
                snap, 'the live comp must yield a non-empty snapshot '
                '(Status runs Enabled while resting is Disabled)')
            for n in names:
                p = getattr(self.embody.par, n)
                if any(sp is p for sp, _v in snap):
                    self.assertEqual(
                        p.eval(), registry[n],
                        '%s must read its resting value while scrubbed' % n)
        finally:
            self.embody_ext._restoreTransientPars(snap)
        after = {n: getattr(self.embody.par, n).eval() for n in names}
        self.assertEqual(before, after,
                         'live status readouts must survive the roundtrip')

    # -- TDN export consumer -----------------------------------------

    def test_tdn_export_records_resting_definition_ships(self):
        comp = self._make_scrub_comp()
        pages = self.embody.ext.TDN._exportCustomPars(comp)
        defs = {d['name']: d for d in pages.get('Info', [])}
        self.assertIn('Teststatus', defs,
                      'the definition (style/label/help) must still ship')
        self.assertEqual(
            defs['Teststatus'].get('value'), self._RESTING,
            'the .tdn must record the RESTING value, not the session '
            'value and not an omitted key')
        self.assertEqual(defs['Authored'].get('value'), 'keep-me')

    def test_tdn_export_preserves_expression_on_registered_par(self):
        """The '='/'~' shorthand encodes the MODE into the value key --
        replacing it would destroy the reference (the scrub half refuses
        the same; the two consumers must agree)."""
        comp = self._make_scrub_comp()
        comp.par.Teststatus.expr = "'live-' + 'value'"
        pages = self.embody.ext.TDN._exportCustomPars(comp)
        defs = {d['name']: d for d in pages.get('Info', [])}
        value = defs['Teststatus'].get('value')
        self.assertTrue(
            isinstance(value, str) and value.startswith('='),
            'an expression on a registered par must survive TDN export, '
            'got %r' % (value,))
        self.assertIn('live-', value)

    def test_tdn_export_omit_names_drop_value_definition_ships(self):
        comp = self._make_scrub_comp('rht_omit_target')
        page = comp.customPages[0]
        page.appendStr('Stampval')[0].val = '2026-07-30 09:45:00 UTC'
        patched = dict(self._orig_omit)
        patched[self._SHORTCUT] = frozenset({'Stampval'})
        self._cls._TDN_VALUE_OMIT_PARS = patched

        pages = self.embody.ext.TDN._exportCustomPars(comp)
        defs = {d['name']: d for d in pages.get('Info', [])}
        self.assertIn('Stampval', defs,
                      'the omit-name definition must still ship')
        self.assertNotIn(
            'value', defs['Stampval'],
            'an omit-name value must never reach the .tdn')

    def test_tdn_export_keeps_same_named_par_on_user_comp(self):
        comp = self.sandbox.create(baseCOMP, 'rht_user_status')
        page = comp.appendCustomPage('Info')
        page.appendStr('Status')[0].val = 'user-authored'
        pages = self.embody.ext.TDN._exportCustomPars(comp)
        defs = {d['name']: d for d in pages.get('Info', [])}
        self.assertEqual(
            defs['Status'].get('value'), 'user-authored',
            'a user par named Status must keep its value -- the registry '
            'is scoped by OP shortcut, never bare names')

    def test_tdn_export_user_about_page_survives(self):
        comp = self.sandbox.create(baseCOMP, 'rht_user_about')
        page = comp.appendCustomPage('About')
        page.appendStr('Version')[0].val = '1.0'
        pages = self.embody.ext.TDN._exportCustomPars(comp)
        self.assertIn(
            'About', pages,
            'a user comp About page must survive -- only a page that is '
            'exactly the Embody metadata stamp is dropped')

    def test_live_embody_export_ships_restings_and_full_about(self):
        """The live Embody export must carry no session status strings,
        record restings for registered pars, KEEP the About page
        definitions (they are the diffable record), and omit only the
        churning stamp values."""
        pages = self.embody.ext.TDN._exportCustomPars(self.embody)
        self.assertIn(
            'About', pages,
            "the Embody About page's definitions belong in the .tdn")
        registry = self._orig_registry['Embody']
        omit = self._orig_omit['Embody']
        for page_name, page_defs in pages.items():
            for d in page_defs:
                name = d.get('name')
                if name in registry:
                    self.assertEqual(
                        d.get('value'), registry[name],
                        '%s on page %s must record its resting value'
                        % (name, page_name))
                if name in omit:
                    self.assertNotIn(
                        'value', d,
                        '%s on page %s leaked a machine-written value'
                        % (name, page_name))

    def test_registered_sequence_count_kept_values_dropped(self):
        comp = self._make_scrub_comp('rht_seq_target')
        page = comp.customPages[0]
        page.appendSequence('Rows')
        page.appendStr('Rowlabel')
        comp.seq.Rows.blockSize = 1
        comp.seq.Rows.numBlocks = 3
        for block in comp.seq.Rows.blocks:
            for group in block:
                for p in group:
                    p.val = 'runtime-row'

        # Pre-check WITHOUT registration: the unfiltered export must carry
        # the session values -- proves the filtered assertion below cannot
        # pass vacuously (panel finding).
        data = self.embody.ext.TDN._exportBuiltinSequences(comp)
        self.assertTrue(
            any(b for b in data.get('Rows', [])),
            'precondition: unregistered export must include block values')

        patched = dict(self._orig_registry)
        patched[self._SHORTCUT] = {'Teststatus': self._RESTING,
                                   'Rows': None}
        self._cls._TRANSIENT_STATUS_PARS = patched

        data = self.embody.ext.TDN._exportBuiltinSequences(comp)
        self.assertEqual(
            data.get('Rows'), [{}, {}, {}],
            'a registered sequence at a NON-default count must export the '
            'count (never numBlocks=0) with no session values')

        snap = self.embody_ext._scrubTransientPars(comp)
        self.assertEqual(comp.seq.Rows.numBlocks, 3,
                         'the scrub must never change the block count')
        for block in comp.seq.Rows.blocks:
            for group in block:
                for p in group:
                    self.assertEqual(p.eval(), p.default,
                                     'scrubbed blocks must sit at defaults')
        self.embody_ext._restoreTransientPars(snap)

    def test_registered_sequence_at_default_count_is_omitted(self):
        comp = self._make_scrub_comp('rht_seq_default')
        page = comp.customPages[0]
        page.appendSequence('Cols')
        page.appendStr('Collabel')
        comp.seq.Cols.blockSize = 1          # default numBlocks stays 1
        for block in comp.seq.Cols.blocks:
            for group in block:
                for p in group:
                    p.val = 'runtime-col'

        patched = dict(self._orig_registry)
        patched[self._SHORTCUT] = {'Teststatus': self._RESTING,
                                   'Cols': None}
        self._cls._TRANSIENT_STATUS_PARS = patched

        data = self.embody.ext.TDN._exportBuiltinSequences(comp)
        self.assertNotIn(
            'Cols', data,
            "a registered sequence at the DEFAULT count ships nothing -- "
            "the amendment's 'reset blocks to defaults so the sequence is "
            "omitted'")

    # -- ExportPortableTox consumer ----------------------------------

    def test_portable_export_ships_resting_and_restores_live(self):
        comp = self._make_scrub_comp('rht_scrub_live')
        sp = os.path.join(self._tmpdir('rht_scrub_'), 'scrubbed.tox')

        ok = self.embody_ext.ExportPortableTox(target=comp, save_path=sp)

        self.assertTrue(ok, 'export must succeed')
        self.assertEqual(
            comp.par.Teststatus.eval(), 'Running on port 999',
            'the LIVE value must be restored after a live-mode export')
        loaded = self.sandbox.loadTox(sp)
        try:
            self.assertEqual(
                loaded.par.Teststatus.eval(), self._RESTING,
                'the artifact must ship the resting value, never session '
                'status')
            self.assertEqual(loaded.par.Authored.eval(), 'keep-me')
        finally:
            loaded.destroy()

    def test_copy_mode_export_ships_resting_live_untouched(self):
        """Copy mode: the export core scrubs the staged candidate (the
        registry is the last word in every mode); the LIVE comp is never
        touched at all."""
        comp = self._make_scrub_comp('rht_scrub_copy')
        comp.create(textDAT, 'pre_release').text = '# no-op hook\n'
        sp = os.path.join(self._tmpdir('rht_scrubc_'), 'scrubbed_copy.tox')

        ok = self.embody_ext.ExportPortableTox(target=comp, save_path=sp)

        self.assertTrue(ok, 'copy-mode export must succeed')
        self.assertEqual(
            comp.par.Teststatus.eval(), 'Running on port 999',
            'copy mode must never touch the live comp')
        loaded = self.sandbox.loadTox(sp)
        try:
            self.assertEqual(
                loaded.par.Teststatus.eval(), self._RESTING,
                'the staged candidate must be scrubbed before the save')
        finally:
            loaded.destroy()

    def test_portable_export_restores_after_save_failure(self):
        """Phase 4's always-restore contract on the FAILURE path: a save
        into an impossible location must still hand the session its
        values back."""
        comp = self._make_scrub_comp('rht_scrub_fail')
        bad_dir = os.path.join(self._tmpdir('rht_scrubf_'), 'blocker')
        with open(bad_dir, 'w', encoding='utf-8') as f:
            f.write('a file where a directory is needed')
        sp = os.path.join(bad_dir, 'nested', 'impossible.tox')

        ok = self.embody_ext.ExportPortableTox(target=comp, save_path=sp)

        self.assertFalse(ok, 'the export must report the save failure')
        self.assertEqual(
            comp.par.Teststatus.eval(), 'Running on port 999',
            'the live value must be restored even when the save fails')
