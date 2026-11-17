# Roadmap

Where Embody and Envoy are headed. This is a living document -- direction,
not promises -- and items move as the ecosystem moves (client support for
new MCP protocol features in particular). Dates are deliberately absent.
Want to influence it? [Open an issue](https://github.com/dylanroscover/Embody/issues).

## Landed since the last revision (2026-09-06)

Items that sat here and have since shipped -- kept one revision for the record:

- **All five ideas adopted from [td-mcp-rs](https://github.com/Verbalize-public/td-mcp-rs)**
  (credited in the README): OS dialog detection and dismissal
  (`list_dialogs` / `dismiss_dialog`), per-call instance addressing
  (`instance` on every tool), stable error codes with fix hints
  (`error_code` on every envelope), capture for every operator family
  (`capture_op`), and shader lint on DAT writes.
- **Cross-instance install lock.** Two TD instances bootstrapping the same
  project venv now take a cross-process lock beside it instead of racing.
- **No worker-thread `print()`.** The transport-security fallback queues its
  message for the main thread instead of writing to the Textport from a
  worker.
- **Dragged-in `.tox` links are a setup-wizard question.** The dropped-tox
  warning no longer opens a modal before the wizard on a first install; the
  wizard asks once and the answer becomes the `Toxdropexpr` preference.

## Next up

- **Structured decisions instead of invisible modals.** When an AI session
  drives an operation that needs a human-style decision (palette black-box
  vs full export, file-cleanup keep/delete), the tool should return a
  structured `needs_decision` payload -- and accept the decision as an
  explicit parameter -- rather than opening a TD modal nobody is sitting in
  front of. TD dialogs remain the path for human-driven moments. Locked
  content already works this way for the MCP export tools (a WARNING naming
  the remedy, never the dialog), and `dismiss_dialog` is the escape hatch
  for a modal that is already up; the general payload is still open.
- **Setup Wizard suppression for automated installs** (landed for the
  bootstrap path: `embody_bootstrap.py` grafts a self-deleting Execute DAT
  that applies the wizard's backend on first open, and the starter project
  installs the latest release into a blank `.toe`). A first-class headless
  switch (apply Auto defaults, enable Envoy per configuration, no dialogs
  armed) is the remaining polish.
- **The starter project with every release.** `starter/Embody-starter.toe`
  is linked from the quickstart today; shipping it as a release asset beside
  the `.tox` lets a project start from a release download alone.

## Gated on MCP 2026-07-28 client adoption

The 2026-07-28 MCP revision (stateless core, versioned extensions) ships in
SDK 2.x, which Envoy already runs (2.2.0). The features below need *clients*
that speak the new revision; Claude clients negotiated the 2025-era revision
with Envoy when this was last checked, so these wait on the ecosystem:

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
  the protocol. The bridge-side blocker is gone: the old read-to-EOF
  transport was replaced by the incremental streaming reader (A-46), so
  what remains is bidirectional routing plus a client that speaks the new
  revision. The structured-decision pattern above delivers the same
  workflow benefit today without protocol support.

## Hardening backlog

Smaller items accepted-and-deferred from reviews. Tracked here so they
cannot silently vanish:

- `list_dialogs` / `dismiss_dialog` on macOS: the backend exists but is
  unverified on real hardware (Windows is verified).
- Import-gate edge: a 1.x MCP stack whose original import died before
  `fastmcp` registered can slip past the legacy-stack refusal (worst case
  is a crash-then-clean-restart, not a stuck state).
- The import-gate warm-up poll re-arms unbounded if a cold import hangs
  indefinitely (e.g. antivirus wedge) -- needs a bound and a give-up
  message.
- Multi-project-in-one-TD-process: the stale-interpreter refusal's wording
  says "upgraded on disk" when the actual cause is a second project with a
  different mcp version (the remedy it gives -- restart TD -- is correct).
