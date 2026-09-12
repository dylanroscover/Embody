# Export Release Toe

Save your **whole project** as a locked, self-contained `.toe` that ships to a customer: every externalized file inlined back into the operators that own them, Embody removed from the network entirely, and — optionally — TouchDesigner's Project Privacy applied so the networks cannot be opened.

[Export Portable Tox](externalization.md#export-portable-tox) does this for **one COMP** and puts everything back afterwards. `ExportReleaseToe` does it for the **project**, and puts nothing back: it deletes the Embody COMP and its tracking table, saves to a path you name, and quits TouchDesigner.

!!! danger "One-way. Run it in a dedicated TouchDesigner instance."
    There is no restore phase and no undo. By the time the file is written, the session that wrote it has no Embody, no `externalizations` table, no file bindings and no way back — so open a **second** TouchDesigner on the project, export from there, and let it quit. Your editing session keeps working.

    The safest second instance is one opened on a **separate checkout** of the project (a git worktree or a copy of the folder): then nothing is shared at all. A second instance on the **same** folder is supported too — from its first step the export makes its own session inert, so nothing it does reaches the folder apart from Embody's log file (see [What the export does](#what-the-export-does)) — but that instance is still a full Embody: its Envoy starts, takes another port and rewrites the shared client config until your editing instance's Envoy restarts, another AI session's tool calls could still write there, and **Autoupdate** set to `auto` could start a self-update at frame 150. Set it to `notify` and keep other sessions off the export instance.

    Your `.toe` on disk is never touched. The save takes an explicit path, resolved up front, and the readiness gate refuses the running project's own file and anything in its save series.

## Usage

### The **Export Release Toe** pulse

One parameter on the Embody page does the whole thing, and it **asks before it
does anything** -- you never land in a bare file browser wondering what you
clicked. Pulsing it:

1. **Prompts first.** Before any file dialog: what the artifact will be, that
   it is saved without Project Privacy, that this session does not survive, and
   the same inline / scrub / destroy counts the preview logs. If the project
   cannot be exported at all -- a stripped shell, an operator error, an armed
   pre-save hook -- you get that list here and the flow **dead-ends**, so you
   are never sent looking for a save location for an export that was going to
   refuse anyway.
2. **Asks where.** A Save dialog for the `.toe` path (a bare name gets `.toe`
   appended rather than bounced by the gate).
3. **Confirms against that path.** The destination-specific verdict -- an
   existing file, a save-series collision, absolute paths that would ship --
   and then **Export and Quit**.

**Cancel at step 1 or 3 is the dry run.** The plan has already been logged and
nothing has been touched, which is why there is no separate Preview parameter.

The button always exports **unlocked**. A privacy-locked build needs
`privacy_key`, and a passphrase does not belong in a parameter -- it would
persist into your `.toe` and `.embody/config.json` -- so that path stays in
Python.

!!! note "One click is one prompt"
    A TouchDesigner modal runs the frame loop while it is up, which re-delivers
    the pending pulse and re-enters the handler -- one click produced two
    stacked dialogs (measured 2026-09-12). The handler carries a re-entrancy
    guard so the second invocation is a no-op.

### From Python

```python
# Look before you leap -- reads the network, changes nothing.
op.Embody.PreviewReleaseToe(save_path='D:/builds/MyShow-1.0.0.toe')

# The real thing.
op.Embody.ExportReleaseToe(save_path='D:/builds/MyShow-1.0.0.toe',
                           privacy_key='a-long-passphrase',
                           confirm=True)
```

| Argument | Default | What it does |
|---|---|---|
| `save_path` | *required* (optional on `PreviewReleaseToe`) | Where the release `.toe` is written. A relative path is taken against the project folder. Must end in `.toe`, must not exist yet, and must not be the running project or a file in its save series. |
| `privacy_key` | `None` | Applies [Project Privacy](https://docs.derivative.ca/Privacy) with this key. **Pro licence only.** |
| `hook_name` | `'pre_release_toe'` | The Text DAT to run before the export. `PreviewReleaseToe` takes it too, so a preview looks for the same hook. |
| `quit_after` | `True` | Quit TouchDesigner once the file is on disk. |
| `confirm` | `False` | **Must be `True`.** The same guard `Uninstall` carries, for the same reason. |
| `ignore_op_errors` | `False` | Log operator errors instead of refusing on them — for dormant templates that carry cook errors they cannot cook away. |

Both return a dict. `PreviewReleaseToe` returns the whole plan; `ExportReleaseToe` returns `{'ran': False, 'reason': ...}` when it refuses, and a summary (with the plan attached) when it proceeds.

## The readiness gate

Every refusal is computed **before** anything is touched, and they are all reported at once — a release operator should not fix them one reopen at a time. The export refuses when:

- **Perform Mode is active.** Embody's pre-save pipeline is bypassed there, so the state you would ship is not the state you can see.
- **The project has never been saved.** Tracked paths are relative to `project.folder`.
- **The save path is missing, is not a `.toe`, already exists, or its folder does not exist.** An existing file would make `project.save` ask about overwriting it, in a session with nothing left to answer.
- **The save path is the running project, or sits in the project folder under the project's own save series** — `MyShow.toe` or `MyShow.99.toe` beside `MyShow.42.toe`. TouchDesigner's incremental save and Embody's own project resolution would both take that file for the newest save of your project. Release outside the project folder, or under another base name.
- **A tracked COMP is a stripped shell.** This is the gate that matters. With **Strip on Save** on, a saved `.toe` holds *empty* TDXN COMPs whose contents live only in the `.tdxn` files on disk; the [startup restores](externalization.md#automatic-restoration) refill them at frames 45-90. Export inside that window and you ship a project whose COMPs are empty — and whose `.tdxn` files are exactly what a release must not need. Every TDXN-strategy row is checked: a COMP with no children is a shell only when its `.tdxn` on disk says it should have some. One that is empty on disk as well (a `/perform` container, a test sandbox) is simply empty and passes; one whose `.tdxn` cannot be read is refused, because nothing can vouch for it.
- **The project is still starting up** (frame 120 or earlier, ninety plus margin). This one counts frames since *TouchDesigner* started rather than since the project opened, so in an instance that already had another project open it passes for free — which is fine, because the shell check above is the direct evidence and this is only the margin around it.
- **A tracked COMP is not in the network** — its table row names an operator that no longer exists.
- **Any operator has an error** — cook errors and Python tracebacks both, anywhere outside Embody's own COMP and `/local`. Pass `ignore_op_errors=True` to log them instead.
- **A privacy key was given without a Pro licence.** Refused here, at the start, because past the point of no return there is no logger left to tell you.
- **A product Execute DAT still has `projectpresave` on and there is no hook to turn it off** (see [Your own pre-save hooks](#the-pre_release_toe-hook)). With a hook present, the same check runs again after it.

## What the export does

In order, each step only after the one before it succeeded — a tracked operator that keeps its binding, or one that keeps an Embody tag, colour or storage key, stops the export before anything is destroyed:

0. **Quiets the session.** Nothing the export does from here on may reach the project folder or block on a dialog: every Embody dialog returns its default, the continuity sweep's file cleanup is switched off, `Update` and Embody's own pre- and post-save hooks are switched off, a TDXN export still in flight is cancelled, the tracking table stops syncing to its `.tsv`, and the pre-save strip and TDXN export are stood down (**File Cleanup** keep first, then `Tdxnmode` off and **Strip on Save** off). Those parameter changes are made with Embody's parameter callbacks disabled, so they are **not** written to `.embody/config.json` — the file your editing instance shares. If any of this fails, the export stops here.
1. **Inlines** every tracked externalization (below).
2. **Runs the `pre_release_toe` hook**, then destroys it. Everything after this is planned again against the network the hook left behind, and the readiness gate is re-run.
3. **Scrubs** Embody's in-network footprint and retires any Embot the AI viz left in the network.
4. **Destroys** the Embody COMP and the `externalizations` table.
5. **Applies privacy**, when a key was given.
6. **Saves** the release `.toe` and quits.

## The `pre_release_toe` hook

### Let Embody write the first draft

You do not have to start from a blank DAT. When the export refuses because a
product Execute DAT still has `projectpresave` on and there is no hook to
disarm it, the prompt offers **Create the hook for me** -- or call it directly:

```python
op.Embody.CreateReleaseToeHook()
```

It writes `pre_release_toe` as a Text DAT on your product COMP, filled in from
the same plan the preview logs:

- **real disarm lines** for every Execute DAT that blocks the export, addressed
  *relative* to the product COMP (`p.op('sources/quiesce').par.projectpresave =
  False`) so they survive a rename or a renest;
- **a checklist of the absolute paths** that would ship, as comments. Embody
  cannot guess where those assets should point, so it never generates code that
  would silently repoint someone's file;
- **commented stubs** for the usual recipe -- `performOnStart`, a version stamp,
  a demo-mode guard.

It is an ordinary Text DAT afterwards: yours to edit, and **never overwritten**.
Regenerating means deleting it first, which is deliberate -- the hook holds your
release recipe, not Embody's.


A **Text DAT** named `pre_release_toe`, a **direct child of your product COMP** — the top-level COMP that contains Embody. If Embody lives at `/myshow/lib/Embody`, the hook belongs at `/myshow/pre_release_toe`.

It runs on the real network, **after every file binding has been cleared** — so a value it stamps into a tracked DAT can never write through to the source file on disk — and before anything is scrubbed. This is where release preparation belongs: stamping a version, embedding assets, setting `project.performOnStart`, destroying runtime chains that would otherwise be baked in stale.

```python
# pre_release_toe -- args = (save_path, embody_version)
save_path, embody_version = args[0], args[1]

project.performOnStart = True
project.performWindowPath = '/perform'
parent().op('network/release_info')[1, 'version'] = '1.0.0'
parent().ext.MyShowExt.EmbedWebBundle()
parent().op('sources/quiesce').par.projectpresave = False
```

- `args[0]` is the resolved, absolute save path; `args[1]` is Embody's own version string.
- Inside the script, `me` is the hook DAT and `parent()` is the product COMP.
- **A raise aborts the export**, and the hook DAT is kept so you can see what it did. Use it as a guard: `if parent().par.Demomode.eval(): raise Exception('demo mode still on')`.
- **The hook DAT is destroyed the moment it returns.** It holds your release recipe and must never ship — which is also why its own tracking row is skipped by the inline pass. A refusal *after* the hook (the re-run gate, an armed pre-save hook) therefore finds it already gone.
- Only a **Text DAT** counts, and only a **direct child** — the same rule as [`pre_release`](externalization.md#script-hooks).
- **Destroying or emptying tracked COMPs is fine.** A TDXN COMP the hook removed, or emptied, keeps its table row, and the re-run gate knows it went on purpose.
- **Do not save, refresh or externalize from the hook.** `project.save()`, `Refresh` and the externalize calls are the writers the quiet step exists to keep away from your folder; a `project.save()` with no path writes the next increment of your dev project. The export checks the project's `.toe` on disk before and after the hook and aborts if the hook saved it.

!!! warning "Your own pre-save hooks must go"
    After the hook runs, the export scans for Execute DATs that still have `projectpresave` on. Embody's own is ignored (it dies with the COMP), but a *product* pre-save hook would fire during the release save against a project that no longer has an Embody — so the export **refuses and names them**. Destroy them, or turn `projectpresave` off, inside `pre_release_toe`. With no hook at all, the refusal is part of the readiness verdict, reported with the others before anything is touched.

## What the artifact contains

**Inlined.** Every operator the `externalizations` table tracks loses its file binding: DATs get `syncfile` off and `file = ''`, COMPs get `externaltox = ''` and `enableexternaltox` off. The *content* stays — clearing the binding detaches the file, it does not empty the operator. Any `file` or `externaltox` still bound anywhere else in the project is a binding Embody never tracked: the export logs them, and an **absolute** one is logged as a warning, because it leaks your build machine's filesystem. The preview lists them too (absolute ones marked `!`), so you can fix them before the run.

Three things are deliberately **not** inlined:

- **The `externalizations` table** — and any sibling table of that name the `Externalizations` parameter no longer links. Clearing a table DAT's file keeps its *text* — three hundred rows naming every source file in your project, baked into the customer's artifact. They are destroyed instead.
- **The `pre_release_toe` hook**, already destroyed above.
- **Everything inside the Embody COMP.** It is deleted whole, and clearing `file` on `EmbodyExt.py` would reinitialize the extension running the export.

**Scrubbed.** Embody leaves no filesystem manifest of its *in-network* footprint, so it is derived and removed operator by operator, from a census taken *after* the inline pass (clearing a tracked extension's source binding can reinitialize that extension):

- **Tags** — the externalization tags, the exclude tag, and `clone` (Embody's duplicate-path marker). Both spellings of each (`tdxn`/`tdn`, `tdxn_exclude`/`tdn_exclude`), including the per-parameter qualifiers: `tdxn_exclude:play` is one tag, and a plain membership test never sees it. Your own tags are untouched.
- **Node colours** — reset on every operator that carried an Embody tag, plus anything the table still tracks whose tag was lost.
- **Storage breadcrumbs** — `_tdn_rel_path`, `_tdn_external_wires`, `_pending_tdn_restore`, `_pending_tox_restore`, `_tdn_palette_handling`, the Embed-toggle keys `embed_dats_in_tdn` / `embed_storage_in_tdn`, and the rest of Embody's `_tdn*` runtime keys. These are written onto the COMPs themselves and a `.toe` save persists them, so a release would otherwise ship a map of where its source files used to live. Only Embody-owned key names are taken; a generic name a component of yours might also use is left alone.
- **Embot.** The AI-build visualizer's parts and colour pulses are retired, exactly as Embody's own save path retires them.

**Deleted.** The Embody COMP first, then the `externalizations` table. The table is deliberately an *undocked sibling* of the COMP — that is what makes an accidental delete of Embody recoverable — which is exactly why the release has to name it separately. (A table that lives *inside* the COMP goes with it, and is not destroyed twice.)

**Privacy.** With a `privacy_key`, `project.addPrivacy(key)` is applied to the live project immediately before the save, so the key travels into the file. The file then opens in Perform Mode only, and Shift+Esc prompts for the key. This needs a **Pro** licence and only works on a `.toe` that has none.

## What happens at the end

The last three steps — destroy, apply privacy, save and quit — run from a generated script a few frames later, **not** from Embody. They have to: the code lives in a DAT inside the COMP they delete. The script reaches only TouchDesigner globals and reports through the textport.

It is fail-closed at every seam. A target that survives its `destroy()`, a refused `addPrivacy`, or a failed `save` each stop before the next step, print what went wrong, and leave the session standing so you can look at it. Nothing is ever written half-locked. A run that gets that far has still wrecked the session, of course — quit without saving and reopen.

When the export succeeds, TouchDesigner quits. A Claude Code session whose bridge was attached to that instance reports it as a crash afterwards; that is the bridge's reading of a vanished process, nothing more.

## The dry run

`PreviewReleaseToe(save_path=...)` composes the identical plan and applies none of it: no hook, no quiet step, no inline, no scrub, nothing destroyed. It logs the readiness verdict, which hook it found and where it looked, how many operators get inlined and scrubbed, what will be destroyed, which file references would remain, and whether privacy applies. Run it first — it is the only way to see the refusal list without a session to throw away.

**Called without a path it runs as a pre-flight**: the log reads `Release .toe pre-flight -> no path chosen yet` and reports only what is true regardless of destination, so `a save path is required` is not listed as a refusal when you deliberately did not give one. That is the mode the pulse uses for its first prompt. Path-specific refusals — an existing file, a save-series collision — still appear in full once a path is given.
