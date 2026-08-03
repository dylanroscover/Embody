"""Minimal, host-private consumer of Owlette's public API.

Convoy and Owlette meet at a deliberately narrow seam.  This module can read
site/machine inventory and can use the public machine-command submit/status
routes documented by Owlette OpenAPI 2.3.1.  It does not import TouchDesigner,
open a listener, create a tunnel, or know anything about Convoy jobs.

AUTHORITY STAYS ON THE HOST.  API keys are obtained through a callable for
each request.  The default callable reads the host process environment; an
installer may inject an OS credential-store reader.  Tokens never enter a
project file, request URL, exception string, result dictionary, or repr.

THE INTERNET LEG IS BOUNDED.  Only the two published HTTPS origins are
accepted, redirects are refused, TLS certificate/hostname verification uses
the platform trust store, request/response sizes and timeouts are capped, and
every side-effecting request requires a caller-held idempotency key.  This
client never automatically retries a mutation.  A failure after sending is
reported as an unknown outcome so the caller can reconcile or replay the same
idempotency key.

NO GENERIC CANCEL IS PUBLISHED.  Owlette 2.3.1 documents POST command and GET
status routes but no cancel method on a machine command.  ``cancel_command``
therefore fails locally with ``owlette_capability_unsupported``.  Do not wire
an internal/dashboard route here; publication is the capability boundary.

Pure stdlib and safe to call from a Convoy host worker thread.
"""

from __future__ import annotations

import http.client
import json
import math
import os
import ssl
import urllib.parse
import uuid


OPENAPI_VERSION = "2.3.1"
PRODUCTION_BASE_URL = "https://owlette.app"
DEVELOPMENT_BASE_URL = "https://dev.owlette.app"
PUBLIC_BASE_URLS = frozenset((PRODUCTION_BASE_URL, DEVELOPMENT_BASE_URL))

ENV_API_KEY = "OWLETTE_API_KEY"
ENV_API_KEY_SECRET = "OWLETTE_API_KEY_SECRET"
ENV_API_BASE_URL = "OWLETTE_API_BASE_URL"
ENV_SITE_ID = "OWLETTE_SITE_ID"
ENV_HTTP_TIMEOUT_S = "OWLETTE_HTTP_TIMEOUT_S"

DEFAULT_HTTP_TIMEOUT_S = 10.0
MIN_HTTP_TIMEOUT_S = 0.5
MAX_HTTP_TIMEOUT_S = 30.0
MAX_REQUEST_BYTES = 256 * 1024
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_IDENTIFIER_BYTES = 512
MAX_TOKEN_BYTES = 4096
MAX_IDEMPOTENCY_KEY_BYTES = 255
_READ_CHUNK = 64 * 1024

# Exact enum on POST /api/sites/{siteId}/machines/{machineId}/commands in
# Owlette OpenAPI 2.3.1.  This is intentionally an allowlist, not a pass-through
# for undocumented agent commands.
PUBLIC_COMMAND_TYPES = frozenset((
    "reboot_machine",
    "shutdown_machine",
    "cancel_reboot",
    "dismiss_reboot_pending",
    "capture_screenshot",
    "start_live_view",
    "stop_live_view",
    "restart_process",
    "start_process",
    "kill_process",
    "set_launch_mode",
    "apply_display_topology",
    "ack_display_topology",
    "enumerate_display_modes",
    "test_display_apply",
    "mcp_tool_call",
    "update_owlette",
))
PUBLIC_COMMAND_STATUSES = frozenset((
    "pending", "in_progress", "completed", "failed",
))


