"""
Test suite: Envoy STDIO bridge (envoy_bridge.py).

Comprehensive tests for the STDIO-to-HTTP proxy including:
- Argument parsing
- HTTP forwarding with SSE format parsing
- STDIO response writing
- Wait/reconnection logic with exponential backoff
- Full event loop: disconnection, single-attempt forwarding, reconnection
- Error type handling (URLError, ConnectionError, OSError)
- Malformed input resilience
- Notification vs request distinction
- Proactive TD process discovery

The bridge is pure Python (no TD dependencies), so these tests
use unittest.mock extensively to simulate network conditions.
"""

import importlib.util
import io
import json
import os
import sys
import time
from unittest.mock import patch, MagicMock, call

# Load the bridge module from disk (pure Python, no TD deps)
_bridge_path = os.path.join(project.folder, 'embody', 'envoy_bridge.py')
_spec = importlib.util.spec_from_file_location('envoy_bridge', _bridge_path)
bridge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bridge)
sys.modules[_spec.name] = bridge  # Register so @patch('envoy_bridge.X') works

# Neutralize background daemon threads so they never spawn during tests.
# Both threads run infinite sleep loops; when tests patch time.sleep with a
# recording mock, the unpatched threads flood the mock with thousands of
# calls AND race the foreground main loop, breaking forward_to_http call
# count assertions and many other expectations.
#   - start_orphan_watchdog: v1, polls parent PID every 30s
#   - start_reconciler:      v2, polls envoy.json + pings backend every 1-5s
# Tests that need to exercise the reconciler call bridge.reconcile()
# directly with explicit heartbeat=True instead.
bridge.start_orphan_watchdog = lambda *args, **kwargs: None
if hasattr(bridge, 'start_reconciler'):
    bridge.start_reconciler = lambda *args, **kwargs: None

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase


# =====================================================================
# Argument Parsing
# =====================================================================

def _slow_clock(start, step=0.01):
    """Callable stand-in for time.monotonic that rises `step` per call.

    NEVER a finite side_effect list: a global time.monotonic patch is
    visible to EVERY thread in the process (the in-process Envoy/uvicorn
    worker calls it constantly), and stray consumers exhaust a finite
    iterator into StopIteration -- observed once mid full-suite run. The
    slow rise keeps timeout deadlines far away; tests using it must drive
    their exit through mocks, never through this clock hitting a deadline.
    """
    state = {'t': start}

    def _tick():
        state['t'] += step
        return state['t']

    return _tick


class TestBridgeParseArgs(EmbodyTestCase):

    def test_default_port(self):
        with patch.object(sys, 'argv', ['envoy_bridge.py']):
            port, config = bridge.parse_args()
            self.assertEqual(port, bridge.DEFAULT_PORT)
            self.assertIsNone(config)

    def test_custom_port(self):
        with patch.object(sys, 'argv', ['envoy_bridge.py', '--port', '9999']):
            port, config = bridge.parse_args()
            self.assertEqual(port, 9999)

    def test_port_flag_at_end_without_value(self):
        """--port as last arg with no value - uses default."""
        with patch.object(sys, 'argv', ['envoy_bridge.py', '--port']):
            port, config = bridge.parse_args()
            self.assertEqual(port, bridge.DEFAULT_PORT)

    def test_ignores_unknown_args(self):
        with patch.object(sys, 'argv', ['envoy_bridge.py', '--verbose', '--port', '8080']):
            port, config = bridge.parse_args()
            self.assertEqual(port, 8080)

    def test_port_zero(self):
        with patch.object(sys, 'argv', ['envoy_bridge.py', '--port', '0']):
            port, config = bridge.parse_args()
            self.assertEqual(port, 0)

    def test_config_arg(self):
        with patch.object(sys, 'argv', ['envoy_bridge.py', '--config', '/tmp/test.json']):
            port, config = bridge.parse_args()
            self.assertEqual(port, bridge.DEFAULT_PORT)
            self.assertEqual(config, '/tmp/test.json')

    def test_port_and_config(self):
        with patch.object(sys, 'argv', ['envoy_bridge.py', '--port', '9999', '--config', '/tmp/c.json']):
            port, config = bridge.parse_args()
            self.assertEqual(port, 9999)
            self.assertEqual(config, '/tmp/c.json')


# =====================================================================
# HTTP Forwarding & SSE Parsing
# =====================================================================

class TestBridgeForwardToHttp(EmbodyTestCase):
    """
    forward_to_http calls ``urllib.request.urlopen`` once per message.
    Tests mock urlopen directly so they exercise the real parsing path
    without opening real sockets.
    """

    def _make_response(self, body):
        """Build a mock response object that urlopen would return."""
        resp = MagicMock()
        resp.read.return_value = body.encode('utf-8')
        return resp

    # --- SSE format ---

    def test_sse_format_single_event(self):
        body = 'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{"ok":true}}\n\n'
        with patch('urllib.request.urlopen', return_value=self._make_response(body)):
            result = bridge.forward_to_http('http://localhost:9870/mcp', {'id': 1})
        self.assertTrue(result['result']['ok'])

    def test_sse_data_only_no_event_line(self):
        """SSE with just data: line, no event: prefix."""
        body = 'data: {"id":1,"result":"bare"}\n\n'
        with patch('urllib.request.urlopen', return_value=self._make_response(body)):
            result = bridge.forward_to_http('http://localhost:9870/mcp', {'id': 1})
        self.assertEqual(result['result'], 'bare')

    def test_sse_multiple_events_returns_first(self):
        body = 'data: {"first":true}\n\ndata: {"second":true}\n\n'
        with patch('urllib.request.urlopen', return_value=self._make_response(body)):
            result = bridge.forward_to_http('http://localhost:9870/mcp', {'id': 1})
        self.assertTrue(result.get('first'))

    def test_sse_with_extra_whitespace(self):
        body = '  data: {"id":1}  \n\n'
        with patch('urllib.request.urlopen', return_value=self._make_response(body)):
            result = bridge.forward_to_http('http://localhost:9870/mcp', {'id': 1})
        self.assertEqual(result['id'], 1)

    # --- Plain JSON fallback ---

    def test_plain_json_response(self):
        body = '{"jsonrpc":"2.0","id":1,"result":"hello"}'
        with patch('urllib.request.urlopen', return_value=self._make_response(body)):
            result = bridge.forward_to_http('http://localhost:9870/mcp', {'id': 1})
        self.assertEqual(result['result'], 'hello')

    def test_plain_json_with_surrounding_whitespace(self):
        body = '  \n  {"jsonrpc":"2.0","id":1,"result":"padded"}  \n  '
        with patch('urllib.request.urlopen', return_value=self._make_response(body)):
            result = bridge.forward_to_http('http://localhost:9870/mcp', {'id': 1})
        self.assertEqual(result['result'], 'padded')

    # --- Empty / malformed responses ---

    def test_empty_response_body(self):
        with patch('urllib.request.urlopen', return_value=self._make_response('')):
            result = bridge.forward_to_http('http://localhost:9870/mcp', {'id': 1})
        self.assertIsNone(result)

    def test_whitespace_only_response(self):
        with patch('urllib.request.urlopen', return_value=self._make_response('   \n  ')):
            result = bridge.forward_to_http('http://localhost:9870/mcp', {'id': 1})
        self.assertIsNone(result)

    def test_malformed_json_in_plain_body(self):
        """Garbled non-JSON body returns None, doesn't crash."""
        with patch('urllib.request.urlopen', return_value=self._make_response('not json at all')):
            result = bridge.forward_to_http('http://localhost:9870/mcp', {'id': 1})
        self.assertIsNone(result)

    # --- Error propagation ---

    def test_http_error_raises_oserror(self):
        """HTTP status >= 400 raises HTTPError (an OSError subclass)."""
        import urllib.error
        err = urllib.error.HTTPError(
            'http://localhost:9870/mcp', 500, 'Internal Server Error', {}, None)
        with patch('urllib.request.urlopen', side_effect=err):
            raised = False
            try:
                bridge.forward_to_http('http://localhost:9870/mcp', {'id': 1})
            except OSError:
                raised = True
            self.assertTrue(raised, 'HTTP 500 should raise OSError')

    def test_connection_error_propagates(self):
        """ConnectionRefusedError (subclass of OSError) propagates."""
        with patch('urllib.request.urlopen',
                   side_effect=ConnectionRefusedError('refused')):
            raised = False
            try:
                bridge.forward_to_http('http://localhost:9870/mcp', {'id': 1})
            except OSError:
                raised = True
            self.assertTrue(raised, 'ConnectionRefusedError should propagate as OSError')

    def test_timeout_propagates(self):
        """Socket timeout (OSError subclass) propagates."""
        import socket
        with patch('urllib.request.urlopen',
                   side_effect=socket.timeout('timed out')):
            raised = False
            try:
                bridge.forward_to_http('http://localhost:9870/mcp', {'id': 1})
            except OSError:
                raised = True
            self.assertTrue(raised, 'socket.timeout should propagate as OSError')

    def test_url_error_propagates_as_oserror(self):
        """urllib.error.URLError (OSError subclass) propagates so the main
        loop's connection-lost handler can catch it."""
        import urllib.error
        with patch('urllib.request.urlopen',
                   side_effect=urllib.error.URLError('bad url')):
            raised = False
            try:
                bridge.forward_to_http('http://localhost:9870/mcp', {'id': 1})
            except OSError:
                raised = True
            self.assertTrue(raised, 'URLError should propagate as OSError')

    # --- Request correctness ---

    def test_request_content_type_header(self):
        with patch('urllib.request.urlopen',
                   return_value=self._make_response('{}')) as mock_urlopen:
            bridge.forward_to_http('http://localhost:9870/mcp', {'id': 1})
        req = mock_urlopen.call_args[0][0]
        # urllib lowercases header names internally; use get_header for safe lookup.
        self.assertEqual(req.get_header('Content-type'), 'application/json')

    def test_request_accept_header(self):
        with patch('urllib.request.urlopen',
                   return_value=self._make_response('{}')) as mock_urlopen:
            bridge.forward_to_http('http://localhost:9870/mcp', {'id': 1})
        req = mock_urlopen.call_args[0][0]
        self.assertIn('text/event-stream', req.get_header('Accept', ''))

    def test_request_body_is_valid_json(self):
        msg = {'jsonrpc': '2.0', 'id': 42, 'method': 'tools/call'}
        with patch('urllib.request.urlopen',
                   return_value=self._make_response('{}')) as mock_urlopen:
            bridge.forward_to_http('http://localhost:9870/mcp', msg)
        req = mock_urlopen.call_args[0][0]
        self.assertDictEqual(json.loads(req.data.decode('utf-8')), msg)

    def test_custom_timeout_passed(self):
        with patch('urllib.request.urlopen',
                   return_value=self._make_response('{}')) as mock_urlopen:
            bridge.forward_to_http('http://localhost:9870/mcp', {'id': 1}, timeout=5)
        # urlopen(req, timeout=5) - timeout passed as kwarg.
        self.assertEqual(mock_urlopen.call_args[1].get('timeout'), 5)


# =====================================================================
# STDIO Response Writing
# =====================================================================

class TestBridgeSendResponse(EmbodyTestCase):

    def test_writes_json_followed_by_newline(self):
        output = io.StringIO()
        with patch.object(sys, 'stdout', output):
            bridge.send_response({'jsonrpc': '2.0', 'id': 1, 'result': 'ok'})
        raw = output.getvalue()
        self.assertTrue(raw.endswith('\n'), 'Must end with newline')
        parsed = json.loads(raw.strip())
        self.assertEqual(parsed['result'], 'ok')

    def test_flushes_stdout(self):
        output = MagicMock()
        with patch.object(sys, 'stdout', output):
            bridge.send_response({'id': 1})
        output.flush.assert_called()

    def test_send_error_format(self):
        output = io.StringIO()
        with patch.object(sys, 'stdout', output):
            bridge.send_error(42, -32000, 'Something failed')
        parsed = json.loads(output.getvalue().strip())
        self.assertEqual(parsed['id'], 42)
        self.assertEqual(parsed['error']['code'], -32000)
        self.assertEqual(parsed['error']['message'], 'Something failed')

    def test_send_error_has_jsonrpc_version(self):
        output = io.StringIO()
        with patch.object(sys, 'stdout', output):
            bridge.send_error(1, -1, 'err')
        parsed = json.loads(output.getvalue().strip())
        self.assertEqual(parsed['jsonrpc'], '2.0')


# =====================================================================
# Logging
# =====================================================================

class TestBridgeLog(EmbodyTestCase):

    def test_log_writes_to_stderr(self):
        err = io.StringIO()
        with patch.object(sys, 'stderr', err):
            bridge.log('hello world')
        self.assertIn('hello world', err.getvalue())

    def test_log_includes_prefix(self):
        err = io.StringIO()
        with patch.object(sys, 'stderr', err):
            bridge.log('test message')
        # Format is "[envoy-bridge:<pid>] <ts> <msg>" -- check the stable prefix.
        self.assertIn('[envoy-bridge:', err.getvalue())

    def test_log_flushes_stderr(self):
        err = MagicMock()
        with patch.object(sys, 'stderr', err):
            bridge.log('flush test')
        err.flush.assert_called()


# =====================================================================
# wait_for_envoy - Retry / Reconnection Logic
# =====================================================================

class TestBridgeWaitForEnvoy(EmbodyTestCase):

    def test_server_up_immediately(self):
        """Server responds on first probe - instant success."""
        with patch('urllib.request.urlopen'):
            result = bridge.wait_for_envoy(
                'http://localhost:9870/mcp', time.monotonic() + 10)
        self.assertTrue(result)

    def test_http_error_means_reachable(self):
        """HTTP 400/500 means server is up (just rejecting the probe)."""
        import urllib.error
        exc = urllib.error.HTTPError('url', 400, 'Bad Request', {}, None)
        with patch('urllib.request.urlopen', side_effect=exc):
            result = bridge.wait_for_envoy(
                'http://localhost:9870/mcp', time.monotonic() + 10)
        self.assertTrue(result)

    def test_http_500_means_reachable(self):
        """HTTP 500 still means server process is running."""
        import urllib.error
        exc = urllib.error.HTTPError('url', 500, 'Server Error', {}, None)
        with patch('urllib.request.urlopen', side_effect=exc):
            result = bridge.wait_for_envoy(
                'http://localhost:9870/mcp', time.monotonic() + 10)
        self.assertTrue(result)

    def test_connection_refused_retries_then_succeeds(self):
        """Connection refused for 2 attempts, then server comes up."""
        import urllib.error
        attempts = [0]

        def side_effect(*a, **kw):
            attempts[0] += 1
            if attempts[0] < 3:
                raise urllib.error.URLError('Connection refused')
            return MagicMock()

        with patch('urllib.request.urlopen', side_effect=side_effect), \
             patch('time.sleep'):
            result = bridge.wait_for_envoy(
                'http://localhost:9870/mcp', time.monotonic() + 60)
        self.assertTrue(result)
        self.assertEqual(attempts[0], 3)

    def test_deadline_expired_returns_false(self):
        """Already-expired deadline returns False immediately."""
        import urllib.error
        with patch('urllib.request.urlopen',
                   side_effect=urllib.error.URLError('refused')), \
             patch('time.sleep'):
            result = bridge.wait_for_envoy(
                'http://localhost:9870/mcp', time.monotonic() - 1)
        self.assertFalse(result)

    def test_os_error_retries(self):
        """OSError (network unreachable) triggers retry."""
        attempts = [0]

        def side_effect(*a, **kw):
            attempts[0] += 1
            if attempts[0] < 2:
                raise OSError('Network unreachable')
            return MagicMock()

        with patch('urllib.request.urlopen', side_effect=side_effect), \
             patch('time.sleep'):
            result = bridge.wait_for_envoy(
                'http://localhost:9870/mcp', time.monotonic() + 60)
        self.assertTrue(result)

    def test_connection_error_retries(self):
        """ConnectionError triggers retry."""
        attempts = [0]

        def side_effect(*a, **kw):
            attempts[0] += 1
            if attempts[0] < 2:
                raise ConnectionError('Connection reset')
            return MagicMock()

        with patch('urllib.request.urlopen', side_effect=side_effect), \
             patch('time.sleep'):
            result = bridge.wait_for_envoy(
                'http://localhost:9870/mcp', time.monotonic() + 60)
        self.assertTrue(result)

    def test_connection_reset_error_retries(self):
        """ConnectionResetError (subclass of ConnectionError) triggers retry."""
        attempts = [0]

        def side_effect(*a, **kw):
            attempts[0] += 1
            if attempts[0] < 2:
                raise ConnectionResetError('Connection reset by peer')
            return MagicMock()

        with patch('urllib.request.urlopen', side_effect=side_effect), \
             patch('time.sleep'):
            result = bridge.wait_for_envoy(
                'http://localhost:9870/mcp', time.monotonic() + 60)
        self.assertTrue(result)

    def test_retry_uses_exponential_backoff(self):
        """Verify sleep intervals follow RETRY_INTERVALS."""
        import urllib.error
        sleeps = []

        # Simulate a clock that advances by the sleep duration each call,
        # so the loop doesn't spin at full speed filling stderr.
        fake_time = [0.0]

        def mock_monotonic():
            return fake_time[0]

        def mock_sleep(duration):
            sleeps.append(duration)
            fake_time[0] += duration

        deadline = 300.0  # Plenty of headroom

        with patch('urllib.request.urlopen',
                   side_effect=urllib.error.URLError('refused')), \
             patch('time.sleep', side_effect=mock_sleep), \
             patch('time.monotonic', side_effect=mock_monotonic):
            bridge.wait_for_envoy('http://localhost:9870/mcp', deadline)

        # Should have retried using all RETRY_INTERVALS entries
        self.assertGreater(len(sleeps), 0, 'Should have retried at least once')
        # First sleep should match RETRY_INTERVALS[0]
        self.assertAlmostEqual(sleeps[0], bridge.RETRY_INTERVALS[0], delta=0.01)
        # Verify several intervals match the schedule
        for i, expected in enumerate(bridge.RETRY_INTERVALS):
            if i < len(sleeps):
                self.assertAlmostEqual(sleeps[i], expected, delta=0.01)

    def test_retry_clamps_sleep_to_remaining_time(self):
        """Sleep duration is clamped to time remaining before deadline."""
        import urllib.error
        sleeps = []

        fake_time = [0.0]
        deadline = 0.3  # Very tight - less than RETRY_INTERVALS[0]=0.5

        def mock_monotonic():
            return fake_time[0]

        def mock_sleep(duration):
            sleeps.append(duration)
            fake_time[0] += duration

        with patch('urllib.request.urlopen',
                   side_effect=urllib.error.URLError('refused')), \
             patch('time.sleep', side_effect=mock_sleep), \
             patch('time.monotonic', side_effect=mock_monotonic):
            bridge.wait_for_envoy('http://localhost:9870/mcp', deadline)

        # With only 0.3s total, sleeps must be clamped below the interval
        self.assertGreater(len(sleeps), 0)
        for s in sleeps:
            self.assertLessEqual(s, deadline + 0.01)

    def test_many_retries_caps_at_last_interval(self):
        """After exhausting RETRY_INTERVALS, uses the last value."""
        import urllib.error
        attempts = [0]
        sleeps = []
        max_attempts = len(bridge.RETRY_INTERVALS) + 3

        fake_time = [0.0]

        def mock_monotonic():
            return fake_time[0]

        def fail(*a, **kw):
            attempts[0] += 1
            if attempts[0] > max_attempts:
                return MagicMock()
            raise urllib.error.URLError('refused')

        def mock_sleep(duration):
            sleeps.append(duration)
            fake_time[0] += duration

        with patch('urllib.request.urlopen', side_effect=fail), \
             patch('time.sleep', side_effect=mock_sleep), \
             patch('time.monotonic', side_effect=mock_monotonic):
            bridge.wait_for_envoy('http://localhost:9870/mcp', 500.0)

        # Past the end of RETRY_INTERVALS, sleeps should cap at the last value
        self.assertGreater(len(sleeps), len(bridge.RETRY_INTERVALS))
        tail_sleep = sleeps[len(bridge.RETRY_INTERVALS)]
        self.assertAlmostEqual(
            tail_sleep, bridge.RETRY_INTERVALS[-1], delta=0.01)


