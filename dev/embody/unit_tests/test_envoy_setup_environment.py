"""
Test suite: Envoy Python environment setup helpers.

Fast path must not tear down and re-import mcp.* on top of an
already-loaded pydantic_core (Rust C extension). Re-running pydantic
model definitions against a live validator can panic the Rust side and
abort() the process -- the "TD just closes on Envoy toggle off/on" crash
introduced by the original sys.modules-clearing path in v5.0.393.

- If mcp.server.mcpserver is already in sys.modules: return True
  immediately, leave sys.modules untouched
- If mcp.server.mcpserver is NOT in sys.modules: still try to import and
  return the outcome (the original behaviour for genuine first imports and
  recovery from a prior failed import)

The gate targets mcp.server.mcpserver -- the exact module EnvoyExt serves
with -- because SDK 2.0.0 kept ``mcp.server`` importable while REMOVING
``mcp.server.fastmcp``; a parent-package gate passed and the server then
died in a 30-minute retry storm (issue #81).

The venv carries an ``embody-env.json`` stamp recording the dep spec and
Python it was built for; _environmentNeedsInstall compares it against the
current spec so every Embody release that changes a pin auto-upgrades
every existing venv (nobody rebuilds a venv by hand).
"""

import os
import importlib
import json
import shutil
import sys
import tempfile
import threading
import types

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase

GATE_MODULE = 'mcp.server.mcpserver'


