"""
Test suite: Envoy server lifecycle and configuration.

Tests status parameters, port configuration, running flag,
.mcp.json management, operation routing, and log piggybacking.
Does NOT start/stop the actual server (avoids port conflicts).
"""

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase


class TestServerLifecycle(EmbodyTestCase):

    def setUp(self):
        super().setUp()
        self.envoy = self.embody.ext.Envoy

    # --- Status parameters ---

    def test_status_parameter_exists(self):
        par = self.embody.par.Envoystatus
        self.assertIsNotNone(par)

    def test_enable_parameter_exists(self):
        par = self.embody.par.Envoyenable
        self.assertIsNotNone(par)

    def test_port_parameter_exists(self):
        par = self.embody.par.Envoyport
        self.assertIsNotNone(par)

    def test_port_is_integer(self):
        port = self.embody.par.Envoyport.eval()
        self.assertIsInstance(int(port), int)

    # --- Running flag ---

    def test_running_flag_is_stored(self):
        # The running flag should be stored in COMP storage
        val = self.embody.fetch('envoy_running', None)
        self.assertIsNotNone(val)

    def test_running_flag_is_bool(self):
        val = self.embody.fetch('envoy_running', False)
        self.assertIsInstance(val, bool)

    # --- Operation routing ---

    def test_execute_operation_unknown(self):
        result = self.envoy._execute_operation(
            'totally_unknown_operation_xyz', {})
        self.assertDictHasKey(result, 'error')
        self.assertIn('Unknown operation', result['error'])

    def test_execute_operation_valid(self):
        # get_td_info should always work
        result = self.envoy._execute_operation('get_td_info', {})
        self.assertNotIn('error', result)
        self.assertDictHasKey(result, 'version')

    def test_execute_operation_with_params(self):
        comp = self.sandbox.create(baseCOMP, 'routing_test')
        result = self.envoy._execute_operation(
            'get_op', {'op_path': comp.path})
        self.assertNotIn('error', result)

    def test_execute_operation_handler_error(self):
        # Passing invalid params should return error, not crash
        result = self.envoy._execute_operation(
            'get_op', {'op_path': '/nonexistent_lifecycle_test'})
        self.assertDictHasKey(result, 'error')

    # --- Log piggybacking ---
    # Cursors are PER SESSION since multi-session Phase 2: _log_cursors maps
    # sid -> last served log id (the single shared _last_served_log_id is
    # retired -- one session polling must not consume another's warnings).

    def test_log_cursors_exists(self):
        self.assertTrue(hasattr(self.envoy, '_log_cursors'))
        self.assertIsInstance(self.envoy._log_cursors, dict)

    def test_log_cursor_values_are_ints(self):
        self.envoy._baselineLogCursor('lifecycle-cursor-probe')
        try:
            self.assertIsInstance(
                self.envoy._log_cursors['lifecycle-cursor-probe'], int)
        finally:
            self.envoy._log_cursors.pop('lifecycle-cursor-probe', None)

    # --- Request/response queues ---

    def test_request_queue_exists(self):
        self.assertIsNotNone(self.envoy.request_queue)

    def test_response_queue_exists(self):
        self.assertIsNotNone(self.envoy.response_queue)

    # --- Shutdown event ---

    def test_shutdown_event_on_sys(self):
        import sys
        registry = getattr(sys, '_envoy_shutdown_events', {})
        self.assertIsInstance(registry, dict)

    def test_shutdown_event_registered(self):
        import sys
        registry = getattr(sys, '_envoy_shutdown_events', {})
        self.assertIn(self.embody.path, registry)

    # --- ENVOY_VERSION ---

    def test_envoy_version_constant(self):
        import sys
        # The module should have ENVOY_VERSION defined
        mod_path = self.embody.op('EnvoyExt')
        if mod_path is not None:
            module = mod_path.module
            self.assertTrue(hasattr(module, 'ENVOY_VERSION'))


class TestAsyncBootstrap(EmbodyTestCase):
    """Contract for the background dependency bootstrap.

    The venv build + pip install that a fresh install / version upgrade
    triggers now runs off the main thread, so TD no longer freezes during an
    upgrade drag-in. Start() routes on EmbodyExt._environmentNeedsInstall:
    ready -> synchronous _continueStart; install-needed -> _beginAsyncBootstrap
    (worker thread) -> run()-scheduled _pollBootstrap -> _continueStart.

    The deep async path (live worker + frame-scheduled poll + live Envoyenable
    coupling) is validated by the live fast-path start and manual upgrade
    testing rather than unit-mocked here -- the run()-scheduled poll fires
    after the test method returns, which makes it unsafe to drive in-process.
    These tests lock down the wiring and the clean initial state.
    """

    def setUp(self):
        super().setUp()
        self.envoy = self.embody.ext.Envoy

    def test_async_methods_exist(self):
        for name in ('_beginAsyncBootstrap', '_pollBootstrap', '_continueStart'):
            self.assertTrue(hasattr(self.envoy, name),
                            f'EnvoyExt must define {name}')

    def test_embody_bootstrap_pieces_exist(self):
        emb = self.embody.ext.Embody
        for name in ('_venvPaths', '_environmentNeedsInstall',
                     '_installDependencies'):
            self.assertTrue(hasattr(emb, name),
                            f'EmbodyExt must define {name}')

    def test_bootstrapping_flag_is_bool(self):
        self.assertIsInstance(self.envoy._bootstrapping, bool)

    def test_bootstrap_result_attr_exists(self):
        self.assertTrue(hasattr(self.envoy, '_bootstrap_result'))

    def test_setup_environment_still_callable(self):
        # The synchronous entry point survives for the venv-recreate recovery
        # path; in the dev project the env is already healthy so it returns True.
        self.assertTrue(self.embody.ext.Embody._setupEnvironment())

    def test_mcp_update_marshal_drains_and_logs(self):
        # WP7b fix: the update-check worker publishes to a plain attribute and
        # the MAIN-thread poll drains + logs it (the worker must never call
        # run()). Exercise the drain half end-to-end with a captured Log.
        emb = self.embody.ext.Embody
        captured = []
        original = emb.Log
        emb.Log = lambda msg, level='INFO', **kw: captured.append((msg, level))
        try:
            emb._mcp_update_notice = 'MCP update available: t -> t2 (test)'
            emb._pollMCPUpdate(0)
        finally:
            emb.Log = original
        self.assertEqual(len(captured), 1,
                         f'drain must log exactly once, got {captured!r}')
        self.assertEqual(captured[0][1], 'WARNING')
        self.assertIsNone(getattr(emb, '_mcp_update_notice', 'unset'),
                          'sentinel must clear after drain')

    def test_mcp_update_empty_sentinel_logs_nothing(self):
        # '' means done-without-notice (up to date, or network failed): the
        # poll must clear it silently and log nothing.
        emb = self.embody.ext.Embody
        captured = []
        original = emb.Log
        emb.Log = lambda msg, level='INFO', **kw: captured.append(msg)
        try:
            emb._mcp_update_notice = ''
            emb._pollMCPUpdate(0)
        finally:
            emb.Log = original
        self.assertEqual(captured, [], 'empty sentinel must not log')
        self.assertIsNone(getattr(emb, '_mcp_update_notice', 'unset'))
