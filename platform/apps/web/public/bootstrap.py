#!/usr/bin/env python3
"""Embody bootstrap: install Embody into a TouchDesigner project file OFFLINE.

For a machine that has TouchDesigner and nothing else (a render node, a
media server, the booth PC). Runs under TouchDesigner's own bundled Python
(or any Python 3.9+; stdlib only) and uses TouchDesigner's own toeexpand /
toecollapse, so nothing is installed on the machine except the Embody COMP
inside the project:

    1. find a TouchDesigner install (newest, or --td)
    2. fetch the latest release tox + manifest, verify its sha256 (or --tox)
    3. expand the target .toe, graft the Embody COMP in at the root, collapse
    4. verify by re-expanding the result, back up the original, replace it
    5. optionally launch TouchDesigner on it (--launch)

Success is judged by filesystem evidence -- the expand dir and its .toc, a
non-empty collapsed file, the grafted entries surviving a re-expand -- never
by the tools' exit codes (toeexpand returns 1 on success). Idea adopted from
td-mcp-rs's project_install_bridge (credited in the README); the mechanics
were re-derived here against 2025.33070 on 2026-09-05.

Usage:
    python embody_bootstrap.py <project.toe> [--launch] [--replace]
                               [--td <TouchDesigner dir>] [--tox <Embody.tox>]
                               [--keep-staging] [--json]
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request
from typing import Optional

MANIFEST_URL = "https://github.com/dylanroscover/Embody/releases/latest/download/embody-release.json"
ASSET_URL = "https://github.com/dylanroscover/Embody/releases/download/{tag}/{asset}"
USER_AGENT = "Embody-Bootstrap"
TOOL_TIMEOUT_S = 300


class BootstrapError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = "embody.bootstrap." + code


# ---------------------------------------------------------------------------
# TouchDesigner install discovery (mirrors envoy_bridge.find_td_installs)
# ---------------------------------------------------------------------------

def _parse_build(name: str):
    m = re.search(r"(\d{4})\.(\d{4,6})", name)
    return (int(m.group(1)), int(m.group(2))) if m else None


def find_td_installs(platform: Optional[str] = None, root: Optional[str] = None) -> list:
    """[(build, install_dir)] newest first. Injectable for tests."""
    platform = platform or sys.platform
    found = []
    if platform.startswith("win"):
        roots = [root] if root else [r"C:\Program Files\Derivative"]
        for base in roots:
            for d in glob.glob(os.path.join(base, "TouchDesigner.*")):
                b = _parse_build(os.path.basename(d))
                if b and os.path.isdir(os.path.join(d, "bin")):
                    found.append((b, d))
    elif platform == "darwin":
        roots = [root] if root else ["/Applications", os.path.expanduser("~/Applications")]
        for base in roots:
            for app in glob.glob(os.path.join(base, "TouchDesigner*.app")):
                plist = os.path.join(app, "Contents", "Info.plist")
                b = None
                try:
                    b = _parse_build(open(plist, encoding="utf-8", errors="ignore").read())
                except OSError:
                    pass
                found.append((b or (0, 0), app))
    else:
        roots = [root] if root else ["/opt/derivative"]
        for base in roots:
            for d in glob.glob(os.path.join(base, "TouchDesigner*")):
                if os.path.isdir(d):
                    found.append((_parse_build(d) or (0, 0), d))
    return sorted(found, key=lambda t: t[0], reverse=True)


def td_tools(install_dir: str, platform: Optional[str] = None) -> dict:
    """Paths to toeexpand, toecollapse, the TD executable and TD's python
    inside an install. Missing entries are None."""
    platform = platform or sys.platform
    if platform.startswith("win"):
        b = os.path.join(install_dir, "bin")
        names = {"toeexpand": "toeexpand.exe", "toecollapse": "toecollapse.exe",
                 "touchdesigner": "TouchDesigner.exe", "python": "python.exe"}
        return {k: (os.path.join(b, v) if os.path.isfile(os.path.join(b, v)) else None)
                for k, v in names.items()}
    if platform == "darwin":
        macos = os.path.join(install_dir, "Contents", "MacOS")
        out = {}
        for k, v in (("toeexpand", "toeexpand"), ("toecollapse", "toecollapse"), ("python", "python")):
            hits = glob.glob(os.path.join(install_dir, "Contents", "**", v), recursive=True)
            out[k] = hits[0] if hits else None
        td = None
        try:
            for name in sorted(os.listdir(macos)):
                if os.path.isfile(os.path.join(macos, name)) and "touch" in name.lower():
                    td = os.path.join(macos, name)
                    break
        except OSError:
            pass
        out["touchdesigner"] = td
        return out
    b = os.path.join(install_dir, "bin")
    return {k: (os.path.join(b, k) if os.path.isfile(os.path.join(b, k)) else None)
            for k in ("toeexpand", "toecollapse", "python")} | {
        "touchdesigner": os.path.join(b, "TouchDesigner") if os.path.isfile(os.path.join(b, "TouchDesigner")) else None}


# ---------------------------------------------------------------------------
# Release download
# ---------------------------------------------------------------------------

def fetch_release(dest_dir: str, fetch=None) -> dict:
    """Download the latest release tox, verify sha256. Returns
    {'tox', 'version', 'tag', 'asset'}. `fetch(url)->bytes` is injectable."""
    fetch = fetch or _http_get
    try:
        manifest = json.loads(fetch(MANIFEST_URL).decode("utf-8"))
    except Exception as exc:
        raise BootstrapError("manifest", f"Could not read the release manifest: {exc}")
    for key in ("tag", "asset", "sha256"):
        if not manifest.get(key):
            raise BootstrapError("manifest", f"Release manifest lacks {key!r}")
    data = fetch(ASSET_URL.format(tag=manifest["tag"], asset=manifest["asset"]))
    digest = hashlib.sha256(data).hexdigest()
    if digest != manifest["sha256"]:
        raise BootstrapError("checksum", "Downloaded tox does not match the manifest sha256")
    path = os.path.join(dest_dir, manifest["asset"])
    with open(path, "wb") as f:
        f.write(data)
    return {"tox": path, "version": manifest.get("version"), "tag": manifest["tag"], "asset": manifest["asset"]}


def _http_get(url: str, timeout: int = 120) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


# ---------------------------------------------------------------------------
# Expand-dir format: the .toc index and the graft
# ---------------------------------------------------------------------------

def read_toc(path: str) -> list:
    """Entries of a .toc (strict LF, no BOM), header/blank lines dropped."""
    raw = open(path, "rb").read()
    if raw.startswith(b"\xef\xbb\xbf"):
        raise BootstrapError("toc", f"{path}: UTF-8 BOM in a .toc")
    if b"\r" in raw:
        raise BootstrapError("toc", f"{path}: CR in a .toc (must be LF-only)")
    return [l for l in raw.decode("utf-8").split("\n") if l and not l.startswith("#")]


def write_toc(path: str, entries: list) -> None:
    with open(path, "wb") as f:
        f.write(("\n".join(entries) + "\n").encode("utf-8"))


def tox_root_name(tox_entries: list) -> str:
    """The COMP a .tox expansion is rooted at: its first '<name>.n' entry."""
    for e in tox_entries:
        if e.endswith(".n") and "/" not in e:
            return e[:-2]
    raise BootstrapError("tox", "The tox expansion has no root COMP entry")


def entries_of(name: str, entries: list) -> list:
    """Every entry that belongs to root COMP `name`."""
    return [e for e in entries if e == name + ".n" or e.startswith(name + ".") or e.startswith(name + "/")]


def plan_graft(project_entries: list, tox_entries: list, replace: bool = False) -> dict:
    """Pure: what a graft would do. {'root', 'add', 'remove', 'existing'}."""
    root = tox_root_name(tox_entries)
    add = [e for e in tox_entries if e != ".build"]
    existing = entries_of(root, project_entries)
    if existing and not replace:
        raise BootstrapError(
            "already_installed",
            f"'{root}' already exists at the project root ({len(existing)} entries). "
            "Pass --replace to overwrite it with this release.")
    return {"root": root, "add": add, "remove": existing if replace else [], "existing": bool(existing)}


def apply_graft(project_dir: str, project_toc: str, tox_dir: str, tox_toc: str, replace: bool = False) -> dict:
    """Copy the tox expansion's files into the project expansion and append
    its entries to the project's .toc. Returns the plan plus counts."""
    p_entries = read_toc(project_toc)
    t_entries = read_toc(tox_toc)
    plan = plan_graft(p_entries, t_entries, replace=replace)
    root = plan["root"]
    if plan["remove"]:
        for e in plan["remove"]:
            fp = os.path.join(project_dir, *e.split("/"))
            if os.path.isfile(fp):
                os.remove(fp)
        shutil.rmtree(os.path.join(project_dir, root), ignore_errors=True)
        p_entries = [e for e in p_entries if e not in set(plan["remove"])]
    copied = 0
    for e in plan["add"]:
        src = os.path.join(tox_dir, *e.split("/"))
        dst = os.path.join(project_dir, *e.split("/"))
        if not os.path.isfile(src):
            raise BootstrapError("tox", f"tox expansion is missing {e}")
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(src, dst)
        copied += 1
    write_toc(project_toc, p_entries + plan["add"])
    plan["copied"] = copied
    return plan


