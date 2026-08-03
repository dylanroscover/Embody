"""Production envelope (A-21 field model): signer seam, tamper-evidence,
named refusals. Pure stdlib, no TD, no network."""

import pytest

import convoy_protocol as cp

CONVOY = "studio"
HOST_A = "host-a"
CTRL = "controller-1"
NODE_B = "node-b"


def signer():
    return cp.HmacSigner("pre-shared-key")


def _envelope(**kw):
    kw.setdefault("convoy_id", CONVOY)
    kw.setdefault("origin_host_id", HOST_A)
    kw.setdefault("controller_id", CTRL)
    kw.setdefault("target_node_id", NODE_B)
    kw.setdefault("operation", "convoy_ping")
    kw.setdefault("signer", signer())
    return cp.build_envelope(**kw)


def _verify(env, **kw):
    kw.setdefault("signer", signer())
    kw.setdefault("convoy_id", CONVOY)
    kw.setdefault("my_node_id", NODE_B)
    return cp.verify_envelope(env, **kw)


# -- A-21 field model -----------------------------------------------

def test_round_trip_verifies():
    _verify(_envelope())


def test_verified_deadline_becomes_target_local_monotonic_budget():
    env = _envelope(timeout_s=30.0, now=1000.0)
    timing = _verify(env, now=1010.0, monotonic_now=500.0)
    assert timing == {
        "request_deadline_unix": 1150.0,
        "signed_deadline_unix": 1030.0,
        "accepted_at_unix": 1010.0,
        "accepted_remaining_s": 30.0,
        "accepted_expires_unix": 1040.0,
        "accepted_deadline_monotonic": 530.0,
    }


def test_replaying_an_envelope_does_not_refresh_its_signed_budget():
    env = _envelope(timeout_s=30.0, now=1000.0)
    first = _verify(env, now=1140.0, monotonic_now=50.0)
    replay = _verify(env, now=1145.0, monotonic_now=900.0)
    assert first["accepted_remaining_s"] == 10.0
    assert replay["accepted_remaining_s"] == 5.0
    assert first["accepted_expires_unix"] == 1150.0
    assert replay["accepted_expires_unix"] == 1150.0


@pytest.mark.parametrize("receiver_now", [880.0, 1120.0])
def test_fresh_request_accepts_full_budget_at_either_clock_skew_limit(
        receiver_now):
    env = _envelope(timeout_s=30.0, now=1000.0)
    timing = _verify(env, now=receiver_now, monotonic_now=500.0)
    assert timing["accepted_remaining_s"] == 30.0
    assert timing["accepted_deadline_monotonic"] == 530.0


def test_deadline_rounding_does_not_shave_budget_at_skew_limit():
    created = 1000.0004
    budget = 30.0004
    env = _envelope(timeout_s=budget, now=created)
    timing = _verify(
        env, now=created + cp.MAX_CLOCK_SKEW_S, monotonic_now=500.0)
    assert timing["accepted_remaining_s"] == pytest.approx(budget)
    assert timing["accepted_deadline_monotonic"] == pytest.approx(
        500.0 + budget)


def test_receiver_more_than_skew_behind_refuses_future_timestamp():
    env = _envelope(timeout_s=30.0, now=1000.0)
    with pytest.raises(cp.EnvelopeRejected) as e:
        _verify(env, now=879.999)
    assert e.value.reason == "timestamp_out_of_window"


def test_replay_past_deadline_plus_skew_is_refused():
    env = _envelope(timeout_s=30.0, now=1000.0)
    with pytest.raises(cp.EnvelopeRejected) as e:
        _verify(env, now=1150.001)
    assert e.value.reason == "deadline_exceeded"


def test_wall_clock_jump_after_admission_cannot_change_monotonic_deadline(
        monkeypatch):
    env = _envelope(timeout_s=30.0, now=1000.0)
    wall_reads = iter((1010.0, 9999999999.0))
    monkeypatch.setattr(cp.time, "time", lambda: next(wall_reads))
    timing = _verify(env, monotonic_now=500.0)
    assert timing["accepted_deadline_monotonic"] == 530.0
    # A later wall-clock jump cannot mutate the already-admitted anchor.
    assert cp.time.time() == 9999999999.0
    assert timing["accepted_deadline_monotonic"] == 530.0


