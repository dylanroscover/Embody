"""Default-inert TDN import transform.

This module is intentionally headless: it imports no TouchDesigner modules and
operates only on parsed TDN dictionaries.
"""
from __future__ import annotations

import copy
import re

# TD built-in palette/system components reached via op.TD<Name> global shortcuts are
# trusted code. An extension resolving through one is NOT disabled -- but only because
# community opshortcut registration is stripped below, so these shortcuts cannot be
# hijacked to point at attacker code.
_TD_PALETTE_REF = re.compile(r"\bop\.TD[A-Z]\w*")


def _is_td_palette_ref(text):
    return isinstance(text, str) and bool(_TD_PALETTE_REF.search(text))


EXECUTE_DAT_TYPES = {
    "executedat",
    "datexecutedat",
    "chopexecutedat",
    "parameterexecutedat",
    "panelexecutedat",
    "execute",
    "datexecute",
    "chopexecute",
    "parameterexecute",
    "panelexecute",
}

IO_DENYLIST_TYPES = {
    "audiodeviceinchop",
    "audiodeviceoutchop",
    "audiofileinchop",
    "audiofileoutchop",
    "chopexecutedat",
    "datexecutedat",
    "executedat",
    "fileindat",
    "fileoutdat",
    "folderdat",
    "moviefileintop",
    "moviefileouttop",
    "oscindat",
    "oscoutdat",
    "parameterexecutedat",
    "panelexecutedat",
    "pipeindat",
    "pipeoutdat",
    "rundat",
    "serialdat",
    "sharedmemintop",
    "sharedmemouttop",
    "socketiodat",
    "tcpipdat",
    "touchindat",
    "touchoutdat",
    "touchinchop",
    "touchoutchop",
    "touchintop",
    "touchouttop",
    "udpindat",
    "udpoutdat",
    "videodeviceintop",
    "webclientdat",
    "webrendertop",
    "webserverdat",
}

IO_DENYLIST_PREFIXES = (
    "ndi",
    "syphonspout",
)

# Script OPs run an attacker-authored Python callback on every cook -- a code-
# execution surface distinct from Execute DATs and IO. Bypassed so they never cook.
SCRIPT_OP_TYPES = {
    "scriptdat",
    "scriptchop",
    "scripttop",
    "scriptsop",
}

# Out-of-band references the importer would otherwise restore (TOX/TDN shells).
# Their content cannot be scanned inline, so for community paste they are removed.
EXTERNAL_REF_KEYS = ("tox_ref", "tdn_ref")

SUMMARY_KEYS = (
    "execute_dats_disabled",
    "exprs_neutralized",
    "extensions_disabled",
    "global_shortcuts_stripped",
    "io_ops_bypassed",
    "script_ops_bypassed",
    "external_refs_stripped",
    "storage_removed",
)

_MISSING = object()

# The pure-value-expression predicate (scanner.is_pure_value_expression), injected
# per make_inert() call by CollectionExt. Set only inside make_inert's try/finally
# and read by _neutralized_value -- TD runs this on a single thread, so a module
# slot is safe and avoids threading the predicate through 9 functions. None ->
# fail closed (neutralize EVERY expression), the original disarm-everything behavior.
_PRESERVE_PURE = None


def _expr_source(value):
    """Python source of an =expr / ~bind value (or {expr|bind} dict), else None."""
    if isinstance(value, str):
        if value.startswith("==") or value.startswith("~~"):
            return None
        if value.startswith("=") or value.startswith("~"):
            return value[1:]
        return None
    if isinstance(value, dict):
        e = value.get("expr")
        if isinstance(e, str):
            return e
        b = value.get("bind")
        if isinstance(b, str):
            return b
    return None


def _is_dangerous_expression(value):
    """True iff value is an expression/bind that is NOT provably pure.

    With no purity predicate set, every expression counts as dangerous (the
    original conservative meaning). With one (is_inert/make_inert injected it),
    a provably-pure value expression is NOT dangerous.
    """
    if not _is_expression_or_bind(value):
        return False
    if _PRESERVE_PURE is None:
        return True
    src = _expr_source(value)
    return src is None or not _PRESERVE_PURE(src)

