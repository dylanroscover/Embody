"""End-to-end HTTP transport contracts for Convoy artifacts.

The store has its own exhaustive suite.  These tests pin the security seams
that only exist once real HTTP framing, the IPC token and peer mTLS admission
are involved: raw bytes, bounded Content-Length, exact hashes, Range resume,
one-shot scoped capabilities, and structural separation of route tables.
"""

import hashlib
import http.client
import io
import json
import threading

import pytest

import convoy_artifact_http as artifact_http
import convoy_artifacts as artifacts
import convoy_hostapp as hostapp
import convoy_peerclient as peerclient
from test_convoy_peerserver import Mesh


CONVOY = "studio"
CONTROLLER = "ctl-artifacts"


class LocalServer:
    def __init__(self, data_dir=None, app=None):
        self.app = app or hostapp.HostApp(
            str(data_dir), artifact_cache_path=str(data_dir / "artifacts"))
        self.server, self.port = hostapp.serve(self.app, port=0)
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def request(self, method, path, body=None, headers=None, token=...):
        headers = dict(headers or {})
        token = self.app.token if token is ... else token
        if token is not None:
            headers[hostapp.TOKEN_HEADER] = token
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request(method, path, body=body, headers=headers)
            response = conn.getresponse()
            raw = response.read()
            return response.status, {
                key.lower(): value for key, value in response.getheaders()
            }, raw
        finally:
            conn.close()

    def register(self, convoy_id=CONVOY, comp="/Embody"):
        code, node = self.app.register_node({
            "project_root": "/Work/artifacts", "convoy_id": convoy_id,
            "comp_path": comp, "runtime_id": "rt-artifacts",
        })
        assert code == 200, node
        return node

    def stop(self, close_app=True):
        self.server.shutdown()
        self.server.server_close()
        if close_app:
            self.app.db.close()


@pytest.fixture
def local(tmp_path):
    server = LocalServer(tmp_path / "host")
    server.app.artifacts.free_space_floor_bytes = 0
    server.app.artifacts.max_artifact_bytes = 2 * 1024 * 1024
    yield server
    server.stop()


@pytest.fixture
def mesh(tmp_path):
    value = Mesh(tmp_path)
    value.b.artifacts.free_space_floor_bytes = 0
    value.b.artifacts.max_artifact_bytes = 2 * 1024 * 1024
    yield value
    value.stop()


def segment(convoy_id=CONVOY):
    return artifact_http.encode_convoy_segment(convoy_id)


def artifact_path(artifact_id=None, convoy_id=CONVOY, *, peer=False,
                  capability=False):
    prefix = (artifact_http.PEER_ROUTE_PREFIX if peer
              else artifact_http.LOCAL_ROUTE_PREFIX)
    path = prefix + segment(convoy_id)
    if artifact_id:
        path += "/" + artifact_id
    if capability:
        path += "/capability"
    return path


def upload_headers(node_id, value, controller=CONTROLLER,
                   mime_type="application/octet-stream", filename=None):
    result = {
        "Content-Length": str(len(value)),
        "Content-Type": mime_type,
        artifact_http.HEADER_SHA256: hashlib.sha256(value).hexdigest(),
        artifact_http.HEADER_NODE_ID: node_id,
        artifact_http.HEADER_CONTROLLER_ID: controller,
    }
    if filename:
        result[artifact_http.HEADER_FILENAME] = filename
    return result


def json_body(value):
    return json.dumps(value, separators=(",", ":")).encode("utf-8")


def decoded(raw):
    return json.loads(raw.decode("utf-8"))


def local_upload(server, node, value, **kwargs):
    status, headers, raw = server.request(
        "POST", artifact_path(), body=value,
        headers=upload_headers(node["node_id"], value, **kwargs))
    return status, headers, decoded(raw)


def peer_request(mesh, method, path, body=None, headers=None):
    target = mesh.target()
    context = peerclient.build_client_ssl_context(
        mesh.a.hostkeys, target.pinned_cert_pem)
    conn = http.client.HTTPSConnection(
        target.address, target.port, timeout=10, context=context)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        raw = response.read()
        return response.status, {
            key.lower(): value for key, value in response.getheaders()
        }, raw
    finally:
        conn.close()


def peer_upload(mesh, node_id, value, controller=CONTROLLER,
                convoy_id=CONVOY):
    status, headers, raw = peer_request(
        mesh, "POST", artifact_path(convoy_id=convoy_id, peer=True),
        body=value, headers=upload_headers(node_id, value, controller))
    return status, headers, decoded(raw)


def peer_grant(mesh, node_id, artifact_id, controller=CONTROLLER,
               convoy_id=CONVOY):
    body = json_body({"node_id": node_id, "controller_id": controller})
    status, headers, raw = peer_request(
        mesh, "POST",
        artifact_path(artifact_id, convoy_id, peer=True, capability=True),
        body=body, headers={"Content-Type": "application/json",
                            "Content-Length": str(len(body))})
    return status, headers, decoded(raw)


def peer_download(mesh, node_id, artifact_id, token,
                  controller=CONTROLLER, convoy_id=CONVOY, byte_range=None):
    headers = {
        artifact_http.HEADER_NODE_ID: node_id,
        artifact_http.HEADER_CONTROLLER_ID: controller,
        artifact_http.HEADER_CAPABILITY: token,
    }
    if byte_range is not None:
        headers["Range"] = byte_range
    return peer_request(
        mesh, "GET", artifact_path(artifact_id, convoy_id, peer=True),
        headers=headers)


