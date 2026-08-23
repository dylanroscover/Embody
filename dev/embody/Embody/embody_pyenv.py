"""
embody_pyenv -- the project Python environment (uv venv) machinery.

Extracted from EmbodyExt (2026-08-19) so one module owns everything venv:
spec building, needs-install detection, uv invocation, stamping, sys.path /
PATH / DLL wiring, the MCP import gate, and the NEW user-extras layer
(declared third-party packages that survive rebuilds and travel in git).

THREAD CONTRACT
- Every function here is pure Python: no op()/mod./parameters/DAT access.
  Safe to CALL from a worker thread once you hold a reference -- with two
  exceptions: detect_tdpyenvmanager dereferences the live TD ``app``
  object when one is passed (MAIN THREAD ONLY then), and link_env
  refuses to run off the main thread (process-env writes from a worker
  are the setenv-corruption class).
- Resolving THIS module (``mod.embody_pyenv``) is a TD object lookup --
  MAIN THREAD ONLY. Resolve on the main thread, hand the module (or bound
  functions) to the worker (EnvoyExt._beginAsyncBootstrap pattern).
- Functions taking a ``log`` callback never touch TD; main-thread callers
  pass ext.Log, workers pass a collector list.

EmbodyExt keeps thin same-named facades (main-thread only) so ConvoyExt,
embody_admin, and the test suite call sites are unchanged.

Author: Dylan Roscover
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
from glob import glob, escape as glob_escape

# TD is a GUI process on Windows and owns no console -- every console child
# would flash a window without this. Same constant as EmbodyExt.NO_WINDOW.
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)

# Frozen resolved closure of the CORE deps, written after every successful
# core install. Extras installs pass it as a uv constraint file so a user
# package can never move mcp/pydantic/starlette/anyio/... out from under
# the running server (the 5 direct pins alone leave the transitive tree
# open -- issue #81's failure mode, self-inflicted).
CONSTRAINTS_FILENAME = 'embody-core-constraints.txt'

# Dists TD itself is known to load-bear on (Script ops, internal tools).
# Runtime enumeration of TD's own site-packages is the real gate; this seed
# covers installs where a bundled package ships without dist-info.
TD_BUNDLED_SEED = frozenset({
    'numpy', 'attrs', 'requests', 'jinja2', 'pillow', 'opencv-python',
})

# Names extras may never touch directly, whatever the constraints say --
# the core stack plus the packaging tools themselves.
CORE_PROTECTED_SEED = frozenset({
    'mcp', 'attrs', 'pyyaml', 'cryptography', 'pywin32',
    'pip', 'uv', 'setuptools', 'wheel',
})

_SPEC_NAME_RE = re.compile(r'^\s*([A-Za-z0-9][A-Za-z0-9._-]*)')

# After the name: optional extras bracket, optional version specifier,
# optional environment marker. Nothing else -- URL, VCS, direct-reference
# and filesystem forms are refused by valid_requirement.
_SPEC_TAIL_RE = re.compile(
    r'^(\[[A-Za-z0-9_.,\s-]*\])?\s*([<>=!~][A-Za-z0-9<>=!~*.,\s+-]*)?'
    r'\s*(;[^@/\\]*)?$')


# ==========================================================================
# Spec building and platform pins (ported from EmbodyExt, 2026-08-19)
# ==========================================================================

def venv_paths(project_dir, mcp_min_version, declared_extras=None,
               state_root=None) -> dict:
    """Build the venv spec dict for this project + interpreter.

    Ported from EmbodyExt._venvPaths; project folder and the pinned MCP
    floor are passed in so this stays pure (the constant remains
    authoritative on EmbodyExt.MCP_MIN_VERSION -- releases bump it there).
    ``declared_extras`` (the committed python.extras list, when the caller
    has it) rides along so worker-side freeze_constraints can exclude the
    user's own packages from the core constraints snapshot.
    """
    venv_dir = os.path.join(project_dir, '.venv')
    python_exe = sys.executable  # current interpreter (cross-platform)
    # pyyaml: powers the .tdn git textconv driver (the committed-diff
    # counterpart to diff_tdn). git invokes that driver via THIS venv
    # python, and v6 .tdn files are YAML, so the venv must carry yaml even
    # though the Envoy bridge itself never imports it.
    ceiling_major = int(mcp_min_version.split('.')[0]) + 1
    # cryptography: the Convoy host app runs under THIS venv python and
    # needs Ed25519 + X.509 + TLS 1.3 for LAN peer identity/mutual-TLS.
    # It usually arrives transitively, but Convoy depends on it directly,
    # so pin an explicit floor (matches the bridge-suite CI floor) rather
    # than relying on another package to keep pulling it in. FLOOR, no
    # upper bound -- an exact pin is what broke fresh installs at mcp 2.0.
    # (The one exception is a ceiling on x86_64 macOS interpreters --
    # see cryptography_pin for why that target cannot take 49+.)
    deps = [f'mcp>={mcp_min_version},<{ceiling_major}', 'attrs<25',
            'pyyaml', cryptography_pin(sys.platform, platform.machine())]
    if sys.platform.startswith('win'):
        site_packages = os.path.join(venv_dir, 'Lib', 'site-packages')
        venv_python = os.path.join(venv_dir, 'Scripts', 'python.exe')
        deps.append('pywin32>=311')  # mcp 2.x floor on win32
    else:
        py_ver = f'python{sys.version_info.major}.{sys.version_info.minor}'
        site_packages = os.path.join(venv_dir, 'lib', py_ver, 'site-packages')
        venv_python = os.path.join(venv_dir, 'bin', 'python')
    return {
        'project_dir': project_dir,
        'venv_dir': venv_dir,
        'site_packages': site_packages,
        'venv_python': venv_python,
        'python_exe': python_exe,
        'deps': deps,
        'mcp_min_version': mcp_min_version,
        'mcp_ceiling_major': ceiling_major,
        # What the stamp records / compares. Binary wheels (pydantic_core,
        # cryptography, pywin32) are ABI-tied to the interpreter, so a TD
        # upgrade that bumps embedded Python must rebuild the venv.
        'python_tag': f'{sys.version_info.major}.{sys.version_info.minor}',
        # ... and ARCHITECTURE-tied too: platform.machine() of the TD
        # process that built the venv. Swapping the Intel TD build for
        # the Apple Silicon one (or migrating a project between Macs)
        # leaves wheels the interpreter can no longer dlopen, with
        # every version check reading clean. (Distinct from macOS
        # library validation, where TD's signed python refuses even a
        # correct-arch foreign wheel standalone -- Convoy handles that
        # with its daemon venv; no stamp can.)
        'machine': platform.machine(),
        'stamp_path': os.path.join(venv_dir, 'embody-env.json'),
        'constraints_path': os.path.join(venv_dir, CONSTRAINTS_FILENAME),
        # OUTSIDE the venv (a --clear must not delete a held lock) and
        # inside gitignored .embody/ at the PROJECT ROOT (state_root) --
        # rooted at the .toe folder it escapes the managed '.embody/*'
        # ignore when the .toe sits in a repo subfolder (2026-08-19).
        'install_lock_path': os.path.join(state_root or project_dir,
                                          '.embody', 'embody-install.lck'),
        'declared_extras': [str(s) for s in (declared_extras or [])],
    }


def cryptography_pin(platform_name: str, machine: str) -> str:
    """The venv's cryptography requirement for this interpreter target.

    cryptography 49.0.0 (2026-06) stopped publishing x86_64 macOS
    wheels -- macOS wheels are arm64-only from there on, while 48.x
    and older shipped universal2 (loads under BOTH architectures).
    An x86_64-running interpreter on macOS (the Intel TD build,
    including under Rosetta) resolving the unbounded pin therefore
    gets either a wheel it can never dlopen or a source build needing
    a Rust toolchain. Cap below 49 on that one target so uv resolves
    the universal2 48.x wheel; everywhere else the pin stays a pure
    floor (an exact pin is what broke fresh installs at mcp 2.0).
    """
    if platform_name == 'darwin' and str(machine).lower() in (
            'x86_64', 'x64', 'amd64'):
        return 'cryptography>=3.4,<49'
    return 'cryptography>=3.4'


def stamp_needs_arch_migration(stamp_machine: str, platform_name: str) -> bool:
    """Should a stamp with NO recorded machine force one rebuild?

    Pre-arch stamps exist exactly on the fleet that can hold wrong-arch
    wheels invisibly (macOS: two TD architectures, one Rosetta), so darwin
    migrates each such venv once to an arch-stamped one. Windows venvs
    with old stamps stay untouched -- a single supported architecture
    cannot flip.
    """
    return not stamp_machine and platform_name == 'darwin'


def measure_venv_arch(venv_python) -> 'str | None':
    """The architecture the venv interpreter runs as when spawned FRESH,
    or None. What uv's wheel resolution keyed on and what a login daemon
    (launchd / Scheduled Task) actually gets -- inside TD,
    platform.machine() reports the TD process instead, and under Rosetta
    the two can differ. Diagnostic breadcrumb only; the rebuild TRIGGER
    compares TD-process values (see environment_needs_install).
    """
    try:
        proc = subprocess.run(
            [venv_python, '-I', '-c',
             'import platform; print(platform.machine())'],
            capture_output=True, text=True, timeout=15,
            encoding='utf-8', errors='replace',
            stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)
        if proc.returncode != 0:
            return None
        return (proc.stdout or '').strip() or None
    except Exception:
        return None


def venv_python_tag(venv_dir) -> 'str | None':
    """major.minor of the interpreter a venv was built for, from its
    pyvenv.cfg (uv writes ``version_info = 3.11.15``; stdlib venv writes
    ``version = 3.11.15``). None when the cfg is missing or unparseable.
    """
    try:
        with open(os.path.join(venv_dir, 'pyvenv.cfg'), 'r',
                  encoding='utf-8') as f:
            for line in f:
                key, _, val = line.partition('=')
                if key.strip().lower() in ('version', 'version_info'):
                    parts = val.strip().split('.')
                    if len(parts) >= 2:
                        return f'{int(parts[0])}.{int(parts[1])}'
    except Exception:
        pass
    return None


# ==========================================================================
# Needs-install detection (ported; extras never gate this -- see
# extras_status for the separate, non-gating extras phase)
# ==========================================================================

def environment_needs_install(spec: dict) -> bool:
    """Cheap, non-blocking check: does the venv need a (slow) CORE install?

    Filesystem-only (no subprocess, no network, no import) -- its cheapness
    is load-bearing: EnvoyExt.Start calls it on the main thread every
    start. User EXTRAS are deliberately excluded from this predicate: a
    typo'd extra must never take the MCP server down with it (extras have
    their own non-gating phase). Side effect: a Python/arch mismatch sets
    ``spec['recreate_venv']`` so install_dependencies rebuilds via
    ``uv venv --clear`` instead of installing onto the wrong ABI.
    """
    site_packages = spec['site_packages']
    if not os.path.isdir(os.path.join(site_packages, 'mcp')):
        return True
    # PyYAML powers the .tdn git textconv driver, which git runs via this
    # venv python. An older venv built before that dependency lacks it, so
    # treat its absence as "needs install" to upgrade existing venvs.
    if not os.path.isdir(os.path.join(site_packages, 'yaml')):
        return True
    # The venv's OWN record of the interpreter that built it, checked
    # before any stamp logic: a TD upgrade that bumps embedded Python
    # must REBUILD (binary wheels are ABI-tied), including pre-stamp
    # venvs and combined python+deps upgrades where a deps-first check
    # would return early and install in place onto the wrong ABI.
    cfg_tag = venv_python_tag(spec.get('venv_dir', ''))
    if cfg_tag and cfg_tag != spec['python_tag']:
        spec['recreate_venv'] = True
        return True
    # Spec stamp: what this venv was built for. Missing (any pre-stamp
    # venv) or different (a pin changed, a dep added) -> reinstall.
    try:
        with open(spec['stamp_path'], 'r', encoding='utf-8') as f:
            stamp = json.load(f)
    except Exception:
        stamp = None
    if not isinstance(stamp, dict):
        return True
    if stamp.get('python') != spec['python_tag']:
        # Secondary to the pyvenv.cfg probe (a cfg-less venv still gets
        # caught here) -- checked before deps so the rebuild verdict wins
        # over the in-place-upgrade verdict when both changed.
        spec['recreate_venv'] = True
        return True
    stamp_machine = str(stamp.get('machine') or '')
    if stamp_machine and stamp_machine != str(spec.get('machine') or ''):
        # Binary wheels are ARCHITECTURE-tied as well as ABI-tied.
        # Swapping the Intel TD build for the Apple Silicon one (or
        # migrating the project between machines) leaves wheels the
        # interpreter can no longer dlopen while every version check
        # reads clean -- cryptography's Rust .dylib refusing to load
        # was exactly this. Same placement rule as the python check:
        # BEFORE deps, so recreate wins over in-place install (uv
        # would resolve wrong-arch-but-present wheels as satisfied).
        spec['recreate_venv'] = True
        return True
    if (spec.get('machine')
            and stamp_needs_arch_migration(stamp_machine, sys.platform)):
        # Guarded on the CURRENT machine value: if platform.machine()
        # ever returned '' here, a rebuild could not stamp anything
        # better and the migration would re-fire every start -- a
        # permanent rebuild loop for zero information gained.
        spec['recreate_venv'] = True
        return True
    # Compare CORE deps only. The stamp also carries extras_applied /
    # extras_failed (see install_extras) -- folding those into this
    # equality would wedge needs-install permanently True.
    if sorted(stamp.get('deps') or []) != sorted(spec['deps']):
        return True
    ver = mcp_dist_version(site_packages)
    if ver is None:
        # mcp present but no parseable metadata -- the old fast path
        # accepted this and proceeded to the import check, so no install
        # is required.
        return False
    try:
        installed = tuple(int(x) for x in ver.split('.')[:3])
        minimum = tuple(int(x) for x in spec['mcp_min_version'].split('.'))
        if installed < minimum:
            return True
        # At or above the unsupported next major (a hand-installed newer
        # mcp, or the unpinned-resolver era that pulled 2.0.0 under 1.x
        # code -- issue #81): reinstall walks it back inside the pin.
        if installed >= (spec['mcp_ceiling_major'],):
            return True
    except Exception:
        return False
    # attrs 25.x conflicts with TD's bundled attr module -- needs a downgrade.
    try:
        attrs_infos = glob(os.path.join(glob_escape(site_packages), 'attrs-*.dist-info'))
        if attrs_infos:
            aver = os.path.basename(attrs_infos[0])[len('attrs-'):-len('.dist-info')]
            if int(aver.split('.')[0]) >= 25:
                return True
    except Exception:
        pass
    return False


# ==========================================================================
# Interpreter wiring: sys.path + PATH + DLL dir + VIRTUAL_ENV
# ==========================================================================

def add_site_packages(site_packages) -> None:
    """Add venv site-packages (and pywin32 subdirs on Windows) to sys.path."""
    paths = [site_packages]
    if sys.platform.startswith('win'):
        paths.append(os.path.join(site_packages, 'win32'))
        paths.append(os.path.join(site_packages, 'win32', 'lib'))
    for p in paths:
        if os.path.isdir(p) and p not in sys.path:
            sys.path.insert(0, p)


def link_env(spec: dict) -> bool:
    """PATH-prepend + DLL search dir + VIRTUAL_ENV for the venv.

    sys.path alone imports pure-Python fine, but native wheels that ship
    companion DLLs need the venv's Scripts/ (bin/) resolvable -- this is
    tdPyEnvManager's proven trio (2026-08-19) and the reason its native
    packages worked where ours needed the pywin32 copy-hack. The retained
    add_dll_directory handle lives in a sys-level registry so extension
    reinit never drops or doubles it. VIRTUAL_ENV is only set when unset:
    a foreign value (tdPyEnvManager pointing at ITS env) is co-existence
    territory -- never fight another manager over env vars; our own
    subprocesses strip it anyway (bootstrap_env). Idempotent.

    MAIN THREAD ONLY, enforced (2026-08-19 review): os.environ writes and
    AddDllDirectory from a worker are the classic non-thread-safe-setenv
    corruption vector -- same class as worker-side run(). Refuses (False)
    off-main instead of regressing silently.
    """
    if threading.current_thread() is not threading.main_thread():
        return False
    venv_dir = spec.get('venv_dir')
    if not venv_dir or not os.path.isdir(venv_dir):
        return False
    bin_dir = os.path.join(
        venv_dir, 'Scripts' if sys.platform.startswith('win') else 'bin')
    if not os.path.isdir(bin_dir):
        return False
    norm = os.path.normcase(os.path.normpath(bin_dir))
    path_env = os.environ.get('PATH', '')
    have = {os.path.normcase(os.path.normpath(p))
            for p in path_env.split(os.pathsep) if p}
    if norm not in have:
        os.environ['PATH'] = bin_dir + os.pathsep + path_env
    if sys.platform.startswith('win') and hasattr(os, 'add_dll_directory'):
        links = getattr(sys, '_embody_pyenv_dll_links', None)
        if links is None:
            links = {}
            sys._embody_pyenv_dll_links = links
        if norm not in links:
            try:
                links[norm] = os.add_dll_directory(bin_dir)
            except OSError:
                pass
    if not os.environ.get('VIRTUAL_ENV'):
        os.environ['VIRTUAL_ENV'] = venv_dir
    return True


def unlink_dll_dir(venv_dir) -> None:
    """Close + drop the retained AddDllDirectory handle for a venv that is
    about to be (or was) rebuilt -- Windows re-resolution across a
    delete/recreate of the same path is not guaranteed, so re-add fresh.
    MAIN THREAD ONLY, enforced (pairs with link_env)."""
    if threading.current_thread() is not threading.main_thread():
        return
    links = getattr(sys, '_embody_pyenv_dll_links', None)
    if not isinstance(links, dict) or not venv_dir:
        return
    bin_dir = os.path.join(
        venv_dir, 'Scripts' if sys.platform.startswith('win') else 'bin')
    norm = os.path.normcase(os.path.normpath(bin_dir))
    handle = links.pop(norm, None)
    if handle is not None:
        try:
            handle.close()
        except Exception:
            pass


def wire_python_paths(spec: dict, log=None) -> bool:
    """Wire the venv's site-packages into sys.path. WORKER-THREAD SAFE
    (sys.path mutation only). PATH/DLL/VIRTUAL_ENV linking is link_env's
    job and happens separately on the MAIN thread -- do not fold it back
    in here (2026-08-19 review: it briefly lived here and put os.environ
    writes on EnvoyExt's bootstrap worker).

    ``log`` names WHICH precondition failed. Pass one unless the caller
    reports the failure itself (Start / _setupEnvironment do): an
    unreported False is the state a cold-open ModuleNotFoundError comes
    from, and it used to be the quietest line in the boot log
    (field 2026-08-20).
    """
    venv_dir = spec.get('venv_dir')
    site_packages = spec.get('site_packages')
    if not venv_dir or not os.path.isdir(venv_dir):
        if log:
            log(f'Python environment NOT wired: no venv at '
                f'{venv_dir or "(unset)"}. Packages installed there will '
                f'not import in TouchDesigner until it is built.',
                'WARNING')
        return False
    if not site_packages or not os.path.isdir(site_packages):
        if log:
            log(f'Python environment NOT wired: the venv at {venv_dir} has '
                f'no site-packages at {site_packages or "(unset)"} -- the '
                f'environment is incomplete. Deleting .venv/ forces a '
                f'clean rebuild.', 'WARNING')
        return False
    add_site_packages(site_packages)
    return True


# ==========================================================================
# MCP import gate (ported)
# ==========================================================================

def import_gate_check(site_packages: 'str | None' = None) -> tuple:
    """Pure import gate for Envoy's MCP stack.

    Safe from a background thread: no TD objects, no logging. Returns
    ``(True, '')`` when ``mcp.server.mcpserver`` -- the module EnvoyExt
    actually serves with -- imports. Gating on the exact module matters:
    SDK 2.0.0 kept ``mcp.server`` importable while REMOVING
    ``mcp.server.fastmcp``, so a parent-package gate passed and the server
    then died in a 30-minute retry storm (issue #81).

    With ``site_packages``, also refuses the stale-interpreter upgrade
    state: deps upgraded on disk while an older mcp stack is already
    imported. Re-running pydantic model definitions over a live
    pydantic_core can abort() the process, so the only safe exit is a TD
    restart -- say so instead of trying.
    """
    needed = 'mcp.server.mcpserver'
    disk_ver = (mcp_dist_version(site_packages) if site_packages else None)
    loaded_ver = getattr(sys, '_envoy_mcp_loaded_version', None)
    stale = (loaded_ver and disk_ver and disk_ver != loaded_ver
             and 'mcp' in sys.modules)
    # A fully-loaded 1.x stack identifies itself by its fastmcp module --
    # 2.x has none. New-generation code gating over a LIVE 1.x stack
    # always needs a restart, whatever the disk state (even mid-install,
    # when disk metadata may be absent): purging a live stack to reimport
    # is the documented pydantic_core abort() vector. A half-loaded
    # FAILED import (parent 'mcp' without fastmcp) is NOT this case and
    # still takes the recovery purge below.
    legacy_loaded = ('mcp.server.fastmcp' in sys.modules
                     and needed not in sys.modules)
    if stale or legacy_loaded:
        # Kill the process-wide fast-path flag: a refusal must make EVERY
        # subsequent Start re-run this gate (and re-refuse) rather than
        # short-circuit into _continueStart and import a mixed stack.
        sys._envoy_import_gate_ok = False
        return False, (
            f'Envoy dependencies were upgraded on disk (installed mcp '
            f'{disk_ver or "unknown"}, loaded {loaded_ver or "1.x"}) but '
            f'the old stack is already imported in this session. Save '
            f'your work and restart TouchDesigner to finish the upgrade.'
        )
    if needed in sys.modules:
        sys._envoy_import_gate_ok = True
        return True, ''
    try:
        import importlib
        # First import attempt of this session, or recovery from a prior
        # failed import: clear any half-loaded mcp.* entries so the loader
        # genuinely re-runs (a failed import leaves the parent package
        # behind but not the submodule).
        for m in list(sys.modules):
            if m == 'mcp' or m.startswith('mcp.'):
                del sys.modules[m]
        importlib.import_module(needed)
        sys._envoy_import_gate_ok = True
        if disk_ver:
            # Remember which dist this interpreter imported so a future
            # on-disk upgrade is detected as restart-required, not
            # hot-swapped into a mixed stack.
            sys._envoy_mcp_loaded_version = disk_ver
        return True, ''
    except BaseException as e:
        sys._envoy_import_gate_ok = False
        return False, str(e) or e.__class__.__name__


def mcp_dist_version(site_packages) -> 'str | None':
    """Newest parseable mcp version among mcp-*.dist-info directories.

    Version-sorted, not filesystem-order: an interrupted uninstall can
    leave an old dist-info beside the current one, and reading the stale
    name would wedge needs-install True forever and feed the gate a wrong
    disk version. None when no parseable metadata exists.
    """
    try:
        best = None
        for p in glob(os.path.join(glob_escape(site_packages), 'mcp-*.dist-info')):
            v = os.path.basename(p)[len('mcp-'):-len('.dist-info')]
            try:
                key = tuple(int(x) for x in v.split('.')[:3])
            except Exception:
                continue
            if best is None or key > best[0]:
                best = (key, v)
        return best[1] if best else None
    except Exception:
        return None


def import_gate_failure_message(site_packages, message):
    if 'restart TouchDesigner' in message:
        return message  # the restart notice is the complete instruction
    return (
        f'Dependencies installed but mcp.server.mcpserver failed to '
        f'import: {message}. '
        f'Inspect {site_packages} for partial installs and try deleting '
        f'.venv/ to force a clean rebuild.'
    )


# ==========================================================================
# uv invocation (single funnel -- every flag here was paid for in the field)
# ==========================================================================

def bootstrap_env(**extra) -> dict:
    """Environment for bootstrap children (python -m pip, uv).

    Starts from TD's environment (children may need TD's loader vars)
    but drops the variables that redirect Python's module search:
    PYTHONPATH, PYTHONSTARTUP, PYTHONUSERBASE, PYTHONNOUSERSITE.
    TouchDesigner's 'Python 64-bit Module Path' preference reaches
    child processes through the environment on macOS, and users also
    set PYTHONPATH globally -- either way a foreign site-packages
    leaking into the pip/uv children changes what they resolve and
    print (field, 2026-08-18: non-ASCII pip output killed env setup on
    a GUI-launched macOS TD). VIRTUAL_ENV joins the strip list
    (2026-08-19): tdPyEnvManager sets it process-wide to ITS env, and
    uv honors it whenever --python is absent -- our children must
    never resolve a foreign target. PYTHONIOENCODING pins child output
    to UTF-8 so the parent-side decode is byte-exact everywhere.
    """
    env = {k: v for k, v in list(os.environ.items())
           if k not in ('PYTHONPATH', 'PYTHONSTARTUP',
                        'PYTHONUSERBASE', 'PYTHONNOUSERSITE',
                        'VIRTUAL_ENV')}
    env['PYTHONIOENCODING'] = 'utf-8'
    env.update(extra)
    return env


def hardened_env(**extra) -> dict:
    """bootstrap_env plus a full UV_*/PIP_* scrub -- the EXTRAS install
    environment (2026-08-19). A poisoned UV_INDEX_URL / PIP_INDEX_URL in
    the user's login env would redirect where user-requested packages
    resolve from; extras are agent-reachable and auto-replayed, so they
    get the paranoid env. The CORE install keeps plain bootstrap_env on
    purpose: studios behind corporate mirrors configure exactly these
    variables, and breaking every mirrored core bootstrap is a worse
    trade than the residual risk on pinned-name-and-version core deps.
    """
    env = bootstrap_env(**extra)
    for k in list(env):
        if k.startswith('UV_') or k.startswith('PIP_'):
            del env[k]
    env.update(extra)
    return env


def run_uv(uv, args, timeout=None, hardened=False):
    """THE uv invocation funnel. Every flag is a paid-for field lesson:
    stdin=DEVNULL (WinError 50 on GUI TD), NO_WINDOW (console flash),
    utf-8+replace decode (macOS ASCII locale), bootstrap_env (foreign
    site-packages / VIRTUAL_ENV leaks), UV_LINK_MODE=copy (cloud-sync
    hardlink poisoning, os error 396). ``hardened=True`` (extras) also
    scrubs UV_*/PIP_* and passes --no-config so a pulled uv.toml /
    pyproject.toml cannot redirect resolution. Raises CalledProcessError
    on non-zero exit like the callers expect. Worker-thread safe.
    """
    env = (hardened_env if hardened else bootstrap_env)(UV_LINK_MODE='copy')
    cmd = [uv] + (['--no-config'] if hardened else []) + list(args)
    return subprocess.run(
        cmd,
        check=True, capture_output=True, text=True,
        encoding='utf-8', errors='replace',
        stdin=subprocess.DEVNULL,
        env=env,
        creationflags=NO_WINDOW, timeout=timeout,
    )


def resolve_uv() -> 'str | None':
    """Locate an existing uv WITHOUT installing one, or None.

    PATH first, then the common pip --user locations. The fallback list
    matters inside a GUI TouchDesigner: macOS GUI apps get the launchd
    PATH, which excludes ~/.local/bin and ~/Library/Python/3.x/bin --
    exactly where the pip --user install puts uv. Resolve-only and
    subprocess-free, so main-thread callers may use it.
    """
    uv = shutil.which('uv')
    if uv:
        return uv
    if sys.platform.startswith('win'):
        appdata = os.environ.get('APPDATA', '')
        if appdata:
            for candidate in glob(os.path.join(
                    glob_escape(appdata), 'Python', 'Python*', 'Scripts', 'uv.exe')):
                if os.path.isfile(candidate):
                    return candidate
    else:
        home = os.path.expanduser('~')
        for candidate in (
                glob(os.path.join(glob_escape(home), 'Library', 'Python', '3.*',
                                  'bin', 'uv'))
                + [os.path.join(home, '.local', 'bin', 'uv')]):
            if os.path.isfile(candidate):
                return candidate
    return None


def find_or_install_uv(python_exe, log=None):
    """Find uv (PATH or user-install locations), or install it via
    pip --user. Returns path to uv executable or None. ``log`` is a
    ``log(message, level='INFO')`` callback -- main-thread callers pass
    ext.Log, worker callers a thread-safe collector; None is tolerated
    (silent) so a direct module caller can never TypeError mid-install.
    """
    log = log or (lambda m, lvl='INFO': None)
    uv = resolve_uv()
    if uv:
        return uv

    # Install uv via pip --user (avoids needing admin for Program Files)
    log('uv not found - installing via pip...')
    try:
        subprocess.run(
            [python_exe, '-m', 'pip', 'install', '--user', 'uv'],
            check=True, capture_output=True, text=True,
            encoding='utf-8', errors='replace',
            stdin=subprocess.DEVNULL, env=bootstrap_env(),
            creationflags=NO_WINDOW,
        )
    except subprocess.CalledProcessError as e:
        log(f'Failed to install uv: {e.stderr or e}', 'ERROR')
        return None

    uv = resolve_uv()
    if uv:
        return uv

    log('Could not find uv after install - is Python user Scripts on PATH?',
        'ERROR')
    return None


# ==========================================================================
# Cross-process install lock + atomic JSON
# ==========================================================================

def _write_json_atomic(path, data) -> None:
    """Atomic tmp+replace JSON write with the Windows PermissionError
    retry (mirrors embody_admin._write_json_atomic; local so this module
    never needs a TD mod lookup). The temp name is unique per write so
    two concurrent writers of one file cannot truncate each other's temp
    (last replace wins -- lost-update, never corruption). Raises on
    final failure."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f'{path}.{os.getpid()}.{threading.get_ident()}.tmp'
    content = json.dumps(data, indent=2) + '\n'
    try:
        for attempt in range(3):
            try:
                with open(tmp, 'w', encoding='utf-8', newline='\n') as f:
                    f.write(content)
                os.replace(tmp, path)
                return
            except (PermissionError, FileNotFoundError):
                if attempt < 2:
                    time.sleep(0.1)
                else:
                    raise
    finally:
        if os.path.isfile(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _pid_alive(pid) -> bool:
    """Best-effort liveness, erring toward ALIVE: stealing a live
    holder's lock is the failure the lock exists to prevent, so only a
    definitive no-such-process verdict reads as dead.

    win32: NEVER os.kill -- CPython maps signal 0 to
    GenerateConsoleCtrlEvent(CTRL_C_EVENT), which broadcasts a real
    Ctrl-C into the shared console (KeyboardInterrupt'd the CI runner's
    pytest, 2026-08-19). ctypes OpenProcess probe instead; canonical
    copy: convoy_client._win32_pid_is_alive."""
    try:
        pid = int(pid)
    except (ValueError, TypeError):
        return False  # garbage pid in the record
    if pid <= 0:
        return False
    if sys.platform == 'win32':
        try:
            import ctypes
            from ctypes import wintypes
            k32 = ctypes.WinDLL('kernel32', use_last_error=True)
            k32.OpenProcess.restype = wintypes.HANDLE
            k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL,
                                        wintypes.DWORD)
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION,
                                False, pid)
            if not h:
                # ERROR_ACCESS_DENIED (5) = alive but unopenable
                # (elevated / other-user TD); anything else = dead.
                return ctypes.get_last_error() == 5
            try:
                STILL_ACTIVE = 259
                code = wintypes.DWORD()
                k32.GetExitCodeProcess.argtypes = (
                    wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
                if k32.GetExitCodeProcess(h, ctypes.byref(code)):
                    return code.value == STILL_ACTIVE
                return True
            finally:
                k32.CloseHandle(h)
        except Exception:
            return True  # cannot probe -- assume alive
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return False
    except Exception:
        return True  # cannot probe -- assume alive


def _lock_stale_after(purpose: str) -> float:
    """Per-purpose staleness window. A core install is minutes; an extras
    install can be a 20+ minute torch download. Core's window sits BELOW
    install_dependencies' 30-min wait deadline so a wedged core holder is
    always reclaimable within the wait (2026-08-19 review: a single
    3600s window made the waiter give up 30 min before it could ever
    judge the holder stale)."""
    return 1200.0 if purpose == 'core' else 3600.0


def acquire_install_lock(spec: dict, purpose: str) -> bool:
    """Cross-process lock guarding every module-side write into the venv.

    Two TD instances legitimately share one project folder (multi-instance
    is a supported workflow), and sys-level busy flags are per-process --
    without this, instance A's ``uv venv --clear`` can run under instance
    B's live extras install (2026-08-19 review). Stale when the recorded
    pid is dead or the lock outlives its purpose's window. A stale lock
    is STOLEN via atomic os.replace of a uniquely-named record, then
    re-read to confirm -- two simultaneous stealers converge on exactly
    one winner (the unlink-then-create dance had a TOCTOU where both
    won). Non-blocking: returns False when someone else holds it. The
    winner's token lands in spec['_lock_token'] for release.
    """
    path = spec.get('install_lock_path')
    if not path:
        return True  # legacy spec dicts (tests, old callers): no gating
    token = f'{os.getpid()}-{threading.get_ident()}-{time.monotonic_ns()}'
    record = {'pid': os.getpid(), 'time': time.time(),
              'purpose': purpose, 'token': token}

    def _confirm():
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f).get('token') == token
        except Exception:
            return False

    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(record, f)
        spec['_lock_token'] = token
        return True
    except FileExistsError:
        pass
    except OSError:
        return False
    try:
        with open(path, 'r', encoding='utf-8') as f:
            holder = json.load(f)
        held_purpose = str(holder.get('purpose') or 'extras')
        stale = (not _pid_alive(holder.get('pid'))
                 or (time.time() - float(holder.get('time') or 0)
                     > _lock_stale_after(held_purpose)))
    except Exception:
        stale = True  # unreadable lock: treat as stale
    if not stale:
        return False
    # Atomic steal: replace, then confirm we are the surviving record.
    try:
        steal_tmp = f'{path}.{token}.steal'
        with open(steal_tmp, 'w', encoding='utf-8') as f:
            json.dump(record, f)
        os.replace(steal_tmp, path)
    except OSError:
        return False
    if _confirm():
        spec['_lock_token'] = token
        return True
    return False


