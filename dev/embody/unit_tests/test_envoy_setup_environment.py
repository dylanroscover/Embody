"""
Test suite: EmbodyExt._verifyMcpImportable.

Fast path must not tear down and re-import mcp.* on top of an
already-loaded pydantic_core (Rust C extension). Re-running pydantic
model definitions against a live validator can panic the Rust side and
abort() the process -- the "TD just closes on Envoy toggle off/on" crash
introduced by the original sys.modules-clearing path in v5.0.393.

- If mcp.server is already in sys.modules: return True immediately, leave
  sys.modules untouched
- If mcp.server is NOT in sys.modules: still try to import and return the
  outcome (the original behaviour for genuine first imports and recovery
  from a prior failed import)
"""

import os
import shutil
import sys
import tempfile
import types

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase


class TestVerifyMcpImportableFastPath(EmbodyTestCase):

	def setUp(self):
		super().setUp()
		# Snapshot every mcp.* entry so the suite leaves sys.modules clean.
		self._saved = {
			k: v for k, v in sys.modules.items()
			if k == 'mcp' or k.startswith('mcp.')
		}

	def tearDown(self):
		# Restore exactly what was there before.
		for k in [k for k in sys.modules if k == 'mcp' or k.startswith('mcp.')]:
			del sys.modules[k]
		sys.modules.update(self._saved)
		super().tearDown()

	def _clearMcp(self):
		for k in [k for k in sys.modules if k == 'mcp' or k.startswith('mcp.')]:
			del sys.modules[k]

	# =================================================================
	# Fast path: already imported
	# =================================================================

	def test_A01_already_imported_returns_true(self):
		"""If mcp.server is in sys.modules, verify returns True."""
		self._clearMcp()
		sys.modules['mcp'] = types.ModuleType('mcp')
		sys.modules['mcp.server'] = types.ModuleType('mcp.server')
		self.assertTrue(self.embody.ext.Embody._verifyMcpImportable('/dummy'))

	def test_A02_already_imported_leaves_sys_modules_untouched(self):
		"""Fast path must NOT del/reimport on top of a live mcp.server.

		Tearing down and re-importing mcp.* on top of an already-loaded
		pydantic_core (Rust) is what aborts the process. The fast path
		exists to skip that teardown -- if any of the sentinel objects
		below are replaced, the fix has regressed.
		"""
		self._clearMcp()
		mcp_sentinel = types.ModuleType('mcp')
		mcp_sentinel._sentinel = object()
		server_sentinel = types.ModuleType('mcp.server')
		server_sentinel._sentinel = object()
		types_sentinel = types.ModuleType('mcp.types')
		types_sentinel._sentinel = object()
		sys.modules['mcp'] = mcp_sentinel
		sys.modules['mcp.server'] = server_sentinel
		sys.modules['mcp.types'] = types_sentinel

		self.assertTrue(self.embody.ext.Embody._verifyMcpImportable('/dummy'))

		self.assertIs(sys.modules.get('mcp'), mcp_sentinel,
			'mcp module object must not be replaced on the fast path')
		self.assertIs(sys.modules.get('mcp.server'), server_sentinel,
			'mcp.server module object must not be replaced on the fast path')
		self.assertIs(sys.modules.get('mcp.types'), types_sentinel,
			'unrelated mcp.* submodules must not be torn down either')

	def test_A03_only_mcp_in_modules_not_mcp_server_does_not_short_circuit(self):
		"""'mcp' alone (without 'mcp.server') means the previous import
		failed midway -- the fast path must NOT trigger, so the recovery
		path runs and clears the half-loaded entry."""
		self._clearMcp()
		half_loaded = types.ModuleType('mcp')
		half_loaded._sentinel = object()
		sys.modules['mcp'] = half_loaded

		# Either this succeeds (the venv really has mcp) or fails -- both
		# are fine; what matters is that the fast path did NOT short-circuit
		# on the half-loaded 'mcp' entry, so 'mcp' is either gone or
		# replaced by a fresh module after the call.
		self.embody.ext.Embody._verifyMcpImportable('/dummy')

		surviving = sys.modules.get('mcp')
		self.assertIsNot(surviving, half_loaded,
			'half-loaded mcp parent must have been cleared and re-imported '
			'(or removed when import failed)')


