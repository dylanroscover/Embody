"""LAN transport configuration for the embody-convoy host app (Phase 3
slice 3). Pure stdlib, no TD, no `cryptography`, no socket bind of its
own -- it decides WHETHER and WHERE to bind, and convoy_peerserver does
the binding.

THREE facts live here, and one non-negotiable default:

  - lan.json: the host-private OVERRIDE for the LAN listener. The
    exposure gate is Enable Convoy on at least one local node (the
    daemon binds for enabled membership with no lan.json at all, see
    convoy_hostapp.desired_lan_endpoint); a present lan.json can only
    NARROW that -- `enabled: false` is the emergency off switch, `bind`
    names the interface, `port` the port. This module still reads an
    absent file as `should_bind == False` for its own callers; the
    daemon is the one that synthesizes the enabled default. (Until
    6.2.61 this docstring said an absent file meant no socket ever --
    a field diagnosis trusted it and lost a day, 2026-09-21.)

  - the BIND ADDRESS: never 0.0.0.0 by default (24.3). The primary
    outbound IPv4 is found with the stdlib no-packet trick -- a UDP
    socket `connect`ed to a documentation-range address reveals the
    interface the OS would route through, WITHOUT a single packet
    leaving the machine (a datagram connect only sets the socket's
    default peer). Loopback/link-local are excluded; IPv6 and
    link-local are refused with a NAMED reason (plan section 6), not
    silently coerced.

  - the ADAPTER behind that address (adapter_inventory / choose_bind):
    the route probe follows whatever owns the default route, and on
    2026-09-13 that was an OpenVPN TAP adapter (10.8.0.2) on a Windows
    network classed Public -- the listener sat there unreachable for
    eight days while Status read Connected. Auto-bind therefore
    classifies the owning adapter by interface type and driver
    description (tunnel / virtual / physical) plus the Windows network
    category, prefers a physical adapter that holds the default route,
    and REFUSES -- named -- to auto-bind a tunnel-only or Public
    network. An explicit `bind` is honoured as written and merely
    described, so an operator can still choose the tunnel on purpose.

  - the PORT: a FIXED default (47600). Across machines there is no
    portfile a peer could read, so a peer must be able to NAME the port
    -- which means it cannot be OS-assigned. A taken port REFUSES the
    LAN listener with a named message and leaves loopback serving; it
    NEVER falls back to a random port no peer could find.

Every platform/network branch is INJECTABLE (D-5): the socket factory,
the adapter inventory and the command runner behind it are parameters,
so the foreign-network paths are exercised on any machine rather than by
owning the hardware.
"""

import ipaddress
import json
import os
import re
import socket
import subprocess
import sys
import time


LAN_FILE = "lan.json"

# The fixed default LAN port. A peer names host:port out of band (slice 4
# admission); there is no cross-machine portfile, so this cannot be
# OS-assigned. 47600 is high, static, and unregistered.
DEFAULT_LAN_PORT = 47600

# The no-packet-trick target: TEST-NET-3 (203.0.113.0/24, RFC 5737
# documentation range). A UDP `connect` to it never emits a packet -- it
# only fixes the socket's default peer, which is enough for the kernel to
# fill in the source address it WOULD route from. A documentation-range
# address guarantees we are not probing a real host even in the
# impossible event a datagram did leave.
_ROUTE_PROBE_TARGET = ("203.0.113.1", 9)


