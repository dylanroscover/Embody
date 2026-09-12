"""
envoy_guard -- pre-exec host-destroy lint for execute_python (issue #110).

An Envoy request runs nested inside the Embody COMP that hosts Envoy:
RefreshHook -> _execute_operation -> exec, in an enabled undo block, while
the worker waits on the response. Destroying or reloading that COMP, an
ancestor, '/', or the EnvoyExt DAT from there has hung TouchDesigner; a
TD-native stall is the leading hypothesis, not proven. EnvoyExt refuses
code this lint flags, before anything runs.

Contract:
- Pure. No TD access here. Receivers are resolved by the `resolve` callable
  EnvoyExt passes in; it evals whitelisted lookups from an EnvoyExt frame,
  because TD resolves relative op()/parent() against the innermost DAT
  frame (probed on 2025.33230), the same frame the exec runs from.
- FAIL-OPEN. Any fault lints to nothing; EnvoyExt's runtime backstop still
  stops a dead host from running its post-exec tail.
- Bounded by a token prefilter: a source with no destroy/reload token is
  never parsed. No size cap -- a missed destroy costs a hung TD.

Not detected (accepted bypasses): dynamic receivers (op(path_var), loops
over .children or findChildren(), ops(...)[0]); code run through exec/eval
strings; destroys inside helper modules; project.load()/project.quit();
extension reinit pulses on Embody (a reinit drops the response but is not
the #110 hang). Followed: td.op / td.parent / td.root spellings, tuple
unpacking from a literal tuple, and a called def's parameters bound to its
call arguments (review, 2026-09-11).

Aliases are flow-insensitive: up to _MAX_BINDINGS bindings of a name are
followed per hop (nearest before the use first, then after it), up to
_ALIAS_DEPTH hops, and me/root/op/opex/parent keep their builtin meaning
even when rebound. So `t = root` later rebound to another op is still
refused -- a false positive that errs toward refusal. An alias that leads
to no lookup is walked once per pass, so alias cycles stay linear.
"""

from __future__ import annotations

import ast
import bisect
from typing import Any, Callable, Iterator, Optional

# Parity with EnvoyExt._HOST_DESTROY_METHODS / _HOST_RELOAD_PULSES is pinned
# by test_host_destroy_lint.py. reinitnet is TD's alias of
# enableexternaltoxpulse (probe, 2025.33230).
DESTROY_METHODS = frozenset({'destroy', 'reload', 'changeType',
                             'progressiveUnload'})
RELOAD_PULSES = frozenset({'enableexternaltoxpulse', 'reinitnet',
                           'enablecloningpulse'})

_TOKENS = ('destroy', 'reload', 'changetype', 'progressiveunload',
           'externaltoxpulse', 'reinitnet', 'cloningpulse')
_LOOKUP_NAMES = frozenset({'me', 'root'})
_LOOKUP_CALLS = frozenset({'op', 'opex', 'parent'})
_LOOKUP_METHODS = frozenset({'parent', 'op'})
_SHORTCUT_HOLDERS = frozenset({'op', 'parent'})
_BUILTIN_LOOKUPS = _LOOKUP_NAMES | _LOOKUP_CALLS | _SHORTCUT_HOLDERS
_ALIAS_DEPTH = 4          # alias-of-alias hops followed
_MAX_BINDINGS = 8         # bindings of one name followed per hop
_MAX_CANDIDATES = 8       # receiver spellings resolved per call
_MAX_EXPANSIONS = 64      # receiver spellings generated per call
_MAX_RUN_NESTING = 2      # run('run(...)') strings parsed this deep
_CALL_TEXT_MAX = 80
# run() delays that are proven to land on a later frame. delayMilliSeconds
# is rounded to the nearest frame (docs.derivative.ca/Td_Module), so a
# short one can round to zero: 100ms is at least one frame at >= 10fps.
_MIN_DELAY_FRAMES = 1
_MIN_DELAY_MS = 100


def has_destroy_token(source: Any) -> bool:
    """Cheap prefilter: can `source` contain a destroy/reload call?"""
    if not isinstance(source, str) or not source:
        return False
    lowered = source.lower()
    return any(token in lowered for token in _TOKENS)


