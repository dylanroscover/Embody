"""A-16/A-17 controllers and leases as a proper reader/writer lock.
Pure policy, injected clock."""

import pytest

import convoy_controllers as cc

C1 = "controller-1"
C2 = "controller-2"
C3 = "controller-3"
NODE = "node-b"


def reg():
    return cc.LeaseRegistry(ttl_s=100.0, controller_timeout_s=60.0,
                            max_ttl_s=3600.0)


# -- controllers ----------------------------------------------------

def test_a_heartbeating_controller_is_alive():
    r = reg()
    r.heartbeat(C1, now=1000.0)
    assert r.controller_alive(C1, now=1055.0) is True
    assert r.controller_alive(C1, now=1061.0) is False


def test_unknown_controller_is_not_alive():
    assert reg().controller_alive("ghost", now=1.0) is False


def test_empty_controller_id_refused():
    with pytest.raises(cc.LeaseError):
        reg().heartbeat("", now=1.0)


def test_live_controller_view_includes_label_selection_and_no_lease():
    r = reg()
    r.heartbeat(C1, now=1000.0, label="Cursor session",
                selected_node_id=NODE)
    assert r.live_controllers(now=1001.0) == [{
        "controller_id": C1,
        "label": "Cursor session",
        "last_seen": 1000.0,
        "selected_node_id": NODE,
        "leases": [],
        "node_ids": [],
    }]


def test_heartbeat_can_explicitly_clear_a_previous_selection():
    r = reg()
    r.heartbeat(C1, now=1000.0, selected_node_id=NODE)
    r.heartbeat(C1, now=1001.0, clear_selection=True)
    assert r.live_controllers(now=1002.0)[0]["selected_node_id"] is None


def test_live_controller_view_groups_its_leases():
    r = reg()
    r.heartbeat(C1, now=1000.0, selected_node_id=NODE)
    r.acquire(NODE, C1, cc.LEASE_EXCLUSIVE, now=1001.0)
    row = r.live_controllers(now=1002.0)[0]
    assert row["controller_id"] == C1
    assert row["selected_node_id"] == NODE
    assert row["node_ids"] == [NODE]
    assert row["leases"][0]["mode"] == cc.LEASE_EXCLUSIVE


def test_malformed_selected_node_is_refused_without_mutation():
    r = reg()
    with pytest.raises(cc.LeaseError) as error:
        r.heartbeat(C1, now=1.0, selected_node_id="")
    assert error.value.reason == "malformed_node"
    assert r.live_controllers(now=1.0) == []


# -- exclusive leases -----------------------------------------------

def test_exclusive_blocks_another_controller():
    r = reg()
    r.acquire(NODE, C1, cc.LEASE_EXCLUSIVE, now=1000.0)
    with pytest.raises(cc.LeaseError) as e:
        r.acquire(NODE, C2, cc.LEASE_EXCLUSIVE, now=1001.0)
    assert e.value.reason == "node_leased" and e.value.holder == C1


def test_holder_renews_its_own_exclusive():
    r = reg()
    r.acquire(NODE, C1, cc.LEASE_EXCLUSIVE, now=1000.0)
    lease = r.acquire(NODE, C1, cc.LEASE_EXCLUSIVE, now=1050.0)
    assert lease["expires"] == 1150.0


def test_expired_exclusive_frees_the_node():
    r = reg()
    r.acquire(NODE, C1, cc.LEASE_EXCLUSIVE, now=1000.0)
    got = r.acquire(NODE, C2, cc.LEASE_EXCLUSIVE, now=1200.0)
    assert got["controller_id"] == C2


def test_a_dead_controller_does_not_hold_to_the_ttl():
    r = reg()
    r.acquire(NODE, C1, cc.LEASE_EXCLUSIVE, now=1000.0)
    # 70s: TTL(100) not passed, but C1 heartbeat(60) has -> C2 may take.
    got = r.acquire(NODE, C2, cc.LEASE_EXCLUSIVE, now=1070.0)
    assert got["controller_id"] == C2


# -- SHARED leases: the blocker the panel found ---------------------

