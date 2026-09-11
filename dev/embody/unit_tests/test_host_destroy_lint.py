"""Host-destroy guard, pure tier (issue #110).

A single execute_python that destroyed the Embody COMP hosting Envoy -- from
inside Envoy's own request -- hung TouchDesigner for minutes. Envoy now
refuses that shape before anything runs:

- envoy_guard.host_destroy_findings: the static execute_python lint. Every
  case here resolves receivers through a fake resolver, so no TouchDesigner
  is involved and the real Embody is never a target.
- EnvoyExt's inline structured guard (_hostDestroyRefusal) and refusal
  texts, driven on a fake `self`.
- EnvoyExt's runtime backstop (_execute_python, _send_response, _onRefresh)
  with TD's globals planted on a private copy of the module.

The in-TD wiring (sandbox stand-in host, the real host chain read-only)
lives in test_envoy_tool_guards.py.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
import time
from pathlib import Path
from queue import Queue
from threading import Event, Lock
from types import SimpleNamespace


_EMBODY_DIR = Path(__file__).resolve().parents[1] / "Embody"


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, _EMBODY_DIR / filename)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


GUARD = _load("host_destroy_guard_under_test", "envoy_guard.py")
ENVOY = _load("host_destroy_envoy_under_test", "EnvoyExt.py")
findings = GUARD.host_destroy_findings

# A fake project: Embody at /a/Embody, its extension DAT inside it.
HOST = "/a/Embody"
EXT_DAT = "/a/Embody/EnvoyExt"
GUARDED = {HOST, "/a", "/", EXT_DAT}
CHAIN = [HOST, "/a", "/", EXT_DAT]
MAP = {
    "op.Embody": HOST, "me": HOST, "parent()": HOST,
    "parent.Embody": HOST, "op('..')": HOST, "parent(2)": "/a",
    "op('/a')": "/a", "opex('/a')": "/a", "root": "/", "op('/')": "/",
    "op.Embody.parent()": "/a", "me.parent()": "/a",
    "op.Embody.op('tagger')": HOST + "/tagger", "op('/p/n1')": "/p/n1",
    "op('.')": EXT_DAT, "op.Embody.op('EnvoyExt')": EXT_DAT,
}


def resolve(node):
    return MAP.get(ast.unparse(node))


def lint(source):
    return findings(source, resolve, GUARDED)


def targets(source):
    return [f["target"] for f in lint(source)]


# --- must flag -----------------------------------------------------------

def test_op_shortcut_destroy_flagged():
    got = lint("op.Embody.destroy()")
    assert got == [{"line": 1, "call": "op.Embody.destroy()", "target": HOST}]


def test_me_and_parent_forms_flagged():
    """me and parent() ARE the Embody COMP inside execute_python (probe)."""
    for source in ("me.destroy()", "parent().destroy()",
                   "parent.Embody.destroy()", "op('..').destroy()"):
        assert targets(source) == [HOST], source


def test_ancestors_and_root_flagged():
    for source, want in (("parent(2).destroy()", "/a"),
                         ("op('/a').destroy()", "/a"),
                         ('op("/a").destroy()', "/a"),
                         ("opex('/a').destroy()", "/a"),
                         ("root.destroy()", "/"),
                         ("op('/').destroy()", "/"),
                         ("op.Embody.parent().destroy()", "/a"),
                         ("me.parent().destroy()", "/a")):
        assert targets(source) == [want], source


def test_envoyext_dat_destroy_flagged():
    """The DAT whose code is on the stack during the exec."""
    assert targets("op('.').destroy()") == [EXT_DAT]
    assert targets("op.Embody.op('EnvoyExt').destroy()") == [EXT_DAT]


def test_alias_forms_flagged():
    assert lint("e = op.Embody\ne.destroy()")[0]["line"] == 2
    assert targets("e: COMP = op.Embody\ne.destroy()") == [HOST]
    assert targets("a = op.Embody\nb = a\nb.destroy()") == [HOST]
    assert targets("(e := op.Embody).destroy()") == [HOST]
    assert targets("e = me.parent()\ne.destroy()") == ["/a"]


def test_every_destroy_method_flagged():
    for method in ("destroy", "reload", "changeType", "progressiveUnload"):
        source = "op.Embody.%s(x)" % method
        assert targets(source) == [HOST], source


def test_reload_and_clone_pulses_flagged():
    for par in ("enableexternaltoxpulse", "reinitnet", "enablecloningpulse"):
        assert targets("op.Embody.par.%s.pulse()" % par) == [HOST], par
        assert targets("op.Embody.par[%r].pulse()" % par) == [HOST], par


def test_pulse_par_write_flagged():
    """Whether a write pulses is unverified; treated as one, like
    set_parameter's structured check."""
    assert targets("op.Embody.par.enableexternaltoxpulse = 1") == [HOST]
    assert targets("op.Embody.par.reinitnet.val = True") == [HOST]


def test_getattr_destroy_flagged():
    assert targets("getattr(op.Embody, 'destroy')()") == [HOST]


