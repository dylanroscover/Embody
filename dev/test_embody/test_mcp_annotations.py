"""
Test suite: MCP annotation tools in ClaudiusExt.

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
        self.claudius = self.embody.ext.Claudius
        self.workspace = self.sandbox.create(baseCOMP, 'ann_workspace')

    # =========================================================================
    # _create_annotation — modes
    # =========================================================================

    def test_create_annotation_default_mode(self):
        """_create_annotation with default mode should create an annotate annotation."""
        result = self.claudius._create_annotation(
            parent_path=self.workspace.path
        )
        self.assertTrue(result.get('success'))
        self.assertDictHasKey(result, 'path')

    def test_create_annotation_comment_mode(self):
        """_create_annotation with comment mode should succeed."""
        result = self.claudius._create_annotation(
            parent_path=self.workspace.path,
            mode='comment'
        )
        self.assertTrue(result.get('success'))

    def test_create_annotation_networkbox_mode(self):
        """_create_annotation with networkbox mode should succeed."""
        result = self.claudius._create_annotation(
            parent_path=self.workspace.path,
            mode='networkbox'
        )
        self.assertTrue(result.get('success'))

    # =========================================================================
    # _create_annotation — parameters
    # =========================================================================

    def test_create_annotation_with_text(self):
        """_create_annotation should set body text."""
        result = self.claudius._create_annotation(
            parent_path=self.workspace.path,
            text='Hello annotation'
        )
        self.assertTrue(result.get('success'))
        ann = op(result['path'])
        self.assertEqual(ann.par.Bodytext.eval(), 'Hello annotation')

    def test_create_annotation_with_title(self):
        """_create_annotation should set title text."""
        result = self.claudius._create_annotation(
            parent_path=self.workspace.path,
            title='My Title'
        )
        self.assertTrue(result.get('success'))
        ann = op(result['path'])
        self.assertEqual(ann.par.Titletext.eval(), 'My Title')

    def test_create_annotation_with_position(self):
        """_create_annotation should set x/y position."""
        result = self.claudius._create_annotation(
            parent_path=self.workspace.path,
            x=100, y=200
        )
        self.assertTrue(result.get('success'))
        self.assertEqual(result['nodeX'], 100)
        self.assertEqual(result['nodeY'], 200)

    def test_create_annotation_with_size(self):
        """_create_annotation should set width/height."""
        result = self.claudius._create_annotation(
            parent_path=self.workspace.path,
            width=500, height=300
        )
        self.assertTrue(result.get('success'))
        self.assertEqual(result['nodeWidth'], 500)
        self.assertEqual(result['nodeHeight'], 300)

    def test_create_annotation_with_name(self):
        """_create_annotation should rename the annotation."""
        result = self.claudius._create_annotation(
            parent_path=self.workspace.path,
            name='my_note'
        )
        self.assertTrue(result.get('success'))
        self.assertEqual(result['name'], 'my_note')

    def test_create_annotation_with_color(self):
        """_create_annotation should set background color."""
        result = self.claudius._create_annotation(
            parent_path=self.workspace.path,
            color=[0.8, 0.2, 0.1]
        )
        self.assertTrue(result.get('success'))
        ann = op(result['path'])
        self.assertApproxEqual(ann.par.Backcolorr.eval(), 0.8, tolerance=0.01)

    # =========================================================================
    # _create_annotation — errors
    # =========================================================================

    def test_create_annotation_invalid_mode(self):
        """_create_annotation with invalid mode should return error."""
        result = self.claudius._create_annotation(
            parent_path=self.workspace.path,
            mode='invalid_mode'
        )
        self.assertDictHasKey(result, 'error')

    def test_create_annotation_invalid_parent(self):
        """_create_annotation with nonexistent parent should return error."""
        result = self.claudius._create_annotation(
            parent_path='/nonexistent/path'
        )
        self.assertDictHasKey(result, 'error')

    # =========================================================================
    # _get_annotations
    # =========================================================================

    def test_get_annotations_returns_list(self):
        """_get_annotations should return a dict with annotations list."""
        result = self.claudius._get_annotations(
            parent_path=self.workspace.path
        )
        self.assertDictHasKey(result, 'annotations')
        self.assertIsInstance(result['annotations'], list)

    def test_get_annotations_finds_created(self):
        """_get_annotations should find an annotation we just created."""
        self.claudius._create_annotation(
            parent_path=self.workspace.path,
            text='findme'
        )
        result = self.claudius._get_annotations(
            parent_path=self.workspace.path
        )
        self.assertGreater(result['count'], 0)
        texts = [a['body_text'] for a in result['annotations']]
        self.assertIn('findme', texts)

    def test_get_annotations_invalid_parent(self):
        """_get_annotations with nonexistent parent should return error."""
        result = self.claudius._get_annotations(
            parent_path='/nonexistent/path'
        )
        self.assertDictHasKey(result, 'error')

    # =========================================================================
    # _set_annotation
    # =========================================================================

    def test_set_annotation_modify_text(self):
        """_set_annotation should modify body text."""
        create_result = self.claudius._create_annotation(
            parent_path=self.workspace.path,
            text='original'
        )
        result = self.claudius._set_annotation(
            op_path=create_result['path'],
            text='modified'
        )
        self.assertTrue(result.get('success'))
        self.assertEqual(result['body_text'], 'modified')

    def test_set_annotation_modify_title(self):
        """_set_annotation should modify title text."""
        create_result = self.claudius._create_annotation(
            parent_path=self.workspace.path,
            title='old title'
        )
        result = self.claudius._set_annotation(
            op_path=create_result['path'],
            title='new title'
        )
        self.assertTrue(result.get('success'))
        self.assertEqual(result['title_text'], 'new title')

    def test_set_annotation_nonexistent(self):
        """_set_annotation on nonexistent path should return error."""
        result = self.claudius._set_annotation(
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
        create_result = self.claudius._create_annotation(
            parent_path=self.workspace.path,
            mode='networkbox',
            x=-500, y=-500, width=2000, height=2000
        )
        result = self.claudius._get_enclosed_ops(
            op_path=create_result['path']
        )
        self.assertTrue(result.get('is_annotation'))
        self.assertDictHasKey(result, 'enclosed_ops')

    def test_get_enclosed_ops_nonexistent(self):
        """_get_enclosed_ops on nonexistent path should return error."""
        result = self.claudius._get_enclosed_ops(
            op_path='/nonexistent/path'
        )
        self.assertDictHasKey(result, 'error')
