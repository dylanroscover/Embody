# Tools Reference

Envoy exposes 53 MCP tools for interacting with TouchDesigner, plus 4 bridge meta-tools (listed below). All tools use the standard MCP protocol and can be called by any compatible client.

Every mutating TD-authoring tool call is wrapped in a TouchDesigner undo block. Press Ctrl+Z in TD to revert an agent change; a `batch_operations` call is one undo step for the whole batch.

Responses are compact by default; opt-in flags such as `include_defaults` and `details` return full detail when needed.

## Operator Management

| Tool | Parameters | Description |
|------|-----------|-------------|
| `create_op` | `parent_path`, `op_type`, `name?` | Create a new operator (e.g., `baseCOMP`, `noiseTOP`, `textDAT`, `gridPOP`) |
| `create_extension` | `parent_path`, `class_name`, `name?`, `code?`, `promote?`, `ext_name?`, `ext_index?`, `existing_comp?` | Create a TD extension: baseCOMP + text DAT + extension wiring, initialized and ready to use |
| `delete_op` | `op_path`, `override?` | Delete an operator. Also purges its externalization tracking (any strategy) and the externalized file — unless the file is clone-owned or still referenced by another operator. Refused while another live session claims the scope or wrote it in the last minute; `override=True` bypasses |
| `copy_op` | `source_path`, `dest_parent`, `new_name?` | Copy operator to new location |
| `rename_op` | `op_path`, `new_name` | Rename an operator |
| `get_op` | `op_path`, `include_defaults?` | Get operator info. Parameters are NON-DEFAULT only by default; pass `include_defaults=True` for all parameters. Parameter-heavy COMPs are expensive in full detail, so prefer `read_tdn` for structure reads |
| `query_network` | `parent_path?`, `recursive?`, `op_type?`, `include_utility?` | List operators in a container. Child rows are compact: `path`, `type`, `family`, `depth` (`name` is derivable from the last path segment). Set `include_utility=True` to include annotations |
| `find_children` | `op_path`, `name?`, `type?`, `depth?`, `tags?`, `text?`, `comment?`, `include_utility?` | Advanced search using TD's `findChildren` — filter by name pattern, type, depth, tags, text content, or comment |
| `cook_op` | `op_path`, `force?`, `recurse?` | Force-cook an operator |

## Parameter Control

| Tool | Parameters | Description |
|------|-----------|-------------|
| `set_parameter` | `op_path`, `par_name`, `value?`, `mode?`, `expr?`, `bind_expr?` | Set a parameter's value, expression, bind expression, or mode (`constant`/`expression`/`export`/`bind`). Invalid Menu values are rejected with valid `menuNames`; sequence-block names auto-grow their sequence (`const5name` grows `numBlocks` to 6) |
| `get_parameter` | `op_path`, `par_name?`, `search?`, `search_in?`, `depth?`, `max_results?`, `details?` | Get one parameter compactly, or search parameters by glob/substring across a subtree. Search fields: `name`, `value`, `expr`, or `any` |

Search mode omits `par_name` and passes `search`. It scans the target operator and children to `depth` (default 2) using fnmatch glob semantics; patterns without `*?[` become contains searches. Results are `{root, pattern, search_in, count, results, truncated?}`, where each hit includes `op`, `par`, `value`, `mode`, and `expr` or `bindExpr` when present.

`search_in='value'` evaluates every parameter it scans (expressions included), so expression side effects and cooking cost are on the caller; `search_in='any'` only evaluates constant-mode values and matches expression/bind parameters by text.

Single-parameter mode returns `path`, `parameter`, `value`, `mode`, `label`, mode-specific refs (`expression`, `bindExpr`, `bindMaster`, `exportOP`), and `menuNames` for Menu parameters. Pass `details=True` to include defaults, custom/read-only/style metadata, numeric ranges, `menuLabels`, and `menuIndex`. Search mode ignores `details`.

**Example** -- find expressions under `/project1` that reference absolute paths in that subtree:

```json
{"op_path": "/project1", "search": "*/project1/*", "search_in": "expr", "depth": 10, "max_results": 100}
```

## DAT Content

