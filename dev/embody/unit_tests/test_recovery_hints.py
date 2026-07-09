"""
Test suite: Envoy recovery hints on error envelopes.

Covers the module-level _recovery_hints_for table (pure logic, matched
against the real error strings Envoy emits) and the _attachRecoveryHints
response decorator (additive, never clobbers, never raises).
"""

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase

_envoy_mod = op.Embody.op('EnvoyExt').module
_recovery_hints_for = _envoy_mod._recovery_hints_for


class TestRecoveryHints(EmbodyTestCase):

    def setUp(self):
        super().setUp()
        self.envoy = self.embody.ext.Envoy

    # --- Pure table: message -> hint ---

    def test_operator_not_found_routes_to_query_network(self):
        hints = _recovery_hints_for('Operator not found: /project1/foo')
        self.assertTrue(hints)
        self.assertIn('query_network', hints[0]['next_tools'])

    def test_parameter_not_found_routes_to_get_op(self):
        # Must NOT be swallowed by the path-not-found rule.
        hints = _recovery_hints_for('Parameter not found: Tx')
        self.assertTrue(hints)
        self.assertEqual(hints[0]['next_tools'][0], 'get_op')

    def test_family_mismatch_routes_to_get_op(self):
        hints = _recovery_hints_for('/x is not a TOP (family: CHOP)')
        self.assertTrue(hints)
        self.assertIn('get_op', hints[0]['next_tools'])

    def test_no_pixel_data_routes_to_performance(self):
        hints = _recovery_hints_for('No pixel data available from /noise1')
        self.assertTrue(hints)
        self.assertIn('get_op_performance', hints[0]['next_tools'])

    def test_timeout_routes_to_project_performance(self):
        hints = _recovery_hints_for('Operation timed out after 30 seconds. ...')
        self.assertTrue(hints)
        self.assertIn('get_project_performance', hints[0]['next_tools'])

    def test_thread_conflict_routes_to_exec(self):
        hints = _recovery_hints_for('THREAD CONFLICT: touched op off main thread')
        self.assertTrue(hints)
        self.assertIn('execute_python', hints[0]['next_tools'])

    def test_no_match_returns_empty(self):
        self.assertEqual(_recovery_hints_for('everything is fine'), [])
        self.assertEqual(_recovery_hints_for(''), [])
        self.assertEqual(_recovery_hints_for(None), [])

    def test_capped_at_two(self):
        # A message that could match several rules never floods the envelope.
        hints = _recovery_hints_for(
            'Operator not found and Parameter not found and timed out after')
        self.assertLessEqual(len(hints), 2)

    def test_hint_shape(self):
        hints = _recovery_hints_for('Operator not found: /a')
        h = hints[0]
        for key in ('cause', 'action', 'next_tools'):
            self.assertDictHasKey(h, key)
        self.assertIsInstance(h['next_tools'], list)

    # --- Table is tied to REAL Envoy error strings ---

    def test_real_operator_not_found_string_matches(self):
        # The actual string a handler emits, end to end.
        result = self.envoy._get_op('/definitely/not/here')
        self.assertDictHasKey(result, 'error')
        self.assertTrue(_recovery_hints_for(result['error']),
                        'live error string should match a recovery rule: '
                        + repr(result['error']))

    # --- Decorator behavior ---

    def test_attach_adds_hints_to_error(self):
        result = {'error': 'Operator not found: /x'}
        self.envoy._attachRecoveryHints(result)
        self.assertDictHasKey(result, 'recovery_hints')
        self.assertTrue(result['recovery_hints'])

    def test_attach_noop_on_success(self):
        result = {'success': True, 'width': 64}
        self.envoy._attachRecoveryHints(result)
        self.assertNotIn('recovery_hints', result)

    def test_attach_noop_on_unmatched_error(self):
        result = {'error': 'a totally novel failure with no rule'}
        self.envoy._attachRecoveryHints(result)
        self.assertNotIn('recovery_hints', result)

    def test_attach_does_not_clobber_existing(self):
        sentinel = [{'cause': 'preexisting'}]
        result = {'error': 'Operator not found: /x',
                  'recovery_hints': sentinel}
        self.envoy._attachRecoveryHints(result)
        self.assertEqual(result['recovery_hints'], sentinel)

    def test_attach_tolerates_non_dict(self):
        # Must never raise regardless of input.
        self.envoy._attachRecoveryHints(None)
        self.envoy._attachRecoveryHints('a string')
        self.envoy._attachRecoveryHints(42)
