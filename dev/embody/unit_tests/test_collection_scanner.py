"""
Test suite: Community-safety TDN capability SCANNER (Collection/scanner).

Exercises the LIVE scanner module wired into the Collection COMP -- resolved
inline every test via op.Embody.op('Collection/scanner').module, never cached
(TD may reinit the externalized scanner.py at any time).

The scanner is pure Python (no TD imports): it accepts a parsed TDN dict and
returns the frozen C2 CapabilityJson shape:

    {
        "scanner_version": str,
        "verdict": "clean" | "flagged" | "blocked",
        "counts": {<each of the 8 CAPABILITY_SURFACES>: int},
        "findings": [{"op_path", "surface", "detail", "evidence"}, ...],
    }

Verdict precedence (from scan_tdn): blocked > flagged > clean.
  - clean   : no surface counted, scan completed.
  - flagged : at least one surface counted, scan completed.
  - blocked : a hard bound was exceeded (serialized size, operator count, AST
              depth/nodes/source-length) OR the internal walk failed closed.

These cases are ported from the standalone suite at
dev/embody/Embody/Collection/tests/test_scanner.py and extended to cover every
surface, the blocked bounds, evasion paths, fail-closed behavior, evidence
bounding, and a mixed clean<flagged<blocked precedence payload.

ASCII punctuation only; tests are independent and deterministic.
"""


# Surface keys mirrored from scanner.CAPABILITY_SURFACES (verified against the
# module's empty_capability_counts() in test_clean_source_to_null_network).
_SURFACES = (
    "execute_dats",
    "file_read_exprs",
    "web_ops",
    "extensions",
    "storage_payloads",
    "denylisted_types",
    "traversal_paths",
    "external_refs",
)


def make_tdn(operators=None, **overrides):
    """Mirror the standalone suite's make_tdn fixture.

    Builds a minimal-but-valid TDN dict. Pass a list of operator dicts; extra
    top-level keys via **overrides (e.g. network_path, type)."""
    tdn = {
        "format": "tdn",
        "version": "1.4",
        "generator": "unit-test",
        "td_build": "099.2025.32820",
        "exported_at": "2026-06-08T00:00:00Z",
        "network_path": "/test",
        "options": {
            "include_dat_content": True,
            "include_storage": True,
        },
        "type": "baseCOMP",
        "operators": operators or [],
    }
    tdn.update(overrides)
    return tdn


