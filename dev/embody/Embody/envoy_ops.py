"""Envoy mutating tool handlers (module DAT).

Module DAT (mod.envoy_ops) called by EnvoyExt on the MAIN THREAD only.
Holds the private implementations behind the mutating MCP tools (create/
copy/delete/rename ops, parameters, connections, flags, position, DAT
content, annotations, externalization tagging, extension creation, network
import, cook). EnvoyExt keeps a thin delegating stub for each -- these
functions carry the real bodies.

No module-level TD access; every function takes the ext instance (`ext`)
as its first argument and reaches TD through it or through the TD globals
(op, ParMode, ...) that are available inside function bodies at call time.
Layout geometry stays on the ext (mod.envoy_layout stubs); calls that
route through the ext keep the `ext.` hop.
"""

from __future__ import annotations


def create_op(ext, parent_path: str, op_type: str, name: str = None) -> dict:
    """Create an operator"""
    parent = op(parent_path)
    if not parent:
        return {'error': f'Parent not found: {parent_path}'}

    if not hasattr(parent, 'create'):
        return {'error': f'Cannot create children in {parent_path} (not a COMP)'}

    try:
        # op_type can be a string like 'baseCOMP', 'noiseTOP', etc.
        new_op = parent.create(op_type, name) if name else parent.create(op_type)
        ext._find_non_overlapping_position(parent, new_op)
        docks_placed = ext._placeDockedOps(new_op)
        # Auto-externalize per the Envoy 'Autoexternalize' preference. create_op
        # is the single creation chokepoint, so tagging here removes the LLM from
        # the externalization decision entirely. Additive + boundary-scoped;
        # never raises (must not break op creation).
        auto_tag = None
        try:
            auto_tag = op.Embody.ext.Embody.AutoExternalizeNewOp(new_op)
        except Exception as e:
            ext._log(f'auto-externalize failed for {new_op.path}: {e}', 'WARNING')
        result = {
            'success': True,
            'path': new_op.path,
            'name': new_op.name,
            'type': new_op.OPType,
            'family': new_op.family,
            'nodeX': new_op.nodeX,
            'nodeY': new_op.nodeY
        }
        if docks_placed:
            result['docks_placed'] = docks_placed
        if auto_tag:
            result['externalized'] = auto_tag
        return result
    except Exception as e:
        return {'error': f'Failed to create operator: {e}'}


def delete_op(ext, op_path: str) -> dict:
    """Delete an operator"""
    target = op(op_path)
    if not target:
        return {'error': f'Operator not found: {op_path}'}

    try:
        name = target.name
        # Purge externalization tracking (ANY strategy) for this op + any
        # tracked descendant BEFORE destroying: an unsaved TDN delete + crash
        # can't leave an orphan row that export-mode autosave recovery would
        # resurrect on next open, and non-TDN rows/files no longer outlive
        # their deleted op (issue #57 follow-up).
        try:
            op.Embody.ext.Embody._purgeExternalizationTracking(op_path)
        except Exception:
            pass
        target.destroy()
        return {'success': True, 'deleted': op_path, 'name': name}
    except Exception as e:
        return {'error': f'Failed to delete operator: {e}'}


