"""Peer records, the FAIL-CLOSED denylist, and THE decision function.

The named test the plan demands is
test_an_unparseable_denylist_refuses_every_peer -- plus the mutant it
kills, test_the_fail_open_denylist_would_be_caught, which asserts that
the natural (and exactly backwards) implementation cannot pass.

NO NETWORK ANYWHERE IN THIS SUITE. Slice 2 is memory and decision only.
"""

import json
import os
import time

import pytest

import convoy_peers as cp

# A real 128-bit hex host id, and two well-formed SPKI fingerprints.
HOST_A = "ab" * 16
HOST_B = "cd" * 16
HOST_C = "ef" * 16
FP_A = "cvfp1-m188-6zc5-0w2r-5k7q-755g-k244-fk1h-5jw8"
FP_B = "cvfp1-0000-1111-2222-3333-4444-5555-6666-7777"
FP_C = "cvfp1-9999-8888-7777-6666-5555-4444-3333-2222"


@pytest.fixture
def store(tmp_path):
    return cp.PeerStore(str(tmp_path / "state"))


def denylist_path(store):
    return store.denylist.path


def write_denylist(store, text, age_s=5.0):
    """Write denylist.json and BACK-DATE it.

    Back-dating matters: a file whose mtime is within MTIME_TRUST_S of
    now is deliberately re-read on every call (same-tick edits are
    invisible to a stat cache), so a test that did not age the file would
    pass even with the mtime cache broken.
    """
    path = denylist_path(store)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    old = time.time() - age_s
    os.utime(path, (old, old))


# =====================================================================
# KNOWN-ANSWER VECTORS
#
# Every derivation here is pinned to a fixed value, because a derivation
# that CHANGES still passes every property test: slice 1 shipped a
# fingerprint whose salted derivation was green across the whole suite.
# A changed peer_digest silently breaks audit correlation; a changed key
# FORM silently stops every existing denylist entry from matching, which
# is a security failure that looks like nothing at all.
# =====================================================================

def test_known_answer_the_peer_digest_is_fixed():
    assert cp.peer_digest(HOST_A, FP_A) == "843ab3119c878cb8"
    assert cp.peer_digest(HOST_A, FP_B) == "44cbe9796223036e"
    assert cp.peer_digest("", "") == "dae83e6eb202e9b7"


def test_known_answer_the_digest_binds_BOTH_halves():
    """It digests the PIN, not either half: changing either changes it."""
    base = cp.peer_digest(HOST_A, FP_A)
    assert cp.peer_digest(HOST_B, FP_A) != base
    assert cp.peer_digest(HOST_A, FP_B) != base
    # and the NUL join means no pair can be forged from another
    assert cp.peer_digest(HOST_A + FP_A, "") != base


def test_known_answer_the_denylist_key_form_is_fixed():
    """The matching form is stripped + lowercased, and nothing else.

    An operator pastes the DISPLAY fingerprint (uppercase, grouped) out
    of a join dialog straight into denylist.json. If the key form ever
    stopped folding case, that entry would stop matching and the block
    would silently do nothing.
    """
    assert cp.normalize_host_id("  " + HOST_A.upper() + " ") == HOST_A
    assert cp.normalize_fingerprint(" " + FP_A.upper() + "  ") == FP_A
    assert cp.fold("  CvFp1-AbCd  ") == "cvfp1-abcd"
    # and the digest is computed over the FOLDED form, so a display-form
    # pair and a canonical pair correlate to the same audit id
    assert cp.peer_digest(HOST_A.upper(), FP_A.upper()) == \
        cp.peer_digest(HOST_A, FP_A)


def test_known_answer_the_file_names_are_fixed():
    """Renaming either file makes an existing install's records
    invisible -- and an absent peers.json reads as 'nobody admitted',
    which is a silent, total loss of membership."""
    assert cp.PEERS_FILE == "peers.json"
    assert cp.DENYLIST_FILE == "denylist.json"


def test_known_answer_the_states_and_reasons_are_fixed():
    assert cp.PEER_STATES == ("pending", "admitted", "observe_only",
                              "blocked")
    assert cp.PEER_REASONS == ("peer_blocked", "peer_unknown",
                               "pin_mismatch", "peer_observe_only",
                               "namespace_not_admitted")


def test_malformed_identities_normalize_to_none():
    for bad in (None, "", "zz" * 16, HOST_A[:-1], 5, ["a"], HOST_A + "a"):
        assert cp.normalize_host_id(bad) is None
    for bad in (None, "", FP_A[:-1], "cvfp1-iiii-" + FP_A[11:], 5,
                FP_A.replace("-", "")):
        assert cp.normalize_fingerprint(bad) is None


# =====================================================================
# THE DENYLIST -- fail-closed
# =====================================================================

@pytest.mark.parametrize("body,why", [
    ("{ truncated", "unparseable JSON"),
    ("[]", "not an object"),
    ("", "empty file"),
    ('{"host_id": ["%s"]}' % HOST_A, "typo'd key name"),
    ('{"hosts": ["%s"]}' % HOST_A, "wrong key name"),
    ('{"host_ids": "%s"}' % HOST_A, "a string where a list belongs"),
    ('{"host_ids": [5]}', "a non-string entry"),
    ('{"host_ids": ["not-a-host-id"]}', "a malformed entry"),
    ('{"fingerprints": ["nope"]}', "a malformed fingerprint entry"),
    ('{"version": 99, "host_ids": []}', "a version from a newer build"),
    ('{"version": "1"}', "a non-integer version"),
    ('{"note": 5}', "a non-string note"),
])
def test_an_unparseable_denylist_refuses_every_peer(store, body, why):
    """THE named test (plan 1.4).

    Every way of failing to understand denylist.json refuses ALL peers.
    An admitted, correctly-pinned peer -- the one that would sail through
    on any other day -- is refused, and the refusal names the file.
    """
    store.admit(HOST_A, FP_A)
    assert store.authorize_peer(HOST_A, FP_A).allowed is True

    write_denylist(store, body)

    decision = store.authorize_peer(HOST_A, FP_A)
    assert decision.allowed is False, (
        f"a denylist with {why} must refuse EVERY peer, not none -- the "
        f"fail-OPEN implementation is the natural one and it is exactly "
        f"backwards")
    assert decision.reason == cp.REASON_BLOCKED
    assert "denylist" in decision.detail


