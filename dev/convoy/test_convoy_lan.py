"""convoy_lan: the LAN posture switch and the bind-address selection.

Two properties this file holds. The module reads an ABSENT lan.json as
`should_bind == False` and a PRESENT one as authoritative: an operator
who wrote a switch gets NAMED failures, never guesses (the daemon is
what synthesizes the enabled default for Convoy membership -- see
convoy_hostapp.desired_lan_endpoint). And auto-bind classifies the
adapter behind the route probe's answer: a tunnel or a Public network
is skipped for a physical private one, or refused by name, never bound
in silence (TEC-A4D sat on an OpenVPN TAP for eight days, 2026-09-21).
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


# -- adapter inventory and choose_bind (2026-09-21) ----------------------

def _adapter(desc, addresses, kind=None, category=None, gateway=False,
             up=True, name=None):
    return {"name": name or desc, "description": desc,
            "kind": kind or lan.classify_adapter(desc, name or desc),
            "addresses": list(addresses), "up": up, "category": category,
            "gateway": gateway}


ETHERNET = _adapter("Intel(R) I211 Gigabit Network Connection",
                    ["192.168.88.10"], category="Private", gateway=True)
TAP = _adapter("TAP-Windows Adapter V9", ["10.8.0.2"], category="Public")


def test_vpn_drivers_classify_as_tunnels_by_description():
    for desc in ("TAP-Windows Adapter V9", "Wintun Userspace Tunnel",
                 "WireGuard Tunnel", "OpenVPN Data Channel Offload",
                 "Tailscale Tunnel", "Cisco AnyConnect Secure Mobility "
                 "Client Virtual Miniport Adapter for Windows x64"):
        assert lan.classify_adapter(desc) == lan.ADAPTER_TUNNEL, desc
    assert lan.classify_adapter("Intel(R) I211 Gigabit Network "
                                "Connection") == lan.ADAPTER_PHYSICAL
    assert lan.classify_adapter("Realtek PCIe GbE Family Controller",
                                iftype=6) == lan.ADAPTER_PHYSICAL
    assert lan.classify_adapter("Killer E2600 Gigabit Ethernet Controller"
                                ) == lan.ADAPTER_PHYSICAL


def test_interface_types_and_virtual_flags_classify_too():
    assert lan.classify_adapter("Some Adapter", iftype=131) == \
        lan.ADAPTER_TUNNEL
    assert lan.classify_adapter("WAN Miniport (PPTP)", iftype=23) == \
        lan.ADAPTER_TUNNEL
    assert lan.classify_adapter("Software Loopback Interface 1",
                                iftype=24) == lan.ADAPTER_VIRTUAL
    assert lan.classify_adapter("Hyper-V Virtual Ethernet Adapter") == \
        lan.ADAPTER_VIRTUAL
    assert lan.classify_adapter("Some Adapter", "vEthernet (WSL)",
                                virtual=True) == lan.ADAPTER_VIRTUAL


def test_posix_names_classify_by_prefix_not_substring():
    assert lan.classify_posix_name("utun3") == lan.ADAPTER_TUNNEL
    assert lan.classify_posix_name("wg0") == lan.ADAPTER_TUNNEL
    assert lan.classify_posix_name("tailscale0") == lan.ADAPTER_TUNNEL
    assert lan.classify_posix_name("en0") == lan.ADAPTER_PHYSICAL
    assert lan.classify_posix_name("eth0") == lan.ADAPTER_PHYSICAL
    assert lan.classify_posix_name("bridge100") == lan.ADAPTER_VIRTUAL
    assert lan.classify_posix_name("lo0") == lan.ADAPTER_VIRTUAL


def test_the_field_case_moves_the_bind_off_the_vpn():
    """TEC-A4D, 2026-09-13: the route probe answered 10.8.0.2 (OpenVPN
    TAP, Public); the LAN was 192.168.88.10 on the Intel NIC holding the
    default route."""
    posture = lan.choose_bind("10.8.0.2", [ETHERNET, TAP])
    assert posture["address"] == "192.168.88.10"
    assert posture["adapter"]["description"].startswith("Intel")
    assert posture["skipped"]["description"] == "TAP-Windows Adapter V9"
    assert posture["probe_address"] == "10.8.0.2"
    assert posture["warning"] is None
    assert posture["inventory"] == "checked"


def test_a_physical_private_probe_answer_stands():
    posture = lan.choose_bind("192.168.88.10", [ETHERNET, TAP])
    assert posture["address"] == "192.168.88.10"
    assert posture["skipped"] is None


def test_a_tunnel_only_machine_is_refused_by_name():
    with pytest.raises(lan.LanConfigError) as info:
        lan.choose_bind("10.8.0.2", [TAP])
    assert info.value.reason == "lan_tunnel_only"
    assert "lan.json" in info.value.detail
    assert "TAP-Windows Adapter V9" in info.value.detail


def test_a_public_network_is_refused_by_name():
    public = _adapter("Intel(R) Wi-Fi 6 AX201 160MHz", ["192.168.1.7"],
                      category="Public", gateway=True)
    with pytest.raises(lan.LanConfigError) as info:
        lan.choose_bind("192.168.1.7", [public])
    assert info.value.reason == "lan_public_network"
    assert "Private" in info.value.detail


def test_a_private_adapter_is_preferred_over_a_public_one():
    public = _adapter("Intel(R) Wi-Fi 6 AX201 160MHz", ["192.168.1.7"],
                      category="Public", gateway=True)
    private = _adapter("Realtek PCIe GbE Family Controller", ["10.0.0.5"],
                       category="Private")
    posture = lan.choose_bind("192.168.1.7", [public, private])
    assert posture["address"] == "10.0.0.5"
    assert posture["skipped"]["category"] == "Public"


def test_a_gateway_holder_is_preferred_among_physical_candidates():
    first = _adapter("Realtek PCIe GbE Family Controller", ["10.0.0.5"],
                     category="Private")
    second = _adapter("Intel(R) Ethernet Connection", ["10.0.1.5"],
                      category="Private", gateway=True)
    posture = lan.choose_bind("10.8.0.2", [TAP, first, second])
    assert posture["address"] == "10.0.1.5"


def test_a_down_adapter_or_link_local_address_is_never_a_candidate():
    down = _adapter("Realtek PCIe GbE Family Controller", ["10.0.0.5"],
                    category="Private", up=False)
    apipa = _adapter("Intel(R) Ethernet Connection", ["169.254.3.3"],
                     category="Private", gateway=True)
    with pytest.raises(lan.LanConfigError) as info:
        lan.choose_bind("10.8.0.2", [TAP, down, apipa])
    assert info.value.reason == "lan_tunnel_only"


def test_no_inventory_means_the_probe_answer_stands():
    posture = lan.choose_bind("10.8.0.2", None)
    assert posture["address"] == "10.8.0.2"
    assert posture["inventory"] == "unavailable"
    assert lan.choose_bind("10.8.0.2", [])["address"] == "10.8.0.2"


def test_an_unlisted_address_stands_unclassified():
    posture = lan.choose_bind("172.16.0.9", [ETHERNET, TAP])
    assert posture["address"] == "172.16.0.9"
    assert posture["adapter"] is None


def test_an_explicit_bind_on_a_tunnel_is_honoured_and_described(data_dir):
    _write_lan(data_dir, {"enabled": True, "bind": "10.8.0.2"})
    config = lan.load_config(data_dir)
    posture = lan.resolve_bind_posture(config, inventory=[ETHERNET, TAP])
    assert posture["address"] == "10.8.0.2"
    assert posture["explicit"] is True
    assert posture["warning"] == "tunnel_adapter"


def test_resolve_bind_auto_uses_the_inventory(data_dir):
    _write_lan(data_dir, {"enabled": True})
    config = lan.load_config(data_dir)
    address = lan.resolve_bind(
        config, socket_factory=lambda: _FakeUDP("10.8.0.2"),
        inventory=[ETHERNET, TAP])
    assert address == "192.168.88.10"


_PS_JSON = json.dumps({
    "ips": [{"IPAddress": "192.168.88.10", "InterfaceIndex": 12,
             "InterfaceAlias": "Ethernet 2"},
            {"IPAddress": "10.8.0.2", "InterfaceIndex": 20,
             "InterfaceAlias": "Local Area Connection"},
            {"IPAddress": "127.0.0.1", "InterfaceIndex": 1,
             "InterfaceAlias": "Loopback Pseudo-Interface 1"}],
    "adapters": [
        {"InterfaceIndex": 12, "Name": "Ethernet 2", "Status": "Up",
         "InterfaceDescription": "Intel(R) I211 Gigabit Network Connection",
         "Virtual": False, "InterfaceType": 6},
        {"InterfaceIndex": 20, "Name": "Local Area Connection",
         "InterfaceDescription": "TAP-Windows Adapter V9", "Status": "Up",
         "Virtual": False, "InterfaceType": 6}],
    "profiles": [{"InterfaceIndex": 12, "NetworkCategory": 1},
                 {"InterfaceIndex": 20, "NetworkCategory": 0}],
    "routes": [{"InterfaceIndex": 12}],
})


def test_windows_inventory_parses_powershell_json():
    inventory = lan.parse_windows_inventory(_PS_JSON)
    by_desc = {a["description"]: a for a in inventory}
    intel = by_desc["Intel(R) I211 Gigabit Network Connection"]
    assert intel["kind"] == lan.ADAPTER_PHYSICAL
    assert intel["category"] == "Private"
    assert intel["gateway"] is True
    assert intel["addresses"] == ["192.168.88.10"]
    tap = by_desc["TAP-Windows Adapter V9"]
    assert tap["kind"] == lan.ADAPTER_TUNNEL
    assert tap["category"] == "Public"
    assert tap["gateway"] is False
    assert by_desc["Loopback Pseudo-Interface 1"]["kind"] == \
        lan.ADAPTER_VIRTUAL
    assert lan.choose_bind("10.8.0.2", inventory)["address"] == \
        "192.168.88.10"


def test_windows_inventory_reads_string_categories_and_single_objects():
    data = json.loads(_PS_JSON)
    data["profiles"] = {"InterfaceIndex": 12, "NetworkCategory": "Private"}
    data["routes"] = {"InterfaceIndex": 12}
    inventory = lan.parse_windows_inventory(json.dumps(data))
    intel = next(a for a in inventory if a["description"].startswith("Intel"))
    assert intel["category"] == "Private"
    assert intel["gateway"] is True
    assert lan.parse_windows_inventory("not json") is None
    assert lan.parse_windows_inventory("[]") is None


_IFCONFIG = """lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> mtu 16384
\tinet 127.0.0.1 netmask 0xff000000
en0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tinet 192.168.88.36 netmask 0xffffff00 broadcast 192.168.88.255
utun3: flags=8051<UP,POINTOPOINT,RUNNING,MULTICAST> mtu 1280
\tinet 100.64.0.9 --> 100.64.0.9 netmask 0xffffffff
"""


def test_ifconfig_inventory_classifies_by_name():
    inventory = lan.parse_ifconfig_inventory(_IFCONFIG, default_iface="en0")
    by_name = {a["name"]: a for a in inventory}
    assert by_name["en0"]["kind"] == lan.ADAPTER_PHYSICAL
    assert by_name["en0"]["gateway"] is True
    assert by_name["utun3"]["kind"] == lan.ADAPTER_TUNNEL
    assert by_name["utun3"]["addresses"] == ["100.64.0.9"]
    assert by_name["lo0"]["kind"] == lan.ADAPTER_VIRTUAL
    assert lan.choose_bind("100.64.0.9", inventory)["address"] == \
        "192.168.88.36"


def test_ip_inventory_reads_sysfs_tunnel_flags():
    text = ("2: eth0    inet 192.168.1.5/24 brd 192.168.1.255 scope global "
            "eth0\n3: vpn0    inet 10.9.0.2/24 scope global vpn0\n")
    inventory = lan.parse_ip_inventory(
        text, default_iface="eth0",
        sysfs={"vpn0": {"tun": True, "operstate": "up"},
               "eth0": {"tun": False, "operstate": "up", "type": 1}})
    by_name = {a["name"]: a for a in inventory}
    assert by_name["vpn0"]["kind"] == lan.ADAPTER_TUNNEL
    assert by_name["eth0"]["gateway"] is True
    assert lan.choose_bind("10.9.0.2", inventory)["address"] == "192.168.1.5"


def test_adapter_inventory_never_raises_and_uses_the_runner():
    calls = []

    def runner(argv, timeout_s=None):
        calls.append(argv)
        return _PS_JSON

    # The conftest patches the public name off the OS for every test;
    # this is the one test OF the inventory, so it drives the impl.
    inventory = lan._adapter_inventory(platform="win32", runner=runner)
    assert calls and calls[0][0] == "powershell.exe"
    assert "-NonInteractive" in calls[0]
    assert any(a["kind"] == lan.ADAPTER_TUNNEL for a in inventory)
    assert lan._adapter_inventory(platform="win32",
                                  runner=lambda *a, **k: None) is None

    def boom(argv, timeout_s=None):
        raise RuntimeError("no")

    assert lan._adapter_inventory(platform="darwin", runner=boom) is None
    assert lan._adapter_inventory(platform="linux", runner=boom) is None


def test_inventory_cache_asks_the_os_once_per_ttl():
    clock = [0.0]
    loads = []

    def loader():
        loads.append(1)
        return [ETHERNET]

    cache = lan.InventoryCache(ttl_s=600, loader=loader,
                               now=lambda: clock[0])
    assert cache.get() == [ETHERNET]
    assert cache.get() == [ETHERNET]
    assert len(loads) == 1
    clock[0] = 601
    cache.get()
    assert len(loads) == 2
    cache.get(refresh=True)
    assert len(loads) == 3


def test_the_docstring_no_longer_claims_absent_means_no_socket():
    assert "NO LAN SOCKET, EVER" not in lan.__doc__
    assert "desired_lan_endpoint" in lan.__doc__
