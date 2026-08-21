---
description: "Procedure for preparing version release commits -- changelog, README, templates, versioning"
---

---
name: release
description: MUST READ before preparing a release commit or GitHub release -- project.save() versioning, changelog, README, template sync verification, fresh-install smoke, and the post-push GitHub release procedure (references/github-release.md).
---

# Release Commit Procedure

When the user asks to prepare a release commit (e.g., "prep a commit for v217"), follow these steps in order. After a successful push, follow `references/github-release.md` for the GitHub release.

## 0. Save the Project

The entire save call is `project.save()` -- no arguments. TD increments the `.toe` filename's trailing build, the `onProjectPreSave` hook in `dev/embody/execute_src_ctrl.py` bumps `par.Version`, deletes the prior release `.tox`, and exports the new one. Filename and `par.Version` stay in lock-step.

The release export honors `pre_release`/`post_release` hook DATs placed
directly under the Embody COMP (none exist today; they fire on EVERY
`project.save`, always in LIVE mode -- the Embody comp is never
copy-staged, so such hooks would mutate the live comp). On any export failure the manifest is NOT written and
the prior version's stale manifest is removed: a pre_release abort or
save failure leaves NO release `.tox` for the new version, while a
post_release failure leaves the fresh `.tox` WITHOUT a manifest -- check
the log and `release/` before pushing. The self-updater's rollback
backup export passes `run_hooks=False`, so shipped hooks never fire in
user projects during updates.

Don't pass a path (TD increments from *your* path's build, desyncing by one). Don't pre-set `par.Version`. Don't call `ExportPortableTox` directly.

**If externalized files changed on disk while TD was closed** (e.g. a landed
worktree diff), verify the affected DATs re-synced into the live network
BEFORE saving: table DATs load their file only on the post-launch refresh
sweep, which can run AFTER an early save -- and the portable export captures
LIVE DAT state, shipping stale content (observed v6.0.133: the exported
`palette_catalog` was missing all 267 just-landed 33070 rows). Pulse
`op.Embody.par.Refresh` and spot-check the changed DATs (row counts, code
markers), then save.

If you've already mis-saved: rename the off-by-one `.toe` on disk to match `par.Version`, then have the user close TD without saving and reopen. Do **not** save again -- the hook will delete the just-correct release `.tox`.

## 1. Audit All Changes

- Run `git diff --stat` and `git diff HEAD --name-status` to identify every changed, added, and deleted file.
- Read the diffs for all core source files (EmbodyExt.py, TDNExt.py, EnvoyExt.py, etc.) to understand what was fixed/added.
- Read diffs for new test files to understand coverage additions.
- Read diffs for docs, schema, and rule/skill files.

## 2. Update Changelog

Add a new entry at the top of `docs/changelog.md`:

```markdown
## v5.0.XXX

One-line summary of the release themes.

- **Feature/fix name**: Description of what changed and why
- ...
```

Each bullet should describe the change clearly enough that a user who didn't write the code understands what happened. Include test counts where relevant.

## 3. Update README.md

- **Version badge + minimum-build statements are AUTOMATED**: `project.save()`
  (via `execute_src_ctrl.updateVersionDocs`) rewrites the README version badge
  from `par.Version` and the minimum-TD-build lines in README.md,
  docs/index.md, and CONTRIBUTING.md from the running `app.build` (the build
  we save with IS the support floor). Verify they match rather than editing by
  hand; the `test_version_sync` suite fails on any drift.
- **Release history**: Add a one-line entry at the top of the "Recent releases" list.
- **Test suite count**: Update the count if new test files were added (count `dev/embody/unit_tests/test_*.py`).

## 4. Verify Template Sync

**When updating a rule or skill in `.claude/`, also update the corresponding template DAT in `dev/embody/Embody/templates/` if one exists.** This applies on every edit, not just at release time -- drift between source and template ships stale guidance to user projects.

