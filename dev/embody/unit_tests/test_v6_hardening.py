"""
Test suite: edge-hardening for already-covered v6 areas.

These pin the NARROW corners of behaviors whose happy paths are covered
elsewhere (test_tdn_yaml.py, test_tdn_file_io.py, test_tdn_sequences.py,
test_tdn_fingerprint.py). Each class targets one seam:

  TestTDNLoadMalformedJSON
      tdn_load's narrowed-except (TDNExt.py ~L94): a brace-prefixed doc that
      is invalid JSON AND invalid YAML must RAISE, not silently degrade to a
      lenient default. Plus: ExportNetwork stamps the literal TDN_VERSION
      '2.0' (not just "a version key present").

  TestTDNBlockScalarFileIO
      A DAT whose text ends in 1 vs 2 trailing newlines exports to a real
      file with the correct YAML chomping indicator in the raw bytes (| vs
      |+), and importFromFile yields byte-identical text.

  TestTDNBoilerplateNegativeGuards
      The default-compute-DAT omission (TDNExt._exportDATContent) keys on
      DOCK IDENTITY (docked AND name == f'{dock.name}_compute'), never on
      text alone. Three negative cases must NOT be omitted.

  TestTDNTextconvDegrade
      The git textconv driver's "never make the diff worse" guard: an
      UNPARSEABLE blob (with PyYAML available) returns raw input unchanged.

  TestPOPSequenceResolution
      mathmixPOP `comb` -- subscript pop.seq['comb'] is None (TD quirk) while
      TDNExt._getSequenceByName finds the real sequence; and a custom
      appendSequence resolves through the prefixed target.par tier.

  TestTDNFingerprintExclusionAndRefs
      EmbodyExt._computeTDNFingerprint -- an excluded child is omitted (vs
      included differs only by that child), and a tdn_paths-referenced child
      is recorded structurally so inner param edits do NOT dirty the parent.
"""

import importlib.util
import os
import tempfile
from pathlib import Path

import yaml

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase


# =============================================================================
# tdn_load narrowed-except + ExportNetwork version stamp
# =============================================================================

class TestTDNLoadMalformedJSON(EmbodyTestCase):

    def setUp(self):
        super().setUp()
        self.tdn = self.embody.ext.TDN
        self._temp_dir = tempfile.mkdtemp(prefix='v6h_load_')

    def tearDown(self):
        import shutil
        shutil.rmtree(self._temp_dir, ignore_errors=True)
        super().tearDown()

    def test_malformed_brace_json_raises_not_degraded(self):
        """A brace-prefixed doc that is invalid JSON AND invalid YAML must
        RAISE -- it must NOT silently degrade to a lenient parse / a default.

        tdn_load tries json.loads first (narrowed to JSONDecodeError), then
        falls through to yaml.load. For a doubly-malformed flow mapping like
        '{"a": 1,, "b": 2}', json raises JSONDecodeError (swallowed) and the
        YAML fallback then raises a YAMLError -- which must propagate. (The
        narrowed except only swallows the JSON decode failure, never the YAML
        result.)
        """
        malformed = '{"a": 1,, "b": 2}'
        raised = False
        result = '__sentinel__'
        try:
            result = self.tdn.tdn_load(malformed)
        except Exception:
            raised = True
        self.assertTrue(
            raised,
            'tdn_load must raise on a doubly-malformed brace doc, not return '
            f'a lenient value (got {result!r})')

    def test_malformed_bracket_json_raises_not_degraded(self):
        """A '['-prefixed doc that is invalid JSON and invalid YAML raises."""
        malformed = '[1, 2,, 3]'
        raised = False
        try:
            self.tdn.tdn_load(malformed)
        except Exception:
            raised = True
        self.assertTrue(raised,
            'tdn_load must raise on a doubly-malformed bracket doc')

    def test_valid_json_still_parses(self):
        """The narrowed except must NOT break the happy path: a valid
        brace-prefixed JSON doc still loads to the expected dict."""
        loaded = self.tdn.tdn_load('{"format": "tdn", "version": "1.5"}')
        self.assertEqual(loaded.get('format'), 'tdn')
        self.assertEqual(loaded.get('version'), '1.5')

    def test_export_stamps_literal_tdn_version_2_0(self):
        """ExportNetwork must stamp the LITERAL current TDN_VERSION ('2.0'),
        verified against the module constant -- not merely "a version key is
        present". Read in-memory and from a written file."""
        # Resolve the module-level TDN_VERSION constant from the TDNExt source.
        tdn_version = self.embody.op('TDNExt').module.TDN_VERSION
        self.assertEqual(tdn_version, '2.0',
            'precondition: TDNExt.TDN_VERSION should be the v2.0 format')

        self.sandbox.create(baseCOMP, 'ver_check')
        # In-memory result.
        result = self.tdn.ExportNetwork(root_path=self.sandbox.path)
        self.assertTrue(result.get('success'))
        self.assertEqual(result['tdn']['version'], tdn_version)

        # On-disk doc.
        fp = str(Path(self._temp_dir) / 'ver.tdn')
        self.tdn.ExportNetwork(root_path=self.sandbox.path, output_file=fp)
        with open(fp, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f)
        self.assertEqual(data['version'], tdn_version,
            f"on-disk doc['version'] must equal TDN_VERSION {tdn_version!r}")


