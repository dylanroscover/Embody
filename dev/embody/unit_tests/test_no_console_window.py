"""
Test suite: no stray console windows.

TouchDesigner is a GUI process on Windows and owns no console, so spawning a
console program (git, uv, pip, schtasks) makes Windows allocate a NEW console
window -- a visible flash over the user's TD. Every subprocess spawned from
inside TD must pass creationflags=CREATE_NO_WINDOW.

This guard exists because that fix was applied one site at a time as symptoms
were reported: the highest-frequency spawn in the product -- the git-status
scan behind the manager's orange "uncommitted" badge, which runs on every
Refresh and therefore on every save -- went unguarded for two months while a
comment two files away named the exact symptom and its cure.
"""

import re
import sys
from pathlib import Path

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase

# Modules that execute INSIDE TouchDesigner. The STDIO bridge and the Convoy
# daemon are separate processes with their own console story -- not in scope.
_TD_SIDE_SOURCES = (
    'EmbodyExt.py', 'EnvoyExt.py', 'TDXNExt.py', 'CatalogManagerExt.py',
    'embody_git.py', 'embody_admin.py', 'embody_launch.py', 'envoy_setup.py',
    'convoy/convoy_install.py',
)

# A spawn may opt out ONLY by saying why, at the call site. Blanket token
# matching was tried first and is too easy to trip accidentally.
_EXEMPT_MARKER = 'no-console-window-exempt'
# Deliberately windowed: the terminal the user explicitly asked for.
_EXEMPT_TOKEN = 'CREATE_NEW_CONSOLE'

_SPAWN = re.compile(r'subprocess\.(?:run|Popen|check_output|check_call)\s*\(')
# A call may carry its flags in a shared kwargs dict (**git_kwargs).
_SPREAD = re.compile(r'\*\*(\w+)')


class TestNoConsoleWindow(EmbodyTestCase):

    def _td_side_sources(self):
        base = Path(project.folder) / 'embody' / 'Embody'
        for rel in _TD_SIDE_SOURCES:
            p = base / rel
            if p.is_file():
                yield rel, p.read_text(encoding='utf-8', errors='replace')

    def test_every_guarded_source_still_exists(self):
        """A rename must not silently turn this guard into a no-op."""
        found = sorted(rel for rel, _ in self._td_side_sources())
        self.assertEqual(
            sorted(_TD_SIDE_SOURCES), found,
            'A TD-side source was renamed or moved -- update '
            '_TD_SIDE_SOURCES, or this suite stops guarding it silently')

    @staticmethod
    def _guarded_kwargs_names(src):
        """Names of kwargs dicts in this file that already carry the flag.

        A call can spread its flags from a shared dict (`**git_kwargs`), so
        the flag lives at the dict's definition, not at the call.
        """
        names = set()
        for m in re.finditer(r'(\w+)\s*=\s*dict\(', src):
            start = m.end()
            depth, i = 1, start
            while i < len(src) and depth:
                if src[i] == '(':
                    depth += 1
                elif src[i] == ')':
                    depth -= 1
                i += 1
            if 'creationflags' in src[start:i]:
                names.add(m.group(1))
        return names

    def test_every_td_side_spawn_suppresses_its_console(self):
        offenders = []
        for rel, src in self._td_side_sources():
            guarded = self._guarded_kwargs_names(src)
            lines = src.splitlines()
            for i, line in enumerate(lines):
                if not _SPAWN.search(line):
                    continue
                call = '\n'.join(lines[i:i + 14])
                # The marker may sit on the lines just above the call.
                context = '\n'.join(lines[max(0, i - 4):i + 14])
                if 'creationflags' in call or _EXEMPT_TOKEN in call:
                    continue
                if _EXEMPT_MARKER in context:
                    continue
                if any(n in guarded for n in _SPREAD.findall(call)):
                    continue
                offenders.append('%s:%d: %s' % (rel, i + 1, line.strip()[:70]))
        self.assertEqual(
            [], offenders,
            'Each of these spawns a console program from inside TouchDesigner '
            'without creationflags, so it FLASHES a console window over the '
            "user's TD:\n  " + '\n  '.join(offenders))

    def test_the_git_status_scan_is_guarded(self):
        """The highest-frequency spawn: every Refresh, so every save."""
        mod_git = op.Embody.op('embody_git').module
        expected = 0x08000000 if sys.platform == 'win32' else 0
        self.assertEqual(expected, getattr(mod_git, 'NO_WINDOW', None))
