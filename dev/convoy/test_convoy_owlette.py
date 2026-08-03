"""Contract and safety tests for the optional Owlette public-API seam."""

import json
import math
import os

import pytest

import convoy_owlette as ow


TOKEN = "owk_test_this-is-a-test-token"
SITE = "site-a"
MACHINE = "machine-b"
COMMAND = "cmd_abc_123"


def response(status, payload, headers=None):
    merged = {"Content-Type": "application/json"}
    merged.update(headers or {})
    return ow.HttpResponse(status, merged, json.dumps(payload).encode("utf-8"))


class FakeTransport:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def request(self, config, method, path, headers, body):
        self.calls.append({
            "config": config, "method": method, "path": path,
            "headers": dict(headers), "body": body,
        })
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer


class WireResponse:
    def __init__(self, status=200, body=b'{"sites":[]}', headers=None):
        self.status = status
        self.body = body
        self.offset = 0
        self.headers = dict(headers or {
            "Content-Type": "application/json",
            "Content-Length": str(len(body)),
        })

    def getheader(self, name):
        for key, value in self.headers.items():
            if key.lower() == name.lower():
                return value
        return None

    def getheaders(self):
        return list(self.headers.items())

    def read(self, amount):
        chunk = self.body[self.offset:self.offset + amount]
        self.offset += len(chunk)
        return chunk


class WireConnection:
    def __init__(self, response=None, connect_error=None, request_error=None):
        self.response = response or WireResponse()
        self.connect_error = connect_error
        self.request_error = request_error
        self.connected = False
        self.closed = False
        self.requests = []

    def connect(self):
        if self.connect_error:
            raise self.connect_error
        self.connected = True

    def request(self, method, path, body=None, headers=None):
        self.requests.append((method, path, body, headers))
        if self.request_error:
            raise self.request_error

    def getresponse(self):
        return self.response

    def close(self):
        self.closed = True


def client(*answers, config=None, provider=None):
    transport = FakeTransport(*answers)
    result = ow.OwletteClient(
        config or ow.OwletteConfig(), provider or (lambda: TOKEN), transport)
    return result, transport


def site_payload():
    return {"id": SITE, "name": "Stage", "tier": "pro",
            "timezone": "America/Los_Angeles"}


def machine_payload(online=True):
    return {
        "id": MACHINE, "name": "Render B", "online": online,
        "lastHeartbeat": "2026-08-02T10:00:00Z",
        "agentVersion": "4.2.0", "os": "windows",
        "currentRoosts": [],
    }


# -- published capability boundary -----------------------------------


def test_capability_snapshot_is_explicit_about_missing_cancel():
    got = ow.public_capabilities()
    assert got["openapi_version"] == "2.3.1"
    assert got["site_inventory"] is True
    assert got["machine_inventory"] is True
    assert got["machine_online"] is True
    assert got["command_submit"] is True
    assert got["command_status"] is True
    assert got["command_cancel"] is False
    assert set(got["command_types"]) == ow.PUBLIC_COMMAND_TYPES


def test_public_command_allowlist_matches_the_reviewed_openapi_enum():
    assert ow.PUBLIC_COMMAND_TYPES == {
        "reboot_machine", "shutdown_machine", "cancel_reboot",
        "dismiss_reboot_pending", "capture_screenshot", "start_live_view",
        "stop_live_view", "restart_process", "start_process", "kill_process",
        "set_launch_mode", "apply_display_topology", "ack_display_topology",
        "enumerate_display_modes", "test_display_apply", "mcp_tool_call",
        "update_owlette",
    }


def test_cancel_fails_locally_without_reading_a_credential_or_using_transport():
    credential_calls = []
    c, transport = client(
        provider=lambda: credential_calls.append(True) or TOKEN)
    with pytest.raises(ow.OwletteCapabilityUnsupported) as caught:
        c.cancel_command(SITE, MACHINE, COMMAND, idempotency_key="same-key")
    assert caught.value.reason == "owlette_capability_unsupported"
    assert "no generic command-cancel endpoint" in caught.value.detail
    assert credential_calls == []
    assert transport.calls == []


def test_undocumented_command_type_fails_before_network():
    c, transport = client()
    with pytest.raises(ow.OwletteCapabilityUnsupported):
        c.submit_command(SITE, MACHINE, "cancel_mcp_tool",
                         idempotency_key="key-1")
    assert transport.calls == []


# -- configuration and credential ownership -------------------------


