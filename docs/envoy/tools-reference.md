# Tools Reference

Envoy exposes 40+ MCP tools for interacting with TouchDesigner. All tools use the standard MCP protocol and can be called by any compatible client.

## Operator Management

| Tool | Parameters | Description |
|------|-----------|-------------|
| `create_op` | `parent_path`, `op_type`, `name?` | Create a new operator (e.g., `baseCOMP`, `noiseTOP`, `textDAT`, `gridPOP`) |
| `create_extension` | `parent_path`, `class_name`, `name?`, `code?`, `promote?`, `ext_name?`, `ext_index?`, `existing_comp?` | Create a TD extension: baseCOMP + text DAT + extension wiring, initialized and ready to use |
| `delete_op` | `op_path` | Delete an operator |
| `copy_op` | `source_path`, `dest_parent`, `new_name?` | Copy operator to new location |
| `rename_op` | `op_path`, `new_name` | Rename an operator |
| `get_op` | `op_path` | Get full operator info (type, family, parameters, inputs, outputs, children) |
| `query_network` | `parent_path?`, `recursive?`, `op_type?`, `include_utility?` | List operators in a container. Set `include_utility=True` to include annotations |
| `find_children` | `op_path`, `name?`, `type?`, `depth?`, `tags?`, `text?`, `comment?`, `include_utility?` | Advanced search using TD's `findChildren` — filter by name pattern, type, depth, tags, text content, or comment |
| `cook_op` | `op_path`, `force?`, `recurse?` | Force-cook an operator |

## Parameter Control

| Tool | Parameters | Description |
|------|-----------|-------------|
| `set_parameter` | `op_path`, `par_name`, `value?`, `mode?`, `expr?`, `bind_expr?` | Set a parameter's value, expression, bind expression, or mode (`constant`/`expression`/`export`/`bind`) |
| `get_parameter` | `op_path`, `par_name` | Get parameter value, mode, expression, bind info, export source, label, range, menu entries, and default |

## DAT Content

| Tool | Parameters | Description |
|------|-----------|-------------|
| `get_dat_content` | `op_path`, `format?` | Get DAT text or table data (`"text"`, `"table"`, or `"auto"`) |
| `set_dat_content` | `op_path`, `text?`, `rows?`, `clear?` | Set DAT content from text string or list of row lists |

## Operator Flags

| Tool | Parameters | Description |
|------|-----------|-------------|
| `get_op_flags` | `op_path` | Get all flags: bypass, lock, display, render, viewer, current, expose, selected, allowCooking |
| `set_op_flags` | `op_path`, `bypass?`, `lock?`, `display?`, `render?`, `viewer?`, `current?`, `expose?`, `allowCooking?`, `selected?` | Set one or more flags on an operator |

## Positioning & Layout

| Tool | Parameters | Description |
|------|-----------|-------------|
| `get_op_position` | `op_path` | Get operator position, size, color, and comment |
| `set_op_position` | `op_path`, `x?`, `y?`, `width?`, `height?`, `color?`, `comment?` | Set operator position, size, color (`[r,g,b]` floats 0-1), or comment |
| `layout_children` | `op_path` | Auto-layout all children in a COMP |

## Annotations

| Tool | Parameters | Description |
|------|-----------|-------------|
| `create_annotation` | `parent_path`, `mode?`, `text?`, `title?`, `x?`, `y?`, `width?`, `height?`, `color?`, `opacity?`, `name?` | Create an annotation. Modes: `"annotate"` (default, has title bar), `"comment"`, `"networkbox"` |
| `get_annotations` | `parent_path` | List all annotations in a COMP with their properties and enclosed operators |
| `set_annotation` | `op_path`, `text?`, `title?`, `color?`, `opacity?`, `width?`, `height?`, `x?`, `y?` | Modify properties of an existing annotation |
| `get_enclosed_ops` | `op_path` | Get operators enclosed by an annotation, or annotations enclosing an operator |

## Connections

| Tool | Parameters | Description |
|------|-----------|-------------|
| `connect_ops` | `source_path`, `dest_path`, `source_index?`, `dest_index?`, `comp?` | Wire two operators together. Set `comp=True` for COMP connectors (top/bottom) |
| `disconnect_op` | `op_path`, `input_index?`, `comp?` | Disconnect an operator's input. Set `comp=True` for COMP connectors (top/bottom) |
| `get_connections` | `op_path` | Get all input/output connections (includes COMP connections for COMPs) |

## Performance Monitoring