# ---------------------------------------------------------------------------
# First-run provisioning: answer the setup wizard without a human
# ---------------------------------------------------------------------------
#
# A freshly installed Embody opens its setup wizard and keeps Envoy disabled
# until someone clicks (observed 2026-07-29 by the smoke harness). A render
# node has no someone. So the graft also adds a root-level Execute DAT whose
# onStart waits for Embody's extension, closes the wizard window, applies the
# wizard's own backend (_applyWizardSetup -- the same call its Finish button
# makes), saves the project and destroys itself. The result is a project that
# opens configured, Envoy up, no dialog, on every later start.
#
# Expanded .text framing (derived from toeexpand output, 2025.33070): the
# line "2", then "*", five big-endian uint32 (1,1,1,1,2), a big-endian uint32
# byte length, then the text bytes.

TEXT_HEADER = b"2\n*" + struct.pack(">5I", 1, 1, 1, 1, 2)
PROVISION_NAME = "embody_provision"
ASSISTANTS = ("claudecode", "opencode", "codex", "gemini", "vscode", "cursor",
              "windsurf", "copilot", "antigravity", "none")

PROVISION_SCRIPT = """# Embody bootstrap provisioning -- runs ONCE on the first open of this
# project, applies the setup wizard's choices through the wizard's own
# backend, saves, and removes itself. Written by embody_bootstrap.py.

ASSISTANT = {assistant!r}
CLIENT = {client!r}


def onStart():
    run("args[0](0)", _provision, delayFrames=60)
    return


def _embody():
    emb = op('/Embody')
    if emb is None or not emb.extensionsReady:
        return None
    try:
        emb.ext.Embody
    except Exception:
        return None
    return emb


def _provision(attempt):
    emb = _embody()
    if emb is None:
        if attempt < 20:
            run("args[0](args[1])", _provision, attempt + 1, delayFrames=30)
        return
    if not emb.fetch('embody_provisioned', False):
        try:
            win = emb.op('window_wizard')
            if win is not None:
                win.par.winclose.pulse()
        except Exception:
            pass
        try:
            emb.ext.Embody._applyWizardSetup(
                mode='auto', assistant=ASSISTANT, client=CLIENT,
                root='projectfolder', custom_root='', permissions='all',
                git='gitskip', externalize='skip')
            emb.store('embody_provisioned', True)
            debug('embody_provision: setup applied (%s)' % ASSISTANT)
        except Exception as e:
            debug('embody_provision: setup failed: %s' % e)
    run("args[0]()", _close_again, delayFrames=95)
    run("args[0]()", _finish, delayFrames=240)


def _close_again():
    emb = op('/Embody')
    try:
        win = emb.op('window_wizard') if emb else None
        if win is not None:
            win.par.winclose.pulse()
    except Exception:
        pass


def _finish():
    # Save with the provisioning DAT already gone, so the saved project
    # opens configured and quiet from now on.
    run("project.save()", delayFrames=2)
    me.destroy()
"""


