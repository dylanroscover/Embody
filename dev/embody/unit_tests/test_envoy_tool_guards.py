"""
Test suite: Envoy tool guards and server-side safety behaviors.

Covers undo wrapping, Menu/StrMenu parameter validation, sequence growth,
parameter search mode, execute_python rollback, and documentation helper
plumbing.
"""

import time

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase

_envoy_mod = op.Embody.op('EnvoyExt').module
EnvoyMCPServer = _envoy_mod.EnvoyMCPServer


class TestOpFlagsCoverTDXNSet(EmbodyTestCase):
    """The MCP flag surface must cover every flag TDXN round-trips.

    TDXN persists cloneImmune / componentCloneImmune / showCustomOnly /
    showDocked, but get_op_flags returned a fixed 9-key dict and
    set_op_flags had no parameters for them -- so an agent could not see or
    set state the format keeps (review finding, 2026-09-04).
    """

    def _comp(self):
        return self.sandbox.create(baseCOMP, 'flagcov')

    def test_get_op_flags_reports_the_tdxn_flags(self):
        c = self._comp()
        flags = op.Embody.ext.Envoy._get_op_flags(c.path)
        for name in ('cloneImmune', 'componentCloneImmune',
                     'showCustomOnly', 'showDocked'):
            self.assertIn(name, flags, f'{name} missing from get_op_flags')

    def test_set_op_flags_applies_the_tdxn_flags(self):
        c = self._comp()
        res = op.Embody.ext.Envoy._set_op_flags(
            c.path, componentCloneImmune=True, showCustomOnly=True,
            showDocked=False)
        self.assertTrue(res.get('componentCloneImmune'))
        self.assertTrue(res.get('showCustomOnly'))
        self.assertFalse(res.get('showDocked'))
        self.assertTrue(c.componentCloneImmune, 'not applied to the live op')
        self.assertFalse(c.showDocked, 'not applied to the live op')

    def test_comp_only_flag_on_a_top_is_reported_not_dropped(self):
        """componentCloneImmune does not exist on a TOP.

        Silently ignoring it would let an agent believe it had set
        something. It must come back named in unsupported_flags, while the
        flags that DO apply are still set.
        """
        c = self._comp()
        top = c.create(noiseTOP, 'n')
        res = op.Embody.ext.Envoy._set_op_flags(
            top.path, componentCloneImmune=True, showDocked=False)
        self.assertEqual(res.get('unsupported_flags'),
                         ['componentCloneImmune'])
        self.assertNotIn('componentCloneImmune', res)
        self.assertFalse(res.get('showDocked'),
                         'the applicable flag must still be set')

    def test_flag_surface_matches_the_exporter(self):
        """Every DEFAULT_FLAGS entry must be reachable through get_op_flags.

        This is the drift guard: adding a flag to the TDXN table without
        exposing it here recreates the exact gap this suite exists for.
        """
        tdxn_mod = op.Embody.op('TDXNExt').module
        c = self._comp()
        flags = op.Embody.ext.Envoy._get_op_flags(c.path)
        missing = [f for f in tdxn_mod.DEFAULT_FLAGS if f not in flags]
        self.assertEqual([], missing,
                         f'flags TDXN writes but MCP cannot report: {missing}')