| Tool | Parameters | Description |
|------|-----------|-------------|
| `get_op_performance` | `op_path`, `include_children?` | Get CPU/GPU cook times, memory usage, cook counts |
| `get_project_performance` | `include_hotspots?` | Get project-level FPS, frame time, GPU/CPU memory, dropped frames, active ops, GPU temp. Optional hotspot ranking of top N COMPs by cook time |

## Code Execution

| Tool | Parameters | Description |
|------|-----------|-------------|
| `execute_python` | `code` | Execute Python code in TD. Set `result` variable to return values |

## Introspection & Diagnostics

| Tool | Parameters | Description |
|------|-----------|-------------|
| `get_td_info` | _(none)_ | Get TD version, build, OS, and Envoy version |
| `get_op_errors` | `op_path`, `recurse?` | Get error and warning messages for an operator and its children |
| `exec_op_method` | `op_path`, `method`, `args?`, `kwargs?` | Call a method on an operator (e.g., `appendRow`, `cook`) |
| `get_td_classes` | _(none)_ | List all Python classes/modules in the `td` module |
| `get_td_class_details` | `class_name` | Get methods, properties, and docs for a TD class |
| `get_module_help` | `module_name` | Get Python help text for a module (supports dotted names like `td.tdu`) |

## Embody Integration

| Tool | Parameters | Description |
|------|-----------|-------------|
| `externalize_op` | `op_path`, `tag_type?` | Tag and externalize operator to disk (auto-detects type if omitted) |
| `remove_externalization_tag` | `op_path` | Remove externalization tag |
| `get_externalizations` | _(none)_ | List all externalized operators with status |
| `save_externalization` | `op_path` | Force save an externalized operator to disk |
| `get_externalization_status` | `op_path` | Get dirty state, build number, timestamp, file path |

## TDN Format

| Tool | Parameters | Description |
|------|-----------|-------------|
| `export_network` | `root_path?`, `include_dat_content?`, `output_file?`, `max_depth?` | Export network to `.tdn` JSON (non-default properties only) |
| `import_network` | `target_path`, `tdn`, `clear_first?` | Recreate a network from `.tdn` JSON |

## TOP Capture

| Tool | Parameters | Description |
|------|-----------|-------------|
| `capture_top` | `op_path`, `format?`, `quality?`, `max_resolution?` | Capture a TOP's output as an image. Saves to temp file and returns the path. Small images (<20 KB) also include an inline MCP `ImageContent` preview. Default: JPEG at 80% quality, max 640px long edge. |

## Logging

| Tool | Parameters | Description |
|------|-----------|-------------|
| `get_logs` | `level?`, `count?`, `since_id?`, `source?` | Get recent log entries from ring buffer. Filter by level, source, or use `since_id` for incremental polling |
| `run_tests` | `suite_name?`, `test_name?` | Run test suites and return results |

!!! info "Auto-piggybacked logs"
    Every MCP tool response includes a `_logs` field with up to 20 log entries generated since the previous tool call. This lets you monitor operations in real-time without needing to call `get_logs` separately.

## Bridge Meta-Tools

These tools run locally on the STDIO bridge script, not inside TouchDesigner. They work even when TD is not running — this is how Claude Code can launch or restart TD without an active Envoy connection.

| Tool | Parameters | Description |
|------|-----------|-------------|
| `get_td_status` | _(none)_ | Check if TD is running, Envoy reachable, crash detection, process liveness, restart attempts remaining |
| `launch_td` | `timeout?` | Launch TD with the project's `.toe` file. Waits for Envoy to become reachable (default: 120s) |
| `restart_td` | `timeout?` | Gracefully quit TD and relaunch. Waits for exit before relaunching (default: 120s) |
| `switch_instance` | `instance?` | List all registered TD instances (omit `instance`) or switch to a different running instance. See [Multiple Instances](architecture.md#multiple-instances) |

!!! info "Bridge architecture"
    Claude Code connects to Envoy via a STDIO bridge script (`.claude/envoy-bridge.py`). The bridge translates between Claude Code's STDIO transport and Envoy's HTTP endpoint. It handles MCP protocol handshake locally when TD is down, so these meta-tools are always available. See [Architecture](architecture.md) for details.

## MCP Prompts

| Prompt | Parameters | Description |
|--------|-----------|-------------|
| `search_op` | `op_name`, `op_type?` | Guide for searching operators by name |
| `check_op_errors` | `op_path` | Guide for inspecting and resolving operator errors |
| `connect_ops` | _(none)_ | Guide for wiring operators together |
| `create_extension_guide` | _(none)_ | Guide for creating TD extensions with proper patterns |
