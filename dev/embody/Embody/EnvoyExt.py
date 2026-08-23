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

import ast
import asyncio
import contextvars
import fnmatch
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
import time
from collections import deque
from html import unescape
from queue import Queue, Empty
from threading import Lock, Event, Thread
from typing import Optional, Any, Callable, Literal

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
    'set_annotation', 'run_tests', 'batch_operations', 'save_project',
    'update_embody',
})

# Coarse scopes for operations whose footprint is not a single op path.
_SPECIAL_SCOPES = {
    'execute_python': 'project:python',
    'run_tests': 'project:tests',
    'save_project': 'project:save',
    'update_embody': 'project:update',
}

_TOUCH_WINDOW_S = 600     # advisories consider touches this recent
_CONFLICT_WINDOW_S = 60   # peer WRITE inside this + own WRITE = conflict
_ADVISORY_DEDUP_S = 300   # same (peer, scope) advisory re-served after this
_TOUCH_RING_CAP = 8       # touches kept per scope
_TOUCH_SCOPE_CAP = 200    # scopes kept (evict oldest-touched beyond this)

_PATH_PARAM_KEYS = ('op_path', 'parent_path', 'source_path', 'dest_path',
                    'target_path', 'comp_path', 'root_path')


# --- Recovery hints: reactive guidance on error envelopes ---
#
# When a tool returns {'error': ...}, match the message against this small
# curated table and ride a 'recovery_hints' list back on the envelope so the
# agent's NEXT step is steered instead of a blind retry of the same failing
# call. Purely additive -- a match adds a hint, a miss changes nothing, and a
# fault here never touches the response (the caller wraps it in try/except).
# The reactive cousin of the .claude skills: the same hard-won knowledge,
# delivered at the moment of failure rather than relying on a pre-loaded doc.
#
# Each entry is (compiled_regex, cause, action, next_tools). Keep it small and
# tuned to errors an agent ACTUALLY hits -- noise here trains the agent to
# ignore the block. `next_tools` are real Envoy tool names.
_RECOVERY_HINT_RULES = [
    (re.compile(r'(operator|parent|source|destination|comp|op) not found'
                r'|does not exist|no operator at', re.IGNORECASE),
     'the operator path does not resolve',
     "Never guess paths. Call query_network on the parent COMP (or '/') to "
     "list real children, or find_children to search by name, then retry with "
     "a verified path. Annotations are utility ops: pass include_utility=True "
     "to query_network/find_children or they will not appear (get_annotations "
     "always sees them). The active network is "
     "execute_python: result = ui.panes.current.owner.path.",
     ['query_network', 'find_children', 'get_op', 'get_annotations']),

    (re.compile(r'parameter not found|no parameter', re.IGNORECASE),
     'no parameter by that name on the operator',
     "List the operator's real parameters with get_op (or read_tdn for a TDN "
     "COMP) before setting. Custom-parameter names are Capitalized; built-in "
     "names are lowercase.",
     ['get_op', 'get_parameter']),

    (re.compile(r'is not a top|is not a comp|\(family:|wrong family',
                re.IGNORECASE),
     'the operator is the wrong family for this tool',
     "Check the operator's family with get_op. capture_top needs a TOP; "
     "connect_ops needs compatible families; annotations need a COMP.",
     ['get_op', 'query_network']),

    (re.compile(r'no pixel data available', re.IGNORECASE),
     'the TOP produced an empty texture (zero resolution or never cooked)',
     "Check the TOP's resolution and whether it cooked "
     "(get_op_performance -> cookedThisFrame), verify a Null terminates the "
     "chain and no bypass flag is set, then force a cook and retry. See the "
     "debug-operator skill.",
     ['get_op_performance', 'get_op_errors', 'get_op']),

    (re.compile(r'thread conflict|outside the main thread|main-thread',
                re.IGNORECASE),
     'a TD object was touched off the main thread, or a raw op was returned',
     "Don't return raw op()/parent() objects from execute_python -- assign "
     "strings instead (result = op('x').path). Resolve any values on the main "
     "thread before returning.",
     ['execute_python']),

    (re.compile(r'unknown (op|operator) type|not a valid operator'
                r"|has no attribute '\w+(TOP|CHOP|SOP|DAT|COMP|MAT|POP)'",
                re.IGNORECASE),
     'the operator type name is misspelled or unavailable in this build',
     "Operator type names are exact (e.g. noiseTOP, not noise). Confirm the "
     "spelling and availability via get_docs before create_op.",
     ['get_docs', 'get_td_classes']),

    (re.compile(r'timed out after|operation timed out', re.IGNORECASE),
     'the operation exceeded the MCP timeout (main-thread work too heavy)',
     "Break the work into smaller steps; check get_project_performance for a "
     "cook stall, and prefer batch_operations over many single calls.",
     ['get_project_performance', 'batch_operations']),
]


def _recovery_hints_for(message: str) -> list:
    """Return recovery-hint dicts for an error message (empty if none match).

    Pure and side-effect free so it is unit-testable without TD. Collects
    every matching rule, capped at 2 to stay token-lean."""
    if not message:
        return []
    hints = []
    for pattern, cause, action, next_tools in _RECOVERY_HINT_RULES:
        if pattern.search(message):
            hints.append({
                'cause': cause,
                'action': action,
                'next_tools': list(next_tools),
            })
            if len(hints) >= 2:
                break
    return hints


# --- Guidance: project doctrine for agents that never see .claude/ --------
#
# Embody ships its TouchDesigner doctrine as `.claude/rules/*.md` and
# `.claude/skills/<slug>/SKILL.md`. Claude Code loads those itself; agents
# running on Codex, Cursor, opencode and friends get the MCP tool
# descriptions and nothing else. get_guidance serves the SAME documents over
# MCP so every client can reach them.
#
# Worker-side: pure filesystem work, ZERO TD access (mcp-safety thread
# boundary). The helpers below are module-level and pure so the unit tests
# can exercise them without a live server.

_GUIDANCE_MAX_CHARS = 20000       # per-document response cap
_GUIDANCE_INDEX_TTL_S = 30.0      # re-scan the docs tree at most this often
_GUIDANCE_SUGGESTIONS = 3         # did-you-mean entries on a miss


def _guidance_key(name: str) -> str:
    """Comparison key for a topic id or query: lowercase, letters and digits
    only. 'create-operator', 'create_operator', '/create-operator' and
    'CreateOperator' all collapse to 'createoperator'. Pure."""
    return re.sub(r'[^a-z0-9]', '', (name or '').lower())


def _guidance_description(text: str) -> str:
    """One-line description of a guidance document.

    Prefers the YAML front-matter `description` field (skills carry one),
    falls back to the first markdown heading, then the first ordinary line.
    Returns '' when the document offers none. Pure -- no filesystem, no TD.
    """
    lines = (text or '').splitlines()
    front = False
    if lines and lines[0].strip() == '---':
        front = True
        for line in lines[1:]:
            stripped = line.strip()
            if stripped == '---':
                break
            if stripped.lower().startswith('description:'):
                value = stripped.split(':', 1)[1].strip()
                if (len(value) >= 2 and value[0] == value[-1]
                        and value[0] in ('"', "'")):
                    value = value[1:-1]
                value = value.strip()
                if value:
                    return value
    for index, line in enumerate(lines):
        stripped = line.strip()
        if front:
            # Skip the front-matter block itself (its closing '---').
            if index == 0:
                continue
            if stripped == '---':
                front = False
            continue
        if not stripped or stripped.startswith('<!--'):
            continue
        if stripped.startswith('#'):
            return stripped.lstrip('#').strip()
        return stripped
    return ''


def _match_guidance_topic(query: str, topic_ids) -> tuple:
    """Resolve a requested topic against the available ids.

    Returns (matched_id_or_None, suggestions_list). Matching is
    case-insensitive and punctuation-insensitive via _guidance_key, then
    tolerates near-misses: prefix, substring, and finally difflib's closest
    matches. Pure -- unit-testable with a plain list of ids.
    """
    ids = list(topic_ids or [])
    key = _guidance_key(query)
    if not key or not ids:
        return None, ids[:_GUIDANCE_SUGGESTIONS]
    keyed = [(_guidance_key(i), i) for i in ids]
    for candidate_key, topic_id in keyed:
        if candidate_key == key:
            return topic_id, []
    starts = [t for k, t in keyed if k.startswith(key) or key.startswith(k)]
    if len(starts) == 1:
        return starts[0], []
    contains = [t for k, t in keyed if key in k or k in key]
    if len(contains) == 1:
        return contains[0], []
    suggestions = []
    for topic_id in starts + contains:
        if topic_id not in suggestions:
            suggestions.append(topic_id)
    if len(suggestions) < _GUIDANCE_SUGGESTIONS:
        try:
            import difflib
            close = difflib.get_close_matches(
                key, [k for k, _t in keyed], n=_GUIDANCE_SUGGESTIONS, cutoff=0.6)
            by_key = dict(keyed)
            for candidate_key in close:
                topic_id = by_key.get(candidate_key)
                if topic_id and topic_id not in suggestions:
                    suggestions.append(topic_id)
        except Exception:
            pass
    return None, suggestions[:_GUIDANCE_SUGGESTIONS]


# --- Write-effect footer: did THIS write break something? -----------------
#
# After a mutating tool call, ride a compact '_effects' block back telling the
# agent whether IT just introduced operator errors or tanked the frame rate,
# instead of waiting to be asked. Diffed against a PER-SESSION snapshot so
# only NEW damage is reported; pre-existing errors stay silent.
#
# Like _attachRecoveryHints this is an ergonomics layer: it must never be able
# to break a response, so every entry point is wrapped and degrades to silence.

_EFFECTS_ERROR_CAP = 3        # newly-erroring ops listed
_EFFECTS_WARNING_CAP = 2      # newly-warning ops listed
_EFFECTS_FPS_DROP_FRAC = 0.85  # report below this fraction of the last sample
_EFFECTS_FPS_DROP_MIN = 5.0    # ...and only when the absolute drop is this big
# The project-wide error scan runs on the MAIN thread once per write. If it
# ever costs more than this, the footer disables that half for the session
# rather than taxing every subsequent write (performance.md: never make the
# user's TD slower to be helpful).
_EFFECTS_SCAN_BUDGET_S = 0.25


_EFFECTS_INTERNAL_ROOTS = ('/ui', '/sys')  # TD's own UI/system subtrees


def _new_error_entries(previous_paths, current_entries, cap):
    """Diff fresh errors/warnings against the previous snapshot.

    Returns (listed, total_new, current_paths); previous_paths None =
    first write, baseline only. Excludes TD's own subtrees (/ui, /sys)
    and pseudo-paths from multi-line warning strings (both seen on the
    footer's first field day, 2026-07-29). Pure and TD-free.
    """
    current_paths = set()
    fresh = []
    for entry in (current_entries or []):
        if not isinstance(entry, dict):
            continue
        path = entry.get('nodePath') or ''
        if not path or not path.startswith('/'):
            continue  # continuation line of a multi-line warning, not a path
        if path == '/' or any(path == r or path.startswith(r + '/')
                              for r in _EFFECTS_INTERNAL_ROOTS):
            continue  # TD-internal noise, not this write's doing
        current_paths.add(path)
        if previous_paths is not None and path not in previous_paths:
            fresh.append({'path': path,
                          'message': str(entry.get('message') or '')[:200]})
    # One line per op: repeated messages on the same node are noise.
    listed, seen = [], set()
    for item in fresh:
        if item['path'] in seen:
            continue
        seen.add(item['path'])
        listed.append(item)
    return listed[:max(0, cap)], len(listed), current_paths


def _fps_regression(previous_fps, current_fps) -> dict:
    """A meaningful frame-rate drop between two samples, or {}.

    Requires BOTH a proportional drop (below _EFFECTS_FPS_DROP_FRAC of the
    previous sample) and an absolute one (_EFFECTS_FPS_DROP_MIN fps), so
    ordinary jitter never reports. Pure."""
    try:
        was = float(previous_fps)
        now = float(current_fps)
    except (TypeError, ValueError):
        return {}
    if was <= 0 or now <= 0:
        return {}
    if now >= was * _EFFECTS_FPS_DROP_FRAC:
        return {}
    if (was - now) < _EFFECTS_FPS_DROP_MIN:
        return {}
    return {'now': round(now, 1), 'was': round(was, 1),
            'drop_pct': int(round((was - now) / was * 100))}


# --- Task ledger: shared work-STATE across sessions ------------------------
#
# Claims answer "who is touching what RIGHT NOW"; the ledger answers "what is
# the state of the WORK" -- in progress, finished-but-uncommitted, committed,
# abandoned. Born 2026-07-29: a session read another session's FINISHED
# (uncommitted) feature as in-flight and held its own work for nothing,
# because nothing shared records completion -- claims expire on silence, and
# each session's todo list is private to its own context.
#
# Storage: .embody/tasks.json at the AI project root, written ONLY through
# announce_task / update_task (worker-side file I/O under _sessions_lock,
# atomic replace -- the durable-worktree-claims idiom above).

_TASK_STATUSES = ('in_progress', 'done_uncommitted', 'committed', 'abandoned')
_TASK_ACTIVE_STATUSES = ('in_progress', 'done_uncommitted')
_TASK_TERMINAL_RETENTION_S = 7 * 24 * 3600.0  # committed/abandoned kept this long
_TASK_STALE_ABANDON_S = 14 * 24 * 3600.0  # silent in_progress -> abandoned after
_TASK_STALE_FLAG_S = 24 * 3600.0          # in_progress flagged stale= after
_TASK_TITLE_MAX = 120
_TASK_NOTE_MAX = 300
_TASK_SCOPES_MAX = 8
_TASK_LIST_CAP = 32


def _task_updated(task: dict) -> float:
    """A task's updated timestamp as a float, 0.0 on any garbage. Pure --
    the single guard every sort/merge/prune shares, so one malformed entry
    from a foreign writer can never take down the whole surface."""
    try:
        return float(task.get('updated', 0) or 0)
    except (TypeError, ValueError, AttributeError):
        return 0.0