class OwletteError(Exception):
    """Base error with a stable, credential-free reason code."""

    reason = "owlette_error"

    def __init__(self, detail="", *, status=None, code=None, request_id=None,
                 retry_after=None, outcome_unknown=False):
        self.detail = str(detail or "")
        self.status = status
        self.code = code
        self.request_id = request_id
        self.retry_after = retry_after
        self.outcome_unknown = bool(outcome_unknown)
        super().__init__(
            "%s: %s" % (self.reason, self.detail)
            if self.detail else self.reason)

    def as_dict(self):
        result = {"ok": False, "reason": self.reason,
                  "outcome_unknown": self.outcome_unknown}
        for name, value in (("detail", self.detail), ("status", self.status),
                            ("code", self.code),
                            ("request_id", self.request_id),
                            ("retry_after", self.retry_after)):
            if value not in (None, ""):
                result[name] = value
        return result


class OwletteConfigError(OwletteError):
    reason = "owlette_config_invalid"


class OwletteCredentialUnavailable(OwletteError):
    reason = "owlette_credentials_unavailable"


class OwletteValidationError(OwletteError):
    reason = "owlette_request_invalid"


class OwletteCapabilityUnsupported(OwletteError):
    reason = "owlette_capability_unsupported"


class OwletteTransportError(OwletteError):
    reason = "owlette_transport_failed"


class OwletteProtocolError(OwletteError):
    reason = "owlette_protocol_invalid"


class OwletteApiError(OwletteError):
    reason = "owlette_api_error"


class OwletteConfig:
    """Non-secret connection settings for one public Owlette origin."""

    __slots__ = ("base_url", "host", "port", "timeout_s", "default_site_id")

    def __init__(self, base_url=PRODUCTION_BASE_URL,
                 timeout_s=DEFAULT_HTTP_TIMEOUT_S, default_site_id=None):
        base_url, host, port = _public_origin(base_url)
        self.base_url = base_url
        self.host = host
        self.port = port
        self.timeout_s = _bounded_timeout(timeout_s)
        self.default_site_id = (
            _identifier(default_site_id, "site_id")
            if default_site_id not in (None, "") else None)

    @classmethod
    def from_env(cls, environ=None):
        environ = os.environ if environ is None else environ
        return cls(
            base_url=environ.get(ENV_API_BASE_URL, PRODUCTION_BASE_URL),
            timeout_s=environ.get(ENV_HTTP_TIMEOUT_S,
                                  DEFAULT_HTTP_TIMEOUT_S),
            default_site_id=environ.get(ENV_SITE_ID) or None,
        )

    def __repr__(self):
        return ("OwletteConfig(base_url=%r, timeout_s=%r, "
                "default_site_id=%r)" %
                (self.base_url, self.timeout_s, self.default_site_id))


class HttpResponse:
    """Detached bounded HTTP response used by the injectable transport."""

    __slots__ = ("status", "headers", "body")

    def __init__(self, status, headers, body):
        self.status = int(status)
        self.headers = {
            str(key).lower(): str(value)
            for key, value in dict(headers or {}).items()
        }
        self.body = bytes(body or b"")


class HttpsTransport:
    """One verified HTTPS request per call; redirects are never followed."""

    def __init__(self, context=None, connection_factory=None):
        self._context = context or _default_tls_context()
        self._connection_factory = (
            connection_factory or http.client.HTTPSConnection)

    def request(self, config, method, path, headers, body):
        conn = self._connection_factory(
            config.host, config.port, timeout=config.timeout_s,
            context=self._context)
        may_have_sent = False
        try:
            try:
                conn.connect()
            except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
                raise OwletteTransportError(
                    "verified HTTPS connection could not be established") from exc
            try:
                # Once request() starts, a partial write is possible even if it
                # raises.  Mutations must be treated as unknown-outcome.
                may_have_sent = True
                conn.request(method, path, body=body, headers=headers)
                response = conn.getresponse()
                raw = _read_bounded(response)
            except (OSError, ssl.SSLError, http.client.HTTPException,
                    _ResponseTooLarge) as exc:
                raise OwletteTransportError(
                    "request failed after transmission may have begun",
                    outcome_unknown=(method != "GET" and may_have_sent)) from exc
            return HttpResponse(response.status, response.getheaders(), raw)
        finally:
            try:
                conn.close()
            except (OSError, http.client.HTTPException):
                pass