def host_destroy_findings(source: Any, resolve: Callable[[ast.AST], Any],
                          guarded: Any) -> list:
    """Destroy/reload calls in `source` whose receiver resolves into
    `guarded`, as [{'line', 'call', 'target'}] sorted by line.

    `resolve(node)` gets ONLY whitelisted lookup expressions (_is_lookup)
    and returns an operator path or None. Returns [] for a non-str or
    token-free source (resolve is never called) and for unparseable code.
    """
    if not has_destroy_token(source):
        return []
    try:
        tree = ast.parse(source)
    except Exception:
        return []
    try:
        scan = _Scan(resolve, guarded)
        scan.run(tree, line=None, nesting=0)
        return sorted(scan.findings, key=lambda f: (f['line'], f['call']))
    except Exception:
        return []


def python_refusal_text(finding: dict, relation: str) -> str:
    """execute_python's refusal message for one finding. ASCII only; the
    user's call text is escaped so it cannot bring a raw glyph along."""
    call = str(finding.get('call', '')).encode(
        'ascii', 'backslashreplace').decode('ascii')
    return (
        "HOST-DESTROY REFUSED (nothing ran): line %s: %s would destroy or "
        "reload %s (%s) while Envoy's request is still executing, and that "
        "has hung TouchDesigner (issue #110). Inside execute_python, me and "
        "parent() are the Embody COMP. If that receiver is a variable you "
        "reassigned, give the Embody reference and the other operator "
        "distinct names. If the user asked to move or remove "
        "Embody: save first (save_project), then defer the WHOLE sequence as "
        "ONE string, run(code, delayFrames=30). Make "
        "op.Embody.ext.Envoy.Stop() the first line inside that string, and "
        "there use op.Embody or resolved absolute paths -- me and parent() "
        "mean something else there. Envoy reconnects from the new instance "
        "afterwards."
        % (finding.get('line'), call, relation, finding.get('target')))


# --- receiver whitelist ----------------------------------------------------

def _is_const_arg(node: ast.AST) -> bool:
    return (isinstance(node, ast.Constant)
            and isinstance(node.value, (str, int))
            and not isinstance(node.value, bool))


def _is_lookup(node: ast.AST) -> bool:
    """True for a pure operator lookup the resolver may eval: me, root,
    op.X / parent.X shortcuts, op('...') / opex('...') / parent() /
    parent(N), and .parent(...) / .op('...') on another lookup. Every
    argument is a literal, so evaluating it can have no side effect."""
    if isinstance(node, ast.Name):
        return node.id in _LOOKUP_NAMES
    if isinstance(node, ast.Attribute):
        return (isinstance(node.value, ast.Name)
                and node.value.id in _SHORTCUT_HOLDERS
                and not node.attr.startswith('_'))
    if isinstance(node, ast.Call):
        if node.keywords or len(node.args) > 1:
            return False
        if not all(_is_const_arg(arg) for arg in node.args):
            return False
        func = node.func
        if isinstance(func, ast.Name):
            return func.id in _LOOKUP_CALLS
        if isinstance(func, ast.Attribute) and func.attr in _LOOKUP_METHODS:
            return _is_lookup(func.value)
    return False


def _position(node: ast.AST) -> tuple:
    return (getattr(node, 'lineno', 0), getattr(node, 'col_offset', 0))


def _nearest(binding: tuple, at: tuple) -> list:
    """Up to _MAX_BINDINGS bound values of one name: nearest before `at`
    first (the likeliest in effect), then those after it (loops)."""
    positions, values = binding
    split = bisect.bisect_left(positions, at)
    before = values[max(0, split - _MAX_BINDINGS):split][::-1]
    return (before + values[split:split + _MAX_BINDINGS])[:_MAX_BINDINGS]