# -- loopback: IPC token and raw byte framing -------------------------


def test_loopback_upload_and_range_download_are_raw_bytes(local):
    node = local.register()
    value = b"\x00\xff\x80raw-not-json\r\n" + bytes(range(64))
    status, upload_response_headers, body = local_upload(
        local, node, value, filename="capture.bin")
    assert status == 200 and body["ok"] is True
    assert upload_response_headers["connection"] == "close"
    reference = body["artifact"]
    assert reference["sha256"] == hashlib.sha256(value).hexdigest()
    assert reference["size"] == len(value)
    assert "path" not in reference

    status, headers, raw = local.request(
        "GET", artifact_path(reference["artifact_id"]))
    assert status == 200
    assert raw == value
    assert headers["content-type"] == "application/octet-stream"
    assert headers["content-length"] == str(len(value))
    assert headers["accept-ranges"] == "bytes"
    assert headers["x-convoy-content-sha256"] == reference["sha256"]

    status, headers, raw = local.request(
        "GET", artifact_path(reference["artifact_id"]),
        headers={"Range": "bytes=3-17"})
    assert status == 206 and raw == value[3:18]
    assert headers["content-range"] == f"bytes 3-17/{len(value)}"

    status, headers, raw = local.request(
        "GET", artifact_path(reference["artifact_id"]),
        headers={"Range": "bytes=-9"})
    assert status == 206 and raw == value[-9:]
    assert headers["content-range"].endswith("/%d" % len(value))


def test_hostapp_uses_platform_cache_default_and_allows_test_injection(
        tmp_path, monkeypatch):
    default = tmp_path / "platform-cache"
    injected = tmp_path / "injected-cache"
    monkeypatch.setattr(
        hostapp.artifacts_mod, "default_cache_root", lambda: str(default))
    first = hostapp.HostApp(str(tmp_path / "state-default"))
    second = hostapp.HostApp(
        str(tmp_path / "state-injected"),
        artifact_cache_path=str(injected))
    try:
        assert first.artifacts.cache_root == str(default.resolve())
        assert second.artifacts.cache_root == str(injected.resolve())
    finally:
        first.db.close()
        second.db.close()


def test_loopback_authentication_precedes_artifact_parsing(local):
    node = local.register()
    status, _headers, raw = local.request(
        "POST", artifact_path(), body=b"not framed",
        headers={artifact_http.HEADER_NODE_ID: node["node_id"]}, token=None)
    assert status == 401
    assert decoded(raw)["reason"] == "unauthenticated"

    status, _headers, raw = local.request(
        "POST", artifact_path("art_" + "0" * 64, capability=True),
        body=b"{}", headers={"Content-Type": "application/json",
                              "Content-Length": "2"}, token=None)
    assert status == 401
    assert decoded(raw)["reason"] == "unauthenticated"

    status, _headers, raw = local.request(
        "GET", artifact_path("art_" + "0" * 64), token=None)
    assert status == 401
    assert decoded(raw)["reason"] == "unauthenticated"


def test_loopback_upload_is_bounded_and_hash_verified_even_when_deduped(local):
    node = local.register()
    local.app.artifacts.max_artifact_bytes = 8
    too_large = b"123456789"
    status, _headers, raw = local.request(
        "POST", artifact_path(), body=too_large,
        headers=upload_headers(node["node_id"], too_large))
    assert status == 413
    assert decoded(raw)["reason"] == "artifact_too_large"

    original = b"12345678"
    status, _headers, body = local_upload(local, node, original)
    assert status == 200
    artifact_id = body["artifact"]["artifact_id"]

    # Same declared digest and size, different body. A network dedupe must
    # still consume and hash the supplied bytes.
    impostor = b"87654321"
    headers = upload_headers(node["node_id"], impostor)
    headers[artifact_http.HEADER_SHA256] = hashlib.sha256(original).hexdigest()
    status, _headers, raw = local.request(
        "POST", artifact_path(), body=impostor, headers=headers)
    assert status == 422
    assert decoded(raw)["reason"] == "artifact_corrupt"
    status, _headers, raw = local.request("GET", artifact_path(artifact_id))
    assert status == 200 and raw == original


def test_invalid_range_is_416_and_does_not_return_content(local):
    node = local.register()
    status, _headers, body = local_upload(local, node, b"abcdef")
    artifact_id = body["artifact"]["artifact_id"]
    status, headers, raw = local.request(
        "GET", artifact_path(artifact_id),
        headers={"Range": "bytes=99-100"})
    assert status == 416
    assert headers["content-range"] == "bytes */6"
    assert decoded(raw)["reason"] == "artifact_range_invalid"


def test_transport_uses_store_quota_and_lru_eviction(local):
    node = local.register()
    local.app.artifacts.max_artifact_bytes = 700_000
    local.app.artifacts.set_quota_mb(1)
    first = b"a" * 600_000
    second = b"b" * 600_000
    assert local_upload(local, node, first)[0] == 200
    first_id = "art_" + hashlib.sha256(first).hexdigest()
    assert local_upload(local, node, second)[0] == 200
    second_id = "art_" + hashlib.sha256(second).hexdigest()
    with pytest.raises(artifacts.ArtifactNotFound):
        local.app.artifacts.describe(CONVOY, first_id)
    assert local.app.artifacts.verify(CONVOY, second_id)["size"] == len(second)