def release_install_lock(spec: dict) -> None:
    """Drop the lock THIS acquisition wrote -- matched by token, never
    by pid alone (two threads of one process must not free each other's
    lock, 2026-08-19 review)."""
    path = spec.get('install_lock_path')
    token = spec.pop('_lock_token', None)
    if not path or not token:
        return
    try:
        with open(path, 'r', encoding='utf-8') as f:
            holder = json.load(f)
        if holder.get('token') == token:
            os.unlink(path)
    except Exception:
        pass


# ==========================================================================
# Core install + stamp + constraints freeze
# ==========================================================================

def install_dependencies(spec: dict, log) -> bool:
    """Create/refresh the venv and install the CORE dependency spec.

    WORKER-THREAD SAFE: subprocess + filesystem only; all TD values were
    precomputed into ``spec`` on the main thread. Does NOT touch sys.path
    or import mcp -- callers do that separately so the delicate
    pydantic_core import can run off the TD main thread. After a
    successful install the resolved closure is frozen to the constraints
    file (extras protection) and the stamp is rewritten -- preserving any
    extras bookkeeping so a core upgrade never forgets user packages.
    """
    venv_dir = spec['venv_dir']
    venv_python = spec['venv_python']
    python_exe = spec['python_exe']
    deps = spec['deps']
    # Cross-process lock. On a WORKER thread (EnvoyExt's async bootstrap)
    # a bounded sleep-wait is fine; on the MAIN thread (the synchronous
    # wizard/_enableEnvoy path) sleeping would freeze TouchDesigner for
    # the whole wait (2026-08-19 review), so that path tries exactly once
    # and fails fast with a retry instruction.
    locked = False
    on_main = threading.current_thread() is threading.main_thread()
    deadline = time.monotonic() + (0 if on_main else 1800)
    while True:
        if acquire_install_lock(spec, 'core'):
            locked = True
            break
        if time.monotonic() >= deadline:
            log('Another install is running against this project\'s '
                '.venv (a second TouchDesigner instance, or a background '
                'package install). Retry after it finishes.', 'ERROR')
            return False
        time.sleep(5)
    try:
        uv = find_or_install_uv(python_exe, log)
        if not uv:
            log('uv not found and could not be installed -- Envoy cannot '
                'bootstrap. Install uv manually (https://docs.astral.sh/uv/) '
                'and ensure it is on PATH visible to TouchDesigner (macOS GUI '
                'apps do not inherit shell PATH).', 'ERROR')
            return False

        recreate = bool(spec.get('recreate_venv'))
        if recreate or not os.path.isdir(venv_dir):
            log('Rebuilding virtual environment (Python version changed)...'
                if recreate else 'Creating virtual environment...')
            cmd = ['venv', venv_dir, '--python', python_exe]
            if recreate:
                # uv owns the removal of the stale-ABI venv; Embody never
                # recursive-deletes it by hand.
                cmd.append('--clear')
            run_uv(uv, cmd, timeout=1800)

        # Name the spec in the log line: when an install resolves the
        # wrong thing in the field, this line is the evidence (issue #81
        # was diagnosed from a log that couldn't show what pip saw).
        log(f'Installing dependencies ({", ".join(deps)})...')
        run_uv(uv, ['pip', 'install'] + deps + ['--python', venv_python],
               timeout=3600)
        # Best-effort: what the venv interpreter runs AS when spawned
        # fresh (can differ from the TD process under Rosetta). Pure
        # breadcrumb for the stamp; never blocks the install.
        spec['venv_arch'] = measure_venv_arch(venv_python)
        freeze_constraints(spec, uv, log)
        write_env_stamp(spec)
        log('Python environment ready', 'SUCCESS')
        return True

    except subprocess.CalledProcessError as e:
        log(f'Environment setup failed: {e.stderr or e}', 'ERROR')
        return False
    except Exception as e:
        log(f'Environment setup failed: {e}', 'ERROR')
        return False
    finally:
        if locked:
            release_install_lock(spec)


