"""
Envoy - MCP Server for TouchDesigner

Enables AI coding assistants to interact with TouchDesigner via the Model Context Protocol.
Supports creating, destroying, editing operators and their parameters.

Architecture:
- MCP server runs in a worker thread (via Thread Manager)
- TD operations execute on main thread (via OnRefresh callback)
- Bidirectional queues handle request/response communication

Usage:
1. Embody auto-installs dependencies via uv on init (see EmbodyExt._setupEnvironment)
2. Enable Envoy via the Envoyenable parameter
3. Connect AI assistant: Envoy auto-creates .mcp.json in the project root on startup
"""

from __future__ import annotations

from typing import Optional, Any, Callable
from queue import Queue, Empty
from threading import Lock, Event, Thread
import json
import subprocess
import sys
import tempfile
import time
import asyncio

ENVOY_VERSION = "1.4.0"

class EnvoyMCPServer:
    """
    MCP Server that runs in a worker thread.

    IMPORTANT: This class must NOT import or use any TouchDesigner modules.
    All TD operations are delegated to the main thread via queues.
    """

    def __init__(self, request_queue: Optional[Queue], response_queue: Queue,
                 add_to_refresh_queue: Callable[[dict], None], port: int = 9870,
                 shutdown_event: Optional[Event] = None) -> None:
        self.request_queue: Optional[Queue] = request_queue
        self.response_queue: Queue = response_queue
        self.add_to_refresh_queue: Callable[[dict], None] = add_to_refresh_queue
        self.port: int = port
        self.shutdown_event: Event = shutdown_event or Event()
        self.pending_requests: dict[int, dict] = {}
        self.request_counter: int = 0
        self.lock: Lock = Lock()
        self.running: bool = True

        # Import mcp only when server is instantiated (in worker thread)
        from mcp.server.fastmcp import FastMCP, Image
        self._Image = Image  # Store for use in tool functions
        self.mcp = FastMCP("Envoy", host="127.0.0.1", port=port, stateless_http=True)
        self._register_tools()

    def _execute_in_td(self, operation: str, params: dict,
                       timeout: float = 30.0) -> dict:
        """Queue operation to main thread and wait for response"""
        with self.lock:
            request_id = self.request_counter
            self.request_counter += 1
            event = Event()
            self.pending_requests[request_id] = {'event': event, 'result': None}

        # Queue request to main thread via Thread Manager's refresh queue
        self.add_to_refresh_queue({
            'id': request_id,
            'operation': operation,
            'params': params
        })

        # Wait for response (with timeout)
        if not event.wait(timeout=timeout):
            with self.lock:
                del self.pending_requests[request_id]
            return {'error': f'Operation timed out after {timeout} seconds. '
                    f'The operation may still execute on the main thread.'}

        with self.lock:
            result = self.pending_requests[request_id].get('result', {'error': 'No result'})
            del self.pending_requests[request_id]
        return result

    def check_responses(self) -> None:
        """Check for responses from main thread"""
        while True:
            try:
                response = self.response_queue.get_nowait()
            except Exception as e:
                # queue.Empty is expected (no more responses). After module
                # recompilation the `Empty` name may no longer resolve, so
                # fall back to checking the class name as a string.
                try:
                    expected = isinstance(e, Empty)
                except NameError:
                    expected = type(e).__name__ == 'Empty'
                if not expected:
                    print(f'[Envoy][WARNING] check_responses unexpected error: {type(e).__name__}: {e}')
                break
            request_id = response['id']
            with self.lock:
                pending = self.pending_requests.get(request_id)
            if pending is not None:
                pending['result'] = response['result']
                pending['event'].set()
            else:
                # Orphaned response — request already timed out and was removed
                print(f'[Envoy][WARNING] Orphaned response for request {request_id} '
                      f'(likely timed out). Operation still executed on main thread.')

    def _register_tools(self):
        """Register all MCP tools"""

        @self.mcp.tool()
        def create_op(parent_path: str, op_type: str, name: str = None) -> dict:
            """
            Create a new operator in TouchDesigner.

            Args:
                parent_path: Path to parent COMP (e.g., "/project1" or "/project1/base1")
                op_type: Operator type (e.g., "baseCOMP", "noiseTOP", "waveCHOP", "textDAT")
                name: Optional name for the new operator

            Returns:
                Dict with path, name, and type of created operator
            """
            return self._execute_in_td('create_op', {
                'parent_path': parent_path,
                'op_type': op_type,
                'name': name
            })

        @self.mcp.tool()
        def delete_op(op_path: str) -> dict:
            """
            Delete an operator.

            Args:
                op_path: Full path to the operator (e.g., "/project1/base1")

            Returns:
                Dict with success status
            """
            return self._execute_in_td('delete_op', {'op_path': op_path})

        @self.mcp.tool()
        def get_op(op_path: str) -> dict:
            """
            Get detailed information about an operator.

            Args:
                op_path: Full path to the operator

            Returns:
                Dict with operator info including type, family, parameters, inputs, outputs
            """
            return self._execute_in_td('get_op', {'op_path': op_path})

        @self.mcp.tool()
        def set_parameter(op_path: str, par_name: str, value: str = None,
                         mode: str = None, expr: str = None,
                         bind_expr: str = None) -> dict:
            """
            Set a parameter value, expression, bind expression, or mode on an operator.

            Args:
                op_path: Full path to the operator
                par_name: Parameter name (e.g., "tx", "frequency", "file")
                value: Constant value to set (used when mode is CONSTANT or unspecified)
                mode: Parameter mode - "constant", "expression", "export", or "bind"
                expr: Python expression string (sets mode to EXPRESSION automatically)
                bind_expr: Bind expression string (sets mode to BIND automatically)

            Returns:
                Dict with success status and new value
            """
            return self._execute_in_td('set_parameter', {
                'op_path': op_path,
                'par_name': par_name,
                'value': value,
                'mode': mode,
                'expr': expr,
                'bind_expr': bind_expr
            })

        @self.mcp.tool()
        def get_parameter(op_path: str, par_name: str) -> dict:
            """
            Get a parameter value from an operator.

            Args:
                op_path: Full path to the operator
                par_name: Parameter name

            Returns:
                Dict with parameter value, mode, expression, bind expression,
                export source, label, min/max, and default value
            """
            return self._execute_in_td('get_parameter', {
                'op_path': op_path,
                'par_name': par_name
            })

        @self.mcp.tool()
        def connect_ops(source_path: str, dest_path: str,
                             source_index: int = 0, dest_index: int = 0,
                             comp: bool = False) -> dict:
            """
            Connect two operators with a wire.

            Args:
                source_path: Path to source operator (output)
                dest_path: Path to destination operator (input)
                source_index: Output connector index (default 0)
                dest_index: Input connector index (default 0)
                comp: If True, use COMP connectors (top/bottom) instead of operator connectors (left/right)

            Returns:
                Dict with success status
            """
            return self._execute_in_td('connect_ops', {
                'source_path': source_path,
                'dest_path': dest_path,
                'source_index': source_index,
                'dest_index': dest_index,
                'comp': comp
            })

        @self.mcp.tool()
        def disconnect_op(op_path: str, input_index: int = 0,
                                comp: bool = False) -> dict:
            """
            Disconnect an operator's input.

            Args:
                op_path: Path to the operator
                input_index: Input connector index to disconnect (default 0)
                comp: If True, disconnect a COMP connector (top/bottom) instead of operator connector (left/right)

            Returns:
                Dict with success status
            """
            return self._execute_in_td('disconnect_op', {
                'op_path': op_path,
                'input_index': input_index,
                'comp': comp
            })

        @self.mcp.tool()
        def query_network(parent_path: str = "/", recursive: bool = False,
                         op_type: str = None,
                         include_utility: bool = False) -> dict:
            """
            List all operators in a network/container.

            Args:
                parent_path: Path to parent COMP to search in (default "/")
                recursive: If True, search recursively into child COMPs
                op_type: Filter by operator type (e.g., "baseCOMP", "TOP", "annotateCOMP")
                include_utility: If True, include utility operators like annotations (default False)

            Returns:
                Dict with list of operators and their basic info
            """
            return self._execute_in_td('query_network', {
                'parent_path': parent_path,
                'recursive': recursive,
                'op_type': op_type,
                'include_utility': include_utility,
            })

        @self.mcp.tool()
        def copy_op(source_path: str, dest_parent: str, new_name: str = None) -> dict:
            """
            Copy an operator to a new location.

            Args:
                source_path: Path to operator to copy
                dest_parent: Path to destination parent COMP
                new_name: Optional new name for the copy

            Returns:
                Dict with path to new operator
            """
            return self._execute_in_td('copy_op', {
                'source_path': source_path,
                'dest_parent': dest_parent,
                'new_name': new_name
            })

        @self.mcp.tool()
        def get_connections(op_path: str) -> dict:
            """
            Get all input and output connections for an operator.

            Args:
                op_path: Path to the operator

            Returns:
                Dict with inputs and outputs lists
            """
            return self._execute_in_td('get_connections', {'op_path': op_path})

        @self.mcp.tool()
        def execute_python(code: str) -> dict:
            """
            Execute arbitrary Python code in TouchDesigner.
            Use with caution - code runs on main thread with full TD access.

            Args:
                code: Python code to execute

            Returns:
                Dict with execution result or error
            """
            return self._execute_in_td('execute_python', {'code': code})

        # === Introspection & Diagnostics Tools ===

        @self.mcp.tool()
        def get_td_info() -> dict:
            """
            Get information about the TouchDesigner environment and Envoy server.

            Returns:
                Dict with TD version, build, OS info, and Envoy/Embody versions
            """
            return self._execute_in_td('get_td_info', {})

        @self.mcp.tool()
        def get_op_errors(op_path: str, recurse: bool = True) -> dict:
            """
            Get error and warning messages for an operator and optionally its children.
            Useful for debugging TD networks — returns both errors and warnings.

            Args:
                op_path: Path to the operator to check
                recurse: If True, also check children (default True)

            Returns:
                Dict with structured error and warning lists
            """
            return self._execute_in_td('get_op_errors', {
                'op_path': op_path,
                'recurse': recurse
            })

        @self.mcp.tool()
        def exec_op_method(op_path: str, method: str,
                            args: list = None, kwargs: dict = None) -> dict:
            """
            Call a method on a TouchDesigner operator.
            Example: exec_op_method("/project1/table1", "appendRow", args=[["a", "b", "c"]])

            Args:
                op_path: Path to the operator
                method: Method name to call (e.g., "appendRow", "clear", "cook")
                args: Positional arguments as a list (default [])
                kwargs: Keyword arguments as a dict (default {})

            Returns:
                Dict with method result
            """
            return self._execute_in_td('exec_op_method', {
                'op_path': op_path,
                'method': method,
                'args': args or [],
                'kwargs': kwargs or {}
            })

        @self.mcp.tool()
        def get_td_classes() -> dict:
            """
            List all Python classes and modules available in the TouchDesigner td module.
            Useful for discovering TD's Python API.

            Returns:
                Dict with list of class names and descriptions
            """
            return self._execute_in_td('get_td_classes', {})

        @self.mcp.tool()
        def get_td_class_details(class_name: str) -> dict:
            """
            Get detailed information about a specific TouchDesigner Python class.
            Shows methods, properties, and descriptions.

            Args:
                class_name: Name of the class in the td module (e.g., "OP", "COMP", "Par")

            Returns:
                Dict with class methods, properties, and descriptions
            """
            return self._execute_in_td('get_td_class_details', {
                'class_name': class_name
            })

        @self.mcp.tool()
        def get_module_help(module_name: str) -> dict:
            """
            Get Python help text for a TouchDesigner module or class.
            Supports dotted names like "td.tdu" or simple names like "OP".

            Args:
                module_name: Module or class name (e.g., "td", "td.tdu", "OP", "Par")

            Returns:
                Dict with module name and help text
            """
            return self._execute_in_td('get_module_help', {
                'module_name': module_name
            })

        # === MCP Prompts ===

        @self.mcp.prompt()
        def search_op(op_name: str, op_type: str = None) -> str:
            """Search for an operator by name in the TouchDesigner project."""
            msg = f'Use the "query_network" and "get_op" tools to search for operators named "{op_name}" in the TouchDesigner project.'
            if op_type:
                msg += f' Filter by type: {op_type}.'
            return msg

        @self.mcp.prompt()
        def check_op_errors(op_path: str) -> str:
            """Check an operator and its children for errors and warnings in TouchDesigner."""
            return f'Use the "get_op_errors" tool to inspect "{op_path}" and its children for error and warning messages. If errors or warnings are found, examine the affected operators\' parameters and connections to resolve them.'

        @self.mcp.prompt()
        def connect_ops() -> str:
            """Guide for connecting operators in TouchDesigner."""
            return 'Use the "connect_ops" tool to wire operators together. First use "query_network" to find the operators, then "get_connections" to see existing wiring, then "connect_ops" with the source and destination paths.'

        @self.mcp.prompt()
        def create_extension_guide() -> str:
            """Guide for creating TouchDesigner extensions with proper patterns."""
            return (
                'To create a TouchDesigner extension:\n\n'
                '1. Use the "create_extension" tool with a class_name and parent_path.\n'
                '   - Set existing_comp=True to add an extension to an existing COMP.\n'
                '   - Provide custom code via the "code" parameter, or omit for boilerplate.\n\n'
                '2. Extension class conventions:\n'
                '   - __init__(self, ownerComp) is required\n'
                '   - Capitalized methods are promoted: op.CompName.Method()\n'
                '   - Lowercase methods need: op.CompName.ext.ClassName.method()\n'
                '   - Store the owner as self.ownerComp\n\n'
                '3. TD auto-reinitializes extensions when their source DATs change.\n'
                '   To force a reinit: exec_op_method on the COMP, method="initializeExtensions".\n'
                '   Implement onDestroyTD(self) for clean teardown of old instances.\n'
                '   Use onInitTD(self) for post-init setup needing a fully-cooked network.\n\n'
                '4. Common patterns:\n'
                '   - Child ops: self.ownerComp.op("childName")\n'
                '   - Parameters: self.ownerComp.par.paramName\n'
                '   - Deferred execution: run("code", delayFrames=1)\n\n'
                '5. The extension text DAT must be INSIDE the COMP it extends.'
            )

        # === DAT Content Tools ===

        @self.mcp.tool()
        def get_dat_content(op_path: str, format: str = "auto") -> dict:
            """
            Get the content of a DAT operator (text or table data).

            Args:
                op_path: Path to the DAT operator
                format: "text" for raw text, "table" for row/column data,
                       "auto" to detect based on DAT type

            Returns:
                Dict with DAT content (text string or table rows/cols)
            """
            return self._execute_in_td('get_dat_content', {
                'op_path': op_path,
                'format': format
            })

        @self.mcp.tool()
        def set_dat_content(op_path: str, text: str = None,
                           rows: list = None, clear: bool = False) -> dict:
            """
            Set the content of a DAT operator.

            Args:
                op_path: Path to the DAT operator
                text: Full text content to set (replaces entire DAT)
                rows: List of lists to set as table rows (replaces entire table)
                clear: If True, clear the DAT before setting content

            Returns:
                Dict with success status
            """
            return self._execute_in_td('set_dat_content', {
                'op_path': op_path,
                'text': text,
                'rows': rows,
                'clear': clear
            })

        # === Operator Flags Tools ===

        @self.mcp.tool()
        def get_op_flags(op_path: str) -> dict:
            """
            Get all flags/properties for an operator (bypass, lock, display, etc.).

            Args:
                op_path: Path to the operator

            Returns:
                Dict with all flag states
            """
            return self._execute_in_td('get_op_flags', {'op_path': op_path})

        @self.mcp.tool()
        def set_op_flags(op_path: str, bypass: bool = None, lock: bool = None,
                        display: bool = None, render: bool = None,
                        viewer: bool = None, current: bool = None,
                        expose: bool = None, allowCooking: bool = None,
                        selected: bool = None) -> dict:
            """
            Set flags/properties on an operator.

            Args:
                op_path: Path to the operator
                bypass: Bypass flag (operator is skipped in chain)
                lock: Lock flag (DAT contents locked from editing/cooking)
                display: Display flag (blue flag, marks output for display)
                render: Render flag (purple flag, marks for rendering)
                viewer: Viewer flag (shows viewer on node tile)
                current: Current flag (yellow flag)
                expose: Expose flag (hides node from network view when False)
                allowCooking: Allow cooking flag (COMPs only)
                selected: Selected flag in network editor

            Returns:
                Dict with success status and updated flags
            """
            return self._execute_in_td('set_op_flags', {
                'op_path': op_path,
                'bypass': bypass,
                'lock': lock,
                'display': display,
                'render': render,
                'viewer': viewer,
                'current': current,
                'expose': expose,
                'allowCooking': allowCooking,
                'selected': selected
            })

        # === Node Positioning & Layout Tools ===

        @self.mcp.tool()
        def get_op_position(op_path: str) -> dict:
            """
            Get an operator's position and size in the network editor.

            Args:
                op_path: Path to the operator

            Returns:
                Dict with nodeX, nodeY, nodeWidth, nodeHeight, color, comment
            """
            return self._execute_in_td('get_op_position', {'op_path': op_path})

        @self.mcp.tool()
        def get_network_layout(comp_path: str, include_annotations: bool = True) -> dict:
            """
            Get positions and sizes of all operators (and optionally annotations) in a COMP.
            Use this instead of calling get_op_position repeatedly for each operator.

            Args:
                comp_path: Path to the parent COMP
                include_annotations: Whether to include annotation positions (default True)

            Returns:
                Dict with operators list (path, name, type, nodeX, nodeY, nodeWidth, nodeHeight,
                nodeCenterX, nodeCenterY), annotations list, and bounding_box of all operators
            """
            return self._execute_in_td('get_network_layout', {
                'comp_path': comp_path,
                'include_annotations': include_annotations
            })

        @self.mcp.tool()
        def set_op_position(op_path: str, x: int = None, y: int = None,
                           width: int = None, height: int = None,
                           color: list = None, comment: str = None) -> dict:
            """
            Set an operator's position, size, color, or comment in the network editor.

            Args:
                op_path: Path to the operator
                x: X position (horizontal, from left)
                y: Y position (vertical, from bottom)
                width: Node tile width
                height: Node tile height
                color: RGB color as [r, g, b] floats (0.0-1.0)
                comment: Comment text annotation

            Returns:
                Dict with success status and new position
            """
            return self._execute_in_td('set_op_position', {
                'op_path': op_path,
                'x': x,
                'y': y,
                'width': width,
                'height': height,
                'color': color,
                'comment': comment
            })

        @self.mcp.tool()
        def layout_children(op_path: str) -> dict:
            """
            Auto-layout all children in a COMP using TouchDesigner's built-in layout.

            Args:
                op_path: Path to the parent COMP

            Returns:
                Dict with success status
            """
            return self._execute_in_td('layout_children', {'op_path': op_path})

        # === Annotation Tools ===

        @self.mcp.tool()
        def create_annotation(parent_path: str, mode: str = "annotate",
                              text: str = "", title: str = "",
                              x: int = None, y: int = None,
                              width: int = None, height: int = None,
                              color: list = None, opacity: float = None,
                              name: str = None) -> dict:
            """
            Create an annotation (Comment, Network Box, or Annotate) in the network editor.
            Annotations are visual documentation elements for grouping, labeling, or documenting operators.

            Args:
                parent_path: Path to parent COMP where the annotation will be created
                mode: Annotation mode - "annotate" (default, has title bar), "comment", or "networkbox"
                text: Body text content
                title: Title bar text (for Network Box and Annotate modes)
                x: X position in the network editor
                y: Y position in the network editor
                width: Width of the annotation
                height: Height of the annotation
                color: Background color as [r, g, b] floats (0.0-1.0)
                opacity: Opacity of the annotation (0.0-1.0)
                name: Optional name for the annotation operator

            Returns:
                Dict with path, name, mode, and position of created annotation
            """
            return self._execute_in_td('create_annotation', {
                'parent_path': parent_path,
                'mode': mode,
                'text': text,
                'title': title,
                'x': x,
                'y': y,
                'width': width,
                'height': height,
                'color': color,
                'opacity': opacity,
                'name': name,
            })

        @self.mcp.tool()
        def get_annotations(parent_path: str) -> dict:
            """
            List all annotations (Comments, Network Boxes, Annotates) in a COMP.

            Args:
                parent_path: Path to the COMP to search for annotations

            Returns:
                Dict with list of annotations and their properties including text, mode, position, and enclosed operators
            """
            return self._execute_in_td('get_annotations', {
                'parent_path': parent_path,
            })

        @self.mcp.tool()
        def set_annotation(op_path: str, text: str = None, title: str = None,
                           color: list = None, opacity: float = None,
                           width: int = None, height: int = None,
                           x: int = None, y: int = None) -> dict:
            """
            Modify properties of an existing annotation.

            Args:
                op_path: Path to the annotation operator
                text: New body text content
                title: New title bar text
                color: Background color as [r, g, b] floats (0.0-1.0)
                opacity: Opacity (0.0-1.0)
                width: New width
                height: New height
                x: New X position
                y: New Y position

            Returns:
                Dict with updated annotation properties
            """
            return self._execute_in_td('set_annotation', {
                'op_path': op_path,
                'text': text,
                'title': title,
                'color': color,
                'opacity': opacity,
                'width': width,
                'height': height,
                'x': x,
                'y': y,
            })

        @self.mcp.tool()
        def get_enclosed_ops(op_path: str) -> dict:
            """
            Get the relationship between an annotation and operators.
            If op_path is an annotation: returns the operators enclosed by it.
            If op_path is a regular operator: returns the annotations enclosing it.

            Args:
                op_path: Path to an annotation or regular operator

            Returns:
                Dict with enclosed_ops or enclosing_annotations depending on operator type
            """
            return self._execute_in_td('get_enclosed_ops', {
                'op_path': op_path,
            })

        # === Operator Management Tools (Extended) ===

        @self.mcp.tool()
        def rename_op(op_path: str, new_name: str) -> dict:
            """
            Rename an operator.

            Args:
                op_path: Full path to the operator
                new_name: New name for the operator

            Returns:
                Dict with success status and new path
            """
            return self._execute_in_td('rename_op', {
                'op_path': op_path,
                'new_name': new_name
            })

        @self.mcp.tool()
        def cook_op(op_path: str, force: bool = True,
                         recurse: bool = False) -> dict:
            """
            Cook (evaluate) an operator.

            Args:
                op_path: Path to the operator
                force: Force cook even if not dirty (default True)
                recurse: Recursively cook children (default False)

            Returns:
                Dict with success status
            """
            return self._execute_in_td('cook_op', {
                'op_path': op_path,
                'force': force,
                'recurse': recurse
            })

        @self.mcp.tool()
        def find_children(op_path: str, name: str = None, type: str = None,
                         depth: int = None, tags: list = None,
                         text: str = None, comment: str = None,
                         include_utility: bool = False) -> dict:
            """
            Search for operators inside a COMP using TouchDesigner's findChildren.
            Much more powerful than query_network for targeted searches.

            Args:
                op_path: Path to the parent COMP to search in
                name: Name pattern to match (e.g., "noise*", "*filter*")
                type: Operator type to filter (e.g., "baseCOMP", "textDAT", "noiseTOP", "annotateCOMP")
                depth: Exact depth to search at (1 = direct children only)
                tags: List of tags to match (operator must have all tags)
                text: Search DAT text content for this string
                comment: Search operator comments for this string
                include_utility: If True, include utility operators like annotations (default False)

            Returns:
                Dict with list of matching operators
            """
            return self._execute_in_td('find_children', {
                'op_path': op_path,
                'name': name,
                'type': type,
                'depth': depth,
                'tags': tags,
                'text': text,
                'comment': comment,
                'include_utility': include_utility,
            })

        @self.mcp.tool()
        def get_op_performance(op_path: str, include_children: bool = False) -> dict:
            """
            Get performance/profiling data for an operator.

            Args:
                op_path: Path to the operator
                include_children: Include aggregate children performance data

            Returns:
                Dict with CPU/GPU cook times, memory usage, cook counts
            """
            return self._execute_in_td('get_op_performance', {
                'op_path': op_path,
                'include_children': include_children
            })

        @self.mcp.tool()
        def get_project_performance(include_hotspots: int = 0) -> dict:
            """
            Get project-level performance metrics: FPS, frame time, GPU/CPU memory,
            dropped frames, active operators, and more.

            Uses a Perform CHOP for accurate real-time measurements. The first call
            creates the monitor operator (negligible overhead).

            Args:
                include_hotspots: Return the top N most expensive COMPs by cook time.
                    0 (default) skips hotspot analysis. Recommended: 5-10.

            Returns:
                Dict with timing (fps, frameTimeMs, cookRate), memory (gpuMemUsedMB,
                totalGpuMemMB, cpuMemUsedMB), frame health (droppedFrames, activeOps,
                totalOps), GPU info (gpuTemp), performance mode status, and optionally
                hotspots (ranked COMPs with cook times and memory).
            """
            return self._execute_in_td('get_project_performance', {
                'include_hotspots': include_hotspots
            })

        # === Embody Integration Tools ===

        @self.mcp.tool()
        def externalize_op(op_path: str, tag_type: str = None) -> dict:
            """
            Tag an operator for Embody externalization and write it to disk.

            Args:
                op_path: Path to the operator
                tag_type: Tag type - "tox" for COMPs, "py"/"txt"/"tsv"/"json" etc for DATs
                         If None, will auto-detect based on operator type

            Returns:
                Dict with success status and applied tag
            """
            return self._execute_in_td('externalize_op', {
                'op_path': op_path,
                'tag_type': tag_type
            })

        @self.mcp.tool()
        def remove_externalization_tag(op_path: str) -> dict:
            """
            Remove Embody externalization tag from an operator.

            Args:
                op_path: Path to the operator

            Returns:
                Dict with success status
            """
            return self._execute_in_td('remove_externalization_tag', {'op_path': op_path})

        @self.mcp.tool()
        def get_externalizations() -> dict:
            """
            Get list of all externalized operators tracked by Embody.

            Returns:
                Dict with list of externalized operators and their status
            """
            return self._execute_in_td('get_externalizations', {})

        @self.mcp.tool()
        def save_externalization(op_path: str) -> dict:
            """
            Force save an externalized operator.

            Args:
                op_path: Path to the externalized operator

            Returns:
                Dict with success status and file path
            """
            return self._execute_in_td('save_externalization', {'op_path': op_path})

        @self.mcp.tool()
        def get_externalization_status(op_path: str) -> dict:
            """
            Get externalization status for an operator (dirty state, build info).

            Args:
                op_path: Path to the operator

            Returns:
                Dict with dirty state, build number, timestamp, file path
            """
            return self._execute_in_td('get_externalization_status', {'op_path': op_path})

        # === Extension Creation ===

        @self.mcp.tool()
        def create_extension(parent_path: str, class_name: str,
                             name: str = None, code: str = None,
                             promote: bool = True, ext_name: str = None,
                             ext_index: int = None,
                             existing_comp: bool = False) -> dict:
            """
            Create a TouchDesigner extension: a baseCOMP with a text DAT
            containing a Python extension class, wired up and initialized.

            Args:
                parent_path: Where to create the new COMP, OR path to an
                             existing COMP if existing_comp=True
                class_name: Python class name (e.g., "MyExtension")
                name: COMP name (defaults to class_name). Ignored if existing_comp=True
                code: Full Python class code. If omitted, generates minimal boilerplate
                promote: Promote capitalized methods to COMP level (default True)
                ext_name: Custom extension name (defaults to class_name)
                ext_index: Extension slot 0-3 (auto-detects first empty if omitted)
                existing_comp: If True, parent_path IS the target COMP (no new COMP created)

            Returns:
                Dict with comp_path, dat_path, class_name, ext_index, success status
            """
            return self._execute_in_td('create_extension', {
                'parent_path': parent_path,
                'class_name': class_name,
                'name': name,
                'code': code,
                'promote': promote,
                'ext_name': ext_name,
                'ext_index': ext_index,
                'existing_comp': existing_comp,
            })

        # === TDN Network Format Tools ===

        @self.mcp.tool()
        def export_network(root_path: str = "/",
                          include_dat_content: bool = None,
                          output_file: str = None,
                          max_depth: int = None,
                          embed_all: bool = False) -> dict:
            """
            Export a TouchDesigner network to .tdn JSON format.
            Only non-default properties are included, keeping output minimal.

            Args:
                root_path: Root COMP to export from (default "/" for entire project)
                include_dat_content: Include DAT text/table content (default None = use Embeddatsintdns toggle)
                output_file: File path to write JSON. Use "auto" to generate name. None returns dict only.
                max_depth: Maximum recursion depth (None = unlimited)
                embed_all: If True, recurse into TDN-tagged COMPs instead of
                    skipping their children. Produces a self-contained export.

            Returns:
                Dict with the .tdn JSON document and optional file path
            """
            return self._execute_in_td('export_network', {
                'root_path': root_path,
                'include_dat_content': include_dat_content,
                'output_file': output_file,
                'max_depth': max_depth,
                'embed_all': embed_all,
            })

        @self.mcp.tool()
        def import_network(target_path: str, tdn: dict,
                          clear_first: bool = False) -> dict:
            """
            Import a .tdn network into a TouchDesigner COMP, recreating all operators.

            Args:
                target_path: Destination COMP path to import into
                tdn: The .tdn JSON document (full document or just the operators array)
                clear_first: If True, delete all existing children before importing

            Returns:
                Dict with import results and created operator paths
            """
            return self._execute_in_td('import_network', {
                'target_path': target_path,
                'tdn': tdn,
                'clear_first': clear_first,
            })

        # === TOP Capture ===

        @self.mcp.tool()
        def capture_top(op_path: str, format: str = "jpeg", quality: float = 0.8,
                        max_resolution: int = 640) -> list:
            """
            Capture a TOP operator's output as an image.

            Returns the image saved to a temp file (path included in response).
            Small images are also returned inline as MCP ImageContent for direct viewing.

            Args:
                op_path: Path to a TOP operator (e.g., "/project1/null1")
                format: Image format - "jpeg" (smaller, lossy) or "png" (lossless)
                quality: JPEG compression quality 0.0-1.0 (ignored for PNG)
                max_resolution: Max pixels on longest edge. 0 = native resolution.

            Returns:
                List with text metadata and optional inline image preview
            """
            import base64
            import os
            import uuid

            result = self._execute_in_td('capture_top', {
                'op_path': op_path,
                'format': format,
                'quality': quality,
                'max_resolution': max_resolution,
            })

            if 'error' in result:
                return result

            # Decode the base64 image data from the main thread
            image_bytes = base64.b64decode(result['image_b64'])

            # Always save to temp file (Claude Code can Read images natively)
            ext = '.jpg' if result['format'] == 'jpeg' else f".{result['format']}"
            file_path = os.path.join(tempfile.gettempdir(), f'envoy_capture_{uuid.uuid4().hex[:8]}{ext}')
            with open(file_path, 'wb') as f:
                f.write(image_bytes)

            size_kb = result['size_bytes'] / 1024
            info = (f"TOP capture: {result['original_width']}x{result['original_height']}"
                    f" → {result['width']}x{result['height']} {result['format'].upper()}"
                    f" ({size_kb:.1f} KB)\nSaved to: {file_path}")

            # Include inline ImageContent for small images (within Claude Code token limits)
            if result['size_bytes'] < 20000:
                return [info, self._Image(data=image_bytes, format=result['format'])]
            else:
                return info + "\n(Use Read tool on the file path above to view the image)"

        # === Logging ===

        @self.mcp.tool()
        def get_logs(level: str = None, count: int = 50, since_id: int = None,
                     source: str = None) -> dict:
            """
            Get recent log entries from Embody's ring buffer.
            Useful for debugging operations or understanding what happened.

            Args:
                level: Filter by log level ("INFO", "WARNING", "ERROR", "SUCCESS", "DEBUG")
                count: Maximum number of entries to return (default 50, max 200)
                since_id: Only return entries with id > since_id (for polling new logs)
                source: Filter by source/caller pattern (substring match)

            Returns:
                Dict with log entries and metadata
            """
            return self._execute_in_td('get_logs', {
                'level': level,
                'count': count,
                'since_id': since_id,
                'source': source,
            })

        @self.mcp.tool()
        def run_tests(suite_name: str = None, test_name: str = None) -> dict:
            """
            Run Embody test suites and return results.

            Args:
                suite_name: Run only this suite (e.g., "test_path_utils"). Omit to run all.
                test_name: Run only this test method within the suite.

            Returns:
                Dict with passed/failed/error/skip counts and full results list
            """
            # Use a dedicated Event so the worker thread can wait directly
            # for test completion — bypasses the response_queue which is
            # fragile against server restarts / extension reinit.
            test_event = Event()
            test_holder: dict = {}
            sys._envoy_pending_test = {
                'event': test_event,
                'holder': test_holder,
            }

            # Queue the start request (main thread will run deferred tests)
            self.add_to_refresh_queue({
                'id': -1,  # Sentinel — no normal response expected
                'operation': 'run_tests',
                'params': {'suite_name': suite_name, 'test_name': test_name},
            })

            # Block worker thread until tests finish, timeout, or shutdown.
            # Poll every 1s so shutdown_event can interrupt promptly.
            deadline = time.time() + 300.0
            while not self.shutdown_event.is_set():
                remaining = deadline - time.time()
                if remaining <= 0:
                    sys._envoy_pending_test = None
                    return {'error': 'Tests timed out after 300 seconds'}
                if test_event.wait(timeout=min(remaining, 1.0)):
                    break  # Tests finished
            else:
                # Server shutting down — unblock cleanly
                sys._envoy_pending_test = None
                return {'error': 'Server shutting down during test run'}

            result = test_holder.get('result', {'error': 'No result'})
            sys._envoy_pending_test = None
            return result

    def run(self) -> None:
        """Run the MCP server (blocking) with graceful shutdown support"""
        import logging
        import uvicorn

        # Silence noisy per-request "Terminating session: None" logs from
        # stateless-mode MCP transport (one per HTTP request, purely cosmetic)
        logging.getLogger("mcp.server.streamable_http").setLevel(logging.WARNING)
        logging.getLogger("mcp.server.streamable_http_manager").setLevel(logging.WARNING)

        # Suppress "Stateless session crashed" noise from MCP SDK race condition:
        # In stateless mode, terminate() closes streams while background tasks may
        # still try to send_log_message → ClosedResourceError. This is cosmetic —
        # the server recovers immediately. Filter these out instead of escalating
        # the log level (which would hide real errors).
        import anyio

        class _DisconnectCrashFilter(logging.Filter):
            def filter(self, record):
                if record.exc_info and record.exc_info[1]:
                    exc = record.exc_info[1]
                    if self._is_disconnect(exc):
                        return False
                return True

            @staticmethod
            def _is_disconnect(exc):
                if isinstance(exc, (anyio.BrokenResourceError,
                                    anyio.ClosedResourceError)):
                    return True
                if isinstance(exc, BaseExceptionGroup):
                    return all(_DisconnectCrashFilter._is_disconnect(e)
                              for e in exc.exceptions)
                return False

        logging.getLogger("mcp.server.streamable_http_manager").addFilter(
            _DisconnectCrashFilter()
        )

        # Response checker is pure Python (no TD objects), so a plain thread is fine
        def response_checker():
            import time  # local import survives DAT recompilation
            while self.running and not self.shutdown_event.is_set():
                try:
                    self.check_responses()
                    time.sleep(0.01)
                except Exception as e:
                    if not self.shutdown_event.is_set():
                        print(f'[Envoy][WARNING] response_checker exiting: {e}')
                    break

        Thread(target=response_checker, daemon=True).start()

        # Manage uvicorn directly so we can signal shutdown via shutdown_event
        starlette_app = self.mcp.streamable_http_app()

        # Wrap the ASGI app to suppress BrokenResourceError from client disconnects.
        # When a VS Code tab closes while the MCP server is still sending responses,
        # anyio raises BrokenResourceError which propagates as nested ExceptionGroups.
        # The server recovers fine (new clients connect immediately), so suppress the noise.
        def _is_client_disconnect(exc):
            if isinstance(exc, (anyio.BrokenResourceError, anyio.ClosedResourceError)):
                return True
            if isinstance(exc, BaseExceptionGroup):
                return all(_is_client_disconnect(e) for e in exc.exceptions)
            return False

        class _SuppressDisconnect:
            def __init__(self, app):
                self.app = app
            async def __call__(self, scope, receive, send):
                try:
                    await self.app(scope, receive, send)
                except BaseException as exc:
                    if _is_client_disconnect(exc):
                        return
                    raise

        starlette_app = _SuppressDisconnect(starlette_app)

        config = uvicorn.Config(
            starlette_app,
            host="127.0.0.1",
            port=self.port,
            log_level="warning",
        )
        uvi_server = uvicorn.Server(config)

        # Store on sys so EnvoyExt.Start() can force-close sockets
        # if the old server thread is stuck and won't release the port.
        sys._envoy_uvi_server = uvi_server

        # Monitor shutdown_event and tell uvicorn to exit
        def shutdown_monitor():
            self.shutdown_event.wait()
            uvi_server.should_exit = True

        Thread(target=shutdown_monitor, daemon=True).start()

        try:
            # On Windows, use SelectorEventLoop instead of the default ProactorEventLoop.
            # The IOCP proactor can permanently kill the listener socket on server restarts
            # with "WinError 64: The specified network name is no longer available" during
            # accept(). SelectorEventLoop handles TCP reliably without IOCP quirks.
            if sys.platform.startswith('win'):
                asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
            asyncio.run(uvi_server.serve())
        finally:
            self.running = False
            if sys.platform.startswith('win'):
                asyncio.set_event_loop_policy(None)


