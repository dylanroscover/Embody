"""Soak testing (envoy_soak): pure summary math under pytest, sampler
lifecycle inside TouchDesigner.

The pure half (summarize / verdict / clean_mask / slope_per_min /
downsample) decides whether a run PASSES, so it runs on the whole CI matrix
off-TD. The lifecycle half drives tick() with an injected clock so a 5-second
soak finishes inside one test method, with the jobs dir pointed at a temp
folder so no real record is written.
"""

from __future__ import annotations

import importlib.util
import os
import tempfile
import time
from pathlib import Path

try:
    runner_mod = op.unit_tests.op('TestRunnerExt').module
    EmbodyTestCase = runner_mod.EmbodyTestCase
except (AttributeError, NameError):
    pass  # EmbodyTestCase already injected by test runner

_EMBODY_DIR = Path(__file__).resolve().parents[1] / 'Embody'


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, _EMBODY_DIR / filename)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


soak = _load('envoy_soak_under_test', 'envoy_soak.py')


def _sample(t, fps=60.0, fps_min=None, ms=16.0, ms_max=None, gpu=1000.0,
            cpu=2000.0, dropped=0, frames=60, total=8000.0, span=1.0):
    return {'t': float(t), 'span': span, 'fps': fps,
            'fps_min': fps if fps_min is None else fps_min,
            'ms': ms, 'ms_max': ms if ms_max is None else ms_max,
            'gpu_mb': gpu, 'gpu_total': total, 'cpu_mb': cpu,
            'dropped': dropped, 'frames': frames, 'active_ops': 100,
            'total_ops': 500, 'gpu_temp': -1.0}


def _run(n=60, **kw):
    return [_sample(i, **kw) for i in range(1, n + 1)]


# --- pure: trends -----------------------------------------------------------

def test_slope_flat_is_zero_and_rising_is_per_minute():
    ts = [float(i) for i in range(60)]
    assert soak.slope_per_min(ts, [5.0] * 60) == 0.0
    # +1 unit per second -> +60 per minute
    assert abs(soak.slope_per_min(ts, ts) - 60.0) < 1e-9
    assert soak.slope_per_min([1.0], [1.0]) == 0.0
    assert soak.slope_per_min([2.0, 2.0], [1.0, 9.0]) == 0.0


def test_clean_mask_excludes_window_and_span():
    samples = [_sample(t) for t in (1, 2, 3, 4, 5)]
    mask = soak.clean_mask(samples, [(3.1, 'capture_top')], window_s=0.5)
    # t=3 aggregates (2,3] and 3.1 lands within 0.5 s after it; t=4's window
    # starts at 4-1-0.5 = 2.5, so it is dirty too; 5 is clean.
    assert mask == [True, True, False, False, True]


def test_downsample_keeps_ends_and_cap():
    samples = _run(3600)
    out = soak.downsample(samples, cap=600)
    assert len(out) <= 600
    assert out[0] is samples[0] and out[-1] is samples[-1]
    assert soak.downsample(_run(10), cap=600) == _run(10)


# --- pure: verdicts ---------------------------------------------------------

def test_clean_run_passes():
    summary = soak.summarize(_run(), [], 60.0, frames_total=3600)
    level, reasons = soak.verdict(summary)
    assert level == 'PASS'
    assert summary['clean_samples'] == 60
    assert summary['dropped_total'] == 0
    assert summary['below_floor_pct'] == 0.0
    assert summary['gpu_headroom_pct_min'] == 87.5
    assert reasons and 'no dropped frames' in reasons[0]


def test_dip_inside_perturbation_window_is_an_artifact_not_a_defect():
    samples = _run()
    samples[29]['fps_min'] = 20.0   # t=30 dipped ...
    samples[29]['dropped'] = 8      # ... and dropped frames ...
    perturbations = [(30.2, 'capture_top')]   # ... while a capture ran
    summary = soak.summarize(samples, perturbations, 60.0, frames_total=3600)
    assert summary['fps_min_raw'] == 20.0
    assert summary['fps_min_clean'] == 60.0
    assert summary['dropped_total'] == 8 and summary['dropped_clean'] == 0
    assert summary['perturbations'] == {'count': 1, 'by_tool': {'capture_top': 1},
                                        'window_s': soak.PERTURB_WINDOW_S}
    level, reasons = soak.verdict(summary)
    assert level == 'PASS'
    joined = ' '.join(reasons)
    assert 'coincided with Envoy calls' in joined
    assert 'measurement artifact' in joined


