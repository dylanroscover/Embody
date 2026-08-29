"""The safety net for demoting promoted extension members (issue #94).

TouchDesigner promotes every CAPITALIZED member of a promoted extension --
methods and class constants alike -- onto the COMP. There is no per-member
opt-out, so the capital letter is the access modifier and the promoted set is
a public API surface. These tests exist so that surface can only shrink, and
so a demotion cannot silently break a caller.

The dangerous callers are the ones no static scan sees: DAT text living inside
the .toe (parexec DATs on tagger buttons, panel callbacks), run() strings, and
the toolbar's action column. Those fail SILENTLY -- getattr misses no-op,
hasattr guards fall back to empty. docs/reports/issue-94-caller-index.json is a
snapshot of them, taken from the live project by walking every DAT.

Off-TD by construction: pure ast + file reads, no TouchDesigner import, so this
runs on both CI legs rather than only inside a live session.
"""

from __future__ import annotations

import ast
import json
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
EMBODY_DIR = REPO / "dev" / "embody"
CALLER_INDEX = REPO / "docs" / "reports" / "issue-94-caller-index.json"

# Per-class ceiling on the promoted surface. These are a HIGH-WATER MARK, not a
# target: the assertion is <=, so demotions pass and a new promoted member
# fails. Lower a number when you demote; raising one is a deliberate decision
# to widen the public API and belongs in review, not in a drive-by.
PROMOTED_CEILING = {
    "EmbodyExt": 79,
    "ConvoyExt": 67,
    "TDXNExt": 24,
    "UpdaterExt": 18,
    "CatalogManagerExt": 7,
    "WindowHeaderExt": 7,
    "EnvoyExt": 5,
    "ToolbarExt": 5,
    "CollectionExt": 2,
}

# Test-harness classes are not part of the product's public surface.
NOT_PRODUCT = {"TestRunnerExt", "EmbodyTestCase", "AgentTestCase",
               "_ThreadManagerHarness"}


def _ext_source_files():
    return sorted(p for p in EMBODY_DIR.rglob("*Ext.py")
                  if "worktrees" not in p.parts)


def _promoted_members(path: Path):
    """Capitalized methods + class constants, per class, in one file."""
    out = {}
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name in NOT_PRODUCT:
            continue
        names = set()
        for item in node.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if item.name[:1].isupper():
                    names.add(item.name)
            elif isinstance(item, ast.Assign):
                for tgt in item.targets:
                    if isinstance(tgt, ast.Name) and tgt.id[:1].isupper():
                        names.add(tgt.id)
            elif isinstance(item, ast.AnnAssign):
                if isinstance(item.target, ast.Name) and item.target.id[:1].isupper():
                    names.add(item.target.id)
        if names:
            out.setdefault(node.name, set()).update(names)
    return out


def _all_promoted():
    merged = {}
    for path in _ext_source_files():
        for cls, names in _promoted_members(path).items():
            merged.setdefault(cls, set()).update(names)
    return merged


def _all_members(cls_name):
    """Every member of a class, at any visibility -- for resolve checks."""
    for path in _ext_source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == cls_name:
                out = set()
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        out.add(item.name)
                    elif isinstance(item, ast.Assign):
                        for tgt in item.targets:
                            if isinstance(tgt, ast.Name):
                                out.add(tgt.id)
                    elif isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                        out.add(item.target.id)
                # instance attributes assigned as self.X = ...
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Assign):
                        for tgt in sub.targets:
                            if (isinstance(tgt, ast.Attribute)
                                    and isinstance(tgt.value, ast.Name)
                                    and tgt.value.id == "self"):
                                out.add(tgt.attr)
                return out
    return set()


