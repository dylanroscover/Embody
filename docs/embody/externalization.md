# Externalization Details

## Build Tracking

Embody adds and updates an **About** page on every externalized COMP with:

- **Build Number** — incremented each time the COMP is saved
- **Touch Build** — the TouchDesigner version used for the save
- **Build Date** — UTC timestamp of when the `.tox` was written

This provides robust version tracking directly on your components.

## Folder Configuration

The externalization folder can be configured in several ways:

- **Static Path**: Set a folder name like `externals` to save to `{project.folder}/externals/`
- **Expression Mode**: Use Python expressions for dynamic paths (e.g., `project.folder + '/build_' + str(app.build)`)
- **Existing Folders**: You can point Embody at a folder containing other files — Embody will only manage its own tracked files and leave others untouched.

!!! note
    When changing the folder location, Embody will migrate tracked files to the new location and clean up empty directories in the old location.

## Duplicate Path Handling

When Embody detects multiple operators pointing to the same external file, it groups them and resolves the duplicates. It tries the automatic resolvers below in order before ever prompting:

**Automatic resolution (replicants):** If the operators are replicants, Embody auto-tags them (the replicator's template is the master) — no dialog needed.

**Automatic resolution (COMPs):** If the operators are COMPs with TouchDesigner clone relationships (`enablecloning` / `clone` parameter), Embody automatically identifies the clone master and tags the others with a `clone` tag — no dialog needed.

**Automatic resolution (DATs in cloned COMPs):** DATs that share a path because they live inside cloned COMPs are auto-tagged too, following their host COMP's clone relationship.

**Automatic resolution (naming convention):** Embody also resolves a group without prompting when exactly one operator's path contains the name set in the **Template Master Name** parameter (`Templatemaster`, default `__template__`). That operator is kept as the master and the rest are tagged as clones. This targets the common app-generated pattern of one template (e.g. a `__template__` COMP) plus many runtime copies that share its externalized files.

- The match is on a whole path segment, not a substring — a COMP named `__template__` matches; one named `mytemplate` does not.
- It only fires when **exactly one** operator in the group matches. Zero or 2+ matches are ambiguous, so Embody falls back to the manual prompt.
- This is opt-in by convention: if none of your operators are named `__template__`, nothing changes and you keep choosing manually. Set the parameter to your own convention (e.g. `_master`) to use a different name, or clear it to always pick the master by hand.

**Batch prompt (multiple unresolved groups):** When 2+ groups remain unresolved after the automatic resolvers, Embody shows one batch dialog with three choices: **Auto-resolve all** (in each group keep the first-listed operator as master, tag the rest as clones), **Review individually** (fall through to the per-group prompt below, once per group), or **Dismiss** (skip for now; re-prompts next cycle). A single unresolved group skips this and goes straight to the per-group prompt.

**Manual resolution:** For groups that none of the automatic resolvers handle, Embody shows a single dialog listing all operators that share the path. You select which operator is the **master**; the others receive a `clone` tag.

- Operators in a group usually share a name, so each selection button is labeled by the path segment that **differs** between them (e.g. `1: __template__`, `2: scene_1exalohf`), numbered to match the list in the dialog body.
- For large groups (more than five operators), a button per operator becomes unreadable, so the dialog instead offers a strategy choice: **Keep first as master** (tags the first-listed operator as master, the rest as clones) or **Dismiss**. The dialog points you at the Template Master Name convention for hands-off resolution next time.
- Selecting a master tags all other operators as clones. Changes to the shared file affect all of them.
- **Dismiss** skips the group for now. Embody will re-prompt on the next Update cycle.

Once any operator in a group has a `clone` tag, the entire group is considered resolved and Embody will not prompt again.

Enable or disable this check with the `Detect Duplicate Paths` parameter.

## Externalizations Table

Embody maintains an `externalizations` tableDAT outside the Embody component with the following columns:

| Column | Description |
|--------|-------------|
| `path` | TouchDesigner operator path (e.g., `/project/base1`) |
| `type` | Operator type (e.g., `base`, `text`, `table`) |
| `rel_file_path` | Relative file path from project folder |
| `timestamp` | Last save time in UTC |
| `dirty` | Dirty state (`True`, `False`, or `Par` for parameter changes) |
| `build` | Build number (COMPs only) |
| `touch_build` | TouchDesigner build version (COMPs only) |
| `strategy` | Externalization strategy (`tox`, `tdxn`, `py`, `txt`, etc.) |
| `node_x` | Operator X position in the network (for restoration) |
| `node_y` | Operator Y position in the network (for restoration) |
| `node_color` | Operator node color (for restoration) |

This table serves as the source of truth for what files Embody manages. Only files listed here will ever be deleted by Embody.

!!! warning
    Never edit the `externalizations.tsv` file directly. It is managed exclusively by Embody's tracking system.

## TDXN Strategy

COMPs can also be externalized using the **TDXN strategy** instead of `.tox`. This exports the COMP's network as human-readable YAML (`.tdxn` files) instead of binary `.tox` files, enabling meaningful git diffs, code review, three-way merges, and schema-validated CI.

!!! note "`.tdn` files from before Embody 6.1"
    The format was called TDN (`.tdn`) until Embody 6.1 renamed it to TDXN (`.tdxn`). Existing files are **not** migrated: Embody keeps writing whichever extension a COMP already uses, and only a *new* externalization mints `.tdxn`, so a project can legitimately hold both. Convert when you choose to, with the **Migrate .tdn to .tdxn** pulse on the Embody COMP.

    The `strategy` column and the operator tag both read `tdxn` as of 6.2.30 -- they were `tdn`, and both spellings are still accepted on read forever, so an existing project keeps working whether or not you re-tag it.

See [TDXN Format](../tdxn/index.md) for format details, and ["Why TDXN"](#why-tdxn) below for the concrete wins.

### TOX vs TDXN: pick by what you want from the file

Both strategies externalize a COMP to its own file on disk. The difference is **what's in the file**, not whether the parent embeds it:

| | TOX | TDXN |
|---|---|---|
| File format | Binary `.tox` | YAML `.tdxn` |
| Git-diffable | No | Yes |
| Load speed | Fast (native TD format) | Slower (parsed and rebuilt) |
| PR review | None — binary blob | Line-by-line parameter diffs |
| Cross-build portable | TD-build-coupled | Format-versioned, portable |
| Embedded VFS files | Preserved | **Not supported — lost** ([details](../tdxn/specification.md#virtual-file-system-vfs-not-supported)) |
| Best for | Palette widgets, third-party COMPs, anything you don't review at the parameter level | Anything you want code-reviewed, anything edited in a text editor, MCP/LLM workflows |

**Both receive the same ownership treatment in parent `.tdxn` files.** When the parent of an externalized child is exported as TDXN, the parent emits a reference (`tdn_ref` or `tox_ref`) and **does not embed the child's internals**. The child's own file is the source of truth. This applies symmetrically — externalizing as TOX does not mean "embed me in the parent."

If you want a parent `.tdxn` that's fully self-contained (snapshot mode), pass `embed_all=True` on export. Otherwise, externalized children stay encapsulated and the parent stays small.

!!! info "If a COMP carries both tags"
    A COMP with both the TDXN tag and the TOX tag is an unusual configuration — strategies are normally mutually exclusive. If it does happen (e.g. tag added by hand), **the TDXN tag wins**: the parent emits `tdn_ref` and the COMP is treated as TDXN-externalized. To switch a COMP between strategies, remove the old tag first.

### TDXN Mode (master switch)

The `Tdxnmode` parameter on the Embody COMP selects how the TDXN subsystem behaves at save/open time:

| Mode | On save (++ctrl+s++) | On project open | When to pick |
|------|----------------------|-----------------|--------------|
| **Off** | No TDXN activity. `.tdxn` files on disk stay untouched. | No reconstruction. | Temporarily disabling TDXN without deleting any files. |
| **Export-on-Save** *(default, recommended)* | Writes `.tdxn` files for every tagged TDXN COMP **whose content changed** since the last save (unchanged COMPs are skipped to avoid noisy git diffs from header churn). `.toe` stays the source of truth; live network is never stripped. | No reconstruction — the `.toe` already has everything. | Day-to-day work. Cheap, predictable, no round-trip risk. Ideal for git-diff / MCP workflows. |
| **Roundtrip (Experimental)** | Writes `.tdxn` files **and** strips COMP children from the `.toe` so the `.toe` stays small. | Children are rebuilt from `.tdxn` files at frame 60. | Large projects where the `.toe` bloats without strip, or workflows that treat `.tdxn` as the primary source. May hit edge cases with extension reload timing on deeply-nested TDXN COMPs. |

You can switch modes at any time — existing `.tdxn` files on disk and tracked COMP entries are preserved across transitions.

!!! note "Opt-in per COMP"
    Regardless of mode, only COMPs you've explicitly tagged with Embody's TDXN tag are touched. A fresh `baseCOMP` you just created is invisible to Embody until you tag it.

### Excluding a COMP from TDXN (the `tdxn_exclude` tag)

The `Tdxnexcludetag` parameter on the Embody COMP (default value: `tdxn_exclude`) defines a tag that **opts a single COMP out of the entire TDXN system**. Tagged COMPs are invisible to TDXN: never exported, never inlined in a parent's `.tdxn`, never stripped on save, never destroyed by reconstruction.

**Primary use case: cascade-autotag bypass.** With cascade autotag enabled (`Tdxncascade` parameter), tagging a parent COMP `tdxn` propagates the `tdxn` tag to every child in the subtree, skipping any child already tagged TOX or `tdxn_exclude` (and everything below it). If a specific child should *not* be externalized — typically because it's app-managed (spawned via `op.copy()` at runtime, populated from user data, or otherwise has a lifecycle outside Embody's control) — apply `tdxn_exclude` to that child to keep it opted out.

**Why not just leave the tag off?** With cascade autotag on, you can't — the cascade would re-apply `tdxn` on the next scan. `tdxn_exclude` is the only durable opt-out.

**For app-managed copies**: a runtime `.copy()` does inherit `tdxn_exclude`, but the tag only keeps the clone out of the **TDXN pipeline** (cascade autotag, parent inlining, strip, reconstruction, and the dropped-`.tox` sweep). It is not a general "invisible to Embody" flag — duplicate-path detection never reads it. So `tdxn_exclude` alone is sufficient only when the copied template is **not itself externalized**. Copy a COMP that *is* externalized (or that contains externalized DATs) and the copy carries the master's Embody tags and file references, joins the master's duplicate group, and still prompts.

For app-spawned copies of an externalized master, use a relationship Embody's duplicate detection already resolves silently:

- **TD-native clone relationship** — set the copy's `clone` parameter to the master, or spawn it as a real clone or replicant. Embody reads TD's clone API (and the ancestor-clone case for DATs inside cloned COMPs), tags the copies as references, and never asks.
- **Template Master Name convention** — keep the master under a path component named by the `Templatemaster` parameter (default `__template__`). When exactly one operator in a duplicate group matches, it becomes the master and the rest are tagged as clones — no prompt. Zero or two-plus matches fall through to the normal dialog, so the choice stays unambiguous.

**Startup prompts honor the tag too.** The dropped-`.tox` expression sweep (the "Dropped .tox Expression Detected" dialog that offers to clean TD's drag-in `externaltox` expression) skips any COMP carrying `tdxn_exclude` — on itself **or on any ancestor**, so tagging a root COMP silences the prompt for its whole subtree. Tag the COMPs in a startup file `tdxn_exclude` and Embody won't ask about them (issue #60). The project-wide **Externalize Full Project** scan skips tagged COMPs the same way.

**Constraints:**

- Only COMPs are excludable. Annotation COMPs are explicitly ineligible.
- Whole-subtree exclusion only applies to a **direct child** of a TDXN boundary. If you nest an excluded COMP *deeper* (under a non-excluded TDXN COMP), the exclusion tag has no effect at that depth — so instead of dropping it, Embody serializes the excluded child as **ordinary content** (it round-trips and survives strip/reconstruction) and warns at export time that the tag was ignored there. The warning names the intervening COMP(s) to tag, or suggests making it a direct child, if you want the exclusion honored.
- Exclusion governs the automatic/cascade pipeline. An explicit user export call (`ext.Embody.saveTDXN()` directly on an excluded COMP) currently still writes the `.tdxn` — the opt-out applies to cascade, parent inlining, strip, and reconstruction, not to deliberate direct invocation.

### Excluding a parameter's value (the `tdxn_exclude:<par>` tag)

Where the bare `tdxn_exclude` removes a whole COMP from the TDXN system, the **colon-suffixed** form removes a **single parameter's value** from an operator that otherwise exports normally. Tag the operator itself — any operator, any family — with one tag per parameter:

```
tdxn_exclude:file
tdxn_exclude:Port
```

This is the opt-out for **runtime state that must not be committed**: a movie player whose `file` par is set per-session by a playback engine, a negotiated network port, live UI readouts. Without it, every export bakes the current session's value into the `.tdxn` and dirties git.

What it does and does not do:

- The operator, its wiring, and its other parameters export normally — only the named parameter's **constant value** stays out of the file.
- An **expression or bind** on the tagged parameter still exports (a reference is configuration, not session state).
- For **custom parameters**, the definition still ships — style, range, default, help — without the `value` key.
- The tag itself appears in the operator's `tags:` list in the `.tdxn`, so the file records why the value is absent, and the marker survives reconstruction.
- A tag naming a parameter the operator does not have logs a **WARNING** at export — a typo cannot silently no-op.
- The prefix is the same **`.tdxn` Exclude Tag** parameter that governs whole-COMP exclusion (Tags page, default `tdxn_exclude`); clearing it disables the whole family.
- Exclusions take effect at the COMP's **next export** — its **Save tdxn** action, an **Update** sweep, or a project save — and the tags themselves persist in the `.toe`/`.tdxn` like any other operator state.

### Excluding a DAT's contents (the `tdxn_exclude:dat_content` tag)

A third form, using the same prefix, targets a **DAT's live contents** instead of a parameter:

```
tdxn_exclude:dat_content
```

`dat_content` is a reserved name — it is not a parameter, so it never triggers the unknown-parameter warning above.

This exists for DATs whose rows are **runtime state with no authored value**: a log ring buffer, a status readout, a scratch table a script rewrites every cook. Such DATs are normally force-captured even when **Embed DATs** is OFF, because Embody refuses to drop content that exists nowhere else on disk (that safety net is what stops a crash from taking your shaders and callbacks with it). For genuinely disposable content that rule is backwards: it rewrites the `.tdxn` on every single export.

Tagging the DAT opts it out:

- The **operator survives** — type, parameters, position, size, color, wiring and tags all export as usual. Only its rows are omitted, and it comes back empty on reconstruction.
- It is the **only sanctioned way past** the "saved nowhere else" safety net, so use it exactly where losing the content is the intent.
- The **save-time content check** below reads the same rule, so it never flags these DATs and **Always Externalize** never files them.
- The tag round-trips in the `.tdxn`, so the file records why the content is absent.

Embody applies it to its own two runtime tables — the log FIFO and the status readout — which between them were rewriting `Embody.tdxn` on every save.

**UI: the Exclude from tdxn panel.** On any TDXN-tagged COMP, the tagger's Actions menu (double-tap the tagger shortcut on the COMP) gains a **◇ Exclude from tdxn** entry. It opens a panel scoped to that COMP: drag any parameter from a parameter dialog onto the drop zone to exclude its value (`tdxn_exclude:<par>`), or drag a COMP from the network editor to exclude it entirely (bare `tdxn_exclude`). The panel lists every exclusion of both kinds in the COMP's subtree with full paths; remove one with the **×** on its row, or by dropping the same item again. Items outside the subtree are refused with a warning, and a COMP excluded deeper than the TDXN boundary gets the same "only honored at a boundary" warning the exporter gives.

### Content Safety (save-time check)

When you save a project (++ctrl+s++, or `save_project` over MCP), Embody reports on TDXN content that a `.tdxn` file cannot hold. A save never waits on a dialog.

**DAT content is captured.** An editable DAT that is not externalized is always written into its COMP's `.tdxn`, whatever **Embed DATs** says: that parameter only decides whether content a file already holds is copied in as well. The keyframe tables of an Animation COMP are always written too. Three cases carry no content:

- **Generated DATs** (Select DATs, filters fed by a wire, device inputs such as Multi Touch In and Keyboard In, Info DATs, Script DAT output) are written as `dat_read_only`. TouchDesigner regenerates their content from parameters, inputs, or live events.
- **DATs tagged `tdxn_exclude:dat_content`** keep the operator but drop its rows, by design.
- **A duplicate docked companion** (`timer1_callbacks1` beside `timer1_callbacks`, both docked to `timer1`) is left out of the export. When it holds content that no file keeps and that differs from the original's, a **WARNING** names it; a copy of the original's content is logged at INFO.

The exporter and this check read the same rule, so they cannot disagree about what is safe. If a DAT's content would still be left out, the save logs a **WARNING** naming it.

**Storage is what the check covers.** With **Embed Storage** off, `comp.storage` entries on a TDXN COMP's descendants are not in its `.tdxn`. Whether that loses them depends on the TDXN mode:

| Mode | Descendant storage (Embed Storage off) | Logged as |
|------|----------------------------------------|-----------|
| Export-on-Save | The live session and the saved `.toe` keep it. Only a COMP rebuilt from its `.tdxn` (crash recovery, a fresh clone) comes back without it. | INFO |
| Roundtrip, **Strip on Save** on | Gone after this save and on every reopen: the strip rebuilds the COMP from its `.tdxn`. | WARNING |
| Roundtrip, **Strip on Save** off | Kept until the next open, when **TDXN Create on Start** rebuilds the COMP from its `.tdxn` without it. | WARNING (INFO with Create on Start off) |

Storage on the TDXN COMP itself rides its shell in the `.toe` in every mode and is logged as INFO. Storage inside a TOX-externalized COMP lives in its `.tox` and is not reported. Each message names the fix: turn on **Embed Storage** (`Embedstorageintdxns`) or the COMP's **Embed storage in tdxn** toggle in the tagging menu.

Findings go to the Embody log: the log files (see the **Log Folder** parameter), and the textport when **Print** is on. Over MCP, a finished `save_project` job lists the save's WARNING and ERROR lines in `get_job_status(job_id)["warnings"]`, so storage a save or the next open will lose is there. INFO findings (Export-on-Save, storage on the TDXN COMP itself) are in the log only; read them with `get_logs`.

The **Content Safety** parameter (`Tdxndatsafety`) sets what a save does:

| Value | Behavior |
|-------|----------|
| **Warn Each Save** (`ask`, default) | Logs the findings above on every save. |
| **Always Externalize** (`externalize`) | Also files unexternalized, editable DATs as their own files, a few seconds after the save finishes (never during it). It leaves out generated DATs, Animation COMP tables, DATs whose `file` parameter is set, and COMPs with **Embed DATs** on. Their content is already in the `.tdxn`, so the wait loses nothing. |
| **Never Warn** (`ignore`) | Silent. |

!!! tip "Unattended projects"
    A scheduled save or an AI agent has nobody to answer a prompt. Set `Tdxndatsafety`, `Tdxnpalettehandling` and `Filecleanup` before saving, and save through `save_project` so its warnings land in the job record.

!!! warning "Embedded VFS files are not supported by TDXN"
    TDXN does **not** capture a COMP's Virtual File System. Files embedded in a
    COMP — fonts, images, shaders, anything addressed as `vfs://...` — are not
    written to the `.tdxn` and **do not come back** when the COMP is
    reconstructed. This is deliberate and permanent: a VFS holds arbitrary
    binary, and carrying it inline would destroy the line-diffability that is
    TDXN's entire point.

    Use **TOX strategy** for COMPs that carry embedded files — a `.tox` preserves
    VFS contents (verified 2026-09-04) — or keep the assets as real files on disk
    and reference them by path, which round-trips through TDXN normally. See
    [Virtual File System (VFS) — Not Supported](../tdxn/specification.md#virtual-file-system-vfs-not-supported).

!!! warning "Locked TOPs, CHOPs, SOPs, and POPs lose their frozen data"
    TDXN cannot store frozen pixel, channel, geometry, or point data. If your network contains locked non-DAT operators, their lock flag is preserved but their content will be **empty after reload** when using Roundtrip mode, or whenever the COMP is rebuilt from its `.tdxn`. Use **TOX strategy** for the COMP that holds the locked operators. See [Lock Flag Limitation](../tdxn/specification.md#lock-flag-limitation) for details.

    The warning labels each locked operator with its source: **re-cooks if unlocked** (the frozen snapshot is replaced, not restored), **NO SOURCE** (unlocking leaves it empty), or **source not traced** (treat it as no source). Unlocking is never a way to keep the frozen data. The dialog's **Switch to TOX** button stores the narrowest child COMP below the TDXN COMP that holds them as a `.tox` (the same as `externalize_op('<child>', tag_type='tox')`), and the parent `.tdxn` then references it with `tox_ref`. The switch runs a few frames after the dialog closes; it never switches a top-level TDXN COMP itself.

    Envoy's `externalize_op`, `save_externalization` and `export_network`, and the Autoexternalize step of `create_op`, `copy_op` and `create_extension`, never show this dialog; Python run through `execute_python` that calls Update or saveTDXN still can. Each export that writes a COMP's `.tdxn` file (`externalize_op`, `save_externalization`, or a save that re-exports a changed COMP) logs one WARNING per exported COMP (an unchanged COMP at save time, autosave checkpoints and ad-hoc `export_network` snapshots of a tracked COMP skip it), naming every operator's source (`source: none|recooks|unknown`) and the COMP to tag `tox`; it rides back on the tool response in `_logs`.

    The save-time warning covers only locked operators the TDXN export itself serializes. Locked content inside a **nested externalization boundary** — a child COMP with its own TOX or TDXN tag, or an exclude-tagged subtree — is that boundary's own concern and does not trigger the parent's warning: a nested TOX-strategy COMP preserves its locked content in its own `.tox`, which is exactly the recommended remedy.

    During a batch sweep — such as **Externalize Full Project** — the per-COMP findings are collected and shown as **one combined dialog** at the end, listing every affected COMP, instead of one popup per export. The dialog's **Don't show again** button sets the **Locked Content Warning** parameter (`Tdxnlockedwarn`) to `quiet`, suppressing future dialogs; the warning is still written to the log on every export. Set it back to `ask` from the Embody COMP's TDXN settings to re-enable the dialog.

### Empty-network overwrite protection

Automatic exports refuse to overwrite a substantial file on disk from a COMP that has **no operator children**. An emptied COMP over a populated file is almost never a real edit — it is the signature of a transiently-emptied shell (an interrupted import, a reload caught mid-flight) about to destroy the only good copy.

| Strategy | What the guard checks | Refusal |
|----------|-----------------------|---------|
| **TDXN** | The COMP has no children other than annotations, and the `.tdxn` on disk parses to a non-empty network — or exists but cannot be parsed at all, which is exactly when its bytes are most worth keeping. | The automatic writers (the dirty sweep, checkpoints, the pre-save update pass) skip the file. |
| **TOX** | The COMP has no children other than annotations and the `.tox` on disk is larger than 4 KB. A `.tox` can't be parsed for content, so size stands in: an empty COMP's `.tox` is only shell and parameters. | The automatic save skips the file. |

A refusal is written to the log as a **WARNING** naming the operator and the reason (`REFUSED auto-export of ...` for TDXN, `REFUSED auto-save of ...` for TOX). The TDXN guard warns once per file state rather than on every sweep, and re-baselines its change fingerprint so a stable refusal stops repeating while any later real edit still registers as changed.

!!! tip "When the empty state is intentional"
    Deliberately emptying a COMP and saving it is still supported — use the **Save** action in the manager (the row's Actions menu). The explicit Save is the deliberate override and writes the empty network to disk. Alternatively, untrack the operator or delete the file if it should no longer exist.

### Why TDXN

TDXN isn't just a different file format — it unlocks workflows that binary `.toe`/`.tox` files can't support.

**File size and density.** Even without compression, `.tdxn` is comparable to or smaller than the equivalent binary `.tox` because only non-default parameters are emitted. Three compaction mechanisms kick in:

- Default omission — parameters are included only when they differ from the operator type's creation defaults.
- `type_defaults` — properties shared across every operator of a type are hoisted once to a top-level block and stripped from each operator.
- `par_templates` — repeated custom-parameter pages collapse into references.

A real leaf-component file like `envoy_toggle.tdxn` is ~1.3 KB — 38 readable lines including only the ~15 parameters whose values actually differ from a `textCOMP`'s defaults.

**Git three-way merge on real conflicts.** `.toe` is binary, so git can't three-way merge it — one side wins, the other loses. `.tdxn` is YAML; git merges it like any other text file, and conflicts show up as readable diffs you can resolve by reading intent:

```yaml
- name: Speed
  style: Float
<<<<<<< HEAD
  default: 1.5
=======
  default: 2.0
>>>>>>> feature/faster-playback
```

**PR review humans can actually do.** A `.toe` diff is literally `Binary files differ`. A `.tdxn` parameter change is a one-line delta. Reviewers comment on specific lines, request changes, and approve — the same workflow as any other text code review.

**Cross-version portability.** `.toe` and `.tox` are coupled to the exact TD build that wrote them. `.tdxn` files are format-versioned and self-describing — every export stamps its own `version`, `td_build`, and `generator`. As long as the referenced operator types exist in the current TD build, the network rebuilds cleanly.

**CI/CD integration.** The `docs/tdxn.schema.yaml` schema (draft 2020-12) validates every `.tdxn` file in CI. You can compute diff stats (operators added/removed, parameters changed), lint for forbidden patterns (absolute paths, missing help text, orphan ops), and gate merges — none of which is possible with binary `.toe`.

**Dramatically lower token cost for LLM / MCP workflows.** Reading a network via `read_tdn` (MCP tool) uses **~20-90× fewer tokens** than walking the same subtree via `get_op`+`query_network`:

- `get_op` returns all 175-219 parameters per operator wrapped in `{value, mode, label}` triples — roughly 15-25 KB per operator.
- `read_tdn` applies the same compaction as `.tdxn` export — default omission, `type_defaults`, `par_templates` — and returns the full subtree in one call.

For a 24-operator COMP (`container_left.tdxn`), the TDXN payload is ~12 KB (~3K tokens) vs an estimated ~360-480 KB (~90-120K tokens) via an equivalent `get_op` walk. The delta scales with network size and type homogeneity. A conservative 5× floor is verified in CI (`test_mcp_tdxn_tools.py`); 20-90× is the typical real-world range. See the [Claude Code skills guide](../envoy/claude-code.md) for which Envoy skill to consult and when to prefer `read_tdn` vs the runtime probes (`get_parameter`, `get_op_errors`, `get_dat_content`, etc.).

## Automatic Restoration

Embody restores externalized operators from disk when a project is opened *and the recovered `.toe` doesn't already have them*. TOX-strategy COMPs are restored from `.tox` when missing; DATs sync from their external files. TDXN behavior depends on the mode: in the default **Export-on-Save** mode the `.toe` stays authoritative and only a TDXN COMP *absent* from the `.toe` (e.g. lost to a crash) is rebuilt from its `.tdxn`; in **Roundtrip** mode children are stripped on save and fully rebuilt from `.tdxn` on open, so disk is the source of truth for those COMPs.

| Strategy | Restoration Method | Toggle |
|----------|-------------------|--------|
| **TOX** | Missing COMPs are restored from `.tox` files on disk | `Toxrestoreonstart` (ON by default) |
| **TDXN** | Children are reconstructed from `.tdxn` YAML files — **Roundtrip mode only** | `Tdxnmode = Roundtrip` + `Tdxncreateonstart` |
| **DAT** | Synced from external files via TouchDesigner's native `file` parameter | Always active |

In **Roundtrip** mode the `.toe` is kept small (children are stripped on save) and rebuilt from `.tdxn` on open, so the files on disk are the source of truth. In **Export-on-Save** mode the `.toe` keeps a complete copy of every COMP, so there's nothing to reconstruct — the `.toe` is the source of truth, and `.tdxn` files exist purely for git diff / MCP reads.

### Crash Recovery

The restoration above covers a *clean* reopen, where your last `.toe` is on disk. A **crash** is different: TouchDesigner exits before you saved, so the `.toe` rolls back to its last save and any work since is gone from it. The **Auto-Save Checkpoints** engine (ON by default) closes that gap.

A beat after the agent (or you) goes idle, Embody writes each changed TDXN COMP to disk as a frame-cheap `.tdxn` checkpoint — **~3-6 ms, with no full project save, no TDXN strip/restore, and no frame freeze**. It also fires a synchronous pre-checkpoint just before a destructive `delete_op` inside a tracked COMP. The engine is bypassed in Perform Mode and during saves, and perf-gated so a checkpoint never lands on a hot frame. `execute_python` and `exec_op_method` run arbitrary code, so neither can name the COMP it touched; rather than going unwatched they arm a *coarse* checkpoint — anything already queued is written before the code runs, and the settle-drain afterwards discovers which tracked COMPs actually changed (once per burst, not once per call).

On the next open after a crash, recovery runs even in Export-on-Save mode: any TDXN COMP that has a `.tdxn` file and a row in `externalizations.tsv` but is **missing from the recovered `.toe`** is rebuilt from its `.tdxn`. This works because `externalizations.tsv` is a `syncfile` DAT, so checkpoint rows reach disk within a frame *without* a project save. Nested TDXN children rebuild with their own content (no empty shells), and a COMP you deleted is not resurrected (its tracking row is purged on delete).

So with auto-save on, a crash costs you at most the handful of operations since the last idle settle — not the whole session. The toggle and a read-only status readout live on the Embody COMP's TDXN page; see [Configuration](configuration.md#tdxn).

## Export Portable Tox

Export any COMP as a **self-contained `.tox`** with all external file references and Embody tags stripped. The exported `.tox` works when loaded into any TouchDesigner project — no missing file errors and no Embody metadata.

For the whole **project** — inlined, Embody deleted out of the network, optionally locked with Project Privacy — see [Export Release Toe](release-toe.md).

### How it works

`ExportPortableTox()` temporarily strips all relative `file`/`syncfile` references from DATs, `externaltox`/`enableexternaltox` references from COMPs, and all Embody tags from every operator, saves the `.tox`, then restores everything. The strip/save/restore cycle is synchronous, so no timing issues arise. Author-supplied `pre_release`/`post_release` hook DATs automate the export — by default the pre-hook runs on a staged throwaway copy, so the live component is never touched — see [Script hooks](#script-hooks).

### Usage

**From the Manager UI:**

1. Click a COMP's strategy cell to open the Actions popup
2. Click **Export portable tox**
3. Choose a save location in the file dialog

**Programmatically:**

```python
op.Embody.ExportPortableTox(target=some_comp, save_path='/path/to/output.tox')
```

Both `target` and `save_path` are optional — when omitted, `target` defaults to the Embody COMP itself and `save_path` defaults to `release/{name}-v{version}.tox`. Two more optional arguments control the script hooks below: `run_hooks` (default `True`) and `hook_mode` (`'copy'` by default, `'live'` for in-place semantics).

!!! warning "Absolute paths"
    Non-system absolute paths (not starting with `/sys/`) in `file` or `externaltox` parameters are logged as warnings but **not** stripped, since they may be intentional. Check the log output after exporting to ensure portability.

### Script hooks

Component authors can run their own code around the export — the same authoring workflow popularized by the `pre_release` DAT in AlphaMoonbase's [Private Investigator](https://github.com/AlphaMoonbaseBerlin/TD_Private_Investigator), the component release manager many TouchDesigner authors already use:

- **`pre_release`** — a **Text DAT** named `pre_release` that is a **direct child** of the exported COMP. It runs on a **staged copy** of your component (in TD's cooking-disabled `/sys/quiet` area, exactly Private Investigator's model), so it shapes the artifact — reset parameters, set defaults, delete scratch/test operators — without ever touching your live component. If the script **errors for any reason** — a deliberate `raise`, a failed `assert`, or a plain bug — the export **aborts**: nothing is written, `post_release` does not run, and the staged copy is **kept** (renamed `<name>_release_failed` under `/sys/quiet`) so you can inspect exactly what your hook did. Inspect it in the same session — `/sys` is not saved with your project, so kept copies vanish on TD restart. A buggy hook can therefore never silently ship a half-prepared component; the deliberate form is just an early guard:

    ```python
    # pre_release: refuse to release in a bad state
    if parent().par.Demomode.eval():
        raise Exception('Demo mode still on -- not releasing')
    ```
- **`post_release`** — a **Text DAT** named `post_release` (also a direct child). It runs on your **live original** after the export — upload the `.tox`, notify, tag a release. It receives the outcome of the save as `args[1]`, so anything that should only happen when an artifact actually exists guards on it:

    ```python
    # post_release: only upload when the .tox was really written
    if not args[1]:
        return
    upload(args[0])
    ```

    Once `pre_release` completes, `post_release` is **guaranteed to run** — even when the save itself failed. That is deliberate, not an oversight: in `hook_mode='live'` the post-hook is the **reset** half of set/reset, so gating it on success would strand a failed export's component in its stripped release state, and it would make notify-on-failure impossible. Copy mode has no live state to reset, but the contract is identical in both modes — check `args[1]` and return early.

Inside a hook, `me` is the hook DAT and `parent()` is the COMP it runs in — the staged copy for `pre_release`, your live component for `post_release`. `args[0]` is the resolved save path; `post_release` additionally receives `args[1]` — `True` when the `.tox` was saved successfully.

!!! note "Hook code never ships"
    Both hook DATs are deleted from the staged copy before the save, so the exported `.tox` contains no release machinery — recipients get your component, not your pipeline or its credentials. (Still, keep secrets out of DATs as a rule.)

Details worth knowing:

- Hooks run **synchronously on the main thread** — keep them fast. Defer uploads and other slow I/O: a multi-MB upload inside a hook freezes TD for its duration, and blows the 30-second MCP timeout when an agent drives the export.
- Only **direct children** count, and only **Text DATs**: a Table DAT (or anything else) named `pre_release` is warned about and ignored, and a hook buried inside an embedded third-party COMP never fires for your export.
- Destroying operators in `pre_release` is safe — it happens on the throwaway copy. Your live component is never mutated by the export, and file-sync is disabled on the staged copy, so editing a synced DAT there never writes through to your source files.
- **Extensions are not initialized on the staged copy** (cooking is off in `/sys/quiet`), and parameter callbacks don't fire there. Shape the artifact with direct parameter, DAT, and operator edits; if your release prep genuinely needs extension logic, use `hook_mode='live'`.
- A `pre_release` may itself call `ExportPortableTox` on a sub-component of the staged copy; nested exports run plain (no hooks, no second copy).
- A failing `post_release` makes the call return `False` even though the `.tox` was already written — check the log to distinguish this from a failed save. The Manager UI shows a message box when an export returns `False`.
- The save path is resolved **before** hooks run; both hooks always see the final path.
- **Conditional releases are yours to define.** Embody has no notion of an "internal" versus a "public" release — put the switch on your own component and branch on it inside the hook: a `Publish` toggle, or a `Releasechannel` menu for more than two states, read with `parent().par.Publish.eval()`. A parameter also works from the **Manager UI** export, where there is no call for arguments to travel through. For a programmatic export that should run no author code at all, pass `run_hooks=False`.
- Staging costs one copy of the component (memory + a main-thread pause proportional to its size). For very large components — or when you *want* hook mutations to persist on your live comp — pass `hook_mode='live'`: hooks then run in place around the strip/save/restore cycle (**set** in `pre_release`, **reset** in `post_release`) and hook DATs ship inside the artifact, dormant. `run_hooks=False` skips hooks entirely and ships them as-is (Embody's self-updater uses this for its rollback backup). Exports whose target is — or contains — the live Embody COMP are never copy-staged: the Embody COMP itself always uses live semantics, and hooks on a container that holds it are skipped with a warning in copy mode. In the unlikely case that `/sys/quiet` is missing, the export falls back to live mode with a warning.

### Release All

`op.Embody.ReleaseAll()` — or the **Export All Release Toxes** pulse on the Embody page — exports every **releasable** component as its own portable `.tox` in one pass. Releasable means two things at once: the component is **externalized by Embody** (tracked — it's yours) *and* it **carries a release hook** (`pre_release` or `post_release` as a direct child). No other tagging, list, or setting is consulted.

Both conditions matter: hooks alone would be too eager, because third-party components arrive with their *authors'* hook DATs baked in (Private Investigator-style tools ship them inside their artifacts) — a hooks-only sweep would execute foreign release machinery. Tracked-and-hooked means "mine, and declared releasable."

- Each target runs the normal single-component export — its own copy staging, its own hooks, full artifact hygiene. A failing component is logged and skipped; the batch never halts.
- `root` scopes the scan (`None` = whole project, excluding TD system networks and Embody itself); `out_dir` overrides the default `release/` folder. Files save as `{name}.tox`; duplicate names get a numeric suffix with a warning.
- Nested releasable components release independently — their own `.tox` in addition to shipping inside any ancestor's artifact.
- Don't call it from inside a release hook (nested exports run with hooks suppressed); export an untracked component explicitly with `ExportPortableTox` when needed.

## Palette Handling During TDXN Export

When a TDXN export encounters a TD palette COMP (e.g. `abletonLink`, Widget components, anything under `Samples/Palette/`), Embody consults the `Tdxnpalettehandling` parameter on the TDXN page to decide how to handle it:

- **Ask** (default): Prompts with four buttons on first encounter of each palette COMP.
    - *Black Box* — this COMP: reference only, skip children. Decision stored on the COMP via `comp.store('_tdn_palette_handling', 'blackbox')`.
    - *Full Export* — this COMP: export all children. Decision stored on the COMP.
    - *Black Box for All*: flip the project-wide par to `Black Box`, ending future prompts.
    - *Full Export for All*: flip the project-wide par to `Full Export`.
- **Black Box**: Always reference the palette and emit `"palette_clone": true` without exporting internals. **Recommended for stock palette COMPs** — lets upstream palette updates from Derivative flow through on round-trip.
- **Full Export**: Always export all internals like a regular COMP. Use when you've heavily customized the palette internals and need that state preserved.

Per-COMP stored decisions take precedence over the project-wide par, so you can mix (most COMPs auto-use the par value; specific COMPs can override). To reset a stored decision, call `op('/path/to/palette_comp').unstore('_tdn_palette_handling')`.

Detection details and the shipped palette catalog are documented in [TDXN Palette Clones](../tdxn/specification.md#palette-clones).

## Resetting

To completely reset and remove externalizations, pulse the **Disable** button.

!!! info "Safe deletion"
    This will delete only the files that Embody created (tracked in the externalizations table). Any other files in the externalization folder will be preserved. Empty folders may be removed, but folders containing untracked files will not be touched.

Options when disabling:

- **Yes, keep Tags**: Remove externalizations but keep the tags on operators for easy re-enabling.
- **Yes, remove Tags**: Remove externalizations and all Embody tags from operators.