def encode_text(body: bytes) -> bytes:
    return TEXT_HEADER + struct.pack(">I", len(body)) + body


def decode_text(blob: bytes) -> bytes:
    if not blob.startswith(TEXT_HEADER):
        raise BootstrapError("text", "unexpected .text framing")
    n = struct.unpack(">I", blob[len(TEXT_HEADER):len(TEXT_HEADER) + 4])[0]
    body = blob[len(TEXT_HEADER) + 4:]
    if len(body) != n:
        raise BootstrapError("text", f".text length {len(body)} != declared {n}")
    return body


def assistant_choice(assistant: str) -> tuple:
    """(assistant, client) as _applyWizardSetup wants them."""
    a = (assistant or "claudecode").lower()
    if a not in ASSISTANTS:
        raise BootstrapError("assistant", f"assistant must be one of {', '.join(ASSISTANTS)}")
    if a in ("claudecode", "none"):
        return a, ""
    return "other", a


def provisioning_files(assistant: str = "claudecode", name: str = PROVISION_NAME) -> dict:
    """{relative entry: bytes} for a root-level Execute DAT that provisions
    Embody on first open. Pure."""
    a, client = assistant_choice(assistant)
    body = PROVISION_SCRIPT.format(assistant=a, client=client).encode("utf-8")
    n = ("DAT:execute\ntile -600 -600 130 90\nflags =  display on parlanguage 0\n"
         "color 0.9 0.3 0.4 \nend\n").encode("utf-8")
    parm = ("?\nstart 0 on\ncreate 0 on\nexit 0 on\nprojectpresave 0 on\n"
            "projectpostsave 0 on\ndefaultreadencoding 0 cp1252\n?\n").encode("utf-8")
    return {f"{name}.n": n, f"{name}.parm": parm, f"{name}.text": encode_text(body)}