def _expand(node: ast.AST, aliases: dict, depth: int = 0,
            dead: Optional[set] = None) -> Iterator[ast.AST]:
    """Alias-free spellings of a receiver: each name bound to a lookup is
    substituted by what it was bound to (flow-insensitive, so a rebound
    name errs toward a refusal). Builtin lookups (me, root, op, opex,
    parent) keep their meaning even when rebound anywhere -- an unrelated
    `op = x` must not hide op.Embody.destroy() (issue #110 review).

    `dead` holds (id(name node), depth) whose bindings yielded nothing; a
    walk skips them. Exact -- a dead end yields nothing to skip -- and it
    keeps an alias cycle linear: yield caps never see a fan-out that
    yields nothing (8^4 steps per call, 5 s for 2000 calls, review)."""
    if isinstance(node, ast.NamedExpr):
        yield from _expand(node.value, aliases, depth, dead)
        return
    # td.op / td.parent / td.root are the same lookups (review).
    if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
            and node.value.id == 'td' and node.attr in _BUILTIN_LOOKUPS):
        yield ast.Name(id=node.attr, ctx=ast.Load())
        return
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == 'td' and node.func.attr in _LOOKUP_CALLS):
        yield ast.Call(func=ast.Name(id=node.func.attr, ctx=ast.Load()),
                       args=node.args, keywords=node.keywords)
        return
    if isinstance(node, ast.Name):
        if node.id in _BUILTIN_LOOKUPS or node.id not in aliases:
            yield node
        if node.id in aliases and depth < _ALIAS_DEPTH:
            key = (id(node), depth)
            if dead is not None and key in dead:
                return
            found = False
            for value in _nearest(aliases[node.id], _position(node)):
                for spelling in _expand(value, aliases, depth + 1, dead):
                    found = True
                    yield spelling
            if not found and dead is not None:
                dead.add(key)   # never reached when the consumer stops early
        return
    if isinstance(node, ast.Attribute):
        for inner in _expand(node.value, aliases, depth, dead):
            yield ast.Attribute(value=inner, attr=node.attr, ctx=ast.Load())
        return
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        for inner in _expand(node.func.value, aliases, depth, dead):
            yield ast.Call(
                func=ast.Attribute(value=inner, attr=node.func.attr,
                                   ctx=ast.Load()),
                args=node.args, keywords=node.keywords)
        return
    yield node


def _collect_aliases(tree: ast.AST) -> dict:
    """name -> ([positions], [bound values]) in source order, for Assign
    (Name targets, and tuple/list targets unpacked from a literal tuple or
    list of the same length), AnnAssign with a value, and walrus bindings,
    anywhere in the tree. _nearest picks which of them a use follows."""
    found: dict = {}

    def bind(name: str, node: ast.AST, value: ast.AST) -> None:
        found.setdefault(name, []).append((_position(node), value))

    def bind_target(target: ast.AST, node: ast.AST, value: ast.AST) -> None:
        if isinstance(target, ast.Name):
            bind(target.id, node, value)
        elif (isinstance(target, (ast.Tuple, ast.List))
                and isinstance(value, (ast.Tuple, ast.List))
                and len(target.elts) == len(value.elts)):
            for sub_target, sub_value in zip(target.elts, value.elts):
                bind_target(sub_target, node, sub_value)

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                bind_target(target, node, node.value)
        elif isinstance(node, ast.AnnAssign):
            if node.value is not None and isinstance(node.target, ast.Name):
                bind(node.target.id, node, node.value)
        elif isinstance(node, ast.NamedExpr):
            if isinstance(node.target, ast.Name):
                bind(node.target.id, node, node.value)
    aliases: dict = {}
    for name, pairs in found.items():
        pairs.sort(key=lambda pair: pair[0])
        aliases[name] = ([pos for pos, _ in pairs], [val for _, val in pairs])
    return aliases


def _run_is_shadowed(tree: ast.AST) -> bool:
    """True when module scope rebinds `run` to something other than TD's
    run() (a def, a class, an assignment, or an import not from td)."""
    stack = list(getattr(tree, 'body', []))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            if node.name == 'run':
                return True
            continue
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            from_td = isinstance(node, ast.ImportFrom) and node.module == 'td'
            for alias in node.names:
                bound = (alias.asname or alias.name).split('.')[0]
                if bound == 'run' and not (from_td and alias.name == 'run'):
                    return True
            continue
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store) \
                and node.id == 'run':
            return True
        stack.extend(ast.iter_child_nodes(node))
    return False


# --- destroy shapes ----------------------------------------------------------

def _pulse_owner(node: ast.AST) -> Optional[ast.AST]:
    """R for R.par.<pulse> or R.par['<pulse>'] naming a reload/clone pulse."""
    if isinstance(node, ast.Attribute):
        name, holder = node.attr, node.value
    elif (isinstance(node, ast.Subscript)
          and isinstance(node.slice, ast.Constant)
          and isinstance(node.slice.value, str)):
        name, holder = node.slice.value, node.value
    else:
        return None
    if (name.lower() in RELOAD_PULSES and isinstance(holder, ast.Attribute)
            and holder.attr == 'par'):
        return holder.value
    return None


def _destroy_receiver(call: ast.Call) -> Optional[ast.AST]:
    """The receiver of R.<destroy method>(...), R.par.<pulse>.pulse(), or
    getattr(R, '<destroy method>')(...); None for any other call."""
    func = call.func
    if isinstance(func, ast.Attribute):
        if func.attr in DESTROY_METHODS:
            return func.value
        if func.attr == 'pulse':
            return _pulse_owner(func.value)
        return None
    if (isinstance(func, ast.Call) and isinstance(func.func, ast.Name)
            and func.func.id == 'getattr' and len(func.args) >= 2
            and isinstance(func.args[1], ast.Constant)
            and func.args[1].value in DESTROY_METHODS):
        return func.args[0]
    return None


