"""What a node tells the host before Convoy restarts it, on the CI matrix.

TD 2025.33230 reports project.modified as a list of operator paths, never a
bool, and it is never empty: 648 entries right after a save (measured
2026-10-31). A guard that wanted a bool refused every restart. These run the
real methods against a stand-in project, so the contract runs without TD.
"""

import importlib.util
import os
import types

_EXT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'Embody', 'EnvoyExt.py')


def _module():
    spec = importlib.util.spec_from_file_location(
        'envoy_ext_lifecycle_state', _EXT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _envoy(tmp_path, modified, resolved='e1.7.toe'):
    """An EnvoyExt on a stand-in project: returns (ext, scheduled, quits)."""
    mod = _module()
    toe = None
    if resolved:
        path = tmp_path / resolved
        path.write_bytes(b'toe')
        toe = str(path)
    quits = []
    mod.project = types.SimpleNamespace(
        modified=modified, folder=str(tmp_path), name='e1.8.toe', saveTime='t',
        quit=lambda force=False: quits.append(force))
    scheduled = []
    mod.run = lambda code, **kwargs: scheduled.append(code)
    ext = object.__new__(mod.EnvoyExt)
    embody = types.SimpleNamespace(_resolveProjectToe=lambda: toe)
    ext.__dict__.update(
        ownerComp=types.SimpleNamespace(
            path='/embody/Embody',
            ext=types.SimpleNamespace(Embody=embody)),
        _log=lambda *_a, **_k: None)
    return ext, scheduled, quits


def test_a_modified_list_is_a_dirty_project_not_an_unknown_one(tmp_path):
    ext, _s, _q = _envoy(tmp_path, ['/', '/project1/noise1'])
    state = ext._convoy_lifecycle_state()
    assert state['ok'] is True
    assert state['dirty'] is True
    assert state['unsaved'] is False
    assert isinstance(state['revision'], str) and state['revision']


def test_an_empty_list_is_clean(tmp_path):
    ext, _s, _q = _envoy(tmp_path, [])
    assert ext._convoy_lifecycle_state()['dirty'] is False


def test_a_build_that_reports_a_bool_still_works(tmp_path):
    ext, _s, _q = _envoy(tmp_path, False)
    assert ext._convoy_lifecycle_state()['dirty'] is False


def test_a_shape_nobody_has_seen_fails_closed(tmp_path):
    ext, _s, _q = _envoy(tmp_path, 'surprise')
    assert ext._convoy_lifecycle_state()['code'] == 'dirty_state_unknown'


def test_the_next_incremental_name_is_not_an_unsaved_project(tmp_path):
    """After a versioned save project.name is the NEXT name, with no file yet.
    The saved file is what the resolver finds."""
    ext, _s, _q = _envoy(tmp_path, ['/'])
    assert ext._convoy_lifecycle_state()['unsaved'] is False


def test_no_saved_file_at_all_is_unsaved(tmp_path):
    ext, _s, _q = _envoy(tmp_path, [], resolved=None)
    assert ext._convoy_lifecycle_state()['unsaved'] is True


def test_housekeeping_noise_does_not_move_the_revision(tmp_path):
    """Embody re-dirties the project for seconds after a save: the quit's
    compare-and-swap must not trip on the list growing."""
    ext, _s, _q = _envoy(tmp_path, ['/'])
    before = ext._convoy_lifecycle_state()['revision']
    _module_project = ext._convoy_lifecycle_state.__globals__['project']
    _module_project.modified = ['/', '/project1/annotate1/body']
    assert ext._convoy_lifecycle_state()['revision'] == before


def test_a_new_save_moves_the_revision(tmp_path):
    ext, _s, _q = _envoy(tmp_path, ['/'])
    before = ext._convoy_lifecycle_state()['revision']
    (tmp_path / 'e1.7.toe').write_bytes(b'a different and longer file')
    assert ext._convoy_lifecycle_state()['revision'] != before


def _revision(ext):
    return ext._convoy_lifecycle_state()['revision']


def test_dirty_with_no_landed_save_refuses_the_quit(tmp_path):
    ext, scheduled, quits = _envoy(tmp_path, ['/'])
    answer = ext._convoy_lifecycle_quit(_revision(ext))
    assert answer['code'] == 'project_dirty'
    assert scheduled == [] and quits == []


def test_a_landed_save_authorizes_the_quit_through_to_the_commit(tmp_path):
    ext, scheduled, quits = _envoy(tmp_path, ['/'])
    revision = _revision(ext)
    assert ext._convoy_lifecycle_quit(revision, save_landed=True)['ok'] is True
    assert 'True' in scheduled[0], 'the delayed commit must carry it'
    assert ext._convoy_lifecycle_commit_quit(
        revision, False, True)['ok'] is True
    assert quits == [True]


def test_the_delayed_commit_still_refuses_dirty_without_a_landed_save(tmp_path):
    ext, _s, quits = _envoy(tmp_path, ['/'])
    answer = ext._convoy_lifecycle_commit_quit(_revision(ext), False)
    assert answer['code'] == 'project_dirty'
    assert quits == []


def test_a_landed_save_never_excuses_a_changed_file(tmp_path):
    """Someone saved again after the host's proof: refuse, do not quit."""
    ext, scheduled, quits = _envoy(tmp_path, ['/'])
    revision = _revision(ext)
    (tmp_path / 'e1.7.toe').write_bytes(b'saved again, and longer')
    answer = ext._convoy_lifecycle_quit(revision, save_landed=True)
    assert answer['code'] == 'dirty_revision_changed'
    assert scheduled == [] and quits == []


def test_a_landed_save_never_excuses_a_project_with_no_file(tmp_path):
    ext, scheduled, _q = _envoy(tmp_path, ['/'], resolved=None)
    answer = ext._convoy_lifecycle_quit(_revision(ext), save_landed=True)
    assert answer['code'] == 'project_dirty'
    assert scheduled == []


def test_save_landed_must_be_boolean(tmp_path):
    ext, _s, _q = _envoy(tmp_path, [])
    answer = ext._convoy_lifecycle_quit(_revision(ext), save_landed='yes')
    assert answer['code'] == 'invalid_arguments'
