"""Node identity semantics, pinned as tests. Pure, no I/O.

Two identifiers (Dylan's decision, 2026-07-31):
  runtime_id -- which RUN. Random every TD start, never stored.
  node_id    -- which saved .toe/COMP launch profile on which machine.
                Host-assigned, keyed on
                (project_root, node_discriminator, comp_path).
"""

import pytest

import convoy_identity as ci

HOST_A = "a" * 32
HOST_B = "b" * 32
ROOT = "/Work/Show"
ROOT2 = "/Work/Show-wt-fix"
CONVOY = "studio-convoy"
DISC_A = "nd_" + "1" * 32
DISC_B = "nd_" + "2" * 32


def test_minted_ids_are_128_bit_hex_and_unique():
    seen = {ci.mint_id() for _ in range(64)}
    assert len(seen) == 64
    assert all(ci.is_valid_id(i) for i in seen)


# -- what the two ids mean -------------------------------------------

def test_host_assigns_the_node_id_nothing_comes_from_the_project():
    d = ci.NodeDirectory(HOST_A)
    record = d.register(ROOT, "/Embody", CONVOY)
    assert ci.is_valid_id(record["node_id"])
    assert record["runtime_id"].startswith("rt_")


def test_reopening_the_same_project_is_the_same_node():
    """Restarting TD must not mint a new node -- permissions and saved
    references have to keep working."""
    d = ci.NodeDirectory(HOST_A)
    first = d.register(ROOT, "/Embody", CONVOY)
    second = d.register(ROOT, "/Embody", CONVOY)
    assert first["node_id"] == second["node_id"]
    assert len(d.nodes()) == 1


def test_a_restart_refreshes_the_runtime_id():
    """Same node, new RUN. This is what lets a request aimed at the
    previous run be refused instead of silently landing on a
    freshly-loaded session."""
    d = ci.NodeDirectory(HOST_A)
    first = d.register(ROOT, "/Embody", CONVOY, runtime_id="rt_before")
    assert first["runtime_id"] == "rt_before"
    second = d.register(ROOT, "/Embody", CONVOY, runtime_id="rt_after")
    assert second["node_id"] == first["node_id"], "same node"
    assert second["runtime_id"] == "rt_after", "different run"


# -- the cases that drove the design ---------------------------------

def test_a_worktree_is_a_different_node():
    """THE case: `git worktree add` -- which this repo's rules mandate --
    must not collapse into the live checkout."""
    d = ci.NodeDirectory(HOST_A)
    live = d.register(ROOT, "/Embody", CONVOY)
    worktree = d.register(ROOT2, "/Embody", CONVOY)
    assert live["node_id"] != worktree["node_id"]
    assert len(d.nodes()) == 2


def test_a_copied_project_folder_is_a_different_node():
    d = ci.NodeDirectory(HOST_A)
    original = d.register("/Work/Show", "/Embody", CONVOY)
    copy = d.register("/Work/Show copy", "/Embody", CONVOY)
    assert original["node_id"] != copy["node_id"]


def test_two_embody_comps_in_one_project_are_two_nodes():
    d = ci.NodeDirectory(HOST_A)
    a = d.register(ROOT, "/Embody", CONVOY)
    b = d.register(ROOT, "/scenes/Embody", CONVOY)
    assert a["node_id"] != b["node_id"]


def test_two_toe_files_in_one_repo_are_two_nodes():
    """The P0 regression: shared repo root and COMP path must not make
    two independent .toe processes overwrite each other's runtime/port."""
    d = ci.NodeDirectory(HOST_A)
    a = d.register(ROOT, "/Embody", CONVOY,
                   node_discriminator=DISC_A, runtime_id="rt_a")
    b = d.register(ROOT, "/Embody", CONVOY,
                   node_discriminator=DISC_B, runtime_id="rt_b")
    assert a["node_id"] != b["node_id"]
    assert a["runtime_id"] == "rt_a"
    assert b["runtime_id"] == "rt_b"
    assert len(d.nodes()) == 2


def test_same_toe_discriminator_reopens_the_same_node():
    d = ci.NodeDirectory(HOST_A)
    first = d.register(ROOT, "/Embody", CONVOY,
                       node_discriminator=DISC_A, runtime_id="rt_before")
    again = d.register(ROOT, "/Embody", CONVOY,
                       node_discriminator=DISC_A, runtime_id="rt_after")
    assert again["node_id"] == first["node_id"]
    assert again["runtime_id"] == "rt_after"
    assert again["node_discriminator"] == DISC_A