class TestPromotedSurfaceCeiling(unittest.TestCase):
    """The public surface may shrink. It may not grow by accident."""

    def test_A01_every_class_within_its_ceiling(self):
        promoted = _all_promoted()
        for cls, ceiling in PROMOTED_CEILING.items():
            actual = len(promoted.get(cls, ()))
            self.assertLessEqual(
                actual, ceiling,
                "{} promotes {} members, ceiling is {}. Promoting a wiring "
                "callback or a class constant is the defect issue #94 was "
                "filed about -- see the three tiers in td-python.md. If this "
                "is a deliberate public API addition, raise the ceiling here "
                "in the same commit.".format(cls, actual, ceiling))

    def test_A02_no_unknown_extension_class_escapes_the_ceiling(self):
        promoted = _all_promoted()
        unknown = sorted(set(promoted) - set(PROMOTED_CEILING))
        self.assertEqual(
            unknown, [],
            "Extension classes with a promoted surface but no ceiling: {}. "
            "Add each to PROMOTED_CEILING so its surface is tracked."
            .format(", ".join(unknown)))

    def test_A03_ceilings_are_not_stale(self):
        """A ceiling far above actual means a demotion was never recorded."""
        promoted = _all_promoted()
        slack = {c: PROMOTED_CEILING[c] - len(promoted.get(c, ()))
                 for c in PROMOTED_CEILING}
        stale = sorted(c for c, s in slack.items() if s > 5)
        self.assertEqual(
            stale, [],
            "Ceilings more than 5 above actual: {}. Lower them to the new "
            "high-water mark so the net keeps catching regressions."
            .format({c: slack[c] for c in stale}))


class TestNamespaceCollisions(unittest.TestCase):
    """Co-mounted extensions share one COMP namespace."""

    def test_B01_co_mounted_classes_do_not_collide(self):
        # The four extensions mounted on the Embody COMP itself.
        co_mounted = ["EmbodyExt", "EnvoyExt", "TDXNExt", "CatalogManagerExt"]
        promoted = _all_promoted()
        seen, clashes = {}, []
        for cls in co_mounted:
            for name in sorted(promoted.get(cls, ())):
                if name in seen:
                    clashes.append("{} on both {} and {}".format(
                        name, seen[name], cls))
                seen[name] = cls
        self.assertEqual(
            clashes, [],
            "Promoted-name collisions across co-mounted extensions: {}. TD "
            "documents no precedence for a collision, so one of the two is "
            "silently unreachable.".format("; ".join(clashes)))


class TestDocumentedApiIsPromoted(unittest.TestCase):
    """Anything we told users to call must still be callable."""

    _PATTERN = re.compile(r"\bop\.Embody\.([A-Z]\w*)")

    def _documented_names(self):
        roots = [REPO / "docs", REPO / ".claude", REPO / "README.md",
                 REPO / "CLAUDE.md", REPO / "AGENTS.md",
                 REPO / "dev" / "embody" / "Embody" / "templates"]
        names = set()
        for root in roots:
            if root.is_file():
                paths = [root]
            elif root.is_dir():
                paths = [p for p in root.rglob("*.md")
                         if "worktrees" not in p.parts]
            else:
                continue
            for p in paths:
                names.update(self._PATTERN.findall(
                    p.read_text(encoding="utf-8", errors="replace")))
        # `ext` is the extension accessor, not a promoted method.
        names.discard("Embody")
        return names

    def test_C01_documented_op_embody_calls_resolve(self):
        documented = self._documented_names()
        self.assertTrue(documented, "Found no documented op.Embody.X calls -- "
                                    "the scan is broken, not the API.")
        members = set()
        for cls in ("EmbodyExt", "EnvoyExt", "TDXNExt", "CatalogManagerExt"):
            members |= _all_members(cls)
        missing = sorted(n for n in documented if n not in members)
        self.assertEqual(
            missing, [],
            "Documented as op.Embody.X but no longer a member: {}. A user who "
            "ever read these docs -- or a generated rule file in their own "
            "project -- still has the old name.".format(", ".join(missing)))


