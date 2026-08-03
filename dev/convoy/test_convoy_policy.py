"""Host-private Convoy policy, CAS, and confirmation contracts."""

import json
import os
import stat
import sys
import threading

import pytest

import convoy_policy as policy


NODE_A = "a" * 32
NODE_B = "b" * 32


class Clock:
    def __init__(self):
        self.wall = 1_900_000_000.0
        self.mono = 10.0

    def time(self):
        return self.wall

    def monotonic(self):
        return self.mono

    def advance(self, seconds):
        self.wall += seconds
        self.mono += seconds


def make_store(tmp_path, clock=None, **kwargs):
    clock = clock or Clock()
    return policy.PolicyStore(
        tmp_path, now=clock.time, monotonic=clock.monotonic, **kwargs)


def confirm_full_shell(store):
    challenge = store.begin_enable_full_shell(
        expected_generation=store.generation)
    return store.confirm_enable(
        challenge["challenge_id"], challenge["confirmation"],
        expected_generation=challenge["generation"])


def confirm_td_python(store, node_id=NODE_A):
    challenge = store.begin_enable_td_python(
        node_id, expected_generation=store.generation)
    return store.confirm_enable(
        challenge["challenge_id"], challenge["confirmation"],
        expected_generation=challenge["generation"])


def test_defaults_are_closed_and_persisted(tmp_path):
    store = make_store(tmp_path)
    assert store.snapshot() == {
        "version": 1,
        "generation": 0,
        "allow_full_shell": False,
        "artifact_quota_mb": 1024,
        "td_python_node_ids": [],
    }
    assert store.allow_td_python(NODE_A) is False
    assert (tmp_path / policy.POLICY_FILE).is_file()


def test_restart_persists_approvals_quota_and_generation(tmp_path):
    store = make_store(tmp_path)
    confirm_full_shell(store)
    confirm_td_python(store)
    store.set_artifact_quota_mb(0, expected_generation=store.generation)

    restored = make_store(tmp_path)
    assert restored.allow_full_shell() is True
    assert restored.allow_td_python(NODE_A) is True
    assert restored.artifact_quota_mb() == 0
    assert restored.generation == 3


def test_challenges_do_not_survive_restart(tmp_path):
    store = make_store(tmp_path)
    challenge = store.begin_enable_full_shell(expected_generation=0)
    restored = make_store(tmp_path)
    with pytest.raises(policy.ChallengeNotFound):
        restored.confirm_enable(
            challenge["challenge_id"], challenge["confirmation"],
            expected_generation=0)
    assert restored.allow_full_shell() is False


def test_node_approval_is_bound_only_to_exact_stable_node_id(tmp_path):
    store = make_store(tmp_path)
    confirm_td_python(store, NODE_A)
    assert store.allow_td_python(NODE_A) is True
    assert store.allow_td_python(NODE_B) is False

    # A moved/copied launch profile gets a new node_id and no inherited grant.
    confirm_td_python(store, NODE_B)
    store.disable_td_python(NODE_A)
    assert store.allow_td_python(NODE_A) is False
    assert store.allow_td_python(NODE_B) is True


def test_there_is_no_direct_dangerous_enable_api(tmp_path):
    store = make_store(tmp_path)
    assert not hasattr(store, "enable_full_shell")
    assert not hasattr(store, "enable_td_python")


def test_wrong_confirmation_is_consumed_and_cannot_be_replayed(tmp_path):
    store = make_store(tmp_path)
    challenge = store.begin_enable_full_shell(expected_generation=0)
    with pytest.raises(policy.ChallengeInvalid):
        store.confirm_enable(
            challenge["challenge_id"], "ENABLE FULL SHELL WRONG",
            expected_generation=0)
    with pytest.raises(policy.ChallengeNotFound):
        store.confirm_enable(
            challenge["challenge_id"], challenge["confirmation"],
            expected_generation=0)
    assert store.allow_full_shell() is False


