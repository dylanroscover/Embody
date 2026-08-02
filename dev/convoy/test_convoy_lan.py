"""convoy_lan: the LAN posture switch and the bind-address selection.

THE property this file exists to hold: a default build has NO lan.json,
and that must mean NO LAN SOCKET, EVER -- not a default port quietly
opened, not a wildcard bind. Everything else is about failing NAMED
rather than guessing when an operator DID write a switch.
"""

import json
import os

import pytest

import convoy_lan as lan


@pytest.fixture
def data_dir(tmp_path):
    d = str(tmp_path / "state")
    os.makedirs(d, exist_ok=True)
    return d


def _write_lan(data_dir, obj):
    with open(os.path.join(data_dir, lan.LAN_FILE), "w", encoding="utf-8") as f:
        f.write(json.dumps(obj))


# -- absent = no LAN, ever ---------------------------------------------

def test_absent_lan_json_is_disabled_and_not_an_error(data_dir):
    config = lan.load_config(data_dir)
    assert config.present is False
    assert config.enabled is False
    assert config.should_bind is False


def test_absent_is_the_default_of_a_fresh_data_dir(tmp_path):
    # No file written at all -- the shipped-build state.
    config = lan.load_config(str(tmp_path))
    assert config.should_bind is False


def test_enabled_false_does_not_bind_even_when_present(data_dir):
    _write_lan(data_dir, {"enabled": False, "port": 47600})
    config = lan.load_config(data_dir)
    assert config.present is True
    assert config.should_bind is False


def test_enabled_true_present_binds(data_dir):
    _write_lan(data_dir, {"enabled": True})
    config = lan.load_config(data_dir)
    assert config.should_bind is True
    assert config.port == lan.DEFAULT_LAN_PORT
    assert config.bind == "auto"


# -- the fixed default port --------------------------------------------

def test_default_port_is_47600(data_dir):
    _write_lan(data_dir, {"enabled": True})
    assert lan.load_config(data_dir).port == 47600


def test_an_explicit_port_is_honoured(data_dir):
    _write_lan(data_dir, {"enabled": True, "port": 50000})
    assert lan.load_config(data_dir).port == 50000


@pytest.mark.parametrize("bad", [0, 65536, -1, "47600", True, 1.5])
def test_a_bad_port_is_a_named_refusal(data_dir, bad):
    _write_lan(data_dir, {"enabled": True, "port": bad})
    with pytest.raises(lan.LanConfigError) as e:
        lan.load_config(data_dir)
    assert e.value.reason == "lan_config_malformed"


# -- malformed file = named refusal, never a guess ---------------------

def test_malformed_json_refuses_named(data_dir):
    with open(os.path.join(data_dir, lan.LAN_FILE), "w") as f:
        f.write("{not json")
    with pytest.raises(lan.LanConfigError) as e:
        lan.load_config(data_dir)
    assert e.value.reason == "lan_config_malformed"


def test_a_non_object_top_level_refuses(data_dir):
    _write_lan(data_dir, [1, 2, 3])
    with pytest.raises(lan.LanConfigError) as e:
        lan.load_config(data_dir)
    assert e.value.reason == "lan_config_malformed"


def test_a_non_bool_enabled_refuses(data_dir):
    _write_lan(data_dir, {"enabled": "yes"})
    with pytest.raises(lan.LanConfigError) as e:
        lan.load_config(data_dir)
    assert e.value.reason == "lan_config_malformed"


# -- the bind literal: v4 only, never a wildcard/loopback/link-local ---

def test_explicit_bind_literal_is_kept(data_dir):
    _write_lan(data_dir, {"enabled": True, "bind": "192.168.1.50"})
    config = lan.load_config(data_dir)
    assert config.bind == "192.168.1.50"
    assert lan.resolve_bind(config) == "192.168.1.50"


def test_ipv6_bind_is_refused_by_name(data_dir):
    _write_lan(data_dir, {"enabled": True, "bind": "::1"})
    with pytest.raises(lan.LanConfigError) as e:
        lan.load_config(data_dir)
    assert e.value.reason == "lan_bind_unsupported"


