"""
Test suite: CatalogManager bootstrap palette TSV.

Covers the shipped palette_catalog tableDAT that ships with Embody so new
users on known TD builds skip the 5-7s runtime palette scan on first open.

- _parseBootstrapPaletteTable: parse rows, filter by build, schema safety
- Empty / malformed tables return None (falls back to runtime scan)
- Row deduplication: first occurrence per name wins
- Integer coercion of min_children with graceful fallback
"""

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase


class TestCatalogBootstrapPalette(EmbodyTestCase):

	def setUp(self):
		super().setUp()
		self.cat = self.embody.ext.CatalogManager
		self.tbl = self.sandbox.create(tableDAT, 'fake_palette_catalog')
		self.tbl.clear()

	def _seed(self, rows):
		"""rows is a list of row-lists, first row is headers."""
		for row in rows:
			self.tbl.appendRow(row)

	# =================================================================
	# A. Parse + build filter
	# =================================================================

	def test_A01_build_match_returns_entries(self):
		"""Rows matching build_str are parsed into the result dict."""
		self._seed([
			['name', 'type', 'min_children', 'build'],
			['3DScope', 'baseCOMP', '2', '099.2025.32280'],
			['TDVR', 'containerCOMP', '13', '099.2025.32280'],
		])
		out = self.cat._parseBootstrapPaletteTable(self.tbl, '099.2025.32280')
		self.assertIsNotNone(out)
		self.assertEqual(len(out), 2)
		self.assertEqual(
			out['3DScope'], {'type': 'baseCOMP', 'min_children': 2})
		self.assertEqual(
			out['TDVR'], {'type': 'containerCOMP', 'min_children': 13})

	def test_A02_build_mismatch_returns_none(self):
		"""No rows match the requested build → None (triggers runtime scan)."""
		self._seed([
			['name', 'type', 'min_children', 'build'],
			['3DScope', 'baseCOMP', '2', '099.2025.32280'],
		])
		out = self.cat._parseBootstrapPaletteTable(self.tbl, '099.2026.99999')
		self.assertIsNone(out)

	def test_A03_multi_build_filters_correctly(self):
		"""Rows for other builds are ignored."""
		self._seed([
			['name', 'type', 'min_children', 'build'],
			['3DScope', 'baseCOMP', '2', '099.2025.32280'],
			['3DScope', 'baseCOMP', '3', '099.2026.33000'],
			['TDVR', 'containerCOMP', '13', '099.2026.33000'],
		])
		out = self.cat._parseBootstrapPaletteTable(self.tbl, '099.2025.32280')
		self.assertEqual(len(out), 1)
		self.assertEqual(out['3DScope']['min_children'], 2)

	# =================================================================
	# B. Edge cases
	# =================================================================

	def test_B01_empty_table_returns_none(self):
		"""A table with only a header row (or less) → None."""
		out = self.cat._parseBootstrapPaletteTable(self.tbl, '099.2025.32280')
		self.assertIsNone(out)

	def test_B02_missing_table_returns_none(self):
		"""None table → None."""
		out = self.cat._parseBootstrapPaletteTable(None, '099.2025.32280')
		self.assertIsNone(out)

	def test_B03_bad_schema_returns_none(self):
		"""Missing required column → None + warning (no raise)."""
		self._seed([
			['name', 'type', 'build'],  # missing min_children
			['3DScope', 'baseCOMP', '099.2025.32280'],
		])
		out = self.cat._parseBootstrapPaletteTable(self.tbl, '099.2025.32280')
		self.assertIsNone(out)

	def test_B04_dedup_first_wins(self):
		"""Duplicate name within the same build → first row wins."""
		self._seed([
			['name', 'type', 'min_children', 'build'],
			['abletonLink', 'baseCOMP', '2', '099.2025.32280'],
			['abletonLink', 'containerCOMP', '99', '099.2025.32280'],
		])
		out = self.cat._parseBootstrapPaletteTable(self.tbl, '099.2025.32280')
		self.assertEqual(out['abletonLink']['type'], 'baseCOMP')
		self.assertEqual(out['abletonLink']['min_children'], 2)

	def test_B05_non_integer_min_children_defaults_to_zero(self):
		"""Malformed min_children cell → 0, not a raise."""
		self._seed([
			['name', 'type', 'min_children', 'build'],
			['weird', 'baseCOMP', 'NaN', '099.2025.32280'],
		])
		out = self.cat._parseBootstrapPaletteTable(self.tbl, '099.2025.32280')
		self.assertEqual(out['weird']['min_children'], 0)

	def test_B06_empty_name_row_skipped(self):
		"""Rows with empty name column are skipped."""
		self._seed([
			['name', 'type', 'min_children', 'build'],
			['', 'baseCOMP', '2', '099.2025.32280'],
			['real', 'baseCOMP', '2', '099.2025.32280'],
		])
		out = self.cat._parseBootstrapPaletteTable(self.tbl, '099.2025.32280')
		self.assertEqual(list(out.keys()), ['real'])

	# =================================================================
	# C. Live table sanity (the actual shipped catalog)
	# =================================================================

	def test_C01_shipped_catalog_covers_current_build(self):
		"""The shipped palette_catalog must have entries for this TD build.

		Guards against releases where the TSV wasn't updated for a new
		supported build. A miss means new users fall back to the runtime
		palette scan (~5-7s on first open).
		"""
		build_str = f'{app.version}.{app.build}'
		live_table = self.embody.op('palette_catalog')
		if live_table is None:
			self.skipTest('palette_catalog tableDAT not present yet')
		out = self.cat._parseBootstrapPaletteTable(live_table, build_str)
		self.assertIsNotNone(out,
			f'Shipped palette_catalog has no rows for build {build_str}. '
			f'Run CatalogManager.ExportPaletteCatalog() after a fresh scan '
			f'to regenerate.')
		self.assertGreater(len(out), 100,
			f'Shipped palette_catalog has only {len(out)} entries for '
			f'{build_str} -- expected >100.')
