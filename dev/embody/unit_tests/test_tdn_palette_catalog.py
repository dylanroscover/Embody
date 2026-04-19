"""
Test suite: TDN palette catalog detection + animationCOMP DAT preservation.

Covers:
- Catalog lookup (Strategy 1 in _isPaletteClone): name + OPType match
- Child count floor: rejects empty user COMPs that collide with palette names
- Legacy string-format catalog entries: backwards compatibility
- TDBasicWidgets clone expression heuristic (Strategy 2 fallback)
- animationCOMP internal tableDATs: content always exported regardless of
  include_dat_content flag; round-trip preserves keyframe data
- _isInsideAnimationCOMP helper
"""

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase

ParMode = type(op('/').par.clone.mode)


class TestTDNPaletteCatalog(EmbodyTestCase):

	def setUp(self):
		super().setUp()
		self.tdn = self.embody.ext.TDN
		# Snapshot the live catalog so tests can inject synthetic entries
		# without polluting cross-test state.
		self._saved_catalog = dict(self.tdn._palette_catalog)

	def tearDown(self):
		self.tdn._palette_catalog = self._saved_catalog
		super().tearDown()

	# =================================================================
	# A. Catalog Lookup (Strategy 1)
	# =================================================================

	def test_A01_catalog_lookup_positive(self):
		"""name + OPType match against catalog → detected as palette."""
		self.tdn._palette_catalog = {
			'myWidget': {'type': 'containerCOMP', 'min_children': 0},
		}
		comp = self.sandbox.create(containerCOMP, 'myWidget')
		self.assertTrue(self.tdn._isPaletteClone(comp),
			'Catalog entry with matching name+type must be detected')

	def test_A02_catalog_lookup_wrong_type_rejected(self):
		"""name match but wrong OPType → not detected via catalog."""
		self.tdn._palette_catalog = {
			'myWidget': {'type': 'containerCOMP', 'min_children': 0},
		}
		# baseCOMP != containerCOMP → catalog match fails.
		# Empty clone parameter → heuristic fallback also returns False.
		comp = self.sandbox.create(baseCOMP, 'myWidget')
		self.assertFalse(self.tdn._isPaletteClone(comp),
			'Wrong OPType must not match catalog entry')

	# =================================================================
	# B. Child Count Floor
	# =================================================================

	def test_B01_child_count_floor_rejects_empty_user_comp(self):
		"""Empty user COMP with palette name → rejected by floor check."""
		self.tdn._palette_catalog = {
			'buttonCheckbox': {'type': 'containerCOMP', 'min_children': 10},
		}
		# floor = max(1, 10//2) = 5; COMP has 0 children → rejected
		comp = self.sandbox.create(containerCOMP, 'buttonCheckbox')
		self.assertFalse(self.tdn._isPaletteClone(comp),
			'Empty COMP must not match palette entry with min_children=10')

	def test_B02_child_count_floor_tolerates_user_mods(self):
		"""User who kept most palette children (half count) → still detected."""
		self.tdn._palette_catalog = {
			'vrHMD': {'type': 'geometryCOMP', 'min_children': 51},
		}
		comp = self.sandbox.create(geometryCOMP, 'vrHMD')
		# floor = max(1, 51//2) = 25; create 26 children (above floor)
		for i in range(26):
			comp.create(nullCHOP, f'ch{i}')
		self.assertTrue(self.tdn._isPaletteClone(comp),
			'COMP at half expected child count must still match')

	def test_B03_child_count_floor_when_min_is_one(self):
		"""min_children=1 → floor 1; 0 children fails, 1+ passes."""
		self.tdn._palette_catalog = {
			'buttonCheckbox': {'type': 'containerCOMP', 'min_children': 1},
		}
		comp = self.sandbox.create(containerCOMP, 'buttonCheckbox')
		self.assertFalse(self.tdn._isPaletteClone(comp),
			'0 children must fail floor when min_children=1')
		comp.create(nullCHOP, 'ch0')
		self.assertTrue(self.tdn._isPaletteClone(comp),
			'1 child must pass floor when min_children=1')

	def test_B04_child_count_floor_zero_skips_check(self):
		"""min_children=0 → floor 0, any child count passes."""
		self.tdn._palette_catalog = {
			'odd': {'type': 'containerCOMP', 'min_children': 0},
		}
		comp = self.sandbox.create(containerCOMP, 'odd')
		self.assertTrue(self.tdn._isPaletteClone(comp),
			'min_children=0 must skip the floor check')

	# =================================================================
	# C. Legacy String Catalog Format (Backwards Compat)
	# =================================================================

	def test_C01_legacy_string_catalog_format(self):
		"""Old string-format catalog entries still work."""
		self.tdn._palette_catalog = {
			'myWidget': 'containerCOMP',  # legacy pre-v5.0.355 format
		}
		comp = self.sandbox.create(containerCOMP, 'myWidget')
		self.assertTrue(self.tdn._isPaletteClone(comp),
			'String-format catalog entries must still be detected')

	# =================================================================
	# D. Clone Expression Heuristic (Strategy 2 Fallback)
	# =================================================================

	def test_D01_tdbasicwidgets_heuristic(self):
		"""op.TDBasicWidgets.* clone expr → detected via heuristic fallback."""
		self.tdn._palette_catalog = {}  # force fallback path
		comp = self.sandbox.create(containerCOMP, 'someWidget')
		# Create a sibling whose path includes the substring, so the clone
		# expression parses, evaluates to a real op (not under /sys/), and
		# leaves the clone parameter in EXPRESSION mode with text that
		# matches the heuristic substring check.
		self.sandbox.create(baseCOMP, 'TDBasicWidgets_stub')
		comp.par.clone.expr = "parent().op('TDBasicWidgets_stub')"
		self.assertTrue(self.tdn._isPaletteClone(comp),
			'TDBasicWidgets clone expression must be detected')

	# =================================================================
	# E. animationCOMP DAT Content Preservation
	# =================================================================

	def test_E01_animationcomp_dat_content_exported_despite_flag(self):
		"""animationCOMP internal DATs get content even with content flag False."""
		self.sandbox.create(animationCOMP, 'anim1')
		result = self.tdn.ExportNetwork(
			root_path=self.sandbox.path, include_dat_content=False)
		self.assertTrue(result.get('success'))

		# Walk the export to find the 'attributes' DAT inside anim1.
		attrs_export = self._findOpInExport(result['tdn'], 'attributes')
		self.assertIsNotNone(attrs_export,
			'attributes DAT inside animationCOMP must appear in export')
		self.assertIn('dat_content', attrs_export,
			'animationCOMP DAT must carry content even with '
			'include_dat_content=False')

	def test_E02_standalone_dat_respects_content_flag(self):
		"""Regular DAT (not inside animationCOMP) still respects the flag."""
		tbl = self.sandbox.create(tableDAT, 'standalone_tbl')
		tbl.clear()
		tbl.appendRow(['a', 'b'])
		result = self.tdn.ExportNetwork(
			root_path=self.sandbox.path, include_dat_content=False)
		self.assertTrue(result.get('success'))

		tbl_export = self._findOpInExport(result['tdn'], 'standalone_tbl')
		self.assertIsNotNone(tbl_export)
		self.assertNotIn('dat_content', tbl_export,
			'Standalone DAT must NOT carry content when flag is False')

	def test_E03_animationcomp_dats_roundtrip(self):
		"""animationCOMP internal data survives export → destroy → import."""
		anim = self.sandbox.create(animationCOMP, 'anim2')
		attrs = anim.op('attributes')
		self.assertIsNotNone(attrs)
		attrs.clear()
		attrs.appendRow(['marker_a', '123'])
		attrs.appendRow(['marker_b', '456'])

		orig = self.tdn.ExportNetwork(
			root_path=self.sandbox.path, include_dat_content=False)
		self.assertTrue(orig.get('success'))

		# Destroy and re-import
		for c in list(self.sandbox.children):
			c.destroy()
		result = self.tdn.ImportNetwork(
			target_path=self.sandbox.path, tdn=orig['tdn'])
		self.assertTrue(result.get('success'), f'Import failed: {result}')

		restored_attrs = self.sandbox.op('anim2/attributes')
		self.assertIsNotNone(restored_attrs,
			'attributes DAT must be restored inside animationCOMP')
		self.assertEqual(restored_attrs.numRows, 2)
		self.assertEqual(str(restored_attrs[0, 0].val), 'marker_a')
		self.assertEqual(str(restored_attrs[0, 1].val), '123')
		self.assertEqual(str(restored_attrs[1, 0].val), 'marker_b')
		self.assertEqual(str(restored_attrs[1, 1].val), '456')

	# =================================================================
	# F. _isInsideAnimationCOMP Helper
	# =================================================================

	def test_F01_inside_animationcomp_positive(self):
		"""Direct child of animationCOMP → True."""
		anim = self.sandbox.create(animationCOMP, 'anim3')
		keys = anim.op('keys')
		self.assertIsNotNone(keys)
		self.assertTrue(self.tdn._isInsideAnimationCOMP(keys))

	def test_F02_inside_animationcomp_negative(self):
		"""DAT outside animationCOMP → False."""
		dat = self.sandbox.create(tableDAT, 'plain_dat')
		self.assertFalse(self.tdn._isInsideAnimationCOMP(dat))

	# =================================================================
	# G. Palette Handling Resolver (_resolvePaletteHandling)
	# =================================================================

	def _fakePalette(self, name='myPalette', typ=containerCOMP):
		"""Create a COMP that the catalog recognizes as a palette."""
		self.tdn._palette_catalog = {
			name: {'type': typ.__name__, 'min_children': 0},
		}
		return self.sandbox.create(typ, name)

	def test_G01_storage_override_blackbox(self):
		"""Per-COMP storage 'blackbox' wins over any par value."""
		comp = self._fakePalette()
		comp.store(self.tdn._PALETTE_HANDLING_KEY, 'blackbox')
		saved_par = self.embody.par.Tdnpalettehandling.eval()
		try:
			self.embody.par.Tdnpalettehandling = 'fullexport'
			self.assertEqual(
				self.tdn._resolvePaletteHandling(comp), 'blackbox',
				'Storage override must win over par value')
		finally:
			self.embody.par.Tdnpalettehandling = saved_par

	def test_G02_storage_override_fullexport(self):
		"""Per-COMP storage 'fullexport' wins too."""
		comp = self._fakePalette()
		comp.store(self.tdn._PALETTE_HANDLING_KEY, 'fullexport')
		saved_par = self.embody.par.Tdnpalettehandling.eval()
		try:
			self.embody.par.Tdnpalettehandling = 'blackbox'
			self.assertEqual(
				self.tdn._resolvePaletteHandling(comp), 'fullexport')
		finally:
			self.embody.par.Tdnpalettehandling = saved_par

	def test_G03_par_blackbox_no_prompt(self):
		"""Par set to blackbox returns directly, no prompt."""
		comp = self._fakePalette()
		saved_par = self.embody.par.Tdnpalettehandling.eval()
		try:
			self.embody.par.Tdnpalettehandling = 'blackbox'
			self.assertEqual(
				self.tdn._resolvePaletteHandling(comp), 'blackbox')
		finally:
			self.embody.par.Tdnpalettehandling = saved_par

	def test_G04_par_fullexport_no_prompt(self):
		"""Par set to fullexport returns directly, no prompt."""
		comp = self._fakePalette()
		saved_par = self.embody.par.Tdnpalettehandling.eval()
		try:
			self.embody.par.Tdnpalettehandling = 'fullexport'
			self.assertEqual(
				self.tdn._resolvePaletteHandling(comp), 'fullexport')
		finally:
			self.embody.par.Tdnpalettehandling = saved_par

	def test_G05_ask_prompt_blackbox_this(self):
		"""Par='ask' + prompt choice 0 -> blackbox, stored on target."""
		comp = self._fakePalette()
		saved_par = self.embody.par.Tdnpalettehandling.eval()
		try:
			self.embody.par.Tdnpalettehandling = 'ask'
			self.embody.store(
				'_smoke_test_responses',
				{'Embody - Palette Component Detected': 0})
			result = self.tdn._resolvePaletteHandling(comp)
			self.assertEqual(result, 'blackbox')
			self.assertEqual(
				comp.fetch(self.tdn._PALETTE_HANDLING_KEY, None, search=False),
				'blackbox',
				'Choice must be persisted on the target COMP')
			# Par must remain unchanged for this-COMP choices
			self.assertEqual(self.embody.par.Tdnpalettehandling.eval(), 'ask')
		finally:
			self.embody.par.Tdnpalettehandling = saved_par

	def test_G06_ask_prompt_fullexport_this(self):
		"""Par='ask' + prompt choice 1 -> fullexport, stored on target."""
		comp = self._fakePalette()
		saved_par = self.embody.par.Tdnpalettehandling.eval()
		try:
			self.embody.par.Tdnpalettehandling = 'ask'
			self.embody.store(
				'_smoke_test_responses',
				{'Embody - Palette Component Detected': 1})
			result = self.tdn._resolvePaletteHandling(comp)
			self.assertEqual(result, 'fullexport')
			self.assertEqual(
				comp.fetch(self.tdn._PALETTE_HANDLING_KEY, None, search=False),
				'fullexport')
			self.assertEqual(self.embody.par.Tdnpalettehandling.eval(), 'ask')
		finally:
			self.embody.par.Tdnpalettehandling = saved_par

	def test_G07_ask_prompt_blackbox_for_all(self):
		"""Par='ask' + prompt choice 2 -> flips par to blackbox, nothing stored on target."""
		comp = self._fakePalette()
		saved_par = self.embody.par.Tdnpalettehandling.eval()
		try:
			self.embody.par.Tdnpalettehandling = 'ask'
			self.embody.store(
				'_smoke_test_responses',
				{'Embody - Palette Component Detected': 2})
			result = self.tdn._resolvePaletteHandling(comp)
			self.assertEqual(result, 'blackbox')
			self.assertEqual(
				self.embody.par.Tdnpalettehandling.eval(), 'blackbox',
				'"for all" must flip the par')
			self.assertIsNone(
				comp.fetch(self.tdn._PALETTE_HANDLING_KEY, None, search=False),
				'"for all" must not write per-COMP storage')
		finally:
			self.embody.par.Tdnpalettehandling = saved_par

	def test_G08_ask_prompt_fullexport_for_all(self):
		"""Par='ask' + prompt choice 3 -> flips par to fullexport."""
		comp = self._fakePalette()
		saved_par = self.embody.par.Tdnpalettehandling.eval()
		try:
			self.embody.par.Tdnpalettehandling = 'ask'
			self.embody.store(
				'_smoke_test_responses',
				{'Embody - Palette Component Detected': 3})
			result = self.tdn._resolvePaletteHandling(comp)
			self.assertEqual(result, 'fullexport')
			self.assertEqual(
				self.embody.par.Tdnpalettehandling.eval(), 'fullexport')
		finally:
			self.embody.par.Tdnpalettehandling = saved_par

	def test_G09_export_blackbox_omits_children(self):
		"""End-to-end: blackbox mode emits palette_clone=true, no children."""
		comp = self._fakePalette('fakePal', baseCOMP)
		# Add an internal child to prove it's skipped
		comp.create(textDAT, 'internal_child')
		comp.store(self.tdn._PALETTE_HANDLING_KEY, 'blackbox')
		result = self.tdn.ExportNetwork(root_path=self.sandbox.path)
		self.assertTrue(result.get('success'))
		entry = self._findOpInExport(result['tdn'], 'fakePal')
		self.assertIsNotNone(entry)
		self.assertTrue(entry.get('palette_clone'),
			'blackbox must set palette_clone=true')
		self.assertNotIn('children', entry,
			'blackbox must not emit internal children')

	def test_G10_export_fullexport_includes_children(self):
		"""End-to-end: fullexport mode recurses, no palette_clone flag."""
		comp = self._fakePalette('fakePal2', baseCOMP)
		comp.create(textDAT, 'internal_child')
		comp.store(self.tdn._PALETTE_HANDLING_KEY, 'fullexport')
		result = self.tdn.ExportNetwork(root_path=self.sandbox.path)
		self.assertTrue(result.get('success'))
		entry = self._findOpInExport(result['tdn'], 'fakePal2')
		self.assertIsNotNone(entry)
		self.assertFalse(entry.get('palette_clone'),
			'fullexport must not set palette_clone flag')
		self.assertIn('children', entry,
			'fullexport must recurse into children')
		names = [c.get('name') for c in entry.get('children', [])]
		self.assertIn('internal_child', names)

	# =================================================================
	# Helpers
	# =================================================================

	def _findOpInExport(self, tdn_doc, name):
		"""Find an operator entry by name anywhere in the TDN export tree."""
		def walk(entries):
			for e in entries or []:
				if e.get('name') == name:
					return e
				found = walk(e.get('children'))
				if found:
					return found
			return None
		return walk(tdn_doc.get('operators'))