NUMERIC_STYLES = {
    "Float",
    "Int",
    "XY",
    "XYZ",
    "XYZW",
    "WH",
    "UV",
    "UVW",
    "RGB",
    "RGBA",
}

STRING_STYLES = {
    "Str",
    "Menu",
    "StrMenu",
    "File",
    "FileSave",
    "Folder",
    "Python",
    "OP",
    "COMP",
    "TOP",
    "CHOP",
    "SOP",
    "DAT",
    "MAT",
    "POP",
    "Object",
    "PanelCOMP",
}

BOOL_PARAM_NAMES = {
    "active",
    "allowcooking",
    "borders",
    "borderover",
    "bypass",
    "clampmax",
    "clampmin",
    "display",
    "enable",
    "enabled",
    "enablecloning",
    "includedialog",
    "lock",
    "promote",
    "reloadbuiltin",
    "render",
    "startsection",
    "syncfile",
    "toggle",
    "viewer",
}

STRING_PARAM_PARTS = (
    "align",
    "callback",
    "class",
    "clone",
    "comment",
    "comp",
    "dat",
    "expr",
    "extension",
    "file",
    "folder",
    "label",
    "language",
    "mat",
    "menu",
    "name",
    "object",
    "opviewer",
    "owner",
    "parent",
    "path",
    "pop",
    "root",
    "scope",
    "shortcut",
    "sop",
    "source",
    "target",
    "text",
    "title",
    "top",
)


def make_inert(tdn: dict, is_pure_expr=None) -> tuple[dict, dict]:
    """Return a deep-copied TDN dict with auto-executable surfaces disabled.

    is_pure_expr: optional predicate(source)->bool. When given (CollectionExt injects
    scanner.is_pure_value_expression), a parameter expression that is a PROVABLY PURE
    value expression (par reads, absTime, math.*, Par.eval(), arithmetic) is PRESERVED
    -- only side-effecting / non-pure expressions are neutralized. When None, every
    expression is neutralized (fail-closed, the original behavior).
    """
    global _PRESERVE_PURE
    inert_tdn = copy.deepcopy(tdn)
    summary = _empty_summary()

    if not isinstance(inert_tdn, dict):
        return inert_tdn, summary

    _PRESERVE_PURE = is_pure_expr
    try:
        type_defaults = _type_defaults(inert_tdn)
        original_type_defaults = copy.deepcopy(type_defaults)
        _neutralize_type_defaults(inert_tdn, summary)
        _neutralize_par_templates(inert_tdn, summary)
        _neutralize_node(
            inert_tdn,
            _root_path(inert_tdn),
            type_defaults,
            original_type_defaults,
            summary,
        )

        operators = inert_tdn.get("operators")
        if isinstance(operators, list):
            for op_def in operators:
                _walk_operator(
                    op_def,
                    _root_path(inert_tdn),
                    type_defaults,
                    original_type_defaults,
                    summary,
                )
    finally:
        _PRESERVE_PURE = None

    return inert_tdn, summary


def is_inert(tdn: dict, is_pure_expr=None) -> bool:
    """Return True iff the TDN contains no known live executable surface.

    is_pure_expr: same predicate make_inert takes. When given, provably-pure value
    expressions do NOT count as a live surface (they are preserved by make_inert),
    so a network whose only expressions are pure value reads is inert. When None,
    any expression counts as live (the original strict meaning).
    """
    global _PRESERVE_PURE
    try:
        if not isinstance(tdn, dict):
            return False
        _PRESERVE_PURE = is_pure_expr
        try:
            type_defaults = _type_defaults(tdn)
            if _has_type_default_expression(tdn):
                return False
            if _has_par_template_expression(tdn):
                return False
            if _node_has_live_surface(tdn, type_defaults, is_root=True):
                return False

            operators = tdn.get("operators")
            if isinstance(operators, list):
                for op_def in operators:
                    if _operator_tree_has_live_surface(op_def, type_defaults):
                        return False
            return True
        finally:
            _PRESERVE_PURE = None
    except Exception:
        return False


def _empty_summary() -> dict:
    summary = {key: 0 for key in SUMMARY_KEYS}
    summary["details"] = []
    return summary