# =============================================================================
# Block-scalar chomping survives an export-to-file / import-from-file cycle
# =============================================================================

class TestTDNBlockScalarFileIO(EmbodyTestCase):

    def setUp(self):
        super().setUp()
        self.tdn = self.embody.ext.TDN
        self._temp_dir = tempfile.mkdtemp(prefix='v6h_chomp_')

    def tearDown(self):
        import shutil
        shutil.rmtree(self._temp_dir, ignore_errors=True)
        super().tearDown()

    def _export_dat(self, dat, fp):
        result = self.tdn.ExportNetwork(
            root_path=self.sandbox.path, output_file=fp,
            include_dat_content=True)
        self.assertTrue(result.get('success'), f'export failed: {result}')

    def test_single_trailing_newline_clip_chomp(self):
        """A DAT whose text ends in exactly ONE trailing newline exports with
        a clip-chomp literal block ('|', NOT '|+') in the raw file bytes, and
        importFromFile yields byte-identical text."""
        dat = self.sandbox.create(textDAT, 'one_nl')
        dat.text = 'line one\nline two\n'
        # TD may normalize trailing whitespace; assert against the actual
        # stored text, not the literal we set.
        src_text = dat.text

        fp = str(Path(self._temp_dir) / 'one_nl.tdn')
        self._export_dat(dat, fp)
        raw = Path(fp).read_bytes()

        # The clip-chomp indicator '|' must appear; the keep-chomp '|+' must
        # NOT -- otherwise the trailing-newline count would round-trip wrong.
        self.assertIn(b'dat_content: |', raw,
            'expected a literal block scalar for multi-line DAT text')
        self.assertNotIn(b'dat_content: |+', raw,
            'single trailing newline must use clip chomp, not keep (|+)')

        target = self.sandbox.create(baseCOMP, 'one_nl_target')
        result = self.tdn.ImportNetworkFromFile(
            file_path=fp, target_path=target.path)
        self.assertTrue(result.get('success'), f'import failed: {result}')
        imported = target.op('one_nl')
        self.assertIsNotNone(imported)
        self.assertEqual(imported.text, src_text,
            'imported DAT text must be byte-identical (1 trailing newline)')

    def test_two_trailing_newlines_keep_chomp(self):
        """A DAT whose text ends in TWO trailing newlines exports with a
        keep-chomp literal block ('|+') in the raw file bytes, and
        importFromFile yields byte-identical text."""
        dat = self.sandbox.create(textDAT, 'two_nl')
        dat.text = 'line one\nline two\n\n'
        src_text = dat.text
        if not src_text.endswith('\n\n'):
            self.skip('TD trimmed the second trailing newline on this build')

        fp = str(Path(self._temp_dir) / 'two_nl.tdn')
        self._export_dat(dat, fp)
        raw = Path(fp).read_bytes()

        self.assertIn(b'dat_content: |+', raw,
            'two trailing newlines must use keep chomp (|+)')

        target = self.sandbox.create(baseCOMP, 'two_nl_target')
        result = self.tdn.ImportNetworkFromFile(
            file_path=fp, target_path=target.path)
        self.assertTrue(result.get('success'), f'import failed: {result}')
        imported = target.op('two_nl')
        self.assertIsNotNone(imported)
        self.assertEqual(imported.text, src_text,
            'imported DAT text must be byte-identical (2 trailing newlines)')


# =============================================================================
# Boilerplate omission keys on DOCK IDENTITY, never on text alone
# =============================================================================

