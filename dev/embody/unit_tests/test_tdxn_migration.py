"""
Test suite: TDN -> TDXN migration (EmbodyExt.MigrateToTDXN).

The migration is the one v6.1.0 path that MOVES a user's files, so it gets a
full smoke project rather than unit pokes: _buildLegacyProject() externalizes
a three-level nested tree, then forces it into the pre-6.1 on-disk shape --
every file .tdn, every parent's tdn_ref pointing at a .tdn -- and the tests
run the REAL migration against it, scoped to this suite's sandbox.

Scoping is what makes that safe. MigrateToTDXN(scope=...) restricts renames
to one subtree, so a run here can never touch the live project's own tracked
files. Parent scanning stays global on purpose (a parent OUTSIDE the scope
can still hold a tdn_ref into it), and test_out_of_scope_files_are_untouched
pins that boundary.

Covered: conversion, table rows, nested tdn_ref repointing, byte-identical
content (so git sees a rename, not a rewrite), idempotency, both crash-resume
shapes, the ambiguous-row refusal, dry-run inertness, and that a migrated
network still imports.
"""

import os

try:
    runner_mod = op.unit_tests.op('TestRunnerExt').module
    EmbodyTestCase = runner_mod.EmbodyTestCase
except (AttributeError, NameError):
    pass


