# Commit and Push Checklist

This rule applies to EVERY commit, not just releases. For version-specific release steps (changelog, README badge, template sync), see `release-commits.md`.

## Before Every Commit

### 1. Evaluate All Changes

- Run `git diff --stat` and `git diff --name-status` (or `git diff --cached` if already staged) to see every file touched.
- **Read the diffs**, not just the filenames. Understand what each change does and why. Never commit changes you haven't reviewed.
- Confirm no unintended files are staged (secrets, `.env`, build artifacts, `externalizations.tsv`).

### 2. Documentation Audit

For each changed file, ask: does this change affect user-facing behavior or developer understanding?

| Change type | Doc action |
|---|---|
| New feature or tool | Add or update relevant page in `docs/` |
| Changed behavior | Update the doc that describes the old behavior |
| New or changed parameter | Update parameter docs and help text |
| Rule or skill change in `.claude/` | Check for template counterpart (see `release-commits.md` step 4) |
| Non-obvious bug fix | Consider adding to gotchas or troubleshooting |

If no docs need updating, that's fine — but the evaluation must happen.

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
- Release commits: follow the format in `release-commits.md` (`Embody vX.Y.Z: <themes>`).

### 5. Release Detection

If the commit includes version-significant changes (new features, bug fixes, behavior changes in core extensions), remind the user that a release commit may be warranted and point to `release-commits.md`. Do not silently skip version prep.

## Before Pushing

Pushing is ONLY done when the user explicitly asks. When they do:

- Confirm the target branch: `git branch --show-current`. Never push to `main` without explicit instruction.
- Check remote state: `git log origin/<branch>..HEAD --oneline` to see what will be pushed.
- If pushing includes release commits, confirm the version number in the commit message matches the changelog and README badge.
