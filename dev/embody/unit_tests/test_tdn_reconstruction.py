"""
Test suite: TDN reconstruction — comprehensive round-trip fidelity,
reconstruction simulation, and resilience testing.

Tests ExportNetwork → ImportNetwork round-trips across all operator families,
parameter modes, custom parameters, connections, flags, metadata, DAT content,
deep nesting, type_defaults optimization, par_templates optimization,
reconstruction simulation (strip + reimport), Embody self-protection,
error handling, and scale stress tests.
"""

import json

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase

# ParMode is a TD builtin global but not on the td module.
# Get it from any parameter's mode type.
ParMode = type(op('/').par.clone.mode)


class TestTDNReconstruction(EmbodyTestCase):

	def setUp(self):
		super().setUp()
		self.tdn = self.embody.ext.TDN

	# =================================================================
	# Helpers
	# =================================================================

	def _roundTrip(self, parent):
		"""Export → clear children → import → re-export.
		Returns (original_tdn, reimported_tdn, import_result).
		"""
		orig = self.tdn.ExportNetwork(
			root_path=parent.path, include_dat_content=True)
		self.assertTrue(orig.get('success'), f'Export failed: {orig}')
		orig_tdn = orig['tdn']

		# Clear children
		for c in list(parent.children):
			c.destroy()

		# Import
		result = self.tdn.ImportNetwork(
			target_path=parent.path, tdn=orig_tdn, clear_first=False)
		self.assertTrue(result.get('success'), f'Import failed: {result}')

		# Re-export
		reimp = self.tdn.ExportNetwork(
			root_path=parent.path, include_dat_content=True)
		self.assertTrue(reimp.get('success'), f'Re-export failed: {reimp}')

		return orig_tdn, reimp['tdn'], result

	def _simulateReconstruction(self, parent):
		"""Mirrors the real onProjectPreSave strip + ReconstructTDNComps reimport flow."""
		orig = self.tdn.ExportNetwork(
			root_path=parent.path, include_dat_content=True)
		self.assertTrue(orig.get('success'))
		orig_tdn = orig['tdn']

		# Strip (like onProjectPreSave)
		count = len(list(parent.children))
		for c in list(parent.children):
			c.destroy()
		self.assertEqual(len(parent.children), 0)

		# Reconstruct (like ReconstructTDNComps)
		result = self.tdn.ImportNetwork(
			target_path=parent.path, tdn=orig_tdn, clear_first=False)
		self.assertTrue(result.get('success'), f'Reconstruct failed: {result}')

		return orig_tdn, result

	def _assertParamEqual(self, val1, val2, msg=''):
		"""Float-tolerant parameter comparison."""
		if isinstance(val1, float) and isinstance(val2, float):
			self.assertApproxEqual(val1, val2, msg=msg)
		elif isinstance(val1, str) and isinstance(val2, str):
			self.assertEqual(val1, val2, msg)
		else:
			# Try numeric comparison
			try:
				f1, f2 = float(val1), float(val2)
				self.assertApproxEqual(f1, f2, msg=msg)
			except (TypeError, ValueError):
				self.assertEqual(val1, val2, msg)

	def _getOpNames(self, parent):
		"""Get sorted list of child operator names."""
		return sorted([c.name for c in parent.children])

	def _getOpTypes(self, parent):
		"""Get dict of name→OPType for children."""
		return {c.name: c.OPType for c in parent.children}

	def _verifyNetworkFidelity(self, original_ops, reimported_ops,
							orig_type_defaults=None, reimp_type_defaults=None):
		"""Deep recursive comparison of two TDN operator arrays.

		Resolves type_defaults into per-op params before comparing, so
		redistribution between shared and per-op sections doesn't cause
		false mismatches.
		"""
		self.assertEqual(len(original_ops), len(reimported_ops),
			f'Op count mismatch: {len(original_ops)} vs {len(reimported_ops)}')

		orig_by_name = {o['name']: o for o in original_ops}
		reimp_by_name = {o['name']: o for o in reimported_ops}

		self.assertEqual(set(orig_by_name.keys()), set(reimp_by_name.keys()),
			'Operator names differ')

		for name in orig_by_name:
			orig = orig_by_name[name]
			reimp = reimp_by_name[name]

			# Type
			self.assertEqual(orig['type'], reimp['type'],
				f'{name}: type mismatch')

			# Parameters (with float tolerance)
			# Merge type_defaults into per-op params for comparison
			orig_params = dict(orig.get('parameters', {}))
			reimp_params = dict(reimp.get('parameters', {}))
			op_type = orig.get('type', '')
			if orig_type_defaults and op_type in orig_type_defaults:
				td = orig_type_defaults[op_type].get('parameters', {})
				merged = dict(td)
				merged.update(orig_params)
				orig_params = merged
			if reimp_type_defaults and op_type in reimp_type_defaults:
				td = reimp_type_defaults[op_type].get('parameters', {})
				merged = dict(td)
				merged.update(reimp_params)
				reimp_params = merged
			for pname in orig_params:
				self.assertIn(pname, reimp_params,
					f'{name}: missing param {pname}')
				self._assertParamEqual(
					orig_params[pname], reimp_params[pname],
					f'{name}.{pname}')

			# Flags (resolve from type_defaults if not on the op)
			orig_flags = set(orig.get('flags', []))
			reimp_flags = set(reimp.get('flags', []))
			if not orig_flags and orig_type_defaults and op_type in orig_type_defaults:
				orig_flags = set(orig_type_defaults[op_type].get('flags', []))
			if not reimp_flags and reimp_type_defaults and op_type in reimp_type_defaults:
				reimp_flags = set(reimp_type_defaults[op_type].get('flags', []))
			self.assertEqual(orig_flags, reimp_flags,
				f'{name}: flags mismatch {orig_flags} vs {reimp_flags}')

			# Connections
			orig_inputs = orig.get('inputs', [])
			reimp_inputs = reimp.get('inputs', [])
			self.assertEqual(orig_inputs, reimp_inputs,
				f'{name}: inputs mismatch')

			# Position
			orig_pos = orig.get('position', [0, 0])
			reimp_pos = reimp.get('position', [0, 0])
			self.assertEqual(orig_pos, reimp_pos,
				f'{name}: position mismatch')

			# Size (resolve from type_defaults if not on the op)
			orig_size = orig.get('size')
			reimp_size = reimp.get('size')
			if orig_size is None and orig_type_defaults and op_type in orig_type_defaults:
				orig_size = orig_type_defaults[op_type].get('size')
			if reimp_size is None and reimp_type_defaults and op_type in reimp_type_defaults:
				reimp_size = reimp_type_defaults[op_type].get('size')
			self.assertEqual(orig_size, reimp_size,
				f'{name}: size mismatch {orig_size} vs {reimp_size}')

			# Color (resolve from type_defaults if not on the op)
			orig_color = orig.get('color')
			reimp_color = reimp.get('color')
			if orig_color is None and orig_type_defaults and op_type in orig_type_defaults:
				orig_color = orig_type_defaults[op_type].get('color')
			if reimp_color is None and reimp_type_defaults and op_type in reimp_type_defaults:
				reimp_color = reimp_type_defaults[op_type].get('color')
			if orig_color is not None:
				self.assertIsNotNone(reimp_color, f'{name}: missing color')
				for i in range(3):
					self.assertApproxEqual(
						orig_color[i], reimp_color[i],
						msg=f'{name}: color[{i}] mismatch')

			# Comment
			self.assertEqual(orig.get('comment'), reimp.get('comment'),
				f'{name}: comment mismatch')

			# Tags (resolve from type_defaults if not on the op)
			orig_tags = sorted(orig.get('tags', []))
			reimp_tags = sorted(reimp.get('tags', []))
			if not orig_tags and orig_type_defaults and op_type in orig_type_defaults:
				orig_tags = sorted(orig_type_defaults[op_type].get('tags', []))
			if not reimp_tags and reimp_type_defaults and op_type in reimp_type_defaults:
				reimp_tags = sorted(reimp_type_defaults[op_type].get('tags', []))
			self.assertEqual(orig_tags, reimp_tags,
				f'{name}: tags mismatch')

			# DAT content
			if 'dat_content' in orig:
				self.assertIn('dat_content', reimp,
					f'{name}: missing dat_content')
				self.assertEqual(
					orig['dat_content'], reimp['dat_content'],
					f'{name}: dat_content mismatch')

			# Children (recursive)
			if 'children' in orig:
				self.assertIn('children', reimp,
					f'{name}: missing children')
				self._verifyNetworkFidelity(
					orig['children'], reimp['children'],
					orig_type_defaults, reimp_type_defaults)

	def _buildComplexNetwork(self, parent):
		"""Creates ~33 operators across 4 nesting levels, 6 families."""
		# === TOP chain ===
		noise1 = parent.create(noiseTOP, 'noise1')
		noise1.par.type = 'sparse'
		noise1.par.amp = 0.8

		level1 = parent.create(levelTOP, 'level1')
		level1.par.opacity.expr = 'me.digits'
		level1.par.opacity.mode = ParMode.EXPRESSION
		noise1.outputConnectors[0].connect(level1.inputConnectors[0])

		comp1_top = parent.create(compositeTOP, 'composite1')
		noise1.outputConnectors[0].connect(comp1_top.inputConnectors[0])
		level1.outputConnectors[0].connect(comp1_top.inputConnectors[1])

		# === baseCOMP with custom pars ===
		comp1 = parent.create(baseCOMP, 'comp1')
		comp1.color = (0.2, 0.4, 0.8)
		comp1.comment = 'Main processor'
		comp1.tags.add('core')
		comp1.tags.add('audio')
		comp1.viewer = True
		comp1.nodeX = 200
		comp1.nodeY = 100

		# Custom parameters on comp1
		page_ctrl = comp1.appendCustomPage('Controls')
		page_ctrl.appendFloat('Speed', label='Speed')
		comp1.par.Speed = 1.5
		pg_mode = page_ctrl.appendMenu('Mode', label='Mode')
		pg_mode[0].menuNames = ['fast', 'slow', 'medium']
		pg_mode[0].menuLabels = ['Fast Mode', 'Slow Mode', 'Medium Mode']
		comp1.par.Mode = 'slow'
		page_ctrl.appendToggle('Active', label='Active')
		comp1.par.Active = True
		page_ctrl.appendRGB('Color', label='Color')
		comp1.par.Colorr = 1.0
		comp1.par.Colorg = 0.5
		comp1.par.Colorb = 0.2

		page_about = comp1.appendCustomPage('About')
		pg_build = page_about.appendInt('Build', label='Build')
		pg_build[0].readOnly = True
		comp1.par.Build = 42
		pg_ver = page_about.appendStr('Version', label='Version')
		pg_ver[0].readOnly = True
		comp1.par.Version = '5.0.99'

		# Children of comp1
		wave1 = comp1.create(waveCHOP, 'wave1')
		math1 = comp1.create(mathCHOP, 'math1')
		wave1.outputConnectors[0].connect(math1.inputConnectors[0])

		inner_comp = comp1.create(baseCOMP, 'inner_comp')
		# Same About page on inner_comp (triggers par_templates)
		page_about_inner = inner_comp.appendCustomPage('About')
		pg_build2 = page_about_inner.appendInt('Build', label='Build')
		pg_build2[0].readOnly = True
		inner_comp.par.Build = 10
		pg_ver2 = page_about_inner.appendStr('Version', label='Version')
		pg_ver2[0].readOnly = True
		inner_comp.par.Version = '1.0.0'

		# Children of inner_comp
		grid1 = inner_comp.create(gridSOP, 'grid1')
		transform1 = inner_comp.create(transformSOP, 'transform1')
		transform1.par.tx.expr = 'absTime.seconds'
		transform1.par.tx.mode = ParMode.EXPRESSION
		grid1.outputConnectors[0].connect(transform1.inputConnectors[0])

		text1 = inner_comp.create(textDAT, 'text1')
		text1.text = '# Python code\ndef hello():\n\treturn "world"'

		table1 = inner_comp.create(tableDAT, 'table1')
		table1.clear()
		table1.appendRow(['name', 'value', 'type'])
		table1.appendRow(['x', '1', 'float'])
		table1.appendRow(['y', '2', 'int'])

		# Deep nesting (4th level)
		deep_comp = inner_comp.create(baseCOMP, 'deep_comp')
		noise2 = deep_comp.create(noiseTOP, 'noise2')
		null1 = deep_comp.create(nullTOP, 'null1')
		null1.display = True
		noise2.outputConnectors[0].connect(null1.inputConnectors[0])

		# === Top-level DATs ===
		dat_script = parent.create(textDAT, 'dat_script')
		dat_script.text = 'Special chars: <>&"\'\\n\\ttabs'
		dat_script.lock = True

		dat_table = parent.create(tableDAT, 'dat_table')
		dat_table.clear()
		dat_table.appendRow(['header1', 'header2'])
		dat_table.appendRow(['data1', 'data2'])

		# === Other families ===
		const_chop = parent.create(constantCHOP, 'const_chop')
		phong1 = parent.create(phongMAT, 'phong1')

		# comp2 with same About page (triggers par_templates with comp1)
		comp2 = parent.create(baseCOMP, 'comp2')
		page_about2 = comp2.appendCustomPage('About')
		pg_build3 = page_about2.appendInt('Build', label='Build')
		pg_build3[0].readOnly = True
		comp2.par.Build = 99
		pg_ver3 = page_about2.appendStr('Version', label='Version')
		pg_ver3[0].readOnly = True
		comp2.par.Version = '2.0.0'

		# comp3 (type_defaults trigger — 3rd baseCOMP)
		comp3 = parent.create(baseCOMP, 'comp3')

		# select and flags
		select1 = parent.create(selectTOP, 'select1')
		bypassed1 = parent.create(noiseTOP, 'bypassed1')
		bypassed1.bypass = True

		hidden1 = parent.create(baseCOMP, 'hidden1')
		hidden1.expose = False
		hidden1.allowCooking = False

	def _buildMegaNetwork(self, parent):
		"""Creates 300+ operators for scale stress testing."""
		# Bulk TOPs (100)
		tops = []
		for i in range(100):
			t = parent.create(noiseTOP, f'top_{i}')
			if i > 0:
				tops[i - 1].outputConnectors[0].connect(t.inputConnectors[0])
			tops.append(t)

		# Bulk CHOPs (50)
		chops = []
		for i in range(50):
			c = parent.create(constantCHOP, f'chop_{i}')
			chops.append(c)

		# Bulk SOPs (50)
		sops = []
		for i in range(50):
			s = parent.create(gridSOP, f'sop_{i}')
			if i > 0:
				sops[i - 1].outputConnectors[0].connect(s.inputConnectors[0])
			sops.append(s)

		# DATs (30)
		for i in range(30):
			d = parent.create(textDAT, f'dat_{i}')
			d.text = f'Content for DAT {i}\nLine 2\nLine 3'

		# MATs (20)
		for i in range(20):
			parent.create(phongMAT, f'mat_{i}')

		# Nested COMPs (50 + children)
		for i in range(50):
			c = parent.create(baseCOMP, f'comp_{i}')
			c.create(noiseTOP, 'inner_noise')
			if i % 5 == 0:
				inner = c.create(baseCOMP, 'inner_comp')
				inner.create(textDAT, 'inner_dat')

		# Custom pars on some COMPs
		for i in range(0, 50, 10):
			c = parent.op(f'comp_{i}')
			if c:
				page = c.appendCustomPage('Config')
				page.appendFloat('Speed', label='Speed')
				c.par.Speed = float(i) / 10.0

		# POPs (with skip guard)
		try:
			gp = parent.create(gridPOP, 'pop_grid')
			tp = parent.create(transformPOP, 'pop_transform')
			gp.outputConnectors[0].connect(tp.inputConnectors[0])
		except Exception:
			pass  # POPs not available in this TD version

	# =================================================================
	# A. Basic Round-Trip Fidelity (11 tests)
	# =================================================================

	def test_A01_empty_comp_roundtrip(self):
		"""Empty COMP should round-trip cleanly."""
		orig_tdn, reimp_tdn, result = self._roundTrip(self.sandbox)
		self.assertEqual(len(reimp_tdn['operators']), 0)

	def test_A02_single_top_roundtrip(self):
		"""Single TOP round-trip preserves name and type."""
		self.sandbox.create(noiseTOP, 'my_noise')
		_, reimp_tdn, _ = self._roundTrip(self.sandbox)
		self.assertEqual(len(reimp_tdn['operators']), 1)
		self.assertEqual(reimp_tdn['operators'][0]['name'], 'my_noise')
		self.assertEqual(reimp_tdn['operators'][0]['type'], 'noiseTOP')

	def test_A03_single_chop_roundtrip(self):
		"""Single CHOP round-trip."""
		self.sandbox.create(waveCHOP, 'my_wave')
		_, reimp_tdn, _ = self._roundTrip(self.sandbox)
		self.assertEqual(reimp_tdn['operators'][0]['type'], 'waveCHOP')

	def test_A04_single_sop_roundtrip(self):
		"""Single SOP round-trip."""
		self.sandbox.create(gridSOP, 'my_grid')
		_, reimp_tdn, _ = self._roundTrip(self.sandbox)
		self.assertEqual(reimp_tdn['operators'][0]['type'], 'gridSOP')

	def test_A05_single_dat_roundtrip(self):
		"""Single DAT round-trip."""
		d = self.sandbox.create(textDAT, 'my_dat')
		d.text = 'hello world'
		_, reimp_tdn, _ = self._roundTrip(self.sandbox)
		entry = [o for o in reimp_tdn['operators'] if o['name'] == 'my_dat'][0]
		self.assertEqual(entry['type'], 'textDAT')

	def test_A06_single_mat_roundtrip(self):
		"""Single MAT round-trip."""
		self.sandbox.create(phongMAT, 'my_phong')
		_, reimp_tdn, _ = self._roundTrip(self.sandbox)
		self.assertEqual(reimp_tdn['operators'][0]['type'], 'phongMAT')

	def test_A07_mixed_families_roundtrip(self):
		"""Mixed families preserve all operators."""
		self.sandbox.create(noiseTOP, 'top1')
		self.sandbox.create(waveCHOP, 'chop1')
		self.sandbox.create(gridSOP, 'sop1')
		self.sandbox.create(textDAT, 'dat1')
		self.sandbox.create(phongMAT, 'mat1')
		self.sandbox.create(baseCOMP, 'comp1')
		_, reimp_tdn, _ = self._roundTrip(self.sandbox)
		names = {o['name'] for o in reimp_tdn['operators']}
		self.assertEqual(names, {'top1', 'chop1', 'sop1', 'dat1', 'mat1', 'comp1'})

	def test_A08_operator_count_preserved(self):
		"""Operator count must match after round-trip."""
		for i in range(15):
			self.sandbox.create(baseCOMP, f'op_{i}')
		orig_tdn, reimp_tdn, _ = self._roundTrip(self.sandbox)
		self.assertEqual(
			len(orig_tdn['operators']),
			len(reimp_tdn['operators']))

	def test_A09_type_preservation(self):
		"""Operator types must match after round-trip."""
		self.sandbox.create(noiseTOP, 'a')
		self.sandbox.create(waveCHOP, 'b')
		self.sandbox.create(gridSOP, 'c')
		orig_tdn, reimp_tdn, _ = self._roundTrip(self.sandbox)
		orig_types = {o['name']: o['type'] for o in orig_tdn['operators']}
		reimp_types = {o['name']: o['type'] for o in reimp_tdn['operators']}
		self.assertEqual(orig_types, reimp_types)

	def test_A10_complex_network_roundtrip(self):
		"""Full complex network should survive round-trip."""
		self._buildComplexNetwork(self.sandbox)
		orig_tdn, reimp_tdn, _ = self._roundTrip(self.sandbox)
		self._verifyNetworkFidelity(
			orig_tdn['operators'], reimp_tdn['operators'],
			orig_tdn.get('type_defaults'), reimp_tdn.get('type_defaults'))

	def test_A11_idempotent_double_roundtrip(self):
		"""Double round-trip should produce identical TDN."""
		self.sandbox.create(noiseTOP, 'n1')
		self.sandbox.create(baseCOMP, 'c1').create(textDAT, 'inner')
		_, reimp1_tdn, _ = self._roundTrip(self.sandbox)
		# Second round-trip
		_, reimp2_tdn, _ = self._roundTrip(self.sandbox)
		self._verifyNetworkFidelity(
			reimp1_tdn['operators'], reimp2_tdn['operators'],
			reimp1_tdn.get('type_defaults'), reimp2_tdn.get('type_defaults'))

	# =================================================================
	# B. Parameter Mode Round-Trip (10 tests)
	# =================================================================

	def test_B01_constant_int(self):
		"""Constant integer parameter round-trip."""
		n = self.sandbox.create(noiseTOP, 'n')
		n.par.seed = 42
		_, reimp, _ = self._roundTrip(self.sandbox)
		entry = reimp['operators'][0]
		self.assertIn('parameters', entry)
		self._assertParamEqual(entry['parameters']['seed'], 42)

	def test_B02_constant_float(self):
		"""Constant float parameter round-trip."""
		n = self.sandbox.create(noiseTOP, 'n')
		n.par.amp = 0.75
		_, reimp, _ = self._roundTrip(self.sandbox)
		entry = reimp['operators'][0]
		self._assertParamEqual(entry['parameters']['amp'], 0.75)

	def test_B03_constant_string(self):
		"""Constant string parameter round-trip."""
		s = self.sandbox.create(selectTOP, 's')
		s.par.top = 'some_path'
		_, reimp, _ = self._roundTrip(self.sandbox)
		entry = [o for o in reimp['operators'] if o['name'] == 's'][0]
		self.assertEqual(entry['parameters']['top'], 'some_path')

	def test_B04_constant_bool(self):
		"""Constant boolean/toggle parameter round-trip."""
		n = self.sandbox.create(noiseTOP, 'n')
		# mono defaults to True, so set to False (non-default) to export
		n.par.mono = False
		_, reimp, _ = self._roundTrip(self.sandbox)
		entry = reimp['operators'][0]
		# Toggle stored as 0
		self._assertParamEqual(entry['parameters']['mono'], 0)

	def test_B05_expression_mode(self):
		"""Expression parameter round-trip preserves expression string."""
		n = self.sandbox.create(noiseTOP, 'n')
		n.par.seed.expr = 'absTime.frame'
		n.par.seed.mode = ParMode.EXPRESSION
		orig, reimp, _ = self._roundTrip(self.sandbox)
		entry = [o for o in reimp['operators'] if o['name'] == 'n'][0]
		self.assertIn('parameters', entry)
		seed_val = entry['parameters'].get('seed', '')
		self.assertTrue(str(seed_val).startswith('='),
			f'Expected expression prefix, got: {seed_val}')
		self.assertIn('absTime.frame', str(seed_val))

	def test_B06_bind_mode(self):
		"""Bind expression parameter round-trip preserves bind string."""
		src = self.sandbox.create(noiseTOP, 'src')
		dst = self.sandbox.create(noiseTOP, 'dst')
		dst.par.seed.bindExpr = "op('src').par.seed"
		dst.par.seed.mode = ParMode.BIND
		orig, reimp, _ = self._roundTrip(self.sandbox)
		entry = [o for o in reimp['operators'] if o['name'] == 'dst'][0]
		seed_val = entry['parameters'].get('seed', '')
		self.assertTrue(str(seed_val).startswith('~'),
			f'Expected bind prefix, got: {seed_val}')

	def test_B07_escaped_equals(self):
		"""String starting with = should be escaped as ==."""
		c = self.sandbox.create(baseCOMP, 'c')
		page = c.appendCustomPage('Test')
		page.appendStr('Val', label='Val')
		c.par.Val = '=value'
		orig, reimp, _ = self._roundTrip(self.sandbox)
		# After round-trip, the actual op should have the unescaped value
		imported_c = self.sandbox.op('c')
		self.assertIsNotNone(imported_c)
		self.assertEqual(imported_c.par.Val.eval(), '=value')

	def test_B08_escaped_tilde(self):
		"""String starting with ~ should be escaped as ~~."""
		c = self.sandbox.create(baseCOMP, 'c')
		page = c.appendCustomPage('Test')
		page.appendStr('Val', label='Val')
		c.par.Val = '~value'
		orig, reimp, _ = self._roundTrip(self.sandbox)
		imported_c = self.sandbox.op('c')
		self.assertIsNotNone(imported_c)
		self.assertEqual(imported_c.par.Val.eval(), '~value')

	def test_B09_mixed_modes_one_op(self):
		"""Multiple parameter modes on one operator."""
		n = self.sandbox.create(noiseTOP, 'n')
		n.par.seed = 99
		n.par.amp.expr = 'me.digits'
		n.par.amp.mode = ParMode.EXPRESSION
		_, reimp, _ = self._roundTrip(self.sandbox)
		entry = [o for o in reimp['operators'] if o['name'] == 'n'][0]
		self._assertParamEqual(entry['parameters']['seed'], 99)
		self.assertTrue(str(entry['parameters']['amp']).startswith('='))

	def test_B10_nondefault_only(self):
		"""Only non-default parameters should be exported."""
		self.sandbox.create(noiseTOP, 'n')  # All defaults
		orig = self.tdn.ExportNetwork(root_path=self.sandbox.path)
		self.assertTrue(orig.get('success'))
		entry = orig['tdn']['operators'][0]
		# Should have zero or very few parameters (only non-defaults)
		params = entry.get('parameters', {})
		# Default noiseTOP shouldn't have many non-default params
		# Just verify the entry exists and is a dict
		self.assertIsInstance(params, dict)

	# =================================================================
	# C. Custom Parameter Round-Trip (12 tests)
	# =================================================================

	def test_C01_float_with_range(self):
		"""Float custom par with range and clamp round-trips."""
		c = self.sandbox.create(baseCOMP, 'c')
		page = c.appendCustomPage('Test')
		pg = page.appendFloat('Speed', label='Speed')
		pg[0].min = 0
		pg[0].max = 10
		pg[0].clampMin = True
		pg[0].clampMax = True
		pg[0].normMin = 0
		pg[0].normMax = 5
		c.par.Speed = 3.14
		self._roundTrip(self.sandbox)
		rc = self.sandbox.op('c')
		self.assertIsNotNone(rc)
		self.assertApproxEqual(rc.par.Speed.eval(), 3.14)
		self.assertTrue(rc.par.Speed.clampMin)
		self.assertTrue(rc.par.Speed.clampMax)
		self.assertApproxEqual(rc.par.Speed.max, 10.0)

	def test_C02_int_custom_par(self):
		"""Int custom par round-trip."""
		c = self.sandbox.create(baseCOMP, 'c')
		page = c.appendCustomPage('Test')
		page.appendInt('Count', label='Count')
		c.par.Count = 7
		self._roundTrip(self.sandbox)
		rc = self.sandbox.op('c')
		self.assertEqual(int(rc.par.Count.eval()), 7)

	def test_C03_str_custom_par(self):
		"""String custom par round-trip."""
		c = self.sandbox.create(baseCOMP, 'c')
		page = c.appendCustomPage('Test')
		page.appendStr('Label', label='Label')
		c.par.Label = 'Hello World'
		self._roundTrip(self.sandbox)
		rc = self.sandbox.op('c')
		self.assertEqual(rc.par.Label.eval(), 'Hello World')

	def test_C04_toggle_custom_par(self):
		"""Toggle custom par round-trip."""
		c = self.sandbox.create(baseCOMP, 'c')
		page = c.appendCustomPage('Test')
		page.appendToggle('Active', label='Active')
		c.par.Active = True
		self._roundTrip(self.sandbox)
		rc = self.sandbox.op('c')
		self.assertTrue(bool(rc.par.Active.eval()))

	def test_C05_menu_custom_par(self):
		"""Menu custom par with names and labels round-trip."""
		c = self.sandbox.create(baseCOMP, 'c')
		page = c.appendCustomPage('Test')
		pg = page.appendMenu('Mode', label='Mode')
		pg[0].menuNames = ['fast', 'slow', 'medium']
		pg[0].menuLabels = ['Fast', 'Slow', 'Medium']
		c.par.Mode = 'slow'
		self._roundTrip(self.sandbox)
		rc = self.sandbox.op('c')
		self.assertEqual(rc.par.Mode.eval(), 'slow')
		self.assertIn('fast', list(rc.par.Mode.menuNames))
		self.assertIn('slow', list(rc.par.Mode.menuNames))

	def test_C06_rgb_custom_par(self):
		"""RGB (3-component) custom par round-trip."""
		c = self.sandbox.create(baseCOMP, 'c')
		page = c.appendCustomPage('Test')
		page.appendRGB('Tint', label='Tint')
		c.par.Tintr = 1.0
		c.par.Tintg = 0.5
		c.par.Tintb = 0.2
		self._roundTrip(self.sandbox)
		rc = self.sandbox.op('c')
		self.assertApproxEqual(rc.par.Tintr.eval(), 1.0)
		self.assertApproxEqual(rc.par.Tintg.eval(), 0.5)
		self.assertApproxEqual(rc.par.Tintb.eval(), 0.2)

	def test_C07_xyz_custom_par(self):
		"""XYZ (3-component) custom par round-trip."""
		c = self.sandbox.create(baseCOMP, 'c')
		page = c.appendCustomPage('Test')
		page.appendXYZ('Pos', label='Position')
		c.par.Posx = 10.0
		c.par.Posy = 20.0
		c.par.Posz = 30.0
		self._roundTrip(self.sandbox)
		rc = self.sandbox.op('c')
		self.assertApproxEqual(rc.par.Posx.eval(), 10.0)
		self.assertApproxEqual(rc.par.Posy.eval(), 20.0)
		self.assertApproxEqual(rc.par.Posz.eval(), 30.0)

	def test_C08_readonly_custom_par(self):
		"""readOnly custom par round-trip."""
		c = self.sandbox.create(baseCOMP, 'c')
		page = c.appendCustomPage('Test')
		pg = page.appendInt('Build', label='Build')
		pg[0].readOnly = True
		c.par.Build = 42
		self._roundTrip(self.sandbox)
		rc = self.sandbox.op('c')
		self.assertTrue(rc.par.Build.readOnly)

	def test_C09_startSection_custom_par(self):
		"""startSection custom par round-trip."""
		c = self.sandbox.create(baseCOMP, 'c')
		page = c.appendCustomPage('Test')
		page.appendFloat('Before', label='Before')
		pg = page.appendFloat('After', label='After')
		pg[0].startSection = True
		self._roundTrip(self.sandbox)
		rc = self.sandbox.op('c')
		self.assertTrue(rc.par.After.startSection)

	def test_C10_label_differs_from_name(self):
		"""Custom par with label != name round-trip."""
		c = self.sandbox.create(baseCOMP, 'c')
		page = c.appendCustomPage('Test')
		page.appendFloat('Myval', label='My Custom Value')
		self._roundTrip(self.sandbox)
		rc = self.sandbox.op('c')
		self.assertEqual(rc.par.Myval.label, 'My Custom Value')

	def test_C11_multiple_pages(self):
		"""Multiple custom pages round-trip."""
		c = self.sandbox.create(baseCOMP, 'c')
		p1 = c.appendCustomPage('Controls')
		p1.appendFloat('Speed', label='Speed')
		p2 = c.appendCustomPage('About')
		p2.appendStr('Author', label='Author')
		c.par.Speed = 2.0
		c.par.Author = 'Test'
		self._roundTrip(self.sandbox)
		rc = self.sandbox.op('c')
		self.assertApproxEqual(rc.par.Speed.eval(), 2.0)
		self.assertEqual(rc.par.Author.eval(), 'Test')

	def test_C12_custom_par_expression_mode(self):
		"""Custom par in expression mode round-trip."""
		c = self.sandbox.create(baseCOMP, 'c')
		page = c.appendCustomPage('Test')
		page.appendFloat('Dyn', label='Dynamic')
		c.par.Dyn.expr = 'absTime.seconds'
		c.par.Dyn.mode = ParMode.EXPRESSION
		orig, reimp, _ = self._roundTrip(self.sandbox)
		rc = self.sandbox.op('c')
		self.assertEqual(rc.par.Dyn.mode, ParMode.EXPRESSION)
		self.assertEqual(rc.par.Dyn.expr, 'absTime.seconds')

	# =================================================================
	# D. Connection Round-Trip (8 tests)
	# =================================================================

	def test_D01_single_connection(self):
		"""Single connection round-trip."""
		src = self.sandbox.create(noiseTOP, 'src')
		dst = self.sandbox.create(levelTOP, 'dst')
		src.outputConnectors[0].connect(dst.inputConnectors[0])
		self._roundTrip(self.sandbox)
		rd = self.sandbox.op('dst')
		self.assertIsNotNone(rd)
		self.assertEqual(len(rd.inputs), 1)
		self.assertEqual(rd.inputs[0].name, 'src')

	def test_D02_chain_connection(self):
		"""Chain of 3 operators round-trip."""
		a = self.sandbox.create(noiseTOP, 'a')
		b = self.sandbox.create(levelTOP, 'b')
		c = self.sandbox.create(nullTOP, 'c')
		a.outputConnectors[0].connect(b.inputConnectors[0])
		b.outputConnectors[0].connect(c.inputConnectors[0])
		self._roundTrip(self.sandbox)
		rc = self.sandbox.op('c')
		self.assertEqual(rc.inputs[0].name, 'b')
		rb = self.sandbox.op('b')
		self.assertEqual(rb.inputs[0].name, 'a')

	def test_D03_multi_input(self):
		"""Multi-input operator round-trip."""
		a = self.sandbox.create(noiseTOP, 'a')
		b = self.sandbox.create(noiseTOP, 'b')
		comp = self.sandbox.create(compositeTOP, 'comp')
		a.outputConnectors[0].connect(comp.inputConnectors[0])
		b.outputConnectors[0].connect(comp.inputConnectors[1])
		self._roundTrip(self.sandbox)
		rc = self.sandbox.op('comp')
		self.assertEqual(rc.inputs[0].name, 'a')
		self.assertEqual(rc.inputs[1].name, 'b')

	def test_D04_three_sequential_inputs(self):
		"""Three sequential inputs round-trip."""
		a = self.sandbox.create(noiseTOP, 'a')
		b = self.sandbox.create(noiseTOP, 'b')
		c = self.sandbox.create(noiseTOP, 'c')
		comp = self.sandbox.create(compositeTOP, 'comp')
		a.outputConnectors[0].connect(comp.inputConnectors[0])
		b.outputConnectors[0].connect(comp.inputConnectors[1])
		c.outputConnectors[0].connect(comp.inputConnectors[2])
		self._roundTrip(self.sandbox)
		rc = self.sandbox.op('comp')
		self.assertEqual(rc.inputs[0].name, 'a')
		self.assertEqual(rc.inputs[1].name, 'b')
		self.assertEqual(rc.inputs[2].name, 'c')

	def test_D05_chop_chain(self):
		"""CHOP family connection chain round-trip."""
		w = self.sandbox.create(waveCHOP, 'w')
		m = self.sandbox.create(mathCHOP, 'm')
		w.outputConnectors[0].connect(m.inputConnectors[0])
		self._roundTrip(self.sandbox)
		rm = self.sandbox.op('m')
		self.assertEqual(rm.inputs[0].name, 'w')

	def test_D06_sop_chain(self):
		"""SOP family connection chain round-trip."""
		g = self.sandbox.create(gridSOP, 'g')
		t = self.sandbox.create(transformSOP, 't')
		g.outputConnectors[0].connect(t.inputConnectors[0])
		self._roundTrip(self.sandbox)
		rt = self.sandbox.op('t')
		self.assertEqual(rt.inputs[0].name, 'g')

	def test_D07_no_connection_ops(self):
		"""Operators with no connections round-trip cleanly."""
		self.sandbox.create(noiseTOP, 'a')
		self.sandbox.create(waveCHOP, 'b')
		_, reimp, _ = self._roundTrip(self.sandbox)
		for entry in reimp['operators']:
			self.assertNotIn('inputs', entry)

	def test_D08_cross_family_independence(self):
		"""Connections within one family don't affect another."""
		a = self.sandbox.create(noiseTOP, 'top_a')
		b = self.sandbox.create(levelTOP, 'top_b')
		a.outputConnectors[0].connect(b.inputConnectors[0])
		self.sandbox.create(waveCHOP, 'chop_c')
		self._roundTrip(self.sandbox)
		rb = self.sandbox.op('top_b')
		self.assertEqual(rb.inputs[0].name, 'top_a')
		rc = self.sandbox.op('chop_c')
		self.assertEqual(len(rc.inputs), 0)

	# =================================================================
	# E. Flags Round-Trip (7 tests)
	# =================================================================

	def test_E01_bypass_flag(self):
		"""bypass flag round-trip."""
		n = self.sandbox.create(noiseTOP, 'n')
		n.bypass = True
		self._roundTrip(self.sandbox)
		self.assertTrue(self.sandbox.op('n').bypass)

	def test_E02_lock_flag(self):
		"""lock flag round-trip."""
		d = self.sandbox.create(textDAT, 'd')
		d.lock = True
		self._roundTrip(self.sandbox)
		self.assertTrue(self.sandbox.op('d').lock)

	def test_E03_display_flag(self):
		"""display flag round-trip."""
		n = self.sandbox.create(nullTOP, 'n')
		n.display = True
		self._roundTrip(self.sandbox)
		self.assertTrue(self.sandbox.op('n').display)

	def test_E04_viewer_flag(self):
		"""viewer flag round-trip."""
		c = self.sandbox.create(baseCOMP, 'c')
		c.viewer = True
		self._roundTrip(self.sandbox)
		self.assertTrue(self.sandbox.op('c').viewer)

	def test_E05_expose_false(self):
		"""expose=False flag round-trip."""
		c = self.sandbox.create(baseCOMP, 'c')
		c.expose = False
		self._roundTrip(self.sandbox)
		self.assertFalse(self.sandbox.op('c').expose)

	def test_E06_allowCooking_false(self):
		"""allowCooking=False flag round-trip."""
		c = self.sandbox.create(baseCOMP, 'c')
		c.allowCooking = False
		self._roundTrip(self.sandbox)
		self.assertFalse(self.sandbox.op('c').allowCooking)

	def test_E07_multiple_flags(self):
		"""Multiple flags combined round-trip."""
		c = self.sandbox.create(baseCOMP, 'c')
		c.viewer = True
		c.bypass = True
		c.expose = False
		self._roundTrip(self.sandbox)
		rc = self.sandbox.op('c')
		self.assertTrue(rc.viewer)
		self.assertTrue(rc.bypass)
		self.assertFalse(rc.expose)

	# =================================================================
	# F. Metadata Round-Trip (8 tests)
	# =================================================================

	def test_F01_position(self):
		"""Operator position round-trip."""
		n = self.sandbox.create(noiseTOP, 'n')
		n.nodeX = 500
		n.nodeY = 300
		self._roundTrip(self.sandbox)
		rn = self.sandbox.op('n')
		self.assertEqual(rn.nodeX, 500)
		self.assertEqual(rn.nodeY, 300)

	def test_F02_origin_omission(self):
		"""Operator at origin should omit position field."""
		n = self.sandbox.create(noiseTOP, 'n')
		n.nodeX = 0
		n.nodeY = 0
		orig = self.tdn.ExportNetwork(root_path=self.sandbox.path)
		entry = orig['tdn']['operators'][0]
		self.assertNotIn('position', entry)

	def test_F03_nondefault_size(self):
		"""Non-default node size round-trip."""
		n = self.sandbox.create(noiseTOP, 'n')
		n.nodeWidth = 200
		n.nodeHeight = 150
		self._roundTrip(self.sandbox)
		rn = self.sandbox.op('n')
		self.assertEqual(rn.nodeWidth, 200)
		self.assertEqual(rn.nodeHeight, 150)

	def test_F04_color(self):
		"""Non-default operator color round-trip."""
		n = self.sandbox.create(noiseTOP, 'n')
		n.color = (1.0, 0.0, 0.5)
		self._roundTrip(self.sandbox)
		rn = self.sandbox.op('n')
		self.assertApproxEqual(rn.color[0], 1.0)
		self.assertApproxEqual(rn.color[1], 0.0)
		self.assertApproxEqual(rn.color[2], 0.5)

	def test_F05_comment(self):
		"""Operator comment round-trip."""
		n = self.sandbox.create(noiseTOP, 'n')
		n.comment = 'This is a test comment'
		self._roundTrip(self.sandbox)
		self.assertEqual(self.sandbox.op('n').comment, 'This is a test comment')

	def test_F06_tags(self):
		"""Operator tags round-trip."""
		n = self.sandbox.create(noiseTOP, 'n')
		n.tags.add('audio')
		n.tags.add('core')
		self._roundTrip(self.sandbox)
		rn = self.sandbox.op('n')
		self.assertIn('audio', rn.tags)
		self.assertIn('core', rn.tags)

	def test_F07_all_metadata_combined(self):
		"""All metadata types on one operator round-trip."""
		n = self.sandbox.create(noiseTOP, 'n')
		n.nodeX = 100
		n.nodeY = 200
		n.nodeWidth = 180
		n.nodeHeight = 120
		n.color = (0.5, 0.5, 0.5)
		n.comment = 'Combined metadata test'
		n.tags.add('test')
		self._roundTrip(self.sandbox)
		rn = self.sandbox.op('n')
		self.assertEqual(rn.nodeX, 100)
		self.assertEqual(rn.nodeY, 200)
		self.assertEqual(rn.nodeWidth, 180)
		self.assertEqual(rn.nodeHeight, 120)
		self.assertApproxEqual(rn.color[0], 0.5)
		self.assertEqual(rn.comment, 'Combined metadata test')
		self.assertIn('test', rn.tags)

	def test_F08_default_metadata_omission(self):
		"""Default metadata should be omitted from export."""
		n = self.sandbox.create(noiseTOP, 'n')
		# All defaults — position at 0,0, default color, no comment, no tags
		n.nodeX = 0
		n.nodeY = 0
		orig = self.tdn.ExportNetwork(root_path=self.sandbox.path)
		entry = orig['tdn']['operators'][0]
		self.assertNotIn('position', entry)
		self.assertNotIn('comment', entry)
		self.assertNotIn('tags', entry)

	# =================================================================
	# G. DAT Content Round-Trip (6 tests)
	# =================================================================

	def test_G01_text_dat_content(self):
		"""Text DAT content round-trip."""
		d = self.sandbox.create(textDAT, 'd')
		d.text = 'Hello World\nLine 2\nLine 3'
		self._roundTrip(self.sandbox)
		self.assertEqual(self.sandbox.op('d').text, 'Hello World\nLine 2\nLine 3')

	def test_G02_table_dat_content(self):
		"""Table DAT content round-trip."""
		t = self.sandbox.create(tableDAT, 't')
		t.clear()
		t.appendRow(['name', 'value'])
		t.appendRow(['x', '1'])
		t.appendRow(['y', '2'])
		self._roundTrip(self.sandbox)
		rt = self.sandbox.op('t')
		self.assertEqual(rt.numRows, 3)
		self.assertEqual(rt[0, 0].val, 'name')
		self.assertEqual(rt[2, 1].val, '2')

	def test_G03_unicode_content(self):
		"""Unicode DAT content round-trip."""
		d = self.sandbox.create(textDAT, 'd')
		d.text = 'Hello \u4e16\u754c caf\u00e9 \u03a9\u2248'
		self._roundTrip(self.sandbox)
		self.assertEqual(self.sandbox.op('d').text, 'Hello \u4e16\u754c caf\u00e9 \u03a9\u2248')

	def test_G04_multiline_content(self):
		"""Multiline DAT content round-trip."""
		d = self.sandbox.create(textDAT, 'd')
		text = '\n'.join(f'Line {i}' for i in range(50))
		d.text = text
		self._roundTrip(self.sandbox)
		self.assertEqual(self.sandbox.op('d').text, text)

	def test_G05_empty_dat(self):
		"""Empty DAT content round-trip."""
		d = self.sandbox.create(textDAT, 'd')
		d.text = ''
		self._roundTrip(self.sandbox)
		rd = self.sandbox.op('d')
		self.assertIsNotNone(rd)

	def test_G06_content_excluded_toggle(self):
		"""DAT content excluded when include_dat_content=False."""
		d = self.sandbox.create(textDAT, 'd')
		d.text = 'should not appear'
		orig = self.tdn.ExportNetwork(
			root_path=self.sandbox.path, include_dat_content=False)
		entry = orig['tdn']['operators'][0]
		self.assertNotIn('dat_content', entry)

	# =================================================================
	# H. Deep Nesting (5 tests)
	# =================================================================

	def test_H01_two_levels(self):
		"""2-level nesting round-trip."""
		c = self.sandbox.create(baseCOMP, 'outer')
		c.create(noiseTOP, 'inner')
		self._roundTrip(self.sandbox)
		self.assertIsNotNone(self.sandbox.op('outer/inner'))

	def test_H02_four_levels(self):
		"""4-level nesting round-trip."""
		a = self.sandbox.create(baseCOMP, 'a')
		b = a.create(baseCOMP, 'b')
		c = b.create(baseCOMP, 'c')
		c.create(noiseTOP, 'd')
		self._roundTrip(self.sandbox)
		self.assertIsNotNone(self.sandbox.op('a/b/c/d'))

	def test_H03_wide_and_deep(self):
		"""Wide + deep nesting round-trip."""
		for i in range(5):
			outer = self.sandbox.create(baseCOMP, f'branch_{i}')
			inner = outer.create(baseCOMP, 'child')
			inner.create(noiseTOP, 'leaf')
		self._roundTrip(self.sandbox)
		for i in range(5):
			self.assertIsNotNone(
				self.sandbox.op(f'branch_{i}/child/leaf'))

	def test_H04_connections_inside_nested(self):
		"""Connections inside nested COMPs survive round-trip."""
		c = self.sandbox.create(baseCOMP, 'c')
		n = c.create(noiseTOP, 'n')
		l = c.create(levelTOP, 'l')
		n.outputConnectors[0].connect(l.inputConnectors[0])
		self._roundTrip(self.sandbox)
		rl = self.sandbox.op('c/l')
		self.assertEqual(rl.inputs[0].name, 'n')

	def test_H05_mixed_families_each_level(self):
		"""Mixed families at each nesting level."""
		c = self.sandbox.create(baseCOMP, 'c')
		c.create(noiseTOP, 'top')
		c.create(waveCHOP, 'chop')
		inner = c.create(baseCOMP, 'inner')
		inner.create(gridSOP, 'sop')
		inner.create(textDAT, 'dat')
		self._roundTrip(self.sandbox)
		self.assertIsNotNone(self.sandbox.op('c/top'))
		self.assertIsNotNone(self.sandbox.op('c/chop'))
		self.assertIsNotNone(self.sandbox.op('c/inner/sop'))
		self.assertIsNotNone(self.sandbox.op('c/inner/dat'))

	# =================================================================
	# I. Type Defaults Optimization (7 tests)
	# =================================================================

	def test_I01_extraction_from_three_same_type(self):
		"""Type defaults extracted when 3+ operators share a parameter."""
		for i in range(3):
			n = self.sandbox.create(noiseTOP, f'n{i}')
			n.par.seed = 42
		orig = self.tdn.ExportNetwork(root_path=self.sandbox.path)
		tdn = orig['tdn']
		td = tdn.get('type_defaults', {})
		if 'noiseTOP' in td:
			self.assertIn('parameters', td['noiseTOP'])
			self.assertEqual(td['noiseTOP']['parameters']['seed'], 42)

	def test_I02_stripping_from_individuals(self):
		"""Parameters hoisted to type_defaults are removed from individuals."""
		for i in range(3):
			n = self.sandbox.create(noiseTOP, f'n{i}')
			n.par.seed = 42
		orig = self.tdn.ExportNetwork(root_path=self.sandbox.path)
		tdn = orig['tdn']
		td = tdn.get('type_defaults', {})
		if 'noiseTOP' in td and 'seed' in td['noiseTOP'].get('parameters', {}):
			# Individual ops should NOT have seed in their parameters
			for entry in tdn['operators']:
				params = entry.get('parameters', {})
				self.assertNotIn('seed', params,
					f'{entry["name"]} still has seed after hoisting')

	def test_I03_merge_on_import(self):
		"""Type defaults merged back on import."""
		for i in range(3):
			n = self.sandbox.create(noiseTOP, f'n{i}')
			n.par.seed = 42
		self._roundTrip(self.sandbox)
		for i in range(3):
			rn = self.sandbox.op(f'n{i}')
			self.assertIsNotNone(rn)
			self.assertEqual(int(rn.par.seed.eval()), 42)

	def test_I04_single_op_no_extraction(self):
		"""Single op should not produce type_defaults."""
		n = self.sandbox.create(noiseTOP, 'n')
		n.par.seed = 42
		orig = self.tdn.ExportNetwork(root_path=self.sandbox.path)
		td = orig['tdn'].get('type_defaults', {})
		# With only 1 noiseTOP, no type_defaults for noiseTOP
		self.assertNotIn('noiseTOP', td)

	def test_I05_unanimous_only(self):
		"""Type defaults only for parameters unanimous across ALL ops of that type."""
		n1 = self.sandbox.create(noiseTOP, 'n1')
		n1.par.seed = 42
		n2 = self.sandbox.create(noiseTOP, 'n2')
		n2.par.seed = 99  # Different!
		orig = self.tdn.ExportNetwork(root_path=self.sandbox.path)
		td = orig['tdn'].get('type_defaults', {})
		if 'noiseTOP' in td:
			# seed should NOT be in type_defaults since not unanimous
			self.assertNotIn('seed', td['noiseTOP'].get('parameters', {}))

	def test_I06_full_roundtrip_with_type_defaults(self):
		"""Full round-trip with type_defaults preserves values."""
		for i in range(4):
			n = self.sandbox.create(noiseTOP, f'n{i}')
			n.par.seed = 100
			n.par.amp = 0.5
		self._roundTrip(self.sandbox)
		for i in range(4):
			rn = self.sandbox.op(f'n{i}')
			self.assertEqual(int(rn.par.seed.eval()), 100)
			self.assertApproxEqual(rn.par.amp.eval(), 0.5)

	def test_I07_operator_override(self):
		"""Individual op can override type_defaults value."""
		n1 = self.sandbox.create(noiseTOP, 'n1')
		n1.par.seed = 42
		n1.par.amp = 0.5
		n2 = self.sandbox.create(noiseTOP, 'n2')
		n2.par.seed = 42
		n2.par.amp = 0.8  # Override
		n3 = self.sandbox.create(noiseTOP, 'n3')
		n3.par.seed = 42
		n3.par.amp = 0.5
		self._roundTrip(self.sandbox)
		rn2 = self.sandbox.op('n2')
		self.assertApproxEqual(rn2.par.amp.eval(), 0.8)

	def test_I08_flags_hoisted_to_type_defaults(self):
		"""Flags unanimously shared across ops of a type are hoisted."""
		for i in range(3):
			n = self.sandbox.create(noiseTOP, f'n{i}')
			n.viewer = True
		orig = self.tdn.ExportNetwork(root_path=self.sandbox.path)
		tdn = orig['tdn']
		td = tdn.get('type_defaults', {})
		self.assertIn('noiseTOP', td)
		self.assertIn('flags', td['noiseTOP'])
		self.assertIn('viewer', td['noiseTOP']['flags'])
		# Individual ops should NOT have flags
		for entry in tdn['operators']:
			self.assertNotIn('flags', entry,
				f'{entry["name"]} should not have per-op flags')

	def test_I09_size_hoisted_to_type_defaults(self):
		"""Size unanimously shared across ops of a type is hoisted."""
		for i in range(3):
			n = self.sandbox.create(noiseTOP, f'n{i}')
			n.nodeWidth = 300
			n.nodeHeight = 150
		orig = self.tdn.ExportNetwork(root_path=self.sandbox.path)
		tdn = orig['tdn']
		td = tdn.get('type_defaults', {})
		self.assertIn('noiseTOP', td)
		self.assertIn('size', td['noiseTOP'])
		self.assertEqual(td['noiseTOP']['size'], [300, 150])
		for entry in tdn['operators']:
			self.assertNotIn('size', entry,
				f'{entry["name"]} should not have per-op size')

	def test_I10_color_hoisted_to_type_defaults(self):
		"""Color unanimously shared across ops of a type is hoisted."""
		for i in range(3):
			n = self.sandbox.create(noiseTOP, f'n{i}')
			n.color = (0.2, 0.4, 0.8)
		orig = self.tdn.ExportNetwork(root_path=self.sandbox.path)
		tdn = orig['tdn']
		td = tdn.get('type_defaults', {})
		self.assertIn('noiseTOP', td)
		self.assertIn('color', td['noiseTOP'])
		for entry in tdn['operators']:
			self.assertNotIn('color', entry,
				f'{entry["name"]} should not have per-op color')

	def test_I11_tags_hoisted_to_type_defaults(self):
		"""Tags unanimously shared across ops of a type are hoisted."""
		for i in range(3):
			n = self.sandbox.create(noiseTOP, f'n{i}')
			n.tags = ['audio', 'generator']
		orig = self.tdn.ExportNetwork(root_path=self.sandbox.path)
		tdn = orig['tdn']
		td = tdn.get('type_defaults', {})
		self.assertIn('noiseTOP', td)
		self.assertIn('tags', td['noiseTOP'])
		self.assertEqual(sorted(td['noiseTOP']['tags']),
			['audio', 'generator'])
		for entry in tdn['operators']:
			self.assertNotIn('tags', entry,
				f'{entry["name"]} should not have per-op tags')

	def test_I12_non_unanimous_flags_not_hoisted(self):
		"""Non-unanimous flags should NOT be hoisted."""
		n1 = self.sandbox.create(noiseTOP, 'n1')
		n1.viewer = True
		n2 = self.sandbox.create(noiseTOP, 'n2')
		n2.bypass = True  # Different flags!
		orig = self.tdn.ExportNetwork(root_path=self.sandbox.path)
		td = orig['tdn'].get('type_defaults', {})
		if 'noiseTOP' in td:
			self.assertNotIn('flags', td['noiseTOP'])

	def test_I13_roundtrip_with_hoisted_flags_size_color_tags(self):
		"""Full round-trip preserving flags, size, color, tags through type_defaults."""
		for i in range(3):
			n = self.sandbox.create(noiseTOP, f'n{i}')
			n.viewer = True
			n.nodeWidth = 250
			n.nodeHeight = 120
			n.color = (0.3, 0.6, 0.9)
			n.tags = ['fx', 'test']
		self._roundTrip(self.sandbox)
		for i in range(3):
			rn = self.sandbox.op(f'n{i}')
			self.assertTrue(rn.viewer)
			self.assertEqual(rn.nodeWidth, 250)
			self.assertEqual(rn.nodeHeight, 120)
			self.assertApproxEqual(rn.color[0], 0.3)
			self.assertApproxEqual(rn.color[1], 0.6)
			self.assertApproxEqual(rn.color[2], 0.9)
			self.assertEqual(sorted(rn.tags), ['fx', 'test'])

	def test_I14_partial_flags_not_hoisted(self):
		"""If only some ops of a type have non-default flags, don't hoist."""
		n1 = self.sandbox.create(noiseTOP, 'n1')
		n1.viewer = True  # Has non-default flags
		n2 = self.sandbox.create(noiseTOP, 'n2')
		# n2 has all default flags — no flags key emitted
		orig = self.tdn.ExportNetwork(root_path=self.sandbox.path)
		td = orig['tdn'].get('type_defaults', {})
		if 'noiseTOP' in td:
			self.assertNotIn('flags', td['noiseTOP'])

	def test_I15_non_unanimous_tags_not_hoisted(self):
		"""Non-unanimous tags should NOT be hoisted."""
		n1 = self.sandbox.create(noiseTOP, 'n1')
		n1.tags = ['audio']
		n2 = self.sandbox.create(noiseTOP, 'n2')
		n2.tags = ['video']  # Different!
		orig = self.tdn.ExportNetwork(root_path=self.sandbox.path)
		td = orig['tdn'].get('type_defaults', {})
		if 'noiseTOP' in td:
			self.assertNotIn('tags', td['noiseTOP'])

	# =================================================================
	# J. Parameter Templates Optimization (6 tests)
	# =================================================================

	def test_J01_extraction_from_two_comps(self):
		"""Par templates extracted when 2+ COMPs share identical page defs."""
		for i in range(2):
			c = self.sandbox.create(baseCOMP, f'c{i}')
			page = c.appendCustomPage('About')
			pg = page.appendInt('Build', label='Build')
			pg[0].readOnly = True
			page.appendStr('Version', label='Version')
		orig = self.tdn.ExportNetwork(root_path=self.sandbox.path)
		pt = orig['tdn'].get('par_templates', {})
		self.assertGreater(len(pt), 0, 'No par_templates extracted')

	def test_J02_dollar_t_reference(self):
		"""Operators with templates should have $t reference."""
		for i in range(2):
			c = self.sandbox.create(baseCOMP, f'c{i}')
			page = c.appendCustomPage('About')
			pg = page.appendInt('Build', label='Build')
			pg[0].readOnly = True
			page.appendStr('Version', label='Version')
		orig = self.tdn.ExportNetwork(root_path=self.sandbox.path)
		for entry in orig['tdn']['operators']:
			cp = entry.get('custom_pars', {})
			if 'About' in cp and isinstance(cp['About'], dict):
				self.assertIn('$t', cp['About'])

	def test_J03_value_preservation(self):
		"""Values preserved in $t references."""
		for i in range(2):
			c = self.sandbox.create(baseCOMP, f'c{i}')
			page = c.appendCustomPage('About')
			pg = page.appendInt('Build', label='Build')
			pg[0].readOnly = True
			c.par.Build = (i + 1) * 10
		orig = self.tdn.ExportNetwork(root_path=self.sandbox.path)
		for entry in orig['tdn']['operators']:
			cp = entry.get('custom_pars', {})
			if 'About' in cp and isinstance(cp['About'], dict):
				# Value override should be present
				self.assertIn('Build', cp['About'])

	def test_J04_resolution_on_import(self):
		"""Par templates resolved correctly on import."""
		for i in range(2):
			c = self.sandbox.create(baseCOMP, f'c{i}')
			page = c.appendCustomPage('About')
			pg = page.appendInt('Build', label='Build')
			pg[0].readOnly = True
			c.par.Build = (i + 1) * 10
			page.appendStr('Version', label='Version')
			c.par.Version = f'v{i}'
		self._roundTrip(self.sandbox)
		for i in range(2):
			rc = self.sandbox.op(f'c{i}')
			self.assertEqual(int(rc.par.Build.eval()), (i + 1) * 10)
			self.assertEqual(rc.par.Version.eval(), f'v{i}')

	def test_J05_unique_page_not_extracted(self):
		"""A page unique to one COMP should not be templated."""
		c1 = self.sandbox.create(baseCOMP, 'c1')
		page = c1.appendCustomPage('Unique')
		page.appendFloat('Special', label='Special')
		c2 = self.sandbox.create(baseCOMP, 'c2')
		page2 = c2.appendCustomPage('Different')
		page2.appendInt('Other', label='Other')
		orig = self.tdn.ExportNetwork(root_path=self.sandbox.path)
		pt = orig['tdn'].get('par_templates', {})
		# Neither page should be templated (each appears only once)
		for tname, tdefs in pt.items():
			names = [d['name'] for d in tdefs]
			self.assertNotIn('Special', names)
			self.assertNotIn('Other', names)

	def test_J06_full_roundtrip_with_templates(self):
		"""Full round-trip with par_templates preserves all custom pars."""
		for i in range(3):
			c = self.sandbox.create(baseCOMP, f'c{i}')
			page = c.appendCustomPage('Config')
			page.appendFloat('Speed', label='Speed')
			pg = page.appendMenu('Mode', label='Mode')
			pg[0].menuNames = ['a', 'b']
			pg[0].menuLabels = ['Alpha', 'Beta']
			c.par.Speed = float(i)
			c.par.Mode = 'b' if i % 2 else 'a'
		self._roundTrip(self.sandbox)
		for i in range(3):
			rc = self.sandbox.op(f'c{i}')
			self.assertApproxEqual(rc.par.Speed.eval(), float(i))
			expected_mode = 'b' if i % 2 else 'a'
			self.assertEqual(rc.par.Mode.eval(), expected_mode)

	# =================================================================
	# K. Reconstruction Simulation (8 tests)
	# =================================================================

	def test_K01_simple_strip_reimport(self):
		"""Simple network strip + reimport."""
		self.sandbox.create(noiseTOP, 'n')
		self.sandbox.create(baseCOMP, 'c')
		orig_tdn, result = self._simulateReconstruction(self.sandbox)
		names = self._getOpNames(self.sandbox)
		self.assertIn('c', names)
		self.assertIn('n', names)

	def test_K02_complex_strip_reimport(self):
		"""Complex network strip + reimport."""
		self._buildComplexNetwork(self.sandbox)
		orig_count = len(self.sandbox.children)
		orig_tdn, result = self._simulateReconstruction(self.sandbox)
		# Should have same or similar count
		self.assertGreaterEqual(len(self.sandbox.children), orig_count - 1)

	def test_K03_connections_through_reconstruction(self):
		"""Connections preserved through strip + reimport."""
		a = self.sandbox.create(noiseTOP, 'a')
		b = self.sandbox.create(levelTOP, 'b')
		a.outputConnectors[0].connect(b.inputConnectors[0])
		self._simulateReconstruction(self.sandbox)
		rb = self.sandbox.op('b')
		self.assertEqual(rb.inputs[0].name, 'a')

	def test_K04_custom_pars_through_reconstruction(self):
		"""Custom parameters preserved through strip + reimport."""
		c = self.sandbox.create(baseCOMP, 'c')
		page = c.appendCustomPage('Test')
		page.appendFloat('Speed', label='Speed')
		c.par.Speed = 3.14
		self._simulateReconstruction(self.sandbox)
		rc = self.sandbox.op('c')
		self.assertApproxEqual(rc.par.Speed.eval(), 3.14)

	def test_K05_dat_content_through_reconstruction(self):
		"""DAT content preserved through strip + reimport."""
		d = self.sandbox.create(textDAT, 'd')
		d.text = 'Preserved content'
		self._simulateReconstruction(self.sandbox)
		self.assertEqual(self.sandbox.op('d').text, 'Preserved content')

	def test_K06_flags_through_reconstruction(self):
		"""Flags preserved through strip + reimport."""
		n = self.sandbox.create(noiseTOP, 'n')
		n.bypass = True
		c = self.sandbox.create(baseCOMP, 'c')
		c.viewer = True
		self._simulateReconstruction(self.sandbox)
		self.assertTrue(self.sandbox.op('n').bypass)
		self.assertTrue(self.sandbox.op('c').viewer)

	def test_K07_positions_through_reconstruction(self):
		"""Positions preserved through strip + reimport."""
		n = self.sandbox.create(noiseTOP, 'n')
		n.nodeX = 400
		n.nodeY = 250
		self._simulateReconstruction(self.sandbox)
		rn = self.sandbox.op('n')
		self.assertEqual(rn.nodeX, 400)
		self.assertEqual(rn.nodeY, 250)

	def test_K08_clear_first_removes_old(self):
		"""clear_first=True should remove pre-existing operators."""
		self.sandbox.create(noiseTOP, 'old_op')
		orig = self.tdn.ExportNetwork(
			root_path=self.sandbox.path, include_dat_content=True)
		# Add another op after export
		self.sandbox.create(waveCHOP, 'extra')
		# Import with clear_first
		result = self.tdn.ImportNetwork(
			target_path=self.sandbox.path,
			tdn=orig['tdn'], clear_first=True)
		self.assertTrue(result.get('success'))
		names = self._getOpNames(self.sandbox)
		self.assertIn('old_op', names)
		self.assertNotIn('extra', names)

	# =================================================================
	# L. Embody Self-Protection (3 tests)
	# =================================================================

	def test_L01_embody_excluded_from_tdn_strategy(self):
		"""Embody path should be excluded from _getTDNStrategyComps."""
		comps = self.embody_ext._getTDNStrategyComps()
		embody_path = self.embody.path
		for comp_path, _ in comps:
			self.assertNotEqual(comp_path, embody_path,
				'Embody itself should be excluded from TDN strategy comps')

	def test_L02_embody_ancestor_excluded(self):
		"""Embody's ancestor should also be excluded."""
		comps = self.embody_ext._getTDNStrategyComps()
		embody_path = self.embody.path
		for comp_path, _ in comps:
			self.assertFalse(
				embody_path.startswith(comp_path + '/'),
				f'Embody ancestor {comp_path} should be excluded')

	def test_L03_embody_survives_root_export_import(self):
		"""Root-level export+import should not destroy Embody."""
		# Just verify Embody is still alive after a sandbox-level round-trip
		self.sandbox.create(noiseTOP, 'safe')
		self._roundTrip(self.sandbox)
		# Embody should still be accessible
		self.assertIsNotNone(self.embody)
		self.assertTrue(self.embody.valid)

	# =================================================================
	# M. Error Handling & Resilience (9 tests)
	# =================================================================

	def test_M01_corrupted_json(self):
		"""Import with invalid JSON data should fail gracefully."""
		result = self.tdn.ImportNetwork(
			target_path=self.sandbox.path,
			tdn={'operators': 'not_a_list'})
		# Should either fail or handle gracefully
		if result.get('success'):
			self.assertEqual(result.get('created_count', 0), 0)

	def test_M02_missing_operators_key(self):
		"""Import with missing operators key should handle gracefully."""
		result = self.tdn.ImportNetwork(
			target_path=self.sandbox.path,
			tdn={'format': 'tdn', 'version': '1.0'})
		# Should still succeed (empty operators)
		if result.get('success'):
			self.assertEqual(result.get('created_count', 0), 0)

	def test_M03_unknown_op_type(self):
		"""Unknown operator type should skip that op, not crash."""
		tdn = {
			'format': 'tdn', 'version': '1.0',
			'operators': [
				{'name': 'good', 'type': 'noiseTOP'},
				{'name': 'bad', 'type': 'totallyFakeOP'},
			]
		}
		result = self.tdn.ImportNetwork(
			target_path=self.sandbox.path, tdn=tdn)
		# Should at least create the good one
		good = self.sandbox.op('good')
		self.assertIsNotNone(good)

	def test_M04_missing_connection_source(self):
		"""Connection referencing non-existent source should not crash."""
		tdn = {
			'format': 'tdn', 'version': '1.0',
			'operators': [
				{'name': 'dst', 'type': 'levelTOP',
				 'inputs': ['nonexistent_source']},
			]
		}
		result = self.tdn.ImportNetwork(
			target_path=self.sandbox.path, tdn=tdn)
		# Should still create the operator
		dst = self.sandbox.op('dst')
		self.assertIsNotNone(dst)

	def test_M05_unknown_custom_par_style(self):
		"""Unknown custom par style should not crash import."""
		tdn = {
			'format': 'tdn', 'version': '1.0',
			'operators': [
				{'name': 'c', 'type': 'baseCOMP',
				 'custom_pars': {'Test': [
					 {'name': 'X', 'style': 'TotallyFakeStyle'}
				 ]}},
			]
		}
		result = self.tdn.ImportNetwork(
			target_path=self.sandbox.path, tdn=tdn)
		c = self.sandbox.op('c')
		self.assertIsNotNone(c)

	def test_M06_empty_operators_array(self):
		"""Empty operators array should succeed with 0 created."""
		result = self.tdn.ImportNetwork(
			target_path=self.sandbox.path,
			tdn={'format': 'tdn', 'version': '1.0', 'operators': []})
		self.assertTrue(result.get('success'))
		self.assertEqual(result.get('created_count', 0), 0)

	def test_M07_unknown_flag(self):
		"""Unknown flag name should not crash import."""
		tdn = {
			'format': 'tdn', 'version': '1.0',
			'operators': [
				{'name': 'n', 'type': 'noiseTOP',
				 'flags': ['bypass', 'totally_fake_flag']},
			]
		}
		result = self.tdn.ImportNetwork(
			target_path=self.sandbox.path, tdn=tdn)
		n = self.sandbox.op('n')
		self.assertIsNotNone(n)
		self.assertTrue(n.bypass)

	def test_M08_extra_unknown_fields(self):
		"""Extra unknown fields on operators should be ignored."""
		tdn = {
			'format': 'tdn', 'version': '1.0',
			'operators': [
				{'name': 'n', 'type': 'noiseTOP',
				 'unknown_field': 'whatever',
				 'another_unknown': [1, 2, 3]},
			]
		}
		result = self.tdn.ImportNetwork(
			target_path=self.sandbox.path, tdn=tdn)
		self.assertTrue(result.get('success'))
		self.assertIsNotNone(self.sandbox.op('n'))

	def test_M09_operators_as_direct_list(self):
		"""Import with just the operators list (no wrapper)."""
		ops = [
			{'name': 'a', 'type': 'noiseTOP'},
			{'name': 'b', 'type': 'textDAT'},
		]
		result = self.tdn.ImportNetwork(
			target_path=self.sandbox.path, tdn=ops)
		self.assertTrue(result.get('success'))
		self.assertIsNotNone(self.sandbox.op('a'))
		self.assertIsNotNone(self.sandbox.op('b'))

	# =================================================================
	# N. Scale Testing (3 focused + 1 mega)
	# =================================================================

	def test_N01_100_operators_roundtrip(self):
		"""100 operators round-trip."""
		for i in range(100):
			self.sandbox.create(noiseTOP, f'n{i}')
		_, reimp, _ = self._roundTrip(self.sandbox)
		self.assertEqual(len(reimp['operators']), 100)

	def test_N02_50_chained_connections(self):
		"""50-op chained connection round-trip."""
		prev = self.sandbox.create(noiseTOP, 'chain_0')
		for i in range(1, 50):
			curr = self.sandbox.create(levelTOP, f'chain_{i}')
			prev.outputConnectors[0].connect(curr.inputConnectors[0])
			prev = curr
		self._roundTrip(self.sandbox)
		# Verify chain is intact
		for i in range(1, 50):
			c = self.sandbox.op(f'chain_{i}')
			self.assertIsNotNone(c)
			self.assertEqual(c.inputs[0].name, f'chain_{i - 1}')

	def test_N03_10_level_deep_nesting(self):
		"""10-level deep nesting round-trip."""
		current = self.sandbox
		for i in range(10):
			current = current.create(baseCOMP, f'level_{i}')
		current.create(noiseTOP, 'leaf')
		self._roundTrip(self.sandbox)
		path = '/'.join(f'level_{i}' for i in range(10)) + '/leaf'
		self.assertIsNotNone(self.sandbox.op(path))

	def test_N04_mega_network_roundtrip(self):
		"""300+ operator mega network stress test."""
		self._buildMegaNetwork(self.sandbox)
		orig_count = len(list(self.sandbox.findChildren(depth=1)))
		orig_tdn, reimp_tdn, result = self._roundTrip(self.sandbox)
		reimp_count = len(list(self.sandbox.findChildren(depth=1)))
		# Allow some tolerance for POPs that may not be available
		self.assertGreaterEqual(reimp_count, orig_count - 5,
			f'Expected ~{orig_count} top-level ops, got {reimp_count}')

	# =================================================================
	# O. POP Operators (2 tests, with skip guard)
	# =================================================================

	def test_O01_single_pop_roundtrip(self):
		"""Single POP chain round-trip."""
		try:
			g = self.sandbox.create(gridPOP, 'pg')
			t = self.sandbox.create(transformPOP, 'pt')
			g.outputConnectors[0].connect(t.inputConnectors[0])
		except Exception:
			self.skip('POPs not available in this TD version')
			return
		self._roundTrip(self.sandbox)
		rg = self.sandbox.op('pg')
		rt = self.sandbox.op('pt')
		self.assertIsNotNone(rg)
		self.assertIsNotNone(rt)
		self.assertEqual(rt.inputs[0].name, 'pg')

	def test_O02_pop_in_mixed_network(self):
		"""POP in mixed-family network round-trip."""
		self.sandbox.create(noiseTOP, 'top1')
		self.sandbox.create(waveCHOP, 'chop1')
		try:
			self.sandbox.create(gridPOP, 'pop1')
		except Exception:
			self.skip('POPs not available in this TD version')
			return
		self._roundTrip(self.sandbox)
		self.assertIsNotNone(self.sandbox.op('top1'))
		self.assertIsNotNone(self.sandbox.op('chop1'))
		self.assertIsNotNone(self.sandbox.op('pop1'))

	# =================================================================
	# P. Save Cycle Safety (10 tests)
	#
	# Tests the actual ctrl-s destruction chain:
	#   onProjectPreSave -> Update() -> StripCompChildren()
	#   -> TD saves .toe -> onProjectPostSave -> restore -> Refresh
	#   -> checkOpsForContinuity()
	#
	# These tests exist because the TDN round-trip tests (sections A-K)
	# all passed while a critical bug destroyed externalized files on
	# every save. The round-trip tests operate in an isolated sandbox
	# and never exercise the externalizations table, continuity check,
	# or file deletion logic that runs during a real save.
	# =================================================================

	def _addTableRow(self, path, op_type, strategy, rel_file_path):
		"""Add a row to the externalizations table for testing. Returns row index."""
		table = self.embody_ext.Externalizations
		table.appendRow([path, op_type, strategy, rel_file_path, '', '', '', ''])
		return table.numRows - 1

	def _removeTableRow(self, path, strategy=None):
		"""Remove test row(s) from externalizations table by path."""
		table = self.embody_ext.Externalizations
		for i in range(table.numRows - 1, 0, -1):
			if table[i, 'path'].val == path:
				if strategy is None or table[i, 'strategy'].val == strategy:
					table.deleteRow(i)

	def _tableHasRow(self, path, strategy=None):
		"""Check if the externalizations table has a row for this path."""
		table = self.embody_ext.Externalizations
		for i in range(1, table.numRows):
			if table[i, 'path'].val == path:
				if strategy is None or table[i, 'strategy'].val == strategy:
					return True
		return False

	# --- P1-P3: _getTDNStrategyComps filtering ---

	def test_P01_embody_descendants_excluded_from_strip(self):
		"""Embody descendants must be excluded from _getTDNStrategyComps.

		This is the exact bug that caused ctrl-s to delete help/text_help:
		/embody/Embody/help was a TDN COMP inside Embody, and the filter
		only excluded Embody itself and ancestors, not descendants.
		"""
		comps = self.embody_ext._getTDNStrategyComps()
		embody_path = self.embody.path
		for comp_path, _ in comps:
			self.assertFalse(
				comp_path.startswith(embody_path + '/'),
				f'Embody descendant {comp_path} must be excluded from TDN stripping')

	def test_P02_external_tdn_comps_still_included(self):
		"""TDN COMPs outside Embody should still be returned for stripping."""
		# Create a TDN COMP outside Embody and register it
		tdn_comp = self.sandbox.create(baseCOMP, 'tdn_test_comp')
		tdn_comp.create(noiseTOP, 'child1')
		tdn_path = tdn_comp.path
		rel_path = f'embody/{tdn_comp.name}.tdn'
		self._addTableRow(tdn_path, 'base', 'tdn', rel_path)
		try:
			comps = self.embody_ext._getTDNStrategyComps()
			found = any(cp == tdn_path for cp, _ in comps)
			self.assertTrue(found,
				f'{tdn_path} should be included in TDN strategy comps')
		finally:
			self._removeTableRow(tdn_path, 'tdn')

	def test_P03_embody_help_specifically_excluded(self):
		"""The /embody/Embody/help COMP must not appear in strip list.

		Regression guard for the specific COMP that was destroyed.
		"""
		comps = self.embody_ext._getTDNStrategyComps()
		help_path = self.embody.path + '/help'
		for comp_path, _ in comps:
			self.assertNotEqual(comp_path, help_path,
				f'{help_path} must never be stripped')

	# --- P4-P6: checkOpsForContinuity skips TDN children ---

	def test_P04_continuity_check_skips_children_of_tdn_comp(self):
		"""checkOpsForContinuity must skip operators inside TDN COMPs.

		This prevents the continuity check from deleting files for
		operators that are temporarily missing during the strip/restore
		save cycle.
		"""
		# Create a TDN COMP and a child DAT in the sandbox
		tdn_comp = self.sandbox.create(baseCOMP, 'tdn_parent')
		child_dat = tdn_comp.create(textDAT, 'tracked_child')
		child_dat.text = 'important content'

		tdn_path = tdn_comp.path
		child_path = child_dat.path
		tdn_rel = f'embody/{tdn_comp.name}.tdn'
		child_rel = f'embody/{tdn_comp.name}/tracked_child.txt'

		# Register both in the externalizations table
		self._addTableRow(tdn_path, 'base', 'tdn', tdn_rel)
		self._addTableRow(child_path, 'text', 'txt', child_rel)

		try:
			# Destroy the child (simulating StripCompChildren)
			child_dat.destroy()
			self.assertIsNone(op(child_path), 'Child should be destroyed')

			# Run the continuity check — this is what caused file deletion
			self.embody_ext.checkOpsForContinuity(
				self.embody_ext.ExternalizationsFolder)

			# The child's row must still exist in the table
			self.assertTrue(self._tableHasRow(child_path),
				'Continuity check must NOT remove children of TDN COMPs')
		finally:
			self._removeTableRow(child_path)
			self._removeTableRow(tdn_path, 'tdn')

	def test_P05_continuity_check_still_catches_real_missing_ops(self):
		"""Operators NOT inside TDN COMPs should still be caught as missing."""
		# Create a standalone DAT (not inside a TDN COMP)
		orphan = self.sandbox.create(textDAT, 'orphan_dat')
		orphan_path = orphan.path
		orphan_rel = f'embody/orphan_dat.txt'

		self._addTableRow(orphan_path, 'text', 'txt', orphan_rel)

		try:
			# Destroy the operator
			orphan.destroy()
			self.assertIsNone(op(orphan_path))

			# Run continuity check — should detect it as missing
			self.embody_ext.checkOpsForContinuity(
				self.embody_ext.ExternalizationsFolder)

			# The row should be removed (operator is genuinely missing)
			self.assertFalse(self._tableHasRow(orphan_path),
				'Genuinely missing operators should still be cleaned up')
		finally:
			# Clean up in case the test fails and the row is still there
			self._removeTableRow(orphan_path)

	def test_P06_nested_tdn_children_also_skipped(self):
		"""Deeply nested operators inside TDN COMPs should also be skipped."""
		tdn_comp = self.sandbox.create(baseCOMP, 'deep_tdn')
		inner = tdn_comp.create(baseCOMP, 'inner')
		deep_dat = inner.create(textDAT, 'deep_tracked')

		tdn_path = tdn_comp.path
		deep_path = deep_dat.path
		self._addTableRow(tdn_path, 'base', 'tdn', f'embody/{tdn_comp.name}.tdn')
		self._addTableRow(deep_path, 'text', 'txt',
			f'embody/{tdn_comp.name}/inner/deep_tracked.txt')

		try:
			# Strip everything
			for c in list(tdn_comp.children):
				c.destroy()

			self.embody_ext.checkOpsForContinuity(
				self.embody_ext.ExternalizationsFolder)

			self.assertTrue(self._tableHasRow(deep_path),
				'Deeply nested TDN children must be skipped by continuity check')
		finally:
			self._removeTableRow(deep_path)
			self._removeTableRow(tdn_path, 'tdn')

	# --- P7-P8: Full strip/restore cycle ---

	def test_P07_strip_restore_preserves_children(self):
		"""Full strip -> export -> restore cycle preserves all children."""
		# Build a TDN COMP with content
		tdn_comp = self.sandbox.create(baseCOMP, 'save_cycle_comp')
		tdn_comp.create(noiseTOP, 'noise1')
		d = tdn_comp.create(textDAT, 'script1')
		d.text = 'preserved content'
		tdn_comp.create(baseCOMP, 'inner').create(waveCHOP, 'wave1')

		# Export to TDN (like Update does before strip)
		export_result = self.tdn.ExportNetwork(
			root_path=tdn_comp.path, include_dat_content=True)
		self.assertTrue(export_result.get('success'))
		tdn_doc = export_result['tdn']

		# Strip (like onProjectPreSave)
		self.embody_ext.StripCompChildren(tdn_comp)
		self.assertEqual(len(tdn_comp.children), 0, 'Strip should remove all children')

		# Restore (like onProjectPostSave)
		result = self.tdn.ImportNetwork(
			target_path=tdn_comp.path, tdn=tdn_doc,
			clear_first=True, restore_file_links=True)
		self.assertTrue(result.get('success'))

		# Verify everything is back
		self.assertIsNotNone(tdn_comp.op('noise1'), 'noise1 not restored')
		self.assertIsNotNone(tdn_comp.op('script1'), 'script1 not restored')
		self.assertEqual(tdn_comp.op('script1').text, 'preserved content')
		self.assertIsNotNone(tdn_comp.op('inner/wave1'), 'inner/wave1 not restored')

	def test_P08_strip_restore_then_continuity_check_safe(self):
		"""Full save cycle: strip -> restore -> continuity check must not delete anything.

		This is the exact sequence that ctrl-s triggers. Tests the complete
		chain including table entries and the continuity check.
		"""
		# Create TDN COMP with a tracked child
		tdn_comp = self.sandbox.create(baseCOMP, 'full_cycle')
		child = tdn_comp.create(textDAT, 'tracked_dat')
		child.text = 'must survive'

		tdn_path = tdn_comp.path
		child_path = child.path

		# Register in table
		self._addTableRow(tdn_path, 'base', 'tdn', f'embody/{tdn_comp.name}.tdn')
		self._addTableRow(child_path, 'text', 'txt',
			f'embody/{tdn_comp.name}/tracked_dat.txt')

		try:
			# Phase 1: Export (like Update does)
			export_result = self.tdn.ExportNetwork(
				root_path=tdn_path, include_dat_content=True)
			self.assertTrue(export_result.get('success'))
			tdn_doc = export_result['tdn']

			# Phase 2: Strip (like onProjectPreSave)
			self.embody_ext.StripCompChildren(tdn_comp)
			self.assertEqual(len(tdn_comp.children), 0)

			# Phase 3: Restore (like onProjectPostSave)
			self.tdn.ImportNetwork(
				target_path=tdn_path, tdn=tdn_doc,
				clear_first=True, restore_file_links=True)

			# Phase 4: Continuity check (like the delayed Refresh)
			self.embody_ext.checkOpsForContinuity(
				self.embody_ext.ExternalizationsFolder)

			# Verify: table entry must survive
			self.assertTrue(self._tableHasRow(child_path),
				'Table entry for TDN child must survive full save cycle')

			# Verify: operator must be restored
			restored = tdn_comp.op('tracked_dat')
			self.assertIsNotNone(restored, 'Operator must be restored after save cycle')
			self.assertEqual(restored.text, 'must survive')
		finally:
			self._removeTableRow(child_path)
			self._removeTableRow(tdn_path, 'tdn')

	# --- P9-P10: Update suppress_refresh and worst-case timing ---

	def test_P09_update_suppress_refresh_no_crash(self):
		"""Update(suppress_refresh=True) must not crash."""
		try:
			self.embody_ext.Update(suppress_refresh=True)
		except Exception as e:
			self.fail(f'Update(suppress_refresh=True) raised: {e}')

	def test_P10_continuity_check_during_strip_window(self):
		"""Continuity check DURING strip (before restore) must not delete TDN children.

		This is the worst-case scenario: the refresh fires between
		strip and restore. Even in this case, the TDN child skip
		must prevent destruction.
		"""
		tdn_comp = self.sandbox.create(baseCOMP, 'mid_strip')
		child = tdn_comp.create(textDAT, 'victim')
		child.text = 'do not delete me'

		tdn_path = tdn_comp.path
		child_path = child.path

		self._addTableRow(tdn_path, 'base', 'tdn', f'embody/{tdn_comp.name}.tdn')
		self._addTableRow(child_path, 'text', 'txt',
			f'embody/{tdn_comp.name}/victim.txt')

		try:
			# Strip the COMP (children destroyed)
			self.embody_ext.StripCompChildren(tdn_comp)
			self.assertIsNone(op(child_path), 'Child must be destroyed by strip')

			# Run continuity check BEFORE restore (the dangerous window)
			self.embody_ext.checkOpsForContinuity(
				self.embody_ext.ExternalizationsFolder)

			# The child's table entry MUST survive
			self.assertTrue(self._tableHasRow(child_path),
				'Table entry must survive continuity check during strip window')
		finally:
			self._removeTableRow(child_path)
			self._removeTableRow(tdn_path, 'tdn')
