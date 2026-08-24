# TDN exporter fidelity: three findings from the Moonshine upgrade

**Filed** 2026-08-24 from the Moonshine repo (TouchDesigner 2025.33070, Windows 11,
Embody 6.0.266, upgraded from 6.0.145/6.0.157). Found while auditing the re-export
that the upgrade produced. Every claim below carries file:line or an observed log
line; the one behavioural fix was verified by import, not by reading.

**One-line summary:** a stubbed default-block-count lookup makes the exporter emit
placeholder sequence blocks for input sequences, which the importer then treats as
declared-but-unconnected inputs — four reconstruction errors on every project open —
and there is no mechanism by which a project can tell the exporter that a given
operator's parameter is runtime state rather than configuration.

---

## 1. `_getDefaultSequenceBlockCount` is a stub, and its "harmless" case is not harmless

**Severity:** produces errors on every project open. Cosmetic in effect so far, but it
is noise in the one channel an operator scans when something is actually wrong.

### What breaks

Reconstructing a COMP containing multi-input `mergePOP`s logs one error per merge:

```
00:32:18  ERROR  EmbodyExt: Reconstruction error: /moonshine/warp/__template__/light/merge1:
                 Error: No input POP (/moonshine/warp/__template__/light/merge1)
   ... same for merge2, merge3, merge_pointLight
00:32:18  WARNING EmbodyExt: TDN reconstruction complete: 16 COMP(s), 4 error(s) detected
```

The network is **not** damaged — `get_op_errors` on the COMP after reconstruction
reports 0 errors, and the wiring is correct. The errors are raised during import and
then resolve. But they fire on every open, and they are indistinguishable at a glance
from a real reconstruction failure.

### Root cause

`dev/embody/Embody/TDNExt.py:3123` never looks the default up:

```python
def _getDefaultSequenceBlockCount(self, target, seq):
    """... Defaults to 1 -- most built-in sequences start with 1 block.
    The worst case of a wrong default is a redundant [{}] in the TDN output (harmless)."""
    cache_key = (target.OPType, seq.name)
    if cache_key not in self._seq_default_blocks_cache:
        self._seq_default_blocks_cache[cache_key] = 1
    return self._seq_default_blocks_cache[cache_key]
```

The cache is populated with the literal `1` on first miss and read back forever. No
op type is ever consulted, so every sequence on every operator has a default block
count of 1.

That feeds the omission test at `TDNExt.py:3107-3112`:

```python
default_count = self._getDefaultSequenceBlockCount(target, seq)
if len(blocks) == default_count and not has_any_nondefault:
    return None          # all defaults -> omit
return blocks            # -> [{}] * N
```

A `mergePOP`'s `input` sequence carries one block per wired input. Every block is at
its defaults, so `has_any_nondefault` is `False` — but `len(blocks)` is 7, not 1, so
the omission branch never fires and the exporter writes seven empty dicts:

```yaml
- name: merge1
  type: mergePOP
  sequences:
    input:
    - {}
    - {}          # x7
  inputs:
  - rectangle1
  - tube_cone1    # ...7 real inputs
```

