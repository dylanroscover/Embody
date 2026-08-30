# TDXN subsystem review (2026-08-30)

Five-lens adversarial review of the TDXN implementation at `4cdbf5b` (the
v6.1.2-6.1.11 overhaul, `adf7812..68273d1`, plus everything since). Five Opus
reviewers with distinct lenses -- round-trip fidelity, lifecycle/ordering,
migration/recovery, spec/robustness, performance/concurrency -- each with
live probes in scratch containers under `/embody` and offline probes against
the real modules. Every BLOCKER and every MAJOR marked (v) below was
independently reproduced by the orchestrator before it was accepted. Not in
the mkdocs nav; internal working record, like the moonshine fidelity audit.

**Verdict: NOT fully stable.** The format and the serializer are solid (see
"Verified clean"); the *engine around them* -- dirty detection, autosave
discovery, ad-hoc export cleanup -- has three data-loss shapes that green
tests do not exercise.

## Blockers

1. **`export_network(output_file=<not the tracked path>)` deletes the COMP's
   canonical file** (v, live). `TDXNExt.ExportNetwork` computes deletion
   candidates for the root's subtree under `project.folder`, protects
   `[filepath] + _getAllTrackedTDNFiles(exclude_path=root_path)` -- i.e. every
   tracked file EXCEPT the exported COMP's own -- and `_cleanupStaleTDNFiles`
   unlinks it. `_trackTDNExport` then repoints the tsv row at the snapshot.
   Reproduced: canonical `victim.tdxn` present -> export to
   `dev/embody/snapshots/victim_snap.tdxn` -> canonical gone, row moved.
   Sibling: `_updateMovedTDNOp`'s re-export branch (`EmbodyExt.py:7442`) is
   the one `ExportNetwork(output_file=...)` call site that passes neither
   `cleanup_protected` nor `skip_cleanup`, so every tracked descendant is a
   candidate there.
2. **The dirty fingerprint misses six field classes the exporter writes** (v,
   live): DAT text, root and child `storage`, `allowCooking`, `dock`, a newly
   appended custom parameter (still at default), COMP-level `comp_inputs`.
   Each changes the export (`_tdn_content_equal` says so) while
   `_isTDNDirty` reads clean. The fingerprint is the ONLY dirty axis for a
   TDXN COMP and the coarse autosave sweep's only discovery, so an
   `execute_python` edit of a callback DAT is never checkpointed and
   `Autosavestatus` keeps reading "Saved". A real Ctrl+S re-exports
   unconditionally, so the loss window is exactly the one autosave owns.
3. **The coarse autosave sweep permanently starves roots past a fixed sorted
   prefix** (v, live + tsv). `_queueDirtyTDNRoots` takes
   `sorted(tdn_paths | fingerprint_keys)[:60]` with no rotation. The union
   re-admits `/embody/Embody`'s 43 descendants (excluded from reconstruction,
   not from this) and fingerprint keys are never popped on delete (14 dead
   keys in the dev project, riding in COMP storage). Measured: 81 keys, 21
   tracked roots, 19 live roots never examined -- every `/specimen_lab`
   specimen, `/perform`, `/marketing_lab/*`. Only ~3 of 60 slots reach a live
   non-Embody root.

## Majors

- **DAT text silently dropped when `file` points at a missing or diverged
  file** (v). `_isDATContentSavedOnDisk` returns `bool(par.file.eval())` --
  no existence or content check -- so with `Embeddatsintdns=False` (the
  default) the export omits the text and the import restores `''`. Its own
  docstring promises "on any doubt return False".
- **Custom-par `enable` / `enableExpr` / `password` never exported** (v):
  zero occurrences in `TDXNExt.py`; spec's Round-Trip Guarantees claim "all
  fields, all styles".
- **`startup_storage` is import-only** (v): the exporter never emits it
  (TD offers no read-back accessor) while the spec documents it as exported.
- **One `float('inf')` in storage aborts the whole export** (v): `int(inf)`
  raises `OverflowError`, absent from the `(TypeError, ValueError,
  RecursionError)` catch; `nan` is skipped as documented. Contradicts "never
  abort an entire export for one value".
- **Embody's own files fail Embody's shipped schema** (v): the exporter
  writes annotation `backAlpha`/`titleHeight`/`bodyFontSize` and custom-par
  `sequence`, none in `docs/tdn.schema.yaml` (`additionalProperties: false`)
  or the spec tables; 3 of 93 repo files fail validation; nothing in CI
  validates.