def test_loopback_and_peer_route_tables_remain_structurally_separate(local):
    local.register()
    body = json_body({})
    status, _headers, raw = local.request(
        "POST", artifact_path(peer=True), body=body,
        headers={"Content-Type": "application/json",
                 "Content-Length": str(len(body))})
    assert status == 404 and decoded(raw)["reason"] == "not_found"


def test_transfer_concurrency_bound_is_shared_by_local_artifacts(local):
    node = local.register()
    status, _headers, body = local_upload(local, node, b"busy")
    artifact_id = body["artifact"]["artifact_id"]
    held = []
    try:
        for _ in range(artifact_http.DEFAULT_MAX_TRANSFERS):
            assert local.app.begin_artifact_transfer()
            held.append(True)
        status, _headers, raw = local.request(
            "GET", artifact_path(artifact_id))
        assert status == 429
        assert decoded(raw)["reason"] == "artifact_transfer_busy"
    finally:
        for _ in held:
            local.app.end_artifact_transfer()


# -- peer: mTLS identity + namespace + node + controller + capability --


def test_peer_upload_grant_and_range_resume_are_end_to_end_binary(mesh):
    node_id, _ = mesh.register_node()
    value = (bytes(range(256)) * 1300) + b"tail"  # exceeds JSON reply cap
    status, _headers, body = peer_upload(mesh, node_id, value)
    assert status == 200 and body["ok"] is True
    reference = body["artifact"]
    assert reference["host_id"] == mesh.a.host_id
    assert reference["node_id"] == node_id
    assert reference["size"] == len(value)

    status, _headers, grant = peer_grant(
        mesh, node_id, reference["artifact_id"])
    assert status == 200
    full_token = grant["capability"]["token"]
    status, _headers, raw = peer_download(
        mesh, node_id, reference["artifact_id"], full_token)
    assert status == 200 and raw == value

    token = peer_grant(mesh, node_id, reference["artifact_id"])[2][
        "capability"]["token"]
    status, headers, raw = peer_download(
        mesh, node_id, reference["artifact_id"], token,
        byte_range="bytes=1000-199999")
    assert status == 206 and raw == value[1000:200000]
    assert headers["content-range"] == f"bytes 1000-199999/{len(value)}"

    # Resume uses a fresh single-purpose capability; the first is one-shot.
    status, _headers, replay = peer_download(
        mesh, node_id, reference["artifact_id"], token,
        byte_range="bytes=200000-")
    assert status == 403
    assert decoded(replay)["reason"] == "artifact_capability_replayed"
    token2 = peer_grant(mesh, node_id, reference["artifact_id"])[2][
        "capability"]["token"]
    status, _headers, raw = peer_download(
        mesh, node_id, reference["artifact_id"], token2,
        byte_range="bytes=200000-")
    assert status == 206 and raw == value[200000:]


def test_peer_upload_is_refused_for_observe_only_membership(mesh):
    node_id, _ = mesh.register_node()
    with mesh.b.lock:
        mesh.b.peers.observe(mesh.a.host_id)
    status, _headers, body = peer_upload(mesh, node_id, b"")
    assert status == 403
    assert body["reason"] == "peer_observe_only"
    assert mesh.b.artifacts.status()["artifact_count"] == 0


def test_observe_only_peer_can_read_an_already_owned_artifact(mesh):
    node_id, _ = mesh.register_node()
    artifact_id = peer_upload(mesh, node_id, b"read-only")[2]["artifact"][
        "artifact_id"]
    with mesh.b.lock:
        mesh.b.peers.observe(mesh.a.host_id)
    status, _headers, grant = peer_grant(mesh, node_id, artifact_id)
    assert status == 200
    status, _headers, raw = peer_download(
        mesh, node_id, artifact_id, grant["capability"]["token"])
    assert status == 200 and raw == b"read-only"


def test_peer_revocation_is_rechecked_before_artifact_download(mesh):
    node_id, _ = mesh.register_node()
    artifact_id = peer_upload(mesh, node_id, b"revoke")[2]["artifact"][
        "artifact_id"]
    token = peer_grant(mesh, node_id, artifact_id)[2]["capability"]["token"]
    with mesh.b.lock:
        mesh.b.peers.block(mesh.a.host_id)
    status, _headers, raw = peer_download(mesh, node_id, artifact_id, token)
    assert status == 403
    assert decoded(raw)["reason"] == "peer_blocked"


def test_peer_cannot_cross_convoy_or_address_a_foreign_node(mesh):
    node_id, _ = mesh.register_node()
    empty = b""
    status, _headers, raw = peer_request(
        mesh, "POST", artifact_path(convoy_id="not-admitted", peer=True),
        body=empty, headers=upload_headers(node_id, empty))
    assert status == 403
    assert decoded(raw)["reason"] == "namespace_not_admitted"

    # One automatic realm per host now correctly refuses a second public
    # registration.  Seed a legacy/foreign directory row directly so this
    # transport-boundary test can still prove that even retained/corrupt
    # cross-realm state is not addressable through the admitted namespace.
    with mesh.b.lock:
        other = mesh.b.directory.register(
            "/Work/other", "/OtherEmbody", "other",
            runtime_id="rt-other")
    status, _headers, body = peer_upload(mesh, other["node_id"], b"")
    assert status == 404
    assert body["reason"] == "artifact_scope_not_found"


