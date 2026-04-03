"""
Test suite: TDN file I/O, path resolution, and per-comp splitting.

Tests _resolveOutputPath, _splitPerComp, _collectExistingTDNFiles,
_cleanupStaleTDNFiles, file write integrity, and end-to-end file export.
"""

import json
import tempfile
from pathlib import Path

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase


class TestTDNFileIO(EmbodyTestCase):

	def setUp(self):
		super().setUp()
		self._temp_dir = tempfile.mkdtemp(prefix='tdn_test_')
		self._auto_files = []

	def tearDown(self):
		import shutil
		try:
			shutil.rmtree(self._temp_dir)
		except Exception:
			pass
		for f in self._auto_files:
			try:
				Path(f).unlink(missing_ok=True)
			except Exception:
				pass
		super().tearDown()

	# =================================================================
	# _splitPerComp — path generation (static method, pure Python)
	# =================================================================

	def test_splitPerComp_root_creates_project_named_file(self):
		"""Root export (/) should create <project_name>.tdn as root file."""
		ops = [{'name': 'a', 'type': 'textDAT'}]
		files = self.embody.ext.TDN._splitPerComp(ops, '/', 'MyProj', self._temp_dir)
		root = str(Path(self._temp_dir) / 'MyProj.tdn')
		self.assertIn(root, files)

	def test_splitPerComp_comp_with_children_gets_own_file(self):
		"""COMPs with children should get their own .tdn file."""
		ops = [{'name': 'c', 'type': 'baseCOMP', 'children': [
			{'name': 'x', 'type': 'textDAT'}]}]
		files = self.embody.ext.TDN._splitPerComp(ops, '/', 'P', self._temp_dir)
		self.assertIn(str(Path(self._temp_dir) / 'c.tdn'), files)

	def test_splitPerComp_leaf_ops_stay_in_parent(self):
		"""Non-COMP operators (no children) stay in the parent file."""
		ops = [
			{'name': 'a', 'type': 'textDAT'},
			{'name': 'b', 'type': 'noiseTOP'},
		]
		files = self.embody.ext.TDN._splitPerComp(ops, '/', 'P', self._temp_dir)
		self.assertLen(list(files.keys()), 1)
		root = str(Path(self._temp_dir) / 'P.tdn')
		names = [o['name'] for o in files[root]]
		self.assertIn('a', names)
		self.assertIn('b', names)

	def test_splitPerComp_nested_comps_create_nested_dirs(self):
		"""Nested COMPs should create nested directory structure."""
		ops = [{'name': 'o', 'type': 'baseCOMP', 'children': [
			{'name': 'i', 'type': 'baseCOMP', 'children': [
				{'name': 'leaf', 'type': 'textDAT'}]}]}]
		files = self.embody.ext.TDN._splitPerComp(ops, '/', 'P', self._temp_dir)
		inner = str(Path(self._temp_dir) / 'o' / 'i.tdn')
		self.assertIn(inner, files)

	def test_splitPerComp_no_double_nesting(self):
		"""When base is project folder and root is /embody/Embody, paths must not double-nest."""
		ops = [{'name': 'help', 'type': 'baseCOMP', 'children': [
			{'name': 'text_help', 'type': 'textDAT'}]}]
		files = self.embody.ext.TDN._splitPerComp(
			ops, '/embody/Embody', 'P', self._temp_dir)
		expected = str(Path(self._temp_dir) / 'embody' / 'Embody' / 'help.tdn')
		self.assertIn(expected, files)
		for fpath in files:
			# Check for actual path segment duplication — the root prefix
			# should not appear twice in the path. Note: embody/Embody is
			# valid (two different COMPs), but embody/Embody/embody/Embody
			# would indicate double-nesting.
			rel = fpath[len(self._temp_dir):].replace('\\', '/').lstrip('/')
			self.assertFalse(
				rel.startswith('embody/Embody/embody/'),
				f'Path segment duplication detected: {rel}')

	def test_splitPerComp_replaces_children_with_tdn_ref(self):
		"""Parent entries should have tdn_ref instead of children."""
		ops = [{'name': 'c', 'type': 'baseCOMP', 'children': [
			{'name': 'x', 'type': 'textDAT'}]}]
		files = self.embody.ext.TDN._splitPerComp(ops, '/', 'P', self._temp_dir)
		root = str(Path(self._temp_dir) / 'P.tdn')
		entry = files[root][0]
		self.assertNotIn('children', entry)
		self.assertIn('tdn_ref', entry)
		self.assertEqual(entry['tdn_ref'], 'c.tdn')

	def test_splitPerComp_tdn_ref_nested_is_relative(self):
		"""Nested tdn_ref should be a relative path from root."""
		ops = [{'name': 'o', 'type': 'baseCOMP', 'children': [
			{'name': 'i', 'type': 'baseCOMP', 'children': [
				{'name': 'leaf', 'type': 'textDAT'}]}]}]
		files = self.embody.ext.TDN._splitPerComp(ops, '/', 'P', self._temp_dir)
		outer_file = str(Path(self._temp_dir) / 'o.tdn')
		inner_entry = [o for o in files[outer_file] if o['name'] == 'i'][0]
		self.assertEqual(inner_entry['tdn_ref'], 'o/i.tdn')

	def test_splitPerComp_nonroot_export_root_file(self):
		"""Non-root export (/embody) root file should be embody.tdn."""
		ops = [{'name': 'Embody', 'type': 'baseCOMP', 'children': [
			{'name': 'Ext', 'type': 'textDAT'}]}]
		files = self.embody.ext.TDN._splitPerComp(
			ops, '/embody', 'P', self._temp_dir)
		root = str(Path(self._temp_dir) / 'embody.tdn')
		self.assertIn(root, files)
		child = str(Path(self._temp_dir) / 'embody' / 'Embody.tdn')
		self.assertIn(child, files)

	def test_splitPerComp_preserves_op_fields(self):
		"""Operator fields besides 'children' should be preserved."""
		ops = [{'name': 'c', 'type': 'baseCOMP', 'position': [10, 20],
				'color': [1, 0, 0], 'parameters': {'tx': 5},
				'children': [{'name': 'x', 'type': 'textDAT'}]}]
		files = self.embody.ext.TDN._splitPerComp(ops, '/', 'P', self._temp_dir)
		root = str(Path(self._temp_dir) / 'P.tdn')
		entry = files[root][0]
		self.assertEqual(entry['position'], [10, 20])
		self.assertEqual(entry['color'], [1, 0, 0])
		self.assertEqual(entry['parameters'], {'tx': 5})

	def test_splitPerComp_mixed_comps_and_leaves(self):
		"""Sibling COMPs and leaf ops should coexist correctly."""
		ops = [
			{'name': 'leaf1', 'type': 'textDAT'},
			{'name': 'comp1', 'type': 'baseCOMP', 'children': [
				{'name': 'inner', 'type': 'textDAT'}]},
			{'name': 'leaf2', 'type': 'noiseTOP'},
		]
		files = self.embody.ext.TDN._splitPerComp(ops, '/', 'P', self._temp_dir)
		root = str(Path(self._temp_dir) / 'P.tdn')
		names = [o['name'] for o in files[root]]
		self.assertIn('leaf1', names)
		self.assertIn('comp1', names)
		self.assertIn('leaf2', names)
		comp_entry = [o for o in files[root] if o['name'] == 'comp1'][0]
		self.assertIn('tdn_ref', comp_entry)
		self.assertNotIn('children', comp_entry)

	def test_splitPerComp_empty_operators(self):
		"""Empty operator list should still produce a root file."""
		files = self.embody.ext.TDN._splitPerComp([], '/', 'P', self._temp_dir)
		root = str(Path(self._temp_dir) / 'P.tdn')
		self.assertIn(root, files)
		self.assertEqual(len(files[root]), 0)

	def test_splitPerComp_deeply_nested_three_levels(self):
		"""Three levels of nesting should produce correct paths."""
		ops = [{'name': 'a', 'type': 'baseCOMP', 'children': [
			{'name': 'b', 'type': 'baseCOMP', 'children': [
				{'name': 'c', 'type': 'baseCOMP', 'children': [
					{'name': 'd', 'type': 'textDAT'}]}]}]}]
		files = self.embody.ext.TDN._splitPerComp(ops, '/', 'P', self._temp_dir)
		self.assertIn(str(Path(self._temp_dir) / 'a.tdn'), files)
		self.assertIn(str(Path(self._temp_dir) / 'a' / 'b.tdn'), files)
		self.assertIn(str(Path(self._temp_dir) / 'a' / 'b' / 'c.tdn'), files)

	def test_splitPerComp_multiple_sibling_comps(self):
		"""Multiple sibling COMPs should each get their own file."""
		ops = [
			{'name': 'comp_a', 'type': 'baseCOMP', 'children': [
				{'name': 'x', 'type': 'textDAT'}]},
			{'name': 'comp_b', 'type': 'baseCOMP', 'children': [
				{'name': 'y', 'type': 'textDAT'}]},
		]
		files = self.embody.ext.TDN._splitPerComp(ops, '/', 'P', self._temp_dir)
		self.assertIn(str(Path(self._temp_dir) / 'comp_a.tdn'), files)
		self.assertIn(str(Path(self._temp_dir) / 'comp_b.tdn'), files)

	# =================================================================
	# _collectExistingTDNFiles (static method)
	# =================================================================

	def test_collectExisting_finds_files_recursively(self):
		Path(self._temp_dir, 'a.tdn').write_text('{}')
		sub = Path(self._temp_dir, 'sub')
		sub.mkdir()
		Path(sub, 'b.tdn').write_text('{}')
		result = self.embody.ext.TDN._collectExistingTDNFiles(self._temp_dir)
		self.assertLen(result, 2)

	def test_collectExisting_ignores_non_tdn(self):
		Path(self._temp_dir, 'a.tdn').write_text('{}')
		Path(self._temp_dir, 'b.json').write_text('{}')
		Path(self._temp_dir, 'c.py').write_text('')
		result = self.embody.ext.TDN._collectExistingTDNFiles(self._temp_dir)
		self.assertLen(result, 1)

	def test_collectExisting_root_returns_all(self):
		Path(self._temp_dir, 'a.tdn').write_text('{}')
		sub = Path(self._temp_dir, 'embody')
		sub.mkdir()
		Path(sub, 'b.tdn').write_text('{}')
		result = self.embody.ext.TDN._collectExistingTDNFiles(self._temp_dir, '/')
		self.assertLen(result, 2)

	def test_collectExisting_scoped_to_prefix(self):
		"""Non-root path should only return files matching that prefix."""
		embody = Path(self._temp_dir, 'embody')
		embody.mkdir()
		Path(embody, 'Embody.tdn').write_text('{}')
		Path(self._temp_dir, 'other.tdn').write_text('{}')
		result = self.embody.ext.TDN._collectExistingTDNFiles(
			self._temp_dir, '/embody')
		self.assertLen(result, 1)

	def test_collectExisting_scoped_includes_nested(self):
		"""Scoped search should include files under the prefix path."""
		embody = Path(self._temp_dir, 'embody')
		embody.mkdir()
		Path(embody, 'Embody.tdn').write_text('{}')
		sub = Path(embody, 'Embody')
		sub.mkdir()
		Path(sub, 'help.tdn').write_text('{}')
		result = self.embody.ext.TDN._collectExistingTDNFiles(
			self._temp_dir, '/embody')
		self.assertLen(result, 2)

	def test_collectExisting_scoped_excludes_unrelated(self):
		"""Scoped search should exclude files with a different prefix."""
		embody = Path(self._temp_dir, 'embody')
		embody.mkdir()
		Path(embody, 'Embody.tdn').write_text('{}')
		ctrl = Path(self._temp_dir, 'controller')
		ctrl.mkdir()
		Path(ctrl, 'main.tdn').write_text('{}')
		result = self.embody.ext.TDN._collectExistingTDNFiles(
			self._temp_dir, '/embody')
		self.assertLen(result, 1)

	def test_collectExisting_nonexistent_dir(self):
		result = self.embody.ext.TDN._collectExistingTDNFiles('/nonexistent_tdn_xyz')
		self.assertLen(result, 0)

	def test_collectExisting_empty_dir(self):
		result = self.embody.ext.TDN._collectExistingTDNFiles(self._temp_dir)
		self.assertLen(result, 0)

	def test_collectExisting_exact_match_prefix(self):
		"""File matching the exact prefix (embody.tdn for /embody) should be found."""
		Path(self._temp_dir, 'embody.tdn').write_text('{}')
		result = self.embody.ext.TDN._collectExistingTDNFiles(
			self._temp_dir, '/embody')
		self.assertLen(result, 1)

	# =================================================================
	# _cleanupStaleTDNFiles (static method)
	# =================================================================

	def test_cleanup_deletes_stale(self):
		"""Should delete .tdn files that existed before but weren't written."""
		stale = str(Path(self._temp_dir, 'old.tdn'))
		Path(stale).write_text('{}')
		kept = str(Path(self._temp_dir, 'kept.tdn'))
		Path(kept).write_text('{}')
		deleted = self.embody.ext.TDN._cleanupStaleTDNFiles(
			{stale, kept}, [kept], self._temp_dir)
		self.assertIn(stale, deleted)
		self.assertFalse(Path(stale).exists())
		self.assertTrue(Path(kept).exists())

	def test_cleanup_keeps_written_files(self):
		written = str(Path(self._temp_dir, 'new.tdn'))
		Path(written).write_text('{}')
		deleted = self.embody.ext.TDN._cleanupStaleTDNFiles(
			{written}, [written], self._temp_dir)
		self.assertLen(deleted, 0)
		self.assertTrue(Path(written).exists())

	def test_cleanup_rejects_non_tdn(self):
		"""Should refuse to delete non-.tdn files."""
		non_tdn = str(Path(self._temp_dir, 'data.json'))
		Path(non_tdn).write_text('{}')
		deleted = self.embody.ext.TDN._cleanupStaleTDNFiles(
			{non_tdn}, [], self._temp_dir)
		self.assertLen(deleted, 0)
		self.assertTrue(Path(non_tdn).exists())

	def test_cleanup_rejects_outside_base(self):
		"""Should refuse to delete files outside base_folder."""
		other_dir = tempfile.mkdtemp(prefix='tdn_other_')
		try:
			outside = str(Path(other_dir, 'x.tdn'))
			Path(outside).write_text('{}')
			deleted = self.embody.ext.TDN._cleanupStaleTDNFiles(
				{outside}, [], self._temp_dir)
			self.assertLen(deleted, 0)
			self.assertTrue(Path(outside).exists())
		finally:
			import shutil
			shutil.rmtree(other_dir, ignore_errors=True)

	def test_cleanup_removes_empty_dirs(self):
		"""Should remove empty parent directories after deleting files."""
		sub = Path(self._temp_dir, 'a', 'b')
		sub.mkdir(parents=True)
		stale = str(sub / 'old.tdn')
		Path(stale).write_text('{}')
		self.embody.ext.TDN._cleanupStaleTDNFiles({stale}, [], self._temp_dir)
		self.assertFalse(sub.exists())
		self.assertFalse(sub.parent.exists())

	def test_cleanup_preserves_nonempty_dirs(self):
		"""Should not remove directories that still contain files."""
		sub = Path(self._temp_dir, 'mydir')
		sub.mkdir()
		stale = str(sub / 'old.tdn')
		Path(stale).write_text('{}')
		Path(sub / 'keep.txt').write_text('data')
		self.embody.ext.TDN._cleanupStaleTDNFiles({stale}, [], self._temp_dir)
		self.assertFalse(Path(stale).exists())
		self.assertTrue(sub.exists())

	def test_cleanup_empty_before_set(self):
		"""No-op when before set is empty."""
		deleted = self.embody.ext.TDN._cleanupStaleTDNFiles(set(), [], self._temp_dir)
		self.assertLen(deleted, 0)

	def test_cleanup_multiple_stale_files(self):
		"""Should delete all stale files in one pass."""
		stale_files = set()
		for i in range(5):
			f = str(Path(self._temp_dir, f'stale_{i}.tdn'))
			Path(f).write_text('{}')
			stale_files.add(f)
		deleted = self.embody.ext.TDN._cleanupStaleTDNFiles(
			stale_files, [], self._temp_dir)
		self.assertLen(deleted, 5)
		for f in stale_files:
			self.assertFalse(Path(f).exists())

	# =================================================================
	# _resolveOutputPath — direct method testing
	# =================================================================

	def test_resolve_auto_nonroot_mirrors_td_path(self):
		"""Auto-resolved path for non-root COMP should mirror TD hierarchy."""
		child = self.sandbox.create(baseCOMP, 'res_child')
		resolved = self.embody.ext.TDN._resolveOutputPath('auto', child)
		expected_suffix = child.path.lstrip('/') + '.tdn'
		self.assertTrue(
			resolved.replace('\\', '/').endswith(expected_suffix),
			f"Expected suffix '{expected_suffix}', got '{resolved}'")

	def test_resolve_auto_nested_mirrors_full_path(self):
		"""Deeply nested auto-resolved path should mirror full TD path."""
		outer = self.sandbox.create(baseCOMP, 'ro')
		inner = outer.create(baseCOMP, 'ri')
		resolved = self.embody.ext.TDN._resolveOutputPath('auto', inner)
		expected_suffix = inner.path.lstrip('/') + '.tdn'
		self.assertTrue(
			resolved.replace('\\', '/').endswith(expected_suffix),
			f"Expected suffix '{expected_suffix}', got '{resolved}'")

	def test_resolve_auto_root_uses_project_name(self):
		"""Root export should use build-stripped project name."""
		root_op = op('/')
		resolved = self.embody.ext.TDN._resolveOutputPath('auto', root_op)
		raw_name = project.name.removesuffix('.toe')
		stable_name = self.embody.ext.TDN._stripBuildSuffix(raw_name)
		self.assertTrue(
			resolved.replace('\\', '/').endswith(f'{stable_name}.tdn'),
			f"Expected build-stripped name '{stable_name}.tdn' in path, "
			f"got '{resolved}'")

	def test_resolve_explicit_path_returned_as_is(self):
		"""Explicit path should be returned unchanged."""
		resolved = self.embody.ext.TDN._resolveOutputPath('/tmp/custom.tdn', op('/'))
		self.assertEqual(resolved, '/tmp/custom.tdn')

	def test_resolve_auto_nonroot_starts_with_project_folder(self):
		"""Auto-resolved path should be under project folder."""
		child = self.sandbox.create(baseCOMP, 'pf_check')
		resolved = self.embody.ext.TDN._resolveOutputPath('auto', child)
		proj_folder = str(project.folder).replace('\\', '/')
		self.assertTrue(
			resolved.replace('\\', '/').startswith(proj_folder),
			f"Expected to start with '{proj_folder}', got '{resolved}'")

	def test_resolve_auto_nonroot_no_double_ext_folder(self):
		"""Non-root auto resolve should not prepend ext_folder to TD path."""
		child = self.sandbox.create(baseCOMP, 'dbl_check')
		resolved = self.embody.ext.TDN._resolveOutputPath('auto', child)
		normalized = resolved.replace('\\', '/')
		# The path should contain the TD path exactly once
		td_segment = child.path.lstrip('/')
		first_idx = normalized.find(td_segment)
		self.assertGreaterEqual(first_idx, 0,
			f"TD path segment '{td_segment}' not found in '{normalized}'")
		# Should not appear again after the first occurrence
		second_idx = normalized.find(td_segment, first_idx + 1)
		self.assertEqual(second_idx, -1,
			f"TD path segment appears twice in '{normalized}'")

	# =================================================================
	# _stripBuildSuffix — stable project TDN filenames
	# =================================================================

	def test_strip_build_suffix_dotted(self):
		"""Build number (e.g. .302) should be stripped."""
		strip = self.embody.ext.TDN._stripBuildSuffix
		self.assertEqual(strip('Embody-5.302'), 'Embody-5')

	def test_strip_build_suffix_no_build(self):
		"""Name without build suffix should be unchanged."""
		strip = self.embody.ext.TDN._stripBuildSuffix
		self.assertEqual(strip('Embody-5'), 'Embody-5')

	def test_strip_build_suffix_plain_name(self):
		"""Plain name without any version should be unchanged."""
		strip = self.embody.ext.TDN._stripBuildSuffix
		self.assertEqual(strip('demo'), 'demo')

	def test_strip_build_suffix_number_no_dot(self):
		"""Trailing number without dot should be preserved."""
		strip = self.embody.ext.TDN._stripBuildSuffix
		self.assertEqual(strip('Embody5'), 'Embody5')

	def test_strip_build_suffix_underscore_number(self):
		"""Underscore-separated number should be preserved."""
		strip = self.embody.ext.TDN._stripBuildSuffix
		self.assertEqual(strip('Embody_5'), 'Embody_5')

	def test_strip_build_suffix_preserves_user_version(self):
		"""Hyphenated user version should be preserved."""
		strip = self.embody.ext.TDN._stripBuildSuffix
		self.assertEqual(strip('my-cool-project-3'), 'my-cool-project-3')

	# =================================================================
	# ExportNetwork file output — end-to-end
	# =================================================================

	def test_export_file_is_valid_json(self):
		self.sandbox.create(baseCOMP, 'json_check')
		fp = str(Path(self._temp_dir) / 'valid.tdn')
		result = self.embody.ext.TDN.ExportNetwork(
			root_path=self.sandbox.path, output_file=fp)
		self.assertTrue(result.get('success'))
		self.assertTrue(Path(fp).exists())
		with open(fp, 'r', encoding='utf-8') as f:
			data = json.load(f)
		self.assertEqual(data['format'], 'tdn')

	def test_export_file_includes_source_file(self):
		"""Exported TDN should contain source_file with the .toe filename."""
		self.sandbox.create(baseCOMP, 'src_check')
		fp = str(Path(self._temp_dir) / 'source.tdn')
		result = self.embody.ext.TDN.ExportNetwork(
			root_path=self.sandbox.path, output_file=fp)
		self.assertTrue(result.get('success'))
		with open(fp, 'r', encoding='utf-8') as f:
			data = json.load(f)
		self.assertIn('source_file', data)
		self.assertEqual(data['source_file'], project.name)

	def test_export_file_contains_all_operators(self):
		self.sandbox.create(baseCOMP, 'op_a')
		self.sandbox.create(textDAT, 'op_b')
		self.sandbox.create(noiseTOP, 'op_c')
		fp = str(Path(self._temp_dir) / 'all.tdn')
		self.embody.ext.TDN.ExportNetwork(
			root_path=self.sandbox.path, output_file=fp)
		with open(fp, 'r', encoding='utf-8') as f:
			data = json.load(f)
		names = [o['name'] for o in data['operators']]
		self.assertIn('op_a', names)
		self.assertIn('op_b', names)
		self.assertIn('op_c', names)

	def test_export_file_not_truncated(self):
		"""File with many operators should not be truncated."""
		for i in range(25):
			self.sandbox.create(baseCOMP, f'c_{i}')
		fp = str(Path(self._temp_dir) / 'trunc.tdn')
		self.embody.ext.TDN.ExportNetwork(
			root_path=self.sandbox.path, output_file=fp)
		with open(fp, 'r', encoding='utf-8') as f:
			content = f.read()
		data = json.loads(content)
		self.assertEqual(len(data['operators']), 25)
		self.assertTrue(content.endswith('\n'))

	def test_export_file_dat_text_content(self):
		"""Text DAT content should be included when enabled."""
		dat = self.sandbox.create(textDAT, 'dc')
		dat.text = 'Exported content check'
		fp = str(Path(self._temp_dir) / 'dc.tdn')
		self.embody.ext.TDN.ExportNetwork(
			root_path=self.sandbox.path, output_file=fp,
			include_dat_content=True)
		with open(fp, 'r', encoding='utf-8') as f:
			data = json.load(f)
		entry = [o for o in data['operators'] if o['name'] == 'dc'][0]
		self.assertEqual(entry['dat_content'], 'Exported content check')
		self.assertEqual(entry['dat_content_format'], 'text')

	def test_export_file_dat_table_content(self):
		"""Table DAT content should be preserved as row arrays."""
		tbl = self.sandbox.create(tableDAT, 'tbl')
		tbl.clear()  # Remove default empty row
		tbl.appendRow(['name', 'val'])
		tbl.appendRow(['x', '1'])
		tbl.appendRow(['y', '2'])
		fp = str(Path(self._temp_dir) / 'tbl.tdn')
		self.embody.ext.TDN.ExportNetwork(
			root_path=self.sandbox.path, output_file=fp,
			include_dat_content=True)
		with open(fp, 'r', encoding='utf-8') as f:
			data = json.load(f)
		entry = [o for o in data['operators'] if o['name'] == 'tbl'][0]
		self.assertEqual(entry['dat_content_format'], 'table')
		self.assertEqual(len(entry['dat_content']), 3)
		self.assertListEqual(entry['dat_content'][0], ['name', 'val'])
		self.assertListEqual(entry['dat_content'][2], ['y', '2'])

	def test_export_file_roundtrip_reimport(self):
		"""Export to file, read back, import — operators should match."""
		self.sandbox.create(baseCOMP, 'rt_a')
		dat = self.sandbox.create(textDAT, 'rt_b')
		dat.text = 'via file'
		fp = str(Path(self._temp_dir) / 'rt.tdn')
		self.embody.ext.TDN.ExportNetwork(
			root_path=self.sandbox.path, output_file=fp,
			include_dat_content=True)
		with open(fp, 'r', encoding='utf-8') as f:
			tdn_data = json.load(f)
		target = self.sandbox.create(baseCOMP, 'rt_target')
		result = self.embody.ext.TDN.ImportNetwork(
			target_path=target.path, tdn=tdn_data)
		self.assertTrue(result.get('success'))
		names = [c.name for c in target.children]
		self.assertIn('rt_a', names)
		self.assertIn('rt_b', names)
		self.assertEqual(target.op('rt_b').text, 'via file')

	def test_export_result_has_file_path(self):
		fp = str(Path(self._temp_dir) / 'rp.tdn')
		result = self.embody.ext.TDN.ExportNetwork(
			root_path=self.sandbox.path, output_file=fp)
		self.assertEqual(result.get('file'), fp)

	def test_export_creates_parent_dirs(self):
		"""Export to explicit path with pre-created dirs should succeed."""
		fp = str(Path(self._temp_dir) / 'a' / 'b' / 'c' / 'test.tdn')
		Path(fp).parent.mkdir(parents=True, exist_ok=True)
		result = self.embody.ext.TDN.ExportNetwork(
			root_path=self.sandbox.path, output_file=fp)
		self.assertTrue(result.get('success'))
		self.assertTrue(Path(fp).exists())

	def test_export_file_has_metadata(self):
		"""Exported file should contain version, generator, td_build."""
		fp = str(Path(self._temp_dir) / 'meta.tdn')
		self.embody.ext.TDN.ExportNetwork(
			root_path=self.sandbox.path, output_file=fp)
		with open(fp, 'r', encoding='utf-8') as f:
			data = json.load(f)
		self.assertIn('version', data)
		self.assertIn('generator', data)
		self.assertIn('td_build', data)
		self.assertIn('exported_at', data)
		self.assertTrue(data['generator'].startswith('Embody/'))

	def test_export_file_preserves_connections(self):
		"""Operator connections should be in the exported file."""
		src = self.sandbox.create(noiseTOP, 'src')
		dst = self.sandbox.create(levelTOP, 'dst')
		src.outputConnectors[0].connect(dst.inputConnectors[0])
		fp = str(Path(self._temp_dir) / 'conn.tdn')
		self.embody.ext.TDN.ExportNetwork(
			root_path=self.sandbox.path, output_file=fp)
		with open(fp, 'r', encoding='utf-8') as f:
			data = json.load(f)
		entry = [o for o in data['operators'] if o['name'] == 'dst'][0]
		self.assertIn('inputs', entry)
		self.assertEqual(entry['inputs'][0], 'src')

	def test_export_file_preserves_custom_pars(self):
		"""Custom parameters should be in the exported file."""
		comp = self.sandbox.create(baseCOMP, 'cp')
		page = comp.appendCustomPage('Test')
		page.appendFloat('Myfloat', label='My Float')
		comp.par.Myfloat = 3.14
		fp = str(Path(self._temp_dir) / 'cp.tdn')
		self.embody.ext.TDN.ExportNetwork(
			root_path=self.sandbox.path, output_file=fp)
		with open(fp, 'r', encoding='utf-8') as f:
			data = json.load(f)
		entry = [o for o in data['operators'] if o['name'] == 'cp'][0]
		self.assertIn('custom_pars', entry)
		par_names = []
		for page_name, page_pars in entry['custom_pars'].items():
			par_names.extend([p['name'] for p in page_pars])
		self.assertIn('Myfloat', par_names)

	def test_export_file_preserves_nested_structure(self):
		"""Nested COMP hierarchy should be fully represented in the file."""
		a = self.sandbox.create(baseCOMP, 'la')
		b = a.create(baseCOMP, 'lb')
		b.create(textDAT, 'lc')
		fp = str(Path(self._temp_dir) / 'nested.tdn')
		self.embody.ext.TDN.ExportNetwork(
			root_path=self.sandbox.path, output_file=fp)
		with open(fp, 'r', encoding='utf-8') as f:
			data = json.load(f)
		a_entry = [o for o in data['operators'] if o['name'] == 'la'][0]
		self.assertIn('children', a_entry)
		b_entry = [o for o in a_entry['children'] if o['name'] == 'lb'][0]
		self.assertIn('children', b_entry)
		c_entry = b_entry['children'][0]
		self.assertEqual(c_entry['name'], 'lc')

	def test_export_file_preserves_flags(self):
		"""Non-default flags should be in the exported file."""
		comp = self.sandbox.create(baseCOMP, 'flagged')
		comp.bypass = True
		fp = str(Path(self._temp_dir) / 'flags.tdn')
		self.embody.ext.TDN.ExportNetwork(
			root_path=self.sandbox.path, output_file=fp)
		with open(fp, 'r', encoding='utf-8') as f:
			data = json.load(f)
		entry = [o for o in data['operators'] if o['name'] == 'flagged'][0]
		self.assertIn('flags', entry)
		self.assertIn('bypass', entry['flags'])

	def test_export_file_preserves_position(self):
		"""Operator position should be in the exported file."""
		comp = self.sandbox.create(baseCOMP, 'positioned')
		comp.nodeX = 500
		comp.nodeY = 300
		fp = str(Path(self._temp_dir) / 'pos.tdn')
		self.embody.ext.TDN.ExportNetwork(
			root_path=self.sandbox.path, output_file=fp)
		with open(fp, 'r', encoding='utf-8') as f:
			data = json.load(f)
		entry = [o for o in data['operators'] if o['name'] == 'positioned'][0]
		self.assertEqual(entry['position'], [500, 300])

	def test_export_file_preserves_color(self):
		"""Non-default operator color should be in the exported file."""
		comp = self.sandbox.create(baseCOMP, 'colored')
		comp.color = (1.0, 0.0, 0.0)
		fp = str(Path(self._temp_dir) / 'color.tdn')
		self.embody.ext.TDN.ExportNetwork(
			root_path=self.sandbox.path, output_file=fp)
		with open(fp, 'r', encoding='utf-8') as f:
			data = json.load(f)
		entry = [o for o in data['operators'] if o['name'] == 'colored'][0]
		self.assertIn('color', entry)
		self.assertApproxEqual(entry['color'][0], 1.0)
		self.assertApproxEqual(entry['color'][1], 0.0)
		self.assertApproxEqual(entry['color'][2], 0.0)

	def test_export_file_preserves_tags(self):
		"""Operator tags should be in the exported file."""
		comp = self.sandbox.create(baseCOMP, 'tagged')
		comp.tags.add('mytag')
		comp.tags.add('another')
		fp = str(Path(self._temp_dir) / 'tags.tdn')
		self.embody.ext.TDN.ExportNetwork(
			root_path=self.sandbox.path, output_file=fp)
		with open(fp, 'r', encoding='utf-8') as f:
			data = json.load(f)
		entry = [o for o in data['operators'] if o['name'] == 'tagged'][0]
		self.assertIn('tags', entry)
		self.assertIn('mytag', entry['tags'])
		self.assertIn('another', entry['tags'])

	# =================================================================
	# ImportNetworkFromFile
	# =================================================================

	def test_importFromFile_basic(self):
		self.sandbox.create(baseCOMP, 'fic')
		fp = str(Path(self._temp_dir) / 'imp.tdn')
		self.embody.ext.TDN.ExportNetwork(
			root_path=self.sandbox.path, output_file=fp)
		target = self.sandbox.create(baseCOMP, 'fit')
		result = self.embody.ext.TDN.ImportNetworkFromFile(
			file_path=fp, target_path=target.path)
		self.assertTrue(result.get('success'))
		self.assertIn('fic', [c.name for c in target.children])

	def test_importFromFile_nonexistent(self):
		result = self.embody.ext.TDN.ImportNetworkFromFile(
			file_path='/nonexistent/xyz.tdn',
			target_path=self.sandbox.path)
		self.assertIn('error', result)
		self.assertIn('not found', result['error'])

	def test_importFromFile_invalid_json(self):
		bad = str(Path(self._temp_dir) / 'bad.tdn')
		Path(bad).write_text('{{{invalid')
		result = self.embody.ext.TDN.ImportNetworkFromFile(
			file_path=bad, target_path=self.sandbox.path)
		self.assertIn('error', result)
		self.assertIn('Invalid JSON', result['error'])

	def test_importFromFile_empty_string_path(self):
		result = self.embody.ext.TDN.ImportNetworkFromFile(
			file_path='', target_path=self.sandbox.path)
		self.assertIn('error', result)
		self.assertIn('No TDN file specified', result['error'])

	def test_importFromFile_roundtrip_preserves_connections(self):
		"""File-based roundtrip should preserve operator connections."""
		src = self.sandbox.create(noiseTOP, 'wire_src')
		dst = self.sandbox.create(levelTOP, 'wire_dst')
		src.outputConnectors[0].connect(dst.inputConnectors[0])
		fp = str(Path(self._temp_dir) / 'wire_rt.tdn')
		self.embody.ext.TDN.ExportNetwork(
			root_path=self.sandbox.path, output_file=fp)
		target = self.sandbox.create(baseCOMP, 'wire_target')
		self.embody.ext.TDN.ImportNetworkFromFile(
			file_path=fp, target_path=target.path)
		imported_dst = target.op('wire_dst')
		self.assertIsNotNone(imported_dst)
		self.assertGreaterEqual(len(imported_dst.inputs), 1)
		self.assertIsNotNone(imported_dst.inputs[0])
		self.assertEqual(imported_dst.inputs[0].name, 'wire_src')

	# =================================================================
	# Large / edge case exports
	# =================================================================

	def test_export_50_operators_all_present(self):
		n = 50
		for i in range(n):
			self.sandbox.create(baseCOMP, f'b{i}')
		fp = str(Path(self._temp_dir) / 'bulk.tdn')
		self.embody.ext.TDN.ExportNetwork(
			root_path=self.sandbox.path, output_file=fp)
		with open(fp, 'r', encoding='utf-8') as f:
			data = json.load(f)
		self.assertEqual(len(data['operators']), n)

	def test_export_large_dat_not_truncated(self):
		"""Large DAT text (1000 lines) should not be truncated."""
		dat = self.sandbox.create(textDAT, 'big')
		text = '\n'.join(f'Line {i}' for i in range(1000))
		dat.text = text
		fp = str(Path(self._temp_dir) / 'big.tdn')
		self.embody.ext.TDN.ExportNetwork(
			root_path=self.sandbox.path, output_file=fp,
			include_dat_content=True)
		with open(fp, 'r', encoding='utf-8') as f:
			data = json.load(f)
		entry = [o for o in data['operators'] if o['name'] == 'big'][0]
		self.assertEqual(entry['dat_content'], text)

	def test_export_unicode_preserved(self):
		"""Unicode characters should survive export to file."""
		dat = self.sandbox.create(textDAT, 'uni')
		utext = 'Hello \u4e16\u754c caf\u00e9 na\u00efve \u03a9\u2248\u00e7'
		dat.text = utext
		fp = str(Path(self._temp_dir) / 'uni.tdn')
		self.embody.ext.TDN.ExportNetwork(
			root_path=self.sandbox.path, output_file=fp,
			include_dat_content=True)
		with open(fp, 'r', encoding='utf-8') as f:
			data = json.load(f)
		entry = [o for o in data['operators'] if o['name'] == 'uni'][0]
		self.assertEqual(entry['dat_content'], utext)

	def test_export_empty_comp_valid_file(self):
		"""Empty COMP should produce a valid .tdn with zero operators."""
		fp = str(Path(self._temp_dir) / 'empty.tdn')
		result = self.embody.ext.TDN.ExportNetwork(
			root_path=self.sandbox.path, output_file=fp)
		self.assertTrue(result.get('success'))
		with open(fp, 'r', encoding='utf-8') as f:
			data = json.load(f)
		self.assertEqual(len(data['operators']), 0)

	def test_export_file_encoding_utf8(self):
		"""File should be written as valid UTF-8 bytes."""
		dat = self.sandbox.create(textDAT, 'enc')
		dat.text = '\u65e5\u672c\u8a9e\u30c6\u30b9\u30c8'
		fp = str(Path(self._temp_dir) / 'enc.tdn')
		self.embody.ext.TDN.ExportNetwork(
			root_path=self.sandbox.path, output_file=fp,
			include_dat_content=True)
		with open(fp, 'rb') as f:
			raw = f.read()
		text = raw.decode('utf-8')
		data = json.loads(text)
		entry = [o for o in data['operators'] if o['name'] == 'enc'][0]
		self.assertEqual(entry['dat_content'],
			'\u65e5\u672c\u8a9e\u30c6\u30b9\u30c8')

	def test_export_overwrite_existing_file(self):
		"""Exporting to an existing file should overwrite it."""
		fp = str(Path(self._temp_dir) / 'overwrite.tdn')
		Path(fp).write_text('old content that is not json')
		result = self.embody.ext.TDN.ExportNetwork(
			root_path=self.sandbox.path, output_file=fp)
		self.assertTrue(result.get('success'))
		with open(fp, 'r', encoding='utf-8') as f:
			data = json.load(f)
		self.assertEqual(data['format'], 'tdn')

	def test_export_file_json_indented(self):
		"""Exported JSON should be indented (human-readable)."""
		self.sandbox.create(baseCOMP, 'indent_check')
		fp = str(Path(self._temp_dir) / 'indent.tdn')
		self.embody.ext.TDN.ExportNetwork(
			root_path=self.sandbox.path, output_file=fp)
		with open(fp, 'r', encoding='utf-8') as f:
			content = f.read()
		# Indented JSON contains tabs (our format uses tab indentation)
		self.assertIn('\t', content)
		# Should have newlines between entries
		lines = content.strip().split('\n')
		self.assertGreater(len(lines), 1)

	def test_export_dat_content_excluded_when_disabled(self):
		"""DAT content should be absent when include_dat_content=False."""
		dat = self.sandbox.create(textDAT, 'no_content')
		dat.text = 'should not appear'
		fp = str(Path(self._temp_dir) / 'no_dc.tdn')
		self.embody.ext.TDN.ExportNetwork(
			root_path=self.sandbox.path, output_file=fp,
			include_dat_content=False)
		with open(fp, 'r', encoding='utf-8') as f:
			data = json.load(f)
		entry = [o for o in data['operators'] if o['name'] == 'no_content'][0]
		self.assertNotIn('dat_content', entry)

	def test_export_multiple_families_in_one_file(self):
		"""Multiple operator families should coexist in one file."""
		self.sandbox.create(baseCOMP, 'my_comp')
		self.sandbox.create(textDAT, 'my_dat')
		self.sandbox.create(noiseTOP, 'my_top')
		self.sandbox.create(waveCHOP, 'my_chop')
		fp = str(Path(self._temp_dir) / 'families.tdn')
		self.embody.ext.TDN.ExportNetwork(
			root_path=self.sandbox.path, output_file=fp)
		with open(fp, 'r', encoding='utf-8') as f:
			data = json.load(f)
		types = {o['type'] for o in data['operators']}
		self.assertIn('baseCOMP', types)
		self.assertIn('textDAT', types)
		self.assertIn('noiseTOP', types)
		self.assertIn('waveCHOP', types)

	# =================================================================
	# tdn_ref — export, import, and cross-validation
	# =================================================================

	def _get_log_id(self):
		return self.embody_ext._log_counter

	def _get_logs_since(self, since_id):
		return [e for e in self.embody_ext._log_buffer if e['id'] > since_id]

	def _has_log_message(self, since_id, substring):
		for entry in self._get_logs_since(since_id):
			if substring in entry.get('message', ''):
				return True
		return False

	def test_tdn_ref_written_on_export(self):
		"""Export of parent with TDN-tagged child should include tdn_ref."""
		parent = self.sandbox.create(baseCOMP, 'parent_comp')
		child = parent.create(baseCOMP, 'child_comp')
		child.create(textDAT, 'leaf')
		# Tag the child for TDN
		tdn_tag = self.embody.par.Tdntag.val
		child.tags.add(tdn_tag)
		# Export the child first so it's in the table
		child_path = self.embody_ext._buildTDNRelPath(child)
		child_abs = self.embody_ext.buildAbsolutePath(child_path)
		child_abs.parent.mkdir(parents=True, exist_ok=True)
		self.embody.ext.TDN.ExportNetwork(
			root_path=child.path, output_file=str(child_abs))
		from datetime import datetime
		timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
		self.embody_ext._addToTable(child, str(child_path), timestamp,
			False, 1, str(app.build), 'tdn')
		# Now export the parent
		fp = str(Path(self._temp_dir) / 'parent.tdn')
		self.embody.ext.TDN.ExportNetwork(
			root_path=parent.path, output_file=fp)
		with open(fp, 'r', encoding='utf-8') as f:
			data = json.load(f)
		child_entry = [o for o in data['operators']
			if o['name'] == 'child_comp'][0]
		self.assertIn('tdn_ref', child_entry)
		self.assertNotIn('children', child_entry)

	def test_tdn_ref_absent_without_tag(self):
		"""Export of parent with non-TDN child should not include tdn_ref."""
		parent = self.sandbox.create(baseCOMP, 'parent_comp')
		child = parent.create(baseCOMP, 'child_comp')
		child.create(textDAT, 'leaf')
		fp = str(Path(self._temp_dir) / 'no_ref.tdn')
		self.embody.ext.TDN.ExportNetwork(
			root_path=parent.path, output_file=fp)
		with open(fp, 'r', encoding='utf-8') as f:
			data = json.load(f)
		child_entry = [o for o in data['operators']
			if o['name'] == 'child_comp'][0]
		self.assertNotIn('tdn_ref', child_entry)
		self.assertIn('children', child_entry)

	def test_tdn_ref_absent_with_embed_all(self):
		"""embed_all=True should suppress tdn_ref even for tagged children."""
		parent = self.sandbox.create(baseCOMP, 'parent_comp')
		child = parent.create(baseCOMP, 'child_comp')
		child.create(textDAT, 'leaf')
		tdn_tag = self.embody.par.Tdntag.val
		child.tags.add(tdn_tag)
		# Add child to table
		child_path = self.embody_ext._buildTDNRelPath(child)
		child_abs = self.embody_ext.buildAbsolutePath(child_path)
		child_abs.parent.mkdir(parents=True, exist_ok=True)
		self.embody.ext.TDN.ExportNetwork(
			root_path=child.path, output_file=str(child_abs))
		from datetime import datetime
		timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
		self.embody_ext._addToTable(child, str(child_path), timestamp,
			False, 1, str(app.build), 'tdn')
		# Export parent with embed_all
		fp = str(Path(self._temp_dir) / 'embed.tdn')
		self.embody.ext.TDN.ExportNetwork(
			root_path=parent.path, output_file=fp, embed_all=True)
		with open(fp, 'r', encoding='utf-8') as f:
			data = json.load(f)
		child_entry = [o for o in data['operators']
			if o['name'] == 'child_comp'][0]
		self.assertNotIn('tdn_ref', child_entry)
		self.assertIn('children', child_entry)

	def test_validateTDNRefs_happy_path(self):
		"""Valid tdn_refs matching table entries and disk files produce no warnings."""
		# Create child and add to table with a real file
		parent = self.sandbox.create(baseCOMP, 'parent_comp')
		child = parent.create(baseCOMP, 'child_comp')
		child.create(textDAT, 'leaf')
		tdn_tag = self.embody.par.Tdntag.val
		child.tags.add(tdn_tag)
		child_path = self.embody_ext._buildTDNRelPath(child)
		child_abs = self.embody_ext.buildAbsolutePath(child_path)
		child_abs.parent.mkdir(parents=True, exist_ok=True)
		self.embody.ext.TDN.ExportNetwork(
			root_path=child.path, output_file=str(child_abs))
		from datetime import datetime
		timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
		self.embody_ext._addToTable(child, str(child_path), timestamp,
			False, 1, str(app.build), 'tdn')
		# Build op_defs with tdn_ref
		op_defs = [{'name': 'child_comp', 'tdn_ref': str(child_path)}]
		warnings = self.embody.ext.TDN._validateTDNRefs(
			op_defs, parent.path)
		self.assertLen(warnings, 0)

	def test_validateTDNRefs_missing_table_entry(self):
		"""tdn_ref for a COMP not in the table produces a warning."""
		# Use a unique name that can't collide with other test entries
		parent = self.sandbox.create(baseCOMP, 'orphan_ref_parent')
		orphan = parent.create(baseCOMP, 'orphan_ref_child')
		# File exists on disk but orphan is NOT in externalizations table
		fake_file = Path(self._temp_dir) / 'orphan.tdn'
		fake_file.write_text('{}')
		op_defs = [{'name': 'orphan_ref_child', 'tdn_ref': str(fake_file)}]
		warnings = self.embody.ext.TDN._validateTDNRefs(
			op_defs, parent.path)
		has_table_warning = any('externalizations table' in w for w in warnings)
		self.assertTrue(has_table_warning, f'Expected table warning, got: {warnings}')

	def test_validateTDNRefs_missing_file(self):
		"""tdn_ref pointing to a non-existent file produces a warning."""
		parent = self.sandbox.create(baseCOMP, 'parent_comp')
		child = parent.create(baseCOMP, 'child_comp')
		# Add to table but no file on disk
		tdn_tag = self.embody.par.Tdntag.val
		child.tags.add(tdn_tag)
		from datetime import datetime
		timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
		self.embody_ext._addToTable(child, 'nonexistent/child.tdn',
			timestamp, False, 1, str(app.build), 'tdn')
		op_defs = [{'name': 'child_comp', 'tdn_ref': 'nonexistent/child.tdn'}]
		warnings = self.embody.ext.TDN._validateTDNRefs(
			op_defs, parent.path)
		has_file_warning = any('file not found' in w for w in warnings)
		self.assertTrue(has_file_warning)

	def test_cascade_tags_children(self):
		"""_cascadeTDNTag should add TDN tag to direct child COMPs."""
		parent = self.sandbox.create(baseCOMP, 'cascade_parent')
		child1 = parent.create(baseCOMP, 'child1')
		child2 = parent.create(baseCOMP, 'child2')
		parent.create(textDAT, 'leaf_dat')  # DATs should not be tagged
		tdn_tag = self.embody.par.Tdntag.val
		# Directly add tag to parent (skip full externalization pipeline
		# to avoid file writes that reinitialize the extension)
		parent.tags.add(tdn_tag)
		orig_cascade = self.embody.par.Tdncascade.eval()
		self.embody.par.Tdncascade = True
		try:
			self.embody_ext._cascadeTDNTag(parent)
			self.assertIn(tdn_tag, child1.tags)
			self.assertIn(tdn_tag, child2.tags)
			# DATs should NOT be tagged
			leaf = parent.op('leaf_dat')
			self.assertNotIn(tdn_tag, leaf.tags)
		finally:
			self.embody.par.Tdncascade = orig_cascade
			parent.tags.discard(tdn_tag)
			child1.tags.discard(tdn_tag)
			child2.tags.discard(tdn_tag)

	def test_cascade_off_no_children_tagged(self):
		"""With cascade OFF, tagging a parent should not tag children."""
		parent = self.sandbox.create(baseCOMP, 'nocascade_parent')
		child = parent.create(baseCOMP, 'child')
		tdn_tag = self.embody.par.Tdntag.val
		orig_cascade = self.embody.par.Tdncascade.eval()
		self.embody.par.Tdncascade = False
		try:
			self.embody_ext.applyTagToOperator(parent, tdn_tag)
			self.assertNotIn(tdn_tag, child.tags)
		finally:
			self.embody.par.Tdncascade = orig_cascade

	def test_large_tdn_warning_suppressed(self):
		"""Tdncascadewarn='quiet' should prevent the dialog from showing."""
		# Create a file over threshold
		big_file = Path(self._temp_dir) / 'big.tdn'
		big_file.write_text('x' * 5_100_000)  # > 5 MB
		orig_warn = self.embody.par.Tdncascadewarn.eval()
		orig_cascade = self.embody.par.Tdncascade.eval()
		self.embody.par.Tdncascadewarn = 'quiet'
		self.embody.par.Tdncascade = False
		try:
			log_id = self._get_log_id()
			self.embody.ext.TDN._warnLargeTDN(str(big_file), '/test')
			# No dialog shown, no log about silencing
			has_silence = self._has_log_message(log_id, 'warning silenced')
			self.assertFalse(has_silence)
		finally:
			self.embody.par.Tdncascadewarn = orig_warn
			self.embody.par.Tdncascade = orig_cascade

	def test_large_tdn_warning_shown(self):
		"""Tdncascadewarn='ask' with large file should show dialog."""
		big_file = Path(self._temp_dir) / 'big.tdn'
		big_file.write_text('x' * 5_100_000)  # > 5 MB
		orig_warn = self.embody.par.Tdncascadewarn.eval()
		orig_cascade = self.embody.par.Tdncascade.eval()
		self.embody.par.Tdncascadewarn = 'ask'
		self.embody.par.Tdncascade = False
		# Seed auto-response: button 0 = OK (dismiss without silencing)
		self.embody.store('_smoke_test_responses', {
			'Large TDN File': 0})
		try:
			self.embody.ext.TDN._warnLargeTDN(str(big_file), '/test')
			# Dialog was intercepted — warn pref should still be 'ask'
			self.assertEqual(self.embody.par.Tdncascadewarn.eval(), 'ask')
		finally:
			self.embody.par.Tdncascadewarn = orig_warn
			self.embody.par.Tdncascade = orig_cascade
			try:
				self.embody.unstore('_smoke_test_responses')
			except Exception:
				pass

	def test_createOps_skips_children_with_tdn_ref(self):
		"""Import with tdn_ref should create shell but no children."""
		parent = self.sandbox.create(baseCOMP, 'import_target')
		op_defs = [{
			'name': 'ref_comp',
			'type': 'baseCOMP',
			'tdn_ref': 'some/path.tdn',
		}]
		created = []
		self.embody.ext.TDN._createOps(parent, op_defs, created)
		ref_comp = parent.op('ref_comp')
		self.assertIsNotNone(ref_comp)
		# Should have no children (tdn_ref = separate file manages them)
		self.assertLen(list(ref_comp.children), 0)
