"""Installing, supervising and removing the Convoy host app -- TD side.

Lives INSIDE the Embody COMP beside convoy_client, for the same reason:
dev/convoy/ exists only in this checkout, so a released .tox has no such
path and ConvoyExt cannot import anything from it. convoy_client is the
twin of convoy_hostprobe (how to FIND a running host app); this module is
its counterpart for the other half of the story -- how one comes to
exist, stay alive across logins, and go away cleanly.

Stdlib only, TD-import-free, every platform branch injectable. It is
imported by plain pytest with no TouchDesigner present (that is what puts
it on the windows+macos CI matrix), and every long-running function is
called from a WORKER THREAD, so it must never touch an operator, a
parameter, or any other main-thread TD object. Nothing here does. The
module contents (the nine host-app sources) arrive as PLAIN STRINGS the
main thread already read off the DATs -- this module never looks one up.

WHAT IS BEING INSTALLED, stated plainly because the code should not read
softer than the dialog: a small Python program written into the per-user
data dir, plus a PER-USER Scheduled Task (macOS: LaunchAgent) that starts
it at login and restarts it within a minute. It runs whenever the user is
logged in, whether or not TouchDesigner is open. It is by construction a
persistence mechanism in a user-writable directory, and it is neither
signed nor notarized. Loopback only -- nothing here opens a port to the
network, and no firewall rule is created or needed.

THE ONE-PER-USER RULE. The data dir, the task and the agent are all
per-LOGGED-IN-USER, never per-machine. Every string in this module says
"user" for that reason; "one per machine" would be a lie on any shared
box, and a lie in exactly the direction that makes people careless.

TWO SEAMS TO KNOW ABOUT BEFORE READING FURTHER.

  1. PATH COMPUTATION vs FILESYSTEM I/O. app_dir/bin_dir/... join with
     the TARGET platform's separator (ntpath vs posixpath), exactly like
     convoy_client.data_dir, so a test can ask this Windows box what a
     macOS install looks like and get a real macOS answer. Discovery
     (find_interpreters) is the opposite: it reads a REAL disk, so it
     uses os.path and only ever runs on the machine it is describing.
     Mixing those up is what broke the bridge suite's first macOS run.

  2. THE RUNNER. Every OS-mutating call goes through an injected
     runner(argv) -> (returncode, stdout, stderr) taking a LIST. Never a
     string, never shell=True: the interpreter and launcher paths contain
     spaces ("C:/Program Files/..."), and a shell would also make every
     one of these a quoting bug away from executing an attacker-chosen
     command. Tests always inject; the default really does register a
     Scheduled Task.

macOS IS UNVERIFIED. Generated and unit-tested here, never run on a Mac:
the interpreter path inside the .app bundle, launchctl bootstrap in a GUI
login session, macOS 13+ Login Items gating, ProcessType/App Nap, and an
unsigned script under a signed interpreter. Where a specific fact is
guessed rather than known, the comment says UNVERIFIED. Do not quietly
promote any of them.
"""

import json
import ntpath
import os
import posixpath
import re
import subprocess
import sys
import time
from xml.sax.saxutils import escape as xml_escape

# Layout under the per-user data dir (convoy_client.data_dir()). State
# and payload live side by side because a second root would need a second
# discovery story, and the daemon already has to find this one.
#
#   host.json host.token host.portfile.json audit.jsonl jobs/
#                                  <- EXISTING state. Never installed,
#                                     never removed. See RETAINED below.
#   host.lock                      <- the singleton lock
#   app/<version>/convoy_*.py + .complete    <- versioned, atomic
#   bin/convoy_host_launch.py                <- STABLE path, never moves
#   installed.json                           <- version, interpreter, by whom
#   logs/host.log
#
# Versioned payload + a stable launcher path is what makes an Embody
# upgrade a FILE REWRITE: the task/agent points at bin/ forever and is
# never re-registered, so upgrading cannot lose supervision.
APP_SUBDIR = "app"
BIN_SUBDIR = "bin"
LOGS_SUBDIR = "logs"
INSTALLED_FILE = "installed.json"
COMPLETE_FILE = ".complete"
LAUNCHER_NAME = "convoy_host_launch.py"
LOG_NAME = "host.log"
TASK_XML_NAME = "convoy_host_task.xml"

# Supervisor identity. The Windows task name and the launchd label are
# stable forever: changing either strands the previously registered
# supervisor, which then keeps launching an uninstalled payload.
TASK_NAME = "EmbodyConvoyHost"
AGENT_LABEL = "tools.embody.convoy.host"
PLIST_NAME = AGENT_LABEL + ".plist"

# Host-app state that install/uninstall NEVER touch. 16.4/A-15 make
# indeterminate job records permanent by design, and A-41 forbids
# uninstall as an evidence-destruction path -- deleting these would
# destroy exactly the evidence the design promises to keep. Deleting them
# is a SEPARATE, second, separately-confirmed action.
RETAINED_NAMES = ("host.json", "host.token", "host.portfile.json",
                  "audit.jsonl", "host.db", "host.lock")
RETAINED_DIRS = ("jobs",)

# The host-app modules the vendoring step must supply. This is a
# MANIFEST OF WHAT TO VENDOR, not a gate: write_payload writes whatever
# dict it is handed, because the caller reads the DATs on the main thread
# and is the only thing that knows which exist. The byte-identity parity
# test between these DATs and dev/convoy/*.py, and the set-equality test
# against the daemon sources on disk, are what actually enforce the set.
HOST_MODULES = (
    "convoy_hostapp.py",
    "convoy_hoststore.py",
    "convoy_platform.py",
    "convoy_identity.py",
    "convoy_protocol.py",
    "convoy_capabilities.py",
    "convoy_controllers.py",
    "convoy_mcpclient.py",
    "convoy_hostprobe.py",
    # Phase 3 slice 1. The daemon imports it at startup for host
    # identity; omitting it shipped a payload whose host could not
    # sign an envelope. It degrades cleanly when `cryptography` is
    # absent (which it IS under TD's interpreter today), so it is
    # safe to vendor before the dependency story exists -- but the
    # daemon then reports identity_reason cryptography_missing
    # rather than pretending to have an identity.
    "convoy_hostkeys.py",
    # Phase 3 slice 2. The peer store, the fail-closed denylist, and
    # THE authorize_peer decision -- convoy_hostapp imports it at
    # module load, so a payload without it cannot start the daemon at
    # all. No network code yet (that is slice 3); safe to vendor now.
    "convoy_peers.py",
)

# Autonomous dispatch, ON for an installed host app: a supervised daemon
# that never drains its own queue would relay nothing unless something
# called /drain. Recorded in installed.json so it can change without
# re-registering the supervisor.
DEFAULT_DRAIN_INTERVAL_S = 2.0

# Log cap, AT LAUNCH ONLY. Not rotation (explicitly out of scope), and
# NOT a bound on a running daemon: the launcher checks the size once, in
# _open_log, before main() is entered. Under MultipleInstancesPolicy
# IgnoreNew a HEALTHY daemon is never relaunched, so the process that
# runs for months is precisely the one that never re-checks -- the cap
# bites a daemon that keeps DYING (many launches, bounded log), not one
# that keeps running. An in-run check belongs in the daemon's own
# writer, not here, and is not in this slice.
#
# This comment used to claim "an unattended daemon running for months
# cannot fill a user's disk", which is the inverse of what the mechanism
# delivers. Do not restore that claim without also implementing it.
LOG_MAX_BYTES = 4 * 1024 * 1024

# Supervisor kinds recorded in installed.json.
SUPERVISOR_TASK = "scheduled_task"      # win32
SUPERVISOR_AGENT = "launch_agent"       # darwin
SUPERVISOR_EXTERNAL = "external"        # A-36 opt-out: Owlette, or a studio's
SUPERVISOR_NONE = "none"

# host_state() outcomes. The STRINGS live in convoy_client.
# host_status_text() -- this module owns the DECISION, that one owns the
# vocabulary, so a status can never be computed in two places and drift.
STATE_NOT_INSTALLED = "not_installed"
STATE_RUNNING = "running"
STATE_NOT_RUNNING = "not_running"
STATE_STOPPED = "stopped"
STATE_NO_SUPERVISOR = "no_supervisor"
STATE_NEEDS_REPAIR_PYTHON = "needs_repair_python"
STATE_NEWER_INSTALL = "newer_install"
STATE_EXTERNAL_SUPERVISOR = "external_supervisor"

# plan_install() outcomes.
ACTION_INSTALL = "install"
ACTION_UPGRADE = "upgrade"
ACTION_CURRENT = "current"
ACTION_REFUSE_DOWNGRADE = "refuse_downgrade"
ACTION_EXTERNAL = "external"

# TouchDesigner install roots, per platform. DRIFT CANARY: the win32 list
# must match envoy_bridge._td_install_roots("win32"). It is deliberately
# a COPY and not an import -- A-44 forbids importing the bridge, and the
# bridge is answering a different question (which app can I LAUNCH, vs
# which interpreter can I RUN A SCRIPT UNDER). A test pins them together
# so the copy cannot silently drift.
TD_INSTALL_ROOTS = {
    "win32": [r"C:\Program Files\Derivative"],
    "darwin": ["/Applications", "~/Applications"],
    "linux": ["/opt/derivative"],
}

_VERSION_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")


# -- paths -------------------------------------------------------------

def _join(platform=None):
    """The TARGET platform's joiner. See seam 1 in the module docstring:
    os.path.join on Windows would answer the darwin branch with
    `/Users/x/Library\\Application Support`, which is not a macOS path
    and so does not exercise the macOS branch at all."""
    platform = platform or sys.platform
    return ntpath.join if platform == "win32" else posixpath.join


def safe_version(version):
    """A version string that is safe to use as ONE path segment.

    Refuses empty, '.', '..', anything with a separator, and anything
    outside [A-Za-z0-9._+-]. The version reaches here from a TD parameter
    and is about to become a DIRECTORY NAME under the user's data dir; a
    value like '../../bin' would place the payload -- and later
    remove_payload's unlinks -- somewhere else entirely.
    """
    text = str(version or "").strip()
    if text in ("", ".", "..") or not _VERSION_OK.match(text):
        raise ValueError("unusable version for a path segment: %r"
                         % (version,))
    return text


