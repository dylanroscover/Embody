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
import sys
import time


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
            self._result = None
            self._busy = False
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
