"""Pure Python TDN capability scanner for Embody.

This module intentionally imports no TouchDesigner modules. It accepts a parsed
TDN dict and returns the frozen C2 CapabilityJson shape from contracts.py.
"""
from __future__ import annotations

import ast
import json
import re


# ---- C2: capability ------------------------------------------------------------------------
SCAN_VERDICTS = ("clean", "flagged", "blocked")

# Must match CapabilityCounts keys in capability.ts, in the same order.
CAPABILITY_SURFACES = (
    "execute_dats",
    "file_read_exprs",
    "web_ops",
    "extensions",
    "storage_payloads",
    "denylisted_types",
    "traversal_paths",
    "external_refs",
)


def empty_capability_counts() -> dict:
    """A zeroed CapabilityCounts dict (all surfaces -> 0)."""
    return {k: 0 for k in CAPABILITY_SURFACES}




MAX_SERIALIZED_TDN_BYTES = 5 * 1024 * 1024
MAX_OPERATORS = 50000
MAX_AST_DEPTH = 80
MAX_AST_NODES = 10000
MAX_AST_SOURCE_CHARS = 200000
EVIDENCE_LIMIT = 200

DENYLIST_SEED_TYPES = frozenset(
    (
        "webclientDAT",
        "webserverDAT",
        "tcpipDAT",
        "udpinDAT",
        "udpoutDAT",
        "oscinDAT",
        "oscoutDAT",
        "serialDAT",
        "runDAT",
        "executeDAT",
        "datexecuteDAT",
        "chopexecuteDAT",
        "parameterexecuteDAT",
        "parametergroupexecuteDAT",
        "panelexecuteDAT",
        "opexecuteDAT",
        "moviefileinTOP",
        "moviefileoutTOP",
        "folderDAT",
        "touchinTOP",
        "touchoutTOP",
        "webRenderTOP",
        "ndi*",
        "syphonspout*",
    )
)

_DENYLIST_NORMALIZED = frozenset(
    re.sub(r"[^a-z0-9]", "", t.lower()) for t in DENYLIST_SEED_TYPES if not t.endswith("*")
)

# Script OPs run an attacker-authored Python callback on every cook. They are not
# IO/network ops, so they are tracked separately and counted as an execute surface
# (safe_import bypasses them).
_SCRIPT_OP_TYPES = frozenset((
    "scriptdat", "scriptchop", "scripttop", "scriptsop",
))

_BAD_NAMES = frozenset(
    (
        "eval",
        "exec",
        "compile",
        "__import__",
        "os",
        "sys",
        "subprocess",
        "socket",
        "shutil",
        "pathlib",
        "open",
        "requests",
        "urllib",
        "mod",
        "tdu",
        "getattr",
        "setattr",
        "globals",
        "locals",
    )
)
_BAD_MODULE_NAMES = frozenset(
    ("os", "sys", "subprocess", "socket", "shutil", "pathlib", "requests", "urllib")
)
_BAD_ATTRS = frozenset(("run", "save", "store"))
_DYNAMIC_ATTR_NAMES = frozenset(("getattr", "setattr", "globals", "locals"))
_PATH_PARAM_NAMES = frozenset(
    (
        "file",
        "syncfile",
        "filepath",
        "filename",
        "folder",
        "directory",
        "dir",
        "path",
    )
)
_PATH_STYLES = frozenset(("File", "FileSave", "Folder"))
_WINDOWS_ABS_RE = re.compile(r"^[A-Za-z]:[/\\]")

# TD's built-in palette/system components are reached through global op shortcuts
# named op.TD<Name> (TDAnnotate, TDResources, TDFunctions, TDJSON, ...). An extension
# that resolves through one of these is TD's own trusted code, NOT a stranger's --
# safe_import strips community opshortcut registration so these shortcuts cannot be
# hijacked to point at attacker code.
_TD_PALETTE_REF = re.compile(r"\bop\.TD[A-Z]\w*")


class _ScanState:
    def __init__(self, counts, findings):
        self.counts = counts
        self.findings = findings
        self.blocked = False


class _AstScanResult:
    def __init__(self, flagged=False, detail="", blocked=False):
        self.flagged = flagged
        self.detail = detail
        self.blocked = blocked


