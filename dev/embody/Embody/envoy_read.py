"""Envoy read/introspection tool handlers (module DAT).

Module DAT (mod.envoy_read) called by EnvoyExt on the MAIN THREAD only.
Holds the private implementations behind the read-only / introspection MCP
tools (get_op, query_network, connections, flags/position, network layout,
annotations, errors, performance, TD/class/module introspection, DAT
content, TOP capture, logs, externalization status, TDN read/export/diff,
exec_op_method). EnvoyExt keeps a thin delegating stub for each -- these
functions carry the real bodies. The get_docs family is NOT here: it runs
on the WORKER thread, where mod.* is unavailable, so it lives on the
facade (ext-diet WP4 finding).

No module-level TD access; every function takes the ext instance (`ext`)
as its first argument and reaches TD through it or through the TD globals
(op, root, app, COMP, annotateCOMP, mod, ...) available inside function
bodies at call time. Helpers shared with the facade or the unit tests
(_maybe_offload_to_file) stay on the ext and are reached via `ext.`;
calls between moved functions are module-local.
"""

from __future__ import annotations

import math


def _op_summary(target, info) -> str:
    """One-line orientation for get_op: type, wiring, and what was folded.

    Deliberately mechanical -- composed from facts get_op already holds. No
    per-optype special-casing: a hand-tuned phrase per operator type is a
    maintenance surface that goes stale as TD adds operators.
    """
    bits = ["%s '%s'" % (target.OPType, target.name)]
    ins = sum(1 for i in info.get('inputs') or [] if i)
    outs = sum(1 for o in info.get('outputs') or [] if o)
    bits.append('in:%d out:%d' % (ins, outs))
    npar = len(info.get('parameters') or {})
    if npar:
        bits.append('%d non-default par%s' % (npar, '' if npar == 1 else 's'))
    seqs = info.get('sequences') or {}
    if seqs:
        bits.append('seq ' + ', '.join('%s[%d]' % (k, len(v))
                                       for k, v in list(seqs.items())[:3]))
    kids = info.get('children')
    if kids:
        bits.append('%d child%s' % (len(kids), '' if len(kids) == 1 else 'ren'))
    return ' | '.join(bits)


def get_op(ext, op_path: str, include_defaults: bool = False) -> dict:
    """Get operator information"""
    target = resolve_op(ext, op_path)
    if not target:
        return {'error': f'Operator not found: {op_path}'}

    info = {
        'path': target.path,
        'name': target.name,
        'type': target.OPType,
        'family': target.family,
        'valid': target.valid,
    }

    # Sequence collapse. Delegates to TDNExt's exporter rather than
    # re-deriving block grouping here -- that code carries hard-won gotchas
    # (uncooked-POP discovery, the list(target.seq) ordering contract,
    # sequenceBlock wrapper identity). scrub_transient=False because this is
    # a LIVE read: an export ships no runtime-status values, a read must.
    # Only in the compact mode -- include_defaults=True keeps the flat dump
    # that test_mcp_tdn_tools' read_tdn-vs-get_op ratio test measures.
    sequences = {}
    if not include_defaults:
        try:
            tdn = getattr(ext.ownerComp.ext, 'TDN', None)
            if tdn is not None:
                sequences = tdn._exportBuiltinSequences(
                    target, scrub_transient=False) or {}
        except Exception as e:
            ext._log(f'Sequence collapse unavailable for {op_path}: {e}',
                     'DEBUG')

    # Get parameters
    params = {}
    parameters_omitted = 0
    sequence_pars_collapsed = 0
    for p in target.pars():
        if sequences:
            try:
                if p.sequence is not None and not p.isSequence:
                    sequence_pars_collapsed += 1
                    continue
            except Exception:
                pass
        if not include_defaults:
            include_param = True
            try:
                mode_name = p.mode.name
            except Exception:
                mode_name = str(getattr(p, 'mode', ''))
            if mode_name == 'CONSTANT':
                include_param = False
                is_pulse = False
                try:
                    is_pulse = bool(getattr(p, 'isPulse', False))
                except Exception:
                    is_pulse = False
                if not is_pulse:
                    try:
                        style = str(getattr(p, 'style', ''))
                        is_pulse = style == 'Pulse' or style.endswith('.Pulse')
                    except Exception:
                        is_pulse = False
                if not is_pulse:
                    try:
                        include_param = p.val != p.default
                    except Exception:
                        include_param = True
            if not include_param:
                parameters_omitted += 1
                continue
        try:
            params[p.name] = {
                'value': str(p.eval()),
                'mode': str(p.mode),
                'label': p.label,
            }
        except Exception as e:
            ext._log(f'Could not read parameter {p.name} on {op_path}: {e}', 'DEBUG')
            params[p.name] = {'value': 'N/A', 'mode': 'N/A'}
    info['parameters'] = params
    if parameters_omitted > 0:
        info['parameters_omitted'] = parameters_omitted
    if sequences:
        info['sequences'] = sequences
    if sequence_pars_collapsed:
        info['sequence_pars_collapsed'] = sequence_pars_collapsed

    # Get inputs/outputs. inputConnectors, never OP.inputs: inputs is a
    # COMPACTED list of connected sources, so a wire on connector 1 with
    # connector 0 empty misreported as index 0 (live repro 2026-08-12).
    info['inputs'] = [
        (connector.connections[0].owner.path
         if connector.connections else None)
        for connector in target.inputConnectors]
    info['outputs'] = [out.path if out else None for out in target.outputs]

    # COMP-specific info
    if hasattr(target, 'children'):
        info['children'] = [child.name for child in target.children]

    info['summary'] = _op_summary(target, info)
    return ext._maybe_offload_to_file(info, 'get_op')


def query_network(ext, parent_path: str = "/", recursive: bool = False,
                  op_type: str = None, include_utility: bool = False) -> dict:
    """List operators in a network"""
    parent = resolve_op(ext, parent_path)
    if not parent:
        return {'error': f'Parent not found: {parent_path}'}

    if not hasattr(parent, 'children'):
        return {'error': f'{parent_path} is not a COMP'}

    def get_ops(comp, depth=0):
        results = []
        if include_utility:
            children = comp.findChildren(includeUtility=True, depth=1)
        else:
            children = comp.children
        for child in children:
            # Filter by type if specified
            if op_type and child.OPType != op_type and child.family != op_type:
                if recursive and hasattr(child, 'children'):
                    results.extend(get_ops(child, depth + 1))
                continue

            info = {
                'path': child.path,
                'type': child.OPType,
                'family': child.family,
                'depth': depth
            }
            if include_utility and child.type == 'annotate':
                info['utility'] = True
            results.append(info)

            if recursive and hasattr(child, 'children'):
                results.extend(get_ops(child, depth + 1))

        return results

    operators = get_ops(parent)
    result = {
        'parent': parent_path,
        'count': len(operators),
        'operators': operators
    }
    return ext._maybe_offload_to_file(result, 'query_network')


def get_connections(ext, op_path: str) -> dict:
    """Get all connections for an operator"""
    target = resolve_op(ext, op_path)
    if not target:
        return {'error': f'Operator not found: {op_path}'}

    # inputConnectors, never OP.inputs: inputs compacts away empty
    # connectors, so a sparse wire's true index was flattened to its
    # position among connected sources (live repro 2026-08-12: a wire
    # on a Matte TOP's connector 2 reported as index 0).
    inputs = []
    for i, connector in enumerate(target.inputConnectors):
        connected = [conn.owner.path for conn in connector.connections]
        inputs.append({
            'index': i,
            'connected_to': connected[0] if connected else None
        })

    outputs = []
    for i, connector in enumerate(target.outputConnectors):
        connected = [conn.owner.path for conn in connector.connections]
        outputs.append({
            'index': i,
            'connected_to': connected
        })

    result = {
        'path': op_path,
        'inputs': inputs,
        'outputs': outputs
    }

    # Include COMP connections (top/bottom) if this is a COMP
    if hasattr(target, 'inputCOMPConnectors'):
        comp_inputs = []
        for i, connector in enumerate(target.inputCOMPConnectors):
            connected = [conn.owner.path for conn in connector.connections]
            comp_inputs.append({
                'index': i,
                'connected_to': connected
            })
        result['comp_inputs'] = comp_inputs

        comp_outputs = []
        for i, connector in enumerate(target.outputCOMPConnectors):
            connected = [conn.owner.path for conn in connector.connections]
            comp_outputs.append({
                'index': i,
                'connected_to': connected
            })
        result['comp_outputs'] = comp_outputs

    return result


def _envoy_version():
    """Version constant from the EnvoyExt DAT module; 'unknown' during a
    broken-DAT-text window so a cosmetic field can't fail the whole tool."""
    try:
        return mod.EnvoyExt.ENVOY_VERSION
    except Exception:
        return 'unknown'