def set_parameter(ext, op_path: str, par_name: str, value=None,
                  mode: str = None, expr: str = None,
                  bind_expr: str = None) -> dict:
    """Set a parameter value, expression, bind expression, or mode"""
    target = op(op_path)
    if not target:
        return {'error': f'Operator not found: {op_path}'}

    if not hasattr(target.par, par_name):
        if not grow_sequence_for(ext, target, par_name):
            return {'error': f'Parameter not found: {par_name}'}

    try:
        par = getattr(target.par, par_name)

        # Set expression (automatically switches to EXPRESSION mode)
        if expr is not None:
            par.expr = expr
            par.mode = ParMode.EXPRESSION
        # Set bind expression (automatically switches to BIND mode)
        elif bind_expr is not None:
            par.bindExpr = bind_expr
            par.mode = ParMode.BIND
        # Set constant value (with type coercion for numeric/toggle pars)
        elif value is not None:
            # TD silently coerces invalid Menu values to index 0 and reports
            # success; a lying success is worse than an error (guard adapted
            # from TDMCP).
            if (isinstance(value, str) and par.isMenu and par.style == 'Menu'
                    and value not in par.menuNames):
                menu_names = list(par.menuNames)
                menu_labels = list(par.menuLabels)
                msg = f'Invalid menu value {value!r} for {par_name}.'
                if value in menu_labels:
                    label_index = menu_labels.index(value)
                    if label_index < len(menu_names):
                        msg += f' Use menuNames value {menu_names[label_index]!r}.'
                return {
                    'error': msg,
                    'menuNames': menu_names,
                    'menuLabels': menu_labels,
                }
            if isinstance(value, str) and par.isNumber:
                try:
                    value = int(value) if par.isInt else float(value)
                except (ValueError, TypeError):
                    pass  # Let TD handle the string as-is
            elif isinstance(value, str) and par.isToggle:
                value = value not in ('0', 'false', 'False', '')
            par.val = value

        # Set mode explicitly if provided (overrides auto-set above)
        if mode is not None:
            mode_map = {
                'constant': ParMode.CONSTANT,
                'expression': ParMode.EXPRESSION,
                'export': ParMode.EXPORT,
                'bind': ParMode.BIND,
            }
            par_mode = mode_map.get(mode.lower())
            if par_mode is None:
                return {'error': f'Invalid mode: {mode}. Use: constant, expression, export, bind'}
            par.mode = par_mode

        return {
            'success': True,
            'path': op_path,
            'parameter': par_name,
            'value': str(par.eval()),
            'mode': str(par.mode)
        }
    except Exception as e:
        return {'error': f'Failed to set parameter: {e}'}


def grow_sequence_for(ext, target, par_name: str) -> bool:
    """Sequence blocks do not exist until numBlocks grows (e.g. const5name
    on a Constant CHOP with 3 blocks). Auto-grow the sequence so agents can
    address block N directly (adapted from TDMCP's _ensure_seq_block).
    Returns True when the parameter exists afterwards."""
    match = ext._SEQ_PAR_RE.match(par_name or '')
    if not match:
        return False
    prefix = match.group(1)
    idx = int(match.group(2))
    try:
        sequences = getattr(target, 'seq', None)
        seq = getattr(sequences, prefix, None) if sequences is not None else None
    except Exception:
        seq = None
    if seq is None:
        return False
    try:
        if idx >= seq.numBlocks:
            if idx >= 100:
                return False
            if seq.numBlocks >= 1:
                # Validate the block-0 suffix first so typos like
                # const5nam do not grow numBlocks before failing.
                if not hasattr(target.par, f'{prefix}0{match.group(3)}'):
                    return False
            seq.numBlocks = idx + 1
    except Exception:
        return False
    try:
        return hasattr(target.par, par_name)
    except Exception:
        return False


def connect_ops(ext, source_path: str, dest_path: str,
                source_index: int = 0, dest_index: int = 0,
                comp: bool = False) -> dict:
    """Connect two operators"""
    source = op(source_path)
    dest = op(dest_path)

    if not source:
        return {'error': f'Source not found: {source_path}'}
    if not dest:
        return {'error': f'Destination not found: {dest_path}'}

    try:
        if comp:
            if not hasattr(source, 'outputCOMPConnectors'):
                return {'error': f'Source {source_path} has no COMP connectors (not a COMP)'}
            if not hasattr(dest, 'inputCOMPConnectors'):
                return {'error': f'Destination {dest_path} has no COMP connectors (not a COMP)'}
            if source_index >= len(source.outputCOMPConnectors):
                return {'error': f'Source COMP output index {source_index} out of range'}
            if dest_index >= len(dest.inputCOMPConnectors):
                return {'error': f'Destination COMP input index {dest_index} out of range'}
            source.outputCOMPConnectors[source_index].connect(dest.inputCOMPConnectors[dest_index])
        else:
            if source_index >= len(source.outputConnectors):
                return {'error': f'Source output index {source_index} out of range'}
            if dest_index >= len(dest.inputConnectors):
                return {'error': f'Destination input index {dest_index} out of range'}
            source.outputConnectors[source_index].connect(dest.inputConnectors[dest_index])

        return {
            'success': True,
            'source': source_path,
            'destination': dest_path,
            'source_index': source_index,
            'dest_index': dest_index,
            'comp': comp
        }
    except Exception as e:
        return {'error': f'Failed to connect: {e}'}


