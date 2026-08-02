"""LAN transport configuration for the embody-convoy host app (Phase 3
slice 3). Pure stdlib, no TD, no `cryptography`, no socket bind of its
own -- it decides WHETHER and WHERE to bind, and convoy_peerserver does
the binding.

TWO facts live here, and one non-negotiable default:

  - lan.json: the host-private switch that turns the LAN listener on.
    ABSENT lan.json means NO LAN SOCKET, EVER -- not a random fallback,
    not a default port quietly opened. A daemon with no lan.json behaves
    exactly as slice 2 did: loopback only, off-box unreachable. The file
    is never CREATED by this module or the daemon; an operator (slice 4's
    consent flow, or a developer for the two-machine smoke) writes it
    deliberately. That is what makes "off by default" structural rather
    than a flag someone has to remember to leave alone.

  - the BIND ADDRESS: never 0.0.0.0 by default (24.3). The primary
    outbound IPv4 is found with the stdlib no-packet trick -- a UDP
    socket `connect`ed to a documentation-range address reveals the
    interface the OS would route through, WITHOUT a single packet
    leaving the machine (a datagram connect only sets the socket's
    default peer). VPN/virtual/loopback/link-local are excluded; IPv6
    and link-local are refused with a NAMED reason (plan section 6), not
    silently coerced.

  - the PORT: a FIXED default (47600). Across machines there is no
    portfile a peer could read, so a peer must be able to NAME the port
    -- which means it cannot be OS-assigned. A taken port REFUSES the
    LAN listener with a named message and leaves loopback serving; it
    NEVER falls back to a random port no peer could find.

Every platform/network branch is INJECTABLE (D-5): the socket factory,
the interface address, and the environment are all parameters, so the
foreign-network paths are exercised on any machine rather than by owning
the hardware.
"""

import ipaddress
import json
import os
import socket


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


def resolve_bind(config, socket_factory=None):
    """The concrete IPv4 address the LAN listener should bind, or raise
    LanConfigError naming why it cannot. Never returns 0.0.0.0.

    An explicit `bind` literal was already validated at load; here it is
    only returned. "auto" runs primary_ipv4 and refuses -- named -- when
    no routable IPv4 exists, rather than falling back to a wildcard or
    loopback bind that would either expose everything or nothing.
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
        return config.bind
    address = primary_ipv4(socket_factory=socket_factory)
    if address is None:
        raise LanConfigError(
            "lan_no_route",
            "could not determine a routable IPv4 interface to bind "
            "(only loopback/link-local found). Set an explicit 'bind' "
            "in lan.json to name the interface")
    return address
