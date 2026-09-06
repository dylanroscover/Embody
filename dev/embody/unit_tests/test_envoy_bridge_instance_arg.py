"""
Test suite: per-call instance addressing in the Envoy STDIO bridge.

Every Envoy tool advertises an optional `instance` argument. The bridge
strips it and routes THAT ONE CALL to the named registered instance's
Envoy; this session's pin is untouched, and a failure to reach the named
instance is that call's error alone (the pinned connection's state never
flips). Pure Python, no TD -- runs under the pytest tier and in TD.
"""

import importlib.util
import io
import json
import os
import sys
from unittest.mock import patch, MagicMock

_bridge_path = os.path.join(project.folder, 'embody', 'envoy_bridge.py')
_spec = importlib.util.spec_from_file_location('envoy_bridge_instance', _bridge_path)
bridge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bridge)
bridge.start_orphan_watchdog = lambda *args, **kwargs: None
if hasattr(bridge, 'start_reconciler'):
    bridge.start_reconciler = lambda *args, **kwargs: None

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase

REGISTRY = {
    'active': 'Dev',
    'instances': {
        'Dev': {'port': 9870, 'td_pid': None, 'toe_path': 'dev/Dev.toe'},
        'Show': {'port': 9871, 'td_pid': None, 'toe_path': 'show/Show.toe'},
    },
}


class TestInstanceArgumentSchema(EmbodyTestCase):

    def test_adds_optional_instance_to_envoy_tools_only(self):
        tools = [
            {'name': 'create_op', 'inputSchema': {'type': 'object', 'properties': {'op_type': {'type': 'string'}}}},
            {'name': 'get_td_status', 'inputSchema': {'type': 'object', 'properties': {}}},
            {'name': 'convoy_ping', 'inputSchema': {'type': 'object', 'properties': {}}},
        ]
        bridge.add_instance_argument(tools)
        self.assertIn('instance', tools[0]['inputSchema']['properties'])
        self.assertEqual(tools[0]['inputSchema']['properties']['instance']['type'], 'string')
        self.assertNotIn('instance', tools[1]['inputSchema']['properties'])
        self.assertNotIn('instance', tools[2]['inputSchema']['properties'])
        # Never required.
        self.assertNotIn('instance', tools[0]['inputSchema'].get('required', []))

    def test_idempotent_and_tolerant_of_odd_shapes(self):
        tools = [{'name': 'get_op'}, {'name': 'weird', 'inputSchema': 'nope'}, 'garbage', None]
        bridge.add_instance_argument(tools)
        bridge.add_instance_argument(tools)
        self.assertIn('instance', tools[0]['inputSchema']['properties'])
        self.assertIn('instance', tools[1]['inputSchema']['properties'])

    def test_augment_tools_list_advertises_it(self):
        response = {'jsonrpc': '2.0', 'id': 1,
                    'result': {'tools': [{'name': 'get_op', 'inputSchema': {'type': 'object', 'properties': {}}}]}}
        bridge.augment_tools_list(response)
        tools = {t['name']: t for t in response['result']['tools']}
        self.assertIn('instance', tools['get_op']['inputSchema']['properties'])
        self.assertIn('get_td_status', tools)
        self.assertNotIn('instance', tools['get_td_status']['inputSchema']['properties'])


class TestInstanceResolution(EmbodyTestCase):

    def test_resolves_registered_instance(self):
        url, reason, available = bridge.resolve_instance_url(REGISTRY, 'Show')
        self.assertEqual(url, 'http://127.0.0.1:9871/mcp')
        self.assertIsNone(reason)
        self.assertEqual(available, ['Dev', 'Show'])

    def test_unknown_and_malformed(self):
        self.assertEqual(bridge.resolve_instance_url(REGISTRY, 'Nope')[1], 'unknown')
        self.assertEqual(bridge.resolve_instance_url(REGISTRY, None)[1], 'unknown')
        self.assertEqual(bridge.resolve_instance_url({}, 'Show')[1], 'unknown')
        broken = {'instances': {'X': {'port': 'not-a-port'}}}
        self.assertEqual(bridge.resolve_instance_url(broken, 'X')[1], 'no_port')

    def test_error_result_shape(self):
        err = bridge.instance_error_result('Nope', 'unknown', 'detail here', ['Dev'])
        self.assertTrue(err['isError'])
        payload = json.loads(err['content'][0]['text'])
        self.assertEqual(payload['error_code'], 'envoy.instance.unknown')
        self.assertEqual(payload['instance'], 'Nope')
        self.assertEqual(payload['available'], ['Dev'])
        self.assertFalse(payload['ok'])


