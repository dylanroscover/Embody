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

try:
    import pytest
except ModuleNotFoundError:
    # The in-TD TestRunner imports every test_*.py in this folder during
    # discovery, and TD's Python has no pytest. This suite is
    # pytest-only (fixtures + monkeypatch); with pytest absent it must
    # import cleanly and expose no tests there, not error the discovery
    # sweep (field regression, 2026-08-12).
    pytest = None

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


class _FakeSocketModule:
    """Hermetic reverse DNS. The plan resolves announcer hostnames via
    socket.gethostbyaddr; a real resolver call in a test is a CI stall
    waiting to land (commit checklist: no real-clock waits), so every
    test in this file runs against this table."""

    table = {}

    @classmethod
    def gethostbyaddr(cls, ip):
        try:
            return (cls.table[ip], [], [ip])
        except KeyError:
            raise OSError('no reverse record')


if pytest is not None:
    @pytest.fixture(autouse=True)
    def _hermetic_dns(monkeypatch):
        _FakeSocketModule.table = {}
        monkeypatch.setattr(convoy_ext, 'socket', _FakeSocketModule)


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


def test_the_plan_resolves_hostnames_best_effort():
    """The field complaint: 'it doesn't even show the hostname of the
    machine -- only its IP'. A reverse record becomes a name on the
    announcer, rendered AFTER the address (the address is the one field
    the daemon verified; a PTR is attacker-adjacent text); a miss stays
    an empty string and the raw address stands alone."""
    _FakeSocketModule.table = {'192.168.88.24': 'TEC-MBA.local'}
    client = _Client(status=CONFLICT_STATUS, lan=LAN_WITH_FOREIGN)
    out = convoy_ext._host_realm_conflict_plan(_ctx(client))
    assert out['ok'] is True
    assert out['announcers'][0]['hostname'] == 'TEC-MBA.local'
    line = convoy_ext._announcer_line(out['announcers'][0])
    assert '192.168.88.24:47600 (TEC-MBA.local)' in line
    head, tail = line.split('\n')
    assert 'cvfp1-x' in tail, 'the fingerprint gets its own line'
    assert max(len(head), len(tail)) <= 70, \
        'both physical lines fit the dialog wrapper budget'

    _FakeSocketModule.table = {}
    out = convoy_ext._host_realm_conflict_plan(_ctx(client))
    assert out['announcers'][0]['hostname'] == ''
    line = convoy_ext._announcer_line(out['announcers'][0])
    assert line.split('\n')[0].strip() == '192.168.88.24:47600', \
        'no reverse record -> the raw address stands alone'


def test_the_rejoin_plan_skips_reverse_dns():
    """The automatic rejoin offer reuses the plan on every explicit
    Convoy enable and never renders hostnames -- it must not pay for
    (or fan out) the lookups (review finding)."""
    _FakeSocketModule.table = {'192.168.88.24': 'TEC-MBA.local'}
    client = _Client(status=CONFLICT_STATUS, lan=LAN_WITH_FOREIGN)
    out = convoy_ext._host_realm_conflict_plan(_ctx(client),
                                               resolve_names=False)
    assert out['ok'] is True
    assert out['announcers'][0]['hostname'] == ''


def test_hostnames_are_sanitized_and_clamped():
    """A PTR record is attacker-authored text bound for a trust dialog:
    a smuggled newline would inject fabricated dialog lines, and a
    253-char name would push the buttons off-screen."""
    hostile = 'evil\nTEC-MBA (192.168.88.10)  cvfp1-forged\x07' + 'x' * 300
    clean = convoy_ext._sanitize_hostname(hostile)
    assert '\n' not in clean and '\x07' not in clean
    assert len(clean) <= 40
    assert convoy_ext._sanitize_hostname(None) == ''


