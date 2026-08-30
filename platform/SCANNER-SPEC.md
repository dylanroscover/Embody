# FROZEN CONTRACT C8 - TDXN Capability Scanner Spec

The scanner classifies the executable / side-effecting surfaces of a TDXN payload and emits a
`CapabilityJson` (contract C2). It is implemented TWICE - `packages/scanner-ts` (server-side,
on submit AND download) and `dev/embody/Embody/Collection/scanner.py` (Embody-side, at import). The two MUST
produce the SAME verdict + counts on the shared fixtures in `platform/packages/scanner-ts/fixtures/`
(mirrored to `dev/embody/unit_tests/fixtures/`). A TDXN is executable code, not a sandboxed shader -
see plan-embody-tools-platform.md section 10. ASCII only.

## Input + bounds (DoS-safe)
- Input: a parsed TDXN dict (schema: docs/tdn/specification.md, docs/tdn.schema.json - contract C7).
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
   module-level / onInitTD code). Count each extension-bearing COMP.
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

## AST allowlist (for dat_content AND every =/~ expression)
`ast.parse` the source, then a NodeVisitor FLAGS any of:
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

## Known divergences (measured 2026-08-29, the first time the corpus was executed)

The shared fixtures now exist and BOTH suites run them. These two do not agree yet, and each
is declared in its fixture's `divergence` field. The ledger check fails in BOTH directions --
a new disagreement fails, and so does fixing one of these without deleting its note -- so a
known gap can never go quiet.

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

2. `extension_td_palette_trusted` -- POLICY. `scanner.py` exempts an extension whose object
   resolves through a TD palette shortcut (`_is_td_palette_ref`, "TD's own trusted code").
   `scanner-ts` has no such carve-out. The `extensions` surface above says plainly "Count each
   extension-bearing COMP" with no exemption, so `scanner-ts` matches the letter of this spec
   and `scanner.py` carries an undocumented carve-out. Either document the carve-out here and
   port it, or drop it from `scanner.py`.

## Cross-impl agreement
`scanner-ts` and `scanner.py` run the SAME fixtures in CI and must return identical `verdict` +
`counts`. Fixtures include evasion cases (code hidden in an expression, in storage, in a nested
COMP, via dynamic attr access) - a single-surface scanner that misses these FAILS the suite.