@pytest.mark.parametrize("url", [
    "http://owlette.app",
    "https://owlette.app/api",
    "https://owlette.app?key=x",
    "https://user:pass@owlette.app",
    "https://example.com",
    "https://owlette.app:444",
])
def test_only_bare_published_https_origins_are_accepted(url):
    with pytest.raises(ow.OwletteConfigError):
        ow.OwletteConfig(base_url=url)


def test_both_published_origins_are_accepted_and_normalized():
    assert ow.OwletteConfig("https://owlette.app/").base_url \
        == "https://owlette.app"
    assert ow.OwletteConfig("https://dev.owlette.app/").base_url \
        == "https://dev.owlette.app"


@pytest.mark.parametrize("timeout", [0, 0.49, 30.01, math.inf, math.nan, "x"])
def test_http_timeout_is_finite_and_bounded(timeout):
    with pytest.raises(ow.OwletteConfigError):
        ow.OwletteConfig(timeout_s=timeout)


def test_client_from_env_reads_the_token_at_request_time_for_rotation():
    environ = {ow.ENV_API_KEY: TOKEN}
    transport = FakeTransport(
        response(200, {"sites": []}), response(200, {"sites": []}))
    c = ow.client_from_env(environ, transport=transport)
    c.list_sites()
    environ[ow.ENV_API_KEY] = "owk_live_rotated-token"
    c.list_sites()
    assert transport.calls[0]["headers"]["Authorization"] == "Bearer " + TOKEN
    assert transport.calls[1]["headers"]["Authorization"] \
        == "Bearer owk_live_rotated-token"


def test_named_os_secret_uses_only_the_injected_secret_reader():
    environ = {
        ow.ENV_API_KEY: "owk_live_must-not-be-used",
        ow.ENV_API_KEY_SECRET: "credential-manager/owlette/convoy",
    }
    seen = []
    transport = FakeTransport(response(200, {"sites": []}))
    c = ow.client_from_env(
        environ, secret_reader=lambda reference: seen.append(reference) or TOKEN,
        transport=transport)
    c.list_sites()
    assert seen == ["credential-manager/owlette/convoy"]
    assert transport.calls[0]["headers"]["Authorization"] == "Bearer " + TOKEN


def test_named_os_secret_without_a_reader_fails_closed():
    c = ow.client_from_env({
        ow.ENV_API_KEY: TOKEN,
        ow.ENV_API_KEY_SECRET: "keychain:owlette-convoy",
    }, transport=FakeTransport())
    with pytest.raises(ow.OwletteCredentialUnavailable) as caught:
        c.list_sites()
    assert "no secret reader" in caught.value.detail


def test_missing_or_non_public_key_fails_before_network():
    for value in (None, "", "owk_live_", "owk_test_", "firebase-id-token",
                  "owk_live_\nheader"):
        c, transport = client(provider=lambda value=value: value)
        with pytest.raises(ow.OwletteCredentialUnavailable):
            c.list_sites()
        assert transport.calls == []


def test_credential_backend_exception_text_is_not_propagated():
    def broken_provider():
        raise RuntimeError("backend included " + TOKEN)

    c, transport = client(provider=broken_provider)
    with pytest.raises(ow.OwletteCredentialUnavailable) as caught:
        c.list_sites()
    assert TOKEN not in str(caught.value)
    assert caught.value.__cause__ is None
    assert transport.calls == []


def test_repr_and_public_results_never_contain_the_token():
    c, unused_transport = client(response(200, {"sites": [site_payload()]}))
    result = c.list_sites()
    assert TOKEN not in repr(c)
    assert TOKEN not in repr(c.config)
    assert TOKEN not in repr(result)


# -- read-first inventory --------------------------------------------


def test_site_inventory_uses_the_canonical_public_route():
    c, transport = client(response(200, {"sites": [site_payload()]}))
    got = c.list_sites()
    assert got == [{"id": SITE, "name": "Stage", "tier": "pro",
                    "timezone": "America/Los_Angeles"}]
    call = transport.calls[0]
    assert call["method"] == "GET"
    assert call["path"] == "/api/sites"
    assert "api_key" not in call["path"]
    assert call["body"] is None


def test_configured_default_site_is_used_for_machine_inventory():
    config = ow.OwletteConfig(default_site_id=SITE)
    c, transport = client(
        response(200, {"machines": [machine_payload(False)]}), config=config)
    got = c.list_machines()
    assert got[0]["online"] is False
    assert transport.calls[0]["path"] == "/api/sites/site-a/machines"