def get_td_info(ext) -> dict:
    """Get TouchDesigner environment and Envoy server info"""
    try:
        import td as _td
        version = _td.app.version
        build = _td.app.build
        # app.osName reports "Windows 10" on Win 11 (same NT 10.0 kernel);
        # EmbodyExt._osLabel() disambiguates via the build number.
        try:
            os_name = ext.ownerComp.ext.Embody._osLabel()
        except Exception:
            os_name = _td.app.osName
        # ENVOY_VERSION is the EnvoyExt module's single source of truth;
        # reach it via mod so this module never duplicates the literal.
        return {
            'server': f'TouchDesigner {version}.{build}',
            'version': f'{version}.{build}',
            'osName': os_name,
            'osVersion': _td.app.osVersion,
            'envoyVersion': _envoy_version(),
        }
    except Exception as e:
        return {'error': f'Failed to get TD info: {e}'}


_FOCUS_SELECTION_CAP = 24


def focus_target(selected, current) -> tuple:
    """Resolve what "this operator" means: (target_path, source) or
    (None, None) when it is ambiguous.

    Exactly one selected operator wins; otherwise the current operator.
    The ROLLOVER op is deliberately not an input here -- it is only
    wherever the mouse happens to sit and must never be acted on.

    Pure (plain strings in, strings out) so the disambiguation rule is
    unit-testable without a UI.
    """
    paths = list(selected or [])
    if len(paths) == 1:
        return paths[0], 'selection'
    if current:
        return current, 'current'
    return None, None


def get_focus(ext) -> dict:
    """What the user is looking at: current pane, selection, rollover.

    Resolves conversational references ("this operator") to real paths.
    'This' is the SELECTED / current operator -- never the rollover, which
    is only wherever the mouse happens to sit. Headless / TouchEngine builds
    have no panes; that returns a clear headless result rather than raising.
    """
    try:
        panes = ui.panes
        pane_count = len(panes)
    except Exception as e:
        return {'error': f'Could not read the UI panes: {e}'}

    if not pane_count:
        return {
            'headless': True,
            'network': None,
            'paneType': None,
            'selected': [],
            'selectedCount': 0,
            'current': None,
            'rollover': None,
            'target': None,
            'targetSource': None,
            'note': 'This TouchDesigner instance has no UI panes '
                    '(headless / TouchEngine), so there is nothing the user '
                    'is looking at. Ask for an explicit operator path, or '
                    'discover one with query_network.',
        }

    try:
        pane = panes.current
    except Exception:
        pane = None
    if pane is None:
        try:
            pane = panes[0]
        except Exception:
            pane = None

    owner = None
    owner_path = None
    pane_type = None
    if pane is not None:
        try:
            owner = pane.owner
        except Exception:
            owner = None
        try:
            owner_path = owner.path if owner is not None else None
        except Exception:
            owner, owner_path = None, None
        try:
            pane_type = str(pane.type)
        except Exception:
            pane_type = None

    selected = []
    current = None
    if owner is not None:
        try:
            for child in owner.selectedChildren:
                if child is not None and child.valid:
                    selected.append(child.path)
        except Exception:
            selected = []
        try:
            child = owner.currentChild
            if child is not None and child.valid:
                current = child.path
        except Exception:
            current = None

    rollover = None
    try:
        hovered = ui.rolloverOp
        if hovered is not None and hovered.valid:
            rollover = hovered.path
    except Exception:
        rollover = None

    # "This operator" -> exactly one selected op, else the current one.
    # Rollover NEVER feeds the target; it is incidental mouse position.
    target, target_source = focus_target(selected, current)

    result = {
        'network': owner_path,
        'paneType': pane_type,
        'selected': selected[:_FOCUS_SELECTION_CAP],
        'selectedCount': len(selected),
        'current': current,
        'rollover': rollover,
        'target': target,
        'targetSource': target_source,
    }
    if target is not None:
        result['note'] = ('"this operator" means %s (from the %s). The '
                          'rollover op, if any, is incidental mouse position '
                          '-- do not act on it.' % (target, target_source))
    elif selected:
        result['note'] = ('%d operators are selected and none is current -- '
                          'ambiguous. Ask the user which one, or act on all '
                          'of them deliberately.' % len(selected))
    else:
        result['note'] = ('Nothing is selected. The user is in the network '
                          '%s; ask which operator they mean rather than '
                          'guessing.' % (owner_path or 'unknown'))
    return result


# GLSL compile errors do NOT reach op.errors() -- TD reports only a WARNING
# ("The GLSL Shader has compile errors (Use Info DAT to see details)") and
# puts the actual text in the auto-docked <name>_info DAT. Cost the project a
# full debug cycle once: a `half` reserved-word error rendered TD's fallback
# while op.errors() said "(none)" (.claude/rules/multi-agent-review.md:15).
# Reading the DOCKED info DAT is non-mutating -- TD creates it with the op.

_SHADER_ERROR_MARKERS = ('error', 'failed', 'invalid')
_SHADER_LOG_LINE_CAP = 6
_SHADER_LOG_CHAR_CAP = 200
_SCRIPT_ERROR_CHAR_CAP = 1200


def _docked_info_dat(target):
    """The op's own docked Info DAT, or None.

    Matched on dat.type == 'info' (short form, per EmbodyExt's
    _TD_MANAGED_DAT_TYPES note), NOT on a '<name>_info' name: rename_op
    renames only the host, never its docks, so name-matching goes stale.
    An infoDAT docked to A can point its `op` par at B, so the target is
    confirmed before its text is trusted.
    """
    try:
        docked = target.docked or []
    except Exception:
        return None
    for d in docked:
        try:
            if d.family != 'DAT' or d.type != 'info':
                continue
            ref = d.par.op.eval()
            if ref is None or ref is target:
                return d
        except Exception:
            continue
    return None


def shader_compile_log(target):
    """Compile diagnostics for one operator, read from its docked Info DAT.

    Returns None when the op docks no Info DAT (i.e. it is not a shader op),
    so callers can tell "not a shader" from "shader, no errors" -- reporting
    a clean bill of health for an absent DAT is a silent false negative.
    """
    info = _docked_info_dat(target)
    if info is None:
        return None
    try:
        text = info.text or ''
    except Exception:
        return {'infoDat': None, 'unreadable': True, 'lines': []}
    lines = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or not any(m in line.lower()
                               for m in _SHADER_ERROR_MARKERS):
            continue
        if len(lines) >= _SHADER_LOG_LINE_CAP:
            lines.append('... (truncated)')
            break
        lines.append(line[:_SHADER_LOG_CHAR_CAP])
    return {'infoDat': info.path, 'lines': lines}


def shader_errors(ext, target, recurse: bool = True) -> list:
    """Shader compile errors for an op and (optionally) its descendants.

    Entries use the SAME shape as get_op_errors' errors[] so the write-effect
    differ (_new_error_entries) consumes them unchanged.
    """
    ops = [target]
    if recurse:
        try:
            # maxDepth, never depth -- TD's `depth` is an EXACT relative depth.
            ops.extend(target.findChildren(maxDepth=99))
        except Exception:
            pass
    found = []
    for o in ops:
        log = shader_compile_log(o)
        if not log or not log.get('lines'):
            continue
        for line in log['lines']:
            found.append({
                'nodePath': o.path,
                'nodeName': o.name,
                'opType': o.OPType,
                'message': line,
                'infoDat': log.get('infoDat'),
            })
    return found


def script_errors(ext, target, recurse: bool = True) -> list:
    """Python tracebacks -- callback, DAT-script and expression errors.

    TD keeps these on OP.scriptErrors(), a SEPARATE surface from
    OP.errors(): a DAT whose onCook raises shows a red X in the network yet
    reads as a perfectly clean op through errors()/warnings() alone (field
    2026-08-22 -- a stale "'EmbodyExt' object has no attribute 'DirtyState'"
    on Embody's own manager list, which get_op_errors called 0 errors).
    One cheap call and nothing cooks, so unlike shader_errors this always
    runs and merges straight into errors[].

    Entries use the SAME shape as get_op_errors' errors[] (plus
    kind='script') so the write-effect differ consumes them unchanged.
    """
    if not hasattr(target, 'scriptErrors'):
        return []
    try:
        output = target.scriptErrors(recurse=recurse) or ''
    except Exception:
        return []

    found = []
    block = []

    def _flush(path):
        message = '\n'.join(block).strip()
        del block[:]
        if not message:
            return
        node = op(path)
        known = bool(node) and node.valid
        found.append({
            'nodePath': node.path if known else path,
            'nodeName': node.name if known else '',
            'opType': node.OPType if known else '',
            'message': message[:_SCRIPT_ERROR_CHAR_CAP],
            'kind': 'script',
        })

    for raw in output.split('\n'):
        line = raw.strip()
        if not line:
            continue
        path = None
        # A block ENDS on its trailing " (/op/path)" line. The leading slash
        # is what makes that safe: a traceback's own last line -- e.g.
        # "TypeError: f() takes 2 positional arguments (3 given)" -- has no
        # slash there, so it cannot split the block early.
        if line.endswith(')'):
            head, sep, tail = line.rpartition('(')
            if sep and tail[:-1].startswith('/'):
                path = tail[:-1]
                line = head.strip()
        if line:
            block.append(line)
        if path is not None:
            _flush(path)
    if block:
        # No terminator seen: attribute the tail to the op we asked about
        # rather than dropping it (CLAUDE.md "fail loud").
        _flush(target.path)
    return found


