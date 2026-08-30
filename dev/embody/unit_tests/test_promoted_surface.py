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

# Dual-tier base. In TD the runner instantiates test classes with a
# sandbox kwarg that plain unittest.TestCase rejects, so the whole suite
# errored out in-TD and had only ever run under pytest. conftest supplies
# a stand-in with the same asserts off-TD.
runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase

REPO = Path(__file__).resolve().parents[3]
EMBODY_DIR = REPO / "dev" / "embody"
CALLER_INDEX = REPO / "docs" / "reports" / "issue-94-caller-index.json"

# Per-class ceiling on the promoted surface. These are a HIGH-WATER MARK, not a
# target: the assertion is <=, so demotions pass and a new promoted member
# fails. Lower a number when you demote; raising one is a deliberate decision
# to widen the public API and belongs in review, not in a drive-by.
PROMOTED_CEILING = {
    # WP4 waves 4e/4f: 57 methods demoted. What remains is the documented
    # op.Embody API plus Disable and Externalizations, which collide with
    # same-named CUSTOM PARAMETERS (Externalizations is a @property, so
    # par.X and self.X are textually identical to any mechanical rule).
    "EmbodyExt": 20,
    # WP4 waves 4b/4b-2: promoted class constants went 79 -> 5. The five that
    # remain are ConvoyExt's HOST_* states, which are DELIBERATELY mirrored as
    # module constants in convoy/convoy_client.py -- test_convoy_client reaches
    # them as client.HOST_INSTALLING on the module, so renaming only the class
    # side would split a parity contract that exists on purpose.
    # WP4 wave 4c: all 17 promoted methods demoted to lowerCamel wiring.
    "ConvoyExt": 5,
    # WP4 wave 4d: 11 wiring methods demoted. The 5 kept are the
    # documented, user-facing surface: ExportNetwork, ImportNetwork,
    # ExportNetworkAsync, DiffLiveVsDisk, DiffAllLiveVsDisk.
    "TDXNExt": 5,
    "UpdaterExt": 5,
    "CatalogManagerExt": 2,
    # WP4 wave 4a: both UI extensions demoted to zero promoted members. Every
    # caller was already a file-backed .py using .ext.<Class>., so nothing had
    # to change but the names -- which is exactly why this wave went first.
    "WindowHeaderExt": 0,
    # WP4 wave 4b-2 demoted READ_ONLY_TOOLS; the four left are the server
    # lifecycle API: RefreshRegistry, RuntimePort, Start, Stop.
    "EnvoyExt": 4,
    "ToolbarExt": 0,
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


def _promoted_instance_attrs():
    """Capitalized `self.X = ...` assignments, per class.

    TD promotes capitalized MEMBERS, not just class-body names (Extensions
    wiki: "all its capitalized methods and members are available at the
    Component level"). `self.ThreadManager = ...` in __init__ put a TD COMP
    on op.Embody outside every ceiling above (issue #94 review).
    """
    out = {}
    for path in _ext_source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef) or node.name in NOT_PRODUCT:
                continue
            for sub in ast.walk(node):
                targets = []
                if isinstance(sub, ast.Assign):
                    targets = sub.targets
                elif isinstance(sub, ast.AnnAssign):
                    targets = [sub.target]
                for tgt in targets:
                    if (isinstance(tgt, ast.Attribute)
                            and isinstance(tgt.value, ast.Name)
                            and tgt.value.id == "self"
                            and tgt.attr[:1].isupper()):
                        out.setdefault(node.name, set()).add(tgt.attr)
    return out


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


class TestPromotedSurfaceCeiling(EmbodyTestCase):
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

    def test_A04_no_capitalized_instance_attribute_is_promoted(self):
        """`self.Foo = ...` is promoted too, and no ceiling above counts it."""
        offenders = sorted("{}.{}".format(cls, n)
                           for cls, names in _promoted_instance_attrs().items()
                           for n in names)
        self.assertEqual(
            offenders, [],
            "Capitalized instance attributes are promoted onto the COMP outside "
            "every ceiling: {}. Name them _lowerCamel (they are state, not API)."
            .format(", ".join(offenders)))


class TestNamespaceCollisions(EmbodyTestCase):
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