class TestInvisibleCallSites(unittest.TestCase):
    """Callers that live only in .toe DAT text, from the WP0 caller index."""

    def _index(self):
        if not CALLER_INDEX.is_file():
            self.skipTest("caller index absent: {}".format(CALLER_INDEX))
        return json.loads(CALLER_INDEX.read_text(encoding="utf-8"))

    def test_D01_toe_only_callers_still_resolve(self):
        index = self._index()
        members = set()
        for cls in ("EmbodyExt", "EnvoyExt", "TDXNExt", "CatalogManagerExt",
                    "ToolbarExt", "WindowHeaderExt", "UpdaterExt",
                    "ConvoyExt", "CollectionExt"):
            members |= _all_members(cls)

        broken = []
        for name, sites in index.get("invisible_call_sites", {}).items():
            toe_only = [s for s in sites.get("toe_only", [])
                        if "/utils/remote/" not in s]   # third-party TauCeti
            if not toe_only or name in members:
                continue
            broken.append("{} (called from {})".format(name, toe_only[0]))

        self.assertEqual(
            broken, [],
            "Referenced from DAT text inside the .toe but absent from every "
            "extension: {}. These fail silently at runtime -- no traceback, "
            "just a dead button.".format("; ".join(sorted(broken))))


class TestToolbarActionsResolve(unittest.TestCase):
    """toolbar_config.tsv dispatches by NAME through getattr."""

    CONFIG = (REPO / "dev" / "embody" / "Embody" / "toolbar"
              / "toolbar_config.tsv")

    # ToolbarExt falls through to getattr(parent.Embody, action), which reaches
    # TD's own OP/COMP API as well as the extensions. Verified on 2025.33070:
    # op.Embody.openParameters is present and callable -- the Pars button works.
    # Off-TD we cannot introspect TD's builtins, so they are listed explicitly.
    TD_BUILTIN_ACTIONS = {"openParameters"}

    def test_E01_every_button_action_resolves(self):
        if not self.CONFIG.is_file():
            self.skipTest("toolbar_config.tsv absent")
        text = self.CONFIG.read_text(encoding="utf-8-sig")
        rows = [r.split("\t") for r in text.splitlines() if r.strip()]
        header = rows[0]
        a_idx, t_idx = header.index("action"), header.index("type")

        # Only clickable rows dispatch. A non-button row's action cell is
        # inert config -- the filter is an `input` driven by parexec_filter
        # calling OnFilterChanged, not by the click dispatcher.
        actions = sorted({
            r[a_idx].strip() for r in rows[1:]
            if len(r) > max(a_idx, t_idx)
            and r[t_idx].strip() == "button"
            and r[a_idx].strip() not in ("", "-")})
        self.assertTrue(actions, "No toolbar button actions parsed -- scan is broken.")

        resolvable = (_all_members("EmbodyExt") | _all_members("ToolbarExt")
                      | self.TD_BUILTIN_ACTIONS)
        # ToolbarExt handles its own actions as _action_<name> first.
        resolvable |= {n[len("_action_"):] for n in _all_members("ToolbarExt")
                       if n.startswith("_action_")}
        missing = sorted(a for a in actions if a not in resolvable)
        self.assertEqual(
            missing, [],
            "Toolbar button actions that resolve to nothing: {}. ToolbarExt "
            "dispatches with getattr(..., None) and no else-branch, so each is "
            "a button that does nothing when clicked.".format(", ".join(missing)))

    def test_E02_non_button_rows_declare_no_action(self):
        """Inert action cells hide dead names from the E01 census."""
        if not self.CONFIG.is_file():
            self.skipTest("toolbar_config.tsv absent")
        text = self.CONFIG.read_text(encoding="utf-8-sig")
        rows = [r.split("\t") for r in text.splitlines() if r.strip()]
        header = rows[0]
        a_idx, t_idx, n_idx = (header.index("action"), header.index("type"),
                               header.index("name"))
        offenders = ["{} (type={}, action={})".format(
            r[n_idx].strip(), r[t_idx].strip(), r[a_idx].strip())
            for r in rows[1:]
            if len(r) > max(a_idx, t_idx, n_idx)
            and r[t_idx].strip() != "button"
            and r[a_idx].strip() not in ("", "-")]
        self.assertEqual(
            offenders, [],
            "Non-button rows carrying an action the dispatcher never calls: {}. "
            "Set the cell to '-' so it cannot be mistaken for live wiring."
            .format("; ".join(offenders)))


if __name__ == "__main__":
    unittest.main()
