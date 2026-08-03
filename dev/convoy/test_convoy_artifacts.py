"""Security, quota, streaming, and portability contracts for artifacts."""

import hashlib
import io
import json
import os
import threading

import pytest

import convoy_artifacts as ca


NS_A = "convoy-alpha"
NS_B = "convoy-beta"


class Clock:
    def __init__(self, value=1_000_000.0):
        self.value = float(value)

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(tmp_path, clock):
    return ca.ArtifactStore(
        str(tmp_path / "private-cache"), quota_mb=4,
        free_space_floor_bytes=0, max_artifact_bytes=2 * 1024 * 1024,
        clock=clock)


def put(store, data=b"artifact", namespace=NS_A, **kwargs):
    kwargs.setdefault("mime_type", "application/octet-stream")
    kwargs.setdefault("filename_hint", "result.bin")
    return store.put_bytes(namespace, data, **kwargs)


def download(store, ref, namespace=NS_A, audience="peer-host"):
    cap = store.issue_download_capability(
        namespace, ref["artifact_id"], audience=audience)
    with store.open_download(
            namespace, ref["artifact_id"], token=cap["token"],
            audience=audience) as lease:
        return b"".join(lease), cap


def test_default_cache_paths_are_per_user_and_not_project_paths():
    win = ca.default_cache_root(
        "win32", {"LOCALAPPDATA": "C:/Users/A/AppData/Local"}, "C:/Users/A")
    mac = ca.default_cache_root("darwin", {}, "/Users/a")
    linux = ca.default_cache_root("linux", {"XDG_CACHE_HOME": "/cache"}, "/h")
    assert win.replace("\\", "/").endswith(
        "AppData/Local/Embody/Convoy/cache/artifacts")
    assert mac == "/Users/a/Library/Caches/Embody/Convoy/artifacts"
    assert linux == "/cache/Embody/Convoy/artifacts"
    assert ".embody" not in win.lower()


def test_reference_is_content_addressed_and_never_discloses_local_path(store):
    data = b"secret screenshot bytes"
    ref = put(store, data, mime_type="image/png", filename_hint="frame.png",
              owner={"job_id": "job-1", "host_id": "host-2"})
    digest = hashlib.sha256(data).hexdigest()
    assert ref["artifact_id"] == "art_" + digest
    assert ref["sha256"] == digest
    assert ref["size"] == len(data)
    assert ref["job_id"] == "job-1"
    assert ref["kind"] == "convoy_artifact"
    assert not any("path" in key or "cache" in key for key in ref)
    assert str(store.cache_root) not in json.dumps(ref)


def test_raw_namespace_and_filename_never_become_managed_path_components(store):
    namespace = "Show One / Stage A"
    ref = put(store, b"x", namespace=namespace, filename_hint="safe.bin")
    target = store._content_path(namespace, ref["sha256"])
    assert namespace not in target
    assert "safe.bin" not in target
    assert os.path.basename(target) == ref["sha256"]


@pytest.mark.parametrize("filename", ["../x", "a/b", "a\\b", "C:x", ".", ".."])
def test_filename_hint_rejects_cross_platform_traversal(store, filename):
    with pytest.raises(ca.ArtifactValidationError):
        put(store, filename_hint=filename)


def test_zero_quota_means_disabled_not_unlimited(tmp_path, clock):
    disabled = ca.ArtifactStore(
        str(tmp_path / "cache"), quota_mb=0, free_space_floor_bytes=0,
        clock=clock)
    assert disabled.status()["storage_enabled"] is False
    with pytest.raises(ca.ArtifactQuotaExceeded) as caught:
        put(disabled)
    assert "disabled" in str(caught.value)


def test_quota_evicts_least_recently_used_unpinned(tmp_path, clock):
    cache = ca.ArtifactStore(
        str(tmp_path / "cache"), quota_mb=1, free_space_floor_bytes=0,
        max_artifact_bytes=1024 * 1024, clock=clock)
    first = put(cache, b"a" * 600_000)
    clock.advance(1)
    second = put(cache, b"b" * 600_000)
    with pytest.raises(ca.ArtifactNotFound):
        cache.describe(NS_A, first["artifact_id"])
    assert cache.verify(NS_A, second["artifact_id"])["size"] == 600_000
    assert cache.status()["usage_bytes"] <= 1024 * 1024


