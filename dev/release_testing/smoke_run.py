"""
Fresh-install smoke orchestrator -- the outer half of the release smoke.

The inner half (smoke_bootstrap.py, inside the template .toe) drops the
release .tox into a virgin project, drives the setup headlessly and writes
two verdict files. This script owns everything a person used to do by hand
around it, the same way on Windows and macOS:

    python dev/release_testing/smoke_run.py [--tox PATH] [--td PATH]
                                            [--out DIR] [--timeout S]

  1. pick the build from release/embody-release.json and verify its sha256
     (never a directory glob: 'Embody-v6.2.9.tox' sorts after 'v6.2.56');
  2. stage a FRESH run directory outside the repo -- template, bootstrap
     and a `smoke_run.json` sidecar (repo, tox, flags dir, run id);
  3. launch TouchDesigner on the template by explicit absolute path;
  4. wait for ready.flag, then features.flag (PENDING = keep waiting);
  5. probe the MCP server over HTTP: handshake, tools/list, one
     create / set / query / errors / delete round-trip;
  6. quit the TD it launched -- and only that one: the pid must be the
     recorded one AND its command line must name the run directory;
  7. write result.json beside the flags and print a one-screen summary.

Exit codes: 0 every verdict PASS; 1 a verdict failed; 2 the smoke could not
run (no TD, bad manifest, no flag by the deadline). 2 is deliberately not 0:
a run that never happened must never read as green.

Nothing here deletes anything. Every run gets its own directory and old
ones are left for the operator (see .claude/rules on recursive deletes).

Why a sidecar and not EMBODY_SMOKE_REPO: on macOS `open` launches through
LaunchServices, which does not pass the shell's environment on, and the
bootstrap's failure mode for a missing repo root is to return before
scheduling anything -- no flag, no error. A file beside the template is
always found. The env var still works for a hand-run (see the bootstrap).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(DEFAULT_REPO, 'dev', 'embody'))
sys.path.insert(0, HERE)
import envoy_bridge  # noqa: E402  (stdlib-only; TD discovery, pids, quit)
import smoke_legs  # noqa: E402  (the extra legs: upgrade, faults, uninstall)

# The bootstrap self-polls Envoy for up to 240 x 1 s before it writes
# ready.flag, on top of TD's own boot; the Convoy leg then polls for up to
# ~3 min. These are ceilings for a wedged run, not expected durations.
READY_TIMEOUT_S = 420
FEATURES_TIMEOUT_S = 360
MCP_TIMEOUT_S = 90
QUIT_TIMEOUT_S = 30
FEATURE_LEGS = ('embody_core', 'tdn_roundtrip', 'autosave_checkpoint',
                'portable_export', 'viz_status', 'envoy_config', 'convoy')
HARNESS_FILES = ('smoke_template.toe', 'smoke_bootstrap.py')


class SmokeSetupError(Exception):
    """The smoke could not be run at all (exit code 2)."""


def _norm(path):
    """Canonical form for containment tests: resolved, and case-folded on
    the platforms whose default volumes are case-insensitive (normcase is
    the identity on macOS, but APFS is not)."""
    p = os.path.normcase(os.path.realpath(path))
    return p.lower() if sys.platform in ('win32', 'darwin') else p


def _inside(path, root):
    path, root = _norm(path), _norm(root)
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _git_checkout_above(path):
    """The nearest ancestor (or `path` itself) holding a .git, else None:
    a smoke rooted in ANY checkout makes that checkout its git root and
    deploys Envoy's config there."""
    cur = os.path.realpath(path)
    while True:
        if os.path.exists(os.path.join(cur, '.git')):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        cur = parent


# ---------------------------------------------------------------------------
# 1. build selection
# ---------------------------------------------------------------------------

def _sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def select_build(repo, tox=None):
    """The .tox to smoke, verified against release/embody-release.json.

    The manifest is what the self-updater installs from, so its `asset` is
    the release. A hash mismatch is refused outright: smoking a stale or
    half-written .tox proves nothing about the build being shipped.
    `tox` overrides the asset path but is still hashed and recorded.
    """
    release_dir = os.path.join(repo, 'release')
    manifest_path = os.path.join(release_dir, 'embody-release.json')
    manifest = {}
    if os.path.isfile(manifest_path):
        try:
            with open(manifest_path, 'r', encoding='utf-8') as f:
                manifest = json.load(f)
        except (OSError, ValueError) as e:
            raise SmokeSetupError(f'unreadable release manifest: {e}')
    if tox:
        path = os.path.abspath(tox)
    else:
        asset = manifest.get('asset')
        if not asset:
            raise SmokeSetupError(
                f'no release manifest at {manifest_path} -- project.save() '
                f'in the dev .toe writes it beside the release .tox')
        path = os.path.join(release_dir, asset)
    if not os.path.isfile(path):
        raise SmokeSetupError(f'release .tox not found: {path}')
    digest = _sha256(path)
    size = os.path.getsize(path)
    if not tox:
        # The manifest is the release: an asset it names without a hash
        # cannot be verified, and an unverified .tox must not read green.
        if not manifest.get('sha256') or not manifest.get('size'):
            raise SmokeSetupError(
                f'{manifest_path} names {os.path.basename(path)} but carries '
                f'no sha256/size -- re-save the dev project to rewrite it')
        if manifest['sha256'] != digest:
            raise SmokeSetupError(
                f'{os.path.basename(path)} sha256 {digest[:12]}... does not '
                f'match the manifest ({manifest["sha256"][:12]}...) -- stale '
                f'or corrupt release asset; re-save the dev project')
        if int(manifest['size']) != size:
            raise SmokeSetupError(
                f'{os.path.basename(path)} is {size} bytes, manifest says '
                f'{manifest["size"]}')
    return {'tox': path, 'version': str(manifest.get('version') or ''),
            'sha256': digest, 'size': size,
            'td_build': manifest.get('td_build')}