def test_capability_is_bound_to_exact_peer_controller_and_node(mesh):
    node_id, _ = mesh.register_node()
    status, _headers, body = peer_upload(mesh, node_id, b"scoped")
    artifact_id = body["artifact"]["artifact_id"]
    token = peer_grant(mesh, node_id, artifact_id)[2]["capability"]["token"]

    status, _headers, raw = peer_download(
        mesh, node_id, artifact_id, token, controller="ctl-other")
    assert status == 403
    assert decoded(raw)["reason"] == "artifact_capability_invalid"

    # A scope mismatch does not burn the legitimate one-shot request.
    status, _headers, raw = peer_download(mesh, node_id, artifact_id, token)
    assert status == 200 and raw == b"scoped"


def test_invalid_capability_cannot_probe_artifact_existence(mesh):
    node_id, _ = mesh.register_node()
    status, _headers, raw = peer_download(
        mesh, node_id, "art_" + "f" * 64, "A" * 43)
    assert status == 403
    assert decoded(raw)["reason"] == "artifact_capability_invalid"


def test_peer_cannot_mint_a_capability_for_an_artifact_it_does_not_own(mesh):
    node_id, _ = mesh.register_node()
    reference = mesh.b.artifacts.put_bytes(
        CONVOY, b"local-only", mime_type="application/octet-stream",
        owner={"host_id": mesh.b.host_id, "node_id": node_id,
               "controller_id": "ctl-local"})
    status, _headers, body = peer_grant(
        mesh, node_id, reference["artifact_id"])
    assert status == 404
    assert body["reason"] == "artifact_scope_not_found"


def test_local_operator_can_explicitly_grant_a_local_artifact_to_peer(
        mesh, tmp_path):
    node_id, _ = mesh.register_node()
    reference = mesh.b.artifacts.put_bytes(
        CONVOY, b"explicit-share", mime_type="application/octet-stream",
        owner={"host_id": mesh.b.host_id, "node_id": node_id,
               "controller_id": "ctl-local"})
    local_http = LocalServer(app=mesh.b)
    try:
        body = json_body({
            "peer_host_id": mesh.a.host_id, "node_id": node_id,
            "controller_id": CONTROLLER,
        })
        status, _headers, raw = local_http.request(
            "POST", artifact_path(
                reference["artifact_id"], capability=True), body=body,
            headers={"Content-Type": "application/json",
                     "Content-Length": str(len(body))})
        assert status == 200
        token = decoded(raw)["capability"]["token"]
        status, _headers, raw = peer_download(
            mesh, node_id, reference["artifact_id"], token)
        assert status == 200 and raw == b"explicit-share"
    finally:
        local_http.stop(close_app=False)


def test_invalid_peer_range_does_not_consume_resume_capability(mesh):
    node_id, _ = mesh.register_node()
    artifact_id = peer_upload(mesh, node_id, b"range")[2]["artifact"][
        "artifact_id"]
    token = peer_grant(mesh, node_id, artifact_id)[2]["capability"]["token"]
    status, headers, raw = peer_download(
        mesh, node_id, artifact_id, token, byte_range="bytes=99-100")
    assert status == 416
    assert headers["content-range"] == "bytes */5"
    assert decoded(raw)["reason"] == "artifact_range_invalid"
    status, _headers, raw = peer_download(mesh, node_id, artifact_id, token)
    assert status == 200 and raw == b"range"


def test_local_artifact_routes_are_not_reachable_on_peer_listener(mesh):
    status, _headers, raw = peer_request(
        mesh, "GET", artifact_path("art_" + "0" * 64))
    assert status == 404
    assert decoded(raw)["reason"] == "not_found"


# -- production peer client: bounded transfer + verified materialization --

def test_peerclient_uploads_and_downloads_into_the_local_artifact_store(
        mesh, tmp_path):
    node_id, _ = mesh.register_node()
    mesh.a.artifacts.free_space_floor_bytes = 0
    mesh.a.artifacts.max_artifact_bytes = 2 * 1024 * 1024
    value = (bytes(range(256)) * 1500) + b"production-client"
    digest = hashlib.sha256(value).hexdigest()
    partials = tmp_path / "partials"
    partials.mkdir()

    uploaded = peerclient.upload_peer_artifact(
        mesh.target(), mesh.a.hostkeys, CONVOY, node_id, CONTROLLER,
        io.BytesIO(value), expected_size=len(value),
        expected_sha256=digest, mime_type="application/octet-stream",
        filename_hint="capture.bin", timeout_s=20, temp_dir=partials)
    assert uploaded["ok"] is True
    assert uploaded["artifact"]["artifact_id"] == "art_" + digest
    assert uploaded["transfer"]["retry_mode"] == "full_from_zero"
    assert list(partials.iterdir()) == []

    downloaded = peerclient.download_peer_artifact(
        mesh.target(), mesh.a.hostkeys, mesh.a.artifacts, CONVOY,
        node_id, CONTROLLER, uploaded["artifact"], timeout_s=20,
        temp_dir=partials, protection_id="relay:" + "d" * 32,
        protection_kind="active_transfer")
    assert downloaded["ok"] is True
    assert downloaded["transfer"] == {
        "attempts": 1, "resumed": False, "bytes": len(value)}
    assert mesh.a.artifacts.verify(
        CONVOY, uploaded["artifact"]["artifact_id"])["sha256"] == digest
    with pytest.raises(artifacts.ArtifactProtected):
        mesh.a.artifacts.delete(
            CONVOY, uploaded["artifact"]["artifact_id"])
    assert mesh.a.artifacts.release(
        CONVOY, uploaded["artifact"]["artifact_id"],
        "relay:" + "d" * 32,
        expected_kind="active_transfer") == 0
    assert list(partials.iterdir()) == []


