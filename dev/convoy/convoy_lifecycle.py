"""Exact-node TouchDesigner process lifecycle for Convoy.

This module is deliberately host-side, stdlib-only, and independent of the
HTTP/LAN/job layers.  It does not import TouchDesigner and it never accepts an
executable, project path, cwd, or environment from a remote operation.  A live
LOCAL node registration first records a launch profile; later lifecycle calls
name only that stable ``node_id`` and ``convoy_id``.

The safety model is intentionally stricter than Envoy's historical bridge
``launch_td`` / ``restart_td`` helpers:

* one exact executable and one exact saved .toe are pinned from a verified
  local process, including file identities, user/session, and process-birth
  identity;
* start/restart is serialized per exact executable/.toe/session launch unit
  and content-bound to a durable operation id;
* every spawn carries a one-time random launch token, and success means the
  expected stable node registered with that token -- a PID is never success;
* restart defaults to ``require_clean``.  Unknown or dirty state is a refusal;
  save, discard, and force are distinct structured policies;
* discard/force require a local policy callback returning literal ``True``;
* cancellation before restart's quit commit is honored.  After the old
  runtime is committed to exit, cancellation is deferred and the replacement
  launch continues, because leaving a show node down is the less safe outcome;
* one absolute monotonic deadline covers lock wait and every pre-commit
  boundary. After the restart commit, expiry is reported as deferred while a
  separate bounded restoration budget finishes quit/force/relaunch;
* macOS launches the exact app binary and signals only the exact PID.  There
  is no AppleScript, ``open -a``, ``killall``, or application-wide quit path.

Integration contract (kept explicit so absent proof fails closed):

``LocalProcessInspector``
    ``inspect_status(pid)`` is the authoritative tri-state API (``alive`` +
    exact snapshot, ``dead``, or ``unknown``). ``unknown`` is never treated as
    offline. ``current_session()`` returns user/session/interactive proof.

``runtime_adapter`` supplied to :class:`LifecycleManager`
    ``current(node_id)`` returns the current host directory/runtime snapshot
    or ``None``. It must also provide an atomic launch-directory reservation:
    ``reserve_launch(node_id, launch_unit_id, operation_id, timeout_s,
    cancel_event)``, ``confirm_launch_reservation(...)``,
    ``release_launch_reservation(...)``, and
    ``restore_launch_reservations(...)``. Confirmation validates the live
    directory fence without consuming it; release happens only after the
    attempt/profile confirmation is durable. Restore rebuilds those fences
    from the durable attempt ledger before the host serves registrations.
    ``dirty(...)`` returns ``ok``,
    boolean ``dirty`` and ``unsaved``, plus an opaque ``revision``. ``quit``
    accepts ``discard`` and ``expected_dirty_revision`` and compares the
    latter on TD's main thread immediately before quitting. ``save`` and all
    of these are structured calls; no full-shell permission participates.

``LifecycleManager.confirm_registration(...)``
    The local register route calls this after validating its ordinary IPC and
    node identity. The launch token and reservation id must be supplied by the
    newly launched TD process (from ``EMBODY_CONVOY_LAUNCH_TOKEN`` and
    ``EMBODY_CONVOY_LAUNCH_RESERVATION``). A registration snapshot contains
    exact ``host_id``, ``comp_path``, ``toe_path``, TD build, runtime id, PID,
    and reservation id. Token-bearing registration confirms before ordinary
    profile refresh; the immutable profile digest also tolerates that refresh.

The host's existing durable job remains the user-facing job authority.  The
small private attempt ledger here is additional crash/idempotency fencing: a
host crash between spawn and ACK cannot make a retry launch a duplicate.
Exactly one singleton HostApp process must own this store; its atomic files are
thread-safe and crash-safe but are intentionally not a multi-process database.

Windows behavior is exercised on Windows hardware. The macOS adapter and
direct-binary argv are unit-tested by construction, but have not yet been
validated on physical Apple Silicon hardware. macOS has no pidfd/process
HANDLE; libproc birth is rechecked immediately before an exact-PID signal, so
the residual inspect-to-signal race remains an explicit release-gate test.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import signal
import stat
import subprocess
import sys
import threading
import time


HOST_LIFECYCLE_CAPABILITY = "host.td-lifecycle/v1"
STATE_SCHEMA = 1
STATE_FILE = "lifecycle.json"

DEFAULT_START_TIMEOUT_S = 120.0
DEFAULT_QUIT_TIMEOUT_S = 30.0
DEFAULT_RESTART_RECOVERY_TIMEOUT_S = (
    DEFAULT_START_TIMEOUT_S + (2 * DEFAULT_QUIT_TIMEOUT_S))
MIN_TIMEOUT_S = 0.1
MAX_TIMEOUT_S = 900.0
POLL_S = 0.05

CRASH_LOOP_WINDOW_S = 5 * 60.0
CRASH_LOOP_MAX_LAUNCHES = 3
LAUNCH_STABILITY_WINDOW_S = 60.0
MAX_ATTEMPTS = 512
MAX_STATE_BYTES = 8 * 1024 * 1024
MAX_TEXT_BYTES = 4096

RESTART_POLICIES = frozenset({
    "require_clean", "save_then_restart", "discard_and_restart", "force",
})
DESTRUCTIVE_RESTART_POLICIES = frozenset({"discard_and_restart", "force"})

ACTIVE_ATTEMPT_STATES = frozenset({
    "created", "restart_committed", "launching", "awaiting_registration",
})
TERMINAL_ATTEMPT_STATES = frozenset({
    "succeeded", "failed", "cancelled", "indeterminate",
})

_HEX128 = re.compile(r"^[0-9a-f]{32}$")
_OPERATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_RUNTIME_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_TOKEN = re.compile(r"^[A-Za-z0-9_-]{32,256}$")

_DETAILS = {
    "ok": "operation completed",
    "already_running": "the exact node is already running",
    "unknown_profile": "no local launch profile exists for this node",
    "profile_disabled": "Convoy is disabled for this node",
    "launch_not_eligible": "the local launch profile is not remotely launchable",
    "namespace_mismatch": "the node is not in the addressed Convoy",
    "invalid_arguments": "lifecycle arguments are invalid",
    "idempotency_conflict": "the operation id is already bound to different work",
    "profile_busy": "another lifecycle operation owns this node",
    "profile_changed": "the launch profile changed during the operation",
    "project_missing": "the exact saved TouchDesigner project is unavailable",
    "project_changed": "the saved TouchDesigner project changed since local registration",
    "executable_missing": "the exact TouchDesigner executable is unavailable",
    "executable_changed": "the TouchDesigner executable changed since local registration",
    "session_unavailable": "the recorded interactive user session is unavailable",
    "runtime_unverifiable": "the running process cannot be proven to be the recorded runtime",
    "runtime_changed": "the addressed TouchDesigner runtime changed",
    "shared_runtime": "the exact TouchDesigner process hosts multiple Embody nodes",
    "node_offline": "the node has no running TouchDesigner runtime",
    "orphan_runtime": "the prior exact process is still alive but is not registered",
    "dirty_state_unknown": "TouchDesigner project dirty state could not be proven",
    "project_dirty": "TouchDesigner has unsaved project changes",
    "save_failed": "TouchDesigner could not save the project safely",
    "destructive_policy_disabled": "the requested destructive restart policy is not locally approved",
    "quit_failed": "the exact TouchDesigner runtime did not exit safely",
    "launch_failed": "the exact TouchDesigner process could not be launched",
    "launch_reservation_failed": "the exact launch unit could not be reserved",
    "launch_unconfirmed": "TouchDesigner launched but the expected node did not register in time",
    "launch_token_invalid": "the one-time launch token did not match",
    "launch_token_replayed": "the one-time launch token was already consumed",
    "registration_mismatch": "a different process or node attempted launch confirmation",
    "crash_loop": "recent launch attempts reached the crash-loop limit",
    "cancelled": "operation was cancelled before its commit point",
    "deadline_exceeded": "operation deadline expired before its commit point",
    "indeterminate": "the lifecycle outcome is uncertain and must be reconciled",
    "store_unavailable": "the private lifecycle store is unavailable or invalid",
    "internal_error": "lifecycle operation failed safely",
}


class LifecycleError(Exception):
    """Internal fail-closed error carrying only a stable public code."""

    def __init__(self, code, detail=None):
        code = code if code in _DETAILS else "internal_error"
        super().__init__(detail or _DETAILS[code])
        self.code = code
        self.safe_detail = detail if isinstance(detail, str) else _DETAILS[code]


def lifecycle_catalog():
    """Return the reviewed host-native lifecycle capability manifest."""
    return {
        "capability": HOST_LIFECYCLE_CAPABILITY,
        "operations": {
            "convoy_start_node": {
                "mutating": True,
                "runtime_required": False,
                "full_shell_required": False,
                "arguments": {
                    "operation_id": "string",
                    "timeout_s": "number?",
                },
            },
            "convoy_restart_node": {
                "mutating": True,
                "runtime_required": True,
                "full_shell_required": False,
                "arguments": {
                    "operation_id": "string",
                    "expected_runtime_id": "string",
                    "policy": "require_clean|save_then_restart|"
                              "discard_and_restart|force?",
                    "timeout_s": "number?",
                },
            },
        },
        "limits": {
            "max_timeout_s": MAX_TIMEOUT_S,
            "crash_loop_window_s": CRASH_LOOP_WINDOW_S,
            "crash_loop_max_launches": CRASH_LOOP_MAX_LAUNCHES,
        },
    }


def _result(ok, code, **fields):
    code = code if code in _DETAILS else "internal_error"
    value = {"ok": bool(ok), "code": code, "detail": _DETAILS[code],
             "capability": HOST_LIFECYCLE_CAPABILITY}
    value.update(fields)
    try:
        json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        return {"ok": False, "code": "internal_error",
                "detail": _DETAILS["internal_error"],
                "capability": HOST_LIFECYCLE_CAPABILITY}
    return value


def _clone(value):
    return copy.deepcopy(value)


def _bounded_text(value, name, *, limit=MAX_TEXT_BYTES, optional=False):
    if optional and value in (None, ""):
        return ""
    if (not isinstance(value, str) or not value or value != value.strip()
            or "\0" in value):
        raise LifecycleError("invalid_arguments", "%s must be bounded text" % name)
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise LifecycleError("invalid_arguments", "%s is not valid UTF-8" % name)
    if len(encoded) > limit or any(ord(char) < 32 or ord(char) == 127
                                   for char in value):
        raise LifecycleError("invalid_arguments", "%s must be bounded text" % name)
    return value


def _stable_id(value, name):
    if not isinstance(value, str) or not _HEX128.fullmatch(value):
        raise LifecycleError("invalid_arguments", "%s is malformed" % name)
    return value


def _operation_id(value):
    if not isinstance(value, str) or not _OPERATION_ID.fullmatch(value):
        raise LifecycleError("invalid_arguments", "operation_id is malformed")
    return value


def _runtime_id(value, *, required=True):
    if not required and value in (None, ""):
        return ""
    if not isinstance(value, str) or not _RUNTIME_ID.fullmatch(value):
        raise LifecycleError("invalid_arguments", "runtime_id is malformed")
    return value


def _timeout(value, default):
    if value is None:
        return float(default)
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or not MIN_TIMEOUT_S <= float(value) <= MAX_TIMEOUT_S):
        raise LifecycleError("invalid_arguments", "timeout is outside the allowed range")
    return float(value)


def _execution_timeout(value, requested_timeout_s):
    """Validate a host-derived execution budget without rebinding content."""
    if value is None:
        return float(requested_timeout_s)
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(float(value)) or float(value) <= 0.0):
        raise LifecycleError(
            "deadline_exceeded", "no delivery execution budget remains")
    return min(float(requested_timeout_s), float(value))


def _restart_recovery_timeout(requested_timeout_s=None):
    """One post-commit restoration budget for quit, force, and relaunch.

    This is deliberately independent of the caller's pre-commit request
    timeout. Once the old runtime is committed to exit, leaving a show node
    down is the less safe outcome, so a short caller ``timeout_s`` (e.g. 5s)
    must never shrink the quit/force/launch phases below what actually
    restores the node. The caller's timeout bounds the pre-commit phase only;
    the post-commit phases are sized from this fixed restoration budget
    (launch + two quit slices).
    """
    return DEFAULT_RESTART_RECOVERY_TIMEOUT_S


def _platform_name(value=None):
    value = value or sys.platform
    if value == "win32":
        return "win32"
    if value == "darwin":
        return "darwin"
    if value.startswith("linux"):
        return "linux"
    raise LifecycleError("invalid_arguments", "unsupported lifecycle platform")


def _canonical_path(value):
    if not isinstance(value, (str, os.PathLike)):
        raise LifecycleError("invalid_arguments", "path must be text")
    try:
        value = os.fspath(value)
        if "\0" in value or not os.path.isabs(value):
            raise LifecycleError("invalid_arguments", "path must be absolute")
        return os.path.realpath(os.path.abspath(value))
    except (OSError, TypeError, ValueError):
        raise LifecycleError("invalid_arguments", "path is invalid")


def _path_same(left, right):
    try:
        if os.path.exists(left) and os.path.exists(right):
            return os.path.samefile(left, right)
    except OSError:
        pass
    return os.path.normcase(os.path.realpath(left)) == os.path.normcase(
        os.path.realpath(right))


def _path_within(root, candidate):
    try:
        return os.path.commonpath([os.path.realpath(root),
                                   os.path.realpath(candidate)]) == os.path.realpath(root)
    except (OSError, ValueError):
        return False


def _file_identity(path):
    """JSON-safe identity for one exact regular file, following no guesses."""
    try:
        info = os.stat(path)
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode):
        return None
    return {
        "device": int(info.st_dev),
        "inode": int(info.st_ino),
        "size": int(info.st_size),
        "mtime_ns": int(getattr(info, "st_mtime_ns", int(info.st_mtime * 1e9))),
    }


def _identity_same(left, right):
    fields = ("device", "inode", "size", "mtime_ns")
    return (isinstance(left, dict) and isinstance(right, dict)
            and all(left.get(field) == right.get(field) for field in fields))


def _validate_file_identity(value):
    if not isinstance(value, dict) or set(value) != {
            "device", "inode", "size", "mtime_ns"}:
        raise LifecycleError("store_unavailable")
    clean = {}
    for field in ("device", "inode", "size", "mtime_ns"):
        item = value.get(field)
        if isinstance(item, bool) or not isinstance(item, int) or item < 0:
            raise LifecycleError("store_unavailable")
        clean[field] = item
    return clean


def _content_digest(value):
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"),
                     ensure_ascii=False, allow_nan=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _profile_fingerprint(profile):
    """Security-relevant immutable launch facts (operator gates excluded)."""
    profile = _validate_profile(profile)
    return _content_digest({
        name: profile[name] for name in (
            "node_id", "host_id", "convoy_id", "project_root", "comp_path",
            "toe_path", "toe_identity", "executable_path",
            "executable_identity", "td_build", "platform", "session")
    })


def _launch_profile_digest(profile, *, include_gates=True):
    profile = _validate_profile(profile)
    value = {"fingerprint": _profile_fingerprint(profile)}
    if include_gates:
        value.update({"enabled": profile["enabled"],
                      "launch_eligible": profile["launch_eligible"]})
    return _content_digest(value)


def _launch_unit_id(profile):
    profile = _validate_profile(profile)
    return _content_digest({
        name: profile[name] for name in (
            "toe_path", "toe_identity", "executable_path",
            "executable_identity", "platform", "session")
    })


def _lock_unit_id(profile):
    """Serialization-lock identity that is immutable for one operation.

    Unlike :func:`_launch_unit_id`, this deliberately excludes the mutable
    file identities (``toe_identity``/``executable_identity``): a
    ``save_then_restart`` rewrites ``toe_identity`` mid-operation via
    :meth:`_refresh_saved_toe`, so a launch-unit-keyed lock would change
    identity while the live thread still held the lock minted from the old
    id -- letting the recovery loop acquire a *different* Lock object and race
    the live post-commit restart it is meant to exclude. Keying on the exact
    paths + platform + session is stable across a save yet still groups every
    node that shares one TouchDesigner process (a shared runtime).
    """
    profile = _validate_profile(profile)
    return _content_digest({
        name: profile[name] for name in (
            "toe_path", "executable_path", "platform", "session")
    })


def _token_hash(token):
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _atomic_replace(source, destination, *, replace=None, sleep=None):
    """Atomic replace with bounded Windows sharing-violation retries."""
    replace = replace or os.replace
    sleep = sleep or time.sleep
    for attempt in range(8):
        try:
            replace(source, destination)
            return
        except PermissionError:
            if attempt == 7:
                raise
            if attempt:
                sleep(0.005 * attempt)


def _validate_process_snapshot(value):
    if not isinstance(value, dict):
        raise LifecycleError("runtime_unverifiable")
    pid = value.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int) or not 1 <= pid <= 2 ** 63 - 1:
        raise LifecycleError("runtime_unverifiable")
    executable = _canonical_path(value.get("executable_path"))
    birth = _bounded_text(value.get("birth_id"), "birth_id", limit=512)
    user = _bounded_text(value.get("user_id"), "user_id", limit=512)
    session = _bounded_text(value.get("session_id"), "session_id", limit=512)
    return {"pid": pid, "executable_path": executable, "birth_id": birth,
            "user_id": user, "session_id": session}


def _process_same(left, right):
    try:
        left = _validate_process_snapshot(left)
        right = _validate_process_snapshot(right)
    except LifecycleError:
        return False
    return (left["pid"] == right["pid"]
            and _path_same(left["executable_path"], right["executable_path"])
            and left["birth_id"] == right["birth_id"]
            and left["user_id"] == right["user_id"]
            and left["session_id"] == right["session_id"])


def _validate_profile(value):
    if not isinstance(value, dict):
        raise LifecycleError("store_unavailable")
    node_id = _stable_id(value.get("node_id"), "node_id")
    host_id = _stable_id(value.get("host_id"), "host_id")
    convoy_id = _bounded_text(value.get("convoy_id"), "convoy_id", limit=128)
    project_root = _canonical_path(value.get("project_root"))
    toe_path = _canonical_path(value.get("toe_path"))
    executable_path = _canonical_path(value.get("executable_path"))
    if not _path_within(project_root, toe_path):
        raise LifecycleError("store_unavailable")
    if os.path.splitext(toe_path)[1].lower() != ".toe":
        raise LifecycleError("store_unavailable")
    if not isinstance(value.get("enabled"), bool) or not isinstance(
            value.get("launch_eligible"), bool):
        raise LifecycleError("store_unavailable")
    platform = _platform_name(value.get("platform"))
    comp_path = _bounded_text(value.get("comp_path"), "comp_path", limit=512)
    td_build = _bounded_text(value.get("td_build"), "td_build", limit=128)
    toe_identity = _validate_file_identity(value.get("toe_identity"))
    executable_identity = _validate_file_identity(
        value.get("executable_identity"))
    session = value.get("session")
    if (not isinstance(session, dict)
            or not isinstance(session.get("interactive"), bool)):
        raise LifecycleError("store_unavailable")
    session = {
        "user_id": _bounded_text(session.get("user_id"), "user_id", limit=512),
        "session_id": _bounded_text(session.get("session_id"), "session_id", limit=512),
        "interactive": session["interactive"],
    }
    last_runtime = value.get("last_runtime")
    if last_runtime is not None:
        if not isinstance(last_runtime, dict):
            raise LifecycleError("store_unavailable")
        last_runtime = {
            "runtime_id": _runtime_id(last_runtime.get("runtime_id")),
            "process": _validate_process_snapshot(last_runtime.get("process")),
        }
    updated_at = value.get("updated_at")
    if (isinstance(updated_at, bool) or not isinstance(updated_at, (int, float))
            or not math.isfinite(float(updated_at))):
        raise LifecycleError("store_unavailable")
    return {
        "node_id": node_id, "host_id": host_id, "convoy_id": convoy_id,
        "project_root": project_root, "comp_path": comp_path,
        "toe_path": toe_path, "toe_identity": _clone(toe_identity),
        "executable_path": executable_path,
        "executable_identity": _clone(executable_identity),
        "td_build": td_build, "platform": platform, "session": session,
        "enabled": value["enabled"],
        "launch_eligible": value["launch_eligible"],
        "last_runtime": last_runtime, "updated_at": float(updated_at),
    }


def _validate_attempt(value):
    if not isinstance(value, dict):
        raise LifecycleError("store_unavailable")
    operation_id = _operation_id(value.get("operation_id"))
    node_id = _stable_id(value.get("node_id"), "node_id")
    convoy_id = _bounded_text(value.get("convoy_id"), "convoy_id", limit=128)
    operation = value.get("operation")
    if operation not in ("start", "restart"):
        raise LifecycleError("store_unavailable")
    state = value.get("state")
    if state not in ACTIVE_ATTEMPT_STATES | TERMINAL_ATTEMPT_STATES:
        raise LifecycleError("store_unavailable")
    digest = value.get("content_digest")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise LifecycleError("store_unavailable")
    created_at = value.get("created_at")
    updated_at = value.get("updated_at")
    for stamp in (created_at, updated_at):
        if (isinstance(stamp, bool) or not isinstance(stamp, (int, float))
                or not math.isfinite(float(stamp))):
            raise LifecycleError("store_unavailable")
    clean = _clone(value)
    clean.update({"operation_id": operation_id, "node_id": node_id,
                  "convoy_id": convoy_id, "operation": operation,
                  "state": state, "content_digest": digest,
                  "created_at": float(created_at), "updated_at": float(updated_at)})
    # The ledger is host-private but still bounded/JSON-only.
    try:
        raw = json.dumps(clean, allow_nan=False, ensure_ascii=False)
    except (TypeError, ValueError):
        raise LifecycleError("store_unavailable")
    if len(raw.encode("utf-8")) > 64 * 1024:
        raise LifecycleError("store_unavailable")
    return clean


class LaunchProfileStore:
    """Atomic host-private launch profiles and lifecycle attempt fences."""

    def __init__(self, data_dir, *, clock=None):
        self.data_dir = _canonical_path(data_dir)
        self.path = os.path.join(self.data_dir, STATE_FILE)
        self._clock = clock or time.time
        self._lock = threading.RLock()
        self._state = self._load()

    def _empty(self):
        return {"schema": STATE_SCHEMA, "profiles": {}, "attempts": {}}

    def _load(self):
        os.makedirs(self.data_dir, mode=0o700, exist_ok=True)
        if os.path.islink(self.path):
            raise LifecycleError("store_unavailable")
        try:
            size = os.path.getsize(self.path)
        except FileNotFoundError:
            return self._empty()
        except OSError:
            raise LifecycleError("store_unavailable")
        if size > MAX_STATE_BYTES:
            raise LifecycleError("store_unavailable")
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                state = json.load(handle)
        except (OSError, ValueError):
            raise LifecycleError("store_unavailable")
        if (not isinstance(state, dict) or set(state) != {
                "schema", "profiles", "attempts"}
                or state.get("schema") != STATE_SCHEMA
                or not isinstance(state.get("profiles"), dict)
                or not isinstance(state.get("attempts"), dict)):
            raise LifecycleError("store_unavailable")
        profiles = {}
        for node_id, profile in state["profiles"].items():
            clean = _validate_profile(profile)
            if node_id != clean["node_id"]:
                raise LifecycleError("store_unavailable")
            profiles[node_id] = clean
        attempts = {}
        for operation_id, attempt in state["attempts"].items():
            clean = _validate_attempt(attempt)
            if operation_id != clean["operation_id"]:
                raise LifecycleError("store_unavailable")
            attempts[operation_id] = clean
        return {"schema": STATE_SCHEMA, "profiles": profiles,
                "attempts": attempts}

    def _write(self, state):
        raw = json.dumps(state, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False) + "\n"
        if len(raw.encode("utf-8")) > MAX_STATE_BYTES:
            raise LifecycleError("store_unavailable")
        if os.path.islink(self.path):
            raise LifecycleError("store_unavailable")
        tmp = "%s.%s.%s.tmp" % (self.path, os.getpid(), secrets.token_hex(8))
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(tmp, flags, stat.S_IRUSR | stat.S_IWUSR)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            _atomic_replace(tmp, self.path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _commit(self, state):
        try:
            self._write(state)
        except LifecycleError:
            raise
        except Exception:
            raise LifecycleError("store_unavailable")
        self._state = state

    def get_profile(self, node_id):
        with self._lock:
            value = self._state["profiles"].get(node_id)
            return _clone(value) if value is not None else None

    def put_profile(self, profile):
        clean = _validate_profile(profile)
        with self._lock:
            state = _clone(self._state)
            state["profiles"][clean["node_id"]] = clean
            self._commit(state)
            return _clone(clean)

    def delete_profile(self, node_id):
        """Drop a node's launch profile; True when one existed.

        Used when a node row is forgotten (manual /nodes/forget or the
        stale-node eviction sweep) -- a profile without its directory row
        is unreachable by every start path and would otherwise sit in
        lifecycle.json for ever. Attempt history is untouched: attempts
        are keyed by operation, and their retention is the job store's
        concern, not the profile's.
        """
        _stable_id(node_id, "node_id")
        with self._lock:
            if node_id not in self._state["profiles"]:
                return False
            state = _clone(self._state)
            del state["profiles"][node_id]
            self._commit(state)
            return True

    def set_enabled(self, node_id, convoy_id, enabled, *, launch_eligible=None):
        _stable_id(node_id, "node_id")
        _bounded_text(convoy_id, "convoy_id", limit=128)
        if not isinstance(enabled, bool):
            raise LifecycleError("invalid_arguments")
        if launch_eligible is not None and not isinstance(launch_eligible, bool):
            raise LifecycleError("invalid_arguments")
        with self._lock:
            current = self._state["profiles"].get(node_id)
            if current is None:
                raise LifecycleError("unknown_profile")
            if current["convoy_id"] != convoy_id:
                raise LifecycleError("namespace_mismatch")
            changed = _clone(current)
            changed["enabled"] = enabled
            if launch_eligible is not None:
                changed["launch_eligible"] = launch_eligible
            changed["updated_at"] = float(self._clock())
            state = _clone(self._state)
            state["profiles"][node_id] = _validate_profile(changed)
            self._commit(state)
            return _clone(state["profiles"][node_id])

    def begin_attempt(self, operation_id, content):
        operation_id = _operation_id(operation_id)
        if not isinstance(content, dict):
            raise LifecycleError("invalid_arguments")
        node_id = _stable_id(content.get("node_id"), "node_id")
        convoy_id = _bounded_text(content.get("convoy_id"), "convoy_id", limit=128)
        operation = content.get("operation")
        if operation not in ("start", "restart"):
            raise LifecycleError("invalid_arguments")
        digest = _content_digest(content)
        now = float(self._clock())
        with self._lock:
            existing = self._state["attempts"].get(operation_id)
            if existing is not None:
                if existing["content_digest"] != digest:
                    raise LifecycleError("idempotency_conflict")
                return _clone(existing), False
            for attempt in self._state["attempts"].values():
                if (attempt["node_id"] == node_id
                        and attempt["state"] in ACTIVE_ATTEMPT_STATES):
                    raise LifecycleError("profile_busy")
            attempt = {
                "operation_id": operation_id, "node_id": node_id,
                "convoy_id": convoy_id, "operation": operation,
                "content_digest": digest, "state": "created",
                "created_at": now, "updated_at": now,
            }
            state = _clone(self._state)
            state["attempts"][operation_id] = attempt
            self._prune_attempts(state)
            self._commit(state)
            return _clone(attempt), True

    @staticmethod
    def _prune_attempts(state):
        attempts = state["attempts"]
        if len(attempts) <= MAX_ATTEMPTS:
            return
        removable = sorted(
            (a for a in attempts.values()
             if a.get("state") in TERMINAL_ATTEMPT_STATES
             # Indeterminate records are safety quarantine, not history: the
             # exact child may still exist and new operations consult this
             # ledger to prevent duplicate launches.
             and a.get("state") != "indeterminate"),
            key=lambda a: (a.get("updated_at", 0), a.get("operation_id", "")))
        while len(attempts) > MAX_ATTEMPTS and removable:
            attempts.pop(removable.pop(0)["operation_id"], None)
        if len(attempts) > MAX_ATTEMPTS:
            raise LifecycleError("store_unavailable")

    def get_attempt(self, operation_id):
        with self._lock:
            value = self._state["attempts"].get(operation_id)
            return _clone(value) if value is not None else None

    def update_attempt(self, operation_id, *, expected_states=None, **fields):
        operation_id = _operation_id(operation_id)
        with self._lock:
            current = self._state["attempts"].get(operation_id)
            if current is None:
                raise LifecycleError("store_unavailable")
            if expected_states is not None and current["state"] not in set(expected_states):
                return _clone(current), False
            changed = _clone(current)
            changed.update(_clone(fields))
            changed["operation_id"] = operation_id
            changed["updated_at"] = float(self._clock())
            clean = _validate_attempt(changed)
            state = _clone(self._state)
            state["attempts"][operation_id] = clean
            self._commit(state)
            return _clone(clean), True

    def confirm_attempt(self, operation_id, profile, result):
        """Atomically consume a token, finish its attempt, and bind runtime.

        The launch ACK and profile's last-runtime proof are one durability
        boundary.  Splitting them lets a host crash report success while the
        profile still identifies the process that was just replaced.

        An ``indeterminate`` attempt is confirmable too: a spawned-but-
        unregistered launch is quarantined there with its one-time token
        preserved precisely so its exact child can still confirm it. Only the
        caller (``confirm_registration``) reaches this after proving token +
        spawned-process + reservation, so the transition is safe.
        """
        operation_id = _operation_id(operation_id)
        clean_profile = _validate_profile(profile)
        if not isinstance(result, dict):
            raise LifecycleError("store_unavailable")
        with self._lock:
            current = self._state["attempts"].get(operation_id)
            if current is None:
                raise LifecycleError("store_unavailable")
            if current["state"] not in (
                    ACTIVE_ATTEMPT_STATES | {"indeterminate"}):
                return _clone(current), False
            if current["node_id"] != clean_profile["node_id"]:
                raise LifecycleError("store_unavailable")
            changed = _clone(current)
            changed.update({"state": "succeeded", "result": _clone(result),
                            "token_hash": "", "token_consumed": True,
                            "confirmed_at": float(self._clock()),
                            "updated_at": float(self._clock())})
            clean_attempt = _validate_attempt(changed)
            state = _clone(self._state)
            state["profiles"][clean_profile["node_id"]] = clean_profile
            state["attempts"][operation_id] = clean_attempt
            self._commit(state)
            return _clone(clean_attempt), True

    def recent_launch_count(self, node_id, *, launch_unit_id=None, now=None,
                            window_s=CRASH_LOOP_WINDOW_S):
        now = float(self._clock() if now is None else now)
        with self._lock:
            return sum(
                1 for attempt in self._state["attempts"].values()
                if ((attempt.get("launch_unit_id") == launch_unit_id
                     or (attempt.get("launch_unit_id") is None
                         and attempt.get("node_id") == node_id))
                    if launch_unit_id is not None
                    else attempt.get("node_id") == node_id)
                and (attempt.get("state") != "succeeded"
                     or isinstance(attempt.get("unstable_exit_at"),
                                   (int, float)))
                and isinstance(attempt.get(
                    "launch_started_at", attempt.get("spawned_at")),
                    (int, float))
                and float(attempt.get(
                    "launch_started_at", attempt.get("spawned_at")))
                >= now - float(window_s))

    def attempts_for_node(self, node_id):
        with self._lock:
            values = [a for a in self._state["attempts"].values()
                      if a.get("node_id") == node_id]
            values.sort(key=lambda a: (a.get("created_at", 0),
                                       a.get("operation_id", "")))
            return _clone(values)

    def committed_restarts(self):
        """Durable restart commits needing restoration or host reconciliation.

        Terminal entries are included because the lifecycle ledger may have
        recorded the executor verdict immediately before the HostApp crashed,
        while the user-facing HostStore delivery was still ``dispatching``.
        The HostStore load sweep correctly calls that delivery indeterminate;
        this stronger executor ledger is what can later reconcile it.
        """
        with self._lock:
            values = [
                attempt for attempt in self._state["attempts"].values()
                if (attempt.get("operation") == "restart"
                    and isinstance(attempt.get("committed_at"), (int, float))
                    and not isinstance(attempt.get("committed_at"), bool)
                    and attempt.get("state") in (
                        {"restart_committed", "launching",
                         "awaiting_registration"}
                        | TERMINAL_ATTEMPT_STATES))
            ]
            values.sort(key=lambda item: (
                item.get("committed_at", 0), item.get("operation_id", "")))
            return _clone(values)

    def stranded_attempts(self):
        """Non-committed active/indeterminate attempts needing reconciliation.

        These are start attempts (and pre-commit ``created`` restart attempts)
        a HostApp crash could strand ACTIVE forever -- wedging ``begin_attempt``
        at ``profile_busy`` for the node -- plus indeterminate quarantines whose
        spawned process may now be provably gone. Post-commit restart attempts
        are excluded: ``recover_committed_restarts`` owns their restoration.
        """
        with self._lock:
            values = []
            for attempt in self._state["attempts"].values():
                if attempt.get("state") not in (
                        ACTIVE_ATTEMPT_STATES | {"indeterminate"}):
                    continue
                if (attempt.get("operation") == "restart"
                        and isinstance(attempt.get("committed_at"), (int, float))
                        and not isinstance(attempt.get("committed_at"), bool)):
                    continue
                values.append(attempt)
            values.sort(key=lambda a: (a.get("created_at", 0),
                                       a.get("operation_id", "")))
            return _clone(values)

    def durable_launch_reservations(self):
        """Detached launch fences that must survive a HostApp restart.

        ``indeterminate`` is included deliberately: it is terminal as a user
        result but remains a safety quarantine because the exact spawned
        process may still exist. Succeeded/failed/cancelled attempts have
        either consumed or explicitly released their reservation.
        """
        with self._lock:
            values = []
            for attempt in self._state["attempts"].values():
                if attempt.get("state") not in (
                        ACTIVE_ATTEMPT_STATES | {"indeterminate"}):
                    continue
                reservation_id = attempt.get("reservation_id")
                launch_unit_id = attempt.get("launch_unit_id")
                # ``launching`` durably records its unit before asking the
                # runtime adapter to reserve it. A crash in that gap has no
                # child and no live fence to reconstruct.
                if reservation_id is None:
                    continue
                if (not isinstance(reservation_id, str)
                        or not reservation_id
                        or len(reservation_id.encode("utf-8")) > 256
                        or any(ord(char) < 32 or ord(char) == 127
                               for char in reservation_id)
                        or not isinstance(launch_unit_id, str)
                        or not re.fullmatch(r"[0-9a-f]{64}",
                                            launch_unit_id)):
                    raise LifecycleError("store_unavailable")
                values.append({
                    "node_id": attempt["node_id"],
                    "launch_unit_id": launch_unit_id,
                    "operation_id": attempt["operation_id"],
                    "reservation_id": reservation_id,
                })
            values.sort(key=lambda value: (
                value["launch_unit_id"], value["operation_id"]))
            return _clone(values)

    def attempts_for_launch_unit(self, launch_unit_id):
        with self._lock:
            values = [a for a in self._state["attempts"].values()
                      if a.get("launch_unit_id") == launch_unit_id]
            values.sort(key=lambda a: (a.get("created_at", 0),
                                       a.get("operation_id", "")))
            return _clone(values)

    def profiles_for_launch_unit(self, launch_unit_id):
        with self._lock:
            return [_clone(profile) for profile in self._state["profiles"].values()
                    if _launch_unit_id(profile) == launch_unit_id]


class _WindowsNativeBackend:
    """Small ctypes adapter.  Kept injectable so the branch runs in CI."""

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    SYNCHRONIZE = 0x00100000
    PROCESS_TERMINATE = 0x0001
    TOKEN_QUERY = 0x0008
    TOKEN_USER = 1
    WM_CLOSE = 0x0010
    WAIT_OBJECT_0 = 0x00000000
    WAIT_TIMEOUT = 0x00000102

    def __init__(self, *, ctypes_module=None):
        if ctypes_module is None:
            import ctypes as ctypes_module
        self.ctypes = ctypes_module
        self.kernel32 = ctypes_module.windll.kernel32
        self.advapi32 = ctypes_module.windll.advapi32
        self.user32 = ctypes_module.windll.user32
        # ctypes defaults to c_int return values, truncating 64-bit HANDLEs.
        # Declare the pointer-sized APIs used below on real Windows.
        try:
            from ctypes import wintypes
            self.kernel32.OpenProcess.argtypes = [
                wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            self.kernel32.OpenProcess.restype = wintypes.HANDLE
            self.kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            self.kernel32.CloseHandle.restype = wintypes.BOOL
            self.kernel32.WaitForSingleObject.argtypes = [
                wintypes.HANDLE, wintypes.DWORD]
            self.kernel32.WaitForSingleObject.restype = wintypes.DWORD
            self.kernel32.TerminateProcess.argtypes = [
                wintypes.HANDLE, wintypes.UINT]
            self.kernel32.TerminateProcess.restype = wintypes.BOOL
            self.kernel32.LocalFree.argtypes = [ctypes_module.c_void_p]
            self.kernel32.LocalFree.restype = ctypes_module.c_void_p
            self.advapi32.OpenProcessToken.argtypes = [
                wintypes.HANDLE, wintypes.DWORD,
                ctypes_module.POINTER(wintypes.HANDLE)]
            self.advapi32.OpenProcessToken.restype = wintypes.BOOL
            self.advapi32.ConvertSidToStringSidW.argtypes = [
                ctypes_module.c_void_p,
                ctypes_module.POINTER(ctypes_module.c_void_p)]
            self.advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
            self.user32.GetProcessWindowStation.argtypes = []
            self.user32.GetProcessWindowStation.restype = wintypes.HANDLE
            self.user32.GetUserObjectInformationW.argtypes = [
                wintypes.HANDLE, ctypes_module.c_int,
                ctypes_module.c_void_p, wintypes.DWORD,
                ctypes_module.POINTER(wintypes.DWORD)]
            self.user32.GetUserObjectInformationW.restype = wintypes.BOOL
            self.user32.GetWindowThreadProcessId.argtypes = [
                wintypes.HWND, ctypes_module.POINTER(wintypes.DWORD)]
            self.user32.GetWindowThreadProcessId.restype = wintypes.DWORD
            self.user32.PostMessageW.argtypes = [
                wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
            self.user32.PostMessageW.restype = wintypes.BOOL
        except Exception:
            pass

    def _user_sid(self, process_handle):
        c = self.ctypes
        token = c.c_void_p()
        if not self.advapi32.OpenProcessToken(
                process_handle, self.TOKEN_QUERY, c.byref(token)):
            return None
        try:
            needed = c.c_ulong(0)
            self.advapi32.GetTokenInformation(
                token, self.TOKEN_USER, None, 0, c.byref(needed))
            if not needed.value or needed.value > 1024 * 1024:
                return None
            buffer = c.create_string_buffer(needed.value)
            if not self.advapi32.GetTokenInformation(
                    token, self.TOKEN_USER, buffer, needed, c.byref(needed)):
                return None
            sid_pointer = c.cast(buffer, c.POINTER(c.c_void_p)).contents.value
            sid_text = c.c_void_p()
            if not sid_pointer or not self.advapi32.ConvertSidToStringSidW(
                    c.c_void_p(sid_pointer), c.byref(sid_text)):
                return None
            try:
                return c.wstring_at(sid_text)
            finally:
                self.kernel32.LocalFree(sid_text)
        finally:
            self.kernel32.CloseHandle(token)

    def inspect_status(self, pid):
        c = self.ctypes

        class FILETIME(c.Structure):
            _fields_ = [("low", c.c_ulong), ("high", c.c_ulong)]

        handle = self.kernel32.OpenProcess(
            self.PROCESS_QUERY_LIMITED_INFORMATION | self.SYNCHRONIZE,
            False, pid)
        if not handle:
            try:
                error = int(self.kernel32.GetLastError())
            except Exception:
                error = 0
            # Windows reports ERROR_INVALID_PARAMETER for a nonexistent PID.
            # ACCESS_DENIED is explicitly not "dead": protected/elevated
            # processes are alive but cannot provide exact identity proof.
            return {"status": "dead" if error == 87 else "unknown"}
        try:
            try:
                wait = self.kernel32.WaitForSingleObject(handle, 0)
                if wait == self.WAIT_OBJECT_0:
                    return {"status": "dead"}
                if wait != self.WAIT_TIMEOUT:
                    return {"status": "unknown"}
            except Exception:
                return {"status": "unknown"}
            size = c.c_ulong(32768)
            path = c.create_unicode_buffer(size.value)
            if not self.kernel32.QueryFullProcessImageNameW(
                    handle, 0, path, c.byref(size)):
                return {"status": "unknown"}
            created, exited, kernel, user = FILETIME(), FILETIME(), FILETIME(), FILETIME()
            if not self.kernel32.GetProcessTimes(
                    handle, c.byref(created), c.byref(exited),
                    c.byref(kernel), c.byref(user)):
                return {"status": "unknown"}
            session = c.c_ulong()
            if not self.kernel32.ProcessIdToSessionId(pid, c.byref(session)):
                return {"status": "unknown"}
            user_sid = self._user_sid(handle)
            if not user_sid:
                return {"status": "unknown"}
            birth = (int(created.high) << 32) | int(created.low)
            return {"status": "alive", "process": {
                "pid": int(pid),
                "executable_path": path.value,
                "birth_id": "winfiletime:%d" % birth,
                "user_id": user_sid,
                "session_id": "windows:%d" % int(session.value),
            }}
        finally:
            self.kernel32.CloseHandle(handle)

    def inspect(self, pid):
        status = self.inspect_status(pid)
        return status.get("process") if status.get("status") == "alive" else None

    def current_session(self):
        current = self.inspect(os.getpid())
        if current is None:
            return None
        c = self.ctypes
        interactive = False
        try:
            station = self.user32.GetProcessWindowStation()
            needed = c.c_ulong()
            self.user32.GetUserObjectInformationW(
                station, 2, None, 0, c.byref(needed))  # UOI_NAME
            if needed.value and needed.value < 64 * 1024:
                name = c.create_unicode_buffer(needed.value)
                if self.user32.GetUserObjectInformationW(
                        station, 2, name, needed, c.byref(needed)):
                    interactive = name.value.casefold() == "winsta0"
        except Exception:
            interactive = False
        return {"user_id": current["user_id"],
                "session_id": current["session_id"],
                "interactive": interactive}

    def terminate(self, pid, *, force):
        if force:
            handle = self.kernel32.OpenProcess(self.PROCESS_TERMINATE, False, pid)
            if not handle:
                return False
            try:
                return bool(self.kernel32.TerminateProcess(handle, 1))
            finally:
                self.kernel32.CloseHandle(handle)
        c = self.ctypes
        posted = []
        callback_type = getattr(c, "WINFUNCTYPE", c.CFUNCTYPE)

        @callback_type(c.c_bool, c.c_void_p, c.c_void_p)
        def close_exact_window(hwnd, _):
            window_pid = c.c_ulong()
            self.user32.GetWindowThreadProcessId(hwnd, c.byref(window_pid))
            if int(window_pid.value) == int(pid):
                posted.append(bool(self.user32.PostMessageW(
                    hwnd, self.WM_CLOSE, 0, 0)))
            return True

        if not self.user32.EnumWindows(close_exact_window, 0):
            return False
        return any(posted)

    def terminate_exact(self, expected, *, force):
        expected = _validate_process_snapshot(expected)
        if not force:
            # Graceful window messages cannot target a process HANDLE. Recheck
            # each window owner's complete process birth immediately before
            # posting; normal restart uses the stronger in-TD quit CAS.
            c = self.ctypes
            posted = []
            callback_type = getattr(c, "WINFUNCTYPE", c.CFUNCTYPE)

            @callback_type(c.c_bool, c.c_void_p, c.c_void_p)
            def close_exact_window(hwnd, _):
                window_pid = c.c_ulong()
                self.user32.GetWindowThreadProcessId(hwnd, c.byref(window_pid))
                if int(window_pid.value) == expected["pid"]:
                    current = self.inspect_status(expected["pid"])
                    if (current.get("status") == "alive"
                            and _process_same(current.get("process"), expected)):
                        posted.append(bool(self.user32.PostMessageW(
                            hwnd, self.WM_CLOSE, 0, 0)))
                return True

            if not self.user32.EnumWindows(close_exact_window, 0):
                return False
            return any(posted)
        access = (self.PROCESS_QUERY_LIMITED_INFORMATION | self.SYNCHRONIZE
                  | self.PROCESS_TERMINATE)
        handle = self.kernel32.OpenProcess(access, False, expected["pid"])
        if not handle:
            return False
        try:
            # The terminate HANDLE remains bound to the old process object.
            # A PID reused after this point cannot redirect TerminateProcess.
            current = self.inspect_status(expected["pid"])
            if (current.get("status") != "alive"
                    or not _process_same(current.get("process"), expected)):
                return False
            if self.kernel32.WaitForSingleObject(handle, 0) != self.WAIT_TIMEOUT:
                return False
            return bool(self.kernel32.TerminateProcess(handle, 1))
        finally:
            self.kernel32.CloseHandle(handle)


class _MacNativeBackend:
    """Exact-PID macOS adapter; deliberately contains no app-wide API."""

    def __init__(self, *, ctypes_module=None, libproc=None, kill=None,
                 getsid=None, stat_fn=None, geteuid=None):
        if ctypes_module is None:
            import ctypes as ctypes_module
        self.ctypes = ctypes_module
        self.kill = kill or os.kill
        self.getsid = getsid or os.getsid
        self.stat = stat_fn or os.stat
        self.geteuid = geteuid or os.geteuid
        self.libproc = libproc or ctypes_module.CDLL("/usr/lib/libproc.dylib")

    def _unavailable_status(self, pid):
        try:
            self.kill(int(pid), 0)
        except ProcessLookupError:
            return {"status": "dead"}
        except (PermissionError, OSError, ValueError):
            return {"status": "unknown"}
        return {"status": "unknown"}

    def inspect_status(self, pid):
        c = self.ctypes

        class PROC_BSDINFO(c.Structure):
            _fields_ = [
                ("pbi_flags", c.c_uint32), ("pbi_status", c.c_uint32),
                ("pbi_xstatus", c.c_uint32), ("pbi_pid", c.c_uint32),
                ("pbi_ppid", c.c_uint32), ("pbi_uid", c.c_uint32),
                ("pbi_gid", c.c_uint32), ("pbi_ruid", c.c_uint32),
                ("pbi_rgid", c.c_uint32), ("pbi_svuid", c.c_uint32),
                ("pbi_svgid", c.c_uint32), ("rfu_1", c.c_uint32),
                ("pbi_comm", c.c_char * 16), ("pbi_name", c.c_char * 32),
                ("pbi_nfiles", c.c_uint32), ("pbi_pgid", c.c_uint32),
                ("pbi_pjobc", c.c_uint32), ("e_tdev", c.c_uint32),
                ("e_tpgid", c.c_uint32), ("pbi_nice", c.c_int32),
                ("pbi_start_tvsec", c.c_uint64),
                ("pbi_start_tvusec", c.c_uint64),
            ]

        path = c.create_string_buffer(4096)
        length = self.libproc.proc_pidpath(int(pid), path, len(path))
        if not length:
            return self._unavailable_status(pid)
        info = PROC_BSDINFO()
        received = self.libproc.proc_pidinfo(
            int(pid), 3, 0, c.byref(info), c.sizeof(info))  # PROC_PIDTBSDINFO
        if received != c.sizeof(info) or int(info.pbi_pid) != int(pid):
            return self._unavailable_status(pid)
        return {"status": "alive", "process": {
            "pid": int(pid),
            "executable_path": os.fsdecode(path.value),
            "birth_id": "mac-time:%d.%06d" % (
                int(info.pbi_start_tvsec), int(info.pbi_start_tvusec)),
            "user_id": "uid:%d" % int(info.pbi_uid),
            "session_id": "macos:%s" % self.getsid(int(pid)),
        }}

    def inspect(self, pid):
        status = self.inspect_status(pid)
        return status.get("process") if status.get("status") == "alive" else None

    def current_session(self):
        try:
            owner = int(self.stat("/dev/console").st_uid)
            euid = int(self.geteuid())
            session = self.getsid(0)
        except (OSError, ValueError):
            return None
        return {"user_id": "uid:%d" % euid,
                "session_id": "macos:%s" % session,
                "interactive": owner != 0 and owner == euid}

    def terminate(self, pid, *, force):
        try:
            self.kill(int(pid), getattr(signal, "SIGKILL", 9)
                      if force else getattr(signal, "SIGTERM", 15))
            return True
        except (OSError, ValueError):
            return False

    def terminate_exact(self, expected, *, force):
        # macOS has no pidfd/process-HANDLE equivalent. libproc birth is
        # revalidated immediately before the exact PID signal; a residual
        # inspect-to-kill race remains a documented Apple-hardware release
        # gate and is why no name/app-wide fallback exists.
        expected = _validate_process_snapshot(expected)
        status = self.inspect_status(expected["pid"])
        if (status.get("status") != "alive"
                or not _process_same(status.get("process"), expected)):
            return False
        return self.terminate(expected["pid"], force=force)


class LocalProcessInspector:
    """Validate and terminate one process without name-based operations."""

    def __init__(self, *, platform=None, backend=None, windows_backend=None,
                 mac_backend=None, clock=None, sleep=None):
        self.platform = _platform_name(platform)
        if backend is not None:
            self.backend = backend
        elif self.platform == "win32":
            self.backend = windows_backend or _WindowsNativeBackend()
        elif self.platform == "darwin":
            self.backend = mac_backend or _MacNativeBackend()
        else:
            raise LifecycleError("invalid_arguments",
                                 "TouchDesigner lifecycle supports Windows and macOS")
        self._clock = clock or time.monotonic
        self._sleep = sleep or time.sleep

    def inspect_status(self, pid):
        if isinstance(pid, bool) or not isinstance(pid, int) or pid < 1:
            return {"status": "unknown"}
        try:
            if hasattr(self.backend, "inspect_status"):
                result = self.backend.inspect_status(pid)
                if not isinstance(result, dict) or result.get("status") not in {
                        "alive", "dead", "unknown"}:
                    return {"status": "unknown"}
                if result["status"] != "alive":
                    return {"status": result["status"]}
                return {"status": "alive", "process":
                        _validate_process_snapshot(result.get("process"))}
            value = self.backend.inspect(pid)
            if value is None:
                # A legacy/injected adapter cannot distinguish ESRCH from an
                # inspection failure; ambiguity is never treated as death.
                return {"status": "unknown"}
            return {"status": "alive", "process":
                    _validate_process_snapshot(value)}
        except Exception:
            return {"status": "unknown"}

    def inspect(self, pid):
        result = self.inspect_status(pid)
        return result.get("process") if result["status"] == "alive" else None

    def current_session(self):
        try:
            value = self.backend.current_session()
            if (not isinstance(value, dict)
                    or not isinstance(value.get("interactive"), bool)):
                return None
            return {
                "user_id": _bounded_text(value.get("user_id"), "user_id", limit=512),
                "session_id": _bounded_text(value.get("session_id"), "session_id",
                                             limit=512),
                "interactive": value["interactive"],
            }
        except Exception:
            return None

    def terminate_exact(self, expected_process, *, force=False,
                        timeout_s=DEFAULT_QUIT_TIMEOUT_S):
        try:
            expected = _validate_process_snapshot(expected_process)
            timeout_s = _timeout(timeout_s, DEFAULT_QUIT_TIMEOUT_S)
        except LifecycleError:
            return False
        status = self.inspect_status(expected["pid"])
        if status["status"] == "dead":
            return True
        if status["status"] != "alive":
            return False
        current = status["process"]
        if not _process_same(current, expected):
            return False
        try:
            if hasattr(self.backend, "terminate_exact"):
                sent = self.backend.terminate_exact(expected, force=bool(force))
            else:
                sent = self.backend.terminate(expected["pid"], force=bool(force))
        except Exception:
            return False
        if not sent:
            return False
        deadline = self._clock() + timeout_s
        while self._clock() < deadline:
            status = self.inspect_status(expected["pid"])
            if status["status"] == "dead":
                return True
            if (status["status"] == "alive"
                    and not _process_same(status["process"], expected)):
                return True
            self._sleep(min(POLL_S, max(0.0, deadline - self._clock())))
        return False


class SpawnedProcess:
    """The owned process handle plus the exact post-spawn identity proof."""

    def __init__(self, popen, snapshot, reservation_id=""):
        self.popen = popen
        self.pid = int(popen.pid)
        self.snapshot = _validate_process_snapshot(snapshot)
        if self.snapshot["pid"] != self.pid:
            raise LifecycleError("launch_failed")
        self.reservation_id = _bounded_text(
            reservation_id, "reservation_id", limit=256, optional=True)

    def poll(self):
        return self.popen.poll()


class ExactProcessLauncher:
    """Direct-spawn only; remote callers cannot supply process arguments."""

    _REMOVE_ENV = frozenset({
        "PYTHONPATH", "PYTHONHOME", "QT_PLUGIN_PATH", "QML2_IMPORT_PATH",
    })

    def __init__(self, inspector, *, platform=None, popen_factory=None,
                 environment=None):
        self.inspector = inspector
        self.platform = _platform_name(platform or inspector.platform)
        self._popen = popen_factory or subprocess.Popen
        self._environment = dict(os.environ if environment is None else environment)

    def _env(self, profile, token, reservation_id):
        clean = {}
        for name, value in self._environment.items():
            upper = str(name).upper()
            if (upper in self._REMOVE_ENV or upper.startswith("DYLD_")
                    or upper.startswith("LD_") or upper.startswith("CONVOY_")):
                continue
            clean[str(name)] = str(value)
        clean.update({
            "EMBODY_CONVOY_LAUNCH_TOKEN": token,
            "EMBODY_CONVOY_NODE_ID": profile["node_id"],
            "EMBODY_CONVOY_ID": profile["convoy_id"],
            "EMBODY_CONVOY_LAUNCH_RESERVATION": reservation_id,
        })
        return clean

    def spawn(self, profile, token, reservation_id):
        profile = _validate_profile(profile)
        if not isinstance(token, str) or not _TOKEN.fullmatch(token):
            raise LifecycleError("launch_failed")
        reservation_id = _bounded_text(
            reservation_id, "reservation_id", limit=256)
        kwargs = {
            "cwd": profile["project_root"],
            "env": self._env(profile, token, reservation_id),
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "shell": False,
            "close_fds": True,
        }
        if self.platform == "win32":
            kwargs["creationflags"] = getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        elif self.platform == "darwin":
            # Remain in the HostApp's verified logged-in GUI session. setsid /
            # start_new_session would make the post-spawn session proof fail
            # and can detach the application from the intended login context.
            pass
        else:
            raise LifecycleError("launch_failed")
        try:
            process = self._popen(
                [profile["executable_path"], profile["toe_path"]], **kwargs)
            pid = int(process.pid)
        except Exception:
            raise LifecycleError("launch_failed")
        status = self.inspector.inspect_status(pid)
        snapshot = status.get("process")
        if status["status"] != "alive" or not _path_same(
                snapshot["executable_path"], profile["executable_path"]):
            try:
                process.terminate()
            except Exception:
                pass
            raise LifecycleError("launch_failed")
        if (snapshot["user_id"] != profile["session"]["user_id"]
                or snapshot["session_id"] != profile["session"]["session_id"]):
            try:
                process.terminate()
            except Exception:
                pass
            raise LifecycleError("launch_failed")
        return SpawnedProcess(process, snapshot, reservation_id)

    def cancel(self, spawned, *, timeout_s=DEFAULT_QUIT_TIMEOUT_S):
        if not isinstance(spawned, SpawnedProcess):
            return False
        return self.inspector.terminate_exact(
            spawned.snapshot, force=True, timeout_s=timeout_s)


class LifecycleManager:
    """Durable exact-node start/restart coordinator.

    This class intentionally exposes no generic command runner.  Its only
    process creation argv is ``[registered_executable, registered_toe]``.
    """

    def __init__(self, store, runtime_adapter, *, process_inspector=None,
                 launcher=None, local_policy=None, audit_callback=None,
                 clock=None, monotonic=None, sleep=None, token_factory=None,
                 crash_loop_max=CRASH_LOOP_MAX_LAUNCHES,
                 crash_loop_window_s=CRASH_LOOP_WINDOW_S,
                 launch_stability_window_s=LAUNCH_STABILITY_WINDOW_S):
        self.store = store
        self.runtime = runtime_adapter
        self.inspector = process_inspector or LocalProcessInspector()
        self.launcher = launcher or ExactProcessLauncher(self.inspector)
        self.local_policy = local_policy or (lambda node_id, policy: False)
        self.audit_callback = audit_callback
        self._clock = clock or time.time
        self._monotonic = monotonic or time.monotonic
        self._sleep = sleep or time.sleep
        self._token_factory = token_factory or (lambda: secrets.token_urlsafe(32))
        self.crash_loop_max = int(crash_loop_max)
        self.crash_loop_window_s = float(crash_loop_window_s)
        self.launch_stability_window_s = float(launch_stability_window_s)
        self._locks_guard = threading.Lock()
        self._locks = {}
        self._spawned_guard = threading.Lock()
        self._spawned = {}
        # operation_ids of start/restart operations executing in THIS process
        # right now. The recovery loop and stranded-attempt reconciliation must
        # never touch an attempt a live in-process op still owns, independent of
        # the launch-unit lock.
        self._live_operations_guard = threading.Lock()
        self._live_operations = set()

    def _enter_operation(self, operation_id):
        with self._live_operations_guard:
            self._live_operations.add(operation_id)

    def _exit_operation(self, operation_id):
        with self._live_operations_guard:
            self._live_operations.discard(operation_id)

    def _operation_is_live(self, operation_id):
        with self._live_operations_guard:
            return operation_id in self._live_operations

    def restore_launch_reservations(self):
        """Rebuild crash-surviving directory fences before serving IPC.

        The attempt ledger is the durable authority. The runtime adapter owns
        the live registration fence, so restoration is an explicit startup
        transaction rather than an incidental side effect of the first
        lifecycle request.

        Stranded start/pre-commit attempts are reconciled first, at load, so a
        HostApp crash mid ``start_node`` (or a pre-commit restart) can never
        leave an attempt ACTIVE forever (which would wedge every future
        lifecycle call for the node at ``profile_busy``), and a proven-gone
        indeterminate quarantine releases its fence instead of bricking the
        node. Only the surviving fences are then rebuilt in the adapter.
        """
        self.reconcile_stranded_attempts()
        reservations = self.store.durable_launch_reservations()
        try:
            result = self.runtime.restore_launch_reservations(reservations)
        except Exception:
            raise LifecycleError("launch_reservation_failed")
        if (not isinstance(result, dict) or result.get("ok") is not True
                or isinstance(result.get("restored"), bool)
                or not isinstance(result.get("restored"), int)
                or result.get("restored") != len(reservations)):
            raise LifecycleError("launch_reservation_failed")
        self._audit("launch_reservations_restored", code="ok")
        return _result(True, "ok", restored=len(reservations))

    def _audit(self, event, **fields):
        if self.audit_callback is None:
            return
        safe = {"event": str(event), "capability": HOST_LIFECYCLE_CAPABILITY}
        for name in ("node_id", "operation_id", "code", "state", "policy"):
            value = fields.get(name)
            if isinstance(value, (str, int, float, bool)):
                safe[name] = value
        try:
            self.audit_callback(safe)
        except Exception:
            pass

    def _lock_for(self, node_id):
        profile = self.store.get_profile(node_id)
        # An immutable lock unit (paths+session, never the mutable file
        # identities) so a save_then_restart that re-pins toe_identity cannot
        # change the lock id mid-operation and let recovery mint a different
        # Lock object for the attempt a live request already owns.
        lock_id = ("launch-unit:" + _lock_unit_id(profile)
                   if profile is not None else "node:" + node_id)
        with self._locks_guard:
            return self._locks.setdefault(lock_id, threading.Lock())

    def _acquire(self, node_id, cancel_event, deadline):
        lock = self._lock_for(node_id)
        while True:
            if lock.acquire(blocking=False):
                return lock
            if cancel_event is not None and cancel_event.is_set():
                raise LifecycleError("cancelled")
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise LifecycleError("deadline_exceeded")
            if lock.acquire(timeout=min(POLL_S, remaining)):
                return lock

    def _remaining(self, deadline, cancel_event=None):
        """Return one operation's remaining monotonic budget.

        Callers invoke this immediately before every potentially blocking
        pre-commit boundary and again after it.  Keeping the deadline
        absolute is what prevents lock wait, TD dirty/save calls, directory
        reservation, process spawn, and registration wait from each quietly
        receiving a fresh copy of the caller's timeout.
        """
        if cancel_event is not None and cancel_event.is_set():
            raise LifecycleError("cancelled")
        remaining = float(deadline) - float(self._monotonic())
        if remaining <= 0.0:
            raise LifecycleError("deadline_exceeded")
        return remaining

    def _deferred_flags(self, cancel_event, operation_deadline, *,
                        deadline_deferred=False):
        flags = {}
        if cancel_event is not None and cancel_event.is_set():
            flags["cancel_deferred"] = True
        if (deadline_deferred
                or (operation_deadline is not None
                    and self._monotonic() >= operation_deadline)):
            flags["deadline_deferred"] = True
        return flags

    def _terminal(self, attempt, result, state=None):
        state = state or ("succeeded" if result.get("ok") else "failed")
        clean, _ = self.store.update_attempt(
            attempt["operation_id"], expected_states=ACTIVE_ATTEMPT_STATES,
            state=state, result=result, token_hash="", token_consumed=True)
        self._audit("lifecycle_terminal", node_id=attempt["node_id"],
                    operation_id=attempt["operation_id"],
                    code=result.get("code"), state=clean.get("state"))
        return _clone(clean.get("result", result))

    def _quarantine_confirmable(self, attempt, result):
        """Quarantine a spawned-but-unregistered attempt WITHOUT consuming its
        one-time token.

        A launch that spawned an exact child and then missed its registration
        deadline is indeterminate as a user result, but the child may still be
        coming up. Preserving the token_hash + spawned fence lets that exact
        child confirm the attempt later (the strongest possible proof the
        launch succeeded), so the durable reservation can finally be released
        instead of bricking the node forever.
        """
        clean, _ = self.store.update_attempt(
            attempt["operation_id"], expected_states=ACTIVE_ATTEMPT_STATES,
            state="indeterminate", result=result)
        self._audit("lifecycle_terminal", node_id=attempt["node_id"],
                    operation_id=attempt["operation_id"],
                    code=result.get("code"), state=clean.get("state"))
        return _clone(clean.get("result", result))

    @staticmethod
    def _prior_result(attempt):
        if attempt.get("state") in TERMINAL_ATTEMPT_STATES:
            result = attempt.get("result")
            if isinstance(result, dict):
                return _clone(result)
            return _result(False, "indeterminate",
                           node_id=attempt.get("node_id"),
                           operation_id=attempt.get("operation_id"))
        return None

    def _persist_deferred(self, attempt, result, **flags):
        flags = {name: True for name in (
            "cancel_deferred", "deadline_deferred")
                 if flags.get(name) is True}
        if not flags:
            return _clone(result)
        changed_result = _clone(result)
        changed_result.update(flags)
        changed, updated = self.store.update_attempt(
            attempt["operation_id"], expected_states=TERMINAL_ATTEMPT_STATES,
            result=changed_result, **flags)
        return (_clone(changed.get("result", changed_result)) if updated
                else changed_result)

    def _persist_cancel_deferred(self, attempt, result):
        return self._persist_deferred(
            attempt, result, cancel_deferred=True)

    def _profile(self, node_id, convoy_id):
        profile = self.store.get_profile(node_id)
        if profile is None:
            raise LifecycleError("unknown_profile")
        if profile["convoy_id"] != convoy_id:
            raise LifecycleError("namespace_mismatch")
        if not profile["enabled"]:
            raise LifecycleError("profile_disabled")
        if not profile["launch_eligible"]:
            raise LifecycleError("launch_not_eligible")
        return profile

    def _ready(self, profile):
        session = self.inspector.current_session()
        if (session is None or not session.get("interactive")
                or session.get("user_id") != profile["session"]["user_id"]
                or session.get("session_id") != profile["session"]["session_id"]):
            raise LifecycleError("session_unavailable")
        toe_identity = _file_identity(profile["toe_path"])
        if toe_identity is None:
            raise LifecycleError("project_missing")
        if not _identity_same(toe_identity, profile["toe_identity"]):
            raise LifecycleError("project_changed")
        executable_identity = _file_identity(profile["executable_path"])
        if executable_identity is None:
            raise LifecycleError("executable_missing")
        if not _identity_same(executable_identity, profile["executable_identity"]):
            raise LifecycleError("executable_changed")

    def _refresh_saved_toe(self, profile):
        """Re-pin the exact file identity after an approved in-TD save."""
        identity = _file_identity(profile["toe_path"])
        if identity is None:
            raise LifecycleError("save_failed")
        refreshed = _clone(profile)
        refreshed["toe_identity"] = identity
        refreshed["updated_at"] = float(self._clock())
        return self.store.put_profile(refreshed)

    def _save_landed(self, toe_path, pre_save_identity):
        """Proof a save actually wrote the exact .toe on disk.

        Embody's own post-save housekeeping (Refresh sweep, TDN re-export,
        externalizations table write) re-dirties ``project.modified`` within
        seconds of a successful ``project.save()``, so ``project.modified``
        alone cannot prove a save failed. The file-identity delta -- the same
        fact ``_refresh_saved_toe`` re-pins -- is durable evidence the save
        landed regardless of that housekeeping dirt.
        """
        current = _file_identity(toe_path)
        return current is not None and not _identity_same(
            current, pre_save_identity)

    def _current(self, node_id):
        try:
            value = self.runtime.current(node_id)
        except Exception:
            raise LifecycleError("runtime_unverifiable")
        if value is not None and not isinstance(value, dict):
            raise LifecycleError("runtime_unverifiable")
        return _clone(value) if isinstance(value, dict) else None

    def _runtime_process(self, runtime, profile, *, require_last=False):
        if not isinstance(runtime, dict):
            raise LifecycleError("node_offline")
        if runtime.get("node_id") != profile["node_id"]:
            raise LifecycleError("runtime_changed")
        if runtime.get("convoy_id") != profile["convoy_id"]:
            raise LifecycleError("namespace_mismatch")
        if runtime.get("host_id") != profile["host_id"]:
            raise LifecycleError("runtime_changed")
        try:
            comp_path = _bounded_text(runtime.get("comp_path"), "comp_path",
                                      limit=512)
        except LifecycleError:
            raise LifecycleError("runtime_changed")
        if comp_path != profile["comp_path"]:
            raise LifecycleError("runtime_changed")
        metadata = runtime.get("metadata")
        if not isinstance(metadata, dict):
            raise LifecycleError("runtime_changed")
        try:
            toe_path = _canonical_path(metadata.get("toe_path"))
            td_build = _bounded_text(
                metadata.get("touchdesigner_version"), "touchdesigner_version",
                limit=128)
        except LifecycleError:
            raise LifecycleError("runtime_changed")
        if (not _path_same(toe_path, profile["toe_path"])
                or td_build != profile["td_build"]):
            raise LifecycleError("runtime_changed")
        runtime_id = _runtime_id(runtime.get("runtime_id"))
        pid = runtime.get("process_id", runtime.get("pid"))
        status = self.inspector.inspect_status(pid)
        if status["status"] != "alive":
            raise LifecycleError("runtime_unverifiable")
        process = status["process"]
        if (not _path_same(process["executable_path"], profile["executable_path"])
                or process["user_id"] != profile["session"]["user_id"]
                or process["session_id"] != profile["session"]["session_id"]):
            raise LifecycleError("runtime_unverifiable")
        if require_last:
            last = profile.get("last_runtime")
            if (last is None or last.get("runtime_id") != runtime_id
                    or not _process_same(last.get("process"), process)):
                raise LifecycleError("runtime_changed")
        return runtime_id, process

    def record_registration(self, node_record, executable_path, *,
                            launch_eligible=None):
        """Pin a profile from a locally authenticated live registration."""
        if (not isinstance(node_record, dict)
                or (launch_eligible is not None
                    and not isinstance(launch_eligible, bool))):
            raise LifecycleError("invalid_arguments")
        node_id = _stable_id(node_record.get("node_id"), "node_id")
        host_id = _stable_id(node_record.get("host_id"), "host_id")
        convoy_id = _bounded_text(node_record.get("convoy_id"), "convoy_id", limit=128)
        project_root = _canonical_path(node_record.get("project_root"))
        comp_path = _bounded_text(node_record.get("comp_path"), "comp_path", limit=512)
        metadata = node_record.get("metadata")
        if not isinstance(metadata, dict):
            raise LifecycleError("invalid_arguments")
        toe_path = _canonical_path(metadata.get("toe_path"))
        executable_path = _canonical_path(executable_path)
        if not _path_within(project_root, toe_path) or os.path.splitext(
                toe_path)[1].lower() != ".toe":
            raise LifecycleError("invalid_arguments")
        toe_identity = _file_identity(toe_path)
        executable_identity = _file_identity(executable_path)
        if toe_identity is None:
            raise LifecycleError("project_missing")
        if executable_identity is None:
            raise LifecycleError("executable_missing")
        pid = node_record.get("process_id", node_record.get("pid",
                              metadata.get("process_id")))
        process = self.inspector.inspect(pid)
        if process is None or not _path_same(
                process["executable_path"], executable_path):
            raise LifecycleError("runtime_unverifiable")
        session = self.inspector.current_session()
        if (session is None or not session.get("interactive")
                or process["user_id"] != session.get("user_id")
                or process["session_id"] != session.get("session_id")):
            raise LifecycleError("session_unavailable")
        runtime_id = _runtime_id(node_record.get("runtime_id"))
        td_build = metadata.get("touchdesigner_version",
                                metadata.get("td_build", ""))
        existing = self.store.get_profile(node_id)
        if existing is None:
            enabled = node_record.get("enabled")
            if not isinstance(enabled, bool) or not isinstance(
                    launch_eligible, bool):
                raise LifecycleError("invalid_arguments")
        else:
            if (existing["host_id"] != host_id
                    or existing["convoy_id"] != convoy_id):
                raise LifecycleError("namespace_mismatch")
            # Registration describes runtime facts. Only the explicit local
            # gate handler may change durable operator intent.
            enabled = existing["enabled"]
            launch_eligible = existing["launch_eligible"]
        profile = {
            "node_id": node_id, "host_id": host_id, "convoy_id": convoy_id,
            "project_root": project_root, "comp_path": comp_path,
            "toe_path": toe_path, "toe_identity": toe_identity,
            "executable_path": executable_path,
            "executable_identity": executable_identity,
            "td_build": _bounded_text(td_build, "td_build", limit=128),
            "platform": self.inspector.platform, "session": session,
            "enabled": enabled, "launch_eligible": launch_eligible,
            "last_runtime": {"runtime_id": runtime_id, "process": process},
            "updated_at": float(self._clock()),
        }
        return self.store.put_profile(profile)

    def set_enabled(self, node_id, convoy_id, enabled, *, launch_eligible=None):
        profile = self.store.set_enabled(
            node_id, convoy_id, enabled, launch_eligible=launch_eligible)
        if not enabled or launch_eligible is False:
            for attempt in reversed(self.store.attempts_for_node(node_id)):
                if (attempt.get("state") not in ACTIVE_ATTEMPT_STATES
                        or (attempt.get("operation") == "restart"
                            and attempt.get("committed_at") is not None)):
                    continue
                with self._spawned_guard:
                    spawned = self._spawned.get(attempt["operation_id"])
                self._cancel_launched_attempt(
                    attempt, spawned, DEFAULT_QUIT_TIMEOUT_S)
                break
        return profile

    def record_runtime_exit(self, node_id, runtime_id):
        """Classify a short-lived confirmed launch for crash-loop fencing.

        Host supervision calls this only after directory deregistration and
        exact process-exit proof. A successful registration is not considered
        stable merely because its one-time token was accepted.
        """
        node_id = _stable_id(node_id, "node_id")
        runtime_id = _runtime_id(runtime_id)
        profile = self.store.get_profile(node_id)
        if profile is None or profile.get("last_runtime") is None:
            return _result(False, "unknown_profile", node_id=node_id)
        last = profile["last_runtime"]
        if last["runtime_id"] != runtime_id:
            return _result(False, "runtime_changed", node_id=node_id)
        status = self.inspector.inspect_status(last["process"]["pid"])
        if (status["status"] == "unknown"
                or (status["status"] == "alive"
                    and _process_same(status["process"], last["process"]))):
            return _result(False, "runtime_unverifiable", node_id=node_id)
        now = float(self._clock())
        for attempt in reversed(self.store.attempts_for_node(node_id)):
            result = attempt.get("result")
            if (attempt.get("state") != "succeeded"
                    or not isinstance(result, dict)
                    or result.get("runtime_id") != runtime_id
                    or not isinstance(attempt.get("confirmed_at"), (int, float))):
                continue
            unstable = now - float(attempt["confirmed_at"]) \
                < self.launch_stability_window_s
            if unstable:
                self.store.update_attempt(
                    attempt["operation_id"], expected_states={"succeeded"},
                    unstable_exit_at=now, unstable_exit=True)
            return _result(True, "ok", node_id=node_id,
                           operation_id=attempt["operation_id"],
                           unstable_exit=unstable)
        return _result(False, "runtime_changed", node_id=node_id)

    def confirm_registration(self, node_id, convoy_id, launch_token,
                             runtime_record):
        """Consume one launch token only after exact node/process proof."""
        node_id = _stable_id(node_id, "node_id")
        convoy_id = _bounded_text(convoy_id, "convoy_id", limit=128)
        if not isinstance(launch_token, str) or not _TOKEN.fullmatch(launch_token):
            return _result(False, "launch_token_invalid", node_id=node_id)
        profile = self.store.get_profile(node_id)
        if profile is None:
            return _result(False, "unknown_profile", node_id=node_id)
        if profile["convoy_id"] != convoy_id:
            return _result(False, "namespace_mismatch", node_id=node_id)
        # ``indeterminate`` attempts are eligible too: a spawned-but-unregistered
        # launch is quarantined there with its token deliberately preserved so
        # its exact child can still confirm it (and release the fence). Attempts
        # whose token was consumed/wiped (regular terminal/cancel quarantines)
        # carry an empty token_hash and are excluded by the truthiness check.
        candidates = [a for a in self.store.attempts_for_node(node_id)
                      if a.get("state") in (
                          ACTIVE_ATTEMPT_STATES | {"indeterminate"})
                      and isinstance(a.get("token_hash"), str)
                      and a.get("token_hash")]
        candidates.sort(key=lambda item: item.get("updated_at", 0), reverse=True)
        if not candidates:
            return _result(False, "launch_token_replayed", node_id=node_id)
        attempt = candidates[0]
        include_gates = not (attempt.get("operation") == "restart"
                             and attempt.get("committed_at") is not None)
        if attempt.get("profile_digest") != _launch_profile_digest(
                profile, include_gates=include_gates):
            return _result(False, "profile_changed", node_id=node_id,
                           operation_id=attempt["operation_id"])
        if attempt.get("token_consumed"):
            return _result(False, "launch_token_replayed", node_id=node_id)
        try:
            supplied = _token_hash(launch_token)
        except (UnicodeEncodeError, AttributeError):
            return _result(False, "launch_token_invalid", node_id=node_id)
        if not hmac.compare_digest(supplied, attempt.get("token_hash", "")):
            return _result(False, "launch_token_invalid", node_id=node_id)
        try:
            runtime_id, process = self._runtime_process(runtime_record, profile)
        except LifecycleError:
            return _result(False, "registration_mismatch", node_id=node_id,
                           operation_id=attempt["operation_id"])
        spawned = attempt.get("spawned_process")
        # Popen returns before its identity can be durably fenced.  A very
        # early registration must retry after the launch attempt reaches
        # awaiting_registration; accepting it here would let any same-user
        # process holding the token choose the PID that becomes authoritative.
        # ``indeterminate`` is accepted only because the spawned/token fence is
        # preserved for exactly this confirmation (the strongest launch proof).
        if spawned is None or attempt.get("state") not in (
                "awaiting_registration", "indeterminate"):
            return _result(False, "launch_unconfirmed", node_id=node_id,
                           operation_id=attempt["operation_id"])
        if not _process_same(spawned, process):
            return _result(False, "registration_mismatch", node_id=node_id,
                           operation_id=attempt["operation_id"])
        if not self._confirm_reservation(attempt, runtime_id, runtime_record):
            return _result(False, "registration_mismatch", node_id=node_id,
                           operation_id=attempt["operation_id"])
        result = _result(True, "ok", node_id=node_id,
                         operation_id=attempt["operation_id"],
                         runtime_id=runtime_id)
        refreshed = _clone(profile)
        refreshed["last_runtime"] = {"runtime_id": runtime_id,
                                     "process": process}
        refreshed["updated_at"] = float(self._clock())
        changed, updated = self.store.confirm_attempt(
            attempt["operation_id"], refreshed, result)
        if not updated:
            return _result(False, "launch_token_replayed", node_id=node_id)
        # The launch ACK and refreshed profile are now durable. Only this
        # side of that boundary may consume the live directory reservation;
        # a failed store write leaves the fence intact so registration can
        # retry safely with the same one-time token.
        if not self._release_reservation(attempt, "confirmed"):
            self._audit("launch_reservation_release_failed", node_id=node_id,
                        operation_id=attempt["operation_id"],
                        code="launch_reservation_failed")
        self._audit("launch_confirmed", node_id=node_id,
                    operation_id=attempt["operation_id"], code="ok")
        return _clone(changed["result"])

    def _cancel_launched_attempt(self, current, spawned, timeout_s):
        """CAS token authority away before terminating the owned child."""
        cancelled = _result(False, "cancelled", node_id=current["node_id"],
                            operation_id=current["operation_id"])
        claimed, updated = self.store.update_attempt(
            current["operation_id"], expected_states=ACTIVE_ATTEMPT_STATES,
            state="cancelled", result=cancelled, token_hash="",
            token_consumed=True, cancellation_committed_at=float(self._clock()))
        if not updated:
            prior = self._prior_result(claimed)
            return prior if prior is not None else _result(
                False, "indeterminate", node_id=current["node_id"],
                operation_id=current["operation_id"])
        if spawned is not None and self.launcher.cancel(
                spawned, timeout_s=min(DEFAULT_QUIT_TIMEOUT_S, timeout_s)):
            self._release_reservation(current, "cancelled")
            self._audit("launch_cancelled", node_id=current["node_id"],
                        operation_id=current["operation_id"], code="cancelled")
            return cancelled
        uncertain = _result(False, "indeterminate", node_id=current["node_id"],
                            operation_id=current["operation_id"])
        changed, _ = self.store.update_attempt(
            current["operation_id"], expected_states={"cancelled"},
            state="indeterminate", result=uncertain)
        return _clone(changed.get("result", uncertain))

    def _reserve_launch(self, attempt, profile, timeout_s, cancel_event):
        launch_unit_id = _launch_unit_id(profile)
        try:
            result = self.runtime.reserve_launch(
                profile["node_id"], launch_unit_id, attempt["operation_id"],
                timeout_s, cancel_event)
        except Exception:
            raise LifecycleError("launch_reservation_failed")
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise LifecycleError("launch_reservation_failed")
        reservation_id = _bounded_text(
            result.get("reservation_id"), "reservation_id", limit=256)
        changed, updated = self.store.update_attempt(
            attempt["operation_id"], expected_states={"launching"},
            reservation_id=reservation_id, launch_unit_id=launch_unit_id)
        if not updated:
            try:
                self.runtime.release_launch_reservation(
                    profile["node_id"], launch_unit_id,
                    attempt["operation_id"], reservation_id, "superseded")
            except Exception:
                pass
            raise LifecycleError("launch_reservation_failed")
        return changed, reservation_id

    def _release_reservation(self, attempt, outcome):
        reservation_id = attempt.get("reservation_id")
        launch_unit_id = attempt.get("launch_unit_id")
        if not isinstance(reservation_id, str) or not reservation_id:
            return False
        try:
            result = self.runtime.release_launch_reservation(
                attempt["node_id"], launch_unit_id, attempt["operation_id"],
                reservation_id, outcome)
            return isinstance(result, dict) and result.get("ok") is True
        except Exception:
            return False

    def _confirm_reservation(self, attempt, runtime_id, runtime_record):
        reservation_id = attempt.get("reservation_id")
        if (not isinstance(reservation_id, str) or not reservation_id
                or runtime_record.get("launch_reservation_id") != reservation_id):
            return False
        try:
            result = self.runtime.confirm_launch_reservation(
                attempt["node_id"], attempt.get("launch_unit_id"),
                attempt["operation_id"], reservation_id, runtime_id)
            return isinstance(result, dict) and result.get("ok") is True
        except Exception:
            return False

    def _wait_for_launch(self, attempt, spawned, deadline, cancel_event,
                         *, cancel_deferred=False,
                         operation_deadline=None,
                         deadline_deferred=False):
        while self._monotonic() < deadline:
            current = self.store.get_attempt(attempt["operation_id"])
            prior = self._prior_result(current)
            if prior is not None:
                if cancel_deferred:
                    prior = self._persist_deferred(
                        current, prior,
                        **self._deferred_flags(
                            cancel_event, operation_deadline,
                            deadline_deferred=deadline_deferred))
                return prior
            if (cancel_event is not None and cancel_event.is_set()
                    and not cancel_deferred):
                return self._cancel_launched_attempt(
                    current, spawned,
                    max(0.0, deadline - self._monotonic()))
            if spawned is not None and spawned.poll() is not None:
                self._release_reservation(current, "process_exited")
                result = self._terminal(
                    current, _result(False, "launch_failed",
                                     node_id=current["node_id"],
                                     operation_id=current["operation_id"]))
                if cancel_deferred:
                    result = self._persist_deferred(
                        self.store.get_attempt(attempt["operation_id"]),
                        result,
                        **self._deferred_flags(
                            cancel_event, operation_deadline,
                            deadline_deferred=deadline_deferred))
                return result
            self._sleep(min(POLL_S, max(0.0, deadline - self._monotonic())))
        current = self.store.get_attempt(attempt["operation_id"])
        prior = self._prior_result(current)
        if prior is not None:
            if cancel_deferred:
                prior = self._persist_deferred(
                    current, prior,
                    **self._deferred_flags(
                        cancel_event, operation_deadline,
                        deadline_deferred=deadline_deferred))
            return prior
        code = ("launch_unconfirmed"
                if (cancel_deferred
                    or current.get("state") in (
                        "launching", "awaiting_registration"))
                else "deadline_exceeded")
        quarantine_result = _result(False, code,
                                    node_id=current["node_id"],
                                    operation_id=current["operation_id"])
        if (isinstance(current.get("spawned_process"), dict)
                and isinstance(current.get("token_hash"), str)
                and current.get("token_hash")
                and not current.get("token_consumed")):
            # An exact child was spawned but did not register in time. Preserve
            # the one-time token + spawned/reservation fence so that exact
            # child can still confirm this quarantine; otherwise the durable
            # reservation would brick the node forever with no clearing path.
            result = self._quarantine_confirmable(current, quarantine_result)
        else:
            result = self._terminal(
                current, quarantine_result, state="indeterminate")
        if cancel_deferred:
            result = self._persist_deferred(
                self.store.get_attempt(attempt["operation_id"]), result,
                **self._deferred_flags(
                    cancel_event, operation_deadline,
                    deadline_deferred=True))
        return result

    def _launch(self, attempt, profile, deadline, cancel_event,
                *, cancel_deferred=False, operation_deadline=None,
                deadline_deferred=False):
        launch_unit_id = _launch_unit_id(profile)

        def terminal(current, result, *, state=None):
            value = self._terminal(current, result, state=state)
            if cancel_deferred:
                value = self._persist_deferred(
                    self.store.get_attempt(attempt["operation_id"]), value,
                    **self._deferred_flags(
                        cancel_event, operation_deadline,
                        deadline_deferred=deadline_deferred))
            return value

        if self.store.recent_launch_count(
                profile["node_id"], launch_unit_id=launch_unit_id,
                window_s=self.crash_loop_window_s
                ) >= self.crash_loop_max:
            return terminal(
                attempt, _result(False, "crash_loop", node_id=profile["node_id"],
                                 operation_id=attempt["operation_id"]))
        if not cancel_deferred:
            try:
                self._remaining(deadline, cancel_event)
            except LifecycleError as exc:
                return terminal(
                    attempt, _result(False, exc.code,
                                     node_id=profile["node_id"],
                                     operation_id=attempt["operation_id"]),
                    state=("cancelled" if exc.code == "cancelled"
                           else "failed"))
        token = self._token_factory()
        if not isinstance(token, str) or not _TOKEN.fullmatch(token):
            return terminal(
                attempt, _result(False, "internal_error", node_id=profile["node_id"],
                                 operation_id=attempt["operation_id"]))
        include_gates = not (attempt.get("operation") == "restart"
                             and attempt.get("committed_at") is not None)
        profile_digest = _launch_profile_digest(
            profile, include_gates=include_gates)
        current, updated = self.store.update_attempt(
            attempt["operation_id"], expected_states={"created", "restart_committed"},
            state="launching", token_hash=_token_hash(token),
            token_consumed=False, profile_digest=profile_digest,
            launch_unit_id=launch_unit_id,
            launch_started_at=float(self._clock()))
        if not updated:
            prior = self._prior_result(current)
            if prior is not None:
                if cancel_deferred:
                    prior = self._persist_deferred(
                        current, prior,
                        **self._deferred_flags(
                            cancel_event, operation_deadline,
                            deadline_deferred=deadline_deferred))
                return prior
            return self._wait_for_launch(
                current, None, deadline, cancel_event,
                cancel_deferred=cancel_deferred,
                operation_deadline=operation_deadline,
                deadline_deferred=deadline_deferred)
        if _launch_profile_digest(
                self.store.get_profile(profile["node_id"]),
                include_gates=include_gates) != profile_digest:
            return terminal(
                current, _result(False, "profile_changed", node_id=profile["node_id"],
                                 operation_id=attempt["operation_id"]))
        try:
            remaining = max(0.001, deadline - self._monotonic())
            if not cancel_deferred:
                remaining = self._remaining(deadline, cancel_event)
            current, reservation_id = self._reserve_launch(
                current, profile, remaining,
                None if cancel_deferred else cancel_event)
            if not cancel_deferred:
                self._remaining(deadline, cancel_event)
            spawned = self.launcher.spawn(profile, token, reservation_id)
        except LifecycleError as exc:
            self._release_reservation(current, "launch_failed")
            return terminal(
                current, _result(False, exc.code, node_id=profile["node_id"],
                                 operation_id=attempt["operation_id"]),
                state=("cancelled" if exc.code == "cancelled" else None))
        with self._spawned_guard:
            self._spawned[attempt["operation_id"]] = spawned
        stamped, updated = self.store.update_attempt(
            attempt["operation_id"], expected_states={"launching"},
            state="awaiting_registration", spawned_pid=spawned.pid,
            spawned_process=spawned.snapshot, spawned_at=float(self._clock()))
        if not updated:
            prior = self._prior_result(stamped)
            if prior is not None:
                if (stamped.get("state") in {"cancelled", "indeterminate"}
                        and self.launcher.cancel(
                            spawned, timeout_s=min(
                                DEFAULT_QUIT_TIMEOUT_S,
                                max(0.0, deadline - self._monotonic())))):
                    self._release_reservation(stamped, "cancelled")
                elif stamped.get("state") in {"cancelled", "indeterminate"}:
                    prior = _result(False, "indeterminate",
                                    node_id=profile["node_id"],
                                    operation_id=attempt["operation_id"])
                if cancel_deferred:
                    prior = self._persist_deferred(
                        stamped, prior,
                        **self._deferred_flags(
                            cancel_event, operation_deadline,
                            deadline_deferred=deadline_deferred))
                return prior
        try:
            return self._wait_for_launch(
                stamped, spawned, deadline, cancel_event,
                cancel_deferred=cancel_deferred,
                operation_deadline=operation_deadline,
                deadline_deferred=deadline_deferred)
        finally:
            with self._spawned_guard:
                self._spawned.pop(attempt["operation_id"], None)

    def _refuse_live_unconfirmed(self, profile):
        """Fence a child left alive by an earlier unconfirmed attempt."""
        launch_unit_id = _launch_unit_id(profile)
        for candidate in self.store.profiles_for_launch_unit(launch_unit_id):
            if candidate["node_id"] == profile["node_id"]:
                continue
            last = candidate.get("last_runtime")
            if last is None:
                continue
            status = self.inspector.inspect_status(last["process"]["pid"])
            if status["status"] == "unknown":
                raise LifecycleError("runtime_unverifiable")
            if (status["status"] == "alive"
                    and _process_same(status["process"], last["process"])):
                raise LifecycleError("orphan_runtime")
        attempts = self.store.attempts_for_launch_unit(launch_unit_id)
        # Created/pre-launch legacy attempts have no unit yet.
        attempts.extend(a for a in self.store.attempts_for_node(
            profile["node_id"]) if a.get("launch_unit_id") is None)
        for attempt in reversed(sorted(
                attempts, key=lambda item: item.get("created_at", 0))):
            process = attempt.get("spawned_process")
            if not isinstance(process, dict):
                if (attempt.get("state") == "indeterminate"
                        and isinstance(attempt.get("launch_started_at"),
                                       (int, float))):
                    # Host loss between Popen and durable PID capture leaves
                    # no safe fact from which to infer that no child exists.
                    raise LifecycleError("indeterminate")
                continue
            status = self.inspector.inspect_status(process.get("pid"))
            if status["status"] == "unknown":
                raise LifecycleError("runtime_unverifiable")
            if (status["status"] == "alive"
                    and _process_same(status["process"], process)):
                raise LifecycleError("orphan_runtime")

    def _spawned_proven_gone(self, spawned):
        """True only when the exact spawned process is provably not running.

        ``dead`` or a different birth at the same PID both mean gone; an
        ``unknown`` probe is never treated as death.
        """
        status = self.inspector.inspect_status(spawned.get("pid"))
        return status["status"] == "dead" or (
            status["status"] == "alive"
            and not _process_same(status["process"], spawned))

    def _fail_stranded(self, attempt, *, release):
        code = "launch_failed"
        if release:
            self._release_reservation(attempt, "process_dead")
        self.store.update_attempt(
            attempt["operation_id"],
            expected_states=ACTIVE_ATTEMPT_STATES | {"indeterminate"},
            state="failed", token_hash="", token_consumed=True,
            result=_result(False, code, node_id=attempt["node_id"],
                           operation_id=attempt["operation_id"]))

    def _reconcile_stranded_attempt(self, attempt):
        """Resolve one crash-stranded start/pre-commit attempt.

        Without this a HostApp crash between ``begin_attempt`` and the terminal
        write leaves an attempt ACTIVE forever, wedging every future lifecycle
        call for the node at ``profile_busy`` (MAJOR), and an ``indeterminate``
        quarantine keeps its durable reservation with no clearing path once the
        child is gone (BLOCKER). Verdicts:

        * ``created`` -> failed (no external effect could exist);
        * a spawned process proven gone -> release the fence and fail;
        * an alive-and-matching child, or an ``unknown`` probe -> left intact so
          a live registering child can still confirm it;
        * a reservation with no durable child (crash in the Popen->PID gap) ->
          quarantine indeterminate, keeping the fence (a child may exist);
        * no reservation and no child -> failed (nothing was ever launched).
        """
        operation_id = attempt["operation_id"]
        node_id = attempt["node_id"]
        state = attempt.get("state")
        spawned = attempt.get("spawned_process")
        if state == "created":
            self._fail_stranded(attempt, release=False)
            return "failed"
        if isinstance(spawned, dict):
            if self._spawned_proven_gone(spawned):
                self._fail_stranded(attempt, release=True)
                return "failed"
            return "kept"
        if attempt.get("reservation_id") is not None:
            if state != "indeterminate":
                self.store.update_attempt(
                    operation_id, expected_states=ACTIVE_ATTEMPT_STATES,
                    state="indeterminate",
                    result=_result(False, "indeterminate", node_id=node_id,
                                   operation_id=operation_id))
            return "quarantined"
        self._fail_stranded(attempt, release=False)
        return "failed"

    def reconcile_stranded_attempts(self, node_id=None):
        """Fail/release/quarantine crash-stranded start/pre-commit attempts.

        Safe to run at load (before serving IPC) and as an operator-triggered
        reconciliation surface. Each attempt is resolved under its own
        launch-unit lock; any attempt a live in-process op still owns is
        skipped so a running request is never raced.
        """
        summary = {"examined": 0, "failed": 0, "quarantined": 0,
                   "kept": 0, "busy": 0}
        for candidate in self.store.stranded_attempts():
            if node_id is not None and candidate.get("node_id") != node_id:
                continue
            summary["examined"] += 1
            if self._operation_is_live(candidate["operation_id"]):
                summary["busy"] += 1
                continue
            lock = self._lock_for(candidate["node_id"])
            if not lock.acquire(blocking=False):
                summary["busy"] += 1
                continue
            try:
                attempt = self.store.get_attempt(candidate["operation_id"])
                if attempt is None or attempt.get("state") not in (
                        ACTIVE_ATTEMPT_STATES | {"indeterminate"}):
                    continue
                verdict = self._reconcile_stranded_attempt(attempt)
            finally:
                lock.release()
            summary[verdict] = summary.get(verdict, 0) + 1
        if summary["failed"] or summary["quarantined"]:
            self._audit("lifecycle_stranded_reconciled", code="ok")
        return summary

    def _release_dead_stranded(self, profile, current_op_id):
        """Release the fence of an earlier stranded attempt whose exact child
        is PROVEN gone, so a fresh start is not blocked by a dead attempt's
        reservation. Called with the launch-unit lock held; only proven-dead
        spawned attempts are touched (alive children and Popen-gap quarantines
        remain for :meth:`_refuse_live_unconfirmed` to fence)."""
        launch_unit_id = _launch_unit_id(profile)
        attempts = self.store.attempts_for_launch_unit(launch_unit_id)
        attempts.extend(a for a in self.store.attempts_for_node(
            profile["node_id"]) if a.get("launch_unit_id") is None)
        for attempt in attempts:
            if attempt.get("operation_id") == current_op_id:
                continue
            if attempt.get("state") not in (
                    ACTIVE_ATTEMPT_STATES | {"indeterminate"}):
                continue
            spawned = attempt.get("spawned_process")
            if isinstance(spawned, dict) and self._spawned_proven_gone(spawned):
                self._fail_stranded(attempt, release=True)

    def start_node(self, node_id, convoy_id, operation_id, *, timeout_s=None,
                   execution_timeout_s=None, cancel_event=None):
        try:
            node_id = _stable_id(node_id, "node_id")
            convoy_id = _bounded_text(convoy_id, "convoy_id", limit=128)
            operation_id = _operation_id(operation_id)
            requested_timeout_s = _timeout(timeout_s, DEFAULT_START_TIMEOUT_S)
            execution_timeout_s = _execution_timeout(
                execution_timeout_s, requested_timeout_s)
            deadline = self._monotonic() + execution_timeout_s
            lock = self._acquire(node_id, cancel_event, deadline)
        except LifecycleError as exc:
            return _result(False, exc.code, node_id=node_id if isinstance(node_id, str) else "",
                           operation_id=operation_id if isinstance(operation_id, str) else "")
        self._enter_operation(operation_id)
        try:
            content = {"operation": "start", "node_id": node_id,
                       "convoy_id": convoy_id,
                       "timeout_s": requested_timeout_s}
            try:
                attempt, created = self.store.begin_attempt(operation_id, content)
                prior = self._prior_result(attempt)
                if prior is not None:
                    return prior
                if not created and attempt["state"] != "created":
                    return self._wait_for_launch(attempt, None, deadline,
                                                 cancel_event)
                self._remaining(deadline, cancel_event)
                profile = self._profile(node_id, convoy_id)
                self._ready(profile)
                self._remaining(deadline, cancel_event)
                runtime = self._current(node_id)
                if runtime is not None:
                    self._runtime_process(runtime, profile)
                    return self._terminal(
                        attempt, _result(True, "already_running", node_id=node_id,
                                         operation_id=operation_id))
                last = profile.get("last_runtime")
                if last is not None:
                    status = self.inspector.inspect_status(
                        last["process"]["pid"])
                    if status["status"] == "unknown":
                        raise LifecycleError("runtime_unverifiable")
                    if (status["status"] == "alive"
                            and _process_same(status["process"],
                                              last["process"])):
                        raise LifecycleError("orphan_runtime")
                # Release the launch fence of any earlier stranded attempt for
                # this exact launch unit whose spawned process is PROVEN gone.
                # Without this, an indeterminate quarantine keeps its durable
                # reservation forever and every future start dies at
                # _reserve_launch with launch_reservation_failed.
                self._release_dead_stranded(profile, operation_id)
                self._refuse_live_unconfirmed(profile)
                self._remaining(deadline, cancel_event)
                return self._launch(attempt, profile, deadline, cancel_event)
            except LifecycleError as exc:
                result = _result(False, exc.code, node_id=node_id,
                                 operation_id=operation_id)
                if exc.code == "idempotency_conflict":
                    return result
                attempt = self.store.get_attempt(operation_id)
                return self._terminal(attempt, result) if attempt else result
            except Exception:
                attempt = self.store.get_attempt(operation_id)
                result = _result(False, "internal_error", node_id=node_id,
                                 operation_id=operation_id)
                return self._terminal(attempt, result) if attempt else result
        finally:
            self._exit_operation(operation_id)
            lock.release()

    def _runtime_call(self, name, node_id, runtime_id, timeout_s,
                      cancel_event, **kwargs):
        try:
            method = getattr(self.runtime, name)
            return method(node_id, runtime_id, timeout_s, cancel_event, **kwargs)
        except Exception:
            return {"ok": False}

    def _runtime_call_before_commit(self, name, node_id, runtime_id,
                                    deadline, cancel_event, **kwargs):
        remaining = self._remaining(deadline, cancel_event)
        result = self._runtime_call(
            name, node_id, runtime_id, remaining, cancel_event, **kwargs)
        # A callee is required to honor the supplied budget, but the manager
        # does not trust that promise at the irreversible boundary.  A late
        # response is expired work, never permission to continue to quit.
        self._remaining(deadline, cancel_event)
        return result

    @staticmethod
    def _dirty_snapshot(value):
        if (not isinstance(value, dict) or value.get("ok") is not True
                or not isinstance(value.get("dirty"), bool)
                or not isinstance(value.get("unsaved"), bool)):
            raise LifecycleError("dirty_state_unknown")
        revision = value.get("revision")
        if isinstance(revision, bool) or not isinstance(revision, (str, int)):
            raise LifecycleError("dirty_state_unknown")
        revision = str(revision)
        try:
            revision = _bounded_text(revision, "dirty_revision", limit=256)
        except LifecycleError:
            raise LifecycleError("dirty_state_unknown")
        return bool(value["dirty"] or value["unsaved"]), revision

    def _wait_exact_exit(self, process, deadline):
        while self._monotonic() < deadline:
            status = self.inspector.inspect_status(process["pid"])
            if status["status"] == "dead":
                return True
            if (status["status"] == "alive"
                    and not _process_same(status["process"], process)):
                return True
            self._sleep(min(POLL_S, max(0.0, deadline - self._monotonic())))
        return False

    def _shared_runtime_nodes(self, profile, process):
        siblings = []
        for candidate in self.store.profiles_for_launch_unit(
                _launch_unit_id(profile)):
            if candidate["node_id"] == profile["node_id"]:
                continue
            last = candidate.get("last_runtime")
            if last is not None and _process_same(last.get("process"), process):
                siblings.append(candidate["node_id"])
        return sorted(siblings)

    def _finish_committed_restart(self, attempt, profile, runtime_id, process,
                                  policy, dirty_revision, recovery_deadline,
                                  cancel_event, *, operation_deadline=None,
                                  deadline_deferred=False):
        """Reconcile/finish work after the durable quit commit.

        This path is safe to call after a HostApp restart.  Exact process
        birth decides whether the old runtime is gone; a missing IPC ACK does
        not.  Once committed, cancellation is observed but deferred until the
        replacement has been restored.
        """
        operation_id = attempt["operation_id"]
        node_id = profile["node_id"]

        def terminal(result, *, state=None):
            value = self._terminal(attempt, result, state=state)
            current = self.store.get_attempt(operation_id)
            return self._persist_deferred(
                current, value,
                **self._deferred_flags(
                    cancel_event, operation_deadline,
                    deadline_deferred=deadline_deferred))

        fingerprint = attempt.get("profile_fingerprint")
        if (not isinstance(fingerprint, str)
                or not hmac.compare_digest(fingerprint,
                                           _profile_fingerprint(profile))):
            return terminal(
                _result(False, "profile_changed", node_id=node_id,
                        operation_id=operation_id),
                state="indeterminate")
        if policy not in DESTRUCTIVE_RESTART_POLICIES and not dirty_revision:
            return terminal(
                _result(False, "indeterminate", node_id=node_id,
                        operation_id=operation_id),
                state="indeterminate")
        try:
            current_runtime = self._current(node_id)
        except LifecycleError:
            return terminal(
                _result(False, "indeterminate", node_id=node_id,
                        operation_id=operation_id),
                state="indeterminate")

        should_request_quit = False
        if current_runtime is not None:
            try:
                current_id, current_process = self._runtime_process(
                    current_runtime, profile, require_last=True)
            except LifecycleError:
                return terminal(
                    _result(False, "runtime_changed", node_id=node_id,
                            operation_id=operation_id),
                    state="indeterminate")
            if (current_id != runtime_id
                    or not _process_same(current_process, process)):
                return terminal(
                    _result(False, "runtime_changed", node_id=node_id,
                            operation_id=operation_id),
                    state="indeterminate")
            should_request_quit = True

        status = self.inspector.inspect_status(process["pid"])
        if status["status"] == "unknown":
            return terminal(
                _result(False, "indeterminate", node_id=node_id,
                        operation_id=operation_id),
                state="indeterminate")
        exited = (status["status"] == "dead"
                  or (status["status"] == "alive"
                      and not _process_same(status["process"], process)))
        # Size the quit/force/launch phases from the restoration budget
        # (recovery_deadline), NOT the caller's pre-commit request timeout: a
        # short caller timeout must never abandon a node already committed to
        # exit. recovery_deadline already caps every phase below.
        quit_phase_budget = DEFAULT_QUIT_TIMEOUT_S
        if (not exited and policy in DESTRUCTIVE_RESTART_POLICIES
                and self.local_policy(node_id, policy) is not True):
            return terminal(
                _result(False, "destructive_policy_disabled",
                        node_id=node_id, operation_id=operation_id),
                state="indeterminate")
        if not exited and should_request_quit:
            # The exact process exit is authoritative even if its IPC reply
            # is lost. The expected revision is a TD-main-thread CAS: a late
            # edit makes quit refuse rather than discarding it.
            remaining = max(0.0, recovery_deadline - self._monotonic())
            if remaining > 0.0:
                self._runtime_call(
                    "quit", node_id, runtime_id,
                    min(quit_phase_budget, remaining), None,
                    discard=policy in DESTRUCTIVE_RESTART_POLICIES,
                    expected_dirty_revision=(
                        None if policy in DESTRUCTIVE_RESTART_POLICIES
                        else dirty_revision))
        if not exited:
            exit_deadline = min(
                recovery_deadline,
                self._monotonic() + quit_phase_budget)
            exited = self._wait_exact_exit(process, exit_deadline)
        if not exited and policy == "force":
            remaining = max(0.0, recovery_deadline - self._monotonic())
            exited = self.inspector.terminate_exact(
                process, force=True,
                timeout_s=min(DEFAULT_QUIT_TIMEOUT_S, remaining))
        if not exited:
            return terminal(
                _result(False, "quit_failed", node_id=node_id,
                        operation_id=operation_id),
                state="indeterminate")
        try:
            self._ready(profile)
        except LifecycleError as exc:
            return terminal(
                _result(False, exc.code, node_id=node_id,
                        operation_id=operation_id))
        return self._launch(
            attempt, profile, recovery_deadline, cancel_event,
            cancel_deferred=True, operation_deadline=operation_deadline,
            deadline_deferred=deadline_deferred)

    def recover_committed_restarts(self, result_callback=None, *,
                                   recovery_timeout_s=None):
        """Autonomously restore durable post-commit restarts.

        ``restart_committed`` is safe to resume by launching anew. A
        pre-reservation ``launching`` record is rewound to that state; an
        ``awaiting_registration`` record waits for its already-proven child.
        A reservation-bearing ``launching`` record is deliberately made
        indeterminate because the crash may have occurred immediately after
        Popen but before the exact child snapshot was durable. Terminal
        committed attempts are reported to ``result_callback`` as well,
        allowing the HostApp to repair a user-facing delivery that its boot
        sweep conservatively made indeterminate between the two durable
        writes.

        A process-local, non-blocking launch-unit lock prevents the recovery
        loop racing a live request.  Persisted semantic arguments remain the
        idempotency authority; ``recovery_timeout_s`` is a new safety budget,
        never substituted into their digest.
        """
        recovery_timeout_s = _timeout(
            recovery_timeout_s, DEFAULT_RESTART_RECOVERY_TIMEOUT_S)
        summary = {"examined": 0, "recovered": 0, "reconciled": 0,
                   "busy": 0, "errors": 0, "results": []}
        for candidate in self.store.committed_restarts():
            summary["examined"] += 1
            callback_attempt = candidate
            result = self._prior_result(candidate)
            if result is not None:
                summary["reconciled"] += 1
            elif self._operation_is_live(candidate["operation_id"]):
                # A live in-process request already owns this operation_id;
                # never re-enter its finish path concurrently even if the
                # launch-unit lock were momentarily free between phases.
                summary["busy"] += 1
                continue
            else:
                lock = self._lock_for(candidate["node_id"])
                if not lock.acquire(blocking=False):
                    summary["busy"] += 1
                    continue
                try:
                    attempt = self.store.get_attempt(
                        candidate["operation_id"])
                    callback_attempt = attempt
                    result = self._prior_result(attempt)
                    if result is None:
                        state = attempt.get("state")
                        recovery_deadline = (
                            self._monotonic() + recovery_timeout_s)
                        if state == "launching":
                            if (attempt.get("reservation_id") is not None
                                    or attempt.get("spawned_process")
                                    is not None):
                                result = self._terminal(
                                    attempt, _result(
                                        False, "indeterminate",
                                        node_id=attempt["node_id"],
                                        operation_id=attempt[
                                            "operation_id"]),
                                    state="indeterminate")
                                result = self._persist_deferred(
                                    self.store.get_attempt(
                                        attempt["operation_id"]),
                                    result, deadline_deferred=True)
                                summary["recovered"] += 1
                                callback_attempt = self.store.get_attempt(
                                    attempt["operation_id"])
                                # The durable reservation remains a fence;
                                # never release it on this ambiguous boundary.
                                state = "indeterminate"
                            else:
                                attempt, rewound = self.store.update_attempt(
                                    attempt["operation_id"],
                                    expected_states={"launching"},
                                    state="restart_committed",
                                    token_hash="", token_consumed=True)
                                if not rewound:
                                    continue
                                state = "restart_committed"
                        if state == "awaiting_registration":
                            result = self._wait_for_launch(
                                attempt, None, recovery_deadline, None,
                                cancel_deferred=True,
                                operation_deadline=None,
                                deadline_deferred=True)
                            summary["recovered"] += 1
                            callback_attempt = self.store.get_attempt(
                                attempt["operation_id"])
                        elif state != "restart_committed":
                            if result is None:
                                continue
                        if result is None:
                            policy = attempt.get("restart_policy")
                            requested_timeout = attempt.get(
                                "requested_timeout_s")
                            if (policy not in RESTART_POLICIES
                                    or requested_timeout is None):
                                summary["errors"] += 1
                                continue
                            try:
                                _timeout(requested_timeout,
                                         DEFAULT_START_TIMEOUT_S)
                                process = _validate_process_snapshot(
                                    attempt.get("old_process"))
                                runtime_id = _runtime_id(
                                    attempt.get("old_runtime_id"))
                            except LifecycleError:
                                summary["errors"] += 1
                                continue
                            profile = self.store.get_profile(
                                attempt["node_id"])
                            if (profile is None
                                    or profile.get("convoy_id")
                                    != attempt.get("convoy_id")):
                                summary["errors"] += 1
                                continue
                            try:
                                result = self._finish_committed_restart(
                                    attempt, profile, runtime_id, process,
                                    policy, attempt.get("dirty_revision"),
                                    recovery_deadline, None,
                                    operation_deadline=None,
                                    deadline_deferred=True)
                            except Exception:
                                summary["errors"] += 1
                                continue
                            summary["recovered"] += 1
                            callback_attempt = self.store.get_attempt(
                                attempt["operation_id"])
                finally:
                    lock.release()
            if result is None:
                continue
            item = {"operation_id": candidate["operation_id"],
                    "result": _clone(result)}
            summary["results"].append(item)
            if result_callback is not None:
                try:
                    result_callback(callback_attempt, _clone(result))
                except Exception:
                    summary["errors"] += 1
        if summary["recovered"] or summary["errors"]:
            self._audit("restart_recovery_pass", code=(
                "ok" if not summary["errors"] else "indeterminate"))
        return summary

    def restart_node(self, node_id, convoy_id, operation_id,
                     expected_runtime_id, *, policy="require_clean",
                     timeout_s=None, execution_timeout_s=None,
                     cancel_event=None):
        try:
            node_id = _stable_id(node_id, "node_id")
            convoy_id = _bounded_text(convoy_id, "convoy_id", limit=128)
            operation_id = _operation_id(operation_id)
            expected_runtime_id = _runtime_id(expected_runtime_id)
            if policy not in RESTART_POLICIES:
                raise LifecycleError("invalid_arguments")
            requested_timeout_s = _timeout(timeout_s, DEFAULT_START_TIMEOUT_S)
            execution_timeout_s = _execution_timeout(
                execution_timeout_s, requested_timeout_s)
            deadline = self._monotonic() + execution_timeout_s
            lock = self._acquire(node_id, cancel_event, deadline)
        except LifecycleError as exc:
            return _result(False, exc.code, node_id=node_id if isinstance(node_id, str) else "",
                           operation_id=operation_id if isinstance(operation_id, str) else "")
        self._enter_operation(operation_id)
        try:
            content = {"operation": "restart", "node_id": node_id,
                       "convoy_id": convoy_id,
                       "expected_runtime_id": expected_runtime_id,
                       "policy": policy,
                       "timeout_s": requested_timeout_s}
            save_may_have_run = False
            save_landed = False
            try:
                attempt, created = self.store.begin_attempt(operation_id, content)
                prior = self._prior_result(attempt)
                if prior is not None:
                    return prior
                if not created and attempt["state"] != "created":
                    if attempt["state"] == "restart_committed":
                        profile = self.store.get_profile(node_id)
                        if profile is None:
                            raise LifecycleError("unknown_profile")
                        if profile["convoy_id"] != convoy_id:
                            raise LifecycleError("namespace_mismatch")
                        try:
                            old_process = _validate_process_snapshot(
                                attempt.get("old_process"))
                        except LifecycleError:
                            return self._terminal(
                                attempt, _result(False, "indeterminate",
                                                 node_id=node_id,
                                                 operation_id=operation_id),
                                state="indeterminate")
                        return self._finish_committed_restart(
                            attempt, profile, expected_runtime_id, old_process,
                            policy, attempt.get("dirty_revision"),
                            self._monotonic()
                            + _restart_recovery_timeout(requested_timeout_s),
                            cancel_event, operation_deadline=deadline,
                            deadline_deferred=True)
                    return self._wait_for_launch(
                        attempt, None,
                        self._monotonic()
                        + _restart_recovery_timeout(requested_timeout_s),
                        cancel_event, cancel_deferred=True,
                        operation_deadline=deadline,
                        deadline_deferred=True)
                self._remaining(deadline, cancel_event)
                profile = self._profile(node_id, convoy_id)
                self._ready(profile)
                self._remaining(deadline, cancel_event)
                # Refuse before the old healthy process is committed to exit.
                # _launch checks again to fence any future refactor/race.
                if self.store.recent_launch_count(
                        node_id, launch_unit_id=_launch_unit_id(profile),
                        window_s=self.crash_loop_window_s
                        ) >= self.crash_loop_max:
                    raise LifecycleError("crash_loop")
                runtime = self._current(node_id)
                runtime_id, process = self._runtime_process(
                    runtime, profile, require_last=True)
                if runtime_id != expected_runtime_id:
                    raise LifecycleError("runtime_changed")
                siblings = self._shared_runtime_nodes(profile, process)
                if siblings:
                    return self._terminal(
                        attempt, _result(
                            False, "shared_runtime", node_id=node_id,
                            operation_id=operation_id,
                            impacted_node_ids=siblings))
                dirty = self._runtime_call_before_commit(
                    "dirty", node_id, runtime_id, deadline, cancel_event)
                effective_dirty = False
                if policy not in DESTRUCTIVE_RESTART_POLICIES:
                    effective_dirty, _ = self._dirty_snapshot(dirty)
                if policy == "require_clean" and effective_dirty:
                    raise LifecycleError("project_dirty")
                if policy == "save_then_restart" and effective_dirty:
                    # From the instant the save call crosses the adapter
                    # boundary, timeout/cancellation cannot prove it did not
                    # run. Preserve that uncertainty on every later refusal.
                    save_may_have_run = True
                    # Snapshot the exact .toe identity so a successful save is
                    # proven by the file being rewritten, not by project dirt.
                    pre_save_toe_identity = _file_identity(profile["toe_path"])
                    saved = self._runtime_call_before_commit(
                        "save", node_id, runtime_id, deadline, cancel_event)
                    if saved.get("ok") is not True:
                        save_may_have_run = (
                            saved.get("save_may_have_run") is True)
                        if saved.get("code") == "cancelled":
                            return self._terminal(
                                attempt, _result(
                                    False, "cancelled", node_id=node_id,
                                    operation_id=operation_id,
                                    save_may_have_run=(
                                        saved.get("save_may_have_run") is True)),
                                state="cancelled")
                        if saved.get("save_may_have_run") is True:
                            return self._terminal(
                                attempt, _result(
                                    False, "save_failed", node_id=node_id,
                                    operation_id=operation_id,
                                    save_may_have_run=True))
                        raise LifecycleError("save_failed")
                    if cancel_event is not None and cancel_event.is_set():
                        return self._terminal(
                            attempt, _result(
                                False, "cancelled", node_id=node_id,
                                operation_id=operation_id,
                                save_may_have_run=True),
                            state="cancelled")
                    dirty = self._runtime_call_before_commit(
                        "dirty", node_id, runtime_id, deadline, cancel_event)
                    if (isinstance(dirty, dict)
                            and dirty.get("code") == "cancelled"):
                        return self._terminal(
                            attempt, _result(
                                False, "cancelled", node_id=node_id,
                                operation_id=operation_id,
                                save_may_have_run=True),
                            state="cancelled")
                    after_save_dirty, _ = self._dirty_snapshot(dirty)
                    # Residual project.modified dirt is only a failed save when
                    # the exact .toe was never rewritten. Embody re-dirties the
                    # project within seconds of a good save, so the file delta,
                    # not project.modified alone, is the clean proof.
                    if after_save_dirty and not self._save_landed(
                            profile["toe_path"], pre_save_toe_identity):
                        raise LifecycleError("save_failed")
                    save_landed = True
                    profile = self._refresh_saved_toe(profile)
                if policy in DESTRUCTIVE_RESTART_POLICIES:
                    if self.local_policy(node_id, policy) is not True:
                        raise LifecycleError("destructive_policy_disabled")
                self._remaining(deadline, cancel_event)
                # Revalidate immediately before the irreversible quit commit.
                now_runtime = self._current(node_id)
                now_id, now_process = self._runtime_process(
                    now_runtime, profile, require_last=True)
                if now_id != runtime_id or not _process_same(now_process, process):
                    raise LifecycleError("runtime_changed")
                self._ready(profile)
                self._remaining(deadline, cancel_event)
                dirty_revision = None
                if policy not in DESTRUCTIVE_RESTART_POLICIES:
                    final_dirty = self._runtime_call_before_commit(
                        "dirty", node_id, runtime_id, deadline, cancel_event)
                    if (isinstance(final_dirty, dict)
                            and final_dirty.get("code") == "cancelled"):
                        fields = {}
                        if policy == "save_then_restart" and effective_dirty:
                            fields["save_may_have_run"] = True
                        return self._terminal(
                            attempt, _result(
                                False, "cancelled", node_id=node_id,
                                operation_id=operation_id, **fields),
                            state="cancelled")
                    changed_again, dirty_revision = self._dirty_snapshot(
                        final_dirty)
                    if changed_again and not (
                            policy == "save_then_restart" and save_landed):
                        # A proven-landed save whose project.modified merely
                        # re-dirtied via Embody housekeeping is NOT a failed
                        # save; the quit CAS still fences a genuine late edit.
                        raise LifecycleError(
                            "save_failed" if policy == "save_then_restart"
                            else "project_dirty")
                # Cancellation is a compare-and-swap gate immediately beside
                # the irreversible durable commit, not merely a check before
                # the final TD round trip.
                try:
                    self._remaining(deadline, cancel_event)
                except LifecycleError as exc:
                    fields = {}
                    if policy == "save_then_restart" and effective_dirty:
                        fields["save_may_have_run"] = True
                    return self._terminal(
                        attempt, _result(
                            False, exc.code, node_id=node_id,
                            operation_id=operation_id, **fields),
                        state=("cancelled" if exc.code == "cancelled"
                               else "failed"))
                if policy in DESTRUCTIVE_RESTART_POLICIES:
                    if self.local_policy(node_id, policy) is not True:
                        raise LifecycleError("destructive_policy_disabled")
                # The last deadline/cancellation CAS is immediately adjacent
                # to the durable restart commit.  No TD call, policy prompt,
                # or process inspection may be inserted below it.
                self._remaining(deadline, cancel_event)
                attempt, _ = self.store.update_attempt(
                    operation_id, expected_states={"created"},
                    state="restart_committed", committed_at=float(self._clock()),
                    old_process=process, old_runtime_id=runtime_id,
                    dirty_revision=dirty_revision,
                    profile_fingerprint=_profile_fingerprint(profile),
                    restart_policy=policy,
                    requested_timeout_s=requested_timeout_s)
                recovery_deadline = max(
                    deadline,
                    self._monotonic()
                    + _restart_recovery_timeout(requested_timeout_s))
                return self._finish_committed_restart(
                    attempt, profile, runtime_id, process, policy,
                    dirty_revision, recovery_deadline, cancel_event,
                    operation_deadline=deadline)
            except LifecycleError as exc:
                fields = {}
                if (save_may_have_run
                        and exc.code in ("cancelled", "deadline_exceeded",
                                         "save_failed")):
                    fields["save_may_have_run"] = True
                result = _result(False, exc.code, node_id=node_id,
                                 operation_id=operation_id, **fields)
                if exc.code == "idempotency_conflict":
                    return result
                attempt = self.store.get_attempt(operation_id)
                state = "cancelled" if exc.code == "cancelled" else None
                return self._terminal(attempt, result, state=state) if attempt else result
            except Exception:
                attempt = self.store.get_attempt(operation_id)
                result = _result(False, "internal_error", node_id=node_id,
                                 operation_id=operation_id)
                return self._terminal(attempt, result) if attempt else result
        finally:
            self._exit_operation(operation_id)
            lock.release()


__all__ = [
    "CRASH_LOOP_MAX_LAUNCHES", "CRASH_LOOP_WINDOW_S",
    "DEFAULT_QUIT_TIMEOUT_S", "DEFAULT_RESTART_RECOVERY_TIMEOUT_S",
    "DEFAULT_START_TIMEOUT_S",
    "DESTRUCTIVE_RESTART_POLICIES", "ExactProcessLauncher",
    "HOST_LIFECYCLE_CAPABILITY", "LaunchProfileStore", "LifecycleError",
    "LAUNCH_STABILITY_WINDOW_S", "LifecycleManager",
    "LocalProcessInspector", "RESTART_POLICIES",
    "SpawnedProcess", "lifecycle_catalog",
]
