"""Get-or-create for custom pages and parameters (issue #94, WP5).

Function Store's third point was PlusPlusOne's declarative parameter ownership.
Embody already declares its own 139 parameters three times over (Embody.tdn,
the release manifest, _PERSISTED_PARAMS), so a fourth declaration would be the
wrong answer. What was actually missing is the CONTRACT underneath: four ad-hoc
copies of the page lookup had drifted, one of them searching for a page named
'Build Info' while creating 'About' -- so it never matched an existing page.

The invariant these tests protect is the one that matters to a user: code owns
the schema, the user owns the value, and nothing here ever destroys a
parameter to force the declared shape.
"""

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase


def _pardef():
    return op.Embody.op('embody_pardef').module


class TestEnsureCustomPage(EmbodyTestCase):

    def test_creates_the_page_when_absent(self):
        comp = self.sandbox.create(baseCOMP, 'pd_page_new')
        page = _pardef().ensureCustomPage(comp, 'About')
        self.assertEqual('About', page.name)
        self.assertLen(comp.customPages, 1)

    def test_is_idempotent(self):
        """Extensions reinit on every source save -- this runs many times."""
        comp = self.sandbox.create(baseCOMP, 'pd_page_idem')
        first = _pardef().ensureCustomPage(comp, 'About')
        second = _pardef().ensureCustomPage(comp, 'About')
        self.assertEqual(first.name, second.name)
        self.assertLen(comp.customPages, 1,
                       'a second ensure must not append a duplicate page')

    def test_finds_a_page_created_by_hand(self):
        comp = self.sandbox.create(baseCOMP, 'pd_page_found')
        comp.appendCustomPage('About')
        _pardef().ensureCustomPage(comp, 'About')
        self.assertLen(comp.customPages, 1)


