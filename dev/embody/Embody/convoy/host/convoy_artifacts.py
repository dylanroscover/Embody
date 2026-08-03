"""Private, quota-managed artifact storage for Convoy.

The store is deliberately independent of TouchDesigner and HTTP.  The host
app supplies authenticated streams and maps the structured exceptions below
to its local/peer API.  Runtime artifacts live in the per-user cache returned
by :func:`default_cache_root`; the only operation which writes into a project
is the explicit :meth:`ArtifactStore.export_to_project` call.

Quota zero has one unambiguous meaning: new managed artifacts are disabled.
It is *not* unlimited.  Existing artifacts remain readable/exportable, and
unpinned entries may be removed by cleanup.  Explicit project exports are
outside the runtime quota because they are user-owned project files.

The metadata index contains no bearer token and no signing/encryption secret.
Download capabilities are opaque random values; only their SHA-256 digests
and namespace/audience restrictions are stored.  Used tokens remain recorded
until expiry, so a daemon restart cannot turn a one-shot token reusable.
"""

from __future__ import annotations

import base64
import errno
import hashlib
import io
import json
import mimetypes
import ntpath
import os
import posixpath
import re
import secrets
import shutil
import stat
import sys
import threading
import time
from typing import BinaryIO, Dict, Iterator, Optional

import convoy_identity


STORE_VERSION = 2
LEGACY_STORE_VERSION = 1
DEFAULT_QUOTA_MB = 1024
DEFAULT_TTL_S = 24 * 60 * 60
DEFAULT_TOKEN_TTL_S = 5 * 60
MAX_TOKEN_TTL_S = 60 * 60
DEFAULT_FREE_SPACE_FLOOR_BYTES = 512 * 1024 * 1024
DEFAULT_MAX_ARTIFACT_BYTES = 512 * 1024 * 1024
MAX_STREAM_CHUNK_BYTES = 1024 * 1024
DEFAULT_STREAM_CHUNK_BYTES = 64 * 1024
MAX_STATE_BYTES = 16 * 1024 * 1024
# Metadata has its own hard ceilings, independent of the byte quota.  These
# bounds are intentionally conservative enough that even maximum-length UTF-8
# owner/audience/reference fields remain well below MAX_STATE_BYTES.  The
# encoded-size check in _save_state_locked is still authoritative; the count
# limits make reaching it an exceptional corruption/upgrade case instead of a
# normal long-running-daemon failure mode.
MAX_ARTIFACT_RECORDS = 4096
MAX_CAPABILITIES = 4096
MAX_TOTAL_OWNER_CLAIMS = 4096
MAX_TOTAL_PROTECTIONS = 4096
PARTIAL_STALE_S = 60 * 60
# A process can die after materializing bytes but before closing/releasing its
# handoff.  Transfer protections are therefore crash-safe but not immortal.
# This exceeds the public one-hour transfer deadline while still recovering
# abandoned cache entries without operator intervention.
ACTIVE_TRANSFER_STALE_S = 2 * 60 * 60
# One content hash may legitimately be produced by multiple peers/jobs.  Keep
# those authorization claims beside the single cached byte object, but bound
# metadata growth independently of the byte quota.
MAX_OWNER_CLAIMS = 1024

