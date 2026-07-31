"""Minimal MCP client: forward ONE synchronous tool call to a node's
Envoy (Streamable HTTP). This is the Convoy dispatcher's forwarder seam
made real for the request/response tools it needs today.

Envoy's /mcp accepts a tools/call DIRECTLY -- no per-call initialize
handshake, no MCP session id, just the X-Envoy-Session attribution header
(verified against a live server: a bare tools/call returned the real
network). So a forward is one POST and one response parse, mirroring the
bridge's own forward_to_http.

Scope, stated honestly: this handles the SYNCHRONOUS request/response tool
call the dispatcher's slice 1 uses. The robust transport -- server-pushed
progress, tools/list_changed, streaming / long-running tools, reconnection
-- is the A-46 rework, and long-running node-job operations (run_tests,
save_project) are a later dispatcher slice that polls the node job rather
than blocking here. Anything this cannot complete returns None, which the
dispatcher records as INDETERMINATE (never a fabricated verdict).

LOOPBACK ONLY: the target is always 127.0.0.1:<port> (Phase 1). The port
comes from the node's own registration, so the reach is bounded to a
local port the node itself claimed -- never an off-box or arbitrary host.
"""

import json
import urllib.error
import urllib.request

SESSION_HEADER = "X-Envoy-Session"
DEFAULT_TIMEOUT_S = 30.0


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
    first = content[0]
    text = first.get("text") if isinstance(first, dict) else None
    if text is None:
        return content
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return text


def forward(port, operation, arguments, opener=None,
            timeout=DEFAULT_TIMEOUT_S, session="convoy-dispatch"):
    """Execute one tool call against a node's local Envoy.

    Returns {"ok": True, "result": ...} for an executed tool, {"ok":
    False, "error": ...} for a tool or protocol error the node reported,
    or None on a TRANSPORT failure (no usable response) -- which the
    dispatcher treats as indeterminate. Never raises: a broken transport
    must not crash dispatch.
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
        return None             # a non-numeric port cannot be reached
    try:
        with opener(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        # An HTTP error IS a response (the node answered) -- a tool error,
        # not a transport failure.
        try:
            body = e.read().decode("utf-8")
        except Exception:
            return {"ok": False, "error": "HTTP %s" % e.code}
    except (urllib.error.URLError, OSError, ValueError):
        return None             # transport gone -> indeterminate
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