def test_directly_called_def_flagged():
    got = lint("def go():\n    op.Embody.destroy()\ngo()")
    assert [(f["line"], f["target"]) for f in got] == [(2, HOST)]


def test_immediately_invoked_lambda_flagged():
    assert targets("(lambda: op.Embody.destroy())()") == [HOST]


def test_zero_delay_run_still_flagged():
    """Queueing of a zero-delay or endFrame run is undocumented."""
    for source in ('run("op.Embody.destroy()")',
                   "run('op.Embody.destroy()', delayFrames=0)",
                   "td.run(lambda: op.Embody.destroy(), endFrame=True)",
                   "run('op.Embody.destroy()', delayFrames=n)",
                   "def go():\n    op.Embody.destroy()\nrun(go)"):
        assert targets(source) == [HOST], source


def test_short_millisecond_delay_still_flagged():
    """delayMilliSeconds rounds to the nearest frame, so 10ms can be 0."""
    assert targets("run('op.Embody.destroy()', delayMilliSeconds=10)") == [HOST]


def test_delayed_run_argument_is_evaluated_now():
    """Only the script is deferred; the other arguments run immediately."""
    assert targets("run('args[0]', op.Embody.destroy(), delayFrames=5)") == [HOST]


def test_issue_110_reporter_sequence_flagged():
    source = ("op.Embody.destroy()\n"
              "c = op('/').create(baseCOMP, 'Embody')\n"
              "c.par.externaltox = 'snap.tox'\n"
              "c.par.reinitnet.pulse()\n")
    got = lint(source)
    assert got[0]["line"] == 1
    assert got[0]["call"] == "op.Embody.destroy()"
    assert len(got) == 1, "the new COMP's pulse is not the host"


def test_oversized_source_with_a_token_is_still_checked():
    padding = "# padding line to push the source past the old cap\n" * 4000
    source = padding + "op.Embody.destroy()\n"
    assert len(source) > 131072
    assert targets(source) == [HOST]


def test_rebinding_a_builtin_lookup_anywhere_does_not_hide_it():
    """Review find: `op = ...` anywhere dropped op's builtin meaning, so the
    reporter's exact op.Embody.destroy() slipped through."""
    for source in ("def helper(x):\n    op = x\n    return op\nop.Embody.destroy()",
                   "op.Embody.destroy()\nop = None",
                   "class K:\n    def m(self):\n        op = 2\nop.Embody.destroy()",
                   "parent = 3\nparent.Embody.destroy()",
                   "op = 1\nop.Embody.parent().destroy()"):
        assert targets(source), source


def test_run_scripts_held_by_a_name_or_bound_method_flagged():
    """Zero-delay runs stay in scope, so every natural spelling of the
    script must be read, not only a literal at the call."""
    for source in ("run(op.Embody.destroy)",
                   "d = op.Embody.destroy\nd()",
                   "d = op.Embody.destroy\nrun(d)",
                   'code = "op.Embody.destroy()"\nrun(code)',
                   'run(f"op.Embody.destroy()")',
                   'code = f"op.Embody.destroy()"\nrun(code)',
                   "f = lambda: op.Embody.destroy()\nrun(f)"):
        assert targets(source) == [HOST], source


def test_deferred_or_dynamic_run_scripts_not_flagged():
    for source in ('code = "op.Embody.destroy()"\nrun(code, delayFrames=30)',
                   "run(op.Embody.destroy, delayFrames=5)",
                   "d = op.Embody.destroy",
                   "d = op.Embody.cook\nd()",
                   "run(op('/p/n1').destroy)",
                   'p = "/a/Embody"\nrun(f"op(\'{p}\').destroy()")'):
        assert lint(source) == [], source


def test_the_binding_nearest_the_use_is_tried_first():
    """Only _MAX_BINDINGS bindings of a name are followed per hop; the one
    just before the use must be among them, however many came earlier or
    later."""
    rebinds = "".join("n = op('/p/n1')\nn.cook()\n" for _ in range(12))
    assert targets(rebinds + "n = op.Embody\nn.destroy()") == [HOST]
    assert targets("n = op.Embody\nn.destroy()\n" + rebinds) == [HOST]


def test_flow_insensitive_aliasing_errs_toward_refusal():
    """Documented trade-off: any binding counts, so a name once bound to
    the host is refused even after a rebind the code relies on."""
    assert targets("t = root\nt = op('/p/n1')\nt.destroy()") == ["/"]


