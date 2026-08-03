"""
ConvoyExt -- this TouchDesigner session's Convoy node registration.

Hosted on the 'convoy' baseCOMP inside the Embody COMP, for the same
reason UpdaterExt lives on 'updater': extension reinit is per-COMP, and
every extension DAT on the Embody COMP hot-reloads ALL of that COMP's
extensions when its file changes. Convoy code inside EnvoyExt.py would
restart the MCP server on every Convoy iteration. A child COMP keeps the
blast radius here. The cost, accepted: there is no op.Embody.ext.Convoy;
call sites use op.Embody.op('convoy').ext.ConvoyExt (updater precedent).

The host is reached CONTEXT-FREE (self.ownerComp.parent.Embody), never
the bare `parent.Embody` global, because the string-form run() callbacks
below can resolve with ROOT as their execution context.

WHAT THIS DOES

One idempotent main-thread reconciler (_convoyTick), armed once per
extension instance from __init__ and generation-guarded through
ownerComp.store('_convoy_gen', ...) -- copied from EnvoyExt._watchdogTick,
whose lesson is that a save's strip/restore arms one tick per reinit and
they all come due in the same frame. Only the newest generation survives.

Every tick computes a desired-state tuple ON THE MAIN THREAD

    (enabled, project_root, comp_path, convoy_id, envoy_port, host_id)

and compares it with the last tuple actually sent. Unchanged, and inside
the heartbeat window -> no network call at all. Changed, or the window
elapsed -> one bounded worker. That single mechanism covers
register-on-Envoy-start, register-on-project-open, re-register-on-restart,
host-app-started-late and host-app-restarted.

The ~30 s heartbeat is load-bearing, not cosmetic: envoy_port and
runtime_id are per-launch and are NOT persisted by the host app, so a host
restart reloads this node's record WITHOUT a port and dispatch dies
silently. The heartbeat heals it.

ABSENCE IS NOT AN ERROR. No host app on this machine is the normal state
of almost every install: status reads 'No Convoy host app', ONE DEBUG line
on the transition, and the tick slows down. Never an Error, never a dialog.

THREADING (TDResources ThreadManager)

Resolve everything on the main thread -> daemon worker doing pure-Python
urllib with ZERO TD access -> publish a generation-tagged plain dict to a
plain attribute -> bounded main-thread run(delayFrames=15) poll chain with
a stale-instance guard. Ordinary work is submitted to one lazy, long-lived,
standalone TDTask owned by op.TDResources.ThreadManager. Multi-target batches
keep that worker as their coordinator and use a small bounded set of
short-lived ThreadManager TDTasks for parallel target I/O. Extension reinit
signals the previous generation's Event and queue sentinel; old work may
finish its current bounded call but can never accept work for the new
generation. Including, critically, the convoy_client MODULE ITSELF: a
`mod.` reference is a live DAT lookup, i.e. a TD access that re-resolves
on every attribute get, so binding the module inside the worker body would
be a threading violation. It is captured in a local by _beginCall BEFORE
the task is queued. See _client(), which holds the one and only such
reference in this file.

STATE THAT MUST OUTLIVE A REINIT BUT NOT THE PROCESS

runtime_id (the per-launch run identity) and node_id (needed to unregister)
live in a sys attribute keyed by COMP path -- see _session(). Both obvious
homes are wrong in opposite directions: an instance attribute is re-minted
on every Ctrl+S (which is not a new run, and re-minting runtime_id
invalidates every in-flight expected_runtime_id precondition host-side),
and ownerComp.store() is saved into the .toe (and, with Embedstorageintdns
on, into convoy.tdn) so it would outlive the process, which a restart must
not do. sys attributes are the established channel here
(sys._envoy_server_gen, sys._envoy_queues, sys._envoy_shutdown_events).
"""

import hmac
import json
import math
import os
import re
import secrets
import socket
import sys
import time
from collections import OrderedDict
from queue import Empty, Full, Queue
from threading import Event

# A payload entry is a BARE FILENAME and nothing else. The accept-list
# is convoy_install._BARE_NAME_OK's, deliberately duplicated at the READ
# site rather than trusted from the write site: these names come off DAT
# parameters a user can edit, and write_payload would reject them anyway
# -- catching it here means a mis-named DAT is reported as a missing
# module instead of failing the whole install.
_BARE_MODULE_NAME = re.compile(r"^[A-Za-z0-9._+-]+\.py$")


