"""
Test suite: Claudius server lifecycle and configuration.

Tests status parameters, port configuration, running flag,
.mcp.json management, operation routing, and log piggybacking.
Does NOT start/stop the actual server (avoids port conflicts).
"""

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase


class TestServerLifecycle(EmbodyTestCase):

    def setUp(self):
        super().setUp()
        self.claudius = self.embody.ext.Claudius

    # --- Status parameters ---

    def test_status_parameter_exists(self):
        par = self.embody.par.Claudiusstatus
        self.assertIsNotNone(par)

    def test_enable_parameter_exists(self):
        par = self.embody.par.Claudiusenable
        self.assertIsNotNone(par)

    def test_port_parameter_exists(self):
        par = self.embody.par.Claudiusport
        self.assertIsNotNone(par)

    def test_port_is_integer(self):
        port = self.embody.par.Claudiusport.eval()
        self.assertIsInstance(int(port), int)

    # --- Running flag ---

    def test_running_flag_is_stored(self):
        # The running flag should be stored in COMP storage
        val = self.embody.fetch('claudius_running', None)
        self.assertIsNotNone(val)

    def test_running_flag_is_bool(self):
        val = self.embody.fetch('claudius_running', False)
        self.assertIsInstance(val, bool)

    # --- Operation routing ---

    def test_execute_operation_unknown(self):
        result = self.claudius._execute_operation(
            'totally_unknown_operation_xyz', {})
        self.assertDictHasKey(result, 'error')
        self.assertIn('Unknown operation', result['error'])

    def test_execute_operation_valid(self):
        # get_td_info should always work
        result = self.claudius._execute_operation('get_td_info', {})
        self.assertNotIn('error', result)
        self.assertDictHasKey(result, 'version')

    def test_execute_operation_with_params(self):
        comp = self.sandbox.create(baseCOMP, 'routing_test')
        result = self.claudius._execute_operation(
            'get_op', {'op_path': comp.path})
        self.assertNotIn('error', result)

    def test_execute_operation_handler_error(self):
        # Passing invalid params should return error, not crash
        result = self.claudius._execute_operation(
            'get_op', {'op_path': '/nonexistent_lifecycle_test'})
        self.assertDictHasKey(result, 'error')

    # --- Log piggybacking ---

    def test_last_served_log_id_exists(self):
        self.assertTrue(hasattr(self.claudius, '_last_served_log_id'))

    def test_last_served_log_id_is_int(self):
        self.assertIsInstance(self.claudius._last_served_log_id, int)

    # --- Request/response queues ---

    def test_request_queue_exists(self):
        self.assertIsNotNone(self.claudius.request_queue)

    def test_response_queue_exists(self):
        self.assertIsNotNone(self.claudius.response_queue)

    # --- Shutdown event ---

    def test_shutdown_event_on_sys(self):
        import sys
        registry = getattr(sys, '_claudius_shutdown_events', {})
        self.assertIsInstance(registry, dict)

    def test_shutdown_event_registered(self):
        import sys
        registry = getattr(sys, '_claudius_shutdown_events', {})
        self.assertIn(self.embody.path, registry)

    # --- CLAUDIUS_VERSION ---

    def test_claudius_version_constant(self):
        import sys
        # The module should have CLAUDIUS_VERSION defined
        mod_path = self.embody.op('ClaudiusExt')
        if mod_path is not None:
            module = mod_path.module
            self.assertTrue(hasattr(module, 'CLAUDIUS_VERSION'))
