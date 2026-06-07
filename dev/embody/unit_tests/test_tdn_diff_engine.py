"""Pure-engine tests for TDNExt's semantic diff (_diff_normalized / _normalize_
tdn_for_compare). These exercise the static diff engine directly with dict
fixtures -- no sandbox operators -- referencing the extension inline per the
no-ext-caching rule.
"""

import copy

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase


class TestTDNNormalize(EmbodyTestCase):

    def test_normalize_empty(self):
        self.assertEqual(op.Embody.ext.TDN._normalize_tdn_for_compare({}), {})

    def test_normalize_nondict(self):
        self.assertEqual(op.Embody.ext.TDN._normalize_tdn_for_compare(None), {})

    def test_input_not_mutated(self):
        src = {'build': 5, 'operators': [{'name': 'x', 'type': 'noiseTOP'}],
               'type_defaults': {'noiseTOP': {'parameters': {'period': 2}}}}
        before = copy.deepcopy(src)
        op.Embody.ext.TDN._normalize_tdn_for_compare(src)
        self.assertEqual(src, before, 'normalize must not mutate its input')

    def test_volatile_keys_stripped(self):
        n = op.Embody.ext.TDN._normalize_tdn_for_compare({
            'build': 1, 'generator': 'x', 'td_build': 'y', 'exported_at': 'z',
            'source_file': 'Proj.toe', 'type': 'baseCOMP', 'operators': []})
        for k in ('build', 'generator', 'td_build', 'exported_at', 'source_file'):
            self.assertNotIn(k, n)
        self.assertEqual(n.get('type'), 'baseCOMP')

    def test_type_defaults_merged_and_dropped(self):
        n = op.Embody.ext.TDN._normalize_tdn_for_compare({
            'operators': [{'name': 'x', 'type': 'noiseTOP'}],
            'type_defaults': {'noiseTOP': {'parameters': {'period': 2}}}})
        self.assertEqual(n['operators'][0].get('parameters'), {'period': 2})
        self.assertNotIn('type_defaults', n)

    def test_par_templates_resolved(self):
        n = op.Embody.ext.TDN._normalize_tdn_for_compare({
            'operators': [{'name': 'c', 'type': 'baseCOMP',
                           'custom_pars': {'Settings': {'$t': 'settings',
                                                        'Speed': 5}}}],
            'par_templates': {'settings': [{'name': 'Speed', 'style': 'Float'}]}})
        resolved = n['operators'][0]['custom_pars']['Settings']
        self.assertIsInstance(resolved, list)
        self.assertEqual(resolved[0]['name'], 'Speed')
        self.assertEqual(resolved[0].get('value'), 5)
        self.assertNotIn('par_templates', n)