class TestVerifyMcpImportableFastPath(EmbodyTestCase):

	def setUp(self):
		super().setUp()
		self.ext = self.embody.ext.Embody
		# Snapshot every mcp.* entry so the suite leaves sys.modules clean.
		self._saved = {
			k: v for k, v in sys.modules.items()
			if k == 'mcp' or k.startswith('mcp.')
		}
		self._had_gate_flag = hasattr(sys, '_envoy_import_gate_ok')
		self._saved_gate_flag = getattr(sys, '_envoy_import_gate_ok', None)
		self._had_loaded_ver = hasattr(sys, '_envoy_mcp_loaded_version')
		self._saved_loaded_ver = getattr(sys, '_envoy_mcp_loaded_version', None)
		self.tmp = tempfile.mkdtemp(prefix='embody_gatetest_')

	def tearDown(self):
		# Restore exactly what was there before.
		for k in [k for k in sys.modules if k == 'mcp' or k.startswith('mcp.')]:
			del sys.modules[k]
		sys.modules.update(self._saved)
		if self._had_gate_flag:
			sys._envoy_import_gate_ok = self._saved_gate_flag
		elif hasattr(sys, '_envoy_import_gate_ok'):
			delattr(sys, '_envoy_import_gate_ok')
		if self._had_loaded_ver:
			sys._envoy_mcp_loaded_version = self._saved_loaded_ver
		elif hasattr(sys, '_envoy_mcp_loaded_version'):
			delattr(sys, '_envoy_mcp_loaded_version')
		shutil.rmtree(self.tmp, ignore_errors=True)
		super().tearDown()

	def _clearMcp(self):
		for k in [k for k in sys.modules if k == 'mcp' or k.startswith('mcp.')]:
			del sys.modules[k]

	def _sentinel(self, name):
		mod = types.ModuleType(name)
		mod._sentinel = object()
		sys.modules[name] = mod
		return mod

	def _make_dist_info(self, version):
		os.makedirs(os.path.join(self.tmp, f'mcp-{version}.dist-info'),
					exist_ok=True)

	# =================================================================
	# Fast path: already imported
	# =================================================================

	def test_A01_already_imported_returns_true(self):
		"""If mcp.server.mcpserver is in sys.modules, verify returns True."""
		self._clearMcp()
		self._sentinel('mcp')
		self._sentinel('mcp.server')
		self._sentinel(GATE_MODULE)
		self.assertTrue(self.ext._verifyMcpImportable('/dummy'))

	def test_A02_already_imported_leaves_sys_modules_untouched(self):
		"""Fast path must NOT del/reimport on top of a live MCP stack.

		Tearing down and re-importing mcp.* on top of an already-loaded
		pydantic_core (Rust) is what aborts the process. The fast path
		exists to skip that teardown -- if any of the sentinel objects
		below are replaced, the fix has regressed.
		"""
		self._clearMcp()
		mcp_sentinel = self._sentinel('mcp')
		server_sentinel = self._sentinel('mcp.server')
		mcpserver_sentinel = self._sentinel(GATE_MODULE)
		types_sentinel = self._sentinel('mcp.types')

		self.assertTrue(self.ext._verifyMcpImportable('/dummy'))

		self.assertIs(sys.modules.get('mcp'), mcp_sentinel,
			'mcp module object must not be replaced on the fast path')
		self.assertIs(sys.modules.get('mcp.server'), server_sentinel,
			'mcp.server module object must not be replaced on the fast path')
		self.assertIs(sys.modules.get(GATE_MODULE), mcpserver_sentinel,
			'gate module object must not be replaced on the fast path')
		self.assertIs(sys.modules.get('mcp.types'), types_sentinel,
			'unrelated mcp.* submodules must not be torn down either')

	def test_A03_parent_only_in_modules_does_not_short_circuit(self):
		"""'mcp.server' alone (without the mcpserver gate module) means a
		partial or legacy import -- the fast path must NOT trigger, so the
		recovery path runs and clears the half-loaded entries. This is
		exactly the SDK 2.0.0 shape that broke the old parent-package gate
		(issue #81)."""
		self._clearMcp()
		half_loaded = self._sentinel('mcp')
		self._sentinel('mcp.server')

		# What matters is that the fast path did NOT short-circuit on the
		# parent packages, so they are cleared before the recovery import.
		# Patch import_module so this test never imports the real MCP stack
		# and then clears it again before later tests.
		real_import_module = importlib.import_module

		def fail_gate_module(name, *args, **kwargs):
			if name == GATE_MODULE:
				raise ImportError('sentinel import failure')
			return real_import_module(name, *args, **kwargs)

		importlib.import_module = fail_gate_module
		try:
			self.ext._verifyMcpImportable('/dummy')
		finally:
			importlib.import_module = real_import_module

		surviving = sys.modules.get('mcp')
		self.assertIsNot(surviving, half_loaded,
			'half-loaded mcp parent must have been cleared and re-imported '
			'(or removed when import failed)')

	# =================================================================
	# Stale-interpreter refusal: upgraded on disk, old stack loaded
	# =================================================================

	def test_A04_legacy_stack_with_v2_on_disk_refuses_with_restart(self):
		"""A loaded 1.x stack (identified by its fastmcp module) with a 2.x
		dist on disk must refuse with a restart-TD message -- importing 2.x
		submodules through cached 1.x parents yields a mixed stack. The
		refusal must ALSO kill the process-wide fast-path flag, or the very
		next Start() short-circuits into _continueStart and performs the
		mixed import the refusal just prevented (panel BLOCKER)."""
		self._clearMcp()
		mcp_sentinel = self._sentinel('mcp')
		self._sentinel('mcp.server')
		self._sentinel('mcp.server.fastmcp')
		self._make_dist_info('2.0.0')
		sys._envoy_import_gate_ok = True  # as a prior 1.x session left it

		ok, msg = self.ext._importGateCheck(self.tmp)
		self.assertFalse(ok)
		self.assertIn('restart TouchDesigner', msg)
		self.assertIs(sys.modules.get('mcp'), mcp_sentinel,
			'refusal must not tear down the loaded stack')
		self.assertFalse(getattr(sys, '_envoy_import_gate_ok', False),
			'refusal must clear the fast-path flag so every subsequent '
			'Start re-runs the gate instead of bypassing it')

	def test_A05_loaded_version_marker_mismatch_refuses(self):
		"""A recorded loaded-version differing from the on-disk dist means
		the venv was upgraded under a live interpreter -- refuse, and clear
		the fast-path flag (same bypass hazard as A04)."""
		self._clearMcp()
		self._sentinel('mcp')
		sys._envoy_mcp_loaded_version = '2.0.0'
		sys._envoy_import_gate_ok = True
		self._make_dist_info('2.1.0')

		ok, msg = self.ext._importGateCheck(self.tmp)
		self.assertFalse(ok)
		self.assertIn('restart TouchDesigner', msg)
		self.assertFalse(getattr(sys, '_envoy_import_gate_ok', False))

	def test_A08_legacy_stack_without_disk_metadata_still_refuses(self):
		"""Mid-install (no dist-info written yet) a live 1.x stack must still
		refuse: purging a LIVE stack to reimport is the abort() vector no
		matter what the disk says."""
		self._clearMcp()
		mcp_sentinel = self._sentinel('mcp')
		self._sentinel('mcp.server')
		self._sentinel('mcp.server.fastmcp')
		# self.tmp deliberately has NO mcp-*.dist-info

		ok, msg = self.ext._importGateCheck(self.tmp)
		self.assertFalse(ok)
		self.assertIn('restart TouchDesigner', msg)
		self.assertIs(sys.modules.get('mcp'), mcp_sentinel)

	def test_A09_dist_version_reads_highest(self):
		"""_mcpDistVersion must version-sort, not take filesystem order: an
		interrupted uninstall can leave an old dist-info beside the current
		one (panel finding)."""
		self._make_dist_info('1.28.1')
		self._make_dist_info('2.0.0')
		self.assertEqual(self.ext._mcpDistVersion(self.tmp), '2.0.0')

	def test_A06_successful_import_records_loaded_version(self):
		"""A clean gate pass records the dist version it imported so a later
		on-disk upgrade is detected as restart-required."""
		self._clearMcp()
		if hasattr(sys, '_envoy_mcp_loaded_version'):
			delattr(sys, '_envoy_mcp_loaded_version')
		self._make_dist_info('2.0.0')

		real_import_module = importlib.import_module
		suite = self

		def fake_gate_import(name, *args, **kwargs):
			if name == GATE_MODULE:
				suite._sentinel('mcp')
				suite._sentinel('mcp.server')
				return suite._sentinel(GATE_MODULE)
			return real_import_module(name, *args, **kwargs)

		importlib.import_module = fake_gate_import
		try:
			ok, msg = self.ext._importGateCheck(self.tmp)
		finally:
			importlib.import_module = real_import_module

		self.assertTrue(ok, msg)
		self.assertEqual(
			getattr(sys, '_envoy_mcp_loaded_version', None), '2.0.0')

	def test_A07_restart_message_passes_through_failure_formatter(self):
		"""_importGateFailureMessage must NOT wrap the restart notice in the
		delete-.venv advice -- the restart IS the complete instruction."""
		msg = ('Envoy dependencies were upgraded on disk (installed mcp 2.1.0, '
			   'loaded 2.0.0) but the old stack is already imported in this '
			   'session. Save your work and restart TouchDesigner to finish '
			   'the upgrade.')
		out = self.ext._importGateFailureMessage('/sp', msg)
		self.assertEqual(out, msg)
		other = self.ext._importGateFailureMessage('/sp', 'boom')
		self.assertIn('.venv', other)

	def test_B01_import_gate_check_succeeds_and_is_thread_safe(self):
		"""The split gate works in the dev env and from a plain Thread."""
		if 'mcp.server.fastmcp' in sys.modules:
			self.skipTest(
				'a 1.x MCP stack is loaded in this session (upgrade landed '
				'live) -- restart TouchDesigner to finish the SDK 2.0 '
				'upgrade, then re-run')
		if hasattr(sys, '_envoy_import_gate_ok'):
			delattr(sys, '_envoy_import_gate_ok')
		spec = self.ext._venvPaths()
		self.assertTrue(self.ext._wirePythonPaths(spec))
		ok, msg = self.ext._importGateCheck(spec['site_packages'])
		self.assertTrue(ok, msg)
		self.assertEqual(msg, '')
		self.assertTrue(getattr(sys, '_envoy_import_gate_ok', False))

		gate = self.ext._importGateCheck
		result = []

		def worker():
			result.append(gate(spec['site_packages']))

		t = threading.Thread(target=worker)
		t.start()
		t.join(30)
		self.assertFalse(t.is_alive(), 'import gate thread timed out')
		self.assertEqual(len(result), 1)
		ok, msg = result[0]
		self.assertTrue(ok, msg)
		# Start() short-circuits on this sys flag; driving Start() needs a full
		# Envoy server lifecycle, so the flag contract is asserted here instead.
		self.assertTrue(getattr(sys, '_envoy_import_gate_ok', False))