def previous_release(repo, current_version, dest_dir, tag=None, run=None):
    """The previous release's .tox, extracted from git history into
    `dest_dir`, sha256-verified against the manifest committed at that tag.

    The upgrade leg installs THIS build first and then updates to the
    current one, so the old side of the test needs no network: every
    shipped .tox and its manifest are in the repo's history. `tag`
    overrides the choice (default: the newest v* tag that is not the
    current version).
    """
    run = run or subprocess.run

    def git(*args, **kw):
        return run(['git', '-C', repo] + list(args), check=True,
                   capture_output=True, **kw)

    if not tag:
        tags = git('tag', '--sort=-creatordate', '--list', 'v*',
                   text=True).stdout.split()
        tags = [t for t in tags if t.lstrip('v') != str(current_version)]
        if not tags:
            raise SmokeSetupError('no previous release tag in this checkout')
        tag = tags[0]
    try:
        manifest = json.loads(
            git('show', f'{tag}:release/embody-release.json', text=True).stdout)
    except (subprocess.CalledProcessError, ValueError) as e:
        raise SmokeSetupError(f'{tag} has no readable release manifest: {e}')
    asset = manifest.get('asset')
    if not asset or not manifest.get('sha256'):
        raise SmokeSetupError(f'{tag}: manifest names no verifiable asset')
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, asset)
    with open(dest, 'wb') as f:
        run(['git', '-C', repo, 'show', f'{tag}:release/{asset}'],
            check=True, stdout=f)
    digest = _sha256(dest)
    if digest != manifest['sha256']:
        raise SmokeSetupError(
            f'{asset} from {tag} hashes {digest[:12]}..., manifest says '
            f'{manifest["sha256"][:12]}...')
    return {'tox': dest, 'version': str(manifest.get('version') or ''),
            'tag': tag, 'sha256': digest}


# ---------------------------------------------------------------------------
# 2. staging
# ---------------------------------------------------------------------------

def stage_run(repo, build, out, platform=None, now=None, install_tox=None):
    """A fresh run directory under `out`: harness files + sidecar.

    `install_tox` overrides the .tox the smoke TD boots (the upgrade leg
    starts on the previous release); the sidecar's tox_path is what the
    bootstrap loads, `build` stays the release under test.

    Refuses an `out` inside the repo: a smoke rooted in the repo makes the
    repo its git root, deploys its AI config there and, on 2026-07-27,
    stripped 16 committed files. Never reuses a directory (a leftover
    smoke_template.N.toe from a saved run gets opened instead of the
    template) and never removes one.
    """
    platform = platform or sys.platform
    now = now or time.time
    if _inside(out, repo):
        raise SmokeSetupError(
            f'refusing to stage inside the repo ({out}); pass --out outside '
            f'{repo}')
    checkout = _git_checkout_above(out) if os.path.exists(out) else \
        _git_checkout_above(os.path.dirname(os.path.abspath(out)))
    if checkout:
        raise SmokeSetupError(
            f'refusing to stage inside a git checkout ({checkout}); the smoke '
            f'would deploy its AI config there')
    stamp = time.strftime('%Y%m%d-%H%M%S', time.localtime(now()))
    base = os.path.join(os.path.abspath(out), 'embody-smoke')
    run_id = f'{platform}-{stamp}'
    run_dir = os.path.join(base, run_id)
    n = 1
    while os.path.exists(run_dir):
        n += 1
        run_id = f'{platform}-{stamp}-{n}'
        run_dir = os.path.join(base, run_id)
    os.makedirs(run_dir)
    src_dir = os.path.join(repo, 'dev', 'release_testing')
    for name in HARNESS_FILES:
        src = os.path.join(src_dir, name)
        if not os.path.isfile(src):
            raise SmokeSetupError(f'harness file missing: {src}')
        shutil.copyfile(src, os.path.join(run_dir, name))
    sidecar = {'run_id': run_id, 'platform': platform,
               'repo_root': repo, 'tox_path': install_tox or build['tox'],
               'flags_dir': run_dir, 'started_at': time.strftime(
                   '%Y-%m-%dT%H:%M:%S', time.localtime(now()))}
    with open(os.path.join(run_dir, 'smoke_run.json'), 'w',
              encoding='utf-8') as f:
        json.dump(sidecar, f, indent=2)
    return {'dir': run_dir, 'run_id': run_id, 'sidecar': sidecar}


# ---------------------------------------------------------------------------
# 3. TouchDesigner
# ---------------------------------------------------------------------------

def find_td(build, override=None, platform=None):
    """(exe_path, warning). The install matching the manifest's td_build
    when present, else the newest; `override` is taken as given."""
    platform = platform or sys.platform
    if override:
        exists = os.path.exists if platform == 'darwin' else os.path.isfile
        if not exists(override):
            raise SmokeSetupError(f'--td not found: {override}')
        return override, None
    exe, warning = envoy_bridge.select_td_install(build.get('td_build'))
    if not exe:
        raise SmokeSetupError(warning or 'no TouchDesigner install found')
    return exe, warning


