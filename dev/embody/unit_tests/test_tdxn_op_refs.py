"""
Test suite: constant-mode OP-reference parameters survive a TDXN round-trip.

Regression cover for issue #132. Sequence-block and custom OP parameters
were exported as str(p.eval()) -- an absolute path, or nothing at all
for a name that did not resolve -- while the built-in `pdat` beside them
read p.val and round-tripped. Two halves: A, export writes the authored
string; B, an absolute value inside the exported root is repaired
owner-relative on export and on import. Mechanism and scope: the #132
note above TDXNExt._relativeOPValue.
"""

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase


class TestTDXNOpRefs(EmbodyTestCase):

	def setUp(self):
		super().setUp()
		self.tdn = self.embody.ext.TDXN

	# =================================================================
	# Helpers
	# =================================================================

	def _export(self, root):
		result = self.tdn.ExportNetwork(root_path=root.path, interactive=False)
		self.assertNotIn('error', result, f'export failed: {result.get("error")}')
		return result['tdn']

	def _import(self, dst, doc):
		result = self.tdn.ImportNetwork(
			target_path=dst.path, tdn=doc, clear_first=True)
		self.assertNotIn('error', result, f'import failed: {result.get("error")}')

	def _byName(self, doc):
		return {o.get('name'): o for o in doc.get('operators', [])}

	def _child(self, entry, name):
		"""A child entry by name -- export order is TD's, not sorted."""
		for c in entry.get('children', []):
			if c.get('name') == name:
				return c
		self.fail(f'no child {name!r} under {entry.get("name")!r}')

	def _build(self, root, sampler_value='constant_bg', target_value='constant_bg'):
		"""The two parameter classes the issue named -- a SEQUENCE block OP
		ref and a custom OP par -- beside `pdat`, the built-in control."""
		bg = root.create(constantTOP, 'constant_bg')
		glsl = root.create(glslMAT, 'glsl_mat')
		aim = root.create(baseCOMP, 'aim')
		glsl.seq.sampler.numBlocks = 1
		glsl.par.sampler0name = 'sBackground'
		glsl.par.sampler0top = sampler_value
		aim.appendCustomPage('Repro').appendOP('Target')
		aim.par.Target = target_value
		return bg, glsl, aim

	def _seqTop(self, doc, op_name='glsl_mat', block=0):
		entry = self._byName(doc)[op_name]
		return entry.get('sequences', {}).get('sampler', [{}])[block].get('top')

	def _customValue(self, doc, op_name='aim'):
		entry = self._byName(doc)[op_name]
		return entry.get('custom_pars', {}).get('Repro', [{}])[0].get('value')

	# =================================================================
	# A. Export writes the authored string, not the evaluated operator
	# =================================================================

	def test_sequence_op_ref_exports_authored_name(self):
		root = self.sandbox.create(baseCOMP, 'authored')
		self._build(root)
		self.assertEqual(self._seqTop(self._export(root)), 'constant_bg')

	def test_custom_op_par_exports_authored_name(self):
		root = self.sandbox.create(baseCOMP, 'authored_custom')
		self._build(root)
		self.assertEqual(self._customValue(self._export(root)), 'constant_bg')

	def test_builtin_op_ref_still_round_trips(self):
		"""Control (passes before and after the fix): the built-in `pdat`
		was never broken -- two OP refs on one operator, only one of them
		absolute, is what made the bug easy to miss."""
		root = self.sandbox.create(baseCOMP, 'control')
		_bg, glsl, _aim = self._build(root)
		doc = self._export(root)
		self.assertEqual(
			self._byName(doc)['glsl_mat']['parameters'].get('pdat'),
			glsl.par.pdat.val)

	def test_unresolved_op_ref_is_not_dropped(self):
		"""A name whose operator does not exist YET evaluated to None, which
		read as the default -- the key vanished and the parameter came
		back blank."""
		root = self.sandbox.create(baseCOMP, 'unresolved')
		self._build(root, sampler_value='not_created_yet',
					target_value='also_missing')
		doc = self._export(root)
		self.assertEqual(self._seqTop(doc), 'not_created_yet')
		self.assertEqual(self._customValue(doc), 'also_missing')

	def test_op_ref_pattern_is_not_dropped(self):
		root = self.sandbox.create(baseCOMP, 'pattern')
		self._build(root, sampler_value='constant*', target_value='constant*')
		doc = self._export(root)
		self.assertEqual(self._seqTop(doc), 'constant*')
		self.assertEqual(self._customValue(doc), 'constant*')

	# =================================================================
	# B. Absolute values already in the wild are repaired
	# =================================================================

	def test_absolute_value_inside_root_is_rebased_on_export(self):
		"""A COMP rebuilt from a pre-fix file holds the ABSOLUTE string
		live, so fix A alone would re-export the damage. Export rebases it
		relative to the OWNER: a sibling name at the top level, '../' from
		one level down."""
		root = self.sandbox.create(baseCOMP, 'damaged')
		bg, glsl, aim = self._build(root)
		nest = root.create(baseCOMP, 'nest')
		deep = nest.create(glslMAT, 'deep_mat')
		deep.seq.sampler.numBlocks = 1
		glsl.par.sampler0top = bg.path
		aim.par.Target = bg.path
		deep.par.sampler0top = bg.path

		doc = self._export(root)
		self.assertEqual(self._seqTop(doc), 'constant_bg')
		self.assertEqual(self._customValue(doc), 'constant_bg')
		nested = self._child(self._byName(doc)['nest'], 'deep_mat')
		self.assertEqual(nested['sequences']['sampler'][0].get('top'),
						 '../constant_bg')

	def test_absolute_value_outside_root_is_left_alone(self):
		"""Control (passes before and after the fix): a reference OUT of
		the exported network is the user's own and stays absolute."""
		outsider = self.sandbox.create(constantTOP, 'outsider')
		root = self.sandbox.create(baseCOMP, 'refs_out')
		_bg, glsl, aim = self._build(root)
		glsl.par.sampler0top = outsider.path
		aim.par.Target = outsider.path

		doc = self._export(root)
		self.assertEqual(self._seqTop(doc), outsider.path)
		self.assertEqual(self._customValue(doc), outsider.path)

	def test_absolute_pattern_and_multi_op_values_stay_as_authored(self):
		"""op() matches the FIRST token of a pattern or a multi-op value,
		so rebasing through it would silently narrow the reference to one
		operator. Such values are left exactly as authored, both ways."""
		root = self.sandbox.create(baseCOMP, 'patterns')
		_bg, glsl, aim = self._build(root)
		root.create(constantTOP, 'constant_fg')
		pattern = f'{root.path}/constant_*'
		multi = f'{root.path}/constant_bg {root.path}/constant_fg'
		glsl.par.sampler0top = pattern
		aim.par.Target = multi

		doc = self._export(root)
		self.assertEqual(self._seqTop(doc), pattern)
		self.assertEqual(self._customValue(doc), multi)

		dst = self.sandbox.create(baseCOMP, 'patterns_copy')
		self._import(dst, doc)
		self.assertEqual(dst.op('glsl_mat').par.sampler0top.val, pattern)
		self.assertEqual(dst.op('aim').par.Target.val, multi)

	def test_relative_op_path_matches_td(self):
		"""The lexical relative path IS OP.relativePath, shape for shape,
		and TD resolves what it produces from the owner's own context."""
		r = self.sandbox.create(baseCOMP, 'shapes')
		bg = r.create(constantTOP, 'bg')
		glsl = r.create(glslMAT, 'glsl')
		nest = r.create(baseCOMP, 'nest')
		deep = nest.create(glslMAT, 'deep')
		inner = nest.create(baseCOMP, 'inner')
		leaf = inner.create(constantTOP, 'leaf')
		nest2 = r.create(baseCOMP, 'nest2')
		y = nest2.create(constantTOP, 'y')
		pairs = [(glsl, bg), (nest, bg), (r, bg), (deep, bg), (glsl, y),
				 (deep, y), (nest, deep), (nest, leaf), (r, deep), (r, leaf),
				 (leaf, bg), (nest, y), (leaf, y), (r, r)]
		for owner, target in pairs:
			self.assertEqual(
				self.tdn._relativeOpPath(owner.path, target.path),
				owner.relativePath(target),
				f'{owner.path} -> {target.path}')

		glsl.seq.sampler.numBlocks = 1
		glsl.par.sampler0top = self.tdn._relativeOpPath(glsl.path, y.path)
		self.assertEqual(glsl.par.sampler0top.eval(), y)
		deep.seq.sampler.numBlocks = 1
		deep.par.sampler0top = self.tdn._relativeOpPath(deep.path, bg.path)
		self.assertEqual(deep.par.sampler0top.eval(), bg)
		r.appendCustomPage('P').appendOP('T')
		r.par.T = self.tdn._relativeOpPath(r.path, leaf.path)
		self.assertEqual(r.par.T.eval(), leaf)

	def test_path_under_root_edge_cases(self):
		"""What is -- and is not -- rebased, as a pure function."""
		f = self.tdn._pathUnderRoot
		self.assertEqual(f('/a/b/c', '/a'), 'b/c')
		self.assertEqual(f('/a/b', '/a/'), 'b')
		self.assertIsNone(f('/a', '/a'), 'the root itself is never rebased')
		self.assertIsNone(f('/x/y', '/a'), 'outside the root')
		self.assertIsNone(f('/a/bc/x', '/a/b'), 'prefix collision')
		self.assertIsNone(f('/a/b', '/'), 'a whole-project root has no outside')
		self.assertIsNone(f('/a/b/../c', '/a/b'), 'never escapes through ..')
		self.assertIsNone(f('/a/./b', '/a'))
		self.assertIsNone(f('/a//b', '/a'))
		self.assertIsNone(f('/a/b/', '/a'), 'trailing slash on the value')
		self.assertIsNone(f('sibling', '/a'), 'a relative value')
		self.assertIsNone(f('/a/b', None), 'no scope')
		self.assertIsNone(f('/a/b /a/c', '/a'), 'a multi-op value')
		self.assertIsNone(f('/a/b*', '/a'), 'a pattern')

	def test_prefix_file_rebases_onto_the_destination_on_import(self):
		"""The acute failure: a file WRITTEN before the fix holds the source
		root's absolute paths. Imported verbatim into a differently-named
		COMP they keep pointing at the ORIGINAL, so the copy silently
		drives the source's operators. Owners at the top level and one
		level down."""
		src = self.sandbox.create(baseCOMP, 'imp_src')
		self._build(src)
		nest = src.create(baseCOMP, 'nest')
		deep = nest.create(glslMAT, 'deep_mat')
		deep.seq.sampler.numBlocks = 1
		deep.par.sampler0top = '../constant_bg'
		doc = self._export(src)

		abs_path = f'{src.path}/constant_bg'
		by_name = self._byName(doc)
		by_name['glsl_mat']['sequences']['sampler'][0]['top'] = abs_path
		by_name['aim']['custom_pars']['Repro'][0]['value'] = abs_path
		self._child(by_name['nest'], 'deep_mat')['sequences']['sampler'][0]['top'] = abs_path

		dst = self.sandbox.create(baseCOMP, 'imp_copy')
		self._import(dst, doc)
		own_bg = dst.op('constant_bg')
		self.assertIsNotNone(own_bg)
		self.assertEqual(dst.op('glsl_mat').par.sampler0top.val, 'constant_bg')
		self.assertEqual(dst.op('aim').par.Target.val, 'constant_bg')
		self.assertEqual(dst.op('nest/deep_mat').par.sampler0top.val, '../constant_bg')
		self.assertEqual(dst.op('glsl_mat').par.sampler0top.eval(), own_bg)
		self.assertEqual(dst.op('aim').par.Target.eval(), own_bg)
		self.assertEqual(dst.op('nest/deep_mat').par.sampler0top.eval(), own_bg)

	def test_prefix_file_with_missing_target_still_rebases_on_import(self):
		"""The rebase is lexical, so a target that does not exist when the
		parameter is set (an op inside a tdn_ref shell, filled later in
		Phase 8.6) is still remapped instead of left pointing at the
		source."""
		src = self.sandbox.create(baseCOMP, 'imp_shell_src')
		self._build(src)
		doc = self._export(src)
		by_name = self._byName(doc)
		by_name['aim']['custom_pars']['Repro'][0]['value'] = (
			f'{src.path}/nest/inner/leaf')

		dst = self.sandbox.create(baseCOMP, 'imp_shell_copy')
		self._import(dst, doc)
		self.assertEqual(dst.op('aim').par.Target.val, 'nest/inner/leaf')

	def test_builtin_absolute_value_is_not_rebased_on_import(self):
		"""Scope: top-level built-in parameters were always written as
		authored, so an absolute value there is the user's and is imported
		verbatim -- the same line export draws."""
		src = self.sandbox.create(baseCOMP, 'imp_builtin_src')
		self._build(src)
		doc = self._export(src)
		abs_dat = f'{src.path}/glsl_mat_pixel'
		self._byName(doc)['glsl_mat']['parameters']['pdat'] = abs_dat

		dst = self.sandbox.create(baseCOMP, 'imp_builtin_copy')
		self._import(dst, doc)
		self.assertEqual(dst.op('glsl_mat').par.pdat.val, abs_dat)

	def test_roundtrip_into_sibling_resolves_locally(self):
		"""End-to-end on a CLEAN export: the copy holds the authored strings
		and drives its own operators."""
		src = self.sandbox.create(baseCOMP, 'rt_src')
		self._build(src)
		doc = self._export(src)

		dst = self.sandbox.create(baseCOMP, 'rt_dst_other_name')
		self._import(dst, doc)
		own_bg = dst.op('constant_bg')
		self.assertEqual(dst.op('glsl_mat').par.sampler0top.val, 'constant_bg')
		self.assertEqual(dst.op('aim').par.Target.val, 'constant_bg')
		self.assertEqual(dst.op('glsl_mat').par.sampler0top.eval(), own_bg)
		self.assertEqual(dst.op('aim').par.Target.eval(), own_bg)

	def test_reconstructed_comp_survives_rename(self):
		"""Issue case 2: with absolute paths a rename left every reference
		dangling (visible only once the MAT cooked)."""
		src = self.sandbox.create(baseCOMP, 'rt_rename_src')
		self._build(src)
		doc = self._export(src)
		dst = self.sandbox.create(baseCOMP, 'rt_before_rename')
		self._import(dst, doc)

		dst.name = 'rt_after_rename'
		own_bg = dst.op('constant_bg')
		self.assertEqual(dst.op('glsl_mat').par.sampler0top.eval(), own_bg)
		self.assertEqual(dst.op('aim').par.Target.eval(), own_bg)

	def test_clone_resync_keeps_refs_inside_the_clone(self):
		"""Since #123 a sibling clone is refilled from its master on import
		(D13). With absolute refs in the master, every clone's sequence
		and custom OP parameters resolved into the MASTER -- which itself
		still looked right. Master and clone live in one exported parent
		so everything stays inside the harness sandbox."""
		parent = self.sandbox.create(baseCOMP, 'clone_parent')
		master = parent.create(baseCOMP, 'layer_master')
		self._build(master)
		clone = parent.create(baseCOMP, 'layer_b')
		clone.par.clone = 'layer_master'
		clone.par.enablecloning = 1

		doc = self._export(parent)
		dst = self.sandbox.create(baseCOMP, 'clone_parent_copy')
		self._import(dst, doc)
		rc = dst.op('layer_b')
		rc.par.enablecloningpulse.pulse()

		own_bg = rc.op('constant_bg')
		self.assertIsNotNone(own_bg, 'the clone is refilled by cloning')
		self.assertEqual(rc.op('glsl_mat').par.sampler0top.val, 'constant_bg')
		self.assertEqual(rc.op('aim').par.Target.val, 'constant_bg')
		self.assertEqual(rc.op('glsl_mat').par.sampler0top.eval(), own_bg,
						 'the clone samples its OWN TOP, not the master\'s')
		self.assertEqual(rc.op('aim').par.Target.eval(), own_bg)