def test_lookup_location_is_canonical_and_returns_a_detached_snapshot():
    d = ci.NodeDirectory(HOST_A)
    record = d.register("C:\\Work\\Show", "/Embody", CONVOY,
                        platform="win32", node_discriminator=DISC_A)
    d.set_metadata(record["node_id"], {"node_name": "stage left"})

    found = d.lookup_location(
        "c:/work/show/", "/Embody", node_discriminator=DISC_A,
        platform="win32")
    assert found["node_id"] == record["node_id"]
    found["enabled"] = False
    found["metadata"]["node_name"] = "mutated by caller"

    stored = d.lookup(record["node_id"])
    assert stored["enabled"] is True
    assert stored["metadata"] == {"node_name": "stage left"}


def test_lookup_location_requires_the_exact_toe_discriminator():
    d = ci.NodeDirectory(HOST_A)
    d.register(ROOT, "/Embody", CONVOY, node_discriminator=DISC_A)
    assert d.lookup_location(
        ROOT, "/Embody", node_discriminator=DISC_B) is None
    assert d.lookup_location(ROOT, "/Embody") is None


@pytest.mark.parametrize("bad", ["", "legacy", "nd_short",
                                  "nd_" + "A" * 32, 123])
def test_malformed_node_discriminator_is_a_named_refusal(bad):
    with pytest.raises(ci.IdentityError) as e:
        ci.NodeDirectory(HOST_A).register(
            ROOT, "/Embody", CONVOY, node_discriminator=bad)
    assert e.value.reason == "malformed_node_discriminator"


def test_renaming_the_folder_makes_a_new_node_the_accepted_trade():
    """Documented trade-off, pinned so nobody "fixes" it by accident: a
    moved folder is a new node and must be re-approved. Chosen because
    the alternative failure -- two live checkouts sharing an identity --
    is silent and lands work on the wrong target."""
    d = ci.NodeDirectory(HOST_A)
    before = d.register("/Work/Show", "/Embody", CONVOY)
    d.approve_td_python(before["node_id"])
    after = d.register("/Work/Renamed", "/Embody", CONVOY)
    assert after["node_id"] != before["node_id"]
    assert after["td_python_approved"] is False, (
        "a moved project re-approves; it never inherits the grant")


def test_the_same_project_on_two_hosts_is_two_nodes():
    on_a = ci.NodeDirectory(HOST_A).register(ROOT, "/Embody", CONVOY)
    on_b = ci.NodeDirectory(HOST_B).register(ROOT, "/Embody", CONVOY)
    assert on_a["node_id"] != on_b["node_id"]


# -- path normalization: one folder is ONE node ----------------------

def test_windows_path_spellings_collapse_to_one_node():
    d = ci.NodeDirectory(HOST_A)
    a = d.register("C:\\Work\\Show", "/Embody", CONVOY, platform="win32")
    b = d.register("C:/Work/Show", "/Embody", CONVOY, platform="win32")
    c = d.register("c:\\work\\show\\", "/Embody", CONVOY, platform="win32")
    assert a["node_id"] == b["node_id"] == c["node_id"]
    assert len(d.nodes()) == 1


def test_posix_paths_stay_case_sensitive():
    """/Work and /work are genuinely different directories on POSIX."""
    d = ci.NodeDirectory(HOST_A)
    upper = d.register("/Work/Show", "/Embody", CONVOY, platform="linux")
    lower = d.register("/work/show", "/Embody", CONVOY, platform="linux")
    assert upper["node_id"] != lower["node_id"]


def test_trailing_separators_and_dots_normalize():
    d = ci.NodeDirectory(HOST_A)
    a = d.register("/Work/Show", "/Embody", CONVOY, platform="linux")
    b = d.register("/Work/./Show/", "/Embody", CONVOY, platform="linux")
    assert a["node_id"] == b["node_id"]


# -- approvals and remint ---------------------------------------------

def test_new_identity_has_no_td_python_approval():
    d = ci.NodeDirectory(HOST_A)
    assert d.register(ROOT, "/Embody", CONVOY)["td_python_approved"] is False


def test_new_identity_is_enabled_with_empty_descriptive_metadata():
    record = ci.NodeDirectory(HOST_A).register(ROOT, "/Embody", CONVOY)
    assert record["enabled"] is True
    assert record["metadata"] == {}


