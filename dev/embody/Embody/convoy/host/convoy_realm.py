"""Host-private Convoy realm genesis and split-realm state.

This module is deliberately small and side-effect free except for one private
JSON record.  Discovery supplies *observations*; this state machine decides
whether the host is still an uncommitted candidate, has an established realm,
or must fail closed because more than one established realm is present.

There is no leader and no elected owner.  During genesis, every host remembers
the lexically lowest candidate it has observed and commits that value after a
bounded settle window.  An established announcement always beats an
uncommitted candidate.  Once established, however, a host never rewrites its
realm merely because a later (even lower) candidate appears.

The record is host-private.  It is not a discovery credential and it grants no
command authority; authenticated peer admission remains a separate boundary.
"""

from __future__ import annotations

import json
import math
import os
import threading
import time

import convoy_identity as identity
import convoy_platform


REALM_FILE = "realm.json"
REALM_VERSION = 1
DEFAULT_SETTLE_DELAY_S = 8.0

# ADR-003 requires the genesis listen window to be RANDOMIZED. The
# fixed 8.0 s that shipped instead loses exactly the SIMULTANEOUS case:
# two machines enabling within the same window each hear nothing before
# their identical deadline and each crowns its own realm. (Machines
# enabled in genuine isolation -- different days, different networks --
# still self-establish; no jitter can fix that, which is why the
# stranger-observation gate and the register check-then-apply carry the
# rest of the split-realm defence.) The production caller (HostApp)
# draws settle_delay_s uniformly from [DEFAULT_SETTLE_DELAY_S,
# DEFAULT_SETTLE_DELAY_S + DEFAULT_SETTLE_JITTER_S]; RealmStore itself
# stays deterministic so the suites keep their injected clocks.
DEFAULT_SETTLE_JITTER_S = 12.0
MAX_SETTLE_DELAY_S = 300.0
MAX_REALM_BYTES = 64 * 1024
MAX_OBSERVED_REALMS = 256
# CONFLICT accumulates every distinct established realm it has seen. Without a
# cap a stream of crafted announcements grows realm.json past MAX_REALM_BYTES,
# after which the loader refuses its own file and the daemon cannot start
# (review 2026-08-02). 32 distinct realms is far beyond any real split; the
# state is operator-visible and cleared by reset(), not by accumulating ids.
MAX_CONFLICT_IDS = 32


def _cap_conflict_ids(ids, preserved=None):
    """Bound a conflict-id set to MAX_CONFLICT_IDS, keeping ``preserved``."""
    ordered = sorted(set(ids))
    if len(ordered) <= MAX_CONFLICT_IDS:
        return ordered
    kept = ordered[:MAX_CONFLICT_IDS]
    if preserved is not None and preserved not in kept:
        kept = sorted(set(kept[:MAX_CONFLICT_IDS - 1]) | {preserved})
    return kept

CANDIDATE = "candidate"
ESTABLISHED = "established"
CONFLICT = "conflict"
REALM_STATES = frozenset((CANDIDATE, ESTABLISHED, CONFLICT))

_STATE_KEYS = frozenset((
    "version",
    "generation",
    "state",
    "convoy_id",
    "candidate_started_unix",
    "settle_deadline_unix",
    "conflict_ids",
))


class RealmError(Exception):
    """Base error with a stable API-facing refusal reason."""

    reason = "realm_error"

    def __init__(self, detail=""):
        super().__init__(f"{self.reason}: {detail}" if detail else self.reason)
        self.detail = detail


class RealmUnreadable(RealmError):
    reason = "realm_unreadable"


class RealmCorrupt(RealmError):
    reason = "realm_corrupt"


class RealmTooNew(RealmError):
    reason = "realm_too_new"

    def __init__(self, found, supported=REALM_VERSION):
        super().__init__(
            f"realm version {found!r} is newer than supported version "
            f"{supported}")
        self.found = found
        self.supported = supported


class RealmValidationError(RealmError):
    reason = "realm_invalid"


class RealmStateError(RealmError):
    reason = "realm_state_conflict"


def _finite_timestamp(value, field, error_type=RealmValidationError):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(float(value)) or float(value) < 0):
        raise error_type(f"{field} must be a finite non-negative number")
    return float(value)


def _generation(value, error_type=RealmValidationError):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise error_type("generation must be a non-negative integer")
    return value


def _convoy_id(value, *, allow_none=False,
               error_type=RealmValidationError):
    if value is None and allow_none:
        return None
    try:
        return identity.normalize_convoy_id(value)
    except identity.IdentityError as exc:
        raise error_type("convoy_id must be canonical bounded text") from exc