def disconnect_op(ext, op_path: str, input_index: int = 0,
                  comp: bool = False) -> dict:
    """Disconnect an operator's input"""
    target = op(op_path)
    if not target:
        return {'error': f'Operator not found: {op_path}'}

    try:
        if comp:
            if not hasattr(target, 'inputCOMPConnectors'):
                return {'error': f'{op_path} has no COMP connectors (not a COMP)'}
            if input_index >= len(target.inputCOMPConnectors):
                return {'error': f'COMP input index {input_index} out of range'}
            target.inputCOMPConnectors[input_index].disconnect()
        else:
            if input_index >= len(target.inputConnectors):
                return {'error': f'Input index {input_index} out of range'}
            target.inputConnectors[input_index].disconnect()

        return {'success': True, 'path': op_path, 'input_index': input_index, 'comp': comp}
    except Exception as e:
        return {'error': f'Failed to disconnect: {e}'}


def copy_op(ext, source_path: str, dest_parent: str, new_name: str = None) -> dict:
    """Copy an operator"""
    source = op(source_path)
    dest = op(dest_parent)

    if not source:
        return {'error': f'Source not found: {source_path}'}
    if not dest:
        return {'error': f'Destination parent not found: {dest_parent}'}
    if not hasattr(dest, 'copy'):
        return {'error': f'{dest_parent} is not a COMP'}

    try:
        new_op = dest.copy(source, name=new_name) if new_name else dest.copy(source)
        ext._find_non_overlapping_position(dest, new_op)
        docks_placed = ext._placeDockedOps(new_op)
        # Auto-externalize the COPY per the Autoexternalize preference. The
        # copied-op path clears externalization state inherited from the
        # source (tags + file refs) so the copy is externalized fresh at its
        # own path and never shares the source's files. Never breaks the copy.
        auto_tag = None
        try:
            auto_tag = op.Embody.ext.Embody.AutoExternalizeCopiedOp(new_op)
        except Exception as e:
            ext._log(f'auto-externalize (copy) failed for {new_op.path}: {e}', 'WARNING')
        result = {
            'success': True,
            'source': source_path,
            'new_path': new_op.path,
            'new_name': new_op.name,
            'nodeX': new_op.nodeX,
            'nodeY': new_op.nodeY
        }
        if docks_placed:
            result['docks_placed'] = docks_placed
        if auto_tag:
            result['externalized'] = auto_tag
        return result
    except Exception as e:
        return {'error': f'Failed to copy: {e}'}