def get_op_errors(ext, op_path: str, recurse: bool = True,
                  include_shaders: bool = True) -> dict:
    """Errors and warnings for an operator and its children.

    errors[] carries TD's cook errors AND Python tracebacks (kind='script');
    shader compile failures ride a separate shaderErrors key because reading
    those cooks Info DATs -- see script_errors / shader_errors.
    """
    target = resolve_op(ext, op_path)
    if not target:
        return {'error': f'Operator not found: {op_path}'}

    all_errors = []
    all_warnings = []

    for severity, method_name, output_list in [
        ('error', 'errors', all_errors),
        ('warning', 'warnings', all_warnings),
    ]:
        if hasattr(target, method_name) and callable(getattr(target, method_name)):
            try:
                output = getattr(target, method_name)(recurse=recurse)
                if output:
                    for line in output.strip().split('\n'):
                        line = line.strip()
                        if not line:
                            continue
                        # TD format: "Message text (node_path)"
                        if '(' in line and line.endswith(')'):
                            message_part, path_part = line.rsplit('(', 1)
                            node_path = path_part.rstrip(')')
                            message = message_part.strip()
                            node = op(node_path)
                            if node and node.valid:
                                output_list.append({
                                    'nodePath': node.path,
                                    'nodeName': node.name,
                                    'opType': node.OPType,
                                    'message': message,
                                })
                            else:
                                output_list.append({
                                    'nodePath': node_path,
                                    'nodeName': '',
                                    'opType': '',
                                    'message': message,
                                })
                        else:
                            output_list.append({
                                'nodePath': target.path,
                                'nodeName': target.name,
                                'opType': target.OPType,
                                'message': line,
                            })
            except Exception as e:
                ext._log(f'Error getting {severity}s from {op_path}: {e}', 'WARNING')

    # Python tracebacks come from TD's own error API and cost one cheap
    # call, so they COUNT: errorCount/hasErrors must mean "what the network
    # shows red". shaderErrors stays a side key only because it must cook.
    all_errors.extend(script_errors(ext, target, recurse))

    result = {
        'path': target.path,
        'errorCount': len(all_errors),
        'warningCount': len(all_warnings),
        'hasErrors': bool(all_errors),
        'hasWarnings': bool(all_warnings),
        'errors': all_errors,
        'warnings': all_warnings,
    }
    # Added only when non-empty: op.errors() cannot see GLSL compile failures,
    # so without this a broken shader reads as a clean operator.
    #
    # include_shaders=False is for the per-write footer, which scans the WHOLE
    # project: reading an Info DAT's .text COOKS it, so a project-wide pass on
    # a cold session cooked ~213 docked ops and took 3.36s (measured
    # 2026-08-21), blowing the footer's budget and tripping its one-way
    # disable latch. The footer does its own pass over touched ops instead.
    # An explicit get_op_errors call is an agent's deliberate request, so it
    # keeps the full walk.
    shader = shader_errors(ext, target, recurse) if include_shaders else None
    if shader:
        result['shaderErrors'] = shader
        result['hasErrors'] = True
    return result


def exec_op_method(ext, op_path: str, method: str,
                   args: list = None, kwargs: dict = None) -> dict:
    """Call a method on a TD operator"""
    target = resolve_op(ext, op_path)
    if not target:
        return {'error': f'Operator not found: {op_path}'}

    if not hasattr(target, method):
        return {'error': f'Method "{method}" not found on {op_path}'}

    func = getattr(target, method)
    if not callable(func):
        return {'error': f'"{method}" is not callable on {op_path}'}

    try:
        result = func(*(args or []), **(kwargs or {}))
        # Process result for JSON serialization
        processed = process_result(ext, result)
        return {'success': True, 'result': processed}
    except Exception as e:
        return {'error': f'Method execution failed: {e}'}


def get_td_classes(ext) -> dict:
    """List all Python classes/modules in the td module"""
    try:
        import td as _td
        import inspect
        classes = []
        for name, obj in inspect.getmembers(_td):
            if name.startswith('_'):
                continue
            description = inspect.getdoc(obj) or ''
            classes.append({
                'name': name,
                'description': description,
            })
        return {'classes': classes}
    except Exception as e:
        return {'error': f'Failed to get TD classes: {e}'}


def get_td_class_details(ext, class_name: str) -> dict:
    """Get detailed info about a specific TD Python class"""
    try:
        import td as _td
        import inspect

        if not hasattr(_td, class_name):
            return {'error': f'Class not found in td module: {class_name}'}

        obj = getattr(_td, class_name)
        methods = []
        properties = []

        for name, member in inspect.getmembers(obj):
            if name.startswith('_'):
                continue
            try:
                info = {
                    'name': name,
                    'description': inspect.getdoc(member) or '',
                    'type': type(member).__name__,
                }
                if (inspect.isfunction(member) or inspect.ismethod(member)
                        or inspect.ismethoddescriptor(member)):
                    methods.append(info)
                else:
                    properties.append(info)
            except Exception as e:
                ext._log(f'Could not inspect member {name} on {class_name}: {e}', 'DEBUG')
                pass

        return {
            'name': class_name,
            'type': type(obj).__name__,
            'description': inspect.getdoc(obj) or '',
            'methods': methods,
            'properties': properties,
        }
    except Exception as e:
        return {'error': f'Failed to get class details: {e}'}


def get_module_help(ext, module_name: str) -> dict:
    """Get Python help text for a TD module or class"""
    try:
        import td as _td
        import pydoc
        import importlib

        target = None
        name = module_name.strip()

        # Try dotted names (e.g., "td.tdu.Position")
        if '.' in name:
            parts = name.split('.')
            if parts[0] == 'td':
                obj = _td
                for part in parts[1:]:
                    if hasattr(obj, part):
                        obj = getattr(obj, part)
                    else:
                        obj = None
                        break
                target = obj

        # Try direct attribute of td
        if target is None and hasattr(_td, name):
            target = getattr(_td, name)

        # Try importing as module
        if target is None:
            try:
                target = importlib.import_module(name)
            except (ImportError, ModuleNotFoundError):
                pass

        if target is None:
            return {'error': f'Module not found: {module_name}'}

        help_text = pydoc.render_doc(target)
        # Strip backspace formatting from pydoc output
        cleaned = []
        for char in help_text:
            if char == '\b':
                if cleaned:
                    cleaned.pop()
            else:
                cleaned.append(char)
        help_text = ''.join(cleaned)

        return {
            'moduleName': module_name,
            'helpText': help_text,
        }
    except Exception as e:
        return {'error': f'Failed to get help: {e}'}


def process_result(ext, result) -> object:
    """Process a method result for JSON serialization"""
    if result is None or isinstance(result, (int, float, str, bool)):
        return result
    if isinstance(result, (list, tuple)):
        return [process_result(ext, item) for item in result]
    if isinstance(result, dict):
        return {k: process_result(ext, v) for k, v in result.items()}
    # TD operator objects -> path string
    if hasattr(result, 'path') and hasattr(result, 'valid'):
        return result.path
    return str(result)


_DAT_STATS_EDGE_ROWS = 5


