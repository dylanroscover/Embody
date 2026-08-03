"""Host/loopback integration for the deliberately narrow Owlette seam."""

from types import SimpleNamespace

import pytest

import convoy_hostapp as hostapp
import convoy_owlette as owlette
from test_convoy_hostapp import Server


class FakeOwletteClient:
    def __init__(self):
        self.config = SimpleNamespace(
            base_url=owlette.PRODUCTION_BASE_URL,
            default_site_id="site-default")
        self.calls = []
        self.error = None

    def _answer(self, name, value):
        self.calls.append((name, value))
        if self.error is not None:
            raise self.error
        return value

    def list_sites(self):
        return self._answer("list_sites", [{"id": "s", "name": "Site"}])

    def get_site(self, site_id):
        return self._answer("get_site", {"id": site_id, "name": "Site"})

    def list_machines(self, site_id):
        return self._answer("list_machines", [{
            "id": "m", "name": "Machine", "online": True}])

    def get_machine(self, site_id, machine_id):
        return self._answer("get_machine", {
            "id": machine_id, "name": "Machine", "online": True,
            "siteId": site_id})

    def get_command_status(self, site_id, machine_id, command_id):
        return self._answer("command_status", {
            "commandId": command_id, "status": "completed"})

    def submit_command(self, site_id, machine_id, command_type, **kwargs):
        self.calls.append(("submit_command", {
            "site_id": site_id, "machine_id": machine_id,
            "command_type": command_type, **kwargs}))
        if self.error is not None:
            raise self.error
        return {"commandId": "cmd-1", "status": "pending",
                "idempotencyKey": kwargs["idempotency_key"]}


@pytest.fixture
def server(tmp_path):
    instance = Server(str(tmp_path / "host"))
    fake = FakeOwletteClient()
    instance.app._owlette_client = fake
    instance.app._owlette_command_policy = lambda: False
    yield instance, fake
    instance.stop()


def test_capabilities_are_local_metadata_and_do_not_contact_owlette(server):
    instance, fake = server
    code, payload = instance.call("/owlette", {"action": "capabilities"})
    assert code == 200
    assert payload["ok"] is True
    assert payload["integration"] == "owlette-public-api"
    assert payload["openapi_version"] == owlette.OPENAPI_VERSION
    assert payload["capabilities"]["machine_inventory"] is True
    assert payload["capabilities"]["command_cancel"] is False
    assert payload["wakes_touchdesigner"] is False
    assert fake.calls == []


@pytest.mark.parametrize("body,key,call_name", [
    ({"action": "list_sites"}, "sites", "list_sites"),
    ({"action": "get_site", "site_id": "s"}, "site", "get_site"),
    ({"action": "list_machines", "site_id": "s"},
     "machines", "list_machines"),
    ({"action": "get_machine", "site_id": "s", "machine_id": "m"},
     "machine", "get_machine"),
    ({"action": "command_status", "site_id": "s", "machine_id": "m",
      "command_id": "cmd"}, "command", "command_status"),
])
def test_read_actions_dispatch_only_to_the_typed_public_client(
        server, body, key, call_name):
    instance, fake = server
    code, payload = instance.call("/owlette", body)
    assert code == 200 and payload["ok"] is True
    assert key in payload
    assert fake.calls[-1][0] == call_name


def test_command_submission_requires_a_separate_local_opt_in(server):
    instance, fake = server
    body = {
        "action": "submit_command", "site_id": "s", "machine_id": "m",
        "command_type": "capture_screenshot", "idempotency_key": "stable",
        "params": {}, "timeout_seconds": 30,
    }
    code, payload = instance.call("/owlette", body)
    assert code == 403
    assert payload["reason"] == "owlette_commands_not_approved"
    assert fake.calls == []

    instance.app._owlette_command_policy = lambda: True
    code, payload = instance.call("/owlette", body)
    assert code == 200 and payload["command"]["commandId"] == "cmd-1"
    call = fake.calls[-1][1]
    assert call["idempotency_key"] == "stable"
    assert call["timeout_seconds"] == 30


def test_generic_mcp_tool_call_is_never_an_owlette_tunnel(server):
    instance, fake = server
    instance.app._owlette_command_policy = lambda: True
    code, payload = instance.call("/owlette", {
        "action": "submit_command", "site_id": "s", "machine_id": "m",
        "command_type": "mcp_tool_call", "idempotency_key": "stable",
    })
    assert code == 403
    assert payload["reason"] == "owlette_tunnel_forbidden"
    assert fake.calls == []