def _prune_tasks(tasks: dict, now: float) -> dict:
    """Lifecycle pressure-release. Terminal tasks (committed/abandoned) are
    dropped after the retention window. done_uncommitted is kept forever --
    finished work sitting in the tree is the load-bearing state a peer must
    see, and only a commit or a deliberate transition may clear it. A stale
    in_progress, though, eventually IS the garbage (a dead session that
    never reported back): after _TASK_STALE_ABANDON_S it auto-transitions
    to abandoned (attributed to '_ledger_prune', still visible for the
    terminal retention window) so the ledger cannot grow without bound.
    Pure."""
    kept = {}
    for tid, task in (tasks or {}).items():
        if not isinstance(task, dict):
            continue
        status = task.get('status')
        age = now - _task_updated(task)
        if status in ('committed', 'abandoned'):
            if age > _TASK_TERMINAL_RETENTION_S:
                continue
        elif status == 'in_progress' and age > _TASK_STALE_ABANDON_S:
            task = dict(task)
            task['status'] = 'abandoned'
            task['updated_by'] = '_ledger_prune'
            task['note'] = ('auto-abandoned: no update for %d days'
                            % int(_TASK_STALE_ABANDON_S // 86400))
            task['updated'] = now
        kept[tid] = task
    return kept


def _task_public(task: dict, now: float) -> dict:
    """The client-facing shape of one ledger entry. Pure."""
    out = {key: task.get(key) for key in
           ('id', 'title', 'status', 'session', 'label', 'note', 'commit',
            'updated_by')}
    out['scopes'] = list(task.get('scopes') or [])
    try:
        out['age_s'] = round(now - float(task.get('created', now)), 1)
    except (TypeError, ValueError):
        out['age_s'] = None
    updated_age = now - _task_updated(task) if _task_updated(task) else None
    out['updated_age_s'] = (round(updated_age, 1)
                            if updated_age is not None else None)
    if (task.get('status') == 'in_progress' and updated_age is not None
            and updated_age > _TASK_STALE_FLAG_S):
        # A day of silence on an in_progress task usually means its session
        # died -- peers should treat the claim on that territory as soft.
        out['stale'] = True
    return {key: value for key, value in out.items()
            if value not in (None, '', [])}


# --- Job layer: long operations as disk-backed jobs ------------------------
#
# Long operations (a full test run, project.save) outlive the 30s MCP
# timeout, and a mid-operation server restart severs a synchronous call
# even though the work completes (observed twice on 2026-07-29: run_tests
# died with "Server force-restarted/shutting down during test run"; a save
# returned IncompleteRead). A job returns its handle IMMEDIATELY and parks
# results on disk (.embody/jobs/<id>.json), so they survive restarts and
# reinits; get_job_status polls. Records are os/json-only plain data --
# writable from the main thread (tiny file) and readable from the worker.
# This registry is the intended shape for future kinds (TDN export, movie
# export) -- see docs/roadmap.md.

_JOB_RETENTION_S = 24 * 3600.0    # finished records kept this long
_JOB_STALE_RUNNING_S = 30 * 60.0  # running-with-no-finish flagged after
_JOB_LIST_CAP = 16


def _jobs_dir():
    root = getattr(sys, '_envoy_repo_root', None)
    # A non-absolute root is a sentinel ('no-git') or garbage, never a
    # place to write: joining it would resolve RELATIVE to TD's cwd
    # (observed live 2026-07-29 -- a suite drove the config path, the
    # assigner cached 'no-git', and every job/ledger read went dark).
    if not root or not os.path.isabs(str(root)):
        return None
    return os.path.join(str(root), '.embody', 'jobs')


def _new_job(kind, params, idempotency_key=None):
    import uuid
    job = {'id': 'job_' + uuid.uuid4().hex[:8], 'kind': kind,
           'params': {k: v for k, v in (params or {}).items()
                      if v is not None},
           'status': 'running', 'started': time.time()}
    if idempotency_key:
        # A-22 / 16.5: the key a controller (or a retrying caller) uses to
        # reconcile a redelivered request to THIS job instead of running
        # the work twice. Absent for a direct call that opts out.
        job['idempotency_key'] = idempotency_key
    return job


def _job_path(job_id):
    d = _jobs_dir()
    if not d or not re.match(r'^job_[0-9a-f]{8}\Z', str(job_id or '')):
        return None  # id doubles as the filename -- never trust it raw
    return os.path.join(d, job_id + '.json')


def _write_job(job):
    """Atomic best-effort job-record write (os/json only).

    The os.replace is retried a few times back-to-back: on Windows a
    replace fails with a sharing violation while the worker-side
    get_job_status poll holds the record open for read -- and the write
    that collides is usually the one that matters (the terminal
    'done'/'error'). The reader's window is sub-millisecond, so immediate
    retries clear it without sleeping.
    """
    path = _job_path(job.get('id'))
    if not path:
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + '.%d.tmp' % os.getpid()
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(job, f, indent=1)
        for attempt in range(3):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == 2:
                    raise
    except Exception:
        pass


def _read_job(job_id):
    path = _job_path(job_id)
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, 'r', encoding='utf-8') as f:
            record = json.load(f)
        return record if isinstance(record, dict) else None
    except Exception:
        return None


# -- idempotency index (16.5): one marker file per key -----------------
#
# A redelivered request (same idempotency_key) must reconcile to the
# job it already minted, never run the work twice. The shipped layer had
# only save_project's 120s in-flight heuristic, which a retry at 121s --
# or any run_tests retry after a severed ack -- defeats by minting a
# second job. ONE marker file per key (idem_<sha256[:24]>.json holding
# {job_id, created}), not a shared index blob: a torn/locked read then
# affects one key and FAILS CLOSED, never annihilates the whole map (the
# defect the host-side store had, fixed 2026-07-31). Single-writer here
# (job starts are main-thread), so no cross-writer gate is needed.

_JOB_IDEM_PREFIX = 'idem_'


class _IdemMarkerUnreadable(Exception):
    """A key's marker is present but unreadable. Admission must FAIL
    CLOSED on this -- treating it as 'no prior job' would run the work a
    second time."""


class _IdemKeyConflict(Exception):
    """A key already names a job of a DIFFERENT kind than the caller is
    starting. Reusing one idempotency_key across operations is a caller
    error; refuse rather than silently reconcile (e.g. a save handed a
    test run's handle, its save never running)."""


def _idem_marker_path(idempotency_key):
    """Path of a key's marker, or None with no jobs dir / no key. The key
    is HASHED into the filename so arbitrary key text can neither escape
    the jobs dir nor collide with a job_<8hex> record.

    SCOPING: the key is used RAW here -- this node's job dir is its own
    namespace. When multi-convoy dispatch lands (A-21 signs convoy_id
    alongside idempotency_key), the CALLER must combine convoy_id /
    controller_id into the key before submitting, so two controllers
    picking the same string cannot cross-reconcile. The node does not
    namespace it, because in Phase 1 there is one local caller."""
    d = _jobs_dir()
    if not d or not idempotency_key or not isinstance(idempotency_key, str):
        return None
    import hashlib
    digest = hashlib.sha256(idempotency_key.encode('utf-8')).hexdigest()[:24]
    return os.path.join(d, _JOB_IDEM_PREFIX + digest + '.json')


def _record_job_key(idempotency_key, job_id):
    """Point a key's marker at the job it minted (atomic, best-effort,
    mirroring _write_job). Called AFTER the job record is durable, so a
    marker never names a job that was never persisted."""
    path = _idem_marker_path(idempotency_key)
    if not path:
        return
    tmp = path + '.%d.tmp' % os.getpid()
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump({'job_id': job_id, 'created': time.time()}, f)
        for attempt in range(3):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == 2:
                    raise
    except Exception:
        # Best-effort: a lost marker means at worst a future retry
        # re-mints -- never a duplicate of ACKNOWLEDGED work, since the
        # job record itself is already durable and its handle returned.
        # Clean the temp so a failed write leaves nothing behind.
        try:
            os.remove(tmp)
        except OSError:
            pass


def _job_for_key(idempotency_key, expected_kind=None):
    """The job RECORD a key already minted, or None on a miss.

    Raises _IdemMarkerUnreadable on a present-but-unreadable marker so the
    caller refuses rather than duplicating. Raises _IdemKeyConflict if the
    key names a job of a kind other than expected_kind -- a key reused
    across operations must not silently reconcile a save to a test run's
    handle. A marker naming a job whose record is gone (retention,
    cleanup) reads as a MISS, so the caller re-mints -- healing rather
    than refusing forever.
    """
    path = _idem_marker_path(idempotency_key)
    if not path:
        return None
    last = None
    for _attempt in range(4):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                text = f.read().strip()
        except FileNotFoundError:
            return None
        except OSError as e:            # locked / sharing violation: retry
            last = e
            continue
        if not text:
            return None                 # created but not yet filled
        try:
            data = json.loads(text)
        except ValueError:
            # os.replace is atomic, so a non-empty marker we wrote is whole
            # JSON. Unparseable-but-present is corruption -- fail closed.
            raise _IdemMarkerUnreadable(path)
        job_id = data.get('job_id') if isinstance(data, dict) else None
        if not job_id:
            return None
        # Honor the marker only if its job still exists (job_id is
        # re-validated by _job_path inside _read_job), else miss -> re-mint.
        record = _read_job(job_id)
        if record is None:
            return None
        if expected_kind is not None and record.get('kind') != expected_kind:
            raise _IdemKeyConflict(
                '%r names a %s job, not %s'
                % (idempotency_key, record.get('kind'), expected_kind))
        return record
    raise _IdemMarkerUnreadable('%s: %s' % (path, last))


def _prune_orphan_marker(path):
    """Drop a per-key marker once the job it names no longer exists, so a
    marker lives EXACTLY as long as its job record is fetchable: never
    shorter (a shorter life would re-mint a still-live job -- the bug a
    'created'-age horizon had, since a done record is retained from its
    FINISH while a marker's age counts from the job's START, and a
    running/indeterminate record is never pruned at all), never
    unboundedly longer. An unreadable/corrupt marker is LEFT untouched --
    it is a fail-closed signal an operator should see, not silently
    erase."""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception:
        return
    job_id = data.get('job_id') if isinstance(data, dict) else None
    if not job_id or _read_job(job_id) is None:
        try:
            os.remove(path)
        except OSError:
            pass


def _job_public(job, now):
    out = dict(job)
    try:
        out['age_s'] = round(now - float(job.get('started', now)), 1)
    except (TypeError, ValueError):
        out['age_s'] = None
    if (out.get('status') == 'running' and out['age_s'] is not None
            and out['age_s'] > _JOB_STALE_RUNNING_S):
        out['stale'] = True
        out['hint'] = ('running far longer than expected -- the completion '
                       'poll may have died in an extension reinit; check '
                       'the operation itself and dev/logs')
    return out


def _list_jobs(now):
    """Recent job records, newest first, pruning expired finished ones."""
    d = _jobs_dir()
    if not d or not os.path.isdir(d):
        return []
    records = []
    try:
        names = os.listdir(d)
    except Exception:
        return []
    for name in names:
        if not name.endswith('.json'):
            continue
        if name.startswith(_JOB_IDEM_PREFIX):
            # Per-key idempotency markers are not job records: never list
            # them, and prune ones whose job is gone so they stay bounded
            # (nothing else sweeps them -- _read_job rejects their ids).
            _prune_orphan_marker(os.path.join(d, name))
            continue
        record = _read_job(name[:-5])
        if record is None:
            continue
        if record.get('status') in ('done', 'error'):
            try:
                finished = float(record.get('finished', 0) or 0)
            except (TypeError, ValueError):
                finished = 0.0
            if (now - finished) > _JOB_RETENTION_S:
                try:
                    os.remove(os.path.join(d, name))
                except Exception:
                    pass
                continue
        records.append(_job_public(record, now))
    records.sort(key=lambda r: -(r.get('started') or 0))
    return records[:_JOB_LIST_CAP]


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
        # Every sub-operation, not the first 16. The scopes recorded here
        # are what PEERS see as your territory, and the destructive gate
        # evaluates the whole batch -- so a 16-item cap meant sub-ops
        # 17..N were gate-checked but left no trace for anyone else,
        # silently under-reporting where this session had been working.
        # The caller-visible list is deduped and capped downstream; the
        # cap belongs there, on presentation, not here on detection.
        for sub in (params.get('operations') or []):
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


# --- Worktree coordination helpers (Phase 2, 2026-07-25) -------------------
# Module-level and TD-free so unit tests can exercise them directly.

WORKTREE_SCOPE_PREFIX = 'project:worktree-'
DURABLE_CLAIM_MAX_AGE_S = 7 * 86400


def durable_claim_alive(claim: dict, now: float,
                        max_age_s: float = DURABLE_CLAIM_MAX_AGE_S) -> bool:
    """A durable worktree claim lives while its worktree DIRECTORY exists
    and it is younger than the max-age backstop. Session silence and TTL
    do not apply -- the claim marks an in-flight worktree task, which
    outlives the AI session that started it."""
    path = claim.get('path')
    if not path or not os.path.isdir(path):
        return False
    try:
        return (now - float(claim.get('ts', 0))) <= max_age_s
    except (TypeError, ValueError):
        return False


def compute_landing_conflicts(landing_files, main_dirty, peer_files,
                              tdn_unsaved) -> dict:
    """Intersect a worktree landing's file list with the three hazard sets.
    Pure function; all args are iterables of repo-relative POSIX paths."""
    landing = set(landing_files)
    return {
        'main_dirty': sorted(landing & set(main_dirty)),
        'peers': sorted(landing & set(peer_files)),
        'tdn_unsaved': sorted(landing & set(tdn_unsaved)),
    }


def read_tsv_dirty_paths(repo_root: str) -> set:
    """Repo-relative paths of externalized files whose live TDN/DAT state
    is UNSAVED (dirty column truthy in externalizations.tsv). LEGACY
    tsvs only since 2026-08-20 -- the live project's dirty state is
    runtime-only and merged from the sys mirror at the call site. Pure
    file read -- safe on the worker thread. Returns an empty set when
    the table cannot be found or parsed."""
    dirty = set()
    try:
        root = os.path.normpath(repo_root)
        candidates = []
        for base, dirs, files in os.walk(root):
            rel_depth = os.path.relpath(base, root).count(os.sep)
            if rel_depth >= 3:
                dirs[:] = []
                continue
            dirs[:] = [d for d in dirs
                       if d not in ('.git', 'node_modules', '.venv',
                                    'Backup', '__pycache__')]
            if 'externalizations.tsv' in files:
                candidates.append(os.path.join(base, 'externalizations.tsv'))
        for tsv in candidates:
            # rel_file_path values are relative to the tsv's parent's
            # parent (the project folder): embody/X.py under dev/ ->
            # repo-relative dev/embody/X.py.
            base_rel = os.path.relpath(
                os.path.dirname(os.path.dirname(tsv)), root).replace('\\', '/')
            prefix = '' if base_rel == '.' else base_rel + '/'
            with open(tsv, 'r', encoding='utf-8') as f:
                header = f.readline().rstrip('\n').split('\t')
                try:
                    i_rel = header.index('rel_file_path')
                    i_dirty = header.index('dirty')
                except ValueError:
                    continue
                for line in f:
                    cols = line.rstrip('\n').split('\t')
                    if len(cols) <= max(i_rel, i_dirty):
                        continue
                    if cols[i_dirty].strip() in ('True', 'true', '1'):
                        rel = cols[i_rel].strip().replace('\\', '/')
                        if rel:
                            dirty.add(prefix + rel)
    except Exception:
        return dirty
    return dirty


# --- Worker-thread run() lint (static, write-time) ---

# Calling TD's global run() from a worker thread does NOT raise on current
# builds -- it silently corrupts TD state and crashes later (Derivative-
# confirmed 2026-08-17). Nothing catches it at runtime, so the tool layer
# catches it at WRITE time, the same contract as LAYOUT WARNING: a warning
# rides back in _logs and the write still lands.

# Matched on the TRAILING callable name so threading.Thread(...), Thread(...),
# TDTask(...), ThreadManager.EnqueueTask(...) and executor.submit(...) all
# resolve without tracking imports or aliases.
_THREAD_CTORS = ('Thread', 'TDTask')
_THREAD_DISPATCHERS = ('EnqueueTask', 'submit')
_THREAD_TARGET_KWARGS = ('target', 'task')
# Subclassing threading.Thread (or TD's TDThread) and overriding run() is the
# other standard spawn idiom; .start() invokes that run() on the worker.
_THREAD_BASES = ('Thread', 'TDThread', 'TDTask', 'Timer')

# The lint runs on TD's main thread inside the per-frame refresh drain, so
# its cost is bounded up front: sources over the byte cap are not linted
# (ast.parse alone measures ~200ms at 500KB), sources without the substring
# 'run(' cannot produce a finding and skip the parse entirely, and each
# distinct (target, body) pair is scanned once no matter how many spawn
# sites hand it to a thread.
_WORKER_RUN_LINT_MAX_BYTES = 131072
_WORKER_RUN_LINT_MAX_TARGETS = 64


def _called_name(node) -> str:
    """Trailing callable name of a Call: 'Thread' for both Thread(...) and
    threading.Thread(...). None when the callee is neither a name nor an
    attribute (a subscript or an immediate call)."""
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _run_calls_in(node, run_shadowed: bool) -> list:
    """(label, lineno) for every worker-hostile run() call inside `node`:
    bare run(...) and td.run(...). subprocess.run and every other x.run are
    deliberately NOT matched -- only TD's global run() is the hazard. String
    and comment occurrences never reach here; ast only yields real calls."""
    out = []
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        func = child.func
        if (isinstance(func, ast.Name) and func.id == 'run'
                and not run_shadowed):
            out.append(('run()', child.lineno))
        elif (isinstance(func, ast.Attribute) and func.attr == 'run'
                and isinstance(func.value, ast.Name)
                and func.value.id == 'td'):
            out.append(('td.run()', child.lineno))
    return out


def _direct_callees(node, defs: dict) -> list:
    """Names called inside `node` that are defined in the SAME submitted
    source. One level of resolution: a helper the thread target calls runs
    on the worker thread too, so its run() is the target's run()."""
    names = []
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            name = _called_name(child)
            if name in defs and name not in names:
                names.append(name)
    return names


def _module_bound_names(tree) -> set:
    """Names bound at MODULE scope: top-level defs, classes, assignments and
    import aliases, descending through module-level control flow (if/try/
    for/while) but never into class or function bodies -- a method or a
    local variable does not rebind the module-scope name a bare call
    resolves to."""
    bound = set()
    stack = list(tree.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            bound.add(node.name)
            continue                     # do not descend into the body
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                bound.add((alias.asname or alias.name).split('.')[0])
            continue
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            bound.add(node.id)
        stack.extend(ast.iter_child_nodes(node))
    return bound


def _worker_run_findings(source) -> list:
    """Findings for code a thread will execute -- a function handed to a
    thread as a TARGET (or a helper it calls in this same source), and the
    run() method of a threading.Thread subclass -- that calls TD's global
    run().

    Pure: source string in, list of {function, call, line, via} out, so it
    is testable without TouchDesigner. Unparseable source lints to NOTHING:
    submitted code may be a fragment, and a lint must never be the thing
    that fails a write. Oversized source and source with no 'run(' at all
    lint to nothing before the parse -- the byte cap keeps the main thread
    inside its frame budget.
    """
    if not isinstance(source, str) or not source:
        return []
    if len(source) > _WORKER_RUN_LINT_MAX_BYTES or 'run(' not in source:
        return []
    try:
        tree = ast.parse(source)
    except Exception:
        return []

    # Every def in the source (nested and local defs included -- a locally
    # defined worker is the common shape).
    defs = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defs.setdefault(node.name, node)

    # Bare run() is suppressed only when MODULE scope rebinds `run` -- that
    # is what a bare call inside a function resolves to. A method named run
    # (every threading.Thread subclass has one) or a local variable must NOT
    # disable the lint: a whole-tree scan goes silent on exactly the
    # threading-heavy sources the lint exists for. td.run() stays
    # unambiguous either way.
    run_shadowed = 'run' in _module_bound_names(tree)

    # (reported name, node to scan) -- callables handed to a thread ctor or
    # dispatcher, plus run() overrides on thread-subclass bodies.
    resolved = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = _called_name(node)
            if name not in _THREAD_CTORS and name not in _THREAD_DISPATCHERS:
                continue
            expr = None
            for kw in node.keywords:
                if kw.arg in _THREAD_TARGET_KWARGS:
                    expr = kw.value
                    break
            # EnqueueTask(task, *args) / executor.submit(fn, *args) take the
            # callable positionally; Thread's first positional is `group`,
            # so positional targets are read for dispatchers only.
            if expr is None and name in _THREAD_DISPATCHERS and node.args:
                expr = node.args[0]
            if expr is None:
                continue
            if isinstance(expr, ast.Lambda):
                resolved.append(('<lambda>', expr))
            elif isinstance(expr, (ast.Name, ast.Attribute)):
                key = (expr.id if isinstance(expr, ast.Name)
                       else expr.attr)   # self.worker / mod.worker by name
                body = defs.get(key)
                if body is not None:
                    resolved.append((key, body))
        elif isinstance(node, ast.ClassDef):
            bases = {b.id if isinstance(b, ast.Name) else b.attr
                     for b in node.bases
                     if isinstance(b, (ast.Name, ast.Attribute))}
            if not bases.intersection(_THREAD_BASES):
                continue
            for stmt in node.body:
                if (isinstance(stmt, (ast.FunctionDef,
                                      ast.AsyncFunctionDef))
                        and stmt.name == 'run'):
                    resolved.append((node.name + '.run', stmt))

    # Scan each distinct (label, body) once -- N spawn sites of one worker
    # are one scan, which is what bounds the repeated-target worst case --
    # and cap the total in case a pathological source defeats the dedupe.
    scan = []
    scanned = set()
    for label, body in resolved:
        key = (label, id(body))
        if key in scanned:
            continue
        scanned.add(key)
        scan.append((label, body, None))
        for callee in _direct_callees(body, defs):
            if defs[callee] is not body:
                scan.append((label, defs[callee], callee))
        if len(scan) >= _WORKER_RUN_LINT_MAX_TARGETS:
            break

    findings = []
    seen = set()
    for label, node, via in scan:
        for call_label, lineno in _run_calls_in(node, run_shadowed):
            key = (label, call_label, lineno, via)
            if key in seen:
                continue
            seen.add(key)
            findings.append({'function': label, 'call': call_label,
                             'line': lineno, 'via': via})
    findings.sort(key=lambda f: (f['line'], f['function']))
    return findings


# Worker threads must not print(): TD replaces sys.stdout with a Textport
# catcher, a main-thread object, so a worker print is the same defect class
# as worker-side run() (Derivative-confirmed 2026-08-17). Workers buffer
# diagnostics here instead; _onRefresh drains them through the normal
# logger on the main thread. Bounded so a chatty worker cannot grow it.
_WORKER_LOG_LINES = deque(maxlen=64)


def _queueWorkerLog(message, level='WARNING'):
    """Buffer one worker-side log line for main-thread delivery."""
    try:
        _WORKER_LOG_LINES.append((level, str(message)))
    except Exception:
        pass


class EnvoyMCPServer:
    """
    MCP Server that runs in a worker thread.

    IMPORTANT: This class must NOT import or use any TouchDesigner modules.
    All TD operations are delegated to the main thread via queues.
    """

    def __init__(self, request_queue: Optional[Queue], response_queue: Queue,
                 add_to_refresh_queue: Callable[[dict], None], port: int = 9870,
                 shutdown_event: Optional[Event] = None,
                 startup_event: Optional[Event] = None,
                 gen: int = 0) -> None:
        self.request_queue: Optional[Queue] = request_queue
        self.response_queue: Queue = response_queue
        self.add_to_refresh_queue: Callable[[dict], None] = add_to_refresh_queue
        self.port: int = port
        self.shutdown_event: Event = shutdown_event or Event()
        # Server generation this worker belongs to. Tagged onto
        # sys._envoy_uvi_gen so _forceCloseOldServer can distinguish a STALE
        # server handle (safe to force-close) from the CURRENT generation's
        # just-started server (closing that one murders a healthy worker --
        # one arm of the 2026-07-15 restart storm, issue #57 follow-up).
        self.gen: int = gen
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
        # Durable worktree claims persist across sessions AND Envoy restarts
        # (.embody/worktree-claims.json): an in-flight worktree task's marker
        # must outlive the AI session that started it.
        self._loadDurableWorktreeClaims()
        # Task ledger: shared work-state (.embody/tasks.json). Loaded lazily
        # inside each ledger operation -- the path needs sys._envoy_repo_root,
        # which the main thread may not have resolved yet at construction.
        self._tasks: dict = {}

        self._docs_state = {'resolved': False, 'root': None, 'index': None, 'cache': {},
                            'build': None, 'defaults': None}
        # get_guidance index: the project's own .claude rules/skills, scanned
        # from disk on the worker thread. {'ts': float, 'root': str,
        # 'index': {topic_id: entry}, 'searched': [dirs]}; rebuilt when older
        # than _GUIDANCE_INDEX_TTL_S so edits to a rule show up mid-session.
        self._guidance_state = {'ts': 0.0, 'root': None, 'index': None,
                                'searched': []}

        # Import mcp only when server is instantiated (in worker thread).
        # SDK 2.0 renamed FastMCP -> MCPServer (mcp.server.mcpserver); the
        # tool-decorator API is unchanged and Image kept its data=/format=
        # signature. Transport settings moved off the constructor onto
        # streamable_http_app() -- applied in run(). port was always bound
        # by our own uvicorn.Config, never by the SDK.
        # BEHAVIOR CHANGE from 1.x: sync tool bodies now run on anyio worker
        # threads and execute CONCURRENTLY (1.x ran them inline on the event
        # loop, serialized). _execute_in_td is safe (per-request Event under
        # self.lock, and the TD main thread drains the queue serially), but
        # any NEW tool-body state must be request-local or lock-guarded.
        from mcp.server.mcpserver import MCPServer, Image
        self._Image = Image  # Store for use in tool functions
        # The MCP SDK auto-enables this for host="127.0.0.1", but pin it
        # explicitly so a default change cannot silently drop the Host/Origin
        # validation that defeats DNS rebinding/CSRF from a local browser.
        # Idea prompted by TDMCP's 1.1.46 security work.
        self._transport_security = None
        try:
            from mcp.server.transport_security import TransportSecuritySettings
            self._transport_security = TransportSecuritySettings(
                enable_dns_rebinding_protection=True,
                allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*"],
                allowed_origins=[
                    "http://127.0.0.1:*",
                    "http://localhost:*",
                    "http://[::1]:*",
                ],
            )
        except Exception as e:
            _queueWorkerLog(f'Transport security settings unavailable; '
                            f'continuing without explicit MCPServer transport_security: {e}')
        # version: 2.0 reports serverInfo.version as "" unless told (1.x
        # substituted the SDK's own version -- neither is Envoy's).
        # MCPServer.__init__ calls logging.basicConfig(level=INFO): inside
        # TouchDesigner that installs a root stderr handler and drops the
        # root level process-wide, turning EVERY info-level logger in the
        # process into textport output (verified against 2.0.0: handlers
        # 0->1, root level 30->20). Snapshot and undo -- Envoy tunes its own
        # loggers in run().
        import logging as _logging
        _root = _logging.getLogger()
        _pre_handlers = list(_root.handlers)
        _pre_level = _root.level
        self.mcp = MCPServer("Envoy", version=ENVOY_VERSION)
        for _h in list(_root.handlers):
            if _h not in _pre_handlers:
                _root.removeHandler(_h)
        _root.setLevel(_pre_level)
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
        snapshot = {'sessions': sessions, 'count': len(sessions)}
        # In-flight worktree tasks (durable claims): visible to every
        # session even after the starting session is gone.
        with self._sessions_lock:
            worktrees = [
                {'scope': s, 'path': c.get('path', ''),
                 'label': c.get('label', ''), 'note': c.get('note', ''),
                 'age_h': round((now - c['ts']) / 3600.0, 1),
                 'holder_sid': c['sid']}
                for s, c in self._claims.items() if c.get('durable')]
        if worktrees:
            snapshot['worktrees'] = worktrees
        return snapshot

    def _prune_claims_locked(self, now):
        """Drop expired claims and claims whose holder went silent for 10
        minutes. Caller holds _sessions_lock."""
        durable_changed = False
        for held_scope in list(self._claims):
            claim = self._claims[held_scope]
            if claim.get('durable'):
                # Durable worktree claims ignore TTL and holder silence --
                # they expire when the worktree DIRECTORY disappears or on
                # the max-age backstop (durable_claim_alive).
                if not durable_claim_alive(claim, now):
                    del self._claims[held_scope]
                    durable_changed = True
                continue
            holder = self._sessions.get(claim['sid'])
            holder_seen = holder.get('last_seen', 0) if holder else 0
            # '_anon' holders (headerless clients) are never in the
            # registry -- for them only the TTL applies, or their claims
            # would evaporate on the next prune.
            holder_silent = (claim['sid'] != '_anon'
                             and now - holder_seen > 600)
            if now > claim['ts'] + claim['ttl'] or holder_silent:
                del self._claims[held_scope]
        if durable_changed:
            self._persistDurableClaimsLocked()

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
            durable = scope.startswith(WORKTREE_SCOPE_PREFIX)
            if durable:
                # Worktree claims are DURABLE: they mark an in-flight
                # worktree task and survive session death and Envoy
                # restarts. Expiry = worktree dir gone or 7-day backstop.
                self._claims[scope]['durable'] = True
                self._claims[scope]['path'] = (
                    self._worktreePathForScope(scope) or '')
                self._persistDurableClaimsLocked()
            if len(self._claims) > 64:
                oldest_first = sorted(self._claims.items(),
                                      key=lambda kv: kv[1]['ts'])
                for stale_scope, _claim in oldest_first[:len(self._claims) - 64]:
                    del self._claims[stale_scope]
        if durable:
            return {'granted': True, 'scope': scope, 'durable': True,
                    'renewal': 'durable worktree claim: survives session '
                               'silence and Envoy restarts; expires when the '
                               'worktree directory is removed or after 7 '
                               'days; release_scope when the diff lands'}
        return {'granted': True, 'scope': scope, 'ttl': ttl,
                'renewal': 'your own tool calls touching this scope renew '
                           'the lease; it expires on TTL or session silence'}

    def _durableClaimsPath(self):
        """Path of .embody/worktree-claims.json, or None before the main
        thread has cached the repo root. Worker-side pure Python."""
        root = getattr(sys, '_envoy_repo_root', None)
        if not root:
            return None
        return os.path.join(root, '.embody', 'worktree-claims.json')

    def _worktreePathForScope(self, scope):
        """Derive the expected worktree directory for a
        project:worktree-<task> scope: sibling '<repo>-wt-<task>'."""
        root = getattr(sys, '_envoy_repo_root', None)
        if not root or not scope.startswith(WORKTREE_SCOPE_PREFIX):
            return None
        task = scope[len(WORKTREE_SCOPE_PREFIX):]
        root = os.path.normpath(root)
        return os.path.join(os.path.dirname(root),
                            os.path.basename(root) + '-wt-' + task)

    def _persistDurableClaimsLocked(self):
        """Write durable worktree claims to disk (atomic, best-effort).
        Caller holds _sessions_lock. Worker-side pure Python."""
        path = self._durableClaimsPath()
        if not path:
            return
        try:
            data = {}
            for s, c in self._claims.items():
                if c.get('durable'):
                    data[s] = {'sid': c.get('sid', '_anon'),
                               'label': c.get('label', ''),
                               'note': c.get('note', ''),
                               'ts': c.get('ts', 0),
                               'path': c.get('path', '')}
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + '.%d.tmp' % os.getpid()
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=1)
            os.replace(tmp, path)
        except Exception:
            pass

    def _loadDurableWorktreeClaims(self):
        """Load persisted durable claims at server start, dropping entries
        whose worktree is gone or that exceed the max-age backstop."""
        path = self._durableClaimsPath()
        if not path or not os.path.isfile(path):
            return
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except Exception:
            return
        if not isinstance(data, dict):
            return
        now = time.time()
        with self._sessions_lock:
            for scope, entry in data.items():
                if (not isinstance(entry, dict)
                        or not scope.startswith(WORKTREE_SCOPE_PREFIX)
                        or scope in self._claims):
                    continue
                claim = {'sid': entry.get('sid', '_anon'),
                         'label': entry.get('label', ''),
                         'note': entry.get('note', ''),
                         'ts': float(entry.get('ts', now) or now),
                         'ttl': DURABLE_CLAIM_MAX_AGE_S,
                         'durable': True,
                         'path': (entry.get('path')
                                  or self._worktreePathForScope(scope) or '')}
                if durable_claim_alive(claim, now):
                    self._claims[scope] = claim

    # --- Task ledger (shared work-state) ----------------------------------
    # Worker-side pure Python + file I/O under _sessions_lock -- ZERO TD
    # access (mcp-safety). Same persistence idiom as the durable worktree
    # claims above. Every operation re-reads the file first: a second TD
    # process (another instance, the fresh-install smoke) may share the
    # repo root, and disk is the source of truth between processes.

    def _taskLedgerPath(self):
        """Path of .embody/tasks.json, or None before the main thread has
        cached the repo root."""
        root = getattr(sys, '_envoy_repo_root', None)
        if not root:
            return None
        return os.path.join(root, '.embody', 'tasks.json')

    def _loadTasksLocked(self) -> dict:
        """Freshest ledger: disk merged over memory, then pruned. Caller
        holds _sessions_lock. Disk wins per task id; tasks that only exist
        in memory (a persist failed earlier) survive the merge."""
        path = self._taskLedgerPath()
        from_disk = {}
        if path and os.path.isfile(path):
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    raw = json.load(f)
                if isinstance(raw, dict) and isinstance(raw.get('tasks'), dict):
                    from_disk = raw['tasks']
            except Exception:
                from_disk = {}
        # Per-id NEWEST-updated wins. A blanket disk-wins here rolled a
        # process's own fresh transition back whenever another process
        # persisted an older view of the file (review finding: the exact
        # done_uncommitted->in_progress reversion this feature exists to
        # prevent). Ties go to disk -- identical stamps mean the same write
        # round-tripped.
        merged = dict(self._tasks)
        for tid, task in from_disk.items():
            if not isinstance(task, dict):
                continue
            mine = merged.get(tid)
            if mine is None or _task_updated(task) >= _task_updated(mine):
                merged[tid] = task
        self._tasks = _prune_tasks(merged, time.time())
        return self._tasks

    def _persistTasksLocked(self):
        """Atomic best-effort write. Caller holds _sessions_lock. A failed
        write leaves memory serving this process; the next operation
        retries."""
        path = self._taskLedgerPath()
        if not path:
            return
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + '.%d.tmp' % os.getpid()
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump({'schema': 1, 'tasks': self._tasks}, f, indent=1)
            os.replace(tmp, path)
        except Exception:
            pass

    def _announce_task(self, sid, label, title, scopes=None, note='') -> dict:
        title = (title or '').strip()[:_TASK_TITLE_MAX]
        if not title:
            return {'error': 'title is required'}
        clean_scopes = []
        for scope in (scopes or [])[:_TASK_SCOPES_MAX]:
            scope = str(scope).strip()
            if not scope:
                continue
            # Same posture as claim_scope: reject malformed scopes loudly.
            # An unprefixed file path would silently never match preflight's
            # file: filter -- exactly where the warning was wanted.
            if not (scope.startswith('/') or scope.startswith('file:')
                    or scope.startswith('project:')):
                return {'error': "scope %r must be an op path ('/...'), "
                                 "'file:<repo-relative-path>', or "
                                 "'project:<name>'" % scope}
            if scope.startswith('file:'):
                scope = 'file:' + scope[5:].replace('\\', '/')
            clean_scopes.append(scope)
        import uuid
        now = time.time()
        task = {
            'id': 'tsk_' + uuid.uuid4().hex[:8],
            'title': title,
            'scopes': clean_scopes,
            'status': 'in_progress',
            'session': sid or '_anon',
            'label': label or '',
            'note': (note or '').strip()[:_TASK_NOTE_MAX],
            'created': now,
            'updated': now,
        }
        with self._sessions_lock:
            self._loadTasksLocked()
            self._tasks[task['id']] = task
            self._persistTasksLocked()
            # Render inside the lock: task dicts are mutated in place by
            # concurrent updates, and references must not escape.
            return {'announced': True, 'task': _task_public(task, now)}

    def _update_task(self, sid, label, task_id, status=None, note=None,
                     commit=None) -> dict:
        task_id = (task_id or '').strip()
        now = time.time()
        with self._sessions_lock:
            self._loadTasksLocked()
            task = self._tasks.get(task_id)
            if task is None:
                active = [_task_public(t, now) for t in self._tasks.values()
                          if t.get('status') in _TASK_ACTIVE_STATUSES]
                return {'error': 'no task with id %r' % task_id,
                        'active_tasks': active[:_TASK_LIST_CAP]}
            if status is not None:
                if status not in _TASK_STATUSES:
                    return {'error': 'status must be one of %s'
                                     % (_TASK_STATUSES,)}
                task['status'] = status
            if note is not None:
                task['note'] = str(note).strip()[:_TASK_NOTE_MAX]
            if commit is not None:
                sha = str(commit).strip()[:64]
                if sha:
                    task['commit'] = sha
                    if status is None and task.get('status') != 'committed':
                        # Recording a real sha IS the committed transition
                        # unless the caller said otherwise. An empty commit
                        # is a no-op, never an implied transition.
                        task['status'] = 'committed'
            if sid and sid != task.get('session'):
                # Peer transition (e.g. marking a dead session's task
                # abandoned) -- cooperative, but keep the trace.
                task['updated_by'] = sid
            elif sid:
                # The owner's own update clears a stale peer attribution so
                # updated_by always describes the LATEST write.
                task.pop('updated_by', None)
            task['updated'] = now
            self._persistTasksLocked()
            return {'updated': True, 'task': _task_public(task, now)}

    def _tasks_snapshot(self, include_terminal=False) -> list:
        """Public task list, newest-updated first, capped. Worker-side.
        Filtered, sorted AND rendered inside the lock -- task dicts are
        mutated in place by concurrent updates, so references must not
        escape; and the sort key routes through _task_updated so one
        malformed entry from a foreign writer cannot take the whole
        surface down."""
        now = time.time()
        with self._sessions_lock:
            self._loadTasksLocked()
            wanted = [t for t in self._tasks.values()
                      if include_terminal
                      or t.get('status') in _TASK_ACTIVE_STATUSES]
            wanted.sort(key=lambda t: -_task_updated(t))
            return [_task_public(t, now) for t in wanted[:_TASK_LIST_CAP]]

    def _ledger_tasks_for_files(self, landing_files) -> list:
        """Active ledger tasks whose file: scopes intersect a set of
        repo-relative paths (forward slashes). Rendered inside the lock;
        each hit carries 'overlap' = the matching path. Only file: scopes
        can be matched worker-side (op paths would need TD to resolve to
        files) -- callers must document that limit."""
        landing_set = {str(p).replace('\\', '/') for p in landing_files or ()}
        if not landing_set:
            return []
        now = time.time()
        hits = []
        with self._sessions_lock:
            self._loadTasksLocked()
            for task in self._tasks.values():
                if task.get('status') not in _TASK_ACTIVE_STATUSES:
                    continue
                overlap = None
                for scope in (task.get('scopes') or []):
                    if isinstance(scope, str) and scope.startswith('file:'):
                        rel = scope[5:].replace('\\', '/')
                        if rel in landing_set:
                            overlap = rel
                            break
                if overlap:
                    entry = _task_public(task, now)
                    entry['overlap'] = overlap
                    hits.append(entry)
        return hits

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
            if claim['sid'] != me and not claim.get('durable'):
                return {'released': False,
                        'reason': 'held by another session',
                        'holder': claim.get('label') or claim['sid']}
            # Durable worktree claims may be released by ANY session (the
            # landing session is often not the session that started the
            # task) -- the worktree dir check backstops mistakes.
            was_durable = bool(claim.get('durable'))
            del self._claims[scope]
            if was_durable:
                self._persistDurableClaimsLocked()
        return {'released': True, 'scope': scope}

    def _preflight_landing(self, worktree_path, caller_sid=None) -> dict:
        """Worker-side landing preflight: pure Python + git subprocess,
        ZERO TD-object access (mcp-safety). Git calls are bounded (15s)."""
        import subprocess

        root = getattr(sys, '_envoy_repo_root', None)
        if not root:
            return {'error': 'repo root not resolved yet -- Envoy is still '
                             'starting; retry in a few seconds'}
        root = os.path.normpath(root)
        wt = (worktree_path or '').strip()
        if not wt:
            return {'error': 'worktree_path is required'}
        if not os.path.isabs(wt):
            wt = os.path.normpath(os.path.join(root, wt))
        if not os.path.isdir(wt) or not os.path.exists(
                os.path.join(wt, '.git')):
            return {'error': 'not a git worktree/checkout: %s' % wt}

        def _git_lines(cwd, *args):
            # stdin=DEVNULL: TD's GUI stdin handle is not duplicatable, so
            # without it subprocess.run raises [WinError 50] inside TD.
            # creationflags: no console flash over TD's GUI (see embody_git).
            r = subprocess.run(['git', '-C', cwd] + list(args),
                               capture_output=True, text=True, timeout=15,
                               encoding='utf-8', errors='replace',
                               stdin=subprocess.DEVNULL,
                               creationflags=getattr(
                                   subprocess, 'CREATE_NO_WINDOW', 0))
            if r.returncode != 0:
                raise RuntimeError(
                    (r.stderr or r.stdout).strip()[:300] or 'git failed')
            return [ln for ln in r.stdout.splitlines() if ln.strip()]

        def _porcelain_paths(lines):
            paths = set()
            for ln in lines:
                p = ln[3:].strip()
                if ' -> ' in p:  # rename: "R  old -> new" -- take both
                    old, new = p.split(' -> ', 1)
                    paths.add(old.strip().strip('"'))
                    p = new
                paths.add(p.strip().strip('"'))
            return {p.replace('\\', '/') for p in paths if p}

        try:
            landing = sorted(
                _porcelain_paths(_git_lines(wt, 'status', '--porcelain'))
                | {p.strip().replace('\\', '/')
                   for p in _git_lines(wt, 'diff', '--name-only', 'HEAD')})
            main_dirty = _porcelain_paths(
                _git_lines(root, 'status', '--porcelain'))
        except Exception as e:
            return {'error': 'git preflight failed: %s' % e}

        tdn_unsaved = read_tsv_dirty_paths(root)
        # Dirty is runtime-only since 2026-08-20 (the tsv column is blank
        # by contract): merge the live mirror EmbodyExt._setDirtyState
        # maintains in a sys slot -- worker-safe, no TD objects. The file
        # scan above stays for foreign/legacy tsvs in the tree.
        try:
            tdn_unsaved |= set(
                dict(getattr(sys, '_embody_dirty_files', {}) or {})
                .values())
        except Exception:
            pass

        # Peer file territory: file: claims + recent file: write touches
        # from OTHER sessions.
        peer_files = set()
        with self._sessions_lock:
            for s, c in self._claims.items():
                if s.startswith('file:') and c.get('sid') != caller_sid:
                    peer_files.add(s[5:].replace('\\', '/'))
            for scope, ring in self._touches.items():
                if not scope.startswith('file:'):
                    continue
                for t in ring:
                    if t.get('sid') != caller_sid:
                        peer_files.add(scope[5:].replace('\\', '/'))
                        break

        collisions = compute_landing_conflicts(
            landing, main_dirty, peer_files, tdn_unsaved)
        has_conflicts = any(collisions.values())
        result = {
            'worktree': wt,
            'landing_files': landing,
            'collisions': collisions,
            'verdict': 'conflicts' if has_conflicts else 'clear',
        }
        if has_conflicts:
            result['hint'] = (
                'Reconcile before landing: rebase the worktree on the '
                'main tree for main_dirty collisions, coordinate with the '
                'listed peers, and save the project (or re-export) for '
                'tdn_unsaved collisions. Never overwrite blind.')

        # Shared-ledger context: ACTIVE tasks whose file: scopes intersect
        # the landing set. Report-only in this iteration (the verdict stays
        # git-truth-driven), but a done_uncommitted overlap is exactly the
        # "finished work sitting in the tree" that must be committed or
        # coordinated before this landing goes over it.
        try:
            ledger_tasks = self._ledger_tasks_for_files(landing)
            if ledger_tasks:
                result['ledger_tasks'] = ledger_tasks
                if any(t.get('status') == 'done_uncommitted'
                       for t in ledger_tasks):
                    result['ledger_hint'] = (
                        'A done_uncommitted ledger task overlaps this '
                        'landing: finished work is sitting uncommitted on '
                        'those files. Commit it (or coordinate with its '
                        'session) BEFORE landing over it.')
        except Exception:
            pass
        return result

    # --- get_guidance: serve the project's own rules/skills over MCP ------
    # Worker-side pure Python + filesystem, ZERO TD access (mcp-safety).

    def _guidanceSearchDirs(self) -> list:
        """Candidate `.claude` directories, most specific first.

        The repo root Envoy already resolved on the main thread
        (sys._envoy_repo_root) is the primary location -- it is where
        embody_git.write_claude_rules_and_skills deploys rules and skills in
        BOTH this repo and a user project. Ancestors and the process cwd are
        defensive fallbacks for unusual layouts (a project folder nested
        under the repo that actually owns .claude).
        """
        dirs, seen = [], set()

        def add(base):
            if not base:
                return
            try:
                candidate = os.path.join(os.path.normpath(str(base)), '.claude')
            except Exception:
                return
            if candidate not in seen:
                seen.add(candidate)
                dirs.append(candidate)

        root = getattr(sys, '_envoy_repo_root', None)
        add(root)
        if root:
            try:
                current = os.path.normpath(str(root))
                for _ in range(3):
                    parent = os.path.dirname(current)
                    if not parent or parent == current:
                        break
                    add(parent)
                    current = parent
            except Exception:
                pass
        try:
            add(os.getcwd())
        except Exception:
            pass
        return dirs

    @staticmethod
    def _guidanceScanDir(claude_dir: str) -> dict:
        """Index one `.claude` directory: {topic_id: entry}. Never raises."""
        index = {}

        def register(topic_id, kind, path):
            try:
                with open(path, 'r', encoding='utf-8', errors='replace') as f:
                    text = f.read()
            except Exception:
                return
            if not text.strip():
                return
            unique = topic_id
            if unique in index:
                # Same slug from both a rule and a skill: keep both, and
                # disambiguate the later one rather than dropping doctrine.
                unique = '%s-%s' % (topic_id, kind)
                if unique in index:
                    return
            index[unique] = {
                'topic': unique,
                'kind': kind,
                'path': path.replace('\\', '/'),
                'description': _guidance_description(text),
            }

        rules_dir = os.path.join(claude_dir, 'rules')
        try:
            for filename in sorted(os.listdir(rules_dir)):
                if not filename.lower().endswith('.md'):
                    continue
                register(os.path.splitext(filename)[0], 'rule',
                         os.path.join(rules_dir, filename))
        except Exception:
            pass

        skills_dir = os.path.join(claude_dir, 'skills')
        try:
            for slug in sorted(os.listdir(skills_dir)):
                skill_file = os.path.join(skills_dir, slug, 'SKILL.md')
                if os.path.isfile(skill_file):
                    register(slug, 'skill', skill_file)
        except Exception:
            pass
        return index

    def _guidanceIndex(self) -> tuple:
        """(index, root_dir, searched_dirs) for the first `.claude` directory
        that actually holds documents. Cached for _GUIDANCE_INDEX_TTL_S so a
        rule edited on disk is picked up mid-session."""
        state = self._guidance_state
        now = time.time()
        if (state.get('index') is not None
                and (now - state.get('ts', 0.0)) < _GUIDANCE_INDEX_TTL_S):
            return state['index'], state.get('root'), state.get('searched', [])
        searched = self._guidanceSearchDirs()
        index, root = {}, None
        for claude_dir in searched:
            found = self._guidanceScanDir(claude_dir)
            if found:
                index, root = found, claude_dir
                break
        state['index'] = index
        state['root'] = root
        state['searched'] = searched
        state['ts'] = now
        return index, root, searched

    def _get_guidance(self, topic=None) -> dict:
        """Body of the get_guidance tool. Worker-side; never raises."""
        try:
            index, root, searched = self._guidanceIndex()
            if not index:
                return {
                    'error': 'No Embody guidance documents found on disk '
                             '(.claude/rules/*.md, .claude/skills/*/SKILL.md).',
                    'searched': searched,
                    'action': 'Ask the user to run op.Embody.InitEnvoy() in '
                              'TouchDesigner -- it regenerates the AI client '
                              'config including .claude/rules and '
                              '.claude/skills. Until then, use get_docs for '
                              'official TouchDesigner documentation.',
                }
            topics = [{'topic': e['topic'], 'kind': e['kind'],
                       'description': e['description']}
                      for e in sorted(index.values(),
                                      key=lambda e: (e['kind'], e['topic']))]
            listing = {
                'topics': topics,
                'count': len(topics),
                'source': (root or '').replace('\\', '/'),
                'usage': "Call get_guidance(topic='create-operator') to read "
                         "one document in full. Rules are always-on project "
                         "policy; skills are workflows to read BEFORE the "
                         "matching action (creating operators, writing TD "
                         "Python, annotations, externalizing, releases).",
            }
            query = (topic or '').strip()
            if not query:
                return listing

            matched, suggestions = _match_guidance_topic(query, list(index))
            if matched is None:
                miss = dict(listing)
                miss['error'] = 'No guidance topic matches %r' % query
                if suggestions:
                    miss['did_you_mean'] = suggestions
                return miss

            entry = index[matched]
            try:
                with open(entry['path'], 'r', encoding='utf-8',
                          errors='replace') as f:
                    content = f.read()
            except Exception as e:
                return {'error': 'Could not read guidance document %r: %s'
                                 % (matched, e)}
            truncated = len(content) > _GUIDANCE_MAX_CHARS
            if truncated:
                content = content[:_GUIDANCE_MAX_CHARS]
            result = {
                'topic': entry['topic'],
                'kind': entry['kind'],
                'description': entry['description'],
                'path': entry['path'],
                'content': content,
            }
            if truncated:
                result['truncated'] = True
                result['note'] = ('Content truncated at %d characters -- read '
                                  'the file at the reported path for the rest.'
                                  % _GUIDANCE_MAX_CHARS)
            return result
        except Exception as e:
            return {'error': 'Guidance lookup failed: %s' % e}

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
                _queueWorkerLog(f'Orphaned response for request {request_id} '
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
                    _queueWorkerLog(f'check_responses unexpected error: {type(e).__name__}: {e}')
                break
            process_response(response)

    def _register_tools(self):
        """Register all MCP tools"""

        @self.mcp.tool()
        def create_op(parent_path: str, op_type: str, name: str = None) -> dict:
            """
            Create a new operator in TouchDesigner.

            Prerequisite: load the /create-operator skill before first use in
            a session -- group placement, wiring direction, and the mandatory
            verify pass live there.

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
                Dict with type, family, parameters, inputs, outputs,
                children. 'inputs' has ONE ENTRY PER INPUT CONNECTOR
                (null = that connector is empty), so a wire on input 2
                with inputs 0-1 unwired reads [null, null, path] --
                entry position IS the real connector index. Dynamic
                multi-input ops (Switch, Composite) always show one
                trailing null (their growth connector). 'outputs' is
                the compacted connected-outputs list, not
                per-connector.
            """
            return self._execute_in_td('get_op', {
                'op_path': op_path,
                'include_defaults': include_defaults,
            })

        @self.mcp.tool()
        def set_parameter(op_path: str, par_name: str, value: str = None,
                         mode: Optional[Literal['constant', 'expression',
                                                'export', 'bind']] = None,
                         expr: str = None,
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
                         search: str = None,
                         search_in: Literal['name', 'value',
                                            'expr', 'any'] = 'any',
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

            Prerequisite: load the /create-operator skill before first use in
            a session (same placement + verify workflow as create_op).

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
                Dict with inputs and outputs lists. 'inputs' has ONE
                ENTRY PER INPUT CONNECTOR: {'index': i, 'connected_to':
                path-or-null}, so index is the REAL connector index and
                a sparse wire (input 2 wired, 0-1 empty) reads exactly
                that -- never compacted. Dynamic multi-input ops show
                one trailing empty connector. 'outputs' entries carry a
                LIST per connector (outputs fan out).
            """
            return self._execute_in_td('get_connections', {'op_path': op_path})

        @self.mcp.tool()
        def execute_python(code: str) -> dict:
            """
            Execute arbitrary Python code in TouchDesigner.
            Use with caution - code runs on main thread with full TD access.

            Prerequisite: load the /td-api-reference skill before writing TD
            Python. Ops created here bypass auto-layout: position them per the
            network-layout rule or a LAYOUT WARNING rides back in _logs.

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
        def get_focus() -> dict:
            """Report what the USER is currently looking at in TouchDesigner.

            Call this to resolve conversational references -- "fix this
            operator", "add a blur here", "what's wrong with that node" --
            before guessing a path or calling query_network.

            DISAMBIGUATION RULE: "this operator" / "that node" means the
            SELECTED (or current) operator. It does NOT mean the rollover
            operator -- rollover is just whatever the mouse happens to be
            hovering over and is incidental. Use `target` when it is set;
            fall back to `selected`/`current`. Only mention `rollover` if
            the user explicitly talks about hovering.

            Returns:
                Dict with network (path of the network the current pane is
                showing), paneType, selected (list of selected operator
                paths), selectedCount, current (the current operator, or
                null), rollover (operator under the mouse, or null), target
                (the operator "this" resolves to, or null when ambiguous),
                targetSource ('selection'|'current'), and note. A
                headless/Engine TouchDesigner has no panes: the result then
                carries headless=true with everything null -- ask the user
                for an explicit path instead.
            """
            return self._execute_in_td('get_focus', {})

        @self.mcp.tool()
        def get_op_errors(op_path: str, recurse: bool = True) -> dict:
            """
            Get error and warning messages for an operator and optionally its children.
            Useful for debugging TD networks -- covers all three surfaces TD
            reports red: cook errors, Python tracebacks from callbacks/DAT
            scripts/expressions (OP.scriptErrors, tagged kind='script' in
            errors[]), and GLSL compile failures (shaderErrors key).

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
        def get_docs(query: str, section: str = None,
                     source: Literal['auto', 'offline', 'web'] = 'auto',
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

        @self.mcp.tool()
        def get_guidance(topic: str = None) -> dict:
            """Read this project's TouchDesigner rules and workflow skills.

            These are the project's own doctrine -- the same documents
            Claude Code loads from .claude/rules and .claude/skills. Every
            other client (Codex, Cursor, opencode, ...) only sees them
            through this tool.

            Call it with no argument once per session to list the topics,
            then read the relevant one BEFORE acting:
              - before creating or moving operators -> 'create-operator',
                'network-layout'
              - before writing TD Python (execute_python, DAT content) ->
                'td-python', 'td-api-reference'
              - before creating or editing annotations -> 'manage-annotations'
              - before designing custom parameters -> 'parameter-design'
              - before externalizing, heavy/visual builds, movie export,
                releases -> the matching topic in the list

            Complements get_docs: get_docs is official Derivative
            documentation, get_guidance is how THIS project wants the work
            done.

            Args:
                topic: Topic id from the listing (e.g. "create-operator").
                    Matching ignores case and punctuation, so
                    "create_operator" and "createoperator" also resolve.
                    Omit to get the topic list.

            Returns:
                Without topic: dict with topics (topic, kind 'rule'|'skill',
                description), count, source, usage. With topic: dict with
                topic, kind, description, path, content, and truncated/note
                when the document exceeded the response cap. On a miss: the
                topic list plus error and did_you_mean.
            """
            # Answered on the worker thread from the filesystem -- no TD
            # access, so no main-thread round-trip (mcp-safety).
            return self._get_guidance(topic)

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
        def get_dat_content(op_path: str,
                            format: Literal["auto", "text", "table",
                                            "stats"] = "auto") -> dict:
            """
            Get the content of a DAT operator (text or table data).

            Args:
                op_path: Path to the DAT operator
                format: "text" for raw text, "table" for row/column data,
                       "auto" to detect based on DAT type, "stats" to reduce
                       a table to per-column min/max/mean (numeric) or
                       distinct counts (text) plus head/tail rows -- use it
                       instead of dumping a large table into context

            Returns:
                Dict with DAT content (text string or table rows/cols)
            """
            return self._execute_in_td('get_dat_content', {
                'op_path': op_path,
                'format': format
            })

        @self.mcp.tool()
        def get_chop_data(op_path: str, channels: str = None,
                          samples: int = 0, compare_to: str = None) -> dict:
            """
            Read a CHOP as per-channel STATISTICS, never a blind dump.

            A 4x600 CHOP is 2400 raw floats; this returns min/max/mean/std +
            first/last per channel instead. Pass samples>0 only when the raw
            values matter.

            Args:
                op_path: Path to the CHOP operator
                channels: Glob on channel name (e.g. "chan*"); omit for all
                    (capped at 32 channels, with channelsOmitted reported)
                samples: Head/tail raw values per channel (0 = stats only)
                compare_to: Another CHOP path -- adds a 'diff' block of
                    per-channel min/max/mean deltas. The "what did this chain
                    actually do to my data" read.

            Returns:
                Dict with numChans/numSamples/rate/isTimeSlice and channels[]
            """
            return self._execute_in_td('get_chop_data', {
                'op_path': op_path,
                'channels': channels,
                'samples': samples,
                'compare_to': compare_to
            })

        @self.mcp.tool()
        def get_pop_data(op_path: str, attributes: str = None,
                         samples: int = 0, max_points: int = 50000) -> dict:
            """
            Read a POP: attribute metadata always, point values on request.

            Metadata (numPoints + point/prim/vert attribute names, sizes and
            types) costs ~0.02ms regardless of point count. Reading actual
            point VALUES is a GPU->CPU readback that stalls the main thread:
            measured ~9.5ms at 16k points and ~69ms at 160k (~4 frames at
            60fps) on 2025.33070. Sampling is therefore opt-in and gated on
            the operator's TOTAL point count, not on how many you ask for --
            POP.points(count=N) does not bound the readback.

            Args:
                op_path: Path to the POP operator
                attributes: Glob on attribute name (e.g. "P"); omit for all
                samples: Head point values to read (0 = metadata only)
                max_points: Refuse the readback above this many points;
                    the response then carries readbackRefused explaining why

            Returns:
                Dict with numPoints and pointAttributes/primAttributes/
                vertAttributes (each name/size/type), plus head values when
                samples>0 and the point count is within max_points
            """
            return self._execute_in_td('get_pop_data', {
                'op_path': op_path,
                'attributes': attributes,
                'samples': samples,
                'max_points': max_points
            })

        @self.mcp.tool()
        def set_dat_content(op_path: str, text: str = None,
                           rows: list = None, clear: bool = False,
                           confirm_wipe: bool = False) -> dict:
            """
            Replace a DAT's entire text or table content.

            Prerequisite when writing TD Python into the DAT: load the
            /td-api-reference skill first.

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

            Prerequisite when writing TD Python into the DAT: load the
            /td-api-reference skill first.

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
        def create_annotation(parent_path: str,
                              mode: Literal["annotate", "comment",
                                            "networkbox"] = "annotate",
                              text: str = "", title: str = "",
                              x: int = None, y: int = None,
                              width: int = None, height: int = None,
                              color: list = None, opacity: float = None,
                              name: str = None) -> dict:
            """
            Create a Comment, Network Box, or Annotate in the network editor.

            Prerequisite: load the /manage-annotations skill first --
            coordinate math (nodeX/nodeY is the bottom-left corner) and
            sizing rules live there.

            The annotation is created utility=True (matching TD UI-drawn
            annotations), so it appears in get_annotations but NOT in
            query_network/find_children unless include_utility=True.
            All op-path tools (set_annotation, delete_op, set_parameter,
            ...) still resolve it by path.

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

            Prerequisite: load the /manage-annotations skill first --
            coordinate math (nodeX/nodeY is the bottom-left corner) and
            sizing rules live there.

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

            Prerequisite: load the /externalize-operator skill before first
            use in a session -- the required workflow steps live there.

            Args:
                op_path: Path to the operator
                tag_type: Tag type - "tox" for COMPs, "py"/"txt"/"tsv"/"json" etc for DATs
                         If None, will auto-detect based on operator type

            Unattended sessions: a TDN operation that meets a TD palette
            component can raise the Black-Box-vs-Full-Export dialog.
            Decide programmatically BEFORE the call: set the
            Tdnpalettehandling parameter on the Embody COMP ('blackbox' |
            'fullexport' | 'ask'), or per COMP via
            comp.store('_tdn_palette_handling', 'blackbox').

            File-removal behavior likewise follows the Filecleanup parameter
            ('ask' | 'keep' | 'delete') -- set it rather than letting a modal
            wait for a human who is not there.

            Returns:
                Dict with success status and applied tag
            """
            return self._execute_in_td('externalize_op', {
                'op_path': op_path,
                'tag_type': tag_type
            })

        @self.mcp.tool()
        def remove_externalization_tag(op_path: str,
                                       delete_file: bool = False) -> dict:
            """
            Remove Embody externalization tracking from an operator
            (tag, table row, and TDN breadcrumb).

            Args:
                op_path: Path to the operator
                delete_file: Also delete the externalized file on disk
                    (best-effort; safety checks may keep it). Default False
                    keeps the file.

            Returns:
                Dict with success, removed_tags (operator tags stripped),
                removed_rows (registry rows dropped, including descendants),
                removed_anything, and a human-readable summary. An operator
                can have a tracked row but no tag (a pre-guard annotation
                artifact), so removed_tags alone cannot confirm cleanup --
                check removed_anything or removed_rows.
            """
            return self._execute_in_td('remove_externalization_tag', {
                'op_path': op_path,
                'delete_file': delete_file
            })

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

            Prerequisite: load the /externalize-operator skill before first
            use in a session.

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

            Prerequisite: load the /create-extension skill before first use --
            required parameters, lifecycle methods, and wiring steps.

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

            Unattended sessions: a TDN operation that meets a TD palette
            component can raise the Black-Box-vs-Full-Export dialog.
            Decide programmatically BEFORE the call: set the
            Tdnpalettehandling parameter on the Embody COMP ('blackbox' |
            'fullexport' | 'ask'), or per COMP via
            comp.store('_tdn_palette_handling', 'blackbox').

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

            Unattended sessions: a TDN operation that meets a TD palette
            component can raise the Black-Box-vs-Full-Export dialog.
            Decide programmatically BEFORE the call: set the
            Tdnpalettehandling parameter on the Embody COMP ('blackbox' |
            'fullexport' | 'ask'), or per COMP via
            comp.store('_tdn_palette_handling', 'blackbox').

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
        def capture_top(op_path: str,
                        format: Literal["jpeg", "png"] = "jpeg",
                        quality: float = 0.8,
                        max_resolution: int = 640, inline: bool = False,
                        sample_grid: int = 0) -> list:
            """
            Capture a TOP as a temp image file or sampled RGBA grid.

            File path is returned by default; inline=True embeds a small preview.
            sample_grid>=2 returns an NxN RGBA grid instead, clamped 2..32 with
            row 0 at image top-left; image format args are ignored.

            The returned text carries a Quality verdict computed from the raw
            pixels (luminance stats, alpha coverage): a FAIL flags a
            black / flat / fully-transparent frame so you can tell an empty
            render from a real one WITHOUT reading the image. Never declare a
            visual task done on a FAIL verdict.

            Args:
                op_path: Path to a TOP operator
                format: "jpeg" or "png"
                quality: JPEG compression quality 0.0-1.0
                max_resolution: Max pixels on longest edge; 0 = native
                inline: True embeds a small base64 preview
                sample_grid: >=2 returns an RGBA sample grid

            Returns:
                Saved path text (with a Quality verdict line), inline image
                content, or sample-grid dict
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

            # Surface the black/empty-frame verdict as text so the agent can
            # branch on it WITHOUT reading the image -- enforces the
            # "never declare a visual task done on a black frame" rule.
            q = result.get('quality') or {}
            if q:
                if q.get('pass'):
                    info += (f"\nQuality: OK (max_lum={q.get('max_luminance')}, "
                             f"std={q.get('std_luminance')})")
                else:
                    info += (f"\nQuality: FAIL {q.get('fail_reasons')} "
                             f"(max_lum={q.get('max_luminance')}, "
                             f"mean_lum={q.get('mean_luminance')}"
                             + (f", mean_alpha={q['mean_alpha']}"
                                if 'mean_alpha' in q else '') + ") -- the frame "
                             f"is likely black/empty/transparent. Do NOT declare "
                             f"the task done; load /debug-operator and fix the "
                             f"chain, then re-capture.")

            # Inline base64 images are token-heavy, so only embed when the caller
            # explicitly asks (inline=True) and the image is small. By default
            # return just the path; Read the file when actually judging a frame.
            if inline and result['size_bytes'] < 20000:
                return [info, self._Image(data=image_bytes, format=result['format'])]
            return info + "\n(Use Read tool on the file path above to view the image)"

        # === Logging ===

        @self.mcp.tool()
        def get_logs(level: Optional[Literal['DEBUG', 'INFO', 'WARNING',
                                             'ERROR', 'SUCCESS']] = None,
                     count: int = 50, since_id: int = None,
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
                claim_scope, stale = no traffic for >90s), 'count', 'you'
                (the caller's own sid, or null for clients that connect
                without a bridge), and 'tasks' -- the shared task ledger's
                ACTIVE entries from every session (see announce_task):
                in_progress, plus done_uncommitted meaning FINISHED work
                sitting uncommitted in the tree -- never mistake that for
                in-flight work, and never build over those files without
                committing or coordinating first. Sessions silent for over
                an hour are dropped. Responses to ANY tool also carry a '_peers'
                advisory list automatically when your request overlaps
                territory another session touched recently; an entry with
                conflict=true means a peer WROTE there within the last
                minute -- stop and coordinate before proceeding. The moment
                peers or a _peers advisory appear, load the
                /multi-session-etiquette skill for the full protocol.
            """
            # Answered on the worker thread from pure-Python state -- no
            # TD access, so no main-thread round-trip (mcp-safety).
            snapshot = self._sessions_snapshot()
            sid, _label = _SESSION_CTX.get()
            snapshot['you'] = sid
            # The shared task ledger rides along so session start needs no
            # extra call: in_progress and done_uncommitted entries from
            # EVERY session (see announce_task / update_task).
            try:
                snapshot['tasks'] = self._tasks_snapshot()
            except Exception:
                pass
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
        def announce_task(title: str, scopes: list = None,
                          note: str = "") -> dict:
            """
            Announce a unit of work to the shared task ledger.

            The ledger (.embody/tasks.json) is how parallel AI sessions
            know what is being worked on and -- crucially -- what is
            FINISHED but not yet committed. Claims (claim_scope) expire
            the moment a session goes quiet; ledger entries persist, so a
            session arriving later still sees the state of the work
            instead of guessing it from dirty files and timestamps.

            Announce at the START of substantive work (a feature, a fix, a
            refactor -- not single-tool edits), then keep it honest with
            update_task: done_uncommitted when the work is finished but
            sitting in the tree, committed (with the sha) once it lands,
            abandoned if dropped. Active tasks ride back on get_sessions
            for every session.

            Args:
                title: Short human-readable name for the work
                scopes: Op paths, file:<repo-relative> paths, or
                    project:<name> scopes the work touches (max 8)
                note: One-line intent or state detail

            Returns:
                {'announced': True, 'task': {...}} with the new task id
                (tsk_...)
            """
            # Worker-side pure Python + ledger file I/O -- no TD access
            # (mcp-safety).
            sid, label = _SESSION_CTX.get()
            return self._announce_task(sid, label, title, scopes, note)

        @self.mcp.tool()
        def update_task(task_id: str,
                        status: Optional[Literal[
                            'in_progress', 'done_uncommitted',
                            'committed', 'abandoned']] = None,
                        note: str = None, commit: str = None) -> dict:
            """
            Update a shared-ledger task's status, note, or commit sha.

            Transitions (see announce_task): -> done_uncommitted when the
            work is complete but uncommitted -- the state peers MUST see
            before touching the same files; -> committed once it lands
            (pass commit=<sha>; a sha alone implies the transition);
            -> abandoned when dropped. Any session may update any task --
            marking a dead session's stale entry abandoned is cooperative
            hygiene -- and the ledger records updated_by when the writer
            is not the owner.

            Args:
                task_id: The id announce_task returned (tsk_...)
                status: New lifecycle status
                note: Replacement one-line note
                commit: Commit sha once the work landed

            Returns:
                {'updated': True, 'task': {...}}, or an error listing the
                active tasks when the id is unknown
            """
            # Worker-side pure Python + ledger file I/O -- no TD access
            # (mcp-safety).
            sid, label = _SESSION_CTX.get()
            return self._update_task(sid, label, task_id, status, note,
                                     commit)

        @self.mcp.tool()
        def preflight_landing(worktree_path: str) -> dict:
            """
            Check whether a worktree's diff can land safely in the main tree.

            Read-only and TD-free (answered on the worker thread). Reports
            three collision classes BEFORE any file moves: landing files
            also dirty in the MAIN tree (a running TD re-exports
            externalized files -- blind overwrite is the classic landing
            failure), landing files claimed or recently written by PEER
            sessions, and landing files whose live TDN/DAT state is
            UNSAVED (dirty in externalizations.tsv). Run it before porting
            any worktree diff; a 'conflicts' verdict means reconcile first.

            Args:
                worktree_path: The worktree directory -- absolute, or
                    relative to the repo root (e.g. "../Embody-wt-task")

            Returns:
                Dict with worktree, landing_files, collisions {main_dirty,
                peers, tdn_unsaved}, verdict 'clear'|'conflicts', or
                {'error': ...}. May also carry 'ledger_tasks' (active
                shared-ledger tasks whose file: scopes intersect the
                landing, each with 'overlap') and 'ledger_hint' when one is
                done_uncommitted -- commit that work or coordinate before
                landing over it. Only file:-scoped tasks can be matched
                here (op-path scopes need TD to resolve), so an absent
                ledger_tasks does NOT prove no ledger work overlaps.
            """
            sid, _label = _SESSION_CTX.get()
            return self._preflight_landing(worktree_path, caller_sid=sid)

        @self.mcp.tool()
        def run_tests(suite_name: str = None, test_name: str = None,
                      override: bool = False, background: bool = False,
                      idempotency_key: str = None) -> dict:
            """
            Run Embody test suites and return results.

            Prerequisite: load the project's /run-tests skill (when present)
            and save the project before a full run.

            background=True is the RESILIENT mode -- recommended for full
            runs: the run starts and this call returns a job id
            IMMEDIATELY; poll get_job_status(job_id) for the summary. The
            synchronous mode holds this HTTP call open for the whole run,
            and a server restart mid-run (the Envoy watchdog suites cause
            one on every full run) severs the transport even though the
            run finishes.

            Args:
                suite_name: Run only this suite (e.g., "test_path_utils"). Omit to run all.
                test_name: Run only this test method within the suite.
                override: Bypass the multi-session gate when another live
                    session holds project:tests or wrote very recently.
                background: Return {'job_id', 'status': 'running'}
                    immediately; results park restart-proof on disk.
                idempotency_key: background only. A stable key that makes a
                    RETRY safe: a second background run with the same key
                    reconciles to the original run's handle instead of
                    starting (or being refused as) a duplicate. Omit for a
                    one-shot run.

            Returns:
                Synchronous: dict with passed/failed/error/skip counts and
                the failing tests. background=True: {'job_id', 'status',
                'hint'}.
            """
            if idempotency_key and not background:
                # A synchronous run holds the transport open and returns
                # results inline -- there is no durable record to reconcile
                # a retry to, so a key here would be silently useless.
                # Refuse loudly rather than drop an idempotency guarantee.
                return {'error': 'idempotency_key requires background=True '
                                 '-- a synchronous run has no durable record '
                                 'to reconcile a retry to.'}
            if background:
                # Normal round-trip: the main-thread handler only STARTS
                # the deferred run and returns the job handle; results
                # land in the disk record, immune to server restarts.
                return self._execute_in_td('run_tests', {
                    'suite_name': suite_name, 'test_name': test_name,
                    'override': override, 'background': True,
                    'idempotency_key': idempotency_key})

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

        @self.mcp.tool()
        def get_job_status(job_id: str = None) -> dict:
            """
            Status of background jobs (run_tests background=True,
            save_project).

            Jobs are disk-backed (.embody/jobs/), so they survive server
            restarts and extension reinits -- the failure mode that severs
            a long synchronous call. Poll with the job_id the starting
            tool returned; omit it to list recent jobs. A finished
            run_tests job carries the summary (counts + the failing
            tests); a finished save_project job carries
            version_before/version_after.

            Args:
                job_id: The id the starting tool returned (job_...). Omit
                    to list recent jobs.

            Returns:
                One job record (status running|done|error, result when
                done, stale=true when a running record stopped updating),
                or {'jobs': [...], 'count'} without job_id. The listing is
                capped at the 16 newest records (older ones remain
                fetchable by id until the 24h retention). Caveat: a run
                interrupted hard enough to kill its own tick chain can
                close as 'done' summarizing only the tests that ran --
                cross-check dev/logs when a summary looks short.
            """
            # Worker-side pure filesystem -- no TD access (mcp-safety).
            now = time.time()
            if job_id:
                record = _read_job(job_id)
                if record is None:
                    return {'error': 'no job with id %r' % job_id,
                            'jobs': _list_jobs(now)}
                return _job_public(record, now)
            jobs = _list_jobs(now)
            return {'jobs': jobs, 'count': len(jobs)}

        @self.mcp.tool()
        def save_project(idempotency_key: str = None) -> dict:
            """
            Save the TouchDesigner project as a tracked background job.

            project.save() blocks TD's main thread for many seconds (the
            TDN strip/restore cycle plus the release-tox export) and
            reinitializes extensions, so a synchronous MCP call is severed
            even though the save succeeds. This tool returns a job id
            immediately; the save runs a few frames later. Poll
            get_job_status(job_id) -- the finished record carries
            version_before/version_after and the saved .toe name. Expect
            this session's next call to ride a brief bridge reconnect.

            Args:
                idempotency_key: A stable key that makes a RETRY safe. A
                    save already dedupes a resubmission arriving within its
                    ~2 min in-flight window; a key extends that to any age,
                    so a retry after a severed ack reconciles to the
                    original save instead of queuing a second multi-second
                    save. Omit for a one-shot save.

            Returns:
                {'job_id', 'status': 'running', 'hint'}
            """
            return self._execute_in_td('save_project',
                                       {'idempotency_key': idempotency_key})

        @self.mcp.tool()
        def update_embody(idempotency_key: str = None) -> dict:
            """
            Self-update Embody to the latest GitHub release, as a job.

            Bounded and non-interactive: the node's own updater fetches
            the official release, verifies the sha256-pinned manifest,
            refuses downgrades and TD-build-floor violations, then swaps
            the component in place (no caller code or URL is involved --
            this never needs the TD Python grant). Returns a job id
            immediately; poll get_job_status(job_id). A finished record
            carries version_before/version_after; an up-to-date node
            finishes 'done' with versions equal. The install restarts
            the MCP server, so expect one reconnect blip.

            Args:
                idempotency_key: Stable key making a RETRY safe -- a
                    redelivery reconciles to the original update job.

            Returns:
                {'job_id', 'status': 'running', 'hint'} or {'error'}.
            """
            return self._execute_in_td('update_embody',
                                       {'idempotency_key': idempotency_key})

        # Host-private lifecycle helpers.  They are visible in the local MCP
        # manifest because FastMCP has no hidden-tool concept, but calls are
        # accepted only from Convoy's dedicated loopback session and Convoy's
        # LAN operation registry does not expose these names.  All TD access
        # still crosses _execute_in_td onto the main thread.

        @self.mcp.tool()
        def convoy_lifecycle_state() -> dict:
            """Host-private clean/unsaved snapshot for exact-node restart."""
            sid, _label = _SESSION_CTX.get()
            if sid != 'convoy-lifecycle':
                return {'error': 'Convoy lifecycle host session required'}
            return self._execute_in_td('convoy_lifecycle_state', {})

        @self.mcp.tool()
        def convoy_lifecycle_quit(expected_dirty_revision: str = None,
                                  discard: bool = False) -> dict:
            """Host-private CAS-guarded quit for exact-node restart."""
            sid, _label = _SESSION_CTX.get()
            if sid != 'convoy-lifecycle':
                return {'error': 'Convoy lifecycle host session required'}
            return self._execute_in_td('convoy_lifecycle_quit', {
                'expected_dirty_revision': expected_dirty_revision,
                'discard': discard,
            })

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
        # NOTE: _get_docs and its helpers run on the WORKER thread (the tool
        # wrapper calls self._get_docs directly -- pure file/HTTP work kept off
        # the main thread). They must live on the facade: mod.* is a TD object
        # and is unavailable off the main thread (ext-diet WP4 gate finding).
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
            # Wiki prose says what a parameter MEANS; this says what a fresh
            # op on THIS build is actually created with. Cached with the page.
            live = self._docsLiveDefaults(query, doc.get('title'))
            if live:
                result['live'] = live
            cache[cache_key] = result
            if len(cache) > 20:
                cache.pop(next(iter(cache)))
            return result
        except Exception as e:
            return {'error': f'Documentation lookup failed: {e}'}

    # --- Live parameter defaults fused into get_docs -----------------
    # The wiki documents a parameter; it does not tell you what a FRESH op on
    # THIS build actually creates it with -- and Par.default lies for menus.
    # Embody already harvests the truth per build (CatalogManager probes real
    # instances; divergent_defaults.tsv is the shipped bootstrap), so the
    # fusion READS that catalog instead of probing again. Worker-thread safe:
    # pure file I/O, no mod.*, no TD objects (see _get_docs's thread note).
    # Idea adapted from describe(docs) in Marius Alwan Meyer's codemode fork
    # (github.com/sporqist/Embody, MIT); the catalog source is ours.

    _DOCS_PAR_CAP = 80

    def _docsDefaultsIndex(self) -> dict:
        """{op_type_lower: {par_name: default}} of creation defaults.

        Source priority mirrors TDNExt._loadDivergentDefaults so the two
        cannot disagree about what a default IS:
          1. .embody/catalog_<build>.json -- probed from real instances on
             THIS build by CatalogManager (~650 op types, complete).
          2. divergent_defaults.tsv -- the shipped bootstrap, which records
             only DIVERGENT params and only for the builds it shipped with.
        Read once per session and cached; any failure yields {} so a missing
        catalog degrades docs to wiki-only rather than breaking them.
        """
        if self._docs_state.get('defaults') is not None:
            return self._docs_state['defaults']
        index = {}
        root = getattr(sys, '_envoy_repo_root', None)
        build = self._docs_state.get('build')

        # 1. per-build catalog
        try:
            if root and build:
                cat = os.path.join(root, '.embody', f'catalog_{build}.json')
                if os.path.isfile(cat):
                    with open(cat, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                    for op_type, pars in data.items():
                        # '_'-prefixed keys are reserved metadata (_palette)
                        if op_type.startswith('_') or not isinstance(pars, dict):
                            continue
                        index[op_type.lower()] = pars
        except Exception as e:
            self._log(f'Docs catalog unreadable: {e}', 'DEBUG')

        # 2. bootstrap tsv (only fills types the catalog did not supply)
        if not index:
            try:
                tsv = os.path.join(root or '', 'dev', 'embody', 'Embody',
                                   'divergent_defaults.tsv')
                if os.path.isfile(tsv):
                    with open(tsv, 'r', encoding='utf-8') as f:
                        rows = [ln.rstrip('\n').split('\t')
                                for ln in f if ln.strip()]
                    header, data = rows[0], rows[1:]
                    col = (header.index(build) if build in header
                           else len(header) - 1)
                    for r in data:
                        if len(r) > col and r[col]:
                            index.setdefault(r[0].lower(), {})[r[1]] = r[col]
            except Exception as e:
                self._log(f'Docs defaults bootstrap unreadable: {e}', 'DEBUG')

        self._docs_state['defaults'] = index
        return index

    def _docsLiveDefaults(self, query, title):
        """Authoritative creation defaults for the op type a docs page is
        about, or None. Matched on the page title first, then the query."""
        index = self._docsDefaultsIndex()
        if not index:
            return None
        for candidate in (title, query):
            if not candidate:
                continue
            key = str(candidate).strip().lower().replace(' ', '')
            for suffix in ('', 'top', 'chop', 'sop', 'dat', 'comp', 'mat', 'pop'):
                hit = index.get(key + suffix)
                if hit:
                    pars = dict(list(hit.items())[:self._DOCS_PAR_CAP])
                    live = {
                        'opType': key + suffix,
                        'build': self._docs_state.get('build'),
                        'source': 'Embody catalog for this TD build',
                        'note': ('creation defaults probed from a real '
                                 'instance -- authoritative where '
                                 'Par.default is not (menus especially)'),
                        'parameters': pars,
                    }
                    if len(hit) > len(pars):
                        live['parametersOmitted'] = len(hit) - len(pars)
                    return live
        return None

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
        self._docs_state['build'] = result.get('build')
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

    @staticmethod
    def _verifiedTlsContext():
        """A VERIFYING SSL context that works on TD's bundled Python.

        macOS's bundled Python has no default CA path (Windows uses the
        OS store), so HTTPS from TD failed CERTIFICATE_VERIFY_FAILED
        there. certifi ships with TD (a requests dependency); load it IN
        ADDITION to system defaults. Never disables verification.
        """
        import ssl
        context = ssl.create_default_context()
        try:
            import certifi
            context.load_verify_locations(cafile=certifi.where())
        except Exception:
            pass  # the OS store may already suffice (Windows)
        return context

    def _docsWeb(self, query):
        try:
            tls = self._verifiedTlsContext()
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
            with urllib.request.urlopen(request, timeout=8,
                                        context=tls) as response:
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
            with urllib.request.urlopen(request, timeout=8,
                                        context=tls) as response:
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
        # 2.0's modern-envelope path (protocol 2026-07-28) logs handler
        # exceptions via mcp.server.runner instead of raising through the
        # ASGI wrapper -- same filter there, so disconnect noise does not
        # return when clients adopt the new protocol revision.
        logging.getLogger("mcp.server.runner").addFilter(
            _DisconnectCrashFilter()
        )

        # (SDK 1.x needed a filter here dropping the lowlevel server's
        # per-request "Processing request of type X" / empty "Received
        # exception from stream:" lines. The 2.0 dispatcher emits neither --
        # verified against the 2.0.0 wheel -- so that filter is gone. If a
        # future SDK reintroduces per-request textport spam, filter it on the
        # emitting logger rather than raising levels, as above.)

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
                        _queueWorkerLog(f'response_checker exiting: {e}')
                    break

        Thread(target=response_checker, daemon=True).start()

        # Manage uvicorn directly so we can signal shutdown via shutdown_event.
        # SDK 2.0: stateless_http / transport_security / host live on the app
        # builder now, not the server constructor.
        # max_request_body_size: 2.0 introduces a 4 MiB default cap that 1.x
        # never enforced; an oversized tools/call gets a 413 + connection
        # reset, which the bridge reads as "Lost connection to Envoy". Big
        # import_network / set_dat_content payloads (multi-thousand-op
        # networks) can cross 4 MiB, so raise it well clear. Localhost-only
        # + Host/Origin-validated, so the cap is sanity, not exposure.
        app_kwargs = {
            'stateless_http': True,
            'host': '127.0.0.1',
            'max_request_body_size': 64 * 1024 * 1024,
        }
        if self._transport_security is not None:
            app_kwargs['transport_security'] = self._transport_security
        starlette_app = self.mcp.streamable_http_app(**app_kwargs)

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
            # TD replaces sys.stdout with a Textport catcher, and some builds
            # (e.g. 2025.32460 on Windows) ship one WITHOUT isatty(). uvicorn's
            # default log formatter probes sys.stdout.isatty() when use_colors
            # is unset, so Config() itself raises ("Unable to configure
            # formatter 'default'") before the socket ever binds -- and the
            # liveness watchdog then restarts the dead worker forever. An
            # explicit False skips the probe entirely (ANSI color codes would
            # be garbage in the Textport anyway).
            use_colors=False,
        )
        uvi_server = uvicorn.Server(config)

        # Store on sys so EnvoyExt.Start() can force-close sockets
        # if the old server thread is stuck and won't release the port.
        sys._envoy_uvi_server = uvi_server
        sys._envoy_uvi_gen = self.gen

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
                sys._envoy_uvi_gen = 0
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
        # Generation counter for stale callback detection. Adopted from sys
        # (not reset to 0) so an extension reinit cannot restart the count
        # below a still-alive worker's sys._envoy_uvi_gen tag -- that would
        # make _forceCloseOldServer's staleness compare lie forever.
        self._server_gen: int = getattr(sys, '_envoy_server_gen', 0)
        # Per-session piggyback cursors: sid (or '_anon') -> last served log
        # id. A single shared cursor let whichever session polled first
        # CONSUME warnings meant for everyone (multi-session bug); each
        # session now tracks its own position in the log ring.
        self._log_cursors: dict = {}
        # Advisory dedup: sid -> {(peer_sid, scope): last_served_ts}. Keeps
        # _peers token-lean; conflicts bypass it. Reset on reinit is fine.
        self._advisories_served: dict = {}
        self._peer_hint_served: set = set()
        # Write-effect footer baselines: sid (or '_anon') -> {'errors': set,
        # 'warnings': set, 'fps': float}. A session's FIRST write only
        # establishes the baseline (nothing pre-existing is ever reported as
        # newly introduced). Plain instance state -- reset on reinit is fine,
        # the next write just re-baselines.
        self._effects_state: dict = {}
        # Safety valve: flipped False (once, loudly) if the project-wide error
        # scan ever exceeds _EFFECTS_SCAN_BUDGET_S on the main thread.
        self._effects_error_scan_ok: bool = True
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
        # Ports that recently FAILED a real uvicorn bind live in
        # sys._envoy_bad_bind_ports ({port: time.time()}) for this long, and
        # _findAvailablePort skips them. Defense-in-depth behind the bind
        # probe: covers a probe/bind race (another process grabs the port in
        # between), so a restart storm advances to the next port instead of
        # re-picking the same poisoned one every attempt (2026-07-23 loop).
        self._BIND_FAIL_TTL_SECONDS: float = 600.0
        # H1 startup-readiness state: 'Running' is declared only after the
        # worker confirms a real bind (via _pollStartup).  _starting guards the
        # window so duplicate Start() calls are suppressed before envoy_running.
        self._starting: bool = False
        self._runtime_port: Optional[int] = None
        self._startup_event: Optional[Event] = None
        self._startup_deadline: float = 0.0
        self._venv_recreated: bool = False  # Guard: only auto-recreate venv once per session
        # Guard: probe each venv python binary at most once per session --
        # Start() re-runs on every watchdog revive, and re-probing each time
        # was a recurring synchronous main-thread stall (issue #60). Holds
        # the PATH of the successfully probed interpreter (empty = none),
        # so a mid-session project-root switch still probes the other venv.
        self._venv_probe_ok: str = ''
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
        self._undo_active_since: float = 0.0  # for the latched-guard self-heal

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
        # Issue #57 activation gates (see envoy_viz: vizSettled/coldHoldElapsed).
        # A create_op on TD 2025.32460 wedged the main thread (orphaned/self-owned
        # CS inside TD's editor internals) when viz editor work ran in the same
        # frame as the mutation; these gates decouple the two.
        self._viz_mutation_frame: int = -10 ** 6      # absTime.frame of the last mutating op (settle gate)
        self._viz_session_warm: bool = False          # False until a cold activation's hold has elapsed
        self._viz_cold_since: int = -1                # absTime.frame the cold hold began; -1 = not started
        # Issue #86 relocation gates (see envoy_viz: netRelocationOK). Embot
        # rebuilds cost 150-230ms of MAIN-THREAD copyOPs; an unbounded relocation
        # rate measured at 28% of all wall clock. These few clocks bound it, and
        # they are the ENTIRE state cost of the fix -- no per-network bookkeeping,
        # no cached operators, nothing that can outlive a vizCleanup.
        self._viz_home: Optional[tuple] = None           # (netpath, absTime.seconds committed) viz is committed to
        self._viz_net_candidate: Optional[tuple] = None  # (netpath, absTime.seconds entered) not yet committed to
        self._viz_relocate_blocked_since: Optional[float] = None  # when the gate started refusing (starvation ceiling)
        self._viz_relocate_blocked_last: Optional[float] = None    # last refusal-eligible gate call; a long gap ends the streak
        self._viz_write_suppress: dict = {}              # {comp_path: expiry} nets a .tox write just retired him out of
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

        Signals all known shutdown events, force-closes our uvicorn
        listeners, unblocks stuck test Events. True ONLY when a live
        handle of OURS was closed (_findAvailablePort keys its drain-wait
        off this). Staleness check runs FIRST: in a restart storm the
        handle can belong to the CURRENT generation's healthy newborn,
        and closing it feeds a self-sustaining loop (2026-07-15) -- so a
        current-gen live worker makes this a no-op. Accepted trade: a
        wedged current-gen worker drifts the port +1 until the watchdog's
        revive (which bumps the generation first) recovers it.
        """
        old_server = getattr(sys, '_envoy_uvi_server', None)
        old_gen = getattr(sys, '_envoy_uvi_gen', 0)
        if old_server is not None and old_gen >= self._server_gen:
            self._log(
                f'Skipping force-close: live server handle is current '
                f'(gen {old_gen} >= gen {self._server_gen})', 'DEBUG')
            return False

        # Signal all known shutdown events (housekeeping -- does not by
        # itself indicate WE are holding a socket). Safe now: the current-
        # generation live-worker case returned above, and the registry entry
        # for this comp is either the previous (dead/stale) start's event or
        # the current one whose worker has already exited.
        registry = getattr(sys, '_envoy_shutdown_events', {})
        for path, evt in registry.items():
            if self._starting and evt is self.shutdown_event:
                # An in-flight start owns this event. Its worker may not have
                # stored its sys._envoy_uvi_server handle yet (first ~100ms),
                # so the gen check above cannot see it -- signaling now would
                # kill the newborn before uvicorn even starts (the exact
                # dead-on-arrival loop of 2026-07-15).
                self._log(
                    f'Skipping shutdown signal for {path}: start in flight',
                    'DEBUG')
                continue
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

        Taken = cannot bind, OR registered to a live PID in envoy.json,
        OR on the recent bind-failure blacklist. The probe is a real
        bind(), never connect(): a zombie TD holds a port bound with a
        dead accept loop -- connects REFUSED, bind still 10048 -- and the
        connect probe re-picked that poisoned port forever (2026-07-23).
        Base port first; if busy, force-close our own stale server, then
        scan the range. Returns the port or None.
        """
        import socket
        import os as _os

        def _port_bindable(port: int) -> bool:
            # Mirror uvicorn's bind exactly (plain bind, no SO_REUSEADDR) so
            # the answer predicts the real startup outcome -- including
            # zombie-held and TIME_WAIT-blocked ports.
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                try:
                    s.bind(('127.0.0.1', port))
                    return True
                except OSError:
                    return False

        def _recent_bind_failure(port: int) -> bool:
            bad = getattr(sys, '_envoy_bad_bind_ports', None)
            if not bad:
                return False
            ts = bad.get(port)
            if ts is None:
                return False
            if time.time() - ts < self._BIND_FAIL_TTL_SECONDS:
                return True
            bad.pop(port, None)  # expired -- eligible again
            return False

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
            return (_recent_bind_failure(port)
                    or not _port_bindable(port)
                    or _port_registered_by_other(port))

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
        # Envoyenable is the master switch. Queued restart fires (auto-restart
        # backoff, watchdog revive) can land AFTER the user -- or the give-up
        # path in _scheduleRestart -- disabled Envoy; without this gate they
        # kept spawning servers for minutes after 'Envoy disabled'
        # (2026-07-15 storm, issue #57 follow-up).
        if not self.ownerComp.par.Envoyenable.eval():
            self._log('Start ignored -- Envoy is disabled', 'DEBUG')
            return
        # Perform Mode suspends Envoy WITHOUT touching Envoyenable (config.json
        # integrity), so the master-switch gate above cannot catch a queued
        # Start: a revive/restart armed before Perform entry would otherwise
        # bring the server back mid-show. Defense-in-depth beside the watchdog's
        # own Perform idle gate. The Perform-exit restart is unaffected --
        # _exitPerformMode runs after the live Performmode par is already off,
        # so its delayed Start() reads False here.
        if self._performModeActive():
            self._log('Start refused -- Perform Mode is active', 'WARNING')
            return
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
        # PATH/DLL/VIRTUAL_ENV linking is main-thread-only, so it lives
        # here (Start runs on the main thread), never in the worker.
        Embody._linkEnv(spec)

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
        # Module function, resolved on the MAIN thread (mod.* is a TD
        # lookup); the EmbodyExt facade would re-resolve mod inside the
        # worker (extracted to embody_pyenv 2026-08-19).
        import_gate_check = mod.embody_pyenv.import_gate_check

        def worker():
            try:
                # site_packages arms the stale-interpreter (restart-required)
                # detection for upgraded-on-disk dependency stacks.
                result = import_gate_check(spec['site_packages'])
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

        # Perform Mode may have been entered while the gate warmed. Its Stop()
        # no-ops on a not-yet-bound start (envoy_running is still False), so
        # finishing the start here would bring the server up mid-show behind
        # the Start()/watchdog Perform gates. Leave status alone --
        # _enterPerformMode owns the 'Perform Mode' readout.
        if self._performModeActive():
            if ok:
                # The warm-up itself succeeded -- keep it, so the
                # post-Perform start takes the fast path instead of
                # re-running the multi-second import gate.
                sys._envoy_import_gate_ok = True
            self._log('Perform Mode entered during Python environment prep '
                      '-- not starting.', 'DEBUG')
            return

        if not ok:
            # 'Error...' statuses idle the liveness watchdog, so a refusal
            # (e.g. restart-required after an on-disk upgrade) is calm: each
            # explicit Start re-runs the gate and re-refuses cheaply.
            self.ownerComp.par.Envoystatus = (
                'Error: restart TouchDesigner to finish MCP upgrade'
                if 'restart TouchDesigner' in message
                else 'Error: Python environment not ready')
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
        embody_pyenv.install_dependencies (pre-resolved on the main thread),
        wires sys.path, and warms the MCP import gate; its log lines are
        captured and replayed on the main thread by _pollBootstrap, which
        also owns the main-thread-only PATH/DLL linking epilogue.
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
        # Pure module functions, resolved on the MAIN thread (mod.* is a
        # TD lookup, illegal from the worker); the EmbodyExt facades would
        # re-resolve mod at call time (extracted to embody_pyenv 2026-08-19).
        pyenv = mod.embody_pyenv
        install_dependencies = pyenv.install_dependencies
        wire_python_paths = pyenv.wire_python_paths
        import_gate_check = pyenv.import_gate_check

        def worker():
            msgs = []
            gate_ok = False
            gate_msg = ''
            try:
                ok = install_dependencies(
                    spec, log=lambda m, lvl='INFO': msgs.append((lvl, m)))
            except BaseException as e:
                ok = False
                msgs.append(('ERROR', f'Dependency install crashed: {e}'))
            if ok:
                try:
                    if wire_python_paths(
                            spec,
                            log=lambda m, lvl='INFO': msgs.append((lvl, m))):
                        # site_packages arms the stale-interpreter
                        # (restart-required) detection after an upgrade
                        # install replaced the packages on disk.
                        gate_ok, gate_msg = import_gate_check(
                            spec['site_packages'])
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

        # Perform Mode entered while deps installed: same reasoning as
        # _pollImportGate -- Stop() cannot catch a start that has not bound
        # yet, so refuse to finish it here. Status stays 'Perform Mode'.
        if self._performModeActive():
            self._log('Perform Mode entered during dependency install '
                      '-- not starting.', 'DEBUG')
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
            # See _pollImportGate: restart-required refusals get an explicit
            # status; all 'Error...' statuses idle the liveness watchdog.
            self.ownerComp.par.Envoystatus = (
                'Error: restart TouchDesigner to finish MCP upgrade'
                if 'restart TouchDesigner' in gate_msg
                else 'Error: Python environment not ready')
            self._log(
                op.Embody.ext.Embody._importGateFailureMessage(
                    spec['site_packages'], gate_msg),
                'ERROR',
            )
            return
        sys._envoy_import_gate_ok = True
        # Main-thread epilogue for the worker's install: PATH/DLL/
        # VIRTUAL_ENV linking (never on the worker -- os.environ writes
        # off-main are the setenv-corruption class), with the retained
        # DLL handle invalidated first when the venv was rebuilt. Then
        # re-arm the non-gating extras reconcile: a --clear rebuild wiped
        # user extras with the venv.
        try:
            Embody = op.Embody.ext.Embody
            if spec.get('recreate_venv'):
                mod.embody_pyenv.unlink_dll_dir(spec['venv_dir'])
            Embody._linkEnv(spec)
            Embody._scheduleExtrasApply(delay_frames=1)
            # startup=True: this poll can resolve during a project OPEN
            # (fresh clone / version bump) before _continueStart arms
            # _startup_config_pass -- an Advanced-mode modal here would
            # block the frame loop (review 2026-08-20).
            Embody._ensurePyEnvContext(startup=True)
        except Exception:
            pass
        self._continueStart(git_root)

    @staticmethod
    def _shouldConfigureAIClient(client) -> bool:
        """Only the explicit ``none`` token selects internal-only startup."""
        return str(client or '').strip().lower() != 'none'

    def _continueStart(self, git_root) -> None:
        """Finish Envoy startup once the Python environment is confirmed ready.

        Runs on the main thread -- either inline from Start() after the session
        import-gate flag is already warm, from _pollImportGate(), or from
        _pollBootstrap() after a background dependency install. Allocates the
        port and spawns the server worker via the Thread Manager. MCP / git
        client config is written only when Aiclient is not ``none``; Convoy-only
        mode uses the same loopback command substrate without configuring or
        launching an AI coding client.
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
        sys._envoy_server_gen = self._server_gen
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
                  self.shutdown_event, startup_event, gen),
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
        # escalates on timeout/failure. When an AI client is selected its config
        # is written below; Convoy-only startup deliberately skips that work.
        self._startup_deadline = time.time() + 10.0
        run(f"op({self.ownerComp.path!r}).ext.Envoy._pollStartup({gen})",
            fromOP=self.ownerComp, delayFrames=6)

        # Auto-configure project files only for a selected AI client. Each step
        # is independent -- one failure must not block the others. MCP + AI
        # config stays co-located, honoring Aiprojectroot.
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
            # Cache the repo root for WORKER-side features (durable worktree
            # claims, preflight_landing) -- workers must never touch TD
            # objects, so resolve it here on the main thread once.
            try:
                sys._envoy_repo_root = str(target_dir) if target_dir else None
            except Exception:
                sys._envoy_repo_root = None
            try:
                configure_client = self._shouldConfigureAIClient(
                    self.ownerComp.par.Aiclient.eval())
            except Exception:
                configure_client = True
            if configure_client:
                self._configureMCPClient(port, target_dir=target_dir)
                try:
                    Embody._upgradeEnvoy()
                except Exception as e:
                    self._log(f'Could not auto-configure AI client files: {e}', 'WARNING')

                # Git config: only when a git repo exists. Always lives at the
                # git root regardless of Aiprojectroot -- .gitignore and
                # .gitattributes are git's files, not Embody's.
                if git_root != 'no-git':
                    from pathlib import Path
                    git_path = Path(git_root)
                    self._configureGitignore(git_path)
                    self._configureGitattributes(git_path)
            else:
                self._log(
                    'Convoy-only Envoy start: AI client configuration skipped',
                    'DEBUG')
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
            if self._performModeActive():
                # The worker bound while Perform Mode was entering:
                # _enterPerformMode's Stop() no-op'd (envoy_running was still
                # False for the whole startup window), so tear the newborn
                # server down here instead of declaring 'Running' over the
                # 'Perform Mode' readout and leaving it up all show. The
                # worker's exit callback sees Perform and skips the restart.
                # After Perform exits, recovery is the watchdog's revive
                # (~8s): _enterPerformMode snapshotted envoy_was_running as
                # False (the bind had not confirmed), so the exit path
                # schedules no Start of its own.
                self._starting = False
                # The bind DID confirm -- keep the healthy-port proof: drop
                # any blacklist entry a stale late error callback left, same
                # as the declare-Running branch below (round-2 panel note).
                bad = getattr(sys, '_envoy_bad_bind_ports', None)
                if bad:
                    bad.pop(self._runtime_port, None)
                try:
                    self.shutdown_event.set()
                except Exception:
                    pass
                self._log(
                    'Perform Mode entered during startup -- shutting the '
                    'just-bound server down instead of declaring Running',
                    'WARNING')
                return
            # Confirmed bound + serving.
            self._starting = False
            # A real bind proves the port is healthy -- drop any blacklist
            # entry a stale late-arriving error callback may have left for it
            # (that callback can't tell an old generation's port from ours).
            bad = getattr(sys, '_envoy_bad_bind_ports', None)
            if bad:
                bad.pop(self._runtime_port, None)
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
        """Self-healing liveness loop, one per extension instance.

        Armed from __init__ (not Start) so it survives a reinit whose
        post-reinit auto-start never completes. Probes the real socket
        and revives enabled-but-down Envoy; bridges reconnect on their
        own. Dies ONLY when a reinit replaces the instance -- not on
        generation bumps or disable (ticks idle so re-enable resumes
        self-healing).
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
            self._healStrandedTestStatus()
        except Exception:
            pass

        try:
            enabled = bool(self.ownerComp.par.Envoyenable.eval())
            status = str(self.ownerComp.par.Envoystatus.eval())
            # The SOCKET is the source of truth -- keying off _starting /
            # _init_complete wedged a dead server forever. Idle cases:
            # disabled, Perform Mode (enabled-but-dead is EXPECTED during
            # a show; the watchdog once revived 4-12s into every
            # performance), one-time deps install, explicit Start Error.
            # Gate on the live par, never the status string.
            performing = self._performModeActive()
            installing = status.startswith('Installing')
            # 'Preparing' is the fast-path import gate warming the MCP Python
            # stack on a worker thread -- a HEALTHY in-flight startup with no
            # socket bound yet. Classifying it as settled probed dead and
            # revived a cold first open ~8s in (issue #60 follow-up: revive
            # 7s after launch while the gate was still importing).
            # Transitional gives it the same ~24s stuck-grace as the other
            # startup states, so a slow import is left alone and an orphaned
            # gate (reinit mid-warmup, whose stale-instance poll exits
            # without finishing the start) still self-heals below.
            transitional = status.startswith(
                ('Starting', 'Restarting', 'Reviving', 'Preparing'))
            if not enabled or performing or installing or status.startswith('Error'):
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
        """Fast 127.0.0.1 connect to the runtime port. True iff a listener answers.

        Connection refused / timeout -> dead. A LIVE listener completes the
        TCP handshake at kernel level in ~1ms regardless of app load, so the
        0.25s timeout never false-negatives a healthy server. Do NOT assume a
        DEAD port refuses instantly: on some Windows hosts a firewall/WFP
        layer stealth-drops loopback SYNs to closed ports and the refusal
        takes ~2s (issue #57 environment) -- the 0.25s timeout is what CAPS
        the main-thread stall in that case, so keep it short. Unknown port
        -> True, so we never restart on missing info.
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

    def _performModeActive(self) -> bool:
        """True while Embody's Perform Mode is suspending Envoy.

        Single authority: Embody's narrow ``_envoyPerformMode`` property. It
        normally follows the live Performmode par, but a valid Convoy wake
        lease may resume Envoy while every unrelated Embody Perform guard
        remains active. Never key off the status string: Stop(), the hooks,
        and the watchdog itself all overwrite status text. Errors read as
        False so a broken Embody ext reference can never disable self-healing.
        """
        try:
            return bool(self.ownerComp.ext.Embody._envoyPerformMode)
        except Exception:
            return False

    def _reviveDeadServer(self, was_running: bool) -> None:
        """Socket dead while enabled, no thread-exit callback fired.

        Tear down and rebind after a short delay (keeps the port stable
        instead of drifting +1 and stranding bridges). A save reinit
        storm arms many ticks that all come due in one frame (18-21x
        revive spam) -- collapsed to ONE revive per frame cooldown; a
        genuine outage (>=~8s between dead ticks) still revives. Dedup
        lives here, in the stable fire-frame, because the per-reinit
        generation counter is unreliable during the storm.
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
        # Never shoot down a bind attempt still inside its startup window:
        # this path clears _starting and signals the CURRENT worker's
        # shutdown event, which kills a healthy just-started server when it
        # races a live start (one arm of the 2026-07-15 restart storm).
        # A genuinely stuck start is still handled: once _startup_deadline
        # passes, _pollStartup routes it to the error path, and this revive
        # proceeds on a later tick.
        if self._starting and time.time() < getattr(self, '_startup_deadline', 0.0):
            self._log(
                'Watchdog: revive skipped -- a start is in flight within its '
                'startup window', 'DEBUG')
            return
        self._last_revive_time = time.monotonic()
        port = getattr(self, '_runtime_port', None)
        self._log(
            f'Watchdog: MCP socket on port {port} unreachable while enabled '
            f'(running={was_running}) -- reviving server', 'WARNING')
        # Bump the generation first so the old worker's exit callbacks (and any
        # pending poll/watchdog) are treated as stale -- a single clean restart,
        # not ours plus a _scheduleRestart racing each other.
        self._server_gen += 1
        sys._envoy_server_gen = self._server_gen
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

    def RuntimePort(self) -> Optional[int]:
        """The confirmed-bound loopback port, or None.

        The public read of what _pollStartup CONFIRMED: a port is reported
        only once a real bind happened (envoy_running) and no start is in
        flight. Exists so ConvoyExt never has to parse Envoystatus for a
        'port (\\d+)' -- the exact anti-pattern A-9 removed.
        """
        if self._starting or not self.ownerComp.fetch('envoy_running', False):
            return None
        return getattr(self, '_runtime_port', None)

    # === Thread Manager Target (runs in worker thread) ===

    @staticmethod
    def _runServer(port: int, request_queue: Queue, response_queue: Queue,
                   shutdown_event: Event, startup_event: Optional[Event] = None,
                   gen: int = 0):
        """
        Target function for TDTask - runs MCP server in worker thread.
        IMPORTANT: No TouchDesigner calls allowed here! This is static.

        Returns an exit-context dict consumed by _onServerSuccess (the
        SuccessHook receives the target's return value). `preset_shutdown`
        is the restart-storm smoking gun: True means some kicker signaled
        this worker's shutdown event BEFORE it even started serving -- the
        dead-on-arrival signature of the 2026-07-15 loop.
        """
        def add_to_refresh(data):
            """Add data to the request queue (polled by RefreshHook on main thread)"""
            request_queue.put(data)

        preset = shutdown_event.is_set()
        t0 = time.monotonic()
        try:
            server = EnvoyMCPServer(
                request_queue=None,  # Not used, we use InfoQueue
                response_queue=response_queue,
                add_to_refresh_queue=add_to_refresh,
                port=port,
                shutdown_event=shutdown_event,
                startup_event=startup_event,
                gen=gen,
            )
            server.run()
            ctx = {
                'preset_shutdown': preset,
                'shutdown_set': shutdown_event.is_set(),
                'bound': bool(startup_event is not None
                              and startup_event.is_set()),
                'lifetime_s': round(time.monotonic() - t0, 3),
                'gen': gen,
            }
            # Hand the exit context to _onServerSuccess via sys: the Thread
            # Manager's SuccessHook does NOT deliver the target's return
            # value (verified empirically 2026-07-16 -- the hook fires with
            # no args). sys attrs are the established cross-thread channel
            # here (same pattern as sys._envoy_uvi_server). Plain-data write
            # from the worker; read + cleared on the main thread.
            sys._envoy_exit_context = ctx
            return ctx
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
                         'batch_operations', 'save_project',
                         'update_embody')

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
        if operation == 'save_project':
            # A save re-exports every externalized file -- project-global.
            return ['project:save'], 'save_project'
        if operation == 'update_embody':
            # Replaces the Embody component in place -- project-global.
            return ['project:update'], 'update_embody'
        if operation == 'batch_operations':
            gated = []
            subs = params.get('operations') or []
            # EVERY sub-operation is evaluated, never a prefix. The old
            # code sliced [:32], so a destructive sub-op at index 33 was
            # never gated -- a bypass costing an attacker 32 padding
            # entries, and a silent half-evaluation for an honest caller
            # with a long batch. A batch too large to evaluate must be
            # REFUSED (see _BATCH_GATE_LIMIT below), never partly checked.
            for sub in subs:
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

    # A batch longer than this is refused outright rather than evaluated:
    # the gate must never make a safety decision on a prefix of the work
    # it is being asked to authorize. Generous enough that no honest
    # batch hits it (the tool's own guidance is to group a handful of
    # repetitive calls), small enough that evaluation stays cheap.
    _BATCH_GATE_LIMIT = 512

    def _gateVerdict(self, sid, operation, params):
        """The destructive gate's verdict, with a FAIL-CLOSED policy.

        Returns a refusal dict to send back, or None to proceed.

        This wrapper exists because the policy is the whole point and it
        used to live inline in the main-thread pump as a bare
        `except Exception: gate = None` -- and None means PROCEED, so any
        error inside the gate silently authorized the very operation the
        gate exists to stop, with no log line. A gate that cannot reach a
        verdict has not granted permission.

        The fail-closed rule applies to GATED operations only. An
        internal gate error must not break unrelated tools that were
        never the gate's business.
        """
        try:
            return self._checkDestructiveGate(sid, operation, params)
        except Exception as e:
            if operation in self._GATED_OPERATIONS:
                self._log(
                    f'MULTI-SESSION GATE: refusing {operation} -- the gate '
                    f'itself failed ({type(e).__name__}: {e}). Failing '
                    f'closed.', 'ERROR')
                return {
                    'error': f'MULTI-SESSION GATE: {operation} refused -- '
                             f'the safety gate could not reach a verdict '
                             f'({type(e).__name__}: {e}). This fails CLOSED '
                             f'by design. Retry, or pass override=True if '
                             f'you are certain no peer is working in this '
                             f'scope.',
                    'gate_error': f'{type(e).__name__}: {e}'}
            self._log(
                f'Destructive-gate check errored for non-gated {operation} '
                f'(proceeding): {type(e).__name__}: {e}', 'WARNING')
            return None

    def _checkDestructiveGate(self, sid, operation, params):
        """Refuse a destructive operation when a LIVE peer session claimed
        an overlapping scope or wrote it within the conflict window, unless
        override=True. Returns the error dict to send back, or None to
        proceed. Advisory-first design: everything else only warns."""
        if operation not in self._GATED_OPERATIONS:
            return None
        if (params or {}).get('override'):
            return None
        # A batch the gate cannot fully evaluate is refused, not
        # half-checked: authorizing a prefix of the requested work is
        # exactly the bypass the old [:32] slice created.
        if operation == 'batch_operations':
            subs = (params or {}).get('operations') or []
            if len(subs) > self._BATCH_GATE_LIMIT:
                self._log(
                    f'MULTI-SESSION GATE: refused a batch of {len(subs)} '
                    f'operations -- above the {self._BATCH_GATE_LIMIT} the '
                    f'gate will evaluate', 'WARNING')
                return {'error': f'MULTI-SESSION GATE: batch_operations '
                                 f'refused -- {len(subs)} sub-operations '
                                 f'exceeds the {self._BATCH_GATE_LIMIT} the '
                                 f'safety gate evaluates. Split the batch. '
                                 f'(The gate never authorizes a prefix of '
                                 f'the work it was asked to check.)'}
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

    def _attachRecoveryHints(self, result):
        """On an error envelope, attach curated next-step guidance keyed off
        the message so the agent recovers instead of retrying blindly. Never
        raises -- an ergonomics layer must not be able to break a response."""
        try:
            if not isinstance(result, dict):
                return
            message = result.get('error')
            if not isinstance(message, str) or 'recovery_hints' in result:
                return
            hints = _recovery_hints_for(message)
            if hints:
                result['recovery_hints'] = hints
        except Exception:
            pass

    def _sampleProjectFps(self):
        """Current fps from Embody's Perform CHOP, or None.

        Reads `_envoy_perform` ONLY if it already exists -- an unrelated
        write must never create a monitor operator as a side effect (the
        get_project_performance tool documents first-call creation; this
        footer deliberately opts out of it). Never raises."""
        try:
            perform = self.ownerComp.op('_envoy_perform')
            if not perform:
                return None
            channel = perform.chan('fps')
            if channel is None:
                return None
            return float(channel.eval())
        except Exception:
            return None

    def _attachEffects(self, result, operation, sid=None, scopes=None):
        """After a MUTATING call, ride back what THIS write just did to the
        project: operator errors/warnings that did not exist before it, and a
        meaningful frame-rate drop. Diffed against a per-session baseline so
        pre-existing damage stays silent.

        Never raises -- an ergonomics layer must not be able to break a
        response (same contract as _attachRecoveryHints)."""
        try:
            if not isinstance(result, dict) or '_effects' in result:
                return
            if operation not in _WRITE_OPERATIONS:
                return
            key = sid or '_anon'
            state = self._effects_state.get(key)
            first_write = state is None
            if first_write:
                state = {}
                self._effects_state[key] = state
            if len(self._effects_state) > 32:
                # Crude cap: a long-lived TD session accumulating many
                # short-lived sids must not grow this unbounded.
                self._effects_state.clear()
                self._effects_state[key] = state

            effects = {}

            # --- newly appeared errors / warnings ---
            snapshot = None
            if getattr(self, '_effects_error_scan_ok', True):
                started = time.time()
                snapshot = mod.envoy_read.get_op_errors(
                    self, '/', True, include_shaders=False)
                elapsed = time.time() - started
                if elapsed > _EFFECTS_SCAN_BUDGET_S:
                    self._effects_error_scan_ok = False
                    self._log(
                        'Write-effect error scan took %.2fs (budget %.2fs) -- '
                        'disabling it for this session so it cannot tax every '
                        'write. Use get_op_errors on demand instead.'
                        % (elapsed, _EFFECTS_SCAN_BUDGET_S), 'WARNING')
            if isinstance(snapshot, dict) and 'error' not in snapshot:
                errors, error_total, error_paths = _new_error_entries(
                    None if first_write else state.get('errors'),
                    snapshot.get('errors'), _EFFECTS_ERROR_CAP)
                warnings, warning_total, warning_paths = _new_error_entries(
                    None if first_write else state.get('warnings'),
                    snapshot.get('warnings'), _EFFECTS_WARNING_CAP)
                state['errors'] = error_paths
                state['warnings'] = warning_paths
                if errors:
                    effects['new_errors'] = errors
                    effects['new_errors_total'] = error_total
                if warnings:
                    effects['new_warnings'] = warnings
                    effects['new_warnings_total'] = warning_total
                # GLSL compile failures ride the same differ and cap: they
                # already share errors[]' entry shape. Kept OUT of errors[]
                # so errorCount/hasErrors keep meaning what they meant. The
                # extra dock walk measured 2.7ms over 1920 ops (2026-08-21),
                # ~1% of _EFFECTS_SCAN_BUDGET_S, so it rides the existing
                # budget rather than earning a second latch.
                # Shader pass scoped to the ops this write actually
                # touched. Never project-wide: reading an Info DAT cooks it
                # (3.36s cold across the project, 2026-08-21).
                touched = []
                for scope in (scopes or []):
                    if not isinstance(scope, str) or not scope.startswith('/'):
                        continue
                    target = mod.envoy_read.resolve_op(self, scope)
                    if target is None:
                        continue
                    # Editing shader SOURCE targets the docked pixel/compute
                    # DAT, which owns no Info DAT -- the diagnostics live on
                    # its host. Walk up so set_dat_content('/x/glsl_a_pixel')
                    # still reports /x/glsl_a's compile errors.
                    for probe in (target, getattr(target, 'dock', None)):
                        if probe is None:
                            continue
                        touched.extend(
                            mod.envoy_read.shader_errors(self, probe, True))
                shaders, shader_total, shader_paths = _new_error_entries(
                    None if first_write else state.get('shaders'),
                    touched, _EFFECTS_ERROR_CAP)
                state['shaders'] = shader_paths
                if shaders:
                    effects['new_shader_errors'] = shaders
                    effects['new_shader_errors_total'] = shader_total

            # --- meaningful frame-rate drop ---
            fps = self._sampleProjectFps()
            if fps is not None:
                if not first_write:
                    regression = _fps_regression(state.get('fps'), fps)
                    if regression:
                        effects['fps'] = regression
                state['fps'] = fps

            if effects:
                effects['hint'] = (
                    'These appeared after YOUR last write. Inspect with '
                    'get_op_errors on the listed paths (or '
                    'get_project_performance for the fps drop) and fix '
                    'before building further.')
                result['_effects'] = effects
        except Exception:
            pass

    def _send_response(self, request_id, result, sid=None, operation=None,
                       scopes=None):
        """Send a response back to the worker thread (token-lean log piggyback).

        `operation` is optional so nothing else has to change: pass it for a
        call that actually EXECUTED, and the write-effect footer runs; leave
        it None (e.g. for a refused multi-session gate, where no TD state
        moved) and only the existing attachments apply."""
        self._attachRecoveryHints(result)
        self._attachNotableLogs(result, sid)
        self._attachEffects(result, operation, sid, scopes)

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

        # Deliver worker-side buffered diagnostics (workers cannot print()
        # or _log() -- both touch main-thread TD objects).
        while _WORKER_LOG_LINES:
            try:
                level, message = _WORKER_LOG_LINES.popleft()
            except IndexError:
                break
            self._log(message, level)

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
            #
            # FAIL CLOSED for gated operations. This used to be a bare
            # `except Exception: gate = None`, and `None` means PROCEED --
            # so any error inside the gate silently authorized the very
            # operation the gate exists to stop, with no log line at all.
            # A gate that cannot reach a verdict has not granted
            # permission. Non-gated operations are unaffected: they were
            # never the gate's business, so an internal error there must
            # not break unrelated tools.
            gate = self._gateVerdict(sid, operation, params)
            if gate is not None:
                if isinstance(request_id, int) and request_id >= 0:
                    # Refused before execution -- no TD state moved, so the
                    # write-effect footer must not run (operation omitted).
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

            self._send_response(request_id, result, sid, operation=operation,
                                scopes=scopes)

        # Live build visualization (opt-in): camera follow + node pulse + the
        # dancing builder-bot. Runs every frame AFTER the drain loop. Wrapped so
        # visualization can NEVER break the refresh loop.
        try:
            self._vizTick()
        except Exception:
            pass

    def _onServerSuccess(self, returnValue=None):
        """SuccessHook - Called when the thread task completes successfully"""
        detail = ''
        if not isinstance(returnValue, dict):
            # ThreadManager's SuccessHook fires without the target's return
            # value; the worker stashes its exit context on sys instead.
            returnValue = getattr(sys, '_envoy_exit_context', None)
            sys._envoy_exit_context = None
        if isinstance(returnValue, dict):
            detail = (f" (gen {returnValue.get('gen')}, "
                      f"lifetime {returnValue.get('lifetime_s')}s, "
                      f"bound: {returnValue.get('bound')})")
            if returnValue.get('gen') != self._server_gen:
                # A stale worker's late stash (or an unclaimed slot from a
                # gen-staled exit) -- annotate rather than misattribute, and
                # never raise the storm warning for it.
                detail += ' [stale exit context]'
            elif returnValue.get('preset_shutdown'):
                # The worker found its own shutdown event ALREADY SET before
                # serving a single request: a stale kicker (queued restart,
                # revive, force-close) signaled the newborn. This is the
                # dead-on-arrival signature of the 2026-07-15 restart storm
                # -- surface it loudly instead of looping silently.
                self._log(
                    'Server worker was dead-on-arrival: its shutdown event '
                    'was set before startup. A stale restart/revive/force-'
                    'close signaled the new server (restart-storm signature).',
                    'WARNING')
        self._log(f'Server thread exited{detail}')
        self.ownerComp.store('envoy_running', False)
        self.current_task = None
        self._starting = False
        # _performModeActive is the single Perform authority (errors read as
        # False, so a broken ext ref degrades to a restart -- self-healing
        # preserved -- instead of raising out of the hook and scheduling
        # nothing).
        if self.ownerComp.par.Envoyenable.eval() and not self._performModeActive():
            self._scheduleRestart('Server exited unexpectedly')
        # If Envoyenable is already off, Stop() set the status -- don't overwrite

    def _onServerError(self, error):
        """ExceptHook - Called when the thread task errors"""
        self._log(f'Server error: {error}', 'ERROR')
        self.ownerComp.store('envoy_running', False)
        self.current_task = None
        self._starting = False
        # Worker died without ever confirming a bind -> blacklist its port so
        # the restart scans PAST it instead of re-picking the same poisoned
        # port every attempt. Non-bind pre-startup failures land here too;
        # blacklisting their port is harmless (entry expires after
        # _BIND_FAIL_TTL_SECONDS, and a confirmed bind clears it).
        bound = (self._startup_event is not None
                 and self._startup_event.is_set())
        port = getattr(self, '_runtime_port', None)
        if not bound and port:
            bad = getattr(sys, '_envoy_bad_bind_ports', {})
            bad[port] = time.time()
            sys._envoy_bad_bind_ports = bad
        if self.ownerComp.par.Envoyenable.eval() and not self._performModeActive():
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
        # Generation-stamped landing point instead of a bare Start(): every
        # death queues a restart, so a long storm stacks HUNDREDS of pending
        # run() calls that all eventually fire (observed: 475 on 2026-07-15,
        # still spawning ~2s apart while 'retry in 60s' was logged). Each
        # fire re-checks the generation; any start/revive that happened in
        # the meantime supersedes it into a no-op.
        run(f"op('{self.ownerComp.path}').ext.Envoy._restartFire({self._server_gen})",
            fromOP=self.ownerComp, delayMilliSeconds=int(delay * 1000))

    def _restartFire(self, expected_gen: int) -> None:
        """Deferred landing point for _scheduleRestart's queued restart.

        No-ops when superseded: a newer Start()/revive bumped the generation,
        or the server is already up/starting (Start()'s own guards cover the
        rest, including the Envoyenable master switch)."""
        if expected_gen != self._server_gen:
            return  # superseded by a newer start/revive -- stale queued restart
        self.Start()

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
            'get_chop_data': self._get_chop_data,
            'get_pop_data': self._get_pop_data,
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
            'get_focus': self._get_focus,
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
            'save_project': self._save_project,
            'update_embody': self._update_embody,
            # Dedicated Convoy host lifecycle leg (session-gated wrappers).
            'convoy_lifecycle_state': self._convoy_lifecycle_state,
            'convoy_lifecycle_quit': self._convoy_lifecycle_quit,
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
                elif operation in self._COARSE_CHECKPOINT_OPS:
                    # Same ordering argument as the delete above, minus the path:
                    # arbitrary code can crash TD, and a root queued by an earlier
                    # tool sits unwritten for up to the settle window. Flush what
                    # is ALREADY known dirty first. It does not sweep for new dirt
                    # (that is the coarse post-arm below, debounced), so when
                    # nothing is queued -- the steady state -- this costs an
                    # empty-set check.
                    try:
                        op.Embody.ext.Embody.FlushPendingCheckpoints()
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
        if operation not in self._UNDOABLE_OPS:
            return False
        if self._undo_active:
            # Self-heal a LATCHED guard: if a begin/end pair is severed
            # (an extension reinit or exception between begin and the
            # finally), the flag would otherwise stay True forever and
            # silently disable undo blocks for the whole session
            # (observed 2026-07-25 after landing an EnvoyExt edit while
            # operations were dispatching). A genuinely-nested caller
            # (batch sub-op) hits the young-flag path and is refused as
            # before; a stale flag older than 60s is reclaimed loudly.
            if time.time() - self._undo_active_since < 60:
                return False
            self._log('Undo-block guard latched >60s -- self-healing (a '
                      'begin/end pair was severed, likely by a reinit)',
                      'WARNING')
            try:
                ui.undo.endBlock()
            except Exception:
                pass
            self._undo_active = False
        try:
            ui.undo.startBlock(f'Envoy {operation}')
            self._undo_active = True
            self._undo_active_since = time.time()
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
    # below are delegating stubs, and only the ones with live callers remain
    # (execute.py _vizCleanup, _onRefresh ticks, the dispatch chokepoint) --
    # 28 unreferenced pass-throughs were dropped 2026-08-21; call
    # mod.envoy_viz directly rather than re-adding one.
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
    # exec_op_method are excluded from THIS set because it is the PATH-RESOLVING
    # one and neither can name what it touched -- not because they are unwatched.
    # They arm COARSELY instead (_noteCheckpointActivity), which is what stopped a
    # whole agent session working through them from checkpointing nothing at all.
    _CHECKPOINT_MUTATING_OPS = frozenset({
        'create_op', 'delete_op', 'set_parameter', 'connect_ops', 'disconnect_op',
        'copy_op', 'rename_op', 'set_op_flags', 'set_op_position', 'layout_children',
        'set_dat_content', 'edit_dat_content', 'create_annotation', 'set_annotation',
        'create_extension', 'import_network', 'externalize_op', 'save_externalization',
        'remove_externalization_tag',
    })

    # The two tools that run arbitrary code. They arm the COARSE sweep (which
    # root changed is discovered at the settle) and pre-flush whatever is already
    # queued, because either can crash TD and take an unwritten checkpoint with
    # it. Kept as a set so the two paths that must agree -- the arm and the
    # pre-flush -- cannot drift apart into "one tool is guarded, the other is not".
    _COARSE_CHECKPOINT_OPS = frozenset({'execute_python', 'exec_op_method'})

    def _noteCheckpointActivity(self, operation: str, params: dict, result) -> None:
        """Arm the auto-save touched-set off the single MCP chokepoint. Best-effort,
        never raises -- a failure here must never affect the tool response."""
        try:
            if operation in self._COARSE_CHECKPOINT_OPS:
                # No path to resolve -- arbitrary code touches whatever it likes,
                # which is why these tools used to arm nothing at all and left a
                # whole agent session uncheckpointed. exec_op_method belongs here
                # for the same reason and not the obvious one: it HAS an op_path,
                # but the method it calls is arbitrary, so the op named is where
                # the call lands, not the bound on what it changed. Arm coarsely;
                # the drain discovers which roots actually changed, once, after
                # the burst.
                op.Embody.ext.Embody.NoteCoarseCheckpointTouch()
                return
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

    def _userTookOver(self, pane) -> bool:
        """True while the user has navigated the follow pane away -- see envoy_viz."""
        return mod.envoy_viz.userTookOver(self, pane)

    def _glideStep(self, pane, target: 'OP') -> None:
        """One eased frame of the camera glide toward the op -- see envoy_viz."""
        return mod.envoy_viz.glideStep(self, pane, target)

    def _placeBot(self, net: 'COMP', target: 'OP', now: float) -> None:
        """Bring Embot to stand on the active op -- see envoy_viz."""
        return mod.envoy_viz.placeBot(self, net, target, now)

    def _ensureBot(self, net: 'COMP') -> bool:
        """Ensure Embot is present/assembling in a network -- see envoy_viz."""
        return mod.envoy_viz.ensureBot(self, net)

    def _assembleTick(self) -> None:
        """Drive Embot's spread assembly per frame -- see envoy_viz."""
        return mod.envoy_viz.assembleTick(self)

    def _startEntrance(self) -> None:
        """Fire Embot's swoop from staging onto his op -- see envoy_viz."""
        return mod.envoy_viz.startEntrance(self)

    def _botDance(self, now: float) -> None:
        """Animate the figure: hop, hover, gesture, colour -- see envoy_viz."""
        return mod.envoy_viz.botDance(self, now)

    def _botUnsafeNet(self, net: 'COMP') -> bool:
        """True if a bot must not be created in the net -- see envoy_viz."""
        return mod.envoy_viz.botUnsafeNet(self, net)

    def _vizCleanup(self) -> None:
        """Retire all live visualization artifacts -- see envoy_viz."""
        return mod.envoy_viz.vizCleanup(self)

    def _purgeVizArtifacts(self, root=None) -> int:
        """Structural sweep of loose Embot parts (save path) -- see envoy_viz."""
        return mod.envoy_viz.purgeVizArtifacts(self, root)

    def _vizRetireForWrite(self, path: str) -> bool:
        """Retire Embot if he is inside a COMP about to be written -- see envoy_viz."""
        return mod.envoy_viz.vizRetireForWrite(self, path)

    def _get_logs(self, level=None, count=50, since_id=None, source=None):
        """Get filtered log entries from Embody's ring buffer -- see envoy_read."""
        return mod.envoy_read.get_logs(self, level, count, since_id, source)

    # --- Testing ---

    def _run_tests(self, suite_name=None, test_name=None, background=False,
                   idempotency_key=None):
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
        if background:
            # Job mode: start the deferred run, park progress in a disk
            # record, return the handle. No transport Event involved -- a
            # server restart mid-run cannot sever anything.
            return self._startTestsJob(suite_name, test_name,
                                       idempotency_key=idempotency_key)

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
        runner = getattr(test_comp.ext, 'TestRunnerExt', None)
        if runner is not None and getattr(runner, '_running', False):
            # Overlapping run_tests calls clobbered the saved Status
            # ('Testing' captured as the "prior" value and restored after the
            # run -> Status stuck at 'Testing' forever) and overwrote
            # sys._envoy_pending_test, leaving the first caller's worker
            # blocked until the transport timeout. Refuse the second run.
            self._signalTestError(pending, 'A test run is already in progress')
            return None
        try:
            # Suppress Embody's Update/Refresh cycle during tests to
            # prevent extension reinit from TDN re-exports triggered by
            # test-created operators making COMPs structurally dirty.
            # The prior Status is kept in COMP storage, not an instance
            # attribute: an extension reinit mid-run wipes the attribute and
            # left Status stuck at 'Testing'. Never capture the literal
            # 'Testing' (an interrupted run's leftover) -- the value already
            # in storage is the true prior.
            embody = op.Embody
            prior = embody.par.Status.eval()
            if prior != 'Testing':
                embody.store('_test_saved_status', prior)
            embody.par.Status = 'Testing'
            test_comp.RunTestsDeferredPerTest(
                suite_name=suite_name, test_name=test_name)
            self._schedulePollTestCompletion()
            return None  # Deferred -- worker thread waits on sys._envoy_pending_test['event']
        except Exception as e:
            self._restoreStatusAfterTests()
            self._signalTestError(pending, f'Test run failed: {e}')
            return None

    def _testRunnerLive(self) -> bool:
        """Whether an in-TD test run is executing right now.

        Unknown reads as LIVE: the caller (the stranded-status heal)
        must never yank Status out from under a run it merely failed to
        see.
        """
        try:
            test_comp = op.unit_tests
            runner = (getattr(test_comp.ext, 'TestRunnerExt', None)
                      if test_comp and test_comp.extensionsReady
                      else None)
            return bool(getattr(runner, '_running', False))
        except Exception:
            return True

    def _healStrandedTestStatus(self) -> None:
        """Watchdog-tick check: restore a stranded 'Testing' Status.

        A run_tests whose completion poll died (extension reinit
        mid-run) or whose run OUTLIVED the ~10 min poll window leaves
        Embody Status at 'Testing' forever -- the only thing that
        restored it was the now-dead poll chain, and the give-up arm
        correctly refuses to restore while the run is still live
        (field, 2026-08-12: two full runs stranded it in one day). The
        stranded signature is precise: the saved-status storage exists
        while NO run is live -- a starting run sets storage and
        _running in the same call, so a tick cannot race it. Two
        consecutive ticks (~8s) of dwell as a belt anyway.
        """
        embody = op.Embody
        saved = embody.fetch('_test_saved_status', None, search=False)
        if (saved is not None and not self._testRunnerLive()
                and str(embody.par.Status.eval()) == 'Testing'):
            self._strandedTestTicks = getattr(
                self, '_strandedTestTicks', 0) + 1
            if self._strandedTestTicks >= 2:
                self._strandedTestTicks = 0
                self._log(
                    "Watchdog: Embody Status stranded at 'Testing' "
                    'with no test run live -- restoring the saved '
                    'status', 'WARNING')
                self._restoreStatusAfterTests()
        else:
            self._strandedTestTicks = 0

    def _restoreStatusAfterTests(self):
        """Re-enable Embody's Update cycle after tests complete.

        Storage-backed and idempotent: safe to call from any poll chain,
        including one whose pending handle was lost or cancelled.
        """
        embody = op.Embody
        saved = embody.fetch('_test_saved_status', None, search=False)
        if saved is None:
            # Legacy fallback: a pre-hardening save lived on the instance.
            saved = getattr(self, '_test_saved_status', None)
        if saved is not None:
            embody.par.Status = saved
        self._test_saved_status = None
        embody.unstore('_test_saved_status')

    def _signalTestError(self, pending, message):
        """Signal an error to the waiting worker thread via the test Event."""
        pending['holder']['result'] = {'error': message}
        pending['event'].set()
        sys._envoy_pending_test = None

    # --- Background jobs (main-thread side) -------------------------------

    def _startTestsJob(self, suite_name=None, test_name=None,
                       idempotency_key=None) -> dict:
        """Start a deferred test run tracked by a disk job record instead
        of a blocked transport. Main thread; mirrors _run_tests' guards."""
        # 16.5: idempotency lookup STRICTLY FIRST -- before the guards. A
        # retry with the same key reconciles to the original run's handle
        # rather than tripping the 'already in progress' guard (which
        # would hand a controller an error instead of its own job).
        if idempotency_key:
            try:
                prior = _job_for_key(idempotency_key,
                                     expected_kind='run_tests')
            except _IdemMarkerUnreadable as e:
                return {'error': 'Idempotency marker unreadable (%s) -- '
                                 'refusing to risk a duplicate run; clear '
                                 'it or retry.' % e}
            except _IdemKeyConflict as e:
                return {'error': 'idempotency_key is already bound to a '
                                 'different operation (%s) -- use a distinct '
                                 'key per operation.' % e}
            if prior is not None:
                return {'job_id': prior['id'],
                        'status': prior.get('status', 'running'),
                        'hint': 'Reconciled to the original run for this '
                                'idempotency_key (calls are idempotent).'}
        test_comp = op.unit_tests
        if not test_comp:
            return {'error': 'Test framework not found (op.unit_tests)'}
        if not test_comp.extensionsReady:
            return {'error': 'Test framework extension not ready'}
        runner = getattr(test_comp.ext, 'TestRunnerExt', None)
        if runner is not None and getattr(runner, '_running', False):
            return {'error': 'A test run is already in progress'}
        if self._activeSaveJob() is not None:
            return {'error': 'A save_project job is in flight -- a test run '
                             'starting inside the save\'s window would race '
                             'its extension reinit. Retry when the save '
                             'job finishes.'}
        job = _new_job('run_tests', {'suite_name': suite_name,
                                     'test_name': test_name},
                       idempotency_key=idempotency_key)
        _write_job(job)
        if _read_job(job['id']) is None:
            # No record can exist (repo root unresolved) -- an unpollable
            # handle would be a lie. The synchronous mode still works.
            return {'error': 'Job records unavailable (project root not '
                             'resolved yet) -- use the synchronous '
                             'run_tests, or retry shortly.'}
        # Record the key AFTER the job record is durable (read-back above),
        # so the marker can never name a job that was not persisted.
        if idempotency_key:
            _record_job_key(idempotency_key, job['id'])
        try:
            embody = op.Embody
            prior = embody.par.Status.eval()
            if prior != 'Testing':
                embody.store('_test_saved_status', prior)
            embody.par.Status = 'Testing'
            # Ownership stamp: the completion poll finalizes ONLY while it
            # still owns the run. If this run ends and another starts inside
            # one poll gap, the newer starter overwrites the stamp and the
            # orphaned poll closes its record as superseded instead of
            # filing the WRONG run's summary. Storage-backed so it survives
            # reinit; listed in SKIP_STORAGE_KEYS so it never reaches disk
            # exports.
            embody.store('_test_run_owner', job['id'])
            test_comp.RunTestsDeferredPerTest(
                suite_name=suite_name, test_name=test_name)
        except Exception as e:
            job['status'] = 'error'
            job['error'] = 'Test run failed to start: %s' % e
            job['finished'] = time.time()
            _write_job(job)
            self._restoreStatusAfterTests()
            return {'job_id': job['id'], 'status': 'error',
                    'error': job['error']}
        self._schedulePollTestJob(job['id'], 0)
        return {'job_id': job['id'], 'status': 'running',
                'hint': 'Poll get_job_status(job_id=...) for the summary; '
                        'results survive server restarts.'}

    def _activeSaveJob(self):
        """The in-flight save_project record, or None. Main thread; a
        'running' save older than 2 minutes is treated as dead (a save
        takes seconds; its record write is retried and reinit-proof)."""
        now = time.time()
        for record in _list_jobs(now):
            if (record.get('kind') == 'save_project'
                    and record.get('status') == 'running'
                    and (record.get('age_s') or 0) < 120):
                return record
        return None

    def _schedulePollTestJob(self, job_id, attempt):
        """String-form run() so the poll survives extension reinit (the
        live instance is resolved at fire time, same as the watchdog)."""
        run("o = op(%r)\nif o and o.valid: "
            "o.ext.Envoy._pollTestCompletionJob(%r, %d)"
            % (self.ownerComp.path, job_id, attempt),
            fromOP=self.ownerComp, delayFrames=30)

    def _pollTestCompletionJob(self, job_id, attempt=0):
        """Finish a BACKGROUND test run into its job record -- the
        transport-free twin of _pollTestCompletion. Bounded; a poll chain
        that dies anyway leaves a 'running' record that get_job_status
        flags stale."""
        # Ownership check FIRST: if a newer run (sync or background) took
        # the stamp, this poll must not file that run's summary under its
        # own job -- close as superseded and stand down.
        try:
            owner = op.Embody.fetch('_test_run_owner', None, search=False)
        except Exception:
            owner = None
        if owner is not None and owner != job_id:
            job = _read_job(job_id) or {'id': job_id, 'kind': 'run_tests'}
            job['status'] = 'error'
            job['error'] = ('Superseded: another test run started before '
                            'this one\'s summary was collected. Its own '
                            'record/results apply; this run\'s did land in '
                            'the test log under dev/logs.')
            job['finished'] = time.time()
            _write_job(job)
            return
        test_comp = op.unit_tests
        runner = (getattr(test_comp.ext, 'TestRunnerExt', None)
                  if test_comp and test_comp.extensionsReady else None)
        if runner is None or getattr(runner, '_running', False):
            if attempt < 1200:   # ~10 min of 30-frame polls at 60fps
                self._schedulePollTestJob(job_id, attempt + 1)
                return
            job = _read_job(job_id) or {'id': job_id, 'kind': 'run_tests'}
            job['status'] = 'error'
            job['error'] = 'Test run did not finish within the poll window'
            job['finished'] = time.time()
            _write_job(job)
            if runner is None or not getattr(runner, '_running', False):
                # Only restore when nothing is actually running -- yanking
                # Status mid-run re-enables the Update cycle the
                # suppression exists to hold off.
                self._restoreStatusAfterTests()
            return
        self._restoreStatusAfterTests()
        summary = runner._getSummary()
        # Token- and disk-lean: counts plus the non-PASS entries, failures
        # FIRST so a skip-heavy run can never crowd a real failure out of
        # the capped list.
        if isinstance(summary.get('results'), list):
            non_pass = [r for r in summary['results']
                        if r.get('status') != 'PASS']
            non_pass.sort(key=lambda r: 0 if r.get('status')
                          in ('FAIL', 'ERROR') else 1)
            summary['results'] = non_pass[:20]
        job = _read_job(job_id) or {'id': job_id, 'kind': 'run_tests'}
        job['status'] = 'done'
        job['finished'] = time.time()
        job['result'] = summary
        _write_job(job)
        try:
            op.Embody.unstore('_test_run_owner')
        except Exception:
            pass

    def _save_project(self, idempotency_key=None) -> dict:
        """Start a project save as a tracked job (main thread).

        The save itself runs a few frames later so this response reaches
        the client BEFORE the main thread blocks on the TDN strip/restore
        and the extension reinit that project.save() triggers. Refuses
        while a test run is active (a mid-run save bakes the runner's
        forced Filecleanup='delete' / Status='Testing' into the exported
        .tdn/.toe -- the exact incident class destructive-tests.md records
        -- and its strip/restore kills the deferred run). Idempotent
        against retries: an idempotency_key reconciles a redelivery to the
        original save regardless of age (16.5); even without one, a second
        call while a save job is in flight returns the EXISTING handle."""
        # 16.5: key lookup STRICTLY FIRST, before the guards -- an
        # age-independent dedupe that the keyless _activeSaveJob 120s
        # window (kept below as a second layer) cannot give.
        if idempotency_key:
            try:
                prior = _job_for_key(idempotency_key,
                                     expected_kind='save_project')
            except _IdemMarkerUnreadable as e:
                return {'error': 'Idempotency marker unreadable (%s) -- '
                                 'refusing to risk a duplicate save; clear '
                                 'it or retry.' % e}
            except _IdemKeyConflict as e:
                return {'error': 'idempotency_key is already bound to a '
                                 'different operation (%s) -- use a distinct '
                                 'key per operation.' % e}
            if prior is not None:
                return {'job_id': prior['id'],
                        'status': prior.get('status', 'running'),
                        'hint': 'Reconciled to the original save for this '
                                'idempotency_key (calls are idempotent).'}
        try:
            if op.Embody.ext.Embody._testRunnerActive():
                return {'error': 'A test run is active -- saving now would '
                                 'bake test-forced parameters into the '
                                 'export and kill the run. Wait for the '
                                 'run (or its job) to finish.'}
        except Exception:
            pass
        active = self._activeSaveJob()
        if active is not None:
            return {'job_id': active['id'], 'status': 'running',
                    'hint': 'A save is already in flight -- returning its '
                            'existing job handle (calls are idempotent).'}
        job = _new_job('save_project', {}, idempotency_key=idempotency_key)
        try:
            job['version_before'] = str(op.Embody.par.Version.eval())
        except Exception:
            pass
        _write_job(job)
        if _read_job(job['id']) is None:
            return {'error': 'Job records unavailable (project root not '
                             'resolved yet) -- retry shortly.'}
        # Record the key AFTER the job is durable, so the marker can never
        # name a save that was not persisted.
        if idempotency_key:
            _record_job_key(idempotency_key, job['id'])
        run("o = op(%r)\nif o and o.valid: o.ext.Envoy._runSaveJob(%r)"
            % (self.ownerComp.path, job['id']),
            fromOP=self.ownerComp, delayFrames=3)
        return {'job_id': job['id'], 'status': 'running',
                'hint': 'project.save() runs in ~3 frames and blocks TD '
                        'briefly (TDN strip/restore + release export); poll '
                        'get_job_status(job_id=...). The save restarts the '
                        'server, so the NEXT call may fail once with a '
                        'connection error -- just retry it; the bridge '
                        'reconnects between calls.'}

    _UPDATE_JOB_TIMEOUT_S = 900   # download + install ceiling
    # Updatestatus texts that mean the update machinery is still working;
    # anything else while a job runs is terminal (success or refusal).
    _UPDATE_ACTIVE_PREFIXES = ('Checking for updates', 'Downloading',
                               'Installing')

    def _activeUpdateJob(self):
        """The in-flight update_embody record, or None. Two-hour scan cap."""
        try:
            now = time.time()
            for record in _list_jobs(now):
                if (record.get('kind') == 'update_embody'
                        and record.get('status') == 'running'
                        and now - float(record.get('started', 0)) < 7200):
                    return record
        except Exception:
            pass
        return None

    def _update_embody(self, idempotency_key=None) -> dict:
        """Start a self-update as a tracked job (main thread).

        Refusals from the updater's own guards (dev checkout, update
        already in progress) return {'error'} with NO job minted. The
        install swaps this whole component in place, so completion is
        stamped by _pollUpdateJob, which re-resolves the comp BY PATH
        each tick -- the NEW instance finishes the record.
        """
        if idempotency_key:
            try:
                prior = _job_for_key(idempotency_key,
                                     expected_kind='update_embody')
            except _IdemMarkerUnreadable as e:
                return {'error': 'Idempotency marker unreadable (%s) -- '
                                 'refusing to risk a duplicate update.' % e}
            except _IdemKeyConflict as e:
                return {'error': 'idempotency_key is already bound to a '
                                 'different operation (%s).' % e}
            if prior is not None:
                return {'job_id': prior['id'],
                        'status': prior.get('status', 'running'),
                        'hint': 'Reconciled to the original update for this '
                                'idempotency_key.'}
        try:
            if op.Embody.ext.Embody._testRunnerActive():
                return {'error': 'A test run is active -- an update swaps '
                                 'the component mid-run. Wait for it.'}
        except Exception:
            pass
        active = self._activeUpdateJob()
        if active is not None:
            return {'job_id': active['id'], 'status': 'running',
                    'hint': 'An update is already in flight -- returning '
                            'its existing job handle.'}
        try:
            if op.Embody.ext.Embody._performMode:
                return {'error': 'This node is in Perform Mode -- never '
                                 'update a machine mid-show.'}
        except Exception:
            pass
        try:
            updater = self.ownerComp.op('updater').ext.UpdaterExt
        except Exception:
            return {'error': 'Updater is unavailable on this component.'}
        started = updater.CheckForUpdate(interactive=False,
                                         auto_install=True)
        if isinstance(started, dict) and started.get('error'):
            return {'error': started['error']}
        job = _new_job('update_embody', {}, idempotency_key=idempotency_key)
        try:
            job['version_before'] = str(op.Embody.par.Version.eval())
        except Exception:
            pass
        _write_job(job)
        if _read_job(job['id']) is None:
            return {'error': 'Job records unavailable (project root not '
                             'resolved yet) -- retry shortly.'}
        if idempotency_key:
            _record_job_key(idempotency_key, job['id'])
        self._armUpdatePoll(job['id'])
        return {'job_id': job['id'], 'status': 'running',
                'hint': 'The updater is checking/downloading; poll '
                        'get_job_status(job_id=...). A successful install '
                        'restarts the MCP server -- expect one reconnect '
                        'blip; the finished record carries '
                        'version_before/version_after.'}

    def _armUpdatePoll(self, job_id, delay_frames=60):
        """Arm one poll tick that SURVIVES the component swap.

        Deliberately no fromOP: the install destroys this comp, and a
        fromOP-bound run dies with it -- the tick re-resolves the comp by
        path and lands on the NEW instance (which ships this method from
        this version on; getattr-guarded for safety)."""
        try:
            run("o = op(%r)\n"
                "f = (o and o.valid) and getattr(o.ext.Envoy, "
                "'_pollUpdateJob', None) or None\n"
                "f and f(%r)" % (self.ownerComp.path, job_id),
                delayFrames=delay_frames)
        except Exception:
            pass

    def _pollUpdateJob(self, job_id):
        """Finalize an update job from live state. Main thread, swap-proof.

        Terminal when: Version moved past version_before (done), the
        updater status left the active set ('Up to date'/'Updated to' ->
        done, refusal/failure text -> error), or the ceiling passed."""
        job = _read_job(job_id)
        if not isinstance(job, dict) or job.get('status') != 'running':
            return
        try:
            version_now = str(op.Embody.par.Version.eval())
        except Exception:
            version_now = ''
        try:
            status = str(op.Embody.par.Updatestatus.eval() or '')
        except Exception:
            status = ''
        before = str(job.get('version_before') or '')
        elapsed = time.time() - float(job.get('started', 0) or 0)
        working = (any(status.startswith(pfx)
                       for pfx in self._UPDATE_ACTIVE_PREFIXES)
                   or status.endswith('available'))
        terminal = None
        if version_now and before and version_now != before:
            terminal = ('done', '')
        elif status.startswith('Updated to') or status.startswith(
                'Up to date'):
            terminal = ('done', '')
        elif status and not working:
            # The updater rested on a refusal/failure text.
            terminal = ('error', status)
        elif elapsed > self._UPDATE_JOB_TIMEOUT_S:
            terminal = ('error', 'update did not finish within %ds '
                                 '(last status: %s)'
                        % (self._UPDATE_JOB_TIMEOUT_S, status or 'none'))
        if terminal is None:
            self._armUpdatePoll(job_id)
            return
        job['status'] = terminal[0]
        if terminal[1]:
            job['error'] = terminal[1]
        job['version_after'] = version_now
        job['update_status'] = status
        job['finished'] = time.time()
        _write_job(job)

    def _runSaveJob(self, job_id):
        """Main-thread body of a save_project job.

        Everything the post-save lines need is bound into LOCALS before
        project.save(): the save recompiles this module, and an old
        frame's module globals have been observed to stop resolving after
        recompilation (see check_responses' Empty handling). Locals --
        including the module OBJECTS os/json/time, whose own internals
        live in sys.modules, untouched by a DAT recompile -- survive
        regardless, and the record path is precomputed so no helper in
        the old namespace is needed afterwards."""
        job = _read_job(job_id) or {'id': job_id, 'kind': 'save_project'}
        path = _job_path(job_id)
        _os, _json, _time = os, json, time
        _project, _op = project, op
        try:
            _project.save()
            job['status'] = 'done'
            try:
                job['version_after'] = str(_op.Embody.par.Version.eval())
                job['toe'] = str(_project.name)
            except Exception:
                pass
        except Exception as e:
            job['status'] = 'error'
            job['error'] = 'project.save() failed: %s' % e
        job['finished'] = _time.time()
        if path:
            try:
                tmp = path + '.%d.tmp' % _os.getpid()
                with open(tmp, 'w', encoding='utf-8') as f:
                    _json.dump(job, f, indent=1)
                for attempt in range(3):
                    try:
                        _os.replace(tmp, path)
                        break
                    except PermissionError:
                        if attempt == 2:
                            raise
            except Exception:
                pass

    def _convoy_lifecycle_state(self) -> dict:
        """Main-thread, fail-closed project state used by Convoy restart.

        TouchDesigner exposes ``project.modified`` but Embody's own post-save
        maintenance can conservatively set it again.  That may refuse a safe
        restart; it can never authorize a dirty one.  The revision is a CAS
        token over the only stable facts available from TD: modified state,
        saved-file identity, and TD's last-save marker.  It contains no path.
        """
        try:
            modified = getattr(project, 'modified', None)
            if type(modified) is not bool:
                return {'error': 'TouchDesigner dirty state is unavailable',
                        'code': 'dirty_state_unknown'}
            toe_path = os.path.join(str(project.folder), str(project.name))
            try:
                stat = os.stat(toe_path)
                unsaved = False
                file_facts = [int(stat.st_size),
                              int(getattr(stat, 'st_mtime_ns',
                                  int(stat.st_mtime * 1000000000)))]
            except (OSError, ValueError, TypeError):
                unsaved = True
                file_facts = [0, 0]
            facts = {
                'v': 1,
                'modified': modified,
                'unsaved': unsaved,
                'size': file_facts[0],
                'mtime_ns': file_facts[1],
                'save_time': str(getattr(project, 'saveTime', '') or ''),
            }
            encoded = json.dumps(
                facts, sort_keys=True, separators=(',', ':'),
                ensure_ascii=True).encode('utf-8')
            revision = hashlib.sha256(encoded).hexdigest()
            return {'ok': True, 'dirty': modified, 'unsaved': unsaved,
                    'revision': revision}
        except Exception as e:
            self._log('Convoy lifecycle state failed: %s' % type(e).__name__,
                      'ERROR')
            return {'error': 'TouchDesigner dirty state is unavailable',
                    'code': 'dirty_state_unknown'}

    def _convoy_lifecycle_quit(self, expected_dirty_revision=None,
                               discard=False) -> dict:
        """Schedule an exact-process quit after a final clean-state CAS.

        The host already verified PID birth, executable, user/session, node,
        and runtime.  This final in-process check prevents a late edit between
        the host's dirty read and the quit commit.  ``force=True`` suppresses
        TD's modal save prompt only after that clean proof, or after the local
        destructive policy explicitly authorized discard.
        """
        if type(discard) is not bool:
            return {'error': 'discard must be boolean',
                    'code': 'invalid_arguments'}
        state = self._convoy_lifecycle_state()
        if state.get('ok') is not True:
            return state
        if not discard:
            if (not isinstance(expected_dirty_revision, str)
                    or not expected_dirty_revision):
                return {'error': 'expected dirty revision is required',
                        'code': 'invalid_arguments'}
            if expected_dirty_revision != state.get('revision'):
                return {'error': 'project changed before quit',
                        'code': 'dirty_revision_changed'}
            if state.get('dirty') or state.get('unsaved'):
                return {'error': 'project is dirty or unsaved',
                        'code': 'project_dirty'}
        try:
            # ``project.quit`` must run after the MCP response has had a
            # chance to leave this process.  Do not schedule a bare quit:
            # authored state can change during those two frames.  The
            # delayed callback repeats the clean-state CAS at the actual
            # destructive boundary and refuses a late edit.
            callback = (
                "op(%r).ext.Envoy._convoy_lifecycle_commit_quit(%r, %r)" %
                (self.ownerComp.path, expected_dirty_revision, discard))
            run(callback, fromOP=self.ownerComp, delayFrames=2)
        except Exception as e:
            self._log('Convoy lifecycle quit scheduling failed: %s' %
                      type(e).__name__, 'ERROR')
            return {'error': 'TouchDesigner quit could not be scheduled',
                    'code': 'quit_failed'}
        return {'ok': True, 'quitting': True,
                'dirty_revision': state.get('revision'),
                'discard': discard}

    def _convoy_lifecycle_commit_quit(self, expected_dirty_revision=None,
                                      discard=False) -> dict:
        """Revalidate and quit at the delayed destructive boundary.

        ``_convoy_lifecycle_quit`` necessarily acknowledges before TD exits.
        This callback therefore performs the same fail-closed state read and
        revision comparison again, immediately before ``project.quit``.  A
        refusal is logged and leaves the process running for the host's
        lifecycle reconciliation path; it never converts uncertainty into a
        forced discard.
        """
        state = self._convoy_lifecycle_state()
        if state.get('ok') is not True:
            self._log('Convoy lifecycle quit aborted: dirty state became '
                      'unavailable', 'ERROR')
            return state
        if not discard:
            if (not isinstance(expected_dirty_revision, str)
                    or not expected_dirty_revision):
                result = {'error': 'expected dirty revision is required',
                          'code': 'invalid_arguments'}
            elif expected_dirty_revision != state.get('revision'):
                result = {'error': 'project changed before quit',
                          'code': 'dirty_revision_changed'}
            elif state.get('dirty') or state.get('unsaved'):
                result = {'error': 'project is dirty or unsaved',
                          'code': 'project_dirty'}
            else:
                result = None
            if result is not None:
                self._log('Convoy lifecycle quit aborted: %s' %
                          result['code'], 'WARNING')
                return result
        try:
            project.quit(force=True)
        except Exception as e:
            self._log('Convoy lifecycle quit failed: %s' %
                      type(e).__name__, 'ERROR')
            return {'error': 'TouchDesigner quit failed',
                    'code': 'quit_failed'}
        return {'ok': True, 'quitting': True,
                'dirty_revision': state.get('revision'),
                'discard': discard}

    def _schedulePollTestCompletion(self):
        """Schedule the test completion poll via run() with a string
        expression that resolves the live extension instance at call time."""
        run(f"op('{self.ownerComp.path}').ext.Envoy._pollTestCompletion()",
            fromOP=self.ownerComp, delayFrames=5)

    def _pollTestCompletion(self):
        """Check if deferred test run has finished; signal worker thread if so."""
        test_comp = op.unit_tests
        if not test_comp or not test_comp.extensionsReady:
            self._schedulePollTestCompletion()
            return
        runner = getattr(test_comp.ext, 'TestRunnerExt', None)
        if runner and not runner._running:
            # Restore Status BEFORE the pending check: a lost or cancelled
            # sys._envoy_pending_test (e.g. a refused overlapping run cleared
            # it) must not leave Status stuck at 'Testing'. The restore is
            # storage-backed and idempotent, so a duplicate chain is safe.
            self._restoreStatusAfterTests()
            pending = getattr(sys, '_envoy_pending_test', None)
            if pending is None:
                return  # Caller already handled/cancelled; Status restored.
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
        """Get operator information -- see envoy_read."""
        return mod.envoy_read.get_op(self, op_path, include_defaults)

    # Sole consumer is envoy_ops.grow_sequence_for (via ext._SEQ_PAR_RE) --
    # kept here so the module DAT needs no re import.
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
        target = mod.envoy_read.resolve_op(self, op_path)
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
        """List operators in a network -- see envoy_read."""
        return mod.envoy_read.query_network(self, parent_path, recursive, op_type, include_utility)

    def _copy_op(self, source_path: str, dest_parent: str, new_name: str = None) -> dict:
        """Copy an operator -- see envoy_ops."""
        return mod.envoy_ops.copy_op(self, source_path, dest_parent, new_name)

    def _get_connections(self, op_path: str) -> dict:
        """Get all connections for an operator -- see envoy_read."""
        return mod.envoy_read.get_connections(self, op_path)

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

    def _lintWorkerRun(self, source, where):
        """WARN when submitted source hands a thread a target that calls TD's
        global run(). Static and bounded (_worker_run_findings size-caps and
        substring-prefilters before parsing), so the per-call frame cost stays
        negligible, and it fires on the WRITE, not on the crash that
        would otherwise arrive frames later. Warning only -- the code still
        executes and the DAT is still written, same contract as LAYOUT
        WARNING; the warning rides back in _logs via _attachNotableLogs."""
        try:
            findings = _worker_run_findings(source or '')
            if not findings:
                return
            detail = '; '.join(
                '"%s" reaches %s at line %d%s'
                % (f['function'], f['call'], f['line'],
                   (' (via %s)' % f['via']) if f['via'] else '')
                for f in findings[:5])
            self._log(
                'THREADING WARNING: ' + where + ' -- thread target ' + detail
                + '. Worker-side run() corrupts TD state and crashes later '
                  '(Derivative-confirmed 2026-08-17); hand results back as '
                  'plain data drained by a main-thread pump instead.',
                'WARNING')
        except Exception:
            pass

    def _execute_python(self, code: str) -> dict:
        """Execute arbitrary Python code"""
        code_preview = code[:200] + ('...' if len(code) > 200 else '')
        self._log(f'execute_python: {code_preview}')
        # Before exec, so the warning lands even when the code itself fails.
        self._lintWorkerRun(code, 'execute_python')
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
        skills/build-ui/td-ui-mechanics.md). Parameter changes to PRE-EXISTING ops are NOT rolled
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
            # build rides along so the worker-side docs fusion can pick the
            # right catalog column without its own main-thread round-trip.
            return {'roots': roots, 'build': f'{app.version}.{app.build}'}
        except Exception as e:
            return {'roots': [], 'error': f'Failed to get docs roots: {e}'}

    def _get_td_info(self) -> dict:
        """Get TouchDesigner environment and Envoy server info -- see envoy_read."""
        return mod.envoy_read.get_td_info(self)

    def _get_focus(self) -> dict:
        """Report the pane/selection the user is looking at -- see envoy_read."""
        return mod.envoy_read.get_focus(self)

    def _get_op_errors(self, op_path: str, recurse: bool = True) -> dict:
        """Get error and warning messages for an operator and its children -- see envoy_read."""
        return mod.envoy_read.get_op_errors(self, op_path, recurse)

    def _exec_op_method(self, op_path: str, method: str,
                          args: list = None, kwargs: dict = None) -> dict:
        """Call a method on a TD operator -- see envoy_read."""
        return mod.envoy_read.exec_op_method(self, op_path, method, args, kwargs)

    def _get_td_classes(self) -> dict:
        """List all Python classes/modules in the td module -- see envoy_read."""
        return mod.envoy_read.get_td_classes(self)

    def _get_td_class_details(self, class_name: str) -> dict:
        """Get detailed info about a specific TD Python class -- see envoy_read."""
        return mod.envoy_read.get_td_class_details(self, class_name)

    def _get_module_help(self, module_name: str) -> dict:
        """Get Python help text for a TD module or class -- see envoy_read."""
        return mod.envoy_read.get_module_help(self, module_name)

    # === DAT Content Operations (Main Thread Only) ===

    def _get_dat_content(self, op_path: str, format: str = "auto") -> dict:
        """Get DAT content as text or table data -- see envoy_read."""
        return mod.envoy_read.get_dat_content(self, op_path, format)

    def _get_chop_data(self, op_path: str, channels: str = None,
                       samples: int = 0, compare_to: str = None) -> dict:
        """Reduce a CHOP to per-channel stats -- see envoy_read."""
        return mod.envoy_read.get_chop_data(
            self, op_path, channels, samples, compare_to)

    def _get_pop_data(self, op_path: str, attributes: str = None,
                      samples: int = 0, max_points: int = 50000) -> dict:
        """Read POP attribute metadata (+ optional points) -- see envoy_read."""
        return mod.envoy_read.get_pop_data(
            self, op_path, attributes, samples, max_points)

    def _set_dat_content(self, op_path: str, text: str = None,
                        rows: list = None, clear: bool = False,
                        confirm_wipe: bool = False) -> dict:
        """Set DAT content from text or table rows -- see envoy_ops."""
        result = mod.envoy_ops.set_dat_content(self, op_path, text, rows, clear, confirm_wipe)
        # text= IS the resulting full text; rows= is a table, never source.
        if (isinstance(result, dict) and result.get('success')
                and isinstance(text, str)):
            self._lintWorkerRun(text, 'set_dat_content ' + op_path)
        return result

    def _edit_dat_content(self, op_path: str, old_string: str,
                         new_string: str, replace_all: bool = False,
                         confirm_wipe: bool = False) -> dict:
        """Surgical text edit on a DAT -- see envoy_ops."""
        result = mod.envoy_ops.edit_dat_content(self, op_path, old_string, new_string, replace_all, confirm_wipe)
        # A partial edit only makes sense against the WHOLE resulting text --
        # re-read the DAT rather than linting the spliced-in fragment. The
        # row-count proxy skips even the full-text read when the DAT is far
        # beyond the lint's byte cap; a finding here can also predate this
        # edit, which the where-label says out loud.
        if (isinstance(result, dict) and result.get('success')
                and result.get('numRows', 0) <= 4096):
            try:
                target = self._resolve_op(op_path)
                edited = target.text if target is not None else None
            except Exception:
                edited = None
            if isinstance(edited, str):
                self._lintWorkerRun(
                    edited, 'edit_dat_content ' + op_path
                    + ' (whole resulting text, may predate this edit)')
        return result

    # === TOP Capture (Main Thread Only) ===

    def _capture_top(self, op_path: str, format: str = 'jpeg',
                     quality: float = 0.8, max_resolution: int = 640,
                     inline: bool = False, sample_grid: int = 0) -> dict:
        """Capture a TOP operator's output as a compressed image -- see envoy_read."""
        return mod.envoy_read.capture_top(self, op_path, format, quality, max_resolution, inline, sample_grid)

    # === Operator Flags Operations (Main Thread Only) ===

    def _get_op_flags(self, op_path: str) -> dict:
        """Get all flags for an operator -- see envoy_read."""
        return mod.envoy_read.get_op_flags(self, op_path)

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
        """Get operator position and visual properties -- see envoy_read."""
        return mod.envoy_read.get_op_position(self, op_path)

    def _get_network_layout(self, comp_path: str, include_annotations: bool = True) -> dict:
        """Get positions of all operators and annotations in a COMP -- see envoy_read."""
        return mod.envoy_read.get_network_layout(self, comp_path, include_annotations)

    def _set_op_position(self, op_path: str, x: int = None, y: int = None,
                        width: int = None, height: int = None,
                        color: list = None, comment: str = None) -> dict:
        """Set operator position and visual properties -- see envoy_ops."""
        return mod.envoy_ops.set_op_position(self, op_path, x, y, width, height, color, comment)

    def _layout_children(self, op_path: str) -> dict:
        """Auto-layout children in a COMP"""
        target = mod.envoy_read.resolve_op(self, op_path)
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
        """List all annotations in a COMP -- see envoy_read."""
        return mod.envoy_read.get_annotations(self, parent_path)

    def _resolve_op(self, op_path: str):
        """Resolve an operator path, tolerating utility-flagged hops (annotations) -- see envoy_read."""
        return mod.envoy_read.resolve_op(self, op_path)

    def _resolve_annotation(self, op_path: str):
        """Resolve an annotation path, including utility-flagged ones -- see envoy_read."""
        return mod.envoy_read.resolve_annotation(self, op_path)

    def _set_annotation(self, op_path: str, text: str = None, title: str = None,
                        color: list = None, opacity: float = None,
                        width: int = None, height: int = None,
                        x: int = None, y: int = None) -> dict:
        """Modify an existing annotation -- see envoy_ops."""
        return mod.envoy_ops.set_annotation(self, op_path, text, title, color, opacity, width, height, x, y)

    def _get_enclosed_ops(self, op_path: str) -> dict:
        """Get annotation/operator enclosure relationships -- see envoy_read."""
        return mod.envoy_read.get_enclosed_ops(self, op_path)

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
        """Search for operators using COMP.findChildren -- see envoy_read."""
        return mod.envoy_read.find_children(self, op_path, name, type, depth, tags, text, comment, include_utility)

    def _get_op_performance(self, op_path: str, include_children: bool = False) -> dict:
        """Get performance data for an operator -- see envoy_read."""
        return mod.envoy_read.get_op_performance(self, op_path, include_children)

    def _get_project_performance(self, include_hotspots: int = 0) -> dict:
        """Get project-level performance via Perform CHOP -- see envoy_read."""
        return mod.envoy_read.get_project_performance(self, include_hotspots)

    # === Embody Integration ===

    def _externalize_op(self, op_path: str, tag_type: str = None) -> dict:
        """Tag an operator for Embody externalization and write it to disk -- see envoy_ops."""
        return mod.envoy_ops.externalize_op(self, op_path, tag_type)

    def _remove_externalization_tag(self, op_path: str,
                                    delete_file: bool = False) -> dict:
        """Remove Embody externalization tag and clean up -- see envoy_ops.

        `delete_file` MUST stay in this signature: the registered tool
        wrapper puts it in the params dict unconditionally (not only when a
        caller passes it) and dispatch is `handler(**params)`, so dropping
        it here made EVERY call to this tool fail with a TypeError from
        v6.0.154 until this fix -- the tool was dead, not merely degraded.
        test_envoy_tool_schema.py now asserts this class of drift for every
        registered tool.
        """
        return mod.envoy_ops.remove_externalization_tag(
            self, op_path, delete_file)

    def _get_externalizations(self) -> dict:
        """Get all externalized operators -- see envoy_read."""
        return mod.envoy_read.get_externalizations(self)

    def _save_externalization(self, op_path: str) -> dict:
        """Force save an externalized operator -- see envoy_ops."""
        return mod.envoy_ops.save_externalization(self, op_path)

    def _get_externalization_status(self, op_path: str) -> dict:
        """Get externalization status for an operator -- see envoy_read."""
        return mod.envoy_read.get_externalization_status(self, op_path)

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
        """Delegate to TDN extension for network export -- see envoy_read."""
        return mod.envoy_read.export_network(self, root_path, include_dat_content, output_file, max_depth, embed_all)

    def _import_network(self, target_path, tdn, clear_first=False):
        """Delegate to TDN extension for network import -- see envoy_ops."""
        return mod.envoy_ops.import_network(self, target_path, tdn, clear_first)

    def _read_tdn(self, comp_path='/', include_dat_content=None,
                  max_depth=None, embed_all=False):
        """Read a network subtree as a TDN dict (in-memory, no disk write) -- see envoy_read."""
        return mod.envoy_read.read_tdn(self, comp_path, include_dat_content, max_depth, embed_all)

    def _diff_tdn(self, target='', max_changed_ops=200, max_bytes=60000):
        """Show what is UNSAVED in TDN-externalized COMPs vs on-disk .tdn -- see envoy_read."""
        return mod.envoy_read.diff_tdn(self, target, max_changed_ops, max_bytes)



    # === Utility Methods ===

    def _configureMCPClient(self, port, target_dir=None):
        """Auto-configure MCP client (.mcp.json + STDIO bridge) -- see envoy_setup."""
        return mod.envoy_setup.configure_mcp_client(self, port, target_dir)

    def _configureMCPClientHTTP(self, target_dir, port):
        """Fallback: configure .mcp.json with direct HTTP transport -- see envoy_setup."""
        return mod.envoy_setup.configure_mcp_client_http(self, target_dir, port)

    def _registryPath(self):
        """Path to .embody/envoy.json honoring Aiprojectroot -- see envoy_setup."""
        return mod.envoy_setup.registry_path(self)

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
        'get_chop_data', 'get_dat_content', 'get_docs', 'get_guidance',
        'get_module_help', 'get_pop_data',
        'get_logs', 'get_focus', 'get_job_status',
        'get_externalizations', 'get_externalization_status', 'get_sessions',
        'query_network', 'find_children', 'get_enclosed_ops',
        'read_tdn', 'diff_tdn', 'capture_top',
    ]

    def _toolPermissionsPosture(self):
        """The Toolpermissions param value, defensively normalized -- see envoy_setup."""
        return mod.envoy_setup.tool_permissions_posture(self)

    def _tempReadDirs(self):
        """Directories that must be readable for capture_top PNGs -- see envoy_setup."""
        return mod.envoy_setup.temp_read_dirs(self)

    def _loadSettingsBaseline(self):
        """The NON-Envoy baseline settings dict -- see envoy_setup."""
        return mod.envoy_setup.load_settings_baseline(self)

    def _worktreePermissionRules(self, root=None):
        """Allow rules pre-authorizing sibling '<repo>-wt-*' worktrees -- see envoy_setup."""
        return mod.envoy_setup.worktree_permission_rules(self, root)

    def _mirrorAiConfigToWorktrees(self, root):
        """Copy gitignored AI config into sibling '<repo>-wt-*' worktrees -- see envoy_setup."""
        return mod.envoy_setup.mirror_ai_config_to_worktrees(self, root)

    def _composeSettings(self, cfg, posture, root=None):
        """Apply a tool-permissions posture onto a settings dict in place -- see envoy_setup."""
        return mod.envoy_setup.compose_settings(self, cfg, posture, root)

    def _settingsSatisfies(self, cfg, posture, root=None):
        """True if an existing settings dict already matches `posture` -- see envoy_setup."""
        return mod.envoy_setup.settings_satisfies(self, cfg, posture, root)

    def _deploySettingsLocal(self, claude_dir):
        """Write .claude/settings.local.json to match the Toolpermissions posture -- see envoy_setup."""
        return mod.envoy_setup.deploy_settings_local(self, claude_dir)

    def _findGitRoot(self):
        """Silently find the git repo root. Returns Path or 'no-git' -- see envoy_setup."""
        return mod.envoy_setup.find_git_root(self)

    def _checkOrInitGitRepo(self):
        """Check for a git repo, prompting to initialize one if missing -- see envoy_setup."""
        return mod.envoy_setup.check_or_init_git_repo(self)

    @staticmethod
    def _atomicWriteJSON(path, data):
        """Write JSON atomically via temp file + os.replace() -- see envoy_setup."""
        return mod.envoy_setup.atomic_write_json(path, data)

    def _instanceKey(self, toe_rel: str, existing_instances: dict) -> str:
        """Compute a unique instance key from the toe filename -- see envoy_setup."""
        return mod.envoy_setup.instance_key(self, toe_rel, existing_instances)

    @staticmethod
    def _isPidAlive(pid):
        """Check whether a process with the given PID is alive -- see envoy_setup."""
        return mod.envoy_setup.is_pid_alive(pid)

    def _writeEnvoyConfig(self, embody_dir, port):
        """Register this instance in the .embody/envoy.json instance registry -- see envoy_setup."""
        return mod.envoy_setup.write_envoy_config(self, embody_dir, port)

    def RefreshRegistry(self):
        """Re-register this instance in envoy.json under its current toe basename -- see envoy_setup."""
        return mod.envoy_setup.refresh_registry(self)

    def _removeFromRegistry(self, git_root=None):
        """Remove this instance from the .embody/envoy.json registry on shutdown -- see envoy_setup."""
        return mod.envoy_setup.remove_from_registry(self, git_root)

    def _configureGitignore(self, git_root):
        """Ensure .gitignore contains Embody/Envoy auto-generated entries -- see envoy_setup."""
        return mod.envoy_setup.configure_gitignore(self, git_root)

    def _configureGitattributes(self, git_root):
        """Ensure .gitattributes normalizes TD line endings + .tdn diffs -- see envoy_setup."""
        return mod.envoy_setup.configure_gitattributes(self, git_root)

    def _configureTdnDiffDriver(self, target_dir, python_cmd):
        """Deploy the .tdn git textconv script and register the diff driver -- see envoy_setup."""
        return mod.envoy_setup.configure_tdn_diff_driver(self, target_dir, python_cmd)

    def _cleanupTempFiles(self):
        """Remove stale Envoy temp files from /tmp -- see envoy_setup."""
        return mod.envoy_setup.cleanup_temp_files(self)

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
