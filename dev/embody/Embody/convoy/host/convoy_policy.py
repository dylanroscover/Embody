"""Host-private safety policy for Convoy.

This module owns the settings which grant dangerous machine-local authority.
They deliberately do not live in a TouchDesigner project, node registration,
or Convoy membership document: copying a project must never copy an approval.

Enabling either dangerous gate is a two-step operation.  A caller first asks
for a short-lived, in-memory challenge and then confirms the exact phrase.
There is intentionally no direct ``enable`` method.  Disabling is immediate,
idempotent, and cannot be blocked by a stale UI generation.

The file format is strict and fail-closed.  An unreadable, corrupt, or newer
``policy.json`` raises instead of silently replacing it with permissive
defaults.  Writes use Convoy's atomic owner-private writer.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import threading
import time

import convoy_platform


POLICY_FILE = "policy.json"
POLICY_VERSION = 1
DEFAULT_ARTIFACT_QUOTA_MB = 1024
# 1 TiB / 1 MiB.  The UI and ArtifactStore both express this setting in MiB.
MAX_ARTIFACT_QUOTA_MB = 1024 * 1024
CHALLENGE_TTL_S = 60.0
MAX_PENDING_CHALLENGES = 256
MAX_POLICY_BYTES = 4 * 1024 * 1024

FULL_SHELL = "full_shell"
TD_PYTHON = "td_python"
_DANGER_SETTINGS = frozenset((FULL_SHELL, TD_PYTHON))
_NODE_ID = re.compile(r"^[0-9a-f]{32}$")
_STATE_KEYS = frozenset((
    "version", "generation", "allow_full_shell", "artifact_quota_mb",
    "td_python_node_ids",
))


class PolicyError(Exception):
    """Base error with a stable HTTP/API-facing refusal reason."""

    reason = "policy_error"

    def __init__(self, detail=""):
        super().__init__(f"{self.reason}: {detail}" if detail else self.reason)
        self.detail = detail


class PolicyUnreadable(PolicyError):
    reason = "policy_unreadable"


class PolicyCorrupt(PolicyError):
    reason = "policy_corrupt"


class PolicyTooNew(PolicyError):
    reason = "policy_too_new"

    def __init__(self, found, supported=POLICY_VERSION):
        super().__init__(
            f"policy version {found!r} is newer than supported version "
            f"{supported}")
        self.found = found
        self.supported = supported


class PolicyConflict(PolicyError):
    reason = "policy_generation_conflict"

    def __init__(self, expected, current):
        super().__init__(f"expected generation {expected!r}; current is {current}")
        self.expected = expected
        self.current = current


class PolicyValidationError(PolicyError):
    reason = "policy_invalid"


class PolicyAlreadyEnabled(PolicyError):
    reason = "policy_already_enabled"


class ChallengeNotFound(PolicyError):
    reason = "policy_challenge_not_found"


class ChallengeInvalid(PolicyError):
    reason = "policy_challenge_invalid"


class ChallengeExpired(PolicyError):
    reason = "policy_challenge_expired"


class ChallengeStale(PolicyError):
    reason = "policy_challenge_stale"

    def __init__(self, challenge_generation, current_generation):
        super().__init__(
            f"challenge generation {challenge_generation}; current is "
            f"{current_generation}")
        self.challenge_generation = challenge_generation
        self.current_generation = current_generation


def _nonnegative_generation(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PolicyValidationError("generation must be a non-negative integer")
    return value


def _node_id(value):
    if not isinstance(value, str) or not _NODE_ID.fullmatch(value):
        raise PolicyValidationError("node_id must be 32 lowercase hex characters")
    return value


def _quota_mb(value):
    if (isinstance(value, bool) or not isinstance(value, int)
            or not 0 <= value <= MAX_ARTIFACT_QUOTA_MB):
        raise PolicyValidationError(
            "artifact_quota_mb must be an integer from 0 through 1048576")
    return value


def _default_state():
    return {
        "version": POLICY_VERSION,
        "generation": 0,
        "allow_full_shell": False,
        "artifact_quota_mb": DEFAULT_ARTIFACT_QUOTA_MB,
        "td_python_node_ids": [],
    }


def _validated_state(value):
    """Return a detached canonical state or raise a fail-closed error."""
    if not isinstance(value, dict):
        raise PolicyCorrupt("top level must be an object")
    version = value.get("version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise PolicyCorrupt("version must be an integer")
    if version > POLICY_VERSION:
        raise PolicyTooNew(version)
    if version != POLICY_VERSION:
        raise PolicyCorrupt(f"unsupported policy version {version!r}")
    if set(value) != _STATE_KEYS:
        missing = sorted(_STATE_KEYS - set(value))
        extra = sorted(set(value) - _STATE_KEYS)
        raise PolicyCorrupt(f"schema mismatch; missing={missing!r}, extra={extra!r}")
    try:
        generation = _nonnegative_generation(value["generation"])
        quota = _quota_mb(value["artifact_quota_mb"])
    except PolicyValidationError as exc:
        raise PolicyCorrupt(exc.detail) from exc
    allow_shell = value["allow_full_shell"]
    if type(allow_shell) is not bool:
        raise PolicyCorrupt("allow_full_shell must be a boolean")
    node_ids = value["td_python_node_ids"]
    if not isinstance(node_ids, list):
        raise PolicyCorrupt("td_python_node_ids must be an array")
    try:
        clean_nodes = [_node_id(item) for item in node_ids]
    except PolicyValidationError as exc:
        raise PolicyCorrupt(exc.detail) from exc
    if len(clean_nodes) != len(set(clean_nodes)):
        raise PolicyCorrupt("td_python_node_ids contains duplicates")
    return {
        "version": POLICY_VERSION,
        "generation": generation,
        "allow_full_shell": allow_shell,
        "artifact_quota_mb": quota,
        "td_python_node_ids": sorted(clean_nodes),
    }


class PolicyStore:
    """Thread-safe policy persistence and danger-gate confirmation flow."""

    def __init__(self, directory, *, now=None, monotonic=None,
                 challenge_ttl_s=CHALLENGE_TTL_S):
        if not isinstance(directory, (str, os.PathLike)):
            raise PolicyValidationError("directory must be a filesystem path")
        if (isinstance(challenge_ttl_s, bool)
                or not isinstance(challenge_ttl_s, (int, float))
                or not 0 < float(challenge_ttl_s) <= 300):
            raise PolicyValidationError(
                "challenge_ttl_s must be greater than 0 and at most 300")
        self.directory = os.fspath(directory)
        self.path = os.path.join(self.directory, POLICY_FILE)
        self._now = now or time.time
        self._monotonic = monotonic or time.monotonic
        self._challenge_ttl_s = float(challenge_ttl_s)
        self._lock = threading.RLock()
        self._challenges = {}
        self._state = self._load_or_create()

    # -- persisted state -------------------------------------------------

    def _load_or_create(self):
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                raw = handle.read(MAX_POLICY_BYTES + 1)
        except FileNotFoundError:
            state = _default_state()
            self._write_state(state)
            return state
        except (OSError, UnicodeError) as exc:
            raise PolicyUnreadable(f"cannot read {self.path}: {exc}") from exc
        if len(raw.encode("utf-8")) > MAX_POLICY_BYTES:
            raise PolicyCorrupt("policy file exceeds size limit")
        try:
            value = json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise PolicyCorrupt("policy file is not valid JSON") from exc
        return _validated_state(value)

    def _write_state(self, state):
        payload = json.dumps(state, indent=1, sort_keys=True) + "\n"
        try:
            convoy_platform._write_private(self.path, payload)
            # POSIX enforces this exactly.  Windows' stdlib chmod controls
            # only the read-only bit, while the per-user LocalAppData ACL is
            # the actual boundary; the call is still useful best effort for
            # portable filesystems mounted by Windows.
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
        except OSError as exc:
            raise PolicyUnreadable(f"cannot write {self.path}: {exc}") from exc

    def _snapshot_locked(self):
        return {
            "version": self._state["version"],
            "generation": self._state["generation"],
            "allow_full_shell": self._state["allow_full_shell"],
            "artifact_quota_mb": self._state["artifact_quota_mb"],
            "td_python_node_ids": list(self._state["td_python_node_ids"]),
        }

    def snapshot(self):
        with self._lock:
            return self._snapshot_locked()

    @property
    def generation(self):
        with self._lock:
            return self._state["generation"]

    def allow_full_shell(self):
        with self._lock:
            return self._state["allow_full_shell"] is True

    def allow_td_python(self, node_id):
        node_id = _node_id(node_id)
        with self._lock:
            return node_id in self._state["td_python_node_ids"]

    def artifact_quota_mb(self):
        with self._lock:
            return self._state["artifact_quota_mb"]

    def _check_generation_locked(self, expected_generation):
        expected_generation = _nonnegative_generation(expected_generation)
        current = self._state["generation"]
        if expected_generation != current:
            raise PolicyConflict(expected_generation, current)
        return current

    def _commit_locked(self, candidate):
        candidate = _validated_state(candidate)
        candidate["generation"] = self._state["generation"] + 1
        self._write_state(candidate)
        self._state = candidate
        return self._snapshot_locked()

    def set_artifact_quota_mb(self, value, *, expected_generation):
        """Set the local artifact quota with compare-and-swap semantics."""
        value = _quota_mb(value)
        with self._lock:
            self._check_generation_locked(expected_generation)
            if value == self._state["artifact_quota_mb"]:
                return self._snapshot_locked()
            candidate = self._snapshot_locked()
            candidate["artifact_quota_mb"] = value
            return self._commit_locked(candidate)

    # -- dangerous setting challenges -----------------------------------

    def _purge_expired_locked(self, now_mono):
        expired = [key for key, value in self._challenges.items()
                   if now_mono >= value["expires_mono"]]
        for key in expired:
            self._challenges.pop(key, None)

    def _begin_enable(self, setting, *, node_id, expected_generation):
        if setting not in _DANGER_SETTINGS:
            raise PolicyValidationError("unknown dangerous setting")
        if setting == TD_PYTHON:
            node_id = _node_id(node_id)
        elif node_id is not None:
            raise PolicyValidationError("full-shell challenge must not name a node")
        with self._lock:
            generation = self._check_generation_locked(expected_generation)
            if (setting == FULL_SHELL and self._state["allow_full_shell"]
                    or setting == TD_PYTHON
                    and node_id in self._state["td_python_node_ids"]):
                raise PolicyAlreadyEnabled(setting)
            now_mono = float(self._monotonic())
            self._purge_expired_locked(now_mono)
            if len(self._challenges) >= MAX_PENDING_CHALLENGES:
                raise PolicyError("too many pending policy challenges")
            challenge_id = secrets.token_urlsafe(24)
            code = secrets.token_hex(4).upper()
            if setting == FULL_SHELL:
                phrase = f"ENABLE FULL SHELL {code}"
            else:
                phrase = f"ENABLE TD PYTHON {node_id} {code}"
            self._challenges[challenge_id] = {
                "setting": setting,
                "node_id": node_id,
                "generation": generation,
                "confirmation_digest": hashlib.sha256(
                    phrase.encode("utf-8")).hexdigest(),
                "expires_mono": now_mono + self._challenge_ttl_s,
            }
            return {
                "challenge_id": challenge_id,
                "setting": setting,
                "node_id": node_id,
                "generation": generation,
                "confirmation": phrase,
                "expires_at_unix": float(self._now()) + self._challenge_ttl_s,
                "expires_in_seconds": self._challenge_ttl_s,
            }

    def begin_enable_full_shell(self, *, expected_generation):
        return self._begin_enable(
            FULL_SHELL, node_id=None, expected_generation=expected_generation)

    def begin_enable_td_python(self, node_id, *, expected_generation):
        return self._begin_enable(
            TD_PYTHON, node_id=node_id,
            expected_generation=expected_generation)

    def confirm_enable(self, challenge_id, confirmation, *,
                       expected_generation):
        """Consume one challenge and enable exactly the authority it names."""
        if not isinstance(challenge_id, str) or not challenge_id:
            raise ChallengeNotFound()
        with self._lock:
            # Pop first: success, typo, expiry, and stale generation are all
            # one-time outcomes.  A retry must start a fresh human-visible
            # challenge rather than probing a reusable bearer secret.
            challenge = self._challenges.pop(challenge_id, None)
            if challenge is None:
                raise ChallengeNotFound()
            if float(self._monotonic()) >= challenge["expires_mono"]:
                raise ChallengeExpired()
            expected_generation = _nonnegative_generation(expected_generation)
            if expected_generation != challenge["generation"]:
                raise PolicyConflict(
                    expected_generation, challenge["generation"])
            current = self._state["generation"]
            if current != challenge["generation"]:
                raise ChallengeStale(challenge["generation"], current)
            if not isinstance(confirmation, str):
                raise ChallengeInvalid()
            supplied = hashlib.sha256(confirmation.encode("utf-8")).hexdigest()
            if not secrets.compare_digest(
                    supplied, challenge["confirmation_digest"]):
                raise ChallengeInvalid()

            candidate = self._snapshot_locked()
            if challenge["setting"] == FULL_SHELL:
                candidate["allow_full_shell"] = True
            elif challenge["setting"] == TD_PYTHON:
                candidate["td_python_node_ids"].append(challenge["node_id"])
            else:  # Defensive against in-memory corruption/monkeypatching.
                raise ChallengeInvalid()
            return self._commit_locked(candidate)

    def decline_challenge(self, challenge_id):
        """Consume a challenge without changing policy."""
        if not isinstance(challenge_id, str) or not challenge_id:
            raise ChallengeNotFound()
        with self._lock:
            challenge = self._challenges.pop(challenge_id, None)
            if challenge is None:
                raise ChallengeNotFound()
            if float(self._monotonic()) >= challenge["expires_mono"]:
                raise ChallengeExpired()
            return self._snapshot_locked()

    # -- safety-first disables ------------------------------------------

    def disable_full_shell(self):
        """Disable immediately; deliberately has no CAS precondition."""
        with self._lock:
            if not self._state["allow_full_shell"]:
                return self._snapshot_locked()
            candidate = self._snapshot_locked()
            candidate["allow_full_shell"] = False
            return self._commit_locked(candidate)

    def disable_td_python(self, node_id):
        """Disable one stable node immediately and idempotently."""
        node_id = _node_id(node_id)
        with self._lock:
            if node_id not in self._state["td_python_node_ids"]:
                return self._snapshot_locked()
            candidate = self._snapshot_locked()
            candidate["td_python_node_ids"].remove(node_id)
            return self._commit_locked(candidate)

