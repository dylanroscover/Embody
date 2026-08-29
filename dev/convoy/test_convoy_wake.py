"""Cross-platform contracts for the tiny local Perform wake plane."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from queue import Queue
from threading import Event, Thread

import convoy_wake as wake


REPO = Path(__file__).resolve().parents[2]
EXT_PATH = REPO / "dev" / "embody" / "Embody" / "convoy" / "ConvoyExt.py"
EMBODY_EXT_PATH = REPO / "dev" / "embody" / "Embody" / "EmbodyExt.py"


def _ext_class():
    spec = importlib.util.spec_from_file_location("convoy_wake_ext", EXT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module.ConvoyExt


def _module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


TOKEN = "A" * 43


def test_host_packet_and_td_parser_share_the_exact_contract():
    ext = _ext_class()
    packet = wake.build_packet(TOKEN, "acquire", "cj_delivery", ttl_s=90)
    assert ext._parseWakeDatagram(packet, TOKEN, ("127.0.0.1", 50000)) == {
        "action": "acquire", "lease_id": "cj_delivery", "ttl_s": 90}


def test_parser_silently_drops_unauthenticated_nonloopback_and_open_schema():
    ext = _ext_class()
    valid = json.loads(wake.build_packet(
        TOKEN, "touch", "cj_delivery").decode("utf-8"))
    cases = [
        (dict(valid, auth="B" * 43), ("127.0.0.1", 1)),
        (valid, ("10.0.0.2", 1)),
        (dict(valid, surprise=True), ("127.0.0.1", 1)),
        (dict(valid, v=2), ("127.0.0.1", 1)),
        (dict(valid, action="ping"), ("127.0.0.1", 1)),
        (dict(valid, ttl_s=0), ("127.0.0.1", 1)),
    ]
    for body, address in cases:
        packet = json.dumps(body).encode("utf-8")
        assert ext._parseWakeDatagram(packet, TOKEN, address) is None
    assert ext._parseWakeDatagram(
        b"x" * (ext._WAKE_PACKET_MAX + 1), TOKEN,
        ("127.0.0.1", 1)) is None


def test_real_loopback_datagram_reaches_the_thread_parser():
    ext = _ext_class()
    commands = Queue(maxsize=4)
    stop = Event()
    ready = Event()
    state = {"port": None, "error": "", "stopped": False}
    thread = Thread(target=ext._wakeListenerLoop,
                    args=(commands, stop, ready, TOKEN, state,
                          ext._WAKE_PACKET_MAX, 0.05), daemon=True)
    thread.start()
    assert ready.wait(2.0), state
    assert isinstance(state["port"], int), state
    result = wake.send(state["port"], TOKEN, "acquire", "cj_live", 75)
    assert result["ok"] is True
    command = commands.get(timeout=2.0)
    assert command["action"] == "acquire"
    assert command["lease_id"] == "cj_live"
    assert command["ttl_s"] == 75
    assert isinstance(command["received_at"], float)
    stop.set()
    thread.join(1.0)
    assert not thread.is_alive()


def test_sender_is_total_bounded_and_never_reflects_the_token():
    for bad in (0, -1, 65536, "47600", True):
        assert wake.send(bad, TOKEN, "acquire", "x")["ok"] is False
    for kwargs in (
            {"token": "short", "action": "acquire", "lease_id": "x"},
            {"token": TOKEN, "action": "ping", "lease_id": "x"},
            {"token": TOKEN, "action": "acquire", "lease_id": ""},
            {"token": TOKEN, "action": "acquire", "lease_id": "x",
             "ttl_s": 601}):
        result = wake.send(47600, **kwargs)
        assert result["ok"] is False

    class ExplodingSocket:
        def settimeout(self, _value):
            pass

        def bind(self, _address):
            pass

        def sendto(self, _packet, _address):
            raise OSError("failure mentions " + TOKEN)

        def close(self):
            pass

    result = wake.send(
        47600, TOKEN, "release", "cj_x",
        socket_factory=lambda *_args: ExplodingSocket())
    assert result["ok"] is False
    assert TOKEN not in result["detail"]
    assert "[redacted]" in result["detail"]


def test_main_thread_lease_poll_wakes_and_zero_grace_resuspends():
    module = _module(EXT_PATH, "convoy_wake_lease_ext")
    ext = module.ConvoyExt.__new__(module.ConvoyExt)
    commands = Queue(maxsize=8)
    stop = Event()
    ext._wake_record = {"queue": commands, "shutdown": stop}
    ext._wake_poll_gen = 7
    session = {"wake_leases": {}}
    active = {"value": False, "begin": 0, "end": 0}

    class EmbodyExtension:
        @property
        def _convoyWakeActive(self):
            return active["value"]

        def _beginConvoyWake(self):
            active["begin"] += 1
            active["value"] = True
            return True

        def _endConvoyWake(self):
            active["end"] += 1
            active["value"] = False

    embody = type("Embody", (), {
        "ext": type("Ext", (), {"Embody": EmbodyExtension()})()})()
    owner_ext = type("OwnerExt", (), {})()
    parent = type("Parent", (), {"Embody": embody})()
    owner = type("Owner", (), {"parent": parent, "ext": owner_ext})()
    owner_ext.ConvoyExt = ext
    ext.ownerComp = owner
    ext._session = lambda: session
    ext._remoteWakeEnabled = lambda: True
    ext._performRequested = lambda: True
    ext._wakeGrace = lambda: 0
    ext._log = lambda *_args: None
    ext._reconcile = lambda **_kwargs: None
    scheduled = []
    module.run = lambda *args, **kwargs: scheduled.append((args, kwargs))

    commands.put({"action": "acquire", "lease_id": "cj_1",
                  "ttl_s": 120, "received_at": 1.0})
    ext._pollWakeCommands(7)
    assert active == {"value": True, "begin": 1, "end": 0}
    assert "cj_1" in session["wake_leases"]
    assert scheduled, "the bounded main-thread watchdog must continue"

    commands.put({"action": "release", "lease_id": "cj_1",
                  "ttl_s": 120, "received_at": 2.0})
    ext._pollWakeCommands(7)
    assert active == {"value": False, "begin": 1, "end": 1}
    assert session["wake_leases"] == {}


def test_embody_effective_perform_override_never_changes_the_user_par():
    module = _module(EMBODY_EXT_PATH, "convoy_wake_embody_ext")
    ext = module.EmbodyExt.__new__(module.EmbodyExt)

    class Par:
        def __init__(self):
            self.value = 1

        def eval(self):
            return self.value

    perform = Par()
    ext.my = type("Embody", (), {
        "path": "/project1/Embody",
        "par": type("Pars", (), {"Performmode": perform})(),
    })()
    registry = getattr(module.sys, "_embody_convoy_wake_states", {})
    registry.pop(ext.my.path, None)
    module.sys._embody_convoy_wake_states = registry

    assert ext._performModeRequested is True
    assert ext._performMode is True
    assert ext._envoyPerformMode is True
    ext._convoyWakeState()["active"] = True
    assert ext._convoyWakeActive is True
    assert ext._performMode is True, "unrelated Embody guards stay suspended"
    assert ext._envoyPerformMode is False, "only Envoy may resume"
    assert perform.eval() == 1, "wake must preserve the Par/expression value"
    ext._convoyWakeState()["active"] = False
    assert ext._performMode is True
    assert ext._envoyPerformMode is True
