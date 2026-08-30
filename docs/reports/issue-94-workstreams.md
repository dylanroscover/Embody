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

Verbatim points, with an assessment each. **Anything asserting TD API
behaviour below must be confirmed against docs.derivative.ca before acting**
(CLAUDE.md rule 6) -- these are his words plus our first read, not verified
claims.

1. **Type hinting arguments / variables / function returns.** Overlaps W6,
   which so far only covers *parameters* with a `None` default. Return
   annotations and local/variable annotations are untouched and unmeasured.
2. **Soft type-checking via `opex` and `asType` as best practices.** `opex`
   is already in `td-python.md` ("use when the operator must exist"), but only
   as an operator-access rule -- not framed as a *type-safety* practice, and
   there is no guidance on `asType` at all. Verify what `asType` is and where
   it applies before writing a rule.
3. **`iop` / `ipars`.** He flags these himself as "a bit debatable". Embody's
   referencing ladder currently does not mention them at all. Decide whether
   they belong on the ladder, and at which rung, before documenting.
4. **Dependencies -- he calls this "the big one".** Direct parameter writes
   and method calls used where a dependency object belongs. Embody already
   ships a measured storage-vs-Dependency-vs-custom-par section (added in W1),
   but it explains *what notifies*; it does not tell an author to reach for a
   dependency instead of writing a par directly. This is the gap he is naming.
   Highest-value item in W9.
5. **Building sequence parameters "takes a bit more thinking than it should".**
   A DX complaint, not a bug. Embody has sequence support in TDXN (`sequences`,
   spec v1.3) and `/parameter-design` covers custom pars. Candidate: a worked
   sequence recipe in `/parameter-design`, or a helper in `embody_pardef.py`
   alongside `ensureCustomPar`.