class TestVenvPaths(EmbodyTestCase):
	"""_venvPaths() produces the plain-data spec handed to the worker thread.

	It reads project.folder (a TD global) and so must run on the main thread;
	everything downstream (_environmentNeedsInstall, _installDependencies)
	consumes only the dict it returns."""

	def setUp(self):
		super().setUp()
		self.ext = self.embody.ext.Embody

	def test_returns_all_expected_keys(self):
		spec = self.ext._venvPaths()
		for key in ('project_dir', 'venv_dir', 'site_packages', 'venv_python',
					'python_exe', 'deps', 'mcp_min_version',
					'mcp_ceiling_major', 'python_tag', 'machine',
					'stamp_path'):
			self.assertIn(key, spec)

	def test_machine_is_this_processes_architecture(self):
		import platform as platform_mod
		spec = self.ext._venvPaths()
		self.assertEqual(spec['machine'], platform_mod.machine())

	def test_cryptography_pin_caps_only_x86_64_darwin(self):
		"""cryptography 49.0.0 dropped x86_64 macOS wheels (arm64-only from
		there on; 48.x was universal2). An x86_64 macOS interpreter resolving
		an unbounded floor gets a wheel it can never dlopen -- the 2026-08-04
		Convoy install failure. Everywhere else the pin stays a pure floor."""
		pin = self.ext._cryptographyPin
		self.assertEqual(pin('darwin', 'x86_64'), 'cryptography>=3.4,<49')
		self.assertEqual(pin('darwin', 'arm64'), 'cryptography>=3.4')
		self.assertEqual(pin('win32', 'AMD64'), 'cryptography>=3.4')
		self.assertEqual(pin('linux', 'x86_64'), 'cryptography>=3.4')

	def test_deps_pin_mcp_floor_and_ceiling(self):
		"""The mcp dep must carry BOTH a floor and a next-major ceiling: SDK
		2.0.0 removed mcp.server.fastmcp overnight and every unpinned fresh
		install broke (issue #81). A new major is adopted only by a port +
		MCP_MIN_VERSION bump, never by the resolver."""
		spec = self.ext._venvPaths()
		mcp_deps = [d for d in spec['deps'] if d.startswith('mcp>=')]
		self.assertEqual(len(mcp_deps), 1, 'exactly one mcp requirement')
		expected = (f"mcp>={self.ext.MCP_MIN_VERSION}"
					f",<{spec['mcp_ceiling_major']}")
		self.assertEqual(mcp_deps[0], expected)
		self.assertIn('attrs<25', spec['deps'])
		self.assertIn('pyyaml', spec['deps'])
		# The Convoy host app runs under THIS venv python and needs the crypto
		# floor (Ed25519 / X.509 / TLS 1.3) for LAN peer identity. It arrives
		# transitively today, but Convoy depends on it directly, so it is
		# pinned explicitly -- a floor, never an exact pin.
		crypto = [d for d in spec['deps'] if d.startswith('cryptography')]
		self.assertEqual(len(crypto), 1, 'exactly one cryptography requirement')
		self.assertTrue(crypto[0].startswith('cryptography>='),
						'cryptography must carry a floor, not an exact pin')

	def test_ceiling_is_next_major(self):
		spec = self.ext._venvPaths()
		min_major = int(self.ext.MCP_MIN_VERSION.split('.')[0])
		self.assertEqual(spec['mcp_ceiling_major'], min_major + 1)

	def test_min_version_matches_class_constant(self):
		spec = self.ext._venvPaths()
		self.assertEqual(spec['mcp_min_version'], self.ext.MCP_MIN_VERSION)

	def test_python_tag_matches_interpreter(self):
		spec = self.ext._venvPaths()
		self.assertEqual(
			spec['python_tag'],
			f'{sys.version_info.major}.{sys.version_info.minor}')

	def test_site_packages_and_stamp_live_under_venv(self):
		spec = self.ext._venvPaths()
		self.assertIn('.venv', spec['venv_dir'])
		self.assertTrue(spec['site_packages'].startswith(spec['venv_dir']))
		self.assertTrue(spec['stamp_path'].startswith(spec['venv_dir']))

	def test_wire_python_paths_is_idempotent(self):
		tmp = tempfile.mkdtemp(prefix='embody_pathtest_')
		before = list(sys.path)
		try:
			venv_dir = os.path.join(tmp, '.venv')
			site_packages = os.path.join(venv_dir, 'site-packages')
			os.makedirs(site_packages)
			spec = {'venv_dir': venv_dir, 'site_packages': site_packages}
			start_count = sys.path.count(site_packages)

			self.assertTrue(self.ext._wirePythonPaths(spec))
			self.assertTrue(self.ext._wirePythonPaths(spec))

			self.assertLessEqual(
				sys.path.count(site_packages) - start_count, 1,
				'_wirePythonPaths must not add duplicate site-packages entries')
		finally:
			sys.path[:] = before
			shutil.rmtree(tmp, ignore_errors=True)