def set_dat_content(ext, op_path: str, text: str = None,
                    rows: list = None, clear: bool = False,
                    confirm_wipe: bool = False) -> dict:
    """Set DAT content from text or table rows.

    Two guardrails:
    1. No-content guard: refuses calls with no actionable content
       (text=None, rows=None, clear=False) -- caller passed nothing.
    2. Wipe guard: refuses calls that would leave the DAT empty
       (text='', rows=[], or clear=True without replacement content)
       unless confirm_wipe=True. Catches the common accident pattern
       where an agent sends empty content from a malformed call and
       silently destroys user content.

    Note: `clear=True` is redundant when `text` or `rows` is also
    provided -- the assignment already replaces the entire content.
    """
    target = op(op_path)
    if not target:
        return {'error': f'Operator not found: {op_path}'}
    if target.family != 'DAT':
        return {'error': f'{op_path} is not a DAT (family: {target.family})'}

    # No-content guard: caller passed nothing actionable. This is the
    # same failure shape as a wipe (silent confused call returning
    # success) so we refuse it the same way.
    if text is None and rows is None and not clear:
        return {'error': (
            f'No content provided for {op_path}. Pass text=, rows=, '
            f'or clear=True with confirm_wipe=True. set_dat_content '
            f'is full-replace -- if you want to edit existing content, '
            f'call get_dat_content first to read it.'
        )}

    # Wipe detection -- check the *resulting* state, not just inputs.
    # `clear=True, text="hello"` is an atomic replace, NOT a wipe.
    if not confirm_wipe:
        wipe_reason = None
        if text == '':
            wipe_reason = "text=''"
        elif rows is not None and len(rows) == 0:
            wipe_reason = 'rows=[]'
        elif clear and text is None and rows is None:
            wipe_reason = 'clear=True with no replacement content'
        if wipe_reason is not None:
            return {'error': (
                f'Refusing to wipe DAT {op_path}: call would set '
                f'content to empty ({wipe_reason}). This is almost '
                f'always an accident -- set_dat_content is full-'
                f'replace, not incremental. Likely fix: call '
                f'get_dat_content first, edit the returned content, '
                f'then send the complete result. Only retry with '
                f'confirm_wipe=True if you have already verified the '
                f'DAT must become empty (e.g. resetting a FIFO log).'
            )}

    try:
        if clear:
            target.clear()

        if text is not None:
            target.text = text
        elif rows is not None:
            target.clear()
            for row in rows:
                target.appendRow(row)

        return {
            'success': True,
            'path': op_path,
            'numRows': target.numRows,
            'numCols': target.numCols,
        }
    except Exception as e:
        return {'error': f'Failed to set DAT content: {e}'}


def edit_dat_content(ext, op_path: str, old_string: str,
                     new_string: str, replace_all: bool = False,
                     confirm_wipe: bool = False) -> dict:
    """Surgical text edit on a DAT -- replaces old_string with
    new_string. Mirrors Claude Code's Edit tool: by default
    old_string must appear exactly once in the DAT's text.

    Token-efficient alternative to set_dat_content for partial
    edits: only the changed substring crosses the wire, not the
    whole DAT.

    Text-only. Tables should use set_dat_content(rows=...) -- string
    matching across cells is a different beast.

    Guardrails:
    - Refuse empty old_string (would match every position).
    - Refuse old_string == new_string (no-op).
    - Refuse non-text DATs (point caller at set_dat_content).
    - Wipe guard: if the resulting text is empty, require
      confirm_wipe=True (mirrors set_dat_content semantics).
    - Not-found error includes diagnostics so the caller can
      self-correct without a second get_dat_content round-trip.
    """
    target = op(op_path)
    if not target:
        return {'error': f'Operator not found: {op_path}'}
    if target.family != 'DAT':
        return {'error': f'{op_path} is not a DAT (family: {target.family})'}
    if not target.isText:
        return {'error': (
            f'{op_path} is not a text DAT (isText=False). '
            f'edit_dat_content is text-only -- use set_dat_content '
            f'with rows= for table DATs.'
        )}

    if old_string == '':
        return {'error': (
            f'old_string is empty. edit_dat_content requires a '
            f'non-empty search string -- an empty string would '
            f'match every position in the DAT.'
        )}
    if old_string == new_string:
        return {'error': (
            f'old_string and new_string are identical -- this '
            f'would be a no-op. If you meant to verify content, '
            f'use get_dat_content instead.'
        )}

    current = target.text
    count = current.count(old_string)
    if count == 0:
        ci_match = old_string.lower() in current.lower()
        hint = (
            ' A case-insensitive search would have matched -- '
            'check the casing of old_string.'
        ) if ci_match else ''
        return {'error': (
            f'old_string not found in {op_path} '
            f'(DAT length: {len(current)} chars, '
            f'{target.numRows} rows).{hint} Call get_dat_content '
            f'to inspect current content.'
        )}
    if count > 1 and not replace_all:
        return {'error': (
            f'old_string appears {count} times in {op_path}; '
            f'edit_dat_content requires a unique match by default. '
            f'Either widen old_string with surrounding context to '
            f'make it unique, or pass replace_all=True to replace '
            f'every occurrence.'
        )}

    if replace_all:
        new_text = current.replace(old_string, new_string)
    else:
        new_text = current.replace(old_string, new_string, 1)

    if new_text == '' and not confirm_wipe:
        return {'error': (
            f'Refusing to wipe DAT {op_path}: this edit would '
            f'leave the DAT empty. Pass confirm_wipe=True if '
            f'this is intentional (e.g. resetting a FIFO log).'
        )}

    try:
        target.text = new_text
        return {
            'success': True,
            'path': op_path,
            'replacements': count if replace_all else 1,
            'numRows': target.numRows,
            'numCols': target.numCols,
        }
    except Exception as e:
        return {'error': f'Failed to edit DAT content: {e}'}