class TestDocumentedApiIsPromoted(EmbodyTestCase):
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
                         if "worktrees" not in p.parts
                         and p.name != "changelog.md"]
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

    # Prose form: `RestoreTOXComps()` in a sentence, no receiver. C01 could not
    # see these, and eleven survived the 6.2.0 docs (issue #94 review).
    _PROSE = re.compile(r"`([A-Z]\w*)\(")

    def test_C02_prose_mentions_of_demoted_methods_are_gone(self):
        defs = set()
        for path in _ext_source_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef) and node.name not in NOT_PRODUCT:
                    for item in node.body:
                        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            defs.add(item.name)
        roots = [REPO / "docs", REPO / ".claude", REPO / "README.md",
                 REPO / "CLAUDE.md", REPO / "AGENTS.md",
                 REPO / "dev" / "embody" / "Embody" / "templates"]
        stale = []
        for root in roots:
            # README's version-history bullets are history, like the changelog.
            paths = ([root] if root.is_file() and root.name != "README.md" else
                     [p for p in root.rglob("*.md")
                      if "worktrees" not in p.parts and "reports" not in p.parts
                      and p.name not in ("changelog.md", "README.md")]
                     if root.is_dir() else [])
            for p in paths:
                text = p.read_text(encoding="utf-8", errors="replace")
                for m in self._PROSE.finditer(text):
                    name = m.group(1)
                    twin = name[:1].lower() + name[1:]
                    # Only a name whose lowerCamel twin IS a def and whose
                    # capitalized form is NOT is a demoted method.
                    if name not in defs and twin in defs:
                        stale.append("{}:{} `{}(`".format(
                            p.relative_to(REPO), text[:m.start()].count("\n") + 1, name))
        self.assertEqual(
            stale, [],
            "Prose still names a demoted method by its old capitalized form: {}. "
            "Write it as `ext.<Name>.{{lowerCamel}}()` or drop the call form."
            .format("; ".join(stale)))


class TestInvisibleCallSites(EmbodyTestCase):
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


class TestStringNamedAttributes(EmbodyTestCase):
    """Attribute names written as STRINGS survive no rename.

    Wave 4b-2 renamed 24 class constants and updated every attribute access --
    but `self._patch(self.convoy, 'API_REQUEST_MAX', 2)` passes the name as a
    string literal, so the rename missed it. The patch then wrote a key nothing
    read, the real constant kept its default, and four tests failed with value
    mismatches that pointed nowhere near the cause.
    """

    _PATTERN = re.compile(
        r"""(?:_patch|setattr|getattr|hasattr)\(\s*[^,()]+,\s*(['"])([A-Z][A-Z0-9_]{3,})\1""")

    def test_F01_string_named_constants_exist_on_some_extension(self):
        members = set()
        for path in _ext_source_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef):
                    members |= _all_members(node.name)

        missing = []
        for path in sorted((REPO / "dev" / "embody" / "unit_tests").glob("test_*.py")):
            if path.name == Path(__file__).name:
                continue  # this file QUOTES the pattern in its own docstring
            for match in self._PATTERN.finditer(
                    path.read_text(encoding="utf-8", errors="replace")):
                name = match.group(2)
                # Only judge names that LOOK like our constants: an underscored
                # twin exists, so the bare form is almost certainly a stale
                # pre-rename reference.
                if name not in members and ("_" + name) in members:
                    missing.append("{}: '{}' (did you mean '_{}'?)".format(
                        path.name, name, name))
        self.assertEqual(
            missing, [],
            "String-literal attribute names left behind by a rename: {}. These "
            "patch or probe a key nothing reads, so the test passes the wrong "
            "value silently.".format("; ".join(missing)))

    # Product code, CamelCase: `hasattr(ext, 'DirtyState')` guarding a call to
    # dirtyState() read False for every manager row after wave 4e, so the
    # manager never showed dirty state again (issue #94 review). F01 scanned
    # only the test suite and only ALL_CAPS names.
    _CAMEL = re.compile(
        r"""(?:setattr|getattr|hasattr)\(\s*[^,()]+,\s*(['"])([A-Z][a-z]\w*)\1""")

    def test_F02_string_named_members_in_product_code_exist(self):
        members = set()
        for path in _ext_source_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef):
                    members |= _all_members(node.name)
        stale = []
        for path in sorted((REPO / "dev" / "embody" / "Embody").rglob("*.py")):
            if "tests" in path.parts or "worktrees" in path.parts:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for m in self._CAMEL.finditer(text):
                name = m.group(2)
                twin = name[:1].lower() + name[1:]
                if name not in members and twin in members:
                    stale.append("{}:{} '{}' (did you mean '{}'?)".format(
                        path.name, text[:m.start()].count("\n") + 1, name, twin))
        self.assertEqual(
            stale, [],
            "Product code probes a demoted member by its OLD capitalized string: "
            "{}. hasattr reads False forever and the guarded call silently never "
            "runs.".format("; ".join(stale)))


