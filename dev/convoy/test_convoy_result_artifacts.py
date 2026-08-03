"""Durable TD-result spill and remote capture materialization contracts."""

import base64
import json
import os
import tempfile
import uuid

import pytest

import convoy_artifacts as artifacts
from test_convoy_hostapp import Server


CONVOY = "studio"


@pytest.fixture
def server(tmp_path):
    value = Server(str(tmp_path / "state"))
    value.app.artifacts.free_space_floor_bytes = 0
    value.app.artifacts.max_artifact_bytes = 2 * 1024 * 1024
    yield value
    value.stop()


def register(server):
    code, node = server.call("/register", {
        "project_root": "/Work/result-artifacts", "convoy_id": CONVOY,
        "comp_path": "/Embody", "envoy_port": 9800,
    })
    assert code == 200
    return node


def enqueue(server, node, operation, arguments=None):
    code, body = server.call("/jobs", {
        "idempotency_key": uuid.uuid4().hex,
        "node_id": node["node_id"], "controller_id": "bridge-controller",
        "operation": operation, "arguments": arguments or {},
    })
    assert code == 200, body
    return body["job"]


def dispatch(server, job, result):
    server.app.forwarder = lambda _port, _operation, _arguments: {
        "ok": True, "result": result}
    code, body = server.call(
        "/dispatch", {"delivery_id": job["delivery_id"]})
    assert code == 200, body
    assert body["job"]["state"] == "succeeded"
    return body["job"]["result"]


def read_artifact(server, reference):
    code, payload, lease, _headers = server.app.artifact_open_local_download(
        reference["convoy_id"], reference["artifact_id"])
    assert code == 200 and payload is None
    try:
        return b"".join(lease)
    finally:
        lease.close()


def test_capture_path_becomes_private_artifact_and_remote_path_is_removed(
        server):
    node = register(server)
    job = enqueue(server, node, "capture_top", {"op_path": "/out"})
    image = b"\x89PNG\r\n\x1a\nverified-capture"
    path = os.path.join(
        tempfile.gettempdir(), "envoy_capture_%s.png" % uuid.uuid4().hex[:8])
    with open(path, "wb") as output:
        output.write(image)
    result = dispatch(
        server, job,
        "TOP capture: 64x64 -> 64x64 PNG\nSaved to: %s\n"
        "Quality: OK (max_lum=1.0, std=0.2)\n"
        "(Use Read tool on the file path above to view the image)" % path)

    assert result["capture"] is True
    assert result["spilled"] is True
    assert "Saved to:" not in result["detail"]
    assert "Quality: OK" in result["detail"]
    assert not os.path.exists(path)
    reference = result["artifact"]
    assert reference["mime_type"] == "image/png"
    assert reference["host_id"] == server.app.host_id
    assert reference["node_id"] == node["node_id"]
    assert reference["controller_id"] == "bridge-controller"
    assert reference["job_id"] == job["delivery_id"]
    assert read_artifact(server, reference) == image


def test_oversized_td_json_result_spills_without_truncating_source(server):
    node = register(server)
    job = enqueue(server, node, "query_network", {"parent_path": "/"})
    original = {"ops": ["/node-%04d" % index for index in range(9000)]}
    result = dispatch(server, job, original)
    assert result["spilled"] is True
    assert result["result_bytes"] > 56 * 1024
    reference = result["artifact"]
    assert reference["mime_type"] == "application/json"
    assert json.loads(read_artifact(server, reference).decode("utf-8")) == original
    with pytest.raises(artifacts.ArtifactProtected):
        server.app.artifacts.delete(CONVOY, reference["artifact_id"])

    code, acknowledged = server.call(
        "/jobs/ack", {"delivery_id": job["delivery_id"]})
    assert code == 200 and acknowledged["ok"] is True
    assert acknowledged["artifact_protection_released"] is True
    assert acknowledged["wakes_touchdesigner"] is False
    assert server.app.artifacts.delete(
        CONVOY, reference["artifact_id"]) == reference["size"]


def test_job_ack_is_terminal_owner_evidence_and_idempotent(server):
    node = register(server)
    job = enqueue(server, node, "query_network", {"parent_path": "/"})
    code, refusal = server.call(
        "/jobs/ack", {"delivery_id": job["delivery_id"]})
    assert code == 409 and refusal["reason"] == "job_not_terminal"

    dispatch(server, job, {"small": True})
    code, first = server.call(
        "/jobs/ack", {"delivery_id": job["delivery_id"]})
    assert code == 200 and first["already_acknowledged"] is False
    code, second = server.call(
        "/jobs/ack", {"delivery_id": job["delivery_id"]})
    assert code == 200 and second["already_acknowledged"] is True
    assert second["acknowledged_at"] == first["acknowledged_at"]


def test_capture_never_reads_or_deletes_a_path_outside_temp_root(
        server, tmp_path):
    node = register(server)
    job = enqueue(server, node, "capture_top", {"op_path": "/out"})
    outside = tmp_path / ("envoy_capture_%s.png" % uuid.uuid4().hex[:8])
    outside.write_bytes(b"\x89PNG\r\n\x1a\nprivate")
    result = dispatch(
        server, job, "TOP capture\nSaved to: %s" % outside)
    assert result["capture"] is True
    assert result["artifact_unavailable"] is True
    assert outside.read_bytes().endswith(b"private")


def test_inline_capture_is_verified_fallback_when_path_is_unavailable(server):
    node = register(server)
    job = enqueue(server, node, "capture_top", {"op_path": "/out"})
    image = b"\xff\xd8small-jpeg\xff\xd9"
    result = dispatch(server, job, {"content": [
        {"type": "text", "text": (
            "TOP capture\nSaved to: C:/not-temp/envoy_capture_deadbeef.jpg")},
        {"type": "image", "mimeType": "image/jpeg",
         "data": base64.b64encode(image).decode("ascii")},
    ]})
    assert result["spilled"] is True
    assert result["artifact"]["mime_type"] == "image/jpeg"
    assert read_artifact(server, result["artifact"]) == image