def _written_pulse_owner(target: ast.AST) -> Optional[ast.AST]:
    """R for a write to R.par.<pulse> or R.par.<pulse>.val. Whether a write
    pulses is unverified; like set_parameter, it is treated as one."""
    owner = _pulse_owner(target)
    if owner is None and isinstance(target, ast.Attribute) \
            and target.attr == 'val':
        owner = _pulse_owner(target.value)
    return owner


def _is_td_run(call: ast.Call, run_shadowed: bool) -> bool:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id == 'run' and not run_shadowed
    return (isinstance(func, ast.Attribute) and func.attr == 'run'
            and isinstance(func.value, ast.Name) and func.value.id == 'td')


def _run_is_deferred(call: ast.Call) -> bool:
    """A literal delay proven to land on a later frame. Zero-delay, endFrame
    and computed delays stay in scope: their queueing is undocumented."""
    for kw in call.keywords:
        value = kw.value
        if not (isinstance(value, ast.Constant)
                and isinstance(value.value, (int, float))
                and not isinstance(value.value, bool)):
            continue
        if kw.arg == 'delayFrames' and value.value >= _MIN_DELAY_FRAMES:
            return True
        if kw.arg == 'delayMilliSeconds' and value.value >= _MIN_DELAY_MS:
            return True
    return False


def _run_script(call: ast.Call) -> Optional[ast.AST]:
    if call.args:
        return call.args[0]
    for kw in call.keywords:
        if kw.arg == 'scriptOrCallable':
            return kw.value
    return None


