"""The minimal MCP client (dispatcher forwarder). The transport is
isolated by an injected opener, so these run with no network -- the live
end-to-end path is exercised separately by the dispatch integration
probe."""

import io
import json
import urllib.error

import pytest

import convoy_mcpclient as mc


class FakeResp:
    def __init__(self, body):
        self._body = body.encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def opener_returning(body):
    return lambda req, timeout=None: FakeResp(body)


def sse(obj):
    return "event: message\ndata: %s\n\n" % json.dumps(obj)


def tool_response(payload, is_error=False):
    return {"jsonrpc": "2.0", "id": 1,
            "result": {"content": [{"type": "text",
                                    "text": json.dumps(payload)}],
                       "isError": is_error}}


def test_success_unwraps_the_tool_result():
    body = sse(tool_response({"count": 9, "ops": ["/ui"]}))
    out = mc.forward(9872, "query_network", {"parent_path": "/"},
                     opener=opener_returning(body))
    assert out == {"ok": True, "result": {"count": 9, "ops": ["/ui"]}}


def test_plain_json_body_also_parses():
    body = json.dumps(tool_response({"v": 1}))
    out = mc.forward(9872, "op", {}, opener=opener_returning(body))
    assert out == {"ok": True, "result": {"v": 1}}


def test_tool_error_is_ok_false():
    body = sse(tool_response({"error": "no such op"}, is_error=True))
    out = mc.forward(9872, "op", {}, opener=opener_returning(body))
    assert out["ok"] is False
    assert out["error"] == {"error": "no such op"}


def test_jsonrpc_protocol_error_is_ok_false():
    body = sse({"jsonrpc": "2.0", "id": 1,
                "error": {"code": -32601, "message": "method not found"}})
    out = mc.forward(9872, "op", {}, opener=opener_returning(body))
    assert out["ok"] is False
    assert out["error"]["code"] == -32601


def test_transport_failure_returns_none():
    def boom(req, timeout=None):
        raise urllib.error.URLError("connection refused")
    assert mc.forward(9872, "op", {}, opener=boom) is None


def test_os_error_returns_none():
    def boom(req, timeout=None):
        raise OSError("reset")
    assert mc.forward(9872, "op", {}, opener=boom) is None


def test_http_error_is_a_tool_error_not_transport_failure():
    def http_err(req, timeout=None):
        raise urllib.error.HTTPError(
            "http://127.0.0.1:9872/mcp", 500, "boom", {},
            io.BytesIO(json.dumps({"jsonrpc": "2.0", "id": 1,
                                   "error": {"message": "server"}}).encode()))
    out = mc.forward(9872, "op", {}, opener=http_err)
    assert out["ok"] is False


def test_empty_body_is_transport_failure():
    assert mc.forward(9872, "op", {}, opener=opener_returning("")) is None


def test_malformed_result_is_ok_false():
    body = sse({"jsonrpc": "2.0", "id": 1, "result": "not-a-dict"})
    out = mc.forward(9872, "op", {}, opener=opener_returning(body))
    assert out == {"ok": False, "error": "malformed tool response"}


def test_non_numeric_port_is_transport_failure():
    assert mc.forward("nope", "op", {},
                      opener=opener_returning(sse(tool_response({})))) is None


def test_never_raises_on_a_weird_opener():
    def weird(req, timeout=None):
        raise ValueError("surprise")
    assert mc.forward(9872, "op", {}, opener=weird) is None


def test_targets_loopback_and_the_right_operation():
    seen = {}

    def capture(req, timeout=None):
        seen["url"] = req.full_url
        seen["msg"] = json.loads(req.data.decode())
        return FakeResp(sse(tool_response({"ok": 1})))

    mc.forward(9800, "capture_top", {"op_path": "/x"}, opener=capture)
    assert seen["url"] == "http://127.0.0.1:9800/mcp"
    assert seen["msg"]["method"] == "tools/call"
    assert seen["msg"]["params"]["name"] == "capture_top"
    assert seen["msg"]["params"]["arguments"] == {"op_path": "/x"}
