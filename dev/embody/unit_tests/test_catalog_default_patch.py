"""
Test suite: CatalogManager cross-build default repair (_patchComp).

When TD changes a parameter's DEFAULT between builds, a value the user left
at the OLD default was omitted from the .tdn (default-omission), so TD
recreates the op carrying the NEW default -- silently changing the network.
_patchComp walks a COMP and restores the old value wherever the current
value equals the new default.

Regression origin (2026-08-21): _patchComp walked
`comp.findChildren(depth=-1)`. TD's `depth` matches an EXACT relative depth,
so depth=-1 matched NOTHING and the loop never ran -- the entire repair was a
silent no-op with zero test coverage. These tests pin the descendant walk at
more than one level, so the same class of bug cannot return unnoticed.
"""

try:
	runner_mod = op.unit_tests.op('TestRunnerExt').module
	EmbodyTestCase = runner_mod.EmbodyTestCase
except (AttributeError, NameError):
	pass


class TestCatalogDefaultPatch(EmbodyTestCase):

	def setUp(self):
		super().setUp()
		self.cat = self.embody.ext.CatalogManager
		self.outer = self.sandbox.create(baseCOMP, 'patch_outer')

	def _shifted(self, old, new):
		"""A synthetic shift table: noiseCHOP.amp moved from `old` to `new`."""
		return {'noiseCHOP': {'amp': (old, new)}}

	def test_patches_direct_child(self):
		child = self.outer.create(noiseCHOP, 'noise_shallow')
		child.par.amp = 1.0          # stands in for the NEW default
		patches = self.cat._patchComp(self.outer, self._shifted(0.25, 1.0), None)
		self.assertLen(patches, 1)
		self.assertAlmostEqual(child.par.amp.val, 0.25, places=6)

	def test_patches_nested_descendant(self):
		"""The case depth=-1 silently missed: an op below the first level."""
		mid = self.outer.create(baseCOMP, 'patch_mid')
		deep = mid.create(noiseCHOP, 'noise_deep')
		deep.par.amp = 1.0
		patches = self.cat._patchComp(self.outer, self._shifted(0.25, 1.0), None)
		self.assertLen(patches, 1)
		self.assertAlmostEqual(deep.par.amp.val, 0.25, places=6,
			msg='nested descendant not patched -- the walk is depth-limited '
			    'again (use maxDepth/no depth arg, never depth=)')

	def test_patches_every_level_at_once(self):
		shallow = self.outer.create(noiseCHOP, 'noise_l1')
		mid = self.outer.create(baseCOMP, 'mid')
		deep = mid.create(noiseCHOP, 'noise_l2')
		deeper = mid.create(baseCOMP, 'deeper').create(noiseCHOP, 'noise_l3')
		for o in (shallow, deep, deeper):
			o.par.amp = 1.0
		patches = self.cat._patchComp(self.outer, self._shifted(0.25, 1.0), None)
		self.assertLen(patches, 3)
		for o in (shallow, deep, deeper):
			self.assertAlmostEqual(o.par.amp.val, 0.25, places=6)

	def test_leaves_values_that_do_not_match_new_default(self):
		"""A user value that is NOT the new default must never be touched."""
		child = self.outer.create(noiseCHOP, 'noise_custom')
		child.par.amp = 0.75         # deliberate user value
		patches = self.cat._patchComp(self.outer, self._shifted(0.25, 1.0), None)
		self.assertLen(patches, 0)
		self.assertAlmostEqual(child.par.amp.val, 0.75, places=6)

	def test_ignores_expression_mode_parameters(self):
		"""An expression is authored intent -- patching it would destroy it."""
		child = self.outer.create(noiseCHOP, 'noise_expr')
		child.par.amp.expr = '1.0'
		patches = self.cat._patchComp(self.outer, self._shifted(0.25, 1.0), None)
		self.assertLen(patches, 0)
		self.assertEqual(child.par.amp.mode.name, 'EXPRESSION')

	def test_ignores_unshifted_operator_types(self):
		child = self.outer.create(noiseCHOP, 'noise_untouched')
		child.par.amp = 1.0
		patches = self.cat._patchComp(
			self.outer, {'waveCHOP': {'amp': (0.25, 1.0)}}, None)
		self.assertLen(patches, 0)
		self.assertAlmostEqual(child.par.amp.val, 1.0, places=6)