class OwletteClient:
    """Stateless public API client.

    ``credential_provider`` is called immediately before every request.  It
    may read an environment variable or an OS credential store, allowing key
    rotation without rebuilding the client.  It must return an Owlette API
    key; raw keys are deliberately not accepted by this constructor.  The
    built-in transport keeps no per-request shared state.  Injected credential
    providers and transports must provide their own thread safety.
    """

    __slots__ = ("config", "_credential_provider", "_transport")

    def __init__(self, config, credential_provider, transport=None):
        if not isinstance(config, OwletteConfig):
            raise OwletteConfigError("config must be OwletteConfig")
        if not callable(credential_provider):
            raise OwletteConfigError("credential_provider must be callable")
        self.config = config
        self._credential_provider = credential_provider
        self._transport = transport or HttpsTransport()

    def __repr__(self):
        return "OwletteClient(config=%r, credential=<provider>)" % self.config

    def list_sites(self):
        payload, unused_headers = self._request("GET", ("api", "sites"),
                                                expected=(200,))
        sites = payload.get("sites") if isinstance(payload, dict) else None
        if not isinstance(sites, list):
            raise OwletteProtocolError("site list has no sites array")
        return [_site(row) for row in sites]

    def get_site(self, site_id=None):
        site_id = self._site_id(site_id)
        payload, unused_headers = self._request(
            "GET", ("api", "sites", site_id), expected=(200,))
        return _site(payload)

    def list_machines(self, site_id=None):
        site_id = self._site_id(site_id)
        payload, unused_headers = self._request(
            "GET", ("api", "sites", site_id, "machines"), expected=(200,))
        machines = payload.get("machines") if isinstance(payload, dict) else None
        if not isinstance(machines, list):
            raise OwletteProtocolError("machine list has no machines array")
        return [_machine(row, detail=False) for row in machines]

    def get_machine(self, site_id, machine_id):
        site_id = self._site_id(site_id)
        machine_id = _identifier(machine_id, "machine_id")
        payload, unused_headers = self._request(
            "GET", ("api", "sites", site_id, "machines", machine_id),
            expected=(200,))
        return _machine(payload, detail=True)

    def machine_online(self, site_id, machine_id):
        """Read the API-owned online bit; never infer it from local clocks."""
        return self.get_machine(site_id, machine_id)["online"]

    def submit_command(self, site_id, machine_id, command_type, *,
                       idempotency_key, params=None, timeout_seconds=60):
        site_id = self._site_id(site_id)
        machine_id = _identifier(machine_id, "machine_id")
        if command_type not in PUBLIC_COMMAND_TYPES:
            raise OwletteCapabilityUnsupported(
                "command type %r is not in Owlette %s's public allowlist" %
                (command_type, OPENAPI_VERSION))
        idempotency_key = _idempotency_key(idempotency_key)
        if params is None:
            params = {}
        if not isinstance(params, dict):
            raise OwletteValidationError("params must be an object")
        if (isinstance(timeout_seconds, bool)
                or not isinstance(timeout_seconds, int)
                or not 1 <= timeout_seconds <= 600):
            raise OwletteValidationError(
                "timeout_seconds must be an integer from 1 through 600")
        body = {"type": command_type, "params": params,
                "timeout_seconds": timeout_seconds}
        payload, headers = self._request(
            "POST", ("api", "sites", site_id, "machines", machine_id,
                     "commands"), expected=(202,), body=body,
            idempotency_key=idempotency_key)
        # The server has accepted (202) and durably recorded the command, so
        # this is a success, not a failure.  A replayed idempotency key
        # legitimately returns the original, already-progressed record, so any
        # documented status (not just "pending") is valid here.  A
        # commandId-format or unknown-status surprise is surfaced as a warning
        # on the accepted result rather than raised; any deeper parse failure
        # is an unknown outcome the caller can reconcile or replay with the
        # same key, never a definitive failure.
        data = _command_data(
            payload, expected_statuses=PUBLIC_COMMAND_STATUSES,
            outcome_unknown=True, lenient=True)
        data["idempotencyKey"] = idempotency_key
        data["replayed"] = headers.get("idempotent-replayed", "").lower() \
            == "true"
        return data

    def get_command_status(self, site_id, machine_id, command_id):
        site_id = self._site_id(site_id)
        machine_id = _identifier(machine_id, "machine_id")
        command_id = _command_id(command_id)
        payload, unused_headers = self._request(
            "GET", ("api", "sites", site_id, "machines", machine_id,
                    "commands", command_id), expected=(200,))
        return _command_data(payload, expected_statuses=PUBLIC_COMMAND_STATUSES)

    def cancel_command(self, site_id, machine_id, command_id, *,
                       idempotency_key):
        """Fail closed: OpenAPI 2.3.1 publishes no generic cancellation."""
        raise OwletteCapabilityUnsupported(
            "Owlette %s publishes command submit/status but no generic "
            "command-cancel endpoint" % OPENAPI_VERSION)

    def _site_id(self, value):
        value = self.config.default_site_id if value in (None, "") else value
        if value in (None, ""):
            raise OwletteValidationError(
                "site_id is required when OWLETTE_SITE_ID is not configured")
        return _identifier(value, "site_id")

    def _request(self, method, segments, *, expected, body=None,
                 idempotency_key=None):
        try:
            token = _api_key(self._credential_provider())
        except OwletteError:
            raise
        except Exception:
            # Credential backends have a habit of putting account/secret
            # material in their exception text.  Never chain or repeat it.
            raise OwletteCredentialUnavailable(
                "the configured credential provider failed") from None
        path = "/" + "/".join(_path_segment(value) for value in segments)
        headers = {
            "Accept": "application/json, application/problem+json",
            "Authorization": "Bearer " + token,
            "User-Agent": "Embody-Convoy-Owlette/1",
        }
        encoded = None
        if body is not None:
            try:
                encoded = json.dumps(
                    body, ensure_ascii=False, separators=(",", ":"),
                    sort_keys=True, allow_nan=False).encode("utf-8")
            except (TypeError, ValueError, UnicodeEncodeError) as exc:
                raise OwletteValidationError(
                    "request body must be finite JSON") from exc
            if len(encoded) > MAX_REQUEST_BYTES:
                raise OwletteValidationError("request body exceeds size limit")
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(encoded))
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        try:
            response = self._transport.request(
                self.config, method, path, headers, encoded)
        except OwletteError:
            raise
        except Exception as exc:
            # An injected transport follows the same fail-closed contract as
            # the real one.  For a mutation, conservatively assume unknown.
            raise OwletteTransportError(
                "transport failed without a response",
                outcome_unknown=(method != "GET")) from exc
        if not isinstance(response, HttpResponse):
            raise OwletteProtocolError(
                "transport returned an invalid response",
                outcome_unknown=(method != "GET"))
        if 300 <= response.status <= 399:
            raise OwletteProtocolError(
                "redirect refused; the API origin is pinned",
                status=response.status, outcome_unknown=False)
        payload = _json_response(response, outcome_unknown=(method != "GET"))
        if response.status not in expected:
            # A 4xx is a definitive refusal.  A 5xx after a mutation can be a
            # server failure after commit, so reconciliation still matters.
            raise _api_error(
                response, payload,
                outcome_unknown=(method != "GET" and response.status >= 500),
                secret=token)
        return payload, response.headers


