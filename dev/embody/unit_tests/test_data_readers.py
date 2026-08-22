"""
Test suite: CHOP / POP data readers (get_chop_data, get_pop_data).

Covers:
  A. get_chop_data reduces to per-channel stats, never a raw dump
  B. channel glob + channel cap
  C. relational diff computes REAL deltas (not an echo of the source)
  D. get_pop_data is metadata-only by default (the free path)
  E. samples>0 returns exactly N points -- POP.points() ignores its own
     startIndex/count args, so the slicing must happen in Python
  F. the readback ceiling refuses an expensive GPU->CPU download
  G. family / missing-operator guards on both readers
  H. get_dat_content(format='stats') reduces a table, never dumps it
  I. get_op collapses parameter SEQUENCES and carries a summary
  J. get_docs fuses build-accurate creation defaults from the catalog
"""

try:
    runner_mod = op.unit_tests.op('TestRunnerExt').module
    EmbodyTestCase = runner_mod.EmbodyTestCase
except (AttributeError, NameError):
    pass


class TestDataReaders(EmbodyTestCase):

    def setUp(self):
        super().setUp()
        self.envoy = self.embody.ext.Envoy

        # CHOP chain: a deterministic source and a gained copy, so the diff
        # has a known-correct answer. gain=5 (not 2) on purpose: with gain=2
        # the delta equals the source value, which would hide an echo bug.
        self.src = self.sandbox.create(noiseCHOP, 'src_chop')
        self.src.par.channelname = 'c[1-3]'
        self.src.par.timeslice = False
        self.src.cook(force=True)

        self.gained = self.sandbox.create(mathCHOP, 'gained_chop')
        self.gained.inputConnectors[0].connect(self.src)
        self.gained.par.gain = 5.0
        self.gained.cook(force=True)

        self.pop = self.sandbox.create(gridPOP, 'grid_pop')
        self.pop.par.rows = 8
        self.pop.par.cols = 8
        self.pop.cook(force=True)

    # ------------------------------------------------------------------
    # A. CHOP reduction
    # ------------------------------------------------------------------

    def test_chop_returns_stats_not_raw_samples(self):
        r = self.envoy._get_chop_data(self.src.path)
        self.assertEqual(r['numChans'], 3)
        self.assertGreater(r['numSamples'], 1)
        self.assertLen(r['channels'], 3)
        for ch in r['channels']:
            for key in ('name', 'min', 'max', 'mean', 'std', 'first', 'last'):
                self.assertIn(key, ch, f'{key} missing from channel entry')
            self.assertLessEqual(ch['min'], ch['max'])
        # The point of the reducer: no raw sample dump unless asked.
        self.assertNotIn('head', r['channels'][0])

    def test_chop_samples_adds_head_and_tail(self):
        r = self.envoy._get_chop_data(self.src.path, samples=4)
        ch = r['channels'][0]
        self.assertLen(ch['head'], 4)
        self.assertLen(ch['tail'], 4)

    # ------------------------------------------------------------------
    # B. Filtering
    # ------------------------------------------------------------------

    def test_chop_channel_glob_filters(self):
        r = self.envoy._get_chop_data(self.src.path, channels='c1')
        self.assertLen(r['channels'], 1)
        self.assertEqual(r['channels'][0]['name'], 'c1')
        # numChans still reports the operator's real width, not the filter.
        self.assertEqual(r['numChans'], 3)

    # ------------------------------------------------------------------
    # C. Relational diff
    # ------------------------------------------------------------------

    def test_chop_diff_reports_real_deltas(self):
        src = self.envoy._get_chop_data(self.src.path)['channels'][0]
        gained = self.envoy._get_chop_data(self.gained.path)['channels'][0]
        diff = self.envoy._get_chop_data(
            self.gained.path, compare_to=self.src.path)['diff']

        self.assertEqual(diff['against'], self.src.path)
        entry = next(c for c in diff['channels'] if c['name'] == src['name'])
        self.assertAlmostEqual(entry['min'], gained['min'] - src['min'],
                               places=4)
        self.assertAlmostEqual(entry['max'], gained['max'] - src['max'],
                               places=4)
        # Guard against the delta secretly being the source value.
        self.assertNotAlmostEqual(entry['min'], src['min'], places=4)

    def test_chop_diff_rejects_non_chop_target(self):
        r = self.envoy._get_chop_data(self.src.path, compare_to=self.pop.path)
        self.assertIn('error', r)

    # ------------------------------------------------------------------
    # D/E/F. POP reader
    # ------------------------------------------------------------------

    def test_pop_is_metadata_only_by_default(self):
        r = self.envoy._get_pop_data(self.pop.path)
        self.assertEqual(r['numPoints'], 64)
        names = {a['name'] for a in r['pointAttributes']}
        self.assertIn('P', names)
        for a in r['pointAttributes']:
            self.assertIn('size', a)
            self.assertIn('type', a)
            self.assertNotIn('head', a,
                'default read must not pay for a GPU readback')
        self.assertIn('note', r)

    def test_pop_samples_returns_exactly_n_points(self):
        """POP.points(attr, start, count) IGNORES start/count on 2025.33070
        (points('P', 5, 3) returned all 64), so the reader slices in Python.
        This test fails loudly if that slicing is ever removed."""
        r = self.envoy._get_pop_data(self.pop.path, attributes='P', samples=3)
        head = r['pointAttributes'][0]['head']
        self.assertLen(head, 3)
        self.assertLen(head[0], 3)  # P is a float3

    def test_pop_attribute_glob_filters(self):
        r = self.envoy._get_pop_data(self.pop.path, attributes='P')
        self.assertLen(r['pointAttributes'], 1)
        self.assertEqual(r['pointAttributes'][0]['name'], 'P')

    def test_pop_refuses_readback_over_ceiling(self):
        """The guard is numPoints, never sample size -- count cannot bound
        the GPU->CPU readback because POP.points() ignores it."""
        r = self.envoy._get_pop_data(self.pop.path, samples=1, max_points=10)
        self.assertIn('readbackRefused', r)
        self.assertIn('64', r['readbackRefused'])
        # Metadata still comes back -- a refusal is not an error.
        self.assertEqual(r['numPoints'], 64)
        self.assertNotIn('error', r)

    # ------------------------------------------------------------------
    # G. Guards
    # ------------------------------------------------------------------

    def test_readers_reject_wrong_family(self):
        self.assertIn('error', self.envoy._get_chop_data(self.pop.path))
        self.assertIn('error', self.envoy._get_pop_data(self.src.path))

    def test_readers_reject_missing_operator(self):
        missing = self.sandbox.path + '/does_not_exist'
        self.assertIn('error', self.envoy._get_chop_data(missing))
        self.assertIn('error', self.envoy._get_pop_data(missing))

    # ------------------------------------------------------------------
    # H. DAT stats
    # ------------------------------------------------------------------

    def _table(self, name='stats_table', rows=120):
        t = self.sandbox.create(tableDAT, name)
        t.clear()
        t.appendRow(['label', 'score'])
        for i in range(rows):
            t.appendRow(['r%d' % i, i * 2])
        return t

    def test_dat_stats_reduces_instead_of_dumping(self):
        import json
        t = self._table()
        stats = self.envoy._get_dat_content(t.path, 'stats')
        full = self.envoy._get_dat_content(t.path, 'table')
        self.assertEqual(stats['format'], 'stats')
        self.assertLess(len(json.dumps(stats)), len(json.dumps(full)) / 3,
            'stats mode must be materially smaller than a full table dump')

    def test_dat_stats_types_columns(self):
        t = self._table()
        cols = {c['name']: c for c in
                self.envoy._get_dat_content(t.path, 'stats')['columns']}
        self.assertFalse(cols['label']['numeric'])
        self.assertTrue(cols['score']['numeric'])
        self.assertEqual(cols['score']['min'], 0)
        self.assertEqual(cols['score']['max'], 238)

    def test_dat_stats_reports_omitted_rows(self):
        t = self._table()
        stats = self.envoy._get_dat_content(t.path, 'stats')
        self.assertIn('rowsOmitted', stats)
        self.assertEqual(
            stats['rowsOmitted'] + len(stats['head']) + len(stats['tail']),
            stats['numRows'],
            'head + tail + omitted must account for every row')

    def test_dat_stats_refuses_text_dat(self):
        d = self.sandbox.create(textDAT, 'plain')
        d.text = 'not a table'
        self.assertIn('error', self.envoy._get_dat_content(d.path, 'stats'))

    def test_existing_dat_formats_unchanged(self):
        """'stats' is additive -- the other modes must be untouched."""
        t = self._table(name='unchanged_table', rows=3)
        self.assertEqual(
            self.envoy._get_dat_content(t.path, 'table')['format'], 'table')
        d = self.sandbox.create(textDAT, 'plain2')
        d.text = 'hello'
        self.assertEqual(
            self.envoy._get_dat_content(d.path, 'auto')['text'], 'hello')

    # ------------------------------------------------------------------
    # I. get_op sequence collapse + summary
    # ------------------------------------------------------------------

    def _const_chop(self):
        ch = self.sandbox.create(constantCHOP, 'const_seq')
        for i, (n, v) in enumerate([('a', 0.25), ('b', 0.5), ('c', 0.75)]):
            setattr(ch.par, 'const%dname' % i, n)
            setattr(ch.par, 'const%dvalue' % i, v)
        ch.cook(force=True)
        return ch

    def test_get_op_collapses_sequences(self):
        ch = self._const_chop()
        info = self.envoy._get_op(ch.path, False)
        self.assertIn('sequences', info)
        blocks = info['sequences']['const']
        self.assertGreaterEqual(len(blocks), 3)
        self.assertEqual(blocks[0]['name'], 'a')
        fanned = [k for k in info['parameters'] if k.startswith('const')]
        self.assertEqual(fanned, [],
            'sequence members must not also fan out into parameters')
        self.assertGreater(info['sequence_pars_collapsed'], 0)

    def test_get_op_full_mode_keeps_flat_dump(self):
        """include_defaults=True must stay compatible: the read_tdn-vs-get_op
        ratio test in test_mcp_tdn_tools measures exactly this mode."""
        ch = self._const_chop()
        info = self.envoy._get_op(ch.path, True)
        fanned = [k for k in info['parameters'] if k.startswith('const')]
        self.assertTrue(fanned, 'full mode must still fan sequences out')
        self.assertNotIn('sequences', info)

    def test_get_op_summary_is_one_line(self):
        ch = self._const_chop()
        summary = self.envoy._get_op(ch.path, False)['summary']
        self.assertNotIn(chr(10), summary)
        self.assertIn('constantCHOP', summary)
        self.assertIn('const_seq', summary)

    # ------------------------------------------------------------------
    # J. get_docs live-default fusion
    # ------------------------------------------------------------------

    def _docs_stub(self):
        Server = self.embody.op('EnvoyExt').module.EnvoyMCPServer

        class Stub:
            _DOCS_PAR_CAP = Server._DOCS_PAR_CAP
            _docsDefaultsIndex = Server._docsDefaultsIndex
            _docsLiveDefaults = Server._docsLiveDefaults

            def __init__(self):
                self._docs_state = {
                    'resolved': True, 'root': None, 'index': None,
                    'cache': {}, 'build': '%s.%s' % (app.version, app.build),
                    'defaults': None}

            def _log(self, *a, **k):
                pass

        return Stub()

    def test_docs_defaults_index_loads(self):
        idx = self._docs_stub()._docsDefaultsIndex()
        self.assertGreater(len(idx), 50,
            'catalog for this build should cover many operator types')
        self.assertNotIn('_palette', idx,
            'reserved _-prefixed catalog keys must be excluded')

    def test_docs_live_defaults_resolve_by_title(self):
        live = self._docs_stub()._docsLiveDefaults('outTOP', 'OutTOP')
        self.assertIsNotNone(live)
        self.assertEqual(live['opType'], 'outtop')
        self.assertIn('filtertype', live['parameters'])

    def test_docs_live_defaults_miss_returns_none(self):
        self.assertIsNone(
            self._docs_stub()._docsLiveDefaults('Totally Fake Op', None))
