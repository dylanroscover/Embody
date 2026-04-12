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