def install_root(data_dir):
    """The directory the installer owns: the per-user data dir itself.

    Trivial by design and named anyway, so there is exactly one place
    that says where an install lives.
    """
    return data_dir


def app_dir(data_dir, version=None, platform=None):
    """<root>/app, or <root>/app/<version> when a version is given."""
    join = _join(platform)
    base = join(install_root(data_dir), APP_SUBDIR)
    if version is None:
        return base
    return join(base, safe_version(version))


def bin_dir(data_dir, platform=None):
    return _join(platform)(install_root(data_dir), BIN_SUBDIR)


def logs_dir(data_dir, platform=None):
    return _join(platform)(install_root(data_dir), LOGS_SUBDIR)


def installed_path(data_dir, platform=None):
    return _join(platform)(install_root(data_dir), INSTALLED_FILE)


def launcher_path(data_dir, platform=None):
    """The STABLE entry point. Never versioned: the supervisor points
    here forever, so an upgrade rewrites files and never re-registers."""
    return _join(platform)(bin_dir(data_dir, platform), LAUNCHER_NAME)


def log_path(data_dir, platform=None):
    return _join(platform)(logs_dir(data_dir, platform), LOG_NAME)


def complete_path(data_dir, version, platform=None):
    return _join(platform)(app_dir(data_dir, version, platform),
                           COMPLETE_FILE)


def task_xml_path(data_dir, platform=None):
    """Where the submitted Scheduled Task XML is kept. Retained after
    registration on purpose: it is the only readable record of what was
    submitted, and Windows does not store it back verbatim (see
    render_task_xml)."""
    return _join(platform)(bin_dir(data_dir, platform), TASK_XML_NAME)


def plist_path(home, platform="darwin"):
    """~/Library/LaunchAgents/<label>.plist. Takes HOME rather than the
    data dir: launchd only loads agents from that one directory."""
    return _join(platform)(home, "Library", "LaunchAgents", PLIST_NAME)


# -- installed.json ----------------------------------------------------

def read_installed(data_dir, platform=None, reader=None):
    """The install record, or None when absent/corrupt/not-an-object.

    Corrupt is treated as absent, exactly like read_portfile: a
    half-written record must make the caller offer a fresh install, never
    raise into a worker thread.
    """
    path = installed_path(data_dir, platform)
    try:
        if reader is not None:
            raw = reader(path)
        else:
            with open(path, "r", encoding="utf-8") as f:
                raw = f.read()
        record = json.loads(raw)
    except (OSError, ValueError, TypeError):
        return None
    return record if isinstance(record, dict) else None


def write_installed(data_dir, record, platform=None, writer=None):
    """Persist the install record ATOMICALLY, and LAST of all.

    Last is the contract: installed.json is what read_installed reports
    and what the launcher reads to find its payload, so it must not name
    a version whose files are still being written. Concurrent installers
    (two projects pulsing Install at once) are handled the same way the
    payload is -- temp + os.replace, so a reader sees the old record or
    the new one, never a partial one.
    """
    path = installed_path(data_dir, platform)
    payload = json.dumps(record, indent=1, sort_keys=True) + "\n"
    if writer is not None:
        writer(path, payload)
        return path
    _atomic_write(path, payload)
    return path


def _atomic_write(path, text):
    """temp + os.replace in the destination directory (a cross-device
    rename is not atomic, and /tmp is frequently another device)."""
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = "%s.%s.tmp" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    try:
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# -- what an install should do -----------------------------------------

def _version_key(text):
    """Sortable key for a dotted-numeric version, or None. Never raises.

    ONLY fully numeric versions are orderable. None means UNKNOWN -- not
    "oldest" and emphatically not "newest" -- and every caller treats it
    as "cannot compare".

    The previous version ranked a non-numeric chunk ABOVE every numeric
    one, so ANY unparseable version in installed.json ('garbage', 'dev',
    'HEAD', 'v6.0.171', a dict, True) read as NEWER than ours and wedged
    Install into refuse_downgrade permanently, with no route out of the
    UI -- the exact opposite of plan_install's promise that a corrupt
    record is what Install-as-repair is for. Comparing '6.0.171-rc1'
    against '6.0.171' has no correct answer anyway; refusing to guess is
    the honest form, and the caller upgrades rather than deadlocking.
    """
    text = str(text or "").strip()
    if not text:
        return None
    chunks = re.split(r"[._+-]", text)
    key = []
    for chunk in chunks:
        if not chunk.isdigit():
            return None         # not orderable -> unknown, never ranked
        key.append(int(chunk))
    return tuple(key) or None


def plan_install(installed, version, platform=None):
    """What pulsing Install should DO, given what is already installed.

    Returns {"action", "version", "installed_version", "supervisor",
    "detail"}. Never raises: an unusable OUR-version is reported as a
    refusal, not thrown into a worker.

    ORDERING, and why:
      1. ours unusable AS A PATH -> refuse. Nothing can be written.
      2. no record               -> install
      3. both orderable and ours OLDER -> refuse_downgrade. A newer host
                                 app must never be replaced by an older
                                 project (A-36: many projects, one
                                 host). This outranks even the external
                                 opt-out -- "someone else supervises it"
                                 is not permission to downgrade it.
      4. supervisor external     -> external. Write the payload, register
                                 NOTHING, never two supervisors.
      5. either side UNORDERABLE -> upgrade. Cannot compare, so repair.
      6. same version            -> current (a stated no-op, so the
                                 second project's Install button is
                                 honest)
      7. otherwise               -> upgrade

    UNUSABLE-AS-A-PATH AND UNORDERABLE ARE DIFFERENT FAILURES and are
    kept apart deliberately. '../evil' cannot become a directory name
    and must refuse; '6.0.171-rc1' is a perfectly good directory name
    that simply cannot be ranked against '6.0.171', and blocking an rc
    build from installing itself would be absurd. Collapsing the two is
    how an unreadable installed version came to wedge Install into
    refuse_downgrade forever.
    """
    record = installed if isinstance(installed, dict) else None
    theirs_text = (record or {}).get("version")
    theirs = _version_key(theirs_text)
    ours = _version_key(version)
    supervisor = (record or {}).get("supervisor") or SUPERVISOR_NONE

    def result(action, detail):
        return {"action": action, "version": str(version or ""),
                "installed_version": theirs_text, "supervisor": supervisor,
                "detail": detail}

    try:
        safe_version(version)
    except ValueError:
        return result(ACTION_REFUSE_DOWNGRADE,
                      "this Embody has no usable version to install")
    if record is None:
        return result(ACTION_INSTALL,
                      "no Convoy host app is installed for this user")
    if theirs is not None and ours is not None and ours < theirs:
        return result(
            ACTION_REFUSE_DOWNGRADE,
            "version %s is already installed by a newer Embody; %s will "
            "not downgrade it" % (theirs_text, version))
    if supervisor == SUPERVISOR_EXTERNAL:
        return result(
            ACTION_EXTERNAL,
            "the host app is managed by another supervisor; the payload "
            "will be updated and no task or agent registered")
    if theirs is None or ours is None:
        # Cannot compare -- so REPAIR rather than deadlock. This is the
        # only route out of a corrupt installed.json from the UI.
        return result(ACTION_UPGRADE,
                      "the installed version (%r) cannot be compared with "
                      "%s; reinstalling" % (theirs_text, version))
    if ours == theirs:
        return result(ACTION_CURRENT,
                      "version %s is already installed" % (theirs_text,))
    return result(ACTION_UPGRADE,
                  "upgrading %s to %s" % (theirs_text, version))


# -- the payload -------------------------------------------------------

# A manifest entry we are willing to unlink: letters, digits, and the
# few punctuation marks a Python module name uses. AN ACCEPT-LIST, NOT A
# DENY-LIST, and that distinction is load-bearing -- the deny-list this
# replaced rejected '/', '\\', '..' and os.path.isabs, and still admitted
# 'D:evil.py'. That entry has no separator, no dot-dot, and is not
# absolute, but ntpath.join(payload_dir, 'D:evil.py') DISCARDS the
# payload dir and resolves against drive D:'s own per-drive current
# directory -- so remove_payload unlinked outside the payload tree
# (proven with an intercepted os.unlink, 2026-08-01). Enumerating what
# is dangerous cannot work on Windows paths; enumerate what is allowed.
_BARE_NAME_OK = re.compile(r"^[A-Za-z0-9._+-]+$")


def _bare_name(name):
    """A manifest entry that is a plain filename and nothing else.

    Entries come back out of a .complete file on disk and are then handed
    to os.unlink, so the check lives at the USE site, not only at the
    write site -- the file could have been edited between the two.

    Rejects, by construction rather than by enumeration: any separator,
    any drive letter or colon ('D:evil.py', 'C:foo', 'D:'), any UNC
    prefix, '.', '..', and the empty string.
    """
    text = str(name or "")
    if text in (".", "..") or not _BARE_NAME_OK.match(text):
        return None
    return text


def _inside(directory, path, platform=None):
    """Is `path` really a child of `directory` on the TARGET platform?

    The second lock on the same door, because a regex is a claim about
    NAMES and this is a claim about the PATH THAT WILL BE UNLINKED. A
    drive-relative entry makes join() return something that does not
    start with the payload dir at all, so this catches the whole class
    even if the character class is ever loosened.
    """
    platform = platform or sys.platform
    sep = "\\" if platform == "win32" else "/"
    prefix = directory if directory.endswith(sep) else directory + sep
    return path.startswith(prefix)


def write_payload(data_dir, version, modules, platform=None, now=None):
    """Write the host-app sources for one version, atomically.

    `modules` is {filename: source text}, already read off the DATs by
    the main thread. Each file lands via temp + os.replace, and the
    .complete manifest is written LAST -- that ordering IS the interlock:
    the launcher refuses any app/<version>/ without it, so a crashed or
    half-finished install can never be executed. Concurrent installers
    writing the same version therefore converge instead of racing.

    Returns the manifest dict. Raises on a genuine I/O failure -- the
    caller (install) owns the never-raises contract.
    """
    version = safe_version(version)
    if not isinstance(modules, dict) or not modules:
        raise ValueError("write_payload needs {filename: source}")
    for name in modules:
        if _bare_name(name) is None:
            raise ValueError("payload entries must be bare filenames: %r"
                             % (name,))
    target = app_dir(data_dir, version, platform)
    os.makedirs(target, exist_ok=True)
    join = _join(platform)

    written = []
    for name in sorted(modules):
        _atomic_write(join(target, name), modules[name])
        written.append(name)

    manifest = {
        "version": version,
        "files": written,
        "written_at": (now or time.time)(),
        "format": "convoy-install/1",
    }
    # LAST. Nothing above this line makes the payload runnable.
    _atomic_write(join(target, COMPLETE_FILE),
                  json.dumps(manifest, indent=1, sort_keys=True) + "\n")
    return manifest


