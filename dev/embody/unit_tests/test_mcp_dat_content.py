"""
Test suite: MCP DAT content handlers in EnvoyExt.

Tests _get_dat_content and _set_dat_content.
"""

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase


class TestMCPDatContent(EmbodyTestCase):

    def setUp(self):
        super().setUp()
        self.envoy = self.embody.ext.Envoy

    # --- _get_dat_content ---

    def test_get_dat_content_text(self):
        dat = self.sandbox.create(textDAT, 'text_dat')
        dat.text = 'hello world'
        result = self.envoy._get_dat_content(op_path=dat.path, format='text')
        self.assertDictHasKey(result, 'text')
        self.assertEqual(result['text'], 'hello world')

    def test_get_dat_content_table(self):
        dat = self.sandbox.create(tableDAT, 'table_dat')
        dat.appendRow(['a', 'b', 'c'])
        dat.appendRow(['1', '2', '3'])
        result = self.envoy._get_dat_content(op_path=dat.path, format='table')
        self.assertDictHasKey(result, 'rows')

    def test_get_dat_content_auto_text(self):
        dat = self.sandbox.create(textDAT, 'auto_text')
        dat.text = 'auto detected'
        result = self.envoy._get_dat_content(op_path=dat.path, format='auto')
        self.assertDictHasKey(result, 'text')

    def test_get_dat_content_auto_table(self):
        dat = self.sandbox.create(tableDAT, 'auto_table')
        dat.appendRow(['x', 'y'])
        result = self.envoy._get_dat_content(op_path=dat.path, format='auto')
        self.assertDictHasKey(result, 'rows')

    def test_get_dat_content_nonexistent(self):
        result = self.envoy._get_dat_content(
            op_path='/nonexistent', format='auto')
        self.assertDictHasKey(result, 'error')

    # --- _set_dat_content ---

    def test_set_dat_content_text(self):
        dat = self.sandbox.create(textDAT, 'set_text')
        result = self.envoy._set_dat_content(
            op_path=dat.path, text='new content')
        self.assertTrue(result.get('success'))
        self.assertEqual(dat.text, 'new content')

    def test_set_dat_content_rows(self):
        dat = self.sandbox.create(tableDAT, 'set_rows')
        result = self.envoy._set_dat_content(
            op_path=dat.path, rows=[['a', 'b'], ['1', '2']])
        self.assertTrue(result.get('success'))

    def test_set_dat_content_clear(self):
        dat = self.sandbox.create(tableDAT, 'clear_dat')
        dat.appendRow(['existing', 'data'])
        result = self.envoy._set_dat_content(
            op_path=dat.path, clear=True, confirm_wipe=True)
        self.assertTrue(result.get('success'))

    def test_set_dat_content_nonexistent(self):
        result = self.envoy._set_dat_content(
            op_path='/nonexistent', text='test')
        self.assertDictHasKey(result, 'error')

    # --- _edit_dat_content ---

    def test_edit_dat_content_basic(self):
        dat = self.sandbox.create(textDAT, 'edit_basic')
        dat.text = 'hello world\nfoo bar\nbaz qux'
        result = self.envoy._edit_dat_content(
            op_path=dat.path, old_string='foo bar',
            new_string='FOO BAR')
        self.assertTrue(result.get('success'))
        self.assertEqual(result.get('replacements'), 1)
        self.assertEqual(dat.text, 'hello world\nFOO BAR\nbaz qux')

    def test_edit_dat_content_requires_unique_match(self):
        dat = self.sandbox.create(textDAT, 'edit_dup')
        dat.text = 'x = 1\ny = 1\nz = 1'
        result = self.envoy._edit_dat_content(
            op_path=dat.path, old_string='= 1', new_string='= 2')
        self.assertDictHasKey(result, 'error')
        self.assertIn('3 times', result['error'])
        self.assertEqual(dat.text, 'x = 1\ny = 1\nz = 1')

    def test_edit_dat_content_replace_all(self):
        dat = self.sandbox.create(textDAT, 'edit_all')
        dat.text = 'x = 1\ny = 1\nz = 1'
        result = self.envoy._edit_dat_content(
            op_path=dat.path, old_string='= 1', new_string='= 2',
            replace_all=True)
        self.assertTrue(result.get('success'))
        self.assertEqual(result.get('replacements'), 3)
        self.assertEqual(dat.text, 'x = 2\ny = 2\nz = 2')

    def test_edit_dat_content_not_found(self):
        dat = self.sandbox.create(textDAT, 'edit_missing')
        dat.text = 'hello world'
        result = self.envoy._edit_dat_content(
            op_path=dat.path, old_string='nope', new_string='yep')
        self.assertDictHasKey(result, 'error')
        self.assertIn('not found', result['error'])

    def test_edit_dat_content_case_insensitive_hint(self):
        dat = self.sandbox.create(textDAT, 'edit_case')
        dat.text = 'Hello World'
        result = self.envoy._edit_dat_content(
            op_path=dat.path, old_string='hello', new_string='HI')
        self.assertDictHasKey(result, 'error')
        self.assertIn('case-insensitive', result['error'])

    def test_edit_dat_content_empty_old_string(self):
        dat = self.sandbox.create(textDAT, 'edit_empty_old')
        dat.text = 'content'
        result = self.envoy._edit_dat_content(
            op_path=dat.path, old_string='', new_string='x')
        self.assertDictHasKey(result, 'error')
        self.assertIn('empty', result['error'])

    def test_edit_dat_content_identical_strings(self):
        dat = self.sandbox.create(textDAT, 'edit_identical')
        dat.text = 'hello world'
        result = self.envoy._edit_dat_content(
            op_path=dat.path, old_string='hello', new_string='hello')
        self.assertDictHasKey(result, 'error')
        self.assertIn('identical', result['error'])

    def test_edit_dat_content_rejects_table_dat(self):
        dat = self.sandbox.create(tableDAT, 'edit_table')
        dat.appendRow(['a', 'b'])
        result = self.envoy._edit_dat_content(
            op_path=dat.path, old_string='a', new_string='A')
        self.assertDictHasKey(result, 'error')
        self.assertIn('text-only', result['error'])

    def test_edit_dat_content_nonexistent(self):
        result = self.envoy._edit_dat_content(
            op_path='/nonexistent', old_string='a', new_string='b')
        self.assertDictHasKey(result, 'error')

    def test_edit_dat_content_wipe_guard(self):
        dat = self.sandbox.create(textDAT, 'edit_wipe')
        dat.text = 'all of it'
        result = self.envoy._edit_dat_content(
            op_path=dat.path, old_string='all of it',
            new_string='')
        self.assertDictHasKey(result, 'error')
        self.assertIn('wipe', result['error'].lower())
        self.assertEqual(dat.text, 'all of it')

    def test_edit_dat_content_wipe_confirmed(self):
        dat = self.sandbox.create(textDAT, 'edit_wipe_ok')
        dat.text = 'all of it'
        result = self.envoy._edit_dat_content(
            op_path=dat.path, old_string='all of it',
            new_string='', confirm_wipe=True)
        self.assertTrue(result.get('success'))
        self.assertEqual(dat.text, '')