def test_peerclient_resumes_an_interrupted_download_with_a_fresh_capability(
        mesh, tmp_path):
    node_id, _ = mesh.register_node()
    mesh.a.artifacts.free_space_floor_bytes = 0
    mesh.a.artifacts.max_artifact_bytes = 2 * 1024 * 1024
    value = bytes(range(256)) * 1200
    reference = peer_upload(mesh, node_id, value)[2]["artifact"]
    original = mesh.b.artifact_open_peer_download
    ranges = []
    interrupt_first = [True]

    class InterruptedLease:
        def __init__(self, lease):
            self._lease = lease
            self._iterator = iter(lease)
            self._sent = False
            for name in ("mime_type", "length", "offset", "total_size",
                         "sha256", "artifact_id"):
                setattr(self, name, getattr(lease, name))

        def __iter__(self):
            return self

        def __next__(self):
            if not self._sent:
                self._sent = True
                return next(self._iterator)
            raise OSError("test connection interruption")

        def close(self):
            self._lease.close()

    def interrupted(convoy_id, artifact_id, token, requested_node,
                    controller_id, range_header=None, **kwargs):
        ranges.append(range_header)
        code, payload, lease, headers = original(
            convoy_id, artifact_id, token, requested_node, controller_id,
            range_header, **kwargs)
        if lease is not None and interrupt_first[0]:
            interrupt_first[0] = False
            lease = InterruptedLease(lease)
        return code, payload, lease, headers

    mesh.b.artifact_open_peer_download = interrupted
    partials = tmp_path / "partials"
    partials.mkdir()
    result = peerclient.download_peer_artifact(
        mesh.target(), mesh.a.hostkeys, mesh.a.artifacts, CONVOY,
        node_id, CONTROLLER, reference, timeout_s=20, max_attempts=3,
        temp_dir=partials)
    assert result["ok"] is True
    assert result["transfer"]["attempts"] == 2
    assert result["transfer"]["resumed"] is True
    assert ranges == [None, "bytes=%d-" % artifact_http.STREAM_CHUNK_BYTES]
    assert list(partials.iterdir()) == []


def test_peerclient_rejects_corrupt_stream_headers_and_removes_partial(
        mesh, tmp_path):
    node_id, _ = mesh.register_node()
    mesh.a.artifacts.free_space_floor_bytes = 0
    value = b"header-integrity"
    reference = peer_upload(mesh, node_id, value)[2]["artifact"]
    original_headers = mesh.b._artifact_download_headers

    def corrupt_headers(lease, partial):
        headers = original_headers(lease, partial)
        headers[artifact_http.HEADER_SHA256] = "0" * 64
        return headers

    mesh.b._artifact_download_headers = corrupt_headers
    partials = tmp_path / "partials"
    partials.mkdir()
    result = peerclient.download_peer_artifact(
        mesh.target(), mesh.a.hostkeys, mesh.a.artifacts, CONVOY,
        node_id, CONTROLLER, reference, timeout_s=20, temp_dir=partials)
    assert result["ok"] is False
    assert result["reason"] == "artifact_corrupt"
    with pytest.raises(artifacts.ArtifactNotFound):
        mesh.a.artifacts.describe(CONVOY, reference["artifact_id"])
    assert list(partials.iterdir()) == []


def test_peerclient_upload_retry_reopens_verified_stage_at_byte_zero(
        mesh, tmp_path, monkeypatch):
    node_id, _ = mesh.register_node()
    value = b"retry-from-zero" * 100
    digest = hashlib.sha256(value).hexdigest()
    partials = tmp_path / "partials"
    partials.mkdir()
    reads = []

    def attempt(_target, _keys, _path, staged_path, metadata,
                _node_id, _controller_id, _deadline):
        with open(staged_path, "rb") as source:
            reads.append(source.read())
        if len(reads) == 1:
            return None
        return {"ok": True, "artifact": {
            "kind": "convoy_artifact", "convoy_id": CONVOY,
            "artifact_id": "art_" + digest, "sha256": digest,
            "size": len(value), "mime_type": metadata["mime_type"],
        }}

    monkeypatch.setattr(peerclient, "_upload_artifact_once", attempt)
    result = peerclient.upload_peer_artifact(
        mesh.target(), mesh.a.hostkeys, CONVOY, node_id, CONTROLLER, value,
        expected_size=len(value), expected_sha256=digest,
        max_attempts=2, temp_dir=partials)
    assert result["ok"] is True
    assert result["transfer"]["attempts"] == 2
    assert reads == [value, value]
    assert list(partials.iterdir()) == []