def read_manifest(data_dir, version, platform=None):
    """The .complete manifest, or None if absent/corrupt/not-an-object.

    None means NOT RUNNABLE -- the launcher refuses on exactly this.
    """
    try:
        with open(complete_path(data_dir, version, platform),
                  "r", encoding="utf-8") as f:
            manifest = json.load(f)
    except (OSError, ValueError):
        return None
    return manifest if isinstance(manifest, dict) else None


def remove_payload(data_dir, version, platform=None):
    """Unlink EXACTLY the files this version's manifest names, then the
    manifest, then rmdir the directory.

    NEVER shutil.rmtree (the repo's file-safety rule, and the amplifier
    behind the 2026-07-01 incident that deleted 18 specimen files): rmtree
    would happily take anything a user had put in that directory, and a
    bad `version` would aim it somewhere else entirely. rmdir REFUSES a
    non-empty directory, so an unexpected file survives and is reported
    instead of destroyed.

    __pycache__ IS OURS AND IS REMOVED. It is a derived artifact of our
    own modules, created by CPython inside our own versioned directory
    the first time the daemon runs -- so leaving it behind meant every
    machine that had actually RUN the host app failed the rmdir and kept
    the payload dir forever, while uninstall reported a clean removal
    (measured 2026-08-01: 8 .pyc files survived, ok=True, kept=[]). Only
    *.pyc/*.pyo directly inside app/<version>/__pycache__ are unlinked,
    and the preview names it, so this is a stated deletion rather than a
    silent one.

    Returns {"removed", "missing", "kept", "removed_dir", "remaining"}.
    `remaining` lists what is STILL in the directory when it could not be
    removed -- the report the old version dropped on the floor. Never
    raises.
    """
    result = {"removed": [], "missing": [], "kept": [], "removed_dir": False,
              "remaining": []}
    try:
        version = safe_version(version)
    except ValueError as e:
        result["kept"].append(str(e))
        return result
    target = app_dir(data_dir, version, platform)
    manifest = read_manifest(data_dir, version, platform)
    join = _join(platform)

    def unlink(path, label):
        # BOTH gates: the name passed the accept-list, and the joined
        # path really is inside the payload dir.
        if not _inside(target, path, platform):
            result["kept"].append(label)
            return
        try:
            os.unlink(path)
            result["removed"].append(label)
        except FileNotFoundError:
            result["missing"].append(label)
        except OSError:
            result["kept"].append(label)

    names = list((manifest or {}).get("files") or [])
    for name in names:
        bare = _bare_name(name)
        if bare is None:
            # A manifest entry that is not a plain filename is refused,
            # not sanitised: it means the file was tampered with, and the
            # honest response is to leave everything alone and say so.
            result["kept"].append(str(name))
            continue
        unlink(join(target, bare), bare)

    # Our own bytecode cache, after the sources it was compiled from.
    cache = join(target, "__pycache__")
    try:
        cached = sorted(os.listdir(cache))
    except OSError:
        cached = []
    for name in cached:
        if name.endswith((".pyc", ".pyo")) and _bare_name(name):
            path = join(cache, name)
            if _inside(cache, path, platform):
                try:
                    os.unlink(path)
                    result["removed"].append("__pycache__/" + name)
                except OSError:
                    result["kept"].append("__pycache__/" + name)
    if cached:
        try:
            os.rmdir(cache)
        except OSError:
            pass        # something else lives there; rmdir refuses, we report

    # The manifest itself, only after everything it names is gone.
    unlink(join(target, COMPLETE_FILE), COMPLETE_FILE)

    try:
        os.rmdir(target)
        result["removed_dir"] = True
    except OSError:
        # Non-empty (something we did not install, or an interrupted
        # payload with no manifest to name its files) or absent. The
        # directory stays -- and unlike before, we SAY WHAT IS IN IT.
        try:
            result["remaining"] = [join(target, n)
                                   for n in sorted(os.listdir(target))]
        except OSError:
            pass        # genuinely absent: nothing to report
    return result


def _app_subdirs(data_dir, platform=None):
    """(usable version dirs, stray dirs) under app/, both sorted.

    Reads the DISK, not installed.json: an interrupted upgrade leaves a
    payload the record never mentioned, and uninstall must still find it.
    That is exactly why it also meets names nobody planned -- 'tmp junk',
    '.hidden', anything a user dropped in there. Those are SPLIT OUT, not
    passed on: handing one to app_dir raised ValueError straight through
    the uninstall PREVIEW, so the dialog that exists to name what is kept
    never returned at all, and uninstall() reported failure after having
    already removed everything (measured 2026-08-01).
    """
    base = app_dir(data_dir, None, platform)
    join = _join(platform)
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return [], []
    usable, stray = [], []
    for name in names:
        if not os.path.isdir(join(base, name)):
            continue
        try:
            safe_version(name)
        except ValueError:
            stray.append(join(base, name))
            continue
        usable.append(name)
    return sorted(usable, key=lambda n: (_version_key(n) or (), n)), stray


def installed_versions(data_dir, platform=None):
    """Every app/<version> directory whose name is a usable version,
    oldest-first. Names that are not are reported by
    plan_host_uninstall's `stray`, never silently removed."""
    return _app_subdirs(data_dir, platform)[0]


# -- the launcher ------------------------------------------------------

def render_launcher(platform, data_dir):
    """The stage-1 launcher, as Python source.

    Embeds exactly ONE fact -- the data dir -- and resolves everything
    else at run time from installed.json. That is what makes an upgrade a
    file rewrite: the supervisor points at this path forever, and a new
    version needs no re-registration.

    FOUR THINGS IT MUST DO, in this order:

    1. OPEN THE LOG AND REBIND sys.stdout/sys.stderr/sys.stdin BEFORE
       importing or calling convoy_hostapp. Under pythonw.exe
       `sys.stderr is None`, and convoy_hostapp.main() calls
       sys.stderr.write(...) unconditionally at startup -- unhandled that
       is an AttributeError on EVERY launch, i.e. a silent 60-second
       death loop that looks exactly like healthy supervision (the task
       runs, exits, runs again). The rebind is not logging convenience;
       it is the thing that makes the daemon able to start at all.
    2. REFUSE an app/<version>/ without .complete. Half-written payloads
       must never execute.
    3. RUN THE DAEMON IN-PROCESS (import convoy_hostapp; main([...])),
       never spawn-and-exit. The whole supervision mechanism rests on
       this: MultipleInstancesPolicy IgnoreNew suppresses the per-minute
       relaunch only while the task INSTANCE is alive, and the instance
       lives exactly as long as this process. A launcher that spawned a
       child and returned would report "finished" immediately, and the
       next repetition would start a SECOND daemon.
    4. CAP THE LOG, AT LAUNCH. Truncate-and-restart above LOG_MAX_BYTES
       when the log is opened. Stated precisely because it is easy to
       over-read: this bounds a daemon that keeps dying and relaunching,
       NOT one long-running process, which never re-enters _open_log.
    5. REFUSE A VERSION THAT IS NOT A PLAIN VERSION -- the same accept-
       list safe_version applies installer-side, re-applied here because
       this is where a version read back off disk becomes a path.

    EXIT CODES, deliberately asymmetric:
      0 -- ran, or declined because another instance holds the singleton
           (the expected once-a-minute outcome on a healthy machine;
           nonzero there would paint LastTaskResult as a fault forever).
      1 -- nothing to run: no install record, or a payload with no
           .complete. That IS a fault and should read as one.
    """
    platform = platform or sys.platform
    # repr() gives a correctly escaped Python literal for either
    # platform's separators -- a Windows path in a plain "..." would turn
    # \b, \t and \n in the user name into control characters.
    return _LAUNCHER_TEMPLATE % {
        "data_dir": repr(str(data_dir)),
        "installed_file": repr(INSTALLED_FILE),
        "app_subdir": repr(APP_SUBDIR),
        "logs_subdir": repr(LOGS_SUBDIR),
        "log_name": repr(LOG_NAME),
        "complete_file": repr(COMPLETE_FILE),
        "log_max_bytes": LOG_MAX_BYTES,
        "drain_interval": DEFAULT_DRAIN_INTERVAL_S,
    }