def launch_td(td_exe, toe_path, platform=None, popen=None, console=None):
    """Start TD on `toe_path` by explicit absolute path; returns (pid, proc).

    `console` is a file object for TD's stdout/stderr (the run dir's
    td-console.log): on macOS TD prints its textport there, including
    the traceback of an execute-DAT callback that died before the
    bootstrap could log anything itself.

    Both platforms spawn the executable directly and get a real pid with
    the .toe in argv -- the ownership check (owns_process) relies on that
    argv. On macOS the executable is the bundle's own binary
    (Contents/MacOS/<CFBundleExecutable>, resolved from Info.plist), the
    way Convoy's launcher does it: `open -n -a` would hand the document
    over as an Apple Event and leave argv without it, and LaunchServices
    would drop the environment too. Verified on a real Mac 2026-09-18
    (TEC-MBA: window opened, Envoy served, quit via Envoy by argv match).
    """
    platform = platform or sys.platform
    popen = popen or subprocess.Popen
    toe_path = os.path.realpath(toe_path)
    exe = td_exe
    if platform == 'darwin' and (td_exe.endswith('.app')
                                 or os.path.isdir(td_exe)):
        exe = envoy_bridge._macos_app_executable(td_exe)
        if not exe:
            raise SmokeSetupError(
                f'{td_exe} has no executable under Contents/MacOS')
    out = console if console is not None else subprocess.DEVNULL
    proc = popen([exe, toe_path], stdout=out, stderr=subprocess.STDOUT
                 if console is not None else subprocess.DEVNULL)
    return proc.pid, proc


def process_cmdline(pid, platform=None, run=None):
    """The command line of `pid`, or None when it cannot be read.

    POSIX goes through the bridge's helper (`ps -ww`: BSD ps truncates
    the args column when piped, which would cut a long run-dir path out
    of the string before the ownership test sees it).
    """
    platform = platform or sys.platform
    run = run or subprocess.run
    try:
        if platform == 'win32':
            ps = ('Get-CimInstance Win32_Process -Filter "ProcessId=%d" | '
                  'Select-Object -ExpandProperty CommandLine' % int(pid))
            r = run(['powershell', '-NoProfile', '-NonInteractive',
                     '-Command', ps], capture_output=True, text=True,
                    timeout=15)
            out = (r.stdout or '').strip()
        else:
            out = (envoy_bridge._process_cmdline(pid, run=run) or '').strip()
        return out or None
    except Exception:
        return None


def _fold(p):
    return os.path.normcase(p).replace('\\', '/').lower().rstrip('/')


def owns_process(pid, run_dir, cmdline=None, realpath=None, is_td=None):
    """True only if `pid` is a live TouchDesigner AND its command line
    names `run_dir` -- the one TD this run launched. A TD the operator
    opened at the same moment, a reused pid, or any other process whose
    argv happens to hold that path must never be signalled.

    Both spellings of the run dir are accepted: the resolved one and the
    one as given. macOS's temp dir is a symlink (/var -> /private/var)
    and a Windows %TEMP% can be an 8.3 short path, so argv may hold
    either form.
    """
    realpath = realpath or os.path.realpath
    is_td = is_td or envoy_bridge.is_td_process_alive
    if not is_td(pid):
        return False
    text = (cmdline or process_cmdline)(pid)
    if not text:
        return False
    wants = {_fold(realpath(run_dir)), _fold(os.path.abspath(run_dir)),
             _fold(run_dir)}
    have = _fold(text)
    return any(w and w in have for w in wants)


def _mcp_quit(port):
    """Ask the smoke instance's own Envoy to quit TD, deferred out of the
    request stack (the pattern EnvoyExt's Convoy quit uses) so the reply
    returns before the process goes away. project.quit(force=True) skips
    the save prompt a modified project would otherwise block on."""
    _rpc(f'http://127.0.0.1:{int(port)}/mcp',
         {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
          'params': {'protocolVersion': '2025-06-18', 'capabilities': {},
                     'clientInfo': {'name': 'smoke_run', 'version': '1'}}},
         10)
    _rpc(f'http://127.0.0.1:{int(port)}/mcp',
         {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
          'params': {'name': 'execute_python', 'arguments': {
              'code': 'run("project.quit(force=True)", delayFrames=3)\n'
                      'result = "quit scheduled"'}}},
         15)


def quit_smoke_td(pid, run_dir, port=None, alive=None, mcp_quit=None,
                  hard_quit=None, clock=None, sleep=None, is_td=None,
                  reap=None):
    """Quit the TD this run launched -- and only that one.

    Order: refuse unless the pid's command line names the run dir; ask
    Envoy to quit (clean exit, TD's own shutdown hooks run, the Convoy
    node signs off); only then the bridge's pid-scoped quit_td, which
    closes and force-kills after its grace period. `method` says which
    path worked: 'mcp', 'close', or 'forced' -- a forced kill is reported,
    never hidden behind 'ok'.

    `reap(pid)` -> exit code or None: this process is TD's PARENT, so a
    killed TD is a zombie until it is waited on, and the bridge's
    kill(pid, 0) liveness test reports a zombie as alive ("could not be
    terminated" on the first CI run, 2026-09-18). A reaped exit code means
    it is gone.
    """
    alive = alive or envoy_bridge.is_td_process_alive
    reap = reap or (lambda p: None)
    mcp_quit = mcp_quit or _mcp_quit
    hard_quit = hard_quit or (lambda p: envoy_bridge.quit_td(
        p, graceful_timeout=QUIT_TIMEOUT_S))
    clock = clock or time.monotonic
    sleep = sleep or time.sleep
    if pid is None:
        return {'ok': False, 'method': 'none', 'message': 'no pid recorded'}
    if not owns_process(pid, run_dir, is_td=is_td):
        return {'ok': False, 'method': 'refused',
                'message': f'pid {pid} does not name {run_dir} in its '
                           f'command line -- left running, not ours to quit'}
    if port:
        try:
            mcp_quit(port)
            deadline = clock() + QUIT_TIMEOUT_S
            while clock() < deadline:
                if not alive(pid):
                    return {'ok': True, 'method': 'mcp',
                            'message': f'TouchDesigner (PID {pid}) quit via '
                                       f'Envoy'}
                sleep(1)
        except Exception as e:  # server already gone, or refused: fall back
            pass
    ok, message = hard_quit(pid)
    if not ok and reap(pid) is not None:
        ok, message = True, f'{message} -- reaped: it had exited (zombie)'
    method = 'close' if ok and 'gracefully' in message else (
        'forced' if ok else 'failed')
    return {'ok': bool(ok), 'method': method, 'message': message}