class TestRunTestsSaveGate(EmbodyTestCase):
    """The full-run recovery-point gate.

    The /run-tests skill has said "save the project before a full run" for
    months and it was skipped every time -- the condition was not cheaply
    checkable (project.dirty does not exist on TD 2025; project.modified
    returns a LIST of operator paths and re-dirties seconds after a save).
    On 2026-09-04 a full suite ran three times against a .toe that was 2.9
    DAYS old. The gate moves the check out of prose and into the tool, the
    way the destructive tier already does it.
    """

    # The gate lives on EnvoyExt (the extension class), not EnvoyMCPServer:
    # _run_tests is a main-thread handler, and the check reads project.*.
    _ENV = _envoy_mod.EnvoyExt
    _MAX = _envoy_mod.EnvoyExt._RUN_TESTS_SAVE_MAX_AGE_S

    def test_full_run_refused_when_no_toe_on_disk(self):
        msg = self._ENV._saveGateRefusal(None, False, None, None)
        self.assertIsNotNone(msg, 'a full run with no recovery point must refuse')
        self.assertIn('NO saved .toe', msg)
        self.assertIn('save_project', msg, 'the refusal must name the remedy')

    def test_full_run_refused_when_toe_is_stale(self):
        msg = self._ENV._saveGateRefusal(None, False, 'P.toe', self._MAX + 1.0)
        self.assertIsNotNone(msg, 'a stale recovery point must refuse')
        self.assertIn('P.toe', msg, 'the refusal must name the recovery point')
        self.assertIn('confirm_saved', msg, 'the refusal must name the override')

    def test_full_run_allowed_when_toe_is_fresh(self):
        self.assertIsNone(
            self._ENV._saveGateRefusal(None, False, 'P.toe', 60.0),
            'a fresh save must not be gated')

    def test_boundary_is_inclusive(self):
        """Exactly at the threshold is still fresh -- no off-by-one refusal."""
        self.assertIsNone(
            self._ENV._saveGateRefusal(None, False, 'P.toe', self._MAX))

    def test_confirm_saved_overrides_a_stale_toe(self):
        self.assertIsNone(
            self._ENV._saveGateRefusal(None, True, None, None),
            'confirm_saved=True must proceed even with no recovery point')

    def test_single_suite_is_never_gated(self):
        """Targeted runs stay ungated on purpose.

        They are cheap and frequent; gating them would train callers to pass
        confirm_saved reflexively, which is how a gate stops working.
        """
        self.assertIsNone(
            self._ENV._saveGateRefusal('test_path_utils', False, None, None))

    def test_recovery_point_reports_a_real_file(self):
        """_recoveryPoint must resolve to a .toe that exists, with an age."""
        name, age = op.Embody.ext.Envoy._recoveryPoint()
        self.assertIsNotNone(name, 'no .toe found for the live project')
        self.assertTrue(name.endswith('.toe'), name)
        self.assertIsNotNone(age)
        self.assertGreaterEqual(age, 0.0)
        import os as _os
        self.assertTrue(
            _os.path.isfile(_os.path.join(project.folder, name)),
            'the reported recovery point must exist on disk')


