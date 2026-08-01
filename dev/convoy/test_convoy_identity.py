"""Node identity semantics, pinned as tests. Pure, no I/O.

Two identifiers (Dylan's decision, 2026-07-31):
  runtime_id -- which RUN. Random every TD start, never stored.
  node_id    -- which PROJECT ON WHICH MACHINE. Host-assigned, keyed on
                (project_root, comp_path). Nothing lives in the project,
                so a worktree or copy is a different node by
                construction.
"""

import pytest

import convoy_identity as ci

HOST_A = "a" * 32
HOST_B = "b" * 32
ROOT = "/Work/Show"
ROOT2 = "/Work/Show-wt-fix"
CONVOY = "studio-convoy"


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


def test_project_cannot_switch_convoy_silently():
    d = ci.NodeDirectory(HOST_A)
    d.register(ROOT, "/Embody", CONVOY)
    with pytest.raises(ci.IdentityError) as e:
        d.register(ROOT, "/Embody", "another-convoy")
    assert e.value.reason == "node_identity_conflict"


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