class CollectionScannerTests(EmbodyTestCase):
    """Live-module scanner contracts: verdicts, counts, findings, bounds."""

    # -----------------------------------------------------------------
    # Live module resolution (never cached -- resolve inline per call).
    # -----------------------------------------------------------------

    def _scanner(self):
        op = self.embody.op('Collection/scanner')
        if op is None:
            raise SkipTest('Collection/scanner DAT not present')
        mod = op.module
        if mod is None or not hasattr(mod, 'scan_tdn'):
            raise SkipTest('Collection/scanner module unavailable')
        return mod

    def _scan(self, tdn):
        return self._scanner().scan_tdn(tdn)

    # -----------------------------------------------------------------
    # Shared shape + evidence assertions.
    # -----------------------------------------------------------------

    def assertResultShape(self, result):
        """Every scan result must carry the frozen C2 shape."""
        self.assertIsInstance(result, dict)
        for key in ('scanner_version', 'verdict', 'counts', 'findings'):
            self.assertIn(key, result)
        self.assertIn(result['verdict'], ('clean', 'flagged', 'blocked'))
        self.assertIsInstance(result['counts'], dict)
        for surface in _SURFACES:
            self.assertIn(surface, result['counts'])
            self.assertIsInstance(result['counts'][surface], int)
        self.assertIsInstance(result['findings'], list)
        for finding in result['findings']:
            for key in ('op_path', 'surface', 'detail', 'evidence'):
                self.assertIn(key, finding)

    def assertEvidenceBounded(self, result):
        """Every finding's evidence is truncated to <= 200 chars."""
        for finding in result['findings']:
            self.assertLessEqual(
                len(finding['evidence']), 200,
                'evidence exceeds 200-char bound: %r' % finding['evidence'])

    # -----------------------------------------------------------------
    # CLEAN: a benign source -> null chain produces no surfaces.
    # -----------------------------------------------------------------

    def test_clean_source_to_null_network(self):
        scanner = self._scanner()
        tdn = make_tdn([
            {"name": "source1", "type": "constantTOP"},
            {"name": "null1", "type": "nullTOP", "inputs": ["source1"]},
        ])

        result = scanner.scan_tdn(tdn)

        self.assertResultShape(result)
        self.assertEqual(result['verdict'], 'clean')
        # Zeroed counts must equal the module's own empty-counts factory.
        self.assertEqual(result['counts'], scanner.empty_capability_counts())
        self.assertEqual(result['findings'], [])

    def test_clean_network_has_zero_external_refs(self):
        result = self._scan(make_tdn([{"name": "null1", "type": "nullTOP"}]))
        self.assertEqual(result['counts']['external_refs'], 0)

    # -----------------------------------------------------------------
    # FLAGGED: each capability surface, one per test.
    # -----------------------------------------------------------------

    def test_execute_dat_with_code_flags_execute_surface(self):
        result = self._scan(make_tdn([
            {
                "name": "execute1",
                "type": "executeDAT",
                "dat_content": "def onStart():\n    return\n",
                "dat_content_format": "text",
            }
        ]))

        self.assertEqual(result['verdict'], 'flagged')
        self.assertGreaterEqual(result['counts']['execute_dats'], 1)
        self.assertEvidenceBounded(result)

    def test_expression_param_reading_file_flags_file_read_expr(self):
        # Leading '=' marks the value as an expression (scanner strips it then
        # AST-scans). open() is a denylisted Name -> file_read_exprs.
        result = self._scan(make_tdn([
            {
                "name": "level1",
                "type": "levelTOP",
                "parameters": {
                    "opacity": "=open('local.txt').read()",
                },
            }
        ]))

        self.assertEqual(result['verdict'], 'flagged')
        self.assertGreaterEqual(result['counts']['file_read_exprs'], 1)
        self.assertEvidenceBounded(result)

    def test_webclient_dat_counts_web_ops_and_denylisted_types(self):
        # A denylisted IO/network op type bumps BOTH web_ops and denylisted_types.
        result = self._scan(make_tdn([{"name": "web1", "type": "webclientDAT"}]))

        self.assertEqual(result['verdict'], 'flagged')
        self.assertGreaterEqual(result['counts']['web_ops'], 1)
        self.assertGreaterEqual(result['counts']['denylisted_types'], 1)
        self.assertEvidenceBounded(result)

    def test_comp_with_extension_counts_extensions(self):
        result = self._scan(make_tdn([
            {
                "name": "base1",
                "type": "baseCOMP",
                "sequences": {
                    "ext": [
                        {
                            "object": "op('./BaseExt').module.BaseExt(me)",
                            "name": "BaseExt",
                            "promote": True,
                        }
                    ]
                },
                "children": [
                    {
                        "name": "BaseExt",
                        "type": "textDAT",
                        "dat_content": "class BaseExt:\n    pass\n",
                        "dat_content_format": "text",
                    }
                ],
            }
        ]))

        self.assertEqual(result['verdict'], 'flagged')
        self.assertGreaterEqual(result['counts']['extensions'], 1)

    def test_non_empty_storage_payload_counts_storage_payloads(self):
        result = self._scan(make_tdn([
            {
                "name": "base1",
                "type": "baseCOMP",
                "storage": {"payload": "data"},
            }
        ]))

        self.assertEqual(result['verdict'], 'flagged')
        self.assertGreaterEqual(result['counts']['storage_payloads'], 1)

    def test_traversal_file_param_counts_traversal_paths(self):
        # A path-named parameter ("file") whose value traverses upward (..).
        result = self._scan(make_tdn([
            {
                "name": "text1",
                "type": "textDAT",
                "parameters": {
                    "file": "../secrets.txt",
                },
            }
        ]))

        self.assertEqual(result['verdict'], 'flagged')
        self.assertGreaterEqual(result['counts']['traversal_paths'], 1)

    def test_absolute_path_param_counts_traversal_paths(self):
        # Absolute path also trips traversal_paths (POSIX-absolute here).
        result = self._scan(make_tdn([
            {
                "name": "text1",
                "type": "textDAT",
                "parameters": {
                    "file": "/etc/passwd",
                },
            }
        ]))

        self.assertEqual(result['verdict'], 'flagged')
        self.assertGreaterEqual(result['counts']['traversal_paths'], 1)

    def test_external_ref_comp_flags_external_refs(self):
        # A COMP that points at out-of-band content (tdn_ref / tox_ref) cannot
        # be scanned inline, so each must surface as an external_ref.
        for key in ('tdn_ref', 'tox_ref'):
            result = self._scan(make_tdn(
                [{"name": "child1", "type": "baseCOMP", key: "child1.tdn"}]))
            self.assertEqual(result['verdict'], 'flagged', key)
            self.assertGreaterEqual(result['counts']['external_refs'], 1, key)
            self.assertEvidenceBounded(result)

    # -----------------------------------------------------------------
    # FLAGGED: evasion variants the walk must still catch.
    # -----------------------------------------------------------------

    def test_evasion_nested_comp_child_is_scanned(self):
        # A malicious execute DAT buried two COMP levels deep is still scanned.
        result = self._scan(make_tdn([
            {
                "name": "outer",
                "type": "baseCOMP",
                "children": [
                    {
                        "name": "inner",
                        "type": "baseCOMP",
                        "children": [
                            {
                                "name": "execute1",
                                "type": "executeDAT",
                                "dat_content": "import os\nos.system('id')\n",
                                "dat_content_format": "text",
                            }
                        ],
                    }
                ],
            }
        ]))

        self.assertEqual(result['verdict'], 'flagged')
        self.assertGreaterEqual(result['counts']['execute_dats'], 1)

    def test_evasion_expression_dynamic_import_is_flagged(self):
        # getattr(__import__('os'), 'system')('id') hidden in an expression par.
        # The expression surface is file_read_exprs (AST-scanned).
        result = self._scan(make_tdn([
            {
                "name": "math1",
                "type": "mathCHOP",
                "parameters": {
                    "postadd": "=getattr(__import__('os'), 'system')('id')",
                },
            }
        ]))

        self.assertEqual(result['verdict'], 'flagged')
        self.assertGreaterEqual(result['counts']['file_read_exprs'], 1)

    def test_evasion_storage_payload_is_scanned(self):
        result = self._scan(make_tdn([
            {
                "name": "base1",
                "type": "baseCOMP",
                "storage": {
                    "payload": "eval(open('../secret.py').read())",
                },
            }
        ]))

        self.assertEqual(result['verdict'], 'flagged')
        self.assertGreaterEqual(result['counts']['storage_payloads'], 1)

    # -----------------------------------------------------------------
    # FLAGGED (NOT blocked): unparseable Python must surface, not abort.
    # -----------------------------------------------------------------

    def test_unparseable_python_flags_not_blocks(self):
        # A SyntaxError in DAT content is flagged ("is not parseable Python"),
        # NOT blocked -- blocked is reserved for resource bounds / fail-closed.
        result = self._scan(make_tdn([
            {
                "name": "text1",
                "type": "textDAT",
                "dat_content": "def (:\n    this is not python\n",
                "dat_content_format": "text",
            }
        ]))

        self.assertEqual(result['verdict'], 'flagged')
        self.assertGreaterEqual(result['counts']['execute_dats'], 1)
        self.assertTrue(
            any('parseable' in f['detail'] for f in result['findings']),
            'expected an unparseable-Python finding')

    # -----------------------------------------------------------------
    # BLOCKED: hard resource bounds.
    # -----------------------------------------------------------------

    def test_oversized_serialized_tdn_is_blocked(self):
        scanner = self._scanner()
        # One DAT whose content alone exceeds the serialized-size bound.
        tdn = make_tdn([
            {
                "name": "text1",
                "type": "textDAT",
                "dat_content": "x" * (scanner.MAX_SERIALIZED_TDN_BYTES + 1),
                "dat_content_format": "text",
            }
        ])

        result = scanner.scan_tdn(tdn)

        self.assertEqual(result['verdict'], 'blocked')
        self.assertTrue(result['findings'])
        self.assertEvidenceBounded(result)

    def test_operator_count_exceeded_is_blocked(self):
        scanner = self._scanner()
        # The walk counts the root + every operator/child. Just over the cap
        # must short-circuit to blocked. Tiny null ops keep serialized size
        # well under the 5 MB bound (so this exercises the op-count gate, not
        # the size gate).
        ops = [{"type": "nullTOP"} for _ in range(scanner.MAX_OPERATORS + 1)]
        tdn = make_tdn(ops)

        result = scanner.scan_tdn(tdn)

        self.assertEqual(result['verdict'], 'blocked')
        self.assertTrue(result['findings'])
        self.assertEvidenceBounded(result)

    def test_ast_source_length_bound_is_blocked(self):
        scanner = self._scanner()
        # An expression longer than MAX_AST_SOURCE_CHARS is rejected before
        # parsing -> blocked. Leading '=' marks it as an expression parameter.
        long_expr = "=" + ("a" * (scanner.MAX_AST_SOURCE_CHARS + 1))
        tdn = make_tdn([
            {
                "name": "math1",
                "type": "mathCHOP",
                "parameters": {"postadd": long_expr},
            }
        ])

        result = scanner.scan_tdn(tdn)

        self.assertEqual(result['verdict'], 'blocked')
        self.assertEvidenceBounded(result)

    def test_ast_depth_bound_is_blocked(self):
        scanner = self._scanner()
        # A left-associative BinOp chain "1+1+1+..." nests one AST level per
        # term; far past MAX_AST_DEPTH it must block. Source stays short, so
        # this isolates the depth gate from the source-length gate.
        terms = scanner.MAX_AST_DEPTH + 50
        deep_expr = "=" + "+".join("1" for _ in range(terms))
        tdn = make_tdn([
            {
                "name": "math1",
                "type": "mathCHOP",
                "parameters": {"postadd": deep_expr},
            }
        ])

        result = scanner.scan_tdn(tdn)

        self.assertEqual(result['verdict'], 'blocked')
        self.assertEvidenceBounded(result)

    def test_ast_node_count_bound_is_blocked(self):
        scanner = self._scanner()
        # A flat list literal of many elements yields > MAX_AST_NODES nodes
        # without deep nesting -- isolates the node-count gate from the depth
        # gate. Elements are single-char "1"s so source stays under the
        # source-length bound.
        n = scanner.MAX_AST_NODES + 100
        list_expr = "=[" + ",".join("1" for _ in range(n)) + "]"
        tdn = make_tdn([
            {
                "name": "math1",
                "type": "mathCHOP",
                "parameters": {"postadd": list_expr},
            }
        ])

        result = scanner.scan_tdn(tdn)

        self.assertEqual(result['verdict'], 'blocked')
        self.assertEvidenceBounded(result)

    # -----------------------------------------------------------------
    # BLOCKED: internal scan error must FAIL CLOSED (never report clean).
    # -----------------------------------------------------------------

    def test_internal_scan_error_fails_closed(self):
        scanner = self._scanner()
        # If the internal walk raises, scan_tdn must catch it and return
        # "blocked" with a "scanner aborted" finding -- never "clean".
        original = scanner._scan_tdn_root

        def _boom(*a, **k):
            raise RuntimeError('boom')

        scanner._scan_tdn_root = _boom
        try:
            result = scanner.scan_tdn(
                make_tdn([{"name": "null1", "type": "nullTOP"}]))
        finally:
            scanner._scan_tdn_root = original

        self.assertEqual(result['verdict'], 'blocked')
        self.assertTrue(
            any(f['detail'].startswith('scanner aborted')
                for f in result['findings']),
            'expected a fail-closed "scanner aborted" finding')

    # -----------------------------------------------------------------
    # EVIDENCE bounding: a deliberately huge evidence string is truncated.
    # -----------------------------------------------------------------

    def test_evidence_is_bounded_to_200_chars(self):
        # A long-but-parseable expression that still trips a surface; its
        # evidence (the source text) must be truncated to <= 200 chars.
        long_arg = "'" + ("a" * 5000) + "'"
        result = self._scan(make_tdn([
            {
                "name": "math1",
                "type": "mathCHOP",
                "parameters": {
                    "postadd": "=open(" + long_arg + ")",
                },
            }
        ]))

        self.assertEqual(result['verdict'], 'flagged')
        self.assertTrue(result['findings'])
        self.assertEvidenceBounded(result)

    # -----------------------------------------------------------------
    # PRECEDENCE: one mixed payload carrying clean + flagged + blocked
    # surfaces must resolve to BLOCKED (blocked > flagged > clean).
    # -----------------------------------------------------------------

    def test_mixed_payload_blocked_takes_precedence(self):
        scanner = self._scanner()
        # benign null (clean) + execute DAT (flagged surface) + an oversized
        # AST source (blocked bound). The hardest verdict must win: blocked.
        blocking_expr = "=" + ("a" * (scanner.MAX_AST_SOURCE_CHARS + 1))
        tdn = make_tdn([
            {"name": "null1", "type": "nullTOP"},
            {
                "name": "execute1",
                "type": "executeDAT",
                "dat_content": "def onStart():\n    return\n",
                "dat_content_format": "text",
            },
            {
                "name": "math1",
                "type": "mathCHOP",
                "parameters": {"postadd": blocking_expr},
            },
        ])

        result = scanner.scan_tdn(tdn)

        self.assertResultShape(result)
        self.assertEqual(result['verdict'], 'blocked')
        # The flagged surface still got counted before the block was set --
        # blocked is a verdict override, not a counts reset.
        self.assertGreaterEqual(result['counts']['execute_dats'], 1)
        self.assertEvidenceBounded(result)
