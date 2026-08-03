"""Persistent Convoy host-pair WebSocket session management.

``convoy_ws`` deliberately stops at one authenticated WebSocket connection.
This module owns the next layer up: at most one live, full-duplex connection
for each trusted host pair, endpoint-aware reconnect, duplicate-dial
convergence, namespace authorization, and bounded lifecycle management.

The TLS and trust boundary stays outside this module.  ``dial_authenticated``
must return a connected socket only after authenticating it as the requested
peer host, and :meth:`HostPairSessionManager.accept_authenticated_socket` must
only be called with a peer id established by the server's TLS/pinning layer.
The peer-provided hello is checked against that identity; it can never replace
it.

The manager uses monotonic durations exclusively.  It never retries an RPC:
after a disconnect a mutation may already have reached the peer, so retry and
job reconciliation policy belongs to the durable operation layer above it.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import queue
import secrets
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Sequence, Tuple

import convoy_ws as ws


DEFAULT_MAX_PEERS = 64
DEFAULT_DIAL_WORKERS = 4
DEFAULT_EVENT_QUEUE = 256
DEFAULT_CONNECT_TIMEOUT_S = 5.0
DEFAULT_SUPERVISOR_INTERVAL_S = 0.1


class PairSessionError(Exception):
    """Base class for named host-pair manager failures."""

    code = "pair_session_error"

    def __init__(self, detail: str = "") -> None:
        self.detail = str(detail or "")
        super().__init__(
            f"{self.code}: {self.detail}" if self.detail else self.code)


class ManagerStopped(PairSessionError):
    code = "manager_stopped"


class PeerUnknown(PairSessionError):
    code = "peer_unknown"


class PeerRevoked(PairSessionError):
    code = "peer_revoked"


class PeerUnavailable(PairSessionError):
    code = "peer_unavailable"


class PeerCapacityReached(PairSessionError):
    code = "peer_capacity_reached"


class NamespaceNotAuthorized(PairSessionError):
    code = "namespace_not_authorized"


def _positive_number(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a finite positive number")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a finite positive number") from exc
    if not (result > 0.0) or result == float("inf"):
        raise ValueError(f"{label} must be a finite positive number")
    return result


def _bounded_int(value: Any, label: str, minimum: int, maximum: int) -> int:
    if (isinstance(value, bool) or not isinstance(value, int)
            or value < minimum or value > maximum):
        raise ValueError(f"{label} must be an integer in [{minimum}, {maximum}]")
    return value


def _identifier(value: Any, label: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ValueError(f"{label} must be non-empty text up to {maximum} characters")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise ValueError(f"{label} contains control characters")
    return value


def _remaining(deadline: float, monotonic: Callable[[], float]) -> float:
    return max(0.0, deadline - monotonic())


@dataclass(frozen=True)
class PeerEndpoint:
    """A discovery/manual endpoint usable by the authenticated TLS dialer."""

    address: str
    port: int
    server_name: Optional[str] = None

    def __post_init__(self) -> None:
        _identifier(self.address, "endpoint address", 512)
        _bounded_int(self.port, "endpoint port", 1, 65535)
        if self.server_name is not None:
            _identifier(self.server_name, "endpoint server_name", 512)

    @property
    def host_header(self) -> str:
        host = self.server_name or self.address
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        return f"{host}:{self.port}"


@dataclass(frozen=True)
class HelloProfile:
    """Dynamic local information included in each authenticated hello."""

    namespaces: Tuple[str, ...]
    capability_summary: Mapping[str, Any]

    def __init__(self, namespaces: Iterable[str],
                 capability_summary: Mapping[str, Any]) -> None:
        normalized = []
        seen = set()
        for namespace in namespaces:
            namespace = _identifier(namespace, "namespace", 128)
            if namespace in seen:
                raise ValueError("hello namespaces must be unique")
            seen.add(namespace)
            normalized.append(namespace)
        if len(normalized) > 128:
            raise ValueError("hello namespaces must contain at most 128 entries")
        if not isinstance(capability_summary, Mapping):
            raise ValueError("capability_summary must be a mapping")
        # Round-trip now so mutable caller data cannot change an in-flight
        # hello and non-JSON/non-finite values fail before touching a socket.
        try:
            encoded = json.dumps(
                dict(capability_summary), ensure_ascii=False,
                allow_nan=False, separators=(",", ":"))
            copied = json.loads(encoded)
        except (TypeError, ValueError) as exc:
            raise ValueError("capability_summary must be strict JSON") from exc
        if not isinstance(copied, dict) or len(copied) > 256:
            raise ValueError(
                "capability_summary must contain at most 256 top-level fields")
        object.__setattr__(self, "namespaces", tuple(normalized))
        object.__setattr__(self, "capability_summary", copied)


@dataclass(frozen=True)
class ReconnectPolicy:
    """Bounded monotonic reconnect/backoff policy."""

    initial_delay_s: float = 0.1
    maximum_delay_s: float = 15.0
    jitter_ratio: float = 0.2
    connect_timeout_s: float = DEFAULT_CONNECT_TIMEOUT_S

    def __post_init__(self) -> None:
        initial = _positive_number(self.initial_delay_s, "initial_delay_s")
        maximum = _positive_number(self.maximum_delay_s, "maximum_delay_s")
        timeout = _positive_number(self.connect_timeout_s, "connect_timeout_s")
        if maximum < initial:
            raise ValueError("maximum_delay_s must be >= initial_delay_s")
        if (isinstance(self.jitter_ratio, bool)
                or not isinstance(self.jitter_ratio, (int, float))
                or not 0.0 <= float(self.jitter_ratio) <= 1.0):
            raise ValueError("jitter_ratio must be between 0 and 1")
        object.__setattr__(self, "initial_delay_s", initial)
        object.__setattr__(self, "maximum_delay_s", maximum)
        object.__setattr__(self, "jitter_ratio", float(self.jitter_ratio))
        object.__setattr__(self, "connect_timeout_s", timeout)


@dataclass(frozen=True)
class SessionEvent:
    sequence: int
    kind: str
    peer_host_id: str
    connection_id: Optional[str] = None
    detail: str = ""


@dataclass(frozen=True)
class AcceptedSession:
    """Result of an inbound/outbound handshake and deduplication decision.

    A ``BaseHTTPRequestHandler`` integration should call ``wait_closed()``
    before returning from its hijacked Upgrade route.  That keeps
    ``socketserver`` from closing a socket now owned by the WebSocket session.
    """

    selected: bool
    peer_host_id: str
    connection_id: str
    role: str
    authorized_namespaces: Tuple[str, ...]
    remote_generation: int
    _closed: threading.Event = field(repr=False, compare=False)

    @property
    def is_closed(self) -> bool:
        return self._closed.is_set()

    def wait_closed(self, timeout_s: Optional[float] = None) -> bool:
        if timeout_s is None:
            self._closed.wait()
            return True
        return self._closed.wait(_positive_number(timeout_s, "timeout_s"))


@dataclass(frozen=True)
class PeerSessionInfo:
    peer_host_id: str
    state: str
    endpoints: Tuple[PeerEndpoint, ...]
    connection_id: Optional[str]
    connection_role: Optional[str]
    active_endpoint: Optional[PeerEndpoint]
    authorized_namespaces: Tuple[str, ...]
    remote_protocol: Optional[str]
    remote_generation: Optional[int]
    remote_capability_summary: Mapping[str, Any]
    failure_count: int
    retry_in_s: Optional[float]
    last_error: str


@dataclass
class _Candidate:
    peer_host_id: str
    session: ws.Session
    role: str
    endpoint: Optional[PeerEndpoint]
    local_generation: int
    remote_hello: ws.PeerHello
    local_nonce: str
    authorized_namespaces: Tuple[str, ...]
    connection_id: str
    rank: Tuple[Any, ...]
    hello_epoch: int
    config_revision: Optional[int]
    authentication_context: Any = None
    ready: threading.Event = field(default_factory=threading.Event)
    closed: threading.Event = field(default_factory=threading.Event)
    admitted_handlers: int = 0


@dataclass
class _PeerRecord:
    peer_host_id: str
    endpoints: Tuple[PeerEndpoint, ...]
    authentication_context: Any = None
    revision: int = 1
    active: Optional[_Candidate] = None
    attempt_inflight: bool = False
    attempt_queued: bool = False
    endpoint_cursor: int = 0
    failures: int = 0
    next_attempt: float = 0.0
    last_error: str = ""


DialAuthenticated = Callable[[str, PeerEndpoint, float], socket.socket]
RpcHandler = Callable[[str, str, str, Any, Any], Any]
HelloProvider = Callable[[], HelloProfile]


class HostPairSessionManager:
    """Own one converged, reconnecting full-duplex session per trusted host.

    ``configure_peer`` is an authorization action, not raw discovery input.
    Unknown and revoked identities are refused before the WebSocket hello.
    Call :meth:`revoke_peer` when a trust pin is revoked and
    :meth:`restore_peer` only after an explicit new trust decision.
    """

    def __init__(
            self, local_host_id: str,
            dial_authenticated: DialAuthenticated,
            hello_provider: HelloProvider,
            handler: Optional[RpcHandler] = None, *,
            reconnect_policy: ReconnectPolicy = ReconnectPolicy(),
            max_peers: int = DEFAULT_MAX_PEERS,
            dial_workers: int = DEFAULT_DIAL_WORKERS,
            max_events: int = DEFAULT_EVENT_QUEUE,
            supervisor_interval_s: float = DEFAULT_SUPERVISOR_INTERVAL_S,
            websocket_path: str = ws.DEFAULT_PATH,
            session_options: Optional[Mapping[str, Any]] = None,
            monotonic: Callable[[], float] = time.monotonic,
            random_value: Callable[[], float] = secrets.SystemRandom().random,
            name: Optional[str] = None) -> None:
        self.local_host_id = _identifier(local_host_id, "local_host_id")
        if not callable(dial_authenticated):
            raise ValueError("dial_authenticated must be callable")
        if not callable(hello_provider):
            raise ValueError("hello_provider must be callable")
        if handler is not None and not callable(handler):
            raise ValueError("handler must be callable")
        self.max_peers = _bounded_int(max_peers, "max_peers", 1, 4096)
        self.dial_workers = _bounded_int(
            dial_workers, "dial_workers", 1, min(64, self.max_peers))
        max_events = _bounded_int(max_events, "max_events", 1, 65536)
        self.supervisor_interval_s = _positive_number(
            supervisor_interval_s, "supervisor_interval_s")
        self.reconnect_policy = reconnect_policy
        if not isinstance(reconnect_policy, ReconnectPolicy):
            raise ValueError("reconnect_policy must be a ReconnectPolicy")
        self._dial_authenticated = dial_authenticated
        self._hello_provider = hello_provider
        self._handler = handler
        self._session_options = dict(session_options or {})
        # The manager supplies this so all lifecycle durations use the same
        # monotonic domain.  A second value would make deadline comparisons
        # internally inconsistent.
        reserved_options = set(self._session_options).difference({
            "max_pending", "max_inbound_queue", "handler_workers",
            "ping_interval_s", "idle_timeout_s",
        })
        if reserved_options:
            raise ValueError(
                "unsupported session_options: "
                + ", ".join(sorted(str(item) for item in reserved_options)))
        if (not isinstance(websocket_path, str)
                or not websocket_path.startswith("/")
                or any(ord(ch) <= 0x20 or ord(ch) >= 0x7F
                       for ch in websocket_path)
                or "#" in websocket_path):
            raise ValueError(
                "websocket_path must be an absolute ASCII HTTP request target")
        self.websocket_path = websocket_path
        self._monotonic = monotonic
        self._random_value = random_value
        self.name = name or f"convoy-pairs-{self.local_host_id}"

        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._started = False
        self._peers: Dict[str, _PeerRecord] = {}
        self._revoked = set()
        self._generation = 0
        self._hello_epoch = 0
        self._supervisor: Optional[threading.Thread] = None
        self._workers: list[threading.Thread] = []
        self._dial_queue: queue.Queue = queue.Queue(self.max_peers)
        self._handshake_slots = threading.BoundedSemaphore(self.max_peers)
        self._handshake_sockets: Dict[socket.socket, str] = {}
        self._events: queue.Queue = queue.Queue(max_events)
        self._event_sequence = 0
        self._events_dropped = 0

    def start(self) -> "HostPairSessionManager":
        with self._condition:
            if self._stop.is_set():
                raise ManagerStopped("a stopped manager cannot be restarted")
            if self._started:
                return self
            # Validate local hello data before exposing any worker.
            self._profile_snapshot()
            self._started = True
            for index in range(self.dial_workers):
                thread = threading.Thread(
                    target=self._dial_worker,
                    name=f"{self.name}-dial-{index}", daemon=True)
                self._workers.append(thread)
                thread.start()
            self._supervisor = threading.Thread(
                target=self._supervisor_loop,
                name=f"{self.name}-supervisor", daemon=True)
            self._supervisor.start()
            self._condition.notify_all()
        self._wake.set()
        return self

    @property
    def is_stopped(self) -> bool:
        return self._stop.is_set()

    def configure_peer(self, peer_host_id: str,
                       endpoints: Sequence[PeerEndpoint], *,
                       authentication_context: Any = None) -> None:
        """Authorize/configure a trusted peer without clearing revocation."""
        peer_host_id = self._validate_peer_id(peer_host_id)
        normalized = self._normalize_endpoints(endpoints)
        authentication_context = copy.deepcopy(authentication_context)
        close_candidate = None
        with self._condition:
            self._ensure_not_stopped_locked()
            if peer_host_id in self._revoked:
                raise PeerRevoked(peer_host_id)
            record = self._peers.get(peer_host_id)
            if record is None:
                if len(self._peers) >= self.max_peers:
                    raise PeerCapacityReached(
                        f"configured peer limit {self.max_peers} reached")
                record = _PeerRecord(
                    peer_host_id, normalized, authentication_context)
                self._peers[peer_host_id] = record
                self._emit_locked("peer_configured", peer_host_id)
            elif (record.endpoints != normalized
                  or record.authentication_context != authentication_context):
                record.endpoints = normalized
                record.authentication_context = authentication_context
                record.revision += 1
                record.endpoint_cursor = 0
                record.failures = 0
                record.next_attempt = self._monotonic()
                record.last_error = ""
                close_candidate = record.active
                record.active = None
                self._emit_locked(
                    "endpoints_changed", peer_host_id,
                    close_candidate.connection_id if close_candidate else None)
            self._condition.notify_all()
        if close_candidate is not None:
            self._close_candidate(
                close_candidate, 0.25, "peer endpoints changed")
        self._wake.set()

    def remove_peer(self, peer_host_id: str) -> None:
        """Remove configuration and close the link without recording a ban."""
        peer_host_id = self._validate_peer_id(peer_host_id)
        candidate = None
        sockets = []
        with self._condition:
            record = self._peers.pop(peer_host_id, None)
            if record is None:
                return
            candidate = record.active
            sockets = self._take_handshake_sockets_locked(peer_host_id)
            self._emit_locked(
                "peer_removed", peer_host_id,
                candidate.connection_id if candidate else None)
            self._condition.notify_all()
        self._close_sockets(sockets)
        if candidate is not None:
            self._close_candidate(candidate, 0.25, "peer removed")
        self._wake.set()

    def revoke_peer(self, peer_host_id: str) -> None:
        """Fail closed immediately and remember the local trust revocation."""
        peer_host_id = self._validate_peer_id(peer_host_id)
        candidate = None
        sockets = []
        with self._condition:
            self._revoked.add(peer_host_id)
            record = self._peers.pop(peer_host_id, None)
            candidate = record.active if record else None
            sockets = self._take_handshake_sockets_locked(peer_host_id)
            self._emit_locked(
                "peer_revoked", peer_host_id,
                candidate.connection_id if candidate else None)
            self._condition.notify_all()
        self._close_sockets(sockets)
        if candidate is not None:
            self._close_candidate(candidate, 0.25, "peer revoked")
        self._wake.set()

    def restore_peer(self, peer_host_id: str,
                     endpoints: Sequence[PeerEndpoint], *,
                     authentication_context: Any = None) -> None:
        """Explicitly clear a local revocation after trust is re-established."""
        peer_host_id = self._validate_peer_id(peer_host_id)
        normalized = self._normalize_endpoints(endpoints)
        authentication_context = copy.deepcopy(authentication_context)
        with self._condition:
            self._ensure_not_stopped_locked()
            if (peer_host_id not in self._peers
                    and len(self._peers) >= self.max_peers):
                raise PeerCapacityReached(
                    f"configured peer limit {self.max_peers} reached")
            self._revoked.discard(peer_host_id)
            self._peers[peer_host_id] = _PeerRecord(
                peer_host_id, normalized, authentication_context,
                next_attempt=self._monotonic())
            self._emit_locked("peer_restored", peer_host_id)
            self._condition.notify_all()
        self._wake.set()

    def refresh_hello(self) -> None:
        """Re-handshake every pair after local namespaces/capabilities change."""
        # Validate the new snapshot before disturbing healthy sessions.
        self._profile_snapshot()
        candidates = []
        with self._condition:
            self._ensure_not_stopped_locked()
            self._hello_epoch += 1
            now = self._monotonic()
            for record in self._peers.values():
                if record.active is not None:
                    candidates.append(record.active)
                    record.active = None
                record.failures = 0
                record.next_attempt = now
            self._emit_locked("hello_refreshed", self.local_host_id)
            self._condition.notify_all()
        for candidate in candidates:
            self._close_candidate(candidate, 0.25, "local hello changed")
        self._wake.set()

    def disconnect_peer(self, peer_host_id: str,
                        reason: str = "link reset") -> bool:
        """Drop one healthy link and leave it eligible for automatic redial."""
        peer_host_id = self._validate_peer_id(peer_host_id)
        candidate = None
        with self._condition:
            record = self._record_locked(peer_host_id)
            candidate = record.active
            if candidate is None:
                return False
            record.active = None
            record.failures = 0
            record.next_attempt = self._monotonic()
            self._emit_locked(
                "disconnected", peer_host_id, candidate.connection_id, reason)
            self._condition.notify_all()
        self._close_candidate(candidate, 0.25, reason[:123])
        self._wake.set()
        return True

    def accept_authenticated_socket(
            self, sock: socket.socket, peer_host_id: str, *,
            expected_host: Optional[str] = None,
            timeout_s: Optional[float] = None,
            authentication_context: Any = None) -> AcceptedSession:
        """Accept one TLS-authenticated inbound peer socket synchronously.

        The caller may invoke this from its bounded server connection worker.
        The manager additionally caps concurrent handshakes at ``max_peers``;
        excess connections fail closed without queueing unbounded work.
        """
        if not isinstance(sock, socket.socket):
            raise ValueError("sock must be a connected socket")
        try:
            peer_host_id = self._validate_peer_id(peer_host_id)
            timeout = (self.reconnect_policy.connect_timeout_s
                       if timeout_s is None else _positive_number(
                           timeout_s, "timeout_s"))
        except BaseException:
            self._close_sockets([sock])
            raise
        if not self._handshake_slots.acquire(blocking=False):
            self._close_sockets([sock])
            raise ws.SessionBusy("inbound handshake capacity reached")
        tracked = False
        try:
            with self._condition:
                self._ensure_accept_allowed_locked(
                    peer_host_id, authentication_context)
                self._handshake_sockets[sock] = peer_host_id
                tracked = True
            return self._establish(
                sock, peer_host_id, role="server", endpoint=None,
                expected_host=expected_host, timeout_s=timeout,
                config_revision=None,
                authentication_context=authentication_context)
        except BaseException:
            self._close_sockets([sock])
            raise
        finally:
            if tracked:
                with self._condition:
                    self._handshake_sockets.pop(sock, None)
            self._handshake_slots.release()

    def accept_authenticated_websocket(
            self, connection: ws.WebSocketConnection, peer_host_id: str, *,
            timeout_s: Optional[float] = None,
            authentication_context: Any = None) -> AcceptedSession:
        """Accept an already-upgraded authenticated server connection.

        This is the integration point for ``BaseHTTPRequestHandler``-style
        listeners, which have already consumed and strictly validated the
        Upgrade request before dispatching a route.  The caller must send the
        101 response and pass a ``server``-role :class:`WebSocketConnection`;
        this method still performs the identity-bound Convoy hello.
        """
        if (not isinstance(connection, ws.WebSocketConnection)
                or connection.role != "server"):
            raise ValueError(
                "connection must be a server-role WebSocketConnection")
        sock = connection.sock
        try:
            peer_host_id = self._validate_peer_id(peer_host_id)
            timeout = (self.reconnect_policy.connect_timeout_s
                       if timeout_s is None else _positive_number(
                           timeout_s, "timeout_s"))
        except BaseException:
            self._close_sockets([sock])
            raise
        if not self._handshake_slots.acquire(blocking=False):
            self._close_sockets([sock])
            raise ws.SessionBusy("inbound handshake capacity reached")
        tracked = False
        try:
            with self._condition:
                self._ensure_accept_allowed_locked(
                    peer_host_id, authentication_context)
                self._handshake_sockets[sock] = peer_host_id
                tracked = True
            return self._negotiate(
                connection, peer_host_id, endpoint=None,
                timeout_s=timeout, config_revision=None,
                authentication_context=authentication_context)
        except BaseException:
            self._close_sockets([sock])
            raise
        finally:
            if tracked:
                with self._condition:
                    self._handshake_sockets.pop(sock, None)
            self._handshake_slots.release()

    def wait_connected(self, peer_host_id: str, timeout_s: float) -> bool:
        peer_host_id = self._validate_peer_id(peer_host_id)
        timeout = _positive_number(timeout_s, "timeout_s")
        deadline = self._monotonic() + timeout
        with self._condition:
            while True:
                record = self._record_locked(peer_host_id)
                candidate = record.active
                if (candidate is not None and candidate.ready.is_set()
                        and not candidate.session.is_closed):
                    return True
                remaining = _remaining(deadline, self._monotonic)
                if remaining <= 0.0 or self._stop.is_set():
                    return False
                self._condition.wait(remaining)

    def call(self, peer_host_id: str, convoy_id: str, method: str,
             params: Any = None, *, timeout_s: float = 30.0) -> Any:
        """Make exactly one namespace-bound RPC using one cumulative deadline."""
        peer_host_id = self._validate_peer_id(peer_host_id)
        convoy_id = _identifier(convoy_id, "convoy_id", 128)
        method = _identifier(method, "method", 256)
        timeout = _positive_number(timeout_s, "timeout_s")
        deadline = self._monotonic() + timeout
        candidate = self._connected_candidate(peer_host_id, deadline)
        if convoy_id not in candidate.authorized_namespaces:
            raise NamespaceNotAuthorized(
                f"{convoy_id} is not authorized for {peer_host_id}")
        remaining = _remaining(deadline, self._monotonic)
        if remaining <= 0.0:
            raise PeerUnavailable(
                f"connection budget expired for {peer_host_id}")
        # Never retry here.  A transport exception may be an ambiguous write.
        try:
            return candidate.session.call(
                method,
                {"convoy_id": convoy_id, "payload": params},
                timeout_s=remaining)
        finally:
            if candidate.session.is_closed:
                self._wake.set()

    def peer_info(self, peer_host_id: str) -> PeerSessionInfo:
        peer_host_id = self._validate_peer_id(peer_host_id)
        with self._condition:
            if peer_host_id in self._revoked:
                return PeerSessionInfo(
                    peer_host_id, "revoked", (), None, None, None, (), None,
                    None, {}, 0, None, "peer trust is revoked")
            record = self._record_locked(peer_host_id)
            return self._peer_info_locked(record)

    def connected_with_authentication_context(
            self, peer_host_id: str, authentication_context: Any) -> bool:
        """Whether the live candidate is bound to this exact current pin.

        HostApp uses this immediately before selecting WSS.  It closes the
        small admission/re-pin race in which the trust store has changed but
        its asynchronous session reconciliation has not yet removed the old
        authenticated socket.
        """
        peer_host_id = self._validate_peer_id(peer_host_id)
        expected = copy.deepcopy(authentication_context)
        with self._condition:
            if peer_host_id in self._revoked:
                return False
            record = self._peers.get(peer_host_id)
            candidate = record.active if record is not None else None
            return bool(
                record is not None
                and record.authentication_context == expected
                and candidate is not None
                and candidate.authentication_context == expected
                and candidate.ready.is_set()
                and not candidate.session.is_closed)

    def snapshot(self) -> Tuple[PeerSessionInfo, ...]:
        with self._condition:
            result = [self._peer_info_locked(record)
                      for _, record in sorted(self._peers.items())]
            for peer_host_id in sorted(self._revoked.difference(self._peers)):
                result.append(PeerSessionInfo(
                    peer_host_id, "revoked", (), None, None, None, (), None,
                    None, {}, 0, None, "peer trust is revoked"))
            return tuple(result)

    def poll_events(self, limit: int = 100) -> Tuple[SessionEvent, ...]:
        limit = _bounded_int(limit, "limit", 1, 10000)
        result = []
        for _ in range(limit):
            try:
                result.append(self._events.get_nowait())
            except queue.Empty:
                break
        return tuple(result)

    def stats(self) -> Mapping[str, int]:
        with self._condition:
            connected = sum(
                1 for record in self._peers.values()
                if record.active is not None
                and record.active.ready.is_set()
                and not record.active.session.is_closed)
            return {
                "configured_peers": len(self._peers),
                "revoked_peers": len(self._revoked),
                "connected_peers": connected,
                "dial_queue": self._dial_queue.qsize(),
                "handshakes": len(self._handshake_sockets),
                "events_queued": self._events.qsize(),
                "events_dropped": self._events_dropped,
            }

    def stop(self, timeout_s: float = 2.0) -> None:
        """Close all exposure and return within one cumulative deadline."""
        timeout = _positive_number(timeout_s, "timeout_s")
        deadline = self._monotonic() + timeout
        with self._condition:
            if self._stop.is_set():
                return
            self._stop.set()
            self._wake.set()
            candidates = []
            for record in self._peers.values():
                if record.active is not None:
                    candidates.append(record.active)
                    record.active = None
            sockets = list(self._handshake_sockets)
            self._handshake_sockets.clear()
            self._emit_locked("manager_stopped", self.local_host_id)
            self._condition.notify_all()
        self._close_sockets(sockets)
        # Initiate every close concurrently.  Sequential close handshakes can
        # otherwise let one unresponsive peer consume the whole deadline and
        # leave later sockets exposed.
        close_threads = []
        for index, candidate in enumerate(candidates):
            thread = threading.Thread(
                target=self._close_candidate,
                args=(candidate, timeout, "manager stopped"),
                name=f"{self.name}-close-{index}", daemon=True)
            close_threads.append(thread)
            thread.start()
        for thread in close_threads:
            remaining = _remaining(deadline, self._monotonic)
            if remaining <= 0.0:
                break
            thread.join(remaining)
        for candidate in candidates:
            if not candidate.session.is_closed:
                self._close_sockets([candidate.session.ws.sock])
            candidate.closed.set()

        threads = [self._supervisor] + list(self._workers)
        for thread in threads:
            if thread is None or thread is threading.current_thread():
                continue
            remaining = _remaining(deadline, self._monotonic)
            if remaining <= 0.0:
                break
            thread.join(remaining)

    close = stop

    # -- Internal lifecycle -------------------------------------------------

    def _validate_peer_id(self, peer_host_id: str) -> str:
        peer_host_id = _identifier(peer_host_id, "peer_host_id")
        if peer_host_id == self.local_host_id:
            raise ValueError("self-host sessions are not supported")
        return peer_host_id

    @staticmethod
    def _normalize_endpoints(
            endpoints: Sequence[PeerEndpoint]) -> Tuple[PeerEndpoint, ...]:
        if isinstance(endpoints, (str, bytes)):
            raise ValueError("endpoints must be a sequence of PeerEndpoint")
        normalized = []
        seen = set()
        for endpoint in endpoints:
            if not isinstance(endpoint, PeerEndpoint):
                raise ValueError("each endpoint must be a PeerEndpoint")
            key = (endpoint.address, endpoint.port, endpoint.server_name)
            if key not in seen:
                seen.add(key)
                normalized.append(endpoint)
        if len(normalized) > 64:
            raise ValueError("one peer may advertise at most 64 endpoints")
        return tuple(normalized)

    def _profile_snapshot(self) -> HelloProfile:
        profile = self._hello_provider()
        if not isinstance(profile, HelloProfile):
            raise ValueError("hello_provider must return HelloProfile")
        # Reconstruct to isolate a provider that reuses a mutable mapping.
        return HelloProfile(profile.namespaces, profile.capability_summary)

    def _ensure_not_stopped_locked(self) -> None:
        if self._stop.is_set():
            raise ManagerStopped()

    def _ensure_accept_allowed_locked(
            self, peer_host_id: str,
            authentication_context: Any = None) -> None:
        self._ensure_not_stopped_locked()
        if not self._started:
            raise PeerUnavailable("session manager has not started")
        if peer_host_id in self._revoked:
            raise PeerRevoked(peer_host_id)
        record = self._peers.get(peer_host_id)
        if record is None:
            raise PeerUnknown(peer_host_id)
        if record.authentication_context != authentication_context:
            raise PeerRevoked(
                f"authenticated context changed for {peer_host_id}")

    def _record_locked(self, peer_host_id: str) -> _PeerRecord:
        if peer_host_id in self._revoked:
            raise PeerRevoked(peer_host_id)
        record = self._peers.get(peer_host_id)
        if record is None:
            raise PeerUnknown(peer_host_id)
        return record

    def _next_generation_locked(self) -> int:
        if self._generation >= 0x7FFFFFFFFFFFFFFF:
            raise PairSessionError("connection generation space exhausted")
        self._generation += 1
        return self._generation

    def _supervisor_loop(self) -> None:
        while not self._stop.is_set():
            self._supervise_once()
            self._wake.wait(self.supervisor_interval_s)
            self._wake.clear()

    def _supervise_once(self) -> None:
        now = self._monotonic()
        closed = []
        with self._condition:
            for record in self._peers.values():
                candidate = record.active
                if candidate is not None and candidate.session.is_closed:
                    record.active = None
                    candidate.closed.set()
                    record.failures += 1
                    record.next_attempt = now + self._backoff(record.failures)
                    error = candidate.session.terminal_error
                    record.last_error = self._error_text(error)
                    closed.append((record.peer_host_id, candidate))
                if (record.active is None and record.endpoints
                        and not record.attempt_inflight
                        and not record.attempt_queued
                        and record.next_attempt <= now):
                    try:
                        self._dial_queue.put_nowait(record.peer_host_id)
                    except queue.Full:
                        # The queue is bounded; the next supervisor pass will
                        # retry rather than growing memory with duplicate work.
                        break
                    record.attempt_queued = True
            if closed:
                self._condition.notify_all()
            for peer_host_id, candidate in closed:
                self._emit_locked(
                    "connection_lost", peer_host_id,
                    candidate.connection_id,
                    self._error_text(candidate.session.terminal_error))

    def _dial_worker(self) -> None:
        while not self._stop.is_set():
            try:
                peer_host_id = self._dial_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                self._run_dial_attempt(peer_host_id)
            finally:
                self._dial_queue.task_done()

    def _run_dial_attempt(self, peer_host_id: str) -> None:
        endpoint = None
        revision = None
        authentication_context = None
        with self._condition:
            record = self._peers.get(peer_host_id)
            if record is None:
                return
            record.attempt_queued = False
            if (self._stop.is_set() or record.active is not None
                    or record.attempt_inflight or not record.endpoints):
                return
            record.attempt_inflight = True
            index = record.endpoint_cursor % len(record.endpoints)
            endpoint = record.endpoints[index]
            authentication_context = copy.deepcopy(
                record.authentication_context)
            record.endpoint_cursor = (index + 1) % len(record.endpoints)
            revision = record.revision
        error = None
        sock = None
        tracked = False
        try:
            sock = self._dial_authenticated(
                peer_host_id, endpoint,
                self.reconnect_policy.connect_timeout_s)
            if not isinstance(sock, socket.socket):
                raise TypeError("dial_authenticated must return a socket")
            with self._condition:
                record = self._peers.get(peer_host_id)
                if (self._stop.is_set() or record is None
                        or record.revision != revision
                        or peer_host_id in self._revoked):
                    raise PeerUnavailable("dial result became stale")
                self._handshake_sockets[sock] = peer_host_id
                tracked = True
            self._establish(
                sock, peer_host_id, role="client", endpoint=endpoint,
                expected_host=None,
                timeout_s=self.reconnect_policy.connect_timeout_s,
                config_revision=revision,
                authentication_context=authentication_context)
        except BaseException as exc:
            error = exc
            if sock is not None:
                self._close_sockets([sock])
        finally:
            if tracked:
                with self._condition:
                    self._handshake_sockets.pop(sock, None)
            now = self._monotonic()
            with self._condition:
                record = self._peers.get(peer_host_id)
                if record is not None:
                    record.attempt_inflight = False
                    if record.active is not None:
                        record.failures = 0
                        record.next_attempt = now
                        record.last_error = ""
                    elif record.revision != revision:
                        record.next_attempt = now
                    elif not self._stop.is_set():
                        record.failures += 1
                        record.next_attempt = now + self._backoff(
                            record.failures)
                        record.last_error = self._error_text(error)
                        self._emit_locked(
                            "dial_failed", peer_host_id, None,
                            record.last_error)
                    self._condition.notify_all()
            self._wake.set()

    def _establish(
            self, sock: socket.socket, peer_host_id: str, *, role: str,
            endpoint: Optional[PeerEndpoint], expected_host: Optional[str],
            timeout_s: float,
            config_revision: Optional[int],
            authentication_context: Any) -> AcceptedSession:
        deadline = self._monotonic() + timeout_s
        try:
            remaining = _remaining(deadline, self._monotonic)
            if remaining <= 0.0:
                raise ws.WebSocketTimeout("session establishment expired")
            if role == "client":
                if endpoint is None:
                    raise ValueError("client establishment requires an endpoint")
                ws.client_upgrade(
                    sock, endpoint.host_header, path=self.websocket_path,
                    timeout_s=remaining)
            elif role == "server":
                ws.server_upgrade(
                    sock, expected_path=self.websocket_path,
                    expected_host=expected_host,
                    timeout_s=remaining)
            else:
                raise ValueError("role must be client or server")
            connection = ws.WebSocketConnection(sock, role)
            remaining = _remaining(deadline, self._monotonic)
            if remaining <= 0.0:
                raise ws.WebSocketTimeout("session establishment expired")
            return self._negotiate(
                connection, peer_host_id, endpoint=endpoint,
                timeout_s=remaining, config_revision=config_revision,
                authentication_context=authentication_context)
        except BaseException:
            self._close_sockets([sock])
            raise

    def _negotiate(
            self, connection: ws.WebSocketConnection, peer_host_id: str, *,
            endpoint: Optional[PeerEndpoint], timeout_s: float,
            config_revision: Optional[int],
            authentication_context: Any) -> AcceptedSession:
        deadline = self._monotonic() + timeout_s
        role = connection.role
        with self._condition:
            self._ensure_accept_allowed_locked(
                peer_host_id, authentication_context)
            generation = self._next_generation_locked()
            hello_epoch = self._hello_epoch
        profile = self._profile_snapshot()
        local_nonce = base64.urlsafe_b64encode(
            secrets.token_bytes(18)).decode("ascii").rstrip("=")
        try:
            local_hello_message = ws.build_hello(
                self.local_host_id, peer_host_id, generation,
                profile.namespaces, profile.capability_summary,
                role=role, connection_nonce=local_nonce)
            remaining = _remaining(deadline, self._monotonic)
            if remaining <= 0.0:
                raise ws.WebSocketTimeout("session establishment expired")
            remote_hello = ws.exchange_hello(
                connection, local_hello_message,
                expected_peer_host_id=peer_host_id,
                timeout_s=remaining, monotonic=self._monotonic)
            authorized = tuple(sorted(
                set(profile.namespaces).intersection(remote_hello.namespaces)))
            if not authorized:
                raise ws.ProtocolError("hello has no mutually authorized namespace")

            holder: list[Optional[_Candidate]] = [None]

            def dispatch(method: str, params: Any) -> Any:
                candidate = holder[0]
                if candidate is None:
                    raise ws.RemoteError(
                        "stale_connection", "connection is not active")
                return self._dispatch(candidate, method, params)

            session = ws.Session(
                connection, dispatch,
                monotonic=self._monotonic,
                name=(f"{self.name}-{peer_host_id}-{role}-{generation}"),
                **self._session_options)
            candidate = self._make_candidate(
                session, peer_host_id, role, endpoint, generation,
                remote_hello, local_nonce, authorized, hello_epoch,
                config_revision, authentication_context)
            holder[0] = candidate
            selected, displaced = self._register_candidate(candidate)
            if not selected:
                self._close_candidate(
                    candidate, 0.1, "duplicate connection")
                return self._accepted_handle(candidate, selected=False)
            try:
                session.start()
                candidate.ready.set()
                with self._condition:
                    self._condition.notify_all()
                    self._emit_locked(
                        "connected", peer_host_id,
                        candidate.connection_id,
                        f"{role}; namespaces={','.join(authorized)}")
            except BaseException:
                self._discard_candidate(candidate)
                raise
            finally:
                if displaced is not None:
                    self._close_candidate(
                        displaced, 0.25, "duplicate connection replaced")
            return self._accepted_handle(candidate, selected=True)
        except BaseException:
            self._close_sockets([connection.sock])
            raise

    def _make_candidate(
            self, session: ws.Session, peer_host_id: str, role: str,
            endpoint: Optional[PeerEndpoint], local_generation: int,
            remote: ws.PeerHello, local_nonce: str,
            authorized: Tuple[str, ...], hello_epoch: int,
            config_revision: Optional[int],
            authentication_context: Any) -> _Candidate:
        client_host = self.local_host_id if role == "client" else peer_host_id
        server_host = peer_host_id if role == "client" else self.local_host_id
        client_nonce = local_nonce if role == "client" else remote.connection_nonce
        server_nonce = remote.connection_nonce if role == "client" else local_nonce
        generations = {
            self.local_host_id: local_generation,
            peer_host_id: remote.generation,
        }
        lower, upper = sorted((self.local_host_id, peer_host_id))
        canonical = int(client_host == lower)
        rank = (
            canonical, generations[lower], generations[upper],
            client_nonce, server_nonce)
        material = json.dumps({
            "client_host": client_host,
            "server_host": server_host,
            "client_nonce": client_nonce,
            "server_nonce": server_nonce,
        }, sort_keys=True, separators=(",", ":")).encode("utf-8")
        connection_id = "wss_" + hashlib.sha256(material).hexdigest()[:32]
        return _Candidate(
            peer_host_id, session, role, endpoint, local_generation, remote,
            local_nonce, authorized, connection_id, rank, hello_epoch,
            config_revision, copy.deepcopy(authentication_context))

    def _register_candidate(
            self, candidate: _Candidate) -> Tuple[bool, Optional[_Candidate]]:
        displaced = None
        with self._condition:
            if self._stop.is_set() or candidate.peer_host_id in self._revoked:
                return False, None
            record = self._peers.get(candidate.peer_host_id)
            if record is None or candidate.hello_epoch != self._hello_epoch:
                return False, None
            if (candidate.config_revision is not None
                    and candidate.config_revision != record.revision):
                return False, None
            if (record.authentication_context
                    != candidate.authentication_context):
                return False, None
            current = record.active
            if current is not None and current.session.is_closed:
                current.closed.set()
                current = None
                record.active = None
            if current is not None and candidate.rank <= current.rank:
                return False, None
            displaced = current
            record.active = candidate
            record.failures = 0
            record.next_attempt = self._monotonic()
            record.last_error = ""
            self._condition.notify_all()
            if displaced is not None:
                self._emit_locked(
                    "duplicate_replaced", candidate.peer_host_id,
                    candidate.connection_id,
                    f"replaced {displaced.connection_id}")
        return True, displaced

    def _discard_candidate(self, candidate: _Candidate) -> None:
        with self._condition:
            record = self._peers.get(candidate.peer_host_id)
            if record is not None and record.active is candidate:
                record.active = None
                record.next_attempt = self._monotonic()
                self._condition.notify_all()
        self._close_candidate(candidate, 0.1, "activation failed")
        self._wake.set()

    @staticmethod
    def _accepted_handle(candidate: _Candidate,
                         selected: bool) -> AcceptedSession:
        return AcceptedSession(
            selected, candidate.peer_host_id, candidate.connection_id,
            candidate.role, candidate.authorized_namespaces,
            candidate.remote_hello.generation, candidate.closed)

    @staticmethod
    def _close_candidate(candidate: _Candidate, timeout_s: float,
                         reason: str) -> None:
        try:
            candidate.session.close(timeout_s=timeout_s, reason=reason)
        finally:
            candidate.closed.set()

    def _dispatch(self, candidate: _Candidate,
                  method: str, envelope: Any) -> Any:
        if (not isinstance(envelope, dict)
                or set(envelope) != {"convoy_id", "payload"}):
            raise ws.RemoteError(
                "invalid_envelope",
                "RPC params must contain convoy_id and payload")
        convoy_id = envelope.get("convoy_id")
        if not isinstance(convoy_id, str) or convoy_id not in (
                candidate.authorized_namespaces):
            raise ws.RemoteError(
                "namespace_forbidden",
                "request namespace was not authorized by this connection")
        # This lock is the admission boundary for replacement/revocation.
        # Work admitted before revocation may finish under durable-job policy;
        # no new callback is admitted after the active candidate is removed.
        with self._condition:
            record = self._peers.get(candidate.peer_host_id)
            if (self._stop.is_set()
                    or candidate.peer_host_id in self._revoked
                    or record is None or record.active is not candidate):
                raise ws.RemoteError(
                    "stale_connection", "connection is no longer active")
            candidate.admitted_handlers += 1
        try:
            if self._handler is None:
                raise ws.RemoteError(
                    "method_not_found", "no host-pair RPC handler configured")
            return self._handler(
                candidate.peer_host_id, convoy_id, method,
                envelope.get("payload"),
                copy.deepcopy(candidate.authentication_context))
        finally:
            with self._condition:
                candidate.admitted_handlers -= 1
                self._condition.notify_all()

    def _connected_candidate(
            self, peer_host_id: str, deadline: float) -> _Candidate:
        with self._condition:
            while True:
                record = self._record_locked(peer_host_id)
                candidate = record.active
                if (candidate is not None and candidate.ready.is_set()
                        and not candidate.session.is_closed):
                    return candidate
                if self._stop.is_set():
                    raise ManagerStopped()
                remaining = _remaining(deadline, self._monotonic)
                if remaining <= 0.0:
                    raise PeerUnavailable(
                        f"no connected session for {peer_host_id}")
                self._condition.wait(remaining)

    def _peer_info_locked(self, record: _PeerRecord) -> PeerSessionInfo:
        now = self._monotonic()
        candidate = record.active
        if (candidate is not None and candidate.ready.is_set()
                and not candidate.session.is_closed):
            state = "connected"
        elif record.attempt_inflight or record.attempt_queued:
            state = "connecting"
        elif not record.endpoints:
            state = "inbound_only"
        elif record.next_attempt > now:
            state = "backoff"
        else:
            state = "disconnected"
        retry = None
        if state == "backoff":
            retry = max(0.0, record.next_attempt - now)
        return PeerSessionInfo(
            record.peer_host_id, state, record.endpoints,
            candidate.connection_id if candidate else None,
            candidate.role if candidate else None,
            candidate.endpoint if candidate else None,
            candidate.authorized_namespaces if candidate else (),
            candidate.remote_hello.protocol if candidate else None,
            candidate.remote_hello.generation if candidate else None,
            (json.loads(json.dumps(
                candidate.remote_hello.capability_summary,
                ensure_ascii=False, allow_nan=False))
             if candidate else {}),
            record.failures, retry, record.last_error)

    def _backoff(self, failures: int) -> float:
        exponent = max(0, min(30, failures - 1))
        delay = min(
            self.reconnect_policy.maximum_delay_s,
            self.reconnect_policy.initial_delay_s * (2 ** exponent))
        jitter = self.reconnect_policy.jitter_ratio
        if jitter:
            try:
                sample = float(self._random_value())
            except (TypeError, ValueError):
                sample = 0.5
            sample = max(0.0, min(1.0, sample))
            delay *= 1.0 + jitter * (2.0 * sample - 1.0)
        return max(0.001, delay)

    @staticmethod
    def _error_text(error: Optional[BaseException]) -> str:
        if error is None:
            return "connection closed"
        return str(error).replace("\r", " ").replace("\n", " ")[:512]

    def _emit_locked(self, kind: str, peer_host_id: str,
                     connection_id: Optional[str] = None,
                     detail: str = "") -> None:
        self._event_sequence += 1
        event = SessionEvent(
            self._event_sequence, kind, peer_host_id,
            connection_id, str(detail or "")[:512])
        try:
            self._events.put_nowait(event)
            return
        except queue.Full:
            self._events_dropped += 1
        # Observability must never backpressure connection correctness.  Keep
        # the newest bounded view by evicting one old event.
        try:
            self._events.get_nowait()
        except queue.Empty:
            pass
        try:
            self._events.put_nowait(event)
        except queue.Full:
            self._events_dropped += 1

    def _take_handshake_sockets_locked(
            self, peer_host_id: str) -> list[socket.socket]:
        result = [sock for sock, owner in self._handshake_sockets.items()
                  if owner == peer_host_id]
        for sock in result:
            self._handshake_sockets.pop(sock, None)
        return result

    @staticmethod
    def _close_sockets(sockets: Iterable[socket.socket]) -> None:
        for sock in sockets:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass


__all__ = [
    "AcceptedSession",
    "DEFAULT_CONNECT_TIMEOUT_S",
    "DEFAULT_DIAL_WORKERS",
    "DEFAULT_EVENT_QUEUE",
    "DEFAULT_MAX_PEERS",
    "HelloProfile",
    "HostPairSessionManager",
    "ManagerStopped",
    "NamespaceNotAuthorized",
    "PairSessionError",
    "PeerCapacityReached",
    "PeerEndpoint",
    "PeerRevoked",
    "PeerSessionInfo",
    "PeerUnavailable",
    "PeerUnknown",
    "ReconnectPolicy",
    "SessionEvent",
]