def test_alias_cycles_stay_linear():
    """Review find: an alias fan-out that never reaches a lookup yields
    nothing, so the yield caps never tripped -- up to 8^4 steps per call,
    5 s for 2000 calls in an a/b cycle. Dead ends are now walked once per
    pass; the pruning is exact, so the host behind one is still found."""
    cycle = "a = b\nb = a\n" * 8
    real, steps = GUARD._expand, []

    def counting(*args, **kwargs):
        steps.append(1)
        return real(*args, **kwargs)

    GUARD._expand = counting
    try:
        assert lint(cycle + "a.destroy()\n" * 2000) == []
    finally:
        GUARD._expand = real
    # Counted, never timed: CI runners stall (commit-push-checklist). About
    # 9 steps per call measured; the old walk took up to 8^4 per call.
    assert len(steps) <= 25 * 2000, len(steps)
    # Every call here sees its own binding window. Counted, not timed:
    # the old walk took ~9,400 _expand steps per call on this shape.
    interleaved = "".join("a = b.op('x%d')\nb = a.parent()\na.destroy()\n" % i
                          for i in range(300))
    real, steps = GUARD._expand, []

    def counting(*args, **kwargs):
        steps.append(1)
        return real(*args, **kwargs)

    GUARD._expand = counting
    try:
        assert lint(interleaved) == []
    finally:
        GUARD._expand = real
    assert len(steps) <= 100 * 300, len(steps)
    assert targets(cycle + "e = op.Embody\ne = a\ne.destroy()") == [HOST]


def test_tuple_unpacked_alias_flagged():
    # Review find: `host, x = op.Embody, 1` bound nothing.
    assert targets("host, x = op.Embody, 1\nhost.destroy()") == [HOST]
    assert targets("[a, b] = [op('/p/n1'), op.Embody]\nb.destroy()") == [HOST]


def test_td_module_prefix_flagged():
    # Review find: td.op / td.parent / td.root spell the same lookups.
    assert targets("td.op.Embody.destroy()") == [HOST]
    assert targets("td.op('/a').destroy()") == ["/a"]
    assert targets("td.root.destroy()") == ["/"]


def test_called_def_parameters_are_bound():
    # Review find: def rm(o): o.destroy() then rm(op.Embody) hid the host.
    assert targets("def rm(o):\n    o.destroy()\nrm(op.Embody)") == [HOST]
    assert targets("def rm(o):\n    o.destroy()\nrm(o=op.Embody)") == [HOST]
    assert targets("def rm(o):\n    o.destroy()\nrm(op('/p/n1'))") == []


def test_each_distinct_lookup_is_resolved_once_per_pass():
    calls = []

    def recorder(node):
        calls.append(ast.unparse(node))
        return resolve(node)

    source = "op('/p/n1').destroy()\n" * 50 + 'run("op(\'/p/n1\').destroy()")'
    assert findings(source, recorder, GUARDED) == []
    assert calls == ["op('/p/n1')"]


# --- must not flag -------------------------------------------------------

def test_delayed_run_not_flagged():
    for source in ('run("op.Embody.destroy()", delayFrames=30)',
                   "run(lambda: op.Embody.destroy(), delayFrames=1)",
                   "td.run('op.Embody.destroy()', delayMilliSeconds=500)",
                   "def go():\n    op.Embody.destroy()\nrun(go, delayFrames=5)"):
        assert lint(source) == [], source


def test_unrelated_and_descendant_destroys_not_flagged():
    assert lint("op('/p/n1').destroy()") == []
    assert lint("op.Embody.op('tagger').destroy()") == []


def test_unresolvable_receivers_not_flagged():
    for source in ("for o in op.Embody.children:\n    o.destroy()",
                   "foo().destroy()", "ops('*')[0].destroy()",
                   "op(path).destroy()", "op.Embody.ext.Embody.destroy()"):
        assert lint(source) == [], source


def test_importlib_reload_not_flagged():
    assert lint("import importlib\nimportlib.reload(mod)") == []


def test_code_that_does_not_run_now_not_flagged():
    for source in ("def later():\n    op.Embody.destroy()",
                   "f = lambda: op.Embody.destroy()",
                   "class K:\n    def m(self):\n        op.Embody.destroy()"):
        assert lint(source) == [], source


def test_unparseable_and_non_str_source_not_flagged():
    assert lint("op.Embody.destroy( (") == []
    assert findings(None, resolve, GUARDED) == []
    assert findings(b"op.Embody.destroy()", resolve, GUARDED) == []


def test_token_free_source_never_calls_the_resolver():
    calls = []

    def recorder(node):
        calls.append(node)
        return HOST

    assert findings("result = op.Embody.path", recorder, GUARDED) == []
    assert calls == []


def test_resolver_only_receives_whitelisted_lookups():
    seen = []

    def recorder(node):
        assert GUARD._is_lookup(node), ast.unparse(node)
        seen.append(ast.unparse(node))
        return resolve(node)

    source = ("x().destroy()\nop.Embody.ext.foo.destroy()\n"
              "op(path).destroy()\nop(open('f').read()).destroy()\n"
              "op.Embody.destroy()\n")
    got = findings(source, recorder, GUARDED)
    assert [f["line"] for f in got] == [5]
    assert seen == ["op.Embody"]


def test_resolver_fault_fails_open():
    def broken(node):
        raise RuntimeError("resolver down")

    assert findings("op.Embody.destroy()", broken, GUARDED) == []


# --- refusal texts and the structured guard (EnvoyExt, pure) -------------

def _ascii(text):
    return all(ord(ch) < 128 for ch in text)


