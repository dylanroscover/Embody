"""
Test suite: MCP externalization integration handlers in EnvoyExt.

Tests _externalize_op, _remove_externalization_tag,
_get_externalizations, _get_externalization_status.
"""

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase


class TestMCPExternalization(EmbodyTestCase):

    def setUp(self):
        super().setUp()
        self.envoy = self.embody.ext.Envoy

    def tearDown(self):
        """Clean up externalizations table rows for sandbox ops."""
        for i in range(self.embody_ext.Externalizations.numRows - 1, 0, -1):
            path = self.embody_ext.Externalizations[i, 'path'].val
            if path.startswith(self.sandbox.path):
                self.embody_ext.Externalizations.deleteRow(i)
        super().tearDown()

    # --- _get_externalizations ---

    def test_get_externalizations_returns_list(self):
        result = self.envoy._get_externalizations()
        self.assertDictHasKey(result, 'externalizations')
        self.assertIsInstance(result['externalizations'], list)

    def test_get_externalizations_has_entries(self):
        result = self.envoy._get_externalizations()
        self.assertGreater(len(result['externalizations']), 0)

    def test_get_externalizations_entry_structure(self):
        result = self.envoy._get_externalizations()
        if result['externalizations']:
            entry = result['externalizations'][0]
            self.assertDictHasKey(entry, 'path')
            self.assertDictHasKey(entry, 'type')

    # --- _get_externalization_status ---

    def test_get_externalization_status_existing(self):
        # Use Embody itself as a known externalized op
        result = self.envoy._get_externalization_status(
            op_path=self.embody.path)
        # Should return some status info
        self.assertNotIn('error', result)

    def test_get_externalization_status_nonexistent(self):
        result = self.envoy._get_externalization_status(
            op_path='/nonexistent')
        self.assertDictHasKey(result, 'error')

    # --- _externalize_op ---

    def test_externalize_op_comp(self):
        comp = self.sandbox.create(baseCOMP, 'tag_ext_comp')
        result = self.envoy._externalize_op(op_path=comp.path)
        self.assertTrue(result.get('success'))

    def test_externalize_op_nonexistent(self):
        result = self.envoy._externalize_op(
            op_path='/nonexistent')
        self.assertDictHasKey(result, 'error')

    # --- _remove_externalization_tag ---

    def test_remove_externalization_tag(self):
        comp = self.sandbox.create(baseCOMP, 'untag_comp')
        # Tag it first
        self.envoy._externalize_op(op_path=comp.path)
        # Now remove
        result = self.envoy._remove_externalization_tag(op_path=comp.path)
        self.assertTrue(result.get('success'))

    def test_remove_externalization_tag_nonexistent(self):
        result = self.envoy._remove_externalization_tag(
            op_path='/nonexistent')
        self.assertDictHasKey(result, 'error')

    # --- DAT auto-detection ---

    def test_tag_textdat_defaults_to_py(self):
        """textDAT with default language should auto-tag as py."""
        dat = self.sandbox.create(textDAT, 'auto_py')
        result = self.envoy._externalize_op(op_path=dat.path)
        self.assertTrue(result.get('success'))
        self.assertEqual(result['tag'], self.embody.par.Pytag.eval())

    def test_tag_textdat_python_language(self):
        """textDAT with language=python should tag as py."""
        dat = self.sandbox.create(textDAT, 'lang_py')
        dat.par.language = 'python'
        result = self.envoy._externalize_op(op_path=dat.path)
        self.assertTrue(result.get('success'))
        self.assertEqual(result['tag'], self.embody.par.Pytag.eval())

    def test_tag_textdat_glsl_language(self):
        """textDAT with language=glsl should tag as glsl."""
        dat = self.sandbox.create(textDAT, 'lang_glsl')
        dat.par.language = 'glsl'
        result = self.envoy._externalize_op(op_path=dat.path)
        self.assertTrue(result.get('success'))
        self.assertEqual(result['tag'], self.embody.par.Glsltag.eval())

    def test_tag_textdat_json_language(self):
        """textDAT with language=json should tag as json."""
        dat = self.sandbox.create(textDAT, 'lang_json')
        dat.par.language = 'json'
        result = self.envoy._externalize_op(op_path=dat.path)
        self.assertTrue(result.get('success'))
        self.assertEqual(result['tag'], self.embody.par.Jsontag.eval())

    def test_tag_textdat_xml_language(self):
        """textDAT with language=xml should tag as xml."""
        dat = self.sandbox.create(textDAT, 'lang_xml')
        dat.par.language = 'xml'
        result = self.envoy._externalize_op(op_path=dat.path)
        self.assertTrue(result.get('success'))
        self.assertEqual(result['tag'], self.embody.par.Xmltag.eval())

    def test_tag_textdat_plaintext_language_defaults_to_py(self):
        """textDAT with language='text' (Plain Text) still defaults to py."""
        dat = self.sandbox.create(textDAT, 'lang_txt')
        dat.par.language = 'text'
        result = self.envoy._externalize_op(op_path=dat.path)
        self.assertTrue(result.get('success'))
        self.assertEqual(result['tag'], self.embody.par.Pytag.eval())

    def test_tag_tabledat_auto(self):
        """tableDAT should auto-tag as tsv."""
        dat = self.sandbox.create(tableDAT, 'auto_tsv')
        result = self.envoy._externalize_op(op_path=dat.path)
        self.assertTrue(result.get('success'))
        self.assertEqual(result['tag'], self.embody.par.Tsvtag.eval())

    def test_tag_executedat_auto(self):
        """executeDAT should auto-tag as py."""
        dat = self.sandbox.create(executeDAT, 'auto_exec')
        result = self.envoy._externalize_op(op_path=dat.path)
        self.assertTrue(result.get('success'))
        self.assertEqual(result['tag'], self.embody.par.Pytag.eval())

    def test_tag_explicit_type_overrides_language(self):
        """Explicit tag_type should override auto-detection."""
        dat = self.sandbox.create(textDAT, 'explicit_txt')
        dat.par.language = 'python'
        result = self.envoy._externalize_op(
            op_path=dat.path, tag_type='txt')
        self.assertTrue(result.get('success'))
        self.assertEqual(result['tag'], 'txt')
