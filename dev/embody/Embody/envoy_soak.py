"""Low-overhead performance soak testing for Envoy (module DAT).

mod.envoy_soak, called by EnvoyExt on the MAIN THREAD. A soak samples the
Perform CHOP Envoy already keeps (_envoy_perform) while a show or a build
runs for minutes to hours, then files trends, dropped frames and a verdict
into a disk-backed job record (get_job_status).

Observer effect is the design constraint: the instrument must not move the
needle it reads. Per frame it evaluates three Perform CHOP channels (a few
microseconds) so dropped frames and the worst frame time inside an interval
are exact rather than sampled; memory and op counts are read once per
interval; nothing is cooked, captured or created. Envoy calls that DO
perturb TD during a soak (a capture, a cook, a script, a save) are logged as
perturbations, and samples within PERTURB_WINDOW_S of one leave the 'clean'
statistics, so a measurement artifact never reads as a show defect.

State lives on sys._envoy_soak so an extension reinit mid-soak keeps
sampling from the new instance's frame hook. The pure helpers (summarize,
verdict, slope_per_min, downsample, clean_mask) touch no TD object and run
under pytest (test_envoy_soak.py).
"""

from __future__ import annotations

import bisect
import sys
import time

STATE_ATTR = '_envoy_soak'
SAMPLE_CAP = 600             # points kept in the finished record
FLUSH_EVERY_S = 10.0         # running-record heartbeat (progress + updated)
PERTURB_WINDOW_S = 2.0       # samples this close to an Envoy call leave the clean stats
MIN_INTERVAL_S = 0.25
MAX_INTERVAL_S = 60.0
MIN_DURATION_S = 5.0
MAX_DURATION_S = 12 * 3600.0
PERTURBATION_CAP = 2000
ERROR_BUDGET = 20            # consecutive sampling failures before the soak errors out
# performance.md stop conditions, applied to a whole run instead of one read
FPS_FLOOR_RATIO = 0.9        # below ~90% of target is a stop condition
BELOW_FLOOR_FAIL_PCT = 5.0   # clean samples under the floor before FAIL (any -> WARN)
DROP_FAIL_RATIO = 0.001      # dropped/observed frames before FAIL (any drop -> WARN)
GPU_HEADROOM_FAIL_PCT = 20.0
MEM_CLIMB_MB_PER_MIN = 1.0   # sustained slope AND ...
MEM_CLIMB_MIN_DELTA_MB = 10.0  # ... total growth before memory reads as a leak

# Main-thread Envoy calls that only read counters or bookkeeping. Anything
# else that reaches the frame hook during a soak cooks, captures, writes or
# executes, and is logged as a perturbation.
QUIET_OPERATIONS = frozenset({
    'run_soak_test', 'get_project_performance', 'get_td_info', 'get_focus',
    'get_sessions', 'get_logs', 'get_guidance', 'announce_task',
    'update_task', 'claim_scope', 'release_scope', 'get_externalizations',
    'get_externalization_status',
})


# --- pure helpers (no TD access; pytest runs these) ------------------------

def slope_per_min(ts, vs):
    """Least-squares slope in units per minute; 0.0 with fewer than two
    distinct times."""
    n = len(ts)
    if n < 2:
        return 0.0
    mt = sum(ts) / n
    mv = sum(vs) / n
    var = sum((t - mt) ** 2 for t in ts)
    if var <= 0:
        return 0.0
    cov = sum((t - mt) * (v - mv) for t, v in zip(ts, vs))
    return cov / var * 60.0


def clean_mask(samples, perturbations, window_s=PERTURB_WINDOW_S):
    """True per sample when no logged Envoy call landed inside the interval
    the sample aggregates (its span) or within window_s on either side.
    perturbations: [(t_rel, operation), ...] in time order."""
    times = sorted(p[0] for p in perturbations)
    out = []
    for s in samples:
        lo = s['t'] - (s.get('span') or 0.0) - window_s
        hi = s['t'] + window_s
        i = bisect.bisect_left(times, lo)
        out.append(not (i < len(times) and times[i] <= hi))
    return out