| Tool | Parameters | Description |
|------|-----------|-------------|
| `get_dat_content` | `op_path`, `format?` | Get DAT text or table data (`"text"`, `"table"`, or `"auto"`) |
| `set_dat_content` | `op_path`, `text?`, `rows?`, `clear?`, `confirm_wipe?` | Full-replace DAT content. Wipe guardrail refuses `text=""`, `rows=[]`, or `clear=True` with no content unless `confirm_wipe=True` is passed. For partial edits to text DATs, prefer `edit_dat_content` -- it sends only the changed substring. |
| `edit_dat_content` | `op_path`, `old_string`, `new_string`, `replace_all?`, `confirm_wipe?` | Surgical text edit on a DAT (mirrors Claude Code's Edit tool). Replaces `old_string` with `new_string`. By default `old_string` must appear exactly once -- pass `replace_all=True` to replace every occurrence. Token-efficient: only the changed substring crosses the wire. Text DATs only; use `set_dat_content(rows=...)` for tables. |

## Operator Flags

| Tool | Parameters | Description |
|------|-----------|-------------|
| `get_op_flags` | `op_path` | Get all flags: bypass, lock, display, render, viewer, current, expose, selected, allowCooking |
| `set_op_flags` | `op_path`, `bypass?`, `lock?`, `display?`, `render?`, `viewer?`, `current?`, `expose?`, `allowCooking?`, `selected?` | Set one or more flags on an operator |

## Positioning & Layout

| Tool | Parameters | Description |
|------|-----------|-------------|
| `get_op_position` | `op_path` | Get operator position, size, color, and comment |
| `get_network_layout` | `comp_path`, `include_annotations?` | Get compact positions of ALL operators (and annotations) in a COMP in one call. Operators include `path`, `type`, `nodeX`, `nodeY`, `nodeWidth`, `nodeHeight`; centers are derivable as `nodeX+nodeWidth/2` and `nodeY+nodeHeight/2`. Annotation text is capped at 160 chars. Returns `bounding_box` |
| `set_op_position` | `op_path`, `x?`, `y?`, `width?`, `height?`, `color?`, `comment?` | Set operator position, size, color (`[r,g,b]` floats 0-1), or comment |
| `layout_children` | `op_path` | Auto-layout all children in a COMP |

## Annotations

| Tool | Parameters | Description |
|------|-----------|-------------|
| `create_annotation` | `parent_path`, `mode?`, `text?`, `title?`, `x?`, `y?`, `width?`, `height?`, `color?`, `opacity?`, `name?` | Create an annotation. Modes: `"annotate"` (default, has title bar), `"comment"`, `"networkbox"`. Created `utility=True` (matching TD UI-drawn annotations): visible to `get_annotations`, hidden from `query_network`/`find_children` unless `include_utility=True`. Every op-path tool still resolves it by path; delete with `delete_op` (durable), never a raw `.destroy()` |
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
| `execute_python` | `code` | Execute Python in TD; set the `result` variable to return values. Auto-lints newly-created ops and emits a **LAYOUT WARNING** when they are left at (0,0) or overlapping (unlike `create_op`, raw `comp.create()` does not auto-position) |

## Introspection & Diagnostics

| Tool | Parameters | Description |
|------|-----------|-------------|
| `get_td_info` | _(none)_ | Get TD version, build, OS, and Envoy version |
| `get_op_errors` | `op_path`, `recurse?` | Get error and warning messages for an operator and its children |
| `exec_op_method` | `op_path`, `method`, `args?`, `kwargs?` | Call a method on an operator (e.g., `appendRow`, `cook`) |
| `get_td_classes` | _(none)_ | List all Python classes/modules in the `td` module |
| `get_td_class_details` | `class_name` | Get methods, properties, and docs for a TD class |
| `get_module_help` | `module_name` | Get Python help text for a module (supports dotted names like `td.tdu`) |
| `get_docs` | `query`, `section?`, `source?`, `max_chars?` | Look up official TouchDesigner docs. `source` is `auto` (offline then web), `offline`, or `web`; normal responses carry `title`, `source`, `sections_available`, `content`, and optional `url`/`truncated`; ambiguous offline lookups return `source` + `matches` only |

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
| `read_tdn` | `comp_path?`, `include_dat_content?`, `max_depth?`, `embed_all?` | **Preferred for reading ≥3 operators.** Return the live network as a TDN dict (in-memory, never written to disk). ~20-90× fewer tokens than a `get_op` walk thanks to default-omission, `type_defaults`, and `par_templates` compaction |
| `export_network` | `root_path?`, `include_dat_content?`, `output_file?`, `max_depth?`, `embed_all?` | Write a `.tdn` file to disk. Same payload as `read_tdn` plus file I/O and stale-file cleanup. Set `embed_all=True` to recurse into TDN-tagged COMPs instead of skipping their children (self-contained export) |
| `import_network` | `target_path`, `tdn`, `clear_first?`, `override?` | Recreate a network from a `.tdn` file. With `clear_first=True`, gated against live peer sessions like `delete_op` |
| `diff_tdn` | `target?`, `max_changed_ops?`, `max_bytes?` | **What is UNSAVED in TDN networks** -- the live in-memory network vs the on-disk `.tdn`, the view git cannot give. Omit `target` for a whole-project summary (every live TDN COMP, which changed + counts); pass a COMP path OR a `.tdn` file path/bare filename for one COMP in full per-field detail (`old`=disk, `new`=live). For committed/history diffs use plain `git diff` -- Embody installs a `.tdn` diff driver that keeps those clean. Read-only, non-interactive |

## TOP Capture

| Tool | Parameters | Description |
|------|-----------|-------------|
| `capture_top` | `op_path`, `format?`, `quality?`, `max_resolution?`, `inline?`, `sample_grid?` | Capture a TOP's output as an image. Saves to a temp file and returns the path -- Read that path to view it. Inline base64 previews are token-heavy, so they are **off by default** (`inline=False`); pass `inline=True` to also embed a small preview. Small images (<20 KB) include the inline MCP `ImageContent` preview when requested. Default: JPEG at 80% quality, max 640px long edge. Pass `sample_grid>=2` to return a downsampled NxN RGBA grid instead of an image: row 0 is the top of the image, stats are computed over the full-resolution texture, the requested grid clamps to 2..32, the returned `grid` is further capped to the TOP's width/height and can drop below 2 on tiny textures, and image params are ignored. Channel padding: RG -> b=0/a=1, mono -> replicated/a=1, monoalpha -> replicated + real alpha; `channels` reports the raw plane count. Every capture also returns a **Quality verdict** from the raw float pixels (`is_black` / `is_flat` / `fully_transparent` / `pass` + `fail_reasons`), surfaced as a `Quality: OK\|FAIL` line so you can tell an empty/black/transparent render from a real one without reading the image (black and fully-transparent are failures; a uniform fill is advisory, `flat_frame`). |

## Multi-Session Awareness

Concurrent AI sessions (multiple Claude Code windows, other MCP clients) working on the same project are tracked, warned about each other, and gated away from destroying each other's work. See [Multi-Session Coordination](multi-session.md) for the full picture.

| Tool | Parameters | Description |
|------|-----------|-------------|
| `get_sessions` | — | List connected AI sessions: label (`repo@branch`), idle time, `recent_scopes` it modified, `claims` it holds, plus `you` (the caller's own session id) |
| `claim_scope` | `scope`, `note?`, `ttl?` | Cooperative write lease on an op-path prefix, a `file:` path, or a `project:` scope. Peers' overlapping claims are refused while yours is live; their destructive operations on it are gated. Auto-renews on your own writes; expires on TTL or session silence |
| `release_scope` | `scope` | Release a lease you hold. Polite — expiry also handles it |

!!! info "Auto-piggybacked peer advisories"
    A `_peers` field rides on any response whose request touches territory another session modified in the last ~10 minutes — one entry per peer: `{label, scope, tool, age_s, conflict}`. `conflict: true` means a peer *wrote* an overlapping scope within the last minute and your operation is also a write — stop and coordinate.

!!! warning "Destructive-operation gate"
    `delete_op`, `import_network` with `clear_first=True`, `run_tests`, and batches containing them are refused with a `MULTI-SESSION GATE` error (naming the holder or recent writer) while a live peer session claims the scope or wrote it within the last minute. Pass `override=True` only when you are certain.

## Logging

| Tool | Parameters | Description |
|------|-----------|-------------|
| `get_logs` | `level?`, `count?`, `since_id?`, `source?` | Get recent log entries from ring buffer. Filter by level, source, or use `since_id` for incremental polling |
| `run_tests` | `suite_name?`, `test_name?`, `override?` | Run test suites and return results. Gated while a peer session holds `project:tests` |

!!! info "Auto-piggybacked logs"
    When a tool call generates `WARNING` or `ERROR` entries since the previous call, the response carries a `_logs` field with up to the last 8 of them. `INFO`/`DEBUG`/`SUCCESS` history does not ride along — fetch it on demand with `get_logs`. Warning cursors are tracked per session, so concurrent AI sessions each receive their own copy — one session polling first no longer consumes a warning meant for everyone.

!!! info "Auto-attached recovery hints"
    When a tool returns an `error`, Envoy attaches a `recovery_hints` list — each entry `{cause, action, next_tools}`, matched to the real error string (path-not-found -> `query_network`/`find_children`, parameter-not-found -> `get_op`, wrong family, empty capture -> `get_op_performance`, thread conflict, timeout -> `get_project_performance`). Additive, never clobbers, never raises — follow the hint instead of retrying the same failing call.

## Bridge Meta-Tools

These tools run locally on the STDIO bridge script, not inside TouchDesigner. They work even when TD is not running — this is how Claude Code can launch or restart TD without an active Envoy connection.

| Tool | Parameters | Description |
|------|-----------|-------------|
| `get_td_status` | _(none)_ | Check if TD is running, Envoy reachable, crash detection, process liveness, restart attempts remaining |
| `launch_td` | `timeout?`, `project_path?` | Launch TD with the project's `.toe` file. Waits for Envoy to become reachable (default: 120s). Pass `project_path` (absolute, or relative to the git root) to open a different `.toe` |
| `restart_td` | `timeout?`, `project_path?` | Gracefully quit TD and relaunch. Waits for exit before relaunching (default: 120s). Pass `project_path` to relaunch with a different `.toe`. Targets only the active instance's verified process — other running TouchDesigner instances are never touched |
| `switch_instance` | `instance?` | List all registered TD instances (omit `instance`) or switch to a different running instance. See [Multiple Instances](architecture.md#multiple-instances) |

!!! info "Bridge architecture"
    Claude Code connects to Envoy via a STDIO bridge script (`.embody/envoy-bridge.py`). The bridge translates between Claude Code's STDIO transport and Envoy's HTTP endpoint. It handles MCP protocol handshake locally when TD is down, so these meta-tools are always available. See [Architecture](architecture.md) for details.

## Batch Operations

| Tool | Parameters | Description |
|------|-----------|-------------|
| `batch_operations` | `operations` | Execute multiple operations in a single request. Reduces latency and token overhead |

`operations` is a list of `{"tool": str, "params": dict}` objects. Each entry maps to an existing tool name and its parameters. Stops on first error.

**When to use**: 3+ calls to the same tool type (positioning, connecting, parameter setting, flags). Use `execute_python` instead when you need conditionals, loops, or computed values between operations.

**Example** — position 4 operators + connect them in one call:
```json
{"operations": [
  {"tool": "set_op_position", "params": {"op_path": "/project1/noise1", "x": 400, "y": 0}},
  {"tool": "set_op_position", "params": {"op_path": "/project1/comp1", "x": 800, "y": 0}},
  {"tool": "set_op_position", "params": {"op_path": "/project1/level1", "x": 1200, "y": 0}},
  {"tool": "set_op_position", "params": {"op_path": "/project1/null1", "x": 1600, "y": 0}},
  {"tool": "connect_ops", "params": {"source_path": "/project1/noise1", "dest_path": "/project1/comp1"}},
  {"tool": "connect_ops", "params": {"source_path": "/project1/comp1", "dest_path": "/project1/level1"}},
  {"tool": "connect_ops", "params": {"source_path": "/project1/level1", "dest_path": "/project1/null1"}}
]}
```

## MCP Prompts

| Prompt | Parameters | Description |
|--------|-----------|-------------|
| `search_op` | `op_name`, `op_type?` | Guide for searching operators by name |
| `check_op_errors` | `op_path` | Guide for inspecting and resolving operator errors |
| `connect_ops` | _(none)_ | Guide for wiring operators together |
| `create_extension_guide` | _(none)_ | Guide for creating TD extensions with proper patterns |
