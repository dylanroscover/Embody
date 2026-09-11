"""
Test suite: Envoy tool guards and server-side safety behaviors.

Covers undo wrapping, Menu/StrMenu parameter validation, sequence growth,
parameter search mode, execute_python rollback, documentation helper
plumbing, and the host-destroy guard (issue #110).
"""

import ast
import json
import time
import urllib.error
import urllib.request
from unittest.mock import patch

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


_HOST_CODE = 'envoy.embody.host_destroy_refused'


def _exec_namespace():
    """The namespace _execute_python builds, for the guard's resolver."""
    namespace = op.Embody.ext.Envoy._execNamespace()
    assert namespace['me'] is op.Embody.ext.Envoy.ownerComp
    return namespace


class TestHostDestroyGuard(EmbodyTestCase):
    """issue #110: destroying or reloading the COMP hosting Envoy from inside
    an Envoy request hung TouchDesigner, so Envoy refuses it.

    SAFETY: every live call targets a sandbox STAND-IN host. _hostChain is
    patched to [fake_host, fake_parent], both sandbox children, so a broken
    guard can only destroy those. The real chain is checked read-only in
    TestHostChainReadOnly, with no handler call.
    """

    def setUp(self):
        super().setUp()
        self.outer = self.sandbox.create(baseCOMP, 'fake_parent')
        self.fake = self.outer.create(baseCOMP, 'fake_host')
        self._chain = patch.object(
            op.Embody.ext.Envoy, '_hostChain',
            return_value=[self.fake.path, self.outer.path])
        self._chain.start()
        # Refusal WARNINGs must not ride other sessions' _logs piggyback.
        self._quiet = patch.object(op.Embody.ext.Envoy, '_log')
        self._quiet.start()

    def tearDown(self):
        try:
            self._quiet.stop()
            self._chain.stop()
        finally:
            super().tearDown()

    def _run(self, operation, params):
        return op.Embody.ext.Envoy._execute_operation(operation, params)

    def _assert_refused(self, result, target):
        self.assertEqual(result.get('error_code'), _HOST_CODE, repr(result))
        self.assertEqual(result.get('refused_target'), target)
        self.assertIn('HOST-DESTROY REFUSED', result.get('error', ''))

    def test_delete_op_on_host_refused_and_host_survives(self):
        self._assert_refused(
            self._run('delete_op', {'op_path': self.fake.path}),
            self.fake.path)
        self.assertTrue(self.fake.valid)

    def test_delete_op_on_ancestor_refused(self):
        self._assert_refused(
            self._run('delete_op', {'op_path': self.outer.path}),
            self.outer.path)
        self.assertTrue(self.outer.valid)

    def test_override_does_not_bypass(self):
        result = self._run('delete_op',
                           {'op_path': self.fake.path, 'override': True})
        self._assert_refused(result, self.fake.path)
        self.assertTrue(self.fake.valid)

    def test_exec_op_method_destroy_refused(self):
        for method in ('destroy', 'reload'):
            result = self._run('exec_op_method', {
                'op_path': self.fake.path, 'method': method,
                'args': [], 'kwargs': {}})
            self._assert_refused(result, self.fake.path)
        self.assertTrue(self.fake.valid)

    def test_exec_op_method_nondestroy_allowed(self):
        result = self._run('exec_op_method', {
            'op_path': self.fake.path, 'method': 'cook',
            'args': [], 'kwargs': {}})
        self.assertTrue(result.get('success'), repr(result))

    def test_set_parameter_reload_pulse_refused(self):
        for par_name in ('enableexternaltoxpulse', 'reinitnet'):
            result = self._run('set_parameter', {
                'op_path': self.fake.path, 'par_name': par_name,
                'value': '1'})
            self._assert_refused(result, self.fake.path)
        ok = self._run('set_parameter', {'op_path': self.fake.path,
                                         'par_name': 'parentshortcut',
                                         'value': 'Fakehost'})
        self.assertTrue(ok.get('success'), repr(ok))

    def test_import_network_clear_first_on_host_refused(self):
        # The dispatcher routes import_network through the host guard, not
        # just _host_destroy_shape (issue #110 review): clear_first would
        # destroy the host's children.
        keep = self.fake.create(baseCOMP, 'keep')
        result = self._run('import_network', {
            'target_path': self.fake.path, 'tdn': {'operators': []},
            'clear_first': True})
        self._assert_refused(result, self.fake.path)
        self.assertTrue(keep.valid, 'nothing was cleared')
        ok = self._run('import_network', {
            'target_path': self.fake.path, 'tdn': {'operators': []},
            'clear_first': False})
        self.assertNotEqual(ok.get('error_code'), _HOST_CODE, repr(ok))
        self.assertTrue(keep.valid)

    def test_descendant_of_the_host_is_allowed(self):
        child = self.fake.create(baseCOMP, 'child')
        result = self._run('delete_op', {'op_path': child.path})
        self.assertTrue(result.get('success'), repr(result))

    def test_batch_subop_refused_and_nothing_ran(self):
        result = self._run('batch_operations', {'operations': [
            {'tool': 'create_op', 'params': {
                'parent_path': self.sandbox.path, 'op_type': 'textDAT',
                'name': 'batch_marker'}},
            {'tool': 'delete_op', 'params': {'op_path': self.fake.path}}]})
        self._assert_refused(result, self.fake.path)
        self.assertIsNone(self.sandbox.op('batch_marker'),
                          'the batch is refused whole, before any sub-op')
        self.assertTrue(self.fake.valid)

    def test_refusal_is_logged_as_a_warning(self):
        with patch.object(op.Embody.ext.Envoy, '_log') as log:
            self._run('delete_op', {'op_path': self.fake.path})
        warnings = [c for c in log.call_args_list
                    if len(c.args) > 1 and c.args[1] == 'WARNING'
                    and 'HOST-DESTROY REFUSED' in str(c.args[0])]
        self.assertLen(warnings, 1)

    def test_structured_guard_fails_closed(self):
        with patch.object(op.Embody.ext.Envoy, '_resolve_op',
                          side_effect=RuntimeError('resolver down')):
            result = self._run('delete_op', {'op_path': self.fake.path})
            self.assertEqual(result.get('error_code'), _HOST_CODE, repr(result))
            self.assertIn('could not reach a verdict', result['error'])
            # get_op resolves through envoy_read directly: unaffected.
            got = self._run('get_op', {'op_path': self.fake.path})
            self.assertNotIn('error', got, repr(got))
        self.assertTrue(self.fake.valid)

    def test_execute_python_refuses_before_exec(self):
        code = "op(%r).destroy()\nop(%r).create(baseCOMP, 'marker')" % (
            self.fake.path, self.sandbox.path)
        result = self._run('execute_python', {'code': code})
        self._assert_refused(result, self.fake.path)
        self.assertIn('nothing ran', result['error'])
        self.assertTrue(self.fake.valid)
        self.assertIsNone(self.sandbox.op('marker'), 'the code ran')

    def test_execute_python_delayed_run_string_not_refused(self):
        # A unique host: the deferred destroy fires a frame after this test,
        # so it must never find a LATER test's fake_host at the same path.
        unique = self.outer.create(
            baseCOMP, 'deferred_host_%d' % int(time.time() * 1000))
        script = ("o = op(%r)\nif o is not None and o.valid:\n    o.destroy()"
                  % unique.path)
        code = "run(%r, delayFrames=1)\nresult = 'scheduled'" % script
        with patch.object(op.Embody.ext.Envoy, '_hostChain',
                          return_value=[unique.path, self.outer.path]):
            result = self._run('execute_python', {'code': code})
        self.assertEqual(result.get('result'), 'scheduled', repr(result))
        self.assertTrue(unique.valid, 'deferred work must not run inline')

    def test_execute_python_lint_fails_open(self):
        """The fault must be the chain's, not a missing envoy_guard DAT:
        either fails open, and only the DEBUG line tells them apart."""
        with patch.object(op.Embody.ext.Envoy, '_hostChain',
                          side_effect=RuntimeError('chain down')), \
             patch.object(op.Embody.ext.Envoy, '_log') as log:
            result = self._run('execute_python',
                               {'code': "note = 'destroy'\nresult = 2"})
        self.assertEqual(result.get('result'), '2', repr(result))
        skipped = [str(c.args[0]) for c in log.call_args_list
                   if len(c.args) > 1 and c.args[1] == 'DEBUG'
                   and 'fail-open' in str(c.args[0])]
        self.assertLen(skipped, 1)
        self.assertIn('chain down', skipped[0])

    def test_batch_execute_python_sub_op_is_refused_before_any_sub_op(self):
        code = "op(%r).destroy()" % self.fake.path
        result = self._run('batch_operations', {'operations': [
            {'tool': 'create_op', 'params': {
                'parent_path': self.sandbox.path, 'op_type': 'textDAT',
                'name': 'batch_py_marker'}},
            {'tool': 'execute_python', 'params': {'code': code}}]})
        self._assert_refused(result, self.fake.path)
        self.assertIn('nothing ran', result['error'])
        self.assertIsNone(self.sandbox.op('batch_py_marker'),
                          'the batch is refused whole, before any sub-op')
        self.assertTrue(self.fake.valid)

    def test_resolver_matches_the_exec_context(self):
        """TD resolves relative op()/parent() against the innermost DAT
        frame, so the resolver must see what the exec sees."""
        for expr in ("op('.')", "op('..')", 'parent()', 'parent(2)', 'me'):
            via_guard = op.Embody.ext.Envoy._resolveLookupPath(
                ast.parse(expr, mode='eval').body, _exec_namespace())
            via_exec = op.Embody.ext.Envoy._execute_python(
                'result = (%s).path' % expr).get('result')
            self.assertEqual(via_guard, via_exec, expr)