def _reduce_dat(target) -> dict:
    """Reduce a table DAT to per-column stats + edge rows, never a full dump.

    A 5000-row table is 5000 rows of context; this is a shape-and-range read.
    Numeric columns report min/max/mean, text columns report distinct counts.
    """
    ncols, nrows = target.numCols, target.numRows
    has_header = nrows > 0
    columns = []
    for c in range(ncols):
        name = target[0, c].val if has_header else f'col{c}'
        values = [target[rr, c].val for rr in range(1 if has_header else 0,
                                                    nrows)]
        entry = {'name': name, 'count': len(values)}
        numeric = []
        for v in values:
            try:
                numeric.append(float(v))
            except (TypeError, ValueError):
                numeric = None
                break
        if numeric:
            entry.update({
                'numeric': True,
                'min': min(numeric), 'max': max(numeric),
                'mean': sum(numeric) / len(numeric),
            })
        else:
            entry['numeric'] = False
            entry['distinct'] = len(set(values))
        columns.append(entry)

    edge = _DAT_STATS_EDGE_ROWS
    body_start = 1 if has_header else 0
    rows = [[target[rr, c].val for c in range(ncols)]
            for rr in range(nrows)]
    head = rows[:body_start + edge]
    tail = rows[-edge:] if nrows > body_start + edge * 2 else []
    out = {
        'numRows': nrows, 'numCols': ncols,
        'columns': columns, 'head': head,
    }
    if tail:
        out['tail'] = tail
        out['rowsOmitted'] = nrows - len(head) - len(tail)
    return out


def get_dat_content(ext, op_path: str, format: str = "auto") -> dict:
    """Get DAT content as text or table data"""
    target = resolve_op(ext, op_path)
    if not target:
        return {'error': f'Operator not found: {op_path}'}
    if target.family != 'DAT':
        return {'error': f'{op_path} is not a DAT (family: {target.family})'}

    try:
        result = {
            'path': op_path,
            'numRows': target.numRows,
            'numCols': target.numCols,
            'isTable': target.isTable,
            'isText': target.isText,
        }

        if format == "stats":
            if not target.isTable:
                return {'error': f'{op_path} is not a table DAT -- '
                                 f'"stats" reduces tables only'}
            result.update(_reduce_dat(target))
            result['format'] = 'stats'
            return result

        use_table = (format == "table") or (format == "auto" and target.isTable)

        if use_table:
            rows = []
            for r in range(target.numRows):
                row = []
                for c in range(target.numCols):
                    row.append(target[r, c].val)
                rows.append(row)
            result['rows'] = rows
            result['format'] = 'table'
        else:
            result['text'] = target.text
            result['format'] = 'text'

        return result
    except Exception as e:
        return {'error': f'Failed to get DAT content: {e}'}


def _frame_quality(arr) -> dict:
    """Compute a token-lean 'is this frame actually renderable' verdict from a
    float32 [H,W,C] texture array (post-flip, values nominally in [0,1]).

    Answers the question the debug-operator skill and CLAUDE.md keep asking --
    "never declare a visual task done on a black or empty frame" -- as machine-
    checkable data, so the agent can branch WITHOUT spending vision tokens or
    trusting an eyeballed JPEG. is_black / fully_transparent are failures;
    is_flat (a uniform fill) is advisory, not a failure (a solid colour is a
    valid render). Alpha is the last channel when C is 2 (lum+alpha) or 4."""
    import numpy as np

    a = arr
    if a.ndim == 2:
        a = a[:, :, np.newaxis]
    if a.ndim != 3:
        return {}
    h, w, c = a.shape
    if h <= 0 or w <= 0 or c <= 0:
        return {}

    rgb = a[:, :, :3] if c >= 3 else np.repeat(a[:, :, :1], 3, axis=2)
    rgb = np.clip(rgb.astype(np.float32), 0.0, 1.0)
    lum = 0.2126 * rgb[:, :, 0] + 0.7152 * rgb[:, :, 1] + 0.0722 * rgb[:, :, 2]
    max_l = float(lum.max())
    mean_l = float(lum.mean())
    std_l = float(lum.std())

    has_alpha = c in (2, 4)
    mean_a = None
    opaque_frac = None
    if has_alpha:
        alpha = np.clip(a[:, :, -1].astype(np.float32), 0.0, 1.0)
        mean_a = float(alpha.mean())
        opaque_frac = float((alpha > 0.5).mean())

    # Thresholds: nearly-unlit, near-uniform, near-invisible.
    is_black = max_l < 0.02
    is_flat = std_l < 0.01
    fully_transparent = has_alpha and mean_a < 0.01

    reasons = []
    if is_black:
        reasons.append('black_frame')
    if fully_transparent:
        reasons.append('fully_transparent')
    if is_flat and not is_black:
        reasons.append('flat_frame')

    verdict = {
        'is_black': is_black,
        'is_flat': is_flat,
        'max_luminance': round(max_l, 4),
        'mean_luminance': round(mean_l, 4),
        'std_luminance': round(std_l, 4),
        'pass': (not is_black) and (not fully_transparent),
        'fail_reasons': reasons,
    }
    if has_alpha:
        verdict['mean_alpha'] = round(mean_a, 4)
        verdict['opaque_fraction'] = round(opaque_frac, 4)
    return verdict


def capture_top(ext, op_path: str, format: str = 'jpeg',
                quality: float = 0.8, max_resolution: int = 640,
                inline: bool = False, sample_grid: int = 0) -> dict:
    """Capture a TOP operator's output as a compressed image."""
    import base64

    target = resolve_op(ext, op_path)
    if not target:
        return {'error': f'Operator not found: {op_path}'}
    if target.family != 'TOP':
        return {'error': f'{op_path} is not a TOP (family: {target.family})'}

    try:
        sample_grid = int(sample_grid or 0)
    except Exception:
        sample_grid = 0
    if sample_grid >= 2:
        return sample_grid_top(ext, target, sample_grid)

    if format not in ('jpeg', 'png'):
        return {'error': f'Unsupported format: {format}. Use "jpeg" or "png".'}
    if not (0.0 <= quality <= 1.0):
        return {'error': f'Quality must be between 0.0 and 1.0, got {quality}'}

    try:
        import numpy as np
        import cv2

        # Force cook so we get current output
        target.cook(force=True)

        original_w = target.width
        original_h = target.height

        # Capture pixel data from GPU
        arr = target.numpyArray()  # float32 [H, W, C], bottom-up
        if arr is None or arr.size == 0:
            return {'error': f'No pixel data available from {op_path}'}

        # Flip vertically (TD textures are bottom-up)
        arr = np.flipud(arr)

        # Verdict computed from the FLOAT array (full res, pre-resize) so the
        # black/flat/transparent judgment reflects true pixel values, not the
        # quantized/downsampled JPEG. Never let it break a capture.
        try:
            quality_verdict = _frame_quality(arr)
        except Exception:
            quality_verdict = {}

        # Convert float32 [0,1] to uint8 [0,255]
        arr = (np.clip(arr, 0.0, 1.0) * 255.0).astype(np.uint8)

        # Convert color channels for cv2 (expects BGR/BGRA)
        channels = arr.shape[2] if arr.ndim == 3 else 1
        if channels == 4:
            arr = cv2.cvtColor(arr, cv2.COLOR_RGBA2BGRA)
        elif channels == 3:
            arr = cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
        elif channels == 2:
            # Luminance + Alpha: extract luminance only
            arr = arr[:, :, 0]

        # Resize if needed
        h, w = arr.shape[:2]
        if max_resolution > 0 and max(h, w) > max_resolution:
            scale = max_resolution / max(h, w)
            new_w = int(w * scale)
            new_h = int(h * scale)
            arr = cv2.resize(arr, (new_w, new_h),
                             interpolation=cv2.INTER_AREA)

        # Encode to image format
        if format == 'jpeg':
            params = [cv2.IMWRITE_JPEG_QUALITY, int(quality * 100)]
            success, buf = cv2.imencode('.jpg', arr, params)
        else:
            success, buf = cv2.imencode('.png', arr)

        if not success:
            return {'error': f'Failed to encode image as {format}'}

        image_data = buf.tobytes()
        out_h, out_w = arr.shape[:2]

        return {
            'success': True,
            'image_b64': base64.b64encode(image_data).decode('ascii'),
            'width': out_w,
            'height': out_h,
            'original_width': original_w,
            'original_height': original_h,
            'format': format,
            'size_bytes': len(image_data),
            'quality': quality_verdict,
        }
    except Exception as e:
        return {'error': f'Failed to capture TOP: {e}'}