class TestTDNDiffEngine(EmbodyTestCase):

    def test_compression_equivalence_is_empty(self):
        inline = {'type': 'baseCOMP', 'operators': [
            {'name': 'n1', 'type': 'noiseTOP', 'parameters': {'period': 2}},
            {'name': 'n2', 'type': 'noiseTOP', 'parameters': {'period': 2}}]}
        compressed = {'type': 'baseCOMP', 'operators': [
            {'name': 'n1', 'type': 'noiseTOP'},
            {'name': 'n2', 'type': 'noiseTOP'}],
            'type_defaults': {'noiseTOP': {'parameters': {'period': 2}}}}
        d = op.Embody.ext.TDN._diff_normalized(inline, compressed, comp_path='/p')
        self.assertFalse(d['changed'])
        self.assertEqual(d['counts'],
                         {'added': 0, 'removed': 0, 'modified': 0})

    def test_operator_reorder_is_clean(self):
        a = {'operators': [{'name': 'n1', 'type': 'noiseTOP'},
                           {'name': 'n2', 'type': 'noiseTOP'}]}
        b = {'operators': [{'name': 'n2', 'type': 'noiseTOP'},
                           {'name': 'n1', 'type': 'noiseTOP'}]}
        self.assertFalse(
            op.Embody.ext.TDN._diff_normalized(a, b, comp_path='/p')['changed'])

    def test_source_file_only_change_is_clean(self):
        a = {'type': 'baseCOMP', 'operators': [], 'source_file': 'A.toe', 'build': 1}
        b = {'type': 'baseCOMP', 'operators': [], 'source_file': 'B.toe', 'build': 2}
        self.assertFalse(
            op.Embody.ext.TDN._diff_normalized(a, b, comp_path='/p')['changed'])

    def test_root_pseudo_op_param_change(self):
        a = {'type': 'baseCOMP', 'operators': [], 'parameters': {'Res': [1920, 1080]}}
        b = {'type': 'baseCOMP', 'operators': [], 'parameters': {'Res': [1280, 720]}}
        d = op.Embody.ext.TDN._diff_normalized(a, b, comp_path='/scene')
        roots = [m for m in d['modified'] if m['kind'] == 'root']
        self.assertEqual(len(roots), 1)
        self.assertEqual(roots[0]['path'], '/scene')
        self.assertEqual(
            roots[0]['changes']['parameters'],
            [{'name': 'Res', 'old': [1280, 720], 'new': [1920, 1080]}])

    def test_added_and_removed_ops(self):
        a = {'operators': [{'name': 'a', 'type': 'noiseTOP'},
                           {'name': 'b', 'type': 'levelCHOP'}]}
        b = {'operators': [{'name': 'a', 'type': 'noiseTOP'},
                           {'name': 'c', 'type': 'blurTOP'}]}
        d = op.Embody.ext.TDN._diff_normalized(a, b, comp_path='/p')
        self.assertTrue(any(e['name'] == 'b' for e in d['added']))
        self.assertTrue(any(e['name'] == 'c' for e in d['removed']))

    def test_deep_child_change_does_not_mark_ancestors(self):
        def net(gain):
            return {'operators': [
                {'name': 'a', 'type': 'baseCOMP', 'children': [
                    {'name': 'bb', 'type': 'baseCOMP', 'children': [
                        {'name': 'c', 'type': 'levelCHOP',
                         'parameters': {'gain': gain}}]}]}]}
        d = op.Embody.ext.TDN._diff_normalized(net(2), net(1), comp_path='/p')
        self.assertEqual(sorted(m['path'] for m in d['modified']), ['/p/a/bb/c'])

    def test_op_parameter_change_detail(self):
        a = {'operators': [{'name': 'lv', 'type': 'levelCHOP',
                            'parameters': {'gain': '0.5'}}]}
        b = {'operators': [{'name': 'lv', 'type': 'levelCHOP',
                            'parameters': {'gain': '0.8'}}]}
        m = op.Embody.ext.TDN._diff_normalized(a, b, comp_path='/p')['modified'][0]
        self.assertEqual(m['changed_keys'], ['parameters'])
        self.assertEqual(m['changes']['parameters'],
                         [{'name': 'gain', 'old': '0.8', 'new': '0.5'}])

    def test_flags_change(self):
        a = {'operators': [{'name': 'x', 'type': 'noiseTOP', 'flags': ['bypass']}]}
        b = {'operators': [{'name': 'x', 'type': 'noiseTOP', 'flags': []}]}
        m = op.Embody.ext.TDN._diff_normalized(a, b, comp_path='/p')['modified'][0]
        self.assertEqual(m['changes']['flags'], {'old': [], 'new': ['bypass']})

    def test_annotation_change_is_separate_kind(self):
        a = {'operators': [], 'annotations': [
            {'name': 'Note1', 'mode': 'comment', 'title': 'old'}]}
        b = {'operators': [], 'annotations': [
            {'name': 'Note1', 'mode': 'comment', 'title': 'new'}]}
        d = op.Embody.ext.TDN._diff_normalized(a, b, comp_path='/p')
        anns = [m for m in d['modified'] if m['kind'] == 'annotation']
        self.assertEqual(len(anns), 1)
        self.assertEqual(anns[0]['name'], 'Note1')

    def test_tdn_ref_pointer_change(self):
        a = {'operators': [{'name': 'ch', 'type': 'baseCOMP', 'tdn_ref': 'ch.tdn'}]}
        b = {'operators': [{'name': 'ch', 'type': 'baseCOMP', 'tdn_ref': 'old.tdn'}]}
        m = op.Embody.ext.TDN._diff_normalized(a, b, comp_path='/p')['modified'][0]
        self.assertEqual(m['changes']['tdn_ref'],
                         {'old': 'old.tdn', 'new': 'ch.tdn'})

    def test_build_mismatch_warning(self):
        a = {'operators': [], 'td_build': '2025.32000', 'version': '1.4'}
        b = {'operators': [], 'td_build': '2024.30000', 'version': '1.4'}
        d = op.Embody.ext.TDN._diff_normalized(a, b, comp_path='/p')
        self.assertTrue(any('TD build differs' in w for w in d['warnings']))

    def test_duplicate_sibling_names_warn_no_crash(self):
        a = {'operators': [{'name': 'dup', 'type': 'noiseTOP'},
                           {'name': 'dup', 'type': 'noiseTOP'}]}
        b = {'operators': [{'name': 'dup', 'type': 'noiseTOP'}]}
        d = op.Embody.ext.TDN._diff_normalized(a, b, comp_path='/p')
        self.assertTrue(any('Duplicate sibling' in w for w in d['warnings']))

    def test_truncation_is_honest(self):
        live = {'operators': [{'name': 'o%d' % i, 'type': 'noiseTOP',
                'parameters': {'period': i}} for i in range(50)]}
        disk = {'operators': [{'name': 'o%d' % i, 'type': 'noiseTOP',
                'parameters': {'period': i + 100}} for i in range(50)]}
        d = op.Embody.ext.TDN._diff_normalized(
            live, disk, comp_path='/p', max_changed_ops=10)
        self.assertEqual(d['counts']['modified'], 50)
        self.assertEqual(len(d['modified']), 10)
        self.assertTrue(d['truncated']['ops'])
        self.assertEqual(d['truncated']['dropped'], 40)

    def test_truncation_caps_added_and_removed(self):
        # codex finding: max_changed_ops must cap added+removed too, honestly
        live = {'operators': [{'name': 'a%d' % i, 'type': 'noiseTOP'}
                              for i in range(40)]}
        disk = {'operators': [{'name': 'd%d' % i, 'type': 'noiseTOP'}
                              for i in range(40)]}
        d = op.Embody.ext.TDN._diff_normalized(
            live, disk, comp_path='/p', max_changed_ops=10)
        self.assertEqual(d['counts']['added'], 40)
        self.assertEqual(d['counts']['removed'], 40)
        emitted = len(d['added']) + len(d['removed']) + len(d['modified'])
        self.assertLessEqual(emitted, 10)
        self.assertTrue(d['truncated']['ops'])
        self.assertEqual(d['truncated']['dropped'], 80 - emitted)

    def test_identical_is_clean(self):
        same = {'type': 'baseCOMP', 'operators': [
            {'name': 'x', 'type': 'noiseTOP', 'parameters': {'period': 3}}]}
        self.assertFalse(op.Embody.ext.TDN._diff_normalized(
            same, copy.deepcopy(same), comp_path='/p')['changed'])

    def test_baseline_and_schema(self):
        d = op.Embody.ext.TDN._diff_normalized(
            {'operators': []}, {'operators': []}, comp_path='/p')
        self.assertEqual(d['baseline'], 'disk')
        self.assertEqual(d['schema_version'],
                         op.Embody.ext.TDN._DIFF_SCHEMA_VERSION)

    def test_baseline_label_parameterized(self):
        # HEAD mode reuses the same engine with baseline='head'.
        d = op.Embody.ext.TDN._diff_normalized(
            {'operators': []}, {'operators': []}, comp_path='/p',
            baseline='head')
        self.assertEqual(d['baseline'], 'head')
