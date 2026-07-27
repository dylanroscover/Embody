"""
Test suite: TDN writer/reader contract invariant.

THE INVARIANT: anything the TDN exporter can emit, the TDN importer must
be able to apply. A writer that produces a document its own reader rejects
is a data-fidelity bug regardless of which side you call wrong.

The existing sequence suite (test_tdn_sequences.py, 27 tests) round-trips
healthy, populated, freshly-created operators and asserts specific values
survive. It cannot catch a degenerate emission, because its fixtures make
the degenerate state unreachable -- see test_sequence_roundtrip_empty_blocks
there, which is named for the 0-block case but sets numBlocks = 2.

This suite attacks the contract itself instead of specific values, and is
the harness used to hunt the Moonshine v6.0.157 report's `sequences: {pt: []}`
emission (an export the importer refuses with "Minimum size is 1 block").
"""

# Import EmbodyTestCase (injected by runner, or from DAT for backwards compat)
try:
    runner_mod = op.unit_tests.op('TestRunnerExt').module
    EmbodyTestCase = runner_mod.EmbodyTestCase
except (AttributeError, NameError):
    pass  # EmbodyTestCase already injected by test runner


# Operator types that carry built-in sequences, spanning the families where
# sequence handling differs. linePOP is the type from the field report.
SEQ_OP_TYPES = [
    'linePOP', 'boxPOP', 'gridPOP', 'noisePOP', 'transformPOP',
    'mathmixPOP', 'attributePOP', 'deletePOP',
    'constantCHOP', 'addSOP', 'glslTOP', 'glslMAT', 'baseCOMP',
]


