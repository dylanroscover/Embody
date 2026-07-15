"""
Test suite: Specimen publish hook (dev/specimen_publish.py) + CatalogManager.

specimen_publish is a PROJECT-ONLY execute DAT (not shipped in the Embody .tox).
On Ctrl+S, onProjectPostSave() -> _publish() refreshes each website Specimen
.tdn from the live /specimen_lab gallery, driven by specimens/manifest.json.

These tests cover:
  - _publish() bucketing (written / skipped / missing):
      * missing manifest.json -> {'error': ...}                  (headless)
      * slug whose /specimen_lab COMP is absent -> missing bucket (headless)
      * ExportNetwork returns success:False -> 'slug (export failed)' in
        missing                                                   (headless)
      * slug -> path mapping: 'reaction-diffusion' resolves to
        /specimen_lab/reaction_diffusion (hyphen -> underscore)   (headless)
      * skip-unchanged: first publish writes, identical second skips,
        post-mutation third writes again                          (LIVE; skips
        gracefully when /specimen_lab is absent)
  - CatalogManagerExt pure helpers:
      * _findShiftedDefaults: only changed defaults, (old,new) tuples, None
        source ignored                                            (headless)
      * _valuesEqual: float tolerance (5.0 == 5), str match/mismatch (headless)
      * _writeCatalog / _readCatalog temp-file round-trip incl reserved
        _palette key                                              (headless)

The _publish bucket tests run HEADLESS by patching the loaded module's
namespace (op / project) and stubbing TDN.ExportNetwork, while delegating the
real, pure TDN comparison statics (_read_existing_tdn, _tdn_content_equal,
_compact_json_dumps). No live save, no /specimen_lab dependency.

NONE of these belong in the release smoke suite: specimen_publish is
project-only and CatalogManager helpers here are exercised against temp files.
"""

import os
import json
import tempfile
import shutil
from pathlib import Path

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase

# Sentinel for "module global was absent before patching".
_MISSING = object()


# =============================================================================
# Headless fakes for _publish()
#
# _publish() reads module-level names: op, project, json, Path. We patch op
# and project in the loaded module's __dict__, then restore in tearDown. The
# fake op is callable (op('/specimen_lab/x') -> comp or None) AND exposes
# .Embody (op.Embody.ext.TDN / op.Embody.Log). json and Path stay the real
# stdlib objects already bound in the module.
# =============================================================================

class _FakeComp:
	"""Minimal stand-in for a /specimen_lab COMP: only .path is read."""
	def __init__(self, path):
		self.path = path


class _FakeTDN:
	"""Stub TDN ext. ExportNetwork is controllable; the comparison statics
	delegate to the REAL TDN extension so the skip-unchanged logic is faithful.
	"""
	def __init__(self, real_tdn, export_result=None, export_fn=None):
		self._real = real_tdn
		self._export_result = export_result
		self._export_fn = export_fn
		self.export_calls = []

	def ExportNetwork(self, root_path=None, include_dat_content=None,
					  embed_all=None, **kwargs):
		self.export_calls.append(root_path)
		if self._export_fn is not None:
			return self._export_fn(root_path)
		return self._export_result

	# Delegate the pure comparison/serialization helpers to the real ext.
	def _read_existing_tdn(self, file_path):
		return self._real._read_existing_tdn(file_path)

	def _tdn_content_equal(self, new_tdn, existing_tdn):
		return self._real._tdn_content_equal(new_tdn, existing_tdn)

	def _compact_json_dumps(self, data):
		return self._real._compact_json_dumps(data)


class _FakeExt:
	def __init__(self, tdn):
		self.TDN = tdn


class _FakeEmbody:
	def __init__(self, tdn):
		self.ext = _FakeExt(tdn)
		self.logs = []

	def __bool__(self):
		return True

	def Log(self, message, level='INFO'):
		self.logs.append((level, message))


class _FakeOp:
	"""Callable + attribute access stand-in for the module-level `op`.

	op('/path') -> comp or None (from `present` map);
	op.Embody    -> the fake Embody COMP.
	"""
	def __init__(self, embody, present):
		self.Embody = embody
		self._present = present       # {full_path: _FakeComp}

	def __call__(self, path):
		return self._present.get(path)


