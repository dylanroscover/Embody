"""A-23 compatibility digest and manifest. Pure, no I/O."""

import pytest

import convoy_capabilities as cc


def test_identical_operations_digest_identically_on_any_machine():
    a = cc.operation_digest("query_network",
                            schema={"parent_path": "str"},
                            gating={"mutating": False})
    b = cc.operation_digest("query_network",
                            schema={"parent_path": "str"},
                            gating={"mutating": False})
    assert a == b and a.startswith("op1-")


def test_a_schema_change_changes_the_digest():
    before = cc.operation_digest("set_parameter", schema={"value": "str"})
    after = cc.operation_digest("set_parameter",
                                schema={"value": "str", "expr": "str"})
    assert before != after


def test_a_gating_change_changes_the_digest():
    before = cc.operation_digest("delete_op", gating={"destructive": False})
    after = cc.operation_digest("delete_op", gating={"destructive": True})
    assert before != after


def test_cosmetic_metadata_is_not_in_the_digest():
    """Help text and ordering differ across builds without changing
    behaviour -- folding them in would fragment the mesh for no gain."""
    a = cc.operation_digest("cook_op", schema={"force": "bool"})
    b = cc.operation_digest("cook_op", schema={"force": "bool"})
    assert a == b


def test_manifest_round_trips_and_has_a_stable_digest():
    m = cc.CapabilityManifest("convoy/1", "node-b")
    m.add("query_network", cc.operation_digest("query_network"))
    m.add("capture_top", cc.operation_digest("capture_top"))
    d = m.to_dict()
    restored = cc.CapabilityManifest.from_dict(d)
    assert restored.manifest_digest() == m.manifest_digest()
    assert d["manifest_digest"].startswith("mf1-")


def test_manifest_digest_changes_when_an_operation_changes():
    m1 = cc.CapabilityManifest("convoy/1", "n")
    m1.add("x", "op1-aaaa")
    m2 = cc.CapabilityManifest("convoy/1", "n")
    m2.add("x", "op1-bbbb")
    assert m1.manifest_digest() != m2.manifest_digest()


# -- the check that actually protects a controller ------------------

def test_matching_digests_are_compatible():
    dig = cc.operation_digest("query_network")
    target = cc.CapabilityManifest("convoy/1", "b", {"query_network": dig})
    assert cc.check_compatible(dig, target, "query_network") is None


def test_a_mismatched_digest_is_refused_before_sending():
    target = cc.CapabilityManifest("convoy/1", "b",
                                   {"delete_op": "op1-newbuild"})
    with pytest.raises(cc.CompatibilityError) as e:
        cc.check_compatible("op1-oldbuild", target, "delete_op")
    assert e.value.reason == "incompatible_operation"


def test_an_operation_the_target_does_not_expose_is_refused():
    target = cc.CapabilityManifest("convoy/1", "b", {"query_network": "op1-x"})
    with pytest.raises(cc.CompatibilityError) as e:
        cc.check_compatible("op1-x", target, "execute_python")
    assert e.value.reason == "operation_not_exposed"