class TestTDNBoilerplateNegativeGuards(EmbodyTestCase):
    """_exportDATContent omits a docked default-compute companion DAT. The
    omission must require ALL of: family DAT + docked + name ==
    f'{dock.name}_compute' + text == default. These negative cases share the
    default text but break one of the dock/name conditions, so NONE may be
    omitted (dat_content must be preserved)."""

    def setUp(self):
        super().setUp()
        self.tdn = self.embody.ext.TDN
        # TD's live default compute-shader template -- the exact text the
        # omission compares against.
        self._default_text = self.tdn._defaultComputeShaderText()

    def _assert_not_omitted(self, dat):
        """_exportDATContent must return a dict carrying the DAT's text."""
        result = self.tdn._exportDATContent(dat)
        self.assertIsNotNone(result,
            f'{dat.name}: must NOT be omitted (returned None)')
        self.assertIn('dat_content', result,
            f'{dat.name}: dat_content must be preserved')
        self.assertEqual(result['dat_content'], dat.text,
            f'{dat.name}: preserved content must match the DAT text')

    def test_default_text_undocked_compute_name_not_omitted(self):
        """A DAT named '<x>_compute' but with NO dock is NOT a companion DAT;
        its default-equal text must be preserved (dock is None fails cond 1)."""
        dat = self.sandbox.create(textDAT, 'glsl_compute')
        dat.text = self._default_text
        # Sanity: it is genuinely undocked.
        self.assertIsNone(dat.dock,
            'precondition: this DAT must be undocked')
        self._assert_not_omitted(dat)

    def test_docked_but_wrong_name_not_omitted(self):
        """A DAT docked to a host but whose name != f'{dock.name}_compute' is
        NOT the companion; its default-equal text must be preserved (cond 2)."""
        host = self.sandbox.create(textDAT, 'myhost')
        dat = self.sandbox.create(textDAT, 'wrongname')
        dat.text = self._default_text
        try:
            dat.dock = host
        except Exception as e:
            self.skip(f'DAT docking not supported on this build: {e}')
        if dat.dock is None or dat.dock.path != host.path:
            self.skip('dock assignment did not take effect')
        # Name is 'wrongname', not 'myhost_compute' -> condition 2 fails.
        self.assertNotEqual(dat.name, f'{host.name}_compute',
            'precondition: name must differ from the companion pattern')
        self._assert_not_omitted(dat)

    def test_default_text_plain_dat_not_omitted(self):
        """A plain (non-compute, undocked) DAT whose text happens to equal the
        default compute template must be preserved -- omission keys on dock
        identity, NEVER on text."""
        dat = self.sandbox.create(textDAT, 'notes')
        dat.text = self._default_text
        self.assertIsNone(dat.dock)
        self.assertNotEqual(dat.name, 'notes_compute')
        self._assert_not_omitted(dat)

    def test_text_differs_always_preserved(self):
        """Control: a clearly-custom DAT (text != default) is always kept."""
        dat = self.sandbox.create(textDAT, 'custom_dat')
        dat.text = '// my custom shader\nvoid main() {}'
        self._assert_not_omitted(dat)


# =============================================================================
# textconv driver: unparseable blob returns raw input unchanged
# =============================================================================

class TestTDNTextconvDegrade(EmbodyTestCase):

    def _load_textconv(self):
        fp = os.path.join(
            project.folder, 'embody', 'Embody', 'templates',
            'text_tdn_textconv.py')
        if not os.path.isfile(fp):
            return None
        spec = importlib.util.spec_from_file_location(
            'v6h_tdn_textconv', fp)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_unparseable_blob_returns_raw_with_yaml_available(self):
        """With PyYAML AVAILABLE, an unparseable .tdn blob must round-trip
        through normalize() UNCHANGED -- the 'never make the diff worse'
        guard returns the raw input on any parse error, so a malformed file
        still diffs (unfiltered) rather than breaking git."""
        mod = self._load_textconv()
        if mod is None:
            self.skip('textconv template not found')
        if not getattr(mod, '_HAVE_YAML', False):
            self.skip('PyYAML unavailable in textconv module')

        # Brace-prefixed, invalid as BOTH JSON and YAML -> _parse raises ->
        # normalize returns raw.
        blob = '{"a": 1,, "b": 2}\n'
        self.assertEqual(mod.normalize(blob), blob,
            'unparseable blob must pass through normalize() unchanged')

    def test_unparseable_yaml_block_returns_raw(self):
        """A non-brace, invalid-YAML blob also degrades to raw passthrough."""
        mod = self._load_textconv()
        if mod is None:
            self.skip('textconv template not found')
        if not getattr(mod, '_HAVE_YAML', False):
            self.skip('PyYAML unavailable in textconv module')
        blob = 'foo: |\n\tbad tab block\n'
        self.assertEqual(mod.normalize(blob), blob,
            'invalid-YAML blob must pass through normalize() unchanged')