def test_access_updates_lru_choice(tmp_path, clock):
    cache = ca.ArtifactStore(
        str(tmp_path / "cache"), quota_mb=1, free_space_floor_bytes=0,
        max_artifact_bytes=1024 * 1024, clock=clock)
    first = put(cache, b"a" * 400_000)
    clock.advance(1)
    second = put(cache, b"b" * 400_000)
    clock.advance(1)
    cache.describe(NS_A, first["artifact_id"], touch=True)
    clock.advance(1)
    third = put(cache, b"c" * 400_000)
    assert cache.describe(NS_A, first["artifact_id"])
    assert cache.describe(NS_A, third["artifact_id"])
    with pytest.raises(ca.ArtifactNotFound):
        cache.describe(NS_A, second["artifact_id"])


def test_running_and_unacknowledged_job_references_are_refcounted(store):
    ref = put(store, b"protected")
    artifact_id = ref["artifact_id"]
    assert store.protect(NS_A, artifact_id, "job:one", kind="running_job") == 1
    assert store.protect(NS_A, artifact_id, "job:one", kind="running_job") == 2
    assert store.release(NS_A, artifact_id, "job:one") == 1
    with pytest.raises(ca.ArtifactProtected):
        store.delete(NS_A, artifact_id)
    assert store.release(NS_A, artifact_id, "job:one") == 0
    assert store.protect(
        NS_A, artifact_id, "result:one", kind="unacknowledged_job") == 1
    with pytest.raises(ca.ArtifactProtected):
        store.delete(NS_A, artifact_id)
    store.release(NS_A, artifact_id, "result:one")
    assert store.delete(NS_A, artifact_id) == len(b"protected")


def test_put_atomically_installs_idempotent_job_protection(store):
    kwargs = {
        "protection_id": "job:cj_123:result",
        "protection_kind": "unacknowledged_job",
    }
    first = put(store, b"terminal result", **kwargs)
    # A resolution retry may deduplicate the same bytes for the same job. It
    # must not increment an invisible refcount that one acknowledgement can
    # never fully release.
    second = put(store, b"terminal result", **kwargs)
    assert second["artifact_id"] == first["artifact_id"]
    with pytest.raises(ca.ArtifactProtected):
        store.delete(NS_A, first["artifact_id"])
    assert store.release(
        NS_A, first["artifact_id"], "job:cj_123:result") == 0
    assert store.delete(NS_A, first["artifact_id"]) == len(b"terminal result")


def test_put_requires_complete_valid_protection_scope(store):
    with pytest.raises(ca.ArtifactValidationError):
        put(store, protection_id="job:cj_123:result")
    with pytest.raises(ca.ArtifactValidationError):
        put(store, protection_kind="unacknowledged_job")
    with pytest.raises(ca.ArtifactValidationError):
        put(store, protection_id="job:cj_123:result",
            protection_kind="made_up")


def test_abandoned_active_transfer_protection_expires_on_cleanup(
        store, clock):
    ref = put(
        store, b"handoff", protection_id="relay:" + "a" * 32,
        protection_kind="active_transfer")
    with pytest.raises(ca.ArtifactProtected):
        store.delete(NS_A, ref["artifact_id"])
    clock.advance(ca.ACTIVE_TRANSFER_STALE_S + 1)
    store.cleanup()
    assert store.delete(NS_A, ref["artifact_id"]) == len(b"handoff")


def test_release_can_require_the_exact_protection_kind(store):
    ref = put(store, b"kind-bound")
    store.ensure_protected(
        NS_A, ref["artifact_id"], "job:cj_1:result",
        kind="unacknowledged_job")
    with pytest.raises(ca.ArtifactValidationError):
        store.release(
            NS_A, ref["artifact_id"], "job:cj_1:result",
            expected_kind="active_transfer")
    with pytest.raises(ca.ArtifactProtected):
        store.delete(NS_A, ref["artifact_id"])


def test_ensure_protected_is_idempotent(store):
    ref = put(store, b"relay copy")
    assert store.ensure_protected(
        NS_A, ref["artifact_id"], "relay:cj_123",
        kind="unacknowledged_job") is True
    assert store.ensure_protected(
        NS_A, ref["artifact_id"], "relay:cj_123",
        kind="unacknowledged_job") is False
    assert store.release(NS_A, ref["artifact_id"], "relay:cj_123") == 0