# =====================================================================
# Main Event Loop - Disconnection & Reconnection Scenarios
# =====================================================================

class TestBridgeMainLoop(EmbodyTestCase):
    """Full integration tests for main() with mocked I/O and network."""

    def _make_stdin(self, messages):
        """Build a mock stdin from a list of JSON-serializable messages or raw strings."""
        lines = []
        for msg in messages:
            lines.append(msg if isinstance(msg, str) else json.dumps(msg))
        return io.StringIO('\n'.join(lines) + '\n')

    def _run_main(self, stdin_messages, wait_result=True,
                  forward_side_effect=None, port_args=None):
        """Run main() with full mocking. Returns list of parsed JSON responses."""
        stdin = self._make_stdin(stdin_messages)
        stdout = io.StringIO()
        stderr = io.StringIO()

        if forward_side_effect is None:
            fwd = MagicMock(
                return_value={'jsonrpc': '2.0', 'id': 1, 'result': 'ok'})
        elif callable(forward_side_effect) and not isinstance(forward_side_effect, MagicMock):
            fwd = MagicMock(side_effect=forward_side_effect)
        else:
            fwd = forward_side_effect

        argv = port_args or ['envoy_bridge.py']

        with patch.object(sys, 'stdin', stdin), \
             patch.object(sys, 'stdout', stdout), \
             patch.object(sys, 'stderr', stderr), \
             patch.object(sys, 'argv', argv), \
             patch.object(bridge, 'wait_for_envoy', return_value=wait_result), \
             patch.object(bridge, 'forward_to_http', fwd), \
             patch.object(bridge, 'find_td_pid', return_value=None), \
             patch.object(bridge, 'kill_stale_bridges'), \
             patch('time.sleep'):
            bridge.main()

        raw_lines = [l for l in stdout.getvalue().strip().split('\n') if l.strip()]
        return [json.loads(l) for l in raw_lines] if raw_lines else []

    # --- Happy path ---

    def test_single_request_forwarded(self):
        msg = {'jsonrpc': '2.0', 'id': 1, 'method': 'resources/list'}
        responses = self._run_main([msg])
        self.assertLen(responses, 1)
        self.assertEqual(responses[0]['result'], 'ok')

    def test_multiple_requests_forwarded(self):
        call_count = [0]

        def forward(url, msg, **kw):
            call_count[0] += 1
            return {'jsonrpc': '2.0', 'id': msg['id'],
                    'result': f'resp_{call_count[0]}'}

        msgs = [
            {'jsonrpc': '2.0', 'id': 1, 'method': 'resources/list'},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'prompts/list'},
            {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call'},
        ]
        responses = self._run_main(msgs, forward_side_effect=forward)
        self.assertLen(responses, 3)
        self.assertEqual(responses[0]['result'], 'resp_1')
        self.assertEqual(responses[2]['result'], 'resp_3')

    # --- Initial connection failure ---

    def test_initial_connection_timeout_sends_error(self):
        """Non-protocol method gets error when forward_to_http fails.

        v2 bridge does not block on initial connect for arbitrary methods --
        it tries to forward and only errors out when the forward call itself
        raises (which is what happens when Envoy is unreachable).
        """
        def fail(url, msg, **kw):
            raise OSError('Connection refused')
        msg = {'jsonrpc': '2.0', 'id': 1, 'method': 'resources/list'}
        responses = self._run_main(
            [msg], wait_result=False, forward_side_effect=fail)
        self.assertLen(responses, 1)
        self.assertDictHasKey(responses[0], 'error')
        self.assertIn('connection lost', responses[0]['error']['message'].lower())

    def test_initial_timeout_includes_actionable_hint(self):
        def fail(url, msg, **kw):
            raise OSError('Connection refused')
        msg = {'jsonrpc': '2.0', 'id': 1, 'method': 'resources/list'}
        responses = self._run_main(
            [msg], wait_result=False, forward_side_effect=fail,
            port_args=['envoy_bridge.py', '--port', '1234'])
        self.assertIn('launch_td', responses[0]['error']['message'])

    def test_initial_timeout_notification_no_response(self):
        """Notification during connection failure produces no output."""
        msg = {'jsonrpc': '2.0', 'method': 'some/notification'}
        responses = self._run_main([msg], wait_result=False)
        self.assertLen(responses, 0)

    def test_initialize_handled_locally_when_disconnected(self):
        """initialize responds with bridge server info, no Envoy needed."""
        msg = {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize'}
        responses = self._run_main([msg], wait_result=False)
        self.assertLen(responses, 1)
        result = responses[0]['result']
        self.assertEqual(result['serverInfo']['name'], 'envoy-bridge')
        self.assertIn('protocolVersion', result)
        self.assertIn('capabilities', result)

    def test_notifications_initialized_handled_locally_when_disconnected(self):
        """notifications/initialized produces no output when disconnected."""
        msg = {'jsonrpc': '2.0', 'method': 'notifications/initialized'}
        responses = self._run_main([msg], wait_result=False)
        self.assertLen(responses, 0)

    def test_tools_list_handled_locally_when_disconnected(self):
        """tools/list returns bridge-only tools without waiting for Envoy."""
        msg = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}
        responses = self._run_main([msg], wait_result=False)
        self.assertLen(responses, 1)
        names = {t['name'] for t in responses[0]['result']['tools']}
        self.assertIn('launch_td', names)
        self.assertIn('get_td_status', names)

    def test_full_mcp_handshake_when_td_down(self):
        """Full init -> tools/list -> launch_td works without Envoy."""
        msgs = [
            {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize'},
            {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
            {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call',
             'params': {'name': 'get_td_status', 'arguments': {}}},
        ]
        responses = self._run_main(msgs, wait_result=False)
        # initialize + tools/list + get_td_status = 3 responses
        # (notifications/initialized produces no response)
        self.assertLen(responses, 3)
        self.assertEqual(responses[0]['result']['serverInfo']['name'],
                         'envoy-bridge')
        self.assertIn('launch_td',
                      {t['name'] for t in responses[1]['result']['tools']})

    def test_local_ping_request_returns_empty_result(self):
        """Ping request is handled locally with empty result, no Envoy call.

        Implementation: envoy_bridge.py ping handler responds with
        {result: {}} for requests, regardless of connection state.
        """
        msg = {'jsonrpc': '2.0', 'id': 42, 'method': 'ping'}
        responses = self._run_main([msg], wait_result=False)
        self.assertLen(responses, 1)
        self.assertEqual(responses[0],
            {'jsonrpc': '2.0', 'id': 42, 'result': {}})

    def test_local_ping_notification_produces_no_response(self):
        """Ping without id is a notification; handler must produce no output.

        Implementation: ping handler short-circuits via `continue` after
        the notification check, so neither a response nor an error is sent.
        """
        msg = {'jsonrpc': '2.0', 'method': 'ping'}
        responses = self._run_main([msg], wait_result=False)
        self.assertLen(responses, 0)

    def test_initial_timeout_then_next_message_retries_connect(self):
        """After a forward failure, the next message keeps trying.

        v2 bridge does not block on wait_for_envoy for arbitrary methods.
        Recovery is per-message: each request calls forward_to_http directly,
        and if it fails, the bridge marks disconnected but continues serving
        subsequent messages (which try forward_to_http again).
        """
        msgs = [
            {'jsonrpc': '2.0', 'id': 1, 'method': 'first'},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'second'},
        ]
        forward_calls = [0]
        def fwd(url, msg, **kw):
            forward_calls[0] += 1
            if forward_calls[0] == 1:
                raise OSError('Connection refused')
            return {'jsonrpc': '2.0', 'id': msg['id'], 'result': 'ok'}

        responses = self._run_main(
            msgs, wait_result=False, forward_side_effect=fwd)

        # Both messages get forwarded -- the second one because the bridge
        # keeps trying after a failure.
        self.assertEqual(forward_calls[0], 2)
        # First: error (forward failed), Second: success.
        self.assertLen(responses, 2)
        self.assertDictHasKey(responses[0], 'error')
        self.assertEqual(responses[1]['result'], 'ok')

    # --- Single-attempt forwarding (v2: no per-request retries) ---

    def test_single_failure_sends_error(self):
        """Single URLError immediately returns error - no retries."""
        import urllib.error

        def always_fail(url, msg, **kw):
            raise urllib.error.URLError('Connection refused')

        msg = {'jsonrpc': '2.0', 'id': 1, 'method': 'test'}
        responses = self._run_main([msg], forward_side_effect=always_fail)
        self.assertLen(responses, 1)
        self.assertDictHasKey(responses[0], 'error')
        self.assertIn('connection lost', responses[0]['error']['message'].lower())

    def test_failure_notification_no_response(self):
        """Notification with forward failure - no error sent."""
        import urllib.error

        def always_fail(url, msg, **kw):
            raise urllib.error.URLError('refused')

        msg = {'jsonrpc': '2.0', 'method': 'notifications/progress'}
        responses = self._run_main([msg], forward_side_effect=always_fail)
        self.assertLen(responses, 0)

    def test_single_attempt_only(self):
        """forward_to_http is called exactly once per request - no retries."""
        import urllib.error
        call_count = [0]

        def forward(url, msg, **kw):
            call_count[0] += 1
            raise urllib.error.URLError('refused')

        msg = {'jsonrpc': '2.0', 'id': 1, 'method': 'test'}
        self._run_main([msg], forward_side_effect=forward)
        self.assertEqual(call_count[0], 1)

    # --- Different connection error types ---

    def test_url_error_sends_error(self):
        import urllib.error
        def forward(url, msg, **kw):
            raise urllib.error.URLError('Connection refused')

        responses = self._run_main(
            [{'jsonrpc': '2.0', 'id': 1, 'method': 'x'}],
            forward_side_effect=forward)
        self.assertDictHasKey(responses[0], 'error')

    def test_connection_error_sends_error(self):
        def forward(url, msg, **kw):
            raise ConnectionError('Connection reset by peer')

        responses = self._run_main(
            [{'jsonrpc': '2.0', 'id': 1, 'method': 'x'}],
            forward_side_effect=forward)
        self.assertDictHasKey(responses[0], 'error')

    def test_os_error_sends_error(self):
        def forward(url, msg, **kw):
            raise OSError('Network unreachable')

        responses = self._run_main(
            [{'jsonrpc': '2.0', 'id': 1, 'method': 'x'}],
            forward_side_effect=forward)
        self.assertDictHasKey(responses[0], 'error')

    def test_connection_reset_error_sends_error(self):
        """ConnectionResetError (server crashed mid-response)."""
        def forward(url, msg, **kw):
            raise ConnectionResetError('Connection reset by peer')

        responses = self._run_main(
            [{'jsonrpc': '2.0', 'id': 1, 'method': 'x'}],
            forward_side_effect=forward)
        self.assertDictHasKey(responses[0], 'error')

    def test_non_connection_error_not_retried(self):
        """ValueError from forward is treated as malformed response."""
        call_count = [0]
        def forward(url, msg, **kw):
            call_count[0] += 1
            raise ValueError('unexpected error')

        # ValueError is caught by the (JSONDecodeError, ValueError) handler,
        # which sends an error response and marks disconnected.
        responses = self._run_main(
            [{'jsonrpc': '2.0', 'id': 1, 'method': 'x'}],
            forward_side_effect=forward)
        self.assertLen(responses, 1)
        self.assertIn('error', responses[0])
        self.assertEqual(call_count[0], 1)

    # --- Disconnection and reconnection ---

    def test_disconnect_triggers_reconnect_on_next_message(self):
        """After failure, next message retries forward directly (no blocking probe).

        v2 removed the blocking reconnect probe: when disconnected, the bridge
        just tries the forward immediately. If it works, connected is restored.
        """
        import urllib.error
        call_count = [0]

        def forward(url, msg, **kw):
            call_count[0] += 1
            if call_count[0] == 1:  # First msg fails
                raise urllib.error.URLError('refused')
            return {'jsonrpc': '2.0', 'id': msg.get('id'), 'result': 'back'}

        msgs = [
            {'jsonrpc': '2.0', 'id': 1, 'method': 'a'},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'b'},
        ]
        stdin = self._make_stdin(msgs)
        stdout = io.StringIO()
        stderr = io.StringIO()

        with patch.object(sys, 'stdin', stdin), \
             patch.object(sys, 'stdout', stdout), \
             patch.object(sys, 'stderr', stderr), \
             patch.object(sys, 'argv', ['envoy_bridge.py']), \
             patch.object(bridge, 'wait_for_envoy', return_value=True), \
             patch.object(bridge, 'forward_to_http', side_effect=forward), \
             patch.object(bridge, 'find_td_pid', return_value=None), \
             patch.object(bridge, 'kill_stale_bridges'), \
             patch('time.sleep'):
            bridge.main()

        lines = [l for l in stdout.getvalue().strip().split('\n') if l.strip()]
        responses = [json.loads(l) for l in lines]
        self.assertLen(responses, 2)
        self.assertDictHasKey(responses[0], 'error')  # First failed
        self.assertEqual(responses[1]['result'], 'back')  # Retry succeeded

    def test_reconnect_fails_sends_error_again(self):
        """Disconnect, reconnect attempt fails - second error sent."""
        import urllib.error

        def always_fail(url, msg, **kw):
            raise urllib.error.URLError('refused')

        msgs = [
            {'jsonrpc': '2.0', 'id': 1, 'method': 'a'},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'b'},
        ]
        stdin = self._make_stdin(msgs)
        stdout = io.StringIO()
        stderr = io.StringIO()

        wait_calls = [0]

        def mock_wait(url, deadline):
            wait_calls[0] += 1
            if wait_calls[0] == 1:
                return True   # Initial connect succeeds
            return False      # Reconnect fails

        with patch.object(sys, 'stdin', stdin), \
             patch.object(sys, 'stdout', stdout), \
             patch.object(sys, 'stderr', stderr), \
             patch.object(sys, 'argv', ['envoy_bridge.py']), \
             patch.object(bridge, 'wait_for_envoy', side_effect=mock_wait), \
             patch.object(bridge, 'forward_to_http', side_effect=always_fail), \
             patch.object(bridge, 'find_td_pid', return_value=None), \
             patch.object(bridge, 'kill_stale_bridges'), \
             patch('time.sleep'):
            bridge.main()

        lines = [l for l in stdout.getvalue().strip().split('\n') if l.strip()]
        responses = [json.loads(l) for l in lines]
        # Both should be errors
        self.assertLen(responses, 2)
        self.assertDictHasKey(responses[0], 'error')
        self.assertDictHasKey(responses[1], 'error')
        self.assertIn('connection lost', responses[1]['error']['message'].lower())

    def test_multiple_disconnect_reconnect_cycles(self):
        """Server goes down, comes back, goes down, comes back."""
        import urllib.error

        def forward(url, msg, **kw):
            msg_id = msg.get('id')
            if msg_id == 1:
                return {'jsonrpc': '2.0', 'id': 1, 'result': 'ok1'}
            if msg_id == 2:
                raise urllib.error.URLError('server down')
            if msg_id == 3:
                return {'jsonrpc': '2.0', 'id': 3, 'result': 'ok3'}
            if msg_id == 4:
                raise urllib.error.URLError('server down again')
            if msg_id == 5:
                return {'jsonrpc': '2.0', 'id': 5, 'result': 'ok5'}
            return None

        msgs = [
            {'jsonrpc': '2.0', 'id': i, 'method': f'op{i}'}
            for i in range(1, 6)
        ]
        stdin = self._make_stdin(msgs)
        stdout = io.StringIO()
        stderr = io.StringIO()

        with patch.object(sys, 'stdin', stdin), \
             patch.object(sys, 'stdout', stdout), \
             patch.object(sys, 'stderr', stderr), \
             patch.object(sys, 'argv', ['envoy_bridge.py']), \
             patch.object(bridge, 'wait_for_envoy', return_value=True), \
             patch.object(bridge, 'forward_to_http', side_effect=forward), \
             patch.object(bridge, 'find_td_pid', return_value=None), \
             patch.object(bridge, 'kill_stale_bridges'), \
             patch('time.sleep'):
            bridge.main()

        lines = [l for l in stdout.getvalue().strip().split('\n') if l.strip()]
        responses = [json.loads(l) for l in lines]
        # 1=ok, 2=error, 3=ok (reconnected), 4=error, 5=ok (reconnected)
        self.assertLen(responses, 5)
        self.assertEqual(responses[0]['result'], 'ok1')
        self.assertDictHasKey(responses[1], 'error')
        self.assertEqual(responses[2]['result'], 'ok3')
        self.assertDictHasKey(responses[3], 'error')
        self.assertEqual(responses[4]['result'], 'ok5')

    def test_rapid_disconnect_reconnect(self):
        """Disconnect and immediately reconnect on the very next message."""
        import urllib.error
        call_count = [0]

        def forward(url, msg, **kw):
            call_count[0] += 1
            if call_count[0] == 1:  # First message fails (single attempt)
                raise urllib.error.URLError('down')
            return {'jsonrpc': '2.0', 'id': msg['id'], 'result': 'up'}

        msgs = [
            {'jsonrpc': '2.0', 'id': 1, 'method': 'a'},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'b'},
        ]
        stdin = self._make_stdin(msgs)
        stdout = io.StringIO()
        stderr = io.StringIO()

        with patch.object(sys, 'stdin', stdin), \
             patch.object(sys, 'stdout', stdout), \
             patch.object(sys, 'stderr', stderr), \
             patch.object(sys, 'argv', ['envoy_bridge.py']), \
             patch.object(bridge, 'wait_for_envoy', return_value=True), \
             patch.object(bridge, 'forward_to_http', side_effect=forward), \
             patch.object(bridge, 'find_td_pid', return_value=None), \
             patch.object(bridge, 'kill_stale_bridges'), \
             patch('time.sleep'):
            bridge.main()

        lines = [l for l in stdout.getvalue().strip().split('\n') if l.strip()]
        responses = [json.loads(l) for l in lines]
        self.assertLen(responses, 2)
        self.assertDictHasKey(responses[0], 'error')
        self.assertEqual(responses[1]['result'], 'up')

    # --- HTTP 500 during forward (single attempt, no retry) ---

    def test_http_500_sends_error(self):
        """HTTPError (subclass of URLError) is caught and returns error."""
        import urllib.error

        def forward(url, msg, **kw):
            raise urllib.error.HTTPError('url', 500, 'Server Error', {}, None)

        responses = self._run_main(
            [{'jsonrpc': '2.0', 'id': 1, 'method': 'x'}],
            forward_side_effect=forward)
        self.assertLen(responses, 1)
        self.assertDictHasKey(responses[0], 'error')

    # --- Notification handling ---

    def test_notification_no_response_sent(self):
        """Notifications (no id) never produce output even with forward data."""
        msg = {'jsonrpc': '2.0', 'method': 'notifications/initialized'}
        responses = self._run_main([msg])
        self.assertLen(responses, 0)

    def test_notification_between_requests(self):
        """Notification sandwiched between requests - only requests get responses."""
        call_count = [0]

        def forward(url, msg, **kw):
            call_count[0] += 1
            return {'jsonrpc': '2.0', 'id': msg.get('id'), 'result': f'r{call_count[0]}'}

        msgs = [
            {'jsonrpc': '2.0', 'id': 1, 'method': 'a'},
            {'jsonrpc': '2.0', 'method': 'notifications/progress'},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'b'},
        ]
        responses = self._run_main(msgs, forward_side_effect=forward)
        self.assertLen(responses, 2)

    def test_notification_forward_failure_no_error_sent(self):
        """Notification that fails forwarding - no error response."""
        import urllib.error

        def forward(url, msg, **kw):
            raise urllib.error.URLError('refused')

        msg = {'jsonrpc': '2.0', 'method': 'notifications/cancelled'}
        responses = self._run_main([msg], forward_side_effect=forward)
        self.assertLen(responses, 0)

    # --- HTTP-status failures are REQUEST failures, not connection loss ---

    def test_http_error_is_request_failure_not_connection_loss(self):
        """A 413 (or any HTTP status) means the server RESPONDED. HTTPError
        subclasses URLError, so before the classification branch an
        oversized tools/call read as 'Lost connection to Envoy' and dropped
        the client to fallback tools. The reply must name the status and,
        for 413, carry the split-the-payload hint."""
        import urllib.error

        def forward(url, msg, **kw):
            raise urllib.error.HTTPError(
                url, 413, 'Payload Too Large', {}, None)

        msg = {'jsonrpc': '2.0', 'id': 7, 'method': 'tools/call'}
        responses = self._run_main([msg], forward_side_effect=forward)
        self.assertLen(responses, 1)
        err = responses[0]['error']['message']
        self.assertIn('HTTP 413', err)
        self.assertIn('64 MiB', err, 'the 413 reply must carry the size hint')
        self.assertNotIn('Lost connection', err)

    def test_http_error_other_codes_have_no_size_hint(self):
        import urllib.error

        def forward(url, msg, **kw):
            raise urllib.error.HTTPError(
                url, 500, 'Internal Server Error', {}, None)

        msg = {'jsonrpc': '2.0', 'id': 8, 'method': 'tools/list'}
        responses = self._run_main([msg], forward_side_effect=forward)
        self.assertLen(responses, 1)
        err = responses[0]['error']['message']
        self.assertIn('HTTP 500', err)
        self.assertNotIn('64 MiB', err)
        self.assertNotIn('Lost connection', err)

    # --- Forward returns None ---

    def test_forward_returns_none_no_response_sent(self):
        """If forward returns None for a request, no response is sent."""
        msg = {'jsonrpc': '2.0', 'id': 1, 'method': 'test'}
        responses = self._run_main(
            [msg], forward_side_effect=lambda url, msg, **kw: None)
        self.assertLen(responses, 0)

    # --- Malformed / unexpected input ---

    def test_malformed_json_skipped(self):
        """Garbled input is skipped, valid messages still processed."""
        msgs = [
            'not valid json {{{',
            json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'test'}),
        ]
        responses = self._run_main(msgs)
        self.assertLen(responses, 1)
        self.assertEqual(responses[0]['result'], 'ok')

    def test_multiple_malformed_lines_skipped(self):
        msgs = [
            '{broken',
            '<<<>>>',
            '',
            json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'test'}),
        ]
        responses = self._run_main(msgs)
        self.assertLen(responses, 1)

    def test_empty_lines_skipped(self):
        stdin = io.StringIO(
            '\n\n' +
            json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'test'}) +
            '\n\n\n')
        stdout = io.StringIO()
        stderr = io.StringIO()

        with patch.object(sys, 'stdin', stdin), \
             patch.object(sys, 'stdout', stdout), \
             patch.object(sys, 'stderr', stderr), \
             patch.object(sys, 'argv', ['envoy_bridge.py']), \
             patch.object(bridge, 'wait_for_envoy', return_value=True), \
             patch.object(bridge, 'forward_to_http',
                          return_value={'jsonrpc': '2.0', 'id': 1, 'result': 'ok'}), \
             patch.object(bridge, 'find_td_pid', return_value=None), \
             patch.object(bridge, 'kill_stale_bridges'), \
             patch('time.sleep'):
            bridge.main()

        lines = [l for l in stdout.getvalue().strip().split('\n') if l.strip()]
        self.assertLen(lines, 1)

    # --- Edge cases ---

    def test_request_id_zero_is_valid(self):
        """JSON-RPC allows id=0 - must NOT be treated as notification."""
        msg = {'jsonrpc': '2.0', 'id': 0, 'method': 'test'}
        responses = self._run_main([msg])
        self.assertLen(responses, 1)

    def test_request_id_string(self):
        """JSON-RPC allows string ids."""
        def forward(url, msg, **kw):
            return {'jsonrpc': '2.0', 'id': msg['id'], 'result': 'ok'}

        msg = {'jsonrpc': '2.0', 'id': 'abc-123', 'method': 'test'}
        responses = self._run_main([msg], forward_side_effect=forward)
        self.assertLen(responses, 1)
        self.assertEqual(responses[0]['id'], 'abc-123')

    def test_request_id_null_treated_as_notification(self):
        """id=null in JSON-RPC is technically a request, but 'id' IS present.
        Our bridge checks 'id' not in message, so null id IS forwarded with response."""
        msg = {'jsonrpc': '2.0', 'id': None, 'method': 'test'}
        responses = self._run_main([msg])
        # id IS in the message dict (even though None), so it's treated as request
        self.assertLen(responses, 1)

    def test_stdin_close_exits_gracefully(self):
        """Empty stdin (immediate EOF) - main() exits without error."""
        stdin = io.StringIO('')
        stdout = io.StringIO()
        stderr = io.StringIO()

        with patch.object(sys, 'stdin', stdin), \
             patch.object(sys, 'stdout', stdout), \
             patch.object(sys, 'stderr', stderr), \
             patch.object(sys, 'argv', ['envoy_bridge.py']):
            bridge.main()  # Should not raise

        self.assertIn('stdin closed', stderr.getvalue())

    def test_only_empty_lines_exits_gracefully(self):
        """stdin with only whitespace/empty lines - exits without error."""
        stdin = io.StringIO('\n\n\n')
        stdout = io.StringIO()
        stderr = io.StringIO()

        with patch.object(sys, 'stdin', stdin), \
             patch.object(sys, 'stdout', stdout), \
             patch.object(sys, 'stderr', stderr), \
             patch.object(sys, 'argv', ['envoy_bridge.py']):
            bridge.main()

        # No responses, no errors
        self.assertEqual(stdout.getvalue().strip(), '')