def test_many_shared_holders_coexist_and_are_all_counted():
    """The single-slot bug: a second shared acquire must NOT evict the
    first. Three readers must show as three live holders."""
    r = reg()
    r.acquire(NODE, C1, cc.LEASE_SHARED, now=1000.0)
    r.acquire(NODE, C2, cc.LEASE_SHARED, now=1000.0)
    r.acquire(NODE, C3, cc.LEASE_SHARED, now=1000.0)
    holders = {l["controller_id"] for l in r.live_leases(now=1000.0)}
    assert holders == {C1, C2, C3}, "all shared readers must be tracked"


def test_a_shared_holder_can_still_release_after_others_join():
    """The single-slot bug made C1.release a no-op once C2 joined."""
    r = reg()
    r.acquire(NODE, C1, cc.LEASE_SHARED, now=1000.0)
    r.acquire(NODE, C2, cc.LEASE_SHARED, now=1000.0)
    assert r.release(NODE, C1) is True
    remaining = {l["controller_id"] for l in r.live_leases(now=1000.0)}
    assert remaining == {C2}, "C2's lease must survive C1 releasing"


def test_exclusive_cannot_be_taken_over_a_live_shared_lease():
    r = reg()
    r.acquire(NODE, C1, cc.LEASE_SHARED, now=1000.0)
    with pytest.raises(cc.LeaseError) as e:
        r.acquire(NODE, C2, cc.LEASE_EXCLUSIVE, now=1001.0)
    assert e.value.reason == "node_leased"


# -- authorize: the read/write rule ---------------------------------

def test_reads_are_always_allowed():
    r = reg()
    r.acquire(NODE, C1, cc.LEASE_EXCLUSIVE, now=1000.0)
    assert r.authorize(NODE, C2, is_mutating=False, now=1001.0) is None


def test_exclusive_holder_may_mutate():
    r = reg()
    r.acquire(NODE, C1, cc.LEASE_EXCLUSIVE, now=1000.0)
    assert r.authorize(NODE, C1, is_mutating=True, now=1001.0) is None


def test_a_SHARED_holder_may_NOT_mutate():
    """THE blocker: a read lease must never grant write rights."""
    r = reg()
    r.acquire(NODE, C1, cc.LEASE_SHARED, now=1000.0)
    with pytest.raises(cc.LeaseError) as e:
        r.authorize(NODE, C1, is_mutating=True, now=1001.0)
    assert e.value.reason == "shared_lease_no_mutation"


def test_mutation_refused_while_another_controller_holds_exclusive():
    r = reg()
    r.acquire(NODE, C1, cc.LEASE_EXCLUSIVE, now=1000.0)
    with pytest.raises(cc.LeaseError) as e:
        r.authorize(NODE, C2, is_mutating=True, now=1001.0)
    assert e.value.reason == "node_leased" and e.value.holder == C1


def test_no_mutation_granted_over_still_live_shared_readers():
    """The fail-open the panel proved: an exclusive/mutation must NOT be
    granted while a shared reader is still active."""
    r = reg()
    r.acquire(NODE, C1, cc.LEASE_SHARED, now=1000.0)   # C1 reading
    r.acquire(NODE, C2, cc.LEASE_SHARED, now=1000.0)   # C2 reading
    # C2's slot is fine; a THIRD controller must not be able to mutate
    # while C1 is still actively reading.
    with pytest.raises(cc.LeaseError) as e:
        r.authorize(NODE, C3, is_mutating=True, now=1001.0)
    assert e.value.reason == "node_leased"


def test_unleased_node_allows_mutation():
    assert reg().authorize(NODE, C1, is_mutating=True, now=1.0) is None


def test_mutation_allowed_once_all_shared_readers_expire():
    r = reg()
    r.acquire(NODE, C1, cc.LEASE_SHARED, now=1000.0)
    assert r.authorize(NODE, C2, is_mutating=True, now=1200.0) is None


# -- implicit per-operation writer claims ---------------------------

