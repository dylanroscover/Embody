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

import asyncio
import contextvars
import fnmatch
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from html import unescape
from queue import Queue, Empty
from threading import Lock, Event, Thread
from typing import Optional, Any, Callable

ENVOY_VERSION = "1.4.0"

# Per-request session identity, set by the ASGI middleware from the
# X-Envoy-Session / X-Envoy-Label headers the bridge sends. anyio's
# to_thread.run_sync copies the caller's context into tool threads, so
# tool functions and _execute_in_td read the same values. (None, None)
# for clients that connect without a bridge (direct HTTP).
_SESSION_CTX = contextvars.ContextVar('envoy_session', default=(None, None))

# --- Multi-session Phase 2: touch map + peer advisories ---

# Operations that MUTATE authored state. Only these record touches in the
# shared touch map; read operations still RECEIVE advisories but leave no
# trace themselves.
_WRITE_OPERATIONS = frozenset({
    'create_op', 'delete_op', 'set_parameter', 'connect_ops',
    'disconnect_op', 'copy_op', 'rename_op', 'execute_python',
    'set_dat_content', 'edit_dat_content', 'set_op_flags',
    'set_op_position', 'layout_children', 'externalize_op',
    'remove_externalization_tag', 'save_externalization',
    'create_extension', 'import_network', 'create_annotation',
    'set_annotation', 'run_tests', 'batch_operations',
})

# Coarse scopes for operations whose footprint is not a single op path.
_SPECIAL_SCOPES = {
    'execute_python': 'project:python',
    'run_tests': 'project:tests',
}

_TOUCH_WINDOW_S = 600     # advisories consider touches this recent
_CONFLICT_WINDOW_S = 60   # peer WRITE inside this + own WRITE = conflict
_ADVISORY_DEDUP_S = 300   # same (peer, scope) advisory re-served after this
_TOUCH_RING_CAP = 8       # touches kept per scope
_TOUCH_SCOPE_CAP = 200    # scopes kept (evict oldest-touched beyond this)

_PATH_PARAM_KEYS = ('op_path', 'parent_path', 'source_path', 'dest_path',
                    'target_path', 'comp_path', 'root_path')


def _scope_overlaps(a: str, b: str) -> bool:
    """True when two scopes denote overlapping territory.

    Op-path scopes overlap when one equals the other or is an ancestor,
    segment-aware ('/a/b' vs '/a/bc' do NOT overlap). file:/project:
    scopes match exactly.
    """
    if a == b:
        return True
    if a.startswith('/') and b.startswith('/'):
        shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
        return longer.startswith(shorter + '/')
    return False


def _scopes_for_operation(operation: str, params: dict, result=None) -> list:
    """Scope strings an operation touches: op paths from its params (and
    the created path from its result) plus coarse special scopes. Pure
    inspection -- file-scope expansion happens on the main thread where
    the externalizations table lives.
    """
    scopes = []
    special = _SPECIAL_SCOPES.get(operation)
    if special:
        scopes.append(special)
    params = params or {}
    if operation == 'batch_operations':
        for sub in (params.get('operations') or [])[:16]:
            if isinstance(sub, dict):
                scopes.extend(_scopes_for_operation(
                    sub.get('tool', ''), sub.get('params') or {}))
    else:
        for key in _PATH_PARAM_KEYS:
            value = params.get(key)
            if isinstance(value, str) and value.startswith('/'):
                scopes.append(value)
        if operation == 'rename_op':
            base = params.get('op_path')
            new_name = params.get('new_name')
            if (isinstance(base, str) and isinstance(new_name, str)
                    and '/' in base):
                scopes.append(base.rsplit('/', 1)[0] + '/' + new_name)
        if isinstance(result, dict):
            created = result.get('path')
            if isinstance(created, str) and created.startswith('/'):
                scopes.append(created)
    seen = set()
    deduped = []
    for s in scopes:
        if s not in seen:
            seen.add(s)
            deduped.append(s)
    return deduped[:8]