def _root_path(tdn: dict) -> str:
    path = tdn.get("network_path") if isinstance(tdn, dict) else None
    return path if isinstance(path, str) and path else "/"


def _type_defaults(tdn: dict) -> dict:
    type_defaults = tdn.get("type_defaults") if isinstance(tdn, dict) else None
    return type_defaults if isinstance(type_defaults, dict) else {}


def _walk_operator(
    op_def,
    parent_path: str,
    type_defaults: dict,
    original_type_defaults: dict,
    summary: dict,
) -> None:
    if not isinstance(op_def, dict):
        return

    op_path = _join_path(parent_path, op_def.get("name"))
    _neutralize_node(op_def, op_path, type_defaults, original_type_defaults, summary)

    children = op_def.get("children")
    if isinstance(children, list):
        for child in children:
            _walk_operator(child, op_path, type_defaults, original_type_defaults, summary)


def _neutralize_node(
    node: dict,
    op_path: str,
    type_defaults: dict,
    original_type_defaults: dict,
    summary: dict,
) -> None:
    op_type = _op_type(node)
    original_active = _effective_param(node, original_type_defaults, "active")
    was_disabled = _is_operator_disabled(node, original_type_defaults)

    _remove_storage(node, op_path, summary)
    _disable_extensions(node, op_path, summary)
    _strip_external_refs(node, op_path, summary)
    _strip_global_shortcuts(node, op_path, summary)
    _neutralize_parameter_mapping(
        node.get("parameters"),
        op_path,
        "parameters",
        summary,
    )
    _neutralize_custom_pars(node.get("custom_pars"), op_path, "custom_pars", summary)
    _neutralize_sequences(node.get("sequences"), op_path, summary)

    if _is_execute_dat_type(op_type) and not _is_off_constant(original_active):
        if _set_active_off(node):
            summary["execute_dats_disabled"] += 1
            _detail(
                summary,
                op_path,
                "execute_dat_disabled",
                original=original_active,
            )
        else:
            _ensure_bypass(node)
            summary["execute_dats_disabled"] += 1
            _detail(
                summary,
                op_path,
                "execute_dat_bypassed",
                original=original_active,
            )

    if _is_io_type(op_type) and not _is_execute_dat_type(op_type) and not was_disabled:
        if not _is_operator_disabled(node, type_defaults):
            _ensure_bypass(node)
        action = "io_op_bypassed"
        summary["io_ops_bypassed"] += 1
        _detail(summary, op_path, action, original=op_type)

    if _is_script_op_type(op_type) and not was_disabled:
        if not _is_operator_disabled(node, type_defaults):
            _ensure_bypass(node)
        summary["script_ops_bypassed"] += 1
        _detail(summary, op_path, "script_op_bypassed", original=op_type)


def _is_script_op_type(op_type) -> bool:
    return _normal_type(op_type) in SCRIPT_OP_TYPES


def _strip_external_refs(node: dict, op_path: str, summary: dict) -> None:
    for key in EXTERNAL_REF_KEYS:
        if key in node:
            original = node.pop(key)
            summary["external_refs_stripped"] += 1
            _detail(
                summary,
                op_path,
                "external_ref_stripped",
                original={key: original},
            )


def _strip_global_shortcuts(node: dict, op_path: str, summary: dict) -> None:
    """Remove a global op-shortcut registration (opshortcut). Untrusted content must
    not register project-wide op.X names: it is namespace pollution, and -- with the
    TD-palette extension allowlist -- a hijack vector (registering op.TDAnnotate to
    repoint a trusted ref at attacker code). Scoped parentshortcut is left intact."""
    params = node.get("parameters")
    if isinstance(params, dict) and params.get("opshortcut"):
        original = params.pop("opshortcut")
        summary["global_shortcuts_stripped"] += 1
        _detail(
            summary,
            op_path,
            "global_shortcut_stripped",
            original={"opshortcut": original},
        )


def _remove_storage(node: dict, op_path: str, summary: dict) -> None:
    for key in ("storage", "startup_storage"):
        if key in node:
            original = node.pop(key)
            summary["storage_removed"] += 1
            _detail(
                summary,
                op_path,
                "storage_removed",
                original={key: original},
            )