# ---------------------------------------------------------------------------
# 4. flags
# ---------------------------------------------------------------------------

def parse_ready(text):
    """ready.flag -> dict. `split('=', 1)`: the problems line itself may
    hold '=' and ';'."""
    out = {}
    for line in text.lstrip('﻿').splitlines():
        if '=' in line:
            k, v = line.split('=', 1)
            out[k.strip()] = v.strip()
    port = None
    status = out.get('envoy_status', '')
    if 'port' in status:
        try:
            port = int(status.rsplit('port', 1)[1].strip().split()[0])
        except (ValueError, IndexError):
            port = None
    out['envoy_port'] = port
    out['verdict'] = out.get('verdict', 'MISSING')
    return out


def check_run_stamp(ready, run_id):
    """The flag's run_id must be this run's: a stamped flag from another
    run (a copied directory, a mis-pointed sidecar) is not evidence."""
    stamped = (ready.get('run_id') or '').strip()
    if stamped and stamped != run_id:
        raise SmokeSetupError(
            f'ready.flag is stamped run_id={stamped}, this run is {run_id}')


def parse_features(text):
    """features.flag -> {legs, terminal, passed, failed}.

    PENDING (the Convoy leg polls to a terminal answer) means not
    terminal; a missing leg means the file is mid-rewrite; SKIP is
    'not reached' and counts as a failure for the run.
    """
    legs = {}
    for line in text.splitlines():
        if '=' not in line:
            continue
        name, rest = line.split('=', 1)
        verdict, _, detail = rest.partition('|')
        legs[name.strip()] = {'verdict': verdict.strip(), 'detail': detail}
    terminal = all(
        leg in legs and legs[leg]['verdict'] not in ('PENDING', '')
        for leg in FEATURE_LEGS)
    failed = [leg for leg in FEATURE_LEGS
              if legs.get(leg, {}).get('verdict') != 'PASS']
    return {'legs': legs, 'terminal': terminal,
            'passed': terminal and not failed, 'failed': failed}


def wait_for(path, timeout, done, poll=2.0, clock=None, sleep=None):
    """Poll `path` until `done(text)` or the deadline.

    Returns (text, ok): the last text read (partial evidence on a
    timeout is still evidence -- a features.flag stuck at PENDING says
    which leg wedged) and whether `done` held. Checks once before
    sleeping, so a zero budget still reads a flag that is already there.
    """
    clock = clock or time.monotonic
    sleep = sleep or time.sleep
    deadline = clock() + timeout
    last = None
    while True:
        if os.path.isfile(path):
            try:
                with open(path, 'r', encoding='utf-8', errors='replace') as f:
                    last = f.read()
            except OSError:
                pass
            if last and done(last):
                return last, True
        if clock() >= deadline:
            return last, False
        sleep(poll)


# ---------------------------------------------------------------------------
# 5. MCP probe
# ---------------------------------------------------------------------------

def _rpc(url, payload, timeout):
    """One JSON-RPC exchange. The reply is the frame whose `id` matches
    the request's -- a server may push notifications (no id) on the same
    stream, and taking the first frame would hand one of those back as
    the tool result (the bug envoy_bridge._route_sse_frames exists for).
    """
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode('utf-8'),
        headers={'Content-Type': 'application/json',
                 'Accept': 'application/json, text/event-stream'},
        method='POST')
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read().decode('utf-8', 'replace')
    return select_reply(body, payload.get('id'))


def select_reply(body, want):
    """The JSON-RPC message with id `want` out of an SSE or plain-JSON
    body; pushed frames (no id, or another id) are skipped."""
    frames = []
    for line in body.splitlines():
        if line.startswith('data:'):
            try:
                frames.append(json.loads(line[5:].strip()))
            except ValueError:
                continue
    if not frames:
        try:
            frames = [json.loads(body)]
        except ValueError:
            return {'error': f'non-JSON reply: {body[:200]!r}'}
    for msg in frames:
        if isinstance(msg, dict) and msg.get('id') == want:
            return msg
    return {'error': f'no reply with id {want} (got {len(frames)} frame(s))'}


def _tool_result(reply):
    """The tool's own JSON out of an MCP tools/call reply. Fails closed:
    a reply with no result, no text content, or unparseable text is an
    error, never an empty success."""
    if not isinstance(reply, dict):
        return {'error': f'non-object reply: {reply!r}'[:300]}
    if 'error' in reply:
        return {'error': reply['error']}
    res = reply.get('result')
    if not isinstance(res, dict):
        return {'error': f'no result in reply: {reply!r}'[:300]}
    if res.get('isError'):
        return {'error': res}
    for block in res.get('content') or []:
        if block.get('type') == 'text':
            try:
                parsed = json.loads(block['text'])
            except ValueError:
                return {'error': f'unparseable tool text: '
                                 f'{block["text"][:200]!r}'}
            if not isinstance(parsed, dict):
                return {'error': f'tool returned {type(parsed).__name__}, '
                                 f'not an object'}
            return parsed
    return {'error': 'tool reply carried no text content'}


