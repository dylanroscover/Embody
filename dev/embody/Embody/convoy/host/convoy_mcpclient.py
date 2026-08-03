"""Minimal MCP client: forward ONE synchronous tool call to a node's
Envoy (Streamable HTTP). This is the Convoy dispatcher's forwarder seam
made real for the request/response tools it needs today.

Envoy's /mcp accepts a tools/call DIRECTLY -- no per-call initialize
handshake, no MCP session id, just the X-Envoy-Session attribution header
(verified against a live server: a bare tools/call returned the real
network). So a forward is one POST and one response parse, mirroring the
bridge's own forward_to_http.

Scope, stated honestly: this handles the SYNCHRONOUS request/response tool
call, and that is ALL a forward ever has to be -- long-running node-job
operations (run_tests, save_project) never block here. They start in
background mode and answer immediately with a job handle; the host then
POLLS the node with this same function (operation 'get_job_status'), so
one 30s request/response transport covers a twenty-minute test run. The
robust transport -- server-pushed progress, tools/list_changed, streaming
responses, reconnection -- is the A-46 rework. Anything this cannot
complete returns None, which the dispatcher records as INDETERMINATE on
the dispatch leg (never a fabricated verdict); on the POLL leg a None
changes nothing at all, because a failed read teaches nothing.

LOOPBACK ONLY: the target is always 127.0.0.1:<port> (Phase 1). The port
comes from the node's own registration, so the reach is bounded to a
local port the node itself claimed -- never an off-box or arbitrary host.
"""

import json
import urllib.error
import urllib.request

SESSION_HEADER = "X-Envoy-Session"
DEFAULT_TIMEOUT_S = 30.0
# The inline MCP leg is a control channel, not an artifact transport.
# Bound the read before decoding/JSON parsing so a compromised or buggy
# local Envoy cannot make the host buffer an arbitrary response. Large
# screenshots move through Convoy's artifact layer instead.
MAX_MCP_RESPONSE_BYTES = 8 * 1024 * 1024


class _Unreachable:
    """The node's Envoy REFUSED the connection: the TCP connect failed, so
    the HTTP request was never delivered and the operation definitely did
    NOT run. Distinct from None (a failure AFTER the request may have been
    sent, where the op may have run): the dispatcher leaves an unreachable
    job QUEUED to retry, but records a no-response failure as
    indeterminate. Never confuse 'did not run, retry' with 'may have run,
    unknown'."""
    __slots__ = ()

    def __repr__(self):
        return "UNREACHABLE"


UNREACHABLE = _Unreachable()


def _parse_jsonrpc(body):
    """Parse a Streamable-HTTP response: SSE-framed ('data: {json}') or a
    plain JSON body. Returns the JSON-RPC object, or None if unparseable.
    Mirrors the bridge's forward_to_http parser exactly."""
    for line in body.split("\n"):
        stripped = line.strip()
        if stripped.startswith("data: "):
            try:
                return json.loads(stripped[6:])
            except ValueError:
                continue        # truncated SSE line -- try the next
    body = body.strip()
    if body:
        try:
            return json.loads(body)
        except ValueError:
            return None
    return None


def _tool_payload(content):
    """Flatten an MCP tool result's content list to what the node
    returned: the parsed JSON of the first text block, else its raw text,
    else the content itself."""
    if not isinstance(content, list) or not content:
        return None
    # Preserve multi-block results.  In particular, capture_top can return
    # a text descriptor followed by an image block; silently taking only
    # the first block made a successful remote screenshot unusable.
    if len(content) > 1:
        return {"content": content}
    first = content[0]
    text = first.get("text") if isinstance(first, dict) else None
    if text is None:
        return content
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return text


def _read_bounded(response):
    raw = response.read(MAX_MCP_RESPONSE_BYTES + 1)
    if len(raw) > MAX_MCP_RESPONSE_BYTES:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def forward(port, operation, arguments, opener=None,
            timeout=DEFAULT_TIMEOUT_S, session="convoy-dispatch"):
    """Execute one tool call against a node's local Envoy.

    Returns {"ok": True, "result": ...} for an executed tool, {"ok":
    False, "error": ...} for a tool or protocol error the node reported,
    UNREACHABLE when the connection was REFUSED (the op did NOT run --
    retry-safe), or None on any other transport failure (no usable
    response after the request may have been sent -- the op MAY have run,
    so the dispatcher records indeterminate). Never raises: a broken
    transport must not crash dispatch.
    """
    opener = opener or urllib.request.urlopen
    message = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": operation, "arguments": arguments or {}}}
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:%d/mcp" % int(port),
            data=json.dumps(message).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Accept": "application/json, text/event-stream",
                     SESSION_HEADER: session,
                     "X-Envoy-Label": "convoy host dispatch"})
    except (TypeError, ValueError):
        return UNREACHABLE      # a non-numeric port cannot be reached
    try:
        with opener(req, timeout=timeout) as resp:
            body = _read_bounded(resp)
        if body is None:
            # The operation may have run, but its response was outside the
            # control-channel contract.  None maps to indeterminate rather
            # than fabricating either success or failure.
            return None
    except urllib.error.HTTPError as e:
        # An HTTP error IS a response (the node answered) -- a tool error,
        # not a transport failure.
        try:
            body = _read_bounded(e)
            if body is None:
                return None
        except Exception:
            return {"ok": False, "error": "HTTP %s" % e.code}
    except urllib.error.URLError as e:
        # A refused connection never delivered the request -> did not run.
        if isinstance(getattr(e, "reason", None), ConnectionRefusedError):
            return UNREACHABLE
        return None             # other transport failure -> may have run
    except ConnectionRefusedError:
        return UNREACHABLE
    except (OSError, ValueError):
        return None             # transport gone after send -> indeterminate
    parsed = _parse_jsonrpc(body)
    if not isinstance(parsed, dict):
        return None
    if parsed.get("error"):
        return {"ok": False, "error": parsed["error"]}
    result = parsed.get("result")
    if not isinstance(result, dict):
        return {"ok": False, "error": "malformed tool response"}
    payload = _tool_payload(result.get("content"))
    if result.get("isError"):
        return {"ok": False, "error": payload}
    return {"ok": True, "result": payload}
