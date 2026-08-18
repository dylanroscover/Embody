"""The runtime-bundle packager's REFUSALS, at build time.

Everything here is about a defect that must be caught while the release
is still buildable. convoy_install refuses a Windows managed runtime
whose daemon interpreter is a console binary -- correctly and
unrepairably, because a managed runtime is a hash-pinned release artifact
and rewriting one of its files would break the inventory it is verified
against. Without the same gate HERE, such a bundle packages cleanly,
ships, and then hard-fails runtime_interpreter_not_windowless on every
user machine, forever, with no route back except a new release.

The crafted PE headers are the point: an actual Windows binary cannot be
borrowed on the macOS leg of this matrix, and the packager's own
platform fence means the win32 build path only ever executes on Windows.
"""

import importlib.util
import json
import os
import pathlib
import sys

import pytest


_HERE = pathlib.Path(__file__).resolve().parent
_BUILDER_PATH = _HERE / "build_convoy_runtime_bundle.py"
_INSTALLER_PATH = (_HERE.parent / "embody" / "Embody" / "convoy"
                   / "convoy_install.py")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# IMPORTED BEHIND THE PLATFORM GATE, not at module scope. Everything in
# this file is skipped off Windows, and a module-scope exec_module would
# still run there -- so a future Windows-only import in the packager (or
# in the installer it loads) would break COLLECTION on the macOS leg, in
# a file that has nothing to say on that platform anyway.
_LOADED = {}


def _modules():
    if not _LOADED:
        _LOADED["builder"] = _load("convoy_runtime_bundle_builder_test",
                                   _BUILDER_PATH)
        _LOADED["installer"] = _load("convoy_install_for_bundle_test",
                                     _INSTALLER_PATH)
    return _LOADED["builder"], _LOADED["installer"]


@pytest.fixture()
def modules():
    return _modules()


RUNTIME_ID = "cpython-3.11.15-crypto-test-win64"
PYTHON_REL = "python/pythonw.exe"
PROBE_PYTHON_REL = "python/python.exe"
CRYPTO_REL = "python/site-packages/cryptography/__init__.py"
STDLIB_REL = "python/Lib/os.py"

pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="the packager refuses to build a Windows bundle off Windows, "
           "so its win32 gate can only run there")


def _pe_bytes(subsystem, tail=b""):
    """A PE header carrying one honest fact: its subsystem.

    Same layout the canonical reader parses (convoy_install.pe_subsystem):
    e_lfanew at 0x3C, the PE signature it points at, a 20-byte COFF
    header, then an optional header whose magic says PE32.
    """
    raw = bytearray(0x80 + 96)
    raw[0:2] = b"MZ"
    raw[0x3C:0x40] = (0x80).to_bytes(4, "little")
    raw[0x80:0x84] = b"PE\0\0"
    raw[0x98:0x9A] = (0x010B).to_bytes(2, "little")
    raw[0xDC:0xDE] = subsystem.to_bytes(2, "little")
    return bytes(raw) + tail


def _prepared_root(tmp_path, daemon_subsystem, probe_subsystem):
    root = tmp_path / "prepared"
    payloads = {
        PYTHON_REL: _pe_bytes(daemon_subsystem, b"daemon"),
        PROBE_PYTHON_REL: _pe_bytes(probe_subsystem, b"probe"),
        CRYPTO_REL: b'__version__ = "test"\n',
        STDLIB_REL: b"# stdlib marker\n",
    }
    for relative, value in payloads.items():
        path = root.joinpath(*relative.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value)
    return root


def _probe_runner(installer, root):
    """A capability probe that answers for the prepared tree, never runs it."""
    def run(argv, timeout_s=None):
        target = str(root)
        result = {
            "format": installer.RUNTIME_PROBE_FORMAT,
            "implementation": "CPython",
            "python": [3, 11, 15],
            "platform": "win32",
            "architecture": installer.normalize_architecture(),
            "executable": os.path.realpath(argv[0]),
            "prefix": target,
            "base_prefix": target,
            "stdlib": os.path.join(target, "python", "Lib"),
            "platstdlib": os.path.join(target, "python", "Lib"),
            "cryptography_version": "test",
            "cryptography_file": os.path.join(target,
                                              *CRYPTO_REL.split("/")),
            "x509": True,
            "ed25519": True,
            "tls13": True,
        }
        return 0, json.dumps(result), ""
    return run