def _literal_text(node: Optional[ast.AST]) -> Optional[str]:
    """A str literal's text, or an f-string's with no placeholders."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr) and all(
            isinstance(part, ast.Constant) and isinstance(part.value, str)
            for part in node.values):
        return ''.join(part.value for part in node.values)
    return None


def _call_text(node: ast.AST) -> str:
    try:
        text = ast.unparse(node)
    except Exception:
        text = type(node).__name__
    text = ' '.join(text.split())
    if len(text) > _CALL_TEXT_MAX:
        text = text[:_CALL_TEXT_MAX - 3] + '...'
    return text


class _Scan:
    """One lint pass. Walks only code that executes during the exec: module
    statements, immediately invoked lambdas, the script of a run() that is
    NOT literally deferred (a literal, a lambda, a def, a bound method, or
    a name holding one), and one level into defs called by name. Def and
    lambda bodies otherwise run later, so they are not scanned."""

    def __init__(self, resolve: Callable[[ast.AST], Any], guarded: Any,
                 resolved: Optional[dict] = None) -> None:
        self.resolve = resolve
        self.guarded = guarded
        # lookup dump -> path, shared with nested run() scans: one eval per
        # distinct lookup per pass, however often it repeats.
        self.resolved: dict = {} if resolved is None else resolved
        self.findings: list = []
        self._seen: set = set()

    def run(self, tree: ast.AST, line: Optional[int], nesting: int) -> None:
        self.aliases = _collect_aliases(tree)
        self.dead: set = set()   # _expand's dead ends, shared by every call
        self.run_shadowed = _run_is_shadowed(tree)
        self.defs: dict = {}
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.defs.setdefault(node.name, node)
        # A def called by name sees its call sites' arguments as aliases of
        # its parameters: def rm(o): o.destroy(); rm(op.Embody) (review).
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id in self.defs):
                self._bind_params(self.defs[node.func.id], node)
        self.followed: set = set()
        self._walk(list(getattr(tree, 'body', [])), line, nesting, level=0)

    def _walk(self, roots: list, line: Optional[int], nesting: int,
              level: int) -> None:
        stack = [(node, level) for node in reversed(roots)]
        while stack:
            node, lvl = stack.pop()
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.Lambda)):
                # Decorators and defaults run at definition; the body later.
                args = node.args
                extras = list(args.defaults) + [d for d in args.kw_defaults
                                                if d is not None]
                extras += list(getattr(node, 'decorator_list', []))
                stack.extend((extra, lvl) for extra in reversed(extras))
                continue
            here = line if line is not None else getattr(node, 'lineno', 0)
            if isinstance(node, ast.Call):
                self._check_call(node, here, nesting, lvl, stack)
            elif isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
                targets = (node.targets if isinstance(node, ast.Assign)
                           else [node.target])
                for target in targets:
                    owner = _written_pulse_owner(target)
                    if owner is not None:
                        self._check(node, owner, here)
            stack.extend((child, lvl)
                         for child in reversed(list(ast.iter_child_nodes(node))))

    def _check_call(self, call: ast.Call, line: int, nesting: int,
                    level: int, stack: list) -> None:
        receiver = _destroy_receiver(call)
        if receiver is not None:
            self._check(call, receiver, line)
        func = call.func
        if isinstance(func, ast.Name):
            for owner in self._bound_destroys(func):   # d = R.destroy; d()
                self._check(call, owner, line)
        if isinstance(func, ast.Lambda):
            stack.append((func.body, level))       # immediately invoked
        elif _is_td_run(call, self.run_shadowed):
            if not _run_is_deferred(call):
                self._scan_run_script(_run_script(call), line, nesting, level,
                                      stack)
        elif (isinstance(func, ast.Name) and func.id in self.defs
                and level == 0 and func.id not in self.followed):
            self.followed.add(func.id)
            body = self.defs[func.id].body
            stack.extend((stmt, 1) for stmt in reversed(body))

    def _bound_destroys(self, name: ast.Name) -> list:
        """R for each `name = R.<destroy method>` binding this use follows."""
        binding = self.aliases.get(name.id)
        if binding is None:
            return []
        return [value.value for value in _nearest(binding, _position(name))
                if isinstance(value, ast.Attribute)
                and value.attr in DESTROY_METHODS]

    def _bind_params(self, fn: ast.AST, call: ast.Call) -> None:
        """Alias fn's parameters to this call's arguments (flow-insensitive,
        like every other binding)."""
        args = fn.args
        params = [a.arg for a in args.posonlyargs + args.args]
        pairs = list(zip(params, call.args))
        names = set(params) | {a.arg for a in args.kwonlyargs}
        pairs += [(kw.arg, kw.value) for kw in call.keywords if kw.arg in names]
        at = _position(call)
        for name, value in pairs:
            positions, values = self.aliases.setdefault(name, ([], []))
            i = bisect.bisect_left(positions, at)
            positions.insert(i, at)
            values.insert(i, value)

    def _scan_run_script(self, script: Optional[ast.AST], line: int,
                         nesting: int, level: int, stack: list,
                         hopped: bool = False) -> None:
        if isinstance(script, ast.Lambda):
            stack.append((script.body, level))
        elif isinstance(script, ast.Attribute):
            if script.attr in DESTROY_METHODS:     # run(op.Embody.destroy)
                self._check(script, script.value, line)
        elif isinstance(script, ast.Name):
            if (script.id in self.defs and level == 0
                    and script.id not in self.followed):
                self.followed.add(script.id)
                stack.extend((stmt, 1) for stmt in
                             reversed(self.defs[script.id].body))
            if not hopped and script.id in self.aliases:
                # code = '...'; run(code): one hop to what the name holds.
                for value in _nearest(self.aliases[script.id],
                                      _position(script)):
                    self._scan_run_script(value, line, nesting, level, stack,
                                          hopped=True)
        else:
            text = _literal_text(script)
            if (text is None or nesting >= _MAX_RUN_NESTING
                    or not has_destroy_token(text)):
                return
            try:
                inner = ast.parse(text)
            except Exception:
                return
            nested = _Scan(self.resolve, self.guarded, self.resolved)
            nested.run(inner, line=line, nesting=nesting + 1)
            for finding in nested.findings:
                self._add(finding)

    def _check(self, node: ast.AST, receiver: ast.AST, line: int) -> None:
        tried = set()
        for count, candidate in enumerate(
                _expand(receiver, self.aliases, dead=self.dead)):
            if count >= _MAX_EXPANSIONS or len(tried) >= _MAX_CANDIDATES:
                return
            if not _is_lookup(candidate):
                continue
            key = ast.dump(candidate)
            if key in tried:
                continue
            tried.add(key)
            if key not in self.resolved:
                try:
                    self.resolved[key] = self.resolve(candidate)
                except Exception:
                    self.resolved[key] = None
            path = self.resolved[key]
            if isinstance(path, str) and path in self.guarded:
                self._add({'line': line, 'call': _call_text(node),
                           'target': path})
                return

    def _add(self, finding: dict) -> None:
        key = (finding['line'], finding['call'])
        if key not in self._seen:
            self._seen.add(key)
            self.findings.append(finding)