class EnvoyMCPServer:
    """
    MCP Server that runs in a worker thread.

    IMPORTANT: This class must NOT import or use any TouchDesigner modules.
    All TD operations are delegated to the main thread via queues.
    """

    def __init__(self, request_queue: Optional[Queue], response_queue: Queue,
                 add_to_refresh_queue: Callable[[dict], None], port: int = 9870,
                 shutdown_event: Optional[Event] = None,
                 startup_event: Optional[Event] = None) -> None:
        self.request_queue: Optional[Queue] = request_queue
        self.response_queue: Queue = response_queue
        self.add_to_refresh_queue: Callable[[dict], None] = add_to_refresh_queue
        self.port: int = port
        self.shutdown_event: Event = shutdown_event or Event()
        # Set once uvicorn has actually bound + started serving (H1). The main
        # thread waits on this before declaring the server "Running".
        self.startup_event: Optional[Event] = startup_event
        self.pending_requests: dict[int, dict] = {}
        self.request_counter: int = 0
        self.lock: Lock = Lock()
        self.running: bool = True

        # Session presence registry: sid -> entry dict. Lives on sys so it
        # survives worker recreation across extension reinits / server
        # restarts (same pattern as sys._envoy_queues). Touched from the
        # ASGI middleware (event loop) and tool threads -- guard with
        # _sessions_lock. Pure Python only; never holds TD objects.
        existing_sessions = getattr(sys, '_envoy_sessions', None)
        self._sessions: dict = existing_sessions if isinstance(existing_sessions, dict) else {}
        sys._envoy_sessions = self._sessions
        # One shared lock guards _sessions AND _touches; it lives on sys so
        # the MAIN thread (touch recording, advisory scans) and the worker
        # (registry, get_sessions) coordinate across reinits.
        existing_lock = getattr(sys, '_envoy_sessions_lock', None)
        self._sessions_lock: Lock = existing_lock if existing_lock is not None else Lock()
        sys._envoy_sessions_lock = self._sessions_lock
        # Touch map: scope -> ring of {'sid', 'tool', 'ts'} for recent WRITE
        # operations. Written by the main thread, read by get_sessions.
        existing_touches = getattr(sys, '_envoy_touches', None)
        self._touches: dict = existing_touches if isinstance(existing_touches, dict) else {}
        sys._envoy_touches = self._touches
        # Claim leases (Phase 3): scope -> {'sid','label','note','ts','ttl'}.
        # Cooperative write leases; guarded by the same shared lock.
        existing_claims = getattr(sys, '_envoy_claims', None)
        self._claims: dict = existing_claims if isinstance(existing_claims, dict) else {}
        sys._envoy_claims = self._claims

        self._docs_state = {'resolved': False, 'root': None, 'index': None, 'cache': {}}

        # Import mcp only when server is instantiated (in worker thread)
        from mcp.server.fastmcp import FastMCP, Image
        self._Image = Image  # Store for use in tool functions
        # The MCP SDK auto-enables this for host="127.0.0.1" since about
        # 1.10, but pin it explicitly so a default change cannot silently
        # drop the Host/Origin validation that defeats DNS rebinding/CSRF
        # from a local browser. Idea prompted by TDMCP's 1.1.46 security work.
        transport_security = None
        try:
            from mcp.server.transport_security import TransportSecuritySettings
            transport_security = TransportSecuritySettings(
                enable_dns_rebinding_protection=True,
                allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*"],
                allowed_origins=[
                    "http://127.0.0.1:*",
                    "http://localhost:*",
                    "http://[::1]:*",
                ],
            )
        except Exception as e:
            print(f'[Envoy][WARNING] Transport security settings unavailable; '
                  f'continuing without explicit FastMCP transport_security: {e}')
        mcp_kwargs = {
            'host': "127.0.0.1",
            'port': port,
            'stateless_http': True,
        }
        if transport_security is not None:
            mcp_kwargs['transport_security'] = transport_security
        self.mcp = FastMCP("Envoy", **mcp_kwargs)
        self._register_tools()

    def _touch_session(self, sid: str, label: str = None,
                       operation: str = None) -> None:
        """Register or refresh a session in the presence registry.

        Called from the ASGI middleware on every headered HTTP request and
        from _execute_in_td to attribute the current operation. Worker-side
        pure Python only (mcp-safety thread boundary).
        """
        now = time.time()
        with self._sessions_lock:
            entry = self._sessions.get(sid)
            if entry is None:
                pid = None
                try:
                    pid = int(str(sid).split('-', 1)[0])
                except Exception:
                    pass
                entry = {'sid': sid, 'label': label or sid, 'pid': pid,
                         'first_seen': now, 'requests': 0, 'last_tool': None}
                self._sessions[sid] = entry
            if label:
                entry['label'] = label
            if operation:
                entry['last_tool'] = operation
            else:
                entry['requests'] += 1
            entry['last_seen'] = now
            # Lazy prune: drop sessions silent for over an hour.
            if len(self._sessions) > 8:
                for stale_sid in [k for k, v in self._sessions.items()
                                  if now - v.get('last_seen', 0) > 3600]:
                    del self._sessions[stale_sid]

    def _sessions_snapshot(self) -> dict:
        """Presence list for get_sessions. Worker-side pure Python."""
        now = time.time()
        with self._sessions_lock:
            self._prune_claims_locked(now)
            sessions = [dict(v) for v in self._sessions.values()]
            claims_by = {}
            for held_scope, claim in self._claims.items():
                claims_by.setdefault(claim['sid'], []).append({
                    'scope': held_scope,
                    'note': claim.get('note', ''),
                    'expires_in_s': round(claim['ts'] + claim['ttl'] - now, 1)})
            touched_by = {}
            for scope, ring in self._touches.items():
                for touch in ring:
                    touched_by.setdefault(touch['sid'], []).append(
                        (touch['ts'], scope, touch['tool']))
        for e in sessions:
            idle = now - e.get('last_seen', now)
            e['idle_s'] = round(idle, 1)
            e['stale'] = idle > 90
            recent = sorted(touched_by.get(e['sid'], []), reverse=True)[:5]
            if recent:
                e['recent_scopes'] = [
                    {'scope': scope, 'tool': tool, 'age_s': round(now - ts, 1)}
                    for ts, scope, tool in recent]
            held = claims_by.get(e['sid'])
            if held:
                e['claims'] = held
        sessions.sort(key=lambda e: e.get('last_seen', 0), reverse=True)
        return {'sessions': sessions, 'count': len(sessions)}

    def _prune_claims_locked(self, now):
        """Drop expired claims and claims whose holder went silent for 10
        minutes. Caller holds _sessions_lock."""
        for held_scope in list(self._claims):
            claim = self._claims[held_scope]
            holder = self._sessions.get(claim['sid'])
            holder_seen = holder.get('last_seen', 0) if holder else 0
            # '_anon' holders (headerless clients) are never in the
            # registry -- for them only the TTL applies, or their claims
            # would evaporate on the next prune.
            holder_silent = (claim['sid'] != '_anon'
                             and now - holder_seen > 600)
            if now > claim['ts'] + claim['ttl'] or holder_silent:
                del self._claims[held_scope]

    def _claim_scope(self, sid, label, scope, note, ttl):
        """Grant/refuse a cooperative write lease. Worker-side pure Python."""
        scope = (scope or '').strip()
        if not (scope.startswith('/') or scope.startswith('file:')
                or scope.startswith('project:')):
            return {'error': "scope must be an op path ('/comp/op'), "
                             "'file:<repo-relative-path>', or 'project:<name>'"}
        if scope.startswith('/') and len(scope) > 1:
            scope = scope.rstrip('/')
        try:
            ttl = max(30, min(3600, int(ttl)))
        except Exception:
            ttl = 300
        me = sid or '_anon'
        now = time.time()
        with self._sessions_lock:
            self._prune_claims_locked(now)
            for held_scope, claim in self._claims.items():
                if claim['sid'] == me:
                    continue
                if _scope_overlaps(scope, held_scope):
                    return {'granted': False,
                            'holder': {
                                'label': claim.get('label') or claim['sid'],
                                'scope': held_scope,
                                'note': claim.get('note', ''),
                                'age_s': round(now - claim['ts'], 1),
                                'expires_in_s': round(
                                    claim['ts'] + claim['ttl'] - now, 1)},
                            'hint': 'Coordinate with the holder, work in a '
                                    'different subtree, or wait for expiry.'}
            self._claims[scope] = {'sid': me, 'label': label or me,
                                   'note': (note or '')[:200],
                                   'ts': now, 'ttl': ttl}
            if len(self._claims) > 64:
                oldest_first = sorted(self._claims.items(),
                                      key=lambda kv: kv[1]['ts'])
                for stale_scope, _claim in oldest_first[:len(self._claims) - 64]:
                    del self._claims[stale_scope]
        return {'granted': True, 'scope': scope, 'ttl': ttl,
                'renewal': 'your own tool calls touching this scope renew '
                           'the lease; it expires on TTL or session silence'}

    def _release_scope(self, sid, scope):
        """Release a lease held by this session. Worker-side pure Python."""
        me = sid or '_anon'
        scope = (scope or '').strip()
        if scope.startswith('/') and len(scope) > 1:
            scope = scope.rstrip('/')
        with self._sessions_lock:
            claim = self._claims.get(scope)
            if claim is None:
                return {'released': False, 'reason': 'no claim on that scope'}
            if claim['sid'] != me:
                return {'released': False,
                        'reason': 'held by another session',
                        'holder': claim.get('label') or claim['sid']}
            del self._claims[scope]
        return {'released': True, 'scope': scope}

    def _execute_in_td(self, operation: str, params: dict,
                       timeout: float = 30.0) -> dict:
        """Queue operation to main thread and wait for response"""
        with self.lock:
            request_id = self.request_counter
            self.request_counter += 1
            event = Event()
            self.pending_requests[request_id] = {'event': event, 'result': None}

        # Attribute the operation to the calling session (if the request
        # arrived through a bridge that sent identity headers).
        sid, _label = _SESSION_CTX.get()
        if sid:
            self._touch_session(sid, operation=operation)

        # Queue request to main thread via Thread Manager's refresh queue
        self.add_to_refresh_queue({
            'id': request_id,
            'operation': operation,
            'params': params,
            'sid': sid
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

    def check_responses(self, first_response: dict = None) -> None:
        """Check for responses from main thread"""
        def process_response(response):
            request_id = response['id']
            with self.lock:
                pending = self.pending_requests.get(request_id)
            if pending is not None:
                pending['result'] = response['result']
                pending['event'].set()
            else:
                # Orphaned response -- request already timed out and was removed
                print(f'[Envoy][WARNING] Orphaned response for request {request_id} '
                      f'(likely timed out). Operation still executed on main thread.')

        if first_response is not None:
            process_response(first_response)

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
            process_response(response)

    def _register_tools(self):
        """Register all MCP tools"""

        @self.mcp.tool()
        def create_op(parent_path: str, op_type: str, name: str = None) -> dict:
            """
            Create a new operator in TouchDesigner.

            Auto-positions the new op clear of siblings, and snaps any docked
            companions it spawns (callback/shader/info DATs) into a tight row
            hugging the host's bottom edge (docks_placed in the result).

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
        def delete_op(op_path: str, override: bool = False) -> dict:
            """
            Delete an operator.

            Args:
                op_path: Full path to the operator (e.g., "/project1/base1")
                override: Bypass the multi-session gate when another live
                    session claimed this scope or wrote it very recently.

            Returns:
                Dict with success status
            """
            return self._execute_in_td('delete_op', {'op_path': op_path,
                                                     'override': override})

        @self.mcp.tool()
        def get_op(op_path: str, include_defaults: bool = False) -> dict:
            """
            Get operator info; parameters are non-default only by default.

            Args:
                op_path: Full path to the operator
                include_defaults: True returns all parameters

            Returns:
                Dict with type, family, parameters, inputs, outputs, children
            """
            return self._execute_in_td('get_op', {
                'op_path': op_path,
                'include_defaults': include_defaults,
            })

        @self.mcp.tool()
        def set_parameter(op_path: str, par_name: str, value: str = None,
                         mode: str = None, expr: str = None,
                         bind_expr: str = None) -> dict:
            """
            Set a parameter value, expression, bind expression, or mode on an operator.

            Invalid Menu values are rejected with the valid menuNames because
            TD would otherwise silently coerce them to index 0. Sequence-block
            parameters auto-grow their sequence, e.g. const5name grows
            numBlocks to 6.

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
        def get_parameter(op_path: str, par_name: str = None,
                         search: str = None, search_in: str = 'any',
                         depth: int = 2, max_results: int = 50,
                         details: bool = False) -> dict:
            """
            Read one parameter compactly, or search parameters by glob/substring.

            Single-parameter mode is compact by default; details=True restores
            full metadata. Search mode ignores details. search_in='value'
            evaluates scanned values; search_in='any' evaluates constants only.

            Args:
                op_path: Full path to the operator
                par_name: Parameter name for single-parameter mode
                search: Glob or substring pattern for search mode
                search_in: Field to search: name, value, expr, or any
                depth: Child search depth
                max_results: Maximum search hits to return
                details: True returns full metadata in single-parameter mode

            Returns:
                Compact parameter info or search hits
            """
            return self._execute_in_td('get_parameter', {
                'op_path': op_path,
                'par_name': par_name,
                'search': search,
                'search_in': search_in,
                'depth': depth,
                'max_results': max_results,
                'details': details,
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
            List operators in a network/container.

            Args:
                parent_path: Path to parent COMP to search in (default "/")
                recursive: If True, search recursively into child COMPs
                op_type: Filter by operator type (e.g., "baseCOMP", "TOP", "annotateCOMP")
                include_utility: If True, include utility operators like annotations (default False)

            Returns:
                Dict with operator path/type/family/depth; name = last path segment
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

            Auto-positions the copy clear of siblings and re-hugs its docked
            companions below it (docks_placed in the result).

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
            Useful for debugging TD networks -- returns both errors and warnings.

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

        @self.mcp.tool()
        def get_docs(query: str, section: str = None, source: str = 'auto',
                     max_chars: int = 20000) -> dict:
            """Look up official TouchDesigner documentation (docs.derivative.ca).

            Resolves operator pages ("Movie File In TOP", "moviefileinTOP"), Python
            class pages ("CHOP Class"), and concept articles. Prefers the local
            offline help mirror when the TD installation has one (version-exact,
            instant); falls back to the live docs.derivative.ca wiki API.

            Args:
                query: Page name or topic (e.g. "noiseTOP", "Timer CHOP", "Instancing")
                section: Optional section heading from a previous call's
                    sections_available -- returns just that section
                source: 'auto' (offline then web), 'offline', or 'web'
                max_chars: Truncate content to this many characters (default 20000)

            Returns:
                Dict with title, source, sections_available, content (markdown-ish
                text), and optional url / matches / truncated fields.
            """
            return self._get_docs(query, section, source, max_chars)

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
                           rows: list = None, clear: bool = False,
                           confirm_wipe: bool = False) -> dict:
            """
            Replace a DAT's entire text or table content.

            Refuses no-content calls and any wipe unless confirm_wipe=True.

            Args:
                op_path: Path to the DAT operator
                text: Full text replacement; text="" is a wipe
                rows: Full table replacement; rows=[] is a wipe
                clear: Empty the DAT; redundant when text/rows is provided
                confirm_wipe: Required when the result would be empty

            Returns:
                Dict with success status, or {'error': ...} if a guard trips
            """
            return self._execute_in_td('set_dat_content', {
                'op_path': op_path,
                'text': text,
                'rows': rows,
                'clear': clear,
                'confirm_wipe': confirm_wipe,
            })

        @self.mcp.tool()
        def edit_dat_content(op_path: str, old_string: str,
                             new_string: str, replace_all: bool = False,
                             confirm_wipe: bool = False) -> dict:
            """
            Replace text in a DAT without sending the whole DAT.

            Text DATs only. old_string must appear exactly once unless
            replace_all=True. Refuses wipes without confirm_wipe=True.

            Args:
                op_path: Path to the DAT operator
                old_string: Text to find; non-empty and unique by default
                new_string: Replacement text; must differ from old_string
                replace_all: True replaces every occurrence
                confirm_wipe: Required when the edit would leave the DAT empty

            Returns:
                Dict with success, path, replacements, numRows, and numCols
            """
            return self._execute_in_td('edit_dat_content', {
                'op_path': op_path,
                'old_string': old_string,
                'new_string': new_string,
                'replace_all': replace_all,
                'confirm_wipe': confirm_wipe,
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
            Set one or more flags/properties on an operator.

            Args:
                op_path: Path to the operator
                bypass: Bypass flag
                lock: Lock flag
                display: Display flag
                render: Render flag
                viewer: Viewer flag
                current: Current flag (yellow flag)
                expose: Expose flag
                allowCooking: Allow cooking flag
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
            Get compact positions and sizes for all children in a COMP.

            Args:
                comp_path: Path to the parent COMP
                include_annotations: Whether to include annotation positions (default True)

            Returns:
                Dict with operator path/type/nodeX/nodeY/nodeWidth/nodeHeight;
                centers are nodeX+nodeWidth/2 and nodeY+nodeHeight/2. Docked
                companions carry dockedTo (their host's name) so the Verify
                step can confirm every dock hugs its host.
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

            Moving an op carries its docked companions along: they are re-hugged
            in a tight row below the new position (docks_moved in the result).
            Position the host FIRST if you also plan to place a dock explicitly.

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
            Create a Comment, Network Box, or Annotate in the network editor.

            Args:
                parent_path: Path to parent COMP
                mode: "annotate" (default), "comment", or "networkbox"
                text: Body text content
                title: Title bar text
                x: X position in the network editor
                y: Y position in the network editor
                width: Width of the annotation
                height: Height of the annotation
                color: Background color as [r, g, b] floats
                opacity: Opacity from 0.0 to 1.0
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
            Create or attach a TouchDesigner extension COMP and code DAT.

            Args:
                parent_path: Parent COMP path, or target COMP when existing_comp=True
                class_name: Python class name
                name: New COMP name; ignored when existing_comp=True
                code: Full class code; omitted generates boilerplate
                promote: Promote capitalized methods to COMP level
                ext_name: Custom extension name
                ext_index: Extension slot 0-3; omitted auto-detects
                existing_comp: True attaches to parent_path instead of creating

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
                          clear_first: bool = False,
                          override: bool = False) -> dict:
            """
            Import a .tdn network into a TouchDesigner COMP, recreating all operators.

            Args:
                target_path: Destination COMP path to import into
                tdn: The .tdn JSON document (full document or just the operators array)
                clear_first: If True, delete all existing children before importing
                override: Bypass the multi-session gate when another live
                    session claimed this COMP or wrote it very recently
                    (applies only with clear_first=True)

            Returns:
                Dict with import results and created operator paths
            """
            return self._execute_in_td('import_network', {
                'target_path': target_path,
                'tdn': tdn,
                'clear_first': clear_first,
                'override': override,
            })

        @self.mcp.tool()
        def read_tdn(comp_path: str = "/",
                     include_dat_content: bool = None,
                     max_depth: int = None,
                     embed_all: bool = False) -> dict:
            """
            Read live authored state under comp_path as a compact TDN dict.

            This is authored-state, not runtime: use runtime probes for
            evaluated values, cook errors, output pixels/data, timing, or flags.

            Args:
                comp_path: Root COMP to read (default "/" for entire project)
                include_dat_content: Include DAT text/table content
                max_depth: Maximum recursion depth (None = unlimited)
                embed_all: Recurse into TDN-tagged COMPs instead of skipping

            Returns:
                Dict with the TDN document under 'tdn', or {'error': ...}
            """
            return self._execute_in_td('read_tdn', {
                'comp_path': comp_path,
                'include_dat_content': include_dat_content,
                'max_depth': max_depth,
                'embed_all': embed_all,
            })
        @self.mcp.tool()
        def diff_tdn(target: str = "",
                     max_changed_ops: int = 200,
                     max_bytes: int = 60000) -> dict:
            """Diff live in-memory TDN state against on-disk .tdn files.

            Empty target (or "/" / "project") returns a project summary; a
            COMP path or .tdn filename returns that COMP in detail. Read-only.

            Args:
                target: Empty for whole project, else COMP path or .tdn file
                max_changed_ops: Cap reported changed operators
                max_bytes: Soft response cap; changed_keys remain when trimmed

            Returns:
                Diff envelope, project summary, or {'error': ...}
            """
            return self._execute_in_td('diff_tdn', {
                'target': target,
                'max_changed_ops': max_changed_ops,
                'max_bytes': max_bytes,
            })


        # === TOP Capture ===

        @self.mcp.tool()
        def capture_top(op_path: str, format: str = "jpeg", quality: float = 0.8,
                        max_resolution: int = 640, inline: bool = False,
                        sample_grid: int = 0) -> list:
            """
            Capture a TOP as a temp image file or sampled RGBA grid.

            File path is returned by default; inline=True embeds a small preview.
            sample_grid>=2 returns an NxN RGBA grid instead, clamped 2..32 with
            row 0 at image top-left; image format args are ignored.

            Args:
                op_path: Path to a TOP operator
                format: "jpeg" or "png"
                quality: JPEG compression quality 0.0-1.0
                max_resolution: Max pixels on longest edge; 0 = native
                inline: True embeds a small base64 preview
                sample_grid: >=2 returns an RGBA sample grid

            Returns:
                Saved path text, inline image content, or sample-grid dict
            """
            import base64
            import os
            import uuid

            try:
                sample_grid_value = int(sample_grid or 0)
            except Exception:
                sample_grid_value = 0

            result = self._execute_in_td('capture_top', {
                'op_path': op_path,
                'format': format,
                'quality': quality,
                'max_resolution': max_resolution,
                'sample_grid': sample_grid_value,
            })

            if 'error' in result:
                return result

            if sample_grid_value >= 2:
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
                    f" -> {result['width']}x{result['height']} {result['format'].upper()}"
                    f" ({size_kb:.1f} KB)\nSaved to: {file_path}")

            # Inline base64 images are token-heavy, so only embed when the caller
            # explicitly asks (inline=True) and the image is small. By default
            # return just the path; Read the file when actually judging a frame.
            if inline and result['size_bytes'] < 20000:
                return [info, self._Image(data=image_bytes, format=result['format'])]
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
        def get_sessions() -> dict:
            """
            List AI client sessions currently connected to this Envoy server.

            Each session is one AI client window (e.g. one Claude Code
            session) connected through its own bridge process. Check this
            at session start and before large or destructive operations
            (import_network with clear_first, delete_op on a COMP, project
            save, test runs) so concurrent sessions don't clobber each
            other's work.

            Returns:
                Dict with 'sessions' (newest-activity first: sid, label,
                pid, first_seen, last_seen, idle_s, requests, last_tool,
                recent_scopes = op paths/files this session recently
                modified, claims = scopes this session holds via
                claim_scope, stale = no traffic for >90s), 'count', and 'you'
                (the caller's own sid, or null for clients that connect
                without a bridge). Sessions silent for over an hour are
                dropped. Responses to ANY tool also carry a '_peers'
                advisory list automatically when your request overlaps
                territory another session touched recently; an entry with
                conflict=true means a peer WROTE there within the last
                minute -- stop and coordinate before proceeding.
            """
            # Answered on the worker thread from pure-Python state -- no
            # TD access, so no main-thread round-trip (mcp-safety).
            snapshot = self._sessions_snapshot()
            sid, _label = _SESSION_CTX.get()
            snapshot['you'] = sid
            return snapshot

        @self.mcp.tool()
        def claim_scope(scope: str, note: str = "", ttl: int = 300) -> dict:
            """
            Claim a cooperative write lease so peer sessions avoid a scope.

            Overlapping claims and destructive ops are refused while the lease
            is live; own writes renew it and silence/TTL expires it.

            Args:
                scope: Op path prefix, file:<repo-relative>, or project:<name>
                note: Short intent shown to peers
                ttl: Lease seconds, 30-3600 (default 300).

            Returns:
                {'granted': True, ...} or {'granted': False, 'holder': {...}}
            """
            # Worker-side pure Python -- no TD access (mcp-safety).
            sid, label = _SESSION_CTX.get()
            return self._claim_scope(sid, label, scope, note, ttl)

        @self.mcp.tool()
        def release_scope(scope: str) -> dict:
            """
            Release a scope you claimed with claim_scope.

            Args:
                scope: The exact scope string you claimed.

            Returns:
                {'released': bool} plus a reason when not released.
            """
            sid, _label = _SESSION_CTX.get()
            return self._release_scope(sid, scope)

        @self.mcp.tool()
        def run_tests(suite_name: str = None, test_name: str = None,
                      override: bool = False) -> dict:
            """
            Run Embody test suites and return results.

            Args:
                suite_name: Run only this suite (e.g., "test_path_utils"). Omit to run all.
                test_name: Run only this test method within the suite.
                override: Bypass the multi-session gate when another live
                    session holds project:tests or wrote very recently.

            Returns:
                Dict with passed/failed/error/skip counts and full results list
            """
            # Use a dedicated Event so the worker thread can wait directly
            # for test completion -- bypasses the response_queue which is
            # fragile against server restarts / extension reinit.
            test_event = Event()
            test_holder: dict = {}
            sys._envoy_pending_test = {
                'event': test_event,
                'holder': test_holder,
            }

            # Queue the start request (main thread will run deferred tests)
            self.add_to_refresh_queue({
                'id': -1,  # Sentinel -- no normal response expected
                'operation': 'run_tests',
                'params': {'suite_name': suite_name, 'test_name': test_name,
                           'override': override},
                'sid': _SESSION_CTX.get()[0],
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
                # Server shutting down -- unblock cleanly
                sys._envoy_pending_test = None
                return {'error': 'Server shutting down during test run'}

            result = test_holder.get('result', {'error': 'No result'})
            sys._envoy_pending_test = None
            return result

        # --- Batch Operations ---

        @self.mcp.tool()
        def batch_operations(operations: list, override: bool = False) -> dict:
            """
            Execute multiple operations in a single request.

            Combines several tool calls into one round-trip, reducing latency
            and token overhead. Stops on first error by default.

            Args:
                operations: List of dicts, each with 'tool' (str) and 'params' (dict).
                    Example: [{"tool": "set_op_position", "params": {"op_path": "/project1/noise1", "x": 400}},
                              {"tool": "connect_ops", "params": {"source_path": "/project1/noise1", "dest_path": "/project1/null1"}}]

            Returns:
                Dict with 'results' (list in same order), 'count', and 'success' (false if any failed)
            """
            return self._execute_in_td('batch_operations', {
                'operations': operations,
                'override': override,
            })

    # === get_docs: official TD documentation lookup ===
    # Design adapted from Derivative's TDMCP get_docs, with permission.

    def _get_docs(self, query, section, source, max_chars) -> dict:
        try:
            query = (query or '').strip()
            if not query:
                return {'error': 'Provide query'}
            section = (section or '').strip() or None
            source = (source or 'auto').strip().lower()
            if source not in ('auto', 'offline', 'web'):
                return {'error': 'Invalid source. Use: auto, offline, web'}
            try:
                max_chars = int(max_chars)
            except Exception:
                max_chars = 20000
            max_chars = max(1, max_chars)

            cache_key = (query.lower(), section.lower() if section else None,
                         source, int(max_chars))
            cache = self._docs_state['cache']
            if cache_key in cache:
                return cache[cache_key]

            doc = None
            offline_reason = None
            web_reason = None

            if source in ('auto', 'offline'):
                try:
                    doc = self._docsOffline(query)
                    if doc is None:
                        offline_reason = 'offline mirror missing or no match'
                except Exception as e:
                    offline_reason = f'offline lookup failed: {e}'
                    doc = None
                if isinstance(doc, dict) and doc.get('matches'):
                    result = {'source': 'offline', 'matches': doc['matches']}
                    cache[cache_key] = result
                    if len(cache) > 20:
                        cache.pop(next(iter(cache)))
                    return result

            if doc is None and source in ('auto', 'web'):
                try:
                    doc = self._docsWeb(query)
                    if doc is None:
                        web_reason = 'web lookup found no match'
                    elif isinstance(doc, dict) and doc.get('error'):
                        web_reason = doc['error']
                        doc = None
                except Exception as e:
                    web_reason = f'web lookup failed: {e}'
                    doc = None

            if doc is None:
                tried = []
                if source in ('auto', 'offline'):
                    tried.append(offline_reason or 'offline mirror missing or no match')
                if source in ('auto', 'web'):
                    tried.append(web_reason or 'web lookup found no match')
                return {'error': 'Documentation lookup failed: ' + '; '.join(tried)}

            sections_available, sections = self._docsSplitSections(doc.get('text') or '')
            content = doc.get('text') or ''
            if section:
                wanted = section.lower()
                title = None
                for candidate in sections_available:
                    if candidate.lower() == wanted:
                        title = candidate
                        break
                if title is None:
                    for candidate in sections_available:
                        if candidate.lower().startswith(wanted):
                            title = candidate
                            break
                if title is None:
                    return {
                        'error': f'Section not found: {section}',
                        'sections_available': sections_available,
                    }
                content = sections.get(title.lower(), '')

            truncated = len(content) > max_chars
            if truncated:
                content = content[:max_chars]
            result = {
                'title': doc.get('title'),
                'source': doc.get('source'),
                'sections_available': sections_available,
                'content': content,
            }
            if doc.get('url'):
                result['url'] = doc['url']
            if truncated:
                result['truncated'] = True
            cache[cache_key] = result
            if len(cache) > 20:
                cache.pop(next(iter(cache)))
            return result
        except Exception as e:
            return {'error': f'Documentation lookup failed: {e}'}

    def _docsOfflineRoot(self):
        if self._docs_state['resolved']:
            return self._docs_state['root']
        root_path = None
        result = self._execute_in_td('get_docs_roots', {})
        if not (isinstance(result, dict) and 'roots' in result
                and not result.get('error')):
            return None
        try:
            for candidate in result.get('roots', []):
                if os.path.isdir(candidate):
                    root_path = candidate
                    break
        except Exception:
            root_path = None
        self._docs_state['resolved'] = True
        self._docs_state['root'] = root_path
        return root_path

    def _docsOffline(self, query):
        root_path = self._docsOfflineRoot()
        if root_path is None:
            return None
        if self._docs_state['index'] is None:
            index = {}
            for filename in os.listdir(root_path):
                if not filename.lower().endswith(('.htm', '.html')):
                    continue
                stem = os.path.splitext(filename)[0]
                key = self._docsNormalize(stem)
                if key and key not in index:
                    index[key] = filename
            self._docs_state['index'] = index

        index = self._docs_state['index']
        key = self._docsNormalize(query)
        if not key:
            return None

        def read_doc(filename):
            path = os.path.join(root_path, filename)
            with open(path, 'r', encoding='utf-8', errors='replace') as f:
                html_src = f.read()
            stem = os.path.splitext(filename)[0]
            return {
                'title': stem.replace('_', ' '),
                'source': 'offline',
                'text': self._docsHtmlToText(html_src),
            }

        if key in index:
            return read_doc(index[key])

        candidates = [filename for index_key, filename in index.items()
                      if key in index_key or index_key in key]
        if len(candidates) == 1:
            return read_doc(candidates[0])
        if 2 <= len(candidates) <= 8:
            return {
                'source': 'offline',
                'matches': [
                    os.path.splitext(filename)[0].replace('_', ' ')
                    for filename in candidates
                ],
            }
        return None

    def _docsWeb(self, query):
        try:
            headers = {'User-Agent': 'Embody-Envoy-get_docs'}
            search_params = urllib.parse.urlencode({
                'action': 'query',
                'list': 'search',
                'format': 'json',
                'srlimit': 5,
                'srsearch': query,
            })
            search_url = 'https://docs.derivative.ca/api.php?' + search_params
            request = urllib.request.Request(search_url, headers=headers)
            with urllib.request.urlopen(request, timeout=8) as response:
                data = json.loads(response.read().decode('utf-8', errors='replace'))
            results = data.get('query', {}).get('search', [])
            if not results:
                return {'error': 'web lookup found no match'}
            title = results[0].get('title')
            if not title:
                return {'error': 'web lookup returned no title'}

            parse_params = urllib.parse.urlencode({
                'action': 'parse',
                'format': 'json',
                'prop': 'text',
                'page': title,
            })
            parse_url = 'https://docs.derivative.ca/api.php?' + parse_params
            request = urllib.request.Request(parse_url, headers=headers)
            with urllib.request.urlopen(request, timeout=8) as response:
                data = json.loads(response.read().decode('utf-8', errors='replace'))
            html_src = data.get('parse', {}).get('text', {}).get('*')
            if html_src is None:
                return {'error': 'web lookup returned no page content'}
            return {
                'title': title,
                'source': 'web',
                'url': f'https://docs.derivative.ca/{title.replace(" ", "_")}',
                'text': self._docsHtmlToText(html_src),
            }
        except Exception as e:
            return {'error': f'web lookup failed: {e}'}

    @staticmethod
    def _docsNormalize(name: str) -> str:
        return re.sub(r'[^a-z0-9]', '', (name or '').lower())

    @staticmethod
    def _docsHtmlToText(html_src: str) -> str:
        text = html_src or ''
        text = re.sub(r'(?is)<(script|style)[^>]*>.*?</\1>', '', text)
        text = re.sub(r'(?i)<h([1-4])[^>]*>',
                      lambda m: '\n' + ('#' * int(m.group(1))) + ' ', text)
        text = re.sub(r'(?i)</h[1-4]>', '\n', text)
        text = re.sub(r'(?i)<li[^>]*>', '\n- ', text)
        text = re.sub(r'(?i)</li>', '\n', text)
        text = re.sub(
            r'(?i)</?(p|div|tr|br|table|tbody|thead|tfoot|td|th|ul|ol)[^>]*>',
            '\n',
            text,
        )
        text = re.sub(r'(?s)<[^>]+>', '', text)
        text = unescape(text)
        text = text.replace('\ufeff', '').replace('[edit]', '')
        text = re.sub(r'[ \t\r\f\v]+', ' ', text)
        text = re.sub(r' *\n *', '\n', text)
        # MediaWiki boilerplate, removed AFTER whitespace normalization so the
        # line anchors see trimmed lines: the nav skeleton renders as bare '-'
        # / 'Jump to ...' lines, and nested headline spans strand '#' markers
        # on their own line -- regluing them keeps sections drill-down-able.
        text = re.sub(r'(?m)^(?:-|Jump to navigation|Jump to search)$\n?', '', text)
        text = re.sub(r'(?m)^(#{1,6})\n+(?=\S)', r'\1 ', text)
        # The mirror's page footer (Personal tools / Namespaces / Views / ...)
        # starts at a '## Personal tools' heading -- nothing after it is page
        # content, so cut there rather than blocklist each footer heading.
        cut = re.search(r'(?m)^#{1,6} Personal tools$', text)
        if cut:
            text = text[:cut.start()]
        text = re.sub(r'\n{3,}', '\n\n', text)
        return text.strip()

    @staticmethod
    def _docsSplitSections(text: str):
        sections_available = []
        buffers = {'': []}
        current = ''
        for line in (text or '').splitlines():
            match = re.match(r'^(#{1,6})\s+(.+?)\s*$', line)
            if match:
                current = match.group(2).strip()
                # MediaWiki chrome headings still bucket their text away from
                # real sections, but are not offered for section= drill-down.
                if current.lower() not in ('contents', 'navigation menu'):
                    sections_available.append(current)
                buffers.setdefault(current.lower(), []).append(line)
            else:
                buffers.setdefault(current.lower(), []).append(line)
        sections = {key: '\n'.join(lines).strip()
                    for key, lines in buffers.items()}
        return sections_available, sections

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
        # still try to send_log_message -> ClosedResourceError. This is cosmetic --
        # the server recovers immediately. Filter these out instead of escalating
        # the log level (which would hide real errors).
        import anyio

        from starlette.requests import ClientDisconnect as _CD

        class _DisconnectCrashFilter(logging.Filter):
            def filter(self, record):
                if record.exc_info and record.exc_info[1]:
                    exc = record.exc_info[1]
                    if self._is_disconnect(exc):
                        return False
                # Also suppress the "Error handling POST request" messages
                # that contain ClientDisconnect in the message text
                msg = record.getMessage() if hasattr(record, 'getMessage') else ''
                if 'ClientDisconnect' in msg:
                    return False
                return True

            @staticmethod
            def _is_disconnect(exc):
                if isinstance(exc, (anyio.BrokenResourceError,
                                    anyio.ClosedResourceError, _CD)):
                    return True
                if isinstance(exc, BaseExceptionGroup):
                    return all(_DisconnectCrashFilter._is_disconnect(e)
                              for e in exc.exceptions)
                return False

        logging.getLogger("mcp.server.streamable_http_manager").addFilter(
            _DisconnectCrashFilter()
        )
        logging.getLogger("mcp.server.streamable_http").addFilter(
            _DisconnectCrashFilter()
        )

        # Drop the per-request "Processing request of type X" log lines from
        # FastMCP's lowlevel server.  The bridge's background reconciler
        # pings the backend every few seconds, which would otherwise flood
        # TD's textport with one "Processing request of type PingRequest"
        # line per ping.  These messages have zero diagnostic value at
        # runtime -- real errors come through different log paths -- so we
        # filter them out instead of raising the logger level (which would
        # also drop legitimate warnings).
        class _RequestProcessingFilter(logging.Filter):
            def filter(self, record):
                try:
                    msg = record.getMessage()
                except Exception:
                    return True
                if msg.startswith("Processing request of type "):
                    return False
                # "Received exception from stream: " with empty or whitespace-
                # only payload = bridge recycled a connection.  Not actionable.
                if msg.startswith("Received exception from stream:"):
                    payload = msg[len("Received exception from stream:"):].strip()
                    if not payload:
                        return False
                return True

        logging.getLogger("mcp.server.lowlevel.server").addFilter(
            _RequestProcessingFilter()
        )

        # Response checker is pure Python (no TD objects), so a plain thread is fine
        def response_checker():
            while self.running and not self.shutdown_event.is_set():
                try:
                    # Was a 10ms poll adding ~5ms mean latency per call (audit finding).
                    response = self.response_queue.get(timeout=0.25)
                    self.check_responses(response)
                except Exception as e:
                    try:
                        expected = isinstance(e, Empty)
                    except NameError:
                        expected = type(e).__name__ == 'Empty'
                    if expected:
                        continue
                    if not self.shutdown_event.is_set():
                        print(f'[Envoy][WARNING] response_checker exiting: {e}')
                    break

        Thread(target=response_checker, daemon=True).start()

        # Manage uvicorn directly so we can signal shutdown via shutdown_event
        starlette_app = self.mcp.streamable_http_app()

        # Wrap the ASGI app to suppress client disconnect noise.
        # During extension reinit or tab close, in-flight connections raise
        # BrokenResourceError (anyio), ClosedResourceError (anyio), or
        # ClientDisconnect (starlette).  All are harmless -- the server
        # recovers on restart.  Without suppression, the flood of tracebacks
        # can destabilize uvicorn's event loop.
        from starlette.requests import ClientDisconnect

        def _is_client_disconnect(exc):
            if isinstance(exc, (anyio.BrokenResourceError,
                                anyio.ClosedResourceError,
                                ClientDisconnect)):
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

        # Session identity capture: read the bridge's X-Envoy-Session /
        # X-Envoy-Label headers, update the presence registry, and stash
        # the identity in _SESSION_CTX for the duration of the request so
        # tool functions can attribute their work. Pure Python only --
        # never touches TD objects (mcp-safety thread boundary).
        worker = self

        class _SessionCapture:
            def __init__(self, app):
                self.app = app

            async def __call__(self, scope, receive, send):
                sid = label = None
                if scope.get('type') == 'http':
                    try:
                        hdrs = {k.decode('latin-1').lower(): v.decode('latin-1')
                                for k, v in (scope.get('headers') or [])}
                        sid = hdrs.get('x-envoy-session') or None
                        label = hdrs.get('x-envoy-label') or None
                    except Exception:
                        sid = label = None
                    if sid:
                        try:
                            worker._touch_session(sid, label)
                        except Exception:
                            pass
                token = _SESSION_CTX.set((sid, label))
                try:
                    await self.app(scope, receive, send)
                finally:
                    _SESSION_CTX.reset(token)

        starlette_app = _SuppressDisconnect(_SessionCapture(starlette_app))

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

        # H1: signal the main thread once uvicorn has ACTUALLY bound and begun
        # serving.  uvicorn.Server.started flips True only after the listener
        # socket is bound and lifespan startup completes -- the only honest
        # "Running" signal.  Without this the main thread declared Running the
        # instant the task was enqueued (zombie status over a dead socket).
        def startup_monitor():
            import time as _t
            while not self.shutdown_event.is_set():
                if getattr(uvi_server, 'started', False):
                    if self.startup_event is not None:
                        self.startup_event.set()
                    return
                _t.sleep(0.05)

        if self.startup_event is not None:
            Thread(target=startup_monitor, daemon=True).start()

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
            # Clear the global handle so the next Start does not mistake
            # this exited server for a live one that needs draining --
            # only clear if it is still pointing at OUR instance (a newer
            # Start may have replaced it already).
            if getattr(sys, '_envoy_uvi_server', None) is uvi_server:
                sys._envoy_uvi_server = None
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
        # Inherit queues from previous instance so pending requests survive
        # extension reinit during save cycles.  Queue is thread-safe.
        _prev_queues = getattr(sys, '_envoy_queues', {}).get(ownerComp.path)
        if _prev_queues is not None:
            self.request_queue: Queue = _prev_queues['request']
            self.response_queue: Queue = _prev_queues['response']
        else:
            self.request_queue: Queue = Queue()
            self.response_queue: Queue = Queue()
        _q_registry = getattr(sys, '_envoy_queues', {})
        _q_registry[ownerComp.path] = {
            'request': self.request_queue,
            'response': self.response_queue,
        }
        sys._envoy_queues = _q_registry
        self.current_task: Optional[Any] = None
        self._server_gen: int = 0  # Generation counter for stale callback detection
        # Per-session piggyback cursors: sid (or '_anon') -> last served log
        # id. A single shared cursor let whichever session polled first
        # CONSUME warnings meant for everyone (multi-session bug); each
        # session now tracks its own position in the log ring.
        self._log_cursors: dict = {}
        # Advisory dedup: sid -> {(peer_sid, scope): last_served_ts}. Keeps
        # _peers token-lean; conflicts bypass it. Reset on reinit is fine.
        self._advisories_served: dict = {}
        self._peer_hint_served: set = set()
        self._restart_count: int = 0
        self._deadTicks: int = 0  # consecutive watchdog ticks seeing a dead/refused socket
        self._last_start_time: float = 0.0  # time.time() when Start() was called
        # Watchdog revive cooldown, kept as time.monotonic() on an INSTANCE
        # attribute -- never absTime.frame, never COMP storage. absTime.frame
        # resets to 0 each launch while storage persists, so a stored frame from a
        # prior session compared negative and permanently no-op'd recovery.
        self._last_revive_time: float = 0.0
        # Auto-restart policy: retry with EXPONENTIAL BACKOFF for up to
        # _RESTART_WINDOW_SECONDS before giving up -- not a tiny fixed strike
        # count. A transient failure (e.g. a port-rebind race during a reload)
        # self-heals long before the window closes; only a genuinely dead server
        # runs the full window out. The old 3-strike / ~6-second cap could trip
        # permanently on a transient blip, then disable Envoy and force a manual
        # toggle (which also defeated the liveness watchdog).
        self._RESTART_WINDOW_SECONDS: float = 1800.0  # keep retrying for 30 min
        self._RESTART_BACKOFF_BASE: float = 1.0       # first retry after ~1s
        self._RESTART_BACKOFF_MAX: float = 60.0       # cap the gap at 1 min
        self._RESTART_RESET_SECONDS: float = 120.0    # stable this long -> fresh storm
        self._restart_window_start: float = 0.0       # time.time() of a storm's 1st failure
        # H1 startup-readiness state: 'Running' is declared only after the
        # worker confirms a real bind (via _pollStartup).  _starting guards the
        # window so duplicate Start() calls are suppressed before envoy_running.
        self._starting: bool = False
        self._runtime_port: Optional[int] = None
        self._startup_event: Optional[Event] = None
        self._startup_deadline: float = 0.0
        self._venv_recreated: bool = False  # Guard: only auto-recreate venv once per session
        # Background dependency-bootstrap state (see Start / _beginAsyncBootstrap).
        # _bootstrap_result is None while the worker runs, then (ok, [(level, msg)..])
        # plus import-gate status once it finishes; the main-thread poll reads
        # it. _bootstrapping guards against overlapping bootstraps from repeated
        # Start() calls.
        self._bootstrap_result: Optional[tuple] = None
        self._bootstrapping: bool = False
        # Background first-import warmup for the ready-venv fast path. Kept
        # separate from dependency bootstrap because no install work is needed.
        self._import_gate_result: Optional[tuple] = None
        self._import_gate_running: bool = False
        self._undo_active: bool = False  # re-entrancy guard: batch sub-ops must not nest undo blocks

        # --- Live build visualization (smooth follow of the active op) ---
        # The network editor glides to centre on the op Envoy just touched.
        # All state is plain instance attrs (reset on reinit, which is fine).
        # NEVER COMP storage (would pickle on save). Only ever read/written from
        # the main thread (via _onRefresh).
        self._viz_target_op: Optional[str] = None    # path of the op to glide to NOW
        # Pending hops Embot still has to step through. A batch runs every sub-op in
        # ONE frame, so without a queue only the LAST op of the batch would ever be
        # seen; instead each mutating sub-op enqueues a (path, caption) hop and the
        # pump below advances through them one at a time so he visibly steps node to
        # node. List of (op_path, action_text).
        self._viz_target_queue: list = []
        self._viz_hop_until: float = 0.0    # hold the current hop until absTime >= this
        self._viz_last_view: Optional[tuple] = None  # (pane_id, owner, x, y, zoom) we last set
        self._viz_takeover_until: float = 0.0  # absTime.seconds; yield to the user until then
        self._viz_settle_until: float = 0.0    # grace after a navigate while the view settles
        self._viz_zoom_pending: bool = False   # apply _VIZ_ZOOM one frame after a navigate
        self._viz_follow_net: Optional[str] = None  # net we're currently following in (zoom-on-engage)
        self._viz_selected_op: Optional[str] = None  # path of the op we last auto-highlighted
        self._viz_last_activity: float = 0.0   # absTime.seconds of the last build op
        self._viz_action_text: str = ''        # what Embot says he is doing (speech bubble)
        self._viz_speech_src: str = ''         # last action typed into the bubble
        self._viz_speech_t0: float = 0.0       # when the current line started typing
        self._viz_last_skin: Optional[tuple] = None  # last colour written (skip redundant writes)
        self._viz_last_paint: float = 0.0            # last figure repaint (caps repaint fps)
        self._viz_gesture_type: int = 0              # 0 wave / 1 reach / 2 pump / 3 dance
        self._viz_gesture_start: float = 0.0         # when the current gesture began
        self._viz_gesture_end: float = 0.0           # when it ends
        self._viz_next_gesture: float = 0.0          # earliest time the next may start
        self._viz_next_blink: float = 0.0            # absTime.seconds of the next eye blink
        self._viz_blink_end: float = 0.0             # absTime.seconds the current blink ends
        self._viz_eyes_closed: bool = False          # eyes currently coloured shut (blink)
        self._viz_next_squint: float = 0.0           # absTime.seconds of the next happy squint
        self._viz_squint_end: float = 0.0            # absTime.seconds the current squint ends
        self._viz_squinting: bool = False            # eyes currently flattened (squint)
        self._viz_pulse_op: Optional[str] = None      # path of the op currently pulsing
        self._viz_pulse_orig: Optional[tuple] = None  # its original node colour
        self._viz_pulse_start: float = 0.0     # absTime.seconds the pulse began
        self._viz_bot_net: Optional[str] = None       # path of the net the bot figure lives in
        self._viz_bot_pos: Optional[tuple] = None     # (x, y) current figure centre (animated)
        self._viz_bot_from: Optional[tuple] = None    # (x, y) jump origin
        self._viz_bot_target: Optional[tuple] = None  # (x, y) jump destination (stands on op)
        self._viz_bot_jump_t0: float = 0.0            # absTime.seconds the current hop began
        self._viz_jump_dur: float = 0.52              # duration of the current hop (longer for the entrance swoop)
        self._viz_bot_pending_entrance: bool = False  # assembled off-view, awaiting the swoop-in
        self._viz_bot_dest: Optional[tuple] = None    # (x,y) op standing point to swoop to once whole
        self._viz_bot_stage: Optional[tuple] = None   # (x,y) off-view point where parts are copied in
        self._viz_bot_build_queue: list = []          # template part names still to copy this assembly
        self._viz_assemble_next_frame: int = 0        # earliest absTime.frame the next spread part may copy
        self._viz_bot_pending_cleanup: set = set()    # nets whose left-behind bot to tear down off-screen
        self._crash_trace_enabled: bool = False       # diagnostic: flush a breadcrumb per viz annotation-graph op
        self._crash_trace_f = None                    # open handle to the breadcrumb file

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
        # to False -- without this guard, Start() bypasses the opt-in prompt.
        # On code recompile (extension reinit during a running session),
        # _init_complete is already True so auto-start proceeds correctly.
        if (self.ownerComp.par.Envoyenable.eval()
                and self.ownerComp.fetch('_init_complete', False, search=False)):
            # Clear stale status so Start() doesn't bail with "already active"
            self.ownerComp.par.Envoystatus = 'Restarting after reinit...'
            run(f"op('{self.ownerComp.path}').ext.Envoy.Start()",
                delayFrames=30)

        # Arm the liveness watchdog for THIS instance, independent of Start().
        # Tied to the instance lifetime so a save/reinit whose post-reinit
        # auto-start never completes (suppressed stale-thread exit, raced port,
        # or skipped guard) still leaves a watchdog running that revives Envoy.
        # The tick guards on Envoyenable + _init_complete + _starting + instance
        # identity, so it stays inert on a fresh .tox drop before the opt-in
        # prompt and resolves to one loop per instance across reinits.
        self._deadTicks = 0
        self._startingTicks = 0
        # Drop the legacy persisted revive-cooldown frame. It was an absTime.frame
        # (session-local, resets to 0 each launch) wrongly saved to COMP storage;
        # a high value baked from a prior session made every revive's cooldown go
        # negative and permanently no-op the watchdog restart (the wedge this
        # fixes). The cooldown now lives in self._last_revive_time (monotonic,
        # instance-only); this unstore just scrubs the obsolete key from old .toes.
        self.ownerComp.unstore('_last_revive_frame')
        # Tag this armed chain with a monotonic generation (stored on the COMP
        # so it survives the reinit storm). Only the newest generation's tick
        # proceeds; the rest exit as stale -- one live loop per save, not ~N.
        _wd_gen = self.ownerComp.fetch('_watchdog_gen', 0) + 1
        self.ownerComp.store('_watchdog_gen', _wd_gen)
        # Pending run() calls can outlive COMP replacement during upgrades.
        run("o = op(%r)\nif o and o.valid: o.ext.Envoy._watchdogTick(%d)" %
            (self.ownerComp.path, _wd_gen),
            delayMilliSeconds=4000)

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
        Only signals the shutdown event here -- actual Thread Manager cleanup
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

            # Skip pool workers -- shutdown_event handles their cleanup via
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
                f'Cleaned {cleaned} stale Envoy thread(s) -- '
                f'{len(self.ThreadManager.ext.ThreadManagerExt.Threads)}'
                f' threads remain '
                f'(capacity: {self.ThreadManager.ext.ThreadManagerExt.MaxNumberOfThreads.eval()})', 'DEBUG')

    def _forceCloseOldServer(self) -> bool:
        """Force-close a stuck old uvicorn server so the port is freed.

        When an old worker thread is stuck (e.g. waiting on a test Event or
        an HTTP connection), the normal shutdown_event signal may not be enough
        because uvicorn's event loop is blocked. This method:
        1. Signals ALL known shutdown events (in case one was orphaned)
        2. Force-closes the uvicorn server's socket listeners
        3. Unblocks any stuck test Event

        Returns True ONLY when we actually closed a live uvicorn server
        handle of ours (`sys._envoy_uvi_server` was set). Re-signaling
        stale shutdown events for already-exited threads is housekeeping
        and does NOT flip the return -- waiting on those is pointless.
        The drain-wait in `_findAvailablePort` keys off this signal to
        skip the 500ms sleep when the port holder is foreign/zombie.
        """
        # Signal all known shutdown events (housekeeping -- does not by
        # itself indicate WE are holding a socket).
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
            # force_exit skips graceful drain -- without this, uvicorn waits
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
            return True   # We actually closed a live socket of ours.
        return False  # Nothing of ours was holding any port.

    def _findAvailablePort(self, base_port: int, range_size: int = 10) -> 'int | None':
        """Find an available port in [base_port, base_port + range_size).

        Checks BOTH the socket state AND the envoy.json registry so that
        two TD instances starting near-simultaneously don't race on the same
        port.  A port is considered taken if:
          - A TCP connect succeeds (something is listening), OR
          - Another instance is registered on it with a live PID.

        Tries the base port first (fast path for single-instance).  If busy,
        attempts to force-close a stale server from the same TD process, then
        scans the remaining range without force-close (those ports belong to
        other instances).

        Returns the first free port, or None if all are occupied.
        """
        import socket
        import os as _os

        def _port_in_use(port: int) -> bool:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(1)
                return s.connect_ex(('127.0.0.1', port)) == 0

        def _port_registered_by_other(port: int) -> bool:
            """Check if another live instance claims this port in envoy.json."""
            try:
                config_path = self._registryPath()
                if config_path is None or not config_path.exists():
                    return False
                config = json.loads(config_path.read_text(encoding='utf-8'))
                my_pid = _os.getpid()
                for name, info in config.get('instances', {}).items():
                    if info.get('port') == port:
                        other_pid = info.get('td_pid', 0)
                        if other_pid and other_pid != my_pid:
                            # Use the shared safe liveness check -- a raw
                            # os.kill(other_pid, 0) here would silently
                            # TerminateProcess() the foreign TD on Windows.
                            if EnvoyExt._isPidAlive(other_pid):
                                return True  # Another live instance owns this port
            except Exception:
                pass
            return False

        def _port_taken(port: int) -> bool:
            return _port_in_use(port) or _port_registered_by_other(port)

        # Fast path: preferred port is free AND not claimed by another instance
        if not _port_taken(base_port):
            return base_port

        # Branch on WHY the port is taken.
        #
        # If a foreign live TD instance has it registered in envoy.json,
        # _forceCloseOldServer cannot help -- that only signals shutdown
        # for OUR server thread. Jump straight to the range scan instead
        # of blocking the main thread on a 1.5s poll loop that cannot
        # change the outcome. (Symptom: ~108 dropped frames per toggle
        # at 60fps whenever a zombie PID claims the preferred port.)
        if _port_registered_by_other(base_port):
            self._log(f'Port {base_port} held by another instance, scanning range...')
            for offset in range(1, range_size):
                candidate = base_port + offset
                if not _port_taken(candidate):
                    return candidate
            return None

        # Port is taken but no foreign registry entry. Try force-close --
        # IF we had anything of ours to close, wait briefly for the socket
        # to drain. Otherwise the port is held by an UNREGISTERED foreign
        # process (e.g. zombie TD that isn't in envoy.json); waiting on
        # that would block the main thread for no benefit -- skip to the
        # range scan.
        self._log(f'Port {base_port} in use, attempting to free it...')
        acted = self._forceCloseOldServer()

        if acted:
            # We had a stale server -- wait briefly for OS-level close.
            # Capped at 500ms (5 x 100ms) because force_exit + explicit
            # sock.close() should free the port near-instantly; longer
            # waits noticeably stutter the UI.
            import time as _time
            for _ in range(5):
                _time.sleep(0.1)
                if not _port_taken(base_port):
                    self._log(f'Port {base_port} freed after force-close')
                    return base_port

        # Either nothing of ours was holding the port (foreign zombie), or
        # the wait expired. Scan the range for any free port.
        self._log(f'Port {base_port} held by another process, scanning range...')
        for offset in range(1, range_size):
            candidate = base_port + offset
            if not _port_taken(candidate):
                return candidate

        return None

    def Start(self) -> None:
        """Start MCP server via op.TDResources.ThreadManager"""
        if self.ownerComp.fetch('envoy_running', False) or self._starting:
            self._log('Server already running/starting (duplicate Start ignored)',
                      'DEBUG')
            return
        # The envoy_running store can be lost on extension reinit (file sync
        # replaces baked-in code -> extension reinitializes -> storage cleared).
        # Check the status parameter as a backup -- it survives reinit.
        # Only 'Running' means the server thread is actually active.
        # 'Starting...' is just a UI hint -- not proof of an active thread.
        status = str(self.ownerComp.par.Envoystatus.eval())
        if status.startswith('Running'):
            # Trust a 'Running' status ONLY if the socket actually answers. A
            # stale 'Running on port N' left behind when a worker died (the save
            # wedge -- status never updated) must NOT short-circuit the restart,
            # or Start bails, re-asserts envoy_running=True, and the server stays
            # down forever. _runtime_port is reset on reinit, so recover the port
            # from the status string (or the configured par) before probing.
            import re as _re, socket as _socket
            _m = _re.search(r'port (\d+)', status)
            _probe_port = (getattr(self, '_runtime_port', None)
                           or (int(_m.group(1)) if _m else None)
                           or int(self.ownerComp.par.Envoyport.eval()))
            _alive = False
            try:
                _s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
                _s.settimeout(0.25)
                try:
                    _s.connect(('127.0.0.1', int(_probe_port)))
                    _alive = True
                finally:
                    _s.close()
            except Exception:
                _alive = False
            if _alive:
                self._log(f'Server already active (status: {status})', 'WARNING')
                self.ownerComp.store('envoy_running', True)
                return
            self._log(f'Stale {status!r} but socket is dead -- restarting fresh',
                      'WARNING')
            # fall through to start a new worker

        # A background dependency install from a prior Start() is still running;
        # _pollBootstrap will finish the start when it completes. Don't stack a
        # second bootstrap on top of it.
        if self._bootstrapping:
            self._log('Dependency install already in progress (Start ignored)', 'DEBUG')
            return
        if self._import_gate_running:
            self._log('Import gate warm-up already in progress (Start ignored)', 'DEBUG')
            return

        # Resolve git root silently -- Start() never prompts. Dialogs belong only
        # in _enableEnvoy() / InitGit() which are explicitly user-initiated.
        git_root = self.ownerComp.fetch('_git_root', None, search=False)
        if not git_root:
            git_root = self._findGitRoot()
            self.ownerComp.store('_git_root', git_root)

        # Ensure the Python environment is ready before starting the server.
        # The fast path (deps already installed and current) is cheap and runs
        # inline. But a fresh install or a version upgrade has to build the venv
        # and pip-install the MCP stack -- tens of seconds to minutes of blocking
        # subprocess work. Running THAT on the main thread froze TD on every
        # drag-in upgrade (the user watched TD lock up, then recover when pip
        # finished). So we route the install-needed case through a background
        # thread and finish the start from _pollBootstrap once it completes.
        Embody = op.Embody.ext.Embody
        spec = Embody._venvPaths()
        if Embody._environmentNeedsInstall(spec):
            self._beginAsyncBootstrap(git_root, spec)
            return

        # Fast path: environment already usable. Wire sys.path inline because
        # that is cheap, but run the first mcp.server import gate on a worker
        # thread. Cold-importing MCP pulls in pydantic/starlette/uvicorn and can
        # freeze TD for several seconds on first open after install/upgrade.
        if not Embody._wirePythonPaths(spec):
            self.ownerComp.par.Envoystatus = 'Error: Python environment not ready'
            self._log(
                Embody._importGateFailureMessage(
                    spec['site_packages'], 'venv site-packages path is missing'),
                'ERROR',
            )
            self._log(
                'Aborting Envoy start -- Python environment is not ready. '
                'See textport above for the underlying failure.',
                'ERROR',
            )
            return

        if getattr(sys, '_envoy_import_gate_ok', False):
            self._continueStart(git_root)
            return

        self._beginAsyncImportGate(git_root, spec)

    def _beginAsyncImportGate(self, git_root, spec) -> None:
        """Warm the MCP import stack on a worker thread for the ready-venv path."""
        self._import_gate_running = True
        self._import_gate_result = None
        self.ownerComp.par.Envoystatus = 'Preparing Python environment...'
        self._log(
            'Warming the MCP Python stack on a background thread -- first open '
            'after install/upgrade can take a few seconds; TD stays responsive.',
            'INFO',
        )
        import_gate_check = op.Embody.ext.Embody._importGateCheck

        def worker():
            try:
                result = import_gate_check()
            except BaseException as e:
                result = (False, str(e) or e.__class__.__name__)
            # Atomic publish: the main-thread poll reads this single attribute.
            self._import_gate_result = result

        Thread(target=worker, daemon=True).start()
        run('args[0]._pollImportGate(args[1], args[2])',
            self, git_root, spec, delayFrames=15)

    def _pollImportGate(self, git_root, spec) -> None:
        """Main-thread poll for the fast-path background MCP import gate."""
        # Stale-instance guard: a save/recompile may have replaced this EnvoyExt
        # while the worker ran. The fresh instance owns startup now.
        try:
            if self.ownerComp.ext.Envoy is not self:
                return
        except Exception:
            return

        result = self._import_gate_result
        if result is None:
            run('args[0]._pollImportGate(args[1], args[2])',
                self, git_root, spec, delayFrames=15)
            return

        self._import_gate_running = False
        ok, message = result

        # The user may have toggled Envoy off while the import gate warmed.
        if not self.ownerComp.par.Envoyenable.eval():
            self._log('Envoy disabled during Python environment prep -- not starting.', 'DEBUG')
            if not str(self.ownerComp.par.Envoystatus.eval()).startswith(
                    ('Error', 'Disabled', 'Off')):
                self.ownerComp.par.Envoystatus = 'Disabled'
            return

        if not ok:
            self.ownerComp.par.Envoystatus = 'Error: Python environment not ready'
            self._log(
                op.Embody.ext.Embody._importGateFailureMessage(
                    spec['site_packages'], message),
                'ERROR',
            )
            self._log(
                'Aborting Envoy start -- Python environment is not ready. '
                'See textport above for the underlying failure.',
                'ERROR',
            )
            return

        sys._envoy_import_gate_ok = True
        self._continueStart(git_root)

    def _beginAsyncBootstrap(self, git_root, spec) -> None:
        """Install Envoy's Python dependencies on a background thread, then
        finish the server start.

        Keeps TouchDesigner responsive during the venv build / pip install that
        a fresh install or a version upgrade triggers. The worker runs
        EmbodyExt._installDependencies, wires sys.path, and warms the MCP import
        gate; its log lines are captured and replayed on the main thread by
        _pollBootstrap, because EmbodyExt.Log writes the FIFO DAT and reads
        parameters.
        """
        self._bootstrapping = True
        self._bootstrap_result = None
        import os as _os
        self._venv_existed = _os.path.isdir(spec['venv_dir'])  # only record a venv Embody creates
        self.ownerComp.par.Envoystatus = 'Installing deps... (one-time)'
        self._log(
            'Installing Envoy Python dependencies in the background (one-time '
            'setup). TouchDesigner stays responsive; MCP will connect when this '
            'finishes.')
        Embody = op.Embody.ext.Embody
        wire_python_paths = Embody._wirePythonPaths
        import_gate_check = Embody._importGateCheck

        def worker():
            msgs = []
            gate_ok = False
            gate_msg = ''
            try:
                ok = Embody._installDependencies(
                    spec, log=lambda m, lvl='INFO': msgs.append((lvl, m)))
            except BaseException as e:
                ok = False
                msgs.append(('ERROR', f'Dependency install crashed: {e}'))
            if ok:
                try:
                    if wire_python_paths(spec):
                        gate_ok, gate_msg = import_gate_check()
                    else:
                        gate_msg = 'venv site-packages path is missing'
                except BaseException as e:
                    gate_msg = str(e) or e.__class__.__name__
            # Atomic publish: the main-thread poll reads this single attribute.
            self._bootstrap_result = (ok, msgs, gate_ok, gate_msg)

        Thread(target=worker, daemon=True).start()
        run('args[0]._pollBootstrap(args[1], args[2])',
            self, git_root, spec, delayFrames=30)

    def _pollBootstrap(self, git_root, spec) -> None:
        """Main-thread poll for the background dependency install (see
        _beginAsyncBootstrap). Replays captured log lines, honors a mid-install
        Envoy-disable, then finishes the start or reports failure."""
        # Stale-instance guard: a save/recompile may have replaced this EnvoyExt
        # while the worker ran. The fresh instance owns startup now.
        try:
            if self.ownerComp.ext.Envoy is not self:
                return
        except Exception:
            return

        result = self._bootstrap_result
        if result is None:
            # Worker still installing -- check again shortly.
            run('args[0]._pollBootstrap(args[1], args[2])',
                self, git_root, spec, delayFrames=30)
            return

        self._bootstrapping = False
        ok, msgs, gate_ok, gate_msg = result
        for lvl, m in msgs:
            self._log(m, lvl)

        # The user may have toggled Envoy off while deps installed -- honor it
        # rather than starting a server they just disabled.
        if not self.ownerComp.par.Envoyenable.eval():
            self._log('Envoy disabled during dependency install -- not starting.', 'DEBUG')
            if not str(self.ownerComp.par.Envoystatus.eval()).startswith(
                    ('Error', 'Disabled', 'Off')):
                self.ownerComp.par.Envoystatus = 'Disabled'
            return

        if not ok:
            self.ownerComp.par.Envoystatus = 'Error: Python environment not ready'
            self._log(
                'Envoy start aborted -- dependency install failed. '
                'See messages above.', 'ERROR')
            return

        # The worker created the venv (if it didn't already exist) -- record it
        # for Uninstall. Best-effort; must never block the start.
        try:
            if not getattr(self, '_venv_existed', True):
                Embody = op.Embody.ext.Embody
                Embody._manifestRecordVenv(
                    str(Embody._findProjectRoot()), Embody._venvPaths()['venv_dir'])
        except Exception:
            pass

        if not gate_ok:
            self.ownerComp.par.Envoystatus = 'Error: Python environment not ready'
            self._log(
                op.Embody.ext.Embody._importGateFailureMessage(
                    spec['site_packages'], gate_msg),
                'ERROR',
            )
            return
        sys._envoy_import_gate_ok = True
        self._continueStart(git_root)

    def _continueStart(self, git_root) -> None:
        """Finish Envoy startup once the Python environment is confirmed ready.

        Runs on the main thread -- either inline from Start() after the session
        import-gate flag is already warm, from _pollImportGate(), or from
        _pollBootstrap() after a background dependency install. Allocates the
        port, spawns the server worker via the Thread Manager, and writes the
        MCP / git config files.
        """
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
            # Note: do NOT set self.ownerComp.par.Envoyport = port here.
            # Envoyport is the user's *preferred* port; parexec.py watches it
            # and triggers Stop+Start on change, causing a restart loop.
            # The actual runtime port is shown in Envoystatus instead.

        # H1: do NOT claim running here -- defer until _pollStartup confirms a
        # real bind. (Previously stored envoy_running=True optimistically, which
        # produced a zombie "Running" status when the worker never bound.)

        # Clean up stale temp files from previous sessions
        self._cleanupTempFiles()

        # Create a FRESH Event for this server instance.  Don't clear() the old
        # one -- it must stay set so the previous thread's shutdown_monitor sees it.
        self.shutdown_event = Event()
        _registry = getattr(sys, '_envoy_shutdown_events', {})
        _registry[self.ownerComp.path] = self.shutdown_event
        sys._envoy_shutdown_events = _registry

        # H1: fresh readiness event for THIS start; the worker sets it once
        # uvicorn binds.  _pollStartup waits on it before declaring Running.
        startup_event = Event()
        self._startup_event = startup_event
        self._runtime_port = port

        self._server_gen += 1
        gen = self._server_gen
        self._last_start_time = time.time()
        self._starting = True  # H1: starting window open (suppresses duplicate Start)

        self._log(f'Starting Envoy MCP server on port {port}')

        # Update status
        self.ownerComp.par.Envoystatus = 'Starting...'

        # Wrap hooks with generation guard so stale callbacks from a previous
        # server thread don't corrupt the running server's state.
        # Two checks: (1) instance identity -- detects extension reinit (Update,
        # recompile) where a NEW EnvoyExt instance replaced us; (2) generation
        # counter -- detects rapid Start() calls on the SAME instance.
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

        # Fresh queues for the new server thread -- the old worker thread
        # drains via its own shutdown_event, not these queues.
        self.request_queue = Queue()
        self.response_queue = Queue()
        _q_registry = getattr(sys, '_envoy_queues', {})
        _q_registry[self.ownerComp.path] = {
            'request': self.request_queue,
            'response': self.response_queue,
        }
        sys._envoy_queues = _q_registry

        # Create and enqueue a TDTask
        self.current_task = self.ThreadManager.TDTask(
            target=self._runServer,
            args=(port, self.request_queue, self.response_queue,
                  self.shutdown_event, startup_event),
            SuccessHook=guarded_success,
            ExceptHook=guarded_error,
            RefreshHook=self._onRefresh
        )
        thread = self.ThreadManager.EnqueueTask(
            self.current_task, standalone=True)

        if thread is None:
            # H1: no standalone worker means the socket can never bind. Treat as
            # a startup failure so escalation engages, instead of a zombie that
            # reports "Running" forever.
            self._log(
                'Thread Manager could not start a standalone server worker.',
                'ERROR')
            self._starting = False
            self._onServerError('Thread Manager could not start server worker')
            return

        # H1: status stays 'Starting...' (set above) until the worker confirms
        # the socket is bound; _pollStartup flips it to 'Running on port N' or
        # escalates on timeout/failure. Config files below are written
        # regardless -- the bridge retries until the server is reachable.
        self._startup_deadline = time.time() + 10.0
        run(f"op({self.ownerComp.path!r}).ext.Envoy._pollStartup({gen})",
            fromOP=self.ownerComp, delayFrames=6)

        # Auto-configure project files.
        # Each step is independent -- one failure must not block the others.
        # MCP + AI config: always co-located, honoring Aiprojectroot.
        #
        # This is a startup Start: in Advanced mode a config write must NOT pop a
        # modal here (it would block the restore chain), so _startup_config_pass
        # makes the guards DEFER + breadcrumb. The setup wizard's _consent_bulk
        # (set before it flipped Envoyenable) takes precedence, so a consented
        # first-run still applies. Cleared in the finally so it can't stick.
        Embody = op.Embody.ext.Embody
        prior_pass = Embody._startup_config_pass
        Embody._startup_config_pass = True
        try:
            try:
                target_dir = Embody._findProjectRoot()
            except Exception:
                # Defensive fallback for older deployments
                target_dir = git_root if git_root != 'no-git' else None
            self._configureMCPClient(port, target_dir=target_dir)
            try:
                Embody._upgradeEnvoy()
            except Exception as e:
                self._log(f'Could not auto-configure AI client files: {e}', 'WARNING')

            # Git config: only when a git repo exists. Always lives at the git
            # root regardless of Aiprojectroot -- .gitignore/.gitattributes are
            # git's files, not Embody's.
            if git_root != 'no-git':
                from pathlib import Path
                git_path = Path(git_root)
                self._configureGitignore(git_path)
                self._configureGitattributes(git_path)
        finally:
            Embody._startup_config_pass = prior_pass
            # Clear the wizard's batch consent now its deferred-Start writes are
            # done (the bounded timer in _enableEnvoyResolved is the backstop).
            Embody._consent_bulk = False

    def _pollStartup(self, gen: int) -> None:
        """Main-thread poll (H1): declare 'Running' only after the worker
        confirms the socket bound; escalate if it never binds in time.

        Replaces the old optimistic 'Running' set in _continueStart, which
        declared success the instant the task was enqueued -- producing a
        zombie 'Running' over a dead/never-bound socket.
        """
        # Stale guard: a newer Start() (or Stop/error, which clears _starting)
        # superseded this attempt.
        if gen != self._server_gen or not self._starting:
            return
        ev = self._startup_event
        if ev is not None and ev.is_set():
            # Confirmed bound + serving.
            self._starting = False
            self.ownerComp.store('envoy_running', True)
            self.ownerComp.par.Envoystatus = f'Running on port {self._runtime_port}'
            self._last_start_time = time.time()
            self._log(
                f'Envoy MCP server confirmed listening on port '
                f'{self._runtime_port}', 'DEBUG')
            # The liveness watchdog is already running for this generation (armed
            # at _continueStart); a confirmed bind just flips it from the _starting
            # defer-state into active socket monitoring on its next tick. It
            # self-heals the socket if it dies later with no thread-exit callback
            # firing -- the zombie behind the recurring "connection dropped while
            # TD runs" symptom.
            return
        if time.time() >= self._startup_deadline:
            # Never bound within the readiness window -> route to the error path
            # so the restart/escalation logic engages (not a silent zombie).
            self._starting = False
            self._onServerError(
                f'Envoy did not bind port {self._runtime_port} within the '
                f'startup timeout')
            return
        # Not yet bound, not timed out -- keep polling.
        run(f"op({self.ownerComp.path!r}).ext.Envoy._pollStartup({gen})",
            fromOP=self.ownerComp, delayFrames=6)

    # === Liveness watchdog (pure Python run()-loop -- no operator, no timer) ===

    def _watchdogTick(self, gen: int = 0) -> None:
        """Self-healing liveness loop, tied to THIS extension instance's lifetime.

        Armed once per instance from __init__ (NOT from Start), so it survives a
        project.save() / extension reinit whose post-reinit auto-start never
        completes -- the failure that left Envoy down with no watchdog and no
        recovery (the old server thread's exit callback suppresses itself once a
        reinit has replaced the instance, and the new instance's Start can be
        skipped or race the old port). It probes the real socket and revives
        Envoy whenever it is enabled-but-down (a dropped-socket zombie, a
        never-bound restart, or a suppressed reinit Start), so every connected
        bridge reconnects on its own -- no manual toggle.

        Lifecycle: ONE loop per EnvoyExt instance. It dies ONLY when a reinit
        replaces the instance (the identity guard); the new instance's __init__
        arms a fresh loop. It does NOT die on a server-generation bump (revive or
        restart) or on a disable -- it keeps ticking idle while disabled so a
        re-enable resumes self-healing without needing a reinit.
        """
        # Collapse the armed-tick storm from a save strip/restore (one tick is
        # armed per reinit, and the run() string re-resolves to the current
        # instance so the identity guard below cannot dedupe them) into a
        # single live loop: only the newest armed generation proceeds; older
        # armed ticks are stale and exit here without rescheduling, reviving,
        # or logging. gen == 0 is a legacy tick armed before this guard existed
        # -- let it proceed so the loop is never orphaned across the upgrade.
        try:
            if gen and gen != self.ownerComp.fetch('_watchdog_gen', 0):
                return
        except Exception:
            pass
        # Die ONLY when a reinit has replaced this instance (the new instance
        # arms its own loop). Server-generation churn must NOT end the loop.
        try:
            if self.ownerComp.ext.Envoy is not self:
                return
        except Exception:
            return

        try:
            enabled = bool(self.ownerComp.par.Envoyenable.eval())
            status = str(self.ownerComp.par.Envoystatus.eval())
            # The SOCKET is the source of truth, never the internal _starting /
            # _init_complete flags. A project.save() clears _init_complete and a
            # reinit resets _starting, and keying off them is exactly what wedged a
            # dead server forever -- the watchdog went idle and never revived.
            # Only two idle cases: disabled, or a one-time deps install (legit
            # long, never interrupt). An explicit Start 'Error' is also left alone
            # so a hard failure (e.g. broken venv) is not hammered every tick.
            installing = status.startswith('Installing')
            transitional = status.startswith(('Starting', 'Restarting', 'Reviving'))
            if not enabled or installing or status.startswith('Error'):
                self._deadTicks = 0
                self._startingTicks = 0
            elif transitional:
                # Startup grace: a transitional status must resolve quickly. Force
                # a restart if it sticks ~24s (stale poll generation, raced reinit
                # Start, a worker that never bound), then retry every ~24s until
                # the socket answers. Status-par driven, so it fires even after a
                # save reset _starting / _init_complete.
                self._deadTicks = 0
                self._startingTicks = getattr(self, '_startingTicks', 0) + 1
                if self._startingTicks >= 6:     # 6 * 4s = ~24s stuck -> restart
                    self._startingTicks = 0
                    self._log(
                        f'Watchdog: status stuck at {status!r} ~24s while '
                        f'enabled -- forcing restart', 'WARNING')
                    self._reviveDeadServer(
                        self.ownerComp.fetch('envoy_running', False))
            else:
                # Settled + enabled, INCLUDING a stale 'Running on port N' left
                # after a save killed the worker without updating the status (the
                # exact 6.36/6.37 wedge): probe the real socket. Dead for ~8s ->
                # revive. _init_complete is intentionally NOT consulted here.
                self._startingTicks = 0
                running = self.ownerComp.fetch('envoy_running', False)
                if running and self._probeAlive():
                    self._deadTicks = 0
                else:
                    self._deadTicks += 1
                    if self._deadTicks >= 2:     # ~8s enabled-but-down -> revive
                        self._deadTicks = 0
                        self._log(
                            f'Watchdog: enabled but socket dead (status '
                            f'{status!r}) -- reviving', 'WARNING')
                        self._reviveDeadServer(running)
        except Exception as e:
            try:
                self._log(f'Watchdog tick error (continuing): {e}', 'DEBUG')
            except Exception:
                pass

        # Always reschedule -- the loop is instance-tied; only the identity guard
        # above ends it. A transient tick error never kills self-healing.
        # Pending run() calls can outlive COMP replacement during upgrades.
        run("o = op(%r)\nif o and o.valid: o.ext.Envoy._watchdogTick(%d)" %
            (self.ownerComp.path, gen),
            fromOP=self.ownerComp, delayMilliSeconds=4000)

    def _probeAlive(self) -> bool:
        """Fast localhost connect to the runtime port. True iff a listener answers.

        Connection refused / timeout -> dead. Localhost makes this effectively
        instant (refused returns immediately), so it never stalls the main-thread
        tick. Unknown port -> True, so we never restart on missing info.
        """
        import socket
        port = getattr(self, '_runtime_port', None)
        if not port:
            return True
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(0.25)
        try:
            sock.connect(('127.0.0.1', int(port)))
            return True
        except Exception:
            return False
        finally:
            try:
                sock.close()
            except Exception:
                pass

    def _reviveDeadServer(self, was_running: bool) -> None:
        """Socket is dead while Envoy is enabled and no thread-exit callback fired.

        Tear the (possibly stuck) worker down and rebind after a short delay that
        lets it release the socket -- so the runtime port stays stable on the
        rebind instead of drifting to port+1, which is what left bridges stranded
        on a refused port.

        A project.save() strip/restore reinits this extension many times; each
        reinit arms a watchdog tick, and ~4s later they ALL come due in the same
        frame and each calls here -- the 18-21x "reviving server" spam plus an
        equal pile of Start() schedules. Collapse them to ONE revive per short
        frame cooldown: the same-frame storm fires once; a genuine later outage
        (dead ticks are >=~8s apart) still revives normally. This runs in the
        stable fire-frame (not mid-reinit), so the COMP store is reliable here
        even though the per-reinit generation counter armed during the storm is
        not -- which is why the dedup lives here and not only on the tick.
        """
        # Cooldown: collapse a same-frame storm of revive calls (multiple armed
        # watchdog ticks coming due together) into one. Uses time.monotonic() on
        # an INSTANCE attribute -- never absTime.frame, never COMP storage --
        # because absTime.frame resets to 0 each launch while storage persists, so
        # a stored frame from a prior session went negative here and PERMANENTLY
        # blocked recovery (detection kept firing, restart never ran). A fresh
        # instance always starts un-wedged; the genuine ~8s revive cadence still
        # clears the 2s window.
        if time.monotonic() - self._last_revive_time < 2.0:
            return  # already revived for this death event -- drop the duplicate
        self._last_revive_time = time.monotonic()
        port = getattr(self, '_runtime_port', None)
        self._log(
            f'Watchdog: MCP socket on port {port} unreachable while enabled '
            f'(running={was_running}) -- reviving server', 'WARNING')
        # Bump the generation first so the old worker's exit callbacks (and any
        # pending poll/watchdog) are treated as stale -- a single clean restart,
        # not ours plus a _scheduleRestart racing each other.
        self._server_gen += 1
        try:
            self.shutdown_event.set()  # nudge a stuck worker to exit + free the socket
        except Exception:
            pass
        self.ownerComp.store('envoy_running', False)
        self._starting = False
        self.ownerComp.par.Envoystatus = 'Reviving (watchdog)...'
        run(f"op({self.ownerComp.path!r}).ext.Envoy.Start()",
            fromOP=self.ownerComp, delayFrames=18)

    def Stop(self) -> None:
        """Stop MCP server"""
        # Always reset auto-restart counter on Stop, even when envoy_running
        # is already False.  Without this, the restart-limit path in
        # _scheduleRestart sets Envoyenable=False -> parexec -> Stop(), but
        # envoy_running was already cleared by _onServerError, so the old
        # code returned early and left _restart_count stuck above MAX.
        # The next manual toggle would immediately hit the limit again,
        # making Envoyenable appear to "do nothing."
        self._restart_count = 0
        self._restart_window_start = 0.0  # fresh retry window on the next storm
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
                   shutdown_event: Event, startup_event: Optional[Event] = None):
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
                shutdown_event=shutdown_event,
                startup_event=startup_event,
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
            # uvicorn raises UnboundLocalError when bind fails -- surface
            # the underlying cause if possible.
            if 'address already in use' in str(e).lower():
                raise RuntimeError(
                    f'Port {port} is already in use. '
                    f'Another Envoy instance or process may be bound to it.'
                ) from e
            raise RuntimeError(f'MCP server failed on port {port}: {e}') from e
        except BaseException as e:
            # uvicorn calls sys.exit(1) on bind failure -> SystemExit, which is
            # a BaseException and escapes the handlers above. Without this it
            # never reaches the ExceptHook (no error -> no restart/escalation ->
            # zombie "Running"). Normalize so _onServerError fires.
            raise RuntimeError(
                f'MCP server exited abnormally on port {port}: {e!r}') from e

    # === Thread Manager Callbacks (run on main thread) ===

    _GATED_OPERATIONS = ('delete_op', 'import_network', 'run_tests',
                         'batch_operations')

    def _destructiveTargets(self, operation, params):
        """(scopes, reason) the destructive gate protects for this
        operation, or ([], '') when nothing is gated."""
        params = params or {}
        if operation == 'delete_op':
            target = params.get('op_path')
            if isinstance(target, str) and target.startswith('/'):
                return [target], 'delete_op'
            return [], ''
        if operation == 'import_network':
            if not params.get('clear_first'):
                return [], ''
            target = params.get('target_path')
            if isinstance(target, str) and target.startswith('/'):
                return [target], 'import_network(clear_first=True)'
            return [], ''
        if operation == 'run_tests':
            return ['project:tests'], 'run_tests'
        if operation == 'batch_operations':
            gated = []
            for sub in (params.get('operations') or [])[:32]:
                if not isinstance(sub, dict):
                    continue
                sub_params = sub.get('params') or {}
                if sub_params.get('override'):
                    continue
                sub_scopes, _reason = self._destructiveTargets(
                    sub.get('tool', ''), sub_params)
                gated.extend(sub_scopes)
            if gated:
                return gated, 'batch_operations (destructive sub-operations)'
            return [], ''
        return [], ''

    def _checkDestructiveGate(self, sid, operation, params):
        """Refuse a destructive operation when a LIVE peer session claimed
        an overlapping scope or wrote it within the conflict window, unless
        override=True. Returns the error dict to send back, or None to
        proceed. Advisory-first design: everything else only warns."""
        if operation not in self._GATED_OPERATIONS:
            return None
        if (params or {}).get('override'):
            return None
        targets, reason = self._destructiveTargets(operation, params)
        if not targets:
            return None
        lock, touches, sessions = self._touchStores()
        claims = getattr(sys, '_envoy_claims', None)
        if lock is None:
            return None
        me = sid or '_anon'
        now = time.time()
        with lock:
            live = {s for s, v in (sessions or {}).items()
                    if now - v.get('last_seen', 0) < 600}
            for held_scope, claim in (claims or {}).items():
                if claim['sid'] == me or claim['sid'] not in live:
                    continue
                if now > claim['ts'] + claim['ttl']:
                    continue
                if not any(_scope_overlaps(target, held_scope)
                           for target in targets):
                    continue
                holder = (sessions or {}).get(claim['sid']) or {}
                label = holder.get('label') or claim.get('label') or claim['sid']
                note = claim.get('note', '') or 'no note'
                self._log(
                    'MULTI-SESSION GATE: refused ' + reason + ' -- "' + label
                    + '" holds ' + held_scope + ' (' + note + ')', 'WARNING')
                return {'error': 'MULTI-SESSION GATE: ' + reason
                                 + ' refused -- session "' + label
                                 + '" holds a claim on ' + held_scope
                                 + ' (' + note + ', expires in '
                                 + str(round(claim['ts'] + claim['ttl'] - now))
                                 + 's). Coordinate, work in another subtree,'
                                 + ' wait for expiry, or pass override=True'
                                 + ' if you are certain.',
                        'holder': {'label': label, 'scope': held_scope,
                                   'note': claim.get('note', '')}}
            for scope, ring in (touches or {}).items():
                if not any(_scope_overlaps(target, scope)
                           for target in targets):
                    continue
                for touch in reversed(ring):
                    if touch['sid'] == me:
                        continue
                    age = now - touch['ts']
                    if age > _CONFLICT_WINDOW_S:
                        break  # ring is chronological; older ones only
                    peer = (sessions or {}).get(touch['sid']) or {}
                    label = peer.get('label') or touch['sid']
                    self._log(
                        'MULTI-SESSION GATE: refused ' + reason + ' -- "'
                        + label + '" wrote ' + scope + ' '
                        + str(round(age, 1)) + 's ago', 'WARNING')
                    return {'error': 'MULTI-SESSION GATE: ' + reason
                                     + ' refused -- session "' + label
                                     + '" wrote ' + scope + ' only '
                                     + str(round(age, 1)) + 's ago. Check'
                                     + ' get_sessions, coordinate, or pass'
                                     + ' override=True if you are certain.',
                            'peer': {'label': label, 'scope': scope,
                                     'tool': touch['tool'],
                                     'age_s': round(age, 1)}}
        return None
    @staticmethod
    def _touchStores():
        """Shared multi-session stores (created by the worker; None-safe
        before the first server start)."""
        return (getattr(sys, '_envoy_sessions_lock', None),
                getattr(sys, '_envoy_touches', None),
                getattr(sys, '_envoy_sessions', None))

    def _expandFileScopes(self, scopes):
        """Append file: scopes for op-path scopes covered by the
        externalizations table (the op's own row, or a tracked ancestor
        such as a TDN COMP). Main thread only -- reads the live table."""
        out = list(scopes)
        try:
            table = op.Embody.ext.Embody.Externalizations
            if not table or table.numRows < 2:
                return out
            rows = []
            for r in range(1, table.numRows):
                tracked_path = table[r, 'path'].val
                rel_file = table[r, 'rel_file_path'].val
                if tracked_path and rel_file:
                    rows.append((tracked_path, rel_file))
            for scope in scopes:
                if not scope.startswith('/'):
                    continue
                matches = [(tracked_path, rel_file)
                           for tracked_path, rel_file in rows
                           if scope == tracked_path
                           or scope.startswith(tracked_path + '/')]
                # Most specific first; keep at most 2 (the op's own file +
                # its nearest tracked ancestor, e.g. the enclosing .tdn).
                # Broader ancestors (a project-root .tdn) would make every
                # write in the project overlap every other one.
                matches.sort(key=lambda m: len(m[0]), reverse=True)
                for _tracked, rel_file in matches[:2]:
                    file_scope = 'file:' + rel_file.replace('\\', '/')
                    if file_scope not in out:
                        out.append(file_scope)
        except Exception:
            pass
        return out[:12]

    def _recordTouches(self, sid, operation, scopes):
        """Record a WRITE operation's scopes in the shared touch map."""
        if operation not in _WRITE_OPERATIONS or not scopes:
            return
        lock, touches, _sessions = self._touchStores()
        if lock is None or touches is None:
            return
        entry = {'sid': sid or '_anon', 'tool': operation, 'ts': time.time()}
        with lock:
            # Lease renewal: the holder's own writes refresh their claims.
            claims = getattr(sys, '_envoy_claims', None)
            if claims:
                for held_scope, claim in claims.items():
                    if claim['sid'] == entry['sid'] and any(
                            _scope_overlaps(s, held_scope) for s in scopes):
                        claim['ts'] = entry['ts']
            for scope in scopes:
                ring = touches.setdefault(scope, [])
                ring.append(entry)
                del ring[:-_TOUCH_RING_CAP]
            if len(touches) > _TOUCH_SCOPE_CAP:
                oldest_first = sorted(touches.items(),
                                      key=lambda kv: kv[1][-1]['ts'])
                for stale_scope, _ring in oldest_first[:len(touches) - _TOUCH_SCOPE_CAP]:
                    del touches[stale_scope]

    def _attachPeerAdvisories(self, result, sid, operation, scopes):
        """Attach _peers: recent overlapping WRITE activity by OTHER
        sessions. conflict=true when both sides are writes within
        _CONFLICT_WINDOW_S -- the response-side half of the relay (the
        shipped rule tells agents to treat a conflict as a hard stop,
        same contract as LAYOUT WARNING)."""
        if not scopes or not isinstance(result, dict):
            return
        lock, touches, sessions = self._touchStores()
        if lock is None or touches is None:
            return
        me = sid or '_anon'
        now = time.time()
        is_write = operation in _WRITE_OPERATIONS
        candidates = []
        with lock:
            served = self._advisories_served.setdefault(me, {})
            for scope, ring in touches.items():
                if not any(_scope_overlaps(s, scope) for s in scopes):
                    continue
                for touch in reversed(ring):
                    peer_sid = touch['sid']
                    if peer_sid == me:
                        continue
                    age = now - touch['ts']
                    if age > _TOUCH_WINDOW_S:
                        continue
                    conflict = is_write and age < _CONFLICT_WINDOW_S
                    dedup_key = (peer_sid, scope)
                    if (not conflict and
                            now - served.get(dedup_key, 0) < _ADVISORY_DEDUP_S):
                        continue
                    peer = (sessions or {}).get(peer_sid) or {}
                    candidates.append({
                        '_sid': peer_sid,
                        '_key': dedup_key,
                        'label': peer.get('label', peer_sid),
                        'scope': scope,
                        'tool': touch['tool'],
                        'age_s': round(age, 1),
                        'conflict': conflict,
                    })
                    break  # newest relevant touch per scope suffices
            # One entry per peer: conflicts first, then op-path scopes over
            # file: scopes (more actionable), then newest. Extra scopes for
            # the same peer are redundant token weight. Mark served ONLY
            # what is actually emitted, so collapsed entries surface later.
            candidates.sort(key=lambda a: (
                not a['conflict'],
                0 if a['scope'].startswith('/') else 1,
                a['age_s']))
            advisories = []
            seen_peers = set()
            for cand in candidates:
                if cand['_sid'] in seen_peers:
                    continue
                seen_peers.add(cand['_sid'])
                served[cand['_key']] = now
                advisories.append({k: v for k, v in cand.items()
                                   if not k.startswith('_')})
                if len(advisories) >= 3:
                    break
            if len(served) > 128:
                for old_key in sorted(served, key=served.get)[:64]:
                    del served[old_key]
        if advisories:
            result['_peers'] = advisories
            if me not in self._peer_hint_served:
                result['_hint'] = 'load /multi-session-etiquette'
                self._peer_hint_served.add(me)
            if any(a['conflict'] for a in advisories):
                worst = advisories[0]
                self._log(
                    'CONFLICT WARNING: session "{}" wrote {} ({}s ago) -- '
                    'coordinate before continuing'.format(
                        worst['label'], worst['scope'], worst['age_s']),
                    'WARNING')

    def _baselineLogCursor(self, sid):
        """On first sight of a session (main thread, BEFORE executing its
        operation), start its cursor at the current end of the log ring so
        it is served exactly the warnings its own operations generate from
        here on -- not the whole ring's history, and not nothing."""
        key = sid or '_anon'
        if key in self._log_cursors:
            return
        log_buffer = getattr(op.Embody.ext.Embody, '_log_buffer', None)
        latest = log_buffer[-1]['id'] if log_buffer else 0
        # Crude cap so a long-lived TD session accumulating many
        # short-lived sids cannot grow unbounded; re-serving up to 8
        # warnings once after a clear is harmless.
        if len(self._log_cursors) > 64:
            self._log_cursors.clear()
        self._log_cursors[key] = latest

    def _attachNotableLogs(self, result, sid=None):
        """Piggyback only WARNING/ERROR logs onto a response, capped small, to
        keep MCP responses token-lean. Cursors are PER SESSION (sid from the
        bridge headers; '_anon' for direct clients): each session's cursor
        advances over ALL entries new to IT, so one session polling cannot
        consume warnings meant for another. The full INFO/DEBUG/SUCCESS
        history is available on demand via the get_logs tool."""
        log_buffer = getattr(op.Embody.ext.Embody, '_log_buffer', None)
        if not log_buffer:
            return
        key = sid or '_anon'
        last_served = self._log_cursors.get(key, 0)
        recent = [e for e in log_buffer
                  if e['id'] > last_served]
        if not recent:
            return
        self._log_cursors[key] = recent[-1]['id']
        notable = [e for e in recent
                   if e.get('level') in ('WARNING', 'ERROR')]
        if notable:
            result['_logs'] = notable[-8:]

    def _send_response(self, request_id, result, sid=None):
        """Send a response back to the worker thread (token-lean log piggyback)."""
        self._attachNotableLogs(result, sid)

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
                # queue.Empty -- no more pending requests this frame
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
            sid = info.get('sid')

            # Baseline this session's log cursor BEFORE executing, so the
            # response carries the warnings THIS operation generates.
            self._baselineLogCursor(sid)

            # Multi-session Phase 3: destructive-op gate. Refusal is
            # instant and skips execution entirely.
            try:
                gate = self._checkDestructiveGate(sid, operation, params)
            except Exception:
                gate = None
            if gate is not None:
                if isinstance(request_id, int) and request_id >= 0:
                    self._send_response(request_id, gate, sid)
                else:
                    # Deferred op (run_tests uses the -1 sentinel): deliver
                    # the refusal through its dedicated event holder.
                    pending = getattr(sys, '_envoy_pending_test', None)
                    if pending:
                        pending['holder']['result'] = gate
                        pending['event'].set()
                continue

            self._log(f'Processing: {operation}')

            result = self._execute_operation(operation, params)

            # Multi-session Phase 2: record write touches and gather peer
            # advisories. Never let awareness break the operation itself.
            try:
                scopes = self._expandFileScopes(
                    _scopes_for_operation(operation, params, result))
                # Failed operations didn't mutate -- don't record them as
                # writes. Exception: a failed batch may have partially
                # succeeded (stops on first error), so it still counts.
                failed = isinstance(result, dict) and 'error' in result
                if not failed or operation == 'batch_operations':
                    self._recordTouches(sid, operation, scopes)
            except Exception:
                scopes = []

            # Deferred operations (e.g. run_tests) return None --
            # the worker thread handles its own response via Event
            if result is None:
                continue

            try:
                self._attachPeerAdvisories(result, sid, operation, scopes)
            except Exception:
                pass

            self._send_response(request_id, result, sid)

        # Live build visualization (opt-in): camera follow + node pulse + the
        # dancing builder-bot. Runs every frame AFTER the drain loop. Wrapped so
        # visualization can NEVER break the refresh loop.
        try:
            self._vizTick()
        except Exception:
            pass

    def _onServerSuccess(self, returnValue=None):
        """SuccessHook - Called when the thread task completes successfully"""
        self._log('Server thread exited')
        self.ownerComp.store('envoy_running', False)
        self.current_task = None
        self._starting = False
        if self.ownerComp.par.Envoyenable.eval() and not self.ownerComp.ext.Embody._performMode:
            self._scheduleRestart('Server exited unexpectedly')
        # If Envoyenable is already off, Stop() set the status -- don't overwrite

    def _onServerError(self, error):
        """ExceptHook - Called when the thread task errors"""
        self._log(f'Server error: {error}', 'ERROR')
        self.ownerComp.store('envoy_running', False)
        self.current_task = None
        self._starting = False
        if self.ownerComp.par.Envoyenable.eval() and not self.ownerComp.ext.Embody._performMode:
            self._scheduleRestart(f'Server error: {error}')

    def _scheduleRestart(self, reason: str):
        """Auto-restart the MCP server with exponential backoff, retrying for up
        to _RESTART_WINDOW_SECONDS (30 min) before giving up. Replaces the old
        3-strike / ~6-second cap, which a transient port-rebind race could trip
        permanently -- then disable Envoy and force a manual toggle."""
        now = time.time()
        uptime = now - self._last_start_time
        # A NEW storm: either the very first failure, or the server had been
        # stable long enough that this death is unrelated to the last streak.
        if self._restart_window_start == 0.0 or uptime > self._RESTART_RESET_SECONDS:
            self._restart_count = 0
            self._restart_window_start = now

        elapsed = now - self._restart_window_start
        if elapsed > self._RESTART_WINDOW_SECONDS:
            mins = int(self._RESTART_WINDOW_SECONDS // 60)
            self._log(
                f'Server kept failing for over {mins} min '
                f'({self._restart_count} attempts) -- giving up. Last: {reason}. '
                f'Toggle Envoy off/on to retry.', 'ERROR')
            self.ownerComp.par.Envoystatus = f'Error: {reason} (gave up after {mins} min)'
            self.ownerComp.par.Envoyenable = False
            return

        self._restart_count += 1
        # Exponential backoff: 1, 2, 4, 8, ... seconds, capped at the max gap.
        delay = min(self._RESTART_BACKOFF_MAX,
                    self._RESTART_BACKOFF_BASE * (2 ** (self._restart_count - 1)))
        remaining = max(0, int((self._RESTART_WINDOW_SECONDS - elapsed) // 60))
        self._log(
            f'Auto-restarting server (attempt {self._restart_count}, retry in '
            f'{delay:.0f}s, ~{remaining} min left in retry window): {reason}', 'WARNING')
        self.ownerComp.par.Envoystatus = (
            f'Restarting (attempt {self._restart_count}, ~{remaining} min left)...')
        run(f"op('{self.ownerComp.path}').ext.Envoy.Start()",
            fromOP=self.ownerComp, delayMilliSeconds=int(delay * 1000))

    # === Operation Routing ===

    # Operations whose handlers mutate TD state. Each top-level call is wrapped
    # in one ui.undo block so the user can Ctrl+Z anything an agent does
    # (adapted from Derivative's TDMCP, with permission). Read-only ops,
    # run_tests (deferred across frames -- an undo block must never span
    # frames), cook_op (cooking is not an undoable edit), and disk-only ops
    # (export_network, save_externalization) stay unwrapped.
    _UNDOABLE_OPS = frozenset({
        'create_op', 'delete_op', 'copy_op', 'rename_op',
        'set_parameter', 'connect_ops', 'disconnect_op',
        'execute_python', 'set_dat_content', 'edit_dat_content',
        'set_op_flags', 'set_op_position', 'layout_children',
        'exec_op_method', 'externalize_op', 'remove_externalization_tag',
        'create_extension', 'import_network',
        'create_annotation', 'set_annotation',
        'batch_operations',
    })

    def _execute_operation(self, operation: str, params: dict) -> dict:
        """Route operation to appropriate handler"""
        # 'override' belongs to the multi-session gate, not the handlers
        # (dispatch is handler(**params)); strip it here so batch
        # sub-operations that loop back through are covered too.
        if params and 'override' in params:
            params = {k: v for k, v in params.items() if k != 'override'}
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
            'edit_dat_content': self._edit_dat_content,
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
            # Documentation root discovery for worker-side get_docs
            'get_docs_roots': self._get_docs_roots,
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
            'read_tdn': self._read_tdn,
            'diff_tdn': self._diff_tdn,
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
            # Batch
            'batch_operations': self._batch_operations,
        }

        handler = handlers.get(operation)
        if handler:
            try:
                # Pre-risky: durably checkpoint the touched TDN root BEFORE a
                # destructive delete so an agent-induced crash during it loses
                # nothing since it. Best-effort, ~6ms. NOT for import_network: its
                # .tdn is the user's source-of-truth being reloaded (the canonical
                # TDN edit->import workflow), so writing the live state over it
                # would corrupt the edit.
                if operation == 'delete_op':
                    try:
                        op.Embody.ext.Embody._preRiskyCheckpoint(operation, params)
                    except Exception:
                        pass
                undo_open = self._beginUndoBlock(operation)
                try:
                    result = handler(**params)
                finally:
                    if undo_open:
                        self._endUndoBlock()
                # Record where Envoy is building for the re-center camera.
                # Routed here (not at the _onRefresh chokepoint) so each sub-op
                # of a batch_operations call is seen -- batches loop back through
                # _execute_operation. Best-effort; never affects the response.
                self._noteVizActivity(operation, params, result)
                self._noteCheckpointActivity(operation, params, result)
                return result
            except Exception as e:
                self._log(f'Operation {operation} failed: {e}', 'ERROR')
                return {'error': str(e)}
        return {'error': f'Unknown operation: {operation}'}

    def _beginUndoBlock(self, operation: str) -> bool:
        if operation not in self._UNDOABLE_OPS or self._undo_active:
            return False
        try:
            ui.undo.startBlock(f'Envoy {operation}')
            self._undo_active = True
            return True
        except Exception as e:
            self._log(f'Could not start undo block for {operation}: {e}', 'WARNING')
            return False

    def _endUndoBlock(self) -> None:
        self._undo_active = False
        try:
            ui.undo.endBlock()
        except Exception as e:
            # annotateCOMP creation tears down open undo blocks TD-internally,
            # making endBlock raise "Cannot end non existent undo operation"
            # (gotcha documented by TDMCP); never break dispatch for that.
            self._log(f'Could not end undo block: {e}', 'DEBUG')

    # === Live Build Visualization: smooth follow + navigate to the active op ===
    # Embot mascot + network-editor camera follow. The whole subsystem now lives
    # in the envoy_viz module DAT (mod.envoy_viz); all _VIZ_* constants moved
    # there. State (self._viz_*) stays on this ext (see __init__). The methods
    # below are delegating stubs -- external callers (execute.py _vizCleanup,
    # _onRefresh ticks, the dispatch chokepoint) keep working.
    # Main-thread only.

    def _noteVizActivity(self, operation: str, params: dict, result) -> None:
        """Enqueue the op Envoy just acted on as a follow hop -- see envoy_viz.

        Never raises: viz is decoration, and this runs at the dispatch
        chokepoint where an escaping exception would fail the tool call
        itself -- the guard must cover the mod.envoy_viz lookup too (a
        broken/renamed module DAT must not take Envoy down with it)."""
        try:
            return mod.envoy_viz.noteVizActivity(self, operation, params, result)
        except Exception:
            pass

    # Ops that change exported .tdn content and should arm an auto-save checkpoint.
    # BROADER than _VIZ_MUTATING_OPS: includes delete/disconnect/layout/annotation
    # ops (which mutate structure but have no camera target). execute_python /
    # exec_op_method are deliberately EXCLUDED (A1 skip+document -- the touched COMP
    # is unknowable; their edits are captured by the next typed op / Ctrl+S).
    _CHECKPOINT_MUTATING_OPS = frozenset({
        'create_op', 'delete_op', 'set_parameter', 'connect_ops', 'disconnect_op',
        'copy_op', 'rename_op', 'set_op_flags', 'set_op_position', 'layout_children',
        'set_dat_content', 'edit_dat_content', 'create_annotation', 'set_annotation',
        'create_extension', 'import_network', 'externalize_op', 'save_externalization',
        'remove_externalization_tag',
    })

    def _noteCheckpointActivity(self, operation: str, params: dict, result) -> None:
        """Arm the auto-save touched-set off the single MCP chokepoint. Best-effort,
        never raises -- a failure here must never affect the tool response."""
        try:
            if operation not in self._CHECKPOINT_MUTATING_OPS:
                return
            path = self._resolveActiveOp(operation, params, result)
            if not path:
                # delete_op leaves no live op; fall back to the param path string.
                path = (params.get('op_path') or params.get('target_path')
                        or params.get('dest_path') or params.get('parent_path'))
            if path:
                op.Embody.ext.Embody.NoteCheckpointTouch(path)
        except Exception:
            pass

    # What each operator type just DID. Embot narrates a node he has already built
    # and is standing ON, so the copy is PAST TENSE ("marked the output") -- present
    # continuous ("marking") reads as outdated the instant he lands on the finished
    # node.
    _OP_DESCRIPTIONS = {
        # TOPs
        'noiseTOP': 'seeded a noise texture',
        'rampTOP': 'laid down a gradient',
        'constantTOP': 'filled a solid colour',
        'transformTOP': 'repositioned the image',
        'blurTOP': 'softened it with a blur',
        'levelTOP': 'graded brightness & contrast',
        'edgeTOP': 'traced the edges',
        'compositeTOP': 'blended two layers',
        'hsvadjustTOP': 'shifted hue & saturation',
        'feedbackTOP': 'fed the output back in',
        'glslTOP': 'ran a GLSL shader',
        'renderTOP': 'rendered the scene',
        'nullTOP': 'marked the output',
        'outTOP': 'exposed the output',
        # CHOPs
        'lfoCHOP': 'set an oscillator going',
        'mathCHOP': 'scaled the signal',
        'filterCHOP': 'smoothed the motion',
        'noiseCHOP': 'added some jitter',
        'nullCHOP': 'marked the channel output',
        # SOPs
        'gridSOP': 'built a point grid',
        'noiseSOP': 'displaced the geometry',
        'transformSOP': 'transformed the points',
        'nullSOP': 'marked the geometry output',
        # POPs
        'gridPOP': 'built GPU points',
        'noisePOP': 'displaced them on the GPU',
        'nullPOP': 'marked the POP output',
        # MATs / COMPs / DATs
        'phongMAT': 'set up a phong material',
        'geometryCOMP': 'placed geometry to render',
        'cameraCOMP': 'set up the camera',
        'lightCOMP': 'added a light',
        'baseCOMP': 'opened a sub-network',
        'webclientDAT': 'wired up a web client',
        'textDAT': 'dropped in a text DAT',
    }

    def _actionText(self, operation: str, path: str) -> str:
        """What Embot says about the node he just finished and is standing on. PAST
        tense throughout: his comment if one is set, else what that op type did, else
        a plain past-tense verb. Never present-continuous -- he has already done it."""
        try:
            o = op(path)
            if o is not None:
                note = (o.comment or '').strip()
                if note:
                    return note
                desc = self._OP_DESCRIPTIONS.get(o.OPType)   # OPType = 'noiseTOP'; .type = 'noise'
                if desc:
                    return desc
        except Exception:
            pass
        verbs = {'create_op': 'built', 'connect_ops': 'wired up',
                 'set_parameter': 'tuned', 'import_network': 'rebuilt'}
        return '%s %s' % (verbs.get(operation, 'worked on'), path.rsplit('/', 1)[-1])

    def _resolveActiveOp(self, operation: str, params: dict, result) -> Optional[str]:
        """Best-effort path of the single op to move to. Prefers the path the
        handler reports (a freshly created op), else the param target."""
        try:
            if isinstance(result, dict):
                for k in ('path', 'new_path', 'comp_path'):
                    v = result.get(k)
                    if v:
                        return v
            if operation == 'connect_ops':
                return params.get('dest_path')
            if operation == 'import_network':
                return params.get('target_path')
            return params.get('op_path')
        except Exception:
            return None

    def _crashTrace(self, msg: str) -> None:
        """Append a FLUSHED breadcrumb so the LAST viz annotation-graph op before a
        hard TD crash survives on disk (logs/embot_crash_trace.log). flush() (no fsync)
        is enough -- a TD process crash leaves kernel-buffered writes intact; we trade
        the cost of fsync to keep frame timing close to normal. Gated on
        _crash_trace_enabled (off in normal use). Never raises."""
        if not self._crash_trace_enabled:
            return
        try:
            f = self._crash_trace_f
            if f is None:
                import os
                d = os.path.join(project.folder, 'logs')
                os.makedirs(d, exist_ok=True)
                f = open(os.path.join(d, 'embot_crash_trace.log'), 'a')
                self._crash_trace_f = f
            f.write('f%d %.3f %s\n' % (absTime.frame, absTime.seconds, msg))
            f.flush()
        except Exception:
            pass

    def _vizTick(self) -> None:
        """Once-per-frame visualization driver -- see envoy_viz."""
        return mod.envoy_viz.vizTick(self)

    def _vizPumpQueue(self, now: float) -> None:
        """Advance queued follow hops one at a time -- see envoy_viz."""
        return mod.envoy_viz.vizPumpQueue(self, now)

    def _trackActive(self, now: float, follow: bool, show_bot: bool) -> None:
        """Stand Embot on / pan camera to the active op -- see envoy_viz."""
        return mod.envoy_viz.trackActive(self, now, follow, show_bot)

    def _pickFollowPane(self, net: 'COMP'):
        """Choose the pane to follow a network in -- see envoy_viz."""
        return mod.envoy_viz.pickFollowPane(self, net)

    def _userTookOver(self, pane) -> bool:
        """True while the user has navigated the follow pane away -- see envoy_viz."""
        return mod.envoy_viz.userTookOver(self, pane)

    def _navigateAndFrame(self, pane, net: 'COMP', target: 'OP') -> None:
        """Cut a pane into a network and frame the target op -- see envoy_viz."""
        return mod.envoy_viz.navigateAndFrame(self, pane, net, target)

    def _glideStep(self, pane, target: 'OP') -> None:
        """One eased frame of the camera glide toward the op -- see envoy_viz."""
        return mod.envoy_viz.glideStep(self, pane, target)

    def _highlightOp(self, target: 'OP') -> None:
        """Select + make-current the op being worked -- see envoy_viz."""
        return mod.envoy_viz.highlightOp(self, target)

    def _pulseStart(self, target: 'OP', now: float) -> None:
        """Begin a colour pulse on the active op -- see envoy_viz."""
        return mod.envoy_viz.pulseStart(self, target, now)

    def _pulseTick(self, now: float) -> None:
        """Fade the active pulse back to the op's colour -- see envoy_viz."""
        return mod.envoy_viz.pulseTick(self, now)

    def _restorePulse(self) -> None:
        """Restore the pulsing op's original colour -- see envoy_viz."""
        return mod.envoy_viz.restorePulse(self)

    def _placeBot(self, net: 'COMP', target: 'OP', now: float) -> None:
        """Bring Embot to stand on the active op -- see envoy_viz."""
        return mod.envoy_viz.placeBot(self, net, target, now)

    def _stageOffset(self, net: 'COMP') -> float:
        """Off-view staging offset for spread assembly -- see envoy_viz."""
        return mod.envoy_viz.stageOffset(self, net)

    def _botFootGap(self) -> float:
        """Figure-centre to feet distance -- see envoy_viz."""
        return mod.envoy_viz.botFootGap(self)

    def _ensureTemplate(self):
        """Build/return Embot's source template -- see envoy_viz."""
        return mod.envoy_viz.ensureTemplate(self)

    def _ensureBot(self, net: 'COMP') -> bool:
        """Ensure Embot is present/assembling in a network -- see envoy_viz."""
        return mod.envoy_viz.ensureBot(self, net)

    def _netIsDisplayed(self, net: 'COMP') -> bool:
        """True if a network-editor pane shows the net -- see envoy_viz."""
        return mod.envoy_viz.netIsDisplayed(self, net)

    def _blockSpawn(self, net: 'COMP') -> None:
        """One-shot block copy of all parts into an off-screen net -- see envoy_viz."""
        return mod.envoy_viz.blockSpawn(self, net)

    def _assembleStep(self, net: 'COMP') -> None:
        """Copy one queued template part into the net -- see envoy_viz."""
        return mod.envoy_viz.assembleStep(self, net)

    def _assembleTick(self) -> None:
        """Drive Embot's spread assembly per frame -- see envoy_viz."""
        return mod.envoy_viz.assembleTick(self)

    def _startEntrance(self) -> None:
        """Fire Embot's swoop from staging onto his op -- see envoy_viz."""
        return mod.envoy_viz.startEntrance(self)

    def _cleanupDeadBots(self) -> None:
        """Tear down a left-behind bot, one net per frame -- see envoy_viz."""
        return mod.envoy_viz.cleanupDeadBots(self)

    def _botDance(self, now: float) -> None:
        """Animate the figure: hop, hover, gesture, colour -- see envoy_viz."""
        return mod.envoy_viz.botDance(self, now)

    def _botUnsafeNet(self, net: 'COMP') -> bool:
        """True if a bot must not be created in the net -- see envoy_viz."""
        return mod.envoy_viz.botUnsafeNet(self, net)

    def _destroyBot(self) -> None:
        """Remove all figure parts if present -- see envoy_viz."""
        return mod.envoy_viz.destroyBot(self)

    def _vizCleanup(self) -> None:
        """Retire all live visualization artifacts -- see envoy_viz."""
        return mod.envoy_viz.vizCleanup(self)

    def _viewTuple(self, pane) -> tuple:
        """Comparable snapshot of a pane's view state -- see envoy_viz."""
        return mod.envoy_viz.viewTuple(self, pane)

    def _recordView(self, pane) -> None:
        """Remember what we last set the pane to -- see envoy_viz."""
        return mod.envoy_viz.recordView(self, pane)

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
        stored on sys -- the main-thread poll signals it when tests finish.
        This bypasses the response_queue entirely, surviving server restarts
        and extension reinit.

        Returns None on success (deferred). On error, signals the worker
        thread directly via the Event and returns None -- never returns a
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
            return None  # Deferred -- worker thread waits on sys._envoy_pending_test['event']
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
            # Token-lean: drop the per-test PASS objects (the full suite is
            # ~1400 of them) -- keep the counts and only the failures/errors.
            # Full per-test detail is in the test log file under dev/logs/.
            if isinstance(result.get('results'), list):
                result['results'] = [r for r in result['results']
                                     if r.get('status') != 'PASS']
            self._attachNotableLogs(result)
            # Signal the worker thread directly via the Event
            pending['holder']['result'] = result
            pending['event'].set()
        else:
            self._schedulePollTestCompletion()

    # --- Batch Operations ---

    def _batch_operations(self, operations: list) -> dict:
        """Execute multiple operations sequentially in one request.

        Each entry is {'tool': str, 'params': dict}. Stops on first error.
        Returns {'success': bool, 'results': [...], 'count': int}.
        """
        if not isinstance(operations, list):
            return {'error': 'operations must be a list'}
        results = []
        for i, op_spec in enumerate(operations):
            if not isinstance(op_spec, dict) or 'tool' not in op_spec:
                results.append({'error': f'Invalid operation at index {i}'})
                break
            tool = op_spec['tool']
            params = op_spec.get('params', {})
            if tool == 'batch_operations':
                results.append({'error': 'Nested batch_operations not allowed'})
                break
            result = self._execute_operation(tool, params)
            results.append(result)
            if 'error' in result:
                break
        return {
            'success': not any('error' in r for r in results),
            'results': results,
            'count': len(results),
        }

    # --- Operator Management ---

    def _create_op(self, parent_path: str, op_type: str, name: str = None) -> dict:
        """Create an operator -- see envoy_ops."""
        return mod.envoy_ops.create_op(self, parent_path, op_type, name)

    def _delete_op(self, op_path: str) -> dict:
        """Delete an operator -- see envoy_ops."""
        return mod.envoy_ops.delete_op(self, op_path)

    def _get_op(self, op_path: str, include_defaults: bool = False) -> dict:
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
        parameters_omitted = 0
        for p in target.pars():
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
                self._log(f'Could not read parameter {p.name} on {op_path}: {e}', 'DEBUG')
                params[p.name] = {'value': 'N/A', 'mode': 'N/A'}
        info['parameters'] = params
        if parameters_omitted > 0:
            info['parameters_omitted'] = parameters_omitted

        # Get inputs/outputs
        info['inputs'] = [inp.path if inp else None for inp in target.inputs]
        info['outputs'] = [out.path if out else None for out in target.outputs]

        # COMP-specific info
        if hasattr(target, 'children'):
            info['children'] = [child.name for child in target.children]

        return self._maybe_offload_to_file(info, 'get_op')

    _SEQ_PAR_RE = re.compile(r'^([A-Za-z]+?)(\d+)([A-Za-z0-9]*)$')

    def _set_parameter(self, op_path: str, par_name: str, value=None,
                      mode: str = None, expr: str = None,
                      bind_expr: str = None) -> dict:
        """Set a parameter value, expression, bind expression, or mode -- see envoy_ops."""
        return mod.envoy_ops.set_parameter(self, op_path, par_name, value, mode, expr, bind_expr)

    def _get_parameter(self, op_path: str, par_name: str = None,
                      search: str = None, search_in: str = 'any',
                      depth: int = 2, max_results: int = 50,
                      details: bool = False) -> dict:
        """Get a parameter value with full details"""
        target = op(op_path)
        if not target:
            return {'error': f'Operator not found: {op_path}'}

        if search is not None:
            valid_search = ('name', 'value', 'expr', 'any')
            search_in = (search_in or 'any').lower()
            if search_in not in valid_search:
                return {'error': 'Invalid search_in. Use: name, value, expr, any'}
            try:
                depth = int(depth)
            except Exception:
                depth = 2
            depth = max(0, depth)
            try:
                max_results = int(max_results)
            except Exception:
                max_results = 50
            max_results = min(500, max(1, max_results))

            # Finding absolute-path expressions project-wide is an Embody
            # code-review rule turned into a query (idea from TDMCP).
            pattern = str(search).lower()
            if not any(ch in pattern for ch in '*?['):
                pattern = f'*{pattern}*'

            ops_to_scan = [target]
            try:
                ops_to_scan.extend(target.findChildren(maxDepth=depth))
            except Exception:
                pass

            hits = []
            truncated = False
            for o in ops_to_scan:
                try:
                    pars = o.pars()
                except Exception:
                    continue
                for p in pars:
                    try:
                        mode_name = p.mode.name
                    except Exception:
                        mode_name = str(getattr(p, 'mode', ''))

                    matched = False
                    if search_in in ('name', 'any'):
                        matched = fnmatch.fnmatch(p.name.lower(), pattern)

                    value_text = None
                    if search_in in ('value', 'any') and not matched:
                        if search_in == 'any' and mode_name != 'CONSTANT':
                            pass
                        else:
                            try:
                                value_text = str(p.eval())
                                matched = fnmatch.fnmatch(value_text.lower(), pattern)
                            except Exception:
                                pass

                    if search_in in ('expr', 'any') and not matched:
                        if mode_name == 'EXPRESSION':
                            try:
                                matched = fnmatch.fnmatch(str(p.expr).lower(), pattern)
                            except Exception:
                                pass
                        elif mode_name == 'BIND':
                            try:
                                matched = fnmatch.fnmatch(str(p.bindExpr).lower(), pattern)
                            except Exception:
                                pass

                    if not matched:
                        continue
                    if value_text is None:
                        try:
                            value_text = str(p.eval())
                        except Exception as e:
                            value_text = f'<eval error: {e}>'
                    hit = {
                        'op': o.path,
                        'par': p.name,
                        'value': value_text,
                        'mode': mode_name,
                    }
                    if mode_name == 'EXPRESSION':
                        try:
                            hit['expr'] = p.expr
                        except Exception:
                            pass
                    elif mode_name == 'BIND':
                        try:
                            hit['bindExpr'] = p.bindExpr
                        except Exception:
                            pass
                    hits.append(hit)
                    if len(hits) >= max_results:
                        truncated = True
                        break
                if truncated:
                    break

            result = {
                'root': op_path,
                'pattern': search,
                'search_in': search_in,
                'count': len(hits),
                'results': hits,
            }
            if truncated:
                result['truncated'] = True
            return result

        if par_name is None:
            return {'error': 'Provide par_name, or search for pattern mode'}

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
            }
            if details:
                result.update({
                    'default': str(par.default),
                    'isCustom': par.isCustom,
                    'readOnly': par.readOnly,
                    'style': par.style,
                })

            # Mode-specific details
            if par.mode.name == 'EXPRESSION':
                result['expression'] = par.expr
            elif par.mode.name == 'BIND':
                result['bindExpr'] = par.bindExpr
                result['bindMaster'] = par.bindMaster.path if par.bindMaster else None
            elif par.mode.name == 'EXPORT':
                result['exportOP'] = par.exportOP.path if par.exportOP else None
                if details:
                    result['exportSource'] = str(par.exportSource) if par.exportSource else None

            # Numeric range info
            if details and par.isNumber:
                result['min'] = par.min
                result['max'] = par.max
                result['clampMin'] = par.clampMin
                result['clampMax'] = par.clampMax
                result['normMin'] = par.normMin
                result['normMax'] = par.normMax

            # Menu info
            if par.isMenu:
                result['menuNames'] = par.menuNames
                if details:
                    result['menuLabels'] = par.menuLabels
                    result['menuIndex'] = par.menuIndex

            return result
        except Exception as e:
            return {'error': f'Failed to get parameter: {e}'}

    def _connect_ops(self, source_path: str, dest_path: str,
                          source_index: int = 0, dest_index: int = 0,
                          comp: bool = False) -> dict:
        """Connect two operators -- see envoy_ops."""
        return mod.envoy_ops.connect_ops(self, source_path, dest_path, source_index, dest_index, comp)

    def _disconnect_op(self, op_path: str, input_index: int = 0,
                            comp: bool = False) -> dict:
        """Disconnect an operator's input -- see envoy_ops."""
        return mod.envoy_ops.disconnect_op(self, op_path, input_index, comp)

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
        """Copy an operator -- see envoy_ops."""
        return mod.envoy_ops.copy_op(self, source_path, dest_parent, new_name)

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

    def _lintLayout(self, comp):
        """Layout lint for a COMP's direct children -- see envoy_layout."""
        return mod.envoy_layout.lint_layout(comp)

    def _lintNewOps(self, pre_paths):
        """After execute_python, WARN if it left newly-created ops piled at
        (0,0) or overlapping in their parent COMP. The warning rides back on the
        response via _attachNotableLogs, so network-layout.md is enforced at the
        tool layer instead of relying on the caller to run the Verify step."""
        if pre_paths is None:
            return
        try:
            new_parents = {}
            new_ops = []
            for o in root.findChildren(maxDepth=12):
                if o.path in pre_paths:
                    continue
                new_ops.append(o)
                par = o.parent()
                if par is not None:
                    new_parents.setdefault(par.path, par)
            # Auto-hug scattered docks of ops created by THIS call before
            # linting: TD drops a new host's shader/callback/info DATs at
            # arbitrary coordinates, and raw comp.create() never fixes them.
            # Only scattered rows are touched, so a deliberate near-host
            # placement (e.g. docks to the host's right) is left alone.
            hugged = 0
            for o in new_ops:
                docks = self._sameNetworkDocks(o)
                if docks and any(abs(d.nodeX - o.nodeX) > 350
                                 or abs(d.nodeY - o.nodeY) > 350 for d in docks):
                    hugged += self._placeDockedOps(o)
            if hugged:
                self._log(
                    'LAYOUT: auto-hugged %d scattered docked op(s) below their '
                    'newly-created host(s); re-run get_network_layout if you '
                    'planned positions around them.' % hugged, 'WARNING')
            for par in new_parents.values():
                issues = self._lintLayout(par)
                if issues:
                    self._log(
                        'LAYOUT WARNING: ' + par.path + ' -- ' + '; '.join(issues)
                        + '. execute_python does NOT auto-position ops (create_op does); '
                        'run get_network_layout and reposition per network-layout.md.',
                        'WARNING')
            self._warnAutoExternalizeBypass(new_ops)
        except Exception:
            pass

    def _warnAutoExternalizeBypass(self, new_ops):
        """When the Autoexternalize preference is on, ops created via
        execute_python bypass it -- only create_op is the auto-externalize
        chokepoint. Warn for any new op that WOULD have been auto-externalized
        (uses EmbodyExt's pure boundary decision, so no false positives on ops
        already captured by an externalized ancestor), steering callers to
        create_op as the preferred creation path."""
        try:
            emb = op.Embody.ext.Embody
            if op.Embody.par.Autoexternalize.eval() == 'neither':
                return
            bypassed = [o.path for o in new_ops if emb._autoExternalizeTagFor(o)]
            if bypassed:
                shown = ', '.join(bypassed[:5]) + ('...' if len(bypassed) > 5 else '')
                self._log(
                    'AUTO-EXTERNALIZE BYPASS: ' + str(len(bypassed)) + ' op(s) created '
                    'via execute_python were NOT auto-externalized (' + shown + '). '
                    'create_op is the preferred creation path and auto-externalizes; '
                    'recreate via create_op, or tag manually with externalize_op.',
                    'WARNING')
        except Exception:
            pass

    def _execute_python(self, code: str) -> dict:
        """Execute arbitrary Python code"""
        code_preview = code[:200] + ('...' if len(code) > 200 else '')
        self._log(f'execute_python: {code_preview}')
        try:
            # Snapshot op paths so we can lint ONLY the ops this call creates.
            # execute_python uses raw comp.create()/copy() (no auto-position),
            # the exact path that keeps dropping ops at (0,0); _lintNewOps below
            # turns that into a WARNING on the response.
            try:
                # Matched pair with _rollbackNewOps maxDepth; ops created
                # deeper than the snapshot depth are invisible to rollback.
                pre_paths = set(o.path for o in root.findChildren(maxDepth=20))
            except Exception:
                pre_paths = None

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
            self._lintNewOps(pre_paths)
            if result is not None:
                return {'success': True, 'result': str(result)}
            return {'success': True}
        except Exception as e:
            self._log(f'execute_python failed: {e}', 'ERROR')
            removed = self._rollbackNewOps(pre_paths)
            msg = f'Execution failed: {e}'
            if removed:
                msg += f' (rolled back {removed} operator(s) the script created before failing)'
            return {'error': msg}

    def _rollbackNewOps(self, pre_paths) -> int:
        """A failed execute_python must not leave a half-built network: destroy
        ops the script created before the exception (documented contract in
        rules/td-ui.md). Parameter changes to PRE-EXISTING ops are NOT rolled
        back -- only creations. Best-effort; returns count destroyed."""
        count = 0
        if pre_paths is None:
            return 0
        try:
            post = []
            # Matched pair with _execute_python snapshot maxDepth; ops created
            # deeper than the snapshot depth are invisible to rollback.
            for o in root.findChildren(maxDepth=20):
                try:
                    if o.valid and o.path not in pre_paths:
                        post.append(o)
                except Exception:
                    pass
            post.sort(key=lambda o: o.path.count('/'))
            destroyed_roots = []
            for o in post:
                try:
                    path = o.path
                    if any(path.startswith(root_path + '/')
                           for root_path in destroyed_roots):
                        continue
                    if not o.valid:
                        continue
                    o.destroy()
                    destroyed_roots.append(path)
                    count += 1
                except Exception:
                    pass
        except Exception:
            pass
        return count

    # === Introspection & Diagnostics (Main Thread Only) ===

    def _get_docs_roots(self) -> dict:
        """Candidate offline-help mirror locations (App Class, main thread)."""
        try:
            samples = str(app.samplesFolder).replace('\\', '/').rstrip('/')
            roots = [samples + '/Learn/offlineHelp/https.docs.derivative.ca']
            return {'roots': roots}
        except Exception as e:
            return {'roots': [], 'error': f'Failed to get docs roots: {e}'}

    def _get_td_info(self) -> dict:
        """Get TouchDesigner environment and Envoy server info"""
        try:
            import td as _td
            version = _td.app.version
            build = _td.app.build
            # app.osName reports "Windows 10" on Win 11 (same NT 10.0 kernel);
            # EmbodyExt._osLabel() disambiguates via the build number.
            try:
                os_name = self.ownerComp.ext.Embody._osLabel()
            except Exception:
                os_name = _td.app.osName
            return {
                'server': f'TouchDesigner {version}.{build}',
                'version': f'{version}.{build}',
                'osName': os_name,
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
                        rows: list = None, clear: bool = False,
                        confirm_wipe: bool = False) -> dict:
        """Set DAT content from text or table rows -- see envoy_ops."""
        return mod.envoy_ops.set_dat_content(self, op_path, text, rows, clear, confirm_wipe)

    def _edit_dat_content(self, op_path: str, old_string: str,
                         new_string: str, replace_all: bool = False,
                         confirm_wipe: bool = False) -> dict:
        """Surgical text edit on a DAT -- see envoy_ops."""
        return mod.envoy_ops.edit_dat_content(self, op_path, old_string, new_string, replace_all, confirm_wipe)

    # === TOP Capture (Main Thread Only) ===

    def _capture_top(self, op_path: str, format: str = 'jpeg',
                     quality: float = 0.8, max_resolution: int = 640,
                     inline: bool = False, sample_grid: int = 0) -> dict:
        """Capture a TOP operator's output as a compressed image."""
        import base64

        target = op(op_path)
        if not target:
            return {'error': f'Operator not found: {op_path}'}
        if target.family != 'TOP':
            return {'error': f'{op_path} is not a TOP (family: {target.family})'}

        try:
            sample_grid = int(sample_grid or 0)
        except Exception:
            sample_grid = 0
        if sample_grid >= 2:
            return self._sample_grid_top(target, sample_grid)

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

    def _sample_grid_top(self, target, grid) -> dict:
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
        """Set flags on an operator -- see envoy_ops."""
        return mod.envoy_ops.set_op_flags(self, op_path, bypass, lock, display, render, viewer, current, expose, allowCooking, selected)

    # === Node Positioning & Layout (Main Thread Only) ===

    def _sameNetworkDocks(self, host):
        """Same-network docked companions -- see envoy_layout."""
        return mod.envoy_layout.same_network_docks(host)

    def _placeDockedOps(self, host):
        """Hug docked companions below their host -- see envoy_layout."""
        return mod.envoy_layout.place_docked_ops(host)

    def _find_non_overlapping_position(self, parent, new_op):
        """Auto-position clear of real siblings -- see envoy_layout."""
        return mod.envoy_layout.find_non_overlapping_position(parent, new_op)

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

            return self._maybe_offload_to_file(result, 'get_network_layout')

        except Exception as e:
            return {'error': f'Failed to get network layout: {e}'}

    def _set_op_position(self, op_path: str, x: int = None, y: int = None,
                        width: int = None, height: int = None,
                        color: list = None, comment: str = None) -> dict:
        """Set operator position and visual properties -- see envoy_ops."""
        return mod.envoy_ops.set_op_position(self, op_path, x, y, width, height, color, comment)

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
        """Create an annotation in the network editor -- see envoy_ops."""
        return mod.envoy_ops.create_annotation(self, parent_path, mode, text, title, x, y, width, height, color, opacity, name)

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

    def _resolve_annotation(self, op_path: str):
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
        parent = op(parent_path) if parent_path else None
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

    def _set_annotation(self, op_path: str, text: str = None, title: str = None,
                        color: list = None, opacity: float = None,
                        width: int = None, height: int = None,
                        x: int = None, y: int = None) -> dict:
        """Modify an existing annotation -- see envoy_ops."""
        return mod.envoy_ops.set_annotation(self, op_path, text, title, color, opacity, width, height, x, y)

    def _get_enclosed_ops(self, op_path: str) -> dict:
        """Get annotation/operator enclosure relationships."""
        target = self._resolve_annotation(op_path)
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
        """Rename an operator -- see envoy_ops."""
        return mod.envoy_ops.rename_op(self, op_path, new_name)

    def _cook_op(self, op_path: str, force: bool = True,
                      recurse: bool = False) -> dict:
        """Cook an operator -- see envoy_ops."""
        return mod.envoy_ops.cook_op(self, op_path, force, recurse)

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
        """Tag an operator for Embody externalization and write it to disk -- see envoy_ops."""
        return mod.envoy_ops.externalize_op(self, op_path, tag_type)

    def _remove_externalization_tag(self, op_path: str) -> dict:
        """Remove Embody externalization tag and clean up -- see envoy_ops."""
        return mod.envoy_ops.remove_externalization_tag(self, op_path)

    def _get_externalizations(self) -> dict:
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
                    'dirty': table[row, 'dirty'].val,
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

    def _save_externalization(self, op_path: str) -> dict:
        """Force save an externalized operator -- see envoy_ops."""
        return mod.envoy_ops.save_externalization(self, op_path)

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
                        'dirty': table[row, 'dirty'].val,
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

    # === Extension Creation (Main Thread Only) ===

    def _create_extension(self, parent_path: str, class_name: str,
                          name: str = None, code: str = None,
                          promote: bool = True, ext_name: str = None,
                          ext_index: int = None,
                          existing_comp: bool = False) -> dict:
        """Create a TD extension: COMP + text DAT + extension wiring -- see envoy_ops."""
        return mod.envoy_ops.create_extension(self, parent_path, class_name, name, code, promote, ext_name, ext_index, existing_comp)

    # === TDN Network Format (Main Thread Only) ===

    def _export_network(self, root_path='/', include_dat_content=True,
                       output_file=None, max_depth=None, embed_all=False):
        """Delegate to TDN extension for network export."""
        if not getattr(self.ownerComp.ext, 'TDN', None):
            return {'error': 'TDN extension not loaded on Embody COMP'}
        # Protect .tdn files belonging to other tracked TDN COMPs
        protected = self.ownerComp.ext.Embody._getAllTrackedTDNFiles(
            exclude_path=root_path) if output_file else None
        result = self.ownerComp.ext.TDN.ExportNetwork(
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

    def _import_network(self, target_path, tdn, clear_first=False):
        """Delegate to TDN extension for network import -- see envoy_ops."""
        return mod.envoy_ops.import_network(self, target_path, tdn, clear_first)

    def _read_tdn(self, comp_path='/', include_dat_content=None,
                  max_depth=None, embed_all=False):
        """Read a network subtree as a TDN dict (in-memory, no disk write).

        Thin delegate over TDN.ExportNetwork(output_file=None). Kept as a
        separate MCP tool so LLM-facing docs can emphasize the token-cost
        win vs get_op/query_network walks.
        """
        if not getattr(self.ownerComp.ext, 'TDN', None):
            return {'error': 'TDN extension not loaded on Embody COMP'}
        return self.ownerComp.ext.TDN.ExportNetwork(
            root_path=comp_path,
            include_dat_content=include_dat_content,
            output_file=None,
            max_depth=max_depth,
            embed_all=embed_all,
        )
    def _resolve_diff_target(self, target):
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

    def _diff_tdn(self, target='', max_changed_ops=200, max_bytes=60000):
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
        if not getattr(self.ownerComp.ext, 'TDN', None):
            return {'error': 'TDN extension not loaded on Embody COMP'}
        # Empty / whole-project target -> project-wide summary. Per-COMP detail
        # uses DiffAllLiveVsDisk's own (smaller) caps; the handler's
        # max_changed_ops governs the single-COMP path below.
        if not target or str(target).strip() in ('', '/', 'project', '.', '*'):
            return self.ownerComp.ext.TDN.DiffAllLiveVsDisk(max_bytes=max_bytes)
        comp_path, err = self._resolve_diff_target(target)
        if err:
            return {'error': err}
        return self.ownerComp.ext.TDN.DiffLiveVsDisk(
            comp_path=comp_path,
            max_changed_ops=max_changed_ops, max_bytes=max_bytes)



    # === Utility Methods ===

    def _configureMCPClient(self, port, target_dir=None):
        """Auto-configure MCP client by writing .mcp.json and the STDIO bridge
        script.  Uses STDIO transport so Claude Code always has tools available
        (the bridge retries until Envoy is reachable).
        Idempotent -- safe to call on every start.

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
            bridge_dir = target_dir / '.embody'
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
                    'Bridge script source not found -- falling back to HTTP transport',
                    'WARNING')
                self._configureMCPClientHTTP(target_dir, port)
                return

            # Write bridge script only if content changed -- preserving
            # mtime prevents Claude Code's file watcher from restarting
            # the MCP server mid-connection.
            needs_write = True
            if bridge_path.exists():
                try:
                    existing = bridge_path.read_text(encoding='utf-8')
                    if existing == bridge_content:
                        needs_write = False
                except OSError:
                    pass  # Can't read -- overwrite

            if needs_write:
                bridge_path.write_text(bridge_content, encoding='utf-8')
                if sys.platform != 'win32':
                    bridge_path.chmod(0o755)
            else:
                if sys.platform != 'win32':
                    bridge_path.chmod(0o755)

            # Migrate: remove old bridge from .claude/ if it exists
            old_bridge = target_dir / '.claude' / 'envoy-bridge.py'
            if old_bridge.exists():
                try:
                    old_bridge.unlink()
                    self._log('Migrated: removed old .claude/envoy-bridge.py')
                except OSError:
                    pass

            # Migrate: remove old files from previous locations
            for old_name, desc in [('.envoy-tools-cache.json', 'tools cache'),
                                    ('.envoy.json', 'envoy config'),
                                    ('.embody.json', 'embody config')]:
                old_file = target_dir / old_name
                if old_file.exists():
                    try:
                        old_file.unlink()
                        self._log(f'Migrated: removed old {old_name} ({desc})')
                    except OSError:
                        pass

            # Prefer the venv Python (created from TD's Python) so the bridge
            # works on machines without a system Python installation.
            # Fall back to system PATH command if the venv doesn't exist yet.
            if sys.platform == 'win32':
                venv_python = project_dir / '.venv' / 'Scripts' / 'python.exe'
            else:
                venv_python = project_dir / '.venv' / 'bin' / 'python3'

            if venv_python.is_file():
                # Verify the venv Python actually executes -- catches stale
                # pyvenv.cfg pointing to an uninstalled TD version, or
                # code-signing mismatches after macOS TD upgrades.
                # stdin=DEVNULL: without it, subprocess.run inside TD on
                # Windows raises [WinError 50] (DuplicateHandle on TD's
                # non-duplicatable GUI stdin handle) -- which then triggers
                # the rmtree path below and destroys a healthy venv.
                try:
                    subprocess.run(
                        [str(venv_python), '-c',
                         'import sys; print(sys.version)'],
                        capture_output=True, timeout=10, check=True,
                        stdin=subprocess.DEVNULL)
                    python_cmd = str(venv_python).replace('\\', '/')
                except (subprocess.CalledProcessError,
                        subprocess.TimeoutExpired, OSError) as e:
                    if not self._venv_recreated:
                        self._venv_recreated = True
                        self._log(
                            f'Venv corrupted ({type(e).__name__}: {e}), '
                            f'recreating...', 'WARNING')
                        import shutil
                        shutil.rmtree(str(project_dir / '.venv'),
                                      ignore_errors=True)
                        op.Embody.ext.Embody._setupEnvironment()
                        # Re-check after recreation
                        if venv_python.is_file():
                            try:
                                subprocess.run(
                                    [str(venv_python), '-c',
                                     'import sys; print(sys.version)'],
                                    capture_output=True, timeout=10,
                                    check=True,
                                    stdin=subprocess.DEVNULL)
                                python_cmd = str(venv_python).replace(
                                    '\\', '/')
                                self._log('Venv recreated successfully',
                                          'SUCCESS')
                            except Exception as e2:
                                self._log(
                                    f'Venv recreation failed: {e2}. '
                                    f'Using system Python.', 'ERROR')
                                python_cmd = ('python' if sys.platform == 'win32'
                                              else 'python3')
                        else:
                            self._log(
                                'Venv recreation did not produce Python '
                                'binary. Using system Python.', 'ERROR')
                            python_cmd = ('python' if sys.platform == 'win32'
                                          else 'python3')
                    else:
                        self._log(
                            f'Venv Python still broken after recreation: '
                            f'{e}. Using system Python.', 'WARNING')
                        python_cmd = ('python' if sys.platform == 'win32'
                                      else 'python3')
            else:
                python_cmd = 'python' if sys.platform == 'win32' else 'python3'

            # --- Deploy the .tdn git diff driver (semantic git diffs) ---
            self._configureTdnDiffDriver(target_dir, python_cmd)

            # --- Write envoy.json project config ---
            self._writeEnvoyConfig(target_dir / '.embody', port)

            # --- Write .mcp.json with STDIO transport ---
            mcp_file = target_dir / '.mcp.json'
            # Use forward slashes even on Windows for JSON portability
            bridge_abs = str(bridge_path).replace('\\', '/')
            config_abs = str(
                (target_dir / '.embody' / 'envoy.json')).replace('\\', '/')

            # Record .mcp.json footprint: Embody manages the mcpServers.envoy
            # key. If it created the file, Uninstall may delete it; if it merged
            # into a pre-existing one, Uninstall removes only that key.
            try:
                Embody = op.Embody.ext.Embody
                if mcp_file.exists():
                    Embody._manifestRecordAppendedFile(
                        str(target_dir), mcp_file, 'mcpServers.envoy',
                        kind='json_key')
                else:
                    Embody._manifestRecordCreatedFile(str(target_dir), mcp_file)
            except Exception:
                pass

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
                self._deploySettingsLocal(target_dir / '.claude')
                return

            servers['envoy'] = {
                'type': 'stdio',
                'command': python_cmd,
                'args': expected_args,
            }
            config['mcpServers'] = servers

            def _write():
                mcp_file.write_text(
                    json.dumps(config, indent=2) + '\n', encoding='utf-8')
                self._log(f'Wrote MCP config to {mcp_file} (STDIO bridge -> port {port})')

            # Advanced mode: confirm before writing the Envoy entry into the
            # user's .mcp.json (only reached when it is missing or out of date).
            verb = 'add the Envoy MCP server entry to' if existing else 'create'
            op.Embody.ext.Embody._guardFileWrite(
                'MCP config', f'{verb} .mcp.json in {target_dir}',
                [str(mcp_file)], _write)

            # --- Deploy settings.local.json (auto-allow read-only MCP tools) ---
            self._deploySettingsLocal(target_dir / '.claude')

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

    def _registryPath(self):
        """Path to .embody/envoy.json honoring Aiprojectroot.

        All registry I/O (port-conflict detection, RefreshRegistry,
        deregistration) must go through here -- the registry must live
        co-located with .mcp.json, which itself follows Aiprojectroot
        via _findProjectRoot. Defaults to legacy git_root behavior if
        the Embody extension isn't accessible (defensive).
        """
        from pathlib import Path
        try:
            root = op.Embody.ext.Embody._findProjectRoot()
            return Path(root) / '.embody' / 'envoy.json'
        except Exception:
            git_root = self.ownerComp.fetch('_git_root', 'no-git')
            if git_root == 'no-git':
                return None
            return Path(git_root) / '.embody' / 'envoy.json'

    # Envoy MCP tools that only READ / query TD state -- safe to auto-approve
    # under the 'some' tool-permissions posture. Anything that creates, edits,
    # deletes, connects, executes, imports, or externalizes is deliberately
    # omitted so it still prompts. Entries are the tool short-names; the
    # permission strings written are 'mcp__envoy__<name>'.
    READ_ONLY_TOOLS = [
        'get_td_status', 'get_td_info', 'get_td_classes', 'get_td_class_details',
        'get_op', 'get_op_errors', 'get_op_flags', 'get_op_position',
        'get_op_performance', 'get_project_performance', 'get_parameter',
        'get_connections', 'get_annotations', 'get_network_layout',
        'get_dat_content', 'get_docs', 'get_module_help', 'get_logs',
        'get_externalizations', 'get_externalization_status', 'get_sessions',
        'query_network', 'find_children', 'get_enclosed_ops',
        'read_tdn', 'diff_tdn', 'capture_top',
    ]

    def _toolPermissionsPosture(self):
        """The Toolpermissions param value, defensively normalized.
        'all' | 'some' | 'prompt' | 'leave' (default 'all')."""
        try:
            posture = (op.Embody.par.Toolpermissions.eval() or 'all').strip().lower()
        except Exception:
            posture = 'all'
        return posture if posture in ('all', 'some', 'prompt', 'leave') else 'all'

    def _tempReadDirs(self):
        """Directories that must be readable so a capture_top PNG (saved to the
        OS temp dir, EnvoyExt._captureTop) can be Read without a prompt. Forward
        slashes for cross-platform JSON. Always includes /tmp as a fallback."""
        dirs = ['/tmp']
        try:
            t = tempfile.gettempdir().replace('\\', '/')
            if t and t not in dirs:
                dirs.append(t)
        except Exception:
            pass
        return dirs

    def _loadSettingsBaseline(self):
        """The NON-Envoy baseline settings dict (from the template DAT if
        present, else a built-in minimum). The Envoy allow entries + temp read
        dirs are layered on per posture by _composeSettings, so the template no
        longer needs to enumerate them."""
        try:
            templates = self.ownerComp.op('templates')
            dat = templates.op('text_settings_local') if templates else None
            if dat and (dat.text or '').strip():
                cfg = json.loads(dat.text)
                if isinstance(cfg, dict):
                    return cfg
        except Exception as e:
            self._log(f'settings baseline template unreadable ({e}); '
                      f'using built-in.', 'DEBUG')
        return {
            'permissions': {
                'allow': ['Bash', 'WebFetch'],
                'additionalDirectories': ['/tmp'],
            },
            'enabledMcpjsonServers': ['envoy'],
            'enableAllProjectMcpServers': True,
        }

    def _composeSettings(self, cfg, posture):
        """Apply a tool-permissions posture onto a settings dict IN PLACE and
        return it. Preserves every non-Envoy key and every non-Envoy allow
        entry; replaces only the Envoy tool entries, ensures the temp read
        dirs, and trusts the Envoy MCP server. `posture` is never 'leave'
        here (the caller short-circuits that)."""
        perms = cfg.setdefault('permissions', {})
        # Strip prior Envoy entries so we author them fresh for this posture.
        allow = [a for a in perms.get('allow', [])
                 if not (a == 'mcp__envoy' or a.startswith('mcp__envoy__'))]
        if posture == 'all':
            allow.append('mcp__envoy')          # wildcard: all current + future tools
        elif posture == 'some':
            allow.extend(f'mcp__envoy__{t}' for t in self.READ_ONLY_TOOLS)
        # posture == 'prompt': no Envoy entries -> every tool prompts.
        perms['allow'] = allow
        add = list(perms.get('additionalDirectories', []))
        for d in self._tempReadDirs():
            if d not in add:
                add.append(d)
        perms['additionalDirectories'] = add
        cfg['permissions'] = perms
        # Trust the project MCP server so tools are available at all.
        cfg['enableAllProjectMcpServers'] = True
        servers = list(cfg.get('enabledMcpjsonServers', []))
        if 'envoy' not in servers:
            servers.append('envoy')
        cfg['enabledMcpjsonServers'] = servers
        return cfg

    def _settingsSatisfies(self, cfg, posture):
        """True if an existing settings dict already matches `posture` (so no
        rewrite is needed). Semantic, order-insensitive -- avoids startup churn
        from list reordering."""
        if not isinstance(cfg, dict):
            return False
        perms = cfg.get('permissions', {}) or {}
        allow = perms.get('allow', []) or []
        envoy = [a for a in allow
                 if a == 'mcp__envoy' or a.startswith('mcp__envoy__')]
        add = perms.get('additionalDirectories', []) or []
        temp_ok = all(d in add for d in self._tempReadDirs())
        server_ok = (cfg.get('enableAllProjectMcpServers') is True
                     or 'envoy' in (cfg.get('enabledMcpjsonServers') or []))
        if not (temp_ok and server_ok):
            return False
        if posture == 'all':
            return 'mcp__envoy' in envoy
        if posture == 'prompt':
            return len(envoy) == 0
        if posture == 'some':
            return set(envoy) == {f'mcp__envoy__{t}' for t in self.READ_ONLY_TOOLS}
        return False

    def _deploySettingsLocal(self, claude_dir):
        """Write .claude/settings.local.json to match the Toolpermissions
        posture, so Claude Code isn't prompted on every Envoy MCP tool call
        (and so a captured TOP in the OS temp dir can be Read without a prompt).

        Postures: all = auto-approve all Envoy tools (wildcard); some =
        read-only tools only; prompt = none; leave = don't touch the file.
        Merges into an existing file, preserving every non-Envoy key, and is
        idempotent (skips when the posture is already satisfied -- no churn).
        The user is told whether the file was created or updated.
        """
        import copy
        posture = self._toolPermissionsPosture()
        settings_path = claude_dir / 'settings.local.json'

        if posture == 'leave':
            self._log('Tool permissions: leaving .claude/settings.local.json '
                      'untouched (your choice).', 'DEBUG')
            return

        existed = settings_path.exists()
        existing = None
        if existed:
            try:
                existing = json.loads(settings_path.read_text(encoding='utf-8'))
                if not isinstance(existing, dict):
                    existing = None
            except (json.JSONDecodeError, OSError) as e:
                # Never clobber a settings.local.json we can't parse -- it may
                # hold hand-authored user permissions.
                self._log(f'Could not parse existing settings.local.json '
                          f'({e}) -- leaving it untouched.', 'WARNING')
                return

        # Idempotent: an already-satisfying file is left exactly as-is.
        if existing is not None and self._settingsSatisfies(existing, posture):
            self._log(f'settings.local.json already matches tool permissions '
                      f'({posture}) -- no change.', 'DEBUG')
            return

        base = copy.deepcopy(existing) if existing is not None \
            else self._loadSettingsBaseline()
        new_cfg = self._composeSettings(base, posture)
        content = json.dumps(new_cfg, indent=2) + '\n'
        verb = 'update' if existed else 'create'

        def _write():
            claude_dir.mkdir(parents=True, exist_ok=True)
            settings_path.write_text(content, encoding='utf-8')
            self._log(f'{verb.capitalize()}d .claude/settings.local.json '
                      f'(tool permissions: {posture}) at {settings_path}',
                      'SUCCESS')
            try:
                Embody = op.Embody.ext.Embody
                root = str(Embody._findProjectRoot())
                if existed:  # merged into a user file -> Uninstall only reverses our unit
                    Embody._manifestRecordAppendedFile(
                        root, settings_path,
                        'permissions (Envoy tools + temp read dirs)',
                        kind='json_key')
                else:        # Embody created it -> safe to remove on Uninstall
                    Embody._manifestRecordCreatedFile(root, settings_path)
            except Exception:
                pass

        # Advanced mode confirms; Auto / consented-batch apply silently. The
        # 'update' verb makes the disclosure honest about touching a user file.
        op.Embody.ext.Embody._guardFileWrite(
            'AI config',
            f'{verb} .claude/settings.local.json (tool permissions: {posture}) '
            f'in {claude_dir.parent}',
            [str(settings_path)],
            _write)

    def _findGitRoot(self):
        """Silently find the git repo root. Returns Path or 'no-git'. Never prompts."""
        from pathlib import Path
        project_dir = Path(project.folder).resolve()
        try:
            home_dir = Path.home().resolve()
        except Exception:
            home_dir = None
        # Only stop at home_dir when it's actually an ancestor of project_dir.
        # Otherwise (e.g. Windows project on D:\ while home is on C:\) the
        # part-count comparison wrongly bailed before searching -- issue #19.
        home_is_ancestor = bool(
            home_dir and (home_dir == project_dir or home_dir in project_dir.parents)
        )
        for parent in [project_dir] + list(project_dir.parents):
            if home_is_ancestor and parent == home_dir:
                break
            if (parent / '.git').exists():
                self._log(f'Found git repo at {parent}', 'INFO')
                return parent
        self._log(f'No git repo found for {project_dir}', 'INFO')
        return 'no-git'

    def _checkOrInitGitRepo(self):
        """Check for a git repo. If missing, prompt user to initialize one.
        Only call from user-initiated flows (_enableEnvoy, InitGit) -- never
        from automatic startup paths. Returns Path, 'no-git', or None (cancelled)."""
        from pathlib import Path
        import os, subprocess

        project_dir = Path(project.folder).resolve()
        try:
            home_dir = Path.home().resolve()
        except Exception:
            home_dir = None

        # Walk up looking for .git, but stop at the home directory only when
        # home is actually an ancestor of project_dir (issue #19 -- previously
        # the comparison broke for projects on a non-home drive on Windows).
        home_is_ancestor = bool(
            home_dir and (home_dir == project_dir or home_dir in project_dir.parents)
        )
        for parent in [project_dir] + list(project_dir.parents):
            if home_is_ancestor and parent == home_dir:
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
                'Envoy -- Git Repository Recommended',
                'A git repository is recommended for .gitignore and\n'
                '.gitattributes management. No git repository was found.\n\n'
                'MCP and AI client config files will be generated either way.\n\n'
                f'Initialize a git repo in:\n  {project_dir}\n\n'
                'Or browse to select a different folder (e.g. an existing repo root).\n'
                'You can also run op.Embody.InitGit() later.',
                buttons=['Cancel', 'Initialize Git Here', 'Browse for Folder', 'Start Without Git'])

            if choice not in (1, 2, 3):  # Cancel or closed dialog
                self.ownerComp.par.Envoyenable = False
                self._log('Envoy cancelled -- no git repository.', 'INFO')
                return None

            if choice == 2:  # Browse for Folder
                result = ui.chooseFolder(
                    title='Select Git Repository Root', start=str(project_dir))
                if not result:
                    self.ownerComp.par.Envoyenable = False
                    self._log('Envoy cancelled -- folder selection aborted.', 'INFO')
                    return None
                chosen = Path(result)
                # If the chosen folder already contains a .git, use it directly
                if (chosen / '.git').exists():
                    self._log(f'Using existing git repo at {chosen}', 'SUCCESS')
                    return chosen
                # No .git there -- offer to initialize in that folder
                init_choice = op.Embody.ext.Embody._messageBox(
                    'Envoy -- Initialize Git',
                    f'No git repo found in:\n  {chosen}\n\nInitialize git here?',
                    buttons=['Cancel', 'Initialize Git'])
                if init_choice not in (1,):
                    self.ownerComp.par.Envoyenable = False
                    return None
                project_dir = chosen  # use chosen folder for init below

            if choice in (1, 2):  # Initialize Git Here, or Browse -> confirmed init
                try:
                    # Strip git env vars that TD's embedded Python may set --
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
                        self._log('Git verify failed after init -- retrying', 'WARNING')
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
                        'Envoy -- Git Initialization Failed',
                        f'Could not initialize a git repository:\n\n  {e}\n\n'
                        'Envoy will start without git. MCP and AI client\n'
                        'config will be generated in the project folder.\n'
                        '.gitignore and .gitattributes will be skipped.\n\n'
                        'To add git later: run "git init" manually, then\n'
                        'call op.Embody.InitGit() from the textport.',
                        buttons=['OK'])
                    # Fall through to start-without-git

            # choice == 3 or git init failed -- start without git
            self._log('Starting Envoy without git repo -- auto-config skipped.', 'WARNING')
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

        Walks forward across TD's auto-version-bump on save: if this PID
        is already registered and its registered toe_path STILL matches
        the current path, the existing key is reused (no churn). If the
        toe_path has changed (rename, save-as-version-up), a fresh key
        is computed from the new basename and the caller is responsible
        for pruning the stale entry under the old key.

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

        # Re-registration with same toe_path: keep the existing key.
        # If the toe_path has changed, fall through and compute a new
        # key from the current basename -- caller prunes the stale row.
        for key, info in existing_instances.items():
            if (info.get('td_pid') == my_pid
                    and info.get('toe_path') == toe_rel):
                return key

        # Check if base key is free, held by a dead process, or held
        # by our own previous (now-stale) registration.
        if base not in existing_instances:
            return base
        existing_pid = existing_instances[base].get('td_pid', 0)
        if not self._isPidAlive(existing_pid) or existing_pid == my_pid:
            return base

        # Base key is held by a live foreign process -- find a unique suffix
        suffix = 2
        while True:
            candidate = f'{base}-{suffix}'
            if candidate not in existing_instances:
                return candidate
            existing_pid = existing_instances[candidate].get('td_pid', 0)
            if not self._isPidAlive(existing_pid) or existing_pid == my_pid:
                return candidate
            suffix += 1

    @staticmethod
    def _isPidAlive(pid):
        """Check whether a process with the given PID is alive.

        CRITICAL: do NOT use ``os.kill(pid, 0)`` on Windows.  CPython's
        posixmodule implements ``os.kill`` on Windows via
        ``OpenProcess(PROCESS_ALL_ACCESS, ...)`` + ``TerminateProcess(handle, sig)``
        regardless of ``sig`` -- when called with ``sig=0`` on a foreign
        TD process Embody has access to, it would silently terminate that
        process with exit code 0.  And when the PID is invalid in a
        particular way (e.g. registry corruption, a wrapped-around PID,
        a non-int), ``OpenProcess`` returns ``INVALID_HANDLE_VALUE``
        instead of NULL; the subsequent ``TerminateProcess`` fails with
        ``WinError 87`` and CPython's wrapper raises ``OSError`` *while
        leaving the interpreter thread state inconsistent*, surfacing as
        ``SystemError: <class 'OSError'> returned a result with an
        exception set`` and intermittently aborting the process on the
        next interpreter tick.  Mirror the bridge's safe pattern instead.
        """
        if not isinstance(pid, int) or pid <= 0:
            return False
        if sys.platform == 'win32':
            try:
                import ctypes
                kernel32 = ctypes.windll.kernel32
                SYNCHRONIZE = 0x00100000
                handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
                if handle:
                    kernel32.CloseHandle(handle)
                    return True
                return False
            except Exception:
                return False
        # POSIX: signal 0 is a real no-op liveness check.  Catch
        # OverflowError too -- pid_t is int32 on most kernels and a
        # registry that's been corrupted with a giant value would
        # otherwise propagate the overflow up through _writeEnvoyConfig.
        import os
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False
        except (OSError, OverflowError, ValueError):
            return False

    def _writeEnvoyConfig(self, embody_dir, port):
        """Register this instance in the .embody/envoy.json instance registry.

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

        embody_dir.mkdir(parents=True, exist_ok=True)
        config_path = embody_dir / 'envoy.json'
        # git_root is embody_dir's parent
        git_root = embody_dir.parent

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

        # Read existing config (migrate from old root-level .envoy.json)
        existing = {}
        if config_path.exists():
            try:
                existing = json.loads(
                    config_path.read_text(encoding='utf-8'))
            except (json.JSONDecodeError, OSError):
                pass
        elif (git_root / '.envoy.json').exists():
            try:
                existing = json.loads(
                    (git_root / '.envoy.json').read_text(encoding='utf-8'))
                self._log('Migrated: seeded envoy.json from old .envoy.json')
            except (json.JSONDecodeError, OSError):
                pass

        # Migrate old flat format -> registry format
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
        my_pid = os.getpid()

        # Garbage-collect any registry rows whose PID is no longer
        # alive. Embody only deregisters cleanly on graceful shutdown
        # (Stop()/onDestroyTD); hard kills, force-quits, OS crashes,
        # and Cmd+Q-without-Envoy-stop all leave dead rows behind that
        # accumulate across sessions. Running this on every registry
        # write keeps the file bounded.
        dead_keys = [
            k for k, info in list(instances.items())
            if not self._isPidAlive(info.get('td_pid', 0))
        ]
        for dead_key in dead_keys:
            del instances[dead_key]
        if dead_keys:
            self._log(
                f'Pruned {len(dead_keys)} dead registry '
                f'{"row" if len(dead_keys) == 1 else "rows"}: '
                f'{", ".join(repr(k) for k in dead_keys)}', 'DEBUG')

        # Prune stale entries under different keys for the same PID
        # (left over from a prior toe rename, e.g. TD's save-time
        # version bump). Keeps the registry walking forward instead of
        # accumulating dead aliases.
        stale_keys = [
            k for k, info in list(instances.items())
            if info.get('td_pid') == my_pid and k != key
        ]
        for stale_key in stale_keys:
            del instances[stale_key]
            self._log(
                f'Pruned stale registry key "{stale_key}" '
                f'(PID {my_pid} now registered as "{key}")', 'DEBUG')

        # Build this instance's entry
        new_entry = {
            'toe_path': toe_rel,
            'port': port,
            'td_pid': my_pid,
        }

        # Check if already up-to-date (no stale prune happened either)
        if (not stale_keys
                and instances.get(key) == new_entry
                and existing.get('active') == key):
            self._log('envoy.json already up to date', 'DEBUG')
            return

        instances[key] = new_entry
        existing['instances'] = instances
        existing['active'] = key
        existing['td_executable'] = td_executable

        self._atomicWriteJSON(config_path, existing)
        self._log(f'Registered instance "{key}" in envoy.json (port {port})')

    def RefreshRegistry(self):
        """Re-register this instance in envoy.json under its current
        toe basename. Safe to call repeatedly (idempotent when nothing
        has changed). Used after `project.save()` to walk the registry
        forward across TD's save-time version bump -- the toe goes
        from `Foo-5.398.toe` to `Foo-5.399.toe` and the registry needs
        to follow.

        Reads the running port from envoy.json by looking up our own
        PID, since EnvoyExt does not retain a runtime port attribute
        (the actual server lives on a worker thread)."""
        import os

        config_path = self._registryPath()
        if config_path is None or not config_path.exists():
            return

        try:
            existing = json.loads(config_path.read_text(encoding='utf-8'))
        except (json.JSONDecodeError, OSError):
            return

        my_pid = os.getpid()
        port = 0
        for info in existing.get('instances', {}).values():
            if info.get('td_pid') == my_pid:
                port = info.get('port', 0)
                break
        if not port:
            # We aren't in the registry yet (Envoy may have only just
            # started, or not started at all). Nothing to refresh.
            return

        try:
            self._writeEnvoyConfig(config_path.parent, port)
        except Exception as e:
            self._log(f'RefreshRegistry failed: {e}', 'WARNING')

    def _removeFromRegistry(self, git_root=None):
        """Remove this instance from the .embody/envoy.json registry on shutdown.

        Honors Aiprojectroot via _registryPath. The git_root kwarg is kept
        for backward compatibility but only used as a defensive fallback
        when the live registry path isn't resolvable.
        """
        import os
        from pathlib import Path

        config_path = self._registryPath()
        if config_path is None and git_root is not None and git_root != 'no-git':
            config_path = Path(git_root) / '.embody' / 'envoy.json'
        if config_path is None or not config_path.exists():
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
            self._log(f'Deregistered instance "{my_key}" from envoy.json')
        except Exception as e:
            self._log(f'Could not deregister from envoy.json: {e}', 'WARNING')

    def _configureGitignore(self, git_root):
        """Ensure .gitignore in the git root contains entries for
        Embody/Envoy auto-generated files.
        Idempotent -- only appends missing entries, preserves all existing content.
        Migrates old `.claude/` blanket entry to specific entries."""
        MANAGED_ENTRIES = [
            # TouchDesigner project
            'Backup/',
            'logs/',
            'CrashAutoSave*',
            # Embody / Envoy
            '.venv/',
            '.mcp.json',
            # Ignore .embody/ runtime files but keep committed project.json
            '.embody/*',
            '!.embody/project.json',
            '.claude/settings.local.json',
            '.claude/projects/',
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

            # Migrate: remove stale entries from older Embody versions.
            # NOTE: .envoy-tools-cache.json is intentionally kept gitignored
            # (v5.0.356+) because a root-level cache can still be written
            # by legacy paths; we don't want to accidentally commit it.
            STALE_ENTRIES = {'.claude/', '.claude/envoy-bridge.py',
                             '.envoy.json', '.embody.json',
                             '.embody/envoy-bridge.py',
                             '.embody/envoy-tools-cache.json',
                             # v5.0.387: replaced by '.embody/*' + '!.embody/project.json'
                             # so .embody/project.json (committed td_build pin) is tracked.
                             '.embody/'}
            existing_stripped = {line.strip() for line in existing_lines}
            found_stale = STALE_ENTRIES & existing_stripped
            if found_stale:
                existing_lines = [
                    line for line in existing_lines
                    if line.strip() not in STALE_ENTRIES
                ]
                existing_content = '\n'.join(existing_lines)
                if existing_content and not existing_content.endswith('\n'):
                    existing_content += '\n'
                self._log(f'Migrated .gitignore: removed stale entries {found_stale}')

            existing_stripped = {line.strip() for line in existing_lines}
            missing = [e for e in MANAGED_ENTRIES if e not in existing_stripped]

            if not missing:
                self._log('.gitignore already configured', 'DEBUG')
                return

            block = '\n# Embody / Envoy (auto-managed)\n'
            block += '\n'.join(missing) + '\n'

            if existing_content and not existing_content.endswith('\n'):
                block = '\n' + block

            def _write():
                gitignore.write_text(existing_content + block, encoding='utf-8')
                self._log(f'Added {len(missing)} entries to .gitignore: {", ".join(missing)}')
                try:  # record the marked block so Uninstall strips only it (never the user's file)
                    Embody = op.Embody.ext.Embody
                    Embody._manifestRecordAppendedFile(
                        str(Embody._findProjectRoot()), gitignore, '# Embody / Envoy')
                except Exception:
                    pass

            # Advanced mode: confirm before editing the user's .gitignore. Only
            # reached when entries are actually missing, so a no-op never prompts.
            op.Embody.ext.Embody._guardFileWrite(
                'Git config',
                f'add {len(missing)} entr{"y" if len(missing) == 1 else "ies"} to '
                f'.gitignore in {git_root}',
                list(missing),
                _write)

        except Exception as e:
            self._log(f'Could not auto-configure .gitignore: {e}', 'WARNING')

    def _configureGitattributes(self, git_root):
        """Ensure .gitattributes normalizes line endings for TD-exported files
        and enables semantic diffs for .tdn. TouchDesigner writes CRLF on all
        platforms; this forces LF in git so externalized files don't show as
        dirty after every TD save. The `diff=tdn` attribute pairs with the git
        diff driver registered by _configureTdnDiffDriver, so `git diff` on a
        .tdn shows only real network changes -- the volatile export header
        (build/timestamp/version/source .toe) is stripped before diffing.
        Idempotent -- migrates an existing managed block that predates the
        diff driver."""
        MANAGED_BLOCK = (
            '\n# Embody / Envoy -- normalize TD line endings (auto-managed)\n'
            '*.py text eol=lf\n'
            '*.md text eol=lf\n'
            '*.tdn text eol=lf diff=tdn\n'
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
                # Migrate a managed block that predates the .tdn diff driver.
                if ('*.tdn text eol=lf diff=tdn' not in existing
                        and '*.tdn text eol=lf' in existing):
                    existing = existing.replace(
                        '*.tdn text eol=lf', '*.tdn text eol=lf diff=tdn')
                    gitattr.write_text(existing, encoding='utf-8')
                    self._log(
                        'Migrated .gitattributes: enabled .tdn semantic diff')
                else:
                    self._log('.gitattributes already configured', 'DEBUG')
                return

            if existing and not existing.endswith('\n'):
                existing += '\n'

            def _write():
                gitattr.write_text(existing + MANAGED_BLOCK, encoding='utf-8')
                self._log('Added line-ending normalization to .gitattributes')
                try:  # record the marked block so Uninstall strips only it (never the user's file)
                    Embody = op.Embody.ext.Embody
                    Embody._manifestRecordAppendedFile(
                        str(Embody._findProjectRoot()), gitattr, MARKER)
                except Exception:
                    pass

            # Advanced mode: confirm before editing the user's .gitattributes.
            op.Embody.ext.Embody._guardFileWrite(
                'Git config',
                f'add line-ending + .tdn-diff rules to .gitattributes in {git_root}',
                [ln for ln in MANAGED_BLOCK.strip().splitlines()
                 if ln and not ln.startswith('#')],
                _write)

        except Exception as e:
            self._log(f'Could not auto-configure .gitattributes: {e}', 'WARNING')

    def _configureTdnDiffDriver(self, target_dir, python_cmd):
        """Deploy the .tdn git textconv script and register it as a git diff
        driver in the repo. With the `*.tdn diff=tdn` attribute (set by
        _configureGitattributes), this makes `git diff` / `git log -p` /
        `git show` on .tdn files show only semantic network changes -- the
        volatile export header is stripped before diffing, so re-exporting an
        unchanged network produces an empty diff. This is the committed/on-disk
        counterpart to the live `diff_tdn` MCP tool. The driver definition must
        live in the repo's git config (git refuses to run textconv commands
        defined by a cloned repo), so Embody configures it the same way it
        manages .gitignore/.gitattributes/.mcp.json. Idempotent."""
        from pathlib import Path
        try:
            target_dir = Path(target_dir)
            embody_dir = target_dir / '.embody'
            embody_dir.mkdir(parents=True, exist_ok=True)
            script_path = embody_dir / 'tdn_textconv.py'

            # Source from the templates textDAT, else the dev/embody fallback.
            content = None
            try:
                templates = self.ownerComp.op('templates')
                dat = templates.op('text_tdn_textconv') if templates else None
                if dat:
                    content = dat.text
            except Exception:
                pass
            if not content:
                source = Path(project.folder) / 'embody' / 'tdn_textconv.py'
                if source.exists():
                    content = source.read_text(encoding='utf-8')
            if not content:
                self._log(
                    'tdn_textconv source not found -- skipping .tdn diff driver',
                    'DEBUG')
                return

            # Write only if changed, to avoid touching mtime needlessly.
            if not (script_path.exists()
                    and script_path.read_text(encoding='utf-8') == content):
                script_path.write_text(content, encoding='utf-8')

            # Register the driver in the repo's git config (idempotent).
            script_str = str(script_path).replace('\\', '/')
            driver = '"%s" "%s"' % (python_cmd, script_str)
            git_kwargs = dict(cwd=str(target_dir), capture_output=True,
                              text=True, timeout=10,
                              stdin=subprocess.DEVNULL)
            current = subprocess.run(
                ['git', 'config', '--get', 'diff.tdn.textconv'], **git_kwargs)
            if (current.stdout or '').strip() != driver:
                def _write():
                    subprocess.run(
                        ['git', 'config', 'diff.tdn.textconv', driver],
                        check=True, **git_kwargs)
                    subprocess.run(
                        ['git', 'config', 'diff.tdn.cachetextconv', 'false'],
                        check=True, **git_kwargs)
                    self._log('Configured git diff driver for .tdn (semantic diffs)')
                    try:  # record so Uninstall un-sets the repo git config
                        op.Embody.ext.Embody._manifestRecordGitConfig(
                            str(target_dir),
                            ['diff.tdn.textconv', 'diff.tdn.cachetextconv'])
                    except Exception:
                        pass

                # Advanced: confirm before mutating the repo's .git/config.
                op.Embody.ext.Embody._guardFileWrite(
                    'Git config',
                    f'register the .tdn semantic-diff driver in '
                    f'{target_dir}/.git/config',
                    ['git config diff.tdn.textconv',
                     'git config diff.tdn.cachetextconv'],
                    _write)

        except (subprocess.SubprocessError, OSError) as e:
            self._log(f'Could not configure .tdn git diff driver: {e}', 'DEBUG')
        except Exception as e:
            self._log(f'Could not deploy tdn_textconv: {e}', 'WARNING')

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
