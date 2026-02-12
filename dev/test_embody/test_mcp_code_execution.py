"""
Test suite: MCP code execution handler in ClaudiusExt.

Tests _execute_python with various code patterns.
"""

runner_mod = op('TestRunner').module
EmbodyTestCase = runner_mod.EmbodyTestCase


class TestMCPCodeExecution(EmbodyTestCase):

    def setUp(self):
        super().setUp()
        self.claudius = self.embody.ext.Claudius

    # --- _execute_python ---

    def test_execute_simple_code(self):
        result = self.claudius._execute_python(code='result = 1 + 1')
        self.assertDictHasKey(result, 'result')
        self.assertEqual(result['result'], '2')

    def test_execute_string_result(self):
        result = self.claudius._execute_python(code='result = "hello"')
        self.assertEqual(result['result'], 'hello')

    def test_execute_op_access(self):
        result = self.claudius._execute_python(
            code='result = op("/").name')
        self.assertDictHasKey(result, 'result')

    def test_execute_no_result_variable(self):
        result = self.claudius._execute_python(code='x = 42')
        # Should succeed but result may be None or empty
        self.assertNotIn('error', result)

    def test_execute_syntax_error(self):
        result = self.claudius._execute_python(code='def (broken')
        self.assertDictHasKey(result, 'error')

    def test_execute_runtime_error(self):
        result = self.claudius._execute_python(
            code='result = 1 / 0')
        self.assertDictHasKey(result, 'error')

    def test_execute_multiline(self):
        code = 'a = 10\nb = 20\nresult = a + b'
        result = self.claudius._execute_python(code=code)
        self.assertEqual(result['result'], '30')