def test_successful_challenge_is_one_time(tmp_path):
    store = make_store(tmp_path)
    challenge = store.begin_enable_td_python(NODE_A, expected_generation=0)
    store.confirm_enable(
        challenge["challenge_id"], challenge["confirmation"],
        expected_generation=0)
    with pytest.raises(policy.ChallengeNotFound):
        store.confirm_enable(
            challenge["challenge_id"], challenge["confirmation"],
            expected_generation=0)


def test_decline_consumes_challenge_without_mutating_generation(tmp_path):
    store = make_store(tmp_path)
    challenge = store.begin_enable_full_shell(expected_generation=0)
    result = store.decline_challenge(challenge["challenge_id"])
    assert result["generation"] == 0
    assert result["allow_full_shell"] is False
    with pytest.raises(policy.ChallengeNotFound):
        store.decline_challenge(challenge["challenge_id"])


def test_expired_challenge_is_consumed(tmp_path):
    clock = Clock()
    store = make_store(tmp_path, clock, challenge_ttl_s=5)
    challenge = store.begin_enable_full_shell(expected_generation=0)
    clock.advance(5)
    with pytest.raises(policy.ChallengeExpired):
        store.confirm_enable(
            challenge["challenge_id"], challenge["confirmation"],
            expected_generation=0)
    with pytest.raises(policy.ChallengeNotFound):
        store.confirm_enable(
            challenge["challenge_id"], challenge["confirmation"],
            expected_generation=0)


def test_any_policy_change_makes_an_outstanding_challenge_stale(tmp_path):
    store = make_store(tmp_path)
    challenge = store.begin_enable_full_shell(expected_generation=0)
    store.set_artifact_quota_mb(2048, expected_generation=0)
    with pytest.raises(policy.ChallengeStale) as caught:
        store.confirm_enable(
            challenge["challenge_id"], challenge["confirmation"],
            expected_generation=0)
    assert caught.value.current_generation == 1
    assert store.allow_full_shell() is False


def test_caller_generation_is_bound_to_challenge(tmp_path):
    store = make_store(tmp_path)
    challenge = store.begin_enable_full_shell(expected_generation=0)
    with pytest.raises(policy.PolicyConflict):
        store.confirm_enable(
            challenge["challenge_id"], challenge["confirmation"],
            expected_generation=1)
    assert store.allow_full_shell() is False


def test_disables_are_immediate_idempotent_and_not_cas_blocked(tmp_path):
    store = make_store(tmp_path)
    confirm_full_shell(store)
    confirm_td_python(store)
    generation = store.generation
    store.disable_full_shell()
    store.disable_td_python(NODE_A)
    assert store.generation == generation + 2
    assert store.allow_full_shell() is False
    assert store.allow_td_python(NODE_A) is False
    # Repeating a safe action is a no-op, including no needless disk/gen churn.
    store.disable_full_shell()
    store.disable_td_python(NODE_A)
    assert store.generation == generation + 2


@pytest.mark.parametrize("quota", [0, 1, 1024, 1024 * 1024])
def test_quota_accepts_documented_range(tmp_path, quota):
    store = make_store(tmp_path)
    result = store.set_artifact_quota_mb(quota, expected_generation=0)
    assert result["artifact_quota_mb"] == quota


@pytest.mark.parametrize("quota", [-1, 1024 * 1024 + 1, True, 1.5, "1024"])
def test_quota_refuses_out_of_range_or_coercive_values(tmp_path, quota):
    store = make_store(tmp_path)
    with pytest.raises(policy.PolicyValidationError):
        store.set_artifact_quota_mb(quota, expected_generation=0)


def test_quota_is_local_cas_without_a_danger_challenge(tmp_path):
    store = make_store(tmp_path)
    store.set_artifact_quota_mb(512, expected_generation=0)
    with pytest.raises(policy.PolicyConflict) as caught:
        store.set_artifact_quota_mb(256, expected_generation=0)
    assert caught.value.current == 1
    assert store.artifact_quota_mb() == 512