def test_site_and_machine_ids_are_single_encoded_path_segments():
    c, transport = client(response(200, {"machines": []}))
    c.list_machines("site/with space")
    assert transport.calls[0]["path"] \
        == "/api/sites/site%2Fwith%20space/machines"


def test_missing_site_context_fails_before_network():
    c, transport = client()
    with pytest.raises(ow.OwletteValidationError):
        c.list_machines()
    assert transport.calls == []


def test_machine_detail_and_online_state_use_api_owned_boolean():
    payload = machine_payload(True)
    payload.update({"siteId": SITE, "hostname": "TEC-B4A",
                    "metrics": {"cpu": 12}, "processes": []})
    c, transport = client(response(200, payload), response(200, payload))
    got = c.get_machine(SITE, MACHINE)
    assert got["online"] is True
    assert got["hostname"] == "TEC-B4A"
    assert c.machine_online(SITE, MACHINE) is True
    assert all(call["method"] == "GET" for call in transport.calls)


@pytest.mark.parametrize("online", [None, 0, 1, "true"])
def test_online_state_is_not_coerced_or_inferred(online):
    payload = machine_payload()
    payload["online"] = online
    c, unused_transport = client(response(200, {"machines": [payload]}))
    with pytest.raises(ow.OwletteProtocolError):
        c.list_machines(SITE)


def test_malformed_inventory_fails_closed():
    c, unused_transport = client(response(200, {"sites": {}}))
    with pytest.raises(ow.OwletteProtocolError):
        c.list_sites()


# -- command submit and status ---------------------------------------


def test_submit_requires_and_sends_caller_held_idempotency_key():
    c, transport = client(response(
        202, {"ok": True, "data": {"commandId": COMMAND,
                                     "status": "pending"}},
        {"Idempotent-Replayed": "true"}))
    got = c.submit_command(
        SITE, MACHINE, "capture_screenshot", params={"monitor": "primary"},
        timeout_seconds=30, idempotency_key="convoy-job-123")
    assert got == {
        "commandId": COMMAND, "status": "pending",
        "idempotencyKey": "convoy-job-123", "replayed": True,
    }
    call = transport.calls[0]
    assert call["method"] == "POST"
    assert call["path"] \
        == "/api/sites/site-a/machines/machine-b/commands"
    assert call["headers"]["Idempotency-Key"] == "convoy-job-123"
    assert json.loads(call["body"]) == {
        "type": "capture_screenshot", "params": {"monitor": "primary"},
        "timeout_seconds": 30,
    }


@pytest.mark.parametrize("key", [None, "", " has-space", "x" * 256, "snowman-☃"])
def test_submit_refuses_invalid_idempotency_keys_before_network(key):
    c, transport = client()
    with pytest.raises(ow.OwletteValidationError):
        c.submit_command(SITE, MACHINE, "reboot_machine",
                         idempotency_key=key)
    assert transport.calls == []


@pytest.mark.parametrize("timeout", [0, 601, 1.0, True])
def test_submit_refuses_command_timeouts_outside_the_public_contract(timeout):
    c, transport = client()
    with pytest.raises(ow.OwletteValidationError):
        c.submit_command(SITE, MACHINE, "reboot_machine",
                         idempotency_key="key", timeout_seconds=timeout)
    assert transport.calls == []


def test_submit_refuses_non_finite_json_and_oversized_bodies():
    c, transport = client()
    with pytest.raises(ow.OwletteValidationError):
        c.submit_command(SITE, MACHINE, "mcp_tool_call",
                         params={"tool_params": {"bad": math.nan}},
                         idempotency_key="key")
    with pytest.raises(ow.OwletteValidationError):
        c.submit_command(SITE, MACHINE, "mcp_tool_call",
                         params={"tool_params": {"blob": "x" *
                                 ow.MAX_REQUEST_BYTES}},
                         idempotency_key="key")
    assert transport.calls == []


def test_submit_accepts_a_replayed_already_progressed_record():
    # A 202 replay of the same idempotency key legitimately returns the
    # original record, which may already have progressed past "pending".
    c, transport = client(response(
        202, {"ok": True, "data": {"commandId": COMMAND,
                                    "status": "in_progress"}},
        {"Idempotent-Replayed": "true"}))
    got = c.submit_command(
        SITE, MACHINE, "capture_screenshot", idempotency_key="convoy-job-1")
    assert got == {
        "commandId": COMMAND, "status": "in_progress",
        "idempotencyKey": "convoy-job-1", "replayed": True,
    }
    assert "warnings" not in got