def test_guard_sets_match_envoyext():
    assert GUARD.DESTROY_METHODS == ENVOY._HOST_DESTROY_METHODS
    assert GUARD.RELOAD_PULSES == ENVOY._HOST_RELOAD_PULSES


def test_envoyext_prefilter_covers_every_flaggable_name():
    """EnvoyExt skips envoy_guard for token-free code; every call the lint
    can flag names one of these, so the prefilter never hides a finding."""
    names = GUARD.DESTROY_METHODS | GUARD.RELOAD_PULSES
    assert set(ENVOY._HOST_LINT_TOKENS) == {n.lower() for n in names}
    assert all(GUARD.has_destroy_token(token)
               for token in ENVOY._HOST_LINT_TOKENS)


def test_host_relation_labels():
    rel = ENVOY._host_relation
    assert rel(HOST, CHAIN) == "the Embody COMP that Envoy runs inside"
    assert "project root" in rel("/", CHAIN)
    assert "extension DAT" in rel(EXT_DAT, CHAIN)
    assert rel("/a", CHAIN) == ("an ancestor of the Embody COMP (/a/Embody) "
                                "that Envoy runs inside")


def test_structured_shapes():
    shape = ENVOY._host_destroy_shape
    assert shape("delete_op", {"op_path": "/x"}) == ("/x", "delete_op")
    assert shape("exec_op_method", {"op_path": "/x", "method": "destroy"}) \
        == ("/x", "destroy()")
    assert shape("exec_op_method", {"op_path": "/x", "method": "cook"}) is None
    assert shape("exec_op_method", {"op_path": "/x", "method": ["destroy"]}) is None
    assert shape("set_parameter", {"op_path": "/x",
                                   "par_name": "Enableexternaltoxpulse"})[0] == "/x"
    assert shape("set_parameter", {"op_path": "/x", "par_name": "tx"}) is None
    assert shape("set_parameter", {"op_path": "/x", "par_name": None}) is None
    assert shape("delete_op", {"op_path": None}) is None
    assert shape("delete_op", {}) is None
    assert shape("delete_op", None) is None
    assert shape("get_op", {"op_path": "/x"}) is None
    assert shape("import_network", {"target_path": "/x", "clear_first": True}) \
        == ("/x", "import_network(clear_first=True)")
    assert shape("import_network", {"target_path": "/x", "clear_first": False}) is None
    assert shape("import_network", {"target_path": "/x"}) is None


def test_structured_refusal_texts_carry_no_destroy_recipe():
    for what, purges in (("delete_op", True), ("destroy()", False),
                         ("setting reinitnet", False)):
        text = ENVOY._host_refusal_text(what, "/a",
                                        ENVOY._host_relation("/a", CHAIN),
                                        purges)
        assert _ascii(text), text
        assert text.startswith("HOST-DESTROY REFUSED (nothing changed)")
        assert "override=True does not bypass" in text
        assert "ask the user" in text
        assert "issue #110" in text
        assert "run(" not in text, "no copy-paste way around the guard"
        assert ("externalization tracking" in text) == purges


def test_python_refusal_text():
    text = GUARD.python_refusal_text(
        {"line": 3, "call": "op('\u00e9').destroy()", "target": HOST},
        ENVOY._host_relation(HOST, CHAIN))
    assert _ascii(text)
    for needle in ("nothing ran", "line 3", "me and parent()",
                   "run(code, delayFrames=30)", "op.Embody.ext.Envoy.Stop()",
                   "first line inside that string", "save first (save_project)",
                   "issue #110",
                   # every binding counts, so a reused name is refused too
                   "variable you reassigned", "distinct names"):
        assert needle in text, needle
    # Stop() is a line INSIDE the deferred string, never a separate call:
    # run on its own it cuts the agent's channel before the run() is sent.
    assert text.index("run(code, delayFrames=30)") < text.index("Stop()")


def test_refusals_match_no_recovery_rule_and_keep_their_code():
    """error_code is set directly; no rule may add a stray hint (for
    example 'unsaved' or 'multi-session gate')."""
    texts = [ENVOY._host_refusal_text("delete_op", "/", "the root", True),
             ENVOY._host_refusal_text("destroy()", HOST, "the host", False),
             GUARD.python_refusal_text(
                 {"line": 1, "call": "me.destroy()", "target": HOST}, "x")]
    for text in texts:
        assert ENVOY._recovery_hints_for(text) == [], text
        env = ENVOY._host_refusal(text, HOST)
        ENVOY.EnvoyExt._attachRecoveryHints(None, env)
        assert env["error_code"] == "envoy.embody.host_destroy_refused"
        assert "recovery_hints" not in env


class _FakeOp:
    def __init__(self, path, parent=None, children=None):
        self.path = path
        self.valid = True
        self._parent = parent
        self._children = children or {}

    def parent(self):
        return self._parent

    def op(self, name):
        return self._children.get(name)

    def destroy(self):
        self.valid = False