def probe_mcp(port, timeout=MCP_TIMEOUT_S, clock=None, sleep=None,
              rpc=None, expect_dir=None):
    """Handshake, tools/list, and one create/set/query/errors/delete
    round-trip against the smoke instance's Envoy.

    Retries the handshake because the feature phase's save reinitialises
    the server underneath the port. With `expect_dir`, refuses to mutate
    anything until the server confirms its project.folder IS the run dir:
    a stale or collided port could otherwise be the developer's live
    project. Every call's socket timeout is clamped to the remaining
    `timeout`, so the probe can never outrun the run's ceiling.
    """
    clock = clock or time.monotonic
    sleep = sleep or time.sleep
    rpc = rpc or _rpc
    url = f'http://127.0.0.1:{int(port)}/mcp'
    steps = []
    deadline = clock() + timeout

    def remaining():
        left = deadline - clock()
        if left <= 0:
            raise RuntimeError(f'MCP probe exceeded its {timeout:.0f}s budget')
        return min(30.0, left)

    def call(name, args):
        reply = rpc(url, {'jsonrpc': '2.0', 'id': len(steps) + 2,
                          'method': 'tools/call',
                          'params': {'name': name, 'arguments': args}},
                    remaining())
        res = _tool_result(reply)
        ok = 'error' not in res
        steps.append({'step': name, 'ok': ok,
                      'detail': (res.get('error') if not ok else '')})
        if not ok:
            raise RuntimeError(f'{name}: {res.get("error")}')
        return res

    result = {'ok': False, 'tools': 0, 'steps': steps, 'error': ''}
    last_err = ''
    while clock() < deadline:
        try:
            rpc(url, {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
                      'params': {'protocolVersion': '2025-06-18',
                                 'capabilities': {},
                                 'clientInfo': {'name': 'smoke_run',
                                                'version': '1'}}},
                min(10.0, max(1.0, deadline - clock())))
            break
        except Exception as e:  # server mid-restart: retry until deadline
            last_err = str(e)
            sleep(2)
    else:
        result['error'] = f'no MCP handshake on port {port}: {last_err}'
        return result
    steps.append({'step': 'initialize', 'ok': True, 'detail': ''})
    probe = '/smoke_mcp_probe'
    created = False
    try:
        listed = rpc(url, {'jsonrpc': '2.0', 'id': 2,
                           'method': 'tools/list', 'params': {}},
                     remaining())
        tools = [t.get('name') for t in
                 (listed.get('result') or {}).get('tools', [])]
        result['tools'] = len(tools)
        steps.append({'step': 'tools/list', 'ok': bool(tools),
                      'detail': '' if tools else 'empty tool list'})
        if not tools:
            raise RuntimeError('tools/list returned no tools')
        if expect_dir:
            who = call('execute_python', {'code': 'result = project.folder'})
            folder = str(who.get('result', ''))
            if _fold(folder) not in {_fold(os.path.realpath(expect_dir)),
                                     _fold(os.path.abspath(expect_dir))}:
                steps.append({'step': 'identity', 'ok': False,
                              'detail': f'port {port} serves {folder!r}, '
                                        f'not the run dir'})
                raise RuntimeError(
                    f'port {port} answers for project.folder={folder!r}, '
                    f'not this run -- refusing to mutate it')
            steps.append({'step': 'identity', 'ok': True, 'detail': ''})
        call('create_op', {'parent_path': '/', 'op_type': 'constantCHOP',
                           'name': probe.strip('/')})
        created = True
        call('set_parameter', {'op_path': probe, 'par_name': 'value0',
                               'value': '1.5'})
        net = call('query_network', {'parent_path': '/'})
        names = json.dumps(net)
        if probe.strip('/') not in names:
            raise RuntimeError('query_network does not list the probe op')
        errs = call('get_op_errors', {'op_path': probe, 'recurse': False})
        if errs.get('errorCount') != 0:  # a missing key is not "no errors"
            raise RuntimeError(f'probe op errors: errorCount='
                               f'{errs.get("errorCount")!r} '
                               f'{errs.get("errors", "")}')
        call('delete_op', {'op_path': probe})
        created = False
        # Close the loop: the delete reply alone does not prove the op is
        # gone (a {'success': False} without an 'error' key would pass).
        after = json.dumps(call('query_network', {'parent_path': '/'}))
        gone = probe.strip('/') not in after
        steps.append({'step': 'verify_delete', 'ok': gone,
                      'detail': '' if gone else 'probe op still listed'})
        if not gone:
            raise RuntimeError('delete_op reported ok but the probe op is '
                               'still listed')
        result['ok'] = True
    except Exception as e:
        result['error'] = str(e)
        if created:  # best effort: never leave the probe op behind
            try:
                rpc(url, {'jsonrpc': '2.0', 'id': 99, 'method': 'tools/call',
                          'params': {'name': 'delete_op',
                                     'arguments': {'op_path': probe}}}, 10)
            except Exception:
                pass
    return result


# ---------------------------------------------------------------------------
# 6. result
# ---------------------------------------------------------------------------

