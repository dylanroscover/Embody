"""
Test suite: MCP annotation tools in EnvoyExt.

Tests _create_annotation, _get_annotations, _set_annotation, _get_enclosed_ops.
"""

# Import EmbodyTestCase (injected by runner, or from DAT for backwards compat)
try:
    runner_mod = op.unit_tests.op('TestRunnerExt').module
    EmbodyTestCase = runner_mod.EmbodyTestCase
except (AttributeError, NameError):
    pass  # EmbodyTestCase already injected by test runner


class TestMCPAnnotations(EmbodyTestCase):

    def setUp(self):
        super().setUp()
        self.envoy = self.embody.ext.Envoy
        self.workspace = self.sandbox.create(baseCOMP, 'ann_workspace')

    # =========================================================================
    # _create_annotation - modes
    # =========================================================================

    def test_create_annotation_default_mode(self):
        """_create_annotation with default mode should create an annotate annotation."""
        result = self.envoy._create_annotation(
            parent_path=self.workspace.path
        )
        self.assertTrue(result.get('success'))
        self.assertDictHasKey(result, 'path')

    def test_create_annotation_comment_mode(self):
        """_create_annotation with comment mode should succeed."""
        result = self.envoy._create_annotation(
            parent_path=self.workspace.path,
            mode='comment'
        )
        self.assertTrue(result.get('success'))

    def test_create_annotation_networkbox_mode(self):
        """_create_annotation with networkbox mode should succeed."""
        result = self.envoy._create_annotation(
            parent_path=self.workspace.path,
            mode='networkbox'
        )
        self.assertTrue(result.get('success'))

    # =========================================================================
    # _create_annotation - parameters
    # =========================================================================

    def test_create_annotation_with_text(self):
        """_create_annotation should set body text."""
        result = self.envoy._create_annotation(
            parent_path=self.workspace.path,
            text='Hello annotation'
        )
        self.assertTrue(result.get('success'))
        # Annotations are utility=True from birth -- bare op() cannot see
        # them; resolve through the utility-aware resolver.
        ann = self.envoy._resolve_annotation(result['path'])
        self.assertEqual(ann.par.Bodytext.eval(), 'Hello annotation')

    def test_create_annotation_with_title(self):
        """_create_annotation should set title text."""
        result = self.envoy._create_annotation(
            parent_path=self.workspace.path,
            title='My Title'
        )
        self.assertTrue(result.get('success'))
        ann = self.envoy._resolve_annotation(result['path'])
        self.assertEqual(ann.par.Titletext.eval(), 'My Title')

    def test_create_annotation_with_position(self):
        """_create_annotation should set x/y position."""
        result = self.envoy._create_annotation(
            parent_path=self.workspace.path,
            x=100, y=200
        )
        self.assertTrue(result.get('success'))
        self.assertEqual(result['nodeX'], 100)
        self.assertEqual(result['nodeY'], 200)

    def test_create_annotation_with_size(self):
        """_create_annotation should set width/height."""
        result = self.envoy._create_annotation(
            parent_path=self.workspace.path,
            width=500, height=300
        )
        self.assertTrue(result.get('success'))
        self.assertEqual(result['nodeWidth'], 500)
        self.assertEqual(result['nodeHeight'], 300)

    def test_create_annotation_with_name(self):
        """_create_annotation should rename the annotation."""
        result = self.envoy._create_annotation(
            parent_path=self.workspace.path,
            name='my_note'
        )
        self.assertTrue(result.get('success'))
        self.assertEqual(result['name'], 'my_note')

    def test_create_annotation_with_color(self):
        """_create_annotation should set background color."""
        result = self.envoy._create_annotation(
            parent_path=self.workspace.path,
            color=[0.8, 0.2, 0.1]
        )
        self.assertTrue(result.get('success'))
        ann = self.envoy._resolve_annotation(result['path'])
        self.assertAlmostEqual(ann.par.Backcolorr.eval(), 0.8, delta=0.01)

    # =========================================================================
    # _create_annotation - errors
    # =========================================================================

    def test_create_annotation_invalid_mode(self):
        """_create_annotation with invalid mode should return error."""
        result = self.envoy._create_annotation(
            parent_path=self.workspace.path,
            mode='invalid_mode'
        )
        self.assertDictHasKey(result, 'error')

    def test_create_annotation_invalid_parent(self):
        """_create_annotation with nonexistent parent should return error."""
        result = self.envoy._create_annotation(
            parent_path='/nonexistent/path'
        )
        self.assertDictHasKey(result, 'error')

    # =========================================================================
    # _get_annotations
    # =========================================================================

    def test_get_annotations_returns_list(self):
        """_get_annotations should return a dict with annotations list."""
        result = self.envoy._get_annotations(
            parent_path=self.workspace.path
        )
        self.assertDictHasKey(result, 'annotations')
        self.assertIsInstance(result['annotations'], list)

    def test_get_annotations_finds_created(self):
        """_get_annotations should find an annotation we just created."""
        self.envoy._create_annotation(
            parent_path=self.workspace.path,
            text='findme'
        )
        result = self.envoy._get_annotations(
            parent_path=self.workspace.path
        )
        self.assertGreater(result['count'], 0)
        texts = [a['body_text'] for a in result['annotations']]
        self.assertIn('findme', texts)

    def test_get_annotations_invalid_parent(self):
        """_get_annotations with nonexistent parent should return error."""
        result = self.envoy._get_annotations(
            parent_path='/nonexistent/path'
        )
        self.assertDictHasKey(result, 'error')

    # =========================================================================
    # _set_annotation
    # =========================================================================

    def test_set_annotation_modify_text(self):
        """_set_annotation should modify body text."""
        create_result = self.envoy._create_annotation(
            parent_path=self.workspace.path,
            text='original'
        )
        result = self.envoy._set_annotation(
            op_path=create_result['path'],
            text='modified'
        )
        self.assertTrue(result.get('success'))
        self.assertEqual(result['body_text'], 'modified')

    def test_set_annotation_modify_title(self):
        """_set_annotation should modify title text."""
        create_result = self.envoy._create_annotation(
            parent_path=self.workspace.path,
            title='old title'
        )
        result = self.envoy._set_annotation(
            op_path=create_result['path'],
            title='new title'
        )
        self.assertTrue(result.get('success'))
        self.assertEqual(result['title_text'], 'new title')

    def test_set_annotation_nonexistent(self):
        """_set_annotation on nonexistent path should return error."""
        result = self.envoy._set_annotation(
            op_path='/nonexistent/ann',
            text='test'
        )
        self.assertDictHasKey(result, 'error')

    # =========================================================================
    # _get_enclosed_ops
    # =========================================================================

    def test_get_enclosed_ops_annotation(self):
        """_get_enclosed_ops on an annotation should return enclosed_ops list."""
        # Create a networkbox large enough to enclose something
        create_result = self.envoy._create_annotation(
            parent_path=self.workspace.path,
            mode='networkbox',
            x=-500, y=-500, width=2000, height=2000
        )
        result = self.envoy._get_enclosed_ops(
            op_path=create_result['path']
        )
        self.assertTrue(result.get('is_annotation'))
        self.assertDictHasKey(result, 'enclosed_ops')

    def test_get_enclosed_ops_nonexistent(self):
        """_get_enclosed_ops on nonexistent path should return error."""
        result = self.envoy._get_enclosed_ops(
            op_path='/nonexistent/path'
        )
        self.assertDictHasKey(result, 'error')

    # =========================================================================
    # Utility-flagged annotations (UI-created) -- path resolution regression
    # =========================================================================

    def test_set_annotation_resolves_utility_flagged(self):
        """_set_annotation must modify a utility-flagged annotation by path.

        Every UI-created annotation has utility=True, which hides it from
        op(), parent.op() and .children entirely -- only
        findChildren(includeUtility=True) sees it. A plain op() lookup
        therefore failed for exactly the annotations users make by hand
        (regression: 'Annotation not found' on a path get_annotations had
        just listed).
        """
        create_result = self.envoy._create_annotation(
            parent_path=self.workspace.path,
            title='util resolve'
        )
        ann_path = create_result['path']
        ann = self.envoy._resolve_annotation(ann_path)
        self.assertIsNotNone(ann)
        ann.utility = True  # match TD UI behavior

        result = self.envoy._set_annotation(
            op_path=ann_path,
            title='resolved anyway',
            width=555
        )
        self.assertFalse('error' in result,
                         'set_annotation failed on utility-flagged '
                         'annotation: {}'.format(result.get('error')))
        self.assertEqual(result.get('title_text'), 'resolved anyway')

    def test_get_enclosed_ops_resolves_utility_flagged(self):
        """_get_enclosed_ops must resolve a utility-flagged annotation."""
        create_result = self.envoy._create_annotation(
            parent_path=self.workspace.path,
            mode='networkbox',
            x=-500, y=-500, width=2000, height=2000
        )
        ann_path = create_result['path']
        ann = self.envoy._resolve_annotation(ann_path)
        self.assertIsNotNone(ann)
        ann.utility = True

        result = self.envoy._get_enclosed_ops(op_path=ann_path)
        self.assertFalse('error' in result,
                         '_get_enclosed_ops failed on utility-flagged '
                         'annotation: {}'.format(result.get('error')))
        self.assertTrue(result.get('is_annotation'))

    # =========================================================================
    # utility=True from birth + universal op-path resolution
    #
    # create_annotation adopts the TD-UI convention (utility=True), and every
    # op-path tool resolves utility ops via resolve_op. Regression source: the
    # TDN annotation double-serialization report (2026-07-21) -- delete_op
    # returned "Operator not found" on a path get_annotations had just listed,
    # pushing agents to raw .destroy() (which is not deletion-durable).
    # =========================================================================

    def test_create_annotation_sets_utility(self):
        """MCP-created annotations must be utility=True from birth."""
        result = self.envoy._create_annotation(
            parent_path=self.workspace.path, title='born utility')
        self.assertTrue(result.get('success'))
        ann = self.envoy._resolve_annotation(result['path'])
        self.assertIsNotNone(ann)
        self.assertTrue(ann.utility)
        # And therefore hidden from bare op(), like every UI annotation.
        self.assertIsNone(op(result['path']))

    def test_delete_op_deletes_utility_annotation(self):
        """delete_op must resolve and destroy a utility annotation."""
        result = self.envoy._create_annotation(
            parent_path=self.workspace.path, title='doomed')
        path = result['path']
        del_result = self.envoy._delete_op(path)
        self.assertTrue(del_result.get('success'),
                        'delete_op failed: {}'.format(del_result.get('error')))
        anns = self.workspace.findChildren(
            type=annotateCOMP, includeUtility=True)
        self.assertNotIn('doomed',
                         [a.par.Titletext.eval() for a in anns])

    def test_get_op_resolves_utility_annotation(self):
        result = self.envoy._create_annotation(
            parent_path=self.workspace.path, title='readable')
        info = self.envoy._get_op(result['path'])
        self.assertFalse('error' in info,
                         'get_op failed: {}'.format(info.get('error')))
        self.assertEqual(info.get('type'), 'annotateCOMP')

    def test_set_parameter_resolves_utility_annotation(self):
        result = self.envoy._create_annotation(
            parent_path=self.workspace.path)
        set_result = self.envoy._set_parameter(
            result['path'], 'Opacity', value='0.5')
        self.assertTrue(set_result.get('success'),
                        'set_parameter failed: {}'.format(
                            set_result.get('error')))
        ann = self.envoy._resolve_annotation(result['path'])
        self.assertAlmostEqual(ann.par.Opacity.eval(), 0.5, delta=0.01)

    def test_set_op_position_resolves_utility_annotation(self):
        result = self.envoy._create_annotation(
            parent_path=self.workspace.path, x=0, y=0)
        pos_result = self.envoy._set_op_position(result['path'], x=600, y=-600)
        self.assertFalse('error' in pos_result,
                         'set_op_position failed: {}'.format(
                             pos_result.get('error')))
        ann = self.envoy._resolve_annotation(result['path'])
        self.assertEqual(ann.nodeX, 600)

    def test_externalize_op_refuses_annotation(self):
        """Annotations round-trip semantically; externalize_op must refuse."""
        result = self.envoy._create_annotation(
            parent_path=self.workspace.path, title='never a boundary')
        ext_result = self.envoy._externalize_op(result['path'])
        self.assertIn('error', ext_result)
        self.assertIn('annotation', ext_result['error'].lower())

    def test_get_parameter_resolves_utility_annotation(self):
        """get_parameter must resolve the same paths set_parameter does."""
        result = self.envoy._create_annotation(
            parent_path=self.workspace.path, title='param read')
        pr = self.envoy._get_parameter(result['path'], par_name='Titletext')
        self.assertFalse('error' in pr,
                         'get_parameter failed: {}'.format(pr.get('error')))
        self.assertEqual(pr.get('value'), 'param read')

    def test_query_network_resolves_utility_annotation_parent(self):
        """query_network pointed AT a utility annotation (to inspect its
        interior) must resolve it as the parent."""
        result = self.envoy._create_annotation(
            parent_path=self.workspace.path)
        qn = self.envoy._query_network(parent_path=result['path'])
        self.assertFalse('error' in qn,
                         'query_network failed: {}'.format(qn.get('error')))

    def test_get_annotations_resolves_utility_annotation_parent(self):
        """get_annotations on a utility annotation's own path (nested
        annotation discovery) must not error 'Parent not found'."""
        result = self.envoy._create_annotation(
            parent_path=self.workspace.path)
        ga = self.envoy._get_annotations(parent_path=result['path'])
        self.assertFalse('error' in ga,
                         'get_annotations failed: {}'.format(ga.get('error')))

    def test_resolve_op_reaches_annotation_interior(self):
        """resolve_op must resolve ops behind a utility annotate hop (the
        widget's own internals, or ops created inside it)."""
        result = self.envoy._create_annotation(
            parent_path=self.workspace.path)
        ann = self.envoy._resolve_annotation(result['path'])
        inner = ann.create(textDAT)
        inner.text = 'interior probe'
        resolved = self.envoy._resolve_op(inner.path)
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.path, inner.path)
        dc = self.envoy._get_dat_content(inner.path)
        self.assertFalse('error' in dc,
                         'get_dat_content failed on interior path: '
                         '{}'.format(dc.get('error')))