_FROZEN_LINE_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]*==\S+$')


def freeze_constraints(spec: dict, uv, log) -> bool:
    """Freeze the venv's resolved closure to the constraints file.

    Runs right after a successful CORE install, so the file is the
    transitive truth of a healthy MCP stack (pydantic, starlette, anyio,
    httpx, ...). install_extras passes it to uv as ``-c`` -- the 5 direct
    pins alone leave the transitive tree open to downgrades.

    Two filters (2026-08-19 review): only plain ``name==version`` lines
    (a ``pkg @ file://...`` direct reference makes the whole constraints
    file unusable), and the user's DECLARED extras are excluded -- a
    freeze after extras exist would pin the user's own packages and block
    their future upgrades while blaming "Embody's pinned core". Known
    residue: an extra's private transitive deps still freeze; the failure
    message names this file and the self-service fix.

    Best-effort: on failure extras fall back to the direct pins.
    """
    try:
        declared = {spec_dist_name(s) for s in
                    (spec.get('declared_extras') or [])}
        proc = run_uv(uv, ['pip', 'freeze', '--python', spec['venv_python']],
                      timeout=300)
        lines = []
        for ln in (proc.stdout or '').splitlines():
            ln = ln.strip()
            if not _FROZEN_LINE_RE.match(ln):
                continue
            if spec_dist_name(ln) in declared:
                continue
            lines.append(ln)
        if not lines:
            log('Constraints freeze returned nothing -- extras will '
                'constrain on the direct pins only', 'WARNING')
            return False
        with open(spec['constraints_path'], 'w', encoding='utf-8',
                  newline='\n') as f:
            f.write('\n'.join(lines) + '\n')
        return True
    except Exception as e:
        log(f'Constraints freeze failed ({e}) -- extras will constrain '
            f'on the direct pins only', 'WARNING')
        return False