def test_peerclient_cumulative_deadline_cleans_partial_state(
        mesh, tmp_path, monkeypatch):
    node_id, _ = mesh.register_node()
    mesh.a.artifacts.free_space_floor_bytes = 0
    value = b"deadline"
    digest = hashlib.sha256(value).hexdigest()
    reference = {
        "kind": "convoy_artifact", "convoy_id": CONVOY,
        "artifact_id": "art_" + digest, "sha256": digest,
        "size": len(value), "mime_type": "application/octet-stream",
    }
    now = [0.0]

    def expire(*_args, **_kwargs):
        now[0] = 2.0
        return None

    monkeypatch.setattr(
        peerclient, "_request_peer_artifact_capability", expire)
    partials = tmp_path / "partials"
    partials.mkdir()
    result = peerclient.download_peer_artifact(
        mesh.target(), mesh.a.hostkeys, mesh.a.artifacts, CONVOY,
        node_id, CONTROLLER, reference, timeout_s=1.0,
        max_attempts=3, temp_dir=partials, monotonic=lambda: now[0])
    assert result["ok"] is False
    assert result["reason"] == "deadline_exceeded"
    assert list(partials.iterdir()) == []


def test_peerclient_zero_byte_download_still_uses_authenticated_grant(
        mesh, tmp_path):
    node_id, _ = mesh.register_node()
    mesh.a.artifacts.free_space_floor_bytes = 0
    empty = b""
    uploaded = peerclient.upload_peer_artifact(
        mesh.target(), mesh.a.hostkeys, CONVOY, node_id, CONTROLLER, empty,
        expected_size=0, expected_sha256=hashlib.sha256(empty).hexdigest(),
        timeout_s=20)
    assert uploaded["ok"] is True
    # Exercise the integration-friendly form: a returned artifact carries the
    # peer-namespaced controller attribution, while HostApp expects the local
    # tail on the wire and applies the namespace exactly once.
    assert uploaded["artifact"]["controller_id"].startswith("peer:")
    partials = tmp_path / "partials"
    partials.mkdir()
    downloaded = peerclient.download_peer_artifact(
        mesh.target(), mesh.a.hostkeys, mesh.a.artifacts, CONVOY,
        node_id, uploaded["artifact"]["controller_id"],
        uploaded["artifact"], timeout_s=20, temp_dir=partials)
    assert downloaded["ok"] is True
    assert downloaded["transfer"] == {
        "attempts": 1, "resumed": False, "bytes": 0}
    assert list(partials.iterdir()) == []


def test_artifact_relay_maps_shared_deadline_to_gateway_timeout():
    assert hostapp.HostApp._artifact_relay_status({
        "ok": False, "reason": "deadline_exceeded",
    }) == 504


def test_peerclient_download_uses_receiving_store_artifact_limit(
        mesh, tmp_path, monkeypatch):
    node_id, _ = mesh.register_node()
    value = b"five"
    reference = peer_upload(mesh, node_id, value)[2]["artifact"]
    mesh.a.artifacts.max_artifact_bytes = len(value) - 1
    requested = []

    def should_not_grant(*args, **kwargs):
        requested.append((args, kwargs))
        return None

    monkeypatch.setattr(
        peerclient, "_request_peer_artifact_capability", should_not_grant)
    partials = tmp_path / "partials"
    partials.mkdir()
    result = peerclient.download_peer_artifact(
        mesh.target(), mesh.a.hostkeys, mesh.a.artifacts, CONVOY,
        node_id, CONTROLLER, reference, max_bytes=1024,
        temp_dir=partials)
    assert result["ok"] is False
    assert result["reason"] == "artifact_invalid"
    assert requested == []
    assert list(partials.iterdir()) == []


def test_identical_content_from_two_peers_keeps_both_grant_authorities(
        mesh, tmp_path):
    node_id, _ = mesh.register_node()
    peer_c = hostapp.HostApp(
        str(tmp_path / "peer-c"),
        artifact_cache_path=str(tmp_path / "peer-c-artifacts"))
    with mesh.b.lock:
        mesh.b.peers.admit(
            peer_c.host_id, peer_c.hostkeys.fingerprint,
            cert_pem=peer_c.hostkeys.certificate_pem,
            convoy_ids=[CONVOY])
    value = b"content-addressed-but-multi-owner"
    digest = hashlib.sha256(value).hexdigest()
    try:
        from_a = peerclient.upload_peer_artifact(
            mesh.target(), mesh.a.hostkeys, CONVOY, node_id, CONTROLLER,
            value, expected_size=len(value), expected_sha256=digest,
            timeout_s=20)
        from_c = peerclient.upload_peer_artifact(
            mesh.target(), peer_c.hostkeys, CONVOY, node_id, CONTROLLER,
            value, expected_size=len(value), expected_sha256=digest,
            timeout_s=20)
        assert from_a["ok"] is True and from_c["ok"] is True
        assert from_a["artifact"]["artifact_id"] == from_c["artifact"][
            "artifact_id"]
        assert from_a["artifact"]["host_id"] == mesh.a.host_id
        assert from_c["artifact"]["host_id"] == peer_c.host_id
        assert mesh.b.artifacts.status()["artifact_count"] == 1

        grant_a = peerclient.request_peer_artifact_capability(
            mesh.target(), mesh.a.hostkeys, CONVOY,
            from_a["artifact"]["artifact_id"], node_id, CONTROLLER,
            timeout_s=20)
        grant_c = peerclient.request_peer_artifact_capability(
            mesh.target(), peer_c.hostkeys, CONVOY,
            from_c["artifact"]["artifact_id"], node_id, CONTROLLER,
            timeout_s=20)
        assert grant_a["ok"] is True
        assert grant_c["ok"] is True
        assert grant_a["capability"]["token"] != grant_c["capability"][
            "token"]
    finally:
        peer_c.db.close()