def test_all_protected_candidates_make_new_upload_fail_closed(tmp_path, clock):
    cache = ca.ArtifactStore(
        str(tmp_path / "cache"), quota_mb=1, free_space_floor_bytes=0,
        max_artifact_bytes=1024 * 1024, clock=clock)
    ref = put(cache, b"a" * 700_000)
    cache.protect(NS_A, ref["artifact_id"], "job", kind="running_job")
    with pytest.raises(ca.ArtifactQuotaExceeded):
        put(cache, b"b" * 700_000)
    assert cache.verify(NS_A, ref["artifact_id"])


def test_active_download_is_protected_until_closed(store):
    ref = put(store, b"abcdefgh")
    cap = store.issue_download_capability(
        NS_A, ref["artifact_id"], audience="peer")
    lease = store.open_download(
        NS_A, ref["artifact_id"], token=cap["token"], audience="peer",
        chunk_size=2)
    with pytest.raises(ca.ArtifactProtected):
        store.delete(NS_A, ref["artifact_id"])
    assert next(lease) == b"ab"
    lease.close()
    assert store.delete(NS_A, ref["artifact_id"]) == 8


def test_ttl_cleanup_removes_expired_but_not_pinned(store, clock):
    old = put(store, b"old", ttl_s=10)
    pinned = put(store, b"pin", ttl_s=10)
    store.protect(NS_A, pinned["artifact_id"], "hold", kind="pin")
    clock.advance(11)
    report = store.cleanup()
    assert any(row["artifact_id"] == old["artifact_id"]
               for row in report["removed"])
    with pytest.raises(ca.ArtifactNotFound):
        store.describe(NS_A, old["artifact_id"])
    assert store.describe(NS_A, pinned["artifact_id"])


def test_live_unspent_token_protects_artifact_until_token_expiry(store, clock):
    ref = put(store, b"token", ttl_s=5)
    store.issue_download_capability(
        NS_A, ref["artifact_id"], audience="peer", ttl_s=20)
    clock.advance(6)
    store.cleanup()
    assert store.describe(NS_A, ref["artifact_id"])
    clock.advance(15)
    store.cleanup()
    with pytest.raises(ca.ArtifactNotFound):
        store.describe(NS_A, ref["artifact_id"])


def test_free_space_floor_evicts_before_reservation(tmp_path, clock, monkeypatch):
    cache = ca.ArtifactStore(
        str(tmp_path / "cache"), quota_mb=4, free_space_floor_bytes=300_000,
        max_artifact_bytes=2 * 1024 * 1024, clock=clock)
    first = put(cache, b"a" * 500_000)
    # Model a 1.2 MB disk: deleting the old object restores enough free space.
    monkeypatch.setattr(
        cache, "_free_bytes_locked", lambda: 1_200_000 - cache._usage_locked())
    second = put(cache, b"b" * 600_000)
    with pytest.raises(ca.ArtifactNotFound):
        cache.describe(NS_A, first["artifact_id"])
    assert cache.describe(NS_A, second["artifact_id"])


def test_corrupt_content_is_never_described_as_verified_or_downloaded(store):
    ref = put(store, b"good")
    target = store._content_path(NS_A, ref["sha256"])
    with open(target, "wb") as handle:
        handle.write(b"evil")
    with pytest.raises(ca.ArtifactCorrupt):
        store.verify(NS_A, ref["artifact_id"])
    # Capability issuance itself verifies content, so a corrupt file never
    # receives a fresh bearer capability.
    with pytest.raises(ca.ArtifactCorrupt):
        store.issue_download_capability(
            NS_A, ref["artifact_id"], audience="peer")


def test_hash_mismatch_removes_partial_and_never_indexes(store):
    expected = hashlib.sha256(b"expected").hexdigest()
    with pytest.raises(ca.ArtifactCorrupt):
        store.put_stream(
            NS_A, io.BytesIO(b"different"), expected_size=len(b"different"),
            expected_sha256=expected)
    assert store.status()["artifact_count"] == 0
    assert not list(store._partial_paths_locked())


def test_failed_upload_after_eviction_leaves_restartable_index(tmp_path, clock):
    root = str(tmp_path / "cache")
    cache = ca.ArtifactStore(
        root, quota_mb=1, free_space_floor_bytes=0,
        max_artifact_bytes=1024 * 1024, clock=clock)
    evicted = put(cache, b"a" * 600_000)
    with pytest.raises(ca.ArtifactValidationError):
        cache.put_stream(NS_A, io.BytesIO(b"too short"),
                         expected_size=600_000)
    with pytest.raises(ca.ArtifactNotFound):
        cache.describe(NS_A, evicted["artifact_id"])
    reopened = ca.ArtifactStore(
        root, quota_mb=1, free_space_floor_bytes=0,
        max_artifact_bytes=1024 * 1024, clock=clock)
    assert reopened.status()["artifact_count"] == 0


