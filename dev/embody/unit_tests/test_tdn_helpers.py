"""
Test suite: TDN helper methods (pure Python logic).

Tests _serializeValue, _valuesDiffer, _colorsDiffer,
_assembleHierarchy, _getGroupBaseName, _serializeStorageValue,
_deserializeStorageValue, _tdn_content_equal, and _read_existing_tdn.
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

    # --- _serializeStorageValue ---

    def test_serializeStorageValue_int(self):
        self.assertEqual(self.tdn._serializeStorageValue(42), 42)

    def test_serializeStorageValue_float(self):
        result = self.tdn._serializeStorageValue(3.14)
        self.assertApproxEqual(result, 3.14)

    def test_serializeStorageValue_string(self):
        self.assertEqual(self.tdn._serializeStorageValue('hello'), 'hello')

    def test_serializeStorageValue_bool(self):
        self.assertTrue(self.tdn._serializeStorageValue(True))
        self.assertFalse(self.tdn._serializeStorageValue(False))

    def test_serializeStorageValue_none(self):
        self.assertIsNone(self.tdn._serializeStorageValue(None))

    def test_serializeStorageValue_list(self):
        result = self.tdn._serializeStorageValue([1, 'a', True])
        self.assertEqual(result, [1, 'a', True])

    def test_serializeStorageValue_dict(self):
        result = self.tdn._serializeStorageValue({'k': 'v'})
        self.assertEqual(result, {'k': 'v'})

    def test_serializeStorageValue_tuple(self):
        result = self.tdn._serializeStorageValue((1, 2, 3))
        self.assertEqual(result, {'$type': 'tuple', '$value': [1, 2, 3]})

    def test_serializeStorageValue_set(self):
        result = self.tdn._serializeStorageValue({'c', 'a', 'b'})
        self.assertEqual(result['$type'], 'set')
        self.assertEqual(sorted(result['$value']), ['a', 'b', 'c'])

    def test_serializeStorageValue_bytes(self):
        import base64
        result = self.tdn._serializeStorageValue(b'\x00\x01\x02')
        self.assertEqual(result['$type'], 'bytes')
        self.assertEqual(base64.b64decode(result['$value']), b'\x00\x01\x02')

    def test_serializeStorageValue_whole_float_to_int(self):
        """Whole-number floats are normalized to int."""
        self.assertEqual(self.tdn._serializeStorageValue(42.0), 42)
        self.assertIsInstance(self.tdn._serializeStorageValue(42.0), int)

    # --- _deserializeStorageValue ---

    def test_deserializeStorageValue_primitives(self):
        self.assertEqual(self.tdn._deserializeStorageValue(42), 42)
        self.assertEqual(self.tdn._deserializeStorageValue('hi'), 'hi')
        self.assertTrue(self.tdn._deserializeStorageValue(True))
        self.assertIsNone(self.tdn._deserializeStorageValue(None))

    def test_deserializeStorageValue_list(self):
        result = self.tdn._deserializeStorageValue([1, 'a'])
        self.assertEqual(result, [1, 'a'])

    def test_deserializeStorageValue_dict(self):
        result = self.tdn._deserializeStorageValue({'k': 'v'})
        self.assertEqual(result, {'k': 'v'})

    def test_deserializeStorageValue_tuple(self):
        result = self.tdn._deserializeStorageValue(
            {'$type': 'tuple', '$value': [1, 2]})
        self.assertEqual(result, (1, 2))
        self.assertIsInstance(result, tuple)

    def test_deserializeStorageValue_set(self):
        result = self.tdn._deserializeStorageValue(
            {'$type': 'set', '$value': ['a', 'b']})
        self.assertEqual(result, {'a', 'b'})
        self.assertIsInstance(result, set)

    def test_deserializeStorageValue_bytes(self):
        import base64
        encoded = base64.b64encode(b'\xff\x00').decode('ascii')
        result = self.tdn._deserializeStorageValue(
            {'$type': 'bytes', '$value': encoded})
        self.assertEqual(result, b'\xff\x00')
        self.assertIsInstance(result, bytes)

    def test_deserializeStorageValue_unknown_type(self):
        """Unknown $type is treated as a plain dict."""
        result = self.tdn._deserializeStorageValue(
            {'$type': 'unknown', '$value': 'x'})
        self.assertIsInstance(result, dict)

    # --- _tdn_content_equal ---

    def _make_tdn(self, **overrides):
        """Build a minimal TDN dict with sensible defaults."""
        base = {
            'format': 'tdn',
            'version': '1.0',
            'build': 1,
            'generator': 'Embody/5.0.200',
            'td_build': '099.2025.32280',
            'exported_at': '2026-01-01T00:00:00Z',
            'network_path': '/test',
            'options': {'include_dat_content': True},
            'operators': [
                {'name': 'noise1', 'type': 'noiseTOP'},
            ],
        }
        base.update(overrides)
        return base

    def test_tdn_content_equal_identical(self):
        """Identical dicts (same volatile fields) returns True."""
        tdn = self._make_tdn()
        self.assertTrue(self.tdn._tdn_content_equal(tdn, tdn.copy()))

    def test_tdn_content_equal_only_volatile_diff(self):
        """Dicts differing only in volatile header fields returns True."""
        a = self._make_tdn()
        b = self._make_tdn(
            build=99,
            generator='Embody/9.9.999',
            td_build='100.2030.99999',
            exported_at='2030-12-31T23:59:59Z',
        )
        self.assertTrue(self.tdn._tdn_content_equal(a, b))

    def test_tdn_content_equal_different_operators(self):
        a = self._make_tdn()
        b = self._make_tdn(operators=[
            {'name': 'noise1', 'type': 'noiseTOP'},
            {'name': 'null1', 'type': 'nullTOP'},
        ])
        self.assertFalse(self.tdn._tdn_content_equal(a, b))

    def test_tdn_content_equal_different_options(self):
        a = self._make_tdn()
        b = self._make_tdn(options={'include_dat_content': False})
        self.assertFalse(self.tdn._tdn_content_equal(a, b))

    def test_tdn_content_equal_extra_key_in_existing(self):
        """Key present in existing but not in new is detected."""
        a = self._make_tdn()
        b = self._make_tdn(annotations=[{'name': 'ann1'}])
        self.assertFalse(self.tdn._tdn_content_equal(a, b))

    def test_tdn_content_equal_extra_key_in_new(self):
        """Key present in new but not in existing is detected."""
        a = self._make_tdn(custom_pars=[{'name': 'Speed'}])
        b = self._make_tdn()
        self.assertFalse(self.tdn._tdn_content_equal(a, b))

    def test_tdn_content_equal_different_version(self):
        """Non-volatile header field 'version' difference is detected."""
        a = self._make_tdn()
        b = self._make_tdn(version='2.0')
        self.assertFalse(self.tdn._tdn_content_equal(a, b))

    # --- _read_existing_tdn ---

    def test_read_existing_tdn_missing_file(self):
        import os, tempfile
        path = os.path.join(tempfile.gettempdir(), 'nonexistent_abc123.tdn')
        self.assertIsNone(self.tdn._read_existing_tdn(path))

    def test_read_existing_tdn_corrupt_file(self):
        import os, tempfile
        path = os.path.join(tempfile.gettempdir(), 'corrupt_test.tdn')
        try:
            with open(path, 'w') as f:
                f.write('not valid json {{{')
            self.assertIsNone(self.tdn._read_existing_tdn(path))
        finally:
            if os.path.exists(path):
                os.unlink(path)

    def test_read_existing_tdn_valid_file(self):
        import os, json, tempfile
        path = os.path.join(tempfile.gettempdir(), 'valid_test.tdn')
        data = {'format': 'tdn', 'operators': []}
        try:
            with open(path, 'w') as f:
                json.dump(data, f)
            result = self.tdn._read_existing_tdn(path)
            self.assertIsNotNone(result)
            self.assertEqual(result['format'], 'tdn')
        finally:
            if os.path.exists(path):
                os.unlink(path)