def test_the_fail_open_denylist_would_be_caught(store, monkeypatch):
    """The MUTANT: prove the fail-open version cannot pass.

    Patch _read to do what the obvious implementation does -- swallow the
    error and hand back an empty denylist -- and assert the suite's
    contract breaks. Without this, "fail-closed" is a claim about code
    nobody perturbed.
    """
    store.admit(HOST_A, FP_A)
    write_denylist(store, "{ truncated")
    assert store.authorize_peer(HOST_A, FP_A).allowed is False

    def fail_open(self, signature):
        return frozenset(), frozenset(), None        # "nothing is blocked"

    monkeypatch.setattr(cp.Denylist, "_read", fail_open)
    store.denylist._volatile = True                  # force a re-read
    assert store.authorize_peer(HOST_A, FP_A).allowed is True, (
        "the fail-open mutant must change behaviour -- if this assertion "
        "fails the fail-closed test above is not actually testing "
        "anything")


def test_an_absent_denylist_blocks_nobody(store):
    """ABSENT is not UNREADABLE. A host that never blocked anybody has
    no denylist file, and that must not refuse the world."""
    store.admit(HOST_A, FP_A)
    assert not os.path.exists(denylist_path(store))
    assert store.authorize_peer(HOST_A, FP_A).allowed is True


def test_namespace_authorization_is_explicit_and_fail_closed(store):
    store.admit(HOST_A, FP_A, convoy_ids=["show-a"])

    # Host/session admission remains available to the TLS connection
    # layer, which does not reveal namespace-owned state.
    assert store.authorize_peer(HOST_A, FP_A).allowed is True

    allowed = store.authorize_peer(HOST_A, FP_A, convoy_id="show-a")
    assert allowed.allowed is True

    refused = store.authorize_peer(HOST_A, FP_A, convoy_id="show-b")
    assert refused.allowed is False
    assert refused.reason == cp.REASON_NAMESPACE
    assert "show-b" in refused.detail


def test_empty_namespace_grant_is_not_a_wildcard(store):
    store.admit(HOST_A, FP_A)
    decision = store.authorize_peer(HOST_A, FP_A, convoy_id="studio")
    assert decision.allowed is False
    assert decision.reason == cp.REASON_NAMESPACE
    # an explicitly empty object is the unambiguous "block nobody"
    write_denylist(store, "{}")
    assert store.authorize_peer(HOST_A, FP_A).allowed is True


def test_an_unreadable_denylist_file_refuses_everyone(store, monkeypatch):
    """Present-but-inaccessible (a lock, a permission) is a failure to
    understand, not an absence -- the discrimination get_job gets wrong
    for jobs and job_file_exists exists to fix."""
    store.admit(HOST_A, FP_A)
    write_denylist(store, "{}")
    assert store.authorize_peer(HOST_A, FP_A).allowed is True

    real_open = cp.open if hasattr(cp, "open") else open

    def locked(path, *a, **kw):
        if path == denylist_path(store):
            raise PermissionError("the file is locked")
        return real_open(path, *a, **kw)

    monkeypatch.setattr("builtins.open", locked)
    store.denylist._volatile = True
    decision = store.authorize_peer(HOST_A, FP_A)
    assert decision.allowed is False
    assert decision.reason == cp.REASON_BLOCKED


def test_an_unstatable_denylist_refuses_everyone(store, monkeypatch):
    store.admit(HOST_A, FP_A)
    write_denylist(store, "{}")
    real_stat = os.stat

    def boom(path, *a, **kw):
        # DELEGATE for every other path: os.stat is global, and a fake
        # stat_result for the rest of the process is how a test starts
        # breaking things it was never about.
        if path == denylist_path(store):
            raise PermissionError("no stat for you")
        return real_stat(path, *a, **kw)

    monkeypatch.setattr(cp.os, "stat", boom)
    store.denylist._volatile = True
    assert store.authorize_peer(HOST_A, FP_A).allowed is False


# -- the two independent keys -----------------------------------------

def test_blocking_by_host_id_alone_works(store):
    store.admit(HOST_A, FP_A)
    write_denylist(store, json.dumps({"host_ids": [HOST_A]}))
    decision = store.authorize_peer(HOST_A, FP_A)
    assert decision.allowed is False
    assert decision.reason == cp.REASON_BLOCKED
    assert HOST_A in decision.detail
    # a DIFFERENT host holding a different key is untouched
    store.admit(HOST_B, FP_B)
    assert store.authorize_peer(HOST_B, FP_B).allowed is True


def test_blocking_by_fingerprint_alone_works(store):
    store.admit(HOST_A, FP_A)
    write_denylist(store, json.dumps({"fingerprints": [FP_A]}))
    decision = store.authorize_peer(HOST_A, FP_A)
    assert decision.allowed is False
    assert decision.reason == cp.REASON_BLOCKED
    assert FP_A in decision.detail


