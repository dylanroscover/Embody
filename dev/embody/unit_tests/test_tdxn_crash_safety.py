"""
Test suite: TDN crash safety - atomic writes, backup rotation, validation,
and import rollback.

Tests cover:
  A. Atomic write behavior (filesystem-isolated)
  B. Backup rotation (filesystem-isolated)
  C. Post-write validation (filesystem-isolated)
  D. Safe write orchestration (filesystem-isolated)
  E. Failure injection and recovery (filesystem-isolated)
  F. Stress tests with real TD operators (sandbox-isolated)
"""

import json
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase


def _make_valid_tdn(op_count=1):
	"""Build a minimal valid TDN document dict."""
	operators = []
	for i in range(op_count):
		operators.append({'name': f'op_{i}', 'type': 'noiseTOP'})
	return {
		'format': 'tdn',
		'version': '1.0',
		'operators': operators,
	}


def _make_valid_tdn_json(op_count=1):
	"""Build a minimal valid TDN JSON string."""
	return json.dumps(_make_valid_tdn(op_count))


class TestTDNCrashSafety(EmbodyTestCase):

	def setUp(self):
		super().setUp()
		self._temp_dir = tempfile.mkdtemp(prefix='tdn_safety_')
		# Simulate a project folder structure
		self._proj_folder = self._temp_dir
		self._tdn_dir = os.path.join(self._temp_dir, 'embody', 'Foo')
		os.makedirs(self._tdn_dir, exist_ok=True)
		self._tdn_path = os.path.join(self._tdn_dir, 'bar.tdn')

	def tearDown(self):
		# Restore permissions before cleanup (in case tests made dirs read-only)
		for dirpath, dirnames, filenames in os.walk(self._temp_dir):
			try:
				os.chmod(dirpath, stat.S_IRWXU)
			except Exception:
				pass
		try:
			shutil.rmtree(self._temp_dir)
		except Exception:
			pass
		super().tearDown()

	@property
	def tdn(self):
		return self.embody.ext.TDXN

	# =================================================================
	# A. Atomic Write Tests
	# =================================================================

	def test_A01_atomic_write_creates_file(self):
		"""Atomic write should create a new file with correct content."""
		content = _make_valid_tdn_json()
		self.tdn._atomic_write(self._tdn_path, content)
		self.assertTrue(Path(self._tdn_path).is_file())
		self.assertEqual(Path(self._tdn_path).read_text(encoding='utf-8'), content)

	def test_A02_atomic_write_replaces_existing(self):
		"""Atomic write should replace existing file content."""
		Path(self._tdn_path).write_text('old content', encoding='utf-8')
		new_content = _make_valid_tdn_json(5)
		self.tdn._atomic_write(self._tdn_path, new_content)
		self.assertEqual(
			Path(self._tdn_path).read_text(encoding='utf-8'), new_content)

	def test_A03_atomic_write_no_temp_residue(self):
		"""No .tmp files should remain after successful atomic write."""
		self.tdn._atomic_write(self._tdn_path, _make_valid_tdn_json())
		tmp_files = list(Path(self._tdn_dir).glob('*.tmp'))
		self.assertEqual(len(tmp_files), 0,
			f'Residual temp files found: {tmp_files}')

	def test_A04_atomic_write_readonly_dir_fails_cleanly(self):
		"""Writing to a read-only directory should raise without leaving debris."""
		if sys.platform == 'win32':
			# chmod cannot write-protect a DIRECTORY on Windows (the readonly
			# attribute is ignored for content changes; only ACLs block it),
			# so the write succeeds and the contract is untestable here.
			self.skipTest('chmod cannot write-protect a directory on Windows')
		ro_dir = os.path.join(self._temp_dir, 'readonly')
		os.makedirs(ro_dir)
		os.chmod(ro_dir, stat.S_IRUSR | stat.S_IXUSR)
		target = os.path.join(ro_dir, 'test.tdn')
		raised = False
		try:
			self.tdn._atomic_write(target, _make_valid_tdn_json())
		except Exception:
			raised = True
		self.assertTrue(raised, 'Should have raised for read-only dir')
		self.assertFalse(Path(target).exists(),
			'No partial file should exist')
		# Restore permissions for cleanup
		os.chmod(ro_dir, stat.S_IRWXU)

	# =================================================================
	# B0. Backup location, dual-read, and failure surfacing (v6.1.6)
	# =================================================================

	def test_B00_writes_land_in_embody_backup_never_legacy(self):
		"""Rotation writes ONLY the current dir. The legacy .tdn_backup/ is
		read but never written -- nothing migrates or deletes it, because a
		bulk move would race a concurrent rotate from a second TD instance
		and _safe_write_tdn swallows that failure into the result dict."""
		Path(self._tdn_path).write_text('v1', encoding='utf-8')
		self.tdn._rotate_backups(self._tdn_path, self._proj_folder)
		self.assertTrue(
			(Path(self._proj_folder) / '.embody_backup').is_dir(),
			'rotation must create .embody_backup/')
		self.assertFalse(
			(Path(self._proj_folder) / '.tdn_backup').exists(),
			'rotation must never write the legacy dir')

	def test_B00b_find_falls_back_to_legacy_tdn_backup(self):
		"""A project that upgraded mid-life has its only recovery copy in
		.tdn_backup/. The finder must still reach it -- otherwise the
		rename silently discards every existing backup."""
		legacy = self.tdn._get_backup_path(
			self._tdn_path, self._proj_folder, '.bak', legacy=True)
		legacy.parent.mkdir(parents=True, exist_ok=True)
		legacy.write_text(_make_valid_tdn_json(7), encoding='utf-8')

		found = self.tdn._find_existing_backup(
			self._tdn_path, self._proj_folder)
		self.assertIsNotNone(found, 'legacy backup must be found')
		self.assertEqual(found.read_text(encoding='utf-8'),
						 _make_valid_tdn_json(7))

	def test_B00c_newest_wins_regardless_of_directory(self):
		"""Selection is by MTIME, not probe order. A pre-v6.1.6 Embody
		(downgrade, mixed fleet, shared project folder) keeps rotating into
		the LEGACY dir while the current one sits frozen -- returning the
		current copy just because it was probed first hands back a stale
		network, and both consumers feed it to ImportNetwork(clear_first)."""
		cur = self.tdn._get_backup_path(
			self._tdn_path, self._proj_folder, '.bak')
		cur.parent.mkdir(parents=True, exist_ok=True)
		cur.write_text(_make_valid_tdn_json(2), encoding='utf-8')
		os.utime(cur, (1_000_000, 1_000_000))          # old

		legacy = self.tdn._get_backup_path(
			self._tdn_path, self._proj_folder, '.bak', legacy=True)
		legacy.parent.mkdir(parents=True, exist_ok=True)
		legacy.write_text(_make_valid_tdn_json(9), encoding='utf-8')
		os.utime(legacy, (2_000_000, 2_000_000))       # newer

		found = self.tdn._find_existing_backup(
			self._tdn_path, self._proj_folder)
		self.assertEqual(found, legacy,
			'the NEWER legacy copy must win over an older current one')

		# ... and the ordinary case still resolves to the current dir.
		os.utime(cur, (3_000_000, 3_000_000))
		self.assertEqual(
			self.tdn._find_existing_backup(
				self._tdn_path, self._proj_folder), cur)

	def test_B00d_corrupt_bak_falls_through_to_bak2(self):
		"""THE REGRESSION: .bak2 was WRITTEN on every rotation since v5 and
		read by nothing. Existence-only selection would also return a
		TRUNCATED .bak -- rotation copies with shutil.copy2, which is not
		atomic -- and stop, making .bak2 unreachable in exactly the case it
		exists for. Candidates must be validated, not merely stat'd."""
		Path(self._tdn_path).write_text(
			_make_valid_tdn_json(3), encoding='utf-8')
		self.tdn._rotate_backups(self._tdn_path, self._proj_folder)
		Path(self._tdn_path).write_text(
			_make_valid_tdn_json(4), encoding='utf-8')
		self.tdn._rotate_backups(self._tdn_path, self._proj_folder)

		bak = self.tdn._get_backup_path(
			self._tdn_path, self._proj_folder, '.bak')
		bak2 = self.tdn._get_backup_path(
			self._tdn_path, self._proj_folder, '.bak2')
		self.assertTrue(bak2.is_file(), '.bak2 must exist after 2 rotations')
		# Truncate the newest generation the way a killed copy2 would.
		bak.write_text('{"format": "tdxn", "operators": [', encoding='utf-8')

		found = self.tdn._find_existing_backup(
			self._tdn_path, self._proj_folder)
		self.assertEqual(found, bak2,
			'a corrupt .bak must fall through to .bak2, not be returned')

	def test_B00d2_migrated_suffix_backups_are_still_found(self):
		"""MigrateToTDXN renames foo.tdn -> foo.tdxn on disk WITHOUT
		touching backups, so a migrated COMP's only copies stay named
		foo.tdn.bak. Probing the current name alone orphans them -- one
		real backup dir was found holding Embody.tdn.bak beside
		guard_victim.tdxn.bak."""
		migrated = os.path.join(self._tdn_dir, 'bar.tdxn')
		old_name_bak = self.tdn._get_backup_path(
			self._tdn_path, self._proj_folder, '.bak')   # bar.tdn.bak
		old_name_bak.parent.mkdir(parents=True, exist_ok=True)
		old_name_bak.write_text(_make_valid_tdn_json(5), encoding='utf-8')

		found = self.tdn._find_existing_backup(migrated, self._proj_folder)
		self.assertEqual(found, old_name_bak,
			'a pre-migration .tdn.bak must be reachable from the .tdxn')

	def test_B00d3_rotation_retires_this_files_legacy_copies(self):
		"""Downgrade guard. Nothing bulk-migrates (that would race a
		concurrent rotate), but once THIS file has a current-dir .bak the
		legacy copies are retired. Otherwise a rollback to <= v6.1.5 reads
		a .tdn_backup copy frozen at upgrade time and silently restores a
		stale network over live work; with them gone the old code reports
		'no backup available' and leaves the COMP alone."""
		legacy = self.tdn._get_backup_path(
			self._tdn_path, self._proj_folder, '.bak', legacy=True)
		legacy.parent.mkdir(parents=True, exist_ok=True)
		legacy.write_text(_make_valid_tdn_json(1), encoding='utf-8')
		legacy2 = self.tdn._get_backup_path(
			self._tdn_path, self._proj_folder, '.bak2', legacy=True)
		legacy2.write_text(_make_valid_tdn_json(1), encoding='utf-8')

		Path(self._tdn_path).write_text(
			_make_valid_tdn_json(6), encoding='utf-8')
		self.tdn._rotate_backups(self._tdn_path, self._proj_folder)

		self.assertFalse(legacy.is_file(), 'legacy .bak must be retired')
		self.assertFalse(legacy2.is_file(), 'legacy .bak2 must be retired')
		self.assertTrue(
			self.tdn._get_backup_path(
				self._tdn_path, self._proj_folder, '.bak').is_file(),
			'and only AFTER the replacement is in place')

	def test_B00d4_backup_dir_is_self_ignoring(self):
		"""The dir is written unconditionally, but the managed .gitignore
		entry only lands when Envoy configures an AI client. An Envoy-off,
		Convoy-only, or declined-prompt install would otherwise accumulate
		untracked .bak files inside a plain `git clean -fd`'s reach."""
		Path(self._tdn_path).write_text(
			_make_valid_tdn_json(), encoding='utf-8')
		self.tdn._rotate_backups(self._tdn_path, self._proj_folder)
		marker = (Path(self._proj_folder) / '.embody_backup' / '.gitignore')
		self.assertTrue(marker.is_file(),
			'the backup dir must ignore itself without Envoy')
		self.assertEqual(marker.read_text(encoding='utf-8').strip(), '*')

	def test_B00e_no_backup_anywhere_returns_none(self):
		"""The finder must not invent a path -- callers branch on None."""
		self.assertIsNone(self.tdn._find_existing_backup(
			self._tdn_path, self._proj_folder))

	def test_B00f_rotation_failure_rides_back_on_the_result(self):
		"""Rotation failure must not BLOCK the write, but must never be
		silent: that write ran with no recovery net. It cannot log from
		here (this path is reached from an export WORKER thread), so it
		rides back on the result dict for a main-thread caller to voice.

		The failure is induced the way it actually happens -- something
		occupies the backup directory's path -- rather than by patching the
		live extension class out from under a running session.
		"""
		Path(self._tdn_path).write_text('old', encoding='utf-8')
		# A plain FILE where the backup tree must be rooted: mkdir(parents)
		# raises, exactly as it would on a permission or disk error.
		blocker = Path(self._proj_folder) / '.embody_backup'
		blocker.write_text('not a directory', encoding='utf-8')

		result = self.tdn._safe_write_tdn(
			self._tdn_path, _make_valid_tdn_json(), self._proj_folder)

		self.assertTrue(result.get('success'),
			'a rotation failure must not block the write')
		self.assertTrue(result.get('backup_error'),
			'the failure must be surfaced, not swallowed')
		self.assertEqual(
			Path(self._tdn_path).read_text(encoding='utf-8'),
			_make_valid_tdn_json(),
			'the write itself must still have landed')

	# =================================================================
	# B. Backup Rotation Tests
	# =================================================================

	def test_B01_rotate_first_export_creates_bak(self):
		"""First rotation should create .bak, no .bak2."""
		Path(self._tdn_path).write_text('v1', encoding='utf-8')
		self.tdn._rotate_backups(self._tdn_path, self._proj_folder)
		bak = self.tdn._get_backup_path(
			self._tdn_path, self._proj_folder, '.bak')
		bak2 = self.tdn._get_backup_path(
			self._tdn_path, self._proj_folder, '.bak2')
		self.assertTrue(bak.is_file(), '.bak should exist')
		self.assertFalse(bak2.is_file(), '.bak2 should not exist yet')
		self.assertEqual(bak.read_text(encoding='utf-8'), 'v1')

	def test_B02_rotate_second_export_rotates(self):
		"""Second rotation: .bak -> .bak2, current -> .bak."""
		Path(self._tdn_path).write_text('v1', encoding='utf-8')
		self.tdn._rotate_backups(self._tdn_path, self._proj_folder)
		# Overwrite current with v2 (simulating atomic write)
		Path(self._tdn_path).write_text('v2', encoding='utf-8')
		self.tdn._rotate_backups(self._tdn_path, self._proj_folder)
		bak = self.tdn._get_backup_path(
			self._tdn_path, self._proj_folder, '.bak')
		bak2 = self.tdn._get_backup_path(
			self._tdn_path, self._proj_folder, '.bak2')
		self.assertEqual(bak.read_text(encoding='utf-8'), 'v2')
		self.assertEqual(bak2.read_text(encoding='utf-8'), 'v1')

	def test_B03_rotate_third_export_overwrites_bak2(self):
		"""Third rotation: oldest .bak2 is overwritten, only 2 backups."""
		Path(self._tdn_path).write_text('v1', encoding='utf-8')
		self.tdn._rotate_backups(self._tdn_path, self._proj_folder)
		Path(self._tdn_path).write_text('v2', encoding='utf-8')
		self.tdn._rotate_backups(self._tdn_path, self._proj_folder)
		Path(self._tdn_path).write_text('v3', encoding='utf-8')
		self.tdn._rotate_backups(self._tdn_path, self._proj_folder)
		bak = self.tdn._get_backup_path(
			self._tdn_path, self._proj_folder, '.bak')
		bak2 = self.tdn._get_backup_path(
			self._tdn_path, self._proj_folder, '.bak2')
		self.assertEqual(bak.read_text(encoding='utf-8'), 'v3')
		self.assertEqual(bak2.read_text(encoding='utf-8'), 'v2')

	def test_B04_rotate_noop_when_no_file(self):
		"""Rotation is a no-op when .tdn doesn't exist yet."""
		nonexistent = os.path.join(self._tdn_dir, 'nope.tdn')
		self.tdn._rotate_backups(nonexistent, self._proj_folder)
		bak = self.tdn._get_backup_path(
			nonexistent, self._proj_folder, '.bak')
		self.assertFalse(bak.is_file())

	def test_B05_rotate_mirrors_directory_structure(self):
		"""Backup path should mirror the relative directory hierarchy."""
		bak = self.tdn._get_backup_path(
			self._tdn_path, self._proj_folder, '.bak')
		expected = (Path(self._proj_folder) / '.embody_backup'
					/ 'embody' / 'Foo' / 'bar.tdn.bak')
		self.assertEqual(str(bak), str(expected))

	def test_B06_rotate_creates_parent_dirs(self):
		"""Rotation should auto-create the backup directory hierarchy."""
		deep_dir = os.path.join(self._temp_dir, 'deep', 'nested', 'path')
		os.makedirs(deep_dir, exist_ok=True)
		deep_tdn = os.path.join(deep_dir, 'test.tdn')
		Path(deep_tdn).write_text('content', encoding='utf-8')
		self.tdn._rotate_backups(deep_tdn, self._proj_folder)
		bak = self.tdn._get_backup_path(
			deep_tdn, self._proj_folder, '.bak')
		self.assertTrue(bak.is_file(), 'Backup should exist in auto-created dirs')

	def test_B07_rotate_preserves_file_content(self):
		"""Backup content must be byte-identical to the original."""
		content = _make_valid_tdn_json(50)
		Path(self._tdn_path).write_text(content, encoding='utf-8')
		self.tdn._rotate_backups(self._tdn_path, self._proj_folder)
		bak = self.tdn._get_backup_path(
			self._tdn_path, self._proj_folder, '.bak')
		self.assertEqual(bak.read_text(encoding='utf-8'), content)

	# =================================================================
	# C. Validation Tests
	# =================================================================

	def test_C01_validate_valid_tdn(self):
		"""Well-formed TDN JSON should pass validation."""
		Path(self._tdn_path).write_text(
			_make_valid_tdn_json(), encoding='utf-8')
		result = self.tdn._validate_tdn_file(self._tdn_path)
		self.assertTrue(result.get('valid'))

	def test_C02_validate_truncated_json(self):
		"""Truncated JSON should fail validation."""
		full = _make_valid_tdn_json(10)
		Path(self._tdn_path).write_text(full[:len(full)//2], encoding='utf-8')
		result = self.tdn._validate_tdn_file(self._tdn_path)
		self.assertFalse(result.get('valid'))
		self.assertIn('Invalid TDXN', result.get('error', ''))

	def test_C03_validate_missing_format_key(self):
		"""JSON without 'format' key should fail."""
		bad = json.dumps({'operators': []})
		Path(self._tdn_path).write_text(bad, encoding='utf-8')
		result = self.tdn._validate_tdn_file(self._tdn_path)
		self.assertFalse(result.get('valid'))
		self.assertIn('format', result.get('error', ''))

	def test_C04_validate_missing_operators_key(self):
		"""JSON with format but no operators should fail."""
		bad = json.dumps({'format': 'tdn', 'version': '1.0'})
		Path(self._tdn_path).write_text(bad, encoding='utf-8')
		result = self.tdn._validate_tdn_file(self._tdn_path)
		self.assertFalse(result.get('valid'))
		self.assertIn('operators', result.get('error', ''))

	def test_C05_validate_empty_file(self):
		"""Empty file should fail validation."""
		Path(self._tdn_path).write_text('', encoding='utf-8')
		result = self.tdn._validate_tdn_file(self._tdn_path)
		self.assertFalse(result.get('valid'))

	def test_C06_validate_binary_garbage(self):
		"""Random binary data should fail validation."""
		Path(self._tdn_path).write_bytes(os.urandom(256))
		result = self.tdn._validate_tdn_file(self._tdn_path)
		self.assertFalse(result.get('valid'))

	# =================================================================
	# D. Safe Write Orchestration Tests
	# =================================================================

	def test_D01_safe_write_full_cycle(self):
		"""Safe write: backup + atomic write + validate, all succeed."""
		# Write initial version
		v1 = _make_valid_tdn_json(3)
		Path(self._tdn_path).write_text(v1, encoding='utf-8')
		# Safe write a new version
		v2 = _make_valid_tdn_json(5)
		result = self.tdn._safe_write_tdn(
			self._tdn_path, v2, self._proj_folder)
		self.assertTrue(result.get('success'))
		# Current file has new content
		self.assertEqual(
			Path(self._tdn_path).read_text(encoding='utf-8'), v2)
		# Backup has old content
		bak = self.tdn._get_backup_path(
			self._tdn_path, self._proj_folder, '.bak')
		self.assertTrue(bak.is_file())
		self.assertEqual(bak.read_text(encoding='utf-8'), v1)

	def test_D02_safe_write_first_export_no_backup(self):
		"""First export: no existing file, no backup created."""
		new_path = os.path.join(self._tdn_dir, 'fresh.tdn')
		content = _make_valid_tdn_json()
		result = self.tdn._safe_write_tdn(
			new_path, content, self._proj_folder)
		self.assertTrue(result.get('success'))
		self.assertEqual(
			Path(new_path).read_text(encoding='utf-8'), content)
		bak = self.tdn._get_backup_path(
			new_path, self._proj_folder, '.bak')
		self.assertFalse(bak.is_file(),
			'No backup for first export')

	def test_D03_safe_write_restores_on_validation_failure(self):
		"""If written content is invalid TDN, backup should be restorable."""
		# Write a valid v1 first
		v1 = _make_valid_tdn_json(3)
		Path(self._tdn_path).write_text(v1, encoding='utf-8')
		# Try safe-writing invalid content (missing 'format' key)
		bad_content = json.dumps({'not_tdn': True})
		result = self.tdn._safe_write_tdn(
			self._tdn_path, bad_content, self._proj_folder)
		# Should report error
		self.assertIn('error', result)
		self.assertIn('restored from', result['error'])
		# v6.1.6: the message NAMES the file restored from, and the path
		# rides back structurally. The fallback chain can land on an older
		# generation or the legacy dir, and a silent revert to a stale
		# network is its own data loss -- 'restored from backup' could not
		# tell the user which one they got.
		expected_bak = self.tdn._get_backup_path(
			self._tdn_path, self._proj_folder, '.bak')
		self.assertEqual(result.get('restored_from'), str(expected_bak))
		self.assertIn(str(expected_bak), result['error'])
		# File should be restored to v1
		restored = Path(self._tdn_path).read_text(encoding='utf-8')
		self.assertEqual(restored, v1)

	# =================================================================
	# E. Failure Injection Tests
	# =================================================================

	def test_E01_corrupt_truncated_json_recovery(self):
		"""Truncated .tdn should be detectable; .bak stays intact."""
		v1 = _make_valid_tdn_json(10)
		Path(self._tdn_path).write_text(v1, encoding='utf-8')
		# Create a backup manually
		self.tdn._rotate_backups(self._tdn_path, self._proj_folder)
		# Corrupt the main file
		Path(self._tdn_path).write_text(v1[:20], encoding='utf-8')
		# Validate catches it
		result = self.tdn._validate_tdn_file(self._tdn_path)
		self.assertFalse(result.get('valid'))
		# Backup is still intact
		bak = self.tdn._get_backup_path(
			self._tdn_path, self._proj_folder, '.bak')
		bak_result = self.tdn._validate_tdn_file(str(bak))
		self.assertTrue(bak_result.get('valid'))

	def test_E02_corrupt_empty_file_recovery(self):
		"""Empty .tdn should be detectable; .bak stays intact."""
		v1 = _make_valid_tdn_json(5)
		Path(self._tdn_path).write_text(v1, encoding='utf-8')
		self.tdn._rotate_backups(self._tdn_path, self._proj_folder)
		Path(self._tdn_path).write_text('', encoding='utf-8')
		result = self.tdn._validate_tdn_file(self._tdn_path)
		self.assertFalse(result.get('valid'))
		bak = self.tdn._get_backup_path(
			self._tdn_path, self._proj_folder, '.bak')
		self.assertEqual(bak.read_text(encoding='utf-8'), v1)

	def test_E03_corrupt_wrong_format_recovery(self):
		"""JSON with wrong format key should fail validation."""
		v1 = _make_valid_tdn_json()
		Path(self._tdn_path).write_text(v1, encoding='utf-8')
		self.tdn._rotate_backups(self._tdn_path, self._proj_folder)
		bad = json.dumps({'format': 'not_tdn', 'operators': []})
		Path(self._tdn_path).write_text(bad, encoding='utf-8')
		result = self.tdn._validate_tdn_file(self._tdn_path)
		self.assertFalse(result.get('valid'))
		self.assertIn('format', result.get('error', ''))

	def test_E04_corrupt_binary_garbage_recovery(self):
		"""Binary garbage should fail validation; .bak stays intact."""
		v1 = _make_valid_tdn_json()
		Path(self._tdn_path).write_text(v1, encoding='utf-8')
		self.tdn._rotate_backups(self._tdn_path, self._proj_folder)
		Path(self._tdn_path).write_bytes(os.urandom(512))
		result = self.tdn._validate_tdn_file(self._tdn_path)
		self.assertFalse(result.get('valid'))
		bak = self.tdn._get_backup_path(
			self._tdn_path, self._proj_folder, '.bak')
		self.assertEqual(bak.read_text(encoding='utf-8'), v1)

	def test_E05_missing_tdn_file_with_backup(self):
		"""When .tdn is deleted but .bak exists, backup is usable."""
		v1 = _make_valid_tdn_json(3)
		Path(self._tdn_path).write_text(v1, encoding='utf-8')
		self.tdn._rotate_backups(self._tdn_path, self._proj_folder)
		os.unlink(self._tdn_path)
		bak = self.tdn._get_backup_path(
			self._tdn_path, self._proj_folder, '.bak')
		self.assertTrue(bak.is_file())
		# Backup can be parsed as valid TDN
		doc = self.tdn.tdn_load(bak.read_text(encoding='utf-8'))
		self.assertEqual(doc.get('format'), 'tdn')

	def test_E06_missing_tdn_and_backup(self):
		"""Both .tdn and .bak missing - validation should fail gracefully."""
		result = self.tdn._validate_tdn_file(self._tdn_path)
		self.assertFalse(result.get('valid'))
		bak = self.tdn._get_backup_path(
			self._tdn_path, self._proj_folder, '.bak')
		self.assertFalse(bak.is_file())

	def test_E07_readonly_backup_dir(self):
		"""Read-only backup dir should not block the write itself."""
		# The CURRENT backup dir -- chmod'ing the legacy one made this test
		# vacuous after the v6.1.6 rename: rotation simply created a fresh
		# .embody_backup/ and succeeded, so nothing about backup failure
		# was exercised.
		backup_dir = os.path.join(self._proj_folder, '.embody_backup')
		os.makedirs(backup_dir)
		os.chmod(backup_dir, stat.S_IRUSR | stat.S_IXUSR)
		# Write initial file
		v1 = _make_valid_tdn_json()
		Path(self._tdn_path).write_text(v1, encoding='utf-8')
		# Safe write should succeed even though backup rotation fails
		v2 = _make_valid_tdn_json(5)
		result = self.tdn._safe_write_tdn(
			self._tdn_path, v2, self._proj_folder)
		self.assertTrue(result.get('success'),
			f'Write should succeed despite backup failure: {result}')
		self.assertEqual(
			Path(self._tdn_path).read_text(encoding='utf-8'), v2)
		if sys.platform != 'win32':
			# chmod does not write-protect a directory on Windows (see
			# test_A04), so only assert the surfacing where it can fail.
			self.assertTrue(result.get('backup_error'),
				f'a rotation failure must ride back, not vanish: {result}')
		# Restore permissions for cleanup
		os.chmod(backup_dir, stat.S_IRWXU)

	def test_E08_disk_full_simulation(self):
		"""Atomic write to a file that already exists preserves original on error.

		We simulate by writing to a read-only file (os.replace should
		still work on macOS/Linux since rename is a directory operation,
		but we test the general error handling path).
		"""
		# This test verifies the error handling path works without crashing.
		# True disk-full simulation requires OS-level tricks (tmpfs with
		# size limit) that may not be portable. We test what we can.
		v1 = _make_valid_tdn_json()
		Path(self._tdn_path).write_text(v1, encoding='utf-8')
		# The atomic write should succeed because os.replace works on
		# the directory, not the file. This just confirms no crash.
		v2 = _make_valid_tdn_json(3)
		self.tdn._atomic_write(self._tdn_path, v2)
		self.assertEqual(
			Path(self._tdn_path).read_text(encoding='utf-8'), v2)

	# =================================================================
	# F. Stress Tests (TD sandbox - real operators)
	# =================================================================

	def _buildStressNetwork(self, parent_comp, op_count=1000):
		"""Build a network with `op_count` operators of mixed types.

		Distribution: TOPs 30%, CHOPs 20%, SOPs 20%, DATs 15%, COMPs 15%.
		Wires sequential chains within each type.
		Adds DAT content to text DATs and custom parameters to some COMPs.

		Returns expected total operator count (including children of COMPs).
		"""
		n_tops = int(op_count * 0.30)
		n_chops = int(op_count * 0.20)
		n_sops = int(op_count * 0.20)
		n_dats = int(op_count * 0.15)
		n_comps = op_count - n_tops - n_chops - n_sops - n_dats

		total = 0

		# TOPs - chained
		tops = []
		for i in range(n_tops):
			t = parent_comp.create(noiseTOP, f'top_{i}')
			if i > 0:
				tops[i - 1].outputConnectors[0].connect(
					t.inputConnectors[0])
			tops.append(t)
			total += 1

		# CHOPs
		for i in range(n_chops):
			parent_comp.create(constantCHOP, f'chop_{i}')
			total += 1

		# SOPs - chained
		sops = []
		for i in range(n_sops):
			s = parent_comp.create(gridSOP, f'sop_{i}')
			if i > 0:
				sops[i - 1].outputConnectors[0].connect(
					s.inputConnectors[0])
			sops.append(s)
			total += 1

		# DATs with content
		for i in range(n_dats):
			d = parent_comp.create(textDAT, f'dat_{i}')
			lines = [f'Line {j} of DAT {i}' for j in range(100)]
			d.text = '\n'.join(lines)
			total += 1

		# COMPs with 1 child each, some with custom pars
		for i in range(n_comps):
			c = parent_comp.create(baseCOMP, f'comp_{i}')
			c.create(noiseTOP, 'inner')
			total += 2  # COMP + child
			if i % 10 == 0:
				page = c.appendCustomPage('Config')
				page.appendFloat('Speed', label='Speed')
				c.par.Speed = float(i) / 10.0

		return total

	def test_F01_1000_operator_export_roundtrip(self):
		"""1000 operators: export -> import -> verify count and connections."""
		expected = self._buildStressNetwork(self.sandbox, 1000)
		# Export to temp file
		tdn_file = os.path.join(self._temp_dir, 'stress.tdn')
		result = self.tdn.ExportNetwork(
			root_path=self.sandbox.path,
			include_dat_content=True,
			output_file=tdn_file)
		self.assertTrue(result.get('success'), f'Export failed: {result}')
		# Validate file
		validation = self.tdn._validate_tdn_file(tdn_file)
		self.assertTrue(validation.get('valid'),
			f'Validation failed: {validation}')
		# Verify TDN JSON has correct operator count
		tdn_doc = self.tdn.tdn_load(Path(tdn_file).read_text(encoding='utf-8'))
		top_level_ops = len(tdn_doc.get('operators', []))
		self.assertGreater(top_level_ops, 900,
			f'Expected 900+ top-level ops in TDN, got {top_level_ops}')
		# Clear and reimport
		for c in list(self.sandbox.children):
			try:
				if c.dock is not None:
					c.dock = None
			except Exception:
				pass
		for c in list(self.sandbox.children):
			c.destroy()
		imp = self.tdn.ImportNetwork(
			target_path=self.sandbox.path, tdn=tdn_doc, clear_first=True)
		self.assertTrue(imp.get('success'), f'Import failed: {imp}')
		# Verify import created the expected operator count
		created = imp.get('created_count', 0)
		self.assertGreaterEqual(created, expected - 20,
			f'Expected ~{expected} ops created, got {created}')

	def test_F02_1000_operator_backup_rotation(self):
		"""1000 ops: export twice, verify backup rotation."""
		self._buildStressNetwork(self.sandbox, 1000)
		tdn_file = os.path.join(self._temp_dir, 'stress.tdn')
		# First export
		r1 = self.tdn.ExportNetwork(
			root_path=self.sandbox.path,
			include_dat_content=True,
			output_file=tdn_file)
		self.assertTrue(r1.get('success'))
		v1_content = Path(tdn_file).read_text(encoding='utf-8')
		# Modify a parameter
		c0 = self.sandbox.op('comp_0')
		if c0 and hasattr(c0.par, 'Speed'):
			c0.par.Speed = 99.9
		# Second export (triggers backup rotation)
		r2 = self.tdn.ExportNetwork(
			root_path=self.sandbox.path,
			include_dat_content=True,
			output_file=tdn_file)
		self.assertTrue(r2.get('success'))
		# Verify .bak has v1 content
		proj_folder = str(project.folder)
		bak = self.tdn._get_backup_path(tdn_file, proj_folder, '.bak')
		# For temp dir exports, backup path may use temp dir as project folder
		bak_temp = self.tdn._get_backup_path(
			tdn_file, self._proj_folder, '.bak')
		found_bak = None
		for candidate in [bak, bak_temp]:
			if candidate.is_file():
				found_bak = candidate
				break
		self.assertIsNotNone(found_bak, 'Backup file should exist')
		# Validate all files are valid JSON
		for f in [tdn_file, str(found_bak)]:
			v = self.tdn._validate_tdn_file(f)
			self.assertTrue(v.get('valid'), f'{f} failed validation: {v}')

	# =================================================================
	# G: no-op guard -- an unchanged network must not be rewritten
	# =================================================================

	def _write(self, doc):
		return self.tdn._safe_write_tdn(
			self._tdn_path, json.dumps(doc), self._proj_folder)

	def test_G01_skips_rewrite_when_network_is_unchanged(self):
		"""Every .tdn write funnels through _safe_write_tdn, so this guard is
		what stops an explicit save (manager Save, save_externalization,
		dirty-driven SaveTDN) from rewriting a file whose network is
		identical. Without it the file reads modified in `git status` while
		`git diff` renders EMPTY -- textconv strips exactly these keys.
		"""
		base = dict(_make_valid_tdn(2), build=1, generator='Embody/6.1.0',
					td_build='099.2025.33070', source_file='A.toe',
					exported_at='2026-08-27T00:00:00Z')
		self.assertTrue(self._write(base).get('success'))
		before = open(self._tdn_path, 'rb').read()
		mtime = os.path.getmtime(self._tdn_path)

		# Same network, entirely fresh volatile header -- the real-world case.
		churned = dict(base, build=2, generator='Embody/6.1.9',
					   source_file='A.7.toe',
					   exported_at='2026-09-01T12:34:56Z')
		res = self._write(churned)
		self.assertTrue(res.get('success'))
		self.assertTrue(res.get('skipped'),
						'unchanged network should report skipped: %r' % res)
		self.assertEqual(open(self._tdn_path, 'rb').read(), before,
						 'file was rewritten despite an identical network')
		self.assertEqual(os.path.getmtime(self._tdn_path), mtime,
						 'mtime moved, so the file was reopened for writing')

	def test_G02_still_writes_a_real_content_change(self):
		"""The guard must never swallow a genuine edit."""
		self.assertTrue(self._write(_make_valid_tdn(2)).get('success'))
		before = open(self._tdn_path, 'rb').read()
		res = self._write(_make_valid_tdn(3))
		self.assertTrue(res.get('success'))
		self.assertFalse(res.get('skipped'), 'a real change must be written')
		self.assertNotEqual(open(self._tdn_path, 'rb').read(), before)

	def test_G03_still_writes_the_format_convergence(self):
		"""`format` and `version` are deliberately NOT volatile, so the
		one-time tdn -> tdxn header convergence still writes. If the guard
		ever treated them as volatile, 6.0-stamped headers would stick
		forever."""
		self.assertTrue(self._write(_make_valid_tdn(1)).get('success'))
		before = open(self._tdn_path, 'rb').read()
		res = self._write(dict(_make_valid_tdn(1), format='tdxn'))
		self.assertTrue(res.get('success'))
		self.assertFalse(res.get('skipped'),
						 'the format bump must still be written')
		self.assertNotEqual(open(self._tdn_path, 'rb').read(), before)

	def test_G04_skip_does_not_rotate_backups(self):
		"""The guard sits BEFORE backup rotation, so a no-op save must not
		churn .tdn_backup/ or push a good generation out of .bak2."""
		self.assertTrue(self._write(_make_valid_tdn(1)).get('success'))
		self.assertTrue(self._write(_make_valid_tdn(2)).get('success'))
		bak = self.tdn._get_backup_path(
			self._tdn_path, self._proj_folder, '.bak')
		self.assertTrue(bak.is_file(), 'fixture: a .bak should exist by now')
		bak_before = bak.read_bytes()

		res = self._write(_make_valid_tdn(2))          # identical -> skip
		self.assertTrue(res.get('skipped'))
		self.assertEqual(bak.read_bytes(), bak_before,
						 'a skipped write still rotated the backups')

	def test_F03_1000_operator_corrupt_and_rollback(self):
		"""1000 ops: export, corrupt .tdn, rollback from saved content."""
		expected = self._buildStressNetwork(self.sandbox, 1000)
		tdn_file = os.path.join(self._temp_dir, 'stress.tdn')
		# First write a dummy so second export creates a backup
		Path(tdn_file).write_text(_make_valid_tdn_json(1), encoding='utf-8')
		r = self.tdn.ExportNetwork(
			root_path=self.sandbox.path,
			include_dat_content=True,
			output_file=tdn_file)
		self.assertTrue(r.get('success'))
		# Read the valid content before corrupting
		valid_content = Path(tdn_file).read_text(encoding='utf-8')
		valid_tdn = self.tdn.tdn_load(valid_content)
		# Corrupt the file
		Path(tdn_file).write_text('CORRUPTED!!!', encoding='utf-8')
		# Verify corruption is detectable
		v = self.tdn._validate_tdn_file(tdn_file)
		self.assertFalse(v.get('valid'), 'Corrupted file should fail validation')
		# Clear sandbox and import from the valid content (simulating rollback)
		for c in list(self.sandbox.children):
			try:
				if c.dock is not None:
					c.dock = None
			except Exception:
				pass
		for c in list(self.sandbox.children):
			c.destroy()
		imp = self.tdn.ImportNetwork(
			target_path=self.sandbox.path, tdn=valid_tdn, clear_first=True)
		self.assertTrue(imp.get('success'), f'Rollback import failed: {imp}')
		created = imp.get('created_count', 0)
		self.assertGreaterEqual(created, expected - 20,
			f'Expected ~{expected} ops after rollback, got {created}')

	def test_F04_1000_operator_partial_import_recovery(self):
		"""1000 ops: export, delete half operators from JSON, detect mismatch."""
		expected = self._buildStressNetwork(self.sandbox, 1000)
		# Export
		result = self.tdn.ExportNetwork(
			root_path=self.sandbox.path, include_dat_content=True)
		self.assertTrue(result.get('success'))
		full_tdn = result['tdn']
		full_count = len(full_tdn['operators'])
		# Make a partial copy with only half the operators
		import copy
		partial_tdn = copy.deepcopy(full_tdn)
		partial_tdn['operators'] = full_tdn['operators'][:full_count // 2]
		# Clear and import partial
		for c in list(self.sandbox.children):
			try:
				if c.dock is not None:
					c.dock = None
			except Exception:
				pass
		for c in list(self.sandbox.children):
			c.destroy()
		imp_partial = self.tdn.ImportNetwork(
			target_path=self.sandbox.path, tdn=partial_tdn, clear_first=True)
		self.assertTrue(imp_partial.get('success'))
		partial_created = imp_partial.get('created_count', 0)
		# Detect mismatch - partial import should have fewer ops
		self.assertLess(partial_created, expected - 100,
			f'Partial import should have significantly fewer ops, got {partial_created}')
		# Rollback: import full
		imp_full = self.tdn.ImportNetwork(
			target_path=self.sandbox.path, tdn=full_tdn, clear_first=True)
		self.assertTrue(imp_full.get('success'))
		full_created = imp_full.get('created_count', 0)
		self.assertGreaterEqual(full_created, expected - 20,
			f'Full rollback should restore ~{expected} ops, got {full_created}')

	def test_F05_deep_nesting_20_levels(self):
		"""20 levels deep: export -> reimport -> verify structure in TDN JSON."""
		# Build 20-level nesting
		current = self.sandbox
		for i in range(20):
			child = current.create(baseCOMP, f'level_{i}')
			child.create(noiseTOP, 'leaf')
			current = child
		# Export
		result = self.tdn.ExportNetwork(
			root_path=self.sandbox.path, include_dat_content=True)
		self.assertTrue(result.get('success'))
		tdn = result['tdn']
		# Verify depth in TDN JSON structure
		def max_depth(ops, d=0):
			m = d
			for o in ops:
				m = max(m, max_depth(o.get('children', []), d + 1))
			return m
		depth = max_depth(tdn['operators'])
		self.assertGreaterEqual(depth, 19,
			f'Expected 19+ levels in TDN JSON, got {depth}')
		# Clear and reimport
		for c in list(self.sandbox.children):
			try:
				if c.dock is not None:
					c.dock = None
			except Exception:
				pass
		for c in list(self.sandbox.children):
			c.destroy()
		imp = self.tdn.ImportNetwork(
			target_path=self.sandbox.path, tdn=tdn, clear_first=True)
		self.assertTrue(imp.get('success'), f'Import failed: {imp}')
		# Verify import created all operators (40 total: 20 COMPs + 20 TOPs)
		created = imp.get('created_count', 0)
		self.assertGreaterEqual(created, 38,
			f'Expected 38+ operators for 20 nesting levels, got {created}')

	def test_F06_large_dat_content_roundtrip(self):
		"""10 DATs x 5000 lines each: export -> import -> verify content."""
		total_lines = 5000
		for i in range(10):
			d = self.sandbox.create(textDAT, f'bigdat_{i}')
			lines = [f'DAT {i} line {j}: ' + 'x' * 80
				for j in range(total_lines)]
			d.text = '\n'.join(lines)
		# Export
		result = self.tdn.ExportNetwork(
			root_path=self.sandbox.path, include_dat_content=True)
		self.assertTrue(result.get('success'))
		tdn = result['tdn']
		# Clear and reimport
		for c in list(self.sandbox.children):
			c.destroy()
		imp = self.tdn.ImportNetwork(
			target_path=self.sandbox.path, tdn=tdn, clear_first=True)
		self.assertTrue(imp.get('success'))
		# Verify content
		for i in range(10):
			d = self.sandbox.op(f'bigdat_{i}')
			self.assertIsNotNone(d, f'bigdat_{i} should exist')
			lines = d.text.split('\n')
			self.assertGreaterEqual(len(lines), total_lines - 1,
				f'bigdat_{i} should have ~{total_lines} lines, got {len(lines)}')

	def test_F07_mixed_failure_modes(self):
		"""500 ops: .tdn + .bak both corrupt -> .bak2 holds oldest version.

		Demonstrates the cascading backup strategy:
		- v1 = first real export (500 ops)
		- v2 = second export (500 ops, modified param)
		- .bak2 = v1, .bak = v2, .tdn = v3
		After corrupting .tdn and .bak, .bak2 (v1) should still be valid.
		"""
		expected = self._buildStressNetwork(self.sandbox, 500)
		# Use _safe_write_tdn directly for controlled backup behavior
		tdn_file = os.path.join(self._temp_dir, 'multi.tdn')
		# v1: export the 500-op network
		result_v1 = self.tdn.ExportNetwork(
			root_path=self.sandbox.path, include_dat_content=True)
		self.assertTrue(result_v1.get('success'))
		v1_json = json.dumps(result_v1['tdn'])
		wr1 = self.tdn._safe_write_tdn(tdn_file, v1_json, self._proj_folder)
		self.assertTrue(wr1.get('success'), f'v1 write failed: {wr1}')
		# v2: modify and re-export
		top0 = self.sandbox.op('top_0')
		if top0:
			top0.par.resolutionw = 512
		result_v2 = self.tdn.ExportNetwork(
			root_path=self.sandbox.path, include_dat_content=True)
		self.assertTrue(result_v2.get('success'))
		v2_json = json.dumps(result_v2['tdn'])
		wr2 = self.tdn._safe_write_tdn(tdn_file, v2_json, self._proj_folder)
		self.assertTrue(wr2.get('success'), f'v2 write failed: {wr2}')
		# v3: modify again and re-export
		if top0:
			top0.par.resolutionw = 1024
		result_v3 = self.tdn.ExportNetwork(
			root_path=self.sandbox.path, include_dat_content=True)
		self.assertTrue(result_v3.get('success'))
		v3_json = json.dumps(result_v3['tdn'])
		wr3 = self.tdn._safe_write_tdn(tdn_file, v3_json, self._proj_folder)
		self.assertTrue(wr3.get('success'), f'v3 write failed: {wr3}')
		# Now: .tdn = v3, .bak = v2, .bak2 = v1
		# Corrupt .tdn and .bak
		Path(tdn_file).write_text('CORRUPT', encoding='utf-8')
		bak = self.tdn._get_backup_path(
			tdn_file, self._proj_folder, '.bak')
		self.assertTrue(bak.is_file(), '.bak should exist')
		bak.write_text('ALSO CORRUPT', encoding='utf-8')
		# .bak2 should still be valid (v1 content)
		bak2 = self.tdn._get_backup_path(
			tdn_file, self._proj_folder, '.bak2')
		self.assertTrue(bak2.is_file(), '.bak2 should exist')
		v = self.tdn._validate_tdn_file(str(bak2))
		self.assertTrue(v.get('valid'),
			f'.bak2 should be valid: {v}')
		# Recover from .bak2
		recovered_tdn = self.tdn.tdn_load(
			bak2.read_text(encoding='utf-8'))
		imp = self.tdn.ImportNetwork(
			target_path=self.sandbox.path,
			tdn=recovered_tdn, clear_first=True)
		self.assertTrue(imp.get('success'),
			f'Recovery from .bak2 failed: {imp}')
		created = imp.get('created_count', 0)
		self.assertGreater(created, 100,
			f'Should recover substantial ops from .bak2, got {created}')