def test_dropped_frames_fail_above_ratio_and_warn_below():
    samples = _run()
    samples[10]['dropped'] = 10                 # 10 of 3600 = 0.28% -> FAIL
    summary = soak.summarize(samples, [], 60.0, frames_total=3600)
    assert soak.verdict(summary)[0] == 'FAIL'
    samples = _run()
    samples[10]['dropped'] = 1                  # 1 of 3600 = 0.03% -> WARN
    summary = soak.summarize(samples, [], 60.0, frames_total=3600)
    level, reasons = soak.verdict(summary)
    assert level == 'WARN'
    assert 'a show drops none' in reasons[0]


def test_below_floor_share_decides_warn_vs_fail():
    samples = _run(100)
    for s in samples[:3]:
        s['fps_min'] = 50.0                     # 3% under the 54 floor -> WARN
    assert soak.verdict(soak.summarize(samples, [], 60.0, 6000))[0] == 'WARN'
    for s in samples[:10]:
        s['fps_min'] = 50.0                     # 10% -> FAIL
    summary = soak.summarize(samples, [], 60.0, 6000)
    assert summary['fps_floor'] == 54.0
    assert summary['below_floor_pct'] == 10.0
    assert soak.verdict(summary)[0] == 'FAIL'


def test_memory_climb_fails_only_when_sustained_and_material():
    leak = [_sample(i, cpu=2000.0 + 2.0 * i) for i in range(1, 601)]   # +2 MB/s
    summary = soak.summarize(leak, [], 60.0, 36000)
    level, reasons = soak.verdict(summary)
    assert level == 'FAIL'
    assert any('CPU memory climbing' in r for r in reasons)
    # a 5 MB wobble over ten minutes is noise, not a leak
    jitter = [_sample(i, cpu=2000.0 + (5.0 if i % 2 else 0.0)) for i in range(1, 601)]
    assert soak.verdict(soak.summarize(jitter, [], 60.0, 36000))[0] == 'PASS'


def test_gpu_headroom_below_twenty_percent_fails():
    summary = soak.summarize(_run(gpu=7000.0, total=8000.0), [], 60.0, 3600)
    assert summary['gpu_headroom_pct_min'] == 12.5
    level, reasons = soak.verdict(summary)
    assert level == 'FAIL'
    assert any('GPU headroom' in r for r in reasons)


def test_job_public_trusts_heartbeat_over_start_time():
    envoy = _load('envoy_ext_for_soak', 'EnvoyExt.py')
    now = time.time()
    beating = {'id': 'job_00000001', 'status': 'running',
               'started': now - 40 * 60, 'updated': now - 5}
    assert 'stale' not in envoy._job_public(beating, now)
    silent = {'id': 'job_00000002', 'status': 'running',
              'started': now - 40 * 60}
    assert envoy._job_public(silent, now).get('stale') is True
    stopped_beating = dict(beating, updated=now - 31 * 60)
    assert envoy._job_public(stopped_beating, now).get('stale') is True


# --- in TD: sampler lifecycle -----------------------------------------------

