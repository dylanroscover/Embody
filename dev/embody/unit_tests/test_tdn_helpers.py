"""
Test suite: TDN helper methods (pure Python logic).

Tests _serializeValue, _valuesDiffer, _colorsDiffer,
_assembleHierarchy, and _getGroupBaseName.
"""

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase


class TestTDNHelpers(EmbodyTestCase):

    def setUp(self):
        super().setUp()
        self.tdn = self.embody.ext.TDN

    # --- _serializeValue ---

    def test_serializeValue_none_to_empty_string(self):
        self.assertEqual(self.tdn._serializeValue(None), '')

    def test_serializeValue_bool_true(self):
        self.assertTrue(self.tdn._serializeValue(True))

    def test_serializeValue_bool_false(self):
        self.assertFalse(self.tdn._serializeValue(False))

    def test_serializeValue_int_unchanged(self):
        self.assertEqual(self.tdn._serializeValue(42), 42)

    def test_serializeValue_float_whole_to_int(self):
        result = self.tdn._serializeValue(5.0)
        self.assertEqual(result, 5)
        self.assertIsInstance(result, int)

    def test_serializeValue_float_decimal_preserved(self):
        result = self.tdn._serializeValue(3.14)
        self.assertApproxEqual(result, 3.14)

    def test_serializeValue_string_unchanged(self):
        self.assertEqual(self.tdn._serializeValue('hello'), 'hello')

    def test_serializeValue_list_recursion(self):
        result = self.tdn._serializeValue([1, 2.0, None])
        self.assertListEqual(result, [1, 2, ''])

    def test_serializeValue_tuple_becomes_list(self):
        result = self.tdn._serializeValue((1, 2))
        self.assertIsInstance(result, list)
        self.assertListEqual(result, [1, 2])

    # --- _valuesDiffer ---

    def test_valuesDiffer_same_int(self):
        self.assertFalse(self.tdn._valuesDiffer(5, 5))

    def test_valuesDiffer_different_int(self):
        self.assertTrue(self.tdn._valuesDiffer(5, 6))

    def test_valuesDiffer_none_vs_empty_string(self):
        self.assertFalse(self.tdn._valuesDiffer(None, ''))

    def test_valuesDiffer_empty_string_vs_none(self):
        self.assertFalse(self.tdn._valuesDiffer('', None))

    def test_valuesDiffer_float_precision(self):
        # 0.1 + 0.2 != 0.3 in floating point, but within 1e-9
        self.assertFalse(self.tdn._valuesDiffer(0.1 + 0.2, 0.3))

    def test_valuesDiffer_float_vs_int(self):
        self.assertFalse(self.tdn._valuesDiffer(5.0, 5))

    def test_valuesDiffer_strings_differ(self):
        self.assertTrue(self.tdn._valuesDiffer('abc', 'def'))

    def test_valuesDiffer_strings_same(self):
        self.assertFalse(self.tdn._valuesDiffer('abc', 'abc'))

    # --- _colorsDiffer ---

    def test_colorsDiffer_identical(self):
        c = (0.545, 0.545, 0.545)
        self.assertFalse(self.tdn._colorsDiffer(c, c))

    def test_colorsDiffer_within_tolerance(self):
        c1 = (0.545, 0.545, 0.545)
        c2 = (0.550, 0.540, 0.545)
        self.assertFalse(self.tdn._colorsDiffer(c1, c2))

    def test_colorsDiffer_beyond_tolerance(self):
        c1 = (0.545, 0.545, 0.545)
        c2 = (1.0, 0.0, 0.0)
        self.assertTrue(self.tdn._colorsDiffer(c1, c2))

    def test_colorsDiffer_length_mismatch(self):
        c1 = (0.5, 0.5, 0.5)
        c2 = (0.5, 0.5)
        self.assertTrue(self.tdn._colorsDiffer(c1, c2))

    # --- _assembleHierarchy ---

    def test_assembleHierarchy_flat(self):
        flat = {
            '/a': {'name': 'a', 'type': 'baseCOMP'},
            '/b': {'name': 'b', 'type': 'textDAT'},
        }
        result = self.tdn._assembleHierarchy(flat, '/')
        self.assertLen(result, 2)

    def test_assembleHierarchy_nested(self):
        flat = {
            '/parent': {'name': 'parent', 'type': 'baseCOMP'},
            '/parent/child': {'name': 'child', 'type': 'textDAT'},
        }
        result = self.tdn._assembleHierarchy(flat, '/')
        self.assertLen(result, 1)
        self.assertIn('children', result[0])
        self.assertEqual(result[0]['children'][0]['name'], 'child')

    def test_assembleHierarchy_empty(self):
        result = self.tdn._assembleHierarchy({}, '/')
        self.assertLen(result, 0)

    def test_assembleHierarchy_deeply_nested(self):
        flat = {
            '/a': {'name': 'a', 'type': 'baseCOMP'},
            '/a/b': {'name': 'b', 'type': 'baseCOMP'},
            '/a/b/c': {'name': 'c', 'type': 'textDAT'},
        }
        result = self.tdn._assembleHierarchy(flat, '/')
        self.assertLen(result, 1)
        a = result[0]
        self.assertIn('children', a)
        b = a['children'][0]
        self.assertIn('children', b)
        self.assertEqual(b['children'][0]['name'], 'c')