def _stats(samples, key):
    pts = [(s['t'], s[key]) for s in samples if s.get(key) is not None]
    if not pts:
        return None
    ts = [p[0] for p in pts]
    vs = [p[1] for p in pts]
    return {'first': vs[0], 'last': vs[-1], 'min': min(vs), 'max': max(vs),
            'mean': round(sum(vs) / len(vs), 3),
            'slope_per_min': round(slope_per_min(ts, vs), 4)}


def _minkey(samples, key):
    vals = [s[key] for s in samples if s.get(key) is not None]
    return min(vals) if vals else None


def _maxkey(samples, key):
    vals = [s[key] for s in samples if s.get(key) is not None]
    return max(vals) if vals else None


def summarize(samples, perturbations, fps_target, frames_total=0,
              window_s=PERTURB_WINDOW_S):
    """Per-metric trends plus the show-critical scalars (drops, floor
    breaches, GPU headroom), split into raw and clean (perturbation-free)."""
    mask = clean_mask(samples, perturbations, window_s)
    clean = [s for s, ok in zip(samples, mask) if ok]
    floor = fps_target * FPS_FLOOR_RATIO if fps_target else None
    by_tool = {}
    for _t, name in perturbations:
        by_tool[name] = by_tool.get(name, 0) + 1
    out = {
        'samples': len(samples), 'clean_samples': len(clean),
        'frames_total': int(frames_total),
        'fps': _stats(samples, 'fps'), 'frame_ms': _stats(samples, 'ms'),
        'gpu_mb': _stats(samples, 'gpu_mb'), 'cpu_mb': _stats(samples, 'cpu_mb'),
        'active_ops': _stats(samples, 'active_ops'),
        'dropped_total': int(sum(s.get('dropped') or 0 for s in samples)),
        'dropped_clean': int(sum(s.get('dropped') or 0 for s in clean)),
        'fps_min_raw': _minkey(samples, 'fps_min'),
        'fps_min_clean': _minkey(clean, 'fps_min'),
        'frame_ms_max_raw': _maxkey(samples, 'ms_max'),
        'frame_ms_max_clean': _maxkey(clean, 'ms_max'),
        'fps_target': fps_target,
        'fps_floor': round(floor, 2) if floor else None,
        'below_floor_pct': None,
        'gpu_headroom_pct_min': None,
        'perturbations': {'count': len(perturbations), 'by_tool': by_tool,
                          'window_s': window_s},
    }
    if floor and clean:
        under = sum(1 for s in clean
                    if (s.get('fps_min') if s.get('fps_min') is not None
                        else s.get('fps') or 0) < floor)
        out['below_floor_pct'] = round(100.0 * under / len(clean), 2)
    heads = [(s['gpu_total'] - s['gpu_mb']) / s['gpu_total'] * 100.0
             for s in samples if s.get('gpu_total') and s.get('gpu_mb') is not None]
    if heads:
        out['gpu_headroom_pct_min'] = round(min(heads), 1)
    return out