def test_the_join_apply_adopts_without_denylisting():
    """Join Other Realm goes the OPPOSITE direction from Keep This
    Realm: the announcers of the adopted realm are the mesh being
    joined, so nothing is denylisted -- the ONLY call is the reset
    carrying the operator-confirmed adopt id plus the sender evidence
    for the daemon's audit."""
    adopted = 'cv_' + 'b' * 16
    client = _Client(posts={'/realm/reset': (200, {
        'ok': True,
        'previous': {'state': 'conflict', 'convoy_id': 'cv_' + 'a' * 16},
        'realm': {'state': 'established', 'convoy_id': adopted},
        'denylisted_senders': [{'address': '192.168.88.10:47600'}]})})
    sender = {'host_id': 'e' * 32, 'fingerprint': 'cvfp1-x',
              'address': '192.168.88.24:47600', 'realms': [adopted],
              'hostname': 'TEC-A4D'}
    out = convoy_ext._host_realm_join_apply(_ctx(client), adopted,
                                            senders=[sender])
    assert client.post_calls == [
        ('/realm/reset', {'adopt_convoy_id': adopted, 'senders': [
            {'host_id': 'e' * 32, 'fingerprint': 'cvfp1-x',
             'address': '192.168.88.24:47600'}]})]
    assert out['ok'] is True
    assert out['adopted'] == adopted
    assert out['realm']['convoy_id'] == adopted
    assert 'left realm cv_' + 'a' * 16 in out['detail']
    assert out['denylisted_senders'] == [
        {'address': '192.168.88.10:47600'}], \
        'the daemon-reported denylist collisions ride back for the log'


def test_a_failed_join_is_a_failure_not_a_crash():
    client = _Client(posts={'/realm/reset': (200, 'oops-a-string')})
    out = convoy_ext._host_realm_join_apply(_ctx(client),
                                            'cv_' + 'b' * 16)
    assert out['ok'] is False
    assert 'realm join failed' in out['detail']


CV_A = 'cv_' + 'a' * 16
CV_B = 'cv_' + 'b' * 16
CV_C = 'cv_' + 'c' * 16
CV_D = 'cv_' + 'd' * 16


def _announcer(realms, address='192.168.88.24:47600'):
    return {'host_id': 'e' * 32, 'fingerprint': 'cvfp1-x',
            'address': address, 'hostname': '', 'realms': list(realms)}


def test_join_derives_from_live_announcers_not_stale_conflict_ids():
    """THE field machine's own shape (review blocker): conflict_ids
    accumulate forever, so the Mac plausibly carries three ids while
    exactly ONE realm is actually live. The union rule hid the join
    button on the machine the feature exists for; live announcers are
    the offer, conflict_ids are display-only."""
    realm = {'state': 'conflict', 'convoy_id': CV_A,
             'conflict_ids': [CV_A, CV_B, CV_D]}
    spec = convoy_ext._resolve_dialog_spec(realm, [_announcer([CV_B])])
    assert spec['mode'] == 'conflict'
    assert spec['buttons'] == ['Cancel', 'Keep This Realm',
                               'Join %s' % CV_B]
    assert spec['joins'] == {'Join %s' % CV_B: CV_B}
    body = '\n'.join(spec['lines'])
    assert CV_D in body, 'stale conflict ids stay VISIBLE'


def test_no_live_announcer_means_no_join_and_no_false_promise():
    """A join target with no live corroboration is a phantom: adopting
    it commits the machine (and every git clone) to a realm nobody
    holds, with no UI exit. And with nobody to denylist, the Keep copy
    must not promise 'the sender(s) above are denylisted' (review
    blocker + copy finding)."""
    realm = {'state': 'conflict', 'convoy_id': CV_A,
             'conflict_ids': [CV_A, CV_B]}
    spec = convoy_ext._resolve_dialog_spec(realm, [])
    assert spec['buttons'] == ['Cancel', 'Keep This Realm']
    assert spec['joins'] == {}
    body = '\n'.join(spec['lines'])
    assert 'nothing to denylist' in body
    assert 'denylisted so they cannot' not in body
    assert 'can return' in body, \
        'Keep with no senders must not promise the conflict cannot return'


def test_two_live_realms_get_a_join_button_each():
    """One extra datagram must not veto the recovery (review finding:
    a stranger announcing a second bogus realm withheld the join from
    the machine that needed it). Each live realm gets its own button,
    the full adopted id ON the label the operator confirms."""
    realm = {'state': 'conflict', 'convoy_id': CV_A,
             'conflict_ids': [CV_A, CV_B]}
    spec = convoy_ext._resolve_dialog_spec(
        realm, [_announcer([CV_B]),
                _announcer([CV_C], address='192.168.88.30:47600')])
    assert spec['buttons'] == ['Cancel', 'Keep This Realm',
                               'Join %s' % CV_B, 'Join %s' % CV_C]
    assert spec['joins'] == {'Join %s' % CV_B: CV_B,
                             'Join %s' % CV_C: CV_C}