class LanConfigError(Exception):
    """lan.json is present but unusable. Distinct from ABSENT (which is
    the ordinary 'no LAN' state and never an error). `reason` is a stable
    machine-readable code."""

    def __init__(self, reason, detail=""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


class LanConfig:
    """The parsed LAN posture. `enabled` is the ONLY thing that decides
    whether a socket binds; everything else describes the bind once it
    is on."""

    __slots__ = ("enabled", "port", "bind", "present")

    def __init__(self, enabled, port, bind, present):
        self.enabled = bool(enabled)
        self.port = int(port)
        # "auto" or an explicit IPv4 literal. Resolved to a concrete
        # address by resolve_bind(); kept as the operator wrote it so an
        # explicit choice is never silently overridden.
        self.bind = bind
        # Whether lan.json existed at load. False is the default build's
        # state and means the listener MUST NOT bind, independent of the
        # (default) enabled value.
        self.present = bool(present)

    @property
    def should_bind(self):
        """The single gate: bind a LAN socket ONLY when lan.json exists
        AND enables it. Absent file -> never, whatever the defaults."""
        return self.present and self.enabled

    def as_dict(self):
        return {"enabled": self.enabled, "port": self.port,
                "bind": self.bind, "present": self.present}


def lan_path(data_dir):
    return os.path.join(data_dir, LAN_FILE)


def load_config(data_dir):
    """Read lan.json. Returns a LanConfig; ABSENT is a disabled config,
    never an error. A PRESENT-but-broken file raises LanConfigError --
    an operator who wrote a LAN switch and fat-fingered it must be told,
    not silently left off-box (that is the wrong failure for a security
    switch, exactly the fail-closed reasoning the denylist uses).
    """
    path = lan_path(data_dir)
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
    except FileNotFoundError:
        # THE DEFAULT. No LAN, no error, no socket. This is the state of
        # every shipped build.
        return LanConfig(enabled=False, port=DEFAULT_LAN_PORT,
                         bind="auto", present=False)
    except OSError as e:
        raise LanConfigError(
            "lan_config_unreadable",
            f"{path} exists but could not be read ({e}); refusing to "
            f"guess a LAN posture")
    try:
        data = json.loads(raw)
    except ValueError as e:
        raise LanConfigError(
            "lan_config_malformed",
            f"{path} is not valid JSON ({e})")
    if not isinstance(data, dict):
        raise LanConfigError(
            "lan_config_malformed",
            f"{path} must be a JSON object, got {type(data).__name__}")

    enabled = data.get("enabled", False)
    if not isinstance(enabled, bool):
        raise LanConfigError(
            "lan_config_malformed",
            "'enabled' must be true or false")

    port = data.get("port", DEFAULT_LAN_PORT)
    if not isinstance(port, int) or isinstance(port, bool) \
            or not (1 <= port <= 65535):
        raise LanConfigError(
            "lan_config_malformed",
            f"'port' must be an integer 1..65535, got {port!r}")

    bind = data.get("bind", "auto")
    if not isinstance(bind, str) or not bind:
        raise LanConfigError(
            "lan_config_malformed",
            "'bind' must be a non-empty string ('auto' or an IPv4 literal)")
    bind = bind.strip()
    # Validate an explicit bind eagerly so a bad literal fails at load,
    # named, rather than deep inside the bind call as an opaque OSError.
    if bind.lower() != "auto":
        _validate_bind_literal(bind)

    return LanConfig(enabled=enabled, port=port, bind=bind, present=True)


def _validate_bind_literal(bind):
    """An explicit `bind` must be a routable IPv4 literal. IPv6 and
    link-local are refused BY NAME (plan section 6: no IPv6/link-local
    this phase), never coerced -- an operator who typed one gets told
    why, not a listener quietly bound somewhere else."""
    try:
        addr = ipaddress.ip_address(bind)
    except ValueError:
        raise LanConfigError(
            "lan_bind_invalid",
            f"{bind!r} is not an IP address")
    if addr.version != 4:
        raise LanConfigError(
            "lan_bind_unsupported",
            f"{bind!r} is IPv6; Convoy binds IPv4 only this phase")
    if addr.is_loopback:
        raise LanConfigError(
            "lan_bind_loopback",
            f"{bind!r} is loopback -- that is not a LAN bind; omit lan.json "
            f"or set bind 'auto' for the primary interface")
    if addr.is_link_local:
        raise LanConfigError(
            "lan_bind_link_local",
            f"{bind!r} is link-local (169.254/16); Convoy refuses "
            f"link-local addresses this phase")
    if addr.is_unspecified:
        raise LanConfigError(
            "lan_bind_wildcard",
            "0.0.0.0 is refused: Convoy never binds the wildcard by "
            "default (24.3); name the interface explicitly or use 'auto'")


def primary_ipv4(socket_factory=None):
    """The IPv4 address of the interface the OS would route outbound
    from, via the no-packet trick. Returns the dotted string, or None
    when no routable IPv4 could be determined (a machine with only
    loopback, or a probe that failed).

    socket_factory is injected (D-5) so the selection logic -- including
    the loopback/link-local rejection below -- is exercised without a
    real interface. It must return an object with `connect`, `getsockname`
    and `close`.
    """
    factory = socket_factory or _default_udp_socket
    sock = factory()
    try:
        # A datagram connect emits NOTHING; it only records the default
        # peer, which is enough for getsockname to report the source the
        # kernel would use. Wrapped so a probe failure is None, never a
        # crash -- a machine with no route still starts (loopback only).
        try:
            sock.connect(_ROUTE_PROBE_TARGET)
            local = sock.getsockname()[0]
        except OSError:
            return None
    finally:
        try:
            sock.close()
        except OSError:
            pass
    if not local:
        return None
    try:
        addr = ipaddress.ip_address(local)
    except ValueError:
        return None
    # Reject exactly the addresses that are NOT a LAN identity: the probe
    # can hand back loopback (no real route), link-local (169.254, no
    # DHCP lease), or an unspecified address. A VPN or virtual adapter
    # that owns the default route legitimately answers here -- Convoy
    # cannot tell "the VPN" from "the LAN" without policy the operator
    # has not given, so an explicit `bind` is the escape hatch, and the
    # named refusal points at it.
    if addr.version != 4 or addr.is_loopback or addr.is_link_local \
            or addr.is_unspecified:
        return None
    return str(addr)


def _default_udp_socket():
    return socket.socket(socket.AF_INET, socket.SOCK_DGRAM)


def resolve_bind(config, socket_factory=None, inventory=None, posture=None):
    """The concrete IPv4 address the LAN listener should bind, or raise
    LanConfigError naming why it cannot. Never returns 0.0.0.0.

    An explicit `bind` literal was already validated at load; here it is
    only described. "auto" runs primary_ipv4 and refuses -- named -- when
    no routable IPv4 exists, rather than falling back to a wildcard or
    loopback bind that would either expose everything or nothing. With
    an `inventory` (adapter_inventory()), the probe's answer is checked
    against the adapter that owns it: see choose_bind. Without one the
    probe's answer stands, as it did before 6.2.61. A `posture` dict, when
    passed, is filled with WHERE the address sits (describe_bind /
    choose_bind); the daemon reads it, and a test that stubs this
    function for a loopback bind leaves it empty.
    """
    if not config.should_bind:
        # A caller that reached here without should_bind is a bug: the
        # daemon must consult should_bind BEFORE trying to bind. Name it
        # rather than binding something.
        raise LanConfigError(
            "lan_disabled",
            "resolve_bind called while the LAN listener is disabled "
            "(no lan.json, or enabled=false)")
    if config.bind.lower() != "auto":
        found = describe_bind(config.bind, inventory, explicit=True)
    else:
        address = primary_ipv4(socket_factory=socket_factory)
        if address is None:
            raise LanConfigError(
                "lan_no_route",
                "could not determine a routable IPv4 interface to bind "
                "(only loopback/link-local found). Set an explicit 'bind' "
                "in lan.json to name the interface")
        found = choose_bind(address, inventory)
    if posture is not None:
        posture.update(found)
    return found["address"]


def resolve_bind_posture(config, socket_factory=None, inventory=None):
    """resolve_bind, returning the whole posture dict instead."""
    posture = {}
    resolve_bind(config, socket_factory=socket_factory, inventory=inventory,
                 posture=posture)
    return posture


# ---------------------------------------------------------------------------
# Adapter inventory: WHICH interface owns an address, and what kind it is.
#
# Stdlib only, like the rest of the daemon. Windows asks PowerShell's
# NetAdapter/NetTCPIP/NetConnection cmdlets in ONE process (interface
# type, driver description, virtual flag, network category, default
# route); macOS reads ifconfig + route; Linux reads `ip` + sysfs. Every
# reader is best-effort: an inventory that cannot be built is None, and
# None means "classify nothing" -- never "refuse everything".
# ---------------------------------------------------------------------------

ADAPTER_PHYSICAL = "physical"
ADAPTER_TUNNEL = "tunnel"
ADAPTER_VIRTUAL = "virtual"

CATEGORY_PUBLIC = "Public"
CATEGORY_PRIVATE = "Private"
CATEGORY_DOMAIN = "DomainAuthenticated"

# IANA ifType values that are tunnels or point-to-point links whatever the
# driver calls itself: 131 tunnel, 23 ppp. 24 is softwareLoopback.
_TUNNEL_IFTYPES = frozenset({131, 23})
_LOOPBACK_IFTYPES = frozenset({24})

# Substrings (lower-cased) of driver descriptions / interface names that
# mark a tunnel. Vendors name their adapters after the product, not the
# interface type, and TAP-Windows reports ifType 6 (ethernet).
_TUNNEL_MARKERS = (
    "tap-windows", "tap-win", "wintun", "wireguard", "openvpn", "tailscale",
    "zerotier", "hamachi", "nordlynx", "expressvpn", "anyconnect",
    "globalprotect", "fortissl", "fortinet ssl", "pulse secure",
    "juniper networks virtual", "sonicwall", "cloudflare warp", "mullvad",
    "proton", " vpn", "vpn ", "ipsec", "l2tp", "pptp", "utun", "wg",
    "ppp", "tun", "tap",
)
# Substrings that mark a virtual adapter: a real interface of a
# hypervisor, container runtime or loopback driver, never the LAN.
_VIRTUAL_MARKERS = (
    "hyper-v", "vethernet", "vmware", "virtualbox", "vboxnet", "vmnet",
    "docker", "wsl", "bluetooth", "loopback", "npcap", "wi-fi direct",
    "teredo", "isatap", "6to4", "bridge", "awdl", "llw", "veth", "virbr",
    "br-", "vnic", "vlan", "parallels", "utm", "qemu",
)
# POSIX interface NAMES are prefix-matched, not substring-matched: an
# `en0` must never match `tun` inside some longer word.
_TUNNEL_NAME_PREFIXES = ("utun", "tun", "tap", "ppp", "wg", "ipsec",
                         "tailscale", "zt", "nordlynx", "gpd")
_VIRTUAL_NAME_PREFIXES = ("bridge", "awdl", "llw", "docker", "veth",
                          "virbr", "br-", "vmnet", "vboxnet", "lo",
                          "anpi", "ap", "vnic", "stf", "gif")

# How long a built inventory is trusted before the OS is asked again.
INVENTORY_TTL_S = 600.0
_INVENTORY_COMMAND_TIMEOUT_S = 12.0

_POWERSHELL_INVENTORY = (
    "$ErrorActionPreference='SilentlyContinue';"
    "$ips=@(Get-NetIPAddress -AddressFamily IPv4 | "
    "Select-Object IPAddress,InterfaceIndex,InterfaceAlias);"
    "$ads=@(Get-NetAdapter -IncludeHidden | "
    "Select-Object InterfaceIndex,InterfaceDescription,Name,Status,"
    "Virtual,InterfaceType);"
    "$prof=@(Get-NetConnectionProfile | "
    "Select-Object InterfaceIndex,NetworkCategory);"
    "$routes=@(Get-NetRoute -AddressFamily IPv4 "
    "-DestinationPrefix '0.0.0.0/0' | Select-Object InterfaceIndex);"
    "@{ips=$ips;adapters=$ads;profiles=$prof;routes=$routes} | "
    "ConvertTo-Json -Compress -Depth 3"
)


def _run_command(argv, timeout_s=_INVENTORY_COMMAND_TIMEOUT_S):
    """Run a read-only OS query; stdout text or None. A NUL stdin and no
    console window: the daemon runs headless under pythonw at logon."""
    kwargs = {"stdin": subprocess.DEVNULL, "stdout": subprocess.PIPE,
              "stderr": subprocess.DEVNULL, "timeout": timeout_s}
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    try:
        out = subprocess.run(argv, **kwargs)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if out.returncode != 0 or not out.stdout:
        return None
    try:
        return out.stdout.decode("utf-8", "replace")
    except Exception:
        return None


def classify_adapter(description, name="", iftype=None, virtual=False):
    """tunnel / virtual / physical for one adapter, from what the OS
    reports about it. Description markers win over ifType because
    TAP-Windows calls itself ethernet."""
    text = ("%s %s" % (description or "", name or "")).lower()
    if iftype in _LOOPBACK_IFTYPES:
        return ADAPTER_VIRTUAL
    if iftype in _TUNNEL_IFTYPES:
        return ADAPTER_TUNNEL
    for marker in _TUNNEL_MARKERS:
        if marker in text:
            return ADAPTER_TUNNEL
    if virtual:
        return ADAPTER_VIRTUAL
    for marker in _VIRTUAL_MARKERS:
        if marker in text:
            return ADAPTER_VIRTUAL
    return ADAPTER_PHYSICAL


def classify_posix_name(name):
    """tunnel / virtual / physical from a POSIX interface name."""
    low = (name or "").lower()
    for prefix in _TUNNEL_NAME_PREFIXES:
        if low.startswith(prefix):
            return ADAPTER_TUNNEL
    for prefix in _VIRTUAL_NAME_PREFIXES:
        if low.startswith(prefix):
            return ADAPTER_VIRTUAL
    return ADAPTER_PHYSICAL


def _adapter(name, description, kind, addresses, up, category=None,
             gateway=False):
    return {"name": str(name or "")[:128],
            "description": str(description or "")[:256],
            "kind": kind, "addresses": list(addresses),
            "up": bool(up), "category": category,
            "gateway": bool(gateway)}


def _as_int(value):
    try:
        if isinstance(value, bool):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _category_name(value):
    """NetworkCategory arrives as an int (Windows PowerShell 5 JSON) or a
    string (PowerShell 7 with -EnumsAsStrings)."""
    if isinstance(value, str):
        low = value.strip().lower()
        if low == "public":
            return CATEGORY_PUBLIC
        if low == "private":
            return CATEGORY_PRIVATE
        if low.startswith("domain"):
            return CATEGORY_DOMAIN
        return None
    number = _as_int(value)
    return {0: CATEGORY_PUBLIC, 1: CATEGORY_PRIVATE,
            2: CATEGORY_DOMAIN}.get(number)


def parse_windows_inventory(text):
    """The PowerShell JSON -> adapter list. Injectable for tests."""
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None

    def rows(key):
        value = data.get(key)
        if isinstance(value, dict):
            value = [value]
        return [row for row in (value or []) if isinstance(row, dict)]

    categories = {}
    for row in rows("profiles"):
        index = _as_int(row.get("InterfaceIndex"))
        if index is not None:
            categories[index] = _category_name(row.get("NetworkCategory"))
    gateways = {_as_int(row.get("InterfaceIndex")) for row in rows("routes")}
    addresses = {}
    for row in rows("ips"):
        index = _as_int(row.get("InterfaceIndex"))
        ip = str(row.get("IPAddress") or "").strip()
        if index is None or not ip:
            continue
        addresses.setdefault(index, []).append(ip)
    inventory = []
    seen = set()
    for row in rows("adapters"):
        index = _as_int(row.get("InterfaceIndex"))
        if index is None:
            continue
        seen.add(index)
        description = str(row.get("InterfaceDescription") or "")
        name = str(row.get("Name") or "")
        kind = classify_adapter(
            description, name, iftype=_as_int(row.get("InterfaceType")),
            virtual=bool(row.get("Virtual")))
        inventory.append(_adapter(
            name, description, kind, addresses.get(index, ()),
            up=str(row.get("Status") or "").strip().lower() == "up",
            category=categories.get(index), gateway=index in gateways))
    for index, ips in addresses.items():
        if index in seen:
            continue
        # An address whose adapter Get-NetAdapter did not list (a
        # hidden pseudo-interface): keep it addressable, classify by
        # alias only.
        alias = ""
        for row in rows("ips"):
            if _as_int(row.get("InterfaceIndex")) == index:
                alias = str(row.get("InterfaceAlias") or "")
                break
        inventory.append(_adapter(
            alias, alias, classify_adapter(alias, alias), ips, up=True,
            category=categories.get(index), gateway=index in gateways))
    return inventory


def _windows_inventory(runner):
    text = runner(["powershell.exe", "-NoProfile", "-NonInteractive",
                   "-ExecutionPolicy", "Bypass", "-Command",
                   _POWERSHELL_INVENTORY])
    return parse_windows_inventory(text) if text else None


_IFCONFIG_HEAD = re.compile(r"^([A-Za-z0-9_.-]+):\s+flags=\d+<([^>]*)>")
_IFCONFIG_INET = re.compile(r"^\s+inet\s+(\d+\.\d+\.\d+\.\d+)")


def parse_ifconfig_inventory(text, default_iface=None):
    """macOS/BSD `ifconfig -a` -> adapter list."""
    if not text:
        return None
    inventory = []
    current = None
    for line in text.splitlines():
        head = _IFCONFIG_HEAD.match(line)
        if head:
            name, flags = head.group(1), head.group(2).split(",")
            current = _adapter(name, name, classify_posix_name(name), [],
                               up="UP" in flags,
                               gateway=(name == default_iface))
            inventory.append(current)
            continue
        if current is None:
            continue
        inet = _IFCONFIG_INET.match(line)
        if inet:
            current["addresses"].append(inet.group(1))
    return inventory


def _darwin_inventory(runner):
    default_iface = None
    route = runner(["route", "-n", "get", "default"])
    for line in (route or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("interface:"):
            default_iface = stripped.split(":", 1)[1].strip()
    return parse_ifconfig_inventory(runner(["ifconfig", "-a"]),
                                    default_iface=default_iface)


_IP_ADDR_LINE = re.compile(
    r"^\d+:\s+([A-Za-z0-9_.@-]+)\s+inet\s+(\d+\.\d+\.\d+\.\d+)/")


def parse_ip_inventory(text, default_iface=None, sysfs=None):
    """Linux `ip -o -4 addr show` -> adapter list. `sysfs` maps an
    interface name to {"tun": bool, "type": int, "operstate": str}."""
    if not text:
        return None
    sysfs = sysfs or {}
    by_name = {}
    for line in text.splitlines():
        found = _IP_ADDR_LINE.match(line)
        if not found:
            continue
        name = found.group(1).split("@", 1)[0]
        info = sysfs.get(name, {})
        kind = classify_posix_name(name)
        if info.get("tun") or info.get("type") == 65534:
            kind = ADAPTER_TUNNEL
        entry = by_name.get(name)
        if entry is None:
            entry = _adapter(name, name, kind, [],
                             up=str(info.get("operstate", "up")) == "up",
                             gateway=(name == default_iface))
            by_name[name] = entry
        entry["addresses"].append(found.group(2))
    return list(by_name.values())


def _linux_sysfs(name):
    base = os.path.join("/sys/class/net", name)
    info = {"tun": os.path.exists(os.path.join(base, "tun_flags"))}
    for key in ("type", "operstate"):
        try:
            with open(os.path.join(base, key), "r", encoding="utf-8") as f:
                value = f.read().strip()
        except OSError:
            continue
        info[key] = _as_int(value) if key == "type" else value
    return info


def _linux_inventory(runner):
    text = runner(["ip", "-o", "-4", "addr", "show"])
    if not text:
        return None
    default_iface = None
    route = runner(["ip", "-o", "-4", "route", "show", "default"]) or ""
    parts = route.split()
    if "dev" in parts:
        default_iface = parts[parts.index("dev") + 1]
    names = {found.group(1).split("@", 1)[0]
             for found in (_IP_ADDR_LINE.match(line)
                           for line in text.splitlines()) if found}
    return parse_ip_inventory(
        text, default_iface=default_iface,
        sysfs={name: _linux_sysfs(name) for name in names})


def _adapter_inventory(platform=None, runner=None):
    """Every IPv4-capable adapter the OS reports, classified, or None
    when the OS could not be asked (then choose_bind classifies nothing).
    Best-effort by construction: never raises. The public name below is
    what InventoryCache binds and what the test suites patch to keep a
    HostApp test off the real OS; this is the unpatched implementation."""
    platform = platform or sys.platform
    runner = runner or _run_command
    try:
        if platform == "win32":
            return _windows_inventory(runner)
        if platform == "darwin":
            return _darwin_inventory(runner)
        return _linux_inventory(runner)
    except Exception:
        return None


adapter_inventory = _adapter_inventory


class InventoryCache:
    """adapter_inventory, remembered for INVENTORY_TTL_S: the lifecycle
    loop re-resolves the bind every few seconds, and a PowerShell spawn
    is not a thing to do every few seconds."""

    def __init__(self, ttl_s=INVENTORY_TTL_S, loader=None, now=None):
        self._ttl_s = float(ttl_s)
        self._loader = loader or adapter_inventory
        self._now = now or time.monotonic
        self._built_at = None
        self._inventory = None

    def get(self, refresh=False):
        now = self._now()
        if (refresh or self._built_at is None
                or (now - self._built_at) >= self._ttl_s):
            self._inventory = self._loader()
            self._built_at = now
        return self._inventory

    def invalidate(self):
        self._built_at = None


def adapter_for(address, inventory):
    for adapter in inventory or ():
        if address in (adapter.get("addresses") or ()):
            return adapter
    return None


def _usable_address(value):
    try:
        addr = ipaddress.ip_address(value)
    except ValueError:
        return False
    return (addr.version == 4 and not addr.is_loopback
            and not addr.is_link_local and not addr.is_unspecified)


def _adapter_public(adapter):
    if not adapter:
        return None
    return {"name": adapter.get("name"),
            "description": adapter.get("description"),
            "kind": adapter.get("kind"),
            "category": adapter.get("category"),
            "gateway": bool(adapter.get("gateway"))}


def _warning_for(adapter):
    if not adapter:
        return None
    if adapter.get("kind") == ADAPTER_TUNNEL:
        return "tunnel_adapter"
    if adapter.get("kind") == ADAPTER_VIRTUAL:
        return "virtual_adapter"
    if adapter.get("category") == CATEGORY_PUBLIC:
        return "public_network"
    return None


def describe_bind(address, inventory, explicit=False, probe_address=None,
                  skipped=None):
    """The posture of a bind: which adapter owns it and whether that is
    something an operator should know about."""
    adapter = adapter_for(address, inventory)
    return {"address": address,
            "adapter": _adapter_public(adapter),
            "warning": _warning_for(adapter),
            "explicit": bool(explicit),
            "probe_address": probe_address or address,
            "skipped": _adapter_public(skipped),
            "inventory": ("unavailable" if inventory is None
                          else "checked")}


def choose_bind(probe_address, inventory):
    """Turn the route probe's answer into the address to auto-bind.

    The probe names the adapter that owns the default route. That is
    the LAN on a plain machine and a tunnel the moment a VPN pushes
    routes. So: keep the probe's adapter when it is physical and not on
    a Public network; otherwise prefer a physical, up adapter holding a
    usable IPv4 and not on a Public network (one that holds a default
    route first); and when nothing qualifies, REFUSE by name rather
    than expose a tunnel or a public network -- the operator's explicit
    `bind` in lan.json is the deliberate override for both.
    """
    if not inventory:
        return describe_bind(probe_address, inventory)
    owner = adapter_for(probe_address, inventory)
    if owner is None or _warning_for(owner) is None:
        return describe_bind(probe_address, inventory)
    candidates = []
    for adapter in inventory:
        if adapter is owner or adapter.get("kind") != ADAPTER_PHYSICAL:
            continue
        if not adapter.get("up") or adapter.get("category") == \
                CATEGORY_PUBLIC:
            continue
        for value in adapter.get("addresses") or ():
            if _usable_address(value):
                candidates.append((0 if adapter.get("gateway") else 1,
                                   value, adapter))
                break
    if candidates:
        candidates.sort(key=lambda item: item[0])
        _rank, value, adapter = candidates[0]
        return describe_bind(value, inventory, probe_address=probe_address,
                             skipped=owner)
    label = "%s (%s)" % (owner.get("description") or owner.get("name")
                         or "unknown adapter", probe_address)
    if owner.get("kind") in (ADAPTER_TUNNEL, ADAPTER_VIRTUAL):
        raise LanConfigError(
            "lan_tunnel_only",
            f"the only routable IPv4 interface is a {owner.get('kind')} "
            f"adapter, {label}; Convoy never auto-binds a tunnel or "
            f"virtual adapter. Set 'bind' in lan.json to use it "
            f"deliberately, or connect the LAN")
    raise LanConfigError(
        "lan_public_network",
        f"the network on {label} is classed Public by Windows; Convoy "
        f"never auto-binds a public network. Set the network profile to "
        f"Private (Settings > Network), or set 'bind' in lan.json to "
        f"bind it deliberately")
