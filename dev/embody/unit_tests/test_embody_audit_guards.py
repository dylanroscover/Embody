"""EmbodyExt defects found by reading, pinned on a fake `self` (audit 2026-11-10).

- The tagger's Reload sent a TDXN COMP to the TOX reload: the strategy was
  assigned the table CELL value ('tdxn') and compared to the WIRE value
  ('tdn'), so the TDXN branch never ran.
- ExportPortableTox restored a snapshot it only assigned inside its try, so
  the failure it promises to contain raised UnboundLocalError instead.
- Update's result lines shared one plural flag ("Saved 1 externalizations").
- The Exclusions panel raised on a COMP drop when its target was gone.

Pure Python: EmbodyExt.py imports only the stdlib at module level.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path
from types import SimpleNamespace

_EMBODY = Path(__file__).resolve().parents[1] / "Embody"
_SOURCE = _EMBODY / "EmbodyExt.py"


def _load(name, relpath):
    spec = importlib.util.spec_from_file_location(name, _EMBODY / relpath)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


_MOD = _load("audit_guard_embody", "EmbodyExt.py")
_EXT = _MOD.EmbodyExt


def _reload_rig(has_tdxn_tag, tags=(), table_strategy=None):
    calls = []
    fake = SimpleNamespace(
        my=SimpleNamespace(par=SimpleNamespace(
            Toxtag=SimpleNamespace(val='tox'),
            Tdxntag=SimpleNamespace(val='tdxn'))),
        _TDXN_STRATEGY_CELL=_EXT._TDXN_STRATEGY_CELL,
        _TDXN_STRATEGY_WIRE=_EXT._TDXN_STRATEGY_WIRE,
        _hasTDXNTag=lambda oper: has_tdxn_tag,
        _getCompStrategy=lambda oper: table_strategy,
        _tagLabel=lambda value: str(value),
        _messageBox=lambda *args, **kwargs: 1,
        _reloadTDXN=lambda oper: calls.append('tdxn'),
        _reloadTox=lambda oper: calls.append('tox'),
        Refresh=lambda: None)
    fake._normalizeStrategy = lambda value: _EXT._normalizeStrategy(fake, value)
    oper = SimpleNamespace(path='/project/comp', tags=set(tags))
    _EXT.handleReload(fake, oper)
    return calls


def test_reload_of_a_tdxn_tagged_comp_reloads_its_tdxn():
    assert _reload_rig(has_tdxn_tag=True) == ['tdxn']


def test_reload_of_an_untagged_tdxn_row_reloads_its_tdxn():
    # _getCompStrategy answers with the wire value
    assert _reload_rig(has_tdxn_tag=False, table_strategy='tdn') == ['tdxn']


def test_reload_of_a_tox_comp_still_reloads_the_tox():
    assert _reload_rig(has_tdxn_tag=False, tags={'tox'}) == ['tox']
    assert _reload_rig(has_tdxn_tag=False, table_strategy=None) == ['tox']


def test_report_results_pluralises_each_line_on_its_own_count():
    logged = []
    fake = SimpleNamespace(Log=lambda message, level='INFO': logged.append(message))
    _EXT._reportResults(fake, ['a'], ['b', 'c'], [])
    assert logged == ['Saved 1 externalization', 'Added 2 operators in total']


def test_export_portable_tox_initialises_every_snapshot_it_restores():
    """Phases 1-3 are exception-contained, so everything the unconditional
    restore block reads must exist before the try."""
    tree = ast.parse(_SOURCE.read_text(encoding='utf-8'))
    func = next(node for node in ast.walk(tree)
                if isinstance(node, ast.FunctionDef)
                and node.name == 'ExportPortableTox')
    first_try = next(i for i, node in enumerate(func.body)
                     if isinstance(node, ast.Try))
    assigned_before = set()
    for node in func.body[:first_try]:
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Store):
                assigned_before.add(sub.id)
    restored = set()
    for node in func.body[first_try + 1:]:
        for sub in ast.walk(node):
            if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)
                    and sub.func.attr.startswith('_restore')):
                restored.update(arg.id for arg in sub.args
                                if isinstance(arg, ast.Name))
    assert restored, 'the restore block moved; update this guard'
    assert restored <= assigned_before, sorted(restored - assigned_before)


def test_omit_panel_comp_drop_survives_a_missing_target():
    """in_scope() treats a missing target as "anywhere", so the direct-child
    warning must not read target.path."""
    logged, refreshed = [], []

    class FakeComp:
        type = 'base'
        path = '/project/geo1'

        def __init__(self):
            self.tags = set()

        def parent(self):
            return object()

    class FakePar:
        pass

    panel = SimpleNamespace(fetch=lambda key, default, search=False: '')
    fake = SimpleNamespace(
        my=SimpleNamespace(
            op=lambda name: panel,
            par=SimpleNamespace(Tdxnexcludetag=SimpleNamespace(
                eval=lambda: 'tdxn_exclude'))),
        Log=lambda message, level='INFO': logged.append((level, message)),
        refreshOmitPanel=lambda: refreshed.append(True))
    # TD's globals, as the extension sees them inside TouchDesigner
    planted = {'op': lambda path: None, 'COMP': FakeComp, 'Par': FakePar}
    missing = object()
    saved = {name: getattr(_MOD, name, missing) for name in planted}
    for name, value in planted.items():
        setattr(_MOD, name, value)
    try:
        item = FakeComp()
        _EXT.omitPanelDrop(fake, [item])
    finally:
        for name, value in saved.items():
            if value is missing:
                delattr(_MOD, name)
            else:
                setattr(_MOD, name, value)
    assert item.tags == {'tdxn_exclude'}
    assert refreshed == [True]
    assert [level for level, _message in logged] == ['SUCCESS']