On import, that list's **length is the numBlocks instruction** — the exporter says so
itself at `TDNExt.py:3040-3046` ("list length is the import-side numBlocks and an
empty list would mean numBlocks=0, which TD refuses"). So the importer declares seven
inputs on the sequence, while the actual wiring arrives separately from the `inputs:`
key. Between those two steps the operator has seven declared inputs and nothing
connected, and TD says so.

The block count is **derived from wiring**, not independent configuration, so
exporting it at all is a double-declaration: `inputs:` already encodes it.

### Observed correlation

Across all four merges in one file, sequence-entry count equals wired-input count
exactly:

| operator | `sequences.input` entries | wired `inputs` | all entries empty |
|---|---|---|---|
| `merge1` | 7 | 7 | yes |
| `merge2` | 2 | 2 | yes |
| `merge3` | 2 | 2 | yes |
| `merge_pointLight` | 3 | 3 | yes |

### Reproduction

Export any COMP containing a `mergePOP` with more than one input wired, then
reconstruct it. The exported file gains an all-empty `sequences: input:` block and
reconstruction logs `No input POP` for that operator.

### Fix, verified

Deleting the four all-empty `sequences: input:` blocks (22 lines, no other change)
removes the errors. Verified by importing the corrected file into a scratch
`lightCOMP` via `import_network`:

- **0 errors**, against 4 before.
- 21 operators created.
- Wiring identical: `merge1` 7 inputs, `merge2` 2, `merge3` 2, `merge_pointLight` 3.

Two candidate fixes, not mutually exclusive:

1. **Skip input-derived sequences.** When a sequence's block count is a consequence of
   operator wiring, omit it — `inputs:` is the source of truth. Narrowest fix, and it
   removes the double-declaration rather than papering over it.
2. **Make the default lookup real.** Query the op type's actual default block count
   instead of assuming 1. This is worth doing regardless: the assumption is currently
   wrong for every sequence whose natural count is not 1, and the "harmless redundant
   `[{}]`" in the docstring is what this report is about.

Note the same `[{}] * len` shape is produced deliberately for registered
runtime-status sequences at `TDNExt.py:3046`. That path is intentional; this one
reaches the same output by accident.

---

## 2. No per-operator/per-parameter export exclusion

**Severity:** systemic. This is the root of every runtime-state leak below, and it has
already been worked around by hand in at least one downstream project.

### What breaks

The exporter captures any non-default parameter value. That is correct for
configuration and wrong for runtime state, and it cannot currently tell them apart.
Three leaks observed in one repo:

| leak | operator | what got committed |
|---|---|---|
| asset path | `playback/deck_a`, `deck_b` (`moviefileinTOP`) | `C:\Users\<user>\...\assets\<id>.png` — the authoring machine's absolute path, changing per session |
| negotiated port | `moonbeam/moonbeam_out` (`MoonbeamoutTOP`) | `Port: 17361` — a value chosen at runtime by probing a port ladder because 17351 was busy |
| live UI state | `output/console` (`containerCOMP`) | window position, size, and a text DAT reading `2 fps \| 1 person connected` |

The asset-path case is already documented downstream as something a human must strip
by hand on every export, with the reason given as "it has no per-operator exclusion
mechanism" (`docs/backend/playback.md:152` in the Moonshine repo).

### Root cause

The only exclusion registry is hardcoded in Embody's own source
(`dev/embody/Embody/EmbodyExt.py:4801`):

```python
_TDN_VALUE_OMIT_PARS = {
    'Embody': frozenset({'Build', 'Date'}),
}
```

Two structural limits make it unusable by a project:

1. **Keyed by global OP shortcut** (`_registryShortcut`, `EmbodyExt.py:4806`, reads
   `comp.par.opshortcut`). Only a COMP holding a global shortcut can be addressed.
2. **COMP-scoped.** `_scrubTransientPars` (`EmbodyExt.py:4832`) walks `root` and every
   descendant **COMP** and scrubs pars on those COMPs. A parameter on a child TOP —
   which is exactly where `Port` and `file` live — cannot be reached even if its
   parent COMP were registered.

So `moonbeam` holds `opshortcut: Moonbeam`, but registering
`'Moonbeam': {'Port', 'Secureport'}` would still not cover `moonbeam_out`'s `Port`,
because that operator is a TOP inside the COMP.

There is also no project-side entry point: the dict is a class attribute in Embody's
source, so a downstream repo cannot register anything without patching Embody.

`_hasExcludeTag` exists but excludes a whole **operator** from the TDN, which is not
what is wanted here — these operators must be exported, just not with their runtime
values.

### Suggested direction

A per-parameter opt-out addressable by the project, without editing Embody. A tag or
a stored key on the operator itself would satisfy both limits above — it reaches
non-COMP operators, and it lives in the project's own network rather than Embody's
source. Something equivalent to `op.store('_tdn_omit_pars', ['file'])`, honoured by
the value exporter, would have prevented all three leaks in the table.

Whatever the mechanism, the omission must be **visible in the exported file** or in
the log. A silently omitted parameter is indistinguishable from one sitting at its
default, and the exporter is otherwise careful about exactly this distinction (see
the `_exportSequenceBlocks` zero-block warning at `TDNExt.py:3100`).

---

## 3. Callback DATs inline TouchDesigner's own boilerplate

**Severity:** bloat, not breakage.

### What breaks

The upgrade added several hundred lines of `dat_content` to committed `.tdn` files —
`refresh_exec`, `hint_callbacks`, `brand_callbacks`, `stats_callbacks`,
`webserver_tls_callbacks`. Every one is TouchDesigner's **unmodified default
template**: the `onValueChange` / `onFocus` / `onTextEdit` stubs a Text COMP ships
with, all bodies still `return`.

These are the DATs named in the log line:

```
SUCCESS EmbodyExt: Skipped externalization of 16 at-risk DAT(s):
  /moonshine/output/console/refresh_exec, .../hint_callbacks, .../brand_callbacks,
  .../stats_callbacks, ...
```

### Root cause

`_findAtRiskDats` (`dev/embody/Embody/EmbodyExt.py:10540`) flags any non-externalized
DAT with non-empty content, skipping TD-managed types — and deliberately does not skip
callback DATs (`EmbodyExt.py:10577-10581`):

```python
# Skip DATs whose content TD generates and regenerates on cook ...
# Callback DATs (execute, parexec, etc.) are intentionally absent from this set.
if dat.type in self._TD_MANAGED_DAT_TYPES:
    continue
```

That exclusion is right in principle — a callback DAT is where user code lives, and
losing it would be serious. But "has content" is standing in for "has authored
content", and an untouched TD template satisfies the first without satisfying the
second.

### Suggested direction

Compare the DAT's text against the default template for its type before classifying
it at-risk. Byte-identical to the template means nothing was authored, so there is
nothing to protect. This is strictly safer than the type-based skip that was
deliberately avoided: it protects any callback DAT the moment a single character is
edited, while keeping untouched boilerplate out of committed files.

---

## Appendix: environment

- Moonshine repo, `touch/moonshine.toe`, TouchDesigner **099.2025.33070**.
- Embody **6.0.266**; the files under audit were last exported by 6.0.145 / 6.0.157,
  so every diff here is upgrade-attributable.
- Reconstruction log: `touch/logs/moonshine.70.toe_260824.log`.
- Finding 1's fix verified live through Envoy `import_network` into a throwaway
  `lightCOMP`, which was destroyed afterwards.

### One process note

Creating and destroying a scratch COMP through Envoy triggered a bulk TDN re-export
(`TDNExt: Exported network to ...` x5 within three seconds) that overwrote unrelated
edits on disk and baked live session values into `output.tdn` and `moonbeam.tdn`.
That is finding 2 in action, but the trigger is worth knowing on its own: an
apparently read-only investigation session can rewrite committed files as a side
effect of touching the network at all.