class TestEnsureCustomPar(EmbodyTestCase):

    def _comp(self, name):
        comp = self.sandbox.create(baseCOMP, name)
        return comp, _pardef().ensureCustomPage(comp, 'About')

    def test_returns_a_par_not_a_pargroup(self):
        """append*() returns a ParGroup; the caller wants the Par."""
        comp, page = self._comp('pd_par_type')
        par = _pardef().ensureCustomPar(comp, page, 'Build', 'Int')
        self.assertTrue(hasattr(par, 'eval'),
                        'must be a Par -- a ParGroup has no .eval()')
        self.assertEqual('Build', par.name)

    def test_applies_declared_attributes(self):
        comp, page = self._comp('pd_par_attrs')
        par = _pardef().ensureCustomPar(
            comp, page, 'Build', 'Int', label='Build Number', readOnly=True)
        self.assertEqual('Build Number', par.label)
        self.assertTrue(par.readOnly)

    def test_reapplies_attributes_on_an_existing_par(self):
        """Symmetric application: a schema change must reach existing installs."""
        comp, page = self._comp('pd_par_resync')
        _pardef().ensureCustomPar(comp, page, 'Build', 'Int', label='Old')
        par = _pardef().ensureCustomPar(comp, page, 'Build', 'Int', label='New')
        self.assertEqual('New', par.label,
                         'attributes applied only at creation never reach an '
                         'install that already has the par')

    def test_never_duplicates_and_never_touches_the_users_value(self):
        comp, page = self._comp('pd_par_value')
        par = _pardef().ensureCustomPar(comp, page, 'Build', 'Int')
        par.val = 42
        again = _pardef().ensureCustomPar(comp, page, 'Build', 'Int')
        self.assertLen([p for p in comp.customPars if p.name == 'Build'], 1)
        self.assertEqual(42, again.eval(),
                         "the value is the user's; ensure must not rewrite it")

    def test_a_zero_valued_par_is_not_mistaken_for_absent(self):
        """Truthiness on a Par EVALUATES it, so `if not par` reads 0 as absent."""
        comp, page = self._comp('pd_par_zero')
        par = _pardef().ensureCustomPar(comp, page, 'Build', 'Int')
        par.val = 0
        _pardef().ensureCustomPar(comp, page, 'Build', 'Int')
        self.assertLen([p for p in comp.customPars if p.name == 'Build'], 1,
                       'a par holding 0 must not be appended twice')

    def test_a_style_mismatch_is_refused_not_destroyed(self):
        """Par.destroy() takes the value, expressions and exports -- forever."""
        comp, page = self._comp('pd_par_style')
        par = _pardef().ensureCustomPar(comp, page, 'Build', 'Int')
        par.val = 7
        with self.assertRaises(ValueError):
            _pardef().ensureCustomPar(comp, page, 'Build', 'Str')
        self.assertIsNotNone(getattr(comp.par, 'Build', None),
                             'the refusal must leave the par standing')
        self.assertEqual(7, comp.par.Build.eval())

    def test_probes_the_whole_comp_not_just_the_page(self):
        """Custom par names are a FLAT per-COMP namespace.

        append*() defaults to replace=True, so a page-scoped probe would
        silently destroy a same-named par living on another page.
        """
        comp, about = self._comp('pd_par_flat')
        par = _pardef().ensureCustomPar(comp, about, 'Build', 'Int')
        par.val = 5
        other = _pardef().ensureCustomPage(comp, 'Other')
        _pardef().ensureCustomPar(comp, other, 'Build', 'Int')
        self.assertLen([p for p in comp.customPars if p.name == 'Build'], 1,
                       'the par on the other page must be found, not replaced')
        self.assertEqual(5, comp.par.Build.eval())


    def test_a_multi_value_style_is_found_by_tuplet_name(self):
        """RGB lives as Tintr/Tintg/Tintb; a probe by .name re-appends forever.

        The first cut probed `p.name == name`, so an RGB par was never found,
        appendRGB (replace=True) re-created it on every reinit, and the
        re-read then raised (issue #94 review).
        """
        comp, page = self._comp('pd_par_rgb')
        group = _pardef().ensureCustomPar(comp, page, 'Tint', 'RGB')
        comp.par.Tintr = 0.25
        again = _pardef().ensureCustomPar(comp, page, 'Tint', 'RGB')
        self.assertLen([p for p in comp.customPars if p.tupletName == 'Tint'], 3,
                       'the tuplet must be found, not re-created')
        self.assertEqual(0.25, comp.par.Tintr.eval(),
                         "a re-ensure must not reset the user's component")
        self.assertEqual(3, len(again),
                         'a tuplet returns the 3-wide ParGroup, not one component')
        self.assertEqual('Tint', again.name)

    def test_user_expression_survives_a_re_ensure(self):
        comp, page = self._comp('pd_par_expr')
        par = _pardef().ensureCustomPar(comp, page, 'Build', 'Int')
        par.expr = 'absTime.frame'
        _pardef().ensureCustomPar(comp, page, 'Build', 'Int', expr='1', val=3)
        self.assertEqual('absTime.frame', comp.par.Build.expr,
                         'expr/val are user state and must never be re-applied')

    def test_a_rejected_attribute_raises_instead_of_vanishing(self):
        """A typo'd keyword silently dropped is the no-op-button shape."""
        comp, page = self._comp('pd_par_typo')
        with self.assertRaises(ValueError):
            _pardef().ensureCustomPar(comp, page, 'Build', 'Int', lable='x')


class TestCallSitesAreRouted(EmbodyTestCase):
    """The four drifted copies must not come back."""

    def test_the_build_stamps_go_through_ensure_custom_par(self):
        """setupBuildParameters is the one production caller -- keep it so."""
        from pathlib import Path
        src = (Path(project.folder).parent / 'dev/embody/Embody/EmbodyExt.py'
               ).read_text(encoding='utf-8')
        body = src.split('def setupBuildParameters', 1)[1].split('\n    def ', 1)[0]
        self.assertIn('ensureCustomPar', body)
        self.assertNotIn('appendInt(', body)
        self.assertNotIn('appendStr(', body)

    def test_no_ad_hoc_page_lookup_remains(self):
        import re
        from pathlib import Path
        repo = Path(project.folder).parent
        pattern = re.compile(r'appendCustomPage\(')
        offenders = []
        for rel in ('dev/embody/Embody/EmbodyExt.py',
                    'dev/embody/Embody/TDXNExt.py'):
            src = (repo / rel).read_text(encoding='utf-8')
            for m in pattern.finditer(src):
                line = src[:m.start()].count('\n') + 1
                offenders.append('%s:%d' % (rel.split('/')[-1], line))
        self.assertEqual(
            [], offenders,
            'appendCustomPage called directly instead of through '
            'embody_pardef.ensureCustomPage: %s' % ', '.join(offenders))
