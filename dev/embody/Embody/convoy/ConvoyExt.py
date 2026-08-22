"""
ConvoyExt -- this TouchDesigner session's Convoy node registration.

Hosted on the 'convoy' baseCOMP (like UpdaterExt on 'updater'): reinit is
per-COMP, and Convoy code inside EnvoyExt.py would restart the MCP server
on every iteration. No op.Embody.ext.Convoy; external callers use
op.Convoy.ext.ConvoyExt, internal code parent.Embody.op('convoy'). The
host COMP is reached context-free (self.ownerComp.parent.Embody) --
string-form run() callbacks can resolve with ROOT as context.

MECHANISM: one idempotent main-thread reconciler (_convoyTick), armed
from __init__, generation-guarded via ownerComp.store('_convoy_gen')
(a save's strip/restore arms one tick per reinit; only the newest
generation survives). Each tick computes a desired-state tuple on the
main thread and compares with the last tuple sent: unchanged + inside
the heartbeat window = no network call; else one bounded worker. Covers
register on Envoy start/project open/restart and late/restarted hosts.
The ~30s heartbeat is load-bearing: envoy_port/runtime_id are per-launch
and not persisted host-side -- a host restart drops the port and the
heartbeat heals it. ABSENCE IS NOT AN ERROR: no host app is the normal
state ('No Convoy host app', one DEBUG line, slow tick -- never a dialog).

THREADING: resolve on main thread -> daemon worker (pure urllib, zero TD
access) -> generation-tagged plain dict -> bounded run(delayFrames=15)
poll chain with stale-instance guard. Work runs on one lazy standalone
ThreadManager TDTask (batches fan out to short-lived TDTasks). Reinit
signals the old generation's Event/queue sentinel. The convoy_client
module itself is captured in a local by _beginCall BEFORE queueing --
`mod.` is a live DAT lookup, a TD access (see _client(), the only
reference in this file).

SESSION STATE: runtime_id/node_id live in a sys attribute keyed by COMP
path (_session()) -- instance attrs re-mint per Ctrl+S (invalidating
host-side preconditions), ownerComp.store() outlives the process via the
.toe. Same channel as sys._envoy_server_gen et al.
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
import traceback
from collections import OrderedDict
from queue import Empty, Full, Queue
from threading import Event, Thread

# A payload entry is a BARE FILENAME and nothing else. The accept-list
# is convoy_install._BARE_NAME_OK's, deliberately duplicated at the READ
# site rather than trusted from the write site: these names come off DAT
# parameters a user can edit, and write_payload would reject them anyway
# -- catching it here means a mis-named DAT is reported as a missing
# module instead of failing the whole install.
_BARE_MODULE_NAME = re.compile(r"^[A-Za-z0-9._+-]+\.py$")


# Distinct "not passed" marker for pre-resolved context fields: the
# venv resolution legitimately RETURNS None (broken or absent venv,
# never-saved project), and treating None as "not passed" made the
# resolve-once dedup re-resolve -- and re-warn -- in exactly the
# broken-venv case it existed for (panel finding, 2026-08-04).
_UNSET = object()


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

    # Deferred-challenge retry: while dialogs are suppressed (a save
    # window) the local confirmation re-arms instead of auto-declining.
    # 20 x 60 frames ~ 20 s at 60 fps -- far past the 120-frame post-save
    # suppression, bounded so a stuck flag still declines eventually.
    CHALLENGE_WAIT_FRAMES = 60
    CHALLENGE_WAIT_MAX = 20

    # Worker poll chain. Worst case in the worker is a 3 s /health plus a
    # 10 s /register; the budget is >= 3x that (160 x 15 frames ~= 40 s at
    # 60 fps), matching UpdaterExt's sizing rule.
    POLL_FRAMES = 15
    POLL_ATTEMPTS = 160

    # Host-app poll cap: the worst-case legitimate install (supervisor
    # spawns + graceful stop + candidate probes + venv repair + daemon
    # venv build + verifier re-probe + version settle/restart tail) sums
    # to ~830 s, so the cap is 3400 x 15 frames (~850 s at 60 fps). A
    # BOUND on a wedged worker, not a timer -- subprocess timeouts end
    # the work. Headroom is thin: lengthen this path and the cap must
    # rise with it, or Install reports timed_out over work that later
    # succeeds.
    HOST_POLL_ATTEMPTS = 3400

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

    # Wait for the restarted daemon to report the just-written version:
    # a supervisor respawn answers as the OUTGOING payload for ~1 s, and
    # one immediate read turned that into a permanent 'stale payload'
    # verdict (3 field logs). 4 x 2 s covers it; carried in ctx so tests
    # zero them.
    VERSION_SETTLE_ATTEMPTS = 5
    VERSION_SETTLE_S = 2.0

    # Mirrors convoy_client.HOST_* -- that module owns the vocabulary and
    # a test pins these five against it. They are the TRANSIENT states,
    # which convoy_install never computes because they describe what this
    # extension is doing rather than what is on disk.
    HOST_CHECKING = 'checking'
    HOST_INSTALLING = 'installing'
    HOST_REPAIRING = 'repairing'
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
        self._post_init_done = False
        self._logged = ''        # last logged status class (transitions only)
        self._tick_ms = self.TICK_MIN_MS
        self._network_rows_digest = None
        self._last_nodes_result = None
        # Rows a confirmed Forget removed from the sequence optimistically,
        # held only until the daemon's verdict lands (_restoreNodeRowsNow
        # puts back whatever was not actually forgotten, then clears this).
        self._optimistically_dropped = []
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
            # A host action in flight dies with this instance (its poll
            # chain is armed on THIS object), stranding the readout on
            # 'Installing...' forever -- any ConvoyExt.py save triggers
            # it (syncfile reinit). _restoreHostStatus puts back the last
            # KNOWN state and invents nothing.
            if self._host_busy:
                self._restoreHostStatus()
            self._result = None
            self._busy = False
            self._host_result = None
            self._host_busy = False
            self._policy_result = None
            self._policy_busy = False
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

    def _log(self, msg, level='INFO', details=None):
        try:
            if details:
                self._embody.Log('Convoy: %s' % (msg,), level, details)
            else:
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

    # Precedence: user-actionable node states (e.g. unsaved) outrank a
    # blocking host-app line, which outranks the registration line.
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

    # The subset of blocking host lines a COMPLETED registration
    # disproves: each claims the host app is absent or down, and the
    # register call just ran THROUGH the host app. Mid-flight lines
    # (Checking/Installing/starting) update themselves when their action
    # completes, and 'Needs repair' / 'Managed by another supervisor'
    # are not claims about whether the app is running -- both stay.
    _DISPROVEN_HOST_TEXTS = (
        'Not installed', 'Install failed', 'Installed -- not running',
        'Installed -- stopped', 'Installed -- no supervisor',
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
            # No age at all: never seen (or a pre-257 host that omits
            # ages). 'Unavailable' read like an error state (field
            # 2026-08-19); 'Never' says what is actually known.
            return 'Now' if online else 'Never'
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
            host = str(node.get('hostname') or '').strip()
            if host and host.lower() not in name.lower():
                # A traveled auto-stamp wears another machine's name
                # (.toe-travel leak: a whole fleet read 'TEC-A4D /
                # Render.36', 2026-08-19). The row's live hostname is the
                # ground truth that tells nodes apart.
                name = ('%s (%s)' % (host, name))[:512]
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
            # The raw node dicts (with node_id) back the readout's
            # synchronous edits: a confirmed Forget filters THIS cache
            # and re-projects in the same frame (_dropNodeRowsNow).
            self._last_nodes_result = result
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

    def _dropNodeRowsNow(self, node_ids):
        """Remove rows from the Convoy Nodes sequence THIS FRAME.

        The visual contract of a confirmed Forget is immediate: the
        block the user just dismissed leaves the sequence parameters in
        the same frame, with NO daemon round trip in the visual path --
        the row sticking around until the background reconciled it is
        exactly what read as a broken button (field feedback 2026-08-05,
        three rounds). The daemon apply runs in the background as
        reconciliation; a row it refuses to forget is put BACK
        synchronously by _restoreNodeRowsNow, with the reason on screen,
        rather than silently reappearing seconds later (field feedback
        2026-08-06: "it clears and after 2-3 sec they show up again").
        """
        cached = getattr(self, '_last_nodes_result', None)
        if not isinstance(cached, dict):
            return
        drop = {str(i) for i in (node_ids or ())}
        dropped = [n for n in (cached.get('nodes') or ())
                   if isinstance(n, dict)
                   and str(n.get('node_id') or '') in drop]
        kept = [n for n in (cached.get('nodes') or ())
                if isinstance(n, dict)
                and str(n.get('node_id') or '') not in drop]
        # Remember what was optimistically removed, so a daemon refusal
        # can restore exactly those rows without waiting for a fetch.
        self._optimistically_dropped = dropped
        filtered = dict(cached)
        filtered['nodes'] = kept
        # One path draws this readout: the same apply the register drain
        # uses recomputes rows, ages and digest, and re-caches, so a
        # second Forget in the same session filters the already-filtered
        # set.
        self._applyNetworkNodes(filtered)

    def _restoreNodeRowsNow(self, node_ids):
        """Put optimistically-dropped rows BACK, this frame. MAIN THREAD.

        Sync twin of _dropNodeRowsNow for daemon-refused forgets (else a
        refused row vanishes and reappears seconds later, unexplained).
        Invalidate _network_rows_digest first -- drop-then-restore
        round-trips the digest and the unchanged-digest suppression
        would swallow the restore. Set to (), not None: None means
        "never drawn" and would let a transient failure blank real rows.
        """
        restore = {str(i) for i in (node_ids or ())}
        cached = getattr(self, '_last_nodes_result', None)
        dropped = getattr(self, '_optimistically_dropped', None) or ()
        coming_back = [n for n in dropped
                       if isinstance(n, dict)
                       and str(n.get('node_id') or '') in restore]
        # One Forget owns this snapshot; release it either way so a later
        # run can never restore rows from a previous one.
        self._optimistically_dropped = []
        if not isinstance(cached, dict) or not coming_back:
            return
        present = {str(n.get('node_id') or '')
                   for n in (cached.get('nodes') or ()) if isinstance(n, dict)}
        merged = list(cached.get('nodes') or ())
        merged.extend(n for n in coming_back
                      if str(n.get('node_id') or '') not in present)
        restored = dict(cached)
        restored['nodes'] = merged
        self._network_rows_digest = ()
        self._applyNetworkNodes(restored)

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

    def _authoritativeCapabilityValues(self):
        """The host-truth projection for the three capability pars.

        Derived from the session's last authenticated policy; with none
        cached, the only truthful projection is the fail-closed default
        state (gates off, default quota).
        """
        policy = (self._session().get('policy') or {})
        return {
            'Convoyallowtdpython': 1 if policy.get('allow_td_python') else 0,
            'Convoyallowfullshell': 1 if policy.get('allow_full_shell') else 0,
            'Convoyartifactquota': int(policy.get('artifact_quota_mb', 1024)),
        }

    def _reconcileDangerGates(self, route=True):
        """Snap the capability pars to host policy; route deviations.

        The extension only ever WRITES authoritative values into these
        pars, so any observed deviation is someone else's write -- a
        click, a synthetic set, or a value whose parexec callback was
        deferred past a suppressed window or swallowed by one (TD defers
        onValueChange to the next cook, so a write-time guard flag can
        never mark self-writes; field 2026-08-20). Deviations are
        snapped immediately and, once _postInit has run, forwarded as
        the user's request: policy_begin to enable (host challenge +
        local confirmation), policy_disable to revoke, policy_quota to
        resize. Runs from the reconcile tick AND the parexec fast path;
        idempotent. route=False (startup) snaps without sending -- a
        loaded value is not a user request.
        """
        session = self._session()
        deviations = []
        for name, value in self._authoritativeCapabilityValues().items():
            par = getattr(self._embody.par, name, None)
            if par is None:
                continue
            try:
                observed = par.eval()
                if int(observed) == int(value):
                    continue
                par.val = value
            except Exception:
                continue
            deviations.append((name, observed))
        if not deviations:
            return {'ok': True, 'in_sync': True}
        reverted = [name for name, _ in deviations]
        if not route or not getattr(self, '_post_init_done', False):
            self._log('ignored unauthorized capability value(s): %s -- '
                      'the local host policy is authoritative'
                      % (', '.join(reverted),), 'WARNING')
            return {'ok': False, 'reason': 'not_routed', 'reverted': reverted}
        if self._policyBusyBlocked():
            self._log('another Convoy safety-policy request is still in '
                      'progress', 'INFO')
            return {'ok': False, 'reason': 'policy_busy',
                    'reverted': reverted}
        node_id = str(session.get('node_id') or '')
        blocked = None
        for name, observed in deviations:
            if name == 'Convoyallowtdpython':
                if not node_id:
                    self._log('Allow Execute TD Python waits until this '
                              'node has registered with the Convoy host app',
                              'WARNING')
                    blocked = blocked or 'node_not_registered'
                    continue
                self._beginPolicyCall(
                    'policy_begin' if observed else 'policy_disable',
                    setting='td_python', node_id=node_id)
            elif name == 'Convoyallowfullshell':
                self._beginPolicyCall(
                    'policy_begin' if observed else 'policy_disable',
                    setting='full_shell', node_id=node_id)
            else:
                try:
                    quota = int(observed)
                except (TypeError, ValueError, OverflowError):
                    blocked = blocked or 'invalid_quota'
                    continue
                if quota < 0 or quota > 1024 * 1024:
                    blocked = blocked or 'invalid_quota'
                    continue
                self._beginPolicyCall('policy_quota', quota_mb=quota)
            self._log('Convoy safety-policy change requested (%s); the '
                      'parameter shows the approved value until the host '
                      'accepts' % (name,), 'INFO')
            return {'ok': True, 'pending': True, 'requested': name,
                    'reverted': reverted}
        return {'ok': False, 'reason': blocked or 'not_routed',
                'reverted': reverted}

    def _resetUntrustedDangerProjections(self):
        """Startup snap: capability pars to host truth, never routing.

        A saved .toe/TDN/clone may arrive with a stale On, but a loaded
        value must never become authority. With no cached policy the snap
        is the fail-closed default state; after a mid-session reinit (the
        per-process session survives) it is that policy. Nothing is
        sent -- a baked value is not a user request.
        """
        return self._reconcileDangerGates(route=False)

    def _applyPolicyProjection(self, result):
        """Project one validated convoy_client policy result. MAIN THREAD.

        The policy is cached BEFORE the pars are written: each write
        queues a deferred parexec callback whose reconcile compares pars
        against the cached policy -- caching first makes those callbacks
        no-ops, and a partial write self-heals on the next tick instead
        of fighting the projection.
        """
        policy = (result or {}).get('policy')
        if not isinstance(policy, dict):
            return False
        required = ('generation', 'allow_td_python', 'allow_full_shell',
                    'artifact_quota_mb')
        if any(name not in policy for name in required):
            return False
        self._session()['policy'] = dict(policy)
        try:
            for name, value in self._authoritativeCapabilityValues().items():
                par = getattr(self._embody.par, name, None)
                if par is not None and par.eval() != value:
                    par.val = value
        except Exception as e:
            self._log('could not project host safety policy: %s' % (e,),
                      'DEBUG')
            return False
        return True

    def LocalDangerGateChanged(self, par_name, requested):
        """Parexec fast path: reconcile the capability pars NOW.

        `requested` is advisory (the par value at callback time). TD
        defers onValueChange to the next cook, so by now the change may
        be stale, coalesced, or one of the extension's own writes; the
        reconcile reads live values against cached policy and routes
        only true deviations, so none of that can mis-route.
        """
        if par_name not in ('Convoyallowtdpython', 'Convoyallowfullshell'):
            return {'ok': False, 'reason': 'unknown_capability'}
        return self._reconcileDangerGates()

    def LocalArtifactQuotaChanged(self, requested):
        """Parexec fast path for the quota par: the same reconcile."""
        return self._reconcileDangerGates()

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

    def _savedToe(self):
        """This project's .toe path, or None if it was never saved.

        Saved-ness is decided by EmbodyExt (_projectSavedOnDisk), the single
        authority: TD's 'NewProject[.N].toe' placeholder name means never
        saved, anything else means the user saved it somewhere. NOT by
        os.path.isfile(project.folder / project.name) -- that literal path is
        routinely absent on a perfectly saved project, because TD reports the
        NEXT incremental name after a save (Control.35.toe on disk,
        project.name Control.36.toe). That check refused to enable Convoy on
        a long-saved production project (field-reported 2026-08-19), and it
        is not project.modified / project.dirty either -- both of those
        proxies have failed here in opposite directions.

        Returns the resolved file when one is reachable (used for the node's
        display name), else the nominal path; None only when unsaved.
        """
        embody = None
        try:
            embody = self._embody.ext.Embody
        except Exception:
            embody = None
        try:
            nominal = os.path.join(str(project.folder), str(project.name))
        except Exception:
            nominal = ''
        if embody is None:
            # No extension to ask -- fall back to the file itself.
            return nominal if nominal and os.path.isfile(nominal) else None
        try:
            if not embody._projectSavedOnDisk():
                return None
        except Exception:
            return None
        try:
            resolved = embody._resolveProjectToe()
        except Exception:
            resolved = None
        return resolved or nominal or None

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

    def _kickTick(self):
        """Fire a reconcile pass NOW, superseding the armed tick chain.

        Shortening _tick_ms cannot accelerate a tick that is ALREADY
        armed: the loop captures its delay when it schedules (line
        above: delayMilliSeconds at re-arm time), so the pending firing
        stays up to a heartbeat away -- the first instant-forget-redraw
        attempt failed exactly this way in the field (2026-08-05).
        Bumping the generation makes that pending tick exit unarmed at
        its gen guard (the same storm-collapse rule a save's reinit
        storm uses), and this freshly-armed near-immediate tick becomes
        the one live loop.
        """
        # ARM FIRST, COMMIT SECOND: publishing a generation no armed
        # tick carries kills the loop (tick sees gen != stored, never
        # reschedules, nothing re-arms until reinit). This order leaves
        # the OLD generation authoritative if run() fails.
        try:
            gen = self.ownerComp.fetch('_convoy_gen', 0) + 1
            run("o = op(%r)\nif o and o.valid: o.ext.ConvoyExt._convoyTick(%d)"
                % (self.ownerComp.path, gen),
                fromOP=self.ownerComp, delayFrames=2)
            self.ownerComp.store('_convoy_gen', gen)
        except Exception as e:
            self._log('could not accelerate the reconcile tick (%s); the '
                      'existing chain still owns it' % (e,), 'WARNING')

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
        # Capability pars reconcile on EVERY tick too: a change whose
        # parexec callback was deferred past a suppressed window (save,
        # settings restore) or swallowed by one would otherwise stand as
        # an unauthorized, unenforced On until the next startup
        # (field 2026-08-20).
        try:
            self._reconcileDangerGates()
        except Exception:
            pass
        if self._busy:
            # A call is already in flight; its poll owns the next schedule.
            self._tick_ms = self.TICK_MIN_MS
            return
        if self._host_busy and not self._recoverWedgedHostSlot():
            # One worker serializes registration with install/start/stop
            # (never queue a short registration behind a long installer's
            # budget). But ask _recoverWedgedHostSlot first: when only
            # button presses could unwedge the slot, a wedged flag
            # starved registration/heartbeat/node-list forever
            # (2026-08-04 Mac).
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
        """Fill Node Name with `hostname / toe-stem`. Idempotent.

        Runtime constant, not an expression (expressions bake into the
        release .tox, 2026-08-03). Never fills unsaved (2026-08-04).
        Heals baked auto-stamps only -- own NewProject placeholder, or a
        foreign host's stamp for this project (travels in the .toe,
        2026-08-19); user overrides match neither shape.
        """
        par = getattr(self._embody.par, 'Convoynodename', None)
        if par is None:
            return
        try:
            hostname = str(socket.gethostname() or '').strip() or 'localhost'
        except Exception:
            hostname = 'localhost'
        try:
            if par.mode != ParMode.CONSTANT:
                return
            value = str(par.eval() or '').strip()
        except Exception:
            return
        saved_toe = self._savedToe()
        if not saved_toe:
            return
        # From the RESOLVED file, not project.name -- after an incremental
        # save project.name is already the next name in the series.
        try:
            toe_stem = os.path.splitext(
                os.path.basename(str(saved_toe)))[0] or 'Untitled'
        except Exception:
            toe_stem = 'Untitled'
        automatic = ('%s / %s' % (hostname, toe_stem))[:512]
        if value:
            # Heal the AUTO-STAMP SHAPE ('<host> / <tail>'). A FOREIGN
            # host half always heals: it traveled here inside the .toe (a
            # cloned fleet all read 'TEC-A4D / Render.36', 2026-08-19),
            # and matching only this project's stem missed clones deployed
            # under new names. An OWN host half heals only when the tail
            # is this project's stem at any version -- or the NewProject
            # placeholder -- so 'TEC-A4D / Embody-6.236' refreshes on a
            # 6.257 project (same field day) while a custom own-host tail
            # stays the user's. Not stamp-shaped: always the user's.
            try:
                m = re.fullmatch(r'(\S+) / (.+)', value)
                if not m:
                    return
                if m.group(1) == hostname:
                    if value == automatic:
                        return
                    base = re.sub(r'\.\d+$', '', toe_stem)
                    own_auto = (
                        re.fullmatch(re.escape(base) + r'(\.\d+)?',
                                     m.group(2))
                        or re.fullmatch(r'NewProject(\.\d+)?', m.group(2)))
                    if not own_auto:
                        return
            except Exception:
                return
        try:
            par.val = automatic
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
    def _workerLoop(work_queue, shutdown_event, generation, idle_s=0.25,
                    _empty=Empty):
        """Long-lived TDTask target. WORKER THREAD; zero TD access.

        `_empty` IS BOUND AT DEFINITION TIME, ON PURPOSE. This loop outlives
        the module that defines it: every save reinitialises the extension
        and TouchDesigner tears down the old module's globals, while this
        thread is still spinning. Referring to the global `Empty` then
        raises `NameError: name 'Empty' is not defined` from inside the
        except clause -- observed on the v6.0.231 save (2026-08-09), where
        it escaped through the Thread Manager as a worker-loop traceback.
        A default argument lives on the function object, so it survives a
        teardown the global namespace does not.
        """
        while not shutdown_event.is_set():
            try:
                fn = work_queue.get(timeout=idle_s)
            except _empty:
                continue
            except BaseException:
                # The queue itself failed in a way teardown can produce.
                # A long-lived worker must end quietly rather than surface a
                # traceback the user can do nothing about.
                return
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
        # Enforce the main-thread contract: this funnel arms td.run()
        # polls, and worker-side run() silently corrupts TD (Derivative-
        # confirmed 2026-08-17). Outside TD the import fails => assume
        # main; any other surprise fails OPEN.
        try:
            import td
            on_main = td.isMainThread()
        except ImportError:
            on_main = True
        except Exception:
            on_main = True
        if not on_main:
            # Mirror the request_capacity refusal handle (state 'failed'
            # with the error in 'result') so every consumer shape survives;
            # request_id is None because nothing was enqueued. The callback
            # is deliberately NOT fired: invoking consumer code from the
            # offending worker thread would extend the very violation this
            # guard exists to stop.
            now = time.time()
            return {
                'request_id': None, 'kind': kind,
                'state': 'failed', 'created': now, 'updated': now,
                'result': self._apiError(
                    'wrong_thread',
                    'sibling API entry points are main-thread-only '
                    '(they schedule td.run() polls)'),
            }
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
        self._policy_busy_since = time.time()
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
            # Even a stale poll must not strand ITS instance's slot: the
            # flags live on the same object this check guards, and any
            # misfire (or exception inside the check) that returns here
            # silently wedges every later policy request -- the exact
            # first-install wedge that ate a Mac session (2026-08-04:
            # _policy_busy True with the finished result parked, forever).
            self._policy_busy = False
            self._policy_result = None
            return
        try:
            out = self._policy_result
            if out is None or out.get('_gen') != gen:
                if attempts < self.POLL_ATTEMPTS:
                    run('args[0]._pollPolicyCall(args[1], args[2])',
                        self, gen, attempts + 1,
                        delayFrames=self.POLL_FRAMES)
                else:
                    self._policy_busy = False
                    self._finishPolicyCall(
                        'policy_timeout',
                        {'state': 'error', 'reason': 'policy_timeout'}, {})
                return
            self._policy_result = None
            self._policy_busy = False
            self._finishPolicyCall(
                out.get('_action'), out.get('result'),
                out.get('request') or {})
        except Exception as e:
            # A drain that dies must recover its slot, never orphan it.
            self._policy_busy = False
            self._policy_result = None
            self._log('policy poll crashed (%s); the slot was recovered'
                      % (e,), 'ERROR', details=traceback.format_exc())

    def _policyBusyBlocked(self):
        """True when a policy call is LEGITIMATELY in flight. MAIN THREAD.

        A dead slot is recovered instead of refused: busy with the
        worker's result PARKED means the drain chain died (a healthy
        chain drains within one 15-frame cycle) -- deliver the result
        now, exactly as the chain would have; busy past the wall-clock
        bound with nothing parked means the worker is gone -- clear and
        say so. The refuse-forever alternative ate a Mac first-install
        session (2026-08-04): every Allow-toggle answered 'another
        request is still in progress' for an hour over a call that had
        finished within seconds.
        """
        if not getattr(self, '_policy_busy', False):
            return False
        parked = self._policy_result
        if parked is not None:
            self._policy_result = None
            self._policy_busy = False
            self._log('a finished Convoy policy call was stuck '
                      'undelivered; delivering it now', 'WARNING')
            self._finishPolicyCall(parked.get('_action'),
                                   parked.get('result'),
                                   parked.get('request') or {})
            # _finishPolicyCall may legitimately begin a follow-up
            # (refresh/confirm); report the CURRENT truth either way.
            return getattr(self, '_policy_busy', False)
        age = time.time() - getattr(self, '_policy_busy_since',
                                    time.time())
        if age > self.SLOT_BUSY_MAX_S:
            self._policy_busy = False
            self._log('a Convoy policy call exceeded its %ds budget with '
                      'no result; the slot was recovered'
                      % (int(self.SLOT_BUSY_MAX_S),), 'WARNING')
            return False
        return True

    def _finishPolicyCall(self, action, result, request):
        # A deferred-challenge retry (below) can outlive a reinit; the
        # request dies with its instance, like every other in-flight
        # host action (see onDestroyTD).
        if self._staleInstance():
            return
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
            # A save window suppresses dialogs (_suppress_dialogs); the
            # auto-answer silently DECLINED an enable requested seconds
            # before a Ctrl+S. Re-arm the finish until the window lifts,
            # bounded so a stuck flag still declines eventually.
            try:
                suppressed = bool(self._embody.fetch(
                    '_suppress_dialogs', False, search=False))
            except Exception:
                suppressed = False
            waited = int((request or {}).get('_challenge_wait', 0))
            if suppressed and waited < self.CHALLENGE_WAIT_MAX:
                deferred = dict(request or {})
                deferred['_challenge_wait'] = waited + 1
                run('args[0]._finishPolicyCall(args[1], args[2], args[3])',
                    self, action, result, deferred,
                    delayFrames=self.CHALLENGE_WAIT_FRAMES)
                return
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
                # NEVER sys.executable: in TD that is the bundled
                # python.exe, and the host's process probe then refuses
                # every launch profile runtime_unverifiable (2026-08-19).
                td_executable=client.process_executable(),
                launch_token=os.environ.get(
                    'EMBODY_CONVOY_LAUNCH_TOKEN'),
                launch_reservation_id=os.environ.get(
                    'EMBODY_CONVOY_LAUNCH_RESERVATION'))
            session['pending_sent'] = state
            # Snapshot the host-line write counter at SEND time so a
            # register that drains AFTER a Stop/Uninstall completed can
            # prove its evidence predates that action and stand down
            # (see _reviveDisprovenHostLine).
            session['register_host_seq'] = getattr(
                self, '_host_line_seq', 0)
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
            # A stale poll still clears ITS instance's slot -- a misfire
            # here wedged a Mac first-install session (2026-08-04).
            self._busy = False
            self._result = None
            return
        try:
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
        except Exception as e:
            self._busy = False
            self._result = None
            self._log('%s poll crashed (%s); the slot was recovered'
                      % (action, e), 'ERROR',
                      details=traceback.format_exc())

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
            # Same proof fixes the READOUT: this call ran through the
            # daemon, so an absent/down host line is stale (a session
            # once latched 'Install failed' for 20 hours while
            # heartbeating through the running daemon, 2026-08-10..11).
            self._reviveDisprovenHostLine(result, client)
            # Daemon provably answered: if its code is older than this
            # Embody, update in place (once per session,
            # _maybeUpdateHostApp). DEFERRED to its own frame callback --
            # InstallHost's prelude has no business inside the register
            # poll (TD crashed seconds after an in-drain firing,
            # 2026-08-05).
            sess_flags = self._session()
            self._resetHostUpdateLatchOnUpgrade(sess_flags)
            reported = result.get('host_app_version')
            if (not sess_flags.get('host_auto_update_done')
                    and sess_flags.get('host_update_checked')
                        != str(reported or '')):
                run('args[0]._maybeUpdateHostApp(args[1])',
                    self, reported, delayFrames=600)
        else:
            session['registered'] = False
            session['sent'] = None
            if (state == 'refused'
                    and str(result.get('reason') or '')
                    == 'local_realm_conflict'):
                until = session.get('offer_rejoin_until')
                if until is not None and time.monotonic() >= until:
                    session.pop('offer_rejoin_until', None)
                    until = None
                if until is not None:
                    # The user recently toggled Convoy on and the local
                    # daemon's realm refused this project's binding.
                    # Offer the rejoin off this drain (a dialog inside
                    # the poll would block it). The flag is cleared at
                    # DELIVERY, not here -- a busy host slot or a failed
                    # plan must not burn the one offer (review finding).
                    run('args[0]._offerRejoinLocalConvoy()', self,
                        delayFrames=5)
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
        # Resolved ONCE per context: three fields need it, and each
        # resolution walks the venv folder and warns when it is unusable
        # -- resolving per-field logged that warning three times per
        # button press (field log 2026-08-04).
        venv_python = self._convoyVenvPython()
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
            'version_settle_attempts': self.VERSION_SETTLE_ATTEMPTS,
            'version_settle_s': self.VERSION_SETTLE_S,
            # Embody's own uv-managed venv python -- the interpreter the rest
            # of Embody's Python runs under. It already carries the Convoy
            # crypto floor (Ed25519/X.509/TLS 1.3), so the host app runs under
            # it when no signed managed runtime is installed. Resolved on the
            # main thread here; None if the venv is not built or is missing.
            'venv_python': venv_python,
            # Every interpreter worth PROVING, best first. The worker probes
            # each (spawning it to import cryptography and prove TLS 1.3) and
            # uses the first that passes -- TD's bundled Python is not always
            # the one that can, notably on Apple Silicon where its venv's
            # cryptography .dylib would not dlopen at all.
            'runtime_candidates': self._convoyRuntimeCandidates(
                venv_python=venv_python),
            # Repair context, resolved MAIN THREAD like everything else
            # here: uv's location (resolve-only -- never _findOrInstallUv,
            # which can run a blocking pip subprocess), the CONSOLE venv
            # python for uv's --python flag, and Embody's own cryptography
            # pin so a repair installs exactly what the venv was built to
            # carry. Any of these being None just means no repair attempt.
            'uv': self._convoyUvPath(),
            'venv_python_repair': self._convoyVenvPythonConsole(
                venv_python=venv_python),
            'venv_crypto_deps': self._convoyCryptoDeps(),
            # The per-user daemon venv: where to build it and which
            # non-TouchDesigner base interpreters may host it. Present on
            # darwin (escaping library validation) AND on win32 (escaping
            # a project-scoped interpreter); None elsewhere.
            'daemon_venv': self._convoyDaemonVenvSpec(),
        }

    def _convoyVenvPython(self):
        """Path to Embody's uv-managed venv python, or None. MAIN THREAD.

        Reads project.folder via EmbodyExt._venvPaths (a main-thread global),
        so it is resolved here into the plain host context and never touched
        from a worker.

        Quiet on a never-saved project: the derived .venv path roots at
        TD's default folder and is meaningless by construction, so
        probing it can only produce a misleading warning (field log
        2026-08-04 warned about Desktop/.venv before the wizard's save).
        """
        if not self._savedToe():
            self._log('project not saved yet -- no venv to resolve for '
                      'the host app', 'DEBUG')
            return None
        try:
            path = self._embody.ext.Embody._venvPaths().get('venv_python')
        except Exception:
            return None
        try:
            if not path:
                self._log('no venv python path is known for this project '
                          '-- Convoy cannot start its host app', 'DEBUG')
                return None
            if not os.path.isfile(path):
                # A venv does not always expose the bare name: depending on
                # how it was built it may carry only python3 / python3.11.
                # Demanding one spelling made a perfectly good macOS venv read
                # as "no runtime available" (2026-08-03).
                folder = os.path.dirname(path)
                for name in ('python3', 'python3.11', 'python3.12',
                             'python3.13', 'python'):
                    candidate = os.path.join(folder, name)
                    if os.path.isfile(candidate):
                        path = candidate
                        break
                else:
                    self._log(
                        'no usable interpreter in %s -- Convoy cannot start '
                        'its host app (looked for python, python3, '
                        'python3.x)' % (folder,), 'WARNING')
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

    def _convoyUvPath(self):
        """uv's executable for the venv repair, or None. MAIN THREAD.

        Resolve-only by design: EmbodyExt._findOrInstallUv can fall back
        to a blocking `pip install --user uv` subprocess, which has no
        place on the main thread mid-install. _resolveUv is its
        subprocess-free lookup -- PATH plus the pip --user locations a
        GUI TouchDesigner's PATH does not include (macOS especially:
        launchd PATH excludes ~/.local/bin and ~/Library/Python/*/bin,
        where the uv that built this very venv usually lives). A bare
        shutil.which here made the repair a silent no-op on macOS.
        Absence simply means the worker skips the repair attempt.
        """
        try:
            return self._embody.ext.Embody._resolveUv()
        except Exception:
            return None

    def _convoyVenvPythonConsole(self, venv_python=_UNSET):
        """The venv interpreter for uv's --python flag. MAIN THREAD.

        Same interpreter _convoyVenvPython resolves, minus the Windows
        pythonw.exe swap: uv drives the target over pipes and the console
        binary is the conventional, known-good target for it. Pass the
        already-resolved path when building a host context so the venv
        walk (and its warning) runs once, not per field.
        """
        path = (venv_python if venv_python is not _UNSET
                else self._convoyVenvPython())
        if not path:
            return None
        if sys.platform == 'win32' and path.lower().endswith('pythonw.exe'):
            console = os.path.join(os.path.dirname(path), 'python.exe')
            if os.path.isfile(console):
                return console
        return path

    def _convoyCryptoDeps(self):
        """Embody's cryptography pin(s) from the venv dependency spec.

        Read from _venvPaths so a repair installs EXACTLY what the venv
        was built to carry (including any platform-conditional ceiling),
        never a second, driftable copy of the pin. MAIN THREAD --
        _venvPaths reads the project folder.
        """
        try:
            deps = self._embody.ext.Embody._venvPaths().get('deps') or []
        except Exception:
            return []
        return [str(d) for d in deps if str(d).startswith('cryptography')]

    def _convoyDaemonVenvSpec(self):
        """Paths for the per-user daemon venv, or None. MAIN THREAD.

        THIN BY DESIGN. The platform decision itself -- which
        interpreters may host the venv, where it lives, and which of a
        Windows python.exe/pythonw.exe pair is the daemon -- lives in
        convoy_install.daemon_venv_spec, where it is pure, injectable and
        reachable by the windows+macos CI matrix. It sat here for one
        release and no runner could test it. All this method owes it is
        the data dir, which needs a live client.
        """
        try:
            return self._installer().daemon_venv_spec(
                self._client().data_dir())
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

    # Host actions that actually SPAWN a child. The rest are audits and
    # daemon HTTP calls whose 'WinError 6' is an ordinary handle error,
    # not "TD cannot launch the Convoy app". 'status' spawns too
    # (schtasks/launchctl) and runs unprompted -- the first failure a
    # spawn-blocked session sees.
    _SPAWNING_HOST_ACTIONS = ('install', 'start', 'stop', 'uninstall',
                              'status')

    def _isBlockedSpawn(self, action, result, text):
        """Does this failure need the cannot-start-any-child explanation?

        Three gates, each earned: (1) the action must actually spawn --
        HTTP calls raise the same Windows handle errors; (2) the install
        path is excluded -- probe_runtime already returns this exact
        advice as spawn_blocked detail; (3) the marker list is ASKED via
        is_spawn_failure(), never copied (a local copy had drifted).
        Module unreachable => plain failure line.
        """
        if action not in self._SPAWNING_HOST_ACTIONS:
            return False
        if isinstance(result, dict) and result.get('reason') == 'spawn_blocked':
            return False        # the detail already says all of this
        try:
            return bool(self._installer().is_spawn_failure(text))
        except Exception:
            return False

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
        self._host_action = action
        self._host_busy_since = time.time()
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
            # A stale poll still clears ITS instance's slot -- a misfire
            # here wedged a Mac first-install session (2026-08-04).
            self._host_busy = False
            self._host_result = None
            return
        try:
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
        except Exception as e:
            self._host_busy = False
            self._host_result = None
            self._log('host %s poll crashed (%s); the slot was recovered'
                      % (action, e), 'ERROR',
                      details=traceback.format_exc())

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
        rejected = result.get('rejected')
        if isinstance(rejected, list) and rejected:
            # Full per-interpreter probe stderr at DEBUG, on success AND
            # failure -- get_logs recovers the untruncated dlopen reason
            # (losing it cost a macOS diagnosis a full round trip).
            for entry in rejected:
                if not isinstance(entry, dict):
                    continue
                self._log(
                    'probe rejected %s (%s)'
                    % (entry.get('candidate'), entry.get('reason')),
                    'DEBUG', details=entry.get('detail'))
        if not ok:
            if action == 'install':
                # The one action whose failure has its own word in the
                # vocabulary, because it is the one a user pulsed and is
                # waiting on.
                state = self.HOST_INSTALL_FAILED
            text = str(detail or 'unknown')
            if self._isBlockedSpawn(action, result, text):
                # Spawn-blocked TD (tool-launched sessions inherit a
                # broken stdin): name the cause once instead of the raw
                # OSError. Install path already says it in its own
                # detail (_isBlockedSpawn).
                self._log(
                    'host %s cannot run: this TouchDesigner process cannot '
                    'start ANY child process, so the Convoy app cannot be '
                    'launched from it. This is the session, not Convoy or '
                    'your Python -- it happens when TouchDesigner was '
                    'started by a tool rather than opened normally. Quit '
                    'TouchDesigner, open the project yourself, and try '
                    'again. (%s)' % (action, text), 'WARNING')
            else:
                self._log('host %s failed: %s' % (action, text), 'WARNING')
        elif result.get('version_verified') is False:
            # The install completed but the daemon that came back is NOT
            # running the version we just wrote -- a stale payload must
            # be a visible warning, never a quiet DEBUG line (and never
            # BOTH lines: this branch replaces the DEBUG one).
            self._log('host %s: %s' % (action, detail or 'the restarted '
                      'daemon did not verify at the installed version'),
                      'WARNING')
        elif ('version_verified' in result
                and result['version_verified'] is None
                and (result.get('outcome') or {}).get('registered')):
            # An install whose daemon never answered is not a debug detail:
            # nothing confirmed the code now running, so it must not slide
            # past as a clean install the way a DEBUG line would. The key is
            # present only on install/repair results, so start/stop -- which
            # verify no version -- do not fall in here.
            self._log('host %s: %s' % (action, detail or 'the restarted '
                      'daemon did not answer in time'), 'WARNING')
        elif result.get('restart_retry'):
            # A self-heal is not a failure, but it is not a debug detail
            # either -- it is why this install took longer than usual.
            self._log('host %s: %s' % (action, detail), 'INFO')
        elif detail:
            self._log('host %s: %s' % (action, detail), 'DEBUG')

        if action == 'preview':
            # AN AUDIT MUST NEVER ALTER STATE -- and the readout is state.
            # Reporting a preview through Convoyhoststatus would overwrite
            # a live 'Running ...' with something the user did not ask to
            # change.
            self._logUninstallPreview(result.get('plan'))
            return

        if action == 'forget_offline_plan':
            # A LISTING is a preview: it must never alter the readout.
            # The slot is already released, so the confirm can chain the
            # apply call (the uninstall preview relies on the same fact).
            if ok:
                self._confirmForgetOffline(
                    result.get('rows'),
                    remote_hosts=result.get('remote_offline_hosts'))
            return

        if action == 'realm_conflict_plan':
            # Same preview contract as forget_offline_plan.
            if ok:
                self._confirmResolveRealm(result)
            else:
                self._log('Resolve Realm Conflict: %s'
                          % (result.get('detail') or 'listing failed'),
                          'WARNING')
            return

        if action == 'rejoin_plan':
            # Preview for the re-enable rejoin offer; alters nothing.
            if ok:
                self._confirmRejoinLocalConvoy(result)
            else:
                self._log('Rejoin offer: %s'
                          % (result.get('detail') or 'listing failed'),
                          'INFO')
            return

        if action == 'realm_conflict_resolve':
            if ok:
                # Re-register PROMPTLY on success: the refusal-backoff
                # loop would otherwise keep the node off its own
                # freshly-reset realm for up to a minute. A FAILED reset
                # earns no kick -- forcing an immediate register the
                # daemon re-refuses only tightens the refusal loop.
                session = self._session()
                session['sent'] = None
                session['next_call_at'] = None
                self._kickTick()
            self._log('Resolve Realm Conflict: %s'
                      % (result.get('detail') or 'done'),
                      'SUCCESS' if ok else 'WARNING')
            # The readout: the register that follows writes the node
            # line; the host line refresh rides the next status action.
            return

        if action == 'realm_join':
            stranded = False
            if ok:
                adopted = str(result.get('adopted') or '')
                project_id = str(self._readConvoyId() or '')
                need_rebind = (project_id and adopted
                               and project_id != adopted)
                rebound = ''
                if need_rebind:
                    # Machine adopted the realm; rebind this project's
                    # git-tracked binding NOW (the join was just
                    # confirmed -- no second dialog). Other projects
                    # offer their own rejoin on next enable.
                    try:
                        rebound = (self._embody.ext.Embody
                                   ._rebindConvoyToCandidate(project_id))
                    except Exception as e:
                        self._log('join rebind raised: %s' % (e,),
                                  'WARNING')
                if need_rebind and not rebound:
                    # A CAS miss returns '' WITHOUT raising -- and a
                    # kick from here would fire a register still
                    # carrying the abandoned realm into a guaranteed
                    # 409 (review finding). Say what stands and how to
                    # finish; do not kick.
                    stranded = True
                    self._log('the machine joined %s but this project '
                              'still carries %s -- toggle Convoy off '
                              'and on to rejoin it'
                              % (adopted, project_id), 'WARNING')
                else:
                    if rebound:
                        self._publishId(rebound)
                    session = self._session()
                    session['sent'] = None
                    session['next_call_at'] = None
                    self._kickTick()
                for sender in (result.get('denylisted_senders')
                               or [])[:8]:
                    self._log('the joined realm\'s sender %s is in this '
                              'machine\'s denylist.json (a previous '
                              '"Keep This Realm") -- remove that entry '
                              'or this machine cannot hear the mesh it '
                              'just joined'
                              % ((sender or {}).get('address')
                                 or (sender or {}).get('host_id')
                                 or 'unknown'), 'WARNING')
            # A stranded project must not close on an unqualified
            # SUCCESS line -- a user scanning only the last line would
            # miss the remedy above (verify round).
            self._log('Join Other Realm: %s'
                      % (result.get('detail') or 'done'),
                      'SUCCESS' if ok and not stranded else 'WARNING')
            return

        if action == 'forget_offline':
            # ARM THE REDRAW FIRST, before any modal, regardless of ok:
            # a dialog blocks the drain, and a host that vanished after
            # the optimistic drop would leave rows missing until the
            # next heartbeat.
            session['next_call_at'] = None
            self._tick_ms = self.TICK_MIN_MS
            self._kickTick()
            kept = [k for k in (result.get('kept_busy') or ())]
            forgotten = list(result.get('forgotten') or ())
            # Restore = dropped MINUS confirmed-gone, never derived from
            # the outcome lists (failed carries strings not ids; a future
            # bucket would silently stop restoring). skipped rows may be
            # back ONLINE -- the worst to leave missing.
            forgotten_set = {str(f) for f in forgotten}
            self._restoreNodeRowsNow([
                str(n.get('node_id') or '')
                for n in (getattr(self, '_optimistically_dropped', None) or ())
                if isinstance(n, dict)
                and str(n.get('node_id') or '') not in forgotten_set])
            # Grade by OUTCOME, not by the absence of a hard failure:
            # `ok` is "nothing errored", so a run that forgot NOTHING
            # used to log green while the user watched every row come
            # back (field report 2026-08-06). This runs OUTSIDE `if ok`
            # -- a batch with one hard failure still forgot and kept
            # rows the user needs told about.
            level = ('SUCCESS' if forgotten and not kept and ok
                     else 'INFO' if forgotten else 'WARNING')
            self._log('Forget Offline Nodes: %s'
                      % (result.get('detail') or 'done'), level)
            if kept:
                self._reportKeptNodes(kept, forgotten)
            # The readout (host-app state) was never part of this.
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
        # Every write bumps the counter _reviveDisprovenHostLine compares
        # against its register's send-time snapshot -- a host action
        # landing between a register's send and its drain makes the
        # stale-line evidence itself stale.
        self._host_line_seq = getattr(self, '_host_line_seq', 0) + 1
        self._host_status_text = str(text)[:160]
        self._publishStatus()

    def _clearHostLineIf(self, prefix):
        """Drop a self-authored transient host line when its wait chain
        dies without a completing action (disable mid-venv-wait would
        otherwise pin 'Installing...' over a disabled readout)."""
        host = str(getattr(self, '_host_status_text', '') or '')
        if not host.startswith(prefix):
            return
        self._host_line_seq = getattr(self, '_host_line_seq', 0) + 1
        self._host_status_text = ''
        self._publishStatus()

    def _reviveDisprovenHostLine(self, result, client):
        """Replace a stale down-claiming host line after a registration.

        Register success = direct evidence the daemon runs, so a line in
        _DISPROVEN_HOST_TEXTS is rewritten (via host_status_text, the one
        vocabulary source; pid/detail dropped -- they described the
        disproven state). Three stand-downs: Perform Mode (readout frozen
        for the show); a host action in flight (fresher line coming);
        line written AFTER this register was sent (the daemon may be
        gone -- reviving would invert the 20-hour latch). Accepted
        imprecision: module-integrity 'Install failed' is not disproven,
        but the mesh works and later actions re-fail loudly.
        """
        host = str(getattr(self, '_host_status_text', '') or '')
        if not host.startswith(self._DISPROVEN_HOST_TEXTS):
            return
        if self._performing() or self._host_busy:
            return
        session = self._session()
        sent_seq = session.get('register_host_seq')
        if sent_seq is not None and sent_seq != getattr(
                self, '_host_line_seq', 0):
            return
        state = session.get('host_state')
        state = dict(state) if isinstance(state, dict) else {}
        state['state'] = getattr(client, 'HOST_RUNNING', 'running')
        state['live'] = True
        state.pop('pid', None)
        state.pop('detail', None)
        reported = str((result or {}).get('host_app_version') or '')
        if reported:
            state['installed_version'] = reported
        session['host_state'] = state
        self._log('host app answered registration -- replacing the stale '
                  'host-app line (%s)' % host, 'INFO')
        self._hostStatus(state)

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

    def _confirmRepairRuntime(self, ctx, interpreter, plan,
                              venv_runtime=False):
        """The A-6 dialog for a RUNTIME-ONLY repair.

        Its own dialog rather than a relaxed install one, because the
        install dialog's sentences would be false here: no files are
        written, no version changes, and the thing being fixed belongs
        to a newer Embody than this project. It still asks, because it
        still rewrites a program that runs at every logon.
        """
        # SAME ACCURACY RULE AS _confirmInstall. On Windows with a venv
        # runtime the ladder prefers (or builds) the per-user Convoy
        # venv, so naming `interpreter` -- the project venv resolved
        # before the ladder runs -- as the thing that will run at login
        # is exactly the sentence _confirmInstall was rewritten to stop
        # saying. Only promise a specific interpreter where one is
        # actually settled.
        if ctx['platform'] == 'win32' and venv_runtime:
            target = (
                '  - re-registers the %s that starts it at login, under\n'
                '    a dedicated per-user Convoy venv in the folder\n'
                '    below, building it if needed (this may download the\n'
                '    pinned cryptography package). If it cannot be\n'
                '    built, it falls back to this project Python:\n'
                '    %s\n'
                % (self._supervisorNoun(ctx['platform']), interpreter))
        else:
            target = (
                '  - re-registers the %s that starts it at login, pointing\n'
                '    at:\n'
                '    %s\n'
                % (self._supervisorNoun(ctx['platform']), interpreter))
        message = (
            'Re-point the Convoy host app at a working Python?\n\n'
            'The host app installed for this user is version %s -- '
            'installed by a NEWER Embody than this project (%s). The '
            'Python it was installed against no longer exists, so it '
            'cannot start.\n\n'
            'What this does:\n'
            '%s'
            '  - updates the recorded interpreter in\n'
            '    %s\n\n'
            'What this does NOT do:\n'
            '  - it does not write, replace or downgrade the host app.\n'
            '    Version %s and its files are left exactly as they are,\n'
            '    and that newer code is what starts back up.\n\n'
            '%s'
            % (plan.get('installed_version'), ctx['version'], target,
               ctx['data_dir'], plan.get('installed_version'),
               plan.get('detail') or ''))
        return self._dialog('Embody - Repair the Convoy host app runtime',
                            message, ['Cancel', 'Repair']) == 1

    def _confirmInstall(self, ctx, interpreter, plan, modules,
                        venv_runtime=False):
        """The A-6 dialog. Every sentence in 1.6, none of them softened."""
        # WHICH PYTHON, said accurately per platform. On Windows the
        # dedicated per-user venv is now the PREFERENCE, not the
        # last-resort repair, and the old wording ('a failing one is
        # repaired or replaced') would describe the exception as if it
        # were the rule -- while naming the project venv as the thing
        # that will run at login, which is precisely what it will not
        # be when the build succeeds.
        # ...and only when a venv runtime is what will actually be
        # resolved. With a signed managed runtime installed, venv_runtime
        # is False, the whole daemon-venv ladder is skipped, and this
        # branch would both promise a build that never happens and call
        # that managed runtime "this project's Python".
        if ctx['platform'] == 'win32' and venv_runtime:
            runtime_note = (
                '  - runs it under a dedicated per-user Convoy venv in the\n'
                '    folder above, built on the first install (this may\n'
                '    download the pinned cryptography package). If that\n'
                '    cannot be built, it falls back to this project\'s\n'
                '    Python:\n'
                '    %s\n'
                '    which stops working if this project is moved or\n'
                '    deleted; the log names the final interpreter\n\n'
                % (interpreter,))
        else:
            runtime_note = (
                '  - runs it under the best Python that proves Convoy\'s\n'
                '    crypto floor, starting with:\n'
                '    %s\n'
                '    a failing one is repaired or replaced with a dedicated\n'
                '    Convoy venv in the folder above (this may download the\n'
                '    pinned cryptography package); the log names the final\n'
                '    interpreter\n\n'
                % (interpreter,))
        message = (
            'Install the Convoy host app for THIS user on THIS machine?\n\n'
            'What this does:\n'
            '  - writes %d small Python files to\n'
            '    %s\n'
            '  - registers a %s that starts the program when you log in\n'
            '    and restarts it within a minute\n'
            '  - IT RUNS WHENEVER YOU ARE LOGGED IN, WHETHER OR NOT\n'
            '    TOUCHDESIGNER IS OPEN\n'
            '%s'
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
               self._supervisorNoun(ctx['platform']), runtime_note,
               plan.get('detail') or ''))
        return self._dialog('Embody - Install the Convoy host app', message,
                            ['Cancel', 'Install']) == 1

    # Absolute wall-clock bound on a busy slot with NO parked result.
    # Independent of frame rate on purpose: the frame-based poll caps
    # stretch arbitrarily on a throttled/background TD, and a wedged
    # worker must not turn into a permanent refusal.
    SLOT_BUSY_MAX_S = 900.0

    def _recoverWedgedHostSlot(self):
        """Recover a DEAD host slot; True when the flag is now clear.

        Busy with the worker's result PARKED means the drain chain died
        -- a healthy chain drains within one 15-frame cycle. Busy past
        the wall-clock bound with nothing parked means the worker itself
        is gone. Refusing forever is strictly worse than recovering
        loudly: that exact wedge ate a Mac first-install session
        (2026-08-04) until a TD restart.
        """
        parked = self._host_result
        if parked is not None:
            self._host_result = None
            self._host_busy = False
            self._log('a finished Convoy host call was stuck undelivered; '
                      'delivering it now', 'WARNING')
            self._finishHost(getattr(self, '_host_action', None) or 'call',
                             parked.get('result'))
            return not self._host_busy
        age = time.time() - getattr(self, '_host_busy_since', time.time())
        if age > self.SLOT_BUSY_MAX_S:
            self._host_busy = False
            self._log('a Convoy host call exceeded its %ds budget with no '
                      'result; the slot was recovered'
                      % (int(self.SLOT_BUSY_MAX_S),), 'WARNING')
            return True
        return False

    def _hostActionAllowed(self, what):
        """False (with a stated reason) when a host action must not run."""
        if self._performing():
            self._log('Perform Mode is on -- %s waits until it ends'
                      % (what,), 'INFO')
            return False
        if self._host_busy and not self._recoverWedgedHostSlot():
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

    def _resetHostUpdateLatchOnUpgrade(self, session, version=None):
        """Give a NEWLY UPGRADED Embody its own daemon-update attempt.

        The update guards live in the sys-keyed session store, which an
        in-place Embody upgrade does not reset -- so the stale
        host_update_checked latch skipped the check and a live-session
        upgrade could never update the daemon (v6.0.213's hole, one step
        along). Latch keyed on THIS Embody's version: one attempt per
        (Embody version, TD session), no retry storm. `version` is
        injectable only for tests (writing the live Version par from a
        test once nearly baked 9.9.9 into a release).
        """
        if version is None:
            try:
                version = str(self._embody.par.Version.eval() or '')
            except Exception:
                return      # never break a registration over bookkeeping
        version = str(version)
        if session.get('host_update_embody_version') == version:
            return
        session['host_update_embody_version'] = version
        session.pop('host_auto_update_done', None)
        session.pop('host_update_checked', None)

    def _maybeUpdateHostApp(self, reported_version):
        """Update a LIVE but older Convoy App in place, automatically.

        The daemon only updates through install(), and pre-6.0.213
        nothing compared its running code to the Embody in front of it
        (nine releases of silently-old daemons; every registry fix
        'did not work'). Closes the loop at the moment the register
        response says what code it runs. Guards: strictly-older or
        version-less only; one attempt per session (slot-busy gives the
        attempt back, real failure does not); non-update outcomes latch
        per reported version. Same-version races are safe by install()'s
        construction; different-version races can briefly downgrade and
        self-heal next session (the NEWER_INSTALL edge the UI names).
        """
        if self._staleInstance():
            # The deferral window (600 frames) is long enough for a
            # hot-synced source edit to reinit this extension; a stale
            # instance's host slot gives no mutual exclusion and its
            # poll chain would discard the install result unseen.
            return
        if self._performing():
            # Never mid-show, and never a per-heartbeat log about it:
            # nothing is spent, the next heartbeat after the show
            # re-checks silently.
            return
        session = self._session()
        if session.get('host_auto_update_done'):
            return
        marker = str(reported_version or '')
        if session.get('host_update_checked') == marker:
            return
        ctx = self._safeHostContext()
        if ctx is None:
            return
        try:
            version_key = ctx['installer'].orderable_version_key
        except Exception:
            return
        try:
            installed = ctx['installer'].read_installed(
                ctx['data_dir'], ctx['platform'])
        except Exception:
            installed = None
        if ((installed or {}).get('supervisor')
                == ctx['installer'].SUPERVISOR_EXTERNAL):
            # An external supervisor owns start/stop: writing a new
            # payload would not restart the daemon, so an automatic
            # install would log a clean success while old code kept
            # running. Say where to act instead -- once.
            session['host_update_checked'] = marker
            self._log('the Convoy App here is managed by an external '
                      'supervisor -- automatic update does not apply; '
                      'update it through that supervisor', 'INFO')
            return
        action, detail = _host_update_decision(
            reported_version, ctx['version'], version_key)
        if action != 'update':
            session['host_update_checked'] = marker
            return
        session['host_auto_update_done'] = True
        self._log('Convoy App update: %s -- updating it in place now '
                  '(automatic; Repair Convoy App remains the manual '
                  'path)' % (detail,), 'INFO')
        out = self.InstallHost(confirm=False)
        if isinstance(out, dict) and out.get('state') == 'deferred':
            # The host slot was busy -- that must not spend this
            # session's one attempt.
            session['host_auto_update_done'] = False

    def InstallHost(self, confirm=True):
        """Install -- or REPAIR -- the Convoy host app for this user.

        Re-runs a full install even at current version: rewriting
        payload/launcher/supervisor is what fixes 'Needs repair' and
        'no supervisor', and every step is idempotent. ONE refusal:
        downgrade over a newer Embody's install (A-36) -- unless its
        recorded Python is gone, then repair_runtime re-points the
        interpreter and leaves version/payload alone (else a dead daemon
        had no UI route back). Login persistence is its own grant
        (beyond A-13), so it gets its own confirmation.
        """
        if not self._hostActionAllowed('installing the host app'):
            return {'state': 'deferred'}
        ctx = self._safeHostContext()
        if ctx is None:
            return {'state': 'error', 'detail': 'installer module missing'}

        installer = ctx['installer']
        installed = installer.read_installed(ctx['data_dir'], ctx['platform'])
        # Ask the SAME question host_state asks, and hand the answer to
        # the planner. Without it plan_install is structurally incapable
        # of seeing the condition the status readout just reported, which
        # is how 'Install re-resolves it' and 'will not downgrade it'
        # came to be printed about the same install.
        interpreter_exists = _host_recorded_interpreter_exists(installed)
        plan = installer.plan_install(installed, ctx['version'],
                                      ctx['platform'],
                                      interpreter_exists=interpreter_exists)
        repair_only = (plan.get('action')
                       == installer.ACTION_REPAIR_RUNTIME)

        modules = self._hostModules()
        if not modules and not repair_only:
            self._log('the vendored host-app modules are missing from the '
                      "convoy COMP's `host` child -- this .tox cannot "
                      'install a host app; reinstall Embody', 'WARNING')
            self._hostStatus(self.HOST_INSTALL_FAILED)
            return {'state': 'error', 'detail': 'no vendored host modules'}
        if plan.get('action') == installer.ACTION_REFUSE_DOWNGRADE:
            self._log('install refused: %s' % (plan.get('detail'),), 'WARNING')
            # Two failures arrive as refuse_downgrade needing opposite
            # words -- re-ask plan_install's first question (is OUR
            # version usable as a dir name?) to tell them apart.
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

        # Runtime resolved HERE so the dialog names the exact login
        # interpreter and a runtime-less machine refuses BEFORE the
        # confirmation. Signed managed runtime preferred; Embody's venv
        # python is the working default.
        interpreter = installer.choose_interpreter(
            installer.find_interpreters(ctx['platform']))
        venv_runtime = False
        if not interpreter:
            interpreter = ctx.get('venv_python')
            venv_runtime = bool(interpreter)
        if not interpreter:
            # Name the checked path (a bare "no runtime" sent a macOS
            # diagnosis hunting blind, 2026-08-03) -- and never tell a
            # user who just enabled Envoy to "enable Envoy first": the
            # venv build simply has not finished yet (2026-08-09).
            if self._envoyIsBringingTheEnvironment():
                self._log('the Python environment Convoy shares is '
                          'still being built by Envoy -- the host app cannot '
                          'install until it exists. This resolves itself; the '
                          'install retries automatically.', 'INFO')
            else:
                self._log('no Convoy runtime is available -- no signed managed '
                          'runtime, and no usable interpreter at %r. Enable '
                          'Envoy (it builds the Python environment Convoy '
                          'shares) and the host app installs itself.'
                          % (ctx.get('venv_python') or '<no venv path>',),
                          'WARNING')
            self._hostStatus(self.HOST_INSTALL_FAILED)
            return {'state': 'error', 'detail': 'no interpreter'}

        if confirm:
            asked = (self._confirmRepairRuntime(ctx, interpreter, plan,
                                                venv_runtime)
                     if repair_only
                     else self._confirmInstall(ctx, interpreter, plan,
                                               modules, venv_runtime))
            if not asked:
                self._log('host app install cancelled -- nothing was '
                          'written and no task or agent was registered',
                          'INFO')
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
                                  venv_runtime=venv_runtime,
                                  repair_only=repair_only),
            # A runtime repair writes NO payload -- the installed version
            # and file list are copied through verbatim -- so reporting
            # 'Installing...' over it promises a version change that
            # cannot happen, on the one path reached BECAUSE the version
            # may not be replaced.
            note=(self.HOST_REPAIRING if repair_only
                  else self.HOST_INSTALLING))
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
        """AUDIT ONLY: what an uninstall would remove and keep.

        Alters nothing -- not even the readout. Plan computed in a worker
        (reads every job record), logged, stashed in the session. Returns
        the LAST preview plus busy=True while a fresh one is in flight
        (why UninstallHost does not call this: it needs the plan in hand).
        """
        if not self._hostActionAllowed('previewing the uninstall'):
            return {'state': 'deferred'}
        ctx = self._safeHostContext()
        if ctx is None:
            return {'state': 'error', 'detail': 'installer module missing'}
        self._beginHostCall('preview', lambda: _host_preview(ctx))
        return {'state': 'previewing', 'busy': True,
                'preview': self._session().get('uninstall_preview')}

    def ResolveRealmConflict(self):
        """Surface a split-realm CONFLICT and offer the sanctioned exit.

        The recovery the plan promised (ADR-003's 'advanced local
        reset/rejoin') finally gets a control: until now /realm/reset
        had ZERO callers anywhere -- an operator whose daemon latched a
        conflict (field, 2026-08-12: one un-admitted MacBook broadcast
        wedged every registration on this LAN) had no way out short of
        hand-crafting an authenticated loopback POST. Two phases on the
        host slot, mirroring Forget Offline Nodes: plan (alters
        nothing), a confirmation that NAMES the preserved realm, the
        conflicting ids and the live announcers, then apply (denylist
        the announcers, reset, re-register).
        """
        if not self._hostActionAllowed('Resolve Realm Conflict'):
            return {'state': 'busy'}
        ctx = self._safeHostContext()
        if ctx is None:
            return {'state': 'unavailable'}
        self._beginHostCall('realm_conflict_plan',
                            lambda: _host_realm_conflict_plan(ctx))
        return {'state': 'listing'}

    def _confirmResolveRealm(self, result):
        """Stage two: show the spec's dialog, dispatch by LABEL. MAIN
        THREAD. All decision logic lives in _resolve_dialog_spec (pure,
        pytest-covered); dispatching on the picked button's TEXT rather
        than its index means a conditional button can never remap a
        destructive action (review finding: with index arithmetic, one
        swapped branch turned 'Denylist Senders' into 'move this machine
        onto a stranger's realm')."""
        if self._performing():
            self._log('Resolve Realm Conflict: suppressed during '
                      'Perform Mode', 'INFO')
            return
        realm = (result or {}).get('realm') or {}
        announcers = (result or {}).get('announcers') or []
        spec = _resolve_dialog_spec(realm, announcers)
        choice = self._dialog('Embody - Resolve Realm Conflict',
                              '\n'.join(spec['lines']), spec['buttons'])
        picked = (spec['buttons'][choice]
                  if isinstance(choice, int)
                  and 0 <= choice < len(spec['buttons']) else '')
        if picked in ('', 'OK', 'Cancel', 'Close'):
            if spec['mode'] != 'clean':
                self._log('Resolve Realm Conflict: cancelled', 'INFO')
            return
        ctx = self._safeHostContext()
        if ctx is None or not self._hostActionAllowed(
                'Resolve Realm Conflict'):
            self._log('Resolve Realm Conflict: host slot unavailable; '
                      'pulse it again', 'WARNING')
            return
        if picked == 'Keep This Realm':
            self._beginHostCall(
                'realm_conflict_resolve',
                lambda: _host_realm_conflict_apply(ctx, announcers))
        elif picked == 'Denylist Senders':
            self._beginHostCall(
                'realm_conflict_resolve',
                lambda: _host_realm_conflict_apply(ctx, announcers,
                                                   reset=False))
        elif picked in spec['joins']:
            join_id = spec['joins'][picked]
            join_senders = [a for a in announcers
                            if join_id in (a.get('realms') or ())][:8]
            self._beginHostCall(
                'realm_join',
                lambda: _host_realm_join_apply(ctx, join_id,
                                               senders=join_senders))
        else:
            self._log('Resolve Realm Conflict: cancelled', 'INFO')

    # How long an explicit enable keeps its rejoin offer live. Long
    # enough for the register/refusal round trip (plus retries), short
    # enough that a much-later realm change cannot pop a modal with no
    # gesture behind it (review finding: the un-expiring flag let a
    # tick raise the dialog arbitrarily long after the toggle).
    REJOIN_OFFER_WINDOW_S = 120.0

    def ArmRejoinOffer(self):
        """Arm the one-shot rejoin offer. Called ONLY from the explicit
        Convoy Enable toggle (parexec), which is already suppressed
        during init and settings restore -- the arming lives THERE, not
        in _ensureConsent, because every already-consented install
        returns from _ensureConsent before its tail (review finding:
        the original arming was unreachable for the exact clone
        scenario the feature exists for)."""
        self._session()['offer_rejoin_until'] = (
            time.monotonic() + self.REJOIN_OFFER_WINDOW_S)

    def _offerRejoinLocalConvoy(self):
        """Fetch the machine realm, then offer the rejoin. MAIN THREAD."""
        if self._staleInstance():
            return
        if 'offer_rejoin_until' not in self._session():
            return          # delivered or expired by a sibling dispatch
        if self._performing():
            return          # the window may re-fire it after the show
        if not self._hostActionAllowed('Rejoin Local Convoy'):
            # NOT consumed: the next refusal inside the window retries.
            self._log('Rejoin offer deferred: host slot busy', 'INFO')
            return
        ctx = self._safeHostContext()
        if ctx is None:
            return
        self._beginHostCall(
            'rejoin_plan',
            lambda: dict(_host_realm_conflict_plan(ctx,
                                                   resolve_names=False),
                         action='rejoin_plan'))

    def _confirmRejoinLocalConvoy(self, result):
        """Name both realms, ask, then rebind. MAIN THREAD."""
        if self._performing():
            return          # window may re-offer after the show
        realm = (result or {}).get('realm') or {}
        machine_id = str(realm.get('convoy_id') or '')
        machine_state = str(realm.get('state') or '')
        project_id = str(self._readConvoyId() or '')
        if machine_state == 'conflict':
            self._log('Rejoin offer: this machine itself has a realm '
                      'conflict -- use Resolve Realm Conflict first',
                      'WARNING')
            return
        if (machine_state != 'established' or not machine_id
                or not project_id or machine_id == project_id):
            self._log('Rejoin offer withdrawn: machine realm %s (%s), '
                      'project %s -- nothing to rejoin'
                      % (machine_state or 'unknown',
                         machine_id or 'no id', project_id or 'no id'),
                      'INFO')
            return
        # DELIVERY is what consumes the offer -- everything above left
        # it armed so a transient bail-out could retry inside the window.
        self._session().pop('offer_rejoin_until', None)
        choice = self._dialog(
            'Embody - Rejoin Local Convoy',
            'This project is bound to Convoy %s,\n'
            "but this machine's mesh is %s.\n\n"
            'Rejoin the local mesh? The project binding becomes a '
            'candidate and adopts %s on the next registration.\n\n'
            'Caution: .embody/project.json is git-tracked, so the '
            'rebind travels to every clone of this repo -- clones on '
            'the ORIGINAL mesh will be refused there until they rejoin '
            'the same way (toggle Convoy off and on).'
            % (project_id, machine_id, machine_id),
            ['Keep Binding', 'Rejoin Local Mesh'])
        if choice != 1:
            self._log('Rejoin declined; the project keeps %s and stays '
                      'refused on this LAN' % (project_id,), 'INFO')
            return
        try:
            rebound = self._embody.ext.Embody._rebindConvoyToCandidate(
                project_id)
        except Exception as e:
            rebound = ''
            self._log('rejoin rebind failed: %s' % (e,), 'WARNING')
        if not rebound:
            return
        session = self._session()
        session['sent'] = None
        session['next_call_at'] = None
        self._publishId(rebound)
        self._kickTick()
        self._log('Rejoining the local Convoy: binding is candidate; '
                  'adoption completes on the next registration', 'SUCCESS')

    def ForgetOfflineNodes(self):
        """Forget this machine's offline node rows -- after NAMING them.

        The user's judgment call the automatic sweeps can't make. The
        confirmation names the rows and the consequence (rejoin = new
        identity, TD Python approval resets). Two phases: list, confirm,
        apply -- the daemon's refusal rules still run, and a row back
        online between dialog and apply is skipped, never killed.
        """
        if not self._hostActionAllowed('Forget Offline Nodes'):
            return {'state': 'busy'}
        ctx = self._safeHostContext()
        if ctx is None:
            return {'state': 'unavailable'}
        self._beginHostCall('forget_offline_plan',
                            lambda: _host_forget_offline_plan(ctx))
        return {'state': 'listing'}

    def _confirmForgetOffline(self, rows, remote_hosts=None):
        """Stage two: name the rows, ask, then apply. MAIN THREAD."""
        rows = [r for r in (rows or [])
                if isinstance(r, dict) and r.get('node_id')]
        if not rows:
            # Visible, not just logged (a log-only nothing-to-do reads
            # as a broken button) -- and name other machines' offline
            # rows, else "nothing to forget" contradicts the list
            # (2026-08-05, twice).
            self._log('no offline nodes to forget on this machine', 'INFO')
            message = ('No offline nodes to forget on this machine -- '
                       'every node this machine owns is online.')
            names = [str(h) for h in (remote_hosts or []) if str(h).strip()]
            if names:
                # Compare the owning host's NAME against this machine before
                # telling anyone to walk to it: ownership is decided by
                # host_id, and a computer name is neither unique nor
                # authoritative (a cloned image, a rebuilt box reusing the
                # name). Without this the dialog sends the user to the
                # machine they are already sitting at (field 2026-08-22).
                try:
                    me = str(socket.gethostname() or '').strip()
                except Exception:
                    me = ''
                shared = [n for n in names
                          if me and n.strip().lower() == me.lower()]
                if shared:
                    where = ('a different Convoy host that also reports the '
                             'computer name %s -- a second machine, or this '
                             'one reinstalled' % (me,))
                    howto = ('Only that host can forget them, and it is not '
                             'this Embody.')
                else:
                    where = ', '.join(names[:5])
                    howto = 'Run Forget Offline Nodes there.'
                # Say what self-cleanup ACTUALLY does. The old text promised
                # "short-lived ghosts within about an hour", which is false
                # for any row that ran longer than node_transient_lived_s --
                # those wait out the 30-day retention, and the user was
                # looking at 2h and 17h rows while being told to just wait.
                message = (
                    'No offline nodes to forget on this machine. A node is '
                    'forgotten from the computer that owns it, and the '
                    'offline row(s) here belong to %s. %s\n\n'
                    'Rows also clear themselves: about half an hour after '
                    'their project file is deleted, or about an hour if the '
                    'node only ever ran for a few minutes. Anything else '
                    'stays until it is forgotten.'
                    % (where, howto))
            self._dialog('Forget Offline Nodes', message, ['OK'])
            return

        def _age(row):
            age = row.get('last_seen_age_s')
            if not isinstance(age, (int, float)):
                return 'age unknown'
            if age >= 3600:
                return 'offline %dh' % int(age // 3600)
            if age >= 60:
                return 'offline %dm' % int(age // 60)
            return 'offline %ds' % int(age)

        shown = rows[:8]
        lines = ['- %s (%s)' % (r.get('node_name') or r.get('toe_name')
                                or r['node_id'][:8], _age(r))
                 for r in shown]
        if len(rows) > len(shown):
            lines.append('- ...and %d more' % (len(rows) - len(shown)))
        noun = ('This offline node' if len(rows) == 1
                else 'These offline nodes')
        message = (
            '%s on THIS machine will be forgotten:\n'
            '\n%s\n\n'
            'A forgotten node rejoins as a NEW identity the next time its '
            'project opens, and its TD Python approval resets. A node with '
            'a delivery still unfinished is kept -- it will be named '
            'afterwards. If any of these is a machine or project you still '
            'start remotely, Cancel.'
            % (noun, '\n'.join(lines)))
        label = 'Forget %d Node%s' % (len(rows),
                                      's' if len(rows) != 1 else '')
        if self._dialog('Forget Offline Nodes', message,
                        ['Cancel', label]) != 1:
            self._log('Forget Offline Nodes cancelled', 'INFO')
            return
        ctx = self._safeHostContext()
        if ctx is None:
            return
        ids = [r['node_id'] for r in rows]
        # THE VISUAL CONTRACT: the confirmed blocks leave the sequence
        # NOW, in this frame. The daemon apply below is reconciliation
        # the user never waits on.
        self._dropNodeRowsNow(ids)
        self._beginHostCall('forget_offline',
                            lambda: _host_forget_offline_apply(ctx, ids))

    def _reportKeptNodes(self, kept, forgotten):
        """Say WHICH rows the daemon kept and why. MAIN THREAD.

        The field's exact failure was silence here: the apply answered
        "forgot 0, kept 8 with unresolved jobs", that line was logged
        SUCCESS, and the eight rows -- optimistically removed on the
        click -- reappeared two or three seconds later with nothing on
        screen. A keep is a real outcome and gets said out loud, with
        the delivery ids the daemon named, so the work can actually be
        found and cancelled.
        """
        shown = list(kept)[:8]
        lines = []
        for entry in shown:
            if not isinstance(entry, dict):
                lines.append('- %s' % str(entry)[:8])
                continue
            label = (entry.get('name')
                     or str(entry.get('node_id') or '')[:8] or 'node')
            blocking = [b for b in (entry.get('blocking') or ())
                        if isinstance(b, dict)]
            ids = ', '.join(str(b.get('delivery_id') or '')[:12]
                            for b in blocking[:3])
            count = entry.get('pending_count') or len(blocking)
            if count:
                lines.append(
                    '- %s: %s delivery(s) still unfinished%s'
                    % (label, count, (' (%s)' % ids) if ids else ''))
                continue
            # No count and no ids: an OLDER daemon, which answers 409 with
            # prose only. Every user passes through that window -- the
            # panel updates with the .tox, the daemon a few seconds later
            # via the once-per-session auto-update -- so print the
            # daemon's own sentence rather than "0 delivery(s)".
            detail = str(entry.get('detail') or '').strip()
            lines.append('- %s: %s' % (label, detail) if detail
                         else '- %s: still has work to finish' % label)
        if len(kept) > len(shown):
            lines.append('- ...and %d more' % (len(kept) - len(shown)))
        header = ('Forgot %d node(s). ' % len(forgotten)) if forgotten else ''
        message = (
            '%s%d node(s) were KEPT because they still have work that has '
            'not finished:\n'
            '\n%s\n\n'
            'A node is only kept while a delivery it was given has not '
            'reached a verdict -- a finished result never holds a node, '
            'because results are fetched by delivery id and outlive the '
            'row. Cancel a queued delivery, or wait for the node to answer, '
            'then run Forget Offline Nodes again.'
            % (header, len(kept), '\n'.join(lines)))
        # A DISTINCT title: _messageBox keys its seeded auto-responses by
        # title, so sharing one with the confirmation would let this
        # report eat the confirmation's seeded answer in a scripted run.
        self._dialog('Forget Offline Nodes - Nodes Kept', message, ['OK'])

    def UninstallHost(self, confirm=True):
        """Remove the host app: preview (worker), then confirm, then run.

        Refuses outright if the plan touches retained paths (host.json,
        host.token, portfile, audit.jsonl, jobs/) -- checked twice, in
        plan_host_uninstall and _uninstallTargetsRetained, because A-41
        forbids uninstall as an evidence-destruction path.
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

    def _convoyRuntimeCandidates(self, venv_python=_UNSET):
        """Interpreters worth PROVING, best first. MAIN THREAD.

        A try-list, not requirements: the worker spawns each and makes it
        import cryptography + prove TLS 1.3; first pass wins. On macOS
        the venv usually CANNOT pass (TD's library-validation-signed
        python refuses foreign-signed wheels -- 'different Team IDs',
        2026-08-04), so this is a ladder down to Homebrew + daemon venv.
        NOT the Windows priority order: _host_install promotes the
        per-user daemon venv above the project venv there; this list owns
        the fallback order. Homebrew by ABSOLUTE PATH (GUI launchd PATH
        lacks /opt/homebrew); /usr/bin/python3 skipped without CLT (would
        pop Apple's install dialog from a worker).
        """
        candidates = []
        venv = (venv_python if venv_python is not _UNSET
                else self._convoyVenvPython())
        if venv:
            candidates.append(venv)
        try:
            import shutil
            # An already-built daemon venv is a candidate everywhere
            # (15 s probe beats a multi-minute rebuild; works offline).
            # Probe the exe that gets RECORDED -- pythonw.exe on Windows,
            # not a file the supervisor never launches.
            spec = self._convoyDaemonVenvSpec() or {}
            existing = spec.get('daemon_python') or spec.get('python')
            if existing and os.path.isfile(existing):
                candidates.append(existing)
            if sys.platform == 'darwin':
                for path in ('/opt/homebrew/bin/python3',
                             '/usr/local/bin/python3'):
                    if os.path.isfile(path) and path not in candidates:
                        candidates.append(path)
            clt_present = (sys.platform != 'darwin' or
                           os.path.isdir('/Library/Developer/'
                                         'CommandLineTools'))
            for name in ('python3', 'python'):
                found = shutil.which(name)
                # Skip Windows' App Execution Alias: it is a stub that can
                # open the Microsoft Store instead of running Python, which
                # is not something an install should do behind the user.
                if found and 'WindowsApps' in found:
                    continue
                # Skip the macOS developer-tools shim without the CLT for
                # the same reason: probing it opens Apple's dialog.
                if (found and not clt_present
                        and found.startswith('/usr/bin/python')):
                    continue
                if found and found not in candidates:
                    candidates.append(found)
        except Exception:
            pass
        return candidates

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

        # Already answered on this install -- never ask again (per-project
        # asking read as nagging). New projects mint silently; the
        # dangerous gates (TD Python, Full Shell) stay per-node, off.
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

    # Wait for Envoy to finish building the shared venv (fresh install =
    # minutes; the wizard enables Convoy seconds after Envoy). Budget is
    # in FRAMES: 120 x 150 ~ 5 min at 60 fps. The short give-up
    # (_awaitHostRuntime, ~10 s) covers Envoy switched off -- five
    # minutes of polling for something disabled is silence, not
    # patience.
    _HOST_RUNTIME_WAIT_FRAMES = 120
    _HOST_RUNTIME_WAIT_TRIES = 150

    @staticmethod
    def _hostRuntimeResolvable(ctx):
        """Could the host app actually run right now?

        The install's own runtime resolution, asked BEFORE committing --
        a not-yet-built runtime becomes a wait, not a failure. Kept
        beside that resolution (two drifting copies once made the status
        name a button that refused). STATIC so CI can drive it without a
        live COMP.
        """
        try:
            installer = ctx['installer']
            if installer.choose_interpreter(
                    installer.find_interpreters(ctx['platform'])):
                return True
            return bool(ctx.get('venv_python'))
        except Exception:
            return False

    def _envoyIsBringingTheEnvironment(self):
        """Is Envoy going to produce the venv Convoy shares?

        "Is it COMING", not "is it building right now": gating on the
        _bootstrapping flag always missed the 30-frame Start() deferral
        window and told the user to "Enable Envoy" right after they had
        (2026-08-09). Envoyenable is the honest signal: ON = on its way,
        OFF = the one case that needs the user.
        """
        try:
            return bool(self._embody.par.Envoyenable.eval())
        except Exception:
            return False

    def _awaitHostRuntime(self, attempt=0):
        """Retry the host install once the shared Python environment exists.

        Says which of the two situations it is, because they need opposite
        things from the user: Envoy building its venv resolves itself and
        wants patience, while Envoy switched off genuinely does need the user
        to turn it on. The old path could not tell them apart and printed the
        second message during the first.
        """
        try:
            if not self._enabled() or self._performing():
                self._clearHostLineIf('Installing...')
                return
            ctx = self._safeHostContext()
            if ctx is None:
                self._clearHostLineIf('Installing...')
                return
            if self._hostRuntimeResolvable(ctx):
                self._log('the shared Python environment is ready -- '
                          'installing the host app now', 'INFO')
                self.InstallHost(confirm=False)
                return
            building = self._envoyIsBringingTheEnvironment()
            if attempt == 0:
                if building:
                    self._log(
                        'waiting for Envoy to finish building the '
                        'Python environment Convoy shares -- the host app '
                        'installs on its own as soon as it is ready. Nothing '
                        'to do.', 'INFO')
                else:
                    self._log(
                        'no Python runtime is available for the host '
                        'app yet. Enable Envoy (it builds the environment '
                        'Convoy shares) and the host app installs itself.',
                        'WARNING')
            if not building and attempt >= 5:
                # Envoy is switched OFF: stop waiting for something nobody
                # is going to build, and leave a status the user can act
                # on. ~10 s at 60 fps -- see _HOST_RUNTIME_WAIT_FRAMES for
                # why that is a frame count and not a duration.
                self._log(
                    'giving up on the host app install: Envoy is off, so '
                    'the shared Python environment is never going to be '
                    'built. Enable Envoy and Convoy installs itself.',
                    'WARNING')
                self._hostStatus(self.HOST_INSTALL_FAILED)
                return
            if attempt >= self._HOST_RUNTIME_WAIT_TRIES:
                self._log(
                    'gave up waiting for the shared Python '
                    'environment after %d attempts -- the host app is not '
                    'installed. Use Install Host App once Envoy has finished.'
                    % (attempt,), 'WARNING')
                self._hostStatus(self.HOST_INSTALL_FAILED)
                return
            run('args[0](args[1])', self._awaitHostRuntime, attempt + 1,
                delayFrames=self._HOST_RUNTIME_WAIT_FRAMES,
                group='convoy_host_runtime_wait')
        except Exception as e:
            self._log('could not wait for the Convoy host runtime: %s' % (e,),
                      'WARNING')

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
                if not self._hostRuntimeResolvable(ctx):
                    # Fresh install enables both at once: Envoy's venv
                    # build takes minutes, Convoy follows seconds later.
                    # Wait for the environment instead of racing it
                    # (2026-08-09: the race told the user to "Enable
                    # Envoy first" right after they had). Say Installing
                    # NOW: the readout held 'Disabled' through this whole
                    # wait, which reads as a dead toggle (field
                    # 2026-08-19). The wait IS the install's first phase;
                    # the give-up paths replace the line themselves.
                    self._hostStatus(self.HOST_INSTALLING)
                    self._awaitHostRuntime()
                    return
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
        """Best-effort: clear this node's Envoy port on the local host.

        One attempt, 1 s timeout, every outcome a value (callers are a
        disable and a closing TD). `disabled` withdraws membership;
        `shutdown` clears only this runtime. Unknown intents fail closed.
        Hard kills leave a stale port -- dispatcher backoff covers it.
        blocking=True runs inline for onExit() where run() never fires
        again; worst case 3 s /health + 1 s unregister on a closing TD.
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
# Module-level on purpose: a bound method carries `self`, one attribute
# from a TD object -- the access class that froze TD in the field. Input
# is the plain ctx dict from _hostContext; output plain dicts for
# _finishHost. No td import, no frame callbacks, no Par reads, no
# logging -- the
# poll does all of that. All total: a worker that dies leaves the poll
# spinning to its cap, so _beginHostCall wraps anyway.


def _host_recorded_interpreter_exists(installed):
    """Does the Python installed.json recorded still exist?

    ONE probe shared by _host_snapshot (readout) and InstallHost
    (planner) -- their agreement is the point (b73fcd0 removed the
    readout/planner contradiction two copies produce). None = UNKNOWN,
    never False; plan_install's repair branch is strictly `is False`.
    """
    recorded = (installed or {}).get('interpreter')
    if not recorded:
        return None
    try:
        return os.path.isfile(str(recorded))
    except Exception:
        return None


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

    interpreter_exists = _host_recorded_interpreter_exists(installed)

    return _host_mark_stale_payload(ctx, installed, installer.host_state(
        installed=installed,
        probe_status=probe_status,
        supervisor=supervisor,
        version=ctx['version'],
        interpreter_exists=interpreter_exists,
        pid=pid))


def _host_mark_stale_payload(ctx, installed, state):
    """Re-derive 'running daemon older than install record'.

    Derived per snapshot, never latched (a stamped value dies on the
    next Refresh, which fires per save). Narrow on purpose -- a false
    'Needs repair' is its own defect: only over RUNNING, only when both
    versions are orderable and the running one strictly older ('source'
    is unorderable, never broken), only when the daemon answered. A
    pre-6.0.213 daemon (answers, no version) IS marked.
    """
    if not isinstance(state, dict):
        return state
    client = ctx['client']
    running = getattr(client, 'HOST_RUNNING', 'running')
    if state.get('state') != running:
        return state
    want = str((installed or {}).get('version') or '')
    if not want:
        return state
    body = _host_status_body(ctx)
    if body is None:
        return state  # could not ask -- claim nothing
    reported = body.get('app_version')
    reported = str(reported) if reported else None
    if reported == want:
        return state
    if reported is not None:
        key = getattr(ctx['installer'], 'orderable_version_key', None)
        mine, theirs = (key(want), key(reported)) if key else (None, None)
        if mine is None or theirs is None or not (theirs < mine):
            # Unorderable ('source'), or the daemon is NEWER than the record
            # -- neither is this state's business.
            return state
    state = dict(state)
    state['state'] = getattr(client, 'HOST_STALE_PAYLOAD', 'stale_payload')
    state['reported_version'] = reported
    return state


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


def _host_status_body(ctx):
    """The authenticated /status body from whatever daemon owns the endpoint.

    A FRESH probe every call -- convoy_client.probe re-reads the portfile and
    re-runs the /health identity check, so this follows a restart to the
    process that now serves, rather than remembering the one that used to.
    None when nothing answered; the caller must not read that as agreement.
    """
    client = ctx['client']
    try:
        probe = client.probe(data_dir=ctx['data_dir'])
        if not probe.use_convoy or probe.handle is None:
            return None
        code, body = client.host_get(probe.handle, '/status')
        if code == 200 and isinstance(body, dict) and body.get('ok'):
            return body
    except Exception:
        return None
    return None


def _host_settle_version(ctx, want, attempts=None, sleep=None):
    """Poll /status until the daemon reports `want` -> (reported, ok, body).

    Not one read: launchd can respawn the OLD payload for ~1 s after the
    graceful shutdown (installed.json is written last), and a single
    immediate read turned that second into a permanent 'stale' verdict
    (3x in one field log). Settling tells a restart race from a stuck
    install. `sleep` injected for fake-clock tests.
    """
    sleep = sleep or time.sleep
    if attempts is None:
        attempts = ctx.get('version_settle_attempts')
    attempts = max(1, int(5 if attempts is None else attempts))
    interval = ctx.get('version_settle_s')
    # `is None`, never `or`: a test injecting 0.0 means ZERO wait, and
    # truthiness would silently restore the two-second production interval.
    interval = 2.0 if interval is None else float(interval)
    reported, body = None, None
    for index in range(attempts):
        body = _host_status_body(ctx)
        reported = str(body.get('app_version')) if (
            body and body.get('app_version')) else None
        if reported and want and reported == str(want):
            return reported, True, body
        if index + 1 < attempts:
            sleep(interval)
    return reported, False, body


def _host_answered_without_version(body):
    """A daemon that ANSWERED /status but named no version.

    That is not silence -- it is a pre-6.0.213 payload, which predates
    version reporting entirely (convoy_hostapp._running_app_version returns
    'source' rather than nothing precisely so absence stays unambiguous). It
    is therefore the STRONGEST stale signal there is, and must not be folded
    in with 'nobody answered': doing so both mislabels it and skips the very
    automatic repair it most needs.
    """
    return isinstance(body, dict) and not body.get('app_version')


def _host_daemon_has_work(body):
    """True when the live daemon is mid-flight on real work.

    Restarting under it would strand a dispatch that stop_drain_loop can spend
    a minute unwinding, and an out-of-date daemon serving jobs correctly is a
    smaller problem than one killed halfway through them. Absent data reads as
    'no work' -- a daemon that cannot say is one we cannot protect.
    """
    if not isinstance(body, dict):
        return False
    for key in ('jobs_running', 'polls_in_flight'):
        try:
            if int(body.get(key) or 0) > 0:
                return True
        except (TypeError, ValueError):
            continue
    return False


def _host_restart_for_version(ctx):
    """ONE targeted restart so the daemon re-reads installed.json.

    THE AUTOMATIC REPAIR. By this point installed.json already names the new
    version, so any relaunch resolves the new payload -- no payload rewrite and
    no supervisor re-registration is needed, which is exactly why this is a
    restart rather than a second install(). It is also strictly what the old
    'Pulse Repair Convoy App' message asked the USER to do, so doing it here
    costs them nothing and saves a warning they would never see.

    Graceful only: the same /shutdown observers install() itself uses, then the
    supervisor's own start. Never a hard kill -- that leaves a portfile naming
    a dead pid, the hazard convoy_install documents at its uninstall path.
    """
    installer = ctx['installer']
    out = {'shutdown': _host_shutdown(ctx)}
    try:
        out['exited'] = installer._await_exit(_host_is_running(ctx))
    except Exception as e:
        out['exited'] = False
        out['detail'] = '%s: %s' % (type(e).__name__, e)
    out['started'] = installer.start(platform=ctx['platform'],
                                     uid=ctx['uid'], home=ctx['home'])
    out['healthy'] = _host_await_health(ctx, ctx.get('health_wait_s'))
    return out


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


def _host_update_decision(reported_version, our_version, version_key):
    """Should THIS Embody update the machine's LIVE Convoy App in place?

    reported_version is the daemon's own account of the code it runs
    (the register response's app_version) -- never installed.json, which
    lies exactly when an update failed to restart the process. None or
    empty means a pre-6.0.213 daemon that cannot say, which is itself
    conclusive: it predates every registry-cleanup fix.

    Strictly-older only: an equal, newer, or UNORDERABLE version (a
    'source' dev daemon, a future tagging scheme) never fires -- when in
    doubt, do not reinstall someone's daemon. Returns (action, detail)
    with action 'update' or None.
    """
    if not reported_version:
        return 'update', ('the running Convoy App predates version '
                          'reporting (pre-6.0.213)')
    theirs_text = str(reported_version)
    ours = version_key(our_version)
    theirs = version_key(theirs_text)
    if ours is None or theirs is None:
        return None, ('running version %r is not orderable against %r'
                      % (theirs_text, str(our_version)))
    if theirs < ours:
        return 'update', ('the running Convoy App is %s, this Embody '
                          'ships %s' % (theirs_text, our_version))
    return None, 'running %s is current' % (theirs_text,)


def _host_offline_rows(ctx):
    """This host's offline node rows, from the daemon's OWN projection.

    /network/nodes is used rather than the raw /nodes listing so
    online-ness comes from the daemon's single authority
    (_node_is_online) instead of a re-implementation here. Rows are
    filtered to the local host: peer rows are not ours to forget --
    their hostnames ride back separately so the all-clear dialog can
    say WHERE to act instead of reading as a button that did nothing
    (field feedback 2026-08-05: the only offline rows on the dev box
    belonged to another machine).
    Returns (rows, remote_hosts, None) or (None, None, detail).
    """
    client = ctx['client']
    try:
        probe = client.probe(data_dir=ctx['data_dir'])
    except Exception as e:
        return None, None, '%s: %s' % (type(e).__name__, e)
    if not probe.use_convoy:
        return None, None, 'no host app answered (%s)' % (probe.status,)
    code, body = client.host_get(probe.handle, '/network/nodes')
    if code != 200 or not isinstance(body, dict):
        return None, None, 'node listing failed (HTTP %s)' % (code,)
    host_id = body.get('host_id')
    rows = []
    remote_hosts = []
    for row in body.get('nodes') or []:
        if not isinstance(row, dict):
            continue
        if row.get('online'):
            continue
        if row.get('host_id') != host_id:
            name = str(row.get('hostname') or row.get('node_name')
                       or '').strip()
            if name and name not in remote_hosts:
                remote_hosts.append(name)
            continue
        node_id = str(row.get('node_id') or '')
        if not node_id:
            continue
        rows.append({'node_id': node_id,
                     'node_name': str(row.get('node_name') or ''),
                     'toe_name': str(row.get('toe_name') or ''),
                     'last_seen_age_s': row.get('last_seen_age_s')})
    return rows, sorted(remote_hosts), None


# The dialog renders at most this many sender lines; nothing beyond the
# slice is ever resolved (review: the unbounded fan-out spawned one OS
# thread per cached candidate -- up to 512 -- for 8 visible rows).
_ANNOUNCER_DISPLAY_CAP = 8


def _sanitize_hostname(name):
    """A PTR record is attacker-adjacent text bound for a trust dialog:
    keep printable ASCII only (a smuggled newline would inject fabricated
    dialog lines) and clamp the length so eight of them cannot push the
    buttons off-screen."""
    return ''.join(ch for ch in str(name or '')
                   if 0x20 <= ord(ch) <= 0x7e).strip()[:40]


def _reverse_dns_names(addresses, timeout_s=1.5):
    """Best-effort reverse-DNS: {ip: hostname} for dialog display.

    WORKER THREAD ONLY (the host slot) -- gethostbyaddr can block for
    seconds on a dead reverse zone and has no portable timeout, so each
    lookup runs on its own daemon thread and the whole batch shares one
    bounded join deadline. Input is capped at the display slice, so at
    most that many threads exist; a straggler past the deadline is an
    orphaned daemon thread that dies when its resolver gives up. A miss
    is simply absent from the result; the dialog falls back to the raw
    address (the field complaint this exists for: 'it doesn't even show
    the hostname of the machine').
    """
    results = {}

    def _lookup(ip):
        try:
            name = _sanitize_hostname(socket.gethostbyaddr(ip)[0])
        except Exception:
            # OSError is the documented miss; a non-UTF-8 PTR raises
            # UnicodeDecodeError, and an unhandled exception here dumps
            # a thread traceback into the textport (review finding).
            return
        if name:
            results[ip] = name

    threads = []
    for ip in sorted(set(a for a in addresses
                         if a))[:_ANNOUNCER_DISPLAY_CAP]:
        t = Thread(target=_lookup, args=(ip,), daemon=True)
        t.start()
        threads.append(t)
    deadline = time.monotonic() + timeout_s
    for t in threads:
        t.join(max(0.0, deadline - time.monotonic()))
    return dict(results)


def _announcer_ip(address):
    """The bare IP out of an 'ip:port' / '[v6]:port' endpoint string."""
    address = str(address or '')
    if address.startswith('['):
        end = address.find(']')
        return address[1:end] if end > 0 else ''
    return address.rsplit(':', 1)[0] if ':' in address else address


def _announcer_line(announcer):
    """Two dialog lines naming a foreign-realm sender.

    The field complaint this answers: the enumeration showed only an IP,
    which identifies nothing to a person. The ADDRESS leads -- it is the
    one field the daemon verified against the datagram's source; the
    reverse-DNS hostname follows in parentheses (a PTR record is
    attacker-adjacent text, already sanitized and clamped at ingestion,
    and must never be the primary identity). The fingerprint sits on its
    own indented continuation so the pair stays inside the dialog
    wrapper's 70-column budget instead of wrapping the fingerprint to
    column 0 (review measurement).
    """
    address = announcer.get('address') or 'unknown address'
    hostname = _sanitize_hostname(announcer.get('hostname'))
    where = '%s (%s)' % (address, hostname) if hostname else address
    return '  %s\n      %s' % (where, announcer.get('fingerprint')
                               or announcer.get('host_id') or 'unknown')


# Join is offered per LIVE foreign realm, up to this many. Beyond it the
# operator is told to silence impostors first -- a wall of join buttons
# for realms a flooder invented is not a recovery UI.
_JOIN_OFFER_CAP = 2

# Genesis-minted realm id shape. Wire realm ids are near-free text (128
# bytes of anything >= 0x20), so join buttons are offered ONLY for
# canonical ids: printable, short, collision-free labels (naive
# sanitization could collapse two hostile ids into one label).
_CANONICAL_REALM_ID_RE = re.compile(r'^cv_[0-9a-f]{16}$')


def _display_realm_id(rid):
    """A realm id bound for dialog text is LAN-supplied text too: it
    gets the same printable-ASCII clamp as a hostname. Display only --
    never feed the sanitized form back into an adopt call."""
    return _sanitize_hostname(rid) or '(unprintable id)'


def _resolve_dialog_spec(realm, announcers):
    """Pure decision core for the Resolve Realm Conflict dialog.

    Returns {'mode', 'lines', 'buttons', 'joins'} (label -> adopted realm
    id). Rules, each reversed by review once: join targets from LIVE
    announcers only (conflict_ids are a never-expiring latch -- uniting
    them hid the button or offered phantom realms; display-only); one
    join button per live foreign realm, labelled with the full id it
    adopts; copy tells the truth per branch (denylist promise only with
    live senders, every button carries its consequences).
    """
    state = str((realm or {}).get('state') or '')
    own_id = str((realm or {}).get('convoy_id') or '')
    live = sorted({str(r) for a in (announcers or [])
                   for r in (a.get('realms') or ())})
    canonical = [rid for rid in live
                 if _CANONICAL_REALM_ID_RE.match(rid)]
    joins = {}
    if 0 < len(canonical) <= _JOIN_OFFER_CAP:
        joins = {'Join %s' % rid: rid for rid in canonical}

    def _join_lines(preserved_label):
        out = []
        for label in sorted(joins):
            out.append('')
            out.append('%s: abandon %s and adopt that realm. Every '
                       'project on this machine moves -- this project '
                       'rebinds now; others offer their rejoin on their '
                       'next Convoy enable. Nothing is denylisted -- '
                       'and if an earlier Keep denylisted that machine '
                       'here, remove it from denylist.json or this '
                       'machine stays deaf to the mesh it just joined.'
                       % (label, preserved_label))
        if len(canonical) > _JOIN_OFFER_CAP:
            out.append('')
            out.append('%d foreign realms are live on the LAN right '
                       'now, so joining one is not offered -- denylist '
                       'the impostor senders first, then run this '
                       'again.' % len(canonical))
        if len(live) > len(canonical):
            out.append('')
            out.append('%d live realm id(s) are not standard Convoy '
                       'realm ids; joining those is not offered.'
                       % (len(live) - len(canonical)))
        return out

    if state == 'conflict':
        preserved = _display_realm_id(own_id) if own_id else '?'
        conflict_ids = [_display_realm_id(c)
                        for c in (realm or {}).get('conflict_ids')
                        or () if str(c) != own_id]
        lines = ["This machine's realm %s is in conflict with: %s."
                 % (preserved, ', '.join(conflict_ids) or 'unknown')]
        if announcers:
            lines.append('')
            lines.append('Live sender(s) of the foreign realm:')
            for announcer in announcers[:_ANNOUNCER_DISPLAY_CAP]:
                lines.append(_announcer_line(announcer))
            if len(announcers) > _ANNOUNCER_DISPLAY_CAP:
                lines.append('  ...and %d more'
                             % (len(announcers)
                                - _ANNOUNCER_DISPLAY_CAP))
            lines.append('')
            lines.append('Keep This Realm: stay on %s; the sender(s) '
                         'above are denylisted so they cannot re-latch '
                         'the conflict. On each of those machines, use '
                         'Join %s (or disable Convoy) so it adopts this '
                         'realm; then remove it from denylist.json '
                         'here.' % (preserved, preserved))
        else:
            lines.append('')
            lines.append('No sender of the foreign realm is announcing '
                         'RIGHT NOW (the listing covers the last ~30s), '
                         'so joining it is not possible and there is '
                         'nothing to denylist. If the conflict returns, '
                         'run this again while the sender is live, or '
                         'engage the LAN killswitch first.')
            lines.append('')
            lines.append('Keep This Realm: stay on %s and clear the '
                         'conflict record. The conflict can return if a '
                         'machine on the foreign realm announces again '
                         '-- run this again while it is live to act on '
                         'the sender.' % preserved)
        lines.extend(_join_lines(preserved))
        return {'mode': 'conflict', 'lines': lines,
                'buttons': ['Cancel', 'Keep This Realm']
                + sorted(joins), 'joins': joins}

    if announcers:
        own_label = _display_realm_id(own_id) if own_id else 'its realm'
        lines = ['No realm conflict -- this machine keeps %s.'
                 % own_label,
                 '',
                 '%d un-admitted sender(s) are advertising foreign '
                 'Convoy realms:' % len(announcers)]
        for announcer in announcers[:_ANNOUNCER_DISPLAY_CAP]:
            lines.append(_announcer_line(announcer))
        lines.append('')
        lines.append('They cannot affect this machine, but they can be '
                     'silenced (Denylist Senders). Joining one instead '
                     'ABANDONS %s -- a working realm -- for a realm '
                     'announced by an un-admitted sender; only do that '
                     'if one of the machines above is the mesh this '
                     'machine should be on.' % own_label)
        lines.extend(_join_lines(own_label))
        return {'mode': 'advisory', 'lines': lines,
                'buttons': ['Close', 'Denylist Senders']
                + sorted(joins), 'joins': joins}

    return {'mode': 'clean',
            'lines': ['No realm conflict on this machine.',
                      '',
                      'The Convoy realm is %s (%s).'
                      % (state or 'unknown',
                         _display_realm_id(own_id) if own_id
                         else 'no id')],
            'buttons': ['OK'], 'joins': {}}


def _host_realm_conflict_plan(ctx, resolve_names=True):
    """Snapshot the realm split and its live announcers. Alters nothing.

    Pure loopback HTTP; needs no spawn. The announcer provenance comes
    from the daemon's discovery candidate cache (/lan/status) -- the
    only place the 2026-08-12 conflict's sender was ever recorded.

    ``resolve_names=False`` skips the reverse-DNS pass: the automatic
    rejoin offer reuses this plan on every explicit Convoy enable and
    never displays hostnames, so it must not pay for (or fan out) the
    lookups (review finding).
    """
    client = ctx['client']
    try:
        probe = client.probe(data_dir=ctx['data_dir'])
    except Exception as e:
        return {'ok': False, 'action': 'realm_conflict_plan',
                'reason': 'no_host',
                'detail': '%s: %s' % (type(e).__name__, e)}
    if not probe.use_convoy:
        return {'ok': False, 'action': 'realm_conflict_plan',
                'reason': 'no_host',
                'detail': 'no host app answered (%s)' % (probe.status,)}
    code, status = client.host_get(probe.handle, '/status')
    if code != 200 or not isinstance(status, dict):
        return {'ok': False, 'action': 'realm_conflict_plan',
                'reason': 'status_failed',
                'detail': 'host status failed (HTTP %s)' % (code,)}
    realm = status.get('realm') or {}
    announcers = []
    code, lan = client.host_get(probe.handle, '/lan/status')
    if code == 200 and isinstance(lan, dict):
        preserved = str(realm.get('convoy_id') or '')
        for cand in (lan.get('discovery') or {}).get('candidates') or []:
            if not isinstance(cand, dict):
                continue
            states = cand.get('realm_states') or {}
            foreign = sorted(str(k) for k, v in states.items()
                             if str(v) == 'established'
                             and str(k) != preserved)
            if not foreign:
                continue
            announcers.append({
                'host_id': str(cand.get('host_id') or ''),
                'fingerprint': str(cand.get('fingerprint') or ''),
                'address': str(cand.get('address') or ''),
                'realms': foreign,
            })
    names = (_reverse_dns_names(
        [_announcer_ip(a['address'])
         for a in announcers[:_ANNOUNCER_DISPLAY_CAP]])
        if resolve_names else {})
    for announcer in announcers:
        announcer['hostname'] = names.get(
            _announcer_ip(announcer['address']), '')
    return {'ok': True, 'action': 'realm_conflict_plan', 'realm': realm,
            'announcers': announcers,
            'detail': ('realm %s; %d foreign announcer(s) live'
                       % (realm.get('state') or 'unknown',
                          len(announcers)))}


def _host_realm_conflict_apply(ctx, offenders, reset=True):
    """Denylist the named announcers, then reset the realm. LOOPBACK.

    Order is the recovery contract (the reset docstring's own advice):
    silence the senders FIRST, or the next datagram re-derives the very
    conflict the reset just cleared. Denylist failures do not abort the
    reset -- with the observer gate a stranger cannot re-wedge a
    committed realm anyway; the denylist just stops the advisories.
    """
    client = ctx['client']
    try:
        probe = client.probe(data_dir=ctx['data_dir'])
    except Exception as e:
        return {'ok': False, 'action': 'realm_conflict_resolve',
                'reason': 'no_host',
                'detail': '%s: %s' % (type(e).__name__, e)}
    if not probe.use_convoy:
        return {'ok': False, 'action': 'realm_conflict_resolve',
                'reason': 'no_host',
                'detail': 'no host app answered (%s)' % (probe.status,)}
    blocked, block_failures = [], []
    for offender in offenders or []:
        host_id = str((offender or {}).get('host_id') or '')
        fingerprint = str((offender or {}).get('fingerprint') or '')
        if not host_id and not fingerprint:
            continue
        code, out = client.host_post(probe.handle, '/peers/denylist', {
            'host_id': host_id, 'fingerprint': fingerprint,
            'reason': 'foreign realm broadcast '
                      '(Resolve Realm Conflict)'})
        body = out if isinstance(out, dict) else {}
        if code == 200:
            blocked.append(host_id or fingerprint)
        else:
            block_failures.append('%s (HTTP %s: %s)' % (
                host_id or fingerprint, code,
                body.get('detail') or body.get('reason')))
    if not reset:
        return {'ok': not block_failures,
                'action': 'realm_conflict_resolve',
                'blocked': blocked, 'block_failures': block_failures,
                'realm': None,
                'detail': ('%d sender(s) denylisted%s'
                           % (len(blocked),
                              ('; blocks failed: '
                               + '; '.join(block_failures))
                              if block_failures else ''))}
    code, out = client.host_post(probe.handle, '/realm/reset', {})
    reset_body = out if isinstance(out, dict) else {}
    ok = code == 200 and reset_body.get('ok') is True
    realm = reset_body.get('realm')
    return {'ok': ok, 'action': 'realm_conflict_resolve',
            'blocked': blocked, 'block_failures': block_failures,
            'realm': realm,
            'detail': ('realm now %s (%s); %d sender(s) denylisted%s'
                       % ((realm or {}).get('state') or 'unknown',
                          (realm or {}).get('convoy_id') or '?',
                          len(blocked),
                          ('; blocks failed: ' + '; '.join(block_failures)
                           if block_failures else ''))
                       if ok else
                       'realm reset failed (HTTP %s: %s)'
                       % (code, reset_body.get('detail')
                          or reset_body.get('reason')))}


def _host_realm_join_apply(ctx, adopt_id, senders=None):
    """Adopt the OTHER realm: an operator-confirmed id via the reset
    route. LOOPBACK.

    The opposite direction from _host_realm_conflict_apply, and
    deliberately WITHOUT any denylisting: the announcers of the adopted
    realm are the mesh being joined, not offenders. ``senders`` is the
    evidence -- the live announcers of the adopted realm -- forwarded so
    the daemon's audit can answer "on whose say-so" (the 2026-08-12
    lesson: a realm change logged without its source is unattributable),
    and so the daemon can report back any of them this machine has
    denylisted. The project-binding rebind happens back on the main
    thread in the 'realm_join' finish branch.
    """
    client = ctx['client']
    try:
        probe = client.probe(data_dir=ctx['data_dir'])
    except Exception as e:
        return {'ok': False, 'action': 'realm_join',
                'reason': 'no_host',
                'detail': '%s: %s' % (type(e).__name__, e)}
    if not probe.use_convoy:
        return {'ok': False, 'action': 'realm_join',
                'reason': 'no_host',
                'detail': 'no host app answered (%s)' % (probe.status,)}
    evidence = [{'host_id': str((s or {}).get('host_id') or ''),
                 'fingerprint': str((s or {}).get('fingerprint') or ''),
                 'address': str((s or {}).get('address') or '')}
                for s in (senders or [])[:8]]
    code, out = client.host_post(probe.handle, '/realm/reset',
                                 {'adopt_convoy_id': adopt_id,
                                  'senders': evidence})
    body = out if isinstance(out, dict) else {}
    ok = code == 200 and body.get('ok') is True
    realm = body.get('realm')
    previous = body.get('previous') if isinstance(
        body.get('previous'), dict) else {}
    return {'ok': ok, 'action': 'realm_join', 'adopted': adopt_id,
            'realm': realm,
            'denylisted_senders': (body.get('denylisted_senders')
                                   if isinstance(
                                       body.get('denylisted_senders'),
                                       list) else []),
            'detail': ('this machine left realm %s and joined %s '
                       '(now %s)'
                       % (previous.get('convoy_id') or 'none', adopt_id,
                          (realm or {}).get('state') or '?')
                       if ok else
                       'realm join failed (HTTP %s: %s)'
                       % (code, body.get('detail')
                          or body.get('reason')))}


def _host_forget_offline_plan(ctx):
    """List this host's offline rows for the confirmation. Alters nothing."""
    rows, remote_hosts, err = _host_offline_rows(ctx)
    if rows is None:
        return {'ok': False, 'action': 'forget_offline_plan',
                'reason': 'no_host', 'detail': err}
    return {'ok': True, 'action': 'forget_offline_plan', 'rows': rows,
            'remote_offline_hosts': remote_hosts,
            'detail': '%d offline node(s) listed' % len(rows)}


def _host_forget_offline_apply(ctx, node_ids):
    """Forget the confirmed rows, re-verifying each is STILL offline.

    The daemon re-checks its own refusal rules per node (/nodes/forget:
    an UNFINISHED delivery is a 409 keep -- an uncollected result is
    not, since it outlives the row); a row that came back online between
    the dialog and now is skipped here, never forgotten.

    A 409 keeps the daemon's own explanation AND the delivery ids that
    pin the row, so the panel can say which node was kept and why. That
    prose was previously read off the wire and thrown away.
    """
    client = ctx['client']
    rows, _remote, err = _host_offline_rows(ctx)
    if rows is None:
        return {'ok': False, 'action': 'forget_offline',
                'reason': 'no_host', 'detail': err}
    still_offline = {r['node_id'] for r in rows}
    try:
        probe = client.probe(data_dir=ctx['data_dir'])
    except Exception as e:
        return {'ok': False, 'action': 'forget_offline',
                'reason': 'no_host',
                'detail': '%s: %s' % (type(e).__name__, e)}
    if not probe.use_convoy:
        return {'ok': False, 'action': 'forget_offline',
                'reason': 'no_host',
                'detail': 'no host app answered (%s)' % (probe.status,)}
    names = {}
    for row in rows:
        label = str(row.get('node_name') or row.get('toe_name') or '').strip()
        if label:
            names[row['node_id']] = label
    forgotten, kept_busy, skipped, failed = [], [], [], []
    for node_id in node_ids:
        if node_id not in still_offline:
            skipped.append(node_id)
            continue
        code, body = client.host_post(probe.handle, '/nodes/forget',
                                      {'node_id': node_id})
        if code == 200:
            forgotten.append(node_id)
        elif code == 409:
            kept_busy.append({
                'node_id': node_id,
                'name': names.get(node_id, ''),
                'detail': str((body or {}).get('detail') or ''),
                'pending_count': (body or {}).get('pending_count'),
                'blocking': list((body or {}).get('blocking') or ()),
            })
        elif code == 404:
            skipped.append(node_id)
        else:
            failed.append('%s (HTTP %s: %s)' % (
                node_id[:8], code,
                (body or {}).get('reason') or 'unknown'))
    bits = ['forgot %d' % len(forgotten)]
    if kept_busy:
        bits.append('kept %d with unfinished deliveries' % len(kept_busy))
    if skipped:
        bits.append('skipped %d (online again or already gone)'
                    % len(skipped))
    if failed:
        bits.append('failed %d: %s' % (len(failed), '; '.join(failed[:3])))
    return {'ok': not failed, 'action': 'forget_offline',
            'forgotten': forgotten, 'kept_busy': kept_busy,
            'skipped': skipped, 'failed': failed,
            'detail': ', '.join(bits)}


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


# One bounded uv reinstall of the cryptography pin. Sized for a cold
# wheel fetch on a slow link, and accounted for in HOST_POLL_ATTEMPTS --
# raising this without re-checking that budget makes Install report
# timed_out over a repair that later succeeds.
VENV_REPAIR_TIMEOUT_S = 120.0


def _host_repair_venv_runtime(ctx, runner=None):
    """ONE-SHOT venv repair from the WORKER: reinstall the crypto pin.

    Re-resolves the wheel for what the interpreter reports TODAY (fixes
    arch-swap staleness; useless for the macOS signature refusal, which
    the caller gates out). Once, never a loop. Worker-safe: ctx data +
    injected runner only; --link-mode copy mirrors UV_LINK_MODE,
    --no-cache keeps a poisoned wheel from returning.
    """
    installer = ctx['installer']
    uv = ctx.get('uv')
    target = ctx.get('venv_python_repair') or ctx.get('venv_python')
    deps = [str(d) for d in (ctx.get('venv_crypto_deps') or ()) if d]
    if not uv or not target or not deps:
        return {'ok': False, 'reason': 'no_repair_context',
                'detail': 'uv, the venv interpreter, or the cryptography '
                          'pin is unknown to this session'}
    call = runner or installer.run_command
    code, out, err = call(
        [uv, 'pip', 'install', '--reinstall', '--no-cache',
         '--link-mode', 'copy'] + deps + ['--python', target],
        timeout_s=VENV_REPAIR_TIMEOUT_S)
    if code != 0:
        text = str(err or out or 'uv exited %s' % (code,)).strip()
        return {'ok': False, 'reason': 'venv_repair_failed',
                'detail': text[-400:]}
    return {'ok': True, 'reason': 'venv_repaired',
            'detail': 'reinstalled %s into the venv' % ', '.join(deps)}


# The dedicated daemon venv build, bounded per step. Machine-scoped like
# the managed runtime, never project-scoped: one working interpreter
# serves every Embody project on the box.
DAEMON_VENV_CREATE_TIMEOUT_S = 60.0
DAEMON_VENV_INSTALL_TIMEOUT_S = 120.0


def _host_build_daemon_venv(ctx, runner=None):
    """Build a DEDICATED daemon venv from a non-TD base Python. WORKER.

    macOS: TD's library-validation-signed python refuses foreign wheels
    forever, so the daemon gets its own venv from a Python outside TD's
    signature domain (Homebrew / CLT >= 3.11). One version-gated base,
    no loop; --clear rebuilds stale fallbacks; caller re-probes with the
    standard verifier. Windows builds it for durability instead: the
    project venv is project-scoped state a machine-scoped daemon must
    not pin (move/delete the project = dead daemon at logon). uv drives
    the CONSOLE python.exe; what is probed and recorded is pythonw.exe,
    the exe the supervisor actually launches.
    """
    installer = ctx['installer']
    platform = ctx.get('platform') or sys.platform
    uv = ctx.get('uv')
    deps = [str(d) for d in (ctx.get('venv_crypto_deps') or ()) if d]
    spec = ctx.get('daemon_venv') or {}
    venv_dir = spec.get('dir')
    venv_python = spec.get('python')
    daemon_python = spec.get('daemon_python') or venv_python
    bases = [str(b) for b in (spec.get('bases') or ()) if b]
    if not uv or not deps or not venv_dir or not venv_python:
        return {'ok': False, 'reason': 'no_daemon_venv_context',
                'detail': 'uv, the cryptography pin, or the daemon venv '
                          'location is unavailable'}
    if not bases:
        # NAME THE REMEDY FOR THIS PLATFORM. Telling a Windows user to
        # `brew install python` is worse than saying nothing: it is a
        # confident instruction that cannot be followed.
        if platform == 'win32':
            detail = ('no Python 3.11+ was found outside TouchDesigner '
                      '(looked in Program Files, the per-user '
                      'Programs\\Python folder, and uv\'s managed '
                      'pythons) -- install Python from python.org, then '
                      'enable Convoy again')
        else:
            detail = ('no Python outside TouchDesigner exists at '
                      '/opt/homebrew/bin/python3, '
                      '/usr/local/bin/python3, or /usr/bin/python3 '
                      '-- install Homebrew Python (brew install '
                      'python), then enable Convoy again')
        return {'ok': False, 'reason': 'no_base_interpreter',
                'names_remedy': True, 'detail': detail}
    call = runner or installer.run_command
    base = None
    for candidate in bases:
        code, out, err = call(
            [candidate, '-I', '-c',
             'import sys; print("%d.%d" % sys.version_info[:2])'],
            timeout_s=15.0)
        if code != 0:
            continue
        try:
            major, minor = (int(x) for x in str(out).strip().split('.'))
        except (TypeError, ValueError):
            continue
        if (major, minor) >= (3, 11):
            base = candidate
            break
    if base is None:
        if platform == 'win32':
            detail = ('none of the interpreters found outside '
                      'TouchDesigner reported Python 3.11+ -- install '
                      'Python from python.org, then enable Convoy again')
        else:
            detail = ('no Python 3.11+ outside TouchDesigner was '
                      'found to host the Convoy venv (Apple\'s '
                      'command-line-tools python3 is older) -- '
                      'install Homebrew Python (brew install '
                      'python), then enable Convoy again')
        return {'ok': False, 'reason': 'no_usable_base_python',
                'names_remedy': True, 'detail': detail}
    code, out, err = call(
        [uv, 'venv', venv_dir, '--clear', '--python', base],
        timeout_s=DAEMON_VENV_CREATE_TIMEOUT_S)
    if code != 0 and '--clear' in str(err or ''):
        # Pre-0.8 uv does not know --clear (it replaced venvs by
        # default); one retry without it keeps ancient user uvs working.
        code, out, err = call(
            [uv, 'venv', venv_dir, '--python', base],
            timeout_s=DAEMON_VENV_CREATE_TIMEOUT_S)
    if code != 0:
        text = str(err or out or 'uv venv exited %s' % (code,)).strip()
        return {'ok': False, 'reason': 'daemon_venv_build_failed',
                'detail': text[-400:]}
    code, out, err = call(
        [uv, 'pip', 'install', '--no-cache', '--link-mode', 'copy']
        + deps + ['--python', venv_python],
        timeout_s=DAEMON_VENV_INSTALL_TIMEOUT_S)
    if code != 0:
        text = str(err or out or 'uv pip exited %s' % (code,)).strip()
        return {'ok': False, 'reason': 'daemon_venv_build_failed',
                'detail': text[-400:]}
    # Make the windowless half real: uv <= 0.11.x writes CONSOLE
    # trampolines for pythonw.exe (uv#19226) -> terminal window at logon.
    # Unconditional (a newer uv fixes nothing already on disk), never
    # fatal (cosmetic; refusing would leave no daemon venv at all).
    window_note = ''
    windowless = installer.ensure_windowless_daemon_python(
        venv_dir, base, platform=platform)
    if not windowless.get('ok'):
        window_note = (
            ' The daemon interpreter could not be made windowless (%s), '
            'so a console window may appear at logon.'
            % (windowless.get('detail') or windowless.get('reason'),))
    elif windowless.get('note'):
        window_note = ' %s.' % (windowless['note'],)
    # Probe/record the SUPERVISOR's interpreter (pythonw.exe), but fall
    # back to python.exe if uv ever stops shipping it: a console window
    # annoys; a recorded-but-absent path is a silent launcher refusal
    # every minute forever.
    chosen = daemon_python
    if chosen != venv_python and not os.path.isfile(chosen):
        chosen = venv_python
    return {'ok': True, 'reason': 'daemon_venv_built',
            'python': chosen, 'console_python': venv_python, 'base': base,
            'note': window_note,
            'detail': 'built a dedicated Convoy venv from %s' % (base,)}


def _host_ensure_windowless_daemon(ctx, daemon_python):
    """Repair an already-built daemon venv's console pythonw.exe. WORKER.

    Reuse-path counterpart of the build gate (uv#19226 venvs are kept
    forever -- one bad build = terminal window at every logon for life).
    Returns a NOTE ('' = nothing to say); never fails an install.
    ALWAYS calls the installer, even when already windowless: the only
    sequence leaving pythonw.exe.old-* behind ends GUI, so a GUI-skip
    rung never swept them. Fast path = one PE read + one listdir.
    """
    installer = ctx['installer']
    spec = ctx.get('daemon_venv') or {}
    # No base is passed: an existing venv names its own in pyvenv.cfg,
    # which is the same answer the repaired interpreter uses at run time.
    fixed = installer.ensure_windowless_daemon_python(
        spec.get('dir'), platform=ctx.get('platform'))
    if not fixed.get('ok'):
        # SAY WHAT IS ACTUALLY THERE -- three different things, and only
        # one of them is a console window. A denied repair that could not
        # put the original back leaves NO interpreter at the recorded
        # path; a denied repair whose file cannot even be READ (usually
        # the same process that denied the write is holding it) proves
        # nothing about a window at all.
        if fixed.get('interpreter_missing'):
            note = (' WARNING: the Convoy venv has no daemon interpreter '
                    'at %s after a denied repair (%s).'
                    % (daemon_python, fixed.get('detail')
                       or fixed.get('reason')))
        elif fixed.get('interpreter_unreadable'):
            note = (' The existing Convoy venv\'s daemon interpreter could '
                    'not be replaced or read just now, so whether it opens '
                    'a console window at logon is unverified (%s).'
                    % (fixed.get('detail') or fixed.get('reason'),))
        else:
            note = (' The existing Convoy venv still has a console daemon '
                    'interpreter, so a console window may appear at logon '
                    '(%s).' % (fixed.get('detail') or fixed.get('reason'),))
        return note + _host_kept_note(fixed)
    if not fixed.get('repaired'):
        # Already windowless, unreadable-so-left-alone, or another process
        # got there first. The sweep may still have run, so a leftover it
        # could not remove is worth one sentence.
        if fixed.get('note'):
            return ' %s.%s' % (fixed['note'], _host_kept_note(fixed))
        return _host_kept_note(fixed)
    note = (' The existing Convoy venv had a console daemon interpreter '
            '(older uv) and was repaired in place.')
    if fixed.get('note'):
        note += ' %s.' % (fixed['note'],)
    return note + _host_kept_note(fixed)


def _host_kept_note(fixed):
    """Name a leftover image the repair could not delete yet. WORKER.

    Expected, not exceptional: a running daemon holds its own exe open,
    so the old image survives until it exits and the NEXT install sweeps
    it. Naming it beats leaving a mystery multi-megabyte file beside the
    interpreter.

    TWO DIFFERENT LEFTOVERS, and calling them the same thing was a lie in
    one direction: `kept` carries what THIS repair renamed aside, but on
    a call that repaired nothing it carries what an EARLIER repair left
    and the sweep still could not remove. Only the first is "the replaced
    interpreter".
    """
    kept = fixed.get('kept') or ()
    if not kept:
        return ''
    if fixed.get('repaired'):
        return (' The replaced interpreter is still in use and was left as '
                '%s; it is removed on the next install.' % (kept[-1],))
    return (' A leftover from an earlier repair could not be removed yet '
            '(%s); it goes at the next install.' % (kept[-1],))


def _host_install(ctx, modules, interpreter, supervisor=None,
                  venv_runtime=False, repair_only=False):
    """Write the payload, register the supervisor, start it, wait.

    repair_only = same probe ladder, no payload write: repair_runtime
    re-points an existing install (the newer-Embody case install() must
    refuse) -- shared ladder so the paths cannot drift. Worker-safe;
    venv_runtime interpreters are verified by the LIVE crypto-floor
    probe, not the managed-runtime receipt.
    """
    installer = ctx['installer']
    verifier = None
    rejected = []
    success_note = ''
    action = 'repair_runtime' if repair_only else 'install'
    # Separate from repair_note; read on every path and must survive a
    # SUCCESS (a fallback still installs a working daemon). Carries why
    # the per-user venv was not used, or what had to be done to it --
    # never what won instead (that sentence is added once, where the
    # winner is known). ACCUMULATES, never overwrites: reassignment once
    # made the loudest warning unreachable. The 'moved or deleted'
    # sentence below is gated on the winner being ctx['venv_python'].
    daemon_note = ''
    if venv_runtime:
        def verifier(data_dir, interp, platform=None, architecture=None,
                     runner=None):
            return installer.probe_runtime(
                interp, platform, architecture, runner=runner)
        # PROVE the interpreter (spawn it, import cryptography, prove
        # TLS 1.3) -- a pip-filled venv can still be useless to the
        # daemon (macOS 'different Team IDs', 2026-08-04). First pass
        # wins; none => repair and daemon-venv rungs below.

        def _probe(candidate):
            probe = verifier(ctx['data_dir'], candidate,
                             ctx['platform'], None, None)
            if isinstance(probe, dict) and probe.get('ok'):
                return True
            record = probe if isinstance(probe, dict) else {}
            detail = str(record.get('detail') or '')
            rejected.append({
                'candidate': candidate,
                'reason': record.get('reason') or 'probe failed',
                'detail': detail,
                'snippet': installer.probe_detail_snippet(detail),
            })
            return False

        chosen = None
        repair_note = ''
        repaired = False
        remedy_named = False

        # Windows order INVERTS macOS: there the daemon venv is a last
        # resort; here it is PREFERRED, because a clean-probing project
        # venv wins rung one and pins a machine-scoped daemon to one
        # project's directory (move/delete = dead at logon, silently).
        # Durability, not capability -- and a preference, never a
        # requirement: building needs uv + 3.11 + network, which a show
        # LAN lacks, so a failed build falls through to the ladder.
        daemon_spec = ctx.get('daemon_venv') or {}
        daemon_python = (daemon_spec.get('daemon_python')
                         or daemon_spec.get('python'))
        prefer_daemon_venv = (ctx['platform'] == 'win32'
                              and bool(daemon_python))
        if prefer_daemon_venv:
            # THREE-WAY: absent -> build; windowless -> reuse untouched;
            # console/unreadable -> repair in place then reuse (rebuild
            # is wrong: the venv probes healthy and --clear would delete
            # an exe the LIVE daemon holds open; uv#19226).
            reuse_note = ''
            if os.path.isfile(daemon_python):
                reuse_note = _host_ensure_windowless_daemon(
                    ctx, daemon_python)
            if os.path.isfile(daemon_python) and _probe(daemon_python):
                # A healthy venv from a previous install: seconds, and it
                # works offline. Never rebuild what already proves itself.
                chosen = daemon_python
                daemon_note = reuse_note
            else:
                # ACCUMULATE, never reassign: the loudest note ("no
                # daemon interpreter, surviving image at X") is exactly
                # the state that sends control here, and overwriting made
                # it unreachable in production while a stub kept the test
                # green.
                built = _host_build_daemon_venv(ctx)
                if built.get('ok') and _probe(built['python']):
                    chosen = built['python']
                    success_note = (
                        ' (daemon runs under a dedicated per-user Convoy '
                        'venv built from %s)' % (built.get('base'),))
                    daemon_note = reuse_note + str(built.get('note') or '')
                else:
                    if built.get('ok'):
                        why = 'the venv was built but its probe failed'
                    else:
                        why = str(built.get('detail')
                                  or built.get('reason') or 'unknown')
                        remedy_named = bool(built.get('names_remedy'))
                    # Not fatal -- but say plainly WHY, because the cost
                    # of the fallback is invisible until the day it bites.
                    daemon_note = reuse_note + (
                        ' A dedicated per-user Convoy venv could not be '
                        'used (%s).' % (why,))

        if chosen is None:
            for candidate in ([interpreter] + [
                    c for c in (ctx.get('runtime_candidates') or ())
                    if c != interpreter]):
                if prefer_daemon_venv and candidate == daemon_python:
                    # Already probed above; a second 15 s spawn would only
                    # add a duplicate rejection record.
                    continue
                if _probe(candidate):
                    chosen = candidate
                    break
        venv_python = ctx.get('venv_python')
        venv_reasons = [r['reason'] for r in rejected
                        if r['candidate'] == venv_python]
        if chosen is None and venv_python and venv_reasons:
            if any(reason in ('runtime_crypto_broken',
                              'runtime_missing_cryptography')
                   for reason in venv_reasons):
                # The venv failed in a way a reinstall can plausibly fix.
                # Reinstall the crypto pin once, re-probe once -- rejected
                # keeps BOTH probe records for the venv so the aggregate
                # shows before and after honestly.
                repair = _host_repair_venv_runtime(ctx)
                if repair.get('ok') and _probe(venv_python):
                    chosen = venv_python
                    repaired = True
                elif repair.get('ok'):
                    repair_note = (' Repair was attempted (reinstalled '
                                   'the cryptography pin into the venv) '
                                   'and the re-probe still failed.')
                else:
                    repair_note = ' Venv repair not possible: %s.' % (
                        repair.get('detail') or repair.get('reason'),)
            elif 'runtime_crypto_signature_blocked' in venv_reasons:
                # Code-signing policy is not a package problem: any wheel
                # a reinstall fetches carries the same foreign signature.
                repair_note = (' Venv repair skipped: reinstalling cannot '
                               'change code-signing policy.')
        if chosen is None and ctx.get('daemon_venv') and not prefer_daemon_venv:
            # No existing interpreter can serve the daemon -- build one
            # OUTSIDE TouchDesigner's signature domain and prove it with
            # the same probe every other candidate faced. Skipped when the
            # build was already attempted as the FIRST rung above: a
            # second attempt would repeat a failure that has not changed.
            built = _host_build_daemon_venv(ctx)
            if built.get('ok') and _probe(built['python']):
                chosen = built['python']
                success_note = (' (daemon runs under a dedicated Convoy '
                                'venv built from %s)' % (built.get('base'),))
                daemon_note += str(built.get('note') or '')
            elif built.get('ok'):
                # Built fine, still refused by the probe -- say THAT, not
                # the build's success line inside a failure message.
                repair_note += (' Daemon venv: built from %s but its '
                                'probe still failed.' % (built.get('base'),))
            else:
                remedy_named = remedy_named or bool(built.get('names_remedy'))
                repair_note += ' Daemon venv: %s.' % (
                    built.get('detail') or built.get('reason'),)
        if chosen is None:
            described = []
            for entry in rejected[:6]:
                described.append('%s (%s%s)' % (
                    entry['candidate'], entry['reason'],
                    ': ' + entry['snippet'] if entry['snippet'] else ''))
            guidance = ''
            if remedy_named:
                # The daemon-venv note already names the way out for THIS
                # platform; a second copy of the same advice is noise.
                # (A flag, not a substring match on 'brew install python':
                # that match silently stopped working the moment the
                # Windows branch started naming python.org instead.)
                pass
            elif any(r['reason'] == 'runtime_crypto_signature_blocked'
                     for r in rejected):
                guidance = (" macOS refused to load cryptography into "
                            "TouchDesigner's bundled Python: it is "
                            'code-signed with library validation and may '
                            'not load third-party native modules when run '
                            'standalone. Reinstalling or rebuilding the '
                            'venv cannot fix this -- Convoy needs a '
                            'Python outside TouchDesigner: install '
                            'Homebrew Python (brew install python), then '
                            'enable Convoy again.')
            elif any(r['reason'] == 'runtime_crypto_broken'
                     for r in rejected):
                guidance = (' A cryptography that is installed but cannot '
                            'load usually means the venv was built for '
                            'another CPU architecture -- toggle Envoy off '
                            'and on to rebuild the environment, then '
                            'enable Convoy again.')
            if rejected and all(r['reason'] == 'runtime_spawn_blocked'
                                for r in rejected):
                # EVERY candidate died before Python ran: the OS refused
                # the spawn, not the interpreters. Historical culprit was
                # our own run_command inheriting a non-duplicatable stdin
                # (fixed 2026-08-19, stdin=DEVNULL); what remains is a
                # genuinely restricted session, so report session facts
                # and prescribe nothing absolute.
                # getattr: test fakes drive this ladder without the helper.
                summarize = getattr(installer, 'spawn_environment_summary',
                                    None)
                try:
                    facts = str(summarize() or '') if summarize else ''
                except Exception:
                    # The diagnostic must never replace the verdict it
                    # decorates -- this is the path where nothing spawns.
                    facts = ''
                return {'ok': False, 'action': action,
                        'reason': 'spawn_blocked',
                        'rejected': rejected[:8],
                        'spawn_environment': facts,
                        'detail':
                            'every interpreter candidate failed the same '
                            'way before Python ran (' + ', '.join(
                                sorted({(r.get('snippet') or r.get('detail')
                                         or r.get('reason') or '')[:60]
                                        for r in rejected if r})) + ') -- the '
                            'operating system refused to start the child '
                            'process, so no interpreter could be tested. '
                            'This is the session, not your Python: the '
                            'process that launched TouchDesigner is '
                            'restricting child processes'
                            + (' (' + facts + ')' if facts else '') + '. '
                            'If a supervisor or service runs TouchDesigner, '
                            'check its job-object limits; opening the '
                            'project in a normally launched TouchDesigner '
                            'and enabling Convoy again also works. '
                            'Tried: ' + '; '.join(described) + '.'}
            return {'ok': False, 'action': action,
                    'reason': 'no_usable_runtime',
                    # 8, not 6: the fullest macOS ladder produces up to
                    # eight records (six candidates + the repair re-probe
                    # + the daemon-venv probe), and the LAST one is the
                    # most decision-relevant -- never slice it away.
                    'rejected': rejected[:8],
                    'detail': 'no interpreter on this machine could load '
                              'cryptography and TLS 1.3. Tried: '
                              + '; '.join(described) + '.'
                              + repair_note + daemon_note + guidance}
        interpreter = chosen
        if repaired:
            # The venv was MUTATED on the way to success -- say so. A
            # background reinstall with no record is the next 'why did
            # my first enable take a minute' diagnosis round trip.
            success_note = ' (venv cryptography was repaired first)'
    if repair_only:
        # NO version, NO modules -- the signature is what makes a
        # downgrade unrepresentable rather than merely untested.
        outcome = installer.repair_runtime(
            ctx['data_dir'], interpreter,
            platform=ctx['platform'], home=ctx['home'], uid=ctx['uid'],
            installed_by=ctx['installed_by'], runtime_verifier=verifier,
            shutdown=lambda: _host_shutdown(ctx),
            is_running=_host_is_running(ctx))
    else:
        outcome = installer.install(
            ctx['data_dir'], ctx['version'], modules, interpreter,
            platform=ctx['platform'], home=ctx['home'], uid=ctx['uid'],
            installed_by=ctx['installed_by'], supervisor=supervisor,
            runtime_verifier=verifier,
            # Repair over a RUNNING daemon: same graceful observers stop()
            # uses, so the installer can ask the old daemon to exit and wait
            # before re-registering (darwin would otherwise EIO at the
            # still-loaded label; win32 would leave the old code running).
            shutdown=lambda: _host_shutdown(ctx),
            is_running=_host_is_running(ctx))
    if not outcome.get('ok'):
        return {'ok': False, 'action': action, 'outcome': outcome,
                'reason': outcome.get('reason'),
                'detail': outcome.get('detail')}
    started = None
    healthy = None
    verified = None
    reported = None
    repaired_restart = None
    if outcome.get('registered'):
        # An external supervisor registered nothing, so there is nothing
        # here for us to start -- A-36's rule is never two supervisors,
        # and that includes never poking someone else's.
        started = installer.start(platform=ctx['platform'], uid=ctx['uid'],
                                  home=ctx['home'])
        healthy = _host_await_health(ctx, ctx.get('health_wait_s'))
        # Install lie detector (2026-08-05: a stale payload shipped while
        # installed.json claimed the new version): the one honest witness
        # is the daemon's own /status app_version. SETTLE, then repair,
        # then report (2026-08-16: launchd respawns the OLD payload for
        # ~1 s; one immediate read made permanent verdicts). Compare
        # against the INSTALLED version, not ours -- a runtime repair
        # keeps a newer record, and ctx would make the detector lie.
        want = outcome.get('version')
        reported, matched, body = _host_settle_version(ctx, want)
        # ANSWERED counts, with or without a version. A versionless answer is
        # a pre-6.0.213 payload -- conclusive staleness, not silence.
        answered = reported is not None or _host_answered_without_version(body)
        if not matched and answered:
            if _host_daemon_has_work(body):
                # Work in flight outranks a version. Report the mismatch
                # instead of stranding a dispatch mid-forward.
                repaired_restart = {'skipped': 'daemon busy'}
            else:
                repaired_restart = _host_restart_for_version(ctx)
                reported, matched, body = _host_settle_version(ctx, want)
                answered = (reported is not None
                            or _host_answered_without_version(body))
        # None (nobody answered at all) is NOT False (answered, wrong or
        # unnameable version).
        verified = bool(matched) if answered else None
    detail = ('%s %s under %s%s%s'
              % ('re-pointed the installed' if repair_only else 'installed',
                 outcome.get('version'), interpreter, success_note,
                 daemon_note))
    if daemon_note and interpreter == ctx.get('venv_python'):
        # ONLY when the project venv actually won. A system Python that
        # wins the ladder is not project-scoped, and saying so would be
        # a new false statement in place of the one just removed.
        detail += (' The daemon is running from this project and will '
                   'stop working if the project is moved or deleted.')
    if verified is False:
        tried = ('and restarting it once'
                 if repaired_restart and 'skipped' not in repaired_restart
                 else '(it was busy with running work, so it was NOT '
                      'restarted)')
        detail += (' -- WARNING: the daemon still reports %r, not the '
                   'installed %s, after waiting for it to settle %s. '
                   'The Convoy App on this machine is running older code '
                   'than this Embody installed; Repair Convoy App is the '
                   'manual retry.'
                   % (reported, outcome.get('version'), tried))
    elif verified is None and outcome.get('registered'):
        # Not verified is not the same as verified: say the check was
        # inconclusive rather than letting a slow or unreachable daemon
        # read as a clean, confirmed install.
        detail += (' -- version unverified: the restarted daemon did '
                   'not answer in time')
    elif repaired_restart:
        # It DID converge, but only after the restart. Say so: a silent
        # self-heal is the next 'why was that install slow' round trip.
        detail += (' -- the daemon was restarted once to pick up the new '
                   'payload, and now reports %s' % (reported,))
    # THE READOUT comes from _host_snapshot, which re-derives the stale state
    # itself (see _host_mark_stale_payload). It is deliberately NOT stamped
    # from `verified` here: this snapshot is taken after the settle and the
    # restart, so it is the FRESHER witness -- a daemon that came good in
    # between must read as running, and a stamp from the older evidence would
    # overrule it with a warning that is no longer true.
    state = _host_snapshot(ctx)
    result = {'ok': True, 'action': action, 'outcome': outcome,
              'started': started, 'healthy': healthy,
              'version_verified': verified,
              'restart_retry': repaired_restart,
              'detail': detail,
              'state': state}
    if rejected:
        # Probe rejections ride along on SUCCESS too: a venv that failed
        # its probe while a later candidate passed (or was repaired) is
        # evidence the next diagnosis needs, and dropping it re-creates
        # the information-loss failure this path exists to end.
        result['rejected'] = rejected[:8]
    return result


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