Templates in `dev/embody/Embody/templates/` must stay in sync with their `.claude/` counterparts:

| `.claude/` file | Template file |
|---|---|
| `rules/td-python.md` | `templates/text_rule_td_python.md` |
| `rules/parameters.md` | `templates/text_rule_parameters.md` |
| `rules/mcp-safety.md` | `templates/text_rule_mcp_safety.md` |
| `rules/network-layout.md` | `templates/text_rule_network_layout.md` |
| `rules/td-connectivity.md` | `templates/text_rule_td_connectivity.md` |
| `rules/multi-session.md` | `templates/text_rule_multi_session.md` |
| `rules/worktree-td-safety.md` | `templates/text_rule_worktree_td_safety.md` |
| `rules/performance.md` | `templates/text_rule_performance.md` |
| `skills/td-api-reference/SKILL.md` | `templates/text_skill_td_api_reference.md` |
| `skills/movie-export/SKILL.md` | `templates/text_skill_movie_export.md` |
| `skills/parameter-design/SKILL.md` | `templates/text_skill_parameter_design.md` |
| `skills/td-recovery/SKILL.md` | `templates/text_skill_td_recovery.md` |
| `skills/multi-session-etiquette/SKILL.md` | `templates/text_skill_multi_session_etiquette.md` |
| `skills/create-operator/SKILL.md` | `templates/text_skill_create_operator.md` |
| `skills/debug-operator/SKILL.md` | `templates/text_skill_debug_operator.md` |
| `skills/externalize-operator/SKILL.md` | `templates/text_skill_externalize.md` |
| `skills/create-extension/SKILL.md` | `templates/text_skill_create_extension.md` |
| `skills/manage-annotations/SKILL.md` | `templates/text_skill_manage_annotations.md` |
| `skills/mcp-tools-reference/SKILL.md` | `templates/text_skill_mcp_tools_reference.md` |
| `skills/pop-networks/SKILL.md` | `templates/text_skill_pop_networks.md` |
| `skills/visual-aesthetics/SKILL.md` | `templates/text_skill_visual_aesthetics.md` |
| `skills/brief/SKILL.md` | `templates/text_skill_brief.md` |
| `skills/merge-divergent-tox/SKILL.md` | `templates/text_skill_merge_divergent_tox.md` |

This table is the source of truth for what ships; keep it in sync with `_TEMPLATE_MAP_RULES` / `_TEMPLATE_MAP_SKILLS` in `EmbodyExt.py` (the actual shipping map). Template files that exist on disk but are NOT in that map (e.g. `text_rule_commit_push_checklist.md`, `text_rule_github_release.md`, `text_rule_refresh_after_commit.py`) are orphans -- do not add them here.

Templates should be UTF-8 with LF line endings and no BOM. Each template carries an Embody/Envoy generated-by HTML comment, and otherwise must match its `.claude/` counterpart in content -- diff them (normalizing any legacy BOM + line endings) and fix any drift.

Dev-only rules and skills (e.g. `.claude/rules/commit-push-checklist.md`, `.claude/rules/skill-prerequisites.md`, `.claude/rules/code-brevity.md`, `.claude/rules/multi-agent-review.md`, `.claude/rules/destructive-tests.md`, `.claude/rules/embody-code-conventions.md`, `.claude/rules/refresh-after-commit.md`, `.claude/skills/release/` (this skill, incl. `references/github-release.md`), `.claude/skills/agent-tests/`, `.claude/skills/add-mcp-tool/`, `.claude/skills/run-tests/`) live under `.claude/` for Embody developers only and are NOT shipped to user projects -- they have no template counterpart. The root `CLAUDE.md` and `dev/embody/Embody/templates/text_claude.md` serve different audiences and are maintained independently.

## 4b. Re-Vendor the Convoy Host App