class TestVenvPaths(EmbodyTestCase):
	"""_venvPaths() produces the plain-data spec handed to the worker thread.

	It reads project.folder (a TD global) and so must run on the main thread;
	everything downstream (_environmentNeedsInstall, _installDependencies)
	consumes only the dict it returns."""

	def setUp(self):
		super().setUp()

	def test_returns_all_expected_keys(self):
		spec = self.embody.ext.Embody._venvPaths()
		for key in ('project_dir', 'venv_dir', 'site_packages', 'venv_python',
					'python_exe', 'deps', 'mcp_min_version'):
			self.assertIn(key, spec)

	def test_deps_pin_mcp_and_attrs(self):
		spec = self.embody.ext.Embody._venvPaths()
		self.assertTrue(any(d.startswith('mcp>=') for d in spec['deps']),
			'deps must pin a minimum mcp version')
		self.assertIn('attrs<25', spec['deps'])

	def test_min_version_matches_class_constant(self):
		spec = self.embody.ext.Embody._venvPaths()
		self.assertEqual(spec['mcp_min_version'], self.embody.ext.Embody.MCP_MIN_VERSION)

	def test_site_packages_lives_under_venv(self):
		spec = self.embody.ext.Embody._venvPaths()
		self.assertIn('.venv', spec['venv_dir'])
		self.assertTrue(spec['site_packages'].startswith(spec['venv_dir']))


class TestEnvironmentNeedsInstall(EmbodyTestCase):
	"""_environmentNeedsInstall() is the cheap, filesystem-only predicate that
	decides sync (fast) vs async (background install) startup. It must never
	run a subprocess, hit the network, or import -- it reads the installed
	version straight from the mcp-X.Y.Z.dist-info directory name."""

	def setUp(self):
		super().setUp()
		self.tmp = tempfile.mkdtemp(prefix='embody_envtest_')

	def tearDown(self):
		shutil.rmtree(self.tmp, ignore_errors=True)
		super().tearDown()

	def _spec(self, min_ver='1.26.0'):
		return {'site_packages': self.tmp, 'mcp_min_version': min_ver}

	def _make_dist(self, name, version):
		# Mirror a real site-packages layout: the package dir + its dist-info.
		os.makedirs(os.path.join(self.tmp, name), exist_ok=True)
		os.makedirs(os.path.join(self.tmp, f'{name}-{version}.dist-info'),
					exist_ok=True)

	def test_missing_mcp_needs_install(self):
		self.assertTrue(self.embody.ext.Embody._environmentNeedsInstall(self._spec()))

	def test_current_version_no_install(self):
		self._make_dist('mcp', '1.26.0')
		self.assertFalse(self.embody.ext.Embody._environmentNeedsInstall(self._spec()))

	def test_old_version_needs_install(self):
		self._make_dist('mcp', '1.0.0')
		self.assertTrue(self.embody.ext.Embody._environmentNeedsInstall(self._spec()))

	def test_newer_version_no_install(self):
		self._make_dist('mcp', '2.5.0')
		self.assertFalse(self.embody.ext.Embody._environmentNeedsInstall(self._spec()))

	def test_attrs_25_forces_install(self):
		self._make_dist('mcp', '1.26.0')
		self._make_dist('attrs', '25.1.0')
		self.assertTrue(self.embody.ext.Embody._environmentNeedsInstall(self._spec()),
			'attrs 25.x conflicts with TD and must trigger a downgrade install')

	def test_attrs_24_is_fine(self):
		self._make_dist('mcp', '1.26.0')
		self._make_dist('attrs', '24.2.0')
		self.assertFalse(self.embody.ext.Embody._environmentNeedsInstall(self._spec()))

	def test_mcp_present_without_metadata_accepts(self):
		# Package dir but no dist-info: the original fast path accepted this and
		# proceeded to the import check, so no install is required.
		os.makedirs(os.path.join(self.tmp, 'mcp'), exist_ok=True)
		self.assertFalse(self.embody.ext.Embody._environmentNeedsInstall(self._spec()))