class _FakeProject:
	def __init__(self, folder):
		self.folder = folder


class TestSpecimenPublishBuckets(EmbodyTestCase):
	"""Headless coverage of _publish() bucketing via module-namespace patching."""

	def setUp(self):
		super().setUp()
		self.pub_op = op('/specimen_publish')
		if self.pub_op is None:
			self.skipTest('/specimen_publish DAT not present in this project')
		self.mod = self.pub_op.module
		# Snapshot the module globals we override so tearDown restores them.
		self._saved_globals = {}
		for name in ('op', 'project'):
			self._saved_globals[name] = self.mod.__dict__.get(name, _MISSING)
		# Temp repo root: project.folder will be <root>/dev so that
		# Path(project.folder).resolve().parent == <root>, manifest at
		# <root>/specimens/manifest.json (mirrors the real layout).
		self._tmp_root = tempfile.mkdtemp(prefix='specpub_')
		self._dev = os.path.join(self._tmp_root, 'dev')
		os.makedirs(self._dev, exist_ok=True)
		self._spec_dir = os.path.join(self._tmp_root, 'specimens')

	def tearDown(self):
		for name, val in self._saved_globals.items():
			if val is _MISSING:
				self.mod.__dict__.pop(name, None)
			else:
				self.mod.__dict__[name] = val
		try:
			shutil.rmtree(self._tmp_root)
		except Exception:
			pass
		super().tearDown()

	# --- helpers -----------------------------------------------------------

	def _write_manifest(self, specimens):
		os.makedirs(self._spec_dir, exist_ok=True)
		Path(self._spec_dir, 'manifest.json').write_text(
			json.dumps({'specimens': specimens}), encoding='utf-8')

	def _install(self, present=None, export_result=None, export_fn=None):
		"""Patch op/project in the module and return the fake TDN for asserts."""
		real_tdn = self.embody.ext.TDN
		fake_tdn = _FakeTDN(real_tdn, export_result=export_result,
							export_fn=export_fn)
		fake_emb = _FakeEmbody(fake_tdn)
		fake_op = _FakeOp(fake_emb, present or {})
		self.mod.__dict__['op'] = fake_op
		self.mod.__dict__['project'] = _FakeProject(self._dev)
		return fake_tdn, fake_emb

	# --- missing manifest --------------------------------------------------

	def test_missing_manifest_returns_error(self):
		"""No specimens/manifest.json -> {'error': 'manifest not found: ...'}."""
		self._install(present={})
		# Intentionally do NOT write a manifest.
		result = self.mod._publish()
		self.assertDictHasKey(result, 'error')
		self.assertIn('manifest not found', result['error'])

	# --- missing slug COMP -------------------------------------------------

	def test_absent_comp_goes_to_missing_bucket(self):
		"""A manifest slug whose /specimen_lab COMP is absent -> missing."""
		self._write_manifest([
			{'slug': 'ghost-spec', 'tdn_path': 'cat/ghost.tdn'},
		])
		# present map is empty -> op('/specimen_lab/ghost_spec') returns None
		self._install(present={})
		result = self.mod._publish()
		self.assertIn('ghost-spec', result['missing'])
		self.assertListEqual(result['written'], [])
		self.assertListEqual(result['skipped'], [])

	# --- export failure ----------------------------------------------------

	def test_export_failure_marks_missing_with_suffix(self):
		"""ExportNetwork success:False -> 'slug (export failed)' in missing."""
		self._write_manifest([
			{'slug': 'reaction-diffusion',
			 'tdn_path': 'generative/reaction-diffusion.tdn'},
		])
		comp_path = '/specimen_lab/reaction_diffusion'
		present = {comp_path: _FakeComp(comp_path)}
		self._install(present=present,
					  export_result={'success': False, 'error': 'boom'})
		result = self.mod._publish()
		self.assertIn('reaction-diffusion (export failed)', result['missing'])
		self.assertListEqual(result['written'], [])

	# --- slug -> path mapping (hyphen -> underscore) -----------------------

	def test_slug_hyphen_maps_to_underscore_path(self):
		"""'reaction-diffusion' must resolve to /specimen_lab/reaction_diffusion.

		Drive a successful export and assert ExportNetwork was called with the
		underscored COMP path -- the slug.replace('-','_') contract.
		"""
		self._write_manifest([
			{'slug': 'reaction-diffusion',
			 'tdn_path': 'generative/reaction-diffusion.tdn'},
		])
		comp_path = '/specimen_lab/reaction_diffusion'
		present = {comp_path: _FakeComp(comp_path)}
		fake_tdn, _ = self._install(
			present=present,
			export_result={'success': True,
						   'tdn': {'format': 'tdn', 'operators': []}})
		result = self.mod._publish()
		# No prior .tdn on disk -> first export writes.
		self.assertIn('reaction-diffusion', result['written'])
		self.assertListEqual(fake_tdn.export_calls, [comp_path])
		# The written file landed at <root>/specimens/generative/reaction-diffusion.tdn
		out = Path(self._spec_dir, 'generative', 'reaction-diffusion.tdn')
		self.assertTrue(out.exists())

	# --- skip-unchanged (headless, via real comparison statics) ------------

	def test_skip_unchanged_then_rewrite_on_change(self):
		"""Identical export skips; a changed export writes again.

		Uses the REAL TDN _read_existing_tdn / _tdn_content_equal /
		_compact_json_dumps via the fake's delegation, so the
		volatile-key-ignoring skip logic is exercised faithfully -- no live
		/specimen_lab COMP and no save required.
		"""
		self._write_manifest([
			{'slug': 'noise-terrain', 'tdn_path': '3d/noise-terrain.tdn'},
		])
		comp_path = '/specimen_lab/noise_terrain'
		present = {comp_path: _FakeComp(comp_path)}

		tdn_a = {'format': 'tdn', 'version': 1, 'td_build': 'A',
				 'operators': [{'name': 'x', 'type': 'noiseTOP'}]}
		# Same content, only a volatile key (td_build) differs -> should skip.
		tdn_a_volatile = dict(tdn_a)
		tdn_a_volatile['td_build'] = 'B'
		# Genuinely different operators -> should write.
		tdn_b = {'format': 'tdn', 'version': 1, 'td_build': 'A',
				 'operators': [{'name': 'y', 'type': 'levelTOP'}]}

		exports = [
			{'success': True, 'tdn': tdn_a},
			{'success': True, 'tdn': tdn_a_volatile},
			{'success': True, 'tdn': tdn_b},
		]
		state = {'i': 0}

		def export_fn(root_path):
			res = exports[state['i']]
			state['i'] += 1
			return res

		self._install(present=present, export_fn=export_fn)
		out = Path(self._spec_dir, '3d', 'noise-terrain.tdn')

		# 1st publish: nothing on disk -> written.
		r1 = self.mod._publish()
		self.assertIn('noise-terrain', r1['written'])
		self.assertTrue(out.exists())

		# 2nd publish: content identical except volatile header -> skipped.
		r2 = self.mod._publish()
		self.assertIn('noise-terrain', r2['skipped'])
		self.assertListEqual(r2['written'], [])

		# 3rd publish: operators changed -> written again.
		r3 = self.mod._publish()
		self.assertIn('noise-terrain', r3['written'])

	def test_written_file_is_valid_tdn_roundtrip(self):
		"""The file _publish writes must re-read via the real _read_existing_tdn
		into a content-equal dict (compact_json_dumps + read are inverse)."""
		self._write_manifest([
			{'slug': 'kaleidoscope', 'tdn_path': 'compositing/kaleidoscope.tdn'},
		])
		comp_path = '/specimen_lab/kaleidoscope'
		present = {comp_path: _FakeComp(comp_path)}
		tdn = {'format': 'tdn', 'version': 1, 'td_build': 'Z',
			   'operators': [{'name': 'a', 'type': 'textDAT'}]}
		self._install(present=present,
					  export_result={'success': True, 'tdn': tdn})
		self.mod._publish()
		out = Path(self._spec_dir, 'compositing', 'kaleidoscope.tdn')
		self.assertTrue(out.exists())
		reread = self.embody.ext.TDN._read_existing_tdn(str(out))
		self.assertIsNotNone(reread)
		self.assertTrue(self.embody.ext.TDN._tdn_content_equal(tdn, reread))