def sample_grid_top(ext, target, grid) -> dict:
    try:
        import numpy as np

        def _finite(value):
            try:
                value = float(value)
                if not math.isfinite(value):
                    return 0.0
                return value
            except Exception:
                return 0.0

        grid = max(2, min(32, int(grid)))

        # Force cook so we get current output, matching the image path.
        target.cook(force=True)

        try:
            arr = target.numpyArray()
        except Exception as e:
            return {'error': f'sample_grid failed: numpyArray failed: {e}'}
        if arr is None or arr.size == 0:
            return {'error': f'sample_grid failed: No pixel data available from {target.path}'}

        # Matches saved-image orientation; TD texture origin is bottom-left,
        # see td-python.md render-coords table.
        arr = np.flipud(arr)

        if arr.ndim == 2:
            arr = arr[:, :, np.newaxis]
        if arr.ndim != 3:
            return {'error': f'sample_grid failed: unexpected array shape {arr.shape}'}

        h, w, c = arr.shape
        if h <= 0 or w <= 0:
            return {'error': f'sample_grid failed: No pixel data available from {target.path}'}

        effective_grid = max(1, min(grid, h, w))

        try:
            pixel_format = str(target.pixelFormat)
        except Exception:
            pixel_format = ''
        fmt = pixel_format.lower()

        # A texture with no alpha plane is opaque; padding a=0 falsely
        # reported opaque textures as transparent (review finding).
        if c >= 4:
            channel_map = (0, 1, 2, 3)
            pad_values = (None, None, None, None)
        elif c == 3:
            channel_map = (0, 1, 2, None)
            pad_values = (None, None, None, 1.0)
        elif c == 2 and 'monoalpha' in fmt:
            channel_map = (0, 0, 0, 1)
            pad_values = (None, None, None, None)
        elif c == 2:
            channel_map = (0, 1, None, None)
            pad_values = (None, None, 0.0, 1.0)
        elif c == 1:
            channel_map = (0, 0, 0, None)
            pad_values = (None, None, None, 1.0)
        else:
            return {'error': f'sample_grid failed: unexpected channel count {c}'}

        # Detect non-finite values without a full-frame float64 RGBA copy:
        # an 8K RGBA copy was ~2GB (review finding).
        sanitized = False
        checked_channels = set()
        for idx in channel_map:
            if idx is None or idx in checked_channels:
                continue
            checked_channels.add(idx)
            channel = arr[:, :, idx]
            try:
                raw_min = float(channel.min())
                raw_max = float(channel.max())
                if not math.isfinite(raw_min) or not math.isfinite(raw_max):
                    sanitized = True
                    break
            except Exception:
                sanitized = True
                break
        if sanitized:
            arr = np.nan_to_num(arr, copy=False, nan=0.0,
                                posinf=0.0, neginf=0.0)

        def _channel_stat(idx, pad):
            if idx is None:
                return {
                    'min': round(float(pad), 4),
                    'max': round(float(pad), 4),
                    'mean': round(float(pad), 4),
                }
            channel = arr[:, :, idx]
            return {
                'min': round(_finite(channel.min()), 4),
                'max': round(_finite(channel.max()), 4),
                'mean': round(_finite(channel.mean()), 4),
            }

        def _block_mean(block, idx, pad):
            if idx is None:
                return round(float(pad), 4)
            return round(_finite(block[:, :, idx].mean()), 4)

        stats = {}
        for out_idx, name in enumerate(('r', 'g', 'b', 'a')):
            stats[name] = _channel_stat(channel_map[out_idx],
                                        pad_values[out_idx])

        row_edges = np.linspace(0, h, effective_grid + 1).astype(int)
        col_edges = np.linspace(0, w, effective_grid + 1).astype(int)
        rows = []
        for row_idx in range(effective_grid):
            y0 = row_edges[row_idx]
            y1 = row_edges[row_idx + 1]
            row = []
            for col_idx in range(effective_grid):
                x0 = col_edges[col_idx]
                x1 = col_edges[col_idx + 1]
                block = arr[y0:y1, x0:x1]
                row.append([
                    _block_mean(block, channel_map[0], pad_values[0]),
                    _block_mean(block, channel_map[1], pad_values[1]),
                    _block_mean(block, channel_map[2], pad_values[2]),
                    _block_mean(block, channel_map[3], pad_values[3]),
                ])
            rows.append(row)

        result = {
            'op': target.path,
            'width': w,
            'height': h,
            'channels': c,
            'pixel_format': pixel_format,
            'grid': effective_grid,
            'origin': 'top-left',
            'cells': rows,
            'stats': stats,
        }
        if sanitized:
            result['nan_or_inf_sanitized'] = True
        return result
    except Exception as e:
        return {'error': f'sample_grid failed: {e}'}


# --- CHOP / POP data readers -------------------------------------------
# Reduced reads, never a blind dump: a 4x600 CHOP is 2400 floats that would
# otherwise land in an agent's context as raw numbers. The reduce-don't-dump
# contract is adapted from the `view` tool in Marius Alwan Meyer's codemode
# fork (github.com/sporqist/Embody, MIT); the POP half and the readback
# ceiling below are ours.

_CHOP_CHAN_CAP = 32           # channels reduced per call before truncating
_POP_READBACK_POINTS = 50000  # see the cost contract in get_pop_data


def _chan_stats(chan, samples: int) -> dict:
    """Per-channel reduction. numpyArray() when available, Channel API else.

    Channel exposes min()/max()/average() but NOT numSamples -- use len(chan)
    (verified live on 2025.33070; the obvious chan.numSamples raises).
    """
    entry = {'name': chan.name}
    try:
        arr = chan.numpyArray()
        entry.update({
            'min': float(arr.min()), 'max': float(arr.max()),
            'mean': float(arr.mean()), 'std': float(arr.std()),
            'first': float(arr[0]), 'last': float(arr[-1]),
        })
        if samples > 0:
            entry['head'] = [float(v) for v in arr[:samples]]
            if len(arr) > samples:
                entry['tail'] = [float(v) for v in arr[-samples:]]
    except Exception:
        n = len(chan)
        entry.update({
            'min': float(chan.min()), 'max': float(chan.max()),
            'mean': float(chan.average()),
            'first': float(chan[0]) if n else None,
            'last': float(chan[n - 1]) if n else None,
        })
    return entry


def _reduce_chop(target, channels=None, samples: int = 0) -> dict:
    """Reduce a CHOP to per-channel stats (shared by read and diff paths)."""
    import fnmatch
    chans = list(target.chans())
    if channels:
        chans = [c for c in chans if fnmatch.fnmatch(c.name, channels)]
    omitted = max(0, len(chans) - _CHOP_CHAN_CAP)
    out = {
        'path': target.path,
        'numChans': target.numChans,
        'numSamples': target.numSamples,
        'rate': target.rate,
        'isTimeSlice': target.isTimeSlice,
        'channels': [_chan_stats(c, samples) for c in chans[:_CHOP_CHAN_CAP]],
    }
    if omitted:
        out['channelsOmitted'] = omitted
    return out


def get_chop_data(ext, op_path: str, channels: str = None, samples: int = 0,
                  compare_to: str = None) -> dict:
    """Reduce a CHOP to per-channel statistics, optionally diffed vs another.

    Args:
        op_path: the CHOP to read
        channels: glob on channel name (e.g. 'chan*') -- omit for all
        samples: head/tail raw values per channel (0 = stats only)
        compare_to: another CHOP path -- returns per-channel deltas, the
            "what did this chain do to my data" read

    Returns:
        dict with numChans/numSamples/rate/isTimeSlice + channels[], each
        {name, min, max, mean, std, first, last}; 'diff' when compare_to set.
    """
    target = resolve_op(ext, op_path)
    if not target:
        return {'error': f'Operator not found: {op_path}'}
    if target.family != 'CHOP':
        return {'error': f'{op_path} is not a CHOP (family: {target.family})'}
    try:
        target.cook(force=True)   # same precedent as capture_top
        result = _reduce_chop(target, channels, samples)
        if compare_to:
            other = resolve_op(ext, compare_to)
            if not other:
                return {'error': f'compare_to not found: {compare_to}'}
            if other.family != 'CHOP':
                return {'error': f'compare_to is not a CHOP: {compare_to}'}
            other.cook(force=True)
            ref = _reduce_chop(other, channels, 0)
            by_name = {c['name']: c for c in ref['channels']}
            mine = {c['name'] for c in result['channels']}
            deltas = []
            for c in result['channels']:
                o = by_name.get(c['name'])
                if not o:
                    deltas.append({'name': c['name'], 'only_in': target.path})
                    continue
                deltas.append({'name': c['name'], **{
                    k: round(c[k] - o[k], 6) for k in ('min', 'max', 'mean')
                    if c.get(k) is not None and o.get(k) is not None}})
            deltas.extend({'name': n, 'only_in': other.path}
                          for n in by_name if n not in mine)
            result['diff'] = {'against': other.path, 'channels': deltas}
        return ext._maybe_offload_to_file(result, 'get_chop_data')
    except Exception as e:
        return {'error': f'Failed to read CHOP: {e}'}