def _guard_self(known=(HOST, "/a", "/", EXT_DAT, HOST + "/tagger", "/p/n1"),
                resolve_op=None):
    logs = []
    fake = SimpleNamespace(logs=logs)
    fake._log = lambda message, level="INFO": logs.append((level, message))
    fake._resolve_op = resolve_op or (
        lambda path: _FakeOp(path) if path in known else None)
    fake._hostChain = lambda: list(CHAIN)
    fake._hostDestroyRefusal = (
        lambda operation, params:
        ENVOY.EnvoyExt._hostDestroyRefusal(fake, operation, params))
    return fake


def test_structured_guard_refuses_the_host_chain():
    fake = _guard_self()
    refuse = ENVOY.EnvoyExt._hostDestroyRefusal
    for operation, params in (
            ("delete_op", {"op_path": "/a"}),
            ("delete_op", {"op_path": "/", "override": True}),
            ("delete_op", {"op_path": EXT_DAT}),
            ("exec_op_method", {"op_path": HOST, "method": "reload"}),
            ("set_parameter", {"op_path": HOST, "par_name": "reinitnet",
                               "value": "1"})):
        got = refuse(fake, operation, params)
        assert got is not None, (operation, params)
        assert got["error_code"] == "envoy.embody.host_destroy_refused"
        assert got["refused_target"] == params["op_path"]
    assert all(level == "WARNING" for level, _ in fake.logs)
    assert len(fake.logs) == 5


def test_structured_guard_allows_everything_else():
    fake = _guard_self()
    refuse = ENVOY.EnvoyExt._hostDestroyRefusal
    for operation, params in (
            ("delete_op", {"op_path": HOST + "/tagger"}),
            ("delete_op", {"op_path": "/nowhere"}),
            ("delete_op", {}),
            ("exec_op_method", {"op_path": HOST, "method": "cook"}),
            ("set_parameter", {"op_path": HOST, "par_name": "tx"}),
            ("get_op", {"op_path": HOST}),
            ("batch_operations", {}),
            ("batch_operations", {"operations": "nope"})):
        assert refuse(fake, operation, params) is None, (operation, params)
    assert fake.logs == []


def test_batch_with_a_host_sub_op_is_refused_whole():
    fake = _guard_self()
    got = ENVOY.EnvoyExt._hostDestroyRefusal(fake, "batch_operations", {
        "operations": [
            {"tool": "create_op", "params": {"parent_path": "/p"}},
            "garbage",
            {"tool": "delete_op", "params": {"op_path": "/a"}}]})
    assert got is not None and got["refused_target"] == "/a"


def test_batch_lints_its_execute_python_sub_ops_up_front():
    """So 'nothing ran' holds for a batch too: an execute_python sub-op's
    refusal lands before the batch's first sub-op."""
    fake = _guard_self()
    seen = []
    refusal = {"error": "HOST-DESTROY REFUSED (nothing ran): ...",
               "error_code": "envoy.embody.host_destroy_refused",
               "refused_target": HOST}

    def lint_(code, namespace):
        seen.append((code, namespace))
        return refusal if "destroy" in str(code) else None

    fake._hostDestroyLint = lint_
    fake._execNamespace = lambda: {"me": "host"}
    got = ENVOY.EnvoyExt._hostDestroyRefusal(fake, "batch_operations", {
        "operations": [
            {"tool": "execute_python", "params": {"code": "result = 1"}},
            {"tool": "create_op", "params": {"parent_path": "/p"}},
            {"tool": "execute_python", "params": "garbage"},
            {"tool": "execute_python", "params": {"code": "me.destroy()"}}]})
    assert got is refusal
    assert [code for code, _ in seen] == ["result = 1", "me.destroy()"]
    assert all(namespace == {"me": "host"} for _, namespace in seen)


def test_structured_guard_fails_closed_when_it_cannot_judge():
    def broken(path):
        raise RuntimeError("resolver down")

    fake = _guard_self(resolve_op=broken)
    got = ENVOY.EnvoyExt._hostDestroyRefusal(fake, "delete_op",
                                             {"op_path": "/p/n1"})
    assert got is not None
    assert "could not reach a verdict" in got["error"]
    assert got["error_code"] == "envoy.embody.host_destroy_refused"
    assert fake.logs[-1][0] == "ERROR"
    # A call that is not a guarded shape never reaches the resolver.
    assert ENVOY.EnvoyExt._hostDestroyRefusal(
        fake, "exec_op_method", {"op_path": "/p/n1", "method": "cook"}) is None


def test_host_chain_walks_to_root_then_adds_the_extension_dat():
    root = _FakeOp("/")
    parent_comp = _FakeOp("/a", parent=root)
    ext_dat = _FakeOp(EXT_DAT)
    host = _FakeOp(HOST, parent=parent_comp, children={"EnvoyExt": ext_dat})
    fake = SimpleNamespace(ownerComp=host)
    assert ENVOY.EnvoyExt._hostChain(fake) == CHAIN


# --- runtime backstop (EnvoyExt with TD globals planted) -----------------

class _FakeRoot:
    def findChildren(self, **_kw):
        return []


ENVOY.root = _FakeRoot()
ENVOY.op = lambda *a, **k: None
ENVOY.ops = lambda *a, **k: []
ENVOY.parent = lambda *a, **k: None