class ConvoyExt:
    """Node-side Convoy registration: reconcile, register, heartbeat."""

    # Consent scope recorded beside the convoy id (A-13).  Pre-LAN projects
    # carry 'local host app only'; the reconciler refuses to expose those
    # until an explicit local enable upgrades this marker.
    CONSENT_SCOPE = 'trusted LAN Convoy mesh'

    # Cadences, in seconds, for the NEXT call. The tick wakes just often
    # enough to serve whichever one is pending (see _scheduleFrom).
    CONVERGING_S = 4.0    # something is still settling (Envoy port pending)
    HEARTBEAT_S = 30.0    # steady state: re-assert port + runtime_id
    ABSENT_S = 60.0       # no host app, or a policy refusal: stay quiet

    # Tick bounds, in milliseconds.
    TICK_MIN_MS = 4000
    TICK_MAX_MS = 60000

    # Worker poll chain. Worst case in the worker is a 3 s /health plus a
    # 10 s /register; the budget is >= 3x that (160 x 15 frames ~= 40 s at
    # 60 fps), matching UpdaterExt's sizing rule.
    POLL_FRAMES = 15
    POLL_ATTEMPTS = 160

    # The HOST-APP poll chain is far longer than the registration one and
    # has to be: convoy_install.run_command allows 30 s per supervisor
    # spawn, install() may issue two and start() three, and the install
    # tail then waits up to HEALTH_WAIT_S for /health. Worst case is
    # ~170 s of legitimate work, so the cap is 800 x 15 frames (~200 s at
    # 60 fps). It is a BOUND on a wedged worker, not a timer -- the
    # worker's own subprocess timeouts are what actually end it.
    HOST_POLL_ATTEMPTS = 800

    # At most one node call and one host-lifecycle call can be outstanding.
    # The long-lived worker serializes them, so two slots are sufficient and
    # a programming error cannot grow an unbounded queue inside TD.
    WORKER_QUEUE_MAX = 2
    WORKER_IDLE_S = 0.25

    # TouchDesigner-originated sibling requests use the SAME long-lived
    # ThreadManager worker as registration and host lifecycle work; a batch
    # uses it as coordinator for a bounded short-lived fanout. The
    # request/result registry is deliberately small: Convoy deployments are
    # dozens of nodes, and retaining an unbounded history (especially image
    # results) inside a .toe would be an easy way to exhaust TD's process.
    # Progress and completion are separate queues so a chatty long-running
    # job can never crowd its own terminal result out of the handoff channel.
    API_REQUEST_MAX = 64
    API_COMPLETION_MAX = 64
    API_PROGRESS_MAX = 128
    API_PROGRESS_PER_REQUEST_MAX = 32
    API_EVENT_DRAIN_MAX = 128
    API_POLL_FRAMES = 4
    # The loopback host accepts a 1 MiB HTTP body. Reserve 64 KiB for routing
    # identities and JSON envelope overhead instead of accepting a payload
    # here that the next hop must deterministically refuse.
    API_REQUEST_MAX_BYTES = 960 * 1024
    API_RESULT_MAX_BYTES = 2 * 1024 * 1024
    API_SNAPSHOT_MAX_BYTES = API_RESULT_MAX_BYTES + 64 * 1024
    API_PROGRESS_VALUE_MAX_BYTES = 128 * 1024
    API_BATCH_TARGET_MAX = 64
    API_BATCH_OPERATION_MAX = 512
    # A batch fans out through short-lived ThreadManager TDTasks, never raw
    # Python threads.  Eight simultaneous target submissions are enough to
    # keep a few-dozen-node LAN busy without letting one TD session create a
    # thread per peer.  Every worker draws from one shared bounded queue, so
    # this is a hard concurrency ceiling rather than a chunk size.
    API_BATCH_WORKER_MAX = 8
    # A wait occupies the same serial worker that heals registration. Keep
    # every public turn below two heartbeat windows; longer operations return
    # a durable delivery id and are reconciled through getJob() instead.
    API_TIMEOUT_MAX_S = 60.0
    API_TERMINAL_REQUEST_STATES = ('completed', 'failed')

    # The wake listener is intentionally tiny and loopback-only.  It is a
    # separate standalone TDTask so Perform Mode can stop Envoy and every
    # ordinary Convoy call while this one bounded UDP socket remains asleep
    # in recvfrom().  Commands are bearer-authenticated, schema-closed and
    # handed to the TD main thread through a bounded Queue; the listener never
    # imports or touches TD.
    WAKE_PROTOCOL = 1
    WAKE_PACKET_MAX = 1024
    WAKE_QUEUE_MAX = 128
    WAKE_SOCKET_TIMEOUT_S = 0.25
    WAKE_POLL_MS = 250
    WAKE_DRAIN_MAX = 32
    WAKE_LEASE_MAX_CHARS = 128
    WAKE_TTL_DEFAULT_S = 120
    WAKE_TTL_MAX_S = 600

    # How long the install/start tail waits for the daemon to answer
    # /health before reporting what it actually sees. Without this the
    # readout would say 'Installed -- not running' for up to a minute
    # after a successful install, which reads exactly like a failure.
    HEALTH_WAIT_S = 20.0
    HEALTH_POLL_S = 1.0

    # Mirrors convoy_client.HOST_* -- that module owns the vocabulary and
    # a test pins these four against it. They are the TRANSIENT states,
    # which convoy_install never computes because they describe what this
    # extension is doing rather than what is on disk.
    HOST_CHECKING = 'checking'
    HOST_INSTALLING = 'installing'
    HOST_STARTING = 'starting'
    HOST_INSTALL_FAILED = 'install_failed'
    RUNTIME_CATALOG_FILENAME = 'convoy_runtime_catalog.json'

    # Status classes that deserve a WARNING on the transition INTO them.
    # 'unreachable' is here and 'absent'/'stale' are NOT, and the difference
    # is real: unreachable can only happen AFTER probe() confirmed a live
    # host app, so it means one vanished mid-call -- worth exactly one line.
    # Absence never was one, and warning about it would train the user to
    # ignore the field.
    _WARN_STATES = ('refused', 'error', 'host_error', 'unreachable')
    # Status classes that are pure noise in the log.
    _QUIET_STATES = ('registering',)

    def __init__(self, ownerComp):
        self.ownerComp = ownerComp
        # Resolve the system COMP on the main thread exactly once. The worker
        # receives only Queue/Event/plain-callable objects and never touches
        # this TD object.
        self.ThreadManager = op.TDResources.ThreadManager
        # Worker handoff slot (a plain attribute -- never a TD object).
        # None = in flight; dict (with '_gen') = published result.
        self._result = None
        self._gen = 0
        self._busy = False
        # The host-app channel gets its OWN slot, generation and busy
        # flag. Sharing the reconciler's would let a 20-second install and
        # a 30-second heartbeat drain each other's answers -- and the
        # reconcile loop has to keep running THROUGH an install, because
        # re-registering the moment the newly installed host app comes up
        # is exactly what makes Install look like it worked.
        self._host_result = None
        self._host_gen = 0
        self._host_busy = False
        self._policy_result = None
        self._policy_gen = 0
        self._policy_busy = False
        self._projecting_policy = False
        self._post_init_done = False
        self._logged = ''        # last logged status class (transitions only)
        self._tick_ms = self.TICK_MIN_MS
        self._network_rows_digest = None
        self._wake_record = None
        self._wake_poll_gen = 0

        # Arm the reconcile loop for THIS instance, tagged with a monotonic
        # generation stored on the COMP so it survives the reinit storm a
        # save produces. Only the newest generation's tick proceeds; the rest
        # exit as stale without rescheduling. Same shape, and the same
        # reasoning, as EnvoyExt's watchdog arming.
        gen = ownerComp.fetch('_convoy_gen', 0) + 1
        ownerComp.store('_convoy_gen', gen)
        self._initWorker(gen)
        self._initSiblingApi(gen)
        # Pending run() calls can outlive COMP replacement during upgrades,
        # so the scheduled string re-resolves the op and checks validity.
        run("o = op(%r)\nif o and o.valid: o.ext.ConvoyExt._convoyTick(%d)"
            % (ownerComp.path, gen), delayMilliSeconds=int(self.TICK_MIN_MS))

    # ==================================================================
    # Lifecycle
    # ==================================================================

    def onInitTD(self):
        """Post-init hook: defer everything that reads the network.

        TDN import can delete and recreate children AFTER extension init,
        so setup that depends on internal network state waits a few frames
        and must be idempotent (td-python.md). No network work happens
        here -- the reconcile tick owns that.
        """
        try:
            run("o = op(%r)\nif o and o.valid: o.ext.ConvoyExt._postInit()"
                % (self.ownerComp.path,), delayFrames=5)
        except Exception:
            pass

    def _postInit(self):
        """Idempotent deferred setup: project the readouts, nothing else."""
        if self._staleInstance() or self._post_init_done:
            return
        self._post_init_done = True
        try:
            # These two parameters are projections of host-private approval,
            # not project-authored configuration. A saved .toe/TDN/clone may
            # therefore arrive with a stale On value, but that value must
            # never become authority merely because TouchDesigner loaded it.
            # Until the first authenticated host response arrives, the only
            # truthful projection is the fail-closed/default state.
            self._resetUntrustedDangerProjections()
            self._ensureNodeName()
            self._publishId(self._readConvoyId())
            if not self._enabled() and not self._performing():
                self._status('Disabled')
                self._projectNodeRows([], 'Convoy is disabled')
        except Exception as e:
            self._log('post-init readout failed: %s' % (e,), 'DEBUG')

    def onDestroyTD(self):
        """Called on the OLD instance before a reinit replaces it.

        There is deliberately NOTHING to unregister: a reinit is not a
        disable, and the registration state that matters (runtime_id,
        node_id) lives in the per-process session precisely so it survives.
        The old ThreadManager task is signalled without joining; it may
        finish its current bounded call but accepts no further work. Dropping
        the result slots stops a superseded answer from being read by the new
        instance, and the poll chain retires through _staleInstance.
        """
        try:
            # A host action in flight dies with this instance: its poll
            # chain is armed against THIS object (run(..., self, ...)), so
            # the new instance never drains it and the worker's result
            # lands on a dead slot. Without this the readout is stranded
            # on a transient string -- 'Installing...' forever -- and
            # there is no non-mutating action in the UI that can clear it.
            # Editing ConvoyExt.py is enough to trigger it, because this
            # is a syncfile DAT and every save reinitializes the class.
            # _restoreHostStatus puts back the last KNOWN state and
            # invents nothing, which is exactly right here: an
            # interrupted install tells us nothing new about the host.
            if self._host_busy:
                self._restoreHostStatus()
            self._result = None
            self._busy = False
            self._host_result = None
            self._host_busy = False
            self._policy_result = None
            self._policy_busy = False
            self._projecting_policy = False
        except Exception:
            pass
        finally:
            try:
                self._destroySiblingApi()
            except Exception:
                pass
            try:
                self._stopWorker()
            except Exception:
                pass

    # ==================================================================
    # Host access, logging, parameter readouts (MAIN THREAD ONLY)
    # ==================================================================

    @property
    def _embody(self):
        """The host Embody COMP, bound to self.ownerComp so it resolves even
        from the root execution context of a surviving string-form run()."""
        return self.ownerComp.parent.Embody

    def _client(self):
        """The convoy_client module (a sibling DAT inside this COMP).

        MAIN THREAD ONLY, and the whole reason this is a method: `mod.name`
        is a live DAT lookup -- a TD access -- that re-resolves on every
        attribute get, so binding the module inside a worker body is a
        threading violation. _beginCall resolves it here and captures the
        module object in a local before the thread is created. The one
        reference below is deliberately the ONLY one in this file, and a
        test asserts that.

        It is also the one test seam: the in-TD suite patches this to a stub
        so no test ever opens a socket.
        """
        return mod.convoy_client

    def _safeClient(self):
        """_client() or None -- for paths that must not raise (status)."""
        try:
            return self._client()
        except Exception:
            return None

    def _installer(self):
        """The convoy_install module (a sibling DAT inside this COMP).

        MAIN THREAD ONLY, for exactly the reason _client() is: `mod.name`
        is a LIVE DAT LOOKUP that re-resolves on every attribute get, so
        binding this module inside a worker body is a threading
        violation. _hostContext resolves it here and captures the module
        OBJECT before any thread is created; the workers below only ever
        see that object.

        The reference below is deliberately the ONLY one in this file,
        and a test asserts that -- the same pin _client() carries. (Do
        not spell the attribute a second time anywhere, including in a
        comment: the test counts occurrences in the source text.)

        It is also the host-app test seam: the in-TD suite patches this to
        a stubbed installer so no test writes a payload, spawns schtasks,
        or touches the real per-user data dir.
        """
        return mod.convoy_install

    def _safeInstaller(self):
        """_installer() or None -- for paths that must not raise."""
        try:
            return self._installer()
        except Exception:
            return None

    def _hostRuntimeRelease(self):
        """Trusted local Convoy Runtime release context. MAIN THREAD ONLY.

        A production package may carry an embedded
        ``convoy_runtime_catalog`` Text DAT and keep its signed runtime ZIPs
        beside the source .tox. A source checkout instead consumes the
        checked-in ``dev/convoy/convoy_runtime_catalog.json``. Both forms hand
        workers plain JSON/path data only; neither form downloads anything or
        searches TD/system Python.

        The current checked-in catalog intentionally has no published assets.
        Returning it is still valuable: InstallHost can report the exact
        release gate (rather than pretending some other interpreter is usable)
        before showing a confirmation or starting a worker.
        """
        try:
            project_folder = str(project.folder or '')
        except Exception:
            project_folder = ''

        def local_path(value):
            value = os.path.expanduser(str(value or '').strip())
            if not value:
                return ''
            if not os.path.isabs(value):
                value = os.path.join(project_folder, value)
            return os.path.abspath(value)

        def external_tox_root():
            try:
                value = self._embody.par.externaltox.eval()
            except Exception:
                return ''
            path = local_path(value)
            return os.path.dirname(path) if path else ''

        # Release form: catalog metadata is part of the trusted .tox. Its
        # binary runtime asset remains a sidecar because a self-contained
        # CPython tree is not suitable Text DAT payload. If this DAT is
        # present but malformed, stop here: falling through to a different
        # catalog would make the release identity ambiguous.
        catalog_dat = self.ownerComp.op('convoy_runtime_catalog')
        if catalog_dat is not None:
            try:
                catalog = json.loads(str(catalog_dat.text or ''))
            except Exception as e:
                return {
                    'catalog': None,
                    'asset_root': None,
                    'source': 'embedded convoy_runtime_catalog DAT',
                    'error': 'embedded Convoy Runtime catalog is invalid '
                             'JSON (%s: %s)' % (type(e).__name__, e),
                }
            asset_root = ''
            try:
                file_par = getattr(catalog_dat.par, 'file', None)
                catalog_file = local_path(file_par.eval()) if file_par else ''
                if catalog_file:
                    asset_root = os.path.dirname(catalog_file)
            except Exception:
                asset_root = ''
            asset_root = asset_root or external_tox_root()
            return {
                'catalog': catalog,
                'asset_root': asset_root or None,
                'source': 'embedded convoy_runtime_catalog DAT',
                'error': '',
            }

        # Sidecar release form. Some load paths preserve External .tox until
        # the updater detaches it; when they do, the catalog and archive are
        # resolved only beside that exact .tox, never by a broad filesystem
        # search.
        release_root = external_tox_root()
        if release_root:
            catalog_path = os.path.join(
                release_root, self.RUNTIME_CATALOG_FILENAME)
            if os.path.isfile(catalog_path):
                return {'catalog': catalog_path,
                        'asset_root': release_root,
                        'source': catalog_path, 'error': ''}

        # Development form. ExportPortableTox strips this source reference,
        # so the branch cannot accidentally make a released .tox depend on
        # the repository checkout.
        try:
            source_dat = self._embody.op('EmbodyExt')
            source_par = (getattr(source_dat.par, 'file', None)
                          if source_dat is not None else None)
            source_path = local_path(source_par.eval()) if source_par else ''
        except Exception:
            source_path = ''
        if source_path:
            catalog_path = os.path.abspath(os.path.join(
                os.path.dirname(source_path), '..', '..', 'convoy',
                self.RUNTIME_CATALOG_FILENAME))
            if os.path.isfile(catalog_path):
                return {'catalog': catalog_path,
                        'asset_root': os.path.dirname(catalog_path),
                        'source': catalog_path, 'error': ''}

        return {'catalog': None, 'asset_root': None, 'source': '',
                'error': ''}

    def _hostModules(self):
        """{filename: source text} for the vendored host-app DATs.

        MAIN THREAD ONLY. `dat.text` is DAT CONTENT -- reading it from a
        worker is the same violation as reading a parameter -- so the
        whole payload is lifted into a plain dict here and handed over as
        data. The worker never sees an operator.

        Returns ONLY the modules that actually exist as DATs. It
        deliberately does NOT filter against convoy_install.HOST_MODULES:
        that tuple is documented in-code as a manifest of what to vendor,
        NOT a gate, and gating on it would silently drop any module added
        to dev/convoy/ but not yet added to the tuple -- shipping a
        payload the daemon cannot import. The byte-identity parity test
        (dev/convoy/test_convoy_host_vendor.py) is the enforcement, and
        it discovers the set by globbing the daemon sources for that
        exact reason.

        An absent `host` COMP returns {} rather than raising: a .tox
        upgraded from before the vendoring step has no such child, and
        the caller turns the empty dict into a stated failure.
        """
        host = self.ownerComp.op('host')
        if host is None:
            return {}
        modules = {}
        for child in host.children:
            try:
                if not child.isDAT:
                    continue
                name = self._hostModuleName(child)
                if not name:
                    continue
                modules[name] = child.text
            except Exception:
                # One unreadable DAT must not cost the other nine. The
                # caller compares the set it got against what the daemon
                # needs; a silently dropped module surfaces there.
                continue
        return modules

    @staticmethod
    def _hostModuleName(dat):
        """The payload filename for one vendored DAT, or '' to skip it.

        The externalized `file` par is the authority -- it is what Embody
        actually wrote the source to, so it cannot disagree with the
        parity test -- and the DAT name is the fallback for a DAT that is
        not externalized yet. Anything that is not a bare `*.py` filename
        is skipped rather than sanitised: write_payload would refuse it,
        and a guessed correction would vendor a file under a name the
        daemon does not import.
        """
        name = ''
        try:
            par = getattr(dat.par, 'file', None)
            if par is not None:
                raw = str(par.eval() or '').replace('\\', '/')
                name = raw.rsplit('/', 1)[-1]
        except Exception:
            name = ''
        if not name:
            try:
                name = '%s.py' % (dat.name,)
            except Exception:
                return ''
        return name if _BARE_MODULE_NAME.match(name) else ''

    def _log(self, msg, level='INFO'):
        try:
            self._embody.Log('Convoy: %s' % (msg,), level)
        except Exception:
            try:
                debug('[Convoy/%s] %s' % (level, msg))
            except Exception:
                pass

    @staticmethod
    def _setPar(par, value):
        """Assign through the readOnly dance (the status pars are locked)."""
        was = par.readOnly
        par.readOnly = False
        par.val = value
        par.readOnly = was

    def _publishId(self, convoy_id):
        """No-op: the Convoy ID is no longer a page parameter.

        It is an opaque, auto-generated identifier a user never chooses or
        types, so it stopped earning a row on the Convoy page. It remains
        available where it is actually useful: .embody/project.json, the
        log, and convoy_list_nodes. Kept as a method because several call
        sites publish it at natural moments (adopt, consent, reconcile) and
        a future advanced/details surface would restore it here.
        """
        return

    # A host-app state that BLOCKS Convoy (or is mid-flight) outranks the
    # node's own registration line, because it is the thing the user has to
    # act on. Anything else lets the node state show through.
    # Node states the USER must resolve before anything else can help. These
    # outrank even a blocking host-app line: installing a host app does not
    # fix an unsaved project, and showing the host line there is a wrong
    # signpost.
    _ACTIONABLE_NODE_TEXTS = (
        'Waiting for project save',
        'Consent required',
    )

    _BLOCKING_HOST_TEXTS = (
        'Not installed', 'Checking...', 'Installing...',
        'Installed -- starting...', 'Installed -- not running',
        'Installed -- stopped', 'Installed -- no supervisor',
        'Needs repair', 'Managed by another supervisor',
        'Install failed',
    )

    def _status(self, text):
        """Record the node/registration line and republish Status."""
        self._node_status_text = str(text)[:160]
        self._publishStatus()

    def _publishStatus(self):
        """Write the ONE Status readout.

        There used to be two fields -- Convoystatus and Convoyhoststatus --
        which between them showed a truncated node hash, a truncated host
        hash and a process id. None of that is actionable, and two status
        lines for one feature is one too many. This composes the single
        line: a blocking/transient host-app state wins, otherwise the node's
        own state shows.
        """
        # getattr, not direct access: this extension can land in a session
        # whose Convoy page has not been created yet (and in an upgraded
        # .tox that predates it). Same guard UpdaterExt._status uses for
        # Updatestatus.
        par = getattr(self._embody.par, 'Convoystatus', None)
        if par is None:
            return
        host = str(getattr(self, '_host_status_text', '') or '')
        node = str(getattr(self, '_node_status_text', '') or '')
        text = node
        if host and host.startswith(self._BLOCKING_HOST_TEXTS):
            text = host
        # ...unless the NODE is reporting something the user must act on
        # first. "Not installed" outranking "Waiting for project save" sent a
        # Mac user hunting for a host-app problem when the real answer was
        # save the .toe (2026-08-03). Order by what unblocks the user, not by
        # which subsystem produced the line.
        if node and node.startswith(self._ACTIONABLE_NODE_TEXTS):
            text = node
        self._setPar(par, (text or 'Disabled')[:160])

    @staticmethod
    def _sequenceByName(comp, name):
        """Resolve a custom sequence by enumeration (TD-safe and portable)."""
        try:
            return next((seq for seq in comp.seq
                         if seq is not None and seq.name == name), None)
        except Exception:
            return None

    @staticmethod
    def _sequenceBlockPar(comp, seq, block, index, base_name):
        """Resolve one custom-sequence block parameter across TD builds.

        Custom SequenceBlock ``.par`` lookup is inconsistent across builds;
        this mirrors TDNExt's proven attribute/bracket/full-name fallback.
        """
        par_collection = getattr(block, 'par', None)
        par = getattr(par_collection, base_name, None)
        if par is not None:
            return par
        try:
            par = par_collection[base_name]
            if par is not None:
                return par
        except Exception:
            pass
        for suffix in (base_name.lower(), base_name):
            par = getattr(comp.par, '%s%s%s' % (seq.name, index, suffix),
                          None)
            if par is not None:
                return par
        return None

    @staticmethod
    def _lastSeenText(age_s, online):
        """Humanize an optional host-projected heartbeat age."""
        try:
            if isinstance(age_s, bool):
                raise ValueError
            age = max(0.0, float(age_s))
            if not math.isfinite(age):
                raise ValueError
        except (TypeError, ValueError, OverflowError):
            return 'Now' if online else 'Unavailable'
        if age < 1.5:
            return 'Now'
        if age < 60:
            return '%ds ago' % int(age)
        if age < 3600:
            return '%dm ago' % int(age // 60)
        if age < 86400:
            return '%dh ago' % int(age // 3600)
        return '%dd ago' % int(age // 86400)

    @staticmethod
    def _nodeStatusRows(result):
        """Turn a bounded client directory result into UI-only row values.

        Deliberately minimal -- Node Name, IP, Status, Last Seen. The node's
        Embody version is folded INTO Status only when it is the point (an
        incompatibility); it is noise as a standing column otherwise. Per-node
        controller counts live in convoy_list_controllers, not here.
        """
        if not isinstance(result, dict) or result.get('state') != 'nodes':
            return None
        rows = []
        for node in result.get('nodes') or ():
            if not isinstance(node, dict):
                continue
            online = bool(node.get('online'))
            raw_status = str(node.get('status') or
                             ('online' if online else 'offline')).strip()
            status = raw_status[:1].upper() + raw_status[1:64]
            version = str(node.get('embody_version') or '').strip()
            if version and 'incompat' in raw_status.lower():
                status = '%s (v%s)' % (status, version[:32])
            name = str(node.get('node_name') or node.get('hostname') or
                       node.get('toe_name') or 'Unnamed node')[:512]
            ip = str(node.get('ip') or '-')[:255]
            rows.append({
                'Nodename': name,
                'Ipaddress': ip,
                'Nodestatus': status or 'Unknown',
                # The host may expose a bounded AGE, never a raw wall-clock
                # timestamp. Older hosts omit it, in which case the observed
                # online/offline state remains the honest fallback.
                'Lastseen': ConvoyExt._lastSeenText(
                    node.get('last_seen_age_s'), online),
            })
        return rows

    def _projectNodeRows(self, rows, empty_detail='No nodes discovered'):
        """Populate the read-only Convoy Nodes sequence. MAIN THREAD ONLY."""
        fields = ('Nodename', 'Ipaddress', 'Nodestatus', 'Lastseen')
        if not rows:
            rows = [{
                'Nodename': str(empty_detail or 'No nodes discovered')[:512],
                'Ipaddress': '-',
                'Nodestatus': 'Offline',
                'Lastseen': 'Never',
            }]
        # A stable tuple prevents 30-second heartbeats from dirtying/redrawing
        # an unchanged custom parameter page.
        digest = tuple(tuple(row.get(field) for field in fields)
                       for row in rows)
        if digest == self._network_rows_digest:
            return
        seq = self._sequenceByName(self._embody, 'Convoynodes')
        if seq is None:
            return
        try:
            seq.numBlocks = len(rows)
            blocks = list(seq.blocks)
        except Exception as e:
            self._log('could not size Convoy Status sequence: %s' % (e,),
                      'DEBUG')
            return
        try:
            for index, row in enumerate(rows, 1):
                block = blocks[index - 1]
                for field in fields:
                    par = self._sequenceBlockPar(
                        self._embody, seq, block, index, field)
                    if par is not None:
                        self._setPar(par, row.get(field))
        except Exception as e:
            self._log('could not populate Convoy Status sequence: %s' % (e,),
                      'DEBUG')
            return
        self._network_rows_digest = digest

    def _applyNetworkNodes(self, result):
        """Apply one worker-fetched directory without erasing good stale data."""
        rows = self._nodeStatusRows(result)
        if rows is not None:
            # Keep the RAW ages plus the moment they were true, so the tick
            # can age the "Last Seen" column between fetches. Without this the
            # column is a relative time frozen at write time: the directory is
            # only fetched on the 30s heartbeat, so a user reading the page in
            # between sees a number that is minutes old but says "15s ago".
            try:
                self._node_ages = [
                    (n.get('last_seen_age_s'), bool(n.get('online')))
                    for n in (result.get('nodes') or ())
                    if isinstance(n, dict)]
                self._node_ages_at = time.time()
            except Exception:
                self._node_ages = None
            detail = ('No enabled nodes in this Convoy' if not rows
                      else 'No nodes discovered')
            self._projectNodeRows(rows, detail)
            return
        # A transient directory failure must not make every known node
        # disappear.  Only initialize the untouched placeholder with an
        # actionable reason; the next successful heartbeat replaces it.
        if self._network_rows_digest is None:
            reason = str((result or {}).get('reason') or
                         (result or {}).get('detail') or
                         'status unavailable')[:160]
            self._projectNodeRows([], 'Status unavailable: %s' % reason)

    def _refreshLastSeen(self):
        """Age the Last Seen column between directory fetches. MAIN THREAD.

        Cheap and bounded: it writes only the one column, only when the text
        actually changes, and only for blocks that already exist. Everything
        else on the page still redraws on the digest, so this cannot cause
        the per-frame parameter churn the digest exists to prevent.
        """
        ages = getattr(self, '_node_ages', None)
        if not ages:
            return
        seq = self._sequenceByName(self._embody, 'Convoynodes')
        if seq is None or not seq.numBlocks:
            return
        elapsed = max(0.0, time.time() - getattr(self, '_node_ages_at', 0.0))
        try:
            blocks = list(seq.blocks)
        except Exception:
            return
        for index, (age, online) in enumerate(ages):
            if index >= len(blocks):
                break
            aged = None if age is None else float(age) + elapsed
            text = self._lastSeenText(aged, online)
            par = self._sequenceBlockPar(
                self._embody, seq, blocks[index], index + 1, 'Lastseen')
            try:
                if par is not None and str(par.eval()) != text:
                    self._setPar(par, text)
            except Exception:
                continue

    def _enabled(self):
        par = getattr(self._embody.par, 'Convoyenable', None)
        try:
            return bool(par.eval()) if par is not None else False
        except Exception:
            return False

    def _setEnabled(self, value):
        """Flip the canonical gate (Toggle pars are 0/1)."""
        if not value:
            self._revokeSiblingApi()
        par = getattr(self._embody.par, 'Convoyenable', None)
        if par is not None:
            par.val = 1 if value else 0

    def _resetUntrustedDangerProjections(self):
        """Reset saved host-policy projections. MAIN THREAD ONLY.

        TD Python approval is node/host-private and Full Shell approval is
        host-private. Neither may be granted by a project file, config restore,
        clone, peer update, or synthetic parameter callback. The host API that
        will own local confirmation is not present yet, so this scaffold only
        supports the safe state and never sends either value in registration.
        """
        reset = []
        self._projecting_policy = True
        try:
            for name, safe in (
                    ('Convoyallowtdpython', 0),
                    ('Convoyallowfullshell', 0),
                    ('Convoyartifactquota', 1024)):
                par = getattr(self._embody.par, name, None)
                if par is None:
                    continue
                try:
                    if par.eval() != safe:
                        par.val = safe
                        reset.append(name)
                except Exception:
                    try:
                        par.val = safe
                    except Exception:
                        pass
        finally:
            self._projecting_policy = False
        if reset:
            self._log('ignored saved capability approval projection(s): %s; '
                      'the local host policy is authoritative'
                      % (', '.join(reset),), 'WARNING')

    def PolicyProjectionActive(self):
        """True only during a host-authored parameter projection."""
        return bool(self._projecting_policy)

    def _applyPolicyProjection(self, result):
        """Project one validated convoy_client policy result. MAIN THREAD."""
        policy = (result or {}).get('policy')
        if not isinstance(policy, dict):
            return False
        required = ('generation', 'allow_td_python', 'allow_full_shell',
                    'artifact_quota_mb')
        if any(name not in policy for name in required):
            return False
        values = {
            'Convoyallowtdpython': 1 if policy['allow_td_python'] else 0,
            'Convoyallowfullshell': 1 if policy['allow_full_shell'] else 0,
            'Convoyartifactquota': int(policy['artifact_quota_mb']),
        }
        self._projecting_policy = True
        try:
            for name, value in values.items():
                par = getattr(self._embody.par, name, None)
                if par is not None and par.eval() != value:
                    par.val = value
        except Exception as e:
            self._log('could not project host safety policy: %s' % (e,),
                      'DEBUG')
            return False
        finally:
            self._projecting_policy = False
        self._session()['policy'] = dict(policy)
        return True

    def LocalDangerGateChanged(self, par_name, requested):
        """Request a local host-policy change; the parameter is projection."""
        if par_name not in ('Convoyallowtdpython', 'Convoyallowfullshell'):
            return {'ok': False, 'reason': 'unknown_capability'}
        if getattr(self, '_projecting_policy', False):
            return {'ok': True, 'projected': True}
        setting = ('td_python' if par_name == 'Convoyallowtdpython'
                   else 'full_shell')
        session = self._session()
        policy = session.get('policy') or {}
        authoritative = bool(policy.get(
            'allow_td_python' if setting == 'td_python'
            else 'allow_full_shell', False))
        par = getattr(self._embody.par, par_name, None)
        self._projecting_policy = True
        try:
            if par is not None:
                # The host has not accepted anything yet. Restore its last
                # projection immediately so a saved/synthetic On can never be
                # authority during the confirmation round trip.
                par.val = 1 if authoritative else 0
        finally:
            self._projecting_policy = False
        if getattr(self, '_policy_busy', False):
            self._log('another Convoy safety-policy request is still in '
                      'progress', 'INFO')
            return {'ok': False, 'reason': 'policy_busy'}
        node_id = str(session.get('node_id') or '')
        if setting == 'td_python' and not node_id:
            self._log('Allow Execute TD Python waits until this node has '
                      'registered with the Convoy host app', 'WARNING')
            return {'ok': False, 'reason': 'node_not_registered'}
        action = 'policy_begin' if bool(requested) else 'policy_disable'
        self._beginPolicyCall(action, setting=setting, node_id=node_id)
        return {'ok': True, 'pending': True, 'enabled': authoritative}

    def LocalArtifactQuotaChanged(self, requested):
        """CAS a host-wide quota, reverting the Par until host acceptance."""
        if getattr(self, '_projecting_policy', False):
            return {'ok': True, 'projected': True}
        session = self._session()
        current = int((session.get('policy') or {}).get(
            'artifact_quota_mb', 1024))
        par = getattr(self._embody.par, 'Convoyartifactquota', None)
        self._projecting_policy = True
        try:
            if par is not None:
                par.val = current
        finally:
            self._projecting_policy = False
        try:
            requested_number = float(requested)
            requested = int(requested_number)
        except (TypeError, ValueError, OverflowError):
            return {'ok': False, 'reason': 'invalid_quota'}
        if requested_number != requested:
            return {'ok': False, 'reason': 'invalid_quota'}
        if requested < 0 or requested > 1024 * 1024:
            return {'ok': False, 'reason': 'invalid_quota'}
        if getattr(self, '_policy_busy', False):
            return {'ok': False, 'reason': 'policy_busy'}
        self._beginPolicyCall('policy_quota', quota_mb=requested)
        return {'ok': True, 'pending': True}

    def _performing(self):
        """True while Embody's Perform Mode is on.

        Single authority: EmbodyExt._performMode (the requested Performmode
        state), never a status string which every writer here overwrites.
        Envoy separately consults the narrower _envoyPerformMode gate so a
        wake lease can resume command service without unsuspending unrelated
        Embody features. An exception reads False so a broken extension
        reference cannot silently switch Convoy off forever.
        """
        try:
            return bool(self._embody.ext.Embody._performMode)
        except Exception:
            return False

    @staticmethod
    def _savedToe():
        """The .toe on disk for this project, or None if it was never saved.

        Checks the invariant directly (a file at project.folder /
        project.name) rather than project.modified / project.dirty -- both
        proxies have failed here in opposite directions, which is why
        _wizardRecoveryPoint and RunDestructiveTests check the file too.
        """
        try:
            path = os.path.join(project.folder, project.name)
            return path if os.path.isfile(path) else None
        except Exception:
            return None

    def _readConvoyId(self):
        """This project's convoy id from .embody/project.json, or ''."""
        try:
            return self._embody.ext.Embody._readConvoyId() or ''
        except Exception:
            return ''

    def _readConsentScope(self):
        """The exact locally granted Convoy scope, or ``''``."""
        try:
            entry = self._embody.ext.Embody._readConvoyEntry() or {}
            return str(entry.get('consent_scope') or '')
        except Exception:
            return ''

    def _readBindingState(self):
        """Safe project realm state; old IDs are established, never fresh."""
        embody = self._embody.ext.Embody
        try:
            state = str(embody._readConvoyBindingState() or '')
            if state in ('candidate', 'established'):
                return state
        except Exception:
            pass
        try:
            entry = embody._readConvoyEntry() or {}
            if entry.get('id'):
                state = str(entry.get('binding_state') or '')
                return state if state in ('candidate', 'established') \
                    else 'established'
        except Exception:
            pass
        return ''

    def _envoyPort(self):
        """Envoy's confirmed-bound loopback port, or None.

        Goes through EnvoyExt.RuntimePort(). Never parses Envoystatus for a
        'port (\\d+)' -- that is the exact anti-pattern A-9 removed.
        """
        try:
            return self._embody.ext.Envoy.RuntimePort()
        except Exception:
            return None

    # ==================================================================
    # Per-process session state (survives reinit, dies with the process)
    # ==================================================================

    def _session(self):
        """This COMP's per-PROCESS session dict, on a sys attribute.

        Holds runtime_id (minted at most once per launch by
        convoy_client.ensure_runtime_id), node_id (without which a disable
        cannot unregister), the last tuple actually sent, and the schedule.
        See the module docstring for why neither an instance attribute nor
        COMP storage can hold these.
        """
        store = getattr(sys, '_convoy_sessions', None)
        if not isinstance(store, dict):
            store = {}
            sys._convoy_sessions = store
        return store.setdefault(self.ownerComp.path, {})

    # ==================================================================
    # Perform-safe local wake listener
    # ==================================================================

    def _remoteWakeEnabled(self):
        par = getattr(self._embody.par, 'Convoyremotewake', None)
        try:
            return bool(par.eval()) if par is not None else True
        except Exception:
            return False

    def _wakeGrace(self):
        par = getattr(self._embody.par, 'Convoywakegrace', None)
        try:
            value = int(par.eval()) if par is not None else 60
        except Exception:
            value = 60
        return max(0, min(value, 3600))

    def _performRequested(self):
        """The user's Perform Mode Par, ignoring a temporary wake override."""
        par = getattr(self._embody.par, 'Performmode', None)
        try:
            return bool(par.eval()) if par is not None else False
        except Exception:
            return False

    def _wakeActive(self):
        try:
            return bool(self._embody.ext.Embody._convoyWakeActive)
        except Exception:
            return False

    @classmethod
    def _parseWakeDatagram(cls, packet, token, address):
        """Validate one loopback wake packet; return a detached command.

        Pure Python and TD-free so both the listener thread and off-TD tests
        exercise the exact same parser.  ``None`` is an intentional silent
        drop: this is a private datagram endpoint, not an oracle.
        """
        try:
            if (not isinstance(packet, bytes)
                    or not packet or len(packet) > cls.WAKE_PACKET_MAX):
                return None
            if (not isinstance(address, tuple) or not address
                    or address[0] != '127.0.0.1'):
                return None
            body = json.loads(packet.decode('utf-8'))
            if not isinstance(body, dict):
                return None
            allowed = {'v', 'auth', 'action', 'lease_id', 'ttl_s'}
            if set(body) - allowed:
                return None
            if body.get('v') != cls.WAKE_PROTOCOL:
                return None
            supplied = body.get('auth')
            if (not isinstance(supplied, str)
                    or not hmac.compare_digest(supplied, str(token))):
                return None
            action = body.get('action')
            if action not in ('acquire', 'touch', 'release'):
                return None
            lease_id = body.get('lease_id')
            if (not isinstance(lease_id, str) or not lease_id
                    or len(lease_id) > cls.WAKE_LEASE_MAX_CHARS
                    or any(ord(ch) < 33 or ord(ch) > 126
                           for ch in lease_id)):
                return None
            ttl_s = body.get('ttl_s', cls.WAKE_TTL_DEFAULT_S)
            if (isinstance(ttl_s, bool) or not isinstance(ttl_s, int)
                    or ttl_s < 1 or ttl_s > cls.WAKE_TTL_MAX_S):
                return None
            return {'action': action, 'lease_id': lease_id,
                    'ttl_s': ttl_s}
        except (UnicodeDecodeError, ValueError, TypeError):
            return None

    @staticmethod
    def _wakeListenerLoop(command_queue, shutdown_event, ready_event,
                          token, state, packet_max, timeout_s):
        """Loopback UDP listener. WORKER THREAD; absolutely zero TD access."""
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.bind(('127.0.0.1', 0))
            sock.settimeout(float(timeout_s))
            state['port'] = int(sock.getsockname()[1])
            state['error'] = ''
            ready_event.set()
            while not shutdown_event.is_set():
                try:
                    packet, address = sock.recvfrom(int(packet_max) + 1)
                except socket.timeout:
                    continue
                except OSError as e:
                    if not shutdown_event.is_set():
                        state['error'] = '%s: %s' % (type(e).__name__, e)
                    break
                command = ConvoyExt._parseWakeDatagram(
                    packet, token, address)
                if command is None:
                    continue
                command['received_at'] = time.monotonic()
                try:
                    command_queue.put_nowait(command)
                except Full:
                    # Keep the newest authenticated intent.  Losing an older
                    # release cannot strand Perform Mode: every acquire has a
                    # hard TTL and the main-thread watchdog expires it.
                    try:
                        command_queue.get_nowait()
                        command_queue.task_done()
                    except Empty:
                        pass
                    try:
                        command_queue.put_nowait(command)
                    except Full:
                        pass
        except Exception as e:
            state['error'] = '%s: %s' % (type(e).__name__, e)
            ready_event.set()
        finally:
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass
            state['stopped'] = True
            ready_event.set()
        return {'stopped': True, 'port': state.get('port')}

    def _ensureWakeListener(self):
        """Adopt or start this node's one process-local wake listener."""
        registry = getattr(sys, '_convoy_wake_listeners', None)
        if not isinstance(registry, dict):
            registry = {}
        path = self.ownerComp.path
        record = registry.get(path)
        if isinstance(record, dict) and not record.get('shutdown').is_set():
            thread = record.get('thread')
            alive = True
            if thread is not None:
                try:
                    alive = bool(thread.is_alive())
                except Exception:
                    alive = True
            if alive and not record.get('state', {}).get('stopped'):
                already_adopted = self._wake_record is record
                self._wake_record = record
                if not already_adopted:
                    self._armWakePoll()
                return True

        command_queue = Queue(maxsize=self.WAKE_QUEUE_MAX)
        shutdown = Event()
        ready = Event()
        token = secrets.token_urlsafe(32)
        state = {'port': None, 'error': '', 'stopped': False}
        record = {
            'queue': command_queue, 'shutdown': shutdown, 'ready': ready,
            'token': token, 'state': state, 'task': None, 'thread': None,
        }
        task = self.ThreadManager.TDTask(
            target=ConvoyExt._wakeListenerLoop,
            args=(command_queue, shutdown, ready, token, state,
                  self.WAKE_PACKET_MAX, self.WAKE_SOCKET_TIMEOUT_S))
        thread = self.ThreadManager.EnqueueTask(task, standalone=True)
        if thread is None:
            shutdown.set()
            state['error'] = 'ThreadManager refused the wake listener task'
            state['stopped'] = True
            self._wake_record = record
            return False
        record['task'] = task
        record['thread'] = thread
        registry[path] = record
        sys._convoy_wake_listeners = registry
        self._wake_record = record
        self._armWakePoll()
        return True

    def _stopWakeListener(self):
        record = self._wake_record
        if not isinstance(record, dict):
            registry = getattr(sys, '_convoy_wake_listeners', {})
            record = registry.get(self.ownerComp.path)
        if isinstance(record, dict):
            try:
                record['shutdown'].set()
            except Exception:
                pass
        registry = getattr(sys, '_convoy_wake_listeners', None)
        if isinstance(registry, dict):
            registry.pop(self.ownerComp.path, None)
        self._wake_record = None
        self._wake_poll_gen += 1
        self.ResetWakeLeases(close_override=True)

    def _wakeEndpoint(self):
        record = self._wake_record
        if (not self._remoteWakeEnabled() or not isinstance(record, dict)
                or record.get('shutdown').is_set()):
            return None, None
        state = record.get('state') or {}
        port = state.get('port')
        token = record.get('token')
        if (not isinstance(port, int) or not (1 <= port <= 65535)
                or not isinstance(token, str)):
            return None, None
        return port, token

    def _armWakePoll(self):
        self._wake_poll_gen += 1
        generation = self._wake_poll_gen
        run('args[0]._pollWakeCommands(args[1])', self, generation,
            delayMilliSeconds=self.WAKE_POLL_MS)

    def _pollWakeCommands(self, generation):
        """Apply wake leases on TD's main thread and expire abandoned ones."""
        if self._staleInstance() or generation != self._wake_poll_gen:
            return
        record = self._wake_record
        if not isinstance(record, dict) or record.get('shutdown').is_set():
            return
        session = self._session()
        leases = session.setdefault('wake_leases', {})
        now = time.monotonic()
        queue = record.get('queue')
        for _ in range(self.WAKE_DRAIN_MAX):
            try:
                command = queue.get_nowait()
            except Empty:
                break
            try:
                lease_id = command['lease_id']
                if command['action'] == 'release':
                    if lease_id in leases:
                        leases[lease_id] = now + self._wakeGrace()
                else:
                    leases[lease_id] = now + int(command['ttl_s'])
            finally:
                queue.task_done()
        for lease_id, expires_at in list(leases.items()):
            try:
                expired = float(expires_at) <= now
            except (TypeError, ValueError):
                expired = True
            if expired:
                leases.pop(lease_id, None)

        desired = bool(leases and self._remoteWakeEnabled()
                       and self._performRequested())
        active = self._wakeActive()
        if desired and not active:
            if self._embody.ext.Embody._beginConvoyWake():
                self._log('Perform Mode temporarily awakened for Convoy',
                          'INFO')
                session['sent'] = None
                session['next_call_at'] = None
                self._reconcile(force=True)
        elif active and not desired:
            self._embody.ext.Embody._endConvoyWake()
            self._log('Convoy wake grace elapsed; Perform Mode restored',
                      'INFO')
            session['sent'] = None
            session['next_call_at'] = None
            self._reconcile(force=True)
        run('args[0]._pollWakeCommands(args[1])', self, generation,
            delayMilliSeconds=self.WAKE_POLL_MS)

    def ResetWakeLeases(self, close_override=True):
        """Drop process-local leases after a local mode/membership change."""
        session = self._session()
        session['wake_leases'] = {}
        if close_override and self._wakeActive():
            try:
                self._embody.ext.Embody._endConvoyWake()
            except Exception:
                pass

    def WakeSettingsChanged(self):
        """Apply local wake/grace changes and refresh host registration."""
        if not self._remoteWakeEnabled():
            self._stopWakeListener()
        else:
            self._ensureWakeListener()
        if self._enabled():
            session = self._session()
            session['sent'] = None
            session['next_call_at'] = None
            self._reconcile(force=True)

    # ==================================================================
    # The reconciler
    # ==================================================================

    def _convoyTick(self, gen=0):
        """One reconcile pass, then reschedule. MAIN THREAD ONLY.

        gen == 0 is a legacy/manual tick armed before a generation existed;
        it is allowed through so the loop can never be orphaned.
        """
        # Collapse the armed-tick storm a save's strip/restore produces (one
        # tick armed per reinit, all coming due together) into a single live
        # loop: only the newest armed generation proceeds.
        try:
            if gen and gen != self.ownerComp.fetch('_convoy_gen', 0):
                return
        except Exception:
            pass
        # Die ONLY when a reinit has replaced this instance; the new instance
        # arms its own loop from __init__.
        try:
            if self.ownerComp.ext.ConvoyExt is not self:
                return
        except Exception:
            return

        try:
            self._reconcile()
        except Exception as e:
            try:
                self._log('tick error (continuing): %s' % (e,), 'DEBUG')
            except Exception:
                pass

        # Always reschedule -- the loop is instance-tied and only the guards
        # above end it. A transient tick error never stops reconciliation.
        run("o = op(%r)\nif o and o.valid: o.ext.ConvoyExt._convoyTick(%d)"
            % (self.ownerComp.path, gen),
            fromOP=self.ownerComp, delayMilliSeconds=int(self._tick_ms))

    def _reconcile(self, force=False):
        """Compare desired state with what was sent; call at most once.

        MAIN THREAD ONLY. Never raises out of the ordinary paths -- the tick
        wraps it, but every branch here is meant to be total.
        """
        # Age the Last Seen column first, on EVERY tick, including the ticks
        # that issue no network call at all (steady state is deliberately
        # call-free). Otherwise the column only moves when the directory
        # happens to be refetched.
        try:
            self._refreshLastSeen()
        except Exception:
            pass
        if self._busy:
            # A call is already in flight; its poll owns the next schedule.
            self._tick_ms = self.TICK_MIN_MS
            return
        if self._host_busy:
            # One long-lived worker deliberately serializes registration
            # with install/start/stop. Do not queue a short registration
            # behind a potentially long installer and then time out its
            # shorter poll budget before execution even begins.
            self._tick_ms = self.TICK_MIN_MS
            return

        client = self._safeClient()
        if client is None:
            self._tick_ms = self.TICK_MAX_MS
            self._status('Error: convoy_client module missing')
            self._logOnce('client_missing',
                          'the convoy_client module is missing from the '
                          'convoy COMP -- reinstall Embody', 'WARNING')
            return

        session = self._session()
        enabled = self._enabled()

        if not enabled:
            self._revokeSiblingApi()
            self._stopWakeListener()
            if session.get('registered'):
                # Disabled without going through parexec (a settings restore,
                # or a scripted par write). Clear the host's port once.
                self._beginCall('unregister', client, session)
                return
            session['sent'] = None
            session['next_call_at'] = None
            self._tick_ms = self.TICK_MAX_MS
            self._apply({'state': client.STATE_DISABLED}, client)
            self._projectNodeRows([], 'Convoy is disabled')
            return

        if not self._savedToe():
            # Refuse a never-saved project: otherwise every scratch TD launch
            # mints a junk node record keyed on a throwaway folder.
            session['sent'] = None
            session['next_call_at'] = None
            self._tick_ms = self.TICK_MAX_MS
            self._apply({'state': client.STATE_UNSAVED}, client)
            return

        # A pre-LAN project may have Convoy Enable persisted On alongside a
        # narrower loopback-only consent marker.  Never let a background tick
        # silently widen that grant: turn the gate back off and require the
        # user to enable it locally, where _ensureConsent shows the trusted-
        # LAN warning and records the new scope.
        convoy_id = self._readConvoyId()
        if convoy_id and self._readConsentScope() != self.CONSENT_SCOPE:
            session['sent'] = None
            session['next_call_at'] = None
            self._setEnabled(False)
            self._publishId(convoy_id)
            self._tick_ms = self.TICK_MAX_MS
            self._status('Consent required -- enable Convoy again')
            self._projectNodeRows([], 'Trusted-LAN consent required')
            self._logOnce(
                'convoy_scope_upgrade_required',
                'Convoy stayed off because this project only recorded the '
                'older loopback scope. Enable Convoy locally to review the '
                'trusted-LAN access warning.', 'WARNING')
            return

        if not convoy_id:
            # Enabled with no convoy key in .embody/project.json. Minting is
            # gated on an EXPLICIT enable (A-13) and never happens on a tick
            # -- no modal may fire during startup -- so this is a
            # misconfiguration (the key was removed, or a persisted toggle
            # was restored onto a project.json that lost it), not absence.
            # Say so honestly, do no network work, and wait.
            session['sent'] = None
            session['next_call_at'] = None
            self._tick_ms = self.TICK_MAX_MS
            self._apply({'state': client.STATE_ERROR,
                         'detail': 'no convoy id -- turn Convoy Enable off '
                                   'and on again to mint one'}, client)
            return

        # This is the only Convoy work intentionally kept alive in Perform
        # Mode. It starts only after membership, project persistence and LAN
        # consent are all valid; an unsaved/declined project consumes no
        # listener thread.
        if self._remoteWakeEnabled():
            self._ensureWakeListener()
        elif self._wake_record is not None:
            self._stopWakeListener()

        state = self._desiredState(session, convoy_id)
        due_at = session.get('next_call_at')
        due = due_at is None or time.monotonic() >= due_at
        # A changed tuple calls immediately; an unchanged one waits out the
        # heartbeat window. Note that any FAILED call clears `sent`, so a
        # failing node always takes the first branch -- and its rate limit
        # is then the TICK CADENCE itself, which _scheduleFrom derives from
        # the very same next_call_at (the jittered backoff). One clock, not
        # two, so the two can never disagree.
        if force or state != session.get('sent') or due:
            self._beginCall('register', client, session, state=state)
            return
        self._tick_ms = self._scheduleFrom(session)

    def _desiredState(self, session, convoy_id):
        """D2's desired-state tuple, resolved ENTIRELY on the main thread.

            (enabled, project_root, comp_path, convoy_id, envoy_port, host_id,
             toe_path, hostname, process_id, embody_version, td_version,
             node_name, remote_wake, perform_mode, wake_active,
             wake_port, wake_token, wake_grace_s, binding_state)

        Comparing it against the last tuple actually sent is what makes an
        unchanged tick cost zero network calls.

        host_id is the host app identity we last OBSERVED. It is promoted
        into `sent` only on a successful register, so a call that saw a new
        host identity but did not complete (a refusal, a 5xx) leaves the
        tuple mismatched and the next tick retries immediately instead of
        waiting out the heartbeat.

        comp_path is the Embody COMP's path: the host addresses a node by
        (project_root, comp_path), and the Embody COMP is what holds the
        Envoy server the host dispatches back to.
        """
        try:
            project_root = str(self._embody.ext.Embody._findProjectRoot())
        except Exception:
            project_root = str(project.folder)
        toe_path = str(self._savedToe() or '')
        try:
            hostname = str(socket.gethostname() or '').strip() or 'localhost'
        except Exception:
            hostname = 'localhost'
        toe_name = os.path.splitext(os.path.basename(toe_path))[0] or 'Untitled'
        try:
            embody_version = str(self._embody.par.Version.eval() or '')
        except Exception:
            embody_version = ''
        try:
            td_version = str(app.version or '')
        except Exception:
            td_version = ''
        node_name = self._nodeName(hostname, toe_name)
        remote_wake = self._remoteWakeEnabled()
        perform_requested = self._performRequested()
        # A wake listener is a routable endpoint ONLY for a sleeping/waking
        # Perform node. Outside Perform Mode, advertising a wake-only route
        # could mark a node online even though dispatch correctly requires its
        # normal Envoy port. The listener may remain warm locally; its secret
        # endpoint is simply withdrawn from the host until it is applicable.
        endpoint = (self._wakeEndpoint()
                    if remote_wake and perform_requested else (None, None))
        wake_port, wake_token = self._advertisedWakeEndpoint(
            remote_wake, perform_requested, endpoint)
        return (True,
                project_root,
                str(self._embody.path),
                str(convoy_id),
                self._envoyPort(),
                str(session.get('host_id') or ''),
                toe_path,
                hostname,
                int(os.getpid()),
                embody_version,
                td_version,
                node_name,
                remote_wake,
                perform_requested,
                self._wakeActive(),
                wake_port,
                wake_token,
                self._wakeGrace(),
                self._readBindingState() or 'established')

    def _nodeName(self, hostname, toe_name):
        """Bounded display-only name, honoring the Node Name parameter.

        The shipped parameter starts in expression mode and evaluates to the
        same automatic ``hostname / toe-stem`` fallback used here. Typing a
        value switches it to constant mode, which is the user's persistent
        display override. Empty/error values fall back; routing never uses it.
        """
        automatic = '%s / %s' % (str(hostname or 'localhost'),
                                  str(toe_name or 'Untitled'))
        par = getattr(self._embody.par, 'Convoynodename', None)
        try:
            value = str(par.eval() if par is not None else '').strip()
        except Exception:
            value = ''
        return (value or automatic)[:512]

    def _ensureNodeName(self):
        """Fill Node Name with this machine's `hostname / toe-stem`.

        Deliberately a RUNTIME fill, not a parameter expression: TouchDesigner
        stores an expression's last evaluated result alongside the expression,
        and _scrubTransientPars skips expression-mode pars (scrubbing one
        would destroy the reference) -- so an expression baked this
        developer's computer name into the released .tox (found 2026-08-03).
        As a constant it is registered transient, exports empty, and each
        machine refills its own. A user edit is a persistent override that
        this never clobbers. Idempotent.
        """
        par = getattr(self._embody.par, 'Convoynodename', None)
        if par is None:
            return
        try:
            if par.mode != ParMode.CONSTANT or str(par.eval() or '').strip():
                return
        except Exception:
            return
        try:
            hostname = str(socket.gethostname() or '').strip() or 'localhost'
        except Exception:
            hostname = 'localhost'
        try:
            toe_stem = str(project.name or '').rsplit('.', 1)[0] or 'Untitled'
        except Exception:
            toe_stem = 'Untitled'
        try:
            par.val = ('%s / %s' % (hostname, toe_stem))[:512]
        except Exception:
            pass

    @staticmethod
    def _advertisedWakeEndpoint(remote_wake, perform_mode, endpoint):
        """Expose a wake-only route exactly while Perform wake can use it."""
        if remote_wake and perform_mode and isinstance(endpoint, tuple):
            return endpoint
        return None, None

    def _scheduleFrom(self, session):
        """Tick delay in ms: soon enough to serve the next due call, never a
        busy loop. Clamped to [TICK_MIN_MS, TICK_MAX_MS]."""
        due_at = session.get('next_call_at')
        if due_at is None:
            return self.TICK_MAX_MS
        remaining_ms = int(max(0.0, due_at - time.monotonic()) * 1000)
        return max(self.TICK_MIN_MS, min(remaining_ms, self.TICK_MAX_MS))

    # ==================================================================
    # Long-lived ThreadManager worker + bounded main-thread poll
    # ==================================================================

    def _initWorker(self, generation):
        """Create this extension generation's plain-Python worker state.

        The task starts lazily on the first call, so a project with Convoy
        disabled consumes no worker thread. State lives on the instance;
        only the previous generation's stop handles live in ``sys`` so a
        recompiled module can retire them without storing an unpicklable
        Event on the COMP.
        """
        self._worker_generation = int(generation)
        self._worker_queue = Queue(maxsize=self.WORKER_QUEUE_MAX)
        self._worker_shutdown = Event()
        self._worker_task = None
        self._worker_thread = None

        registry = getattr(sys, '_convoy_workers', None)
        if not isinstance(registry, dict):
            registry = {}
        previous = registry.get(self.ownerComp.path)
        if isinstance(previous, dict):
            old_event = previous.get('shutdown')
            old_queue = previous.get('queue')
            try:
                if old_event is not None:
                    old_event.set()
            except Exception:
                pass
            try:
                if old_queue is not None:
                    old_queue.put_nowait(None)
            except Exception:
                pass
        registry[self.ownerComp.path] = {
            'generation': self._worker_generation,
            'shutdown': self._worker_shutdown,
            'queue': self._worker_queue,
        }
        sys._convoy_workers = registry

    def _stopWorker(self):
        """Generation-safe, non-blocking worker shutdown.

        Never joins and never mutates ThreadManager internals during an
        extension reinit. A bounded network/subprocess call already running
        may finish, but the Event prevents the loop from accepting another
        queued callable afterward.
        """
        event = getattr(self, '_worker_shutdown', None)
        queue = getattr(self, '_worker_queue', None)
        if event is not None:
            event.set()
        try:
            if queue is not None:
                queue.put_nowait(None)
        except Full:
            # A full queue is fine: the set Event is authoritative and the
            # loop exits before taking another job.
            pass

    @staticmethod
    def _workerLoop(work_queue, shutdown_event, generation, idle_s=0.25):
        """Long-lived TDTask target. WORKER THREAD; zero TD access."""
        while not shutdown_event.is_set():
            try:
                fn = work_queue.get(timeout=idle_s)
            except Empty:
                continue
            try:
                if fn is None or shutdown_event.is_set():
                    break
                # Each submitted closure catches and publishes its own error,
                # so one failed call cannot kill this long-lived task.
                fn()
            finally:
                work_queue.task_done()
        return {'generation': generation, 'stopped': True}

    def _ensureWorker(self):
        """Start this generation's standalone TDTask once. MAIN THREAD."""
        if self._worker_shutdown.is_set():
            return False
        thread = self._worker_thread
        if thread is not None:
            try:
                if thread.is_alive():
                    return True
            except Exception:
                # ThreadManager thread wrappers are expected to expose
                # is_alive(); if a future wrapper does not, the retained
                # handle still proves EnqueueTask accepted this task.
                return True

        task = self.ThreadManager.TDTask(
            target=ConvoyExt._workerLoop,
            args=(self._worker_queue, self._worker_shutdown,
                  self._worker_generation, self.WORKER_IDLE_S))
        thread = self.ThreadManager.EnqueueTask(task, standalone=True)
        if thread is None:
            return False
        self._worker_task = task
        self._worker_thread = thread
        return True

    def _runInWorker(self, fn):
        """Submit one callable to the long-lived standalone TDTask.

        Isolated in one method so the in-TD suite can execute the worker
        body synchronously against a stub client. Returns False instead of
        silently dropping work when ThreadManager is at capacity or the
        bounded queue is unexpectedly full.
        """
        if not self._ensureWorker():
            return False
        try:
            self._worker_queue.put_nowait(fn)
            return True
        except Full:
            return False

    def _runBatchInWorkers(self, client, context, request, progress,
                           complete, gate_event):
        """Fan one sibling batch out through bounded ThreadManager tasks.

        MAIN THREAD ONLY.  This method is the sole place the batch fanout
        touches ``ThreadManager``.  Child TDTasks receive only plain data,
        Queue/Event objects and the already-resolved client module; the
        existing long-lived worker performs the one local-host preflight,
        owns the cumulative deadline and publishes the terminal result.

        A start Event is deliberately held until that coordinator has been
        accepted.  If ThreadManager refuses the coordinator, already-created
        child tasks observe cancellation without issuing network traffic.
        """
        targets = list(request.get('targets') or ())
        if not targets:
            return False
        worker_count = min(
            len(targets), max(1, int(self.API_BATCH_WORKER_MAX)))
        work_queue = Queue(maxsize=len(targets))
        result_queue = Queue(maxsize=len(targets))
        start_event = Event()
        cancel_event = Event()
        shared = {'handle': None}
        deadline = time.monotonic() + float(request['timeout_s'])
        for index, target in enumerate(targets):
            work_queue.put_nowait((index, dict(target)))

        accepted = 0
        for _worker_index in range(worker_count):
            try:
                task = self.ThreadManager.TDTask(
                    target=_sibling_batch_target_worker,
                    args=(client, shared, context['convoy_id'],
                          context['controller_id'], request, work_queue,
                          result_queue, start_event, cancel_event,
                          gate_event, deadline, progress))
                thread = self.ThreadManager.EnqueueTask(
                    task, standalone=True)
            except Exception:
                thread = None
            if thread is not None:
                accepted += 1

        if accepted == 0:
            cancel_event.set()
            start_event.set()
            return False

        # Worker-thread closure: captures plain synchronization/data only.
        # In particular it does not capture this TD-bound extension object.
        def _coordinate():
            try:
                result = _coordinate_sibling_batch(
                    client, context, request, work_queue, result_queue,
                    start_event, cancel_event, gate_event, shared, deadline,
                    progress)
            except Exception as e:
                cancel_event.set()
                start_event.set()
                result = _sibling_worker_error(
                    'worker_exception', '%s: %s' % (type(e).__name__, e))
            complete(result)

        if not self._runInWorker(_coordinate):
            cancel_event.set()
            start_event.set()
            return False
        return True

    # ------------------------------------------------------------------
    # TouchDesigner-originated sibling API
    # ------------------------------------------------------------------

    def _initSiblingApi(self, generation):
        """Initialize bounded, generation-local sibling request state.

        Every object here is plain Python.  The two Queue instances are the
        only worker-to-main-thread handoff; callbacks and retained request
        records are never captured by a worker callable.
        """
        self._api_generation = int(generation)
        self._api_requests = OrderedDict()
        self._api_callbacks = {}
        self._api_completion_events = Queue(
            maxsize=self.API_COMPLETION_MAX)
        self._api_progress_events = Queue(maxsize=self.API_PROGRESS_MAX)
        self._api_gate_event = Event()
        self._api_poll_armed = False

    def _destroySiblingApi(self):
        """Forget callbacks/results and invalidate every old worker event."""
        self._api_generation = -1
        self._api_poll_armed = False
        try:
            self._api_gate_event.set()
        except Exception:
            pass
        try:
            self._api_callbacks.clear()
            self._api_requests.clear()
        except Exception:
            pass
        for queue_name in ('_api_completion_events',
                           '_api_progress_events'):
            queue = getattr(self, queue_name, None)
            if queue is None:
                continue
            while True:
                try:
                    queue.get_nowait()
                except Empty:
                    break

    @staticmethod
    def _apiError(reason, detail, **extra):
        out = {'state': 'error', 'ok': False,
               'reason': str(reason), 'detail': str(detail)}
        out.update(extra)
        return out

    @staticmethod
    def _apiText(value, name, limit=256):
        if (not isinstance(value, str) or not value
                or value != value.strip() or len(value) > limit
                or any(ord(char) < 32 or ord(char) == 127
                       for char in value)):
            raise ValueError(
                '%s must be bounded non-empty printable text' % (name,))
        return value

    @classmethod
    def _apiTimeout(cls, value):
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not 0.1 <= float(value) <= cls.API_TIMEOUT_MAX_S):
            raise ValueError('timeout_s must be within [0.1, %d]'
                             % int(cls.API_TIMEOUT_MAX_S))
        return float(value)

    @staticmethod
    def _apiPlain(value, max_bytes, name):
        """Return a detached JSON value or reject it before any worker I/O."""
        try:
            encoded = json.dumps(
                value, ensure_ascii=False, allow_nan=False,
                separators=(',', ':')).encode('utf-8')
        except (TypeError, ValueError, OverflowError, UnicodeError) as e:
            raise ValueError('%s must contain only JSON data (%s)'
                             % (name, type(e).__name__))
        if len(encoded) > int(max_bytes):
            raise ValueError('%s exceeds the %d-byte limit'
                             % (name, int(max_bytes)))
        try:
            return json.loads(encoded.decode('utf-8'))
        except (ValueError, UnicodeError):
            raise ValueError('%s could not be detached safely' % (name,))

    def _apiBoundResult(self, value):
        """Detach a worker result and replace oversized/broken values."""
        try:
            return self._apiPlain(
                value, self.API_RESULT_MAX_BYTES, 'result')
        except ValueError as e:
            reason = ('result_too_large' if 'byte limit' in str(e)
                      else 'invalid_worker_result')
            return self._apiError(reason, str(e))

    def _apiNewRequest(self, kind, callback):
        """Create a retained local handle, evicting terminal history only."""
        now = time.time()
        request_id = 'cr_' + secrets.token_hex(16)
        while request_id in self._api_requests:
            request_id = 'cr_' + secrets.token_hex(16)

        while len(self._api_requests) >= self.API_REQUEST_MAX:
            evicted = False
            for old_id, old in tuple(self._api_requests.items()):
                if old.get('state') in self.API_TERMINAL_REQUEST_STATES:
                    self._api_requests.pop(old_id, None)
                    self._api_callbacks.pop(old_id, None)
                    evicted = True
                    break
            if not evicted:
                # All retained slots are genuinely in flight.  Do not evict
                # a durable lookup handle merely to accept more work.
                result = self._apiError(
                    'request_capacity',
                    'the bounded Convoy request table is full')
                handle = {
                    'request_id': request_id, 'kind': kind,
                    'state': 'failed', 'created': now, 'updated': now,
                    'result': result,
                }
                if callable(callback):
                    try:
                        # Preserve the ordinary async contract even when no
                        # retained slot exists. run() invokes this callable
                        # on TD's main thread after the public method returns.
                        run('args[0]._apiCapacityCallback(args[1], args[2])',
                            self, callback, dict(handle, event='complete'),
                            delayFrames=1)
                    except Exception as e:
                        self._log('could not schedule sibling API capacity '
                                  'callback: %s' % (e,), 'DEBUG')
                return None, handle

        record = {
            'request_id': request_id,
            'kind': str(kind),
            'state': 'queued',
            'created': now,
            'updated': now,
            'source': None,
            'target': None,
            'result': None,
            'progress': None,
        }
        self._api_requests[request_id] = record
        if callable(callback):
            self._api_callbacks[request_id] = callback
        return record, self._apiRecordSnapshot(record)

    def _apiCapacityCallback(self, callback, snapshot):
        """Deliver an unretained capacity refusal on TD's main thread."""
        if self._staleInstance() or not callable(callback):
            return
        try:
            callback(dict(snapshot))
        except Exception as e:
            self._log('sibling API capacity callback failed: %s' % (e,),
                      'DEBUG')

    def _apiRecordSnapshot(self, record, event=None):
        names = ('request_id', 'kind', 'state', 'created', 'updated',
                 'source', 'target', 'result', 'progress')
        out = {name: record.get(name) for name in names
               if record.get(name) is not None}
        if event is not None:
            out['event'] = event
        try:
            return self._apiPlain(
                out, self.API_SNAPSHOT_MAX_BYTES, 'request snapshot')
        except ValueError as e:
            return self._apiError('invalid_request_snapshot', str(e))

    def _apiContext(self):
        """Resolve non-spoofable source identity on the TD main thread."""
        if not self._enabled():
            return None, self._apiError(
                'convoy_disabled', 'Convoy Enable is off')
        convoy_id = str(self._readConvoyId() or '')
        if not convoy_id:
            return None, self._apiError(
                'convoy_unbound', 'this project has no Convoy id')
        if self._readBindingState() != 'established':
            return None, self._apiError(
                'convoy_not_established',
                'the automatic Convoy realm has not converged yet')
        session = self._session()
        if not session.get('registered'):
            return None, self._apiError(
                'node_not_registered',
                'this Embody node is not registered with its host app')
        try:
            source_host_id = self._apiText(
                str(session.get('host_id') or ''), 'source_host_id')
            source_node_id = self._apiText(
                str(session.get('node_id') or ''), 'source_node_id')
            source_runtime_id = self._apiText(
                str(session.get('runtime_id') or ''), 'source_runtime_id')
            convoy_id = self._apiText(convoy_id, 'convoy_id', 128)
            controller_id = self._apiText(
                'td:%s:%s:%s' % (source_host_id, source_node_id,
                                  source_runtime_id),
                'controller_id')
        except ValueError as e:
            return None, self._apiError('invalid_source_identity', str(e))
        source = {
            'host_id': source_host_id,
            'node_id': source_node_id,
            'runtime_id': source_runtime_id,
            'convoy_id': convoy_id,
            'controller_id': controller_id,
        }
        # A prior disable sets the old generation's event so queued workers
        # fail before I/O. Re-registration under an enabled, established
        # realm starts a fresh submission epoch.
        if self._api_gate_event.is_set():
            self._api_gate_event = Event()
        return {'source': source, 'convoy_id': convoy_id,
                'controller_id': controller_id}, None

    def _revokeSiblingApi(self):
        """Prevent not-yet-started sibling workers from issuing any I/O."""
        try:
            self._api_gate_event.set()
        except Exception:
            pass

    @staticmethod
    def _apiTargetProjection(kind, request):
        if kind == 'batch':
            return {'targets': [
                {name: row.get(name) for name in (
                    'host_id', 'node_id', 'expected_runtime_id')
                 if row.get(name) is not None}
                for row in request.get('targets', [])]}
        if kind in ('call', 'ping'):
            return {name: request.get(name) for name in (
                'host_id', 'node_id', 'expected_runtime_id')
                    if request.get(name) is not None}
        if kind in ('get_job', 'cancel_job'):
            return {name: request.get(name) for name in (
                'host_id', 'delivery_id') if request.get(name) is not None}
        return None

    def listNodes(self, callback=None):
        """Asynchronously list this established Convoy; never wakes TD."""
        return self._submitSiblingApi('list_nodes', {}, callback=callback)

    def ping(self, host_id, node_id, expected_runtime_id=None,
             timeout_s=10.0, callback=None):
        """Asynchronously ping one exact node through the host plane."""
        error = None
        request = {}
        try:
            request = {
                'host_id': self._apiText(host_id, 'host_id'),
                'node_id': self._apiText(node_id, 'node_id'),
                'timeout_s': self._apiTimeout(timeout_s),
            }
            if expected_runtime_id is not None:
                request['expected_runtime_id'] = self._apiText(
                    expected_runtime_id, 'expected_runtime_id')
        except (TypeError, ValueError) as e:
            error = self._apiError('invalid_arguments', str(e))
        return self._submitSiblingApi(
            'ping', request, callback=callback, local_error=error)

    def call(self, host_id, node_id, operation, arguments=None,
             expected_runtime_id=None, timeout_s=30.0, wait=False,
             callback=None, idempotency_key=None):
        """Submit one exact sibling tool call and optionally await verdict.

        ``wait=False`` is the scalable default: the local request completes
        with the durable delivery id, which can be reconciled with getJob().
        ``wait=True`` occupies the one Convoy worker only until timeout_s.
        """
        error = None
        request = {}
        try:
            if type(wait) is not bool:
                raise ValueError('wait must be a boolean')
            if arguments is None:
                arguments = {}
            if not isinstance(arguments, dict):
                raise ValueError('arguments must be an object')
            request = {
                'host_id': self._apiText(host_id, 'host_id'),
                'node_id': self._apiText(node_id, 'node_id'),
                'operation': self._apiText(
                    operation, 'operation', 128),
                'arguments': self._apiPlain(
                    arguments, self.API_REQUEST_MAX_BYTES, 'arguments'),
                'timeout_s': self._apiTimeout(timeout_s),
                'wait': wait,
            }
            if expected_runtime_id is not None:
                request['expected_runtime_id'] = self._apiText(
                    expected_runtime_id, 'expected_runtime_id')
            if idempotency_key is not None:
                request['idempotency_key'] = self._apiText(
                    idempotency_key, 'idempotency_key')
        except (TypeError, ValueError) as e:
            error = self._apiError('invalid_arguments', str(e))
        return self._submitSiblingApi(
            'call', request, callback=callback, local_error=error)

    def batch(self, targets, operations, timeout_s=30.0, wait=False,
              callback=None):
        """Fan one Envoy batch to explicit targets (ordered, not atomic).

        Target I/O overlaps through a bounded ThreadManager fanout. The
        returned rows remain in input order, failures are per-target, and
        ``timeout_s`` is one cumulative deadline for the entire fanout.
        """
        error = None
        request = {}
        try:
            if type(wait) is not bool:
                raise ValueError('wait must be a boolean')
            if not isinstance(targets, (list, tuple)) or not targets:
                raise ValueError('targets must be a non-empty list')
            if len(targets) > self.API_BATCH_TARGET_MAX:
                raise ValueError('too many batch targets (maximum %d)'
                                 % self.API_BATCH_TARGET_MAX)
            if not isinstance(operations, (list, tuple)):
                raise ValueError('operations must be a list')
            if len(operations) > self.API_BATCH_OPERATION_MAX:
                raise ValueError('too many batch operations (maximum %d)'
                                 % self.API_BATCH_OPERATION_MAX)
            clean_targets = []
            for index, target in enumerate(targets):
                if not isinstance(target, dict):
                    raise ValueError('target %d must be an object' % index)
                unknown = set(target) - {
                    'host_id', 'node_id', 'expected_runtime_id'}
                if unknown:
                    raise ValueError('target %d has unknown fields' % index)
                clean = {
                    'host_id': self._apiText(
                        target.get('host_id'), 'target host_id'),
                    'node_id': self._apiText(
                        target.get('node_id'), 'target node_id'),
                }
                if target.get('expected_runtime_id') is not None:
                    clean['expected_runtime_id'] = self._apiText(
                        target.get('expected_runtime_id'),
                        'target expected_runtime_id')
                clean_targets.append(clean)
            clean_operations = []
            for index, child in enumerate(operations):
                if not isinstance(child, dict):
                    raise ValueError('operation %d must be an object' % index)
                if set(child) - {'tool', 'params'}:
                    raise ValueError('operation %d has unknown fields' % index)
                params = child.get('params', {})
                if not isinstance(params, dict):
                    raise ValueError(
                        'operation %d params must be an object' % index)
                clean_operations.append({
                    'tool': self._apiText(
                        child.get('tool'), 'operation tool', 128),
                    'params': params,
                })
            request = self._apiPlain({
                'targets': clean_targets,
                'operations': clean_operations,
                'timeout_s': self._apiTimeout(timeout_s),
                'wait': wait,
            }, self.API_REQUEST_MAX_BYTES, 'batch request')
        except (TypeError, ValueError) as e:
            error = self._apiError('invalid_arguments', str(e))
        return self._submitSiblingApi(
            'batch', request, callback=callback, local_error=error)

    def getJob(self, host_id, delivery_id, since=None, timeout_s=5.0,
               callback=None):
        """Asynchronously read a job from its exact owner; never wakes TD."""
        error = None
        request = {}
        try:
            request = {
                'host_id': self._apiText(host_id, 'host_id'),
                'delivery_id': self._apiText(
                    delivery_id, 'delivery_id'),
                'timeout_s': self._apiTimeout(timeout_s),
            }
            if since is not None:
                if (isinstance(since, bool)
                        or not isinstance(since, (int, float))
                        or not float('-inf') < float(since) < float('inf')):
                    raise ValueError('since must be a finite number')
                request['since'] = float(since)
        except (TypeError, ValueError) as e:
            error = self._apiError('invalid_arguments', str(e))
        return self._submitSiblingApi(
            'get_job', request, callback=callback, local_error=error)

    def cancelJob(self, host_id, delivery_id, timeout_s=5.0,
                  callback=None):
        """Request owner-routed cancellation; never wakes TouchDesigner."""
        error = None
        request = {}
        try:
            request = {
                'host_id': self._apiText(host_id, 'host_id'),
                'delivery_id': self._apiText(
                    delivery_id, 'delivery_id'),
                'timeout_s': self._apiTimeout(timeout_s),
            }
        except (TypeError, ValueError) as e:
            error = self._apiError('invalid_arguments', str(e))
        return self._submitSiblingApi(
            'cancel_job', request, callback=callback, local_error=error)

    def requestResult(self, request, consume=False):
        """Return a detached local request snapshot, or None if unknown."""
        request_id = (request.get('request_id')
                      if isinstance(request, dict) else request)
        if not isinstance(request_id, str):
            return None
        record = self._api_requests.get(request_id)
        if record is None:
            return None
        out = self._apiRecordSnapshot(record)
        if consume and record.get('state') in self.API_TERMINAL_REQUEST_STATES:
            self._api_requests.pop(request_id, None)
            self._api_callbacks.pop(request_id, None)
        return out

    def _submitSiblingApi(self, kind, request, callback=None,
                          local_error=None):
        """Validate/gate on main, then enqueue one pure worker callable."""
        if callback is not None and not callable(callback):
            local_error = self._apiError(
                'invalid_callback', 'callback must be callable or None')
            callback = None
        record, handle = self._apiNewRequest(kind, callback)
        if record is None:
            return handle

        request_id = record['request_id']
        generation = self._api_generation
        completion_queue = self._api_completion_events
        progress_queue = self._api_progress_events

        def _complete(result):
            event = {'generation': generation,
                     'request_id': request_id,
                     'result': result}
            try:
                completion_queue.put_nowait(event)
                return True
            except Full:
                # Invariant: there can be at most API_REQUEST_MAX retained
                # in-flight requests and each publishes exactly one terminal
                # event into an equally sized queue. Reaching this branch is
                # a programming fault; a short bounded wait still gives the
                # main-thread poll one chance to preserve the terminal result.
                try:
                    completion_queue.put(event, timeout=0.25)
                    return True
                except Full:
                    return False

        if local_error is not None:
            _complete(local_error)
            self._armApiPoll()
            return handle

        context, context_error = self._apiContext()
        if context_error is not None:
            _complete(context_error)
            self._armApiPoll()
            return handle

        # The retained projection contains identities only, never command
        # arguments, source code, shell text, tokens or a module reference.
        record['source'] = dict(context['source'])
        record['target'] = self._apiTargetProjection(kind, request)
        if kind in ('call', 'ping') and not request.get('idempotency_key'):
            request = dict(request)
            request['idempotency_key'] = request_id
        elif kind == 'batch':
            request = dict(request)
            request['idempotency_key_prefix'] = request_id
            request['result_budget_bytes'] = max(
                64 * 1024, self.API_RESULT_MAX_BYTES - 256 * 1024)

        client = self._safeClient()
        if client is None:
            _complete(self._apiError(
                'client_missing', 'the convoy_client module is unavailable'))
            self._armApiPoll()
            return handle

        # All values captured below are plain data, Queue instances, or the
        # convoy_client module resolved on the main thread.  In particular,
        # the callback table and this extension object are NOT captured.
        try:
            plain_context = self._apiPlain(
                context, self.API_REQUEST_MAX_BYTES, 'source context')
            plain_request = self._apiPlain(
                request, self.API_REQUEST_MAX_BYTES, 'request')
        except ValueError as e:
            _complete(self._apiError('invalid_arguments', str(e)))
            self._armApiPoll()
            return handle
        gate_event = self._api_gate_event
        progress_limit = int(self.API_PROGRESS_PER_REQUEST_MAX)
        progress_value_limit = int(self.API_PROGRESS_VALUE_MAX_BYTES)
        progress_tokens = Queue(maxsize=progress_limit)
        for _token_index in range(progress_limit):
            progress_tokens.put_nowait(None)

        def _progress(value):
            # Queue operations are synchronized: batch target TDTasks may
            # publish concurrently, so a list check/increment is not a hard
            # per-request bound.
            try:
                progress_tokens.get_nowait()
            except Empty:
                return
            value = _bound_sibling_worker_value(
                value, progress_value_limit, 'progress_result_too_large')
            event = {'generation': generation,
                     'request_id': request_id,
                     'result': value}
            try:
                progress_queue.put_nowait(event)
            except Full:
                # Progress is advisory; the dedicated completion queue keeps
                # the terminal durable answer lossless.
                pass

        def _worker():
            try:
                result = _run_sibling_api_request(
                    client, kind, plain_context, plain_request, _progress,
                    gate_event)
            except Exception as e:
                result = {
                    'state': 'error', 'ok': False,
                    'reason': 'worker_exception',
                    'detail': '%s: %s' % (type(e).__name__, e),
                }
            _complete(result)

        if kind == 'batch':
            started = self._runBatchInWorkers(
                client, plain_context, plain_request, _progress, _complete,
                gate_event)
        else:
            started = self._runInWorker(_worker)
        if not started:
            _complete(self._apiError(
                'thread_manager_unavailable',
                'ThreadManager could not start the Convoy worker or its '
                'bounded queue was full'))
        self._armApiPoll()
        return handle

    def _armApiPoll(self):
        """Arm at most one generation-tagged main-thread event drain."""
        if self._api_poll_armed or self._api_generation < 0:
            return
        self._api_poll_armed = True
        try:
            run('args[0]._pollApiEvents(args[1])',
                self, self._api_generation,
                delayFrames=self.API_POLL_FRAMES)
        except Exception as e:
            self._api_poll_armed = False
            self._log('could not arm sibling API result poll: %s' % (e,),
                      'DEBUG')

    def _apiHasPending(self):
        return any(record.get('state') not in
                   self.API_TERMINAL_REQUEST_STATES
                   for record in self._api_requests.values())

    @staticmethod
    def _apiResultFailed(result):
        if not isinstance(result, dict):
            return True
        if result.get('ok') is False:
            return True
        return str(result.get('state') or '') in (
            'error', 'host_error', 'unreachable', 'absent', 'stale',
            'refused', 'disabled', 'unsaved')

    def _apiInvokeCallback(self, request_id, record, event):
        callback = self._api_callbacks.get(request_id)
        if not callable(callback):
            return
        try:
            callback(self._apiRecordSnapshot(record, event=event))
        except Exception as e:
            self._log('sibling API callback failed: %s' % (e,), 'DEBUG')

    def _applyApiProgress(self, event):
        request_id = event.get('request_id')
        record = self._api_requests.get(request_id)
        if (record is None or record.get('state') in
                self.API_TERMINAL_REQUEST_STATES):
            return
        record['state'] = 'running'
        record['updated'] = time.time()
        record['progress'] = self._apiBoundResult(event.get('result'))
        self._apiInvokeCallback(request_id, record, 'progress')

    def _applyApiCompletion(self, event):
        request_id = event.get('request_id')
        record = self._api_requests.get(request_id)
        if (record is None or record.get('state') in
                self.API_TERMINAL_REQUEST_STATES):
            return
        result = self._apiBoundResult(event.get('result'))
        record['result'] = result
        record['progress'] = None
        record['state'] = ('failed' if self._apiResultFailed(result)
                           else 'completed')
        record['updated'] = time.time()
        self._apiInvokeCallback(request_id, record, 'complete')
        self._api_callbacks.pop(request_id, None)
        self._api_requests.move_to_end(request_id)

    def _pollApiEvents(self, generation):
        """Drain plain worker events and invoke callbacks on TD's main thread."""
        self._api_poll_armed = False
        if (self._staleInstance() or generation != self._api_generation
                or generation != self._worker_generation):
            return

        # Progress was published before completion. Drain its dedicated queue
        # first so a callback never observes "complete" and then "running".
        drained = 0
        while drained < self.API_EVENT_DRAIN_MAX:
            try:
                event = self._api_progress_events.get_nowait()
            except Empty:
                break
            drained += 1
            if event.get('generation') == generation:
                self._applyApiProgress(event)

        drained = 0
        while drained < self.API_EVENT_DRAIN_MAX:
            try:
                event = self._api_completion_events.get_nowait()
            except Empty:
                break
            drained += 1
            if event.get('generation') == generation:
                self._applyApiCompletion(event)

        if (self._apiHasPending()
                or not self._api_progress_events.empty()
                or not self._api_completion_events.empty()):
            self._armApiPoll()

    # ------------------------------------------------------------------
    # Host-private policy worker chain
    # ------------------------------------------------------------------

    def _beginPolicyCall(self, action, **request):
        """Run one local policy request off the TD main thread."""
        client = self._safeClient()
        if client is None or getattr(self, '_policy_busy', False):
            return False
        session = self._session()
        node_id = str(request.get('node_id') or session.get('node_id') or '')
        request = dict(request)
        request['node_id'] = node_id
        self._policy_busy = True
        self._policy_result = None
        self._policy_gen += 1
        gen = self._policy_gen

        def _worker():
            out = {'_gen': gen, '_action': action, 'request': request}
            try:
                probe = client.probe()
                if not probe.use_convoy:
                    result = {'state': probe.status, 'detail': probe.detail}
                else:
                    handle = probe.handle
                    if action == 'policy_refresh':
                        result = client.get_policy(
                            handle, node_id=node_id or None)
                    elif action == 'policy_begin':
                        current = client.get_policy(
                            handle, node_id=(node_id or None))
                        if current.get('state') != client.POLICY_RESULT:
                            result = current
                        else:
                            result = client.begin_policy_challenge(
                                handle, request.get('setting'),
                                current['policy']['generation'],
                                node_id=(node_id or None))
                    elif action == 'policy_disable':
                        result = client.disable_policy(
                            handle, request.get('setting'),
                            node_id=(node_id or None))
                    elif action == 'policy_quota':
                        current = client.get_policy(
                            handle, node_id=(node_id or None))
                        if current.get('state') != client.POLICY_RESULT:
                            result = current
                        else:
                            result = client.set_artifact_quota(
                                handle, request.get('quota_mb'),
                                current['policy']['generation'])
                    elif action == 'policy_confirm':
                        challenge = request.get('challenge') or {}
                        result = client.confirm_policy_challenge(
                            handle, challenge.get('challenge_id'),
                            challenge.get('confirmation'),
                            challenge.get('generation'))
                    elif action == 'policy_decline':
                        challenge = request.get('challenge') or {}
                        result = client.decline_policy_challenge(
                            handle, challenge.get('challenge_id'))
                    else:
                        result = {'state': 'error',
                                  'reason': 'unknown_policy_action'}
                out['result'] = result
            except Exception as e:
                out['result'] = {
                    'state': 'error',
                    'detail': '%s: %s' % (type(e).__name__, e),
                }
            self._policy_result = out

        if not self._runInWorker(_worker):
            self._policy_result = {
                '_gen': gen, '_action': action, 'request': request,
                'result': {
                    'state': 'error',
                    'reason': 'thread_manager_unavailable',
                    'detail': 'ThreadManager could not run the policy call',
                },
            }
        run('args[0]._pollPolicyCall(args[1], args[2])',
            self, gen, 0, delayFrames=self.POLL_FRAMES)
        return True

    def _pollPolicyCall(self, gen, attempts):
        if self._staleInstance():
            return
        out = self._policy_result
        if out is None or out.get('_gen') != gen:
            if attempts < self.POLL_ATTEMPTS:
                run('args[0]._pollPolicyCall(args[1], args[2])',
                    self, gen, attempts + 1, delayFrames=self.POLL_FRAMES)
            else:
                self._policy_busy = False
                self._finishPolicyCall(
                    'policy_timeout',
                    {'state': 'error', 'reason': 'policy_timeout'}, {})
            return
        self._policy_result = None
        self._policy_busy = False
        self._finishPolicyCall(
            out.get('_action'), out.get('result'), out.get('request') or {})

    def _finishPolicyCall(self, action, result, request):
        result = result if isinstance(result, dict) else {
            'state': 'error', 'detail': 'no policy result'}
        state = str(result.get('state') or '')
        client = self._safeClient()
        policy_state = str(getattr(client, 'POLICY_RESULT', 'policy'))
        if state == policy_state:
            projected = self._applyPolicyProjection(result)
            if not projected and action != 'policy_refresh':
                self._beginPolicyCall('policy_refresh')
            return
        if state == 'challenge':
            challenge = result.get('challenge') or {}
            setting = str(challenge.get('setting') or '')
            phrase = str(challenge.get('confirmation') or '')
            if setting == 'td_python':
                warning = (
                    'Allow arbitrary TouchDesigner Python on THIS node?\n\n'
                    'A remote Convoy controller could execute Python with '
                    'this TouchDesigner process\'s access. This can modify '
                    'the project, read files available to TD, or crash the '
                    'session.')
                label = 'Enable TD Python'
            else:
                warning = (
                    'Allow Full Shell on THIS computer?\n\nA remote Convoy '
                    'controller could run arbitrary operating-system '
                    'commands as your user. This can change or delete files, '
                    'install software, or expose secrets. Structured Git and '
                    'GitHub tools do not need this option.')
                label = 'Enable Full Shell'
            message = (warning + '\n\nOne-time local confirmation:\n' +
                       phrase + '\n\nThis approval is stored only by the '
                       'local Convoy host app.')
            if self._dialog('Embody - ' + label, message,
                            ['Cancel', label]) == 1:
                self._beginPolicyCall(
                    'policy_confirm', challenge=dict(challenge))
            else:
                self._beginPolicyCall(
                    'policy_decline', challenge=dict(challenge))
            return
        reason = str(result.get('reason') or result.get('detail') or state)
        if action not in ('policy_decline', 'policy_refresh'):
            self._log('local safety-policy request was not applied: %s'
                      % (reason,), 'WARNING')
        # Re-read after a conflict/refusal so every Par returns to the host's
        # current value rather than the user's speculative click.
        if action != 'policy_refresh' and not self._policy_busy:
            self._beginPolicyCall('policy_refresh')

    def _beginCall(self, action, client, session, state=None,
                   unregister_reason='disabled'):
        """Kick ONE bounded worker plus its poll chain. MAIN THREAD ONLY.

        Everything the worker touches is resolved here and handed over as
        plain data: the payload dict, the node id, the runtime id, and the
        convoy_client MODULE OBJECT itself (see _client). The worker returns
        plain data; the poll applies status, logs and schedule.
        """
        runtime_id = client.ensure_runtime_id(session, 'runtime_id')
        node_id = session.get('node_id')

        if action == 'unregister' and not node_id:
            # Nothing was ever registered from this process -- there is no
            # node to clear, and inventing one would be a lie to the host.
            session['registered'] = False
            session['sent'] = None
            session['next_call_at'] = None
            self._tick_ms = self.TICK_MAX_MS
            self._apply({'state': client.STATE_UNREGISTERED}, client)
            return

        payload = None
        if action == 'register':
            node_discriminator = client.stable_node_discriminator(
                state[6], platform=sys.platform)
            metadata = {
                'toe_path': state[6],
                'toe_name': os.path.basename(state[6]),
                'hostname': state[7],
                'process_id': state[8],
                'embody_version': state[9],
                'touchdesigner_version': state[10],
                'node_name': state[11],
            }
            payload = client.registration_payload(
                state[1], state[2], state[3], runtime_id,
                envoy_port=state[4],
                node_discriminator=node_discriminator,
                metadata=metadata,
                envoy_ready=bool(state[4]),
                remote_wake=bool(state[12]),
                perform_mode=bool(state[13]),
                wake_active=bool(state[14]),
                wake_port=state[15], wake_token=state[16],
                wake_grace_s=state[17], binding_state=state[18],
                td_executable=sys.executable,
                launch_token=os.environ.get(
                    'EMBODY_CONVOY_LAUNCH_TOKEN'),
                launch_reservation_id=os.environ.get(
                    'EMBODY_CONVOY_LAUNCH_RESERVATION'))
            session['pending_sent'] = state
            self._apply({'state': client.STATE_REGISTERING}, client)

        self._busy = True
        self._result = None
        self._gen += 1
        gen = self._gen

        def _worker():
            # ZERO TD access in here: plain Python and the captured module.
            out = {'_gen': gen, '_action': action}
            try:
                probe = client.probe()
                if not probe.use_convoy:
                    # absent / stale -- the normal state of a machine with no
                    # host app. Report the probe's own vocabulary; status_text
                    # maps it.
                    out['result'] = {'state': probe.status,
                                     'detail': probe.detail}
                else:
                    out['host_id'] = probe.handle.host_id
                    if action == 'register':
                        out['result'] = client.register(probe.handle, payload)
                        if (isinstance(out['result'], dict)
                                and out['result'].get('state')
                                == client.STATE_REGISTERED):
                            # Directory aggregation is host-side and passive:
                            # it never wakes TD. Fetch it in the same bounded
                            # worker turn as the heartbeat so the UI needs no
                            # extra thread or busy poll.
                            network_id = (out['result'].get('convoy_id')
                                          or state[3])
                            out['result']['_network_nodes'] = \
                                client.network_nodes(probe.handle, network_id)
                    else:
                        out['result'] = client.unregister(
                            probe.handle, node_id, runtime_id=runtime_id,
                            reason=unregister_reason)
            except Exception as e:
                # convoy_client is written never to raise; if it ever does,
                # the worker must still publish something or the poll spins
                # to its cap.
                out['result'] = {'state': 'error',
                                 'detail': '%s: %s' % (type(e).__name__, e)}
            self._result = out

        if not self._runInWorker(_worker):
            self._result = {
                '_gen': gen, '_action': action,
                'result': {
                    'state': 'error',
                    'reason': 'thread_manager_unavailable',
                    'detail': 'ThreadManager could not start the Convoy '
                              'worker or its bounded queue was full',
                },
            }
        self._tick_ms = self.TICK_MIN_MS
        run('args[0]._pollCall(args[1], args[2], args[3])',
            self, action, gen, 0, delayFrames=self.POLL_FRAMES)

    def _staleInstance(self):
        try:
            return self.ownerComp.ext.ConvoyExt is not self
        except Exception:
            return True

    def _pollCall(self, action, gen, attempts):
        """Drain the worker slot. MAIN THREAD ONLY."""
        if self._staleInstance():
            return
        out = self._result
        # Only accept the result from THIS call's worker generation.
        if out is None or out.get('_gen') != gen:
            if attempts < self.POLL_ATTEMPTS:
                run('args[0]._pollCall(args[1], args[2], args[3])',
                    self, action, gen, attempts + 1,
                    delayFrames=self.POLL_FRAMES)
            else:
                self._busy = False
                self._finish(action, {
                    'state': 'error',
                    'detail': 'the %s call timed out' % (action,)}, None)
            return
        self._result = None
        self._busy = False
        self._finish(action, out.get('result'), out.get('host_id'))

    def _finish(self, action, result, host_id):
        """Apply one call's outcome: session, schedule, status, log."""
        session = self._session()
        client = self._safeClient()
        if not isinstance(result, dict):
            result = {'state': 'error', 'detail': 'no result'}
        state = str(result.get('state') or '')
        now = time.monotonic()
        if host_id:
            session['host_id'] = str(host_id)

        if action == 'unregister':
            session['registered'] = False
            session['sent'] = None
            session['pending_sent'] = None
            session['next_call_at'] = None
            self._tick_ms = self.TICK_MAX_MS
            if not self._enabled():
                # The resting readout after a disable is 'Disabled' whatever
                # the host said: this node IS off locally even when the host
                # app could not be reached to hear about it.
                if state not in ('unregistered',):
                    self._log('unregister on disable reported %r -- the node '
                              'is off locally regardless' % (state,), 'DEBUG')
                self._apply({'state': 'unregistered'}, client)
                self._projectNodeRows([], 'Convoy is disabled')
            else:
                self._apply(result, client)
            return

        realm_changed = False
        if client is not None and state == client.STATE_REGISTERED:
            authoritative_id = str(result.get('convoy_id') or '')
            authoritative_state = str(result.get('realm_state') or '')
            current_id = self._readConvoyId()
            current_state = self._readBindingState()
            if (authoritative_id and authoritative_state
                    in ('candidate', 'established')
                    and (authoritative_id != current_id
                         or authoritative_state != current_state)):
                try:
                    adopted = self._embody.ext.Embody._adoptConvoyId(
                        authoritative_id, current_id, authoritative_state)
                except Exception as e:
                    adopted = ''
                    self._log('automatic Convoy realm adoption failed: %s'
                              % (e,), 'WARNING')
                if adopted:
                    realm_changed = True
                    self._publishId(adopted)
                    self._log('automatic Convoy realm is now %s (%s)'
                              % (adopted, authoritative_state), 'INFO')
                else:
                    result = {
                        'state': getattr(client, 'STATE_HOST_ERROR',
                                         'host_error'),
                        'reason': 'project_rebind_failed',
                        'detail': 'the host selected an automatic Convoy '
                                  'realm, but .embody/project.json could not '
                                  'be updated safely',
                    }
                    state = str(result['state'])

        registered = client is not None and state == client.STATE_REGISTERED
        if registered:
            session['registered'] = True
            session['node_id'] = (result.get('node_id')
                                  or session.get('node_id'))
            if result.get('host_id'):
                session['host_id'] = str(result['host_id'])
            pending = session.get('pending_sent')
            if pending is not None and not realm_changed:
                # Promote the observed host identity into the sent tuple so a
                # successful call converges instead of oscillating.
                sent = list(pending)
                sent[5] = str(session.get('host_id') or '')
                session['sent'] = tuple(sent)
            session['fails'] = 0
            # Registered but portless is NOT steady: Envoy binds seconds
            # after open, and the host cannot dispatch back until it knows
            # the port. Keep converging until it does.
            session['next_call_at'] = (now if realm_changed else now + (
                self.HEARTBEAT_S
                if (result.get('envoy_port')
                    or result.get('perform_mode'))
                else self.CONVERGING_S))
            self._applyPolicyProjection(result)
            self._applyNetworkNodes(result.get('_network_nodes'))
        else:
            session['registered'] = False
            session['sent'] = None
            if state in ('absent', 'stale', 'refused'):
                # Absence is normal and a policy refusal is a decision:
                # neither is retried hard.
                session['fails'] = 0
                session['next_call_at'] = now + self.ABSENT_S
            else:
                # unreachable / host_error / error: a transport or host-side
                # fault, jittered 5 s -> 60 s so a fleet recovering from one
                # host restart does not retry in lockstep.
                fails = int(session.get('fails') or 0)
                session['fails'] = fails + 1
                try:
                    delay = float(client.backoff_delay(fails))
                except Exception:
                    delay = self.ABSENT_S
                session['next_call_at'] = now + delay
        session['pending_sent'] = None
        self._tick_ms = self._scheduleFrom(session)
        self._apply(result, client)

    # ==================================================================
    # Status + logging
    # ==================================================================

    def _apply(self, result, client):
        """Write the status readout and log the transition (if any)."""
        if client is not None:
            try:
                text = client.status_text(result)
            except Exception:
                text = 'Error: unreadable result'
        else:
            text = 'Error: convoy_client module missing'
        if self._performing():
            # Perform Mode may have started while a call was in flight. Keep
            # the session bookkeeping, leave the show's readout alone.
            return
        self._status(text)
        self._logTransition(result, text)

    def _logTransition(self, result, text):
        """One line per status CLASS change. Steady state is silent.

        A heartbeat that keeps reporting 'registered' logs nothing; only
        the move INTO a class speaks. Failures carry their detail, because
        'No Convoy host app' alone would not say which of the two very
        different things just happened.
        """
        state = str((result or {}).get('state') or '')
        if state in self._QUIET_STATES:
            # 'Registering...' is a status readout, not a log line -- and it
            # must NOT reset the transition memory. Every heartbeat and every
            # retry passes through it, so resetting here would re-log the
            # state it lands back in, once per cycle, forever.
            return
        if state == self._logged:
            return
        self._logged = state
        result = result or {}
        if state in self._WARN_STATES:
            detail = str(result.get('detail') or result.get('reason') or '')
            self._log('%s -- %s' % (text, detail) if detail else text,
                      'WARNING')
            return
        # 'registered' is the recovery line as much as the first-success
        # line, so it is INFO either way; everything else (absence, the
        # resting states) stays at DEBUG.
        self._log(text, 'INFO' if state == 'registered' else 'DEBUG')

    def _logOnce(self, key, msg, level='INFO'):
        """Log `msg` only when `key` differs from the last one-shot key."""
        if self._logged == key:
            return
        self._logged = key
        self._log(msg, level)

    # ==================================================================
    # Host app: context, worker chain, readout
    # ==================================================================

    def _hostContext(self):
        """Everything a host worker needs, resolved on the MAIN THREAD.

        The ENTIRE main-thread surface of the host-app feature is this
        method. Both module objects (never a `mod.` lookup from a
        thread), the per-user data dir, this Embody's version (a Par
        read), the home dir and the POSIX uid are all lifted into a plain
        dict here; every worker below closes over that dict and touches
        nothing else. Raises if the modules are missing -- callers use
        _safeHostContext.
        """
        client = self._client()
        installer = self._installer()
        try:
            version = str(self._embody.par.Version.eval() or '')
        except Exception:
            version = ''
        try:
            project_root = str(self._embody.ext.Embody._findProjectRoot())
        except Exception:
            project_root = str(project.folder)
        return {
            'client': client,
            'installer': installer,
            'platform': sys.platform,
            'data_dir': client.data_dir(),
            'version': version,
            'home': os.path.expanduser('~'),
            'uid': (os.getuid() if hasattr(os, 'getuid') else None),
            'installed_by': '%s (%s)' % (project_root, self._embody.path),
            'health_wait_s': self.HEALTH_WAIT_S,
            'health_poll_s': self.HEALTH_POLL_S,
            # Embody's own uv-managed venv python -- the interpreter the rest
            # of Embody's Python runs under. It already carries the Convoy
            # crypto floor (Ed25519/X.509/TLS 1.3), so the host app runs under
            # it when no signed managed runtime is installed. Resolved on the
            # main thread here; None if the venv is not built or is missing.
            'venv_python': self._convoyVenvPython(),
        }

    def _convoyVenvPython(self):
        """Path to Embody's uv-managed venv python, or None. MAIN THREAD.

        Reads project.folder via EmbodyExt._venvPaths (a main-thread global),
        so it is resolved here into the plain host context and never touched
        from a worker.
        """
        try:
            path = self._embody.ext.Embody._venvPaths().get('venv_python')
        except Exception:
            return None
        try:
            if not path or not os.path.isfile(path):
                return None
            # WINDOWLESS on Windows. The host app is a background daemon
            # started by a Scheduled Task at login; launching it with
            # python.exe pops a console window that sits on the user's
            # desktop for the whole session. pythonw.exe is the same
            # interpreter with no console -- which is why the managed-runtime
            # manifest demanded it too ("Windows runtimes must use
            # pythonw.exe for the daemon and python.exe for the captured
            # capability probe").
            if sys.platform == 'win32':
                windowless = os.path.join(os.path.dirname(path),
                                          'pythonw.exe')
                if os.path.isfile(windowless):
                    return windowless
            return path
        except Exception:
            return None

    def _safeHostContext(self):
        """_hostContext() or None, saying WHICH module is missing."""
        try:
            return self._hostContext()
        except Exception as e:
            self._log('the Convoy host-app modules are not available in '
                      'this COMP (%s) -- reinstall Embody' % (e,), 'WARNING')
            self._hostStatus(self.HOST_INSTALL_FAILED)
            return None

    def _beginHostCall(self, action, fn, note=None):
        """Kick ONE bounded host worker plus its poll chain. MAIN THREAD.

        Identical in shape to _beginCall -- resolve on the main thread,
        hand the worker a closure over PLAIN DATA ONLY, publish a
        generation-tagged dict to a plain attribute, drain it from a
        bounded run() poll with a stale-instance guard -- on the separate
        slot described in __init__.

        `fn` must be worker-safe: no operator, no parameter, no DAT
        content, no run(). Everything it needs comes from the context
        dict built above.
        """
        if note is not None:
            self._hostStatus(note)
        self._host_busy = True
        self._host_result = None
        self._host_gen += 1
        gen = self._host_gen

        def _worker():
            # ZERO TD access in here.
            out = {'_gen': gen, '_action': action}
            try:
                out['result'] = fn()
            except Exception as e:
                # The installer is written never to raise; if it ever
                # does, the worker must still publish something or the
                # poll spins to its cap.
                out['result'] = {'ok': False, 'reason': 'worker_error',
                                 'detail': '%s: %s' % (type(e).__name__, e)}
            self._host_result = out

        if not self._runInWorker(_worker):
            self._host_result = {
                '_gen': gen, '_action': action,
                'result': {
                    'ok': False,
                    'reason': 'thread_manager_unavailable',
                    'detail': 'ThreadManager could not start the Convoy '
                              'worker or its bounded queue was full',
                },
            }
        run('args[0]._pollHostCall(args[1], args[2], args[3])',
            self, action, gen, 0, delayFrames=self.POLL_FRAMES)

    def _pollHostCall(self, action, gen, attempts):
        """Drain the host worker slot. MAIN THREAD ONLY."""
        if self._staleInstance():
            return
        out = self._host_result
        # Only accept the result from THIS call's worker generation.
        if out is None or out.get('_gen') != gen:
            if attempts < self.HOST_POLL_ATTEMPTS:
                run('args[0]._pollHostCall(args[1], args[2], args[3])',
                    self, action, gen, attempts + 1,
                    delayFrames=self.POLL_FRAMES)
            else:
                self._host_busy = False
                self._finishHost(action, {
                    'ok': False, 'reason': 'timed_out',
                    'detail': 'the host %s call timed out' % (action,)})
            return
        self._host_result = None
        self._host_busy = False
        self._finishHost(action, out.get('result'))

    def _finishHost(self, action, result):
        """Apply one host call's outcome: session, readout, log, next step."""
        if not isinstance(result, dict):
            result = {'ok': False, 'reason': 'no_result',
                      'detail': 'no result'}
        session = self._session()
        ok = bool(result.get('ok'))
        state = result.get('state')
        if isinstance(state, dict):
            session['host_state'] = state
        if result.get('plan') is not None:
            session['uninstall_preview'] = result.get('plan')

        detail = str(result.get('detail')
                     or result.get('reason') or '').strip()
        if not ok:
            if action == 'install':
                # The one action whose failure has its own word in the
                # vocabulary, because it is the one a user pulsed and is
                # waiting on.
                state = self.HOST_INSTALL_FAILED
            self._log('host %s failed: %s' % (action, detail or 'unknown'),
                      'WARNING')
        elif detail:
            self._log('host %s: %s' % (action, detail), 'DEBUG')

        if action == 'preview':
            # AN AUDIT MUST NEVER ALTER STATE -- and the readout is state.
            # Reporting a preview through Convoyhoststatus would overwrite
            # a live 'Running ...' with something the user did not ask to
            # change.
            self._logUninstallPreview(result.get('plan'))
            return

        if state is None:
            state = session.get('host_state')
        if state is None and not ok:
            # We have no idea what is on disk AND the call failed. The
            # vocabulary has exactly one string that points somewhere
            # useful, so that is what it gets.
            state = self.HOST_INSTALL_FAILED
        if state is not None:
            # A successful call that computed no state (an uninstall
            # preview) LEAVES THE READOUT ALONE rather than inventing
            # one -- 'Install failed' because we happened not to look is
            # the kind of lie this field exists to avoid.
            self._hostStatus(state)

        if action == 'uninstall_preview' and ok:
            # Stage two of the uninstall: the preview came back, so the
            # confirmation can now NAME what goes and COUNT what stays.
            self._confirmUninstall(result.get('plan'))

    def _hostStatus(self, state):
        """Record the host-app line and republish the merged Status.

        convoy_client still owns the words; there is simply no separate
        Host App parameter any more -- _publishStatus decides when this
        line outranks the node's own.
        """
        client = self._safeClient()
        try:
            text = client.host_status_text(state)
        except Exception:
            text = 'Install failed -- see log'
        self._host_status_text = str(text)[:160]
        self._publishStatus()

    def _restoreHostStatus(self):
        """Put the readout back to the last KNOWN state, or leave it be.

        Never invents one. A cancelled install must not write
        'Not installed' over a host app that IS installed (Install is
        also the repair path, so cancelling it is a common case), and it
        must not write 'Install failed' either -- nothing failed.
        """
        state = self._session().get('host_state')
        if state is not None:
            self._hostStatus(state)

    # ------------------------------------------------------------------
    # Uninstall safety and confirmation (MAIN THREAD)
    # ------------------------------------------------------------------

    @staticmethod
    def _normPath(path):
        text = str(path or '').replace('\\', '/').rstrip('/')
        return text.lower() if sys.platform == 'win32' else text

    def _uninstallTargetsRetained(self, plan):
        """Retained paths the removal list would touch. MUST come back [].

        A second lock on plan_host_uninstall's door, checked HERE because
        this is the last main-thread moment before a worker starts
        unlinking. host.json, host.token, host.portfile.json, audit.jsonl
        and jobs/ are permanent by design (16.4/A-15) and A-41 forbids
        uninstall as an evidence-destruction path -- so a preview that
        aimed at one of them is a refusal, not a warning to click past.

        Directory containment is checked too, not just exact equality:
        `jobs` is retained as a DIRECTORY, and a remove entry underneath
        it would destroy the same evidence without ever matching the
        retained path itself.
        """
        if not isinstance(plan, dict):
            return ['<no preview>']
        retained = [self._normPath(p) for p in (plan.get('retain') or [])]
        retained = [p for p in retained if p]
        targets = []
        for key in ('remove', 'remove_dirs'):
            targets.extend(self._normPath(p) for p in (plan.get(key) or []))
        hits = set()
        for target in targets:
            if not target:
                continue
            for keep in retained:
                if target == keep or target.startswith(keep + '/'):
                    hits.add(target)
        return sorted(hits)

    def _dialog(self, title, message, buttons):
        """Embody's message box, or -1 when it cannot be reached.

        -1 is the suppressed-dialog / unseeded-test default and every
        non-affirmative value means no, so an unreachable dialog can only
        ever DECLINE a system modification.
        """
        try:
            return self._embody.ext.Embody._messageBox(title, message,
                                                       buttons)
        except Exception as e:
            self._log('could not show the dialog (%s) -- treating it as '
                      'a decline' % (e,), 'WARNING')
            return -1

    def _logUninstallPreview(self, plan):
        """One INFO line summarising an audit. Writes nothing else."""
        if not isinstance(plan, dict):
            self._log('the uninstall preview could not be computed', 'WARNING')
            return
        self._log(
            'uninstall preview: %d files and %d directories would be '
            'removed; %d paths are retained and never touched (%d job '
            'records, %d indeterminate); %d incomplete and %d unrecognised '
            'paths would be left in place'
            % (len(plan.get('remove') or []),
               len(plan.get('remove_dirs') or []),
               len(plan.get('retain') or []),
               int(plan.get('jobs') or 0),
               int(plan.get('indeterminate') or 0),
               len(plan.get('incomplete') or []),
               len(plan.get('stray') or [])), 'INFO')

    def _confirmUninstall(self, plan):
        """Name what goes, COUNT what stays, then remove -- or don't."""
        unsafe = self._uninstallTargetsRetained(plan)
        if unsafe:
            self._log('REFUSING to uninstall: the plan would remove %d '
                      'retained path(s) that are permanent by design (%s)'
                      % (len(unsafe), ', '.join(unsafe[:3])), 'WARNING')
            self._restoreHostStatus()
            return
        ctx = self._safeHostContext()
        if ctx is None:
            return
        retained = plan.get('retain_present') or plan.get('retain') or []
        message = (
            'Remove the Convoy host app for this user?\n\n'
            'This removes:\n'
            '  - %d files and %d directories under\n'
            '    %s\n'
            '  - the %s that starts it when you log in\n\n'
            'This KEEPS, and never touches:\n'
            '%s\n'
            '  - %d job records (%d indeterminate)\n\n'
            'The job records are kept on purpose. An indeterminate record\n'
            'is permanent evidence that something may have run, and\n'
            'uninstall is never a way to destroy evidence. Deleting host\n'
            'state is a separate action -- and a re-install after it mints\n'
            'a NEW host id.'
            % (len(plan.get('remove') or []),
               len(plan.get('remove_dirs') or []),
               ctx['data_dir'],
               self._supervisorNoun(ctx['platform']),
               '\n'.join('  - %s' % (p,) for p in retained[:8])
               or '  - (nothing recorded yet)',
               int(plan.get('jobs') or 0),
               int(plan.get('indeterminate') or 0)))
        if self._dialog('Embody - Remove the Convoy host app', message,
                        ['Cancel', 'Remove']) != 1:
            self._log('uninstall cancelled -- nothing was removed', 'INFO')
            self._restoreHostStatus()
            return
        self._beginHostCall('uninstall',
                            lambda: _host_uninstall(ctx),
                            note=self.HOST_CHECKING)

    @staticmethod
    def _supervisorNoun(platform):
        return ('per-user Scheduled Task' if platform == 'win32'
                else 'per-user LaunchAgent')

    def _confirmInstall(self, ctx, interpreter, plan, modules):
        """The A-6 dialog. Every sentence in 1.6, none of them softened."""
        message = (
            'Install the Convoy host app for THIS user on THIS machine?\n\n'
            'What this does:\n'
            '  - writes %d small Python files to\n'
            '    %s\n'
            '  - registers a %s that starts the program when you log in\n'
            '    and restarts it within a minute\n'
            '  - IT RUNS WHENEVER YOU ARE LOGGED IN, WHETHER OR NOT\n'
            '    TOUCHDESIGNER IS OPEN\n'
            '  - runs it with Convoy\'s managed Python runtime:\n'
            '    %s\n\n'
            'Network behavior:\n'
            '  - while at least one local node has Enable Convoy On, the\n'
            '    app advertises on the trusted LAN and opens Convoy\'s\n'
            '    authenticated, encrypted peer listener on one selected\n'
            '    LAN interface. It never exposes Envoy itself.\n'
            '  - turning the final local Enable Convoy Off withdraws the\n'
            '    advertisement and closes the LAN listener.\n'
            '  - Embody does not modify your firewall. Windows or macOS may\n'
            '    ask you to allow local-network access; denying it leaves\n'
            '    local Embody/Envoy usable but remote nodes unreachable.\n'
            '  - do NOT enable Convoy on a guest, public, or otherwise\n'
            '    untrusted network. Automatic first-contact trust assumes\n'
            '    this LAN is already trusted.\n'
            '  - it never asks for administrator rights.\n\n'
            'Where the boundary really is:\n'
            '  - ANYTHING RUNNING AS YOUR USER ON THIS MACHINE CAN READ\n'
            '    ITS TOKEN AND SEND IT WORK. The token is a boundary\n'
            '    against OTHER users, not against you.\n'
            '  - it relays only operations in the audited registry, and\n'
            '    only into projects where you turned Convoy on.\n'
            '  - the managed runtime is pinned to this Embody release. Check\n'
            '    the release notes for its current signing/notarization and\n'
            '    platform-certification status before production use.\n'
            '  - it keeps a record of relayed jobs. Uninstall KEEPS that\n'
            '    record unless you separately ask for it to be deleted.\n\n'
            '%s'
            % (len(modules), ctx['data_dir'],
               self._supervisorNoun(ctx['platform']), interpreter,
               plan.get('detail') or ''))
        return self._dialog('Embody - Install the Convoy host app', message,
                            ['Cancel', 'Install']) == 1

    def _hostActionAllowed(self, what):
        """False (with a stated reason) when a host action must not run."""
        if self._performing():
            self._log('Perform Mode is on -- %s waits until it ends'
                      % (what,), 'INFO')
            return False
        if self._host_busy:
            self._log('another Convoy host action is still running -- '
                      '%s was ignored' % (what,), 'INFO')
            return False
        return True

    # ==================================================================
    # Promoted API: the host app
    # ==================================================================

    def HostStatus(self, refresh=True):
        """A plain-dict snapshot of the host app's state. Never raises.

        refresh=True (the default) also kicks ONE bounded worker to
        recompute it: the computation needs a /health round trip and a
        schtasks/launchctl spawn, and NEITHER may happen on the main
        thread. What comes back RIGHT NOW is the last computed answer;
        the readout updates when that worker's poll drains.
        """
        session = self._session()
        out = {'state': '', 'installed_version': '', 'supervisor': '',
               'live': False, 'pid': None, 'detail': '', 'busy': False,
               'status': ''}
        try:
            out['busy'] = bool(self._host_busy)
            state = session.get('host_state')
            if isinstance(state, dict):
                for key in ('state', 'installed_version', 'supervisor',
                            'live', 'pid', 'detail'):
                    out[key] = state.get(key)
            out['status'] = str(getattr(self, '_host_status_text', '') or '')
            if refresh and not self._host_busy and not self._performing():
                ctx = self._safeHostContext()
                if ctx is not None:
                    # 'Checking...' only when there is nothing to show
                    # yet; flashing it over a good 'Running 6.0.171
                    # (pid N)' on every refresh is pure flicker.
                    note = (None if isinstance(state, dict)
                            else self.HOST_CHECKING)
                    self._beginHostCall(
                        'status',
                        lambda: {'ok': True, 'state': _host_snapshot(ctx)},
                        note=note)
                    out['busy'] = True
        except Exception as e:
            out['error'] = '%s: %s' % (type(e).__name__, e)
        return out

    def InstallHost(self, confirm=True):
        """Install -- or REPAIR -- the Convoy host app for this user.

        This is the repair path as well as the first install, and that is
        why it re-runs a full install even when plan_install says the
        version is already current: writing the payload again, rewriting
        the launcher and re-registering the supervisor is exactly what
        fixes 'Needs repair -- managed runtime unavailable' (the runtime is
        re-resolved here) and 'Installed -- no supervisor'. Every one of
        those steps is idempotent by construction -- temp + os.replace,
        .complete written last, schtasks /Create /F rewriting the whole
        definition.

        The ONE refusal is a downgrade: a host app installed by a NEWER
        Embody is never replaced by an older project (A-36).

        Registering a program that runs at LOGIN is a different grant
        from enabling Convoy (A-13 covers minting an id and registering
        with a host app), so it gets its own confirmation naming exactly
        what is written, what runs, and where the trust boundary is.
        """
        if not self._hostActionAllowed('installing the host app'):
            return {'state': 'deferred'}
        ctx = self._safeHostContext()
        if ctx is None:
            return {'state': 'error', 'detail': 'installer module missing'}

        modules = self._hostModules()
        if not modules:
            self._log('the vendored host-app modules are missing from the '
                      "convoy COMP's `host` child -- this .tox cannot "
                      'install a host app; reinstall Embody', 'WARNING')
            self._hostStatus(self.HOST_INSTALL_FAILED)
            return {'state': 'error', 'detail': 'no vendored host modules'}

        installer = ctx['installer']
        installed = installer.read_installed(ctx['data_dir'], ctx['platform'])
        plan = installer.plan_install(installed, ctx['version'],
                                      ctx['platform'])
        if plan.get('action') == installer.ACTION_REFUSE_DOWNGRADE:
            self._log('install refused: %s' % (plan.get('detail'),), 'WARNING')
            # TWO DIFFERENT FAILURES ARRIVE AS refuse_downgrade, and they
            # need opposite words. `installed_version` cannot tell them
            # apart -- plan_install fills it in from the record either
            # way -- so ask the same question plan_install asked first:
            # is OUR version usable as a directory name at all? If not,
            # nothing can be written and this is not "a newer Embody owns
            # it", which would send the user hunting for a host app that
            # is not the problem.
            try:
                installer.safe_version(ctx['version'])
                ours_usable = True
            except Exception:
                ours_usable = False
            if ours_usable and plan.get('installed_version'):
                self._hostStatus({'state': installer.STATE_NEWER_INSTALL,
                                  'installed_version':
                                      plan.get('installed_version'),
                                  'live': bool((self._session().get(
                                      'host_state') or {}).get('live'))})
            else:
                self._hostStatus(self.HOST_INSTALL_FAILED)
            return {'state': 'refused', 'detail': plan.get('detail')}

        # Runtime resolution, resolved HERE so the dialog can name the exact
        # interpreter that will run at login and a machine with no usable
        # runtime refuses BEFORE the confirmation. Prefer a signed managed
        # runtime if one is installed; otherwise run the host app under
        # Embody's own uv-managed venv python -- the same interpreter the rest
        # of Embody's Python uses, which already carries the crypto floor. The
        # managed runtime stays the signed-release path; the venv is the
        # working default.
        interpreter = installer.choose_interpreter(
            installer.find_interpreters(ctx['platform']))
        venv_runtime = False
        if not interpreter:
            interpreter = ctx.get('venv_python')
            venv_runtime = bool(interpreter)
        if not interpreter:
            self._log('no Convoy runtime is available -- neither a signed '
                      'managed runtime nor an Embody venv python with the '
                      'crypto floor was found', 'WARNING')
            self._hostStatus(self.HOST_INSTALL_FAILED)
            return {'state': 'error', 'detail': 'no interpreter'}

        if confirm and not self._confirmInstall(ctx, interpreter, plan,
                                                modules):
            self._log('host app install cancelled -- nothing was written '
                      'and no task or agent was registered', 'INFO')
            self._restoreHostStatus()
            return {'state': 'declined'}

        # A-36's escape hatch: an install already marked external keeps
        # its own supervisor. Passing the kind through is what stops
        # install() defaulting to a Scheduled Task and creating a SECOND
        # supervisor for the same daemon.
        supervisor = (installer.SUPERVISOR_EXTERNAL
                      if plan.get('action') == installer.ACTION_EXTERNAL
                      else None)
        self._beginHostCall(
            'install',
            lambda: _host_install(ctx, modules, interpreter, supervisor,
                                  venv_runtime=venv_runtime),
            note=self.HOST_INSTALLING)
        return {'state': 'installing', 'action': plan.get('action'),
                'interpreter': interpreter, 'venv_runtime': venv_runtime,
                'modules': sorted(modules)}

    def StartHost(self):
        """Enable the supervisor and run it now, then wait for /health."""
        if not self._hostActionAllowed('starting the host app'):
            return {'state': 'deferred'}
        ctx = self._safeHostContext()
        if ctx is None:
            return {'state': 'error', 'detail': 'installer module missing'}
        installed = ctx['installer'].read_installed(ctx['data_dir'],
                                                    ctx['platform'])
        if not installed:
            self._log('there is no Convoy host app installed for this user '
                      '-- use Install first', 'INFO')
            self._hostStatus({'state': ctx['installer'].STATE_NOT_INSTALLED})
            return {'state': 'not_installed'}
        self._beginHostCall('start', lambda: _host_start(ctx),
                            note=self.HOST_STARTING)
        return {'state': 'starting'}

    def StopHost(self):
        """Stop the host app AND stop it coming back.

        The order lives in convoy_install.stop() and it is the whole
        point: ask the daemon to exit (so it clears its own portfile),
        wait for it to actually be gone, DISABLE the supervisor, and only
        then end it. Skipping the disable is what makes the Stop button
        look broken -- Windows' repetition trigger relaunches within a
        minute, macOS' KeepAlive within about one second. Nothing here
        may take a shortcut past stop() and kill the daemon directly.
        """
        if not self._hostActionAllowed('stopping the host app'):
            return {'state': 'deferred'}
        ctx = self._safeHostContext()
        if ctx is None:
            return {'state': 'error', 'detail': 'installer module missing'}
        self._beginHostCall('stop', lambda: _host_stop(ctx),
                            note=self.HOST_CHECKING)
        return {'state': 'stopping'}

    def PreviewHostUninstall(self):
        """AUDIT ONLY: what an uninstall would remove, and what it keeps.

        Removes nothing, registers nothing, prompts for nothing, and does
        not even move the Convoy Host readout -- an audit that altered
        state would not be an audit. The plan is computed in a worker
        (plan_host_uninstall lists the payload and reads every job record;
        on a busy host that is not main-thread work), logged as a summary,
        and stashed in the session.

        Returns the LAST computed preview plus busy=True while the fresh
        one is in flight -- the same shape ConvoyStatus uses, and the
        reason the confirmation path (UninstallHost) does not call this
        one: it needs the plan in hand before it can ask.
        """
        if not self._hostActionAllowed('previewing the uninstall'):
            return {'state': 'deferred'}
        ctx = self._safeHostContext()
        if ctx is None:
            return {'state': 'error', 'detail': 'installer module missing'}
        self._beginHostCall('preview', lambda: _host_preview(ctx))
        return {'state': 'previewing', 'busy': True,
                'preview': self._session().get('uninstall_preview')}

    def UninstallHost(self, confirm=True):
        """Remove the host app in two stages: preview, then confirm.

        Stage one computes the plan in a worker. Stage two runs on the
        main thread when it lands: refuse outright if the plan aims at
        anything retained, then show a confirmation that NAMES the
        retained paths and COUNTS the job records, and only then start
        the removal.

        host.json, host.token, host.portfile.json, audit.jsonl and jobs/
        are never removed. That is checked twice -- once inside
        plan_host_uninstall, once again in _uninstallTargetsRetained
        right before the confirmation -- because A-41 forbids uninstall
        as an evidence-destruction path and a single check is not a
        guarantee.
        """
        if not self._hostActionAllowed('uninstalling the host app'):
            return {'state': 'deferred'}
        ctx = self._safeHostContext()
        if ctx is None:
            return {'state': 'error', 'detail': 'installer module missing'}
        if not confirm:
            # Only the in-TD suite and a scripted repair take this path;
            # a pulse always confirms.
            self._beginHostCall('uninstall', lambda: _host_uninstall(ctx),
                                note=self.HOST_CHECKING)
            return {'state': 'uninstalling'}
        self._beginHostCall('uninstall_preview', lambda: _host_preview(ctx),
                            note=self.HOST_CHECKING)
        return {'state': 'previewing'}

    # ==================================================================
    # Consent (A-13): the first EXPLICIT enable
    # ==================================================================

    # Install-level consent marker, beside the per-user Convoy state. The
    # trusted-LAN explanation is answered ONCE per install -- not once per
    # project -- because it describes what Convoy does on THIS MACHINE, and
    # re-asking on every new project is noise the user has already read.
    CONSENT_MARKER = 'consent.json'

    def _installConsentPath(self):
        try:
            return os.path.join(self._client().data_dir(), self.CONSENT_MARKER)
        except Exception:
            return None

    def _installConsentGiven(self):
        """Has this install already accepted the trusted-LAN explanation?"""
        path = self._installConsentPath()
        if not path:
            return False
        try:
            with open(path, 'r', encoding='utf-8') as handle:
                return str(json.load(handle).get('scope') or '') == \
                    self.CONSENT_SCOPE
        except Exception:
            return False

    def RecordInstallConsent(self):
        """Remember that the user accepted Convoy on this install.

        Called by the Setup Wizard's Convoy step and by the first-enable
        dialog. Both are the same grant; the wizard simply asks it in its own
        words, so raising the long modal afterwards would ask twice. Failure
        to persist is not fatal -- worst case the dialog appears once more.
        """
        path = self._installConsentPath()
        if not path:
            return False
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'w', encoding='utf-8') as handle:
                json.dump({'scope': self.CONSENT_SCOPE,
                           'recorded_unix': time.time()}, handle)
            return True
        except Exception as e:
            self._log('could not record Convoy consent: %s' % (e,), 'DEBUG')
            return False

    def _ensureConsent(self):
        """True to proceed with registration; False when the user declined.

        TWO records, deliberately. The per-PROJECT entry in the committed
        .embody/project.json carries the convoy id and the granted scope --
        that is what a clone inherits. The per-INSTALL marker records that
        THIS USER, on THIS MACHINE, has read and accepted the trusted-LAN
        explanation. The modal is tied to the second: it fires the first time
        ever, and never again on this install (a new project silently mints
        its id). The Setup Wizard's Convoy step records the same marker, so a
        user who enabled Convoy there is never asked twice.

        It never fires on a tick or project open -- no modal during startup,
        ever. The provisional id is a genesis candidate; the host may later
        converge an uncommitted fresh LAN onto the established realm.
        """
        embody = self._embody.ext.Embody
        entry = {}
        try:
            entry = embody._readConvoyEntry() or {}
        except Exception as e:
            self._log('could not read .embody/project.json (%s) -- Convoy '
                      'stays off' % (e,), 'WARNING')
            self._setEnabled(False)
            return False
        existing_id = str(entry.get('id') or '')
        existing_scope = str(entry.get('consent_scope') or '')
        if existing_id and existing_scope == self.CONSENT_SCOPE:
            self._publishId(entry.get('id'))
            return True

        if not self._savedToe():
            self._log('save the project before enabling Convoy -- a node is '
                      'identified by its project folder, and an unsaved one '
                      'would mint a throwaway identity', 'WARNING')
            # SAY IT, do not just log it. This silently flipped the toggle
            # back off: the user enabled Convoy (in the wizard or on the
            # page), watched it turn itself off, and the only explanation was
            # a textport line they had no reason to have open (2026-08-03,
            # macOS, fresh drag-and-drop into an unsaved network).
            try:
                self._embody.ext.Embody._messageBox(
                    'Embody - Save the project first',
                    'Convoy could not be enabled because this project has '
                    'never been saved.\n\n'
                    'A Convoy node is identified by its project folder, so an '
                    'unsaved project would mint a throwaway identity that '
                    'disappears the moment you save.\n\n'
                    'Save the project, then turn Enable Convoy on again.',
                    ['OK'])
            except Exception:
                pass
            self._setEnabled(False)
            self._apply({'state': 'unsaved'}, self._safeClient())
            return False

        candidate = existing_id or embody._mintConvoyId()
        widening = bool(existing_id)

        # ALREADY ANSWERED ON THIS INSTALL -- do not ask again. The user has
        # read the trusted-LAN explanation once (here, or as the Setup
        # Wizard's Convoy step, which records the same marker). Asking per
        # PROJECT turned a one-time explanation into a recurring modal that
        # new users read as nagging. A new project silently mints its id and
        # records the same scope; the dangerous gates (TD Python, Full Shell)
        # are untouched by this and stay local, per-node and default-off.
        if self._installConsentGiven():
            try:
                recorded = embody._ensureConvoyId(
                    candidate, self.CONSENT_SCOPE,
                    ('established' if existing_id else 'candidate'))
            except Exception as e:
                recorded = None
                self._log('recording the convoy id failed: %s' % (e,),
                          'WARNING')
            if not recorded:
                self._log('could not record the convoy id in '
                          '.embody/project.json -- Convoy stays off', 'WARNING')
                self._setEnabled(False)
                self._apply({'state': 'disabled'}, self._safeClient())
                return False
            self._publishId(recorded)
            self._log('enabled for this project: convoy %s (consent already '
                      'given on this install)' % (recorded,), 'SUCCESS')
            return True

        choice = embody._messageBox(
            ('Embody - Upgrade Convoy Access' if widening
             else 'Embody - Enable Convoy'),
            ('Allow this Embody node to join the Convoy on this trusted '
             'LAN?\n\n'
             'Enabled Convoy nodes discover one another automatically. Any '
             'approved controller on a sibling node can inspect and control '
             'this TouchDesigner session through Convoy\'s audited tools.\n\n'
             'Do NOT enable Convoy on guest, public, or otherwise untrusted '
             'networks. Transport is authenticated and encrypted after first '
             'contact, but automatic first-contact trust assumes the LAN is '
             'already trusted.\n\n'
             + (('This upgrades the existing project grant from scope '
                 + repr(existing_scope or 'unknown') + '.\n')
                if widening else
                ('The node will automatically join the established LAN '
                 'Convoy or help establish one if none exists.\n'))
             + 'The non-secret binding and this consent are recorded in '
             '.embody/project.json, a committed file shared by project '
             'clones.\n\n'
             'Convoy needs a small background app to reach the LAN, so '
             'enabling installs and starts it for YOUR user account if it '
             'is not already there. It runs whenever you are logged in, '
             'whether or not TouchDesigner is open, so a node stays '
             'reachable while TD is closed. It never asks for '
             'administrator rights, and anything running as your user on '
             'this machine can talk to it. Uninstall it any time from the '
             'Convoy parameters.\n\n'
             'Allow Execute TD Python and Allow Full Shell remain separate, '
             'local, default-Off approvals. Turn Enable Convoy off at any '
             'time to withdraw this node.\n\n'
             'Scope granted: ' + self.CONSENT_SCOPE + '.'),
            ['Cancel', 'Enable Convoy'])
        if choice != 1:
            # -1 is the suppressed-dialog / unseeded-test default, and every
            # non-affirmative value means no (UpdaterExt._dialog's contract).
            # Declining a feature is not an error: INFO, no dialog, no status
            # beyond the resting one.
            self._log('Convoy enable cancelled -- no consent change was '
                      'written', 'INFO')
            self._setEnabled(False)
            self._apply({'state': 'disabled'}, self._safeClient())
            return False

        try:
            recorded = embody._ensureConvoyId(
                candidate, self.CONSENT_SCOPE,
                ('established' if existing_id else 'candidate'))
        except Exception as e:
            recorded = None
            self._log('recording the convoy id failed: %s' % (e,), 'WARNING')
        if not recorded:
            self._log('could not record the convoy id in '
                      '.embody/project.json -- Convoy stays off', 'WARNING')
            self._setEnabled(False)
            self._apply({'state': 'disabled'}, self._safeClient())
            return False

        self._publishId(recorded)
        # The one and only time this install asks.
        self.RecordInstallConsent()
        self._log('enabled for this project: convoy %s, consent scope %r '
                  '(recorded in .embody/project.json)'
                  % (recorded, self.CONSENT_SCOPE), 'SUCCESS')
        return True

    # ==================================================================
    # Promoted API
    # ==================================================================

    def Register(self):
        """Reconcile NOW. The explicit-enable entry point (parexec).

        A-13: the first explicit enable of a project with no convoy key in
        .embody/project.json shows a confirmation naming the id about to be
        minted and the scope it grants, and mints only on confirm. On cancel
        the toggle goes back off with an INFO line and no error.
        """
        if not self._enabled():
            self._log('Convoy is off -- turn Convoy Enable on to register',
                      'INFO')
            return {'state': 'disabled'}
        if not self._ensureConsent():
            return {'state': 'declined'}
        self._ensureHostApp()
        self._ensureWakeListener()
        self._reconcile(force=True)
        return self.ConvoyStatus()

    def _ensureHostApp(self):
        """Install and/or start the host app so ENABLING is the only step.

        Plan 8.1 step 2: enabling Convoy ensures the per-user host app is
        installed and running. Convoy cannot reach the LAN without it, so
        making the user find a second button afterwards was pure ceremony --
        the toggle already carries the consent (the enable dialog discloses
        the background app and its login persistence), which is why the
        install runs with confirm=False rather than raising a second prompt.

        Both calls are the existing bounded workers with their own poll
        chains, so this never blocks the main thread and never raises. The
        Install/Start/Stop/Uninstall pulses remain for repair, upgrade, and
        deliberate control.
        """
        try:
            if self._host_busy or self._performing():
                return
            ctx = self._safeHostContext()
            if ctx is None:
                return
            installed = ctx['installer'].read_installed(
                ctx['data_dir'], ctx['platform'])
            if not installed:
                self._log('Convoy enabled -- installing the host app it '
                          'needs to reach the LAN', 'INFO')
                self.InstallHost(confirm=False)
                return
            # Installed already: start it only if nothing is answering.
            probe = ctx['client'].probe(ctx['data_dir'])
            if getattr(probe, 'status', None) != ctx['client'].STATUS_RUNNING:
                self._log('Convoy enabled -- starting the installed host app',
                          'INFO')
                self.StartHost()
        except Exception as e:
            # A host-app problem must never block enabling; the Host App
            # readout and log carry the reason.
            self._log('could not ensure the Convoy host app: %s' % (e,),
                      'WARNING')

    def Unregister(self, blocking=False, reason='disabled'):
        """Best-effort: clear this node's Envoy port on the local host app.

        One attempt, a 1 s timeout, every outcome a value -- convoy_client's
        contract, because the callers are a disable and a shutting-down TD.
        ``disabled`` withdraws membership intent; ``shutdown`` (including
        the legacy local label ``TD exit``) clears only this runtime so the
        host can report the enabled node offline and accept its reconnect.
        Unknown intents fail closed before a request is sent.
        A hard kill still leaves a stale port; that is covered host-side by
        the dispatcher's UNREACHABLE backoff, and a true reaper is Phase 4.

        blocking=True runs the probe and the call INLINE on the main thread,
        for execute.py's onExit() where no run() callback will ever fire
        again. Cost when nothing was registered: zero (it returns below
        before touching the filesystem). Cost when a host app is live: a
        couple of milliseconds. Worst case, a live process holding the host
        port that answers nothing: the 3 s /health plus the 1 s unregister,
        on a TD that is already closing.
        """
        session = self._session()
        client = self._safeClient()
        # ``TD exit`` is the label used by the existing execute DAT. Fold it
        # at this local API boundary; convoy_client and the wire accept only
        # the two protocol values and fail closed on anything else.
        wire_reason = ('shutdown' if str(reason).strip().lower() == 'td exit'
                       else str(reason).strip().lower())
        allowed_reasons = tuple(getattr(
            client, 'UNREGISTER_REASONS', ('disabled', 'shutdown')))
        if wire_reason not in allowed_reasons:
            result = {
                'state': 'error',
                'reason': 'invalid_unregister_reason',
                'detail': 'reason must be one of: %s'
                          % (', '.join(allowed_reasons),),
            }
            self._log('unregister refused locally: %s' % result['detail'],
                      'WARNING')
            return result
        # The listener is useful only while membership is live.  Stopping it
        # is local and immediate; the authenticated unregister below clears
        # the host's transient endpoint record when available.
        self._revokeSiblingApi()
        self._stopWakeListener()
        if client is None or not session.get('node_id'):
            session['registered'] = False
            session['sent'] = None
            session['next_call_at'] = None
            if client is not None and not blocking:
                self._apply({'state': client.STATE_UNREGISTERED}, client)
            return {'state': 'unregistered', 'already_gone': True}

        if not blocking:
            self._beginCall('unregister', client, session,
                            unregister_reason=wire_reason)
            return {'state': 'unregistering'}

        result = {'state': 'error', 'detail': 'unregister did not run'}
        try:
            probe = client.probe()
            if probe.use_convoy:
                result = client.unregister(
                    probe.handle, session.get('node_id'),
                    runtime_id=session.get('runtime_id'),
                    reason=wire_reason)
            else:
                result = {'state': probe.status, 'detail': probe.detail}
        except Exception as e:
            result = {'state': 'error',
                      'detail': '%s: %s' % (type(e).__name__, e)}
        session['registered'] = False
        session['sent'] = None
        session['next_call_at'] = None
        try:
            self._log('unregistered on %s: %s'
                      % (reason, client.status_text(result)), 'DEBUG')
        except Exception:
            pass
        return result

    def ConvoyStatus(self):
        """A plain-dict snapshot of this node's Convoy state. Never raises."""
        out = {'enabled': False, 'performing': False,
               'perform_mode_requested': False, 'wake_active': False,
               'remote_wake': False, 'wake_port': None,
               'saved_project': False,
               'convoy_id': '', 'node_id': '', 'host_id': '', 'runtime_id': '',
               'registered': False, 'envoy_port': None, 'busy': False,
               'api_pending': 0, 'api_results': 0,
               'status': ''}
        try:
            session = self._session()
            out.update({
                'enabled': self._enabled(),
                'performing': self._performing(),
                'perform_mode_requested': self._performRequested(),
                'wake_active': self._wakeActive(),
                'remote_wake': self._remoteWakeEnabled(),
                'wake_port': self._wakeEndpoint()[0],
                'saved_project': bool(self._savedToe()),
                'convoy_id': self._readConvoyId(),
                'node_id': str(session.get('node_id') or ''),
                'host_id': str(session.get('host_id') or ''),
                'runtime_id': str(session.get('runtime_id') or ''),
                'registered': bool(session.get('registered')),
                'envoy_port': self._envoyPort(),
                'busy': bool(self._busy),
                'api_pending': sum(
                    1 for record in self._api_requests.values()
                    if record.get('state') not in
                    self.API_TERMINAL_REQUEST_STATES),
                'api_results': len(self._api_requests),
            })
            par = getattr(self._embody.par, 'Convoystatus', None)
            if par is not None:
                out['status'] = str(par.eval())
        except Exception as e:
            out['error'] = '%s: %s' % (type(e).__name__, e)
        return out


# ======================================================================
# SIBLING API WORKER BODY -- WORKER THREAD, ZERO TD ACCESS
# ======================================================================

def _sibling_worker_error(reason, detail, **extra):
    out = {'state': 'error', 'ok': False,
           'reason': str(reason), 'detail': str(detail)}
    out.update(extra)
    return out


def _sibling_worker_result(value, wakes_touchdesigner):
    if not isinstance(value, dict):
        value = _sibling_worker_error(
            'invalid_worker_result', 'convoy_client returned no result')
    else:
        value = dict(value)
    value['wakes_touchdesigner'] = bool(wakes_touchdesigner)
    return value


def _bound_sibling_worker_value(value, max_bytes, reason):
    """Detach/bound one worker event before it can occupy a Queue slot."""
    try:
        encoded = json.dumps(
            value, ensure_ascii=False, allow_nan=False,
            separators=(',', ':')).encode('utf-8')
        if len(encoded) <= int(max_bytes):
            return json.loads(encoded.decode('utf-8'))
    except (TypeError, ValueError, OverflowError, UnicodeError):
        pass
    delivery_id = None
    if isinstance(value, dict):
        delivery_id = ((value.get('job') or {}).get('delivery_id')
                       if isinstance(value.get('job'), dict)
                       else value.get('delivery_id'))
    out = _sibling_worker_error(
        reason, 'the worker value exceeded its bounded handoff budget')
    if isinstance(delivery_id, str) and delivery_id:
        out['delivery_id'] = delivery_id
    return out


def _sibling_gate_revoked(gate_event):
    try:
        return gate_event is not None and gate_event.is_set()
    except Exception:
        return True


def _sibling_worker_preflight(client, context, gate_event):
    """Resolve and authenticate the local host once for a worker request."""
    if _sibling_gate_revoked(gate_event):
        return None, _sibling_worker_result({
            'state': 'disabled', 'ok': False,
            'reason': 'convoy_disabled',
            'detail': 'Convoy was disabled before this request started',
        }, False)
    probe = client.probe()
    if not probe.use_convoy:
        return None, _sibling_worker_result({
            'state': probe.status, 'ok': False,
            'reason': 'host_unavailable', 'detail': probe.detail,
        }, False)
    handle = probe.handle
    if _sibling_gate_revoked(gate_event):
        return None, _sibling_worker_result({
            'state': 'disabled', 'ok': False,
            'reason': 'convoy_disabled',
            'detail': 'Convoy was disabled before command submission',
        }, False)
    source = context['source']
    # Registration is the source-identity proof. If the local host app was
    # replaced between registration and this request, never issue a command
    # under the stale controller identity.
    if handle.host_id != source['host_id']:
        return None, _sibling_worker_result({
            'state': 'refused', 'ok': False,
            'reason': 'source_host_changed',
            'detail': 'the local host identity changed; wait for Convoy to '
                      're-register this node',
            'expected_host_id': source['host_id'],
            'actual_host_id': handle.host_id,
        }, False)
    return handle, None


def _sibling_batch_target_call(client, handle, convoy_id, controller_id,
                               request, index, target, deadline, progress,
                               gate_event, cancel_event=None):
    """Submit/wait one target under the batch's single absolute deadline."""
    if (_sibling_gate_revoked(gate_event)
            or (cancel_event is not None and cancel_event.is_set())):
        return {
            'state': 'disabled', 'ok': False,
            'reason': 'convoy_disabled',
            'detail': 'Convoy was disabled before this target was submitted',
        }
    remaining = deadline - time.monotonic()
    if remaining <= 0.0:
        return _sibling_worker_error(
            'batch_timeout',
            'the total batch timeout elapsed before this target was '
            'submitted')
    timeout_s = float(request['timeout_s'])
    submitted = client.submit_sibling_call(
        handle, target['host_id'], convoy_id, target['node_id'],
        controller_id, 'batch_operations',
        {'operations': request['operations']},
        expected_runtime_id=target.get('expected_runtime_id'),
        idempotency_key='%s:%d' % (
            request['idempotency_key_prefix'], index),
        timeout_s=max(0.001, min(timeout_s, remaining)))
    final = submitted
    if submitted.get('ok') and request.get('wait'):
        delivery_id = (submitted.get('job') or {}).get('delivery_id')
        remaining = deadline - time.monotonic()
        if delivery_id and remaining > 0.0:
            def _wait_progress(value):
                if callable(progress):
                    progress({
                        'state': 'batch_progress', 'ok': True,
                        'index': index, 'target': dict(target),
                        'result': _sibling_worker_result(value, True),
                        'wakes_touchdesigner': True,
                    })

            final = client.wait_sibling_job(
                handle, target['host_id'], convoy_id, delivery_id,
                initial=submitted, timeout_s=remaining,
                progress=_wait_progress)
        elif not delivery_id:
            final = _sibling_worker_error(
                'host_bad_response',
                'the accepted batch omitted its durable delivery id')
        else:
            final = _sibling_worker_error(
                'batch_timeout',
                'the total batch timeout elapsed before waiting')
    return final


def _sibling_batch_target_worker(client, shared, convoy_id, controller_id,
                                 request, work_queue, result_queue,
                                 start_event, cancel_event, gate_event,
                                 deadline, progress):
    """Short-lived ThreadManager TDTask; processes queued targets only."""
    # The coordinator sets this Event on every normal/refusal path. Polling
    # the gate and absolute deadline also retires the task if extension reinit
    # prevents a queued coordinator from ever starting.
    while not start_event.is_set():
        if cancel_event.is_set() or _sibling_gate_revoked(gate_event):
            return {'stopped': True}
        remaining = deadline - time.monotonic()
        if remaining <= 0.0:
            return {'stopped': True}
        start_event.wait(min(0.25, remaining))
    if cancel_event.is_set() or _sibling_gate_revoked(gate_event):
        return {'stopped': True}
    handle = shared.get('handle')
    if handle is None:
        return {'stopped': True}
    while not cancel_event.is_set():
        try:
            index, target = work_queue.get_nowait()
        except Empty:
            break
        try:
            try:
                value = _sibling_batch_target_call(
                    client, handle, convoy_id, controller_id, request,
                    index, target, deadline, progress, gate_event,
                    cancel_event)
            except Exception as e:
                value = _sibling_worker_error(
                    'worker_exception', '%s: %s' % (type(e).__name__, e))
            try:
                result_queue.put_nowait((index, dict(target), value))
            except Full:
                # Exactly one result is produced per bounded work item into
                # an equally-sized queue. This branch is defensive only.
                pass
        finally:
            work_queue.task_done()
    return {'stopped': True}


def _collect_sibling_batch_results(targets, request, result_queue,
                                   cancel_event, gate_event, deadline,
                                   progress):
    """Collect concurrent rows to one stable-order, size-bounded answer."""
    raw_results = {}
    while len(raw_results) < len(targets):
        if _sibling_gate_revoked(gate_event):
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0.0:
            break
        try:
            index, target, value = result_queue.get(timeout=remaining)
        except Empty:
            break
        try:
            if index in raw_results:
                continue
            raw_results[index] = value
            if callable(progress):
                wrapped = _sibling_worker_result(value, True)
                progress({
                    'state': 'batch_progress',
                    'ok': wrapped.get('ok') is not False,
                    'index': index, 'target': dict(target),
                    'result': wrapped,
                    'wakes_touchdesigner': True,
                })
        finally:
            result_queue.task_done()

    revoked = _sibling_gate_revoked(gate_event)
    cancel_event.set()
    result_budget = int(request.get('result_budget_bytes') or 0)
    result_bytes = 0
    results = []
    for index, target in enumerate(targets):
        if index in raw_results:
            value = raw_results[index]
        elif revoked:
            value = {
                'state': 'disabled', 'ok': False,
                'reason': 'convoy_disabled',
                'detail': 'Convoy was disabled before this target '
                          'completed',
            }
        else:
            value = _sibling_worker_error(
                'batch_timeout',
                'the total batch timeout elapsed before this target '
                'completed')
        row = {'index': index, 'target': dict(target),
               'result': _sibling_worker_result(value, True)}
        remaining_budget = max(0, result_budget - result_bytes)
        bounded = _bound_sibling_worker_value(
            row, remaining_budget, 'batch_result_budget_exceeded')
        if (isinstance(bounded, dict)
                and bounded.get('reason') == 'batch_result_budget_exceeded'):
            bounded = {
                'index': index, 'target': dict(target),
                'result': bounded,
            }
        try:
            size = len(json.dumps(
                bounded, ensure_ascii=False, allow_nan=False,
                separators=(',', ':')).encode('utf-8'))
        except (TypeError, ValueError, OverflowError, UnicodeError):
            bounded = {
                'index': index, 'target': dict(target),
                'result': _sibling_worker_error(
                    'invalid_batch_result',
                    'the target returned non-JSON data'),
            }
            size = 256
        result_bytes += size
        results.append(bounded)

    all_ok = (len(results) == len(targets)
              and all(row['result'].get('ok') is not False
                      for row in results))
    return {
        'state': 'batch', 'ok': all_ok,
        'atomic': False, 'partial': not all_ok,
        'count': len(results), 'target_count': len(targets),
        'results': results, 'wakes_touchdesigner': True,
    }


def _coordinate_sibling_batch(client, context, request, work_queue,
                              result_queue, start_event, cancel_event,
                              gate_event, shared, deadline, progress):
    """Long-lived ThreadManager worker: preflight, release, aggregate."""
    if deadline - time.monotonic() <= 0.0:
        cancel_event.set()
        start_event.set()
        return _collect_sibling_batch_results(
            request['targets'], request, result_queue, cancel_event,
            gate_event, deadline, progress)
    handle, error = _sibling_worker_preflight(client, context, gate_event)
    if error is not None:
        cancel_event.set()
        start_event.set()
        return error
    shared['handle'] = handle
    start_event.set()
    return _collect_sibling_batch_results(
        request['targets'], request, result_queue, cancel_event, gate_event,
        deadline, progress)


def _run_sibling_batch_inline(client, handle, context, request, progress,
                              gate_event):
    """Single-worker harness using the exact production target/collector."""
    targets = request['targets']
    work_queue = Queue(maxsize=len(targets))
    result_queue = Queue(maxsize=len(targets))
    start_event = Event()
    cancel_event = Event()
    shared = {'handle': handle}
    deadline = time.monotonic() + float(request['timeout_s'])
    for index, target in enumerate(targets):
        work_queue.put_nowait((index, dict(target)))
    start_event.set()
    _sibling_batch_target_worker(
        client, shared, context['convoy_id'], context['controller_id'],
        request, work_queue, result_queue, start_event, cancel_event,
        gate_event, deadline, progress)
    return _collect_sibling_batch_results(
        targets, request, result_queue, cancel_event, gate_event, deadline,
        progress)


def _run_sibling_api_request(client, kind, context, request, progress,
                             gate_event=None):
    """Execute one already-validated sibling request. WORKER THREAD ONLY.

    ``client`` was resolved from ``mod`` on the main thread. Everything else
    is detached JSON data or a plain callable that writes to a Queue.
    """
    handle, preflight_error = _sibling_worker_preflight(
        client, context, gate_event)
    if preflight_error is not None:
        return preflight_error

    convoy_id = context['convoy_id']
    controller_id = context['controller_id']

    if kind == 'list_nodes':
        return _sibling_worker_result(
            client.network_nodes(handle, convoy_id), False)

    if kind in ('call', 'ping'):
        operation = ('convoy_ping' if kind == 'ping'
                     else request['operation'])
        arguments = ({} if kind == 'ping' else request['arguments'])
        deadline = time.monotonic() + float(request['timeout_s'])
        result = client.submit_sibling_call(
            handle, request['host_id'], convoy_id, request['node_id'],
            controller_id, operation, arguments,
            expected_runtime_id=request.get('expected_runtime_id'),
            idempotency_key=request.get('idempotency_key'),
            timeout_s=request['timeout_s'])
        wakes = kind != 'ping'
        if not result.get('ok'):
            return _sibling_worker_result(result, wakes)
        should_wait = kind == 'ping' or bool(request.get('wait'))
        if not should_wait:
            return _sibling_worker_result(result, wakes)
        if callable(progress):
            progress(_sibling_worker_result(result, wakes))
        job = result.get('job') or {}
        delivery_id = job.get('delivery_id')
        if not delivery_id:
            return _sibling_worker_result(_sibling_worker_error(
                'host_bad_response',
                'the accepted call omitted its durable delivery id'), wakes)
        remaining = deadline - time.monotonic()
        if remaining < 0.1:
            timed_out = dict(result)
            timed_out['wait_timed_out'] = True
            timed_out['detail'] = (
                'the total request deadline elapsed after durable '
                'submission; reconcile the delivery with getJob()')
            return _sibling_worker_result(timed_out, wakes)
        waited = client.wait_sibling_job(
            handle, request['host_id'], convoy_id, delivery_id,
            initial=result, timeout_s=remaining,
            progress=(lambda value: progress(
                _sibling_worker_result(value, wakes))))
        return _sibling_worker_result(waited, wakes)

    if kind == 'get_job':
        return _sibling_worker_result(client.get_sibling_job(
            handle, request['host_id'], convoy_id,
            request['delivery_id'], since=request.get('since'),
            timeout=request['timeout_s']), False)

    if kind == 'cancel_job':
        return _sibling_worker_result(client.cancel_sibling_job(
            handle, request['host_id'], convoy_id,
            request['delivery_id'], timeout=request['timeout_s']), False)

    if kind == 'batch':
        return _run_sibling_batch_inline(
            client, handle, context, request, progress, gate_event)

    return _sibling_worker_result(_sibling_worker_error(
        'unknown_request_kind', 'unsupported sibling request kind'), False)


# ======================================================================
# HOST-APP WORKER BODIES -- WORKER THREAD, ZERO TD ACCESS
# ======================================================================
#
# Module-level on purpose. A bound method would carry `self`, and `self`
# is one attribute away from an operator, a parameter or a DAT -- the
# exact class of access td-python.md forbids off the main thread and the
# exact mistake that froze TD in the field. These take ONLY the plain
# context dict _hostContext built on the main thread (two captured module
# OBJECTS, plus strings and numbers), and they return plain dicts for
# _finishHost to apply. Nothing here may import td, schedule a frame
# callback, read a Par, or log -- the poll does all of that.
#
# Every one of them is total: convoy_install and convoy_client are
# written never to raise, and _beginHostCall wraps the call anyway,
# because a worker that dies leaves the poll spinning to its cap.


def _host_snapshot(ctx):
    """THE host status computation, assembled from its four inputs.

    convoy_install.host_state() owns the decision; this gathers what it
    decides on -- and every gather here is worker-only work: a /health
    round trip with a 3 s timeout, a schtasks/launchctl spawn, and a
    stat of the recorded interpreter.
    """
    installer = ctx['installer']
    client = ctx['client']
    data_dir = ctx['data_dir']
    platform = ctx['platform']

    installed = installer.read_installed(data_dir, platform)

    probe_status = None
    try:
        probe_status = client.probe(data_dir=data_dir).status
    except Exception:
        probe_status = None

    # The pid is the daemon's OWN, from the portfile it wrote -- read
    # through read_live_portfile so a dead writer can never be reported
    # as 'Running ... (pid N)'.
    pid = None
    try:
        live = client.read_live_portfile(data_dir)
        if live:
            pid = live.get('pid')
    except Exception:
        pid = None

    supervisor = None
    kind = (installed or {}).get('supervisor')
    if kind in (installer.SUPERVISOR_TASK, installer.SUPERVISOR_AGENT):
        try:
            code, out, err = installer.run_command(
                installer.supervisor_argv('status', platform, uid=ctx['uid']))
            supervisor = installer.parse_supervisor_status(platform, out, err,
                                                           code)
        except Exception:
            # Leave it None: host_state reads that as no_supervisor only
            # when the record does not claim one, and query_failed is the
            # honest reading when we genuinely did not find out.
            supervisor = None

    interpreter_exists = None
    interpreter = (installed or {}).get('interpreter')
    if interpreter:
        try:
            interpreter_exists = os.path.isfile(str(interpreter))
        except Exception:
            interpreter_exists = None

    return installer.host_state(installed=installed,
                                probe_status=probe_status,
                                supervisor=supervisor,
                                version=ctx['version'],
                                interpreter_exists=interpreter_exists,
                                pid=pid)


def _host_await_health(ctx, timeout_s, sleep=None, now=None):
    """Poll /health until the daemon answers, or the bound expires.

    THE TAIL THE PLAN CALLS FOR. A Scheduled Task started with
    `schtasks /Run` returns the moment it has launched, not when the
    daemon is serving; without this wait Install would report
    'Installed -- not running (restarts within a minute)' on a perfectly
    good install and the user would watch a blank minute. Bounded, so a
    host app that never comes up is reported as what it is.
    """
    client = ctx['client']
    sleep = sleep or time.sleep
    now = now or time.monotonic
    deadline = now() + max(0.0, float(timeout_s or 0.0))
    while True:
        try:
            if client.probe(data_dir=ctx['data_dir']).status == \
                    client.STATUS_RUNNING:
                return True
        except Exception:
            pass
        if now() >= deadline:
            return False
        sleep(ctx.get('health_poll_s') or 1.0)


def _host_shutdown(ctx):
    """The authenticated POST /shutdown, or a stated no-op.

    Handed to convoy_install.stop()/uninstall() as their `shutdown`
    callable: they cannot build it themselves because it needs a live
    probe result and the per-install token. A daemon that will not answer
    is not a failure here -- it is exactly why the supervisor stop that
    follows exists.
    """
    client = ctx['client']
    try:
        probe = client.probe(data_dir=ctx['data_dir'])
    except Exception as e:
        return {'ok': False, 'detail': '%s: %s' % (type(e).__name__, e)}
    if not probe.use_convoy:
        return {'ok': False,
                'detail': 'no host app answered (%s)' % (probe.status,)}
    status, answer = client.host_post(probe.handle, '/shutdown', {})
    if status is None:
        return {'ok': False, 'detail': 'the host app did not answer'}
    ok = isinstance(answer, dict) and answer.get('ok') is not False
    return {'ok': bool(ok), 'http_status': status, 'answer': answer}


def _host_is_running(ctx):
    """The liveness observer stop()/uninstall() wait on.

    read_live_portfile, never read_portfile: it verifies the writer pid,
    so a portfile left behind by a supervisor kill reads as gone instead
    of stranding the wait for its whole bound.
    """
    client = ctx['client']

    def observe():
        try:
            return client.read_live_portfile(ctx['data_dir']) is not None
        except Exception:
            return False

    return observe


def _host_install(ctx, modules, interpreter, supervisor=None,
                  venv_runtime=False):
    """Write the payload, register the supervisor, start it, wait.

    WORKER-SAFE. When ``venv_runtime`` is set the interpreter is Embody's own
    uv-managed venv python, not a signed managed-runtime bundle, so it is
    verified by the LIVE crypto-floor probe (Ed25519/X.509/TLS 1.3) rather
    than the managed-runtime receipt -- the default verifier deliberately
    refuses a .venv interpreter. probe_runtime spawns the interpreter in
    isolation and touches no TouchDesigner objects.
    """
    installer = ctx['installer']
    verifier = None
    if venv_runtime:
        def verifier(data_dir, interp, platform=None, architecture=None,
                     runner=None):
            return installer.probe_runtime(
                interp, platform, architecture, runner=runner)
    outcome = installer.install(
        ctx['data_dir'], ctx['version'], modules, interpreter,
        platform=ctx['platform'], home=ctx['home'], uid=ctx['uid'],
        installed_by=ctx['installed_by'], supervisor=supervisor,
        runtime_verifier=verifier)
    if not outcome.get('ok'):
        return {'ok': False, 'action': 'install', 'outcome': outcome,
                'reason': outcome.get('reason'),
                'detail': outcome.get('detail')}
    started = None
    healthy = None
    if outcome.get('registered'):
        # An external supervisor registered nothing, so there is nothing
        # here for us to start -- A-36's rule is never two supervisors,
        # and that includes never poking someone else's.
        started = installer.start(platform=ctx['platform'], uid=ctx['uid'],
                                  home=ctx['home'])
        healthy = _host_await_health(ctx, ctx.get('health_wait_s'))
    return {'ok': True, 'action': 'install', 'outcome': outcome,
            'started': started, 'healthy': healthy,
            'detail': 'installed %s under %s'
                      % (outcome.get('version'), interpreter),
            'state': _host_snapshot(ctx)}


def _host_start(ctx):
    installer = ctx['installer']
    outcome = installer.start(platform=ctx['platform'], uid=ctx['uid'],
                              home=ctx['home'])
    healthy = _host_await_health(ctx, ctx.get('health_wait_s'))
    return {'ok': bool(outcome.get('ok')), 'action': 'start',
            'outcome': outcome, 'healthy': healthy,
            'reason': outcome.get('reason'), 'detail': outcome.get('detail'),
            'state': _host_snapshot(ctx)}


def _host_stop(ctx):
    installer = ctx['installer']
    outcome = installer.stop(platform=ctx['platform'], uid=ctx['uid'],
                             shutdown=lambda: _host_shutdown(ctx),
                             is_running=_host_is_running(ctx))
    return {'ok': bool(outcome.get('ok')), 'action': 'stop',
            'outcome': outcome, 'reason': outcome.get('reason'),
            'detail': outcome.get('detail'),
            'state': _host_snapshot(ctx)}


def _host_preview(ctx):
    """plan_host_uninstall, and NOTHING else. Alters no state at all."""
    installer = ctx['installer']
    plan = installer.plan_host_uninstall(ctx['data_dir'], ctx['platform'],
                                         ctx['home'])
    return {'ok': True, 'action': 'preview', 'plan': plan}


def _host_uninstall(ctx):
    installer = ctx['installer']
    outcome = installer.uninstall(ctx['data_dir'], platform=ctx['platform'],
                                  uid=ctx['uid'], home=ctx['home'],
                                  shutdown=lambda: _host_shutdown(ctx),
                                  is_running=_host_is_running(ctx))
    return {'ok': bool(outcome.get('ok')), 'action': 'uninstall',
            'outcome': outcome, 'reason': outcome.get('reason'),
            'detail': outcome.get('detail'),
            'state': _host_snapshot(ctx)}
