#!/usr/bin/env python3
"""Cross-machine Convoy hardware end-to-end harness.

The harness deliberately depends only on the Python standard library.  It
talks to the origin HostApp over its authenticated loopback API and uses
OpenSSH only to ask another computer to probe *its own* loopback HostApp.
No HostApp token is ever returned over stdout, written to the result files,
or placed in the JSON configuration.

Default execution is nondestructive: discovery, topology checks, ping, and a
read-only Envoy call.  Every write/cancellation/export requires
``--allow-mutation``.  Network dropout and process restarts additionally
require ``--allow-disruption``.
"""

from __future__ import annotations

import argparse
import base64
import dataclasses
import datetime as _datetime
import hashlib
import http.client
import json
import math
import os
import pathlib
import platform
import re
import secrets
import subprocess
import sys
import tempfile
import time
import urllib.parse
import xml.etree.ElementTree as ET
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


SCHEMA_VERSION = 1
RESULT_SCHEMA_VERSION = 1
MAX_JSON_BYTES = 4 * 1024 * 1024
MAX_ARTIFACT_BYTES = 512 * 1024 * 1024
TERMINAL_STATES = frozenset(
    ("succeeded", "failed", "cancelled", "canceled", "expired",
     "indeterminate", "refused")
)
ALL_SCENARIOS = (
    "ping", "read", "mutation", "artifact", "cancel", "dropout",
    "host_restart", "td_restart",
)
MUTATING_SCENARIOS = frozenset(
    ("mutation", "artifact", "cancel", "dropout", "host_restart",
     "td_restart")
)
DISRUPTIVE_SCENARIOS = frozenset(("dropout", "host_restart", "td_restart"))
TOKEN_HEADER = "X-Convoy-Host-Token"
SENSITIVE_KEY = re.compile(
    r"(?:^|_)(?:authorization|cookie|password|passphrase|secret|token|"
    r"private_key|api_key|credential|capability)(?:$|_)", re.IGNORECASE)
SAFE_HOST = re.compile(r"^[A-Za-z0-9_.:-]+$")
SAFE_USER = re.compile(r"^[A-Za-z0-9_.-]+$")
SAFE_PYTHON_COMMAND = re.compile(r"^[A-Za-z0-9_ .:/+\\-]+$")
ARTIFACT_ID = re.compile(r"^art_[0-9a-f]{64}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")


class HarnessError(RuntimeError):
    """An expected, operator-actionable scenario failure."""


class ConfigError(HarnessError):
    """The JSON configuration is unsafe or incomplete."""


class SkipScenario(HarnessError):
    """A configured prerequisite is absent; report a skip, not a pass."""


def utc_now() -> str:
    return (_datetime.datetime.now(_datetime.timezone.utc)
            .isoformat(timespec="milliseconds").replace("+00:00", "Z"))


def strict_json_loads(raw: str) -> Any:
    def reject(name: str) -> None:
        raise ValueError("non-finite JSON constant %s" % name)
    return json.loads(raw, parse_constant=reject)


def _bounded_text(value: Any, name: str, limit: int = 512) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError("%s must be non-empty text" % name)
    if len(value.encode("utf-8", "strict")) > limit:
        raise ConfigError("%s exceeds %d UTF-8 bytes" % (name, limit))
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ConfigError("%s contains control characters" % name)
    return value


def _mapping(value: Any, name: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError("%s must be an object" % name)
    return value


def _finite_positive(value: Any, name: str, default: float) -> float:
    if value is None:
        return default
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(float(value)) or float(value) <= 0):
        raise ConfigError("%s must be a positive finite number" % name)
    return float(value)