def test_the_keys_do_not_subsume_each_other(store):
    """Blocking a FINGERPRINT survives a host_id change; blocking a
    HOST_ID survives a key rotation. Neither alone covers both."""
    # by fingerprint: the same key offered under a brand new host_id
    write_denylist(store, json.dumps({"fingerprints": [FP_A]}))
    store.admit(HOST_B, FP_A)         # peer "moved" to a new host_id
    assert store.authorize_peer(HOST_B, FP_A).allowed is False

    # by host_id: the same host offering a freshly rotated key
    write_denylist(store, json.dumps({"host_ids": [HOST_C]}))
    store.admit(HOST_C, FP_C)
    assert store.authorize_peer(HOST_C, FP_C).allowed is False
    assert store.authorize_peer(HOST_C, FP_B).allowed is False


def test_a_denylist_entry_matches_in_display_form(store):
    """An operator pastes the UPPERCASE display fingerprint at 2am."""
    store.admit(HOST_A, FP_A)
    write_denylist(store, json.dumps({"fingerprints": [FP_A.upper()],
                                      "host_ids": [HOST_B.upper()]}))
    assert store.authorize_peer(HOST_A, FP_A).allowed is False
    store.admit(HOST_B, FP_B)
    assert store.authorize_peer(HOST_B, FP_B).allowed is False


def test_a_blocked_peer_is_refused_even_with_a_malformed_fingerprint(store):
    """The denylist runs on the RAW values, before validation: a refusal
    must never depend on the offered identity being well-formed."""
    write_denylist(store, json.dumps({"host_ids": [HOST_A]}))
    decision = store.authorize_peer(HOST_A, "garbage")
    assert decision.allowed is False and decision.reason == cp.REASON_BLOCKED


def test_a_note_key_is_allowed_for_the_human(store):
    store.admit(HOST_A, FP_A)
    write_denylist(store, json.dumps(
        {"note": "blocked 2026-08-01 after the booth incident",
         "host_ids": [HOST_A]}))
    assert store.authorize_peer(HOST_A, FP_A).reason == cp.REASON_BLOCKED


# -- mtime re-read ----------------------------------------------------

def test_a_hand_edit_takes_effect_without_a_restart(store):
    """Incident response is a text editor, not a daemon restart."""
    store.admit(HOST_A, FP_A)
    assert store.authorize_peer(HOST_A, FP_A).allowed is True

    write_denylist(store, json.dumps({"host_ids": [HOST_A]}))
    assert store.authorize_peer(HOST_A, FP_A).allowed is False, (
        "the denylist must be re-read on an mtime change; a supervised "
        "daemon cannot be restarted to apply one")

    # ... and lifting the block is equally live
    write_denylist(store, json.dumps({"host_ids": []}))
    assert store.authorize_peer(HOST_A, FP_A).allowed is True


def test_an_unchanged_denylist_is_not_reparsed_every_call(store):
    store.admit(HOST_A, FP_A)
    write_denylist(store, json.dumps({"host_ids": [HOST_B]}))
    store.authorize_peer(HOST_A, FP_A)          # first read
    before = store.denylist.reloads
    for _ in range(10):
        store.authorize_peer(HOST_A, FP_A)
    assert store.denylist.reloads == before, (
        "a stable denylist must be cached; re-parsing per authorization "
        "puts a file read on the hot path of every dispatch")


def test_a_freshly_written_denylist_is_not_cached(tmp_path):
    """A file written THIS instant may be written again inside the same
    filesystem timestamp tick, producing an identical (mtime, size) for
    different bytes. That window must not be cached."""
    store = cp.PeerStore(str(tmp_path / "state"))
    store.admit(HOST_A, FP_A)
    path = store.denylist.path
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps({"host_ids": [HOST_B]}))     # NOT back-dated
    store.authorize_peer(HOST_A, FP_A)
    before = store.denylist.reloads
    store.authorize_peer(HOST_A, FP_A)
    assert store.denylist.reloads > before


def test_a_same_size_edit_is_still_seen(store):
    """Two denylists of identical byte length, different content."""
    store.admit(HOST_A, FP_A)
    store.admit(HOST_B, FP_B)
    write_denylist(store, json.dumps({"host_ids": [HOST_A]}))
    assert store.authorize_peer(HOST_A, FP_A).allowed is False
    assert store.authorize_peer(HOST_B, FP_B).allowed is True
    body = json.dumps({"host_ids": [HOST_B]})
    write_denylist(store, body, age_s=3.0)
    assert store.authorize_peer(HOST_A, FP_A).allowed is True
    assert store.authorize_peer(HOST_B, FP_B).allowed is False


# =====================================================================
# THE DECISION -- pin and admission
# =====================================================================

def test_an_unknown_peer_is_refused_by_name(store):
    decision = store.authorize_peer(HOST_A, FP_A)
    assert decision.allowed is False
    assert decision.reason == cp.REASON_UNKNOWN
    assert decision.may_mutate is False
    assert bool(decision) is False


def test_a_pending_peer_is_not_admitted(store):
    store.record_peer(HOST_A, FP_A, display_name="booth laptop")
    decision = store.authorize_peer(HOST_A, FP_A)
    assert decision.allowed is False
    assert decision.reason == cp.REASON_UNKNOWN
    assert decision.state == cp.PEER_PENDING


def test_an_admitted_peer_may_act(store):
    store.admit(HOST_A, FP_A, admitted_via="manual")
    decision = store.authorize_peer(HOST_A, FP_A)
    assert decision.allowed is True and decision.may_mutate is True
    assert decision.reason is None
    assert decision.host_id == HOST_A and decision.fingerprint == FP_A