class RecordingReader:
    def __init__(self, data):
        self.data = data
        self.offset = 0
        self.requests = []

    def read(self, amount):
        self.requests.append(amount)
        result = self.data[self.offset:self.offset + amount]
        self.offset += len(result)
        return result


def test_stream_upload_and_download_requests_are_bounded(store):
    data = b"x" * 200_000
    reader = RecordingReader(data)
    ref = store.put_stream(
        NS_A, reader, expected_size=len(data), chunk_size=8192)
    assert max(reader.requests) <= 8192
    cap = store.issue_download_capability(
        NS_A, ref["artifact_id"], audience="peer")
    lease = store.open_download(
        NS_A, ref["artifact_id"], token=cap["token"], audience="peer",
        chunk_size=4096)
    blocks = list(lease)
    assert b"".join(blocks) == data
    assert max(map(len, blocks)) <= 4096


class OverReturningReader:
    def read(self, amount):
        return b"x" * (amount + 1)


def test_malicious_stream_cannot_exceed_requested_bound(store):
    with pytest.raises(ca.ArtifactValidationError):
        store.put_stream(NS_A, OverReturningReader(), expected_size=10,
                         chunk_size=4)
    assert not list(store._partial_paths_locked())


@pytest.mark.parametrize("actual, declared", [(b"short", 20), (b"long", 2)])
def test_content_length_mismatch_fails_without_partial(store, actual, declared):
    with pytest.raises(ca.ArtifactValidationError):
        store.put_stream(NS_A, io.BytesIO(actual), expected_size=declared)
    assert not list(store._partial_paths_locked())
    assert store.status()["artifact_count"] == 0