def _observed_ids(values, field):
    if values is None:
        return set()
    if isinstance(values, (str, bytes, bytearray, dict)):
        raise RealmValidationError(f"{field} must be an iterable of IDs")
    try:
        iterator = iter(values)
    except TypeError as exc:
        raise RealmValidationError(
            f"{field} must be an iterable of IDs") from exc
    clean = set()
    for index, item in enumerate(iterator):
        if index >= MAX_OBSERVED_REALMS:
            raise RealmValidationError(
                f"{field} may contain at most {MAX_OBSERVED_REALMS} IDs")
        clean.add(_convoy_id(item))
    return clean


def _validated_state(value):
    """Return a detached canonical persisted state or fail closed."""
    if not isinstance(value, dict):
        raise RealmCorrupt("top level must be an object")
    version = value.get("version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise RealmCorrupt("version must be an integer")
    if version > REALM_VERSION:
        raise RealmTooNew(version)
    if version != REALM_VERSION:
        raise RealmCorrupt(f"unsupported realm version {version!r}")
    if set(value) != _STATE_KEYS:
        missing = sorted(_STATE_KEYS - set(value))
        extra = sorted(set(value) - _STATE_KEYS)
        raise RealmCorrupt(
            f"schema mismatch; missing={missing!r}, extra={extra!r}")

    try:
        generation = _generation(value["generation"], RealmCorrupt)
        state = value["state"]
        if not isinstance(state, str) or state not in REALM_STATES:
            raise RealmCorrupt(f"unknown realm state {state!r}")
        convoy_id = _convoy_id(
            value["convoy_id"], allow_none=state == CONFLICT,
            error_type=RealmCorrupt)
    except RealmValidationError as exc:
        raise RealmCorrupt(exc.detail) from exc

    conflict_ids = value["conflict_ids"]
    if not isinstance(conflict_ids, list):
        raise RealmCorrupt("conflict_ids must be an array")
    try:
        clean_conflicts = [_convoy_id(item, error_type=RealmCorrupt)
                           for item in conflict_ids]
    except RealmValidationError as exc:
        raise RealmCorrupt(exc.detail) from exc
    if clean_conflicts != sorted(set(clean_conflicts)):
        raise RealmCorrupt("conflict_ids must be unique and sorted")
    # Truncate an over-long list (e.g. a record written by a pre-cap daemon)
    # so the daemon still starts, keeping the preserved convoy_id if present.
    if len(clean_conflicts) > MAX_CONFLICT_IDS:
        clean_conflicts = _cap_conflict_ids(clean_conflicts, convoy_id)

    started = value["candidate_started_unix"]
    deadline = value["settle_deadline_unix"]
    if state == CANDIDATE:
        try:
            started = _finite_timestamp(
                started, "candidate_started_unix", RealmCorrupt)
            deadline = _finite_timestamp(
                deadline, "settle_deadline_unix", RealmCorrupt)
        except RealmValidationError as exc:
            raise RealmCorrupt(exc.detail) from exc
        if deadline < started:
            raise RealmCorrupt(
                "settle_deadline_unix precedes candidate_started_unix")
        if clean_conflicts:
            raise RealmCorrupt("candidate state cannot contain conflict_ids")
    elif state == ESTABLISHED:
        if started is not None or deadline is not None:
            raise RealmCorrupt(
                "established state cannot contain candidate timestamps")
        if clean_conflicts:
            raise RealmCorrupt("established state cannot contain conflict_ids")
    else:
        if started is not None or deadline is not None:
            raise RealmCorrupt(
                "conflict state cannot contain candidate timestamps")
        if len(clean_conflicts) < 2:
            raise RealmCorrupt(
                "conflict state must name at least two established realms")
        if convoy_id is not None and convoy_id not in clean_conflicts:
            raise RealmCorrupt(
                "preserved convoy_id must be present in conflict_ids")

    return {
        "version": REALM_VERSION,
        "generation": generation,
        "state": state,
        "convoy_id": convoy_id,
        "candidate_started_unix": started,
        "settle_deadline_unix": deadline,
        "conflict_ids": list(clean_conflicts),
    }


def _state_payload(state, convoy_id, *, started=None, deadline=None,
                   conflict_ids=()):
    return {
        "version": REALM_VERSION,
        "generation": 0,
        "state": state,
        "convoy_id": convoy_id,
        "candidate_started_unix": started,
        "settle_deadline_unix": deadline,
        "conflict_ids": sorted(set(conflict_ids)),
    }


class RealmStore:
    """Thread-safe, persisted host realm convergence state.

    ``path`` is the full private record path, which keeps tests and host-app
    integration explicit.  A missing record represents an unbound host and
    ``snapshot()`` returns ``None``.  The caller can listen first, adopt a
    discovered established realm with ``reconcile()``, or begin leaderless
    genesis with ``begin_candidate()`` after its listen window.
    """

    def __init__(self, path, *, now=None,
                 settle_delay_s=DEFAULT_SETTLE_DELAY_S):
        if not isinstance(path, (str, os.PathLike)):
            raise RealmValidationError("path must be a filesystem path")
        path = os.fspath(path)
        if not path:
            raise RealmValidationError("path must not be empty")
        if (isinstance(settle_delay_s, bool)
                or not isinstance(settle_delay_s, (int, float))
                or not math.isfinite(float(settle_delay_s))
                or not 0 < float(settle_delay_s) <= MAX_SETTLE_DELAY_S):
            raise RealmValidationError(
                "settle_delay_s must be greater than 0 and at most 300")
        self.path = path
        self._now = now or time.time
        self._settle_delay_s = float(settle_delay_s)
        self._lock = threading.RLock()
        self._state = self._load()

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                raw = handle.read(MAX_REALM_BYTES + 1)
        except FileNotFoundError:
            return None
        except (OSError, UnicodeError) as exc:
            raise RealmUnreadable(f"cannot read {self.path}: {exc}") from exc
        try:
            byte_length = len(raw.encode("utf-8"))
        except UnicodeError as exc:
            raise RealmCorrupt("realm file is not valid UTF-8") from exc
        if byte_length > MAX_REALM_BYTES:
            raise RealmCorrupt("realm file exceeds size limit")
        try:
            value = json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise RealmCorrupt("realm file is not valid JSON") from exc
        return _validated_state(value)

    def _write_state(self, state):
        payload = json.dumps(state, indent=1, sort_keys=True) + "\n"
        # A writer must never produce a record its own loader rejects. With the
        # conflict-id cap this cannot trigger in practice; it is a backstop so
        # any future unbounded field fails loud at the write, not silently at
        # the next start.
        if len(payload.encode("utf-8")) > MAX_REALM_BYTES:
            raise RealmValidationError(
                "refusing to write a realm record larger than the loader "
                "accepts")
        try:
            convoy_platform._write_private(self.path, payload)
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
        except OSError as exc:
            raise RealmUnreadable(f"cannot write {self.path}: {exc}") from exc

    def _snapshot_locked(self):
        if self._state is None:
            return None
        return {
            "version": self._state["version"],
            "generation": self._state["generation"],
            "state": self._state["state"],
            "convoy_id": self._state["convoy_id"],
            "candidate_started_unix":
                self._state["candidate_started_unix"],
            "settle_deadline_unix": self._state["settle_deadline_unix"],
            "conflict_ids": list(self._state["conflict_ids"]),
        }

    def snapshot(self):
        with self._lock:
            return self._snapshot_locked()

    @property
    def state(self):
        with self._lock:
            return None if self._state is None else self._state["state"]

    @property
    def generation(self):
        with self._lock:
            return None if self._state is None else self._state["generation"]

    def _commit_locked(self, candidate):
        candidate = dict(candidate)
        candidate["generation"] = (
            0 if self._state is None else self._state["generation"] + 1)
        candidate = _validated_state(candidate)

        if self._state is not None:
            unchanged = dict(candidate)
            unchanged["generation"] = self._state["generation"]
            if unchanged == self._state:
                return self._snapshot_locked()

        # Write before publishing to memory: a failed atomic write cannot make
        # the running process believe a transition survived when it did not.
        self._write_state(candidate)
        self._state = candidate
        return self._snapshot_locked()

    def begin_candidate(self, convoy_id):
        """Persist one uncommitted local candidate after passive listening.

        Candidate selection is monotonic during the window: if the host has
        already remembered a lower observed candidate, retrying this method
        with a higher ID cannot raise it again.
        """
        convoy_id = _convoy_id(convoy_id)
        with self._lock:
            if self._state is None:
                started = _finite_timestamp(self._now(), "now")
                deadline = _finite_timestamp(
                    started + self._settle_delay_s,
                    "settle_deadline_unix")
                return self._commit_locked(_state_payload(
                    CANDIDATE,
                    convoy_id,
                    started=started,
                    deadline=deadline,
                ))
            if self._state["state"] == CANDIDATE:
                if convoy_id >= self._state["convoy_id"]:
                    return self._snapshot_locked()
                candidate = self._snapshot_locked()
                candidate["convoy_id"] = convoy_id
                return self._commit_locked(candidate)
            raise RealmStateError(
                f"cannot begin genesis while state is "
                f"{self._state['state']!r}")

    def reconcile(self, *, candidate_ids=(), established_ids=()):
        """Apply one bounded set of signed discovery/local observations.

        Inputs are realm identifiers already classified by discovery.  An
        integration importing an old announcement with no explicit state must
        classify it as ``established`` before calling this method.
        """
        candidates = _observed_ids(candidate_ids, "candidate_ids")
        established = _observed_ids(established_ids, "established_ids")
        now = _finite_timestamp(self._now(), "now")

        with self._lock:
            if self._state is None:
                if len(established) > 1:
                    return self._enter_conflict_locked(established)
                if len(established) == 1:
                    return self._commit_locked(_state_payload(
                        ESTABLISHED, next(iter(established))))
                return None

            state = self._state["state"]
            if state == CONFLICT:
                # Conflict is an operator-visible recovery state, not a
                # transient presence calculation.  It never silently clears,
                # and its id set is BOUNDED so a stream of announcements cannot
                # grow realm.json past the loader's ceiling.
                combined = _cap_conflict_ids(
                    set(self._state["conflict_ids"]) | set(established),
                    self._state["convoy_id"])
                if combined != self._state["conflict_ids"]:
                    candidate = self._snapshot_locked()
                    candidate["conflict_ids"] = combined
                    return self._commit_locked(candidate)
                return self._snapshot_locked()

            if state == ESTABLISHED:
                local_id = self._state["convoy_id"]
                distinct = set(established)
                distinct.add(local_id)
                if len(distinct) > 1:
                    return self._enter_conflict_locked(
                        distinct, preserved_id=local_id)
                # Candidate announcements are uncommitted and cannot rewrite
                # a realm which has already crossed the commit boundary.
                return self._snapshot_locked()

            # Candidate: established evidence takes precedence immediately.
            # Preserve the local candidate id as the conflict's authoritative
            # id so the state is not convoy_id=None -- that null form refused
            # every operation with no recovery (review 2026-08-02). reset()
            # restarts genesis from here.
            if len(established) > 1:
                return self._enter_conflict_locked(
                    established, preserved_id=self._state["convoy_id"])
            if len(established) == 1:
                return self._commit_locked(_state_payload(
                    ESTABLISHED, next(iter(established))))

            selected = min(candidates | {self._state["convoy_id"]})
            if now >= self._state["settle_deadline_unix"]:
                return self._commit_locked(_state_payload(
                    ESTABLISHED, selected))
            if selected != self._state["convoy_id"]:
                candidate = self._snapshot_locked()
                candidate["convoy_id"] = selected
                return self._commit_locked(candidate)
            return self._snapshot_locked()

    def tick(self):
        """Advance an expired candidate without requiring network traffic."""
        return self.reconcile()

    def _enter_conflict_locked(self, established_ids, preserved_id=None):
        conflict_ids = set(established_ids)
        if preserved_id is not None:
            conflict_ids.add(preserved_id)
        if len(conflict_ids) < 2:
            raise RealmValidationError(
                "split-realm conflict requires two established IDs")
        conflict_ids = _cap_conflict_ids(conflict_ids, preserved_id)
        return self._commit_locked(_state_payload(
            CONFLICT,
            preserved_id,
            conflict_ids=conflict_ids,
        ))

    def adopt(self, convoy_id):
        """Operator-confirmed adoption: commit ``convoy_id`` as this host's
        ESTABLISHED realm, from ANY current state (including conflict and
        unbound), in one atomic disk-first transition.

        This exists so Join Other Realm never passes through an unbound
        instant: the first design reset() first and re-derived, and a
        failed write in that window left the host uncommitted -- which is
        precisely the state an un-admitted LAN announcement is allowed to
        claim (security review, 2026-08-12). ``_commit_locked`` writes
        before publishing, so a failed write leaves both memory and disk
        on the previous committed realm.

        Authority is the CALLER's problem: HostApp exposes this only
        through the loopback-authenticated reset route, carrying an
        operator confirmation. Discovery observations must keep using
        ``reconcile``, which cannot rewrite a committed realm.
        """
        convoy_id = _convoy_id(convoy_id)
        with self._lock:
            return self._commit_locked(_state_payload(
                ESTABLISHED, convoy_id))

    def reset(self):
        """Advanced local recovery: clear the realm binding/conflict so this
        host can re-run leaderless genesis.

        This is the plan's required local reset/rejoin action (section 9.1).
        A CONFLICT never clears on its own; the operator invokes this after
        resolving the split. Afterwards the host is unbound and
        ``begin_candidate`` (or adopting a discovered established realm) works
        again.
        """
        with self._lock:
            self._state = None
            try:
                os.remove(self.path)
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise RealmUnreadable(
                    f"cannot clear {self.path}: {exc}") from exc
            return None