def test_an_observe_only_peer_is_allowed_but_may_not_mutate(store):
    store.admit(HOST_A, FP_A)
    store.observe(HOST_A)
    decision = store.authorize_peer(HOST_A, FP_A)
    assert decision.allowed is True, "observe-only still permits X0 (24.6)"
    assert decision.may_mutate is False
    assert decision.reason == cp.REASON_OBSERVE_ONLY


def test_a_blocked_record_refuses_every_class_including_x0(store):
    store.admit(HOST_A, FP_A)
    store.block(HOST_A)
    decision = store.authorize_peer(HOST_A, FP_A)
    assert decision.allowed is False and decision.may_mutate is False
    assert decision.reason == cp.REASON_BLOCKED


def test_a_changed_key_is_a_pin_mismatch_never_an_auto_update(store):
    store.admit(HOST_A, FP_A)
    decision = store.authorize_peer(HOST_A, FP_B)
    assert decision.reason == cp.REASON_PIN_MISMATCH
    assert FP_A in decision.detail and FP_B in decision.detail
    # THE PIN IS NEVER AUTO-UPDATED -- not even after repeated mismatch
    for _ in range(5):
        store.authorize_peer(HOST_A, FP_B)
    assert store.get(HOST_A)["fingerprint"] == FP_A
    assert store.authorize_peer(HOST_A, FP_A).allowed is True


def test_a_pinned_key_offered_under_a_new_host_id_is_a_pin_mismatch(store):
    """The pin is the BINDING, so half of it moving is a broken binding
    -- not a stranger, and the operator must be told which half."""
    store.admit(HOST_A, FP_A)
    decision = store.authorize_peer(HOST_B, FP_A)
    assert decision.reason == cp.REASON_PIN_MISMATCH
    assert HOST_A in decision.detail


def test_a_malformed_identity_is_refused(store):
    """A malformed HOST_ID is a stranger; a malformed FINGERPRINT from a
    host we DO know is a broken pin, and saying so is the point of the
    branch order (an operator debugging a stuck queue must not be told
    their valid host_id is malformed)."""
    store.admit(HOST_A, FP_A)
    for host, fingerprint in (("nope", FP_A), ("", ""), (None, None),
                              (HOST_A[:-1], FP_A)):
        decision = store.authorize_peer(host, fingerprint)
        assert decision.allowed is False
        assert decision.reason == cp.REASON_UNKNOWN
    for fingerprint in ("nope", "", None, FP_A[:-1]):
        decision = store.authorize_peer(HOST_A, fingerprint)
        assert decision.allowed is False
        assert decision.reason == cp.REASON_PIN_MISMATCH
        assert FP_A in decision.detail, "name the key we DID pin"


def test_the_denylist_is_consulted_before_admission(store):
    """ORDER: an ADMITTED, correctly-pinned peer is still refused when it
    is on the denylist, and the reason is peer_blocked -- not the
    permissive answer admission alone would give."""
    store.admit(HOST_A, FP_A)
    write_denylist(store, json.dumps({"host_ids": [HOST_A]}))
    assert store.authorize_peer(HOST_A, FP_A).reason == cp.REASON_BLOCKED


def test_the_denylist_beats_a_pin_mismatch_too(store):
    """A blocked peer is refused as BLOCKED even when what it offered
    would otherwise be reported as a pin mismatch: the denylist is step
    one and nothing downstream of it runs."""
    store.admit(HOST_A, FP_A)
    write_denylist(store, json.dumps({"host_ids": [HOST_A]}))
    assert store.authorize_peer(HOST_A, FP_B).reason == cp.REASON_BLOCKED


# =====================================================================
# THE KILLSWITCH (A-32)
# =====================================================================

def test_the_killswitch_refuses_every_peer_at_once(store):
    store.admit(HOST_A, FP_A)
    store.admit(HOST_B, FP_B)
    store.set_killswitch(True, reason="venue network is hostile")
    for host, fingerprint in ((HOST_A, FP_A), (HOST_B, FP_B)):
        decision = store.authorize_peer(host, fingerprint)
        assert decision.allowed is False
        assert decision.reason == cp.REASON_BLOCKED
        assert "killswitch" in decision.detail
        assert "venue network is hostile" in decision.detail


def test_the_killswitch_is_reversible_and_unwinds_no_membership(store):
    store.admit(HOST_A, FP_A, display_name="booth")
    store.admit(HOST_B, FP_B)
    store.observe(HOST_B)
    before = store.peers()
    store.set_killswitch(True)
    assert store.peers() == before, "membership must be untouched"
    store.set_killswitch(False)
    assert store.authorize_peer(HOST_A, FP_A).may_mutate is True
    observed = store.authorize_peer(HOST_B, FP_B)
    assert observed.allowed is True and observed.may_mutate is False, (
        "releasing the killswitch must restore the EXACT prior state, "
        "including a narrowing to observe-only")


def test_the_killswitch_survives_a_reload(tmp_path):
    directory = str(tmp_path / "state")
    first = cp.PeerStore(directory)
    first.admit(HOST_A, FP_A)
    first.set_killswitch(True, reason="incident")
    second = cp.PeerStore(directory)
    assert second.killswitch()["engaged"] is True
    assert second.authorize_peer(HOST_A, FP_A).allowed is False


# =====================================================================
# THE PEER RECORD AND ITS FILE
# =====================================================================