def write_env_stamp(spec: dict) -> None:
    """Record what the venv was built for (dep spec + Python tag).

    WORKER-THREAD SAFE: pure filesystem write on paths precomputed in
    ``spec``. environment_needs_install compares this stamp against the
    current spec, which is what makes every Embody upgrade that changes a
    pin auto-upgrade every existing venv on its next Start. A torn or
    missing stamp just reads as needs-install -- self-healing. Extras
    bookkeeping (extras_applied / extras_failed) is carried forward from
    the existing stamp so a core reinstall never forgets user packages;
    the DECLARATION lives in .embody/project.json (committed), never here
    -- this file dies with every ``uv venv --clear``.
    """
    carried = {}
    try:
        with open(spec['stamp_path'], 'r', encoding='utf-8') as f:
            old = json.load(f)
        if isinstance(old, dict):
            for k in ('extras_applied', 'extras_failed',
                      'extras_failed_transient'):
                if k in old:
                    carried[k] = old[k]
    except Exception:
        pass
    stamp = {
        'schema': 1,
        'deps': sorted(spec['deps']),
        'python': spec['python_tag'],
        # TD-process architecture at build time -- the value the
        # rebuild trigger compares (loop-free: after a rebuild it
        # always equals the current process). Older readers ignore
        # unknown keys, so this is silently backward-compatible.
        'machine': spec.get('machine') or '',
        # What the venv interpreter reported when spawned fresh, or
        # the TD value when that probe failed. Diagnostic only.
        'arch': spec.get('venv_arch') or spec.get('machine') or '',
    }
    stamp.update(carried)
    try:
        # Atomic: three writers touch this file (core install worker,
        # extras worker, clear_extras_failures) and a torn read parses as
        # needs-install -- a plain open('w') race could trigger a
        # spurious multi-minute core reinstall (2026-08-19 review).
        _write_json_atomic(spec['stamp_path'], stamp)
    except Exception:
        pass  # unstamped venv reinstalls next Start -- never block install