# =============================================================================
# Live skip-unchanged against a REAL /specimen_lab COMP
# =============================================================================

class TestSpecimenPublishLive(EmbodyTestCase):
	"""Exercise the real export/skip cycle against one live /specimen_lab COMP,
	writing to a temp specimens dir. Skips when /specimen_lab is absent."""

	def setUp(self):
		super().setUp()
		self.pub_op = op('/specimen_publish')
		if self.pub_op is None:
			self.skipTest('/specimen_publish DAT not present')
		lab = op('/specimen_lab')
		if lab is None or not list(lab.children):
			self.skipTest('/specimen_lab gallery not present (live-only test)')
		# Pick the first child COMP as the live specimen to publish.
		self._live = None
		for ch in lab.children:
			if ch.isCOMP:
				self._live = ch
				break
		if self._live is None:
			self.skipTest('/specimen_lab has no COMP children')
		self._tmp_out = tempfile.mkdtemp(prefix='specpub_live_')

	def tearDown(self):
		try:
			shutil.rmtree(self._tmp_out)
		except Exception:
			pass
		super().tearDown()

	def test_live_first_writes_second_skips(self):
		"""First export to a fresh path writes a file; identical export to the
		same path is content-equal and would skip."""
		TDN = self.embody.ext.TDN
		out = Path(self._tmp_out, 'live.tdn')
		res = TDN.ExportNetwork(root_path=self._live.path,
								include_dat_content=True, embed_all=True)
		self.assertTrue(res.get('success'),
						f'live export failed: {res.get("error")}')
		new = res['tdn']
		# First write: no existing file -> _read_existing_tdn is None.
		self.assertIsNone(TDN._read_existing_tdn(str(out)))
		out.write_text(TDN._compact_json_dumps(new), encoding='utf-8')
		self.assertTrue(out.exists())

		# Re-export the same live COMP. Content (minus volatile header) must
		# equal what's on disk -> the publish hook would land in 'skipped'.
		res2 = TDN.ExportNetwork(root_path=self._live.path,
								 include_dat_content=True, embed_all=True)
		self.assertTrue(res2.get('success'))
		old = TDN._read_existing_tdn(str(out))
		self.assertIsNotNone(old)
		self.assertTrue(TDN._tdn_content_equal(res2['tdn'], old),
						'identical re-export should be content-equal -> skipped')