def test_operation_claim_blocks_another_controller_but_not_its_owner():
    r = reg()
    claim = r.claim_operation(NODE, C1, "cj_one", now=1000.0)
    assert claim["implicit"] is True
    assert r.authorize(NODE, C1, is_mutating=True, now=1001.0) is None
    with pytest.raises(cc.LeaseError) as error:
        r.authorize(NODE, C2, is_mutating=True, now=1001.0)
    assert error.value.reason == "node_leased"
    assert error.value.holder == C1


def test_same_controller_can_hold_multiple_exact_operation_claims():
    r = reg()
    r.claim_operation(NODE, C1, "cj_one", now=1000.0)
    r.claim_operation(NODE, C1, "cj_two", now=1001.0)
    rows = [row for row in r.live_leases(now=1002.0)
            if row.get("implicit")]
    assert {row["delivery_id"] for row in rows} == {"cj_one", "cj_two"}
    assert r.release_operation(NODE, "cj_one") is True
    with pytest.raises(cc.LeaseError):
        r.authorize(NODE, C2, is_mutating=True, now=1002.0)
    assert r.release_operation(NODE, "cj_two") is True
    assert r.authorize(NODE, C2, is_mutating=True, now=1002.0) is None


def test_operation_release_never_releases_an_explicit_lease():
    r = reg()
    r.acquire(NODE, C1, cc.LEASE_EXCLUSIVE, now=1000.0)
    r.claim_operation(NODE, C1, "cj_one", now=1001.0)
    assert r.release_operation(NODE, "cj_one") is True
    with pytest.raises(cc.LeaseError):
        r.authorize(NODE, C2, is_mutating=True, now=1002.0)


def test_shared_reader_blocks_an_implicit_writer_claim():
    r = reg()
    r.acquire(NODE, C1, cc.LEASE_SHARED, now=1000.0)
    with pytest.raises(cc.LeaseError) as error:
        r.claim_operation(NODE, C2, "cj_one", now=1001.0)
    assert error.value.reason == "node_leased"
    assert error.value.holder == C1


def test_dead_controller_reaps_its_operation_claim():
    r = reg()
    r.claim_operation(NODE, C1, "cj_one", now=1000.0)
    assert r.reap(now=1061.0) == 1
    assert r.live_leases(now=1061.0) == []


def test_releasing_controller_drops_every_operation_claim():
    r = reg()
    r.claim_operation(NODE, C1, "cj_one", now=1000.0)
    r.claim_operation(NODE, C1, "cj_two", now=1000.0)
    assert r.release_controller(C1) == 2
    assert r.live_leases(now=1001.0) == []


# -- release, reap, ttl clamp ---------------------------------------

def test_release_frees_an_exclusive_node():
    r = reg()
    r.acquire(NODE, C1, cc.LEASE_EXCLUSIVE, now=1000.0)
    assert r.release(NODE, C1) is True
    got = r.acquire(NODE, C2, cc.LEASE_EXCLUSIVE, now=1001.0)
    assert got["controller_id"] == C2


def test_releasing_a_lease_you_do_not_hold_is_a_noop():
    r = reg()
    r.acquire(NODE, C1, cc.LEASE_EXCLUSIVE, now=1000.0)
    assert r.release(NODE, C2) is False


def test_reap_counts_every_dead_holder_slot():
    r = reg()
    r.acquire(NODE, C1, cc.LEASE_SHARED, now=1000.0)
    r.acquire(NODE, C2, cc.LEASE_SHARED, now=1000.0)
    assert len(r.live_leases(now=1000.0)) == 2
    reaped = r.reap(now=1200.0)
    assert reaped == 2
    assert r.live_leases(now=1200.0) == []


def test_ttl_is_clamped():
    r = cc.LeaseRegistry(ttl_s=100.0, max_ttl_s=500.0)
    lease = r.acquire(NODE, C1, cc.LEASE_EXCLUSIVE, now=0.0, ttl_s=999999.0)
    assert lease["expires"] == 500.0, "an over-long TTL is clamped (A-17)"


def test_bad_mode_refused():
    with pytest.raises(cc.LeaseError):
        reg().acquire(NODE, C1, "sideways", now=1.0)
