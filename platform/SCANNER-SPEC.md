# FROZEN CONTRACT C8 - TDN Capability Scanner Spec

The scanner classifies the executable / side-effecting surfaces of a TDN payload and emits a
`CapabilityJson` (contract C2). It is implemented TWICE - `packages/scanner-ts` (server-side,
on submit AND download) and `dev/embody/v6/scanner.py` (Embody-side, at import). The two MUST
produce the SAME verdict + counts on the shared fixtures in `platform/packages/scanner-ts/fixtures/`
(mirrored to `dev/embody/unit_tests/fixtures/`). A TDN is executable code, not a sandboxed shader -
see plan-embody-tools-platform.md section 10. ASCII only.

## Input + bounds (DoS-safe)
- Input: a parsed TDN dict (schema: docs/tdn/specification.md, docs/tdn.schema.json - contract C7).
- Hard bounds BEFORE deep scan: reject if serialized size > 5 MB; cap AST recursion depth (Python
  `ast.parse` then a bounded NodeVisitor); cap total operators scanned. Exceeding a bound -> verdict
  `blocked` with a `size`/`depth` finding (never hang or crash the worker/import).

## Surfaces -> CapabilityCounts keys
Walk every operator (and nested COMP) in the TDN. Classify:

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

## Cross-impl agreement
`scanner-ts` and `scanner.py` run the SAME fixtures in CI and must return identical `verdict` +
`counts`. Fixtures include evasion cases (code hidden in an expression, in storage, in a nested
COMP, via dynamic attr access) - a single-surface scanner that misses these FAILS the suite.