def _disable_extensions(node: dict, op_path: str, summary: dict) -> None:
    sequences = node.get("sequences")
    if isinstance(sequences, dict):
        ext_blocks = sequences.get("ext")
        if isinstance(ext_blocks, list):
            disabled_blocks = []
            changed = False
            originals = []
            for block in ext_blocks:
                if _is_enabled_extension_block(block):
                    originals.append(copy.deepcopy(block))
                    disabled_blocks.append({})
                    changed = True
                else:
                    disabled_blocks.append(block)
            if changed:
                sequences["ext"] = disabled_blocks
                summary["extensions_disabled"] += len(originals)
                _detail(
                    summary,
                    op_path,
                    "extensions_disabled",
                    original=originals,
                )

    for key in ("extensions", "extension_declarations", "td_extensions"):
        value = node.get(key)
        if value:
            original = node.pop(key)
            summary["extensions_disabled"] += 1
            _detail(
                summary,
                op_path,
                "legacy_extensions_removed",
                original={key: original},
            )


def _neutralize_type_defaults(tdn: dict, summary: dict) -> None:
    type_defaults = tdn.get("type_defaults")
    if not isinstance(type_defaults, dict):
        return

    for op_type, defaults in type_defaults.items():
        if not isinstance(defaults, dict):
            continue
        op_path = f"type_defaults/{op_type}"
        _neutralize_parameter_mapping(
            defaults.get("parameters"),
            op_path,
            "parameters",
            summary,
        )


def _neutralize_par_templates(tdn: dict, summary: dict) -> None:
    templates = tdn.get("par_templates")
    if not isinstance(templates, dict):
        return

    for template_name, defs in templates.items():
        _neutralize_custom_par_defs(
            defs,
            f"par_templates/{template_name}",
            "par_templates",
            summary,
        )


def _neutralize_parameter_mapping(
    params,
    op_path: str,
    location: str,
    summary: dict,
) -> None:
    if not isinstance(params, dict):
        return

    for par_name, value in list(params.items()):
        replacement = _neutralized_value(
            par_name,
            value,
            style=None,
            existing_default=_MISSING,
        )
        if replacement.changed:
            params[par_name] = replacement.value
            summary["exprs_neutralized"] += 1
            _detail(
                summary,
                op_path,
                "expression_neutralized",
                original=replacement.original,
                field=f"{location}.{par_name}",
            )


def _neutralize_custom_pars(custom_pars, op_path: str, location: str, summary: dict) -> None:
    if isinstance(custom_pars, list):
        _neutralize_custom_par_defs(custom_pars, op_path, location, summary)
        return

    if not isinstance(custom_pars, dict):
        return

    for page_name, page in custom_pars.items():
        page_location = f"{location}.{page_name}"
        if isinstance(page, list):
            _neutralize_custom_par_defs(page, op_path, page_location, summary)
        elif isinstance(page, dict):
            if "$t" in page:
                for par_name, value in list(page.items()):
                    if par_name == "$t":
                        continue
                    replacement = _neutralized_value(
                        par_name,
                        value,
                        style=None,
                        existing_default=_MISSING,
                    )
                    if replacement.changed:
                        page[par_name] = replacement.value
                        summary["exprs_neutralized"] += 1
                        _detail(
                            summary,
                            op_path,
                            "expression_neutralized",
                            original=replacement.original,
                            field=f"{page_location}.{par_name}",
                        )
            else:
                _neutralize_custom_par_defs(
                    list(page.values()),
                    op_path,
                    page_location,
                    summary,
                )