# =====================================================================
# Entrypoint Exception Handling
# =====================================================================

class TestBridgeEntrypoint(EmbodyTestCase):
    """Test the if __name__ == '__main__' exception handlers.

    These don't go through main() - they wrap it at the top level.
    We test the exception handling logic directly.
    """

    def test_keyboard_interrupt_suppressed(self):
        """KeyboardInterrupt during main() - logged, not propagated."""
        stderr = io.StringIO()

        with patch.object(bridge, 'main', side_effect=KeyboardInterrupt), \
             patch.object(sys, 'stderr', stderr):
            # Simulate the __main__ block behavior
            try:
                bridge.main()
            except KeyboardInterrupt:
                bridge.log('Interrupted, exiting')

        self.assertIn('Interrupted', stderr.getvalue())

    def test_broken_pipe_suppressed(self):
        """BrokenPipeError (client closed stdout) - silently suppressed."""
        with patch.object(bridge, 'main', side_effect=BrokenPipeError):
            # Simulate the __main__ block behavior
            try:
                bridge.main()
            except BrokenPipeError:
                pass  # Should be silently caught


# =====================================================================
# Config Loading
# =====================================================================

class TestBridgeConfig(EmbodyTestCase):

    def test_load_config_missing_file(self):
        result = bridge.load_config('/nonexistent/path.json')
        self.assertEqual(result, {})

    def test_load_config_none_path(self):
        result = bridge.load_config(None)
        self.assertEqual(result, {})

    def test_load_config_valid(self):
        import tempfile
        config = {'toe_path': 'dev/test.toe', 'port': 9870, 'td_executable': '/usr/bin/td'}
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            json.dump(config, f)
            path = f.name
        try:
            result = bridge.load_config(path)
            self.assertEqual(result['toe_path'], 'dev/test.toe')
            self.assertEqual(result['port'], 9870)
        finally:
            os.unlink(path)

    def test_load_config_malformed_json(self):
        import tempfile
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            f.write('not json {{{')
            path = f.name
        try:
            result = bridge.load_config(path)
            self.assertEqual(result, {})
        finally:
            os.unlink(path)

    def test_resolve_toe_path_absolute(self):
        config = {'toe_path': '/abs/path/test.toe'}
        result = bridge.resolve_toe_path(config, '/some/config.json')
        self.assertEqual(result, '/abs/path/test.toe')

    def test_resolve_toe_path_relative(self):
        config = {'toe_path': 'dev/test.toe'}
        result = bridge.resolve_toe_path(config, '/repo/.embody/envoy.json')
        # Build the expectation with the same os.path machinery so the
        # assertion is platform-portable: on Windows, abspath('/repo/...')
        # prepends the current drive and flips separators, so a hardcoded
        # POSIX string can never match there.
        git_root = os.path.dirname(os.path.dirname(
            os.path.abspath('/repo/.embody/envoy.json')))
        self.assertEqual(result, os.path.join(git_root, 'dev/test.toe'))

    def test_resolve_toe_path_missing(self):
        result = bridge.resolve_toe_path({}, '/some/config.json')
        self.assertIsNone(result)


# =====================================================================
# project.json + TD install discovery
# =====================================================================

