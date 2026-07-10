"""Pure-logic tests for EmbodyExt's git-uncommitted status helpers
(_parseGitPorcelain, _mapChangedToOps, _rowHasChanges). These drive the static
methods directly with fixtures -- no git repo, no sandbox operators --
referencing the extension inline per the no-ext-caching rule.
"""

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase


class TestParseGitPorcelain(EmbodyTestCase):
    """`git status --porcelain -z` -> {repo_rel_posix: code}."""

    def test_empty(self):
        self.assertEqual(op.Embody.ext.Embody._parseGitPorcelain(''), {})

    def test_modified(self):
        out = ' M dev/embody/a.py\0'
        self.assertEqual(op.Embody.ext.Embody._parseGitPorcelain(out),
                         {'dev/embody/a.py': ' M'})

    def test_untracked_counts_as_changed(self):
        out = '?? dev/embody/new.py\0'
        self.assertEqual(op.Embody.ext.Embody._parseGitPorcelain(out),
                         {'dev/embody/new.py': '??'})

    def test_staged_add(self):
        out = 'A  dev/embody/a.py\0'
        self.assertEqual(op.Embody.ext.Embody._parseGitPorcelain(out),
                         {'dev/embody/a.py': 'A '})

    def test_deleted(self):
        out = ' D dev/embody/gone.py\0'
        self.assertEqual(op.Embody.ext.Embody._parseGitPorcelain(out),
                         {'dev/embody/gone.py': ' D'})

    def test_multiple_entries(self):
        out = ' M a.py\0?? b.py\0 D c.py\0'
        result = op.Embody.ext.Embody._parseGitPorcelain(out)
        self.assertEqual(set(result), {'a.py', 'b.py', 'c.py'})

    def test_rename_records_both_paths(self):
        # -z renames carry two NUL-separated paths (new + origin); both recorded
        # so the membership test hits whichever currently exists on disk.
        out = 'R  dev/new.py\0dev/old.py\0'
        result = op.Embody.ext.Embody._parseGitPorcelain(out)
        self.assertIn('dev/new.py', result)
        self.assertIn('dev/old.py', result)

    def test_path_with_space_unquoted(self):
        # -z means NO path quoting, so spaces/unicode survive intact.
        out = ' M dev/my file.py\0'
        self.assertEqual(op.Embody.ext.Embody._parseGitPorcelain(out),
                         {'dev/my file.py': ' M'})

    def test_garbage_tokens_ignored(self):
        # Stray/short tokens (no "XY " prefix) are skipped, not crashed on.
        out = 'x\0\0 M ok.py\0'
        self.assertEqual(op.Embody.ext.Embody._parseGitPorcelain(out),
                         {'ok.py': ' M'})


class TestMapChangedToOps(EmbodyTestCase):
    """Pure string mapping of a git changed-set to {op_path: code}."""

    def _m(self, changed, prefix, rows):
        return op.Embody.ext.Embody._mapChangedToOps(changed, prefix, rows)

    def test_empty_changed(self):
        self.assertEqual(self._m({}, 'dev/', [('/x', 'embody/a.py')]), {})

    def test_prefix_match(self):
        self.assertEqual(
            self._m({'dev/embody/a.py': ' M'}, 'dev/', [('/x', 'embody/a.py')]),
            {'/x': ' M'})

    def test_no_prefix_when_root_is_project(self):
        self.assertEqual(
            self._m({'foo.tox': '??'}, '', [('/y', 'foo.tox')]),
            {'/y': '??'})

    def test_unchanged_row_skipped(self):
        out = self._m({'dev/embody/a.py': ' M'}, 'dev/',
                      [('/x', 'embody/a.py'), ('/y', 'embody/clean.py')])
        self.assertEqual(out, {'/x': ' M'})

    def test_synthetic_and_root_rows_skipped(self):
        # path '/' and empty-rel synthetic-parent rows never map.
        out = self._m({'dev/embody/a.py': ' M'}, 'dev/',
                      [('/', 'whole.tdn'), ('/parent', ''), ('/x', 'embody/a.py')])
        self.assertEqual(out, {'/x': ' M'})

    def test_backslash_rel_normalized(self):
        self.assertEqual(
            self._m({'dev/embody/b.py': ' M'}, 'dev/', [('/w', 'embody\\b.py')]),
            {'/w': ' M'})


class TestRowHasChanges(EmbodyTestCase):
    """The 'changed' filter predicate: unsaved OR git-uncommitted."""

    def test_clean_committed_is_false(self):
        self.assertFalse(op.Embody.ext.Embody._rowHasChanges('', None))
        self.assertFalse(op.Embody.ext.Embody._rowHasChanges('', ''))

    def test_unsaved_dirty_is_true(self):
        self.assertTrue(op.Embody.ext.Embody._rowHasChanges('True', None))
        self.assertTrue(op.Embody.ext.Embody._rowHasChanges('1', None))

    def test_par_change_is_true(self):
        self.assertTrue(op.Embody.ext.Embody._rowHasChanges('Par', None))

    def test_uncommitted_only_is_true(self):
        # DAT case: saved via syncfile (no dirty) but uncommitted -> changed.
        self.assertTrue(op.Embody.ext.Embody._rowHasChanges('', ' M'))
        self.assertTrue(op.Embody.ext.Embody._rowHasChanges('', '??'))

    def test_both_axes_is_true(self):
        self.assertTrue(op.Embody.ext.Embody._rowHasChanges('True', ' M'))