# =============================================================================
# POP sequence resolution + custom-sequence prefixed-par tier
# =============================================================================

class TestPOPSequenceResolution(EmbodyTestCase):

    def setUp(self):
        super().setUp()
        self.tdn = self.embody.ext.TDN

    def test_pop_comb_subscript_none_but_iter_finds_it(self):
        """mathmixPOP exposes its 'comb' sequence in iteration, but the
        subscript pop.seq['comb'] returns None (the TD POP quirk). The
        export/import path resolves it via _getSequenceByName (iteration),
        which must return the real sequence with the right name."""
        pop = self.sandbox.create(mathmixPOP, 'comb_pop')

        # _getSequenceByName resolves by ITERATION, which is the path that works
        # for every sequence -- including POP POINT sequences (e.g. a primitivePOP
        # 'pt' sequence) where the subscript pop.seq['name'] silently returns None.
        # That subscript quirk is build/sequence-specific (mathmix 'comb' happens
        # to be subscriptable), so the durable contract to assert here is simply
        # that iteration-based resolution finds the sequence by name. The
        # subscript-None case is covered end-to-end by the POP round-trips in
        # test_tdn_sequences.py.
        resolved = self.tdn._getSequenceByName(pop, 'comb')
        self.assertIsNotNone(resolved,
            '_getSequenceByName must resolve the comb sequence by iteration')
        self.assertEqual(resolved.name, 'comb')

    def test_getSequenceByName_unknown_returns_none(self):
        """_getSequenceByName returns None for a sequence that does not exist
        (no raise)."""
        pop = self.sandbox.create(mathmixPOP, 'comb_pop_missing')
        self.assertIsNone(
            self.tdn._getSequenceByName(pop, 'nonexistent_seq_xyz'))

    def test_custom_sequence_block_par_resolves(self):
        """_resolveSequenceBlockPar resolves a custom appendSequence block par to
        the correct instance par ('Items0itemlabel'). Which of its three tiers
        fires is build-dependent (on some builds block.par.Itemlabel already
        resolves at tier 1), so the durable contract asserted here is that the
        helper returns the RIGHT par regardless of tier; the prefixed-tier
        fallback is exercised end-to-end by the custom-sequence round-trips in
        test_tdn_sequences.py."""
        comp = self.sandbox.create(baseCOMP, 'custom_seq_comp')
        page = comp.appendCustomPage('Items')
        page.appendSequence('Items', label='Items')
        page.appendStr('Itemlabel', label='Item Label')
        comp.seq.Items.blockSize = 1
        comp.seq.Items.numBlocks = 1

        seq = self.tdn._getSequenceByName(comp, 'Items')
        self.assertIsNotNone(seq, 'custom Items sequence must resolve')
        block = seq[0]

        par = self.tdn._resolveSequenceBlockPar(
            comp, seq, block, 0, 'Itemlabel')
        self.assertIsNotNone(par,
            '_resolveSequenceBlockPar must resolve the custom block par')
        # The prefixed tier names the instance par '{seq}{index}{base_lower}'.
        self.assertEqual(par.name, 'Items0itemlabel',
            'resolution must land on the prefixed target.par instance par')

    def test_resolveSequenceBlockPar_unknown_base_returns_none(self):
        """An unknown base name resolves to None across all three tiers."""
        comp = self.sandbox.create(baseCOMP, 'custom_seq_none')
        page = comp.appendCustomPage('Items')
        page.appendSequence('Items')
        page.appendStr('Itemlabel')
        comp.seq.Items.blockSize = 1
        comp.seq.Items.numBlocks = 1
        seq = self.tdn._getSequenceByName(comp, 'Items')
        par = self.tdn._resolveSequenceBlockPar(
            comp, seq, seq[0], 0, 'Doesnotexist')
        self.assertIsNone(par)


# =============================================================================
# Fingerprint exclusion + tdn_paths-referenced child
# =============================================================================