def verdict(summary):
    """('PASS'|'WARN'|'FAIL', [reasons]) from a summarize() dict."""
    level = ['PASS']
    reasons = []

    def worst(l):
        if l == 'FAIL' or (l == 'WARN' and level[0] == 'PASS'):
            level[0] = l

    frames = summary.get('frames_total') or 0
    dropped = summary.get('dropped_clean') or 0
    total_dropped = summary.get('dropped_total') or 0
    if dropped:
        ratio = (dropped / frames) if frames else 1.0
        if ratio > DROP_FAIL_RATIO:
            worst('FAIL')
            reasons.append('%d frames dropped in %d observed (%.2f%%) with no '
                           'Envoy call nearby' % (dropped, frames, ratio * 100))
        else:
            worst('WARN')
            reasons.append('%d dropped frame(s) in %d observed -- rare, but a '
                           'show drops none' % (dropped, frames))
    if total_dropped > dropped:
        reasons.append('%d further drop(s) coincided with Envoy calls '
                       '(capture, cook, script, save) and are excluded from '
                       'the verdict' % (total_dropped - dropped))
    bf = summary.get('below_floor_pct')
    floor = summary.get('fps_floor')
    if bf:
        if bf > BELOW_FLOOR_FAIL_PCT:
            worst('FAIL')
        else:
            worst('WARN')
        reasons.append('%.1f%% of clean samples ran under the %.1f fps floor '
                       '(target %.0f)' % (bf, floor or 0, summary.get('fps_target') or 0))
    gh = summary.get('gpu_headroom_pct_min')
    if gh is not None and gh < GPU_HEADROOM_FAIL_PCT:
        worst('FAIL')
        reasons.append('GPU headroom fell to %.1f%% (stop condition is %.0f%%)'
                       % (gh, GPU_HEADROOM_FAIL_PCT))
    for key, label in (('cpu_mb', 'CPU memory'), ('gpu_mb', 'GPU memory')):
        st = summary.get(key)
        if (st and st['slope_per_min'] > MEM_CLIMB_MB_PER_MIN
                and (st['last'] - st['first']) > MEM_CLIMB_MIN_DELTA_MB):
            worst('FAIL')
            reasons.append('%s climbing %.1f MB/min (%.0f -> %.0f MB) -- a leak '
                           'or an unbounded buffer' % (label, st['slope_per_min'],
                                                       st['first'], st['last']))
    fmr = summary.get('fps_min_raw')
    fmc = summary.get('fps_min_clean')
    if floor and fmr is not None and fmr < floor and (fmc is None or fmc >= floor):
        reasons.append('lowest fps (%.1f) fell inside an Envoy perturbation '
                       'window; clean minimum %.1f -- a measurement artifact, '
                       'not a show defect' % (fmr, fmc if fmc is not None else 0))
    if not reasons:
        reasons.append('no dropped frames, fps held above %s, memory flat'
                       % (('%.1f' % floor) if floor else 'floor'))
    return level[0], reasons


def downsample(samples, cap=SAMPLE_CAP):
    """At most cap points, evenly strided, first and last always kept."""
    n = len(samples)
    if n <= cap:
        return list(samples)
    step = (n - 1) / float(cap - 1)
    idx = sorted({int(round(i * step)) for i in range(cap)} | {0, n - 1})
    return [samples[i] for i in idx]


# --- sampler (main thread) --------------------------------------------------

def _state():
    return getattr(sys, STATE_ATTR, None)


def _fresh_acc():
    return {'dropped': 0, 'fps_min': None, 'ms_max': None, 'frames': 0}


def _chan(perform, name, default=0.0):
    ch = perform.chan(name)
    return ch.eval() if ch is not None else default