class TestSoakLifecycle(EmbodyTestCase):
    """Drives tick() with an injected clock; the jobs dir is a temp folder.

    Not destructive: reads the Perform CHOP, writes JSON into a temp dir
    that is removed file-by-file in tearDown (no recursive delete).
    """

    def setUp(self):
        super().setUp()
        self.envoy = self.embody.ext.Envoy
        self.soak = self.embody.op('envoy_soak').module
        self.J = self.embody.op('EnvoyExt').module
        if self.soak._state() is not None:
            self.soak.finalize(self.envoy, 'stopped')
        self.tmp = tempfile.mkdtemp(prefix='embody_soak_')
        self._real_jobs_dir = self.J._jobs_dir
        self.J._jobs_dir = lambda: self.tmp

    def tearDown(self):
        if self.soak._state() is not None:
            self.soak.finalize(self.envoy, 'stopped')
        self.J._jobs_dir = self._real_jobs_dir
        for name in os.listdir(self.tmp):
            os.remove(os.path.join(self.tmp, name))
        os.rmdir(self.tmp)
        super().tearDown()

    def _start(self, **kw):
        res = self.soak.run_soak_test(self.envoy, **kw)
        self.assertEqual(res.get('status'), 'running', res)
        return res

    def test_A01_start_tick_finish(self):
        res = self._start(duration_s=5, interval_s=1)
        job_id = res['job_id']
        self.assertEqual(res['duration_s'], 5.0)
        self.assertGreater(res['fps_target'], 0)
        st = self.soak._state()
        self.assertIsNotNone(st)
        t0 = st['t0']
        finished = None
        for i in range(1, 6):
            finished = self.soak.tick(self.envoy, now=t0 + i)
            if i < 5:
                self.assertIsNone(finished)
        self.assertIsNotNone(finished, 'the tick at duration must finish the soak')
        self.assertEqual(finished['status'], 'done')
        result = finished['result']
        self.assertEqual(result['reason'], 'completed')
        self.assertIn(result['verdict'], ('PASS', 'WARN', 'FAIL'))
        self.assertGreaterEqual(result['summary']['samples'], 4)
        self.assertEqual(result['summary']['frames_total'],
                         sum(s['frames'] for s in result['samples']))
        self.assertIsNone(self.soak._state())
        self.assertEqual(self.J._read_job(job_id)['status'], 'done')

    def test_A02_running_record_heartbeats_and_stops(self):
        res = self._start(duration_s=60, interval_s=1)
        t0 = self.soak._state()['t0']
        for i in range(1, 12):
            self.assertIsNone(self.soak.tick(self.envoy, now=t0 + i))
        rec = self.J._read_job(res['job_id'])
        self.assertEqual(rec['status'], 'running')
        self.assertIn('progress', rec)
        self.assertGreaterEqual(rec['progress']['elapsed_s'], 10)
        self.assertIn('summary_so_far', rec['progress'])
        self.assertNotIn('stale', self.J._job_public(rec, time.time()))
        stopped = self.soak.run_soak_test(self.envoy, stop=True)
        self.assertEqual(stopped['status'], 'done')
        self.assertEqual(stopped['result']['reason'], 'stopped')
        self.assertNotIn('progress', stopped)
        self.assertIsNone(self.soak._state())

    def test_A03_second_start_returns_the_running_handle(self):
        res = self._start(duration_s=30)
        again = self.soak.run_soak_test(self.envoy, duration_s=30)
        self.assertEqual(again['job_id'], res['job_id'])
        self.assertEqual(again['status'], 'running')
        self.assertIn('already running', again['hint'])

    def test_A04_perturbations_logged_and_excluded(self):
        self._start(duration_s=5, interval_s=1)
        self.soak.note_operation('capture_top')
        self.soak.note_operation('get_project_performance')   # quiet
        st = self.soak._state()
        self.assertEqual([p[1] for p in st['perturbations']], ['capture_top'])
        t0 = st['t0']
        finished = None
        for i in range(1, 6):
            finished = self.soak.tick(self.envoy, now=t0 + i)
        summary = finished['result']['summary']
        self.assertEqual(summary['perturbations']['by_tool'], {'capture_top': 1})
        # a call at t~0 dirties the samples at t=1..3 (span 1 + 2 s window)
        self.assertEqual(summary['samples'], 5)
        self.assertEqual(summary['clean_samples'], 2)

    def test_A05_stop_without_a_run_is_an_error(self):
        res = self.soak.run_soak_test(self.envoy, stop=True)
        self.assertEqual(res.get('error_code'), 'envoy.soak.not_running')

    def test_A06_clamps_and_rejects_bad_numbers(self):
        res = self._start(duration_s=1, interval_s=0.01)
        self.assertEqual(res['duration_s'], self.soak.MIN_DURATION_S)
        self.assertEqual(res['interval_s'], self.soak.MIN_INTERVAL_S)
        self.soak.finalize(self.envoy, 'stopped')
        bad = self.soak.run_soak_test(self.envoy, duration_s='soon')
        self.assertIn('error', bad)

    def test_A07_handler_stub_is_wired(self):
        """The facade must expose _run_soak_test for the tool wrapper."""
        res = self.envoy._run_soak_test(stop=True)
        self.assertEqual(res.get('error_code'), 'envoy.soak.not_running')
