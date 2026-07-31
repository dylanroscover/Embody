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


def test_deadline_is_absolute():
    env = _envelope(timeout_s=30.0, now=1000.0)
    assert env["deadline_unix"] == 1030.0
    assert cp.remaining_budget(env, now=1020.0) == pytest.approx(10.0)
    assert cp.remaining_budget(env, now=9999.0) == 0.0


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
    with pytest.raises(cp.EnvelopeRejected) as e:
        _verify(_envelope(timeout_s=-1.0))
    assert e.value.reason == "deadline_exceeded"


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
    "convoy_id", "request_id", "controller_id", "origin_host_id",
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


# -- panel regression (2026-07-31): non-finite deadlines --------------

@pytest.mark.parametrize("bad", [float("nan"), float("inf"),
                                 float("-inf")])
def test_non_finite_deadline_is_refused(bad):
    """PROVEN FAIL-OPEN: every comparison against NaN is False, so
    `deadline <= now` never fired and a signed NaN deadline produced a
    request that could never expire. Infinity is the same guarantee
    broken the honest way. JSON's NaN/Infinity tokens make both
    reachable over the wire, so the verifier -- not just the transport
    -- must refuse them."""
    env = _envelope()
    env["deadline_unix"] = bad
    env["signature"] = signer().sign(cp._signing_payload(env))
    with pytest.raises(cp.EnvelopeRejected) as e:
        _verify(env)
    assert e.value.reason == "malformed"
    assert "finite" in e.value.detail


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_remaining_budget_of_a_non_finite_deadline_is_zero(bad):
    """A non-finite budget reads as 'plenty of time' to every downstream
    timeout comparison; no budget is the safe reading."""
    assert cp.remaining_budget({"deadline_unix": bad}) == 0.0