def get_pop_data(ext, op_path: str, attributes: str = None, samples: int = 0,
                 max_points: int = _POP_READBACK_POINTS) -> dict:
    """Read a POP: attribute metadata always, point values only on request.

    COST CONTRACT (measured on 2025.33070, gridPOP, 2026-08-21):
      metadata only ............ 0.02 ms, flat, ANY point count
      readback, 16k points ..... ~9.5 ms
      readback, 160k points .... ~69 ms  (~4 frames at 60fps)
    The `count` argument of POP.points() cannot bound this: it is accepted
    and IGNORED (points('P', 5, 3) returns every point), so reading 4096
    points off a 160k POP measured SLOWER (80ms) than reading all of them.
    The guard is numPoints, never sample size. Do not "optimize" this by
    passing a count and dropping the ceiling -- the count does nothing.

    Args:
        attributes: glob on attribute name (e.g. 'P') -- omit for all
        samples: head point values to read (0 = metadata only, the default)
        max_points: refuse a readback above this many points
    """
    import fnmatch
    target = resolve_op(ext, op_path)
    if not target:
        return {'error': f'Operator not found: {op_path}'}
    if target.family != 'POP':
        return {'error': f'{op_path} is not a POP (family: {target.family})'}
    try:
        target.cook(force=True)
        n = target.numPoints()

        def _attrs(seq):
            out = []
            for a in seq:
                if attributes and not fnmatch.fnmatch(a.name, attributes):
                    continue
                out.append({'name': a.name, 'size': a.size,
                            'type': str(a.type).split('.')[-1]})
            return out

        result = {
            'path': target.path,
            'numPoints': n,
            'pointAttributes': _attrs(target.pointAttributes),
            'primAttributes': _attrs(target.primAttributes),
            'vertAttributes': _attrs(target.vertAttributes),
        }
        if samples <= 0:
            result['note'] = ('metadata only -- pass samples>0 for point '
                              'values (GPU readback, see max_points)')
            return result
        if n > max_points:
            result['readbackRefused'] = (
                f'{n} points exceeds max_points={max_points}; a readback this '
                f'size stalls the main thread (~69ms at 160k points). Raise '
                f'max_points deliberately if you accept the cost.')
            return result

        for entry in result['pointAttributes']:
            # Slice in PYTHON: POP.points(attr, startIndex, count) accepts
            # both args and IGNORES them -- points('P', 5, 3) returned all 64
            # points of an 8x8 grid (verified 2025.33070, 2026-08-21). That is
            # also why the readback ceiling below is the only real guard.
            raw = target.points(entry['name'])
            take = min(samples, n)
            vals = [raw[i] for i in range(take)]
            if entry['size'] > 1:
                entry['head'] = [tuple(round(float(v), 6) for v in p)
                                 for p in vals]
            else:
                entry['head'] = [round(float(p[0]), 6) for p in vals]
        return ext._maybe_offload_to_file(result, 'get_pop_data')
    except Exception as e:
        return {'error': f'Failed to read POP: {e}'}


def get_op_flags(ext, op_path: str) -> dict:
    """Get all flags for an operator"""
    target = resolve_op(ext, op_path)
    if not target:
        return {'error': f'Operator not found: {op_path}'}

    try:
        result = {
            'path': op_path,
            'bypass': target.bypass,
            'lock': target.lock,
            'display': target.display,
            'render': target.render,
            'viewer': target.viewer,
            'current': target.current,
            'expose': target.expose,
            'selected': target.selected,
        }
        if target.isCOMP:
            result['allowCooking'] = target.allowCooking
        return result
    except Exception as e:
        return {'error': f'Failed to get flags: {e}'}


def get_op_position(ext, op_path: str) -> dict:
    """Get operator position and visual properties"""
    target = resolve_op(ext, op_path)
    if not target:
        return {'error': f'Operator not found: {op_path}'}

    try:
        return {
            'path': op_path,
            'nodeX': target.nodeX,
            'nodeY': target.nodeY,
            'nodeWidth': target.nodeWidth,
            'nodeHeight': target.nodeHeight,
            'nodeCenterX': target.nodeCenterX,
            'nodeCenterY': target.nodeCenterY,
            'color': list(target.color),
            'comment': target.comment,
        }
    except Exception as e:
        return {'error': f'Failed to get position: {e}'}


def get_network_layout(ext, comp_path: str, include_annotations: bool = True) -> dict:
    """Get positions of all operators and annotations in a COMP"""
    parent_op = resolve_op(ext, comp_path)
    if not parent_op:
        return {'error': f'COMP not found: {comp_path}'}
    if not hasattr(parent_op, 'children'):
        return {'error': f'{comp_path} is not a COMP'}

    try:
        operators = []
        min_x = min_y = float('inf')
        max_x = max_y = float('-inf')

        for child in parent_op.children:
            entry = {
                'path': child.path,
                'type': child.OPType,
                'nodeX': child.nodeX,
                'nodeY': child.nodeY,
                'nodeWidth': child.nodeWidth,
                'nodeHeight': child.nodeHeight,
            }
            # Surface dock relationships so the layout Verify step can
            # check "every docked op hugs its host" mechanically.
            try:
                dock_host = child.dock
                if (dock_host is not None and dock_host.parent() is not None
                        and dock_host.parent().path == parent_op.path):
                    entry['dockedTo'] = dock_host.name
            except Exception:
                pass
            operators.append(entry)
            min_x = min(min_x, child.nodeX)
            min_y = min(min_y, child.nodeY)
            max_x = max(max_x, child.nodeX + child.nodeWidth)
            max_y = max(max_y, child.nodeY + child.nodeHeight)

        result = {
            'comp_path': comp_path,
            'count': len(operators),
            'operators': operators,
        }

        if operators:
            result['bounding_box'] = {
                'min_x': min_x,
                'min_y': min_y,
                'max_x': max_x,
                'max_y': max_y,
                'width': max_x - min_x,
                'height': max_y - min_y,
            }

        if include_annotations:
            annotations = []
            for child in parent_op.findChildren(type=annotateCOMP, includeUtility=True, depth=1):
                text = child.par.text.eval() if hasattr(child.par, 'text') else ''
                text = '' if text is None else str(text)
                if len(text) > 160:
                    text = text[:160] + '...'
                annotations.append({
                    'path': child.path,
                    'name': child.name,
                    'nodeX': child.nodeX,
                    'nodeY': child.nodeY,
                    'nodeWidth': child.nodeWidth,
                    'nodeHeight': child.nodeHeight,
                    'text': text,
                })
            result['annotations'] = annotations

        return ext._maybe_offload_to_file(result, 'get_network_layout')

    except Exception as e:
        return {'error': f'Failed to get network layout: {e}'}


def get_annotations(ext, parent_path: str) -> dict:
    """List all annotations in a COMP."""
    parent = resolve_op(ext, parent_path)
    if not parent:
        return {'error': f'Parent not found: {parent_path}'}
    if not parent.isCOMP:
        return {'error': f'{parent_path} is not a COMP'}

    try:
        import td as _td
        ann_class = getattr(_td, 'annotateCOMP', None)
        kwargs = {'includeUtility': True, 'depth': 1}
        if ann_class is not None:
            kwargs['type'] = ann_class

        annotations = parent.findChildren(**kwargs)

        # If class resolution failed, filter by type string
        if ann_class is None:
            annotations = [c for c in annotations if c.type == 'annotate']

        results = []
        for ann in annotations:
            info = {
                'path': ann.path,
                'name': ann.name,
                'mode': ann.par.Mode.eval(),
                'body_text': ann.par.Bodytext.eval(),
                'title_text': ann.par.Titletext.eval(),
                'nodeX': ann.nodeX,
                'nodeY': ann.nodeY,
                'nodeWidth': ann.nodeWidth,
                'nodeHeight': ann.nodeHeight,
                'opacity': ann.par.Opacity.eval(),
                'back_color': [
                    ann.par.Backcolorr.eval(),
                    ann.par.Backcolorg.eval(),
                    ann.par.Backcolorb.eval(),
                ],
                'enclosed_ops': [o.path for o in ann.enclosedOPs],
            }
            results.append(info)

        return {
            'parent': parent_path,
            'count': len(results),
            'annotations': results,
        }
    except Exception as e:
        return {'error': f'Failed to get annotations: {e}'}


def resolve_op(ext, op_path: str):
    """Resolve an operator path, tolerating utility-flagged hops.

    Bare op() first -- the fast path every normal operator takes. On
    failure, walk the path segment by segment from the root, retrying
    each unresolvable hop with findChildren(includeUtility=True,
    depth=1): utility ops (every UI-created annotation, and MCP-created
    ones since create_annotation adopted the utility=True convention)
    are HIDDEN from op()/parent.op()/.children, so a path whose leaf OR
    intermediate segment is a utility annotateCOMP fails the bare lookup
    even though the operator exists. This is the single resolver behind
    every op_path-taking Envoy tool; without it, tools error 'Operator
    not found' on paths get_annotations has just listed. Returns None
    when the operator genuinely does not exist. Never raises.
    """
    target = op(op_path)
    if target is not None:
        return target
    if not op_path or not isinstance(op_path, str) \
            or not op_path.startswith('/'):
        return None
    try:
        node = op('/')
        for seg in op_path.split('/'):
            if not seg:
                continue
            if not node.isCOMP:
                return None  # a non-COMP hop cannot have children
            nxt = node.op(seg)
            if nxt is None:
                nxt = next(
                    (c for c in node.findChildren(
                        depth=1, includeUtility=True)
                     if c.name == seg), None)
            if nxt is None:
                return None
            node = nxt
        return node
    except Exception:
        return None


