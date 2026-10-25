"""
Project-wide externalization scope (issue #176).

Externalize Full Project tagged Embody's own COMP and everything inside it,
plus TouchDesigner's system COMPs. A TDXN export of the COMP holding Embody
inlined Embody's whole network and its tracking table. A project last saved
before the tracking table was first written reopened with no record of what
was externalized after that save.

No project-wide sweep runs here. Every check is a predicate, an in-memory
export, a sandbox table, or an operation with its side effects stubbed. The
real untracks touch only sandbox operators, but they do rewrite the live
tracking table (one table.text assignment, dropping only their own rows).
"""

import json
import os
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace


SYSTEM_TOPS = ('local', 'perform', 'sys', 'ui')
CLEANUP_TITLE = 'Embody -- Embody Is Tracking Itself'


class TestProjectExternalizeScope(EmbodyTestCase):

    def setUp(self):
        super().setUp()
        self._stubbed = []
        self._restore_ops = []
        self._temp_files = []
        self._temp_dirs = []
        self._saved_fp = None
        self._probe_key = None
        self._untrack_after = []

    def tearDown(self):
        ext = self.embody_ext
        for name in self._stubbed:
            try:
                delattr(ext, name)
            except AttributeError:
                pass
        if self._untrack_after:
            ext._untrackEntries(self._untrack_after)
        for oper, tags, color in self._restore_ops:
            try:
                oper.tags = list(tags)
                oper.color = color
            except Exception:
                pass
        ext.__dict__.pop('_refused_tag_paths', None)
        if self._saved_fp is not None:
            path, fp = self._saved_fp
            if fp is None:
                ext._tdn_fingerprints.pop(path, None)
            else:
                ext._tdn_fingerprints[path] = fp
        if self._probe_key:
            self.embody.unstore(self._probe_key)
        for f in self._temp_files:
            try:
                os.remove(f)
            except OSError:
                pass
        for d in reversed(self._temp_dirs):
            try:
                os.rmdir(d)
            except OSError:
                pass
        super().tearDown()

    # ----- helpers ---------------------------------------------------------

    def _stub(self, name, fn):
        setattr(self.embody_ext, name, fn)
        self._stubbed.append(name)

    def _tracking_table(self, name, rows=()):
        """A sandbox table shaped like the externalizations table."""
        t = self.sandbox.create(tableDAT, name)
        t.clear()
        t.appendRow(['path', 'type', 'strategy', 'rel_file_path',
                     'timestamp', 'dirty', 'build', 'touch_build'])
        for path, strategy, rel in rows:
            t.appendRow([path, 'base', strategy, rel, '', '', '', ''])
        return t

    def _stray_rows(self, extra=()):
        own = self.embody.path
        return self._tracking_table('stray176', [
            ('/perform', 'tdxn', 'perform.tdxn'),
            (own, 'tdxn', 'x/Embody.tdxn'),
            (own + '/EmbodyExt', 'py', 'x/Embody/EmbodyExt.py'),
            ('/project1/geo1', 'tdxn', 'project1/geo1.tdxn'),
        ] + list(extra))

    def _externalized(self, oper, tag):
        """Tag and externalize a sandbox op for real; untracked in tearDown."""
        ext = self.embody_ext
        self.assertTrue(ext.applyTagToOperator(oper, tag))
        ext.handleAddition(oper)
        strategy = 'tdn' if oper.family == 'COMP' else tag
        rel = (ext._getStrategyFilePath(oper.path, 'tdn')
               if oper.family == 'COMP' else ext.getExternalPath(oper))
        self.assertTrue(rel, f'precondition: {oper.path} is externalized')
        self._untrack_after.append((oper.path, strategy, rel))
        return strategy, rel

    def _rows_for(self, path):
        ext = self.embody_ext
        table = ext.Externalizations
        return [i for i in range(1, table.numRows)
                if ext._cellVal(i, 'path') == path]

    # ----- Externalize Full Project scope ----------------------------------

    def test_sweep_skips_embody_and_its_internals(self):
        skip = self.embody_ext._shouldSkipOp
        self.assertTrue(skip(self.embody, set()),
                        "Embody's own COMP must never be swept")
        for name in ('tagger', 'EmbodyExt'):
            inner = self.embody.op(name)
            self.assertIsNotNone(inner, f'Embody has no {name} to test with')
            self.assertTrue(skip(inner, set()),
                            f"Embody's {name} must never be swept")

    def test_sweep_skips_system_comps(self):
        skip = self.embody_ext._shouldSkipOp
        checked = 0
        for top in ('perform', 'ui', 'sys'):
            o = op('/' + top)
            if o is None:
                continue
            checked += 1
            self.assertTrue(skip(o, set()), f'/{top} belongs to TouchDesigner')
            child = next(iter(o.children), None)
            if child is not None:
                self.assertTrue(skip(child, set()),
                                f'{child.path} belongs to TouchDesigner')
        self.assertGreater(checked, 0, 'no system COMP to test with')

    def test_sweep_still_takes_user_operators(self):
        skip = self.embody_ext._shouldSkipOp
        comp = self.sandbox.create(baseCOMP, 'user176')
        dat = self.sandbox.create(textDAT, 'user176_dat')
        self.assertFalse(skip(comp, set()))
        self.assertFalse(skip(dat, set()))

    # ----- tagging refusals -------------------------------------------------

    def test_system_comps_refuse_comp_tags(self):
        ui = op('/ui')
        tag = self.embody.par.Tdxntag.eval()
        self._restore_ops.append((ui, set(ui.tags), tuple(ui.color)))
        # should the refusal regress, never export /ui into the live table
        self._stub('_handleTDXNAddition', lambda oper: None)
        self.assertFalse(self.embody_ext.applyTagToOperator(ui, tag),
                         'a system COMP must refuse a COMP tag')
        self.assertNotIn(tag, ui.tags)

    def test_the_tagger_refuses_what_apply_refuses(self):
        """The tagger UI (tagSetter) bypassed every new refusal."""
        ext = self.embody_ext
        ui = op('/ui')
        parent = self.embody.parent()
        for o in (ui, parent):
            self._restore_ops.append((o, set(o.tags), tuple(o.color)))
        # should a refusal regress, never untrack the live container
        self._stub('_removeCompStrategy', lambda oper, tag: None)
        self.assertFalse(ext.tagSetter(ui, self.embody.par.Tdxntag.eval()))
        self.assertFalse(ext.tagSetter(parent, self.embody.par.Toxtag.eval()))
        self.assertNotIn(self.embody.par.Toxtag.eval(), parent.tags)

    def test_the_root_and_embodys_own_operators_refuse_tags(self):
        ext = self.embody_ext
        tag = self.embody.par.Tdxntag.eval()
        self.assertIsNotNone(ext._tagRefusal(op('/'), tag))
        inner = self.embody.op('tagger')
        self._stub('_ownTrackingChoice', lambda: False)
        self.assertIsNotNone(ext._tagRefusal(inner, tag),
                             "a user project never tracks Embody's own ops")
        self._stub('_ownTrackingChoice', lambda: None)
        self.assertIsNotNone(ext._tagRefusal(inner, tag))
        self._stub('_ownTrackingChoice', lambda: True)
        self.assertIsNone(ext._tagRefusal(inner, tag),
                          "Embody's own repo tracks them on purpose")

    def test_the_update_sweep_skips_a_refused_hand_typed_tag(self):
        ext = self.embody_ext
        ui = op('/ui')
        tag = self.embody.par.Tdxntag.eval()
        self._restore_ops.append((ui, set(ui.tags), tuple(ui.color)))
        ui.tags.add(tag)
        self._stub('checkForDuplicates', lambda: None)
        _, additions, _ = ext._updateScan()
        self.assertNotIn('/ui', [o.path for o in additions])

    def test_a_hand_typed_refused_tag_is_never_externalized(self):
        ext = self.embody_ext
        ui = op('/ui')
        tag = self.embody.par.Tdxntag.eval()
        self._restore_ops.append((ui, set(ui.tags), tuple(ui.color)))
        ui.tags.add(tag)
        self.assertTrue(ext._refusesTypedTag(ui, ext.getTags()))
        user = self.sandbox.create(baseCOMP, 'typed176')
        user.tags.add(tag)
        self.assertFalse(ext._refusesTypedTag(user, ext.getTags()))

    def test_a_comp_holding_embody_refuses_a_tox_tag(self):
        """A .tox of Embody's container embeds Embody (1.74 MB, E2E 2026-10-25) and
        reloading it replaces the running Embody. TDXN stays allowed."""
        ext = self.embody_ext
        parent = self.embody.parent()
        tox = self.embody.par.Toxtag.eval()
        self.assertTrue(ext._isToxOverEmbody(parent.path, tox))
        self.assertTrue(ext._isToxOverEmbody(parent.path, 'tox'))
        self.assertFalse(ext._isToxOverEmbody(parent.path,
                                              self.embody.par.Tdxntag.eval()))
        self.assertFalse(ext._isToxOverEmbody(self.sandbox.path, tox))
        self._restore_ops.append((parent, set(parent.tags), tuple(parent.color)))
        self._stub('_removeCompStrategy', lambda oper, tag: None)
        self.assertFalse(ext.applyTagToOperator(parent, tox))
        self.assertNotIn(tox, parent.tags)

    def test_protected_paths(self):
        prot = self.embody_ext._isProtectedPath
        own = self.embody.path
        for path in ('/', own, own + '/tagger', self.embody.parent().path,
                     '/perform', '/perform/anything', '/ui'):
            self.assertTrue(prot(path), f'{path} must be protected')
        self.assertFalse(prot(self.sandbox.path))
        self.assertFalse(prot('/project1/geo1'))

    def test_strategy_lists_skip_protected_rows(self):
        """Both lists come from the live table, which holds no system row in
        Embody's own repo, so feed them one."""
        ext = self.embody_ext
        t = self._tracking_table('lists176', [
            ('/perform', 'tdxn', 'perform.tdxn'),
            ('/perform/win', 'tox', 'perform/win.tox'),
            (self.embody.path, 'tdxn', 'x/Embody.tdxn'),
            (self.sandbox.path + '/kept176', 'tdxn', 'x/kept176.tdxn')])
        # Both read the Externalizations property. Swap the property, never
        # the par: a par change runs Embody's callbacks against the live
        # project, while this call runs no frame and no other code.
        cls = type(ext)
        original = cls.__dict__['Externalizations']
        try:
            cls.Externalizations = property(lambda self: t)
            tdxn = [p for p, _ in ext._getTDXNStrategyComps()]
            tox = [row[0] for row in ext._getTOXStrategyComps()]
        finally:
            cls.Externalizations = original
        self.assertEqual(tdxn, [self.sandbox.path + '/kept176'],
                         'the user row is still listed')
        for p in tdxn + tox:
            self.assertNotIn(p.strip('/').split('/')[0], SYSTEM_TOPS,
                             f'{p} would be stripped or reloaded')
            self.assertFalse(ext._isInsideEmbody(p), f'{p} is Embody')

    # ----- exports of the COMP that holds Embody ---------------------------

    def test_container_export_omits_embody_and_its_table(self):
        res = self.embody.ext.TDXN.ExportNetwork(
            root_path=self.embody.parent().path, output_file=None,
            max_depth=0, interactive=False)
        self.assertTrue(res.get('success'), res.get('error'))
        names = [o.get('name') for o in res['tdn'].get('operators', [])]
        self.assertNotIn(self.embody.name, names,
                         'Embody must not be written into its container')
        table = self.embody_ext.Externalizations
        if table is not None and table.parent() == self.embody.parent():
            self.assertNotIn(table.name, names,
                             "Embody's tracking table must not be written either")

    def test_container_path_walk_omits_embody_and_its_table(self):
        """The async/project exporter walks with _collectAllPaths."""
        paths = self.embody.ext.TDXN._collectAllPaths(self.embody.parent(),
                                                      max_depth=0)
        self.assertNotIn(self.embody.path, paths)
        for owned in self.embody_ext.embodyOwnedPaths():
            self.assertNotIn(owned, paths)

    def test_the_table_is_owned_only_where_it_sits_beside_embody(self):
        ext = self.embody_ext
        table = ext.Externalizations
        self.assertIsNotNone(table, 'precondition: a tracking table')
        self.assertTrue(ext._isProtectedPath(table.parent().path),
                        "precondition: Embody's repo keeps it beside Embody")
        self.assertIn(table.path, ext.embodyOwnedPaths())
        self.assertIn(self.embody.path, ext.embodyOwnedPaths())
        # a table moved into user content exports as content
        moved = self._tracking_table('moved176')
        cls = type(ext)
        original = cls.__dict__['Externalizations']
        try:
            cls.Externalizations = property(lambda self: moved)
            owned = ext.embodyOwnedPaths()
        finally:
            cls.Externalizations = original
        self.assertEqual(owned, frozenset({self.embody.path}))

    def _container_file(self, text):
        rel = 'embody/unit_tests/test_sandbox/container176.tdxn'
        path = self.embody_ext.buildAbsolutePath(rel)
        if not path.parent.is_dir():
            path.parent.mkdir(parents=True)
            self._temp_dirs.append(str(path.parent))
        if str(path) not in self._temp_files:
            self._temp_files.append(str(path))
        path.write_text(text, encoding='utf-8')
        return rel

    def test_container_file_still_naming_embody_is_marked_for_the_next_save(self):
        """An older build wrote Embody into its container's .tdxn, and the
        fingerprint now skips Embody: mark it dirty so the next sweep
        rewrites it, never a synchronous export from the cleanup."""
        ext = self.embody_ext
        parent = self.embody.parent()
        self._saved_fp = (parent.path, ext._tdn_fingerprints.get(parent.path))
        ext._tdn_fingerprints[parent.path] = ('clean',)
        exported = []
        self._stub('saveTDXN', lambda *a, **k: exported.append(a))
        rel = self._container_file('operators:\n- name: geo1\n')
        self._stub('_getStrategyFilePath',
                   lambda p, s: rel if p == parent.path else None)
        ext._markContainerFileStale()
        self.assertEqual(ext._tdn_fingerprints[parent.path], ('clean',),
                         'a clean container file is left alone')
        self._container_file(f'operators:\n- name: other\n  children:\n'
                             f'    - name: {self.embody.name}\n')
        ext._markContainerFileStale()
        self.assertEqual(ext._tdn_fingerprints[parent.path], ('stale',),
                         'nested at any depth, the entry still marks it')
        self.assertEqual(exported, [], 'the cleanup itself never exports')

    def test_the_project_snapshot_is_never_marked(self):
        ext = self.embody_ext
        self._saved_fp = ('/', ext._tdn_fingerprints.get('/'))
        rel = self._container_file(f'operators:\n- name: {self.embody.name}\n')
        self._stub('_getStrategyFilePath',
                   lambda p, s: rel if p == '/' else None)
        ext._markContainerFileStale()
        self.assertNotEqual(ext._tdn_fingerprints.get('/'), ('stale',))

    def test_container_fingerprint_ignores_embody(self):
        ext = self.embody_ext
        parent = self.embody.parent()
        self._saved_fp = (parent.path, ext._tdn_fingerprints.get(parent.path))
        ext._storeTDXNFingerprint(parent)
        self._probe_key = '_test_issue176_probe'
        self.embody.store(self._probe_key, 1)
        self.assertFalse(ext._isTDXNDirty(parent),
                         "Embody's own state must not dirty its container")

    def test_no_save_loop_writes_embodys_own_tox(self):
        """The TOX twin of the r16 churn: dirtyHandler, the deferred chain
        and _updateHead all save through Save()."""
        ext = self.embody_ext

        def looked(*a, **k):
            raise AssertionError('Save() got past the own-operator gate')

        self._stub('_ownTrackingChoice', lambda: False)
        self._stub('_getStrategyFilePath', looked)
        self.assertFalse(ext.Save(self.embody.op('tagger').path))

    def test_untracked_projects_never_walk_or_export_embody(self):
        """After an update, an over-tagged project re-walked and rewrote all
        of Embody inline on every save (7.1 MB, E2E 2026-10-25) until the cleanup
        prompt was answered."""
        ext = self.embody_ext

        def walked(*a, **k):
            raise AssertionError('fingerprinted Embody')

        self._stub('_ownTrackingChoice', lambda: False)
        self._stub('_computeTDXNFingerprint', walked)
        self.assertFalse(ext._isTDXNDirty(self.embody))
        self.assertFalse(ext.saveTDXN(self.embody.path))
        self.assertFalse(ext._skipsOwnExport(self.sandbox.path))
        self._stub('_ownTrackingChoice', lambda: True)
        self.assertFalse(ext._skipsOwnExport(self.embody.path),
                         "Embody's own repo keeps exporting its receipts")
        self._stub('_ownTrackingChoice', lambda: None)
        self.assertFalse(ext._skipsOwnExport(self.embody.path),
                         'an unreadable project.json changes nothing')

    def test_cascade_never_tags_embody(self):
        tagged = []
        self._stub('_hasTDXNTag', lambda oper: False)
        self._stub('applyTagToOperator',
                   lambda oper, tag: tagged.append(oper.path) or True)
        self.embody_ext._cascadeTDXNTag(self.embody.parent())
        self.assertNotIn(self.embody.path, tagged)

    # ----- reopen after a save that predates the tracking file -------------

    def _table_file(self, t, text):
        _, save, _, rel = self.embody_ext.getOpPaths(t)
        folder = os.path.dirname(str(save))
        if not os.path.isdir(folder):
            os.makedirs(folder)
            self._temp_dirs.append(folder)
        self._temp_files.append(str(save))
        with open(save, 'w', encoding='utf-8', newline='') as f:
            f.write(text)
        return str(save), rel

    def _unlinked_table(self, name):
        t = self._tracking_table(name)
        t.tags = [self.embody.par.Tsvtag.eval()]
        return t

    def _record(self, *paths):
        return ('path\ttype\tstrategy\trel_file_path\r\n'
                + ''.join(f'{p}\tbase\ttdxn\tx.tdxn\r\n' for p in paths))

    def test_unlinked_empty_table_relinks_to_its_tsv(self):
        ext = self.embody_ext
        status = self.embody.par.Status.eval()
        t = self._unlinked_table('tracking176')
        _, rel = self._table_file(t, self._record(self.sandbox.path))
        self.assertTrue(ext._relinkTableToDisk(t))
        self.assertEqual(ext.normalizePath(t.par.file.eval()),
                         ext.normalizePath(rel))
        self.assertTrue(t.par.syncfile.eval())
        self.assertEqual(t.numRows, 2, 'rows come from disk at once')
        self.assertEqual(t[1, 'path'].val, self.sandbox.path)
        self.assertEqual(self.embody.par.Status.eval(), status,
                         'an enabled project stays as it was')

    def test_relink_never_replaces_rows_or_guesses(self):
        ext = self.embody_ext
        held = self._tracking_table('held176', [('/y', 'tdxn', 'y.tdxn')])
        held.tags = [self.embody.par.Tsvtag.eval()]
        self._table_file(held, self._record())
        self.assertFalse(ext._relinkTableToDisk(held),
                         'a table holding rows is never replaced from disk')
        self.assertEqual(held.par.file.eval(), '')
        missing = self._unlinked_table('missing176')
        self.assertFalse(ext._relinkTableToDisk(missing),
                         'no file on disk, nothing to link')
        self.assertEqual(missing.par.file.eval(), '')

    def test_relink_keeps_rows_built_after_the_save(self):
        """Operators built after the last save are not in the .toe; their
        rows are what lets the restores bring them back."""
        ext = self.embody_ext
        t = self._unlinked_table('unsaved176')
        self._table_file(t, self._record('/nowhere176/a', '/nowhere176/b'))
        self.assertTrue(ext._relinkTableToDisk(t))
        self.assertEqual(t.numRows, 3)

    def test_relink_needs_evidence_the_file_is_this_projects(self):
        ext = self.embody_ext
        bad = self._unlinked_table('header176')
        self._table_file(bad, 'not\ta\ttracking\tfile\r\n')
        self.assertFalse(ext._relinkTableToDisk(bad), 'not an Embody header')
        old = self._unlinked_table('older176')
        save, _ = self._table_file(old, self._record(self.sandbox.path))
        toe = ext._resolveProjectToe()
        self.assertTrue(toe, 'precondition: a saved .toe')
        stamp = os.path.getmtime(toe) - 60
        os.utime(save, (stamp, stamp))
        self.assertFalse(ext._relinkTableToDisk(old),
                         'a file older than the saved .toe was seen by it')
        for t in (bad, old):
            self.assertEqual(t.par.file.eval(), '')

    def test_relink_never_overrides_a_disable(self):
        ext = self.embody_ext
        t = self._unlinked_table('disabled176')
        save, _ = self._table_file(t, self._record(self.sandbox.path))
        t.store(ext._DISABLED_MARK, time.time() + 1)
        self.assertFalse(ext._relinkTableToDisk(t))
        self.assertEqual(t.par.file.eval(), '')

    def test_relink_adopts_a_record_written_after_a_disable(self):
        """Disable deletes the tracking file, so one newer than the mark
        was written by a re-enabled session that was never saved."""
        ext = self.embody_ext
        t = self._unlinked_table('reenabled176')
        save, _ = self._table_file(t, self._record(self.sandbox.path))
        t.store(ext._DISABLED_MARK, os.path.getmtime(save) - 60)
        self.assertTrue(ext._relinkTableToDisk(t))

    def test_relink_skips_an_untagged_table(self):
        t = self._tracking_table('untagged176')
        self.assertFalse(self.embody_ext._relinkTableToDisk(t))

    def test_another_projects_newer_toe_blocks_the_relink(self):
        """Two .toe files in one folder share one tracking-file path."""
        newer = self.embody_ext._newerOtherProject
        root = Path(tempfile.mkdtemp(prefix='embody176_toe_'))
        self._temp_dirs.append(str(root))
        own = root / 'show.4.toe'
        stamp = time.time() - 3600
        for name in ('show.4.toe', 'show.toe', 'show.7.toe',
                     'CrashAutoSave.show.4.toe', 'other.toe'):
            (root / name).write_bytes(b'')
            self._temp_files.append(str(root / name))
            os.utime(root / name, (stamp, stamp))
        self.assertTrue(newer(str(own), stamp - 1), 'other.toe was saved later')
        self.assertFalse(newer(str(own), stamp + 1))
        for name in ('show.7.toe', 'CrashAutoSave.show.4.toe'):
            os.utime(root / name, (stamp + 600, stamp + 600))
        self.assertFalse(newer(str(own), stamp + 300),
                         "this project's increments and crash copy are its own")
        os.utime(root / 'other.toe', (stamp + 600, stamp + 600))
        self.assertTrue(newer(str(own), stamp + 300))

    # ----- cleanup of projects the old sweep over-tagged -------------------

    def test_stray_rows_classified(self):
        parent = self.embody.parent().path
        found = self.embody_ext._strayTrackedRows(
            self._stray_rows([(parent, 'tox', 'x/container.tox')]), tagged=[])
        self.assertEqual([r[0] for r in found['system']], ['/perform'])
        self.assertEqual([r[0] for r in found['retag']], [parent])
        self.assertEqual(sorted(r[0] for r in found['own']),
                         sorted([self.embody.path,
                                 self.embody.path + '/EmbodyExt']))

    def test_stray_tags_without_a_row(self):
        comp = self.sandbox.create(baseCOMP, 'tagonly176')
        tdxn = self.embody.par.Tdxntag.eval()
        td_own = SimpleNamespace(path='/ui/x176', family='DAT', tags={'py'})
        system_comp = SimpleNamespace(path='/perform/c176', family='COMP',
                                      tags={tdxn})
        found = self.embody_ext._strayTrackedRows(
            self._tracking_table('empty176'),
            tagged=[self.embody, comp, td_own, system_comp])
        self.assertEqual(found['own'], [(self.embody.path, None, '')],
                         'only the Embody-owned tag counts as stray')
        self.assertEqual(found['system'], [('/perform/c176', None, '')],
                         "TD's own tags in its system COMPs are not Embody's")

    def _run_cleanup(self, choice, answer, extra=()):
        calls, asked, kept = [], [], []
        self._stub('_untrackEntries',
                   lambda entries, delete_within=None:
                   calls.append(([e[0] for e in entries], delete_within)) or 0)
        self._stub('_ownTrackingChoice', lambda: choice)
        self._stub('_recordTracksOwnOperators', lambda: kept.append(True) or True)
        self._stub('_messageBox',
                   lambda title, message, buttons:
                   asked.append(title) or answer)
        self._stub('_reportOwnLeftovers', lambda folder, prune: None)
        self._stub('_updatePendingVerify', lambda: False)
        done = self.embody_ext.untrackStrayExternalizations(
            table=self._stray_rows(extra), tagged=[])
        return calls, asked, kept, done

    def _own(self):
        return sorted([self.embody.path, self.embody.path + '/EmbodyExt'])

    def test_cleanup_untracks_system_rows_and_asks_about_own(self):
        calls, asked, kept, _ = self._run_cleanup(False, 2)
        self.assertEqual(asked, [CLEANUP_TITLE])
        self.assertEqual(calls[0], (['/perform'], None))
        self.assertEqual((sorted(calls[1][0]), calls[1][1]), (self._own(), None))
        self.assertEqual(len(calls), 2, '/project1/geo1 is user content')
        self.assertEqual(kept, [])

    def test_cleanup_deletes_only_inside_embodys_mirror(self):
        calls, _, _, _ = self._run_cleanup(False, 1)
        self.assertEqual(calls[0], (['/perform'], None),
                         'system COMP files are never deleted by the cleanup')
        self.assertEqual(calls[1][1], self.embody_ext._ownMirrorFolder())

    def test_cleanup_leaves_own_rows_when_project_tracks_them(self):
        calls, asked, _, _ = self._run_cleanup(True, 2)
        self.assertEqual(asked, [], 'no prompt for a project that tracks Embody')
        self.assertEqual(calls, [(['/perform'], None)])

    def test_cleanup_never_asks_when_project_json_is_unreadable(self):
        calls, asked, _, _ = self._run_cleanup(None, 1)
        self.assertEqual(asked, [], 'unknown is not a reason to delete')
        self.assertEqual(calls, [(['/perform'], None)])

    def test_cleanup_records_keep_choice(self):
        calls, _, kept, _ = self._run_cleanup(False, 3)
        self.assertEqual(kept, [True])
        self.assertEqual(calls, [(['/perform'], None)])

    def test_cleanup_not_now_changes_nothing_of_embodys(self):
        calls, _, kept, _ = self._run_cleanup(False, 0)
        self.assertEqual(calls, [(['/perform'], None)])
        self.assertEqual(kept, [])

    def _container_switch(self, tdxn_on):
        """A sandbox COMP stands in for the container: in Embody's own repo
        any par change on /embody reinitializes Embody's extensions."""
        holder = self.sandbox.create(baseCOMP, 'holder176')
        holder.par.externaltox = 'x/holder176.tox'
        exported = []
        self._stub('_strayTrackedRows', lambda table=None, tagged=None: {
            'system': [], 'own': [],
            'retag': [(holder.path, None, '')]})   # tag only, no row
        self._stub('_tdxnEnabled', lambda: tdxn_on)
        self._stub('_handleTDXNAddition', lambda oper: exported.append(oper))
        calls, _, _, done = self._run_cleanup(True, 0)
        self.assertEqual(calls, [([holder.path], None)])
        self.assertEqual(holder.par.externaltox.eval(), '',
                         'even a tag-only entry stops loading the old .tox')
        self.assertEqual(exported, [], 'the cleanup itself never exports')
        return holder, done

    def test_cleanup_switches_a_tox_container_to_tdxn(self):
        """The COMP holding Embody is user content: keep it tracked as TDXN
        instead of untracking it (its .tox would carry all of Embody)."""
        holder, done = self._container_switch(True)
        self.assertEqual(done['retag'], [holder.path])
        self.assertIn(self.embody.par.Tdxntag.val, holder.tags)

    def test_cleanup_only_untracks_a_tox_container_with_tdxn_off(self):
        holder, done = self._container_switch(False)
        self.assertEqual(done['retag'], [], 'TDXN off: Embody touches no file')
        self.assertNotIn(self.embody.par.Tdxntag.val, holder.tags)

    def test_cleanup_waits_for_an_update_to_be_verified(self):
        asked = []
        self._stub('_updatePendingVerify', lambda: True)
        self._stub('_messageBox', lambda *a, **k: asked.append(a) or 1)
        done = self.embody_ext.untrackStrayExternalizations(
            table=self._stray_rows(), tagged=[])
        self.assertEqual(done, {'system': [], 'retag': [], 'own': []})
        self.assertEqual(asked, [])

    def test_an_update_is_pending_while_its_sentinel_is_fresh(self):
        """verify() clears Embody's externaltox at frame 30 of an update, so
        the updater's sentinel is the witness that it is unverified."""
        ext = self.embody_ext
        root = Path(tempfile.mkdtemp(prefix='embody176_upd_'))
        updates = root / '.embody' / 'updates'
        updates.mkdir(parents=True)
        sentinel = updates / 'pending.json'
        self._temp_dirs.extend([str(root), str(root / '.embody'), str(updates)])
        self._temp_files.append(str(sentinel))
        self._stub('_findProjectRoot', lambda: root)
        self.assertFalse(ext._updatePendingVerify(), 'no sentinel, no update')
        sentinel.write_text('{}', encoding='utf-8')
        self.assertTrue(ext._updatePendingVerify())
        stamp = time.time() - 3600
        os.utime(sentinel, (stamp, stamp))
        self.assertFalse(ext._updatePendingVerify(),
                         'a leftover sentinel never blocks the cleanup')

    def test_untrack_entries_deletes_inside_the_folder_only(self):
        ext = self.embody_ext
        dat = self.sandbox.create(textDAT, 'untrack176')
        dat.text = '# issue 176'
        strategy, rel = self._externalized(dat, self.embody.par.Pytag.eval())
        path = ext.buildAbsolutePath(rel)
        self.assertTrue(path.is_file(), 'precondition: the DAT is externalized')
        ext._untrackEntries([(dat.path, strategy, rel)],
                            delete_within='embody/__no_such_mirror_176__')
        self.assertTrue(path.is_file(), 'a file outside the folder is kept')
        self.assertEqual(self._rows_for(dat.path), [])
        self.assertEqual(dat.par.file.eval(), '')
        self.assertFalse(set(ext.getTags()).intersection(dat.tags))
        self._temp_files.append(str(path))

    def test_untrack_entries_deletes_a_tdxn_and_its_breadcrumb(self):
        ext = self.embody_ext
        comp = self.sandbox.create(baseCOMP, 'untrack176_comp')
        strategy, rel = self._externalized(comp, self.embody.par.Tdxntag.eval())
        path = ext.buildAbsolutePath(rel)
        self.assertTrue(path.is_file(), 'precondition: the .tdxn is written')
        deleted = ext._untrackEntries(
            [(comp.path, strategy, rel)],
            delete_within=str(Path(rel).parent).replace('\\', '/'))
        self.assertEqual(deleted, 1)
        self.assertFalse(path.is_file())
        self.assertEqual(self._rows_for(comp.path), [])
        self.assertEqual(comp.fetch('_tdn_rel_path', '', search=False), '')
        self.assertFalse(set(ext.getTags()).intersection(comp.tags))

    def test_untrack_entries_keeps_a_file_another_op_reads(self):
        ext = self.embody_ext
        dat = self.sandbox.create(textDAT, 'shared176')
        dat.text = '# shared'
        strategy, rel = self._externalized(dat, self.embody.par.Pytag.eval())
        reader = self.sandbox.create(textDAT, 'reader176')
        reader.par.file = rel
        path = ext.buildAbsolutePath(rel)
        deleted = ext._untrackEntries(
            [(dat.path, strategy, rel)],
            delete_within=str(Path(rel).parent).replace('\\', '/'))
        self.assertEqual(deleted, 0)
        self.assertTrue(path.is_file(), 'a live reader keeps the file')
        reader.par.file = ''
        self._temp_files.append(str(path))

    def test_untrack_entries_keeps_a_file_par_naming_another_file(self):
        """TD's own DATs in its system COMPs carry file pars of their own."""
        ext = self.embody_ext
        dat = self.sandbox.create(textDAT, 'ownfile176')
        dat.par.file = 'somewhere/else176.txt'
        dat.tags.add(self.embody.par.Pytag.eval())
        ext._untrackEntries([(dat.path, None, '')])
        self.assertEqual(dat.par.file.eval(), 'somewhere/else176.txt')
        self.assertFalse(set(ext.getTags()).intersection(dat.tags))

    def test_own_mirror_folder_is_where_embodys_files_land(self):
        ext = self.embody_ext
        folder = ext._ownMirrorFolder()
        self.assertTrue(folder.endswith(self.embody.path.strip('/')), folder)
        rel = ext.getExternalPath(self.embody.op('EmbodyExt'))
        if rel:  # tracked only where Embody's internals are on purpose
            self.assertEqual(ext.normalizePath(str(Path(rel).parent)), folder)

    def test_empty_folder_prune_works_and_spares_scm_dirs(self):
        """_SCM_DIRS went missing in v6.0.261, so every empty-folder sweep
        (Disable, the cleanup's mirror prune) died on its first folder."""
        ext = self.embody_ext
        root = ext.buildAbsolutePath('embody/unit_tests/test_sandbox/prune176')
        deep = root / 'a' / 'b' / 'c'
        scm = root / '.git' / 'objects'
        for d in (deep, scm):
            d.mkdir(parents=True, exist_ok=True)
        self._temp_dirs.extend(str(p) for p in (
            root, root / 'a', root / 'a' / 'b', deep, root / '.git', scm))
        ext.deleteEmptyDirectories(root)
        self.assertFalse((root / 'a').exists(), 'empty folders are pruned')
        self.assertTrue(scm.is_dir(), 'version-control folders are never touched')

    def test_empty_folder_prune_refuses_a_folder_holding_the_project(self):
        ext = self.embody_ext
        mod_os = self.embody.op('EmbodyExt').module.os
        walk = mod_os.walk

        def walked(*a, **k):
            raise AssertionError('walked a folder holding the project')

        try:
            mod_os.walk = walked
            ext.deleteEmptyDirectories(Path(project.folder))
            ext.deleteEmptyDirectories(Path(project.folder).parent)
        finally:
            mod_os.walk = walk

    def test_update_subtracts_embodys_rows_only_where_tracked_on_purpose(self):
        """An update reload strips the tags from Embody's own operators;
        their rows must survive the sweep so the cleanup can still delete
        their files. Embody's own repo untracks them as any other op."""
        ext = self.embody_ext
        inner = self.embody.op('EmbodyExt')
        user = self.sandbox.create(textDAT, 'subtract176')
        self._stub('_ownTrackingChoice', lambda: False)
        self.assertFalse(ext._isSubtraction(inner, set(), []))
        self.assertTrue(ext._isSubtraction(user, set(), []),
                        'an untagged user op is still a subtraction')
        self._stub('_ownTrackingChoice', lambda: None)
        self.assertFalse(ext._isSubtraction(inner, set(), []),
                         'an unreadable project.json drops no own rows')
        self._stub('_ownTrackingChoice', lambda: True)
        self.assertTrue(ext._isSubtraction(inner, set(), []))

    def test_keep_choice_round_trips_through_project_json(self):
        ext = self.embody_ext
        admin = self.embody.op('embody_admin').module
        root = Path(tempfile.mkdtemp(prefix='embody176_'))
        meta = root / '.embody'
        meta.mkdir()
        pj = meta / 'project.json'
        pj.write_text(json.dumps({'envoy': {'enabled': True}}), encoding='utf-8')
        self._temp_files.append(str(pj))
        self._temp_dirs.extend([str(root), str(meta)])
        self._stub('_findProjectRoot', lambda: root)
        self.assertIs(admin.read_embody_internals_tracked(ext), False)
        self.assertTrue(admin.record_embody_internals_tracked(ext))
        self.assertIs(admin.read_embody_internals_tracked(ext), True)
        data = json.loads(pj.read_text(encoding='utf-8'))
        self.assertEqual(data.get('envoy'), {'enabled': True},
                         'keys Embody does not own are kept')
        pj.write_text('{not json', encoding='utf-8')
        self.assertIsNone(admin.read_embody_internals_tracked(ext),
                          'unreadable is unknown, never "not tracked"')
        self.assertFalse(admin.record_embody_internals_tracked(ext))