class TestEnvironmentNeedsInstall(EmbodyTestCase):
	"""_environmentNeedsInstall() is the cheap, filesystem-only predicate that
	decides sync (fast) vs async (background install) startup. It must never
	run a subprocess, hit the network, or import -- it reads installed
	versions straight from *-X.Y.Z.dist-info directory names and the
	embody-env.json stamp."""

	def setUp(self):
		super().setUp()
		self.ext = self.embody.ext.Embody
		self.tmp = tempfile.mkdtemp(prefix='embody_envtest_')

	def tearDown(self):
		shutil.rmtree(self.tmp, ignore_errors=True)
		super().tearDown()

	def _spec(self, min_ver='2.0.0', ceiling=3, python_tag=None, deps=None):
		if python_tag is None:
			python_tag = f'{sys.version_info.major}.{sys.version_info.minor}'
		if deps is None:
			deps = [f'mcp>={min_ver},<{ceiling}', 'attrs<25', 'pyyaml']
		return {
			'site_packages': self.tmp,
			# nonexistent by default: no pyvenv.cfg -> the cfg probe stays
			# neutral; tests that exercise it create the dir + cfg themselves
			'venv_dir': os.path.join(self.tmp, '.venv'),
			'mcp_min_version': min_ver,
			'mcp_ceiling_major': ceiling,
			'python_tag': python_tag,
			'stamp_path': os.path.join(self.tmp, 'embody-env.json'),
			'deps': deps,
		}

	def _write_pyvenv_cfg(self, spec, version):
		os.makedirs(spec['venv_dir'], exist_ok=True)
		with open(os.path.join(spec['venv_dir'], 'pyvenv.cfg'), 'w',
				  encoding='utf-8') as f:
			f.write(f'home = /x\nversion_info = {version}\n')

	def _make_dist(self, name, version):
		# Mirror a real site-packages layout: the package dir + its dist-info.
		os.makedirs(os.path.join(self.tmp, name), exist_ok=True)
		os.makedirs(os.path.join(self.tmp, f'{name}-{version}.dist-info'),
					exist_ok=True)

	def _write_stamp(self, spec, **over):
		stamp = {'schema': 1, 'deps': sorted(spec['deps']),
				 'python': spec['python_tag']}
		stamp.update(over)
		with open(spec['stamp_path'], 'w', encoding='utf-8') as f:
			json.dump(stamp, f)

	def test_missing_mcp_needs_install(self):
		self.assertTrue(self.ext._environmentNeedsInstall(self._spec()))

	def test_current_version_with_stamp_no_install(self):
		spec = self._spec()
		self._make_dist('mcp', '2.0.0')
		self._make_dist('yaml', '6.0.1')
		self._write_stamp(spec)
		self.assertFalse(self.ext._environmentNeedsInstall(spec))

	def test_old_version_needs_install(self):
		spec = self._spec()
		self._make_dist('mcp', '1.28.1')
		self._make_dist('yaml', '6.0.1')
		self._write_stamp(spec)
		self.assertTrue(self.ext._environmentNeedsInstall(spec),
			'an mcp below MCP_MIN_VERSION must trigger the upgrade install')

	def test_newer_within_major_no_install(self):
		spec = self._spec()
		self._make_dist('mcp', '2.5.0')
		self._make_dist('yaml', '6.0.1')
		self._write_stamp(spec)
		self.assertFalse(self.ext._environmentNeedsInstall(spec))

	def test_above_ceiling_needs_install(self):
		"""REGRESSION (issue #81): an mcp at/above the next major must read
		as needs-install so uv walks it back inside the pin. The old
		floor-only check accepted 2.0.0 under 1.x code and the server died
		at import time."""
		spec = self._spec()
		self._make_dist('mcp', '3.0.0')
		self._make_dist('yaml', '6.0.1')
		self._write_stamp(spec)
		self.assertTrue(self.ext._environmentNeedsInstall(spec))

	def test_missing_stamp_needs_install(self):
		"""Any pre-stamp venv (the whole existing fleet) upgrades once."""
		self._make_dist('mcp', '2.0.0')
		self._make_dist('yaml', '6.0.1')
		self.assertTrue(self.ext._environmentNeedsInstall(self._spec()))

	def test_stamp_deps_mismatch_needs_install(self):
		"""Changing any pin in _venvPaths must carry every venv forward."""
		spec = self._spec()
		self._make_dist('mcp', '2.0.0')
		self._make_dist('yaml', '6.0.1')
		self._write_stamp(spec, deps=sorted(['mcp>=1.26.0', 'attrs<25',
											 'pyyaml']))
		self.assertTrue(self.ext._environmentNeedsInstall(spec))

	def test_stamp_python_mismatch_sets_recreate(self):
		"""A TD upgrade that bumps embedded Python must REBUILD the venv:
		binary wheels are ABI-tied to the old interpreter."""
		spec = self._spec()
		self._make_dist('mcp', '2.0.0')
		self._make_dist('yaml', '6.0.1')
		self._write_stamp(spec, python='3.9')
		self.assertTrue(self.ext._environmentNeedsInstall(spec))
		self.assertTrue(spec.get('recreate_venv'),
			'python mismatch must flag the venv for uv venv --clear')

	def test_pyvenv_cfg_bump_forces_recreate_even_with_deps_change(self):
		"""REGRESSION (panel): combined TD-python-bump + pin-change upgrade
		must REBUILD. A deps-first check returned needs-install without the
		recreate flag and installed old-ABI wheels in place; the pyvenv.cfg
		probe must win before any stamp logic -- including when the stamp is
		missing entirely (pre-stamp fleet venv crossing a python bump)."""
		spec = self._spec()
		self._write_pyvenv_cfg(spec, '3.9.13')
		self._make_dist('mcp', '2.0.0')
		self._make_dist('yaml', '6.0.1')
		# stamp says OLD deps -- deps mismatch alone must NOT decide first
		self._write_stamp(spec, deps=['mcp>=1.26.0'])
		self.assertTrue(self.ext._environmentNeedsInstall(spec))
		self.assertTrue(spec.get('recreate_venv'),
			'cfg-vs-interpreter mismatch must force the rebuild path')
		# ...and with NO stamp at all (pre-stamp venv):
		spec2 = self._spec()
		self._write_pyvenv_cfg(spec2, '3.9.13')
		os.remove(spec2['stamp_path'])
		self.assertTrue(self.ext._environmentNeedsInstall(spec2))
		self.assertTrue(spec2.get('recreate_venv'))

	def test_pyvenv_cfg_matching_python_stays_neutral(self):
		spec = self._spec()
		self._write_pyvenv_cfg(
			spec, f'{sys.version_info.major}.{sys.version_info.minor}.99')
		self._make_dist('mcp', '2.0.0')
		self._make_dist('yaml', '6.0.1')
		self._write_stamp(spec)
		self.assertFalse(self.ext._environmentNeedsInstall(spec))
		self.assertFalse(spec.get('recreate_venv', False))

	def test_torn_stamp_needs_install(self):
		spec = self._spec()
		self._make_dist('mcp', '2.0.0')
		self._make_dist('yaml', '6.0.1')
		with open(spec['stamp_path'], 'w', encoding='utf-8') as f:
			f.write('{not json')
		self.assertTrue(self.ext._environmentNeedsInstall(spec))

	def test_stamp_machine_mismatch_sets_recreate(self):
		"""Binary wheels are ARCHITECTURE-tied as well as ABI-tied: swapping
		the Intel TD build for the Apple Silicon one leaves wheels the
		interpreter can no longer dlopen while every version check reads
		clean (the 2026-08-04 cryptography dlopen failure)."""
		spec = self._spec()
		spec['machine'] = 'arm64'
		self._make_dist('mcp', '2.0.0')
		self._make_dist('yaml', '6.0.1')
		self._write_stamp(spec, machine='x86_64')
		self.assertTrue(self.ext._environmentNeedsInstall(spec))
		self.assertTrue(spec.get('recreate_venv'),
			'an arch flip must REBUILD -- an in-place install resolves '
			'wrong-arch-but-present wheels as already satisfied')

	def test_stamp_machine_match_stays_neutral(self):
		spec = self._spec()
		spec['machine'] = 'AMD64'
		self._make_dist('mcp', '2.0.0')
		self._make_dist('yaml', '6.0.1')
		self._write_stamp(spec, machine='AMD64')
		self.assertFalse(self.ext._environmentNeedsInstall(spec))
		self.assertFalse(spec.get('recreate_venv', False))

	def test_stamp_machine_mismatch_wins_over_deps_change(self):
		"""Mirror of the pyvenv.cfg regression: when arch AND deps both
		changed, the rebuild verdict must win over in-place install."""
		spec = self._spec()
		spec['machine'] = 'arm64'
		self._make_dist('mcp', '2.0.0')
		self._make_dist('yaml', '6.0.1')
		self._write_stamp(spec, machine='x86_64',
						  deps=sorted(['mcp>=1.26.0']))
		self.assertTrue(self.ext._environmentNeedsInstall(spec))
		self.assertTrue(spec.get('recreate_venv'),
			'arch mismatch must set the rebuild flag even when deps also '
			'changed')

	def test_pre_arch_stamp_is_neutral_here_and_migrates_on_darwin(self):
		"""An old stamp with no machine field: Windows keeps returning
		False (single architecture, nothing to flip); darwin forces one
		migration rebuild -- exercised via the static helper because this
		suite runs inside a Windows TD."""
		spec = self._spec()
		spec['machine'] = 'AMD64'
		self._make_dist('mcp', '2.0.0')
		self._make_dist('yaml', '6.0.1')
		self._write_stamp(spec)		# no machine field, the whole old fleet
		self.assertFalse(self.ext._environmentNeedsInstall(spec))
		self.assertTrue(
			self.ext._stampNeedsArchMigration('', 'darwin'))
		self.assertFalse(
			self.ext._stampNeedsArchMigration('', 'win32'))
		self.assertFalse(
			self.ext._stampNeedsArchMigration('arm64', 'darwin'))

	def test_attrs_25_forces_install(self):
		spec = self._spec()
		self._make_dist('mcp', '2.0.0')
		self._make_dist('yaml', '6.0.1')
		self._make_dist('attrs', '25.1.0')
		self._write_stamp(spec)
		self.assertTrue(self.ext._environmentNeedsInstall(spec),
			'attrs 25.x conflicts with TD and must trigger a downgrade install')

	def test_attrs_24_is_fine(self):
		spec = self._spec()
		self._make_dist('mcp', '2.0.0')
		self._make_dist('attrs', '24.2.0')
		self._make_dist('yaml', '6.0.1')
		self._write_stamp(spec)
		self.assertFalse(self.ext._environmentNeedsInstall(spec))

	def test_missing_yaml_needs_install(self):
		# PyYAML powers the .tdn git textconv driver, invoked via the venv
		# python -- an mcp-complete venv that still lacks yaml needs install.
		spec = self._spec()
		self._make_dist('mcp', '2.0.0')
		self._write_stamp(spec)
		self.assertTrue(self.ext._environmentNeedsInstall(spec))

	def test_mcp_present_without_metadata_accepts(self):
		# Package dir but no dist-info: the original fast path accepted this and
		# proceeded to the import check, so no install is required (a valid
		# stamp is still required -- metadata-less venvs predate stamps only
		# until their first stamped install).
		spec = self._spec()
		os.makedirs(os.path.join(self.tmp, 'mcp'), exist_ok=True)
		self._make_dist('yaml', '6.0.1')
		self._write_stamp(spec)
		self.assertFalse(self.ext._environmentNeedsInstall(spec))