def scan_tdn(tdn: dict, scanner_version: str = "v6-scan-1") -> dict:
    """Return a C2 CapabilityJson dict for a parsed TDN payload."""
    counts = empty_capability_counts()
    findings = []

    serialized_size = _serialized_size(tdn)
    if serialized_size is None:
        findings.append(
            _finding(
                "/",
                "storage_payloads",
                "TDN could not be serialized safely for scanner bounds",
                "serialization failed",
            )
        )
        return _capability(scanner_version, "blocked", counts, findings)

    if serialized_size > MAX_SERIALIZED_TDN_BYTES:
        findings.append(
            _finding(
                "/",
                "storage_payloads",
                "Serialized TDN exceeds 5 MB scanner bound",
                "%d bytes" % serialized_size,
            )
        )
        return _capability(scanner_version, "blocked", counts, findings)

    too_many_ops, op_count = _operator_count_exceeds(tdn, MAX_OPERATORS)
    if too_many_ops:
        findings.append(
            _finding(
                "/",
                "denylisted_types",
                "TDN operator count exceeds scanner bound",
                "%d operators" % op_count,
            )
        )
        return _capability(scanner_version, "blocked", counts, findings)

    state = _ScanState(counts, findings)
    try:
        _scan_tdn_root(tdn, state)
    except Exception as exc:
        # FAIL CLOSED: an internal scan error must NOT be reported as clean - an aborted walk
        # may have missed surfaces. Treat it as blocked so an unverifiable payload is never
        # waved through. (Review finding, dimension 5/10.)
        state.blocked = True
        findings.append(
            _finding(
                "/",
                "execute_dats",
                "scanner aborted on internal error; failing closed (treat as unsafe)",
                type(exc).__name__,
            )
        )

    if state.blocked:
        verdict = "blocked"
    elif any(counts.get(surface, 0) > 0 for surface in CAPABILITY_SURFACES):
        verdict = "flagged"
    else:
        verdict = "clean"
    return _capability(scanner_version, verdict, counts, findings)


def _capability(scanner_version, verdict, counts, findings):
    return {
        "scanner_version": scanner_version,
        "verdict": verdict,
        "counts": {surface: int(counts.get(surface, 0)) for surface in CAPABILITY_SURFACES},
        "findings": findings,
    }


def _scan_tdn_root(tdn, state):
    if not isinstance(tdn, dict):
        return

    type_defaults = tdn.get("type_defaults")
    if not isinstance(type_defaults, dict):
        type_defaults = {}

    root_path = _root_path(tdn)
    _scan_operator_like(tdn, root_path, type_defaults, state)

    for child in _safe_list(tdn.get("operators")):
        if isinstance(child, dict):
            _scan_operator(child, root_path, type_defaults, state)


def _scan_operator(op_data, parent_path, type_defaults, state):
    name = op_data.get("name")
    child_path = _join_path(parent_path, name if isinstance(name, str) and name else "<unnamed>")
    _scan_operator_like(op_data, child_path, type_defaults, state)

    for child in _safe_list(op_data.get("children")):
        if isinstance(child, dict):
            _scan_operator(child, child_path, type_defaults, state)


def _scan_operator_like(op_data, op_path, type_defaults, state):
    if not isinstance(op_data, dict):
        return

    op_type = _safe_str(op_data.get("type"))
    params = _effective_parameters(op_data, type_defaults, op_type)

    if _is_denylisted_type(op_type):
        _add_count(
            state,
            op_path,
            "web_ops",
            "Operator type is an IO or network surface",
            op_type,
        )
        _add_count(
            state,
            op_path,
            "denylisted_types",
            "Operator type is on the scanner denylist",
            op_type,
        )

    if _is_script_op_type(op_type):
        _add_count(
            state,
            op_path,
            "execute_dats",
            "Script OP runs an authored Python callback on cook",
            op_type,
        )

    _scan_execute_dat(op_data, op_path, op_type, state)
    _scan_dat_content_ast(op_data, op_path, op_type, params, state)
    _scan_parameters(params, op_path, state)
    _scan_custom_parameters(op_data.get("custom_pars"), op_path, state)
    _scan_sequences(op_data.get("sequences"), op_path, op_type, state)
    _scan_storage(op_data, op_path, state)
    _scan_external_refs(op_data, op_path, state)


def _scan_external_refs(op_data, op_path, state):
    for key in ("tdn_ref", "tox_ref"):
        ref = op_data.get(key)
        if isinstance(ref, str) and ref.strip():
            _add_count(
                state,
                op_path,
                "external_refs",
                "COMP references external content via %s (not inlined, not scanned)" % key,
                ref,
            )