The Convoy host-app daemon exists TWICE on purpose: the source of truth in `dev/convoy/`, and a vendored copy at `dev/embody/Embody/convoy/host/` carried inside the `.tox` as text DATs so that installing works with no network access. If the vendored copy is stale, the release ships an installer that writes an OLD daemon -- and nothing about the running project looks wrong.

```
python dev/convoy/vendor_host_modules.py --check    # exit 1 = drift
python dev/convoy/vendor_host_modules.py            # re-vendor, then SAVE
```

The vendored DATs are `syncfile=True`, so copying the file is the whole re-vendor -- TD reloads the DAT on its own. **The new content only reaches the shipped `.tox` on the next `project.save()`**, so re-vendor BEFORE step 0's save, never after.

`--check` reports four drift classes; only the first is fixed by copying:

| Class | Meaning | Fix |
|---|---|---|
| `STALE` | content differs | re-vendor (this script) |
| `MISSING` | daemon module has NO vendored DAT | create a text DAT of that name in the `host` COMP and externalize it (tag `py`) -- **a file copy alone does not make it ship** |
| `ORPHAN` | vendored file with no daemon source | `delete_op` the DAT (deleting only the file lets the next save re-create it) |
| `ok` | current | -- |

`test_convoy_host_vendor.py` asserts the same thing on the CI matrix, comparing newline-normalized text (Embody writes CRLF on Windows; `.gitattributes` stores LF, so the committed bytes are identical). Do not "fix" a CRLF diff by changing how Embody writes files -- that path is shared by every externalized DAT in the project.

Note `convoy_install.HOST_MODULES` is a hardcoded manifest and has gone stale twice already. It is not the gate; the parity test is. If you add a daemon module, the test tells you what else to do.

## 5. Fresh-Install Smoke (before the release is announced)

Cold-open smoke of the DEV project is not enough: a fresh install runs a
different path (the shipped `.tox` dropped into a virgin project -- `init()`
lifecycle, baked par values, no dev checkout, no externalized files). After
exporting the release `.tox`, drag it into a NEW empty project (or a scratch
`.toe`) and verify: no errors, the Advanced-page status/read-only pars show
their intended fresh-install values (e.g. `Updatestatus` = `Disabled`, never
blank), Envoy opt-in prompts behave, and the manager opens. The v6.0.145
empty-Update-Status miss shipped precisely because this step was skipped.

## 6. Stage and Commit

- Stage all changed, added, and deleted files explicitly (avoid `git add -A`).
- Include new `.toe` and `.tox` files; include deletions of old versioned `.toe`/`.tox` files.
- Commit message format:
  ```
  Embody vX.Y.Z: <comma-separated themes>
  ```
- Do NOT push unless the user asks.

## 7. Publish the Docs (`main` only)

The changelog entry from step 2 -- and every other `docs/` edit in the release -- is invisible to users until it reaches `main`. `.github/workflows/docs.yml` triggers on a push to `main` touching `docs/**` or `mkdocs.yml`; a commit sitting on `dev` deploys nothing.

When the user asks to push a release:

1. Push `dev`, then merge `dev` -> `main` and push `main` (the same flow the earlier `Merge pull request` commits used).
2. Verify the deploy actually ran: `gh run list --workflow=docs.yml -L 3`. A queued or failed run means the site still serves the previous version.
3. Confirm the published page, then report the URL. Until then, describe the state accurately ("committed, not yet deployed") rather than linking as though it were live.

`git log origin/main..HEAD --oneline -- docs/ mkdocs.yml` lists doc commits that have not reached `main` yet -- run it against `origin/main`, since a local `main` branch can be many commits stale and makes the gap look larger than it is.

The push also triggers CI (bridge-tests, Actions Security). Watch EVERY
triggered run to green and auto-remediate failures per
`.claude/rules/commit-push-checklist.md` (After Pushing) before
reporting the deploy done -- a deploy is not finished with a red run on
either branch.

This step is NOT release-only: the same chain applies to any standalone docs fix. See `.claude/rules/commit-push-checklist.md` (Documentation Audit).