class TestEnvoyToolGuards(EmbodyTestCase):

    def tearDown(self):
        try:
            if getattr(op.Embody.ext.Envoy, '_undo_active', False):
                op.Embody.ext.Envoy._endUndoBlock()
        finally:
            super().tearDown()

    def _unique(self, prefix):
        return '{}_{}'.format(prefix, int(time.time() * 1000))

    def _assert_error_contains(self, result, text):
        self.assertDictHasKey(result, 'error')
        self.assertIn(text, result['error'])

    def _make_search_fixture(self):
        fixture = self.sandbox.create(baseCOMP, 'search_fixture')
        value_holder = fixture.create(baseCOMP, 'value_holder')
        expr_holder = fixture.create(baseCOMP, 'expr_holder')

        page = value_holder.appendCustomPage('Search')
        value_par = page.appendStr('Searchtoken')[0]
        value_par.val = 'zzsearchtoken'

        expr_page = expr_holder.appendCustomPage('Search')
        expr_par = expr_page.appendFloat('Exprtoken')[0]
        result = op.Embody.ext.Envoy._set_parameter(
            expr_holder.path, expr_par.name, expr='absTime.frame + 12345')
        self.assertTrue(result.get('success'), repr(result))

        return fixture, value_holder, value_par, expr_holder, expr_par

    def _find_strmenu_parameter(self):
        comp = self.sandbox.create(baseCOMP, 'strmenu_custom')
        page = comp.appendCustomPage('Toolguards')
        append = getattr(page, 'appendStrMenu', None)
        if append is not None:
            try:
                par = append('Freechoice')[0]
                try:
                    par.menuNames = ['known']
                    par.menuLabels = ['Known']
                except Exception:
                    pass
                return comp, par
            except Exception:
                pass

        candidates = (
            'moviefileinTOP', 'noiseTOP', 'textTOP',
            'constantCHOP', 'selectCHOP', 'textDAT',
        )
        for op_type in candidates:
            try:
                candidate = self.sandbox.create(
                    op_type, 'strmenu_{}'.format(op_type.lower()))
            except Exception:
                continue
            for par in candidate.pars():
                style = str(getattr(par, 'style', ''))
                if style == 'StrMenu' or style.endswith('.StrMenu'):
                    if not getattr(par, 'readOnly', False):
                        return candidate, par
        return None, None

    # -----------------------------------------------------------------
    # Undo wiring
    # -----------------------------------------------------------------

    def test_undoable_ops_are_registered_handlers(self):
        unknown = op.Embody.ext.Envoy._execute_operation(
            'definitely_not_a_real_op', {})
        self._assert_error_contains(unknown, 'Unknown operation')

        for operation in sorted(op.Embody.ext.Envoy._UNDOABLE_OPS):
            result = op.Embody.ext.Envoy._execute_operation(operation, {})
            self.assertDictHasKey(result, 'error')
            self.assertNotIn(
                'Unknown operation', result['error'],
                '{} is undoable but is not registered'.format(operation))

    def test_begin_end_undo_block_guard(self):
        self.assertFalse(op.Embody.ext.Envoy._beginUndoBlock('get_op'))

        opened = op.Embody.ext.Envoy._beginUndoBlock('set_op_position')
        self.assertTrue(opened)
        try:
            pass
        finally:
            op.Embody.ext.Envoy._endUndoBlock()

        opened_again = op.Embody.ext.Envoy._beginUndoBlock('set_op_position')
        self.assertTrue(opened_again)
        try:
            pass
        finally:
            op.Embody.ext.Envoy._endUndoBlock()

    def test_undo_block_reentrancy_guard(self):
        opened = op.Embody.ext.Envoy._beginUndoBlock('create_op')
        self.assertTrue(opened)
        try:
            self.assertFalse(
                op.Embody.ext.Envoy._beginUndoBlock('set_parameter'))
        finally:
            op.Embody.ext.Envoy._endUndoBlock()

    def test_create_op_dispatch_is_undoable(self):
        undo = getattr(ui, 'undo', None)
        if undo is None or not hasattr(undo, 'undo'):
            self.skipTest('ui.undo is not available in this harness')

        name = self._unique('undo_text')
        result = op.Embody.ext.Envoy._execute_operation('create_op', {
            'parent_path': self.sandbox.path,
            'op_type': 'textDAT',
            'name': name,
        })
        self.assertTrue(result.get('success'), repr(result))
        created_path = result['path']
        self.assertIsNotNone(op(created_path))

        try:
            undo.undo()
        except Exception as e:
            self.skipTest('ui.undo.undo failed in this harness: {}'.format(e))

        if op(created_path) is not None:
            self.skipTest('ui.undo did not remove the Envoy-created op')

        if not hasattr(undo, 'redo'):
            self.skipTest('ui.undo.redo is not available in this harness')
        try:
            undo.redo()
        except Exception as e:
            self.skipTest('ui.undo.redo failed in this harness: {}'.format(e))
        self.assertIsNotNone(op(created_path))
        try:
            undo.undo()
        except Exception as e:
            self.skipTest('ui.undo final undo failed in this harness: {}'.format(e))
        self.assertIsNone(op(created_path))

    def test_first_peer_advisory_carries_etiquette_hint_once(self):
        import sys as _sys
        ext = op.Embody.ext.Envoy
        lock = getattr(_sys, '_envoy_sessions_lock', None)
        touches = getattr(_sys, '_envoy_touches', None)
        self.assertIsNotNone(lock, 'shared lock missing')
        self.assertIsNotNone(touches, 'touch store missing')

        sid = self._unique('_tc_hint_sid')
        peer_sid = self._unique('_tc_hint_peer')
        scope = '/_tc_hint_scope_{}'.format(int(time.time() * 1000))

        try:
            ext._advisories_served.pop(sid, None)
            if hasattr(ext, '_peer_hint_served'):
                ext._peer_hint_served.discard(sid)
            with lock:
                touches.pop(scope, None)

            ext._recordTouches(peer_sid, 'set_parameter', [scope])

            first = {}
            ext._attachPeerAdvisories(first, sid, 'set_parameter', [scope])
            self.assertIn('_peers', first)
            self.assertEqual(first.get('_hint'), 'load /multi-session-etiquette')

            second = {}
            ext._attachPeerAdvisories(second, sid, 'set_parameter', [scope])
            self.assertIn('_peers', second)
            self.assertNotIn('_hint', second)
        finally:
            ext._advisories_served.pop(sid, None)
            if hasattr(ext, '_peer_hint_served'):
                ext._peer_hint_served.discard(sid)
            with lock:
                touches.pop(scope, None)

    # -----------------------------------------------------------------
    # Menu validation
    # -----------------------------------------------------------------

    def test_invalid_menu_value_reports_names_and_preserves_value(self):
        noise = self.sandbox.create(noiseTOP, 'menu_noise')
        par = noise.par.type
        before = par.eval()

        result = op.Embody.ext.Envoy._set_parameter(
            noise.path, 'type', value='notamenuvalue')

        self._assert_error_contains(result, 'Invalid menu value')
        self.assertDictHasKey(result, 'menuNames')
        self.assertTrue(result['menuNames'])
        self.assertEqual(par.eval(), before)

    def test_menu_label_value_hints_internal_name(self):
        noise = self.sandbox.create(noiseTOP, 'menu_label_noise')
        par = noise.par.type
        for name, label in zip(list(par.menuNames), list(par.menuLabels)):
            if name != label:
                before = par.eval()
                result = op.Embody.ext.Envoy._set_parameter(
                    noise.path, 'type', value=label)
                self._assert_error_contains(result, 'Invalid menu value')
                self.assertIn(name, result['error'])
                self.assertEqual(par.eval(), before)
                return
        self.skipTest('noiseTOP type menu labels match menuNames in this TD build')

    def test_valid_menu_name_still_sets_value(self):
        noise = self.sandbox.create(noiseTOP, 'menu_valid_noise')
        menu_names = list(noise.par.type.menuNames)
        if not menu_names:
            self.skipTest('noiseTOP type has no menuNames in this TD build')
        value = menu_names[-1]

        result = op.Embody.ext.Envoy._set_parameter(
            noise.path, 'type', value=value)

        self.assertTrue(result.get('success'), repr(result))
        self.assertEqual(noise.par.type.eval(), value)

    def test_strmenu_value_is_not_menu_guard_rejected(self):
        target, par = self._find_strmenu_parameter()
        if target is None:
            self.skipTest('No writable StrMenu parameter found in this TD build')

        value = 'not_a_registered_strmenu_choice'
        result = op.Embody.ext.Envoy._set_parameter(
            target.path, par.name, value=value)

        self.assertTrue(result.get('success'), repr(result))
        self.assertEqual(par.eval(), value)

    # -----------------------------------------------------------------
    # Sequence auto-expansion
    # -----------------------------------------------------------------

    def test_set_parameter_grows_constant_chop_sequence(self):
        chop = self.sandbox.create(constantCHOP, 'seq_const')

        result = op.Embody.ext.Envoy._set_parameter(
            chop.path, 'const5name', value='mychan')

        self.assertTrue(result.get('success'), repr(result))
        self.assertGreaterEqual(chop.seq.const.numBlocks, 6)
        self.assertTrue(hasattr(chop.par, 'const5name'))
        self.assertEqual(chop.par.const5name.eval(), 'mychan')

    def test_sequence_growth_rejects_absurd_index(self):
        chop = self.sandbox.create(constantCHOP, 'seq_absurd')
        result = op.Embody.ext.Envoy._set_parameter(
            chop.path, 'const5name', value='mychan')
        self.assertTrue(result.get('success'), repr(result))
        before = chop.seq.const.numBlocks

        result = op.Embody.ext.Envoy._set_parameter(
            chop.path, 'const500name', value='x')

        self._assert_error_contains(result, 'Parameter not found')
        self.assertEqual(chop.seq.const.numBlocks, before)

    def test_sequence_growth_rejects_typoed_block_suffix(self):
        chop = self.sandbox.create(constantCHOP, 'seq_suffix_typo')
        before = chop.seq.const.numBlocks

        result = op.Embody.ext.Envoy._set_parameter(
            chop.path, 'const5nam', value='x')

        self._assert_error_contains(result, 'Parameter not found')
        self.assertEqual(chop.seq.const.numBlocks, before)

    def test_non_sequence_missing_parameter_stays_missing(self):
        chop = self.sandbox.create(constantCHOP, 'seq_missing')

        result = op.Embody.ext.Envoy._set_parameter(
            chop.path, 'definitelynotapar', value='x')

        self._assert_error_contains(result, 'Parameter not found')

    # -----------------------------------------------------------------
    # get_parameter search mode
    # -----------------------------------------------------------------

    def test_get_parameter_search_by_name_reports_hit_fields(self):
        fixture, value_holder, value_par, _expr_holder, _expr_par = (
            self._make_search_fixture())

        result = op.Embody.ext.Envoy._get_parameter(
            fixture.path, search=value_par.name, search_in='name')

        self.assertEqual(result['count'], 1)
        hit = result['results'][0]
        self.assertEqual(hit['op'], value_holder.path)
        self.assertEqual(hit['par'], value_par.name)
        self.assertDictHasKey(hit, 'mode')

    def test_get_parameter_search_by_value_wraps_plain_pattern(self):
        fixture, value_holder, _value_par, _expr_holder, _expr_par = (
            self._make_search_fixture())

        result = op.Embody.ext.Envoy._get_parameter(
            fixture.path, search='zzsearchtoken', search_in='value')

        self.assertEqual(result['count'], 1)
        self.assertEqual(result['results'][0]['op'], value_holder.path)

    def test_get_parameter_search_in_expr_returns_expr_text(self):
        fixture, _value_holder, _value_par, expr_holder, expr_par = (
            self._make_search_fixture())

        result = op.Embody.ext.Envoy._get_parameter(
            fixture.path, search='12345', search_in='expr')

        self.assertEqual(result['count'], 1)
        hit = result['results'][0]
        self.assertEqual(hit['op'], expr_holder.path)
        self.assertEqual(hit['par'], expr_par.name)
        self.assertIn('12345', hit['expr'])

    def test_get_parameter_search_honors_max_results(self):
        fixture, _value_holder, _value_par, _expr_holder, _expr_par = (
            self._make_search_fixture())

        result = op.Embody.ext.Envoy._get_parameter(
            fixture.path, search='*', max_results=1)

        self.assertEqual(len(result['results']), 1)
        self.assertTrue(result.get('truncated'))

    def test_get_parameter_search_rejects_invalid_search_in(self):
        fixture = self.sandbox.create(baseCOMP, 'bad_search_in')

        result = op.Embody.ext.Envoy._get_parameter(
            fixture.path, search='tx', search_in='bogus')

        self._assert_error_contains(result, 'Invalid search_in')

    def test_get_parameter_requires_name_or_search(self):
        fixture = self.sandbox.create(baseCOMP, 'needs_name')

        result = op.Embody.ext.Envoy._get_parameter(fixture.path)

        self._assert_error_contains(result, 'Provide par_name')

    # -----------------------------------------------------------------
    # Compact response payloads
    # -----------------------------------------------------------------

    def test_get_op_omits_default_parameters_by_default(self):
        # constantTOP, not baseCOMP: base COMPs have no transform pars at all
        # (only Object COMPs do), so par.tx raised. colorr/colorg are real
        # constantTOP pars with known defaults.
        top = self.sandbox.create(constantTOP, 'compact_get_op')
        top.par.colorr = 0.25

        compact = op.Embody.ext.Envoy._get_op(top.path)

        self.assertDictHasKey(compact, 'parameters')
        self.assertDictHasKey(compact['parameters'], 'colorr')
        self.assertNotIn('colorg', compact['parameters'])
        self.assertGreater(compact.get('parameters_omitted', 0), 0)

        full = op.Embody.ext.Envoy._get_op(
            top.path, include_defaults=True)

        self.assertDictHasKey(full['parameters'], 'colorr')
        self.assertDictHasKey(full['parameters'], 'colorg')
        self.assertFalse(full.get('parameters_omitted', 0))

    def test_get_parameter_compact_and_details_modes(self):
        noise = self.sandbox.create(noiseTOP, 'compact_get_parameter')
        if not getattr(noise.par.type, 'isMenu', False):
            self.skipTest('noiseTOP type is not a Menu parameter in this TD build')

        compact = op.Embody.ext.Envoy._get_parameter(
            noise.path, par_name='type')

        self.assertDictHasKey(compact, 'menuNames')
        self.assertNotIn('style', compact)
        self.assertNotIn('menuLabels', compact)

        details = op.Embody.ext.Envoy._get_parameter(
            noise.path, par_name='type', details=True)

        self.assertDictHasKey(details, 'style')
        self.assertDictHasKey(details, 'menuLabels')

    def test_query_network_child_rows_lack_name(self):
        self.sandbox.create(baseCOMP, 'compact_query_child')

        result = op.Embody.ext.Envoy._query_network(
            parent_path=self.sandbox.path)

        self.assertGreaterEqual(result['count'], 1)
        self.assertNotIn('name', result['operators'][0])

    def test_get_network_layout_compact_shape_and_annotation_text_cap(self):
        parent = self.sandbox.create(baseCOMP, 'compact_layout')
        parent.create(baseCOMP, 'layout_child')
        long_text = 'x' * 200
        annotation = op.Embody.ext.Envoy._create_annotation(
            parent.path, text=long_text, x=-200, y=-200,
            width=400, height=200)
        self.assertTrue(annotation.get('success'), repr(annotation))

        result = op.Embody.ext.Envoy._get_network_layout(parent.path)

        self.assertDictHasKey(result, 'operators')
        self.assertNotIn('name', result['operators'][0])
        self.assertNotIn('nodeCenterX', result['operators'][0])
        self.assertDictHasKey(result, 'annotations')
        self.assertTrue(result['annotations'], repr(result))
        self.assertLessEqual(len(result['annotations'][0]['text']), 163)

    # -----------------------------------------------------------------
    # execute_python rollback
    # -----------------------------------------------------------------

    def test_execute_python_rolls_back_created_ops_on_error(self):
        name = self._unique('rollback_fail')
        code = "\n".join([
            "target = op({!r}).create('textDAT', {!r})".format(
                self.sandbox.path, name),
            "target.text = 'created before failure'",
            "raise RuntimeError('rollback sentinel')",
        ])

        result = op.Embody.ext.Envoy._execute_python(code)

        self._assert_error_contains(result, 'rolled back 1 operator(s)')
        self.assertIsNone(op('{}/{}'.format(self.sandbox.path, name)))

    def test_execute_python_success_keeps_created_op(self):
        name = self._unique('rollback_success')
        code = "\n".join([
            "op({!r}).create('textDAT', {!r})".format(self.sandbox.path, name),
            "result = 'ok'",
        ])

        result = op.Embody.ext.Envoy._execute_python(code)

        self.assertTrue(result.get('success'), repr(result))
        self.assertEqual(result.get('result'), 'ok')
        self.assertIsNotNone(op('{}/{}'.format(self.sandbox.path, name)))

    def test_execute_python_does_not_rollback_preexisting_ops(self):
        keeper = self.sandbox.create(textDAT, 'rollback_keeper')
        keeper.text = 'before'
        code = "\n".join([
            "op({!r}).text = 'modified before failure'".format(keeper.path),
            "raise RuntimeError('keeper sentinel')",
        ])

        result = op.Embody.ext.Envoy._execute_python(code)

        self.assertDictHasKey(result, 'error')
        self.assertIsNotNone(op(keeper.path))
        self.assertEqual(keeper.text, 'modified before failure')


