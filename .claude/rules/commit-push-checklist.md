# Commit and Push Checklist

This rule applies to EVERY commit, not just releases. For version-specific release steps (changelog, README badge, template sync), load the `/release` skill.

## Before Every Commit

### 1. Evaluate All Changes

- Run `git diff --stat` and `git diff --name-status` (or `git diff --cached` if already staged) to see every file touched.
- **Read the diffs**, not just the filenames. Understand what each change does and why. Never commit changes you haven't reviewed.
- Confirm no unintended files are staged (secrets, `.env`, build artifacts). `externalizations.tsv` is machine-written by Embody: commit its changes, never hand-edit it.

### 2. Documentation Audit

For each changed file, ask: does this change affect user-facing behavior or developer understanding?

| Change type | Doc action |
|---|---|
| New feature or tool | Add or update relevant page in `docs/` |
| Changed behavior | Update the doc that describes the old behavior |
| New or changed parameter | Update parameter docs and help text |
| Rule or skill change in `.claude/` | Check for template counterpart (see the `/release` skill, step 4) |
| Non-obvious bug fix | Consider adding to gotchas or troubleshooting |

If no docs need updating, that's fine — but the evaluation must happen.

#### Docs are not live until they are on `main`

`.github/workflows/docs.yml` builds and deploys the MkDocs site **only** on a push to `main` that touches `docs/**` or `mkdocs.yml`. Editing a page on `dev` changes nothing at `https://dylanroscover.github.io/Embody/` — the full chain is: edit → **commit** → **merge/push to `main`** → workflow run → live.

- **Never hand the user (or an issue reply, or anyone else) a docs URL as if it reflects an edit that has not deployed yet.** Say plainly what state it is in: uncommitted, committed but on `dev`, or deployed.
- Any turn that edits `docs/**` ends by stating the remaining steps to publication, not just "docs fixed."
- Confirm a deploy rather than assuming it: `gh run list --workflow=docs.yml -L 3` (and `git log origin/main..HEAD --oneline -- docs/ mkdocs.yml` to see doc commits still unmerged). Compare against `origin/main`, never a local `main` pointer that may be stale.
- `mkdocs build --strict` locally proves the page *builds*; it says nothing about whether it is *published*.
- When confirming a deployed page with WebFetch, remember it **caches per URL for 15 minutes** — re-fetching a URL you already fetched this session returns the pre-deploy copy and reads exactly like a failed deploy. Verify with a cache-busting query string (`?cb=<change>`), which MkDocs ignores.

### 3. Test Audit

CLAUDE.md critical rule #10: "Always update unit tests when modifying project code."

- Changed code in `dev/embody/Embody/` → check whether existing tests assert against the changed behavior.
- New code path, function, or MCP tool → add test coverage.
- Refactor without behavior change → run existing tests to confirm they still pass.
- If tests were added or removed, note the new count for the next release commit's README update.

### 4. Commit Message

- Stage files explicitly by name — avoid `git add -A` or `git add .`.
- Write a message that describes **why**, not just what. The diff shows "what."
- Non-release commits: imperative mood, concise summary.
- Release commits: follow the format in the `/release` skill (`Embody vX.Y.Z: <themes>`).

### 5. Release Detection

If the commit includes version-significant changes (new features, bug fixes, behavior changes in core extensions), remind the user that a release commit may be warranted and point to the `/release` skill. Do not silently skip version prep.

## Before Pushing

Pushing is ONLY done when the user explicitly asks. When they do:

- Confirm the target branch: `git branch --show-current`. Never push to `main` without explicit instruction.
- Check remote state: `git log origin/<branch>..HEAD --oneline` to see what will be pushed.
- If pushing includes release commits, confirm the version number in the commit message matches the changelog and README badge.
