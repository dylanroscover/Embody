# Agent-Tier Tests (AI-client connectivity)

The AGENT tier verifies that real AI clients -- Claude Code, Codex, and a
deterministic MCP contract client -- can reach and correctly use Envoy's MCP
tools end to end. Like `destructive-tests.md`, this is a dev-only convention
for the Embody test harness -- it has NO shipped template counterpart.

## The tier model

- Suites inherit `AgentTestCase` (which carries `AGENT = True`). They are
  EXCLUDED from `RunTests` / `RunTestsSync` / `RunTestsDeferred*` AND from
  `RunDestructiveTests` by `_discoverTestSuites`' tier filter. Do not remove
  that guard: a normal full run must never spawn AI clients or burn
  subscription usage silently.
- They run ONLY via `op.unit_tests.RunAgentTests(suite_name=None,
  test_name=None, delay_frames=30)` -- fire-and-forget; poll `GetResults()`.
- Two layers:
  - **Tier 1 (`test_agent_contract`)**: no LLM. An out-of-process stdlib MCP
    client (`agent_clients/mcp_contract_client.py`) spawns the EXACT bridge
    command from `.mcp.json` and checks handshake, tool inventory vs the
    `EXPECTED_ENVOY_TOOLS` manifest, and a curated call sequence. This is the
    cheap, deterministic half -- run it first when diagnosing.
  - **Tier 2 (`test_agent_smoke_claude` / `test_agent_smoke_codex`)**: a real
    agent on scripted micro-tasks, verified against LIVE TD state (probe op
    exists with the exact run token), never the agent's prose alone.

## Why the runner is async (do not "simplify" it)

Envoy drains MCP requests on TD's MAIN thread (max 5 per frame). A test that
blocks the main thread while an agent subprocess makes MCP calls deadlocks
the very tools under test until every call times out. So `RunAgentTests`
launches subprocesses non-blocking (stdout/stderr to temp FILES -- an unread
PIPE deadlocks the child at ~64KB; stdin always DEVNULL -- codex exec hangs
probing a silent stdin pipe on Windows, openai/codex#20919) and polls them
via a `run(delayFrames=N)` chain using the STRING-EXPRESSION form, so a
mid-run extension reinit cannot strand the state machine on a stale instance.
Timeouts kill the whole process TREE (the CLIs spawn the bridge as a child).

## Auth and billing (subscription only)

- `AgentTestCase.launchEnv()` strips `ANTHROPIC_API_KEY`,
  `ANTHROPIC_AUTH_TOKEN`, `OPENAI_API_KEY`, `CODEX_API_KEY` from the child
  env: a set API key silently OVERRIDES subscription auth and bills per
  token. With them absent, `claude -p` uses the stored Pro/Max OAuth login
  and `codex exec` the ChatGPT login.
- Missing CLI -> loud SKIP (`requireCli`). Codex suites gate on
  `codex login status` (exit 0 = logged in) before any billed task.
- Claude exit codes worth knowing: 100 = not logged in, 101 = MCP server
  unreachable (with `--strict-mcp-config`), 102 = tool permission denied in
  `-p` mode (no TTY -> no prompt -> immediate deny). Codex exec exit codes
  are NOT documented as task success -- judge by `--output-last-message` +
  TD state.

## Isolation choices (keep them)

- Smoke agents run from a NEUTRAL temp cwd: no CLAUDE.md / AGENTS.md /
  project settings, so the agent does the micro-task, not session rituals.
- Claude: `--mcp-config <envoy-only json>` + `--strict-mcp-config` +
  per-task `--allowedTools` allowlists + a standing `--disallowedTools`
  denylist (execute_python, run_tests, delete_op, import_network,
  restart_td, launch_td, switch_instance).
- Codex: inline `-c mcp_servers.envoy.*` overrides (Codex does not read
  `.mcp.json`; the user's `~/.codex/config.toml` is never touched), with
  `-c approval_policy="never"` (the exec subcommand rejects `-a` on
  installed builds) + `default_tools_approval_mode="auto"` so MCP calls are
  not auto-cancelled, `--sandbox read-only` (MCP servers spawn OUTSIDE the
  sandbox), and a real `.exe` when the npm `.cmd` shim is what PATH probing
  finds.
- `delete_op` is NOT part of any Tier-2 smoke task: a peer session's recent
  writes can gate it mid-test (multi-session destructive gate). Tier 1
  covers delete_op and treats a MULTI-SESSION GATE refusal as
  operational-with-gate.
- Each spawned client registers as its own Envoy session via
  `EMBODY_SESSION_LABEL` (`agent-test-tier1`, `claude-smoke`,
  `codex-smoke`), so `get_sessions` / `_peers` traffic is attributable.

## Maintenance

- Envoy tool surface changed -> update `EXPECTED_ENVOY_TOOLS` in
  `test_agent_contract.py` in the SAME commit. The inventory check fails on
  drift in either direction. Remember tools register on the FastMCP instance
  at Envoy START -- restart Envoy before believing a mismatch.
- Model pins live at the top of the smoke suites (`CLAUDE_SMOKE_MODEL`,
  `CODEX_SMOKE_MODEL`). The Codex model list rotates with releases; update
  deliberately, never blindly.
- The runner machinery itself is unit-tested in `test_agent_runner.py`
  (normal tier): tier gating, job lifecycle, timeout kill, verdict
  classification, Filecleanup storage roundtrip. Update it when touching the
  agent runner.

## For the AI agent

- Never run `RunAgentTests()` casually: it spends the user's subscription
  usage and takes minutes. Run it when asked, before releases, or when
  MCP-facing code changed.
- Prefer Tier 1 alone (`RunAgentTests(suite_name='test_agent_contract')`)
  for transport/contract diagnosis -- it is free and deterministic.
- The spawned agents appear as peer sessions; expect `_peers` advisories
  during a run and do not treat the smoke agents' touches as a human peer.