def fix_pywin32_dlls(site_packages) -> None:
    """Copy pywin32 DLLs to win32/ so they're importable without
    post-install. Overwrites stale copies when the source is newer
    (2026-08-19: the old exists-check skipped updates, so a pywin32
    upgrade left version-skewed DLLs where sys.path actually resolves).
    """
    src_dir = os.path.join(site_packages, 'pywin32_system32')
    dst_dir = os.path.join(site_packages, 'win32')
    if not os.path.isdir(src_dir) or not os.path.isdir(dst_dir):
        return
    for dll in os.listdir(src_dir):
        if not dll.endswith('.dll'):
            continue
        src = os.path.join(src_dir, dll)
        dst = os.path.join(dst_dir, dll)
        try:
            if (not os.path.exists(dst)
                    or os.path.getmtime(src) > os.path.getmtime(dst)):
                shutil.copy2(src, dst)
        except OSError:
            pass  # a loaded DLL is locked on Windows -- next cold start wins


# ==========================================================================
# User extras: declaration (committed) + install (constrained, non-gating)
# ==========================================================================

def canonical_name(name: str) -> str:
    """PEP 503 normalization: case-insensitive, runs of -_. collapse to -."""
    return re.sub(r'[-_.]+', '-', (name or '').strip().lower())


def spec_dist_name(spec_str: str) -> str:
    """The canonical dist name a requirement spec targets, or ''."""
    m = _SPEC_NAME_RE.match(spec_str or '')
    return canonical_name(m.group(1)) if m else ''


def valid_requirement(spec_str: str) -> bool:
    """Strict gate: plain PEP 508 NAME-BASED requirements only.

    URL, VCS (git+...), direct-reference ('pkg @ https://...'), and
    filesystem forms are refused outright (2026-08-19 review: 'git+...'
    parsed as name 'git' and 'C:\\wheels\\numpy...whl' as name 'c', both
    sailing past the core-pin AND TD-bundled guards -- a URL wheel can
    carry any dist name it likes, so name checks mean nothing for them).
    """
    s = (spec_str or '').strip()
    if not s:
        return False
    if '://' in s or '@' in s or '/' in s or '\\' in s:
        return False
    m = _SPEC_NAME_RE.match(s)
    if not m:
        return False
    return bool(_SPEC_TAIL_RE.match(s[m.end():].strip()))


def read_declared_extras(project_root) -> list:
    """The committed extras declaration: ``python.extras`` in
    .embody/project.json. Returns a list of requirement strings (may be
    empty). Pure file read -- never raises, never writes.
    """
    path = os.path.join(str(project_root), '.embody', 'project.json')
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        extras = (data.get('python') or {}).get('extras')
        if not isinstance(extras, list):
            return []
        return [str(s) for s in extras if isinstance(s, str) and s.strip()]
    except Exception:
        return []


def read_allow_shadow_names(project_root) -> list:
    """The committed ``python.extras_allow_shadow`` name list -- the
    durable record that a TD-bundled shadow was a deliberate choice."""
    path = os.path.join(str(project_root), '.embody', 'project.json')
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        names = (data.get('python') or {}).get('extras_allow_shadow')
        if not isinstance(names, list):
            return []
        return [str(s) for s in names if isinstance(s, str) and s.strip()]
    except Exception:
        return []


def write_declared_extras(project_root, extras, log,
                          allow_shadow_names=None) -> bool:
    """Steward the ``python`` key of the COMMITTED .embody/project.json.

    Same discipline as embody_admin.write_project_json (key-level
    ownership): an unreadable or non-object file is NEVER overwritten
    blind -- co-writers' keys (convoy, future stewards) must survive any
    parse failure -- and the absent-file case uses an exclusive create so
    a co-writer landing the file first is never clobbered with our
    template. Owns exactly data['python']['extras'] (plus, when given,
    data['python']['extras_allow_shadow']); every other key is preserved.
    Never raises: any failure logs a WARNING and returns False.
    """
    path = os.path.join(str(project_root), '.embody', 'project.json')
    try:
        existing = {}
        created = not os.path.isfile(path)
        if not created:
            with open(path, 'r', encoding='utf-8') as f:
                raw = f.read()
            if raw.strip():
                try:
                    loaded = json.loads(raw)
                except ValueError as e:
                    log(f'.embody/project.json is unreadable ({e}) -- '
                        f'extras declaration not written. Fix or restore '
                        f'it from git.', 'WARNING')
                    return False
                if not isinstance(loaded, dict):
                    log('.embody/project.json is not a JSON object -- '
                        'extras declaration not written. Fix or restore '
                        'it from git.', 'WARNING')
                    return False
                existing = loaded
        python_key = existing.get('python')
        if not isinstance(python_key, dict):
            python_key = {}
        python_key['extras'] = sorted(set(str(s).strip() for s in extras
                                          if str(s).strip()))
        if allow_shadow_names is not None:
            merged = set(python_key.get('extras_allow_shadow') or []
                         if isinstance(python_key.get('extras_allow_shadow'),
                                       list) else [])
            merged |= {canonical_name(n) for n in allow_shadow_names
                       if str(n).strip()}
            python_key['extras_allow_shadow'] = sorted(merged)
        existing['python'] = python_key
        if created:
            # Exclusive create closes the check-then-write window
            # (embody_admin.write_project_json's A-14 guarantee).
            os.makedirs(os.path.dirname(path), exist_ok=True)
            try:
                with open(path, 'x', encoding='utf-8', newline='\n') as f:
                    f.write(json.dumps(existing, indent=2) + '\n')
                return True
            except FileExistsError:
                log('.embody/project.json appeared mid-create (co-writer) '
                    '-- retry the extras request', 'WARNING')
                return False
        _write_json_atomic(path, existing)
        return True
    except Exception as e:
        log(f'Failed to write the extras declaration: {e}', 'WARNING')
        return False


def read_acknowledged_extras(project_root) -> list:
    """Machine-local consent record: ``python_acknowledged_extras`` in
    .embody/local.json (gitignored, survives ``uv venv --clear``).

    The declaration travels in git, so WITHOUT this gate a pulled
    project.json would auto-install packages -- including sdists running
    build code -- on every collaborator's machine at project open
    (2026-08-19 review, BLOCKER). Only specs this machine consented to
    (via InstallPackages or ApplyDeclaredExtras) reconcile automatically;
    the rest surface as a WARNING telling the user how to apply them.
    """
    path = os.path.join(str(project_root), '.embody', 'local.json')
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        acked = data.get('python_acknowledged_extras')
        if not isinstance(acked, list):
            return []
        return [str(s) for s in acked if isinstance(s, str) and s.strip()]
    except Exception:
        return []


