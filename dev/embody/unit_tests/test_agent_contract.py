"""
Tier-1 agent test: deterministic MCP contract check (no LLM).

Spawns the SAME Envoy STDIO bridge command Claude Code uses (read from the
repo's .mcp.json) via the out-of-process client in agent_clients/, then
verifies the MCP handshake, the advertised tool inventory against the
EXPECTED_* manifests below, and a curated tool-call sequence (create / write /
read-back / batch / delete) against this suite's sandbox.

AGENT tier: runs ONLY via op.unit_tests.RunAgentTests(). No LLM and no
subscription usage - this tier is pure transport + tool contract.

When adding or removing an Envoy tool, update EXPECTED_ENVOY_TOOLS here
DELIBERATELY - the inventory check fails on drift in either direction. The
check runs against the LIVE server; tools register on the FastMCP instance
only when Envoy (re)starts, so a mismatch right after editing EnvoyExt.py
usually means "restart Envoy", not a code bug.
"""

import json
import os
import tempfile

runner_mod = op.unit_tests.op('TestRunnerExt').module
AgentTestCase = runner_mod.AgentTestCase

# Source of truth: the @self.mcp.tool() registrations in EnvoyExt.py
# (_register_tools). Update deliberately when the tool surface changes.
EXPECTED_ENVOY_TOOLS = [
    'batch_operations',
    'capture_top',
    'claim_scope',
    'connect_ops',
    'cook_op',
    'copy_op',
    'create_annotation',
    'create_extension',
    'create_op',
    'delete_op',
    'diff_tdn',
    'disconnect_op',
    'edit_dat_content',
    'exec_op_method',
    'execute_python',
    'export_network',
    'externalize_op',
    'find_children',
    'get_annotations',
    'get_connections',
    'get_dat_content',
    'get_docs',
    'get_enclosed_ops',
    'get_externalization_status',
    'get_externalizations',
    'get_logs',
    'get_module_help',
    'get_network_layout',
    'get_op',
    'get_op_errors',
    'get_op_flags',
    'get_op_performance',
    'get_op_position',
    'get_parameter',
    'get_project_performance',
    'get_sessions',
    'get_td_class_details',
    'get_td_classes',
    'get_td_info',
    'import_network',
    'layout_children',
    'query_network',
    'read_tdn',
    'release_scope',
    'remove_externalization_tag',
    'rename_op',
    'run_tests',
    'save_externalization',
    'set_annotation',
    'set_dat_content',
    'set_op_flags',
    'set_op_position',
    'set_parameter',
]

# Served bridge-side; present in tools/list even when TD is down.
EXPECTED_BRIDGE_TOOLS = [
    'get_td_status',
    'launch_td',
    'restart_td',
    'switch_instance',
]


class TestAgentMCPContract(AgentTestCase):

    def test_A01_contract(self):
        entry = self.envoyBridgeEntry()
        client_py = os.path.join(project.folder, 'embody', 'unit_tests',
                                 'agent_clients', 'mcp_contract_client.py')
        self.assertTrue(os.path.isfile(client_py),
                        f'contract client missing: {client_py}')

        tmp = tempfile.mkdtemp(prefix='embody_tier1_')
        self._agent_tmpdirs = getattr(self, '_agent_tmpdirs', [])
        self._agent_tmpdirs.append(tmp)
        self._report_path = os.path.join(tmp, 'report.json')
        expected_path = os.path.join(tmp, 'expected.json')
        with open(expected_path, 'w', encoding='utf-8') as f:
            json.dump(sorted(set(EXPECTED_ENVOY_TOOLS +
                                 EXPECTED_BRIDGE_TOOLS)), f)

        argv = [
            entry['command'],           # the project venv python
            client_py,
            '--mcp-json', self.findMcpJson(),
            '--server', 'envoy',
            '--sandbox', self.sandbox.path,
            '--expected-tools', expected_path,
            '--report', self._report_path,
        ]
        return self.job(
            argv=argv,
            timeout_s=180,
            env=self.launchEnv('agent-test-tier1'),
            verify=self._verifyContract,
            label='tier-1 MCP contract client',
        )

    def _verifyContract(self, result):
        if not os.path.isfile(self._report_path):
            raise AssertionError(
                f"contract client wrote no report "
                f"(rc {result['returncode']}); stderr tail: "
                f"{(result['stderr'] or '')[-300:]}")
        with open(self._report_path, encoding='utf-8') as f:
            report = json.load(f)

        self.assertTrue(report.get('handshake'), 'MCP handshake failed')
        self.assertEqual(report.get('missing_tools'), [],
                         f"tools missing from live server (stale Envoy? "
                         f"restart it): {report.get('missing_tools')}")
        self.assertEqual(report.get('unexpected_tools'), [],
                         f"unlisted tools advertised (update "
                         f"EXPECTED_ENVOY_TOOLS deliberately): "
                         f"{report.get('unexpected_tools')}")
        self.assertTrue(report.get('roundtrip_ok'),
                        'DAT content roundtrip failed')
        if not report.get('ok'):
            raise AssertionError(
                'contract failures: '
                + '; '.join(report.get('failures', [])[:5]))

        # Primary evidence in live TD: delete_op removed the probe - or, if a
        # peer's recent write gated it, the probe must still exist. Either
        # way the sandbox teardown cleans up afterwards.
        calls = {c['tool']: c for c in report.get('calls', [])}
        delete_call = calls.get('delete_op') or {}
        probe = self.sandbox.op('tier1_probe')
        if delete_call.get('gated'):
            self.assertIsNotNone(
                probe, 'delete_op was gated yet the probe is gone')
        else:
            self.assertIsNone(
                probe, 'delete_op reported ok but the probe survives')
        self.assertEqual(result['returncode'], 0,
                         f"client exit {result['returncode']}")