def _neutralize_custom_par_defs(defs, op_path: str, location: str, summary: dict) -> None:
    if not isinstance(defs, list):
        return

    for index, par_def in enumerate(defs):
        if not isinstance(par_def, dict):
            continue
        par_name = par_def.get("name")
        if not isinstance(par_name, str) or not par_name:
            par_name = str(index)
        style = par_def.get("style") if isinstance(par_def.get("style"), str) else None
        existing_default = par_def.get("default")
        base_field = f"{location}.{par_name}"

        if "value" in par_def:
            replacement = _neutralized_value(
                par_name,
                par_def.get("value"),
                style=style,
                existing_default=existing_default if "default" in par_def else _MISSING,
            )
            if replacement.changed:
                par_def["value"] = replacement.value
                summary["exprs_neutralized"] += 1
                _detail(
                    summary,
                    op_path,
                    "expression_neutralized",
                    original=replacement.original,
                    field=f"{base_field}.value",
                )

        values = par_def.get("values")
        if isinstance(values, list):
            for value_index, value in enumerate(list(values)):
                replacement = _neutralized_value(
                    par_name,
                    value,
                    style=style,
                    existing_default=existing_default if "default" in par_def else _MISSING,
                )
                if replacement.changed:
                    values[value_index] = replacement.value
                    summary["exprs_neutralized"] += 1
                    _detail(
                        summary,
                        op_path,
                        "expression_neutralized",
                        original=replacement.original,
                        field=f"{base_field}.values.{value_index}",
                    )

        for extra_field in ("default", "menuSource"):
            if extra_field in par_def:
                replacement = _neutralized_value(
                    par_name,
                    par_def.get(extra_field),
                    style=style,
                    existing_default=_MISSING,
                )
                if replacement.changed:
                    par_def[extra_field] = replacement.value
                    summary["exprs_neutralized"] += 1
                    _detail(
                        summary,
                        op_path,
                        "expression_neutralized",
                        original=replacement.original,
                        field=f"{base_field}.{extra_field}",
                    )


def _neutralize_sequences(sequences, op_path: str, summary: dict) -> None:
    if not isinstance(sequences, dict):
        return

    for seq_name, blocks in sequences.items():
        if not isinstance(blocks, list):
            continue
        for index, block in enumerate(blocks):
            if not isinstance(block, dict):
                continue
            for par_name, value in list(block.items()):
                replacement = _neutralized_value(
                    par_name,
                    value,
                    style=None,
                    existing_default=_MISSING,
                )
                if replacement.changed:
                    block[par_name] = replacement.value
                    summary["exprs_neutralized"] += 1
                    _detail(
                        summary,
                        op_path,
                        "expression_neutralized",
                        original=replacement.original,
                        field=f"sequences.{seq_name}.{index}.{par_name}",
                    )


class _Replacement:
    def __init__(self, changed: bool, value=None, original=None):
        self.changed = changed
        self.value = value
        self.original = original


def _neutralized_value(
    par_name: str,
    value,
    style: str | None,
    existing_default,
) -> _Replacement:
    if _is_expression_or_bind(value):
        # Preserve a provably PURE value expression (par reads, absTime, math.*,
        # Par.eval(), arithmetic) when a purity predicate was injected. Everything
        # not provably pure -- side-effecting calls, imports, dynamic-attr escapes
        # -- is neutralized. With no predicate, every expression is neutralized.
        if _PRESERVE_PURE is not None:
            src = _expr_source(value)
            if src is not None and _PRESERVE_PURE(src):
                return _Replacement(False)
        return _Replacement(
            True,
            _safe_constant(par_name, style, existing_default),
            _original_expression(value),
        )
    return _Replacement(False)


def _is_expression_or_bind(value) -> bool:
    if isinstance(value, str):
        return _is_mode_string(value)
    if isinstance(value, dict):
        return isinstance(value.get("expr"), str) or isinstance(value.get("bind"), str)
    return False


def _is_mode_string(value: str) -> bool:
    if value.startswith("==") or value.startswith("~~"):
        return False
    return value.startswith("=") or value.startswith("~")


def _original_expression(value):
    if isinstance(value, dict):
        if isinstance(value.get("expr"), str):
            return "=" + value.get("expr")
        if isinstance(value.get("bind"), str):
            return "~" + value.get("bind")
    return value


def _safe_constant(par_name: str, style: str | None, existing_default):
    if existing_default is not _MISSING and not _is_expression_or_bind(existing_default) and _is_json_scalar(existing_default):
        return copy.deepcopy(existing_default)

    if style == "Toggle":
        return False
    if style in STRING_STYLES:
        return ""
    if style in NUMERIC_STYLES:
        return 0

    name = str(par_name or "").lower()
    if name in BOOL_PARAM_NAMES:
        return False
    if name.startswith(("enable", "use", "allow", "is", "has", "show")):
        return False
    if any(part in name for part in STRING_PARAM_PARTS):
        return ""
    return 0


