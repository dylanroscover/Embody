"""
Tests for the Auto/Advanced mode chokepoint (_embodyMode / _guard).

_guard is the single gate every invasive, project-level action flows through:
  - auto     -> apply immediately (silent)
  - advanced -> confirm via _messageBox (Apply/Skip); apply only on Apply
  - a suppressed/headless dialog (-1) declines in advanced (never act without an
    explicit yes when the user asked to be consulted)

Mode is passed explicitly so these tests don't depend on live param state, and
the advanced confirm is driven through the real _messageBox seeding machinery
(_smoke_test_responses). Pure-logic -- no operators, NOT destructive.
"""


class TestEmbodyModeGuard(EmbodyTestCase):

    TITLE = 'Embody guard test'

    def setUp(self):
        self._saved = self.embody.fetch('_smoke_test_responses', None, search=False)

    def tearDown(self):
        if self._saved is not None:
            self.embody.store('_smoke_test_responses', self._saved)
        else:
            self.embody.unstore('_smoke_test_responses')

    @property
    def ext(self):
        return self.embody_ext

    def _seed(self, idx):
        self.embody.store('_smoke_test_responses', {self.TITLE: idx})

    # ----- auto: act silently ---------------------------------------------

    def test_auto_applies_without_prompt(self):
        self.embody.unstore('_smoke_test_responses')  # no seed at all
        applied = []
        ok = self.ext._guard(self.TITLE, 'msg', lambda: applied.append(1), mode='auto')
        self.assertTrue(ok)
        self.assertEqual(applied, [1], 'auto must apply the action')

    # ----- advanced: ask first --------------------------------------------

    def test_advanced_applies_on_confirm(self):
        self._seed(0)  # Apply
        applied = []
        ok = self.ext._guard(self.TITLE, 'msg', lambda: applied.append(1), mode='advanced')
        self.assertTrue(ok)
        self.assertEqual(applied, [1])

    def test_advanced_skips_on_decline(self):
        self._seed(1)  # Skip
        applied = []
        ok = self.ext._guard(self.TITLE, 'msg', lambda: applied.append(1), mode='advanced')
        self.assertFalse(ok)
        self.assertEqual(applied, [], 'declined action must not run')

    def test_advanced_skips_when_suppressed(self):
        self.embody.unstore('_smoke_test_responses')  # _messageBox -> -1
        applied = []
        ok = self.ext._guard(self.TITLE, 'msg', lambda: applied.append(1), mode='advanced')
        self.assertFalse(ok)
        self.assertEqual(applied, [],
                         'a suppressed/headless dialog must decline in advanced')

    # ----- default posture -------------------------------------------------

    def test_guard_uses_embody_mode_when_mode_omitted(self):
        # With no explicit mode, _guard consults _embodyMode(); with no param
        # authored that resolves to 'auto', so the action applies silently.
        self.embody.unstore('_smoke_test_responses')
        applied = []
        ok = self.ext._guard(self.TITLE, 'msg', lambda: applied.append(1))
        self.assertTrue(ok)
        self.assertEqual(applied, [1])
