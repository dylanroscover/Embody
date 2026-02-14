"""
Test suite: MCP DAT content handlers in ClaudiusExt.

Tests _get_dat_content and _set_dat_content.
"""

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase


class TestMCPDatContent(EmbodyTestCase):

    def setUp(self):
        super().setUp()
        self.claudius = self.embody.ext.Claudius

    # --- _get_dat_content ---

    def test_get_dat_content_text(self):
        dat = self.sandbox.create(textDAT, 'text_dat')
        dat.text = 'hello world'
        result = self.claudius._get_dat_content(op_path=dat.path, format='text')
        self.assertDictHasKey(result, 'text')
        self.assertEqual(result['text'], 'hello world')

    def test_get_dat_content_table(self):
        dat = self.sandbox.create(tableDAT, 'table_dat')
        dat.appendRow(['a', 'b', 'c'])
        dat.appendRow(['1', '2', '3'])
        result = self.claudius._get_dat_content(op_path=dat.path, format='table')
        self.assertDictHasKey(result, 'rows')

    def test_get_dat_content_auto_text(self):
        dat = self.sandbox.create(textDAT, 'auto_text')
        dat.text = 'auto detected'
        result = self.claudius._get_dat_content(op_path=dat.path, format='auto')
        self.assertDictHasKey(result, 'text')

    def test_get_dat_content_auto_table(self):
        dat = self.sandbox.create(tableDAT, 'auto_table')
        dat.appendRow(['x', 'y'])
        result = self.claudius._get_dat_content(op_path=dat.path, format='auto')
        self.assertDictHasKey(result, 'rows')

    def test_get_dat_content_nonexistent(self):
        result = self.claudius._get_dat_content(
            op_path='/nonexistent', format='auto')
        self.assertDictHasKey(result, 'error')

    # --- _set_dat_content ---

    def test_set_dat_content_text(self):
        dat = self.sandbox.create(textDAT, 'set_text')
        result = self.claudius._set_dat_content(
            op_path=dat.path, text='new content')
        self.assertTrue(result.get('success'))
        self.assertEqual(dat.text, 'new content')

    def test_set_dat_content_rows(self):
        dat = self.sandbox.create(tableDAT, 'set_rows')
        result = self.claudius._set_dat_content(
            op_path=dat.path, rows=[['a', 'b'], ['1', '2']])
        self.assertTrue(result.get('success'))

    def test_set_dat_content_clear(self):
        dat = self.sandbox.create(tableDAT, 'clear_dat')
        dat.appendRow(['existing', 'data'])
        result = self.claudius._set_dat_content(
            op_path=dat.path, clear=True)
        self.assertTrue(result.get('success'))

    def test_set_dat_content_nonexistent(self):
        result = self.claudius._set_dat_content(
            op_path='/nonexistent', text='test')
        self.assertDictHasKey(result, 'error')