def test_membership_intent_can_be_disabled_without_forgetting_identity():
    d = ci.NodeDirectory(HOST_A)
    record = d.register(ROOT, "/Embody", CONVOY)
    changed = d.set_enabled(record["node_id"], False)
    assert changed["enabled"] is False
    assert d.lookup(record["node_id"])["enabled"] is False
    assert d.lookup(record["node_id"])["node_id"] == record["node_id"]


@pytest.mark.parametrize("bad", [0, 1, None, "true"])
def test_membership_intent_requires_a_real_boolean(bad):
    d = ci.NodeDirectory(HOST_A)
    record = d.register(ROOT, "/Embody", CONVOY)
    with pytest.raises(ci.IdentityError) as e:
        d.set_enabled(record["node_id"], bad)
    assert e.value.reason == "malformed_enabled"


def test_metadata_is_allowlisted_bounded_and_detached():
    d = ci.NodeDirectory(HOST_A)
    record = d.register(ROOT, "/Embody", CONVOY)
    supplied = {
        "toe_path": "  /Work/Show/show.toe  ",
        "toe_name": "show.toe",
        "node_name": "stage left",
        "hostname": "render-01",
        "process_id": 1234,
        "embody_version": "6.0.178",
        "touchdesigner_version": "2025.30000",
    }
    changed = d.set_metadata(record["node_id"], supplied)
    supplied["node_name"] = "caller mutation"
    changed["metadata"]["hostname"] = "response mutation"
    stored = d.lookup(record["node_id"])["metadata"]
    assert stored["toe_path"] == "/Work/Show/show.toe"
    assert stored["node_name"] == "stage left"
    assert stored["hostname"] == "render-01"
    assert stored["process_id"] == 1234


@pytest.mark.parametrize("metadata", [
    [],
    {"controller_id": "not descriptive"},
    {"hostname": 123},
    {"hostname": "bad\nline"},
    {"hostname": "x" * 256},
    {"process_id": True},
    {"process_id": 0},
])
def test_malformed_or_authority_shaped_metadata_is_refused(metadata):
    d = ci.NodeDirectory(HOST_A)
    record = d.register(ROOT, "/Embody", CONVOY)
    with pytest.raises(ci.IdentityError) as e:
        d.set_metadata(record["node_id"], metadata)
    assert e.value.reason == "malformed_metadata"
    assert d.lookup(record["node_id"])["metadata"] == {}


def test_unknown_node_metadata_and_membership_updates_are_named_refusals():
    d = ci.NodeDirectory(HOST_A)
    for action in (lambda: d.set_enabled("ghost", False),
                   lambda: d.set_metadata("ghost", {})):
        with pytest.raises(ci.IdentityError) as e:
            action()
        assert e.value.reason == "unknown_node"


def test_remint_resets_td_python_approval_to_off():
    d = ci.NodeDirectory(HOST_A)
    old = d.register(ROOT, "/Embody", CONVOY)
    d.approve_td_python(old["node_id"])
    fresh = d.remint(old["node_id"])
    assert fresh["node_id"] != old["node_id"]
    assert fresh["td_python_approved"] is False
    assert d.lookup(old["node_id"]) is None
    again = d.register(ROOT, "/Embody", CONVOY)
    assert again["node_id"] == fresh["node_id"]


def test_remint_preserves_the_toe_discriminator():
    d = ci.NodeDirectory(HOST_A)
    old = d.register(ROOT, "/Embody", CONVOY,
                     node_discriminator=DISC_A)
    fresh = d.remint(old["node_id"])
    assert fresh["node_discriminator"] == DISC_A
    again = d.register(ROOT, "/Embody", CONVOY,
                       node_discriminator=DISC_A)
    assert again["node_id"] == fresh["node_id"]


def test_project_cannot_switch_convoy_silently():
    d = ci.NodeDirectory(HOST_A)
    d.register(ROOT, "/Embody", CONVOY)
    with pytest.raises(ci.IdentityError) as e:
        d.register(ROOT, "/Embody", "another-convoy")
    assert e.value.reason == "node_identity_conflict"


# -- automatic Convoy realm binding ---------------------------------

def test_legacy_registration_defaults_to_an_established_binding():
    record = ci.NodeDirectory(HOST_A).register(ROOT, "/Embody", CONVOY)
    assert record["binding_state"] == "established"


def test_live_registration_can_create_an_explicit_candidate_binding():
    record = ci.NodeDirectory(HOST_A).register(
        ROOT, "/Embody", "cv_candidate", binding_state="candidate")
    assert record["convoy_id"] == "cv_candidate"
    assert record["binding_state"] == "candidate"