def test_concurrent_same_content_deduplicates_atomically(store):
    data = b"same" * 25_000
    digest = hashlib.sha256(data).hexdigest()
    barrier = threading.Barrier(8)
    results = []
    errors = []

    def writer():
        try:
            barrier.wait()
            results.append(store.put_stream(
                NS_A, io.BytesIO(data), expected_size=len(data),
                expected_sha256=digest))
        except Exception as exc:  # pragma: no cover - asserted empty below
            errors.append(exc)

    threads = [threading.Thread(target=writer) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert not errors
    assert len(results) == 8
    assert len({row["artifact_id"] for row in results}) == 1
    assert store.status()["artifact_count"] == 1
    assert store.status()["usage_bytes"] == len(data)
    assert download(store, results[0])[0] == data


def test_same_content_keeps_exact_bounded_owner_claims_across_restart(
        tmp_path, clock):
    root = str(tmp_path / "cache")
    cache = ca.ArtifactStore(
        root, quota_mb=4, free_space_floor_bytes=0, clock=clock)
    owner_a = {
        "host_id": "host-a", "node_id": "node-a",
        "controller_id": "peer:host-a:ctl", "job_id": "job-a",
    }
    owner_b = {
        "host_id": "host-b", "node_id": "node-a",
        "controller_id": "peer:host-b:ctl", "job_id": "job-b",
    }
    first = put(cache, b"identical", owner=owner_a)
    second = put(cache, b"identical", owner=owner_b)
    assert first["artifact_id"] == second["artifact_id"]
    assert first["host_id"] == "host-a"
    assert second["host_id"] == "host-b"  # just-claimed projection
    assert cache.has_owner(NS_A, first["artifact_id"], owner_a)
    assert cache.has_owner(NS_A, first["artifact_id"], owner_b)
    assert not cache.has_owner(
        NS_A, first["artifact_id"], {**owner_b, "job_id": "wrong"})
    assert cache.describe_for_owner(
        NS_A, first["artifact_id"], owner_a)["job_id"] == "job-a"
    generic = cache.describe(NS_A, first["artifact_id"])
    assert not any(field in generic for field in ca._OWNER_FIELDS)
    with pytest.raises(ca.ArtifactNotFound):
        cache.describe_for_owner(
            NS_A, first["artifact_id"], {**owner_a, "job_id": "wrong"})

    reopened = ca.ArtifactStore(
        root, quota_mb=4, free_space_floor_bytes=0, clock=clock)
    assert reopened.has_owner(NS_A, first["artifact_id"], owner_a)
    assert reopened.has_owner(NS_A, first["artifact_id"], owner_b)
    persisted = json.loads(open(reopened.state_path, encoding="utf-8").read())
    record = persisted["artifacts"][NS_A][first["artifact_id"]]
    assert persisted["version"] == ca.STORE_VERSION
    assert record["owners"] == [owner_a, owner_b]
    assert "owner" not in record


def test_owner_claim_limit_retires_oldest_unprotected_claim(store, monkeypatch):
    monkeypatch.setattr(ca, "MAX_OWNER_CLAIMS", 2)
    owners = [{"host_id": "host-%d" % index} for index in range(3)]
    ref = put(store, b"bounded-claims", owner=owners[0])
    put(store, b"bounded-claims", owner=owners[1])
    put(store, b"bounded-claims", owner=owners[2])
    assert not store.has_owner(NS_A, ref["artifact_id"], owners[0])
    assert store.has_owner(NS_A, ref["artifact_id"], owners[1])
    assert store.has_owner(NS_A, ref["artifact_id"], owners[2])
    assert not list(store._partial_paths_locked())


def test_owner_claim_limit_keeps_every_unacknowledged_job_claim(
        store, monkeypatch):
    monkeypatch.setattr(ca, "MAX_OWNER_CLAIMS", 2)
    owners = [{"host_id": "host", "job_id": "job-%d" % index}
              for index in range(3)]
    first = put(
        store, b"active-claims", owner=owners[0],
        protection_id="job:job-0:result",
        protection_kind="unacknowledged_job")
    put(store, b"active-claims", owner=owners[1],
        protection_id="job:job-1:result",
        protection_kind="unacknowledged_job")
    with pytest.raises(ca.ArtifactOwnerClaimsExceeded):
        put(store, b"active-claims", owner=owners[2])
    assert store.has_owner(NS_A, first["artifact_id"], owners[0])
    assert store.has_owner(NS_A, first["artifact_id"], owners[1])
    assert not store.has_owner(NS_A, first["artifact_id"], owners[2])


def test_legacy_single_owner_index_migrates_once_and_validates(tmp_path, clock):
    root = str(tmp_path / "cache")
    cache = ca.ArtifactStore(
        root, quota_mb=4, free_space_floor_bytes=0, clock=clock)
    owner = {"host_id": "legacy-host", "node_id": "legacy-node"}
    ref = put(cache, b"legacy", owner=owner)
    state = json.loads(open(cache.state_path, encoding="utf-8").read())
    record = state["artifacts"][NS_A][ref["artifact_id"]]
    record["owner"] = record.pop("owners")[0]
    state["version"] = ca.LEGACY_STORE_VERSION
    with open(cache.state_path, "w", encoding="utf-8") as handle:
        json.dump(state, handle, sort_keys=True, separators=(",", ":"))

    reopened = ca.ArtifactStore(
        root, quota_mb=4, free_space_floor_bytes=0, clock=clock)
    assert reopened.has_owner(NS_A, ref["artifact_id"], owner)
    migrated = json.loads(open(reopened.state_path, encoding="utf-8").read())
    migrated_record = migrated["artifacts"][NS_A][ref["artifact_id"]]
    assert migrated["version"] == ca.STORE_VERSION
    assert migrated_record["owners"] == [owner]
    assert "owner" not in migrated_record


def test_same_hash_is_logically_isolated_between_namespaces(store):
    first = put(store, b"shared", namespace=NS_A)
    second = put(store, b"shared", namespace=NS_B)
    assert first["artifact_id"] == second["artifact_id"]
    assert store.status()["artifact_count"] == 2
    assert store._content_path(NS_A, first["sha256"]) != store._content_path(
        NS_B, second["sha256"])


def test_capability_is_namespace_and_audience_bound_one_shot(store):
    first = put(store, b"shared", namespace=NS_A)
    put(store, b"shared", namespace=NS_B)
    cap = store.issue_download_capability(
        NS_A, first["artifact_id"], audience="peer-a")
    with pytest.raises(ca.ArtifactUnauthorized):
        store.open_download(
            NS_B, first["artifact_id"], token=cap["token"],
            audience="peer-a")
    with pytest.raises(ca.ArtifactUnauthorized):
        store.open_download(
            NS_A, first["artifact_id"], token=cap["token"],
            audience="peer-b")
    # Scope failures do not burn the legitimate request.
    with store.open_download(
            NS_A, first["artifact_id"], token=cap["token"],
            audience="peer-a") as lease:
        assert b"".join(lease) == b"shared"
    with pytest.raises(ca.ArtifactTokenReplay):
        store.open_download(
            NS_A, first["artifact_id"], token=cap["token"],
            audience="peer-a")


def test_capability_expiry_fails_closed(store, clock):
    ref = put(store)
    cap = store.issue_download_capability(
        NS_A, ref["artifact_id"], audience="peer", ttl_s=2)
    clock.advance(2)
    with pytest.raises(ca.ArtifactTokenExpired):
        store.open_download(
            NS_A, ref["artifact_id"], token=cap["token"], audience="peer")


def test_corruption_does_not_consume_otherwise_valid_capability(store):
    original = b"verified before every transfer"
    ref = put(store, original)
    cap = store.issue_download_capability(
        NS_A, ref["artifact_id"], audience="peer")
    target = store._content_path(NS_A, ref["sha256"])
    with open(target, "wb") as handle:
        handle.write(b"tampered")
    with pytest.raises(ca.ArtifactCorrupt):
        store.open_download(
            NS_A, ref["artifact_id"], token=cap["token"], audience="peer")
    # Repair only simulates recovery tooling.  It proves validation happened
    # before the one-shot capability was marked used.
    with open(target, "wb") as handle:
        handle.write(original)
    with store.open_download(
            NS_A, ref["artifact_id"], token=cap["token"],
            audience="peer") as lease:
        assert b"".join(lease) == original


def test_capability_replay_refusal_survives_store_restart(tmp_path, clock):
    root = str(tmp_path / "cache")
    first_store = ca.ArtifactStore(
        root, quota_mb=4, free_space_floor_bytes=0, clock=clock)
    ref = put(first_store, b"restart")
    cap = first_store.issue_download_capability(
        NS_A, ref["artifact_id"], audience="peer")
    with first_store.open_download(
            NS_A, ref["artifact_id"], token=cap["token"],
            audience="peer") as lease:
        assert b"".join(lease) == b"restart"
    reopened = ca.ArtifactStore(
        root, quota_mb=4, free_space_floor_bytes=0, clock=clock)
    with pytest.raises(ca.ArtifactTokenReplay):
        reopened.open_download(
            NS_A, ref["artifact_id"], token=cap["token"], audience="peer")


def test_raw_capability_is_never_written_to_index(store):
    ref = put(store)
    cap = store.issue_download_capability(
        NS_A, ref["artifact_id"], audience="peer")
    text = open(store.state_path, encoding="utf-8").read()
    assert cap["token"] not in text
    assert hashlib.sha256(cap["token"].encode("ascii")).hexdigest() in text


def test_range_download_supports_resume_without_unbounded_chunks(store):
    data = bytes(range(256)) * 4
    ref = put(store, data)
    cap = store.issue_download_capability(
        NS_A, ref["artifact_id"], audience="peer")
    lease = store.open_download(
        NS_A, ref["artifact_id"], token=cap["token"], audience="peer",
        offset=111, length=333, chunk_size=37)
    assert lease.offset == 111
    assert lease.length == 333
    assert lease.total_size == len(data)
    blocks = list(lease)
    assert b"".join(blocks) == data[111:444]
    assert max(map(len, blocks)) <= 37


def test_invalid_range_does_not_consume_capability(store):
    ref = put(store, b"abcdef")
    cap = store.issue_download_capability(
        NS_A, ref["artifact_id"], audience="peer")
    with pytest.raises(ca.ArtifactValidationError):
        store.open_download(
            NS_A, ref["artifact_id"], token=cap["token"], audience="peer",
            offset=5, length=2)
    with store.open_download(
            NS_A, ref["artifact_id"], token=cap["token"],
            audience="peer") as lease:
        assert b"".join(lease) == b"abcdef"


def test_explicit_project_export_is_atomic_and_outside_quota(store, tmp_path):
    ref = put(store, b"saved content", filename_hint="capture.bin")
    project = tmp_path / "project"
    project.mkdir()
    result = store.export_to_project(
        str(project), NS_A, ref["artifact_id"])
    expected = project / ".embody" / "convoy" / "artifacts" / "capture.bin"
    assert result["kind"] == "local_project_artifact"
    assert result["saved_path"] == str(expected.resolve())
    assert expected.read_bytes() == b"saved content"
    assert store.status()["usage_bytes"] == len(b"saved content")
    files = [path.name for path in expected.parent.iterdir()]
    assert files == ["capture.bin"]  # no token, metadata, or temp sidecar


def test_project_export_requires_explicit_overwrite(store, tmp_path):
    ref = put(store, b"new", filename_hint="result.bin")
    project = tmp_path / "project"
    target = project / ".embody" / "convoy" / "artifacts"
    target.mkdir(parents=True)
    (target / "result.bin").write_bytes(b"old")
    with pytest.raises(ca.ArtifactExists):
        store.export_to_project(str(project), NS_A, ref["artifact_id"])
    assert (target / "result.bin").read_bytes() == b"old"
    store.export_to_project(
        str(project), NS_A, ref["artifact_id"], overwrite=True)
    assert (target / "result.bin").read_bytes() == b"new"


def test_project_export_filename_cannot_escape(store, tmp_path):
    ref = put(store)
    project = tmp_path / "project"
    project.mkdir()
    with pytest.raises(ca.ArtifactValidationError):
        store.export_to_project(
            str(project), NS_A, ref["artifact_id"], filename="../../escape")
    assert not (tmp_path / "escape").exists()


def test_project_export_refuses_symlinked_artifact_directory(store, tmp_path):
    ref = put(store)
    project = tmp_path / "project"
    elsewhere = tmp_path / "elsewhere"
    (project / ".embody" / "convoy").mkdir(parents=True)
    elsewhere.mkdir()
    link = project / ".embody" / "convoy" / "artifacts"
    try:
        link.symlink_to(elsewhere, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable for this test user")
    with pytest.raises(ca.ArtifactValidationError):
        store.export_to_project(str(project), NS_A, ref["artifact_id"])
    assert not list(elsewhere.iterdir())


def test_managed_content_symlink_is_refused(tmp_path, clock):
    cache = ca.ArtifactStore(
        str(tmp_path / "cache"), quota_mb=4, free_space_floor_bytes=0,
        clock=clock)
    data = b"payload"
    digest = hashlib.sha256(data).hexdigest()
    target_dir = cache._prepare_content_directory(NS_A, digest)
    target = os.path.join(target_dir, digest)
    outside = tmp_path / "outside"
    outside.write_bytes(data)
    try:
        os.symlink(str(outside), target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable for this test user")
    with pytest.raises(ca.ArtifactCorrupt):
        cache.put_stream(
            NS_A, io.BytesIO(data), expected_size=len(data),
            expected_sha256=digest)
    assert outside.read_bytes() == data


def test_malformed_index_refuses_instead_of_silently_losing_records(tmp_path, clock):
    root = tmp_path / "cache"
    store = ca.ArtifactStore(
        str(root), quota_mb=4, free_space_floor_bytes=0, clock=clock)
    put(store)
    with open(store.state_path, "w", encoding="utf-8") as handle:
        handle.write("{truncated")
    with pytest.raises(ca.ArtifactValidationError):
        ca.ArtifactStore(
            str(root), quota_mb=4, free_space_floor_bytes=0, clock=clock)


def test_partial_files_count_against_managed_usage_and_stale_ones_recover(
        store, clock):
    uploads = os.path.join(store.data_root, "uploads")
    store._make_private_directory(uploads)
    partial = os.path.join(uploads, ".upload-crashed.part")
    with open(partial, "wb") as handle:
        handle.write(b"x" * 123)
    os.utime(partial, (clock() - ca.PARTIAL_STALE_S - 1,
                       clock() - ca.PARTIAL_STALE_S - 1))
    assert store.status()["usage_bytes"] == 123
    store.cleanup()
    assert not os.path.exists(partial)
    assert store.status()["usage_bytes"] == 0


def test_set_quota_zero_cleans_unpinned_but_preserves_active_work(store):
    removable = put(store, b"remove")
    protected = put(store, b"protect")
    store.protect(
        NS_A, protected["artifact_id"], "job", kind="unacknowledged_job")
    report = store.set_quota_mb(0)
    assert report["storage_enabled"] is False
    with pytest.raises(ca.ArtifactNotFound):
        store.describe(NS_A, removable["artifact_id"])
    assert store.describe(NS_A, protected["artifact_id"])
    with pytest.raises(ca.ArtifactQuotaExceeded):
        put(store, b"new")


def test_empty_artifact_round_trips(store):
    ref = put(store, b"")
    assert ref["size"] == 0
    assert download(store, ref)[0] == b""


def test_metadata_record_ceiling_evicts_lru_instead_of_refusing(
        store, clock, monkeypatch):
    # A metadata ceiling must be reclaimable exactly like the byte quota: a
    # host with byte-quota to spare must never silently stop storing artifacts
    # once it reaches MAX_ARTIFACT_RECORDS (review 2026-08-02).
    monkeypatch.setattr(ca, "MAX_ARTIFACT_RECORDS", 3)
    first = put(store, b"one")
    clock.advance(1)
    put(store, b"two")
    clock.advance(1)
    put(store, b"three")
    clock.advance(1)
    assert store.status()["artifact_count"] == 3
    fourth = put(store, b"four")
    assert store.describe(NS_A, fourth["artifact_id"])
    # The least-recently-used unprotected record is evicted, not the new write.
    with pytest.raises(ca.ArtifactNotFound):
        store.describe(NS_A, first["artifact_id"])
    assert store.status()["artifact_count"] == 3


def test_metadata_record_ceiling_evicts_expired_before_lru(
        store, clock, monkeypatch):
    monkeypatch.setattr(ca, "MAX_ARTIFACT_RECORDS", 2)
    short = put(store, b"short", ttl_s=10)
    clock.advance(1)
    fresh = put(store, b"fresh", ttl_s=10_000)
    clock.advance(20)  # 'short' is now expired; 'fresh' is not
    new = put(store, b"new")
    # The expired record is reclaimed first even though 'fresh' was accessed
    # earlier, mirroring the byte-quota eviction ordering.
    with pytest.raises(ca.ArtifactNotFound):
        store.describe(NS_A, short["artifact_id"])
    assert store.describe(NS_A, fresh["artifact_id"])
    assert store.describe(NS_A, new["artifact_id"])


def test_metadata_record_ceiling_fails_closed_when_all_protected(
        store, monkeypatch):
    monkeypatch.setattr(ca, "MAX_ARTIFACT_RECORDS", 2)
    kept_a = put(store, b"aa", protection_id="job:a:result",
                 protection_kind="unacknowledged_job")
    kept_b = put(store, b"bb", protection_id="job:b:result",
                 protection_kind="unacknowledged_job")
    # No unprotected record can be evicted, so a genuine ceiling still refuses.
    with pytest.raises(ca.ArtifactQuotaExceeded):
        put(store, b"cc")
    assert store.describe(NS_A, kept_a["artifact_id"])
    assert store.describe(NS_A, kept_b["artifact_id"])


def test_open_download_verifies_content_without_holding_the_store_lock(store):
    # Whole-file verification must run with the store lock RELEASED so a large
    # download cannot stall the daemon (callers also hold HostApp.lock).
    ref = put(store, b"x" * 8192)
    cap = store.issue_download_capability(
        NS_A, ref["artifact_id"], audience="peer")
    original = store._hash_file
    observed = {}

    def probing_hash(path):
        def probe():
            got = store._lock.acquire(blocking=True, timeout=2)
            observed["lock_free_during_hash"] = got
            if got:
                store._lock.release()
        thread = threading.Thread(target=probe)
        thread.start()
        thread.join(5)
        return original(path)

    store._hash_file = probing_hash
    with store.open_download(
            NS_A, ref["artifact_id"], token=cap["token"],
            audience="peer") as lease:
        assert b"".join(lease) == b"x" * 8192
    assert observed.get("lock_free_during_hash") is True


def test_issue_capability_verifies_without_holding_the_store_lock(store):
    ref = put(store, b"y" * 8192)
    original = store._hash_file
    observed = {}

    def probing_hash(path):
        def probe():
            got = store._lock.acquire(blocking=True, timeout=2)
            observed["lock_free_during_hash"] = got
            if got:
                store._lock.release()
        thread = threading.Thread(target=probe)
        thread.start()
        thread.join(5)
        return original(path)

    store._hash_file = probing_hash
    cap = store.issue_download_capability(
        NS_A, ref["artifact_id"], audience="peer")
    assert cap["token"]
    assert observed.get("lock_free_during_hash") is True