def _scan_execute_dat(op_data, op_path, op_type, state):
    if not _is_execute_dat_type(op_type):
        return

    content = op_data.get("dat_content")
    if not _has_dat_content(content):
        return

    _add_count(
        state,
        op_path,
        "execute_dats",
        "Execute-family DAT has non-empty content",
        _dat_content_to_text(content),
    )


# DAT file extensions whose content is NOT Python (shaders, markup, data). A GLSL
# pixel DAT often carries `extension: 'frag'` with no `language` param, so extension
# is a second signal beyond `language`.
_NON_PYTHON_DAT_EXTENSIONS = frozenset((
    "frag", "vert", "glsl", "comp", "geom", "tesc", "tese", "hlsl", "cg",
    "txt", "json", "xml", "csv", "tsv", "md", "html", "htm", "css", "js", "yaml", "yml",
))


def _dat_content_is_python(op_type, params):
    """True iff a DAT's content is Python that TD can execute.

    Execute-family DATs are always Python callbacks. Text DATs hold source whose
    language is Python (the textDAT default) unless an explicit non-Python language
    (e.g. 'glsl') OR a non-Python file extension (e.g. 'frag') is set -- a GLSL shader
    DAT is specimen content, not a Python execution surface, so AST-scanning it as
    Python wrongly flags every shader. Data DATs (table, etc.) are never Python.
    """
    if _is_execute_dat_type(op_type):
        return True
    key = _type_key(op_type)
    if key != "textdat":
        return False
    lang = params.get("language") if isinstance(params, dict) else None
    if isinstance(lang, str) and lang.strip():
        return lang.strip().lower() in ("python", "py")
    ext = params.get("extension") if isinstance(params, dict) else None
    if isinstance(ext, str) and ext.strip().lstrip(".").lower() in _NON_PYTHON_DAT_EXTENSIONS:
        return False
    return True  # textDAT default language is Python


def _scan_dat_content_ast(op_data, op_path, op_type, params, state):
    content = op_data.get("dat_content")
    if not isinstance(content, str) or not content.strip():
        return

    if not _dat_content_is_python(op_type, params):
        return

    result = _scan_python_source(content)
    if not result.flagged:
        return

    surface = "execute_dats"
    already_counted = _is_execute_dat_type(op_type) and _has_dat_content(content)
    if not already_counted:
        state.counts[surface] += 1

    if result.blocked:
        state.blocked = True

    state.findings.append(
        _finding(
            op_path,
            surface,
            result.detail or "DAT content references executable or IO surface",
            content,
        )
    )


def _scan_parameters(params, op_path, state):
    if not isinstance(params, dict):
        return

    for par_name, value in list(params.items()):
        _scan_parameter_value(par_name, value, op_path, state)
        _scan_path_parameter(par_name, value, op_path, state)


def _scan_parameter_value(par_name, value, op_path, state):
    expr = _expression_source(value)
    if expr is None:
        return

    # DoS bounds FIRST: a maliciously huge / deeply-nested expression must BLOCK
    # regardless of purity (the unbounded parse/walk is itself the attack). Reuse
    # the bounded AST walker only for its `blocked` signal; its denylist `flagged`
    # result is deliberately ignored in favor of the pure-value allowlist below.
    bounded = _scan_python_source(expr)
    if bounded.blocked:
        state.blocked = True
        state.findings.append(
            _finding(
                op_path,
                "file_read_exprs",
                "Expression parameter %s %s" % (_safe_str(par_name), bounded.detail),
                expr,
            )
        )
        return

    # A parameter expression is safe iff it is a PROVABLY PURE value expression
    # (par reads, absTime, math.*, Par.eval(), arithmetic). Anything that is not
    # provably pure -- a side-effecting call, dynamic attribute access, import,
    # lambda/comprehension/dunder escape -- counts as a danger surface. This
    # replaces the old denylist (which both false-flagged Par.eval()/.store()/tdu
    # AND waved through op('x').destroy(), getattr tricks, etc.).
    if is_pure_value_expression(expr):
        return

    state.counts["file_read_exprs"] += 1
    state.findings.append(
        _finding(
            op_path,
            "file_read_exprs",
            "Expression parameter %s is not a pure value expression" % _safe_str(par_name),
            expr,
        )
    )


