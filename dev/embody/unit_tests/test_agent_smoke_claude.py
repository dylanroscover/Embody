"""
Tier-2 agent smoke test: Claude Code headless (claude -p) driving Envoy.

Verifies that a REAL Claude Code agent can discover and correctly use the
Envoy MCP tools end-to-end: schema quality, tool choice, the full bridge
transport, and subscription auth. Each test is a scripted micro-task; the
verdict comes from PRIMARY EVIDENCE (live TD state + structured CLI output),
never from the agent's prose alone.

AGENT tier: runs ONLY via op.unit_tests.RunAgentTests(). Requirements on this
machine: the claude CLI installed AND logged in with a Pro/Max subscription
(missing CLI -> loud SKIP). Runs bill SUBSCRIPTION usage, never an API key:
the child env strips ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN, so stored
OAuth credentials are the only auth available.

Isolation: the agent runs from a NEUTRAL temp cwd with --strict-mcp-config
and a generated MCP config containing only envoy - no CLAUDE.md, no project
settings, no other MCP servers. Tool access is a per-task --allowedTools
allowlist (permission prompts do not exist in -p mode; anything not allowed
is denied). delete_op is deliberately NOT part of any smoke task - tier 1
covers it, and a peer session's recent writes could gate it mid-test.
"""

import json
import os
import time

runner_mod = op.unit_tests.op('TestRunnerExt').module
AgentTestCase = runner_mod.AgentTestCase

# Model alias for smoke runs. 'sonnet' = current Sonnet; use a full model id
# to pin an exact snapshot. Cheap + capable enough for scripted micro-tasks.
CLAUDE_SMOKE_MODEL = 'sonnet'

# Denied even if a future allowlist accidentally widens: state-mutating or
# expensive tools a smoke task must never touch.
CLAUDE_DISALLOWED = ','.join([
    'mcp__envoy__execute_python',
    'mcp__envoy__run_tests',
    'mcp__envoy__delete_op',
    'mcp__envoy__import_network',
    'mcp__envoy__restart_td',
    'mcp__envoy__launch_td',
    'mcp__envoy__switch_instance',
])


class TestAgentSmokeClaude(AgentTestCase):

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _writeMcpConfig(self, tmpdir):
        """Temp MCP config containing ONLY the envoy bridge entry."""
        entry = self.envoyBridgeEntry()
        path = os.path.join(tmpdir, 'mcp-envoy-only.json')
        with open(path, 'w', encoding='utf-8') as f:
            json.dump({'mcpServers': {'envoy': entry}}, f, indent=2)
        return path

    def _claudeJob(self, prompt, allowed_tools, verify, label,
                   max_turns=8, timeout_s=300):
        claude = self.requireCli('claude')
        cwd = self.neutralCwd()
        cfg = self._writeMcpConfig(cwd)
        argv = [
            claude, '-p', prompt,
            '--output-format', 'json',
            '--model', CLAUDE_SMOKE_MODEL,
            '--max-turns', str(max_turns),
            '--mcp-config', cfg,
            '--strict-mcp-config',
            '--allowedTools', allowed_tools,
            '--disallowedTools', CLAUDE_DISALLOWED,
        ]
        return self.job(argv=argv, timeout_s=timeout_s,
                        env=self.launchEnv('claude-smoke'),
                        cwd=cwd, verify=verify, label=label)

    def _parseCliJson(self, result):
        """Parse claude -p --output-format json stdout, with rich errors."""
        stdout = (result['stdout'] or '').strip()
        if result['returncode'] != 0:
            raise AssertionError(
                f"claude exited {result['returncode']} "
                f"(100=not logged in, 101=MCP unreachable, 102=tool denied); "
                f"stderr tail: {(result['stderr'] or '')[-300:]}")
        try:
            payload = json.loads(stdout)
        except Exception:
            raise AssertionError(
                f'claude stdout is not JSON: {stdout[-300:]!r}')
        if payload.get('is_error'):
            raise AssertionError(f'claude reported is_error: '
                                 f'{str(payload)[:300]}')
        return payload

    # ------------------------------------------------------------------
    # tests
    # ------------------------------------------------------------------

    def test_A01_read_only(self):
        """Claude calls one read-only Envoy tool and reports live TD state."""
        # get_td_info returns 'version' = f'{app.version}.{app.build}' (there
        # is no bare 'build' key) - ask for that field and check the live
        # build number appears in the echoed value.
        prompt = (
            'You are connected to the Envoy MCP server for a TouchDesigner '
            'project. Call the tool mcp__envoy__get_td_info exactly once, '
            'then reply with a single line: VERSION:<the value of the '
            '"version" field from the result>. Do nothing else: no other '
            'tools, no files.')
        return self._claudeJob(
            prompt,
            allowed_tools='mcp__envoy__get_td_info',
            verify=self._verifyReadOnly,
            label='claude -p read-only (get_td_info)',
            max_turns=6,
        )

    def _verifyReadOnly(self, result):
        payload = self._parseCliJson(result)
        text = payload.get('result') or ''
        self.assertIn('VERSION:', text,
                      f'no VERSION: line in reply: {text[-200:]!r}')
        self.assertIn(str(app.build), text,
                      f'reply lacks the live TD build {app.build} - the '
                      f'tool result never reached the agent: {text[-200:]!r}')

    def test_A02_create_and_write(self):
        """Claude creates a DAT, writes a token, reads it back - state is
        verified in live TD, not from the agent's claim."""
        self._token = f'claude-smoke-{int(time.time())}-{os.getpid()}'
        sandbox_path = self.sandbox.path
        prompt = (
            'You are connected to the Envoy MCP server for a TouchDesigner '
            'project. Perform exactly these steps using the envoy MCP tools, '
            'then stop. '
            f"1) create_op with parent_path='{sandbox_path}', "
            "op_type='textDAT', name='claude_smoke'. "
            f"2) set_dat_content with op_path='{sandbox_path}/claude_smoke' "
            f"and text='{self._token}'. "
            '3) get_dat_content on the same op_path and confirm the text '
            'matches what you wrote. '
            f'Reply with a single line: DONE {self._token} if every step '
            'succeeded, otherwise FAIL <reason>. Do not delete anything; do '
            'not use any other tools.')
        allowed = ','.join([
            'mcp__envoy__create_op',
            'mcp__envoy__set_dat_content',
            'mcp__envoy__get_dat_content',
            'mcp__envoy__get_op',
        ])
        return self._claudeJob(
            prompt,
            allowed_tools=allowed,
            verify=self._verifyCreateAndWrite,
            label='claude -p create/write/read-back',
            max_turns=12,
        )

    def _verifyCreateAndWrite(self, result):
        payload = self._parseCliJson(result)
        text = payload.get('result') or ''
        self.assertIn('DONE', text, f'agent did not report DONE: '
                                    f'{text[-300:]!r}')
        self.assertIn(self._token, text,
                      'agent reply is missing the run token')
        # PRIMARY EVIDENCE: the operator actually exists with the token.
        probe = self.sandbox.op('claude_smoke')
        self.assertIsNotNone(probe, 'claude_smoke DAT not found in sandbox '
                                    '- the create never landed in TD')
        self.assertEqual(probe.text.strip(), self._token,
                         f'DAT content mismatch: {probe.text[:120]!r}')
