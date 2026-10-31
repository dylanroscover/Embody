"""The Convoy Machines page and project auto-join, on the CI matrix.

ConvoyExt imports without TouchDesigner, and these paths take plain data, so
the page's contract runs beside the daemon that feeds it. Writing the
parameters themselves stays in the in-TD suite (test_convoy_ext.py).
"""

import importlib.util
import json
import os

_EXT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'Embody', 'convoy', 'ConvoyExt.py')


def _module():
    spec = importlib.util.spec_from_file_location(
        'convoy_ext_machine_rows', _EXT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _rows(machines=None, nodes=()):
    result = {'state': 'nodes', 'nodes': list(nodes)}
    if machines is not None:
        result['machines'] = machines
    return _module().ConvoyExt._machineRows(result)


def _machine(**fields):
    row = {'hostname': 'TEC-B4A', 'ip': '192.168.88.20', 'online': True,
           'status': 'online', 'last_seen_age_s': 5.0, 'toes': []}
    row.update(fields)
    return row


def test_a_machine_reads_hostname_slash_ip_with_its_open_toes():
    row = _rows([_machine(toes=['Project.1.toe', 'Moonshine.toe', 'e1.toe'])])[0]
    assert row['Machinename'] == 'TEC-B4A / 192.168.88.20'
    assert row['Toefiles'] == 'Project.1.toe, Moonshine.toe, e1.toe'
    assert row['Machinestatus'] == 'Online / 5s ago'


def test_a_machine_with_nothing_open_is_still_online():
    row = _rows([_machine(last_seen_age_s=0.0)])[0]
    assert row['Toefiles'] == 'None open'
    assert row['Machinestatus'] == 'Online / Now'


def test_an_offline_machine_says_how_long_and_why():
    row = _rows([_machine(online=False, status='offline', toes=[],
                          last_seen_age_s=7200.0,
                          reason='peer_unreachable')])[0]
    assert row['Machinestatus'] == 'Offline / 2h ago -- peer unreachable'


def test_limited_compatibility_names_the_reason():
    """'limited' alone sent an operator to read the other machine's log for
    the version (2026-09-21)."""
    row = _rows([_machine(
        compatibility='limited',
        compatibility_reason='12 of 19 operations shared with host app 6.0.280',
    )])[0]
    assert row['Machinestatus'] == ('Online / 5s ago -- limited: 12 of 19 '
                                    'operations shared with host app 6.0.280')


def test_a_machine_with_no_name_falls_back_to_its_address():
    assert _rows([_machine(hostname='')])[0]['Machinename'] == '192.168.88.20'


def test_an_older_host_is_grouped_into_one_row_per_machine():
    node = {'host_id': 'a' * 32, 'hostname': 'TEC-B4A', 'ip': '10.0.0.2',
            'status': 'online', 'online': True, 'last_seen_age_s': 3.0}
    rows = _rows(nodes=[dict(node, toe_name='e1.toe'),
                        dict(node, toe_name='moonshine.toe'),
                        dict(node, toe_name='closed.toe', online=False,
                             status='offline')])
    assert len(rows) == 1, 'one computer, one row'
    assert rows[0]['Toefiles'] == 'e1.toe, moonshine.toe'
    assert rows[0]['Machinestatus'] == 'Online / 3s ago'


def test_a_failed_directory_read_means_leave_existing_rows():
    rows = _module().ConvoyExt._machineRows
    for value in (None, {}, {'state': 'unreachable'}, {'state': 'host_error'}):
        assert rows(value) is None


def test_rows_are_bounded():
    row = _rows([_machine(hostname='x' * 2000, ip='1' * 1000,
                          toes=['t' * 2000])])[0]
    assert len(row['Machinename']) <= 512
    assert len(row['Toefiles']) <= 1024


# -- the node name follows a version-up ---------------------------------

def test_this_projects_own_stamp_at_any_version_is_not_an_override():
    stamp = _module().ConvoyExt._isOwnNameStamp
    assert stamp('TEC-B4A / e1.5', 'TEC-B4A', 'e1.6')
    assert stamp('TEC-B4A / e1', 'TEC-B4A', 'e1.6')
    assert not stamp('Lobby wall', 'TEC-B4A', 'e1.6')
    assert not stamp('TEC-B4A / lobby', 'TEC-B4A', 'e1.6')
    assert not stamp('OTHER / e1.5', 'TEC-B4A', 'e1.6')


# -- a project joins by itself on a machine that has already said yes ----

class _Client:
    def __init__(self, live=True, joined=True):
        self.live = live
        self.joined = joined

    def data_dir(self):
        return '/tmp/nowhere'

    def machine_joined(self, data_dir):
        return self.joined

    def read_live_portfile(self, data_dir):
        return {'port': 1} if self.live else None


class _Node:
    """Stands in for a COMP: attribute access is all the check does."""

    def __init__(self, **attrs):
        self.__dict__.update(attrs)


def _convoy(consent=True, saved=True, performing=False, live=True,
            declined=False, joined=True, settled=True, convoy_id='',
            scope=''):
    mod = _module()
    embody = _Node(_convoyDeclinedHere=lambda: declined,
                   _convoyMayAutoJoinNow=lambda: settled)
    convoy = object.__new__(mod.ConvoyExt)
    session = {}
    convoy.__dict__.update(
        _session=lambda: session,
        ownerComp=_Node(parent=_Node(Embody=_Node(ext=_Node(Embody=embody)))),
        _performing=lambda: performing,
        _savedToe=lambda: 'C:/w/e1.toe' if saved else None,
        _installConsentGiven=lambda: consent,
        _safeClient=lambda: _Client(live, joined),
        _readConvoyId=lambda: convoy_id,
        _readConsentScope=lambda: scope,
        _log=lambda *_a, **_k: None,
    )
    return convoy


def test_a_saved_project_joins_a_machine_that_consented():
    assert _convoy()._mayJoinMachineConvoy() is True


def test_no_consent_on_this_machine_never_joins():
    assert _convoy(consent=False)._mayJoinMachineConvoy() is False


def test_an_unsaved_project_never_joins():
    assert _convoy(saved=False)._mayJoinMachineConvoy() is False


def test_a_show_in_perform_mode_is_left_alone():
    assert _convoy(performing=True)._mayJoinMachineConvoy() is False


def test_a_stopped_host_app_stays_stopped():
    """Stop Convoy App takes the machine out: opening a project must not
    bring it back."""
    assert _convoy(live=False)._mayJoinMachineConvoy() is False


def test_a_machine_that_never_joined_does_not_pull_a_project_in():
    """An old consent.json alone is no yes: all of a machine's projects may
    have been turned off to close its LAN."""
    assert _convoy(joined=False)._mayJoinMachineConvoy() is False


def test_an_unsettled_session_waits_without_spending_its_attempt():
    convoy = _convoy(settled=False)
    assert convoy._mayJoinMachineConvoy() is False
    assert 'auto_join_tried' not in convoy._session()


def test_an_older_loopback_scope_is_not_widened_from_a_tick():
    convoy = _convoy(convoy_id='cv_1', scope='local host app only')
    assert convoy._mayJoinMachineConvoy() is False


def test_one_attempt_per_run():
    """A join that failed, or a toggle turned back off, stays that way."""
    convoy = _convoy()
    convoy._session()['auto_join_tried'] = True
    assert convoy._mayJoinMachineConvoy() is False


def test_a_project_someone_said_no_for_stays_out():
    assert _convoy(declined=True)._mayJoinMachineConvoy() is False


class _Join:
    """A real ConvoyExt with its TD-facing edges recorded."""

    def __init__(self, consent_ok=True):
        self.calls = []
        mod = _module()
        self.convoy = object.__new__(mod.ConvoyExt)
        session = {}
        self.session = session
        self.convoy.__dict__.update(
            _session=lambda: session,
            _setEnabled=lambda value: self.calls.append(('enabled', value)),
            _ensureConsent=lambda: self.calls.append('consent') or consent_ok,
            _ensureWakeListener=lambda: self.calls.append('wake'),
            _reconcile=lambda force=False: self.calls.append('reconcile'),
            _ensureEnvoy=lambda: self.calls.append('ENVOY'),
            _ensureHostApp=lambda: self.calls.append('HOST APP'),
            armRejoinOffer=lambda: self.calls.append('rejoin offer'),
            register=lambda: self.calls.append('register') or {},
            _log=lambda *_a, **_k: None,
        )


def test_an_auto_join_registers_and_touches_nothing_else():
    """No Enable Envoy, no virtual environment, no host-app start."""
    join = _Join()
    join.convoy._joinMachineConvoy()
    assert join.calls == [('enabled', True), 'consent', 'wake', 'reconcile']
    assert join.session['auto_join_tried'] is True


def test_the_toggle_callback_of_an_auto_join_raises_nothing():
    """TD delivers it deferred, and it must not arm the rejoin dialog or run
    the explicit-enable path."""
    join = _Join()
    join.convoy._joinMachineConvoy()
    del join.calls[:]
    join.convoy.enableToggledOn()
    assert join.calls == []


def test_a_real_toggle_after_it_takes_the_explicit_path():
    join = _Join()
    join.convoy._joinMachineConvoy()
    join.convoy.enableToggledOn()
    del join.calls[:]
    join.convoy.enableToggledOn()
    assert join.calls == ['rejoin offer', 'register']


def test_a_toggle_with_no_auto_join_takes_the_explicit_path():
    join = _Join()
    join.convoy.enableToggledOn()
    assert join.calls == ['rejoin offer', 'register']


# -- saying no ---------------------------------------------------------

_ADMIN_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'Embody', 'embody_admin.py')


