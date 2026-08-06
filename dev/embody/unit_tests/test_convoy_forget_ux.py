"""The Forget Offline Nodes daemon -> panel contract, on the CI matrix.

WHY THIS FILE EXISTS. The half of "Forget Offline Nodes" that failed in
the field on 2026-08-06 is the reporting half: the daemon refused every
row, the panel threw away the explanation it was handed, logged the run
as a SUCCESS reading "forgot 0", and the rows the click had removed came
back seconds later with nothing on screen. The in-TD suite that covers
the panel (test_convoy_host_install.py) is NOT in pytest.ini testpaths,
so none of it has ever run on either CI leg.

_host_forget_offline_apply is module level and touches no TD object --
it takes a context and a client -- so the contract belongs on the same
windows+macos matrix as the daemon it talks to. The dialog and sequence
helpers around it stay in the in-TD suite, where a real panel exists.
"""

import importlib.util
import os

_EXT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'Embody', 'convoy', 'ConvoyExt.py')

_HOST_ID = 'h' * 32


def _module():
    spec = importlib.util.spec_from_file_location(
        'convoy_ext_forget_ux', _EXT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Probe:
    use_convoy = True
    handle = object()
    status = 'ok'


class _Client:
    """The two daemon calls the apply path makes, and nothing else."""

    def __init__(self, nodes, responses):
        self._nodes = nodes
        self._responses = responses
        self.posted = []

    def probe(self, data_dir=None):
        return _Probe()

    def host_get(self, handle, path):
        assert path == '/network/nodes'
        return 200, {'host_id': _HOST_ID, 'nodes': self._nodes}

    def host_post(self, handle, path, body):
        assert path == '/nodes/forget'
        self.posted.append(body['node_id'])
        return self._responses[body['node_id']]


def _row(node_id, name):
    return {'node_id': node_id, 'node_name': name, 'toe_name': name + '.toe',
            'host_id': _HOST_ID, 'online': False, 'last_seen_age_s': 900}


def _ctx(nodes, responses):
    return {'client': _Client(nodes, responses), 'data_dir': '/tmp/nowhere'}


def _refusal(count, delivery_ids):
    return 409, {
        'ok': False, 'reason': 'node_has_work',
        'detail': 'this node still has %d delivery(s) that have not '
                  'finished (queued); cancel a queued one or wait for the '
                  'node to answer' % count,
        'pending_count': count,
        'blocking': [{'delivery_id': d, 'state': 'queued',
                      'operation': 'query_network'} for d in delivery_ids],
    }


def test_a_refusal_keeps_the_daemons_explanation_and_delivery_ids():
    """The panel must carry back WHY a row was kept and WHICH deliveries
    pin it. Previously only the node id survived, so the reason existed
    on the wire and nowhere else."""
    mod = _module()
    nodes = [_row('n1', 'alpha')]
    ctx = _ctx(nodes, {'n1': _refusal(2, ['cj_aaa', 'cj_bbb'])})

    result = mod._host_forget_offline_apply(ctx, ['n1'])

    assert result['forgotten'] == []
    kept = result['kept_busy']
    assert len(kept) == 1
    assert kept[0]['node_id'] == 'n1'
    assert kept[0]['name'] == 'alpha'
    assert kept[0]['pending_count'] == 2
    assert [b['delivery_id'] for b in kept[0]['blocking']] == ['cj_aaa',
                                                              'cj_bbb']
    assert 'have not finished' in kept[0]['detail']


def test_the_summary_still_counts_kept_rows():
    """The summary phrasing is load-bearing elsewhere (the parameter help
    and the in-TD assertion both read 'unresolved jobs'), and len() must
    keep working now that kept_busy holds dicts rather than ids."""
    mod = _module()
    nodes = [_row('n1', 'alpha'), _row('n2', 'beta')]
    ctx = _ctx(nodes, {'n1': _refusal(1, ['cj_aaa']),
                       'n2': _refusal(1, ['cj_bbb'])})

    result = mod._host_forget_offline_apply(ctx, ['n1', 'n2'])

    assert 'forgot 0' in result['detail']
    assert 'kept 2 with unresolved jobs' in result['detail']


def test_a_mixed_run_reports_both_halves():
    mod = _module()
    nodes = [_row('n1', 'alpha'), _row('n2', 'beta')]
    ctx = _ctx(nodes, {'n1': (200, {'ok': True, 'forgotten': True}),
                       'n2': _refusal(1, ['cj_bbb'])})

    result = mod._host_forget_offline_apply(ctx, ['n1', 'n2'])

    assert result['forgotten'] == ['n1']
    assert [k['node_id'] for k in result['kept_busy']] == ['n2']
    assert 'forgot 1' in result['detail']
    assert 'kept 1' in result['detail']


def test_a_row_that_came_back_online_is_skipped_never_forgotten():
    """The dialog and the apply are two round trips apart; a node that
    re-registered in between must not be deleted underneath its own live
    session."""
    mod = _module()
    online = _row('n1', 'alpha')
    online['online'] = True
    ctx = _ctx([online], {})

    result = mod._host_forget_offline_apply(ctx, ['n1'])

    assert result['skipped'] == ['n1']
    assert result['forgotten'] == []
    assert ctx['client'].posted == [], 'no forget may be sent for a live row'


def test_an_all_kept_run_is_not_a_failure_but_forgets_nothing():
    """`ok` stays True (nothing errored) -- which is exactly why the log
    level must be graded on `forgotten`/`kept_busy` rather than on `ok`.
    This pins the shape the panel grades."""
    mod = _module()
    nodes = [_row('n1', 'alpha')]
    ctx = _ctx(nodes, {'n1': _refusal(1, ['cj_aaa'])})

    result = mod._host_forget_offline_apply(ctx, ['n1'])

    assert result['ok'] is True
    assert result['failed'] == []
    assert result['forgotten'] == []
    assert result['kept_busy'], (
        'an all-kept run must be distinguishable from a clean sweep by '
        'its payload alone -- the field logged this case as SUCCESS')