def _exec_self(lint_result=None):
    host = _FakeOp(HOST)
    calls = {"lintNewOps": 0, "rollback": 0, "lint_ns": None}
    fake = SimpleNamespace(ownerComp=host, calls=calls)
    host.ext = SimpleNamespace(Envoy=fake)
    fake._log = lambda *a, **k: None
    fake._lintWorkerRun = lambda *a, **k: None

    def lint_(code, namespace):
        calls["lint_ns"] = namespace
        return lint_result

    def lint_new_ops(pre_paths):
        calls["lintNewOps"] += 1

    def rollback(pre_paths):
        calls["rollback"] += 1
        return 0

    fake._hostDestroyLint = lint_
    fake._lintNewOps = lint_new_ops
    fake._rollbackNewOps = rollback
    fake._execNamespace = lambda: ENVOY.EnvoyExt._execNamespace(fake)
    fake._hostIsGone = lambda: ENVOY.EnvoyExt._hostIsGone(fake)
    fake._hostDestroyedResult = (
        lambda result: ENVOY.EnvoyExt._hostDestroyedResult(fake, result))
    return fake


def test_host_destroyed_mid_exec_skips_the_post_exec_tail():
    fake = _exec_self()
    got = ENVOY.EnvoyExt._execute_python(fake, "me.destroy()\nresult = 'moved'")
    assert got["success"] is True
    assert got["host_destroyed"] is True
    assert got["result"] == "moved"
    assert "delayFrames=30" in got["warning"] and _ascii(got["warning"])
    assert fake.calls["lintNewOps"] == 0


def test_host_reloaded_mid_exec_counts_as_gone():
    """A reload or reinit replaces this instance but destroys no COMP, so
    it is worded apart from a destroy."""
    fake = _exec_self()
    code = "me.ext.Envoy = object()"   # what an extension reinit does
    got = ENVOY.EnvoyExt._execute_python(fake, code)
    assert got.get("host_reloaded") is True
    assert "host_destroyed" not in got
    assert "reinitialized" in got["warning"] and _ascii(got["warning"])
    assert fake.calls["lintNewOps"] == 0


def test_a_host_check_that_raises_counts_as_gone_and_says_so():
    class _Raising:
        @property
        def valid(self):
            raise RuntimeError("tdError: gone")

    logs = []
    fake = SimpleNamespace(ownerComp=_Raising(), _log=(
        lambda message, level="INFO": logs.append((level, message))))
    assert ENVOY.EnvoyExt._hostIsGone(fake) is True
    assert [level for level, _ in logs] == ["WARNING"]
    assert "tdError: gone" in logs[0][1]


def test_batch_stops_once_a_sub_op_destroyed_the_host():
    """The lint missed it (a dynamic receiver) and the destroying sub-op
    reports success: the rest must not run from the dead instance."""
    host = _FakeOp(HOST)
    ran = []
    fake = SimpleNamespace(ownerComp=host, _log=lambda *a, **k: None)
    host.ext = SimpleNamespace(Envoy=fake)
    fake._hostIsGone = lambda: ENVOY.EnvoyExt._hostIsGone(fake)

    def run_op(tool, params):
        ran.append(tool)
        if tool == "execute_python":
            host.destroy()
            return {"success": True, "host_destroyed": True}
        return {"success": True}

    fake._execute_operation = run_op
    got = ENVOY.EnvoyExt._batch_operations(fake, [
        {"tool": "get_op", "params": {}},
        {"tool": "execute_python", "params": {}},
        {"tool": "delete_op", "params": {"op_path": HOST + "/x"}}])
    assert ran == ["get_op", "execute_python"]
    assert got["success"] is False
    assert "skipped" in got["results"][-1]["error"]
    assert _ascii(got["results"][-1]["error"])


class _NoGuardMod:
    """TD's `mod` before the envoy_guard DAT exists."""

    def __getattr__(self, name):
        raise AttributeError(name)


def _plant_mod(value):
    saved = ENVOY.__dict__.get("mod", _MISSING)
    ENVOY.mod = value
    return saved


def _unplant_mod(saved):
    if saved is _MISSING:
        ENVOY.__dict__.pop("mod", None)
    else:
        ENVOY.mod = saved


def test_lint_prefilters_before_the_module_lookup():
    logs = []
    fake = SimpleNamespace(_log=(
        lambda message, level="INFO": logs.append((level, message))))
    saved = _plant_mod(_NoGuardMod())
    try:
        assert ENVOY.EnvoyExt._hostDestroyLint(fake, "result = 1", {}) is None
        assert ENVOY.EnvoyExt._hostDestroyLint(fake, None, {}) is None
        assert logs == [], "token-free code never reaches mod.envoy_guard"
        assert ENVOY.EnvoyExt._hostDestroyLint(fake, "x.destroy()", {}) is None
    finally:
        _unplant_mod(saved)
    assert [level for level, _ in logs] == ["DEBUG"]
    assert "fail-open" in logs[0][1]


