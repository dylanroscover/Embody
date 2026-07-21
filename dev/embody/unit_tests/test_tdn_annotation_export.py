"""
TDN annotation export + build-header tests.

Covers two format fixes:

  A. Annotation COMPs (annotateCOMP) are captured ONLY in the dedicated
     `annotations:` section, never duplicated as a heavy `operators:` entry.
     Before the fix, an annotate child was double-serialized: once as a
     compact annotation and once as a full operator dumping ALL of its
     custom pars (every Opviewer*/Body*), adding ~180 lines of pure noise
     per annotation. Round-trip must still recreate the annotation from the
     `annotations:` section (import Phase 7a).

  B. The header omits `build` entirely when there is no build number
     (untracked / portable networks), rather than emitting `build: null`.
     Matches the format's omit-when-absent philosophy (position, size, etc.).
"""

try:
    runner_mod = op.unit_tests.op('TestRunnerExt').module
    EmbodyTestCase = runner_mod.EmbodyTestCase
except (AttributeError, NameError):
    pass


class TestTDNAnnotationExport(EmbodyTestCase):

    @property
    def tdn_ext(self):
        """Resolve TDNExt live on every access (never cache - reinit-safe)."""
        return self.embody.ext.TDN

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _export(self, root):
        result = self.tdn_ext.ExportNetwork(
            root.path, output_file=None, embed_all=True)
        return result.get('tdn', result)

    @staticmethod
    def _op_names(doc):
        return [o.get('name') for o in doc.get('operators', [])]

    @staticmethod
    def _op_types(doc):
        return [o.get('type') for o in doc.get('operators', [])]

    def _build_with_annotation(self, name='anno_parent'):
        parent = self.sandbox.create(baseCOMP, name)
        parent.create(noiseTOP, 'noise_src')
        note = parent.create(annotateCOMP, 'annotate1')
        note.par.Titletext = 'Plasma'
        note.par.Bodytext = 'interfering sine fields'
        return parent, note

    # ------------------------------------------------------------------
    # A. Annotation excluded from operators:, present in annotations:
    # ------------------------------------------------------------------

    def test_annotate_excluded_from_operators(self):
        parent, _ = self._build_with_annotation()
        doc = self._export(parent)
        self.assertNotIn('annotate1', self._op_names(doc),
            'annotateCOMP must NOT appear in the operators: list - it is '
            'captured exclusively by the annotations: section')
        self.assertNotIn('annotateCOMP', self._op_types(doc),
            'No operators: entry may be of type annotateCOMP')

    def test_non_annotation_ops_still_exported(self):
        """The fix only drops annotates - real ops stay in operators:."""
        parent, _ = self._build_with_annotation()
        doc = self._export(parent)
        self.assertIn('noise_src', self._op_names(doc),
            'Non-annotation operators must still be exported normally')

    def test_annotate_present_in_annotations_section(self):
        parent, _ = self._build_with_annotation()
        doc = self._export(parent)
        anns = doc.get('annotations', [])
        names = [a.get('name') for a in anns]
        self.assertIn('annotate1', names,
            'annotateCOMP must appear in the annotations: section')
        entry = next(a for a in anns if a.get('name') == 'annotate1')
        self.assertEqual(entry.get('title'), 'Plasma')
        self.assertEqual(entry.get('text'), 'interfering sine fields')

    def test_no_heavy_custom_pars_dumped_for_annotate(self):
        """Regression guard: the ~180-line custom_pars dump (Opviewer*,
        Body*, etc.) must be gone. If any operators: entry is an annotate,
        the bloat is back."""
        parent, _ = self._build_with_annotation()
        doc = self._export(parent)
        for o in doc.get('operators', []):
            self.assertNotEqual(o.get('type'), 'annotateCOMP',
                f'Annotate "{o.get("name")}" leaked back into operators: '
                f'with {len(o.get("custom_pars", []))} custom pars')

    # ------------------------------------------------------------------
    # A (round-trip). Annotation rebuilt from annotations: on import
    # ------------------------------------------------------------------

    def test_annotate_roundtrips_via_annotations(self):
        """Excluding annotates from operators: must not break round-trip -
        import recreates them from the annotations: section (Phase 7a)."""
        parent, _ = self._build_with_annotation()
        doc = self._export(parent)

        target = self.sandbox.create(baseCOMP, 'anno_import_target')
        result = self.tdn_ext.ImportNetwork(
            target.path, doc, clear_first=True)
        self.assertTrue(result.get('success'),
            f'import failed: {result.get("error")}')

        # Import recreates annotations as UTILITY annotateCOMPs (matching TD's
        # native behavior -- hidden from .op()/.children), so look them up via
        # findChildren(includeUtility=True), not target.op('annotate1').
        anns = target.findChildren(type=annotateCOMP, includeUtility=True)
        recreated = next((a for a in anns if a.name == 'annotate1'), None)
        self.assertIsNotNone(recreated,
            'Annotation must be recreated on import from annotations: section')
        self.assertEqual(recreated.type, 'annotate')
        self.assertEqual(recreated.par.Titletext.eval(), 'Plasma')
        self.assertEqual(recreated.par.Bodytext.eval(),
            'interfering sine fields')
        # And it must NOT be duplicated into a second op.
        self.assertEqual(recreated.par.Mode.eval(), 'annotate')

    # ------------------------------------------------------------------
    # A (durability). A destroyed annotation leaves no resurrection fuel
    # ------------------------------------------------------------------

    def test_destroyed_annotation_dropped_from_next_export(self):
        """_exportAnnotations reads LIVE state only: after the annotation is
        destroyed, the next export must drop its annotations: entry -- a
        stale entry is exactly what resurrected live-deleted annotations on
        the next parent reimport (2026-07-21 report, Bug 3)."""
        parent, note = self._build_with_annotation('anno_durability')
        doc1 = self._export(parent)
        self.assertIn('annotate1',
                      [a.get('name') for a in doc1.get('annotations', [])])
        note.destroy()
        doc2 = self._export(parent)
        self.assertNotIn('annotate1',
                         [a.get('name') for a in doc2.get('annotations', [])])

    def test_reimport_after_destroy_does_not_resurrect(self):
        """Round-trip the post-destroy export: importing it clear_first must
        not recreate the destroyed annotation."""
        parent, note = self._build_with_annotation('anno_no_resurrect')
        self._export(parent)
        note.destroy()
        doc = self._export(parent)
        result = self.tdn_ext.ImportNetwork(parent.path, doc, clear_first=True)
        self.assertTrue(result.get('success'),
                        f'import failed: {result.get("error")}')
        anns = parent.findChildren(type=annotateCOMP, includeUtility=True)
        self.assertEqual(len(anns), 0,
                         'destroyed annotation must stay gone after reimport')

    # ------------------------------------------------------------------
    # A (additive reimport). Utility annotations are reused, not duplicated
    # ------------------------------------------------------------------

    def test_additive_import_reuses_utility_annotation(self):
        """clear_first=False import must UPDATE an existing (utility=True)
        annotation by name, not create a duplicate -- bare parent.op()
        cannot see utility ops, so the reuse lookup must be utility-aware."""
        parent, note = self._build_with_annotation('anno_additive')
        note.utility = True  # every annotation is utility now
        doc = self._export(parent)
        result = self.tdn_ext.ImportNetwork(parent.path, doc,
                                            clear_first=False)
        self.assertTrue(result.get('success'),
                        f'import failed: {result.get("error")}')
        anns = parent.findChildren(type=annotateCOMP, includeUtility=True)
        self.assertEqual(
            len(anns), 1,
            'additive reimport must reuse the existing utility annotation, '
            'got {} annotations'.format(len(anns)))

    # ------------------------------------------------------------------
    # B. build: omitted when there is no build number
    # ------------------------------------------------------------------

    def test_build_omitted_for_untracked_network(self):
        """A sandbox COMP has no TSV row and no Build par, so _getBuildNumber
        returns None and the header must omit `build` entirely."""
        parent = self.sandbox.create(baseCOMP, 'untracked_net')
        parent.create(noiseTOP, 'n')
        doc = self._export(parent)
        self.assertNotIn('build', doc,
            'build key must be absent (not null) when there is no build number')

    def test_build_never_serialized_as_null(self):
        """Whatever the tracking state, `build` must never be present-but-None
        - that is exactly the noisy `build: null` this fix removes."""
        parent = self.sandbox.create(baseCOMP, 'null_check_net')
        doc = self._export(parent)
        if 'build' in doc:
            self.assertIsNotNone(doc['build'],
                'build must be a real number when present, never null')
