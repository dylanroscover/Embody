# Multi-Session Coordination

Run two Claude Code windows on the same project and, without coordination, they will eventually clobber each other: one imports a network over the COMP the other is building in, both edit the same externalized file, or a test run lands in the middle of a build. Envoy is the one process every session already talks to, so it acts as the coordination hub. Sessions are identified, their activity is tracked, overlaps produce warnings, and destructive operations are gated — automatically, with no setup.

## How sessions are identified

Each AI client session runs its own bridge process, and every bridge stamps its requests with a session id and a human-readable label (`repo@branch`, e.g. `MyProject@main`). Envoy uses the headers to tell sessions apart; nothing is required from the model or the user.

- Set the `EMBODY_SESSION_LABEL` environment variable before launching a client to override the label.
- Clients that connect without the bridge (or bridges from before this feature) appear as a shared anonymous session and upgrade to full identity when the session is reopened.
- `get_td_status` also lists live bridge sessions from their heartbeat files — that view works even while TouchDesigner is down.

## Presence: `get_sessions`

`get_sessions` returns every connected session: label, idle time, request count, the last tool it ran, `recent_scopes` (op paths and files it recently modified), `claims` it holds, and `you` — the caller's own session id. AI agents are instructed (via the generated `multi-session` rule) to check it at session start and before large operations.

## Advisories: the `_peers` field

Every write operation records the territory it touched — the op paths involved, plus the files they map to through Embody's externalization tracking (an operator inside a TDN-tracked COMP maps to its `.tdn`; an externalized DAT maps to its `.py`). When any later request overlaps territory another session touched in the last ~10 minutes, the response carries a `_peers` advisory:

```json
"_peers": [
  {
    "label": "MyProject@feature-x",
    "scope": "/project1/scene/cam",
    "tool": "set_parameter",
    "age_s": 12.4,
    "conflict": true
  }
]
```

`conflict: true` means both sides are *writes* within the last minute — the agent is instructed to treat it as a hard stop: check `get_sessions`, report to the user, and coordinate before continuing. Informational (non-conflict) advisories are deduplicated per peer and scope so they stay token-lean; conflicts always ride, and also log a `CONFLICT WARNING`.

## Claims: cooperative leases

Before a large build or an externalized-file edit, an agent can claim its work area:

- `claim_scope(scope, note, ttl)` — scope is an op-path prefix (`/project1/scene`), a repo file (`file:scripts/tools.py`), or a special scope (`project:tests`). The note tells peers *why* ("rebuilding camera rig").
- A live peer's overlapping `claim_scope` is refused with the holder's label, note, and expiry.
- Leases renew automatically whenever the holder's own tools touch the scope, and expire on their TTL or when the holding session goes silent — a crashed or closed session can never deadlock the project.
- `release_scope(scope)` releases early; it is polite, not required.

Claims are cooperative write leases, not read locks: they never block another session from *looking* at anything.

## The destructive-operation gate

Some operations are unrecoverable enough that a warning after the fact is not good enough. While a live peer session holds a claim on the scope — or wrote it within the last minute — these are refused up front with a `MULTI-SESSION GATE` error naming who and why:

- `delete_op`
- `import_network` with `clear_first=True`
- `run_tests` (gated by the `project:tests` scope)
- `batch_operations` containing any of the above

Every gated tool accepts `override=True` for the cases where the refusal is wrong — a stale session of the same user, or explicit user instruction. Agents are instructed never to override silently.

## What Envoy cannot see

Envoy only observes MCP traffic. When an AI session edits an externalized file directly with its own file tools (outside MCP), no touch is recorded. The generated `multi-session` rule closes this gap by convention: agents check `get_sessions` and claim the `file:` scope before editing externalized files, and re-read files a peer recently touched.

## For agent authors

Projects configured by Embody receive a `multi-session` rule alongside the other generated rules. It encodes the etiquette: check presence at session start, treat `conflict: true` and `MULTI-SESSION GATE` as hard stops, claim narrow scopes before big work, never override silently, and divide work spatially (separate COMP subtrees) when running sessions in parallel.