# Written as a template rather than assembled, so the golden test reads
# like the file that lands on disk. %(...)s substitutions only; every
# literal % in the body is doubled.
_LAUNCHER_TEMPLATE = '''\
"""Convoy host app launcher -- generated by Embody, do not edit.

Rewritten by every install. Its PATH is stable forever, so the Scheduled
Task / LaunchAgent that points here is never re-registered on upgrade.

It runs the daemon IN-PROCESS on purpose: the supervisor's
MultipleInstancesPolicy=IgnoreNew suppresses the per-minute relaunch only
while this task instance is alive, and it is alive exactly as long as
this process. Spawning a child and exiting would break supervision --
every repetition would start another daemon.
"""

import os
import re
import sys
import time

DATA_DIR = %(data_dir)s
LOG_MAX_BYTES = %(log_max_bytes)s

# The SAME accept-list convoy_install.safe_version applies, re-applied
# here because this file is the one consumer that reads the version back
# off disk and turns it into a path. Without it, installed.json holding
# {"version": "../../elsewhere"} made this launcher escape app/, find a
# .complete out there and import convoy_hostapp from it -- executing
# that file at EVERY LOGIN (demonstrated 2026-08-01). Inside the stated
# trust model that is not an escalation, but it turns one file write
# into a login-persistence primitive in the one component that runs
# whether or not TouchDesigner is open.
_VERSION_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")


def _open_log():
    """Open logs/host.log and REBIND the std streams onto it.

    THIS MUST HAPPEN BEFORE convoy_hostapp IS IMPORTED OR CALLED. Under
    pythonw.exe sys.stdout/sys.stderr are None, and the daemon writes its
    startup banner to sys.stderr unconditionally -- without this the
    process would die with an AttributeError on every launch and the task
    would look perfectly healthy while nothing ever ran.
    """
    directory = os.path.join(DATA_DIR, %(logs_subdir)s)
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError:
        pass
    path = os.path.join(directory, %(log_name)s)
    mode = "a"
    try:
        if os.path.getsize(path) > LOG_MAX_BYTES:
            # Truncate and restart. Not rotation (out of scope) -- just a
            # cap, so an unattended daemon cannot fill the disk.
            mode = "w"
    except OSError:
        pass
    try:
        log = open(path, mode, buffering=1, encoding="utf-8",
                   errors="replace", newline="\\n")
    except OSError:
        # A LOG WE CANNOT OPEN MUST NOT BECOME A DEAD DAEMON. Under
        # pythonw the streams are None, and the daemon writes to
        # sys.stderr unconditionally at startup -- leaving them None
        # here would reintroduce the exact per-minute death loop this
        # function exists to prevent. Losing the log is bad; losing the
        # host app because of the log is worse.
        try:
            log = open(os.devnull, "w")
        except OSError:
            return None
    sys.stdout = log
    sys.stderr = log
    try:
        sys.stdin = open(os.devnull, "r")
    except OSError:
        pass
    if mode == "w":
        log.write("--- log restarted (over %%d bytes) ---\\n"
                  %% (LOG_MAX_BYTES,))
    return log


def _say(message):
    stream = sys.stderr
    if stream is None:
        return
    try:
        stream.write("[%%s] %%s\\n"
                     %% (time.strftime("%%Y-%%m-%%d %%H:%%M:%%S"), message))
        stream.flush()
    except (OSError, ValueError):
        pass


def _read_installed():
    path = os.path.join(DATA_DIR, %(installed_file)s)
    try:
        import json
        with open(path, "r", encoding="utf-8") as f:
            record = json.load(f)
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def main():
    _open_log()
    record = _read_installed()
    if not record or not record.get("version"):
        _say("no Convoy host app is installed for this user "
             "(no readable installed.json) -- nothing to run")
        return 1
    version = str(record["version"])
    if not _VERSION_OK.match(version):
        # Refuse rather than join it into a path: see _VERSION_OK.
        _say("refusing an unusable version %%r from installed.json -- it "
             "must be a plain version like 6.0.171" %% (version,))
        return 1
    payload = os.path.join(DATA_DIR, %(app_subdir)s, version)
    if not os.path.isfile(os.path.join(payload, %(complete_file)s)):
        # Half-written or interrupted install. Refusing beats importing
        # a partial daemon.
        _say("payload for version %%s is incomplete (no .complete "
             "manifest) -- refusing to run it" %% (version,))
        return 1

    interval = record.get("drain_interval", %(drain_interval)s)
    try:
        interval = float(interval)
    except (TypeError, ValueError):
        interval = %(drain_interval)s

    if payload not in sys.path:
        sys.path.insert(0, payload)
    import convoy_hostapp
    _say("starting Convoy host app %%s (drain interval %%ss)"
         %% (version, interval))
    # IN-PROCESS, never spawn-and-exit: see the module docstring.
    return convoy_hostapp.main(["--data-dir", DATA_DIR,
                                "--drain-interval", str(interval)]) or 0


if __name__ == "__main__":
    sys.exit(main())
'''


# -- the Windows supervisor --------------------------------------------

def current_user_account(platform=None, env=None):
    """This login's Windows account, as `DOMAIN\\user`, or None.

    It is what <UserId> must carry -- see render_task_xml for why a task
    without one cannot be registered unelevated. `env` is injected (D-5)
    so both the domain-joined and the workgroup shapes are testable
    anywhere; the real values come from the process environment, which
    is the logged-in user by construction.

    Returns None rather than guessing when USERNAME is unset. A wrong
    account here produces a task that registers and then runs for
    somebody else -- far worse than a refusal the installer can report.
    """
    platform = platform or sys.platform
    env = env if env is not None else os.environ
    user = (env.get("USERNAME") or "").strip()
    domain = (env.get("USERDOMAIN") or "").strip()
    if not user:
        return None
    # A workgroup machine may have no USERDOMAIN; the bare name is a
    # valid UserId there.
    return "%s\\%s" % (domain, user) if domain else user


def render_task_xml(interpreter, launcher, user, author="Embody",
                    description=None):
    """The Scheduled Task definition, UTF-16LE WITH BOM.

    Encoding is not cosmetic: schtasks /Create /XML rejects the file
    outright if it is UTF-8, so the test asserts the bytes.

    `user` is REQUIRED and is not defaulted here. Resolving it silently
    is exactly how the UserId went missing the first time: the document
    stayed well-formed, every unit test passed, and the failure only
    appeared against real schtasks. A caller that cannot name the
    account must fail loudly, not register a task for "anyone".

    THE RECIPE IS SETTLED (2026-07-31 spike + registration probe) -- do
    not re-derive it:
      UserId, IN THE LOGON TRIGGER *AND* IN THE PRINCIPAL  <- WITHOUT
        THIS THE INSTALLER CANNOT REGISTER AT ALL. A LogonTrigger with
        no UserId means "when ANY user logs on", which is an
        administrator-only registration, so schtasks answers
        `ERROR: Access is denied.` from the non-elevated TouchDesigner
        this installer runs in. MEASURED 2026-08-01, real schtasks, one
        process, one moment, three XMLs differing only here:
          (A) no UserId anywhere                -> Access is denied
          (B) UserId in the LogonTrigger only   -> SUCCESS
          (C) UserId in trigger AND principal   -> SUCCESS
        (C) is what we emit: the trigger says WHOSE logon starts it and
        the principal says WHO it runs as, and leaving the two to be
        inferred separately is how they drift apart. NO UNIT TEST CAN
        CATCH THIS -- the UserId-less document is perfectly well-formed
        and round-trips through ElementTree. Only a real schtasks call
        surfaces it, which is why the guard below is a rendered-text
        assertion and why this paragraph exists.
      LogonTrigger + Delay PT30S + Repetition Interval PT1M  <- THE
        supervisor. Task Scheduler does not watch the child; the
        repetition is what notices it died.
      MultipleInstancesPolicy IgnoreNew  <- suppresses the per-minute
        relaunch while the daemon lives. Load-bearing, and only works
        because the launcher runs the daemon in-process.
      ExecutionTimeLimit PT0S  <- the DEFAULT IS P3D, which would kill a
        healthy daemon after three days. A one-hour acceptance run cannot
        see this.
      DisallowStartIfOnBatteries / StopIfGoingOnBatteries false, plus
        StopOnIdleEnd false  <- the other quiet killers, all defaulting
        the wrong way for a laptop.
      NO RestartOnFailure/RestartCount/RestartInterval  <- the spike
        proved they respond to a failure to LAUNCH, not to a child that
        died. A negative golden assertion pins their absence.

    SUBMITTED IS NOT STORED. VERIFIED, DO NOT "FIX" THE TESTS TO MATCH
    THE STORED FORM. This function's golden tests assert what we SUBMIT,
    because Windows rewrites parts of the document on registration.
    Measured by registering the real output and reading it back with
    `schtasks /Query /XML` (2026-07-31, re-confirmed 2026-08-01):

      SURVIVES VERBATIM -- Repetition Interval PT1M,
        MultipleInstancesPolicy IgnoreNew, ExecutionTimeLimit PT0S,
        DisallowStartIfOnBatteries false, StopOnIdleEnd false,
        Hidden true, and the ABSENCE of any RestartOnFailure element.
      DROPPED -- RunLevel. Stored ABSENT for an unelevated registration,
        however we submit it.
      NORMALIZED, AND THE TWO UserId VALUES DIFFERENTLY FROM EACH OTHER:
        the LogonTrigger's UserId is stored as a SID
        (S-1-5-21-...-1000), while the Principal's is stored as the
        account name (TEC-A4D\\admin). So "the stored trigger UserId
        equals the account we submitted" is FALSE, and "trigger and
        principal name the same string" is true ONLY of the submitted
        document. Both are still the same account -- one is just spelled
        as a security identifier.

    A round-trip assertion against stored output would therefore fail on
    three counts while the task works perfectly. Assert the rendering.
    """
    if not str(user or "").strip():
        raise ValueError(
            "render_task_xml needs the account to register for: a "
            "LogonTrigger without a UserId means 'any user logs on', "
            "which only an administrator may register")
    description = description or (
        "Runs the Embody Convoy host app for this user and restarts it "
        "within a minute if it stops. Loopback only; never elevated.")
    command = xml_escape(str(interpreter))
    # The launcher path is quoted INSIDE the Arguments element: TD's
    # interpreter and the data dir both live under paths with spaces.
    arguments = xml_escape('"%s"' % (launcher,))
    text = _TASK_XML_TEMPLATE % {
        "author": xml_escape(str(author)),
        "description": description and xml_escape(description),
        "task_name": xml_escape(TASK_NAME),
        "command": command,
        "arguments": arguments,
        # ONE escaped value, substituted into BOTH places, so the
        # trigger and the principal cannot disagree about the account.
        "user": xml_escape(str(user).strip()),
    }
    # Explicit LE + explicit BOM. The "utf-16" codec picks an endianness
    # from the HOST, so it would emit UTF-16BE on a big-endian machine
    # and schtasks would refuse the file.
    return b"\xff\xfe" + text.encode("utf-16-le")


_TASK_XML_TEMPLATE = """\
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Author>%(author)s</Author>
    <Description>%(description)s</Description>
    <URI>\\%(task_name)s</URI>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <Repetition>
        <Interval>PT1M</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
      <UserId>%(user)s</UserId>
      <Delay>PT30S</Delay>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>%(user)s</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>true</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>%(command)s</Command>
      <Arguments>%(arguments)s</Arguments>
    </Exec>
  </Actions>
</Task>
"""


# -- the macOS supervisor (UNVERIFIED on hardware) ---------------------

