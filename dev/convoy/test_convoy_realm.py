"""Automatic Convoy realm genesis and split-realm contracts."""

import json
import os
import stat

import pytest

import convoy_realm as realm


CV_A = "cv_" + "a" * 32
CV_B = "cv_" + "b" * 32
CV_C = "cv_" + "c" * 32


class Clock:
    def __init__(self, value=1_900_000_000.0):
        self.value = float(value)

    def time(self):
        return self.value

    def advance(self, seconds):
        self.value += float(seconds)


def make_store(tmp_path, clock=None, name="realm.json", **kwargs):
    clock = clock or Clock()
    return realm.RealmStore(
        tmp_path / name, now=clock.time, settle_delay_s=10.0, **kwargs)


def test_missing_record_is_unbound_and_listens_without_minting(tmp_path):
    store = make_store(tmp_path)

    assert store.snapshot() is None
    assert store.state is None
    assert store.generation is None
    assert store.reconcile(candidate_ids=[CV_A, CV_B]) is None
    assert not (tmp_path / "realm.json").exists()


def test_one_established_realm_is_adopted_without_local_candidate(tmp_path):
    store = make_store(tmp_path)

    adopted = store.reconcile(established_ids=[CV_B])

    assert adopted["state"] == realm.ESTABLISHED
    assert adopted["convoy_id"] == CV_B
    assert adopted["generation"] == 0


def test_legacy_no_state_is_safely_classified_by_integration_as_established(
        tmp_path):
    # Discovery's compatibility adapter passes an old/no-state announcement
    # through established_ids.  The realm state machine then takes the safe
    # path rather than treating it as a rewritable fresh candidate.
    store = make_store(tmp_path)

    result = store.reconcile(established_ids=[CV_A])

    assert result["state"] == realm.ESTABLISHED
    assert result["convoy_id"] == CV_A


def test_candidate_commits_only_after_its_persisted_settle_deadline(tmp_path):
    clock = Clock()
    store = make_store(tmp_path, clock)

    started = store.begin_candidate(CV_B)
    assert started == {
        "version": realm.REALM_VERSION,
        "generation": 0,
        "state": realm.CANDIDATE,
        "convoy_id": CV_B,
        "candidate_started_unix": clock.value,
        "settle_deadline_unix": clock.value + 10.0,
        "conflict_ids": [],
    }

    clock.advance(9.999)
    assert store.tick()["state"] == realm.CANDIDATE
    clock.advance(0.001)
    committed = store.tick()
    assert committed["state"] == realm.ESTABLISHED
    assert committed["convoy_id"] == CV_B
    assert committed["generation"] == 1


def test_simultaneous_candidates_converge_on_lexically_lowest_id(tmp_path):
    clock = Clock()
    first = make_store(tmp_path, clock, name="first.json")
    second = make_store(tmp_path, clock, name="second.json")
    first.begin_candidate(CV_B)
    second.begin_candidate(CV_A)

    # Each host hears the other's uncommitted signed announcement.
    assert first.reconcile(candidate_ids=[CV_A])["convoy_id"] == CV_A
    assert second.reconcile(candidate_ids=[CV_B])["convoy_id"] == CV_A

    clock.advance(10.0)
    assert first.tick()["convoy_id"] == CV_A
    assert second.tick()["convoy_id"] == CV_A
    assert first.state == second.state == realm.ESTABLISHED


def test_lowest_candidate_seen_does_not_rise_when_presence_expires(tmp_path):
    clock = Clock()
    store = make_store(tmp_path, clock)
    store.begin_candidate(CV_C)

    selected = store.reconcile(candidate_ids=[CV_A, CV_B])
    assert selected["convoy_id"] == CV_A
    clock.advance(5.0)

    # Candidate observations are presence snapshots, not ballots.  Forgetting
    # the lowest one here could split hosts whose announcements are staggered.
    assert store.reconcile(candidate_ids=[])["convoy_id"] == CV_A


def test_established_remote_immediately_beats_uncommitted_candidate(tmp_path):
    clock = Clock()
    store = make_store(tmp_path, clock)
    store.begin_candidate(CV_A)

    adopted = store.reconcile(
        candidate_ids=[CV_A], established_ids=[CV_C])

    assert adopted["state"] == realm.ESTABLISHED
    assert adopted["convoy_id"] == CV_C
    assert clock.value < 1_900_000_010.0


def test_late_lower_candidate_never_rewrites_established_realm(tmp_path):
    clock = Clock()
    store = make_store(tmp_path, clock)
    store.begin_candidate(CV_B)
    clock.advance(10.0)
    established = store.tick()

    unchanged = store.reconcile(candidate_ids=[CV_A])

    assert unchanged == established
    assert unchanged["state"] == realm.ESTABLISHED
    assert unchanged["convoy_id"] == CV_B