# =============================================================================
# CatalogManager headless helpers
# =============================================================================

class TestCatalogManagerHelpers(EmbodyTestCase):
	"""Pure helpers on CatalogManagerExt: _findShiftedDefaults, _valuesEqual,
	_writeCatalog / _readCatalog round-trip."""

	def setUp(self):
		super().setUp()
		self.cat = self.embody.ext.CatalogManager
		self._tmp_dir = tempfile.mkdtemp(prefix='catalog_')

	def tearDown(self):
		try:
			shutil.rmtree(self._tmp_dir)
		except Exception:
			pass
		super().tearDown()

	# --- _findShiftedDefaults ----------------------------------------------

	def test_findShifted_returns_only_changed(self):
		"""Only params whose default changed between builds are returned, as
		(old, new) tuples keyed by op_type then par_name."""
		source = {'noiseTOP': {'period': 1.0, 'amp': 0.5}}
		current = {'noiseTOP': {'period': 2.0, 'amp': 0.5}}
		shifted = self.cat._findShiftedDefaults(source, current)
		self.assertIn('noiseTOP', shifted)
		self.assertIn('period', shifted['noiseTOP'])
		self.assertEqual(shifted['noiseTOP']['period'], (1.0, 2.0))
		# Unchanged param must not appear.
		self.assertNotIn('amp', shifted['noiseTOP'])

	def test_findShifted_none_source_ignored(self):
		"""A param present only in current (source default is None) is ignored
		-- it iterates current_catalog and skips when source_val is None."""
		source = {'levelTOP': {}}
		current = {'levelTOP': {'brightness1': 1.0}}
		shifted = self.cat._findShiftedDefaults(source, current)
		# brightness1 has no source counterpart -> source_val None -> skipped.
		self.assertNotIn('levelTOP', shifted)

	def test_findShifted_no_changes_empty(self):
		"""Identical catalogs yield an empty shifted dict."""
		source = {'transformTOP': {'tx': 0.0, 'ty': 0.0}}
		current = {'transformTOP': {'tx': 0.0, 'ty': 0.0}}
		shifted = self.cat._findShiftedDefaults(source, current)
		self.assertDictEqual(shifted, {})

	def test_findShifted_unknown_op_type_in_source_ignored(self):
		"""op_types only in source (not current) never enter the result --
		iteration is over current_catalog."""
		source = {'oldTOP': {'x': 1}, 'noiseTOP': {'period': 1.0}}
		current = {'noiseTOP': {'period': 9.0}}
		shifted = self.cat._findShiftedDefaults(source, current)
		self.assertNotIn('oldTOP', shifted)
		self.assertIn('noiseTOP', shifted)

	# --- _valuesEqual (static, float tolerance) ----------------------------

	def test_valuesEqual_float_vs_int_tolerance(self):
		"""5.0 (float) equals 5 (int) under the 1e-9 tolerance."""
		self.assertTrue(self.cat._valuesEqual(5.0, 5))
		self.assertTrue(self.cat._valuesEqual(5, 5.0))

	def test_valuesEqual_float_within_tolerance(self):
		"""Two floats within 1e-9 are equal; outside it, not."""
		self.assertTrue(self.cat._valuesEqual(1.0, 1.0 + 1e-12))
		self.assertFalse(self.cat._valuesEqual(1.0, 1.1))

	def test_valuesEqual_str_match_mismatch(self):
		"""String equality falls through to plain ==."""
		self.assertTrue(self.cat._valuesEqual('perlin', 'perlin'))
		self.assertFalse(self.cat._valuesEqual('perlin', 'simplex'))

	def test_valuesEqual_str_vs_number_not_equal(self):
		"""A string and a number are not equal."""
		self.assertFalse(self.cat._valuesEqual('5', 5))

	# --- _writeCatalog / _readCatalog round-trip ---------------------------

	def test_catalog_roundtrip_preserves_data(self):
		"""Write then read returns an equal dict, including nested params."""
		path = os.path.join(self._tmp_dir, 'catalog_test.json')
		catalog = {
			'noiseTOP': {'period': 1.0, 'amp': 0.5, 'type': 'perlin'},
			'levelTOP': {'brightness1': 1.0},
		}
		self.cat._writeCatalog(path, catalog)
		self.assertTrue(os.path.isfile(path))
		read = self.cat._readCatalog(path)
		self.assertDictEqual(read, catalog)

	def test_catalog_roundtrip_preserves_palette_key(self):
		"""The reserved _palette key survives the round-trip intact."""
		path = os.path.join(self._tmp_dir, 'catalog_palette.json')
		catalog = {
			'noiseTOP': {'period': 1.0},
			'_palette': {
				'3DScope': {'type': 'baseCOMP', 'min_children': 2},
				'TDVR': {'type': 'containerCOMP', 'min_children': 13},
			},
		}
		self.cat._writeCatalog(path, catalog)
		read = self.cat._readCatalog(path)
		self.assertIn('_palette', read)
		self.assertDictEqual(read['_palette'], catalog['_palette'])
		self.assertEqual(read['_palette']['3DScope']['min_children'], 2)

	def test_readCatalog_missing_file_returns_none(self):
		"""Reading a nonexistent catalog path returns None (not a raise)."""
		path = os.path.join(self._tmp_dir, 'does_not_exist.json')
		self.assertIsNone(self.cat._readCatalog(path))

	def test_writeCatalog_creates_parent_dirs(self):
		"""_writeCatalog mkdirs the parent directory if absent."""
		path = os.path.join(self._tmp_dir, 'nested', 'deep', 'catalog.json')
		self.cat._writeCatalog(path, {'topTOP': {'x': 1}})
		self.assertTrue(os.path.isfile(path))
		read = self.cat._readCatalog(path)
		self.assertDictEqual(read, {'topTOP': {'x': 1}})