def collect_logs(run_dir, lines=40):
    """{'tail', 'warnings'}: the bootstrap's own mirror log (bootstrap.log
    in the run dir -- Embody's file log only starts after the feature
    phase's save, so on a wedged startup this is the ONLY evidence), then
    EVERY logs/*.log in mtime order. TD rotates the log at that save, so
    the newest file alone holds none of the boot phase, including its
    WARNING lines."""
    logs_dir = os.path.join(run_dir, 'logs')
    files = [os.path.join(run_dir, 'td-console.log'),
             os.path.join(run_dir, 'bootstrap.log')]
    try:
        files += sorted((os.path.join(logs_dir, n) for n in
                         os.listdir(logs_dir) if n.endswith('.log')),
                        key=os.path.getmtime)
    except OSError:
        pass
    all_lines = []
    for path in files:
        try:
            with open(path, 'r', encoding='utf-8', errors='replace') as f:
                all_lines.extend(f.readlines())
        except OSError:
            continue
    warnings = [l.rstrip('\n') for l in all_lines
                if ' WARNING ' in l or ' ERROR ' in l]
    return {'tail': ''.join(all_lines[-lines:]), 'warnings': warnings}


def convoy_data_dir(platform=None, env=None, home=None):
    """Convoy's per-user install state (mirrors convoy_install.default_data_dir)."""
    platform = platform or sys.platform
    env = env if env is not None else os.environ
    home = home or os.path.expanduser('~')
    if platform == 'win32':
        base = env.get('LOCALAPPDATA') or os.path.join(home, 'AppData', 'Local')
        return os.path.join(base, 'EmbodyConvoy')
    if platform == 'darwin':
        return os.path.join(home, 'Library', 'Application Support',
                            'EmbodyConvoy')
    return os.path.join(home, '.local', 'share', 'EmbodyConvoy')


def convoy_install_mtime(data_dir=None):
    path = os.path.join(data_dir or convoy_data_dir(), 'installed.json')
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def convoy_install_state(before, after):
    """What the Convoy leg actually exercised on this machine: the host
    app's first install ('fresh_install'), or a reconnect to one already
    installed ('reused'). The leg exists to catch install failures on a
    clean machine, so a 'reused' PASS proves less and must say so."""
    if after is None:
        return 'absent'
    if before is None or after > before:
        return 'fresh_install'
    return 'reused'


def compute_outcome(ready, features, mcp, teardown, no_mcp=False, legs=None):
    """PASS only when every gate held: startup verdict, all feature legs,
    the MCP probe (unless skipped), every extra leg that was asked for, AND
    the teardown -- a run that leaves its TouchDesigner behind is not
    green."""
    if not ready or ready.get('verdict') != 'PASS':
        return 'FAIL'
    if not features or not features.get('passed'):
        return 'FAIL'
    if not no_mcp and not (mcp or {}).get('ok'):
        return 'FAIL'
    if legs is not None and not legs.get('_ok'):
        return 'FAIL'
    if teardown is not None and not teardown.get('ok'):
        return 'FAIL'
    return 'PASS'


def make_leg_context(run, build, installed, upgrade_from, port, pid, td_exe,
                     budget, rpc=None, log=None):
    """The `ctx` every smoke_legs module receives (contract in
    smoke_legs/__init__.py)."""
    rpc = rpc or _rpc
    log = log or (lambda m: print(f'[smoke_run] {m}', file=sys.stderr))
    state = {'port': int(port), 'seq': 100}

    def call(name, arguments, timeout=30):
        state['seq'] += 1
        url = f"http://127.0.0.1:{state['port']}/mcp"
        reply = rpc(url, {'jsonrpc': '2.0', 'id': state['seq'],
                          'method': 'tools/call',
                          'params': {'name': name, 'arguments': arguments}},
                    min(float(timeout), max(1.0, budget())))
        res = _tool_result(reply)
        if 'error' in res:
            raise RuntimeError(f'{name}: {res["error"]}')
        return res

    def py(code, timeout=30):
        return call('execute_python', {'code': code}, timeout).get('result')

    def set_port(p):
        state['port'] = int(p)

    return {
        'run_dir': run['dir'], 'repo': build.get('repo'), 'build': build,
        'installed': installed, 'upgrade_from': upgrade_from,
        'port': state['port'], 'set_port': set_port, 'pid': pid,
        'td_exe': td_exe, 'platform': sys.platform,
        'call': call, 'py': py, 'log': log, 'budget': budget,
        'wait_for_flag': lambda name, timeout, done: wait_for(
            os.path.join(run['dir'], name), timeout, done),
    }


def exit_code(result):
    return {'PASS': 0, 'FAIL': 1}.get(result.get('outcome'), 2)