def _build(modules, tmp_path, root):
    builder, installer = modules
    return builder.build_bundle(
        str(root), str(tmp_path / "runtime.zip"), RUNTIME_ID, PYTHON_REL,
        PROBE_PYTHON_REL, "win32", installer.normalize_architecture(),
        "test-fixture", installer=installer,
        probe_runner=_probe_runner(installer, root))


def test_a_correctly_split_windows_runtime_packages(modules, tmp_path):
    """The control. GUI daemon interpreter, CONSOLE probe interpreter --
    the split _validate_runtime_manifest enforces by NAME, here proven by
    what the binaries actually are."""
    _builder, installer = modules
    root = _prepared_root(tmp_path, installer.PE_SUBSYSTEM_GUI,
                          installer.PE_SUBSYSTEM_CONSOLE)
    metadata = _build(modules, tmp_path, root)
    assert metadata["runtime_id"] == RUNTIME_ID
    assert metadata["platform"] == "win32"
    assert (tmp_path / "runtime.zip").is_file()


def test_a_console_daemon_interpreter_refuses_to_package(modules, tmp_path):
    """THE GATE. On a user's machine this is unrepairable by design (the
    runtime is hash-pinned), so it has to be caught while the release can
    still be rebuilt -- otherwise it ships and refuses forever."""
    _builder, installer = modules
    root = _prepared_root(tmp_path, installer.PE_SUBSYSTEM_CONSOLE,
                          installer.PE_SUBSYSTEM_CONSOLE)
    with pytest.raises(ValueError) as caught:
        _build(modules, tmp_path, root)
    assert "windowless" in str(caught.value)
    assert not (tmp_path / "runtime.zip").exists(), (
        "a refused build must not leave an archive behind")


def test_an_unreadable_daemon_interpreter_refuses_to_package(modules,
                                                             tmp_path):
    """Not a PE at all -- a stub, a shim, a copied text file. Unknown is
    not 'probably fine' when the answer decides whether a window opens on
    every machine that installs it."""
    _builder, installer = modules
    root = _prepared_root(tmp_path, installer.PE_SUBSYSTEM_GUI,
                          installer.PE_SUBSYSTEM_CONSOLE)
    root.joinpath(*PYTHON_REL.split("/")).write_bytes(b"not a PE at all")
    with pytest.raises(ValueError) as caught:
        _build(modules, tmp_path, root)
    assert "windowless" in str(caught.value)


def test_a_windowless_probe_interpreter_refuses_to_package(modules,
                                                           tmp_path):
    """The other half of the split. The probe binary is driven over pipes
    during provisioning; shipping the GUI one under the name python.exe
    is the same mix-up in the opposite direction."""
    _builder, installer = modules
    root = _prepared_root(tmp_path, installer.PE_SUBSYSTEM_GUI,
                          installer.PE_SUBSYSTEM_GUI)
    with pytest.raises(ValueError) as caught:
        _build(modules, tmp_path, root)
    assert "CONSOLE" in str(caught.value)


def test_the_gate_refuses_before_the_capability_probe_spawns_anything(
        modules, tmp_path):
    """Cheap first: a bundle that cannot ship must not cost a subprocess,
    and the refusal must not depend on the probe having succeeded."""
    builder, installer = modules
    root = _prepared_root(tmp_path, installer.PE_SUBSYSTEM_CONSOLE,
                          installer.PE_SUBSYSTEM_CONSOLE)
    spawned = []

    def exploding_probe(argv, timeout_s=None):
        spawned.append(list(argv))
        raise AssertionError("the probe must not run for a refused bundle")

    with pytest.raises(ValueError):
        builder.build_bundle(
            str(root), str(tmp_path / "runtime.zip"), RUNTIME_ID,
            PYTHON_REL, PROBE_PYTHON_REL, "win32",
            installer.normalize_architecture(), "test-fixture",
            installer=installer, probe_runner=exploding_probe)
    assert spawned == []