def test_a21_signs_controller_and_origin_host():
    """A HOST signs, on behalf of a CONTROLLER, targeting a NODE."""
    env = _envelope()
    assert env["origin_host_id"] == HOST_A
    assert env["source_host_id"] == HOST_A
    assert env["controller_id"] == CTRL
    assert env["target_node_id"] == NODE_B
    for f in ("controller_id", "origin_host_id", "source_host_id",
              "target_node_id"):
        assert f in cp._SIGNED_FIELDS, f + " must be signed (A-21)"


def test_algorithm_tag_is_signed():
    assert _envelope()["sig_alg"] == cp.ALG_HMAC_SHA256
    assert "sig_alg" in cp._SIGNED_FIELDS


def test_duration_and_creation_time_are_signed():
    assert "created_unix" in cp._SIGNED_FIELDS
    assert "budget_s" in cp._SIGNED_FIELDS


def test_deadline_is_absolute():
    env = _envelope(timeout_s=30.0, now=1000.0)
    assert env["created_unix"] == 1000.0
    assert env["budget_s"] == 30.0
    assert env["deadline_unix"] == 1030.0
    assert cp.remaining_budget(env, now=1020.0) == pytest.approx(10.0)
    assert cp.remaining_budget(env, now=9999.0) == 0.0


def test_sender_budget_is_cumulative_on_its_own_clock_and_rollback_capped():
    env = _envelope(timeout_s=30.0, now=1000.0)
    assert cp.remaining_budget(env, now=1005.0) == 25.0
    assert cp.remaining_budget(env, now=1025.0) == 5.0
    assert cp.remaining_budget(env, now=900.0) == 30.0


def test_no_job_id_in_the_request():
    assert "job_id" not in _envelope()


# -- pluggable signer -----------------------------------------------

def test_a_different_psk_does_not_verify():
    with pytest.raises(cp.EnvelopeRejected) as e:
        _verify(_envelope(), signer=cp.HmacSigner("WRONG"))
    assert e.value.reason == "bad_signature"


def test_algorithm_mismatch_is_its_own_reason():
    env = _envelope()
    env["sig_alg"] = "keypair-ed25519"
    with pytest.raises(cp.EnvelopeRejected) as e:
        _verify(env)
    assert e.value.reason == "algorithm_mismatch"


def test_non_ascii_signature_is_refused_not_crashed():
    env = _envelope()
    env["signature"] = "\u00ff\u00fe"
    with pytest.raises(cp.EnvelopeRejected) as e:
        _verify(env)
    assert e.value.reason == "bad_signature"


def test_hmac_signer_requires_a_key():
    with pytest.raises(ValueError):
        cp.HmacSigner("")


# -- tamper evidence ------------------------------------------------

@pytest.mark.parametrize("field,value", [
    ("operation", "execute_python"),
    ("target_node_id", "node-c"),
    ("origin_host_id", "attacker-host"),
    ("controller_id", "attacker-controller"),
    ("created_unix", 123.0),
    ("budget_s", 999.0),
    ("deadline_unix", 9999999999.0),
    ("expected_runtime_id", "someone-elses-runtime"),
    ("hop_limit", 99),
    ("sig_alg", "hmac-sha256-but-lying"),
])
def test_every_signed_field_is_tamper_evident(field, value):
    env = _envelope(expected_runtime_id="rt-1")
    env[field] = value
    with pytest.raises(cp.EnvelopeRejected) as e:
        _verify(env, my_node_id=env.get("target_node_id"),
                my_runtime_id="rt-1")
    assert e.value.reason in ("bad_signature", "algorithm_mismatch")


def test_swapped_arguments_caught_even_with_valid_signature():
    env = _envelope(operation="convoy_ping", arguments={"echo": "hi"})
    env["arguments"] = {"code": "rm -rf /"}
    with pytest.raises(cp.EnvelopeRejected) as e:
        _verify(env)
    assert e.value.reason == "arguments_tampered"


