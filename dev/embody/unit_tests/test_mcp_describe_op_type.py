"""describe_op_type: parameter catalog for an operator TYPE (envoy_read).

The look-before-you-guess read. Probes a throwaway instance in /sys/quiet,
caches per type, filters by pattern/page, and names the closest types on a
miss. Not destructive: nothing is created inside the project.
"""

try:
    runner_mod = op.unit_tests.op('TestRunnerExt').module
    EmbodyTestCase = runner_mod.EmbodyTestCase
except (AttributeError, NameError):
    pass  # EmbodyTestCase already injected by test runner


class TestDescribeOpType(EmbodyTestCase):

    def setUp(self):
        super().setUp()
        self.envoy = self.embody.ext.Envoy
        self.read = self.embody.op('envoy_read').module
        self.envoy.__dict__.pop('_optype_cache', None)

    def _describe(self, *args, **kw):
        return self.read.describe_op_type(self.envoy, *args, **kw)

    def test_A01_full_catalog_for_a_top(self):
        r = self._describe('noiseTOP')
        self.assertNotIn('error', r)
        self.assertEqual(r['family'], 'TOP')
        self.assertEqual(r['count'], r['total'])
        self.assertGreater(r['total'], 30)
        self.assertIn('Noise', r['pages'])
        names = {p['name'] for p in r['parameters']}
        self.assertIn('resolutionw', names)
        typ = next(p for p in r['parameters'] if p['name'] == 'type')
        self.assertEqual(typ['style'], 'Menu')
        self.assertIn('simplex3d', typ['menu'])
        self.assertIn('menu_labels', typ)
        self.assertEqual(typ['page'], 'Noise')
        self.assertFalse(r['cached'])

    def test_A02_second_call_is_cached(self):
        first = self._describe('noiseTOP')
        second = self._describe('noiseTOP')
        self.assertFalse(first['cached'])
        self.assertTrue(second['cached'])
        self.assertEqual(first['total'], second['total'])

    def test_A03_pattern_substring_and_glob(self):
        sub = self._describe('noiseTOP', pattern='resol')
        self.assertGreater(sub['count'], 0)
        self.assertLess(sub['count'], sub['total'])
        for p in sub['parameters']:
            self.assertTrue('resol' in p['name'] or 'resol' in p['label'].lower(), p)
        glob = self._describe('noiseTOP', pattern='resolution?')
        self.assertEqual({p['name'] for p in glob['parameters']},
                         {'resolutionw', 'resolutionh'})

    def test_A04_pattern_matches_labels_too(self):
        r = self._describe('lfoCHOP', pattern='Frequency')
        self.assertGreater(r['count'], 0)
        self.assertIn('frequency', {p['name'] for p in r['parameters']})

    def test_A05_page_filter_is_case_insensitive(self):
        r = self._describe('noiseTOP', page='noise')
        self.assertGreater(r['count'], 0)
        self.assertTrue(all(p['page'] == 'Noise' for p in r['parameters']))

    def test_A06_include_menus_false_drops_menus(self):
        r = self._describe('noiseTOP', include_menus=False)
        self.assertFalse(any('menu' in p or 'menu_labels' in p
                             for p in r['parameters']))

    def test_A07_unknown_type_names_the_closest(self):
        r = self._describe('noisTOP')
        self.assertEqual(r.get('error_code'), 'envoy.describe.unknown_type')
        self.assertIn('noiseTOP', r['did_you_mean'])
        self.assertIn('did_you_mean', self._describe(''))

    def test_A08_no_match_carries_a_hint(self):
        r = self._describe('noiseTOP', pattern='zzzz_never')
        self.assertEqual(r['count'], 0)
        self.assertIn('hint', r)

    def test_A09_no_probe_left_behind(self):
        self._describe('lagCHOP')
        left = op('/sys/quiet/envoy_describe_tmp')
        self.assertTrue(left is None or not left.valid)

    def test_A10_handler_stub_is_wired(self):
        """The facade must expose _describe_op_type for the tool wrapper."""
        r = self.envoy._describe_op_type('lagCHOP', pattern='lag*')
        self.assertIn('lag1', {p['name'] for p in r['parameters']})