def test_owlette_errors_keep_named_outcomes_and_do_not_leak_secrets(server):
    instance, fake = server
    fake.error = owlette.OwletteCredentialUnavailable(
        "Owlette API key is not configured")
    code, payload = instance.call("/owlette", {"action": "list_sites"})
    assert code == 503
    assert payload["reason"] == "owlette_credentials_unavailable"
    assert "secret" not in repr(payload).lower()

    fake.error = owlette.OwletteTransportError(
        "request failed after transmission may have begun",
        outcome_unknown=True)
    code, payload = instance.call("/owlette", {"action": "list_sites"})
    assert code == 502
    assert payload["reason"] == "owlette_transport_failed"
    assert payload["outcome_unknown"] is True


def test_owlette_route_is_authenticated_and_malformed_actions_fail_closed(
        server):
    instance, fake = server
    code, payload = instance.call(
        "/owlette", {"action": "list_sites"}, token=None)
    assert code == 401 and payload["reason"] == "unauthenticated"
    assert fake.calls == []

    code, payload = instance.call("/owlette", {"action": "not-public"})
    assert code == 400 and payload["reason"] == "unsupported_action"
    assert fake.calls == []


def test_default_command_policy_requires_literal_local_opt_in(monkeypatch):
    monkeypatch.delenv(hostapp.OWLETTE_COMMAND_OPT_IN_ENV, raising=False)
    assert hostapp.HostApp._allow_owlette_commands() is False
    monkeypatch.setenv(hostapp.OWLETTE_COMMAND_OPT_IN_ENV, "yes")
    assert hostapp.HostApp._allow_owlette_commands() is True
    monkeypatch.setenv(hostapp.OWLETTE_COMMAND_OPT_IN_ENV, "anything-else")
    assert hostapp.HostApp._allow_owlette_commands() is False


@pytest.mark.parametrize("command_type", [
    "reboot_machine", "shutdown_machine", "restart_process", "kill_process"])
def test_machine_affecting_commands_need_a_durable_policy_opt_in(
        server, command_type):
    """A-35: reboot/shutdown/process-kill can take a show machine down with no
    unsaved-work check, so the benign env opt-in is not enough -- they need a
    durable policy approval, and are default-denied until it exists."""
    instance, fake = server
    instance.app._owlette_command_policy = lambda: True  # benign env opt-in ON
    body = {
        "action": "submit_command", "site_id": "s", "machine_id": "m",
        "command_type": command_type, "idempotency_key": "stable",
    }
    code, payload = instance.call("/owlette", body)
    assert code == 403
    assert payload["reason"] == "owlette_machine_command_not_approved"
    assert payload["command_type"] == command_type
    assert fake.calls == []

    # With the durable policy opt-in ALSO granted it reaches the client.
    instance.app._allow_owlette_machine_commands = lambda: True
    code, payload = instance.call("/owlette", body)
    assert code == 200 and payload["command"]["commandId"] == "cmd-1"
    assert fake.calls[-1][1]["command_type"] == command_type


def test_machine_affecting_command_defaults_denied_even_with_env_optin(server):
    """Default-deny: PolicyStore does not expose the durable machine-command
    opt-in yet, so the real reader denies and a reboot is refused even when the
    benign env flag is on."""
    instance, fake = server
    instance.app._owlette_command_policy = lambda: True
    assert instance.app._allow_owlette_machine_commands() is False
    code, payload = instance.call("/owlette", {
        "action": "submit_command", "site_id": "s", "machine_id": "m",
        "command_type": "reboot_machine", "idempotency_key": "k"})
    assert code == 403
    assert payload["reason"] == "owlette_machine_command_not_approved"
    assert fake.calls == []


def test_benign_command_is_unaffected_by_the_machine_command_gate(server):
    """A non-machine-affecting command (capture_screenshot) still needs only
    the benign env opt-in -- the machine-command gate does not touch it."""
    instance, fake = server
    instance.app._owlette_command_policy = lambda: True
    code, payload = instance.call("/owlette", {
        "action": "submit_command", "site_id": "s", "machine_id": "m",
        "command_type": "capture_screenshot", "idempotency_key": "stable"})
    assert code == 200 and payload["command"]["commandId"] == "cmd-1"
    assert fake.calls[-1][1]["command_type"] == "capture_screenshot"