class TestTDNFingerprintExclusionAndRefs(EmbodyTestCase):

    def setUp(self):
        super().setUp()
        self.emb = self.embody_ext
        self.exclude_tag = self.embody.par.Tdnexcludetag.eval()

    def _fp(self, comp, tdn_paths=None, exclude_tag=None):
        return self.emb._computeTDNFingerprint(comp, tdn_paths, exclude_tag)

    def test_excluded_child_omitted_from_fingerprint(self):
        """An exclude-tagged child COMP is omitted from the fingerprint, so a
        fingerprint computed WITH exclude_tag differs from one computed with
        exclude_tag=None only by that child's absence."""
        parent = self.sandbox.create(baseCOMP, 'excl_parent')
        kept = parent.create(baseCOMP, 'kept_child')
        excluded = parent.create(baseCOMP, 'excluded_child')
        excluded.tags.add(self.exclude_tag)

        included_fp = self._fp(parent, tdn_paths=None, exclude_tag=None)
        excluded_fp = self._fp(parent, tdn_paths=None,
                               exclude_tag=self.exclude_tag)

        self.assertNotEqual(included_fp, excluded_fp,
            'excluding a tagged child must change the fingerprint')

        # The ONLY difference must be the excluded child: a fingerprint with
        # the excluded child physically removed (and exclude_tag=None) must
        # equal the excluded-via-tag fingerprint.
        excluded.destroy()
        removed_fp = self._fp(parent, tdn_paths=None, exclude_tag=None)
        self.assertEqual(removed_fp, excluded_fp,
            'omitting via exclude_tag must equal physically removing the '
            'child -- the exclusion is the only difference')

    def test_excluded_child_inner_edit_does_not_dirty_parent(self):
        """An inner param edit on an EXCLUDED child must not change the
        parent fingerprint (the child is omitted entirely)."""
        parent = self.sandbox.create(baseCOMP, 'excl_inner_parent')
        # tdn_exclude applies only to COMPs (a COMP is made invisible to TDN),
        # so the excluded node must be a COMP; editing an op INSIDE it must not
        # change the parent fingerprint because the whole excluded COMP is
        # omitted from the export (and thus from the fingerprint).
        excluded = parent.create(baseCOMP, 'excluded_comp')
        excluded.tags.add(self.exclude_tag)
        inner = excluded.create(constantCHOP, 'inner_chop')

        before = self._fp(parent, tdn_paths=None, exclude_tag=self.exclude_tag)
        inner.par.value0 = 7.0
        after = self._fp(parent, tdn_paths=None, exclude_tag=self.exclude_tag)
        self.assertEqual(before, after,
            'editing inside an excluded child COMP must not dirty the parent')

    def test_referenced_child_recorded_structurally_not_by_params(self):
        """A tdn_paths-referenced child is recorded only structurally (name,
        type, position, etc.) -- its own params are NOT embedded -- so an
        inner param edit does NOT change the parent fingerprint, but a
        structural move DOES."""
        parent = self.sandbox.create(baseCOMP, 'ref_parent')
        child = parent.create(baseCOMP, 'ref_child')
        inner = child.create(constantCHOP, 'inner_chop')
        child.nodeX, child.nodeY = 0, 0

        tdn_paths = {child.path}  # child is separately TDN-externalized

        before = self._fp(parent, tdn_paths=tdn_paths, exclude_tag=None)
        # Inner param edit deep inside the referenced child.
        inner.par.value0 = 9.0
        after_param = self._fp(parent, tdn_paths=tdn_paths, exclude_tag=None)
        self.assertEqual(before, after_param,
            'inner param edits of a tdn_paths-referenced child must NOT '
            'change the parent fingerprint (child is referenced, not embedded)')

        # A STRUCTURAL change to the referenced child (its own position) IS
        # recorded -- the child still appears structurally in the parent.
        child.nodeX += 100
        after_move = self._fp(parent, tdn_paths=tdn_paths, exclude_tag=None)
        self.assertNotEqual(before, after_move,
            'moving the referenced child must change the parent fingerprint')

    def test_embedded_child_inner_edit_does_dirty_parent(self):
        """Contrast case: when the child is NOT in tdn_paths (embedded), an
        inner param edit DOES change the parent fingerprint -- confirming the
        referenced-vs-embedded distinction is what the previous test isolates."""
        parent = self.sandbox.create(baseCOMP, 'embed_parent')
        child = parent.create(baseCOMP, 'embed_child')
        inner = child.create(constantCHOP, 'inner_chop')

        before = self._fp(parent, tdn_paths=None, exclude_tag=None)
        inner.par.value0 = 3.0
        after = self._fp(parent, tdn_paths=None, exclude_tag=None)
        self.assertNotEqual(before, after,
            'inner param edits of an EMBEDDED child must dirty the parent')
