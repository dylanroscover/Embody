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

import sys
import types

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase


class TestVerifyMcpImportableFastPath(EmbodyTestCase):

	def setUp(self):
		super().setUp()
		self.ext = self.embody.ext.Embody
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
		self.assertTrue(self.ext._verifyMcpImportable('/dummy'))

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

		self.assertTrue(self.ext._verifyMcpImportable('/dummy'))

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
		self.ext._verifyMcpImportable('/dummy')

		surviving = sys.modules.get('mcp')
		self.assertIsNot(surviving, half_loaded,
			'half-loaded mcp parent must have been cleared and re-imported '
			'(or removed when import failed)')