- **C1 clipboard envelope hash diverges between Python and TS** (v): six
  value classes serialize differently (`1.0` -> `1`, `-0.0`, `1e16`, astral
  key order, NaN/Inf); a real specimen hashes differently on each side.
  Latent only because the web never verifies the hash.
- **Unknown or empty `$t` template reference silently deletes a custom-par
  page** (v): spec says "log a warning, skip that page"; code logs nothing
  and `_flattenCustomPars` drops it.
- **`Tdncreateonstart` gate precedes export-mode crash recovery** (v):
  `reconstructTDNComps` returns before `_recoverMissingTDNComps` when the par
  is off, and `_applyTdnModeGating` greys the par out in export mode -- a
  value the user cannot see decides whether crash recovery runs, silently.
- **A tagged-but-untracked COMP mints `.tdxn` beside its surviving `.tdn`**
  (v): `_handleTDNAddition` calls `_buildTDNRelPath(oper)` with no suffix;
  after a lost tsv row the Update sweep "restores" at a different suffix,
  orphans the committed `.tdn`, can write an EMPTY network in full mode (no
  `_refusesEmptyTDNOverwrite` on that path), and `migrateToTDXN` then refuses
  the stem forever.
- **`_resolveOutputPath('auto')` ignores `externalizationsFolder` for
  non-root exports** (v): the tracked path includes the folder, the auto
  export does not, and `_trackTDNExport` repoints the row at the wrong place
  whenever `Folder` is non-empty.
- **The "no table mutation in the save window" invariant is not true**: the
  pre-save pipeline mutates the table (`Update` -> `saveTDN`,
  `_trackTDNExport`, `syncVersionIntoTDN` at post-save+5f while
  `_suppress_dialogs` is still set), and `_reBaselineCheckpoint` DROPS its
  re-baseline on that flag rather than deferring -- a stale baseline until a
  later checkpoint lands outside a save window.
- **`_refusesEmptyTDNOverwrite` makes "emptied" unreachable for automatic
  writers**: a user who deletes all children of a tracked COMP and saves
  keeps the old network on disk; in full mode the next open resurrects the
  deleted children. The dev project's `test_sandbox.tdn` is the permanent
  instance (test residue that no automatic path can clear).
- **`_removeOrphanedTDNChildren` / `_removeTDNStrategy` strand files**: rows
  deleted, files left untracked (invisible to recovery, protected from
  cleanup), and the TDXN delete is a deferred raw `unlink` with none of the
  shared-file guards its TOX sibling has. `dev/embody/base1.tdxn` is a live
  instance.
- **`checkpoint()` costs 200-390 ms of main-thread block** at 500 ops
  (docstrings say "~6ms typical"); `_autosaveDrain`, `flushPendingCheckpoints`
  (called synchronously inside every `execute_python`) and
  `_preRiskyCheckpoint` are unbudgeted. 87% of a write is YAML parsing:
  `_safe_write_tdn` parses the document three times; the disk write is ~3 ms.
- **`_onExportRefresh` batches by COUNT (200 ops/frame), not time**, and the
  completion frame does an O(N) pass -- the same defect `_queueDirtyTDNRoots`
  was rewritten to fix.
- **`ExportNetworkAsync` latches forever if `EnqueueTask` returns `None`**
  (v): `_export_state` is set before the enqueue and only a warning follows,
  so every later async export is refused as "already in progress".
- **No interlock between an in-flight async export worker and
  `onProjectPreSave`**: the worker's `os.replace` and its stale-cleanup
  (snapshot taken before the save) can land after pre-save exported, tracked
  and fingerprinted the same file; `_onExportSuccess` then tracks a COMP that
  strip has just emptied.
- **`export_network`'s documented return shape is wrong** when `output_file`
  is set (`tdn` is popped; `summary`/`note` substituted, undocumented).

## Minors and notes

