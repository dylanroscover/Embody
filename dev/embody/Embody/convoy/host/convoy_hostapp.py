"""embody-convoy host app -- Phase 1 local skeleton.

One per machine, outside TD, LOOPBACK ONLY in Phase 1: LAN transport is
Phase 3 (with real identity/TLS); nothing here binds off-box, so no
firewall rule is needed yet (D-6). Development entry point per 12.2
("may start as a Python entry point"); packaging/signing is the Phase 1
spike, supervision is A-36's exactly-one-supervisor.

What it owns TODAY (the Phase 1 exit slice):
  - host identity (host_id minted once, stored host-private),
  - the node registry: TD runtimes register with their project-side
    anchor and get their host-minted node_id back (A-12),
  - durable jobs: persist-before-acknowledge, idempotent create, state
    survives host restart,
  - authenticated local IPC: every request presents the per-install
    token; a wrong/missing token is refused before ANY state is touched,
  - the audit trail (A-40),
  - THE HOST KEYPAIR (Phase 3 slice 1, convoy_hostkeys): one Ed25519
    identity per machine, served on /identity and replaceable via
    /identity/rotate. NOTHING BINDS OFF-BOX YET -- the key exists, the
    TLS listener that will use it is slice 3. Absent `cryptography`
    degrades (no identity, named reason); a corrupt key file refuses to
    start,
  - PEERS AND REVOCATION (Phase 3 slice 2, convoy_peers): host-private
    admission records, a hand-editable FAIL-CLOSED denylist re-read on
    mtime change, and ONE decision function consulted BEFORE any
    signature is verified. /peers* and /lan/killswitch are loopback
    routes; STILL NOTHING BINDS OFF-BOX -- the listener that will call
    the same decision on a real connection is slice 3,
  - THE GUARDED REQUEST PATH: a signed Convoy/1 envelope submitted to
    /envelope is verified (convoy_protocol), gated by the operation
    registry (A-1 executability), authorized against leases
    (convoy_controllers), and only then becomes a durable job. The
    capability manifest (convoy_capabilities, A-23) is served on
    /manifest so a controller can refuse-before-send.

What it deliberately does NOT do yet: LAN anything, discovery, peers,
relay, TD launch, artifacts. Those phases build ON this file's contracts
rather than amending them.
"""

import argparse
import base64
import collections
import concurrent.futures
import copy
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import signal
import socket
import ssl
import stat
import sys
import tempfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import convoy_capabilities as capabilities
import convoy_controllers as controllers
import convoy_artifacts as artifacts_mod
import convoy_artifact_http as artifact_http
import convoy_discovery as discovery_mod
import convoy_hostkeys as hostkeys
import convoy_hostops as hostops_mod
import convoy_hoststore as hoststore
import convoy_identity as identity
import convoy_lan as lan_mod
import convoy_lifecycle as lifecycle_mod
import convoy_mcpclient as mcpclient
import convoy_owlette as owlette_mod
import convoy_peerclient as peerclient
import convoy_peers as peers_mod
import convoy_peerserver as peerserver
import convoy_platform as platform_mod
import convoy_policy as policy_mod
import convoy_protocol as protocol
import convoy_realm as realm_mod
import convoy_sessions as sessions_mod
import convoy_wake as wake_mod
import convoy_ws as ws_mod

MAX_BODY_BYTES = 1 * 1024 * 1024
TOKEN_HEADER = "X-Convoy-Host-Token"


def _running_app_version(module_file=None):
    """The version segment of the app directory THIS code runs from.

    Installed daemons live in app/<version>/convoy_*.py (convoy_install
    lays them out), so the running module's own directory name IS its
    version -- the one answer that cannot drift the way installed.json
    can: a supervisor rewrite whose restart silently failed leaves
    installed.json claiming the new version while the OLD code keeps
    serving (the exact hole that let nine releases ship without any
    deployed daemon updating, found 2026-08-05). "source" means an
    unversioned checkout (a dev tree), and it is deliberately not None:
    a /status or /health answer with NO app_version at all can then only
    mean a daemon too old to report one -- which is itself the signal
    the TD side uses to update it.
    """
    try:
        path = module_file or os.path.abspath(__file__)
        segment = os.path.basename(os.path.dirname(path))
        if re.fullmatch(r"[0-9]+(\.[0-9]+)*", segment):
            return segment
    except Exception:
        pass
    return "source"


APP_VERSION = _running_app_version()


class _PeerProjectionTarget:
    """Trust-owned display identity when a live WSS peer has no dial URI."""

    __slots__ = ("host_id", "address")

    def __init__(self, host_id, address=""):
        self.host_id = host_id
        self.address = address
OWLETTE_COMMAND_OPT_IN_ENV = "EMBODY_CONVOY_ALLOW_OWLETTE_COMMANDS"
OWLETTE_TUNNEL_COMMAND = "mcp_tool_call"
# Owlette command types that can take a show machine down with no unsaved-work
# check (adopted review A-35).  These escalate past every other Owlette action,
# so -- like Full Shell -- they require a DURABLE policy opt-in, not merely the
# benign-command env flag.  Default-deny until that opt-in exists.
OWLETTE_MACHINE_AFFECTING_COMMANDS = frozenset({
    "reboot_machine", "shutdown_machine", "restart_process", "kill_process",
})

# ONE condition, ONE machine-readable code, on every surface: /status's
# identity_reason, /identity's and /identity/rotate's refusal reason,
# and the audit line all carry this exact string, which is also
# hostkeys.CryptographyMissing.reason. A client keying off `reason`
# used to get a different answer depending on which route it asked.
IDENTITY_UNAVAILABLE_REASON = "cryptography_missing"

# -- async node jobs (the polling slice) ------------------------------
#
# Some node operations do not RETURN a result -- they mint a node-side
# job and return its HANDLE (run_tests background=True, save_project).
# The host mirrors that handle as 'running' and then POLLS the node for
# the outcome, so a 20-minute test run never sits inside a 30s forward.
#
# The one operation the poller calls, hardcoded: read-only, worker-side
# on the node (it answers while TD's main thread is blocked), and the
# only argument it takes is an id the node itself minted.
POLL_OPERATION = "get_job_status"
# Mirrors EnvoyExt's own job-id validation (_job_path). A node-supplied
# id is UNTRUSTED input that we hand straight back to the node and store
# in a durable record -- validate its shape before either.
NODE_JOB_ID_RE = re.compile(r"^job_[0-9a-f]{8}\Z")
# The node's status vocabulary, read from the store's mapping so the two
# can never drift apart (the store is what translates them to states).
NODE_JOB_STATUSES = tuple(hoststore._NODE_STATUS_TO_STATE)
# A node result rides into a durable job file and back out of every
# /jobs response. A test-run summary is small; a pathological one is
# not, and nothing else bounds it.
MAX_RESULT_BYTES = 64 * 1024
MAX_CAPTURE_ARTIFACT_BYTES = 64 * 1024 * 1024
_ENVOY_CAPTURE_NAME_RE = re.compile(
    r"^envoy_capture_[0-9a-f]{8}\.(?:jpg|png)$", re.IGNORECASE)
# Before a poll may conclude the node FORGOT a job (which terminalises
# it as indeterminate), the unknown answer must repeat and outlast a
# grace window. A node restarting between flushes, the 24h retention,
# and the transient dark-job layer all produce one-off unknowns.
POLL_UNKNOWN_MIN_OBSERVATIONS = 3
POLL_UNKNOWN_GRACE_S = 60.0
# The node's "I have no such job" answer, specifically (EnvoyExt
# get_job_status: {'error': "no job with id 'job_x'", 'jobs': [...]}).
# The unknown-job path is the ONE place a read turns into a terminal,
# precious record, so it may not fire on just any error payload: a
# node answering {'error': 'Job records unavailable'} is reporting its
# OWN trouble, not the job's absence, and terminalising on it would put
# a claim in the record the node never made.
_NODE_UNKNOWN_JOB_RE = re.compile(r"no job with id", re.IGNORECASE)
# A running mirror is rewritten at most this often. Every successful
# running-poll used to rewrite the whole job file for a record whose
# state and provenance had not changed -- measured at 6-38 ms each, 200
# atomic rewrites per backoff window at 200 running jobs, forever. The
# durable record may lag its last observation by this much; nothing
# reads observed_at for a decision, and a real change (state, handle,
# stale, terminal) always writes immediately.
POLL_MIRROR_REFRESH_S = 300.0
# A successful fire-and-forget wake gets a quick durable-drain retry.  The TD
# main-thread poll runs every 250 ms and Envoy normally binds within a second;
# this stays bounded without making every sleeping operation wait the ordinary
# 30-second unavailable-node backoff.
WAKE_RETRY_S = 1.0
# A wake lease has a hard TTL in the TD process.  Long lifecycle calls must
# refresh it while they are actually running; otherwise Perform Mode can close
# Envoy in the middle of an exact-node restart.  Tests lower this constant,
# while production also caps the interval to one third of the advertised TTL.
WAKE_REFRESH_MAX_S = 10.0
LIFECYCLE_RECOVERY_INTERVAL_S = 5.0

# The complete Convoy operation registry.  Every Envoy MCP tool that can be
# meaningfully attributed to a Convoy controller is classified here; absence
# is a refusal.  This is intentionally data rather than a permissive fallback:
# the AST drift test fails whenever Envoy grows a tool without an explicit
# registry entry or an explicit, justified exclusion.
def _operation(schema, *, mutating, executes_arbitrary_code,
               remote_exposed, runtime_required, batch_eligible,
               side_effects=None, **extra):
    """Build one fully classified registry entry.

    All policy arguments are required keyword-only values.  The resulting
    dict always contains every gating field, so a reviewer can distinguish a
    deliberate False from an omitted/unaudited field.  `gating_of` remains
    strict for injected/future sparse entries.
    """
    entry = {
        "schema": dict(schema or {}),
        "mutating": bool(mutating),
        "executes_arbitrary_code": bool(executes_arbitrary_code),
        "remote_exposed": bool(remote_exposed),
        "runtime_required": bool(runtime_required),
        "batch_eligible": bool(batch_eligible),
        "side_effects": dict(side_effects or {}),
    }
    entry.update(extra)
    return entry


# These tools are genuine Envoy MCP tools, but not Convoy node operations.
# They deliberately stay out of the registry rather than being silently
# omitted.  Docs/guidance are bridge-facing content lookups; session/scope/
# task calls depend on the originating Envoy bridge session, whereas all host
# forwards necessarily share the `convoy-dispatch` Envoy session.  Relaying
# them would merge unrelated remote controllers into one false identity and
# create a second lease/task authority beside Convoy's own.
ENVOY_TOOL_EXCLUSIONS = {
    "get_docs": "bridge-side documentation lookup, not a node operation",
    "get_guidance": "bridge-side project guidance lookup, not a node operation",
    "get_sessions": "Envoy-local session identity is not preserved by relay",
    "claim_scope": "Envoy-local session lease; Convoy leases are authoritative",
    "release_scope": "Envoy-local session lease; Convoy leases are authoritative",
    "announce_task": "Envoy-local task attribution is not preserved by relay",
    "update_task": "Envoy-local task attribution is not preserved by relay",
    "preflight_landing": "bridge worktree workflow, not a TD node operation",
    "convoy_lifecycle_state":
        "host-private exact-runtime helper, session-gated and not relayable",
    "convoy_lifecycle_quit":
        "host-private exact-runtime helper, session-gated and not relayable",
}


PHASE1_OPERATIONS = {
    # Host-native liveness (not an Envoy tool).
    "convoy_ping": _operation(
        {}, mutating=False, executes_arbitrary_code=False,
        remote_exposed=True, runtime_required=False, batch_eligible=False),

    # Host-native repository/CLI work.  These deliberately use the same
    # durable delivery path as TD operations while never crossing Envoy or
    # waking TouchDesigner.  `convoy_git` is call-shape-sensitive: inspection
    # is read-only, while fetch/pull/push acquire the normal writer lease.
    "convoy_git": _operation(
        {"operation": "status|remotes|branches|current_branch|revision|"
                      "upstream|divergence|fetch|pull_ff_only|push_branch",
         "arguments": "object?", "timeout_s": "number?",
         "output_limit": "int?"},
        mutating=False, executes_arbitrary_code=False,
        remote_exposed=True, runtime_required=False, batch_eligible=False,
        side_effects={"execution_locus": "host_subprocess",
                      "mutating_when": "fetch_or_pull_or_push",
                      "capability": hostops_mod.HOST_GIT_CAPABILITY}),
    "convoy_gh": _operation(
        {"operation": "auth_status|repo_view|pr_list|pr_view|pr_checks|"
                      "workflow_list|run_list|run_view",
         "arguments": "object?", "timeout_s": "number?",
         "output_limit": "int?"},
        mutating=False, executes_arbitrary_code=False,
        remote_exposed=True, runtime_required=False, batch_eligible=False,
        side_effects={"execution_locus": "host_subprocess",
                      "capability": hostops_mod.HOST_GH_CAPABILITY}),
    "convoy_shell": _operation(
        {"command": "string", "cwd": "relative-directory?",
         "env_additions": "object?", "redact_values": "list?",
         "timeout_s": "number?", "output_limit": "int?"},
        mutating=True, executes_arbitrary_code=False,
        remote_exposed=True, runtime_required=False, batch_eligible=False,
        side_effects={"execution_locus": "host_subprocess",
                      "executes_full_shell": True,
                      "required_local_policy": "full_shell",
                      "capability": hostops_mod.HOST_SHELL_CAPABILITY}),
    "convoy_start_node": _operation(
        {"timeout_s": "number?"},
        mutating=True, executes_arbitrary_code=False,
        remote_exposed=True, runtime_required=False, batch_eligible=False,
        side_effects={"execution_locus": "host_lifecycle",
                      "starts_touchdesigner": True,
                      "full_shell_required": False,
                      "capability":
                      lifecycle_mod.HOST_LIFECYCLE_CAPABILITY}),
    "convoy_restart_node": _operation(
        {"policy": "require_clean|save_then_restart?",
         "timeout_s": "number?"},
        mutating=True, executes_arbitrary_code=False,
        remote_exposed=True, runtime_required=True, batch_eligible=False,
        side_effects={"execution_locus": "host_lifecycle",
                      "restarts_touchdesigner": True,
                      "full_shell_required": False,
                      "capability":
                      lifecycle_mod.HOST_LIFECYCLE_CAPABILITY}),

    # Operator creation / wiring / inspection.
    "create_op": _operation(
        {"parent_path": "string", "op_type": "string", "name": "string?"},
        mutating=True, executes_arbitrary_code=False, remote_exposed=True,
        runtime_required=False, batch_eligible=True,
        side_effects={"creates_operator": True}),
    "delete_op": _operation(
        {"op_path": "string", "override": "bool?"}, mutating=True,
        executes_arbitrary_code=False, remote_exposed=True,
        runtime_required=True, batch_eligible=True,
        side_effects={"deletes_operator": True}),
    "get_op": _operation(
        {"op_path": "string", "include_defaults": "bool?"}, mutating=False,
        executes_arbitrary_code=False, remote_exposed=True,
        runtime_required=False, batch_eligible=True),
    "set_parameter": _operation(
        {"op_path": "string", "par_name": "string", "value": "any?",
         "mode": "constant|expression|export|bind?", "expr": "string?",
         "bind_expr": "string?"}, mutating=True,
        # Dynamic: constants/exports stay available; expressions and binds
        # are promoted to arbitrary-code by effective_operation_gating().
        executes_arbitrary_code=False, remote_exposed=True,
        runtime_required=False, batch_eligible=True,
        side_effects={"arbitrary_code_when": "expression_or_bind"}),
    "get_parameter": _operation(
        {"op_path": "string", "par_name": "string?", "search": "string?",
         "search_in": "string?", "depth": "int?", "max_results": "int?",
         "details": "bool?"}, mutating=False, executes_arbitrary_code=False,
        remote_exposed=True, runtime_required=False, batch_eligible=True),
    "connect_ops": _operation(
        {"source_path": "string", "dest_path": "string",
         "source_index": "int?", "dest_index": "int?", "comp": "bool?"},
        mutating=True, executes_arbitrary_code=False, remote_exposed=True,
        runtime_required=False, batch_eligible=True),
    "disconnect_op": _operation(
        {"op_path": "string", "input_index": "int?", "comp": "bool?"},
        mutating=True, executes_arbitrary_code=False, remote_exposed=True,
        runtime_required=False, batch_eligible=True),
    "query_network": _operation(
        {"parent_path": "string?", "recursive": "bool?", "op_type": "string?",
         "include_utility": "bool?"}, mutating=False,
        executes_arbitrary_code=False, remote_exposed=True,
        runtime_required=False, batch_eligible=True),
    "copy_op": _operation(
        {"source_path": "string", "dest_parent": "string",
         "new_name": "string?"}, mutating=True, executes_arbitrary_code=False,
        remote_exposed=True, runtime_required=False, batch_eligible=True),
    "get_connections": _operation(
        {"op_path": "string"}, mutating=False, executes_arbitrary_code=False,
        remote_exposed=True, runtime_required=False, batch_eligible=True),

    # Immediate caller-controlled execution.  These are exposed only because
    # the host-private policy store (policy.allow_td_python, local
    # confirmation required, fail-closed) is checked at admission and again
    # under the app lock immediately before dispatch.  The directory's
    # td_python_approved field is a display projection, never the authority.
    # Residual window, accepted under best-effort cancellation: a disable
    # landing after the dispatch check cannot recall the one already-
    # forwarded operation.
    "execute_python": _operation(
        {"code": "string"}, mutating=True, executes_arbitrary_code=True,
        remote_exposed=True, runtime_required=True, batch_eligible=True,
        side_effects={"executes_python": True}),
    "exec_op_method": _operation(
        {"op_path": "string", "method": "string", "args": "list?",
         "kwargs": "dict?"}, mutating=True, executes_arbitrary_code=True,
        remote_exposed=True, runtime_required=True, batch_eligible=True,
        side_effects={"dynamic_method_dispatch": True}),

    # Introspection and diagnostics.
    "get_td_info": _operation(
        {}, mutating=False, executes_arbitrary_code=False,
        remote_exposed=True, runtime_required=False, batch_eligible=True),
    "get_focus": _operation(
        {}, mutating=False, executes_arbitrary_code=False,
        remote_exposed=True, runtime_required=False, batch_eligible=True),
    "get_op_errors": _operation(
        {"op_path": "string", "recurse": "bool?"}, mutating=False,
        executes_arbitrary_code=False, remote_exposed=True,
        runtime_required=False, batch_eligible=True,
        side_effects={"may_cook": True}),
    "get_td_classes": _operation(
        {}, mutating=False, executes_arbitrary_code=False,
        remote_exposed=True, runtime_required=False, batch_eligible=True),
    "get_td_class_details": _operation(
        {"class_name": "string"}, mutating=False,
        executes_arbitrary_code=False, remote_exposed=True,
        runtime_required=False, batch_eligible=True),
    "get_module_help": _operation(
        {"module_name": "string"}, mutating=False,
        executes_arbitrary_code=False, remote_exposed=True,
        runtime_required=False, batch_eligible=True),

    # DAT content.  Writing a DAT can immediately reinitialize an extension
    # or fire an Execute/Callback DAT, so both write forms use the same
    # explicit TD-Python approval as execute_python.
    "get_dat_content": _operation(
        {"op_path": "string", "format": "string?"}, mutating=False,
        executes_arbitrary_code=False, remote_exposed=True,
        runtime_required=False, batch_eligible=True),
    # Reduced family reads. Both force-cook their target before reading
    # (the capture_top precedent), hence may_cook. get_pop_data is the
    # expensive one: reading point VALUES is a GPU->CPU readback measured
    # at ~69ms for 160k points, so it is gated node-side on the operator's
    # total point count (max_points, default 50k) and returns metadata
    # only unless samples>0 is asked for.
    "get_chop_data": _operation(
        {"op_path": "string", "channels": "string?", "samples": "int?",
         "compare_to": "string?"}, mutating=False,
        executes_arbitrary_code=False, remote_exposed=True,
        runtime_required=False, batch_eligible=True,
        side_effects={"may_cook": True}),
    "get_pop_data": _operation(
        {"op_path": "string", "attributes": "string?", "samples": "int?",
         "max_points": "int?"}, mutating=False,
        executes_arbitrary_code=False, remote_exposed=True,
        runtime_required=False, batch_eligible=True,
        side_effects={"may_cook": True}),
    "set_dat_content": _operation(
        {"op_path": "string", "text": "string?", "rows": "list?",
         "clear": "bool?", "confirm_wipe": "bool?"}, mutating=True,
        executes_arbitrary_code=True, remote_exposed=True,
        runtime_required=True, batch_eligible=True,
        side_effects={"writes_dat": True, "may_execute_dat": True}),
    "edit_dat_content": _operation(
        {"op_path": "string", "old_string": "string",
         "new_string": "string", "replace_all": "bool?",
         "confirm_wipe": "bool?"}, mutating=True,
        executes_arbitrary_code=True, remote_exposed=True,
        runtime_required=True, batch_eligible=True,
        side_effects={"writes_dat": True, "may_execute_dat": True}),

    # Flags, layout, annotations and extended operator operations.
    "get_op_flags": _operation(
        {"op_path": "string"}, mutating=False, executes_arbitrary_code=False,
        remote_exposed=True, runtime_required=False, batch_eligible=True),
    "set_op_flags": _operation(
        {"op_path": "string", "bypass": "bool?", "lock": "bool?",
         "display": "bool?", "render": "bool?", "viewer": "bool?",
         "current": "bool?", "expose": "bool?", "allowCooking": "bool?",
         "selected": "bool?"}, mutating=True, executes_arbitrary_code=False,
        remote_exposed=True, runtime_required=False, batch_eligible=True),
    "get_op_position": _operation(
        {"op_path": "string"}, mutating=False, executes_arbitrary_code=False,
        remote_exposed=True, runtime_required=False, batch_eligible=True),
    "get_network_layout": _operation(
        {"comp_path": "string", "include_annotations": "bool?"},
        mutating=False, executes_arbitrary_code=False, remote_exposed=True,
        runtime_required=False, batch_eligible=True),
    "set_op_position": _operation(
        {"op_path": "string", "x": "number?", "y": "number?",
         "width": "number?", "height": "number?", "color": "list?",
         "comment": "string?"}, mutating=True, executes_arbitrary_code=False,
        remote_exposed=True, runtime_required=False, batch_eligible=True,
        side_effects={"layout": True}),
    "layout_children": _operation(
        {"op_path": "string"}, mutating=True, executes_arbitrary_code=False,
        remote_exposed=True, runtime_required=False, batch_eligible=True,
        side_effects={"layout": True}),
    "create_annotation": _operation(
        {"parent_path": "string", "mode": "string?", "text": "string?",
         "title": "string?", "x": "number?", "y": "number?",
         "width": "number?", "height": "number?", "color": "list?",
         "opacity": "number?", "name": "string?"}, mutating=True,
        executes_arbitrary_code=False, remote_exposed=True,
        runtime_required=False, batch_eligible=True),
    "get_annotations": _operation(
        {"parent_path": "string"}, mutating=False,
        executes_arbitrary_code=False, remote_exposed=True,
        runtime_required=False, batch_eligible=True),
    "set_annotation": _operation(
        {"op_path": "string", "text": "string?", "title": "string?",
         "color": "list?", "opacity": "number?", "width": "number?",
         "height": "number?", "x": "number?", "y": "number?"},
        mutating=True, executes_arbitrary_code=False, remote_exposed=True,
        runtime_required=False, batch_eligible=True),
    "get_enclosed_ops": _operation(
        {"op_path": "string"}, mutating=False, executes_arbitrary_code=False,
        remote_exposed=True, runtime_required=False, batch_eligible=True),
    "rename_op": _operation(
        {"op_path": "string", "new_name": "string"}, mutating=True,
        executes_arbitrary_code=False, remote_exposed=True,
        runtime_required=False, batch_eligible=True),
    "cook_op": _operation(
        {"op_path": "string", "force": "bool?", "recurse": "bool?"},
        mutating=True, executes_arbitrary_code=False, remote_exposed=True,
        runtime_required=True, batch_eligible=True,
        side_effects={"cooks": True}),
    "find_children": _operation(
        {"op_path": "string", "name": "string?", "type": "string?",
         "depth": "int?", "tags": "list?", "text": "string?",
         "comment": "string?", "include_utility": "bool?"}, mutating=False,
        executes_arbitrary_code=False, remote_exposed=True,
        runtime_required=False, batch_eligible=True),
    "get_op_performance": _operation(
        {"op_path": "string", "include_children": "bool?"}, mutating=False,
        executes_arbitrary_code=False, remote_exposed=True,
        runtime_required=False, batch_eligible=True),
    "get_project_performance": _operation(
        {"include_hotspots": "int?"}, mutating=False,
        executes_arbitrary_code=False, remote_exposed=True,
        runtime_required=False, batch_eligible=True),

    # Embody externalization and TDN network operations.
    "externalize_op": _operation(
        {"op_path": "string", "tag_type": "string?"}, mutating=True,
        executes_arbitrary_code=False, remote_exposed=True,
        runtime_required=True, batch_eligible=True,
        side_effects={"writes_project_files": True}),
    "remove_externalization_tag": _operation(
        {"op_path": "string", "delete_file": "bool?"}, mutating=True,
        executes_arbitrary_code=False, remote_exposed=True,
        runtime_required=True, batch_eligible=True,
        side_effects={"may_delete_project_file": True}),
    "get_externalizations": _operation(
        {}, mutating=False, executes_arbitrary_code=False,
        remote_exposed=True, runtime_required=False, batch_eligible=True),
    "save_externalization": _operation(
        {"op_path": "string"}, mutating=True, executes_arbitrary_code=False,
        remote_exposed=True, runtime_required=True, batch_eligible=True,
        side_effects={"writes_project_files": True}),
    "get_externalization_status": _operation(
        {"op_path": "string"}, mutating=False, executes_arbitrary_code=False,
        remote_exposed=True, runtime_required=False, batch_eligible=True),
    "create_extension": _operation(
        {"parent_path": "string", "class_name": "string", "name": "string?",
         "code": "string?", "promote": "bool?", "ext_name": "string?",
         "ext_index": "int?", "existing_comp": "bool?"}, mutating=True,
        executes_arbitrary_code=True, remote_exposed=True,
        runtime_required=True, batch_eligible=True,
        side_effects={"creates_extension": True, "executes_python": True}),
    "export_network": _operation(
        {"root_path": "string?", "include_dat_content": "bool?",
         "output_file": "string?", "max_depth": "int?", "embed_all": "bool?"},
        # output_file makes this a filesystem write even though the ordinary
        # in-memory form is a read. Static conservative classification keeps
        # observe-only peers from gaining an arbitrary write path.
        mutating=True, executes_arbitrary_code=False, remote_exposed=True,
        runtime_required=True, batch_eligible=True,
        side_effects={"may_write_output_file": True}),
    "import_network": _operation(
        {"target_path": "string", "tdn": "dict", "clear_first": "bool?",
         "override": "bool?"}, mutating=True, executes_arbitrary_code=True,
        remote_exposed=True, runtime_required=True, batch_eligible=True,
        side_effects={"imports_code_and_expressions": True,
                      "may_delete_operators": True}),
    "read_tdn": _operation(
        {"comp_path": "string?", "include_dat_content": "bool?",
         "max_depth": "int?", "embed_all": "bool?"}, mutating=False,
        executes_arbitrary_code=False, remote_exposed=True,
        runtime_required=False, batch_eligible=True),
    "diff_tdn": _operation(
        {"target": "string?", "max_changed_ops": "int?", "max_bytes": "int?"},
        mutating=False, executes_arbitrary_code=False, remote_exposed=True,
        runtime_required=False, batch_eligible=True),

    # Visuals, logs and node-side background jobs.
    "capture_top": _operation(
        {"op_path": "string", "format": "jpeg|png?", "quality": "number?",
         "max_resolution": "int?", "inline": "bool?", "sample_grid": "int?"},
        mutating=False, executes_arbitrary_code=False, remote_exposed=True,
        runtime_required=False, batch_eligible=True,
        side_effects={"cooks": True, "writes_temp_image": True}),
    "get_logs": _operation(
        {"level": "string?", "count": "int?", "since_id": "int?",
         "source": "string?"}, mutating=False, executes_arbitrary_code=False,
        remote_exposed=True, runtime_required=False, batch_eligible=True),
    # A-1 / R-2: NEVER relayable to a remote peer. TestRunnerExt discovers
    # suites by SCANNING DISK (every test_*.py AND test_*.txt it finds), so
    # "the code already in the project" is only a loopback assumption -- it
    # dissolves the moment a socket binds off-box. It also stays classified
    # arbitrary-code so the local path still demands TD-Python approval.
    # Returns to the remote surface only behind A-30/A-31, never ungated.
    "run_tests": _operation(
        {"suite_name": "string?", "test_name": "string?"}, mutating=True,
        executes_arbitrary_code=True, remote_exposed=False,
        runtime_required=True, batch_eligible=False,
        side_effects={"runs_tests": True, "writes_logs": True,
                      "may_restart_server": True, "executes_project_code": True},
        async_job={"kind": "run_tests", "key_arg": "idempotency_key",
                   "caller_args": ("suite_name", "test_name"),
                   "inject": {"background": True, "override": False}}),
    # Worker-side, but explicitly relayable because controllers need it to
    # poll handles returned by run_tests/save_project.
    "get_job_status": _operation(
        {"job_id": "string?"}, mutating=False, executes_arbitrary_code=False,
        remote_exposed=True, runtime_required=False, batch_eligible=False),
    # NOT relayable to a remote peer: it blocks TD's main thread 15+ seconds
    # and restarts the Envoy server under itself. A-30's performance guard
    # and A-31's per-node remote-work policy -- the machinery whose whole job
    # is to stop a remote peer wrecking a live output -- are Phase 4. Returns
    # to the remote surface in Phase 4, gated, never ungated.
    "save_project": _operation(
        {}, mutating=True, executes_arbitrary_code=False, remote_exposed=False,
        runtime_required=True, batch_eligible=False,
        side_effects={"writes_toe": True, "blocks_main_thread": True,
                      "restarts_server": True},
        async_job={"kind": "save_project", "key_arg": "idempotency_key",
                   "caller_args": (), "inject": {}}),
    # Bounded self-update: the node's own UpdaterExt fetches the official
    # GitHub release, verifies the sha256-pinned manifest, refuses
    # downgrades and TD-build-floor violations, and swaps the component in
    # place. No caller-supplied code or URL ever crosses the wire, so this
    # is NOT arbitrary-code -- the whole point is that patching a fleet
    # must not require the TD Python grant (field 2026-08-19).
    # Remote-exposed unlike run_tests/save_project (finding 544): those are
    # excluded for caller-code execution and unprotected 15s+ main-thread
    # blocks; this one runs no caller code and the node itself refuses in
    # Perform Mode, so it is no more consequential than the already-remote
    # delete_op/set_parameter surface.
    "update_embody": _operation(
        {}, mutating=True, executes_arbitrary_code=False, remote_exposed=True,
        runtime_required=True, batch_eligible=False,
        side_effects={"replaces_component": True, "network_fetch": True,
                      "restarts_server": True},
        async_job={"kind": "update_embody", "key_arg": "idempotency_key",
                   "caller_args": (), "inject": {}}),
    # A batch is a neutral container. Its effective gates are the union of
    # every validated child, computed recursively before admission and again
    # before dispatch. Envoy itself rejects nested batches, so the host does
    # too rather than advertising a shape the target cannot execute.
    "batch_operations": _operation(
        {"operations": "list", "override": "bool?"}, mutating=False,
        executes_arbitrary_code=False, remote_exposed=True,
        runtime_required=False, batch_eligible=False,
        side_effects={"gating": "union_of_children"}),
}

# These operations terminate on the host itself.  They still traverse the
# durable admission/lease/idempotency state machine, but they must never be
# mistaken for an Envoy tool: no Envoy port, TD heartbeat, runtime precondition,
# or Perform wake is relevant to them.
HOST_SUBPROCESS_OPERATIONS = frozenset(
    {"convoy_git", "convoy_gh", "convoy_shell"})
HOST_LIFECYCLE_OPERATIONS = frozenset(
    {"convoy_start_node", "convoy_restart_node"})
HOST_NATIVE_OPERATIONS = frozenset(
    set(HOST_SUBPROCESS_OPERATIONS) | set(HOST_LIFECYCLE_OPERATIONS)
    | {"convoy_ping"})
HOST_CANCELABLE_OPERATIONS = frozenset(
    set(HOST_SUBPROCESS_OPERATIONS) | set(HOST_LIFECYCLE_OPERATIONS))

# How many pending deliveries a census NAMES to a human. Display only:
# the census itself collects every pending delivery, because the
# retirement gate cannot refuse a queue it cannot see and would have to
# spare any row whose queue exceeded a cap -- a constant silently
# deciding correctness, and permanently unretirable rows for exactly the
# reason this whole change exists to remove. The tuples are (id, state,
# operation) and the count of PENDING deliveries on one node is small in
# any real deployment (the 2026-08-06 field machine's worst row held
# one); the scan that produces them is already O(all jobs) either way.
_CENSUS_NAMED_CAP = 8

# "no census was supplied", distinct from "the scan FAILED" (index None).
# Overloading None for both made a failed hoisted scan re-run the whole
# listing once per candidate row, under the app lock, on the one path
# where the listing is already failing.
_CENSUS_UNSET = object()

# The strict reading of a registry entry: anything a registry entry does
# not say is assumed to be the most dangerous answer. `executes_arbitrary
# _code` True is A-1 verbatim; `mutating` True demands the exclusive
# lease; `runtime_required` True demands the A-22 precondition;
# `remote_exposed` FALSE means an operation nobody audited for the LAN is
# not reachable from it -- absence is a refusal, exactly like absence
# from the registry itself.
_GATING_DEFAULTS = {
    "executes_arbitrary_code": True,
    "mutating": True,
    "runtime_required": True,
    "remote_exposed": False,
}


def gating_of(entry):
    """The gating triple actually enforced for a registry entry, strict
    defaults applied. ONE reader, so the enforced value and the value in
    the capability digest can never drift apart."""
    return {field: bool(entry.get(field, default))
            for field, default in _GATING_DEFAULTS.items()}


# `executes_arbitrary_code` is the irreducible A-1 audit bit: if it is not
# stated, approval cannot make the operation safe because nobody classified
# it. The other missing fields retain gating_of's strict defaults (mutating,
# runtime-bound, and not remotely exposed), preserving the useful fail-closed
# behaviour for tests/extensions that install a local-only registry entry.
_REQUIRED_GATING_FIELDS = frozenset({"executes_arbitrary_code"})
_BATCH_OPERATION_LIMIT = 512

# Remote calls must not turn off Convoy, enable a more dangerous permission,
# or otherwise rewrite the control plane that authorizes the call itself.
# Embody's Convoy custom parameters are prefixed `Convoy`; the unprefixed
# names cover the user-facing names planned for standalone/subcomponent UIs.
_RESERVED_CONVOY_PARAMETER_NAMES = frozenset({
    "remotewake", "allowtdpython", "allowexecutetdpython",
    "allowfullshell", "artifactquota", "convoyenable",
})


class OperationRegistryError(Exception):
    """A request cannot be represented safely by the operation registry."""

    def __init__(self, reason, detail, code=403):
        super().__init__(detail)
        self.reason = reason
        self.detail = detail
        self.code = code


def _normalized_parameter_name(value):
    if not isinstance(value, str):
        return ""
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _parameter_executes_code(arguments):
    """Whether set_parameter activates caller-controlled Python semantics."""
    if not isinstance(arguments, dict):
        return False
    mode = arguments.get("mode")
    mode = mode.lower() if isinstance(mode, str) else ""
    return (mode in ("expression", "bind")
            or arguments.get("expr") is not None
            or arguments.get("bind_expr") is not None)


def _validate_host_operation_arguments(operation, arguments):
    """Validate the host-operation wrapper and return its catalog action.

    The subprocess facade validates every typed action argument again.  This
    first door exists so unknown wrapper fields/actions are refused before a
    durable job is accepted, and so dynamic Git mutation gating cannot be
    tricked by a malformed shape.
    """
    if not isinstance(arguments, dict):
        raise OperationRegistryError(
            "malformed", f"{operation} arguments must be an object", 400)
    common = {"timeout_s", "output_limit"}
    if operation in ("convoy_git", "convoy_gh"):
        allowed = common | {"operation", "arguments"}
        unknown = sorted(set(arguments) - allowed)
        if unknown:
            raise OperationRegistryError(
                "malformed",
                f"{operation} has unknown field {unknown[0]!r}", 400)
        action = arguments.get("operation")
        catalog = (hostops_mod.GIT_CATALOG if operation == "convoy_git"
                   else hostops_mod.GH_CATALOG)
        if not isinstance(action, str) or action not in catalog:
            raise OperationRegistryError(
                "host_operation_not_exposed",
                f"{action!r} is not in {operation}'s reviewed catalog", 403)
        nested = arguments.get("arguments", {})
        if not isinstance(nested, dict):
            raise OperationRegistryError(
                "malformed", f"{operation}.arguments must be an object", 400)
        return action
    if operation == "convoy_shell":
        allowed = common | {
            "command", "cwd", "env_additions", "redact_values"}
        unknown = sorted(set(arguments) - allowed)
        if unknown:
            raise OperationRegistryError(
                "malformed",
                f"convoy_shell has unknown field {unknown[0]!r}", 400)
        command = arguments.get("command")
        if not isinstance(command, str) or not command.strip():
            raise OperationRegistryError(
                "malformed", "convoy_shell.command must be non-empty text",
                400)
        return "execute"
    raise OperationRegistryError(
        "operation_not_exposed",
        f"{operation!r} is not a host subprocess operation")


def _validate_lifecycle_operation_arguments(operation, arguments):
    """Validate the small exact-node lifecycle surface before admission.

    The durable delivery id is the lifecycle idempotency key; callers may
    not inject a second operation id, executable, project path, environment,
    or argv.  Those launch facts come only from a locally verified profile.
    """
    if not isinstance(arguments, dict):
        raise OperationRegistryError(
            "malformed", f"{operation} arguments must be an object", 400)
    allowed = {"timeout_s"}
    if operation == "convoy_restart_node":
        allowed.add("policy")
    unknown = sorted(set(arguments) - allowed)
    if unknown:
        raise OperationRegistryError(
            "malformed",
            f"{operation} has unknown field {unknown[0]!r}", 400)
    timeout_s = arguments.get("timeout_s")
    if timeout_s is not None and (
            isinstance(timeout_s, bool)
            or not isinstance(timeout_s, (int, float))
            or not math.isfinite(float(timeout_s))
            or not lifecycle_mod.MIN_TIMEOUT_S <= float(timeout_s)
            <= lifecycle_mod.MAX_TIMEOUT_S):
        raise OperationRegistryError(
            "malformed", "lifecycle timeout_s is outside the allowed range",
            400)
    if operation == "convoy_restart_node":
        policy = arguments.get("policy", "require_clean")
        # Destructive policies exist in the internal lifecycle engine for
        # future locally-approved recovery tooling, but Convoy v1 exposes no
        # local approval UX.  Do not advertise or accept a policy that every
        # shipped host must subsequently refuse.
        if policy not in {"require_clean", "save_then_restart"}:
            raise OperationRegistryError(
                "malformed", "restart policy is not supported", 400)


def effective_operation_gating(registry, operation, arguments=None,
                               *, _in_batch=False):
    """Return the gates for this exact call, validating batch children.

    Registry entries are static capability declarations. Two call shapes need
    a stricter dynamic answer: set_parameter only becomes arbitrary-code for
    expression/bind forms, and batch_operations inherits the union of every
    child. Unknown, sparse, malformed, nested, or non-batchable children are
    refused here -- before enqueue -- so batching can never bypass the same
    policy applied to a top-level call.
    """
    entry = registry.get(operation)
    if entry is None:
        raise OperationRegistryError(
            "operation_not_exposed",
            f"{operation!r} is not in this host's operation registry")
    if not isinstance(entry, dict) or not _REQUIRED_GATING_FIELDS.issubset(entry):
        raise OperationRegistryError(
            "operation_not_relayable",
            f"{operation!r} has no complete audited gate classification")
    if _in_batch and operation == "batch_operations":
        raise OperationRegistryError(
            "nested_batch_not_allowed",
            "nested batch_operations is not supported")
    if _in_batch and not bool(entry.get("batch_eligible", False)):
        raise OperationRegistryError(
            "operation_not_batchable",
            f"{operation!r} cannot execute inside batch_operations")

    gating = gating_of(entry)
    if operation == "set_parameter":
        par_name = _normalized_parameter_name(
            arguments.get("par_name") if isinstance(arguments, dict) else None)
        if par_name.startswith("convoy") or par_name in _RESERVED_CONVOY_PARAMETER_NAMES:
            raise OperationRegistryError(
                "reserved_parameter",
                "Convoy control parameters cannot be changed through Convoy")
        if _parameter_executes_code(arguments):
            gating["executes_arbitrary_code"] = True

    if operation in HOST_SUBPROCESS_OPERATIONS:
        action = _validate_host_operation_arguments(operation, arguments)
        if operation == "convoy_git":
            gating["mutating"] = bool(
                hostops_mod.GIT_CATALOG[action]["mutating"])
    elif operation in HOST_LIFECYCLE_OPERATIONS:
        _validate_lifecycle_operation_arguments(operation, arguments)

    if operation != "batch_operations":
        return gating
    if not isinstance(arguments, dict):
        raise OperationRegistryError(
            "malformed", "batch_operations arguments must be an object", 400)
    children = arguments.get("operations")
    if not isinstance(children, list):
        raise OperationRegistryError(
            "malformed", "batch_operations.operations must be a list", 400)
    if len(children) > _BATCH_OPERATION_LIMIT:
        raise OperationRegistryError(
            "malformed",
            f"batch_operations exceeds {_BATCH_OPERATION_LIMIT} operations", 400)

    combined = dict(gating)
    for index, child in enumerate(children):
        if not isinstance(child, dict):
            raise OperationRegistryError(
                "malformed", f"batch operation {index} must be an object", 400)
        child_name = child.get("tool")
        child_arguments = child.get("params", {})
        if not isinstance(child_name, str) or not child_name:
            raise OperationRegistryError(
                "malformed", f"batch operation {index} needs a tool name", 400)
        if not isinstance(child_arguments, dict):
            raise OperationRegistryError(
                "malformed", f"batch operation {index} params must be an object", 400)
        child_gating = effective_operation_gating(
            registry, child_name, child_arguments, _in_batch=True)
        combined["mutating"] = (combined["mutating"]
                                or child_gating["mutating"])
        combined["executes_arbitrary_code"] = (
            combined["executes_arbitrary_code"]
            or child_gating["executes_arbitrary_code"])
        combined["runtime_required"] = (
            combined["runtime_required"]
            or child_gating["runtime_required"])
        combined["remote_exposed"] = (
            combined["remote_exposed"]
            and child_gating["remote_exposed"])
    return combined


def operation_capability_side_effects(entry):
    """Digest material beyond the four top-level gate booleans.

    Batch eligibility changes whether the same named tool can execute in a
    batch and async-ness changes result into handle semantics; both are wire
    compatibility, not cosmetic registry metadata.
    """
    side_effects = dict(entry.get("side_effects") or {})
    side_effects["batch_eligible"] = bool(entry.get("batch_eligible", False))
    if entry.get("async_job"):
        side_effects["async_job"] = True
    return side_effects


# HTTP status per EnvelopeRejected/LeaseError reason. The STRUCTURED
# reason is the contract; the HTTP code is a coarse class: 4xx malformed,
# 403 authentication/namespace, 409 state conflict, 410 expired.
_REFUSAL_HTTP = {
    "bad_signature": 403,
    "arguments_tampered": 403,
    "namespace_mismatch": 403,
    "algorithm_mismatch": 403,
    "malformed": 400,
    "runtime_id_required": 400,
    "wrong_target": 409,
    "runtime_changed": 409,
    "runtime_unverifiable": 409,
    "hop_limit_exceeded": 409,
    "deadline_mismatch": 400,
    "timestamp_out_of_window": 410,
    "node_leased": 409,
    "shared_lease_no_mutation": 409,
    "deadline_exceeded": 410,
    # Peer authorization (Phase 3 slice 2). All 403: every one of them is
    # "this host will not hear you", not "you sent something malformed".
    peers_mod.REASON_BLOCKED: 403,
    peers_mod.REASON_UNKNOWN: 403,
    peers_mod.REASON_PIN_MISMATCH: 403,
    peers_mod.REASON_OBSERVE_ONLY: 403,
    peers_mod.REASON_NAMESPACE: 403,
    # Peer TRANSPORT (Phase 3 slice 3). Channel binding and an unusable
    # pinned key are both "this host will not hear you" refusals.
    "source_mismatch": 403,
    "peer_key_unusable": 403,
}

# Bounds for caller-supplied text. Ids are host-minted 32-hex or
# controller-chosen labels; nothing legitimate approaches these.
MAX_ID_CHARS = 128
MAX_OPERATION_CHARS = 128
# One host can run many TD processes, but a few-dozen-machine Convoy is the
# product ceiling today.  Bound both the peer-facing directory and the local
# aggregation so a faulty admitted host cannot turn a status refresh into an
# unbounded memory/JSON operation.
MAX_PUBLIC_NODES_PER_HOST = 256
MAX_NETWORK_NODE_ROWS = 4096
MAX_PUBLIC_CONTROLLERS_PER_HOST = 512
MAX_ACTIVE_JOBS_PER_CONTROLLER = 128
MAX_NETWORK_CONTROLLER_ROWS = 4096
NETWORK_QUERY_TIMEOUT_S = 2.0
# A status client often asks for the same directory several times while it
# renders one view.  Keep that hot result briefly; membership mutations clear
# it immediately, and the monotonic TTL prevents clock changes extending it.
NETWORK_NODE_CACHE_TTL_S = 2.0
# A coalesced caller waits a little longer than the peer socket budget so the
# leader has time to collect and project completed futures without causing a
# second fanout wave.
NETWORK_NODE_FLIGHT_WAIT_S = NETWORK_QUERY_TIMEOUT_S + 1.0
# A directory read that observes a membership mutation landing DURING its
# refresh recomputes rather than return a projection that predates the caller's
# own write.  Bounded so sustained churn returns a stale-marked projection
# instead of looping forever.
NETWORK_NODE_MAX_REFRESH_ATTEMPTS = 3
# The passive directory/status reads must not do one durable job-file read per
# live operation claim under the app lock on every call: a 1 Hz /status poller
# once drove /peers to hundreds of times its normal latency that way.  The
# mutation/dispatch paths reconcile eagerly; the read paths reconcile at most
# once per this interval so a burst of readers cannot multiply the file I/O.
OPERATION_CLAIM_READ_RECONCILE_TTL_S = 1.0
MANIFEST_PREFLIGHT_TIMEOUT_S = 2.0
MAX_PEER_MANIFEST_CACHE = 128
MAX_PEER_MANIFEST_OPERATIONS = 1024
_MANIFEST_DIGEST_RE = re.compile(r"^mf1-[0-9a-f]{24}$")
_OPERATION_DIGEST_RE = re.compile(r"^op1-[0-9a-f]{24}$")
# One wave across every sibling at the supported 30-host ceiling.  The calls
# use separate per-target connection locks; this does not permit concurrent
# requests to squat on one peer channel.
NETWORK_QUERY_WORKERS = 32
# TD re-registers every ~30 seconds. Two missed beats is the agreed grace:
# enough for ordinary frame stalls, finite enough that a hard-killed process
# does not remain "online" forever because its old Envoy port was retained.
NODE_HEARTBEAT_GRACE_S = 60.0

# Cap on the in-memory (peer -> controller) map, matching the drain and
# poll maps: nothing prunes it on a host whose peers churn, and an
# unbounded map fed by remote input is a slow memory leak with a caller.
MAX_PEER_CONTROLLERS = 2048

# FLOOR for the drain bookkeeping maps' size cap. The effective cap
# SCALES with the live queue (2x the last drain snapshot, see
# drain_once): at a FIXED 2048, a backlog one entry over the cap made
# every insertion evict another LIVE entry, and the eviction CASCADED --
# each re-attempt evicted the next job's pacing, so a single pass over
# the cap unpaced and re-audited the ENTIRE queue, not just the
# overflow (measured 2026-08-02: 2100 queued refusals -> second pass
# backoff=0, all 2100 re-attempted and re-audited inside the 30s
# window). The floor keeps the maps bounded on a loop-off host where no
# pass ever runs to scale them.
DRAIN_MAP_FLOOR = 2048
# Poll and dispatch passes overlap independent nodes, but never queue an
# unbounded future per job. One rolling slot per target prevents eight queued
# calls to a hung TouchDesigner instance from starving every healthy node.
PASS_MAX_WORKERS = 8


class _LoopbackOrigin:
    """The sentinel meaning "this arrived over the loopback route".

    submit_envelope's `origin` is REQUIRED and has no default, and this
    is why: a default of None fails OPEN -- it means "this host is the
    origin" and short-circuits peer authorization entirely, so the whole
    slice's security rested on slice 3's listener remembering to pass an
    origin. Omitting it now raises TypeError at the call site instead.
    None is NOT this sentinel: a caller that computed an origin and got
    None is refused, not trusted.
    """

    def __repr__(self):
        return "<loopback origin: IPC token, this host>"


LOOPBACK_ORIGIN = _LoopbackOrigin()

# Which summary bucket a per-job refusal reason falls in. A DICT, not a
# chain of `in (...)` tuples: as tuples, a refusal reason added later
# silently landed in `errors` and the pass summary quietly lied about
# what happened. Anything not named here is counted as an error on
# purpose -- an unbucketed reason is a bug, and it must be visible.
_DRAIN_BUCKET = {
    "node_disabled": "refused",
    "namespace_mismatch": "refused",
    "full_shell_not_approved": "refused",
    "node_unreachable": "unreachable",
    "claim_lost": "unreachable",
    "no_node_job_handle": "no_handle",
    # A stored-arguments defect: permanent, not paced -- an anomaly the
    # summary must show rather than bury in 'deferred'.
    "malformed_arguments": "errors",
    "node_endpoint_unknown": "deferred",
    "node_endpoint_stale": "deferred",
    "runtime_changed": "deferred",
    "operation_not_exposed": "deferred",
    "operation_not_relayable": "deferred",
    "store_unavailable": "errors",
    # The peer that submitted this job is not allowed to right now, but a
    # SWITCH is what is refusing it (the A-32 killswitch, a denylist
    # entry, a store this host temporarily cannot read). Membership was
    # not touched, so the job is skipped and paced rather than burnt --
    # flipping the switch back must bring exactly this work with it.
    "origin_not_admitted": "deferred",
    # A MEMBERSHIP DECISION was taken instead (blocked, forgotten,
    # narrowed to observe-only, re-pinned, or the operation taken off the
    # remote surface), so the job was terminalised. NOTE the distinction
    # is NOT "reversibility": /peers/admit can undo a block, a forget and
    # a narrowing alike. It is that a human decided about this PEER, and
    # re-admitting them consents to the peer, not to a specific piece of
    # work they submitted before. Its own bucket, because counting a
    # burnt job as 'deferred' would say the pass left it for later when
    # the pass ended it.
    "origin_revoked": "refused",
}

# The same, for a poll pass. 'unreachable' covers BOTH ways a poll can
# fail to observe the node (refused connection, no usable answer): each
# leaves the job running and teaches nothing, so they are one bucket.
# A running job with no node provenance is an anomaly, not a pacing
# state -- it counts as an error so it cannot hide.
_POLL_BUCKET = {
    "node_unreachable": "unreachable",
    "poll_no_response": "unreachable",
    # The node answered but does not (yet) know the job: still an
    # unobserved job, and the grace window has not elapsed.
    "node_forgot_job_pending": "unreachable",
    "node_endpoint_unknown": "deferred",
    "poll_in_flight": "skipped",
    "no_node_provenance": "errors",
    "poll_id_mismatch": "errors",
    "unknown_job": "errors",
    "unknown_node": "errors",
    "poll_recording_failed": "errors",
    "malformed": "errors",
}

# What a COMPLETED poll's resulting job state counts as.
_POLL_STATE_BUCKET = {"succeeded": "finished", "failed": "failed",
                      "indeterminate": "indeterminate",
                      "running": "running"}


class Malformed(Exception):
    """A caller-supplied field is the wrong type/shape. Raised by the
    field readers so every handler answers with ONE named 400 instead of
    letting a TypeError become an unaudited 500."""

    def __init__(self, detail):
        super().__init__(detail)
        self.detail = detail


def text_field(body, name, required=True, limit=MAX_ID_CHARS):
    """Read a string field from a request body, or raise Malformed.

    Every id, operation name, and convoy id arrives from an untrusted
    caller and is then used as a DICT KEY. An unhashable value (a list
    or dict from JSON) raises TypeError at the lookup, which the
    handler's catch-all turns into an unaudited 500 -- a refusal the
    audit trail cannot classify (A-39). Reading fields through here
    makes the refusal named, audited, and a 400.
    """
    value = body.get(name)
    if value is None or value == "":
        if required:
            raise Malformed(f"{name} is required")
        return ""
    if not isinstance(value, str):
        raise Malformed(f"{name} must be a string, got "
                        f"{type(value).__name__}")
    if len(value) > limit:
        raise Malformed(f"{name} exceeds {limit} characters")
    return value


def dict_field(body, name):
    """Read an OBJECT field from a request body, or raise Malformed.

    `arguments` is the one caller-supplied field that is neither an id
    nor a scalar, and it becomes the node tool's KEYWORD ARGUMENTS -- a
    list, string or number was never meaningful there. It was type-
    checked nowhere: the store wrote `arguments or {}` verbatim, the
    envelope digest json-dumps any JSON value, and the dispatcher then
    built the async call with `dict(arguments)`, which RAISES on a list.
    That raise landed between a durable claim and its cleanup and wedged
    the delivery permanently (panel probe, 2026-08-01). Refuse it at the
    door, on both create paths, like every other wrong-typed field.
    """
    value = body.get(name)
    if value is None:
        return None
    if not isinstance(value, dict):
        raise Malformed(f"{name} must be an object, got "
                        f"{type(value).__name__}")
    return value


def _loopback_port_open(port, timeout=0.15):
    """Whether something is still accepting connections on a node port.

    Used only to arbitrate two runtimes claiming one stable saved-`.toe`
    identity.  It sends no bytes and is intentionally short; uncertainty
    fails toward preserving the existing live claim, never silently stealing
    it.  Call without HostApp.lock held.
    """
    try:
        with socket.create_connection(("127.0.0.1", int(port)),
                                      timeout=timeout):
            return True
    except (OSError, TypeError, ValueError):
        return False


def _json_safe(value):
    """The value itself if it survives strict JSON, else an honest
    substitute. A node result rides into the durable job file and back
    out through every /jobs response; an unserializable payload (or a
    bare NaN, which json.dumps emits but strict parsers and our own
    _send refuse) would otherwise blow up the RECORDING of a verdict the
    node really produced -- the verdict must survive even when its
    payload cannot."""
    try:
        # EXACTLY the store's dumps options (sort_keys included): a
        # value that only fails under sort_keys (mixed-type dict keys)
        # passed a laxer check here and then blew up the store write,
        # downgrading a real verdict to indeterminate.
        json.dumps(value, indent=1, sort_keys=True, allow_nan=False)
        return value
    except (TypeError, ValueError):
        return {"detail": "node result was not JSON-serializable; "
                          "sanitized to its repr",
                "repr": repr(value)[:512]}


def _bounded_result(value):
    """_json_safe, plus a size cap. A terminal node payload is COPIED
    into the delivery record (the node's own fetch-by-id window closes at
    24h, so the host record is what survives), and nothing else bounds
    what a node may return. An oversized payload is replaced by an HONEST
    note carrying its head -- the verdict is what matters, and it is
    recorded either way."""
    value = _json_safe(value)
    try:
        blob = json.dumps(value, indent=1, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError):     # cannot happen after _json_safe
        return value
    if len(blob.encode("utf-8")) <= MAX_RESULT_BYTES:
        return value
    return {"detail": f"node result exceeded {MAX_RESULT_BYTES} bytes and "
                      f"was truncated; the verdict is unaffected",
            "truncated": True,
            "bytes": len(blob.encode("utf-8")),
            "head": blob[:2048]}


def _is_unknown_job_answer(payload):
    """Whether an ok=True payload is the node saying it has no such job.

    Two independent signals, either of which is enough: the companion
    `jobs` listing the node ships with that answer and nothing else, or
    the message itself. Deliberately narrow -- see
    _NODE_UNKNOWN_JOB_RE."""
    if not isinstance(payload, dict):
        return False
    message = payload.get("error")
    if not isinstance(message, str):
        return False
    return ("jobs" in payload
            or bool(_NODE_UNKNOWN_JOB_RE.search(message)))


def _node_job_handle(payload):
    """(node_job_id, node_status) when a node payload is a JOB HANDLE,
    else None.

    A handle is what an async operation returns INSTEAD of a result: the
    node minted a job and this names it. Both fields are required and the
    id is shape-validated -- an unvalidated node-supplied id would be
    handed straight back to the node as a poll argument and written into
    a durable record. `job_id` is the starting tool's field, `id` the
    poll record's; one reader serves both legs.
    """
    if not isinstance(payload, dict):
        return None
    job_id = payload.get("job_id")
    if not isinstance(job_id, str):
        job_id = payload.get("id")
    status = payload.get("status")
    if (not isinstance(job_id, str) or not NODE_JOB_ID_RE.match(job_id)
            or status not in NODE_JOB_STATUSES):
        return None
    return job_id, status


class _HostLifecycleRuntime:
    """Bind LifecycleManager to HostApp's exact local node directory.

    The adapter accepts no executable, project path, environment, or argv
    from a remote request. It resolves the pinned node, calls only reviewed
    session-gated Envoy helpers, and closes the live directory-to-Popen race
    with an exact launch-unit reservation.
    """

    MAX_RESERVATIONS = 512
    SAVE_POLL_S = 0.2

    def __init__(self, app):
        self.app = app
        self._reservation_lock = threading.Lock()
        self._reservations = {}
        # Lifecycle operations execute concurrently for different nodes.  A
        # thread-local binds a save attempt to its durable delivery id without
        # changing the frozen runtime-adapter method signatures.
        self._operation_context = threading.local()

    @staticmethod
    def _runtime_snapshot(record):
        if not isinstance(record, dict) or not record.get("runtime_id"):
            return None
        snapshot = dict(record)
        snapshot["metadata"] = dict(record.get("metadata") or {})
        pid = snapshot["metadata"].get("process_id")
        if pid is not None:
            snapshot["process_id"] = pid
        return snapshot

    def current(self, node_id):
        with self.app.lock:
            record = self._runtime_snapshot(
                self.app.directory.lookup(node_id))
        if record is None:
            return None

        # A hard-killed TD process cannot unregister itself.  Treat only exact
        # process-death proof (or proven PID reuse) as permission to clear its
        # transient directory presence; unknown inspection remains fail-closed.
        lifecycle = self.app.lifecycle
        profile = (lifecycle.store.get_profile(node_id)
                   if lifecycle is not None else None)
        last = profile.get("last_runtime") if isinstance(profile, dict) else None
        process = last.get("process") if isinstance(last, dict) else None
        runtime_id = record.get("runtime_id")
        pid = record.get("process_id")
        if (not isinstance(last, dict) or last.get("runtime_id") != runtime_id
                or not isinstance(process, dict)
                or process.get("pid") != pid):
            return record
        try:
            status = lifecycle.inspector.inspect_status(pid)
        except Exception:
            return record
        definitively_gone = (
            isinstance(status, dict) and (
                status.get("status") == "dead"
                or (status.get("status") == "alive"
                    and not lifecycle_mod._process_same(
                        status.get("process"), process))))
        if not definitively_gone:
            return record

        # Compare-and-clear under the directory lock.  A new registration may
        # have arrived during process inspection and must never be erased.
        cleared = False
        with self.app.lock:
            current = self.app.directory.lookup(node_id)
            current_pid = ((current.get("metadata") or {}).get("process_id")
                           if isinstance(current, dict) else None)
            if (isinstance(current, dict)
                    and current.get("runtime_id") == runtime_id
                    and current_pid == pid):
                self.app.directory.clear_envoy_port(node_id)
                self.app._audit_best_effort(
                    "stale_runtime_reconciled",
                    {"node_id": node_id, "runtime_id": runtime_id})
                cleared = True
                current = None
            else:
                current = self._runtime_snapshot(current)
        if cleared:
            # Classify an unstable confirmed launch for crash-loop fencing.
            # This must remain outside app.lock because lifecycle persistence
            # and callbacks have their own synchronization.
            try:
                lifecycle.record_runtime_exit(node_id, runtime_id)
            except Exception:
                pass
            return None
        return current

    def begin_operation(self, operation_id):
        self._operation_context.operation_id = operation_id

    def end_operation(self):
        try:
            del self._operation_context.operation_id
        except AttributeError:
            pass

    def _endpoint(self, node_id, runtime_id):
        with self.app.lock:
            record = self.app.directory.lookup(node_id)
            if (record is None or record.get("host_id") != self.app.host_id
                    or record.get("runtime_id") != runtime_id
                    or record.get("enabled", True) is not True):
                return None
            port = record.get("envoy_port")
            if isinstance(port, bool) or not isinstance(port, int):
                return None
            return port if 1 <= port <= 65535 else None

    @staticmethod
    def _payload(outcome):
        if (not isinstance(outcome, dict)
                or outcome.get("ok") is not True
                or not isinstance(outcome.get("result"), dict)):
            return {"ok": False}
        return dict(outcome["result"])

    @staticmethod
    def _cancelled(cancel_event):
        return cancel_event is not None and cancel_event.is_set()

    def _call(self, node_id, runtime_id, operation, arguments, timeout_s,
              cancel_event):
        if self._cancelled(cancel_event):
            return {"ok": False, "code": "cancelled"}
        port = self._endpoint(node_id, runtime_id)
        if port is None:
            return {"ok": False, "code": "runtime_changed"}
        outcome = mcpclient.forward(
            port, operation, arguments,
            # The public lifecycle timeout is validated at >=100 ms, but by
            # the time lock/dirty/save phases consume it the remaining slice
            # can be smaller.  Re-expanding that slice to 100 ms here breaks
            # the manager's absolute deadline guarantee.
            timeout=max(0.001,
                        min(float(timeout_s), mcpclient.DEFAULT_TIMEOUT_S)),
            session="convoy-lifecycle")
        payload = self._payload(outcome)
        # A response from an endpoint whose ownership changed while the call
        # was in flight is not evidence about the addressed runtime.
        if self._endpoint(node_id, runtime_id) != port:
            return {"ok": False, "code": "runtime_changed"}
        return payload

    def dirty(self, node_id, runtime_id, timeout_s, cancel_event):
        return self._call(
            node_id, runtime_id, "convoy_lifecycle_state", {}, timeout_s,
            cancel_event)

    def save(self, node_id, runtime_id, timeout_s, cancel_event):
        deadline = time.monotonic() + float(timeout_s)
        job_id = None
        operation_id = getattr(self._operation_context, "operation_id", None)
        if not isinstance(operation_id, str) or not operation_id:
            return {"ok": False, "code": "internal_error"}
        # Stable within ONE lifecycle operation: retries reconcile to that
        # save, while a later restart of the same runtime receives a fresh key.
        material = (node_id + "\0" + runtime_id + "\0" + operation_id).encode(
            "utf-8", "strict")
        key = "convoy-lifecycle-save:" + hashlib.sha256(material).hexdigest()
        while time.monotonic() < deadline and job_id is None:
            if self._cancelled(cancel_event):
                return {"ok": False, "code": "cancelled"}
            remaining = deadline - time.monotonic()
            result = self._call(
                node_id, runtime_id, "save_project",
                {"idempotency_key": key}, remaining, cancel_event)
            candidate = result.get("job_id") if isinstance(result, dict) else None
            if (isinstance(candidate, str)
                    and NODE_JOB_ID_RE.fullmatch(candidate)):
                job_id = candidate
                break
            time.sleep(min(self.SAVE_POLL_S, max(0.0, remaining)))
        while time.monotonic() < deadline and job_id is not None:
            if self._cancelled(cancel_event):
                return {"ok": False, "code": "cancelled", "job_id": job_id,
                        "save_may_have_run": True}
            remaining = deadline - time.monotonic()
            result = self._call(
                node_id, runtime_id, POLL_OPERATION, {"job_id": job_id},
                remaining, cancel_event)
            status = result.get("status") if isinstance(result, dict) else None
            if status == "done":
                return {"ok": True, "job_id": job_id,
                        "result": result.get("result")}
            if status == "error":
                return {"ok": False, "code": "save_failed",
                        "job_id": job_id}
            time.sleep(min(self.SAVE_POLL_S, max(0.0, remaining)))
        result = {"ok": False, "code": "save_failed"}
        if job_id is not None:
            result.update({"job_id": job_id, "save_may_have_run": True})
        return result

    def quit(self, node_id, runtime_id, timeout_s, cancel_event, *, discard,
             expected_dirty_revision=None):
        return self._call(
            node_id, runtime_id, "convoy_lifecycle_quit",
            {"expected_dirty_revision": expected_dirty_revision,
             "discard": bool(discard)}, timeout_s, cancel_event)

    def reservation_for_node(self, node_id):
        """Return a detached live reservation used by /register preflight."""
        with self._reservation_lock:
            for value in self._reservations.values():
                if value.get("node_id") == node_id:
                    return dict(value)
        return None

    def restore_launch_reservations(self, reservations):
        """Atomically rebuild live fences from the validated durable ledger."""
        if not isinstance(reservations, list):
            return {"ok": False, "code": "invalid_reservations"}
        rebuilt = {}
        seen_nodes = set()
        for value in reservations:
            if not isinstance(value, dict) or set(value) != {
                    "node_id", "launch_unit_id", "operation_id",
                    "reservation_id"}:
                return {"ok": False, "code": "invalid_reservations"}
            node_id = value.get("node_id")
            launch_unit_id = value.get("launch_unit_id")
            operation_id = value.get("operation_id")
            reservation_id = value.get("reservation_id")
            if (not identity.is_valid_id(node_id)
                    or not isinstance(launch_unit_id, str)
                    or not re.fullmatch(r"[0-9a-f]{64}", launch_unit_id)
                    or not isinstance(operation_id, str)
                    or not re.fullmatch(
                        r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}",
                        operation_id)
                    or not isinstance(reservation_id, str)
                    or not reservation_id
                    or len(reservation_id.encode("utf-8")) > 256
                    or any(ord(char) < 32 or ord(char) == 127
                           for char in reservation_id)
                    or node_id in seen_nodes
                    or launch_unit_id in rebuilt):
                return {"ok": False, "code": "invalid_reservations"}
            seen_nodes.add(node_id)
            rebuilt[launch_unit_id] = {
                "node_id": node_id,
                "launch_unit_id": launch_unit_id,
                "operation_id": operation_id,
                "reservation_id": reservation_id,
            }
        with self._reservation_lock:
            if self._reservations and self._reservations != rebuilt:
                return {"ok": False, "code": "reservation_conflict"}
            self._reservations = rebuilt
        return {"ok": True, "restored": len(rebuilt)}

    def reserve_launch(self, node_id, launch_unit_id, operation_id,
                       timeout_s, cancel_event):
        if self._cancelled(cancel_event):
            return {"ok": False, "code": "cancelled"}
        with self.app.lock:
            record = self.app.directory.lookup(node_id)
            if (record is None or record.get("host_id") != self.app.host_id
                    or record.get("enabled", True) is not True
                    or record.get("runtime_id") is not None):
                return {"ok": False, "code": "occupied"}
            with self._reservation_lock:
                existing = self._reservations.get(launch_unit_id)
                if existing is not None:
                    if (existing.get("node_id") == node_id
                            and existing.get("operation_id") == operation_id):
                        return {"ok": True,
                                "reservation_id": existing["reservation_id"]}
                    return {"ok": False, "code": "busy"}
                if len(self._reservations) >= self.MAX_RESERVATIONS:
                    return {"ok": False, "code": "capacity"}
                reservation_id = "lr_" + secrets.token_urlsafe(24)
                self._reservations[launch_unit_id] = {
                    "node_id": node_id,
                    "launch_unit_id": launch_unit_id,
                    "operation_id": operation_id,
                    "reservation_id": reservation_id,
                }
                return {"ok": True, "reservation_id": reservation_id}

    def confirm_launch_reservation(self, node_id, launch_unit_id,
                                   operation_id, reservation_id, runtime_id):
        # LifecycleManager calls this while register_node holds app.lock.
        # This is validation only: consuming the fence before the attempt
        # ledger commits creates an unrecoverable store-failure gap. The
        # manager calls release_launch_reservation after its atomic commit.
        # The separate reservation lock keeps HostApp's coordination lock
        # deliberately non-reentrant.
        with self._reservation_lock:
            expected = self._reservations.get(launch_unit_id)
            if expected != {
                    "node_id": node_id,
                    "launch_unit_id": launch_unit_id,
                    "operation_id": operation_id,
                    "reservation_id": reservation_id}:
                return {"ok": False, "code": "reservation_mismatch"}
            record = self.app.directory.lookup(node_id)
            if (record is None or record.get("host_id") != self.app.host_id
                    or record.get("runtime_id") != runtime_id
                    or record.get("enabled", True) is not True):
                return {"ok": False, "code": "runtime_changed"}
            return {"ok": True}

    def release_launch_reservation(self, node_id, launch_unit_id,
                                   operation_id, reservation_id, outcome):
        with self._reservation_lock:
            expected = self._reservations.get(launch_unit_id)
            if (expected is not None
                    and expected.get("node_id") == node_id
                    and expected.get("operation_id") == operation_id
                    and expected.get("reservation_id") == reservation_id):
                del self._reservations[launch_unit_id]
        return {"ok": True}


class HostApp:
    """All state behind one lock: a host app is coordination, not
    throughput. Every handler acquires it around the whole request --
    EXCEPT dispatch_job, drain/drain_once, and poll_job/poll_once, which
    SELF-lock in phases so the forward I/O runs outside the lock. Never
    call those from inside `with app.lock:` -- threading.Lock is not
    reentrant, and the double-acquire deadlocks the handler thread."""

    def __init__(self, directory_path, now=None, forwarder=None, waker=None,
                 artifact_cache_path=None, realm_path=None,
                 realm_settle_delay_s=None,
                 owlette_client=None, owlette_command_policy=None,
                 lifecycle_manager=None, lifecycle_local_policy=None):
        self.data_dir = directory_path
        self._now = now or time.time
        self.started = self._now()
        if realm_settle_delay_s is None:
            # ADR-003 mandates a RANDOMIZED genesis listen window; the
            # fixed 8.0 s that shipped meant every isolated enable, on
            # every machine, waited identically, heard nothing, and
            # crowned its own realm -- manufacturing the split-realm
            # conflicts the field kept hitting. One draw per daemon
            # process; suites keep determinism by passing an explicit
            # value.
            realm_settle_delay_s = (
                realm_mod.DEFAULT_SETTLE_DELAY_S
                + (secrets.randbelow(1000) / 1000.0)
                * realm_mod.DEFAULT_SETTLE_JITTER_S)
        self.token = platform_mod.ensure_ipc_token(directory_path)
        # Safety authority is host-private and fail-closed.  Project/TDN
        # values are projections only; a corrupt or too-new policy file must
        # stop the daemon rather than silently restoring dangerous defaults.
        self.policy = policy_mod.PolicyStore(directory_path, now=now)
        self.artifacts = artifacts_mod.ArtifactStore(
            (artifact_cache_path if artifact_cache_path is not None
             else artifacts_mod.default_cache_root()),
            quota_mb=self.policy.artifact_quota_mb(),
            clock=(now or time.time))
        # One bound shared by loopback and LAN transfers.  The peer listener
        # also bounds connections, but local IPC uses ThreadingHTTPServer and
        # therefore needs an artifact-specific ceiling of its own.
        self.artifact_transfer_slots = threading.BoundedSemaphore(
            artifact_http.DEFAULT_MAX_TRANSFERS)
        self.db = hoststore.HostStore(directory_path, now=now)
        self.host_id = self.db.host_id()
        self.directory, self.quarantined = self.db.load_directory()
        self.realm = realm_mod.RealmStore(
            (realm_path if realm_path is not None
             else os.path.join(directory_path, realm_mod.REALM_FILE)),
            now=self._now, settle_delay_s=realm_settle_delay_s)
        self._hostop_context = threading.local()
        self.host_operations = hostops_mod.HostOperations(
            self._resolve_host_worktree,
            full_shell_policy=self._allow_host_shell,
            safe_state_dir=os.path.join(directory_path, "hostops"),
            audit_callback=self._audit_host_operation)
        # Optional public-API consumer.  Construct lazily so an installation
        # with no Owlette account pays no TLS/context startup cost and Convoy
        # never depends on Internet availability.  The injected seams keep
        # tests and future OS credential-store adapters deterministic.
        self._owlette_client = owlette_client
        self._owlette_lock = threading.Lock()
        self._owlette_command_policy = (
            owlette_command_policy or self._allow_owlette_commands)
        # The node SEAM: how the host executes a queued job against a
        # node's Envoy. Signature (port, operation, arguments) -> a dict
        # {"ok": bool, "result"/"error": ...} for an observed node result,
        # or None on a transport failure (-> indeterminate). The default
        # is the minimal MCP client (convoy_mcpclient.forward), which
        # handles the synchronous request/response tool call the dispatcher
        # needs today; tests inject their own. The robust transport
        # (streaming, reconnection) is the A-46 rework, and this seam is
        # exactly where it plugs in.
        #
        # ONE seam, TWO callers: the poll pass forwards through the same
        # function with operation POLL_OPERATION ("get_job_status"). A
        # test fake therefore answers BOTH legs and must branch on the
        # operation -- a fake that always returns a dispatch result will
        # answer polls with nonsense. Existing fakes are unaffected:
        # only an async operation ever reaches 'running', and only a
        # running job is ever polled.
        self.forwarder = forwarder or mcpclient.forward
        # Fire-and-forget LOOPBACK UDP.  It is safe inside the short phase-a
        # lock because it never waits for a response; the durable drain retries
        # a dropped datagram and node leases have a hard TTL.  Tests inject a
        # recorder through this seam.
        self.waker = waker or wake_mod.send
        # DEEP copy per instance: a shallow one shares the nested schema
        # and side_effects dicts with the module constant, so mutating
        # one in a test would silently change every other instance's
        # capability digest.
        self.operations = copy.deepcopy(PHASE1_OPERATIONS)
        # In-memory BY DESIGN: leases are cooperative and TTL-bounded, so
        # a host restart dropping them is safe (controllers re-acquire),
        # exactly like claim_scope's session-silence expiry. Durable
        # leases arrive with wake-leases (Phase 4) if at all.
        self.leases = controllers.LeaseRegistry()
        # PEERS (Phase 3 slice 2). Host-private admission records plus the
        # hand-editable, FAIL-CLOSED denylist. NOTHING BINDS OFF-BOX YET:
        # this is the memory and the decision; the listener that consults
        # it on a real connection is slice 3. The audit sink is passed as
        # a plain callable so the store never imports the store it audits
        # into -- and it is best-effort inside convoy_peers, so a failed
        # append can never change an admission.
        self.peers = peers_mod.PeerStore(
            directory_path, now=now,
            audit=lambda event, detail: self.db.audit("peers", event,
                                                      detail))
        # origin_host_id -> {controller_id: last_seen}. Which controllers
        # a peer has acted as, so revocation can drop their leases even
        # for a peer whose job records are unreadable. BOUNDED (see
        # _note_peer_controller): in-memory, advisory, and unioned with a
        # scan of the durable job records, which is what survives a
        # restart.
        self._peer_controllers = {}
        # Last peer-safe node directory received for each
        # (peer_host_id, convoy_id).  A temporary LAN dropout must not make
        # every node vanish from the operator's Status sequence: cached rows
        # remain visible and are marked offline/error by network_nodes().
        # This cache contains only the already-sanitized public projection --
        # never project_root, toe_path, Envoy's loopback port, or credentials.
        self._peer_node_cache = {}
        # Host-wide remote manifests, guarded by self.lock.  The key binds a
        # manifest to the exact trust lineage and the digest advertised by
        # the authenticated WSS hello.  HTTP-only peers are fetched on every
        # submission because they provide no separate current-digest signal.
        self._peer_manifest_cache = collections.OrderedDict()
        self.lock = threading.Lock()
        # One host-wide, fixed-size network query pool.  A new executor per
        # request lets simultaneous status clients multiply the 32-worker
        # bound; this pool makes the bound true for the whole process.  The
        # network_nodes single-flight layer below removes duplicate waves for
        # one namespace before work reaches this executor.
        self._network_query_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=NETWORK_QUERY_WORKERS,
            thread_name_prefix="convoy-network")
        # The injected test clock drives cache expiry exactly like every
        # other store's ``now`` seam; production (now=None) keeps a
        # monotonic clock so a wall-clock change can never extend a TTL.
        self._network_nodes_cache_clock = now or time.monotonic
        self._network_nodes_result_cache = {}
        self._network_nodes_flights = {}
        self._network_nodes_cache_generation = 0
        self._network_query_metrics = {
            "refreshes": 0,
            "cache_hits": 0,
            "coalesced": 0,
            "wait_timeouts": 0,
        }
        self._unreadable_operation_jobs = set()
        self._operation_job_scan_failed = False
        # Operation claims are normally in-memory coordination, but a job
        # already running at daemon restart is durable evidence of a writer.
        # Rebuild those exact claims before any listener can admit new work.
        self._restore_operation_claims_at_boot()
        self.lifecycle_runtime = _HostLifecycleRuntime(self)
        self.lifecycle_unavailable_reason = ""
        if lifecycle_manager is not None:
            self.lifecycle = lifecycle_manager
        else:
            try:
                lifecycle_store = lifecycle_mod.LaunchProfileStore(
                    os.path.join(directory_path, "lifecycle"), clock=now)
                self.lifecycle = lifecycle_mod.LifecycleManager(
                    lifecycle_store, self.lifecycle_runtime,
                    local_policy=(lifecycle_local_policy
                                  or (lambda node_id, policy: False)),
                    audit_callback=self._audit_lifecycle,
                    clock=now)
                # Rebuild every unresolved launch fence before any listener
                # can accept a manual or token-bearing registration. The
                # durable attempt ledger, not process memory, is authoritative
                # across supervisor restarts.
                self.lifecycle.restore_launch_reservations()
            except lifecycle_mod.LifecycleError as exc:
                # Unsupported OS/session or unreadable lifecycle state must
                # not take the whole Convoy host down. The two lifecycle
                # operations remain registered but fail closed by name.
                self.lifecycle = None
                self.lifecycle_unavailable_reason = exc.code
            except Exception:
                self.lifecycle = None
                self.lifecycle_unavailable_reason = "store_unavailable"
        # Exact-node restart recovery is independent of queue draining and
        # LAN exposure. main() starts this loop unconditionally: an operator
        # may disable both features while a durable restart commit still
        # obliges the host to restore the TouchDesigner process.
        self._lifecycle_recovery_thread = None
        self._lifecycle_recovery_stop = None
        self._lifecycle_recovery_done = set()
        # The autonomous dispatcher (start_drain_loop). Off by default:
        # dispatch stays a per-call affair until someone opts in.
        self._drain_thread = None
        self._drain_stop = None
        # Test-injectable downward, hard-capped by PASS_MAX_WORKERS in the
        # scheduler. A per-pass executor is short-lived and never owns more
        # futures than active worker slots.
        self.pass_max_workers = PASS_MAX_WORKERS
        # Dispatch bookkeeping, all guarded by self.lock:
        #   _in_flight     delivery_id -> ATTEMPT token for forwards in
        #                  progress in THIS process -- what separates a
        #                  live claim from a stranded one (drain_once's
        #                  reaper). Attempt-scoped, not job-scoped: a
        #                  finished attempt may only remove ITS OWN
        #                  marker, because a job released back to queued
        #                  can be re-claimed by a second attempt before
        #                  the first attempt's cleanup runs -- a
        #                  job-scoped discard there erased the live
        #                  marker and the reaper burned an undelivered
        #                  job to indeterminate (review probe,
        #                  2026-07-31).
        #   _drain_backoff delivery_id -> not-before timestamp; the drain
        #                  loop skips a refused/deferred job until it
        #                  passes. Manual /dispatch ignores it (an
        #                  explicit call is its own authority).
        #   _drain_noted   delivery_id -> last audited refusal event, so a
        #                  steady failing state audits ON TRANSITION, not
        #                  on every tick (unbounded audit growth).
        self._in_flight = {}
        self._flight_counter = 0
        # delivery_id -> threading.Event for a currently executing host-side
        # subprocess.  The map is guarded by self.lock and is deliberately
        # process-local: after host exit, HostStore's dispatching sweep records
        # the only honest outcome (indeterminate) rather than re-running work.
        self._hostop_cancel_events = {}
        self._drain_backoff = {}
        self._drain_noted = {}
        # The maps' effective size cap: max(DRAIN_MAP_FLOOR, 2x the live
        # queue), rescaled at the start of every drain pass so one pass
        # can hold an entry for EVERY queued job without self-eviction.
        self._drain_map_cap = DRAIN_MAP_FLOOR
        # Poll bookkeeping -- SEPARATE maps, not the drain ones. The
        # drain prune's notion of "live" is the QUEUED set, so a poll
        # entry parked in a drain map would be wiped by the next drain
        # pass (the sharpest trap in this slice). Same shapes:
        #   _polls_in_flight delivery_id -> ATTEMPT token, so a late
        #                    response cannot clear a newer poll's marker.
        #                    In-memory only and NOT a claim: a poll is a
        #                    READ, two concurrent polls are harmless, and
        #                    a host dying mid-poll must leave the job
        #                    running (the node still owns it).
        #   _poll_backoff    delivery_id -> not-before timestamp; paces
        #                    the node. Manual /poll ignores it.
        #   _poll_noted      delivery_id -> last audited (event, reason).
        #   _poll_unknown    delivery_id -> {first, count, node_job_id,
        #                    message}: the evidence behind RULE 2's
        #                    node_forgot_job terminalisation.
        self._polls_in_flight = {}
        self._poll_counter = 0
        self._poll_backoff = {}
        self._poll_noted = {}
        self._poll_unknown = {}
        # delivery_ids whose UNREACHABLE requeue write FAILED: the job is
        # still claimed on disk, its flight marker is deliberately kept
        # (the reaper must not resolve a never-delivered job), and the
        # drain pass retries the release until the disk heals.
        self._pending_release = {}
        # delivery_ids whose ASYNC HANDOFF write failed: the node IS
        # running the job and the host holds its handle, but the running
        # mirror could not be written. Parked exactly like a failed
        # requeue -- claim still on disk, flight marker deliberately
        # kept, drain pass retries the mirror until the disk heals --
        # because the alternative (falling into the indeterminate
        # downgrade) DESTROYS the handle and with it the only key to an
        # outcome the node can still answer for 24h.
        self._pending_handoff = {}
        # Last summary PER PASS KIND (drain, poll) -- the audit only
        # fires when a pass's shape changes, and the two passes must not
        # overwrite each other's baseline.
        self._last_pass_summary = {}
        self.drain_backoff_s = 30.0
        self.poll_backoff_s = 30.0
        # RETENTION. Terminal records older than this are reaped off the
        # drain loop so the jobs dir does not grow without bound (every
        # status/revocation scan reads all of it). Far larger than the
        # idempotency horizon -- a retry lands in seconds, this is 24h --
        # so reaping cannot re-open acknowledged work. Reaped at most once
        # per reap_interval_s, and only when the drain loop is running.
        self.job_retention_s = 24 * 3600.0
        self.reap_interval_s = 300.0
        self._last_reap = 0.0
        # NODE RETENTION (the stale-row sweep, field report 2026-08-04:
        # abandoned test projects lingered as Offline rows forever). Two
        # independent horizons, both guarded by the /nodes/forget idle
        # rules: a node whose .toe is PROVABLY deleted (absent while a
        # parent directory is still reachable -- an unmounted volume has
        # no reachable ancestry and is spared) is evicted once it has
        # been silent for node_dead_grace_s; any node silent for
        # node_retention_s is evicted regardless. Offline alone is NEVER
        # stale -- a closed TD stays listed and remotely launchable.
        self.node_retention_s = 30 * 24 * 3600.0
        self.node_dead_grace_s = 1800.0
        # TRANSIENT rows are the exception to "offline is never stale": a
        # row that never LIVED past node_transient_lived_s (a smoke run,
        # a one-shot registration, an install probe) is debris, not a
        # launchable node, and waiting out 30 days on it is what filled
        # fleet pages with dead Offline rows (field 2026-08-19). Cleared
        # after node_transient_retention_s of silence -- unless the user
        # explicitly granted it td_python, which proves deliberate setup.
        self.node_transient_lived_s = 900.0
        self.node_transient_retention_s = 3600.0
        # lifecycle_profile_unavailable transitions per node_id: the same
        # failure used to be audited on EVERY 30s heartbeat (~5.7k
        # lines/day/node, field 2026-08-19). Process-local by design --
        # one line per node per daemon run is the desired cadence.
        self._lifecycle_profile_reasons = {}
        # Last time a PASSIVE read path reconciled operation claims. Mutation
        # and dispatch paths still reconcile eagerly; this only bounds the
        # per-claim file I/O the read paths would otherwise do on every call.
        self._last_claim_read_reconcile = 0.0
        # HOST IDENTITY (Phase 3 slice 1). The Ed25519 keypair that signs
        # envelopes and, from slice 3, backs the TLS certificate.
        #
        # TWO FAILURES, TWO OPPOSITE ANSWERS, and getting them the wrong
        # way round would be a security bug either way:
        #   - `cryptography` ABSENT -> DEGRADE. An older install has no
        #     such package; the loopback host app owes it nothing, and a
        #     daemon that refuses to start is a worse failure than one
        #     that cannot yet speak to peers. status() and /identity say
        #     so by name.
        #   - key file CORRUPT/UNREADABLE -> REFUSE TO START, by letting
        #     HostKeyError propagate out of __init__. Minting a fresh
        #     identity instead would orphan every peer relationship on
        #     every peer (convoy_hostkeys' module docstring).
        self.hostkeys = None
        self.identity_detail = ""
        try:
            self.hostkeys = hostkeys.load_or_create(directory_path)
        except hostkeys.CryptographyMissing as e:
            self.identity_detail = e.detail
        self.peer_pool = (peerclient.PeerConnectionPool(self.hostkeys)
                          if self.hostkeys is not None else None)
        # Persistent WSS control plane.  It exists only while the LAN
        # listener is exposed; HTTPS remains the artifact plane and a
        # compatibility fallback for peers that have not established WSS.
        self.session_manager = None
        self._session_manager_lock = threading.Lock()
        self._session_hello_signature = None
        # THE LAN LISTENER (Phase 3 slice 3). None until main() binds it,
        # and it binds ONLY when lan.json enables it (convoy_lan;
        # `absent = NO LAN SOCKET EVER`). These describe its state for
        # /lan/status and for the shutdown order (the LAN listener stops
        # FIRST). An embedded HostApp -- every test that does not call
        # serve_lan -- leaves them None and reports "not bound".
        self.lan_server = None
        self.lan_thread = None
        self.lan_address = None
        self.lan_port = None
        self.discovery_service = None
        # Why the LAN listener is not up, when it is not: 'disabled' (no
        # lan.json), a convoy_lan reason, a bind refusal, or 'no_identity'.
        # Surfaced on /lan/status so "off" is never indistinguishable from
        # "broken".
        self.lan_reason = "disabled"
        # The LAN surface follows durable enabled-node membership.  Socket
        # start/stop never runs under the request lock: registrations merely
        # set this Event and the host lifecycle thread reconciles state.
        self._lan_refresh = threading.Event()
        self._lan_lifecycle_stop = threading.Event()
        self._lan_lifecycle_thread = None
        self._lan_retry_s = 5.0
        # How /shutdown stops the server. Set by main() to EXACTLY the
        # callable the SIGTERM handler uses -- one shutdown path, not
        # two. None until then, so an embedded HostApp (every test that
        # builds one without serve()) refuses the route instead of
        # pretending it stopped something.
        self._shutdown_hook = None
        self._initialize_realm_from_directory()
        self.db.audit("hostapp", "started", {"host_id": self.host_id})
        self._auditIdentityAtBoot()

    @staticmethod
    def _allow_owlette_commands():
        value = os.environ.get(OWLETTE_COMMAND_OPT_IN_ENV, "")
        return value.strip().lower() in ("1", "true", "yes", "on")

    def _allow_owlette_machine_commands(self):
        """Durable opt-in for machine-affecting Owlette commands.

        reboot/shutdown/restart-process/kill-process escalate past every other
        Owlette action, so they must not ride the single benign env flag.  Like
        Full Shell (``_allow_host_shell`` -> ``self.policy``), they require a
        DURABLE PolicyStore approval.  PolicyStore does not yet expose this
        field, so this reads it defensively (a dedicated reader if one is added,
        else the snapshot) and DEFAULT-DENIES until the durable opt-in and its
        challenge/confirm flow land in convoy_policy.py (cross-file follow-up).
        """
        reader = getattr(self.policy, "allow_owlette_machine_commands", None)
        if callable(reader):
            try:
                return reader() is True
            except Exception:
                return False
        try:
            snapshot = self.policy.snapshot()
        except Exception:
            return False
        return bool(isinstance(snapshot, dict)
                    and snapshot.get("allow_owlette_machine_commands") is True)

    def _get_owlette_client(self):
        client = self._owlette_client
        if client is not None:
            return client
        with self._owlette_lock:
            if self._owlette_client is None:
                self._owlette_client = owlette_mod.client_from_env()
            return self._owlette_client

    @staticmethod
    def _owlette_error_response(exc):
        payload = exc.as_dict()
        status = getattr(exc, "status", None)
        if isinstance(exc, owlette_mod.OwletteValidationError):
            code = 400
        elif isinstance(exc, (owlette_mod.OwletteConfigError,
                              owlette_mod.OwletteCredentialUnavailable)):
            code = 503
        elif isinstance(exc, owlette_mod.OwletteApiError) \
                and isinstance(status, int) and 400 <= status <= 599:
            code = status
        else:
            code = 502
        payload.update({"integration": "owlette-public-api",
                        "wakes_touchdesigner": False})
        return code, payload

    def owlette_action(self, body):
        """Consume one bounded public Owlette API action from loopback.

        This is deliberately not a Convoy transport and is never exposed by
        the peer HTTPS server.  Inventory/status are read-only.  Command
        submission additionally requires a host-local environment opt-in;
        the generic mcp_tool_call command remains forbidden so Owlette cannot
        become an undocumented Convoy or unrestricted shell tunnel.
        """
        if not isinstance(body, dict):
            return 400, {"ok": False, "reason": "malformed"}
        action = body.get("action", "capabilities")
        if not isinstance(action, str) or not action:
            return 400, {"ok": False, "reason": "malformed",
                         "detail": "action must be non-empty text"}
        read_actions = {
            "capabilities", "list_sites", "get_site", "list_machines",
            "get_machine", "command_status",
        }
        if action not in read_actions | {"submit_command"}:
            return 400, {"ok": False, "reason": "unsupported_action",
                         "detail": "the requested Owlette action is not published"}
        try:
            client = self._get_owlette_client()
            if action == "capabilities":
                result = {
                    "capabilities": owlette_mod.public_capabilities(),
                    "base_url": client.config.base_url,
                    "default_site_id": client.config.default_site_id,
                    "credential_configured": bool(
                        os.environ.get(owlette_mod.ENV_API_KEY)
                        or os.environ.get(owlette_mod.ENV_API_KEY_SECRET)),
                }
            elif action == "list_sites":
                result = {"sites": client.list_sites()}
            elif action == "get_site":
                result = {"site": client.get_site(body.get("site_id"))}
            elif action == "list_machines":
                result = {"machines": client.list_machines(
                    body.get("site_id"))}
            elif action == "get_machine":
                result = {"machine": client.get_machine(
                    body.get("site_id"), body.get("machine_id"))}
            elif action == "command_status":
                result = {"command": client.get_command_status(
                    body.get("site_id"), body.get("machine_id"),
                    body.get("command_id"))}
            else:
                command_type = body.get("command_type")
                if not bool(self._owlette_command_policy()):
                    return 403, {
                        "ok": False,
                        "reason": "owlette_commands_not_approved",
                        "detail": "set %s locally on the Convoy host to "
                                  "enable reviewed Owlette commands" %
                                  OWLETTE_COMMAND_OPT_IN_ENV,
                        "wakes_touchdesigner": False,
                    }
                if command_type == OWLETTE_TUNNEL_COMMAND:
                    return 403, {
                        "ok": False,
                        "reason": "owlette_tunnel_forbidden",
                        "detail": "mcp_tool_call is not an approved Convoy/Owlette bridge",
                        "wakes_touchdesigner": False,
                    }
                if (command_type in OWLETTE_MACHINE_AFFECTING_COMMANDS
                        and not self._allow_owlette_machine_commands()):
                    # A-35: reboot/shutdown/process-kill can take a show
                    # machine down with no unsaved-work check, so the benign
                    # command opt-in above is not enough -- they need the
                    # durable policy approval and default-deny until it exists.
                    return 403, {
                        "ok": False,
                        "reason": "owlette_machine_command_not_approved",
                        "detail": "reboot/shutdown/process-kill Owlette "
                                  "commands require a durable Convoy policy "
                                  "opt-in that is not enabled",
                        "command_type": str(command_type or "")[:128],
                        "wakes_touchdesigner": False,
                    }
                result = {"command": client.submit_command(
                    body.get("site_id"), body.get("machine_id"),
                    command_type,
                    idempotency_key=body.get("idempotency_key"),
                    params=body.get("params"),
                    timeout_seconds=body.get("timeout_seconds", 60))}
                self.db.audit("owlette", "command_submitted", {
                    "site_id": str(body.get("site_id") or "")[:128],
                    "machine_id": str(body.get("machine_id") or "")[:128],
                    "command_type": str(command_type or "")[:128],
                })
        except owlette_mod.OwletteError as exc:
            return self._owlette_error_response(exc)
        except Exception as exc:
            # Third-party credential readers/transports can raise arbitrary
            # exceptions.  Do not reflect their text: it may contain account
            # or secret material.
            return 502, {
                "ok": False, "reason": "owlette_integration_error",
                "detail": type(exc).__name__,
                "integration": "owlette-public-api",
                "wakes_touchdesigner": False,
            }
        return 200, {
            "ok": True, "action": action,
            "integration": "owlette-public-api",
            "openapi_version": owlette_mod.OPENAPI_VERSION,
            "wakes_touchdesigner": False,
            **result,
        }

    def _auditIdentityAtBoot(self):
        """Record THIS boot's fingerprint, and shout if it moved.

        The fingerprint is audited on EVERY boot, not only on the
        interesting origins. `origin == 'loaded'` means "a key file was
        present" -- it does NOT mean "the same key as last time", so
        auditing only the other origins left the single most
        audit-worthy event in the Phase 3 trust model completely
        unrecorded: restore the wrong backup, swap in another machine's
        identity.key, or half-complete a rotation, and the host came up
        with a different fingerprint, an unchanged host_id, and not one
        line in the trail.
        """
        if self.hostkeys is None:
            self.db.audit("hostkeys", "identity_unavailable",
                          {"reason": "cryptography_missing",
                           "detail": self.identity_detail})
            return
        current = self.hostkeys.fingerprint
        previous = self.db.last_identity_fingerprint()
        if previous and previous != current:
            self.db.audit("hostkeys", "identity_changed",
                          {"previous_fingerprint": previous,
                           "fingerprint": current,
                           "origin": self.hostkeys.origin,
                           "detail": "this host's identity is NOT the one "
                                     "recorded at the previous start; every "
                                     "peer that pinned it must re-admit"})
        self.db.record_identity_fingerprint(current)
        self.db.audit("hostkeys", "identity_" + self.hostkeys.origin,
                      {"fingerprint": current,
                       "certificate": self.hostkeys.certificate_pem is not None,
                       "certificate_reason": self.hostkeys.cert_reason})

    # -- automatic LAN realm (ADR-003) ---------------------------------

    @staticmethod
    def _realm_public(snapshot):
        """Bounded, secret-free realm status for loopback projections."""
        if not isinstance(snapshot, dict):
            return {"state": "unbound", "convoy_id": None,
                    "conflict_ids": [], "generation": None}
        return {
            "state": snapshot.get("state"),
            "convoy_id": snapshot.get("convoy_id"),
            "conflict_ids": list(snapshot.get("conflict_ids") or ())[:32],
            "generation": snapshot.get("generation"),
        }

    def _realm_projection_locked(self):
        return self._realm_public(self.realm.snapshot())

    def _invalidate_network_nodes_cache_locked(self):
        """Invalidate directory projections while ``self.lock`` is held.

        The generation also fences an already-running refresh: that caller
        may finish from its valid snapshot, but it cannot republish the old
        projection into the cache after membership changed underneath it.
        """
        self._network_nodes_cache_generation += 1
        self._network_nodes_result_cache.clear()

    def _apply_realm_observations_locked(self, *, candidate_ids=(),
                                         established_ids=(), source=None):
        """Reconcile signed/local observations and persist candidate rebases.

        CALLED WITH ``self.lock`` held. RealmStore writes its own private
        record before publishing state; HostStore then atomically projects an
        authoritative ID/state across every provisional local node. An
        established node is never selected by that projection.

        ``source`` names WHO supplied the observation (announcement
        sender, registering node, startup/reset derivation) and rides
        the audit record: the 2026-08-12 conflict was unattributable
        because only the RESULT was ever logged.
        """
        candidate_ids = tuple(candidate_ids or ())
        established_ids = tuple(established_ids or ())
        before = self.realm.snapshot()
        if before is None and not established_ids and candidate_ids:
            self.realm.begin_candidate(min(candidate_ids))
        after = self.realm.reconcile(
            candidate_ids=candidate_ids,
            established_ids=established_ids)
        if after is not None and after.get("state") in (
                realm_mod.CANDIDATE, realm_mod.ESTABLISHED):
            changed_nodes = self.db.rebind_candidates(
                self.directory, after["convoy_id"],
                binding_state=after["state"])
            if after["state"] == realm_mod.ESTABLISHED:
                self.db.ensure_convoy_psk(after["convoy_id"])
        else:
            changed_nodes = []

        changed = before != after or bool(changed_nodes)
        if changed:
            self._invalidate_network_nodes_cache_locked()
        if changed and after is not None:
            event = "realm_" + str(after.get("state") or "changed")
            payload = {
                "convoy_id": after.get("convoy_id"),
                "conflict_ids": list(after.get("conflict_ids") or ())[:16],
                "generation": after.get("generation"),
                "local_nodes_rebound": len(changed_nodes),
            }
            if source:
                payload["source"] = source
            self._audit_best_effort(event, payload)
        return after, changed

    def _initialize_realm_from_directory(self):
        """Recover one host realm from durable node bindings at startup."""
        with self.lock:
            enabled = [record for record in self.directory.nodes()
                       if bool(record.get("enabled", True))]
            if not enabled:
                return
            candidate_ids = {
                record.get("convoy_id") for record in enabled
                if record.get("binding_state") == realm_mod.CANDIDATE
            }
            established_ids = {
                record.get("convoy_id") for record in enabled
                if record.get("binding_state", realm_mod.ESTABLISHED)
                == realm_mod.ESTABLISHED
            }
            self._apply_realm_observations_locked(
                candidate_ids=candidate_ids,
                established_ids=established_ids,
                source={"via": "startup"})

    def reset_realm(self, body):
        """Advanced LOCAL recovery: clear a realm binding/conflict and re-run
        genesis. Loopback-only (routed through _post_locked).

        A split-realm CONFLICT never self-clears -- that is deliberate. This
        is the plan's required local reset/rejoin action (section 9.1). It
        clears the durable realm record, then re-derives a clean
        candidate/established binding from the still-registered local nodes so
        the host escapes the conflict without a restart. The announcements
        that caused the conflict are now gated by the killswitch/denylist, so
        an operator blocks the offending sender (or engages the killswitch)
        first, then resets.

        ``adopt_convoy_id`` in the body is the OTHER direction -- Join
        Other Realm -- and takes a different, reset-free path: see
        ``_adopt_realm_locked``. The KEY's presence selects the join
        (an explicit null is a 400, never a silent bare reset).
        """
        if isinstance(body, dict) and "adopt_convoy_id" in body:
            return self._adopt_realm_locked(body)
        before = self.realm.snapshot()
        self.realm.reset()
        enabled = [record for record in self.directory.nodes()
                   if bool(record.get("enabled", True))]
        candidate_ids = {
            record.get("convoy_id") for record in enabled
            if record.get("binding_state") == realm_mod.CANDIDATE
            and record.get("convoy_id")}
        established_ids = {
            record.get("convoy_id") for record in enabled
            if record.get("binding_state", realm_mod.ESTABLISHED)
            == realm_mod.ESTABLISHED and record.get("convoy_id")}
        if candidate_ids or established_ids:
            self._apply_realm_observations_locked(
                candidate_ids=candidate_ids,
                established_ids=established_ids,
                source={"via": "reset"})
        else:
            self._invalidate_network_nodes_cache_locked()
        after = self.realm.snapshot()
        self._audit_best_effort("realm_reset", {
            "previous_state": (before or {}).get("state"),
            "previous_convoy_id": (before or {}).get("convoy_id"),
            "new_state": (after or {}).get("state"),
        })
        return 200, {"ok": True,
                     "previous": self._realm_public(before),
                     "realm": self._realm_public(after)}

    def _adopt_realm_locked(self, body):
        """Join Other Realm: operator-confirmed adoption of a foreign
        established realm. CALLED WITH ``self.lock`` held (via the
        loopback reset route).

        A bare reset re-derives from this host's own durable rows, so a
        machine whose own realm is the WRONG one (field, 2026-08-12: an
        isolated self-crowned MacBook) could only ever re-crown itself;
        there was no way onto the house realm short of deleting
        realm.json AND host.json by hand.

        The sequence never passes through an unbound or reset instant
        (both reviews, 2026-08-12: the reset-first draft let a single
        failed write strand the host uncommitted -- the exact state an
        un-admitted announcement is allowed to claim):

        1. GATE: the adopted id must be evidenced -- present in this
           host's latched conflict ids, its own committed id, or a live
           discovery candidate's established realms. A refusal mutates
           nothing. (The id is operator-confirmed but attacker-supplied;
           this refuses ids this machine has never seen. A LATCHED id
           with no live announcer IS accepted -- the machine holds
           first-hand evidence it exists; the UI additionally requires
           a live announcer before OFFERING a join, so the two layers
           together stop phantom adoptions.)
        2. Rows move to candidate AT the adopted id, disk first. A
           failure here leaves the previous realm committed; the join is
           simply retryable.
        3. ``RealmStore.adopt`` commits the adopted realm atomically
           (write-before-publish).
        4. The standard observation apply re-establishes the projection
           (rebind, PSK, cache, audit) on the now-committed realm.
        """
        try:
            adopt = identity.normalize_convoy_id(body.get("adopt_convoy_id"))
        except identity.IdentityError as exc:
            return 400, {"ok": False, "reason": "bad_request",
                         "detail": "adopt_convoy_id: %s" % (exc,)}
        before = self.realm.snapshot()
        known = set((before or {}).get("conflict_ids") or ())
        if (before or {}).get("convoy_id"):
            known.add(before["convoy_id"])
        if self.discovery_service is not None:
            for cand in (self.discovery_service.status().get("candidates")
                         or []):
                if not isinstance(cand, dict):
                    continue
                states = cand.get("realm_states")
                if not isinstance(states, dict):
                    continue
                for realm_id, state in states.items():
                    if str(state) == realm_mod.ESTABLISHED:
                        known.add(str(realm_id))
        if adopt not in known:
            self._audit_best_effort("realm_adopt_refused", {
                "adopted_convoy_id": adopt,
                "reason": "adopt_unknown_realm",
            })
            return 409, {"ok": False, "reason": "adopt_unknown_realm",
                         "detail": "that realm is not in this machine's "
                                   "conflict record and no live LAN "
                                   "sender is announcing it; run the "
                                   "join again while the other machine "
                                   "is on and announcing"}
        abandoned_ids = sorted({
            record["convoy_id"] for record in self.directory.nodes()
            if record["binding_state"] == realm_mod.ESTABLISHED
            and record["convoy_id"] != adopt})
        demoted = self.db.abandon_established(self.directory, adopt)
        self.realm.adopt(adopt)
        self._apply_realm_observations_locked(
            candidate_ids=(), established_ids=(adopt,),
            source={"via": "operator_adopt"})
        # The apply invalidates only when rows or realm snapshot changed;
        # adopting the host's OWN id out of a conflict changes neither
        # yet still changes what the node projection should report.
        self._invalidate_network_nodes_cache_locked()
        after = self.realm.snapshot()
        denylisted_senders = []
        senders = body.get("senders")
        audit_senders = []
        if isinstance(senders, list):
            for sender in senders[:8]:
                if not isinstance(sender, dict):
                    continue
                entry = {key: str(sender.get(key) or "")[:128]
                         for key in ("host_id", "fingerprint", "address")}
                if not (entry["host_id"] or entry["fingerprint"]):
                    continue
                audit_senders.append(entry)
                try:
                    blocked, _detail = self.peers.denylist.blocks(
                        entry["host_id"], entry["fingerprint"])
                except Exception:
                    blocked = False
                if blocked:
                    denylisted_senders.append(entry)
        self._audit_best_effort("realm_adopted", {
            "previous_state": (before or {}).get("state"),
            "previous_convoy_id": (before or {}).get("convoy_id"),
            "adopted_convoy_id": adopt,
            "abandoned_convoy_ids": abandoned_ids[:16],
            "demoted_node_ids": [record["node_id"]
                                 for record in demoted][:16],
            "senders": audit_senders,
            "denylisted_senders": denylisted_senders,
        })
        return 200, {"ok": True,
                     "previous": self._realm_public(before),
                     "realm": self._realm_public(after),
                     "abandoned_convoy_ids": abandoned_ids[:16],
                     "denylisted_senders": denylisted_senders}

    def _accept_registration_realm_locked(self, convoy_id, binding_state,
                                          current=None, source=None):
        """Return the host-authoritative (id, state), or a conflict refusal.

        CHECK, THEN APPLY (field incidents 2026-08-06 and 2026-08-12):
        the old order fed the incoming observation into the DURABLE
        realm store first and only then noticed the resulting CONFLICT
        -- one register carrying a foreign established realm (a repo
        cloned from another LAN, or the malformed literal "cv")
        permanently wedged the receiving daemon before its 409 was even
        computed. A foreign ESTABLISHED claim against a committed realm
        is now refused WITHOUT touching realm state, and audited with
        the claim and its source; entering CONFLICT is reserved for
        evidence channels that carry authority (an admitted peer's
        announcement, or this host's own durable rows).
        """
        snapshot = self.realm.snapshot()
        # No convoy_id-truthiness term: a legacy CONFLICT record can carry
        # convoy_id=None, and skipping the guard there let a foreign
        # established register mutate the very state the docstring says
        # is never touched (panel finding).
        if (binding_state == realm_mod.ESTABLISHED
                and snapshot is not None
                and snapshot.get("state") in (realm_mod.ESTABLISHED,
                                              realm_mod.CONFLICT)
                and convoy_id != snapshot.get("convoy_id")):
            self._audit_best_effort("realm_foreign_register_refused", {
                "convoy_id": convoy_id,
                "binding_state": binding_state,
                "local_convoy_id": snapshot.get("convoy_id"),
                "source": dict(source or {}, via="register"),
            })
            return None, (
                409, "local_realm_conflict",
                "this project is bound to a different established Convoy "
                "than this machine's realm; it was not joined "
                "automatically (the local realm was left untouched)")
        candidates = ((convoy_id,) if binding_state == realm_mod.CANDIDATE
                      else ())
        established = ((convoy_id,)
                       if binding_state == realm_mod.ESTABLISHED else ())
        snapshot, _changed = self._apply_realm_observations_locked(
            candidate_ids=candidates, established_ids=established,
            source=dict(source or {}, via="register"))
        if snapshot is None:
            return None, (503, "realm_unbound",
                          "the automatic Convoy realm is not ready")
        if snapshot.get("state") == realm_mod.CONFLICT:
            preserved = snapshot.get("convoy_id")
            if (current is not None and preserved
                    and current.get("convoy_id") == preserved
                    and current.get("binding_state") ==
                    realm_mod.ESTABLISHED
                    and convoy_id == preserved
                    and binding_state == realm_mod.ESTABLISHED):
                # Existing authenticated work inside the preserved realm
                # remains operational while the operator resolves the split.
                return (preserved, realm_mod.ESTABLISHED), None
            return None, (
                409, "local_realm_conflict",
                "multiple established Convoys were found on this LAN; "
                "this project was not joined automatically")
        return (snapshot["convoy_id"], snapshot["state"]), None

    def _realm_operation_refusal(self, convoy_id):
        """None when this realm may execute, else (reason, detail)."""
        snapshot = self.realm.snapshot()
        if snapshot is None:
            return "realm_unbound", "the automatic Convoy realm is unbound"
        state = snapshot.get("state")
        authoritative = snapshot.get("convoy_id")
        if (state == realm_mod.ESTABLISHED
                and authoritative == convoy_id):
            return None
        if (state == realm_mod.CONFLICT and authoritative
                and authoritative == convoy_id):
            return None
        if state == realm_mod.CANDIDATE:
            return ("realm_not_established",
                    "automatic Convoy genesis is still settling")
        if state == realm_mod.CONFLICT:
            return ("realm_conflict",
                    "multiple established Convoys were found on this LAN")
        return ("realm_namespace_mismatch",
                "the target node is not bound to this host's Convoy realm")

    def active_realm_states(self):
        """One atomic discovery projection: {realm_id: wire state}."""
        with self.lock:
            if not any(bool(record.get("enabled", True))
                       for record in self.directory.nodes()):
                return {}
            snapshot = self.realm.snapshot()
            if not snapshot or not snapshot.get("convoy_id"):
                return {}
            state = snapshot.get("state")
            if state in (realm_mod.CANDIDATE, realm_mod.ESTABLISHED):
                return {snapshot["convoy_id"]: state}
            if state == realm_mod.CONFLICT:
                # Keep the pre-existing realm alive, but never advertise the
                # conflicting realm(s) as if this host had merged them.
                return {snapshot["convoy_id"]: realm_mod.ESTABLISHED}
            return {}

    def _observe_realm_announcement(self, announcement):
        """Discovery callback; announcement is already signature-verified.

        TRUST-SCOPED (field incident 2026-08-12): a signature only proves
        the datagram matches the sender's own self-signed cert -- TOFU
        means ANY host on the subnet has one. An UN-ADMITTED sender may
        inform a host that is still unbound or a candidate (genesis and
        adoption need to hear the LAN before any admission exists), but
        it must NEVER move a host whose realm is already committed: one
        stranger's broadcast latched this machine's daemon into a durable
        CONFLICT that refused every registration, with nothing recording
        who sent it. Committed-state transitions now require the sender
        to be an ADMITTED peer (the same host-level pin admission uses);
        an un-admitted claim of a foreign established realm is recorded
        as an audited ADVISORY with full sender provenance -- visible,
        denylistable, and powerless.
        """
        states = announcement.get("realm_states")
        if not isinstance(states, dict):
            return
        candidates = [realm_id for realm_id, state in states.items()
                      if state == realm_mod.CANDIDATE]
        established = [realm_id for realm_id, state in states.items()
                       if state == realm_mod.ESTABLISHED]
        endpoint = announcement.get("endpoint")
        sender = {
            "host_id": str(announcement.get("host_id") or ""),
            "fingerprint": str(announcement.get("fingerprint") or ""),
            # The parsed wire format carries endpoint={"address","port"},
            # never a top-level address -- reading the wrong key recorded
            # an EMPTY address on every production advisory (panel
            # finding, reproduced).
            "address": ("%s:%s" % (endpoint.get("address"),
                                   endpoint.get("port"))
                        if isinstance(endpoint, dict) else ""),
        }
        advisory = None
        with self.lock:
            snapshot = self.realm.snapshot()
            committed = bool(snapshot) and snapshot.get("state") in (
                realm_mod.ESTABLISHED, realm_mod.CONFLICT)
            if committed and not self._realm_mover_locked(
                    sender, snapshot):
                foreign = [realm_id for realm_id in established
                           if realm_id != snapshot.get("convoy_id")]
                if foreign:
                    advisory = self._note_foreign_realm_locked(
                        sender, states)
                changed = False
            else:
                _snapshot, changed = self._apply_realm_observations_locked(
                    candidate_ids=candidates, established_ids=established,
                    source=dict(sender, via="announcement"))
        if advisory:
            # OUTSIDE the lock: audit I/O must not ride the discovery
            # thread's hold on the one lock /register and /jobs share.
            self._audit_best_effort("realm_foreign_advisory", advisory)
        if changed:
            self.request_lan_refresh()

    def _realm_mover_locked(self, sender, snapshot):
        """May this announcement sender MOVE a committed realm?

        The bar is OPERATOR-GRADE membership in the realm being moved,
        not mere admission: `allowed` alone passes an observe-only peer
        (a peer the operator deliberately stripped of every mutation),
        and TOFU admission is self-service -- any LAN host that echoes
        this host's own broadcast convoy_id gets auto-admitted, so
        'admitted' by itself is two datagrams of work for a stranger
        (both reproduced by the review panel). A genuine split between
        two meshes an operator deliberately joined still latches; a
        TOFU-auto-admitted neighbour is an advisory like any stranger.
        """
        try:
            block = self.peers.authorize_peer(
                sender["host_id"], sender["fingerprint"],
                convoy_id=snapshot.get("convoy_id"))
            if not (block.allowed and block.may_mutate):
                return False
            record = self.peers.get(sender["host_id"]) or {}
            return str(record.get("admitted_via") or "") not in (
                "", "lan_tofu")
        except Exception:
            return False

    # Foreign-realm advisories: bounded, deduped per sender WITH a TTL,
    # rate-limited globally, and AUDITED -- the 2026-08-12 conflict was
    # unattributable because no ingest path recorded who sent the
    # observation. Both dedupe-key halves are attacker-chosen, so the
    # dedupe alone cannot bound the audit rate (rotating identities);
    # the global budget is what protects audit.jsonl from being rolled
    # over (~3 min at datagram rate, measured by the review panel).
    _MAX_FOREIGN_REALM_ADVISORIES = 16
    _FOREIGN_ADVISORY_TTL_S = 3600.0
    _FOREIGN_ADVISORY_BUDGET = 6           # audits per budget window
    _FOREIGN_ADVISORY_WINDOW_S = 3600.0

    def _note_foreign_realm_locked(self, sender, states):
        """Return the advisory payload to audit, or None. Lock held.

        Decides only -- the caller writes the audit OUTSIDE the lock.
        """
        try:
            now = self._now()
            key = (sender.get("host_id"),
                   tuple(sorted(str(k) for k in states)))
            seen = getattr(self, "_foreign_realm_seen", None)
            if seen is None:
                seen = self._foreign_realm_seen = collections.OrderedDict()
            last = seen.get(key)
            if last is not None and (now - last) < \
                    self._FOREIGN_ADVISORY_TTL_S:
                return None
            while len(seen) >= self._MAX_FOREIGN_REALM_ADVISORIES:
                seen.popitem(last=False)
            seen[key] = now
            budget = getattr(self, "_foreign_advisory_budget", None)
            if budget is None:
                budget = self._foreign_advisory_budget = collections.deque()
            while budget and (now - budget[0]) > \
                    self._FOREIGN_ADVISORY_WINDOW_S:
                budget.popleft()
            if len(budget) >= self._FOREIGN_ADVISORY_BUDGET:
                self._foreign_advisories_suppressed = getattr(
                    self, "_foreign_advisories_suppressed", 0) + 1
                return None
            budget.append(now)
            suppressed = getattr(self, "_foreign_advisories_suppressed", 0)
            self._foreign_advisories_suppressed = 0
            payload = {
                "sender": dict(sender),
                "realm_states": {str(k): str(v)
                                 for k, v in list(states.items())[:16]},
                "detail": "un-admitted LAN host advertises a foreign "
                          "established Convoy; ignored (realm is "
                          "committed). Use Resolve Realm Conflict / the "
                          "denylist if it should be silenced.",
            }
            if suppressed:
                payload["suppressed_since_last"] = suppressed
            return payload
        except Exception:
            return None

    def _tick_realm(self):
        with self.lock:
            _snapshot, changed = self._apply_realm_observations_locked(
                source={"via": "tick"})
        if changed:
            self.request_lan_refresh()
        return changed

    def _resolve_host_worktree(self, node_id):
        """Resolve only an enabled local node to its registered project root.

        HostOperations canonicalizes and revalidates the returned path before
        every spawn.  This resolver supplies the other half of that contract:
        callers name a stable node, never an arbitrary filesystem path, and a
        disabled/forgotten node immediately loses host-operation authority.
        """
        record = self.directory.lookup(node_id)
        if (record is None or record.get("host_id") != self.host_id
                or record.get("enabled", True) is not True
                or self._realm_operation_refusal(
                    record.get("convoy_id")) is not None):
            return None
        expected_convoy = getattr(
            self._hostop_context, "expected_convoy_id", None)
        if (expected_convoy is not None
                and record.get("convoy_id") != expected_convoy):
            return None
        root = record.get("project_root")
        return root if isinstance(root, str) and root else None

    def _allow_host_shell(self, node_id):
        """The literal host-private Full Shell decision for one live member."""
        return (self._resolve_host_worktree(node_id) is not None
                and self.policy.allow_full_shell() is True)

    def _audit_host_operation(self, payload):
        """Append a deliberately narrow, secret-safe host-operation audit.

        HostOperations itself never supplies argv, command text, environment,
        stdout or stderr.  The whitelist here is a second boundary so a future
        facade change cannot silently turn the durable audit into a secret or
        Full Shell command log.
        """
        if not isinstance(payload, dict):
            return
        event = payload.get("event")
        if not isinstance(event, str) or not event.startswith(
                "host_operation_"):
            return
        allowed = {
            "capability", "operation", "target_id", "mutating", "code",
            "exit_code", "truncated", "duration_ms", "cwd"}
        detail = {key: payload[key] for key in allowed if key in payload}
        # Structured Git/GH arguments are reviewed enum fields.  Full Shell
        # never gets this exception, even if a future caller accidentally
        # supplies an `arguments` member.
        if (payload.get("capability") != hostops_mod.HOST_SHELL_CAPABILITY
                and isinstance(payload.get("arguments"), dict)):
            detail["arguments"] = payload["arguments"]
        try:
            self.db.audit("hostops", event, detail)
        except Exception:
            pass

    def set_shutdown_hook(self, hook):
        """Wire /shutdown to the server-stopping callable main() already
        installs for SIGTERM. Kept a setter rather than a constructor
        argument because the server does not exist until after
        HostApp is built and handed to serve()."""
        self._shutdown_hook = hook

    # -- request handlers (called WITH self.lock held -- except the
    #    self-locking dispatch_job / drain, see their docstrings) -------

    def status(self):
        # SELF-LOCKING, and the reason is a measured starvation, not
        # tidiness. state_counts() reads EVERY job file; held under the
        # app lock it made /status the slowest route in the system, and a
        # single 1 Hz /status poller then drove /peers to 331x its normal
        # latency while /health (the only lock-free route) stayed at 2 ms
        # -- so the supervising OS task saw a healthy host that was in
        # fact wedged for seconds at a stretch. drain_once already moved
        # its scan out of the lock for exactly this reason. So must this:
        # the scan runs LOCK-FREE (job files are written atomically; a
        # vanished one just isn't counted), then the lock is taken only
        # for the O(1) in-memory snapshot. NEVER call this from inside
        # `with self.lock:` -- it self-locks and would deadlock.
        counts = self.db.state_counts()
        with self.lock:
            return self._status_locked(counts)

    def _status_locked(self, counts):
        """The in-memory half of status(). CALLED WITH self.lock held.

        Every read here is O(1) in memory or a single cheap stat on a
        host-private file (peers.json / denylist) -- the unbounded jobs
        scan is done by the caller, lock-free.
        """
        peer_records = self.peers.peers()
        return {
            "ok": True,
            "protocol": "convoy-host/1",
            "host_id": self.host_id,
            "app_version": APP_VERSION,
            "nodes": len(self.directory.nodes()),
            "realm": self._realm_projection_locked(),
            "jobs_queued": counts.get("queued", 0),
            # Node jobs the host handed off and is polling: work in
            # flight ON THE NODE, invisible in jobs_queued.
            "jobs_running": counts.get("running", 0),
            "polls_in_flight": len(self._polls_in_flight),
            "quarantined_nodes": len(self.quarantined),
            "drain_loop": bool(self._drain_thread is not None
                               and self._drain_thread.is_alive()),
            # Identity, reported HONESTLY rather than omitted: a host
            # with no keypair is a host that can never join a LAN
            # convoy, and "no identity" must be visible in the same
            # place an operator already looks, with the reason attached.
            #
            # TWO SEPARATE CONDITIONS, two separate fields, because they
            # have different consequences: identity_reason means there
            # is NO identity at all, while identity_cert_reason means
            # the identity is fine and only the derived TLS artifact is
            # missing. Collapsing them would make "cannot sign anything"
            # and "cannot serve TLS yet" look identical.
            #
            # identity_reason carries the HostKeyError reason verbatim,
            # the same string /identity, /identity/rotate and the audit
            # trail use -- one condition, one machine-readable code, on
            # every surface.
            "identity_alg": (self.hostkeys.alg if self.hostkeys
                             else None),
            "identity_fingerprint": (self.hostkeys.fingerprint
                                     if self.hostkeys else None),
            "identity_reason": (None if self.hostkeys
                                else IDENTITY_UNAVAILABLE_REASON),
            "identity_certificate": (
                bool(self.hostkeys and self.hostkeys.certificate_pem)),
            "identity_cert_reason": (self.hostkeys.cert_reason
                                     if self.hostkeys else None),
            # PEERS, reported where an operator already looks. Three
            # separate conditions because they mean different things: how
            # many peers may act, whether the A-32 killswitch is refusing
            # everyone, and whether either host-private file has gone
            # unreadable (which refuses everyone too, for a different
            # reason, and must never look like "no peers admitted").
            "peers_admitted": sum(
                1 for p in peer_records
                if p["state"] == peers_mod.PEER_ADMITTED),
            "peers_total": len(peer_records),
            "lan_killswitch": bool(self.peers.killswitch().get("engaged")),
            "peers_reason": self.peers.unreadable,
            "denylist_fail_closed": bool(
                self.peers.denylist.snapshot()["fail_closed"]),
            "lifecycle_available": self.lifecycle is not None,
            "lifecycle_capability":
                lifecycle_mod.HOST_LIFECYCLE_CAPABILITY,
            "lifecycle_reason": (None if self.lifecycle is not None
                                 else self.lifecycle_unavailable_reason
                                 or "lifecycle_unavailable"),
            "uptime_s": round(self._now() - self.started, 1),
        }

    # -- host identity (Phase 3 slice 1) -------------------------------
    #
    # LOOPBACK ONLY, behind the existing IPC token, in the existing
    # route table. When the LAN listener arrives in slice 3 it gets its
    # OWN handler class and its own table; /identity* is named in the
    # plan's loopback list and must never appear in the peer one.

    def _identity_unavailable(self):
        return 503, {"ok": False, "reason": IDENTITY_UNAVAILABLE_REASON,
                     "detail": self.identity_detail,
                     "host_id": self.host_id}

    def get_identity(self):
        """This host's PUBLIC identity. Never the private key -- there
        is no code path on any route that serializes it."""
        if self.hostkeys is None:
            return self._identity_unavailable()
        payload = {"ok": True, "host_id": self.host_id}
        payload.update(self.hostkeys.public_identity())
        return 200, payload

    def rotate_identity(self, body):
        """Mint a fresh keypair, retiring the old one. GATED.

        Every peer that pinned this host will now see pin_mismatch and
        must re-admit after comparing fingerprints out of band. That is
        the point of the operation, not a side effect of it -- and it is
        why the caller must ECHO THE CURRENT FINGERPRINT
        (`confirm_fingerprint`) to prove it knows which identity it is
        destroying. A bare POST with `{}` used to be sufficient, which
        made a blind or replayed call permanently cost a two-human
        out-of-band re-admission on every peer in the fleet.

        THE THREE OUTCOMES ARE REPORTED HONESTLY, and getting that wrong
        is how the previous version silently changed the host identity:
          200  rotated      -- disk moved, memory follows, audited.
          409  refused      -- disk did NOT move. Safe to retry.
          500  indeterminate-- disk MAY have moved and could not be
               rolled back. Never reported as a refusal; the identity is
               RE-READ FROM DISK so what this process serves is what the
               next boot will load, and the audit says so.
        """
        if not hostkeys.cryptography_available():
            return self._identity_unavailable()
        previous = self.hostkeys.fingerprint if self.hostkeys else None
        confirm = body.get("confirm_fingerprint")
        if isinstance(confirm, str):
            # The display form is what an operator copies off a screen;
            # accept it and canonicalize rather than refusing a correct
            # answer over letter case.
            confirm = confirm.strip().lower()
        try:
            fresh = hostkeys.rotate(self.data_dir, confirm)
        except hostkeys.RotationIndeterminate as e:
            # THE DISK MAY HAVE MOVED. Re-read it so memory and disk can
            # never disagree, and audit the outcome under its own name
            # -- a caller must not be able to read this as "refused".
            landed = self._reload_identity()
            self.db.audit("hostkeys", "identity_rotate_indeterminate",
                          {"reason": e.reason, "detail": e.detail,
                           "previous_fingerprint": previous,
                           "fingerprint": landed})
            return 500, {"ok": False, "reason": e.reason,
                         "detail": e.detail,
                         "host_id": self.host_id,
                         "previous_fingerprint": previous,
                         "fingerprint": landed}
        except hostkeys.HostKeyError as e:
            # A TRUE refusal: nothing on disk changed. Includes the
            # compare-and-swap refusals and a staged/commit write that
            # failed and rolled back.
            self.db.audit("hostkeys", "identity_rotate_refused",
                          {"reason": e.reason, "detail": e.detail,
                           "fingerprint": previous})
            code = 409 if e.reason.startswith("rotation_") else 500
            return code, {"ok": False, "reason": e.reason,
                          "detail": e.detail, "host_id": self.host_id,
                          "fingerprint": previous}
        except OSError as e:
            # The write layer raises OSError, NOT HostKeyError, and an
            # uncaught one used to escape as an unnamed 500 while the
            # new key sat on disk. rotate() is all-or-nothing now, but
            # this stays as the belt: name it, re-read, audit.
            landed = self._reload_identity()
            self.db.audit("hostkeys", "identity_rotate_indeterminate",
                          {"reason": "rotation_io_error",
                           "detail": f"{type(e).__name__}: {e}",
                           "previous_fingerprint": previous,
                           "fingerprint": landed})
            return 500, {"ok": False, "reason": "rotation_io_error",
                         "detail": f"{type(e).__name__}: {e}",
                         "host_id": self.host_id,
                         "previous_fingerprint": previous,
                         "fingerprint": landed}
        self.hostkeys = fresh
        self._reset_peer_pool()
        self.identity_detail = ""
        self._refresh_discovery_identity()
        # GRANT RESET HOOK (A-12's precedent: remint resets
        # td_python_approved, because a new identity inherits no
        # privileges). Rotation resets every grant attached to the
        # identity -- peer admissions, pins, lan_exposed_approved. There
        # are NO peer grants in slice 1, so there is nothing to reset
        # here yet; slice 2 adds the reset at exactly this point, when
        # peers.json exists to be reset.
        self.db.record_identity_fingerprint(fresh.fingerprint)
        self.db.audit("hostkeys", "identity_rotated",
                      {"previous_fingerprint": previous,
                       "fingerprint": fresh.fingerprint})
        payload = {"ok": True, "host_id": self.host_id,
                   "previous_fingerprint": previous}
        payload.update(fresh.public_identity())
        return 200, payload

    def _reload_identity(self):
        """Re-read the identity from disk after an uncertain write.

        Returns the fingerprint now in force, or None. NEVER raises: it
        is called on the error path, and the caller's job there is to
        report the truth, not to acquire a second failure. A key that is
        now unreadable leaves self.hostkeys as it was and returns None,
        which the audit records as "unknown".
        """
        try:
            self.hostkeys = hostkeys.load_or_create(self.data_dir)
            self._reset_peer_pool()
            self._refresh_discovery_identity()
            return self.hostkeys.fingerprint
        except Exception:
            return None

    def _reset_peer_pool(self):
        self._stop_peer_session_manager(timeout_s=1.0)
        old = getattr(self, "peer_pool", None)
        if old is not None:
            old.close()
        self.peer_pool = (peerclient.PeerConnectionPool(self.hostkeys)
                          if self.hostkeys is not None else None)
        self.request_lan_refresh()

    def _refresh_discovery_identity(self):
        """Publish a live identity replacement without touching TD state."""
        service = self.discovery_service
        if service is None:
            return
        if self.hostkeys is None or not self.hostkeys.certificate_pem:
            try:
                service.stop()
            finally:
                self.discovery_service = None
            return
        try:
            service.replace_identity(self.hostkeys)
        except Exception as exc:
            try:
                service.stop()
            finally:
                self.discovery_service = None
            self._audit_best_effort(
                "discovery_identity_refresh_failed",
                {"error": f"{type(exc).__name__}: {exc}"})
            self.request_lan_refresh()

    def request_shutdown(self, body=None):
        """Stop the host app cleanly, on request. Authenticated (every
        POST is) and audited.

        THE POINT IS THE PORTFILE. Stop, upgrade and uninstall all need
        the daemon gone, and their only alternative is a hard kill --
        which on Windows skips every cleanup path and leaves a portfile
        naming a dead port. Clients survive that (read_live_portfile
        checks the writer's pid), but the NEXT install then starts
        against a data dir that looks occupied. An orderly exit unwinds
        main()'s `finally`, which clears it.

        This does NOT stop the server itself: it fires the SAME callable
        the SIGTERM handler does -- a thread that calls
        server.shutdown() -- so there is exactly one shutdown path and
        the response still goes out on this connection before
        serve_forever() unwinds. Inventing a second path here is how the
        two drift.
        """
        if self._shutdown_hook is None:
            # No server to stop: an embedded HostApp, or one built
            # without serve(). Refusing beats reporting a shutdown that
            # nothing performed.
            return 409, {"ok": False, "reason": "shutdown_unavailable",
                         "detail": "this host app is not serving"}
        try:
            self.db.audit("hostapp", "shutdown_requested",
                          {"host_id": self.host_id})
        except Exception:
            pass        # an audit failure must not strand a stop request
        self._shutdown_hook()
        return 200, {"ok": True, "host_id": self.host_id, "stopping": True}

    def register_node(self, body):
        """Register one live TD runtime and enable its Convoy membership.

        SELF-LOCKING: stable-identity collision probing performs one bounded
        loopback connect without the app lock; all directory/store mutations
        then occur atomically under it.  Route handlers must call this method
        outside their own lock block.
        """
        try:
            project_root = text_field(body, "project_root", limit=4096)
            convoy_id = identity.normalize_convoy_id(
                text_field(body, "convoy_id", limit=MAX_ID_CHARS))
            # Pre-genesis callers omitted this field and persisted durable
            # IDs, so rolling compatibility must treat omission as
            # established, never as a fresh/rebindable candidate.
            binding_state = identity.normalize_binding_state(
                body.get("binding_state", realm_mod.ESTABLISHED))
            comp_path = text_field(body, "comp_path", limit=512)
            runtime_id = text_field(
                body, "runtime_id", required=False, limit=128) or None
            raw_discriminator = body.get("node_discriminator")
            discriminator = identity.normalize_node_discriminator(
                raw_discriminator)
            metadata_present = "metadata" in body
            clean_metadata = (identity.sanitize_node_metadata(
                body.get("metadata")) if metadata_present else None)
            td_executable = (text_field(
                body, "td_executable", required=False, limit=4096) or None)
            launch_token = (text_field(
                body, "launch_token", required=False, limit=256) or None)
            launch_reservation_id = (text_field(
                body, "launch_reservation_id", required=False,
                limit=256) or None)
        except (Malformed, identity.IdentityError) as e:
            reason = getattr(e, "reason", "malformed")
            detail = getattr(e, "detail", str(e))
            self._audit_best_effort("register_refused",
                                    {"reason": reason, "detail": detail})
            return 400, {"ok": False, "reason": reason, "detail": detail}

        if (launch_token is None) != (launch_reservation_id is None):
            return self._refuse(
                "register", "malformed",
                "launch_token and launch_reservation_id must be paired", 400)
        if (launch_token is not None
                and not re.fullmatch(r"[A-Za-z0-9_-]{32,256}",
                                     launch_token)):
            return self._refuse(
                "register", "malformed",
                "launch_token must be bounded URL-safe text", 400)

        # Where the node's local Envoy listens. Optional and per-launch.
        envoy_port = body.get("envoy_port")
        if envoy_port is not None and (
                isinstance(envoy_port, bool)
                or not isinstance(envoy_port, int)
                or not (1 <= envoy_port <= 65535)):
            self._audit_best_effort(
                "register_refused",
                {"reason": "malformed", "detail": "envoy_port"})
            return 400, {"ok": False, "reason": "malformed",
                         "detail": "envoy_port must be an integer 1..65535"}

        # Explicit endpoint readiness lets Perform Mode retire a previously
        # registered Envoy port without overloading port 0/null.  Legacy
        # callers omit the bit and retain the historical preserve-on-omit
        # behavior.
        envoy_ready = body.get("envoy_ready")
        if envoy_ready is not None and not isinstance(envoy_ready, bool):
            return self._refuse("register", "malformed",
                                "envoy_ready must be boolean", 400)
        if envoy_ready is True and envoy_port is None:
            return self._refuse("register", "malformed",
                                "envoy_ready requires envoy_port", 400)
        if envoy_ready is False and envoy_port is not None:
            return self._refuse("register", "malformed",
                                "envoy_port conflicts with envoy_ready=false",
                                400)

        live_bools = {}
        for field in ("remote_wake", "perform_mode", "wake_active"):
            value = body.get(field, False)
            if not isinstance(value, bool):
                return self._refuse("register", "malformed",
                                    f"{field} must be boolean", 400)
            live_bools[field] = value
        wake_pending = body.get("wake_pending", False)
        if not isinstance(wake_pending, bool):
            return self._refuse("register", "malformed",
                                "wake_pending must be boolean", 400)
        wake_port = body.get("wake_port")
        wake_token = body.get("wake_token")
        if (wake_port is None) != (wake_token is None):
            return self._refuse(
                "register", "malformed",
                "wake_port and wake_token must be supplied together", 400)
        if wake_port is not None:
            if (isinstance(wake_port, bool)
                    or not isinstance(wake_port, int)
                    or not (1 <= wake_port <= 65535)):
                return self._refuse(
                    "register", "malformed",
                    "wake_port must be an integer 1..65535", 400)
            if (not isinstance(wake_token, str)
                    or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}",
                                        wake_token)):
                return self._refuse(
                    "register", "malformed",
                    "wake_token must be bounded URL-safe text", 400)
            if not live_bools["remote_wake"]:
                return self._refuse(
                    "register", "malformed",
                    "a wake endpoint requires remote_wake=true", 400)
        if live_bools["wake_active"] and not live_bools["perform_mode"]:
            return self._refuse(
                "register", "malformed",
                "wake_active requires perform_mode=true", 400)
        if (live_bools["perform_mode"] and not live_bools["wake_active"]
                and envoy_ready is True):
            return self._refuse(
                "register", "malformed",
                "a sleeping Perform node cannot advertise Envoy ready", 400)
        wake_grace_s = body.get("wake_grace_s", 60)
        if (isinstance(wake_grace_s, bool)
                or not isinstance(wake_grace_s, int)
                or wake_grace_s < 0 or wake_grace_s > 3600):
            return self._refuse(
                "register", "malformed",
                "wake_grace_s must be an integer from 0 through 3600", 400)

        # Snapshot a possible incumbent, then test its old port without
        # holding the coordination lock.  Same port is a normal process
        # restart (the old process could not still own it); different live
        # ports mean two processes are claiming one stable saved-.toe node.
        try:
            with self.lock:
                incumbent = self.directory.lookup_location(
                    project_root, comp_path,
                    node_discriminator=(discriminator or None))
        except identity.IdentityError as e:
            return self._refuse("register", e.reason, e.detail, 400)
        # An omitted runtime id is a legacy heartbeat, not a new claimant:
        # preserve the incumbent runtime rather than minting a different one
        # on every heartbeat. New nodes still mint in NodeDirectory.
        if incumbent is not None and runtime_id is None:
            runtime_id = incumbent.get("runtime_id")

        # An exact-node launch reservation fences the gap between the
        # lifecycle worker's final offline check and Popen. A normal/manual
        # claimant may not steal that target while the reservation is live.
        incumbent_reservation = (
            self.lifecycle_runtime.reservation_for_node(
                incumbent.get("node_id")) if incumbent is not None else None)
        if incumbent_reservation is not None:
            if self.lifecycle is None:
                return self._refuse(
                    "register", "lifecycle_unavailable",
                    self.lifecycle_unavailable_reason
                    or "exact-node lifecycle is unavailable", 503)
            if (launch_token is None
                    or launch_reservation_id !=
                    incumbent_reservation.get("reservation_id")):
                return self._refuse(
                    "register", "launch_reservation_mismatch",
                    "an exact lifecycle launch currently owns this node",
                    409, node=incumbent)

        runtime_changed = bool(
            incumbent and incumbent.get("runtime_id") and runtime_id
            and incumbent.get("runtime_id") != runtime_id)
        incumbent_pid = ((incumbent.get("metadata") or {}).get("process_id")
                         if incumbent else None)
        claimant_pid = ((clean_metadata or {}).get("process_id")
                        if metadata_present else None)
        ownership_checked = False
        incumbent_alive = False
        if runtime_changed:
            if incumbent_pid and claimant_pid and incumbent_pid == claimant_pid:
                # Extension reinitialization inside the same TD process.
                ownership_checked = True
                incumbent_alive = False
            elif incumbent_pid:
                ownership_checked = True
                incumbent_alive = platform_mod.pid_is_alive(incumbent_pid)
            elif incumbent.get("envoy_port"):
                ownership_checked = True
                incumbent_alive = _loopback_port_open(
                    incumbent.get("envoy_port"))
            else:
                # A legacy/no-port incumbent cannot prove it went away. The
                # safe answer is to preserve its claim until clean unregister
                # or host restart, not silently redirect its stable address.
                ownership_checked = True
                incumbent_alive = True

        with self.lock:
            try:
                current = self.directory.lookup_location(
                    project_root, comp_path,
                    node_discriminator=(discriminator or None))
            except identity.IdentityError as e:
                return self._refuse("register", e.reason, e.detail, 400)

            current_reservation = (
                self.lifecycle_runtime.reservation_for_node(
                    current.get("node_id")) if current is not None else None)
            if current_reservation is not None and self.lifecycle is None:
                return self._refuse(
                    "register", "lifecycle_unavailable",
                    self.lifecycle_unavailable_reason
                    or "exact-node lifecycle is unavailable", 503,
                    node=current)
            if current_reservation is not None and (
                    launch_token is None
                    or launch_reservation_id !=
                    current_reservation.get("reservation_id")):
                return self._refuse(
                    "register", "launch_reservation_mismatch",
                    "an exact lifecycle launch currently owns this node",
                    409, node=current)
            launch_confirmation_required = current_reservation is not None
            conflict = (current and current.get("runtime_id") and runtime_id
                        and current.get("runtime_id") != runtime_id)
            if conflict:
                same_snapshot = (incumbent is not None
                                 and incumbent.get("node_id") ==
                                 current.get("node_id")
                                 and incumbent.get("runtime_id") ==
                                 current.get("runtime_id")
                                 and incumbent.get("envoy_port") ==
                                 current.get("envoy_port")
                                 and incumbent.get("wake_port") ==
                                 current.get("wake_port"))
                if not same_snapshot or not ownership_checked or incumbent_alive:
                    return self._refuse(
                        "register", "node_runtime_conflict",
                        "another live runtime already owns this saved .toe "
                        "node; give each live .toe its own saved path or "
                        "close the existing runtime", 409,
                        node=current)
            if (current is not None
                    and current.get("binding_state") ==
                    realm_mod.ESTABLISHED
                    and current.get("convoy_id") != convoy_id
                    and binding_state == realm_mod.ESTABLISHED):
                return self._refuse(
                    "register", "node_identity_conflict",
                    "this saved TouchDesigner node is already bound to "
                    f"Convoy {current.get('convoy_id')!r}", 409,
                    node=current)

            authority, realm_refusal = self._accept_registration_realm_locked(
                convoy_id, binding_state, current=current,
                source={"project_root": str(project_root),
                        "hostname": str((clean_metadata or {})
                                        .get("hostname") or "")})
            if realm_refusal is not None:
                code, reason, detail = realm_refusal
                return self._refuse(
                    "register", reason, detail, code, node=current,
                    extra={"convoy_id": convoy_id,
                           "binding_state": binding_state})
            authoritative_id, authoritative_state = authority
            # Candidate adoption may have rebound this incumbent while the
            # lock was held. Snapshot it again so persistence rollback can
            # never restore the pre-convergence binding.
            try:
                current = self.directory.lookup_location(
                    project_root, comp_path,
                    node_discriminator=(discriminator or None))
            except identity.IdentityError as e:
                return self._refuse("register", e.reason, e.detail, 400)

            known_before = {r["node_id"] for r in self.directory.nodes()}
            previous = dict(current) if current else None
            if previous and isinstance(previous.get("metadata"), dict):
                previous["metadata"] = dict(previous["metadata"])
            try:
                record = self.directory.register(
                    project_root, comp_path, authoritative_id,
                    runtime_id=runtime_id, envoy_port=envoy_port,
                    node_discriminator=(discriminator or None),
                    binding_state=authoritative_state)
                newly_minted = record["node_id"] not in known_before
                self.directory.set_live_state(
                    record["node_id"], envoy_ready=envoy_ready,
                    wake_port=wake_port, wake_token=wake_token,
                    remote_wake=live_bools["remote_wake"],
                    perform_mode=live_bools["perform_mode"],
                    wake_active=live_bools["wake_active"],
                    wake_grace_s=wake_grace_s)
                self.directory.set_enabled(record["node_id"], True)
                if metadata_present:
                    self.directory.set_metadata(record["node_id"],
                                                clean_metadata)
                record = self.directory.lookup(record["node_id"])
                record["last_heartbeat_unix"] = self._now()
                # Persist before acknowledging. The PSK and membership
                # intent share this failure domain.
                if self._realm_operation_refusal(record["convoy_id"]) is None:
                    self.db.ensure_convoy_psk(record["convoy_id"])
                self.db.save_node(record)
                superseded = self._retire_superseded_nodes_locked(record)

                if launch_confirmation_required:
                    runtime_record = dict(record)
                    runtime_record["metadata"] = dict(
                        record.get("metadata") or {})
                    runtime_record["process_id"] = runtime_record[
                        "metadata"].get("process_id")
                    runtime_record["launch_reservation_id"] = \
                        launch_reservation_id
                    confirmation = self.lifecycle.confirm_registration(
                        record["node_id"], record["convoy_id"],
                        launch_token, runtime_record)
                    if not isinstance(confirmation, dict) \
                            or confirmation.get("ok") is not True:
                        if newly_minted:
                            self.directory.forget(record["node_id"])
                            try:
                                self.db.delete_node(record["node_id"])
                            except Exception:
                                pass
                        elif previous is not None:
                            live = self.directory.lookup(record["node_id"])
                            live.clear()
                            live.update(previous)
                            try:
                                self.db.save_node(previous)
                            except Exception:
                                pass
                        reason = (confirmation.get("code")
                                  if isinstance(confirmation, dict)
                                  else "launch_unconfirmed")
                        self._audit_best_effort(
                            "launch_registration_refused",
                            {"node_id": record["node_id"],
                             "reason": str(reason)[:64]})
                        return 409, {
                            "ok": False,
                            "reason": reason or "launch_unconfirmed",
                            "detail": "the exact lifecycle launch could not "
                                      "yet be confirmed; registration was "
                                      "not accepted",
                        }
                elif self.lifecycle is not None and td_executable:
                    # Audited on TRANSITION only: register is the 30s
                    # heartbeat, and a standing failure repeated per beat
                    # buried the audit log (~5.7k lines/day/node, field
                    # 2026-08-19). One line when it breaks, one when it
                    # recovers.
                    profile_reason = None
                    try:
                        self.lifecycle.record_registration(
                            record, td_executable, launch_eligible=True)
                        self.lifecycle.set_enabled(
                            record["node_id"], record["convoy_id"], True,
                            launch_eligible=True)
                    except lifecycle_mod.LifecycleError as exc:
                        # Convoy routing remains usable if this particular TD
                        # process cannot be proven launchable. Lifecycle calls
                        # then fail closed with unknown_profile/profile state.
                        profile_reason = exc.code
                    except Exception:
                        profile_reason = "internal_error"
                    reasons = self._lifecycle_profile_reasons
                    if profile_reason is None:
                        if reasons.pop(record["node_id"], None):
                            self._audit_best_effort(
                                "lifecycle_profile_recorded",
                                {"node_id": record["node_id"]})
                    elif reasons.get(record["node_id"]) != profile_reason:
                        reasons[record["node_id"]] = profile_reason
                        self._audit_best_effort(
                            "lifecycle_profile_unavailable",
                            {"node_id": record["node_id"],
                             "reason": profile_reason})
            except identity.IdentityError as e:
                return self._refuse(
                    "register", e.reason, e.detail,
                    400 if e.reason.startswith("malformed") else 409)
            except Exception as e:
                if 'record' in locals() and record is not None:
                    if record["node_id"] not in known_before:
                        self.directory.forget(record["node_id"])
                    elif previous is not None:
                        live = self.directory.lookup(record["node_id"])
                        live.clear()
                        live.update(previous)
                self._audit_best_effort(
                    "register_failed",
                    {"error": f"{type(e).__name__}: {e}",
                     "rolled_back": True})
                return 500, {"ok": False, "reason": "persist_failed",
                             "detail": f"{type(e).__name__}: {e}"}
            # Audited only when the register CHANGED something -- register
            # doubles as the 30s heartbeat, and a line per beat buried the
            # audit log (field 2026-08-19). "comeback" marks the first
            # register after silence (including after a daemon restart,
            # when the process-local beat is gone).
            register_cause = None
            if newly_minted:
                register_cause = "minted"
            elif previous is not None:
                if previous.get("runtime_id") != record.get("runtime_id"):
                    register_cause = "new_runtime"
                elif previous.get("envoy_port") != record.get("envoy_port"):
                    register_cause = "port_changed"
                else:
                    beat = previous.get("last_heartbeat_unix")
                    try:
                        silent = (beat is None or self._now() - float(beat)
                                  >= self.node_dead_grace_s)
                    except (TypeError, ValueError):
                        silent = True
                    if silent:
                        register_cause = "comeback"
            if register_cause:
                self._audit_best_effort(
                    "node_registered",
                    {"node_id": record["node_id"], "comp_path": comp_path,
                     "node_discriminator": record.get("node_discriminator"),
                     "cause": register_cause})
            self._invalidate_network_nodes_cache_locked()
            self.request_lan_refresh()
            return 200, {
                "ok": True,
                "node_id": record["node_id"],
                "runtime_id": record["runtime_id"],
                "host_id": self.host_id,
                # What code THIS daemon actually runs, so the TD that
                # just registered can update an out-of-date daemon in
                # place. Absence of this key is itself the signal: a
                # pre-6.0.213 daemon never sends it.
                "app_version": APP_VERSION,
                "convoy_id": authoritative_id,
                "realm_state": authoritative_state,
                "envoy_port": record.get("envoy_port"),
                "perform_mode": bool(record.get("perform_mode")),
                "wake_active": bool(record.get("wake_active")),
                "wake_ready": bool(record.get("remote_wake")
                                   and record.get("wake_port")
                                   and record.get("wake_token")),
                "enabled": bool(record.get("enabled", True)),
                "td_python_approved": self.policy.allow_td_python(
                    record["node_id"]),
                "policy": self._policy_projection(record["node_id"]),
            }

    def _retire_superseded_nodes_locked(self, live):
        """Drop node records the just-registered node REPLACES. Lock held.

        Node identity includes the project file, so every Save As, rename or
        versioned save mints a NEW node and leaves the old one listed offline
        for ever. Users read that as "Convoy is showing me duplicates", and
        they are right -- it is one node wearing two names.

        The match is deliberately narrow: same HOST, same project root, same
        COMP path. Not "same IP and offline", which is the tempting rule and
        the wrong one -- two genuinely different projects on one machine share
        an IP, and retiring one of those would delete a real node that is
        still remotely launchable. Same host + root + COMP is the same logical
        node, re-identified.

        A candidate is retired only when it is provably idle OR provably
        THIS registration's own past self: an old row still holding an
        envoy_port is normally live and untouchable -- except when that
        port is the very one the NEW registration carries. A LIVE
        session that saves (TouchDesigner's versioned save renames the
        .toe) re-registers under a new identity without ever
        unregistering the old one, so the predecessor kept a port that
        now belongs to its successor and ghosted for ever
        (field-reported 2026-08-05, the third duplicate report). The
        PORT is the test because it is daemon-owned live state; the
        metadata process_id is client-supplied display data and is
        deliberately never authority for a retirement. A successor
        whose first register has no port yet simply retires its past
        self one heartbeat later, when the port arrives and matches.
        UNFINISHED work still spares the row (the /nodes/forget rule) --
        but a finished result nobody has collected does NOT, because it
        was never held by the row: results are fetched by delivery_id and
        outlive it. Treating the two as one thing is what pinned the
        field's duplicate rows for ever (2026-08-06); see
        _node_work_census and _retire_superseded_row_locked.
        """
        retired = []
        ports_cleared = []
        try:
            root = str(live.get("project_root") or "")
            comp = str(live.get("comp_path") or "")
            if not root or not comp:
                return retired
            live_port = live.get("envoy_port")
            live_rt = live.get("runtime_id")
            live_disc = live.get("node_discriminator")
            # ONE jobs scan for the whole sweep -- and only once a
            # candidate actually exists. Building it up front put a full
            # parse of the jobs directory on EVERY register, inside the
            # app lock, for every host, including the overwhelmingly
            # common case of a heartbeat with nothing to collapse
            # (measured: 0.05s at 500 job records, 0.48s at 5000, worse
            # cold). The predicate this replaced was reachable only from
            # inside the retire call, so it charged nothing there; this
            # keeps that property while still scanning at most once per
            # sweep.
            scan = {}

            def _census(node_id):
                if "index" not in scan:
                    scan["index"], scan["unreadable"] = \
                        self._job_census_index()
                return self._node_work_census(
                    node_id, scan["index"], scan["unreadable"])

            for record in list(self.directory.nodes()):
                node_id = record.get("node_id")
                if (node_id == live.get("node_id")
                        or record.get("host_id") != live.get("host_id")):
                    continue
                same_root = str(record.get("project_root") or "") == root
                same_comp = str(record.get("comp_path") or "") == comp
                old_port = record.get("envoy_port")
                if same_root and same_comp:
                    if old_port and not (live_port
                                         and old_port == live_port):
                        # A genuinely different live server on the same
                        # project (a second TD holding the old file
                        # open): never retire a row that can still
                        # answer. A live server HEARTBEATS, though, and
                        # this spare used to key on the port alone -- so
                        # a session that died without a clean
                        # /unregister (a crash, a kill) left a port
                        # nobody can clear, and the row was spared for
                        # ever exactly like the rows this sweep exists
                        # to collapse. Silence past the dead grace is
                        # the evidence, taken from the DURABLE last_seen
                        # (never a socket probe: a closed-port refusal
                        # costs ~2s on Windows and this runs inline on
                        # every register, under the lock).
                        latest = self._node_last_activity(record)
                        if (latest is None
                                or (self._now() - latest)
                                < self.node_dead_grace_s):
                            continue
                    if self._retire_superseded_row_locked(
                            node_id, live=live, census=_census(node_id)):
                        retired.append(node_id)
                    continue
                # Cross-project rows on this host. Same root + COMP was
                # the original (deliberately narrow) match, and it MISSES
                # a whole class: a Save As writes the .toe into a NEW
                # folder, so the same live TD re-registers under a new
                # project root and its old row keeps a port nobody can
                # ever clear -- immune to eviction (port guard), to this
                # sweep (root mismatch), and even to Forget Offline Nodes
                # (port-bearing refusal). Field-reported 2026-08-05, the
                # FOURTH duplicate report. A cross-project row is claimed
                # only on run-identity (runtime_id, asserted once per
                # launch inside the authenticated same-user IPC boundary)
                # or on the port this registration just proved it owns;
                # descriptive metadata (process_id and friends) never
                # counts.
                same_process = bool(live_rt) and (
                    record.get("runtime_id") == live_rt)
                if (same_process and same_root
                        and record.get("node_discriminator") == live_disc):
                    # A sibling COMP registration of the SAME live
                    # project. Today's client gives every Convoy COMP its
                    # own runtime_id and port, so this spare is defense
                    # in depth -- if a future client ever shared them,
                    # both rows would still be real.
                    continue
                if same_process and same_comp:
                    # The same TD process (runtime_id is minted once per
                    # launch and survives every save) re-registered this
                    # COMP under a new project identity: the old row is
                    # this registration's own past self.
                    if self._retire_superseded_row_locked(
                            node_id, live=live, census=_census(node_id)):
                        retired.append(node_id)
                    continue
                if same_process:
                    # Same run, different COMP AND different project
                    # identity: another of this process's own rows
                    # mid-transition. Its OWN successor registration
                    # retires it through the rule above -- clearing its
                    # port here would also wipe its runtime_id
                    # (clear_envoy_port drops all launch presence) and
                    # destroy the very evidence that retirement needs,
                    # leaving an order-dependent permanent ghost (panel
                    # catch, 2026-08-05).
                    continue
                if old_port and live_port and old_port == live_port:
                    # A loopback port is exclusive per host: a DIFFERENT
                    # process's row claiming the port THIS registration
                    # just proved it owns cannot answer as that node any
                    # more. Keep the row -- it may be a real project,
                    # offline and still remotely launchable -- but clear
                    # the port so the eviction sweep and Forget Offline
                    # Nodes can reach it again.
                    self.directory.clear_envoy_port(node_id)
                    ports_cleared.append(node_id)
            if retired or ports_cleared:
                self._invalidate_network_nodes_cache_locked()
            if retired:
                self._audit_best_effort(
                    "nodes_superseded",
                    {"by": live.get("node_id"), "retired": retired[:8],
                     "count": len(retired)})
            if ports_cleared:
                self._audit_best_effort(
                    "stale_ports_cleared",
                    {"by": live.get("node_id"),
                     "cleared": ports_cleared[:8],
                     "count": len(ports_cleared)})
        except Exception as e:
            # Never let tidying break a registration.
            self._audit_best_effort(
                "supersede_sweep_failed",
                {"error": f"{type(e).__name__}: {e}"})
        return retired

    def _retire_superseded_row_locked(self, node_id, live=None, census=None):
        """Delete ONE superseded row. False means spared (or kept on
        failure) -- and the row is then left fully intact, memory and
        disk agreeing, so a later register simply tries again.

        Durable delete FIRST, the eviction sweep's rule: forgetting the
        directory row before host.json is written would resurrect the
        ghost on the next daemon start. The launch reservation guard is
        the eviction sweep's too -- a row being launched right now is
        not debris, and unknowable reservation state spares (fail
        closed).

        GATE ORDER IS LOAD-BEARING. Every SPARE is evaluated before any
        write, so a row spared by a guard is never left with work this
        method resolved on its way to sparing it -- the in-flight remote
        start being the case that matters (its delivery would have been
        terminalised and the row kept anyway).

        What is NOT atomic, and must not be claimed to be: the queue is
        refused before the durable node delete, so a delete that RAISES
        (a Windows sharing violation outliving _write_private's retries)
        leaves the row present with its queue already terminal. That
        state is consistent and self-healing rather than corrupt -- the
        deliveries are honestly refused (their identity really was
        superseded), the row now has no pending work at all, and the next
        heartbeat's sweep walks straight to the delete. It is audited
        (`superseded_retire_failed`) rather than returned as a silent
        False, because "the row is still here and its work is gone" is
        exactly the state an operator would otherwise have to infer.

        Work only spares a row it can still carry to a verdict:
          - unreadable job store          -> spare (fail closed)
          - a reservation is live         -> spare (a launch is in flight)
          - a forward is out right now    -> spare (a verdict may land)
          - anything past `queued` pends  -> spare (a delivery that crossed
                                             the dispatch boundary is the
                                             node's verdict to give, never
                                             the host's to invent)
          - a queued delivery would still
            RUN without this row          -> spare (see
                                             _queued_delivery_survives_
                                             retirement -- host-native work
                                             does not need the node's
                                             endpoint, and refusing it would
                                             destroy work that was going to
                                             succeed)
          - the rest of the queue         -> refuse it and retire; those
                                             deliveries provably never left
                                             this host, which is exactly
                                             what mark_refused is CAS'd to
        A finished-but-unacknowledged outcome no longer spares anything --
        see _node_work_census.

        There is deliberately no "is the row still pollable" term. It
        would only earn its keep if this method terminalised deliveries
        that had crossed the dispatch boundary -- work whose verdict is
        still collectible from a live endpoint. It never does: that
        class always spares, so the distinction cannot change an
        outcome, and a `queued` delivery has no node_job_id to poll with
        in the first place.
        """
        if census is None:
            census = self._node_work_census(node_id)
        if census.get("unreadable"):
            return False
        try:
            if self.lifecycle_runtime.reservation_for_node(node_id):
                return False
        except Exception:
            return False        # unknowable reservation state: spare
        pending = census.get("pending") or ()
        if pending:
            if any(did in self._in_flight for did, _s, _op in pending):
                return False
            if any(state != "queued" for _d, state, _op in pending):
                return False
            if any(self._queued_delivery_survives_retirement(op)
                   for _d, _s, op in pending):
                return False
            if not self._refuse_superseded_queued_locked(node_id, live,
                                                         pending):
                return False
        try:
            self.db.delete_node(node_id)
        except Exception as exc:
            self._audit_best_effort(
                "superseded_retire_failed",
                {"node_id": node_id, "superseded_by": (live or {}).get(
                    "node_id"),
                 "queue_already_refused": bool(pending),
                 "error": "%s: %s" % (type(exc).__name__, exc)})
            return False
        self.directory.forget(node_id)
        # Same hygiene as forget_node and the eviction sweep: a retired
        # row's launch profile is unreachable and would otherwise
        # accumulate in lifecycle.json on every versioned save of a
        # live session.
        self._forget_launch_profile(node_id)
        self._drop_retired_node_policy(node_id)
        self._lifecycle_profile_reasons.pop(node_id, None)
        return True

    def _queued_delivery_survives_retirement(self, operation):
        """True when a QUEUED delivery would still run after its node row
        is retired -- so retiring must spare the row instead of refusing it.

        The dispatcher decides this, and it is not "is it a lifecycle
        operation": `needs_td_endpoint` is False for every HOST_NATIVE
        operation EXCEPT convoy_restart_node. So convoy_git / convoy_gh /
        convoy_shell / convoy_ping / convoy_start_node execute on the HOST
        and would have succeeded; refusing them as "the node was
        superseded" destroys work that was going to run. Sparing costs
        nothing and is bounded -- the drain executes them within a tick and
        they terminalise on their own, after which the row retires.

        convoy_restart_node is the deliberate exception the dispatcher
        itself carves out: it DOES need the endpoint, so on a superseded
        row it can only defer, for ever. Refusing it is both honest (the
        identity it was addressed to is gone) and the only thing that stops
        it pinning the row permanently -- pressing Start/Restart on a
        duplicate offline row must not make that row undeletable.
        """
        return (operation in HOST_NATIVE_OPERATIONS
                and operation != "convoy_restart_node")

    def _refuse_superseded_queued_locked(self, node_id, live, pending):
        """Refuse the queued deliveries of a row being superseded RIGHT NOW.

        True only when every one of them is durably terminal. A queued
        delivery has provably never left this host (that is precisely the
        state mark_refused is CAS'd to, convoy_hoststore.py), so calling
        it refused invents no execution verdict and cannot contradict a
        node -- A-15 holds. Without this the delivery is immortal: the
        drain can never route it (its node row is about to stop existing,
        and already answers node_endpoint_unknown), the reaper only
        unlinks TERMINAL records, and ack refuses a non-terminal one.

        A failure part-way through does NOT roll back -- there is no
        primitive to un-refuse a delivery, and inventing one would be a
        second way to rewrite history. The deliveries already refused
        stay refused (their identity really was superseded, so the
        verdict is honest either way), the row is spared, and the next
        heartbeat retries the remainder. The partial state is audited
        rather than silent.
        """
        successor = (live or {}).get("node_id")
        done = []
        for delivery_id, state, _operation in pending:
            if state != "queued":
                return False
            evidence = {
                "reason": "node_superseded",
                "detail": ("the node identity this delivery was addressed "
                           "to was superseded by its own successor and can "
                           "never answer again; the delivery never left "
                           "this host"),
                "superseded_by": successor,
                "at": self._now(),
            }
            try:
                updated = self.db.mark_refused(delivery_id, evidence)
            except Exception as exc:
                self._audit_partial_refusal(node_id, successor, done, pending,
                                            "%s: %s" % (type(exc).__name__,
                                                        exc))
                return False
            if not updated:
                # CAS lost it: the delivery left `queued` between the
                # census and the write. Spare, retry next pass.
                self._audit_partial_refusal(node_id, successor, done, pending,
                                            "cas_lost:%s" % delivery_id)
                return False
            done.append(delivery_id)
            self._release_operation_claim_locked(updated)
        self._audit_best_effort(
            "superseded_work_refused",
            {"node_id": node_id, "superseded_by": successor,
             "deliveries": done[:8], "count": len(done)})
        return True

    def _audit_partial_refusal(self, node_id, successor, done, pending,
                               error):
        """Record a queue that was only partly refused, so the state is
        legible instead of inferred. No-op when nothing was written."""
        if not done:
            return
        self._audit_best_effort(
            "superseded_work_partly_refused",
            {"node_id": node_id, "superseded_by": successor,
             "refused": done[:8], "refused_count": len(done),
             "queue_size": len(pending), "error": error})

    def _drop_retired_node_policy(self, node_id):
        """Forget a retired row's TD-Python approval.

        /remint already does this when it re-keys a row, on the same
        argument: an approval is consent for one identity, and the
        identity is gone. Without it every collapsed duplicate leaves a
        stale grant in policy.json for ever -- harmless in isolation
        (node_ids are randomly minted, so nothing can land on one again)
        but unbounded, and this change retires rows automatically.

        Audited like every other revocation of this grant (/remint records
        `node_reminted`): silently dropping an arbitrary-code-execution
        approval is not something to leave only in a diff. The
        allow_td_python check first means the common case (no grant) does
        no write at all, so it cannot bump the policy generation and
        invalidate someone's in-flight approval challenge.
        """
        try:
            if not self.policy.allow_td_python(node_id):
                return      # nothing granted: no write, no generation bump
        except Exception:
            return
        try:
            self.policy.disable_td_python(node_id)
            self._audit_best_effort("node_policy_dropped",
                                    {"node_id": node_id,
                                     "td_python_approved": False})
        except Exception:
            pass        # best effort: the row is already gone

    def _job_census_index(self):
        """(index, unreadable) -- ONE jobs scan for a whole sweep.

        index maps node_id -> {'pending': [(delivery_id, state, operation)]
        for EVERY unfinished delivery, 'pending_count': int, 'unacked': int}.
        'pending' is deliberately uncapped -- the retirement gate has to
        act on the whole queue, and a cap there would spare any row that
        exceeded it. _CENSUS_NAMED_CAP slices it at the point of DISPLAY.

        Read through scan_jobs, NOT jobs(): scan_jobs RAISES on a listing
        failure and reports unreadable records separately, so "could not
        read" stops being the same answer as "found nothing". jobs()
        collapses both into an empty list, which made the old guard fail
        OPEN on an unreadable store -- the exact opposite of the
        fail-closed contract its docstring claimed.

        Hoisted deliberately: the predicate this replaces re-parsed the
        entire jobs directory once PER CANDIDATE ROW, so eight duplicate
        rows cost eight full scans on every heartbeat register, under the
        app lock.
        """
        index = {}
        try:
            jobs, unreadable_ids = self.db.scan_jobs()
        except Exception:
            return None, ["*"]
        if unreadable_ids:
            # NAME them. An unreadable record blocks every cleanup path
            # below, and nothing else in the daemon ever reports one: reap
            # skips it (`job is None: continue`), so without this the
            # operator sees three sweeps and a button silently doing
            # nothing, with no way to learn which file to remove.
            self._audit_best_effort(
                "job_store_unreadable",
                {"count": len(unreadable_ids),
                 "delivery_ids": list(unreadable_ids)[:8]})
        for job in jobs:
            node_id = (job or {}).get("node_id")
            if not node_id:
                continue
            entry = index.setdefault(
                node_id, {"pending": [], "pending_count": 0, "unacked": 0})
            state = str((job or {}).get("state") or "")
            if state not in hoststore.TERMINAL_STATES:
                entry["pending_count"] += 1
                entry["pending"].append(
                    (job.get("delivery_id"), state, job.get("operation")))
            elif job.get("outcome_acknowledged_at") is None:
                entry["unacked"] += 1
        return index, list(unreadable_ids)

    def _node_work_census(self, node_id, index=_CENSUS_UNSET, unreadable=()):
        """What work, if any, one node still owns -- as three separable
        facts rather than one collapsed boolean.

        THE DISTINCTION THE OLD PREDICATE LOST: a delivery that has not
        finished ('pending') genuinely needs its node, because only that
        node can produce the verdict. A finished-but-unacknowledged
        outcome does not: GET /jobs/<delivery_id> and POST /jobs/ack
        both resolve purely by delivery_id and never consult the node
        directory, and forgetting a row does not delete a single job
        record. Collapsing the two meant one uncollected result pinned a
        duplicate row for ever, against ALL THREE cleanup paths -- the
        field's eight versioned-save siblings (2026-08-06), 115 of whose
        123 pins were exactly this class.

        'unreadable' fails closed for every caller: a record nobody can
        parse might be this node's, and might be pending, so it must never
        license a deletion. It carries the offending DELIVERY IDS rather
        than a bare flag -- an unreadable record cannot be attributed to a
        node (that is what unreadable means), so the block is unavoidably
        host-wide, and the only thing that keeps it from being an
        invisible, permanent freeze of every cleanup path is naming the
        files. reap() removes one past retention (see convoy_hoststore),
        so the state is bounded rather than for ever.
        """
        if index is _CENSUS_UNSET:
            index, unreadable = self._job_census_index()
        unreadable = list(unreadable or ())
        if index is None or unreadable:
            return {"pending": [], "pending_count": 0, "unacked": 0,
                    "unreadable": True, "unreadable_ids": unreadable}
        entry = index.get(node_id) or {}
        return {
            "pending": list(entry.get("pending") or ()),
            "pending_count": int(entry.get("pending_count") or 0),
            "unacked": int(entry.get("unacked") or 0),
            "unreadable": False,
            "unreadable_ids": [],
        }

    def forget_node(self, body):
        """ADVANCED RECOVERY: delete a stale node record entirely.

        /unregister is the ordinary shutdown path and deliberately KEEPS the
        node -- node_id is the durable address approvals attach to, and a
        closed TD must stay listed as remotely launchable. That leaves genuine
        debris behind though: a renamed or moved .toe mints a new node_id, so
        the old row lingers offline forever with no way to clear it (the plan's
        "Forget Stale Node" action, section 7.5).

        Refuses while the node still has work it can carry to a verdict: an
        UNFINISHED delivery, because only this node can answer it. A finished
        outcome nobody has acknowledged no longer refuses -- it never needed
        the row (results are fetched by delivery_id, and forgetting a row
        deletes no job record), and treating it as a blocker is what pinned
        the field's duplicate rows for ever. Force is deliberately NOT offered
        here -- cancel the unfinished work first, or wait for it to answer.

        The refusal NAMES the deliveries in its body: `_refuse`'s own `extra`
        lands in the audit record only, so the caller could previously see a
        de-duplicated list of state WORDS and had no way to find the jobs.
        """
        try:
            node_id = text_field(body, "node_id")
        except Malformed as e:
            return self._refuse("forget_node", "malformed", e.detail, 400)
        record = self.directory.lookup(node_id)
        if record is None:
            return self._refuse("forget_node", "unknown_node", node_id, 404)
        census = self._node_work_census(node_id)
        if census["unreadable"]:
            # Never delete on an unreadable job store: fail closed.
            return self._refuse(
                "forget_node", "job_state_unreadable",
                "the durable job records could not be read; not forgetting",
                503)
        if census["pending"]:
            blocking = [
                {"delivery_id": did, "state": state, "operation": operation}
                for did, state, operation in
                census["pending"][:_CENSUS_NAMED_CAP]]
            code, payload = self._refuse(
                "forget_node", "node_has_work",
                "this node still has %d delivery(s) that have not finished "
                "(%s); cancel a queued one or wait for the node to answer"
                % (census["pending_count"],
                   ", ".join(sorted({s for _d, s, _o in census["pending"]}))),
                409)
            payload["blocking"] = blocking
            payload["pending_count"] = census["pending_count"]
            # Context, not a reason: finished results do NOT hold the row
            # any more, and saying how many are uncollected stops the
            # count being mistaken for the blocker it used to be.
            payload["uncollected_results"] = census["unacked"]
            return code, payload
        # Durable delete FIRST -- the rule _retire_superseded_row_locked
        # documents and the eviction sweep follows. This path had it
        # backwards: it forgot the row in memory and only then wrote
        # host.json, so a failed write returned 500 with the row gone
        # from the directory, still on disk, and resurrecting on the next
        # daemon start.
        try:
            self.db.delete_node(node_id)
        except Exception as e:
            return self._refuse("forget_node", "forget_failed",
                                "%s: %s" % (type(e).__name__, e), 500)
        self.directory.forget(node_id)
        self._forget_launch_profile(node_id)
        self._drop_retired_node_policy(node_id)
        self._lifecycle_profile_reasons.pop(node_id, None)
        self._invalidate_network_nodes_cache_locked()
        self._audit_best_effort("node_forgotten", {"node_id": node_id})
        return 200, {"ok": True, "forgotten": True, "node_id": node_id}

    def _forget_launch_profile(self, node_id):
        """Drop the launch profile a forgotten node would orphan.

        Best-effort: the node row is already gone, and a leftover profile
        is unreachable anyway (every start path resolves the directory row
        first) -- but leaving it accumulates dead entries in
        lifecycle.json for ever.
        """
        try:
            if self.lifecycle is not None:
                self.lifecycle.store.delete_profile(node_id)
        except Exception:
            pass

    def _node_last_activity(self, record):
        """Newest liveness stamp we hold for a node, or None.

        last_heartbeat_unix is process-local (stamped on register and
        unregister); the durable last_seen in host.json survives daemon
        restarts. Take the newest of whichever exist.
        """
        stamps = []
        try:
            beat = record.get("last_heartbeat_unix")
            if beat is not None:
                stamps.append(float(beat))
        except Exception:
            pass
        try:
            seen = self.db.node_last_seen(record.get("node_id"))
            if seen is not None:
                stamps.append(float(seen))
        except Exception:
            pass
        return max(stamps) if stamps else None

    # Directories whose direct children are VOLUMES, not folders: a
    # missing child here means an unplugged/unmounted drive, never a
    # deletion. (Windows needs no entry -- an unplugged drive letter has
    # no reachable ancestry at all, which the walk below already spares.)
    _MOUNT_CONTAINERS = ("/volumes", "/mnt", "/media", "/run/media")

    @classmethod
    def _path_provably_deleted(cls, path):
        """True when a path is gone but was demonstrably DELETED.

        Deleted means: some ancestor still exists, and the first missing
        segment is an ordinary folder -- not a volume sitting directly
        under a mount container (/Volumes, /mnt, /media...) and not an
        unreachable drive letter. An unplugged or unmounted project must
        never evict its node: the row is its launch handle when it
        returns.
        """
        try:
            # Relative paths (buggy client metadata, or metadata written by
            # another OS's path rules) prove nothing about deletion.
            if not path or not os.path.isabs(path) or os.path.exists(path):
                return False
            # Walk up to the FIRST MISSING segment whose parent exists.
            first_missing = path
            while True:
                parent = os.path.dirname(first_missing)
                if not parent or parent == first_missing:
                    return False        # no reachable ancestry: unplugged
                if os.path.exists(parent):
                    break
                first_missing = parent
            norm = parent.replace("\\", "/").rstrip("/").lower() or "/"
            if norm in cls._MOUNT_CONTAINERS:
                return False            # a whole volume is absent, not a file
            # /media/<user>/<drive>: the container is one level deeper.
            if os.path.dirname(norm) in cls._MOUNT_CONTAINERS:
                return False
            # The missing segment BEING a mount container (a foreign-OS
            # path judged here, e.g. /Volumes/... on Windows) proves an
            # absent volume tree, never a deletion.
            missing = first_missing.replace("\\", "/").rstrip("/").lower()
            if missing in cls._MOUNT_CONTAINERS:
                return False
            return True
        except Exception:
            return False

    def _evict_stale_nodes(self, now):
        """Forget provably-dead node rows on the reap cadence.

        Offline is NOT stale (a closed TD stays remotely launchable); a row
        is evicted only when it is silent AND either its .toe is provably
        deleted (dead_project, after node_dead_grace_s of silence), it
        has been silent past node_retention_s (retired_unseen), or it
        never LIVED past node_transient_lived_s and has been silent past
        node_transient_retention_s (transient_unseen -- smoke runs and
        one-shot registrations; an explicit td_python grant spares it as
        deliberate setup). Every eviction honors the /nodes/forget idle
        rules: no live Envoy port, no unresolved or unacknowledged work
        (fail closed on an unreadable job store -- and this is also what
        spares an in-flight remote start: the start job stays
        non-terminal for the whole spawn-to-register window), and no live
        launch reservation. A row with no heartbeat stamp is aged from
        its durable first_seen (it used to be spared FOREVER, field
        2026-08-19). Nothing is evicted during the first
        node_dead_grace_s of daemon uptime, so running TDs get a full
        heartbeat cycle to re-register after a daemon restart.

        Three phases so filesystem probes NEVER run under the app lock
        (the reap's own rule, and the snapshot-then-stat convention
        elsewhere in this file): a hung network volume in a toe_path must
        stall this sweep, not every route on the host.
        """
        if now - self.started < self.node_dead_grace_s:
            return []
        # Phase 1: snapshot candidates under the lock -- memory only.
        with self.lock:
            candidates = []
            for record in list(self.directory.nodes()):
                if record.get("envoy_port"):
                    continue
                node_id = record.get("node_id")
                first = self.db.node_first_seen(node_id)
                latest = self._node_last_activity(record)
                if latest is None:
                    latest = first      # mint time is still provable age
                if latest is None:
                    continue
                silent_s = now - latest
                if silent_s < self.node_dead_grace_s:
                    continue
                lived_s = (max(0.0, latest - first)
                           if first is not None else None)
                toe = str((record.get("metadata") or {})
                          .get("toe_path") or "")
                candidates.append((node_id, silent_s, toe, lived_s))
        if not candidates:
            return []
        # Phase 2: judge OUTSIDE the lock -- this is where stats happen.
        judged = []
        for node_id, silent_s, toe, lived_s in candidates:
            if silent_s >= self.node_retention_s:
                judged.append((node_id, "retired_unseen", silent_s))
            elif (lived_s is not None
                    and lived_s < self.node_transient_lived_s
                    and silent_s >= self.node_transient_retention_s):
                judged.append((node_id, "transient_unseen", silent_s))
            elif toe and os.path.isabs(toe) \
                    and self._path_provably_deleted(toe):
                judged.append((node_id, "dead_project", silent_s))
        if not judged:
            return []
        # Phase 3: re-acquire, re-verify every guard, then forget.
        # Durable delete FIRST: if host.json cannot be written the
        # directory row survives untouched, instead of a memory-forgotten
        # row resurrecting on the next daemon start.
        evicted = []
        with self.lock:
            # ONE jobs scan for the whole phase, not one per candidate.
            evict_index, evict_unreadable = self._job_census_index()
            for node_id, cause, silent_s in judged:
                record = self.directory.lookup(node_id)
                if record is None or record.get("envoy_port"):
                    continue
                latest = self._node_last_activity(record)
                if latest is None:
                    latest = self.db.node_first_seen(node_id)
                if latest is None or now - latest < self.node_dead_grace_s:
                    continue
                if cause == "transient_unseen":
                    try:
                        if self.policy.allow_td_python(node_id):
                            continue    # deliberate setup, not debris
                    except Exception:
                        continue        # unknowable grant state: spare
                census = self._node_work_census(
                    node_id, evict_index, evict_unreadable)
                if census["unreadable"] or census["pending"]:
                    # NO resolution on this path: an evicted row has no
                    # successor claiming its identity, so nothing
                    # licenses the host to terminalise its work. Only
                    # UNFINISHED work spares now -- an uncollected
                    # result never needed its row (see
                    # _node_work_census).
                    continue
                try:
                    if self.lifecycle_runtime.reservation_for_node(node_id):
                        continue
                except Exception:
                    continue        # unknowable reservation state: spare
                try:
                    self.db.delete_node(node_id)
                except Exception:
                    continue
                self.directory.forget(node_id)
                self._forget_launch_profile(node_id)
                self._drop_retired_node_policy(node_id)
                self._lifecycle_profile_reasons.pop(node_id, None)
                evicted.append({"node_id": node_id, "cause": cause,
                                "silent_s": round(silent_s, 1)})
            if evicted:
                self._invalidate_network_nodes_cache_locked()
                self._audit_best_effort(
                    "nodes_evicted",
                    {"count": len(evicted), "evicted": evicted[:8]})
        return evicted

    def unregister_node(self, body):
        """Clear a node's live Envoy port -- it is shutting down cleanly.

        The counterpart to /register, called best-effort from the TD side
        on exit and on disable. It does NOT delete the node: node_id is
        the durable address (approvals attach to it), and only envoy_port
        is per-launch. Without this, a closed TD leaves behind a port the
        dispatcher keeps forwarding into, retrying every 30 s, forever.

        A hard kill still leaves a stale port -- unavoidable, and handled
        elsewhere: an UNREACHABLE forward keeps the job queued and backs
        off. This route makes the COMMON case clean, exactly like the
        SIGTERM handler makes the common host stop clean.

        ONLY CLEAR THE PORT YOU REGISTERED. node_id is derived from
        (project_root, comp_path), so two TD sessions on one project
        folder share it -- the plan's OQ-1. Without a precondition, the
        first session's CLEAN EXIT zeroes the second session's live
        port, and the surviving node goes undispatchable until its next
        heartbeat: a wrong-direction failure manufactured by an orderly
        shutdown. runtime_id is the per-launch proof of which run is
        talking (the same field A-22's expected_runtime_id relies on),
        so a caller that supplies it and does not match is answered with
        a 200 no-op. That is the plan's shared-identity rule -- "do not
        fight" -- rather than a refusal, because the departing session
        genuinely has nothing left to do.
        """
        try:
            node_id = text_field(body, "node_id")
            runtime_id = text_field(body, "runtime_id", required=False)
            # Missing is the pre-intent wire shape used by older builds;
            # those calls meant "this process is closing", not "withdraw
            # this durable node from Convoy", so rolling upgrades map it to
            # the safe shutdown semantics.
            reason = (text_field(body, "reason", required=False,
                                 limit=16) or "shutdown")
        except Malformed as e:
            return self._refuse("unregister", "malformed", e.detail, 400)
        if reason not in ("disabled", "shutdown"):
            return self._refuse(
                "unregister", "invalid_unregister_reason",
                "reason must be disabled or shutdown", 400)
        record = self.directory.lookup(node_id)
        if record is None:
            return self._refuse("unregister", "unknown_node", node_id, 404)
        current = record.get("runtime_id")
        if runtime_id and current and runtime_id != current:
            self._audit_best_effort("unregister_superseded",
                                    {"node_id": node_id,
                                     "claimed_runtime_id": runtime_id,
                                     "current_runtime_id": current})
            return 200, {"ok": True,
                         "cleared": False,
                         "reason": "runtime_superseded",
                         "node_id": record["node_id"],
                         "host_id": self.host_id,
                         "envoy_port": record.get("envoy_port"),
                         "enabled": bool(record.get("enabled", True)),
                         "td_python_approved": self.policy.allow_td_python(
                             record["node_id"]),
                         "policy": self._policy_projection(
                             record["node_id"])}
        self.directory.clear_envoy_port(node_id)
        record["last_heartbeat_unix"] = self._now()
        if reason == "disabled":
            self.directory.set_enabled(node_id, False)
            if self.lifecycle is not None:
                try:
                    self.lifecycle.set_enabled(
                        node_id, record["convoy_id"], False,
                        launch_eligible=False)
                except Exception:
                    self._audit_best_effort(
                        "lifecycle_disable_failed",
                        {"node_id": node_id,
                         "reason": "profile_unavailable"})
        # Unlike register there is NOTHING to roll back: envoy_port is
        # per-launch and hoststore.save_node does not persist it, so the
        # clear is already complete in the only place it lives. This write
        # exists to stamp last_seen. Refusing a clear that has demonstrably
        # happened would be a lie, so a failed stamp is audited, not
        # escalated to a 500.
        try:
            self.db.save_node(record)
        except Exception as e:
            self._audit_best_effort("unregister_persist_failed",
                                    {"node_id": node_id,
                                     "error": f"{type(e).__name__}: {e}"})
        self._audit_best_effort("node_unregistered",
                                {"node_id": record["node_id"],
                                 "comp_path": record["comp_path"],
                                 "reason": reason,
                                 "enabled": bool(record.get("enabled", True))})
        self._record_lifecycle_exit(node_id, runtime_id or current)
        self._invalidate_network_nodes_cache_locked()
        self.request_lan_refresh()
        return 200, {"ok": True,
                     "cleared": True,
                     "node_id": record["node_id"],
                     "host_id": self.host_id,
                     "envoy_port": record.get("envoy_port"),
                     "enabled": bool(record.get("enabled", True)),
                     "reason": reason,
                     "td_python_approved": self.policy.allow_td_python(
                         record["node_id"]),
                     "policy": self._policy_projection(record["node_id"])}

    def _audit_best_effort(self, event, detail):
        """Audit without letting the trail's failure fail the request.

        Same reasoning as _refuse's swallowed audit: an unregister that
        really cleared the port must report success even if the append
        could not be written.
        """
        try:
            self.db.audit("hostapp", event, detail)
        except Exception:
            pass

    def _audit_lifecycle(self, detail):
        """LifecycleManager's already-redacted audit sink."""
        if not isinstance(detail, dict):
            return
        event = str(detail.get("event") or "lifecycle_event")[:64]
        payload = {key: value for key, value in detail.items()
                   if key != "event" and isinstance(
                       value, (str, int, float, bool))}
        self._audit_best_effort(event, payload)

    def _record_lifecycle_exit(self, node_id, runtime_id):
        """Classify a confirmed launch's early exit without blocking IPC."""
        if self.lifecycle is None or not runtime_id:
            return
        try:
            first = self.lifecycle.record_runtime_exit(node_id, runtime_id)
        except Exception:
            return
        if (not isinstance(first, dict)
                or first.get("code") != "runtime_unverifiable"):
            return

        lifecycle = self.lifecycle

        def _wait_for_exact_exit():
            deadline = time.monotonic() + \
                lifecycle_mod.LAUNCH_STABILITY_WINDOW_S
            while time.monotonic() < deadline:
                time.sleep(0.25)
                try:
                    result = lifecycle.record_runtime_exit(
                        node_id, runtime_id)
                except Exception:
                    return
                if (isinstance(result, dict)
                        and result.get("code") != "runtime_unverifiable"):
                    return

        threading.Thread(
            target=_wait_for_exact_exit,
            name="ConvoyLifecycleExit-" + str(node_id)[:8],
            daemon=True).start()

    def remint_node(self, body):
        node_id = body.get("node_id") or ""
        if self.directory.lookup(node_id) is None:
            return 404, {"ok": False, "reason": "unknown_node",
                         "detail": node_id}
        old_node = self.directory.lookup(node_id)
        if self.lifecycle is not None:
            try:
                self.lifecycle.set_enabled(
                    node_id, old_node["convoy_id"], False,
                    launch_eligible=False)
            except Exception:
                pass
        # A new stable identity inherits no code-execution authority.  Revoke
        # the old identity first; a later persistence error can only become
        # more restrictive, never preserve a stale dangerous grant.
        try:
            self.policy.disable_td_python(node_id)
        except policy_mod.PolicyValidationError as e:
            return 400, {"ok": False, "reason": e.reason,
                         "detail": e.detail}
        try:
            fresh = self.directory.remint(node_id)
        except identity.IdentityError as e:
            return 404, {"ok": False, "reason": e.reason, "detail": e.detail}
        self.db.delete_node(node_id)
        self.db.save_node(fresh)
        self.db.audit("hostapp", "node_reminted",
                      {"old_node_id": node_id,
                       "new_node_id": fresh["node_id"]})
        self._invalidate_network_nodes_cache_locked()
        self.request_lan_refresh()
        return 200, {"ok": True, "node_id": fresh["node_id"],
                     "td_python_approved": False,
                     "policy": self._policy_projection(fresh["node_id"])}

    def list_nodes(self):
        rows = []
        for record in self.directory.nodes():
            row = dict(record)
            row["metadata"] = dict(record.get("metadata") or {})
            row["td_python_approved"] = self.policy.allow_td_python(
                record["node_id"])
            rows.append(row)
        return {"ok": True, "host_id": self.host_id,
                "nodes": rows}

    # -- host-private safety policy (loopback only) ---------------------

    def _policy_projection(self, node_id=None):
        """Return only the policy projection relevant to one local node.

        The persisted list of every TD-Python-approved identity is host-
        private implementation state, not a status payload.  A node learns
        its own grant plus the host-wide shell/quota values and generation.
        """
        state = self.policy.snapshot()
        return {
            "generation": state["generation"],
            "allow_td_python": bool(
                node_id and self.policy.allow_td_python(node_id)),
            "allow_full_shell": state["allow_full_shell"],
            "artifact_quota_mb": state["artifact_quota_mb"],
        }

    @staticmethod
    def _policy_error(exc):
        if isinstance(exc, policy_mod.PolicyValidationError):
            code = 400
        elif isinstance(exc, policy_mod.ChallengeInvalid):
            code = 403
        elif isinstance(exc, policy_mod.ChallengeNotFound):
            code = 404
        elif isinstance(exc, policy_mod.ChallengeExpired):
            code = 410
        elif isinstance(exc, (policy_mod.PolicyConflict,
                              policy_mod.ChallengeStale,
                              policy_mod.PolicyAlreadyEnabled)):
            code = 409
        elif isinstance(exc, policy_mod.PolicyUnreadable):
            code = 503
        else:
            code = 500
        payload = {"ok": False, "reason": exc.reason,
                   "detail": exc.detail}
        current = getattr(exc, "current", None)
        if current is None:
            current = getattr(exc, "current_generation", None)
        if current is not None:
            payload["current_generation"] = current
        return code, payload

    def get_policy(self, node_id=None):
        if node_id is not None:
            try:
                node_id = text_field({"node_id": node_id}, "node_id")
            except Malformed as exc:
                return 400, {"ok": False, "reason": "malformed",
                             "detail": exc.detail}
            if self.directory.lookup(node_id) is None:
                return 404, {"ok": False, "reason": "unknown_node",
                             "detail": node_id}
        return 200, {"ok": True, "host_id": self.host_id,
                     "policy": self._policy_projection(node_id)}

    def begin_policy_challenge(self, body):
        try:
            setting = text_field(body, "setting", limit=32)
            generation = body.get("expected_generation")
            if setting == policy_mod.TD_PYTHON:
                node_id = text_field(body, "node_id")
                node = self.directory.lookup(node_id)
                if node is None:
                    return 404, {"ok": False, "reason": "unknown_node",
                                 "detail": node_id}
                challenge = self.policy.begin_enable_td_python(
                    node_id, expected_generation=generation)
            elif setting == policy_mod.FULL_SHELL:
                challenge = self.policy.begin_enable_full_shell(
                    expected_generation=generation)
            else:
                raise policy_mod.PolicyValidationError(
                    "setting must be td_python or full_shell")
        except Malformed as exc:
            return 400, {"ok": False, "reason": "malformed",
                         "detail": exc.detail}
        except policy_mod.PolicyError as exc:
            return self._policy_error(exc)
        self._audit_best_effort(
            "policy_challenge_started",
            {"setting": challenge["setting"],
             "node_id": challenge.get("node_id"),
             "generation": challenge["generation"]})
        # Loopback/authenticated only.  The confirmation phrase is returned
        # to the local TD modal and is deliberately never written to audit.
        return 200, {"ok": True, "challenge": challenge}

    def confirm_policy_challenge(self, body):
        try:
            challenge_id = text_field(body, "challenge_id", limit=256)
            confirmation = text_field(body, "confirmation", limit=512)
            state = self.policy.confirm_enable(
                challenge_id, confirmation,
                expected_generation=body.get("expected_generation"))
        except Malformed as exc:
            return 400, {"ok": False, "reason": "malformed",
                         "detail": exc.detail}
        except policy_mod.PolicyError as exc:
            self._audit_best_effort(
                "policy_challenge_refused", {"reason": exc.reason})
            return self._policy_error(exc)
        self._audit_best_effort(
            "policy_enabled", {"generation": state["generation"]})
        return 200, {"ok": True, "policy": self._policy_projection()}

    def decline_policy_challenge(self, body):
        try:
            state = self.policy.decline_challenge(
                text_field(body, "challenge_id", limit=256))
        except Malformed as exc:
            return 400, {"ok": False, "reason": "malformed",
                         "detail": exc.detail}
        except policy_mod.PolicyError as exc:
            return self._policy_error(exc)
        self._audit_best_effort(
            "policy_challenge_declined", {"generation": state["generation"]})
        return 200, {"ok": True, "policy": self._policy_projection()}

    def disable_policy(self, body):
        try:
            setting = text_field(body, "setting", limit=32)
            if setting == policy_mod.TD_PYTHON:
                node_id = text_field(body, "node_id")
                state = self.policy.disable_td_python(node_id)
            elif setting == policy_mod.FULL_SHELL:
                node_id = text_field(
                    body, "node_id", required=False) or None
                if node_id is not None and self.directory.lookup(node_id) is None:
                    return 404, {"ok": False, "reason": "unknown_node",
                                 "detail": node_id}
                state = self.policy.disable_full_shell()
            else:
                raise policy_mod.PolicyValidationError(
                    "setting must be td_python or full_shell")
        except Malformed as exc:
            return 400, {"ok": False, "reason": "malformed",
                         "detail": exc.detail}
        except policy_mod.PolicyError as exc:
            return self._policy_error(exc)
        self._audit_best_effort(
            "policy_disabled", {"setting": setting, "node_id": node_id,
                                "generation": state["generation"]})
        return 200, {"ok": True,
                     "policy": self._policy_projection(node_id)}

    def set_artifact_quota(self, body):
        try:
            state = self.policy.set_artifact_quota_mb(
                body.get("artifact_quota_mb"),
                expected_generation=body.get("expected_generation"))
            artifact_status = self.artifacts.set_quota_mb(
                state["artifact_quota_mb"])
        except policy_mod.PolicyError as exc:
            return self._policy_error(exc)
        except artifacts_mod.ArtifactError as exc:
            # The persisted policy remains authoritative and is applied on
            # restart.  Report the partial reconcile explicitly; never claim
            # that a committed host-private setting was rolled back.
            return 500, {"ok": False,
                         "reason": "artifact_quota_reconcile_failed",
                         "detail": exc.detail,
                         "policy": self._policy_projection()}
        self._audit_best_effort(
            "artifact_quota_changed",
            {"generation": state["generation"],
             "artifact_quota_mb": state["artifact_quota_mb"]})
        return 200, {"ok": True, "policy": self._policy_projection(),
                     "artifacts": artifact_status}

    # -- artifact byte transport ----------------------------------------

    @staticmethod
    def _artifact_error(exc):
        """Map store errors without exposing cache paths or exception text."""
        if isinstance(exc, artifacts_mod.ArtifactNotFound):
            code = 404
        elif isinstance(exc, (artifacts_mod.ArtifactQuotaExceeded,
                              artifacts_mod.ArtifactOwnerClaimsExceeded)):
            code = 507
        elif isinstance(exc, artifacts_mod.ArtifactUnauthorized):
            code = 403
        elif isinstance(exc, (artifacts_mod.ArtifactProtected,
                              artifacts_mod.ArtifactExists)):
            code = 409
        elif isinstance(exc, artifacts_mod.ArtifactCorrupt):
            code = 422
        elif isinstance(exc, artifacts_mod.ArtifactValidationError):
            code = 400
        else:
            code = 500
        payload = {"ok": False, "reason": exc.reason}
        if exc.detail:
            payload["detail"] = exc.detail[:256]
        return code, payload

    def export_artifact_to_project(self, body):
        """Explicitly copy one verified cache object into a local project.

        This is a loopback-only convenience boundary.  The caller cannot name
        an arbitrary destination: its real project root must exactly match an
        enabled local node already registered in the requested Convoy.  The
        store remains the sole authority for filename, symlink, overwrite,
        atomic-write, and final content-verification behavior.
        """
        try:
            target_host_id = text_field(body, "target_host_id")
            target_node_id = text_field(body, "target_node_id")
            convoy_id = identity.normalize_convoy_id(
                text_field(body, "convoy_id"))
            project_root = text_field(body, "project_root", limit=4096)
            reference = dict_field(body, "artifact")
            filename = body.get("filename")
            if filename is not None and (
                    not isinstance(filename, str) or not filename
                    or len(filename.encode("utf-8", "strict")) > 255):
                raise Malformed("filename must be bounded non-empty text")
            overwrite = body.get("overwrite", False)
            if not isinstance(overwrite, bool):
                raise Malformed("overwrite must be boolean")
            if (not os.path.isabs(project_root)
                    or any(ord(char) < 32 or ord(char) == 127
                           for char in project_root)):
                raise Malformed("project_root must be an absolute local path")
            requested_root = os.path.realpath(os.path.abspath(project_root))
            if not os.path.isdir(requested_root):
                raise Malformed("project_root is not a local directory")
        except (Malformed, identity.IdentityError, OSError, ValueError) as exc:
            detail = getattr(exc, "detail", str(exc))
            return self._refuse(
                "artifact_export", "malformed", detail, 400)

        # Snapshot the registered roots under the coordination lock, then do
        # every realpath/filesystem operation outside it.
        with self.lock:
            registered_roots = [
                record.get("project_root") for record in self.directory.nodes()
                if (record.get("host_id") == self.host_id
                    and record.get("convoy_id") == convoy_id
                    and record.get("enabled", True) is True
                    and isinstance(record.get("project_root"), str))
            ]
        requested_key = os.path.normcase(requested_root)
        matched = False
        for registered_root in registered_roots:
            try:
                registered_real = os.path.realpath(
                    os.path.abspath(registered_root))
            except (OSError, TypeError, ValueError):
                continue
            if os.path.normcase(registered_real) == requested_key:
                matched = True
                break
        if not matched:
            return self._refuse(
                "artifact_export", "artifact_project_unregistered",
                "project_root is not an enabled local project in this Convoy",
                403, extra={"convoy_id": convoy_id})

        if (reference.get("kind") != "convoy_artifact"
                or reference.get("convoy_id") != convoy_id
                or reference.get("node_id") != target_node_id):
            return self._refuse(
                "artifact_export", "artifact_invalid",
                "artifact reference does not match the requested Convoy/node",
                400, extra={"convoy_id": convoy_id})
        owner = {}
        for name in ("host_id", "node_id", "controller_id", "job_id"):
            value = reference.get(name)
            if value is not None:
                owner[name] = value
        if not all(isinstance(owner.get(name), str) and owner[name]
                   for name in ("host_id", "node_id", "controller_id")):
            return self._refuse(
                "artifact_export", "artifact_invalid",
                "artifact reference omits its exact owner",
                400, extra={"convoy_id": convoy_id})

        try:
            cached = self.artifacts.describe_for_owner(
                convoy_id, reference.get("artifact_id"), owner,
                verify=True, touch=True)
        except artifacts_mod.ArtifactError as exc:
            return self._artifact_error(exc)
        for name in ("artifact_id", "sha256", "size", "mime_type"):
            if (type(reference.get(name)) is not type(cached.get(name))
                    or reference.get(name) != cached.get(name)):
                return 422, {
                    "ok": False, "reason": "artifact_corrupt",
                    "detail": "artifact reference metadata does not match "
                              "the verified local cache",
                }

        if not self.begin_artifact_transfer():
            return 429, {"ok": False, "reason": "artifact_transfer_busy",
                         "wakes_touchdesigner": False}
        try:
            saved = self.artifacts.export_to_project(
                requested_root, convoy_id, cached["artifact_id"],
                filename=filename, overwrite=overwrite)
        except artifacts_mod.ArtifactError as exc:
            return self._artifact_error(exc)
        finally:
            self.end_artifact_transfer()
        self._audit_best_effort(
            "artifact_exported",
            {"convoy_id": convoy_id, "artifact_id": cached["artifact_id"],
             "size": cached["size"], "overwrite": overwrite})
        return 200, {
            "ok": True, "artifact": saved,
            "target_host_id": target_host_id,
            "target_node_id": target_node_id,
            "convoy_id": convoy_id,
            "wakes_touchdesigner": False,
        }

    def begin_artifact_transfer(self):
        """Non-blocking shared loopback/LAN transfer admission."""
        return self.artifact_transfer_slots.acquire(blocking=False)

    def end_artifact_transfer(self):
        self.artifact_transfer_slots.release()

    def _artifact_subject(self, convoy_id, node_id, controller_id, *,
                          peer_host_id=None, peer_fingerprint=None,
                          mutating=False):
        """Resolve the exact namespace/node/controller authorization.

        This is a second authorization inside HostApp, after the LAN handler's
        request gate, so a concurrent block/re-pin cannot win a check/use race.
        It self-locks and must not be called while ``self.lock`` is held.
        """
        try:
            convoy_id = identity.normalize_convoy_id(convoy_id)
            node_id = text_field({"node_id": node_id}, "node_id")
            controller_id = text_field(
                {"controller_id": controller_id}, "controller_id")
        except (identity.IdentityError, Malformed) as exc:
            detail = getattr(exc, "detail", str(exc))
            return None, (400, {"ok": False, "reason": "artifact_invalid",
                                "detail": detail[:256]})
        with self.lock:
            if peer_host_id is not None:
                decision = self.peers.authorize_peer(
                    peer_host_id, peer_fingerprint, convoy_id=convoy_id)
                if not decision.allowed or mutating and not decision.may_mutate:
                    return None, (403, {
                        "ok": False,
                        "reason": decision.reason or "peer_not_authorized",
                        "detail": decision.detail,
                    })
            node = self.directory.lookup(node_id)
            if (node is None or node.get("convoy_id") != convoy_id
                    or not bool(node.get("enabled", True))):
                # A peer gets one non-oracular answer for absent, disabled and
                # cross-namespace nodes.  A content hash plus node guessing is
                # not an inventory API.
                reason = ("artifact_scope_not_found" if peer_host_id
                          is not None else "artifact_node_unavailable")
                return None, (404, {"ok": False, "reason": reason})
            realm_refusal = self._realm_operation_refusal(convoy_id)
            if realm_refusal is not None:
                reason, detail = realm_refusal
                return None, (409, {"ok": False, "reason": reason,
                                    "detail": detail})
            if peer_host_id is not None:
                controller_id = peers_mod.namespaced_controller(
                    peer_host_id, controller_id)
                self._note_peer_controller(peer_host_id, controller_id)
                owner_host_id = peer_host_id
            else:
                owner_host_id = self.host_id
            return {
                "convoy_id": convoy_id,
                "node_id": node_id,
                "controller_id": controller_id,
                "host_id": owner_host_id,
            }, None

    def artifact_upload(self, convoy_id, stream, metadata, *,
                        peer_host_id=None, peer_fingerprint=None):
        """Consume and hash one already-bounded raw HTTP request body."""
        subject, refusal = self._artifact_subject(
            convoy_id, metadata.get("node_id"),
            metadata.get("controller_id"), peer_host_id=peer_host_id,
            peer_fingerprint=peer_fingerprint,
            mutating=peer_host_id is not None)
        if refusal is not None:
            return refusal
        try:
            reference = self.artifacts.put_stream(
                subject["convoy_id"], stream,
                expected_size=metadata.get("expected_size"),
                expected_sha256=metadata.get("expected_sha256"),
                mime_type=metadata.get("mime_type")
                or "application/octet-stream",
                filename_hint=metadata.get("filename_hint"),
                owner={"host_id": subject["host_id"],
                       "node_id": subject["node_id"],
                       "controller_id": subject["controller_id"]},
                # Never trust a network claim merely because its digest is
                # already cached; consume and verify duplicate bodies too.
                verify_existing_stream=True)
        except artifacts_mod.ArtifactError as exc:
            return self._artifact_error(exc)
        self._audit_best_effort(
            "artifact_uploaded",
            {"artifact_id": reference["artifact_id"],
             "convoy_id": subject["convoy_id"],
             "node_id": subject["node_id"],
             "origin_host_id": subject["host_id"],
             "size": reference["size"]})
        return 200, {"ok": True, "artifact": reference}

    def artifact_local_grant(self, convoy_id, artifact_id, body):
        """IPC-only: explicitly grant one admitted peer a one-shot read."""
        try:
            peer_host_id = text_field(body, "peer_host_id")
            node_id = text_field(body, "node_id")
            controller_id = text_field(body, "controller_id")
        except Malformed as exc:
            return 400, {"ok": False, "reason": "artifact_invalid",
                         "detail": exc.detail}
        with self.lock:
            fingerprint = self.peers.pinned_fingerprint(peer_host_id)
        subject, refusal = self._artifact_subject(
            convoy_id, node_id, controller_id,
            peer_host_id=peer_host_id, peer_fingerprint=fingerprint)
        if refusal is not None:
            return refusal
        audience = artifact_http.peer_audience(
            peer_host_id, subject["convoy_id"], subject["node_id"],
            subject["controller_id"])
        try:
            kwargs = {"audience": audience}
            if "ttl_s" in body:
                kwargs["ttl_s"] = body.get("ttl_s")
            capability = self.artifacts.issue_download_capability(
                subject["convoy_id"], artifact_id, **kwargs)
        except artifacts_mod.ArtifactError as exc:
            return self._artifact_error(exc)
        self._audit_best_effort(
            "artifact_capability_issued",
            {"artifact_id": artifact_id, "convoy_id": subject["convoy_id"],
             "peer_host_id": peer_host_id, "node_id": subject["node_id"]})
        return 200, {"ok": True, "capability": capability}

    def artifact_peer_grant(self, convoy_id, artifact_id, body, *,
                            peer_host_id, peer_fingerprint):
        """Let a peer re-mint a resume token only for its exact artifact."""
        try:
            node_id = text_field(body, "node_id")
            controller_id = text_field(body, "controller_id")
            job_id = text_field(body, "job_id", required=False)
        except Malformed as exc:
            return 400, {"ok": False, "reason": "artifact_invalid",
                         "detail": exc.detail}
        subject, refusal = self._artifact_subject(
            convoy_id, node_id, controller_id,
            peer_host_id=peer_host_id, peer_fingerprint=peer_fingerprint)
        if refusal is not None:
            return refusal
        expected_owner = {
            "host_id": peer_host_id,
            "node_id": subject["node_id"],
            "controller_id": subject["controller_id"],
        }
        if job_id:
            expected_owner["job_id"] = job_id
        try:
            # Exact claim lookup is both authorization and non-disclosure:
            # no other peer/job claim is projected or iterated here.
            self.artifacts.describe_for_owner(
                subject["convoy_id"], artifact_id, expected_owner)
        except artifacts_mod.ArtifactNotFound:
            return 404, {"ok": False, "reason": "artifact_scope_not_found"}
        except artifacts_mod.ArtifactError as exc:
            return self._artifact_error(exc)
        audience = artifact_http.peer_audience(
            peer_host_id, subject["convoy_id"], subject["node_id"],
            subject["controller_id"])
        try:
            kwargs = {"audience": audience}
            if "ttl_s" in body:
                kwargs["ttl_s"] = body.get("ttl_s")
            capability = self.artifacts.issue_download_capability(
                subject["convoy_id"], artifact_id, **kwargs)
        except artifacts_mod.ArtifactError as exc:
            return self._artifact_error(exc)
        self._audit_best_effort(
            "artifact_capability_issued",
            {"artifact_id": artifact_id, "convoy_id": subject["convoy_id"],
             "peer_host_id": peer_host_id, "node_id": subject["node_id"]})
        return 200, {"ok": True, "capability": capability}

    @staticmethod
    def _artifact_download_headers(lease, partial):
        try:
            mime_type = lease.mime_type.encode("ascii").decode("ascii")
        except (AttributeError, UnicodeEncodeError):
            mime_type = "application/octet-stream"
        headers = {
            "Content-Type": mime_type,
            "Content-Length": str(lease.length),
            "Accept-Ranges": "bytes",
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
            "ETag": '"sha256-%s"' % lease.sha256,
            "X-Convoy-Artifact-ID": lease.artifact_id,
            "X-Convoy-Content-SHA256": lease.sha256,
        }
        if partial:
            end = lease.offset + lease.length - 1
            headers["Content-Range"] = (
                f"bytes {lease.offset}-{end}/{lease.total_size}")
        return headers

    def artifact_open_local_download(self, convoy_id, artifact_id,
                                     range_header=None):
        """Open a token-authenticated loopback download."""
        try:
            reference = self.artifacts.describe(convoy_id, artifact_id)
            offset, length, partial = artifact_http.parse_range(
                range_header, reference["size"])
            audience = "artifact-local-v1-" + self.host_id
            capability = self.artifacts.issue_download_capability(
                convoy_id, artifact_id, audience=audience)
            lease = self.artifacts.open_download(
                convoy_id, artifact_id, token=capability["token"],
                audience=audience, offset=offset, length=length)
        except artifact_http.ArtifactHTTPError as exc:
            return exc.status, exc.payload(), None, exc.headers
        except artifacts_mod.ArtifactError as exc:
            code, payload = self._artifact_error(exc)
            return code, payload, None, {}
        return ((206 if partial else 200), None, lease,
                self._artifact_download_headers(lease, partial))

    def artifact_open_peer_download(self, convoy_id, artifact_id, token,
                                    node_id, controller_id, range_header=None,
                                    *, peer_host_id, peer_fingerprint):
        """Open an mTLS + namespace + node + controller + token download."""
        subject, refusal = self._artifact_subject(
            convoy_id, node_id, controller_id,
            peer_host_id=peer_host_id, peer_fingerprint=peer_fingerprint)
        if refusal is not None:
            return refusal[0], refusal[1], None, {}
        audience = artifact_http.peer_audience(
            peer_host_id, subject["convoy_id"], subject["node_id"],
            subject["controller_id"])
        try:
            reference = self.artifacts.describe_download(
                subject["convoy_id"], artifact_id, token=token,
                audience=audience)
            offset, length, partial = artifact_http.parse_range(
                range_header, reference["size"])
            lease = self.artifacts.open_download(
                subject["convoy_id"], artifact_id, token=token,
                audience=audience, offset=offset, length=length)
        except artifact_http.ArtifactHTTPError as exc:
            return exc.status, exc.payload(), None, exc.headers
        except artifacts_mod.ArtifactError as exc:
            code, payload = self._artifact_error(exc)
            return code, payload, None, {}
        return ((206 if partial else 200), None, lease,
                self._artifact_download_headers(lease, partial))

    @staticmethod
    def _public_text(value, limit):
        """Return bounded descriptive text, never an arbitrary object."""
        if not isinstance(value, str):
            return ""
        return value[:limit]

    def _node_is_online(self, record):
        routable = bool(record.get("envoy_port")) or bool(
            record.get("remote_wake") and record.get("wake_port")
            and record.get("wake_token"))
        if not routable or not record.get("runtime_id"):
            return False
        try:
            age = float(self._now()) - float(record["last_heartbeat_unix"])
        except (KeyError, TypeError, ValueError):
            return False
        return (math.isfinite(age) and 0.0 <= age
                and age <= NODE_HEARTBEAT_GRACE_S)

    def _public_node_row(self, record, address=None, status=None,
                         controller_count=0):
        """The only node projection permitted across the LAN boundary.

        Identity/routing fields and useful display/version metadata are
        included.  Host-private paths, the local Envoy port, capability
        approvals, and every unknown future field are deliberately omitted.
        """
        metadata = record.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
        online = self._node_is_online(record)
        status = status or ("online" if online else "offline")
        try:
            last_seen_age_s = max(
                0.0, float(self._now())
                - float(record["last_heartbeat_unix"]))
            if not math.isfinite(last_seen_age_s):
                last_seen_age_s = None
        except (KeyError, TypeError, ValueError, OverflowError):
            last_seen_age_s = None
        if last_seen_age_s is None:
            # The process-local beat dies with the daemon; the durable
            # stamp survives it. Without this every row read "Unavailable"
            # after a daemon restart until its TD re-registered.
            try:
                seen = self.db.node_last_seen(record.get("node_id"))
                if seen is not None:
                    last_seen_age_s = max(0.0, float(self._now())
                                          - float(seen))
                    if not math.isfinite(last_seen_age_s):
                        last_seen_age_s = None
            except Exception:
                last_seen_age_s = None
        if (isinstance(controller_count, bool)
                or not isinstance(controller_count, int)
                or controller_count < 0):
            controller_count = 0
        launchable = False
        if self.lifecycle is not None:
            try:
                profile = self.lifecycle.store.get_profile(
                    record.get("node_id"))
                launchable = bool(
                    profile and profile.get("enabled") is True
                    and profile.get("launch_eligible") is True)
            except Exception:
                launchable = False
        return {
            "node_id": record.get("node_id"),
            "host_id": self.host_id,
            "convoy_id": record.get("convoy_id"),
            "runtime_id": self._public_text(record.get("runtime_id"), 128),
            "node_name": self._public_text(metadata.get("node_name"), 256),
            "hostname": self._public_text(metadata.get("hostname"), 255),
            "toe_name": self._public_text(metadata.get("toe_name"), 256),
            "embody_version": self._public_text(
                metadata.get("embody_version"), 64),
            "touchdesigner_version": self._public_text(
                metadata.get("touchdesigner_version"), 64),
            "ip": self._public_text(address, 255),
            "status": status,
            "online": online if status == "online" else False,
            "enabled": bool(record.get("enabled", True)),
            "perform_mode": bool(record.get("perform_mode")),
            "wake_active": bool(record.get("wake_active")),
            "sleeping": bool(record.get("perform_mode")
                             and not record.get("wake_active")),
            "remotely_launchable": launchable,
            "last_seen_age_s": last_seen_age_s,
            "controller_count": min(
                controller_count, MAX_PUBLIC_CONTROLLERS_PER_HOST),
        }

    def _controller_counts_locked(self, convoy_id=None):
        """Coalesce live controller counts without per-node I/O.

        A passive read: it reconciles claims only behind the read TTL so a
        burst of directory/status callers cannot each drive one durable
        job-file read per live claim under the app lock.
        """
        self._reconcile_operation_claims_if_stale_locked()
        allowed = {
            row.get("node_id") for row in self.directory.nodes()
            if bool(row.get("enabled", True))
            and (convoy_id is None or row.get("convoy_id") == convoy_id)
        }
        counts = {node_id: set() for node_id in allowed if node_id}
        for controller in self.leases.live_controllers(self._now()):
            controller_id = controller.get("controller_id")
            if not isinstance(controller_id, str):
                continue
            node_ids = set(controller.get("node_ids") or ())
            selected = controller.get("selected_node_id")
            if isinstance(selected, str):
                node_ids.add(selected)
            for node_id in node_ids.intersection(allowed):
                counts[node_id].add(controller_id)
        return {node_id: len(controllers_) for node_id, controllers_
                in counts.items()}

    @staticmethod
    def _sanitize_peer_node(row, target, convoy_id, fallback_status=None):
        """Validate one untrusted peer row and override its trust anchors.

        A peer may describe its node, but it may not claim another host,
        namespace or network address.  Those three values come exclusively
        from this host's pinned admission record and the request namespace.
        """
        if not isinstance(row, dict):
            return None
        node_id = row.get("node_id")
        if not identity.is_valid_id(node_id):
            return None
        raw_status = row.get("status")
        status = (raw_status if raw_status in ("online", "offline", "error")
                  else fallback_status or "error")

        def clean(name, limit):
            value = row.get(name)
            return value[:limit] if isinstance(value, str) else ""

        controller_count = row.get("controller_count", 0)
        if (isinstance(controller_count, bool)
                or not isinstance(controller_count, int)
                or not 0 <= controller_count
                <= MAX_PUBLIC_CONTROLLERS_PER_HOST):
            controller_count = 0
        last_seen_age_s = row.get("last_seen_age_s")
        if (isinstance(last_seen_age_s, bool)
                or not isinstance(last_seen_age_s, (int, float))
                or not math.isfinite(float(last_seen_age_s))
                or float(last_seen_age_s) < 0):
            last_seen_age_s = None
        elif last_seen_age_s is not None:
            last_seen_age_s = float(last_seen_age_s)

        return {
            "node_id": node_id,
            "host_id": target.host_id,
            "convoy_id": convoy_id,
            "runtime_id": clean("runtime_id", 128),
            "node_name": clean("node_name", 256),
            "hostname": clean("hostname", 255),
            "toe_name": clean("toe_name", 256),
            "embody_version": clean("embody_version", 64),
            "touchdesigner_version": clean("touchdesigner_version", 64),
            "ip": str(target.address)[:255],
            "status": status,
            "online": bool(row.get("online")) and status == "online",
            "perform_mode": row.get("perform_mode") is True,
            "wake_active": row.get("wake_active") is True,
            "sleeping": row.get("sleeping") is True,
            "remotely_launchable": row.get("remotely_launchable") is True,
            # A peer endpoint publishes only participating nodes.  Do not
            # let an untrusted row invert that local policy bit.
            "enabled": True,
            "controller_count": controller_count,
            "last_seen_age_s": last_seen_age_s,
        }

    def peer_nodes_view(self, origin_host_id, convoy_id,
                        authenticated_fingerprint=None):
        """Peer-safe directory for exactly one admitted namespace.

        Called by convoy_peerserver after mutual-TLS authentication, without
        the app lock.  Re-authorizing here closes the small race in which an
        operator revokes a peer between the handler's check and this snapshot.
        """
        try:
            convoy_id = identity.normalize_convoy_id(convoy_id)
        except identity.IdentityError:
            return 400, {"ok": False, "reason": "malformed"}
        with self.lock:
            record = self.peers.get(origin_host_id)
            decision = self.peers.authorize_peer(
                origin_host_id,
                # Re-check the key that authenticated THIS connection, not
                # the current stored pin.  If the operator re-pins between
                # the peer handler's gate and this snapshot, an old TLS
                # connection must not be laundered through the new pin.
                authenticated_fingerprint,
                convoy_id=convoy_id)
            if not decision.allowed:
                return _REFUSAL_HTTP.get(decision.reason, 403), {
                    "ok": False, "reason": decision.reason,
                    "detail": decision.detail,
                }
            realm_refusal = self._realm_operation_refusal(convoy_id)
            if realm_refusal is not None:
                reason, detail = realm_refusal
                return 409, {"ok": False, "reason": reason,
                             "detail": detail}
            address = self.lan_address or ""
            controller_counts = self._controller_counts_locked(convoy_id)
            rows = [
                self._public_node_row(
                    node, address=address,
                    controller_count=controller_counts.get(
                        node.get("node_id"), 0))
                for node in self.directory.nodes()
                if node.get("convoy_id") == convoy_id
                and bool(node.get("enabled", True))
            ]
        if len(rows) > MAX_PUBLIC_NODES_PER_HOST:
            return 503, {
                "ok": False,
                "reason": "node_directory_too_large",
                "detail": "peer directory exceeds %d nodes"
                          % MAX_PUBLIC_NODES_PER_HOST,
            }
        return 200, {"ok": True, "host_id": self.host_id,
                     "convoy_id": convoy_id, "nodes": rows}

    def network_nodes(self, convoy_id=None):
        """Return a cached/coalesced local-and-peer node directory.

        Exactly one caller per namespace performs a refresh.  Followers share
        its result, while unrelated namespaces still make progress through a
        single host-wide bounded executor.  This keeps sixteen UI/tool callers
        from turning one 29-peer status wave into 464 simultaneous requests.

        The generation fence is applied to the RETURNED result, not only to
        the cache write: a membership mutation (a /register) that lands during
        a flight supersedes that flight's projection, so leader and followers
        recompute rather than hand back a directory that predates the caller's
        own write.  This is read-your-own-write for ConvoyExt, which registers
        and then immediately reads the directory in the same worker turn.
        """
        if convoy_id is not None:
            try:
                convoy_id = identity.normalize_convoy_id(convoy_id)
            except identity.IdentityError:
                return 400, {"ok": False, "reason": "malformed",
                             "detail": "convoy_id must be canonical bounded text"}

        cache_key = convoy_id
        for _attempt in range(NETWORK_NODE_MAX_REFRESH_ATTEMPTS):
            now = self._network_nodes_cache_clock()
            with self.lock:
                cached = self._network_nodes_result_cache.get(cache_key)
                if (cached is not None
                        and now - cached[0] < NETWORK_NODE_CACHE_TTL_S):
                    self._network_query_metrics["cache_hits"] += 1
                    return copy.deepcopy(cached[1])

                flight = self._network_nodes_flights.get(cache_key)
                if flight is None:
                    flight = {
                        "event": threading.Event(),
                        "generation": self._network_nodes_cache_generation,
                        "result": None,
                    }
                    self._network_nodes_flights[cache_key] = flight
                    self._network_query_metrics["refreshes"] += 1
                    leader = True
                else:
                    self._network_query_metrics["coalesced"] += 1
                    leader = False

            if not leader:
                if flight["event"].wait(NETWORK_NODE_FLIGHT_WAIT_S):
                    result = flight.get("result")
                    if result is not None:
                        with self.lock:
                            superseded = (
                                result[0] == 200
                                and flight["generation"]
                                != self._network_nodes_cache_generation)
                        if not superseded:
                            return copy.deepcopy(result)
                        # A membership mutation landed during this flight; its
                        # projection predates the caller's own write.  Retry
                        # for a fresh read rather than return a stale directory.
                        continue
                with self.lock:
                    self._network_query_metrics["wait_timeouts"] += 1
                    stale = self._network_nodes_result_cache.get(cache_key)
                if stale is not None:
                    result = copy.deepcopy(stale[1])
                    if result[0] == 200 and isinstance(result[1], dict):
                        result[1]["stale"] = True
                        result[1]["refresh_in_progress"] = True
                    return result
                return 503, {
                    "ok": False,
                    "reason": "network_query_busy",
                    "detail": "another directory refresh exceeded its bounded "
                              "wait; retry without starting a duplicate fanout",
                    "wakes_touchdesigner": False,
                }

            try:
                result = self._network_nodes_uncached(convoy_id)
            except Exception:
                result = (503, {
                    "ok": False,
                    "reason": "network_query_failed",
                    "detail": "the shared directory refresh failed",
                    "wakes_touchdesigner": False,
                })
                with self.lock:
                    flight["result"] = copy.deepcopy(result)
                    if self._network_nodes_flights.get(cache_key) is flight:
                        del self._network_nodes_flights[cache_key]
                    flight["event"].set()
                raise

            with self.lock:
                superseded = (
                    result[0] == 200
                    and flight["generation"]
                    != self._network_nodes_cache_generation)
                flight["result"] = copy.deepcopy(result)
                # Only a projection that still matches the live generation may
                # be published as authoritative -- to the cache OR to callers.
                if result[0] == 200 and not superseded:
                    self._network_nodes_result_cache[cache_key] = (
                        self._network_nodes_cache_clock(),
                        copy.deepcopy(result))
                if self._network_nodes_flights.get(cache_key) is flight:
                    del self._network_nodes_flights[cache_key]
                flight["event"].set()
            if not superseded:
                return result
            # The leader's own projection predates a mutation that landed
            # mid-flight; recompute rather than return it as authoritative.
            continue

        # Sustained membership churn outlasted the bounded retries: return the
        # freshest projection available, marked stale, rather than loop.
        with self.lock:
            stale = self._network_nodes_result_cache.get(cache_key)
        if stale is not None:
            result = copy.deepcopy(stale[1])
            if result[0] == 200 and isinstance(result[1], dict):
                result[1]["stale"] = True
            return result
        return 503, {
            "ok": False,
            "reason": "network_query_busy",
            "detail": "the directory kept changing under repeated refreshes; "
                      "retry",
            "wakes_touchdesigner": False,
        }

    def _network_nodes_uncached(self, convoy_id=None):
        """Aggregate local and reachable peer nodes for the loopback API.

        The mutable registry is snapshotted under the lock; all mutual-TLS
        I/O runs concurrently after releasing it.  At 30 hosts this keeps a
        status refresh near one network timeout rather than thirty stacked
        timeouts, and leaves registration/dispatch responsive throughout.
        """
        if convoy_id is not None:
            try:
                convoy_id = identity.normalize_convoy_id(convoy_id)
            except identity.IdentityError:
                return 400, {"ok": False, "reason": "malformed",
                             "detail": "convoy_id must be canonical bounded text"}

        with self.lock:
            local_records = list(self.directory.nodes())
            peer_records = list(self.peers.peers())
            keys = self.hostkeys
            local_address = self.lan_address or "127.0.0.1"
            active_namespaces = set(self._active_convoy_ids_locked())
            if convoy_id is not None and convoy_id not in active_namespaces:
                # This is authenticated loopback status, not LAN exposure.
                # A disabled/offline final node still needs to project its
                # retained profile. Refuse only a different known realm;
                # an entirely unbound host returns an honest empty view.
                snapshot = self.realm.snapshot()
                if (snapshot and snapshot.get("convoy_id")
                        and snapshot.get("convoy_id") != convoy_id):
                    return 409, {
                        "ok": False,
                        "reason": "realm_namespace_mismatch",
                        "detail": "the requested Convoy is not this host's "
                                  "automatic realm",
                    }
            namespaces = ({convoy_id} if convoy_id is not None
                          else active_namespaces)

            controller_counts = self._controller_counts_locked(convoy_id)

            local_rows = []
            for record in local_records:
                if not bool(record.get("enabled", True)):
                    continue
                if convoy_id is not None and \
                        record.get("convoy_id") != convoy_id:
                    continue
                row = self._public_node_row(
                    record, address=local_address,
                    controller_count=controller_counts.get(
                        record.get("node_id"), 0))
                row["compatibility"] = "compatible"
                # Loopback-only capability projection, LOCAL rows only --
                # _public_node_row deliberately never crosses the LAN with
                # capability approvals (target-marking), so peer rows carry
                # no `capabilities` key: absent = unknown, never allowed.
                # Lets a controller see a shut gate on call #1 instead of
                # planning around a blocked mechanism (field 2026-08-19).
                try:
                    row["capabilities"] = {
                        "td_python": bool(self.policy.allow_td_python(
                            record.get("node_id"))),
                        "full_shell": self.policy.allow_full_shell() is True,
                    }
                except Exception:
                    pass
                local_rows.append(row)
            queries = []
            refused = []
            for peer in peer_records:
                peer_host_id = peer.get("host_id")
                for namespace in sorted(namespaces):
                    decision = self.peers.authorize_peer(
                        peer_host_id, peer.get("fingerprint"),
                        convoy_id=namespace)
                    if not decision.allowed:
                        # Only report records that actually claim this
                        # namespace; an unrelated peer is not an error.
                        if namespace in (peer.get("convoy_ids") or ()):
                            refused.append({
                                "host_id": peer_host_id,
                                "convoy_id": namespace,
                                "status": "error",
                                "reason": decision.reason,
                            })
                        continue
                    targets, error = self._peer_targets_from_record(peer)
                    projection = (targets[0] if targets else
                                  _PeerProjectionTarget(peer_host_id))
                    # An inbound-established WSS link is fully routable even
                    # when discovery has not supplied a dial endpoint.  Keep
                    # that peer in the query set and decide fallback only
                    # after the WSS preflight below.
                    queries.append((projection, targets, namespace, error))
            cached_peer_rows = {key: [dict(row) for row in rows]
                                for key, rows in
                                self._peer_node_cache.items()}

        peer_status = [{"host_id": self.host_id,
                        "convoy_id": convoy_id,
                        "status": "online", "ip": local_address,
                        "local": True, "compatibility": "compatible"}]
        peer_status.extend(refused)
        remote_rows = []
        cache_updates = {}

        def fetch(item):
            projection, targets, namespace, target_error = item
            used_session, session_result = self._session_call_if_connected(
                projection.host_id, namespace,
                peerserver.SESSION_RPC_NODES, {},
                NETWORK_QUERY_TIMEOUT_S)
            if used_session:
                return (projection, namespace), session_result
            if not targets:
                return (projection, namespace), {
                    "ok": False, "reason": "peer_endpoint_unknown",
                    "detail": target_error,
                }
            if keys is None:
                return (projection, namespace), None
            self._audit_http_compat_fallback(
                projection.host_id, peerserver.SESSION_RPC_NODES)
            target, result = self._call_peer_targets(
                targets,
                lambda candidate, remaining: peerclient.get_peer_nodes(
                    candidate, keys, namespace, timeout=remaining,
                    pool=self.peer_pool),
                NETWORK_QUERY_TIMEOUT_S,
                retry_ambiguous=True)
            return (target, namespace), result

        outcomes = self._fanout(
            queries, fetch,
            on_error=lambda item: ((item[0], item[2]), None))

        for (target, namespace), result in outcomes:
            cache_key = (target.host_id, namespace)
            compatibility = self._peer_compatibility_projection(
                target.host_id)
            if result is peerclient.UNREACHABLE:
                status, reason = "offline", "peer_unreachable"
            elif isinstance(result, peerclient._PinMismatch):
                status, reason = "error", "pin_mismatch"
            elif result is None:
                status = "error"
                reason = ("identity_unavailable" if keys is None
                          else "peer_bad_response")
            elif not isinstance(result, dict) or not result.get("ok"):
                status, reason = "error", (
                    result.get("reason", "peer_refused")
                    if isinstance(result, dict) else "peer_bad_response")
            else:
                status, reason = "online", None

            entry = {"host_id": target.host_id, "convoy_id": namespace,
                     "status": status, "ip": str(target.address)[:255]}
            if reason:
                entry["reason"] = reason
            if compatibility is not None:
                entry["compatibility"] = compatibility
            peer_status.append(entry)

            if status == "online":
                clean_rows = []
                if (result.get("convoy_id") == namespace
                        and isinstance(result.get("nodes"), list)):
                    for row in result["nodes"][:MAX_PUBLIC_NODES_PER_HOST]:
                        clean = self._sanitize_peer_node(
                            row, target, namespace)
                        if clean is not None:
                            if compatibility is not None:
                                clean["compatibility"] = compatibility
                            clean_rows.append(clean)
                cached_rows = []
                cached_at = self._now()
                for clean in clean_rows:
                    cached_row = dict(clean)
                    cached_row["_cached_at"] = cached_at
                    cached_rows.append(cached_row)
                cache_updates[cache_key] = cached_rows
                remote_rows.extend(clean_rows)
            else:
                for row in cached_peer_rows.get(cache_key, ()):
                    stale = dict(row)
                    cached_at = stale.pop("_cached_at", None)
                    age = stale.get("last_seen_age_s")
                    try:
                        elapsed = max(0.0, self._now() - float(cached_at))
                        if age is not None:
                            stale["last_seen_age_s"] = float(age) + elapsed
                    except (TypeError, ValueError, OverflowError):
                        stale["last_seen_age_s"] = None
                    stale["status"] = status
                    stale["online"] = False
                    stale.pop("compatibility", None)
                    remote_rows.append(stale)

        if cache_updates:
            with self.lock:
                self._peer_node_cache.update(cache_updates)

        # A node is uniquely addressed by (host_id, node_id).  Keep local
        # rows first, then one deterministic remote row per address.
        deduped = {}
        for row in local_rows + remote_rows:
            key = (row.get("host_id"), row.get("node_id"))
            if key not in deduped:
                deduped[key] = row
            if len(deduped) >= MAX_NETWORK_NODE_ROWS:
                break
        rows = sorted(deduped.values(), key=lambda row: (
            row.get("node_name") or row.get("hostname") or "",
            row.get("host_id") or "", row.get("node_id") or ""))
        return 200, {
            "ok": True,
            "host_id": self.host_id,
            "convoy_id": convoy_id,
            "nodes": rows,
            "peers": peer_status,
            "remote_nodes_available": keys is not None,
            "truncated": len(deduped) >= MAX_NETWORK_NODE_ROWS,
        }

    def shutdown_network_queries(self, wait=True):
        """Release the process-wide directory workers during final teardown."""
        executor = getattr(self, "_network_query_executor", None)
        if executor is not None:
            executor.shutdown(wait=bool(wait), cancel_futures=not bool(wait))

    def _fanout(self, queries, fetch, on_error):
        """Run peer fetches on the ONE host-wide bounded query pool.

        Both passive network directory reads (``network_nodes`` and
        ``network_controllers``) funnel their fanout through here.  A new
        ``ThreadPoolExecutor`` per request would let simultaneous status
        callers multiply the NETWORK_QUERY_WORKERS bound and stack N*peers
        concurrent peer TLS calls; the shared executor makes that bound true
        for the whole process.  ``on_error`` maps one query item to the
        outcome recorded when its future raises, so each caller keeps its
        own outcome shape.
        """
        outcomes = []
        if not queries:
            return outcomes
        future_map = {self._network_query_executor.submit(fetch, item): item
                      for item in queries}
        for future, item in list(future_map.items()):
            try:
                outcomes.append(future.result())
            except Exception:
                outcomes.append(on_error(item))
        return outcomes

    def _local_controller_view(self, convoy_id, node_id=None):
        """Build this host's bounded, non-waking controller projection.

        Mutable lease state is snapshotted under the app lock. Durable jobs
        are scanned after releasing it so a large retained job directory can
        never stall registration or dispatch. Only active job summaries are
        exposed; arguments and results remain private.
        """
        with self.lock:
            now = self._now()
            # Passive read: reconcile behind the TTL, never one job-file read
            # per claim on every call (the slice-2 status/peers regression).
            self._reconcile_operation_claims_if_stale_locked()
            allowed_nodes = {
                row["node_id"] for row in self.directory.nodes()
                if row.get("convoy_id") == convoy_id
                and bool(row.get("enabled", True))
            }
            raw_controllers = [dict(row) for row in
                               self.leases.live_controllers(now)]
        if node_id is not None:
            allowed_nodes.intersection_update({node_id})
        try:
            jobs, unreadable = self.db.scan_jobs()
        except Exception:
            jobs, unreadable = [], 1

        grouped = {}
        for raw in raw_controllers[:MAX_PUBLIC_CONTROLLERS_PER_HOST]:
            controller_id = raw.get("controller_id")
            if not isinstance(controller_id, str) or not controller_id:
                continue
            selected = raw.get("selected_node_id")
            leases = [dict(lease) for lease in (raw.get("leases") or ())
                      if isinstance(lease, dict)
                      and lease.get("node_id") in allowed_nodes]
            if selected not in allowed_nodes:
                selected = None
            if not leases and selected is None:
                continue
            last_seen = raw.get("last_seen")
            try:
                age = max(0.0, now - float(last_seen))
            except (TypeError, ValueError):
                age = None
            grouped[controller_id] = {
                "controller_id": controller_id[:MAX_ID_CHARS],
                "label": str(raw.get("label") or "")[:128],
                "selected_node_id": selected,
                "last_seen_age_s": age,
                "leases": leases[:MAX_PUBLIC_NODES_PER_HOST],
                "node_ids": sorted({
                    lease.get("node_id") for lease in leases
                    if lease.get("node_id")
                } | ({selected} if selected else set())),
                "active_jobs": [],
                "origin_host_ids": [],
            }

        for job in jobs:
            if (not isinstance(job, dict)
                    or job.get("convoy_id") != convoy_id
                    or job.get("state") in hoststore.TERMINAL_STATES
                    or job.get("node_id") not in allowed_nodes):
                continue
            controller_id = job.get("controller_id")
            if not isinstance(controller_id, str) or not controller_id:
                continue
            row = grouped.setdefault(controller_id, {
                "controller_id": controller_id[:MAX_ID_CHARS],
                "label": "", "selected_node_id": job.get("node_id"),
                "last_seen_age_s": None, "leases": [],
                "node_ids": [], "active_jobs": [],
                "origin_host_ids": [],
            })
            if len(row["active_jobs"]) < MAX_ACTIVE_JOBS_PER_CONTROLLER:
                row["active_jobs"].append({
                    "delivery_id": str(job.get("delivery_id") or "")[:128],
                    "node_id": str(job.get("node_id") or "")[:MAX_ID_CHARS],
                    "operation": str(job.get("operation") or "")[:128],
                    "state": str(job.get("state") or "")[:32],
                })
            if job.get("node_id") and job["node_id"] not in row["node_ids"]:
                row["node_ids"].append(job["node_id"])
            origin = job.get("origin_host_id")
            if (isinstance(origin, str) and origin
                    and origin not in row["origin_host_ids"]):
                row["origin_host_ids"].append(origin[:MAX_ID_CHARS])

        rows = []
        for controller_id in sorted(grouped):
            row = grouped[controller_id]
            row["host_id"] = self.host_id
            row["convoy_id"] = convoy_id
            row["node_ids"] = sorted(set(row["node_ids"]))
            row["origin_host_ids"] = sorted(set(row["origin_host_ids"]))
            row["active_jobs"].sort(key=lambda item: (
                item.get("node_id") or "", item.get("delivery_id") or ""))
            rows.append(row)
            if len(rows) >= MAX_PUBLIC_CONTROLLERS_PER_HOST:
                break
        return {
            "ok": True, "host_id": self.host_id, "convoy_id": convoy_id,
            "controllers": rows, "controller_count": len(rows),
            "jobs_unreadable": int(unreadable or 0),
            "wakes_touchdesigner": False,
        }

    def peer_controllers_view(self, origin_host_id, convoy_id,
                              authenticated_fingerprint=None):
        """Peer-safe controller view with authorization on both sides of I/O."""
        try:
            convoy_id = identity.normalize_convoy_id(convoy_id)
        except identity.IdentityError:
            return 400, {"ok": False, "reason": "malformed"}

        def authorize():
            with self.lock:
                record = self.peers.get(origin_host_id)
                decision = self.peers.authorize_peer(
                    origin_host_id, authenticated_fingerprint,
                    convoy_id=convoy_id)
                if not decision.allowed:
                    return _REFUSAL_HTTP.get(decision.reason, 403), {
                        "ok": False, "reason": decision.reason,
                        "detail": decision.detail,
                    }
                realm_refusal = self._realm_operation_refusal(convoy_id)
                if realm_refusal is not None:
                    reason, detail = realm_refusal
                    return 409, {"ok": False, "reason": reason,
                                 "detail": detail}
            return None

        refusal = authorize()
        if refusal is not None:
            return refusal
        payload = self._local_controller_view(convoy_id)
        refusal = authorize()
        if refusal is not None:
            return refusal
        return 200, payload

    @staticmethod
    def _sanitize_peer_controller(row, target, convoy_id, node_id=None):
        if not isinstance(row, dict):
            return None
        controller_id = row.get("controller_id")
        if not isinstance(controller_id, str) or not controller_id:
            return None
        raw_leases = row.get("leases")
        if not isinstance(raw_leases, list):
            raw_leases = []
        leases = []
        for lease in raw_leases[:MAX_PUBLIC_NODES_PER_HOST]:
            if not isinstance(lease, dict):
                continue
            lease_node = lease.get("node_id")
            if (not isinstance(lease_node, str) or not lease_node
                    or (node_id is not None and lease_node != node_id)):
                continue
            leases.append({
                "node_id": lease_node[:MAX_ID_CHARS],
                "controller_id": controller_id[:MAX_ID_CHARS],
                "mode": str(lease.get("mode") or "")[:32],
                "expires": lease.get("expires"),
            })
        selected = row.get("selected_node_id")
        if not isinstance(selected, str) or (node_id and selected != node_id):
            selected = None
        raw_jobs = row.get("active_jobs")
        if not isinstance(raw_jobs, list):
            raw_jobs = []
        jobs = []
        for job in raw_jobs[:MAX_ACTIVE_JOBS_PER_CONTROLLER]:
            if not isinstance(job, dict):
                continue
            job_node = job.get("node_id")
            if node_id is not None and job_node != node_id:
                continue
            jobs.append({
                "delivery_id": str(job.get("delivery_id") or "")[:128],
                "node_id": str(job_node or "")[:MAX_ID_CHARS],
                "operation": str(job.get("operation") or "")[:128],
                "state": str(job.get("state") or "")[:32],
            })
        if node_id is not None and not leases and not jobs and selected is None:
            return None
        node_ids = sorted({item["node_id"] for item in leases + jobs
                           if item.get("node_id")}
                          | ({selected} if selected else set()))
        raw_origins = row.get("origin_host_ids")
        if not isinstance(raw_origins, list):
            raw_origins = []
        return {
            "host_id": target.host_id,
            "convoy_id": convoy_id,
            "controller_id": controller_id[:MAX_ID_CHARS],
            "label": str(row.get("label") or "")[:128],
            "selected_node_id": selected,
            "last_seen_age_s": row.get("last_seen_age_s"),
            "leases": leases, "node_ids": node_ids,
            "active_jobs": jobs,
            "origin_host_ids": [str(value)[:MAX_ID_CHARS]
                                for value in raw_origins
                                if isinstance(value, str)][:64],
        }

    def network_controllers(self, convoy_id=None, host_id=None, node_id=None):
        """Aggregate live controller state from reachable sibling hosts."""
        try:
            if convoy_id is not None:
                convoy_id = identity.normalize_convoy_id(convoy_id)
            if host_id is not None and not identity.is_valid_id(host_id):
                raise identity.IdentityError("malformed_host_id", host_id)
            if node_id is not None and not identity.is_valid_id(node_id):
                raise identity.IdentityError("malformed_node_id", node_id)
        except identity.IdentityError as exc:
            return 400, {"ok": False, "reason": "malformed",
                         "detail": str(exc)[:256]}

        with self.lock:
            active = sorted(self._active_convoy_ids_locked())
            if convoy_id is None:
                convoy_id = active[0] if len(active) == 1 else None
            if convoy_id is None:
                return 200, {"ok": True, "host_id": self.host_id,
                             "convoy_id": None, "controllers": [],
                             "peers": [], "controller_count": 0,
                             "wakes_touchdesigner": False}
            if convoy_id not in active:
                snapshot = self.realm.snapshot()
                if (snapshot and snapshot.get("convoy_id")
                        and snapshot.get("convoy_id") != convoy_id):
                    return 409, {
                        "ok": False, "reason": "realm_namespace_mismatch",
                        "detail": "the requested Convoy is not this host's "
                                  "automatic realm"}
            keys = self.hostkeys
            peer_records = list(self.peers.peers())
            queries = []
            peer_status = []
            for peer in peer_records:
                peer_host_id = peer.get("host_id")
                if host_id is not None and peer_host_id != host_id:
                    continue
                decision = self.peers.authorize_peer(
                    peer_host_id, peer.get("fingerprint"),
                    convoy_id=convoy_id)
                if not decision.allowed:
                    continue
                targets, error = self._peer_targets_from_record(peer)
                projection = (targets[0] if targets else
                              _PeerProjectionTarget(peer_host_id))
                queries.append((projection, targets, error))

        local_rows = []
        if host_id is None or host_id == self.host_id:
            local_rows = self._local_controller_view(
                convoy_id, node_id=node_id)["controllers"]
            peer_status.append({"host_id": self.host_id, "status": "online",
                                "local": True,
                                "compatibility": "compatible"})

        def fetch(item):
            projection, targets, target_error = item
            used_session, session_result = self._session_call_if_connected(
                projection.host_id, convoy_id,
                peerserver.SESSION_RPC_CONTROLLERS, {},
                NETWORK_QUERY_TIMEOUT_S)
            if used_session:
                return projection, session_result
            if not targets:
                return projection, {
                    "ok": False, "reason": "peer_endpoint_unknown",
                    "detail": target_error,
                }
            if keys is None:
                return projection, None
            self._audit_http_compat_fallback(
                projection.host_id, peerserver.SESSION_RPC_CONTROLLERS)
            return self._call_peer_targets(
                targets,
                lambda candidate, remaining:
                    peerclient.get_peer_controllers(
                        candidate, keys, convoy_id, timeout=remaining,
                        pool=self.peer_pool),
                NETWORK_QUERY_TIMEOUT_S,
                retry_ambiguous=True)

        outcomes = self._fanout(
            queries, fetch, on_error=lambda item: (item[0], None))

        remote_rows = []
        for target, result in outcomes:
            compatibility = self._peer_compatibility_projection(
                target.host_id)
            if result is peerclient.UNREACHABLE:
                status, reason = "offline", "peer_unreachable"
            elif isinstance(result, peerclient._PinMismatch):
                status, reason = "error", "pin_mismatch"
            elif not isinstance(result, dict) or not result.get("ok"):
                status, reason = "error", (
                    result.get("reason", "peer_bad_response")
                    if isinstance(result, dict) else "peer_bad_response")
            else:
                status, reason = "online", None
                for row in result.get("controllers", ())[
                        :MAX_PUBLIC_CONTROLLERS_PER_HOST]:
                    clean = self._sanitize_peer_controller(
                        row, target, convoy_id, node_id=node_id)
                    if clean is not None:
                        remote_rows.append(clean)
            entry = {"host_id": target.host_id, "status": status}
            if reason:
                entry["reason"] = reason
            if compatibility is not None:
                entry["compatibility"] = compatibility
            peer_status.append(entry)

        rows = sorted((local_rows + remote_rows)[
                      :MAX_NETWORK_CONTROLLER_ROWS], key=lambda row: (
                          row.get("host_id") or "",
                          row.get("controller_id") or ""))
        return 200, {
            "ok": True, "host_id": self.host_id,
            "convoy_id": convoy_id, "controllers": rows,
            "controller_count": len(rows), "peers": peer_status,
            "truncated": len(local_rows) + len(remote_rows)
                         > MAX_NETWORK_CONTROLLER_ROWS,
            "wakes_touchdesigner": False,
        }

    def _release_operation_claim_locked(self, job):
        """Release exactly this delivery's implicit writer claim."""
        if not isinstance(job, dict):
            return False
        node_id = job.get("node_id")
        delivery_id = job.get("delivery_id")
        if not isinstance(node_id, str) or not isinstance(delivery_id, str):
            return False
        return self.leases.release_operation(node_id, delivery_id)

    def _restore_operation_claims_at_boot(self):
        """Rebuild writer exclusion for durable in-flight mutations."""
        try:
            jobs, unreadable = self.db.scan_jobs()
        except Exception:
            self._operation_job_scan_failed = True
            return 0
        self._unreadable_operation_jobs.update(
            delivery_id for delivery_id in (unreadable or ())
            if isinstance(delivery_id, str) and delivery_id)
        restored = 0
        for job in sorted(jobs, key=lambda row: (
                row.get("created", 0), row.get("delivery_id", ""))):
            if job.get("state") not in ("dispatching", "running"):
                continue
            node_id = job.get("node_id")
            controller_id = job.get("controller_id")
            delivery_id = job.get("delivery_id")
            if not all(isinstance(value, str) and value for value in (
                    node_id, controller_id, delivery_id)):
                continue
            try:
                gating = effective_operation_gating(
                    self.operations, job.get("operation"),
                    job.get("arguments"))
                if not gating["mutating"]:
                    continue
                self.leases.restore_operation(
                    node_id, controller_id, delivery_id, self._now())
                restored += 1
            except (OperationRegistryError, controllers.LeaseError):
                # A corrupt/legacy conflict is kept visible in durable job
                # state and must not prevent the host from starting.  The
                # first valid claim wins deterministically by creation time.
                self._unreadable_operation_jobs.add(delivery_id)
                continue
        return restored

    def _refresh_unreadable_operation_fence_locked(self):
        """Recover unreadable boot records or retain a global writer fence.

        Without a parseable job record the host cannot know which node may
        already have a mutation in flight. Reads remain available, but new
        mutations fail closed until the record is repaired or explicitly
        removed.
        """
        if self._operation_job_scan_failed:
            try:
                jobs, unreadable = self.db.scan_jobs()
            except Exception:
                return True
            self._operation_job_scan_failed = False
            self._unreadable_operation_jobs.update(
                delivery_id for delivery_id in (unreadable or ())
                if isinstance(delivery_id, str) and delivery_id)
            for job in jobs:
                if job.get("state") not in ("dispatching", "running"):
                    continue
                delivery_id = job.get("delivery_id")
                try:
                    gating = effective_operation_gating(
                        self.operations, job.get("operation"),
                        job.get("arguments"))
                    if gating["mutating"]:
                        self.leases.restore_operation(
                            job.get("node_id"), job.get("controller_id"),
                            delivery_id, self._now())
                except (OperationRegistryError, controllers.LeaseError):
                    if isinstance(delivery_id, str):
                        self._unreadable_operation_jobs.add(delivery_id)

        for delivery_id in list(self._unreadable_operation_jobs):
            job = self.db.get_job(delivery_id)
            if job is None:
                if not self.db.job_file_exists(delivery_id):
                    self._unreadable_operation_jobs.discard(delivery_id)
                continue
            if job.get("state") in ("dispatching", "running"):
                try:
                    gating = effective_operation_gating(
                        self.operations, job.get("operation"),
                        job.get("arguments"))
                    if gating["mutating"]:
                        self.leases.restore_operation(
                            job.get("node_id"), job.get("controller_id"),
                            delivery_id, self._now())
                except (OperationRegistryError, controllers.LeaseError):
                    continue
            self._unreadable_operation_jobs.discard(delivery_id)
        return bool(self._unreadable_operation_jobs
                    or self._operation_job_scan_failed)

    def _reconcile_operation_claims_if_stale_locked(self):
        """Reconcile claims on a read path at most once per TTL.

        ``_reconcile_operation_claims_locked`` does one durable job-file read
        per live claim while holding the app lock.  On the passive
        directory/status reads that is exactly the cost the slice-2 fix
        removed, so those callers use THIS wrapper: mutation/dispatch paths
        keep reconciling eagerly, but a burst of readers can trigger the
        per-claim file I/O only once per interval.  Returns the released
        count, or 0 when the TTL suppressed the pass.
        """
        now = self._now()
        try:
            elapsed = float(now) - float(self._last_claim_read_reconcile)
        except (TypeError, ValueError):
            elapsed = OPERATION_CLAIM_READ_RECONCILE_TTL_S
        if 0.0 <= elapsed < OPERATION_CLAIM_READ_RECONCILE_TTL_S:
            return 0
        self._last_claim_read_reconcile = now
        return self._reconcile_operation_claims_locked()

    def _reconcile_operation_claims_locked(self):
        """Drop terminal/missing claims before making a lease decision."""
        now = self._now()
        released = 0
        # Inspect even expired claims before reap: a durable in-flight job
        # renews its exclusion after OS sleep; a dead controller's QUEUED job
        # releases promptly so it cannot wedge the node.
        for lease in self.leases.operation_claims():
            delivery_id = lease.get("delivery_id")
            job = self.db.get_job(delivery_id)
            if job is None and self.db.job_file_exists(delivery_id):
                # HostStore deliberately collapses unreadable and absent to
                # None.  An unreadable durable job may be running, so retain
                # its writer fence (fail closed) until the record is readable
                # or an operator removes the corrupt state explicitly.
                self.leases.renew_operation(
                    lease.get("node_id"), delivery_id, now, detach=True)
            elif job is None or job.get("state") in hoststore.TERMINAL_STATES:
                if self.leases.release_operation(
                        lease.get("node_id"), delivery_id):
                    released += 1
            elif job.get("state") == "queued" and not \
                    self.leases.controller_alive(
                        lease.get("controller_id"), now):
                if self.leases.release_operation(
                        lease.get("node_id"), delivery_id):
                    released += 1
            elif job.get("state") in ("dispatching", "running"):
                self.leases.renew_operation(
                    lease.get("node_id"), delivery_id, now, detach=True)
        self.leases.reap(now)
        # Any full reconcile (eager or read-path) resets the read TTL, so a
        # passive read right after a mutation reconcile does not repeat it.
        self._last_claim_read_reconcile = now
        return released

    def _refresh_job_controller_locked(self, job):
        """Treat an owned status poll as the controller's idle heartbeat."""
        if not isinstance(job, dict):
            return
        if job.get("state") in hoststore.TERMINAL_STATES:
            self._release_operation_claim_locked(job)
            return
        controller_id = job.get("controller_id")
        node_id = job.get("node_id")
        if not isinstance(controller_id, str) or not controller_id:
            return
        try:
            self.leases.heartbeat(
                controller_id, self._now(), selected_node_id=node_id)
        except controllers.LeaseError:
            return
        try:
            gating = effective_operation_gating(
                self.operations, job.get("operation"), job.get("arguments"))
            if gating["mutating"]:
                # Same-controller renewal is idempotent.  A stale queued job
                # that lost ownership to another controller stays queued and
                # cannot steal that controller's live claim back merely by
                # being polled.
                self.leases.claim_operation(
                    node_id, controller_id, job.get("delivery_id"),
                    self._now())
        except (OperationRegistryError, controllers.LeaseError):
            pass

    def _ensure_operation_claim_locked(self, job, source):
        """Acquire/renew a queued/running mutation's exact writer claim."""
        if not isinstance(job, dict):
            return None
        if job.get("state") in hoststore.TERMINAL_STATES:
            self._release_operation_claim_locked(job)
            return None
        controller_id = job.get("controller_id")
        if not isinstance(controller_id, str) or not controller_id:
            # Legacy/local jobs may predate controller attribution.  New
            # Convoy bridge and peer submissions always carry one.
            return None
        try:
            gating = effective_operation_gating(
                self.operations, job.get("operation"), job.get("arguments"))
        except OperationRegistryError:
            return None
        if not gating["mutating"]:
            self._release_operation_claim_locked(job)
            return None
        try:
            self.leases.claim_operation(
                job.get("node_id"), controller_id,
                job.get("delivery_id"), self._now())
        except controllers.LeaseError as exc:
            code, payload = self._refuse(
                source, exc.reason, exc.detail,
                _REFUSAL_HTTP.get(exc.reason, 409),
                self.directory.lookup(job.get("node_id")), {
                    "operation": str(job.get("operation") or "")[
                        :MAX_OPERATION_CHARS],
                    "controller_id": controller_id[:MAX_ID_CHARS],
                    "delivery_id": str(job.get("delivery_id") or "")[:128],
                })
            payload["holder"] = exc.holder
            payload["delivery_id"] = job.get("delivery_id")
            return code, payload
        return None

    def create_job(self, body):
        """The LOCAL job path: token-authenticated, loopback-only, no
        envelope. It shares the registry and lease gates with /envelope
        -- ONE gate for what may be enqueued, never two authorities --
        and it never rides the LAN: remote submission is /envelope only.
        """
        try:
            idempotency_key = text_field(body, "idempotency_key")
            operation = text_field(body, "operation",
                                   limit=MAX_OPERATION_CHARS)
            node_id = text_field(body, "node_id")
            # controller_id is optional on the local path; without one
            # the caller simply has no lease rights of its own.
            controller_id = text_field(body, "controller_id",
                                       required=False)
            expected_runtime_id = text_field(body, "expected_runtime_id",
                                             required=False)
            arguments = dict_field(body, "arguments")
        except Malformed as e:
            return self._refuse("jobs", "malformed", e.detail, 400)
        node = self.directory.lookup(node_id)
        if node is None:
            return self._refuse("jobs", "unknown_node", node_id, 404)
        refusal = self._gate_operation(
            node, operation, controller_id, source="jobs",
            expected_runtime_id=expected_runtime_id,
            arguments=arguments)
        if refusal is not None:
            return refusal
        # convoy_id comes from the REGISTERED node, never from the
        # request: a caller must not be able to choose which namespace
        # its idempotency key lands in.
        try:
            job, created = self.db.create_job(
                idempotency_key, node_id, operation, arguments,
                convoy_id=node["convoy_id"],
                expected_runtime_id=expected_runtime_id,
                # THIS host is the origin: the local path is loopback-only
                # and its credential is the IPC token, so the machine that
                # asked is this one. Recorded rather than left None so a
                # local job and a pre-origin legacy record are
                # distinguishable.
                origin_host_id=self.host_id, controller_id=controller_id)
        except hoststore.IdempotencyOriginConflict as e:
            # A peer already holds this key. Say so, rather than handing
            # the local caller a record it does not own and cannot keep.
            return self._refuse("jobs", e.reason, str(e), 409, node)
        claim_refusal = self._ensure_operation_claim_locked(job, "jobs")
        if claim_refusal is not None:
            return claim_refusal
        return 200, {"ok": True, "created": created, "job": job}

    def get_job(self, delivery_id):
        job = self.db.get_job(delivery_id)
        if job is None:
            return 404, {"ok": False, "reason": "unknown_job",
                         "detail": delivery_id}
        self._refresh_job_controller_locked(job)
        return 200, {"ok": True, "job": job}

    @staticmethod
    def _job_artifact_reference(job):
        result = job.get("result") if isinstance(job, dict) else None
        reference = result.get("artifact") if isinstance(result, dict) else None
        return reference if isinstance(reference, dict) else None

    def _release_acknowledged_job_artifact(self, job):
        reference = self._job_artifact_reference(job)
        if reference is None:
            return False
        released = False
        try:
            self.artifacts.release(
                job["convoy_id"], reference["artifact_id"],
                self._artifact_protection_for_job(job))
            released = True
        except (artifacts_mod.ArtifactError, KeyError, TypeError, ValueError):
            # A missing/expired cache object must not make the durable job
            # outcome impossible to acknowledge. The audit/result still says
            # whether a protection was actually released.
            pass
        try:
            # Ownership is authorization history, not a permanent retention
            # mechanism.  Once the exact terminal outcome is acknowledged (or
            # its durable job reaped), retire only that job's claim so repeated
            # identical screenshots cannot exhaust the deduplicated object's
            # bounded owner table.
            self.artifacts.release_owner(
                job["convoy_id"], reference["artifact_id"],
                self._artifact_owner_for_job(job))
        except (artifacts_mod.ArtifactError, KeyError, TypeError, ValueError):
            pass
        return released

    def _acknowledge_job_locked(self, delivery_id):
        job = self.db.get_job(delivery_id)
        if job is None:
            return 404, {"ok": False, "reason": "unknown_job",
                         "detail": str(delivery_id)[:128]}
        if job.get("state") not in hoststore.TERMINAL_STATES:
            return 409, {"ok": False, "reason": "job_not_terminal",
                         "delivery_id": job.get("delivery_id"),
                         "state": job.get("state")}
        self._release_operation_claim_locked(job)
        already = job.get("outcome_acknowledged_at") is not None
        try:
            acknowledged = self.db.acknowledge_outcome(delivery_id)
        except (KeyError, ValueError, OSError):
            return 500, {"ok": False, "reason": "job_acknowledge_failed",
                         "delivery_id": str(delivery_id)[:128]}
        released = self._release_acknowledged_job_artifact(acknowledged)
        return 200, {
            "ok": True, "delivery_id": acknowledged["delivery_id"],
            "state": acknowledged.get("state"),
            "acknowledged_at": acknowledged.get("outcome_acknowledged_at"),
            "already_acknowledged": already,
            "artifact_protection_released": released,
            "wakes_touchdesigner": False,
        }

    def acknowledge_job(self, body):
        """IPC-only acknowledgement of an observed terminal outcome."""
        try:
            delivery_id = text_field(body, "delivery_id")
        except Malformed as exc:
            return self._refuse("jobs", "malformed", exc.detail, 400)
        with self.lock:
            return self._acknowledge_job_locked(delivery_id)

    def peer_acknowledge_job(self, origin_host_id, convoy_id, delivery_id,
                             authenticated_fingerprint=None):
        """Acknowledge only a terminal job owned by this exact peer."""
        try:
            convoy_id = identity.normalize_convoy_id(convoy_id)
        except identity.IdentityError:
            return 400, {"ok": False, "reason": "malformed"}
        with self.lock:
            decision = self.peers.authorize_peer(
                origin_host_id, authenticated_fingerprint,
                convoy_id=convoy_id)
            if not decision.allowed:
                return _REFUSAL_HTTP.get(decision.reason, 403), {
                    "ok": False, "reason": decision.reason,
                    "detail": decision.detail,
                }
            job = self.db.get_job(delivery_id)
            if (job is None or job.get("convoy_id") != convoy_id
                    or job.get("origin_host_id") != origin_host_id):
                return 404, {"ok": False, "reason": "unknown_job",
                             "detail": str(delivery_id)[:128]}
            return self._acknowledge_job_locked(delivery_id)

    def _send_node_wake(self, node, action, lease_id):
        """Send one secret-safe, fire-and-forget loopback wake command.

        The caller may hold ``self.lock``: convoy_wake uses one UDP send and
        never waits for a response.  The random token and port are transient
        node-record fields and are never audited or returned by this method.
        """
        port = node.get("wake_port")
        token = node.get("wake_token")
        if (not node.get("remote_wake") or not port or not token):
            return {"ok": False, "reason": "remote_wake_disabled",
                    "detail": "the node has no active remote-wake endpoint"}
        ttl_s = self._node_wake_ttl(node)
        try:
            result = self.waker(port, token, action, lease_id, ttl_s)
        except Exception as exc:
            safe_detail = str(exc).replace(str(token), "[redacted]")[:160]
            result = {"ok": False, "reason": "wake_sender_failed",
                      "detail": f"{type(exc).__name__}: {safe_detail}"}
        if not isinstance(result, dict) or result.get("ok") is not True:
            reason = (result.get("reason") if isinstance(result, dict)
                      else "wake_sender_bad_response")
            return {"ok": False, "reason": str(reason)[:64],
                    "detail": "the local wake datagram could not be sent"}
        return {"ok": True, "action": action, "lease_id": lease_id}

    @staticmethod
    def _node_wake_ttl(node):
        return min(wake_mod.TTL_MAX_S, max(
            wake_mod.TTL_DEFAULT_S,
            int(node.get("wake_grace_s") or 0) + 30))

    def _start_wake_refresher(self, node, lease_id):
        """Refresh one active TD wake lease until the caller stops it."""
        stop = threading.Event()
        interval = max(0.05, min(
            WAKE_REFRESH_MAX_S, self._node_wake_ttl(node) / 3.0))

        def _refresh():
            while not stop.wait(interval):
                self._send_node_wake(node, "touch", lease_id)

        thread = threading.Thread(
            target=_refresh,
            name="ConvoyWakeRefresh-" + str(lease_id)[:12], daemon=True)
        thread.start()
        return stop, thread

    @staticmethod
    def _stop_wake_refresher(refresher):
        if refresher is None:
            return True
        stop, thread = refresher
        stop.set()
        thread.join(timeout=1.0)
        return not thread.is_alive()

    @staticmethod
    def _wake_result_running(response):
        """Whether a HostApp (status, payload) keeps the wake lease open."""
        try:
            return response[1]["job"]["state"] == "running"
        except (KeyError, IndexError, TypeError):
            return False

    # -- dispatch: execute a queued job against the node (Phase 4 slice 1)

    def dispatch_job(self, delivery_id):
        """Execute one QUEUED job by forwarding its operation to the node's
        Envoy (loopback), then mirror the verdict.

        SELF-LOCKING, unlike every other handler: called WITHOUT
        self.lock held (the /dispatch and /drain routes sit outside
        do_POST's lock block; threading.Lock is not reentrant, so calling
        this from inside the lock would deadlock). Three phases:

          a) UNDER the lock: read the job and node, re-gate against the
             CURRENT registry and the runtime AS THE HOST LAST OBSERVED
             IT, then CLAIM the job (queued -> dispatching, the CAS in
             claim_for_dispatch) and mark it in-flight. The claim is what
             lets the drain loop and manual /dispatch race safely --
             exactly one caller wins a given job.
          b) OUTSIDE the lock: the forward, up to 30s of I/O. Holding the
             lock here would freeze every other route for the duration --
             the drain loop would make that permanent.
          c) UNDER the lock again: resolve the claim (_resolve_dispatch).
             The A-15 invariant holds: a node verdict (record_sync_result,
             demanding a real boolean ok), back to queued (UNREACHABLE =
             never delivered), or indeterminate (no response, a
             non-verdict response, or the recording itself failed =
             outcome unknown). The host never invents a verdict.

        ASYNC operations (registry entries carrying `async_job`) resolve
        differently in phase c: the node answers with a job HANDLE, not
        a result, so the job is recorded 'running' with that provenance
        and the POLL pass owns its outcome from there. An async answer
        with no handle goes back to queued -- the retry reconciles on
        the idempotency key rather than guessing a verdict.

        Idempotent: a job already claimed or past queued is returned
        unchanged (dispatched=False), so a double dispatch cannot re-run
        the work.
        """
        wake_lease = False
        wake_refresher = None
        # -- phase a: read, gate, claim -- under the lock ---------------
        with self.lock:
            if (not delivery_id or not isinstance(delivery_id, str)
                    or len(delivery_id) > MAX_ID_CHARS):
                # Named, audited 400 -- not a TypeError-500 the audit
                # trail cannot classify (A-39), matching text_field's
                # treatment on every other route.
                return self._refuse("dispatch", "malformed",
                                    "delivery_id must be a string", 400)
            job = self.db.get_job(delivery_id)
            if job is None:
                # Audited directly, NOT through the dedupe map: the map
                # is pruned against jobs that exist, so ghost ids would
                # accumulate entries forever (the drain loop never sees
                # them). Caller-driven and token-gated, so unconditional
                # audit lines are the caller's own doing.
                try:
                    self.db.audit("hostapp", "dispatch_refused",
                                  {"reason": "unknown_job",
                                   "delivery_id": delivery_id})
                except Exception:
                    pass    # named 404 beats an unaudited 500
                return 404, {"ok": False, "reason": "unknown_job",
                             "detail": delivery_id}
            if job.get("state") != "queued":
                # Claimed by another dispatcher, or terminal -- never
                # re-run.
                return 200, {"ok": True, "dispatched": False, "job": job}
            # THE ORDER (plan 1.4) holds here too, and this is its FIRST
            # step: the origin's admission is re-asked before the registry
            # gate, the runtime precondition or the claim. A job is
            # authorized at submission AND again at every dispatch,
            # because the queue outlives the admission -- a peer blocked
            # by a hand edit to denylist.json while its work sat queued
            # must stop dispatching with no API call and no restart, and
            # this is the caller that notices the mtime change.
            decision = self._authorize_origin(
                job.get("origin_host_id"), job.get("convoy_id"))
            denied = None if decision is None or decision.allowed else decision
            if denied is not None:
                return self._refuse_origin(job, denied)
            if decision is not None and (job.get("origin_admission_id")
                                         != decision.admission_id):
                # THE LINEAGE FENCE. The revocation sweep is only the
                # fast path and cannot, by construction, reach
                # everything -- a record unreadable during the sweep, a
                # claim in flight, work that re-enters the queue after
                # it ran. This comparison is what makes "re-admitting
                # the peer does NOT resurrect its pre-revocation work"
                # (_refuse_origin's contract) true even then: the job
                # carries the admission lineage it was authorized under
                # (create_job), and a FULL revocation (block / forget /
                # re-pin) stamps a fresh epoch the moment it happens
                # (convoy_peers _set_state / _upsert), so the old lineage
                # never matches again -- not even a block laundered
                # through observe-only before the re-admit. (observe-only
                # itself is a reversible narrowing, NOT an epoch break;
                # see _refuse_origin.) TERMINAL: a membership decision
                # intervened, so this job can never be served -- leaving
                # it queued would make /jobs lie.
                return self._refuse_origin(
                    job, decision, reason="stale_admission",
                    detail="this job was submitted under a previous "
                           "admission of its origin peer; a revocation "
                           "intervened, and re-admission consents to the "
                           "PEER, never to its pre-revocation work",
                    terminal=True)
            node = self.directory.lookup(job["node_id"])
            if node is None:
                self._note_dispatch_event(delivery_id, "dispatch_refused",
                                          {"reason": "unknown_node",
                                           "node_id": job["node_id"]})
                self._set_drain_backoff(delivery_id,
                                        self._now() + self.drain_backoff_s)
                return 404, {"ok": False, "reason": "unknown_node",
                             "detail": job["node_id"]}
            if not bool(node.get("enabled", True)):
                # Disable is a durable membership decision, not a temporary
                # outage.  Burn work accepted before the switch so toggling
                # Convoy back on cannot resurrect a stale mutation minutes or
                # days later.  A job already running remains pollable because
                # the node may already have executed it; this branch is only
                # reachable for still-queued, definitely-undelivered work.
                detail = {
                    "reason": "node_disabled",
                    "detail": "the target withdrew from Convoy before this "
                              "job was delivered; it did not run",
                    "operation": job.get("operation"),
                    "at": self._now(),
                }
                try:
                    refused = self.db.mark_refused(delivery_id, detail)
                except Exception:
                    refused = None
                self._note_dispatch_event(
                    delivery_id, "dispatch_refused",
                    {"reason": "node_disabled", "terminal": True})
                payload = {
                    "ok": False, "reason": "node_disabled",
                    "detail": detail["detail"],
                }
                if refused is not None:
                    self._release_operation_claim_locked(refused)
                    payload["job"] = refused
                return 409, payload
            if (job.get("convoy_id") != node.get("convoy_id")
                    or node.get("host_id") != self.host_id):
                # A durable job is namespace-bound at admission.  A corrupt
                # or hand-edited record must never use a still-valid node id
                # to execute in a different Convoy (especially host shell).
                detail = {
                    "reason": "namespace_mismatch",
                    "detail": "the durable delivery no longer belongs to "
                              "the target node's local Convoy namespace",
                    "operation": job.get("operation"),
                    "at": self._now(),
                }
                try:
                    refused = self.db.mark_refused(delivery_id, detail)
                except Exception:
                    refused = None
                self._note_dispatch_event(
                    delivery_id, "dispatch_refused",
                    {"reason": "namespace_mismatch", "terminal": True})
                payload = {"ok": False, "reason": "namespace_mismatch",
                           "detail": detail["detail"]}
                if refused is not None:
                    self._release_operation_claim_locked(refused)
                    payload["job"] = refused
                return 403, payload
            # Re-gate against the registry AS OF NOW, not as of enqueue:
            # the entry may have been tightened or removed while the job
            # sat queued. The runtime re-check below is HONESTLY BOUNDED:
            # it compares against the runtime the host LAST OBSERVED (the
            # registry record), so it catches a restart the node has
            # re-registered -- and CANNOT catch a restart the host has
            # not seen yet, or one that lands mid-forward, because the
            # expectation does not ride the wire. Full A-22 closure needs
            # the node itself to verify expected_runtime_id (Phase 2
            # ConvoyExt + the A-46 transport); named deferral, not an
            # oversight.
            try:
                gating = effective_operation_gating(
                    self.operations, job["operation"], job.get("arguments"))
            except OperationRegistryError as e:
                # This queued job can never execute under the registry that
                # now governs the host.  It is still provably undelivered, so
                # terminal `refused` is honest; leaving it queued would let a
                # later permissive edit resurrect stale work unexpectedly.
                detail = {"reason": e.reason, "detail": e.detail,
                          "operation": job.get("operation"),
                          "at": self._now()}
                try:
                    refused = self.db.mark_refused(delivery_id, detail)
                except Exception:
                    refused = None
                self._note_dispatch_event(
                    delivery_id, "dispatch_refused",
                    {"reason": e.reason, "terminal": True,
                     "operation": job["operation"][:MAX_OPERATION_CHARS]})
                payload = {"ok": False, "reason": e.reason,
                           "detail": e.detail}
                if refused is not None:
                    self._release_operation_claim_locked(refused)
                    payload["job"] = refused
                return e.code, payload
            # The helper proved the entry exists and is classified. Keep the
            # concrete entry for the async argument-injection contract below.
            entry = self.operations[job["operation"]]
            self._reconcile_operation_claims_locked()
            if (gating["mutating"]
                    and self._refresh_unreadable_operation_fence_locked()):
                self._note_dispatch_event(
                    delivery_id, "dispatch_deferred",
                    {"reason": "operation_state_unreadable"})
                self._set_drain_backoff(
                    delivery_id, self._now() + self.drain_backoff_s)
                return 503, {
                    "ok": False,
                    "reason": "operation_state_unreadable",
                    "detail": "durable job state is unreadable; the host "
                              "cannot prove another mutation is not in "
                              "flight, so this job stays queued",
                }
            if (gating["executes_arbitrary_code"]
                    and not self.policy.allow_td_python(node["node_id"])):
                # Permission withdrawal is a durable authority change, like
                # disabling the node. Burn still-queued work so switching the
                # permission back on cannot resurrect code accepted earlier.
                detail = {
                    "reason": "td_python_not_approved",
                    "detail": "TD Python permission was withdrawn before "
                              "this job was delivered; it did not run",
                    "operation": job.get("operation"),
                    "at": self._now(),
                }
                try:
                    refused = self.db.mark_refused(delivery_id, detail)
                except Exception:
                    refused = None
                self._note_dispatch_event(
                    delivery_id, "dispatch_refused",
                    {"reason": "td_python_not_approved", "terminal": True})
                payload = {"ok": False,
                           "reason": "td_python_not_approved",
                           "detail": detail["detail"]}
                if refused is not None:
                    self._release_operation_claim_locked(refused)
                    payload["job"] = refused
                return 403, payload
            if (job["operation"] == "convoy_shell"
                    and self.policy.allow_full_shell() is not True):
                detail = {
                    "reason": "full_shell_not_approved",
                    "detail": "Allow Full Shell was withdrawn before this "
                              "host command was dispatched; it did not run",
                    "operation": job.get("operation"),
                    "at": self._now(),
                }
                try:
                    refused = self.db.mark_refused(delivery_id, detail)
                except Exception:
                    refused = None
                self._note_dispatch_event(
                    delivery_id, "dispatch_refused",
                    {"reason": "full_shell_not_approved", "terminal": True})
                payload = {"ok": False,
                           "reason": "full_shell_not_approved",
                           "detail": detail["detail"]}
                if refused is not None:
                    self._release_operation_claim_locked(refused)
                    payload["job"] = refused
                return 403, payload
            # THE REMOTE SURFACE, and the OBSERVE-ONLY narrowing, re-asked
            # HERE -- not only at submission. The queue outlives both: a
            # peer narrowed to observe-only while its mutation was in
            # flight gets that mutation requeued (UNREACHABLE = never
            # delivered, which is honest and correct) and the next pass
            # would forward it. A one-shot revocation sweep CANNOT cover
            # that by construction, which is why re-authorization per pass
            # is the containment and the sweep is only the fast path.
            if decision is not None:
                if not gating["remote_exposed"]:
                    return self._refuse_origin(
                        job, decision, reason="operation_not_remote_exposed",
                        detail=f"{job['operation']!r} is not on this host's "
                               f"REMOTE surface", terminal=True)
                if not decision.may_mutate and gating["mutating"]:
                    return self._refuse_origin(
                        job, decision,
                        reason=peers_mod.REASON_OBSERVE_ONLY,
                        detail=decision.detail, terminal=True)
            if gating["runtime_required"]:
                expected = job.get("expected_runtime_id")
                current = node.get("runtime_id")
                if not expected or not current or expected != current:
                    self._note_dispatch_event(
                        delivery_id, "dispatch_runtime_changed",
                        {"reason": f"expected {str(expected)[:64]!r}, "
                                   f"node {str(current)[:64]!r}",
                         "expected": str(expected)[:64],
                         "current": str(current)[:64]})
                    self._set_drain_backoff(delivery_id,
                        self._now() + self.drain_backoff_s)
                    return 409, {
                        "ok": False, "reason": "runtime_changed",
                        "detail": f"the job addressed runtime "
                                  f"{expected!r} but the node is now "
                                  f"{current!r}; refusing to dispatch "
                                  f"into a different runtime (A-22)"}
            controller_id = job.get("controller_id")
            if (gating["mutating"] and isinstance(controller_id, str)
                    and controller_id):
                if not self.leases.controller_alive(
                        controller_id, self._now()):
                    self._note_dispatch_event(
                        delivery_id, "dispatch_deferred",
                        {"reason": "controller_heartbeat_required"})
                    self._set_drain_backoff(
                        delivery_id, self._now() + self.drain_backoff_s)
                    return 409, {
                        "ok": False,
                        "reason": "controller_heartbeat_required",
                        "detail": "the mutating delivery's controller is "
                                  "not live; it stays queued until the "
                                  "controller reconnects",
                    }
                claim_refusal = self._ensure_operation_claim_locked(
                    job, "dispatch")
                if claim_refusal is not None:
                    self._set_drain_backoff(
                        delivery_id, self._now() + self.drain_backoff_s)
                    return claim_refusal
            port = node.get("envoy_port")
            needs_td_endpoint = (
                job["operation"] not in HOST_NATIVE_OPERATIONS
                or job["operation"] == "convoy_restart_node")
            if (needs_td_endpoint
                    and node.get("perform_mode")):
                if not node.get("wake_active") or not port:
                    wake = self._send_node_wake(
                        node, "acquire", delivery_id)
                    if not wake.get("ok"):
                        reason = wake.get("reason") or \
                            "remote_wake_unavailable"
                        self._note_dispatch_event(
                            delivery_id, "dispatch_deferred",
                            {"reason": reason})
                        self._set_drain_backoff(
                            delivery_id, self._now() + self.drain_backoff_s)
                        return 409, {
                            "ok": False, "reason": reason,
                            "detail": "the target is in Perform Mode and "
                                      "cannot be remotely awakened; the job "
                                      "stays queued",
                        }
                    self._note_dispatch_event(
                        delivery_id, "node_wake_requested",
                        {"reason": "perform_mode"})
                    self._set_drain_backoff(
                        delivery_id, self._now() + WAKE_RETRY_S)
                    return 409, {
                        "ok": False, "reason": "node_waking",
                        "detail": "the target accepted a Perform wake; the "
                                  "job stays queued until Envoy re-registers",
                    }
                # Already awake for this or another delivery.  Refresh this
                # delivery's hard TTL before forwarding; release below starts
                # the user-configured idle grace for terminal sync results.
                self._send_node_wake(node, "touch", delivery_id)
                wake_lease = True
            if not port and needs_td_endpoint:
                # No endpoint yet -- the node has not registered its live
                # Envoy port. Leave the job queued (unclaimed) to dispatch
                # once it does; this is a not-yet, not a failure.
                self._note_dispatch_event(
                    delivery_id, "dispatch_deferred",
                    {"reason": "node_endpoint_unknown"})
                self._set_drain_backoff(delivery_id,
                                        self._now() + self.drain_backoff_s)
                return 409, {"ok": False, "reason": "node_endpoint_unknown",
                             "detail": "the node has not registered its "
                                       "Envoy port; the job stays queued"}
            if needs_td_endpoint and not self._node_is_online(node):
                self._note_dispatch_event(
                    delivery_id, "dispatch_deferred",
                    {"reason": "node_endpoint_stale"})
                self._set_drain_backoff(delivery_id,
                                        self._now() + self.drain_backoff_s)
                return 409, {
                    "ok": False, "reason": "node_endpoint_stale",
                    "detail": "the node missed its 60-second heartbeat "
                              "grace; the job stays queued until it "
                              "re-registers",
                }
            try:
                claimed = self.db.claim_for_dispatch(delivery_id)
            except Exception as e:
                # The claim WRITE failed (a Windows sharing violation
                # that outlived the retries, disk trouble): os.replace
                # is atomic, so the job on disk is still queued. A named
                # refusal, never the unaudited 500 this used to become.
                detail = f"{type(e).__name__}: {e}"
                try:
                    self.db.audit("hostapp", "dispatch_store_unavailable",
                                  {"delivery_id": delivery_id,
                                   "detail": detail[:256]})
                except Exception:
                    pass
                return 503, {"ok": False, "reason": "store_unavailable",
                             "detail": f"could not claim the job "
                                       f"({detail}); it stays queued"}
            if claimed is None:
                # Should be unreachable (the state was read as queued
                # under this same lock hold) -- but a skip is always the
                # safe answer to a lost claim.
                job = self.db.get_job(delivery_id) or job
                return 200, {"ok": True, "dispatched": False, "job": job}
            if gating["mutating"]:
                # The durable job has crossed queued -> dispatching.  Its
                # writer exclusion now follows the in-flight job rather than
                # controller heartbeat, so a dropped client cannot admit a
                # concurrent mutation while this operation may be running.
                self.leases.renew_operation(
                    job.get("node_id"), delivery_id, self._now(),
                    detach=True)
            # Capture everything the rest of the dispatch needs BEFORE
            # the marker goes up, so that nothing at all sits between
            # the marker and the try/finally below.
            operation = job["operation"]
            raw_arguments = job.get("arguments")
            idempotency_key = job.get("idempotency_key")
            async_spec = entry.get("async_job")
            self._flight_counter += 1
            attempt = self._flight_counter
            self._in_flight[delivery_id] = attempt
            cancel_event = None
            if operation in HOST_CANCELABLE_OPERATIONS:
                cancel_event = threading.Event()
                self._hostop_cancel_events[delivery_id] = cancel_event

        # FROM HERE the claim is durable and the marker is up, so the
        # claim and its cleanup share ONE failure domain. They did not:
        # the async argument merge sat inside phase a, ABOVE this try,
        # and a raise there (a non-dict `arguments` -> `dict(list)`)
        # leaked the marker forever. That made drain_once's reaper skip
        # the job permanently (`if did in self._in_flight: continue`),
        # left it invisible to /dispatch and /poll alike, and ended in a
        # host restart writing a FALSE indeterminate for an operation
        # that never left phase a (panel probe, 2026-08-01). Structural
        # on purpose: no future edit to the post-claim tail can reopen
        # the class.
        try:
            # -- phase a tail: the arguments actually put on the wire ---
            with self.lock:
                try:
                    arguments, injected, dropped = self._merged_arguments(
                        raw_arguments, idempotency_key, async_spec)
                except Malformed as e:
                    # Belt to the door's braces: enqueue refuses a
                    # non-dict on both create paths, so only an older
                    # build or hand-edited state reaches this -- and it
                    # RELEASES the claim into a named refusal instead of
                    # raising. The job never goes on the wire malformed.
                    return self._requeue_claim(
                        delivery_id, operation, self._now(),
                        reason="malformed_arguments",
                        event="dispatch_malformed_arguments",
                        detail=f"the job's stored arguments are unusable "
                               f"({e.detail}); it stays queued and nothing "
                               f"was forwarded",
                        cause="the job's stored arguments are unusable")
                # Unconditional, and deliberately NOT deduped: this
                # records a real state transition on disk (queued ->
                # dispatching), not a refusal. Named cost (panel,
                # 2026-08-01): a delivery that requeues forever appends
                # one line per attempt, ~2 per minute at the default
                # backoff. Deduping it would hide genuine claims; the
                # bound belongs to the deferred reaper that stops the
                # retry loop itself, and until then `attempts` on the
                # record is what makes such a job findable without
                # reading the trail at all.
                try:
                    self.db.audit("hostapp", "dispatch_claimed",
                                  {"delivery_id": delivery_id,
                                   "operation": operation, "port": port,
                                   "injected": injected,
                                   "dropped": dropped})
                except Exception:
                    # An audit failure must never divert or strand a
                    # dispatch: the claim is on disk and the in-flight
                    # marker is set -- the forward proceeds.
                    pass

            if wake_lease:
                wake_refresher = self._start_wake_refresher(
                    node, delivery_id)

            # -- phase b: the forward, OUTSIDE the lock -----------------
            detail = ""
            host_outcome = None
            if operation == "convoy_ping":
                # Host-native liveness.  Advertising convoy_ping while
                # forwarding it to Envoy made every real ping fail as an
                # unknown MCP tool.  It intentionally still traverses the
                # normal durable job, authorization, deadline and dispatch
                # state machine; only the final execution is host-local.
                outcome = {"ok": True,
                    "pong": True,
                    "host_id": self.host_id,
                    "node_id": node.get("node_id"),
                    "runtime_id": node.get("runtime_id"),
                    "online": self._node_is_online(node),
                }
                host_outcome = outcome
            elif operation in HOST_SUBPROCESS_OPERATIONS:
                # Host-native execution is intentionally on this branch,
                # before the Envoy forward.  It never consults the node's
                # loopback port and therefore cannot wake or touch TD.
                host_outcome = self._execute_host_operation(
                    operation, node.get("node_id"), job.get("convoy_id"),
                    arguments, cancel_event)
                host_outcome = self._materialize_host_operation_result(
                    host_outcome, job)
                outcome = host_outcome
            elif operation in HOST_LIFECYCLE_OPERATIONS:
                host_outcome = self._execute_lifecycle_operation(
                    operation, node.get("node_id"), job.get("convoy_id"),
                    job.get("expected_runtime_id"), delivery_id, arguments,
                    cancel_event)
                host_outcome = self._materialize_host_operation_result(
                    host_outcome, job)
                outcome = host_outcome
            else:
                try:
                    outcome = self.forwarder(port, operation, arguments)
                except Exception as e:  # a forwarder must not crash dispatch
                    outcome = None
                    detail = f"{type(e).__name__}: {e}"
            if (outcome is not None
                    and outcome is not mcpclient.UNREACHABLE
                    and not (isinstance(outcome, dict)
                             and isinstance(outcome.get("ok"), bool))):
                # A broken forwarder must resolve like any transport
                # failure -- and 'broken' includes a DICT carrying no
                # real boolean verdict. Recording {} or {'error': ...} as
                # failed would fabricate a node verdict the node never
                # produced (A-15); the type check alone was half a guard.
                detail = (f"forwarder returned {type(outcome).__name__} "
                          f"without a boolean 'ok' verdict")
                outcome = None
            observed = self._now()

            # No refresh may race the terminal release below.  Stop it before
            # durable resolution; a crash before here is still bounded by the
            # TD-side hard TTL.
            wake_release_safe = self._stop_wake_refresher(wake_refresher)
            wake_refresher = None

            # -- phase c: resolve the claim, under the lock -------------
            try:
                with self.lock:
                    if host_outcome is not None:
                        response = self._resolve_host_dispatch(
                            delivery_id, operation, host_outcome, observed,
                            job)
                    else:
                        response = self._resolve_dispatch(
                            delivery_id, operation, outcome, observed,
                            detail, async_spec, job)
            except Exception as e:
                response = self._downgrade_failed_recording(
                    delivery_id, operation, e)
            if wake_lease and not self._wake_result_running(response):
                if wake_release_safe:
                    self._send_node_wake(node, "release", delivery_id)
                else:
                    # A pathological injected/blocking sender may wake again
                    # after a release.  Do not race it; the TD-side hard TTL is
                    # the fail-safe and this condition is visible in audit.
                    self._audit_best_effort(
                        "wake_refresh_stop_timeout",
                        {"delivery_id": delivery_id,
                         "node_id": node.get("node_id")})
            return response
        finally:
            self._stop_wake_refresher(wake_refresher)
            with self.lock:
                # Remove ONLY this attempt's marker. After a release back
                # to queued, another attempt may already have re-claimed
                # this job and own a newer marker -- erasing it would let
                # the reaper mark a live forward stranded. And a PARKED
                # release (failed requeue write) or PARKED HANDOFF
                # (failed running-mirror write) keeps its marker on
                # purpose: the claim is still on disk, and the reaper
                # must not resolve it before the drain pass retries.
                if (delivery_id not in self._pending_release
                        and delivery_id not in self._pending_handoff
                        and self._in_flight.get(delivery_id) == attempt):
                    del self._in_flight[delivery_id]
                if (cancel_event is not None
                        and self._hostop_cancel_events.get(delivery_id)
                        is cancel_event):
                    del self._hostop_cancel_events[delivery_id]

    def _execute_host_operation(self, operation, node_id, convoy_id,
                                arguments, cancel_event):
        """Run one reviewed host operation with namespace bound to the resolver.

        Called only after the durable job is claimed and outside ``self.lock``.
        The thread-local namespace is consulted by every HostOperations target
        revalidation, including the one adjacent to process spawn.
        """
        try:
            _validate_host_operation_arguments(operation, arguments)
        except OperationRegistryError:
            return {"ok": False, "code": "invalid_arguments",
                    "detail": "host operation arguments are invalid",
                    "operation": operation, "target_id": node_id}
        with self.lock:
            current = self.directory.lookup(node_id)
            target_ok = bool(
                current is not None
                and current.get("host_id") == self.host_id
                and current.get("convoy_id") == convoy_id
                and current.get("enabled", True) is True)
        if not target_ok:
            return {"ok": False, "code": "target_changed",
                    "detail": "target registration changed before execution",
                    "operation": operation, "target_id": node_id}

        self._hostop_context.expected_convoy_id = convoy_id
        try:
            timeout_s = arguments.get("timeout_s")
            output_limit = arguments.get("output_limit")
            if operation == "convoy_git":
                return self.host_operations.run_git(
                    node_id, arguments.get("operation"),
                    arguments.get("arguments", {}), timeout_s=timeout_s,
                    output_limit=output_limit, cancel_event=cancel_event)
            if operation == "convoy_gh":
                return self.host_operations.run_gh(
                    node_id, arguments.get("operation"),
                    arguments.get("arguments", {}), timeout_s=timeout_s,
                    output_limit=output_limit, cancel_event=cancel_event)
            return self.host_operations.run_shell(
                node_id, arguments.get("command"), cwd=arguments.get("cwd"),
                env_additions=arguments.get("env_additions"),
                timeout_s=timeout_s, output_limit=output_limit,
                cancel_event=cancel_event,
                redact_values=arguments.get("redact_values", ()))
        except Exception:
            # The facade promises not to raise, but this composition boundary
            # still returns a static, secret-free result if that contract is
            # ever broken.  The command/argv/environment are never reflected.
            return {"ok": False, "code": "internal_error",
                    "detail": "host operation failed safely",
                    "operation": operation, "target_id": node_id}
        finally:
            try:
                del self._hostop_context.expected_convoy_id
            except AttributeError:
                pass

    def _execute_lifecycle_operation(self, operation, node_id, convoy_id,
                                     expected_runtime_id, operation_id,
                                     arguments, cancel_event):
        """Run one exact, profile-pinned TD lifecycle operation."""
        try:
            _validate_lifecycle_operation_arguments(operation, arguments)
        except OperationRegistryError:
            return {"ok": False, "code": "invalid_arguments",
                    "detail": "lifecycle arguments are invalid",
                    "operation": operation, "target_id": node_id}
        if self.lifecycle is None:
            return {
                "ok": False,
                "code": "lifecycle_unavailable",
                "detail": self.lifecycle_unavailable_reason
                or "exact-node lifecycle is unavailable",
                "operation": operation,
                "target_id": node_id,
            }
        with self.lock:
            current = self.directory.lookup(node_id)
            target_ok = bool(
                current is not None
                and current.get("host_id") == self.host_id
                and current.get("convoy_id") == convoy_id
                and current.get("enabled", True) is True)
        if not target_ok:
            return {"ok": False, "code": "target_changed",
                    "detail": "target registration changed before execution",
                    "operation": operation, "target_id": node_id}
        timing = self.db.job_timing(operation_id)
        execution_timeout_s = None
        if timing.get("bounded"):
            if timing.get("expired"):
                return {
                    "ok": False, "code": "deadline_exceeded",
                    "detail": "the accepted delivery budget expired before "
                              "lifecycle execution",
                    "operation": operation, "target_id": node_id,
                }
            execution_timeout_s = timing.get("remaining_s")
        self.lifecycle_runtime.begin_operation(operation_id)
        try:
            if operation == "convoy_start_node":
                return self.lifecycle.start_node(
                    node_id, convoy_id, operation_id,
                    timeout_s=arguments.get("timeout_s"),
                    execution_timeout_s=execution_timeout_s,
                    cancel_event=cancel_event)
            return self.lifecycle.restart_node(
                node_id, convoy_id, operation_id, expected_runtime_id,
                policy=arguments.get("policy", "require_clean"),
                timeout_s=arguments.get("timeout_s"),
                execution_timeout_s=execution_timeout_s,
                cancel_event=cancel_event)
        except Exception:
            return {"ok": False, "code": "internal_error",
                    "detail": "lifecycle operation failed safely",
                    "operation": operation, "target_id": node_id}
        finally:
            self.lifecycle_runtime.end_operation()

    def _artifact_owner_for_job(self, job):
        owner = {
            "host_id": (job.get("origin_host_id") or self.host_id),
            "node_id": job.get("node_id"),
            "controller_id": job.get("controller_id"),
            "job_id": job.get("delivery_id"),
        }
        return {key: value for key, value in owner.items()
                if isinstance(value, str) and value}

    @staticmethod
    def _artifact_protection_for_job(job):
        delivery_id = job.get("delivery_id")
        if not isinstance(delivery_id, str) or not delivery_id:
            raise ValueError("job artifact requires a delivery id")
        return "job:%s:result" % delivery_id

    @staticmethod
    def _capture_parts(value):
        """Return (safe description, generated path, inline image block)."""
        texts = []
        image = None
        if isinstance(value, str):
            texts.append(value)
        elif isinstance(value, dict) and isinstance(value.get("content"), list):
            for block in value["content"]:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and isinstance(
                        block.get("text"), str):
                    texts.append(block["text"])
                elif block.get("type") == "image" and image is None:
                    image = block
        path = None
        cleaned = []
        for text in texts:
            for line in text.splitlines():
                if line.startswith("Saved to: "):
                    candidate = line[len("Saved to: "):].strip()
                    if candidate and path is None:
                        path = candidate
                    continue
                if line.startswith("(Use Read tool on the file path"):
                    continue
                cleaned.append(line)
        description = "\n".join(cleaned).strip()[:8192]
        return description, path, image

    @staticmethod
    def _capture_mime_and_extension(value):
        value = str(value or "").strip().lower()
        if value in ("jpeg", "jpg", "image/jpeg"):
            return "image/jpeg", ".jpg"
        if value in ("png", "image/png"):
            return "image/png", ".png"
        raise ValueError("capture image type is unsupported")

    @staticmethod
    def _capture_bytes_valid(value, mime_type):
        if mime_type == "image/png":
            return value.startswith(b"\x89PNG\r\n\x1a\n")
        return (len(value) >= 4 and value.startswith(b"\xff\xd8")
                and value.endswith(b"\xff\xd9"))

    @staticmethod
    def _safe_capture_path(path):
        if not isinstance(path, str) or not path:
            raise ValueError("capture path is missing")
        candidate = os.path.abspath(path)
        name = os.path.basename(candidate)
        if not _ENVOY_CAPTURE_NAME_RE.fullmatch(name):
            raise ValueError("capture path does not use Envoy's filename")
        temp_root = os.path.normcase(os.path.realpath(tempfile.gettempdir()))
        parent = os.path.normcase(os.path.realpath(os.path.dirname(candidate)))
        if parent != temp_root or os.path.islink(candidate):
            raise ValueError("capture path is outside the local temp root")
        return candidate

    @staticmethod
    def _unlink_same_capture(path, original_stat):
        try:
            current = os.stat(path, follow_symlinks=False)
            if (stat.S_ISREG(current.st_mode)
                    and os.path.samestat(original_stat, current)):
                os.unlink(path)
        except (FileNotFoundError, OSError):
            pass

    def _capture_artifact_from_path(self, convoy_id, path, owner,
                                    protection_id):
        candidate = self._safe_capture_path(path)
        mime_type, extension = self._capture_mime_and_extension(
            os.path.splitext(candidate)[1].lstrip("."))
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(candidate, flags)
        original = None
        try:
            original = os.fstat(descriptor)
            if (not stat.S_ISREG(original.st_mode)
                    or original.st_size < 1
                    or original.st_size > MAX_CAPTURE_ARTIFACT_BYTES
                    or original.st_size > self.artifacts.max_artifact_bytes):
                raise ValueError("capture size is outside the artifact limit")
            with os.fdopen(descriptor, "rb") as source:
                descriptor = -1
                head = source.read(8)
                source.seek(max(0, original.st_size - 2), os.SEEK_SET)
                tail = source.read(2)
                source.seek(0)
                sample = head if mime_type == "image/png" else head + tail
                valid = (sample.startswith(b"\x89PNG\r\n\x1a\n")
                         if mime_type == "image/png"
                         else (head.startswith(b"\xff\xd8")
                               and tail == b"\xff\xd9"))
                if not valid:
                    raise ValueError("capture bytes do not match image type")
                return self.artifacts.put_stream(
                    convoy_id, source, expected_size=original.st_size,
                    mime_type=mime_type,
                    filename_hint="capture" + extension, owner=owner,
                    protection_id=protection_id,
                    protection_kind="unacknowledged_job")
        finally:
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            if original is not None:
                self._unlink_same_capture(candidate, original)

    def _capture_artifact_from_inline(self, convoy_id, block, owner,
                                      protection_id):
        if not isinstance(block, dict):
            raise ValueError("capture image content is missing")
        encoded = block.get("data")
        if not isinstance(encoded, str) or len(encoded) > (
                (MAX_CAPTURE_ARTIFACT_BYTES * 4 // 3) + 8):
            raise ValueError("capture image content is outside the limit")
        mime_value = (block.get("mimeType") or block.get("mime_type")
                      or block.get("format"))
        mime_type, extension = self._capture_mime_and_extension(mime_value)
        try:
            raw = base64.b64decode(encoded.encode("ascii"), validate=True)
        except (UnicodeEncodeError, ValueError) as exc:
            raise ValueError("capture image base64 is invalid") from exc
        if (not raw or len(raw) > MAX_CAPTURE_ARTIFACT_BYTES
                or len(raw) > self.artifacts.max_artifact_bytes
                or not self._capture_bytes_valid(raw, mime_type)):
            raise ValueError("capture image bytes are invalid")
        return self.artifacts.put_bytes(
            convoy_id, raw, mime_type=mime_type,
            filename_hint="capture" + extension, owner=owner,
            protection_id=protection_id,
            protection_kind="unacknowledged_job")

    def _materialize_capture_result(self, value, job):
        description, path, image = self._capture_parts(value)
        # sample_grid mode and ordinary capture errors contain neither a
        # generated image path nor an image content block; leave those as the
        # normal structured result rather than inventing an artifact failure.
        if path is None and image is None:
            return None
        owner = self._artifact_owner_for_job(job)
        protection_id = self._artifact_protection_for_job(job)
        reference = None
        failure = None
        try:
            if path is not None:
                reference = self._capture_artifact_from_path(
                    job["convoy_id"], path, owner, protection_id)
            else:
                reference = self._capture_artifact_from_inline(
                    job["convoy_id"], image, owner, protection_id)
        except (artifacts_mod.ArtifactError, OSError, ValueError,
                TypeError) as exc:
            failure = getattr(exc, "reason", type(exc).__name__)
            # If the generated path was unavailable but Envoy also supplied a
            # small inline image, use that independently verified copy.
            if image is not None and path is not None:
                try:
                    reference = self._capture_artifact_from_inline(
                        job["convoy_id"], image, owner, protection_id)
                    failure = None
                except (artifacts_mod.ArtifactError, OSError, ValueError,
                        TypeError) as fallback_exc:
                    failure = getattr(
                        fallback_exc, "reason", type(fallback_exc).__name__)
        if reference is None:
            return {
                "detail": description or "TOP capture completed",
                "capture": True, "artifact_unavailable": True,
                "artifact_reason": failure or "artifact_unavailable",
            }
        return {
            "detail": description or "TOP capture materialized",
            "capture": True, "spilled": True, "artifact": reference,
            "result_bytes": reference["size"],
        }

    def _materialize_node_result(self, result, job):
        """Persist terminal TD results without leaking remote paths/bytes."""
        if job.get("operation") == "capture_top":
            capture = self._materialize_capture_result(result, job)
            if capture is not None:
                return capture
        result = _json_safe(result)
        try:
            blob = json.dumps(
                result, sort_keys=True, allow_nan=False,
                separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError):
            return _bounded_result(result)
        if len(blob) <= 56 * 1024:
            return result
        try:
            reference = self.artifacts.put_bytes(
                job["convoy_id"], blob, mime_type="application/json",
                filename_hint=(str(job.get("operation") or "envoy")
                               + "-result.json"),
                owner=self._artifact_owner_for_job(job),
                protection_id=self._artifact_protection_for_job(job),
                protection_kind="unacknowledged_job")
        except (artifacts_mod.ArtifactError, OSError, ValueError, TypeError):
            reference = None
        if reference is not None:
            return {
                "detail": "full Envoy result is stored as a private "
                          "Convoy artifact",
                "spilled": True, "result_bytes": len(blob),
                "artifact": reference,
            }
        return _bounded_result(result)

    def _materialize_host_operation_result(self, result, job):
        """Keep a durable result under 64 KiB, spilling full JSON when clean.

        The artifact is private, quota-managed and path-free.  If quota is
        disabled/full, a bounded summary replaces the body rather than letting
        a subprocess inflate every job/status/peer response.
        """
        if not isinstance(result, dict) or not isinstance(
                result.get("ok"), bool):
            result = {"ok": False, "code": "internal_error",
                      "detail": "host operation failed safely"}
        result = _json_safe(result)
        try:
            blob = json.dumps(
                result, sort_keys=True, allow_nan=False,
                separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError):
            return {"ok": False, "code": "internal_error",
                    "detail": "host operation returned invalid data"}
        # Leave headroom for the delivery record and JSON escaping on the
        # peer/loopback response.  The product contract is under 64 KiB, not
        # merely "stdout was configured for 64 KiB".
        if len(blob) <= 56 * 1024:
            return result

        reference = None
        try:
            reference = self.artifacts.put_bytes(
                job["convoy_id"], blob,
                mime_type="application/json",
                filename_hint=(str(job.get("operation") or "host-operation")
                               + "-result.json"),
                owner=self._artifact_owner_for_job(job),
                protection_id=self._artifact_protection_for_job(job),
                protection_kind="unacknowledged_job")
        except (artifacts_mod.ArtifactError, OSError, ValueError, TypeError):
            reference = None

        summary = {key: result.get(key) for key in (
            "ok", "code", "detail", "capability", "operation", "target_id",
            "exit_code", "truncated", "observed_bytes", "duration_ms", "cwd")
                   if key in result}
        summary["result_bytes"] = len(blob)
        if reference is not None:
            summary.update({
                "spilled": True,
                "artifact": reference,
                "detail": "full host-operation output is stored as a "
                          "private Convoy artifact",
            })
            return summary
        summary.update({
            "truncated": True,
            "spill_failed": True,
            "detail": "host-operation output exceeded the inline limit and "
                      "artifact storage was unavailable",
            "head": blob[:2048].decode("utf-8", "replace"),
        })
        return summary

    def _resolve_host_dispatch(self, delivery_id, operation, outcome,
                               observed, job):
        """Persist a host-executor verdict.  Called with ``self.lock`` held."""
        ok = outcome.get("ok") is True
        updated = self.db.record_host_result(
            delivery_id, ok, observed, result=outcome)
        try:
            self.db.audit(
                "hostapp", "host_operation_dispatched",
                {"delivery_id": delivery_id, "operation": operation,
                 "node_id": job.get("node_id"),
                 "convoy_id": job.get("convoy_id"), "ok": ok,
                 "code": outcome.get("code"),
                 "spilled": outcome.get("spilled") is True})
        except Exception:
            pass
        self._drain_noted.pop(delivery_id, None)
        self._drain_backoff.pop(delivery_id, None)
        if updated.get("state") in hoststore.TERMINAL_STATES:
            self._release_operation_claim_locked(updated)
        return 200, {"ok": True, "dispatched": True, "job": updated}

    def _cancel_job_locked(self, delivery_id, *, expected_convoy_id=None,
                           expected_origin_host_id=None):
        """Cancel one owned delivery. CALLED WITH ``self.lock`` held.

        Namespace/origin mismatches deliberately collapse to ``unknown_job``:
        the caller must not learn that another Convoy or peer owns an ID.
        Queued work of every locus is cancellable because no execution has
        begun. Once TD work crosses the dispatch boundary there is no honest
        cancellation primitive yet, so that case is refused rather than
        pretending that dropping a host record stopped TouchDesigner.
        """
        job = self.db.get_job(delivery_id)
        if (job is None
                or (expected_convoy_id is not None
                    and job.get("convoy_id") != expected_convoy_id)
                or (expected_origin_host_id is not None
                    and job.get("origin_host_id") !=
                    expected_origin_host_id)):
            return 404, {"ok": False, "reason": "unknown_job",
                         "detail": delivery_id}
        state = job.get("state")
        if state == "queued":
            evidence = {
                "reason": "cancelled_before_dispatch",
                "detail": "the operation was cancelled before it started "
                          "and did not run",
                "operation": job.get("operation"), "at": self._now()}
            updated = self.db.mark_refused(delivery_id, evidence)
            self._release_operation_claim_locked(updated)
            # A prior dispatch pass may already have sent `acquire` and left
            # the job queued while Envoy re-registered.  Release that exact
            # lease after the cancellation is durable instead of holding TD
            # awake until its hard TTL expires.
            node = self.directory.lookup(job.get("node_id"))
            if isinstance(node, dict):
                self._send_node_wake(node, "release", delivery_id)
            return 200, {"ok": True, "cancelled": True,
                         "definitive": True, "job": updated}
        if (state == "dispatching"
                and job.get("operation") in HOST_CANCELABLE_OPERATIONS):
            event = self._hostop_cancel_events.get(delivery_id)
            if event is None:
                return 409, {"ok": False,
                             "reason": "cancellation_race",
                             "detail": "the host operation is claimed but "
                                       "no live cancellation handle is "
                                       "available"}
            event.set()
            return 202, {"ok": True, "cancel_requested": True,
                         "definitive": False,
                         "delivery_id": delivery_id}
        if state in hoststore.TERMINAL_STATES:
            return 200, {"ok": True, "cancelled": False,
                         "definitive": True, "job": job}
        return 409, {
            "ok": False, "reason": "cancellation_not_supported",
            "detail": "this TouchDesigner delivery has crossed its "
                      "cancellable host boundary; query the job for its "
                      "definitive outcome",
            "delivery_id": delivery_id, "state": state,
        }

    def cancel_host_job(self, body):
        """Cancel owned local work without waking TouchDesigner.

        Optional namespace/origin fields are defense-in-depth for callers that
        already resolved a job. The authenticated peer route always supplies
        both; legacy loopback callers may continue supplying only delivery_id.
        """
        try:
            delivery_id = text_field(body, "delivery_id")
            convoy_id = (text_field(
                body, "convoy_id", required=False) or None)
            origin_host_id = (text_field(
                body, "origin_host_id", required=False) or None)
        except Malformed as exc:
            return self._refuse("cancel", "malformed", exc.detail, 400)
        with self.lock:
            return self._cancel_job_locked(
                delivery_id, expected_convoy_id=convoy_id,
                expected_origin_host_id=origin_host_id)

    def _merged_arguments(self, raw_arguments, idempotency_key, async_spec):
        """The arguments actually put on the wire: (arguments, injected,
        dropped). Raises Malformed if the stored arguments are unusable.

        Pure and side-effect free, so a raise here can only ever be a
        refusal -- never a half-applied state.

        For a SYNC operation this is the caller's arguments verbatim.
        For an ASYNC one, host policy OVERRIDES the caller: `inject`
        forces the fields whose caller-supplied values would each break
        a different invariant (background False blocks the forward for
        the whole run, a caller idempotency_key breaks the node's 16.5
        anchor, override True bypasses the NODE's destructive gate), and
        `caller_args` -- when the entry declares one -- is the exhaustive
        list of caller fields the node's tool signature actually accepts.
        Anything else is DROPPED rather than forwarded: the host knows
        the operation's argument surface, and putting a field on the
        wire that the node must reject is a failure the host invented
        (panel finding, 2026-08-01: save_project's `inject: {}` overrode
        nothing, so junk rode through and killed the job at the node).
        This is not general schema validation (out of scope) -- it is
        the async injection contract being complete.

        The merged arguments are deliberately NOT persisted onto the job
        record: they are how THIS host relays it, not what was asked.
        """
        if raw_arguments is None:
            raw_arguments = {}
        if not isinstance(raw_arguments, dict):
            raise Malformed(f"arguments must be an object, got "
                            f"{type(raw_arguments).__name__}")
        # SECURITY: `override` bypasses the NODE's own multi-session
        # destructive gate (EnvoyExt._checkDestructiveGate short-circuits on
        # a truthy override). It is a LOCAL-only escape hatch and must NEVER
        # ride the Convoy relay -- CONVOY_PHASE3_PLAN section 6 lists "No
        # override=True over the relay" under WHAT MUST NOT BE BUILT. Strip a
        # caller-supplied override from EVERY relayed operation, sync or
        # async, so the target always applies its own gate. The async
        # `inject` contract may still force override=False deliberately.
        if not async_spec:
            base = dict(raw_arguments)
            dropped = ["override"] if base.pop("override", None) is not None \
                else []
            return base, [], dropped
        allowed = async_spec.get("caller_args")
        dropped = []
        base = dict(raw_arguments)
        override_present = base.pop("override", None) is not None
        if allowed is not None:
            dropped = sorted(k for k in base if k not in allowed)
            base = {k: v for k, v in base.items() if k in allowed}
        base.update(async_spec.get("inject") or {})
        key_arg = async_spec.get("key_arg", "idempotency_key")
        base[key_arg] = idempotency_key
        injected = sorted(set(async_spec.get("inject") or {}) | {key_arg})
        if override_present:
            dropped = sorted(set(dropped) | {"override"})
        return base, injected, dropped

    def _note_dispatch_event(self, delivery_id, event, detail):
        """Audit a REPEATING per-job dispatch refusal once per transition.

        Called with self.lock held. The drain loop re-attempts refused
        and deferred jobs on a timer, so auditing every attempt grows
        audit.jsonl without bound on a steady failing state (a portless
        node is today's NORMAL state -- nothing in TD auto-registers
        yet). The noted event clears when the job resolves (see
        _resolve_dispatch) or falls out of the queue, so a recurrence
        after a recovery is audited afresh.

        The dedupe key is (event, reason), not the event name alone: a
        job whose REFUSAL REASON changes (deferred -> unknown_node, one
        registry refusal -> another) is a genuine transition the trail
        must show -- deduping on the event name swallowed it."""
        key = (event, (detail or {}).get("reason"))
        if self._drain_noted.get(delivery_id) == key:
            return
        try:
            self.db.audit("hostapp", event,
                          dict(detail, delivery_id=delivery_id))
        except Exception:
            # Audit trouble must neither break the dispatch (an escape
            # here surfaced as an unnamed 500 on a refusal path) nor
            # poison the dedupe: marking noted for a line never written
            # would swallow the NEXT attempt's audit too. Leave unnoted
            # so a later attempt retries the append.
            return
        self._drain_noted[delivery_id] = key
        while len(self._drain_noted) > self._drain_map_cap:
            # Bounded: on a loop-off host nothing prunes, and the map
            # must not grow one entry per refused delivery forever. The
            # cap scales with the live queue (see DRAIN_MAP_FLOOR) so
            # eviction can no longer cascade through live entries.
            self._drain_noted.pop(next(iter(self._drain_noted)))

    def _requeue_claim(self, delivery_id, operation, observed, reason,
                       event, detail, cause):
        """Release a claim back to QUEUED and refuse -- the shared
        never-terminalise path. Called WITH self.lock held.

        Two callers, one honesty contract: UNREACHABLE (the connection
        was refused, so the request was never delivered) and an async
        answer carrying no node-job handle (the node may have started
        work whose id we lost, and the retry's idempotency key provably
        recovers it). Both must return the job to the queue rather than
        burn it -- indeterminate is terminal and precious (16.4).

        `cause` is the human phrase naming why, spliced into the failure
        responses; `reason`/`event`/`detail` shape the normal refusal.
        Returns (code, payload).
        """
        try:
            released = self.db.release_claim(delivery_id)
        except Exception as e:
            # The REQUEUE write failed. The op was never delivered (or
            # is recoverable by key), so this must NOT decay to
            # indeterminate (the forbidden collapse -- round-3 panel
            # reproduced it under natural file contention). Park the
            # release: this attempt's flight marker is kept (the finally
            # skips parked ids) so the reaper cannot resolve the claim,
            # and the drain pass retries the release until the disk
            # heals. Named residual: a host that DIES before the retry
            # lands leaves a claim the boot sweep resolves to
            # indeterminate -- never-delivered knowledge cannot outlive
            # the process without a write, which is exactly what is
            # failing.
            failure = f"{type(e).__name__}: {e}"
            self._pending_release[delivery_id] = failure
            try:
                self.db.audit("hostapp", "dispatch_release_failed",
                              {"delivery_id": delivery_id,
                               "reason": reason,
                               "detail": failure[:256]})
            except Exception:
                pass
            return 503, {"ok": False, "reason": "store_unavailable",
                         "detail": f"{cause} but the requeue write failed "
                                   f"({failure}); the drain loop will "
                                   f"retry the requeue"}
        if released is None:
            # The CAS did not release. Distinguish UNREADABLE from GONE
            # before concluding anything: get_job swallows read errors
            # into None, and treating a transiently unreadable file as a
            # lost claim handed a never-delivered job to the reaper
            # (round-4 panel).
            current = self.db.get_job(delivery_id)
            state = current.get("state") if current else None
            if state == "dispatching" or (
                    current is None
                    and self.db.job_file_exists(delivery_id)):
                # Still claimed on disk (or unreadable-but-present,
                # conservatively the same): the CAS's READ failed, not
                # the claim. Park it exactly like a failed release write.
                self._pending_release[delivery_id] = (
                    "release CAS could not read the record")
                try:
                    self.db.audit("hostapp", "dispatch_release_failed",
                                  {"delivery_id": delivery_id,
                                   "reason": reason,
                                   "detail": "release CAS read failure"})
                except Exception:
                    pass
                return 503, {"ok": False,
                             "reason": "store_unavailable",
                             "detail": f"{cause} but the requeue could not "
                                       f"read the record; the drain loop "
                                       f"will retry"}
            state = state if current else "missing"
            try:
                self.db.audit("hostapp", "dispatch_claim_lost",
                              {"delivery_id": delivery_id,
                               "reason": reason, "state": state})
            except Exception:
                pass
            return 409, {"ok": False, "reason": "claim_lost",
                         "detail": f"{cause}, but this dispatcher no "
                                   f"longer held the claim; the job is "
                                   f"now {state!r}"}
        try:
            # The retry loop's ONLY on-record evidence. The audit line
            # is deduped to one per transition (that is what keeps
            # audit.jsonl bounded on a steady failing state), so without
            # this a job requeuing forever looks brand new in /jobs.
            # Best-effort and strictly after the release: a bookkeeping
            # write must never alter or divert dispatch state.
            self.db.record_dispatch_note(delivery_id, reason, observed)
        except Exception:
            pass
        self._note_dispatch_event(delivery_id, event,
                                  {"operation": operation})
        self._set_drain_backoff(delivery_id, observed + self.drain_backoff_s)
        return 409, {"ok": False, "reason": reason, "detail": detail}

    def _resolve_dispatch(self, delivery_id, operation, outcome, observed,
                          detail, async_spec=None, job=None):
        """Phase c of dispatch_job -- called WITH self.lock held."""
        if job is None:
            job = self.db.get_job(delivery_id) or {
                "delivery_id": delivery_id, "operation": operation,
                "convoy_id": "unavailable",
            }
        if outcome is mcpclient.UNREACHABLE:
            # The node refused the connection: the request was never
            # delivered, so the op did NOT run. Release the claim -- the
            # job returns to QUEUED to retry. A transient node-down must
            # never burn a job to indeterminate (or, worse, a fabricated
            # verdict).
            return self._requeue_claim(
                delivery_id, operation, observed,
                reason="node_unreachable", event="dispatch_unreachable",
                detail="the node's Envoy refused the connection; the job "
                       "stays queued to retry",
                cause="the node refused the connection")
        handle = None
        if (async_spec is not None and isinstance(outcome, dict)
                and outcome.get("ok") is True):
            # An async operation must answer with a node-job HANDLE. The
            # payload arrives as ok=True even when it is a refusal --
            # Envoy's MCP layer sets isError only when a tool RAISES,
            # and these tools RETURN {'error': ...} dicts. So 'ok' means
            # the CALL completed, never that the operation succeeded:
            # the payload itself has to be inspected.
            handle = _node_job_handle(outcome.get("result"))
            if handle is None:
                # No handle. The node refused before minting -- OR it
                # started a run whose id we lost (the node's own 30s
                # main-thread timeout answers exactly like a refusal).
                # Recording either 'failed' or 'indeterminate' would be
                # a guess; requeue instead, because the retry carries
                # the same idempotency_key and the node's index hands
                # back the original handle (proven by
                # test_a_requeued_async_delivery_recovers_the_lost_handle).
                # Checked BEFORE the bookkeeping is cleared below, so
                # the repeat audits once per transition like every other
                # steady refusal.
                #
                # NAMED RESIDUAL (panel, 2026-08-01): the retry only
                # PROVABLY recovers when the node actually minted a job.
                # Part of the 'refused before minting' family is
                # permanent by construction -- an idempotency_key
                # already bound to a different operation is caused BY
                # the key the retry keeps sending; the multi-session
                # gate and 'Job records unavailable' persist as long as
                # their cause does. Those requeue at the backoff rate
                # forever. That is deliberate under A-15 (the host has
                # no node-originated evidence to terminalise on, and
                # inventing one is the forbidden collapse); bounding it
                # is the deferred host reaper's job (A-15 item b). What
                # this slice owes such a job is VISIBILITY, and that is
                # record_dispatch_note in _requeue_claim: attempts and
                # last_attempt on the delivery record, because the audit
                # line is deduped to one by design.
                return self._requeue_claim(
                    delivery_id, operation, observed,
                    reason="no_node_job_handle", event="dispatch_no_handle",
                    detail="the node answered an async operation without a "
                           "job handle; the job stays queued and the retry "
                           "reconciles on its idempotency key",
                    cause="the node returned no job handle")
        # The job is resolving -- clear its refusal bookkeeping so a
        # future recurrence is audited afresh.
        self._drain_noted.pop(delivery_id, None)
        self._drain_backoff.pop(delivery_id, None)
        if outcome is None:
            # Transport failure AFTER the request may have been sent:
            # the operation MAY have executed, and we have no result.
            # Indeterminate is the only honest terminal -- never a
            # silent retry (it might double-run) or a fake fail.
            updated = self.db.mark_indeterminate(delivery_id, {
                "reason": "no_response",
                "detail": detail or "no response from the node's Envoy",
                "operation": operation})
            self._release_operation_claim_locked(updated)
            try:
                self.db.audit("hostapp", "dispatch_indeterminate",
                              {"delivery_id": delivery_id,
                               "operation": operation})
            except Exception:
                pass    # the outcome is durably written; audits never
                        # alter dispatch state (round-3 panel)
            return 200, {"ok": True, "dispatched": True, "job": updated}
        if handle is not None:
            # THE HANDOFF. The node minted a job and named it; the host
            # mirrors that with the node's provenance and stops here --
            # the poller owns the outcome from now on.
            #
            # 'running' the instant the handle lands is load-bearing:
            # the job LEAVES 'dispatching' inside this phase, before any
            # drain pass can run, so the stranded-claim reaper (which
            # resolves dispatching claims with no forward in flight)
            # can never see it. Parking an awaited node job at
            # 'dispatching' would have it reaped to indeterminate on the
            # very next tick.
            node_job_id, node_status = handle
            # AUDIT FIRST, then write. The handle is the only key to an
            # outcome the node can still answer for 24h; audit.jsonl is
            # append-only and survives a process death that the parked
            # retry below would not. Auditing after the write meant a
            # failed write threw the handle away entirely (panel probe,
            # 2026-08-01).
            try:
                self.db.audit(
                    "hostapp", "dispatch_node_job_started",
                    {"delivery_id": delivery_id, "operation": operation,
                     "node_job_id": node_job_id, "status": node_status,
                     "kind": (async_spec or {}).get("kind")})
            except Exception:
                pass    # audits never alter dispatch state (round-3 panel)
            result = _bounded_result(self._handoff_result(outcome.get(
                "result"), node_status))
            try:
                updated = self.db.record_node_verdict(
                    delivery_id, node_status, node_job_id=node_job_id,
                    observed_at=observed, result=result)
            except Exception as e:
                # The mirror write failed -- transiently, most likely
                # (the sharing-violation class _requeue_claim parks
                # for). PARK it: the node is RUNNING this job and we
                # hold its handle, so letting this fall through to the
                # indeterminate downgrade would terminalise a run the
                # host provably observed AND destroy the only key to
                # its outcome. Claim stays on disk, flight marker stays
                # up (the finally skips parked ids, so the reaper cannot
                # touch it), and the drain pass retries the mirror.
                # Named residual: a host that DIES before the retry
                # lands leaves a claim the boot sweep resolves to
                # indeterminate -- but the handle is already in the
                # audit trail above, so the run stays findable by hand.
                failure = f"{type(e).__name__}: {e}"
                self._pending_handoff[delivery_id] = {
                    "node_job_id": node_job_id, "node_status": node_status,
                    "observed_at": observed, "result": result,
                    "detail": failure}
                try:
                    self.db.audit("hostapp", "dispatch_handoff_parked",
                                  {"delivery_id": delivery_id,
                                   "node_job_id": node_job_id,
                                   "status": node_status,
                                   "detail": failure[:256]})
                except Exception:
                    pass
                return 503, {"ok": False, "reason": "store_unavailable",
                             "detail": f"the node started {node_job_id!r} "
                                       f"but the running mirror could not "
                                       f"be written ({failure}); the drain "
                                       f"loop will retry the mirror"}
            started = updated.get("state") == "running"
            if updated.get("state") in hoststore.TERMINAL_STATES:
                self._release_operation_claim_locked(updated)
            return 200, {"ok": True, "dispatched": True, "started": started,
                         "job": updated}
        ok = outcome.get("ok")      # a real bool -- phase b enforced it
        result = outcome.get("result") if ok else {
            "error": outcome.get("error")}
        result = self._materialize_node_result(result, job)
        updated = self.db.record_sync_result(delivery_id, ok, observed,
                                             result=result)
        self._release_operation_claim_locked(updated)
        try:
            self.db.audit("hostapp", "dispatched",
                          {"delivery_id": delivery_id, "ok": ok,
                           "operation": operation})
        except Exception:
            # The node's verdict is durably recorded. An audit-append
            # failure after it must never reach the downgrade path --
            # that DESTROYED a real node verdict (round-3 panel, A-15).
            pass
        return 200, {"ok": True, "dispatched": True, "job": updated}

    @staticmethod
    def _handoff_result(payload, node_status):
        """The handle payload as it is mirrored, with an honest note when
        a TERMINAL handle carries no outcome.

        A 16.5 idempotency RECONCILE hands back {'job_id', 'status':
        'done', 'hint'} -- a real node verdict, but with no result body,
        because the node is answering "you already asked for this" and
        not "here is what happened". Recording it terminal is right (the
        node authored the status), but the record would then hold no
        outcome at all, and the poller never revisits a terminal job. So
        say so, on the record, and name where the outcome still lives
        for the node's 24h window. Polling for the body instead would
        mean either a forward under the lock or writing a 'running'
        state the node never reported."""
        if node_status == "running" or not isinstance(payload, dict):
            return payload
        if payload.get("result") is not None or payload.get("error"):
            return payload      # the node did carry an outcome
        return dict(payload, detail=(
            "the node reconciled this delivery to a run that had already "
            "finished, so its answer carried no result body; fetch "
            "get_job_status(job_id) on the node within its 24h retention "
            "for the outcome"))

    def _downgrade_failed_recording(self, delivery_id, operation, error):
        """Phase c raised: the store could not write the honest outcome.
        The durable record can no longer reflect what was observed, so
        downgrade the claim to indeterminate with the failure as
        evidence. If even that write fails, leave the stranded claim for
        drain_once's reaper (or the load-time sweep) -- never let the
        exception escape as an unexplained 500 that strands the job
        silently."""
        detail = f"{type(error).__name__}: {error}"
        try:
            with self.lock:
                current = self.db.get_job(delivery_id)
                state = current.get("state") if current else None
                if state != "dispatching":
                    # The durable record already resolved -- a verdict
                    # landed, or the claim was released -- and the raise
                    # was something else. NEVER overwrite what is on
                    # disk with a host guess: doing so destroyed a real
                    # node verdict and re-terminalised a released job
                    # (round-3 panel, A-15 / 16.4).
                    try:
                        self.db.audit("hostapp",
                                      "dispatch_recording_glitch",
                                      {"delivery_id": delivery_id,
                                       "state": state,
                                       "detail": detail[:256]})
                    except Exception:
                        pass
                    if current is not None:
                        return 200, {"ok": True,
                                     "dispatched": state != "queued",
                                     "job": current}
                    return 500, {"ok": False,
                                 "reason": "recording_failed",
                                 "detail": detail}
                updated = self.db.mark_indeterminate(delivery_id, {
                    "reason": "verdict_recording_failed",
                    "detail": detail, "operation": operation})
                self._release_operation_claim_locked(updated)
                try:
                    self.db.audit("hostapp", "dispatch_recording_failed",
                                  {"delivery_id": delivery_id,
                                   "detail": detail[:256]})
                except Exception:
                    pass    # the downgrade LANDED; the response below
                            # must not claim it is still pending
            return 500, {"ok": False, "reason": "recording_failed",
                         "detail": f"the node outcome could not be "
                                   f"recorded ({detail}); the job is "
                                   f"marked indeterminate"}
        except Exception:
            return 500, {"ok": False, "reason": "recording_failed",
                         "detail": f"the node outcome could not be "
                                   f"recorded and the downgrade write "
                                   f"also failed ({detail}); the job "
                                   f"remains claimed until reaped"}

    # -- poll: observe a node job the host handed off -------------------

    def poll_job(self, delivery_id):
        """Ask the node what became of ONE running node job, and mirror
        the answer.

        SELF-LOCKING, exactly like dispatch_job, and for the same reason
        (/poll sits outside do_POST's lock block; calling this from
        inside `with app.lock:` deadlocks). Three phases: read under the
        lock, forward outside it, resolve under it again.

        What is DELIBERATELY absent, versus dispatch:

          - no durable claim. A poll is a READ; two concurrent polls are
            harmless, and a host that dies mid-poll must leave the job
            running -- the node owns the execution and will still answer
            for it. _polls_in_flight is in-memory only: it avoids
            duplicate I/O and keeps a late answer from regressing state.
          - no re-gate. The gate decides whether work may START; this
            job already did. Re-gating would refuse to LOOK at a running
            node job and abandon it to limbo. The bound is that this
            calls exactly one hardcoded read-only operation, with a
            shape-validated id the node itself minted.
        """
        wake_lease = False
        # -- phase a: read the job, node and port -- under the lock -----
        with self.lock:
            if (not delivery_id or not isinstance(delivery_id, str)
                    or len(delivery_id) > MAX_ID_CHARS):
                return self._refuse("poll", "malformed",
                                    "delivery_id must be a string", 400)
            job = self.db.get_job(delivery_id)
            if job is None:
                # Audited directly, not through the dedupe map -- ghost
                # ids would accumulate entries nothing prunes (the same
                # reasoning as dispatch_job's unknown_job).
                try:
                    self.db.audit("hostapp", "poll_refused",
                                  {"reason": "unknown_job",
                                   "delivery_id": delivery_id})
                except Exception:
                    pass    # named 404 beats an unaudited 500
                return 404, {"ok": False, "reason": "unknown_job",
                             "detail": delivery_id}
            if job.get("state") != "running":
                # Queued, claimed, or already settled -- not ours.
                return 200, {"ok": True, "polled": False, "job": job}
            node_job_id = job.get("node_job_id")
            if (not isinstance(node_job_id, str)
                    or not NODE_JOB_ID_RE.match(node_job_id)):
                # Unreachable through this code (record_node_verdict
                # demands the provenance), so this is hand-edited or
                # corrupt state. Refuse loudly rather than forward a
                # guessed id.
                self._note_poll_event(delivery_id, "poll_missing_provenance",
                                      {"reason": "no_node_provenance",
                                       "node_job_id": str(node_job_id)[:64]})
                self._set_poll_backoff(delivery_id,
                                       self._now() + self.poll_backoff_s)
                return 409, {"ok": False, "reason": "no_node_provenance",
                             "detail": "the job is running but carries no "
                                       "valid node job id; refusing to poll "
                                       "for an id the node never minted"}
            if delivery_id in self._polls_in_flight:
                return 200, {"ok": True, "polled": False,
                             "reason": "poll_in_flight", "job": job}
            node = self.directory.lookup(job["node_id"])
            if node is None:
                self._note_poll_event(delivery_id, "poll_refused",
                                      {"reason": "unknown_node",
                                       "node_id": job["node_id"]})
                self._set_poll_backoff(delivery_id,
                                       self._now() + self.poll_backoff_s)
                return 404, {"ok": False, "reason": "unknown_node",
                             "detail": job["node_id"]}
            port = node.get("envoy_port")
            if node.get("perform_mode"):
                if not node.get("wake_active") or not port:
                    wake = self._send_node_wake(
                        node, "acquire", delivery_id)
                    if not wake.get("ok"):
                        reason = wake.get("reason") or \
                            "remote_wake_unavailable"
                        self._note_poll_event(
                            delivery_id, "poll_deferred",
                            {"reason": reason})
                        self._set_poll_backoff(
                            delivery_id, self._now() + self.poll_backoff_s)
                        return 409, {
                            "ok": False, "reason": reason,
                            "detail": "the running job's node is in Perform "
                                      "Mode and cannot be remotely awakened",
                        }
                    self._note_poll_event(
                        delivery_id, "node_wake_requested",
                        {"reason": "perform_mode"})
                    self._set_poll_backoff(
                        delivery_id, self._now() + WAKE_RETRY_S)
                    return 409, {
                        "ok": False, "reason": "node_waking",
                        "detail": "the target accepted a Perform wake; the "
                                  "running job remains unchanged",
                    }
                self._send_node_wake(node, "touch", delivery_id)
                wake_lease = True
            if not port:
                # envoy_port is PER-LAUNCH and never persisted, so after
                # a host restart every poll defers here until the node
                # re-registers. The job stays running -- honest: the run
                # is still the node's, we simply cannot ask yet.
                self._note_poll_event(delivery_id, "poll_deferred",
                                      {"reason": "node_endpoint_unknown"})
                self._set_poll_backoff(delivery_id,
                                       self._now() + self.poll_backoff_s)
                return 409, {"ok": False, "reason": "node_endpoint_unknown",
                             "detail": "the node has not registered its "
                                       "Envoy port; the job stays running"}
            self._poll_counter += 1
            attempt = self._poll_counter
            self._polls_in_flight[delivery_id] = attempt

        try:
            # -- phase b: the poll forward, OUTSIDE the lock ------------
            detail = ""
            try:
                outcome = self.forwarder(port, POLL_OPERATION,
                                         {"job_id": node_job_id})
            except Exception as e:      # a forwarder must not crash the pass
                outcome = None
                detail = f"{type(e).__name__}: {e}"
            if (outcome is not None
                    and outcome is not mcpclient.UNREACHABLE
                    and not (isinstance(outcome, dict)
                             and isinstance(outcome.get("ok"), bool))):
                detail = (f"forwarder returned {type(outcome).__name__} "
                          f"without a boolean 'ok' verdict")
                outcome = None
            observed = self._now()

            # -- phase c: mirror the answer, under the lock -------------
            try:
                with self.lock:
                    response = self._resolve_poll(
                        delivery_id, node_job_id, outcome, observed, detail)
            except Exception as e:
                # Contrast _downgrade_failed_recording: a dispatch holds
                # a CLAIM, so a failed phase-c write must resolve it or
                # the job strands. A poll holds NOTHING -- the record is
                # exactly as the node last left it, so the honest answer
                # is to leave it running and let the next poll retry.
                failure = f"{type(e).__name__}: {e}"
                try:
                    with self.lock:
                        self.db.audit("hostapp", "poll_recording_failed",
                                      {"delivery_id": delivery_id,
                                       "detail": failure[:256]})
                except Exception:
                    pass
                response = (500, {
                    "ok": False, "reason": "poll_recording_failed",
                    "detail": f"the node's answer could not be recorded "
                              f"({failure}); the job is unchanged and stays "
                              "running"})
            if wake_lease and not self._wake_result_running(response):
                self._send_node_wake(node, "release", delivery_id)
            return response
        finally:
            with self.lock:
                # ATTEMPT-scoped, like _in_flight: a slow poll finishing
                # after a newer one started must not clear the newer
                # one's marker.
                if self._polls_in_flight.get(delivery_id) == attempt:
                    del self._polls_in_flight[delivery_id]

    def _note_poll_event(self, delivery_id, event, detail):
        """Audit a REPEATING per-job poll event once per transition.

        Called with self.lock held. A clone of _note_dispatch_event
        against its OWN map: the poll pass re-visits the same running
        jobs on a timer, and an unreachable node (today's normal state
        between TD restarts) would otherwise append a line per tick
        forever. Same (event, reason) dedupe key, so a CHANGED failure
        is still a visible transition."""
        key = (event, (detail or {}).get("reason"))
        if self._poll_noted.get(delivery_id) == key:
            return
        try:
            self.db.audit("hostapp", event,
                          dict(detail, delivery_id=delivery_id))
        except Exception:
            # Never poison the dedupe with a line that was not written:
            # leave it unnoted so a later poll retries the append.
            return
        self._poll_noted[delivery_id] = key
        if len(self._poll_noted) > 2048:
            self._poll_noted.pop(next(iter(self._poll_noted)))

    def _resolve_poll(self, delivery_id, node_job_id, outcome, observed,
                      detail):
        """Phase c of poll_job -- called WITH self.lock held.

        THE ASYMMETRY WITH DISPATCH, stated once and deliberately:

            a dispatch that got no response may have EXECUTED the
            operation, so the only honest terminal is indeterminate. A
            poll is a READ. A read that failed teaches nothing at all --
            the node's own job record is durable and will answer when
            the node returns. So an unreachable or unanswered poll NEVER
            writes anything: the job stays running.

        This is not an oversight in the symmetry; inverting it would
        burn a healthy 20-minute test run every time the node blipped --
        and save_project restarts the Envoy server under itself, so the
        first poll after one is EXPECTED to be unreachable.

        Two answers do terminalise, and both carry the node's own
        evidence rather than a host guess: the node repeatedly and
        durably not knowing the job (node_forgot_job, gated by
        observations AND a grace window), and the node's own derived
        stale-running verdict.
        """
        if outcome is mcpclient.UNREACHABLE:
            return self._defer_poll(delivery_id, "poll_unreachable",
                                    "node_unreachable",
                                    {"node_job_id": node_job_id},
                                    observed,
                                    "the node's Envoy refused the "
                                    "connection; the job stays running "
                                    "and the next poll retries")
        if outcome is None:
            return self._defer_poll(delivery_id, "poll_no_response",
                                    "poll_no_response",
                                    {"node_job_id": node_job_id,
                                     "reason": "transport",
                                     "detail": (detail
                                                or "no response")[:256]},
                                    observed,
                                    "no usable answer about the node job; "
                                    "the job stays running and the next "
                                    "poll retries")
        if outcome.get("ok") is not True:
            # A protocol-level refusal (JSON-RPC error / isError): the
            # CALL failed, so we learned nothing about the job either.
            return self._defer_poll(delivery_id, "poll_no_response",
                                    "poll_no_response",
                                    {"node_job_id": node_job_id,
                                     "reason": "node_error",
                                     "detail": str(outcome.get("error")
                                                   )[:256]},
                                    observed,
                                    "the node refused the status call; the "
                                    "job stays running")
        payload = outcome.get("result")
        handle = _node_job_handle(payload)
        if handle is None:
            if _is_unknown_job_answer(payload):
                # "no job with id ..." -- RULE 2 territory. Gated on the
                # node's ACTUAL not-found answer, not on any error key:
                # this is the one path that turns a read into a terminal
                # record, and the host distrusts node-supplied data
                # everywhere else (NODE_JOB_ID_RE, the id-mismatch
                # check). An unrecognised node error defers below, like
                # every other answer we cannot read.
                return self._resolve_unknown_job(delivery_id, node_job_id,
                                                 payload, observed)
            if (isinstance(payload, dict)
                    and payload.get("error") is not None):
                return self._defer_poll(delivery_id, "poll_no_response",
                                        "poll_no_response",
                                        {"node_job_id": node_job_id,
                                         "reason": "node_error_payload",
                                         "detail": str(payload["error"]
                                                       )[:256]},
                                        observed,
                                        "the node reported its own trouble "
                                        "rather than this job's status; the "
                                        "job stays running")
            # Anything else is an answer we cannot read. Learning
            # nothing is not evidence of anything.
            return self._defer_poll(delivery_id, "poll_no_response",
                                    "poll_no_response",
                                    {"node_job_id": node_job_id,
                                     "reason": "unreadable"},
                                    observed,
                                    "the node's answer was not a job "
                                    "record; the job stays running")
        answered_id, node_status = handle
        if answered_id != node_job_id:
            # Debris (a confused node, a crossed response). Mirroring it
            # would file ANOTHER run's verdict against this delivery.
            return self._defer_poll(delivery_id, "poll_id_mismatch",
                                    "poll_id_mismatch",
                                    {"node_job_id": node_job_id,
                                     "answered": answered_id},
                                    observed,
                                    f"the node answered about "
                                    f"{answered_id!r}, not {node_job_id!r}; "
                                    f"ignored")
        if node_status == "running":
            if isinstance(payload, dict) and payload.get("stale") is True:
                # RULE 3: the NODE's own derived verdict -- its
                # completion poll chain died. The host mirrors that as
                # indeterminate carrying the node's payload; it does NOT
                # invent a second host-side staleness horizon.
                updated = self.db.mark_indeterminate(delivery_id, {
                    "reason": "node_reported_stale",
                    "detail": "the node's own record reports this run as "
                              "stale (running far past its expected "
                              "lifetime); the outcome is unobservable",
                    "node_job_id": node_job_id,
                    "node_record": _bounded_result(payload)})
                self._release_operation_claim_locked(updated)
                self._forget_poll(delivery_id)
                try:
                    self.db.audit("hostapp", "poll_stale_indeterminate",
                                  {"delivery_id": delivery_id,
                                   "node_job_id": node_job_id})
                except Exception:
                    pass    # the outcome is durably written; audits
                            # never alter poll state
                return 200, {"ok": True, "polled": True, "job": updated}
            # Still running: a clean observation, and nothing DECISION-
            # RELEVANT changed -- same state, same handle. The payload
            # itself always differs (the node stamps a fresh age_s, and
            # progress fields may move), so the skip deliberately trades
            # /jobs-view freshness for I/O: a running record's mirrored
            # payload can lag reality by up to POLL_MIRROR_REFRESH_S.
            # No reader decides on a running result or observed_at
            # (verified in round-2 review); stale:true and terminals
            # always write immediately. Rewriting the file every poll
            # cost 6-38 ms per poll per job, forever (measured, 200
            # running jobs). So write only when something real changed
            # or the mirror has gone stale past POLL_MIRROR_REFRESH_S.
            # The freshness comes off the RECORD, not an in-memory map:
            # no map to bound, and it survives a host restart. When we
            # do write, _apply_state reads the file anyway, so the skip
            # is strictly fewer I/O operations, never more.
            current = self.db.get_job(delivery_id)
            last = current.get("observed_at") if current else None
            unchanged = (current is not None
                         and current.get("state") == "running"
                         and current.get("node_job_id") == node_job_id
                         and isinstance(last, (int, float))
                         and (observed - last) < POLL_MIRROR_REFRESH_S)
            if unchanged:
                updated = current
            else:
                # Refresh the mirror FIRST -- if that write fails, the
                # bookkeeping must stay exactly as it was so the next
                # pass retries unpaced.
                updated = self.db.record_node_verdict(
                    delivery_id, "running", node_job_id=node_job_id,
                    observed_at=observed, result=_bounded_result(payload))
            # Clear the failure bookkeeping (a later failure is a fresh
            # transition) and pace the node.
            self._poll_noted.pop(delivery_id, None)
            self._poll_unknown.pop(delivery_id, None)
            self._set_poll_backoff(delivery_id, observed + self.poll_backoff_s)
            return 200, {"ok": True, "polled": True, "refreshed": not unchanged,
                         "job": updated}
        # done / error: the node's verdict on its own run. The terminal
        # payload is COPIED in -- the node's fetch-by-id window closes
        # at 24h, so the host's record is what survives.
        terminal_job = self.db.get_job(delivery_id) or {
            "delivery_id": delivery_id,
            "convoy_id": "unavailable",
            "operation": POLL_OPERATION,
        }
        terminal_result = self._materialize_node_result(
            payload, terminal_job)
        updated = self.db.record_node_verdict(
            delivery_id, node_status, node_job_id=node_job_id,
            observed_at=observed, result=terminal_result)
        self._release_operation_claim_locked(updated)
        self._forget_poll(delivery_id)
        try:
            self.db.audit("hostapp", "poll_finished",
                          {"delivery_id": delivery_id,
                           "node_job_id": node_job_id,
                           "status": node_status,
                           "state": updated.get("state")})
        except Exception:
            pass        # the verdict is durably written; an audit
                        # failure must never destroy it (A-15)
        return 200, {"ok": True, "polled": True, "job": updated}

    def _defer_poll(self, delivery_id, event, reason, note, observed,
                    detail):
        """Leave the job RUNNING, write nothing, audit once per
        transition, and pace the next attempt. Every non-terminal poll
        outcome funnels here, so there is exactly ONE place that could
        ever be mistakenly taught to terminalise.

        The note's own `reason` WINS over the response reason when it
        carries one: the dedupe key is (event, reason), so a
        poll_no_response that changes cause (transport -> the node
        refusing the status call) is a genuine transition the trail must
        show. Defaulting them all to the response reason collapsed
        exactly that distinction."""
        self._note_poll_event(delivery_id, event,
                              dict({"reason": reason}, **note))
        self._set_poll_backoff(delivery_id, observed + self.poll_backoff_s)
        return 409, {"ok": False, "reason": reason, "detail": detail}

    def _set_poll_backoff(self, delivery_id, not_before):
        """Pace the next poll of one job, BOUNDED. Called with the lock.

        The other two poll maps evict past 2048; this one was pruned
        only by poll_once's end-of-pass sweep -- and the drain loop is
        off by default, so a controller driving /poll by hand had
        nothing pruning it at all. Same cap, same eviction, one writer."""
        self._poll_backoff[delivery_id] = not_before
        if len(self._poll_backoff) > 2048:
            self._poll_backoff.pop(next(iter(self._poll_backoff)))

    def _set_drain_backoff(self, delivery_id, not_before):
        """Pace the next drain attempt of one job, BOUNDED. Called with
        the lock. Same rationale as _set_poll_backoff: the end-of-pass
        prune is the only other reclaim and the drain loop is off by
        default, so hand-driven /dispatch refusals grew the map one
        entry per distinct delivery forever (round-2 verify probe:
        3000 portless dispatches -> 3000 entries)."""
        self._drain_backoff[delivery_id] = not_before
        while len(self._drain_backoff) > self._drain_map_cap:
            # Queue-scaled cap, same as _drain_noted (DRAIN_MAP_FLOOR).
            self._drain_backoff.pop(next(iter(self._drain_backoff)))

    def _forget_poll(self, delivery_id):
        """Drop a settled job's poll bookkeeping. Called with the lock."""
        self._poll_backoff.pop(delivery_id, None)
        self._poll_noted.pop(delivery_id, None)
        self._poll_unknown.pop(delivery_id, None)

    def _resolve_unknown_job(self, delivery_id, node_job_id, payload,
                             observed):
        """RULE 2: the node says it has no such job.

        That is real evidence -- but not YET proof the run is lost. A
        node restarting between record flushes, the 24h retention, and
        the transient dark-job layer (a repo root the node had not
        resolved when it wrote the record) all produce one-off unknowns.
        So terminalisation needs BOTH a repeated observation and a grace
        window, and the evidence written down is the node's own message.

        A host restart resets the counter, which only ever DELAYS
        terminalisation -- safe in the direction that matters.
        """
        entry = self._poll_unknown.get(delivery_id)
        if entry is None or entry.get("node_job_id") != node_job_id:
            entry = {"first": observed, "count": 0,
                     "node_job_id": node_job_id}
        entry["count"] += 1
        entry["message"] = str(payload.get("error"))[:256]
        self._poll_unknown[delivery_id] = entry
        if len(self._poll_unknown) > 2048:
            self._poll_unknown.pop(next(iter(self._poll_unknown)))
        aged = (observed - entry["first"]) >= POLL_UNKNOWN_GRACE_S
        if entry["count"] >= POLL_UNKNOWN_MIN_OBSERVATIONS and aged:
            updated = self.db.mark_indeterminate(delivery_id, {
                "reason": "node_forgot_job",
                "detail": f"the node has no record of {node_job_id!r} "
                          f"({entry['message']}); the run may have "
                          f"completed unobserved",
                "node_job_id": node_job_id,
                "node_message": entry["message"],
                "first_unknown_at": entry["first"],
                "observations": entry["count"]})
            self._release_operation_claim_locked(updated)
            self._forget_poll(delivery_id)
            try:
                self.db.audit("hostapp", "poll_node_forgot_job",
                              {"delivery_id": delivery_id,
                               "node_job_id": node_job_id,
                               "observations": entry["count"]})
            except Exception:
                pass    # durably written; audits never alter poll state
            return 200, {"ok": True, "polled": True, "job": updated}
        return self._defer_poll(
            delivery_id, "poll_node_forgot_job_pending",
            "node_forgot_job_pending",
            {"node_job_id": node_job_id, "observations": entry["count"]},
            observed,
            f"the node does not know {node_job_id!r} yet "
            f"({entry['count']} of {POLL_UNKNOWN_MIN_OBSERVATIONS} "
            f"observations); the job stays running")

    @staticmethod
    def _pass_target_key(task):
        """One rolling lane per node; corrupt/missing ids isolate by job."""
        delivery_id, node_id = task
        if isinstance(node_id, str) and node_id:
            return "node:" + node_id
        return "job:" + str(delivery_id)

    def _run_rolling_pass(self, tasks, worker, account, *, stop, kind):
        """Run a bounded, target-fair rolling set of futures.

        Only active calls are submitted: pending work remains ordinary Python
        data, never an executor's unbounded queue. At most one call per node is
        active, so one hung target consumes one slot while healthy targets keep
        replenishing the other slots. A stop prevents every later submission;
        already-submitted calls are allowed to finish so their CAS/result
        reconciliation remains authoritative.
        """
        try:
            configured = int(self.pass_max_workers)
        except (TypeError, ValueError, OverflowError):
            configured = PASS_MAX_WORKERS
        workers = max(1, min(PASS_MAX_WORKERS, configured))

        queues = {}
        ready = collections.deque()
        for ordinal, task in enumerate(tasks):
            key = self._pass_target_key(task)
            queue = queues.get(key)
            if queue is None:
                queue = collections.deque()
                queues[key] = queue
                ready.append(key)
            queue.append((ordinal, task))

        active = {}
        aborted = False

        def stopping():
            return stop is not None and stop.is_set()

        with concurrent.futures.ThreadPoolExecutor(
                max_workers=workers,
                thread_name_prefix="convoy-%s" % kind) as executor:
            def replenish():
                nonlocal aborted
                while ready and len(active) < workers:
                    if stopping():
                        aborted = True
                        return
                    key = ready.popleft()
                    ordinal, task = queues[key].popleft()
                    future = executor.submit(worker, task[0])
                    active[future] = (ordinal, task, key)

            replenish()
            while active:
                done, _pending = concurrent.futures.wait(
                    tuple(active),
                    return_when=concurrent.futures.FIRST_COMPLETED)
                completed = []
                for future in done:
                    completed.append((*active.pop(future), future))
                # A simultaneous completion wave accounts and replenishes in
                # snapshot order, keeping tests/audits stable despite thread
                # scheduling order.
                completed.sort(key=lambda row: row[0])
                for _ordinal, task, key, future in completed:
                    try:
                        outcome = future.result()
                    except Exception as exc:
                        account(task[0], None, exc)
                    else:
                        account(task[0], outcome, None)
                    if queues[key]:
                        ready.append(key)
                if stopping():
                    aborted = True
                else:
                    replenish()
        if stopping():
            aborted = True
        return aborted

    def poll_once(self, stop=None):
        """Poll every currently-running node job once. Synchronous and
        directly testable; the background loop runs this before each
        dispatch pass.

        Called WITHOUT the lock (poll_job self-locks). The snapshot is
        taken lock-free for the same reason drain_once's is: jobs() is
        O(every job file on disk). A stale snapshot is safe -- poll_job
        re-reads under the lock, and a job that settled in the window is
        just a skip.

        stop: optional threading.Event checked before every rolling
        submission. Shutdown starts no new polls; the bounded active wave
        finishes and reconciles its read results before the pass returns.
        """
        running_jobs = self.db.jobs(state="running")
        running = [j["delivery_id"] for j in running_jobs]
        summary = {"examined": len(running), "finished": 0, "failed": 0,
                   "running": 0, "indeterminate": 0, "unreachable": 0,
                   "deferred": 0, "skipped": 0, "errors": 0, "backoff": 0,
                   "aborted": False}
        now = self._now()
        eligible = []
        for job in running_jobs:
            if stop is not None and stop.is_set():
                summary["aborted"] = True
                break
            delivery_id = job["delivery_id"]
            with self.lock:
                held_until = self._poll_backoff.get(delivery_id)
            if held_until is not None and held_until > now:
                summary["backoff"] += 1
                continue
            eligible.append((delivery_id, job.get("node_id")))

        def account_poll(_delivery_id, outcome, error):
            if error is not None:
                # One job's failure costs ONE job, never the rest of the
                # pass (drain_once learned this the hard way).
                summary["errors"] += 1
                return
            try:
                code, payload = outcome
                if payload.get("polled"):
                    bucket = _POLL_STATE_BUCKET.get(
                        payload.get("job", {}).get("state"))
                    summary[bucket if bucket else "errors"] += 1
                else:
                    bucket = _POLL_BUCKET.get(payload.get("reason"))
                    if bucket is not None:
                        summary[bucket] += 1
                    elif code == 200:
                        summary["skipped"] += 1   # settled in the window
                    else:
                        summary["errors"] += 1
            except Exception:
                summary["errors"] += 1

        if eligible:
            summary["aborted"] = (
                self._run_rolling_pass(
                    eligible, self.poll_job, account_poll,
                    stop=stop, kind="poll") or summary["aborted"])
        elif stop is not None and stop.is_set():
            summary["aborted"] = True
        # Prune against THIS pass's own snapshot -- never the drain
        # maps' queued view, which would wipe every poll entry. Same
        # conservative rule: keep anything still live, and keep an
        # unreadable-but-present job (get_job returns None for absent
        # AND unreadable alike).
        with self.lock:
            keep = set(running)
            for did in ((set(self._poll_backoff) | set(self._poll_noted)
                         | set(self._poll_unknown)) - keep):
                current = self.db.get_job(did)
                if current is not None and current.get("state") == "running":
                    keep.add(did)
                elif current is None and self.db.job_file_exists(did):
                    keep.add(did)
            self._poll_backoff = {k: v for k, v
                                  in self._poll_backoff.items()
                                  if k in keep}
            self._poll_noted = {k: v for k, v in self._poll_noted.items()
                                if k in keep}
            self._poll_unknown = {k: v for k, v
                                  in self._poll_unknown.items()
                                  if k in keep}
        return summary

    def drain_once(self, stop=None):
        """Dispatch every currently-queued job once. Synchronous and
        directly testable; the background loop is just this on a timer.

        Called WITHOUT the lock (dispatch_job self-locks). Both store
        snapshots are taken WITHOUT the lock, deliberately: jobs() is
        O(every job file on disk), and holding the lock across that scan
        froze every route for the duration (measured in review: tens of
        seconds cold at a few thousand files). A stale snapshot is safe
        because dispatch_job re-reads and CASes under the lock -- a job
        that resolved or got claimed in the window is just a skip.

        stop: optional threading.Event checked before every rolling
        submission. Shutdown starts no new dispatches; already-submitted
        calls finish so their claim/verdict reconciliation is never abandoned.
        """
        # Reconcile controller operation claims before taking a queue
        # snapshot.  This frees dead-controller queued work and preserves
        # running writer exclusion across long sleeps.
        with self.lock:
            self._reconcile_operation_claims_locked()

        # Reap claims stranded by a failed phase-c write: 'dispatching'
        # on disk with no forward in flight in THIS process can never
        # resolve on its own -- without this, such a job is invisible to
        # every route until a host restart.
        # Retry PARKED requeues first: a failed release write left a
        # never-delivered job claimed with its flight marker held. Until
        # the release lands, the reaper must skip it and the job cannot
        # retry -- so heal it before anything else in the pass.
        requeued = 0
        with self.lock:
            parked = list(self._pending_release)
        for did in parked:
            with self.lock:
                if did not in self._pending_release:
                    continue
                try:
                    released = self.db.release_claim(did)
                except Exception:
                    continue        # disk still failing; next pass
                if released is None:
                    current = self.db.get_job(did)
                    state = current.get("state") if current else None
                    if state == "dispatching" or (
                            current is None
                            and self.db.job_file_exists(did)):
                        # Unreadable, not resolved -- stay parked. An
                        # unconditional pop here handed the still-
                        # claimed job to the same-pass reaper.
                        continue
                self._pending_release.pop(did, None)
                self._in_flight.pop(did, None)
                if released is not None:
                    requeued += 1
                    try:
                        self.db.audit("hostapp",
                                      "dispatch_requeued_after_retry",
                                      {"delivery_id": did})
                    except Exception:
                        pass

        # Retry PARKED HANDOFFS on the same terms: a node job the host
        # observed START, whose running mirror could not be written.
        # Until it lands the claim is held (marker kept, reaper skipping
        # it) -- so heal it here, before the reaper runs, or a transient
        # write failure would end up terminalising a live node job.
        handoffs = 0
        with self.lock:
            pending = list(self._pending_handoff.items())
        for did, park in pending:
            with self.lock:
                if did not in self._pending_handoff:
                    continue
                try:
                    self.db.record_node_verdict(
                        did, park["node_status"],
                        node_job_id=park["node_job_id"],
                        observed_at=park["observed_at"],
                        result=park["result"])
                except Exception:
                    continue        # disk still failing; next pass
                self._pending_handoff.pop(did, None)
                self._in_flight.pop(did, None)
                handoffs += 1
                try:
                    self.db.audit("hostapp", "dispatch_handoff_recovered",
                                  {"delivery_id": did,
                                   "node_job_id": park["node_job_id"]})
                except Exception:
                    pass

        # ONE scan for both snapshots -- jobs() parses every job file on
        # disk, so two filtered calls doubled the pass's dominant cost.
        snapshot = self.db.jobs()
        stranded = 0
        for job in [j for j in snapshot if j.get("state") == "dispatching"]:
            did = job["delivery_id"]
            with self.lock:
                if did in self._in_flight:
                    continue        # a live dispatch owns it
                current = self.db.get_job(did)
                if (current is None
                        or current.get("state") != "dispatching"):
                    continue        # resolved in the window -- fine
                try:
                    updated = self.db.mark_indeterminate(did, {
                        "reason": "claim_stranded",
                        "detail": "claimed for dispatch but no forward "
                                  "is in flight in this process; a "
                                  "recording failure likely orphaned it",
                        "operation": current.get("operation")})
                    self._release_operation_claim_locked(updated)
                except Exception:
                    continue        # still unwritable; next pass retries
                stranded += 1       # the reap LANDED -- count it even
                try:                # if the audit append fails
                    self.db.audit("hostapp", "stranded_claim_reaped",
                                  {"delivery_id": did})
                except Exception:
                    pass

        queued_jobs = [j for j in snapshot if j.get("state") == "queued"]
        queued = [j["delivery_id"] for j in queued_jobs]
        with self.lock:
            # Rescale the bookkeeping caps BEFORE the per-job loop: the
            # maps must be able to hold one entry per queued job within
            # a single pass, or eviction defeats the very pacing and
            # audit dedupe they exist for (see DRAIN_MAP_FLOOR). Never
            # shrunk below the floor, and 2x leaves room for entries
            # set mid-pass by manual /dispatch on other jobs.
            self._drain_map_cap = max(DRAIN_MAP_FLOOR, 2 * len(queued))
        summary = {"examined": len(queued), "dispatched": 0, "started": 0,
                   "indeterminate": 0, "unreachable": 0, "no_handle": 0,
                   "deferred": 0, "skipped": 0, "errors": 0, "backoff": 0,
                   "refused": 0, "stranded": stranded, "requeued": requeued,
                   "handoffs": handoffs, "aborted": False}
        now = self._now()
        eligible = []
        for job in queued_jobs:
            if stop is not None and stop.is_set():
                summary["aborted"] = True
                break
            delivery_id = job["delivery_id"]
            with self.lock:
                held_until = self._drain_backoff.get(delivery_id)
            if held_until is not None and held_until > now:
                summary["backoff"] += 1
                continue
            eligible.append((delivery_id, job.get("node_id")))

        def account_dispatch(_delivery_id, outcome, error):
            if error is not None:
                # One job's failure must cost ONE job, never the rest of
                # the pass (a leading bad job would wedge the whole
                # queue every pass -- round-4 panel).
                summary["errors"] += 1
                return
            try:
                code, payload = outcome
                if payload.get("dispatched"):
                    summary["dispatched"] += 1
                    state = payload.get("job", {}).get("state")
                    if state == "indeterminate":
                        # Surfaced separately: 16.4 says a may-have-run must
                        # never be lost from view, a plain 'dispatched' count
                        # would hide it.
                        summary["indeterminate"] += 1
                    elif state == "running":
                        # An async HANDOFF, not a completed operation: the
                        # work is now running on the node and the poll pass
                        # owns its outcome.
                        summary["started"] += 1
                else:
                    bucket = _DRAIN_BUCKET.get(payload.get("reason"))
                    if bucket is not None:
                        summary[bucket] += 1
                    elif code == 200:
                        summary["skipped"] += 1  # raced: claimed or terminal
                    else:
                        summary["errors"] += 1   # unknown_job / unknown_node
            except Exception:
                summary["errors"] += 1

        if eligible:
            summary["aborted"] = (
                self._run_rolling_pass(
                    eligible, self.dispatch_job, account_dispatch,
                    stop=stop, kind="drain") or summary["aborted"])
        elif stop is not None and stop.is_set():
            summary["aborted"] = True
        # Keep the per-job maps from outliving their jobs -- but never
        # drop an entry whose job is still LIVE. The snapshot is a
        # start-of-pass view: an entry set mid-pass (a manual /dispatch,
        # a job created after the snapshot, a file the lock-free scan
        # transiently failed to read) belongs to a real queued or
        # claimed job, and dropping it defeated the backoff and
        # re-audited the same refusal (review probe, 2026-07-31).
        #
        # THE PER-ENTRY get_job READS RUN LOCK-FREE, and that is not
        # tidiness -- it is the same measured starvation status() and the
        # drain snapshot were both restructured for. Since the cap scales
        # to 2x the queue, a mass-terminalise pass (a re-pin / block /
        # remote-surface change against a large deferred backlog) leaves
        # the NEXT pass with O(backlog) stale entries; reading each one's
        # file under self.lock wedged every route but /health for the
        # whole scan. So: snapshot the stale KEYS under the lock (O(n)
        # memory, no disk), read the files WITHOUT it, and re-take the
        # lock only to drop the confirmed-dead keys. Dropping by an
        # explicit `drop` set (not rebuilding around `keep`) is what
        # preserves an entry ADDED between the two lock holds -- a
        # mid-pass /dispatch on another job -- which a keep-rebuild would
        # silently discard (the invariant the round-3 probe pinned).
        with self.lock:
            keep = set(queued)
            stale = (set(self._drain_backoff)
                     | set(self._drain_noted)) - keep
        drop = set()
        for did in stale:
            current = self.db.get_job(did)
            if current is not None and current.get("state") in (
                    "queued", "dispatching"):
                continue                      # still live -- keep pacing it
            if current is None and self.db.job_file_exists(did):
                # UNREADABLE is not ABSENT: get_job swallows the same
                # transient sharing violation that makes the lock-free
                # scan miss files. Keep -- conservative.
                continue
            drop.add(did)                     # examined, confirmed dead/gone
        with self.lock:
            self._drain_backoff = {k: v for k, v
                                   in self._drain_backoff.items()
                                   if k not in drop}
            self._drain_noted = {k: v for k, v
                                 in self._drain_noted.items()
                                 if k not in drop}
        return summary

    def drain(self):
        """The /drain route: one synchronous pass over the queue."""
        return 200, {"ok": True, **self.drain_once()}

    def _audit_pass(self, kind, summary):
        """One audit line when a pass's shape CHANGES and either side of
        the change shows trouble -- so a steady state (healthy or stuck)
        costs nothing, but every degradation and every recovery leaves a
        trace (A-40). Keyed by pass kind: the drain and poll passes have
        different shapes and must not overwrite each other's baseline."""
        def troubled(s):
            # .get, not [...]: the two summaries share only some keys,
            # and a KeyError here would kill the loop thread.
            return bool(s and (s.get("errors") or s.get("unreachable")
                               or s.get("stranded") or s.get("indeterminate")
                               or s.get("no_handle")))
        with self.lock:
            previous = self._last_pass_summary.get(kind)
            self._last_pass_summary[kind] = summary
            if summary != previous and (troubled(summary)
                                        or troubled(previous)):
                self.db.audit("hostapp", f"{kind}_pass", summary)

    def _drain_loop(self, stop, interval_s):
        while not stop.wait(interval_s):
            # POLL FIRST, then dispatch. Learning what already-running
            # node jobs did before starting new work keeps a finished
            # job from being re-polled on the next tick, and keeps the
            # running set from growing across one tick. Separate
            # try/except per pass: a poll failure must not cost the
            # dispatch pass (or vice versa).
            try:
                self._audit_pass("poll", self.poll_once(stop=stop))
            except Exception as e:
                try:
                    with self.lock:
                        self.db.audit("hostapp", "poll_loop_error", {
                            "error": f"{type(e).__name__}: {e}"})
                except Exception:
                    pass
            if stop.is_set():
                continue        # stopping: do not open a new forward
            try:
                self._audit_pass("drain", self.drain_once(stop=stop))
            except Exception as e:
                # The loop must survive anything -- a dead drain thread
                # silently ends autonomous dispatch.
                try:
                    with self.lock:
                        self.db.audit("hostapp", "drain_loop_error", {
                            "error": f"{type(e).__name__}: {e}"})
                except Exception:
                    pass
            # RETENTION, on a cadence, LOCK-FREE (reap only unlinks
            # already-terminal records). Without it the jobs dir grows
            # forever and every status/revocation scan slows with it.
            # Wrapped like the passes above: a reap failure must never
            # end the loop.
            try:
                self._maybe_reap()
            except Exception as e:
                try:
                    with self.lock:
                        self.db.audit("hostapp", "reap_loop_error", {
                            "error": f"{type(e).__name__}: {e}"})
                except Exception:
                    pass

    def _maybe_reap(self):
        """Reap terminal records at most once per reap_interval_s.

        Cadence-gated because a reap is a full jobs-dir scan; running it
        every drain tick would reintroduce the very cost it exists to
        bound. Lock-free -- reap() only unlinks records already terminal.
        """
        now = self._now()
        if now - self._last_reap < self.reap_interval_s:
            return
        self._last_reap = now
        result = self.db.reap(
            self.job_retention_s, now=now,
            on_reap=self._release_reaped_job_artifact)
        if result.get("jobs") or result.get("markers"):
            self._audit_pass("reap", result)
        # The ArtifactStore has its own maintenance -- TTL expiry, consumed/
        # expired capability pruning, stale-partial recovery -- that runs
        # ONLY from cleanup().  Nothing else schedules it, so without this the
        # index grows unbounded toward MAX_STATE_BYTES for the life of the
        # process.  cleanup() takes its own store lock (never the app lock),
        # and a failure here must not end the reap cadence.
        try:
            self.artifacts.cleanup()
        except Exception as e:
            try:
                with self.lock:
                    self.db.audit("hostapp", "artifact_cleanup_error", {
                        "error": f"{type(e).__name__}: {e}"})
            except Exception:
                pass
        # Node retention rides the same cadence: forget rows whose project
        # is provably deleted or that have been silent past the retention
        # horizon (see _evict_stale_nodes for the idle rules).
        try:
            self._evict_stale_nodes(now)
        except Exception as e:
            try:
                with self.lock:
                    self.db.audit("hostapp", "node_eviction_error", {
                        "error": f"{type(e).__name__}: {e}"})
            except Exception:
                pass

    def _release_reaped_job_artifact(self, job):
        """Drop the durable cache hold after its owning job is reaped."""
        self._release_acknowledged_job_artifact(job)

    def _record_lifecycle_recovery_result(self, attempt, result):
        """Reconcile one private-ledger verdict into its HostStore delivery.

        Called by the recovery worker.  A live dispatch keeps its in-memory
        marker until it records the result itself; touching it here would race
        phase C.  After a host restart no marker survives, and HostStore's
        conservative boot sweep has changed the claim to indeterminate, so
        the lifecycle-specific reconciliation door is then authoritative.
        """
        operation_id = attempt.get("operation_id")
        if not isinstance(operation_id, str) or not isinstance(result, dict):
            return False
        with self.lock:
            if operation_id in self._in_flight:
                return False
            job = self.db.get_job(operation_id)
            if job is None:
                return True
            if job.get("operation") != "convoy_restart_node":
                return False
            if (attempt.get("node_id") != job.get("node_id")
                    or attempt.get("convoy_id") != job.get("convoy_id")
                    or attempt.get("old_runtime_id")
                    != job.get("expected_runtime_id")):
                return False
            state = job.get("state")
            try:
                if state == "dispatching":
                    updated = self.db.record_host_result(
                        operation_id, result.get("ok") is True,
                        self._now(), result=result)
                elif state == "indeterminate":
                    updated = self.db.reconcile_lifecycle_result(
                        operation_id, result.get("ok") is True,
                        self._now(), result=result)
                elif state in ("succeeded", "failed"):
                    desired = "succeeded" if result.get("ok") is True \
                        else "failed"
                    if (state != desired or job.get("result") != result
                            or job.get("verdict_source") not in (
                                "host_operation",
                                "host_operation_recovery")):
                        return False
                    updated = job
                else:
                    return False
                if updated.get("state") in hoststore.TERMINAL_STATES:
                    self._release_operation_claim_locked(updated)
                self.db.audit("hostapp", "lifecycle_recovery_recorded", {
                    "delivery_id": operation_id,
                    "state": updated.get("state"),
                    "code": result.get("code")})
                return True
            except Exception as exc:
                self._audit_best_effort(
                    "lifecycle_recovery_record_failed", {
                        "delivery_id": operation_id,
                        "error": type(exc).__name__})
                return False

    def recover_lifecycle_once(self):
        """Run one independently testable durable restart recovery pass."""
        lifecycle = self.lifecycle
        recover = getattr(lifecycle, "recover_committed_restarts", None)
        if not callable(recover):
            return {"available": False, "examined": 0, "recovered": 0,
                    "reconciled": 0, "busy": 0, "errors": 0,
                    "results": []}

        def record(attempt, result):
            operation_id = attempt.get("operation_id")
            with self.lock:
                if operation_id in self._lifecycle_recovery_done:
                    return
            if self._record_lifecycle_recovery_result(attempt, result):
                with self.lock:
                    self._lifecycle_recovery_done.add(operation_id)

        summary = recover(result_callback=record)
        if not isinstance(summary, dict):
            raise RuntimeError("lifecycle recovery returned invalid summary")
        visible = {
            item.get("operation_id") for item in summary.get("results", [])
            if isinstance(item, dict)
            and isinstance(item.get("operation_id"), str)}
        with self.lock:
            self._lifecycle_recovery_done.intersection_update(visible)
        return dict(summary, available=True)

    def _lifecycle_recovery_loop(self, stop, interval_s):
        while True:
            try:
                self.recover_lifecycle_once()
            except Exception as exc:
                self._audit_best_effort(
                    "lifecycle_recovery_loop_error", {
                        "error": type(exc).__name__})
            if stop.wait(interval_s):
                return

    def start_lifecycle_recovery_loop(
            self, interval_s=LIFECYCLE_RECOVERY_INTERVAL_S):
        """Start immediate startup recovery plus bounded interval retries."""
        try:
            interval_s = float(interval_s)
        except (TypeError, ValueError):
            return False
        if not math.isfinite(interval_s) or interval_s <= 0.0:
            return False
        if not callable(getattr(
                self.lifecycle, "recover_committed_restarts", None)):
            return False
        with self.lock:
            if (self._lifecycle_recovery_thread is not None
                    and self._lifecycle_recovery_thread.is_alive()):
                return False
            stop = threading.Event()
            thread = threading.Thread(
                target=self._lifecycle_recovery_loop,
                args=(stop, interval_s), daemon=True,
                name="convoy-lifecycle-recovery")
            thread.start()
            self._lifecycle_recovery_stop = stop
            self._lifecycle_recovery_thread = thread
        return True

    def stop_lifecycle_recovery_loop(self, timeout_s=None):
        """Stop recovery without pretending a safety restoration vanished."""
        with self.lock:
            thread = self._lifecycle_recovery_thread
            if self._lifecycle_recovery_stop is not None:
                self._lifecycle_recovery_stop.set()
        if thread is None:
            return True
        if timeout_s is None:
            timeout_s = (lifecycle_mod.DEFAULT_RESTART_RECOVERY_TIMEOUT_S
                         + 5.0)
        thread.join(timeout=max(0.0, float(timeout_s)))
        if thread.is_alive():
            self._audit_best_effort(
                "lifecycle_recovery_stop_timeout", {
                    "timeout_s": timeout_s})
            return False
        with self.lock:
            if self._lifecycle_recovery_thread is thread:
                self._lifecycle_recovery_thread = None
                self._lifecycle_recovery_stop = None
        return True

    def start_drain_loop(self, interval_s=2.0):
        """Start the autonomous dispatcher: a daemon thread draining the
        queue every interval_s seconds. OPT-IN (nothing starts it unless
        asked -- main() wires it to --drain-interval). Returns True when
        started, False when a loop is already running -- including one a
        timed-out stop could not retire; the handle is kept alive
        precisely so this guard and status() stay honest."""
        with self.lock:
            if (self._drain_thread is not None
                    and self._drain_thread.is_alive()):
                return False
            # Audit BEFORE publishing any handle: if the append fails,
            # nothing was half-started. And start() INSIDE the lock:
            # publishing a not-yet-started thread opened a window where
            # a concurrent stop joined an unstarted thread
            # (RuntimeError) or a second start saw is_alive() False and
            # ran two loops (review probe, 2026-07-31).
            try:
                self.db.audit("hostapp", "drain_loop_started",
                              {"interval_s": interval_s})
            except Exception:
                pass    # an audit failure must not block the loop
            stop = threading.Event()
            thread = threading.Thread(
                target=self._drain_loop, args=(stop, interval_s),
                daemon=True, name="convoy-drain")
            # start() BEFORE publishing: a start() failure must not leave
            # handles pointing at a never-started thread (join on one
            # raises RuntimeError in every stop path).
            thread.start()
            self._drain_stop = stop
            self._drain_thread = thread
        return True

    def stop_drain_loop(self, timeout_s=None):
        """Stop the drain loop and wait for it to exit. Returns True when
        the loop is verifiably stopped (or none was running), False when
        it did not exit within the bound.

        On False the handles are KEPT: status() keeps reporting the live
        loop and start_drain_loop keeps refusing, instead of lying that
        the loop is gone and letting a second one start over it. The
        default bound covers the bounded active poll and drain waves. A stop
        prevents rolling replenishment but lets already-submitted calls finish
        their CAS/reconciliation. Safe to call when no loop is running."""
        with self.lock:
            thread = self._drain_thread
            if self._drain_stop is not None:
                self._drain_stop.set()
        if thread is None:
            return True
        if timeout_s is None:
            # TWO pass waves, not one: a tick can be inside active poll calls
            # and then active dispatch calls. Bounding to a single timeout
            # made a healthy stop report failure.
            timeout_s = 2 * mcpclient.DEFAULT_TIMEOUT_S + 5.0
        thread.join(timeout=timeout_s)
        if thread.is_alive():
            with self.lock:
                self.db.audit("hostapp", "drain_loop_stop_timeout",
                              {"timeout_s": timeout_s})
            return False
        with self.lock:
            if self._drain_thread is thread:
                self._drain_thread = None
                self._drain_stop = None
            self.db.audit("hostapp", "drain_loop_stopped", {})
        return True

    # -- the guarded request path (Phase 1 completion) ------------------

    def _refuse(self, source, reason, detail, code, node=None, extra=None):
        """Audit a gate denial and shape its response (A-39: denials, not
        just successes, leave a trace)."""
        record = {"reason": reason, "detail": str(detail)[:256]}
        if node is not None:
            record["node_id"] = node["node_id"]
        record.update(extra or {})
        try:
            self.db.audit("hostapp", f"{source}_refused", record)
        except Exception:
            # A refusal the trail could not record is still a refusal:
            # answering the named 4xx beats escalating to an unnamed,
            # equally-unaudited 500 (round-4 panel).
            pass
        payload = {"ok": False, "reason": reason, "detail": detail}
        return code, payload

    def _gate_operation(self, node, operation, controller_id, source,
                        expected_runtime_id=None, peer=None, arguments=None):
        """Registry gate (A-1), runtime precondition (A-22), and lease
        gate (A-17) -- shared by BOTH job-creating paths, so /jobs and
        /envelope can never diverge into two authorities. Returns None
        when allowed, or an audited (code, payload) refusal.

        `peer` is the PeerDecision when the request came from a LAN peer,
        None on the loopback path. It adds two REMOTE-ONLY refusals at
        the A-1 step, where the operation's class is finally known --
        deliberately here rather than in a second gate, for the same
        reason the two create paths share this one: two authorities on
        what may be invoked is how they drift apart.
        """
        # The node's own Enable Convoy parameter is the authoritative
        # participation gate.  A disabled node may remain in the durable
        # directory for identity/history, but no new or queued operation may
        # cross into it.  Re-checked here at both admission and dispatch.
        if not bool(node.get("enabled", True)):
            return self._refuse(
                source, "node_disabled",
                "the target node has disabled Convoy participation",
                409, node, {"operation": operation[:MAX_OPERATION_CHARS]})
        realm_refusal = self._realm_operation_refusal(node.get("convoy_id"))
        if realm_refusal is not None:
            reason, detail = realm_refusal
            return self._refuse(
                source, reason, detail, 409, node,
                {"operation": operation[:MAX_OPERATION_CHARS]})
        try:
            gating = effective_operation_gating(
                self.operations, operation, arguments)
        except OperationRegistryError as e:
            return self._refuse(
                source, e.reason, e.detail, e.code, node,
                {"operation": operation[:MAX_OPERATION_CHARS]})
        if (gating["executes_arbitrary_code"]
                and not self.policy.allow_td_python(node["node_id"])):
            return self._refuse(
                source, "td_python_not_approved",
                f"{operation!r} can execute Python; enable Allow Execute "
                f"TD Python on the target node before relaying it",
                403, node, {"operation": operation[:MAX_OPERATION_CHARS]})
        if (operation == "convoy_shell"
                and self.policy.allow_full_shell() is not True):
            return self._refuse(
                source, "full_shell_not_approved",
                "convoy_shell requires Allow Full Shell to be enabled "
                "locally on the target host",
                403, node, {"operation": operation[:MAX_OPERATION_CHARS]})
        if peer is not None and not gating["remote_exposed"]:
            # A-1 / R-2, ENFORCED. This flag was written a slice before
            # anything could read it, and then nothing did: run_tests
            # (which exec_module's every test_*.py AND test_*.txt it finds
            # on disk -- the registry's own comment says that argument
            # "dissolves the moment a socket binds off-box") and
            # save_project (15+ seconds of blocked main thread on a show
            # machine, before A-30's show protection exists) were both
            # remotely submittable. STRICT DEFAULT FALSE: an operation
            # nobody audited for the LAN is refused by ABSENCE, exactly
            # like absence from the registry itself. The LOCAL path is
            # untouched -- this is the owner's own machine.
            return self._refuse(
                source, "operation_not_remote_exposed",
                f"{operation!r} is not on this host's REMOTE surface; it "
                f"remains available locally",
                403, node, {"operation": operation[:MAX_OPERATION_CHARS],
                            "peer_digest": peer.digest})
        if peer is not None and not peer.may_mutate and gating["mutating"]:
            # OBSERVE-ONLY (24.6): X0 is permitted, everything past it is
            # refused REGARDLESS of local gate state. Honest limit, and it
            # belongs where an operator will read it: this is containment
            # for EXECUTABILITY, not confidentiality -- an observe-only
            # peer can still query_network and capture_top. If you do not
            # want them looking, BLOCK them.
            return self._refuse(
                source, peers_mod.REASON_OBSERVE_ONLY,
                f"{operation!r} is refused: "
                f"{peer.detail or 'this peer may not mutate'}",
                _REFUSAL_HTTP[peers_mod.REASON_OBSERVE_ONLY], node,
                {"operation": operation[:MAX_OPERATION_CHARS],
                 "peer_digest": peer.digest})
        # A-22: an operation that may act on stale state must name the
        # run it addressed. On the envelope path verify_envelope has
        # already enforced this over the SIGNED field; here it also
        # covers the local path, whose body is unsigned -- one rule for
        # both, rather than a precondition the local path can skip.
        if gating["runtime_required"]:
            if not expected_runtime_id:
                return self._refuse(
                    source, "runtime_id_required",
                    f"{operation!r} may act on stale state and MUST carry "
                    f"expected_runtime_id (A-22)",
                    400, node, {"operation": operation[:MAX_OPERATION_CHARS]})
            current = node.get("runtime_id")
            if not current:
                return self._refuse(
                    source, "runtime_unverifiable",
                    "expected_runtime_id was set but this node has no "
                    "known runtime to check it against",
                    409, node)
            if expected_runtime_id != current:
                return self._refuse(
                    source, "runtime_changed",
                    f"addressed runtime {expected_runtime_id!r}, node is "
                    f"now {current!r}",
                    409, node)
        now = self._now()
        self._reconcile_operation_claims_locked()
        if (gating["mutating"]
                and self._refresh_unreadable_operation_fence_locked()):
            return self._refuse(
                source, "operation_state_unreadable",
                "one or more durable Convoy jobs are unreadable, so the "
                "host cannot prove that no mutation is already in flight; "
                "reads remain available but new mutations fail closed",
                503, node, {"operation": operation[:MAX_OPERATION_CHARS],
                            "unreadable_jobs": len(
                                self._unreadable_operation_jobs)})
        # Operation bodies do not refresh controller liveness. The explicit
        # authenticated heartbeat route owns that authority; otherwise a
        # caller could name a dead controller on a harmless read and keep its
        # exclusive lease alive indefinitely.
        try:
            self.leases.authorize(node["node_id"], controller_id,
                                  gating["mutating"], now)
        except controllers.LeaseError as e:
            code, payload = self._refuse(
                source, e.reason, e.detail,
                _REFUSAL_HTTP.get(e.reason, 409), node,
                {"operation": operation[:MAX_OPERATION_CHARS],
                 "controller_id": controller_id[:MAX_ID_CHARS]})
            payload["holder"] = e.holder
            return code, payload
        return None

    def submit_envelope(self, body, origin):
        """Verify a signed Convoy/1 envelope and enqueue it as a durable
        job. THE guarded request path, in THE ORDER (plan 1.4):

            denylist -> pin/admission -> envelope verification
                     -> A-1 registry -> A-22 runtime -> A-17 lease

        `origin` is REQUIRED, with no default, because a default fails
        OPEN (see LOOPBACK_ORIGIN). It is either that sentinel -- the
        loopback route, where the IPC token is the credential and this
        host is itself the origin -- or the PEER this envelope arrived
        from: {"host_id": ..., "fingerprint": ...}, both established
        LOCALLY by the TLS layer (slice 3) from the certificate it
        actually saw, never anything the peer merely asserted in the body.

        THE ORDER IS THE POINT, not the outcome. authorize_peer runs
        FIRST, before a single signature is checked, so a revoked peer is
        refused while still holding a perfectly valid key. Pinned by a
        signature-verifier spy that must never be called for a blocked
        peer.
        """
        envelope = body.get("envelope")
        if not isinstance(envelope, dict):
            return self._refuse("envelope", "malformed",
                                "body must carry an 'envelope' object", 400)
        # -- STEP 1+2: denylist, then pin/admission. BEFORE VERIFICATION.
        origin_host_id = self.host_id
        peer_decision = None
        if origin is not LOOPBACK_ORIGIN:
            if not isinstance(origin, dict):
                # NOT the loopback sentinel and not a peer identity: a
                # caller that computed an origin and got None (or
                # anything else) is refused, never trusted.
                return self._refuse(
                    "peer", peers_mod.REASON_UNKNOWN,
                    "no peer identity was established for this envelope; "
                    "the loopback path must pass LOOPBACK_ORIGIN and the "
                    "peer path must pass the TLS-authenticated identity",
                    403)
            try:
                peer_host = text_field(origin, "host_id")
                peer_fingerprint = text_field(origin, "fingerprint",
                                              limit=MAX_ID_CHARS)
            except Malformed as e:
                return self._refuse("peer", peers_mod.REASON_UNKNOWN,
                                    e.detail, 403)
            peer_decision = self.peers.authorize_peer(
                peer_host, peer_fingerprint,
                convoy_id=envelope.get("convoy_id"))
            if not peer_decision.allowed:
                return self._refuse(
                    "peer", peer_decision.reason, peer_decision.detail,
                    _REFUSAL_HTTP.get(peer_decision.reason, 403),
                    extra={"peer_digest": peer_decision.digest,
                           "peer_state": peer_decision.state})
            origin_host_id = peer_decision.host_id
            # The peer's PINNED public key, carried IN-PROCESS by the LAN
            # listener from the certificate it actually presented (never
            # from the request body). It is what makes THE LISTENER CHOOSE
            # THE SIGNER below -- a peer envelope is verified against this
            # key, not the group PSK.
            peer_public_der = origin.get("public_der")
            # CHANNEL BINDING (source_mismatch) -- the half slice 2 named
            # as deferred because S7 says its absence is INVISIBLE, now
            # closed with the handshake that establishes the peer. The
            # envelope's SIGNED origin and source must BOTH be the
            # TLS-authenticated peer. Checked BEFORE verification because
            # it is an identity check, not a content check: a mismatch is
            # refused whether or not the signature is valid, and
            # verify_envelope then re-confirms these very fields are signed
            # (so a matching value cannot have been forged). Together with
            # v1's origin==source this closes the path by which an admitted
            # peer replays a third party's envelope (L-09) or names a third
            # machine as origin to make it execute elsewhere (L-10).
            claimed_origin = envelope.get("origin_host_id")
            claimed_source = envelope.get("source_host_id")
            if (claimed_origin != origin_host_id
                    or claimed_source != origin_host_id):
                return self._refuse(
                    "peer", "source_mismatch",
                    f"the envelope names origin={str(claimed_origin)[:64]!r} "
                    f"source={str(claimed_source)[:64]!r}, but the "
                    f"TLS-authenticated peer is {origin_host_id!r}; a peer "
                    f"may only submit envelopes it originated as itself", 403,
                    extra={"peer_digest": peer_decision.digest})
        # PRE-VERIFICATION READS. Two fields are read before the
        # signature is checked, and both only SELECT, never decide:
        # target_node_id selects the node record (and so the convoy PSK
        # to verify against -- without it no verification is possible at
        # all), and operation selects the registry entry whose
        # runtime_required flag sets verification STRICTNESS. Both are
        # inside the signed subset, so a tampered value cannot survive
        # the signature check that follows; and an unknown operation
        # yields the LESS strict verify, then is refused outright by the
        # registry gate afterwards. Every other field is used only after
        # verification.
        try:
            target = text_field(envelope, "target_node_id")
            operation = text_field(envelope, "operation",
                                   limit=MAX_OPERATION_CHARS)
        except Malformed as e:
            return self._refuse("envelope", "malformed", e.detail, 400)
        node = self.directory.lookup(target)
        if node is None:
            return self._refuse("envelope", "unknown_node", target, 404)
        # THE LISTENER CHOOSES THE SIGNER, NEVER THE ENVELOPE (plan 1.1).
        # A LOOPBACK envelope is group-authenticated with the convoy PSK,
        # exactly as before; a PEER envelope is verified against the
        # peer's PINNED Ed25519 key -- the one in the certificate the TLS
        # layer already matched to the pin. An envelope that arrived over
        # the LAN but is signed hmac-sha256 is refused by verify_envelope's
        # algorithm_mismatch: that IS the PSK-downgrade defense (S5), and
        # /psk is structurally unreachable from the LAN handler, so a peer
        # cannot obtain the group key to begin with.
        if peer_decision is not None:
            try:
                signer = hostkeys.verifier_from_public_der(peer_public_der)
            except hostkeys.HostKeyError as e:
                # No usable pinned key for this peer (missing/corrupt DER,
                # or cryptography absent -- in which case no LAN listener
                # could have bound at all). Refuse rather than fall back to
                # a signer that would verify the wrong thing.
                return self._refuse(
                    "peer", "peer_key_unusable", e.detail, 403, node,
                    {"peer_digest": peer_decision.digest})
        else:
            # ensure (not read): self-heals a register that predates PSK
            # minting; a fresh key can never validate an old signature, so
            # healing here can only produce a refusal, never an acceptance.
            signer = protocol.HmacSigner(
                self.db.ensure_convoy_psk(node["convoy_id"]))
        try:
            gating = effective_operation_gating(
                self.operations, operation, envelope.get("arguments"))
        except OperationRegistryError:
            gating = None
        # The A-22 precondition is asked of RELAYABLE operations only.
        # An unaudited or unknown operation is refused outright by the
        # registry gate below, and demanding expected_runtime_id first
        # would answer "you forgot a field" to a request whose real
        # problem is that it may never run here at all.
        runtime_required = bool(gating and gating["runtime_required"])
        try:
            # my_node_id is the record we looked up BY the envelope's
            # target id, so wrong_target cannot fire here -- it becomes
            # meaningful when a node verifies for itself (Phase 2+). The
            # real unknown-target protection is the lookup refusal above.
            accepted_timing = protocol.verify_envelope(
                envelope, signer, node["convoy_id"], node["node_id"],
                my_runtime_id=node.get("runtime_id"), now=self._now(),
                runtime_required=runtime_required)
        except protocol.EnvelopeRejected as e:
            return self._refuse(
                "envelope", e.reason, e.detail,
                _REFUSAL_HTTP.get(e.reason, 400), node,
                {"operation": operation[:MAX_OPERATION_CHARS],
                 "request_id": str(envelope.get("request_id"))[:MAX_ID_CHARS]})
        # verify_envelope requires controller_id/operation non-empty, but
        # NOT idempotency_key -- and the job store raises on a missing
        # one. A signed-but-malformed key must be a named refusal, not a
        # 500.
        try:
            idempotency_key = text_field(envelope, "idempotency_key")
            controller_id = text_field(envelope, "controller_id")
            # Read AFTER verification, like the two above: the signature
            # covers arguments_sha256, so a tampered value never reaches
            # here -- but a SIGNED non-dict does, and it is refused for
            # the same reason the local path refuses it. One rule for
            # both create paths, never two authorities.
            arguments = dict_field(envelope, "arguments")
        except Malformed as e:
            return self._refuse("envelope", "malformed", e.detail, 400, node)
        if peer_decision is not None:
            # NAMESPACE THE CONTROLLER, before it reaches the lease gate,
            # the heartbeat table, the attribution map or the delivery
            # record. controller_id is self-asserted free text: unscoped,
            # a peer names `ctl-local` and (measured) its OWN revocation
            # releases the local operator's exclusive lease, while one
            # envelope keeps a long-dead local controller reading alive.
            controller_id = peers_mod.namespaced_controller(
                origin_host_id, controller_id)
            # ... and register it BEFORE the gate, not after. _gate_operation
            # heartbeats the controller on the way through, so a peer whose
            # request was REFUSED used to land in the lease registry and
            # NOT in the attribution map -- invisible to revocation.
            self._note_peer_controller(origin_host_id, controller_id)
        refusal = self._gate_operation(
            node, operation, controller_id, source="envelope",
            expected_runtime_id=envelope.get("expected_runtime_id"),
            peer=peer_decision, arguments=arguments)
        if refusal is not None:
            return refusal
        try:
            job, created = self.db.create_job(
                idempotency_key, node["node_id"], operation,
                arguments, convoy_id=node["convoy_id"],
                expected_runtime_id=envelope.get("expected_runtime_id"),
                origin_host_id=origin_host_id, controller_id=controller_id,
                # The admission lineage this envelope was authorized
                # under -- the dispatch fence re-compares it at every
                # dispatch (stale_admission). None on the loopback path.
                origin_admission_id=(peer_decision.admission_id
                                     if peer_decision is not None
                                     else None),
                accepted_timing=accepted_timing)
        except hoststore.IdempotencyOriginConflict as e:
            return self._refuse(
                "envelope", e.reason, str(e), 409, node,
                {"operation": operation[:MAX_OPERATION_CHARS]})
        claim_refusal = self._ensure_operation_claim_locked(job, "envelope")
        if claim_refusal is not None:
            return claim_refusal
        # The ack carries the HOST's delivery_id (cj_...), the id of the
        # routing record -- NOT A-22's target-minted job_id, which is the
        # node's own job_<8hex> and does not exist until a node accepts
        # and runs the work (job["node_job_id"], None here). Keeping the
        # two distinct is A-15's "two records": one for delivery, one for
        # execution.
        return 200, {"ok": True, "created": created, "job": job}

    # -- capability manifest (A-23) --------------------------------------

    def build_manifest(self, node_id=None):
        """The manifest of operations this host will accept -- identical
        for every node in Phase 1, bound to a node_id when serving a
        per-node view. Digests cover name/schema/gating/side-effects,
        never source bytes or build numbers (A-23)."""
        manifest = capabilities.CapabilityManifest(protocol.PROTOCOL, node_id)
        for name in sorted(self.operations):
            entry = self.operations[name]
            # Whether an operation is ASYNC is compatibility-relevant: a
            # controller that expects a result and gets a job handle is
            # talking to a host it does not understand. Folded in only
            # when set, so the operations that predate the async slice
            # keep their existing digests byte-identical.
            side_effects = operation_capability_side_effects(entry)
            # gating_of, not entry.get: the digest must describe the
            # gating that is actually ENFORCED, defaults included, or a
            # controller could match digests with a host that treats the
            # same entry more permissively.
            manifest.add(name, capabilities.operation_digest(
                name,
                schema=entry.get("schema"),
                gating=gating_of(entry),
                side_effects=side_effects))
        return manifest

    def get_manifest(self, node_id=None):
        if node_id is not None and self.directory.lookup(node_id) is None:
            return 404, {"ok": False, "reason": "unknown_node",
                         "detail": str(node_id)[:64]}
        manifest = self.build_manifest(node_id)
        return 200, {"ok": True, "host_id": self.host_id,
                     "manifest": manifest.to_dict()}

    def build_remote_manifest(self):
        """The manifest a peer sees, filtered by explicit remote exposure.

        The reviewed registry exposes every node operation EXCEPT the two
        worker-only escalation operations (run_tests, save_project), which a
        peer must never even see (remote_exposed False). The filter also stays
        fail-closed for future/local-only entries and sparse extensions.
        Per-node code approval is deliberately not baked into this host-wide
        manifest -- it is mutable node policy checked at admission and
        dispatch, while operation compatibility is stable.
        """
        manifest = capabilities.CapabilityManifest(protocol.PROTOCOL, None)
        for name in sorted(self.operations):
            entry = self.operations[name]
            if not gating_of(entry)["remote_exposed"]:
                continue
            side_effects = operation_capability_side_effects(entry)
            manifest.add(name, capabilities.operation_digest(
                name,
                schema=entry.get("schema"),
                gating=gating_of(entry),
                side_effects=side_effects))
        return manifest

    def get_peer_manifest(self, host_id):
        """The remote-exposed manifest, served on the LAN /peer/manifest.

        LOCK-FREE, and it MUST stay so: it reads only self.operations,
        which is deep-copied once in __init__ and never mutated at
        runtime, so no app-lock is needed and the peer GET path does not
        take one. `host_id` is the TLS-authenticated peer -- the peer
        handler has already run authorize_peer before calling this, so a
        blocked/pending/killswitched peer never reaches here. The manifest
        is the same for every admitted peer today; the parameter is a seam
        for a future per-peer view -- BUT any such view that reads
        lock-guarded mutable state (self.peers, self._peer_controllers,
        self.leases) MUST take self.lock, because this method is called
        without it."""
        manifest = self.build_remote_manifest()
        return 200, {"ok": True, "host_id": self.host_id,
                     "manifest": manifest.to_dict()}

    def peer_job_view(self, host_id, delivery_id, since=None):
        """A delivery record's status for the PEER THAT SUBMITTED IT, with
        a monotonic cursor. Served on the LAN /peer/jobs/<delivery_id>.

        PER-PEER AUTHORIZATION (Gap 2): a peer may read its OWN delivery
        records and no others. The job's origin_host_id must equal the
        TLS-authenticated peer -- otherwise the answer is
        indistinguishable from 'no such job' (a not_found, never a
        'forbidden' that would confirm the id exists to a peer not
        entitled to it).

        THE CURSOR is the record's own `updated` timestamp -- the
        TARGET's clock, which the caller echoes back in `since`, so there
        is no cross-machine clock dependency (the field is compared to
        itself). since >= updated -> not changed (the caller already has
        this state); since < updated (or absent) -> the current view. A
        monotonic integer sequence with SSE push is the A-46 upgrade,
        deferred.

        The durable record read stays lock-free.  After ownership is proven,
        one short lock section refreshes the submitting controller and its
        exact operation claim.  Polling is therefore the idle heartbeat for
        a long-running call, without holding the host lock across disk or LAN
        I/O.
        """
        job = self.db.get_job(delivery_id) if delivery_id else None
        # NOT FOUND covers three cases a peer must not be able to tell
        # apart: no such id, an unreadable record, and a record owned by
        # a DIFFERENT origin. Confirming existence to an unentitled peer
        # is itself a leak.
        if job is None or (job.get("origin_host_id") or None) != host_id:
            return 404, {"ok": False, "reason": "not_found",
                         "delivery_id": str(delivery_id)[:64]}
        with self.lock:
            self._refresh_job_controller_locked(job)
        updated = job.get("updated")
        try:
            updated_f = float(updated)
        except (TypeError, ValueError):
            updated_f = None
        if (since is not None and updated_f is not None
                and float(since) >= updated_f):
            return 200, {"ok": True, "changed": False, "cursor": updated_f,
                         "delivery_id": job["delivery_id"],
                         "state": job.get("state")}
        # A BOUNDED, PEER-SAFE VIEW: delivery status and the node's
        # verdict, never internal attribution (controller_id,
        # origin_admission_id) or another node's business. The result is
        # bounded exactly as the loopback /jobs path bounds it.
        view = {
            "delivery_id": job["delivery_id"],
            "state": job.get("state"),
            "operation": job.get("operation"),
            "node_job_id": job.get("node_job_id"),
            "verdict_source": job.get("verdict_source"),
            "result": _bounded_result(job.get("result"))
            if job.get("result") is not None else None,
            "created": job.get("created"),
            "updated": job.get("updated"),
        }
        return 200, {"ok": True, "changed": True, "cursor": updated_f,
                     "job": view}

    def peer_cancel_job(self, origin_host_id, convoy_id, delivery_id,
                        authenticated_fingerprint=None):
        """Cancel only a delivery owned by this authenticated peer/realm."""
        try:
            convoy_id = identity.normalize_convoy_id(convoy_id)
        except identity.IdentityError:
            return 400, {"ok": False, "reason": "malformed"}
        with self.lock:
            decision = self.peers.authorize_peer(
                origin_host_id, authenticated_fingerprint,
                convoy_id=convoy_id)
            if not decision.allowed:
                return _REFUSAL_HTTP.get(decision.reason, 403), {
                    "ok": False, "reason": decision.reason,
                    "detail": decision.detail,
                }
            # Ownership is checked before realm diagnostics so an admitted
            # peer cannot use cancellation as an existence oracle for another
            # origin's job on an unbound/conflicted target.
            job = self.db.get_job(delivery_id)
            if (job is None or job.get("convoy_id") != convoy_id
                    or job.get("origin_host_id") != origin_host_id):
                return 404, {"ok": False, "reason": "unknown_job",
                             "detail": delivery_id}
            realm_refusal = self._realm_operation_refusal(convoy_id)
            if realm_refusal is not None:
                reason, detail = realm_refusal
                return 409, {"ok": False, "reason": reason,
                             "detail": detail}
            return self._cancel_job_locked(
                delivery_id, expected_convoy_id=convoy_id,
                expected_origin_host_id=origin_host_id)

    # -- LAN transport (Phase 3 slice 3) ---------------------------------

    @staticmethod
    def _session_auth_context(record):
        """Return the exact immutable (fingerprint, SPKI) pin for a peer."""
        if not isinstance(record, dict):
            return None
        fingerprint = record.get("fingerprint")
        cert_pem = record.get("cert_pem")
        if not fingerprint or not cert_pem:
            return None
        try:
            certificate_der = ssl.PEM_cert_to_DER_cert(cert_pem)
            public_der = hostkeys.public_der_from_certificate(certificate_der)
        except (ValueError, TypeError, ssl.SSLError,
                hostkeys.HostKeyError):
            return None
        if hostkeys.fingerprint(public_der) != fingerprint:
            return None
        return fingerprint, bytes(public_der)

    def _session_peer_config(self, record):
        context = self._session_auth_context(record)
        if context is None:
            return None
        targets, _error = self._peer_targets_from_record(record)
        endpoints = tuple(sessions_mod.PeerEndpoint(
            target.address, target.port, server_name=target.address)
            for target in targets)
        return record.get("host_id"), endpoints, context

    def _session_configs(self):
        """Snapshot only peers authorized for a currently active namespace."""
        with self.lock:
            namespaces = tuple(self._active_convoy_ids_locked())
            records = list(self.peers.peers())
            configs = []
            for record in records:
                host_id = record.get("host_id")
                if not host_id or not any(
                        self.peers.authorize_peer(
                            host_id, record.get("fingerprint"),
                            convoy_id=namespace).allowed
                        for namespace in namespaces):
                    continue
                config = self._session_peer_config(record)
                if config is not None:
                    configs.append(config)
        return namespaces, configs

    def _session_hello_profile(self):
        namespaces = self.active_convoy_ids()
        manifest = self.build_remote_manifest().to_dict()
        return sessions_mod.HelloProfile(namespaces, {
            "peer_protocol": "convoy-peer/1",
            "envelope_protocol": protocol.PROTOCOL,
            "manifest_digest": manifest.get("manifest_digest"),
            "max_frame_bytes": ws_mod.MAX_FRAME_BYTES,
            "artifact_transport": "https",
            "control_transport": "wss",
        })

    def _session_hello_sig(self):
        profile = self._session_hello_profile()
        material = json.dumps(
            {"namespaces": profile.namespaces,
             "capabilities": profile.capability_summary},
            sort_keys=True, separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def _dial_peer_session(self, peer_host_id, endpoint, timeout_s):
        """Return a pinned mTLS socket without sending any request bytes."""
        with self.lock:
            record = self.peers.get(peer_host_id)
            if record is None:
                raise peerclient.PeerSocketUnavailable("peer is unknown")
            namespaces = self._active_convoy_ids_locked()
            if not any(self.peers.authorize_peer(
                    peer_host_id, record.get("fingerprint"),
                    convoy_id=namespace).allowed
                    for namespace in namespaces):
                raise peerclient.PeerSocketUnavailable(
                    "peer is not currently authorized")
            targets, error = self._peer_targets_from_record(record)
            target = next((candidate for candidate in targets
                           if candidate.address == endpoint.address
                           and candidate.port == endpoint.port), None)
            keys = self.hostkeys
        if target is None or keys is None:
            raise peerclient.PeerSocketUnavailable(
                error or "peer endpoint is no longer configured")
        return peerclient.open_authenticated_socket(
            target, keys, timeout=timeout_s)

    def _start_peer_session_manager(self):
        if self.hostkeys is None or not self.active_convoy_ids():
            return None
        with self._session_manager_lock:
            manager = self.session_manager
            if manager is not None and not manager.is_stopped:
                return manager
            manager = sessions_mod.HostPairSessionManager(
                self.host_id, self._dial_peer_session,
                self._session_hello_profile, self._handle_session_rpc,
                websocket_path=peerserver.ROUTE_SESSION,
                max_peers=peerserver.DEFAULT_MAX_CONNECTIONS,
                dial_workers=8,
                session_options={
                    "max_pending": 128,
                    "max_inbound_queue": 128,
                    "handler_workers": 4,
                    "ping_interval_s": 15.0,
                    "idle_timeout_s": 45.0,
                },
                name=f"ConvoyWSS-{self.host_id}")
            namespaces, configs = self._session_configs()
            for host_id, endpoints, context in configs:
                manager.configure_peer(
                    host_id, endpoints,
                    authentication_context=context)
            manager.start()
            self.session_manager = manager
            self._session_hello_signature = self._session_hello_sig()
        self._audit_best_effort(
            "peer_sessions_started",
            {"namespaces": list(namespaces), "peers": len(configs)})
        return manager

    def _stop_peer_session_manager(self, timeout_s=2.0):
        lock = getattr(self, "_session_manager_lock", None)
        if lock is None:
            return
        with lock:
            manager = getattr(self, "session_manager", None)
            self.session_manager = None
            self._session_hello_signature = None
        if manager is not None:
            try:
                manager.stop(timeout_s=max(0.01, float(timeout_s)))
            except Exception:
                pass

    def reconcile_peer_sessions(self):
        """Converge WSS trust/endpoints without holding HostApp's lock."""
        manager = self.session_manager
        if manager is None or manager.is_stopped:
            return self._start_peer_session_manager()
        namespaces, configs = self._session_configs()
        if not namespaces:
            self._stop_peer_session_manager()
            return None
        desired = {config[0] for config in configs}
        try:
            current = {item.peer_host_id for item in manager.snapshot()}
        except sessions_mod.PairSessionError:
            return None
        for host_id in current.difference(desired):
            try:
                manager.revoke_peer(host_id)
            except sessions_mod.PairSessionError:
                pass
        for host_id, endpoints, context in configs:
            try:
                manager.configure_peer(
                    host_id, endpoints,
                    authentication_context=context)
            except sessions_mod.PeerRevoked:
                manager.restore_peer(
                    host_id, endpoints,
                    authentication_context=context)
            except sessions_mod.PairSessionError:
                continue
        signature = self._session_hello_sig()
        if signature != self._session_hello_signature:
            try:
                manager.refresh_hello()
                self._session_hello_signature = signature
            except sessions_mod.PairSessionError:
                pass
        return manager

    def prepare_peer_session(self, host_id, fingerprint, public_der):
        """Authorize/configure the exact certificate before WSS hello."""
        public_der = bytes(public_der)
        if hostkeys.fingerprint(public_der) != fingerprint:
            raise sessions_mod.PeerRevoked("peer certificate changed")
        with self.lock:
            record = self.peers.get(host_id)
            namespaces = self._active_convoy_ids_locked()
            if record is None or not any(self.peers.authorize_peer(
                    host_id, fingerprint, convoy_id=namespace).allowed
                    for namespace in namespaces):
                raise sessions_mod.PeerRevoked(
                    "peer is not authorized for an active namespace")
            config = self._session_peer_config(record)
        if config is None or config[2] != (fingerprint, public_der):
            raise sessions_mod.PeerRevoked(
                "authenticated certificate does not match current pin")
        manager = self.session_manager
        if manager is None or manager.is_stopped:
            manager = self._start_peer_session_manager()
        if manager is None:
            raise sessions_mod.PeerUnavailable(
                "Convoy WSS manager is not active")
        try:
            manager.configure_peer(
                host_id, config[1], authentication_context=config[2])
        except sessions_mod.PeerRevoked:
            manager.restore_peer(
                host_id, config[1], authentication_context=config[2])
        return manager

    @staticmethod
    def _session_payload(code, payload):
        result = dict(payload) if isinstance(payload, dict) else {
            "ok": False, "reason": "invalid_peer_response"}
        result.setdefault("http_status", int(code))
        return result

    def _handle_session_rpc(self, origin_host_id, convoy_id, method,
                            payload, authentication_context):
        """Map one WSS RPC onto the existing peer route authorities."""
        if (not isinstance(authentication_context, (tuple, list))
                or len(authentication_context) != 2
                or not isinstance(authentication_context[0], str)
                or not isinstance(authentication_context[1], bytes)):
            raise ws_mod.RemoteError(
                "peer_identity_invalid", "authenticated context is absent")
        fingerprint, public_der = authentication_context
        if hostkeys.fingerprint(public_der) != fingerprint:
            raise ws_mod.RemoteError(
                "peer_identity_invalid", "authenticated key does not match")
        with self.lock:
            decision = self.peers.authorize_peer(
                origin_host_id, fingerprint, convoy_id=convoy_id)
        if not decision.allowed:
            raise ws_mod.RemoteError(
                decision.reason, decision.detail,
                {"ok": False, "reason": decision.reason,
                 "detail": decision.detail,
                 "http_status": _REFUSAL_HTTP.get(decision.reason, 403)})
        body = payload if isinstance(payload, dict) else {}

        if method == peerserver.SESSION_RPC_HEALTH:
            return {"ok": True, "protocol": "convoy-peer/1",
                    "host_id": self.host_id, "http_status": 200}
        if method == peerserver.SESSION_RPC_MANIFEST:
            return self._session_payload(*self.get_peer_manifest(
                origin_host_id))
        if method == peerserver.SESSION_RPC_NODES:
            return self._session_payload(*self.peer_nodes_view(
                origin_host_id, convoy_id, fingerprint))
        if method == peerserver.SESSION_RPC_CONTROLLERS:
            return self._session_payload(*self.peer_controllers_view(
                origin_host_id, convoy_id, fingerprint))
        if method == peerserver.SESSION_RPC_CONTROLLER_HEARTBEAT:
            return self._session_payload(*self.peer_heartbeat_controller(
                origin_host_id, convoy_id, body, fingerprint))
        if method == peerserver.SESSION_RPC_ENVELOPE:
            if not isinstance(payload, dict) or not isinstance(
                    payload.get("envelope"), dict):
                return {"ok": False, "reason": "malformed",
                        "http_status": 400}
            origin = {"host_id": origin_host_id,
                      "fingerprint": fingerprint,
                      "public_der": public_der}
            with self.lock:
                code, result = self.submit_envelope(payload, origin)
            return self._session_payload(code, result)
        if method == peerserver.SESSION_RPC_JOB:
            delivery_id = body.get("delivery_id")
            since = body.get("since")
            if (not isinstance(delivery_id, str) or not delivery_id
                    or len(delivery_id) > 128
                    or any(not (char.isalnum() or char in "_-")
                           for char in delivery_id)
                    or (since is not None and (
                        isinstance(since, bool)
                        or not isinstance(since, (int, float))
                        or not math.isfinite(float(since))))):
                return {"ok": False, "reason": "malformed",
                        "http_status": 400}
            return self._session_payload(*self.peer_job_view(
                origin_host_id, delivery_id, since))
        if method == peerserver.SESSION_RPC_CANCEL:
            delivery_id = body.get("delivery_id")
            if (not isinstance(delivery_id, str) or not delivery_id
                    or len(delivery_id) > 128
                    or any(not (char.isalnum() or char in "_-")
                           for char in delivery_id)):
                return {"ok": False, "reason": "malformed",
                        "http_status": 400}
            return self._session_payload(*self.peer_cancel_job(
                origin_host_id, convoy_id, delivery_id, fingerprint))
        if method == peerserver.SESSION_RPC_ACK:
            delivery_id = body.get("delivery_id")
            if (not isinstance(delivery_id, str) or not delivery_id
                    or len(delivery_id) > 128
                    or any(not (char.isalnum() or char in "_-")
                           for char in delivery_id)):
                return {"ok": False, "reason": "malformed",
                        "http_status": 400}
            return self._session_payload(*self.peer_acknowledge_job(
                origin_host_id, convoy_id, delivery_id, fingerprint))
        raise ws_mod.RemoteError(
            "method_not_found", f"unknown peer RPC method {method!r}")

    def _session_call_if_connected(self, peer_host_id, convoy_id, method,
                                   payload, timeout_s):
        """Return ``(established_before_send, result)`` without replay."""
        manager = self.session_manager
        if manager is None or manager.is_stopped:
            return False, None
        try:
            info = manager.peer_info(peer_host_id)
        except sessions_mod.PairSessionError:
            return False, None
        if info.state != "connected":
            return False, None
        summary = info.remote_capability_summary
        advertised_digest = (summary.get("manifest_digest")
                             if isinstance(summary, dict) else None)
        with self.lock:
            record = self.peers.get(peer_host_id)
            authentication_context = self._session_auth_context(record)
            cache_key = self._peer_manifest_cache_key(
                record, advertised_digest)
            self._prune_peer_manifest_cache_locked(
                peer_host_id, keep_key=cache_key)
        if (authentication_context is None
                or not manager.connected_with_authentication_context(
                    peer_host_id, authentication_context)):
            return False, None
        if convoy_id not in info.authorized_namespaces:
            return True, {"ok": False, "reason": "namespace_forbidden",
                          "http_status": 403}
        try:
            return True, manager.call(
                peer_host_id, convoy_id, method, payload,
                timeout_s=max(0.001, float(timeout_s)))
        except ws_mod.RemoteError as exc:
            if isinstance(exc.data, dict):
                return True, dict(exc.data)
            return True, {"ok": False, "reason": exc.remote_code,
                          "detail": exc.detail, "http_status": 409}
        except ws_mod.SessionBusy as exc:
            return True, {"ok": False, "reason": "peer_session_busy",
                          "detail": str(exc), "http_status": 429}
        except (sessions_mod.PeerUnavailable,
                sessions_mod.NamespaceNotAuthorized,
                ws_mod.MessageTooLarge):
            # PROVABLY PRE-SEND: nothing was written to the socket, so the
            # HTTPS compatibility path is safe to run (it is NOT a possible
            # double-execute).  PeerUnavailable / NamespaceNotAuthorized are
            # raised by HostPairSessionManager.call BEFORE candidate.session
            # .call, and MessageTooLarge is send_json's outbound size check
            # BEFORE any byte reaches send_frame.  Return "not established"
            # so the caller runs the HTTP fallback instead of reporting a
            # 202 delivery_indeterminate for a request that never left.
            return False, None
        except (sessions_mod.PairSessionError,
                ws_mod.ConvoyWebSocketError, ValueError):
            # A selected session existed and bytes may already be on the
            # wire -- a mid-send ConnectionClosed/WebSocketTimeout is
            # genuinely ambiguous -- so HTTP compatibility replay is
            # forbidden.  CROSS-FILE follow-up: only a "bytes entered
            # _send_all" marker (a PreSendRefused) in convoy_ws.Session.call
            # / convoy_sessions can safely move the pre-send ConnectionClosed
            # state-checks ("session is closed") into the class above.
            return True, None

    def _audit_http_compat_fallback(self, peer_host_id, method):
        self._audit_best_effort("peer_http_compat_fallback", {
            "peer_host_id": peer_host_id, "method": method})

    @staticmethod
    def _peer_manifest_cache_key(record, advertised_digest):
        if (not isinstance(record, dict)
                or not isinstance(advertised_digest, str)
                or not _MANIFEST_DIGEST_RE.fullmatch(advertised_digest)):
            return None
        host_id = record.get("host_id")
        fingerprint = record.get("fingerprint")
        admission_id = record.get("admission_id")
        if (not isinstance(host_id, str) or not host_id
                or not isinstance(fingerprint, str) or not fingerprint
                or not isinstance(admission_id, str) or not admission_id):
            return None
        return host_id, fingerprint, admission_id, advertised_digest

    def _prune_peer_manifest_cache_locked(self, host_id, keep_key=None):
        """Drop stale trust/digest lineages while self.lock is held."""
        for key in list(self._peer_manifest_cache):
            if key[0] == host_id and key != keep_key:
                self._peer_manifest_cache.pop(key, None)

    def _invalidate_peer_manifest_cache(self, host_id=None):
        with self.lock:
            if host_id is None:
                self._peer_manifest_cache.clear()
            else:
                self._prune_peer_manifest_cache_locked(host_id)

    def _cached_peer_manifest(self, record, advertised_digest):
        host_id = record.get("host_id") if isinstance(record, dict) else None
        with self.lock:
            current = self.peers.get(host_id) if host_id else None
            key = self._peer_manifest_cache_key(
                current, advertised_digest)
            if host_id:
                # A new pin, admission lineage, or WSS-advertised digest may
                # never leave the old manifest reachable, even transiently.
                self._prune_peer_manifest_cache_locked(host_id, keep_key=key)
            if key is None:
                return None
            raw = self._peer_manifest_cache.get(key)
            if raw is None:
                return None
            self._peer_manifest_cache.move_to_end(key)
            return capabilities.CapabilityManifest.from_dict(copy.deepcopy(raw))

    def _store_peer_manifest(self, record, advertised_digest, manifest):
        key = self._peer_manifest_cache_key(record, advertised_digest)
        if key is None:
            return
        raw = manifest.to_dict()
        with self.lock:
            current = self.peers.get(key[0])
            current_key = self._peer_manifest_cache_key(
                current, advertised_digest)
            self._prune_peer_manifest_cache_locked(
                key[0], keep_key=current_key)
            if current_key != key:
                return
            self._peer_manifest_cache[key] = copy.deepcopy(raw)
            self._peer_manifest_cache.move_to_end(key)
            while len(self._peer_manifest_cache) > MAX_PEER_MANIFEST_CACHE:
                self._peer_manifest_cache.popitem(last=False)

    def _compatibility_refusal(self, peer_host_id, operation, reason,
                               detail, code=409, **fields):
        code, payload = self._refuse(
            "relay", reason, detail, code,
            extra={"target_host_id": peer_host_id,
                   "operation": str(operation)[:MAX_OPERATION_CHARS],
                   **fields})
        payload.update({"target_host_id": peer_host_id,
                        "operation": operation})
        payload.update(fields)
        return code, payload

    def _validate_peer_manifest(self, peer_host_id, operation, result,
                                advertised_digest=None):
        """Validate one untrusted manifest response and its WSS advert."""
        if (not isinstance(result, dict) or result.get("ok") is not True
                or result.get("host_id") != peer_host_id):
            return None, self._compatibility_refusal(
                peer_host_id, operation, "peer_bad_manifest",
                "the peer returned no authenticated host manifest", 502)
        raw = result.get("manifest")
        if not isinstance(raw, dict):
            return None, self._compatibility_refusal(
                peer_host_id, operation, "peer_bad_manifest",
                "the peer manifest is not an object", 502)
        remote_protocol = raw.get("protocol")
        operations = raw.get("operations")
        claimed_digest = raw.get("manifest_digest")
        if (not isinstance(operations, dict)
                or len(operations) > MAX_PEER_MANIFEST_OPERATIONS
                or raw.get("node_id") is not None
                or not isinstance(claimed_digest, str)
                or not _MANIFEST_DIGEST_RE.fullmatch(claimed_digest)):
            return None, self._compatibility_refusal(
                peer_host_id, operation, "peer_bad_manifest",
                "the peer manifest has an invalid bounded shape", 502)
        for name, digest in operations.items():
            if (not isinstance(name, str) or not name
                    or len(name) > MAX_OPERATION_CHARS
                    or any(ord(char) < 0x20 or ord(char) == 0x7f
                           for char in name)
                    or not isinstance(digest, str)
                    or not _OPERATION_DIGEST_RE.fullmatch(digest)):
                return None, self._compatibility_refusal(
                    peer_host_id, operation, "peer_bad_manifest",
                    "the peer manifest contains an invalid operation entry",
                    502)
        manifest = capabilities.CapabilityManifest(
            remote_protocol, None, operations)
        computed_digest = manifest.manifest_digest()
        if computed_digest != claimed_digest:
            return None, self._compatibility_refusal(
                peer_host_id, operation, "manifest_digest_mismatch",
                "the peer manifest does not match its claimed digest", 409,
                advertised_manifest_digest=advertised_digest,
                received_manifest_digest=claimed_digest)
        if (advertised_digest is not None
                and advertised_digest != computed_digest):
            self._invalidate_peer_manifest_cache(peer_host_id)
            return None, self._compatibility_refusal(
                peer_host_id, operation, "manifest_digest_mismatch",
                "the fetched manifest changed from this session's "
                "authenticated hello advertisement", 409,
                advertised_manifest_digest=advertised_digest,
                received_manifest_digest=computed_digest)
        if remote_protocol != protocol.PROTOCOL:
            return None, self._compatibility_refusal(
                peer_host_id, operation, "protocol_mismatch",
                f"controller requires {protocol.PROTOCOL!r}, peer advertises "
                f"{remote_protocol!r}", 409,
                expected_protocol=protocol.PROTOCOL,
                peer_protocol=remote_protocol)
        return manifest, None

    def _check_manifest_operation(self, peer_host_id, operation,
                                  expected_digest, manifest):
        try:
            capabilities.check_compatible(
                expected_digest, manifest, operation)
        except capabilities.CompatibilityError as exc:
            return self._compatibility_refusal(
                peer_host_id, operation, exc.reason, exc.detail,
                403 if exc.reason == "operation_not_exposed" else 409,
                expected_operation_digest=expected_digest,
                peer_operation_digest=manifest.operations.get(operation))
        return None

    def _peer_manifest_preflight(self, peer_host_id, convoy_id, operation,
                                 record, targets, keys):
        """Prove operation compatibility before any envelope is sent."""
        local_manifest = self.build_remote_manifest()
        expected_digest = local_manifest.operations.get(operation)
        if expected_digest is None:
            return self._compatibility_refusal(
                peer_host_id, operation, "operation_not_exposed",
                "this controller has no remote-exposed compatibility "
                f"contract for {operation!r}", 403)

        advertised_digest = None
        advertised_protocol = None
        manager = self.session_manager
        if manager is not None and not manager.is_stopped:
            try:
                info = manager.peer_info(peer_host_id)
            except sessions_mod.PairSessionError:
                info = None
            if (info is not None and info.state == "connected"
                    and convoy_id in info.authorized_namespaces):
                summary = info.remote_capability_summary
                if isinstance(summary, dict):
                    advertised_digest = summary.get("manifest_digest")
                    advertised_protocol = summary.get("envelope_protocol")
                if (advertised_protocol is not None
                        and advertised_protocol != protocol.PROTOCOL):
                    self._invalidate_peer_manifest_cache(peer_host_id)
                    return self._compatibility_refusal(
                        peer_host_id, operation, "protocol_mismatch",
                        f"controller requires {protocol.PROTOCOL!r}, peer "
                        f"session advertises {advertised_protocol!r}", 409,
                        expected_protocol=protocol.PROTOCOL,
                        peer_protocol=advertised_protocol)
                cached = self._cached_peer_manifest(
                    record, advertised_digest)
                if cached is not None:
                    return self._check_manifest_operation(
                        peer_host_id, operation, expected_digest, cached)

        used_session, result = self._session_call_if_connected(
            peer_host_id, convoy_id, peerserver.SESSION_RPC_MANIFEST, {},
            MANIFEST_PREFLIGHT_TIMEOUT_S)
        if not used_session:
            # Without a separate authenticated digest advertisement, an HTTP
            # peer is fetched every submission.  Reusing its old cache would
            # let an operation/schema change remain invisible indefinitely.
            self._invalidate_peer_manifest_cache(peer_host_id)
            advertised_digest = None
            if not targets:
                return self._compatibility_refusal(
                    peer_host_id, operation, "peer_endpoint_unknown",
                    "the peer has no WSS session or usable HTTPS endpoint",
                    409)
            if keys is None:
                return self._compatibility_refusal(
                    peer_host_id, operation, "identity_unavailable",
                    "this host has no usable mutual-TLS identity", 503)
            self._audit_http_compat_fallback(
                peer_host_id, peerserver.SESSION_RPC_MANIFEST)
            _target, result = self._call_peer_targets(
                targets,
                lambda candidate, remaining: peerclient.get_peer_manifest(
                    candidate, keys, timeout=remaining,
                    pool=self.peer_pool),
                MANIFEST_PREFLIGHT_TIMEOUT_S,
                retry_ambiguous=True)

        if result is peerclient.UNREACHABLE:
            return self._compatibility_refusal(
                peer_host_id, operation, "peer_unreachable",
                "the peer manifest could not be reached", 503)
        if isinstance(result, peerclient._PinMismatch):
            self._invalidate_peer_manifest_cache(peer_host_id)
            payload = result.as_dict()
            payload.update({"operation": operation,
                            "target_host_id": peer_host_id})
            return 409, payload
        if result is None:
            return self._compatibility_refusal(
                peer_host_id, operation, "peer_manifest_unavailable",
                "the peer manifest response was ambiguous or invalid", 502)
        if not isinstance(result, dict) or result.get("ok") is not True:
            if isinstance(result, dict):
                payload = dict(result)
                payload.setdefault("target_host_id", peer_host_id)
                payload.setdefault("operation", operation)
                return int(payload.get("http_status") or 409), payload
            return self._compatibility_refusal(
                peer_host_id, operation, "peer_bad_manifest",
                "the peer manifest response was not an object", 502)

        manifest, refusal = self._validate_peer_manifest(
            peer_host_id, operation, result,
            advertised_digest=advertised_digest)
        if refusal is not None:
            return refusal
        digest = manifest.manifest_digest()
        self._store_peer_manifest(record, digest, manifest)
        return self._check_manifest_operation(
            peer_host_id, operation, expected_digest, manifest)

    def _peer_compatibility_projection(self, peer_host_id):
        """Host-wide compatibility label requiring no per-node request."""
        manager = self.session_manager
        if manager is None or manager.is_stopped:
            return None
        try:
            info = manager.peer_info(peer_host_id)
        except sessions_mod.PairSessionError:
            return None
        if info.state != "connected":
            return None
        summary = info.remote_capability_summary
        if not isinstance(summary, dict):
            return None
        remote_protocol = summary.get("envelope_protocol")
        advertised_digest = summary.get("manifest_digest")
        if (remote_protocol is not None
                and remote_protocol != protocol.PROTOCOL):
            return "incompatible"
        local = self.build_remote_manifest()
        if advertised_digest == local.manifest_digest():
            return "compatible"
        with self.lock:
            record = self.peers.get(peer_host_id)
        cached = self._cached_peer_manifest(record, advertised_digest)
        if cached is None or cached.protocol != protocol.PROTOCOL:
            return None
        matches = set(local.operations.items()).intersection(
            cached.operations.items())
        if cached.operations == local.operations:
            return "compatible"
        return "limited" if matches else "incompatible"

    def _active_convoy_ids_locked(self):
        if not any(bool(record.get("enabled", True))
                   for record in self.directory.nodes()):
            return ()
        snapshot = self.realm.snapshot()
        return ((snapshot["convoy_id"],)
                if snapshot and snapshot.get("convoy_id") else ())

    def active_convoy_ids(self):
        """Enabled local namespaces, detached and safe for discovery."""
        with self.lock:
            return self._active_convoy_ids_locked()

    def request_lan_refresh(self):
        """Signal membership/interface reconciliation without socket I/O."""
        self._lan_refresh.set()
        service = self.discovery_service
        if service is not None:
            service.wake()

    def start_lan_lifecycle(self, log=None):
        """Start the one host-side LAN membership reconciler.

        This is external host Python, not a TouchDesigner thread.  The first
        pass is immediate, so durable enabled launch profiles resume exposure
        after a daemon restart even while their TD process is offline.
        """
        thread = self._lan_lifecycle_thread
        if thread is not None and thread.is_alive():
            return False
        self._lan_lifecycle_stop.clear()
        self._lan_refresh.set()
        thread = threading.Thread(
            target=self._lan_lifecycle_loop, args=(log,),
            name="ConvoyLanLifecycle", daemon=True)
        self._lan_lifecycle_thread = thread
        thread.start()
        return True

    def stop_lan_lifecycle(self, timeout_s=5.0):
        self._lan_lifecycle_stop.set()
        self._lan_refresh.set()
        thread = self._lan_lifecycle_thread
        if thread is not None and thread is not threading.current_thread():
            try:
                thread.join(max(0.0, float(timeout_s)))
            except Exception:
                pass
        self._lan_lifecycle_thread = None
        self.stop_lan_server(timeout_s=timeout_s)

    def _lan_lifecycle_loop(self, log=None):
        while not self._lan_lifecycle_stop.is_set():
            self._lan_refresh.wait(self._lan_retry_s)
            self._lan_refresh.clear()
            if self._lan_lifecycle_stop.is_set():
                break
            try:
                if self.active_convoy_ids():
                    self._tick_realm()
                self._reconcile_lan_once(log=log)
            except Exception as exc:
                self.lan_reason = "lifecycle_error"
                self._audit_best_effort(
                    "lan_lifecycle_error",
                    {"error": f"{type(exc).__name__}: {exc}"})
        self.stop_lan_server()

    def _reconcile_lan_once(self, log=None):
        """Reconcile membership, config, and the currently routed interface."""
        active = bool(self.active_convoy_ids())
        if not active:
            if self.lan_server is not None:
                self.stop_lan_server()
            self.lan_reason = "no_enabled_nodes"
            return
        if self.lan_server is None:
            start_lan_if_configured(self, log=log)
            return
        try:
            address, port = desired_lan_endpoint(self)
        except lan_mod.LanConfigError as exc:
            self.stop_lan_server()
            self.lan_reason = exc.reason
            return
        if address != self.lan_address or int(port) != int(self.lan_port):
            previous = f"{self.lan_address}:{self.lan_port}"
            self.stop_lan_server()
            self._audit_best_effort(
                "lan_endpoint_changed",
                {"previous": previous, "current": f"{address}:{port}"})
            start_lan_if_configured(self, log=log)
            return
        if self.discovery_service is None:
            # A rotated/unavailable discovery identity or a failed discovery
            # construction is repaired by rebuilding both endpoint owners.
            self.stop_lan_server()
            start_lan_if_configured(self, log=log)
        else:
            self.reconcile_peer_sessions()
            self.discovery_service.wake()

    def lan_trust_material(self):
        """(signature, [cert_pem, ...]) for the LAN server's TLS trust
        store. Called by convoy_peerserver's context provider, which
        rebuilds the SSL context whenever the signature changes.

        EVERY PEER WITH A STORED CERTIFICATE, including blocked ones: a
        blocked peer keeps its pin, so it still HANDSHAKES and is then
        refused by authorize_peer at accept and again at /peer/envelope --
        which is exactly what makes 'refused while holding a valid key'
        true (L-06) and the verifier-spy order test meaningful. A peer
        with NO stored cert is fail-closed out of the trust store and
        cannot connect at all. The signature is cheap and changes on any
        admit/block/forget/re-pin, so admission takes effect without a
        restart.
        """
        pems = []
        signature = []
        for record in self.peers.peers():
            cert_pem = record.get("cert_pem")
            if not cert_pem:
                continue
            pems.append(cert_pem)
            signature.append((record.get("host_id"),
                              record.get("fingerprint")))
        # Sorted so the signature is order-independent, and prefixed with
        # THIS host's own fingerprint: a local identity rotation must also
        # rebuild the server cert chain.
        signature.sort()
        my_fp = self.hostkeys.fingerprint if self.hostkeys else None
        return (my_fp, tuple(signature)), pems

    def lan_status(self):
        """Whether the LAN listener is up, and where. LOOPBACK-only route
        (/lan/status): an operator asks their OWN host, never a peer."""
        bound = self.lan_server is not None
        peers_material = self.lan_trust_material()[1]
        discovery = (self.discovery_service.status()
                     if self.discovery_service is not None else {
                         "active": False, "candidates": [],
                         "last_error": None,
                         "last_announcement_unix": None,
                     })
        session_stats = (self.session_manager.stats()
                         if self.session_manager is not None
                         and not self.session_manager.is_stopped else {
                             "configured_peers": 0,
                             "connected_peers": 0,
                         })
        return 200, {
            "ok": True,
            "host_id": self.host_id,
            "lan_bound": bound,
            "lan_address": self.lan_address,
            "lan_port": self.lan_port,
            "lan_reason": None if bound else self.lan_reason,
            # How many peers could actually establish a mutual-TLS
            # connection right now (have a pinned cert). Admission state
            # is separate -- these can still be refused by authorize_peer.
            "lan_trust_anchors": len(peers_material),
            "identity_fingerprint": (self.hostkeys.fingerprint
                                     if self.hostkeys else None),
            "identity_certificate": bool(
                self.hostkeys and self.hostkeys.certificate_pem),
            "active_convoy_ids": list(self._active_convoy_ids_locked()),
            "realm": self._realm_projection_locked(),
            "discovery": discovery,
            "peer_sessions": session_stats,
        }

    def set_lan_server(self, server, thread, address, port):
        """Record the bound LAN listener (main() calls this after
        serve_lan). Kept a setter for the same reason as the shutdown
        hook: the server does not exist until after HostApp is built."""
        self.lan_server = server
        self.lan_thread = thread
        self.lan_address = address
        self.lan_port = port
        self.lan_reason = None
        try:
            self._start_peer_session_manager()
        except Exception as exc:
            self._audit_best_effort(
                "peer_sessions_start_failed",
                {"error": f"{type(exc).__name__}: {exc}"})
        try:
            coordinator = discovery_mod.DiscoveryCoordinator(
                self.host_id, self.peers, self.active_convoy_ids,
                admission_lock=self.lock, now=self._now,
                active_realm_states=self.active_realm_states,
                realm_observer=self._observe_realm_announcement)
            service = discovery_mod.DiscoveryService(
                coordinator, self.hostkeys,
                listener_endpoint=lambda: (
                    (self.lan_address, self.lan_port)
                    if self.lan_server is not None else None),
                local_address=address)
            self.discovery_service = service
            service.start()
        except Exception as exc:
            self.discovery_service = None
            self._audit_best_effort(
                "discovery_start_failed",
                {"error": f"{type(exc).__name__}: {exc}"})

    def stop_lan_server(self, timeout_s=5.0):
        """Stop the LAN listener FIRST and unconditionally (A-46 point 4:
        stop accepting peer connections -> stop_drain_loop ->
        clear_portfile). Idempotent and never raises -- shutdown hygiene
        must not depend on it, exactly like stop_drain_loop."""
        service = self.discovery_service
        self.discovery_service = None
        if service is not None:
            try:
                service.stop(timeout=min(2.0, max(0.0, float(timeout_s))))
            except Exception:
                pass
        server = self.lan_server
        self.lan_server = None
        if server is not None:
            try:
                server.shutdown()
            except Exception:
                pass
            try:
                server.server_close()
            except Exception:
                pass
        self._stop_peer_session_manager(
            timeout_s=min(2.0, max(0.01, float(timeout_s))))
        if self.peer_pool is not None:
            self.peer_pool.close()
        thread = self.lan_thread
        if thread is not None:
            try:
                thread.join(timeout_s)
            except Exception:
                pass
        self.lan_thread = None
        self.lan_reason = "stopped"

    # -- convoy PSK issuance (Phase 1 local key distribution) ------------

    def issue_convoy_psk(self, body):
        """Hand the convoy's group signing key to a LOCAL caller.

        Phase 1 trust boundary, stated plainly: loopback + the per-install
        IPC token IS the admission control -- any process that can read
        the token file already owns this OS user. A convoy must have
        registered a node here before its key is issuable; Phase 3
        replaces PSK distribution with per-host keypairs + pinning.
        """
        try:
            convoy_id = text_field(body, "convoy_id")
        except Malformed as e:
            return self._refuse("psk", "malformed", e.detail, 400)
        psk = self.db.convoy_psk(convoy_id)
        if not psk:
            snapshot = self.realm.snapshot()
            if (snapshot and snapshot.get("convoy_id") == convoy_id
                    and snapshot.get("state") == realm_mod.CANDIDATE):
                return self._refuse(
                    "psk", "realm_not_established",
                    "automatic Convoy genesis is still settling", 409)
            return self._refuse("psk", "unknown_convoy", convoy_id, 404)
        realm_refusal = self._realm_operation_refusal(convoy_id)
        if realm_refusal is not None:
            reason, detail = realm_refusal
            return self._refuse("psk", reason, detail, 409)
        self.db.audit("hostapp", "convoy_psk_issued",
                      {"convoy_id": convoy_id})
        return 200, {"ok": True, "convoy_id": convoy_id, "psk": psk}

    # -- controllers and leases (A-16/A-17) -------------------------------

    def heartbeat_controller(self, body):
        now = self._now()
        try:
            controller_id = text_field(body, "controller_id")
            label = text_field(body, "label", required=False, limit=128)
            selected_node_id = (text_field(
                body, "selected_node_id", required=False) or None)
        except Malformed as e:
            return self._refuse("heartbeat", "malformed", e.detail, 400)
        clear_selected = body.get("clear_selected", False)
        if (not isinstance(clear_selected, bool)
                or (clear_selected and selected_node_id is not None)):
            return self._refuse(
                "heartbeat", "malformed",
                "clear_selected must be boolean and cannot accompany a node",
                400)
        if (selected_node_id is not None
                and self.directory.lookup(selected_node_id) is None):
            return self._refuse(
                "heartbeat", "unknown_node", selected_node_id, 404)
        try:
            self.leases.heartbeat(
                controller_id, now, label=label,
                selected_node_id=selected_node_id,
                clear_selection=clear_selected)
        except controllers.LeaseError as e:
            return self._refuse("heartbeat", e.reason, e.detail, 400)
        # Reap through the durable-aware reconciler. A raw TTL reap could
        # erase a running writer claim after a long sleep.
        self._reconcile_operation_claims_locked()
        return 200, {"ok": True}

    def peer_heartbeat_controller(self, origin_host_id, convoy_id, body,
                                  authenticated_fingerprint=None):
        """Refresh an idle/active peer controller on the selected host.

        The free-text controller id is namespaced by the authenticated host
        before it reaches lease state, exactly like envelope submission.
        """
        try:
            convoy_id = identity.normalize_convoy_id(convoy_id)
            controller_id = text_field(body, "controller_id")
            label = text_field(body, "label", required=False, limit=128)
            selected_node_id = (text_field(
                body, "selected_node_id", required=False) or None)
        except (identity.IdentityError, Malformed) as exc:
            detail = getattr(exc, "detail", str(exc))
            return self._refuse("heartbeat", "malformed", detail, 400)
        clear_selected = body.get("clear_selected", False)
        if (not isinstance(clear_selected, bool)
                or (clear_selected and selected_node_id is not None)):
            return self._refuse(
                "heartbeat", "malformed",
                "clear_selected must be boolean and cannot accompany a node",
                400)
        with self.lock:
            decision = self.peers.authorize_peer(
                origin_host_id, authenticated_fingerprint,
                convoy_id=convoy_id)
            if not decision.allowed:
                return _REFUSAL_HTTP.get(decision.reason, 403), {
                    "ok": False, "reason": decision.reason,
                    "detail": decision.detail,
                }
            if selected_node_id is not None:
                node = self.directory.lookup(selected_node_id)
                if (node is None or node.get("convoy_id") != convoy_id
                        or not bool(node.get("enabled", True))):
                    return self._refuse(
                        "heartbeat", "unknown_node", selected_node_id, 404)
            namespaced = peers_mod.namespaced_controller(
                origin_host_id, controller_id)
            self._note_peer_controller(origin_host_id, namespaced)
            try:
                self.leases.heartbeat(
                    namespaced, self._now(), label=label,
                    selected_node_id=selected_node_id,
                    clear_selection=clear_selected)
                self._reconcile_operation_claims_locked()
            except controllers.LeaseError as exc:
                return self._refuse(
                    "heartbeat", exc.reason, exc.detail, 400)
        return 200, {"ok": True, "controller_id": namespaced,
                     "selected_node_id": selected_node_id,
                     "wakes_touchdesigner": False}

    def acquire_lease(self, body):
        now = self._now()
        self._reconcile_operation_claims_locked()
        try:
            controller_id = text_field(body, "controller_id")
            node_id = text_field(body, "node_id")
            mode = (text_field(body, "mode", required=False)
                    or controllers.LEASE_SHARED)
        except Malformed as e:
            return self._refuse("lease", "malformed", e.detail, 400)
        ttl_s = body.get("ttl_s")
        if ttl_s is not None and (isinstance(ttl_s, bool)
                                  or not isinstance(ttl_s, (int, float))
                                  or not math.isfinite(ttl_s)
                                  or ttl_s <= 0):
            # NaN is the trap here: it IS a float and `nan <= 0` is
            # False, so it passed -- and `now + nan` is nan, which
            # `expires > now` then reads as ALREADY EXPIRED. The caller
            # got 200 and a lease that holds nothing: the phantom the
            # unknown_node check below exists to prevent.
            return self._refuse("lease", "malformed",
                                "ttl_s must be a positive finite number",
                                400)
        node = self.directory.lookup(node_id)
        if node is None:
            # A lease names a real node -- a typo must not mint a
            # phantom lease that blocks nobody and reassures its holder.
            return self._refuse("lease", "unknown_node", node_id, 404)
        realm_refusal = self._realm_operation_refusal(node.get("convoy_id"))
        if realm_refusal is not None:
            reason, detail = realm_refusal
            return self._refuse("lease", reason, detail, 409, node)
        if (mode == controllers.LEASE_EXCLUSIVE
                and self._refresh_unreadable_operation_fence_locked()):
            return self._refuse(
                "lease", "operation_state_unreadable",
                "durable job state is unreadable, so an exclusive writer "
                "lease cannot be granted safely",
                503, node, {"controller_id": controller_id[:MAX_ID_CHARS]})
        try:
            lease = self.leases.acquire(node_id, controller_id, mode, now,
                                        ttl_s=ttl_s)
        except controllers.LeaseError as e:
            code, payload = self._refuse(
                "lease", e.reason, e.detail,
                400 if e.reason in ("bad_mode",
                                    "malformed_controller") else 409,
                extra={"node_id": node_id,
                       "controller_id": controller_id[:MAX_ID_CHARS]})
            payload["holder"] = e.holder
            return code, payload
        self.db.audit("hostapp", "lease_acquired",
                      {"node_id": node_id, "mode": lease["mode"],
                       "controller_id": controller_id[:MAX_ID_CHARS]})
        self._reconcile_operation_claims_locked()
        return 200, {"ok": True, "lease": lease}

    def release_lease(self, body):
        try:
            controller_id = text_field(body, "controller_id")
            node_id = text_field(body, "node_id")
        except Malformed as e:
            return self._refuse("lease", "malformed", e.detail, 400)
        released = self.leases.release(node_id, controller_id)
        if released:
            self.db.audit("hostapp", "lease_released",
                          {"node_id": node_id,
                           "controller_id": str(controller_id)[:64]})
        # Idempotent by contract: releasing a hold you do not have is a
        # clean no-op, so cleanup paths can always fire it.
        return 200, {"ok": True, "released": released}

    def list_leases(self):
        now = self._now()
        self._reconcile_operation_claims_locked()
        return 200, {"ok": True, "leases": self.leases.live_leases(now)}

    # -- peers (Phase 3 slice 2, A-7) ------------------------------------
    #
    # LOOPBACK ONLY, behind the existing IPC token, in the existing route
    # table -- exactly like /identity*. /peers* and /lan* are named in the
    # plan's LOOPBACK list and must never appear in slice 3's peer table:
    # a peer that could admit itself is not an admission control.

    def _note_peer_controller(self, host_id, controller_id):
        """Remember that a peer acted as a controller. BOUNDED, LRU.

        Called WITH self.lock held. The map exists so revocation can drop
        a peer's leases even when its job records cannot be read; the
        durable job scan is the primary source.

        Eviction is by OLDEST ENTRY across every peer. It used to pop from
        the first-INSERTED host's bucket, so a chatty peer could evict a
        quiet peer's freshly-refreshed attribution -- which is precisely
        the entry revocation needs.
        """
        if not host_id or not controller_id:
            return
        seen = self._peer_controllers.setdefault(host_id, {})
        seen[controller_id] = self._now()
        total = sum(len(v) for v in self._peer_controllers.values())
        while total > MAX_PEER_CONTROLLERS:
            victim = min(
                ((h, c, t) for h, entry in self._peer_controllers.items()
                 for c, t in entry.items()), key=lambda row: row[2])
            entry = self._peer_controllers[victim[0]]
            entry.pop(victim[1], None)
            if not entry:
                self._peer_controllers.pop(victim[0], None)
            total -= 1

    def _controllers_for_origin(self, host_id, records=None):
        """Every controller_id this peer has acted as: the in-memory map
        UNIONED with the durable job records. The records are what
        survives a host restart; the map is what survives an unreadable
        job file, and what covers a request the gate REFUSED. Neither
        alone is enough.

        `records` is passed in by any caller that has already scanned, so
        the emergency paths scan the job store exactly ONCE.
        """
        found = set(self._peer_controllers.get(host_id) or ())
        # Operation claims survive independently of controller heartbeats.
        # After a host restart the advisory _peer_controllers map is empty,
        # and a failing durable-job scan cannot reconstruct ownership from
        # disk.  The peer namespace is therefore the last trustworthy link
        # between an in-memory claim and its origin.  Include it so emergency
        # revocation paths can preserve (never accidentally drop) an
        # in-flight writer claim while durable state is unreadable.
        prefix = peers_mod.namespaced_controller(host_id, "")
        found.update(
            claim.get("controller_id")
            for claim in self.leases.operation_claims()
            if isinstance(claim.get("controller_id"), str)
            and claim["controller_id"].startswith(prefix)
        )
        if records is None:
            try:
                records, _ = self.db.scan_jobs()
            except Exception:
                records = []
        for job in records:
            if job.get("origin_host_id") == host_id and \
                    job.get("controller_id"):
                found.add(job["controller_id"])
        return found

    def _authorize_origin(self, origin_host_id, convoy_id=None):
        """Re-ask THE decision for a stored job's origin.

        Returns None for LOCAL work, or the FULL PeerDecision otherwise --
        allowed ones included. Returning None for "allowed" threw away
        `may_mutate`, and an observe-only peer is allowed=True: the
        dispatcher read that as "go ahead" and forwarded mutations for a
        peer narrowed to read-only (four reviewers reproduced it; one
        watched the host connect to the node and record the result as
        indeterminate -- a mutation logged as MAY HAVE RUN on behalf of a
        peer that may not mutate at all). The caller needs the whole
        decision, not a boolean.

        Called on every dispatch, which is what makes a hand-edited
        denylist bite without an API call and without a restart: the
        denylist re-reads on mtime change, and this is the caller that
        notices.
        """
        if not origin_host_id or origin_host_id == self.host_id:
            # Locally originated -- or a record written before this field
            # existed, which is the same thing: nothing but this host
            # could have created it.
            return None
        return self.peers.authorize_peer(
            origin_host_id, self.peers.pinned_fingerprint(origin_host_id),
            convoy_id=convoy_id)

    def _refuse_origin(self, job, decision, reason=None, detail=None,
                       terminal=None):
        """Refuse a dispatch on the origin's account. Called WITH the lock.

        BURN OR SKIP, and the distinction is NOT "reversibility" (the
        earlier comment said that and it was false -- /peers/admit can
        undo a block, a forget and a narrowing alike). It is whether a
        MEMBERSHIP DECISION WAS TAKEN:

          - a SWITCH is in force (the A-32 killswitch, a denylist entry,
            a store this host temporarily cannot read). Membership is
            untouched, so the work is SKIPPED and paced; flipping the
            switch back must bring exactly that work with it.
          - a MEMBERSHIP DECISION was taken (blocked, forgotten,
            re-pinned, or an operation taken off the remote surface).
            This host will never serve this job, so leaving it queued
            makes /jobs LIE -- it terminalises as `refused`. Re-admitting
            the peer does NOT resurrect it: a FULL revocation (block /
            forget / re-pin) stamps a fresh admission_id epoch (see
            convoy_peers _set_state / _upsert), so every job the old
            lineage stamped is stale at the dispatch fence FOREVER, even
            work the revocation sweep could not reach and even a block
            later laundered through observe-only before the re-admit.

        OBSERVE-ONLY IS DELIBERATELY NOT IN THAT LIST, and that is a
        correction of an earlier overclaim. It is a REVERSIBLE
        class-narrowing, not a full revocation: 24.6 requires a peer's
        READS to keep flowing under it, so a read submitted while
        admitted must survive an observe narrowing AND a later widen --
        which means observe cannot stamp a new epoch (that would burn the
        very reads it must preserve). Its containment of MUTATIONS is two
        other mechanisms, not the epoch: the mutating-only revocation
        sweep at narrow time, and the per-dispatch may_mutate refusal
        that is in force for exactly as long as the peer is observe-only.
        NAMED RESIDUAL: a mutation submitted before the narrow, left
        UNREACHABLE during that sweep, and still queued when the operator
        WIDENS the peer back to full admission will then dispatch -- but
        under the admission the operator just RE-GRANTED, which is
        indistinguishable from the peer re-submitting it, so no boundary
        is crossed. A block (not an observe) is the tool for durably
        killing a peer's in-flight work.
        """
        delivery_id = job["delivery_id"]
        reason = reason or decision.reason
        detail = detail if detail is not None else decision.detail
        if terminal is None:
            terminal = not decision.reversible
        self._note_dispatch_event(
            delivery_id, "dispatch_refused",
            # COMPOUND on purpose: _note_dispatch_event dedupes on
            # (event, reason), and a job whose refusal moves from
            # denylisted to forgotten to pin-mismatched is a real
            # transition the trail must show. Prefix-stable, so a reader
            # filtering on origin_not_admitted still works.
            {"reason": "origin_not_admitted:" + reason,
             "peer_reason": reason,
             "terminal": bool(terminal),
             "origin_host_id": str(job.get("origin_host_id"))[:64],
             "peer_digest": decision.digest})
        if terminal:
            try:
                refused = self.db.mark_refused(delivery_id, {
                    "reason": "peer_revoked",
                    "cause": reason,
                    "peer_reason": reason,
                    "detail": "the peer that submitted this job may no "
                              "longer have it served by this host; it was "
                              "never delivered to a node and never ran",
                    "origin_host_id": job.get("origin_host_id"),
                    "operation": job.get("operation"),
                    "attempts": int(job.get("attempts") or 0),
                    "at": self._now()})
            except Exception:
                refused = None
            if refused is not None:
                self._release_operation_claim_locked(refused)
                return 403, {"ok": False, "reason": "origin_revoked",
                             "detail": detail, "peer_reason": reason,
                             "job": refused}
            # The terminalise did not land (unwritable disk, or the job
            # left queued underneath us). Fall through to the skip: the
            # next pass retries, and a job left queued is the safe side.
        self._set_drain_backoff(delivery_id,
                                self._now() + self.drain_backoff_s)
        return 403, {"ok": False, "reason": "origin_not_admitted",
                     "detail": detail, "peer_reason": reason}

    def list_peers(self):
        killswitch = self.peers.killswitch()
        return 200, {"ok": True, "host_id": self.host_id,
                     "peers": self.peers.peers(),
                     "peers_unreadable": self.peers.unreadable,
                     "killswitch": killswitch,
                     "denylist": self.peers.denylist.snapshot()}

    # -- outbound peer relay (loopback controller surface) -------------

    @staticmethod
    def _peer_targets_from_record(record):
        """Build every usable pinned target from one peer admission.

        Endpoints are untrusted persisted text.  Parsing happens locally
        and never falls back to plaintext or an unpinned certificate.
        Discovery places its newest observation first and retains bounded
        older/manual endpoints as fallbacks.  Callers try those fallbacks
        only after a definitely-before-send ``UNREACHABLE`` outcome (or for
        read-only GETs); a pin mismatch is always terminal.
        """
        if not isinstance(record, dict):
            return (), "peer record is missing"
        cert_pem = record.get("cert_pem")
        fingerprint = record.get("fingerprint")
        host_id = record.get("host_id")
        if not cert_pem or not fingerprint or not host_id:
            return (), "peer has no complete pinned TLS identity"
        targets = []
        seen = set()
        for raw in record.get("endpoints") or ():
            if not isinstance(raw, str) or len(raw) > 256:
                continue
            try:
                address, port_text = raw.rsplit(":", 1)
                port = int(port_text)
            except (ValueError, TypeError):
                continue
            address = address.strip()
            if (not address or not (1 <= port <= 65535)
                    or any(ch in address for ch in "/\\?#@")):
                continue
            endpoint = (address, port)
            if endpoint in seen:
                continue
            seen.add(endpoint)
            targets.append(peerclient.PeerTarget(
                host_id=host_id, address=address, port=port,
                pinned_cert_pem=cert_pem,
                expected_fingerprint=fingerprint))
        if targets:
            return tuple(targets), None
        return (), "peer has no usable host:port endpoint"

    @staticmethod
    def _peer_target_from_record(record):
        """Compatibility projection for callers that need one target."""
        targets, error = HostApp._peer_targets_from_record(record)
        return (targets[0] if targets else None), error

    @staticmethod
    def _call_peer_targets(targets, call, timeout_s, retry_ambiguous=False):
        """Try persisted endpoints within one cumulative monotonic budget.

        ``call(target, remaining_s)`` follows peerclient's outcome contract.
        Mutations may move to the next address only after ``UNREACHABLE``,
        which proves no request byte was sent.  Read-only callers may also
        retry an ambiguous/bad response because repeating a GET cannot run a
        mutation.  A pin mismatch never falls through to another address.
        """
        targets = tuple(targets or ())
        if not targets:
            return None, peerclient.UNREACHABLE
        try:
            timeout_s = float(timeout_s)
        except (TypeError, ValueError):
            timeout_s = 0.0
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            return targets[0], peerclient.UNREACHABLE
        deadline = time.monotonic() + timeout_s
        last_target = targets[0]
        last_result = peerclient.UNREACHABLE
        for index, target in enumerate(targets):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            # One black-holed endpoint (SYN into the void burns its whole
            # timeout in the CONNECT phase) must not starve the untried
            # rest of the list: an attempt may spend at most its even
            # share of what remains, and the final target keeps
            # everything still left.  A stale-first endpoint list is the
            # normal post-DHCP-change shape, not a corner case.
            untried = len(targets) - index
            attempt_s = remaining if untried == 1 else remaining / untried
            last_target = target
            result = call(target, attempt_s)
            last_result = result
            if isinstance(result, peerclient._PinMismatch):
                return target, result
            if result is peerclient.UNREACHABLE:
                continue
            if result is None and retry_ambiguous:
                continue
            return target, result
        return last_target, last_result

    def relay_submit(self, body):
        """Originate one signed request on behalf of a LOCAL controller.

        SELF-LOCKING: route handlers call this outside the app lock because
        the mutual-TLS request may block.  All mutable admission/identity
        state is validated and snapshotted first; the receiving host then
        re-authorizes independently before it persists anything.
        """
        try:
            peer_host_id = text_field(body, "target_host_id")
            convoy_id = text_field(body, "convoy_id")
            target_node_id = text_field(body, "target_node_id")
            controller_id = text_field(body, "controller_id")
            operation = text_field(body, "operation",
                                   limit=MAX_OPERATION_CHARS)
            arguments = (dict_field(body, "arguments")
                         if "arguments" in body else {})
            idempotency_key = text_field(
                body, "idempotency_key", required=False) or None
            expected_runtime_id = text_field(
                body, "expected_runtime_id", required=False) or None
        except Malformed as e:
            return self._refuse("relay", "malformed", e.detail, 400)
        timeout_s = body.get("timeout_s", 30.0)
        if (isinstance(timeout_s, bool)
                or not isinstance(timeout_s, (int, float))
                or not math.isfinite(float(timeout_s))
                or float(timeout_s) <= 0
                or float(timeout_s) > protocol.MAX_DEADLINE_HORIZON_S):
            return self._refuse(
                "relay", "malformed",
                f"timeout_s must be within (0, "
                f"{protocol.MAX_DEADLINE_HORIZON_S:.0f}]", 400)

        # Several TD/Embody nodes may share this machine and therefore this
        # host_id.  Sending such a call through the peer table would require
        # admitting our own certificate as a peer and would make same-IP
        # routing fail whenever the LAN listener is quiescent.  Use the exact
        # same local durable-job gate instead; there is still no direct Envoy
        # shortcut and therefore no semantic split between local siblings and
        # remote siblings.
        if peer_host_id == self.host_id:
            with self.lock:
                node = self.directory.lookup(target_node_id)
                if (node is None or node.get("convoy_id") != convoy_id):
                    return self._refuse(
                        "relay", "unknown_node",
                        "the target node is not in the requested Convoy",
                        404)
                local_body = {
                    "idempotency_key": (idempotency_key
                                        or identity.mint_id()),
                    "node_id": target_node_id,
                    "controller_id": controller_id,
                    "operation": operation,
                    "arguments": arguments,
                }
                if expected_runtime_id is not None:
                    local_body["expected_runtime_id"] = expected_runtime_id
                code, result = self.create_job(local_body)
            result = dict(result)
            result.setdefault("target_host_id", self.host_id)
            result.setdefault("convoy_id", convoy_id)
            result.setdefault("local_sibling", True)
            return code, result

        with self.lock:
            if self.hostkeys is None:
                return self._identity_unavailable()
            record = self.peers.get(peer_host_id)
            decision = self.peers.authorize_peer(
                peer_host_id,
                record.get("fingerprint") if record else None,
                convoy_id=convoy_id)
            if not decision.allowed:
                return self._refuse(
                    "relay", decision.reason, decision.detail,
                    _REFUSAL_HTTP.get(decision.reason, 403),
                    extra={"peer_digest": decision.digest})
            targets, target_error = self._peer_targets_from_record(record)
            keys = self.hostkeys
            signer = keys.signer()

        compatibility_refusal = self._peer_manifest_preflight(
            peer_host_id, convoy_id, operation, record, targets,
            keys)
        if compatibility_refusal is not None:
            return compatibility_refusal

        try:
            envelope = protocol.build_envelope(
                convoy_id, self.host_id, controller_id, target_node_id,
                operation, signer, arguments=arguments,
                timeout_s=float(timeout_s),
                expected_runtime_id=expected_runtime_id,
                idempotency_key=idempotency_key)
        except (ValueError, TypeError, hostkeys.HostKeyError) as e:
            return self._refuse(
                "relay", "envelope_build_failed",
                f"{type(e).__name__}: {e}", 400)

        send_budget = min(
            peerclient.DEFAULT_PEER_TIMEOUT_S,
            protocol.remaining_budget(envelope))
        used_session, result = self._session_call_if_connected(
            peer_host_id, convoy_id, peerserver.SESSION_RPC_ENVELOPE,
            {"envelope": envelope}, send_budget)
        if not used_session:
            if not targets:
                return self._refuse("relay", "peer_endpoint_unknown",
                                    target_error, 409)
            self._audit_http_compat_fallback(
                peer_host_id, peerserver.SESSION_RPC_ENVELOPE)
            _target, result = self._call_peer_targets(
                targets,
                lambda candidate, remaining: peerclient.send_envelope(
                    candidate, keys, envelope, timeout=remaining,
                    pool=self.peer_pool),
                send_budget)
        if result is peerclient.UNREACHABLE:
            return 503, {"ok": False, "reason": "peer_unreachable",
                         "target_host_id": peer_host_id,
                         "idempotency_key": envelope["idempotency_key"]}
        if isinstance(result, peerclient._PinMismatch):
            return 409, result.as_dict()
        if result is None:
            # The peer may have persisted the job.  Never blind-retry with
            # a different key; expose the signed request identity for the
            # reconciliation API.
            return 202, {
                "ok": False, "reason": "delivery_indeterminate",
                "detail": "the request may have reached the peer; reconcile "
                          "using the same idempotency key",
                "target_host_id": peer_host_id,
                "request_id": envelope["request_id"],
                "idempotency_key": envelope["idempotency_key"],
            }
        code = 200 if result.get("ok") else int(
            result.get("http_status") or 409)
        result = dict(result)
        result.setdefault("target_host_id", peer_host_id)
        result.setdefault("convoy_id", convoy_id)
        result.setdefault("request_id", envelope["request_id"])
        result.setdefault("idempotency_key", envelope["idempotency_key"])
        return code, result

    def relay_job(self, body):
        """Poll one remote delivery through its pinned peer connection."""
        try:
            peer_host_id = text_field(body, "target_host_id")
            convoy_id = text_field(body, "convoy_id")
            delivery_id = text_field(body, "delivery_id")
        except Malformed as e:
            return self._refuse("relay", "malformed", e.detail, 400)
        since = body.get("since")
        if since is not None and (isinstance(since, bool)
                                  or not isinstance(since, (int, float))
                                  or not math.isfinite(float(since))):
            return self._refuse("relay", "malformed",
                                "since must be a finite number", 400)

        if peer_host_id == self.host_id:
            stored = self.db.get_job(delivery_id)
            if (stored is None or stored.get("convoy_id") != convoy_id
                    or stored.get("origin_host_id") != self.host_id):
                return 404, {"ok": False, "reason": "not_found",
                             "delivery_id": delivery_id}
            code, result = self.peer_job_view(
                self.host_id, delivery_id, since)
            if code == 200 and result.get("ok"):
                result = dict(result)
                result.setdefault("target_host_id", self.host_id)
                result.setdefault("convoy_id", convoy_id)
                result.setdefault("local_sibling", True)
            return code, result
        with self.lock:
            record = self.peers.get(peer_host_id)
            decision = self.peers.authorize_peer(
                peer_host_id,
                record.get("fingerprint") if record else None,
                convoy_id=convoy_id)
            if not decision.allowed:
                return self._refuse(
                    "relay", decision.reason, decision.detail,
                    _REFUSAL_HTTP.get(decision.reason, 403))
            targets, target_error = self._peer_targets_from_record(record)
            keys = self.hostkeys
        if keys is None:
            return self._identity_unavailable()
        used_session, result = self._session_call_if_connected(
            peer_host_id, convoy_id, peerserver.SESSION_RPC_JOB,
            {"delivery_id": delivery_id, "since": since},
            peerclient.DEFAULT_PEER_TIMEOUT_S)
        if not used_session:
            if not targets:
                return self._refuse("relay", "peer_endpoint_unknown",
                                    target_error, 409)
            self._audit_http_compat_fallback(
                peer_host_id, peerserver.SESSION_RPC_JOB)
            _target, result = self._call_peer_targets(
                targets,
                lambda candidate, remaining: peerclient.get_peer_job(
                    candidate, keys, delivery_id, since=since,
                    timeout=remaining, pool=self.peer_pool),
                peerclient.DEFAULT_PEER_TIMEOUT_S,
                retry_ambiguous=True)
        if result is peerclient.UNREACHABLE:
            return 503, {"ok": False, "reason": "peer_unreachable"}
        if isinstance(result, peerclient._PinMismatch):
            return 409, result.as_dict()
        if result is None:
            return 502, {"ok": False, "reason": "peer_bad_response"}
        return (200 if result.get("ok") else 404), result

    def relay_acknowledge(self, body):
        """Acknowledge one observed terminal sibling outcome.

        This is an explicit, idempotent mutation rather than a polling side
        effect.  A controller can therefore retry an unanswered ACK without
        risking command replay, while the target keeps terminal evidence and
        any result artifact protected until an ACK actually arrives.
        """
        try:
            peer_host_id = text_field(body, "target_host_id")
            convoy_id = identity.normalize_convoy_id(
                text_field(body, "convoy_id"))
            delivery_id = text_field(body, "delivery_id")
        except (Malformed, identity.IdentityError) as exc:
            detail = getattr(exc, "detail", str(exc))
            return self._refuse("relay", "malformed", detail, 400)

        if peer_host_id == self.host_id:
            with self.lock:
                job = self.db.get_job(delivery_id)
                if (job is None or job.get("convoy_id") != convoy_id
                        or job.get("origin_host_id") != self.host_id):
                    return 404, {"ok": False, "reason": "unknown_job",
                                 "delivery_id": delivery_id}
                code, result = self._acknowledge_job_locked(delivery_id)
            result = dict(result)
            result.setdefault("target_host_id", self.host_id)
            result.setdefault("convoy_id", convoy_id)
            result.setdefault("local_sibling", True)
            return code, result

        with self.lock:
            record = self.peers.get(peer_host_id)
            decision = self.peers.authorize_peer(
                peer_host_id,
                record.get("fingerprint") if record else None,
                convoy_id=convoy_id)
            if not decision.allowed:
                return self._refuse(
                    "relay", decision.reason, decision.detail,
                    _REFUSAL_HTTP.get(decision.reason, 403))
            targets, target_error = self._peer_targets_from_record(record)
            keys = self.hostkeys
        if keys is None:
            return self._identity_unavailable()

        used_session, result = self._session_call_if_connected(
            peer_host_id, convoy_id, peerserver.SESSION_RPC_ACK,
            {"delivery_id": delivery_id},
            peerclient.DEFAULT_PEER_TIMEOUT_S)
        if not used_session:
            if not targets:
                return self._refuse("relay", "peer_endpoint_unknown",
                                    target_error, 409)
            self._audit_http_compat_fallback(
                peer_host_id, peerserver.SESSION_RPC_ACK)
            _target, result = self._call_peer_targets(
                targets,
                lambda candidate, remaining:
                    peerclient.acknowledge_peer_job(
                        candidate, keys, convoy_id, delivery_id,
                        timeout=remaining, pool=self.peer_pool),
                peerclient.DEFAULT_PEER_TIMEOUT_S)
        if result is peerclient.UNREACHABLE:
            return 503, {"ok": False, "reason": "peer_unreachable",
                         "target_host_id": peer_host_id,
                         "delivery_id": delivery_id}
        if isinstance(result, peerclient._PinMismatch):
            return 409, result.as_dict()
        if result is None:
            # ACK is idempotent, so unlike command submission an ambiguous
            # reply is safe for the caller to retry with this delivery id.
            return 503, {
                "ok": False, "reason": "acknowledgement_indeterminate",
                "detail": "the acknowledgement may have reached the peer; "
                          "retry with the same delivery_id",
                "target_host_id": peer_host_id,
                "delivery_id": delivery_id,
            }
        code = 200 if result.get("ok") else int(
            result.get("http_status") or 409)
        result = dict(result)
        result.setdefault("target_host_id", peer_host_id)
        result.setdefault("convoy_id", convoy_id)
        result.setdefault("delivery_id", delivery_id)
        return code, result

    def relay_heartbeat(self, body):
        """Publish this local bridge controller to one exact sibling host."""
        try:
            peer_host_id = text_field(body, "target_host_id")
            convoy_id = identity.normalize_convoy_id(
                text_field(body, "convoy_id"))
            controller_id = text_field(body, "controller_id")
            selected_node_id = (text_field(
                body, "selected_node_id", required=False) or None)
            label = text_field(body, "label", required=False, limit=128)
        except (Malformed, identity.IdentityError) as exc:
            detail = getattr(exc, "detail", str(exc))
            return self._refuse("relay", "malformed", detail, 400)
        clear_selected = body.get("clear_selected", False)
        if (not isinstance(clear_selected, bool)
                or (clear_selected and selected_node_id is not None)):
            return self._refuse("relay", "malformed",
                                "invalid selection heartbeat", 400)
        heartbeat = {
            "controller_id": controller_id, "label": label,
            "clear_selected": clear_selected,
        }
        if selected_node_id is not None:
            heartbeat["selected_node_id"] = selected_node_id

        if peer_host_id == self.host_id:
            with self.lock:
                if convoy_id not in self._active_convoy_ids_locked():
                    return self._refuse(
                        "relay", "unknown_convoy", convoy_id, 404)
                if selected_node_id is not None:
                    node = self.directory.lookup(selected_node_id)
                    if (node is None or node.get("convoy_id") != convoy_id
                            or not bool(node.get("enabled", True))):
                        return self._refuse(
                            "relay", "unknown_node", selected_node_id, 404)
                code, result = self.heartbeat_controller(heartbeat)
            result = dict(result)
            result.setdefault("target_host_id", self.host_id)
            result.setdefault("convoy_id", convoy_id)
            result.setdefault("local_sibling", True)
            return code, result

        with self.lock:
            record = self.peers.get(peer_host_id)
            decision = self.peers.authorize_peer(
                peer_host_id,
                record.get("fingerprint") if record else None,
                convoy_id=convoy_id)
            if not decision.allowed:
                return self._refuse(
                    "relay", decision.reason, decision.detail,
                    _REFUSAL_HTTP.get(decision.reason, 403))
            targets, target_error = self._peer_targets_from_record(record)
            keys = self.hostkeys
        if keys is None:
            return self._identity_unavailable()
        used_session, result = self._session_call_if_connected(
            peer_host_id, convoy_id,
            peerserver.SESSION_RPC_CONTROLLER_HEARTBEAT, heartbeat,
            peerclient.DEFAULT_PEER_TIMEOUT_S)
        if not used_session:
            if not targets:
                return self._refuse("relay", "peer_endpoint_unknown",
                                    target_error, 409)
            self._audit_http_compat_fallback(
                peer_host_id,
                peerserver.SESSION_RPC_CONTROLLER_HEARTBEAT)
            _target, result = self._call_peer_targets(
                targets,
                lambda candidate, remaining:
                    peerclient.heartbeat_peer_controller(
                        candidate, keys, convoy_id, controller_id,
                        selected_node_id,
                        clear_selected=clear_selected, label=label,
                        timeout=remaining, pool=self.peer_pool),
                peerclient.DEFAULT_PEER_TIMEOUT_S)
        if result is peerclient.UNREACHABLE:
            return 503, {"ok": False, "reason": "peer_unreachable"}
        if isinstance(result, peerclient._PinMismatch):
            return 409, result.as_dict()
        if result is None:
            return 503, {"ok": False,
                         "reason": "heartbeat_indeterminate"}
        code = 200 if result.get("ok") else int(
            result.get("http_status") or 409)
        result = dict(result)
        result.setdefault("target_host_id", peer_host_id)
        result.setdefault("convoy_id", convoy_id)
        result.setdefault("wakes_touchdesigner", False)
        return code, result

    @staticmethod
    def _artifact_relay_status(result):
        reason = result.get("reason") if isinstance(result, dict) else None
        return {
            "artifact_invalid": 400,
            "artifact_scope_not_found": 404,
            "artifact_not_found": 404,
            "artifact_unauthorized": 403,
            "artifact_transfer_busy": 429,
            "deadline_exceeded": 504,
            "artifact_deadline_exceeded": 504,
            "artifact_quota_exceeded": 507,
            "artifact_owner_claims_exceeded": 507,
            "artifact_corrupt": 422,
            "artifact_local_io": 500,
        }.get(reason, 502)

    def relay_artifact(self, body):
        """Materialize one exact peer artifact into this host's cache.

        The loopback bridge never treats a remote path as local.  It passes
        the small artifact reference returned by the durable job here; this
        host then uses the peer's pinned mTLS identity, one cumulative
        transfer deadline, a private partial file, and exact size/SHA-256
        verification before exposing a local reference.  Reads may fail over
        to another persisted endpoint because replaying an artifact GET cannot
        execute a command.
        """
        try:
            peer_host_id = text_field(body, "target_host_id")
            convoy_id = identity.normalize_convoy_id(
                text_field(body, "convoy_id"))
            target_node_id = text_field(body, "target_node_id")
            controller_id = text_field(body, "controller_id")
            reference = dict_field(body, "artifact")
        except (Malformed, identity.IdentityError) as exc:
            detail = getattr(exc, "detail", str(exc))
            return self._refuse("relay", "malformed", detail, 400)
        timeout_s = body.get(
            "timeout_s", peerclient.DEFAULT_ARTIFACT_TIMEOUT_S)
        if (isinstance(timeout_s, bool)
                or not isinstance(timeout_s, (int, float))
                or not math.isfinite(float(timeout_s))
                or not 0.1 <= float(timeout_s)
                <= protocol.MAX_DEADLINE_HORIZON_S):
            return self._refuse(
                "relay", "malformed",
                f"timeout_s must be within [0.1, "
                f"{protocol.MAX_DEADLINE_HORIZON_S:.0f}]", 400)

        # Keep the newly materialized cache entry non-evictable across the
        # gap between this response and the bridge's authenticated loopback
        # download/export.  The bridge releases this opaque handoff after it
        # has a safe local owner; abandoned handoffs age out in ArtifactStore.
        relay_protection_id = "relay:" + secrets.token_hex(16)
        protected_artifact_id = reference.get("artifact_id")
        protection_handed_off = False

        if not self.begin_artifact_transfer():
            return 429, {"ok": False,
                         "reason": "artifact_transfer_busy",
                         "wakes_touchdesigner": False}
        try:
            if peer_host_id == self.host_id:
                with self.lock:
                    node = self.directory.lookup(target_node_id)
                    if (node is None or node.get("convoy_id") != convoy_id
                            or not bool(node.get("enabled", True))):
                        return 404, {"ok": False,
                                     "reason": "artifact_node_unavailable"}
                owner = {
                    "host_id": self.host_id,
                    "node_id": target_node_id,
                    "controller_id": controller_id,
                }
                if isinstance(reference.get("job_id"), str):
                    owner["job_id"] = reference["job_id"]
                try:
                    local_reference = self.artifacts.describe_for_owner(
                        convoy_id, reference.get("artifact_id"), owner,
                        verify=True, touch=True,
                        protection_id=relay_protection_id,
                        protection_kind="active_transfer")
                except artifacts_mod.ArtifactError as exc:
                    return self._artifact_error(exc)
                if any(local_reference.get(name) != reference.get(name)
                       for name in ("artifact_id", "sha256", "size",
                                    "mime_type")):
                    return 422, {"ok": False,
                                 "reason": "artifact_corrupt",
                                 "detail": "artifact reference metadata "
                                           "does not match local content"}
                protection_handed_off = True
                return 200, {
                    "ok": True, "artifact": local_reference,
                    "relay_protection_id": relay_protection_id,
                    "transfer": {"attempts": 0, "resumed": False,
                                 "bytes": local_reference["size"]},
                    "target_host_id": self.host_id,
                    "convoy_id": convoy_id, "local_sibling": True,
                    "wakes_touchdesigner": False,
                }

            with self.lock:
                record = self.peers.get(peer_host_id)
                decision = self.peers.authorize_peer(
                    peer_host_id,
                    record.get("fingerprint") if record else None,
                    convoy_id=convoy_id)
                if not decision.allowed:
                    return self._refuse(
                        "relay", decision.reason, decision.detail,
                        _REFUSAL_HTTP.get(decision.reason, 403))
                targets, target_error = self._peer_targets_from_record(record)
                if not targets:
                    return self._refuse(
                        "relay", "peer_endpoint_unknown", target_error, 409)
                keys = self.hostkeys
            if keys is None:
                return self._identity_unavailable()

            _target, result = self._call_peer_targets(
                targets,
                lambda candidate, remaining: (
                    peerclient.download_peer_artifact(
                        candidate, keys, self.artifacts, convoy_id,
                        target_node_id, controller_id, reference,
                        timeout_s=remaining,
                        protection_id=relay_protection_id,
                        protection_kind="active_transfer")),
                float(timeout_s), retry_ambiguous=True)
            if result is peerclient.UNREACHABLE:
                return 503, {"ok": False, "reason": "peer_unreachable",
                             "target_host_id": peer_host_id,
                             "wakes_touchdesigner": False}
            if isinstance(result, peerclient._PinMismatch):
                return 409, result.as_dict()
            if result is None:
                return 502, {"ok": False, "reason": "peer_bad_response",
                             "target_host_id": peer_host_id,
                             "wakes_touchdesigner": False}
            result = dict(result)
            result.setdefault("target_host_id", peer_host_id)
            result.setdefault("convoy_id", convoy_id)
            result.setdefault("target_node_id", target_node_id)
            result.setdefault("wakes_touchdesigner", False)
            if result.get("ok") is True:
                artifact = result.get("artifact") or {}
                if artifact.get("artifact_id") != protected_artifact_id:
                    return 422, {
                        "ok": False, "reason": "artifact_corrupt",
                        "detail": "materialized artifact identity changed",
                        "wakes_touchdesigner": False,
                    }
                result["relay_protection_id"] = relay_protection_id
                self._audit_best_effort(
                    "artifact_materialized_from_peer", {
                        "peer_host_id": peer_host_id,
                        "convoy_id": convoy_id,
                        "node_id": target_node_id,
                        "artifact_id": artifact.get("artifact_id"),
                        "size": artifact.get("size"),
                    })
                protection_handed_off = True
                return 200, result
            return self._artifact_relay_status(result), result
        finally:
            self.end_artifact_transfer()
            if (protected_artifact_id and not protection_handed_off):
                try:
                    self.artifacts.release(
                        convoy_id, protected_artifact_id,
                        relay_protection_id,
                        expected_kind="active_transfer")
                except artifacts_mod.ArtifactError:
                    pass

    def release_relay_artifact(self, body):
        """Release one authenticated local relay handoff, idempotently."""
        try:
            convoy_id = identity.normalize_convoy_id(
                text_field(body, "convoy_id"))
            artifact_id = text_field(body, "artifact_id")
            protection_id = text_field(body, "relay_protection_id")
        except (Malformed, identity.IdentityError) as exc:
            detail = getattr(exc, "detail", str(exc))
            return self._refuse(
                "artifact_release", "malformed", detail, 400)
        if not re.fullmatch(r"relay:[0-9a-f]{32}", protection_id):
            return self._refuse(
                "artifact_release", "malformed",
                "invalid relay protection id", 400)
        try:
            remaining = self.artifacts.release(
                convoy_id, artifact_id, protection_id,
                expected_kind="active_transfer")
        except artifacts_mod.ArtifactNotFound:
            remaining = 0
        except artifacts_mod.ArtifactError as exc:
            return self._artifact_error(exc)
        return 200, {
            "ok": True, "convoy_id": convoy_id,
            "artifact_id": artifact_id,
            "released": remaining == 0,
            "wakes_touchdesigner": False,
        }

    def relay_cancel(self, body):
        """Cancel a local-sibling or remote-peer delivery by exact owner."""
        try:
            peer_host_id = text_field(body, "target_host_id")
            convoy_id = identity.normalize_convoy_id(
                text_field(body, "convoy_id"))
            delivery_id = text_field(body, "delivery_id")
        except (Malformed, identity.IdentityError) as exc:
            detail = getattr(exc, "detail", str(exc))
            return self._refuse("relay", "malformed", detail, 400)

        if peer_host_id == self.host_id:
            with self.lock:
                code, result = self._cancel_job_locked(
                    delivery_id, expected_convoy_id=convoy_id,
                    expected_origin_host_id=self.host_id)
            result = dict(result)
            result.setdefault("target_host_id", self.host_id)
            result.setdefault("convoy_id", convoy_id)
            result.setdefault("local_sibling", True)
            result.setdefault("wakes_touchdesigner", False)
            return code, result

        with self.lock:
            record = self.peers.get(peer_host_id)
            decision = self.peers.authorize_peer(
                peer_host_id,
                record.get("fingerprint") if record else None,
                convoy_id=convoy_id)
            if not decision.allowed:
                return self._refuse(
                    "relay", decision.reason, decision.detail,
                    _REFUSAL_HTTP.get(decision.reason, 403))
            targets, target_error = self._peer_targets_from_record(record)
            keys = self.hostkeys
        if keys is None:
            return self._identity_unavailable()
        used_session, result = self._session_call_if_connected(
            peer_host_id, convoy_id, peerserver.SESSION_RPC_CANCEL,
            {"delivery_id": delivery_id},
            peerclient.DEFAULT_PEER_TIMEOUT_S)
        if not used_session:
            if not targets:
                return self._refuse("relay", "peer_endpoint_unknown",
                                    target_error, 409)
            self._audit_http_compat_fallback(
                peer_host_id, peerserver.SESSION_RPC_CANCEL)
            _target, result = self._call_peer_targets(
                targets,
                lambda candidate, remaining: peerclient.cancel_peer_job(
                    candidate, keys, convoy_id, delivery_id,
                    timeout=remaining, pool=self.peer_pool),
                peerclient.DEFAULT_PEER_TIMEOUT_S)
        if result is peerclient.UNREACHABLE:
            return 503, {"ok": False, "reason": "peer_unreachable",
                         "target_host_id": peer_host_id,
                         "delivery_id": delivery_id}
        if isinstance(result, peerclient._PinMismatch):
            return 409, result.as_dict()
        if result is None:
            # The cancellation request may have arrived. Its outcome must be
            # reconciled by polling the original delivery.
            return 202, {
                "ok": False, "reason": "cancellation_indeterminate",
                "detail": "the peer may have accepted cancellation; query "
                          "the delivery for its definitive state",
                "target_host_id": peer_host_id,
                "convoy_id": convoy_id, "delivery_id": delivery_id,
            }
        result = dict(result)
        result.setdefault("target_host_id", peer_host_id)
        result.setdefault("convoy_id", convoy_id)
        result.setdefault("delivery_id", delivery_id)
        result.setdefault("wakes_touchdesigner", False)
        return (200 if result.get("ok") else int(
            result.get("http_status") or 409)), result

    def admit_peer(self, body):
        """Admit a peer against an EXPLICIT fingerprint the operator has
        compared out of band. Never a pin auto-update: the fingerprint is
        mandatory, and a change to it is audited as a re-admission.

        SELF-LOCKING (see _revoke_route): called WITHOUT self.lock. The
        admission lands O(1) under the lock; a RE-PIN then runs the
        revocation sweep lock-free, because a re-pin repudiates the old
        key and the work it authorized must not survive it.
        """
        try:
            host_id = text_field(body, "host_id")
            fingerprint = text_field(body, "fingerprint")
            display_name = text_field(body, "display_name", required=False)
            admitted_via = text_field(body, "admitted_via",
                                      required=False) or "manual"
        except Malformed as e:
            return self._refuse("peers", "malformed", e.detail, 400)
        # A PEER MAY NEVER CARRY THIS HOST'S OWN ID. Locality is inferred by
        # string-comparing ids -- _authorize_origin short-circuits on
        # `origin_host_id == self.host_id` and returns None, meaning "local,
        # no peer checks" -- so a peer admitted under this id would have its
        # STORED jobs skip both the may_mutate and the remote_exposed
        # re-checks at dispatch. That is the original blocker reopened
        # through the back door: measured, an ordinary peer's narrowed job
        # is refused 403 origin_revoked while a self-id peer's identical job
        # reaches the forward (409 node_unreachable). Submission-time checks
        # still fire, which is exactly what makes it easy to miss.
        #
        # The id is not secret -- GET /health publishes it unauthenticated by
        # design -- so it is freely choosable and the guard belongs here, at
        # the ONE place membership is granted, rather than at each of the
        # three places locality is inferred.
        if host_id and host_id.strip().lower() == self.host_id:
            return self._refuse(
                "peers", "peer_is_this_host",
                "that host_id is THIS host's own id, which the dispatcher "
                "reads as 'locally originated' and exempts from every peer "
                "check -- admitting it would let the peer's queued work "
                "bypass revocation. Use the peer's own host id (from ITS "
                "GET /health).", 400)
        with self.lock:
            # Read the OLD pin before admit overwrites it -- it is what
            # tells a re-pin from a first admission or a no-op re-admit.
            old_fp = self.peers.pinned_fingerprint(host_id)
            try:
                record = self.peers.admit(
                    host_id, fingerprint, admitted_via=admitted_via,
                    display_name=display_name,
                    endpoints=body.get("endpoints"),
                    convoy_ids=body.get("convoy_ids"),
                    cert_pem=body.get("cert_pem"),
                    clock_offset_s=body.get("clock_offset_s"))
                self._prune_peer_manifest_cache_locked(record["host_id"])
                self._invalidate_network_nodes_cache_locked()
            except peers_mod.PeerError as e:
                return self._refuse("peers", e.reason, e.detail,
                                    409 if e.reason == "peers_unreadable"
                                    else 400)
        self._audit_best_effort("peer_admitted",
                                {"host_id": record["host_id"],
                                 "peer_digest": peers_mod.peer_digest(
                                     record["host_id"],
                                     record["fingerprint"]),
                                 "admitted_via": record["admitted_via"]})
        self.reconcile_peer_sessions()
        # A RE-PIN REPUDIATES THE OLD KEY, so its queued work must burn.
        # The per-dispatch re-check re-derives the CURRENT pin, so without
        # this a job the old key submitted would be re-authorized under
        # the NEW key and forwarded -- the pin_mismatch BURN case that
        # _refuse_origin documents but nothing reached, because admit ran
        # no sweep at all. Only queued work is affected and there is no
        # new-key work yet (admit just returned), so this cannot burn the
        # peer's future. Lock-free (SELF-LOCKING _revoke_peer_work).
        summary = None
        if old_fp is not None and old_fp != record["fingerprint"]:
            summary = self._revoke_peer_work(record["host_id"],
                                             cause="repinned")
            self._audit_best_effort(
                "peer_repinned",
                {"host_id": record["host_id"],
                 "peer_digest": peers_mod.peer_digest(
                     record["host_id"], record["fingerprint"]),
                 **summary})
        resp = {"ok": True, "peer": record}
        if summary is not None:
            resp["revocation"] = summary
        return 200, resp

    def block_peer(self, body):
        """Block a peer: every class including X0, and REVOKE its work."""
        return self._revoke_route(body, peers_mod.PEER_BLOCKED)

    def forget_peer(self, body):
        """Drop the identity AND the pin, and revoke its work.

        Distinct from block on purpose: a blocked peer stays pinned (so
        an impersonator still trips pin_mismatch); a forgotten one is a
        stranger whose next join is a fresh decision.
        """
        return self._revoke_route(body, "forgotten")

    def observe_peer(self, body):
        """Narrow a peer to observe-only: X0 permitted, every mutation
        refused regardless of local gate state (24.6).

        NOT a full revocation. Its queued MUTATING jobs are terminalised
        (they can never be dispatched again, so leaving them queued would
        make /jobs lie) and its WRITER leases are released (it may no
        longer mutate, so an exclusive hold only blocks everyone else) --
        its reads keep working, which is the entire point of the state.

        SELF-LOCKING (see _revoke_peer_work): called WITHOUT self.lock.
        """
        try:
            host_id = text_field(body, "host_id")
        except Malformed as e:
            return self._refuse("peers", "malformed", e.detail, 400)
        with self.lock:
            try:
                record = self.peers.observe(host_id)
                self._prune_peer_manifest_cache_locked(record["host_id"])
                self._invalidate_network_nodes_cache_locked()
            except peers_mod.PeerError as e:
                return self._refuse(
                    "peers", e.reason, e.detail,
                    404 if e.reason == "unknown_peer" else 409)
        summary = self._revoke_peer_work(
            record["host_id"], cause="peer_observe_only",
            mutating_only=True, lease_modes=(controllers.LEASE_EXCLUSIVE,))
        self.reconcile_peer_sessions()
        return 200, {"ok": True, "peer": record, "revocation": summary}

    def _revoke_route(self, body, outcome):
        """SELF-LOCKING: called WITHOUT self.lock.

        The membership change lands FIRST and under the lock (it is O(1),
        and from that instant authorize_peer refuses the peer everywhere,
        including at every dispatch). Only then does the sweep run, and
        it runs lock-free.
        """
        try:
            host_id = text_field(body, "host_id")
        except Malformed as e:
            return self._refuse("peers", "malformed", e.detail, 400)
        with self.lock:
            try:
                if outcome == "forgotten":
                    record = self.peers.forget(host_id)
                else:
                    record = self.peers.block(host_id)
                self._prune_peer_manifest_cache_locked(record["host_id"])
                self._invalidate_network_nodes_cache_locked()
            except peers_mod.PeerError as e:
                return self._refuse(
                    "peers", e.reason, e.detail,
                    404 if e.reason == "unknown_peer" else 409)
        manager = self.session_manager
        if manager is not None:
            try:
                if outcome == "forgotten":
                    manager.remove_peer(record["host_id"])
                else:
                    manager.revoke_peer(record["host_id"])
            except sessions_mod.PairSessionError:
                pass
        summary = self._revoke_peer_work(record["host_id"], cause=outcome)
        self._audit_best_effort(
            "peer_revoked",
            {"host_id": record["host_id"], "outcome": outcome,
             "peer_digest": peers_mod.peer_digest(record["host_id"],
                                                  record["fingerprint"]),
             **summary})
        return 200, {"ok": True, "peer": record, "outcome": outcome,
                     "revocation": summary}

    def set_lan_killswitch(self, body):
        """A-32: the SAME predicate applied to every peer at once.

        REVERSIBLE, and it unwinds NO membership -- pins, admissions and
        observe-only narrowings are all untouched, so releasing it
        restores the mesh with nobody re-admitting anybody. It therefore
        terminalises NOTHING: queued peer work is SKIPPED by the drain
        while the switch is on and dispatches again when it is off.
        Leases are released, because an emergency stop that leaves a
        peer's exclusive hold blocking the local operator is not a stop.

        SELF-LOCKING, and this is THE EMERGENCY ROUTE, so the phases
        matter: the switch is set FIRST, under the lock, in O(1) -- from
        that instant every peer is refused everywhere. The job scan that
        finds the leases to drop then runs LOCK-FREE. It used to run one
        full scan PER PEER inside the global lock: 36.45s at 3000 jobs
        and 20 peers, during which the operator's own client hit its 10s
        timeout and RETRIED, starting the whole thing again.
        """
        engaged = body.get("engaged")
        if not isinstance(engaged, bool):
            return self._refuse("peers", "malformed",
                                "engaged must be true or false", 400)
        try:
            reason = text_field(body, "reason", required=False)
        except Malformed as e:
            return self._refuse("peers", "malformed", e.detail, 400)
        with self.lock:
            try:
                state = self.peers.set_killswitch(engaged, reason)
            except peers_mod.PeerError as e:
                return self._refuse("peers", e.reason, e.detail, 409)
            hosts = [r["host_id"] for r in self.peers.peers()]
            self._peer_manifest_cache.clear()
            self._invalidate_network_nodes_cache_locked()
        self.reconcile_peer_sessions()
        released = 0
        if engaged:
            scan_failed = False
            try:
                records, unreadable = self.db.scan_jobs()
            except Exception:
                scan_failed = True
                records, unreadable = [], []  # switch already in force
            preserve_claims = {
                job.get("delivery_id") for job in records
                if job.get("state") in ("dispatching", "running")
                and isinstance(job.get("delivery_id"), str)
            }
            preserve_claims.update(
                delivery_id for delivery_id in (unreadable or ())
                if isinstance(delivery_id, str))
            with self.lock:
                if scan_failed:
                    # A total scan failure is strictly stronger than an
                    # unreadable individual record: we do not know any of
                    # the delivery ids that may already be executing.  Hold
                    # the global mutation fence and preserve every affected
                    # implicit claim until a later successful reconciliation
                    # can prove which jobs are terminal.
                    self._operation_job_scan_failed = True
                for host_id in hosts:
                    controller_ids = self._controllers_for_origin(
                        host_id, records)
                    host_preserve_claims = set(preserve_claims)
                    if scan_failed:
                        host_preserve_claims.update(
                            claim.get("delivery_id")
                            for claim in self.leases.operation_claims()
                            if claim.get("controller_id") in controller_ids
                        )
                    for controller_id in controller_ids:
                        released += self.leases.release_controller(
                            controller_id,
                            preserve_claims=host_preserve_claims)
        self._audit_best_effort("lan_killswitch",
                                {"engaged": engaged, "reason": reason,
                                 "leases_released": released})
        return 200, {"ok": True, "killswitch": state,
                     "leases_released": released}

    def denylist_identity(self, body):
        """Loopback-only: block a LAN identity that has NO peer record.

        The realm-conflict recovery path for strangers: /peers/block
        routes through PeerStore state and 404s for a host it never
        admitted, while the identity that just wedged the realm is by
        definition un-admitted. Appends to the hand-editable
        denylist.json (folded matching, fail-closed semantics preserved)
        and audits what was blocked and why.
        """
        host_id = str(body.get("host_id") or "").strip()
        fingerprint = str(body.get("fingerprint") or "").strip()
        try:
            snapshot = self.peers.denylist.add(host_id=host_id or None,
                                               fingerprint=fingerprint
                                               or None)
        except peers_mod.PeerError as e:
            return self._refuse("peers", e.reason, e.detail, 400)
        self._audit_best_effort("peer_denylisted", {
            "host_id": host_id, "fingerprint": fingerprint,
            "reason": str(body.get("reason") or "operator"),
        })
        return 200, {"ok": True, "denylist": snapshot}

    def quarantine_peers(self, body):
        """Move an UNREADABLE peers.json aside so the host is operable.

        The in-band recovery an unreadable store previously had none of:
        admit, block, forget and the killswitch all refused, leaving a
        host-private file to hand-edit during an incident. It DESTROYS
        MEMBERSHIP -- every admission and every block -- so it demands an
        explicit confirm, refuses a store the host can still read, and
        never deletes the damaged file.
        """
        if body.get("confirm") is not True:
            return self._refuse(
                "peers", "confirm_required",
                "quarantine discards every admission and every block on "
                "this host; pass confirm=true if that is what you want",
                400)
        try:
            kept = self.peers.quarantine()
            self._peer_manifest_cache.clear()
            self._invalidate_network_nodes_cache_locked()
        except peers_mod.PeerError as e:
            return self._refuse("peers", e.reason, e.detail,
                                409 if e.reason == "peers_readable" else 500)
        # HOLD THE EMERGENCY STOP across recovery. quarantine wiped
        # membership during an incident; resuming peer dispatch the moment
        # a peer is re-admitted is the opposite of what recovering a
        # corrupted SECURITY store should do. Re-engage through the store
        # API so it PERSISTS (a fresh peers.json carries it), and surface
        # it -- silently changing an emergency stop is the finding.
        try:
            self.peers.set_killswitch(
                True, "peers.json quarantined -- emergency stop held; "
                      "lift it once the mesh is trusted again")
        except peers_mod.PeerError:
            pass    # engaging never raises on a readable store; be safe
        # Membership is gone, so every lease a peer held is orphaned --
        # release them, the same reason every other membership-destroying
        # route does (an exclusive hold left behind would keep blocking
        # local mutations for the rest of its TTL). Under the app lock
        # already (this route runs in _post_locked), so touch the lease
        # registry directly rather than the self-locking sweep.
        released = 0
        now = self._now()
        for lease in self.leases.live_leases(now):
            cid = lease.get("controller_id", "")
            if cid.startswith(peers_mod.CONTROLLER_NAMESPACE):
                # Quarantine runs under the app lock and cannot safely scan a
                # large jobs directory here. Preserve implicit claims; the
                # durable-aware reconciler later releases queued/terminal
                # ones, while in-flight writers remain fenced.
                preserved = {
                    claim.get("delivery_id")
                    for claim in self.leases.operation_claims()
                    if claim.get("controller_id") == cid
                }
                self.leases.release_controller(
                    cid, preserve_claims=preserved)
                released += 1
        killswitch = self.peers.killswitch()
        self._audit_best_effort(
            "peers_quarantined",
            {"kept_at": kept, "leases_released": released,
             "lan_killswitch": bool(killswitch.get("engaged"))})
        return 200, {
            "ok": True, "quarantined": kept, "leases_released": released,
            # SURFACED, never silent: quarantine holds the emergency stop
            # engaged (see PeerStore.quarantine), and an operator must be
            # told, because peer dispatch stays paused until they lift it.
            "lan_killswitch": bool(killswitch.get("engaged")),
            "detail": "peer records were discarded; the damaged file was "
                      "kept for inspection. Every peer must be admitted "
                      "again, and the LAN emergency stop is HELD ENGAGED "
                      "-- lift it with /lan/killswitch once the mesh is "
                      "trusted again."}

    def _revoke_peer_work(self, host_id, cause, mutating_only=False,
                          lease_modes=None):
        """Apply A-7 revocation to work ALREADY IN THE MESH.

        FIVE CASES, FIVE DIFFERENT RIGHT ANSWERS -- and four of them are
        "do not touch it", which is the part that is easy to get wrong:

          queued      -> TERMINALISE `refused`, with evidence. It
                         provably never ran (it never left this host), so
                         mark_indeterminate would be a LIE: indeterminate
                         means "may have run" and is the precious record
                         that says so (16.4). Refused says what actually
                         happened -- this host declined to deliver it.
          dispatching -> LEAVE IT. The forward is in flight; you cannot
                         un-run something. Revocation stops NEW work, it
                         does not rewrite history, and the dispatcher's
                         own resolution (verdict / requeue / indeterminate)
                         is the honest record of what happened.
          running     -> LEAVE IT, AND KEEP POLLING. The node owns this
                         job and holds its verdict for 24h. Abandoning it
                         would manufacture a false indeterminate and
                         destroy a real answer that still exists.
          settled     -> COUNTED, AND BY STATE. A record that is already
                         terminal (or carries a state this store does not
                         know) is nothing a revocation can change -- but
                         it is still a record this sweep EXAMINED, and
                         the summary is evidence, so it has to add up.
                         See _account_untouched for what silence cost.
          leases      -> RELEASED IMMEDIATELY. A revoked peer's exclusive
                         hold must not keep blocking local mutations for
                         the rest of its TTL.

        THE ACCOUNTING INVARIANT: `examined` equals the sum of refused,
        errors, left_in_flight, left_running, already_terminal,
        left_queued and unknown_state. The one entry it cannot cover is
        a scan that failed OUTRIGHT: that path examined no records at
        all (examined stays 0) and its single `errors` is the failure to
        enumerate, not a record.

        SELF-LOCKING, and called WITHOUT self.lock. The scan runs
        lock-free (jobs() is O(every job file on disk); drain_once was
        explicitly rewritten to keep exactly this scan out of the lock,
        and holding it here stalled the WHOLE host for 8.1s at 300 jobs
        -- on the emergency path). Each mark_refused then takes the lock
        for one file write, which is what keeps its read-then-CAS
        serialized against a concurrent claim_for_dispatch. A job that
        becomes queued AFTER this snapshot is not missed containment:
        every dispatch re-authorizes its origin, which is the real
        guarantee -- this sweep is the fast path, not the fence.
        """
        summary = {"refused": 0, "left_in_flight": 0, "left_running": 0,
                   "leases_released": 0, "errors": 0, "unreadable": 0,
                   "examined": 0,
                   # THE ARMS THAT DID NOT EXIST. queued/dispatching/
                   # running were the only states this loop could count,
                   # so every OTHER record fell out of the summary
                   # silently: a windows-latest run measured examined
                   # 250 against buckets summing to 14, and neither the
                   # operator's response nor the `peer_revoked` audit
                   # line said where the remaining 236 went.
                   "already_terminal": 0,
                   "left_queued": 0,
                   "unknown_state": 0,
                   # ...and 236 is a number, where "succeeded: 236" is
                   # an answer. Carried in the summary rather than a
                   # separate audit line because the summary IS the
                   # audit payload (**summary) as well as the response.
                   "untouched_states": {}}
        scan_failed = False

        def _account_untouched(bucket, state):
            """Count a record this sweep deliberately left alone, by state.

            Every arm that does not transition a record calls this, so
            the summary can never claim a containment it did not
            perform. `state` is whatever was on disk, which is not
            necessarily a string -- a record with no state at all is
            still a record, and must still be counted.
            """
            summary[bucket] += 1
            name = state if isinstance(state, str) and state else "<none>"
            summary["untouched_states"][name] = (
                summary["untouched_states"].get(name, 0) + 1)
        try:
            records, unreadable = self.db.scan_jobs()
        except Exception as e:
            # A scan that FAILED must never read as "no work found". The
            # revocation is INCOMPLETE and the operator has to be told.
            summary["errors"] += 1
            self._audit_best_effort(
                "peer_revocation_incomplete",
                {"origin_host_id": host_id, "detail": str(e)[:256],
                 "reason": "job_scan_failed"})
            scan_failed = True
            records, unreadable = [], []
        if unreadable:
            # UNREADABLE IS NOT ABSENT. get_job returns None for both, so
            # a sweep that could read nothing used to be byte-identical to
            # one that found nothing -- errors 0, and an operator told the
            # peer was contained.
            summary["unreadable"] = len(unreadable)
            self._audit_best_effort(
                "peer_revocation_incomplete",
                {"origin_host_id": host_id, "reason": "records_unreadable",
                 "count": len(unreadable), "delivery_ids": unreadable[:16]})
        mine = [j for j in records if j.get("origin_host_id") == host_id]
        preserve_claims = {
            job.get("delivery_id") for job in mine
            if job.get("state") in ("dispatching", "running")
            and isinstance(job.get("delivery_id"), str)
        }
        preserve_claims.update(
            delivery_id for delivery_id in (unreadable or ())
            if isinstance(delivery_id, str))
        summary["examined"] = len(mine)
        for job in mine:
            state = job.get("state")
            delivery_id = job.get("delivery_id")
            if state == "queued":
                if mutating_only:
                    entry = self.operations.get(job.get("operation"))
                    # An operation no longer in the registry counts as
                    # mutating: strict default, same as gating_of.
                    if entry is not None and not gating_of(entry)["mutating"]:
                        # A READ survives observe-only and will still
                        # dispatch. That is the right answer, and it is
                        # not the same answer as "contained" -- so it is
                        # counted, never skipped out of the arithmetic.
                        _account_untouched("left_queued", state)
                        continue
                try:
                    with self.lock:
                        refused = self.db.mark_refused(delivery_id, {
                            "reason": "peer_revoked",
                            "cause": cause,
                            # "never DISPATCHED" would be false: a
                            # requeued job has attempts >= 1 and a forward
                            # WAS attempted (it was never delivered). Only
                            # "never ran" is true of every case, and the
                            # attempts counter is right here beside it.
                            "detail": "the peer that submitted this job is "
                                      "no longer admitted on this host; it "
                                      "was never delivered to a node and "
                                      "never ran",
                            "peer_reason": cause,
                            "origin_host_id": host_id,
                            "operation": job.get("operation"),
                            "attempts": int(job.get("attempts") or 0),
                            "at": self._now()})
                except Exception:
                    summary["errors"] += 1
                    continue
                if refused is None:
                    # mark_refused DECLINED (the job left queued under us,
                    # or its file vanished). Counting it anyway made the
                    # summary -- which is returned to the operator AND
                    # written into the audit line -- claim refusals that
                    # never happened. Count only what transitioned.
                    summary["errors"] += 1
                    continue
                summary["refused"] += 1
                with self.lock:
                    if self._release_operation_claim_locked(refused):
                        summary["leases_released"] += 1
                self._audit_best_effort(
                    "peer_revocation_refused_job",
                    {"delivery_id": delivery_id, "origin_host_id": host_id})
            elif state == "dispatching":
                summary["left_in_flight"] += 1
                self._audit_best_effort(
                    "peer_revocation_left_in_flight",
                    {"delivery_id": delivery_id, "origin_host_id": host_id,
                     "detail": "a forward was already in flight; it is "
                               "allowed to finish and be recorded honestly"})
            elif state == "running":
                summary["left_running"] += 1
                self._audit_best_effort(
                    "peer_revocation_keeps_polling",
                    {"delivery_id": delivery_id, "origin_host_id": host_id,
                     "node_job_id": job.get("node_job_id"),
                     "detail": "the node owns this job and still holds its "
                               "verdict; polling continues to a terminal"})
            elif state in hoststore.TERMINAL_STATES:
                # SETTLED BEFORE THE SWEEP SAW IT. Nothing to contain and
                # nothing to rewrite -- a terminal is the record of what
                # already happened -- but it was examined, so it counts.
                _account_untouched("already_terminal", state)
            else:
                # Not a JOB_STATE at all. Unreachable through this store's
                # own writers; reachable through a hand-edited or damaged
                # record, which is exactly when an operator needs the
                # arithmetic to hold rather than quietly lose the row.
                _account_untouched("unknown_state", state)
        if summary["untouched_states"]:
            self._audit_best_effort(
                "peer_revocation_left_untouched",
                {"origin_host_id": host_id,
                 "states": dict(summary["untouched_states"]),
                 "detail": "these records were examined and deliberately "
                           "not transitioned; they are counted so the "
                           "summary accounts for every record it saw"})
        with self.lock:
            controller_ids = self._controllers_for_origin(host_id, records)
            if scan_failed:
                # Do not translate "could not enumerate jobs" into "there
                # are no jobs".  Preserve all claims attributable to this
                # peer and make every new mutation fail closed until the job
                # store can be scanned and reconciled successfully.
                self._operation_job_scan_failed = True
                preserve_claims.update(
                    claim.get("delivery_id")
                    for claim in self.leases.operation_claims()
                    if claim.get("controller_id") in controller_ids
                )
            for controller_id in controller_ids:
                summary["leases_released"] += self.leases.release_controller(
                    controller_id, modes=lease_modes,
                    preserve_claims=preserve_claims)
            if lease_modes is None:
                self._peer_controllers.pop(host_id, None)
        return summary


def _reject_json_constant(name):
    raise ValueError(f"{name} is not permitted in a request body")


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            pass

        def _send(self, code, payload, extra_headers=None):
            try:
                # allow_nan=False: Python's json emits BARE NaN /
                # Infinity tokens, which are not JSON and which strict
                # parsers reject -- a response no client can read is a
                # failure, so it becomes a named 500 instead.
                body = json.dumps(payload, allow_nan=False).encode("utf-8")
            except ValueError:
                code = 500
                body = json.dumps({"ok": False, "reason": "internal_error",
                                   "detail": "unserializable response"
                                   }).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            for name, value in (extra_headers or {}).items():
                if name.lower() not in ("content-type", "content-length"):
                    self.send_header(name, str(value))
            if self.close_connection:
                self.send_header("Connection", "close")
            self.end_headers()
            try:
                self.wfile.write(body)
            except OSError:
                pass

        def _send_artifact_stream(self, code, lease, headers):
            """Write verified bytes directly; never JSON/base64 content."""
            try:
                self.send_response(code)
                for name, value in headers.items():
                    self.send_header(name, str(value))
                self.end_headers()
                for block in lease:
                    self.wfile.write(block)
                self.wfile.flush()
            except OSError:
                # Client disconnected during a resumable transfer. Closing the
                # lease releases active-transfer protection; a fresh one-shot
                # capability can resume with Range.
                self.close_connection = True
            finally:
                lease.close()

        def _authenticated(self):
            provided = self.headers.get(TOKEN_HEADER) or ""
            # compare_digest raises TypeError on non-ASCII str, and
            # http.client decodes headers as iso-8859-1 -- so a single
            # 0xFF byte from an UNAUTHENTICATED caller crashed the auth
            # check itself (no 401, dead handler thread). Compare bytes.
            try:
                provided_bytes = provided.encode("ascii")
            except UnicodeEncodeError:
                return False
            return hmac.compare_digest(provided_bytes,
                                       app.token.encode("ascii"))

        def _read_body(self):
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return None
            if length <= 0 or length > MAX_BODY_BYTES:
                return None
            try:
                # parse_constant rejects JSON's NaN / Infinity /
                # -Infinity tokens, which Python accepts by default.
                # They are the fail-open vector for every numeric guard
                # downstream: `nan <= 0` and `nan > now` are BOTH False,
                # so a NaN slips past minimum checks and expiry checks
                # alike. Refuse them at the door, once.
                return json.loads(self.rfile.read(length).decode("utf-8"),
                                  parse_constant=_reject_json_constant)
            except ValueError:      # JSONDecodeError and UnicodeDecodeError
                return None

        def do_GET(self):
            if self.path == "/health":
                # The ONLY unauthenticated route: liveness + IDENTITY, no
                # secrets. host_id lets a client confirm it reached the
                # right host app (not a recycled pid) BEFORE it sends the
                # IPC token -- see convoy_hostprobe identity confirmation.
                # No app_version here: /health is the one pre-token
                # route, and the running version is a fingerprint other
                # local users have no business reading. Authenticated
                # surfaces (/status, the register response) carry it.
                self._send(200, {"ok": True, "protocol": "convoy-host/1",
                                 "host_id": app.host_id})
                return
            if not self._authenticated():
                self._send(401, {"ok": False, "reason": "unauthenticated"})
                return
            try:
                artifact_route = artifact_http.parse_route(
                    self.path, artifact_http.LOCAL_ROUTE_PREFIX)
                if artifact_route is not None:
                    action, convoy_id, artifact_id = artifact_route
                    if action != "download":
                        self._send(405, {"ok": False,
                                         "reason": "method_not_allowed"})
                        return
                    if not app.begin_artifact_transfer():
                        self._send(429, {"ok": False,
                                         "reason": "artifact_transfer_busy"})
                        return
                    try:
                        code, payload, lease, headers = (
                            app.artifact_open_local_download(
                                convoy_id, artifact_id,
                                artifact_http.range_from_headers(
                                    self.headers)))
                        if lease is None:
                            self._send(code, payload, headers)
                        else:
                            self._send_artifact_stream(code, lease, headers)
                    finally:
                        app.end_artifact_transfer()
                    return
                parsed_path = urllib.parse.urlsplit(self.path)
                route = parsed_path.path
                if route == "/status":
                    # OUTSIDE the lock: status() self-locks in phases so
                    # its jobs-dir scan does not starve every other route
                    # (see status()). Wrapping it in `with app.lock:`
                    # would deadlock -- threading.Lock is not reentrant.
                    code, payload = 200, app.status()
                    self._send(code, payload)
                    return
                if route == "/network/nodes":
                    # SELF-LOCKING and performs bounded peer I/O after its
                    # snapshot.  It must never run under app.lock.
                    query = urllib.parse.parse_qs(
                        parsed_path.query, keep_blank_values=True)
                    if set(query) - {"convoy_id"} or len(
                            query.get("convoy_id", [])) > 1:
                        code, payload = 400, {
                            "ok": False, "reason": "malformed",
                            "detail": "only one convoy_id query is allowed",
                        }
                    else:
                        values = query.get("convoy_id") or []
                        selected = values[0] if values else None
                        code, payload = app.network_nodes(selected)
                    self._send(code, payload)
                    return
                if route == "/network/controllers":
                    # SELF-LOCKING; peer fanout and job scans must run outside
                    # the global host lock. This route is passive/non-waking.
                    query = urllib.parse.parse_qs(
                        parsed_path.query, keep_blank_values=True)
                    allowed = {"convoy_id", "host_id", "node_id"}
                    if (set(query) - allowed
                            or any(len(query.get(name, [])) > 1
                                   for name in allowed)):
                        code, payload = 400, {
                            "ok": False, "reason": "malformed",
                            "detail": "convoy_id, host_id, and node_id may "
                                      "each appear at most once",
                        }
                    else:
                        def one(name):
                            values = query.get(name) or []
                            return values[0] if values else None
                        code, payload = app.network_controllers(
                            one("convoy_id"), one("host_id"), one("node_id"))
                    self._send(code, payload)
                    return
                with app.lock:
                    if route == "/nodes":
                        code, payload = 200, app.list_nodes()
                    elif route == "/policy":
                        query = urllib.parse.parse_qs(
                            parsed_path.query, keep_blank_values=True)
                        if set(query) - {"node_id"} or len(
                                query.get("node_id", [])) > 1:
                            code, payload = 400, {
                                "ok": False, "reason": "malformed",
                                "detail": "only one node_id query is allowed",
                            }
                        else:
                            values = query.get("node_id") or []
                            code, payload = app.get_policy(
                                values[0] if values else None)
                    elif route == "/manifest":
                        code, payload = app.get_manifest()
                    elif route.startswith("/manifest/"):
                        code, payload = app.get_manifest(
                            route[len("/manifest/"):])
                    elif route == "/identity":
                        code, payload = app.get_identity()
                    elif route == "/lan/status":
                        # LOOPBACK ONLY: an operator asks their OWN host
                        # whether the LAN listener is up. The peer leg has
                        # its own /peer/health; this is never in the LAN
                        # route table.
                        code, payload = app.lan_status()
                    elif route == "/peers":
                        code, payload = app.list_peers()
                    elif route == "/leases":
                        code, payload = app.list_leases()
                    elif route.startswith("/jobs/"):
                        code, payload = app.get_job(
                            route[len("/jobs/"):])
                    else:
                        code, payload = 404, {"ok": False,
                                              "reason": "not_found"}
            except artifact_http.ArtifactHTTPError as e:
                code, payload = e.status, e.payload()
                self._send(code, payload, e.headers)
                return
            except Exception as e:
                # LAST RESort, not the refusal mechanism: every expected
                # bad input is a named, audited 4xx before it reaches
                # here. This exists so an unforeseen bug cannot kill the
                # handler with NO response at all (the dead-thread
                # failure class the panel proved on the auth path).
                code, payload = 500, {"ok": False,
                                      "reason": "internal_error",
                                      "detail": type(e).__name__}
            self._send(code, payload)

        def do_POST(self):
            # Authenticate BEFORE parsing the body: an unauthenticated
            # caller gets no parser surface at all.
            if not self._authenticated():
                self._send(401, {"ok": False, "reason": "unauthenticated"})
                return
            try:
                artifact_route = artifact_http.parse_route(
                    self.path, artifact_http.LOCAL_ROUTE_PREFIX)
            except artifact_http.ArtifactHTTPError as e:
                self.close_connection = True
                self._send(e.status, e.payload(), e.headers)
                return
            if artifact_route is not None and artifact_route[0] == "upload":
                try:
                    metadata = artifact_http.upload_metadata(
                        self.headers, app.artifacts.max_artifact_bytes)
                except artifact_http.ArtifactHTTPError as e:
                    self.close_connection = True
                    self._send(e.status, e.payload(), e.headers)
                    return
                if not app.begin_artifact_transfer():
                    self.close_connection = True
                    self._send(429, {"ok": False,
                                     "reason": "artifact_transfer_busy"})
                    return
                reader = artifact_http.LimitedReader(
                    self.rfile, metadata["expected_size"])
                try:
                    code, payload = app.artifact_upload(
                        artifact_route[1], reader, metadata)
                except (OSError, ConnectionError):
                    self.close_connection = True
                    return
                finally:
                    app.end_artifact_transfer()
                # Raw uploads are one request per connection.  Even on
                # success, bytes beyond the declared Content-Length must not
                # be reinterpreted as a pipelined second request.
                self.close_connection = True
                self._send(code, payload)
                return
            body = self._read_body()
            if not isinstance(body, dict):
                self._send(400, {"ok": False, "reason": "malformed"})
                return
            try:
                # /dispatch, /drain and /poll are handled OUTSIDE the
                # lock block: they self-lock in phases so the forward
                # I/O runs lock-free, and threading.Lock is not reentrant
                # -- wrapping these in `with app.lock:` would deadlock on
                # the first claim. They still flow into the SINGLE _send
                # below, like every other route.
                if (artifact_route is not None
                        and artifact_route[0] == "capability"):
                    code, payload = app.artifact_local_grant(
                        artifact_route[1], artifact_route[2], body)
                elif artifact_route is not None:
                    code, payload = 405, {
                        "ok": False, "reason": "method_not_allowed"}
                elif self.path == "/dispatch":
                    code, payload = app.dispatch_job(
                        body.get("delivery_id"))
                elif self.path == "/jobs/cancel":
                    # Self-locking: an in-flight host subprocess owns its
                    # cancellation Event under app.lock, then observes it
                    # outside the lock in ProcessRunner.
                    code, payload = app.cancel_host_job(body)
                elif self.path == "/jobs/ack":
                    code, payload = app.acknowledge_job(body)
                elif self.path == "/register":
                    # SELF-LOCKING: it may probe an incumbent runtime's
                    # loopback port before mutating the registry.
                    code, payload = app.register_node(body)
                elif self.path == "/drain":
                    code, payload = app.drain()
                elif self.path == "/poll":
                    code, payload = app.poll_job(body.get("delivery_id"))
                elif self.path == "/relay":
                    code, payload = app.relay_submit(body)
                elif self.path == "/relay/job":
                    code, payload = app.relay_job(body)
                elif self.path == "/relay/ack":
                    code, payload = app.relay_acknowledge(body)
                elif self.path == "/relay/heartbeat":
                    code, payload = app.relay_heartbeat(body)
                elif self.path == "/relay/artifact":
                    code, payload = app.relay_artifact(body)
                elif self.path == "/relay/artifact/release":
                    code, payload = app.release_relay_artifact(body)
                elif self.path == "/artifact/export":
                    code, payload = app.export_artifact_to_project(body)
                elif self.path == "/relay/cancel":
                    code, payload = app.relay_cancel(body)
                elif self.path == "/owlette":
                    # Public HTTPS consumer, not a peer route.  It performs
                    # bounded Internet I/O and therefore must stay outside
                    # the host's coordination lock.
                    code, payload = app.owlette_action(body)
                # The revocation routes self-lock in phases too: the
                # membership change is O(1) under the lock, and the job
                # sweep that follows runs lock-free. /lan/killswitch is
                # the EMERGENCY route and must never be the slowest one.
                elif self.path == "/peers/block":
                    code, payload = app.block_peer(body)
                elif self.path == "/peers/forget":
                    code, payload = app.forget_peer(body)
                elif self.path == "/peers/observe":
                    code, payload = app.observe_peer(body)
                elif self.path == "/lan/killswitch":
                    code, payload = app.set_lan_killswitch(body)
                elif self.path == "/peers/admit":
                    # SELF-LOCKING like the revocation routes: a RE-PIN
                    # runs the job sweep, which cannot hold the app lock.
                    code, payload = app.admit_peer(body)
                else:
                    code, payload = self._post_locked(body)
            except Exception as e:      # same last-resort contract
                code, payload = 500, {"ok": False,
                                      "reason": "internal_error",
                                      "detail": type(e).__name__}
            self._send(code, payload)

        def _post_locked(self, body):
            with app.lock:
                if self.path == "/unregister":
                    return app.unregister_node(body)
                if self.path == "/nodes/forget":
                    # Advanced LOCAL recovery ("Forget Stale Node", 7.5).
                    # Loopback-only by construction: the LAN peer server is a
                    # separate class with its own table.
                    return app.forget_node(body)
                if self.path == "/remint":
                    return app.remint_node(body)
                if self.path == "/jobs":
                    return app.create_job(body)
                if self.path == "/envelope":
                    # The loopback sentinel, passed EXPLICITLY: there is
                    # no default, so slice 3's listener cannot bypass peer
                    # authorization by forgetting to name an origin.
                    return app.submit_envelope(body, LOOPBACK_ORIGIN)
                if self.path == "/psk":
                    return app.issue_convoy_psk(body)
                if self.path == "/identity/rotate":
                    return app.rotate_identity(body)
                # Peers and the LAN killswitch: LOOPBACK ONLY. Slice 3's
                # peer handler is a SEPARATE CLASS with its own table, so
                # none of these can ever become LAN-reachable by someone
                # adding a branch to the wrong if-chain.
                if self.path == "/peers/quarantine":
                    return app.quarantine_peers(body)
                if self.path == "/peers/denylist":
                    # LOOPBACK ONLY: append an identity to denylist.json
                    # even when no peer record exists -- the sender class
                    # that poisons realms is precisely the one /peers/block
                    # cannot reach (no record, 404).
                    return app.denylist_identity(body)
                if self.path == "/realm/reset":
                    # Advanced LOCAL recovery for a split-realm conflict.
                    # Loopback-only by construction (this table is not on the
                    # LAN peer server's class).
                    return app.reset_realm(body)
                if self.path == "/leases":
                    return app.acquire_lease(body)
                if self.path == "/leases/release":
                    return app.release_lease(body)
                if self.path == "/heartbeat":
                    return app.heartbeat_controller(body)
                if self.path == "/policy/challenge":
                    return app.begin_policy_challenge(body)
                if self.path == "/policy/confirm":
                    return app.confirm_policy_challenge(body)
                if self.path == "/policy/decline":
                    return app.decline_policy_challenge(body)
                if self.path == "/policy/disable":
                    return app.disable_policy(body)
                if self.path == "/policy/artifact-quota":
                    return app.set_artifact_quota(body)
                if self.path == "/shutdown":
                    # Authenticated by do_POST like every other POST --
                    # an unauthenticated caller was already refused 401
                    # before the body was even parsed, which is what
                    # keeps "anything on this machine can stop it" from
                    # meaning "anything on the NETWORK can".
                    return app.request_shutdown(body)
                return 404, {"ok": False, "reason": "not_found"}

    return Handler


def serve(app, port=0):
    """Bind loopback, write the portfile, serve until shutdown.

    port=0 lets the OS pick -- clients find us via the portfile, so a
    fixed port (and its collision/failure modes) is never needed locally.
    """
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))
    actual_port = server.server_address[1]
    platform_mod.write_portfile(app.data_dir, actual_port, os.getpid(),
                                app.host_id)
    return server, actual_port


def desired_lan_endpoint(app):
    """Return the currently configured concrete LAN endpoint.

    Enabled Convoy membership is the exposure consent gate.  ``lan.json``
    is therefore optional, but when present its emergency-disable and bind
    overrides remain authoritative.  Keeping this resolution in one helper
    lets the lifecycle loop detect DHCP/NIC changes without first creating a
    second listener.
    """
    config = lan_mod.load_config(app.data_dir)
    if config.present and not config.enabled:
        raise lan_mod.LanConfigError(
            "admin_disabled", "Convoy LAN is disabled by lan.json")
    if not config.present:
        config = lan_mod.LanConfig(
            enabled=True, port=config.port, bind=config.bind, present=True)
    return lan_mod.resolve_bind(config), int(config.port)


def start_lan_if_configured(app, log=None):
    """Bind for enabled Convoy membership, with optional LAN overrides.

    ``Enable Convoy`` on at least one durable local node is the normal and
    mandatory exposure gate.  Absent ``lan.json`` uses the safe automatic
    interface and fixed discovery port; no hidden setup file is required.
    A present ``lan.json`` may still explicitly disable networking as an
    advanced host-admin emergency override or select an interface/port.

    Every refusal leaves loopback service alive and records a named reason.
    No branch binds a wildcard address.
    """
    say = log or (lambda msg: sys.stderr.write(msg + "\n"))
    if app.lan_server is not None:
        return True
    if not app.active_convoy_ids():
        app.lan_reason = "no_enabled_nodes"
        return False
    # TLS needs the derived certificate, not just the signing key. No
    # cert -> the identity still signs envelopes locally, but no mutual
    # TLS is possible, so the LAN listener stays down with a named reason.
    if app.hostkeys is None:
        app.lan_reason = "no_identity"
        say("convoy LAN: refusing to bind -- no host identity "
            f"({app.identity_detail or hostkeys.CryptographyMissing().reason})")
        return False
    if not app.hostkeys.certificate_pem:
        app.lan_reason = "no_certificate"
        say("convoy LAN: refusing to bind -- the identity has no TLS "
            f"certificate ({app.hostkeys.cert_reason or 'unknown'})")
        return False
    try:
        address, configured_port = desired_lan_endpoint(app)
    except lan_mod.LanConfigError as e:
        app.lan_reason = e.reason
        say(f"convoy LAN: {e}")
        return False
    try:
        server, port = peerserver.serve_lan(
            app, address, configured_port)
    except peerserver.LanBindError as e:
        app.lan_reason = e.reason
        say(f"convoy LAN: {e}")
        return False
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    app.set_lan_server(server, thread, address, port)
    say(f"convoy LAN: peer listener on {address}:{port} "
        f"(pinned mutual TLS; identity {app.hostkeys.fingerprint})")
    return True


def build_parser():
    """The command line, as its own function so the flag wiring is
    testable without starting a daemon -- `--singleton` defaulting ON is
    a safety property, and a test that rebuilt its own parser to check
    it would be asserting against a copy."""
    parser = argparse.ArgumentParser(description="embody-convoy host app")
    parser.add_argument("--data-dir", default=None,
                        help="state directory (default: per-user app dir)")
    parser.add_argument("--port", type=int, default=0,
                        help="loopback port (default: OS-assigned)")
    parser.add_argument("--drain-interval", type=float, default=0.0,
                        help="seconds between autonomous ticks -- each "
                             "polls running node jobs, then dispatches "
                             "queued ones (0 = off; per-call only)")
    parser.add_argument("--singleton", dest="singleton",
                        action="store_true", default=True,
                        help="refuse to start when another host app "
                             "already holds this data dir (the default)")
    parser.add_argument("--no-singleton", dest="singleton",
                        action="store_false",
                        help="start even if another host app holds this "
                             "data dir -- UNSAFE, for tests only: two "
                             "daemons on one data dir burn each other's "
                             "live claims to indeterminate")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)

    directory = args.data_dir or platform_mod.data_dir()

    # BEFORE HostApp(), not after. HostStore.__init__ runs
    # _sweep_interrupted_dispatches() the moment it opens the data dir,
    # so a second daemon that got as far as constructing the app has
    # ALREADY burned the first one's in-flight claims to indeterminate --
    # and 16.4/A-15 make those records permanent. The lock has to come
    # first or it does not protect the thing it exists for.
    singleton = None
    if args.singleton:
        singleton = platform_mod.acquire_singleton(directory)
        if singleton is None:
            # EXIT 0, deliberately. The supervisor runs this every
            # minute by design (Repetition PT1M + IgnoreNew), so on a
            # healthy machine the daemon is ALREADY running and this is
            # the expected outcome, not a fault. A nonzero exit would
            # paint Task Scheduler's LastTaskResult as a failure once a
            # minute forever and bury a real one.
            sys.stderr.write(
                f"embody-convoy host app already running for {directory} "
                f"(another process holds host.lock); this launch is a "
                f"no-op\n")
            sys.stderr.flush()
            return 0

    try:
        app = HostApp(directory)
    except Exception:
        platform_mod.release_singleton(singleton)
        raise
    try:
        server, port = serve(app, args.port)
    except Exception:
        # A bind failure must not strand the slot: the kernel would drop
        # it on process exit, but an in-process caller (a test, or a
        # supervisor calling main() twice) would refuse itself forever.
        app.db.close()
        platform_mod.release_singleton(singleton)
        raise
    if args.drain_interval > 0:
        app.start_drain_loop(args.drain_interval)
    # Independent of autonomous job draining: a crash after the durable
    # restart commit must restore the exact TouchDesigner node even when the
    # ordinary queue is configured for manual dispatch only.
    app.start_lifecycle_recovery_loop()
    # Membership drives LAN exposure dynamically.  With no enabled node the
    # lifecycle thread owns no socket; first registration binds/advertises,
    # and disabling the final node withdraws both discovery and peer TLS.
    app.start_lan_lifecycle()
    sys.stderr.write(
        f"embody-convoy host {app.host_id[:8]} on 127.0.0.1:{port} "
        f"(data: {directory})\n")
    sys.stderr.flush()

    # A supervisor stops us with SIGTERM (Scheduled Task / LaunchAgent,
    # A-36). Without a handler, Python does not unwind -- the `finally`
    # below never runs and the portfile outlives the process, pointing
    # clients at a dead port. Handle it so the COMMON stop is clean;
    # clients still verify liveness, because SIGKILL/power-loss can
    # never be handled here.
    #
    # SHUTDOWN ORDER (A-46 point 4): stop accepting PEER connections
    # FIRST, unconditionally, THEN the loopback server. The LAN listener
    # is the off-box surface; it goes down before anything else so no new
    # peer work can arrive while the daemon is winding down.
    def _stop(signum=None, _frame=None):
        def _teardown():
            app.stop_lan_lifecycle()
            server.shutdown()
        threading.Thread(target=_teardown, daemon=True).start()

    # ONE shutdown path, two triggers: the signal above and the
    # authenticated POST /shutdown route. Stop/upgrade/uninstall use the
    # route so the `finally` below runs and the portfile is cleared;
    # a hard kill is what that exists to avoid.
    app.set_shutdown_hook(_stop)

    for signame in ("SIGTERM", "SIGINT", "SIGBREAK"):
        sig = getattr(signal, signame, None)
        if sig is not None:
            try:
                signal.signal(sig, _stop)
            except (ValueError, OSError):
                pass        # not the main thread, or unsupported here

    try:
        server.serve_forever()
    finally:
        # LAN listener FIRST (idempotent -- _stop may already have stopped
        # it), THEN the drain loop, THEN the portfile: the plan's exact
        # order, so no peer connection is accepted after the daemon has
        # begun clearing its own state.
        try:
            app.stop_lan_lifecycle()
        except Exception:
            pass
        try:
            app.stop_drain_loop()
        except Exception:
            # Shutdown hygiene must not depend on the loop stopping
            # cleanly: a raise here skipped clear_portfile and left a
            # portfile pointing at a dead port.
            pass
        try:
            app.stop_lifecycle_recovery_loop()
        except Exception:
            pass
        try:
            app.shutdown_network_queries()
        except Exception:
            pass
        platform_mod.clear_portfile(directory)
        app.db.audit("hostapp", "stopped", {})
        app.db.close()
        # Last of all: the kernel drops this on process exit anyway, but
        # an in-process caller (a test, or a supervisor that runs main()
        # twice) must get the slot back without exiting.
        platform_mod.release_singleton(singleton)
    return 0


if __name__ == "__main__":
    main()