def test_argument_digest_is_order_independent():
    a = cp.canonical_arguments_digest({"x": 1, "y": {"b": 2, "a": 3}})
    b = cp.canonical_arguments_digest({"y": {"a": 3, "b": 2}, "x": 1})
    assert a == b


# -- routing / preconditions ----------------------------------------

def test_foreign_convoy_rejected():
    with pytest.raises(cp.EnvelopeRejected) as e:
        _verify(_envelope(), convoy_id="other")
    assert e.value.reason == "namespace_mismatch"


def test_wrong_target_rejected():
    with pytest.raises(cp.EnvelopeRejected) as e:
        _verify(_envelope(), my_node_id="node-z")
    assert e.value.reason == "wrong_target"


def test_relayed_envelope_rejected_in_v1():
    env = cp.build_envelope(
        convoy_id=CONVOY, origin_host_id=HOST_A, controller_id=CTRL,
        target_node_id=NODE_B, operation="convoy_ping", signer=signer(),
        source_host_id="middleman-host")
    with pytest.raises(cp.EnvelopeRejected) as e:
        _verify(env)
    assert e.value.reason == "hop_limit_exceeded"


def test_expired_deadline_rejected():
    env = _envelope(timeout_s=30.0, now=1000.0)
    with pytest.raises(cp.EnvelopeRejected) as e:
        _verify(env, now=1150.0)
    assert e.value.reason == "deadline_exceeded"


def test_excessive_signed_budget_is_a_named_refusal():
    env = _envelope(now=1000.0)
    env["budget_s"] = cp.MAX_DEADLINE_HORIZON_S + 0.001
    env["deadline_unix"] = env["created_unix"] + env["budget_s"]
    env["signature"] = signer().sign(cp._signing_payload(env))
    with pytest.raises(cp.EnvelopeRejected) as e:
        _verify(env, now=1000.0)
    assert e.value.reason == "deadline_too_far"


@pytest.mark.parametrize("bad", [
    0, -1, cp.MAX_DEADLINE_HORIZON_S + 0.001,
    float("nan"), float("inf"), True, "not-a-number",
])
def test_builder_refuses_invalid_budget(bad):
    with pytest.raises(ValueError):
        _envelope(timeout_s=bad, now=1000.0)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1, True])
def test_builder_refuses_invalid_creation_time(bad):
    with pytest.raises(ValueError):
        _envelope(now=bad)


def test_resigned_inconsistent_deadline_is_refused():
    env = _envelope(timeout_s=30.0, now=1000.0)
    env["deadline_unix"] += 0.01
    env["signature"] = signer().sign(cp._signing_payload(env))
    with pytest.raises(cp.EnvelopeRejected) as e:
        _verify(env, now=1000.0)
    assert e.value.reason == "deadline_mismatch"


@pytest.mark.parametrize("field", ["created_unix", "budget_s"])
def test_resigned_missing_duration_field_is_a_named_refusal(field):
    env = _envelope(timeout_s=30.0, now=1000.0)
    del env[field]
    env["signature"] = signer().sign(cp._signing_payload(env))
    with pytest.raises(cp.EnvelopeRejected) as e:
        _verify(env, now=1000.0)
    assert e.value.reason == "malformed"


@pytest.mark.parametrize("bad", [0, -1, cp.MAX_HOP_LIMIT + 1, 1.5, True])
def test_malformed_or_excessive_hop_limit_is_refused(bad):
    env = _envelope()
    env["hop_limit"] = bad
    env["signature"] = signer().sign(cp._signing_payload(env))
    with pytest.raises(cp.EnvelopeRejected) as e:
        _verify(env)
    assert e.value.reason in ("malformed", "hop_limit_exceeded")


# -- A-22 runtime precondition (the fail-open fix) ------------------

def test_runtime_precondition_rejects_a_restarted_target():
    env = _envelope(expected_runtime_id="rt-before")
    with pytest.raises(cp.EnvelopeRejected) as e:
        _verify(env, my_runtime_id="rt-after")
    assert e.value.reason == "runtime_changed"