@pytest.mark.parametrize("bad", [None, "", "pending", 1, True, []])
def test_binding_state_is_closed_to_candidate_or_established(bad):
    with pytest.raises(ci.IdentityError) as e:
        ci.NodeDirectory(HOST_A).register(
            ROOT, "/Embody", CONVOY, binding_state=bad)
    assert e.value.reason == "malformed_binding_state"


def test_registration_cannot_silently_promote_or_demote_a_binding():
    d = ci.NodeDirectory(HOST_A)
    candidate = d.register(
        ROOT, "/Embody", CONVOY, binding_state="candidate",
        runtime_id="rt_before")
    with pytest.raises(ci.IdentityError) as e:
        d.register(ROOT, "/Embody", CONVOY,
                   binding_state="established", runtime_id="rt_after")
    assert e.value.reason == "node_binding_conflict"
    assert candidate["binding_state"] == "candidate"
    assert candidate["runtime_id"] == "rt_before"

    established = d.register(
        ROOT2, "/Embody", CONVOY, binding_state="established")
    with pytest.raises(ci.IdentityError) as e:
        d.register(ROOT2, "/Embody", CONVOY, binding_state="candidate")
    assert e.value.reason == "node_binding_conflict"
    assert established["binding_state"] == "established"


def test_candidate_rebind_is_cas_and_preserves_node_runtime_and_policy():
    d = ci.NodeDirectory(HOST_A)
    record = d.register(
        ROOT, "/Embody", "cv_provisional", binding_state="candidate",
        runtime_id="rt_live", envoy_port=9981)
    d.approve_td_python(record["node_id"])
    d.set_enabled(record["node_id"], False)
    d.set_metadata(record["node_id"], {"node_name": "render-a"})
    d.set_live_state(
        record["node_id"], wake_port=44001, wake_token="secret",
        remote_wake=True, perform_mode=True, wake_grace_s=75)

    changed = d.rebind_candidate(
        record["node_id"], "cv_provisional", "cv_authoritative")
    assert changed["convoy_id"] == "cv_authoritative"
    assert changed["binding_state"] == "established"
    assert changed["node_id"] == record["node_id"]
    assert changed["runtime_id"] == "rt_live"
    assert changed["envoy_port"] == 9981
    assert changed["wake_port"] == 44001
    assert changed["td_python_approved"] is True
    assert changed["enabled"] is False
    assert changed["metadata"] == {"node_name": "render-a"}


def test_candidate_can_rebind_while_provisional_then_promote_in_place():
    d = ci.NodeDirectory(HOST_A)
    record = d.register(
        ROOT, "/Embody", "cv_high", binding_state="candidate")
    first = d.rebind_candidate(
        record["node_id"], "cv_high", "cv_low",
        binding_state="candidate")
    assert (first["convoy_id"], first["binding_state"]) == (
        "cv_low", "candidate")
    final = d.rebind_candidate(record["node_id"], "cv_low", "cv_low")
    assert (final["convoy_id"], final["binding_state"]) == (
        "cv_low", "established")


def test_established_binding_can_never_be_rebound_or_demoted():
    d = ci.NodeDirectory(HOST_A)
    record = d.register(ROOT, "/Embody", CONVOY)
    for target_id, target_state in (("cv_other", "established"),
                                    (CONVOY, "candidate")):
        with pytest.raises(ci.IdentityError) as e:
            d.rebind_candidate(
                record["node_id"], CONVOY, target_id,
                binding_state=target_state)
        assert e.value.reason == "candidate_rebind_conflict"
    assert d.lookup(record["node_id"])["convoy_id"] == CONVOY
    assert d.lookup(record["node_id"])["binding_state"] == "established"


def test_bulk_candidate_rebind_leaves_established_mismatch_untouched():
    d = ci.NodeDirectory(HOST_A)
    a = d.register(
        ROOT, "/Embody", "cv_b", binding_state="candidate")
    b = d.register(
        ROOT2, "/Embody", "cv_c", binding_state="candidate")
    established = d.register(
        "/Work/Existing", "/Embody", "cv_existing")

    changed = d.rebind_candidates("cv_a")
    assert {row["node_id"] for row in changed} == {
        a["node_id"], b["node_id"]}
    assert all(row["convoy_id"] == "cv_a"
               and row["binding_state"] == "established"
               for row in changed)
    assert established["convoy_id"] == "cv_existing"
    assert established["binding_state"] == "established"