def set_op_flags(ext, op_path: str, bypass: bool = None, lock: bool = None,
                 display: bool = None, render: bool = None,
                 viewer: bool = None, current: bool = None,
                 expose: bool = None, allowCooking: bool = None,
                 selected: bool = None) -> dict:
    """Set flags on an operator"""
    target = op(op_path)
    if not target:
        return {'error': f'Operator not found: {op_path}'}

    try:
        if bypass is not None:
            target.bypass = bypass
        if lock is not None:
            target.lock = lock
        if display is not None:
            target.display = display
        if render is not None:
            target.render = render
        if viewer is not None:
            target.viewer = viewer
        if current is not None:
            target.current = current
        if expose is not None:
            target.expose = expose
        if selected is not None:
            target.selected = selected
        if allowCooking is not None and target.isCOMP:
            target.allowCooking = allowCooking

        return ext._get_op_flags(op_path)
    except Exception as e:
        return {'error': f'Failed to set flags: {e}'}


def set_op_position(ext, op_path: str, x: int = None, y: int = None,
                    width: int = None, height: int = None,
                    color: list = None, comment: str = None) -> dict:
    """Set operator position and visual properties"""
    target = op(op_path)
    if not target:
        return {'error': f'Operator not found: {op_path}'}

    try:
        if x is not None:
            target.nodeX = x
        if y is not None:
            target.nodeY = y
        if width is not None:
            target.nodeWidth = width
        if height is not None:
            target.nodeHeight = height
        if color is not None:
            target.color = tuple(color)
        if comment is not None:
            target.comment = comment

        # Docked companions follow their host: re-hug them below the new
        # position so a repositioned GLSL TOP / callback host never leaves
        # its shader/callback/info DATs stranded at the old spot.
        docks_moved = 0
        if x is not None or y is not None:
            docks_moved = ext._placeDockedOps(target)

        result = ext._get_op_position(op_path)
        if docks_moved:
            result['docks_moved'] = docks_moved

        # Check for overlaps with siblings after repositioning
        if (x is not None or y is not None) and target.parent():
            MARGIN = 20
            overlaps = []
            own_dock_paths = {d.path for d in ext._sameNetworkDocks(target)}
            tx, ty, tw, th = target.nodeX, target.nodeY, target.nodeWidth, target.nodeHeight
            for sibling in target.parent().children:
                if sibling.path == target.path:
                    continue
                if sibling.path in own_dock_paths:
                    continue
                if sibling.type == 'annotate':
                    continue  # annotations enclose ops by design
                sx, sy, sw, sh = sibling.nodeX, sibling.nodeY, sibling.nodeWidth, sibling.nodeHeight
                if (tx < sx + sw + MARGIN and tx + tw + MARGIN > sx and
                        ty < sy + sh + MARGIN and ty + th + MARGIN > sy):
                    overlaps.append(sibling.name)
            if overlaps:
                result['overlap_warning'] = f'Overlaps with: {", ".join(overlaps)}. Reposition to avoid.'

        return result
    except Exception as e:
        return {'error': f'Failed to set position: {e}'}