class TestTdxnMigration(EmbodyTestCase):

    def setUp(self):
        """A clean workspace per test -- every test builds its own project."""
        self.workspace = self.sandbox.create(baseCOMP, 'workspace')

    def tearDown(self):
        """Drop this suite's rows AND its files.

        Files matter here in a way they do not for most suites: this suite
        deliberately creates .tdn/.tdxn pairs on disk, and a leftover pair
        would look like the ambiguous-row case to the NEXT run.
        """
        table = self.embody_ext.Externalizations
        for i in range(table.numRows - 1, 0, -1):
            path = self.embody_ext._cellVal(i, 'path', table=table)
            if not path.startswith(self.sandbox.path):
                continue
            rel = self.embody_ext._cellVal(i, 'rel_file_path', table=table)
            if rel:
                for candidate in (rel, rel[:rel.rfind('.')] + '.tdn',
                                  rel[:rel.rfind('.')] + '.tdxn'):
                    try:
                        f = self._abs(candidate)
                        if f.is_file():
                            f.unlink()
                    except Exception:
                        pass
            table.deleteRow(i)
        super().tearDown()

    # ------------------------------------------------------------------
    # Building the legacy project
    # ------------------------------------------------------------------

    def _abs(self, rel):
        return self.embody_ext.buildAbsolutePath(
            self.embody_ext.normalizePath(rel)).resolve()

    def _rel(self, comp_path):
        return self.embody_ext.normalizePath(
            self.embody_ext._getStrategyFilePath(comp_path, 'tdn') or '')

    def _externalize(self, parent, name):
        """Create a TDN-tagged COMP with content and externalize it."""
        comp = parent.create(baseCOMP, name)
        comp.create(constantTOP, 'content')
        comp.tags.add(self.embody.par.Tdntag.val)
        self.embody_ext.handleAddition(comp)
        return comp

    def _buildLegacyProject(self):
        """A nested TDN project in the pre-6.1 shape: all .tdn on disk.

        Returns {name: comp_path}. Three levels so parent tdn_ref pointers
        exist at two depths -- a one-level tree would not exercise the
        rewrite at all.
        """
        root = self._externalize(self.workspace, 'mig_root')
        child = self._externalize(root, 'mig_child')
        self._externalize(child, 'mig_leaf')
        sibling = self._externalize(root, 'mig_sibling')

        paths = {
            'root': root.path,
            'child': child.path,
            'leaf': child.op('mig_leaf').path,
            'sibling': sibling.path,
        }

        # Force every file back to .tdn (v6.1.0 mints .tdxn), deepest first
        # so a parent is never re-exported before its children are renamed.
        for key in ('leaf', 'child', 'sibling', 'root'):
            rel = self._rel(paths[key])
            if not rel or not rel.endswith('.tdxn'):
                continue
            legacy = rel[:-len('.tdxn')] + '.tdn'
            src, dst = self._abs(rel), self._abs(legacy)
            if src.is_file():
                dst.parent.mkdir(parents=True, exist_ok=True)
                src.replace(dst)
            self.embody_ext._updateRowCells(
                paths[key], {'rel_file_path': legacy}, strategy='tdn')

        # Re-export the parents so their tdn_ref pointers regenerate from
        # the now-.tdn table rows. Without this the refs still say .tdxn and
        # the fixture would not be a real pre-6.1 project.
        for key in ('child', 'root'):
            self.embody_ext.SaveTDN(paths[key])

        return paths

    def _refs(self, comp_path):
        """Every tdn_ref inside one tracked file."""
        rel = self._rel(comp_path)
        path = self._abs(rel)
        if not path.is_file():
            return set()
        doc = self.embody.ext.TDXN.tdn_load(path.read_text(encoding='utf-8'))
        return self.embody_ext._collectTDNRefs(doc)

    def _migrate(self, **kw):
        kw.setdefault('auto', True)
        kw.setdefault('scope', self.workspace.path)
        return self.embody_ext.MigrateToTDXN(**kw)

    # ------------------------------------------------------------------
    # The fixture itself must be a real legacy project
    # ------------------------------------------------------------------

    def test_fixture_starts_fully_tdn(self):
        """Guard the guard: if the fixture is not .tdn, nothing below means
        anything."""
        paths = self._buildLegacyProject()
        for key, comp_path in paths.items():
            rel = self._rel(comp_path)
            self.assertTrue(rel.endswith('.tdn'),
                            '%s should start as .tdn, got %r' % (key, rel))
            self.assertTrue(self._abs(rel).is_file(),
                            '%s file missing on disk: %s' % (key, rel))
        refs = self._refs(paths['root'])
        self.assertTrue(refs, 'root should carry tdn_ref pointers')
        self.assertTrue(all(r.endswith('.tdn') for r in refs),
                        'fixture refs must all be .tdn, got %s' % sorted(refs))

    # ------------------------------------------------------------------
    # Core conversion
    # ------------------------------------------------------------------

    def test_every_tracked_file_converts(self):
        paths = self._buildLegacyProject()
        res = self._migrate()
        self.assertEqual(res.get('failed'), [], repr(res.get('failed')))
        self.assertEqual(res.get('skipped'), [], repr(res.get('skipped')))
        for key, comp_path in paths.items():
            rel = self._rel(comp_path)
            self.assertTrue(rel.endswith('.tdxn'),
                            '%s not migrated: %r' % (key, rel))
            self.assertTrue(self._abs(rel).is_file(),
                            '%s missing after migration: %s' % (key, rel))

    def test_old_files_are_gone_not_copied(self):
        """A rename, not a copy -- a leftover .tdn would be an untracked
        orphan that stale-file cleanup can never reclaim."""
        paths = self._buildLegacyProject()
        before = {k: self._rel(p) for k, p in paths.items()}
        self._migrate()
        for key, old_rel in before.items():
            self.assertFalse(self._abs(old_rel).is_file(),
                             'legacy file survived the rename: %s' % old_rel)

    def test_parent_tdn_refs_are_repointed(self):
        """The failure this prevents: children renamed, parents still
        pointing at the old names, so nested COMPs come back empty."""
        paths = self._buildLegacyProject()
        self._migrate()
        for key in ('root', 'child'):
            refs = self._refs(paths[key])
            self.assertTrue(refs, '%s lost its tdn_ref pointers' % key)
            for ref in refs:
                self.assertTrue(ref.endswith('.tdxn'),
                                '%s still points at %r' % (key, ref))
                self.assertTrue(self._abs(ref).is_file(),
                                '%s ref does not resolve: %s' % (key, ref))

    def test_leaf_content_is_byte_identical(self):
        """Leaves are renamed untouched, so git scores them R100 and
        `git log --follow` survives. Only parents get a content edit."""
        paths = self._buildLegacyProject()
        leaf_rel = self._rel(paths['leaf'])
        before = self._abs(leaf_rel).read_bytes()
        self._migrate()
        after = self._abs(self._rel(paths['leaf'])).read_bytes()
        self.assertEqual(before, after,
                         'leaf content changed during migration')

    # ------------------------------------------------------------------
    # Idempotency and crash resume -- the whole safety argument
    # ------------------------------------------------------------------

    def test_second_run_is_a_noop(self):
        self._buildLegacyProject()
        self._migrate()
        again = self._migrate()
        self.assertEqual(again.get('migrated'), [])
        self.assertEqual(again.get('row_only'), [])
        self.assertEqual(again.get('rename_only'), [])
        self.assertEqual(again.get('failed'), [])

    def test_resumes_when_file_renamed_but_row_stale(self):
        """Crash between pass A and pass B: file is .tdxn, row still .tdn."""
        paths = self._buildLegacyProject()
        rel = self._rel(paths['leaf'])
        moved = rel[:-len('.tdn')] + '.tdxn'
        self._abs(rel).replace(self._abs(moved))

        res = self._migrate()
        self.assertEqual(res.get('failed'), [], repr(res.get('failed')))
        self.assertTrue(self._rel(paths['leaf']).endswith('.tdxn'))
        self.assertTrue(self._abs(self._rel(paths['leaf'])).is_file())

    def test_resumes_when_row_written_but_file_unrenamed(self):
        """Crash between pass B and a completed rename: row is .tdxn, the
        file on disk is still .tdn."""
        paths = self._buildLegacyProject()
        rel = self._rel(paths['leaf'])
        self.embody_ext._updateRowCells(
            paths['leaf'], {'rel_file_path': rel[:-len('.tdn')] + '.tdxn'},
            strategy='tdn')

        res = self._migrate()
        self.assertEqual(res.get('failed'), [], repr(res.get('failed')))
        final = self._rel(paths['leaf'])
        self.assertTrue(final.endswith('.tdxn'))
        self.assertTrue(self._abs(final).is_file(),
                        'resume did not land the file at the row path')

    def test_ambiguous_row_is_refused_and_nothing_is_deleted(self):
        """Both extensions present: never guess which is authoritative."""
        paths = self._buildLegacyProject()
        rel = self._rel(paths['leaf'])
        twin = self._abs(rel[:-len('.tdn')] + '.tdxn')
        twin.write_text(self._abs(rel).read_text(encoding='utf-8'),
                        encoding='utf-8')

        res = self._migrate()
        self.assertTrue(any('BOTH' in s for s in res.get('skipped', [])),
                        'ambiguous row should be skipped: %r' % res)
        self.assertTrue(self._abs(rel).is_file(), 'refused row lost its .tdn')
        self.assertTrue(twin.is_file(), 'refused row lost its .tdxn')

    # ------------------------------------------------------------------
    # Blast radius
    # ------------------------------------------------------------------

    def test_dry_run_writes_nothing(self):
        paths = self._buildLegacyProject()
        before = {k: self._rel(p) for k, p in paths.items()}
        plan = self._migrate(dry_run=True)
        self.assertTrue(plan.get('migrated'), 'dry run reported no plan')
        for key, rel in before.items():
            self.assertEqual(self._rel(paths[key]), rel,
                             'dry run mutated the %s row' % key)
            self.assertTrue(self._abs(rel).is_file(),
                            'dry run moved the %s file' % key)

    def test_out_of_scope_files_are_untouched(self):
        """The guarantee that lets this suite run beside the live project."""
        self._buildLegacyProject()
        outside = {}
        table = self.embody_ext.Externalizations
        for i in range(1, table.numRows):
            if self.embody_ext._cellVal(i, 'strategy', table=table) != 'tdn':
                continue
            p = self.embody_ext._cellVal(i, 'path', table=table)
            if not p.startswith(self.workspace.path):
                outside[p] = self.embody_ext._cellVal(
                    i, 'rel_file_path', table=table)

        self._migrate()

        for p, rel in outside.items():
            self.assertEqual(
                self.embody_ext._cellVal(
                    self.embody_ext.cleanupDuplicateRows(p) or 0,
                    'rel_file_path'),
                rel, 'migration escaped its scope and touched %s' % p)

    def test_unscoped_call_still_sees_the_whole_table(self):
        """Scope is opt-in: the shipped pulse passes none, so a bare
        dry_run must plan across every tracked row, not just a subtree."""
        self._buildLegacyProject()
        scoped = self.embody_ext.MigrateToTDXN(
            auto=True, dry_run=True, scope=self.workspace.path)
        every = self.embody_ext.MigrateToTDXN(auto=True, dry_run=True)
        self.assertGreaterEqual(
            len(every.get('migrated', [])), len(scoped.get('migrated', [])),
            'unscoped plan should cover at least the scoped one')

    # ------------------------------------------------------------------
    # The point of the whole exercise
    # ------------------------------------------------------------------

    def test_migrated_network_still_imports(self):
        """A migrated project must still rebuild from its files -- the
        migration is worthless if the result cannot be reconstructed."""
        paths = self._buildLegacyProject()
        self._migrate()

        leaf_rel = self._rel(paths['leaf'])
        doc = self.embody.ext.TDXN.tdn_load(
            self._abs(leaf_rel).read_text(encoding='utf-8'))
        self.assertIn('operators', doc)
        self.assertIn(doc.get('format'), ('tdxn', 'tdn'))

        target = op(paths['child']).create(baseCOMP, 'reimport_target')
        res = self.embody.ext.TDXN.ImportNetwork(
            target.path, doc, clear_first=True)
        self.assertTrue(res.get('success'), repr(res.get('error')))
        self.assertTrue(target.op('content') is not None,
                        'reimported COMP lost its content operator')

    # ------------------------------------------------------------------
    # The mint-vs-preserve rule (what stops a save renaming a user's file)
    # ------------------------------------------------------------------

    def test_tracked_suffix_mints_for_new_and_preserves_a_legacy_row(self):
        """_trackedTDNSuffix is the single mechanism behind "existing files
        are left alone". Both halves are asserted against LITERALS -- deriving
        either expectation from the function under test would pass whether the
        rule holds or not.
        """
        comp = self._externalize(self.workspace, 'suffix_probe')
        rel = self._rel(comp.path)
        self.assertTrue(rel.endswith('.tdxn'),
                        'a NEW externalization must mint .tdxn, got %r' % rel)
        self.assertEqual(self.embody_ext._trackedTDNSuffix(comp.path), '.tdxn')

        legacy = rel[:-len('.tdxn')] + '.tdn'
        src, dst = self._abs(rel), self._abs(legacy)
        if src.is_file():
            src.replace(dst)
        self.embody_ext._updateRowCells(
            comp.path, {'rel_file_path': legacy}, strategy='tdn')
        self.assertEqual(
            self.embody_ext._trackedTDNSuffix(comp.path), '.tdn',
            'a tracked legacy row must keep .tdn -- minting here is what '
            'silently renames (and for the root resync, deletes) user files')

    def test_rename_of_a_legacy_comp_keeps_its_suffix(self):
        """Renaming a .tdn COMP must move it to the new NAME, not convert it
        to .tdxn -- an unrequested migration performed as a bare file move.
        """
        comp = self._externalize(self.workspace, 'rename_legacy')
        rel = self._rel(comp.path)
        legacy = rel[:-len('.tdxn')] + '.tdn'
        src = self._abs(rel)
        if src.is_file():
            src.replace(self._abs(legacy))
        self.embody_ext._updateRowCells(
            comp.path, {'rel_file_path': legacy}, strategy='tdn')

        comp.name = 'rename_legacy_moved'
        self.embody_ext.checkOpsForContinuity(
            self.embody_ext.ExternalizationsFolder)
        moved = self._rel(comp.path)
        self.assertTrue(
            moved.endswith('.tdn'),
            'rename converted a legacy file to %r -- renames must preserve '
            'the tracked suffix' % moved)
        self.assertIn('rename_legacy_moved', moved,
                      'rename did not follow the new operator name: %r' % moved)

    # ------------------------------------------------------------------
    # Partial-failure and convergence (audit 2026-08-27)
    # ------------------------------------------------------------------

    def _break_refs_back_to_legacy(self, comp_path):
        """Rewrite only this file's tdn_ref lines back to .tdn."""
        abs_path = self._abs(self._rel(comp_path))
        lines = abs_path.read_text(encoding='utf-8').split('\n')
        for i, line in enumerate(lines):
            if 'tdn_ref:' in line and line.rstrip().endswith('.tdxn'):
                lines[i] = line.rstrip()[:-len('.tdxn')] + '.tdn'
        abs_path.write_text('\n'.join(lines), encoding='utf-8')

    def test_failed_rename_does_not_repoint_parent_refs(self):
        """A rename that did NOT land must not be advertised to Pass C.

        The parent would otherwise be repointed at a file that was never
        created -- a dangling ref, which is worse than the stale one it
        replaced because the child no longer resolves at all.
        """
        paths = self._buildLegacyProject()
        leaf_rel = self._rel(paths['leaf'])
        # A directory where the .tdxn should go makes replace() raise on
        # every platform, without patching anything.
        blocker = self._abs(leaf_rel[:-len('.tdn')] + '.tdxn')
        blocker.mkdir(parents=True, exist_ok=True)
        try:
            res = self._migrate()
            self.assertTrue(res.get('failed'),
                            'blocked rename should be reported as failed')
            self.assertTrue(self._abs(leaf_rel).is_file(),
                            'source must survive a failed rename')
            for ref in self._refs(paths['child']):
                self.assertTrue(
                    self._abs(ref).is_file(),
                    'parent repointed at a file that was never created: %s'
                    % ref)
        finally:
            try:
                blocker.rmdir()
            except Exception:
                pass

    def test_parent_scanned_by_stem_when_row_disagrees(self):
        """A parent whose row and file disagree is the crash-resume shape.

        Resolving it through the row drops it from the tdn_ref scan, so its
        children are renamed and it keeps pointing at the old names.
        """
        paths = self._buildLegacyProject()
        root_rel = self._rel(paths['root'])
        self.assertTrue(root_rel.endswith('.tdn'))
        # Row claims .tdxn; the file on disk is still .tdn.
        self.embody_ext._updateRowCells(
            paths['root'], {'rel_file_path': root_rel[:-len('.tdn')] + '.tdxn'},
            strategy='tdn')
        res = self._migrate()
        self.assertEqual(res.get('failed'), [], repr(res.get('failed')))
        refs = self._refs(paths['root'])
        self.assertTrue(refs, 'root lost its tdn_ref pointers')
        for ref in refs:
            self.assertTrue(
                ref.endswith('.tdxn'),
                'row/file mismatch dropped the parent from the scan: %r' % ref)
            self.assertTrue(self._abs(ref).is_file(),
                            'repointed ref does not resolve: %s' % ref)

    def test_dangling_refs_are_repaired_when_the_plan_is_empty(self):
        """A run that died before Pass C leaves every row migrated but the
        parents still pointing at .tdn. The plan is then empty, so the run
        must still scan and repair -- otherwise it can never converge.
        """
        paths = self._buildLegacyProject()
        self._migrate()
        self._break_refs_back_to_legacy(paths['root'])
        self.assertTrue(
            any(r.endswith('.tdn') for r in self._refs(paths['root'])),
            'fixture: root refs should be broken back to .tdn')

        res = self._migrate()
        self.assertEqual(res.get('migrated'), [],
                         'nothing left to rename; the plan must be empty')
        self.assertEqual(res.get('failed'), [], repr(res.get('failed')))
        for ref in self._refs(paths['root']):
            self.assertTrue(ref.endswith('.tdxn'),
                            'dangling ref not repaired: %r' % ref)
            self.assertTrue(self._abs(ref).is_file(),
                            'repaired ref does not resolve: %s' % ref)