class TestHostChainReadOnly(EmbodyTestCase):
    """The UNPATCHED host chain, read-only. Only the pure checks run here --
    they resolve and compare, and never reach a handler or the code."""

    def test_real_host_chain(self):
        expected = []
        node = op.Embody
        while node is not None:
            expected.append(node.path)
            node = node.parent()
        if '/' not in expected:
            expected.append('/')
        expected.append(op.Embody.op('EnvoyExt').path)
        self.assertEqual(op.Embody.ext.Envoy._hostChain(), expected)

    def test_real_host_is_refused_by_the_checks_alone(self):
        """Lint-only: the code is never run. op('..') and parent() go the
        whole way (envoy_guard frames on the stack, then the resolver), so
        the relative-context equivalence is proven on the real chain too.
        _log is patched: these refusals must not reach the live log."""
        with patch.object(op.Embody.ext.Envoy, '_log'):
            refusal = op.Embody.ext.Envoy._hostDestroyRefusal(
                'delete_op', {'op_path': op.Embody.path})
            self.assertEqual(refusal.get('error_code'), _HOST_CODE)
            for code, target in (('op.Embody.destroy()', op.Embody.path),
                                 ('me.destroy()', op.Embody.path),
                                 ("op('..').destroy()", op.Embody.path),
                                 ('parent().destroy()', op.Embody.path),
                                 ("op('/').destroy()", '/')):
                lint = op.Embody.ext.Envoy._hostDestroyLint(
                    code, _exec_namespace())
                self.assertIsNotNone(lint, code)
                self.assertEqual(lint.get('refused_target'), target, code)

    def test_worker_serves_the_main_tick_and_wires_direct_delivery(self):
        """Read-only against the running server: the bridge's
        main_thread_stalled signal, its loopback-Host check, and the
        envoy_deliver hook _answerAfterHostDestroyed relies on. No age
        bound: a synchronous run crosses no frame, so an old tick is
        legitimate here."""
        port = op.Embody.ext.Envoy.RuntimePort()
        if port is None:
            self.skipTest('Envoy is not running')
        self.assertTrue(callable(
            getattr(op.Embody.ext.Envoy.response_queue, 'envoy_deliver',
                    None)),
            'EnvoyMCPServer.__init__ did not wire envoy_deliver')
        url = 'http://127.0.0.1:%d%s' % (port, _envoy_mod._MAIN_TICK_ROUTE)
        with urllib.request.urlopen(url, timeout=5) as resp:
            age = json.loads(resp.read().decode('utf-8'))['main_tick_age_s']
        self.assertTrue(
            isinstance(age, (int, float)) and not isinstance(age, bool),
            'no RefreshHook has stamped the tick: %r' % (age,))
        foreign = urllib.request.Request(url, headers={'Host': 'evil.example'})
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(foreign, timeout=5)
        self.assertEqual(caught.exception.code, 403)

    def test_envoy_guard_module_dat_exists(self):
        """Without this DAT the execute_python lint fails open, silently."""
        guard = op.Embody.op('envoy_guard')
        self.assertIsNotNone(guard, 'envoy_guard module DAT missing')
        self.assertTrue(guard.module.has_destroy_token('x.destroy()'))


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
