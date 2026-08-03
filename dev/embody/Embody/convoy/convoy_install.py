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
module contents (the host-app sources) arrive as PLAIN STRINGS the
main thread already read off the DATs -- this module never looks one up.

WHAT IS BEING INSTALLED, stated plainly because the code should not read
softer than the dialog: a small Python program and a separately released,
self-contained Convoy CPython runtime in the per-user data dir, plus a
PER-USER Scheduled Task (macOS: LaunchAgent) that starts it at login and
restarts it within a minute. It runs whenever the user is logged in,
whether or not TouchDesigner is open. The source payload is user-writable;
the runtime archive is release-hash pinned, installed offline, and live-
probed for cryptography before it is trusted. Packaging/signing/notarizing
that platform asset belongs to the release build. This installer creates
no firewall rule and never downloads a runtime.

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

macOS SUPERVISION IS UNVERIFIED. Generated and unit-tested here, never run
on a Mac: launchctl bootstrap in a GUI login session, macOS 13+ Login Items
gating, and ProcessType/App Nap. The managed runtime target is Apple
Silicon only and must be signed/notarized by the release build. Where a
specific fact is guessed rather than known, the comment says UNVERIFIED.
Do not quietly promote any of them.
"""

import hashlib
import json
import ntpath
import os
import platform as platform_lib
import posixpath
import re
import stat
import subprocess
import sys
import time
import zipfile
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
#   runtime/<runtime-id>/... + .complete     <- offline managed CPython
#   bin/convoy_host_launch.py                <- STABLE path, never moves
#   installed.json                           <- app/runtime versions, by whom
#   logs/host.log
#
# Versioned payload + a stable launcher path is what makes an Embody
# upgrade a FILE REWRITE: the task/agent points at bin/ forever and is
# never re-registered, so upgrading cannot lose supervision.
APP_SUBDIR = "app"
BIN_SUBDIR = "bin"
LOGS_SUBDIR = "logs"
RUNTIME_SUBDIR = "runtime"
INSTALLED_FILE = "installed.json"
COMPLETE_FILE = ".complete"
RUNTIME_MANIFEST_FILE = "convoy-runtime.json"
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
    # Phase 3 slice 3 (the LAN transport). convoy_hostapp imports all
    # three at module load: convoy_lan (the lan.json switch + bind-address
    # selection), convoy_peerserver (the mutual-TLS peer listener), and
    # convoy_peerclient (the pinned client). A payload missing any of them
    # cannot start the daemon. They bind NOTHING off-box unless lan.json
    # enables it -- absent lan.json (the shipped state) means no LAN
    # socket -- so vendoring them changes no default behaviour.
    "convoy_lan.py",
    "convoy_peerserver.py",
    "convoy_peerclient.py",
    # Convoy's completed host-owned slices.  Every one is imported directly
    # or transitively by convoy_hostapp at daemon startup; omitting even a
    # seemingly optional helper makes the offline-installed payload fail at
    # login before TouchDesigner is available to explain it.
    "convoy_discovery.py",
    "convoy_realm.py",
    "convoy_policy.py",
    "convoy_artifacts.py",
    "convoy_artifact_http.py",
    "convoy_wake.py",
    "convoy_hostops.py",
    # Optional public-API inventory/command-status consumer.  It is inert
    # until the loopback bridge calls it and never makes Owlette a host-app
    # dependency, but the daemon imports the module at startup so the offline
    # payload must carry it.
    "convoy_owlette.py",
    # Host-native exact-process start/restart coordination.  This stays in
    # the background app so a stopped TouchDesigner node can be launched
    # without turning arbitrary shell access into a prerequisite.
    "convoy_lifecycle.py",
    # Full-duplex pinned mTLS control sessions. HTTPS remains the artifact
    # plane and compatibility fallback, but the daemon imports both modules
    # at startup even before a peer session is established.
    "convoy_ws.py",
    "convoy_sessions.py",
)

# Autonomous dispatch, ON for an installed host app: a supervised daemon
# that never drains its own queue would relay nothing unless something
# called /drain. Recorded in installed.json so it can change without
# re-registering the supervisor.
DEFAULT_DRAIN_INTERVAL_S = 2.0

# Hard cap for the launcher's process-lifetime bounded writer. stdout and
# stderr share that writer, which checks the byte count under one lock before
# every write and truncates/restarts the file when the next write would cross
# the ceiling. This covers both crash loops and one healthy daemon running
# for months; it is deliberately a cap, not archival log rotation.
LOG_MAX_BYTES = 4 * 1024 * 1024

# The daemon does NOT run under TouchDesigner or a project .venv. Its
# interpreter is a separately versioned, self-contained CPython bundle in
# the per-user Convoy data directory. A release process prepares one bundle
# per supported target with cryptography already installed; installation is
# deliberately offline and accepts only an archive whose SHA-256 came from
# trusted release metadata. There is no downloader in this module.
RUNTIME_BUNDLE_FORMAT = "embody-convoy-runtime/1"
RUNTIME_RECEIPT_FORMAT = "embody-convoy-runtime-install/1"
RUNTIME_PROBE_FORMAT = "embody-convoy-runtime-probe/1"
RUNTIME_CATALOG_FORMAT = "embody-convoy-runtime-catalog/1"
RUNTIME_RELEASE_FORMAT = "embody-convoy-runtime-release/1"
RUNTIME_MANIFEST_MAX_BYTES = 256 * 1024
RUNTIME_CATALOG_MAX_BYTES = 256 * 1024
RUNTIME_MAX_FILES = 20000
RUNTIME_MAX_UNCOMPRESSED_BYTES = 768 * 1024 * 1024
# A valid ZIP can be slightly larger than the bytes it stores. This bound is
# intentionally a little above the uncompressed ceiling, while still making
# a accidentally selected multi-gigabyte file fail before Convoy hashes it.
RUNTIME_MAX_ARCHIVE_BYTES = (RUNTIME_MAX_UNCOMPRESSED_BYTES
                             + RUNTIME_MANIFEST_MAX_BYTES + 16 * 1024 * 1024)
RUNTIME_PROBE_TIMEOUT_S = 15.0
SUPPORTED_RUNTIME_TARGETS = frozenset((
    ("win32", "x86_64"),
    ("darwin", "arm64"),
))
RUNTIME_SIGNATURE_ATTESTATIONS = {
    ("win32", "x86_64"): "authenticode-verified",
    ("darwin", "arm64"): "developer-id-notarized-verified",
}

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
    raw = str(version or "")
    text = raw.strip()
    if (raw != text or text in ("", ".", "..")
            or not _VERSION_OK.fullmatch(text)):
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


def runtime_dir(data_dir, runtime_id=None, platform=None):
    """<root>/runtime, or one content-versioned managed runtime.

    Runtime IDs obey the same one-segment rule as app versions. A runtime
    is shared by every local Embody node and survives ordinary Embody app
    upgrades; it is never project-scoped.
    """
    join = _join(platform)
    base = join(install_root(data_dir), RUNTIME_SUBDIR)
    if runtime_id is None:
        return base
    return join(base, safe_version(runtime_id))


def runtime_complete_path(data_dir, runtime_id, platform=None):
    return _join(platform)(runtime_dir(data_dir, runtime_id, platform),
                           COMPLETE_FILE)


def runtime_manifest_path(data_dir, runtime_id, platform=None):
    return _join(platform)(runtime_dir(data_dir, runtime_id, platform),
                           RUNTIME_MANIFEST_FILE)


def _runtime_fs_dir(data_dir, runtime_id=None):
    """Runtime path on THIS filesystem, for discovery/extraction I/O."""
    base = os.path.join(install_root(data_dir), RUNTIME_SUBDIR)
    if runtime_id is None:
        return base
    return os.path.join(base, safe_version(runtime_id))


def default_data_dir(platform=None, env=None, home=None):
    """Convoy's per-user data directory without importing convoy_client.

    The installer is vendored independently and must stay importable before
    the client module. This deliberately mirrors convoy_client.data_dir;
    tests pin the literal paths so the two copies cannot drift unnoticed.
    """
    platform = platform or sys.platform
    env = os.environ if env is None else env
    home = home or env.get("HOME") or os.path.expanduser("~")
    join = _join(platform)
    if platform == "win32":
        base = env.get("LOCALAPPDATA") or join(home, "AppData", "Local")
        return join(base, "EmbodyConvoy")
    if platform == "darwin":
        return join(home, "Library", "Application Support", "EmbodyConvoy")
    return join(home, ".local", "share", "EmbodyConvoy")


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
    tmp = None
    try:
        for attempt in range(100):
            candidate = "%s.%s-%s-%s.tmp" % (
                path, os.getpid(), time.time_ns(), attempt)
            try:
                descriptor = os.open(
                    candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                tmp = candidate
                break
            except FileExistsError:
                continue
        if tmp is None:
            raise OSError("could not reserve a unique atomic-write temp file")
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        for replace_attempt in range(10):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                # Windows can transiently deny two same-process replacements
                # of the same destination even after both source handles are
                # closed. Keep the retry short and bounded; any persistent ACL
                # or sharing violation still surfaces to the caller.
                if replace_attempt == 9:
                    raise
                time.sleep(0.005 * (replace_attempt + 1))
        tmp = None
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


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
    if text in (".", "..") or not _BARE_NAME_OK.fullmatch(text):
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


# -- the isolated managed runtime -------------------------------------

def normalize_architecture(machine=None):
    """Canonical runtime architecture, or a lowercase unknown value."""
    value = str(machine or platform_lib.machine() or "").strip().lower()
    aliases = {
        "amd64": "x86_64",
        "x64": "x86_64",
        "x86-64": "x86_64",
        "aarch64": "arm64",
        "arm64e": "arm64",
    }
    return aliases.get(value, value)


def _declared_architecture(value):
    """Normalize metadata without treating a missing field as this machine."""
    if value is None or not str(value).strip():
        return ""
    return normalize_architecture(value)


def runtime_target_supported(platform=None, architecture=None):
    platform = platform or sys.platform
    architecture = normalize_architecture(architecture)
    return (platform, architecture) in SUPPORTED_RUNTIME_TARGETS


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _valid_sha256(value):
    text = str(value or "").strip().lower()
    return text if re.match(r"^[0-9a-f]{64}$", text) else None


def _safe_runtime_relpath(value):
    """A portable, nested path inside a runtime archive, or None.

    Runtime bundles use POSIX separators on every platform. Every component
    uses the same accept-list as payload names, which excludes absolute,
    drive-relative, UNC, dot-dot, control-character and separator tricks by
    construction rather than by a growing deny-list.
    """
    text = str(value or "")
    if (not text or len(text) > 1024 or "\\" in text
            or text.startswith("/") or text.endswith("/")
            or "\x00" in text):
        return None
    parts = text.split("/")
    if not parts or any(_portable_runtime_part(part) is None for part in parts):
        return None
    return "/".join(parts)


_WINDOWS_RESERVED_RUNTIME_NAMES = frozenset(
    ("con", "prn", "aux", "nul")
    + tuple("com%d" % number for number in range(1, 10))
    + tuple("lpt%d" % number for number in range(1, 10)))


def _portable_runtime_part(value):
    """One archive path component valid on every supported filesystem.

    Runtime catalogs are shared by Windows and macOS release tooling. Reject
    Windows device aliases and trailing dots everywhere, and compare paths
    case-insensitively below, so an archive cannot be safe on the build host
    yet overwrite a different member on the install host.
    """
    part = _bare_name(value)
    if part is None or len(part) > 255 or part.endswith("."):
        return None
    stem = part.split(".", 1)[0].lower()
    if stem in _WINDOWS_RESERVED_RUNTIME_NAMES:
        return None
    return part


def _runtime_path_key(path):
    """Portable collision key for supported case-insensitive volumes."""
    return "/".join(part.lower() for part in str(path).split("/"))


def _runtime_file_index(manifest):
    """Validated {relative_path: file_record}, or (None, detail)."""
    records = manifest.get("files") if isinstance(manifest, dict) else None
    if not isinstance(records, list) or not records:
        return None, "runtime manifest files must be a non-empty list"
    if len(records) > RUNTIME_MAX_FILES:
        return None, "runtime manifest names too many files"
    indexed = {}
    portable_paths = {}
    total = 0
    for item in records:
        if not isinstance(item, dict):
            return None, "runtime manifest contains a non-object file entry"
        path = _safe_runtime_relpath(item.get("path"))
        digest = _valid_sha256(item.get("sha256"))
        size = item.get("size")
        mode = item.get("mode", 0o644)
        if path is None:
            return None, "runtime manifest contains an unsafe file path"
        if path in (COMPLETE_FILE, RUNTIME_MANIFEST_FILE):
            return None, "runtime files may not replace installer control files"
        if path in indexed:
            return None, "runtime manifest contains duplicate path %s" % path
        portable_key = _runtime_path_key(path)
        if portable_key in portable_paths:
            return None, ("runtime manifest paths collide on a supported "
                          "filesystem: %s and %s"
                          % (portable_paths[portable_key], path))
        if digest is None:
            return None, "runtime manifest has an invalid SHA-256 for %s" % path
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            return None, "runtime manifest has an invalid size for %s" % path
        if (isinstance(mode, bool) or not isinstance(mode, int)
                or mode < 0 or mode > 0o777):
            return None, "runtime manifest has an invalid mode for %s" % path
        total += size
        if total > RUNTIME_MAX_UNCOMPRESSED_BYTES:
            return None, "runtime bundle exceeds the uncompressed size limit"
        indexed[path] = {"path": path, "sha256": digest,
                         "size": size, "mode": mode}
        portable_paths[portable_key] = path
    portable_keys = set(portable_paths)
    for key in sorted(portable_keys):
        parts = key.split("/")
        for end in range(1, len(parts)):
            prefix = "/".join(parts[:end])
            if prefix in portable_keys:
                return None, ("runtime manifest uses %s as both a file and "
                              "a parent directory" % portable_paths[prefix])
    return indexed, ""


def _validate_runtime_manifest(manifest, platform, architecture):
    """Return a normalized manifest or a named, actionable refusal."""
    if not isinstance(manifest, dict):
        return _failed("runtime_manifest_invalid",
                       "convoy-runtime.json must contain an object")
    if manifest.get("format") != RUNTIME_BUNDLE_FORMAT:
        return _failed("runtime_manifest_invalid",
                       "unsupported runtime bundle format")
    try:
        runtime_id = safe_version(manifest.get("runtime_id"))
    except ValueError as e:
        return _failed("runtime_manifest_invalid", e)
    target_platform = str(manifest.get("platform") or "")
    target_arch = _declared_architecture(manifest.get("architecture"))
    if (target_platform, target_arch) != (platform, architecture):
        return _failed(
            "runtime_target_mismatch",
            "runtime targets %s/%s, this install needs %s/%s"
            % (target_platform or "?", target_arch or "?",
               platform, architecture))
    if not runtime_target_supported(target_platform, target_arch):
        return _failed(
            "runtime_target_unsupported",
            "Convoy currently supports managed runtimes for Windows x64 "
            "and Apple Silicon only")
    python_rel = _safe_runtime_relpath(manifest.get("python"))
    probe_python_rel = _safe_runtime_relpath(manifest.get("probe_python"))
    if python_rel is None:
        return _failed("runtime_manifest_invalid",
                       "runtime manifest has an unsafe Python path")
    if probe_python_rel is None:
        return _failed("runtime_manifest_invalid",
                       "runtime manifest has an unsafe probe-Python path")
    if target_platform == "win32":
        if (posixpath.basename(python_rel).lower() != "pythonw.exe"
                or posixpath.basename(probe_python_rel).lower()
                   != "python.exe"):
            return _failed(
                "runtime_manifest_invalid",
                "Windows runtimes must use pythonw.exe for the daemon and "
                "python.exe for the captured capability probe")
    python_version = str(manifest.get("python_version") or "").strip()
    crypto_version = str(manifest.get("cryptography_version") or "").strip()
    source_revision = str(manifest.get("source_revision") or "").strip()
    if not python_version or not crypto_version or not source_revision:
        return _failed(
            "runtime_manifest_invalid",
            "runtime manifest must name Python, cryptography, and source "
            "revision provenance")
    files, detail = _runtime_file_index(manifest)
    if files is None:
        return _failed("runtime_manifest_invalid", detail)
    if python_rel not in files:
        return _failed("runtime_manifest_invalid",
                       "runtime Python is not listed in manifest files")
    if probe_python_rel not in files:
        return _failed("runtime_manifest_invalid",
                       "runtime probe Python is not listed in manifest files")
    if (target_platform == "darwin"
            and (not files[python_rel]["mode"] & 0o111
                 or not files[probe_python_rel]["mode"] & 0o111)):
        return _failed("runtime_manifest_invalid",
                       "Apple Silicon runtime Python must be executable")
    normalized = dict(manifest)
    normalized.update({
        "runtime_id": runtime_id,
        "platform": target_platform,
        "architecture": target_arch,
        "python": python_rel,
        "probe_python": probe_python_rel,
        "python_version": python_version,
        "cryptography_version": crypto_version,
        "source_revision": source_revision,
        "files": [files[name] for name in sorted(files)],
    })
    return _ok(manifest=normalized, file_index=files)


def _validate_runtime_receipt(receipt, runtime_id, platform, architecture):
    """Validate the installed completion record before discovery trusts it."""
    if not isinstance(receipt, dict):
        return _failed("runtime_receipt_invalid",
                       "managed runtime completion receipt is not an object")
    if receipt.get("format") != RUNTIME_RECEIPT_FORMAT:
        return _failed("runtime_receipt_invalid",
                       "unsupported managed runtime receipt format")
    if receipt.get("runtime_id") != runtime_id:
        return _failed("runtime_receipt_invalid",
                       "managed runtime receipt ID does not match its directory")
    if (receipt.get("platform") != platform
            or _declared_architecture(receipt.get("architecture"))
               != architecture):
        return _failed("runtime_receipt_target_mismatch",
                       "managed runtime receipt targets another platform")
    archive_sha256 = _valid_sha256(receipt.get("archive_sha256"))
    if archive_sha256 is None:
        return _failed("runtime_receipt_invalid",
                       "managed runtime receipt has no release SHA-256")
    python_rel = _safe_runtime_relpath(receipt.get("python"))
    probe_python_rel = _safe_runtime_relpath(receipt.get("probe_python"))
    if python_rel is None or probe_python_rel is None:
        return _failed("runtime_receipt_invalid",
                       "managed runtime receipt has an unsafe Python path")
    if (platform == "win32"
            and (posixpath.basename(python_rel).lower() != "pythonw.exe"
                 or posixpath.basename(probe_python_rel).lower()
                    != "python.exe")):
        return _failed("runtime_receipt_invalid",
                       "Windows runtime receipt must name pythonw.exe and "
                       "python.exe")
    python_version = str(receipt.get("python_version") or "").strip()
    crypto_version = str(receipt.get("cryptography_version") or "").strip()
    source_revision = str(receipt.get("source_revision") or "").strip()
    if not python_version or not crypto_version or not source_revision:
        return _failed("runtime_receipt_invalid",
                       "managed runtime receipt lacks release provenance")
    files, detail = _runtime_file_index(receipt)
    if files is None:
        return _failed("runtime_receipt_invalid", detail)
    if python_rel not in files or probe_python_rel not in files:
        return _failed("runtime_receipt_invalid",
                       "managed runtime receipt does not inventory Python")
    if (platform == "darwin"
            and (not files[python_rel]["mode"] & 0o111
                 or not files[probe_python_rel]["mode"] & 0o111)):
        return _failed("runtime_receipt_invalid",
                       "Apple Silicon runtime receipt lost executable modes")
    return _ok(receipt=receipt, file_index=files, python=python_rel,
               probe_python=probe_python_rel,
               archive_sha256=archive_sha256,
               python_version=python_version,
               cryptography_version=crypto_version,
               source_revision=source_revision)


def _validate_runtime_release(record):
    """Normalize one catalog artifact that release CI has attested."""
    if not isinstance(record, dict):
        return _failed("runtime_catalog_invalid",
                       "runtime catalog artifact must be an object")
    if record.get("format") != RUNTIME_RELEASE_FORMAT:
        return _failed("runtime_catalog_invalid",
                       "runtime catalog artifact has an unsupported format")
    try:
        runtime_id = safe_version(record.get("runtime_id"))
    except ValueError as e:
        return _failed("runtime_catalog_invalid", e)
    target = (str(record.get("platform") or ""),
              _declared_architecture(record.get("architecture")))
    if target not in SUPPORTED_RUNTIME_TARGETS:
        return _failed("runtime_catalog_invalid",
                       "runtime catalog contains an unsupported target")
    asset = _safe_runtime_relpath(record.get("asset"))
    if asset is None:
        return _failed("runtime_catalog_invalid",
                       "runtime catalog artifact has an unsafe local path")
    digest = _valid_sha256(record.get("sha256"))
    if digest is None:
        return _failed("runtime_catalog_invalid",
                       "runtime catalog artifact has no valid SHA-256")
    size = record.get("size")
    if (isinstance(size, bool) or not isinstance(size, int) or size <= 0
            or size > RUNTIME_MAX_ARCHIVE_BYTES):
        return _failed("runtime_catalog_invalid",
                       "runtime catalog artifact has an invalid archive size")
    if record.get("status") != "published":
        return _failed("runtime_catalog_invalid",
                       "only published runtime artifacts belong in the catalog")
    if not isinstance(record.get("current"), bool):
        return _failed("runtime_catalog_invalid",
                       "runtime catalog artifact must declare current true/false")
    required_signature = RUNTIME_SIGNATURE_ATTESTATIONS[target]
    if record.get("signature") != required_signature:
        return _failed(
            "runtime_catalog_invalid",
            "runtime catalog lacks the required %s release attestation"
            % required_signature)
    python_version = str(record.get("python_version") or "").strip()
    crypto_version = str(record.get("cryptography_version") or "").strip()
    source_revision = str(record.get("source_revision") or "").strip()
    if not python_version or not crypto_version or not source_revision:
        return _failed("runtime_catalog_invalid",
                       "runtime catalog artifact lacks release provenance")
    normalized = dict(record)
    normalized.update({
        "runtime_id": runtime_id,
        "platform": target[0],
        "architecture": target[1],
        "asset": asset,
        "sha256": digest,
        "size": size,
        "python_version": python_version,
        "cryptography_version": crypto_version,
        "source_revision": source_revision,
    })
    return _ok(artifact=normalized)


def validate_runtime_catalog(catalog):
    """Validate trusted, release-shipped metadata without touching a network."""
    if not isinstance(catalog, dict):
        return _failed("runtime_catalog_invalid",
                       "Convoy Runtime catalog must contain an object")
    if catalog.get("format") != RUNTIME_CATALOG_FORMAT:
        return _failed("runtime_catalog_invalid",
                       "unsupported Convoy Runtime catalog format")
    policy = catalog.get("policy")
    if (not isinstance(policy, dict)
            or policy.get("network_install") is not False
            or policy.get("release_sha256_required") is not True):
        return _failed("runtime_catalog_invalid",
                       "runtime catalog must forbid network installation and "
                       "require release SHA-256 metadata")
    declared = catalog.get("required_targets")
    if not isinstance(declared, list):
        return _failed("runtime_catalog_invalid",
                       "runtime catalog has no required-target declarations")
    target_status = {}
    for row in declared:
        if not isinstance(row, dict):
            return _failed("runtime_catalog_invalid",
                           "runtime catalog target must be an object")
        target = (str(row.get("platform") or ""),
                  _declared_architecture(row.get("architecture")))
        status = str(row.get("status") or "").strip()
        if target not in SUPPORTED_RUNTIME_TARGETS or not status:
            return _failed("runtime_catalog_invalid",
                           "runtime catalog has an invalid target declaration")
        if target in target_status:
            return _failed("runtime_catalog_invalid",
                           "runtime catalog repeats a target declaration")
        target_status[target] = status
    if set(target_status) != set(SUPPORTED_RUNTIME_TARGETS):
        return _failed("runtime_catalog_invalid",
                       "runtime catalog must declare Windows x64 and Apple "
                       "Silicon release status")

    records = catalog.get("artifacts")
    if not isinstance(records, list) or len(records) > 32:
        return _failed("runtime_catalog_invalid",
                       "runtime catalog artifacts must be a bounded list")
    artifacts = []
    identities = set()
    assets = set()
    current_targets = set()
    for record in records:
        checked = _validate_runtime_release(record)
        if not checked.get("ok"):
            return checked
        artifact = checked["artifact"]
        identity = (artifact["platform"], artifact["architecture"],
                    artifact["runtime_id"])
        asset_key = _runtime_path_key(artifact["asset"])
        if identity in identities or asset_key in assets:
            return _failed("runtime_catalog_invalid",
                           "runtime catalog repeats an artifact identity or path")
        target = identity[:2]
        if artifact["current"]:
            if target in current_targets:
                return _failed("runtime_catalog_invalid",
                               "runtime catalog selects two current artifacts "
                               "for one target")
            current_targets.add(target)
        identities.add(identity)
        assets.add(asset_key)
        artifacts.append(artifact)
    for target, status in target_status.items():
        has_current = target in current_targets
        if has_current != (status == "published"):
            return _failed(
                "runtime_catalog_invalid",
                "runtime target status and current published artifact disagree")
    return _ok(catalog=dict(catalog), artifacts=artifacts,
               target_status=target_status)


def read_runtime_catalog(path):
    """Read a bounded local catalog. URLs and implicit downloads do not exist."""
    try:
        if os.path.islink(path) or not os.path.isfile(path):
            return _failed("runtime_catalog_unreadable",
                           "runtime catalog is absent or is not an ordinary file")
        with open(path, "rb") as stream:
            raw = stream.read(RUNTIME_CATALOG_MAX_BYTES + 1)
        if len(raw) > RUNTIME_CATALOG_MAX_BYTES:
            return _failed("runtime_catalog_invalid",
                           "runtime catalog is unexpectedly large")
        catalog = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, ValueError) as e:
        return _failed("runtime_catalog_unreadable",
                       "%s: %s" % (type(e).__name__, e))
    return validate_runtime_catalog(catalog)


def select_runtime_artifact(catalog, platform=None, architecture=None):
    """Select the one release-designated local bundle for this target."""
    platform = platform or sys.platform
    architecture = normalize_architecture(architecture)
    target = (platform, architecture)
    if target not in SUPPORTED_RUNTIME_TARGETS:
        return _failed(
            "runtime_target_unsupported",
            "Convoy Runtime supports Windows x64 and Apple Silicon; this "
            "machine reports %s/%s" % target)
    checked = validate_runtime_catalog(catalog)
    if not checked.get("ok"):
        return checked
    selected = [row for row in checked["artifacts"]
                if (row["platform"], row["architecture"]) == target
                and row["current"]]
    if not selected:
        status = checked["target_status"].get(target, "not-published")
        return _failed(
            "runtime_bundle_unavailable",
            "no signed offline Convoy Runtime bundle is published for %s/%s "
            "(release status: %s); TouchDesigner Python, system Python, and "
            "network installation are not fallbacks"
            % (platform, architecture, status), release_status=status,
            platform=platform, architecture=architecture)
    return _ok(artifact=selected[0], platform=platform,
               architecture=architecture)


def plan_runtime_from_catalog(catalog, asset_root=None, platform=None,
                              architecture=None):
    """Read-only preflight for one release-pinned local runtime bundle.

    This deliberately stops before hashing, opening the ZIP, extracting, or
    probing an interpreter.  ConvoyExt uses it on TouchDesigner's main thread
    so its confirmation can name the exact offline package that will be used
    and so a missing/empty release fails before a worker or supervisor action
    starts.  ``provision_runtime_from_catalog`` repeats this entire check on
    the worker; the preflight is for honest UX, never a security shortcut.
    """
    if isinstance(catalog, (str, os.PathLike)):
        catalog_path = os.fspath(catalog)
        checked = read_runtime_catalog(catalog_path)
        if asset_root is None:
            asset_root = os.path.dirname(os.path.abspath(catalog_path))
    else:
        checked = validate_runtime_catalog(catalog)
    if not checked.get("ok"):
        return checked
    selected = select_runtime_artifact(
        checked["catalog"], platform, architecture)
    if not selected.get("ok"):
        return selected
    if not asset_root:
        return _failed("runtime_asset_root_required",
                       "parsed runtime catalogs require a local asset root")
    artifact = selected["artifact"]
    try:
        root = os.path.realpath(os.fspath(asset_root))
    except (TypeError, ValueError, OSError) as e:
        return _failed("runtime_asset_root_invalid", e, artifact=artifact)
    bundle = os.path.join(root, *artifact["asset"].split("/"))
    try:
        if (not _actual_inside(root, bundle) or os.path.islink(bundle)
                or not stat.S_ISREG(os.stat(bundle).st_mode)):
            return _failed("runtime_bundle_unavailable",
                           "catalog runtime asset is absent or unsafe",
                           artifact=artifact)
        actual_size = os.path.getsize(bundle)
    except OSError as e:
        return _failed("runtime_bundle_unavailable", e, artifact=artifact)
    if actual_size != artifact["size"]:
        return _failed(
            "runtime_bundle_size_mismatch",
            "runtime archive size does not match trusted release metadata",
            expected_size=artifact["size"], actual_size=actual_size,
            artifact=artifact)
    return _ok(artifact=artifact, bundle=bundle, asset_root=root,
               platform=selected["platform"],
               architecture=selected["architecture"])


def provision_runtime_from_catalog(data_dir, catalog, asset_root=None,
                                   platform=None, architecture=None,
                                   runner=None, now=None):
    """Select and install a catalog-pinned local artifact, entirely offline.

    `catalog` is either an already parsed object or a local JSON filename. A
    filename also supplies the default asset root. For an object, callers must
    pass the directory containing the release assets explicitly. No URL parser,
    downloader, package manager, or fallback interpreter is reachable here.
    """
    planned = plan_runtime_from_catalog(
        catalog, asset_root=asset_root, platform=platform,
        architecture=architecture)
    if not planned.get("ok"):
        return planned
    artifact = planned["artifact"]
    bundle = planned["bundle"]
    result = provision_runtime_bundle(
        data_dir, bundle, artifact["sha256"], artifact["platform"],
        artifact["architecture"], runner=runner, now=now,
        expected_runtime_id=artifact["runtime_id"],
        expected_release=artifact)
    result.setdefault("artifact", artifact)
    return result


_RUNTIME_PROBE_CODE = r'''\
import json
import platform
import ssl
import sys
import sysconfig
import cryptography
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

key = ed25519.Ed25519PrivateKey.generate()
key.private_bytes(serialization.Encoding.Raw,
                  serialization.PrivateFormat.Raw,
                  serialization.NoEncryption())
print(json.dumps({
    "format": "embody-convoy-runtime-probe/1",
    "implementation": platform.python_implementation(),
    "python": list(sys.version_info[:3]),
    "platform": sys.platform,
    "architecture": platform.machine(),
    "executable": sys.executable,
    "prefix": sys.prefix,
    "base_prefix": sys.base_prefix,
    "stdlib": sysconfig.get_path("stdlib"),
    "platstdlib": sysconfig.get_path("platstdlib"),
    "cryptography_version": getattr(cryptography, "__version__", ""),
    "cryptography_file": getattr(cryptography, "__file__", ""),
    "x509": bool(x509),
    "ed25519": True,
    "tls13": bool(getattr(ssl, "HAS_TLSv1_3", False)),
}, sort_keys=True))
'''


def probe_runtime(interpreter, platform=None, architecture=None, runner=None):
    """Run the candidate in isolated mode and prove Convoy's crypto floor.

    This is a live capability probe, not a package-name check: it exercises
    Ed25519, X.509 imports and TLS 1.3 in the exact executable the supervisor
    will launch. It never raises and returns an actionable structured reason.
    """
    platform = platform or sys.platform
    architecture = normalize_architecture(architecture)
    run = runner or run_command
    try:
        code, out, err = run(
            [str(interpreter), "-I", "-c", _RUNTIME_PROBE_CODE],
            timeout_s=RUNTIME_PROBE_TIMEOUT_S)
    except Exception as e:
        return _failed("runtime_probe_failed",
                       "%s: %s" % (type(e).__name__, e))
    if code != 0:
        return _failed(
            "runtime_probe_failed",
            (str(err or out or "managed runtime did not start").strip())[:500],
            returncode=code)
    lines = [line.strip() for line in str(out or "").splitlines()
             if line.strip()]
    try:
        result = json.loads(lines[-1]) if lines else None
    except (TypeError, ValueError):
        result = None
    if not isinstance(result, dict) or result.get("format") != RUNTIME_PROBE_FORMAT:
        return _failed("runtime_probe_invalid",
                       "managed runtime returned no valid capability record")
    if result.get("implementation") != "CPython":
        return _failed("runtime_probe_invalid",
                       "managed runtime must use CPython")
    version = result.get("python")
    if (not isinstance(version, list) or len(version) < 2
            or tuple(version[:2]) < (3, 11)):
        return _failed("runtime_probe_invalid",
                       "managed runtime requires CPython 3.11 or newer")
    actual_platform = str(result.get("platform") or "")
    actual_arch = _declared_architecture(result.get("architecture"))
    if (actual_platform, actual_arch) != (platform, architecture):
        return _failed(
            "runtime_probe_target_mismatch",
            "runtime reported %s/%s, expected %s/%s"
            % (actual_platform or "?", actual_arch or "?",
               platform, architecture))
    if (not result.get("cryptography_version") or not result.get("x509")
            or not result.get("ed25519") or not result.get("tls13")):
        return _failed(
            "runtime_crypto_unavailable",
            "managed runtime must provide cryptography with X.509, Ed25519, "
            "and TLS 1.3")
    return _ok(probe=result)


def read_runtime_receipt(data_dir, runtime_id, platform=None):
    try:
        with open(os.path.join(_runtime_fs_dir(data_dir, runtime_id),
                               COMPLETE_FILE),
                  "r", encoding="utf-8") as stream:
            value = json.load(stream)
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _runtime_candidates_from_root(root, platform, architecture):
    found = []
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return found
    for name in names:
        try:
            runtime_id = safe_version(name)
        except ValueError:
            continue
        # Discovery reads THIS machine's filesystem. Use os.path here,
        # unlike the target-path renderers, so cross-platform fixture trees
        # exercise the Windows and macOS receipt policy honestly.
        target = os.path.join(root, runtime_id)
        if os.path.islink(target) or not _actual_inside(root, target):
            continue
        try:
            with open(os.path.join(target, COMPLETE_FILE),
                      "r", encoding="utf-8") as stream:
                receipt = json.load(stream)
        except (OSError, ValueError):
            continue
        checked = _validate_runtime_receipt(
            receipt, runtime_id, platform, architecture)
        if not checked.get("ok"):
            continue
        python_rel = checked["python"]
        probe_python_rel = checked["probe_python"]
        interpreter = os.path.join(target, *python_rel.split("/"))
        if not os.path.isfile(interpreter):
            continue
        found.append({
            "path": interpreter,
            "build": _version_key(receipt.get("python_version")) or (),
            "windowless": True,
            "managed": True,
            "runtime_id": runtime_id,
            "probe_python": os.path.join(
                target, *probe_python_rel.split("/")),
            "cryptography_version": receipt.get("cryptography_version"),
            "receipt": receipt,
        })
    return found


def find_interpreters(platform=None, roots=None, data_dir=None, env=None,
                      home=None, architecture=None):
    """Installed, complete Convoy managed runtimes on this machine.

    The historical function name remains because ConvoyExt calls it, but
    TouchDesigner, system Python and project .venv interpreters are no longer
    candidates. `roots` is an injectable list of runtime directories for
    tests; normally the single per-user <data>/runtime directory is scanned.
    The expensive crypto capability probe runs later on the worker thread.
    """
    platform = platform or sys.platform
    architecture = normalize_architecture(architecture)
    if not runtime_target_supported(platform, architecture):
        return []
    if roots is None:
        data_dir = data_dir or default_data_dir(platform, env, home)
        roots = [runtime_dir(data_dir, platform=platform)]
    found = []
    for root in roots:
        found.extend(_runtime_candidates_from_root(root, platform,
                                                   architecture))
    found.sort(key=lambda c: (c.get("build") or (), c.get("runtime_id") or ""),
               reverse=True)
    return found


def choose_interpreter(candidates, prefer_windowless=True):
    """Pick the newest complete MANAGED runtime, never another Python."""
    usable = [c for c in (candidates or [])
              if c.get("path") and c.get("managed") is True]
    if not usable:
        return None
    if prefer_windowless:
        windowless = [c for c in usable if c.get("windowless")]
        if windowless:
            usable = windowless
    usable.sort(key=lambda c: (c.get("build") or (),
                               c.get("runtime_id") or ""), reverse=True)
    return usable[0]["path"]


def _actual_inside(directory, path):
    try:
        directory = os.path.normcase(os.path.realpath(directory))
        path = os.path.normcase(os.path.realpath(path))
        return os.path.commonpath([directory, path]) == directory
    except (OSError, ValueError):
        return False


def _probe_paths_inside_runtime(probe, target):
    """Every dependency-bearing Python path must live in the bundle."""
    for field in ("executable", "prefix", "base_prefix", "stdlib",
                  "platstdlib", "cryptography_file"):
        path = (probe or {}).get(field)
        if not path or not _actual_inside(target, path):
            return False, field
    return True, ""


def verify_managed_runtime(data_dir, interpreter, platform=None,
                           architecture=None, runner=None):
    """Prove an interpreter is one of our complete, live crypto runtimes."""
    platform = platform or sys.platform
    architecture = normalize_architecture(architecture)
    if not runtime_target_supported(platform, architecture):
        return _failed(
            "runtime_target_unsupported",
            "Convoy currently supports Windows x64 and Apple Silicon; "
            "this machine reports %s/%s" % (platform, architecture))
    base = _runtime_fs_dir(data_dir)
    match = None
    for candidate in _runtime_candidates_from_root(base, platform,
                                                    architecture):
        try:
            same = (os.path.normcase(os.path.realpath(candidate["path"]))
                    == os.path.normcase(os.path.realpath(str(interpreter))))
        except OSError:
            same = False
        if same:
            match = candidate
            break
    if match is None:
        return _failed(
            "runtime_not_managed",
            "Convoy refuses TouchDesigner Python, system Python, and project "
            ".venv interpreters. Install the signed offline Convoy Runtime "
            "bundle for Windows x64 or Apple Silicon, then retry.")
    receipt = match["receipt"]
    checked = _validate_runtime_receipt(
        receipt, match["runtime_id"], platform, architecture)
    if not checked.get("ok"):
        return checked
    files = checked["file_index"]
    target = _runtime_fs_dir(data_dir, match["runtime_id"])
    # Hash the complete installed inventory, not just python.exe. Native
    # cryptography libraries and stdlib modules are equally executable code.
    # This runs only during Install/Repair, not on every status refresh.
    for relative, record in files.items():
        path = os.path.join(target, *relative.split("/"))
        if (not _actual_inside(target, path) or os.path.islink(path)
                or not os.path.isfile(path)):
            return _failed(
                "runtime_integrity_failed",
                "managed runtime file is absent or unsafe: %s" % relative)
        try:
            if (os.path.getsize(path) != record["size"]
                    or _sha256_file(path) != record["sha256"]):
                return _failed(
                    "runtime_integrity_failed",
                    "managed runtime file changed after installation: %s"
                    % relative)
        except OSError as e:
            return _failed("runtime_integrity_failed",
                           "%s: %s" % (relative, e))
    live = probe_runtime(match["probe_python"], platform, architecture,
                         runner)
    if not live.get("ok"):
        return live
    probe = live.get("probe") or {}
    live_python = ".".join(str(v) for v in (probe.get("python") or []))
    if (live_python != str(receipt.get("python_version") or "")
            or probe.get("cryptography_version")
               != receipt.get("cryptography_version")):
        return _failed(
            "runtime_version_changed",
            "managed Python or cryptography changed after installation; "
            "reinstall the signed offline Convoy Runtime bundle")
    self_contained, outside_field = _probe_paths_inside_runtime(probe, target)
    if not self_contained:
        return _failed(
            "runtime_dependency_outside_bundle",
            "%s loaded outside the managed Convoy Runtime; reinstall the "
            "signed offline runtime bundle" % outside_field)
    return _ok(
        runtime_id=match["runtime_id"],
        platform=platform,
        architecture=architecture,
        python_version=receipt.get("python_version"),
        cryptography_version=probe.get("cryptography_version"),
        source_revision=receipt.get("source_revision"),
        archive_sha256=receipt.get("archive_sha256"),
        interpreter=match["path"],
        receipt_format=RUNTIME_RECEIPT_FORMAT,
        probe=live.get("probe"))


def _write_runtime_member(archive, info, destination, expected, root=None):
    """Extract one verified ordinary file through temp + replace."""
    parent = os.path.dirname(destination)
    root = root or parent
    if not _actual_inside(root, destination):
        raise ValueError("runtime member resolved outside the install root")
    os.makedirs(parent, exist_ok=True)
    # Recheck after directory creation: an existing junction/symlink in an
    # interrupted repair must not redirect the temporary file outside root.
    if not _actual_inside(root, destination):
        raise ValueError("runtime member parent redirects outside install root")
    temp = "%s.tmp-%s-%s" % (destination, os.getpid(), time.time_ns())
    digest = hashlib.sha256()
    size = 0
    try:
        with archive.open(info, "r") as source, open(temp, "wb") as target:
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > expected["size"]:
                    raise ValueError("runtime member exceeded declared size")
                digest.update(chunk)
                target.write(chunk)
            target.flush()
            os.fsync(target.fileno())
        if size != expected["size"] or digest.hexdigest() != expected["sha256"]:
            raise ValueError("runtime member digest or size mismatch")
        os.chmod(temp, expected.get("mode", 0o644))
        os.replace(temp, destination)
    finally:
        try:
            os.unlink(temp)
        except FileNotFoundError:
            pass


def _discard_runtime_stage(stage, files):
    """Remove only files named by a trusted manifest from our staging dir."""
    if not stage:
        return
    directories = {stage}
    for relative in sorted(files or {}):
        path = os.path.join(stage, *relative.split("/"))
        if not _actual_inside(stage, path):
            continue
        try:
            os.unlink(path)
        except OSError:
            pass
        parent = os.path.dirname(path)
        while parent and parent != stage:
            directories.add(parent)
            parent = os.path.dirname(parent)
    for name in (RUNTIME_MANIFEST_FILE, COMPLETE_FILE):
        try:
            os.unlink(os.path.join(stage, name))
        except OSError:
            pass
    for directory in sorted(directories,
                            key=lambda value: (value.count(os.sep), len(value)),
                            reverse=True):
        try:
            os.rmdir(directory)
        except OSError:
            pass


def provision_runtime_bundle(data_dir, bundle_path, expected_sha256,
                             platform=None, architecture=None, runner=None,
                             now=None, expected_runtime_id=None,
                             expected_release=None):
    """Install one release-pinned runtime archive, entirely offline.

    `expected_sha256` is mandatory trusted release metadata. The archive's
    own manifest is never allowed to bless itself. Files are hash-checked,
    symlinks and extra members are refused, each file lands atomically, a
    live isolated crypto probe must pass, and .complete is written LAST.
    No network operation exists in this path.
    """
    platform = platform or sys.platform
    architecture = normalize_architecture(architecture)
    expected_sha256 = _valid_sha256(expected_sha256)
    if expected_sha256 is None:
        return _failed(
            "runtime_digest_required",
            "a trusted release SHA-256 is required; Convoy will not install "
            "an unpinned runtime archive")
    if not runtime_target_supported(platform, architecture):
        return _failed(
            "runtime_target_unsupported",
            "Convoy currently supports managed runtimes for Windows x64 "
            "and Apple Silicon only")
    if expected_runtime_id is not None:
        try:
            expected_runtime_id = safe_version(expected_runtime_id)
        except ValueError as e:
            return _failed("runtime_catalog_invalid", e)
    if expected_release is not None and not isinstance(expected_release, dict):
        return _failed("runtime_catalog_invalid",
                       "expected runtime release metadata must be an object")
    bundle_stream = None
    try:
        if os.path.islink(bundle_path):
            return _failed("runtime_bundle_unreadable",
                           "runtime bundle must be an ordinary local file")
        bundle_stream = open(bundle_path, "rb")
        bundle_stat = os.fstat(bundle_stream.fileno())
        if not stat.S_ISREG(bundle_stat.st_mode):
            bundle_stream.close()
            return _failed("runtime_bundle_unreadable",
                           "runtime bundle must be an ordinary local file")
        if bundle_stat.st_size <= 0 or bundle_stat.st_size > RUNTIME_MAX_ARCHIVE_BYTES:
            bundle_stream.close()
            return _failed("runtime_bundle_invalid",
                           "runtime archive size is outside the release bound")
        archive_digest = hashlib.sha256()
        while True:
            chunk = bundle_stream.read(1024 * 1024)
            if not chunk:
                break
            archive_digest.update(chunk)
        actual_sha256 = archive_digest.hexdigest()
    except OSError as e:
        if bundle_stream is not None:
            bundle_stream.close()
        return _failed("runtime_bundle_unreadable", e)
    if actual_sha256 != expected_sha256:
        bundle_stream.close()
        return _failed(
            "runtime_bundle_digest_mismatch",
            "runtime archive does not match trusted release metadata",
            expected_sha256=expected_sha256, actual_sha256=actual_sha256)
    stage = None
    staged_files = {}
    try:
        bundle_stream.seek(0)
        with bundle_stream, zipfile.ZipFile(bundle_stream, "r") as archive:
            infos = archive.infolist()
            if len(infos) > RUNTIME_MAX_FILES + 1:
                return _failed("runtime_bundle_invalid",
                               "runtime archive contains too many members")
            names = [info.filename for info in infos]
            if len(names) != len(set(names)):
                return _failed("runtime_bundle_invalid",
                               "runtime archive contains duplicate members")
            manifest_infos = [info for info in infos
                              if info.filename == RUNTIME_MANIFEST_FILE]
            if len(manifest_infos) != 1:
                return _failed(
                    "runtime_bundle_invalid",
                    "runtime archive must contain one convoy-runtime.json")
            manifest_mode = (manifest_infos[0].external_attr >> 16) & 0xFFFF
            if manifest_infos[0].is_dir() or stat.S_ISLNK(manifest_mode):
                return _failed("runtime_bundle_invalid",
                               "runtime manifest must be an ordinary file")
            if manifest_infos[0].file_size > RUNTIME_MANIFEST_MAX_BYTES:
                return _failed("runtime_bundle_invalid",
                               "runtime manifest is unexpectedly large")
            raw_manifest = archive.read(manifest_infos[0])
            manifest = json.loads(raw_manifest.decode("utf-8"))
            checked = _validate_runtime_manifest(manifest, platform,
                                                 architecture)
            if not checked.get("ok"):
                return checked
            manifest = checked["manifest"]
            files = checked["file_index"]
            if (expected_runtime_id is not None
                    and manifest["runtime_id"] != expected_runtime_id):
                return _failed(
                    "runtime_catalog_mismatch",
                    "runtime archive ID does not match trusted catalog metadata",
                    expected_runtime_id=expected_runtime_id,
                    actual_runtime_id=manifest["runtime_id"])
            if expected_release is not None:
                release_fields = {
                    "runtime_id": manifest["runtime_id"],
                    "platform": manifest["platform"],
                    "architecture": manifest["architecture"],
                    "python_version": manifest["python_version"],
                    "cryptography_version": manifest["cryptography_version"],
                    "source_revision": manifest["source_revision"],
                }
                mismatched = [name for name, value in release_fields.items()
                              if expected_release.get(name) != value]
                if mismatched:
                    return _failed(
                        "runtime_catalog_mismatch",
                        "runtime archive disagrees with trusted catalog fields: "
                        + ", ".join(sorted(mismatched)))
            expected_names = set(files) | {RUNTIME_MANIFEST_FILE}
            if set(names) != expected_names:
                return _failed(
                    "runtime_bundle_invalid",
                    "runtime archive members do not exactly match its manifest")
            info_by_name = {info.filename: info for info in infos}
            total = 0
            for name, record in files.items():
                info = info_by_name[name]
                mode = (info.external_attr >> 16) & 0xFFFF
                if stat.S_ISLNK(mode):
                    return _failed("runtime_bundle_invalid",
                                   "runtime archives may not contain symlinks")
                if info.is_dir() or info.file_size != record["size"]:
                    return _failed(
                        "runtime_bundle_invalid",
                        "runtime archive size disagrees for %s" % name)
                total += info.file_size
                if total > RUNTIME_MAX_UNCOMPRESSED_BYTES:
                    return _failed(
                        "runtime_bundle_invalid",
                        "runtime archive exceeds the uncompressed size limit")

            runtime_id = manifest["runtime_id"]
            target = _runtime_fs_dir(data_dir, runtime_id)
            runtime_root = _runtime_fs_dir(data_dir)
            if (os.path.lexists(runtime_root)
                    and (os.path.islink(runtime_root)
                         or not _actual_inside(install_root(data_dir),
                                               runtime_root))):
                return _failed(
                    "runtime_path_unsafe",
                    "managed runtime root redirects outside Convoy data")
            if (os.path.lexists(target)
                    and (os.path.islink(target)
                         or not _actual_inside(runtime_root, target))):
                return _failed(
                    "runtime_path_unsafe",
                    "managed runtime directory redirects outside Convoy data")
            existing = read_runtime_receipt(data_dir, runtime_id, platform)
            if existing is not None:
                receipt_check = _validate_runtime_receipt(
                    existing, runtime_id, platform, architecture)
                if not receipt_check.get("ok"):
                    return receipt_check
                if receipt_check["archive_sha256"] != expected_sha256:
                    return _failed(
                        "runtime_id_collision",
                        "an installed runtime with this ID has different "
                        "release bytes; use a new content-versioned runtime ID")
                interpreter = os.path.join(
                    _runtime_fs_dir(data_dir, runtime_id),
                    *manifest["python"].split("/"))
                verified = verify_managed_runtime(
                    data_dir, interpreter, platform, architecture, runner)
                if verified.get("ok"):
                    verified.update({"current": True,
                                     "archive_sha256": expected_sha256})
                    return verified
                # Same trusted archive, but the installed runtime no longer
                # verifies. Remove ONLY the completion receipt before repair
                # so a concurrent supervisor fails closed while files are
                # replaced atomically. A different archive under the same ID
                # was refused above and never reaches this branch.
                try:
                    os.unlink(os.path.join(
                        _runtime_fs_dir(data_dir, runtime_id), COMPLETE_FILE))
                except OSError as e:
                    return _failed(
                        "runtime_repair_blocked",
                        "could not mark the damaged runtime incomplete: %s"
                        % e)
            elif os.path.lexists(target):
                # A prior repair can legitimately have removed .complete and
                # then lost power. Resume only when its on-disk manifest is
                # byte-semantically the SAME trusted release manifest. Unknown
                # files remain untouched; an absent/different manifest makes
                # ownership unverifiable and is refused.
                try:
                    with open(os.path.join(target, RUNTIME_MANIFEST_FILE),
                              "r", encoding="utf-8") as stream:
                        interrupted_manifest = json.load(stream)
                    interrupted_check = _validate_runtime_manifest(
                        interrupted_manifest, platform, architecture)
                    resumable = (interrupted_check.get("ok")
                                 and interrupted_check["manifest"] == manifest)
                except (OSError, ValueError, UnicodeError):
                    resumable = False
                if not resumable:
                    return _failed(
                        "runtime_incomplete_exists",
                        "an incomplete or unrecognized runtime directory "
                        "already uses this ID; Convoy will not overwrite "
                        "unknown files")
                existing = interrupted_manifest

            if existing is None:
                os.makedirs(runtime_root, exist_ok=True)
                stage = os.path.join(
                    runtime_root, ".install-%s-%s-%s"
                    % (runtime_id, os.getpid(), time.time_ns()))
                os.mkdir(stage)
                install_target = stage
            else:
                # A valid receipt with the SAME release hash proved ownership
                # above. Repair can safely replace its inventoried files; the
                # removed .complete keeps it undiscoverable until verification.
                install_target = target
            staged_files = files
            for name in sorted(files):
                destination = os.path.join(install_target, *name.split("/"))
                _write_runtime_member(archive, info_by_name[name],
                                      destination, files[name], install_target)
            _atomic_write(
                os.path.join(install_target, RUNTIME_MANIFEST_FILE),
                json.dumps(manifest, indent=1, sort_keys=True) + "\n")
    except (OSError, ValueError, UnicodeError, zipfile.BadZipFile,
            RuntimeError, NotImplementedError) as e:
        if bundle_stream is not None and not bundle_stream.closed:
            bundle_stream.close()
        _discard_runtime_stage(stage, staged_files)
        return _failed("runtime_bundle_invalid",
                       "%s: %s" % (type(e).__name__, e))

    interpreter = os.path.join(
        install_target,
        *manifest["python"].split("/"))
    probe_interpreter = os.path.join(
        install_target,
        *manifest["probe_python"].split("/"))
    live = probe_runtime(probe_interpreter, platform, architecture, runner)
    if not live.get("ok"):
        _discard_runtime_stage(stage, staged_files)
        # No .complete: launcher and discovery refuse an in-place repair; a
        # fresh install never appears at its final path at all.
        return live
    probe = live.get("probe") or {}
    live_python = ".".join(str(v) for v in (probe.get("python") or []))
    if (live_python != manifest["python_version"]
            or probe.get("cryptography_version")
               != manifest["cryptography_version"]):
        _discard_runtime_stage(stage, staged_files)
        return _failed(
            "runtime_manifest_probe_mismatch",
            "runtime capability versions do not match convoy-runtime.json")
    self_contained, outside_field = _probe_paths_inside_runtime(
        probe, install_target)
    if not self_contained:
        _discard_runtime_stage(stage, staged_files)
        return _failed(
            "runtime_dependency_outside_bundle",
            "%s loaded outside the extracted Convoy Runtime"
            % outside_field)
    receipt = dict(manifest)
    receipt.update({
        "format": RUNTIME_RECEIPT_FORMAT,
        "archive_sha256": expected_sha256,
        "installed_at": (now or time.time)(),
        "cryptography_version": probe.get("cryptography_version"),
    })
    try:
        _atomic_write(os.path.join(install_target, COMPLETE_FILE),
            json.dumps(receipt, indent=1, sort_keys=True) + "\n")
        if stage is not None:
            os.replace(stage, target)
            stage = None
    except OSError as e:
        _discard_runtime_stage(stage, staged_files)
        # Another Embody process may have atomically activated the identical
        # content-addressed runtime between our absence check and rename. That
        # is convergence, not failure, but only after full receipt/hash/probe
        # verification against the same trusted archive digest.
        winner = read_runtime_receipt(
            data_dir, manifest["runtime_id"], platform)
        if (isinstance(winner, dict)
                and _valid_sha256(winner.get("archive_sha256"))
                   == expected_sha256):
            winner_interpreter = os.path.join(
                target, *manifest["python"].split("/"))
            verified = verify_managed_runtime(
                data_dir, winner_interpreter, platform, architecture, runner)
            if verified.get("ok"):
                verified.update({"current": True,
                                 "archive_sha256": expected_sha256})
                return verified
        return _failed("runtime_activation_failed",
                       "could not atomically activate runtime: %s" % e)
    interpreter = os.path.join(target, *manifest["python"].split("/"))
    return _ok(
        runtime_id=manifest["runtime_id"], interpreter=interpreter,
        platform=platform, architecture=architecture,
        python_version=manifest["python_version"],
        cryptography_version=receipt["cryptography_version"],
        source_revision=manifest["source_revision"],
        archive_sha256=expected_sha256, current=False,
        receipt=receipt, probe=live.get("probe"))


def _runtime_installations(data_dir):
    """(complete runtimes, incomplete paths, stray paths) on THIS disk."""
    root = _runtime_fs_dir(data_dir)
    complete, incomplete, stray = [], [], []
    if (os.path.lexists(root)
            and (os.path.islink(root)
                 or not _actual_inside(install_root(data_dir), root))):
        return complete, incomplete, [root]
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return complete, incomplete, stray
    for name in names:
        target = os.path.join(root, name)
        if not os.path.isdir(target):
            stray.append(target)
            continue
        if os.path.islink(target) or not _actual_inside(root, target):
            stray.append(target)
            continue
        try:
            runtime_id = safe_version(name)
        except ValueError:
            stray.append(target)
            continue
        receipt = read_runtime_receipt(data_dir, runtime_id)
        files, _ = _runtime_file_index(receipt)
        if (receipt is None
                or receipt.get("format") != RUNTIME_RECEIPT_FORMAT
                or receipt.get("runtime_id") != runtime_id
                or files is None):
            incomplete.append(target)
            continue
        complete.append((runtime_id, target, receipt, files))
    return complete, incomplete, stray


def plan_runtime_uninstall(data_dir):
    """Exact managed-runtime files/dirs removable without recursion."""
    remove, remove_dirs = [], []
    complete, incomplete, stray = _runtime_installations(data_dir)
    for runtime_id, target, receipt, files in complete:
        directories = {target}
        for relative in sorted(files):
            path = os.path.join(target, *relative.split("/"))
            if _actual_inside(target, path):
                remove.append(path)
                parent = os.path.dirname(path)
                while parent and parent != target:
                    directories.add(parent)
                    parent = os.path.dirname(parent)
        remove.extend((os.path.join(target, RUNTIME_MANIFEST_FILE),
                       os.path.join(target, COMPLETE_FILE)))
        remove_dirs.extend(sorted(directories,
                                  key=lambda p: (p.count(os.sep), len(p)),
                                  reverse=True))
    remove_dirs.append(_runtime_fs_dir(data_dir))
    return {"remove": remove, "remove_dirs": remove_dirs,
            "incomplete": incomplete, "stray": stray,
            "runtime_ids": [row[0] for row in complete]}


def remove_managed_runtime(data_dir, runtime_id):
    """Remove exactly one receipt-listed runtime, never a whole tree."""
    removed, kept, remaining = [], [], []
    try:
        runtime_id = safe_version(runtime_id)
    except ValueError as e:
        return {"removed": removed, "kept": [str(e)],
                "remaining": remaining, "removed_dir": False}
    target = _runtime_fs_dir(data_dir, runtime_id)
    root = _runtime_fs_dir(data_dir)
    if (os.path.islink(root) or not _actual_inside(install_root(data_dir), root)
            or os.path.islink(target) or not _actual_inside(root, target)):
        return {"removed": removed, "kept": [target],
                "remaining": [target] if os.path.lexists(target) else [],
                "removed_dir": False}
    receipt = read_runtime_receipt(data_dir, runtime_id)
    files, detail = _runtime_file_index(receipt)
    if (receipt is None
            or receipt.get("format") != RUNTIME_RECEIPT_FORMAT
            or receipt.get("runtime_id") != runtime_id
            or files is None):
        kept.append(target)
        if detail:
            kept.append(detail)
        return {"removed": removed, "kept": kept,
                "remaining": [target] if os.path.exists(target) else [],
                "removed_dir": False}

    directories = {target}
    for relative in sorted(files):
        path = os.path.join(target, *relative.split("/"))
        if not _actual_inside(target, path):
            kept.append(path)
            continue
        parent = os.path.dirname(path)
        while parent and parent != target:
            directories.add(parent)
            parent = os.path.dirname(parent)
        try:
            os.unlink(path)
            removed.append(path)
        except FileNotFoundError:
            pass
        except OSError:
            kept.append(path)
    for path in (os.path.join(target, RUNTIME_MANIFEST_FILE),
                 os.path.join(target, COMPLETE_FILE)):
        try:
            os.unlink(path)
            removed.append(path)
        except FileNotFoundError:
            pass
        except OSError:
            kept.append(path)
    for directory in sorted(directories,
                            key=lambda p: (p.count(os.sep), len(p)),
                            reverse=True):
        try:
            os.rmdir(directory)
        except OSError:
            pass
    if os.path.isdir(target):
        try:
            remaining.extend(os.path.join(target, name)
                             for name in sorted(os.listdir(target)))
        except OSError:
            remaining.append(target)
    return {"removed": removed, "kept": kept, "remaining": remaining,
            "removed_dir": not os.path.exists(target)}


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

    SIX THINGS IT MUST DO, in this order:

    1. OPEN THE LOG AND REBIND sys.stdout/sys.stderr/sys.stdin BEFORE
       importing or calling convoy_hostapp. A background supervisor may
       provide no standard streams, and convoy_hostapp.main() calls
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
    4. CAP THE LOG FOR THE PROCESS LIFETIME. stdout/stderr share one locked
       writer that truncates-and-restarts before a write crosses
       LOG_MAX_BYTES, including in one healthy months-long daemon.
    5. REFUSE A VERSION THAT IS NOT A PLAIN VERSION -- the same accept-
       list safe_version applies installer-side, re-applied here because
       this is where a version read back off disk becomes a path.
    6. REFUSE A LEGACY/FOREIGN INTERPRETER OR MISSING CRYPTOGRAPHY. The
       supervisor must be running the exact managed interpreter recorded
       by install(), and cryptography must load from inside that runtime.

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
        "runtime_subdir": repr(RUNTIME_SUBDIR),
        "runtime_receipt_format": repr(RUNTIME_RECEIPT_FORMAT),
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

import hashlib
import json
import os
import re
import sys
import threading
import time

sys.dont_write_bytecode = True

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
_SHA256_OK = re.compile(r"^[0-9a-f]{64}$")


def _safe_segment(value):
    value = str(value or "")
    return value not in ("", ".", "..") and bool(_VERSION_OK.fullmatch(value))


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


class _BoundedLog:
    """Text writer whose on-disk byte count stays under LOG_MAX_BYTES."""

    encoding = "utf-8"
    errors = "replace"

    def __init__(self, path):
        self.path = path
        self._lock = threading.RLock()
        mode = "a"
        try:
            if os.path.getsize(path) > LOG_MAX_BYTES:
                mode = "w"
        except OSError:
            pass
        self._stream = self._open(mode)
        try:
            self._bytes = os.path.getsize(path)
        except OSError:
            self._bytes = 0
        if mode == "w":
            self._restart_marker()

    def _open(self, mode):
        return open(self.path, mode, buffering=1, encoding="utf-8",
                    errors="replace", newline="\\n")

    def _restart_marker(self):
        marker = "--- log restarted (over %%d bytes) ---\\n" %% LOG_MAX_BYTES
        self._stream.write(marker)
        self._stream.flush()
        self._bytes = len(marker.encode("utf-8"))

    def write(self, value):
        if not isinstance(value, str):
            value = str(value)
        encoded = value.encode("utf-8", "replace")
        with self._lock:
            if self._bytes + len(encoded) > LOG_MAX_BYTES:
                try:
                    self._stream.close()
                finally:
                    self._stream = self._open("w")
                self._bytes = 0
                self._restart_marker()
            available = max(0, LOG_MAX_BYTES - self._bytes)
            if len(encoded) > available:
                encoded = encoded[:available]
                value = encoded.decode("utf-8", "ignore")
                encoded = value.encode("utf-8")
            written = self._stream.write(value)
            self._bytes += len(encoded)
            return written

    def flush(self):
        with self._lock:
            self._stream.flush()

    def close(self):
        with self._lock:
            self._stream.close()

    @property
    def closed(self):
        return self._stream.closed

    def fileno(self):
        return self._stream.fileno()

    def isatty(self):
        return False

    def writable(self):
        return True


def _open_log():
    """Open logs/host.log and REBIND the std streams onto it.

    THIS MUST HAPPEN BEFORE convoy_hostapp IS IMPORTED OR CALLED. Under a
    background supervisor sys.stdout/sys.stderr may be None, and the daemon
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
    try:
        log = _BoundedLog(path)
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
    if not _safe_segment(version):
        # Refuse rather than join it into a path: see _VERSION_OK.
        _say("refusing an unusable version %%r from installed.json -- it "
             "must be a plain version like 6.0.171" %% (version,))
        return 1
    runtime = record.get("runtime")
    # TWO RUNTIME SHAPES, both fail-closed.
    #
    # 1. MANAGED (a signed offline runtime bundle): every check below --
    #    receipt, archive hash, interpreter hash, crypto-inside-the-bundle.
    #    This is the release shape and nothing about it is relaxed.
    #
    # 2. VENV: the host app is plain Python (exactly like the Envoy bridge)
    #    running under Embody's own uv-managed .venv interpreter, which
    #    already carries the crypto floor. There is no bundle to hash, so the
    #    proof is different but still real: the interpreter RECORDED at
    #    install must be the one actually executing this file (so a rewritten
    #    installed.json cannot redirect the login task at another Python),
    #    and cryptography + TLS 1.3 must genuinely import. The payload's own
    #    .complete check below is unchanged.
    venv_runtime = bool(record.get("venv_runtime")) and not isinstance(
        runtime, dict)
    if venv_runtime:
        configured = str(record.get("interpreter") or "")
        try:
            same_interpreter = (
                os.path.normcase(os.path.realpath(configured))
                == os.path.normcase(os.path.realpath(sys.executable)))
        except (OSError, ValueError):
            same_interpreter = False
        if not configured or not same_interpreter:
            _say("supervisor launched an interpreter other than the one "
                 "recorded at install -- refusing to start; use Install or "
                 "Update")
            return 1
    elif (not isinstance(runtime, dict)
            or runtime.get("format") != %(runtime_receipt_format)s):
        _say("installed host has no verified managed-runtime receipt -- "
             "use Install or Update to install the signed offline Convoy "
             "Runtime; TouchDesigner and project Python are refused")
        return 1
    runtime_id = "" if venv_runtime else str(runtime.get("runtime_id") or "")
    if not venv_runtime and not _safe_segment(runtime_id):
        _say("installed host names an unusable managed runtime -- use "
             "Install or Update to repair it")
        return 1
    runtime_root = ("" if venv_runtime
                    else os.path.join(DATA_DIR, %(runtime_subdir)s,
                                      runtime_id))
    try:
        with open(os.path.join(runtime_root, %(complete_file)s), "r",
                  encoding="utf-8") as f:
            runtime_receipt = json.load(f)
    except (OSError, ValueError):
        runtime_receipt = None
    archive_sha256 = ("" if venv_runtime
                      else str(runtime.get("archive_sha256") or "").lower())
    if not venv_runtime and (not isinstance(runtime_receipt, dict)
            or runtime_receipt.get("format") != %(runtime_receipt_format)s
            or runtime_receipt.get("runtime_id") != runtime_id
            or not _SHA256_OK.fullmatch(archive_sha256)
            or runtime_receipt.get("archive_sha256") != archive_sha256):
        _say("managed Convoy Runtime receipt is absent or changed -- use "
             "Install or Update to repair the offline runtime bundle")
        return 1
    python_rel = ("" if venv_runtime
                  else str(runtime_receipt.get("python") or ""))
    python_parts = python_rel.split("/")
    if not venv_runtime and (not python_parts or "\\\\" in python_rel
            or any(not _safe_segment(part) for part in python_parts)):
        _say("managed Convoy Runtime receipt has an unsafe Python path -- "
             "use Install or Update to repair it")
        return 1
    if not venv_runtime:
        receipt_interpreter = os.path.join(runtime_root, *python_parts)
        configured = str(record.get("interpreter") or "")
        try:
            same_interpreter = (os.path.normcase(os.path.realpath(configured))
                                == os.path.normcase(
                                    os.path.realpath(sys.executable))
                                == os.path.normcase(
                                    os.path.realpath(receipt_interpreter)))
        except (OSError, ValueError):
            same_interpreter = False
        if not configured or not same_interpreter:
            _say("supervisor launched an interpreter other than the verified "
                 "Convoy Runtime -- refusing to start; use Install or Update")
            return 1
    files = None if venv_runtime else runtime_receipt.get("files")
    python_records = ([item for item in files
                       if isinstance(item, dict)
                       and item.get("path") == python_rel]
                      if isinstance(files, list) else [])
    try:
        python_record = python_records[0] if len(python_records) == 1 else None
        expected_python_sha = str(
            (python_record or {}).get("sha256") or "").lower()
        expected_python_size = (python_record or {}).get("size")
        python_intact = (
            python_record is not None
            and _SHA256_OK.fullmatch(expected_python_sha)
            and isinstance(expected_python_size, int)
            and not isinstance(expected_python_size, bool)
            and os.path.getsize(receipt_interpreter) == expected_python_size
            and _sha256(receipt_interpreter) == expected_python_sha)
    except OSError:
        python_intact = False
    if not venv_runtime and not python_intact:
        _say("managed Convoy Runtime interpreter changed after installation "
             "-- refusing to start; use Install or Update")
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
    try:
        import ssl
        import cryptography
        import convoy_hostkeys
    except Exception as exc:
        _say("Convoy runtime cannot import cryptography: %%s -- use Install "
             "or Update to repair it" %% (exc,))
        return 1
    if venv_runtime:
        # No bundle to be "inside", so prove the capability itself: real
        # cryptography and real TLS 1.3, or refuse to start.
        if (not convoy_hostkeys.cryptography_available()
                or not getattr(ssl, "HAS_TLSv1_3", False)):
            _say("Convoy runtime failed its cryptography/TLS check -- the "
                 "venv is missing cryptography or TLS 1.3")
            return 1
    else:
        crypto_file = getattr(cryptography, "__file__", "")
        try:
            crypto_inside = (os.path.commonpath([
                os.path.normcase(os.path.realpath(runtime_root)),
                os.path.normcase(os.path.realpath(crypto_file)),
            ]) == os.path.normcase(os.path.realpath(runtime_root)))
        except (OSError, ValueError):
            crypto_inside = False
        if (not convoy_hostkeys.cryptography_available() or not crypto_inside
                or not getattr(ssl, "HAS_TLSv1_3", False)):
            _say("managed Convoy Runtime failed its cryptography/TLS "
                 "integrity check -- use Install or Update to repair it")
            return 1
        expected_crypto = str(runtime.get("cryptography_version") or "")
        if (not expected_crypto
                or runtime_receipt.get("cryptography_version") != expected_crypto
                or getattr(cryptography, "__version__", "") != expected_crypto):
            _say("managed Convoy Runtime cryptography version changed after "
                 "installation -- refusing to start; use Install or Update")
            return 1
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
        account name (COMPUTER\\user). So "the stored trigger UserId
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
        "within a minute if it stops. Per-user and never elevated.")
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
    runtime_plan = plan_runtime_uninstall(data_dir)
    remove.extend(runtime_plan["remove"])
    remove_dirs.extend(runtime_plan["remove_dirs"])
    incomplete.extend(runtime_plan["incomplete"])
    stray.extend(runtime_plan["stray"])
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
            "runtime_ids": runtime_plan["runtime_ids"],
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
            supervisor=None, now=None, user=None, env=None, uid=None,
            runtime_verifier=None, runtime_runner=None, architecture=None,
            runtime_catalog=None, runtime_asset_root=None):
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
    if not interpreter and runtime_catalog is not None:
        try:
            provisioned = provision_runtime_from_catalog(
                data_dir, runtime_catalog, asset_root=runtime_asset_root,
                platform=platform, architecture=architecture,
                runner=runtime_runner, now=now)
        except Exception as e:
            return _failed("runtime_provision_failed",
                           "%s: %s" % (type(e).__name__, e))
        if not provisioned.get("ok"):
            return _failed(
                provisioned.get("reason") or "runtime_provision_failed",
                provisioned.get("detail") or
                "the signed offline Convoy Runtime could not be installed",
                runtime=provisioned)
        interpreter = provisioned.get("interpreter")
    if not interpreter:
        return _failed(
            "no_managed_runtime",
            "no complete Convoy Runtime is installed for this user; install "
            "the signed offline runtime bundle for Windows x64 or Apple "
            "Silicon, then retry")

    # FAIL CLOSED BEFORE WRITING A PAYLOAD OR SUPERVISOR. A Python path is
    # not proof of an approved runtime: project venvs disappear, TD Python
    # lacks cryptography, and a PATH interpreter is mutable outside Convoy's
    # lifecycle. The default verifier requires our .complete receipt, checks
    # the interpreter hash and runs a live isolated crypto probe. Tests inject
    # a verifier because cross-platform CI must never execute target binaries.
    verifier = runtime_verifier or verify_managed_runtime
    try:
        runtime_check = verifier(
            data_dir, interpreter, platform=platform,
            architecture=normalize_architecture(architecture),
            runner=runtime_runner)
    except Exception as e:
        return _failed("runtime_verification_failed",
                       "%s: %s" % (type(e).__name__, e))
    if not isinstance(runtime_check, dict) or not runtime_check.get("ok"):
        runtime_check = (runtime_check if isinstance(runtime_check, dict)
                         else {"reason": "runtime_verification_failed",
                               "detail": "runtime verifier returned no result"})
        return _failed(
            runtime_check.get("reason") or "runtime_verification_failed",
            runtime_check.get("detail") or
            "the managed Convoy Runtime did not pass verification",
            runtime=runtime_check)

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
        # A managed runtime carries a receipt the launcher re-verifies (hash
        # of the bundle, of its interpreter, crypto loaded from inside it).
        # The venv runtime has no bundle, so it records no receipt at all and
        # is marked explicitly -- the launcher then proves the recorded
        # interpreter is the one executing plus a live crypto/TLS check. The
        # two shapes are mutually exclusive by construction: a record can
        # never claim managed verification it did not get.
        if runtime_check.get("runtime_id"):
            record["runtime"] = {
                "format": runtime_check.get("receipt_format",
                                            RUNTIME_RECEIPT_FORMAT),
                "runtime_id": runtime_check.get("runtime_id"),
                "platform": runtime_check.get("platform", platform),
                "architecture": runtime_check.get(
                    "architecture", normalize_architecture(architecture)),
                "python_version": runtime_check.get("python_version"),
                "cryptography_version": runtime_check.get(
                    "cryptography_version"),
                "source_revision": runtime_check.get("source_revision"),
                "archive_sha256": runtime_check.get("archive_sha256"),
            }
        else:
            record["venv_runtime"] = True
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

        runtimes, runtime_incomplete, runtime_stray = _runtime_installations(
            data_dir)
        for runtime_id, target, receipt, runtime_files in runtimes:
            outcome = remove_managed_runtime(data_dir, runtime_id)
            removed.extend(outcome["removed"])
            kept.extend(outcome["kept"])
            remaining.extend(outcome["remaining"])
        # No .complete receipt means we cannot prove the directory's files
        # are ours. Leave it and say so, exactly like an interrupted app
        # payload; never turn uninstall into an unbounded recursive delete.
        remaining.extend(runtime_incomplete)
        remaining.extend(runtime_stray)

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
                          bin_dir(data_dir, platform),
                          _runtime_fs_dir(data_dir)):
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
