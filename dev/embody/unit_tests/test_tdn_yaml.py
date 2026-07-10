"""
Test suite: TDN v2.0 (JSON -> YAML) serialization.

Covers the tdn_dump / tdn_load helpers on the TDN ext: lossless round-trip,
block-scalar chomping, tab-shader fallback, YAML typing safety, JSON
back-compat (legacy tab-indented and BOM-prefixed), determinism, trailing
newline, no-anchors, dumper isolation, post-write validation, the textconv
driver's both-sides normalization, the save-path serializer, and
boilerplate-omission of default docked compute DATs.

These exercise the v2.0 format end-to-end. Most are headless (pure-Python
through the ext helpers); the boilerplate-omission test builds a live glslTOP
and skips gracefully if glsl docking is unavailable.
"""

import importlib.util
import json
import os
from pathlib import Path

import yaml

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase


def _specimen_root():
    """Resolve the repo-root specimens/ folder.

    Tests run with project.folder == .../Embody/dev; the gallery specimens
    live at the git-repo root .../Embody/specimens. Return None if missing.
    """
    candidates = [
        Path(project.folder).parent / 'specimens',
        Path(project.folder) / 'specimens',
    ]
    for c in candidates:
        if c.is_dir():
            return c
    return None


def _v15_lists_to_strings(node):
    """Convert v1.5 array-of-lines dat_content to a plain string in place.

    Mirrors the textconv driver's _normalize_dat_content: a list joined with
    '\\n' is exactly what v2.0 stores, so this makes a re-dump faithful v2.0.
    """
    if isinstance(node, dict):
        if (node.get('dat_content_format') == 'text'
                and isinstance(node.get('dat_content'), list)):
            node['dat_content'] = '\n'.join(node['dat_content'])
        for value in node.values():
            _v15_lists_to_strings(value)
    elif isinstance(node, list):
        for item in node:
            _v15_lists_to_strings(item)
    return node