class TestInstanceRouting(EmbodyTestCase):
    """The main loop, with mocked I/O, network and registry."""

    def _run(self, messages, forward):
        stdin = io.StringIO('\n'.join(json.dumps(m) for m in messages) + '\n')
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch.object(sys, 'stdin', stdin), \
             patch.object(sys, 'stdout', stdout), \
             patch.object(sys, 'stderr', stderr), \
             patch.object(sys, 'argv', ['envoy_bridge.py']), \
             patch.object(bridge, 'wait_for_envoy', return_value=True), \
             patch.object(bridge, 'forward_to_http', forward), \
             patch.object(bridge, 'load_config', return_value=REGISTRY), \
             patch.object(bridge, 'find_td_pid', return_value=None), \
             patch.object(bridge, 'kill_stale_bridges'), \
             patch('time.sleep'):
            bridge.main()
        lines = [l for l in stdout.getvalue().strip().split('\n') if l.strip()]
        return [json.loads(l) for l in lines]

    def _call(self, request_id, arguments):
        return {'jsonrpc': '2.0', 'id': request_id, 'method': 'tools/call',
                'params': {'name': 'get_op', 'arguments': arguments}}

    def test_instance_routes_one_call_and_strips_the_argument(self):
        seen = []

        def forward(url, msg, **kw):
            seen.append((url, json.loads(json.dumps(msg))))
            return {'jsonrpc': '2.0', 'id': msg.get('id'), 'result': {'content': [], 'url': url}}

        responses = self._run([
            self._call(1, {'op_path': '/a', 'instance': 'Show'}),
            self._call(2, {'op_path': '/b'}),
        ], forward)
        urls = [u for u, _ in seen]
        self.assertIn('http://127.0.0.1:9871/mcp', urls, 'the addressed call goes to Show')
        routed = [m for u, m in seen if u.endswith(':9871/mcp')][0]
        self.assertNotIn('instance', routed['params']['arguments'],
                         'Envoy declares no such parameter -- it must be stripped')
        # The next, unaddressed call still goes to the pinned instance.
        pinned = [u for u, m in seen if m.get('id') == 2][0]
        self.assertTrue(pinned.endswith(':9870/mcp'))
        self.assertEqual([r['id'] for r in responses], [1, 2])

    def test_naming_the_pinned_instance_falls_through(self):
        seen = []

        def forward(url, msg, **kw):
            seen.append((url, msg))
            return {'jsonrpc': '2.0', 'id': msg.get('id'), 'result': {}}

        self._run([self._call(1, {'op_path': '/a', 'instance': 'Dev'})], forward)
        self.assertLen(seen, 1)
        self.assertTrue(seen[0][0].endswith(':9870/mcp'))
        self.assertNotIn('instance', seen[0][1]['params']['arguments'])

    def test_unknown_instance_fails_that_call_only(self):
        seen = []

        def forward(url, msg, **kw):
            seen.append(url)
            return {'jsonrpc': '2.0', 'id': msg.get('id'), 'result': {}}

        responses = self._run([
            self._call(1, {'op_path': '/a', 'instance': 'Nope'}),
            self._call(2, {'op_path': '/b'}),
        ], forward)
        first = responses[0]['result']
        self.assertTrue(first['isError'])
        payload = json.loads(first['content'][0]['text'])
        self.assertEqual(payload['error_code'], 'envoy.instance.unknown')
        self.assertEqual(payload['available'], ['Dev', 'Show'])
        # Nothing was forwarded for the bad name; the next call went out normally.
        self.assertLen(seen, 1)
        self.assertEqual(responses[1]['id'], 2)

    def test_unreachable_instance_does_not_flip_the_pinned_connection(self):
        calls = []

        def forward(url, msg, **kw):
            calls.append(url)
            if url.endswith(':9871/mcp'):
                raise OSError('Connection refused')
            return {'jsonrpc': '2.0', 'id': msg.get('id'), 'result': {'ok': True}}

        responses = self._run([
            self._call(1, {'op_path': '/a', 'instance': 'Show'}),
            self._call(2, {'op_path': '/b'}),
        ], forward)
        first = responses[0]['result']
        self.assertTrue(first['isError'])
        payload = json.loads(first['content'][0]['text'])
        self.assertEqual(payload['error_code'], 'envoy.instance.unreachable')
        # The pinned call after it is a normal forward with a normal result --
        # no "Lost connection" error and no fallback tools.
        self.assertEqual(responses[1]['result'], {'ok': True})
        self.assertNotIn('error', responses[1])