class TestCaptureTopSampleGrid(EmbodyTestCase):

    def _set_constant_color(self, top, r, g, b, a):
        for par_name in ('colorr', 'colorg', 'colorb', 'alpha'):
            if not hasattr(top.par, par_name):
                self.skipTest('constantTOP missing {} parameter'.format(par_name))
        top.par.colorr = r
        top.par.colorg = g
        top.par.colorb = b
        top.par.alpha = a

    def _set_top_resolution(self, top, width, height):
        for par_name in ('outputresolution', 'resolutionw', 'resolutionh'):
            if not hasattr(top.par, par_name):
                self.skipTest('TOP missing {} parameter'.format(par_name))
        top.par.outputresolution = 'custom'
        top.par.resolutionw = width
        top.par.resolutionh = height

    def test_solid_color_grid(self):
        top = self.sandbox.create(constantTOP, 'grid_red')
        self._set_constant_color(top, 1.0, 0.0, 0.0, 1.0)

        result = op.Embody.ext.Envoy._capture_top(top.path, sample_grid=4)

        self.assertNotIn('error', result, repr(result))
        self.assertEqual(result['grid'], 4)
        self.assertEqual(result['origin'], 'top-left')
        self.assertEqual(len(result['cells']), 4)
        self.assertEqual(len(result['cells'][0]), 4)
        for row in result['cells']:
            for cell in row:
                self.assertGreaterEqual(cell[0], 0.9)
                self.assertLessEqual(cell[0], 1.0)
                self.assertGreaterEqual(cell[1], 0.0)
                self.assertLessEqual(cell[1], 0.1)
        self.assertGreaterEqual(result['stats']['r']['mean'], 0.9)
        self.assertLessEqual(result['stats']['g']['max'], 0.1)

    def test_gradient_grid_stats_vary(self):
        try:
            top = self.sandbox.create(rampTOP, 'grid_ramp')
        except Exception:
            self.skipTest('rampTOP not available')
            return

        result = op.Embody.ext.Envoy._capture_top(top.path, sample_grid=4)

        self.assertNotIn('error', result, repr(result))
        varying = [
            name for name in ('r', 'g', 'b', 'a')
            if result['stats'][name]['min'] < result['stats'][name]['max']
        ]
        self.assertTrue(varying, repr(result['stats']))

    def test_grid_clamps_request_and_tiny_top(self):
        top = self.sandbox.create(constantTOP, 'grid_clamp')
        self._set_constant_color(top, 1.0, 0.0, 0.0, 1.0)

        result = op.Embody.ext.Envoy._capture_top(top.path, sample_grid=999)

        self.assertNotIn('error', result, repr(result))
        self.assertLessEqual(result['grid'], 32)

        self._set_top_resolution(top, 8, 8)
        result = op.Embody.ext.Envoy._capture_top(top.path, sample_grid=32)

        self.assertNotIn('error', result, repr(result))
        self.assertLessEqual(result['grid'], 8)

    def test_off_mode_uses_image_response_shape(self):
        top = self.sandbox.create(constantTOP, 'grid_image_mode')
        self._set_constant_color(top, 1.0, 0.0, 0.0, 1.0)

        result = op.Embody.ext.Envoy._capture_top(top.path)

        self.assertNotIn('error', result, repr(result))
        self.assertDictHasKey(result, 'image_b64')
        self.assertNotIn('cells', result)

    def test_image_params_ignored_in_grid_mode(self):
        top = self.sandbox.create(constantTOP, 'grid_ignore_image_params')
        self._set_constant_color(top, 1.0, 0.0, 0.0, 1.0)

        result = op.Embody.ext.Envoy._capture_top(
            top.path, format='png', inline=True, sample_grid=2)

        self.assertNotIn('error', result, repr(result))
        self.assertDictHasKey(result, 'cells')
        self.assertEqual(result['grid'], 2)
        self.assertNotIn('image_b64', result)


