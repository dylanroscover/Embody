# Worktrees and a Running TouchDesigner

Embody externalizes operators to files and TD hot-syncs them: a source DAT (`.py` and other text) has `syncfile=True`, so TD reloads it, and reinitializes any extension it backs, the moment the file changes on disk. A half-applied multi-file edit therefore hot-reloads straight into the live session.

## The rule

Match the isolation to the risk, and check for other writers first.

- **Default: edit the live tree directly** when you are the sole writer (`get_sessions` shows no other active, non-stale session on the same files, and the user is not mid-save) and the change is small to moderate. Land each file, then verify (`get_op_errors`, exercise the change) before the next. This is the normal Embody workflow; one reload self-heals.
- **Use an isolated git worktree** (`git worktree add ../<repo>-wt-<task> HEAD`) when landing live is genuinely risky: another writer is active on the checkout, the change spans several extensions at once (a half-applied state would hot-reload), or you must pass through known-broken intermediate states. Build there, then land the finished diff.
- Keep the `<repo>-wt-<task>` name beside the repo: Envoy's generated settings pre-authorize Read/Edit in that sibling pattern, so the AI client never prompts per file, and Envoy mirrors the gitignored AI config (`.mcp.json`, `.claude/settings.local.json`) into every sibling `-wt-` worktree when it deploys config. A worktree created while Envoy was already running misses that sweep: copy those two files from the repo root, or restart Envoy, before launching a session inside it.
- Two writers never share one checkout: two agents, or an agent plus a human saving from TD. If other AI sessions are active, coordinate worktree tasks through claims (`/multi-session-etiquette`, Worktrees).

## The windows

1. Editing in a worktree: TD may run freely. TD reads externalized files from the main tree only and cannot see the worktree.
2. Landing in the live main tree: safe when you are the sole writer and verify file by file; close TD for the port only when a broken intermediate would matter or a second writer cannot be excluded.
3. After landing: let Embody's startup restores finish, then verify for real (`get_op_errors` with `recurse=true` on the affected COMPs, exercise the behavior, run the project's tests). Worktree verification is static only; nothing is truly tested until it lands, because TD always loads the main tree.

## Drift check before landing

A running TD WRITES to the main tree on every save. Run `preflight_landing(worktree_path)` before porting any diff: it intersects the landing's files with main-tree dirt, peer `file:` claims and unsaved live TDXN state in one call; a `conflicts` verdict means reconcile first (rebase the worktree changes, or save and commit in TD), never overwrite main-tree edits blindly. Move the diff with `git cherry-pick -n <commit>`, or stage everything in the worktree and pipe `git diff --cached --binary` into `git apply` (`--binary` carries `.tox` changes, staging carries new files).

## Cleanup

`git worktree remove <path>` once the diff has landed and been verified, as part of finishing the task; stale worktrees accumulate silently.