def render_launch_agent_plist(interpreter, launcher, data_dir,
                              label=AGENT_LABEL, platform="darwin"):
    """The LaunchAgent definition, as plist XML text.

    NO ACCOUNT KEY, AND THAT IS DELIBERATE -- stated rather than left to
    omission, because the Windows twin needs the opposite (see
    render_task_xml's UserId, whose absence made the task unregisterable
    for a non-elevated user). A LaunchAgent is per-user BY CONSTRUCTION,
    three times over: the file lives in ~/Library/LaunchAgents, it is
    bootstrapped into the `gui/<uid>` domain, and launchd runs an agent
    as the owner of that domain. There is nothing to name. The keys that
    WOULD change the account -- UserName / GroupName -- are the
    LaunchDAEMON form, need root, and would be a different and much
    larger grant than the one the install dialog asks for; a test asserts
    they never appear.

    launchd GENUINELY SUPERVISES the child, which Task Scheduler does
    not: KeepAlive restarts the daemon in about a second, against
    Windows' up-to-60. THAT ASYMMETRY IS REAL AND THE DOCS MUST STATE IT
    rather than implying both platforms behave alike.

    ThrottleInterval 10 keeps a crash-looping daemon from spinning.
    ProcessType Interactive because Background is App-Nap throttled --
    UNVERIFIED, like every other macOS claim here.

    StandardOutPath is deliberately ABSENT and StandardErrorPath points
    at a SEPARATE file, not host.log: the launcher rebinds Python's
    sys.stdout/sys.stderr onto host.log itself, and letting launchd
    append to the same path from an independent file offset would
    interleave two writers into one file. What remains on launchd's
    stderr is only what dies BEFORE the rebind -- rare, tiny, and exactly
    the thing that is otherwise invisible.
    """
    join = _join(platform)
    stderr_path = join(logs_dir(data_dir, platform), "launchd-stderr.log")
    return _PLIST_TEMPLATE % {
        "label": xml_escape(str(label)),
        "interpreter": xml_escape(str(interpreter)),
        "launcher": xml_escape(str(launcher)),
        "stderr_path": xml_escape(stderr_path),
    }


_PLIST_TEMPLATE = """\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" \
"http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
\t<key>Label</key>
\t<string>%(label)s</string>
\t<key>ProgramArguments</key>
\t<array>
\t\t<string>%(interpreter)s</string>
\t\t<string>%(launcher)s</string>
\t</array>
\t<key>RunAtLoad</key>
\t<true/>
\t<key>KeepAlive</key>
\t<true/>
\t<key>ThrottleInterval</key>
\t<integer>10</integer>
\t<key>ProcessType</key>
\t<string>Interactive</string>
\t<key>StandardErrorPath</key>
\t<string>%(stderr_path)s</string>
</dict>
</plist>
"""


# -- talking to the supervisor -----------------------------------------

def _domain(uid, label=AGENT_LABEL):
    return "gui/%s/%s" % (uid, label)


def supervisor_argv(action, platform=None, xml_path=None, plist_path=None,
                    uid=None, label=AGENT_LABEL, task_name=TASK_NAME):
    """The argv LIST for one supervisor action. Never a string.

    A list is not a style preference: the interpreter, the launcher and
    the data dir all sit under paths with spaces, and a shell string
    would be one quoting bug away from running an attacker-chosen
    command. Nothing in this module ever passes shell=True.

    Actions, and what each means on each platform:

      register   win32  schtasks /Create /XML (idempotent via /F)
                 darwin launchctl bootstrap gui/<uid> <plist>
      unregister win32  schtasks /Delete
                 darwin launchctl bootout  (the PLIST FILE is unlinked
                        separately, in Python -- bootout only removes the
                        job from the domain, so a plist left on disk
                        would come back at the next login)
      enable/disable    schtasks /Change /ENABLE|/DISABLE
                 darwin launchctl enable|disable
      start      win32  schtasks /Run
                 darwin launchctl kickstart
      stop       win32  schtasks /End
                 darwin launchctl bootout   (KeepAlive means SIGTERM
                        alone would be undone in about a second)
      status     win32  schtasks /Query /FO LIST /V
                 darwin launchctl print

    STOP IS TWO STEPS, not one -- see stop(), which disables first. On
    Windows the task would re-run within a minute and the button would
    look broken; on macOS KeepAlive would restart it even faster.

    ACCOUNT CONTEXT, audited after the 2026-08-01 UserId defect. Nothing
    here names a user, and nothing here needs to:
      - schtasks addresses the task by name and operates on the CURRENT
        user's task store. It is never given /RU or /RP -- Embody does
        not ask for, store, or pass a credential, and a task that ran as
        another account would be a different grant entirely. WHO the
        task belongs to is settled once, in the registered XML's UserId.
      - launchctl is scoped to `gui/<uid>`, which IS the account. The
        `system/` domain (root, all users) is never targeted.
    A test pins both.
    """
    platform = platform or sys.platform
    if platform == "win32":
        if action == "register":
            if not xml_path:
                raise ValueError("register needs the task XML path")
            return ["schtasks", "/Create", "/TN", task_name,
                    "/XML", str(xml_path), "/F"]
        if action == "unregister":
            return ["schtasks", "/Delete", "/TN", task_name, "/F"]
        if action == "enable":
            return ["schtasks", "/Change", "/TN", task_name, "/ENABLE"]
        if action == "disable":
            return ["schtasks", "/Change", "/TN", task_name, "/DISABLE"]
        if action == "start":
            return ["schtasks", "/Run", "/TN", task_name]
        if action == "stop":
            return ["schtasks", "/End", "/TN", task_name]
        if action == "status":
            # /FO LIST /V, not /XML: the list view carries Status, Last
            # Result and Scheduled Task State, which is what a user-facing
            # status needs. Its Repetition reporting is a known liar --
            # see parse_supervisor_status.
            return ["schtasks", "/Query", "/TN", task_name,
                    "/FO", "LIST", "/V"]
        raise ValueError("unknown supervisor action: %r" % (action,))

    # darwin (and any other POSIX, though only darwin is supported)
    if uid is None:
        uid = os.getuid() if hasattr(os, "getuid") else 0
    target = _domain(uid, label)
    if action == "register":
        if not plist_path:
            raise ValueError("register needs the plist path")
        return ["launchctl", "bootstrap", "gui/%s" % (uid,), str(plist_path)]
    if action == "unregister":
        return ["launchctl", "bootout", target]
    if action == "enable":
        return ["launchctl", "enable", target]
    if action == "disable":
        return ["launchctl", "disable", target]
    if action == "start":
        return ["launchctl", "kickstart", target]
    if action == "stop":
        return ["launchctl", "bootout", target]
    if action == "status":
        return ["launchctl", "print", target]
    raise ValueError("unknown supervisor action: %r" % (action,))


def run_command(argv, timeout_s=30.0):
    """The default runner: (returncode, stdout, stderr). Never raises.

    LIST ARGS, shell=False (the default, stated here because it is the
    security property). A timeout is mandatory -- schtasks and launchctl
    are quick, but this is called from a worker thread that must not hang
    forever on a wedged service.
    """
    try:
        proc = subprocess.run(list(argv), capture_output=True, text=True,
                              timeout=timeout_s)
    except subprocess.TimeoutExpired:
        return 1, "", "timed out after %ss: %s" % (timeout_s, argv[0])
    except OSError as e:
        return 1, "", "%s: %s" % (type(e).__name__, e)
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def parse_supervisor_status(platform, stdout, stderr="", returncode=None):
    """Read a supervisor's status output. TOTAL -- never raises, and
    never guesses.

    Returns {"registered", "state", "enabled", "last_result", "pid",
    "repetition", "query_failed", "detail"}.

    STDERR IS NOT OPTIONAL DECORATION. Measured: `schtasks /Query /TN
    <missing>` writes ZERO bytes to stdout and puts
    "ERROR: The system cannot find the file specified." on STDERR with
    exit 1. A parser handed only stdout therefore never reached its own
    not-registered branch in production -- it fell into "the supervisor
    returned nothing" -- and the test that covered that branch was
    feeding stderr text into the stdout parameter, so it passed over a
    path the code could not enter.

    AND THE TWO FAILURES ARE NOT THE SAME. An ACCESS-DENIED query also
    yields empty stdout, so without stderr it is indistinguishable from
    "no task registered" -- and the UI would tell a user whose task is
    fine to run Install and repair it. `query_failed` marks "we could
    not find out", which is not the same claim as "there is nothing
    there", and host_state refuses to report no_supervisor on it.

    THE TRAP, MEASURED: `schtasks /Query /FO LIST /V` prints
    `Repeat: Every: N/A` for a LOGON trigger EVEN WHEN the one-minute
    repetition is correctly stored -- confirmed by exporting the same
    registered task with /XML and reading the Repetition element back.
    The human view simply does not render repetition for this trigger
    type. So `repetition` is ALWAYS None here, meaning UNKNOWN FROM THIS
    VIEW, and must never be reported as False: concluding "no repetition"
    would send someone re-registering a task that is working perfectly.
    """
    result = {"registered": False, "state": "unknown", "enabled": None,
              "last_result": None, "pid": None, "repetition": None,
              "query_failed": False, "detail": ""}
    text = str(stdout or "")
    errors = str(stderr or "")

    # The diagnosis lives in stderr whenever stdout is empty, which is
    # every failure case.
    if not text.strip():
        lowered = errors.lower()
        if "access is denied" in lowered or "permission" in lowered:
            result["query_failed"] = True
            result["detail"] = ("could not read the supervisor state: "
                                "access denied")
        elif ("cannot find the file" in lowered
              or "does not exist" in lowered
              or "could not find service" in lowered
              or "no such process" in lowered):
            result["detail"] = ("nothing is registered to start the host "
                                "app for this user")
        elif errors.strip():
            result["query_failed"] = True
            result["detail"] = errors.strip().splitlines()[0][:200]
        elif returncode:
            result["query_failed"] = True
            result["detail"] = ("the supervisor query failed (exit %s) "
                                "with no output" % (returncode,))
        else:
            result["detail"] = "the supervisor returned nothing"
        return result

    if platform == "win32":
        return _parse_schtasks(text, result)
    return _parse_launchctl(text, result)