class TestInstallDependenciesWorkerSafe(EmbodyTestCase):
	"""_installDependencies() runs on a background thread, so it must touch NO
	TouchDesigner objects -- all output goes through the log callback, never
	self.Log (which writes the FIFO DAT and reads parameters). These tests stub
	uv discovery + subprocess so no real install runs, and assert self.Log is
	never invoked."""

	def setUp(self):
		super().setUp()
		self.mod = self.embody.op('EmbodyExt').module
		self.tmp = tempfile.mkdtemp(prefix='embody_instest_')
		self._real_sub = self.mod.subprocess
		self._real_find = self.embody.ext.Embody._findOrInstallUv  # ext-cache-ok: save original to restore after monkeypatch
		# Trip-wire: if _installDependencies ever calls self.Log, record it.
		self._log_calls = []
		self.embody.ext.Embody.Log = lambda *a, **k: self._log_calls.append((a, k))

	def tearDown(self):
		self.mod.subprocess = self._real_sub
		self.embody.ext.Embody._findOrInstallUv = self._real_find
		try:
			del self.embody.ext.Embody.Log
		except Exception:
			pass
		shutil.rmtree(self.tmp, ignore_errors=True)
		super().tearDown()

	def _spec(self):
		return {
			'venv_dir': os.path.join(self.tmp, '.venv'),
			'venv_python': os.path.join(self.tmp, '.venv', 'bin', 'python'),
			'python_exe': '/usr/bin/python3',
			'deps': ['mcp>=1.26.0', 'attrs<25'],
			'site_packages': os.path.join(self.tmp, 'sp'),
			'mcp_min_version': '1.26.0',
		}

	def _stub_subprocess(self, run_fn):
		real = self._real_sub

		class _FakeSub:
			DEVNULL = getattr(real, 'DEVNULL', -3)
			CalledProcessError = real.CalledProcessError
			run = staticmethod(run_fn)

		self.mod.subprocess = _FakeSub

	def test_success_logs_via_callback_not_Log(self):
		self.embody.ext.Embody._findOrInstallUv = lambda python_exe, log=None: '/fake/uv'
		runs = []
		self._stub_subprocess(lambda *a, **k: runs.append(a))
		msgs = []
		ok = self.embody.ext.Embody._installDependencies(
			self._spec(), log=lambda m, lvl='INFO': msgs.append((lvl, m)))
		self.assertTrue(ok)
		self.assertTrue(any(lvl == 'SUCCESS' for lvl, _ in msgs))
		self.assertTrue(len(runs) >= 1, 'should have invoked uv to install')
		self.assertEqual(self._log_calls, [],
			'must not call self.Log -- illegal from a worker thread')

	def test_failure_returns_false_and_reports_stderr(self):
		self.embody.ext.Embody._findOrInstallUv = lambda python_exe, log=None: '/fake/uv'

		def boom(*a, **k):
			raise self._real_sub.CalledProcessError(1, 'uv', stderr='kaboom')

		self._stub_subprocess(boom)
		msgs = []
		ok = self.embody.ext.Embody._installDependencies(
			self._spec(), log=lambda m, lvl='INFO': msgs.append((lvl, m)))
		self.assertFalse(ok)
		self.assertTrue(any(lvl == 'ERROR' and 'kaboom' in m for lvl, m in msgs))
		self.assertEqual(self._log_calls, [])

	def test_no_uv_returns_false(self):
		self.embody.ext.Embody._findOrInstallUv = lambda python_exe, log=None: None
		self._stub_subprocess(lambda *a, **k: None)
		msgs = []
		ok = self.embody.ext.Embody._installDependencies(
			self._spec(), log=lambda m, lvl='INFO': msgs.append((lvl, m)))
		self.assertFalse(ok)
		self.assertTrue(any('uv' in m.lower() for _, m in msgs))
		self.assertEqual(self._log_calls, [])