def client_from_env(environ=None, *, secret_reader=None, transport=None):
    """Build a client without copying a token into configuration state.

    With ``OWLETTE_API_KEY_SECRET`` set, ``secret_reader(reference)`` is the
    OS-secret-store seam.  Without it, the provider reads ``OWLETTE_API_KEY``
    from the process environment at request time.  A named secret with no
    reader fails closed; it never falls back to a second credential source.
    """
    environ = os.environ if environ is None else environ
    config = OwletteConfig.from_env(environ)
    secret_reference = environ.get(ENV_API_KEY_SECRET)
    if secret_reference:
        secret_reference = _secret_reference(secret_reference)
        if not callable(secret_reader):
            def credential_provider():
                raise OwletteCredentialUnavailable(
                    "an OS secret reference is configured but no secret "
                    "reader is available")
        else:
            def credential_provider():
                try:
                    return secret_reader(secret_reference)
                except OwletteError:
                    raise
                except Exception:
                    raise OwletteCredentialUnavailable(
                        "the OS credential store could not read the configured "
                        "secret") from None
    else:
        def credential_provider():
            return environ.get(ENV_API_KEY)
    return OwletteClient(config, credential_provider, transport=transport)


def new_idempotency_key(prefix="embody-convoy"):
    """Mint a caller-held key; reuse this exact value when reconciling."""
    prefix = str(prefix or "").strip()
    if (not prefix or len(prefix.encode("utf-8")) > 64
            or any(ord(ch) < 0x21 or ord(ch) > 0x7e for ch in prefix)):
        raise OwletteValidationError(
            "idempotency key prefix must be 1-64 visible ASCII characters")
    return _idempotency_key("%s-%s" % (prefix, uuid.uuid4().hex))