def test_many_live_realms_offer_no_join_but_say_why():
    realm = {'state': 'conflict', 'convoy_id': CV_A,
             'conflict_ids': [CV_A, CV_B]}
    spec = convoy_ext._resolve_dialog_spec(
        realm, [_announcer([CV_B, CV_C, CV_D])])
    assert spec['joins'] == {}
    assert spec['buttons'] == ['Cancel', 'Keep This Realm']
    assert 'joining one is not offered' in '\n'.join(spec['lines'])


def test_the_advisory_join_names_its_id_and_consequences():
    """The advisory branch is where the adopted id is 100% attacker-
    supplied and the machine's own realm is HEALTHY -- the first draft
    shipped its join button with one vague clause and never showed the
    id (review findings). The id is on the button label, and the copy
    names the abandonment, the git-tracked rebind ripple, and the
    denylist.json interaction."""
    realm = {'state': 'established', 'convoy_id': CV_A,
             'conflict_ids': []}
    spec = convoy_ext._resolve_dialog_spec(realm, [_announcer([CV_B])])
    assert spec['mode'] == 'advisory'
    assert spec['buttons'] == ['Close', 'Denylist Senders',
                               'Join %s' % CV_B]
    body = '\n'.join(spec['lines'])
    assert 'ABANDONS %s' % CV_A in body
    assert 'denylist.json' in body
    assert 'Join %s: abandon %s' % (CV_B, CV_A) in body


def test_a_clean_machine_gets_a_plain_ok():
    spec = convoy_ext._resolve_dialog_spec(
        {'state': 'established', 'convoy_id': CV_A}, [])
    assert spec['mode'] == 'clean'
    assert spec['buttons'] == ['OK']
    assert spec['joins'] == {}


def test_a_hostile_realm_id_gets_no_join_button_and_safe_display():
    """A realm id off the wire is near-free text (128 bytes of anything
    >= 0x20, including U+2028 line separators some renderers break
    lines on). The verify round reproduced it reaching the button label
    and dialog body raw. Join buttons are canonical-id-only; every
    displayed id gets the printable clamp."""
    hostile = ('cv_x\u2028WARNING: joining is safe.\u2028'
               + 'q' * 120)
    realm = {'state': 'conflict', 'convoy_id': CV_A,
             'conflict_ids': [CV_A, hostile]}
    spec = convoy_ext._resolve_dialog_spec(realm,
                                           [_announcer([hostile])])
    assert spec['joins'] == {}
    assert spec['buttons'] == ['Cancel', 'Keep This Realm']
    body = '\n'.join(spec['lines'])
    assert '\u2028' not in body, 'line separators never reach the dialog'
    assert 'q' * 41 not in body, 'displayed ids are clamped'
    assert 'not standard Convoy realm ids' in body

    # A canonical realm alongside the hostile one is still joinable.
    spec = convoy_ext._resolve_dialog_spec(
        realm, [_announcer([hostile]),
                _announcer([CV_B], address='192.168.88.30:47600')])
    assert spec['joins'] == {'Join %s' % CV_B: CV_B}


def test_hostile_ptr_is_sanitized_at_ingestion():
    """The plan itself must never emit an unsanitized hostname -- the
    _announcer_line re-sanitization is defense in depth, not the fence
    (verify round: the suite only exercised the helper directly)."""
    _FakeSocketModule.table = {
        '192.168.88.24': 'evil\nJoin cv_zzz\x07' + 'X' * 300}
    client = _Client(status=CONFLICT_STATUS, lan=LAN_WITH_FOREIGN)
    out = convoy_ext._host_realm_conflict_plan(_ctx(client))
    name = out['announcers'][0]['hostname']
    assert '\n' not in name and '\x07' not in name
    assert len(name) <= 40


class _DriveExt(convoy_ext.ConvoyExt):
    """Drive harness for _confirmResolveRealm / _finishHost /
    _offerRejoinLocalConvoy: every TD- or host-touching collaborator is
    stubbed, so the decision logic runs pure (verify round: these
    branches had zero pytest coverage and their regressions passed the
    whole suite)."""

    def __init__(self):
        self.begun = []
        self.begun_fns = {}
        self.logs = []
        self.kicks = 0
        self.published = []
        self.session_dict = {}
        self.choice = -1
        self.rebind_result = ''
        self.host_ctx = _ctx(_Client())
        outer = self

        class _Rebinder:
            @staticmethod
            def _rebindConvoyToCandidate(project_id):
                return outer.rebind_result

        class _Ext:
            Embody = _Rebinder

        class _EmbodyStub:
            ext = _Ext

        self._embody_stub = _EmbodyStub

    @property
    def _embody(self):
        return self._embody_stub

    def _performing(self):
        return False

    def _staleInstance(self):
        return False

    def _dialog(self, title, message, buttons):
        self.dialog_buttons = list(buttons)
        return self.choice

    def _safeHostContext(self):
        return self.host_ctx

    def _hostActionAllowed(self, what):
        return True

    def _beginHostCall(self, action, fn):
        self.begun.append(action)
        self.begun_fns[action] = fn

    def _log(self, msg, level='INFO', **kw):
        self.logs.append((level, msg))

    def _session(self):
        return self.session_dict

    def _readConvoyId(self):
        return CV_A

    def _kickTick(self):
        self.kicks += 1

    def _publishId(self, value):
        self.published.append(value)