def _scan_path_parameter(par_name, value, op_path, state):
    if not _is_path_param_name(par_name):
        return

    for text in _string_values(value):
        if _is_absolute_or_traversal_path(text):
            _add_count(
                state,
                op_path,
                "traversal_paths",
                "Path parameter %s is absolute or traverses upward" % _safe_str(par_name),
                text,
            )
            return


def _scan_custom_parameters(custom_pars, op_path, state):
    if isinstance(custom_pars, list):
        for item in custom_pars:
            if isinstance(item, dict):
                _scan_custom_parameter_def(item, op_path, state)
        return

    if not isinstance(custom_pars, dict):
        return

    for page in list(custom_pars.values()):
        if isinstance(page, list):
            for item in page:
                if isinstance(item, dict):
                    _scan_custom_parameter_def(item, op_path, state)
        elif isinstance(page, dict):
            for key, value in list(page.items()):
                if key == "$t":
                    continue
                _scan_parameter_value(key, value, op_path, state)
                _scan_path_parameter(key, value, op_path, state)


def _scan_custom_parameter_def(par_def, op_path, state):
    name = par_def.get("name")
    style = par_def.get("style")

    if "value" in par_def:
        _scan_parameter_value(name, par_def.get("value"), op_path, state)
        if style in _PATH_STYLES or _is_path_param_name(name):
            _scan_path_parameter(name, par_def.get("value"), op_path, state)

    values = par_def.get("values")
    if isinstance(values, list):
        for value in values:
            _scan_parameter_value(name, value, op_path, state)
            if style in _PATH_STYLES or _is_path_param_name(name):
                _scan_path_parameter(name, value, op_path, state)

    # default / menuSource can also carry expressions -- safe_import neutralizes
    # them, so the scanner must see them too (verdict/neutralization parity).
    for extra in ("default", "menuSource"):
        if extra in par_def:
            _scan_parameter_value(name, par_def.get(extra), op_path, state)


def _scan_sequences(sequences, op_path, op_type, state):
    if not isinstance(sequences, dict):
        return

    if _is_comp_type(op_type) and _sequence_has_extension(sequences.get("ext")):
        _add_count(
            state,
            op_path,
            "extensions",
            "COMP declares one or more extensions",
            sequences.get("ext"),
        )

    for sequence_name, blocks in list(sequences.items()):
        if not isinstance(blocks, list):
            continue
        for block in blocks:
            if not isinstance(block, dict):
                continue
            for key, value in list(block.items()):
                _scan_parameter_value(key, value, op_path, state)
                _scan_path_parameter(key, value, op_path, state)


def _scan_storage(op_data, op_path, state):
    for key in ("storage", "startup_storage"):
        payload = op_data.get(key)
        if _has_storage_payload(payload):
            _add_count(
                state,
                op_path,
                "storage_payloads",
                "Operator has non-empty %s" % key,
                payload,
            )


def _effective_parameters(op_data, type_defaults, op_type):
    params = {}
    defaults_for_type = type_defaults.get(op_type) if isinstance(type_defaults, dict) else None
    if isinstance(defaults_for_type, dict) and isinstance(defaults_for_type.get("parameters"), dict):
        params.update(defaults_for_type.get("parameters"))
    op_params = op_data.get("parameters")
    if isinstance(op_params, dict):
        params.update(op_params)
    return params


# ---- Pure-value expression policy (allowlist) ----------------------------------------------
# A TD parameter expression is Python evaluated for its VALUE on cook. The threat
# is never the math -- it is a side effect (op mutation, .run/.store, import, open,
# file/network). is_pure_value_expression is an ALLOWLIST: it returns True only for
# a closed grammar of provably side-effect-free value reads/computation, and FAILS
# CLOSED on anything it does not recognize (-> the caller neutralizes it). "Safe"
# means "pure value math", never "did not mention a denylisted token" -- a denylist
# is trivially defeated (lambda/walrus/dunder/subscript/getattr/f-string escapes).
# Shared as the single source of truth: scanner uses it for the verdict; safe_import
# is handed it (by CollectionExt) to decide which expressions to neutralize.