def test_the_record_carries_every_field_the_plan_names(store):
    record = store.admit(HOST_A, FP_A, admitted_via="manual",
                         display_name="booth laptop",
                         endpoints=["192.168.88.30:47600"],
                         convoy_ids=["studio"], cert_pem="-----BEGIN-----",
                         clock_offset_s=0.25)
    for field in ("host_id", "fingerprint", "cert_pem", "display_name",
                  "state", "admitted_at", "admitted_via", "pin_first_seen",
                  "last_seen", "endpoints", "convoy_ids", "clock_offset_s"):
        assert field in record, field
    assert record["state"] == cp.PEER_ADMITTED
    assert record["admitted_via"] == "manual"
    assert record["endpoints"] == ["192.168.88.30:47600"]
    assert record["clock_offset_s"] == 0.25
    assert record["admitted_at"] is not None


def test_peers_are_host_private_and_survive_a_reload(tmp_path):
    directory = str(tmp_path / "state")
    first = cp.PeerStore(directory)
    first.admit(HOST_A, FP_A, display_name="booth")
    first.record_peer(HOST_B, FP_B)
    path = os.path.join(directory, cp.PEERS_FILE)
    assert os.path.exists(path)

    second = cp.PeerStore(directory)
    assert second.authorize_peer(HOST_A, FP_A).allowed is True
    assert second.authorize_peer(HOST_B, FP_B).allowed is False
    assert second.get(HOST_A)["display_name"] == "booth"


@pytest.mark.skipif(os.name == "nt",
                    reason="POSIX mode bits; NTFS ACLs govern on Windows")
def test_peers_json_is_0600(tmp_path):
    import stat as stat_mod
    directory = str(tmp_path / "state")
    store = cp.PeerStore(directory)
    store.admit(HOST_A, FP_A)
    mode = os.stat(os.path.join(directory, cp.PEERS_FILE)).st_mode
    assert stat_mod.S_IMODE(mode) == 0o600


def test_forget_drops_the_identity_and_the_pin(store):
    store.admit(HOST_A, FP_A)
    dropped = store.forget(HOST_A)
    assert dropped["host_id"] == HOST_A
    assert store.get(HOST_A) is None
    decision = store.authorize_peer(HOST_A, FP_A)
    assert decision.reason == cp.REASON_UNKNOWN, (
        "a forgotten peer is a stranger again -- not a pin mismatch")
    with pytest.raises(cp.PeerError):
        store.forget(HOST_A)


def test_block_keeps_the_pin_so_impersonation_is_still_detected(store):
    """The difference between block and forget, stated as a test."""
    store.admit(HOST_A, FP_A)
    store.block(HOST_A)
    assert store.get(HOST_A)["fingerprint"] == FP_A
    store.forget(HOST_A)
    assert store.get(HOST_A) is None


def test_admitting_without_a_fingerprint_is_refused(store):
    """Admission is consent to a BINDING. An admit that inherited
    whatever the peer last offered would be a pin auto-update wearing a
    different name."""
    with pytest.raises(cp.PeerError) as e:
        store.admit(HOST_A, None)
    assert e.value.reason == "malformed_fingerprint"


def test_a_re_admission_records_the_pin_change(store):
    store.admit(HOST_A, FP_A, cert_pem="-----OLD-----")
    first_seen = store.get(HOST_A)["pin_first_seen"]
    time.sleep(0.01)
    store.admit(HOST_A, FP_B)
    record = store.get(HOST_A)
    assert record["fingerprint"] == FP_B
    assert record["pin_first_seen"] != first_seen
    assert record["cert_pem"] is None, (
        "the old certificate must not survive a key change")


def test_state_changes_require_an_existing_record(store):
    for method in (store.block, store.observe, store.forget):
        with pytest.raises(cp.PeerError) as e:
            method(HOST_C)
        assert e.value.reason == "unknown_peer"


def test_the_store_is_bounded(store, monkeypatch):
    monkeypatch.setattr(cp, "MAX_PEERS", 2)
    store.admit(HOST_A, FP_A)
    store.admit(HOST_B, FP_B)
    with pytest.raises(cp.PeerError) as e:
        store.admit(HOST_C, FP_C)
    assert e.value.reason == "too_many_peers"
    # an UPDATE to an existing peer still works at the cap
    assert store.admit(HOST_A, FP_A)["state"] == cp.PEER_ADMITTED


def test_endpoints_and_convoy_ids_are_bounded(store):
    """REJECTED, never silently truncated: _clean_list used to slice to
    the limit and write the truncation back, dropping an operator's
    hand-added entries with no error while every sibling validator
    raises. The new contract matches the siblings: over-limit refuses."""
    for kwargs in ({"endpoints": [f"10.0.0.{i}:47600" for i in range(64)]},
                   {"convoy_ids": [f"cv{i}" for i in range(64)]}):
        with pytest.raises(cp.PeerError) as e:
            store.admit(HOST_A, FP_A, **kwargs)
        assert e.value.reason == "malformed_list"
    # ... and a refused admit must not have half-landed
    assert store.get(HOST_A) is None
    # at the limit, both land verbatim
    record = store.admit(
        HOST_A, FP_A,
        endpoints=[f"10.0.0.{i}:47600" for i in range(cp.MAX_ENDPOINTS)],
        convoy_ids=[f"cv{i}" for i in range(cp.MAX_CONVOY_IDS)])
    assert len(record["endpoints"]) == cp.MAX_ENDPOINTS
    assert len(record["convoy_ids"]) == cp.MAX_CONVOY_IDS


def test_a_non_finite_clock_offset_is_refused(store):
    """NaN is the trap: `nan > threshold` and `nan <= threshold` are BOTH
    False, so it slips past every guard downstream."""
    with pytest.raises(cp.PeerError):
        store.admit(HOST_A, FP_A, clock_offset_s=float("nan"))
    with pytest.raises(cp.PeerError):
        store.admit(HOST_A, FP_A, clock_offset_s=float("inf"))