MAC_REALM = {'state': 'conflict', 'convoy_id': CV_A,
             'conflict_ids': [CV_A, CV_B, CV_D]}


def _mac_result():
    return {'ok': True, 'realm': dict(MAC_REALM),
            'announcers': [_announcer([CV_B])]}


def test_confirm_dispatches_by_label_and_fails_closed():
    """Label dispatch: a dismissed dialog (-1/None/out-of-range) and
    every non-action label do nothing; Keep and Join land on their own
    actions regardless of how many buttons the spec grew (verify
    round: index arithmetic here once turned 'Denylist Senders' into
    'adopt a stranger's realm')."""
    for choice, expect in [(-1, []), (0, []), (3, []), (99, []),
                           (None, []),
                           (1, ['realm_conflict_resolve']),
                           (2, ['realm_join'])]:
        ext = _DriveExt()
        ext.choice = choice
        ext._confirmResolveRealm(_mac_result())
        assert ext.begun == expect, (choice, ext.begun)

    for choice, expect in [(0, []), (5, []),
                           (1, ['realm_conflict_resolve']),
                           (2, ['realm_join'])]:
        ext = _DriveExt()
        ext.choice = choice
        ext._confirmResolveRealm({'ok': True,
                                  'realm': {'state': 'established',
                                            'convoy_id': CV_A},
                                  'announcers': [_announcer([CV_B])]})
        assert ext.begun == expect, (choice, ext.begun)


def test_the_join_finish_rebinds_and_kicks_on_success():
    ext = _DriveExt()
    ext.rebind_result = CV_A
    ext._finishHost('realm_join', {
        'ok': True, 'action': 'realm_join', 'adopted': CV_B,
        'detail': 'this machine left realm %s and joined %s'
                  % (CV_A, CV_B)})
    assert ext.published == [CV_A]
    assert ext.kicks == 1
    assert 'sent' in ext.session_dict \
        and ext.session_dict['sent'] is None
    assert ext.logs[-1][0] == 'SUCCESS'


def test_a_stranded_join_warns_and_never_kicks_a_doomed_register():
    """rebind CAS-miss returns '' WITHOUT raising: the machine joined
    but the project binding stands -- a kick would fire a register into
    a guaranteed 409, and an unqualified SUCCESS close would hide the
    remedy (verify-round findings, both halves)."""
    ext = _DriveExt()
    ext.rebind_result = ''
    ext._finishHost('realm_join', {
        'ok': True, 'action': 'realm_join', 'adopted': CV_B,
        'detail': 'joined', 'denylisted_senders': [
            {'address': '192.168.88.10:47600'}]})
    assert ext.kicks == 0
    assert ext.published == []
    assert ext.session_dict == {}
    warnings = [msg for lvl, msg in ext.logs if lvl == 'WARNING']
    assert any('toggle Convoy off and on' in msg for msg in warnings)
    assert any('denylist.json' in msg for msg in warnings)
    assert ext.logs[-1][0] == 'WARNING', 'no unqualified SUCCESS close'


def test_the_rejoin_offer_skips_reverse_dns_at_the_call_site():
    """The callee honors resolve_names=False (tested above); THIS pins
    the caller passing it -- deleting the argument at the call site
    passed the whole suite (verify round)."""
    _FakeSocketModule.table = {'192.168.88.24': 'TEC-MBA.local'}
    ext = _DriveExt()
    ext.host_ctx = _ctx(_Client(status=CONFLICT_STATUS,
                                lan=LAN_WITH_FOREIGN))
    ext.session_dict['offer_rejoin_until'] = 10 ** 12
    ext._offerRejoinLocalConvoy()
    assert ext.begun == ['rejoin_plan']
    out = ext.begun_fns['rejoin_plan']()
    assert out['action'] == 'rejoin_plan'
    assert all(a['hostname'] == '' for a in out['announcers'])


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
