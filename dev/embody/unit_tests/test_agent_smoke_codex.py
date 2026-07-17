"""
Tier-2 agent smoke test: OpenAI Codex CLI (codex exec) driving Envoy.

Verifies that a REAL Codex agent can reach and correctly use the Envoy MCP
tools end-to-end. Embody deploys no Codex MCP wiring (Codex does not read
.mcp.json), so each invocation defines the envoy STDIO server INLINE via
repeatable ``-c mcp_servers.envoy.*`` config overrides - the user's
~/.codex/config.toml is never touched.

AGENT tier: runs ONLY via op.unit_tests.RunAgentTests(). Requirements: the
codex CLI installed AND logged in with a ChatGPT subscription (checked by
``codex login status`` first; missing/unauthenticated -> loud SKIP). Runs
bill SUBSCRIPTION usage: OPENAI_API_KEY / CODEX_API_KEY are stripped from the
child env.

Codex specifics handled here (see the research notes in
.claude/rules/agent-tests.md):
- stdin is DEVNULL (codex exec hangs probing a silent stdin pipe on Windows).
- Exit codes are NOT documented as task success - the verdict comes from the
  --output-last-message file plus live TD state.
- Approvals cannot be answered in exec mode: ``-c approval_policy="never"``
  (the exec subcommand rejects ``-a`` on installed builds) plus
  ``default_tools_approval_mode="auto"`` on the server entry keep MCP tool
  calls from being auto-cancelled.
- The sandbox does not block MCP: servers are spawned OUTSIDE it, so
  ``--sandbox read-only`` is safe for tasks that only use MCP tools.
- Windows npm installs expose a codex.cmd shim CreateProcess cannot exec
  directly; prefer the vendored codex.exe, else fall back to a cmd /c line.
"""

import glob
import json
import os
import subprocess
import time

runner_mod = op.unit_tests.op('TestRunnerExt').module
AgentTestCase = runner_mod.AgentTestCase

# None = use the CLI's configured default model (robust across CLI
# versions). Pin a slug (e.g. 'gpt-5.6-luna') only deliberately: the model
# list rotates with releases - see https://developers.openai.com/codex/models
# - and an older CLI hard-400s on a slug it does not know (verified live:
# codex 0.142.x rejects 'gpt-5.6-luna' with "requires a newer version").
CODEX_SMOKE_MODEL = None