def test_peerclient_forwards_exact_job_owner_when_minting_download_grant(
        mesh, tmp_path):
    node_id, _ = mesh.register_node()
    mesh.a.artifacts.free_space_floor_bytes = 0
    job_id = "cj_" + "a" * 32
    namespaced = "peer:%s:%s" % (mesh.a.host_id, CONTROLLER)
    reference = mesh.b.artifacts.put_bytes(
        CONVOY, b"spilled-job-result", mime_type="application/json",
        owner={"host_id": mesh.a.host_id, "node_id": node_id,
               "controller_id": namespaced, "job_id": job_id})
    partials = tmp_path / "partials"
    partials.mkdir()
    result = peerclient.download_peer_artifact(
        mesh.target(), mesh.a.hostkeys, mesh.a.artifacts, CONVOY,
        node_id, namespaced, reference, timeout_s=20, temp_dir=partials)
    assert result["ok"] is True
    assert result["artifact"]["job_id"] == job_id
    assert list(partials.iterdir()) == []


def test_loopback_relay_materializes_peer_artifact_with_exact_owner(mesh):
    node_id, _ = mesh.register_node()
    mesh.a.artifacts.free_space_floor_bytes = 0
    with mesh.a.lock:
        mesh.a.peers.admit(
            mesh.b.host_id, mesh.b.hostkeys.fingerprint,
            cert_pem=mesh.b.hostkeys.certificate_pem,
            convoy_ids=[CONVOY],
            endpoints=["127.0.0.1:%d" % mesh.port])
    value = b"remote capture bytes"
    job_id = "cj_" + "b" * 32
    namespaced = "peer:%s:%s" % (mesh.a.host_id, CONTROLLER)
    reference = mesh.b.artifacts.put_bytes(
        CONVOY, value, mime_type="image/png", filename_hint="capture.png",
        owner={"host_id": mesh.a.host_id, "node_id": node_id,
               "controller_id": namespaced, "job_id": job_id})

    local_server = LocalServer(app=mesh.a)
    try:
        body = json_body({
            "target_host_id": mesh.b.host_id, "convoy_id": CONVOY,
            "target_node_id": node_id, "controller_id": CONTROLLER,
            "artifact": reference, "timeout_s": 20.0,
        })
        status, _headers, raw = local_server.request(
            "POST", "/relay/artifact", body=body,
            headers={"Content-Type": "application/json",
                     "Content-Length": str(len(body))})
        result = decoded(raw)
        assert status == 200, result
        assert result["ok"] is True
        assert result["wakes_touchdesigner"] is False
        assert result["artifact"]["artifact_id"] == reference["artifact_id"]
        assert result["artifact"]["job_id"] == job_id
        assert result["transfer"]["bytes"] == len(value)
        assert result["relay_protection_id"].startswith("relay:")
        with pytest.raises(artifacts.ArtifactProtected):
            mesh.a.artifacts.delete(
                CONVOY, result["artifact"]["artifact_id"])

        status, headers, downloaded = local_server.request(
            "GET", artifact_path(result["artifact"]["artifact_id"]))
        assert status == 200
        assert downloaded == value
        assert headers["x-convoy-content-sha256"] == reference["sha256"]

        release = json_body({
            "convoy_id": CONVOY,
            "artifact_id": result["artifact"]["artifact_id"],
            "relay_protection_id": result["relay_protection_id"],
        })
        status, _headers, raw = local_server.request(
            "POST", "/relay/artifact/release", body=release,
            headers={"Content-Type": "application/json",
                     "Content-Length": str(len(release))})
        assert status == 200 and decoded(raw)["released"] is True
        assert mesh.a.artifacts.delete(
            CONVOY, result["artifact"]["artifact_id"]) == len(value)
    finally:
        local_server.stop(close_app=False)


