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
| `strategy` | Externalization strategy (`tox`, `tdn`, `py`, `txt`, etc.) |
| `node_x` | Operator X position in the network (for restoration) |
| `node_y` | Operator Y position in the network (for restoration) |
| `node_color` | Operator node color (for restoration) |

This table serves as the source of truth for what files Embody manages. Only files listed here will ever be deleted by Embody.

!!! warning
    Never edit the `externalizations.tsv` file directly. It is managed exclusively by Embody's tracking system.

## TDN Strategy

COMPs can also be externalized using the **TDN strategy** instead of `.tox`. This exports the COMP's network as human-readable YAML (`.tdn` files) instead of binary `.tox` files, enabling meaningful git diffs, code review, three-way merges, and schema-validated CI.

See [TDN Format](../tdn/index.md) for format details, and ["Why TDN"](#why-tdn) below for the concrete wins.

### TOX vs TDN: pick by what you want from the file

Both strategies externalize a COMP to its own file on disk. The difference is **what's in the file**, not whether the parent embeds it:

| | TOX | TDN |
|---|---|---|
| File format | Binary `.tox` | YAML `.tdn` |
| Git-diffable | No | Yes |
| Load speed | Fast (native TD format) | Slower (parsed and rebuilt) |
| PR review | None — binary blob | Line-by-line parameter diffs |
| Cross-build portable | TD-build-coupled | Format-versioned, portable |
| Best for | Palette widgets, third-party COMPs, anything you don't review at the parameter level | Anything you want code-reviewed, anything edited in a text editor, MCP/LLM workflows |

**Both receive the same ownership treatment in parent `.tdn` files.** When the parent of an externalized child is exported as TDN, the parent emits a reference (`tdn_ref` or `tox_ref`) and **does not embed the child's internals**. The child's own file is the source of truth. This applies symmetrically — externalizing as TOX does not mean "embed me in the parent."

If you want a parent `.tdn` that's fully self-contained (snapshot mode), pass `embed_all=True` on export. Otherwise, externalized children stay encapsulated and the parent stays small.

!!! info "If a COMP carries both tags"
    A COMP with both the TDN tag and the TOX tag is an unusual configuration — strategies are normally mutually exclusive. If it does happen (e.g. tag added by hand), **the TDN tag wins**: the parent emits `tdn_ref` and the COMP is treated as TDN-externalized. To switch a COMP between strategies, remove the old tag first.

### TDN Mode (master switch)

The `Tdnmode` parameter on the Embody COMP selects how the TDN subsystem behaves at save/open time:

| Mode | On save (++ctrl+s++) | On project open | When to pick |
|------|----------------------|-----------------|--------------|
| **Off** | No TDN activity. `.tdn` files on disk stay untouched. | No reconstruction. | Temporarily disabling TDN without deleting any files. |
| **Export-on-Save** *(default, recommended)* | Writes `.tdn` files for every tagged TDN COMP **whose content changed** since the last save (unchanged COMPs are skipped to avoid noisy git diffs from header churn). `.toe` stays the source of truth; live network is never stripped. | No reconstruction — the `.toe` already has everything. | Day-to-day work. Cheap, predictable, no round-trip risk. Ideal for git-diff / MCP workflows. |
| **Roundtrip (Experimental)** | Writes `.tdn` files **and** strips COMP children from the `.toe` so the `.toe` stays small. | Children are rebuilt from `.tdn` files at frame 60. | Large projects where the `.toe` bloats without strip, or workflows that treat `.tdn` as the primary source. May hit edge cases with extension reload timing on deeply-nested TDN COMPs. |

You can switch modes at any time — existing `.tdn` files on disk and tracked COMP entries are preserved across transitions.

!!! note "Opt-in per COMP"
    Regardless of mode, only COMPs you've explicitly tagged with Embody's TDN tag are touched. A fresh `baseCOMP` you just created is invisible to Embody until you tag it.

### Excluding a COMP from TDN (the `tdn_exclude` tag)

The `Tdnexcludetag` parameter on the Embody COMP (default value: `tdn_exclude`) defines a tag that **opts a single COMP out of the entire TDN system**. Tagged COMPs are invisible to TDN: never exported, never inlined in a parent's `.tdn`, never stripped on save, never destroyed by reconstruction.

**Primary use case: cascade-autotag bypass.** With cascade autotag enabled (`Tdncascade` parameter), tagging a parent COMP `tdn` propagates the `tdn` tag to every child in the subtree. If a specific child should *not* be externalized — typically because it's app-managed (spawned via `op.copy()` at runtime, populated from user data, or otherwise has a lifecycle outside Embody's control) — apply `tdn_exclude` to that child to keep it opted out.