_ARTIFACT_ID = re.compile(r"^art_[0-9a-f]{64}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CAPABILITY = re.compile(r"^[A-Za-z0-9_-]{32,256}$")
_PROTECTION_KINDS = frozenset(
    ("active_transfer", "running_job", "unacknowledged_job", "pin")
)
_OWNER_FIELDS = frozenset(
    ("job_id", "controller_id", "host_id", "node_id")
)


class ArtifactError(Exception):
    """Base class carrying a stable machine-readable refusal reason."""

    reason = "artifact_error"

    def __init__(self, detail: str = ""):
        super().__init__(f"{self.reason}: {detail}" if detail else self.reason)
        self.detail = detail


class ArtifactValidationError(ArtifactError):
    reason = "artifact_invalid"


class ArtifactNotFound(ArtifactError):
    reason = "artifact_not_found"


class ArtifactQuotaExceeded(ArtifactError):
    reason = "artifact_quota_exceeded"


class ArtifactCorrupt(ArtifactError):
    reason = "artifact_corrupt"


class ArtifactUnauthorized(ArtifactError):
    reason = "artifact_capability_invalid"


class ArtifactTokenExpired(ArtifactUnauthorized):
    reason = "artifact_capability_expired"


class ArtifactTokenReplay(ArtifactUnauthorized):
    reason = "artifact_capability_replayed"


class ArtifactProtected(ArtifactError):
    reason = "artifact_protected"


class ArtifactExists(ArtifactError):
    reason = "artifact_export_exists"


class ArtifactOwnerClaimsExceeded(ArtifactError):
    reason = "artifact_owner_claims_exceeded"


def default_cache_root(platform: Optional[str] = None,
                       environ: Optional[dict] = None,
                       home: Optional[str] = None) -> str:
    """Return the per-user managed artifact cache root.

    ``platform``/``environ``/``home`` are injectable only for deterministic
    platform tests.  Linux is supported as a courteous fallback even though
    the first release targets Windows and Apple Silicon macOS.
    """

    platform = sys.platform if platform is None else platform
    environ = os.environ if environ is None else environ
    home = os.path.expanduser("~") if home is None else home
    if platform.startswith("win"):
        local = environ.get("LOCALAPPDATA")
        if not local:
            local = ntpath.join(home, "AppData", "Local")
        return ntpath.join(local, "Embody", "Convoy", "cache", "artifacts")
    if platform == "darwin":
        return posixpath.join(home, "Library", "Caches", "Embody", "Convoy",
                              "artifacts")
    base = environ.get("XDG_CACHE_HOME") or posixpath.join(home, ".cache")
    return posixpath.join(base, "Embody", "Convoy", "artifacts")


def _finite_number(value, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ArtifactValidationError(f"{name} must be a finite number")
    value = float(value)
    if value != value or value in (float("inf"), float("-inf")):
        raise ArtifactValidationError(f"{name} must be a finite number")
    return value


def _bounded_text(value, name: str, limit: int, *, optional=False) -> str:
    if value is None and optional:
        return ""
    if not isinstance(value, str):
        raise ArtifactValidationError(f"{name} must be text")
    if not value or len(value.encode("utf-8", "strict")) > limit:
        raise ArtifactValidationError(f"{name} is empty or too long")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ArtifactValidationError(f"{name} contains control characters")
    return value


def _filename(value, *, optional=False) -> str:
    value = _bounded_text(value, "filename", 255, optional=optional)
    if not value:
        return ""
    # Treat both separator families as separators on every OS.  This keeps a
    # token minted on macOS from becoming traversal when consumed on Windows.
    if (value in (".", "..") or "/" in value or "\\" in value
            or ":" in value or os.path.isabs(value)):
        raise ArtifactValidationError("filename must be one plain basename")
    return value


def _mime_type(value: str) -> str:
    value = _bounded_text(value, "mime_type", 255)
    if "/" not in value or any(char.isspace() for char in value):
        raise ArtifactValidationError("mime_type must be a media type")
    return value.lower()


def _sha256(value: Optional[str]) -> str:
    if value is None:
        return ""
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ArtifactValidationError("expected_sha256 must be lowercase SHA-256")
    return value


def _artifact_id(value: str) -> str:
    if not isinstance(value, str) or not _ARTIFACT_ID.fullmatch(value):
        raise ArtifactValidationError("malformed artifact_id")
    return value


def _namespace(value: str) -> str:
    try:
        return convoy_identity.normalize_convoy_id(value)
    except convoy_identity.IdentityError as exc:
        raise ArtifactValidationError(str(exc)) from exc


def _regular_file(path: str) -> bool:
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        return False
    return stat.S_ISREG(mode)


class ArtifactDownload(Iterator[bytes]):
    """A bounded, verified download lease.

    Construction has already consumed the one-shot capability and verified
    the complete content hash.  Iteration never exposes the host cache path.
    Call ``close`` when an HTTP client disconnects; exhaustion closes it
    automatically and releases the active-transfer protection.
    """

    def __init__(self, store: "ArtifactStore", file_obj: BinaryIO,
                 namespace: str, artifact_id: str, protection_id: str,
                 *, offset: int, length: int, chunk_size: int,
                 metadata: dict):
        self._store = store
        self._file = file_obj
        self._namespace = namespace
        self._artifact_id = artifact_id
        self._protection_id = protection_id
        self._remaining = length
        self._chunk_size = chunk_size
        self._closed = False
        self.offset = offset
        self.length = length
        self.total_size = metadata["size"]
        self.sha256 = metadata["sha256"]
        self.mime_type = metadata["mime_type"]
        self.filename_hint = metadata.get("filename_hint", "")
        self.artifact_id = artifact_id
        file_obj.seek(offset)

    def __iter__(self):
        return self

    def __next__(self) -> bytes:
        if self._closed or self._remaining <= 0:
            self.close()
            raise StopIteration
        amount = min(self._chunk_size, self._remaining)
        data = self._file.read(amount)
        if not data:
            self.close()
            raise ArtifactCorrupt("artifact ended during download")
        if len(data) > amount:
            self.close()
            raise ArtifactCorrupt("file reader exceeded requested bound")
        self._remaining -= len(data)
        if self._remaining == 0:
            self.close()
        return data

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._file.close()
        finally:
            self._store._finish_download(
                self._namespace, self._artifact_id, self._protection_id)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    def __del__(self):  # pragma: no cover - best-effort shutdown guard
        try:
            self.close()
        except Exception:
            pass


class ArtifactStore:
    """Thread-safe content-addressed cache shared by all local Convoys."""

    def __init__(self, cache_root: Optional[str] = None, *,
                 quota_mb: int = DEFAULT_QUOTA_MB,
                 free_space_floor_bytes: int = DEFAULT_FREE_SPACE_FLOOR_BYTES,
                 default_ttl_s: float = DEFAULT_TTL_S,
                 max_artifact_bytes: int = DEFAULT_MAX_ARTIFACT_BYTES,
                 clock=time.time):
        self.cache_root = os.path.abspath(cache_root or default_cache_root())
        self.data_root = os.path.join(self.cache_root, "v1")
        self.state_path = os.path.join(self.data_root, "index.json")
        self._lock = threading.RLock()
        # In-flight uploads stream without holding _lock.  Reservations keep
        # their full declared size charged against the quota while their
        # partial files grow, so concurrent uploads cannot each observe the
        # same free capacity and overcommit it.
        self._upload_reservations = {}
        self._active_upload_artifacts = {}
        self._clock = clock
        self.quota_bytes = self._quota_bytes(quota_mb)
        self.free_space_floor_bytes = self._nonnegative_int(
            free_space_floor_bytes, "free_space_floor_bytes")
        self.max_artifact_bytes = self._positive_int(
            max_artifact_bytes, "max_artifact_bytes")
        self.default_ttl_s = _finite_number(default_ttl_s, "default_ttl_s")
        if self.default_ttl_s <= 0:
            raise ArtifactValidationError("default_ttl_s must be positive")

        self._make_private_directory(self.cache_root)
        self._make_private_directory(self.data_root)
        self._assert_not_symlink(self.state_path, allow_missing=True)
        self._state = self._load_state()
        with self._lock:
            changed = self._remove_stale_partials_locked(self._clock())
            changed |= self._prune_capabilities_locked(self._clock())
            changed |= self._expire_stale_transfer_protections_locked(
                self._clock())
            if changed:
                self._save_state_locked()

    @staticmethod
    def _nonnegative_int(value, name):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ArtifactValidationError(f"{name} must be a nonnegative integer")
        return value

    @classmethod
    def _positive_int(cls, value, name):
        value = cls._nonnegative_int(value, name)
        if value == 0:
            raise ArtifactValidationError(f"{name} must be positive")
        return value

    @classmethod
    def _quota_bytes(cls, quota_mb):
        quota_mb = cls._nonnegative_int(quota_mb, "quota_mb")
        # A hard sanity limit avoids accidental multiplication into absurd
        # values while remaining far above any plausible installation.
        if quota_mb > 1024 * 1024:
            raise ArtifactValidationError("quota_mb exceeds 1 PiB")
        return quota_mb * 1024 * 1024

    @staticmethod
    def _assert_not_symlink(path: str, *, allow_missing=False) -> None:
        try:
            mode = os.lstat(path).st_mode
        except FileNotFoundError:
            if allow_missing:
                return
            raise
        if stat.S_ISLNK(mode):
            raise ArtifactValidationError(f"managed path is a symlink: {path}")

    def _make_private_directory(self, path: str) -> None:
        if os.path.lexists(path):
            self._assert_not_symlink(path)
            if not os.path.isdir(path):
                raise ArtifactValidationError(f"managed path is not a directory: {path}")
        else:
            os.makedirs(path, mode=0o700, exist_ok=False)
        try:
            os.chmod(path, 0o700)
        except OSError:
            # Windows ACL ownership is provided by the per-user LocalAppData
            # tree; chmod's POSIX bit projection is neither complete nor
            # consistently implemented there.
            if os.name != "nt":
                raise

    def _load_state(self) -> dict:
        if not os.path.exists(self.state_path):
            state = {"version": STORE_VERSION, "artifacts": {},
                     "capabilities": {}}
            self._state = state
            self._save_state_locked()
            return state
        if not _regular_file(self.state_path):
            raise ArtifactValidationError("artifact index is not a regular file")
        if os.path.getsize(self.state_path) > MAX_STATE_BYTES:
            raise ArtifactValidationError("artifact index exceeds size limit")
        try:
            with open(self.state_path, "r", encoding="utf-8") as handle:
                state = json.load(handle)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ArtifactValidationError("artifact index is unreadable") from exc
        if (not isinstance(state, dict)
                or state.get("version") not in (
                    LEGACY_STORE_VERSION, STORE_VERSION)
                or not isinstance(state.get("artifacts"), dict)
                or not isinstance(state.get("capabilities"), dict)):
            raise ArtifactValidationError("artifact index schema/version is invalid")
        migrated = self._migrate_legacy_state(state)
        self._validate_loaded_state(state)
        if migrated:
            # Commit the unambiguous v1 owner -> v2 owners[] migration before
            # serving requests. A restart can then never reinterpret a claim.
            self._state = state
            self._save_state_locked()
        return state

    def _encoded_state_locked(self) -> bytes:
        try:
            encoded = (json.dumps(
                self._state, sort_keys=True, separators=(",", ":"),
                ensure_ascii=False) + "\n").encode("utf-8", "strict")
        except (TypeError, ValueError, UnicodeError) as exc:
            raise ArtifactValidationError(
                "artifact index cannot be serialized") from exc
        if len(encoded) > MAX_STATE_BYTES:
            raise ArtifactQuotaExceeded(
                "artifact metadata reached its bounded index limit")
        return encoded

    def _migrate_legacy_state(self, state: dict) -> bool:
        if state.get("version") == STORE_VERSION:
            return False
        if state.get("version") != LEGACY_STORE_VERSION:
            raise ArtifactValidationError("artifact index schema/version is invalid")
        for records in state["artifacts"].values():
            if not isinstance(records, dict):
                raise ArtifactValidationError("artifact namespace record is invalid")
            for record in records.values():
                if not isinstance(record, dict) or "owners" in record:
                    raise ArtifactValidationError(
                        "legacy artifact owner schema is ambiguous")
                owner = self._owner(record.pop("owner", None))
                record["owners"] = [owner] if owner else []
        state["version"] = STORE_VERSION
        return True

    def _validate_loaded_state(self, state: dict) -> None:
        """Fail closed on private-index tampering instead of deriving paths
        or cleanup decisions from partially trusted JSON."""

        for namespace, records in state["artifacts"].items():
            _namespace(namespace)
            if not isinstance(records, dict):
                raise ArtifactValidationError("artifact namespace record is invalid")
            for artifact_id, record in records.items():
                _artifact_id(artifact_id)
                if not isinstance(record, dict):
                    raise ArtifactValidationError("artifact record is invalid")
                digest = _sha256(record.get("sha256"))
                if (not digest or record.get("artifact_id") != artifact_id
                        or artifact_id != "art_" + digest):
                    raise ArtifactValidationError("artifact identity/digest mismatch")
                size = record.get("size")
                if (isinstance(size, bool) or not isinstance(size, int)
                        or not 0 <= size <= 2 ** 63 - 1):
                    raise ArtifactValidationError("artifact size is invalid")
                _mime_type(record.get("mime_type"))
                _filename(record.get("filename_hint"), optional=True)
                for name in ("created_at", "last_accessed_at", "expires_at"):
                    _finite_number(record.get(name), name)
                ttl_s = _finite_number(record.get("ttl_s"), "ttl_s")
                if ttl_s <= 0:
                    raise ArtifactValidationError("artifact ttl is invalid")
                if "owner" in record:
                    raise ArtifactValidationError(
                        "legacy owner field is invalid in this store version")
                owners = record.get("owners")
                if (not isinstance(owners, list)
                        or len(owners) > MAX_OWNER_CLAIMS):
                    raise ArtifactValidationError(
                        "artifact owner claims are invalid")
                seen_owners = set()
                for owner in owners:
                    owner = self._owner(owner)
                    if not owner:
                        raise ArtifactValidationError(
                            "empty artifact owner claim is invalid")
                    identity = tuple(sorted(owner.items()))
                    if identity in seen_owners:
                        raise ArtifactValidationError(
                            "duplicate artifact owner claim is invalid")
                    seen_owners.add(identity)
                protections = record.get("protections")
                if not isinstance(protections, dict):
                    raise ArtifactValidationError("artifact protections are invalid")
                for reference_id, protection in protections.items():
                    _bounded_text(reference_id, "reference_id", 256)
                    if (not isinstance(protection, dict)
                            or protection.get("kind") not in _PROTECTION_KINDS):
                        raise ArtifactValidationError("artifact protection is invalid")
                    count = protection.get("count")
                    if (isinstance(count, bool) or not isinstance(count, int)
                            or count <= 0):
                        raise ArtifactValidationError(
                            "artifact protection count is invalid")
                    _finite_number(protection.get("created_at"), "created_at")

        for token_digest, cap in state["capabilities"].items():
            if not isinstance(token_digest, str) or not _SHA256.fullmatch(token_digest):
                raise ArtifactValidationError("capability digest is invalid")
            if not isinstance(cap, dict) or cap.get("purpose") != "download":
                raise ArtifactValidationError("capability record is invalid")
            namespace = _namespace(cap.get("convoy_id"))
            artifact_id = _artifact_id(cap.get("artifact_id"))
            if artifact_id not in state["artifacts"].get(namespace, {}):
                raise ArtifactValidationError("capability names an absent artifact")
            _bounded_text(cap.get("audience"), "audience", 256)
            _finite_number(cap.get("created_at"), "created_at")
            _finite_number(cap.get("expires_at"), "expires_at")
            if cap.get("used_at") is not None:
                _finite_number(cap.get("used_at"), "used_at")

    def _save_state_locked(self) -> None:
        # Serialize and enforce the SAME byte ceiling used by _load_state
        # before opening/replacing anything.  A daemon must never write an
        # index that its next process refuses to load.
        encoded = self._encoded_state_locked()
        temp = os.path.join(
            self.data_root, ".index-%s.tmp" % secrets.token_hex(8))
        try:
            with open(temp, "xb") as handle:
                try:
                    os.chmod(temp, 0o600)
                except OSError:
                    if os.name != "nt":
                        raise
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            self._assert_not_symlink(self.state_path, allow_missing=True)
            os.replace(temp, self.state_path)
            self._fsync_directory(self.data_root)
        finally:
            try:
                os.unlink(temp)
            except FileNotFoundError:
                pass

    @staticmethod
    def _fsync_directory(path: str) -> None:
        if os.name == "nt":
            return
        flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            flags |= os.O_DIRECTORY
        descriptor = os.open(path, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _namespace_dirname(namespace: str) -> str:
        digest = hashlib.sha256(namespace.encode("utf-8")).hexdigest()
        return "ns_" + digest

    def _namespace_dir(self, namespace: str) -> str:
        return os.path.join(self.data_root, self._namespace_dirname(namespace))

    def _content_path(self, namespace: str, digest: str) -> str:
        return os.path.join(self._namespace_dir(namespace), digest[:2], digest)

    def _prepare_content_directory(self, namespace: str, digest: str) -> str:
        namespace_dir = self._namespace_dir(namespace)
        self._make_private_directory(namespace_dir)
        prefix_dir = os.path.join(namespace_dir, digest[:2])
        self._make_private_directory(prefix_dir)
        return prefix_dir

    def _namespace_records_locked(self, namespace: str, *, create=False) -> dict:
        artifacts = self._state["artifacts"]
        if create:
            return artifacts.setdefault(namespace, {})
        return artifacts.get(namespace, {})

    def _record_locked(self, namespace: str, artifact_id: str) -> dict:
        record = self._namespace_records_locked(namespace).get(artifact_id)
        if not isinstance(record, dict):
            raise ArtifactNotFound(artifact_id)
        return record

    def _path_for_record_locked(self, namespace: str, record: dict) -> str:
        digest = record.get("sha256")
        if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
            raise ArtifactCorrupt("metadata has invalid digest")
        path = self._content_path(namespace, digest)
        if not _regular_file(path):
            if os.path.lexists(path):
                raise ArtifactCorrupt("managed content is not a regular file")
            raise ArtifactCorrupt("managed content is missing")
        return path

    @staticmethod
    def _hash_file(path: str) -> tuple[str, int]:
        digest = hashlib.sha256()
        total = 0
        with open(path, "rb") as handle:
            while True:
                block = handle.read(MAX_STREAM_CHUNK_BYTES)
                if not block:
                    break
                total += len(block)
                digest.update(block)
        return digest.hexdigest(), total

    def _verify_record_locked(self, namespace: str, record: dict) -> str:
        path = self._path_for_record_locked(namespace, record)
        digest, size = self._hash_file(path)
        if digest != record.get("sha256") or size != record.get("size"):
            raise ArtifactCorrupt("managed content failed size/hash verification")
        return path

    def _verify_record_unlocked(self, namespace: str, artifact_id: str) -> None:
        """Full-content verify with ``_lock`` RELEASED across the hash.

        Whole-file SHA-256 verification (up to ``max_artifact_bytes``) must not
        run while holding ``_lock`` -- callers hold ``HostApp.lock`` too, so one
        large/slow download or dedup would otherwise stall the whole daemon
        (review 2026-08-02).  This snapshots the content path + expected
        digest/size under the lock, hashes with the lock released, then relies
        on content-addressing for correctness: a record's ``artifact_id`` is
        ``art_<sha256>``, so its digest is immutable and its content file can
        only be deleted, never mutated.  Callers re-fetch the record under the
        lock immediately after and act on that fresh record; a record deleted
        meanwhile raises ``ArtifactNotFound`` there.  Corrupt content is still
        refused before any caller proceeds.
        """
        with self._lock:
            record = self._record_locked(namespace, artifact_id)
            path = self._path_for_record_locked(namespace, record)
            expected_digest = record["sha256"]
            expected_size = record["size"]
        digest, size = self._hash_file(path)
        if digest != expected_digest or size != expected_size:
            raise ArtifactCorrupt(
                "managed content failed size/hash verification")

    @staticmethod
    def _public_record(namespace: str, record: dict,
                       owner: Optional[dict] = None) -> dict:
        # Deliberately no local filesystem path, cache directory, protection
        # references, bearer capability, or OTHER owner claim appears in this
        # projection. Generic describe() supplies no owner; a put or exact
        # describe_for_owner() supplies only the caller's selected claim.
        result = {
            "kind": "convoy_artifact",
            "convoy_id": namespace,
            "artifact_id": record["artifact_id"],
            "sha256": record["sha256"],
            "size": record["size"],
            "mime_type": record["mime_type"],
            "created_at": record["created_at"],
            "expires_at": record["expires_at"],
        }
        if record.get("filename_hint"):
            result["filename_hint"] = record["filename_hint"]
        for name in _OWNER_FIELDS:
            value = (owner or {}).get(name)
            if value:
                result[name] = value
        return result

    @staticmethod
    def _owner(value) -> dict:
        if value is None:
            return {}
        if not isinstance(value, dict) or set(value) - _OWNER_FIELDS:
            raise ArtifactValidationError("owner contains unsupported fields")
        result = {}
        for name, item in value.items():
            result[name] = _bounded_text(item, name, 256)
        return result

    @classmethod
    def _claim_owner_locked(cls, record: dict, owner: dict) -> bool:
        """Add one exact claim, returning whether persistent state changed."""
        if not owner:
            return False
        owners = record.get("owners")
        if not isinstance(owners, list):
            raise ArtifactCorrupt("artifact owner claims are invalid")
        if owner in owners:
            return False
        if len(owners) >= MAX_OWNER_CLAIMS:
            # A terminal result is protected as job:<id>:result until its
            # controller acknowledges it.  Once that protection is gone the
            # owner claim is no longer needed to serve unobserved work and is
            # safe to retire.  This keeps a static screenshot (one content
            # hash produced by thousands of acknowledged jobs) from failing
            # forever at claim 1025 while retaining every still-active job.
            protections = record.get("protections") or {}
            victim = None
            for index, existing in enumerate(owners):
                job_id = existing.get("job_id") if isinstance(
                    existing, dict) else None
                job_protection = (
                    "job:%s:result" % job_id
                    if isinstance(job_id, str) and job_id else None)
                if job_protection is None or job_protection not in protections:
                    victim = index
                    break
            if victim is None:
                raise ArtifactOwnerClaimsExceeded(
                    "artifact reached its bounded active owner-claim limit")
            del owners[victim]
        owners.append(dict(owner))
        return True

    @staticmethod
    def _has_owner_locked(record: dict, owner: dict) -> bool:
        owners = record.get("owners")
        if not isinstance(owners, list):
            raise ArtifactCorrupt("artifact owner claims are invalid")
        return bool(owner) and owner in owners

    def _metadata_counts_locked(self) -> tuple:
        artifacts = 0
        owners = 0
        protections = 0
        for records in self._state["artifacts"].values():
            if not isinstance(records, dict):
                continue
            artifacts += len(records)
            for record in records.values():
                if not isinstance(record, dict):
                    continue
                owners += len(record.get("owners") or ())
                protections += len(record.get("protections") or {})
        return artifacts, owners, protections

    def _metadata_capacity_error_locked(self, *, artifacts, owners,
                                        protections, capabilities):
        """Return the ceiling-breach exception, or None if the request fits."""
        current_artifacts, current_owners, current_protections = (
            self._metadata_counts_locked())
        if artifacts and current_artifacts + artifacts > MAX_ARTIFACT_RECORDS:
            return ArtifactQuotaExceeded(
                "artifact metadata reached its record limit")
        if owners and current_owners + owners > MAX_TOTAL_OWNER_CLAIMS:
            return ArtifactOwnerClaimsExceeded(
                "artifact metadata reached its owner-claim limit")
        if (protections and current_protections + protections
                > MAX_TOTAL_PROTECTIONS):
            return ArtifactQuotaExceeded(
                "artifact metadata reached its protection limit")
        if (capabilities and len(self._state["capabilities"]) + capabilities
                > MAX_CAPABILITIES):
            return ArtifactQuotaExceeded(
                "artifact metadata reached its capability limit")
        return None

    def _require_metadata_capacity_locked(self, *, artifacts=0, owners=0,
                                          protections=0, capabilities=0,
                                          exclude=None) -> None:
        error = self._metadata_capacity_error_locked(
            artifacts=artifacts, owners=owners,
            protections=protections, capabilities=capabilities)
        if error is None:
            return
        # A metadata ceiling is reclaimable exactly like the byte quota: the
        # record/owner/protection counts fall when unprotected artifacts are
        # evicted.  Run the same expired-then-LRU eviction before refusing so a
        # host with byte-quota to spare does not silently stop storing
        # artifacts at MAX_ARTIFACT_RECORDS (review 2026-08-02).  Expired
        # capabilities are pruned too, since an exhausted capability ceiling is
        # otherwise unreachable-by-eviction (a live-capability record is
        # protected).
        now = self._clock()
        if capabilities and self._prune_capabilities_locked(now):
            self._save_state_locked()
        for _, _, _, namespace, artifact_id in self._eviction_candidates_locked(
                now, exclude=exclude):
            if self._metadata_capacity_error_locked(
                    artifacts=artifacts, owners=owners,
                    protections=protections, capabilities=capabilities) is None:
                break
            self._delete_locked(namespace, artifact_id, force=True, now=now)
            # Persist each eviction immediately: _delete_locked unlinks bytes
            # right away, so an unsaved index could name deleted files after a
            # crash (mirrors _ensure_capacity_locked).
            self._save_state_locked()
        error = self._metadata_capacity_error_locked(
            artifacts=artifacts, owners=owners,
            protections=protections, capabilities=capabilities)
        if error is not None:
            raise error

    def _touch_locked(self, record: dict, now: float) -> None:
        record["last_accessed_at"] = now
        record["expires_at"] = now + record.get("ttl_s", self.default_ttl_s)

    def _partial_paths_locked(self):
        for base, directories, files in os.walk(self.data_root, followlinks=False):
            # Do not descend through a planted symlink.
            kept = []
            for name in directories:
                path = os.path.join(base, name)
                if not os.path.islink(path):
                    kept.append(name)
            directories[:] = kept
            for name in files:
                if name.startswith(".upload-") and name.endswith(".part"):
                    yield os.path.join(base, name)

    def _remove_stale_partials_locked(self, now: float) -> bool:
        changed = False
        for path in list(self._partial_paths_locked()):
            try:
                info = os.lstat(path)
                if stat.S_ISREG(info.st_mode) and now - info.st_mtime >= PARTIAL_STALE_S:
                    os.unlink(path)
                    changed = True
            except FileNotFoundError:
                pass
        return changed

    def _expire_stale_transfer_protections_locked(self, now: float) -> bool:
        """Recover protections orphaned by a crashed transfer process."""
        changed = False
        cutoff = now - ACTIVE_TRANSFER_STALE_S
        for records in self._state["artifacts"].values():
            if not isinstance(records, dict):
                continue
            for record in records.values():
                if not isinstance(record, dict):
                    continue
                protections = record.get("protections")
                if not isinstance(protections, dict):
                    continue
                for reference_id, protection in list(protections.items()):
                    if (isinstance(protection, dict)
                            and protection.get("kind") == "active_transfer"
                            and protection.get("created_at", now) <= cutoff):
                        del protections[reference_id]
                        changed = True
        return changed

    def _usage_locked(self) -> int:
        total = 0
        for records in self._state["artifacts"].values():
            if not isinstance(records, dict):
                continue
            for record in records.values():
                if isinstance(record, dict):
                    size = record.get("size", 0)
                    if isinstance(size, int) and not isinstance(size, bool) and size >= 0:
                        total += size
        active_paths = {
            reservation.get("path")
            for reservation in self._upload_reservations.values()
            if isinstance(reservation, dict) and reservation.get("path")
        }
        for path in self._partial_paths_locked():
            # Active partial bytes are represented by their FULL reservation
            # below.  Counting both would make quota enforcement spuriously
            # charge an upload twice as it approached completion.
            if path in active_paths:
                continue
            try:
                info = os.lstat(path)
                if stat.S_ISREG(info.st_mode):
                    total += info.st_size
            except FileNotFoundError:
                pass
        total += sum(
            reservation.get("expected_size", 0)
            for reservation in self._upload_reservations.values()
            if isinstance(reservation, dict))
        return total

    def _reserved_remaining_locked(self) -> int:
        """Disk bytes promised but not yet present in active partials."""
        remaining = 0
        for reservation in self._upload_reservations.values():
            if not isinstance(reservation, dict):
                continue
            expected = reservation.get("expected_size", 0)
            current = 0
            path = reservation.get("path")
            if path:
                try:
                    info = os.lstat(path)
                    if stat.S_ISREG(info.st_mode):
                        current = min(expected, info.st_size)
                except FileNotFoundError:
                    pass
            remaining += max(0, expected - current)
        return remaining

    def _free_bytes_locked(self) -> int:
        return shutil.disk_usage(self.data_root).free

    def _has_live_capability_locked(self, namespace: str,
                                    artifact_id: str, now: float) -> bool:
        for cap in self._state["capabilities"].values():
            if (isinstance(cap, dict) and cap.get("used_at") is None
                    and cap.get("convoy_id") == namespace
                    and cap.get("artifact_id") == artifact_id
                    and isinstance(cap.get("expires_at"), (int, float))
                    and cap["expires_at"] > now):
                return True
        return False

    def _is_protected_locked(self, namespace: str, record: dict,
                             now: float) -> bool:
        if self._active_upload_artifacts.get(
                (namespace, record.get("artifact_id")), 0) > 0:
            return True
        protections = record.get("protections")
        if isinstance(protections, dict) and protections:
            return True
        return self._has_live_capability_locked(
            namespace, record.get("artifact_id", ""), now)

    def _delete_locked(self, namespace: str, artifact_id: str,
                       *, force=False, now=None) -> int:
        records = self._namespace_records_locked(namespace)
        record = records.get(artifact_id)
        if not isinstance(record, dict):
            raise ArtifactNotFound(artifact_id)
        now = self._clock() if now is None else now
        if not force and self._is_protected_locked(namespace, record, now):
            raise ArtifactProtected(artifact_id)
        digest = record.get("sha256")
        if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
            # Never derive a deletion target from corrupt metadata.  In
            # particular, an empty digest would otherwise name a directory.
            raise ArtifactCorrupt("metadata has invalid digest")
        path = self._content_path(namespace, digest)
        self._assert_not_symlink(path, allow_missing=True)
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        size = record.get("size", 0)
        del records[artifact_id]
        if not records:
            self._state["artifacts"].pop(namespace, None)
        for key, cap in list(self._state["capabilities"].items()):
            if (isinstance(cap, dict) and cap.get("convoy_id") == namespace
                    and cap.get("artifact_id") == artifact_id):
                del self._state["capabilities"][key]
        return size if isinstance(size, int) else 0

    def _eviction_candidates_locked(self, now: float, exclude=None):
        rows = []
        for namespace, records in self._state["artifacts"].items():
            if not isinstance(records, dict):
                continue
            for artifact_id, record in records.items():
                if ((namespace, artifact_id) == exclude
                        or not isinstance(record, dict)
                        or self._is_protected_locked(namespace, record, now)):
                    continue
                expired = record.get("expires_at", 0) <= now
                accessed = record.get("last_accessed_at", 0)
                rows.append((0 if expired else 1, accessed,
                             record.get("created_at", 0), namespace,
                             artifact_id))
        return sorted(rows)

    def _ensure_capacity_locked(self, required: int, *, exclude=None) -> list:
        if self.quota_bytes == 0:
            raise ArtifactQuotaExceeded("artifact storage is disabled (quota is 0)")
        if required > self.quota_bytes:
            raise ArtifactQuotaExceeded("artifact exceeds host quota")
        now = self._clock()
        if self._expire_stale_transfer_protections_locked(now):
            self._save_state_locked()
        removed = []
        while True:
            usage = self._usage_locked()
            free = self._free_bytes_locked()
            quota_ok = usage + required <= self.quota_bytes
            floor_ok = (free - self._reserved_remaining_locked() - required
                        >= self.free_space_floor_bytes)
            if quota_ok and floor_ok:
                return removed
            candidates = self._eviction_candidates_locked(now, exclude=exclude)
            if not candidates:
                why = "free-space floor" if not floor_ok else "quota"
                raise ArtifactQuotaExceeded(
                    f"insufficient {why}; all remaining artifacts are protected")
            _, _, _, namespace, artifact_id = candidates[0]
            removed.append((namespace, artifact_id,
                            self._delete_locked(namespace, artifact_id,
                                                force=True, now=now)))
            # Eviction deletes bytes immediately.  Persist its metadata side
            # before attempting the subsequent upload so a short body or
            # ENOSPC failure cannot leave the index pointing at deleted files.
            self._save_state_locked()

    @staticmethod
    def _consume_stream(stream: BinaryIO, expected_size: int,
                        chunk_size: int, writer=None) -> tuple:
        """Consume exactly one bounded body, optionally writing each block."""
        digest = hashlib.sha256()
        total = 0
        while total < expected_size:
            amount = min(chunk_size, expected_size - total)
            block = stream.read(amount)
            if not isinstance(block, (bytes, bytearray, memoryview)):
                raise ArtifactValidationError(
                    "stream.read() must return bytes")
            block = bytes(block)
            if len(block) > amount:
                raise ArtifactValidationError(
                    "stream exceeded requested read bound")
            if not block:
                raise ArtifactValidationError(
                    "stream ended before expected_size")
            if writer is not None:
                writer.write(block)
            digest.update(block)
            total += len(block)
        extra = stream.read(1)
        if not isinstance(extra, (bytes, bytearray, memoryview)):
            raise ArtifactValidationError(
                "stream.read() must return bytes")
        if extra:
            raise ArtifactValidationError("stream exceeds expected_size")
        return digest.hexdigest(), total

    @staticmethod
    def _record_growth(record: dict, owner: dict,
                       protection_id: Optional[str]) -> tuple:
        owners = record.get("owners") or []
        owner_growth = int(bool(owner) and owner not in owners
                           and len(owners) < MAX_OWNER_CLAIMS)
        protections = record.get("protections") or {}
        protection_growth = int(
            protection_id is not None and protection_id not in protections)
        return owner_growth, protection_growth

    def _update_existing_locked(self, namespace: str, record: dict,
                                owner: dict, protection_id: Optional[str],
                                protection_kind: Optional[str], now: float
                                ) -> dict:
        """Commit owner/protection/access metadata for verified content."""
        owner_growth, protection_growth = self._record_growth(
            record, owner, protection_id)
        # Exclude this live record from eviction: we are claiming ownership on
        # it, so it must not be reclaimed to make room for its own claim.
        self._require_metadata_capacity_locked(
            owners=owner_growth, protections=protection_growth,
            exclude=(namespace, record["artifact_id"]))
        old_owners = [dict(item) for item in record.get("owners") or ()]
        old_protections = {
            key: dict(value) for key, value in
            (record.get("protections") or {}).items()}
        old_accessed = record.get("last_accessed_at")
        old_expires = record.get("expires_at")
        try:
            self._claim_owner_locked(record, owner)
            self._ensure_protection_locked(
                record, protection_id, protection_kind, now)
            self._touch_locked(record, now)
            # Preflight before replacing index.json, so a metadata-bound
            # refusal leaves this live object exactly as it was.
            self._encoded_state_locked()
        except Exception:
            record["owners"] = old_owners
            record["protections"] = old_protections
            record["last_accessed_at"] = old_accessed
            record["expires_at"] = old_expires
            raise
        self._save_state_locked()
        return self._public_record(namespace, record, owner)

    def put_stream(self, namespace: str, stream: BinaryIO, *,
                   expected_size: int, expected_sha256: Optional[str] = None,
                   mime_type: str = "application/octet-stream",
                   filename_hint: Optional[str] = None,
                   ttl_s: Optional[float] = None,
                   owner: Optional[dict] = None,
                   protection_id: Optional[str] = None,
                   protection_kind: Optional[str] = None,
                   verify_existing_stream: bool = False,
                   chunk_size: int = DEFAULT_STREAM_CHUNK_BYTES) -> dict:
        """Store one bounded stream and return a path-free public reference.

        ``expected_size`` is mandatory so the host can reserve both quota and
        free-disk headroom *before* accepting bytes.  ``expected_sha256`` is
        optional for locally produced data but should be mandatory at the
        network boundary.  A trusted local caller may deduplicate a known,
        verified object without reading the supplied stream again.  Network
        callers set ``verify_existing_stream`` so a body whose claimed digest
        already exists is still consumed and verified rather than accepted by
        metadata alone.
        """

        namespace = _namespace(namespace)
        expected_size = self._nonnegative_int(expected_size, "expected_size")
        if expected_size > self.max_artifact_bytes:
            raise ArtifactQuotaExceeded("artifact exceeds per-artifact limit")
        expected_sha256 = _sha256(expected_sha256)
        mime_type = _mime_type(mime_type)
        filename_hint = _filename(filename_hint, optional=True)
        owner = self._owner(owner)
        if (protection_id is None) != (protection_kind is None):
            raise ArtifactValidationError(
                "protection_id and protection_kind must be supplied together")
        if protection_id is not None:
            protection_id = _bounded_text(
                protection_id, "protection_id", 256)
            if protection_kind not in _PROTECTION_KINDS:
                raise ArtifactValidationError("unsupported protection kind")
        if not isinstance(verify_existing_stream, bool):
            raise ArtifactValidationError(
                "verify_existing_stream must be boolean")
        chunk_size = self._positive_int(chunk_size, "chunk_size")
        if chunk_size > MAX_STREAM_CHUNK_BYTES:
            raise ArtifactValidationError("chunk_size exceeds streaming bound")
        ttl_s = (self.default_ttl_s if ttl_s is None
                 else _finite_number(ttl_s, "ttl_s"))
        if ttl_s <= 0:
            raise ArtifactValidationError("ttl_s must be positive")
        if not hasattr(stream, "read"):
            raise ArtifactValidationError("stream must provide read()")

        # A known digest can skip a trusted local body, but a network body
        # must still be consumed and verified.  Mark the indexed object active
        # in a process-local table while doing that slow read; eviction sees
        # the mark, but it never bloats or dirties the durable index.
        known_id = "art_" + expected_sha256 if expected_sha256 else None
        known_key = (namespace, known_id) if known_id else None
        known_active = False
        if known_id:
            with self._lock:
                known = self._namespace_records_locked(namespace).get(known_id)
                known_present = isinstance(known, dict)
                if known_present and known.get("size") != expected_size:
                    raise ArtifactCorrupt("known digest has conflicting size")
            if known_present:
                # Verify the already-cached object with the lock released so a
                # large cached artifact never stalls the daemon during dedup.
                try:
                    self._verify_record_unlocked(namespace, known_id)
                except ArtifactNotFound:
                    # Evicted between the check and the hash; fall through to
                    # the normal upload path below.
                    known_present = False
            if known_present:
                with self._lock:
                    known = self._namespace_records_locked(
                        namespace).get(known_id)
                    if isinstance(known, dict):
                        if known.get("size") != expected_size:
                            raise ArtifactCorrupt(
                                "known digest has conflicting size")
                        if not verify_existing_stream:
                            return self._update_existing_locked(
                                namespace, known, owner, protection_id,
                                protection_kind, self._clock())
                        self._active_upload_artifacts[known_key] = (
                            self._active_upload_artifacts.get(known_key, 0) + 1)
                        known_active = True
            if known_active:
                try:
                    supplied_digest, supplied_size = self._consume_stream(
                        stream, expected_size, chunk_size)
                    if (supplied_size != expected_size
                            or supplied_digest != expected_sha256):
                        raise ArtifactCorrupt("uploaded content hash mismatch")
                    # The record is held active (protected) above, so it cannot
                    # be evicted while we verify it with the lock released.
                    self._verify_record_unlocked(namespace, known_id)
                    with self._lock:
                        known = self._record_locked(namespace, known_id)
                        if known.get("size") != supplied_size:
                            raise ArtifactCorrupt(
                                "known digest has conflicting size")
                        return self._update_existing_locked(
                            namespace, known, owner, protection_id,
                            protection_kind, self._clock())
                finally:
                    with self._lock:
                        count = self._active_upload_artifacts.get(
                            known_key, 1) - 1
                        if count <= 0:
                            self._active_upload_artifacts.pop(known_key, None)
                        else:
                            self._active_upload_artifacts[known_key] = count

        reservation_id = secrets.token_hex(16)
        upload_dir = os.path.join(self.data_root, "uploads")
        temp = os.path.join(
            upload_dir, ".upload-%s.part" % reservation_id)
        with self._lock:
            self._ensure_capacity_locked(expected_size)
            self._make_private_directory(upload_dir)
            self._upload_reservations[reservation_id] = {
                "expected_size": expected_size, "path": temp}

        total = 0
        actual_sha256 = ""
        try:
            # Slow/untrusted body reads and fsync happen without the artifact
            # metadata lock.  Reads, grants, status and unrelated transfers
            # remain responsive while this stream stalls.
            with open(temp, "xb") as handle:
                try:
                    os.chmod(temp, 0o600)
                except OSError:
                    if os.name != "nt":
                        raise
                actual_sha256, total = self._consume_stream(
                    stream, expected_size, chunk_size, writer=handle)
                handle.flush()
                os.fsync(handle.fileno())
            if expected_sha256 and actual_sha256 != expected_sha256:
                raise ArtifactCorrupt("uploaded content hash mismatch")

            artifact_id = "art_" + actual_sha256
            with self._lock:
                existing_present = isinstance(
                    self._namespace_records_locked(namespace).get(artifact_id),
                    dict)
            if existing_present:
                # Dedup against the already-cached object; verify it with the
                # lock released so a large cached artifact never stalls dedup.
                try:
                    self._verify_record_unlocked(namespace, artifact_id)
                except ArtifactNotFound:
                    existing_present = False
            if existing_present:
                with self._lock:
                    existing = self._namespace_records_locked(
                        namespace).get(artifact_id)
                    if isinstance(existing, dict):
                        if existing.get("size") != total:
                            raise ArtifactCorrupt(
                                "deduplicated content size conflict")
                        result = self._update_existing_locked(
                            namespace, existing, owner, protection_id,
                            protection_kind, self._clock())
                        try:
                            os.unlink(temp)
                        except FileNotFoundError:
                            pass
                        temp = ""
                        return result
                    # Evicted between verify and re-lock: install our own bytes
                    # through the create path below.
            with self._lock:
                now = self._clock()
                records = self._namespace_records_locked(
                    namespace, create=True)
                existing = records.get(artifact_id)
                if isinstance(existing, dict):
                    # A concurrent upload installed the identical object while
                    # we hashed unlocked.  Adopt it (content is addressed by
                    # digest and was verified by that upload) and drop temp.
                    if existing.get("size") != total:
                        raise ArtifactCorrupt(
                            "deduplicated content size conflict")
                    result = self._update_existing_locked(
                        namespace, existing, owner, protection_id,
                        protection_kind, now)
                    try:
                        os.unlink(temp)
                    except FileNotFoundError:
                        pass
                    temp = ""
                    return result

                owner_count = int(bool(owner))
                protection_count = int(protection_id is not None)
                self._require_metadata_capacity_locked(
                    artifacts=1, owners=owner_count,
                    protections=protection_count)
                record = {
                    "artifact_id": artifact_id,
                    "sha256": actual_sha256,
                    "size": total,
                    "mime_type": mime_type,
                    "filename_hint": filename_hint,
                    "created_at": now,
                    "last_accessed_at": now,
                    "ttl_s": ttl_s,
                    "expires_at": now + ttl_s,
                    "owners": [dict(owner)] if owner else [],
                    "protections": ({protection_id: {
                        "kind": protection_kind, "count": 1,
                        "created_at": now,
                    }} if protection_id is not None else {}),
                }
                # Prove candidate metadata is reloadable before moving bytes
                # into their content-addressed final location.
                records[artifact_id] = record
                try:
                    self._encoded_state_locked()
                except Exception:
                    records.pop(artifact_id, None)
                    if not records:
                        self._state["artifacts"].pop(namespace, None)
                    raise
                # Candidate validation is complete.  Keep the durable/live
                # index free of a record until its final content path has
                # been installed and fsynced.
                records.pop(artifact_id, None)

                target_dir = self._prepare_content_directory(
                    namespace, actual_sha256)
                target = self._content_path(namespace, actual_sha256)
                if os.path.lexists(target):
                    if not _regular_file(target):
                        raise ArtifactCorrupt(
                            "content target is not a regular file")
                    target_hash, target_size = self._hash_file(target)
                    if target_hash != actual_sha256 or target_size != total:
                        raise ArtifactCorrupt(
                            "unindexed content target conflicts")
                    os.unlink(temp)
                    temp = ""
                else:
                    os.replace(temp, target)
                    temp = ""
                    try:
                        os.chmod(target, 0o600)
                    except OSError:
                        if os.name != "nt":
                            raise
                    self._fsync_directory(target_dir)
                records[artifact_id] = record
                self._save_state_locked()
                return self._public_record(namespace, record, owner)
        except OSError as exc:
            if exc.errno in (errno.ENOSPC, getattr(errno, "EDQUOT", -1)):
                raise ArtifactQuotaExceeded(
                    "disk refused artifact write") from exc
            raise
        finally:
            with self._lock:
                self._upload_reservations.pop(reservation_id, None)
            if temp:
                try:
                    os.unlink(temp)
                except FileNotFoundError:
                    pass

    def put_bytes(self, namespace: str, value: bytes, **kwargs) -> dict:
        if not isinstance(value, bytes):
            raise ArtifactValidationError("value must be bytes")
        kwargs.setdefault("expected_size", len(value))
        return self.put_stream(namespace, io.BytesIO(value), **kwargs)

    def describe(self, namespace: str, artifact_id: str, *,
                 verify=False, touch=False) -> dict:
        """Describe content metadata without disclosing any owner claim."""
        namespace = _namespace(namespace)
        artifact_id = _artifact_id(artifact_id)
        with self._lock:
            record = self._record_locked(namespace, artifact_id)
            if verify:
                self._verify_record_locked(namespace, record)
            if touch:
                self._touch_locked(record, self._clock())
                self._save_state_locked()
            return self._public_record(namespace, record)

    def has_owner(self, namespace: str, artifact_id: str, owner: dict) -> bool:
        """Whether this artifact has exactly ``owner`` as a persisted claim.

        Missing artifacts and non-matching claims are intentionally the same
        boolean answer. Invalid selectors still raise so a caller cannot turn
        malformed authorization input into an accidental wildcard.
        """
        namespace = _namespace(namespace)
        artifact_id = _artifact_id(artifact_id)
        owner = self._owner(owner)
        if not owner:
            raise ArtifactValidationError("owner claim must not be empty")
        with self._lock:
            record = self._namespace_records_locked(namespace).get(artifact_id)
            if not isinstance(record, dict):
                return False
            return self._has_owner_locked(record, owner)

    def release_owner(self, namespace: str, artifact_id: str,
                      owner: dict) -> bool:
        """Retire one exact authorization claim, idempotently.

        Job acknowledgement/reaping uses this together with release() so
        content deduplication cannot accumulate one permanent owner row per
        completed job.  It never removes bytes or another owner's claim.
        """
        namespace = _namespace(namespace)
        artifact_id = _artifact_id(artifact_id)
        owner = self._owner(owner)
        if not owner:
            raise ArtifactValidationError("owner claim must not be empty")
        with self._lock:
            record = self._namespace_records_locked(namespace).get(artifact_id)
            if not isinstance(record, dict):
                return False
            owners = record.get("owners")
            if not isinstance(owners, list):
                raise ArtifactCorrupt("artifact owner claims are invalid")
            try:
                owners.remove(owner)
            except ValueError:
                return False
            try:
                self._save_state_locked()
            except Exception:
                owners.append(owner)
                raise
            return True

    def describe_for_owner(self, namespace: str, artifact_id: str,
                           owner: dict, *, verify=False,
                           touch=False, protection_id=None,
                           protection_kind=None) -> dict:
        """Return metadata carrying only one exact persisted owner claim.

        A missing content object and a wrong claim share ArtifactNotFound so
        network callers cannot use this API as an ownership/inventory oracle.
        """
        namespace = _namespace(namespace)
        artifact_id = _artifact_id(artifact_id)
        owner = self._owner(owner)
        if not owner:
            raise ArtifactValidationError("owner claim must not be empty")
        if (protection_id is None) != (protection_kind is None):
            raise ArtifactValidationError(
                "protection_id and protection_kind must be supplied together")
        if protection_id is not None:
            protection_id = _bounded_text(
                protection_id, "protection_id", 256)
            if protection_kind not in _PROTECTION_KINDS:
                raise ArtifactValidationError("unsupported protection kind")
        with self._lock:
            record = self._namespace_records_locked(namespace).get(artifact_id)
            if (not isinstance(record, dict)
                    or not self._has_owner_locked(record, owner)):
                raise ArtifactNotFound(artifact_id)
            if verify:
                self._verify_record_locked(namespace, record)
            changed = self._ensure_protection_locked(
                record, protection_id, protection_kind, self._clock())
            if touch:
                self._touch_locked(record, self._clock())
                changed = True
            if changed:
                self._save_state_locked()
            return self._public_record(namespace, record, owner)

    def verify(self, namespace: str, artifact_id: str) -> dict:
        return self.describe(namespace, artifact_id, verify=True)

    def protect(self, namespace: str, artifact_id: str, reference_id: str,
                *, kind: str) -> int:
        namespace = _namespace(namespace)
        artifact_id = _artifact_id(artifact_id)
        reference_id = _bounded_text(reference_id, "reference_id", 256)
        if kind not in _PROTECTION_KINDS:
            raise ArtifactValidationError("unsupported protection kind")
        with self._lock:
            record = self._record_locked(namespace, artifact_id)
            protections = record.setdefault("protections", {})
            current = protections.get(reference_id)
            if current and current.get("kind") != kind:
                raise ArtifactValidationError(
                    "reference_id already has a different protection kind")
            if not current:
                current = {"kind": kind, "count": 0,
                           "created_at": self._clock()}
                protections[reference_id] = current
            current["count"] += 1
            self._save_state_locked()
            return current["count"]

    @staticmethod
    def _ensure_protection_locked(record: dict, reference_id: Optional[str],
                                  kind: Optional[str], now: float) -> bool:
        """Install one idempotent durable protection on a locked record."""
        if reference_id is None:
            return False
        protections = record.setdefault("protections", {})
        current = protections.get(reference_id)
        if current is not None:
            if current.get("kind") != kind:
                raise ArtifactValidationError(
                    "reference_id already has a different protection kind")
            return False
        protections[reference_id] = {
            "kind": kind, "count": 1, "created_at": now}
        return True

    def ensure_protected(self, namespace: str, artifact_id: str,
                         reference_id: str, *, kind: str) -> bool:
        """Idempotently protect a record without incrementing a refcount."""
        namespace = _namespace(namespace)
        artifact_id = _artifact_id(artifact_id)
        reference_id = _bounded_text(reference_id, "reference_id", 256)
        if kind not in _PROTECTION_KINDS:
            raise ArtifactValidationError("unsupported protection kind")
        with self._lock:
            record = self._record_locked(namespace, artifact_id)
            changed = self._ensure_protection_locked(
                record, reference_id, kind, self._clock())
            if changed:
                self._save_state_locked()
            return changed

    def release(self, namespace: str, artifact_id: str,
                reference_id: str, *, expected_kind=None) -> int:
        namespace = _namespace(namespace)
        artifact_id = _artifact_id(artifact_id)
        reference_id = _bounded_text(reference_id, "reference_id", 256)
        if expected_kind is not None and expected_kind not in _PROTECTION_KINDS:
            raise ArtifactValidationError("unsupported protection kind")
        with self._lock:
            record = self._record_locked(namespace, artifact_id)
            protections = record.setdefault("protections", {})
            current = protections.get(reference_id)
            if not current:
                return 0
            if (expected_kind is not None
                    and current.get("kind") != expected_kind):
                raise ArtifactValidationError(
                    "reference_id has a different protection kind")
            count = current.get("count", 1) - 1
            if count <= 0:
                del protections[reference_id]
                count = 0
            else:
                current["count"] = count
            self._save_state_locked()
            return count

    def issue_download_capability(self, namespace: str, artifact_id: str,
                                  *, audience: str,
                                  ttl_s: float = DEFAULT_TOKEN_TTL_S) -> dict:
        namespace = _namespace(namespace)
        artifact_id = _artifact_id(artifact_id)
        audience = _bounded_text(audience, "audience", 256)
        ttl_s = _finite_number(ttl_s, "ttl_s")
        if ttl_s <= 0 or ttl_s > MAX_TOKEN_TTL_S:
            raise ArtifactValidationError(
                f"token ttl_s must be in (0, {MAX_TOKEN_TTL_S}]")
        # Verify content before granting a capability, but with the lock
        # released so a large artifact's hash does not stall the daemon.
        self._verify_record_unlocked(namespace, artifact_id)
        with self._lock:
            record = self._record_locked(namespace, artifact_id)
            now = self._clock()
            token = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(
                b"=").decode("ascii")
            token_digest = hashlib.sha256(token.encode("ascii")).hexdigest()
            expires_at = now + ttl_s
            # Prune expired capabilities before adding one, so a long-lived
            # daemon reclaims index space instead of only growing toward the
            # ceiling (see the cleanup-scheduling fix).
            self._prune_capabilities_locked(now)
            self._state["capabilities"][token_digest] = {
                "convoy_id": namespace,
                "artifact_id": artifact_id,
                "purpose": "download",
                "audience": audience,
                "created_at": now,
                "expires_at": expires_at,
                "used_at": None,
            }
            try:
                self._save_state_locked()
            except Exception:
                # Roll back the in-memory insert exactly as open_download does.
                # Without this, a rejected grant (e.g. hitting the metadata
                # ceiling) leaves the record in memory while disk is unchanged,
                # so the in-memory state is permanently over the limit and
                # EVERY later save fails -- a self-amplifying wedge that only a
                # restart clears (review 2026-08-02).
                self._state["capabilities"].pop(token_digest, None)
                raise
            return {"token": token, "expires_at": expires_at,
                    "artifact_id": artifact_id, "convoy_id": namespace,
                    "audience": audience, "purpose": "download"}

    def _capability_locked(self, token: str, namespace: str,
                           artifact_id: str, audience: str, now: float) -> tuple:
        if not isinstance(token, str) or not _CAPABILITY.fullmatch(token):
            raise ArtifactUnauthorized("malformed capability")
        token_digest = hashlib.sha256(token.encode("ascii")).hexdigest()
        cap = self._state["capabilities"].get(token_digest)
        if not isinstance(cap, dict):
            raise ArtifactUnauthorized("unknown capability")
        if cap.get("used_at") is not None:
            raise ArtifactTokenReplay("capability was already consumed")
        expiry = cap.get("expires_at")
        if not isinstance(expiry, (int, float)) or expiry <= now:
            raise ArtifactTokenExpired("capability has expired")
        if (cap.get("purpose") != "download"
                or cap.get("convoy_id") != namespace
                or cap.get("artifact_id") != artifact_id
                or cap.get("audience") != audience):
            raise ArtifactUnauthorized("capability scope does not match request")
        return token_digest, cap

    def describe_download(self, namespace: str, artifact_id: str, *,
                          token: str, audience: str) -> dict:
        """Describe a download only after its capability scope is valid.

        The capability is not consumed.  HTTP uses this to validate a Range
        before ``open_download`` marks the one-shot token used, without
        creating an artifact-existence oracle for callers holding no grant.
        """
        namespace = _namespace(namespace)
        artifact_id = _artifact_id(artifact_id)
        audience = _bounded_text(audience, "audience", 256)
        with self._lock:
            self._capability_locked(
                token, namespace, artifact_id, audience, self._clock())
            record = self._record_locked(namespace, artifact_id)
            return self._public_record(namespace, record)

    def open_download(self, namespace: str, artifact_id: str, *, token: str,
                      audience: str, offset: int = 0,
                      length: Optional[int] = None,
                      chunk_size: int = DEFAULT_STREAM_CHUNK_BYTES
                      ) -> ArtifactDownload:
        namespace = _namespace(namespace)
        artifact_id = _artifact_id(artifact_id)
        audience = _bounded_text(audience, "audience", 256)
        offset = self._nonnegative_int(offset, "offset")
        if length is not None:
            length = self._nonnegative_int(length, "length")
        chunk_size = self._positive_int(chunk_size, "chunk_size")
        if chunk_size > MAX_STREAM_CHUNK_BYTES:
            raise ArtifactValidationError("chunk_size exceeds streaming bound")
        # Fail fast on an invalid/expired/replayed token BEFORE an expensive
        # whole-file hash, without consuming the one-shot capability.
        with self._lock:
            self._capability_locked(
                token, namespace, artifact_id, audience, self._clock())
        # Verify content with the lock released (see _verify_record_unlocked):
        # a large download must never stall the whole store/daemon.  The
        # capability is consumed only in the final locked block below, so a
        # corrupt artifact never burns it.
        self._verify_record_unlocked(namespace, artifact_id)
        with self._lock:
            now = self._clock()
            token_digest, cap = self._capability_locked(
                token, namespace, artifact_id, audience, now)
            record = self._record_locked(namespace, artifact_id)
            path = self._path_for_record_locked(namespace, record)
            total_size = record["size"]
            if offset > total_size:
                raise ArtifactValidationError("range offset exceeds artifact size")
            if length is None:
                length = total_size - offset
            if length > total_size - offset:
                raise ArtifactValidationError("range exceeds artifact size")
            try:
                file_obj = open(path, "rb")
            except OSError as exc:
                raise ArtifactCorrupt("managed content cannot be opened") from exc
            protection_id = "download:" + secrets.token_hex(16)
            record.setdefault("protections", {})[protection_id] = {
                "kind": "active_transfer", "count": 1, "created_at": now}
            cap["used_at"] = now
            self._touch_locked(record, now)
            try:
                self._save_state_locked()
            except Exception:
                record["protections"].pop(protection_id, None)
                cap["used_at"] = None
                file_obj.close()
                raise
            return ArtifactDownload(
                self, file_obj, namespace, artifact_id, protection_id,
                offset=offset, length=length, chunk_size=chunk_size,
                metadata=dict(record))

    def _finish_download(self, namespace: str, artifact_id: str,
                         protection_id: str) -> None:
        with self._lock:
            try:
                record = self._record_locked(namespace, artifact_id)
            except ArtifactNotFound:
                return
            record.setdefault("protections", {}).pop(protection_id, None)
            self._touch_locked(record, self._clock())
            self._save_state_locked()

    def delete(self, namespace: str, artifact_id: str, *, force=False) -> int:
        namespace = _namespace(namespace)
        artifact_id = _artifact_id(artifact_id)
        with self._lock:
            size = self._delete_locked(namespace, artifact_id, force=force)
            self._save_state_locked()
            return size

    def _prune_capabilities_locked(self, now: float) -> bool:
        changed = False
        for key, cap in list(self._state["capabilities"].items()):
            expiry = cap.get("expires_at") if isinstance(cap, dict) else None
            if not isinstance(expiry, (int, float)) or expiry <= now:
                del self._state["capabilities"][key]
                changed = True
        return changed

    def cleanup(self) -> dict:
        """Remove expired, then over-quota/free-floor LRU entries.

        Protected entries are never removed.  A store can consequently remain
        above quota while work is active; new uploads fail closed until those
        references are released and cleanup succeeds.
        """

        with self._lock:
            now = self._clock()
            self._remove_stale_partials_locked(now)
            self._prune_capabilities_locked(now)
            self._expire_stale_transfer_protections_locked(now)
            removed = []
            for _, _, _, namespace, artifact_id in list(
                    self._eviction_candidates_locked(now)):
                record = self._namespace_records_locked(namespace).get(artifact_id)
                if not record or record.get("expires_at", 0) > now:
                    continue
                size = self._delete_locked(namespace, artifact_id,
                                           force=True, now=now)
                removed.append({"convoy_id": namespace,
                                "artifact_id": artifact_id, "size": size,
                                "cause": "ttl"})

            while (self._usage_locked() > self.quota_bytes
                   or self._free_bytes_locked() < self.free_space_floor_bytes):
                candidates = self._eviction_candidates_locked(now)
                if not candidates:
                    break
                _, _, _, namespace, artifact_id = candidates[0]
                size = self._delete_locked(namespace, artifact_id,
                                           force=True, now=now)
                removed.append({"convoy_id": namespace,
                                "artifact_id": artifact_id, "size": size,
                                "cause": "quota_or_free_space"})
            self._save_state_locked()
            return {"removed": removed,
                    "removed_bytes": sum(row["size"] for row in removed),
                    **self.status()}

    def set_quota_mb(self, quota_mb: int) -> dict:
        with self._lock:
            self.quota_bytes = self._quota_bytes(quota_mb)
        return self.cleanup()

    def status(self) -> dict:
        with self._lock:
            usage = self._usage_locked()
            artifact_count = sum(
                len(records) for records in self._state["artifacts"].values()
                if isinstance(records, dict))
            protected_count = 0
            now = self._clock()
            for namespace, records in self._state["artifacts"].items():
                if not isinstance(records, dict):
                    continue
                protected_count += sum(
                    1 for record in records.values()
                    if isinstance(record, dict)
                    and self._is_protected_locked(namespace, record, now))
            return {
                "quota_bytes": self.quota_bytes,
                "usage_bytes": usage,
                "artifact_count": artifact_count,
                "protected_count": protected_count,
                "free_space_floor_bytes": self.free_space_floor_bytes,
                "storage_enabled": self.quota_bytes > 0,
            }

    @staticmethod
    def _safe_project_artifact_dir(project_root: str) -> str:
        if not isinstance(project_root, str) or not project_root:
            raise ArtifactValidationError("project_root must be a path")
        project_root = os.path.realpath(os.path.abspath(project_root))
        if not os.path.isdir(project_root):
            raise ArtifactValidationError("project_root is not a directory")
        current = project_root
        for segment in (".embody", "convoy", "artifacts"):
            current = os.path.join(current, segment)
            if os.path.lexists(current):
                mode = os.lstat(current).st_mode
                if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
                    raise ArtifactValidationError(
                        "project artifact directory contains a symlink/non-directory")
            else:
                os.mkdir(current, mode=0o700)
        expected = os.path.join(project_root, ".embody", "convoy", "artifacts")
        if os.path.realpath(current) != os.path.realpath(expected):
            raise ArtifactValidationError("project artifact directory escaped project")
        return current

    def export_to_project(self, project_root: str, namespace: str,
                          artifact_id: str, *, filename: Optional[str] = None,
                          overwrite=False) -> dict:
        """Explicitly save verified bytes under ``.embody/convoy/artifacts``.

        The returned ``saved_path`` is explicitly a path on *this* host.  It
        is never included in remote artifact references.  No capability,
        cache metadata, owner detail, or other secret is written beside it.
        """

        namespace = _namespace(namespace)
        artifact_id = _artifact_id(artifact_id)
        if not isinstance(overwrite, bool):
            raise ArtifactValidationError("overwrite must be boolean")
        with self._lock:
            record = self._record_locked(namespace, artifact_id)
            source = self._verify_record_locked(namespace, record)
            chosen = filename or record.get("filename_hint")
            if not chosen:
                extension = mimetypes.guess_extension(record["mime_type"]) or ""
                chosen = artifact_id + extension
            chosen = _filename(chosen)
            destination_dir = self._safe_project_artifact_dir(project_root)
            destination = os.path.join(destination_dir, chosen)
            if os.path.lexists(destination):
                if os.path.islink(destination) or not _regular_file(destination):
                    raise ArtifactValidationError(
                        "export destination is not a regular file")
                if not overwrite:
                    raise ArtifactExists(chosen)
            temp = os.path.join(
                destination_dir, ".export-%s.tmp" % secrets.token_hex(16))
            digest = hashlib.sha256()
            total = 0
            try:
                with open(source, "rb") as reader, open(temp, "xb") as writer:
                    while True:
                        block = reader.read(DEFAULT_STREAM_CHUNK_BYTES)
                        if not block:
                            break
                        writer.write(block)
                        digest.update(block)
                        total += len(block)
                    writer.flush()
                    os.fsync(writer.fileno())
                if digest.hexdigest() != record["sha256"] or total != record["size"]:
                    raise ArtifactCorrupt("artifact changed during project export")
                os.replace(temp, destination)
                temp = ""
                self._fsync_directory(destination_dir)
                self._touch_locked(record, self._clock())
                self._save_state_locked()
                return {
                    "kind": "local_project_artifact",
                    "saved_path": os.path.abspath(destination),
                    "artifact_id": artifact_id,
                    "convoy_id": namespace,
                    "sha256": record["sha256"],
                    "size": record["size"],
                    "mime_type": record["mime_type"],
                }
            finally:
                if temp:
                    try:
                        os.unlink(temp)
                    except FileNotFoundError:
                        pass


__all__ = [
    "ArtifactStore", "ArtifactDownload", "ArtifactError",
    "ArtifactValidationError", "ArtifactNotFound", "ArtifactQuotaExceeded",
    "ArtifactCorrupt", "ArtifactUnauthorized", "ArtifactTokenExpired",
    "ArtifactTokenReplay", "ArtifactProtected", "ArtifactExists",
    "ArtifactOwnerClaimsExceeded",
    "default_cache_root", "DEFAULT_QUOTA_MB", "DEFAULT_TTL_S",
    "DEFAULT_TOKEN_TTL_S", "DEFAULT_FREE_SPACE_FLOOR_BYTES",
    "MAX_OWNER_CLAIMS",
]