class TestAgentSmokeCodex(AgentTestCase):

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _codexCommand(self):
        """Resolve codex to something Popen can exec: (argv_head, use_cmdline).

        Prefers a real .exe (native installer, or the npm package's vendored
        binary); a bare .cmd shim needs a cmd /c command line."""
        path = self.requireCli('codex')
        if not path.lower().endswith(('.cmd', '.bat')):
            return path, False
        # npm shim: look for the vendored native exe anywhere inside the
        # @openai/codex package (layout nests a platform sub-package, e.g.
        # node_modules/@openai/codex-win32-x64/vendor/.../bin/codex.exe).
        npm_root = os.path.dirname(path)
        pattern = os.path.join(npm_root, 'node_modules', '@openai', 'codex',
                               '**', 'codex.exe')
        matches = sorted(glob.glob(pattern, recursive=True))
        if matches:
            return matches[0], False
        return path, True

    def _codexJob(self, prompt, verify, label, timeout_s=360):
        head, use_cmdline = self._codexCommand()
        entry = self.envoyBridgeEntry()
        cwd = self.neutralCwd()
        self._last_message_path = os.path.join(cwd, 'last_message.txt')

        # JSON string/array literals double as TOML for simple values
        # (forward-slash paths, double quotes) - values of -c are TOML.
        cmd_toml = json.dumps(entry['command'])
        args_toml = json.dumps(entry.get('args', []))
        overrides = [
            '-c', f'mcp_servers.envoy.command={cmd_toml}',
            '-c', f'mcp_servers.envoy.args={args_toml}',
            '-c', 'mcp_servers.envoy.required=true',
            # "approve" = pre-approve every tool on this server. "auto" lets
            # codex elicit approval for side-effecting calls, which exec mode
            # cannot answer -> "cancelled by user" (openai/codex#24135,
            # observed live on codex 0.142.x with create_op).
            '-c', 'mcp_servers.envoy.default_tools_approval_mode="approve"',
            '-c', 'mcp_servers.envoy.startup_timeout_sec=30',
            '-c', 'mcp_servers.envoy.tool_timeout_sec=60',
            '-c', 'mcp_servers.envoy.env={EMBODY_SESSION_LABEL="codex-smoke"}',
        ]
        # approval_policy goes through -c (version-stable): installed codex
        # builds reject -a/--ask-for-approval on the exec subcommand.
        argv = [
            head, 'exec',
            '--skip-git-repo-check',
            '--sandbox', 'read-only',
            '--output-last-message', self._last_message_path,
            '-c', 'approval_policy="never"',
        ]
        if CODEX_SMOKE_MODEL:
            argv += ['-m', CODEX_SMOKE_MODEL]
        argv += overrides + [prompt]

        kwargs = {
            'timeout_s': timeout_s,
            'env': self.launchEnv('codex-smoke'),
            'cwd': cwd,
            'verify': verify,
            'label': label,
        }
        if use_cmdline:
            return self.job(cmdline=subprocess.list2cmdline(argv), **kwargs)
        return self.job(argv=argv, **kwargs)

    def _lastMessage(self, result):
        """The agent's final message (the reliable success channel)."""
        path = getattr(self, '_last_message_path', None)
        if path and os.path.isfile(path):
            try:
                with open(path, encoding='utf-8', errors='replace') as f:
                    text = f.read().strip()
                if text:
                    return text
            except Exception:
                pass
        # Fallback: default mode prints only the final message to stdout.
        return (result['stdout'] or '').strip()

    def _requireAuth(self):
        if not getattr(self, '_codex_authed', False):
            raise SkipTest('codex not logged in (codex login status failed '
                           'earlier) - run: codex login')

    # ------------------------------------------------------------------
    # tests (alphabetical order = execution order: auth gate runs first)
    # ------------------------------------------------------------------

    def test_A01_auth_status(self):
        """codex login status must pass before any billed smoke task runs."""
        head, use_cmdline = self._codexCommand()
        argv = [head, 'login', 'status']
        kwargs = {
            'timeout_s': 60,
            'env': self.launchEnv('codex-smoke'),
            'verify': self._verifyAuth,
            'label': 'codex login status',
        }
        if use_cmdline:
            return self.job(cmdline=subprocess.list2cmdline(argv), **kwargs)
        return self.job(argv=argv, **kwargs)

    def _verifyAuth(self, result):
        if result['returncode'] != 0:
            raise SkipTest(
                'codex is installed but not logged in - run: codex login '
                f"(rc {result['returncode']}, "
                f"tail: {(result['stderr'] or result['stdout'] or '')[-200:]})")
        self._codex_authed = True

    def test_A02_read_only(self):
        """Codex calls one read-only Envoy tool and reports live TD state."""
        self._requireAuth()
        # get_td_info returns 'version' = f'{app.version}.{app.build}' (no
        # bare 'build' key) - ask for that field, check the live build in it.
        prompt = (
            'You have access to MCP tools from a server named envoy '
            '(a TouchDesigner automation server). Call its get_td_info tool '
            'exactly once, then reply with a single line: '
            'VERSION:<the value of the "version" field from the result>. '
            'Do nothing else: no other tools, no shell commands, no files.')
        return self._codexJob(prompt, verify=self._verifyReadOnly,
                              label='codex exec read-only (get_td_info)')

    def _verifyReadOnly(self, result):
        text = self._lastMessage(result)
        self.assertTrue(text, f'no final message from codex; stderr tail: '
                              f"{(result['stderr'] or '')[-300:]}")
        self.assertIn('VERSION:', text,
                      f'no VERSION: line in reply: {text[-200:]!r}')
        self.assertIn(str(app.build), text,
                      f'reply lacks the live TD build {app.build} - the '
                      f'tool result never reached the agent: {text[-200:]!r}')

    def test_A03_create_and_write(self):
        """Codex creates a DAT and writes a token - verified in live TD."""
        self._requireAuth()
        self._token = f'codex-smoke-{int(time.time())}-{os.getpid()}'
        sandbox_path = self.sandbox.path
        prompt = (
            'You have access to MCP tools from a server named envoy '
            '(a TouchDesigner automation server). Perform exactly these '
            'steps using envoy tools, then stop. '
            f"1) create_op with parent_path='{sandbox_path}', "
            "op_type='textDAT', name='codex_smoke'. "
            f"2) set_dat_content with op_path='{sandbox_path}/codex_smoke' "
            f"and text='{self._token}'. "
            '3) get_dat_content on the same op_path and confirm the text '
            'matches what you wrote. '
            f'Reply with a single line: DONE {self._token} if every step '
            'succeeded, otherwise FAIL <reason>. Do not delete anything; do '
            'not use any other tools, shell commands, or files.')
        return self._codexJob(prompt, verify=self._verifyCreateAndWrite,
                              label='codex exec create/write/read-back')

    def _verifyCreateAndWrite(self, result):
        text = self._lastMessage(result)
        self.assertIn('DONE', text, f'agent did not report DONE: '
                                    f'{text[-300:]!r}')
        self.assertIn(self._token, text,
                      'agent reply is missing the run token')
        # PRIMARY EVIDENCE: the operator actually exists with the token.
        probe = self.sandbox.op('codex_smoke')
        self.assertIsNotNone(probe, 'codex_smoke DAT not found in sandbox '
                                    '- the create never landed in TD')
        self.assertEqual(probe.text.strip(), self._token,
                         f'DAT content mismatch: {probe.text[:120]!r}')