def test_link_local_bind_is_refused(data_dir):
    _write_lan(data_dir, {"enabled": True, "bind": "169.254.1.1"})
    with pytest.raises(lan.LanConfigError) as e:
        lan.load_config(data_dir)
    assert e.value.reason == "lan_bind_link_local"


def test_loopback_bind_is_refused(data_dir):
    _write_lan(data_dir, {"enabled": True, "bind": "127.0.0.1"})
    with pytest.raises(lan.LanConfigError) as e:
        lan.load_config(data_dir)
    assert e.value.reason == "lan_bind_loopback"


def test_wildcard_bind_is_refused(data_dir):
    _write_lan(data_dir, {"enabled": True, "bind": "0.0.0.0"})
    with pytest.raises(lan.LanConfigError) as e:
        lan.load_config(data_dir)
    assert e.value.reason == "lan_bind_wildcard"


def test_a_garbage_bind_literal_is_refused(data_dir):
    _write_lan(data_dir, {"enabled": True, "bind": "not-an-ip"})
    with pytest.raises(lan.LanConfigError) as e:
        lan.load_config(data_dir)
    assert e.value.reason == "lan_bind_invalid"


# -- primary_ipv4: the no-packet trick, every branch injected ----------

class _FakeUDP:
    """A stand-in for a UDP socket that reports a chosen source address
    from getsockname -- so the selection AND the rejection logic run on
    any machine, no real interface required (D-5)."""

    def __init__(self, local, raise_on_connect=False):
        self._local = local
        self._raise = raise_on_connect
        self.connected = None
        self.closed = False

    def connect(self, target):
        if self._raise:
            raise OSError("no route")
        self.connected = target

    def getsockname(self):
        return (self._local, 0)

    def close(self):
        self.closed = True


def test_primary_ipv4_returns_a_routable_address():
    got = lan.primary_ipv4(socket_factory=lambda: _FakeUDP("192.168.1.42"))
    assert got == "192.168.1.42"


def test_primary_ipv4_connect_sends_to_a_documentation_range():
    fake = _FakeUDP("10.0.0.9")
    lan.primary_ipv4(socket_factory=lambda: fake)
    # A datagram connect emits nothing; assert we target the RFC 5737
    # documentation range and never a real host.
    assert fake.connected[0].startswith("203.0.113.")
    assert fake.closed is True


def test_primary_ipv4_rejects_loopback():
    assert lan.primary_ipv4(
        socket_factory=lambda: _FakeUDP("127.0.0.1")) is None


def test_primary_ipv4_rejects_link_local():
    assert lan.primary_ipv4(
        socket_factory=lambda: _FakeUDP("169.254.9.9")) is None


def test_primary_ipv4_rejects_unspecified():
    assert lan.primary_ipv4(
        socket_factory=lambda: _FakeUDP("0.0.0.0")) is None


def test_primary_ipv4_none_when_probe_raises():
    assert lan.primary_ipv4(
        socket_factory=lambda: _FakeUDP("x", raise_on_connect=True)) is None


def test_the_socket_is_always_closed_even_on_failure():
    fake = _FakeUDP("x", raise_on_connect=True)
    lan.primary_ipv4(socket_factory=lambda: fake)
    assert fake.closed is True


# -- resolve_bind ------------------------------------------------------

def test_resolve_bind_auto_uses_primary_ipv4(data_dir):
    _write_lan(data_dir, {"enabled": True, "bind": "auto"})
    config = lan.load_config(data_dir)
    got = lan.resolve_bind(config,
                           socket_factory=lambda: _FakeUDP("192.168.5.5"))
    assert got == "192.168.5.5"


def test_resolve_bind_auto_refuses_when_no_route(data_dir):
    _write_lan(data_dir, {"enabled": True, "bind": "auto"})
    config = lan.load_config(data_dir)
    with pytest.raises(lan.LanConfigError) as e:
        lan.resolve_bind(config,
                         socket_factory=lambda: _FakeUDP("127.0.0.1"))
    assert e.value.reason == "lan_no_route"


def test_resolve_bind_refuses_a_disabled_config(tmp_path):
    config = lan.load_config(str(tmp_path))       # absent -> disabled
    with pytest.raises(lan.LanConfigError) as e:
        lan.resolve_bind(config)
    assert e.value.reason == "lan_disabled"