def create_annotation(ext, parent_path: str, mode: str = "annotate",
                      text: str = "", title: str = "",
                      x: int = None, y: int = None,
                      width: int = None, height: int = None,
                      color: list = None, opacity: float = None,
                      name: str = None) -> dict:
    """Create an annotation in the network editor."""
    parent = op(parent_path)
    if not parent:
        return {'error': f'Parent not found: {parent_path}'}
    if not hasattr(parent, 'create'):
        return {'error': f'Cannot create annotations in {parent_path} (not a COMP)'}

    valid_modes = ('comment', 'networkbox', 'annotate')
    if mode not in valid_modes:
        return {'error': f'Invalid mode: {mode}. Use: {", ".join(valid_modes)}'}

    try:
        ann = parent.create('annotateCOMP')

        # Set mode first (affects default sizing/appearance)
        ann.par.Mode = mode

        # Rename if requested (TD ignores name param on create for annotations)
        if name:
            ann.name = name

        # Set body text
        if text:
            ann.par.Bodytext = text

        # Set title text
        if title:
            ann.par.Titletext = title

        # Apply readable default sizes when not specified
        if width is None and height is None:
            # Estimate height from text line count
            lines = text.count('\n') + 1 if text else 1
            ann.nodeWidth = 400
            ann.nodeHeight = max(200, 60 + lines * 22)

        # Set position
        if x is not None:
            ann.nodeX = x
        if y is not None:
            ann.nodeY = y

        # Set explicit size (overrides defaults above)
        if width is not None:
            ann.nodeWidth = width
        if height is not None:
            ann.nodeHeight = height

        # Set background color
        if color is not None:
            ann.par.Backcolorr = color[0]
            ann.par.Backcolorg = color[1]
            ann.par.Backcolorb = color[2]

        # Set opacity
        if opacity is not None:
            ann.par.Opacity = opacity

        return {
            'success': True,
            'path': ann.path,
            'name': ann.name,
            'mode': mode,
            'nodeX': ann.nodeX,
            'nodeY': ann.nodeY,
            'nodeWidth': ann.nodeWidth,
            'nodeHeight': ann.nodeHeight,
        }
    except Exception as e:
        return {'error': f'Failed to create annotation: {e}'}


def set_annotation(ext, op_path: str, text: str = None, title: str = None,
                   color: list = None, opacity: float = None,
                   width: int = None, height: int = None,
                   x: int = None, y: int = None) -> dict:
    """Modify an existing annotation."""
    target = ext._resolve_annotation(op_path)
    if not target:
        return {'error': f'Annotation not found: {op_path}'}
    if target.type != 'annotate':
        return {'error': f'{op_path} is not an annotation (type: {target.type})'}

    try:
        if text is not None:
            target.par.Bodytext = text
        if title is not None:
            target.par.Titletext = title
        if color is not None:
            target.par.Backcolorr = color[0]
            target.par.Backcolorg = color[1]
            target.par.Backcolorb = color[2]
        if opacity is not None:
            target.par.Opacity = opacity
        if width is not None:
            target.nodeWidth = width
        if height is not None:
            target.nodeHeight = height
        if x is not None:
            target.nodeX = x
        if y is not None:
            target.nodeY = y

        return {
            'success': True,
            'path': op_path,
            'mode': target.par.Mode.eval(),
            'body_text': target.par.Bodytext.eval(),
            'title_text': target.par.Titletext.eval(),
            'nodeX': target.nodeX,
            'nodeY': target.nodeY,
            'nodeWidth': target.nodeWidth,
            'nodeHeight': target.nodeHeight,
            'opacity': target.par.Opacity.eval(),
        }
    except Exception as e:
        return {'error': f'Failed to set annotation: {e}'}


def rename_op(ext, op_path: str, new_name: str) -> dict:
    """Rename an operator"""
    target = op(op_path)
    if not target:
        return {'error': f'Operator not found: {op_path}'}

    try:
        old_name = target.name
        target.name = new_name
        return {
            'success': True,
            'old_name': old_name,
            'new_name': target.name,
            'new_path': target.path,
        }
    except Exception as e:
        return {'error': f'Failed to rename: {e}'}


