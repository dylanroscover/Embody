"""
Test suite: MCP diagnostics and introspection handlers in EnvoyExt.

Tests _get_td_info, _get_op_errors, _exec_op_method,
_get_td_classes, _get_td_class_details, _get_module_help, _get_logs.
"""

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase


class TestMCPDiagnostics(EmbodyTestCase):

    def setUp(self):
        super().setUp()
        self.envoy = self.embody.ext.Envoy

    # --- _get_td_info ---

    def test_get_td_info(self):
        result = self.envoy._get_td_info()
        self.assertDictHasKey(result, 'version')

    def test_get_td_info_has_os(self):
        result = self.envoy._get_td_info()
        self.assertDictHasKey(result, 'osName')

    # --- _get_op_errors ---

    def test_get_op_errors_clean_op(self):
        comp = self.sandbox.create(baseCOMP, 'clean_comp')
        result = self.envoy._get_op_errors(
            op_path=comp.path, recurse=False)
        self.assertDictHasKey(result, 'errorCount')
        self.assertEqual(result['errorCount'], 0)

    def test_get_op_errors_recursive(self):
        comp = self.sandbox.create(baseCOMP, 'parent_comp')
        comp.create(baseCOMP, 'child_comp')
        result = self.envoy._get_op_errors(
            op_path=comp.path, recurse=True)
        self.assertDictHasKey(result, 'errorCount')

    def test_get_op_errors_nonexistent(self):
        result = self.envoy._get_op_errors(
            op_path='/nonexistent', recurse=False)
        self.assertDictHasKey(result, 'error')

    # --- _get_op_errors: Python tracebacks (OP.scriptErrors) ---
    #
    # TD reports script errors on a DIFFERENT surface from cook errors. A
    # DAT whose onCook raises shows a red X in the network yet reads as a
    # clean op through errors()/warnings(). get_op_errors was blind to the
    # whole class until 2026-08-22, when it called a red-X'd Embody manager
    # list "0 errors" -- the same shape of hole shaderErrors once had.

    BOOM = 'boom_marker_xyz'

    def _broken_script_dat(self, name='script_boom'):
        s = self.sandbox.create(scriptDAT, name)
        cb = self.sandbox.op(name + '_callbacks')
        if cb is None:                      # TD did not auto-dock one
            cb = self.sandbox.create(textDAT, name + '_callbacks')
            s.par.callbacks = cb
        cb.text = ('def onCook(scriptOp):\n'
                   '    raise RuntimeError("%s")\n' % self.BOOM)
        s.cook(force=True)
        return s, cb

    def test_script_error_invisible_to_cook_errors(self):
        """Pins the PREMISE. If TD ever routes script errors through
        errors(), script_errors() can be reconsidered."""
        s, cb = self._broken_script_dat('script_premise')
        self.assertTrue(cb.scriptErrors(recurse=False),
            'callback did not raise -- test is not exercising anything')
        self.assertEqual(cb.errors(recurse=False), '',
            'TD now reports script errors via errors() -- revisit '
            'script_errors, it may be redundant')

    def test_script_error_is_reported(self):
        """The incident: a raising callback must not read as a clean op."""
        self._broken_script_dat('script_reported')
        result = self.envoy._get_op_errors(
            op_path=self.sandbox.path, recurse=True)
        scripts = [e for e in result['errors']
                   if e.get('kind') == 'script']
        self.assertTrue(scripts, 'script error missing from errors[]')
        self.assertTrue(any(self.BOOM in e['message'] for e in scripts),
            'traceback text not carried through')
        self.assertGreater(result['errorCount'], 0)
        self.assertTrue(result['hasErrors'])

    def test_script_error_entry_shape_feeds_the_differ(self):
        """Entries must match errors[] shape or the write-effect footer
        (_new_error_entries) silently drops them."""
        s, cb = self._broken_script_dat('script_shape')
        result = self.envoy._get_op_errors(
            op_path=self.sandbox.path, recurse=True)
        entry = [e for e in result['errors']
                 if e.get('kind') == 'script'][0]
        for key in ('nodePath', 'nodeName', 'opType', 'message'):
            self.assertIn(key, entry)
        self.assertTrue(entry['nodePath'].startswith('/'),
            'differ drops any entry whose nodePath is not an op path')
        self.assertEqual(entry['nodePath'], cb.path)

    def test_clean_op_reports_no_script_errors(self):
        """No false positives -- the other half of the shader contract."""
        comp = self.sandbox.create(baseCOMP, 'script_clean')
        result = self.envoy._get_op_errors(
            op_path=comp.path, recurse=True)
        self.assertEqual(result['errorCount'], 0)
        self.assertEqual([e for e in result['errors']
                          if e.get('kind') == 'script'], [])

    def test_script_error_parser_survives_parens_in_traceback(self):
        """A traceback's OWN last line can end in "(...)" -- e.g.
        "takes 2 positional arguments (3 given)". Only a leading slash
        terminates a block, or one error splits into two bogus entries."""
        read = self.embody.op('envoy_read').module
        boom = ('  Error: Traceback (most recent call last):\n'
                '  File "/x/cb", line 2, in onCook\n'
                'TypeError: f() takes 2 positional arguments (3 given)\n'
                ' (/x/cb)\n')

        class _Stub:
            path = '/x/cb'

            def scriptErrors(self, recurse=True):
                return boom

        entries = read.script_errors(None, _Stub(), True)
        self.assertEqual(len(entries), 1,
            'traceback split on its own parenthesised line')
        self.assertEqual(entries[0]['nodePath'], '/x/cb')
        self.assertIn('3 given', entries[0]['message'])

    # --- _exec_op_method ---

    def test_exec_op_method_cook(self):
        comp = self.sandbox.create(baseCOMP, 'method_test')
        result = self.envoy._exec_op_method(
            op_path=comp.path, method='cook', args=[], kwargs={'force': True})
        self.assertNotIn('error', result)

    def test_exec_op_method_nonexistent_method(self):
        comp = self.sandbox.create(baseCOMP, 'bad_method')
        result = self.envoy._exec_op_method(
            op_path=comp.path, method='nonExistentMethod123')
        self.assertDictHasKey(result, 'error')

    # --- _get_td_classes ---

    def test_get_td_classes(self):
        result = self.envoy._get_td_classes()
        self.assertDictHasKey(result, 'classes')
        self.assertGreater(len(result['classes']), 0)

    # --- _get_td_class_details ---

    def test_get_td_class_details_op(self):
        result = self.envoy._get_td_class_details(class_name='OP')
        self.assertDictHasKey(result, 'methods')

    def test_get_td_class_details_nonexistent(self):
        result = self.envoy._get_td_class_details(
            class_name='NonExistentClass12345')
        self.assertDictHasKey(result, 'error')

    # --- _get_module_help ---

    def test_get_module_help_td_attr(self):
        # Use 'OP' to test the hasattr(td, name) path - fast unlike 'td' (7s)
        result = self.envoy._get_module_help(module_name='OP')
        self.assertDictHasKey(result, 'helpText')
        self.assertIn('OP', result['helpText'])

    def test_get_module_help_tdu(self):
        result = self.envoy._get_module_help(module_name='td.tdu')
        self.assertDictHasKey(result, 'helpText')

    # --- _get_logs ---

    def test_get_logs_returns_entries(self):
        op.Embody.Log('test_get_logs marker', 'INFO')
        result = self.envoy._get_logs()
        self.assertDictHasKey(result, 'entries')
        self.assertIsInstance(result['entries'], list)
        self.assertDictHasKey(result, 'latest_id')
        self.assertDictHasKey(result, 'total_in_buffer')

    def test_get_logs_count_capped(self):
        result = self.envoy._get_logs(count=5)
        self.assertLessEqual(len(result['entries']), 5)

    def test_get_logs_level_filter(self):
        op.Embody.Log('an error for the filter test', 'ERROR')
        result = self.envoy._get_logs(level='ERROR')
        for e in result['entries']:
            self.assertEqual(e['level'], 'ERROR')

    def test_get_logs_since_id_returns_only_newer(self):
        op.Embody.Log('before the cursor', 'INFO')
        cursor = self.envoy._get_logs()['latest_id']
        op.Embody.Log('after the cursor', 'INFO')
        result = self.envoy._get_logs(since_id=cursor)
        for e in result['entries']:
            self.assertGreater(e['id'], cursor)
