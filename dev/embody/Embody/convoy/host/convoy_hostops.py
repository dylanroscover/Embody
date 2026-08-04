"""Bounded host-side Git, GitHub CLI, and opt-in shell operations.

This module is deliberately independent from :mod:`convoy_hostapp`.  The
host app supplies two pieces of *local* authority:

``worktree_resolver(target_id) -> path | None``
    Resolves an enabled, registered node/launch profile to its canonical
    project worktree.  Callers never submit a filesystem path.

``full_shell_policy(target_id) -> True | False``
    Reads the current host-private ``Allow Full Shell`` approval.  It is
    consulted before resolution and again immediately before process spawn.

Structured Git and ``gh`` operations are enums with typed fields.  They are
never an argv pass-through and always use ``shell=False``.  Full shell is a
separate, visibly dangerous operation and is disabled when no policy callback
is provided.  No function in this file imports TouchDesigner modules.

The implementation is intentionally conservative.  Git network/worktree
operations preflight executable configuration and refuse hooks, executable
filters, custom remote/credential helpers, URL rewrites, submodule commands,
and unreviewed protocols.  This is not an OS sandbox: once Full Shell is
locally enabled, the selected shell has the OS user's authority.
"""

from __future__ import annotations

import json
import math
import ntpath
import os
import posixpath
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse


HOST_GIT_CAPABILITY = "host.git/v1"
HOST_GH_CAPABILITY = "host.gh/v1"
HOST_SHELL_CAPABILITY = "host.shell/v1"

DEFAULT_TIMEOUT_S = 60.0
MAX_TIMEOUT_S = 300.0
DEFAULT_OUTPUT_LIMIT = 64 * 1024
MIN_OUTPUT_LIMIT = 1024
MAX_OUTPUT_LIMIT = 1024 * 1024
MAX_ARG_COUNT = 128
MAX_ARG_BYTES = 128 * 1024
MAX_ENV_ENTRIES = 192
MAX_ENV_BYTES = 256 * 1024
MAX_SHELL_COMMAND_BYTES = 32 * 1024
MAX_ENV_ADDITIONS = 32
MAX_ENV_VALUE_BYTES = 8 * 1024
MAX_REDACT_VALUES = 64
MAX_REDACT_BYTES = 64 * 1024
PROCESS_POLL_S = 0.025
TERMINATE_GRACE_S = 0.75
_SIGTERM = getattr(signal, "SIGTERM", 15)
_SIGKILL = getattr(signal, "SIGKILL", 9)

_TARGET_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_REMOTE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,254}\Z")
# Windows defines standard variables such as ``ProgramFiles(x86)``.  Parentheses
# are therefore valid in an inherited environment name; ``=`` and NUL remain
# forbidden by the explicit validation below.
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_()]{0,127}\Z")