def cook_op(ext, op_path: str, force: bool = True,
            recurse: bool = False) -> dict:
    """Cook an operator"""
    target = op(op_path)
    if not target:
        return {'error': f'Operator not found: {op_path}'}

    try:
        target.cook(force=force, recurse=recurse)
        return {
            'success': True,
            'path': op_path,
            'cookFrame': target.cookFrame,
        }
    except Exception as e:
        return {'error': f'Failed to cook: {e}'}


def externalize_op(ext, op_path: str, tag_type: str = None) -> dict:
    """Tag an operator for Embody externalization and write it to disk"""
    target = op(op_path)
    if not target:
        return {'error': f'Operator not found: {op_path}'}

    try:
        # Determine tag based on operator type if not specified
        if not tag_type:
            if target.family == 'COMP':
                tag_type = 'tox'
            elif target.family == 'DAT':
                tag_type = op.Embody.ext.Embody._inferDATTagValue(target)
            else:
                return {'error': f'Cannot externalize {target.family} operators'}

        # Apply the tag and run Update to externalize to disk
        op.Embody.ext.Embody.applyTagToOperator(target, tag_type)
        op.Embody.Update()

        file_path = target.par.file.eval() if target.family == 'DAT' else target.par.externaltox.eval()
        return {
            'success': True,
            'path': op_path,
            'tag': tag_type,
            'file': file_path
        }
    except Exception as e:
        return {'error': f'Failed to tag: {e}'}


def remove_externalization_tag(ext, op_path: str) -> dict:
    """Remove Embody externalization tag and clean up"""
    target = op(op_path)
    if not target:
        return {'error': f'Operator not found: {op_path}'}

    try:
        # Get all tags and remove them
        tags = op.Embody.ext.Embody.getTags()
        removed = []
        for tag in tags:
            if target.tags and tag in target.tags:
                target.tags.remove(tag)
                removed.append(tag)

        # Run Update to process the subtraction
        if removed:
            op.Embody.Update()

        return {
            'success': True,
            'path': op_path,
            'removed_tags': removed
        }
    except Exception as e:
        return {'error': f'Failed to remove tag: {e}'}


def save_externalization(ext, op_path: str) -> dict:
    """Force save an externalized operator"""
    target = op(op_path)
    if not target:
        return {'error': f'Operator not found: {op_path}'}

    try:
        if target.family == 'COMP':
            strategy = op.Embody.ext.Embody._getCompStrategy(target)
            if strategy == 'tdn':
                op.Embody.SaveTDN(op_path)
            else:
                op.Embody.Save(op_path)
        elif target.family == 'DAT':
            if hasattr(target.par, 'syncfile') and target.par.syncfile.eval():
                return {
                    'success': True,
                    'path': op_path,
                    'note': 'DAT is file-synced automatically by TouchDesigner'
                }
            else:
                return {'error': f'DAT at {op_path} does not have file sync enabled -- not externalized'}
        else:
            return {'error': f'Operator family "{target.family}" is not supported for save_externalization'}

        return {'success': True, 'path': op_path}
    except Exception as e:
        return {'error': f'Failed to save: {e}'}