class TestInstallDependenciesWorkerSafe(EmbodyTestCase):
	"""_installDependencies() runs on a background thread, so it must touch NO
	TouchDesigner objects -- all output goes through the log callback, never
	self.Log (which writes the FIFO DAT and reads parameters). These tests stub
	uv discovery + subprocess so no real install runs, and assert self.Log is
	never invoked."""

	def setUp(self):
		super().setUp()
		self.ext = self.embody.ext.Embody
		self.mod = self.embody.op('EmbodyExt').module
		self.tmp = tempfile.mkdtemp(prefix='embody_instest_')
		self._real_sub = self.mod.subprocess
		self._real_find = self.ext._findOrInstallUv
		# Trip-wire: if _installDependencies ever calls self.Log, record it.
		self._log_calls = []
		self.ext.Log = lambda *a, **k: self._log_calls.append((a, k))

	def tearDown(self):
		self.mod.subprocess = self._real_sub
		self.ext._findOrInstallUv = self._real_find
		try:
			del self.ext.Log
		except Exception:
			pass
		shutil.rmtree(self.tmp, ignore_errors=True)
		super().tearDown()

	def _spec(self, **over):
		spec = {
			'venv_dir': os.path.join(self.tmp, '.venv'),
			'venv_python': os.path.join(self.tmp, '.venv', 'bin', 'python'),
			'python_exe': '/usr/bin/python3',
			'deps': ['mcp>=2.0.0,<3', 'attrs<25'],
			'site_packages': os.path.join(self.tmp, 'sp'),
			'mcp_min_version': '2.0.0',
			'mcp_ceiling_major': 3,
			'python_tag': '3.11',
			'stamp_path': os.path.join(self.tmp, 'embody-env.json'),
		}
		spec.update(over)
		return spec

	def _stub_subprocess(self, run_fn):
		real = self._real_sub

		class _FakeSub:
			DEVNULL = getattr(real, 'DEVNULL', -3)
			CalledProcessError = real.CalledProcessError
			run = staticmethod(run_fn)

		self.mod.subprocess = _FakeSub

	def test_success_logs_via_callback_not_Log_and_writes_stamp(self):
		self.ext._findOrInstallUv = lambda python_exe, log=None: '/fake/uv'
		runs = []
		self._stub_subprocess(lambda *a, **k: runs.append(a))
		msgs = []
		spec = self._spec()
		ok = self.ext._installDependencies(
			spec, log=lambda m, lvl='INFO': msgs.append((lvl, m)))
		self.assertTrue(ok)
		self.assertTrue(any(lvl == 'SUCCESS' for lvl, _ in msgs))
		self.assertTrue(len(runs) >= 1, 'should have invoked uv to install')
		self.assertEqual(self._log_calls, [],
			'must not call self.Log -- illegal from a worker thread')
		# The install line must NAME the spec it resolves -- issue #81 was
		# diagnosed from a field log that could not show what pip saw.
		self.assertTrue(any('mcp>=2.0.0,<3' in m for _, m in msgs),
			'install log line must include the dependency spec')
		with open(spec['stamp_path'], 'r', encoding='utf-8') as f:
			stamp = json.load(f)
		self.assertEqual(stamp['deps'], sorted(spec['deps']))
		self.assertEqual(stamp['python'], spec['python_tag'])

	def test_stamp_records_machine_and_fresh_spawn_arch(self):
		"""The stamp must carry the TD-process machine (the rebuild
		comparator) and the venv interpreter's fresh-spawn arch (the
		diagnostic breadcrumb), measured through the stubbed subprocess."""
		self.ext._findOrInstallUv = lambda python_exe, log=None: '/fake/uv'
		spec = self._spec(machine='AMD64')

		class _Result:
			returncode = 0
			stdout = 'arm64\n'

		probes = []

		def run_fn(argv, **k):
			if argv and argv[0] == spec['venv_python']:
				probes.append(argv)
				return _Result()
			return None

		self._stub_subprocess(run_fn)
		ok = self.ext._installDependencies(spec, log=lambda m, lvl='INFO': None)
		self.assertTrue(ok)
		self.assertEqual(len(probes), 1, 'exactly one fresh-spawn arch probe')
		self.assertIn('-I', probes[0])
		with open(spec['stamp_path'], 'r', encoding='utf-8') as f:
			stamp = json.load(f)
		self.assertEqual(stamp['machine'], 'AMD64')
		self.assertEqual(stamp['arch'], 'arm64',
			'the measured fresh-spawn value, not the TD process value')
		self.assertEqual(self._log_calls, [])

	def test_recreate_flag_rebuilds_venv_with_clear(self):
		"""spec['recreate_venv'] (stale-ABI venv after a TD Python bump) must
		route the rebuild through uv venv --clear -- uv owns the removal."""
		self.ext._findOrInstallUv = lambda python_exe, log=None: '/fake/uv'
		calls = []
		self._stub_subprocess(lambda *a, **k: calls.append(a[0]))
		msgs = []
		# venv_dir EXISTS, so without the flag no uv-venv call would happen.
		spec = self._spec(recreate_venv=True)
		os.makedirs(spec['venv_dir'], exist_ok=True)
		ok = self.ext._installDependencies(
			spec, log=lambda m, lvl='INFO': msgs.append((lvl, m)))
		self.assertTrue(ok)
		venv_calls = [c for c in calls if len(c) > 1 and c[1] == 'venv']
		self.assertEqual(len(venv_calls), 1,
			'recreate must re-run uv venv even though the dir exists')
		self.assertIn('--clear', venv_calls[0])

	def test_existing_venv_without_flag_skips_venv_creation(self):
		self.ext._findOrInstallUv = lambda python_exe, log=None: '/fake/uv'
		calls = []
		self._stub_subprocess(lambda *a, **k: calls.append(a[0]))
		spec = self._spec()
		os.makedirs(spec['venv_dir'], exist_ok=True)
		ok = self.ext._installDependencies(spec, log=lambda m, lvl='INFO': None)
		self.assertTrue(ok)
		venv_calls = [c for c in calls if len(c) > 1 and c[1] == 'venv']
		self.assertEqual(venv_calls, [],
			'an existing venv must be upgraded in place, not recreated')

	def test_failure_returns_false_and_reports_stderr(self):
		self.ext._findOrInstallUv = lambda python_exe, log=None: '/fake/uv'

		def boom(*a, **k):
			raise self._real_sub.CalledProcessError(1, 'uv', stderr='kaboom')

		self._stub_subprocess(boom)
		msgs = []
		spec = self._spec()
		ok = self.ext._installDependencies(
			spec, log=lambda m, lvl='INFO': msgs.append((lvl, m)))
		self.assertFalse(ok)
		self.assertTrue(any(lvl == 'ERROR' and 'kaboom' in m for lvl, m in msgs))
		self.assertEqual(self._log_calls, [])
		self.assertFalse(os.path.exists(spec['stamp_path']),
			'a failed install must not stamp the venv as current')

	def test_no_uv_returns_false(self):
		self.ext._findOrInstallUv = lambda python_exe, log=None: None
		self._stub_subprocess(lambda *a, **k: None)
		msgs = []
		ok = self.ext._installDependencies(
			self._spec(), log=lambda m, lvl='INFO': msgs.append((lvl, m)))
		self.assertFalse(ok)
		self.assertTrue(any('uv' in m.lower() for _, m in msgs))
		self.assertEqual(self._log_calls, [])