class TestBridgeProjectJsonAndDiscovery(EmbodyTestCase):
    """Covers load_project_config(), build parsing, and select_td_install()
    matching policy. find_td_installs() itself is platform-dependent, so
    select_td_install is tested via the ``installs=`` injection point."""

    # --- load_project_config -------------------------------------------

    def test_load_project_config_missing(self):
        self.assertEqual(bridge.load_project_config(None), {})
        self.assertEqual(
            bridge.load_project_config('/nonexistent/.embody/envoy.json'), {})

    def test_load_project_config_valid(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            embody = os.path.join(td, '.embody')
            os.makedirs(embody)
            project_json = os.path.join(embody, 'project.json')
            with open(project_json, 'w') as f:
                json.dump({'td_build': '2025.32660'}, f)
            envoy_json = os.path.join(embody, 'envoy.json')
            result = bridge.load_project_config(envoy_json)
            self.assertEqual(result, {'td_build': '2025.32660'})

    def test_load_project_config_malformed(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            embody = os.path.join(td, '.embody')
            os.makedirs(embody)
            with open(os.path.join(embody, 'project.json'), 'w') as f:
                f.write('not json {{{')
            envoy_json = os.path.join(embody, 'envoy.json')
            self.assertEqual(bridge.load_project_config(envoy_json), {})

    def test_load_project_config_non_dict(self):
        """A JSON list/scalar at top level should yield {}."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            embody = os.path.join(td, '.embody')
            os.makedirs(embody)
            with open(os.path.join(embody, 'project.json'), 'w') as f:
                json.dump(['not', 'a', 'dict'], f)
            envoy_json = os.path.join(embody, 'envoy.json')
            self.assertEqual(bridge.load_project_config(envoy_json), {})

    def test_load_project_config_local_overlays_legacy(self):
        """A-14: the machine-local local.json pin wins over a legacy
        td_build still committed in project.json; foreign project.json
        keys survive the merge."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            embody = os.path.join(td, '.embody')
            os.makedirs(embody)
            with open(os.path.join(embody, 'project.json'), 'w') as f:
                json.dump({'td_build': '2025.30000',
                           'convoy': {'id': 'future-key'}}, f)
            with open(os.path.join(embody, 'local.json'), 'w') as f:
                json.dump({'td_build': '2025.33070'}, f)
            result = bridge.load_project_config(
                os.path.join(embody, 'envoy.json'))
            self.assertEqual(result.get('td_build'), '2025.33070',
                             'the machine-local pin must win')
            self.assertEqual(result.get('convoy'), {'id': 'future-key'},
                             'foreign committed keys must survive the merge')

    def test_load_project_config_local_only(self):
        """A repo with the td_build key already retired from project.json
        resolves the pin from local.json alone."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            embody = os.path.join(td, '.embody')
            os.makedirs(embody)
            with open(os.path.join(embody, 'project.json'), 'w') as f:
                json.dump({}, f)
            with open(os.path.join(embody, 'local.json'), 'w') as f:
                json.dump({'td_build': '2025.33070'}, f)
            result = bridge.load_project_config(
                os.path.join(embody, 'envoy.json'))
            self.assertEqual(result.get('td_build'), '2025.33070')

    def test_load_project_config_malformed_local_keeps_legacy(self):
        """A corrupt local.json must not poison the legacy fallback."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            embody = os.path.join(td, '.embody')
            os.makedirs(embody)
            with open(os.path.join(embody, 'project.json'), 'w') as f:
                json.dump({'td_build': '2025.30000'}, f)
            with open(os.path.join(embody, 'local.json'), 'w') as f:
                f.write('not json {{{')
            result = bridge.load_project_config(
                os.path.join(embody, 'envoy.json'))
            self.assertEqual(result.get('td_build'), '2025.30000')

    def test_load_project_config_mangled_project_good_local(self):
        """The realistic post-migration recovery case: a mangled committed
        file must not block the machine-local pin."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            embody = os.path.join(td, '.embody')
            os.makedirs(embody)
            with open(os.path.join(embody, 'project.json'), 'w') as f:
                f.write('corrupt {{{')
            with open(os.path.join(embody, 'local.json'), 'w') as f:
                json.dump({'td_build': '2025.33070'}, f)
            result = bridge.load_project_config(
                os.path.join(embody, 'envoy.json'))
            self.assertEqual(result.get('td_build'), '2025.33070')

    def test_load_project_config_non_dict_local_keeps_legacy(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            embody = os.path.join(td, '.embody')
            os.makedirs(embody)
            with open(os.path.join(embody, 'project.json'), 'w') as f:
                json.dump({'td_build': '2025.30000'}, f)
            with open(os.path.join(embody, 'local.json'), 'w') as f:
                json.dump(['not', 'a', 'dict'], f)
            result = bridge.load_project_config(
                os.path.join(embody, 'envoy.json'))
            self.assertEqual(result.get('td_build'), '2025.30000')

    def test_load_project_config_utf16_degrades_to_warning(self):
        """UnicodeDecodeError is a ValueError, not JSONDecodeError -- a
        UTF-16/binary file must degrade to {} with a warning, never
        raise out of the bridge (panel finding)."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            embody = os.path.join(td, '.embody')
            os.makedirs(embody)
            with open(os.path.join(embody, 'project.json'), 'wb') as f:
                f.write('{"td_build": "x"}'.encode('utf-16'))
            result = bridge.load_project_config(
                os.path.join(embody, 'envoy.json'))
            self.assertEqual(result, {})

    def test_launch_td_unpinned_reaches_install_discovery(self):
        """PANEL BLOCKER pin: with NO pin anywhere (the normal fresh-clone
        state after A-14 retires the committed td_build), launch_td must
        still consult select_td_install -- whose newest-install tier is
        the fresh-clone story -- instead of failing on a missing
        td_executable. The old `if target_build:` gate made that tier
        unreachable exactly when it was needed."""
        import tempfile
        selected = []

        def fake_select(target_build, fallback_exe=None, installs=None):
            selected.append(target_build)
            return None, 'no install (test stop)'

        with tempfile.TemporaryDirectory() as td:
            embody = os.path.join(td, '.embody')
            os.makedirs(embody)
            with open(os.path.join(embody, 'project.json'), 'w') as f:
                json.dump({}, f)   # post-migration committed file
            with patch.object(bridge, 'select_td_install',
                              side_effect=fake_select):
                ok, msg, pid = bridge.launch_td(
                    {}, os.path.join(embody, 'envoy.json'))

        self.assertEqual(
            selected, [None],
            'select_td_install must be consulted unconditionally, with '
            'target_build=None when no pin exists')
        self.assertFalse(ok)
        self.assertIn('no install (test stop)', msg)

    # --- _parse_build --------------------------------------------------

    def test_parse_build_valid(self):
        self.assertEqual(bridge._parse_build('2025.32660'), (2025, 32660))
        self.assertEqual(bridge._parse_build('2023.11340'), (2023, 11340))

    def test_parse_build_embedded(self):
        """Should still parse when surrounded by directory/version text."""
        self.assertEqual(
            bridge._parse_build('TouchDesigner.2025.32660'),
            (2025, 32660))

    def test_parse_build_invalid(self):
        self.assertIsNone(bridge._parse_build(None))
        self.assertIsNone(bridge._parse_build(''))
        self.assertIsNone(bridge._parse_build('not-a-build'))

    # --- select_td_install ---------------------------------------------

    def test_select_exact_match(self):
        installs = [
            ('2025.32660', '/Applications/TD2025.app'),
            ('2024.30000', '/Applications/TD2024.app'),
        ]
        exe, warn = bridge.select_td_install('2025.32660', None, installs)
        self.assertEqual(exe, '/Applications/TD2025.app')
        self.assertIsNone(warn)

    def test_select_same_year_closest(self):
        installs = [
            ('2025.32700', '/Applications/TD2025-newer.app'),
            ('2025.32500', '/Applications/TD2025-older.app'),
            ('2024.30000', '/Applications/TD2024.app'),
        ]
        exe, warn = bridge.select_td_install('2025.32660', None, installs)
        # 32700 is closer to 32660 (delta 40) than 32500 (delta 160)
        self.assertEqual(exe, '/Applications/TD2025-newer.app')
        self.assertIsNotNone(warn)
        self.assertIn('2025.32660', warn)
        self.assertIn('2025.32700', warn)

    def test_select_falls_back_to_envoy_json_when_no_year_match(self):
        installs = [('2023.11340', '/Applications/TD2023.app')]
        # Use a real existing path so os.path.exists() returns True.
        fallback = sys.executable
        exe, warn = bridge.select_td_install('2025.32660', fallback, installs)
        self.assertEqual(exe, fallback)
        self.assertIsNotNone(warn)
        self.assertIn('falling back', warn.lower())

    def test_select_falls_back_to_newest_when_no_envoy_json(self):
        installs = [
            ('2025.32700', '/Applications/TD2025.app'),
            ('2024.30000', '/Applications/TD2024.app'),
        ]
        # No fallback, no year match
        exe, warn = bridge.select_td_install('2023.11340', None, installs)
        self.assertEqual(exe, '/Applications/TD2025.app')
        self.assertIsNotNone(warn)
        self.assertIn('newest', warn.lower())

    def test_select_no_pin_uses_fallback(self):
        """No td_build -> use fallback verbatim, no warning."""
        installs = [('2025.32660', '/Applications/TD2025.app')]
        fallback = sys.executable
        exe, warn = bridge.select_td_install(None, fallback, installs)
        self.assertEqual(exe, fallback)
        self.assertIsNone(warn)

    def test_select_no_pin_no_fallback_returns_newest(self):
        """No pin and no fallback -> newest install, no warning."""
        installs = [
            ('2025.32700', '/Applications/TD2025.app'),
            ('2024.30000', '/Applications/TD2024.app'),
        ]
        exe, warn = bridge.select_td_install(None, None, installs)
        self.assertEqual(exe, '/Applications/TD2025.app')
        self.assertIsNone(warn)

    def test_select_nothing_found(self):
        exe, warn = bridge.select_td_install('2025.32660', None, [])
        self.assertIsNone(exe)
        self.assertIn('No TouchDesigner', warn)
        self.assertIn('2025.32660', warn)

    def test_select_nothing_found_no_pin(self):
        exe, warn = bridge.select_td_install(None, None, [])
        self.assertIsNone(exe)
        self.assertIn('No TouchDesigner', warn)


# =====================================================================
# Process Management
# =====================================================================

class TestBridgeProcessManagement(EmbodyTestCase):

    def test_is_process_alive_none_pid(self):
        self.assertFalse(bridge.is_process_alive(None))

    def test_is_process_alive_current_process(self):
        self.assertTrue(bridge.is_process_alive(os.getpid()))

    def test_is_process_alive_nonexistent_pid(self):
        # PID 99999999 almost certainly doesn't exist
        self.assertFalse(bridge.is_process_alive(99999999))

    def _win32_ctypes(self, open_result, wait_result=0x00000102):
        """Mock ctypes for the win32 branch. Default wait = WAIT_TIMEOUT.

        The branch builds a PRIVATE kernel32 via ctypes.WinDLL('kernel32')
        rather than the process-wide ctypes.windll.kernel32 cache, so that
        setting restype/argtypes cannot mutate prototypes TouchDesigner (or
        anything else in-process) relies on. Mock WinDLL accordingly.
        """
        mock_kernel32 = MagicMock()
        mock_kernel32.OpenProcess.return_value = open_result
        mock_kernel32.WaitForSingleObject.return_value = wait_result
        mock_ctypes = MagicMock()
        mock_ctypes.WinDLL.return_value = mock_kernel32
        return mock_kernel32, mock_ctypes

    @patch('envoy_bridge.sys')
    def test_is_process_alive_win32_uses_openprocess(self, mock_sys):
        """On Windows, uses OpenProcess(SYNCHRONIZE) instead of os.kill."""
        mock_sys.platform = "win32"
        k, mock_ctypes = self._win32_ctypes(42)  # non-zero = valid handle
        with patch.dict('sys.modules', {'ctypes': mock_ctypes}):
            self.assertTrue(bridge.is_process_alive(1234))
        k.OpenProcess.assert_called_once_with(0x00100000, False, 1234)
        k.CloseHandle.assert_called_once_with(42)
        # Never ctypes.windll -- that cache is shared process-wide.
        mock_ctypes.WinDLL.assert_called_once_with("kernel32")

    @patch('envoy_bridge.sys')
    def test_is_process_alive_win32_dead_process(self, mock_sys):
        """On Windows, returns False when OpenProcess returns 0 (dead PID)."""
        mock_sys.platform = "win32"
        k, mock_ctypes = self._win32_ctypes(0)  # zero = failed / no process
        with patch.dict('sys.modules', {'ctypes': mock_ctypes}):
            self.assertFalse(bridge.is_process_alive(9999))
        k.CloseHandle.assert_not_called()
        k.WaitForSingleObject.assert_not_called()

    @patch('envoy_bridge.sys')
    def test_is_process_alive_win32_zombie_handle_is_dead(self, mock_sys):
        """Exited-but-handle-still-open must read DEAD, not alive.

        A Windows process object (and its PID) stays allocated while ANY
        handle to it is open, so OpenProcess keeps succeeding after the
        process exits. OpenProcess alone therefore reports such a PID
        alive forever -- which stranded heartbeat files for exited
        sessions here, leaving them as phantom peers. WAIT_OBJECT_0 means
        the object is signaled, which happens exactly on exit.
        """
        mock_sys.platform = "win32"
        k, mock_ctypes = self._win32_ctypes(42, wait_result=0x00000000)
        with patch.dict('sys.modules', {'ctypes': mock_ctypes}):
            self.assertFalse(bridge.is_process_alive(1234))
        k.CloseHandle.assert_called_once_with(42)  # and no handle leak

    @patch('envoy_bridge.sys')
    def test_is_process_alive_win32_wait_failed_counts_as_alive(self, mock_sys):
        """WAIT_FAILED is inconclusive -- err toward ALIVE.

        Callers use this to prune registry rows and reap heartbeats;
        deleting state for a process we could not verify is the dangerous
        direction, so anything that is not an explicit WAIT_OBJECT_0
        keeps the process considered alive.
        """
        mock_sys.platform = "win32"
        k, mock_ctypes = self._win32_ctypes(42, wait_result=0xFFFFFFFF)
        with patch.dict('sys.modules', {'ctypes': mock_ctypes}):
            self.assertTrue(bridge.is_process_alive(1234))
        k.CloseHandle.assert_called_once_with(42)

    # --- find_all_td_pids: pgrep filtering on macOS/Linux ---

    # _process_is_real_td (added v6.0.80) ps-checks each candidate PID;
    # fake test PIDs would all be dropped as not-real-TD, so stub it True.
    @patch.object(bridge, '_process_is_real_td', new=lambda pid: True)
    @patch.object(bridge, '_is_bridge_process')
    @patch('envoy_bridge.subprocess.run')
    def test_find_all_td_pids_filters_self_and_bridges(
            self, mock_run, mock_is_bridge):
        """find_all_td_pids excludes own PID and bridge processes from
        pgrep output. Without filtering, pgrep -f 'TouchDesigner' would
        match the bridge process running TD's bundled Python."""
        if bridge.sys.platform == 'win32':
            self.skipTest('macOS/Linux pgrep path')
        my_pid = os.getpid()
        fake = MagicMock()
        fake.returncode = 0
        fake.stdout = f'{my_pid}\n12345\n67890\n11111\n'
        mock_run.return_value = fake
        # Simulate one of the candidate PIDs being a bridge process
        mock_is_bridge.side_effect = lambda pid: pid == 67890

        pids = bridge.find_all_td_pids()

        self.assertNotIn(my_pid, pids,
            'Own PID must be excluded')
        self.assertNotIn(67890, pids,
            'Bridge process PID must be excluded')
        self.assertIn(12345, pids)
        self.assertIn(11111, pids)

    @patch('envoy_bridge.subprocess.run')
    def test_find_all_td_pids_returns_empty_on_timeout(self, mock_run):
        """find_all_td_pids returns [] when subprocess times out."""
        if bridge.sys.platform == 'win32':
            self.skipTest('macOS/Linux pgrep path')
        import subprocess as sp
        mock_run.side_effect = sp.TimeoutExpired(cmd=['pgrep'], timeout=5)
        self.assertEqual(bridge.find_all_td_pids(), [])

    @patch('envoy_bridge.subprocess.run')
    def test_find_all_td_pids_returns_empty_when_pgrep_missing(self, mock_run):
        """find_all_td_pids returns [] when pgrep binary is not found."""
        if bridge.sys.platform == 'win32':
            self.skipTest('macOS/Linux pgrep path')
        mock_run.side_effect = FileNotFoundError()
        self.assertEqual(bridge.find_all_td_pids(), [])

    @patch('envoy_bridge.subprocess.run')
    def test_find_all_td_pids_returns_empty_on_pgrep_no_match(self, mock_run):
        """pgrep returncode != 0 (no TD processes found) yields []."""
        if bridge.sys.platform == 'win32':
            self.skipTest('macOS/Linux pgrep path')
        fake = MagicMock()
        fake.returncode = 1  # pgrep returns 1 when no matches
        fake.stdout = ''
        mock_run.return_value = fake
        self.assertEqual(bridge.find_all_td_pids(), [])

    @patch.object(bridge, '_process_is_real_td', new=lambda pid: True)
    @patch.object(bridge, '_is_bridge_process', return_value=False)
    @patch.object(bridge, '_process_cmdline')
    @patch('envoy_bridge.subprocess.run')
    def test_find_all_td_pids_filters_helper_processes(
            self, mock_run, mock_cmdline, _mock_bridge):
        """find_all_td_pids excludes bundled TD helper / CEF subprocesses.

        `pgrep -f TouchDesigner` also matches the Web Render helper
        ("TouchDesigner Web Render.app/.../TouchDesigner") and CEF
        GPU/renderer children -- they share the executable name but are
        not TD instances, and CEF recycles them every few seconds.
        """
        if bridge.sys.platform == 'win32':
            self.skipTest('macOS/Linux pgrep path')
        fake = MagicMock()
        fake.returncode = 0
        fake.stdout = '100\n200\n300\n'
        mock_run.return_value = fake
        cmdlines = {
            100: '/Applications/TouchDesigner.app/Contents/MacOS/TouchDesigner',
            200: '/Applications/TouchDesigner.app/Contents/MacOS/'
                 'TouchDesigner Web Render.app/Contents/MacOS/TouchDesigner',
            300: '/Applications/TouchDesigner.app/Contents/MacOS/'
                 'TouchDesigner Web Render Helper (GPU).app/Contents/MacOS/'
                 'TouchDesigner --type=gpu-process',
        }
        mock_cmdline.side_effect = lambda pid: cmdlines.get(pid, '')

        pids = bridge.find_all_td_pids()

        self.assertEqual(pids, [100],
            'Only the real TD process survives; Web Render helper and CEF '
            'child are filtered out')

    @patch.object(bridge, '_process_cmdline')
    def test_is_td_helper_process_markers(self, mock_cmdline):
        """_is_td_helper_process matches Web Render and CEF --type= cmdlines."""
        mock_cmdline.return_value = '.../TouchDesigner Web Render.app/.../TouchDesigner'
        self.assertTrue(bridge._is_td_helper_process(1))
        mock_cmdline.return_value = '.../TouchDesigner --type=renderer'
        self.assertTrue(bridge._is_td_helper_process(2))
        mock_cmdline.return_value = '/Applications/TouchDesigner.app/Contents/MacOS/TouchDesigner'
        self.assertFalse(bridge._is_td_helper_process(3))
        mock_cmdline.return_value = ''
        self.assertFalse(bridge._is_td_helper_process(4))


# =====================================================================
# Meta-Tool Interception
# =====================================================================

class TestBridgeMetaTools(EmbodyTestCase):

    def _make_state(self, **overrides):
        """Construct a BridgeState (v2) for handler tests.

        Accepts the same kwargs the v1 dict-based factory did.  Fields not
        in BridgeState's constructor signature are applied via setattr after
        construction so the call sites in this file don't need to change.
        """
        # BridgeState requires url as a kwarg; the rest are optional.
        constructor_kwargs = {
            'url': overrides.pop('url', 'http://localhost:9870/mcp'),
        }
        for k in ('td_pid', 'config', 'config_path', 'active_name'):
            if k in overrides:
                constructor_kwargs[k] = overrides.pop(k)
        state = bridge.BridgeState(**constructor_kwargs)
        # Apply remaining attribute overrides directly
        for k, v in overrides.items():
            setattr(state, k, v)
        return state

    def test_get_td_status_disconnected(self):
        state = self._make_state()
        result = bridge.handle_get_td_status(state)
        self.assertFalse(result['connected'])
        self.assertFalse(result['td_process_alive'])
        self.assertFalse(result['crash_detected'])
        self.assertIsNone(result['last_connected'])

    def test_get_td_status_connected(self):
        state = self._make_state(connected=True, last_connected_time=time.time())
        result = bridge.handle_get_td_status(state)
        self.assertTrue(result['connected'])
        self.assertIsNotNone(result['last_connected'])

    def test_get_td_status_crash_detection(self):
        """Dead PID should trigger crash_detected."""
        state = self._make_state(td_pid=99999999)
        result = bridge.handle_get_td_status(state)
        self.assertTrue(result['crash_detected'])
        with state:
            self.assertTrue(state.crash_detected)  # Side-effect on state

    def test_get_td_status_restart_attempts(self):
        state = self._make_state()
        result = bridge.handle_get_td_status(state)
        self.assertEqual(result['restart_attempts_remaining'], bridge.CRASH_LOOP_MAX)

    def test_get_td_status_restart_attempts_depleted(self):
        now = time.monotonic()
        timestamps = [now - 10, now - 5, now - 1]
        state = self._make_state(launch_timestamps=timestamps)
        result = bridge.handle_get_td_status(state)
        self.assertEqual(result['restart_attempts_remaining'], 0)

    def test_launch_td_no_executable(self):
        # Mock find_td_pid -> None so the "already running" guard doesn't
        # short-circuit before we reach the missing-config check.  Without
        # this the test fails on any machine actually running TD (e.g. the
        # Embody dev project itself).
        with patch.object(bridge, 'find_td_pid', return_value=None):
            state = self._make_state(config={})
            result = bridge.handle_launch_td({}, state)
        self.assertEqual(result['status'], 'error')
        self.assertIn('envoy.json', result['message'])

    def test_launch_td_already_running(self):
        """Refuses to launch if THIS instance is already running.

        The guard is registry-based (v5.0.402): handle_launch_td resolves
        the target .toe basename and refuses if an instance registered under
        that name has a live td_pid. Seed the registry with this test
        process's own PID so is_process_alive() returns True deterministically.
        """
        state = self._make_state(config={
            'td_executable': '/usr/bin/td',
            'active': 'test',
            'instances': {
                'test': {'td_pid': os.getpid(), 'toe_path': 'test.toe'},
            },
        })
        # The seeded pid must read as a REAL TouchDesigner: inside TD the
        # runner's own pid genuinely is one, but under pytest (the A-51
        # dual-runner) it is python.exe -- mock the image check so the
        # test asserts the guard, not the identity of the test runner.
        with patch.object(bridge, 'is_td_process_alive', return_value=True):
            result = bridge.handle_launch_td({}, state)
        self.assertEqual(result['status'], 'error')
        self.assertIn('already running', result['message'])

    def test_launch_td_crash_loop_guard(self):
        """Refuses after too many recent launches."""
        now = time.monotonic()
        timestamps = [now - 10, now - 5, now - 1]
        state = self._make_state(
            launch_timestamps=timestamps,
            config={'td_executable': '/usr/bin/td', 'toe_path': 'test.toe'})
        with patch.object(bridge, 'find_td_pid', return_value=None):
            result = bridge.handle_launch_td({}, state)
        self.assertEqual(result['status'], 'error')
        self.assertIn('crashed', result['message'])

    def test_launch_td_missing_executable(self):
        """Error when TD executable doesn't exist."""
        state = self._make_state(
            config={'td_executable': '/nonexistent/TD.app', 'toe_path': 'test.toe'},
            config_path='/tmp/.embody/envoy.json')
        with patch.object(bridge, 'find_td_pid', return_value=None):
            result = bridge.handle_launch_td({}, state)
        self.assertEqual(result['status'], 'error')
        self.assertIn('not found', result['message'].lower())

    # --- quit_td ---

    def test_quit_td_none_pid(self):
        success, msg = bridge.quit_td(None)
        self.assertFalse(success)

    def test_quit_td_already_exited(self):
        success, msg = bridge.quit_td(99999999)
        self.assertTrue(success)
        self.assertIn('already exited', msg)

    # The quit_td tests inject clock/sleep (quit_td grew injectable params
    # for exactly this) and key their aliveness mocks by PID + state, never
    # by absolute call counts. Both lessons are from full-suite failures:
    # a global time.monotonic patch fed the in-process uvicorn threads,
    # which exhausted finite side_effect lists into StopIteration; and a
    # concurrent bridge caller consumed a call-count-keyed aliveness mock,
    # making quit_td see 'already exited' before it ever signaled.

    def test_quit_td_graceful_exit(self):
        """Graceful quit succeeds when the process exits promptly."""
        seen = {'n': 0}

        def mock_alive(pid):
            if pid != 12345:
                return False        # stray concurrent caller -- not ours
            seen['n'] += 1
            return seen['n'] <= 1   # alive at entry, dead after the request

        # os.kill stays patched: on macOS/Linux the graceful branch sends a
        # pid-scoped SIGTERM (a POSIX run would otherwise signal whatever
        # real process holds pid 12345).
        with patch.object(bridge, 'is_process_alive', side_effect=mock_alive), \
             patch('subprocess.run'), \
             patch('os.kill'):
            success, msg = bridge.quit_td(
                12345, clock=_slow_clock(100.0), sleep=lambda s: None,
                platform='win32')
        self.assertTrue(success)
        self.assertIn('gracefully', msg)

    def test_quit_td_force_kill(self):
        """Force kill when the graceful quit times out."""
        state = {'killed': False}

        def mock_alive(pid):
            if pid != 12345:
                return False
            return not state['killed']   # alive until the FORCE action

        def fake_run(args, **_kw):
            # taskkill /F is the Windows force path; plain taskkill is the
            # graceful request and must not count as the kill.
            if '/F' in args:
                state['killed'] = True
            return MagicMock()

        import signal

        def fake_kill(_pid, sig):
            if sig == signal.SIGKILL:    # POSIX force path; SIGTERM is not
                state['killed'] = True

        # Fast clock (step 5.0): the 15s deadline is exceeded within a few
        # loop iterations; the loop exit is time-driven by the INJECTED
        # clock, which no other thread can touch.
        with patch.object(bridge, 'is_process_alive', side_effect=mock_alive), \
             patch('subprocess.run', side_effect=fake_run), \
             patch('os.kill', side_effect=fake_kill):
            success, msg = bridge.quit_td(
                12345, graceful_timeout=15,
                clock=_slow_clock(100.0, step=5.0), sleep=lambda s: None,
                platform='win32')
        self.assertTrue(success)
        self.assertIn('force-killed', msg)

    # --- restart_td ---

    def test_restart_td_not_running(self):
        """Error when the ACTIVE instance is not running."""
        with patch.object(bridge, 'load_config', return_value={}), \
             patch.object(bridge, 'find_td_pid', return_value=None), \
             patch.object(bridge, 'is_td_process_alive', return_value=False):
            state = self._make_state()
            result = bridge.handle_restart_td({}, state)
        self.assertEqual(result['status'], 'error')
        self.assertIn('not running', result['message'])

    def test_restart_td_ignores_other_instances(self):
        """A dead ACTIVE instance must refuse even while OTHER TD processes
        run -- the old find_td_pid() path would have quit one of them."""
        registry = {'active': 'proj', 'instances': {
            'proj': {'port': 9870, 'td_pid': 111, 'toe_path': 'p.toe'}}}
        quit_calls = []
        with patch.object(bridge, 'load_config', return_value=registry), \
             patch.object(bridge, 'is_td_process_alive', return_value=False), \
             patch.object(bridge, 'find_td_pid', return_value=999), \
             patch.object(bridge, 'quit_td',
                          side_effect=lambda p: quit_calls.append(p)):
            state = self._make_state()
            result = bridge.handle_restart_td({}, state)
        self.assertEqual(result['status'], 'error')
        self.assertEqual(quit_calls, [],
                         'must never quit an unrelated TD process')

    def test_restart_td_quit_fails(self):
        """Error when TD cannot be terminated."""
        registry = {'active': 'proj', 'instances': {
            'proj': {'port': 9870, 'td_pid': 12345, 'toe_path': 'p.toe'}}}
        with patch.object(bridge, 'load_config', return_value=registry), \
             patch.object(bridge, 'is_td_process_alive', return_value=True), \
             patch.object(bridge, 'quit_td',
                          return_value=(False, 'Could not terminate')):
            state = self._make_state(td_pid=12345)
            result = bridge.handle_restart_td({}, state)
        self.assertEqual(result['status'], 'error')
        self.assertIn('Could not terminate', result['message'])

    def test_restart_td_success(self):
        """Full restart: quit then launch, Envoy reachable."""
        registry = {'active': 'proj', 'instances': {
            'proj': {'port': 9870, 'td_pid': 12345, 'toe_path': 't.toe'}}}
        with patch.object(bridge, 'load_config', return_value=registry), \
             patch.object(bridge, 'is_td_process_alive', return_value=True), \
             patch.object(bridge, 'quit_td',
                          return_value=(True, 'Exited gracefully')), \
             patch.object(bridge, 'launch_td',
                          return_value=(True, 'Launched', 67890)), \
             patch.object(bridge, 'wait_for_envoy', return_value=True), \
             patch('time.monotonic', return_value=100), \
             patch('time.time', return_value=1000):
            state = self._make_state(
                td_pid=12345, connected=True,
                config={'td_executable': '/td', 'toe_path': 't.toe'})
            result = bridge.handle_restart_td({}, state)
        self.assertEqual(result['status'], 'success')
        self.assertIn('67890', result['message'])
        with state:
            self.assertTrue(state.connected)
            self.assertEqual(state.td_pid, 67890)

    def test_restart_td_clears_state(self):
        """Restart clears connection state before relaunch."""
        registry = {'active': 'proj', 'instances': {
            'proj': {'port': 9870, 'td_pid': 12345, 'toe_path': 't.toe'}}}
        with patch.object(bridge, 'load_config', return_value=registry), \
             patch.object(bridge, 'is_td_process_alive', return_value=True), \
             patch.object(bridge, 'quit_td',
                          return_value=(True, 'Exited')), \
             patch.object(bridge, 'launch_td',
                          return_value=(True, 'Launched', 67890)), \
             patch.object(bridge, 'wait_for_envoy', return_value=False), \
             patch('time.monotonic', return_value=100):
            state = self._make_state(
                td_pid=12345, connected=True, crash_detected=True,
                config={'td_executable': '/td', 'toe_path': 't.toe'})
            result = bridge.handle_restart_td({}, state)
        self.assertEqual(result['status'], 'partial')
        with state:
            self.assertFalse(state.crash_detected)

    # --- dispatch ---

    def test_handle_bridge_tool_dispatch(self):
        state = self._make_state()
        content = bridge.handle_bridge_tool('get_td_status', {}, state)
        self.assertIsInstance(content, list)
        self.assertEqual(content[0]['type'], 'text')
        parsed = json.loads(content[0]['text'])
        self.assertIn('connected', parsed)

    def test_handle_bridge_tool_restart_dispatch(self):
        """restart_td is dispatched through handle_bridge_tool."""
        with patch.object(bridge, 'load_config', return_value={}), \
             patch.object(bridge, 'find_td_pid', return_value=None), \
             patch.object(bridge, 'is_td_process_alive', return_value=False):
            state = self._make_state()
            content = bridge.handle_bridge_tool('restart_td', {}, state)
        parsed = json.loads(content[0]['text'])
        self.assertEqual(parsed['status'], 'error')  # Not running

    # --- instance-aware pid semantics (issue #57 follow-up) ---

    def test_is_td_process_alive_rejects_recycled_pid(self):
        """A live pid that is NOT a TouchDesigner image must read as dead."""
        with patch.object(bridge, 'is_process_alive', return_value=True), \
             patch.object(bridge, 'find_all_td_pids', return_value=[42, 43]):
            self.assertFalse(bridge.is_td_process_alive(999))
            self.assertTrue(bridge.is_td_process_alive(42))
        with patch.object(bridge, 'is_process_alive', return_value=False):
            self.assertFalse(bridge.is_td_process_alive(42))

    def test_resolve_from_registry_dead_active_pid_returns_none(self):
        """A dead ACTIVE-instance pid resolves to None -- never adopt some
        other TD process (the multi-instance false-alive bug)."""
        registry = {'active': 'proj', 'instances': {
            'proj': {'port': 9876, 'td_pid': 111}}}
        with patch.object(bridge, 'is_td_process_alive', return_value=False), \
             patch.object(bridge, 'find_td_pid', return_value=999):
            port, pid, active = bridge._resolve_from_registry(registry, 9870)
        self.assertEqual(port, 9876)
        self.assertIsNone(pid, 'must not adopt an unrelated TD pid')
        self.assertEqual(active, 'proj')

    def test_status_alive_when_connected_without_pid(self):
        """A reachable port proves the instance is alive even when no pid is
        known (registered pid dead, port re-answered after a revive)."""
        state = self._make_state(connected=True, td_pid=None)
        with patch.object(bridge, 'load_config', return_value={}):
            result = bridge.handle_get_td_status(state)
        self.assertTrue(result['td_process_alive'])
        self.assertFalse(result['crash_detected'])

    def test_find_all_td_pids_excludes_webrender_windows(self):
        """TouchDesignerWebRender helpers match the tasklist TouchDesigner*
        filter but are not TD instances -- they must be excluded so a
        recycled/helper pid can never satisfy a liveness check."""
        if sys.platform != 'win32':
            self.skipTest('Windows tasklist branch only')
        fake = MagicMock()
        fake.stdout = (
            '"TouchDesigner.exe","111","Console","1","1,000 K"\n'
            '"TouchDesignerWebRender.exe","222","Console","1","1,000 K"\n')
        with patch.object(bridge.subprocess, 'run', return_value=fake):
            pids = bridge.find_all_td_pids()
        self.assertIn(111, pids)
        self.assertNotIn(222, pids)

    def test_handle_bridge_tool_unknown(self):
        state = self._make_state()
        content = bridge.handle_bridge_tool('unknown_tool', {}, state)
        parsed = json.loads(content[0]['text'])
        self.assertIn('error', parsed)


# =====================================================================
# Tool List Augmentation
# =====================================================================

class TestBridgeToolListAugmentation(EmbodyTestCase):

    def test_augment_adds_bridge_tools(self):
        response = {
            'jsonrpc': '2.0',
            'id': 1,
            'result': {'tools': [{'name': 'create_op'}]}
        }
        bridge.augment_tools_list(response)
        names = {t['name'] for t in response['result']['tools']}
        self.assertIn('create_op', names)
        self.assertIn('get_td_status', names)
        self.assertIn('launch_td', names)

    def test_augment_tools_list_is_idempotent(self):
        response = {
            'jsonrpc': '2.0',
            'id': 1,
            'result': {'tools': [{'name': 'create_op'}]}
        }

        bridge.augment_tools_list(response)
        bridge.augment_tools_list(response)

        names = [t['name'] for t in response['result']['tools']]
        self.assertEqual(names.count('get_td_status'), 1)
        self.assertEqual(names.count('launch_td'), 1)

    def test_augment_no_result_key(self):
        response = {'jsonrpc': '2.0', 'id': 1, 'error': {'code': -1}}
        bridge.augment_tools_list(response)  # Should not crash
        self.assertNotIn('result', response)

    def test_bridge_only_tools_list(self):
        response = bridge.bridge_only_tools_list(42)
        self.assertEqual(response['id'], 42)
        names = {t['name'] for t in response['result']['tools']}
        self.assertIn('get_td_status', names)
        self.assertIn('launch_td', names)

    def test_tools_list_augmented_in_main_loop(self):
        """tools/list response from TD gets bridge tools appended."""
        td_tools = {'jsonrpc': '2.0', 'id': 1,
                     'result': {'tools': [{'name': 'create_op'}]}}

        def forward(url, msg, **kw):
            return td_tools

        msg = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}
        stdin = io.StringIO(json.dumps(msg) + '\n')
        stdout = io.StringIO()
        stderr = io.StringIO()

        with patch.object(sys, 'stdin', stdin), \
             patch.object(sys, 'stdout', stdout), \
             patch.object(sys, 'stderr', stderr), \
             patch.object(sys, 'argv', ['envoy_bridge.py']), \
             patch.object(bridge, 'wait_for_envoy', return_value=True), \
             patch.object(bridge, 'forward_to_http', side_effect=forward), \
             patch.object(bridge, 'find_td_pid', return_value=None), \
             patch.object(bridge, 'kill_stale_bridges'), \
             patch('time.sleep'):
            bridge.main()

        lines = [l for l in stdout.getvalue().strip().split('\n') if l.strip()]
        response = json.loads(lines[0])
        names = {t['name'] for t in response['result']['tools']}
        self.assertIn('create_op', names)
        self.assertIn('get_td_status', names)
        self.assertIn('launch_td', names)

    def test_tools_list_bridge_only_when_td_down(self):
        """When TD is down, tools/list returns bridge-only tools."""
        msg = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}
        stdin = io.StringIO(json.dumps(msg) + '\n')
        stdout = io.StringIO()
        stderr = io.StringIO()

        with patch.object(sys, 'stdin', stdin), \
             patch.object(sys, 'stdout', stdout), \
             patch.object(sys, 'stderr', stderr), \
             patch.object(sys, 'argv', ['envoy_bridge.py']), \
             patch.object(bridge, 'wait_for_envoy', return_value=False), \
             patch.object(bridge, 'find_td_pid', return_value=None), \
             patch.object(bridge, 'kill_stale_bridges'), \
             patch('time.sleep'):
            bridge.main()

        lines = [l for l in stdout.getvalue().strip().split('\n') if l.strip()]
        response = json.loads(lines[0])
        names = {t['name'] for t in response['result']['tools']}
        self.assertIn('get_td_status', names)
        self.assertIn('launch_td', names)
        # TD tools should NOT be present
        self.assertNotIn('create_op', names)

    def test_meta_tool_call_intercepted(self):
        """tools/call for get_td_status is handled locally, not forwarded."""
        msg = {
            'jsonrpc': '2.0', 'id': 1,
            'method': 'tools/call',
            'params': {'name': 'get_td_status', 'arguments': {}}
        }
        stdin = io.StringIO(json.dumps(msg) + '\n')
        stdout = io.StringIO()
        stderr = io.StringIO()
        fwd = MagicMock()

        with patch.object(sys, 'stdin', stdin), \
             patch.object(sys, 'stdout', stdout), \
             patch.object(sys, 'stderr', stderr), \
             patch.object(sys, 'argv', ['envoy_bridge.py']), \
             patch.object(bridge, 'wait_for_envoy', return_value=True), \
             patch.object(bridge, 'forward_to_http', fwd), \
             patch.object(bridge, 'find_td_pid', return_value=None), \
             patch.object(bridge, 'kill_stale_bridges'), \
             patch('time.sleep'):
            bridge.main()

        # Should NOT have forwarded to TD
        fwd.assert_not_called()

        lines = [l for l in stdout.getvalue().strip().split('\n') if l.strip()]
        response = json.loads(lines[0])
        # Should have result with content array
        self.assertIn('result', response)
        content = response['result']['content']
        self.assertIsInstance(content, list)
        parsed = json.loads(content[0]['text'])
        self.assertIn('connected', parsed)

    def test_non_meta_tool_forwarded(self):
        """tools/call for create_op is forwarded to TD, not intercepted."""
        msg = {
            'jsonrpc': '2.0', 'id': 1,
            'method': 'tools/call',
            'params': {'name': 'create_op', 'arguments': {}}
        }

        def forward(url, msg, **kw):
            return {'jsonrpc': '2.0', 'id': 1, 'result': {'content': [{'type': 'text', 'text': 'ok'}]}}

        stdin = io.StringIO(json.dumps(msg) + '\n')
        stdout = io.StringIO()
        stderr = io.StringIO()

        with patch.object(sys, 'stdin', stdin), \
             patch.object(sys, 'stdout', stdout), \
             patch.object(sys, 'stderr', stderr), \
             patch.object(sys, 'argv', ['envoy_bridge.py']), \
             patch.object(bridge, 'wait_for_envoy', return_value=True), \
             patch.object(bridge, 'forward_to_http', side_effect=forward), \
             patch.object(bridge, 'find_td_pid', return_value=None), \
             patch.object(bridge, 'kill_stale_bridges'), \
             patch('time.sleep'):
            bridge.main()

        lines = [l for l in stdout.getvalue().strip().split('\n') if l.strip()]
        response = json.loads(lines[0])
        self.assertEqual(response['result']['content'][0]['text'], 'ok')