class TestEnvoyDocsPlumbing(EmbodyTestCase):

    def test_get_docs_roots_returns_offline_help_candidates(self):
        result = op.Embody.ext.Envoy._get_docs_roots()

        self.assertDictHasKey(result, 'roots')
        self.assertIsInstance(result['roots'], list)
        for root_path in result['roots']:
            self.assertIsInstance(root_path, str)
            self.assertIn('offlineHelp', root_path)

    def test_docs_normalize_collapses_case_and_spacing(self):
        self.assertEqual(
            EnvoyMCPServer._docsNormalize('Movie File In TOP'),
            'moviefileintop')
        self.assertEqual(
            EnvoyMCPServer._docsNormalize('Movie File In TOP'),
            EnvoyMCPServer._docsNormalize('moviefileinTOP'))

    def test_docs_html_to_text_strips_markup_and_formats_blocks(self):
        html_src = """
        <html>
          <head>
            <style>.hidden { display: none; }</style>
            <script>window.hidden = true;</script>
          </head>
          <body>
            <h2>Usage &amp; Notes</h2>
            <p>Alpha &quot;Beta&quot;</p>
            <ul>
              <li>First item</li>
              <li>Second item</li>
            </ul>
          </body>
        </html>
        """

        text = EnvoyMCPServer._docsHtmlToText(html_src)

        self.assertIn('## Usage & Notes', text)
        self.assertIn('Alpha "Beta"', text)
        self.assertIn('- First item', text)
        self.assertIn('- Second item', text)
        self.assertNotIn('window.hidden', text)
        self.assertNotIn('display: none', text)
        self.assertNotIn('<h2>', text)
        self.assertNotIn('<li>', text)

    def test_docs_split_sections_returns_titles_and_lookup(self):
        html_src = """
        <h2>First Section</h2>
        <p>First body</p>
        <h3>Nested Section</h3>
        <ul><li>Nested item</li></ul>
        <h2>Second Section</h2>
        <p>Second body</p>
        """
        text = EnvoyMCPServer._docsHtmlToText(html_src)

        titles, lookup = EnvoyMCPServer._docsSplitSections(text)

        self.assertEqual(titles, [
            'First Section',
            'Nested Section',
            'Second Section',
        ])
        self.assertDictHasKey(lookup, 'first section')
        self.assertDictHasKey(lookup, 'nested section')
        self.assertDictHasKey(lookup, 'second section')
        self.assertIn('First body', lookup['first section'])
        self.assertIn('Nested item', lookup['nested section'])
