"""The find-the-host-app + fallback contract (12.3). Pure, injected I/O."""

import json
import urllib.error

import pytest

import convoy_hostprobe as hp


def _live_reader(port=8080, pid=4242, host_id="h" * 32):
    return lambda d: {"port": port, "pid": pid, "host_id": host_id,
                      "protocol": "convoy-host/1"}


# -- the three outcomes --------------------------------------------

def test_running_when_a_live_host_app_is_found():
    r = hp.probe(data_dir="/x",
                 portfile_reader=_live_reader(host_id="h" * 32),
                 token_reader=lambda d: "tok",
                 health_check=lambda handle: "h" * 32)  # confirms identity
    assert r.status == hp.STATUS_RUNNING
    assert r.use_convoy is True
    assert r.handle.port == 8080
    assert r.handle.base_url == "http://127.0.0.1:8080"
    assert r.handle.token == "tok"


def test_absent_when_no_portfile_at_all(monkeypatch):
    monkeypatch.setattr(hp.platform_mod, "read_portfile", lambda d: None)
    r = hp.probe(data_dir="/x", portfile_reader=lambda d: None,
                 token_reader=lambda d: "tok")
    assert r.status == hp.STATUS_ABSENT
    assert r.use_convoy is False, "Convoy off falls back to direct Envoy"


def test_stale_when_a_portfile_exists_but_its_writer_is_dead(monkeypatch):
    """read_live_portfile returns None for a dead writer; the raw file is
    still there, so this is STALE, not ABSENT -- distinguished for logs,
    same fallback action."""
    monkeypatch.setattr(hp.platform_mod, "read_portfile",
                        lambda d: {"port": 1, "pid": 999999})
    r = hp.probe(data_dir="/x", portfile_reader=lambda d: None,
                 token_reader=lambda d: "tok")
    assert r.status == hp.STATUS_STALE
    assert r.use_convoy is False
    assert "stale" in r.detail


def test_a_live_host_with_no_readable_token_falls_back():
    """Sending unauthenticated traffic would just 401 -- fall back
    instead of guaranteeing a failed call."""
    r = hp.probe(data_dir="/x", portfile_reader=_live_reader(),
                 token_reader=lambda d: None,
                 health_check=lambda handle: "h" * 32)
    assert r.status == hp.STATUS_ABSENT
    assert r.use_convoy is False


def test_probe_never_raises_on_a_corrupt_portfile(monkeypatch):
    """A corrupt portfile makes read_live_portfile return None; the raw
    read also None -> ABSENT, never an exception."""
    monkeypatch.setattr(hp.platform_mod, "read_portfile", lambda d: None)
    r = hp.probe(data_dir="/x", portfile_reader=lambda d: None,
                 token_reader=lambda d: "tok")
    assert r.status == hp.STATUS_ABSENT


# -- authenticated calls with fallback -----------------------------

def _handle():
    return hp.HostHandle(port=8080, host_id="h", token="tok", data_dir="/x")


def test_host_get_returns_parsed_json():
    class Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return json.dumps({"ok": True, "v": 1}).encode()
    result = hp.host_get(_handle(), "/status",
                         opener=lambda req, timeout: Resp())
    assert result == {"ok": True, "v": 1}


def test_host_get_sends_the_token():
    seen = {}
    class Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b"{}"
    def opener(req, timeout):
        seen["token"] = req.get_header("X-convoy-host-token")
        seen["url"] = req.full_url
        return Resp()
    hp.host_get(_handle(), "/status", opener=opener)
    assert seen["token"] == "tok"
    assert seen["url"] == "http://127.0.0.1:8080/status"


def test_transport_failure_returns_none_so_the_caller_falls_back():
    def opener(req, timeout):
        raise urllib.error.URLError("connection refused")
    assert hp.host_get(_handle(), "/status", opener=opener) is None


def test_a_structured_refusal_is_surfaced_not_swallowed():
    def opener(req, timeout):
        raise urllib.error.HTTPError(
            "u", 401, "Unauthorized", {},
            _FakeBody(json.dumps({"ok": False, "reason": "unauthenticated"})))
    result = hp.host_get(_handle(), "/status", opener=opener)
    assert result == {"ok": False, "reason": "unauthenticated"}


def test_post_sends_a_body():
    seen = {}
    class Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b'{"ok": true}'
    def opener(req, timeout):
        seen["data"] = req.data
        return Resp()
    hp.host_post(_handle(), "/register", {"project_root": "/p"},
                 opener=opener)
    assert json.loads(seen["data"]) == {"project_root": "/p"}


class _FakeBody:
    def __init__(self, text): self._text = text.encode()
    def read(self): return self._text


# -- identity confirmation before the token (pid-reuse defense) -----

def test_a_recycled_port_answering_with_a_different_host_id_is_stale():
    """pid-liveness is not identity: if /health reports a DIFFERENT
    host_id than the portfile claims, it is a recycled port -- fall back,
    do NOT send the token."""
    r = hp.probe(data_dir="/x",
                 portfile_reader=_live_reader(host_id="expected" + "0" * 24),
                 token_reader=lambda d: "tok",
                 health_check=lambda handle: "someone-else" + "0" * 20)
    assert r.status == hp.STATUS_STALE
    assert r.use_convoy is False


def test_a_port_that_does_not_answer_health_is_stale():
    r = hp.probe(data_dir="/x",
                 portfile_reader=_live_reader(),
                 token_reader=lambda d: "tok",
                 health_check=lambda handle: None)   # no /health answer
    assert r.status == hp.STATUS_STALE


def test_health_check_confirms_before_token_is_transmitted():
    """The check must run and confirm identity as part of probe()."""
    called = {}
    hp.probe(data_dir="/x",
             portfile_reader=_live_reader(host_id="h" * 32),
             token_reader=lambda d: "tok",
             health_check=lambda handle: called.setdefault("ran", True) or "h" * 32)
    assert called.get("ran") is True
