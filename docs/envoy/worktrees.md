# Git Worktrees

Git worktrees give an AI agent (or you) an **isolated checkout of the repo, beside the repo** — a place to build multi-step changes without the live TouchDesigner session ever seeing an intermediate state. Envoy treats worktrees as first-class territory: access is pre-authorized, config is mirrored in, in-flight worktree tasks are visible to every session, and landings get an automated safety check.

## Why worktrees matter in a TouchDesigner project

Embody externalizes operators to files, and TouchDesigner **hot-syncs** them: a source DAT reloads — and its extension reinitializes — the moment its file changes on disk. That is the normal, fast editing loop for small changes. It is also the hazard for big ones: a half-applied, multi-file refactor hot-reloading into a running session can break the very extension serving your MCP tools.

A worktree breaks that coupling:

```
git worktree add ../<repo>-wt-<task> HEAD
```

TouchDesigner reads externalized files from the **main tree only**. Nothing in a worktree is visible to the live session until the finished diff is deliberately landed — the invisibility *is* the isolation. Conversely, a running TD **writes** to the main tree on every save, which is why landing is the step that needs discipline (below).

!!! tip "Keep the `-wt-` naming"
    Everything on this page keys off the sibling `<repo>-wt-<task>` convention: pre-authorized file access, config mirroring, durable-claim path derivation, and sandbox detection all identify worktrees by that name pattern. A worktree named or located differently gets none of it.

## What Envoy does for you automatically

- **Pre-authorized access** — the generated `.claude/settings.local.json` includes Read/Edit/Write rules for sibling `<repo>-wt-*` folders, so an AI session never permission-prompts inside a worktree ([Setup](setup.md)).
- **Config mirroring** — `.mcp.json` and `.claude/settings.local.json` are gitignored, so a fresh worktree has neither. Envoy copies both into every sibling worktree on each config deploy **and on every registry refresh** (each project save), so worktrees created mid-session get config without an Envoy restart. The mirror **yields to worktree-native sandboxes**: a worktree with its own `.embody/envoy.json` (see below) keeps its own config untouched.
- **Durable task claims** — claiming `project:worktree-<task>` marks the task in flight for every session, persistently (next section).

## Coordinating worktree tasks across sessions

Worktree work is doubly invisible to peers: the edits are raw file edits (no MCP touch records) *and* they happen outside the repo Envoy watches. Claims make the work visible by intent:

1. **On starting a task**, claim `project:worktree-<task>` with a note naming the files or subsystem the diff will land on.
2. Worktree claims are **durable**: they persist to `.embody/worktree-claims.json`, survive session death *and* Envoy restarts, and expire only when the worktree directory is removed (with a 7-day backstop). Any session may release one at landing time — the landing session is often not the session that started the task.
3. Every session sees in-flight worktree tasks in `get_sessions` under `worktrees` — path, note, holder, and age — even after the starting session is long gone.

One writer per checkout, always: two sessions never share one worktree. A second session starts its own `../<repo>-wt-<other-task>`.

## Landing a worktree diff

Landing is the one moment worktree work touches the live tree — and the moment things historically go wrong: a running TD may have re-exported files the diff also touches, a peer may be mid-edit, or the live network may hold unsaved TDXN state that the next save will write over the landed files.

**Run `preflight_landing` first.** It is read-only, answers off the main thread, and intersects the landing's file list with three hazard sets in one call:

```
preflight_landing(worktree_path="../<repo>-wt-<task>")
```

| Collision class | What it means | What to do |
|---|---|---|
| `main_dirty` | Files the landing touches are also uncommitted-dirty in the main tree (often TD's own re-exports) | Reconcile: rebase the worktree on the main tree, or commit/save the main tree first — never overwrite blind |
| `peers` | A peer session holds a `file:` claim on — or recently wrote — a landing file | Coordinate with the named peer before proceeding |
| `tdn_unsaved` | A landing file's live TDXN/DAT state is unsaved (`dirty` in `externalizations.tsv`) | Save the project (or re-export) so the live state is on disk before you replace it |

The verdict is `clear` or `conflicts`; an empty intersection on all three classes means the diff can land as one reviewed unit (e.g. `git apply` of the worktree diff). After landing: let the hot-sync sweep pick the files up, check `get_op_errors`, and run the project's tests — worktree verification is static-only, so nothing is truly tested until the main tree runs it.

Remove the worktree (`git worktree remove`) and release the claim when done — stale worktrees accumulate silently, and the claim's directory check will expire it either way.

## Worktree-native TD sandboxes

Nothing stops you from opening a **worktree's own `.toe` in a second TouchDesigner instance**. A worktree is its own git root, so Envoy started there writes the worktree's own `.embody/` registry and `.mcp.json` — a fully isolated sandbox where an agent can *run* its changes before landing, instead of verifying statically.

[Per-session instance pinning](tools-reference.md) is what makes this safe: sessions pin to instances independently, so a sandbox instance registering — or a session switching to it — never re-routes the sessions working against your main project. Point a session at a sandbox with `switch_instance` (session-local by default) or pin it from birth with the `EMBODY_PIN_INSTANCE` environment variable. The config mirror detects native sandboxes and leaves their config alone.

## The safe/unsafe windows at a glance

| Window | Safety |
|---|---|
| Editing in a worktree | TD may run freely — it cannot see worktree files |
| Landing into the live main tree | Safe when you are the sole writer and `preflight_landing` reports `clear`; each landed file hot-reloads once |
| A running TD saving while you land | The hazard `main_dirty` / `tdn_unsaved` collisions exist to catch — reconcile, never race |
| Running a worktree-native sandbox instance | Safe with per-session pinning; main-project sessions are never re-routed |

!!! info "For agent authors"
    Projects configured by Embody ship this workflow to agents as the `worktree-td-safety` rule plus the worktree sections of the `multi-session-etiquette` skill — the conventions here are enforced by generated guidance, not just documentation.