def resolve_annotation(ext, op_path: str):
    """Resolve an annotation path, including utility-flagged ones.

    Utility ops (every UI-created annotation, and MCP-created ones per
    the utility=True convention) are HIDDEN from op(), parent.op() and
    .children -- only findChildren(includeUtility=True) sees them. A
    plain op() lookup therefore fails for exactly the annotations this
    tool exists to modify.
    """
    target = op(op_path)
    if target is not None:
        return target
    parent_path, _, name = op_path.rpartition('/')
    parent = resolve_op(ext, parent_path) if parent_path else None
    if not parent or not name:
        return None
    try:
        import td as _td
        ann_class = getattr(_td, 'annotateCOMP', None)
        kwargs = {'includeUtility': True, 'depth': 1}
        if ann_class is not None:
            kwargs['type'] = ann_class
        for candidate in parent.findChildren(**kwargs):
            if candidate.name == name:
                return candidate
    except Exception:
        return None
    return None


def get_enclosed_ops(ext, op_path: str) -> dict:
    """Get annotation/operator enclosure relationships."""
    target = resolve_annotation(ext, op_path)
    if not target:
        return {'error': f'Operator not found: {op_path}'}

    try:
        if target.type == 'annotate':
            enclosed = target.enclosedOPs
            return {
                'path': op_path,
                'is_annotation': True,
                'enclosed_ops': [
                    {'path': o.path, 'name': o.name, 'type': o.OPType}
                    for o in enclosed
                ],
                'count': len(enclosed),
            }
        else:
            enclosing = target.enclosedBy
            return {
                'path': op_path,
                'is_annotation': False,
                'enclosing_annotations': [
                    {'path': a.path, 'name': a.name, 'mode': a.par.Mode.eval()}
                    for a in enclosing
                ],
                'count': len(enclosing),
            }
    except Exception as e:
        return {'error': f'Failed to get enclosure info: {e}'}


def find_children(ext, op_path: str, name: str = None, type: str = None,
                  depth: int = None, tags: list = None,
                  text: str = None, comment: str = None,
                  include_utility: bool = False) -> dict:
    """Search for operators using COMP.findChildren"""
    target = resolve_op(ext, op_path)
    if not target:
        return {'error': f'Operator not found: {op_path}'}
    if not target.isCOMP:
        return {'error': f'{op_path} is not a COMP'}

    try:
        import td as _td
        kwargs = {}
        if name is not None:
            kwargs['name'] = name
        if type is not None:
            # Try to resolve as a TD class (e.g., "baseCOMP" -> td.baseCOMP)
            td_class = getattr(_td, type, None)
            if td_class is not None and hasattr(td_class, '__mro__'):
                kwargs['type'] = td_class
            # Otherwise we'll filter by OPType string after search
        if depth is not None:
            kwargs['depth'] = depth
        if tags is not None:
            kwargs['tags'] = tags
        if text is not None:
            kwargs['text'] = text
        if comment is not None:
            kwargs['comment'] = comment
        if include_utility:
            kwargs['includeUtility'] = True

        children = target.findChildren(**kwargs)

        # If type was provided but not resolved to a TD class, filter by OPType/type string
        if type is not None and 'type' not in kwargs:
            children = [c for c in children if c.OPType == type or c.type == type]

        results = []
        for child in children:
            results.append({
                'path': child.path,
                'name': child.name,
                'type': child.OPType,
                'family': child.family,
            })
        return {
            'parent': op_path,
            'count': len(results),
            'operators': results
        }
    except Exception as e:
        return {'error': f'Failed to find children: {e}'}


def get_op_performance(ext, op_path: str, include_children: bool = False) -> dict:
    """Get performance data for an operator"""
    target = resolve_op(ext, op_path)
    if not target:
        return {'error': f'Operator not found: {op_path}'}

    try:
        result = {
            'path': op_path,
            'cpuCookTime': target.cpuCookTime,
            'gpuCookTime': target.gpuCookTime,
            'cookFrame': target.cookFrame,
            'cookedThisFrame': target.cookedThisFrame,
            'totalCooks': target.totalCooks,
            'cpuMemory': target.cpuMemory,
            'gpuMemory': target.gpuMemory,
        }

        if include_children and target.isCOMP:
            result['childrenCPUCookTime'] = target.childrenCPUCookTime
            result['childrenGPUCookTime'] = target.childrenGPUCookTime
            result['childrenCPUMemory'] = target.childrenCPUMemory()
            result['childrenGPUMemory'] = target.childrenGPUMemory()

        return result
    except Exception as e:
        return {'error': f'Failed to get performance: {e}'}


def get_project_performance(ext, include_hotspots: int = 0) -> dict:
    """Get project-level performance via Perform CHOP."""
    try:
        perform = ext.ownerComp.op('_envoy_perform')
        if not perform:
            return {'error': 'Perform CHOP (_envoy_perform) not found inside Embody'}

        def chan_val(name, default=0):
            ch = perform.chan(name)
            return ch.eval() if ch is not None else default

        result = {
            'timing': {
                'fps': chan_val('fps'),
                'frameTimeMs': chan_val('msec'),
                'cookRate': chan_val('cookrate'),
                'cookRealTime': bool(chan_val('cookrealtime')),
                'timeSliceMs': chan_val('timeslice_msec'),
                'timeSliceStep': chan_val('timeslice_step'),
            },
            'memory': {
                'gpuMemUsedMB': chan_val('gpu_mem_used'),
                'totalGpuMemMB': chan_val('total_gpu_mem'),
                'cpuMemUsedMB': chan_val('cpu_mem_used'),
            },
            'frameHealth': {
                'droppedFrames': int(chan_val('dropped_frames')),
                'cookedLastFrame': bool(chan_val('cook')),
                'activeOps': int(chan_val('active_ops')),
                'totalOps': int(chan_val('total_ops')),
            },
            'gpu': {
                'chipTemperatureC': chan_val('gpu0_chip_temp'),
                'boardTemperatureC': chan_val('gpu0_board_temp'),
            },
            'performMode': bool(chan_val('perform_mode')),
        }

        if include_hotspots > 0:
            result['hotspots'] = get_performance_hotspots(ext, include_hotspots)

        return result
    except Exception as e:
        return {'error': f'Failed to get project performance: {e}'}


def get_performance_hotspots(ext, top_n: int) -> list:
    """Return the top N most expensive COMPs by combined cook time."""
    comps = []
    for child in root.findChildren(type=COMP, maxDepth=1):
        cpu_cook = child.childrenCPUCookTime
        gpu_cook = child.childrenGPUCookTime
        comps.append({
            'path': child.path,
            'name': child.name,
            'cpuCookTimeMs': cpu_cook,
            'gpuCookTimeMs': gpu_cook,
            'combinedCookTimeMs': cpu_cook + gpu_cook,
            'cpuMemoryBytes': child.childrenCPUMemory(),
            'gpuMemoryBytes': child.childrenGPUMemory(),
        })
    comps.sort(key=lambda c: c['combinedCookTimeMs'], reverse=True)
    return comps[:top_n]


def get_externalizations(ext) -> dict:
    """Get all externalized operators"""
    try:
        table = op.Embody.ext.Embody.Externalizations
        if not table:
            return {'error': 'Externalizations table not found'}

        headers = [table[0, c].val for c in range(table.numCols)]
        has_strategy = 'strategy' in headers
        externalizations = []
        for row in range(1, table.numRows):
            rel = table[row, 'rel_file_path'].val
            strategy = (table[row, 'strategy'].val if has_strategy
                        else table[row, 'type'].val) or 'tox'
            try:
                abs_path = str(op.Embody.ext.Embody.buildAbsolutePath(
                    op.Embody.ext.Embody.normalizePath(rel)))
            except Exception:
                abs_path = rel
            externalizations.append({
                'path': table[row, 'path'].val,
                'type': table[row, 'type'].val,
                'strategy': strategy,
                'file_path': rel,
                'absolute_path': abs_path,
                'timestamp': table[row, 'timestamp'].val,
                # Runtime-only since 2026-08-20; the tsv column is blank.
                'dirty': op.Embody.ext.Embody.DirtyState(
                    table[row, 'path'].val),
                'build': table[row, 'build'].val,
                # Hint so an agent seeing a dirty TDN row knows the tool
                # that explains exactly what changed (live vs on-disk).
                'recommended_tool': 'diff_tdn' if strategy == 'tdn' else None,
            })

        return {
            'count': len(externalizations),
            'externalizations': externalizations
        }
    except Exception as e:
        return {'error': f'Failed to get externalizations: {e}'}