# ============================================================
# MAIN THREAD CODE (TouchDesigner Extension)
# ============================================================

class EnvoyExt:
    """
    Envoy - MCP Server Extension for TouchDesigner

    Enables AI coding assistants to create, modify, and connect operators
    via the Model Context Protocol.

    This extension manages:
    - MCP server lifecycle (start/stop via op.TDResources.ThreadManager)
    - Request processing on main thread
    - TouchDesigner operation execution
    """

    def __init__(self, ownerComp: 'COMP') -> None:
        self.ownerComp: COMP = ownerComp
        self.request_queue: Queue = Queue()   # Worker -> Main thread
        self.response_queue: Queue = Queue()  # Main -> Worker thread
        self.current_task: Optional[Any] = None
        self._server_gen: int = 0  # Generation counter for stale callback detection
        self._last_served_log_id: int = 0

        # Get Thread Manager from TDResources
        self.ThreadManager = op.TDResources.ThreadManager

        # Shut down any server left over from a previous init cycle.
        # Extensions get re-initialized when TD recompiles externalized code
        # during project load, so __init__ can run multiple times.
        # The Event is stored on sys because:
        #   - .store() gets pickled on .toe save (Event has a Lock, not picklable)
        #   - COMP attributes aren't supported on td.containerCOMP
        #   - Module-level vars reset on recompile
        #   - sys attributes persist across recompiles and are never pickled
        _registry = getattr(sys, '_envoy_shutdown_events', {})
        prev_event = _registry.get(self.ownerComp.path)
        if prev_event is not None and isinstance(prev_event, Event):
            prev_event.set()

        # Clean up stale Event from .store() if present (not picklable)
        if self.ownerComp.fetch('envoy_shutdown_event', None) is not None:
            self.ownerComp.unstore('envoy_shutdown_event')

        self.shutdown_event = Event()
        _registry[self.ownerComp.path] = self.shutdown_event
        sys._envoy_shutdown_events = _registry
        self.ownerComp.store('envoy_running', False)

        # Defer auto-start so all init/recompile cycles finish first.
        # Guard: only auto-start if init() has already run. On fresh .tox
        # drop, __init__ fires BEFORE init() can reset the baked Envoyenable
        # to False — without this guard, Start() bypasses the opt-in prompt.
        # On code recompile (extension reinit during a running session),
        # _init_complete is already True so auto-start proceeds correctly.
        if (self.ownerComp.par.Envoyenable.eval()
                and self.ownerComp.fetch('_init_complete', False, search=False)):
            run(f"op('{self.ownerComp.path}').ext.Envoy.Start()",
                delayFrames=30)

        # If a deferred test run was in progress, unblock the old worker
        # thread so the old server can shut down cleanly (release the port).
        # The worker checks shutdown_event every 1s, but setting the Event
        # directly is faster and ensures it unblocks even if shutdown_event
        # was already set before the worker started polling.
        pending_test = getattr(sys, '_envoy_pending_test', None)
        if pending_test is not None:
            self._restoreStatusAfterTests()
            pending_test['holder']['result'] = {
                'error': 'Extension reinitialized during test run'}
            pending_test['event'].set()
            sys._envoy_pending_test = None

    # === Server Lifecycle ===

    def onDestroyTD(self):
        """Signal server shutdown when extension reinitializes.

        TD calls this on the OLD instance before the new one initializes.
        Only signals the shutdown event here — actual Thread Manager cleanup
        is deferred to _cleanupStaleThreads() in Start(), because modifying
        system COMP state (thread.clean(), Runningthreads parameter) during
        extension reinit can crash TD if triggered by a save-time file sync.
        """
        self.shutdown_event.set()

    def _cleanupStaleThreads(self) -> None:
        """Remove stale Envoy threads from the Thread Manager.

        Safety net called from Start() before creating the new server thread.
        Primary cleanup happens in onDestroyTD(). This catches edge cases:
        - onDestroyTD didn't run (project load, first init)
        - Multiple rapid reinits
        """
        try:
            self.ThreadManager.ext.ThreadManagerExt
        except Exception:
            return

        # Log Thread Manager state before cleanup
        thread_info = []
        for t in self.ThreadManager.ext.ThreadManagerExt.Threads:
            task = getattr(t, 'TDTask', None)
            target = getattr(task, 'target', None) if task else None
            name = getattr(target, '__name__', '?') if target else 'None'
            thread_info.append(
                f'{t.name}({name}, pool={t.InPool}, alive={t.is_alive()})')
        if thread_info:
            self._log(
                f'Thread Manager pre-cleanup: {len(thread_info)} threads: '
                f'{"; ".join(thread_info)}', 'DEBUG')

        cleaned = 0
        for thread in list(self.ThreadManager.ext.ThreadManagerExt.Threads):
            task = getattr(thread, 'TDTask', None)
            if task is None:
                continue
            target = getattr(task, 'target', None)
            if target is None or getattr(target, '__name__', '') != '_runServer':
                continue

            # Skip pool workers — shutdown_event handles their cleanup via
            # workLoop. Calling clean() would destroy the worker permanently.
            if thread.InPool:
                self._log(
                    'Skipping pool-worker _runServer '
                    '(shutdown_event handles it)', 'DEBUG')
                continue

            # All standalone _runServer threads here are stale:
            # onDestroyTD already cleaned the previous instance's thread,
            # and self.current_task is None (new task not created yet).
            thread.clean()
            with self.ThreadManager.ext.ThreadManagerExt.ManagerCondition:
                if task in self.ThreadManager.ext.ThreadManagerExt.Tasks:
                    self.ThreadManager.ext.ThreadManagerExt.Tasks.remove(task)
            cleaned += 1

        if cleaned:
            # CRITICAL: sync the Runningthreads parameter so EnqueueTask
            # sees the actual thread count, not the stale pre-cleanup value.
            self.ThreadManager.par.Runningthreads.val = len(
                self.ThreadManager.ext.ThreadManagerExt.Threads)
            self._log(
                f'Cleaned {cleaned} stale Envoy thread(s) — '
                f'{len(self.ThreadManager.ext.ThreadManagerExt.Threads)}'
                f' threads remain '
                f'(capacity: {self.ThreadManager.ext.ThreadManagerExt.MaxNumberOfThreads.eval()})', 'DEBUG')

    def _forceCloseOldServer(self) -> None:
        """Force-close a stuck old uvicorn server so the port is freed.

        When an old worker thread is stuck (e.g. waiting on a test Event or
        an HTTP connection), the normal shutdown_event signal may not be enough
        because uvicorn's event loop is blocked. This method:
        1. Signals ALL known shutdown events (in case one was orphaned)
        2. Force-closes the uvicorn server's socket listeners
        3. Unblocks any stuck test Event
        """
        # Signal all known shutdown events
        registry = getattr(sys, '_envoy_shutdown_events', {})
        for path, evt in registry.items():
            if not evt.is_set():
                self._log(f'Force-signaling shutdown event for {path}', 'DEBUG')
                evt.set()

        # Unblock any stuck test wait
        pending_test = getattr(sys, '_envoy_pending_test', None)
        if pending_test is not None:
            pending_test['holder']['result'] = {
                'error': 'Server force-restarted during test run'}
            pending_test['event'].set()
            sys._envoy_pending_test = None

        # Force-close the old uvicorn server's sockets
        old_server = getattr(sys, '_envoy_uvi_server', None)
        if old_server is not None:
            self._log('Force-closing old uvicorn server sockets', 'DEBUG')
            old_server.should_exit = True
            # force_exit skips graceful drain — without this, uvicorn waits
            # for established connections (e.g. MCP client keep-alives) to
            # close, which can block the port indefinitely.
            old_server.force_exit = True
            # Close all listener sockets to immediately free the port.
            # uvicorn.Server.servers holds asyncio.Server objects; each has
            # a .sockets tuple of the underlying socket.socket objects.
            for srv in getattr(old_server, 'servers', []):
                for sock in getattr(srv, 'sockets', ()) or ():
                    try:
                        sock.close()
                    except Exception:
                        pass
                try:
                    srv.close()
                except Exception:
                    pass
            sys._envoy_uvi_server = None

    def _findAvailablePort(self, base_port: int, range_size: int = 10) -> 'int | None':
        """Find an available port in [base_port, base_port + range_size).

        Tries the base port first (fast path for single-instance).  If busy,
        attempts to force-close a stale server from the same TD process, then
        scans the remaining range without force-close (those ports belong to
        other instances).

        Returns the first free port, or None if all are occupied.
        """
        import socket

        def _port_in_use(port: int) -> bool:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(1)
                return s.connect_ex(('127.0.0.1', port)) == 0

        # Fast path: preferred port is free
        if not _port_in_use(base_port):
            return base_port

        # Base port busy — try to reclaim it from our own stale server
        self._log(f'Port {base_port} in use, attempting to free it...')
        self._forceCloseOldServer()

        # Brief wait for socket to close (up to ~0.5s)
        import time as _time
        for _ in range(5):
            _time.sleep(0.1)
            if not _port_in_use(base_port):
                self._log(f'Port {base_port} freed after force-close')
                return base_port

        # Still busy — it belongs to another process/instance.
        # Scan the rest of the range without force-close.
        self._log(f'Port {base_port} held by another instance, scanning range...')
        for offset in range(1, range_size):
            candidate = base_port + offset
            if not _port_in_use(candidate):
                return candidate

        return None

    def Start(self) -> None:
        """Start MCP server via op.TDResources.ThreadManager"""
        if self.ownerComp.fetch('envoy_running', False):
            self._log('Server already running (duplicate Start ignored)', 'DEBUG')
            return
        # The envoy_running store can be lost on extension reinit (file sync
        # replaces baked-in code → extension reinitializes → storage cleared).
        # Check the status parameter as a backup — it survives reinit.
        # Only 'Running' means the server thread is actually active.
        # 'Starting...' is just a UI hint — not proof of an active thread.
        status = str(self.ownerComp.par.Envoystatus.eval())
        if status.startswith('Running'):
            self._log(f'Server already active (status: {status})', 'WARNING')
            self.ownerComp.store('envoy_running', True)
            return

        # Resolve git root silently — Start() never prompts. Dialogs belong only
        # in _enableEnvoy() / InitGit() which are explicitly user-initiated.
        git_root = self.ownerComp.fetch('_git_root', None, search=False)
        if not git_root:
            git_root = self._findGitRoot()
            self.ownerComp.store('_git_root', git_root)

        # Ensure Python environment is ready (idempotent fast path if already installed)
        op.Embody.ext.Embody._setupEnvironment()

        base_port = self.ownerComp.par.Envoyport.eval()
        port = self._findAvailablePort(base_port)
        if port is None:
            self._log(
                f'All ports {base_port}-{base_port + 9} in use. '
                f'Close a TouchDesigner instance or change the Port parameter.', 'ERROR')
            self.ownerComp.par.Envoystatus = f'Error: ports {base_port}\u2013{base_port + 9} in use'
            return
        if port != base_port:
            self._log(f'Port {base_port} in use by another instance, using {port}')

        self.ownerComp.store('envoy_running', True)

        # Clean up stale temp files from previous sessions
        self._cleanupTempFiles()

        # Create a FRESH Event for this server instance.  Don't clear() the old
        # one — it must stay set so the previous thread's shutdown_monitor sees it.
        self.shutdown_event = Event()
        _registry = getattr(sys, '_envoy_shutdown_events', {})
        _registry[self.ownerComp.path] = self.shutdown_event
        sys._envoy_shutdown_events = _registry

        self._server_gen += 1
        gen = self._server_gen

        self._log(f'Starting Envoy MCP server on port {port}')

        # Update status
        self.ownerComp.par.Envoystatus = 'Starting...'

        # Wrap hooks with generation guard so stale callbacks from a previous
        # server thread don't corrupt the running server's state.
        # Two checks: (1) instance identity — detects extension reinit (Update,
        # recompile) where a NEW EnvoyExt instance replaced us; (2) generation
        # counter — detects rapid Start() calls on the SAME instance.
        def guarded_success(returnValue=None, _gen=gen):
            try:
                if self.ownerComp.ext.Envoy is not self:
                    self._log('Stale server thread from previous init (ignored)', 'DEBUG')
                    return
            except Exception:
                return
            if self._server_gen != _gen:
                self._log('Stale server thread exited (ignored)', 'DEBUG')
                return
            self._onServerSuccess(returnValue)

        def guarded_error(error, _gen=gen):
            try:
                if self.ownerComp.ext.Envoy is not self:
                    self._log(f'Stale server error from previous init (ignored): {error}', 'DEBUG')
                    return
            except Exception:
                return
            if self._server_gen != _gen:
                self._log(f'Stale server error (ignored): {error}', 'DEBUG')
                return
            self._onServerError(error)

        # Free Thread Manager slots occupied by stale Envoy threads
        self._cleanupStaleThreads()

        # Create and enqueue a TDTask
        self.current_task = self.ThreadManager.TDTask(
            target=self._runServer,
            args=(port, self.request_queue, self.response_queue, self.shutdown_event),
            SuccessHook=guarded_success,
            ExceptHook=guarded_error,
            RefreshHook=self._onRefresh
        )
        thread = self.ThreadManager.EnqueueTask(
            self.current_task, standalone=True)

        if thread is None:
            self._log(
                'Thread Manager at capacity — Envoy task queued for pool '
                'execution instead of standalone thread.', 'WARNING')

        # Update status
        self.ownerComp.par.Envoystatus = f'Running on port {port}'

        # Auto-configure project files.
        # Each step is independent — one failure must not block the others.
        # MCP + AI config: always (uses git root if available, else project.folder)
        target_dir = git_root if git_root != 'no-git' else None
        self._configureMCPClient(port, target_dir=target_dir)
        try:
            op.Embody.ext.Embody._upgradeEnvoy()
        except Exception as e:
            self._log(f'Could not auto-configure AI client files: {e}', 'WARNING')

        # Git config: only when a git repo exists
        if git_root != 'no-git':
            from pathlib import Path
            git_path = Path(git_root)
            self._configureGitignore(git_path)
            self._configureGitattributes(git_path)

    def Stop(self) -> None:
        """Stop MCP server"""
        if not self.ownerComp.fetch('envoy_running', False):
            self._log('Envoy disabled')
            # Only set 'Disabled' if hooks haven't already set a more
            # specific status (e.g. 'Stopped' or 'Error: ...')
            current = str(self.ownerComp.par.Envoystatus.eval())
            if not current.startswith(('Stopped', 'Error')):
                self.ownerComp.par.Envoystatus = 'Disabled'
            return

        self._log('Stopping Envoy MCP server')
        self.ownerComp.store('envoy_running', False)
        self.shutdown_event.set()  # Signal uvicorn to exit

        # Remove this instance from the registry
        try:
            self._removeFromRegistry()
        except Exception as e:
            self._log(f'Registry cleanup failed: {e}', 'WARNING')

        # Update status
        self.ownerComp.par.Envoystatus = 'Disabled'

    # === Thread Manager Target (runs in worker thread) ===

    @staticmethod
    def _runServer(port: int, request_queue: Queue, response_queue: Queue,
                   shutdown_event: Event):
        """
        Target function for TDTask - runs MCP server in worker thread.
        IMPORTANT: No TouchDesigner calls allowed here! This is static.
        """
        def add_to_refresh(data):
            """Add data to the request queue (polled by RefreshHook on main thread)"""
            request_queue.put(data)

        try:
            server = EnvoyMCPServer(
                request_queue=None,  # Not used, we use InfoQueue
                response_queue=response_queue,
                add_to_refresh_queue=add_to_refresh,
                port=port,
                shutdown_event=shutdown_event
            )
            server.run()
        except OSError as e:
            if e.errno == 48 or 'address already in use' in str(e).lower():
                raise RuntimeError(
                    f'Port {port} is already in use. '
                    f'Another Envoy instance or process may be bound to it.'
                ) from e
            raise RuntimeError(f'MCP server failed on port {port}: {e}') from e
        except Exception as e:
            # uvicorn raises UnboundLocalError when bind fails — surface
            # the underlying cause if possible.
            if 'address already in use' in str(e).lower():
                raise RuntimeError(
                    f'Port {port} is already in use. '
                    f'Another Envoy instance or process may be bound to it.'
                ) from e
            raise RuntimeError(f'MCP server failed on port {port}: {e}') from e

    # === Thread Manager Callbacks (run on main thread) ===

    def _send_response(self, request_id, result):
        """Send a response back to the worker thread with piggybacked logs."""
        log_buffer = getattr(op.Embody.ext.Embody, '_log_buffer', None)
        if log_buffer:
            recent = [e for e in log_buffer
                      if e['id'] > self._last_served_log_id]
            if recent:
                result['_logs'] = recent[-20:]
                self._last_served_log_id = recent[-1]['id']

        self.response_queue.put({
            'id': request_id,
            'result': result
        })

    def _onRefresh(self):
        """
        RefreshHook - Called every frame on main thread while task is running.
        Polls request_queue for operations queued by the worker thread.
        """
        # Guard: bail if this RefreshHook fires on a stale instance
        # (e.g., thread wasn't cleaned yet after extension reinit)
        try:
            if self.ownerComp.ext.Envoy is not self:
                return
        except Exception:
            return

        # Process up to MAX_REQUESTS_PER_FRAME to avoid frame stalls from
        # burst MCP traffic.  Remaining requests queue to next frame.
        MAX_REQUESTS_PER_FRAME = 5
        processed = 0
        while processed < MAX_REQUESTS_PER_FRAME:
            try:
                info = self.request_queue.get_nowait()
            except Exception as e:
                # queue.Empty — no more pending requests this frame
                try:
                    expected = isinstance(e, Empty)
                except NameError:
                    expected = type(e).__name__ == 'Empty'
                if not expected:
                    self._log(f'Unexpected error reading request queue: {type(e).__name__}: {e}', 'WARNING')
                break
            processed += 1

            if not isinstance(info, dict) or 'operation' not in info:
                self._log(f'Invalid payload received: {info}', 'WARNING')
                continue

            request_id = info.get('id')
            operation = info['operation']
            params = info.get('params', {})

            self._log(f'Processing: {operation}')

            result = self._execute_operation(operation, params)

            # Deferred operations (e.g. run_tests) return None —
            # the worker thread handles its own response via Event
            if result is None:
                continue

            self._send_response(request_id, result)

    def _onServerSuccess(self, returnValue=None):
        """SuccessHook - Called when the thread task completes successfully"""
        self._log('Server thread completed')
        self.ownerComp.store('envoy_running', False)
        self.current_task = None
        if self.ownerComp.par.Envoyenable.eval():
            # Server stopped unexpectedly while still enabled —
            # turn off the toggle so toolbar UI stays in sync
            self.ownerComp.par.Envoystatus = 'Stopped'
            self.ownerComp.par.Envoyenable = False
        # If Envoyenable is already off, Stop() set the status — don't overwrite

    def _onServerError(self, error):
        """ExceptHook - Called when the thread task errors"""
        self._log(f'Server error: {error}', 'ERROR')
        self.ownerComp.store('envoy_running', False)
        self.current_task = None
        self.ownerComp.par.Envoystatus = f'Error: {error}'
        if self.ownerComp.par.Envoyenable.eval():
            # Turn off the toggle so toolbar UI stays in sync
            self.ownerComp.par.Envoyenable = False

    # === Operation Routing ===

    def _execute_operation(self, operation: str, params: dict) -> dict:
        """Route operation to appropriate handler"""
        handlers = {
            'create_op': self._create_op,
            'delete_op': self._delete_op,
            'get_op': self._get_op,
            'set_parameter': self._set_parameter,
            'get_parameter': self._get_parameter,
            'connect_ops': self._connect_ops,
            'disconnect_op': self._disconnect_op,
            'query_network': self._query_network,
            'copy_op': self._copy_op,
            'get_connections': self._get_connections,
            'execute_python': self._execute_python,
            # DAT content
            'get_dat_content': self._get_dat_content,
            'set_dat_content': self._set_dat_content,
            # Operator flags
            'get_op_flags': self._get_op_flags,
            'set_op_flags': self._set_op_flags,
            # Operator positioning & layout
            'get_op_position': self._get_op_position,
            'get_network_layout': self._get_network_layout,
            'set_op_position': self._set_op_position,
            'layout_children': self._layout_children,
            # Extended operator management
            'rename_op': self._rename_op,
            'cook_op': self._cook_op,
            'find_children': self._find_children,
            'get_op_performance': self._get_op_performance,
            'get_project_performance': self._get_project_performance,
            # Introspection & diagnostics
            'get_td_info': self._get_td_info,
            'get_op_errors': self._get_op_errors,
            'exec_op_method': self._exec_op_method,
            'get_td_classes': self._get_td_classes,
            'get_td_class_details': self._get_td_class_details,
            'get_module_help': self._get_module_help,
            # Embody integration
            'externalize_op': self._externalize_op,
            'remove_externalization_tag': self._remove_externalization_tag,
            'get_externalizations': self._get_externalizations,
            'save_externalization': self._save_externalization,
            'get_externalization_status': self._get_externalization_status,
            # Extension creation
            'create_extension': self._create_extension,
            # TDN network format
            'export_network': self._export_network,
            'import_network': self._import_network,
            # Annotations
            'create_annotation': self._create_annotation,
            'get_annotations': self._get_annotations,
            'set_annotation': self._set_annotation,
            'get_enclosed_ops': self._get_enclosed_ops,
            # Logging
            'get_logs': self._get_logs,
            # TOP capture
            'capture_top': self._capture_top,
            # Testing
            'run_tests': self._run_tests,
        }

        handler = handlers.get(operation)
        if handler:
            try:
                return handler(**params)
            except Exception as e:
                self._log(f'Operation {operation} failed: {e}', 'ERROR')
                return {'error': str(e)}
        return {'error': f'Unknown operation: {operation}'}

    # === TD Operations (Main Thread Only) ===

    # --- Logging ---

    def _get_logs(self, level=None, count=50, since_id=None, source=None):
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

    # --- Testing ---

    def _run_tests(self, suite_name=None, test_name=None):
        """Run Embody test suites via /embody/unit_tests extension (deferred).

        Starts tests with RunTestsDeferredPerTest (one test per frame) to
        keep TD responsive. The worker thread waits on a threading.Event
        stored on sys — the main-thread poll signals it when tests finish.
        This bypasses the response_queue entirely, surviving server restarts
        and extension reinit.

        Returns None on success (deferred). On error, signals the worker
        thread directly via the Event and returns None — never returns a
        dict, because the sentinel request_id=-1 would be silently dropped
        by check_responses, leaving the worker thread blocked.
        """
        pending = getattr(sys, '_envoy_pending_test', None)
        if pending is None:
            # Worker thread hasn't set up sys._envoy_pending_test yet.
            # This shouldn't happen because the worker sets it before queuing.
            return {'error': 'Test pending state not initialized'}

        test_comp = op.unit_tests
        if not test_comp:
            self._signalTestError(pending, 'Test framework not found (op.unit_tests)')
            return None
        if not test_comp.extensionsReady:
            self._signalTestError(pending, 'Test framework extension not ready')
            return None
        try:
            # Suppress Embody's Update/Refresh cycle during tests to
            # prevent extension reinit from TDN re-exports triggered by
            # test-created operators making COMPs structurally dirty.
            embody = op.Embody
            self._test_saved_status = embody.par.Status.eval()
            embody.par.Status = 'Testing'
            test_comp.RunTestsDeferredPerTest(
                suite_name=suite_name, test_name=test_name)
            self._schedulePollTestCompletion()
            return None  # Deferred — worker thread waits on sys._envoy_pending_test['event']
        except Exception as e:
            self._restoreStatusAfterTests()
            self._signalTestError(pending, f'Test run failed: {e}')
            return None

    def _restoreStatusAfterTests(self):
        """Re-enable Embody's Update cycle after tests complete."""
        saved = getattr(self, '_test_saved_status', None)
        if saved is not None:
            op.Embody.par.Status = saved
            self._test_saved_status = None

    def _signalTestError(self, pending, message):
        """Signal an error to the waiting worker thread via the test Event."""
        pending['holder']['result'] = {'error': message}
        pending['event'].set()
        sys._envoy_pending_test = None

    def _schedulePollTestCompletion(self):
        """Schedule the test completion poll via run() with a string
        expression that resolves the live extension instance at call time."""
        run(f"op('{self.ownerComp.path}').ext.Envoy._pollTestCompletion()",
            fromOP=self.ownerComp, delayFrames=5)

    def _pollTestCompletion(self):
        """Check if deferred test run has finished; signal worker thread if so."""
        pending = getattr(sys, '_envoy_pending_test', None)
        if pending is None:
            return  # Already handled or cancelled
        test_comp = op.unit_tests
        if not test_comp or not test_comp.extensionsReady:
            self._schedulePollTestCompletion()
            return
        runner = getattr(test_comp.ext, 'TestRunnerExt', None)
        if runner and not runner._running:
            self._restoreStatusAfterTests()
            result = runner._getSummary()
            # Piggyback logs
            log_buffer = getattr(op.Embody.ext.Embody, '_log_buffer', None)
            if log_buffer:
                recent = [e for e in log_buffer
                          if e['id'] > self._last_served_log_id]
                if recent:
                    result['_logs'] = recent[-20:]
                    self._last_served_log_id = recent[-1]['id']
            # Signal the worker thread directly via the Event
            pending['holder']['result'] = result
            pending['event'].set()
        else:
            self._schedulePollTestCompletion()

    # --- Operator Management ---

    def _create_op(self, parent_path: str, op_type: str, name: str = None) -> dict:
        """Create an operator"""
        parent = op(parent_path)
        if not parent:
            return {'error': f'Parent not found: {parent_path}'}

        if not hasattr(parent, 'create'):
            return {'error': f'Cannot create children in {parent_path} (not a COMP)'}

        try:
            # op_type can be a string like 'baseCOMP', 'noiseTOP', etc.
            new_op = parent.create(op_type, name) if name else parent.create(op_type)
            self._find_non_overlapping_position(parent, new_op)
            return {
                'success': True,
                'path': new_op.path,
                'name': new_op.name,
                'type': new_op.OPType,
                'family': new_op.family,
                'nodeX': new_op.nodeX,
                'nodeY': new_op.nodeY
            }
        except Exception as e:
            return {'error': f'Failed to create operator: {e}'}

    def _delete_op(self, op_path: str) -> dict:
        """Delete an operator"""
        target = op(op_path)
        if not target:
            return {'error': f'Operator not found: {op_path}'}

        try:
            name = target.name
            target.destroy()
            return {'success': True, 'deleted': op_path, 'name': name}
        except Exception as e:
            return {'error': f'Failed to delete operator: {e}'}

    def _get_op(self, op_path: str) -> dict:
        """Get operator information"""
        target = op(op_path)
        if not target:
            return {'error': f'Operator not found: {op_path}'}

        info = {
            'path': target.path,
            'name': target.name,
            'type': target.OPType,
            'family': target.family,
            'valid': target.valid,
        }

        # Get parameters
        params = {}
        for p in target.pars():
            try:
                params[p.name] = {
                    'value': str(p.eval()),
                    'mode': str(p.mode),
                    'label': p.label,
                }
            except Exception as e:
                self._log(f'Could not read parameter {p.name} on {op_path}: {e}', 'DEBUG')
                params[p.name] = {'value': 'N/A', 'mode': 'N/A'}
        info['parameters'] = params

        # Get inputs/outputs
        info['inputs'] = [inp.path if inp else None for inp in target.inputs]
        info['outputs'] = [out.path if out else None for out in target.outputs]

        # COMP-specific info
        if hasattr(target, 'children'):
            info['children'] = [child.name for child in target.children]

        return self._maybe_offload_to_file(info, 'get_op')

    def _set_parameter(self, op_path: str, par_name: str, value=None,
                      mode: str = None, expr: str = None,
                      bind_expr: str = None) -> dict:
        """Set a parameter value, expression, bind expression, or mode"""
        target = op(op_path)
        if not target:
            return {'error': f'Operator not found: {op_path}'}

        if not hasattr(target.par, par_name):
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

    def _get_parameter(self, op_path: str, par_name: str) -> dict:
        """Get a parameter value with full details"""
        target = op(op_path)
        if not target:
            return {'error': f'Operator not found: {op_path}'}

        if not hasattr(target.par, par_name):
            return {'error': f'Parameter not found: {par_name}'}

        try:
            par = getattr(target.par, par_name)
            result = {
                'path': op_path,
                'parameter': par_name,
                'value': str(par.eval()),
                'mode': str(par.mode),
                'label': par.label,
                'default': str(par.default),
                'isCustom': par.isCustom,
                'readOnly': par.readOnly,
                'style': par.style,
            }

            # Mode-specific details
            if par.mode.name == 'EXPRESSION':
                result['expression'] = par.expr
            elif par.mode.name == 'BIND':
                result['bindExpr'] = par.bindExpr
                result['bindMaster'] = par.bindMaster.path if par.bindMaster else None
            elif par.mode.name == 'EXPORT':
                result['exportOP'] = par.exportOP.path if par.exportOP else None
                result['exportSource'] = str(par.exportSource) if par.exportSource else None

            # Numeric range info
            if par.isNumber:
                result['min'] = par.min
                result['max'] = par.max
                result['clampMin'] = par.clampMin
                result['clampMax'] = par.clampMax
                result['normMin'] = par.normMin
                result['normMax'] = par.normMax

            # Menu info
            if par.isMenu:
                result['menuNames'] = par.menuNames
                result['menuLabels'] = par.menuLabels
                result['menuIndex'] = par.menuIndex

            return result
        except Exception as e:
            return {'error': f'Failed to get parameter: {e}'}

    def _connect_ops(self, source_path: str, dest_path: str,
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

    def _disconnect_op(self, op_path: str, input_index: int = 0,
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

    def _query_network(self, parent_path: str = "/", recursive: bool = False,
                      op_type: str = None, include_utility: bool = False) -> dict:
        """List operators in a network"""
        parent = op(parent_path)
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
                    'name': child.name,
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
        return self._maybe_offload_to_file(result, 'query_network')

    def _copy_op(self, source_path: str, dest_parent: str, new_name: str = None) -> dict:
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
            self._find_non_overlapping_position(dest, new_op)
            return {
                'success': True,
                'source': source_path,
                'new_path': new_op.path,
                'new_name': new_op.name,
                'nodeX': new_op.nodeX,
                'nodeY': new_op.nodeY
            }
        except Exception as e:
            return {'error': f'Failed to copy: {e}'}

    def _get_connections(self, op_path: str) -> dict:
        """Get all connections for an operator"""
        target = op(op_path)
        if not target:
            return {'error': f'Operator not found: {op_path}'}

        inputs = []
        for i, inp in enumerate(target.inputs):
            inputs.append({
                'index': i,
                'connected_to': inp.path if inp else None
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

    def _execute_python(self, code: str) -> dict:
        """Execute arbitrary Python code"""
        code_preview = code[:200] + ('...' if len(code) > 200 else '')
        self._log(f'execute_python: {code_preview}')
        try:
            # Create a namespace with useful globals
            namespace = {
                'op': op,
                'ops': ops,
                'parent': parent,
                'root': root,
                'me': self.ownerComp,
                'result': None
            }

            exec(code, namespace)

            # Return the 'result' variable if set
            result = namespace.get('result')
            self._log(f'execute_python: completed successfully')
            if result is not None:
                return {'success': True, 'result': str(result)}
            return {'success': True}
        except Exception as e:
            self._log(f'execute_python failed: {e}', 'ERROR')
            return {'error': f'Execution failed: {e}'}

    # === Introspection & Diagnostics (Main Thread Only) ===

    def _get_td_info(self) -> dict:
        """Get TouchDesigner environment and Envoy server info"""
        try:
            import td as _td
            version = _td.app.version
            build = _td.app.build
            return {
                'server': f'TouchDesigner {version}.{build}',
                'version': f'{version}.{build}',
                'osName': _td.app.osName,
                'osVersion': _td.app.osVersion,
                'envoyVersion': ENVOY_VERSION,
            }
        except Exception as e:
            return {'error': f'Failed to get TD info: {e}'}

    def _get_op_errors(self, op_path: str, recurse: bool = True) -> dict:
        """Get error and warning messages for an operator and its children"""
        target = op(op_path)
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
                    self._log(f'Error getting {severity}s from {op_path}: {e}', 'WARNING')

        return {
            'path': target.path,
            'errorCount': len(all_errors),
            'warningCount': len(all_warnings),
            'hasErrors': bool(all_errors),
            'hasWarnings': bool(all_warnings),
            'errors': all_errors,
            'warnings': all_warnings,
        }

    def _exec_op_method(self, op_path: str, method: str,
                          args: list = None, kwargs: dict = None) -> dict:
        """Call a method on a TD operator"""
        target = op(op_path)
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
            processed = self._process_result(result)
            return {'success': True, 'result': processed}
        except Exception as e:
            return {'error': f'Method execution failed: {e}'}

    def _get_td_classes(self) -> dict:
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

    def _get_td_class_details(self, class_name: str) -> dict:
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
                    self._log(f'Could not inspect member {name} on {class_name}: {e}', 'DEBUG')
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

    def _get_module_help(self, module_name: str) -> dict:
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

    def _process_result(self, result) -> object:
        """Process a method result for JSON serialization"""
        if result is None or isinstance(result, (int, float, str, bool)):
            return result
        if isinstance(result, (list, tuple)):
            return [self._process_result(item) for item in result]
        if isinstance(result, dict):
            return {k: self._process_result(v) for k, v in result.items()}
        # TD operator objects -> path string
        if hasattr(result, 'path') and hasattr(result, 'valid'):
            return result.path
        return str(result)

    # === DAT Content Operations (Main Thread Only) ===

    def _get_dat_content(self, op_path: str, format: str = "auto") -> dict:
        """Get DAT content as text or table data"""
        target = op(op_path)
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

    def _set_dat_content(self, op_path: str, text: str = None,
                        rows: list = None, clear: bool = False) -> dict:
        """Set DAT content from text or table rows"""
        target = op(op_path)
        if not target:
            return {'error': f'Operator not found: {op_path}'}
        if target.family != 'DAT':
            return {'error': f'{op_path} is not a DAT (family: {target.family})'}

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

    # === TOP Capture (Main Thread Only) ===

    def _capture_top(self, op_path: str, format: str = 'jpeg',
                     quality: float = 0.8, max_resolution: int = 640) -> dict:
        """Capture a TOP operator's output as a compressed image."""
        import base64

        target = op(op_path)
        if not target:
            return {'error': f'Operator not found: {op_path}'}
        if target.family != 'TOP':
            return {'error': f'{op_path} is not a TOP (family: {target.family})'}

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
            }
        except Exception as e:
            return {'error': f'Failed to capture TOP: {e}'}

    # === Operator Flags Operations (Main Thread Only) ===

    def _get_op_flags(self, op_path: str) -> dict:
        """Get all flags for an operator"""
        target = op(op_path)
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

    def _set_op_flags(self, op_path: str, bypass: bool = None, lock: bool = None,
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

            return self._get_op_flags(op_path)
        except Exception as e:
            return {'error': f'Failed to set flags: {e}'}

    # === Node Positioning & Layout (Main Thread Only) ===

    def _find_non_overlapping_position(self, parent, new_op):
        """Reposition new_op so it doesn't overlap any sibling in the parent COMP."""
        MARGIN = 20

        siblings = [child for child in parent.children if child.path != new_op.path]
        if not siblings:
            return  # No siblings — default position is fine

        w = new_op.nodeWidth
        h = new_op.nodeHeight

        # Collect sibling bounding rectangles
        rects = [(s.nodeX, s.nodeY, s.nodeWidth, s.nodeHeight) for s in siblings]

        def has_overlap(x, y):
            for (sx, sy, sw, sh) in rects:
                if (x < sx + sw + MARGIN and x + w + MARGIN > sx and
                        y < sy + sh + MARGIN and y + h + MARGIN > sy):
                    return True
            return False

        # If current position is already clear, nothing to do
        if not has_overlap(new_op.nodeX, new_op.nodeY):
            return

        # Grid scan: cell size = op dimensions + margin
        step_x = w + MARGIN
        step_y = h + MARGIN

        # Start from top-left corner of existing layout
        origin_x = min(r[0] for r in rects)
        origin_y = max(r[1] for r in rects)  # highest Y = top

        for row in range(20):
            for col in range(20):
                test_x = origin_x + col * step_x
                test_y = origin_y - row * step_y  # scan downward
                if not has_overlap(test_x, test_y):
                    new_op.nodeX = int(test_x)
                    new_op.nodeY = int(test_y)
                    return

    def _get_op_position(self, op_path: str) -> dict:
        """Get operator position and visual properties"""
        target = op(op_path)
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

    def _get_network_layout(self, comp_path: str, include_annotations: bool = True) -> dict:
        """Get positions of all operators and annotations in a COMP"""
        parent_op = op(comp_path)
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
                    'name': child.name,
                    'type': child.OPType,
                    'family': child.family,
                    'nodeX': child.nodeX,
                    'nodeY': child.nodeY,
                    'nodeWidth': child.nodeWidth,
                    'nodeHeight': child.nodeHeight,
                    'nodeCenterX': child.nodeCenterX,
                    'nodeCenterY': child.nodeCenterY,
                }
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
                    annotations.append({
                        'path': child.path,
                        'name': child.name,
                        'nodeX': child.nodeX,
                        'nodeY': child.nodeY,
                        'nodeWidth': child.nodeWidth,
                        'nodeHeight': child.nodeHeight,
                        'text': child.par.text.eval() if hasattr(child.par, 'text') else '',
                    })
                result['annotations'] = annotations

            return self._maybe_offload_to_file(result, 'get_network_layout')

        except Exception as e:
            return {'error': f'Failed to get network layout: {e}'}

    def _set_op_position(self, op_path: str, x: int = None, y: int = None,
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

            result = self._get_op_position(op_path)

            # Check for overlaps with siblings after repositioning
            if (x is not None or y is not None) and target.parent():
                MARGIN = 20
                overlaps = []
                tx, ty, tw, th = target.nodeX, target.nodeY, target.nodeWidth, target.nodeHeight
                for sibling in target.parent().children:
                    if sibling.path == target.path:
                        continue
                    sx, sy, sw, sh = sibling.nodeX, sibling.nodeY, sibling.nodeWidth, sibling.nodeHeight
                    if (tx < sx + sw + MARGIN and tx + tw + MARGIN > sx and
                            ty < sy + sh + MARGIN and ty + th + MARGIN > sy):
                        overlaps.append(sibling.name)
                if overlaps:
                    result['overlap_warning'] = f'Overlaps with: {", ".join(overlaps)}. Reposition to avoid.'

            return result
        except Exception as e:
            return {'error': f'Failed to set position: {e}'}

    def _layout_children(self, op_path: str) -> dict:
        """Auto-layout children in a COMP"""
        target = op(op_path)
        if not target:
            return {'error': f'Operator not found: {op_path}'}
        if not target.isCOMP:
            return {'error': f'{op_path} is not a COMP'}

        try:
            target.layout()
            return {'success': True, 'path': op_path}
        except Exception as e:
            return {'error': f'Failed to layout: {e}'}

    # === Annotations (Main Thread Only) ===

    def _create_annotation(self, parent_path: str, mode: str = "annotate",
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

    def _get_annotations(self, parent_path: str) -> dict:
        """List all annotations in a COMP."""
        parent = op(parent_path)
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

    def _set_annotation(self, op_path: str, text: str = None, title: str = None,
                        color: list = None, opacity: float = None,
                        width: int = None, height: int = None,
                        x: int = None, y: int = None) -> dict:
        """Modify an existing annotation."""
        target = op(op_path)
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

    def _get_enclosed_ops(self, op_path: str) -> dict:
        """Get annotation/operator enclosure relationships."""
        target = op(op_path)
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

    # === Extended Operator Management (Main Thread Only) ===

    def _rename_op(self, op_path: str, new_name: str) -> dict:
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

    def _cook_op(self, op_path: str, force: bool = True,
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

    def _find_children(self, op_path: str, name: str = None, type: str = None,
                      depth: int = None, tags: list = None,
                      text: str = None, comment: str = None,
                      include_utility: bool = False) -> dict:
        """Search for operators using COMP.findChildren"""
        target = op(op_path)
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

    def _get_op_performance(self, op_path: str, include_children: bool = False) -> dict:
        """Get performance data for an operator"""
        target = op(op_path)
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

    def _get_project_performance(self, include_hotspots: int = 0) -> dict:
        """Get project-level performance via Perform CHOP."""
        try:
            perform = self.ownerComp.op('_envoy_perform')
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
                result['hotspots'] = self._get_performance_hotspots(include_hotspots)

            return result
        except Exception as e:
            return {'error': f'Failed to get project performance: {e}'}

    def _get_performance_hotspots(self, top_n: int) -> list:
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

    # === Embody Integration ===

    def _externalize_op(self, op_path: str, tag_type: str = None) -> dict:
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

    def _remove_externalization_tag(self, op_path: str) -> dict:
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

    def _get_externalizations(self) -> dict:
        """Get all externalized operators"""
        try:
            table = op.Embody.ext.Embody.Externalizations
            if not table:
                return {'error': 'Externalizations table not found'}

            externalizations = []
            for row in range(1, table.numRows):
                externalizations.append({
                    'path': table[row, 'path'].val,
                    'type': table[row, 'type'].val,
                    'file_path': table[row, 'rel_file_path'].val,
                    'timestamp': table[row, 'timestamp'].val,
                    'dirty': table[row, 'dirty'].val,
                    'build': table[row, 'build'].val,
                })

            return {
                'count': len(externalizations),
                'externalizations': externalizations
            }
        except Exception as e:
            return {'error': f'Failed to get externalizations: {e}'}

    def _save_externalization(self, op_path: str) -> dict:
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
                    return {'error': f'DAT at {op_path} does not have file sync enabled — not externalized'}
            else:
                return {'error': f'Operator family "{target.family}" is not supported for save_externalization'}

            return {'success': True, 'path': op_path}
        except Exception as e:
            return {'error': f'Failed to save: {e}'}

    def _get_externalization_status(self, op_path: str) -> dict:
        """Get externalization status for an operator"""
        target = op(op_path)
        if not target:
            return {'error': f'Operator not found: {op_path}'}

        try:
            table = op.Embody.ext.Embody.Externalizations
            if not table:
                return {'error': 'Externalizations table not found'}

            # Find the row for this operator
            for row in range(1, table.numRows):
                if table[row, 'path'].val == op_path:
                    return {
                        'path': op_path,
                        'externalized': True,
                        'type': table[row, 'type'].val,
                        'file_path': table[row, 'rel_file_path'].val,
                        'timestamp': table[row, 'timestamp'].val,
                        'dirty': table[row, 'dirty'].val,
                        'build': table[row, 'build'].val,
                        'touch_build': table[row, 'touch_build'].val,
                    }

            return {
                'path': op_path,
                'externalized': False
            }
        except Exception as e:
            return {'error': f'Failed to get status: {e}'}

    # === Extension Creation (Main Thread Only) ===

    def _create_extension(self, parent_path: str, class_name: str,
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
                self._find_non_overlapping_position(parent_op, comp)
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

        if init_warning:
            result['warning'] = init_warning

        return result

    # === TDN Network Format (Main Thread Only) ===

    def _export_network(self, root_path='/', include_dat_content=True,
                       output_file=None, max_depth=None, embed_all=False):
        """Delegate to TDN extension for network export."""
        if not getattr(self.ownerComp.ext, 'TDN', None):
            return {'error': 'TDN extension not loaded on Embody COMP'}
        # Protect .tdn files belonging to other tracked TDN COMPs
        protected = self.ownerComp.ext.Embody._getAllTrackedTDNFiles(
            exclude_path=root_path) if output_file else None
        return self.ownerComp.ext.TDN.ExportNetwork(
            root_path=root_path,
            include_dat_content=include_dat_content,
            output_file=output_file,
            max_depth=max_depth,
            cleanup_protected=protected,
            embed_all=embed_all,
        )

    def _import_network(self, target_path, tdn, clear_first=False):
        """Delegate to TDN extension for network import."""
        if not getattr(self.ownerComp.ext, 'TDN', None):
            return {'error': 'TDN extension not loaded on Embody COMP'}
        return self.ownerComp.ext.TDN.ImportNetwork(
            target_path=target_path,
            tdn=tdn,
            clear_first=clear_first,
        )

    # === Utility Methods ===

    def _configureMCPClient(self, port, target_dir=None):
        """Auto-configure MCP client by writing .mcp.json and the STDIO bridge
        script.  Uses STDIO transport so Claude Code always has tools available
        (the bridge retries until Envoy is reachable).
        Idempotent — safe to call on every start.

        Args:
            port: The port Envoy is running on.
            target_dir: Directory to write config files into. Defaults to
                git root if available, else project.folder.
        """
        from pathlib import Path
        try:
            project_dir = Path(project.folder)

            if target_dir is None:
                # Find the git root by walking up from the .toe directory
                for parent_path in [project_dir] + list(project_dir.parents):
                    if (parent_path / '.git').exists():
                        target_dir = parent_path
                        break
                if target_dir is None:
                    target_dir = project_dir

            target_dir = Path(target_dir)

            # --- Deploy the STDIO bridge script ---
            bridge_dir = target_dir / '.claude'
            bridge_dir.mkdir(parents=True, exist_ok=True)
            bridge_path = bridge_dir / 'envoy-bridge.py'

            # Read bridge script from templates textDAT, else from disk fallback
            bridge_content = None
            try:
                templates = self.ownerComp.op('templates')
                bridge_dat = templates.op('text_envoy_bridge') if templates else None
                if bridge_dat:
                    bridge_content = bridge_dat.text
            except Exception:
                pass

            if not bridge_content:
                # Fallback: read from the externalized file in dev/embody/
                source = Path(project.folder) / 'embody' / 'envoy_bridge.py'
                if source.exists():
                    bridge_content = source.read_text(encoding='utf-8')

            if not bridge_content:
                self._log(
                    'Bridge script source not found — falling back to HTTP transport',
                    'WARNING')
                self._configureMCPClientHTTP(target_dir, port)
                return

            # Write bridge script only if content changed — preserving
            # mtime prevents Claude Code's file watcher from restarting
            # the MCP server mid-connection.
            needs_write = True
            if bridge_path.exists():
                try:
                    existing = bridge_path.read_text(encoding='utf-8')
                    if existing == bridge_content:
                        needs_write = False
                except OSError:
                    pass  # Can't read — overwrite

            if needs_write:
                bridge_path.write_text(bridge_content, encoding='utf-8')
                if sys.platform != 'win32':
                    bridge_path.chmod(0o755)
            else:
                if sys.platform != 'win32':
                    bridge_path.chmod(0o755)

            # Prefer the venv Python (created from TD's Python) so the bridge
            # works on machines without a system Python installation.
            # Fall back to system PATH command if the venv doesn't exist yet.
            if sys.platform == 'win32':
                venv_python = project_dir / '.venv' / 'Scripts' / 'python.exe'
            else:
                venv_python = project_dir / '.venv' / 'bin' / 'python3'

            if venv_python.is_file():
                # Verify the venv Python actually executes — catches stale
                # pyvenv.cfg pointing to an uninstalled TD version.
                try:
                    subprocess.run(
                        [str(venv_python), '-c',
                         'import sys; print(sys.version)'],
                        capture_output=True, timeout=10, check=True)
                    python_cmd = str(venv_python).replace('\\', '/')
                except (subprocess.CalledProcessError,
                        subprocess.TimeoutExpired, OSError) as e:
                    self._log(
                        f'Venv Python at {venv_python} exists but failed to '
                        f'execute: {e}. Delete the .venv directory and '
                        f'restart Envoy to recreate it.', 'WARNING')
                    python_cmd = ('python' if sys.platform == 'win32'
                                  else 'python3')
            else:
                python_cmd = 'python' if sys.platform == 'win32' else 'python3'

            # --- Write .envoy.json project config ---
            self._writeEnvoyConfig(target_dir, port)

            # --- Write .mcp.json with STDIO transport ---
            mcp_file = target_dir / '.mcp.json'
            # Use forward slashes even on Windows for JSON portability
            bridge_abs = str(bridge_path).replace('\\', '/')
            config_abs = str(
                (target_dir / '.envoy.json')).replace('\\', '/')

            # Read existing config to preserve other servers
            config = {}
            if mcp_file.exists():
                try:
                    config = json.loads(mcp_file.read_text(encoding='utf-8'))
                except (json.JSONDecodeError, OSError) as e:
                    self._log(f'Could not parse existing .mcp.json, will overwrite: {e}', 'DEBUG')

            servers = config.get('mcpServers', {})
            existing = servers.get('envoy', {})

            # Check if already configured with matching STDIO bridge
            expected_args = ['-u', bridge_abs, '--port', str(port),
                             '--config', config_abs]
            if (existing.get('type') == 'stdio'
                    and existing.get('command') == python_cmd
                    and existing.get('args') == expected_args):
                self._log('MCP .mcp.json already configured (STDIO bridge)', 'DEBUG')
                self._deploySettingsLocal(bridge_dir)
                return

            servers['envoy'] = {
                'type': 'stdio',
                'command': python_cmd,
                'args': expected_args,
            }
            config['mcpServers'] = servers
            mcp_file.write_text(
                json.dumps(config, indent=2) + '\n', encoding='utf-8')
            self._log(f'Wrote MCP config to {mcp_file} (STDIO bridge → port {port})')

            # --- Deploy settings.local.json (auto-allow read-only MCP tools) ---
            self._deploySettingsLocal(bridge_dir)

        except Exception as e:
            self._log(f'Could not auto-configure MCP client: {e}', 'WARNING')

    def _configureMCPClientHTTP(self, target_dir, port):
        """Fallback: configure .mcp.json with direct HTTP transport.
        Used when the STDIO bridge script cannot be deployed."""
        url = f'http://localhost:{port}/mcp'
        mcp_file = target_dir / '.mcp.json'

        config = {}
        if mcp_file.exists():
            try:
                config = json.loads(mcp_file.read_text(encoding='utf-8'))
            except (json.JSONDecodeError, OSError):
                pass

        servers = config.get('mcpServers', {})
        servers['envoy'] = {'type': 'http', 'url': url}
        config['mcpServers'] = servers
        mcp_file.write_text(
            json.dumps(config, indent=2) + '\n', encoding='utf-8')
        self._log(f'Wrote MCP config to {mcp_file} (HTTP fallback)')

    def _deploySettingsLocal(self, claude_dir):
        """Deploy settings.local.json to .claude/ from the template DAT.

        Auto-allows read-only Envoy MCP tools so the user isn't prompted
        for every query operation. Only writes if the file doesn't exist
        yet — never overwrites user customizations.
        """
        settings_path = claude_dir / 'settings.local.json'
        if settings_path.exists():
            self._log('settings.local.json already exists — skipping', 'DEBUG')
            return

        settings_content = None
        try:
            templates = self.ownerComp.op('templates')
            settings_dat = templates.op('text_settings_local') if templates else None
            if settings_dat:
                settings_content = settings_dat.text
        except Exception:
            pass

        if not settings_content:
            self._log('text_settings_local template not found — skipping settings deployment', 'DEBUG')
            return

        claude_dir.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(settings_content, encoding='utf-8')
        self._log(f'Deployed settings.local.json to {settings_path}')

    def _findGitRoot(self):
        """Silently find the git repo root. Returns Path or 'no-git'. Never prompts."""
        from pathlib import Path
        project_dir = Path(project.folder)
        home_dir = Path.home()
        for parent in [project_dir] + list(project_dir.parents):
            if parent == home_dir or len(parent.parts) <= len(home_dir.parts):
                break
            if (parent / '.git').exists():
                self._log(f'Found git repo at {parent}', 'INFO')
                return parent
        self._log(f'No git repo found for {project_dir} (stopped at {home_dir})', 'INFO')
        return 'no-git'

    def _checkOrInitGitRepo(self):
        """Check for a git repo. If missing, prompt user to initialize one.
        Only call from user-initiated flows (_enableEnvoy, InitGit) — never
        from automatic startup paths. Returns Path, 'no-git', or None (cancelled)."""
        from pathlib import Path
        import os, subprocess

        project_dir = Path(project.folder)
        home_dir = Path.home()

        # Walk up looking for .git, but stop before the home directory —
        # a .git in ~ (e.g. dotfiles repo) is never the intended project repo.
        for parent in [project_dir] + list(project_dir.parents):
            if parent == home_dir or len(parent.parts) <= len(home_dir.parts):
                break
            if (parent / '.git').exists():
                self._log(f'Found git repo at {parent}', 'INFO')
                return parent

        # No git repo found between project folder and home directory.
        self._log(
            f'No git repo found for {project_dir} (stopped at {home_dir})',
            'INFO')

        # Prompt user.
        # Guard against concurrent calls: ui.messageBox blocks the main
        # thread but TD's run() callbacks still fire, so a second Start()
        # can reach here while the first dialog is open.
        if getattr(self, '_git_prompt_active', False):
            self._log('Git prompt already active (duplicate suppressed)', 'DEBUG')
            return 'no-git'
        self._git_prompt_active = True
        try:
            choice = op.Embody.ext.Embody._messageBox(
                'Envoy — Git Repository Recommended',
                'A git repository is recommended for .gitignore and\n'
                '.gitattributes management. No git repository was found.\n\n'
                'MCP and AI client config files will be generated either way.\n\n'
                f'Initialize a git repo in:\n  {project_dir}\n\n'
                'Or browse to select a different folder (e.g. an existing repo root).\n'
                'You can also run op.Embody.InitGit() later.',
                buttons=['Cancel', 'Initialize Git Here', 'Browse for Folder', 'Start Without Git'])

            if choice not in (1, 2, 3):  # Cancel or closed dialog
                self.ownerComp.par.Envoyenable = False
                self._log('Envoy cancelled — no git repository.', 'INFO')
                return None

            if choice == 2:  # Browse for Folder
                result = ui.chooseFolder(
                    title='Select Git Repository Root', start=str(project_dir))
                if not result:
                    self.ownerComp.par.Envoyenable = False
                    self._log('Envoy cancelled — folder selection aborted.', 'INFO')
                    return None
                chosen = Path(result)
                # If the chosen folder already contains a .git, use it directly
                if (chosen / '.git').exists():
                    self._log(f'Using existing git repo at {chosen}', 'SUCCESS')
                    return chosen
                # No .git there — offer to initialize in that folder
                init_choice = op.Embody.ext.Embody._messageBox(
                    'Envoy — Initialize Git',
                    f'No git repo found in:\n  {chosen}\n\nInitialize git here?',
                    buttons=['Cancel', 'Initialize Git'])
                if init_choice not in (1,):
                    self.ownerComp.par.Envoyenable = False
                    return None
                project_dir = chosen  # use chosen folder for init below

            if choice in (1, 2):  # Initialize Git Here, or Browse → confirmed init
                try:
                    # Strip git env vars that TD's embedded Python may set —
                    # these can cause git init to produce a broken repository.
                    clean_env = {
                        k: v for k, v in os.environ.items()
                        if k not in (
                            'GIT_DIR', 'GIT_WORK_TREE',
                            'GIT_INDEX_FILE', 'GIT_CEILING_DIRECTORIES',
                        )
                    }
                    git_kwargs = dict(
                        capture_output=True, text=True,
                        cwd=str(project_dir), env=clean_env,
                    )
                    subprocess.run(['git', 'init'], check=True, **git_kwargs)
                    self._log(f'Initialized git repo in {project_dir}', 'SUCCESS')

                    # Verify the init produced a working repository
                    verify = subprocess.run(
                        ['git', 'rev-parse', '--is-inside-work-tree'],
                        **git_kwargs)
                    if verify.returncode != 0:
                        self._log('Git verify failed after init — retrying', 'WARNING')
                        subprocess.run(['git', 'init'], check=True, **git_kwargs)
                        verify = subprocess.run(
                            ['git', 'rev-parse', '--is-inside-work-tree'],
                            **git_kwargs)
                        if verify.returncode != 0:
                            raise RuntimeError(
                                f'git rev-parse failed after retry: '
                                f'{verify.stderr.strip()}')
                        self._log('Git repo verified after retry', 'SUCCESS')

                    # Git config files belong with git init (issue #8).
                    self._configureGitignore(project_dir)
                    self._configureGitattributes(project_dir)

                    return project_dir
                except Exception as e:
                    self._log(f'Failed to initialize git repo: {e}', 'ERROR')
                    op.Embody.ext.Embody._messageBox(
                        'Envoy — Git Initialization Failed',
                        f'Could not initialize a git repository:\n\n  {e}\n\n'
                        'Envoy will start without git. MCP and AI client\n'
                        'config will be generated in the project folder.\n'
                        '.gitignore and .gitattributes will be skipped.\n\n'
                        'To add git later: run "git init" manually, then\n'
                        'call op.Embody.InitGit() from the textport.',
                        buttons=['OK'])
                    # Fall through to start-without-git

            # choice == 3 or git init failed — start without git
            self._log('Starting Envoy without git repo — auto-config skipped.', 'WARNING')
            return 'no-git'
        finally:
            self._git_prompt_active = False

    @staticmethod
    def _atomicWriteJSON(path, data):
        """Write JSON atomically via temp file + os.replace().
        Retries on PermissionError (Windows file-in-use)."""
        import os
        from pathlib import Path
        tmp = Path(str(path) + '.tmp')
        content = json.dumps(data, indent=2) + '\n'
        for attempt in range(3):
            try:
                tmp.write_text(content, encoding='utf-8')
                os.replace(str(tmp), str(path))
                return
            except PermissionError:
                if attempt < 2:
                    import time as _time
                    _time.sleep(0.1)
                else:
                    raise

    def _instanceKey(self, toe_rel: str, existing_instances: dict) -> str:
        """Compute a unique instance key from the toe filename.
        Uses basename without .toe.  Appends -2, -3, etc. on collision
        with a live instance (same or different toe_path).
        If Envoyinstancename is set, uses that as the key instead."""
        import os
        from pathlib import Path

        # User override via parameter
        try:
            custom = self.ownerComp.par.Envoyinstancename.eval()
            if custom:
                return custom
        except:
            pass

        base = Path(toe_rel).stem  # e.g. 'Embody-5.251'
        my_pid = os.getpid()

        # Check if this PID already has a key (re-registration)
        for key, info in existing_instances.items():
            if info.get('td_pid') == my_pid:
                return key

        # Check if base key is free or held by a dead process
        if base not in existing_instances:
            return base
        existing_pid = existing_instances[base].get('td_pid', 0)
        if not self._isPidAlive(existing_pid):
            return base  # Reclaim stale entry

        # Base key is held by a live process — find a unique suffix
        suffix = 2
        while True:
            candidate = f'{base}-{suffix}'
            if candidate not in existing_instances:
                return candidate
            existing_pid = existing_instances[candidate].get('td_pid', 0)
            if not self._isPidAlive(existing_pid):
                return candidate  # Reclaim stale entry
            suffix += 1

    @staticmethod
    def _isPidAlive(pid):
        """Check if a process with the given PID is alive."""
        import os
        if not pid:
            return False
        try:
            os.kill(pid, 0)
            return True
        except (OSError, ProcessLookupError):
            return False

    def _writeEnvoyConfig(self, git_root, port):
        """Register this instance in the .envoy.json instance registry.

        The registry tracks all running Envoy instances so the bridge can
        discover and switch between them.  Atomic writes prevent corruption
        when multiple TD instances write concurrently.

        Format:
            {
                "active": "Embody-5.251",
                "td_executable": "/path/to/TouchDesigner",
                "instances": {
                    "Embody-5.251": {
                        "toe_path": "dev/Embody-5.251.toe",
                        "port": 9870,
                        "td_pid": 12345
                    }
                }
            }
        """
        import os
        import td as _td
        from pathlib import Path

        config_path = git_root / '.envoy.json'

        # Compute toe_path relative to git root
        project_dir = Path(project.folder)
        name = project.name
        toe_file = project_dir / (name if name.endswith('.toe') else name + '.toe')
        try:
            toe_rel = str(toe_file.relative_to(git_root)).replace('\\', '/')
        except ValueError:
            toe_rel = str(toe_file).replace('\\', '/')

        # Derive TD executable path from app.binFolder
        bin_folder = Path(_td.app.binFolder)
        if sys.platform == 'darwin':
            td_executable = str(bin_folder.parent.parent)
        elif sys.platform == 'win32':
            exe = bin_folder / 'TouchDesigner.exe'
            if not exe.exists():
                exe = bin_folder / 'TouchDesigner099.exe'
            td_executable = str(exe).replace('\\', '/')
        else:
            td_executable = str(bin_folder / 'TouchDesigner')

        # Read existing config
        existing = {}
        if config_path.exists():
            try:
                existing = json.loads(
                    config_path.read_text(encoding='utf-8'))
            except (json.JSONDecodeError, OSError):
                pass

        # Migrate old flat format → registry format
        if 'instances' not in existing:
            instances = {}
            if 'toe_path' in existing:
                # Wrap old flat config as a single instance
                old_key = Path(existing['toe_path']).stem
                instances[old_key] = {
                    'toe_path': existing.get('toe_path', ''),
                    'port': existing.get('port', port),
                    'td_pid': existing.get('td_pid', 0),
                }
            existing = {
                'active': existing.get('active', ''),
                'td_executable': existing.get('td_executable', td_executable),
                'instances': instances,
            }

        instances = existing.get('instances', {})
        key = self._instanceKey(toe_rel, instances)

        # Build this instance's entry
        new_entry = {
            'toe_path': toe_rel,
            'port': port,
            'td_pid': os.getpid(),
        }

        # Check if already up-to-date
        if instances.get(key) == new_entry and existing.get('active') == key:
            self._log('.envoy.json already up to date', 'DEBUG')
            return

        instances[key] = new_entry
        existing['instances'] = instances
        existing['active'] = key
        existing['td_executable'] = td_executable

        self._atomicWriteJSON(config_path, existing)
        self._log(f'Registered instance "{key}" in .envoy.json (port {port})')

    def _removeFromRegistry(self, git_root=None):
        """Remove this instance from the .envoy.json registry on shutdown."""
        import os
        from pathlib import Path

        if git_root is None:
            git_root = self.ownerComp.fetch('_git_root', 'no-git')
        if git_root == 'no-git':
            return

        config_path = Path(git_root) / '.envoy.json'
        if not config_path.exists():
            return

        try:
            config = json.loads(config_path.read_text(encoding='utf-8'))
        except (json.JSONDecodeError, OSError):
            return

        instances = config.get('instances', {})
        if not instances:
            return

        # Find our entry by PID
        my_pid = os.getpid()
        my_key = None
        for key, info in instances.items():
            if info.get('td_pid') == my_pid:
                my_key = key
                break

        if my_key is None:
            return

        del instances[my_key]
        config['instances'] = instances

        # If we were active, switch to first remaining instance (or null)
        if config.get('active') == my_key:
            remaining = list(instances.keys())
            config['active'] = remaining[0] if remaining else None

        try:
            self._atomicWriteJSON(config_path, config)
            self._log(f'Deregistered instance "{my_key}" from .envoy.json')
        except Exception as e:
            self._log(f'Could not deregister from .envoy.json: {e}', 'WARNING')

    def _configureGitignore(self, git_root):
        """Ensure .gitignore in the git root contains entries for
        Embody/Envoy auto-generated files.
        Idempotent — only appends missing entries, preserves all existing content.
        Migrates old `.claude/` blanket entry to specific entries."""
        MANAGED_ENTRIES = [
            # TouchDesigner project
            'Backup/',
            'logs/',
            'CrashAutoSave*',
            # Embody / Envoy
            '.venv/',
            '.mcp.json',
            '.envoy.json',
            '.embody.json',
            '.claude/settings.local.json',
            '.claude/projects/',
            '.claude/envoy-bridge.py',
            '__pycache__/',
            '.DS_Store',
        ]

        try:
            gitignore = git_root / '.gitignore'

            existing_content = ''
            existing_lines = []
            if gitignore.exists():
                existing_content = gitignore.read_text(encoding='utf-8')
                existing_lines = existing_content.splitlines()

            # Migrate: remove old blanket `.claude/` entry
            existing_stripped = {line.strip() for line in existing_lines}
            if '.claude/' in existing_stripped:
                existing_lines = [
                    line for line in existing_lines
                    if line.strip() != '.claude/'
                ]
                existing_content = '\n'.join(existing_lines)
                if existing_content and not existing_content.endswith('\n'):
                    existing_content += '\n'
                self._log('Migrated .gitignore: removed blanket .claude/ entry')

            existing_stripped = {line.strip() for line in existing_lines}
            missing = [e for e in MANAGED_ENTRIES if e not in existing_stripped]

            if not missing:
                self._log('.gitignore already configured', 'DEBUG')
                return

            block = '\n# Embody / Envoy (auto-managed)\n'
            block += '\n'.join(missing) + '\n'

            if existing_content and not existing_content.endswith('\n'):
                block = '\n' + block

            gitignore.write_text(existing_content + block, encoding='utf-8')
            self._log(f'Added {len(missing)} entries to .gitignore: {", ".join(missing)}')

        except Exception as e:
            self._log(f'Could not auto-configure .gitignore: {e}', 'WARNING')

    def _configureGitattributes(self, git_root):
        """Ensure .gitattributes normalizes line endings for TD-exported files.
        TouchDesigner writes CRLF on all platforms; this forces LF in git
        so externalized files don't show as dirty after every TD save.
        Idempotent — only writes if the managed block is missing."""
        MANAGED_BLOCK = (
            '\n# Embody / Envoy — normalize TD line endings (auto-managed)\n'
            '*.py text eol=lf\n'
            '*.md text eol=lf\n'
            '*.tdn text eol=lf\n'
            '*.json text eol=lf\n'
            '*.tsv text eol=lf\n'
            '*.xml text eol=lf\n'
            '*.toe binary\n'
            '*.tox binary\n'
        )
        MARKER = 'Embody / Envoy'

        try:
            gitattr = git_root / '.gitattributes'
            existing = ''
            if gitattr.exists():
                existing = gitattr.read_text(encoding='utf-8')

            if MARKER in existing:
                self._log('.gitattributes already configured', 'DEBUG')
                return

            if existing and not existing.endswith('\n'):
                existing += '\n'

            gitattr.write_text(existing + MANAGED_BLOCK, encoding='utf-8')
            self._log('Added line-ending normalization to .gitattributes')

        except Exception as e:
            self._log(f'Could not auto-configure .gitattributes: {e}', 'WARNING')

    def _cleanupTempFiles(self):
        """Remove stale Envoy temp files (captures, offloaded responses) from /tmp.
        Deletes files older than 24 hours matching envoy_* patterns."""
        import glob
        import os

        tmp = tempfile.gettempdir()
        patterns = [os.path.join(tmp, 'envoy_capture_*'),
                    os.path.join(tmp, 'envoy_query_network_*'),
                    os.path.join(tmp, 'envoy_get_op_*')]
        cutoff = time.time() - 86400  # 24 hours ago
        removed = 0
        for pattern in patterns:
            for path in glob.glob(pattern):
                try:
                    if os.path.getmtime(path) < cutoff:
                        os.remove(path)
                        removed += 1
                except OSError:
                    pass
        if removed:
            self._log(f'Cleaned up {removed} stale Envoy temp file(s)', 'DEBUG')

    def _maybe_offload_to_file(self, result: dict, label: str,
                                threshold: int = 50000) -> dict:
        """If the JSON-serialized result exceeds threshold bytes, write it
        to a temp file and return a pointer instead. This prevents MCP
        transport/token-limit issues with very large payloads."""
        import os, uuid
        serialized = json.dumps(result)
        if len(serialized) <= threshold:
            return result
        file_path = os.path.join(tempfile.gettempdir(), f'envoy_{label}_{uuid.uuid4().hex[:8]}.json')
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(serialized)
        return {
            'offloaded': True,
            'file_path': file_path,
            'size_bytes': len(serialized),
            'message': f'Response too large ({len(serialized)} bytes). '
                       f'Full result saved to {file_path}. '
                       f'Use the Read tool to view the file.',
        }

    def _log(self, message: str, level: str = 'INFO'):
        """Log a message via Embody's centralized logger."""
        try:
            op.Embody.Log(message, level, _depth=2)
        except Exception:
            print(f'[Envoy][{level}] {message}')