def _is_json_scalar(value) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def _op_type(op_def: dict) -> str:
    value = op_def.get("type") if isinstance(op_def, dict) else None
    return value if isinstance(value, str) else ""


def _normal_type(op_type: str) -> str:
    return "".join(ch for ch in str(op_type).lower() if ch.isalnum())


def _is_execute_dat_type(op_type: str) -> bool:
    return _normal_type(op_type) in EXECUTE_DAT_TYPES


def _is_io_type(op_type: str) -> bool:
    normal = _normal_type(op_type)
    if normal in IO_DENYLIST_TYPES:
        return True
    return any(normal.startswith(prefix) for prefix in IO_DENYLIST_PREFIXES)


def _effective_param(node: dict, type_defaults: dict, par_name: str):
    params = node.get("parameters")
    if isinstance(params, dict) and par_name in params:
        return params.get(par_name)

    defaults = _defaults_for(node, type_defaults)
    default_params = defaults.get("parameters") if isinstance(defaults, dict) else None
    if isinstance(default_params, dict) and par_name in default_params:
        return default_params.get(par_name)
    return None


def _defaults_for(node: dict, type_defaults: dict):
    op_type = node.get("type") if isinstance(node, dict) else None
    if not isinstance(op_type, str):
        return {}
    defaults = type_defaults.get(op_type)
    return defaults if isinstance(defaults, dict) else {}


def _set_active_off(node: dict) -> bool:
    params = node.get("parameters")
    if params is None:
        node["parameters"] = {"active": False}
        return True
    if isinstance(params, dict):
        params["active"] = False
        return True
    return False


def _is_off_constant(value) -> bool:
    if value is False:
        return True
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value == 0
    if isinstance(value, str):
        return value.strip().lower() in ("0", "false", "off")
    return False


def _is_operator_disabled(node: dict, type_defaults: dict) -> bool:
    return _has_bypass(node, type_defaults) or _is_off_constant(
        _effective_param(node, type_defaults, "active")
    )


def _has_bypass(node: dict, type_defaults: dict) -> bool:
    flags = node.get("flags")
    if flags is None:
        defaults = _defaults_for(node, type_defaults)
        flags = defaults.get("flags") if isinstance(defaults, dict) else None
    return _flag_enabled(flags, "bypass", default=False)


def _flag_enabled(flags, name: str, default: bool) -> bool:
    value = default
    if isinstance(flags, list):
        for entry in flags:
            if not isinstance(entry, str):
                continue
            if entry == name:
                value = True
            elif entry == "-" + name:
                value = False
    elif isinstance(flags, dict):
        raw = flags.get(name, default)
        if isinstance(raw, bool):
            value = raw
    return value


def _ensure_bypass(node: dict) -> bool:
    flags = node.get("flags")
    if isinstance(flags, list):
        filtered = [entry for entry in flags if entry != "-bypass"]
        if "bypass" not in filtered:
            filtered.append("bypass")
        node["flags"] = filtered
        return True
    if isinstance(flags, dict):
        flags["bypass"] = True
        return True
    node["flags"] = ["bypass"]
    return True


def _is_enabled_extension_block(block) -> bool:
    """True for a FOREIGN enabled extension (one safe_import must disable).

    An extension whose object resolves through a TD palette/system shortcut
    (op.TD<Name>) is trusted -- not disabled, not counted as a live surface. This
    is safe only because community opshortcut registration is stripped, so the
    shortcut cannot be repointed at attacker code."""
    if not isinstance(block, dict):
        return False
    value = block.get("object")
    if not isinstance(value, str) or not value.strip():
        return False
    return not _is_td_palette_ref(value)


def _has_type_default_expression(tdn: dict) -> bool:
    type_defaults = tdn.get("type_defaults")
    if not isinstance(type_defaults, dict):
        return False
    for defaults in type_defaults.values():
        if isinstance(defaults, dict) and _mapping_has_expression(defaults.get("parameters")):
            return True
    return False


