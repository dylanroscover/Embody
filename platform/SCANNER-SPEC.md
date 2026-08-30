# FROZEN CONTRACT C8 - TDXN Capability Scanner Spec

The scanner classifies the executable / side-effecting surfaces of a TDXN payload and emits a
`CapabilityJson` (contract C2). It is implemented TWICE - `packages/scanner-ts` (server-side,
on submit AND download) and `dev/embody/Embody/Collection/scanner.py` (Embody-side, at import). The two MUST
produce the SAME verdict + counts on the shared fixtures in `platform/packages/scanner-ts/fixtures/`
(mirrored to `dev/embody/unit_tests/fixtures/`). A TDXN is executable code, not a sandboxed shader -
see plan-embody-tools-platform.md section 10. ASCII only.

## Input + bounds (DoS-safe)
- Input: a parsed TDXN dict (schema: docs/tdxn/specification.md, docs/tdn.schema.json - contract C7).
- Hard bounds BEFORE deep scan: reject if serialized size > 5 MB; cap AST recursion depth (Python
  `ast.parse` then a bounded NodeVisitor); cap total operators scanned. Exceeding a bound -> verdict
  `blocked` with a `size`/`depth` finding (never hang or crash the worker/import).

## Surfaces -> CapabilityCounts keys
Walk every operator (and nested COMP) in the TDXN. Classify:

1. `execute_dats` - Execute-family DATs whose `dat_content` runs on create()/onStart() at import:
   types `executeDAT`, `datexecuteDAT`, `chopexecuteDAT`, `parameterexecuteDAT`, `panelexecuteDAT`
   (and the CHOP/Panel exec variants). The #1 vector. Any non-empty content counts.
2. `file_read_exprs` - parameters in `=`/`~` (expression/bind) mode whose Python reads files/does IO.
   Treat ALL `=`/`~` values as executable Python; AST-scan them (see allowlist). Count those whose
   AST references file/IO/dynamic-exec names.
3. `web_ops` - operators of IO/network types (see denylist) present anywhere.
4. `extensions` - COMPs declaring extensions (extension object + backing DAT auto-init runs
   module-level / onInitTD code). Count each extension-bearing COMP ONCE, whichever way it is
   declared: `sequences.ext` blocks OR the flat `ext<N>object` / `ext<N>name` / `ext<N>promote`
   parameters (every baseCOMP carries one such block by default, and TDXN import sets any
   parameter by name -- a plain-string `ext0object` is not `=`-prefixed, so the expression scan
   never saw it; found 2026-08-29). A block with a non-string object, or a name and no object,
   is foreign. ONE carve-out, identical on both sides: an object that is a bare dotted path
   through one of TD's OWN global shortcuts (`op.TDModules`, `op.TDResources`, `op.TDDialogs`,
   `op.TDAnnotate`, `op.TDTox`, `op.TDUpdater` -- the ALLOWLIST probed on 2025.33070, optionally
   called with `(me)`) is TD palette code and is NOT counted. It is an allowlist, not a `TD*`
   prefix: a community network can name its own shortcut `TDEvil`. The carve-out is sound only
   because community paste strips `opshortcut` on EVERY path (live and inert, nodes and
   `type_defaults`), so a pasted network can never register one of those names itself.
5. `storage_payloads` - non-empty `storage` / `startup_storage` on any operator (restored on import;
   can carry pickled/callable state).
6. `denylisted_types` - operators whose type is on the IO/network denylist (overlaps web_ops; this
   count is the raw denylisted-op tally).
7. `traversal_paths` - `file` / `syncfile` (and similar path) params holding an ABSOLUTE path or a
   `..` traversal segment -> disk read/write + SSRF/exfiltration even with zero Python.
8. `external_refs` - COMPs using `tdn_ref` / `tox_ref` (mutually exclusive with inlined `children`):
   they reference EXTERNAL .tdxn/.tox content NOT present in this payload, so it cannot be scanned.
   Legitimate inside a user's own Embody project, but a community SUBMISSION must be self-contained:
   the submit pipeline REJECTS any TDXN with `external_refs > 0` (not self-contained), and the Embody
   import side warns. Scored as `flagged` (not `blocked`) at the scanner level so own-network
   round-trips still import.

## Source scan: a pure-value ALLOWLIST for expressions, a DENYLIST for dat_content
Two different rules, and the heading used to claim both were an allowlist.

**Every `=`/`~` expression (Python side)** is admitted only if it is a PROVABLY PURE value
expression (`is_pure_value_expression`: par reads, `absTime`, `math.*`, `Par.eval()`,
arithmetic); anything not recognized counts. That is an allowlist and fails closed.