def summarize(result):
    r = result
    ready = r.get('ready') or {}
    feats = r.get('features') or {}
    mcp = r.get('mcp') or {}
    lines = [f"smoke  {r.get('platform')}  v{r.get('version') or '?'}  "
             f"{os.path.basename(str(r.get('td') or '?'))}  "
             f"run {r.get('run_id')}"]
    if ready:
        lines.append(f"ready     {ready.get('verdict')}  settled after "
                     f"{ready.get('settled_after_attempts', '?')} polls  "
                     f"{ready.get('envoy_status', '')}"
                     + (f"  problems: {ready['problems']}"
                        if ready.get('problems', 'none') != 'none' else ''))
    if feats.get('legs'):
        passed = [l for l in FEATURE_LEGS
                  if feats['legs'].get(l, {}).get('verdict') == 'PASS']
        lines.append(f"features  {len(passed)}/{len(FEATURE_LEGS)}  "
                     + ' '.join(passed))
        for leg in feats.get('failed', []):
            info = feats['legs'].get(leg, {})
            lines.append(f"          {leg}: {info.get('verdict', 'MISSING')} "
                         f"{info.get('detail', '')}")
    if mcp:
        okc = sum(1 for s in mcp.get('steps', []) if s.get('ok'))
        lines.append(f"mcp       {okc}/{len(mcp.get('steps', []))}  "
                     f"{mcp.get('tools', 0)} tools listed"
                     + (f"  {mcp['error']}" if mcp.get('error') else ''))
    legs = r.get('legs') or {}
    for name, leg in legs.items():
        if name.startswith('_'):
            continue
        okc = sum(1 for s in leg.get('steps', []) if s.get('ok'))
        lines.append(f"{name:<9} {'PASS' if leg.get('ok') else 'FAIL'}  "
                     f"{okc}/{len(leg.get('steps', []))} steps  "
                     f"{leg.get('elapsed_s', 0)}s"
                     + (f"  {leg['error']}" if leg.get('error') else ''))
        for s in leg.get('steps', []):
            if not s.get('ok'):
                lines.append(f"          {s.get('step')}: {s.get('detail', '')}")
    if r.get('convoy_host'):
        lines.append(f"convoy    host app {r['convoy_host']}"
                     + ('' if r['convoy_host'] == 'fresh_install' else
                        '  (install path NOT exercised on this machine)'))
    if ready.get('envoy_port') and r.get('default_port') is False:
        lines.append(f"port      Envoy fell back to {ready['envoy_port']} "
                     f"(9870 was taken -- the default-port path was not "
                     f"exercised)")
    warnings = r.get('log_warnings') or []
    if warnings:
        lines.append(f"log       {len(warnings)} WARNING/ERROR line(s) "
                     f"(see result.json log_warnings)")
    td = r.get('teardown') or {}
    if td:
        state = ('ok' if td.get('ok') and td.get('method') != 'forced'
                 else 'ok (FORCED)' if td.get('ok') else 'FAILED')
        lines.append(f"teardown  {state}  {td.get('message', '')}")
    if r.get('error'):
        lines.append(f"error     {r['error']}")
    lines.append(f"result: {r.get('outcome')}  ({r.get('elapsed_s', 0):.0f}s)"
                 f"  ->  {r.get('result_path', '')}")
    if r.get('outcome') != 'PASS':
        # A failing run prints its evidence: a CI step log survives when the
        # artifact upload does not (ETIMEDOUT from the Mac runner, 2026-09-18).
        if warnings:
            lines.append('--- WARNING/ERROR lines ---')
            lines.extend(f"  {w[:240]}" for w in warnings[:20])
        tail = (r.get('log_tail') or '').rstrip()
        if tail:
            lines.append('--- log tail (td-console, bootstrap, embody) ---')
            lines.extend(f"  {l[:240]}" for l in tail.splitlines()[-40:])
    return '\n'.join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--repo', default=DEFAULT_REPO)
    ap.add_argument('--tox', help='release .tox to smoke (default: the '
                                   'manifest asset, sha256-verified)')
    ap.add_argument('--td', help='TouchDesigner executable / .app')
    ap.add_argument('--out', default=(os.environ.get('RUNNER_TEMP')
                                      or tempfile.gettempdir()),
                    help='where run directories go (never inside the repo)')
    ap.add_argument('--timeout', type=float, default=900,
                    help='overall ceiling in seconds')
    ap.add_argument('--no-mcp', action='store_true',
                    help='skip the MCP probe')
    ap.add_argument('--keep-td', action='store_true',
                    help='leave the smoke TouchDesigner running')
    ap.add_argument('--legs', default='',
                    help='extra legs after the fresh-install verdict, comma '
                         'separated: upgrade, faults, uninstall (run in '
                         'that order; see smoke_legs/__init__.py)')
    ap.add_argument('--upgrade-from', default=None,
                    help='for the upgrade leg: a release tag (vX.Y.Z) or a '
                         '.tox path to install FIRST; default: the previous '
                         'release tag, extracted from git history')
    args = ap.parse_args(argv)

    t0 = time.monotonic()
    result = {'outcome': 'ERROR', 'platform': sys.platform, 'run_id': None,
              'version': None, 'tox': None, 'td': None, 'ready': None,
              'features': None, 'mcp': None, 'legs': None, 'teardown': None,
              'installed': None, 'upgrade_from': None,
              'default_port': None, 'convoy_host': None,
              'log_tail': '', 'log_warnings': [], 'error': '',
              'elapsed_s': 0.0, 'result_path': ''}
    try:
        legs = smoke_legs.parse_legs(args.legs)
    except ValueError as e:
        print(f'[smoke_run] {e}', file=sys.stderr)
        return 2
    pid = None
    proc = None
    console = None
    run = None
    convoy_before = convoy_install_mtime()
    try:
        repo = os.path.abspath(args.repo)
        build = select_build(repo, args.tox)
        # With --tox the manifest's version may not describe the file.
        result['version'] = '' if args.tox else build['version']
        result['tox'] = build['tox']
        td_exe, warning = find_td(build, args.td)
        result['td'] = td_exe
        if warning:
            print(f'[smoke_run] {warning}', file=sys.stderr)
        build['repo'] = repo
        upgrade_from = None
        install_tox = None
        if 'upgrade' in legs:
            builds_dir = os.path.join(os.path.abspath(args.out), 'embody-smoke',
                                      '_builds')
            if args.upgrade_from and os.path.isfile(args.upgrade_from):
                p = os.path.abspath(args.upgrade_from)
                upgrade_from = {'tox': p, 'version': '', 'tag': '',
                                'sha256': _sha256(p)}
            else:
                upgrade_from = previous_release(repo, build['version'],
                                                builds_dir,
                                                tag=args.upgrade_from)
            install_tox = upgrade_from['tox']
            print(f"[smoke_run] upgrade leg: installing "
                  f"{os.path.basename(install_tox)} first "
                  f"({upgrade_from.get('tag') or 'file'}), then updating to "
                  f"v{build['version']}", file=sys.stderr)
        result['upgrade_from'] = upgrade_from
        result['installed'] = {'tox': install_tox or build['tox'],
                               'version': (upgrade_from or {}).get('version')
                               if install_tox else build['version']}
        run = stage_run(repo, build, args.out, install_tox=install_tox)
        result['run_id'] = run['run_id']
        result['result_path'] = os.path.join(run['dir'], 'result.json')
        print(f"[smoke_run] staged {run['dir']}", file=sys.stderr)

        console = open(os.path.join(run['dir'], 'td-console.log'), 'ab')
        pid, proc = launch_td(td_exe,
                              os.path.join(run['dir'], 'smoke_template.toe'),
                              console=console)
        result['pid'] = pid
        print(f'[smoke_run] launched TouchDesigner pid {pid}', file=sys.stderr)

        def budget():
            return max(0.0, args.timeout - (time.monotonic() - t0))

        # The bootstrap writes the flag atomically; `tox=` is its last line,
        # so requiring it guards against an older bootstrap that still
        # writes line by line.
        ready_budget = min(READY_TIMEOUT_S, budget())
        text, ok = wait_for(os.path.join(run['dir'], 'ready.flag'),
                            ready_budget,
                            lambda t: 'verdict=' in t and 'tox=' in t)
        if not ok:
            raise SmokeSetupError(
                f'no complete ready.flag within {ready_budget:.0f}s -- TD '
                f'never got to the startup verdict (see log_tail)')
        result['ready'] = ready = parse_ready(text)
        check_run_stamp(ready, run['run_id'])
        result['default_port'] = (ready['envoy_port'] == envoy_bridge.DEFAULT_PORT
                                  if ready['envoy_port'] else None)
        if ready['verdict'] == 'PASS':
            feat_budget = min(FEATURES_TIMEOUT_S, budget())
            text, ok = wait_for(os.path.join(run['dir'], 'features.flag'),
                                feat_budget,
                                lambda t: parse_features(t)['terminal'])
            if text:
                result['features'] = parse_features(text)
            if not ok:
                raise SmokeSetupError(
                    f'features.flag not terminal within {feat_budget:.0f}s '
                    f'(partial verdicts kept in result.json)')
            if not args.no_mcp:
                if ready['envoy_port'] is None:
                    result['mcp'] = {'ok': False, 'tools': 0, 'steps': [],
                                     'error': 'no Envoy port in ready.flag'}
                else:
                    result['mcp'] = probe_mcp(ready['envoy_port'],
                                              timeout=min(MCP_TIMEOUT_S,
                                                          budget()),
                                              expect_dir=run['dir'])
            mcp_ok = args.no_mcp or bool((result['mcp'] or {}).get('ok'))
            if legs and mcp_ok and ready['envoy_port']:
                ctx = make_leg_context(run, build, result['installed'],
                                       upgrade_from, ready['envoy_port'], pid,
                                       td_exe, budget)
                result['legs'] = smoke_legs.run_legs(legs, ctx)
            elif legs:
                result['legs'] = {'_ok': False, 'skipped': {
                    'ok': False, 'steps': [],
                    'error': 'legs not run: the fresh-install verdict or '
                             'the MCP probe already failed'}}
        result['outcome'] = 'PENDING_TEARDOWN'
    except SmokeSetupError as e:
        result['outcome'] = 'ERROR'
        result['error'] = str(e)
    except Exception as e:  # anything else is still "could not run"
        result['outcome'] = 'ERROR'
        result['error'] = f'{type(e).__name__}: {e}'

    # Teardown runs whatever happened above, and it is part of the verdict.
    # Nothing in this epilogue may escape: a crash here would exit 1 (a
    # verdict failed) for what is really "could not run".
    try:
        if pid is not None and not args.keep_td:
            port = (result['ready'] or {}).get('envoy_port')
            result['teardown'] = quit_smoke_td(
                pid, run['dir'], port,
                reap=(lambda p: proc.poll()) if proc is not None else None)
        if console is not None:
            console.close()
        if result['outcome'] != 'ERROR':
            result['outcome'] = compute_outcome(
                result['ready'], result['features'], result['mcp'],
                result['teardown'], no_mcp=args.no_mcp, legs=result['legs'])
        if run:
            logs = collect_logs(run['dir'])
            result['log_tail'] = logs['tail']
            result['log_warnings'] = logs['warnings']
            result['convoy_host'] = convoy_install_state(
                convoy_before, convoy_install_mtime())
    except Exception as e:
        result['outcome'] = 'ERROR'
        result['error'] = (result['error'] + '; ' if result['error'] else '') \
            + f'epilogue failed: {type(e).__name__}: {e}'
    result['elapsed_s'] = time.monotonic() - t0
    if run:
        try:
            with open(result['result_path'], 'w', encoding='utf-8') as f:
                json.dump(result, f, indent=2, default=str)
        except Exception as e:
            print(f'[smoke_run] could not write result.json: {e}',
                  file=sys.stderr)
    try:
        print(summarize(result))
    except Exception as e:
        print(f'[smoke_run] outcome={result.get("outcome")} '
              f'(summary failed: {e})')
    return exit_code(result)


if __name__ == '__main__':
    sys.exit(main())