def test_runtime_precondition_passes_when_it_matches():
    _verify(_envelope(expected_runtime_id="rt-1"), my_runtime_id="rt-1")


def test_a_set_precondition_with_no_target_runtime_fails_CLOSED():
    """The fail-open the panel caught: expected_runtime_id set but the
    target passes my_runtime_id=None must be REFUSED, not waved through."""
    env = _envelope(expected_runtime_id="rt-1")
    with pytest.raises(cp.EnvelopeRejected) as e:
        _verify(env, my_runtime_id=None)
    assert e.value.reason == "runtime_unverifiable"


def test_absent_precondition_is_allowed_only_when_not_required():
    _verify(_envelope(), my_runtime_id="whatever")


def test_runtime_required_op_without_the_field_is_refused():
    """A-22: restart/RMW/exclusive/wake ops MUST carry the field."""
    env = _envelope(operation="restart")
    with pytest.raises(cp.EnvelopeRejected) as e:
        _verify(env, my_runtime_id="rt-1", runtime_required=True)
    assert e.value.reason == "runtime_id_required"


# -- named refusals, never raw exceptions ---------------------------

def test_a_nonnumeric_deadline_is_a_named_refusal_not_a_valueerror():
    """The 'every refusal is named' contract: a signed-but-garbage
    deadline must be EnvelopeRejected, not a raw ValueError."""
    s = signer()
    env = cp.build_envelope(convoy_id=CONVOY, origin_host_id=HOST_A,
                            controller_id=CTRL, target_node_id=NODE_B,
                            operation="x", signer=s)
    env["deadline_unix"] = "not-a-number"
    env["signature"] = s.sign(cp._signing_payload(env))   # re-sign the lie
    with pytest.raises(cp.EnvelopeRejected) as e:
        _verify(env)
    assert e.value.reason == "malformed"


def test_remaining_budget_never_raises_on_garbage():
    assert cp.remaining_budget({"deadline_unix": "nope"}) == 0.0
    assert cp.remaining_budget({}) == 0.0


def test_protocol_mismatch_rejected():
    env = _envelope()
    env["protocol"] = "convoy/99"
    with pytest.raises(cp.EnvelopeRejected) as e:
        _verify(env)
    assert e.value.reason == "protocol_mismatch"


@pytest.mark.parametrize("field", [
    "convoy_id", "request_id", "idempotency_key", "controller_id", "origin_host_id",
    "source_host_id", "target_node_id", "operation",
])
def test_missing_required_field_rejected(field):
    env = _envelope()
    del env[field]
    with pytest.raises(cp.EnvelopeRejected) as e:
        _verify(env)
    assert e.value.reason in ("malformed", "bad_signature")


def test_non_object_rejected():
    with pytest.raises(cp.EnvelopeRejected) as e:
        _verify(["nope"])
    assert e.value.reason == "malformed"


# -- panel regression (2026-07-31): non-finite timing -----------------

@pytest.mark.parametrize("field", [
    "created_unix", "budget_s", "deadline_unix",
])
@pytest.mark.parametrize("bad", [float("nan"), float("inf"),
                                 float("-inf"), True, "garbage"])
def test_malformed_signed_timing_field_is_refused(field, bad):
    """PROVEN FAIL-OPEN: every comparison against NaN is False, so
    `deadline <= now` never fired and a signed NaN deadline produced a
    request that could never expire. Infinity is the same guarantee
    broken the honest way. JSON's NaN/Infinity tokens make both
    reachable over the wire, so the verifier -- not just the transport
    -- must refuse them."""
    env = _envelope()
    env[field] = bad
    env["signature"] = signer().sign(cp._signing_payload(env))
    with pytest.raises(cp.EnvelopeRejected) as e:
        _verify(env)
    assert e.value.reason == "malformed"


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_remaining_budget_of_a_non_finite_deadline_is_zero(bad):
    """A non-finite budget reads as 'plenty of time' to every downstream
    timeout comparison; no budget is the safe reading."""
    env = _envelope(now=1000.0)
    env["deadline_unix"] = bad
    assert cp.remaining_budget(env, now=1000.0) == 0.0
