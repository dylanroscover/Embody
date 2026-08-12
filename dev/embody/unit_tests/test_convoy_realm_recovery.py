"""The TD-side realm-conflict recovery: worker bodies + the rejoin rebind.

The adversarial panel's finding: the entire ResolveRealmConflict surface
shipped with zero tests while driving two destructive host operations.
These run under plain pytest -- the worker bodies are module-level
functions that touch no TD object BY CONTRACT (same load-off-disk
pattern as test_convoy_host_ladder), and the rejoin rebind is a pure
file operation taking the ext instance as an argument.
"""

import importlib.util
import json
import os
import pathlib
import sys

# unit_tests/ -> embody/ -> dev/ -> the repo root.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
_EMBODY_DIR = os.path.join(_REPO_ROOT, 'dev', 'embody', 'Embody')


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sys.modules[name] = module
    return module


convoy_ext = _load('convoy_ext_for_recovery',
                   os.path.join(_EMBODY_DIR, 'convoy', 'ConvoyExt.py'))
admin = _load('embody_admin_for_recovery',
              os.path.join(_EMBODY_DIR, 'embody_admin.py'))


class _Probe:
    use_convoy = True
    status = 'running'
    handle = object()


class _Client:
    """Scripted loopback client: records calls, spawns nothing."""

    def __init__(self, status=None, lan=None, posts=None):
        self._status = status if status is not None else {}
        self._lan = lan if lan is not None else {}
        self._posts = posts or {}
        self.post_calls = []

    def probe(self, data_dir=None):
        return _Probe()

    def host_get(self, handle, path):
        if path == '/status':
            return 200, self._status
        if path == '/lan/status':
            return 200, self._lan
        return 404, {'ok': False}

    def host_post(self, handle, path, body):
        self.post_calls.append((path, body))
        return self._posts.get(path, (200, {'ok': True}))


def _ctx(client):
    return {'client': client, 'data_dir': 'X:/nowhere'}


CONFLICT_STATUS = {'realm': {'state': 'conflict',
                             'convoy_id': 'cv_' + 'a' * 16,
                             'conflict_ids': ['cv_' + 'a' * 16,
                                              'cv_' + 'b' * 16]}}
LAN_WITH_FOREIGN = {'discovery': {'candidates': [
    {'host_id': 'e' * 32, 'fingerprint': 'cvfp1-x',
     'address': '192.168.88.24:47600',
     'realm_states': {'cv_' + 'b' * 16: 'established'}},
    {'host_id': 'f' * 32, 'fingerprint': 'cvfp1-y',
     'address': '192.168.88.30:47600',
     'realm_states': {'cv_' + 'a' * 16: 'established'}},
]}}


def test_the_plan_names_only_FOREIGN_established_announcers():
    client = _Client(status=CONFLICT_STATUS, lan=LAN_WITH_FOREIGN)
    out = convoy_ext._host_realm_conflict_plan(_ctx(client))
    assert out['ok'] is True
    assert out['realm']['state'] == 'conflict'
    assert len(out['announcers']) == 1, \
        'a sender of OUR OWN realm is not an offender'
    assert out['announcers'][0]['address'] == '192.168.88.24:47600'
    assert out['announcers'][0]['realms'] == ['cv_' + 'b' * 16]


def test_the_apply_denylists_BEFORE_resetting():
    client = _Client()
    offenders = [{'host_id': 'e' * 32, 'fingerprint': 'cvfp1-x'}]
    out = convoy_ext._host_realm_conflict_apply(_ctx(client), offenders)
    paths = [path for path, _body in client.post_calls]
    assert paths == ['/peers/denylist', '/realm/reset'], \
        'silence the sender FIRST or the next datagram re-derives it'
    assert out['ok'] is True
    assert out['blocked'] == ['e' * 32]


def test_the_denylist_only_mode_never_resets():
    client = _Client()
    out = convoy_ext._host_realm_conflict_apply(
        _ctx(client), [{'host_id': 'e' * 32, 'fingerprint': ''}],
        reset=False)
    paths = [path for path, _body in client.post_calls]
    assert paths == ['/peers/denylist']
    assert out['ok'] is True and out['realm'] is None


def test_a_non_dict_reset_body_is_a_failure_not_a_crash():
    client = _Client(posts={'/realm/reset': (200, 'oops-a-string')})
    out = convoy_ext._host_realm_conflict_apply(_ctx(client), [])
    assert out['ok'] is False
    assert 'realm reset failed' in out['detail']


class _Ext:
    """The minimum of an ext the admin function touches."""

    def __init__(self, root):
        self._root = root
        self.logged = []

    def Log(self, msg, level='INFO', *a):
        self.logged.append((level, msg))

    def _findProjectRoot(self):
        return pathlib.Path(self._root)


def _write_project_json(root, entry):
    path = os.path.join(root, '.embody')
    os.makedirs(path, exist_ok=True)
    with open(os.path.join(path, 'project.json'), 'w',
              encoding='utf-8') as f:
        json.dump({'convoy': entry}, f)


def _read_entry(root):
    with open(os.path.join(root, '.embody', 'project.json'),
              encoding='utf-8') as f:
        return json.load(f)['convoy']


def test_rejoin_demotes_established_to_candidate_and_keeps_the_id(
        tmp_path):
    root = str(tmp_path)
    _write_project_json(root, {
        'id': 'cv_' + 'c' * 16, 'binding_state': 'established',
        'consent_scope': 'trusted LAN Convoy mesh',
        'granted_at': '2026-08-03T10:11:50Z',
        'bound_at': '2026-08-03T11:17:31Z'})
    ext = _Ext(root)
    out = admin.rebind_convoy_to_candidate(ext, 'cv_' + 'c' * 16)
    assert out == 'cv_' + 'c' * 16
    entry = _read_entry(root)
    assert entry['binding_state'] == 'candidate'
    assert entry['id'] == 'cv_' + 'c' * 16, 'the id is KEPT'
    assert 'bound_at' not in entry, 'the stamp described the old binding'
    assert entry['consent_scope'] == 'trusted LAN Convoy mesh', \
        'consent survives the rejoin'


def test_rejoin_refuses_a_stale_expected_id(tmp_path):
    root = str(tmp_path)
    _write_project_json(root, {'id': 'cv_' + 'c' * 16,
                               'binding_state': 'established'})
    ext = _Ext(root)
    assert admin.rebind_convoy_to_candidate(ext, 'cv_' + 'd' * 16) == ''
    assert _read_entry(root)['binding_state'] == 'established', \
        'a CAS miss must change nothing'