**Why not just leave the tag off?** With cascade autotag on, you can't — the cascade would re-apply `tdn` on the next scan. `tdn_exclude` is the only durable opt-out.

**For app-managed copies**: when a runtime `.copy()` clones a COMP that has `tdn_exclude`, the clone inherits the tag and stays invisible to Embody. This is the recommended pattern for app-spawned content (Moonshine's `proj_<id>` projector chains, for example) — Embody won't track or interfere with the copies.

**Constraints:**

- Only COMPs are excludable. Annotation COMPs are explicitly ineligible.
- Whole-subtree exclusion only applies to a **direct child** of a TDN boundary. If you nest an excluded COMP *deeper* (under a non-excluded TDN COMP), the exclusion tag has no effect at that depth — so instead of dropping it, Embody serializes the excluded child as **ordinary content** (it round-trips and survives strip/reconstruction) and warns at export time that the tag was ignored there. The warning names the intervening COMP(s) to tag, or suggests making it a direct child, if you want the exclusion honored.
- Exclusion governs the automatic/cascade pipeline. An explicit user export call (`SaveTDN()` directly on an excluded COMP) currently still writes the `.tdn` — the opt-out applies to cascade, parent inlining, strip, and reconstruction, not to deliberate direct invocation.

### Content Safety (save-time check)

When you save a project (++ctrl+s++), Embody checks for **unprotected content** inside TDN-managed COMPs:

- **At-risk DATs** — DATs that contain content but are neither externalized (no Embody tag) nor embedded (the **Embed DATs in TDNs** parameter is OFF).
- **At-risk storage** — `comp.storage` entries on the TDN COMP or its descendants that won't be preserved when **Embed Storage in TDNs** is OFF.

DATs whose content is generated by TouchDesigner — Info DATs, Folder DAT, WebRTC DAT, Monitors DAT, device-discovery DATs, Error/Perform/Examine DATs, and similar read-only outputs — are excluded from the at-risk check. Their content is regenerated from inputs and parameters on cook, so warning that it will be lost is noise the user cannot act on. Callback DATs (Execute, CHOP Execute, DAT Execute, Panel Execute, Parameter Execute, etc.) hold user-authored Python and **continue to surface** in the warning — losing a callback silently is exactly what the check exists to prevent.

If at-risk content is found, Embody prompts you with four options:

| Button | Behavior |
|--------|----------|
| **Externalize DATs** | Tag and externalize the at-risk DATs so their content is saved to files on disk. Storage has no externalization path — enable **Embed Storage in TDNs** to preserve it. (Shown as **Continue** when only storage is at risk.) |
| **Always Externalize** | Externalize now, and do so automatically on future saves without asking. Sets `Tdndatsafety = 'externalize'`. |
| **Skip Once** | Proceed with this save. Skipped content is logged so you know exactly what was dropped. You will be prompted again next save. |
| **Always Skip** | Proceed and suppress the check on future saves. Sets `Tdndatsafety = 'ignore'` — the same opt-out described below. |

The preference is stored in the **Content Safety** parameter (`Tdndatsafety`) and can be changed at any time from the Embody COMP's TDN settings. Setting `Tdndatsafety = 'ignore'` explicitly suppresses the check entirely — an opt-in escape hatch for power users who accept the risk.

!!! tip
    To avoid this prompt entirely, either enable **Embed DATs in TDNs** / **Embed Storage in TDNs** (stores content directly in the `.tdn` file) or externalize your DATs with Embody tags before saving.

!!! warning "Locked TOPs, CHOPs, and SOPs lose their frozen data"
    TDN cannot store frozen pixel, channel, or geometry data. If your network contains locked non-DAT operators, their lock flag is preserved but their content will be **empty after reload** when using Roundtrip mode. Use **TOX strategy** instead of TDN for COMPs that contain locked TOPs, CHOPs, or SOPs. See [Lock Flag Limitation](../tdn/specification.md#lock-flag-limitation) for details.

    The save-time warning covers only locked operators the TDN export itself serializes. Locked content inside a **nested externalization boundary** — a child COMP with its own TOX or TDN tag, or an exclude-tagged subtree — is that boundary's own concern and does not trigger the parent's warning: a nested TOX-strategy COMP preserves its locked content in its own `.tox`, which is exactly the recommended remedy.

### Why TDN

TDN isn't just a different file format — it unlocks workflows that binary `.toe`/`.tox` files can't support.

**File size and density.** Even without compression, `.tdn` is comparable to or smaller than the equivalent binary `.tox` because only non-default parameters are emitted. Three compaction mechanisms kick in:

- Default omission — parameters are included only when they differ from the operator type's creation defaults.
- `type_defaults` — properties shared across every operator of a type are hoisted once to a top-level block and stripped from each operator.
- `par_templates` — repeated custom-parameter pages collapse into references.

A real leaf-component file like `envoy_toggle.tdn` is ~1.3 KB — 38 readable lines including only the ~15 parameters whose values actually differ from a `textCOMP`'s defaults.

**Git three-way merge on real conflicts.** `.toe` is binary, so git can't three-way merge it — one side wins, the other loses. `.tdn` is YAML; git merges it like any other text file, and conflicts show up as readable diffs you can resolve by reading intent:

```yaml
- name: Speed
  style: Float
<<<<<<< HEAD
  default: 1.5
=======
  default: 2.0
>>>>>>> feature/faster-playback
```

**PR review humans can actually do.** A `.toe` diff is literally `Binary files differ`. A `.tdn` parameter change is a one-line delta. Reviewers comment on specific lines, request changes, and approve — the same workflow as any other text code review.

**Cross-version portability.** `.toe` and `.tox` are coupled to the exact TD build that wrote them. `.tdn` files are format-versioned and self-describing — every export stamps its own `version`, `td_build`, and `generator`. As long as the referenced operator types exist in the current TD build, the network rebuilds cleanly.

**CI/CD integration.** The `docs/tdn.schema.yaml` schema (draft 2020-12) validates every `.tdn` file in CI. You can compute diff stats (operators added/removed, parameters changed), lint for forbidden patterns (absolute paths, missing help text, orphan ops), and gate merges — none of which is possible with binary `.toe`.

**Dramatically lower token cost for LLM / MCP workflows.** Reading a network via `read_tdn` (MCP tool) uses **~20-90× fewer tokens** than walking the same subtree via `get_op`+`query_network`:

- `get_op` returns all 175-219 parameters per operator wrapped in `{value, mode, label}` triples — roughly 15-25 KB per operator.
- `read_tdn` applies the same compaction as `.tdn` export — default omission, `type_defaults`, `par_templates` — and returns the full subtree in one call.

For a 24-operator COMP (`container_left.tdn`), the TDN payload is ~12 KB (~3K tokens) vs an estimated ~360-480 KB (~90-120K tokens) via an equivalent `get_op` walk. The delta scales with network size and type homogeneity. A conservative 5× floor is verified in CI (`test_mcp_tdn_tools.py`); 20-90× is the typical real-world range. See the [Claude Code skills guide](../envoy/claude-code.md) for which Envoy skill to consult and when to prefer `read_tdn` vs the runtime probes (`get_parameter`, `get_op_errors`, `get_dat_content`, etc.).

## Automatic Restoration

Embody restores externalized operators from disk when a project is opened *and the recovered `.toe` doesn't already have them*. TOX-strategy COMPs are restored from `.tox` when missing; DATs sync from their external files. TDN behavior depends on the mode: in the default **Export-on-Save** mode the `.toe` stays authoritative and only a TDN COMP *absent* from the `.toe` (e.g. lost to a crash) is rebuilt from its `.tdn`; in **Roundtrip** mode children are stripped on save and fully rebuilt from `.tdn` on open, so disk is the source of truth for those COMPs.

| Strategy | Restoration Method | Toggle |
|----------|-------------------|--------|
| **TOX** | Missing COMPs are restored from `.tox` files on disk | `Toxrestoreonstart` (ON by default) |
| **TDN** | Children are reconstructed from `.tdn` YAML files — **Roundtrip mode only** | `Tdnmode = Roundtrip` + `Tdncreateonstart` |
| **DAT** | Synced from external files via TouchDesigner's native `file` parameter | Always active |

In **Roundtrip** mode the `.toe` is kept small (children are stripped on save) and rebuilt from `.tdn` on open, so the files on disk are the source of truth. In **Export-on-Save** mode the `.toe` keeps a complete copy of every COMP, so there's nothing to reconstruct — the `.toe` is the source of truth, and `.tdn` files exist purely for git diff / MCP reads.

### Crash Recovery

The restoration above covers a *clean* reopen, where your last `.toe` is on disk. A **crash** is different: TouchDesigner exits before you saved, so the `.toe` rolls back to its last save and any work since is gone from it. The **Auto-Save Checkpoints** engine (ON by default) closes that gap.

A beat after the agent (or you) goes idle, Embody writes each changed TDN COMP to disk as a frame-cheap `.tdn` checkpoint — **~3-6 ms, with no full project save, no TDN strip/restore, and no frame freeze**. It also fires a synchronous pre-checkpoint just before a destructive `delete_op` inside a tracked COMP. The engine is bypassed in Perform Mode and during saves, and perf-gated so a checkpoint never lands on a hot frame. `execute_python` is deliberately not a trigger (its effects are unbounded and opaque).

On the next open after a crash, recovery runs even in Export-on-Save mode: any TDN COMP that has a `.tdn` file and a row in `externalizations.tsv` but is **missing from the recovered `.toe`** is rebuilt from its `.tdn`. This works because `externalizations.tsv` is a `syncfile` DAT, so checkpoint rows reach disk within a frame *without* a project save. Nested TDN children rebuild with their own content (no empty shells), and a COMP you deleted is not resurrected (its tracking row is purged on delete).

So with auto-save on, a crash costs you at most the handful of operations since the last idle settle — not the whole session. The toggle and a read-only status readout live on the Embody COMP's TDN page; see [Configuration](configuration.md#tdn).

## Export Portable Tox

Export any COMP as a **self-contained `.tox`** with all external file references and Embody tags stripped. The exported `.tox` works when loaded into any TouchDesigner project — no missing file errors and no Embody metadata.

### How it works

`ExportPortableTox()` temporarily strips all relative `file`/`syncfile` references from DATs, `externaltox`/`enableexternaltox` references from COMPs, and all Embody tags from every operator, saves the `.tox`, then restores everything. The strip/save/restore cycle is synchronous, so no timing issues arise.

### Usage

**From the Manager UI:**

1. Click a COMP's strategy cell to open the Actions popup
2. Click **Export portable tox**
3. Choose a save location in the file dialog

**Programmatically:**

```python
op.Embody.ExportPortableTox(target=some_comp, save_path='/path/to/output.tox')
```

Both `target` and `save_path` are optional — when omitted, `target` defaults to the Embody COMP itself and `save_path` defaults to `release/{name}-v{version}.tox`.

!!! warning "Absolute paths"
    Non-system absolute paths (not starting with `/sys/`) in `file` or `externaltox` parameters are logged as warnings but **not** stripped, since they may be intentional. Check the log output after exporting to ensure portability.

## Palette Handling During TDN Export

When a TDN export encounters a TD palette COMP (e.g. `abletonLink`, Widget components, anything under `Samples/Palette/`), Embody consults the `Tdnpalettehandling` parameter on the TDN page to decide how to handle it:

- **Ask** (default): Prompts with four buttons on first encounter of each palette COMP.
    - *Black Box* — this COMP: reference only, skip children. Decision stored on the COMP via `comp.store('_tdn_palette_handling', 'blackbox')`.
    - *Full Export* — this COMP: export all children. Decision stored on the COMP.
    - *Black Box for All*: flip the project-wide par to `Black Box`, ending future prompts.
    - *Full Export for All*: flip the project-wide par to `Full Export`.
- **Black Box**: Always reference the palette and emit `"palette_clone": true` without exporting internals. **Recommended for stock palette COMPs** — lets upstream palette updates from Derivative flow through on round-trip.
- **Full Export**: Always export all internals like a regular COMP. Use when you've heavily customized the palette internals and need that state preserved.

Per-COMP stored decisions take precedence over the project-wide par, so you can mix (most COMPs auto-use the par value; specific COMPs can override). To reset a stored decision, call `op('/path/to/palette_comp').unstore('_tdn_palette_handling')`.

Detection details and the shipped palette catalog are documented in [TDN Palette Clones](../tdn/specification.md#palette-clones).

## Resetting

To completely reset and remove externalizations, pulse the **Disable** button.

!!! info "Safe deletion"
    This will delete only the files that Embody created (tracked in the externalizations table). Any other files in the externalization folder will be preserved. Empty folders may be removed, but folders containing untracked files will not be touched.

Options when disabling:

- **Yes, keep Tags**: Remove externalizations but keep the tags on operators for easy re-enabling.
- **Yes, remove Tags**: Remove externalizations and all Embody tags from operators.
