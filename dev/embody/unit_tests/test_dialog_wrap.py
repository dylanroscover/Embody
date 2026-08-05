"""Every dialog's prose wraps to readable lines (field feedback
2026-08-05: ui.messageBox sizes itself to its longest line, so unwrapped
prose made screen-wide dialogs). One choke point does it --
EmbodyExt._wrapDialogText, applied in _messageBox and at every direct
call site -- so these tests pin the wrapping contract itself.
"""

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase


class TestDialogWrap(EmbodyTestCase):

    def wrap(self, text, **kw):
        return op.Embody.ext.Embody._wrapDialogText(text, **kw)

    def test_long_prose_wraps_to_about_seventy_chars(self):
        prose = ('A forgotten node rejoins as a new identity the next '
                 'time its project opens, and its TD Python approval '
                 'resets, so read this dialog before you confirm it.')
        wrapped = self.wrap(prose)
        lines = wrapped.split('\n')
        self.assertGreater(len(lines), 1, 'long prose must wrap')
        for line in lines:
            self.assertLessEqual(len(line), 70, repr(line))
        self.assertEqual(' '.join(wrapped.split()), ' '.join(prose.split()),
                         'wrapping must never alter the words')

    def test_authored_structure_is_preserved(self):
        text = ('Header line.\n'
                '\n'
                '- first item\n'
                '- second item\n'
                '\n'
                'Footer.')
        self.assertEqual(self.wrap(text), text,
                         'short authored lines pass through untouched, '
                         'blank lines and all')

    def test_list_items_wrap_with_a_hanging_indent(self):
        item = ('- TEC-MBA.local / a-very-long-node-display-name '
                '(offline for two hours and forty-one minutes today)')
        wrapped = self.wrap(item)
        lines = wrapped.split('\n')
        self.assertGreater(len(lines), 1)
        for cont in lines[1:]:
            self.assertTrue(cont.startswith('  '),
                            'a wrapped continuation must indent so the '
                            'item still reads as one bullet: %r' % cont)

    def test_long_tokens_are_never_broken(self):
        token = 'C:/Users/someone/Documents/very/deep/project/folder/show.toe'
        wrapped = self.wrap('The file lives at %s on this machine.' % token)
        self.assertIn(token, wrapped.replace('\n', ' '),
                      'paths and other long tokens must survive intact '
                      'on their own line rather than being hyphenated')

    def test_the_message_box_choke_point_applies_the_wrap(self):
        src = op.Embody.op('EmbodyExt').text
        gate = src.split('def _messageBox', 1)[1]
        self.assertIn('_wrapDialogText', gate,
                      'the real ui.messageBox call must wrap its message')
        self.assertNotIn(
            'ui.messageBox(title, message,', gate,
            'no unwrapped passthrough may remain at the choke point')