# -- peers.json: unreadable is not absent -----------------------------

def test_an_unreadable_peers_file_admits_nobody(tmp_path):
    directory = str(tmp_path / "state")
    store = cp.PeerStore(directory)
    store.admit(HOST_A, FP_A)
    with open(os.path.join(directory, cp.PEERS_FILE), "w",
              encoding="utf-8") as f:
        f.write("{ truncated")

    reloaded = cp.PeerStore(directory)
    assert reloaded.unreadable
    decision = reloaded.authorize_peer(HOST_A, FP_A)
    assert decision.allowed is False
    assert decision.reason == cp.REASON_UNKNOWN
    assert "cannot read its own peer records" in decision.detail


def test_an_unreadable_peers_file_is_never_overwritten(tmp_path):
    """Rewriting it would silently drop every admission AND every block
    it held -- fail-open one layer up from the denylist."""
    directory = str(tmp_path / "state")
    path = os.path.join(directory, cp.PEERS_FILE)
    cp.PeerStore(directory).admit(HOST_A, FP_A)
    with open(path, "w", encoding="utf-8") as f:
        f.write("{ truncated")
    original = open(path, encoding="utf-8").read()

    reloaded = cp.PeerStore(directory)
    for call in (lambda: reloaded.admit(HOST_B, FP_B),
                 # RELEASING the killswitch is a real change and still
                 # refuses; ENGAGING it is already true in effect and is
                 # answered rather than raised at an operator mid-incident
                 # (it writes nothing either, as the file check proves).
                 lambda: reloaded.set_killswitch(False)):
        with pytest.raises(cp.PeerStoreUnreadable):
            call()
    assert reloaded.set_killswitch(True)["engaged"] is True
    assert open(path, encoding="utf-8").read() == original


def test_one_malformed_record_makes_the_whole_store_unreadable(tmp_path):
    """Skipping a bad record is fail-OPEN the moment the skipped record
    is a BLOCKED one."""
    directory = str(tmp_path / "state")
    store = cp.PeerStore(directory)
    store.admit(HOST_A, FP_A)
    store.admit(HOST_B, FP_B)
    store.block(HOST_B)
    path = os.path.join(directory, cp.PEERS_FILE)
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    data["peers"][HOST_B]["state"] = "totally_fine_honest"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)

    reloaded = cp.PeerStore(directory)
    assert reloaded.unreadable
    assert reloaded.authorize_peer(HOST_A, FP_A).allowed is False, (
        "a store holding one record we cannot parse admits NOBODY -- the "
        "unparsed record might be the block")


def test_a_newer_peers_version_refuses_rather_than_guessing(tmp_path):
    directory = str(tmp_path / "state")
    cp.PeerStore(directory).admit(HOST_A, FP_A)
    path = os.path.join(directory, cp.PEERS_FILE)
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    data["version"] = cp.PEERS_VERSION + 5
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    reloaded = cp.PeerStore(directory)
    assert reloaded.unreadable
    assert reloaded.authorize_peer(HOST_A, FP_A).allowed is False


# -- peers.json: absent is not a membership decision ------------------

def test_an_absent_peers_file_refuses_REVERSIBLY(tmp_path):
    """A DELETED (or AV-quarantined) store looks exactly like a fresh
    host's absent one -- and queued peer work proves an admission was
    once granted. The refusal must be reversible (defer), like
    unreadable: burning it hands a file deletion the authority of a
    membership decision nobody took."""
    directory = str(tmp_path / "state")
    store = cp.PeerStore(directory)
    store.admit(HOST_A, FP_A)
    os.remove(os.path.join(directory, cp.PEERS_FILE))

    reloaded = cp.PeerStore(directory)
    assert reloaded.absent and not reloaded.unreadable
    decision = reloaded.authorize_peer(HOST_A, FP_A)
    assert decision.allowed is False
    assert decision.reason == cp.REASON_UNKNOWN
    assert decision.reversible is True
    assert "absent" in decision.detail


def test_a_forgotten_peer_is_refused_IRREVERSIBLY_file_still_present(store):
    """forget() REWRITES the file, so its refusal keeps the membership
    weight absence must not have."""
    store.admit(HOST_A, FP_A)
    store.forget(HOST_A)
    assert store.absent is False
    decision = store.authorize_peer(HOST_A, FP_A)
    assert decision.allowed is False and decision.reversible is False


def test_restoring_an_absent_store_restores_the_admission(tmp_path):
    directory = str(tmp_path / "state")
    store = cp.PeerStore(directory)
    store.admit(HOST_A, FP_A)
    path = os.path.join(directory, cp.PEERS_FILE)
    saved = open(path, encoding="utf-8").read()
    os.remove(path)
    reloaded = cp.PeerStore(directory)
    assert reloaded.authorize_peer(HOST_A, FP_A).allowed is False
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(saved)
    reloaded._signature = ("re-read",)
    restored = reloaded.authorize_peer(HOST_A, FP_A)
    assert restored.allowed is True
    assert restored.admission_id, "the restored record keeps its lineage"


# -- the admission lineage (admission_id) -----------------------------

def test_admission_id_is_minted_on_first_admission(store):
    record = store.admit(HOST_A, FP_A)
    assert record["admission_id"]
    decision = store.authorize_peer(HOST_A, FP_A)
    assert decision.admission_id == record["admission_id"]


