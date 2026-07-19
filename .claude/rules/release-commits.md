---
description: "Procedure for preparing version release commits -- changelog, README, templates, versioning"
---

# Release Commit Procedure

When the user asks to prepare a release commit (e.g., "prep a commit for v217"), follow these steps in order.

## 0. Save the Project

The entire save call is `project.save()` -- no arguments. TD increments the `.toe` filename's trailing build, the `onProjectPreSave` hook in `dev/embody/execute_src_ctrl.py` bumps `par.Version`, deletes the prior release `.tox`, and exports the new one. Filename and `par.Version` stay in lock-step.

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

This table is the source of truth for what ships; keep it in sync with `_TEMPLATE_MAP_RULES` / `_TEMPLATE_MAP_SKILLS` in `EmbodyExt.py` (the actual shipping map). Template files that exist on disk but are NOT in that map (e.g. `text_rule_commit_push_checklist.md`, `text_rule_github_release.md`, `text_rule_refresh_after_commit.py`) are orphans -- do not add them here.

Templates should be UTF-8 with LF line endings and no BOM. Each template carries an Embody/Envoy generated-by HTML comment, and otherwise must match its `.claude/` counterpart in content -- diff them (normalizing any legacy BOM + line endings) and fix any drift.

Dev-only rules and skills (e.g. `.claude/rules/commit-push-checklist.md`, `.claude/rules/github-release.md`, `.claude/rules/release-commits.md`, `.claude/rules/skill-prerequisites.md`, `.claude/skills/add-mcp-tool/`, `.claude/skills/run-tests/`) live under `.claude/` for Embody developers only and are NOT shipped to user projects -- they have no template counterpart. The root `CLAUDE.md` and `dev/embody/Embody/templates/text_claude.md` serve different audiences and are maintained independently.

## 5. Stage and Commit

- Stage all changed, added, and deleted files explicitly (avoid `git add -A`).
- Include new `.toe` and `.tox` files; include deletions of old versioned `.toe`/`.tox` files.
- Commit message format:
  ```
  Embody vX.Y.Z: <comma-separated themes>
  ```
- Do NOT push unless the user asks.