def test_threaded_cas_allows_exactly_one_writer(tmp_path):
    store = make_store(tmp_path)
    barrier = threading.Barrier(12)
    winners = []
    conflicts = []

    def write(value):
        barrier.wait()
        try:
            store.set_artifact_quota_mb(value, expected_generation=0)
            winners.append(value)
        except policy.PolicyConflict:
            conflicts.append(value)

    threads = [threading.Thread(target=write, args=(2000 + index,))
               for index in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(winners) == 1
    assert len(conflicts) == 11
    assert store.artifact_quota_mb() == winners[0]
    assert store.generation == 1


@pytest.mark.parametrize("contents", [
    "not json", "[]", "{}",
    '{"version":1,"generation":0,"allow_full_shell":true,'
    '"artifact_quota_mb":1024,"td_python_node_ids":["bad"]}',
])
def test_corrupt_state_refuses_to_start_instead_of_resetting(tmp_path, contents):
    (tmp_path / policy.POLICY_FILE).write_text(contents, encoding="utf-8")
    with pytest.raises(policy.PolicyCorrupt):
        make_store(tmp_path)
    assert (tmp_path / policy.POLICY_FILE).read_text(encoding="utf-8") == contents


def test_newer_state_refuses_to_start(tmp_path):
    state = {
        "version": policy.POLICY_VERSION + 1,
        "generation": 99,
        "allow_full_shell": True,
        "artifact_quota_mb": 1024,
        "td_python_node_ids": [NODE_A],
    }
    (tmp_path / policy.POLICY_FILE).write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(policy.PolicyTooNew):
        make_store(tmp_path)


def test_unreadable_state_fails_closed(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    real_open = open

    def denied(path, *args, **kwargs):
        if os.fspath(path) == store.path:
            raise PermissionError("denied by test")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", denied)
    with pytest.raises(policy.PolicyUnreadable):
        policy.PolicyStore(tmp_path)


@pytest.mark.skipif(sys.platform == "win32",
                    reason="POSIX mode bits; Windows uses inherited user ACLs")
def test_policy_file_is_owner_only_on_posix_including_macos(tmp_path):
    store = make_store(tmp_path)
    mode = stat.S_IMODE(os.stat(store.path).st_mode)
    assert mode == 0o600


def test_permission_hardening_is_best_effort_on_windows_and_macos(
        tmp_path, monkeypatch):
    calls = []
    real_chmod = policy.os.chmod

    def observe(path, mode):
        calls.append((os.fspath(path), mode))
        # Do not depend on the current runner's ACL/mode implementation.
        return real_chmod(path, mode)

    monkeypatch.setattr(policy.os, "chmod", observe)
    store = make_store(tmp_path)
    assert calls == [(store.path, 0o600)]
    assert not list(tmp_path.glob("*.tmp"))


def test_platform_chmod_limit_does_not_break_atomic_private_writer(
        tmp_path, monkeypatch):
    """Windows may reject POSIX mode hardening; its profile ACL still rules."""
    monkeypatch.setattr(
        policy.os, "chmod",
        lambda path, mode: (_ for _ in ()).throw(PermissionError("ACL only")))
    store = make_store(tmp_path)
    store.set_artifact_quota_mb(512, expected_generation=0)
    assert make_store(tmp_path).artifact_quota_mb() == 512


def test_failed_atomic_write_does_not_advance_memory_or_generation(
        tmp_path, monkeypatch):
    store = make_store(tmp_path)

    def fail(path, payload):
        raise PermissionError("disk denied")

    monkeypatch.setattr(policy.convoy_platform, "_write_private", fail)
    with pytest.raises(policy.PolicyUnreadable):
        store.set_artifact_quota_mb(512, expected_generation=0)
    assert store.generation == 0
    assert store.artifact_quota_mb() == 1024


def test_snapshot_is_detached_from_authoritative_state(tmp_path):
    store = make_store(tmp_path)
    snapshot = store.snapshot()
    snapshot["allow_full_shell"] = True
    snapshot["td_python_node_ids"].append(NODE_A)
    assert store.allow_full_shell() is False
    assert store.allow_td_python(NODE_A) is False


@pytest.mark.parametrize("node_id", [None, "", "A" * 32, "a" * 31, True])
def test_node_ids_are_strict(node_id, tmp_path):
    store = make_store(tmp_path)
    with pytest.raises(policy.PolicyValidationError):
        store.allow_td_python(node_id)