def _parse_schtasks(text, result):
    lowered = text.lower()
    # Retained for the case where schtasks puts its error on STDOUT
    # (older builds, and some redirected shells do this). The primary
    # not-registered path is the empty-stdout branch above.
    if ("cannot find the file" in lowered
            or "does not exist" in lowered
            or ("error:" in lowered and "taskname" not in lowered)):
        result["detail"] = ("nothing is registered to start the host app "
                            "for this user")
        return result

    fields = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower()
        # Later fields win: /V output repeats some keys per trigger, and
        # the task-level values come first. Keep the FIRST for the ones
        # we read, so a trigger row cannot overwrite the task's Status.
        if key and key not in fields:
            fields[key] = value.strip()

    if "taskname" not in fields:
        result["detail"] = "unrecognised schtasks output"
        return result
    result["registered"] = True

    status = (fields.get("status") or "").lower()
    state = (fields.get("scheduled task state") or "").lower()
    if state == "disabled" or status == "disabled":
        result["state"] = "disabled"
        result["enabled"] = False
    elif status == "running":
        result["state"] = "running"
        result["enabled"] = True
    elif status in ("ready", "queued"):
        result["state"] = status
        result["enabled"] = True
    else:
        result["state"] = status or "unknown"
        result["enabled"] = True if state == "enabled" else None

    raw_result = fields.get("last result")
    if raw_result is not None:
        try:
            # Signed: schtasks reports negative HRESULTs for some
            # failures, and int() handles the leading '-'.
            result["last_result"] = int(raw_result.strip())
        except (TypeError, ValueError):
            result["last_result"] = None

    # repetition stays None. See the docstring: the LIST view says N/A
    # for a logon trigger even when PT1M is stored.
    result["detail"] = fields.get("taskname", "")
    return result


def _parse_launchctl(text, result):
    lowered = text.lower()
    if ("could not find service" in lowered
            or "no such process" in lowered
            or "could not find" in lowered):
        result["detail"] = "no such agent is loaded for this user"
        return result
    result["registered"] = True
    for line in text.splitlines():
        stripped = line.strip()
        if "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip().lower()
        value = value.strip()
        if key == "state" and result["state"] == "unknown":
            result["state"] = value.lower()
        elif key == "pid" and result["pid"] is None:
            try:
                result["pid"] = int(value)
            except (TypeError, ValueError):
                pass
        elif key == "last exit code" and result["last_result"] is None:
            try:
                result["last_result"] = int(value)
            except (TypeError, ValueError):
                pass
    if result["state"] == "unknown" and result["pid"]:
        result["state"] = "running"
    # launchd's `print` DOES report the job as enabled/disabled through
    # the domain, but not on a line this parser can trust across
    # versions. UNVERIFIED -- left None rather than guessed.
    return result


# -- which Python runs the daemon --------------------------------------

def find_interpreters(platform=None, roots=None):
    """TouchDesigner's bundled Python interpreters on THIS machine.

    Returns [{"path", "build", "windowless"}], newest build first.

    WHY TD'S PYTHON. Not the Envoy venv: that is project-scoped
    (project.folder/.venv), Embody's Uninstall deletes it, and it is
    rebuilt on an ABI change -- a machine-scoped daemon must not die
    because someone deleted a project. Not "system Python": it may not
    exist at all on Windows, and /usr/bin/python3 can trigger the Xcode
    command-line-tools prompt. TD's is machine-scoped, version-stable,
    and on macOS Derivative-signed.

    A NEW, SMALL FUNCTION, not a copy of the bridge's find_td_installs:
    A-44 forbids importing the bridge, and the bridge answers a different
    question (which app can I LAUNCH). The shared fact -- where TD is
    installed -- is pinned by a drift-canary test against
    envoy_bridge._td_install_roots.

    Reads a REAL disk, so it uses os.path throughout (seam 1 in the
    module docstring): `roots` is injected against fixture trees, which
    is how the darwin branch is exercised on Windows and vice versa.
    """
    platform = platform or sys.platform
    if roots is None:
        roots = [os.path.expanduser(r)
                 for r in TD_INSTALL_ROOTS.get(platform, [])]
    found = []
    for root in roots:
        try:
            names = sorted(os.listdir(root))
        except OSError:
            continue
        for name in names:
            install = os.path.join(root, name)
            if not os.path.isdir(install):
                continue
            build = _parse_build(name)
            if platform == "win32":
                if not name.lower().startswith("touchdesigner."):
                    continue
                # pythonw.exe FIRST: console python opens a window that
                # stays up for the daemon's whole life.
                for exe, windowless in (("pythonw.exe", True),
                                        ("python.exe", False)):
                    candidate = os.path.join(install, "bin", exe)
                    if os.path.isfile(candidate):
                        found.append({"path": candidate, "build": build,
                                      "windowless": windowless})
            elif platform == "darwin":
                if not name.lower().startswith("touchdesigner"):
                    continue
                # UNVERIFIED: the interpreter's path inside the bundle is
                # from Derivative's layout, not from a Mac we have run
                # this on. Every plausible location is probed and the
                # first that EXISTS wins, so being wrong about one of
                # them is not fatal -- being wrong about all of them
                # surfaces as "Needs repair -- Python not found".
                for relative in _DARWIN_PYTHON_RELPATHS:
                    candidate = os.path.join(install, *relative)
                    if os.path.isfile(candidate):
                        found.append({"path": candidate, "build": build,
                                      "windowless": True})
                        break
            else:
                candidate = os.path.join(install, "bin", "python")
                if os.path.isfile(candidate):
                    found.append({"path": candidate, "build": build,
                                  "windowless": True})
    found.sort(key=lambda c: (c["build"] or (), c["windowless"]),
               reverse=True)
    return found


# UNVERIFIED, in probe order. A TouchDesigner.app carries a framework
# build of Python; which of these is real has never been checked on a
# Mac.
_DARWIN_PYTHON_RELPATHS = (
    ("Contents", "Frameworks", "Python.framework", "Versions", "Current",
     "bin", "python3"),
    ("Contents", "Frameworks", "Python.framework", "Versions", "3.11",
     "bin", "python3.11"),
    ("Contents", "MacOS", "python3"),
    ("Contents", "Resources", "bin", "python3"),
)


def _parse_build(name):
    """(year, number) out of 'TouchDesigner.2025.33070', or None.

    None sorts LAST (never first): an unparseable directory name must not
    be chosen over a known build just because it sorts high.
    """
    match = re.search(r"(\d{4})\.(\d+)", str(name or ""))
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def choose_interpreter(candidates, prefer_windowless=True):
    """Pick one interpreter from find_interpreters(), or None.

    Newest build wins; within a build, pythonw.exe wins -- a console
    python would leave a window on screen for as long as the daemon runs,
    which on a show machine is not cosmetic.
    """
    usable = [c for c in (candidates or []) if c.get("path")]
    if not usable:
        return None
    if prefer_windowless:
        windowless = [c for c in usable if c.get("windowless")]
        if windowless:
            usable = windowless
    usable.sort(key=lambda c: (c.get("build") or (0, 0),
                               bool(c.get("windowless"))), reverse=True)
    return usable[0]["path"]


# -- THE status computation --------------------------------------------

def host_state(installed=None, probe_status=None, supervisor=None,
               version=None, interpreter_exists=None, pid=None):
    """What the Convoy Host field should say. ONE place, never raises.

    The STRING is convoy_client.host_status_text(state)'s job; this owns
    the DECISION. Splitting it that way is what stops a status being
    computed in two places and drifting.

    Inputs (all already gathered by the caller, on whichever thread):
      installed           read_installed() result, or None
      probe_status        convoy_client.probe().status ('running' when a
                          live host app answered /health)
      supervisor          parse_supervisor_status() result, or None
      version             THIS Embody's version, for the newer-install
                          comparison
      interpreter_exists  isfile(installed['interpreter']); pass None to
                          skip the check

    PRECEDENCE, and the one judgement call in it: needs_repair_python
    outranks running. A TD upgrade deletes the recorded interpreter while
    the daemon it launched keeps running, so "Running" would be true
    today and a permanent silent death tomorrow -- with no warning in
    between, because the failure only appears at the next launch. The
    actionable answer wins, and `live`/`pid` still ride along so a caller
    can say both.
    """
    record = installed if isinstance(installed, dict) else None
    live = probe_status == "running"
    out = {"state": STATE_NOT_INSTALLED,
           "installed_version": (record or {}).get("version"),
           "supervisor": (record or {}).get("supervisor"),
           "live": live, "pid": pid, "detail": ""}

    if record is None:
        out["detail"] = "no Convoy host app is installed for this user"
        return out

    if interpreter_exists is False:
        out["state"] = STATE_NEEDS_REPAIR_PYTHON
        out["detail"] = (
            "the recorded Python (%s) is gone -- a TouchDesigner upgrade "
            "or uninstall usually does this; Install re-resolves it"
            % (record.get("interpreter"),))
        return out

    ours = _version_key(version)
    theirs = _version_key(record.get("version"))
    if ours is not None and theirs is not None and theirs > ours:
        out["state"] = STATE_NEWER_INSTALL
        out["detail"] = (
            "version %s was installed by a newer Embody; this project "
            "(%s) will not change it" % (record.get("version"), version))
        return out

    if record.get("supervisor") == SUPERVISOR_EXTERNAL:
        out["state"] = STATE_EXTERNAL_SUPERVISOR
        out["detail"] = ("another supervisor manages this host app; "
                         "Embody registered no task or agent")
        return out

    if live:
        out["state"] = STATE_RUNNING
        out["detail"] = "the host app answered /health"
        return out

    status = supervisor if isinstance(supervisor, dict) else None
    if status is not None and status.get("query_failed"):
        # WE DID NOT FIND OUT. "Nothing is registered" is a claim we
        # have no evidence for -- an access-denied query looks exactly
        # like an absent task -- and acting on it would tell a user
        # whose supervisor is fine to reinstall it. not_running is the
        # weaker, true statement: the host app is installed and is not
        # answering.
        out["state"] = STATE_NOT_RUNNING
        out["detail"] = str(status.get("detail")
                            or "the supervisor state could not be read")
        return out
    if status is None or not status.get("registered"):
        out["state"] = STATE_NO_SUPERVISOR
        out["detail"] = ("installed, but nothing is registered to start "
                         "it -- use Install to repair")
        return out
    if status.get("enabled") is False or status.get("state") == "disabled":
        out["state"] = STATE_STOPPED
        out["detail"] = "the supervisor is registered but disabled"
        return out
    out["state"] = STATE_NOT_RUNNING
    out["detail"] = ("registered and enabled, but nothing is answering; "
                     "the supervisor restarts it within a minute")
    return out


# -- uninstall preview --------------------------------------------------