class TestTDNRoundtripInvariant(EmbodyTestCase):

    def setUp(self):
        super().setUp()

    @property
    def tdn(self):
        """Resolved inline every access -- never cached. TDNExt reinits on
        any source edit (syncfile), and a cached ref taken in setUp would
        go stale mid-run against the very extension this suite tests."""
        return self.embody.ext.TDN

    # --- helpers ---------------------------------------------------------

    def _create(self, type_name):
        """Create an operator by type NAME, or return None if this TD build
        does not have that type."""
        td_type = globals().get(type_name)
        if td_type is None:
            return None
        try:
            return self.sandbox.create(td_type, f'inv_{type_name.lower()}')
        except Exception:
            return None

    def _sequence_report(self, target):
        """Per-sequence: numBlocks, len(seq.blocks), and what the exporter
        would emit. Never raises."""
        out = {}
        seen = set()
        # Iterate target.seq, NOT target.pars() + p.isSequence. The
        # pars-based path is the one this suite proves misses 'pt' on a
        # fresh POP -- using it here made these diagnostics blind to
        # exactly the sequences under investigation, so their
        # assertEqual([], ...) passed vacuously.
        try:
            all_seqs = list(target.seq)
        except Exception:
            return out
        for seq in all_seqs:
            if seq is None or seq.name in seen:
                continue
            seen.add(seq.name)
            try:
                num = int(seq.numBlocks)
            except Exception as e:
                num = f'raised {type(e).__name__}'
            try:
                nblocks = len(list(seq.blocks))
            except Exception as e:
                nblocks = f'raised {type(e).__name__}'
            try:
                emitted = self.tdn._exportSequenceBlocks(target, seq)
                emitted_desc = ('OMITTED' if emitted is None
                                else f'list(len={len(emitted)})')
            except Exception as e:
                emitted_desc = f'raised {type(e).__name__}: {e}'
            out[seq.name] = {
                'numBlocks': num,
                'len(seq.blocks)': nblocks,
                'exporter': emitted_desc,
            }
        return out

    # --- THE INVARIANT ---------------------------------------------------

    def test_exporter_never_emits_an_empty_sequence_list(self):
        """No operator type may export `sequences: {name: []}`.

        An empty list is unimportable: TD rejects numBlocks=0 on any
        sequence with a minimum of 1, so the file cannot round-trip.
        Either the sequence has blocks (emit them) or it is at defaults
        (omit the key) -- never an empty list.
        """
        offenders = []
        for type_name in SEQ_OP_TYPES:
            target = self._create(type_name)
            if target is None:
                continue
            try:
                sequences = self.tdn._exportBuiltinSequences(target)
                for seq_name, blocks in (sequences or {}).items():
                    if isinstance(blocks, list) and len(blocks) == 0:
                        offenders.append(
                            f'{type_name}.{seq_name} exported []')
            finally:
                target.destroy()
        self.assertEqual(
            [], offenders,
            'exporter emitted an unimportable empty sequence list: '
            + '; '.join(offenders))

    def test_zero_block_sequences_do_not_exist_at_defaults(self):
        """DIAGNOSTIC BASIS: if any built-in sequence legitimately sits at
        0 blocks, the omit-gate in _exportSequenceBlocks (which compares
        against a hardcoded default of 1) emits [] DETERMINISTICALLY, not
        intermittently. This test records whether such a type exists.
        """
        zero_block = []
        for type_name in SEQ_OP_TYPES:
            target = self._create(type_name)
            if target is None:
                continue
            try:
                for seq_name, info in self._sequence_report(target).items():
                    if info['numBlocks'] == 0 or info['len(seq.blocks)'] == 0:
                        zero_block.append(f'{type_name}.{seq_name}: {info}')
            finally:
                target.destroy()
        self.assertEqual(
            [], zero_block,
            'found a sequence sitting at 0 blocks -- this makes the empty '
            'export deterministic for this type: ' + '; '.join(zero_block))

    def test_numblocks_and_blocks_view_agree(self):
        """seq.numBlocks and len(seq.blocks) must report the same count.

        The proposed export guard keys on numBlocks != 0 to detect a bad
        blocks read. That only works if the two can disagree. This test
        establishes whether they ever do.
        """
        disagreements = []
        for type_name in SEQ_OP_TYPES:
            target = self._create(type_name)
            if target is None:
                continue
            try:
                for seq_name, info in self._sequence_report(target).items():
                    if info['numBlocks'] != info['len(seq.blocks)']:
                        disagreements.append(f'{type_name}.{seq_name}: {info}')
            finally:
                target.destroy()
        self.assertEqual(
            [], disagreements,
            'numBlocks disagrees with the blocks view: '
            + '; '.join(disagreements))

    def _discovery_probe(self, target, label):
        """Compare the exporter's discovery path (target.pars() + isSequence)
        against the importer's (iterate target.seq). A disagreement between
        them is what lets an empty list reach the file."""
        via_pars = {}
        for p in target.pars():
            if p.isSequence and p.sequence is not None:
                via_pars[p.sequence.name] = p.name
        via_seq = {}
        try:
            for s in target.seq:
                try:
                    via_seq[s.name] = (int(s.numBlocks), len(list(s.blocks)))
                except Exception as e:
                    via_seq[s.name] = f'raised {type(e).__name__}'
        except Exception as e:
            via_seq = f'target.seq raised {type(e).__name__}'
        return {
            'label': label,
            'exporter_discovery(pars)': sorted(via_pars),
            'importer_discovery(seq)': via_seq,
            'has_pt_prefixed_pars': sorted(
                p.name for p in target.pars() if p.name.startswith('pt'))[:6],
        }

    def test_sequence_discovery_paths_agree(self):
        """THE DEFECT (reproduces the Moonshine v6.0.157 sequence bug).

        The exporter discovers sequences via `target.pars()` + `p.isSequence`
        (TDNExt.py:2872-2878). The importer resolves them by iterating
        `target.seq` (`_getSequenceByName`, TDNExt.py:4105-4106).

        MEASURED 2026-07-25, TD 099.2025.33070, fresh linePOP:
            pars-based discovery : ['attr']
            seq-based discovery  : {'attr': (1,1), 'pt': (2,2)}
        After a cook, pars-based discovery finds 'pt' too. Note the pt
        block pars ('pt0posx', ...) only appear in target.pars() AFTER
        target.seq has been iterated -- iterating materializes them. An
        earlier version of this docstring claimed they were present all
        along; that reading came from a probe that had already touched
        target.seq.

        So on a never-cooked POP the exporter silently DROPS a populated
        sequence from the .tdn. TDNExt already documents this accessor as
        unreliable on POPs and hardened the LOOKUP path for it
        (_getSequenceByName docstring, TDNExt.py:4099-4103) -- the
        DISCOVERY path was never given the same treatment.

        FIX: discover sequences by iterating `target.seq`, the path the
        importer already trusts. That is strictly better than gating on
        `numBlocks != 0`, which cannot fire here (numBlocks and the blocks
        view agree in every state measured -- see
        test_numblocks_and_blocks_view_agree).
        """
        target = self._create('linePOP')
        if target is None:
            self.skipTest('linePOP not available in this TD build')
        try:
            probe = self._discovery_probe(target, 'fresh (never cooked)')
            via_pars = set(probe['exporter_discovery(pars)'])
            via_seq = set(probe['importer_discovery(seq)'])
            # This asserts TD's behavior, not Embody's -- Embody cannot fix
            # the asymmetry, only stop depending on the unreliable half.
            # If a future TD build makes pars()-discovery complete, this
            # test fails and the workaround can be reconsidered.
            self.assertTrue(
                via_seq - via_pars,
                'TD no longer hides uncooked POP sequences from '
                f'pars()-based discovery: {probe}')
            self.assertIn(
                'pt', via_seq,
                'target.seq iteration is the reliable path and must see pt')
        finally:
            target.destroy()

    def test_exporter_finds_sequences_on_an_untouched_uncooked_pop(self):
        """REGRESSION (Moonshine v6.0.157): the exporter must find a
        populated sequence on a fresh POP that nothing has touched.

        Ordering matters: reading `target.seq` materializes the block pars,
        so a test that inspects the sequence first would mask the bug. This
        calls the exporter as the very first thing done to the operator.
        """
        target = self._create('linePOP')
        if target is None:
            self.skipTest('linePOP not available in this TD build')
        try:
            exported = self.tdn._exportBuiltinSequences(target) or {}
            self.assertIn(
                'pt', exported,
                'the exporter dropped a populated sequence from an '
                'untouched, never-cooked POP -- it must discover via '
                f'target.seq, not pars() (got: {sorted(exported)})')
            self.assertEqual(
                2, len(exported['pt']),
                f'both point blocks must survive: {exported["pt"]}')
            # A count alone cannot tell a correct export from
            # `pt: [{}, {}]` -- two structurally-present blocks with every
            # VALUE dropped, which is what happens if the block pars are
            # still unmaterialized when _exportSequenceBlocks scans
            # target.pars(). Assert real content survived.
            self.assertTrue(
                any(block for block in exported['pt']),
                f'blocks must carry values, not just exist: {exported["pt"]}')
            live = {s.name: int(s.numBlocks) for s in target.seq}
            self.assertEqual(2, live.get('pt'),
                             'sanity: the live POP really has 2 pt blocks')
        finally:
            target.destroy()

    def test_uncooked_pop_sequence_survives_export(self):
        """A populated sequence on a never-cooked POP must reach the .tdn.

        Consequence of the discovery mismatch above: the 2-block `pt`
        sequence is silently absent from the exported document, so a
        reconstruction rebuilds the POP at its default block count.
        """
        target = self._create('linePOP')
        if target is None:
            self.skipTest('linePOP not available in this TD build')
        try:
            live = {s.name: int(s.numBlocks) for s in target.seq}
            self.assertEqual(
                2, live.get('pt'),
                'precondition: the live POP really has a 2-block pt sequence')
            exported = self.tdn._exportBuiltinSequences(target) or {}
            self.assertIn(
                'pt', exported,
                'the populated pt sequence must survive export; it is '
                f'silently dropped (exported: {sorted(exported)})')
        finally:
            target.destroy()