def _has_par_template_expression(tdn: dict) -> bool:
    templates = tdn.get("par_templates")
    if not isinstance(templates, dict):
        return False
    for defs in templates.values():
        if _custom_defs_have_expression(defs):
            return True
    return False


def _operator_tree_has_live_surface(op_def, type_defaults: dict) -> bool:
    if not isinstance(op_def, dict):
        return False
    if _node_has_live_surface(op_def, type_defaults, is_root=False):
        return True
    children = op_def.get("children")
    if isinstance(children, list):
        for child in children:
            if _operator_tree_has_live_surface(child, type_defaults):
                return True
    return False


def _node_has_live_surface(node: dict, type_defaults: dict, is_root: bool) -> bool:
    if _has_storage(node):
        return True
    if _has_enabled_extension(node):
        return True
    if any(node.get(key) for key in EXTERNAL_REF_KEYS):
        return True
    params = node.get("parameters")
    if isinstance(params, dict) and params.get("opshortcut"):
        return True
    if _mapping_has_expression(node.get("parameters")):
        return True
    if _custom_pars_have_expression(node.get("custom_pars")):
        return True
    if _sequences_have_expression(node.get("sequences")):
        return True

    op_type = _op_type(node)
    if not is_root and _is_execute_dat_type(op_type):
        if not _is_off_constant(_effective_param(node, type_defaults, "active")):
            return True
    if not is_root and _is_io_type(op_type):
        if not _is_operator_disabled(node, type_defaults):
            return True
    if not is_root and _is_script_op_type(op_type):
        if not _is_operator_disabled(node, type_defaults):
            return True
    return False


def _has_storage(node: dict) -> bool:
    return "storage" in node or "startup_storage" in node


def _has_enabled_extension(node: dict) -> bool:
    sequences = node.get("sequences")
    if isinstance(sequences, dict):
        ext_blocks = sequences.get("ext")
        if isinstance(ext_blocks, list):
            for block in ext_blocks:
                if _is_enabled_extension_block(block):
                    return True
    return bool(node.get("extensions") or node.get("extension_declarations") or node.get("td_extensions"))


def _mapping_has_expression(params) -> bool:
    if not isinstance(params, dict):
        return False
    return any(_is_dangerous_expression(value) for value in params.values())


def _custom_pars_have_expression(custom_pars) -> bool:
    if isinstance(custom_pars, list):
        return _custom_defs_have_expression(custom_pars)
    if not isinstance(custom_pars, dict):
        return False
    for page in custom_pars.values():
        if isinstance(page, list):
            if _custom_defs_have_expression(page):
                return True
        elif isinstance(page, dict):
            if "$t" in page:
                for key, value in page.items():
                    if key != "$t" and _is_dangerous_expression(value):
                        return True
            elif _custom_defs_have_expression(list(page.values())):
                return True
    return False


def _custom_defs_have_expression(defs) -> bool:
    if not isinstance(defs, list):
        return False
    for par_def in defs:
        if not isinstance(par_def, dict):
            continue
        if _is_dangerous_expression(par_def.get("value")):
            return True
        if _is_dangerous_expression(par_def.get("default")):
            return True
        if _is_dangerous_expression(par_def.get("menuSource")):
            return True
        values = par_def.get("values")
        if isinstance(values, list):
            for value in values:
                if _is_dangerous_expression(value):
                    return True
    return False


def _sequences_have_expression(sequences) -> bool:
    if not isinstance(sequences, dict):
        return False
    for blocks in sequences.values():
        if not isinstance(blocks, list):
            continue
        for block in blocks:
            if isinstance(block, dict) and _mapping_has_expression(block):
                return True
    return False


def _join_path(parent_path: str, name) -> str:
    safe_name = name if isinstance(name, str) and name else "<unnamed>"
    if not isinstance(parent_path, str) or not parent_path:
        parent_path = "/"
    if parent_path == "/":
        return "/" + safe_name
    return parent_path.rstrip("/") + "/" + safe_name


def _detail(summary: dict, op_path: str, action: str, original=None, field: str | None = None) -> None:
    entry = {
        "op_path": op_path,
        "action": action,
    }
    if field is not None:
        entry["field"] = field
    if original is not None:
        entry["original"] = copy.deepcopy(original)
    summary["details"].append(entry)