def _reject_config_secrets(value: Any, path: str = "config") -> None:
    """Configuration may reference key files, but may never contain secrets."""
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ConfigError("%s has a non-text key" % path)
            if SENSITIVE_KEY.search(key) and key not in ("identity_file",):
                raise ConfigError(
                    "%s.%s looks like credential material; use SSH agent or "
                    "an identity_file path instead" % (path, key))
            _reject_config_secrets(item, "%s.%s" % (path, key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_config_secrets(item, "%s[%d]" % (path, index))


def _resolve_optional_path(value: Any, base: pathlib.Path,
                           name: str, must_exist: bool = False) -> Optional[str]:
    if value in (None, ""):
        return None
    text = _bounded_text(value, name, 4096)
    path = pathlib.Path(text).expanduser()
    if not path.is_absolute():
        path = base / path
    path = path.resolve()
    if must_exist and not path.is_file():
        raise ConfigError("%s does not name a readable file: %s" % (name, path))
    return str(path)


def load_config(path: str) -> Dict[str, Any]:
    config_path = pathlib.Path(path).expanduser().resolve()
    try:
        raw = config_path.read_bytes()
    except OSError as exc:
        raise ConfigError("cannot read config: %s" % type(exc).__name__) from exc
    if len(raw) > 1024 * 1024:
        raise ConfigError("config exceeds 1 MiB")
    try:
        data = strict_json_loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ConfigError("config is not strict UTF-8 JSON") from exc
    data = _mapping(data, "config")
    _reject_config_secrets(data)
    if data.get("schema_version") != SCHEMA_VERSION:
        raise ConfigError("schema_version must be %d" % SCHEMA_VERSION)
    data["convoy_id"] = _bounded_text(data.get("convoy_id"), "convoy_id", 256)

    local = _mapping(data.get("local", {}), "local")
    remote = _mapping(data.get("remote"), "remote")
    scenarios = _mapping(data.get("scenarios", {}), "scenarios")
    timeouts = _mapping(data.get("timeouts", {}), "timeouts")
    base = config_path.parent

    local["data_dir"] = _resolve_optional_path(
        local.get("data_dir"), base, "local.data_dir")
    if local.get("node_id") not in (None, ""):
        local["node_id"] = _bounded_text(local["node_id"], "local.node_id")

    ssh_host = _bounded_text(remote.get("ssh_host"), "remote.ssh_host", 255)
    if not SAFE_HOST.fullmatch(ssh_host) or ssh_host.startswith("-"):
        raise ConfigError("remote.ssh_host has an unsafe OpenSSH shape")
    remote["ssh_host"] = ssh_host
    if remote.get("ssh_user") not in (None, ""):
        ssh_user = _bounded_text(remote["ssh_user"], "remote.ssh_user", 128)
        if not SAFE_USER.fullmatch(ssh_user):
            raise ConfigError("remote.ssh_user has an unsafe shape")
        remote["ssh_user"] = ssh_user
    port = remote.get("ssh_port", 22)
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ConfigError("remote.ssh_port must be an integer in [1, 65535]")
    remote["ssh_port"] = port
    remote["target_node_id"] = _bounded_text(
        remote.get("target_node_id"), "remote.target_node_id")
    remote["known_hosts_file"] = _resolve_optional_path(
        remote.get("known_hosts_file"), base,
        "remote.known_hosts_file", must_exist=True)
    remote["identity_file"] = _resolve_optional_path(
        remote.get("identity_file"), base,
        "remote.identity_file", must_exist=True)
    python_command = remote.get("python_command", "python")
    python_command = _bounded_text(
        python_command, "remote.python_command", 256)
    if not SAFE_PYTHON_COMMAND.fullmatch(python_command):
        raise ConfigError("remote.python_command contains shell metacharacters")
    remote["python_command"] = python_command
    remote["ssh_executable"] = _bounded_text(
        remote.get("ssh_executable", "ssh"), "remote.ssh_executable", 4096)
    remote["data_dir"] = remote.get("data_dir") or None
    if remote["data_dir"] is not None:
        _bounded_text(remote["data_dir"], "remote.data_dir", 4096)

    timeout_defaults = {
        "http_s": 10.0, "ssh_s": 20.0, "job_s": 90.0,
        "reconnect_s": 90.0, "poll_s": 0.5,
    }
    for key, default in timeout_defaults.items():
        timeouts[key] = _finite_positive(
            timeouts.get(key), "timeouts.%s" % key, default)

    for name, value in scenarios.items():
        if name not in ALL_SCENARIOS:
            raise ConfigError("unknown scenario %r" % name)
        scenario = _mapping(value, "scenarios.%s" % name)
        enabled = scenario.get("enabled", False)
        if not isinstance(enabled, bool):
            raise ConfigError("scenarios.%s.enabled must be boolean" % name)
        if scenario.get("operation") not in (None, ""):
            _bounded_text(scenario["operation"],
                          "scenarios.%s.operation" % name, 128)
        if "arguments" in scenario and not isinstance(scenario["arguments"], dict):
            raise ConfigError("scenarios.%s.arguments must be an object" % name)
        for command_name in ("command", "self_reverting_command"):
            command = scenario.get(command_name)
            if command not in (None, ""):
                _bounded_text(command, "scenarios.%s.%s" %
                              (name, command_name), 8192)
        if "accepted_exit_codes" in scenario:
            codes = scenario["accepted_exit_codes"]
            if (not isinstance(codes, list) or not codes
                    or any(isinstance(code, bool) or not isinstance(code, int)
                           or code < -255 or code > 255 for code in codes)):
                raise ConfigError(
                    "scenarios.%s.accepted_exit_codes must be bounded integers"
                    % name)
        if name == "mutation" and "cleanup" in scenario:
            cleanup = _mapping(scenario["cleanup"],
                               "scenarios.mutation.cleanup")
            if cleanup.get("operation") not in (None, ""):
                _bounded_text(cleanup["operation"],
                              "scenarios.mutation.cleanup.operation", 128)
            if ("arguments" in cleanup
                    and not isinstance(cleanup["arguments"], dict)):
                raise ConfigError(
                    "scenarios.mutation.cleanup.arguments must be an object")
        if name == "artifact":
            for field in ("export_project_root", "filename"):
                if scenario.get(field) not in (None, ""):
                    _bounded_text(scenario[field],
                                  "scenarios.artifact.%s" % field, 4096)
        if name == "cancel" and "expected_terminal_states" in scenario:
            states = scenario["expected_terminal_states"]
            if (not isinstance(states, list) or not states
                    or any(not isinstance(state, str)
                           or state not in TERMINAL_STATES for state in states)):
                raise ConfigError(
                    "scenarios.cancel.expected_terminal_states is invalid")
        if name == "td_restart" and scenario.get("policy", "require_clean") \
                not in ("require_clean", "save_then_restart"):
            raise ConfigError("scenarios.td_restart.policy is invalid")

    data["local"] = local
    data["remote"] = remote
    data["scenarios"] = scenarios
    data["timeouts"] = timeouts
    data["_config_path"] = str(config_path)
    expected = data.get("expected_embody_version")
    if expected not in (None, ""):
        data["expected_embody_version"] = _bounded_text(
            expected, "expected_embody_version", 64)
    return data


def redact(value: Any, secret_values: Iterable[str] = ()) -> Any:
    """Return a JSON-safe, recursively redacted result projection."""
    needles = tuple(item for item in secret_values
                    if isinstance(item, str) and item)

    def clean(item: Any, key: Optional[str] = None) -> Any:
        if key is not None and SENSITIVE_KEY.search(key):
            return "[REDACTED]"
        if dataclasses.is_dataclass(item):
            item = dataclasses.asdict(item)
        if isinstance(item, dict):
            return {str(k): clean(v, str(k)) for k, v in item.items()}
        if isinstance(item, (list, tuple, set, frozenset)):
            return [clean(entry) for entry in item]
        if isinstance(item, bytes):
            return "[bytes:%d]" % len(item)
        if isinstance(item, str):
            result = item
            for needle in needles:
                result = result.replace(needle, "[REDACTED]")
            return result[:16384]
        if item is None or isinstance(item, (bool, int)):
            return item
        if isinstance(item, float):
            return item if math.isfinite(item) else "[non-finite]"
        return str(item)[:1024]

    return clean(value)


@dataclasses.dataclass
class CaseResult:
    name: str
    category: str
    status: str
    duration_s: float
    message: str = ""
    details: Any = None


class ResultRecorder:
    def __init__(self, run_id: str, started_at: Optional[str] = None,
                 secret_values: Iterable[str] = ()):
        self.run_id = run_id
        self.started_at = started_at or utc_now()
        self.secret_values = list(secret_values)
        self.cases: List[CaseResult] = []

    def add(self, name: str, category: str, status: str,
            duration_s: float, message: str = "", details: Optional[Any] = None) -> None:
        if status not in ("passed", "failed", "skipped", "error"):
            raise ValueError("invalid case status")
        safe_message = redact(message, self.secret_values)
        safe_details = redact(details, self.secret_values)
        self.cases.append(CaseResult(
            name=name, category=category, status=status,
            duration_s=max(0.0, float(duration_s)),
            message=str(safe_message), details=safe_details))

    def summary(self) -> Dict[str, int]:
        result = {key: 0 for key in ("passed", "failed", "skipped", "error")}
        for case in self.cases:
            result[case.status] += 1
        result["total"] = len(self.cases)
        return result

    def document(self, metadata: Optional[dict] = None) -> Dict[str, Any]:
        return {
            "schema_version": RESULT_SCHEMA_VERSION,
            "run_id": self.run_id,
            "started_at": self.started_at,
            "finished_at": utc_now(),
            "runner": {"python": platform.python_version(),
                       "platform": platform.platform()},
            "metadata": redact(metadata or {}, self.secret_values),
            "summary": self.summary(),
            "cases": [dataclasses.asdict(case) for case in self.cases],
        }

    def junit(self, suite_name: str = "convoy.hardware") -> ET.ElementTree:
        summary = self.summary()
        suite = ET.Element("testsuite", {
            "name": suite_name,
            "tests": str(summary["total"]),
            "failures": str(summary["failed"]),
            "errors": str(summary["error"]),
            "skipped": str(summary["skipped"]),
            "time": "%.6f" % sum(case.duration_s for case in self.cases),
        })
        for case in self.cases:
            node = ET.SubElement(suite, "testcase", {
                "classname": "convoy.hardware.%s" % case.category,
                "name": case.name,
                "time": "%.6f" % case.duration_s,
            })
            if case.status == "failed":
                child = ET.SubElement(node, "failure", {
                    "message": case.message or "scenario failed"})
                child.text = case.message
            elif case.status == "error":
                child = ET.SubElement(node, "error", {
                    "message": case.message or "harness error"})
                child.text = case.message
            elif case.status == "skipped":
                ET.SubElement(node, "skipped", {
                    "message": case.message or "prerequisite absent"})
            if case.details not in (None, {}, []):
                out = ET.SubElement(node, "system-out")
                out.text = json.dumps(case.details, indent=2, sort_keys=True,
                                      ensure_ascii=False, allow_nan=False)
        return ET.ElementTree(suite)


def _atomic_write(path: str, payload: bytes) -> None:
    target = pathlib.Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp = tempfile.mkstemp(
        prefix=".%s." % target.name, suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp, target)
        temp = ""
    finally:
        if temp:
            try:
                os.unlink(temp)
            except FileNotFoundError:
                pass


def write_results(recorder: ResultRecorder, json_path: str, junit_path: str,
                  metadata: Optional[dict] = None) -> None:
    document = recorder.document(metadata)
    raw = json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False,
                     allow_nan=False).encode("utf-8") + b"\n"
    _atomic_write(json_path, raw)
    tree = recorder.junit()
    if hasattr(ET, "indent"):  # Python 3.9+; valid compact XML on 3.8.
        ET.indent(tree, space="  ")
    xml = ET.tostring(tree.getroot(), encoding="utf-8", xml_declaration=True)
    _atomic_write(junit_path, xml + b"\n")


@dataclasses.dataclass
class HostHandle:
    data_dir: str
    port: int
    host_id: str
    token: str = dataclasses.field(repr=False)


class HostClient:
    def __init__(self, handle: HostHandle, timeout_s: float = 10.0):
        self.handle = handle
        self.timeout_s = timeout_s

    def _request(self, method: str, path: str, payload: Optional[dict] = None,
                 authenticated: bool = True,
                 max_bytes: int = MAX_JSON_BYTES) -> Tuple[int, Mapping[str, str], bytes]:
        parsed = urllib.parse.urlsplit(path)
        if (method not in ("GET", "POST") or parsed.scheme or parsed.netloc
                or not parsed.path.startswith("/") or "\r" in path or "\n" in path):
            raise HarnessError("refused unsafe loopback HTTP route")
        body = None
        headers = {"Connection": "close"}
        if authenticated:
            headers[TOKEN_HEADER] = self.handle.token
        if payload is not None:
            body = json.dumps(payload, allow_nan=False,
                              separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.handle.port, timeout=self.timeout_s)
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            raw = response.read(max_bytes + 1)
            if len(raw) > max_bytes:
                raise HarnessError("HostApp response exceeded %d bytes" % max_bytes)
            return (response.status,
                    {key.lower(): value for key, value in response.getheaders()},
                    raw)
        except (OSError, http.client.HTTPException) as exc:
            raise HarnessError("HostApp transport failed: %s" %
                               type(exc).__name__) from exc
        finally:
            connection.close()

    def json(self, method: str, path: str,
             payload: Optional[dict] = None,
             authenticated: bool = True) -> Tuple[int, Dict[str, Any]]:
        status, _headers, raw = self._request(
            method, path, payload, authenticated=authenticated)
        try:
            value = strict_json_loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise HarnessError("HostApp returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise HarnessError("HostApp returned a non-object JSON response")
        return status, value

    def require_ok(self, method: str, path: str,
                   payload: Optional[dict] = None,
                   authenticated: bool = True) -> Dict[str, Any]:
        status, value = self.json(method, path, payload, authenticated)
        if status < 200 or status >= 300 or value.get("ok") is not True:
            raise HarnessError("%s %s refused: HTTP %d %s" % (
                method, path, status, value.get("reason", "unknown")))
        return value

    def raw_get(self, path: str) -> Tuple[Mapping[str, str], bytes]:
        status, headers, raw = self._request(
            "GET", path, authenticated=True, max_bytes=MAX_ARTIFACT_BYTES)
        if status != 200:
            try:
                reason = strict_json_loads(raw.decode("utf-8")).get("reason")
            except Exception:
                reason = "unknown"
            raise HarnessError("artifact GET failed: HTTP %d %s" %
                               (status, reason))
        return headers, raw

    def snapshot(self, convoy_id: str) -> Dict[str, Any]:
        encoded = urllib.parse.quote(convoy_id, safe="")
        return {
            "health": self.require_ok("GET", "/health", authenticated=False),
            "status": self.require_ok("GET", "/status"),
            "lan": self.require_ok("GET", "/lan/status"),
            "nodes": self.require_ok("GET", "/nodes"),
            "peers": self.require_ok("GET", "/peers"),
            "network_nodes": self.require_ok(
                "GET", "/network/nodes?convoy_id=" + encoded),
        }


def default_convoy_data_dir() -> str:
    home = os.path.expanduser("~")
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.join(
            home, "AppData", "Local")
        return os.path.join(base, "EmbodyConvoy")
    if sys.platform == "darwin":
        return os.path.join(home, "Library", "Application Support",
                            "EmbodyConvoy")
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(home, ".local", "share")
    return os.path.join(base, "EmbodyConvoy")


def discover_local_host(config: Mapping[str, Any]) -> Tuple[HostClient, Dict[str, Any]]:
    data_dir = config["local"].get("data_dir") or default_convoy_data_dir()
    portfile_path = os.path.join(data_dir, "host.portfile.json")
    token_path = os.path.join(data_dir, "host.token")
    try:
        with open(portfile_path, "rb") as source:
            raw = source.read(64 * 1024 + 1)
        if len(raw) > 64 * 1024:
            raise HarnessError("local HostApp portfile is oversized")
        portfile = strict_json_loads(raw.decode("utf-8"))
        port = portfile.get("port")
        host_id = portfile.get("host_id")
        if (isinstance(port, bool) or not isinstance(port, int)
                or not 1 <= port <= 65535 or not isinstance(host_id, str)
                or not host_id):
            raise HarnessError("local HostApp portfile is malformed")
    except HarnessError:
        raise
    except (OSError, UnicodeDecodeError, ValueError, AttributeError) as exc:
        raise HarnessError("local HostApp portfile is unavailable or invalid") from exc

    unauth = HostClient(HostHandle(data_dir, port, host_id, ""),
                        config["timeouts"]["http_s"])
    health = unauth.require_ok("GET", "/health", authenticated=False)
    if health.get("host_id") != host_id:
        raise HarnessError("local HostApp identity does not match its portfile")
    try:
        with open(token_path, "r", encoding="utf-8") as source:
            token = source.read(4097).strip()
    except (OSError, UnicodeError) as exc:
        raise HarnessError("local HostApp token is unreadable") from exc
    if not token or len(token) > 4096:
        raise HarnessError("local HostApp token is malformed")
    client = HostClient(HostHandle(data_dir, port, host_id, token),
                        config["timeouts"]["http_s"])
    snapshot = client.snapshot(config["convoy_id"])
    return client, snapshot


_REMOTE_PROBE_SOURCE = r'''
import base64, http.client, json, os, platform, sys, urllib.parse

CFG = json.loads(base64.b64decode('__CONFIG__').decode('utf-8'))

def data_dir():
    if CFG.get('data_dir'):
        return os.path.abspath(os.path.expanduser(CFG['data_dir']))
    home = os.path.expanduser('~')
    if sys.platform == 'win32':
        base = os.environ.get('LOCALAPPDATA') or os.path.join(home, 'AppData', 'Local')
        return os.path.join(base, 'EmbodyConvoy')
    if sys.platform == 'darwin':
        return os.path.join(home, 'Library', 'Application Support', 'EmbodyConvoy')
    base = os.environ.get('XDG_DATA_HOME') or os.path.join(home, '.local', 'share')
    return os.path.join(base, 'EmbodyConvoy')

def request(port, token, method, path):
    headers = {'Connection': 'close'}
    if token:
        headers['X-Convoy-Host-Token'] = token
    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=CFG['http_s'])
    try:
        conn.request(method, path, headers=headers)
        response = conn.getresponse()
        raw = response.read(4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024:
            raise RuntimeError('HostApp response too large')
        value = json.loads(raw.decode('utf-8'), parse_constant=lambda x: (_ for _ in ()).throw(ValueError(x)))
        if not isinstance(value, dict):
            raise RuntimeError('HostApp returned non-object JSON')
        if response.status < 200 or response.status >= 300 or value.get('ok') is not True:
            raise RuntimeError('%s %s refused HTTP %s %s' % (method, path, response.status, value.get('reason')))
        return value
    finally:
        conn.close()

try:
    root = data_dir()
    with open(os.path.join(root, 'host.portfile.json'), 'r', encoding='utf-8') as source:
        portfile = json.load(source)
    port = portfile.get('port')
    expected = portfile.get('host_id')
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535 or not isinstance(expected, str) or not expected:
        raise RuntimeError('HostApp portfile is malformed')
    health = request(port, '', 'GET', '/health')
    if health.get('host_id') != expected:
        raise RuntimeError('HostApp identity does not match portfile')
    with open(os.path.join(root, 'host.token'), 'r', encoding='utf-8') as source:
        token = source.read(4097).strip()
    if not token or len(token) > 4096:
        raise RuntimeError('HostApp token is malformed')
    convoy = urllib.parse.quote(CFG['convoy_id'], safe='')
    result = {
        'ok': True,
        'platform': sys.platform,
        'python': platform.python_version(),
        'writer_pid': portfile.get('pid'),
        'port': port,
        'health': health,
        'status': request(port, token, 'GET', '/status'),
        'lan': request(port, token, 'GET', '/lan/status'),
        'nodes': request(port, token, 'GET', '/nodes'),
        'peers': request(port, token, 'GET', '/peers'),
        'network_nodes': request(port, token, 'GET', '/network/nodes?convoy_id=' + convoy),
    }
except BaseException as exc:
    result = {'ok': False, 'reason': 'remote_probe_failed',
              'error_type': type(exc).__name__, 'detail': str(exc)[:512],
              'platform': sys.platform}
print(json.dumps(result, separators=(',', ':'), ensure_ascii=True, allow_nan=False))
'''


def build_ssh_argv(remote: Mapping[str, Any], remote_command: str,
                   connect_timeout_s: float) -> List[str]:
    """Build an OpenSSH invocation that can never auto-trust a new key."""
    destination = remote["ssh_host"]
    if remote.get("ssh_user"):
        destination = "%s@%s" % (remote["ssh_user"], destination)
    argv = [
        remote.get("ssh_executable", "ssh"),
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=yes",
        "-o", "ConnectTimeout=%d" % max(1, int(math.ceil(connect_timeout_s))),
        "-p", str(remote.get("ssh_port", 22)),
    ]
    if remote.get("known_hosts_file"):
        argv.extend(["-o", "UserKnownHostsFile=%s" %
                     remote["known_hosts_file"]])
    if remote.get("identity_file"):
        argv.extend(["-o", "IdentitiesOnly=yes",
                     "-i", remote["identity_file"]])
    argv.extend([destination, remote_command])
    return argv


class SSHRemote:
    def __init__(self, config: Mapping[str, Any]):
        self.config = config
        self.remote = config["remote"]

    def _run(self, remote_command: str, timeout_s: Optional[float] = None,
             accepted_codes: Sequence[int] = (0,)) -> subprocess.CompletedProcess:
        timeout_s = timeout_s or self.config["timeouts"]["ssh_s"]
        argv = build_ssh_argv(self.remote, remote_command, timeout_s)
        try:
            result = subprocess.run(
                argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, encoding="utf-8",
                errors="replace", timeout=timeout_s, check=False)
        except FileNotFoundError as exc:
            raise HarnessError("OpenSSH executable was not found") from exc
        except subprocess.TimeoutExpired as exc:
            raise HarnessError("SSH command exceeded %.1fs" % timeout_s) from exc
        if result.returncode not in accepted_codes:
            stderr = redact(result.stderr.strip())
            raise HarnessError(
                "SSH failed with exit %d: %s. Host keys are strict; verify "
                "an unknown or changed fingerprint out of band and update "
                "known_hosts yourself." % (result.returncode, stderr))
        return result

    def probe(self) -> Dict[str, Any]:
        probe_config = {
            "convoy_id": self.config["convoy_id"],
            "http_s": self.config["timeouts"]["http_s"],
            "data_dir": self.remote.get("data_dir"),
        }
        encoded_config = base64.b64encode(json.dumps(
            probe_config, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")).decode("ascii")
        source = _REMOTE_PROBE_SOURCE.replace("__CONFIG__", encoded_config)
        encoded_source = base64.b64encode(source.encode("utf-8")).decode("ascii")
        launcher = "import base64;exec(base64.b64decode('%s'))" % encoded_source
        command = '%s -c "%s"' % (self.remote["python_command"], launcher)
        result = self._run(command)
        if len(result.stdout.encode("utf-8")) > MAX_JSON_BYTES:
            raise HarnessError("remote probe output exceeded 4 MiB")
        try:
            value = strict_json_loads(result.stdout.strip())
        except ValueError as exc:
            raise HarnessError("remote probe did not return one JSON object") from exc
        if not isinstance(value, dict):
            raise HarnessError("remote probe returned non-object JSON")
        if value.get("ok") is not True:
            raise HarnessError("remote probe failed: %s: %s" % (
                value.get("error_type", value.get("reason", "unknown")),
                value.get("detail", "")))
        return value

    def command(self, command: str, timeout_s: float,
                accepted_codes: Sequence[int]) -> Dict[str, Any]:
        _bounded_text(command, "disruption command", 8192)
        result = self._run(command, timeout_s=timeout_s,
                           accepted_codes=accepted_codes)
        return {"returncode": result.returncode,
                "stdout": redact(result.stdout.strip())[:8192],
                "stderr": redact(result.stderr.strip())[:8192]}


def _nodes(snapshot: Mapping[str, Any]) -> List[dict]:
    rows = snapshot.get("nodes", {}).get("nodes", [])
    return [row for row in rows if isinstance(row, dict)]


def _find_node(snapshot: Mapping[str, Any], node_id: str) -> Optional[dict]:
    return next((row for row in _nodes(snapshot)
                 if row.get("node_id") == node_id), None)


def _job_from_response(value: Mapping[str, Any]) -> Optional[dict]:
    job = value.get("job")
    if isinstance(job, dict):
        return job
    if isinstance(value.get("state"), str):
        return dict(value)
    return None


def _delivery_id(value: Mapping[str, Any]) -> Optional[str]:
    direct = value.get("delivery_id")
    if isinstance(direct, str) and direct:
        return direct
    job = value.get("job")
    if isinstance(job, dict):
        direct = job.get("delivery_id")
        if isinstance(direct, str) and direct:
            return direct
    return None


def _artifact_reference(job: Mapping[str, Any]) -> Optional[dict]:
    result = job.get("result")
    if not isinstance(result, dict):
        return None
    artifact = result.get("artifact")
    return artifact if isinstance(artifact, dict) else None


class ConvoyHardwareRunner:
    def __init__(self, config: Dict[str, Any], scenarios: Iterable[str],
                 allow_mutation: bool, allow_disruption: bool,
                 recorder: ResultRecorder):
        self.config = config
        self.scenarios = tuple(scenarios)
        self.allow_mutation = allow_mutation
        self.allow_disruption = allow_disruption
        self.recorder = recorder
        self.run_id = recorder.run_id
        self.controller_id = "hardware-e2e:%s" % self.run_id
        self.local: Optional[HostClient] = None
        self.local_snapshot: Optional[Dict[str, Any]] = None
        self.remote_snapshot: Optional[Dict[str, Any]] = None
        self.remote = SSHRemote(config)

    def case(self, name: str, category: str, callback) -> None:
        started = time.monotonic()
        try:
            details = callback()
        except SkipScenario as exc:
            self.recorder.add(name, category, "skipped",
                              time.monotonic() - started, str(exc))
        except HarnessError as exc:
            self.recorder.add(name, category, "failed",
                              time.monotonic() - started, str(exc))
        except Exception as exc:
            self.recorder.add(name, category, "error",
                              time.monotonic() - started,
                              "%s: %s" % (type(exc).__name__, exc))
        else:
            self.recorder.add(name, category, "passed",
                              time.monotonic() - started, details=details)

    def _require_preflight(self) -> Tuple[HostClient, dict, dict]:
        if self.local is None or self.local_snapshot is None:
            raise SkipScenario("local HostApp preflight did not pass")
        if self.remote_snapshot is None:
            raise SkipScenario("remote HostApp preflight did not pass")
        return self.local, self.local_snapshot, self.remote_snapshot

    def _scenario_config(self, name: str, required: bool = False) -> dict:
        value = self.config["scenarios"].get(name)
        if not isinstance(value, dict) or value.get("enabled") is not True:
            if required:
                raise SkipScenario("scenario is not enabled in config")
            return {}
        return value

    def _require_safety(self, name: str) -> None:
        if name in MUTATING_SCENARIOS and not self.allow_mutation:
            raise SkipScenario("requires explicit --allow-mutation")
        if name in DISRUPTIVE_SCENARIOS and not self.allow_disruption:
            raise SkipScenario("requires explicit --allow-disruption")

    def _remote_node(self) -> dict:
        _local, _ls, remote = self._require_preflight()
        node_id = self.config["remote"]["target_node_id"]
        node = _find_node(remote, node_id)
        if node is None:
            raise HarnessError("configured remote target node is absent")
        return node

    def _submit(self, operation: str, arguments: dict, idempotency_key: str,
                timeout_s: Optional[float] = None,
                expected_runtime: bool = True) -> Tuple[str, dict]:
        client, _local, _remote = self._require_preflight()
        node = self._remote_node()
        body = {
            "target_host_id": self.remote_snapshot["health"]["host_id"],
            "convoy_id": self.config["convoy_id"],
            "target_node_id": node["node_id"],
            "controller_id": self.controller_id,
            "operation": operation,
            "arguments": arguments,
            "idempotency_key": idempotency_key,
            "timeout_s": timeout_s or self.config["timeouts"]["job_s"],
        }
        runtime_id = node.get("runtime_id")
        if expected_runtime and isinstance(runtime_id, str) and runtime_id:
            body["expected_runtime_id"] = runtime_id
        status, value = client.json("POST", "/relay", body)
        if not 200 <= status < 300 or value.get("ok") is not True:
            raise HarnessError("relay submit refused: HTTP %d %s" %
                               (status, value.get("reason", "unknown")))
        delivery_id = _delivery_id(value)
        if not delivery_id:
            raise HarnessError("relay submit omitted delivery_id")
        return delivery_id, value

    def _poll(self, delivery_id: str, timeout_s: Optional[float] = None) -> dict:
        client, _local, remote = self._require_preflight()
        deadline = time.monotonic() + (timeout_s or self.config["timeouts"]["job_s"])
        last = None
        while time.monotonic() < deadline:
            status, value = client.json("POST", "/relay/job", {
                "target_host_id": remote["health"]["host_id"],
                "convoy_id": self.config["convoy_id"],
                "delivery_id": delivery_id,
            })
            if status == 200 and value.get("ok") is True:
                job = _job_from_response(value)
                if job is not None:
                    last = job
                    if job.get("state") in TERMINAL_STATES:
                        return job
            elif value.get("reason") not in (
                    "peer_unreachable", "peer_session_unavailable"):
                raise HarnessError("job poll refused: HTTP %d %s" %
                                   (status, value.get("reason", "unknown")))
            time.sleep(self.config["timeouts"]["poll_s"])
        raise HarnessError("job %s did not terminate; last state=%s" %
                           (delivery_id, (last or {}).get("state")))

    def _ack(self, delivery_id: str) -> dict:
        client, _local, remote = self._require_preflight()
        status, value = client.json("POST", "/relay/ack", {
            "target_host_id": remote["health"]["host_id"],
            "convoy_id": self.config["convoy_id"],
            "delivery_id": delivery_id,
        })
        if not 200 <= status < 300 or value.get("ok") is not True:
            raise HarnessError("job acknowledgement refused: HTTP %d %s" %
                               (status, value.get("reason", "unknown")))
        return value

    @staticmethod
    def _require_succeeded(job: Mapping[str, Any]) -> None:
        if job.get("state") != "succeeded":
            raise HarnessError("remote job ended in %s" % job.get("state"))

    def _delivery(self, operation: str, arguments: dict, key_suffix: str,
                  expected_runtime: bool = True) -> dict:
        delivery_id, _accepted = self._submit(
            operation, arguments, "%s:%s" % (self.run_id, key_suffix),
            expected_runtime=expected_runtime)
        job = self._poll(delivery_id)
        ack = self._ack(delivery_id)
        self._require_succeeded(job)
        return {"delivery_id": delivery_id, "state": job.get("state"),
                "operation": job.get("operation"),
                "acknowledged": ack.get("ok") is True,
                "result_present": job.get("result") is not None}

    # -- preflight -----------------------------------------------------

    def local_probe(self) -> dict:
        self.local, self.local_snapshot = discover_local_host(self.config)
        self.recorder.secret_values.append(self.local.handle.token)
        return {"host_id": self.local.handle.host_id,
                "protocol": self.local_snapshot["status"].get("protocol"),
                "nodes": len(_nodes(self.local_snapshot))}

    def remote_probe(self) -> dict:
        self.remote_snapshot = self.remote.probe()
        return {"host_id": self.remote_snapshot["health"].get("host_id"),
                "platform": self.remote_snapshot.get("platform"),
                "python": self.remote_snapshot.get("python"),
                "nodes": len(_nodes(self.remote_snapshot))}

    def verify_realm(self) -> dict:
        _client, local, remote = self._require_preflight()
        expected = self.config["convoy_id"]
        values = []
        for label, snapshot in (("local", local), ("remote", remote)):
            realm = snapshot.get("status", {}).get("realm")
            value = realm.get("convoy_id") if isinstance(realm, dict) else None
            if value != expected:
                raise HarnessError("%s realm is %r, expected %r" %
                                   (label, value, expected))
            values.append(value)
        return {"convoy_id": expected, "matched": len(set(values)) == 1}

    def verify_versions(self) -> dict:
        _client, local, remote = self._require_preflight()
        local_id = self.config["local"].get("node_id")
        if local_id:
            local_node = _find_node(local, local_id)
        else:
            local_node = next((node for node in _nodes(local)
                               if node.get("convoy_id") == self.config["convoy_id"]),
                              None)
        remote_node = self._remote_node()
        if local_node is None:
            raise HarnessError("no local node was available for version comparison")
        local_version = (local_node.get("metadata") or {}).get("embody_version")
        remote_version = (remote_node.get("metadata") or {}).get("embody_version")
        if not local_version or not remote_version:
            raise HarnessError("one or both nodes omit metadata.embody_version")
        if local_version != remote_version:
            raise HarnessError("Embody version mismatch: local=%s remote=%s" %
                               (local_version, remote_version))
        local_protocol = local.get("status", {}).get("protocol")
        remote_protocol = remote.get("status", {}).get("protocol")
        if (not isinstance(local_protocol, str) or not local_protocol
                or local_protocol != remote_protocol):
            raise HarnessError(
                "HostApp protocol mismatch: local=%r remote=%r" %
                (local_protocol, remote_protocol))
        expected = self.config.get("expected_embody_version")
        if expected and local_version != expected:
            raise HarnessError("nodes report %s, expected %s" %
                               (local_version, expected))
        return {"embody_version": local_version,
                "host_protocol": local_protocol,
                "local_node_id": local_node.get("node_id"),
                "remote_node_id": remote_node.get("node_id")}

    def verify_peer_sessions(self) -> dict:
        self._require_preflight()
        self._wait_for_sessions(self.config["timeouts"]["reconnect_s"])
        _client, local, remote = self._require_preflight()
        local_id = local["health"].get("host_id")
        remote_id = remote["health"].get("host_id")
        if local_id == remote_id:
            raise HarnessError("local and remote probes report the same host_id")
        for label, snapshot, peer_id in (
                ("local", local, remote_id), ("remote", remote, local_id)):
            stats = snapshot.get("lan", {}).get("peer_sessions")
            if not isinstance(stats, dict):
                raise HarnessError("%s HostApp has no peer session stats" % label)
            if int(stats.get("connected_peers") or 0) < 1:
                raise HarnessError("%s HostApp has no connected peer session" % label)
            peers = snapshot.get("peers", {}).get("peers", [])
            record = next((row for row in peers if isinstance(row, dict)
                           and row.get("host_id") == peer_id), None)
            if record is None or record.get("state") != "admitted":
                raise HarnessError("%s HostApp has not admitted its sibling" % label)
            if self.config["convoy_id"] not in (record.get("convoy_ids") or []):
                raise HarnessError("%s peer admission omits the Convoy" % label)
        return {"local_host_id": local_id, "remote_host_id": remote_id,
                "local_connected": local["lan"]["peer_sessions"]["connected_peers"],
                "remote_connected": remote["lan"]["peer_sessions"]["connected_peers"]}

    def verify_nodes(self) -> dict:
        _client, local, remote = self._require_preflight()
        target = self.config["remote"]["target_node_id"]
        if _find_node(remote, target) is None:
            raise HarnessError("remote target node is absent from remote /nodes")
        network = local.get("network_nodes", {})
        rows = network.get("nodes", [])
        remote_row = next((row for row in rows if isinstance(row, dict)
                           and row.get("node_id") == target), None)
        if remote_row is None:
            raise HarnessError("remote target is absent from local network view")
        statuses = network.get("peers", [])
        remote_host = remote["health"].get("host_id")
        peer = next((row for row in statuses if isinstance(row, dict)
                     and row.get("host_id") == remote_host), None)
        if peer is None:
            raise HarnessError("remote host is absent from local peer status")
        if peer.get("status") != "online":
            raise HarnessError("remote peer status is %s" % peer.get("status"))
        return {"target_node_id": target,
                "remote_status": remote_row.get("status", "online"),
                "network_nodes": len(rows)}

    # -- ordinary scenarios -------------------------------------------

    def ping(self) -> dict:
        return self._delivery("convoy_ping", {}, "ping", expected_runtime=False)

    def read(self) -> dict:
        scenario = self.config["scenarios"].get("read", {})
        if scenario and scenario.get("enabled") is False:
            raise SkipScenario("read scenario is disabled in config")
        operation = scenario.get("operation", "query_network")
        arguments = scenario.get("arguments", {
            "parent_path": "/", "recursive": False,
        })
        return self._delivery(operation, arguments, "read")

    def mutation(self) -> dict:
        self._require_safety("mutation")
        scenario = self._scenario_config("mutation", required=True)
        operation = scenario.get("operation")
        if not operation:
            raise SkipScenario("mutation.operation is not configured")
        arguments = scenario.get("arguments", {})
        key = "%s:mutation" % self.run_id
        first_id, first = self._submit(operation, arguments, key)
        second_id, second = self._submit(operation, arguments, key)
        if first_id != second_id:
            raise HarnessError("idempotent retry returned a different delivery")
        if second.get("created") is True:
            raise HarnessError("idempotent retry claimed it created new work")
        job = self._poll(first_id)
        cleanup_result = None
        try:
            self._require_succeeded(job)
            cleanup = scenario.get("cleanup")
            if isinstance(cleanup, dict) and cleanup.get("operation"):
                cleanup_result = self._delivery(
                    cleanup["operation"], cleanup.get("arguments", {}),
                    "mutation-cleanup")
        finally:
            ack = self._ack(first_id)
        return {"delivery_id": first_id, "same_delivery": True,
                "first_created": first.get("created"),
                "second_created": second.get("created"),
                "state": job.get("state"), "acknowledged": ack.get("ok"),
                "cleanup": cleanup_result}

    def artifact(self) -> dict:
        self._require_safety("artifact")
        scenario = self._scenario_config("artifact", required=True)
        operation = scenario.get("operation")
        project_root = scenario.get("export_project_root")
        if not operation or not isinstance(project_root, str) or not project_root:
            raise SkipScenario(
                "artifact operation/export_project_root is not configured")
        delivery_id, _accepted = self._submit(
            operation, scenario.get("arguments", {}),
            "%s:artifact" % self.run_id)
        job = self._poll(delivery_id)
        self._require_succeeded(job)
        reference = _artifact_reference(job)
        if reference is None:
            raise HarnessError("artifact scenario returned no artifact reference")
        client, _local, remote = self._require_preflight()
        materialized = client.require_ok("POST", "/relay/artifact", {
            "target_host_id": remote["health"]["host_id"],
            "convoy_id": self.config["convoy_id"],
            "target_node_id": self.config["remote"]["target_node_id"],
            "controller_id": self.controller_id,
            "artifact": reference,
            "timeout_s": self.config["timeouts"]["job_s"],
        })
        local_ref = materialized.get("artifact")
        if not isinstance(local_ref, dict):
            raise HarnessError("artifact materialization omitted local reference")
        artifact_id = local_ref.get("artifact_id")
        digest = local_ref.get("sha256")
        size = local_ref.get("size")
        if (not isinstance(artifact_id, str) or not ARTIFACT_ID.fullmatch(artifact_id)
                or not isinstance(digest, str) or not SHA256.fullmatch(digest)
                or artifact_id != "art_" + digest
                or isinstance(size, bool) or not isinstance(size, int)
                or size < 0 or size > MAX_ARTIFACT_BYTES):
            raise HarnessError("materialized artifact metadata is invalid")
        segment = base64.urlsafe_b64encode(
            self.config["convoy_id"].encode("utf-8")).decode("ascii").rstrip("=")
        headers, content = client.raw_get(
            "/artifacts/%s/%s" % (segment, artifact_id))
        if len(content) != size or hashlib.sha256(content).hexdigest() != digest:
            raise HarnessError("downloaded artifact failed size/hash verification")
        if headers.get("x-convoy-artifact-id") not in (None, artifact_id):
            raise HarnessError("artifact response header named different content")

        filename_template = scenario.get(
            "filename", "convoy-hardware-{run_id}-{artifact_id}.bin")
        try:
            filename = filename_template.format(
                run_id=self.run_id, artifact_id=artifact_id[4:16])
        except (KeyError, ValueError) as exc:
            raise HarnessError("artifact filename template is invalid") from exc
        if (not filename or filename in (".", "..") or "/" in filename
                or "\\" in filename or ":" in filename or len(filename) > 255):
            raise HarnessError("artifact filename must be one plain basename")
        exported = client.require_ok("POST", "/artifact/export", {
            "target_host_id": remote["health"]["host_id"],
            "target_node_id": self.config["remote"]["target_node_id"],
            "convoy_id": self.config["convoy_id"],
            "project_root": project_root,
            "artifact": local_ref,
            "filename": filename,
            "overwrite": False,
        })
        ack = self._ack(delivery_id)
        saved = exported.get("artifact") if isinstance(
            exported.get("artifact"), dict) else exported
        return {"delivery_id": delivery_id, "artifact_id": artifact_id,
                "bytes": size, "sha256_verified": True,
                "saved_path": saved.get("saved_path"),
                "acknowledged": ack.get("ok") is True}

    def cancel(self) -> dict:
        self._require_safety("cancel")
        scenario = self._scenario_config("cancel", required=True)
        operation = scenario.get("operation")
        if not operation:
            raise SkipScenario("cancel.operation is not configured")
        delivery_id, _accepted = self._submit(
            operation, scenario.get("arguments", {}),
            "%s:cancel" % self.run_id)
        client, _local, remote = self._require_preflight()
        status, cancelled = client.json("POST", "/relay/cancel", {
            "target_host_id": remote["health"]["host_id"],
            "convoy_id": self.config["convoy_id"],
            "delivery_id": delivery_id,
        })
        if not 200 <= status < 300 and cancelled.get("reason") != \
                "cancellation_indeterminate":
            raise HarnessError("cancel refused: HTTP %d %s" %
                               (status, cancelled.get("reason", "unknown")))
        job = self._poll(delivery_id)
        allowed = scenario.get("expected_terminal_states", ["cancelled", "canceled"])
        if job.get("state") not in allowed:
            raise HarnessError("cancel ended in %s, expected one of %s" %
                               (job.get("state"), allowed))
        ack = self._ack(delivery_id)
        return {"delivery_id": delivery_id, "state": job.get("state"),
                "cancel_reason": cancelled.get("reason"),
                "acknowledged": ack.get("ok") is True}

    # -- disruptive scenarios -----------------------------------------

    def _wait_for_sessions(self, timeout_s: Optional[float] = None) -> dict:
        deadline = time.monotonic() + (
            timeout_s or self.config["timeouts"]["reconnect_s"])
        last = None
        while time.monotonic() < deadline:
            try:
                local = self.local.snapshot(self.config["convoy_id"])
                remote = self.remote.probe()
                local_connected = int(local["lan"]["peer_sessions"].get(
                    "connected_peers") or 0)
                remote_connected = int(remote["lan"]["peer_sessions"].get(
                    "connected_peers") or 0)
                last = {"local": local_connected, "remote": remote_connected}
                if local_connected >= 1 and remote_connected >= 1:
                    self.local_snapshot, self.remote_snapshot = local, remote
                    return last
            except HarnessError as exc:
                last = {"error": str(exc)}
            time.sleep(self.config["timeouts"]["poll_s"])
        raise HarnessError("peer session did not reconnect: %s" % last)

    def dropout(self) -> dict:
        self._require_safety("dropout")
        scenario = self._scenario_config("dropout", required=True)
        command = scenario.get("self_reverting_command")
        if not command:
            raise SkipScenario("dropout.self_reverting_command is not configured")
        result = self.remote.command(
            command, float(scenario.get("command_timeout_s", 20)),
            tuple(scenario.get("accepted_exit_codes", [0, 255])))
        observed = False
        observe_deadline = time.monotonic() + float(
            scenario.get("observe_disconnect_s", 20))
        while time.monotonic() < observe_deadline:
            try:
                lan = self.local.require_ok("GET", "/lan/status")
                if int(lan.get("peer_sessions", {}).get(
                        "connected_peers") or 0) == 0:
                    observed = True
                    break
            except HarnessError:
                pass
            time.sleep(self.config["timeouts"]["poll_s"])
        if scenario.get("require_observed_disconnect", True) and not observed:
            raise HarnessError("dropout command ran but no session loss was observed")
        reconnected = self._wait_for_sessions()
        recovery_ping = self._delivery(
            "convoy_ping", {}, "dropout-recovery", expected_runtime=False)
        return {"disconnect_observed": observed, "reconnected": reconnected,
                "recovery_ping": recovery_ping,
                "command_returncode": result["returncode"]}

    def host_restart(self) -> dict:
        self._require_safety("host_restart")
        scenario = self._scenario_config("host_restart", required=True)
        command = scenario.get("command")
        if not command:
            raise SkipScenario("host_restart.command is not configured")
        before = self.remote_snapshot
        if (scenario.get("require_pid_change", True)
                and not isinstance(before.get("writer_pid"), int)):
            raise SkipScenario("remote HostApp portfile omits its writer PID")
        result = self.remote.command(
            command, float(scenario.get("command_timeout_s", 30)),
            tuple(scenario.get("accepted_exit_codes", [0, 255])))
        # Do not let a command which merely *scheduled* the restart look
        # complete because the old session was still healthy for one poll.
        change_deadline = time.monotonic() + self.config["timeouts"]["reconnect_s"]
        observed_restart = False
        while time.monotonic() < change_deadline:
            try:
                interim = self.remote.probe()
                if (before.get("writer_pid") is not None
                        and interim.get("writer_pid") != before.get("writer_pid")):
                    observed_restart = True
                    break
            except HarnessError:
                observed_restart = True
                break
            time.sleep(self.config["timeouts"]["poll_s"])
        if scenario.get("require_pid_change", True) and not observed_restart:
            raise HarnessError("HostApp restart was never observed")
        reconnected = self._wait_for_sessions()
        after = self.remote_snapshot
        if after["health"].get("host_id") != before["health"].get("host_id"):
            raise HarnessError("HostApp restart changed stable host identity")
        if scenario.get("require_pid_change", True):
            if (before.get("writer_pid") is not None
                    and after.get("writer_pid") == before.get("writer_pid")):
                raise HarnessError("HostApp writer PID did not change")
        recovery_ping = self._delivery(
            "convoy_ping", {}, "host-restart-recovery", expected_runtime=False)
        return {"host_id": after["health"].get("host_id"),
                "old_pid": before.get("writer_pid"),
                "new_pid": after.get("writer_pid"),
                "reconnected": reconnected,
                "recovery_ping": recovery_ping,
                "command_returncode": result["returncode"]}

    def td_restart(self) -> dict:
        self._require_safety("td_restart")
        scenario = self._scenario_config("td_restart", required=True)
        before = self._remote_node()
        old_runtime = before.get("runtime_id")
        if not isinstance(old_runtime, str) or not old_runtime:
            raise SkipScenario("remote target has no runtime_id")
        delivery_id, _accepted = self._submit(
            "convoy_restart_node",
            {"policy": scenario.get("policy", "require_clean"),
             "timeout_s": float(scenario.get("timeout_s", 90))},
            "%s:td-restart" % self.run_id,
            timeout_s=float(scenario.get("timeout_s", 90)) + 30)
        job = self._poll(delivery_id, float(scenario.get("timeout_s", 90)) + 30)
        self._require_succeeded(job)
        deadline = time.monotonic() + self.config["timeouts"]["reconnect_s"]
        new_runtime = None
        while time.monotonic() < deadline:
            try:
                snapshot = self.remote.probe()
                node = _find_node(snapshot, before["node_id"])
                new_runtime = node.get("runtime_id") if node else None
                if isinstance(new_runtime, str) and new_runtime != old_runtime:
                    self.remote_snapshot = snapshot
                    break
            except HarnessError:
                pass
            time.sleep(self.config["timeouts"]["poll_s"])
        if not new_runtime or new_runtime == old_runtime:
            raise HarnessError("TouchDesigner runtime did not change after restart")
        ack = self._ack(delivery_id)
        sessions = self._wait_for_sessions()
        recovery_read = self._delivery(
            "query_network", {"parent_path": "/", "recursive": False},
            "td-restart-read")
        return {"delivery_id": delivery_id, "node_id": before["node_id"],
                "runtime_changed": True, "acknowledged": ack.get("ok"),
                "sessions": sessions, "recovery_read": recovery_read}

    def run(self) -> None:
        self.case("local_host", "preflight", self.local_probe)
        self.case("remote_host_over_ssh", "preflight", self.remote_probe)
        self.case("matching_realm", "preflight", self.verify_realm)
        self.case("matching_versions", "preflight", self.verify_versions)
        self.case("peer_sessions", "preflight", self.verify_peer_sessions)
        self.case("node_directory", "preflight", self.verify_nodes)
        callbacks = {
            "ping": self.ping, "read": self.read,
            "mutation": self.mutation, "artifact": self.artifact,
            "cancel": self.cancel, "dropout": self.dropout,
            "host_restart": self.host_restart, "td_restart": self.td_restart,
        }
        for name in self.scenarios:
            self.case(name, "scenario", callbacks[name])


def select_scenarios(values: Optional[Sequence[str]]) -> Tuple[str, ...]:
    requested = list(values or ["smoke"])
    selected: List[str] = []
    for value in requested:
        if value == "smoke":
            candidates = ("ping", "read")
        elif value == "all":
            candidates = ALL_SCENARIOS
        elif value in ALL_SCENARIOS:
            candidates = (value,)
        else:
            raise ConfigError("unknown scenario %r" % value)
        for candidate in candidates:
            if candidate not in selected:
                selected.append(candidate)
    return tuple(selected)


def _default_result_paths(run_id: str) -> Tuple[str, str]:
    root = pathlib.Path(__file__).resolve().parent / "results"
    return (str(root / ("convoy-hardware-%s.json" % run_id)),
            str(root / ("convoy-hardware-%s.junit.xml" % run_id)))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True,
                        help="Path to hardware harness JSON config")
    parser.add_argument("--scenario", action="append",
                        choices=("smoke", "all") + ALL_SCENARIOS,
                        help="Scenario to run; repeatable (default: smoke)")
    parser.add_argument("--allow-mutation", action="store_true",
                        help="Permit configured write/export/cancel scenarios")
    parser.add_argument("--allow-disruption", action="store_true",
                        help="Permit configured dropout/restart scenarios")
    parser.add_argument("--json-out", help="JSON result path")
    parser.add_argument("--junit-out", help="JUnit XML result path")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    run_id = (_datetime.datetime.now(_datetime.timezone.utc)
              .strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(3))
    default_json, default_junit = _default_result_paths(run_id)
    json_out = args.json_out or default_json
    junit_out = args.junit_out or default_junit
    recorder = ResultRecorder(run_id)
    metadata: Dict[str, Any] = {
        "config": str(pathlib.Path(args.config).expanduser()),
        "allow_mutation": bool(args.allow_mutation),
        "allow_disruption": bool(args.allow_disruption),
    }
    try:
        config = load_config(args.config)
        scenarios = select_scenarios(args.scenario)
        metadata["convoy_id"] = config["convoy_id"]
        metadata["scenarios"] = list(scenarios)
        runner = ConvoyHardwareRunner(
            config, scenarios, args.allow_mutation,
            args.allow_disruption, recorder)
        runner.run()
    except ConfigError as exc:
        recorder.add("configuration", "harness", "error", 0.0, str(exc))
    finally:
        write_results(recorder, json_out, junit_out, metadata)

    for case in recorder.cases:
        suffix = (": " + case.message) if case.message else ""
        print("%-7s %-28s%s" % (case.status.upper(), case.name, suffix))
    summary = recorder.summary()
    print("JSON:  %s" % pathlib.Path(json_out).resolve())
    print("JUnit: %s" % pathlib.Path(junit_out).resolve())
    print("Summary: %s" % json.dumps(summary, sort_keys=True))
    return 1 if summary["failed"] or summary["error"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