def test_delayed_candidate_adopts_existing_realm_even_when_it_is_lower(
        tmp_path):
    clock = Clock()
    established_host = make_store(tmp_path, clock, name="old.json")
    late_host = make_store(tmp_path, clock, name="late.json")
    established_host.begin_candidate(CV_B)
    clock.advance(10.0)
    established_host.tick()

    late_host.begin_candidate(CV_A)
    assert late_host.reconcile(established_ids=[CV_B]) == {
        "version": realm.REALM_VERSION,
        "generation": 1,
        "state": realm.ESTABLISHED,
        "convoy_id": CV_B,
        "candidate_started_unix": None,
        "settle_deadline_unix": None,
        "conflict_ids": [],
    }
    assert established_host.reconcile(candidate_ids=[CV_A])[
        "convoy_id"] == CV_B


def test_multiple_established_realms_fail_closed_while_unbound(tmp_path):
    store = make_store(tmp_path)

    result = store.reconcile(established_ids=[CV_B, CV_A, CV_B])

    assert result["state"] == realm.CONFLICT
    assert result["convoy_id"] is None
    assert result["conflict_ids"] == [CV_A, CV_B]


def test_multiple_established_realms_fail_closed_while_candidate(tmp_path):
    store = make_store(tmp_path)
    store.begin_candidate(CV_C)

    result = store.reconcile(established_ids=[CV_A, CV_B])

    assert result["state"] == realm.CONFLICT
    # The local candidate id is preserved as the conflict's authoritative id
    # so the state is recoverable (convoy_id=None refused every operation with
    # no recovery path -- review 2026-08-02). reset() restarts genesis.
    assert result["convoy_id"] == CV_C
    assert result["conflict_ids"] == sorted([CV_A, CV_B, CV_C])


def test_reset_clears_a_conflict_and_reenables_genesis(tmp_path):
    store = make_store(tmp_path)
    store.begin_candidate(CV_C)
    conflict = store.reconcile(established_ids=[CV_A, CV_B])
    assert conflict["state"] == realm.CONFLICT

    assert store.reset() is None
    assert store.snapshot() is None
    # A fresh store on the same path also reads unbound (the file is gone).
    assert make_store(tmp_path).snapshot() is None
    # Genesis can restart after recovery.
    again = store.begin_candidate(CV_C)
    assert again["state"] == realm.CANDIDATE


def test_conflict_ids_are_capped_and_the_record_stays_loadable(tmp_path):
    # A stream of crafted announcements must never grow realm.json past the
    # loader's ceiling (review 2026-08-02): the id set is bounded.
    store = make_store(tmp_path)
    store.reconcile(established_ids=[CV_A, CV_B])  # enter conflict, bound host
    for batch in range(40):
        ids = ["cv_%032x" % (batch * 16 + i) for i in range(16)]
        store.reconcile(established_ids=ids)
    snap = store.snapshot()
    assert snap["state"] == realm.CONFLICT
    assert len(snap["conflict_ids"]) <= realm.MAX_CONFLICT_IDS
    # The record its own writer produced must reload without error.
    reloaded = make_store(tmp_path).snapshot()
    assert reloaded["state"] == realm.CONFLICT
    assert len(reloaded["conflict_ids"]) <= realm.MAX_CONFLICT_IDS


def test_bound_host_preserves_its_realm_when_split_realm_conflict_occurs(
        tmp_path):
    store = make_store(tmp_path)
    store.reconcile(established_ids=[CV_B])

    result = store.reconcile(established_ids=[CV_A])

    assert result["state"] == realm.CONFLICT
    assert result["convoy_id"] == CV_B
    assert result["conflict_ids"] == [CV_A, CV_B]


def test_conflict_is_sticky_accumulates_realms_and_survives_restart(tmp_path):
    clock = Clock()
    store = make_store(tmp_path, clock)
    store.reconcile(established_ids=[CV_A, CV_B])
    result = store.reconcile(established_ids=[CV_C])
    assert result["conflict_ids"] == [CV_A, CV_B, CV_C]

    restored = make_store(tmp_path, clock)
    assert restored.snapshot() == result
    # Ordinary discovery expiry cannot silently merge or clear trust.
    assert restored.reconcile()["state"] == realm.CONFLICT
    assert restored.snapshot()["conflict_ids"] == [CV_A, CV_B, CV_C]