# =====================================================================
# Connection Loss Messages
# =====================================================================

class TestBridgeConnectionLostMessage(EmbodyTestCase):

    def _state(self, td_pid):
        return bridge.BridgeState(url='http://localhost:9870/mcp', td_pid=td_pid)

    def test_message_no_pid(self):
        state = self._state(None)
        msg = bridge.connection_lost_message(state)
        self.assertIn('connection lost', msg.lower())
        self.assertIn('launch_td', msg)

    def test_message_dead_pid(self):
        state = self._state(99999999)
        msg = bridge.connection_lost_message(state)
        self.assertIn('crashed', msg.lower())
        with state:
            self.assertTrue(state.crash_detected)

    def test_message_alive_pid(self):
        state = self._state(os.getpid())
        # Runner-agnostic (A-51): inside TD this process IS a live TD;
        # under pytest it is python.exe -- mock the TD-image check so the
        # message logic is what gets asserted, not the runner's identity.
        with patch.object(bridge, 'is_td_process_alive', return_value=True):
            msg = bridge.connection_lost_message(state)
        self.assertIn('not responding', msg.lower())
        self.assertIn(str(os.getpid()), msg)


# =====================================================================
# crash_detected lifecycle -- TRANSITIONS, not just states
# =====================================================================

class TestBridgeCrashFlagLifecycle(EmbodyTestCase):
    """The existing crash tests assert two STATES (dead pid -> flag set;
    restart_td -> flag cleared). The v6.0.157 field report hit a
    TRANSITION neither covered: the flag was set, then TD was relaunched
    OUTSIDE the bridge and the reconciler reattached.

    Before the fix that was worse than stale -- with state.td_pid still
    naming the dead process, reconcile() re-asserted crash_detected on
    every heartbeat tick, so get_td_status reported a crash forever while
    TD was alive, reachable and healthy.
    """

    REGISTRY = {'active': 'proj', 'instances': {
        'proj': {'port': 9870, 'td_pid': 4242, 'toe_path': 't.toe'}}}

    def _state(self):
        return bridge.BridgeState(
            url='http://127.0.0.1:9870/mcp', td_pid=1234,
            config_path='/tmp/envoy.json')

    def test_reconnect_clears_a_stale_crash_flag(self):
        state = self._state()
        with state:
            state.crash_detected = True
            state.connected = False
        # Old pid dead, the relaunched instance's registered pid alive --
        # the only shape in which the flag SHOULD clear. (Mocking every
        # pid dead would mean we cannot attribute the live socket to our
        # own instance, and the flag deliberately survives that.)
        with patch.object(bridge, 'ping_backend_mcp', return_value=True), \
             patch.object(bridge, 'is_td_process_alive',
                          side_effect=lambda pid: pid == 4242), \
             patch.object(bridge, 'load_config', return_value=self.REGISTRY), \
             patch.object(bridge, 'find_all_td_pids', return_value=[]), \
             patch.object(bridge, 'fetch_tools_list', return_value=None), \
             patch.object(bridge, 'save_tools_cache'):
            bridge.reconcile(state, None, heartbeat=True)
        with state:
            self.assertFalse(
                state.crash_detected,
                'a successful reconnect must clear the crash flag')

    def test_reconnect_reresolves_the_tracked_pid(self):
        """The dead pid must be replaced from the registry, or the flag
        would simply be re-asserted on the next tick."""
        state = self._state()
        # The real scenario: the pid we track (1234) is gone, the pid the
        # relaunched instance registered (4242) is alive.
        with patch.object(bridge, 'ping_backend_mcp', return_value=True), \
             patch.object(bridge, 'is_td_process_alive',
                          side_effect=lambda pid: pid == 4242), \
             patch.object(bridge, 'load_config', return_value=self.REGISTRY), \
             patch.object(bridge, 'find_all_td_pids', return_value=[]), \
             patch.object(bridge, 'fetch_tools_list', return_value=None), \
             patch.object(bridge, 'save_tools_cache'):
            bridge.reconcile(state, None, heartbeat=True)
        with state:
            self.assertEqual(
                4242, state.td_pid,
                'td_pid must be re-resolved from the registry, not left '
                'pointing at the dead process')

    TWO_INSTANCE_REGISTRY = {'active': 'projB', 'instances': {
        'projA': {'port': 9870, 'td_pid': 7001, 'toe_path': 'a.toe'},
        'projB': {'port': 9871, 'td_pid': 8002, 'toe_path': 'b.toe'}}}

    def test_stale_pid_never_adopts_a_foreign_instance(self):
        """REGRESSION: re-resolving without the pin bound this session's
        tracked pid to a DIFFERENT project's TD. is_td_process_alive would
        then read True for a stranger and suppress genuine crash detection
        for the pinned instance forever."""
        state = bridge.BridgeState(
            url='http://127.0.0.1:9870/mcp', td_pid=1234,
            config_path='/tmp/envoy.json')
        with state:
            state.pinned_instance = 'projA'
        # Our pinned instance (projA, pid 7001) is ALSO dead; the
        # registry's global default projB (pid 8002) is alive.
        with patch.object(bridge, 'ping_backend_mcp', return_value=True), \
             patch.object(bridge, 'is_td_process_alive',
                          side_effect=lambda pid: pid == 8002), \
             patch.object(bridge, 'load_config',
                          return_value=self.TWO_INSTANCE_REGISTRY), \
             patch.object(bridge, 'find_all_td_pids', return_value=[]), \
             patch.object(bridge, 'fetch_tools_list', return_value=None), \
             patch.object(bridge, 'save_tools_cache'):
            bridge.reconcile(state, None, heartbeat=True)
        with state:
            self.assertNotEqual(
                8002, state.td_pid,
                "must never adopt the global 'active' instance's pid while "
                "pinned to another instance")
            self.assertEqual(1234, state.td_pid)

    def test_foreign_instance_on_port_does_not_clear_crash_flag(self):
        """A stranger answering our port must not erase a real crash."""
        state = bridge.BridgeState(
            url='http://127.0.0.1:9870/mcp', td_pid=1234,
            config_path='/tmp/envoy.json')
        with state:
            state.pinned_instance = 'projA'
            state.crash_detected = True
        with patch.object(bridge, 'ping_backend_mcp', return_value=True), \
             patch.object(bridge, 'is_td_process_alive',
                          side_effect=lambda pid: pid == 8002), \
             patch.object(bridge, 'load_config',
                          return_value=self.TWO_INSTANCE_REGISTRY), \
             patch.object(bridge, 'find_all_td_pids', return_value=[]), \
             patch.object(bridge, 'fetch_tools_list', return_value=None), \
             patch.object(bridge, 'save_tools_cache'):
            bridge.reconcile(state, None, heartbeat=True)
        with state:
            self.assertTrue(
                state.crash_detected,
                'our TD is still dead -- a foreign instance holding the '
                'port must not clear the flag')

    def test_crash_detected_even_when_something_answers_the_port(self):
        """Our TD dying IS a crash even if the port still answers.

        Envoy's port scanner hands a just-freed port to the next instance,
        so a sibling can be answering within one heartbeat. An earlier fix
        chained this branch to `is_up` as an elif, which meant the crash
        was never recorded in exactly that race.
        """
        state = bridge.BridgeState(
            url='http://127.0.0.1:9870/mcp', td_pid=1234,
            config_path='/tmp/envoy.json')
        with state:
            state.pinned_instance = 'projA'
            state.crash_detected = False
        # Ping succeeds (a stranger holds the port); OUR pid is dead and
        # our pinned instance is not recoverable.
        with patch.object(bridge, 'ping_backend_mcp', return_value=True), \
             patch.object(bridge, 'is_td_process_alive',
                          side_effect=lambda pid: pid == 8002), \
             patch.object(bridge, 'load_config',
                          return_value=self.TWO_INSTANCE_REGISTRY), \
             patch.object(bridge, 'find_all_td_pids', return_value=[]), \
             patch.object(bridge, 'fetch_tools_list', return_value=None), \
             patch.object(bridge, 'save_tools_cache'):
            bridge.reconcile(state, None, heartbeat=True)
        with state:
            self.assertTrue(
                state.crash_detected,
                'a dead tracked pid must register as a crash even while '
                'the port answers')

    def test_pinned_instance_recovery_is_exercised(self):
        """The pinned-POSITIVE recovery path.

        Both 'reconnect' tests leave pinned_instance unset, so they take
        the `not pinned` escape hatch and never exercise `_rname == pinned`
        -- which is the production path, since Phase 1 always adopts a pin.
        Without this test, removing `pin=pinned` from the resolve call
        leaves the whole suite green while real recovery regresses.
        """
        registry = {'active': 'projB', 'instances': {
            'projA': {'port': 9870, 'td_pid': 7003, 'toe_path': 'a.toe'},
            'projB': {'port': 9871, 'td_pid': 8002, 'toe_path': 'b.toe'}}}
        state = bridge.BridgeState(
            url='http://127.0.0.1:9870/mcp', td_pid=1234,
            config_path='/tmp/envoy.json')
        with state:
            state.pinned_instance = 'projA'
            state.crash_detected = True
        # projA (our pin) relaunched as 7003 and is alive; the registry
        # default names projB. We must follow OUR pin, not the default.
        with patch.object(bridge, 'ping_backend_mcp', return_value=True), \
             patch.object(bridge, 'is_td_process_alive',
                          side_effect=lambda pid: pid in (7003, 8002)), \
             patch.object(bridge, 'load_config', return_value=registry), \
             patch.object(bridge, 'find_all_td_pids', return_value=[]), \
             patch.object(bridge, 'fetch_tools_list', return_value=None), \
             patch.object(bridge, 'save_tools_cache'):
            bridge.reconcile(state, None, heartbeat=True)
        with state:
            self.assertEqual(
                7003, state.td_pid,
                'must recover OUR pinned instance, not the registry default')
            self.assertFalse(
                state.crash_detected,
                'recovering our own pin clears the crash flag')

    def test_gc_dropped_pin_does_not_adopt_the_default_instance(self):
        """The REAL post-crash shape, and the one the name guard exists for.

        The registry garbage-collects dead-pid rows on every write by any
        peer TD, so after our instance crashes its row is GONE. Resolution
        then falls through to the registry default -- a different, live
        project. Neither its URL nor its pid may become our crash signal.
        """
        registry = {'active': 'projB', 'instances': {
            'projB': {'port': 9871, 'td_pid': 8002, 'toe_path': 'b.toe'}}}
        state = bridge.BridgeState(
            url='http://127.0.0.1:9870/mcp', td_pid=1234,
            config_path='/tmp/envoy.json')
        with state:
            state.pinned_instance = 'projA'      # GC'd out of the registry
            state.crash_detected = False
        with patch.object(bridge, 'ping_backend_mcp', return_value=True), \
             patch.object(bridge, 'is_td_process_alive',
                          side_effect=lambda pid: pid == 8002), \
             patch.object(bridge, 'load_config', return_value=registry), \
             patch.object(bridge, 'find_all_td_pids', return_value=[]), \
             patch.object(bridge, 'fetch_tools_list', return_value=None), \
             patch.object(bridge, 'save_tools_cache'):
            bridge.reconcile(state, None, heartbeat=True)
        with state:
            self.assertNotEqual(
                8002, state.td_pid,
                "must not adopt the default instance's pid when our pinned "
                'instance has been GC-ed out of the registry')
            self.assertTrue(
                state.crash_detected,
                'our TD is dead -- a live foreign instance answering the '
                'port must not mask that')

    def test_phase1_config_reload_does_not_adopt_a_foreign_pid(self):
        """PHASE 1 (the config-mtime reload) must not adopt a foreign pid.

        Phase 1 only runs when a real config file's mtime differs from
        state.config_mtime, so it needs a file on disk -- mocking
        load_config alone never reaches it, which is why the Phase 1 gate
        was untested when it was written. With our pin GC-ed out of the
        registry the bridge deliberately follows the default's URL, but
        taking its PID too would point crash detection at another
        project's TouchDesigner.
        """
        import shutil
        import tempfile
        registry = {'active': 'projB', 'instances': {
            'projB': {'port': 9871, 'td_pid': 8002, 'toe_path': 'b.toe'}}}
        tmpdir = tempfile.mkdtemp()
        tmp = os.path.join(tmpdir, 'envoy.json')
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump(registry, fh)
        try:
            state = bridge.BridgeState(
                url='http://127.0.0.1:9870/mcp', td_pid=1234,
                config_path=tmp)
            with state:
                state.pinned_instance = 'projA'   # GC'd from the registry
                state.config_mtime = 0            # force the Phase 1 reload
                state.crash_detected = False
            with patch.object(bridge, 'ping_backend_mcp', return_value=True), \
                 patch.object(bridge, 'is_td_process_alive',
                              side_effect=lambda pid: pid == 8002), \
                 patch.object(bridge, 'find_all_td_pids', return_value=[]), \
                 patch.object(bridge, 'fetch_tools_list', return_value=None), \
                 patch.object(bridge, 'save_tools_cache'):
                bridge.reconcile(state, None, heartbeat=True)
            with state:
                self.assertNotEqual(
                    8002, state.td_pid,
                    'Phase 1 must not adopt the default instance pid while '
                    'pinned elsewhere')
                self.assertTrue(
                    state.crash_detected,
                    'our TD is dead; following a foreign URL must not clear '
                    'the crash flag')
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_renamed_instance_on_same_port_is_still_ours(self):
        """A version bump renames the .toe, so the pin goes stale on the
        next open (Embody-6.157 -> Embody-6.159). If the resolved instance
        answers on the port we are ALREADY using, it is our instance under
        a new name -- refusing its pid would degrade crash detection after
        every release. Observed live during the v6.0.159 smoke run.
        """
        import shutil
        import tempfile
        registry = {'active': 'Embody-6.159', 'instances': {
            'Embody-6.159': {'port': 9872, 'td_pid': 60844,
                             'toe_path': 'dev/Embody-6.159.toe'}}}
        tmpdir = tempfile.mkdtemp()
        tmp = os.path.join(tmpdir, 'envoy.json')
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump(registry, fh)
        try:
            state = bridge.BridgeState(
                url='http://127.0.0.1:9872/mcp', td_pid=None,
                config_path=tmp)
            with state:
                state.pinned_instance = 'Embody-6.157'   # stale after rename
                state.config_mtime = 0
            with patch.object(bridge, 'ping_backend_mcp', return_value=True), \
                 patch.object(bridge, 'is_td_process_alive',
                              side_effect=lambda pid: pid == 60844), \
                 patch.object(bridge, 'find_all_td_pids', return_value=[]), \
                 patch.object(bridge, 'fetch_tools_list', return_value=None), \
                 patch.object(bridge, 'save_tools_cache'):
                bridge.reconcile(state, None, heartbeat=True)
            with state:
                self.assertEqual(
                    60844, state.td_pid,
                    'same port => same instance, just renamed by the '
                    'release bump; its pid must be adopted')
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_no_pin_never_adopts_an_unidentified_pid(self):
        """With no pin and no registry, _resolve_from_registry falls back
        to find_td_pid() -- an arbitrary TD on the machine. That must never
        become our tracked pid, per that function's own refusal."""
        state = bridge.BridgeState(
            url='http://127.0.0.1:9870/mcp', td_pid=1234,
            config_path='/tmp/envoy.json')
        with state:
            state.crash_detected = True
        with patch.object(bridge, 'ping_backend_mcp', return_value=True), \
             patch.object(bridge, 'is_td_process_alive',
                          side_effect=lambda pid: pid == 5555), \
             patch.object(bridge, 'load_config', return_value={}), \
             patch.object(bridge, 'find_td_pid', return_value=5555), \
             patch.object(bridge, 'find_all_td_pids', return_value=[]), \
             patch.object(bridge, 'fetch_tools_list', return_value=None), \
             patch.object(bridge, 'save_tools_cache'):
            bridge.reconcile(state, None, heartbeat=True)
        with state:
            self.assertNotEqual(
                5555, state.td_pid,
                'a blind find_td_pid() guess must never be adopted')
            self.assertTrue(
                state.crash_detected,
                'and the genuine crash must stand')

    def test_crash_still_detected_when_envoy_is_unreachable(self):
        """The fix must not suppress genuine crash detection."""
        state = self._state()
        with patch.object(bridge, 'ping_backend_mcp', return_value=False), \
             patch.object(bridge, 'is_td_process_alive', return_value=False), \
             patch.object(bridge, 'load_config', return_value=self.REGISTRY), \
             patch.object(bridge, 'find_all_td_pids', return_value=[]), \
             patch.object(bridge, 'fetch_tools_list', return_value=None), \
             patch.object(bridge, 'save_tools_cache'):
            bridge.reconcile(state, None, heartbeat=True)
        with state:
            self.assertTrue(
                state.crash_detected,
                'pid dead AND Envoy unreachable is a real crash')


