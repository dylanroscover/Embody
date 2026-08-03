"""HostApp wiring for local-only Convoy safety policy.

These tests exercise the authenticated HTTP surface because TD parameters are
projections, never authority.  There is intentionally no peer route for any
method in this file.
"""

import json

import pytest

import convoy_hostapp as hostapp
import convoy_policy as policy
from test_convoy_hostapp import Server


@pytest.fixture
def server(tmp_path):
    value = Server(str(tmp_path / "state"))
    yield value
    value.stop()


def register(server):
    code, body = server.call("/register", {
        "project_root": "/Work/p", "convoy_id": "studio",
        "comp_path": "/Embody", "runtime_id": "rt1",
        "envoy_port": 9800,
    })
    assert code == 200, body
    return body


def test_default_projection_is_fail_closed_and_host_private(server):
    node = register(server)
    code, body = server.call("/policy?node_id=" + node["node_id"])
    assert code == 200
    assert body["policy"] == {
        "generation": 0,
        "allow_td_python": False,
        "allow_full_shell": False,
        "artifact_quota_mb": 1024,
    }
    assert "td_python_node_ids" not in json.dumps(body)


def test_td_python_requires_one_shot_local_challenge(server):
    node = register(server)
    code, started = server.call("/policy/challenge", {
        "setting": "td_python", "node_id": node["node_id"],
        "expected_generation": 0,
    })
    assert code == 200
    challenge = started["challenge"]
    assert challenge["confirmation"].startswith(
        "ENABLE TD PYTHON " + node["node_id"])

    code, refused = server.call("/policy/confirm", {
        "challenge_id": challenge["challenge_id"],
        "confirmation": "wrong", "expected_generation": 0,
    })
    assert code == 403
    assert refused["reason"] == "policy_challenge_invalid"

    # A typo consumes the challenge.  A fresh local confirmation is needed.
    code, started = server.call("/policy/challenge", {
        "setting": "td_python", "node_id": node["node_id"],
        "expected_generation": 0,
    })
    challenge = started["challenge"]
    code, body = server.call("/policy/confirm", {
        "challenge_id": challenge["challenge_id"],
        "confirmation": challenge["confirmation"],
        "expected_generation": 0,
    })
    assert code == 200, body
    code, body = server.call("/policy?node_id=" + node["node_id"])
    assert body["policy"]["allow_td_python"] is True

    code, body = server.call("/policy/disable", {
        "setting": "td_python", "node_id": node["node_id"],
    })
    assert code == 200
    assert body["policy"]["allow_td_python"] is False


def test_legacy_directory_bit_cannot_bypass_policy(server):
    node = register(server)
    with server.app.lock:
        server.app.directory.approve_td_python(node["node_id"])
        code, body = server.app.create_job({
            "idempotency_key": "legacy-bit",
            "node_id": node["node_id"], "operation": "execute_python",
            "arguments": {"code": "result = 1"},
            "expected_runtime_id": node["runtime_id"],
        })
    assert code == 403
    assert body["reason"] == "td_python_not_approved"


def test_artifact_quota_is_cas_and_updates_live_store(server):
    register(server)
    code, body = server.call("/policy/artifact-quota", {
        "artifact_quota_mb": 0, "expected_generation": 0,
    })
    assert code == 200, body
    assert body["policy"]["artifact_quota_mb"] == 0
    assert body["artifacts"]["storage_enabled"] is False

    code, body = server.call("/policy/artifact-quota", {
        "artifact_quota_mb": 1, "expected_generation": 0,
    })
    assert code == 409
    assert body["reason"] == "policy_generation_conflict"


def test_policy_routes_require_loopback_token(server):
    node = register(server)
    for path, body in (
        ("/policy?node_id=" + node["node_id"], None),
        ("/policy/challenge", {"setting": "full_shell",
                               "expected_generation": 0}),
        ("/policy/disable", {"setting": "full_shell"}),
    ):
        code, payload = server.call(path, body, token=None)
        assert code == 401
        assert payload["reason"] == "unauthenticated"


def test_corrupt_or_legacy_permissive_policy_never_resets(tmp_path):
    directory = tmp_path / "state"
    directory.mkdir()
    path = directory / policy.POLICY_FILE
    path.write_text('{"allow_full_shell":true}', encoding="utf-8")
    with pytest.raises(policy.PolicyCorrupt):
        hostapp.HostApp(str(directory))
    assert path.read_text(encoding="utf-8") == '{"allow_full_shell":true}'

