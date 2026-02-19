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

    def test_last_served_log_id_exists(self):
        self.assertTrue(hasattr(self.envoy, '_last_served_log_id'))

    def test_last_served_log_id_is_int(self):
        self.assertIsInstance(self.envoy._last_served_log_id, int)

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