def _admin():
    spec = importlib.util.spec_from_file_location(
        'embody_admin_declined', _ADMIN_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _declined(monkeypatch, tmp_path, entry, saved=None):
    admin = _admin()
    config = tmp_path / 'config.json'
    if saved is not None:
        config.write_text(json.dumps(
            {'params': {'Convoyenable': {'val': saved}}}), encoding='utf-8')
    monkeypatch.setattr(admin, 'read_convoy_entry', lambda ext: entry)
    monkeypatch.setattr(admin, 'settings_path', lambda ext: config)
    return admin.convoy_declined_here(None)


ID = 'cv_0123456789abcdef'


def test_a_project_that_never_joined_has_not_declined(monkeypatch, tmp_path):
    assert _declined(monkeypatch, tmp_path, {}, saved=False) is False


def test_an_explicit_no_is_a_no(monkeypatch, tmp_path):
    assert _declined(monkeypatch, tmp_path, {'declined': True}) is True


def test_joined_then_turned_off_is_a_no(monkeypatch, tmp_path):
    assert _declined(monkeypatch, tmp_path, {'id': ID}, saved=False) is True


def test_joined_and_on_is_not_a_no(monkeypatch, tmp_path):
    assert _declined(monkeypatch, tmp_path, {'id': ID}, saved=True) is False


def test_a_fresh_clone_with_no_local_config_joins(monkeypatch, tmp_path):
    assert _declined(monkeypatch, tmp_path, {'id': ID}) is False


def test_a_later_explicit_enable_outranks_an_old_no(monkeypatch, tmp_path):
    entry = {'id': ID, 'declined': True}
    assert _declined(monkeypatch, tmp_path, entry, saved=True) is False


def test_recording_a_no_keeps_every_other_key(monkeypatch):
    admin = _admin()
    written = {}
    monkeypatch.setattr(admin, '_load_project_json',
                        lambda ext, purpose: ({'other': 1}, True))
    monkeypatch.setattr(admin, 'project_json_path', lambda ext: 'p')
    monkeypatch.setattr(admin, '_save_project_json',
                        lambda path, data: written.update(data) or True)
    assert admin.record_convoy_declined(None) is True
    assert written == {'other': 1, 'convoy': {'declined': True}}


def test_recording_a_no_never_overwrites_an_unreadable_file(monkeypatch):
    admin = _admin()
    written = []
    monkeypatch.setattr(admin, '_load_project_json',
                        lambda ext, purpose: ({}, False))
    monkeypatch.setattr(admin, '_save_project_json',
                        lambda path, data: written.append(data))
    assert admin.record_convoy_declined(None) is False
    assert written == []


# -- forgetting a machine ------------------------------------------------

class _Probe:
    use_convoy = True
    handle = object()
    status = 'ok'


class _ForgetClient:
    def __init__(self, machines):
        self.machines = machines
        self.posted = []

    def probe(self, data_dir=None):
        return _Probe()

    def host_get(self, handle, path):
        return 200, {'host_id': 'h' * 32, 'nodes': [],
                     'machines': self.machines}

    def host_post(self, handle, path, body):
        self.posted.append((path, body))
        return 200, {'ok': True}


def _peer(host_id, online):
    return {'host_id': host_id, 'hostname': 'OLD-BOX', 'ip': '10.0.0.9',
            'online': online, 'status': 'online' if online else 'offline',
            'local': False}


def test_only_offline_remote_machines_are_offered_for_forgetting():
    """Refused is not offline: forgetting a blocked peer drops its block and
    re-opens first contact."""
    mod = _module()
    client = _ForgetClient([
        _peer('a' * 32, False), _peer('b' * 32, True),
        dict(_peer('c' * 32, False), local=True),
        dict(_peer('d' * 32, False), status='error', reason='peer_blocked')])
    plan = mod._host_forget_offline_plan({'client': client,
                                          'data_dir': '/tmp/nowhere'})
    assert plan['ok'] is True
    assert plan['machines'] == [{'host_id': 'a' * 32,
                                 'label': 'OLD-BOX / 10.0.0.9'}]


def test_a_machine_back_online_before_the_confirm_is_skipped():
    mod = _module()
    client = _ForgetClient([_peer('a' * 32, False), _peer('b' * 32, True)])
    result = mod._host_forget_offline_apply(
        {'client': client, 'data_dir': '/tmp/nowhere'}, [],
        ['a' * 32, 'b' * 32])
    assert client.posted == [('/peers/forget', {'host_id': 'a' * 32})]
    assert result['forgotten'] == ['a' * 32]
    assert result['skipped'] == ['b' * 32]
