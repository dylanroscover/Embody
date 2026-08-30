# Issue #94 workstreams (W1-W9)

Tracking doc for the Function Store issue-94 arc. Not published in the mkdocs
nav -- this is an internal working list, unlike `docs/roadmap.md`, which is a
public direction document.

Status legend: **DONE** shipped / **PARTIAL** landed with a named remainder /
**OPEN** not started / **DECISION** blocked on a human call.

## Shipped in v6.2.0 (`c2476ef`)

| # | Workstream | Status |
|---|---|---|
| W1 | Rules + skills rewrite: three namespace tiers, referencing ladder, parameter ownership, and their shipped template twins | **DONE** |
| W2 | Promoted-surface census (`test_promoted_surface.py`, 11 tests, both tiers) | **DONE** |
| W3 | Freeze the cross-version update rendezvous (5 `UpdaterExt` entry points) | **DONE** |
| W4 | The demotion itself: 214 promoted members -> 43; `op.Embody` 79 -> 20 | **DONE** |
| W5 | `embody_pardef` get-or-create; 4 drifted call sites routed through it | **DONE** |

## Open

### W6 -- Type hinting

**PARTIAL.** 227 implicit-Optional parameters found (PEP 484: `x: str = None`
must be `Optional[str]`). 150 internal ones fixed. The remaining **77 live
inside `EnvoyExt._register_tools`** and define the JSON schema served to
clients and persisted in `.embody/envoy-tools-cache.json` for 86 tools.

Those 77 currently publish `{"type": "string", "default": null}` -- a schema
that claims string and defaults null. That is objectively wrong, and the
codebase already contains the correct form beside it (`Optional[Literal[...]]`
publishes a proper `anyOf` with `{"type":"null"}`). Fixing them is a
**published-contract change**; tool *names* and *parameter names* are frozen
(see `embody-code-conventions.md`), types are not, and permission rules key on
names -- so the blast radius is schema accuracy, not user permissions.

Remaining beyond Optional: `TYPE_CHECKING` stubs, an `embody_types.py`, and a
pyright leg for the Convoy host. Not started.

### W7 -- C8 scanner parity

**DONE (corpus) / DECISION (divergences).** 21 fixtures across all eight
capability surfaces plus evasion cases, run from both sides
(`scanner-ts/src/parity.test.ts`, `dev/embody/unit_tests/test_scanner_parity.py`),
mirrored between two directories with a byte-identity check, wired into
`pytest.ini` and both `bridge-tests.yml` path-filter blocks. Committed
`e197372`.

First execution found two divergences, pinned per-fixture with a ledger that
fails in both directions:

1. **`expr_impure_side_effect` -- ARCHITECTURAL, needs a decision.**
   `scanner.py` `ast.parse`s an expression and applies an allowlist;
   `scanner-ts` has no Python parser and regex-matches a denylist, so
   `=op('target').destroy()` scores `clean` on the **server that gates
   SUBMIT** and `flagged` in Embody. Not closable by appending identifiers.
   Options: parse Python in TS, move the expression scan to a Python worker,
   or amend C8 to declare `scanner-ts` a coarse pre-filter with the Embody
   side authoritative (and say so where users read the capability summary).
2. **`extension_td_palette_trusted` -- POLICY, and it concealed a live bypass.**
   Chasing this divergence found that the carve-out used `re.search`, so any
   object string *containing* `op.TD<Name>` was trusted. Proven end to end:
   `op('./Evil').module.Evil(me)  # op.TDFunctions` reported `extensions: 0`
   and `safe_import.make_inert` left the extension **enabled** on import. A
   dead `if True else op.TDModules` branch did the same. Hardened to a strict
   full match in both copies (`scanner.py`, `safe_import.py`) with three
   regression tests. The narrow policy difference remains: `scanner.py` exempts
   extensions resolving through a TD palette shortcut; `scanner-ts` does not.
   C8 says "Count each extension-bearing COMP" with no exemption.

### W8 -- Note to Wieland Hilker (PlusPlusOne)

**DRAFTED, NOT SENT.** Outward-facing correspondence; Dylan sends it. Covers
what was adopted (virtues, not code), offers to change or remove attribution,
and asks him to correct anything mischaracterized.

### W9 -- Function Store's second round (comment of 2026-08-30)

His five points, with what was done. `iop`/`ipar` and `asType` were confirmed
against docs.derivative.ca before anything was written (CLAUDE.md rule 6).

1. **Type hinting arguments / variables / function returns.** **PARTIAL.**
   Parameters are covered by W6 (150 internal fixed, 77 published MCP ones held
   back). Return annotations are measured and NOT done: **2,402 of 3,367
   functions in `dev/embody/Embody` (71%) have no return annotation.** Not
   attempted, deliberately -- inferring a return type mechanically is unsafe, and
   a sweep that guesses `-> None` wrongly is worse than no annotation. This wants
   incremental per-module work with a type checker actually running (which is the
   un-started pyright half of W6), not a scripted pass. Local/variable
   annotations unmeasured.
2. **Soft type-checking via `opex` + `asType`.** **DONE.** `td-python.md` and its
   shipped template now carry the typed one-liner
   `opex('x').asType(Type, checkType=True)`, why it cannot be `op()` (a checker
   rejects `.asType` on an `Optional[OP]`), and that `checkType=True` converts a
   silent wrong-operator bug into an immediate raise.
3. **`iop` / `ipar`.** **DONE.** Added to the referencing ladder as rung 3b, with
   the prerequisite (Internal OP / Internal OP Shortcut on the Common page), the
   inside-only resolution rule, the `op(ipar.X.Operatorpath)` gotcha for
   operator-valued internal parameters, and his own "debatable" caveat recorded
   as the real trade-off: a shortcut is configuration on the COMP, so a reader
   must leave the code to follow the reference.
4. **Dependencies -- "the big one".** **DONE.** The existing section explained
   *what notifies*; it never said when to reach for a dependency. Added
   "Publish the value; do not push it": pushing (`other.par.Value = x` per
   consumer, or a method call per consumer) has to enumerate its consumers, so it
   goes stale when a reader is added, fires whether or not the value changed, and
   inverts TD's cook model. With the one case where push is still correct --
   handing a value to something that is not dependency-aware.
5. **Sequence parameters "take more thinking than they should".** **DONE (docs).**
   A "Sequence Parameters" recipe added to `/parameter-design` and its shipped
   template: reach `comp.seq.<name>` rather than hand-building `<seq><i><par>`
   names; `numBlocks` is the idempotent get-or-create (which is what an extension
   reinitializing on every source save needs); shrinking destroys the block's
   values, expressions and exports exactly like `Par.destroy()`, so grow to fit
   and leave user-added blocks alone; and the two Embody-specific facts -- TDXN
   keys `sequences:` by BASE name with a PROBED default block count, and creates
   blocks in import Phase 2.5 before Phase 3 writes values. A code helper in
   `embody_pardef.py` was NOT added -- `numBlocks` already is the get-or-create,
   so a wrapper would add a layer without removing a decision.
