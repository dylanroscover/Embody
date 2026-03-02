"""
Test suite: Path utility methods in EmbodyExt.

Tests normalizePath, getExternalPath, setExternalPath,
buildAbsolutePath, and getOpPaths.
"""

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase


class TestPathUtils(EmbodyTestCase):

    # --- normalizePath ---

    def test_normalizePath_backslashes_converted(self):
        result = self.embody_ext.normalizePath('foo\\bar\\baz')
        self.assertEqual(result, 'foo/bar/baz')

    def test_normalizePath_forward_slashes_unchanged(self):
        result = self.embody_ext.normalizePath('foo/bar/baz')
        self.assertEqual(result, 'foo/bar/baz')

    def test_normalizePath_mixed_slashes(self):
        result = self.embody_ext.normalizePath('foo\\bar/baz\\qux')
        self.assertEqual(result, 'foo/bar/baz/qux')

    def test_normalizePath_none_returns_none(self):
        result = self.embody_ext.normalizePath(None)
        self.assertIsNone(result)

    def test_normalizePath_empty_string_returns_falsy(self):
        result = self.embody_ext.normalizePath('')
        self.assertFalse(result)

    def test_normalizePath_pathlib_path(self):
        from pathlib import Path
        result = self.embody_ext.normalizePath(Path('foo/bar'))
        self.assertNotIn('\\', result)
        self.assertIn('foo', result)
        self.assertIn('bar', result)

    # --- getExternalPath ---

    def test_getExternalPath_comp_returns_externaltox(self):
        comp = self.sandbox.create(baseCOMP, 'test_comp')
        comp.par.externaltox = 'some/path.tox'
        result = self.embody_ext.getExternalPath(comp)
        self.assertEqual(result, 'some/path.tox')

    def test_getExternalPath_comp_normalizes_backslashes(self):
        comp = self.sandbox.create(baseCOMP, 'test_comp')
        comp.par.externaltox = 'some\\path.tox'
        result = self.embody_ext.getExternalPath(comp)
        self.assertEqual(result, 'some/path.tox')

    def test_getExternalPath_dat_returns_file(self):
        dat = self.sandbox.create(textDAT, 'test_dat')
        dat.par.file = 'some/path.py'
        result = self.embody_ext.getExternalPath(dat)
        self.assertEqual(result, 'some/path.py')

    def test_getExternalPath_empty_returns_falsy(self):
        comp = self.sandbox.create(baseCOMP, 'test_comp')
        result = self.embody_ext.getExternalPath(comp)
        self.assertFalse(result)

    # --- setExternalPath ---

    def test_setExternalPath_comp_sets_externaltox(self):
        comp = self.sandbox.create(baseCOMP, 'test_comp')
        self.embody_ext.setExternalPath(comp, 'new/path.tox')
        self.assertEqual(comp.par.externaltox.eval(), 'new/path.tox')

    def test_setExternalPath_comp_normalizes(self):
        comp = self.sandbox.create(baseCOMP, 'test_comp')
        self.embody_ext.setExternalPath(comp, 'new\\path.tox')
        self.assertEqual(comp.par.externaltox.eval(), 'new/path.tox')

    def test_setExternalPath_comp_readonly_default(self):
        comp = self.sandbox.create(baseCOMP, 'test_comp')
        self.embody_ext.setExternalPath(comp, 'new/path.tox')
        self.assertTrue(comp.par.externaltox.readOnly)

    def test_setExternalPath_comp_not_readonly(self):
        comp = self.sandbox.create(baseCOMP, 'test_comp')
        self.embody_ext.setExternalPath(comp, 'new/path.tox', readonly=False)
        self.assertFalse(comp.par.externaltox.readOnly)

    def test_setExternalPath_dat_sets_file(self):
        dat = self.sandbox.create(textDAT, 'test_dat')
        self.embody_ext.setExternalPath(dat, 'new/path.py')
        self.assertEqual(dat.par.file.eval(), 'new/path.py')

    # --- buildAbsolutePath ---

    def test_buildAbsolutePath_returns_path_object(self):
        from pathlib import Path
        result = self.embody_ext.buildAbsolutePath('embody/test.tox')
        self.assertIsInstance(result, Path)

    def test_buildAbsolutePath_contains_rel_path(self):
        result = self.embody_ext.buildAbsolutePath('embody/test.tox')
        result_str = str(result).replace('\\', '/')
        self.assertIn('embody/test.tox', result_str)

    def test_buildAbsolutePath_is_absolute(self):
        result = self.embody_ext.buildAbsolutePath('embody/test.tox')
        self.assertTrue(result.is_absolute())