def test_admission_id_survives_reaffirmation_and_observe_widening(store):
    """Re-admitting an unbroken lineage (metadata refresh, or widening
    an observe-only narrowing back to admitted) must NOT strand the
    peer's queued work -- the lineage is only broken by a revocation."""
    first = store.admit(HOST_A, FP_A)["admission_id"]
    # metadata refresh: same pin, still admitted
    assert store.admit(HOST_A, FP_A,
                       display_name="Studio B")["admission_id"] == first
    # narrow to observe-only: the decision still carries the lineage
    store.observe(HOST_A)
    assert store.authorize_peer(HOST_A, FP_A).admission_id == first
    # widen back to admitted: STILL the same lineage
    assert store.admit(HOST_A, FP_A)["admission_id"] == first


@pytest.mark.parametrize("break_lineage", [
    lambda s: s.block(HOST_A),
    lambda s: s.forget(HOST_A),
    lambda s: s.admit(HOST_A, FP_B),      # re-pin repudiates the old key
])
def test_admission_id_changes_across_every_revocation_shape(
        store, break_lineage):
    first = store.admit(HOST_A, FP_A)["admission_id"]
    break_lineage(store)
    second = store.admit(HOST_A, FP_A)["admission_id"]
    assert second and second != first, (
        "a lineage broken by a revocation must never match again")


def test_a_block_stamps_the_epoch_DURABLY_even_before_readmission(store):
    """Panel BLOCKER (spec-fidelity lens): the break must live on a
    DURABLE epoch stamped at block time, not be inferred from the
    record's transient state at re-admit time. If block only changed the
    STATE, a job's lineage would still match the record's until the
    re-admit -- and block -> observe -> admit could then launder it."""
    first = store.admit(HOST_A, FP_A)["admission_id"]
    blocked = store.block(HOST_A)
    assert blocked["admission_id"] and blocked["admission_id"] != first, (
        "block must mint a fresh epoch immediately, so outstanding "
        "old-lineage work is stale from the block onward")


def test_block_then_observe_then_readmit_does_NOT_restore_the_old_lineage(
        store):
    """Panel BLOCKER: block -> observe -> admit laundered the blocked
    state into an 'unbroken' observe_only one, and the re-admit PRESERVED
    the pre-block admission_id -- resurrecting pre-block work. The block
    epoch must survive the laundering."""
    pre_block = store.admit(HOST_A, FP_A)["admission_id"]
    store.block(HOST_A)                       # mints a fresh epoch
    store.observe(HOST_A)                      # a narrowing, NOT a break
    readmitted = store.admit(HOST_A, FP_A)["admission_id"]
    assert readmitted != pre_block, (
        "block -> observe -> admit must NOT resurrect the pre-block "
        "lineage: %r == %r" % (readmitted, pre_block))


def test_observe_narrowing_alone_preserves_the_lineage(store):
    """The counterweight to the block case: observe on its own is a
    narrowing, never a break -- a read submitted while admitted must keep
    dispatching across it (24.6), so the lineage is preserved."""
    first = store.admit(HOST_A, FP_A)["admission_id"]
    observed = store.observe(HOST_A)
    assert observed["admission_id"] == first
    assert store.authorize_peer(HOST_A, FP_A).admission_id == first


def test_record_peer_mints_a_fresh_lineage_not_the_blank_None(store):
    """Confirming-panel resurrection-hunt lens. record_peer recreates a
    PENDING record; the mint used to fire only on the ADMITTED
    transition, so a re-recorded peer carried the blank None. That None
    COLLIDES with a forgotten peer's None-lineage jobs (a laundered full
    revocation). A brand-new in-process record must always mint."""
    rec = store.record_peer(HOST_A, FP_A)
    assert rec["state"] == cp.PEER_PENDING
    assert rec["admission_id"], "a recreated PENDING record must mint an id"


def test_forget_cannot_be_laundered_back_via_record_peer_observe_admit(
        tmp_path):
    """Confirming-panel MAJOR (latent). A forgotten LEGACY None-lineage
    peer, recreated via record_peer -> observe -> admit, must NOT come
    back carrying None -- that None would match the forgotten peer's
    pre-revocation jobs and resurrect them. forget is a FULL revocation;
    the re-admit must mint a fresh epoch."""
    directory = str(tmp_path / "state")
    store = cp.PeerStore(directory)
    store.admit(HOST_A, FP_A)
    # Downgrade to a pre-fix legacy record (admission_id absent on disk).
    path = os.path.join(directory, cp.PEERS_FILE)
    data = json.load(open(path, encoding="utf-8"))
    data["peers"][HOST_A].pop("admission_id", None)
    json.dump(data, open(path, "w", encoding="utf-8"))
    store = cp.PeerStore(directory)
    assert store.get(HOST_A)["admission_id"] is None
    # ATTACK: forget -> record_peer -> observe -> admit
    store.forget(HOST_A)
    store.record_peer(HOST_A, FP_A)
    store.observe(HOST_A)
    final = store.admit(HOST_A, FP_A)
    assert final["admission_id"] is not None, (
        "forget was laundered: the re-admitted record carries the blank "
        "None that collides with the forgotten peer's None jobs")
    assert store.authorize_peer(HOST_A, FP_A).admission_id is not None