_PURE_ROOT_NAMES = frozenset((
    "parent", "me", "op", "absTime", "math", "tdu", "ipar", "iop",
))
# Bare-name builtins that are pure and never invoke a passed callable. Excludes
# higher-order builtins (map/filter/sorted) that could invoke a smuggled callable.
_PURE_BUILTINS = frozenset((
    "abs", "round", "int", "float", "str", "bool", "len", "pow", "sum",
    "divmod", "complex", "ord", "chr", "hex", "bin", "min", "max",
    # hasattr returns a bool (read-only introspection, cannot leak a callable the
    # way getattr can), so it is safe; getattr/setattr stay rejected.
    "hasattr",
))
# tdu helpers that are pure value math. Conservative; extend deliberately.
_TDU_VALUE_HELPERS = frozenset((
    "remap", "clamp", "Color", "Vector", "Position", "Quaternion", "Matrix",
    "rgbToHsv", "hsvToRgb", "legalName",
))
# Attribute names that open an execution / mutation surface -> never a pure read.
_FORBIDDEN_PURE_ATTRS = frozenset(("module", "mod", "storage"))


def is_pure_value_expression(source):
    """True iff source is a provably side-effect-free TD value expression.

    Allowlist + fail-closed: anything not recognized as a pure value read/compute
    returns False. Fixes the .eval()/.store()/tdu false positives (they are pure)
    AND blocks every code-execution bypass (they are not).
    """
    if not isinstance(source, str) or not source.strip():
        return False
    try:
        tree = ast.parse(source, mode="eval")
    except Exception:
        return False
    try:
        return _expr_is_pure(tree.body)
    except Exception:
        return False


def _expr_is_pure(node):
    t = type(node)
    if t is ast.Constant:
        return True
    if t is ast.BinOp:
        return _expr_is_pure(node.left) and _expr_is_pure(node.right)
    if t is ast.UnaryOp:
        return _expr_is_pure(node.operand)
    if t is ast.BoolOp:
        return all(_expr_is_pure(v) for v in node.values)
    if t is ast.Compare:
        return _expr_is_pure(node.left) and all(_expr_is_pure(c) for c in node.comparators)
    if t is ast.IfExp:
        return _expr_is_pure(node.test) and _expr_is_pure(node.body) and _expr_is_pure(node.orelse)
    if t in (ast.Tuple, ast.List, ast.Set):
        return all(_expr_is_pure(e) for e in node.elts)
    if t is ast.Dict:
        return (all(k is None or _expr_is_pure(k) for k in node.keys)
                and all(_expr_is_pure(v) for v in node.values))
    if t is ast.Subscript:
        return _expr_is_pure(node.value) and _expr_slice_is_pure(node.slice)
    if t is ast.Name:
        return node.id in _PURE_ROOT_NAMES
    if t is ast.Attribute:
        if node.attr.startswith("__") or node.attr in _FORBIDDEN_PURE_ATTRS:
            return False
        return _expr_is_pure(node.value)
    if t is ast.Call:
        return _expr_call_is_pure(node)
    # Lambda, NamedExpr (walrus), comprehensions, JoinedStr/FormattedValue (f-string),
    # Starred, Await/Yield, ... -> not provably pure.
    return False


def _expr_slice_is_pure(node):
    t = type(node)
    if t is ast.Slice:
        return all(x is None or _expr_is_pure(x) for x in (node.lower, node.upper, node.step))
    if hasattr(ast, "Index") and t is ast.Index:  # py<3.9
        return _expr_is_pure(node.value)
    return _expr_is_pure(node)


def _expr_call_is_pure(node):
    # No smuggling a callable via keyword (min(x, key=evil)) or *args/**kwargs.
    if node.keywords or any(isinstance(a, ast.Starred) for a in node.args):
        return False
    if not all(_expr_is_pure(a) for a in node.args):
        return False
    func = node.func
    if isinstance(func, ast.Name):
        return func.id in _PURE_BUILTINS or func.id in ("parent", "op")
    if isinstance(func, ast.Attribute):
        recv = func.value
        if func.attr == "eval":           # Par.eval() / value-eval on a pure receiver
            return _expr_is_pure(recv)
        if isinstance(recv, ast.Name) and recv.id == "math":   # math.* is all pure
            return True
        if isinstance(recv, ast.Name) and recv.id == "tdu" and func.attr in _TDU_VALUE_HELPERS:
            return True
        return False
    # func is a Subscript / Call / Lambda result -> reject.
    return False