def run_soak_test(ext, duration_s=600, interval_s=1.0, fps_target=None,
                  label=None, stop=False, idempotency_key=None):
    """Start (or stop) the one soak this instance runs -- see the tool
    docstring in EnvoyExt for the contract."""
    J = mod.EnvoyExt
    st = _state()
    if stop:
        if st is None:
            return {'error': 'No soak test is running.',
                    'error_code': 'envoy.soak.not_running'}
        return finalize(ext, 'stopped')
    if st is not None:
        return {'job_id': st['job_id'], 'status': 'running',
                'elapsed_s': round(time.monotonic() - st['t0'], 1),
                'hint': 'A soak is already running -- poll get_job_status(job_id), '
                        'or run_soak_test(stop=True) to end it early.'}
    if idempotency_key:
        try:
            prior = J._job_for_key(idempotency_key, expected_kind='soak_test')
        except J._IdemMarkerUnreadable as e:
            return {'error': 'Idempotency marker unreadable (%s) -- refusing to '
                             'risk a duplicate soak; clear it or retry.' % e}
        except J._IdemKeyConflict as e:
            return {'error': 'idempotency_key is already bound to a different '
                             'operation (%s) -- use a distinct key.' % e}
        if prior is not None:
            return {'job_id': prior['id'],
                    'status': prior.get('status', 'running'),
                    'hint': 'Reconciled to the soak this idempotency_key '
                            'already started.'}
    try:
        duration_s = float(duration_s)
        interval_s = float(interval_s)
    except (TypeError, ValueError):
        return {'error': 'duration_s and interval_s must be numbers'}
    duration_s = max(MIN_DURATION_S, min(MAX_DURATION_S, duration_s))
    interval_s = max(MIN_INTERVAL_S, min(MAX_INTERVAL_S, interval_s))
    baseline = mod.envoy_read.get_project_performance(ext, include_hotspots=5)
    if 'error' in baseline:
        return baseline
    try:
        target = float(fps_target) if fps_target else 0.0
    except (TypeError, ValueError):
        return {'error': 'fps_target must be a number'}
    if target <= 0:
        target = float(baseline['timing'].get('cookRate') or 0) or 60.0
    now = time.time()
    job = J._new_job('soak_test', {'duration_s': duration_s,
                                  'interval_s': interval_s,
                                  'fps_target': target, 'label': label},
                     idempotency_key=idempotency_key)
    job['updated'] = now
    J._write_job(job)
    if J._read_job(job['id']) is None:
        return {'error': 'Job records unavailable (project root not resolved '
                         'yet) -- retry shortly.'}
    if idempotency_key:
        J._record_job_key(idempotency_key, job['id'])
    setattr(sys, STATE_ATTR, {
        'job_id': job['id'], 'label': label, 't0': time.monotonic(),
        'wall0': now, 'duration': duration_s, 'interval': interval_s,
        'fps_target': target, 'next_sample': interval_s, 'last_flush': 0.0,
        'samples': [], 'perturbations': [], 'acc': _fresh_acc(),
        'frames_total': 0, 'errors': 0,
        'hotspots_start': baseline.get('hotspots', []),
    })
    return {'job_id': job['id'], 'status': 'running',
            'duration_s': duration_s, 'interval_s': interval_s,
            'fps_target': target,
            'hint': 'Sampling on Envoy\'s frame hook. Poll get_job_status(job_id) '
                    'for progress and the final verdict. Avoid capture, cook, '
                    'script and save calls while it runs -- each is logged as a '
                    'perturbation and its neighborhood leaves the clean stats.'}


def note_operation(operation):
    """Record a main-thread Envoy call that may have disturbed the run."""
    st = _state()
    if st is None or operation in QUIET_OPERATIONS:
        return
    if len(st['perturbations']) < PERTURBATION_CAP:
        st['perturbations'].append((round(time.monotonic() - st['t0'], 3),
                                    operation))


def tick(ext, now=None):
    """Per-frame step: accumulate drops and extremes, sample on the interval,
    heartbeat the record, finish on duration. `now` is injectable for tests.
    Returns the finished record when this tick ended the soak, else None."""
    st = _state()
    if st is None:
        return None
    if now is None:
        now = time.monotonic()
    t = now - st['t0']
    try:
        perform = ext.ownerComp.op('_envoy_perform')
        if perform is not None:
            acc = st['acc']
            acc['frames'] += 1
            acc['dropped'] += int(_chan(perform, 'dropped_frames'))
            fps = _chan(perform, 'fps')
            ms = _chan(perform, 'msec')
            acc['fps_min'] = fps if acc['fps_min'] is None else min(acc['fps_min'], fps)
            acc['ms_max'] = ms if acc['ms_max'] is None else max(acc['ms_max'], ms)
        if t >= st['next_sample']:
            _take_sample(ext, st, t)
            while st['next_sample'] <= t:
                st['next_sample'] += st['interval']
        if t - st['last_flush'] >= FLUSH_EVERY_S:
            _flush(st, t)
        st['errors'] = 0
    except Exception as e:
        st['errors'] += 1
        if st['errors'] > ERROR_BUDGET:
            return finalize(ext, 'error',
                            error='sampling failed repeatedly: %s' % e)
    if t >= st['duration']:
        return finalize(ext, 'completed')
    return None


