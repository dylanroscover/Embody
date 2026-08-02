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

THREADING (rung 4, UpdaterExt's shape verbatim)

Resolve everything on the main thread -> daemon worker doing pure-Python
urllib with ZERO TD access -> publish a generation-tagged plain dict to a
plain attribute -> bounded main-thread run(delayFrames=15) poll chain with
a stale-instance guard. Including, critically, the convoy_client MODULE
ITSELF: a `mod.` reference is a live DAT lookup, i.e. a TD access that
re-resolves on every attribute get, so binding the module inside the
worker body would be a threading violation. It is captured in a local by
_beginCall BEFORE the thread is created. See _client(), which holds the
one and only such reference in this file.

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

import os
import re
import sys
import time

# A payload entry is a BARE FILENAME and nothing else. The accept-list
# is convoy_install._BARE_NAME_OK's, deliberately duplicated at the READ
# site rather than trusted from the write site: these names come off DAT
# parameters a user can edit, and write_payload would reject them anyway
# -- catching it here means a mis-named DAT is reported as a missing
# module instead of failing the whole install.
_BARE_MODULE_NAME = re.compile(r"^[A-Za-z0-9._+-]+\.py$")


class ConvoyExt:
    """Node-side Convoy registration: reconcile, register, heartbeat."""

    # Consent scope recorded beside the convoy id (A-13). Phase 3's LAN
    # widening must ask again rather than inherit this grant.
    CONSENT_SCOPE = 'local host app only'

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
        self._post_init_done = False
        self._logged = ''        # last logged status class (transitions only)
        self._tick_ms = self.TICK_MIN_MS

        # Arm the reconcile loop for THIS instance, tagged with a monotonic
        # generation stored on the COMP so it survives the reinit storm a
        # save produces. Only the newest generation's tick proceeds; the rest
        # exit as stale without rescheduling. Same shape, and the same
        # reasoning, as EnvoyExt's watchdog arming.
        gen = ownerComp.fetch('_convoy_gen', 0) + 1
        ownerComp.store('_convoy_gen', gen)
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
            self._publishId(self._readConvoyId())
            if not self._enabled() and not self._performing():
                self._status('Disabled')
        except Exception as e:
            self._log('post-init readout failed: %s' % (e,), 'DEBUG')

    def onDestroyTD(self):
        """Called on the OLD instance before a reinit replaces it.

        There is deliberately nothing to tear down and NOTHING to
        unregister: a reinit is not a disable, and the registration state
        that matters (runtime_id, node_id) lives in the per-process session
        precisely so it survives this. Dropping the worker slot just stops
        a superseded result from being read by the new instance; the poll
        chain retires itself through _staleInstance.
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

    def _status(self, text):
        # getattr, not direct access: this extension can land in a session
        # whose Convoy page has not been created yet (and in an upgraded
        # .tox that predates it). Same guard UpdaterExt._status uses for
        # Updatestatus.
        par = getattr(self._embody.par, 'Convoystatus', None)
        if par is not None:
            self._setPar(par, str(text)[:160])

    def _publishId(self, convoy_id):
        """Project .embody/project.json's convoy id into the read-only par.

        Registered in EmbodyExt._TRANSIENT_STATUS_PARS (resting ''), which
        is what stops THIS machine's convoy id from baking into the tracked
        Embody.tdn and into every released .tox.
        """
        par = getattr(self._embody.par, 'Convoyid', None)
        if par is not None:
            self._setPar(par, str(convoy_id or ''))

    def _enabled(self):
        par = getattr(self._embody.par, 'Convoyenable', None)
        try:
            return bool(par.eval()) if par is not None else False
        except Exception:
            return False

    def _setEnabled(self, value):
        """Flip the canonical gate (Toggle pars are 0/1)."""
        par = getattr(self._embody.par, 'Convoyenable', None)
        if par is not None:
            par.val = 1 if value else 0

    def _performing(self):
        """True while Embody's Perform Mode is on.

        Single authority: EmbodyExt._performMode (the live Performmode par),
        the same signal Envoy's watchdog consults -- never a status string,
        which every writer here overwrites. An exception reads False, so a
        broken Embody ext reference cannot silently switch Convoy off
        forever; that matches EnvoyExt._performModeActive.
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
        if self._performing():
            # Perform Mode: ZERO network work, and the status par is left
            # exactly as the show found it. Resumes on exit.
            self._tick_ms = self.TICK_MAX_MS
            return
        if self._busy:
            # A call is already in flight; its poll owns the next schedule.
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
            if session.get('registered'):
                # Disabled without going through parexec (a settings restore,
                # or a scripted par write). Clear the host's port once.
                self._beginCall('unregister', client, session)
                return
            session['sent'] = None
            session['next_call_at'] = None
            self._tick_ms = self.TICK_MAX_MS
            self._apply({'state': client.STATE_DISABLED}, client)
            return

        if not self._savedToe():
            # Refuse a never-saved project: otherwise every scratch TD launch
            # mints a junk node record keyed on a throwaway folder.
            session['sent'] = None
            session['next_call_at'] = None
            self._tick_ms = self.TICK_MAX_MS
            self._apply({'state': client.STATE_UNSAVED}, client)
            return

        convoy_id = self._readConvoyId()
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

            (enabled, project_root, comp_path, convoy_id, envoy_port, host_id)

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
        return (True,
                project_root,
                str(self._embody.path),
                str(convoy_id),
                self._envoyPort(),
                str(session.get('host_id') or ''))

    def _scheduleFrom(self, session):
        """Tick delay in ms: soon enough to serve the next due call, never a
        busy loop. Clamped to [TICK_MIN_MS, TICK_MAX_MS]."""
        due_at = session.get('next_call_at')
        if due_at is None:
            return self.TICK_MAX_MS
        remaining_ms = int(max(0.0, due_at - time.monotonic()) * 1000)
        return max(self.TICK_MIN_MS, min(remaining_ms, self.TICK_MAX_MS))

    # ==================================================================
    # Worker + bounded main-thread poll (UpdaterExt's shape)
    # ==================================================================

    def _runInWorker(self, fn):
        """Start the daemon worker. Isolated in one method so the in-TD
        suite can run the worker body synchronously against a stubbed
        client -- no thread, no socket, no timing race inside one frame."""
        import threading
        threading.Thread(target=fn, daemon=True).start()

    def _beginCall(self, action, client, session, state=None):
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
            payload = client.registration_payload(
                state[1], state[2], state[3], runtime_id, envoy_port=state[4])
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
                    else:
                        out['result'] = client.unregister(
                            probe.handle, node_id, runtime_id=runtime_id)
            except Exception as e:
                # convoy_client is written never to raise; if it ever does,
                # the worker must still publish something or the poll spins
                # to its cap.
                out['result'] = {'state': 'error',
                                 'detail': '%s: %s' % (type(e).__name__, e)}
            self._result = out

        self._runInWorker(_worker)
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
            else:
                self._apply(result, client)
            return

        registered = client is not None and state == client.STATE_REGISTERED
        if registered:
            session['registered'] = True
            session['node_id'] = (result.get('node_id')
                                  or session.get('node_id'))
            if result.get('host_id'):
                session['host_id'] = str(result['host_id'])
            pending = session.get('pending_sent')
            if pending is not None:
                # Promote the observed host identity into the sent tuple so a
                # successful call converges instead of oscillating.
                sent = list(pending)
                sent[5] = str(session.get('host_id') or '')
                session['sent'] = tuple(sent)
            session['fails'] = 0
            # Registered but portless is NOT steady: Envoy binds seconds
            # after open, and the host cannot dispatch back until it knows
            # the port. Keep converging until it does.
            session['next_call_at'] = now + (
                self.HEARTBEAT_S if result.get('envoy_port')
                else self.CONVERGING_S)
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
        }

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

        self._runInWorker(_worker)
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
        """Write the Convoy Host readout. convoy_client owns the words."""
        client = self._safeClient()
        try:
            text = client.host_status_text(state)
        except Exception:
            text = 'Install failed -- see log'
        # getattr, not direct access: this extension can land in a session
        # whose Convoy page predates the host-app parameters (the same
        # guard _status uses for Convoystatus).
        par = getattr(self._embody.par, 'Convoyhoststatus', None)
        if par is not None:
            self._setPar(par, str(text)[:160])

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
            '  - runs it under TouchDesigner\'s own Python:\n'
            '    %s\n\n'
            'What it does NOT do:\n'
            '  - it listens on 127.0.0.1 only, on a port the OS assigns.\n'
            '    Nothing is exposed to the network. No firewall rule is\n'
            '    created, and none is needed.\n'
            '  - it never asks for administrator rights, and Embody never\n'
            '    modifies your firewall.\n\n'
            'Where the boundary really is:\n'
            '  - ANYTHING RUNNING AS YOUR USER ON THIS MACHINE CAN READ\n'
            '    ITS TOKEN AND SEND IT WORK. The token is a boundary\n'
            '    against OTHER users, not against you.\n'
            '  - it relays only operations in the audited registry, and\n'
            '    only into projects where you turned Convoy on.\n'
            '  - IT IS NOT CODE-SIGNED OR NOTARIZED. Security software may\n'
            '    flag an unsigned Python program that runs at login.\n'
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
            par = getattr(self._embody.par, 'Convoyhoststatus', None)
            if par is not None:
                out['status'] = str(par.eval())
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
        fixes 'Needs repair -- Python not found' (the interpreter is
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

        # TD's own bundled Python, resolved HERE so the dialog can name
        # the interpreter that will run at login, and so a machine with no
        # usable TD install refuses BEFORE the confirmation rather than
        # after it. A handful of stat calls, once per pulse.
        interpreter = installer.choose_interpreter(
            installer.find_interpreters(ctx['platform']))
        if not interpreter:
            self._log('no TouchDesigner Python was found for this user -- '
                      'the host app has nothing to run under', 'WARNING')
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
            lambda: _host_install(ctx, modules, interpreter, supervisor),
            note=self.HOST_INSTALLING)
        return {'state': 'installing', 'action': plan.get('action'),
                'interpreter': interpreter, 'modules': sorted(modules)}

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

    def _ensureConsent(self):
        """True to proceed with registration; False when the user declined.

        Consent is recorded per PROJECT, in the COMMITTED
        .embody/project.json, not per session: a clone that inherits the
        tracked convoy key inherits the convoy (Model B). So the
        confirmation fires exactly once per project, on the first explicit
        enable, and never on a tick or on project open -- no modal during
        startup, ever.
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
        if entry.get('id'):
            self._publishId(entry.get('id'))
            return True

        if not self._savedToe():
            self._log('save the project before enabling Convoy -- a node is '
                      'identified by its project folder, and an unsaved one '
                      'would mint a throwaway identity', 'WARNING')
            self._setEnabled(False)
            self._apply({'state': 'unsaved'}, self._safeClient())
            return False

        candidate = embody._mintConvoyId()
        choice = embody._messageBox(
            'Embody - Enable Convoy',
            'Enable Convoy for this project?\n\n'
            'Convoy gives this project a stable identity so a Convoy host\n'
            'app can find and reach this TouchDesigner session.\n\n'
            'Enabling it will:\n'
            '  - mint the convoy id ' + str(candidate) + '\n'
            '  - record that id, and this consent, in\n'
            '    .embody/project.json -- a COMMITTED file, so everyone who\n'
            '    clones this repo shares the same convoy\n'
            '  - register this session with the Convoy host app running on\n'
            '    THIS machine, over loopback only\n\n'
            'Scope granted: this machine\'s local host app only. Convoy does\n'
            'not reach the network here, and widening that scope later will\n'
            'ask you again.\n\n'
            'Turn Convoy Enable off at any time to undo the registration.',
            ['Cancel', 'Enable Convoy'])
        if choice != 1:
            # -1 is the suppressed-dialog / unseeded-test default, and every
            # non-affirmative value means no (UpdaterExt._dialog's contract).
            # Declining a feature is not an error: INFO, no dialog, no status
            # beyond the resting one.
            self._log('Convoy enable cancelled -- no convoy id was minted '
                      'and nothing was written', 'INFO')
            self._setEnabled(False)
            self._apply({'state': 'disabled'}, self._safeClient())
            return False

        try:
            recorded = embody._ensureConvoyId(candidate, self.CONSENT_SCOPE)
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
        if self._performing():
            self._log('Perform Mode is on -- registration waits until it '
                      'ends', 'INFO')
            return {'state': 'deferred'}
        if not self._enabled():
            self._log('Convoy is off -- turn Convoy Enable on to register',
                      'INFO')
            return {'state': 'disabled'}
        if not self._ensureConsent():
            return {'state': 'declined'}
        self._reconcile(force=True)
        return self.ConvoyStatus()

    def Unregister(self, blocking=False, reason='disabled'):
        """Best-effort: clear this node's Envoy port on the local host app.

        One attempt, a 1 s timeout, every outcome a value -- convoy_client's
        contract, because the callers are a disable and a shutting-down TD.
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
        if client is None or not session.get('node_id'):
            session['registered'] = False
            session['sent'] = None
            session['next_call_at'] = None
            if client is not None and not blocking:
                self._apply({'state': client.STATE_UNREGISTERED}, client)
            return {'state': 'unregistered', 'already_gone': True}

        if not blocking:
            self._beginCall('unregister', client, session)
            return {'state': 'unregistering'}

        result = {'state': 'error', 'detail': 'unregister did not run'}
        try:
            probe = client.probe()
            if probe.use_convoy:
                result = client.unregister(
                    probe.handle, session.get('node_id'),
                    runtime_id=session.get('runtime_id'))
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
        out = {'enabled': False, 'performing': False, 'saved_project': False,
               'convoy_id': '', 'node_id': '', 'host_id': '', 'runtime_id': '',
               'registered': False, 'envoy_port': None, 'busy': False,
               'status': ''}
        try:
            session = self._session()
            out.update({
                'enabled': self._enabled(),
                'performing': self._performing(),
                'saved_project': bool(self._savedToe()),
                'convoy_id': self._readConvoyId(),
                'node_id': str(session.get('node_id') or ''),
                'host_id': str(session.get('host_id') or ''),
                'runtime_id': str(session.get('runtime_id') or ''),
                'registered': bool(session.get('registered')),
                'envoy_port': self._envoyPort(),
                'busy': bool(self._busy),
            })
            par = getattr(self._embody.par, 'Convoystatus', None)
            if par is not None:
                out['status'] = str(par.eval())
        except Exception as e:
            out['error'] = '%s: %s' % (type(e).__name__, e)
        return out


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


def _host_install(ctx, modules, interpreter, supervisor=None):
    """Write the payload, register the supervisor, start it, wait."""
    installer = ctx['installer']
    outcome = installer.install(
        ctx['data_dir'], ctx['version'], modules, interpreter,
        platform=ctx['platform'], home=ctx['home'], uid=ctx['uid'],
        installed_by=ctx['installed_by'], supervisor=supervisor)
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