def apply_provisioning(project_dir: str, project_toc: str, assistant: str = "claudecode",
                       name: str = PROVISION_NAME) -> list:
    """Write the provisioning DAT into the expansion and index it. Returns
    the entries added. Refuses if an op of that name already exists."""
    entries = read_toc(project_toc)
    if entries_of(name, entries):
        raise BootstrapError("provision", f"'{name}' already exists at the project root")
    files = provisioning_files(assistant, name)
    for rel, data in files.items():
        with open(os.path.join(project_dir, rel), "wb") as f:
            f.write(data)
    added = list(files.keys())
    write_toc(project_toc, entries + added)
    return added


# ---------------------------------------------------------------------------
# The official tools, judged by artifacts
# ---------------------------------------------------------------------------

def _run_tool(exe: str, arg: str, cwd: str) -> None:
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        subprocess.run([exe, arg], cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       stdin=subprocess.DEVNULL, timeout=TOOL_TIMEOUT_S, **kwargs)
    except subprocess.TimeoutExpired:
        raise BootstrapError("tool_timeout", f"{os.path.basename(exe)} {arg} did not finish in {TOOL_TIMEOUT_S}s")
    except OSError as exc:
        raise BootstrapError("tool", f"Could not run {exe}: {exc}")


def expand(toeexpand: str, packed: str) -> tuple:
    """Expand a .toe/.tox beside itself. Returns (dir, toc); raises when the
    artifacts did not appear (the exit code says nothing)."""
    cwd, name = os.path.split(os.path.abspath(packed))
    _run_tool(toeexpand, name, cwd)
    d, toc = os.path.join(cwd, name + ".dir"), os.path.join(cwd, name + ".toc")
    if not (os.path.isdir(d) and os.path.isfile(toc)):
        raise BootstrapError("expand", f"toeexpand produced no expansion for {packed}")
    return d, toc


def collapse(toecollapse: str, packed: str) -> str:
    """Collapse <packed>.dir + <packed>.toc back into <packed>; the file must
    exist and be non-empty afterwards."""
    cwd, name = os.path.split(os.path.abspath(packed))
    _run_tool(toecollapse, name, cwd)
    if not (os.path.isfile(packed) and os.path.getsize(packed) > 0):
        raise BootstrapError("collapse", f"toecollapse produced no file at {packed}")
    return packed


# ---------------------------------------------------------------------------
# The whole job
# ---------------------------------------------------------------------------