def get_externalization_status(ext, op_path: str) -> dict:
    """Get externalization status for an operator"""
    target = resolve_op(ext, op_path)
    if not target:
        return {'error': f'Operator not found: {op_path}'}

    try:
        table = op.Embody.ext.Embody.Externalizations
        if not table:
            return {'error': 'Externalizations table not found'}

        # Find the row for this operator
        headers = [table[0, c].val for c in range(table.numCols)]
        has_strategy = 'strategy' in headers
        for row in range(1, table.numRows):
            if table[row, 'path'].val == op_path:
                rel = table[row, 'rel_file_path'].val
                strategy = (table[row, 'strategy'].val if has_strategy
                            else table[row, 'type'].val) or 'tox'
                try:
                    abs_path = str(op.Embody.ext.Embody.buildAbsolutePath(
                        op.Embody.ext.Embody.normalizePath(rel)))
                except Exception:
                    abs_path = rel
                return {
                    'path': op_path,
                    'externalized': True,
                    'type': table[row, 'type'].val,
                    'strategy': strategy,
                    'file_path': rel,
                    'absolute_path': abs_path,
                    'timestamp': table[row, 'timestamp'].val,
                    # Runtime-only since 2026-08-20; tsv column is blank.
                    'dirty': op.Embody.ext.Embody.DirtyState(op_path),
                    'build': table[row, 'build'].val,
                    'touch_build': table[row, 'touch_build'].val,
                    # Hint so an agent seeing a dirty TDN row knows the
                    # tool that explains what changed (live vs on-disk).
                    'recommended_tool': 'diff_tdn' if strategy == 'tdn' else None,
                }

        return {
            'path': op_path,
            'externalized': False
        }
    except Exception as e:
        return {'error': f'Failed to get status: {e}'}


def export_network(ext, root_path='/', include_dat_content=True,
                   output_file=None, max_depth=None, embed_all=False):
    """Delegate to TDN extension for network export."""
    if not getattr(ext.ownerComp.ext, 'TDN', None):
        return {'error': 'TDN extension not loaded on Embody COMP'}
    # Protect .tdn files belonging to other tracked TDN COMPs
    protected = ext.ownerComp.ext.Embody._getAllTrackedTDNFiles(
        exclude_path=root_path) if output_file else None
    result = ext.ownerComp.ext.TDN.ExportNetwork(
        root_path=root_path,
        include_dat_content=include_dat_content,
        output_file=output_file,
        max_depth=max_depth,
        cleanup_protected=protected,
        embed_all=embed_all,
    )
    # Token-lean: when the .tdn was written to a file, don't echo the whole
    # document back -- return a compact summary and let the caller Read the
    # file on demand (CLAUDE.md already prefers reading .tdn from disk).
    if (output_file and isinstance(result, dict)
            and result.get('file') and isinstance(result.get('tdn'), dict)):
        doc = result.pop('tdn')
        result['summary'] = {
            'network_path': doc.get('network_path'),
            'version': doc.get('version'),
            'operators': len(doc.get('operators', [])),
            'annotations': len(doc.get('annotations', [])),
        }
        result['note'] = ('Full .tdn written to file; Read the file for '
                          'operators, params and DAT content.')
    return result


def read_tdn(ext, comp_path='/', include_dat_content=None,
             max_depth=None, embed_all=False):
    """Read a network subtree as a TDN dict (in-memory, no disk write).

    Thin delegate over TDN.ExportNetwork(output_file=None). Kept as a
    separate MCP tool so LLM-facing docs can emphasize the token-cost
    win vs get_op/query_network walks.
    """
    if not getattr(ext.ownerComp.ext, 'TDN', None):
        return {'error': 'TDN extension not loaded on Embody COMP'}
    return ext.ownerComp.ext.TDN.ExportNetwork(
        root_path=comp_path,
        include_dat_content=include_dat_content,
        output_file=None,
        max_depth=max_depth,
        embed_all=embed_all,
    )


def resolve_diff_target(ext, target):
    """Resolve a diff_tdn target to a TDN COMP path.

    Accepts either a COMP path directly, or a .tdn file reference (absolute
    path, repo-relative path, or bare filename like "tooltip.tdn"), which is
    reverse-resolved through the externalizations table. Returns
    (comp_path, None) on success or (None, error_message)."""
    # A live COMP path wins outright.
    if op(target) is not None:
        return target, None
    # Otherwise treat it as a .tdn file reference.
    try:
        table = op.Embody.ext.Embody.Externalizations
    except Exception:
        table = None
    if not table:
        return None, ('Operator not found and externalizations table '
                      'unavailable: %s' % target)
    norm = str(target).replace('\\', '/')
    base = norm.rsplit('/', 1)[-1]
    headers = [table[0, c].val for c in range(table.numCols)]
    has_strategy = 'strategy' in headers
    matches = []
    for row in range(1, table.numRows):
        rel = (table[row, 'rel_file_path'].val or '').replace('\\', '/')
        try:
            abs_p = str(op.Embody.ext.Embody.buildAbsolutePath(
                op.Embody.ext.Embody.normalizePath(rel))).replace('\\', '/')
        except Exception:
            abs_p = rel
        strat = (table[row, 'strategy'].val if has_strategy
                 else table[row, 'type'].val) or 'tox'
        if norm in (abs_p, rel) or base == rel.rsplit('/', 1)[-1]:
            matches.append((table[row, 'path'].val, strat))
    if not matches:
        return None, ('No externalized COMP found for %r (not a COMP path '
                      'or a tracked .tdn file)' % target)
    if len(matches) > 1:
        comps = ', '.join(m[0] for m in matches)
        return None, ('Ambiguous: %r matches multiple externalized files '
                      '(%s). Pass the COMP path instead.' % (target, comps))
    comp_path, strat = matches[0]
    if strat != 'tdn':
        return None, ('%s is externalized as %s, not tdn -- diff_tdn only '
                      'applies to TDN-strategy COMPs.' % (comp_path, strat))
    return comp_path, None


def diff_tdn(ext, target='', max_changed_ops=200, max_bytes=60000):
    """Show what is UNSAVED in TDN-externalized COMPs: live network(s) vs
    the on-disk .tdn(s) -- the view git cannot provide.

    `target` empty (or '/', 'project', '.', '*') -> PROJECT-WIDE: every live
    TDN COMP, summarized (which changed + counts). Otherwise `target` is a
    COMP path OR a .tdn file path/bare filename (resolved via the
    externalizations table) -> that one COMP in full detail.

    For committed/history diffs use git (the .tdn git diff driver keeps
    those clean). Thin delegate to TDN.DiffLiveVsDisk / DiffAllLiveVsDisk.
    Read-only, non-interactive, pull-only.
    """
    if not getattr(ext.ownerComp.ext, 'TDN', None):
        return {'error': 'TDN extension not loaded on Embody COMP'}
    # Empty / whole-project target -> project-wide summary. Per-COMP detail
    # uses DiffAllLiveVsDisk's own (smaller) caps; the handler's
    # max_changed_ops governs the single-COMP path below.
    if not target or str(target).strip() in ('', '/', 'project', '.', '*'):
        return ext.ownerComp.ext.TDN.DiffAllLiveVsDisk(max_bytes=max_bytes)
    comp_path, err = resolve_diff_target(ext, target)
    if err:
        return {'error': err}
    return ext.ownerComp.ext.TDN.DiffLiveVsDisk(
        comp_path=comp_path,
        max_changed_ops=max_changed_ops, max_bytes=max_bytes)


def get_logs(ext, level=None, count=50, since_id=None, source=None):
    """Get filtered log entries from Embody's ring buffer."""
    buffer = getattr(op.Embody.ext.Embody, '_log_buffer', None)
    if buffer is None:
        return {'error': 'Log buffer not initialized'}

    count = min(count or 50, 200)
    entries = list(buffer)

    if since_id is not None:
        entries = [e for e in entries if e['id'] > since_id]
    if level:
        entries = [e for e in entries if e['level'] == level.upper()]
    if source:
        entries = [e for e in entries if source.lower() in e['source'].lower()]

    entries = entries[-count:]

    return {
        'entries': entries,
        'count': len(entries),
        'total_in_buffer': len(buffer),
        'latest_id': buffer[-1]['id'] if buffer else 0,
    }