# =====================================================================
# Bridge v2 - BridgeState, notify_stdout, reconciler, caching, hashing
# =====================================================================
#
# These tests cover the v2 upgrade described in
# /Users/rosco/.claude/plans/inherited-seeking-cosmos.md
# (items 1-6: BridgeState class, notify_stdout helper, listChanged=true,
# tool list caching, tool hash diff detection, reconciler thread).
#
# Every v2 symbol is probed at import time so the existing v1 tests
# keep running cleanly even before the parallel implementation lands.
# A test class using a missing symbol raises SkipTest in setUp via the
# _require_v2(...) helper.
# =====================================================================

import threading

# --- v2 symbol probing --------------------------------------------------
#
# Each feature probed independently so partial v2 landings still run
# whichever tests they support.

_V2_BRIDGE_STATE = hasattr(bridge, 'BridgeState')
_V2_NOTIFY_STDOUT = hasattr(bridge, 'notify_stdout')
_V2_HASH_TOOLS = hasattr(bridge, '_hash_tools') or hasattr(bridge, 'hash_tools')
_V2_RECONCILE = hasattr(bridge, 'reconcile')
_V2_LIST_CHANGED_TRUE = True  # checked at runtime from initialize response


def _get_hash_tools():
    """Return whichever hash-tools symbol exists (private or public)."""
    return getattr(bridge, '_hash_tools', None) or getattr(bridge, 'hash_tools', None)


def _require_v2(flag, feature):
    """Call from setUp to skip the whole class if a v2 symbol is missing."""
    if not flag:
        raise SkipTest(f'bridge v2 {feature} not yet implemented')


# =====================================================================
# Shared v2 fixtures
# =====================================================================

def _make_v2_state(**overrides):
    """
    Build a mock state object for reconciler tests.

    Prefers the real BridgeState if available; otherwise returns a
    SimpleNamespace-ish object supporting both attribute access and
    a context-manager protocol (for `with state:` lock scoping).
    """
    defaults = dict(
        connected=False,
        td_pid=None,
        url='http://localhost:9870/mcp',
        config={},
        config_path=None,
        config_mtime=0,
        cached_tools=None,
        cached_tools_hash=None,
        last_heartbeat_ok=0,
        last_connected_time=None,
        launch_timestamps=[],
        crash_detected=False,
        active_name=None,
        known_td_pids=set(),
    )
    defaults.update(overrides)

    if _V2_BRIDGE_STATE:
        # BridgeState's __init__ only accepts a small set of constructor
        # kwargs (url, td_pid, config, config_path, active_name). Apply
        # the rest via setattr after construction.
        BS = bridge.BridgeState
        ctor_keys = ('url', 'td_pid', 'config', 'config_path', 'active_name')
        ctor_kwargs = {k: defaults[k] for k in ctor_keys if k in defaults}
        try:
            inst = BS(**ctor_kwargs)
        except TypeError:
            # Implementation may differ - try constructing with just url.
            inst = BS(url=defaults.get('url', 'http://localhost:9870/mcp'))
        for k, v in defaults.items():
            if k in ctor_keys:
                continue
            try:
                setattr(inst, k, v)
            except Exception:
                pass
        return inst

    # v1 fallback - plain object with lock-compatible context manager
    class _FauxState:
        def __init__(self, d):
            self.__dict__.update(d)
            self._lock = threading.RLock()

        def __enter__(self):
            self._lock.acquire()
            return self

        def __exit__(self, *a):
            self._lock.release()

    return _FauxState(defaults)