class TestTDNYaml(EmbodyTestCase):

    def setUp(self):
        super().setUp()
        self.tdn = self.embody.ext.TDN

    # =================================================================
    # Round-trip + determinism on the real gallery specimens
    # =================================================================

    def test_tdn_yaml_roundtrip(self):
        """Each of the 3 specimens (after v1.5 array->string revert) dumps,
        loads back equal, and re-dumps byte-identical (determinism)."""
        root = _specimen_root()
        if root is None:
            self.skipTest('specimens/ folder not found')
        names = [
            'generative/reaction-diffusion.tdn',
            'compositing/kaleidoscope.tdn',
            '3d/noise-terrain.tdn',
        ]
        checked = 0
        for rel in names:
            fp = root / rel
            if not fp.is_file():
                continue
            checked += 1
            doc = self.tdn.tdn_load(fp.read_text(encoding='utf-8'))
            _v15_lists_to_strings(doc)
            dumped = self.tdn.tdn_dump(doc)
            reloaded = self.tdn.tdn_load(dumped)
            self.assertEqual(reloaded, doc,
                f'{rel}: round-trip mismatch')
            # Determinism: re-dump must be byte-identical.
            self.assertEqual(self.tdn.tdn_dump(doc), dumped,
                f'{rel}: re-dump not byte-identical')
        if checked == 0:
            self.skipTest('no specimen files present')

    # =================================================================
    # Block-scalar losslessness (headless, no TD)
    # =================================================================

    def test_tdn_block_scalar_lossless(self):
        """Multi-line strings with 0/1/2 trailing newlines map to |- / | / |+
        and round-trip byte-identical; tabs/trailing-space/CRLF fall back to
        double-quoted but stay lossless."""
        cases = {
            'no_trailing': ('a\nb', '|-'),
            'one_trailing': ('a\nb\n', '|'),
            'two_trailing': ('a\nb\n\n', '|+'),
        }
        for label, (s, indicator) in cases.items():
            dumped = self.tdn.tdn_dump(
                {'dat_content': s, 'dat_content_format': 'text'})
            self.assertIn(indicator, dumped,
                f'{label}: expected chomping indicator {indicator!r}')
            back = self.tdn.tdn_load(dumped)
            self.assertEqual(back['dat_content'], s,
                f'{label}: not byte-identical')

        # Tab / trailing-space / CRLF strings -> double-quoted, still lossless.
        tricky = {
            'content_tab': 'a\tb\nc',
            'leading_tab': '\tx\ny',
            'tab_only': '\t\n\t',
            'trailing_space': 'a \nb',
            'crlf': 'a\r\nb',
        }
        for label, s in tricky.items():
            back = self.tdn.tdn_load(self.tdn.tdn_dump(
                {'dat_content': s, 'dat_content_format': 'text'}))
            self.assertEqual(back['dat_content'], s,
                f'{label}: tricky string not byte-identical')

    def test_tdn_tab_shader_lossless(self):
        """A TAB-indented GLSL compute string round-trips byte-identical and
        serializes as a DOUBLE-QUOTED scalar (not a broken tab block) --
        guards against re-introducing the CSafe-unreadable tab-block override.
        """
        shader = ('void main()\n{\n\tvec4 c = vec4(1.0);\n'
                  '\tfragColor = c;\n}')
        dumped = self.tdn.tdn_dump(
            {'dat_content': shader, 'dat_content_format': 'text'})
        # A tab-bearing multi-line string must NOT become a literal block (|);
        # it falls back to a double-quoted scalar.
        self.assertNotIn('dat_content: |', dumped,
            'tab shader must not serialize as a literal block scalar')
        self.assertIn('"', dumped,
            'tab shader should serialize as a double-quoted scalar')
        back = self.tdn.tdn_load(dumped)
        self.assertEqual(back['dat_content'], shader,
            'tab shader not byte-identical after round-trip')

    # =================================================================
    # YAML typing safety
    # =================================================================

    def test_tdn_typing_safety(self):
        """Ambiguous YAML scalars survive as str; expression/bind shorthand
        ('=expr' / '~bind') emit UNQUOTED and survive as str."""
        ambiguous = [
            'off', 'on', 'yes', 'no', 'true', 'false', 'null', '~',
            '1.0', '007', '42', '12:30', '1:2:3', '.inf', '',
            '*x', '&y', '# z', '2026-06-10', 'y', 'n', '1e3',
        ]
        doc = {f'k{i}': v for i, v in enumerate(ambiguous)}
        back = self.tdn.tdn_load(self.tdn.tdn_dump(doc))
        for i, v in enumerate(ambiguous):
            key = f'k{i}'
            self.assertIsInstance(back[key], str,
                f'{v!r} did not survive as str (got {type(back[key]).__name__})')
            self.assertEqual(back[key], v,
                f'{v!r} changed value to {back[key]!r}')

        # Expression / bind shorthand must round-trip as str AND stay unquoted.
        shorthand = {
            'expr_frame': '=me.time.frame',
            'expr_op': '=op("ctrl").par.X',
            'bind_speed': '~Speed',
            'bind_op': '~op("c").par.Y',
        }
        dumped = self.tdn.tdn_dump(shorthand)
        back = self.tdn.tdn_load(dumped)
        for key, val in shorthand.items():
            self.assertEqual(back[key], val,
                f'{val!r} did not survive as str')
        # The leading '=' and '~' tokens must appear UNQUOTED in the output.
        self.assertIn('=me.time.frame', dumped)
        self.assertIn('~Speed', dumped)
        self.assertNotIn("'=me.time.frame'", dumped,
            'expression shorthand must not be quoted')
        self.assertNotIn("'~Speed'", dumped,
            'bind shorthand must not be quoted')

    # =================================================================
    # JSON back-compat (legacy tab-indented + BOM)
    # =================================================================

    def test_tdn_backcompat_legacy_json(self):
        """A legacy v1.5 tab-indented JSON .tdn (dat_content as a list of
        lines) imports, the multi-line DAT is rejoined, and the json-first
        fallback parses it even under a forced pure-python SafeLoader."""
        doc = {
            'format': 'tdn',
            'version': '1.5',
            'operators': [
                {'name': 'legacy_dat', 'type': 'textDAT',
                 'dat_content': ['one', 'two', 'three'],
                 'dat_content_format': 'text'},
            ],
        }
        # json.dumps(indent='\t') is exactly how legacy .tdn were written.
        legacy_text = json.dumps(doc, indent='\t')
        self.assertIn('\t', legacy_text)

        parsed = self.tdn.tdn_load(legacy_text)
        self.assertEqual(parsed['version'], '1.5')
        self.assertEqual(parsed['operators'][0]['dat_content'],
            ['one', 'two', 'three'])

        # Import it and confirm the list dat_content is rejoined with '\n'.
        target = self.sandbox.create(baseCOMP, 'legacy_json_target')
        result = self.tdn.ImportNetwork(
            target_path=target.path, tdn=parsed)
        self.assertTrue(result.get('success'))
        imported = target.op('legacy_dat')
        self.assertIsNotNone(imported)
        self.assertEqual(imported.text, 'one\ntwo\nthree')

        # The pure-python SafeLoader alone would ScannerError on the tab;
        # tdn_load's json-first path must succeed regardless of libyaml.
        self.assertRaises(yaml.YAMLError,
            yaml.load, legacy_text, Loader=yaml.SafeLoader)
        self.assertEqual(self.tdn.tdn_load(legacy_text), doc)

    def test_tdn_backcompat_bom_legacy_json(self):
        """A UTF-8 BOM-prefixed legacy tab-indented JSON .tdn loads via the
        json-first path (which strips the BOM before json.loads). Without the
        strip, json.loads raises and the pure-python YAML fallback would
        ScannerError on the tab -- so this also passes under a forced
        pure-python loader."""
        doc = {
            'format': 'tdn',
            'version': '1.5',
            'operators': [{'name': 'a', 'type': 'textDAT'}],
        }
        legacy_text = json.dumps(doc, indent='\t')
        bom_text = '\ufeff' + legacy_text

        parsed = self.tdn.tdn_load(bom_text)
        self.assertEqual(parsed, doc)

        # Forced pure-python loader on the raw BOM+tab text would fail; the
        # json-first strip is what rescues it. Confirm the stripped json.loads
        # succeeds where the bare YAML loader does not.
        self.assertRaises(yaml.YAMLError,
            yaml.load, bom_text, Loader=yaml.SafeLoader)
        self.assertEqual(json.loads(bom_text.lstrip('\ufeff').lstrip()), doc)

    # =================================================================
    # Determinism, trailing newline, anchors, isolation
    # =================================================================

    def test_tdn_determinism(self):
        """Dumping the same document N times is byte-identical (no key
        reorder, no anchor renumbering)."""
        doc = {
            'format': 'tdn', 'version': '2.0',
            'operators': [
                {'name': 'b', 'type': 'noiseTOP', 'position': [10, 20]},
                {'name': 'a', 'type': 'textDAT',
                 'dat_content': 'x\ny', 'dat_content_format': 'text'},
            ],
        }
        first = self.tdn.tdn_dump(doc)
        for _ in range(5):
            self.assertEqual(self.tdn.tdn_dump(doc), first,
                'tdn_dump is not deterministic')

    def test_tdn_trailing_newline(self):
        """tdn_dump output always ends with a single trailing newline --
        locks the contract test_export_file_not_truncated depends on now that
        the explicit `raw + '\\n'` serializer is gone."""
        doc = {'format': 'tdn', 'version': '2.0', 'operators': []}
        self.assertTrue(self.tdn.tdn_dump(doc).endswith('\n'))
        # Even single-line / scalar-only docs end with exactly one newline.
        out = self.tdn.tdn_dump({'k': 'v'})
        self.assertTrue(out.endswith('\n'))
        self.assertFalse(out.endswith('\n\n'))

    def test_tdn_no_anchors(self):
        """A document with two identical subtrees emits NO '&'/'*' anchor or
        alias tokens (locks the no-anchors decision; keeps block scalars)."""
        shared = {
            'name': 'c', 'type': 'baseCOMP',
            'dat_content': 'shared\nmulti\nline', 'dat_content_format': 'text',
        }
        doc = {
            'format': 'tdn', 'version': '2.0',
            'operators': [dict(shared), dict(shared)],
        }
        dumped = self.tdn.tdn_dump(doc)
        self.assertNotIn('&', dumped, 'unexpected YAML anchor token')
        self.assertNotIn('*', dumped, 'unexpected YAML alias token')

    def test_tdn_dumper_isolation(self):
        """Representers are scoped to the private dumper subclass: the global
        yaml.dump does NOT block-style a multi-line string, while tdn_dump
        DOES."""
        plain = yaml.dump({'k': 'a\nb'})
        self.assertNotIn('|', plain,
            'global SafeDumper unexpectedly uses block style')
        ours = self.tdn.tdn_dump({'k': 'a\nb'})
        self.assertIn('|', ours,
            'tdn_dump should use a literal block scalar for multi-line text')

    # =================================================================
    # Post-write validation (YAML + legacy JSON)
    # =================================================================

    def test_tdn_validate_yaml_file(self):
        """_validate_tdn_file accepts a freshly-written v2.0 YAML .tdn AND a
        legacy JSON .tdn (guards the post-write read-back)."""
        import tempfile
        d = tempfile.mkdtemp(prefix='tdn_yaml_val_')
        try:
            doc = {'format': 'tdn', 'version': '2.0',
                   'operators': [{'name': 'a', 'type': 'noiseTOP'}]}
            # v2.0 YAML
            yaml_fp = os.path.join(d, 'v2.tdn')
            Path(yaml_fp).write_text(
                self.tdn.tdn_dump(doc), encoding='utf-8')
            v = self.tdn._validate_tdn_file(yaml_fp)
            self.assertTrue(v.get('valid'),
                f'v2.0 YAML should validate: {v}')
            # Legacy JSON
            json_fp = os.path.join(d, 'legacy.tdn')
            Path(json_fp).write_text(
                json.dumps(doc, indent='\t'), encoding='utf-8')
            v2 = self.tdn._validate_tdn_file(json_fp)
            self.assertTrue(v2.get('valid'),
                f'legacy JSON should still validate: {v2}')
        finally:
            import shutil
            shutil.rmtree(d, ignore_errors=True)

    # =================================================================
    # textconv driver: both-sides normalization + degrade
    # =================================================================

    def _load_textconv(self):
        """Import the textconv driver template module from disk."""
        fp = os.path.join(
            project.folder, 'embody', 'Embody', 'templates',
            'text_tdn_textconv.py')
        if not os.path.isfile(fp):
            return None
        spec = importlib.util.spec_from_file_location(
            'tdn_textconv_under_test', fp)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_textconv_normalizes_both_sides(self):
        """The textconv driver normalizes a v1.5 JSON history blob and a v2.0
        YAML working-tree blob of the SAME network to IDENTICAL output
        (requires array->string normalization AND 'version' in VOLATILE_KEYS),
        and degrades to raw passthrough when yaml is unavailable."""
        mod = self._load_textconv()
        if mod is None:
            self.skipTest('textconv template not found')
        if not getattr(mod, '_HAVE_YAML', False):
            self.skipTest('PyYAML unavailable in textconv module')

        # Same network, two on-disk forms.
        v15 = {
            'format': 'tdn', 'version': '1.5',
            'build': 100, 'generator': 'Embody/5.0.1', 'td_build': '2025',
            'exported_at': '2026-06-09', 'source_file': 'Old.toe',
            'operators': [
                {'name': 'script', 'type': 'textDAT',
                 'dat_content': ['print(1)', 'print(2)'],
                 'dat_content_format': 'text'},
            ],
        }
        v20 = {
            'format': 'tdn', 'version': '2.0',
            'build': 200, 'generator': 'Embody/6.0.4', 'td_build': '2025',
            'exported_at': '2026-06-10', 'source_file': 'New.toe',
            'operators': [
                {'name': 'script', 'type': 'textDAT',
                 'dat_content': 'print(1)\nprint(2)',
                 'dat_content_format': 'text'},
            ],
        }
        json_blob = json.dumps(v15, indent='\t')
        yaml_blob = self.tdn.tdn_dump(v20)

        norm_json = mod.normalize(json_blob)
        norm_yaml = mod.normalize(yaml_blob)
        self.assertEqual(norm_json, norm_yaml,
            'textconv must normalize both format sides identically')

        # Degrade to raw passthrough when yaml is unavailable.
        orig = mod._HAVE_YAML
        try:
            mod._HAVE_YAML = False
            self.assertEqual(mod.normalize(json_blob), json_blob,
                'driver must return raw input when yaml is unavailable')
        finally:
            mod._HAVE_YAML = orig

    # =================================================================
    # Save-path serializer emits YAML (guards the third caller)
    # =================================================================

    def test_tdn_save_path_writes_yaml(self):
        """_compact_json_dumps (the save-path serializer, called from
        execute.py onProjectPreSave) returns YAML v2.0, not JSON: block
        scalars + space indentation, re-parseable to the same document."""
        doc = {
            'format': 'tdn', 'version': '2.0',
            'operators': [
                {'name': 'a', 'type': 'textDAT', 'position': [10, 20],
                 'dat_content': 'line1\nline2', 'dat_content_format': 'text'},
            ],
        }
        out = self.tdn._compact_json_dumps(doc)
        # YAML, not JSON: a JSON dump of this doc would start with '{'.
        self.assertFalse(out.lstrip().startswith('{'),
            'save-path serializer emitted JSON, not YAML')
        # Multi-line dat_content renders as a literal block scalar.
        self.assertIn('|', out, 'expected a YAML literal block scalar')
        # Space-indented (PyYAML never emits tab indentation).
        self.assertIn('  ', out)
        self.assertNotIn('\t', out, 'YAML output must not contain tabs')
        # Round-trips back to the same document.
        self.assertEqual(self.tdn.tdn_load(out), doc)
        self.assertTrue(out.endswith('\n'))

    # =================================================================
    # Boilerplate omission: default docked compute DAT
    # =================================================================

    def test_tdn_boilerplate_omission(self):
        """A glslTOP's default docked <name>_compute DAT exports with NO
        dat_content (TD recreates it); a CUSTOM compute text IS exported; and
        re-import yields the default present."""
        glsl = self.sandbox.create(glslTOP, 'glsl_omit')
        compute = glsl.op(f'{glsl.name}_compute')
        if compute is None:
            self.skipTest('glslTOP does not dock a <name>_compute DAT here')
        if compute.dock is None or compute.dock.path != glsl.path:
            self.skipTest('compute DAT is not docked to the glslTOP')

        default_text = compute.text

        # 1) Default compute -> omitted (no dat_content key on the entry).
        result = self.tdn.ExportNetwork(
            root_path=self.sandbox.path, include_dat_content=True)
        self.assertTrue(result.get('success'))
        entry = self._find_op(result['tdn']['operators'], compute.name)
        self.assertIsNotNone(entry,
            'compute DAT should still appear as an operator entry')
        self.assertNotIn('dat_content', entry,
            'default compute DAT should be omitted (no dat_content)')
        self.assertNotIn('dat_content_format', entry)

        # 2) Re-import into a clean COMP -> compute DAT present with default.
        target = self.sandbox.create(baseCOMP, 'glsl_reimport')
        self.tdn.ImportNetwork(
            target_path=target.path, tdn=result['tdn'], clear_first=True)
        imported_glsl = target.op('glsl_omit')
        self.assertIsNotNone(imported_glsl)
        imported_compute = target.op(f'{compute.name}')
        self.assertIsNotNone(imported_compute,
            'TD should recreate the docked compute DAT on import')
        self.assertEqual(imported_compute.text, default_text,
            'recreated compute DAT should hold the default text')

        # 3) Custom compute text -> IS exported.
        compute.text = '// custom compute shader\nlayout (local_size_x = 8) in;'
        result2 = self.tdn.ExportNetwork(
            root_path=self.sandbox.path, include_dat_content=True)
        entry2 = self._find_op(result2['tdn']['operators'], compute.name)
        self.assertIsNotNone(entry2)
        self.assertIn('dat_content', entry2,
            'customized compute DAT must be exported')
        self.assertEqual(entry2['dat_content'], compute.text)

    @staticmethod
    def _find_op(operators, name):
        for o in operators:
            if o.get('name') == name:
                return o
        return None