**`dat_content` (both sides), and every expression on the TS side,** is a DENYLIST:
`ast.parse` the source (TS: identifier tokens), then FLAG any of:
`eval`, `exec`, `compile`, `__import__`, `import`/`from ... import`, attribute/calls into
`os`, `sys`, `subprocess`, `socket`, `shutil`, `pathlib`, `open`, `requests`/`urllib`,
TD side-effect calls (`op(...).run`, `.save`, `.store`, `mod`, `tdu`), and dynamic attribute access
(`getattr`/`setattr`/`globals`/`locals`). Anything flagged contributes to the relevant count + a
`ScanFinding` (op_path, surface, detail, evidence<=200 chars). Unparseable source -> a finding +
treat as executable (conservative).

## Operator denylist (web_ops / denylisted_types)
SOURCE IT FROM THE LIVE TD CATALOG, do not hardcode a frozen list - the Embody-side scanner can
enumerate real IO/network op types from TD; the server-side scanner ships a snapshot (regenerated
from the catalog) under version control. Seed set (non-exhaustive): `webclientDAT`, `webserverDAT`,
`tcpipDAT`, `udpinDAT`/`udpoutDAT`, `oscinDAT`/`oscoutDAT`, `serialDAT`, `runDAT`, `executeDAT`
family, `moviefileinTOP`/`moviefileoutTOP`, `folderDAT`, `touchinTOP`/`touchoutTOP`,
`webRenderTOP`, `ndi*`, `syphonspout*`.

## Verdict rules
- `clean`   - no counts > 0.
- `flagged` - any executable/IO surface present (counts > 0) but nothing on the hard-block set.
- `blocked` - hard-block conditions: bound exceeded; or an AST surface that is unambiguously
  malicious-by-construction per the deny rules the platform enforces at SUBMIT (server may block;
  the Embody side never auto-runs - it presents the capability summary and default-inert imports).

## Which side is authoritative
`scanner-ts` is a COARSE PRE-FILTER. It gates SUBMIT and stamps the stored verdict, but it
cannot parse Python, so its expression scan is a denylist and `=op('x').destroy()` scores
`clean` there (divergence 1 below). The Embody side (`scanner.py` + `safe_import`) runs on
EVERY paste, holds the allowlist, and is the side whose verdict decides live-vs-inert. A
server `clean` is therefore never a safety claim to a user: the only capability summary a user
reads is the TD paste prompt, computed by `scanner.py`. Amended 2026-08-29 (issue #94 review);
closing divergence 1 for real means giving the TS side a recognizer for the same pure-value
grammar that fails closed on anything it cannot parse -- not a longer denylist.

## Known divergences (measured 2026-08-29, the first time the corpus was executed)

The shared fixtures now exist and BOTH suites run them. Each open divergence is declared in
its fixture's `divergence` field. The ledger check fails in BOTH directions -- a new
disagreement fails, and so does fixing one of these without deleting its note -- so a known
gap can never go quiet.

1. `expr_impure_side_effect` (`=op('target').destroy()`) -- ARCHITECTURAL.
   `scanner.py` `ast.parse`s the expression and applies an ALLOWLIST
   (`is_pure_value_expression`: anything not provably pure is counted). `scanner-ts` has no
   Python parser -- it regex-matches identifiers against a `DANGEROUS_IDENTIFIERS` DENYLIST --
   so `destroy` passes and the payload scores `clean`. The SERVER, which gates SUBMIT, is the
   permissive side. This is NOT closable by adding identifiers: the denylist is unbounded,
   which is exactly why the Python side abandoned it. Options: parse Python in TS, move the
   expression scan to a Python worker, or amend this contract to declare `scanner-ts` a coarse
   pre-filter with the Embody side authoritative -- and say so wherever users read the
   capability summary.

2. `extension_td_palette_trusted` -- CLOSED 2026-08-29. It hid two real bypasses on the way:
   the carve-out was first a `re.search` substring match (a comment containing `op.TDFunctions`
   trusted a foreign extension), then a `TD[A-Z]\w*` PREFIX match (`op.TDEvil.mod.X.X(me)` was
   trusted, and the network could declare `opshortcut: TDEvil` itself -- the strip that made the
   carve-out safe ran only inside `make_inert`, which a clean verdict skipped). The carve-out is
   now the documented allowlist under surface 4, ported to `scanner-ts`, and `opshortcut` is
   stripped on every paste path. The fixture's expectations agree and its note is gone.
   Three more divergences found by review the same day were closed with fixtures rather than
   declared: script OPs (`script_op_top`), custom-par `default`/`menuSource` expressions
   (`custom_par_default_expr`) and non-Python DAT content (`dat_non_python_glsl`).

## Cross-impl agreement
`scanner-ts` and `scanner.py` run the SAME fixtures in CI and must return identical `verdict` +
`counts` (26 fixtures, byte-mirrored between the two directories). Fixtures include evasion
cases (code hidden in an expression, in storage, in a nested COMP, via dynamic attr access, a
flat `ext0object`, an attacker-named `TDEvil` shortcut) - a single-surface scanner that misses
these FAILS the suite.