def public_capabilities():
    """Stable integration hook; unsupported operations are explicit."""
    return {
        "openapi_version": OPENAPI_VERSION,
        "site_inventory": True,
        "machine_inventory": True,
        "machine_online": True,
        "command_submit": True,
        "command_status": True,
        "command_cancel": False,
        "command_types": sorted(PUBLIC_COMMAND_TYPES),
    }


class _ResponseTooLarge(Exception):
    pass


def _default_tls_context():
    context = ssl.create_default_context()
    if hasattr(ssl, "TLSVersion"):
        context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


def _public_origin(value):
    if not isinstance(value, str):
        raise OwletteConfigError("Owlette API base URL must be text")
    value = value.strip().rstrip("/")
    try:
        parsed = urllib.parse.urlsplit(value)
    except ValueError as exc:
        raise OwletteConfigError("Owlette API base URL is malformed") from exc
    if (parsed.scheme != "https" or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
        raise OwletteConfigError(
            "Owlette API base URL must be a bare published HTTPS origin")
    try:
        port = parsed.port or 443
    except ValueError as exc:
        raise OwletteConfigError("Owlette API port is invalid") from exc
    origin = "https://" + parsed.hostname.lower()
    if port != 443:
        origin += ":%d" % port
    if origin not in PUBLIC_BASE_URLS:
        raise OwletteConfigError(
            "Owlette API origin is not in the published origin allowlist")
    return origin, parsed.hostname.lower(), port


def _bounded_timeout(value):
    try:
        value = float(value)
    except (TypeError, ValueError) as exc:
        raise OwletteConfigError("HTTP timeout must be numeric") from exc
    if (not math.isfinite(value)
            or not MIN_HTTP_TIMEOUT_S <= value <= MAX_HTTP_TIMEOUT_S):
        raise OwletteConfigError(
            "HTTP timeout must be between %.1f and %.1f seconds" %
            (MIN_HTTP_TIMEOUT_S, MAX_HTTP_TIMEOUT_S))
    return value


def _identifier(value, label):
    if not isinstance(value, str) or value != value.strip() or not value:
        raise OwletteValidationError("%s must be non-empty text" % label)
    try:
        raw = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise OwletteValidationError("%s is not valid UTF-8" % label) from exc
    if (len(raw) > MAX_IDENTIFIER_BYTES
            or any(byte < 0x20 or byte == 0x7f for byte in raw)):
        raise OwletteValidationError("%s is outside the accepted bounds" % label)
    return value


def _path_segment(value):
    value = _identifier(value, "path segment")
    return urllib.parse.quote(value, safe="")


def _command_id(value):
    value = _identifier(value, "command_id")
    if (not value.startswith("cmd_") or len(value) > 84
            or any(not (ch.isalnum() or ch in "_-") for ch in value[4:])):
        raise OwletteValidationError(
            "command_id does not match the public cmd_ format")
    suffix = value[4:]
    if not 1 <= len(suffix) <= 80 or not suffix.isascii():
        raise OwletteValidationError(
            "command_id does not match the public cmd_ format")
    return value


def _idempotency_key(value):
    if not isinstance(value, str) or value != value.strip() or not value:
        raise OwletteValidationError("idempotency_key is required")
    try:
        raw = value.encode("ascii")
    except UnicodeEncodeError as exc:
        raise OwletteValidationError(
            "idempotency_key must be visible ASCII") from exc
    if (len(raw) > MAX_IDEMPOTENCY_KEY_BYTES
            or any(byte < 0x21 or byte > 0x7e for byte in raw)):
        raise OwletteValidationError(
            "idempotency_key must be at most 255 visible ASCII bytes")
    return value


def _secret_reference(value):
    if not isinstance(value, str) or value != value.strip() or not value:
        raise OwletteConfigError("OS secret reference must be non-empty text")
    if (len(value.encode("utf-8")) > 512
            or any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in value)):
        raise OwletteConfigError("OS secret reference is outside accepted bounds")
    return value


