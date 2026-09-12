"""
Test suite: TDXN content safety -- the save-time report (issue #109).

The 'TDXN Content at Risk' modal was unreachable on every save since
v6.0.39 and reported a false DAT loss at SUCCESS: the exporter has embedded
editable, unbacked DAT content since v6.0.261. The check now shares the
exporter's rule (TDXNExt._datContentDisposition), never opens a dialog, and
reports storage the .tdxn cannot hold at the level its consequence earns.

Covers:
  A. _findAtRiskStorage: user keys found, also below a self-referencing
     clone master; control/runtime keys, a per-COMP embed override and
     TOX-tagged subtrees excluded; nested TDXN shells counted once, as a
     descendant of their TDXN parent
  B. _checkTDXNContentSafety: no dialog, no tagging under 'ask', storage
     logged with its knobs at the _storageLossConsequence level, 'ignore'
     silent
  C. 'externalize' defers the filing past the save window (its run()
     string compiles and reaches the filer) and files only unbacked,
     editable DATs
  D. DAT filters: TD-managed and untaggable types, generated DATs,
     animationCOMP tables, callback DATs, TOX/clone interiors, and a static
     pin that every content disposition keeps the content
  E. Reporter step 4 end to end: the check plus a live export keep every
     authored table and text in the .tdxn
  F. Save-window hardening: a content-check bug cannot skip Phase 1; an
     unanswered palette (one WARNING per save, naming every clone) or duplicate-path
     prompt, and a dropped duplicate companion holding unique content
     (once per path), all log at WARNING -- a copy of the original stays
     INFO; WARNING/ERROR lines also land in the ring save_project reads

Every log assertion is scoped: _getTDXNStrategyComps is stubbed to the
sandbox's TDXN parent and entries are diffed by id (the ring is bounded).
The real _externalizeDATs never runs here -- it would tag and write files
in the live project.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

try:
    runner_mod = op.unit_tests.op('TestRunnerExt').module
    EmbodyTestCase = runner_mod.EmbodyTestCase
except (AttributeError, NameError):
    pass


class TestTDXNSafetyGuards(EmbodyTestCase):

    def setUp(self) -> None:
        super().setUp()
        # The sandbox lives inside a registered TDXN-strategy COMP
        # (test_sandbox in the unit_tests project), so storage we set on
        # self.sandbox is detected by _findAtRiskStorage under that parent.
        self._prev_embed_storage = self.embody.par.Embedstorageintdxns.eval()
        self._prev_embed_dats = self.embody.par.Embeddatsintdxns.eval()
        self.embody.par.Embedstorageintdxns.val = False
        self.embody.par.Embeddatsintdxns.val = False
        self._prev_safety = self.embody.par.Tdxndatsafety.eval()
        self.embody.par.Tdxndatsafety.val = 'ask'
        self._stubbed = []
        # Intercept messageBox so tests never block on UI -- and so any
        # dialog the check tried to open is counted.
        self._captured = []
        self._scripted_choice = -1

        def _box(title: str, message: str, buttons: list) -> int:
            self._captured.append({'title': title, 'message': message,
                                   'buttons': list(buttons)})
            return self._scripted_choice

        self._stub('_messageBox', _box)

    def tearDown(self) -> None:
        for name in reversed(self._stubbed):
            try:
                delattr(self.embody_ext, name)
            except AttributeError:
                pass
        self.embody.par.Embedstorageintdxns.val = self._prev_embed_storage
        self.embody.par.Embeddatsintdxns.val = self._prev_embed_dats
        self.embody.par.Tdxndatsafety.val = self._prev_safety
        super().tearDown()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _stub(self, name: str, fn: Callable) -> None:
        """Shadow an EmbodyExt method on the live instance for this test."""
        setattr(self.embody_ext, name, fn)
        self._stubbed.append(name)

    def _scopeToSandbox(self, extra_paths: tuple = ()) -> COMP:
        """Limit the TDXN row set to the sandbox's TDXN parent (plus any
        extra synthetic rows) so nothing depends on project state."""
        parent = self.sandbox.parent()
        rows = [r for r in self.embody_ext._getTDXNStrategyComps()
                if r[0] == parent.path][:1] or [(parent.path, '')]
        rows += [(p, '') for p in extra_paths]
        self._stub('_getTDXNStrategyComps', lambda: list(rows))
        return parent

    def _mark(self) -> int:
        buf = self.embody_ext._log_buffer
        return buf[-1]['id'] if buf else 0

    def _logsSince(self, mark: int) -> list:
        return [e for e in self.embody_ext._log_buffer if e['id'] > mark]

    def _flatten(self, result: list) -> dict:
        """Flatten [(comp_path, [(op_path, [keys])])] into {op_path: set(keys)}."""
        out = {}
        for _, entries in result:
            for op_path, keys in entries:
                out.setdefault(op_path, set()).update(keys)
        return out

    def _flatten_dats(self, result: list) -> set:
        """Flatten [(comp_path, [dat_ops])] into a set of DAT paths."""
        return {d.path for _, dats in result for d in dats}

    def _text(self, name: str, text: str = 'user-authored content',
              parent: Optional[COMP] = None) -> DAT:
        d = (parent or self.sandbox).create(textDAT, name)
        d.text = text
        return d

    def _generatedSet(self) -> tuple:
        """tbl, a null wired from it, a select reading it, and a select
        cooked then locked (locked = editable again)."""
        tbl = self.sandbox.create(tableDAT, 'gen_tbl')
        tbl.clear()
        tbl.appendRow(['a', 'b'])
        tbl.appendRow(['1', '2'])
        nul = self.sandbox.create(nullDAT, 'gen_null')
        nul.inputConnectors[0].connect(tbl)
        sel = self.sandbox.create(selectDAT, 'gen_select')
        sel.par.dat = 'gen_tbl'
        locked = self.sandbox.create(selectDAT, 'gen_select_locked')
        locked.par.dat = 'gen_tbl'
        for d in (tbl, nul, sel, locked):
            d.cook(force=True)
        locked.lock = True
        return tbl, nul, sel, locked

    # ------------------------------------------------------------------
    # A. Storage detection
    # ------------------------------------------------------------------

    def test_findAtRiskStorage_detects_user_key(self) -> None:
        self.sandbox.store('my_user_key', {'some': 'data'})
        try:
            flat = self._flatten(self.embody_ext._findAtRiskStorage())
            self.assertIn(self.sandbox.path, flat,
                f'Sandbox at {self.sandbox.path} missing from result: {flat}')
            self.assertIn('my_user_key', flat[self.sandbox.path])
        finally:
            self.sandbox.unstore('my_user_key')

    def test_findAtRiskStorage_ignores_control_keys(self) -> None:
        # Control keys on an op must not surface as at-risk.
        self.sandbox.store('embed_storage_in_tdn', False)
        self.sandbox.store('embed_dats_in_tdn', False)
        try:
            flat = self._flatten(self.embody_ext._findAtRiskStorage())
            keys_on_sandbox = flat.get(self.sandbox.path, set())
            self.assertNotIn('embed_storage_in_tdn', keys_on_sandbox)
            self.assertNotIn('embed_dats_in_tdn', keys_on_sandbox)
        finally:
            self.sandbox.unstore('embed_storage_in_tdn')
            self.sandbox.unstore('embed_dats_in_tdn')

    def test_findAtRiskStorage_ignores_runtime_keys(self) -> None:
        # Keys in _STORAGE_SKIP_KEYS are runtime noise, not user data.
        self.sandbox.store('_init_complete', True)
        self.sandbox.store('hover', False)
        try:
            flat = self._flatten(self.embody_ext._findAtRiskStorage())
            keys_on_sandbox = flat.get(self.sandbox.path, set())
            self.assertNotIn('_init_complete', keys_on_sandbox)
            self.assertNotIn('hover', keys_on_sandbox)
        finally:
            self.sandbox.unstore('_init_complete')
            self.sandbox.unstore('hover')

    def test_findAtRiskStorage_empty_when_per_comp_embed_on(self) -> None:
        # Per-COMP override of embed_storage_in_tdn=True excludes the
        # enclosing TDXN COMP from at-risk detection.
        # (We store on the test_sandbox TDXN COMP, which is self.sandbox's
        # registered TDXN parent.)
        tdxn_parent = self.sandbox.parent()
        tdxn_parent.store('embed_storage_in_tdn', True)
        self.sandbox.store('my_key', 'value')
        try:
            flat = self._flatten(self.embody_ext._findAtRiskStorage())
            keys_on_sandbox = flat.get(self.sandbox.path, set())
            self.assertNotIn('my_key', keys_on_sandbox,
                'embed_storage=True on the TDXN parent must exclude descendants')
        finally:
            self.sandbox.unstore('my_key')
            tdxn_parent.unstore('embed_storage_in_tdn')

    def test_findAtRiskStorage_skips_tox_tagged_subtree(self) -> None:
        """A .tox keeps its own storage and its descendants' -- the
        exporter writes a tox_ref and never recurses into it."""
        self._scopeToSandbox()
        tox_tag = self.embody.par.Toxtag.val
        toxchild = self.sandbox.create(baseCOMP, 'safety_toxchild')
        inner = toxchild.create(baseCOMP, 'inner')
        plain = self.sandbox.create(baseCOMP, 'safety_plain')
        toxchild.tags.add(tox_tag)
        try:
            toxchild.store('tox_key', 1)
            inner.store('deep_key', 1)
            plain.store('plain_key', 1)
            flat = self._flatten(self.embody_ext._findAtRiskStorage())
            self.assertIn(plain.path, flat,
                'positive control: an untagged COMP with storage is listed')
            self.assertNotIn(toxchild.path, flat,
                'a TOX-tagged COMP keeps its own storage in its .tox')
            self.assertNotIn(inner.path, flat,
                'storage below a TOX-tagged COMP lives in that .tox')
        finally:
            toxchild.tags.discard(tox_tag)

    def test_storage_below_self_referencing_master_is_reported(self) -> None:
        """clone=me with cloning on marks a MASTER (EmbodyExt.isInsideClone),
        not a clone: the export serializes its interior, so storage below it
        is reported -- the shared scope walk once skipped it."""
        self._scopeToSandbox()
        master = self.sandbox.create(baseCOMP, 'safety_selfmaster')
        master.par.clone.expr = 'me'  # expression: no clone-sync recursion
        master.par.enablecloning = True
        inner = master.create(baseCOMP, 'inner')
        inner.store('master_key', 1)
        self.assertFalse(self.embody_ext.isInsideClone(inner),
            'setup: isInsideClone reads this as a master')
        flat = self._flatten(self.embody_ext._findAtRiskStorage())
        self.assertIn('master_key', flat.get(inner.path, set()),
            f'storage below a self-referencing master is at risk: {flat}')

    def test_nested_tdxn_shell_counted_once_as_descendant(self) -> None:
        """The parent's strip/restore rebuilds a nested TDXN shell, so its
        own keys belong to the parent's report -- once, never twice."""
        shell = self.sandbox.create(baseCOMP, 'safety_nested_shell')
        inner = shell.create(baseCOMP, 'inner')
        parent = self._scopeToSandbox(extra_paths=(shell.path,))
        shell.store('shell_key', 1)
        inner.store('inner_key', 1)
        result = self.embody_ext._findAtRiskStorage()
        by_comp = {cp: dict(entries) for cp, entries in result}
        self.assertIn(shell.path, by_comp.get(parent.path, {}),
            'the nested shell is a descendant of its TDXN parent')
        self.assertNotIn(shell.path, by_comp.get(shell.path, {}),
            'the nested shell is never a root of its own row')
        self.assertIn(inner.path, by_comp.get(shell.path, {}),
            'the nested row still reports its own descendants')
        seen = [p for _, entries in result for p, _ in entries]
        self.assertEqual(len(seen), len(set(seen)),
            f'an op was reported twice: {seen}')

    def test_nested_shell_embedding_storage_is_not_reported(self) -> None:
        """A nested shell with its own embed on restores its root keys
        from its own .tdxn -- the parent must not report them."""
        shell = self.sandbox.create(baseCOMP, 'safety_nested_embed')
        self._scopeToSandbox(extra_paths=(shell.path,))
        shell.store('embed_storage_in_tdn', True)
        shell.store('shell_key', 1)
        flat = self._flatten(self.embody_ext._findAtRiskStorage())
        self.assertNotIn(shell.path, flat)

    # ------------------------------------------------------------------
    # B. The check: no dialog, no tagging, storage at the verdict level
    # ------------------------------------------------------------------

    def test_check_never_opens_a_dialog(self) -> None:
        self._scopeToSandbox()
        self._text('safety_text')
        self.sandbox.store('risky', 'data')
        try:
            for pref in ('ask', 'externalize', 'ignore'):
                self.embody.par.Tdxndatsafety.val = pref
                if pref == 'externalize':
                    self._stub('_scheduleUnbackedFiling',
                               lambda attempt=0: None)
                self.embody_ext._checkTDXNContentSafety()
            self.assertEqual(self._captured, [],
                'the save-time check must never open a dialog')
        finally:
            self.sandbox.unstore('risky')

    def test_ask_does_not_flag_or_tag_unbacked_dats(self) -> None:
        """An unbacked text DAT is embedded by the export: 'ask' must say
        nothing about DATs and must not tag it."""
        self._scopeToSandbox()
        dat = self._text('safety_text')
        mark = self._mark()
        self.embody_ext._checkTDXNContentSafety()
        noisy = [e['message'] for e in self._logsSince(mark)
                 if e.get('level') in ('WARNING', 'SUCCESS')
                 and 'DAT(s)' in e.get('message', '')]
        self.assertEqual(noisy, [], 'no DAT line may be logged under ask')
        dat_tags = set(self.embody_ext.getTags('DAT'))
        self.assertFalse(dat.tags & dat_tags,
            'the check must never tag a DAT inside the save window')

    def test_storage_finding_names_the_knobs_at_the_verdict_level(self) -> None:
        self._scopeToSandbox()
        self.sandbox.store('soon_gone', 1)
        try:
            self.assertIn('soon_gone', self._flatten(
                self.embody_ext._findAtRiskStorage()).get(self.sandbox.path, set()))
            strip_par = getattr(self.embody.par, 'Tdxnstriponsave', None)
            create_par = getattr(self.embody.par, 'Tdxncreateonstart', None)
            level, consequence = self.embody_ext._storageLossConsequence(
                self.embody_ext._tdxnMode(),
                bool(strip_par.eval()) if strip_par is not None else True,
                bool(create_par.eval()) if create_par is not None else True,
                False)
            mark = self._mark()
            self.embody_ext._checkTDXNContentSafety()
            # One entry per consequence class; the sandbox's op may sit past
            # the 5-path summary cut, so match the class, not the key.
            hits = [e for e in self._logsSince(mark)
                    if consequence in e.get('message', '')]
            self.assertEqual(len(hits), 1, f'expected one storage entry: {hits}')
            msg = hits[0]['message']
            self.assertTrue('soon_gone' in msg or 'more)' in msg,
                f'the key is named or counted past the summary cut: {msg}')
            self.assertEqual(hits[0]['level'], level,
                'the level must be the consequence verdict for live settings')
            for knob in ('Embedstorageintdxns', 'Embed storage in tdxn',
                         'Tdxndatsafety', '.tdxn'):
                self.assertIn(knob, msg)
            self.assertTrue(msg.isascii(), f'non-ASCII in log text: {msg!r}')
        finally:
            self.sandbox.unstore('soon_gone')

    def test_root_only_storage_logs_info(self) -> None:
        """Root keys ride the COMP shell in the .toe in every mode."""
        parent = self._scopeToSandbox()
        parent.store('root_only_key', 1)
        try:
            mark = self._mark()
            self.embody_ext._checkTDXNContentSafety()
            hits = [e for e in self._logsSince(mark)
                    if 'root_only_key' in e.get('message', '')]
            self.assertEqual(len(hits), 1)
            self.assertEqual(hits[0]['level'], 'INFO')
            self.assertIn('rides the COMP shell', hits[0]['message'])
        finally:
            parent.unstore('root_only_key')

    def test_ignore_preference_is_silent(self) -> None:
        self._scopeToSandbox()
        self.embody.par.Tdxndatsafety.val = 'ignore'
        self.sandbox.store('risky', 'data')
        try:
            mark = self._mark()
            self.embody_ext._checkTDXNContentSafety()
            said = [e['message'] for e in self._logsSince(mark)
                    if 'risky' in e.get('message', '')
                    or 'storage' in e.get('message', '').lower()]
            self.assertEqual(said, [], "'ignore' must log nothing")
            self.assertEqual(self._captured, [])
        finally:
            self.sandbox.unstore('risky')

    # ------------------------------------------------------------------
    # C. 'externalize': deferred filing, unbacked editable DATs only
    # ------------------------------------------------------------------

    def test_externalize_defers_filing_past_the_save_window(self) -> None:
        self._scopeToSandbox()
        self.embody.par.Tdxndatsafety.val = 'externalize'
        scheduled, filed = [], []
        self._stub('_scheduleUnbackedFiling',
                   lambda attempt=0: scheduled.append(attempt))
        self._stub('_externalizeDATs',
                   lambda dats: filed.append(list(dats)) or 0)
        self._text('safety_text')
        self.embody_ext._checkTDXNContentSafety()
        self.assertEqual(scheduled, [0], 'externalize must arm the filing')
        self.assertEqual(filed, [],
            'nothing may be filed inside the save window')

    def test_deferred_filing_files_only_unbacked_editable_dats(self) -> None:
        self._scopeToSandbox()
        self.embody.par.Tdxndatsafety.val = 'externalize'
        filed = []
        self._stub('_externalizeDATs',
                   lambda dats: filed.extend(dats) or len(dats))
        # A test run holds _suppressDialogs True; the fire-time gate is
        # exercised in the re-arm test below.
        self._stub('_suppressDialogs', lambda: False)
        txt = self._text('safety_text')
        cb = self.sandbox.create(chopexecuteDAT, 'safety_callback')
        cb.text = '# user-authored callback\n'
        tbl, nul, sel, locked = self._generatedSet()
        anim = self.sandbox.create(animationCOMP, 'safety_anim')
        excluded = self._text('safety_excluded', 'runtime rows')
        excluded.tags.add(
            str(self.embody.par.Tdxnexcludetag.eval()).strip() + ':dat_content')
        self.embody_ext._fileUnbackedDATs(0)
        paths = {d.path for d in filed}
        for d in (txt, cb, tbl):
            self.assertIn(d.path, paths, f'{d.path} is an unbacked candidate')
        # locked: a locked select DAT is editable, but Embody cannot tag a
        # select DAT (supported_dat_types), so it is never a candidate.
        for d in (nul, sel, locked, excluded):
            self.assertNotIn(d.path, paths, f'{d.path} must never be filed')
        self.assertFalse(
            [p for p in paths if p.startswith(anim.path + '/')],
            'animationCOMP tables stay in the .tdxn')

    def test_externalize_skips_a_refused_tag(self) -> None:
        """applyTagToOperator returns False when it refuses a DAT: nothing
        may be written or counted (issue #109 review)."""
        dat = self._text('safety_refused')
        written = []
        self._stub('applyTagToOperator', lambda d, t: False)
        self._stub('externalizeImmediate', lambda d: written.append(d.path))
        self.assertEqual(self.embody_ext._externalizeDATs([dat]), 0)
        self.assertEqual(written, [])

    def test_deferred_filing_run_string_reaches_the_filer(self) -> None:
        """The filing is a string run() resolved by path at fire time; a typo
        there is a silent runtime break, so compile it, prove it lands past
        execute.py's 120-frame _suppress_dialogs clear, and exec it."""
        ext = self.embody_ext
        self.assertGreater(ext._UNBACKED_FILING_DELAY_FRAMES, 120)
        g = type(ext)._scheduleUnbackedFiling.__globals__
        had_run, orig_run = 'run' in g, g.get('run')
        calls = []
        g['run'] = lambda code, **kwargs: calls.append((code, kwargs))
        try:
            ext._scheduleUnbackedFiling(2)
        finally:
            if had_run:
                g['run'] = orig_run
            else:
                del g['run']
        self.assertEqual(len(calls), 1, 'one run() armed')
        code, kwargs = calls[0]
        self.assertGreater(kwargs.get('delayFrames', 0), 120)
        compile(code, '<unbacked filing>', 'exec')
        fired = []
        self._stub('_fileUnbackedDATs', lambda attempt=0: fired.append(attempt))
        exec(code, {'op': op})
        self.assertEqual(fired, [2], 'the string reaches _fileUnbackedDATs')

    def test_deferred_filing_rearms_while_dialogs_suppressed(self) -> None:
        scheduled = []
        self._stub('_scheduleUnbackedFiling',
                   lambda attempt=0: scheduled.append(attempt))
        self._stub('_externalizeDATs', lambda dats: self.fail(
            'filed while dialogs were suppressed'))
        self._stub('_suppressDialogs', lambda: True)
        self.embody.par.Tdxndatsafety.val = 'externalize'
        self.embody_ext._fileUnbackedDATs(3)
        self.assertEqual(scheduled, [4], 'a suppressed fire re-arms')
        mark = self._mark()
        self.embody_ext._fileUnbackedDATs(
            self.embody_ext._UNBACKED_FILING_MAX_REARMS)
        self.assertEqual(scheduled, [4], 'the cap stops re-arming')
        self.assertTrue(
            [e for e in self._logsSince(mark)
             if e.get('level') == 'WARNING'
             and 'externalize' in e.get('message', '')],
            'giving up must be a WARNING')

    # ------------------------------------------------------------------
    # D. DAT filters and the at-risk tripwire
    # ------------------------------------------------------------------

    def test_TD_MANAGED_DAT_TYPES_membership(self) -> None:
        """The denylist must include the types that triggered the user's
        original noise (info, webrtc, folder, monitors, devices) AND must
        NOT include any callback DAT type -- callbacks hold user-authored
        Python and losing them silently is exactly what the warning
        exists to prevent."""
        types = self.embody_ext._TD_MANAGED_DAT_TYPES
        # Read-only TD-generated outputs that must be skipped
        for t in ('info', 'webrtc', 'folder', 'monitors',
                  'audiodevices', 'videodevices', 'serialdevices',
                  'mididevices', 'mtouchin'):
            self.assertIn(t, types,
                f'TD-managed DAT type {t!r} missing from skip set')
        # 'multitouchin' never matched: TD's type string is 'mtouchin'
        # (issue #109). Checked without creating the op -- Multi Touch In
        # is not supported on macOS.
        self.assertNotIn('multitouchin', types)
        # Callback DAT types that must NEVER be skipped
        for t in ('execute', 'parexec', 'pargroupexec', 'chopexec',
                  'datexec', 'opexec', 'panelexec'):
            self.assertNotIn(t, types,
                f'Callback DAT type {t!r} must NOT be in skip set -- '
                f'callbacks hold user-authored Python')
        # Common user-authored types that must never be skipped
        for t in ('text', 'table'):
            self.assertNotIn(t, types,
                f'User-authored type {t!r} must NOT be in skip set')

    def test_findUnbackedDATs_ignores_td_managed_folder_dat(self) -> None:
        """Functional end-to-end: a folderDAT with real rows (TD-managed
        content) is never an externalize candidate even though it has
        non-empty content."""
        self._scopeToSandbox()
        folder_dat = self.sandbox.create(folderDAT, 'mgr_folder')
        folder_dat.par.folder = project.folder
        folder_dat.cook(force=True)
        # Sanity: must have rows, otherwise the empty-content skip
        # would short-circuit before the type filter runs and the
        # test would pass for the wrong reason.
        self.assertGreater(folder_dat.numRows, 0,
            f'Test setup: folder DAT must have rows '
            f'(got {folder_dat.numRows})')
        flat = self._flatten_dats(self.embody_ext._findUnbackedDATs())
        self.assertNotIn(folder_dat.path, flat,
            'TD-managed folder DAT with content was listed as unbacked')

    def test_findUnbackedDATs_keeps_callback_dats(self) -> None:
        """Callback DATs (executeDAT family) hold user-authored Python and
        stay externalize candidates."""
        self._scopeToSandbox()
        cb_dat = self.sandbox.create(chopexecuteDAT, 'safety_callback')
        cb_dat.text = (
            '# user-authored callback\n'
            'def onValueChange(channel, sampleIndex, val, prev):\n'
            '\tpass\n'
        )
        flat = self._flatten_dats(self.embody_ext._findUnbackedDATs())
        self.assertIn(cb_dat.path, flat,
            'chopexecuteDAT with user-authored callback content must be '
            'an unbacked candidate')

    def test_findUnbackedDATs_flags_plain_text_dat(self) -> None:
        """Baseline: a user-authored textDAT with content is listed.
        Confirms the filters did not break the happy path."""
        self._scopeToSandbox()
        text_dat = self._text('safety_text')
        flat = self._flatten_dats(self.embody_ext._findUnbackedDATs())
        self.assertIn(text_dat.path, flat,
            'Plain textDAT with content must be an unbacked candidate')

    def test_unbacked_dats_exclude_generated_dats(self) -> None:
        """isEditable is the filter: a wired null and a select are TD
        output (dat_read_only on export). A LOCKED select is editable, but
        Embody cannot tag a select DAT, so it is not a candidate either."""
        self._scopeToSandbox()
        tbl, nul, sel, locked = self._generatedSet()
        flat = self._flatten_dats(self.embody_ext._findUnbackedDATs())
        self.assertIn(tbl.path, flat)
        self.assertNotIn(locked.path, flat,
                         'a locked select is editable but untaggable')
        self.assertNotIn(nul.path, flat, 'a wired null DAT is generated')
        self.assertNotIn(sel.path, flat, 'a select DAT is generated')

    def test_unbacked_dats_exclude_animation_comp_tables(self) -> None:
        self._scopeToSandbox()
        anim = self.sandbox.create(animationCOMP, 'safety_anim')
        loose = self._text('safety_loose')
        flat = self._flatten_dats(self.embody_ext._findUnbackedDATs())
        self.assertIn(loose.path, flat, 'positive control: a loose text DAT')
        for name in ('keys', 'graph', 'channels', 'attributes'):
            self.assertNotIn(f'{anim.path}/{name}', flat,
                f'animationCOMP {name} table must stay in the .tdxn')

    def test_unbacked_dats_exclude_tox_tagged_descendants(self) -> None:
        self._scopeToSandbox()
        tox_tag = self.embody.par.Toxtag.val
        toxchild = self.sandbox.create(baseCOMP, 'safety_toxchild')
        inside = self._text('inside', parent=toxchild)
        sibling = self._text('safety_sibling')
        toxchild.tags.add(tox_tag)
        try:
            flat = self._flatten_dats(self.embody_ext._findUnbackedDATs())
            self.assertIn(sibling.path, flat, 'positive control')
            self.assertNotIn(inside.path, flat,
                'content under a TOX-tagged COMP lives in its .tox')
        finally:
            toxchild.tags.discard(tox_tag)

    def test_unbacked_dats_exclude_clone_interiors(self) -> None:
        """TD regenerates a clone's interior from its master: filing it
        would churn files for content the clone does not own."""
        self._scopeToSandbox()
        master = self.sandbox.create(baseCOMP, 'safety_master')
        m_txt = self._text('mtxt', 'master text', parent=master)
        clone = self.sandbox.create(baseCOMP, 'safety_clone')
        clone.par.enablecloning = True
        clone.par.clone = 'safety_master'  # sibling name, never a /path
        clone.cook(force=True)
        clone_dats = clone.findChildren(type=DAT)
        if not clone_dats:
            self.skipTest('cloning did not populate the clone in this build')
        flat = self._flatten_dats(self.embody_ext._findUnbackedDATs())
        self.assertIn(m_txt.path, flat, 'positive control: the master DAT')
        for d in clone_dats:
            self.assertNotIn(d.path, flat, f'{d.path} is a clone interior')

    def test_every_content_disposition_keeps_the_content(self) -> None:
        """The export and the externalize candidates read one rule,
        TDXNExt._datContentDisposition. Every value it can return keeps the
        content somewhere, is regenerated by TD, or was opted out -- pinned
        here statically, replacing a per-save walk that could never fire
        (issue #109 review)."""
        import ast
        src = self.embody.op('TDXNExt').text
        fn = next(n for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.FunctionDef)
                  and n.name == '_datContentDisposition')
        values = []
        for node in ast.walk(fn):
            if not isinstance(node, ast.Return):
                continue
            exprs = ([node.value.body, node.value.orelse]
                     if isinstance(node.value, ast.IfExp) else [node.value])
            for expr in exprs:
                self.assertIsInstance(
                    expr, ast.Constant,
                    f'non-literal disposition at line {node.lineno}')
                values.append(expr.value)
        self.assertGreaterEqual(len(values), 4,
                                'positive control: every return was read')
        self.assertLessEqual(
            set(values), {'embedded', 'backed', 'generated', 'excluded'})

    # ------------------------------------------------------------------
    # E. Reporter step 4 (issue #109), end to end
    # ------------------------------------------------------------------

    def _exportedOps(self, root: COMP) -> dict:
        res = self.embody.ext.TDXN.ExportNetwork(
            root_path=root.path, output_file=None, interactive=False)
        self.assertTrue(res.get('success'), res)
        flat = {}

        def walk(ops: Optional[list], prefix: str) -> None:
            for o in ops or []:
                key = prefix + o.get('name', '')
                flat[key] = o
                walk(o.get('children'), key + '/')

        walk(res['tdn'].get('operators'), '')
        return flat

    def test_reporter_step4_ask_save_keeps_authored_content(self) -> None:
        """Tdxndatsafety='ask', Embed DATs off, first-ever capture: the
        check runs, then the export -- keyframe tables and unbacked text
        carry dat_content, generated DATs carry dat_read_only, and nothing
        prompts or claims a DAT was skipped."""
        self._scopeToSandbox()
        self.sandbox.create(animationCOMP, 'animation1')
        self._text('notes', 'authored text')
        src = self.sandbox.create(tableDAT, 'src')
        src.clear()
        src.appendRow(['x', 'y'])
        src.appendRow(['8000', '1'])
        wired = self.sandbox.create(nullDAT, 'wired')
        wired.inputConnectors[0].connect(src)
        selected = self.sandbox.create(selectDAT, 'selected')
        selected.par.dat = 'src'
        for d in self.sandbox.findChildren(type=DAT):
            d.cook(force=True)
        mark = self._mark()
        self.embody_ext._checkTDXNContentSafety()
        ops = self._exportedOps(self.sandbox)
        for name in ('animation1/keys', 'animation1/graph', 'notes', 'src'):
            self.assertIn(name, ops, f'{name} missing from the export')
            self.assertIn('dat_content', ops[name],
                f'{name} content must be in the .tdxn')
        for name in ('wired', 'selected'):
            self.assertTrue(ops[name].get('dat_read_only'),
                f'{name} is generated: dat_read_only, TD recreates it')
            self.assertNotIn('dat_content', ops[name])
        self.assertEqual(self._captured, [], 'no dialog may open')
        # Today's DAT-loss line ('TDXN export will not keep N DAT(s)') and
        # the retired ones ('at-risk DAT', 'Skipped externalization').
        claimed = [e['message'] for e in self._logsSince(mark)
                   if (e.get('level') in ('WARNING', 'SUCCESS')
                       and 'DAT(s)' in e.get('message', ''))
                   or 'at-risk DAT' in e.get('message', '')
                   or 'Skipped externalization' in e.get('message', '')]
        self.assertEqual(claimed, [], 'no DAT-loss line may be logged')

    # ------------------------------------------------------------------
    # F. Save-window hardening
    # ------------------------------------------------------------------

    def test_content_check_failure_cannot_skip_phase1(self) -> None:
        """A bug in the content check logs ERROR and the pre-save export
        still runs for every TDXN COMP (issue #109). Each save also starts
        a fresh palette WARNING set, so its job record names every clone."""
        embody = self.embody
        ext_class = type(self.embody_ext)
        tdxn_class = type(self.embody.ext.TDXN)
        orig_mode = embody.par.Tdxnmode.eval()
        orig_update = ext_class.Update
        orig_get = ext_class._getTDXNStrategyComps
        orig_check = ext_class._checkTDXNContentSafety
        orig_export = tdxn_class.ExportNetwork
        orig_read = tdxn_class.__dict__['_read_existing_tdxn']
        orig_equal = tdxn_class.__dict__['_tdxn_content_equal']
        comp = self.sandbox.create(baseCOMP, 'safety_phase1')
        comp.create(baseCOMP, 'placeholder')  # Phase 1 skips empty COMPs
        exported = []

        def boom(self_: Any) -> None:
            raise RuntimeError('simulated content-check bug')

        def fake_export(self_: Any, root_path: Optional[str] = None,
                        output_file: Optional[str] = None,
                        **kwargs: Any) -> dict:
            exported.append(root_path)
            return {'success': True, 'tdn': {'version': '2.1'}}

        embody.par.Tdxnmode = 'export'   # no strip: Phase 1 only
        ext_class.Update = lambda self_, *a, **k: None
        ext_class._getTDXNStrategyComps = (
            lambda self_: [(comp.path, 'fake/safety_phase1.tdxn')])
        ext_class._checkTDXNContentSafety = boom
        tdxn_class.ExportNetwork = fake_export
        tdxn_class._read_existing_tdxn = staticmethod(lambda path: {'x': 1})
        tdxn_class._tdxn_content_equal = staticmethod(lambda a, b: True)
        sentinel = self.sandbox.path + '/palette_warned_last_save'
        self.embody.ext.TDXN._palette_unanswered_warned.add(sentinel)
        self.embody.ext.TDXN._palette_unanswered_pending.append(sentinel)
        self.embody.ext.TDXN._companion_drop_logged.add((sentinel, 'INFO'))
        try:
            mark = self._mark()
            embody.op('execute').module._runPreSaveExternalization()
            self.assertNotIn(sentinel,
                self.embody.ext.TDXN._palette_unanswered_warned,
                'the palette WARNING set is cleared once per save')
            self.assertNotIn(sentinel,
                self.embody.ext.TDXN._palette_unanswered_pending,
                'the pending palette list is cleared once per save')
            self.assertNotIn((sentinel, 'INFO'),
                self.embody.ext.TDXN._companion_drop_logged,
                'the companion-drop marks are cleared once per save')
            self.assertEqual(exported, [comp.path],
                'Phase 1 must still export after a content-check error')
            errors = [e for e in self._logsSince(mark)
                      if e.get('level') == 'ERROR'
                      and 'content check failed' in e.get('message', '')]
            self.assertTrue(errors, 'the failure must be logged at ERROR')
        finally:
            ext_class.Update = orig_update
            ext_class._getTDXNStrategyComps = orig_get
            ext_class._checkTDXNContentSafety = orig_check
            tdxn_class.ExportNetwork = orig_export
            tdxn_class._read_existing_tdxn = orig_read
            tdxn_class._tdxn_content_equal = orig_equal
            embody.par.Tdxnmode = orig_mode
            self.embody.ext.TDXN._palette_unanswered_warned.discard(sentinel)
            if sentinel in self.embody.ext.TDXN._palette_unanswered_pending:
                self.embody.ext.TDXN._palette_unanswered_pending.remove(sentinel)
            self.embody.ext.TDXN._companion_drop_logged.discard((sentinel, 'INFO'))

    def test_unanswered_palette_prompt_warns(self) -> None:
        """A suppressed palette prompt (-1) blackboxes the clone: that can
        drop a customized interior, so it logs a WARNING. Outside a save:
        once per clone, DEBUG after (inside a save, see the next test)."""
        target = self.sandbox.create(baseCOMP, 'safety_palette')
        tdxn = self.embody.ext.TDXN
        tdxn._palette_unanswered_warned.discard(target.path)
        self._scripted_choice = -1
        had_flag = self.embody.fetch('_suppress_dialogs', None, search=False)
        self.embody.unstore('_suppress_dialogs')  # the outside-a-save path

        def warnings_since(mark: int) -> list:
            return [e for e in self._logsSince(mark)
                    if e.get('level') == 'WARNING'
                    and target.path in e.get('message', '')
                    and 'Tdxnpalettehandling' in e.get('message', '')]

        try:
            mark = self._mark()
            handling = tdxn._promptPaletteHandling(target)
            self.assertEqual(handling, 'blackbox')
            self.assertEqual(len(self._captured), 1, 'the prompt was attempted')
            self.assertTrue(warnings_since(mark),
                'an unanswered prompt must warn')
            self.assertIsNone(
                target.fetch('_tdn_palette_handling', None, search=False),
                'an unanswered prompt must not persist a choice')
            mark = self._mark()
            self.assertEqual(tdxn._promptPaletteHandling(target), 'blackbox')
            self.assertEqual(warnings_since(mark), [],
                'a repeat for the same clone logs DEBUG, not WARNING')
        finally:
            tdxn._palette_unanswered_warned.discard(target.path)
            if had_flag is not None:
                self.embody.store('_suppress_dialogs', had_flag)

    def test_unanswered_palettes_in_a_save_log_one_warning(self) -> None:
        """Inside a save every unanswered clone is collected and the save
        logs ONE WARNING naming them all, not one per clone per save
        (issue #109 review)."""
        tdxn = self.embody.ext.TDXN
        targets = [self.sandbox.create(baseCOMP, f'safety_pal{i}')
                   for i in range(3)]
        self._scripted_choice = -1
        had_flag = self.embody.fetch('_suppress_dialogs', None, search=False)
        saved = list(tdxn._palette_unanswered_pending)
        tdxn._palette_unanswered_pending.clear()
        self.embody.store('_suppress_dialogs', True)
        try:
            mark = self._mark()
            for t in targets:
                self.assertEqual(tdxn._promptPaletteHandling(t), 'blackbox')
            self.assertEqual(
                [e for e in self._logsSince(mark)
                 if e.get('level') == 'WARNING'
                 and 'Palette handling' in e.get('message', '')],
                [], 'no per-clone WARNING inside a save')
            mark = self._mark()
            tdxn.flushPaletteUnanswered()
            combined = [e for e in self._logsSince(mark)
                        if e.get('level') == 'WARNING'
                        and 'Tdxnpalettehandling' in e.get('message', '')]
            self.assertEqual(len(combined), 1, 'one WARNING for the save')
            for t in targets:
                self.assertIn(t.path, combined[0]['message'])
            self.assertEqual(tdxn._palette_unanswered_pending, [])
        finally:
            tdxn._palette_unanswered_pending[:] = saved
            if had_flag is None:
                self.embody.unstore('_suppress_dialogs')
            else:
                self.embody.store('_suppress_dialogs', had_flag)

    def test_unanswered_duplicate_batch_prompt_warns(self) -> None:
        """The batch prompt falls through to auto-resolve on -1, which
        re-tags operators -- never silently."""
        self._scripted_choice = -1
        mark = self._mark()
        choice = self.embody_ext._promptForBatchResolution(
            [('embody/safety/a.py', []), ('embody/safety/b.py', [])])
        self.assertEqual(choice, 'auto')
        self.assertTrue(
            [e for e in self._logsSince(mark)
             if e.get('level') == 'WARNING'
             and 'Duplicate Paths Detected' in e.get('message', '')],
            'an unanswered batch prompt must warn')

    def _forgetCompanionLogs(self, *paths: str) -> None:
        """Clear the once-per-(path, level) marks these companions left on
        the live TDXNExt, so a rerun in the same session logs again."""
        logged = self.embody.ext.TDXN._companion_drop_logged
        logged.difference_update({k for k in logged if k[0] in paths})

    def _companionLines(self, mark: int, name: str) -> list:
        return [(e.get('level'), e.get('message', ''))
                for e in self._logsSince(mark)
                if e.get('message', '').startswith('Skipping duplicate companion')
                and name in e.get('message', '')]

    def test_dropped_duplicate_companion_with_content_warns(self) -> None:
        """The duplicate-companion drop is unchanged, but a dropped DAT
        holding content no file keeps -- and the original does not share --
        is named at WARNING, once per path; an empty one stays INFO."""
        host = self.sandbox.create(nullCHOP, 'hostop')
        orig = self._text('hostop_notes', 'original')
        dup = self._text('hostop_notes1', 'authored in the duplicate')
        orig.dock = host
        dup.dock = host
        host2 = self.sandbox.create(nullCHOP, 'hostop2')
        orig2 = self._text('hostop2_notes', 'original')
        empty_dup = self._text('hostop2_notes1', '')
        orig2.dock = host2
        empty_dup.dock = host2
        self._forgetCompanionLogs(dup.path, empty_dup.path)
        try:
            mark = self._mark()
            ops = self._exportedOps(self.sandbox)
            self.assertNotIn('hostop_notes1', ops,
                             'the drop itself is unchanged')
            new = self._logsSince(mark)
            self.assertTrue(
                [e for e in new if e.get('level') == 'WARNING'
                 and dup.path in e.get('message', '')],
                'a dropped companion holding content must warn')
            self.assertFalse(
                [e for e in new if e.get('level') == 'WARNING'
                 and 'hostop2_notes1' in e.get('message', '')],
                'an empty dropped companion stays INFO')
            mark = self._mark()
            self._exportedOps(self.sandbox)
            self.assertEqual(self._companionLines(mark, 'hostop_notes1'), [],
                'every read_tdxn/diff_tdxn/checkpoint exports: once per path')
        finally:
            self._forgetCompanionLogs(dup.path, empty_dup.path)

    def test_dropped_identical_companion_copy_stays_info(self) -> None:
        """A byte-identical copy of the original (what a re-import leaves
        behind) loses nothing -- the original is in the .tdxn: INFO."""
        host = self.sandbox.create(nullCHOP, 'copyhost')
        orig = self._text('copyhost_notes', 'same authored text')
        dup = self._text('copyhost_notes1', 'same authored text')
        orig.dock = host
        dup.dock = host
        self._forgetCompanionLogs(dup.path)
        try:
            mark = self._mark()
            ops = self._exportedOps(self.sandbox)
            self.assertNotIn('copyhost_notes1', ops)
            self.assertIn('copyhost_notes', ops, 'the original is exported')
            self.assertEqual(
                [level for level, _ in
                 self._companionLines(mark, 'copyhost_notes1')], ['INFO'])
        finally:
            self._forgetCompanionLogs(dup.path)

    def test_notable_ring_holds_only_warnings_and_errors(self) -> None:
        """save_project reads the save's warnings from a WARNING/ERROR ring
        (EnvoyExt._save_warnings), so a Roundtrip save's DEBUG churn through
        the 200-entry full ring cannot evict the storage report first."""
        ext = self.embody_ext
        ext.Log('i109 ring probe debug', 'DEBUG')
        ext.Log('i109 ring probe warning', 'WARNING')
        warn_entry = ext._log_buffer[-1]
        ext.Log('i109 ring probe info', 'INFO')
        ext.Log('i109 ring probe error', 'ERROR')
        err_entry = ext._log_buffer[-1]
        ring = list(ext._notable_log_buffer)
        self.assertIs(ring[-1], err_entry, 'ERROR lands in the ring')
        self.assertIs(ring[-2], warn_entry,
            'WARNING lands in the ring: the same dict and id as the full ring')
        self.assertFalse([e for e in ring
                          if e.get('message', '').startswith('i109 ring probe')
                          and e.get('level') in ('DEBUG', 'INFO')],
            'DEBUG/INFO never enter the ring')