Emitter not deterministic across PyYAML builds (15/93 files differ between
CSafeDumper and pure-Python on astral/PUA characters; no test pins parity);
storage keys unsorted (the tags fix's own failure class); `flags` importer
`setattr`s any name (schema pins seven); `$type`/`$value` is an undocumented
reserved pair (a user dict with those keys comes back as a `set`); annotation
alpha/title-height/font-size cannot round-trip zero; `cloneImmune` not a
flag; NaN/Inf parameter values vanish at DEBUG; `type_defaults` key order
follows encounter order; `_embody_tdn: true` accepted by Python, rejected by
TS, and the web editor never applies `isEmbodyTdnEnvelope`; `tdn_load` has no
alias/size bound; YAML 1.1 scalar traps (`012`, `0x10`, `on`) undocumented for
hand editors; MCP schema drift (`{"type":"bool","default":null}`,
`import_network.tdn` typed object while docs allow an array, ".tdn JSON"
wording); `tdn_textconv.py` (+ shipped twin) describes a `_TDN_VOLATILE_KEYS`
that no longer matches; export-mode is silent when a tracked COMP is in
neither the `.toe` nor on disk; no on-disk-newer detection at open (a pulled
`.tdn` is overwritten by the first save, the multi-user sibling of the known
export-mode revert); rename + crash before save yields a duplicated COMP on
reopen; `migrateToTDXN` is the one lifecycle path with no Embody-subtree
exclusion (would rename `Embody.tdn`, breaking two repo tools loudly);
`_recoverMissingTDNComps`' parent-restored branch skips About-page and
param-store bookkeeping; backup rotation is non-atomic across two TD
instances sharing a folder (recovery depth drops, never corrupts); full
project `rglob` is 177 ms and correctly kept off the checkpoint path;
`ImportNetwork` has no async variant (~3.9 ms/op: ~14 s for a 3.7k-op
network, exceeding the 30 s MCP window near 7.7k ops); `_updateRowCells`
docstring's "~15 ms" is now ~1.4 ms; moonshine audit findings #3 (callback
boilerplate inlined) and #4 (relative `output_file` unanchored) still open,
#1 and #2 closed; a locked non-DAT op in an explicit export opens a real
modal (documented, correct for a user, a hazard for programmatic export).

## Verified clean

Export -> import -> re-export is byte-stable (only `network_path` differs)
across parameter modes incl. mixed per-component tuplets, `==`/`~~` escapes at
any depth, expressions with quotes/newlines/unicode, every custom-par style
with ranges/sections/help/readOnly and page order, Menu default-before-names
ordering, StrMenu freeform values, `menuSource`, DAT tables with empty cells/
tabs/CRLF/unicode/numeric-looking strings, `tdn_exclude:dat_content`, sparse
fixed-connector inputs, COMP connectors, In/Out wiring through nested COMPs,
docking, all seven flags, colors/sizes/comments/tags, `$type` storage
wrappers, `options` (`max_depth`, `include_storage`). 90/93 repo files
validate against the schema; all committed files have zero dangling
`inputs`/`comp_inputs`/`dock` references. `tdn_load` is a SafeLoader (no code
execution via tags), the emitter quotes every YAML 1.1 trap it writes, LF is
pinned, `_validateOpDefs` runs before anything destructive, and the
Embody-self-destruction guard holds. All dbb69e2/adf7812 backup and migration
claims hold (rotation generations, truncated-`.bak` fallthrough, post-write
validation restore, legacy dual-read, suffix-varying finder, self-ignoring
backup dir, `migrateToTDXN(dry_run)` plan 65/0/0 on the dev project). Suffix
resolution reads the tsv row everywhere that matters. The async export worker
touches no TD object and never calls `run()`; no main-thread blocking wait
exists in the three extensions; no memory growth across 20 exports; async
chunking holds the frame at <=11.3 ms vs 253 ms synchronous; the Refresh
sweep writes zero tsv rows; `_computeTDNFingerprint` is 0.02 ms/op.

## The 52-minute stall during the review

Not a TDXN defect. The Embody log's frame counter brackets the silence:
93,391 frames over 1,557 s = 59.98 fps -- the main thread never paused.
Every TD-touching MCP call timed out at exactly 30 s while bridge-side tools
answered, then five "Orphaned response" lines landed in one frame: Envoy's
main-thread request pump (`_onRefresh`, the server task's RefreshHook) was
not being invoked. Reachable mechanisms that hold standalone Thread Manager
slots: the async-export latch above and its 300 s `done_event.wait`. Not
reproduced on demand; not attributed further.

## Method and hygiene

Reviewers were read-only on the repo, probed live only inside
`/embody/review_scratch_*` containers (untracked and deleted afterwards),
exported only to a scratchpad, and never saved the project. Leftovers found
and removed by the orchestrator: two empty scratch dirs, one stray
`cases.json`. One reviewer opened a real modal (locked-TOP export warning) on
the user's screen before switching to Embody's `_smoke_test_responses`
auto-response hook -- future live reviews seed that hook first.