def test_lint_glue_end_to_end_with_the_real_guard():
    logs = []
    fake = SimpleNamespace(
        _log=lambda message, level="INFO": logs.append((level, message)),
        _hostChain=lambda: list(CHAIN),
        _resolveLookupPath=lambda node, namespace: MAP.get(ast.unparse(node)))
    saved = _plant_mod(SimpleNamespace(envoy_guard=GUARD))
    try:
        got = ENVOY.EnvoyExt._hostDestroyLint(
            fake, "x = 1\nop('..').destroy()", {})
        clean = ENVOY.EnvoyExt._hostDestroyLint(
            fake, "op('/p/n1').destroy()", {})
    finally:
        _unplant_mod(saved)
    assert got["error_code"] == "envoy.embody.host_destroy_refused"
    assert got["refused_target"] == HOST
    assert "line 2" in got["error"] and _ascii(got["error"])
    assert clean is None
    assert [level for level, _ in logs] == ["WARNING"]


def test_start_stamps_a_fresh_main_tick_even_when_it_bails():
    """sys outlives Stop: a restarted worker must not serve an age left
    over from before a long disable."""
    fake = SimpleNamespace(
        ownerComp=SimpleNamespace(par=SimpleNamespace(
            Envoyenable=SimpleNamespace(eval=lambda: False))),
        _log=lambda *a, **k: None)
    saved = getattr(sys, "_envoy_main_tick", _MISSING)
    try:
        sys._envoy_main_tick = 0.0
        before = time.monotonic()
        ENVOY.EnvoyExt.Start(fake)
        assert sys._envoy_main_tick >= before
    finally:
        _restore_tick(saved)


def test_host_destroyed_then_raise_never_rolls_back():
    fake = _exec_self()
    got = ENVOY.EnvoyExt._execute_python(
        fake, "me.destroy()\nraise RuntimeError('boom')")
    assert "nothing was rolled back" in got["error"]
    assert "boom" in got["error"]
    assert fake.calls["rollback"] == 0


def test_healthy_exec_keeps_its_tail():
    fake = _exec_self()
    got = ENVOY.EnvoyExt._execute_python(fake, "result = 7")
    assert got == {"success": True, "result": "7"}
    assert fake.calls["lintNewOps"] == 1
    failed = ENVOY.EnvoyExt._execute_python(fake, "raise RuntimeError('x')")
    assert "error" in failed and fake.calls["rollback"] == 1


def test_lint_refusal_returns_before_anything_runs():
    refusal = {"error": "HOST-DESTROY REFUSED (nothing ran): ...",
               "error_code": "envoy.embody.host_destroy_refused"}
    fake = _exec_self(lint_result=refusal)
    got = ENVOY.EnvoyExt._execute_python(fake, "me.destroy()")
    assert got is refusal
    assert fake.ownerComp.valid, "the code must not have run"


def test_lint_sees_the_namespace_the_exec_uses():
    fake = _exec_self()
    ENVOY.EnvoyExt._execute_python(fake, "result = 5")
    namespace = fake.calls["lint_ns"]
    assert namespace["me"] is fake.ownerComp
    assert namespace["result"] == 5, "the lint must see the exec's own dict"


def test_send_response_survives_failing_attachments():
    queue = Queue()
    effects = []

    def boom(*a, **k):
        raise RuntimeError("op.Embody is gone")

    fake = SimpleNamespace(response_queue=queue,
                           _attachRecoveryHints=boom, _attachNotableLogs=boom,
                           _attachEffects=lambda *a, **k: effects.append(1))
    ENVOY.EnvoyExt._send_response(fake, 7, {"ok": True}, None, "get_op")
    assert queue.get_nowait() == {"id": 7, "result": {"ok": True}}
    assert effects == [1], "each attachment rides its own try"


def _server(pending_ids=(7,)):
    """A worker-side stand-in wired the way EnvoyMCPServer.__init__ wires
    the direct-delivery hook."""
    queue = Queue()
    server = SimpleNamespace(
        lock=Lock(), response_queue=queue,
        pending_requests={i: {"event": Event(), "result": None}
                          for i in pending_ids})
    queue.envoy_deliver = (
        lambda message: ENVOY.EnvoyMCPServer.check_responses(server, message))
    return server


def _refresh_self(server, execute):
    host = _FakeOp(HOST)
    calls = {"finish": 0, "viz": 0, "gate": 0, "execute": 0}
    fake = SimpleNamespace(ownerComp=host, calls=calls, _viewer_swept=True)
    host.ext = SimpleNamespace(Envoy=fake)
    fake.request_queue = Queue()
    fake.response_queue = server.response_queue
    fake.shutdown_event = Event()
    fake._log = lambda *a, **k: None
    fake._baselineLogCursor = lambda sid: None
    fake._hostDestroyRefusal = lambda operation, params: None

    def gate(sid, operation, params):
        calls["gate"] += 1
        return None

    def run_op(operation, params):
        calls["execute"] += 1
        return execute(fake)

    def finish(*a, **k):
        calls["finish"] += 1

    def viz():
        calls["viz"] += 1

    fake._gateVerdict = gate
    fake._execute_operation = run_op
    fake._finishOperation = finish
    fake._vizTick = viz
    fake._hostIsGone = lambda: ENVOY.EnvoyExt._hostIsGone(fake)
    fake._attachRecoveryHints = (
        lambda result: ENVOY.EnvoyExt._attachRecoveryHints(fake, result))
    fake._answerAfterHostDestroyed = (
        lambda rid, result:
        ENVOY.EnvoyExt._answerAfterHostDestroyed(fake, rid, result))
    fake._send_response = lambda *a, **k: calls.setdefault("sent", a)
    return fake