class _StdoutCapture:
    """Thread-safe stdout collector used by notify_stdout tests."""

    def __init__(self):
        self._buf = io.StringIO()
        self._lock = threading.Lock()

    def write(self, s):
        with self._lock:
            self._buf.write(s)

    def flush(self):
        pass

    def getvalue(self):
        with self._lock:
            return self._buf.getvalue()


# =====================================================================
# Test case 1 - BridgeState locking under contention
# =====================================================================

class TestBridgeStateLocking(EmbodyTestCase):
    """Two threads hammering a BridgeState counter produce no corruption."""

    def setUp(self):
        _require_v2(_V2_BRIDGE_STATE, 'BridgeState')

    def test_concurrent_increment_no_corruption(self):
        state = _make_v2_state()
        # Seed a counter field. BridgeState may not declare this attribute
        # by default - setattr should succeed either way.
        setattr(state, 'counter', 0)

        iterations = 10_000
        errors = []

        def hammer():
            try:
                for _ in range(iterations):
                    with state:
                        state.counter = state.counter + 1
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=hammer, name='hammer-1')
        t2 = threading.Thread(target=hammer, name='hammer-2')
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        self.assertFalse(t1.is_alive(), 'Thread 1 did not finish within 5s')
        self.assertFalse(t2.is_alive(), 'Thread 2 did not finish within 5s')
        self.assertEqual(
            errors, [],
            f'Threads raised: {[type(e).__name__ + ": " + str(e) for e in errors]}')
        self.assertEqual(
            state.counter, 2 * iterations,
            f'Expected {2 * iterations}, got {state.counter} - lock did not serialize writes')


# =====================================================================
# Test case 2 - Tool hash diff detection
# =====================================================================

class TestBridgeHashTools(EmbodyTestCase):
    """_hash_tools produces stable, order-independent, name/description-sensitive hashes."""

    def setUp(self):
        _require_v2(_V2_HASH_TOOLS, '_hash_tools')
        self.hash_tools = _get_hash_tools()

    def _tool(self, name, description=''):
        return {'name': name, 'description': description}

    def test_identical_lists_same_hash(self):
        a = [self._tool('create_op', 'Create an operator'),
             self._tool('delete_op', 'Delete an operator')]
        b = [self._tool('create_op', 'Create an operator'),
             self._tool('delete_op', 'Delete an operator')]
        self.assertEqual(self.hash_tools(a), self.hash_tools(b))

    def test_reordered_lists_same_hash(self):
        """Order must not affect the hash - plan says 'sort by name first'."""
        a = [self._tool('create_op', 'Create an operator'),
             self._tool('delete_op', 'Delete an operator'),
             self._tool('cook_op', 'Cook an operator')]
        b = [self._tool('cook_op', 'Cook an operator'),
             self._tool('create_op', 'Create an operator'),
             self._tool('delete_op', 'Delete an operator')]
        self.assertEqual(
            self.hash_tools(a), self.hash_tools(b),
            'Reordered tool lists must produce identical hashes')

    def test_added_tool_changes_hash(self):
        a = [self._tool('create_op', 'x')]
        b = [self._tool('create_op', 'x'),
             self._tool('delete_op', 'y')]
        self.assertNotEqual(self.hash_tools(a), self.hash_tools(b))

    def test_removed_tool_changes_hash(self):
        a = [self._tool('create_op', 'x'),
             self._tool('delete_op', 'y')]
        b = [self._tool('create_op', 'x')]
        self.assertNotEqual(self.hash_tools(a), self.hash_tools(b))

    def test_renamed_tool_changes_hash(self):
        a = [self._tool('create_op', 'x')]
        b = [self._tool('create_operator', 'x')]
        self.assertNotEqual(
            self.hash_tools(a), self.hash_tools(b),
            'Renamed tool must produce a different hash')

    def test_description_change_changes_hash(self):
        """Description is part of the hash per the plan - any change matters."""
        a = [self._tool('create_op', 'Create an operator')]
        b = [self._tool('create_op', 'Create an op')]
        self.assertNotEqual(self.hash_tools(a), self.hash_tools(b))

    def test_empty_list_produces_stable_hash(self):
        self.assertEqual(self.hash_tools([]), self.hash_tools([]))


# =====================================================================
# Test case 3 - Reconciler state transitions
# =====================================================================

class TestBridgeReconcilerTransitions(EmbodyTestCase):
    """
    Verify that reconcile() fires the on_tools_change callback only
    on connection transitions (False->True and True->False), never on
    steady-state ticks (True->True).

    Mock ping sequence: False, True, True, False.
    Expected notifications: 0, 1 (became connected), 1 (unchanged), 2 (became disconnected).
    """

    def setUp(self):
        _require_v2(_V2_RECONCILE, 'reconcile')
        _require_v2(_V2_BRIDGE_STATE, 'BridgeState')

    def test_fires_on_each_transition_exactly_once(self):
        state = _make_v2_state(
            connected=False,
            url='http://localhost:9870/mcp',
            config_path=None,  # disable phase-1 config reconciliation
        )

        ping_sequence = [False, True, True, False]
        ping_idx = [0]

        def fake_ping(url, timeout=2):
            i = ping_idx[0]
            ping_idx[0] = i + 1
            return ping_sequence[i]

        # Mock tool fetch to return a stable list - avoids hash-mismatch noise.
        # Using ONE stable list means on_tools_change fires only on transitions,
        # not on in-place tool-list changes.
        stable_tools = [{'name': 't1', 'description': 'd1'}]

        notify_count = [0]

        def on_tools_change():
            notify_count[0] += 1

        # Patch only the functions that actually exist on the module -
        # the reconciler may use any subset depending on implementation.
        patches_spec = {
            'ping_backend_mcp': fake_ping,
            'fetch_tools_list': lambda url, *a, **kw: stable_tools,
            'find_all_td_pids': lambda: [],
            'is_process_alive': lambda pid: False,
        }
        p_list = [
            patch.object(bridge, name, new=impl)
            for name, impl in patches_spec.items()
            if hasattr(bridge, name)
        ]
        # Also kill time.sleep just in case reconcile is called in a loop.
        p_list.append(patch('time.sleep'))

        for p in p_list:
            p.start()
        try:
            # Tick 1: ping=False. was_connected=False -> no transition. notify=0.
            bridge.reconcile(state, on_tools_change, heartbeat=True)
            # Tick 2: ping=True. was=False -> became_connected. notify=1.
            bridge.reconcile(state, on_tools_change, heartbeat=True)
            # Tick 3: ping=True. was=True -> no transition. notify=1.
            bridge.reconcile(state, on_tools_change, heartbeat=True)
            # Tick 4: ping=False. was=True -> became_disconnected. notify=2.
            bridge.reconcile(state, on_tools_change, heartbeat=True)
        finally:
            for p in p_list:
                p.stop()

        self.assertEqual(
            notify_count[0], 2,
            f'Expected exactly 2 transitions (connect + disconnect), got {notify_count[0]}')


# =====================================================================
# Test case 5 - listChanged: true in initialize response
# =====================================================================