def bootstrap(target: str, tools: dict, tox: Optional[str] = None, replace: bool = False,
              keep_staging: bool = False, fetch=None, log=print,
              assistant: str = "claudecode", provision: bool = True) -> dict:
    target = os.path.abspath(target)
    if not os.path.isfile(target):
        raise BootstrapError("target", f"No such project file: {target}")
    for k in ("toeexpand", "toecollapse"):
        if not tools.get(k):
            raise BootstrapError("tools", f"TouchDesigner's {k} was not found; pass --td <install dir>")
    staging = tempfile.mkdtemp(prefix="embody_bootstrap_")
    summary = {"target": target, "staging": staging}
    try:
        if tox:
            tox_path = os.path.join(staging, "Embody.tox")
            shutil.copyfile(tox, tox_path)
            summary["release"] = {"tox": tox, "version": None}
        else:
            log("Fetching the latest Embody release...")
            rel = fetch_release(staging, fetch=fetch)
            tox_path = os.path.join(staging, "Embody.tox")
            shutil.move(rel["tox"], tox_path)
            summary["release"] = rel
            log(f"  {rel['asset']} ({rel['tag']}) verified")

        work = os.path.join(staging, "project.toe")
        shutil.copyfile(target, work)
        log("Expanding the project and the release tox...")
        p_dir, p_toc = expand(tools["toeexpand"], work)
        t_dir, t_toc = expand(tools["toeexpand"], tox_path)

        log("Grafting Embody into the project root...")
        plan = apply_graft(p_dir, p_toc, t_dir, t_toc, replace=replace)
        summary["graft"] = {"root": plan["root"], "entries": len(plan["add"]),
                            "replaced": bool(plan["remove"])}
        expected = list(plan["add"])
        if provision:
            log(f"Adding first-run provisioning (assistant: {assistant})...")
            added = apply_provisioning(p_dir, p_toc, assistant=assistant)
            expected += added
            summary["provisioning"] = {"dat": PROVISION_NAME, "assistant": assistant}

        log("Collapsing...")
        collapse(tools["toecollapse"], work)

        log("Verifying by re-expanding the result...")
        vdir = os.path.join(staging, "verify")
        os.makedirs(vdir)
        vfile = os.path.join(vdir, "project.toe")
        shutil.copyfile(work, vfile)
        _v_dir, v_toc = expand(tools["toeexpand"], vfile)
        got = set(read_toc(v_toc))
        missing = [e for e in expected if e not in got]
        if missing:
            raise BootstrapError("verify", f"{len(missing)} grafted entries did not survive the collapse "
                                           f"(first: {missing[0]})")
        summary["verified_entries"] = len(expected)

        backup = f"{target}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
        shutil.copy2(target, backup)
        os.replace(work, target)
        summary["backup"] = backup
        summary["ok"] = True
        log(f"Installed: {target}  (backup: {backup})")
        return summary
    finally:
        if keep_staging:
            summary["staging_kept"] = staging
        else:
            shutil.rmtree(staging, ignore_errors=True)


def launch(touchdesigner: str, target: str) -> int:
    """Start TouchDesigner on the project, detached. Returns the pid."""
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = (getattr(subprocess, "DETACHED_PROCESS", 0)
                                   | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    if sys.platform == "darwin" and touchdesigner.endswith(".app"):
        proc = subprocess.Popen(["open", "-a", touchdesigner, target])
    else:
        proc = subprocess.Popen([touchdesigner, target], cwd=os.path.dirname(target),
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, **kwargs)
    return proc.pid


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Install Embody into a TouchDesigner project file, offline.")
    ap.add_argument("target", help="the .toe to install Embody into")
    ap.add_argument("--td", help="TouchDesigner install dir (default: the newest found)")
    ap.add_argument("--tox", help="a local Embody release .tox instead of downloading")
    ap.add_argument("--replace", action="store_true", help="overwrite an Embody already at the root")
    ap.add_argument("--assistant", default="claudecode", choices=ASSISTANTS,
                    help="which AI client the first-run setup configures (none = Envoy disabled)")
    ap.add_argument("--no-provision", action="store_true",
                    help="skip the first-run provisioning DAT (the setup wizard will show)")
    ap.add_argument("--launch", action="store_true", help="start TouchDesigner on the project afterwards")
    ap.add_argument("--keep-staging", action="store_true", help="leave the staging dir for inspection")
    ap.add_argument("--json", action="store_true", help="print a JSON summary on stdout")
    a = ap.parse_args(argv)
    log = (lambda *x: print(*x, file=sys.stderr)) if a.json else print
    try:
        if a.td:
            install = a.td
        else:
            installs = find_td_installs()
            if not installs:
                raise BootstrapError("tools", "No TouchDesigner install found; pass --td <install dir>")
            install = installs[0][1]
            log(f"TouchDesigner: {install}")
        tools = td_tools(install)
        summary = bootstrap(a.target, tools, tox=a.tox, replace=a.replace,
                            keep_staging=a.keep_staging, log=log,
                            assistant=a.assistant, provision=not a.no_provision)
        if a.launch:
            if not tools.get("touchdesigner"):
                raise BootstrapError("tools", "TouchDesigner executable not found for --launch")
            summary["launched_pid"] = launch(tools["touchdesigner"], summary["target"])
            log(f"Launched TouchDesigner (pid {summary['launched_pid']}) on {summary['target']}")
        if a.json:
            print(json.dumps(summary, indent=1))
        return 0
    except BootstrapError as exc:
        out = {"ok": False, "error": str(exc), "error_code": exc.code}
        if a.json:
            print(json.dumps(out))
        else:
            print(f"ERROR [{exc.code}] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