def _take_sample(ext, st, t):
    perf = mod.envoy_read.get_project_performance(ext)
    if 'error' in perf:
        raise RuntimeError(perf['error'])
    acc = st['acc']
    timing, mem = perf['timing'], perf['memory']
    health, gpu = perf['frameHealth'], perf['gpu']
    fps = timing['fps']
    ms = timing['frameTimeMs']
    st['samples'].append({
        't': round(t, 3), 'span': round(st['interval'], 3),
        'fps': round(fps, 2),
        'fps_min': round(acc['fps_min'] if acc['fps_min'] is not None else fps, 2),
        'ms': round(ms, 3),
        'ms_max': round(acc['ms_max'] if acc['ms_max'] is not None else ms, 3),
        'gpu_mb': round(mem['gpuMemUsedMB'], 1),
        'gpu_total': round(mem['totalGpuMemMB'], 1),
        'cpu_mb': round(mem['cpuMemUsedMB'], 1),
        'dropped': int(acc['dropped']), 'frames': int(acc['frames']),
        'active_ops': int(health['activeOps']),
        'total_ops': int(health['totalOps']),
        'gpu_temp': gpu.get('chipTemperatureC'),
    })
    st['frames_total'] += acc['frames']
    st['acc'] = _fresh_acc()


def _flush(st, t):
    J = mod.EnvoyExt
    job = J._read_job(st['job_id']) or {'id': st['job_id'], 'kind': 'soak_test',
                                        'status': 'running', 'started': st['wall0']}
    if job.get('status') != 'running':
        return
    summary = summarize(st['samples'], st['perturbations'], st['fps_target'],
                        st['frames_total'])
    level, reasons = verdict(summary)
    job['updated'] = time.time()
    job['progress'] = {
        'elapsed_s': round(t, 1), 'duration_s': st['duration'],
        'pct': round(100.0 * t / st['duration'], 1),
        'last_sample': st['samples'][-1] if st['samples'] else None,
        'summary_so_far': summary, 'verdict_so_far': level,
        'reasons_so_far': reasons,
    }
    J._write_job(job)
    st['last_flush'] = t


def finalize(ext, reason, error=None):
    """Close the running soak into its job record and clear the sampler."""
    J = mod.EnvoyExt
    st = _state()
    if st is None:
        return {'error': 'No soak test is running.',
                'error_code': 'envoy.soak.not_running'}
    setattr(sys, STATE_ATTR, None)   # clear first: a raise below must not leave a zombie sampler
    t = time.monotonic() - st['t0']
    job = J._read_job(st['job_id']) or {'id': st['job_id'], 'kind': 'soak_test',
                                        'started': st['wall0']}
    job.pop('progress', None)
    try:
        summary = summarize(st['samples'], st['perturbations'],
                            st['fps_target'], st['frames_total'])
        level, reasons = verdict(summary)
        try:
            hotspots_end = mod.envoy_read.get_performance_hotspots(ext, 5)
        except Exception:
            hotspots_end = []
        job['result'] = {
            'reason': reason, 'label': st['label'], 'elapsed_s': round(t, 1),
            'planned_duration_s': st['duration'], 'interval_s': st['interval'],
            'verdict': level, 'reasons': reasons, 'summary': summary,
            'hotspots_start': st['hotspots_start'], 'hotspots_end': hotspots_end,
            'samples': downsample(st['samples']),
        }
        job['status'] = 'error' if reason == 'error' else 'done'
    except Exception as e:
        job['status'] = 'error'
        error = error or ('summary failed: %s' % e)
    if error:
        job['error'] = error
    job['finished'] = time.time()
    job['updated'] = job['finished']
    J._write_job(job)
    return J._job_public(job, time.time())