def _api_key(value):
    if not isinstance(value, str):
        raise OwletteCredentialUnavailable("Owlette API key is not configured")
    try:
        raw = value.encode("ascii")
    except UnicodeEncodeError as exc:
        raise OwletteCredentialUnavailable(
            "Owlette API key has an invalid format") from exc
    prefix = ("owk_live_" if value.startswith("owk_live_")
              else "owk_test_" if value.startswith("owk_test_") else None)
    if (prefix is None or len(value) <= len(prefix)
            or len(raw) > MAX_TOKEN_BYTES
            or any(byte < 0x21 or byte > 0x7e for byte in raw)):
        raise OwletteCredentialUnavailable(
            "Owlette API key has an invalid public-key format")
    return value


def _read_bounded(response):
    declared = response.getheader("Content-Length")
    if declared is not None:
        try:
            declared = int(declared)
        except (TypeError, ValueError) as exc:
            raise _ResponseTooLarge("invalid content length") from exc
        if declared < 0 or declared > MAX_RESPONSE_BYTES:
            raise _ResponseTooLarge("response exceeds size limit")
    out = bytearray()
    while True:
        chunk = response.read(min(_READ_CHUNK,
                                  MAX_RESPONSE_BYTES + 1 - len(out)))
        if not chunk:
            break
        out.extend(chunk)
        if len(out) > MAX_RESPONSE_BYTES:
            raise _ResponseTooLarge("response exceeds size limit")
    if declared is not None and len(out) != declared:
        raise _ResponseTooLarge("response length does not match header")
    return bytes(out)


