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
		"""name + OPType match against catalog -> detected as palette."""
		self.tdn._palette_catalog = {
			'myWidget': {'type': 'containerCOMP', 'min_children': 0},
		}
		comp = self.sandbox.create(containerCOMP, 'myWidget')
		self.assertTrue(self.tdn._isPaletteClone(comp),
			'Catalog entry with matching name+type must be detected')

	def test_A02_catalog_lookup_wrong_type_rejected(self):
		"""name match but wrong OPType -> not detected via catalog."""
		self.tdn._palette_catalog = {
			'myWidget': {'type': 'containerCOMP', 'min_children': 0},
		}
		# baseCOMP != containerCOMP -> catalog match fails.
		# Empty clone parameter -> heuristic fallback also returns False.
		comp = self.sandbox.create(baseCOMP, 'myWidget')
		self.assertFalse(self.tdn._isPaletteClone(comp),
			'Wrong OPType must not match catalog entry')

	# =================================================================
	# B. Child Count Floor
	# =================================================================

	def test_B01_child_count_floor_rejects_empty_user_comp(self):
		"""Empty user COMP with palette name -> rejected by floor check."""
		self.tdn._palette_catalog = {
			'buttonCheckbox': {'type': 'containerCOMP', 'min_children': 10},
		}
		# floor = max(1, 10//2) = 5; COMP has 0 children -> rejected
		comp = self.sandbox.create(containerCOMP, 'buttonCheckbox')
		self.assertFalse(self.tdn._isPaletteClone(comp),
			'Empty COMP must not match palette entry with min_children=10')

	def test_B02_child_count_floor_tolerates_user_mods(self):
		"""User who kept most palette children (half count) -> still detected."""
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
		"""min_children=1 -> floor 1; 0 children fails, 1+ passes."""
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
		"""min_children=0 -> floor 0, any child count passes."""
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
		"""op.TDBasicWidgets.* clone expr -> detected via heuristic fallback."""
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
	# D2. Broken Clone Expressions (issue #46)
	# =================================================================
	# A clone expression that RAISES on eval (e.g. a widget master that
	# no longer exists) must never abort detection or export. bool(Par)
	# evaluates the parameter, so even a truthiness check can raise.

	def test_D02_broken_clone_expr_no_crash(self):
		"""Raising clone expression -> _isPaletteClone returns False, no raise."""
		self.tdn._palette_catalog = {}
		comp = self.sandbox.create(containerCOMP, 'brokenClone')
		# op() returns None -> .path raises -> par eval raises tdError
		comp.par.clone.expr = "op('nonexistent_widget_master').path"
		self.assertFalse(self.tdn._isPaletteClone(comp),
			'Broken non-widget clone expression must return False, not raise')

	def test_D03_broken_widget_clone_not_palette(self):
		"""Raising expr naming TDBasicWidgets -> NOT a palette clone.

		Blackboxing omits children on the promise the clone master
		restores them. A broken clone means the master is missing, so
		the only faithful serialization is a full export.
		"""
		self.tdn._palette_catalog = {}
		comp = self.sandbox.create(containerCOMP, 'brokenWidgetClone')
		comp.par.clone.expr = "op('TDBasicWidgets_missing_master').path"
		self.assertFalse(self.tdn._isPaletteClone(comp),
			'Broken clone must export fully, never blackbox')

	def test_D04_clone_source_diffs_broken_clone(self):
		"""_getCloneSourceDiffs on a broken clone -> empty dict, no raise."""
		comp = self.sandbox.create(containerCOMP, 'brokenDiffs')
		comp.par.clone.expr = "op('nonexistent_widget_master').path"
		self.assertEqual(self.tdn._getCloneSourceDiffs(comp), {},
			'Broken clone expression must yield no clone-source diffs')

	def test_D05_broken_clone_export_succeeds_with_children(self):
		"""Export containing a broken-clone COMP succeeds and keeps children."""
		holder = self.sandbox.create(containerCOMP, 'issue46')
		child = holder.create(containerCOMP, 'rollover')
		child.par.enablecloning = True
		# Widget-substring expr: even matching the palette heuristic,
		# a broken clone must serialize its children.
		child.par.clone.expr = "op('TDBasicWidgets_missing_master').path"
		inner = child.create(textDAT, 'guts')
		inner.text = 'local content the missing master cannot restore'
		result = self.tdn.ExportNetwork(root_path=holder.path)
		self.assertTrue(result.get('success'),
			f"Export must succeed despite broken clone: {result.get('error')}")
		ops = result.get('tdn', {}).get('operators', [])
		rollover = next((o for o in ops if o.get('name') == 'rollover'), None)
		self.assertIsNotNone(rollover,
			'Broken-clone COMP must still be serialized')
		self.assertEqual(
			rollover.get('parameters', {}).get('clone'),
			"=op('TDBasicWidgets_missing_master').path",
			'Clone expression must round-trip as text, never evaluated')
		self.assertFalse(rollover.get('palette_clone', False),
			'Broken clone must not carry the palette_clone marker')
		child_names = [o.get('name') for o in rollover.get('children', [])]
		self.assertIn('guts', child_names,
			'Broken-clone COMP children must be serialized (no blackbox)')

	def test_D06_broken_clone_roundtrip(self):
		"""Broken-clone network survives export -> import intact."""
		holder = self.sandbox.create(containerCOMP, 'issue46_rt')
		child = holder.create(containerCOMP, 'rollover')
		child.par.enablecloning = True
		child.par.clone.expr = "op('TDBasicWidgets_missing_master').path"
		inner = child.create(textDAT, 'guts')
		inner.text = 'survive me'
		exported = self.tdn.ExportNetwork(
			root_path=holder.path, include_dat_content=True)
		self.assertTrue(exported.get('success'))

		dest = self.sandbox.create(containerCOMP, 'issue46_rt_dest')
		imported = self.tdn.ImportNetwork(dest.path, exported['tdn'])
		self.assertTrue(imported.get('success'),
			f"Import must succeed: {imported.get('error')}")
		new_rollover = dest.op('rollover')
		self.assertIsNotNone(new_rollover, 'rollover must be rebuilt')
		self.assertEqual(new_rollover.par.clone.expr,
			"op('TDBasicWidgets_missing_master').path",
			'Broken clone expression must be restored as text')
		new_guts = new_rollover.op('guts')
		self.assertIsNotNone(new_guts, 'children must be rebuilt')
		self.assertEqual(new_guts.text, 'survive me',
			'child content must survive the round-trip')

	def test_D07_cloning_disabled_exports_fully(self):
		"""Catalog-classified COMP with cloning OFF -> full export.

		The TauCeti widgets: enablecloning=False, local authored
		children. Classification may say palette, but the restorability
		gate must force a full export -- blackboxing would gut them.
		"""
		self.tdn._palette_catalog = {
			'gatedWidget': {'type': 'containerCOMP', 'min_children': 0},
		}
		master = self.sandbox.create(containerCOMP, 'gated_master')
		comp = self.sandbox.create(containerCOMP, 'gatedWidget')
		comp.par.clone = 'gated_master'
		comp.par.enablecloning = False
		local = comp.create(textDAT, 'local_guts')
		local.text = 'authored content'
		self.assertTrue(self.tdn._isPaletteClone(comp),
			'Classification (catalog) must still match')
		self.assertFalse(self.tdn._cloneRestorable(comp),
			'Disabled cloning must fail the restorability gate')
		result = self.tdn.ExportNetwork(root_path=self.sandbox.path)
		self.assertTrue(result.get('success'))
		entry = next(o for o in result['tdn']['operators']
			if o.get('name') == 'gatedWidget')
		self.assertFalse(entry.get('palette_clone', False),
			'Unrestorable clone must not carry the blackbox marker')
		child_names = [o.get('name') for o in entry.get('children', [])]
		self.assertIn('local_guts', child_names,
			'Local children must be serialized when cloning is off')

	def test_D08_unresolvable_master_exports_fully(self):
		"""Defensive non-raising widget expr with no master -> full export.

		The exact TauCeti fadetime shape:
		"op.TDBasicWidgets.op(...) if hasattr(op, ...) else ''" --
		evaluates to None without raising, classification matches the
		string heuristic, but nothing can restore a blackboxed shell.
		"""
		self.tdn._palette_catalog = {}
		comp = self.sandbox.create(containerCOMP, 'defensiveWidget')
		comp.par.enablecloning = False
		comp.par.clone.expr = (
			"op.TDBasicWidgets.op('sliderHorz') "
			"if hasattr(op, 'TDBasicWidgets_never') else ''")
		local = comp.create(textDAT, 'local_guts')
		local.text = 'authored content'
		self.assertTrue(self.tdn._isPaletteClone(comp),
			'String heuristic must classify the widget expr')
		self.assertFalse(self.tdn._cloneRestorable(comp),
			'Unresolvable master must fail the restorability gate')
		result = self.tdn.ExportNetwork(root_path=self.sandbox.path)
		self.assertTrue(result.get('success'))
		entry = next(o for o in result['tdn']['operators']
			if o.get('name') == 'defensiveWidget')
		self.assertFalse(entry.get('palette_clone', False),
			'Unrestorable clone must not carry the blackbox marker')
		child_names = [o.get('name') for o in entry.get('children', [])]
		self.assertIn('local_guts', child_names,
			'Local children must be serialized when master is missing')

	def test_D09_restorable_blackbox_roundtrip(self):
		"""True palette clone blackbox: clone ref kept, rebuild re-clones.

		The exported .tdn must carry the clone reference (older versions
		stripped it, leaving rebuilds empty), the rebuilt shell must
		refill from the master, and explicit exported values must win
		over master values (the buttontype problem).
		"""
		self.tdn._palette_catalog = {
			'trueClone': {'type': 'containerCOMP', 'min_children': 0},
		}
		master = self.sandbox.create(containerCOMP, 'true_master')
		payload = master.create(textDAT, 'payload')
		payload.text = 'from master'
		master.par.w = 250
		clone = self.sandbox.create(containerCOMP, 'trueClone')
		clone.par.enablecloning = True
		clone.par.clone = 'true_master'
		clone.cook(force=True)
		self.assertGreater(len(clone.children), 0,
			'Live clone must have populated from master')
		clone.par.w = 333  # user customization, differs from master

		exported = self.tdn.ExportNetwork(
			root_path=self.sandbox.path, include_dat_content=True)
		self.assertTrue(exported.get('success'))
		entry = next(o for o in exported['tdn']['operators']
			if o.get('name') == 'trueClone')
		self.assertTrue(entry.get('palette_clone', False),
			'Restorable palette clone must blackbox')
		self.assertNotIn('children', entry,
			'Blackboxed entry must omit children')
		self.assertIn('clone', entry.get('parameters', {}),
			'Clone reference must be KEPT so the rebuilt shell can '
			're-clone (stripping it left rebuilds empty)')

		dest = self.sandbox.create(containerCOMP, 'd09_dest')
		imported = self.tdn.ImportNetwork(dest.path, exported['tdn'])
		self.assertTrue(imported.get('success'),
			f"Import must succeed: {imported.get('error')}")
		rebuilt = dest.op('trueClone')
		self.assertIsNotNone(rebuilt)
		rebuilt.cook(force=True)
		self.assertIsNotNone(rebuilt.op('payload'),
			'Rebuilt shell must refill children by re-cloning')
		self.assertEqual(int(rebuilt.par.w.eval()), 333,
			'Explicit exported value must win over the master value')

	def test_D10_divergent_enabled_clone_export_faithful(self):
		"""Diverged ENABLED clone: export is faithful; rebuild is bounded.

		TD fires a clone-establishment sync whenever clone/enablecloning
		is set programmatically, and that sync deletes non-master
		children regardless of import ordering (verified empirically:
		establish-then-import, import-then-establish, and enable-last
		all wipe). Only TD's native .toe/.tox loader restores a
		diverged enabled clone -- such COMPs belong in TOX strategy or
		tdn_exclude. TDN's contract: the .tdn itself must capture the
		divergence faithfully, and the import must succeed with the
		master-derived content intact.
		"""
		master = self.sandbox.create(containerCOMP, 'd10_master')
		master.create(textDAT, 'base_a')
		clone = self.sandbox.create(containerCOMP, 'd10_clone')
		clone.par.enablecloning = True
		clone.par.clone = 'd10_master'
		clone.cook(force=True)
		extra = clone.create(textDAT, 'diverged_b')
		extra.text = 'local divergence'

		exported = self.tdn.ExportNetwork(
			root_path=self.sandbox.path, include_dat_content=True)
		self.assertTrue(exported.get('success'))
		entry = next(o for o in exported['tdn']['operators']
			if o.get('name') == 'd10_clone')
		child_names = [c.get('name') for c in entry.get('children', [])]
		self.assertIn('diverged_b', child_names,
			'Diverged child must be serialized in the export')
		self.assertIn('base_a', child_names,
			'Master-derived child must be serialized in the export')

		dest = self.sandbox.create(containerCOMP, 'd10_dest')
		imported = self.tdn.ImportNetwork(dest.path, exported['tdn'])
		self.assertTrue(imported.get('success'),
			f"Import must succeed: {imported.get('error')}")
		rebuilt = dest.op('d10_clone')
		self.assertIsNotNone(rebuilt)
		rebuilt.cook(force=True)
		self.assertIsNotNone(rebuilt.op('base_a'),
			'Master-derived child must be present after round-trip')

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
		"""animationCOMP internal data survives export -> destroy -> import."""
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
		"""Direct child of animationCOMP -> True."""
		anim = self.sandbox.create(animationCOMP, 'anim3')
		keys = anim.op('keys')
		self.assertIsNotNone(keys)
		self.assertTrue(self.tdn._isInsideAnimationCOMP(keys))

	def test_F02_inside_animationcomp_negative(self):
		"""DAT outside animationCOMP -> False."""
		dat = self.sandbox.create(tableDAT, 'plain_dat')
		self.assertFalse(self.tdn._isInsideAnimationCOMP(dat))

	# =================================================================
	# G. Palette Handling Resolver (_resolvePaletteHandling)
	# =================================================================

	def _fakePalette(self, name='myPalette', typ=containerCOMP):
		"""Create a COMP that the catalog recognizes as a palette clone.

		Blackbox eligibility requires a RESTORABLE clone (cloning on +
		resolving master), so the fixture wires one to a sandbox master.
		"""
		self.tdn._palette_catalog = {
			name: {'type': typ.__name__, 'min_children': 0},
		}
		master_name = f'{name}_master'
		if self.sandbox.op(master_name) is None:
			self.sandbox.create(typ, master_name)
		comp = self.sandbox.create(typ, name)
		comp.par.enablecloning = True
		comp.par.clone = master_name
		return comp

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