def test_loopback_relay_local_sibling_requires_exact_owner(local):
    node = local.register()
    value = b"same-host result"
    job_id = "cj_" + "c" * 32
    reference = local.app.artifacts.put_bytes(
        CONVOY, value, mime_type="application/json",
        owner={"host_id": local.app.host_id, "node_id": node["node_id"],
               "controller_id": CONTROLLER, "job_id": job_id})
    request = {
        "target_host_id": local.app.host_id, "convoy_id": CONVOY,
        "target_node_id": node["node_id"], "controller_id": CONTROLLER,
        "artifact": reference, "timeout_s": 5.0,
    }
    body = json_body(request)
    status, _headers, raw = local.request(
        "POST", "/relay/artifact", body=body,
        headers={"Content-Type": "application/json",
                 "Content-Length": str(len(body))})
    result = decoded(raw)
    assert status == 200
    assert result["local_sibling"] is True
    assert result["relay_protection_id"].startswith("relay:")
    assert result["transfer"] == {
        "attempts": 0, "resumed": False, "bytes": len(value)}
    with pytest.raises(artifacts.ArtifactProtected):
        local.app.artifacts.delete(CONVOY, reference["artifact_id"])

    release = json_body({
        "convoy_id": CONVOY,
        "artifact_id": reference["artifact_id"],
        "relay_protection_id": result["relay_protection_id"],
    })
    status, _headers, raw = local.request(
        "POST", "/relay/artifact/release", body=release,
        headers={"Content-Type": "application/json",
                 "Content-Length": str(len(release))})
    assert status == 200 and decoded(raw)["released"] is True

    wrong = dict(request, controller_id="other-controller")
    body = json_body(wrong)
    status, _headers, raw = local.request(
        "POST", "/relay/artifact", body=body,
        headers={"Content-Type": "application/json",
                 "Content-Length": str(len(body))})
    assert status == 404
    assert decoded(raw)["reason"] == "artifact_not_found"


def _registered_export(local, project, value=b"exported bytes"):
    code, node = local.app.register_node({
        "project_root": str(project), "convoy_id": CONVOY,
        "comp_path": "/EmbodyExport", "runtime_id": "rt-export",
    })
    assert code == 200, node
    owner = {
        "host_id": local.app.host_id,
        "node_id": node["node_id"],
        "controller_id": CONTROLLER,
        "job_id": "cj_" + "e" * 32,
    }
    reference = local.app.artifacts.put_bytes(
        CONVOY, value, mime_type="application/octet-stream",
        filename_hint="export.bin", owner=owner)
    return node, reference


def _export_request(local, project, node, reference, **overrides):
    body = {
        "target_host_id": local.app.host_id,
        "target_node_id": node["node_id"],
        "convoy_id": CONVOY,
        "project_root": str(project),
        "artifact": reference,
        "overwrite": False,
    }
    body.update(overrides)
    encoded = json_body(body)
    status, _headers, raw = local.request(
        "POST", "/artifact/export", body=encoded,
        headers={"Content-Type": "application/json",
                 "Content-Length": str(len(encoded))})
    return status, decoded(raw)


def test_loopback_export_is_registered_atomic_and_overwrite_is_explicit(
        local, tmp_path):
    project = tmp_path / "registered-project"
    project.mkdir()
    node, reference = _registered_export(local, project)

    status, result = _export_request(
        local, project, node, reference, filename="result.bin")
    assert status == 200, result
    assert result["ok"] is True
    saved = result["artifact"]
    expected = project / ".embody" / "convoy" / "artifacts" / "result.bin"
    assert saved["saved_path"] == str(expected.resolve())
    assert expected.read_bytes() == b"exported bytes"
    expected.write_bytes(b"user-owned content")

    status, result = _export_request(
        local, project, node, reference, filename="result.bin")
    assert status == 409
    assert result["reason"] == "artifact_export_exists"
    assert expected.read_bytes() == b"user-owned content"

    status, result = _export_request(
        local, project, node, reference, filename="result.bin",
        overwrite=True)
    assert status == 200, result
    assert expected.read_bytes() == b"exported bytes"


def test_loopback_export_rejects_unregistered_root_and_traversal(
        local, tmp_path):
    registered = tmp_path / "registered"
    registered.mkdir()
    unregistered = tmp_path / "unregistered"
    unregistered.mkdir()
    node, reference = _registered_export(local, registered)

    status, result = _export_request(
        local, unregistered, node, reference, filename="escape.bin")
    assert status == 403
    assert result["reason"] == "artifact_project_unregistered"
    assert not (unregistered / ".embody").exists()

    status, result = _export_request(
        local, registered, node, reference, filename="../../escape.bin")
    assert status == 400
    assert result["reason"] == "artifact_invalid"
    assert not (tmp_path / "escape.bin").exists()


def test_loopback_export_rejects_symlinked_managed_directory(
        local, tmp_path):
    project = tmp_path / "registered-symlink"
    project.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (project / ".embody").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable for this test user")
    node, reference = _registered_export(local, project)
    status, result = _export_request(
        local, project, node, reference, filename="escape.bin")
    assert status == 400
    assert result["reason"] == "artifact_invalid"
    assert list(outside.iterdir()) == []


def test_peerclient_artifact_channel_recomputes_pin_and_cleans_temp(
        mesh, tmp_path):
    node_id, _ = mesh.register_node()
    reference = peer_upload(mesh, node_id, b"pin-artifact")[2]["artifact"]
    wrong = mesh.target(
        fingerprint="cvfp1-" + "0000-" * 7 + "0000")
    partials = tmp_path / "partials"
    partials.mkdir()
    result = peerclient.download_peer_artifact(
        wrong, mesh.a.hostkeys, mesh.a.artifacts, CONVOY,
        node_id, CONTROLLER, reference, timeout_s=20, temp_dir=partials)
    assert isinstance(result, peerclient._PinMismatch)
    assert result.offered == mesh.b.hostkeys.fingerprint
    assert list(partials.iterdir()) == []