def acknowledge_extras(project_root, specs, log) -> bool:
    """Record consent for specs in machine-local .embody/local.json.

    local.json is a regenerable machine-local cache (same contract as
    embody_admin.write_local_json's td_build pin): corrupt content is
    recreated with a warning, readable foreign keys are merge-preserved.
    """
    path = os.path.join(str(project_root), '.embody', 'local.json')
    existing = {}
    if os.path.isfile(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                existing = loaded
        except ValueError as e:
            # Genuine corruption: recreate (machine-local cache contract).
            log(f'.embody/local.json was unreadable ({e}) -- recreating '
                f'the machine-local file', 'WARNING')
        except OSError as e:
            # A transient lock (AV scanner, co-writer) is NOT corruption:
            # recreating here would drop foreign keys like td_build.
            log(f'.embody/local.json is momentarily unreadable ({e}) -- '
                f'consent not recorded; retry the request', 'WARNING')
            return False
    acked = set(existing.get('python_acknowledged_extras') or []
                if isinstance(existing.get('python_acknowledged_extras'),
                              list) else [])
    acked |= {str(s).strip() for s in specs if str(s).strip()}
    existing['python_acknowledged_extras'] = sorted(acked)
    try:
        _write_json_atomic(path, existing)
        return True
    except Exception as e:
        log(f'Failed to record extras consent: {e}', 'WARNING')
        return False


def td_bundled_dist_names(venv_dir) -> set:
    """Canonical dist names TD's OWN site-packages carries (runtime
    enumeration of the site-packages dirs UNDER sys.base_prefix -- i.e.
    shipped with the interpreter -- plus the known load-bearing seed). A venv extra shadowing one of
    these at sys.path[0] is a process-abort class: TD's C++ ops link
    against specific builds (numpy in Script TOP/CHOP/DAT interop --
    Derivative-confirmed crash class, fixed on their side only for
    matching versions).
    """
    # Session cache: this scans every site-packages on sys.path, and it
    # gets called from main-thread reconcile paths (2026-08-19 review:
    # the "pure JSON reads" claim upstream was false without this). The
    # bundled set only changes on a TD upgrade, i.e. never mid-session.
    venv_norm = (os.path.normcase(os.path.normpath(venv_dir))
                 if venv_dir else None)
    # "Bundled" means SHIPPED WITH THE INTERPRETER. Enumerating every
    # site-packages on sys.path instead credited TouchDesigner with a
    # user's --user install, the "Python 64-bit Module Path" preference
    # dir, or a PREVIOUSLY-OPENED project's venv (sys.path only ever
    # grows -- see add_site_packages), and then refused the package as
    # "ships inside TouchDesigner itself". False, and clearable only via
    # allow_shadow=True, which gets COMMITTED to project.json and travels
    # to the whole team (field 2026-08-23).
    base_norm = (os.path.normcase(os.path.normpath(sys.base_prefix))
                 if getattr(sys, 'base_prefix', None) else None)
    cache = getattr(sys, '_embody_pyenv_bundled_cache', None)
    if isinstance(cache, dict) and cache.get('key') == venv_norm:
        return set(cache['names'])
    names = set(TD_BUNDLED_SEED)
    # Separator-terminated prefix, not bare startswith: 'C:\p\.venv2'
    # must not match 'C:\p\.venv' (2026-08-19 review); empty venv_dir
    # normpaths to '.' and would swallow relative entries -- skip it.
    for entry in list(sys.path):
        try:
            if not entry or 'site-packages' not in entry.replace('\\', '/'):
                continue
            entry_norm = os.path.normcase(os.path.normpath(entry))
            if venv_norm and (entry_norm == venv_norm
                              or entry_norm.startswith(venv_norm + os.sep)):
                continue  # our own venv is not "bundled"
            if base_norm and not (entry_norm == base_norm
                                  or entry_norm.startswith(base_norm + os.sep)):
                continue  # outside the interpreter's install: not TD's
            if not os.path.isdir(entry):
                continue
            for p in os.listdir(entry):
                if p.endswith('.dist-info') and '-' in p:
                    names.add(canonical_name(p[:-len('.dist-info')]
                                             .rsplit('-', 1)[0]))
        except OSError:
            continue
    sys._embody_pyenv_bundled_cache = {'key': venv_norm,
                                       'names': sorted(names)}
    return names


def check_extras_specs(specs, spec, allow_shadow=False) -> tuple:
    """Validate requested extras: (accepted, refused: {spec: reason}).

    Refuses non-name-based requirement forms (URL/VCS/path -- they carry
    arbitrary dist names past every check), core-protected names always
    (the constraints file is the enforcement of last resort, not an
    invitation), and TD-bundled names unless ``allow_shadow=True`` --
    shadowing TD's own numpy/opencv from sys.path[0] is the #1 crash
    class of sideloaded environments.
    """
    core = set(CORE_PROTECTED_SEED)
    for dep in spec.get('deps') or []:
        core.add(spec_dist_name(dep))
    bundled = td_bundled_dist_names(spec.get('venv_dir'))
    accepted, refused = [], {}
    for s in specs:
        name = spec_dist_name(s)
        if not valid_requirement(s):
            refused[s] = (
                'only plain name-based requirements are accepted '
                '(no URLs, VCS refs, direct references, or file paths)')
        elif name in core:
            refused[s] = (
                f'{name} is part of Embody\'s pinned core stack -- its '
                f'version is managed by Embody releases and cannot be '
                f'changed per-project')
        elif name in bundled and not allow_shadow:
            refused[s] = (
                f'{name} ships inside TouchDesigner itself; a different '
                f'version loaded from the project venv can crash TD ops '
                f'that link against the bundled build. Pass '
                f'allow_shadow=True only if you accept that risk')
        else:
            accepted.append(s)
    return accepted, refused


def core_refused_specs(spec: dict, declared, allow_shadow_names=None) -> dict:
    """Declared entries refused at RECONCILE time (2026-08-19): the
    declaration is a committed file, so a hand edit or a git pull must
    not bypass any boundary InstallPackages enforces. Refuses invalid
    forms (URL/VCS/path), core-pin names, and TD-bundled names whose
    canonical name is NOT in ``allow_shadow_names`` (the persisted
    python.extras_allow_shadow list -- the record that the shadow was a
    deliberate local choice, so it survives rebuilds while a pulled
    stranger's shadow does not). Returns {spec: reason}.
    """
    core = set(CORE_PROTECTED_SEED)
    for dep in spec.get('deps') or []:
        core.add(spec_dist_name(dep))
    allow = {canonical_name(n) for n in (allow_shadow_names or [])}
    bundled = td_bundled_dist_names(spec.get('venv_dir'))
    refused = {}
    for s in declared or []:
        name = spec_dist_name(s)
        if not valid_requirement(s):
            refused[s] = (
                f'{s!r} in .embody/project.json is not a plain name-based '
                f'requirement -- URL/VCS/path forms are never installed')
        elif name in core:
            refused[s] = (
                f'{name} is part of Embody\'s pinned core stack -- '
                f'declared in .embody/project.json but not installable')
        elif name in bundled and name not in allow:
            refused[s] = (
                f'{name} shadows a TouchDesigner-bundled package and is '
                f'not in python.extras_allow_shadow -- install it via '
                f'op.Embody.InstallPackages(..., allow_shadow=True) to '
                f'record the opt-in')
    return refused


def extras_status(spec: dict, declared, acknowledged=None,
                  allow_shadow_names=None) -> dict:
    """Declared-vs-applied delta, from pure JSON reads (cheap enough for
    any main-thread call).

    Gates, in order: reconcile-time validation (invalid / core-pin /
    un-opted shadow -> ``refused``); machine-local consent (declared but
    never acknowledged on THIS machine -> ``unacknowledged``, never
    auto-installed -- a pulled declaration is not consent); permanent
    resolver failures suppress retry until the declaration string
    changes; transient failures (locks, network) retry up to 3 attempts.
    Pass ``acknowledged=None`` to skip the consent gate (trusted caller).
    """
    applied, failed, transient = [], {}, {}
    try:
        with open(spec['stamp_path'], 'r', encoding='utf-8') as f:
            stamp = json.load(f)
        if isinstance(stamp, dict):
            applied = [s for s in (stamp.get('extras_applied') or [])
                       if isinstance(s, str)]
            failed = {k: v for k, v in (stamp.get('extras_failed') or {}).items()
                      if isinstance(k, str)}
            transient = {k: v for k, v in
                         (stamp.get('extras_failed_transient') or {}).items()
                         if isinstance(k, str) and isinstance(v, dict)}
    except Exception:
        pass
    declared = [str(s) for s in (declared or [])]
    refused = core_refused_specs(spec, declared, allow_shadow_names)
    unacknowledged = []
    if acknowledged is not None:
        acked = set(acknowledged)
        unacknowledged = [s for s in declared
                          if s not in acked and s not in refused]
    exhausted = sorted(s for s, rec in transient.items()
                       if int(rec.get('attempts') or 0) >= 3
                       and s in declared)
    # Transient failures back off: no retry within 10 minutes of the
    # last attempt, or the reconcile drain would burn all 3 attempts in
    # seconds and then go quiet (2026-08-19 review). Pure clock read.
    now = time.time()
    cooling = {s for s, rec in transient.items()
               if now - float(rec.get('t') or 0) < 600}
    # 'applied' is a stamp CLAIM, never evidence. A targeted uninstall --
    # including the `uv pip uninstall` line Embody's OWN log prints -- left
    # the claim behind, so a declared extra read as satisfied forever while
    # the venv lacked it: no reinstall, ModuleNotFoundError at runtime, and
    # a fresh clone silently diverging from the box that ran the uninstall
    # (field 2026-08-23). Verify against site-packages; anything missing
    # falls back into to_install. Fails SAFE -- an absent or unreadable
    # site-packages keeps the stamp's word rather than mass-reinstalling.
    verify_dir = spec.get('site_packages')
    if applied and verify_dir and os.path.isdir(verify_dir):
        present = dist_info_snapshot(verify_dir)
        if present:
            applied = [a for a in applied
                       if not spec_dist_name(a)
                       or spec_dist_name(a) in present]

    to_install = [s for s in declared
                  if s not in applied and s not in failed
                  and s not in refused and s not in unacknowledged
                  and s not in exhausted and s not in cooling]
    return {'applied': applied, 'failed': failed, 'transient': transient,
            'declared': declared, 'refused': refused,
            'unacknowledged': unacknowledged, 'exhausted': exhausted,
            'to_install': to_install}


def clear_extras_failures(spec: dict, specs) -> None:
    """Drop failure records (permanent AND transient) for re-requested
    specs so extras_status retries them now. Best-effort, atomic."""
    try:
        with open(spec['stamp_path'], 'r', encoding='utf-8') as f:
            stamp = json.load(f)
        if not isinstance(stamp, dict):
            return
        failed = stamp.get('extras_failed') or {}
        transient = stamp.get('extras_failed_transient') or {}
        changed = False
        for s in specs:
            if s in failed:
                del failed[s]
                changed = True
            if s in transient:
                del transient[s]
                changed = True
        if changed:
            stamp['extras_failed'] = failed
            stamp['extras_failed_transient'] = transient
            _write_json_atomic(spec['stamp_path'], stamp)
    except Exception:
        pass


def dist_info_snapshot(site_packages) -> dict:
    """{canonical dist name: version} from *.dist-info directory names."""
    snap = {}
    try:
        for p in glob(os.path.join(glob_escape(site_packages), '*.dist-info')):
            base = os.path.basename(p)[:-len('.dist-info')]
            if '-' not in base:
                continue
            name, ver = base.rsplit('-', 1)
            snap[canonical_name(name)] = ver
    except Exception:
        pass
    return snap


def loaded_changed_dists(before, after, site_packages) -> list:
    """Dists whose on-disk version changed -- OR that are NEWLY installed
    -- while their top-level modules are already imported in this
    interpreter: the restart-required set. Keyed on loaded distributions,
    not requested names: an innocuous extra can bump pydantic_core
    transitively, and hot-swapping a live Rust extension is the
    documented abort() vector. New dists matter for the allow_shadow
    case: a first-time venv numpy under an already-imported TD numpy is
    exactly the frame where the user must be told to restart
    (2026-08-19 review).
    """
    changed = [n for n, v in after.items()
               if n not in before or before[n] != v]
    loaded = []
    for dist in changed:
        mods = _top_level_modules(site_packages, dist)
        if any(m in sys.modules for m in mods):
            loaded.append(dist)
    return loaded


# Dists whose import name differs from the dist name AND that commonly
# ship without top_level.txt -- exactly the TD-bundled shadow candidates
# where the restart warning matters most (2026-08-19 review).
_IMPORT_NAME_ALIASES = {
    'pillow': ['PIL'],
    'opencv-python': ['cv2'],
    'opencv-python-headless': ['cv2'],
    'pyyaml': ['yaml'],
    'beautifulsoup4': ['bs4'],
    'scikit-learn': ['sklearn'],
}


def _top_level_modules(site_packages, dist) -> list:
    """Importable top-level names a dist owns: top_level.txt when present,
    else known aliases, else the normalized dist name."""
    names = []
    try:
        for p in glob(os.path.join(glob_escape(site_packages), '*.dist-info')):
            base = os.path.basename(p)[:-len('.dist-info')]
            if '-' not in base:
                continue
            name, _ = base.rsplit('-', 1)
            if canonical_name(name) != dist:
                continue
            tl = os.path.join(p, 'top_level.txt')
            if os.path.isfile(tl):
                with open(tl, 'r', encoding='utf-8') as f:
                    names = [ln.strip() for ln in f if ln.strip()]
            break
    except Exception:
        pass
    return names or _IMPORT_NAME_ALIASES.get(dist) or [dist.replace('-', '_')]


def install_extras(spec: dict, to_install, log) -> dict:
    """Install declared extras into the venv, constrained by the frozen
    core closure. WORKER-THREAD SAFE (subprocess + filesystem only).

    Non-gating by contract: failures are recorded per-spec in the stamp
    and NEVER affect the core needs-install verdict. Resolver failures
    are permanent (suppressed until the declaration changes or the spec
    is re-requested); IO/lock/timeout failures are transient (retried on
    later reconciles, max 3 attempts). Batch first; on a batch failure
    each spec retries alone so one conflicting package cannot sink its
    neighbors. Hardened uv invocation: --no-config, --no-build (sdist
    build hooks are arbitrary code -- wheel-less packages fail with an
    actionable message), scrubbed UV_*/PIP_* env. Returns {'installed',
    'failed', 'transient', 'restart_required', 'deferred'}.
    """
    result = {'installed': [], 'failed': {}, 'transient': {},
              'restart_required': [], 'deferred': ''}
    if not to_install:
        return result
    uv = resolve_uv()
    if not uv:
        # resolve-only on purpose: find_or_install_uv can block on a pip
        # subprocess and this may run at project open. Core bootstrap is
        # the path that installs uv; if it never ran, extras wait.
        result['deferred'] = 'uv not found (core bootstrap installs it)'
        return result
    if not acquire_install_lock(spec, 'extras'):
        # Another instance (or the core bootstrap) owns the venv right
        # now. Deferred, not failed: the caller re-arms, nothing is
        # recorded, and the specs retry cleanly next reconcile.
        result['deferred'] = ('another install holds this project\'s '
                              'environment -- will retry')
        return result
    pins_tmp = ''
    try:
        constraints = []
        if os.path.isfile(spec.get('constraints_path') or ''):
            constraints = ['-c', spec['constraints_path']]
        elif spec.get('deps'):
            # Fallback protection: the direct pins, written to a
            # constraints file (uv takes -c <file> only). Weaker than the
            # frozen closure but never nothing.
            pins_tmp = os.path.join(spec['venv_dir'], 'embody-pins.tmp.txt')
            try:
                with open(pins_tmp, 'w', encoding='utf-8', newline='\n') as f:
                    f.write('\n'.join(spec['deps']) + '\n')
                constraints = ['-c', pins_tmp]
            except OSError as e:
                # NEVER install unconstrained: a core spec exists but no
                # constraints could be written -- defer (transient) and
                # retry when the filesystem cooperates.
                result['deferred'] = (f'could not write the constraints '
                                      f'file ({e}) -- will retry')
                return result
        before = dist_info_snapshot(spec['site_packages'])

        def _attempt(batch):
            run_uv(uv, ['pip', 'install', '--no-build'] + list(batch)
                   + constraints + ['--python', spec['venv_python']],
                   timeout=1800, hardened=True)

        def _classify(exc, s):
            reason = _failure_reason(exc, s, spec)
            if _is_resolver_failure(exc):
                result['failed'][s] = reason
            else:
                result['transient'][s] = reason

        try:
            log(f'Installing project extras ({", ".join(to_install)})...')
            _attempt(to_install)
            result['installed'] = list(to_install)
        except (subprocess.CalledProcessError,
                subprocess.TimeoutExpired):
            # One bad spec fails the whole resolve -- isolate per spec so
            # the good ones land and the failure names the culprit.
            for s in to_install:
                try:
                    _attempt([s])
                    result['installed'].append(s)
                except Exception as e:
                    _classify(e, s)
        except Exception as e:
            for s in to_install:
                _classify(e, s)

        after = dist_info_snapshot(spec['site_packages'])
        if sys.platform.startswith('win'):
            # A pywin32 extra-set upgrade must not leave stale copied DLLs.
            fix_pywin32_dlls(spec['site_packages'])
        result['restart_required'] = loaded_changed_dists(
            before, after, spec['site_packages'])
        _record_extras_outcome(spec, result)
    finally:
        release_install_lock(spec)
        if pins_tmp and os.path.isfile(pins_tmp):
            try:
                os.unlink(pins_tmp)
            except OSError:
                pass
    for s in result['installed']:
        log(f'Extra installed: {s}', 'SUCCESS')
    for s, reason in result['failed'].items():
        log(f'Extra FAILED: {s} -- {reason}', 'ERROR')
    for s, reason in result['transient'].items():
        log(f'Extra deferred: {s} -- {reason} (will retry)', 'WARNING')
    for dist in result['restart_required']:
        log(f'{dist} changed on disk but a different build is already '
            f'imported in this session. Save your work and restart '
            f'TouchDesigner to finish the change.', 'WARNING')
    return result


def _is_resolver_failure(exc) -> bool:
    """Resolver verdicts are permanent; everything else (locks, network,
    timeouts, sharing violations) is transient and worth retrying on a
    later cold start (2026-08-19 review: a one-off Windows sharing
    violation must not become a forever-suppressed package)."""
    text = (getattr(exc, 'stderr', '') or str(exc)).lower()
    return any(marker in text for marker in (
        'no solution', 'not found in the package registry',
        'no matching distribution', 'no wheels are available',
        'requires-python', 'is not a valid requirement',
        'failed to parse'))


def _failure_reason(exc, spec_str, spec) -> str:
    """A first-class message out of uv's multi-paragraph failure output.
    Never blames "Embody's pinned core" blind -- the constraints file
    also carries frozen versions of an extra's transitive deps, and a
    stale one is self-serviceable (2026-08-19 review)."""
    text = (getattr(exc, 'stderr', '') or str(exc)).strip()
    if isinstance(exc, subprocess.TimeoutExpired):
        return 'install timed out'
    low = text.lower()
    if 'no wheels are available' in low or '--no-build' in low:
        return (
            f'{spec_str} has no pre-built wheel for this Python; Embody '
            f'installs wheels only (source builds execute arbitrary '
            f'code). Pick a version with wheels, or install it into a '
            f'separate environment.')
    if 'no solution' in low:
        return (
            f'{spec_str} conflicts with the pinned environment. The pins '
            f'live in {os.path.basename(spec.get("constraints_path") or "")}'
            f' (protecting the MCP stack) -- if this package worked '
            f'before, the snapshot may be stale: delete that file from '
            f'.venv/ and retry to re-resolve against the direct pins. '
            f'Resolver said: ...{text[-300:]}')
    return f'...{text[-300:]}' if len(text) > 300 else (text or 'install failed')


def _record_extras_outcome(spec: dict, result: dict) -> None:
    """Merge an install outcome into the stamp's extras bookkeeping.
    Permanent failures land in extras_failed; transient ones in
    extras_failed_transient with an attempt counter (retried until 3).
    Atomic read-modify-write; a missing stamp gets a minimal one."""
    try:
        stamp = {}
        try:
            with open(spec['stamp_path'], 'r', encoding='utf-8') as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                stamp = loaded
        except Exception:
            pass
        applied = set(s for s in (stamp.get('extras_applied') or [])
                      if isinstance(s, str))
        failed = {k: v for k, v in (stamp.get('extras_failed') or {}).items()
                  if isinstance(k, str)}
        transient = {k: v for k, v in
                     (stamp.get('extras_failed_transient') or {}).items()
                     if isinstance(k, str) and isinstance(v, dict)}
        for s in result.get('installed', []):
            applied.add(s)
            failed.pop(s, None)
            transient.pop(s, None)
        for s, reason in (result.get('failed') or {}).items():
            failed[s] = str(reason)[:400]
            applied.discard(s)
            transient.pop(s, None)
        for s, reason in (result.get('transient') or {}).items():
            rec = transient.get(s) or {}
            transient[s] = {'reason': str(reason)[:400],
                            'attempts': int(rec.get('attempts') or 0) + 1,
                            't': time.time()}
            applied.discard(s)
        stamp['extras_applied'] = sorted(applied)
        stamp['extras_failed'] = failed
        stamp['extras_failed_transient'] = transient
        _write_json_atomic(spec['stamp_path'], stamp)
    except Exception:
        pass  # bookkeeping must never fail an install


# ==========================================================================
# tdPyEnvManager co-existence detection (read-only, warn-don't-mutate --
# Embody's OWN context authoring lives in the section below)
# ==========================================================================

def detect_tdpyenvmanager(project_root, venv_dir, app_obj=None,
                          extra_dirs=None) -> dict:
    """Classify any tdPyEnvManager presence in this project. READ-ONLY --
    Embody never writes, flips, or deletes another manager's context.
    ``app_obj`` is TD's ``app`` global, passed by the main-thread facade
    (TD constructs app.pyEnvHelper in every session and links any context
    file found in cwd BEFORE any COMP cooks). Returns
    {'present': bool, 'kind': 'none'|'ours_linked'|'ours_unlinked'|
     'same_venv'|'different_env'|'conda', 'env_path': str|None,
     'context_files': [...]} -- the ours_* kinds mean every context found
    is Embody's own (see classify_td_context below).
    """
    finding = {'present': False, 'kind': 'none', 'env_path': None,
               'context_files': []}
    root = str(project_root or '')
    # The helper resolves its context from CWD (= the .toe folder), which
    # differs from Embody's project root when the .toe sits in a repo
    # subfolder -- scan both (extra_dirs carries project.folder).
    dirs, seen_dirs = [], set()
    for d in [root] + [str(x) for x in (extra_dirs or []) if x]:
        key = os.path.normcase(os.path.normpath(d))
        if key not in seen_dirs:
            seen_dirs.add(key)
            dirs.append(d)
    for d in dirs:
        for fname in ('TDPyEnvManagerContext.yaml',
                      'TDPyEnvManagerContext.json'):
            p = os.path.join(d, fname)
            if os.path.isfile(p) and p not in finding['context_files']:
                finding['context_files'].append(p)
        pyproject = os.path.join(d, 'pyproject.toml')
        try:
            if os.path.isfile(pyproject):
                with open(pyproject, 'r', encoding='utf-8',
                          errors='replace') as f:
                    if ('[tool.touchdesigner.TDPyEnvManagerContext]'
                            in f.read()):
                        finding['context_files'].append(pyproject)
        except OSError:
            pass
    env_path = None
    conda = False
    helper = getattr(app_obj, 'pyEnvHelper', None) if app_obj else None
    if helper is not None:
        try:
            linked = bool(getattr(helper, 'HasLinkedCustomEnv', False))
            raw = getattr(helper, 'envPath', None)
            if linked and raw:
                env_path = str(raw)
        except Exception:
            pass
    if finding['context_files'] and not conda:
        # Cheap mode sniff without a yaml/toml dependency (neither may be
        # wired yet at init). Covers pyproject.toml too -- the regex
        # matches both YAML ('mode: Conda Env') and TOML ('mode = "Conda
        # Env"') spellings. Best-effort only.
        for fpath in finding['context_files']:
            try:
                with open(fpath, 'r', encoding='utf-8',
                          errors='replace') as f:
                    text = f.read()
                if re.search(r'mode["\']?\s*[:=]\s*["\']?conda', text,
                             re.IGNORECASE):
                    conda = True
            except OSError:
                pass
    if not env_path and not finding['context_files']:
        return finding
    finding['present'] = True
    finding['env_path'] = env_path
    # 'ours' only when EVERY context found is Embody's own -- a coexisting
    # foreign file keeps the loud different_env warning.
    owned = bool(finding['context_files']) and all(
        p.endswith(TD_CONTEXT_FILENAME)
        and classify_td_context(os.path.dirname(p), venv_dir) == 'ours'
        for p in finding['context_files'])
    if conda:
        finding['kind'] = 'conda'
    elif env_path:
        same = (os.path.normcase(os.path.normpath(env_path))
                == os.path.normcase(os.path.normpath(str(venv_dir or ''))))
        if same:
            # Embody's own context linked = the healthy expected state
            # (silent); a user-authored context on .venv keeps its INFO.
            finding['kind'] = 'ours_linked' if owned else 'same_venv'
        else:
            finding['kind'] = 'different_env'
    else:
        # Context file exists but nothing is linked (or the helper is
        # unreadable) -- still worth a note: active:false does NOT stop
        # tdPyEnvManager's startup link (verified against the shipped
        # helper source, 2026-08-19), so a stale context keeps acting.
        # Embody's own unlinked context gets its own diagnostic (see
        # tdpyenvmanager_notice).
        finding['kind'] = 'ours_unlinked' if owned else 'different_env'
    return finding


def tdpyenvmanager_notice(finding: dict) -> 'tuple | None':
    """(level, message) for a detection finding, or None when silent."""
    if not finding.get('present'):
        return None
    kind = finding.get('kind')
    if kind == 'ours_linked':
        return None  # Embody's own pre-cook link, healthy -- silence
    if kind == 'same_venv':
        return ('INFO', (
            'tdPyEnvManager is active on this project and points at '
            'Embody\'s own .venv -- the benign configuration. Its PATH/DLL '
            'handling stacks harmlessly on Embody\'s.'))
    if kind == 'conda':
        return ('WARNING', (
            'tdPyEnvManager manages a CONDA environment on this project. '
            'Embody manages pip packages in .venv only; both environments '
            'sit on sys.path, and name collisions resolve unpredictably. '
            'Conda stays under tdPyEnvManager\'s control -- Embody will '
            'not convert or touch it.'))
    if kind == 'ours_unlinked':
        return ('WARNING', (
            'Embody\'s TD pre-cook venv context exists but TouchDesigner '
            'did not link it this session -- module-level venv imports in '
            'other extensions fall back to Embody\'s later wiring. Check '
            'TDLogs/TDPyEnvManagerHelper_<pid>.log: it only exists when '
            'the link was REFUSED; if it is absent, TD\'s working '
            f'directory at launch (now {os.getcwd()}) was not the '
            'context\'s folder, so the helper never saw the file.'))
    env_path = finding.get('env_path') or 'unknown'
    return ('WARNING', (
        f'tdPyEnvManager is linked to a DIFFERENT environment '
        f'({env_path}) while Embody manages .venv. Python modules will '
        f'resolve from Embody\'s venv (later sys.path insert) but native '
        f'DLLs may resolve from the other env -- a hard-to-diagnose '
        f'split. Recommend pointing tdPyEnvManager at .venv (its own '
        f'default) or importing its packages into Embody\'s environment '
        f'(op.Embody.InstallPackages). Note: setting active:false in its '
        f'context file does NOT stop the startup link -- remove the '
        f'context file to fully hand off.'))


# ==========================================================================
# TD pre-cook venv context authoring (TDPyEnvManagerContext.yaml)
# ==========================================================================
# TD's app.pyEnvHelper links the context found in the process CWD BEFORE any
# COMP cooks; TD chdirs to the .toe folder at launch (verified live
# 2026-08-20), so a context in project.folder covers every launch surface.
# This closes the cold-open race the extension-time wiring cannot: a sibling
# extension's module-level venv import constructing before Embody (the
# node-pa-td outage, docs/reports/feature-td-pyenv-context.md). envPath in
# the file is IGNORED by TD -- it recomputes from envName + installPath,
# both CWD-relative -- so the content is machine-independent and gitignored.
# Stance: write only when no context exists or the existing one is already
# ours; a foreign context is NEVER touched (EmbodyExt._ensurePyEnvContext
# owns the gating: guard, manifest tombstone, gitignore entry).

TD_CONTEXT_FILENAME = 'TDPyEnvManagerContext.yaml'


def render_td_context(python_tag) -> str:
    """The minimal context TD needs to link <cwd>/.venv pre-cook.
    Hand-rolled ASCII YAML: stable key order, no yaml import (this module
    bootstraps the venv that carries PyYAML). autoSetup stays false --
    Embody owns provisioning; TD's auto-setup must never race it."""
    return (
        'contextVersion: 2\n'
        'mode: Python vEnv\n'
        'envName: .venv\n'
        'installPath: .\n'
        f"pythonVersion: '{python_tag}'\n"
        'autoSetup: false\n'
    )


def _td_context_value(text, key) -> 'str | None':
    """One TOP-LEVEL scalar from context YAML/TOML text, quotes stripped.
    Regex, not yaml/toml -- neither parser may be wired at extension-init
    time. Column-0 anchor: an indented duplicate under a nested mapping
    must never shadow the real key (that flipped foreign to ours in
    review, 2026-08-20)."""
    m = re.search(rf'(?m)^{key}\s*[:=]\s*(.+?)\s*$', text)
    if not m:
        return None
    v = m.group(1).strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
        v = v[1:-1]
    return v


def classify_td_context(context_dir, venv_dir) -> str:
    """Ownership verdict for the TD env context in ``context_dir``:
    'absent', 'ours' (a Python-vEnv context resolving to ``venv_dir``), or
    'foreign' (pyproject section, legacy JSON, conda, another env, or
    unreadable -- all hands-off). Mirrors the shipped helper's resolution:
    pyproject wins the precedence, an envName carrying a path separator is
    a path of its own, and relative paths resolve against the context's
    directory (= TD's CWD at load time)."""
    d = str(context_dir or '')
    try:
        py = os.path.join(d, 'pyproject.toml')
        if os.path.isfile(py):
            with open(py, 'r', encoding='utf-8', errors='replace') as f:
                # Bare-name substring: the helper LOADS via tomllib, which
                # also accepts the inline-table spelling its own section
                # regex misses. Over-matching fails safe (hands-off).
                if 'TDPyEnvManagerContext' in f.read():
                    return 'foreign'
    except OSError:
        pass
    yaml_path = os.path.join(d, TD_CONTEXT_FILENAME)
    if not os.path.isfile(yaml_path):
        # Legacy JSON matters only when no yaml exists -- the helper
        # migrates/reads it ONLY then (postInit:106), so a stray JSON
        # beside Embody's yaml must not orphan the live context.
        if os.path.isfile(os.path.join(d, 'TDPyEnvManagerContext.json')):
            return 'foreign'
        return 'absent'
    try:
        with open(yaml_path, 'r', encoding='utf-8', errors='replace') as f:
            text = f.read()
    except OSError:
        return 'foreign'
    mode = _td_context_value(text, 'mode')
    # Missing/null mode = the helper's vEnv default (linkEnv treats any
    # non-'Conda Env' mode as vEnv; TD can write-back 'mode: null').
    if mode is not None and mode.lower() not in ('python venv', 'null',
                                                 '~', ''):
        return 'foreign'
    env_name = _td_context_value(text, 'envName')
    if not env_name:
        return 'foreign'
    if os.path.isabs(env_name) or '/' in env_name or '\\' in env_name:
        env_path = env_name if os.path.isabs(env_name) \
            else os.path.join(d, env_name)
    else:
        install = _td_context_value(text, 'installPath') or '.'
        root = install if os.path.isabs(install) else os.path.join(d, install)
        env_path = os.path.join(root, env_name)
    same = (os.path.normcase(os.path.normpath(env_path))
            == os.path.normcase(os.path.normpath(str(venv_dir or ''))))
    return 'ours' if same else 'foreign'


def td_context_status(project_dir, venv_dir, python_tag) -> str:
    """Ensure-step verdict: 'foreign' (never touch), 'ok' (ours, current),
    'refresh' (ours but pythonVersion drifted -- the posix site-packages
    path derives from it), or 'absent'."""
    kind = classify_td_context(project_dir, venv_dir)
    if kind != 'ours':
        return kind
    try:
        with open(os.path.join(project_dir, TD_CONTEXT_FILENAME), 'r',
                  encoding='utf-8', errors='replace') as f:
            text = f.read()
    except OSError:
        return 'refresh'
    current = _td_context_value(text, 'pythonVersion') == str(python_tag)
    return 'ok' if current else 'refresh'


def write_td_context(project_dir, python_tag) -> str:
    """Serialize Embody's context file (atomic: tmp + os.replace, so a
    crash mid-write cannot strand a half-file classify calls foreign);
    returns its path. The caller owns every gate (classify / guard /
    tombstone)."""
    path = os.path.join(project_dir, TD_CONTEXT_FILENAME)
    tmp = path + '.embody-tmp'
    with open(tmp, 'w', encoding='utf-8', newline='\n') as f:
        f.write(render_td_context(python_tag))
    os.replace(tmp, path)
    return path


def refresh_td_context(project_dir, python_tag) -> str:
    """Update ONLY pythonVersion in an existing (ours) context, preserving
    keys TD or the palette wrote back (extraPaths, autoSetupReqs, active,
    ...). Atomic like write_td_context; full render when the file cannot
    be read or the key line is missing."""
    path = os.path.join(project_dir, TD_CONTEXT_FILENAME)
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            text = f.read()
    except OSError:
        return write_td_context(project_dir, python_tag)
    new_line = f"pythonVersion: '{python_tag}'"
    updated, n = re.subn(r'(?m)^pythonVersion\s*:.*$', new_line, text,
                         count=1)
    if not n:
        updated = text.rstrip('\n') + '\n' + new_line + '\n'
    tmp = path + '.embody-tmp'
    with open(tmp, 'w', encoding='utf-8', newline='\n') as f:
        f.write(updated)
    os.replace(tmp, path)
    return path


def remove_td_context_if_ours(project_dir, venv_dir, log=None) -> bool:
    """Delete Embody's own context: TD must not pre-cook-link a venv
    Embody itself refuses to wire (stale-ABI wheels). Foreign contexts
    are never touched. A removal FAILURE is loud via ``log`` -- a stale
    context TD keeps linking must never disappear silently."""
    path = os.path.join(project_dir, TD_CONTEXT_FILENAME)
    if classify_td_context(project_dir, venv_dir) != 'ours':
        return False
    try:
        os.remove(path)
        return True
    except OSError as e:
        if log:
            log(f'Could not remove the TD pre-cook venv context at '
                f'{path}: {e} -- TD may keep linking a stale venv.',
                'WARNING')
        return False