def _json_response(response, *, outcome_unknown):
    content_type = response.headers.get("content-type", "")
    if content_type and not (
            content_type.lower().startswith("application/json")
            or content_type.lower().startswith("application/problem+json")):
        raise OwletteProtocolError(
            "Owlette response is not JSON", status=response.status,
            outcome_unknown=outcome_unknown)
    if len(response.body) > MAX_RESPONSE_BYTES:
        raise OwletteProtocolError(
            "Owlette response exceeds size limit", status=response.status,
            outcome_unknown=outcome_unknown)
    try:
        payload = json.loads(response.body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise OwletteProtocolError(
            "Owlette response is not valid UTF-8 JSON", status=response.status,
            outcome_unknown=outcome_unknown) from exc
    if not isinstance(payload, dict):
        raise OwletteProtocolError(
            "Owlette response top level must be an object",
            status=response.status, outcome_unknown=outcome_unknown)
    return payload


def _api_error(response, payload, *, outcome_unknown, secret=None):
    code = _server_text(payload.get("code"), secret, 128)
    detail = payload.get("detail")
    if not isinstance(detail, str):
        detail = payload.get("title")
    if not isinstance(detail, str):
        detail = "Owlette API returned HTTP %d" % response.status
    detail = _server_text(detail, secret, 2048)
    request_id = _server_text(payload.get("requestId"), secret, 512)
    retry_after = _server_text(
        response.headers.get("retry-after"), secret, 128)
    return OwletteApiError(
        detail, status=response.status, code=code, request_id=request_id,
        retry_after=retry_after, outcome_unknown=outcome_unknown)


def _server_text(value, secret, limit):
    """Bound and redact untrusted diagnostic strings before surfacing them."""
    if not isinstance(value, str):
        return None
    if secret:
        value = value.replace(secret, "<redacted>")
    if len(value) > limit:
        value = value[:limit] + "..."
    return value


def _text_field(row, name, *, required=False, nullable=False):
    value = row.get(name)
    if value is None and nullable:
        return None
    if not isinstance(value, str) or (required and not value):
        raise OwletteProtocolError("response field %s must be text" % name)
    return value


def _site(row):
    if not isinstance(row, dict):
        raise OwletteProtocolError("site entry must be an object")
    result = {
        "id": _text_field(row, "id", required=True),
        "name": _text_field(row, "name", required=True),
    }
    for name in ("plan", "tier", "timezone", "owner", "createdAt",
                 "tierUpgradedAt"):
        if name in row:
            result[name] = row[name]
    return result


def _machine(row, *, detail):
    if not isinstance(row, dict):
        raise OwletteProtocolError("machine entry must be an object")
    if type(row.get("online")) is not bool:
        raise OwletteProtocolError("response field online must be a boolean")
    result = {
        "id": _text_field(row, "id", required=True),
        "name": _text_field(row, "name", required=True),
        "online": row["online"],
    }
    for name in ("lastHeartbeat", "agentVersion", "os", "currentRoosts"):
        if name in row:
            result[name] = row[name]
    if detail:
        for name in ("siteId", "hostname", "metrics", "processes"):
            if name in row:
                result[name] = row[name]
    return result


def _command_data(payload, *, expected_statuses, outcome_unknown=False,
                  lenient=False):
    """Parse a command envelope.

    ``outcome_unknown`` is stamped on any error raised here so a caller that
    has already sent a mutation (a 202 accept) reports an unknown outcome the
    server may still act on.  ``lenient`` (the post-accept submit path)
    downgrades a commandId-format or unknown-status surprise to a ``warnings``
    field on an otherwise successful result instead of raising, because the
    command was already accepted and the caller must be able to reconcile it.
    """
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        raise OwletteProtocolError(
            "command response is not a successful envelope",
            outcome_unknown=outcome_unknown)
    data = payload.get("data")
    if not isinstance(data, dict):
        raise OwletteProtocolError(
            "command response has no data object",
            outcome_unknown=outcome_unknown)
    warnings = []
    raw_command_id = data.get("commandId")
    try:
        command_id = _command_id(raw_command_id)
    except OwletteValidationError:
        if not lenient:
            raise
        command_id = raw_command_id if isinstance(raw_command_id, str) else None
        warnings.append("commandId does not match the public cmd_ format")
    status = data.get("status")
    if status not in expected_statuses:
        if not lenient:
            raise OwletteProtocolError(
                "command response has an invalid status",
                outcome_unknown=outcome_unknown)
        warnings.append("status is not a documented command status")
    result = {"commandId": command_id, "status": status}
    if "result" in data:
        if not isinstance(data["result"], dict):
            raise OwletteProtocolError(
                "command result must be an object",
                outcome_unknown=outcome_unknown)
        result["result"] = data["result"]
    if "error" in data:
        if not isinstance(data["error"], str):
            raise OwletteProtocolError(
                "command error must be text", outcome_unknown=outcome_unknown)
        result["error"] = data["error"]
    for name in ("createdAt", "updatedAt"):
        if name in data:
            value = data[name]
            if value is not None and not isinstance(value, str):
                raise OwletteProtocolError(
                    "command %s must be text or null" % name,
                    outcome_unknown=outcome_unknown)
            result[name] = value
    if warnings:
        result["warnings"] = warnings
    return result