def count_jobs(data_dir, platform=None):
    """(total delivery records, indeterminate ones). Never raises.

    Reads the job files directly rather than importing HostStore -- that
    module ships only in the payload, and this runs in TD. It mirrors
    HostStore.jobs()'s filter exactly: idem_* markers and _-prefixed
    bookkeeping files are not deliveries and must not be counted.
    """
    directory = _join(platform)(install_root(data_dir), "jobs")
    total = 0
    indeterminate = 0
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return 0, 0
    for name in names:
        if (not name.endswith(".json") or name.startswith("_")
                or name.startswith("idem_")):
            continue
        total += 1
        try:
            with open(os.path.join(directory, name),
                      "r", encoding="utf-8") as f:
                job = json.load(f)
        except (OSError, ValueError):
            continue
        if isinstance(job, dict) and job.get("state") == "indeterminate":
            indeterminate += 1
    return total, indeterminate


def plan_host_uninstall(data_dir, platform=None, home=None):
    """The uninstall PREVIEW: exactly what goes, exactly what stays.

    Returns {"remove", "remove_dirs", "retain", "retain_present",
    "jobs", "indeterminate"}.

    `retain` is the CANONICAL list, not a filtered one: these paths are
    never touched whether or not they exist today, and a preview that
    silently dropped host.json because this user has not run a job yet
    would be promising less than the design guarantees. `retain_present`
    is the subset that actually exists, for a dialog that wants to count
    honestly.

    WHAT STAYS, AND WHY IT IS NOT NEGOTIABLE: host.json (the host
    identity and node registry), host.token, the jobs/ records and
    audit.jsonl. 16.4/A-15 make indeterminate job records PERMANENT BY
    DESIGN, and A-41 forbids uninstall as an evidence-destruction path --
    deleting them here would quietly destroy the exact evidence the
    design promises to keep. Deleting host state is a SEPARATE second
    action with its own confirmation, and a re-install after it mints a
    NEW host_id.
    """
    join = _join(platform)
    root = install_root(data_dir)
    remove = [launcher_path(data_dir, platform),
              task_xml_path(data_dir, platform),
              installed_path(data_dir, platform)]
    remove_dirs = []
    incomplete = []
    usable, stray = _app_subdirs(data_dir, platform)
    for version in usable:
        target = app_dir(data_dir, version, platform)
        manifest = read_manifest(data_dir, version, platform)
        if manifest is None:
            # An interrupted install: files, but no manifest naming
            # them. We will NOT delete what we cannot prove we wrote --
            # so the preview must not promise removal either. Named
            # under `incomplete` so the dialog can say it is being left
            # behind, instead of listing a phantom .complete under
            # `remove` and silently leaving the sources on disk.
            try:
                incomplete.extend(join(target, n)
                                  for n in sorted(os.listdir(target)))
            except OSError:
                pass
            continue
        for name in manifest.get("files") or []:
            if _bare_name(name) is not None:
                remove.append(join(target, name))
        remove.append(join(target, COMPLETE_FILE))
        # Our own bytecode cache -- a derived artifact of OUR modules in
        # OUR directory. Listed explicitly so the deletion is stated in
        # the dialog rather than performed silently.
        cache = join(target, "__pycache__")
        try:
            for name in sorted(os.listdir(cache)):
                if name.endswith((".pyc", ".pyo")):
                    remove.append(join(cache, name))
            remove_dirs.append(cache)
        except OSError:
            pass
        remove_dirs.append(target)
    remove_dirs.extend([app_dir(data_dir, None, platform),
                        bin_dir(data_dir, platform)])
    if platform == "darwin" and home:
        remove.append(plist_path(home, platform))

    retain = [join(root, name) for name in RETAINED_NAMES]
    retain.extend(join(root, name) for name in RETAINED_DIRS)
    # The log is diagnostic, not evidence, but it is also the only record
    # of WHY a host app was failing -- so it is retained too, and the
    # dialog can say so.
    retain.append(log_path(data_dir, platform))
    retain_present = [p for p in retain if os.path.exists(p)]

    jobs, indeterminate = count_jobs(data_dir, platform)
    return {"remove": remove, "remove_dirs": remove_dirs,
            "retain": retain, "retain_present": retain_present,
            # Left behind and SAID SO: `incomplete` is an interrupted
            # payload we cannot prove we wrote, `stray` is a directory
            # under app/ that is not a version at all. Neither is
            # deleted; both used to be invisible.
            "incomplete": incomplete, "stray": stray,
            "jobs": jobs, "indeterminate": indeterminate}


# -- the four actions (thin; every seam injected) -----------------------

# How long to wait for the daemon to actually be GONE after it answers
# /shutdown. Measured 2026-08-01: an idle daemon needs ~0.52 s to unwind
# main()'s finally and clear the portfile (the 0.5 s serve_forever poll
# dominates), and ~27.9 s with one dispatch forward in flight against an
# unresponsive node; stop_drain_loop's own bound is 2*30+5 = 65 s. Two
# schtasks spawns take ~57 ms, so WITHOUT a wait the supervisor stop
# lands ~460 ms into a ~520 ms unwind on EVERY ordinary stop.
#
# 15 s is a deliberate middle: it covers the ordinary stop many times
# over and most of a forward, without freezing a UI behind the 65 s
# worst case. Past it we proceed to the supervisor stop anyway -- that
# is the backstop -- and REPORT exited:False rather than pretending.
EXIT_WAIT_S = 15.0
EXIT_POLL_S = 0.1


def _await_exit(is_running, timeout_s=EXIT_WAIT_S, sleep=None, now=None):
    """Poll until the daemon is gone, or the bound expires.

    Returns True if it was observed to exit (or there was nothing to
    wait for). `is_running`, `sleep` and `now` are injected so the whole
    thing is testable without a real daemon and without real time.
    """
    if is_running is None:
        return True             # caller gave us no way to observe
    sleep = sleep or time.sleep
    now = now or time.time
    deadline = now() + max(0.0, timeout_s)
    while True:
        try:
            if not is_running():
                return True
        except Exception:
            # An observer that cannot answer must not strand the stop.
            return False
        if now() >= deadline:
            return False
        sleep(EXIT_POLL_S)


def _ok(**fields):
    out = {"ok": True}
    out.update(fields)
    return out


def _failed(reason, detail="", **fields):
    out = {"ok": False, "reason": reason, "detail": str(detail)[:500]}
    out.update(fields)
    return out


def install(data_dir, version, modules, interpreter, platform=None,
            runner=None, home=None, drain_interval=None, installed_by=None,
            supervisor=None, now=None, user=None, env=None, uid=None):
    """Write the payload, write the launcher, register the supervisor,
    record the install. NEVER RAISES -- it is called from a worker.

    ORDER IS THE CORRECTNESS ARGUMENT, and it is the same one
    write_payload makes one level down:
      payload (.complete last) -> launcher -> supervisor definition ->
      register -> installed.json LAST OF ALL.
    installed.json is what the launcher reads to find its payload, so
    writing it earlier would mean a crash mid-install left a record
    pointing at files that do not exist. Written last, a crash leaves the
    PREVIOUS install intact and running.

    `supervisor` forces the kind; the default follows the platform.
    SUPERVISOR_EXTERNAL writes everything and registers NOTHING (A-36:
    never two supervisors).
    """
    platform = platform or sys.platform
    run = runner or run_command
    try:
        version = safe_version(version)
    except ValueError as e:
        return _failed("bad_version", e)
    if not interpreter:
        return _failed("no_interpreter",
                       "no TouchDesigner Python was found for this user")

    kind = supervisor or (SUPERVISOR_TASK if platform == "win32"
                          else SUPERVISOR_AGENT)
    interval = (DEFAULT_DRAIN_INTERVAL_S if drain_interval is None
                else drain_interval)
    steps = []
    try:
        manifest = write_payload(data_dir, version, modules,
                                 platform=platform, now=now)
        steps.append("payload")

        launcher = launcher_path(data_dir, platform)
        _atomic_write(launcher, render_launcher(platform, data_dir))
        steps.append("launcher")
        # The daemon writes here from its first line; creating it now
        # means a permissions problem surfaces during Install, with a
        # dialog in front of the user, instead of silently at 3am.
        os.makedirs(logs_dir(data_dir, platform), exist_ok=True)

        registered = False
        # Recorded on every platform (it is what the task was registered
        # for, and the first thing to check when a supervisor stops
        # firing after an account change), required only on win32.
        # `env` is injected (D-5) so the "cannot name the account"
        # refusal is testable -- without it a test could only fall
        # through to the real environment, which always HAS a username.
        account = user or current_user_account(platform, env)
        if kind == SUPERVISOR_TASK:
            # Refuse BEFORE writing the XML rather than registering a
            # task for "any user": that is an administrator-only
            # registration and schtasks answers Access is denied
            # (measured 2026-08-01 -- see render_task_xml).
            if not account:
                return _failed(
                    "no_user_account",
                    "could not determine this Windows account (USERNAME "
                    "is unset), and a Scheduled Task must name the user "
                    "whose logon starts it",
                    steps=steps)
            xml_file = task_xml_path(data_dir, platform)
            os.makedirs(os.path.dirname(xml_file), exist_ok=True)
            with open(xml_file, "wb") as f:
                f.write(render_task_xml(interpreter, launcher, account))
            steps.append("task_xml")
            code, out, err = run(supervisor_argv(
                "register", platform, xml_path=xml_file))
            if code != 0:
                return _failed("register_failed",
                               (err or out or "").strip(),
                               steps=steps, returncode=code)
            registered = True
        elif kind == SUPERVISOR_AGENT:
            if not home:
                return _failed("no_home",
                               "a LaunchAgent needs the user's home dir")
            agent = plist_path(home, platform)
            _atomic_write(agent, render_launch_agent_plist(
                interpreter, launcher, data_dir))
            steps.append("plist")
            # ENABLE BEFORE BOOTSTRAP, and this is not belt-and-braces.
            # launchctl's disabled state is PERSISTENT, lives OUTSIDE the
            # plist, is keyed by the constant label, and survives boots.
            # stop() and uninstall() both disable (they must -- KeepAlive
            # would otherwise resurrect the agent in about a second), so
            # without this the plan's designated repair path (Stop ->
            # Install, Uninstall -> Install) leaves the agent permanently
            # unloadable. The Windows twin is safe only by accident of
            # mechanism: schtasks /Create /F rewrites the whole
            # definition including <Enabled>true</Enabled>, so the
            # asymmetry is invisible on the platform we can test.
            # A failure here is tolerated: enabling an already-enabled
            # label reports differently across macOS versions, and it
            # must not turn a good install into a failed one.
            run(supervisor_argv("enable", platform, uid=uid))
            steps.append("enable")
            code, out, err = run(supervisor_argv(
                "register", platform, plist_path=agent))
            if code != 0:
                return _failed("register_failed",
                               (err or out or "").strip(),
                               steps=steps, returncode=code)
            registered = True

        record = {
            "version": version,
            "interpreter": str(interpreter),
            "launcher": launcher,
            "supervisor": kind,
            "account": account or "",
            "drain_interval": interval,
            "installed_at": (now or time.time)(),
            "installed_by": str(installed_by or ""),
            "files": manifest.get("files", []),
            "format": "convoy-install/1",
        }
        write_installed(data_dir, record, platform)      # LAST
        steps.append("installed.json")
        return _ok(version=version, supervisor=kind, registered=registered,
                   launcher=launcher, interpreter=str(interpreter),
                   steps=steps, record=record)
    except Exception as e:
        return _failed("install_failed", "%s: %s" % (type(e).__name__, e),
                       steps=steps)