def test_candidate_and_original_deadline_survive_restart(tmp_path):
    clock = Clock()
    store = make_store(tmp_path, clock)
    store.begin_candidate(CV_C)
    store.reconcile(candidate_ids=[CV_A])
    saved = store.snapshot()

    clock.advance(7.0)
    # A constructor's new configured delay cannot silently extend a genesis
    # window already persisted by a prior process.
    restored = realm.RealmStore(
        tmp_path / realm.REALM_FILE,
        now=clock.time,
        settle_delay_s=100.0,
    )
    assert restored.snapshot() == saved
    clock.advance(3.0)
    result = restored.tick()
    assert result["state"] == realm.ESTABLISHED
    assert result["convoy_id"] == CV_A


def test_established_binding_and_generation_survive_restart(tmp_path):
    clock = Clock()
    store = make_store(tmp_path, clock)
    store.reconcile(established_ids=[CV_A])
    saved = store.snapshot()

    restored = make_store(tmp_path, clock)

    assert restored.snapshot() == saved
    assert restored.state == realm.ESTABLISHED
    assert restored.generation == 0


def test_idempotent_observation_does_not_rewrite_or_advance_generation(
        tmp_path):
    store = make_store(tmp_path)
    initial = store.reconcile(established_ids=[CV_A])
    before = os.stat(store.path).st_mtime_ns

    repeated = store.reconcile(
        candidate_ids=[CV_B], established_ids=[CV_A])

    assert repeated == initial
    assert repeated["generation"] == 0
    assert os.stat(store.path).st_mtime_ns == before


def test_failed_atomic_write_does_not_publish_transition_in_memory(
        tmp_path, monkeypatch):
    clock = Clock()
    store = make_store(tmp_path, clock)
    candidate = store.begin_candidate(CV_A)
    clock.advance(10.0)

    def fail_write(_path, _payload):
        raise OSError("disk unavailable")

    monkeypatch.setattr(realm.convoy_platform, "_write_private", fail_write)
    with pytest.raises(realm.RealmUnreadable):
        store.tick()
    assert store.snapshot() == candidate
    assert json.loads((tmp_path / realm.REALM_FILE).read_text(
        encoding="utf-8"))["state"] == realm.CANDIDATE


def test_record_is_owner_private_where_posix_mode_bits_apply(tmp_path):
    store = make_store(tmp_path)
    store.begin_candidate(CV_A)

    if os.name != "nt":
        assert stat.S_IMODE(os.stat(store.path).st_mode) == 0o600


@pytest.mark.parametrize("bad", ["", " cv_a", "cv_a ", "cv\nrealm"])
def test_malformed_ids_are_rejected_without_creating_state(tmp_path, bad):
    store = make_store(tmp_path)

    with pytest.raises(realm.RealmValidationError):
        store.begin_candidate(bad)
    assert store.snapshot() is None


def test_corrupt_or_newer_record_fails_closed(tmp_path):
    path = tmp_path / realm.REALM_FILE
    path.write_text("not json", encoding="utf-8")
    with pytest.raises(realm.RealmCorrupt):
        make_store(tmp_path)

    path.write_text(json.dumps({"version": realm.REALM_VERSION + 1}),
                    encoding="utf-8")
    with pytest.raises(realm.RealmTooNew):
        make_store(tmp_path)


def test_unhashable_persisted_state_and_unbounded_observations_fail_closed(
        tmp_path):
    path = tmp_path / realm.REALM_FILE
    path.write_text(json.dumps({
        "version": realm.REALM_VERSION,
        "generation": 0,
        "state": [],
        "convoy_id": CV_A,
        "candidate_started_unix": None,
        "settle_deadline_unix": None,
        "conflict_ids": [],
    }), encoding="utf-8")
    with pytest.raises(realm.RealmCorrupt):
        make_store(tmp_path)

    path.unlink()
    store = make_store(tmp_path)
    with pytest.raises(realm.RealmValidationError):
        store.reconcile(candidate_ids=(
            f"cv_{index:032x}"
            for index in range(realm.MAX_OBSERVED_REALMS + 1)
        ))
    assert store.snapshot() is None


def test_existing_established_or_conflict_state_cannot_restart_genesis(
        tmp_path):
    established = make_store(tmp_path, name="established.json")
    established.reconcile(established_ids=[CV_A])
    with pytest.raises(realm.RealmStateError):
        established.begin_candidate(CV_B)

    conflict = make_store(tmp_path, name="conflict.json")
    conflict.reconcile(established_ids=[CV_A, CV_B])
    with pytest.raises(realm.RealmStateError):
        conflict.begin_candidate(CV_C)