class TestBridgeListChangedCapability(EmbodyTestCase):
    """The initialize response must declare capabilities.tools.listChanged = true."""

    def _run_initialize(self, wait_result):
        """Feed a single initialize message through main(), return parsed response.

        The v2 background reconciler is neutralized at module load (see top
        of file), so this test exercises only the main-thread initialize
        handler.
        """
        stdin = io.StringIO(json.dumps(
            {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize'}) + '\n')
        stdout = io.StringIO()
        stderr = io.StringIO()

        with patch.object(sys, 'stdin', stdin), \
             patch.object(sys, 'stdout', stdout), \
             patch.object(sys, 'stderr', stderr), \
             patch.object(sys, 'argv', ['envoy_bridge.py']), \
             patch.object(bridge, 'wait_for_envoy', return_value=wait_result), \
             patch.object(bridge, 'forward_to_http',
                          return_value={'jsonrpc': '2.0', 'id': 1,
                                        'result': {}}), \
             patch.object(bridge, 'find_td_pid', return_value=None), \
             patch.object(bridge, 'kill_stale_bridges'), \
             patch('time.sleep'):
            bridge.main()

        lines = [l for l in stdout.getvalue().strip().split('\n') if l.strip()]
        if not lines:
            return None
        return json.loads(lines[0])

    def test_list_changed_true_when_disconnected(self):
        """Bridge should advertise listChanged=true even when TD is down.

        v1 answers initialize locally when disconnected but hardcodes
        listChanged=False at line 1066 of envoy-bridge.py. v2 step 3
        flips this to True. This is the primary assertion for the
        listChanged capability change.
        """
        resp = self._run_initialize(wait_result=False)
        self.assertIsNotNone(resp, 'initialize must produce a response')
        self.assertIn('result', resp)
        caps = resp['result'].get('capabilities', {})
        tools_cap = caps.get('tools', {})
        self.assertEqual(
            tools_cap.get('listChanged'), True,
            f'capabilities.tools.listChanged must be True (got {tools_cap!r}). '
            'This is bridge v2 step 3 in the plan.')


# =====================================================================
# Test case 7 - tools/list cache hit within 5s
# =====================================================================

class TestBridgeToolsListCache(EmbodyTestCase):
    """Second tools/list within 5s returns cached response, no HTTP forward."""

    def setUp(self):
        # The cache feature is step 6 in the plan. If the implementation
        # doesn't yet have caching (no `cached_tools` attribute path),
        # skip rather than fail. We probe by running a quick introspection.
        if not _V2_BRIDGE_STATE:
            raise SkipTest('bridge v2 caching depends on BridgeState (step 1)')

    def test_second_tools_list_within_window_uses_cache(self):
        """
        Send two tools/list requests back-to-back. Only the first should
        forward to TD; the second should return the cached response.

        We count tools/list-specific forwards (ignoring any initialize or
        notification forwards) so implementation details around other
        methods don't affect the assertion.
        """
        td_tools_result = {
            'tools': [{'name': 'create_op', 'description': 'create'}],
        }

        tools_list_forward_count = [0]

        def forward_counter(url, msg, **kw):
            if msg.get('method') == 'tools/list':
                tools_list_forward_count[0] += 1
            return {'jsonrpc': '2.0', 'id': msg.get('id'),
                    'result': td_tools_result}

        msgs = [
            {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize'},
            {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
            {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/list'},
        ]
        stdin = io.StringIO('\n'.join(json.dumps(m) for m in msgs) + '\n')
        stdout = io.StringIO()
        stderr = io.StringIO()

        # The v2 background reconciler is neutralized at module load
        # (see top of file), so forward_to_http calls only come from the
        # main-thread tools/list path.
        # Freeze time.time so both requests land within the 5s cache window.
        # Also freeze time.monotonic in case the cache uses it.
        with patch.object(sys, 'stdin', stdin), \
             patch.object(sys, 'stdout', stdout), \
             patch.object(sys, 'stderr', stderr), \
             patch.object(sys, 'argv', ['envoy_bridge.py']), \
             patch.object(bridge, 'wait_for_envoy', return_value=True), \
             patch.object(bridge, 'forward_to_http', side_effect=forward_counter), \
             patch.object(bridge, 'find_td_pid', return_value=None), \
             patch.object(bridge, 'kill_stale_bridges'), \
             patch('time.sleep'), \
             patch('time.time', return_value=1000.0), \
             patch('time.monotonic', return_value=1000.0):
            bridge.main()

        # Sanity: both tools/list responses should have been written.
        lines = [l for l in stdout.getvalue().strip().split('\n') if l.strip()]
        responses = [json.loads(l) for l in lines]
        tools_list_responses = [
            r for r in responses
            if 'result' in r and isinstance(r['result'], dict)
            and 'tools' in r['result']
        ]
        self.assertGreaterEqual(
            len(tools_list_responses), 2,
            'Expected two tools/list responses (both should succeed)')

        for response in tools_list_responses:
            names = [t['name'] for t in response['result']['tools']]
            self.assertEqual(names.count('get_td_status'), 1)
            self.assertEqual(names.count('launch_td'), 1)

        self.assertEqual(
            tools_list_forward_count[0], 1,
            f'Expected exactly 1 tools/list forward (second should hit cache), '
            f'got {tools_list_forward_count[0]}. '
            'This is bridge v2 step 6 in the plan.')


# =====================================================================
# Test case 9 - Stdout serialization under concurrent writers
# =====================================================================

class TestBridgeStdoutSerialization(EmbodyTestCase):
    """
    10 threads x 100 concurrent calls to notify_stdout must produce
    only valid newline-delimited JSON (no interleaved bytes).
    """

    def setUp(self):
        _require_v2(_V2_NOTIFY_STDOUT, 'notify_stdout')

    def test_concurrent_notify_stdout_produces_valid_jsonl(self):
        capture = _StdoutCapture()
        errors = []

        def hammer(thread_id):
            try:
                for i in range(100):
                    # Mix methods + params so every line is a distinct object.
                    bridge.notify_stdout(
                        'notifications/tools/list_changed',
                        params={'thread': thread_id, 'seq': i},
                    )
            except TypeError:
                # If notify_stdout doesn't accept params, retry without.
                try:
                    for i in range(100):
                        bridge.notify_stdout('notifications/tools/list_changed')
                except Exception as e:
                    errors.append(e)
            except Exception as e:
                errors.append(e)

        with patch.object(sys, 'stdout', capture):
            threads = [
                threading.Thread(target=hammer, args=(tid,), name=f'notif-{tid}')
                for tid in range(10)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=5)
                self.assertFalse(t.is_alive(), f'{t.name} did not finish')

        self.assertEqual(
            errors, [],
            f'Worker threads raised: {[type(e).__name__ + ": " + str(e) for e in errors]}')

        raw = capture.getvalue()
        lines = [l for l in raw.split('\n') if l.strip()]

        # Expect exactly 10 x 100 = 1000 lines (unless notify_stdout was
        # called with fewer args due to TypeError fallback - still should
        # be 1000).
        self.assertEqual(
            len(lines), 1000,
            f'Expected 1000 notification lines, got {len(lines)}')

        # The critical assertion: every line parses as valid JSON.
        bad_lines = []
        for idx, line in enumerate(lines):
            try:
                obj = json.loads(line)
                # Must be a notification - no id, has method
                if not isinstance(obj, dict) or 'method' not in obj:
                    bad_lines.append((idx, f'not a notification: {line[:80]}'))
                if obj.get('jsonrpc') != '2.0':
                    bad_lines.append((idx, f'missing jsonrpc=2.0: {line[:80]}'))
            except json.JSONDecodeError as e:
                bad_lines.append((idx, f'invalid JSON: {e}: {line[:80]}'))

        self.assertEqual(
            bad_lines, [],
            f'{len(bad_lines)} corrupt lines (lock not serializing stdout writes): '
            f'{bad_lines[:5]}')




# =====================================================================
# Per-bridge instance pinning (2026-07-25 redesign)
# =====================================================================

class TestBridgeInstancePinning(EmbodyTestCase):
    """A bridge follows its own pinned instance, not the global 'active'.

    Regression tests for the 2026-07-25 incident: a freshly-registering
    instance flipped the registry 'active' and yanked every live
    session's bridge to it (once to a dead port). With pinning, only an
    explicit all-sessions switch (active_epoch bump) may re-target a
    pinned bridge.
    """

    REG = {
        'active': 'A',
        'instances': {
            'A': {'toe_path': 'dev/A.toe', 'port': 9870, 'td_pid': 111},
            'B': {'toe_path': 'dev/B.toe', 'port': 9871, 'td_pid': 222},
        },
    }

    def _write_registry(self, data):
        import json as _json
        import tempfile
        fd, path = tempfile.mkstemp(suffix='.json', prefix='envoy_pin_')
        os.close(fd)
        with open(path, 'w', encoding='utf-8') as f:
            _json.dump(data, f)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        return path

    # -- _resolve_from_registry ---------------------------------------

    def test_pin_beats_active(self):
        with patch.object(bridge, 'is_td_process_alive', return_value=True):
            port, pid, name = bridge._resolve_from_registry(
                dict(self.REG), None, pin='B')
        self.assertEqual(name, 'B')
        self.assertEqual(port, 9871)
        self.assertEqual(pid, 222)

    def test_no_pin_follows_active(self):
        with patch.object(bridge, 'is_td_process_alive', return_value=True):
            port, pid, name = bridge._resolve_from_registry(
                dict(self.REG), None)
        self.assertEqual(name, 'A')
        self.assertEqual(port, 9870)

    def test_vanished_pin_falls_back_to_active(self):
        with patch.object(bridge, 'is_td_process_alive', return_value=True):
            port, pid, name = bridge._resolve_from_registry(
                dict(self.REG), None, pin='GONE')
        self.assertEqual(name, 'A')

    # -- reconcile Phase 1 --------------------------------------------

    def _reconcile_once(self, state):
        with patch.object(bridge, 'ping_backend_mcp', return_value=False), \
             patch.object(bridge, 'is_td_process_alive', return_value=True), \
             patch.object(bridge, 'is_process_alive', return_value=True), \
             patch.object(bridge, 'find_all_td_pids', return_value=[]):
            bridge.reconcile(state, None, heartbeat=False)

    def test_active_flip_does_not_move_pinned_bridge(self):
        """Registry churn (a new instance claiming active) must not
        re-target a bridge pinned elsewhere."""
        reg = dict(self.REG)
        reg['active'] = 'B'  # someone flipped the default
        path = self._write_registry(reg)
        state = bridge.BridgeState(
            url='http://127.0.0.1:9870/mcp', config=dict(self.REG),
            config_path=path, active_name='A',
            pinned_instance='A', seen_epoch=0)
        self._reconcile_once(state)
        self.assertEqual(state.pinned_instance, 'A')
        self.assertIn('9870', state.url)  # still on A

    def test_epoch_bump_moves_pinned_bridge(self):
        """An explicit all-sessions switch (active + active_epoch) IS a
        command and overrides the pin."""
        reg = dict(self.REG)
        reg['active'] = 'B'
        reg['active_epoch'] = 5
        path = self._write_registry(reg)
        state = bridge.BridgeState(
            url='http://127.0.0.1:9870/mcp', config=dict(self.REG),
            config_path=path, active_name='A',
            pinned_instance='A', seen_epoch=4)
        self._reconcile_once(state)
        self.assertEqual(state.pinned_instance, 'B')
        self.assertIn('9871', state.url)
        self.assertEqual(state.seen_epoch, 5)

    def test_stale_epoch_is_not_a_command(self):
        """An epoch already seen (e.g. seeded at startup) is history,
        not a fresh command."""
        reg = dict(self.REG)
        reg['active'] = 'B'
        reg['active_epoch'] = 5
        path = self._write_registry(reg)
        state = bridge.BridgeState(
            url='http://127.0.0.1:9870/mcp', config=dict(self.REG),
            config_path=path, active_name='A',
            pinned_instance='A', seen_epoch=5)
        self._reconcile_once(state)
        self.assertEqual(state.pinned_instance, 'A')
        self.assertIn('9870', state.url)

    def test_unpinned_bridge_adopts_first_resolution_as_pin(self):
        path = self._write_registry(dict(self.REG))
        state = bridge.BridgeState(
            url='http://127.0.0.1:9870/mcp', config=dict(self.REG),
            config_path=path, active_name=None,
            pinned_instance=None, seen_epoch=0)
        self._reconcile_once(state)
        self.assertEqual(state.pinned_instance, 'A')

    def test_pinned_instance_port_change_self_heals(self):
        """The pinned instance restarted on a new port: the bridge must
        follow it by NAME (the survivability 'active' used to provide,
        now per-instance)."""
        reg = dict(self.REG)
        reg['instances'] = dict(reg['instances'])
        reg['instances']['A'] = {'toe_path': 'dev/A.toe',
                                 'port': 9999, 'td_pid': 111}
        path = self._write_registry(reg)
        state = bridge.BridgeState(
            url='http://127.0.0.1:9870/mcp', config=dict(self.REG),
            config_path=path, active_name='A',
            pinned_instance='A', seen_epoch=0)
        self._reconcile_once(state)
        self.assertIn('9999', state.url)
        self.assertEqual(state.pinned_instance, 'A')

    # -- switch_instance ----------------------------------------------

    def _switch(self, params, registry):
        state = bridge.BridgeState(
            url='http://127.0.0.1:9870/mcp', config=dict(registry),
            config_path='/fake/envoy.json', active_name='A',
            pinned_instance='A', seen_epoch=0)
        writes = []
        with patch.object(bridge, 'load_config', return_value=dict(registry)), \
             patch.object(bridge, 'ping_envoy_port', return_value=True), \
             patch.object(bridge, 'is_td_process_alive', return_value=True), \
             patch.object(bridge, 'is_process_alive', return_value=True), \
             patch.object(bridge, 'find_all_td_pids', return_value=[]), \
             patch.object(bridge, 'atomic_write_json',
                          side_effect=lambda p, d: writes.append((p, d))), \
             patch.object(bridge, 'notify_stdout', lambda *a, **k: None):
            result = bridge.handle_switch_instance(params, state)
        return result, state, writes

    def test_switch_default_is_session_local(self):
        result, state, writes = self._switch({'instance': 'B'}, self.REG)
        self.assertEqual(result.get('status'), 'success')
        self.assertFalse(result.get('all_sessions'))
        self.assertEqual(state.pinned_instance, 'B')
        self.assertIn('9871', state.url)
        # The registry must NOT be rewritten -- peers unaffected.
        self.assertEqual(writes, [])

    def test_switch_all_sessions_writes_active_and_epoch(self):
        result, state, writes = self._switch(
            {'instance': 'B', 'all_sessions': True}, self.REG)
        self.assertEqual(result.get('status'), 'success')
        self.assertTrue(result.get('all_sessions'))
        self.assertEqual(len(writes), 1)
        _path, written = writes[0]
        self.assertEqual(written.get('active'), 'B')
        self.assertEqual(written.get('active_epoch'), 1)
        self.assertEqual(state.seen_epoch, 1)

    def test_switch_list_mode_reports_pin(self):
        result, _state, writes = self._switch({}, self.REG)
        self.assertEqual(result.get('status'), 'list')
        self.assertEqual(result.get('pinned_instance'), 'A')
        self.assertEqual(writes, [])


class TestRegistryAdoptIfVacant(EmbodyTestCase):
    """envoy_setup.write_envoy_config must not steal 'active' from a
    live instance (the 2026-07-25 smoke-run hijack)."""

    def _stub_ext(self, alive_pids):
        ext = MagicMock()
        # instance_key() consults Envoyinstancename -- return '' so the
        # key derives from the toe basename instead of a MagicMock (which
        # would become a non-serializable registry key).
        ext.ownerComp.par.Envoyinstancename.eval.return_value = ''
        ext._isPidAlive = lambda pid: pid in alive_pids
        ext._atomicWriteJSON = lambda path, data: path.write_text(
            json.dumps(data), encoding='utf-8')
        ext._log = lambda *a, **k: None
        return ext

    def _run(self, existing_registry, alive_pids, port=9871):
        import shutil
        import tempfile
        from pathlib import Path
        setup_mod = op.Embody.op('envoy_setup').module
        root = Path(tempfile.mkdtemp(prefix='adopt_vacant_'))
        embody_dir = root / '.embody'
        embody_dir.mkdir()
        cfg = embody_dir / 'envoy.json'
        if existing_registry is not None:
            cfg.write_text(json.dumps(existing_registry), encoding='utf-8')
        ext = self._stub_ext(alive_pids)
        setup_mod.write_envoy_config(ext, embody_dir, port)
        self.addCleanup(lambda: shutil.rmtree(root, True))
        return json.loads(cfg.read_text(encoding='utf-8'))

    def test_registration_does_not_steal_active_from_live_instance(self):
        me = os.getpid()
        reg = {'active': 'other', 'instances': {
            'other': {'toe_path': 'dev/other.toe', 'port': 9870,
                      'td_pid': 424242}}}
        out = self._run(reg, alive_pids={424242, me})
        self.assertEqual(out['active'], 'other')
        self.assertIn('other', out['instances'])
        # ...and we still registered ourselves alongside.
        self.assertEqual(len(out['instances']), 2)

    def test_registration_adopts_vacant_active(self):
        out = self._run({'active': '', 'instances': {}},
                        alive_pids={os.getpid()})
        self.assertTrue(out['active'])
        self.assertIn(out['active'], out['instances'])

    def test_registration_adopts_active_of_dead_instance(self):
        me = os.getpid()
        reg = {'active': 'dead', 'instances': {
            'dead': {'toe_path': 'dev/dead.toe', 'port': 9870,
                     'td_pid': 999999999}}}
        out = self._run(reg, alive_pids={me})
        self.assertNotEqual(out['active'], 'dead')
        self.assertNotIn('dead', out['instances'])  # GC pruned
        self.assertIn(out['active'], out['instances'])


# =====================================================================
# Worktree coordination (Phase 2, 2026-07-25)
# =====================================================================

class TestWorktreeCoordination(EmbodyTestCase):
    """Durable worktree claims + landing-preflight logic (module-level,
    TD-free helpers in EnvoyExt)."""

    def _mod(self):
        return op.Embody.op('EnvoyExt').module

    # -- durable_claim_alive ------------------------------------------

    def test_durable_claim_lives_while_worktree_exists(self):
        import tempfile, time as _t
        mod = self._mod()
        wt = tempfile.mkdtemp(prefix='wt_alive_')
        self.addCleanup(lambda: __import__('shutil').rmtree(wt, True))
        claim = {'path': wt, 'ts': _t.time() - 3600}
        self.assertTrue(mod.durable_claim_alive(claim, _t.time()))

    def test_durable_claim_dies_when_worktree_gone(self):
        import time as _t
        mod = self._mod()
        claim = {'path': r'C:\nonexistent\wt_gone_xyz', 'ts': _t.time()}
        self.assertFalse(mod.durable_claim_alive(claim, _t.time()))

    def test_durable_claim_dies_past_max_age(self):
        import tempfile, time as _t
        mod = self._mod()
        wt = tempfile.mkdtemp(prefix='wt_old_')
        self.addCleanup(lambda: __import__('shutil').rmtree(wt, True))
        now = _t.time()
        claim = {'path': wt, 'ts': now - (8 * 86400)}
        self.assertFalse(mod.durable_claim_alive(claim, now))

    # -- compute_landing_conflicts ------------------------------------

    def test_landing_conflicts_intersections(self):
        mod = self._mod()
        out = mod.compute_landing_conflicts(
            landing_files=['a.py', 'b.py', 'c.tdn', 'd.md'],
            main_dirty=['b.py', 'zz.txt'],
            peer_files=['c.tdn'],
            tdn_unsaved=['c.tdn', 'other.tdn'])
        self.assertEqual(out['main_dirty'], ['b.py'])
        self.assertEqual(out['peers'], ['c.tdn'])
        self.assertEqual(out['tdn_unsaved'], ['c.tdn'])

    def test_landing_conflicts_clear(self):
        mod = self._mod()
        out = mod.compute_landing_conflicts(
            ['a.py'], ['b.py'], ['c.py'], ['d.py'])
        self.assertEqual(out, {'main_dirty': [], 'peers': [],
                               'tdn_unsaved': []})

    # -- read_tsv_dirty_paths -----------------------------------------

    def test_read_tsv_dirty_paths(self):
        import tempfile
        from pathlib import Path
        mod = self._mod()
        root = Path(tempfile.mkdtemp(prefix='tsv_dirty_'))
        self.addCleanup(lambda: __import__('shutil').rmtree(root, True))
        tsv_dir = root / 'dev' / 'embody'
        tsv_dir.mkdir(parents=True)
        (tsv_dir / 'externalizations.tsv').write_text(
            'path\ttype\tstrategy\trel_file_path\ttimestamp\tdirty\tbuild\n'
            '/a\ttext\tpy\tembody/a.py\t2026\tTrue\t1\n'
            '/b\ttext\tpy\tembody/b.py\t2026\t\t1\n'
            '/c\tbase\ttdn\tembody/c.tdn\t2026\tTrue\t1\n',
            encoding='utf-8')
        out = mod.read_tsv_dirty_paths(str(root))
        self.assertEqual(out, {'dev/embody/a.py', 'dev/embody/c.tdn'})

    def test_read_tsv_dirty_paths_missing_table(self):
        import tempfile
        mod = self._mod()
        root = tempfile.mkdtemp(prefix='tsv_none_')
        self.addCleanup(lambda: __import__('shutil').rmtree(root, True))
        self.assertEqual(mod.read_tsv_dirty_paths(root), set())


class TestQuitTdPidScoping(EmbodyTestCase):
    """quit_td must act on ONE pid, never the whole application (v6.0.169+).

    The old darwin graceful path delegated to an AppleScript
    application-level quit, which is pid-blind -- it targets whichever TD
    instance the system resolves, never reliably the intended one, so a
    probe-instance quit could take the user's dev session down instead. The
    darwin branch is now the pid-scoped SIGTERM the other POSIX platforms
    already used. Coverage is three-layered: a behavioral test drives the
    darwin branch with the platform mocked (runs on every OS), source
    tripwires catch the known app-wide phrasings (exact literals only --
    they are a cheap alarm, not a proof), and a byte-identity check keeps
    the shipped template copy honest (deployment PREFERS the template DAT
    while these tests load the fallback -- drift means green tests over a
    buggy shipped bridge).
    """

    def test_no_app_wide_quit_in_bridge_source(self):
        with open(_bridge_path, encoding='utf-8') as f:
            src = f.read()
        for phrase in ('quit app', 'osascript', 'killall', 'pkill'):
            self.assertNotIn(
                phrase, src,
                f'The bridge must never quit TouchDesigner app-wide '
                f'({phrase!r} found) -- quit_td is pid-scoped (SIGTERM on '
                f'POSIX, taskkill /PID on Windows)')

    def test_quit_td_posix_branch_is_pid_scoped(self):
        import inspect
        src = inspect.getsource(bridge.quit_td)
        self.assertIn(
            'signal.SIGTERM', src,
            'quit_td must send a pid-scoped SIGTERM on POSIX platforms')

    def test_quit_td_darwin_sends_sigterm_to_target_pid(self):
        """Behavioral pin, runs on every platform: with the platform mocked
        to darwin, quit_td must SIGTERM exactly the target pid -- not
        itself, not every TD pid (the app-wide bug in new clothes would
        pass a source grep; it cannot pass this)."""
        import signal as _signal
        kills = []
        seen = {'n': 0}

        def mock_alive(pid):
            if pid != 12345:
                return False        # stray concurrent caller -- not ours
            seen['n'] += 1
            return seen['n'] <= 1   # alive at entry, dead after SIGTERM

        # Everything quit_td needs is INJECTED (clock/sleep/platform) --
        # no time patching (uvicorn threads exhausted a finite list into
        # StopIteration) and no module-sys mocking (a mocked platform
        # silently failed to take in a full run and a REAL
        # `taskkill /PID 12345` escaped to the host). subprocess.run is
        # patched as pure belt-and-suspenders: if platform routing ever
        # regresses, nothing real executes. os.kill is the recorder; a
        # stray concurrent os.kill would ADD entries and fail loudly.
        with patch.object(bridge, 'is_process_alive', side_effect=mock_alive), \
             patch('subprocess.run'), \
             patch('os.kill', side_effect=lambda p, s: kills.append((p, s))):
            success, _msg = bridge.quit_td(
                12345, clock=_slow_clock(100.0), sleep=lambda s: None,
                platform='darwin')

        self.assertTrue(success)
        self.assertEqual(
            kills, [(12345, _signal.SIGTERM)],
            'quit_td on darwin must send exactly one SIGTERM, to exactly '
            'the target pid')

    def test_template_copy_byte_identical(self):
        """The two bridge copies must never drift: envoy_setup deploys the
        TEMPLATE DAT (falling back to this file only when the DAT is
        missing), while every test in this suite loads the FALLBACK --
        drift means the suite goes green over a buggy shipped bridge."""
        import hashlib
        template_path = os.path.join(
            project.folder, 'embody', 'Embody', 'templates',
            'text_envoy_bridge.py')
        with open(_bridge_path, 'rb') as f:
            fallback_sha = hashlib.sha256(f.read()).hexdigest()
        with open(template_path, 'rb') as f:
            template_sha = hashlib.sha256(f.read()).hexdigest()
        self.assertEqual(
            fallback_sha, template_sha,
            'dev/embody/envoy_bridge.py and templates/text_envoy_bridge.py '
            'must stay byte-identical (see memory: bridge-template-fallback-'
            'sync)')


class TestWaitForNewTdPid(EmbodyTestCase):
    """wait_for_new_td_pid: the poll-until-deadline replacement for the old
    single fixed-2s snapshot diff after a macOS 'open -n' launch. Pure
    injected logic -- runs on every platform, no real sleeping."""

    def _run(self, snapshots, existing=(1, 2), timeout=5.0, poll=0.5):
        """Drive the helper with a scripted find_pids() sequence and a fake
        clock advanced only by the injected sleep. Returns (pid, state)."""
        state = {'t': 0.0, 'i': 0, 'sleeps': []}

        def find_pids():
            i = min(state['i'], len(snapshots) - 1)
            state['i'] += 1
            return snapshots[i]

        def clock():
            return state['t']

        def sleep(s):
            state['sleeps'].append(s)
            state['t'] += s

        pid = bridge.wait_for_new_td_pid(
            existing, timeout=timeout, poll=poll,
            find_pids=find_pids, clock=clock, sleep=sleep)
        return pid, state

    def test_immediate_new_pid_returns_without_sleeping(self):
        pid, state = self._run([[1, 2, 77]])
        self.assertEqual(pid, 77)
        self.assertEqual(state['sleeps'], [],
                         'A pid visible on the first diff must return at once')

    def test_slow_spawn_found_on_later_poll(self):
        pid, state = self._run([[1, 2], [1, 2], [1, 2, 88]])
        self.assertEqual(
            pid, 88,
            'A spawn slower than one poll interval must still be attributed '
            '-- the old single 2s diff returned None here')
        self.assertEqual(len(state['sleeps']), 2)

    def test_timeout_returns_none_and_stops_sleeping(self):
        pid, state = self._run([[1, 2]], timeout=2.0, poll=0.5)
        self.assertIsNone(pid, 'No new pid by the deadline must yield None')
        self.assertEqual(
            sum(state['sleeps']), 2.0,
            'The loop must stop exactly at the deadline (a >= -> > '
            'regression would sleep one extra poll and read 2.5 here)')

    def test_pid_appearing_exactly_at_deadline_is_attributed(self):
        """The loop diffs BEFORE checking the deadline: a pid visible on
        the final poll (clock == deadline) is attributed, not dropped."""
        pid, _ = self._run([[1, 2]] * 4 + [[1, 2, 99]],
                           timeout=2.0, poll=0.5)
        self.assertEqual(pid, 99)

    def test_existing_pids_never_attributed(self):
        pid, _ = self._run([[2, 1]], existing=(1, 2), timeout=1.0)
        self.assertIsNone(
            pid, 'Pre-launch pids must never be attributed to the launch')

    def test_multiple_new_pids_attributes_lowest_and_logs(self):
        with patch.object(bridge, 'log') as mock_log:
            pid, _ = self._run([[1, 2, 90, 85]])
        self.assertEqual(
            pid, 85,
            'Same-window ambiguity must resolve deterministically '
            '(lowest new pid), never by list order')
        logged = ' '.join(str(c) for c in mock_log.call_args_list)
        self.assertIn('85', logged)
        self.assertIn('90', logged)

    def test_launch_td_darwin_branch_uses_the_helper(self):
        import inspect
        src = inspect.getsource(bridge.launch_td)
        self.assertIn(
            'wait_for_new_td_pid(', src,
            'launch_td must resolve the darwin pid via the polling helper')
        self.assertNotIn(
            'time.sleep(2)', src,
            'The old fixed 2s single-shot diff must not return')