def create_extension(ext, parent_path: str, class_name: str,
                     name: str = None, code: str = None,
                     promote: bool = True, ext_name: str = None,
                     ext_index: int = None,
                     existing_comp: bool = False) -> dict:
    """Create a TD extension: COMP + text DAT + extension wiring"""

    # Validate class_name
    if not class_name.isidentifier():
        return {'error': f'class_name must be a valid Python identifier, got: {class_name}'}

    # Resolve or create the target COMP
    created_comp = False
    if existing_comp:
        comp = op(parent_path)
        if not comp:
            return {'error': f'Operator not found: {parent_path}'}
        if not comp.isCOMP:
            return {'error': f'{parent_path} is not a COMP'}
    else:
        parent_op = op(parent_path)
        if not parent_op:
            return {'error': f'Parent not found: {parent_path}'}
        if not hasattr(parent_op, 'create'):
            return {'error': f'Cannot create children in {parent_path} (not a COMP)'}
        comp_name = name or class_name
        try:
            comp = parent_op.create('baseCOMP', comp_name)
            ext._find_non_overlapping_position(parent_op, comp)
            created_comp = True
        except Exception as e:
            return {'error': f'Failed to create COMP: {e}'}

    # Find extension slot
    if ext_index is not None:
        if not (0 <= ext_index <= 3):
            if created_comp:
                comp.destroy()
            return {'error': f'ext_index must be 0-3, got {ext_index}'}
        existing_val = getattr(comp.par, f'ext{ext_index}object').eval()
        if existing_val:
            if created_comp:
                comp.destroy()
            return {'error': f'Extension slot {ext_index} is already in use (set to: {existing_val})'}
    else:
        ext_index = None
        for i in range(4):
            if not getattr(comp.par, f'ext{i}object').eval():
                ext_index = i
                break
        if ext_index is None:
            if created_comp:
                comp.destroy()
            return {'error': f'All 4 extension slots are occupied on {comp.path}'}

    # Check for name collision inside the COMP
    existing_dat = comp.op(class_name)
    if existing_dat:
        if created_comp:
            comp.destroy()
        return {'error': f"An operator named '{class_name}' already exists in {comp.path}"}

    # Create text DAT
    try:
        text_dat = comp.create('textDAT', class_name)
    except Exception as e:
        if created_comp:
            comp.destroy()
        return {'error': f'Failed to create text DAT: {e}'}

    # Write extension code
    if code:
        text_dat.text = code
    else:
        text_dat.text = (
            f'class {class_name}:\n'
            f'    """\n'
            f'    {class_name} description.\n'
            f'    """\n'
            f'\n'
            f'    def __init__(self, ownerComp):\n'
            f'        self.ownerComp = ownerComp\n'
        )

    # Set extension parameters
    effective_ext_name = ext_name or class_name
    try:
        getattr(comp.par, f'ext{ext_index}object').val = (
            f"op('./{class_name}').module.{class_name}(me)"
        )
        getattr(comp.par, f'ext{ext_index}name').val = effective_ext_name
        getattr(comp.par, f'ext{ext_index}promote').val = promote
    except Exception as e:
        if created_comp:
            comp.destroy()
        else:
            text_dat.destroy()
        return {'error': f'Failed to set extension parameters: {e}'}

    # Initialize the extension
    init_warning = None
    try:
        comp.initializeExtensions(ext_index)
    except Exception as e:
        init_warning = f'Extension created but initialization failed: {e}. Check the code for errors.'

    # Ensure viewer flag stays off (initializeExtensions can activate it)
    comp.viewer = False

    # Auto-externalize per the Autoexternalize preference. Externalize the
    # host COMP only if WE created it (COMP -> TDN; the code DAT is then
    # captured inside it). The code DAT is always a fresh op: under 'dats'
    # (COMP not externalized) it becomes its own .py; under 'comps'/'both'
    # the COMP's TDN already captures it, so its own call boundary-skips.
    auto_ext = {}
    try:
        emb = op.Embody.ext.Embody
        if created_comp:
            t = emb.AutoExternalizeNewOp(comp)
            if t:
                auto_ext['comp'] = t
        t = emb.AutoExternalizeNewOp(text_dat)
        if t:
            auto_ext['dat'] = t
    except Exception as e:
        ext._log(f'auto-externalize (extension) failed for {comp.path}: {e}', 'WARNING')

    result = {
        'success': True,
        'comp_path': comp.path,
        'comp_name': comp.name,
        'dat_path': text_dat.path,
        'class_name': class_name,
        'ext_index': ext_index,
        'ext_name': effective_ext_name,
        'promote': promote,
        'created_comp': created_comp,
    }
    if auto_ext:
        result['externalized'] = auto_ext

    if init_warning:
        result['warning'] = init_warning

    return result


def import_network(ext, target_path, tdn, clear_first=False):
    """Delegate to TDN extension for network import."""
    if not getattr(ext.ownerComp.ext, 'TDN', None):
        return {'error': 'TDN extension not loaded on Embody COMP'}
    return ext.ownerComp.ext.TDN.ImportNetwork(
        target_path=target_path,
        tdn=tdn,
        clear_first=clear_first,
    )
