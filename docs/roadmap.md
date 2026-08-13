# Roadmap

Where Embody and Envoy are headed. This is a living document -- direction,
not promises -- and items move as the ecosystem moves (client support for
new MCP protocol features in particular). Dates are deliberately absent.
Want to influence it? [Open an issue](https://github.com/dylanroscover/Embody/issues).

## Next up

- **Structured decisions instead of invisible modals.** When an AI session
  drives an operation that needs a human-style decision (palette black-box
  vs full export, file-cleanup keep/delete, dropped-tox expression
  warnings), the tool should return a structured `needs_decision` payload
  -- and accept the decision as an explicit parameter -- rather than
  opening a TD modal nobody is sitting in front of. TD dialogs remain the
  path for human-driven moments.
- **Headless setup for automated installs.** The Setup Wizard is right for
  humans and wrong for harnesses: the fresh-install smoke test currently
  stalls on it. A seeded/suppressed path (apply Auto defaults, enable
  Envoy per configuration) lets automated installs and CI-style smokes run
  unattended.

## Gated on MCP 2026-07-28 client adoption

The 2026-07-28 MCP revision (stateless core, versioned extensions) ships in
SDK 2.x, which Envoy already runs. The features below need *clients* that
speak the new revision; Claude clients currently negotiate the 2025-era
revision with Envoy, so these wait on the ecosystem:

- **Protocol-revision telemetry.** Log which MCP revision each session
  negotiates -- the tripwire that tells us when 2026-07-28 clients actually
  arrive in the field and the items below become real.
- **Tasks extension binding.** Bind the shipped job layer -- `run_tests`
  with `background=True`, `save_project`, and the `get_job_status` poll,
  whose results park restart-proof on disk -- to MCP's Tasks extension,
  with capability gating and a polling fallback for older clients. The
  job layer was deliberately built in the Tasks shape, so this is an
  adapter rather than a rearchitecture.
- **MCP Apps.** Interactive UI surfaces served through the protocol into
  the client. Natural Embody candidates, roughly in value order: a live
  TOP capture viewer (pick an op, watch it -- versus one-shot image
  embeds), the Embody manager / externalization dashboard, and eventually
  the Setup Wizard itself for agent-driven installs.
- **True MCP elicitation.** Mid-tool questions routed to the client over
  the protocol. Verified blocker (2026-07-29): the STDIO bridge reads each
  HTTP response to completion and returns the first SSE event, so a
  server-initiated question inside an open stream would deadlock the read
  and be misrouted as the response. Real elicitation therefore requires a
  streaming bridge rework (incremental SSE reader + bidirectional
  routing). The structured-decision pattern above delivers the same
  workflow benefit today without protocol support.

## Hardening backlog

Smaller items accepted-and-deferred from recent reviews (v6.0.162's
adversarial panel, mostly). Tracked here so they cannot silently vanish:

- Streaming SSE reader in the bridge (shared prerequisite with elicitation
  above; also enables server-side progress notifications).
- Cross-instance install mutex: two TD instances bootstrapping the same
  project venv can race; uv's own locking and the spec-stamp self-heal
  mitigate, but a lock beside the venv would close it.
- A worker-thread `print()` on the transport-security fallback path routes
  to the Textport from off-main-thread (fires only on a broken venv).
- Import-gate edge: a 1.x MCP stack whose original import died before
  `fastmcp` registered can slip past the legacy-stack refusal (worst case
  is a crash-then-clean-restart, not a stuck state).
- The import-gate warm-up poll re-arms unbounded if a cold import hangs
  indefinitely (e.g. antivirus wedge) -- needs a bound and a give-up
  message.
- Multi-project-in-one-TD-process: the stale-interpreter refusal's wording
  says "upgraded on disk" when the actual cause is a second project with a
  different mcp version (the remedy it gives -- restart TD -- is correct).
- Fresh-install smoke: suppress the Setup Wizard for headless runs (see
  Next up) -- the settle-check race half of this landed already.