def _scan_python_source(source):
    if not isinstance(source, str):
        return _AstScanResult()

    if not source.strip():
        return _AstScanResult()

    if len(source) > MAX_AST_SOURCE_CHARS:
        return _AstScanResult(True, "exceeds AST source length bound", True)

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return _AstScanResult(True, "is not parseable Python", False)
    except (RecursionError, MemoryError):
        return _AstScanResult(True, "exceeds AST parser bound", True)
    except Exception:
        return _AstScanResult(True, "could not be parsed safely", False)

    return _scan_ast_tree(tree)


def _scan_ast_tree(tree):
    stack = [(tree, 0)]
    seen_nodes = 0

    while stack:
        node, depth = stack.pop()
        seen_nodes += 1

        if depth > MAX_AST_DEPTH:
            return _AstScanResult(True, "exceeds AST depth bound", True)
        if seen_nodes > MAX_AST_NODES:
            return _AstScanResult(True, "exceeds AST node count bound", True)

        detail = _ast_node_detail(node)
        if detail:
            return _AstScanResult(True, detail, False)

        try:
            children = list(ast.iter_child_nodes(node))
        except Exception:
            children = []
        for child in reversed(children):
            stack.append((child, depth + 1))

    return _AstScanResult()


def _ast_node_detail(node):
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return "uses an import statement"

    if isinstance(node, ast.Name):
        if node.id in _BAD_NAMES:
            return "references %s" % node.id

    if isinstance(node, ast.Attribute):
        if node.attr in _BAD_ATTRS:
            return "references .%s" % node.attr
        root_name = _root_name(node)
        if root_name in _BAD_MODULE_NAMES or root_name in ("mod", "tdu"):
            return "references %s" % root_name

    if isinstance(node, ast.Call):
        call_name = _call_name(node.func)
        if call_name:
            base = call_name.split(".", 1)[0]
            leaf = call_name.rsplit(".", 1)[-1]
            if leaf in _DYNAMIC_ATTR_NAMES:
                return "uses dynamic attribute access"
            if base in _BAD_MODULE_NAMES or base in ("mod", "tdu"):
                return "calls %s" % call_name
            if leaf in ("eval", "exec", "compile", "__import__", "open"):
                return "calls %s" % leaf
            if leaf in _BAD_ATTRS:
                return "calls .%s" % leaf

    return ""


def _expression_source(value):
    if isinstance(value, str):
        if value.startswith("==") or value.startswith("~~"):
            return None
        if value.startswith("=") or value.startswith("~"):
            return value[1:]
        return None

    if isinstance(value, dict):
        expr = value.get("expr")
        if isinstance(expr, str):
            return expr
        bind = value.get("bind")
        if isinstance(bind, str):
            return bind

    return None


def _string_values(value):
    if isinstance(value, str):
        return (value,)
    if isinstance(value, dict):
        values = []
        for key in ("expr", "bind"):
            candidate = value.get(key)
            if isinstance(candidate, str):
                values.append(candidate)
        return tuple(values)
    if isinstance(value, list):
        result = []
        for item in value:
            if isinstance(item, str):
                result.append(item)
        return tuple(result)
    return ()


def _is_absolute_or_traversal_path(value):
    if not isinstance(value, str):
        return False

    text = value.strip().strip("'\"")
    if not text:
        return False
    if text.startswith("=") and not text.startswith("=="):
        text = text[1:].strip().strip("'\"")
    if text.startswith("~") and not text.startswith("~~"):
        text = text[1:].strip().strip("'\"")

    normalized = text.replace("\\", "/")
    if normalized.startswith("/") or normalized.startswith("//"):
        return True
    if _WINDOWS_ABS_RE.match(text):
        return True

    parts = [part for part in normalized.split("/") if part]
    return ".." in parts


def _is_path_param_name(name):
    if not isinstance(name, str):
        return False
    key = name.lower()
    if key in _PATH_PARAM_NAMES:
        return True
    return key.endswith("file") or key.endswith("path") or key.endswith("folder")


def _is_td_palette_ref(text):
    return isinstance(text, str) and bool(_TD_PALETTE_REF.search(text))


def _sequence_has_extension(ext_sequence):
    """True iff a FOREIGN (non-TD-palette) extension is declared.

    An extension whose object resolves through a TD palette/system shortcut
    (op.TD<Name>) is TD's own trusted code -- not a danger surface. A declaration
    with no recognizable trusted object (or only a name) is treated as foreign.
    """
    if not isinstance(ext_sequence, list):
        return False

    for block in ext_sequence:
        if not isinstance(block, dict):
            continue
        obj = block.get("object")
        if isinstance(obj, str) and obj.strip():
            if not _is_td_palette_ref(obj):
                return True
        elif isinstance(block.get("name"), str) and block.get("name").strip():
            return True  # a name with no verifiable trusted object -> foreign
    return False