def test_stale_bulk_candidate_cas_changes_nothing():
    d = ci.NodeDirectory(HOST_A)
    a = d.register(
        ROOT, "/Embody", "cv_a", binding_state="candidate")
    b = d.register(
        ROOT2, "/Embody", "cv_b", binding_state="candidate")
    with pytest.raises(ci.IdentityError) as e:
        d.rebind_candidates(
            "cv_final", expected={a["node_id"]: "cv_a",
                                  b["node_id"]: "cv_stale"})
    assert e.value.reason == "candidate_rebind_conflict"
    assert d.lookup(a["node_id"])["binding_state"] == "candidate"
    assert d.lookup(a["node_id"])["convoy_id"] == "cv_a"
    assert d.lookup(b["node_id"])["binding_state"] == "candidate"
    assert d.lookup(b["node_id"])["convoy_id"] == "cv_b"


def test_duplicate_node_id_is_a_structured_conflict():
    d = ci.NodeDirectory(HOST_A)
    first = d.register(ROOT, "/Embody", CONVOY)
    with pytest.raises(ci.IdentityError) as e:
        d.register(ROOT2, "/Embody", CONVOY, minted_id=first["node_id"])
    assert e.value.reason == "node_identity_conflict"


def test_forget_rolls_back_a_registration():
    d = ci.NodeDirectory(HOST_A)
    record = d.register(ROOT, "/Embody", CONVOY)
    d.forget(record["node_id"])
    assert d.nodes() == []
    fresh = d.register(ROOT, "/Embody", CONVOY)
    assert fresh["node_id"] != record["node_id"]


def test_runtime_ids_are_unique_per_mint():
    assert len({ci.mint_runtime_id() for _ in range(64)}) == 64


@pytest.mark.parametrize("bad", [None, "", 123, "not-hex", "A" * 32])
def test_malformed_host_id_refused(bad):
    with pytest.raises(ci.IdentityError):
        ci.NodeDirectory(bad)


@pytest.mark.parametrize("root,comp,convoy", [
    (None, "/Embody", CONVOY), ("", "/Embody", CONVOY),
    (ROOT, None, CONVOY), (ROOT, "", CONVOY),
    (ROOT, "/Embody", None), (ROOT, "/Embody", ""),
])
def test_malformed_registration_refused(root, comp, convoy):
    with pytest.raises(ci.IdentityError):
        ci.NodeDirectory(HOST_A).register(root, comp, convoy)


# -- the live Envoy port: set on register, cleared only explicitly -----

def test_clear_envoy_port_forgets_the_port_but_keeps_the_node():
    """A node's clean exit. node_id is the durable address an approval
    attaches to; only the per-launch port goes."""
    d = ci.NodeDirectory(HOST_A)
    record = d.register(ROOT, "/Embody", CONVOY, envoy_port=9981)
    d.approve_td_python(record["node_id"])

    cleared = d.clear_envoy_port(record["node_id"])
    assert cleared["envoy_port"] is None
    assert cleared["node_id"] == record["node_id"]
    assert cleared["td_python_approved"] is True
    assert d.lookup(record["node_id"]) is cleared


def test_clear_envoy_port_is_idempotent():
    d = ci.NodeDirectory(HOST_A)
    record = d.register(ROOT, "/Embody", CONVOY, envoy_port=9981)
    d.clear_envoy_port(record["node_id"])
    assert d.clear_envoy_port(record["node_id"])["envoy_port"] is None


def test_clear_envoy_port_of_an_unknown_node_refused():
    d = ci.NodeDirectory(HOST_A)
    with pytest.raises(ci.IdentityError) as e:
        d.clear_envoy_port("ghost")
    assert e.value.reason == "unknown_node"


def test_a_portless_re_register_never_clears_a_known_port():
    """The rule clear_envoy_port exists to preserve: a store replay (or
    any re-register omitting the port) passes None, and treating that as
    "clear it" would wipe a live port on every restart replay."""
    d = ci.NodeDirectory(HOST_A)
    d.register(ROOT, "/Embody", CONVOY, envoy_port=9981)
    again = d.register(ROOT, "/Embody", CONVOY)
    assert again["envoy_port"] == 9981


def test_registering_after_a_clear_restores_the_port():
    d = ci.NodeDirectory(HOST_A)
    first = d.register(ROOT, "/Embody", CONVOY, envoy_port=9981)
    d.clear_envoy_port(first["node_id"])
    again = d.register(ROOT, "/Embody", CONVOY, envoy_port=9982)
    assert again["node_id"] == first["node_id"]
    assert again["envoy_port"] == 9982