def start(platform=None, runner=None, uid=None, home=None):
    """Enable the supervisor, then run it now. Never raises.

    NO data_dir, deliberately: a supervisor is addressed by task name or
    agent label -- both per-user constants -- never by path. Carrying an
    argument this function cannot use would imply it acts on that
    directory, and the first reader to believe that would point start()
    at one data dir while it started the supervisor for another.

    ENABLE FIRST: Stop disabled it (it has to -- see stop()), and
    /Run on a disabled task silently does nothing on Windows.

    On darwin there is an extra step, because stop() there is `bootout`,
    which removes the job from the launchd domain entirely: `kickstart`
    would have nothing to kick. So the agent is bootstrapped back in
    before it is kicked, and a failure of THAT is tolerated -- the usual
    reason is that it was never booted out in the first place.

    Only the final START has to succeed. enable/bootstrap are no-ops on
    an already-live supervisor and report differently across versions;
    treating their noise as failure would make a working Start read
    broken.
    """
    platform = platform or sys.platform
    run = runner or run_command
    results = []

    def step(action, **kw):
        code, out, err = run(supervisor_argv(action, platform, uid=uid, **kw))
        results.append({"action": action, "returncode": code,
                        "stdout": out, "stderr": err})
        return code

    try:
        step("enable")
        if platform == "darwin" and home:
            step("register", plist_path=plist_path(home, platform))
        code = step("start")
        if code != 0:
            final = results[-1]
            return _failed("start_failed",
                           (final["stderr"] or final["stdout"] or "").strip(),
                           results=results)
        return _ok(results=results)
    except Exception as e:
        return _failed("start_failed", "%s: %s" % (type(e).__name__, e),
                       results=results)


def stop(platform=None, runner=None, uid=None, shutdown=None,
         is_running=None, exit_timeout_s=EXIT_WAIT_S, sleep=None):
    """Ask the daemon to exit, WAIT FOR IT TO GO, then stop the
    supervisor -- and DISABLE it first, or it comes straight back.
    Never raises.

    THE WAIT IS THE POINT, and its absence made /shutdown pointless. The
    route answers `{"stopping": true}` -- not "stopped" -- and the
    daemon then needs ~0.52 s to unwind main()'s finally and clear its
    portfile (up to ~65 s with a forward in flight). Firing `schtasks
    /End` ~460 ms later lands INSIDE that unwind on every ordinary stop,
    which is precisely the hard kill the route exists to avoid: a
    portfile naming a dead port. `is_running` is the observer (the
    caller builds it from read_live_portfile, which verifies the writer
    pid); without one we cannot wait and say so by reporting
    exited:None.

    THE TWO STOP PATHS ARE NOT EQUIVALENT. Measured 2026-08-01 against a
    real Scheduled Task driving this module's own launcher:

      GRACEFUL (POST /shutdown, then this wait) -- main()'s `finally`
        runs: drain loop stopped, PORTFILE CLEARED, "stopped" audited,
        db closed, singleton released.
      SUPERVISOR (`schtasks /End`) -- WORKS, and works fast: rc=0, the
        daemon process was gone in 1.3 s and the task returned to Status
        Ready. But it is a KILL: main()'s `finally` never runs, so THE
        PORTFILE IS LEFT BEHIND NAMING A DEAD PID (observed: the file
        still read pid 72712 / port 11830 after the process was gone).

    That leftover is not a defect and nothing here should try to clean
    it up -- no stop path can cover SIGKILL or power loss anyway. It is
    survivable for exactly one reason: read_live_portfile verifies the
    writer is alive before handing the port out, and it correctly
    REJECTED that stale file. Every client goes through it, never
    read_portfile. If that pid check is ever weakened, this path starts
    handing out dead ports -- so it is pinned by a test.

    So: prefer the graceful path, treat `/End` as the backstop, and read
    exited:False as "the supervisor killed it" rather than as failure.

    No data_dir, for the same reason as start(): the supervisor is
    addressed by name, and the daemon is reached through the injected
    `shutdown` callable, which already carries its own port and token.

    THE ORDER IS THE WHOLE POINT. On Windows the repetition trigger
    restarts the daemon within 60 seconds and the Stop button looks
    broken; on macOS KeepAlive does it in about one. So: disable, then
    end/bootout.

    `shutdown` is the authenticated POST /shutdown call, injected because
    it needs a live probe result and a token that only the caller has.
    It runs FIRST so the daemon clears its own portfile; a hard stop
    afterwards would leave one naming a dead port.
    """
    platform = platform or sys.platform
    run = runner or run_command
    results = []
    shutdown_result = None
    exited = None
    try:
        if shutdown is not None:
            try:
                shutdown_result = shutdown()
            except Exception as e:
                # A daemon that will not answer is exactly why the
                # supervisor stop below exists. Record and continue.
                shutdown_result = {"ok": False,
                                   "detail": "%s: %s" % (type(e).__name__, e)}
            if is_running is not None:
                exited = _await_exit(is_running, exit_timeout_s, sleep)
        for action in ("disable", "stop"):
            code, out, err = run(supervisor_argv(action, platform, uid=uid))
            results.append({"action": action, "returncode": code,
                            "stdout": out, "stderr": err})
        return _ok(results=results, shutdown=shutdown_result, exited=exited)
    except Exception as e:
        return _failed("stop_failed", "%s: %s" % (type(e).__name__, e),
                       results=results, shutdown=shutdown_result,
                       exited=exited)


def uninstall(data_dir, platform=None, runner=None, uid=None, home=None,
              shutdown=None, is_running=None, exit_timeout_s=EXIT_WAIT_S,
              sleep=None):
    """Remove the host app, KEEP its record. Never raises.

    Order: stop the daemon (so the portfile is cleared and no file is in
    use) -> unregister the supervisor -> unlink exactly the manifest
    entries -> unlink the launcher, task XML, plist and installed.json ->
    rmdir the now-empty directories.

    RMDIR ONLY, never shutil.rmtree -- see remove_payload. Anything the
    user put in these directories survives and is reported.

    host.json, host.token, jobs/ and audit.jsonl are NOT touched. See
    plan_host_uninstall for why that is not negotiable.

    THE PORTFILE IS host.portfile.json, WHICH IS RETAINED -- and after a
    supervisor kill it can be STALE. Measured 2026-08-01: `schtasks /End`
    stops the daemon in 1.3 s (task returns to Status Ready) but skips
    main()'s `finally`, so the portfile survives naming a dead pid. That
    is why the graceful path runs first here: POST /shutdown lets the
    daemon clear its own portfile before we start unlinking the modules
    underneath it. When it cannot be reached, the stale file is left in
    place deliberately -- a re-install mints a new one, and every client
    reads through read_live_portfile, which rejects a portfile whose
    writer is dead.
    """
    platform = platform or sys.platform
    run = runner or run_command
    removed, kept, remaining = [], [], []
    results = []
    shutdown_result = None
    exited = None
    try:
        if shutdown is not None:
            try:
                shutdown_result = shutdown()
            except Exception as e:
                shutdown_result = {"ok": False,
                                   "detail": "%s: %s" % (type(e).__name__, e)}
            if is_running is not None:
                # Even more load-bearing here than in stop(): below we
                # unlink the payload modules and installed.json out from
                # under a daemon that may still be running.
                exited = _await_exit(is_running, exit_timeout_s, sleep)
        for action in ("disable", "unregister"):
            code, out, err = run(supervisor_argv(action, platform, uid=uid))
            results.append({"action": action, "returncode": code,
                            "stdout": out, "stderr": err})

        usable, stray = _app_subdirs(data_dir, platform)
        for version in usable:
            outcome = remove_payload(data_dir, version, platform)
            removed.extend(outcome["removed"])
            kept.extend(outcome["kept"])
            # What survived, reported instead of dropped. Previously
            # remove_payload's removed_dir was discarded entirely, so a
            # payload dir left behind by __pycache__ or an interrupted
            # install went unmentioned and uninstall claimed kept=[].
            if not outcome["removed_dir"]:
                remaining.extend(outcome["remaining"])
        # Directories under app/ that are not versions at all. Never
        # touched, always named -- they are not ours to delete.
        remaining.extend(stray)

        files = [launcher_path(data_dir, platform),
                 task_xml_path(data_dir, platform),
                 installed_path(data_dir, platform)]
        if platform == "darwin" and home:
            files.append(plist_path(home, platform))
        for path in files:
            try:
                os.unlink(path)
                removed.append(path)
            except FileNotFoundError:
                pass
            except OSError:
                kept.append(path)

        for directory in (app_dir(data_dir, None, platform),
                          bin_dir(data_dir, platform)):
            try:
                os.rmdir(directory)
            except OSError:
                # Absent, or holding something we did not put there. An
                # absent directory is a clean outcome; a non-empty one is
                # a fact the caller has to be told.
                if os.path.isdir(directory):
                    remaining.append(directory)
        return _ok(removed=removed, kept=kept, remaining=remaining,
                   complete=not remaining, results=results,
                   shutdown=shutdown_result, exited=exited,
                   retained=plan_host_uninstall(data_dir, platform,
                                                home)["retain"])
    except Exception as e:
        return _failed("uninstall_failed", "%s: %s" % (type(e).__name__, e),
                       removed=removed, kept=kept, remaining=remaining,
                       results=results, shutdown=shutdown_result,
                       exited=exited)
