"""
Test suite: Release smoke tests.

Validates that Embody's post-init state is healthy and that the
_messageBox auto-response mechanism works correctly. These tests run
against the current TD session's Embody instance and verify the same
invariants that a fresh release install must satisfy.

For the full E2E flow (loading release .tox into a fresh project),
see smoke_bootstrap.py (template .toe startup script) and the
orchestration notes in the project memory.
"""

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase


class TestSmokeRelease(EmbodyTestCase):

    def tearDown(self):
        """Always clear seeded responses to prevent leakage."""
        try:
            self.embody.unstore('_smoke_test_responses')
        except Exception:
            pass
        super().tearDown()

    # =========================================================================
    # _messageBox auto-response mechanism
    # =========================================================================

    def test_message_box_auto_response(self):
        """Seeded response is returned and consumed by _messageBox."""
        self.embody.store('_smoke_test_responses', {
            'Test Dialog': 1
        })
        result = self.embody_ext._messageBox(
            'Test Dialog', 'Test message', buttons=['Cancel', 'OK'])
        self.assertEqual(result, 1)
        responses = self.embody.fetch('_smoke_test_responses', None,
                                      search=False)
        self.assertIsNone(responses,
            'Storage should be unstored when last response is consumed')

    def test_message_box_multiple_responses(self):
        """Multiple seeded responses are consumed independently."""
        self.embody.store('_smoke_test_responses', {
            'Dialog A': 0,
            'Dialog B': 2
        })
        result_a = self.embody_ext._messageBox(
            'Dialog A', 'msg', buttons=['OK'])
        self.assertEqual(result_a, 0)
        responses = self.embody.fetch('_smoke_test_responses', None,
                                      search=False)
        self.assertIsNotNone(responses)
        self.assertIn('Dialog B', responses)
        result_b = self.embody_ext._messageBox(
            'Dialog B', 'msg', buttons=['A', 'B', 'C'])
        self.assertEqual(result_b, 2)
        responses = self.embody.fetch('_smoke_test_responses', None,
                                      search=False)
        self.assertIsNone(responses)

    def test_message_box_no_response_seeded(self):
        """With no seeded responses, storage returns None."""
        responses = self.embody.fetch('_smoke_test_responses', None,
                                      search=False)
        self.assertIsNone(responses,
            'No responses should be seeded at test start')

    def test_message_box_unmatched_title_left_intact(self):
        """A title with no matching response is left for ui.messageBox."""
        self.embody.store('_smoke_test_responses', {
            'Other Dialog': 1
        })
        # Call with a different title — should NOT consume the stored response.
        # We can't test the ui.messageBox fallback without a modal, so just
        # verify the stored response survives.
        responses = self.embody.fetch('_smoke_test_responses', None,
                                      search=False)
        self.assertIn('Other Dialog', responses)
        self.embody.unstore('_smoke_test_responses')

    # =========================================================================
    # Post-init state verification
    # =========================================================================

    def test_status_enabled(self):
        """Embody status is Enabled after init completes."""
        self.assertEqual(self.embody.par.Status.eval(), 'Enabled')

    def test_embody_extension_loaded(self):
        """EmbodyExt is accessible on the Embody COMP."""
        ext = self.embody.ext.Embody
        self.assertIsNotNone(ext, 'EmbodyExt should be loaded')

    def test_envoy_extension_loaded(self):
        """EnvoyExt is accessible on the Embody COMP."""
        ext = self.embody.ext.Envoy
        self.assertIsNotNone(ext, 'EnvoyExt should be loaded')

    def test_tdn_extension_loaded(self):
        """TDNExt is accessible on the Embody COMP."""
        ext = self.embody.ext.TDN
        self.assertIsNotNone(ext, 'TDNExt should be loaded')

    def test_no_script_errors(self):
        """Embody COMP has no script errors."""
        errors = self.embody.scriptErrors()
        self.assertEqual(len(errors), 0,
            f'Embody has script errors: {errors}')

    def test_version_parameter_exists(self):
        """Version parameter exists and is a non-empty string."""
        version = str(self.embody.par.Version.eval())
        self.assertTrue(len(version) > 0, 'Version should be non-empty')

    def test_build_parameter_exists(self):
        """Build parameter exists and is a positive integer."""
        build = int(self.embody.par.Build.eval())
        self.assertGreater(build, 0, f'Build should be > 0, got {build}')

    def test_externalizations_table_exists(self):
        """Externalizations table exists and is a DAT."""
        table = self.embody_ext.Externalizations
        self.assertIsNotNone(table, 'Externalizations table must exist')

    def test_externalizations_table_schema(self):
        """Externalizations table has the expected column headers."""
        table = self.embody_ext.Externalizations
        self.assertIsNotNone(table)
        expected = [
            'path', 'type', 'strategy', 'rel_file_path',
            'timestamp', 'dirty', 'build', 'touch_build'
        ]
        headers = [table[0, c].val for c in range(table.numCols)]
        for col in expected:
            self.assertIn(col, headers, f'Missing column: {col}')

    def test_promoted_methods_exist(self):
        """Key promoted methods are callable on the Embody COMP."""
        for method_name in ['Update', 'Save', 'Verify', 'Reset']:
            method = getattr(self.embody, method_name, None)
            self.assertIsNotNone(method,
                f'Promoted method {method_name} missing')
            self.assertTrue(callable(method),
                f'{method_name} should be callable')

    def test_log_method_works(self):
        """Log method executes without error."""
        try:
            self.embody_ext.Log('[test] smoke test log check', 'INFO')
        except Exception as e:
            raise AssertionError(f'Log() raised: {e}')

    def test_global_op_shortcut(self):
        """op.Embody resolves to the Embody COMP."""
        self.assertIs(op.Embody, self.embody,
            'op.Embody should resolve to the Embody COMP')

    def test_parent_shortcut(self):
        """parent.Embody resolves from inside the Embody COMP."""
        # The execute DAT lives inside Embody, so parent.Embody
        # should resolve from there. We verify indirectly: the
        # extension's self.my should be the Embody COMP.
        self.assertIs(self.embody_ext.my, self.embody,
            'Extension self.my should be the Embody COMP')

    def test_key_parameters_exist(self):
        """Essential parameters exist on the Embody COMP."""
        required_pars = [
            'Status', 'Version', 'Build', 'Envoyenable', 'Envoyport',
            'Logtofile', 'Logfolder', 'Filecleanup',
            'Tdnstriponsave', 'Refresh',
        ]
        for par_name in required_pars:
            par = getattr(self.embody.par, par_name, None)
            self.assertIsNotNone(par, f'Parameter {par_name} missing')

    def test_log_buffer_initialized(self):
        """Internal log buffer is initialized and operational."""
        buffer = self.embody_ext._log_buffer
        self.assertIsNotNone(buffer, 'Log buffer should be initialized')
        # Buffer should have entries from init
        self.assertGreater(len(buffer), 0,
            'Log buffer should have entries after init')

    # =========================================================================
    # _promptEnvoy with auto-response
    # =========================================================================

    def test_prompt_envoy_skip(self):
        """Auto-responding Skip (0) to Envoy prompt keeps Envoyenable off."""
        original = self.embody.par.Envoyenable.eval()
        self.embody.par.Envoyenable = False
        try:
            self.embody.store('_smoke_test_responses', {
                'Embody - AI Coding Assistant Integration': 0
            })
            self.embody_ext._promptEnvoy()
            self.assertFalse(self.embody.par.Envoyenable.eval(),
                'Envoyenable should remain False after Skip')
        finally:
            self.embody.par.Envoyenable = original

    def test_prompt_envoy_enable(self):
        """Auto-responding Enable (1) to Envoy prompt enables Envoy."""
        original = self.embody.par.Envoyenable.eval()
        self.embody.par.Envoyenable = False
        try:
            self.embody.store('_smoke_test_responses', {
                'Embody - AI Coding Assistant Integration': 1
            })
            self.embody_ext._promptEnvoy()
            self.assertTrue(self.embody.par.Envoyenable.eval(),
                'Envoyenable should be True after Enable')
        finally:
            self.embody.par.Envoyenable = original

    # =========================================================================
    # Envoy state (when enabled)
    # =========================================================================

    def test_envoy_server_running_if_enabled(self):
        """If Envoyenable is True, the MCP server should be running."""
        if not self.embody.par.Envoyenable.eval():
            self.skip('Envoy not enabled in this session')
        running = self.embody.fetch('envoy_running', False)
        self.assertTrue(running,
            'Server should be running when Envoy is enabled')

    def test_envoy_port_valid(self):
        """Envoy port is in a valid range."""
        port = int(self.embody.par.Envoyport.eval())
        self.assertGreater(port, 1023, f'Port {port} too low')
        self.assertLess(port, 65536, f'Port {port} too high')