class TestReceiverReachability(EmbodyTestCase):
    """A demoted member is unreachable ON THE COMP, only through .ext.

    The other tests here ask whether a name resolves SOMEWHERE. This one asks
    whether the RECEIVER can reach it, which is a different question and the one
    that bit us: wave 4e shipped 12 calls shaped `parent.Embody.updateHandler()`
    -- correct name, wrong receiver -- and 3432 tests passed over them, because
    every one is a UI callback (parameter pulses, manager rows) that no unit
    test exercises. It surfaced only when a real save_externalization failed
    with "'td.containerCOMP' object has no attribute 'saveTDN'".
    """

    # Genuine COMP/OP API. Reaching these on the COMP is correct.
    _COMP_API = {
        "op", "ops", "store", "unstore", "fetch", "fetchOwner", "par", "pars",
        "cook", "destroy", "copy", "create", "save", "load", "openParameters",
        "storeStartupValue", "findChildren", "evalExpression", "relativePath",
        "shortcutPath", "changeType", "resetCustomPages", "ext", "parent",
    }

    def _embody_members(self):
        promoted, private = set(), set()
        for path in _ext_source_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.ClassDef) or node.name != "EmbodyExt":
                    continue
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        (promoted if item.name[:1].isupper() else private).add(item.name)
        return promoted, private

    def test_H01_no_comp_receiver_call_to_a_demoted_member(self):
        promoted, private = self._embody_members()
        unreachable = private - self._COMP_API
        self.assertTrue(promoted, "found no promoted EmbodyExt members -- scan is broken")

        names = "|".join(sorted(map(re.escape, unreachable), key=len, reverse=True))
        # The COMP can be reached literally (parent.Embody / op.Embody), through
        # ToolbarExt's `self.embody` property, or through a local alias --
        # `emb = self.ownerComp.parent.Embody`, `e = parent.Embody`. The first
        # cut matched only the literal form and passed 11/11 over three
        # aliased call sites that shipped broken in 6.2.0 (issue #94 review).
        alias_def = re.compile(
            r"^\s*(\w+)\s*=\s*(?:[\w.]*\.)?(?:parent|op)\.Embody\s*(?:#.*)?$",
            re.MULTILINE)

        offenders = []
        for path in sorted((REPO / "dev").rglob("*.py")):
            if ("worktrees" in path.parts or ".venv" in str(path)
                    or path.name == Path(__file__).name):
                continue  # this file QUOTES the pattern in its own docstring
            text = path.read_text(encoding="utf-8", errors="replace")
            # Code only: a comment explaining the bug must not BE the bug.
            text = re.sub(r"#[^\n]*", "", text)
            receivers = {r"(?:parent|op)\.Embody", r"self\.embody"}
            receivers |= {re.escape(a) for a in alias_def.findall(text)}
            pattern = re.compile(
                r"(?<![\w.])(?:" + "|".join(sorted(receivers)) + r")\.(" + names + r")\s*\(")
            for match in pattern.finditer(text):
                offenders.append("{}:{} {}".format(
                    path.name, text[:match.start()].count("\n") + 1, match.group(1)))
        self.assertEqual(
            offenders, [],
            "Calls to a NON-promoted EmbodyExt member on the COMP: {}. TD only "
            "promotes capitalized members, so each of these raises "
            "AttributeError at runtime -- reroute through .ext.Embody."
            .format("; ".join(offenders)))


class TestSourceTextAssertions(EmbodyTestCase):
    """Tests that slice Embody's own source on a 'def Name' literal.

    This is the SILENT half of the rename problem. `src.split('def Foo', 1)[1]`
    against a renamed method either raises IndexError (loud, fine) or -- when
    the split target is merely absent from a longer string -- returns the WHOLE
    source, so the test keeps passing while asserting against a slice many times
    larger than intended. Wave 4c shipped exactly that: split('def InstallHost')
    went vacuous and nothing reported it.
    """

    # Uppercase-initial only: that is the promoted-surface class this guards.
    # Fixture code that BUILDS Python source ("def onStart") is lowerCamel
    # by TD callback convention and is not slicing Embody's own source.
    _PATTERN = re.compile(r"""['"]def ([A-Z]\w*)['"(]""")

    def test_G01_every_sliced_def_name_exists(self):
        defined = set()
        for path in _ext_source_files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    defined.add(node.name)

        missing = []
        for path in sorted((REPO / "dev" / "embody" / "unit_tests").glob("test_*.py")):
            if path.name == Path(__file__).name:
                continue
            for match in self._PATTERN.finditer(
                    path.read_text(encoding="utf-8", errors="replace")):
                name = match.group(1)
                if name not in defined:
                    missing.append("{}: 'def {}'".format(path.name, name))
        self.assertEqual(
            missing, [],
            "Tests slice source on a def that no longer exists: {}. The slice "
            "silently returns the whole file instead of the intended function, "
            "so the assertions still pass against the wrong text."
            .format("; ".join(missing)))


class TestToolbarActionsResolve(EmbodyTestCase):
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