def test_submit_downgrades_bad_command_id_format_to_a_warning():
    # The command was accepted (202); a surprising commandId format must not
    # turn a real accept into a definitive failure.
    c, transport = client(response(
        202, {"ok": True, "data": {"commandId": "01JABCXYZ",
                                    "status": "pending"}}))
    got = c.submit_command(
        SITE, MACHINE, "reboot_machine", idempotency_key="convoy-job-2")
    assert got["commandId"] == "01JABCXYZ"
    assert got["status"] == "pending"
    assert got["warnings"] == ["commandId does not match the public cmd_ format"]


def test_submit_downgrades_unknown_status_to_a_warning():
    c, transport = client(response(
        202, {"ok": True, "data": {"commandId": COMMAND,
                                    "status": "queued"}}))
    got = c.submit_command(
        SITE, MACHINE, "reboot_machine", idempotency_key="convoy-job-3")
    assert got["status"] == "queued"
    assert got["warnings"] == ["status is not a documented command status"]


def test_submit_deeper_parse_failure_is_an_unknown_outcome():
    # A broken envelope AFTER a 202 accept is reconcilable, not definitive.
    c, transport = client(response(
        202, {"ok": True, "data": {"commandId": COMMAND, "status": "pending",
                                    "result": "not-an-object"}}))
    with pytest.raises(ow.OwletteProtocolError) as caught:
        c.submit_command(SITE, MACHINE, "reboot_machine",
                         idempotency_key="reconcile-with-this")
    assert caught.value.outcome_unknown is True


def test_status_poll_uses_only_the_published_get_route():
    c, transport = client(response(200, {
        "ok": True,
        "data": {
            "commandId": COMMAND, "status": "completed",
            "result": {"screenshot_url": "https://signed.example/object"},
            "createdAt": "2026-08-02T10:00:00Z",
            "updatedAt": "2026-08-02T10:00:01Z",
        },
    }))
    got = c.get_command_status(SITE, MACHINE, COMMAND)
    assert got["status"] == "completed"
    assert got["result"]["screenshot_url"].startswith("https://")
    assert transport.calls[0]["method"] == "GET"
    assert transport.calls[0]["path"].endswith("/commands/cmd_abc_123")
    assert "Idempotency-Key" not in transport.calls[0]["headers"]


def test_status_rejects_non_public_command_id_before_network():
    c, transport = client()
    with pytest.raises(ow.OwletteValidationError):
        c.get_command_status(SITE, MACHINE, "../completed")
    assert transport.calls == []


def test_command_envelopes_are_validated_not_truthiness_coerced():
    c, unused_transport = client(response(200, {
        "ok": 1, "data": {"commandId": COMMAND, "status": "completed"},
    }))
    with pytest.raises(ow.OwletteProtocolError):
        c.get_command_status(SITE, MACHINE, COMMAND)


# -- transport bounds and failure semantics -------------------------


def test_public_api_problem_is_structured_and_never_echoes_the_token():
    c, unused_transport = client(response(409, {
        "type": "https://owlette.app/problems/machine-offline",
        "title": "machine offline", "status": 409,
        "code": "machine_offline", "detail": "target is offline",
        "docsUrl": "https://owlette.app/docs/api/errors#machine_offline",
        "requestId": "req_123",
    }, {"Retry-After": "5"}))
    with pytest.raises(ow.OwletteApiError) as caught:
        c.submit_command(SITE, MACHINE, "reboot_machine",
                         idempotency_key="same-key")
    error = caught.value
    assert error.status == 409
    assert error.code == "machine_offline"
    assert error.request_id == "req_123"
    assert error.outcome_unknown is False
    assert TOKEN not in str(error)
    assert TOKEN not in repr(error.as_dict())


def test_mutating_server_error_is_an_unknown_outcome_and_redacts_token():
    c, unused_transport = client(response(500, {
        "title": "internal error", "status": 500, "code": "internal_error",
        "detail": "debug accidentally included " + TOKEN,
        "requestId": "req_500",
    }))
    with pytest.raises(ow.OwletteApiError) as caught:
        c.submit_command(SITE, MACHINE, "reboot_machine",
                         idempotency_key="reconcile-with-this")
    assert caught.value.outcome_unknown is True
    assert TOKEN not in str(caught.value)
    assert "<redacted>" in caught.value.detail