def test_a_legacy_None_lineage_record_is_not_burned_by_a_reaffirm(tmp_path):
    """Panel MAJOR (edge-cases lens): a record predating admission_id
    (loaded with None) must NOT have a fresh id minted on a routine
    same-pin re-affirm -- doing so burned all of that peer's legitimate
    None-lineage work as stale with no revocation ever taken."""
    directory = str(tmp_path / "state")
    store = cp.PeerStore(directory)
    store.admit(HOST_A, FP_A)
    # Simulate a pre-fix record: strip admission_id off disk, reload.
    path = os.path.join(directory, cp.PEERS_FILE)
    data = json.load(open(path, encoding="utf-8"))
    data["peers"][HOST_A].pop("admission_id", None)
    json.dump(data, open(path, "w", encoding="utf-8"))
    reloaded = cp.PeerStore(directory)
    assert reloaded.get(HOST_A)["admission_id"] is None
    # a routine no-op re-affirm (same pin, already admitted) PRESERVES None
    again = reloaded.admit(HOST_A, FP_A, display_name="refresh")
    assert again["admission_id"] is None, (
        "a benign re-affirm of a legacy record must not mint an id -- "
        "that burns its outstanding None-lineage work")
    # ... but a REAL revocation of that legacy record DOES mint an epoch,
    # which is what makes its None work stale
    reloaded.block(HOST_A)
    reblocked = reloaded.get(HOST_A)
    assert reblocked["admission_id"], (
        "a real revocation of a None-lineage peer must stamp an epoch")


def test_an_absent_peers_file_is_an_empty_store_not_a_failure(tmp_path):
    store = cp.PeerStore(str(tmp_path / "state"))
    assert store.unreadable is None
    assert store.peers() == []
    assert store.admit(HOST_A, FP_A)["state"] == cp.PEER_ADMITTED


# -- the audit sink may never alter a decision ------------------------

def test_an_exploding_audit_sink_changes_nothing(tmp_path):
    def explode(event, detail):
        raise RuntimeError("the trail is on fire")

    store = cp.PeerStore(str(tmp_path / "state"), audit=explode)
    record = store.admit(HOST_A, FP_A)
    assert record["state"] == cp.PEER_ADMITTED
    assert store.authorize_peer(HOST_A, FP_A).allowed is True
    store.block(HOST_A)
    assert store.authorize_peer(HOST_A, FP_A).reason == cp.REASON_BLOCKED
    # ... and it is still on disk, which is what the audit could not
    # have been allowed to prevent
    assert cp.PeerStore(str(tmp_path / "state")).get(HOST_A)["state"] == \
        cp.PEER_BLOCKED


def test_authorize_peer_writes_no_audit_at_all(tmp_path):
    """The decision is PURE. It runs on the hot re-check path, so an
    audit line per call would let one revoked peer's queue grow
    audit.jsonl without bound -- and a raising sink could then change a
    refusal."""
    seen = []
    store = cp.PeerStore(str(tmp_path / "state"),
                         audit=lambda e, d: seen.append(e))
    store.admit(HOST_A, FP_A)
    seen.clear()
    for _ in range(20):
        store.authorize_peer(HOST_A, FP_A)
        store.authorize_peer(HOST_B, FP_B)
        store.authorize_peer(HOST_A, FP_C)
    assert seen == []


def test_touch_seen_never_raises_and_never_decides(store, monkeypatch):
    """last_seen is an operator convenience. A failed write of it must
    never be able to interrupt a request -- so it reports failure, it
    does not raise it."""
    store.admit(HOST_A, FP_A)
    assert store.touch_seen(HOST_A, when=1234.5) is True
    assert store.get(HOST_A)["last_seen"] == 1234.5
    # unknown / malformed / unreadable: all False, none of them raise
    assert store.touch_seen(HOST_C) is False
    assert store.touch_seen("nonsense") is False

    def boom(*a, **kw):
        raise OSError("the disk is full")

    monkeypatch.setattr(cp.platform_mod, "_write_private", boom)
    assert store.touch_seen(HOST_A) is False, (
        "a failed last_seen write is reported, never raised")
    monkeypatch.undo()
    # and it changed no decision at all
    assert store.authorize_peer(HOST_A, FP_A).allowed is True


def test_an_unreadable_store_audits_on_transition_not_every_read(tmp_path):
    """peers.json is re-read on a revalidation window now, so auditing
    every failed load would grow audit.jsonl forever while the file stays
    broken -- the unbounded-trail trap the drain path already avoids."""
    directory = str(tmp_path / "state")
    cp.PeerStore(directory).admit(HOST_A, FP_A)
    path = os.path.join(directory, cp.PEERS_FILE)
    good = open(path, encoding="utf-8").read()
    with open(path, "w", encoding="utf-8") as f:
        f.write("{ truncated")

    seen = []
    store = cp.PeerStore(directory, audit=lambda e, d: seen.append(e))
    for _ in range(10):
        store._signature = cp._UNREAD          # force a reload each time
        store.authorize_peer(HOST_A, FP_A)
    assert seen.count("peers_unreadable") == 1

    # ... and a repair, then a fresh break, IS audited again
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(good)
    store._signature = cp._UNREAD
    assert store.authorize_peer(HOST_A, FP_A).allowed is True
    with open(path, "w", encoding="utf-8") as f:
        f.write("{ truncated")
    store._signature = cp._UNREAD
    store.authorize_peer(HOST_A, FP_A)
    assert seen.count("peers_unreadable") == 2


def test_pinned_fingerprint_never_invents_one(store):
    assert store.pinned_fingerprint(HOST_A) is None
    store.admit(HOST_A, FP_A)
    assert store.pinned_fingerprint(HOST_A) == FP_A
    assert store.pinned_fingerprint("nonsense") is None


def test_no_network_module_is_imported_here():
    """Slice 2 is memory and decision. A socket, an ssl context or an
    http client appearing in this module means slice 3 leaked backwards.
    """
    import ast
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "convoy_peers.py")
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    forbidden = {"socket", "ssl", "http", "urllib", "asyncio",
                 "socketserver", "select", "requests"}
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            found.add((node.module or "").split(".")[0])
    assert not (found & forbidden), f"network imports leaked in: {found}"
