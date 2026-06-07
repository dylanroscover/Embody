"""
Test suite: MCP DAT content handlers in EnvoyExt.

Tests _get_dat_content, _set_dat_content, and _edit_dat_content.
"""

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase


class TestMCPDatContent(EmbodyTestCase):

    def setUp(self):
        super().setUp()

    # --- _get_dat_content ---

    def test_get_dat_content_text(self):
        dat = self.sandbox.create(textDAT, 'text_dat')
        dat.text = 'hello world'
        result = self.embody.ext.Envoy._get_dat_content(op_path=dat.path, format='text')
        self.assertDictHasKey(result, 'text')
        self.assertEqual(result['text'], 'hello world')

    def test_get_dat_content_table(self):
        dat = self.sandbox.create(tableDAT, 'table_dat')
        dat.appendRow(['a', 'b', 'c'])
        dat.appendRow(['1', '2', '3'])
        result = self.embody.ext.Envoy._get_dat_content(op_path=dat.path, format='table')
        self.assertDictHasKey(result, 'rows')

    def test_get_dat_content_auto_text(self):
        dat = self.sandbox.create(textDAT, 'auto_text')
        dat.text = 'auto detected'
        result = self.embody.ext.Envoy._get_dat_content(op_path=dat.path, format='auto')
        self.assertDictHasKey(result, 'text')

    def test_get_dat_content_auto_table(self):
        dat = self.sandbox.create(tableDAT, 'auto_table')
        dat.appendRow(['x', 'y'])
        result = self.embody.ext.Envoy._get_dat_content(op_path=dat.path, format='auto')
        self.assertDictHasKey(result, 'rows')

    def test_get_dat_content_nonexistent(self):
        result = self.embody.ext.Envoy._get_dat_content(
            op_path='/nonexistent', format='auto')
        self.assertDictHasKey(result, 'error')

    # --- _set_dat_content ---

    def test_set_dat_content_text(self):
        dat = self.sandbox.create(textDAT, 'set_text')
        result = self.embody.ext.Envoy._set_dat_content(
            op_path=dat.path, text='new content')
        self.assertTrue(result.get('success'))
        self.assertEqual(dat.text, 'new content')

    def test_set_dat_content_rows(self):
        dat = self.sandbox.create(tableDAT, 'set_rows')
        result = self.embody.ext.Envoy._set_dat_content(
            op_path=dat.path, rows=[['a', 'b'], ['1', '2']])
        self.assertTrue(result.get('success'))

    def test_set_dat_content_clear_requires_confirm(self):
        """clear=True with no replacement content must hit the wipe guard
        (v5.0.397) and refuse without confirm_wipe -- no 'success' key."""
        dat = self.sandbox.create(tableDAT, 'clear_guard')
        dat.appendRow(['existing', 'data'])
        before = dat.numRows
        result = self.embody.ext.Envoy._set_dat_content(
            op_path=dat.path, clear=True)
        self.assertDictHasKey(result, 'error')
        self.assertEqual(dat.numRows, before,
            'guarded clear must not wipe the DAT')

    def test_set_dat_content_clear_with_confirm(self):
        """clear=True + confirm_wipe=True bypasses the guard and empties it."""
        dat = self.sandbox.create(tableDAT, 'clear_confirmed')
        dat.appendRow(['existing', 'data'])
        result = self.embody.ext.Envoy._set_dat_content(
            op_path=dat.path, clear=True, confirm_wipe=True)
        self.assertTrue(result.get('success'))
        self.assertEqual(dat.numRows, 0)

    def test_set_dat_content_no_content_refused(self):
        """No actionable args (no text/rows/clear) hits the no-content guard."""
        dat = self.sandbox.create(textDAT, 'no_content')
        result = self.embody.ext.Envoy._set_dat_content(op_path=dat.path)
        self.assertDictHasKey(result, 'error')

    def test_set_dat_content_nonexistent(self):
        result = self.embody.ext.Envoy._set_dat_content(
            op_path='/nonexistent', text='test')
        self.assertDictHasKey(result, 'error')

    # --- _edit_dat_content (surgical text replace) ---

    def test_edit_dat_content_unique_match(self):
        dat = self.sandbox.create(textDAT, 'edit_unique')
        dat.text = 'alpha beta gamma'
        result = self.embody.ext.Envoy._edit_dat_content(
            op_path=dat.path, old_string='beta', new_string='BETA')
        self.assertTrue(result.get('success'))
        self.assertEqual(dat.text, 'alpha BETA gamma')
        self.assertEqual(result['replacements'], 1)

    def test_edit_dat_content_replace_all(self):
        dat = self.sandbox.create(textDAT, 'edit_all')
        dat.text = 'x x x'
        result = self.embody.ext.Envoy._edit_dat_content(
            op_path=dat.path, old_string='x', new_string='y', replace_all=True)
        self.assertTrue(result.get('success'))
        self.assertEqual(dat.text, 'y y y')
        self.assertEqual(result['replacements'], 3)

    def test_edit_dat_content_ambiguous_match_refused(self):
        """Multiple matches without replace_all must error, not guess."""
        dat = self.sandbox.create(textDAT, 'edit_ambiguous')
        dat.text = 'dup dup'
        result = self.embody.ext.Envoy._edit_dat_content(
            op_path=dat.path, old_string='dup', new_string='z')
        self.assertDictHasKey(result, 'error')
        self.assertEqual(dat.text, 'dup dup', 'ambiguous edit must not mutate')

    def test_edit_dat_content_not_found(self):
        dat = self.sandbox.create(textDAT, 'edit_missing')
        dat.text = 'hello'
        result = self.embody.ext.Envoy._edit_dat_content(
            op_path=dat.path, old_string='nope', new_string='x')
        self.assertDictHasKey(result, 'error')

    def test_edit_dat_content_empty_old_string_refused(self):
        dat = self.sandbox.create(textDAT, 'edit_empty')
        dat.text = 'content'
        result = self.embody.ext.Envoy._edit_dat_content(
            op_path=dat.path, old_string='', new_string='x')
        self.assertDictHasKey(result, 'error')

    def test_edit_dat_content_wipe_requires_confirm(self):
        """An edit that would empty the DAT needs confirm_wipe."""
        dat = self.sandbox.create(textDAT, 'edit_wipe')
        dat.text = 'all'
        refused = self.embody.ext.Envoy._edit_dat_content(
            op_path=dat.path, old_string='all', new_string='')
        self.assertDictHasKey(refused, 'error')
        self.assertEqual(dat.text, 'all')
        ok = self.embody.ext.Envoy._edit_dat_content(
            op_path=dat.path, old_string='all', new_string='',
            confirm_wipe=True)
        self.assertTrue(ok.get('success'))
        self.assertEqual(dat.text, '')

    def test_edit_dat_content_table_dat_refused(self):
        """Table DATs are not text -- edit_dat_content must redirect."""
        dat = self.sandbox.create(tableDAT, 'edit_table')
        dat.appendRow(['a', 'b'])
        result = self.embody.ext.Envoy._edit_dat_content(
            op_path=dat.path, old_string='a', new_string='x')
        self.assertDictHasKey(result, 'error')

    def test_edit_dat_content_nonexistent(self):
        result = self.embody.ext.Envoy._edit_dat_content(
            op_path='/nonexistent', old_string='a', new_string='b')
        self.assertDictHasKey(result, 'error')
