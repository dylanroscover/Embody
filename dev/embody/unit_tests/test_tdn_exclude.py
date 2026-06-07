"""
Test suite: tdn_exclude tag - making a COMP (and its whole subtree)
invisible to the TDN system.

A COMP whose tags include the exclude tag (default 'tdn_exclude') is
transparent to TDN: never exported (no inline entry, no tdn_ref/tox_ref),
not stripped on save, and not destroyed/recreated by reconstruction's
clear_first pass. The owning application owns its lifecycle.

Covers:
  A. _hasExcludeTag detection (COMP-only, tag presence, empty-tag guard)
  B. getTags never leaks the exclude tag into any selector
  C. Export skips the excluded subtree entirely (no entry, no ref)
  D. _collectAllPaths skips the excluded subtree
  E. Reconstruction (clear_first=True) preserves the excluded subtree
  F. StripCompChildren preserves the excluded COMP
  G. Removing the tag restores normal export (acceptance #4)
  H. Exclusion holds regardless of cascade/embed_all (acceptance #5)
"""

import json

try:
    runner_mod = op.unit_tests.op('TestRunnerExt').module
    EmbodyTestCase = runner_mod.EmbodyTestCase
except (AttributeError, NameError):
    pass


class TestTDNExclude(EmbodyTestCase):

    @property
    def tdn_ext(self):
        """Resolve TDNExt live on every access (never cache - reinit-safe)."""
        return self.embody.ext.TDN

    @property
    def exclude_tag(self):
        return self.embody.par.Tdnexcludetag.val

    # ------------------------------------------------------------------
    # Fixture: parent -> {keep, drop[excluded] -> inside}
    # ------------------------------------------------------------------

    def _build(self, name='exc_parent'):
        parent = self.sandbox.create(baseCOMP, name)
        keep = parent.create(baseCOMP, 'keep')
        drop = parent.create(baseCOMP, 'drop')
        drop.tags.add(self.exclude_tag)
        inside = drop.create(baseCOMP, 'inside')
        return parent, keep, drop, inside

    def _export_doc(self, parent, embed_all=False):
        result = self.tdn_ext.ExportNetwork(
            parent.path, output_file=None, embed_all=embed_all)
        return result.get('tdn', result)

    @staticmethod
    def _op_names(doc):
        return [o.get('name') for o in doc.get('operators', [])]

    # ------------------------------------------------------------------
    # A. _hasExcludeTag detection
    # ------------------------------------------------------------------

    def test_hasExcludeTag_true_for_tagged_comp(self):
        c = self.sandbox.create(baseCOMP, 'tagged')
        c.tags.add(self.exclude_tag)
        self.assertTrue(self.tdn_ext._hasExcludeTag(c))

    def test_hasExcludeTag_false_for_untagged_comp(self):
        c = self.sandbox.create(baseCOMP, 'plain')
        self.assertFalse(self.tdn_ext._hasExcludeTag(c))

    def test_hasExcludeTag_false_for_non_comp(self):
        d = self.sandbox.create(textDAT, 'plain_dat')
        d.tags.add(self.exclude_tag)
        self.assertFalse(self.tdn_ext._hasExcludeTag(d),
            'Only COMPs can be excluded; a DAT must return False')

    def test_hasExcludeTag_guards_empty_tag_name(self):
        """If the exclude tag name is blank, nothing must be excluded -
        an empty tag must never match every operator."""
        c = self.sandbox.create(baseCOMP, 'guard')
        prev = self.embody.par.Tdnexcludetag.val
        self.embody.par.Tdnexcludetag.val = ''
        try:
            self.assertFalse(self.tdn_ext._hasExcludeTag(c),
                'Empty exclude-tag name must not exclude operators')
        finally:
            self.embody.par.Tdnexcludetag.val = prev

    # ------------------------------------------------------------------
    # B. getTags never leaks the exclude tag
    # ------------------------------------------------------------------

    def test_getTags_excludes_exclude_tag_from_all_selectors(self):
        tag = self.exclude_tag
        self.assertNotIn(tag, self.embody_ext.getTags(),
            'Exclude tag must not appear in getTags() (all)')
        self.assertNotIn(tag, self.embody_ext.getTags('DAT'),
            'Exclude tag must not appear in the DAT selector - it would '
            'wrongly drive DAT externalization')
        self.assertNotIn(tag, self.embody_ext.getTags('comp'))
        self.assertNotIn(tag, self.embody_ext.getTags('tdn'))
        self.assertNotIn(tag, self.embody_ext.getTags('tox'))

    # ------------------------------------------------------------------
    # C. Export skips the excluded subtree (no entry, no ref)
    # ------------------------------------------------------------------

    def test_export_omits_excluded_comp(self):
        parent, keep, drop, inside = self._build()
        doc = self._export_doc(parent)
        names = self._op_names(doc)
        self.assertIn('keep', names, 'Normal child must be exported')
        self.assertNotIn('drop', names,
            'Excluded COMP must not appear in the export')

    def test_export_emits_no_ref_for_excluded(self):
        parent, keep, drop, inside = self._build()
        doc = self._export_doc(parent)
        blob = json.dumps(doc)
        self.assertNotIn('drop', blob,
            'No trace of the excluded COMP (inline, tdn_ref, or tox_ref) '
            'may appear anywhere in the export')
        self.assertNotIn('inside', blob,
            'The excluded subtree must be fully absent from the export')
        self.assertNotIn('tdn_ref', blob,
            'Exclusion must not emit a tdn_ref (unlike the .tdn tag)')

    # ------------------------------------------------------------------
    # D. _collectAllPaths skips the excluded subtree
    # ------------------------------------------------------------------

    def test_collectAllPaths_skips_excluded_subtree(self):
        parent, keep, drop, inside = self._build()
        paths = self.tdn_ext._collectAllPaths(parent)
        self.assertIn(keep.path, paths)
        self.assertNotIn(drop.path, paths,
            'Excluded COMP must not be collected')
        self.assertNotIn(inside.path, paths,
            'Descendants of an excluded COMP must not be collected')

    # ------------------------------------------------------------------
    # E. Reconstruction (clear_first=True) preserves the excluded subtree
    # ------------------------------------------------------------------

    def test_reconstruct_preserves_excluded_subtree(self):
        parent, keep, drop, inside = self._build()
        doc = self._export_doc(parent)
        # Reconstruct with clear_first=True - the destroy pass must NOT
        # touch the excluded COMP (it isn't in the doc, so destroying it
        # would be permanent loss).
        self.tdn_ext.ImportNetwork(
            target_path=parent.path, tdn=doc, clear_first=True)
        self.assertIsNotNone(op(parent.path + '/drop'),
            'Excluded COMP must survive clear_first reconstruction')
        self.assertIsNotNone(op(parent.path + '/drop/inside'),
            'Excluded COMP subtree must survive intact')
        self.assertIsNotNone(op(parent.path + '/keep'),
            'Normal child must be rebuilt from the doc')

    def test_reconstruct_does_not_duplicate_excluded(self):
        """The preserved COMP must not also be recreated (no duplicate)."""
        parent, keep, drop, inside = self._build()
        doc = self._export_doc(parent)
        self.tdn_ext.ImportNetwork(
            target_path=parent.path, tdn=doc, clear_first=True)
        drops = [c for c in parent.children if c.name == 'drop']
        self.assertEqual(len(drops), 1,
            f'Exactly one excluded COMP expected, found {len(drops)}')

    # ------------------------------------------------------------------
    # F. StripCompChildren preserves the excluded COMP
    # ------------------------------------------------------------------

    def test_strip_preserves_excluded_comp(self):
        parent, keep, drop, inside = self._build()
        self.embody_ext.StripCompChildren(parent)
        self.assertIsNotNone(op(parent.path + '/drop'),
            'Excluded COMP must survive the save-time strip pass')
        self.assertIsNone(op(parent.path + '/keep'),
            'Normal child must be stripped')

    # ------------------------------------------------------------------
    # G. Removing the tag restores normal export (acceptance #4)
    # ------------------------------------------------------------------

    def test_tag_removal_restores_export(self):
        parent, keep, drop, inside = self._build()
        # Excluded first
        self.assertNotIn('drop', self._op_names(self._export_doc(parent)))
        # Remove the tag - COMP returns to normal cascade behaviour
        drop.tags.remove(self.exclude_tag)
        names = self._op_names(self._export_doc(parent))
        self.assertIn('drop', names,
            'After tag removal the COMP must be exported normally')

    # ------------------------------------------------------------------
    # H. Exclusion holds regardless of cascade/embed_all (acceptance #5)
    # ------------------------------------------------------------------

    def test_exclusion_holds_with_embed_all(self):
        parent, keep, drop, inside = self._build()
        for embed in (False, True):
            doc = self._export_doc(parent, embed_all=embed)
            self.assertNotIn('drop', self._op_names(doc),
                f'Exclusion must win with embed_all={embed}')
            self.assertNotIn('inside', json.dumps(doc),
                f'Excluded subtree must stay absent with embed_all={embed}')

    def test_exclusion_holds_regardless_of_cascade_toggle(self):
        parent, keep, drop, inside = self._build()
        prev = self.embody.par.Tdncascade.eval()
        try:
            for cascade in (False, True):
                self.embody.par.Tdncascade.val = cascade
                doc = self._export_doc(parent)
                self.assertNotIn('drop', self._op_names(doc),
                    f'Exclusion must win with Tdncascade={cascade}')
        finally:
            self.embody.par.Tdncascade.val = prev

    # ------------------------------------------------------------------
    # I. Cascade auto-tagging must skip excluded children (the real path)
    # ------------------------------------------------------------------

    def test_cascade_autotag_skips_excluded(self):
        """The real cascade path (_cascadeTDNTag) must not tag an excluded
        child. Stub applyTagToOperator to capture targets without the file/
        table side effects of a real externalization."""
        parent = self.sandbox.create(baseCOMP, 'casc_parent')
        keep = parent.create(baseCOMP, 'casc_keep')
        drop = parent.create(baseCOMP, 'casc_drop')
        drop.tags.add(self.exclude_tag)
        calls = []
        ext = self.embody_ext
        orig = ext.applyTagToOperator
        ext.applyTagToOperator = lambda o, t: calls.append(o.path)
        try:
            ext._cascadeTDNTag(parent)
        finally:
            ext.applyTagToOperator = orig
        self.assertIn(keep.path, calls,
            'Normal child must be cascade-tagged')
        self.assertNotIn(drop.path, calls,
            'Excluded child must NOT be cascade auto-tagged (acceptance #1/#5)')

    # ------------------------------------------------------------------
    # J. Nested-under-normal warns AND is serialized (no silent data loss)
    # ------------------------------------------------------------------

    def test_nested_excluded_under_normal_warns_and_is_preserved(self):
        # Exclusion is honored only at a TDN boundary's DIRECT children,
        # because the strip/clear passes only preserve direct children. A
        # COMP tagged for exclusion but nested under a non-excluded child
        # cannot be preserved by those passes -- so if the export ALSO
        # skipped it, the .tdn would omit it while strip destroyed it:
        # permanent data loss. Instead the export serializes it as normal
        # content (with a warning) so it round-trips and survives.
        parent = self.sandbox.create(baseCOMP, 'warn_parent')
        normal = parent.create(baseCOMP, 'warn_normal')
        nested = normal.create(baseCOMP, 'warn_nested')
        nested.tags.add(self.exclude_tag)
        before = self.embody_ext._log_counter
        doc = self._export_doc(parent)
        new = [e for e in self.embody_ext._log_buffer
               if e['id'] > before]
        warns = [e for e in new if e.get('level') == 'WARNING'
                 and 'nested under' in e.get('message', '')]
        self.assertTrue(warns,
            'Excluded COMP nested under a non-excluded child must warn; '
            f'got: {[e.get("message", "") for e in new]}')
        # And -- critically -- it is now PRESENT in the export (serialized as
        # normal content) so strip/restore can round-trip it instead of
        # destroying it with no .tdn to rebuild from.
        names, _ = self._collect_all_refs(doc)
        self.assertIn('warn_nested', names,
            'A nested excluded COMP must be serialized (not dropped) so it '
            'survives the strip/restore cycle -- exclusion at depth>0 is a '
            'no-op, never silent data loss')

    # ------------------------------------------------------------------
    # K. Annotation COMPs are never excludable
    # ------------------------------------------------------------------

    def test_hasExcludeTag_false_for_annotation_comp(self):
        note = self.sandbox.create(annotateCOMP, 'note_excl')
        note.tags.add(self.exclude_tag)
        self.assertFalse(self.tdn_ext._hasExcludeTag(note),
            'Annotation COMPs must never be eligible for exclusion')

    # ------------------------------------------------------------------
    # L. getTags filters by parameter name, not value (no collision drop)
    # ------------------------------------------------------------------

    def test_getTags_collision_preserves_real_tag(self):
        prev = self.embody.par.Tdnexcludetag.val
        py = self.embody.par.Pytag.eval()
        self.embody.par.Tdnexcludetag.val = py  # collide exclude tag with 'py'
        try:
            self.assertIn(py, self.embody_ext.getTags('DAT'),
                'Naming the exclude tag identically to a real tag must not '
                'drop that real tag from the selector (filter by name).')
        finally:
            self.embody.par.Tdnexcludetag.val = prev

    # ------------------------------------------------------------------
    # M. Excluded children do not dirty the parent fingerprint (H)
    # ------------------------------------------------------------------

    def test_excluded_child_does_not_dirty_parent(self):
        parent = self.sandbox.create(baseCOMP, 'fp_parent')
        keep = parent.create(baseCOMP, 'fp_keep')
        drop = parent.create(baseCOMP, 'fp_drop')
        drop.tags.add(self.exclude_tag)
        try:
            self.embody_ext._storeTDNFingerprint(parent)
            # Mutating the excluded child must NOT dirty the parent.
            drop.nodeX += 100
            drop.create(baseCOMP, 'fp_inside_new')
            self.assertFalse(self.embody_ext._isTDNDirty(parent),
                'Changes inside an excluded child must not dirty the parent')
            # Control: mutating a normal child DOES dirty the parent.
            keep.nodeX += 100
            self.assertTrue(self.embody_ext._isTDNDirty(parent),
                'Changes to a normal child must dirty the parent')
        finally:
            self.embody_ext._tdn_fingerprints.pop(parent.path, None)

    # ------------------------------------------------------------------
    # N. Reconstruct leaves the excluded COMP truly unmodified
    # ------------------------------------------------------------------

    def test_excluded_comp_unmodified_by_reconstruct(self):
        parent, keep, drop, inside = self._build()
        drop.store('app_state', {'k': 1})
        drop.nodeX = 1234
        doc = self._export_doc(parent)
        self.tdn_ext.ImportNetwork(
            target_path=parent.path, tdn=doc, clear_first=True)
        d2 = op(parent.path + '/drop')
        self.assertIsNotNone(d2, 'excluded COMP must survive')
        self.assertEqual(d2.fetch('app_state', None, search=False), {'k': 1},
            'excluded COMP storage must be unmodified')
        self.assertEqual(d2.nodeX, 1234,
            'excluded COMP position must be unmodified')

    # ------------------------------------------------------------------
    # Structural assertion helper (replaces fragile json.dumps substring)
    # ------------------------------------------------------------------

    @staticmethod
    def _collect_all_refs(doc):
        """Recursively gather operator names and every reference string
        (tdn_ref/tox_ref/dock/inputs/connections) from a TDN doc."""
        names, refs = set(), set()

        def walk(ops):
            for o in ops:
                names.add(o.get('name'))
                for key in ('tdn_ref', 'tox_ref', 'dock'):
                    if o.get(key):
                        refs.add(str(o[key]))
                for inp in (o.get('inputs') or []):
                    refs.add(str(inp))
                for conn in (o.get('connections') or []):
                    refs.add(str(conn))
                walk(o.get('children') or [])
        walk(doc.get('operators', []))
        return names, refs

    def test_export_has_no_structural_reference_to_excluded(self):
        parent, keep, drop, inside = self._build()
        names, refs = self._collect_all_refs(self._export_doc(parent))
        self.assertIn('keep', names)
        self.assertNotIn('drop', names)
        self.assertNotIn('inside', names)
        for r in refs:
            self.assertNotIn('drop', r,
                f'Excluded COMP must not be referenced anywhere: {r}')
