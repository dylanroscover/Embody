"""
Test suite: GLSL .glsl externalization (v6.0.34).

Regression guard for the bug where GLSL shader DATs (type 'text',
language 'glsl') were externalized as '.py' instead of '.glsl'. The fix
routes tag inference through EmbodyExt._inferDATTagValue, which reads a
text DAT's language/extension rather than a bare type->tag map.

Covers:
  - The end-to-end regression guard: a glsl-language textDAT externalized
    via EnvoyExt._externalize_op lands on a '.glsl' file path, never '.py'.
  - EmbodyExt._inferDATTagValue forward inference:
      * text DAT language='glsl'                -> Glsltag value
      * text DAT language unmapped + ext='frag' -> Glsltag value (ext fallback)
      * non-text tableDAT                        -> Tsvtag value (fast path)
  - EmbodyExt._setDATLanguageForTag reverse mapping:
      * 'glsl' tag value -> par.language == 'glsl'
      * 'frag' tag value -> par.extension == 'frag'
      * 'vert' tag value -> par.extension == 'vert'
"""

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase


class TestGLSLExternalize(EmbodyTestCase):

    def setUp(self):
        super().setUp()
        self.envoy = self.embody.ext.Envoy

    def tearDown(self):
        """Drop externalizations-table rows for any sandbox op we tagged.

        _externalize_op tags the DAT and runs Update(), which can append a
        table row. Sandbox children are auto-destroyed by the base tearDown,
        but the table rows must be removed here (mirrors
        test_mcp_externalization.py's tearDown).
        """
        table = self.embody_ext.Externalizations
        for i in range(table.numRows - 1, 0, -1):
            path = table[i, 'path'].val
            if path.startswith(self.sandbox.path):
                table.deleteRow(i)
        super().tearDown()

    # =================================================================
    # THE REGRESSION GUARD: glsl-language DAT externalizes as .glsl
    # =================================================================

    def test_glsl_dat_externalizes_to_glsl_not_py(self):
        """A textDAT with language='glsl' must externalize to a '.glsl'
        file, never '.py'. This is the v6.0.34 regression guard."""
        dat = self.sandbox.create(textDAT, 'shader_glsl')
        dat.par.language = 'glsl'

        result = self.envoy._externalize_op(op_path=dat.path)

        self.assertTrue(
            result.get('success'),
            f'externalize failed: {result.get("error")}')

        # _externalize_op returns 'file' = target.par.file.eval() for DATs.
        file_path = result.get('file', '')
        self.assertTrue(
            file_path,
            'externalized DAT should have a non-empty file path')
        self.assertEndsWith(
            file_path, '.glsl',
            f'GLSL DAT externalized to wrong extension: {file_path}')
        self.assertFalse(
            file_path.endswith('.py'),
            f'GLSL DAT must NOT externalize as .py (got {file_path})')

    def test_glsl_dat_file_par_ends_with_glsl(self):
        """Cross-check the regression guard directly against the DAT's
        live 'file' parameter (the accessor getExternalPath reads)."""
        dat = self.sandbox.create(textDAT, 'shader_glsl2')
        dat.par.language = 'glsl'

        result = self.envoy._externalize_op(op_path=dat.path)
        self.assertTrue(
            result.get('success'),
            f'externalize failed: {result.get("error")}')

        live_file = dat.par.file.eval()
        self.assertEndsWith(
            live_file, '.glsl',
            f'live file par should end in .glsl, got {live_file}')
        self.assertFalse(live_file.endswith('.py'))

    def test_glsl_externalize_tag_is_glsltag(self):
        """The returned tag for a glsl-language DAT equals the Glsltag
        parameter value (the same contract test_mcp_externalization asserts)."""
        dat = self.sandbox.create(textDAT, 'shader_tagcheck')
        dat.par.language = 'glsl'

        result = self.envoy._externalize_op(op_path=dat.path)
        self.assertTrue(
            result.get('success'),
            f'externalize failed: {result.get("error")}')
        self.assertEqual(result['tag'], self.embody.par.Glsltag.eval())

    # =================================================================
    # _inferDATTagValue forward inference
    # =================================================================

    def test_infer_text_glsl_language_returns_glsltag(self):
        """text DAT with language='glsl' -> Glsltag value (language map)."""
        dat = self.sandbox.create(textDAT, 'infer_glsl')
        dat.par.language = 'glsl'

        value = self.embody_ext._inferDATTagValue(dat)
        self.assertEqual(value, self.embody.par.Glsltag.eval())

    def test_infer_text_frag_extension_returns_glsltag(self):
        """text DAT with an unmapped language but extension='frag' falls
        through to the extension_to_tag mapping -> Glsltag value.

        'text' (Plain Text) is not in extension_to_tag, so the language
        lookup misses and the extension lookup ('frag' -> 'Glsltag') wins.
        """
        dat = self.sandbox.create(textDAT, 'infer_frag')
        # Plain text language is unmapped in extension_to_tag.
        dat.par.language = 'text'
        dat.par.extension = 'frag'

        value = self.embody_ext._inferDATTagValue(dat)
        self.assertEqual(value, self.embody.par.Glsltag.eval())

    def test_infer_table_dat_returns_tsvtag(self):
        """non-text tableDAT takes the dat_type_to_tag fast path -> Tsvtag."""
        dat = self.sandbox.create(tableDAT, 'infer_tsv')

        value = self.embody_ext._inferDATTagValue(dat)
        self.assertEqual(value, self.embody.par.Tsvtag.eval())

    def test_infer_text_default_language_returns_pytag(self):
        """A default-language textDAT (no glsl/extension hint) infers Pytag,
        proving the glsl branch is content-specific, not a blanket change."""
        dat = self.sandbox.create(textDAT, 'infer_default')

        value = self.embody_ext._inferDATTagValue(dat)
        self.assertEqual(value, self.embody.par.Pytag.eval())

    # =================================================================
    # _setDATLanguageForTag reverse mapping
    # =================================================================

    def test_set_language_glsl_tag(self):
        """Applying the 'glsl' tag value sets the DAT language to 'glsl'."""
        dat = self.sandbox.create(textDAT, 'setlang_glsl')

        self.embody_ext._setDATLanguageForTag(dat, 'glsl')
        self.assertEqual(dat.par.language.eval(), 'glsl')

    def test_set_extension_frag_tag(self):
        """Applying the 'frag' tag value sets the DAT extension to 'frag'
        (tag_to_extension), and the language to 'glsl' (tag_to_language)."""
        dat = self.sandbox.create(textDAT, 'setlang_frag')

        self.embody_ext._setDATLanguageForTag(dat, 'frag')
        self.assertEqual(dat.par.extension.eval(), 'frag')
        self.assertEqual(dat.par.language.eval(), 'glsl')

    def test_set_extension_vert_tag(self):
        """Applying the 'vert' tag value sets the DAT extension to 'vert'
        and the language to 'glsl'."""
        dat = self.sandbox.create(textDAT, 'setlang_vert')

        self.embody_ext._setDATLanguageForTag(dat, 'vert')
        self.assertEqual(dat.par.extension.eval(), 'vert')
        self.assertEqual(dat.par.language.eval(), 'glsl')

    def test_set_language_noop_on_non_text_dat(self):
        """_setDATLanguageForTag is a no-op on non-text DATs (guarded by
        type != 'text'); a tableDAT must not gain a language/extension change."""
        dat = self.sandbox.create(tableDAT, 'setlang_table')

        # Should not raise and should not alter the table DAT (it has no
        # 'glsl' language). The guard returns early for type != 'text'.
        self.embody_ext._setDATLanguageForTag(dat, 'glsl')
        # tableDAT has no 'language' parameter to assert against; the test
        # passing without exception confirms the early-return guard.
        self.assertEqual(dat.type, 'table')