def _has_storage_payload(payload):
    if isinstance(payload, dict):
        return len(payload) > 0
    if isinstance(payload, (list, tuple, set)):
        return len(payload) > 0
    if isinstance(payload, str):
        return bool(payload.strip())
    return payload is not None


def _has_dat_content(content):
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        for row in content:
            if isinstance(row, list):
                if any(isinstance(cell, str) and cell.strip() for cell in row):
                    return True
            elif isinstance(row, str) and row.strip():
                return True
    return False


def _dat_content_to_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        rows = []
        for row in content:
            if isinstance(row, list):
                rows.append("\t".join(_safe_str(cell) for cell in row))
            else:
                rows.append(_safe_str(row))
        return "\n".join(rows)
    return _safe_str(content)


def _add_count(state, op_path, surface, detail, evidence):
    if surface not in CAPABILITY_SURFACES:
        return
    state.counts[surface] += 1
    state.findings.append(_finding(op_path, surface, detail, evidence))


def _finding(op_path, surface, detail, evidence):
    return {
        "op_path": _safe_str(op_path) or "/",
        "surface": surface,
        "detail": _safe_str(detail),
        "evidence": _evidence(evidence),
    }


def _evidence(value):
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        except Exception:
            text = repr(value)
    text = " ".join(text.split())
    if len(text) > EVIDENCE_LIMIT:
        return text[: EVIDENCE_LIMIT - 3] + "..."
    return text


def _serialized_size(value):
    try:
        text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return len(text.encode("utf-8"))
    except (TypeError, ValueError, RecursionError, MemoryError):
        return None


def _operator_count_exceeds(tdn, cap):
    if not isinstance(tdn, dict):
        return False, 0

    count = 1
    stack = []
    stack.extend(reversed(_safe_list(tdn.get("operators"))))
    seen = set()

    while stack:
        item = stack.pop()
        if not isinstance(item, dict):
            continue
        item_id = id(item)
        if item_id in seen:
            continue
        seen.add(item_id)
        count += 1
        if count > cap:
            return True, count
        stack.extend(reversed(_safe_list(item.get("children"))))

    return False, count


def _is_denylisted_type(op_type):
    key = _type_key(op_type)
    if key in _DENYLIST_NORMALIZED:
        return True
    if key.endswith("executedat"):
        return True
    if key.startswith("ndi"):
        return True
    if key.startswith("syphonspout"):
        return True
    if key.startswith("web") and (key.endswith("dat") or key.endswith("top")):
        return True
    return False


def _is_execute_dat_type(op_type):
    key = _type_key(op_type)
    return key == "executedat" or key.endswith("executedat")


def _is_script_op_type(op_type):
    return _type_key(op_type) in _SCRIPT_OP_TYPES


def _is_comp_type(op_type):
    return _type_key(op_type).endswith("comp")


def _type_key(value):
    if not isinstance(value, str):
        return ""
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _root_name(node):
    current = node
    while isinstance(current, ast.Attribute):
        current = current.value
    if isinstance(current, ast.Call):
        return _root_name(current.func)
    if isinstance(current, ast.Name):
        return current.id
    return ""


def _call_name(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _call_name(node.value)
        if prefix:
            return "%s.%s" % (prefix, node.attr)
        return node.attr
    if isinstance(node, ast.Call):
        return _call_name(node.func)
    return ""


def _root_path(tdn):
    network_path = tdn.get("network_path") if isinstance(tdn, dict) else None
    if isinstance(network_path, str) and network_path:
        return network_path
    name = tdn.get("name") if isinstance(tdn, dict) else None
    if isinstance(name, str) and name:
        return name
    return "/"


def _join_path(parent_path, child_name):
    parent = _safe_str(parent_path)
    child = _safe_str(child_name) or "<unnamed>"
    if not parent or parent == "/":
        return child
    return parent.rstrip("/") + "/" + child


def _safe_list(value):
    return value if isinstance(value, list) else []


def _safe_str(value):
    if isinstance(value, str):
        return value
    try:
        return str(value)
    except Exception:
        return ""