def test_dead_host_answers_the_worker_even_with_shutdown_already_set():
    server = _server()

    def destroy_host(fake):
        fake.ownerComp.destroy()
        return {"success": True, "host_destroyed": True}

    fake = _refresh_self(server, destroy_host)
    fake.shutdown_event.set()   # onDestroyTD got there first
    fake.request_queue.put({"id": 7, "operation": "execute_python",
                            "params": {"code": "x"}, "sid": None})
    ENVOY.EnvoyExt._onRefresh(fake)
    pending = server.pending_requests[7]
    assert pending["event"].is_set(), "the worker would wait its full 30s"
    assert pending["result"]["host_destroyed"] is True
    assert fake.calls["finish"] == 0, "the tail reads op.Embody"
    assert fake.calls["viz"] == 0
    assert fake.shutdown_event.is_set()


def test_refresh_survives_a_failing_log_baseline():
    server = _server()
    fake = _refresh_self(server, lambda f: {"success": True})

    def boom(sid):
        raise RuntimeError("no COMP holds op.Embody")

    fake._baselineLogCursor = boom
    fake.request_queue.put({"id": 7, "operation": "get_op",
                            "params": {}, "sid": None})
    ENVOY.EnvoyExt._onRefresh(fake)
    assert fake.calls["finish"] == 1


def test_host_guard_answers_before_the_session_gate():
    server = _server()
    fake = _refresh_self(server, lambda f: {"success": True})
    refusal = {"error": "HOST-DESTROY REFUSED (nothing changed): ...",
               "error_code": "envoy.embody.host_destroy_refused"}
    fake._hostDestroyRefusal = lambda operation, params: refusal
    fake.request_queue.put({"id": 7, "operation": "delete_op",
                            "params": {"op_path": HOST, "override": True},
                            "sid": None})
    ENVOY.EnvoyExt._onRefresh(fake)
    assert fake.calls["gate"] == 0, "no 'pass override=True' advice"
    assert fake.calls["execute"] == 0
    assert fake.calls["sent"][:2] == (7, refusal)


def test_refresh_stamps_the_main_tick_even_on_a_stale_instance():
    server = _server()
    fake = _refresh_self(server, lambda f: {"success": True})
    fake.ownerComp.ext.Envoy = object()          # a newer instance took over
    saved = getattr(sys, "_envoy_main_tick", _MISSING)
    try:
        sys._envoy_main_tick = 0.0
        before = time.monotonic()
        ENVOY.EnvoyExt._onRefresh(fake)
        assert sys._envoy_main_tick >= before
        assert fake.calls["execute"] == 0
    finally:
        _restore_tick(saved)


_MISSING = object()


def _restore_tick(saved):
    if saved is _MISSING:
        if hasattr(sys, "_envoy_main_tick"):
            del sys._envoy_main_tick
    else:
        sys._envoy_main_tick = saved


def test_main_tick_age():
    saved = getattr(sys, "_envoy_main_tick", _MISSING)
    try:
        sys._envoy_main_tick = 100.0
        assert ENVOY._main_tick_age(now=160.5) == 60.5
        assert ENVOY._main_tick_age(now=90.0) == 0.0
        sys._envoy_main_tick = "stale"
        assert ENVOY._main_tick_age(now=1.0) is None
        del sys._envoy_main_tick
        assert ENVOY._main_tick_age() is None
    finally:
        _restore_tick(saved)


def test_worker_timeout_names_a_stuck_main_thread():
    """The event is never set, so the 0.01s wait always expires: no race."""
    fake = SimpleNamespace(lock=Lock(), request_counter=0, pending_requests={},
                           add_to_refresh_queue=lambda data: None,
                           _touch_session=lambda *a, **k: None)
    saved = getattr(sys, "_envoy_main_tick", _MISSING)
    try:
        sys._envoy_main_tick = time.monotonic() - 100.0
        stuck = ENVOY.EnvoyMCPServer._execute_in_td(fake, "x", {}, timeout=0.01)
        assert "list_dialogs" in stuck["error"]
        assert ENVOY._error_code_for(stuck["error"]) == "envoy.timeout"
        sys._envoy_main_tick = time.monotonic() + 1000.0
        fresh = ENVOY.EnvoyMCPServer._execute_in_td(fake, "x", {}, timeout=0.01)
        assert "list_dialogs" not in fresh["error"]
        assert fake.pending_requests == {}
    finally:
        _restore_tick(saved)