def test_redirect_is_refused_instead_of_leaking_bearer_to_another_origin():
    c, unused_transport = client(ow.HttpResponse(
        307, {"Location": "https://evil.invalid/collect",
              "Content-Type": "application/json"}, b"{}"))
    with pytest.raises(ow.OwletteProtocolError) as caught:
        c.list_sites()
    assert caught.value.status == 307
    assert "redirect refused" in caught.value.detail


def test_mutating_transport_failure_is_unknown_and_is_not_auto_retried():
    transport = FakeTransport(OSError("wire vanished"))
    c = ow.OwletteClient(ow.OwletteConfig(), lambda: TOKEN, transport)
    with pytest.raises(ow.OwletteTransportError) as caught:
        c.submit_command(SITE, MACHINE, "reboot_machine",
                         idempotency_key="reuse-this-key")
    assert caught.value.outcome_unknown is True
    assert len(transport.calls) == 1


def test_read_transport_failure_is_not_an_unknown_mutation_outcome():
    c, unused_transport = client(OSError("offline"))
    with pytest.raises(ow.OwletteTransportError) as caught:
        c.list_sites()
    assert caught.value.outcome_unknown is False


def test_real_transport_uses_verified_https_origin_and_bounded_timeout():
    seen = []
    wire = WireConnection()

    def factory(host, port, timeout, context):
        seen.append((host, port, timeout, context))
        return wire

    transport = ow.HttpsTransport(connection_factory=factory)
    got = transport.request(
        ow.OwletteConfig(timeout_s=3), "GET", "/api/sites",
        {"Authorization": "Bearer " + TOKEN}, None)
    assert got.status == 200
    assert seen[0][0:3] == ("owlette.app", 443, 3.0)
    assert seen[0][3].check_hostname is True
    assert seen[0][3].verify_mode != 0
    assert wire.connected is True and wire.closed is True
    assert wire.requests[0][0:2] == ("GET", "/api/sites")


def test_real_transport_connect_failure_is_known_not_sent():
    wire = WireConnection(connect_error=OSError("offline"))
    transport = ow.HttpsTransport(connection_factory=lambda *args, **kw: wire)
    with pytest.raises(ow.OwletteTransportError) as caught:
        transport.request(
            ow.OwletteConfig(), "POST", "/api/sites/s/machines/m/commands",
            {}, b"{}")
    assert caught.value.outcome_unknown is False
    assert wire.requests == []
    assert wire.closed is True


def test_real_transport_send_failure_marks_mutation_unknown():
    wire = WireConnection(request_error=OSError("connection reset"))
    transport = ow.HttpsTransport(connection_factory=lambda *args, **kw: wire)
    with pytest.raises(ow.OwletteTransportError) as caught:
        transport.request(
            ow.OwletteConfig(), "POST", "/api/sites/s/machines/m/commands",
            {}, b"{}")
    assert caught.value.outcome_unknown is True
    assert len(wire.requests) == 1


def test_real_transport_refuses_declared_oversize_response():
    wire = WireConnection(response=WireResponse(
        headers={"Content-Type": "application/json",
                 "Content-Length": str(ow.MAX_RESPONSE_BYTES + 1)}))
    transport = ow.HttpsTransport(connection_factory=lambda *args, **kw: wire)
    with pytest.raises(ow.OwletteTransportError) as caught:
        transport.request(ow.OwletteConfig(), "GET", "/api/sites", {}, None)
    assert caught.value.outcome_unknown is False


def test_injected_oversize_response_is_rejected_before_json_parse(monkeypatch):
    monkeypatch.setattr(ow, "MAX_RESPONSE_BYTES", 8)
    c, unused_transport = client(ow.HttpResponse(
        200, {"Content-Type": "application/json"}, b'{"sites":[]}'))
    with pytest.raises(ow.OwletteProtocolError) as caught:
        c.list_sites()
    assert "size limit" in caught.value.detail


def test_idempotency_key_helper_returns_reusable_bounded_ascii():
    key = ow.new_idempotency_key()
    assert key.startswith("embody-convoy-")
    assert len(key.encode("ascii")) <= 255
    assert ow._idempotency_key(key) == key


def test_source_has_no_plaintext_or_credential_query_fallback():
    path = os.path.join(os.path.dirname(__file__), "convoy_owlette.py")
    with open(path, encoding="utf-8") as stream:
        source = stream.read()
    assert "http://" not in source
    assert "?api_key" not in source
    assert "x-api-key" not in source