_SENSITIVE_ENV_NAME_RE = re.compile(
    r"(?:TOKEN|SECRET|PASS(?:WORD)?|API_?KEY|PRIVATE_?KEY|AUTH)",
    re.IGNORECASE,
)
_TOKEN_PATTERNS = (
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{12,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{12,}\b", re.IGNORECASE),
    re.compile(r"\bowk_(?:live|test)_[A-Za-z0-9_-]{8,}\b", re.IGNORECASE),
    re.compile(r"(?i)\b(Bearer\s+)[A-Za-z0-9._~+/-]{8,}=*"),
    re.compile(
        r"(?i)\b(password|passwd|token|secret|api[_-]?key)"
        r"(\s*[:=]\s*)[^\s,;]+"
    ),
    re.compile(
        r"(?i)([?&](?:access[_-]?)?(?:token|secret|password|api[_-]?key)=)"
        r"[^&#\s]+"
    ),
)
_URL_CREDENTIAL_RE = re.compile(
    r"(?i)(\b[a-z][a-z0-9+.-]*://)([^/@\s:]+)(?::[^/@\s]*)?@"
)

# Exact reviewed catalogs.  The values are capability metadata, not command
# fragments supplied by a controller.  Changing these is a schema change.
GIT_CATALOG = {
    "status": {"mutating": False, "network": False, "arguments": {}},
    "remotes": {"mutating": False, "network": False, "arguments": {}},
    "branches": {"mutating": False, "network": False, "arguments": {}},
    "current_branch": {"mutating": False, "network": False, "arguments": {}},
    "revision": {"mutating": False, "network": False, "arguments": {}},
    "upstream": {"mutating": False, "network": False, "arguments": {}},
    "divergence": {"mutating": False, "network": False, "arguments": {}},
    "fetch": {
        "mutating": True, "network": True,
        "arguments": {"remote": "string", "prune": "bool?",
                      "tags": "bool?"},
    },
    "pull_ff_only": {
        "mutating": True, "network": True,
        "arguments": {"remote": "string", "branch": "string"},
    },
    "push_branch": {
        "mutating": True, "network": True,
        "arguments": {"remote": "string", "branch": "existing-local-branch"},
    },
}

GH_CATALOG = {
    "auth_status": {"mutating": False, "arguments": {}},
    "repo_view": {"mutating": False, "arguments": {}},
    "pr_list": {
        "mutating": False,
        "arguments": {"limit": "int? 1..100", "state": "open|closed|merged|all?"},
    },
    "pr_view": {"mutating": False, "arguments": {"number": "positive-int"}},
    "pr_checks": {"mutating": False, "arguments": {"number": "positive-int"}},
    "workflow_list": {
        "mutating": False, "arguments": {"limit": "int? 1..100"}},
    "run_list": {
        "mutating": False,
        "arguments": {"limit": "int? 1..100", "status": "reviewed-enum?"},
    },
    "run_view": {"mutating": False, "arguments": {"run_id": "positive-int"}},
}

# Helpers implemented by Git itself or the two OS credential stores.  Any
# helper containing arguments, a path, or ``!shell`` is refused.  A future
# helper belongs in a reviewed capability revision, not a permissive regex.
DEFAULT_CREDENTIAL_HELPERS = frozenset({
    "cache", "store", "manager", "manager-core", "wincred",
    "osxkeychain", "libsecret",
})

_REVIEWED_LFS_FILTERS = {
    "filter.lfs.clean": "git-lfs clean -- %f",
    "filter.lfs.smudge": "git-lfs smudge -- %f",
    "filter.lfs.process": "git-lfs filter-process",
    "filter.lfs.required": "true",
}

# HTTP can disclose credentials, SSH may execute ProxyCommand from the OS
# user's ssh configuration, and git:// is unauthenticated.  None is claimed
# safe-by-default.  Network Git is HTTPS-only in this capability revision.
DEFAULT_REMOTE_SCHEMES = frozenset({"https"})
PUSH_REMOTE_SCHEMES = frozenset({"https"})

_BASE_ENV_KEYS = frozenset({
    "PATH", "PATHEXT", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR",
    "TEMP", "TMP", "SYSTEMROOT", "WINDIR", "COMSPEC", "USERPROFILE",
    "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES",
    "PROGRAMFILES(X86)", "COMMONPROGRAMFILES", "COMMONPROGRAMFILES(X86)",
    "SSH_AUTH_SOCK", "LANG", "LANGUAGE", "LC_ALL", "TERM",
})
_GH_SECRET_ENV_KEYS = frozenset({
    "GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN",
    "GITHUB_ENTERPRISE_TOKEN",
})
_BLOCKED_ENV_ADDITION_RE = re.compile(
    r"^(?:PATH|PATHEXT|COMSPEC|SHELL|SYSTEMROOT|WINDIR|"
    r"PYTHON(?:HOME|PATH)?|PERL5LIB|RUBYLIB|NODE_OPTIONS|"
    r"LD_.*|DYLD_.*|GIT_.*|GH_.*|GITHUB_.*|CONVOY_.*)$",
    re.IGNORECASE,
)

_PUBLIC_DETAILS = {
    "ok": "operation completed",
    "unknown_operation": "operation is not in the reviewed catalog",
    "invalid_arguments": "operation arguments are invalid",
    "unknown_target": "target is not an enabled registered node",
    "target_changed": "target registration changed before execution",
    "worktree_unavailable": "registered project worktree is unavailable",
    "worktree_escape": "working directory is outside the registered worktree",
    "repository_required": "registered worktree is not a Git repository root",
    "command_unavailable": "required local executable is unavailable",
    "command_refused": "the operating system refused to start the executable",
    "command_failed": "command returned a non-zero exit status",
    "authentication_required": "local GitHub authentication is unavailable or refused",
    "dirty_worktree": "operation was refused because the worktree has local changes",
    "non_fast_forward": "operation was refused because it is not a fast-forward",
    "upstream_missing": "the branch has no configured upstream",
    "unsafe_repository_config": "repository executable configuration is not allowed",
    "unsafe_remote": "configured remote is outside the reviewed transport policy",
    "remote_missing": "configured remote was not found",
    "ref_missing": "requested local branch was not found",
    "shell_disabled": "Allow Full Shell is disabled on the target host",
    "invalid_environment": "environment additions are invalid or unsafe",
    "busy": "host subprocess capacity is busy",
    "timeout": "operation exceeded its deadline",
    "cancelled": "operation was cancelled",
    "invalid_command_output": "command returned an invalid structured result",
    "internal_error": "host operation failed safely",
}


class _Refusal(Exception):
    """Internal control flow carrying only a stable, non-secret code."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code if code in _PUBLIC_DETAILS else "internal_error"


def operation_catalog():
    """Return JSON-safe capability data for a manifest/bridge."""
    catalog = {
        "git": {
            "capability": HOST_GIT_CAPABILITY,
            "operations": GIT_CATALOG,
        },
        "gh": {
            "capability": HOST_GH_CAPABILITY,
            "operations": GH_CATALOG,
        },
        "shell": {
            "capability": HOST_SHELL_CAPABILITY,
            "operations": {"execute": {
                "mutating": True, "local_opt_in": True,
                "arguments": {
                    "command": "string", "cwd": "relative-directory?",
                    "env_additions": "bounded-object?",
                    "redact_values": "string-list?",
                },
            }},
        },
        "limits": {"max_timeout_s": MAX_TIMEOUT_S,
                   "max_output_bytes": MAX_OUTPUT_LIMIT},
    }
    return json.loads(json.dumps(catalog, sort_keys=True))


def _public_result(ok, code, **fields):
    code = code if code in _PUBLIC_DETAILS else "internal_error"
    result = {"ok": bool(ok), "code": code, "detail": _PUBLIC_DETAILS[code]}
    result.update(fields)
    # A final JSON encoding check catches accidental bytes/path objects before
    # they cross IPC.  Never put the encoder exception in the result.
    try:
        json.dumps(result, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        return {"ok": False, "code": "internal_error",
                "detail": _PUBLIC_DETAILS["internal_error"]}
    return result


def _redact_text(value, secret_values=()):
    """Best-effort output redaction without ever raising.

    Structured error *details* are static regardless; this additionally
    protects stdout/stderr generated by third-party tools.  Literal known
    secret values are replaced longest-first, followed by common token and
    credential-bearing URL forms.
    """
    try:
        text = str(value or "")
        known = sorted({str(v) for v in secret_values if v is not None and
                        len(str(v)) >= 4}, key=len, reverse=True)
        for secret in known:
            text = text.replace(secret, "[REDACTED]")
        text = _URL_CREDENTIAL_RE.sub(r"\1[REDACTED]@", text)
        for pattern in _TOKEN_PATTERNS:
            if pattern.groups == 1:
                text = pattern.sub(r"\1[REDACTED]", text)
            elif pattern.groups == 2:
                text = pattern.sub(r"\1\2[REDACTED]", text)
            else:
                text = pattern.sub("[REDACTED]", text)
        return text
    except Exception:
        return "[REDACTION FAILED]"


def _validate_timeout(value):
    if value is None:
        return DEFAULT_TIMEOUT_S
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _Refusal("invalid_arguments")
    value = float(value)
    if not math.isfinite(value) or value <= 0 or value > MAX_TIMEOUT_S:
        raise _Refusal("invalid_arguments")
    return value


def _validate_output_limit(value):
    if value is None:
        return DEFAULT_OUTPUT_LIMIT
    if isinstance(value, bool) or not isinstance(value, int):
        raise _Refusal("invalid_arguments")
    if value < MIN_OUTPUT_LIMIT or value > MAX_OUTPUT_LIMIT:
        raise _Refusal("invalid_arguments")
    return value


def _validate_target_id(target_id):
    if not isinstance(target_id, str) or not _TARGET_RE.fullmatch(target_id):
        raise _Refusal("unknown_target")
    return target_id


def _safe_target_field(target_id):
    return target_id if isinstance(target_id, str) and _TARGET_RE.fullmatch(target_id) else None


def _safe_operation_field(operation, catalog):
    return operation if isinstance(operation, str) and operation in catalog else None


def _paths_same(left, right):
    """Filesystem identity with a portable lexical fallback.

    ``samefile`` handles case-preserving aliases, Unicode normalization and
    Windows junctions.  It can fail on missing/permission-blocked paths, where
    the already-realpathed normalized comparison remains fail-closed enough
    for a diagnostic decision.
    """
    try:
        return os.path.samefile(left, right)
    except (OSError, ValueError, TypeError, AttributeError):
        try:
            return os.path.normcase(os.path.realpath(left)) == os.path.normcase(
                os.path.realpath(right))
        except (OSError, ValueError, TypeError):
            return False


def _path_within(root, candidate):
    try:
        common = os.path.commonpath([os.path.realpath(root), os.path.realpath(candidate)])
    except (OSError, ValueError, TypeError):
        return False
    return _paths_same(common, root)


def _normalize_secret_values(values):
    if not isinstance(values, (list, tuple)) or len(values) > MAX_REDACT_VALUES:
        raise _Refusal("invalid_arguments")
    normalized = []
    total = 0
    for value in values:
        if not isinstance(value, str) or "\0" in value:
            raise _Refusal("invalid_arguments")
        size = len(value.encode("utf-8"))
        if size > MAX_ENV_VALUE_BYTES:
            raise _Refusal("invalid_arguments")
        total += size
        if total > MAX_REDACT_BYTES:
            raise _Refusal("invalid_arguments")
        normalized.append(value)
    return normalized


def _windows_taskkill_path(environment=None, isfile=None):
    """Return the absolute System32 taskkill path, never PATH-search it."""
    isfile = os.path.isfile if isfile is None else isfile
    system_directory = None
    if environment is None and sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.GetSystemDirectoryW.argtypes = [wintypes.LPWSTR, wintypes.UINT]
            kernel32.GetSystemDirectoryW.restype = wintypes.UINT
            buffer = ctypes.create_unicode_buffer(32768)
            length = kernel32.GetSystemDirectoryW(buffer, len(buffer))
            if 0 < length < len(buffer):
                system_directory = buffer.value
        except Exception:
            system_directory = None
    if system_directory is None:
        environment = os.environ if environment is None else environment
        root = environment.get("SystemRoot") or environment.get("WINDIR")
        if not isinstance(root, str) or not ntpath.isabs(root) or "\0" in root:
            return None
        system_directory = ntpath.join(root, "System32")
    if not ntpath.isabs(system_directory) or "\0" in system_directory:
        return None
    expected_parent = ntpath.normcase(ntpath.normpath(system_directory))
    candidate = ntpath.normpath(ntpath.join(system_directory, "taskkill.exe"))
    if ntpath.normcase(ntpath.dirname(candidate)) != expected_parent:
        return None
    return candidate if isfile(candidate) else None


def _safe_environment(source=None, *, include_gh_secrets=False):
    """Create a small inherited environment with stable safety overrides."""
    source = os.environ if source is None else source
    wanted = set(_BASE_ENV_KEYS)
    if include_gh_secrets:
        wanted.update(_GH_SECRET_ENV_KEYS)
    result = {}
    for key, value in source.items():
        if not isinstance(key, str) or not isinstance(value, str):
            continue
        upper = key.upper()
        if upper not in wanted and not upper.startswith("LC_"):
            continue
        if "\0" in key or "\0" in value or len(value.encode("utf-8")) > MAX_ENV_VALUE_BYTES:
            continue
        result[key] = value
    # Ensure executable lookup remains deterministic enough to function even
    # under a minimal service environment.  Popen still receives an already
    # resolved absolute executable from HostOperations.
    if not any(k.upper() == "PATH" for k in result):
        result["PATH"] = os.defpath
    result.update({
        "NO_COLOR": "1",
        "CLICOLOR": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_LITERAL_PATHSPECS": "1",
        "SSH_ASKPASS_REQUIRE": "never",
        "GH_PROMPT_DISABLED": "1",
        "GH_NO_UPDATE_NOTIFIER": "1",
        "GH_PAGER": "",
        "PAGER": "",
    })
    return result


def _sensitive_values(environment):
    values = []
    for key, value in (environment or {}).items():
        if _SENSITIVE_ENV_NAME_RE.search(str(key)) and value:
            values.append(str(value))
    return values


def _validate_environment(environment):
    if not isinstance(environment, dict) or len(environment) > MAX_ENV_ENTRIES:
        raise _Refusal("invalid_environment")
    total = 0
    for key, value in environment.items():
        if (not isinstance(key, str) or not isinstance(value, str) or
                not _ENV_NAME_RE.fullmatch(key) or "\0" in value):
            raise _Refusal("invalid_environment")
        total += len(key.encode("utf-8")) + len(value.encode("utf-8"))
        if total > MAX_ENV_BYTES:
            raise _Refusal("invalid_environment")


class _OutputCapture:
    """Drain both pipes fully while retaining at most ``limit`` bytes."""

    def __init__(self, limit):
        self.limit = int(limit)
        self.remaining = int(limit)
        self.buffers = {"stdout": bytearray(), "stderr": bytearray()}
        self.observed = {"stdout": 0, "stderr": 0}
        self.lock = threading.Lock()

    def feed(self, stream, chunk):
        if not chunk:
            return
        with self.lock:
            self.observed[stream] += len(chunk)
            keep = min(self.remaining, len(chunk))
            if keep:
                self.buffers[stream].extend(chunk[:keep])
                self.remaining -= keep

    @property
    def truncated(self):
        return sum(self.observed.values()) > self.limit

    def text(self, stream):
        return bytes(self.buffers[stream]).decode("utf-8", errors="replace")


class _WindowsJob:
    """Best-effort kill-on-close Job Object wrapper (actual Windows only)."""

    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    _JobObjectExtendedLimitInformation = 9

    def __init__(self, process):
        import ctypes
        from ctypes import wintypes

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class BASIC_LIMIT(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class EXTENDED_LIMIT(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BASIC_LIMIT),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self._kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        self._kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        self._kernel32.SetInformationJobObject.restype = wintypes.BOOL
        self._kernel32.AssignProcessToJobObject.argtypes = [
            wintypes.HANDLE, wintypes.HANDLE]
        self._kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        self._kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self._kernel32.TerminateJobObject.restype = wintypes.BOOL
        self._kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel32.CloseHandle.restype = wintypes.BOOL
        self._handle = self._kernel32.CreateJobObjectW(None, None)
        if not self._handle:
            raise OSError("CreateJobObjectW failed")
        info = EXTENDED_LIMIT()
        info.BasicLimitInformation.LimitFlags = self._JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self._kernel32.SetInformationJobObject(
                self._handle, self._JobObjectExtendedLimitInformation,
                ctypes.byref(info), ctypes.sizeof(info)):
            self.close()
            raise OSError("SetInformationJobObject failed")
        process_handle = getattr(process, "_handle", None)
        if process_handle is None or not self._kernel32.AssignProcessToJobObject(
                self._handle, int(process_handle)):
            self.close()
            raise OSError("AssignProcessToJobObject failed")

    def terminate(self):
        if self._handle and not self._kernel32.TerminateJobObject(self._handle, 1):
            raise OSError("TerminateJobObject failed")

    def close(self):
        if getattr(self, "_handle", None):
            self._kernel32.CloseHandle(self._handle)
            self._handle = None


def _popen_platform_kwargs(platform=None):
    """Platform process-group options, exposed for foreign-OS tests."""
    platform = platform or sys.platform
    if platform == "win32":
        return {
            "creationflags": (
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200) |
                getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            )
        }
    return {"start_new_session": True}


class ProcessRunner:
    """Run an exact executable/argv with bounded resources and cancellation."""

    def __init__(self, *, max_concurrent=4, platform=None,
                 popen_factory=None, clock=None, job_factory=None):
        if isinstance(max_concurrent, bool) or not isinstance(max_concurrent, int) or max_concurrent < 1:
            raise ValueError("max_concurrent must be a positive integer")
        self.platform = platform or sys.platform
        self._popen = popen_factory or subprocess.Popen
        self._clock = clock or time.monotonic
        self._slots = threading.BoundedSemaphore(max_concurrent)
        self._job_factory = job_factory

    def _acquire_slot(self, deadline, cancel_event):
        while True:
            if cancel_event is not None and cancel_event.is_set():
                return "cancelled"
            remaining = deadline - self._clock()
            if remaining <= 0:
                return "busy"
            if self._slots.acquire(timeout=min(PROCESS_POLL_S, remaining)):
                return None

    @staticmethod
    def _validate_invocation(executable, argv):
        if (not isinstance(executable, str) or not executable or "\0" in executable or
                not isinstance(argv, (list, tuple)) or len(argv) > MAX_ARG_COUNT):
            raise _Refusal("invalid_arguments")
        total = len(executable.encode("utf-8"))
        for arg in argv:
            if not isinstance(arg, str) or "\0" in arg:
                raise _Refusal("invalid_arguments")
            total += len(arg.encode("utf-8"))
            if total > MAX_ARG_BYTES:
                raise _Refusal("invalid_arguments")

    def _new_job(self, process):
        if self.platform != "win32":
            return None
        if self._job_factory is not None:
            return self._job_factory(process)
        if sys.platform != "win32":
            return None
        try:
            return _WindowsJob(process)
        except Exception:
            return None

    def _terminate_tree(self, process, job):
        """Best effort; every branch is bounded and exception-safe."""
        if self.platform == "win32":
            if job is not None:
                try:
                    job.terminate()
                    return
                except Exception:
                    pass
            # taskkill is a process-tree fallback when Job Object assignment
            # was blocked (for example, an already-jobbed service process).
            if sys.platform == "win32":
                taskkill = _windows_taskkill_path()
                try:
                    if taskkill:
                        subprocess.run(
                            [taskkill, "/PID", str(process.pid), "/T", "/F"],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, shell=False, timeout=2,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                            check=False,
                        )
                except Exception:
                    pass
            try:
                process.kill()
            except Exception:
                pass
            return

        try:
            os.killpg(process.pid, _SIGTERM)
        except Exception:
            try:
                process.terminate()
            except Exception:
                pass
        # Waiting only for the group leader is insufficient: a child can
        # ignore SIGTERM while the shell exits immediately.  Poll the process
        # group itself for the full grace window, then SIGKILL what remains.
        grace_deadline = time.monotonic() + TERMINATE_GRACE_S
        try:
            process.wait(timeout=TERMINATE_GRACE_S)
        except Exception:
            pass
        while time.monotonic() < grace_deadline:
            try:
                os.killpg(process.pid, 0)
            except ProcessLookupError:
                return
            except Exception:
                break
            time.sleep(PROCESS_POLL_S)
        try:
            os.killpg(process.pid, _SIGKILL)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass

    @staticmethod
    def _reader(pipe, capture, stream):
        try:
            while True:
                chunk = pipe.read(8192)
                if not chunk:
                    break
                capture.feed(stream, chunk)
        except (OSError, ValueError):
            pass

    def run(self, *, executable, argv, cwd, environment, timeout_s,
            output_limit, cancel_event=None, secret_values=()):
        started = self._clock()
        try:
            self._validate_invocation(executable, argv)
            _validate_environment(environment)
            timeout_s = _validate_timeout(timeout_s)
            output_limit = _validate_output_limit(output_limit)
            secret_values = _normalize_secret_values(secret_values)
        except _Refusal as exc:
            return _public_result(False, exc.code)

        deadline = started + timeout_s
        wait_code = self._acquire_slot(deadline, cancel_event)
        if wait_code:
            return _public_result(False, wait_code, exit_code=None,
                                  stdout="", stderr="", truncated=False,
                                  duration_ms=int((self._clock() - started) * 1000))

        process = None
        job = None
        capture = _OutputCapture(output_limit)
        outcome = None
        try:
            kwargs = {
                "cwd": cwd,
                "env": environment,
                "stdin": subprocess.DEVNULL,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "shell": False,
                "bufsize": 0,
                "close_fds": True,
            }
            kwargs.update(_popen_platform_kwargs(self.platform))
            try:
                process = self._popen([executable] + list(argv), **kwargs)
            except FileNotFoundError:
                return _public_result(False, "command_unavailable")
            except PermissionError:
                return _public_result(False, "command_refused")
            except OSError:
                return _public_result(False, "command_refused")

            job = self._new_job(process)
            readers = []
            for stream, pipe in (("stdout", process.stdout), ("stderr", process.stderr)):
                thread = threading.Thread(
                    target=self._reader, args=(pipe, capture, stream), daemon=True,
                    name="convoy-%s-reader" % stream,
                )
                thread.start()
                readers.append(thread)

            while process.poll() is None:
                if cancel_event is not None and cancel_event.is_set():
                    outcome = "cancelled"
                    self._terminate_tree(process, job)
                    break
                if self._clock() >= deadline:
                    outcome = "timeout"
                    self._terminate_tree(process, job)
                    break
                time.sleep(PROCESS_POLL_S)

            try:
                process.wait(timeout=2)
            except Exception:
                self._terminate_tree(process, job)
                try:
                    process.wait(timeout=2)
                except Exception:
                    pass

            for thread in readers:
                thread.join(timeout=1)
            for pipe in (process.stdout, process.stderr):
                try:
                    pipe.close()
                except Exception:
                    pass

            stdout = _redact_text(capture.text("stdout"), secret_values)
            stderr = _redact_text(capture.text("stderr"), secret_values)
            duration = int((self._clock() - started) * 1000)
            exit_code = process.returncode
            if outcome is not None:
                return _public_result(
                    False, outcome, exit_code=exit_code, stdout=stdout,
                    stderr=stderr, truncated=capture.truncated,
                    observed_bytes=dict(capture.observed), duration_ms=duration,
                )
            code = "ok" if exit_code == 0 else "command_failed"
            return _public_result(
                exit_code == 0, code, exit_code=exit_code, stdout=stdout,
                stderr=stderr, truncated=capture.truncated,
                observed_bytes=dict(capture.observed), duration_ms=duration,
            )
        finally:
            if job is not None:
                try:
                    # KILL_ON_JOB_CLOSE also cleans up descendants a successful
                    # CLI process attempted to leave behind.
                    job.close()
                except Exception:
                    pass
            self._slots.release()


class HostOperations:
    """Reviewed host operation facade suitable for Convoy route handlers."""

    def __init__(self, worktree_resolver, *, full_shell_policy=None,
                 git_executable="git", gh_executable="gh", shell_executable=None,
                 process_runner=None, platform=None, environment=None,
                 safe_state_dir=None, audit_callback=None,
                 allowed_remote_schemes=None,
                 allowed_credential_helpers=None):
        if not callable(worktree_resolver):
            raise TypeError("worktree_resolver must be callable")
        self._resolve_registered = worktree_resolver
        self._full_shell_policy = full_shell_policy
        self.platform = platform or sys.platform
        self._environment = dict(os.environ if environment is None else environment)
        self._runner = process_runner or ProcessRunner(platform=self.platform)
        self._audit_callback = audit_callback
        self._git_candidate = git_executable
        self._gh_candidate = gh_executable
        self._shell_candidate = shell_executable
        schemes = (DEFAULT_REMOTE_SCHEMES if allowed_remote_schemes is None
                   else frozenset(str(v).lower() for v in allowed_remote_schemes))
        helpers = (DEFAULT_CREDENTIAL_HELPERS if allowed_credential_helpers is None
                   else frozenset(str(v).lower() for v in allowed_credential_helpers))
        # These constructor values are host-local policy narrowing, never an
        # escape hatch which broadens the reviewed capability.
        if not schemes.issubset(DEFAULT_REMOTE_SCHEMES):
            raise ValueError("allowed_remote_schemes may only narrow the reviewed set")
        if not helpers.issubset(DEFAULT_CREDENTIAL_HELPERS):
            raise ValueError("allowed_credential_helpers may only narrow the reviewed set")
        self._allowed_remote_schemes = schemes
        self._allowed_credential_helpers = helpers
        self._locks_guard = threading.Lock()
        self._worktree_locks = {}

        base = safe_state_dir
        if base is None:
            base = tempfile.mkdtemp(prefix="embody-convoy-hostops-")
        base = os.path.realpath(os.path.abspath(os.fspath(base)))
        self._hooks_dir = os.path.join(base, "disabled-hooks")
        self._attributes_file = os.path.join(base, "empty-attributes")
        os.makedirs(self._hooks_dir, mode=0o700, exist_ok=True)
        # Never truncate a user file: the host-private directory is expected
        # to be ours, but an unexpected non-empty file fails closed later.
        if not os.path.exists(self._attributes_file):
            fd = os.open(self._attributes_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                         0o600)
            os.close(fd)

    def _resolve_executable(self, candidate):
        if not isinstance(candidate, str) or not candidate or "\0" in candidate:
            raise _Refusal("command_unavailable")
        if ntpath.isabs(candidate) or posixpath.isabs(candidate):
            resolved = os.path.realpath(candidate)
            if not os.path.isfile(resolved):
                raise _Refusal("command_unavailable")
            return resolved
        path_value = next((value for key, value in self._environment.items()
                           if str(key).upper() == "PATH"), os.defpath)
        if not isinstance(path_value, str):
            raise _Refusal("command_unavailable")
        separator = ";" if self.platform == "win32" else ":"
        path_module = ntpath if self.platform == "win32" else posixpath
        safe_entries = []
        for entry in path_value.split(separator):
            if (not entry or entry != entry.strip() or "\0" in entry or
                    not path_module.isabs(entry) or entry.startswith(('"', "'"))):
                continue
            canonical = os.path.realpath(entry)
            if os.path.isdir(canonical):
                safe_entries.append(canonical)
        path_value = separator.join(safe_entries)
        resolved = shutil.which(candidate, path=path_value)
        if not resolved:
            raise _Refusal("command_unavailable")
        return os.path.realpath(resolved)

    def _require_structured_executable(self, executable, root):
        if _path_within(root, executable):
            raise _Refusal("command_refused")
        if self.platform == "win32" and ntpath.splitext(executable)[1].lower() in {
                ".cmd", ".bat"}:
            # CreateProcess delegates batch files to cmd.exe despite
            # shell=False.  Structured operations require a native executable.
            raise _Refusal("command_refused")

    def _structured_environment(self, root, executables, *, include_gh=False):
        """Sanitize PATH for structured executables and any reviewed helpers."""
        environment = _safe_environment(
            self._environment, include_gh_secrets=include_gh)
        raw_path = next((value for key, value in self._environment.items()
                         if str(key).upper() == "PATH"), "")
        separator = ";" if self.platform == "win32" else ":"
        path_module = ntpath if self.platform == "win32" else posixpath
        directories = []
        directory_keys = set()
        root_real = os.path.realpath(root)

        def lexically_inside_root(candidate):
            try:
                common = os.path.commonpath([root_real, candidate])
            except (OSError, ValueError, TypeError):
                return False
            return os.path.normcase(common) == os.path.normcase(root_real)

        def add_directory(value, *, must_exist, inherited=False):
            if (not isinstance(value, str) or not value or "\0" in value or
                    not path_module.isabs(value)):
                if inherited:
                    return
                raise _Refusal("unsafe_repository_config")
            # Quoted PATH components have inconsistent platform parsing and
            # can hide a relative/current-directory lookup.  Reject them.
            if value != value.strip() or value.startswith(('"', "'")):
                if inherited:
                    return
                raise _Refusal("unsafe_repository_config")
            canonical = os.path.realpath(value)
            if lexically_inside_root(canonical):
                if inherited:
                    return
                raise _Refusal("unsafe_repository_config")
            if must_exist and not os.path.isdir(canonical):
                raise _Refusal("unsafe_repository_config")
            if not os.path.isdir(canonical):
                return
            key = os.path.normcase(canonical)
            if key not in directory_keys:
                directories.append(canonical)
                directory_keys.add(key)

        # Pinned executable directories always lead.  A stale absolute PATH
        # entry is omitted; empty/relative/worktree entries are a hard refusal.
        for executable in executables:
            add_directory(os.path.dirname(executable), must_exist=True)
        if raw_path:
            for component in raw_path.split(separator):
                add_directory(component, must_exist=False, inherited=True)
        for key in list(environment):
            if key.upper() == "PATH":
                del environment[key]
        environment["PATH"] = separator.join(directories)
        _validate_environment(environment)
        return environment

    def _resolve_root(self, target_id):
        _validate_target_id(target_id)
        try:
            supplied = self._resolve_registered(target_id)
        except Exception:
            raise _Refusal("unknown_target")
        if not isinstance(supplied, (str, os.PathLike)):
            raise _Refusal("unknown_target")
        try:
            root = os.path.realpath(os.path.abspath(os.fspath(supplied)))
        except (OSError, TypeError, ValueError):
            raise _Refusal("worktree_unavailable")
        if not os.path.isdir(root):
            raise _Refusal("worktree_unavailable")
        return root

    def _revalidate_root(self, target_id, expected):
        try:
            current = self._resolve_root(target_id)
        except _Refusal:
            raise _Refusal("target_changed")
        if not _paths_same(current, expected):
            raise _Refusal("target_changed")

    def _worktree_lock(self, root):
        try:
            stat_result = os.stat(root)
            key = ("inode", stat_result.st_dev, stat_result.st_ino)
        except OSError:
            key = ("path", os.path.normcase(os.path.realpath(root)))
        with self._locks_guard:
            return self._worktree_locks.setdefault(key, threading.RLock())

    @staticmethod
    def _acquire_lock(lock, deadline, cancel_event):
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise _Refusal("cancelled")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _Refusal("timeout")
            if lock.acquire(timeout=min(PROCESS_POLL_S, remaining)):
                return

    def _audit(self, event, **fields):
        if not callable(self._audit_callback):
            return
        payload = {"event": event}
        payload.update(fields)
        try:
            # Copy through JSON to guarantee a callback never receives a
            # mutable exotic object.  No argv, raw command, env, or output is
            # included by any caller below.
            self._audit_callback(json.loads(json.dumps(payload, allow_nan=False)))
        except Exception:
            pass

    def _git_prefix(self, root):
        try:
            if (_path_within(root, self._hooks_dir) or
                    _path_within(root, self._attributes_file)):
                raise _Refusal("unsafe_repository_config")
            if os.listdir(self._hooks_dir):
                raise _Refusal("unsafe_repository_config")
            if os.path.getsize(self._attributes_file) != 0:
                raise _Refusal("unsafe_repository_config")
        except OSError:
            raise _Refusal("unsafe_repository_config")
        return [
            "--no-pager",
            "-c", "core.hooksPath=" + self._hooks_dir,
            "-c", "core.fsmonitor=false",
            "-c", "core.untrackedCache=false",
            "-c", "diff.external=",
            "-c", "core.attributesFile=" + self._attributes_file,
            "-c", "fetch.recurseSubmodules=false",
            "-c", "submodule.recurse=false",
            "-c", "protocol.ext.allow=never",
            "-c", "credential.interactive=never",
            "-C", root,
        ]

    @staticmethod
    def _remaining(deadline):
        value = deadline - time.monotonic()
        if value <= 0:
            raise _Refusal("timeout")
        return min(value, MAX_TIMEOUT_S)

    def _run_internal_git(self, executable, root, args, deadline,
                          output_limit=64 * 1024, cancel_event=None):
        result = self._runner.run(
            executable=executable,
            argv=self._git_prefix(root) + list(args),
            cwd=root,
            environment=self._structured_environment(
                root, [executable]),
            timeout_s=self._remaining(deadline),
            output_limit=output_limit,
            cancel_event=cancel_event,
            secret_values=(),
        )
        if result.get("code") in {"timeout", "cancelled", "busy",
                                  "command_unavailable", "command_refused"}:
            raise _Refusal(result["code"])
        return result

    def _validate_repository(self, executable, root, deadline, cancel_event):
        result = self._run_internal_git(
            executable, root, ["rev-parse", "--show-toplevel"], deadline,
            cancel_event=cancel_event,
        )
        if not result.get("ok"):
            raise _Refusal("repository_required")
        reported = result.get("stdout", "").strip()
        try:
            reported = os.path.realpath(os.path.abspath(reported))
        except (OSError, TypeError, ValueError):
            raise _Refusal("repository_required")
        if not _paths_same(reported, root):
            raise _Refusal("repository_required")

    def _effective_config_names(self, executable, root, deadline, cancel_event):
        result = self._run_internal_git(
            executable, root, ["config", "--null", "--name-only", "--list"],
            deadline, cancel_event=cancel_event,
        )
        if not result.get("ok"):
            raise _Refusal("unsafe_repository_config")
        if result.get("truncated"):
            raise _Refusal("unsafe_repository_config")
        return [item.lower() for item in result.get("stdout", "").split("\0")
                if item]

    def _credential_helpers(self, executable, root, deadline, cancel_event):
        """Every configured credential-helper VALUE, bare AND URL-scoped.

        Git honors both `credential.helper` and URL-scoped
        `credential.<url>.helper`; a scoped `!shell`/path/argument helper is
        the SAME remote-code-execution vector as a bare one (git runs it via
        the shell as the host user on any authenticating fetch/pull/push), so
        both must be validated against the allowlist. The bare key is fetched
        directly; scoped keys are found with `--get-regexp` so a
        case-sensitive URL subsection is matched exactly -- a lowercased name
        from `--name-only --list` would not `--get-all`-match it, which was
        the bypass (review 2026-08-02).
        """
        values = []
        bare = self._run_internal_git(
            executable, root,
            ["config", "--null", "--get-all", "credential.helper"],
            deadline, cancel_event=cancel_event,
        )
        if bare.get("truncated"):
            raise _Refusal("unsafe_repository_config")
        if not (bare.get("exit_code") == 1 and not bare.get("stdout")):
            if not bare.get("ok"):
                raise _Refusal("unsafe_repository_config")
            values.extend(item for item in bare.get("stdout", "").split("\0")
                          if item.strip())
        scoped = self._run_internal_git(
            executable, root,
            ["config", "--null", "--get-regexp", r"^credential\..+\.helper$"],
            deadline, cancel_event=cancel_event,
        )
        if scoped.get("truncated"):
            raise _Refusal("unsafe_repository_config")
        # exit 1 with no output == no scoped helpers, the normal case.
        if not (scoped.get("exit_code") == 1 and not scoped.get("stdout")):
            if not scoped.get("ok"):
                raise _Refusal("unsafe_repository_config")
            # `--get-regexp --null` emits one "name\nvalue" record per NUL.
            for record in scoped.get("stdout", "").split("\0"):
                if not record.strip():
                    continue
                parts = record.split("\n", 1)
                value = parts[1] if len(parts) == 2 else ""
                if value.strip():
                    values.append(value)
        return [item.strip().lower() for item in values if item.strip()]

    def _git_config_value(self, executable, root, key, deadline, cancel_event):
        result = self._run_internal_git(
            executable, root, ["config", "--get", key], deadline,
            cancel_event=cancel_event,
        )
        if not result.get("ok") or result.get("truncated"):
            raise _Refusal("unsafe_repository_config")
        return result.get("stdout", "").rstrip("\r\n")

    def _reviewed_lfs_filter(self, executable, root, filter_names, deadline,
                             cancel_event):
        """Allow only the exact, host-installed Git LFS filter contract.

        Git LFS is installed as system-level filter configuration by standard
        Git for Windows, so rejecting every filter would disable even `status`
        on a clean repository.  The exception is deliberately narrow: all
        four effective values must match the reviewed built-in LFS contract,
        and the binary the SANITIZED PATH resolves must be a real file outside
        the target worktree.  The sanitized environment is what makes this
        sound -- the repository cannot add PATH entries, so whatever git-lfs
        resolves there is host-owned software.  A same-directory-as-git
        requirement was tried and dropped: the standalone Git LFS installer
        (and GitHub's runner images) put git-lfs in its own directory, which
        refused `status` on every clean repository of a legitimately
        configured host (CI, 2026-08-04).
        """
        if set(filter_names) != set(_REVIEWED_LFS_FILTERS):
            return False
        for key, expected in _REVIEWED_LFS_FILTERS.items():
            if self._git_config_value(
                    executable, root, key, deadline, cancel_event) != expected:
                return False
        path_value = self._structured_environment(root, [executable])["PATH"]
        lfs = shutil.which("git-lfs", path=path_value)
        if not lfs:
            return False
        lfs = os.path.realpath(lfs)
        if _path_within(root, lfs):
            return False
        return os.path.isfile(lfs)

    def _preflight_executable_config(self, executable, root, operation,
                                     deadline, cancel_event):
        names = self._effective_config_names(
            executable, root, deadline, cancel_event)
        network = GIT_CATALOG[operation]["network"]
        # `git status` may hash a racily-clean worktree entry through its
        # configured clean/process filter.  It therefore needs the same
        # executable-filter refusal as a checkout-producing pull.
        touches_worktree = operation in {"status", "pull_ff_only"}
        executable_filters = [
            name for name in names
            if name.startswith("filter.") and name.rsplit(".", 1)[-1] in {
                "clean", "smudge", "process", "required"}
        ]
        if (touches_worktree and executable_filters and
                not self._reviewed_lfs_filter(
                    executable, root, executable_filters, deadline,
                    cancel_event)):
            raise _Refusal("unsafe_repository_config")
        for name in names:
            if network and (
                    # protocol.ext.allow is deliberately present in this list
                    # because our own final CLI override sets it to `never`;
                    # unlike the entries below, an earlier value cannot win.
                    name in {"core.sshcommand", "core.gitproxy"} or
                    (name.startswith("url.") and
                     (name.endswith(".insteadof") or name.endswith(".pushinsteadof"))) or
                    (name.startswith("remote.") and
                     name.rsplit(".", 1)[-1] in {
                         "proxy", "uploadpack", "receivepack", "vcs"}) or
                    (name.startswith("submodule.") and
                     name.rsplit(".", 1)[-1] in {"update", "url"})):
                raise _Refusal("unsafe_repository_config")
        if network:
            for helper in self._credential_helpers(
                    executable, root, deadline, cancel_event):
                if helper not in self._allowed_credential_helpers:
                    raise _Refusal("unsafe_repository_config")

    def _git_lines(self, executable, root, args, deadline, cancel_event):
        result = self._run_internal_git(
            executable, root, args, deadline, cancel_event=cancel_event)
        if not result.get("ok") or result.get("truncated"):
            return []
        return [line.strip() for line in result.get("stdout", "").splitlines()
                if line.strip()]

    def _validate_remote(self, executable, root, remote, operation,
                         deadline, cancel_event):
        if not isinstance(remote, str) or not _REMOTE_RE.fullmatch(remote):
            raise _Refusal("invalid_arguments")
        remotes = self._git_lines(
            executable, root, ["remote"], deadline, cancel_event)
        if remote not in remotes:
            raise _Refusal("remote_missing")
        key = "remote.%s.%s" % (
            remote, "pushurl" if operation == "push_branch" else "url")
        urls = self._git_lines(
            executable, root, ["config", "--get-all", key], deadline,
            cancel_event)
        if operation == "push_branch" and not urls:
            urls = self._git_lines(
                executable, root, ["config", "--get-all", "remote.%s.url" % remote],
                deadline, cancel_event)
        if not urls:
            raise _Refusal("unsafe_remote")
        schemes = (PUSH_REMOTE_SCHEMES if operation == "push_branch" else
                   self._allowed_remote_schemes)
        schemes = schemes.intersection(self._allowed_remote_schemes)
        for url in urls:
            # scp-like syntax implicitly invokes ssh and can consult
            # ProxyCommand; it is intentionally outside the default catalog.
            parsed = urllib.parse.urlsplit(url)
            scheme = parsed.scheme.lower()
            if (scheme not in schemes or not parsed.hostname or parsed.query or
                    parsed.fragment or parsed.username is not None or
                    parsed.password is not None):
                raise _Refusal("unsafe_remote")
        return remote

    @staticmethod
    def _validate_ref(ref):
        if not isinstance(ref, str) or not _REF_RE.fullmatch(ref):
            raise _Refusal("invalid_arguments")
        if (".." in ref or "//" in ref or "@{" in ref or "\\" in ref or
                ref.endswith(("/", ".", ".lock")) or ref.startswith(("/", ".")) or
                any(part.startswith(".") or part.endswith(".lock")
                    for part in ref.split("/"))):
            raise _Refusal("invalid_arguments")
        return ref

    def _require_local_branch(self, executable, root, branch, deadline,
                              cancel_event):
        branch = self._validate_ref(branch)
        branches = self._git_lines(
            executable, root,
            ["for-each-ref", "--format=%(refname:short)", "refs/heads"],
            deadline, cancel_event)
        if branch not in branches:
            raise _Refusal("ref_missing")
        return branch

    @staticmethod
    def _reject_unknown(arguments, allowed):
        if not isinstance(arguments, dict) or any(
                not isinstance(key, str) or key not in allowed for key in arguments):
            raise _Refusal("invalid_arguments")

    def _build_git_args(self, executable, root, operation, arguments,
                        deadline, cancel_event):
        if operation == "status":
            self._reject_unknown(arguments, set())
            return ["status", "--porcelain=v2", "--branch", "--untracked-files=normal"]
        if operation == "remotes":
            self._reject_unknown(arguments, set())
            # Names only: `remote -v` can expose credentials embedded in URL.
            return ["remote"]
        if operation == "branches":
            self._reject_unknown(arguments, set())
            return ["for-each-ref",
                    "--format=%(refname:short)%00%(objectname)%00%(upstream:short)",
                    "refs/heads"]
        if operation == "current_branch":
            self._reject_unknown(arguments, set())
            return ["symbolic-ref", "--quiet", "--short", "HEAD"]
        if operation == "revision":
            self._reject_unknown(arguments, set())
            return ["rev-parse", "--verify", "HEAD"]
        if operation == "upstream":
            self._reject_unknown(arguments, set())
            return ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"]
        if operation == "divergence":
            self._reject_unknown(arguments, set())
            return ["rev-list", "--left-right", "--count", "HEAD...@{upstream}"]
        if operation == "fetch":
            self._reject_unknown(arguments, {"remote", "prune", "tags"})
            remote = self._validate_remote(
                executable, root, arguments.get("remote"), operation,
                deadline, cancel_event)
            prune = arguments.get("prune", False)
            tags = arguments.get("tags", False)
            if not isinstance(prune, bool) or not isinstance(tags, bool):
                raise _Refusal("invalid_arguments")
            args = ["fetch", "--no-recurse-submodules",
                    "--prune" if prune else "--no-prune",
                    "--tags" if tags else "--no-tags", "--", remote]
            return args
        if operation == "pull_ff_only":
            self._reject_unknown(arguments, {"remote", "branch"})
            remote = self._validate_remote(
                executable, root, arguments.get("remote"), operation,
                deadline, cancel_event)
            branch = self._validate_ref(arguments.get("branch"))
            return ["pull", "--ff-only", "--no-rebase", "--no-recurse-submodules",
                    "--", remote, "refs/heads/" + branch]
        if operation == "push_branch":
            self._reject_unknown(arguments, {"remote", "branch"})
            remote = self._validate_remote(
                executable, root, arguments.get("remote"), operation,
                deadline, cancel_event)
            branch = self._require_local_branch(
                executable, root, arguments.get("branch"), deadline, cancel_event)
            ref = "refs/heads/%s:refs/heads/%s" % (branch, branch)
            return ["push", "--porcelain", "--", remote, ref]
        raise _Refusal("unknown_operation")

    @staticmethod
    def _classify_failure(result):
        if result.get("ok"):
            return result
        if result.get("code") != "command_failed":
            return result
        text = (result.get("stderr", "") + "\n" + result.get("stdout", "")).lower()
        code = "command_failed"
        if any(token in text for token in (
                "authentication failed", "could not read username",
                "terminal prompts disabled", "not logged into", "http 401",
                "http 403", "permission denied (publickey)")):
            code = "authentication_required"
        elif any(token in text for token in (
                "would be overwritten by", "local changes to the following files",
                "unstaged changes", "uncommitted changes")):
            code = "dirty_worktree"
        elif any(token in text for token in (
                "non-fast-forward", "not possible to fast-forward", "fetch first")):
            code = "non_fast_forward"
        elif any(token in text for token in (
                "no upstream configured", "no upstream branch", "does not point to a branch")):
            code = "upstream_missing"
        result = dict(result)
        result["code"] = code
        result["detail"] = _PUBLIC_DETAILS[code]
        return result

    def run_git(self, target_id, operation, arguments=None, *, timeout_s=None,
                output_limit=None, cancel_event=None):
        started = time.monotonic()
        lock = None
        acquired = False
        try:
            _validate_target_id(target_id)
            if operation not in GIT_CATALOG:
                raise _Refusal("unknown_operation")
            arguments = {} if arguments is None else arguments
            timeout = _validate_timeout(timeout_s)
            limit = _validate_output_limit(output_limit)
            deadline = started + timeout
            executable = self._resolve_executable(self._git_candidate)
            root = self._resolve_root(target_id)
            self._require_structured_executable(executable, root)
            if GIT_CATALOG[operation]["mutating"]:
                lock = self._worktree_lock(root)
                self._acquire_lock(lock, deadline, cancel_event)
                acquired = True
            self._validate_repository(executable, root, deadline, cancel_event)
            self._preflight_executable_config(
                executable, root, operation, deadline, cancel_event)
            argv = self._build_git_args(
                executable, root, operation, arguments, deadline, cancel_event)
            self._revalidate_root(target_id, root)
            self._audit("host_operation_started", capability=HOST_GIT_CAPABILITY,
                        operation=operation, target_id=target_id,
                        mutating=GIT_CATALOG[operation]["mutating"],
                        arguments=dict(arguments))
            result = self._runner.run(
                executable=executable,
                argv=self._git_prefix(root) + argv,
                cwd=root,
                environment=self._structured_environment(root, [executable]),
                timeout_s=self._remaining(deadline),
                output_limit=limit,
                cancel_event=cancel_event,
                secret_values=(),
            )
            result = self._classify_failure(result)
            result.update({"capability": HOST_GIT_CAPABILITY,
                           "operation": operation, "target_id": target_id,
                           "duration_ms": int((time.monotonic() - started) * 1000)})
            self._audit("host_operation_finished", capability=HOST_GIT_CAPABILITY,
                        operation=operation, target_id=target_id,
                        code=result.get("code"), exit_code=result.get("exit_code"),
                        truncated=bool(result.get("truncated")),
                        duration_ms=result["duration_ms"])
            return _public_result(bool(result.get("ok")), result.get("code", "internal_error"),
                                  **{k: v for k, v in result.items()
                                     if k not in {"ok", "code", "detail"}})
        except _Refusal as exc:
            safe_target = _safe_target_field(target_id)
            safe_operation = _safe_operation_field(operation, GIT_CATALOG)
            self._audit("host_operation_refused", capability=HOST_GIT_CAPABILITY,
                        operation=safe_operation, target_id=safe_target,
                        code=exc.code)
            return _public_result(False, exc.code, capability=HOST_GIT_CAPABILITY,
                                  operation=safe_operation, target_id=safe_target,
                                  duration_ms=int((time.monotonic() - started) * 1000))
        except Exception:
            return _public_result(False, "internal_error", capability=HOST_GIT_CAPABILITY,
                                  duration_ms=int((time.monotonic() - started) * 1000))
        finally:
            if acquired:
                lock.release()

    @staticmethod
    def _bounded_int(value, *, minimum=1, maximum=100):
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
            raise _Refusal("invalid_arguments")
        return value

    def _build_gh_args(self, operation, arguments):
        if operation == "auth_status":
            self._reject_unknown(arguments, set())
            return ["auth", "status"], False
        if operation == "repo_view":
            self._reject_unknown(arguments, set())
            return ["repo", "view", "--json",
                    "nameWithOwner,url,defaultBranchRef,isPrivate"], True
        if operation == "pr_list":
            self._reject_unknown(arguments, {"limit", "state"})
            limit = self._bounded_int(arguments.get("limit", 30))
            state = arguments.get("state", "open")
            if state not in {"open", "closed", "merged", "all"}:
                raise _Refusal("invalid_arguments")
            return ["pr", "list", "--limit", str(limit), "--state", state,
                    "--json", "number,title,state,headRefName,baseRefName,url"], True
        if operation in {"pr_view", "pr_checks"}:
            self._reject_unknown(arguments, {"number"})
            number = self._bounded_int(arguments.get("number"), maximum=2 ** 31 - 1)
            if operation == "pr_view":
                return ["pr", "view", str(number), "--json",
                        "number,title,state,headRefName,baseRefName,url,mergeable"], True
            return ["pr", "checks", str(number), "--json",
                    "name,state,link,bucket"], True
        if operation == "workflow_list":
            self._reject_unknown(arguments, {"limit"})
            limit = self._bounded_int(arguments.get("limit", 50))
            return ["workflow", "list", "--limit", str(limit), "--json",
                    "id,name,path,state"], True
        if operation == "run_list":
            self._reject_unknown(arguments, {"limit", "status"})
            limit = self._bounded_int(arguments.get("limit", 30))
            status = arguments.get("status")
            allowed = {None, "queued", "in_progress", "completed", "success",
                       "failure", "cancelled", "skipped", "timed_out",
                       "action_required", "neutral", "stale", "startup_failure"}
            if status not in allowed:
                raise _Refusal("invalid_arguments")
            args = ["run", "list", "--limit", str(limit)]
            if status:
                args.extend(["--status", status])
            args.extend(["--json", "databaseId,name,status,conclusion,workflowName,url"])
            return args, True
        if operation == "run_view":
            self._reject_unknown(arguments, {"run_id"})
            run_id = self._bounded_int(arguments.get("run_id"), maximum=2 ** 63 - 1)
            return ["run", "view", str(run_id), "--json",
                    "databaseId,name,status,conclusion,workflowName,url,jobs"], True
        raise _Refusal("unknown_operation")

    def run_gh(self, target_id, operation, arguments=None, *, timeout_s=None,
               output_limit=None, cancel_event=None):
        started = time.monotonic()
        try:
            _validate_target_id(target_id)
            if operation not in GH_CATALOG:
                raise _Refusal("unknown_operation")
            arguments = {} if arguments is None else arguments
            timeout = _validate_timeout(timeout_s)
            limit = _validate_output_limit(output_limit)
            deadline = started + timeout
            executable = self._resolve_executable(self._gh_candidate)
            root = self._resolve_root(target_id)
            self._require_structured_executable(executable, root)
            argv, expects_json = self._build_gh_args(operation, arguments)
            executables = [executable]
            if operation != "auth_status":
                git_executable = self._resolve_executable(self._git_candidate)
                self._require_structured_executable(git_executable, root)
                self._validate_repository(
                    git_executable, root, deadline, cancel_event)
                executables.append(git_executable)
            self._revalidate_root(target_id, root)
            environment = self._structured_environment(
                root, executables, include_gh=True)
            secrets = _sensitive_values(environment)
            self._audit("host_operation_started", capability=HOST_GH_CAPABILITY,
                        operation=operation, target_id=target_id, mutating=False,
                        arguments=dict(arguments))
            result = self._runner.run(
                executable=executable, argv=argv, cwd=root,
                environment=environment, timeout_s=self._remaining(deadline),
                output_limit=limit, cancel_event=cancel_event,
                secret_values=secrets,
            )
            result = self._classify_failure(result)
            if result.get("ok") and expects_json:
                try:
                    result["data"] = json.loads(result.get("stdout", ""))
                except (TypeError, ValueError):
                    result["ok"] = False
                    result["code"] = "invalid_command_output"
                    result["detail"] = _PUBLIC_DETAILS["invalid_command_output"]
            result.update({"capability": HOST_GH_CAPABILITY,
                           "operation": operation, "target_id": target_id,
                           "duration_ms": int((time.monotonic() - started) * 1000)})
            self._audit("host_operation_finished", capability=HOST_GH_CAPABILITY,
                        operation=operation, target_id=target_id,
                        code=result.get("code"), exit_code=result.get("exit_code"),
                        truncated=bool(result.get("truncated")),
                        duration_ms=result["duration_ms"])
            return _public_result(bool(result.get("ok")), result.get("code", "internal_error"),
                                  **{k: v for k, v in result.items()
                                     if k not in {"ok", "code", "detail"}})
        except _Refusal as exc:
            safe_target = _safe_target_field(target_id)
            safe_operation = _safe_operation_field(operation, GH_CATALOG)
            self._audit("host_operation_refused", capability=HOST_GH_CAPABILITY,
                        operation=safe_operation, target_id=safe_target,
                        code=exc.code)
            return _public_result(False, exc.code, capability=HOST_GH_CAPABILITY,
                                  operation=safe_operation, target_id=safe_target,
                                  duration_ms=int((time.monotonic() - started) * 1000))
        except Exception:
            return _public_result(False, "internal_error", capability=HOST_GH_CAPABILITY,
                                  duration_ms=int((time.monotonic() - started) * 1000))

    def _shell_allowed(self, target_id):
        if not callable(self._full_shell_policy):
            return False
        try:
            # Require literal True: truthy strings/objects from a malformed
            # projection must never broaden host execution permission.
            return self._full_shell_policy(target_id) is True
        except Exception:
            return False

    def _shell_spec(self):
        if self._shell_candidate:
            executable = self._resolve_executable(self._shell_candidate)
            if self.platform == "win32":
                return executable, ["-NoLogo", "-NoProfile", "-NonInteractive", "-Command"]
            return executable, ["-f", "-c"] if self.platform == "darwin" else ["-c"]
        if self.platform == "win32":
            executable = self._resolve_executable("powershell.exe")
            return executable, ["-NoLogo", "-NoProfile", "-NonInteractive", "-Command"]
        if self.platform == "darwin":
            return self._resolve_executable("/bin/zsh"), ["-f", "-c"]
        return self._resolve_executable("/bin/sh"), ["-c"]

    def _resolve_relative_cwd(self, root, relative):
        if relative is None or relative == "":
            return root, "."
        if not isinstance(relative, str) or "\0" in relative:
            raise _Refusal("worktree_escape")
        # Reject both path dialects independent of the executing OS; a Windows
        # drive/UNC path must not become an innocent-looking POSIX filename in
        # a cross-platform request (or vice versa).
        if ntpath.isabs(relative) or posixpath.isabs(relative) or ntpath.splitdrive(relative)[0]:
            raise _Refusal("worktree_escape")
        candidate = os.path.realpath(os.path.join(root, relative))
        if not _path_within(root, candidate):
            raise _Refusal("worktree_escape")
        if not os.path.isdir(candidate):
            raise _Refusal("worktree_unavailable")
        rel = os.path.relpath(candidate, root).replace("\\", "/")
        return candidate, rel

    def _shell_environment(self, additions):
        environment = _safe_environment(self._environment)
        if additions is None:
            return environment, _sensitive_values(environment)
        if not isinstance(additions, dict) or len(additions) > MAX_ENV_ADDITIONS:
            raise _Refusal("invalid_environment")
        for key, value in additions.items():
            if (not isinstance(key, str) or not _ENV_NAME_RE.fullmatch(key) or
                    _BLOCKED_ENV_ADDITION_RE.fullmatch(key) or
                    not isinstance(value, str) or "\0" in value or
                    len(value.encode("utf-8")) > MAX_ENV_VALUE_BYTES):
                raise _Refusal("invalid_environment")
            environment[key] = value
        _validate_environment(environment)
        # Environment additions are data supplied for a command, not result
        # data.  Treat every value as sensitive even when its key has an
        # innocuous name; this avoids turning `FOO=<credential>` into an output
        # oracle merely because the caller chose a poor variable name.
        explicit_values = list(additions.values())
        if sum(len(value.encode("utf-8")) for value in explicit_values) > MAX_REDACT_BYTES:
            raise _Refusal("invalid_environment")
        return environment, _sensitive_values(environment) + explicit_values

    def run_shell(self, target_id, command, *, cwd=None, env_additions=None,
                  timeout_s=None, output_limit=None, cancel_event=None,
                  redact_values=()):
        """Execute a raw platform shell command behind the local-only gate.

        The command is never returned or audited.  ``cwd`` is relative to the
        registered worktree and may not escape through ``..`` or a symlink.
        """
        started = time.monotonic()
        try:
            _validate_target_id(target_id)
            if not self._shell_allowed(target_id):
                raise _Refusal("shell_disabled")
            if (not isinstance(command, str) or not command.strip() or "\0" in command or
                    len(command.encode("utf-8")) > MAX_SHELL_COMMAND_BYTES):
                raise _Refusal("invalid_arguments")
            redact_values = _normalize_secret_values(redact_values)
            timeout = _validate_timeout(timeout_s)
            limit = _validate_output_limit(output_limit)
            deadline = started + timeout
            root = self._resolve_root(target_id)
            working_dir, relative_cwd = self._resolve_relative_cwd(root, cwd)
            environment, secrets = self._shell_environment(env_additions)
            executable, prefix = self._shell_spec()
            self._revalidate_root(target_id, root)
            # The second read is intentionally adjacent to spawn.  The host
            # policy callback must consult durable host-private state each time.
            if not self._shell_allowed(target_id):
                raise _Refusal("shell_disabled")
            self._audit("host_operation_started", capability=HOST_SHELL_CAPABILITY,
                        operation="execute", target_id=target_id, mutating=True,
                        cwd=relative_cwd)
            combined_secrets = _normalize_secret_values(
                list(secrets) + list(redact_values))
            result = self._runner.run(
                executable=executable,
                argv=prefix + [command],
                cwd=working_dir,
                environment=environment,
                timeout_s=self._remaining(deadline),
                output_limit=limit,
                cancel_event=cancel_event,
                secret_values=combined_secrets,
            )
            result.update({"capability": HOST_SHELL_CAPABILITY,
                           "operation": "execute", "target_id": target_id,
                           "cwd": relative_cwd,
                           "duration_ms": int((time.monotonic() - started) * 1000)})
            self._audit("host_operation_finished", capability=HOST_SHELL_CAPABILITY,
                        operation="execute", target_id=target_id,
                        code=result.get("code"), exit_code=result.get("exit_code"),
                        truncated=bool(result.get("truncated")),
                        duration_ms=result["duration_ms"])
            return _public_result(bool(result.get("ok")), result.get("code", "internal_error"),
                                  **{k: v for k, v in result.items()
                                     if k not in {"ok", "code", "detail"}})
        except _Refusal as exc:
            safe_target = _safe_target_field(target_id)
            self._audit("host_operation_refused", capability=HOST_SHELL_CAPABILITY,
                        operation="execute", target_id=safe_target,
                        code=exc.code)
            return _public_result(False, exc.code, capability=HOST_SHELL_CAPABILITY,
                                  operation="execute",
                                  target_id=safe_target,
                                  duration_ms=int((time.monotonic() - started) * 1000))
        except Exception:
            return _public_result(False, "internal_error", capability=HOST_SHELL_CAPABILITY,
                                  duration_ms=int((time.monotonic() - started) * 1000))


__all__ = [
    "DEFAULT_OUTPUT_LIMIT", "DEFAULT_TIMEOUT_S", "GH_CATALOG", "GIT_CATALOG",
    "HOST_GH_CAPABILITY", "HOST_GIT_CAPABILITY", "HOST_SHELL_CAPABILITY",
    "HostOperations", "MAX_OUTPUT_LIMIT", "MAX_TIMEOUT_S", "ProcessRunner",
    "operation_catalog",
]
