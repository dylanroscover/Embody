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
#   - start_reconciler:      v2, polls .envoy.json + pings backend every 1-5s
# Tests that need to exercise the reconciler call bridge.reconcile()
# directly with explicit heartbeat=True instead.
bridge.start_orphan_watchdog = lambda: None
if hasattr(bridge, 'start_reconciler'):
    bridge.start_reconciler = lambda *args, **kwargs: None

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase


# =====================================================================
# Argument Parsing
# =====================================================================

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
        """--port as last arg with no value — uses default."""
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
    v2 forward_to_http uses a pooled ``http.client.HTTPConnection`` per
    URL (see bridge._http_pool) instead of ``urllib.request.urlopen``.
    Tests mock the pooled connection directly via
    ``_get_http_connection`` so they exercise the real code path
    without opening real sockets.
    """

    def setUp(self):
        # Clear the pool between tests so mocks don't leak state and
        # unreachable connections from earlier tests don't linger.
        with bridge._http_pool_lock:
            bridge._http_pool.clear()

    def _make_conn(self, body, status=200, reason='OK'):
        """Build a mock HTTPConnection that returns ``body`` as the response."""
        resp = MagicMock()
        resp.read.return_value = body.encode('utf-8')
        resp.status = status
        resp.reason = reason
        conn = MagicMock()
        conn.sock = None
        conn.getresponse.return_value = resp
        return conn

    # --- SSE format ---

    def test_sse_format_single_event(self):
        body = 'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{"ok":true}}\n\n'
        conn = self._make_conn(body)
        with patch.object(bridge, '_get_http_connection', return_value=conn):
            result = bridge.forward_to_http('http://localhost:9870/mcp', {'id': 1})
        self.assertTrue(result['result']['ok'])

    def test_sse_data_only_no_event_line(self):
        """SSE with just data: line, no event: prefix."""
        body = 'data: {"id":1,"result":"bare"}\n\n'
        conn = self._make_conn(body)
        with patch.object(bridge, '_get_http_connection', return_value=conn):
            result = bridge.forward_to_http('http://localhost:9870/mcp', {'id': 1})
        self.assertEqual(result['result'], 'bare')

    def test_sse_multiple_events_returns_first(self):
        body = 'data: {"first":true}\n\ndata: {"second":true}\n\n'
        conn = self._make_conn(body)
        with patch.object(bridge, '_get_http_connection', return_value=conn):
            result = bridge.forward_to_http('http://localhost:9870/mcp', {'id': 1})
        self.assertTrue(result.get('first'))

    def test_sse_with_extra_whitespace(self):
        body = '  data: {"id":1}  \n\n'
        conn = self._make_conn(body)
        with patch.object(bridge, '_get_http_connection', return_value=conn):
            result = bridge.forward_to_http('http://localhost:9870/mcp', {'id': 1})
        self.assertEqual(result['id'], 1)

    # --- Plain JSON fallback ---

    def test_plain_json_response(self):
        body = '{"jsonrpc":"2.0","id":1,"result":"hello"}'
        conn = self._make_conn(body)
        with patch.object(bridge, '_get_http_connection', return_value=conn):
            result = bridge.forward_to_http('http://localhost:9870/mcp', {'id': 1})
        self.assertEqual(result['result'], 'hello')

    def test_plain_json_with_surrounding_whitespace(self):
        body = '  \n  {"jsonrpc":"2.0","id":1,"result":"padded"}  \n  '
        conn = self._make_conn(body)
        with patch.object(bridge, '_get_http_connection', return_value=conn):
            result = bridge.forward_to_http('http://localhost:9870/mcp', {'id': 1})
        self.assertEqual(result['result'], 'padded')

    # --- Empty / malformed responses ---

    def test_empty_response_body(self):
        conn = self._make_conn('')
        with patch.object(bridge, '_get_http_connection', return_value=conn):
            result = bridge.forward_to_http('http://localhost:9870/mcp', {'id': 1})
        self.assertIsNone(result)

    def test_whitespace_only_response(self):
        conn = self._make_conn('   \n  ')
        with patch.object(bridge, '_get_http_connection', return_value=conn):
            result = bridge.forward_to_http('http://localhost:9870/mcp', {'id': 1})
        self.assertIsNone(result)

    def test_malformed_json_in_plain_body(self):
        """Garbled non-JSON body returns None, doesn't crash."""
        conn = self._make_conn('not json at all')
        with patch.object(bridge, '_get_http_connection', return_value=conn):
            result = bridge.forward_to_http('http://localhost:9870/mcp', {'id': 1})
        self.assertIsNone(result)

    # --- Error propagation ---

    def test_http_error_raises_oserror(self):
        """HTTP status >= 400 raises OSError so the retry path catches it."""
        conn = self._make_conn('error body', status=500, reason='Internal Server Error')
        with patch.object(bridge, '_get_http_connection', return_value=conn):
            raised = False
            try:
                bridge.forward_to_http('http://localhost:9870/mcp', {'id': 1})
            except OSError:
                raised = True
            self.assertTrue(raised, 'HTTP 500 should raise OSError')

    def test_connection_error_propagates(self):
        """ConnectionRefusedError (subclass of OSError) propagates."""
        conn = MagicMock()
        conn.sock = None
        conn.request.side_effect = ConnectionRefusedError('refused')
        with patch.object(bridge, '_get_http_connection', return_value=conn):
            raised = False
            try:
                bridge.forward_to_http('http://localhost:9870/mcp', {'id': 1})
            except OSError:
                raised = True
            self.assertTrue(raised, 'ConnectionRefusedError should propagate as OSError')

    def test_timeout_propagates(self):
        """Socket timeout (OSError subclass) propagates."""
        import socket
        conn = MagicMock()
        conn.sock = None
        conn.request.side_effect = socket.timeout('timed out')
        with patch.object(bridge, '_get_http_connection', return_value=conn):
            raised = False
            try:
                bridge.forward_to_http('http://localhost:9870/mcp', {'id': 1})
            except OSError:
                raised = True
            self.assertTrue(raised, 'socket.timeout should propagate as OSError')

    def test_http_exception_wrapped_as_oserror(self):
        """http.client.HTTPException is wrapped so retry path catches it."""
        import http.client
        conn = MagicMock()
        conn.sock = None
        conn.request.side_effect = http.client.BadStatusLine('oops')
        with patch.object(bridge, '_get_http_connection', return_value=conn):
            raised = False
            try:
                bridge.forward_to_http('http://localhost:9870/mcp', {'id': 1})
            except OSError:
                raised = True
            self.assertTrue(raised, 'HTTPException should be wrapped as OSError')

    # --- Request correctness ---

    def test_request_content_type_header(self):
        conn = self._make_conn('{}')
        with patch.object(bridge, '_get_http_connection', return_value=conn):
            bridge.forward_to_http('http://localhost:9870/mcp', {'id': 1})
        call_kwargs = conn.request.call_args
        headers = call_kwargs[1].get('headers') if call_kwargs[1] else None
        if headers is None and len(call_kwargs[0]) >= 4:
            headers = call_kwargs[0][3]
        self.assertEqual(headers.get('Content-Type'), 'application/json')

    def test_request_accept_header(self):
        conn = self._make_conn('{}')
        with patch.object(bridge, '_get_http_connection', return_value=conn):
            bridge.forward_to_http('http://localhost:9870/mcp', {'id': 1})
        call_kwargs = conn.request.call_args
        headers = call_kwargs[1].get('headers') if call_kwargs[1] else None
        if headers is None and len(call_kwargs[0]) >= 4:
            headers = call_kwargs[0][3]
        self.assertIn('text/event-stream', headers.get('Accept', ''))

    def test_request_body_is_valid_json(self):
        msg = {'jsonrpc': '2.0', 'id': 42, 'method': 'tools/call'}
        conn = self._make_conn('{}')
        with patch.object(bridge, '_get_http_connection', return_value=conn):
            bridge.forward_to_http('http://localhost:9870/mcp', msg)
        call_args = conn.request.call_args
        body = call_args[1].get('body') if call_args[1] else None
        if body is None and len(call_args[0]) >= 3:
            body = call_args[0][2]
        self.assertDictEqual(json.loads(body), msg)

    def test_custom_timeout_passed(self):
        conn = self._make_conn('{}')
        with patch.object(bridge, '_get_http_connection', return_value=conn):
            bridge.forward_to_http('http://localhost:9870/mcp', {'id': 1}, timeout=5)
        # Timeout is applied to conn.timeout; conn.sock is None so no settimeout call.
        # conn is a MagicMock so conn.timeout = 5 was set via attribute assignment.
        self.assertEqual(conn.timeout, 5)


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
        self.assertIn('[envoy-bridge]', err.getvalue())

    def test_log_flushes_stderr(self):
        err = MagicMock()
        with patch.object(sys, 'stderr', err):
            bridge.log('flush test')
        err.flush.assert_called()


# =====================================================================
# wait_for_envoy — Retry / Reconnection Logic
# =====================================================================

class TestBridgeWaitForEnvoy(EmbodyTestCase):

    def test_server_up_immediately(self):
        """Server responds on first probe — instant success."""
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
        self.assertApproxEqual(sleeps[0], bridge.RETRY_INTERVALS[0], tolerance=0.01)
        # Verify several intervals match the schedule
        for i, expected in enumerate(bridge.RETRY_INTERVALS):
            if i < len(sleeps):
                self.assertApproxEqual(sleeps[i], expected, tolerance=0.01)

    def test_retry_clamps_sleep_to_remaining_time(self):
        """Sleep duration is clamped to time remaining before deadline."""
        import urllib.error
        sleeps = []

        fake_time = [0.0]
        deadline = 0.3  # Very tight — less than RETRY_INTERVALS[0]=0.5

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
        self.assertApproxEqual(
            tail_sleep, bridge.RETRY_INTERVALS[-1], tolerance=0.01)


# =====================================================================
# Main Event Loop — Disconnection & Reconnection Scenarios
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
        """Non-protocol method gets error when Envoy is unreachable."""
        msg = {'jsonrpc': '2.0', 'id': 1, 'method': 'resources/list'}
        responses = self._run_main([msg], wait_result=False)
        self.assertLen(responses, 1)
        self.assertDictHasKey(responses[0], 'error')
        self.assertIn('connection lost', responses[0]['error']['message'].lower())

    def test_initial_timeout_includes_actionable_hint(self):
        msg = {'jsonrpc': '2.0', 'id': 1, 'method': 'resources/list'}
        responses = self._run_main(
            [msg], wait_result=False,
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
        """Full init → tools/list → launch_td works without Envoy."""
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

    def test_initial_timeout_then_next_message_retries_connect(self):
        """After initial timeout, the next message triggers wait_for_envoy again."""
        msgs = [
            {'jsonrpc': '2.0', 'id': 1, 'method': 'first'},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'second'},
        ]
        stdin = self._make_stdin(msgs)
        stdout = io.StringIO()
        stderr = io.StringIO()

        wait_calls = [0]
        def mock_wait(url, deadline):
            wait_calls[0] += 1
            return wait_calls[0] >= 2  # Fail first, succeed second

        with patch.object(sys, 'stdin', stdin), \
             patch.object(sys, 'stdout', stdout), \
             patch.object(sys, 'stderr', stderr), \
             patch.object(sys, 'argv', ['envoy_bridge.py']), \
             patch.object(bridge, 'wait_for_envoy', side_effect=mock_wait), \
             patch.object(bridge, 'forward_to_http',
                          return_value={'jsonrpc': '2.0', 'id': 2, 'result': 'ok'}), \
             patch.object(bridge, 'find_td_pid', return_value=None), \
             patch.object(bridge, 'kill_stale_bridges'), \
             patch('time.sleep'):
            bridge.main()

        self.assertEqual(wait_calls[0], 2)
        lines = [l for l in stdout.getvalue().strip().split('\n') if l.strip()]
        responses = [json.loads(l) for l in lines]
        # First: error (connection failed), Second: success
        self.assertLen(responses, 2)
        self.assertDictHasKey(responses[0], 'error')
        self.assertEqual(responses[1]['result'], 'ok')

    # --- Single-attempt forwarding (v2: no per-request retries) ---

    def test_single_failure_sends_error(self):
        """Single URLError immediately returns error — no retries."""
        import urllib.error

        def always_fail(url, msg, **kw):
            raise urllib.error.URLError('Connection refused')

        msg = {'jsonrpc': '2.0', 'id': 1, 'method': 'test'}
        responses = self._run_main([msg], forward_side_effect=always_fail)
        self.assertLen(responses, 1)
        self.assertDictHasKey(responses[0], 'error')
        self.assertIn('connection lost', responses[0]['error']['message'].lower())

    def test_failure_notification_no_response(self):
        """Notification with forward failure — no error sent."""
        import urllib.error

        def always_fail(url, msg, **kw):
            raise urllib.error.URLError('refused')

        msg = {'jsonrpc': '2.0', 'method': 'notifications/progress'}
        responses = self._run_main([msg], forward_side_effect=always_fail)
        self.assertLen(responses, 0)

    def test_single_attempt_only(self):
        """forward_to_http is called exactly once per request — no retries."""
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
        """After failure, next message calls wait_for_envoy again."""
        import urllib.error
        call_count = [0]

        def forward(url, msg, **kw):
            call_count[0] += 1
            if call_count[0] == 1:  # First msg fails (single attempt)
                raise urllib.error.URLError('refused')
            return {'jsonrpc': '2.0', 'id': msg.get('id'), 'result': 'back'}

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
            return True

        with patch.object(sys, 'stdin', stdin), \
             patch.object(sys, 'stdout', stdout), \
             patch.object(sys, 'stderr', stderr), \
             patch.object(sys, 'argv', ['envoy_bridge.py']), \
             patch.object(bridge, 'wait_for_envoy', side_effect=mock_wait), \
             patch.object(bridge, 'forward_to_http', side_effect=forward), \
             patch.object(bridge, 'find_td_pid', return_value=None), \
             patch.object(bridge, 'kill_stale_bridges'), \
             patch('time.sleep'):
            bridge.main()

        # Initial connect + reconnect after disconnect
        self.assertEqual(wait_calls[0], 2)

        lines = [l for l in stdout.getvalue().strip().split('\n') if l.strip()]
        responses = [json.loads(l) for l in lines]
        self.assertLen(responses, 2)
        self.assertDictHasKey(responses[0], 'error')  # First failed
        self.assertEqual(responses[1]['result'], 'back')  # Reconnected

    def test_reconnect_fails_sends_error_again(self):
        """Disconnect, reconnect attempt fails — second error sent."""
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
        """Notification sandwiched between requests — only requests get responses."""
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
        """Notification that fails forwarding — no error response."""
        import urllib.error

        def forward(url, msg, **kw):
            raise urllib.error.URLError('refused')

        msg = {'jsonrpc': '2.0', 'method': 'notifications/cancelled'}
        responses = self._run_main([msg], forward_side_effect=forward)
        self.assertLen(responses, 0)

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
        """JSON-RPC allows id=0 — must NOT be treated as notification."""
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
        """Empty stdin (immediate EOF) — main() exits without error."""
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
        """stdin with only whitespace/empty lines — exits without error."""
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

    These don't go through main() — they wrap it at the top level.
    We test the exception handling logic directly.
    """

    def test_keyboard_interrupt_suppressed(self):
        """KeyboardInterrupt during main() — logged, not propagated."""
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
        """BrokenPipeError (client closed stdout) — silently suppressed."""
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
        result = bridge.resolve_toe_path(config, '/repo/.envoy.json')
        self.assertEqual(result, '/repo/dev/test.toe')

    def test_resolve_toe_path_missing(self):
        result = bridge.resolve_toe_path({}, '/some/config.json')
        self.assertIsNone(result)


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

    @patch('envoy_bridge.sys')
    def test_is_process_alive_win32_uses_openprocess(self, mock_sys):
        """On Windows, uses OpenProcess(SYNCHRONIZE) instead of os.kill."""
        mock_sys.platform = "win32"
        mock_kernel32 = MagicMock()
        mock_kernel32.OpenProcess.return_value = 42  # non-zero = valid handle
        mock_ctypes = MagicMock()
        mock_ctypes.windll.kernel32 = mock_kernel32
        with patch.dict('sys.modules', {'ctypes': mock_ctypes}):
            self.assertTrue(bridge.is_process_alive(1234))
        mock_kernel32.OpenProcess.assert_called_once_with(0x00100000, False, 1234)
        mock_kernel32.CloseHandle.assert_called_once_with(42)

    @patch('envoy_bridge.sys')
    def test_is_process_alive_win32_dead_process(self, mock_sys):
        """On Windows, returns False when OpenProcess returns 0 (dead PID)."""
        mock_sys.platform = "win32"
        mock_kernel32 = MagicMock()
        mock_kernel32.OpenProcess.return_value = 0  # zero = failed / no process
        mock_ctypes = MagicMock()
        mock_ctypes.windll.kernel32 = mock_kernel32
        with patch.dict('sys.modules', {'ctypes': mock_ctypes}):
            self.assertFalse(bridge.is_process_alive(9999))
        mock_kernel32.CloseHandle.assert_not_called()


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
        # Mock find_td_pid → None so the "already running" guard doesn't
        # short-circuit before we reach the missing-config check.  Without
        # this the test fails on any machine actually running TD (e.g. the
        # Embody dev project itself).
        with patch.object(bridge, 'find_td_pid', return_value=None):
            state = self._make_state(config={})
            result = bridge.handle_launch_td({}, state)
        self.assertEqual(result['status'], 'error')
        self.assertIn('.envoy.json', result['message'])

    def test_launch_td_already_running(self):
        """Refuses to launch if TD is already running."""
        with patch.object(bridge, 'find_td_pid', return_value=os.getpid()):
            state = self._make_state(
                config={'td_executable': '/usr/bin/td', 'toe_path': 'test.toe'})
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
            config_path='/tmp/.envoy.json')
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

    def test_quit_td_graceful_exit(self):
        """Graceful quit succeeds when process exits promptly."""
        call_count = [0]
        def mock_alive(pid):
            call_count[0] += 1
            # First call: alive (initial check), second call: dead (after quit)
            return call_count[0] <= 1
        with patch.object(bridge, 'is_process_alive', side_effect=mock_alive), \
             patch('subprocess.run'), \
             patch('time.sleep'), \
             patch('time.monotonic', side_effect=[100, 100, 101]):
            success, msg = bridge.quit_td(12345)
        self.assertTrue(success)
        self.assertIn('gracefully', msg)

    def test_quit_td_force_kill(self):
        """Force kill when graceful quit times out."""
        call_count = [0]
        def mock_alive(pid):
            call_count[0] += 1
            # Alive through graceful period (calls 1-3), dead after force kill (call 4)
            return call_count[0] <= 3
        # monotonic calls: deadline calc, then loop iterations, then past deadline
        # deadline = 100 + 15 = 115
        # Loop: check 105 (<115, iter), check 110 (<115, iter), check 116 (>=115, exit)
        # is_process_alive calls: 1 (initial), 2 (loop iter 1), 3 (loop iter 2), 4 (post-kill)
        mono_values = [100, 105, 110, 116]
        with patch.object(bridge, 'is_process_alive', side_effect=mock_alive), \
             patch('subprocess.run'), \
             patch('os.kill'), \
             patch('time.sleep'), \
             patch('time.monotonic', side_effect=mono_values):
            success, msg = bridge.quit_td(12345, graceful_timeout=15)
        self.assertTrue(success)
        self.assertIn('force-killed', msg)

    # --- restart_td ---

    def test_restart_td_not_running(self):
        """Error when TD is not running."""
        with patch.object(bridge, 'find_td_pid', return_value=None):
            state = self._make_state()
            result = bridge.handle_restart_td({}, state)
        self.assertEqual(result['status'], 'error')
        self.assertIn('not running', result['message'])

    def test_restart_td_quit_fails(self):
        """Error when TD cannot be terminated."""
        with patch.object(bridge, 'find_td_pid', return_value=12345), \
             patch.object(bridge, 'is_process_alive', return_value=True), \
             patch.object(bridge, 'quit_td',
                          return_value=(False, 'Could not terminate')):
            state = self._make_state(td_pid=12345)
            result = bridge.handle_restart_td({}, state)
        self.assertEqual(result['status'], 'error')
        self.assertIn('Could not terminate', result['message'])

    def test_restart_td_success(self):
        """Full restart: quit then launch, Envoy reachable."""
        with patch.object(bridge, 'find_td_pid', return_value=12345), \
             patch.object(bridge, 'is_process_alive', return_value=True), \
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
        with patch.object(bridge, 'find_td_pid', return_value=12345), \
             patch.object(bridge, 'is_process_alive', return_value=True), \
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
        with patch.object(bridge, 'find_td_pid', return_value=None):
            state = self._make_state()
            content = bridge.handle_bridge_tool('restart_td', {}, state)
        parsed = json.loads(content[0]['text'])
        self.assertEqual(parsed['status'], 'error')  # Not running

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
        msg = bridge.connection_lost_message(state)
        self.assertIn('not responding', msg.lower())
        self.assertIn(str(os.getpid()), msg)


# =====================================================================
# Bridge v2 — BridgeState, notify_stdout, reconciler, caching, hashing
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
            # Implementation may differ — try constructing with just url.
            inst = BS(url=defaults.get('url', 'http://localhost:9870/mcp'))
        for k, v in defaults.items():
            if k in ctor_keys:
                continue
            try:
                setattr(inst, k, v)
            except Exception:
                pass
        return inst

    # v1 fallback — plain object with lock-compatible context manager
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
# Test case 1 — BridgeState locking under contention
# =====================================================================

class TestBridgeStateLocking(EmbodyTestCase):
    """Two threads hammering a BridgeState counter produce no corruption."""

    def setUp(self):
        _require_v2(_V2_BRIDGE_STATE, 'BridgeState')

    def test_concurrent_increment_no_corruption(self):
        state = _make_v2_state()
        # Seed a counter field. BridgeState may not declare this attribute
        # by default — setattr should succeed either way.
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
            f'Expected {2 * iterations}, got {state.counter} — lock did not serialize writes')


# =====================================================================
# Test case 2 — Tool hash diff detection
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
        """Order must not affect the hash — plan says 'sort by name first'."""
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
        """Description is part of the hash per the plan — any change matters."""
        a = [self._tool('create_op', 'Create an operator')]
        b = [self._tool('create_op', 'Create an op')]
        self.assertNotEqual(self.hash_tools(a), self.hash_tools(b))

    def test_empty_list_produces_stable_hash(self):
        self.assertEqual(self.hash_tools([]), self.hash_tools([]))


# =====================================================================
# Test case 3 — Reconciler state transitions
# =====================================================================

class TestBridgeReconcilerTransitions(EmbodyTestCase):
    """
    Verify that reconcile() fires the on_tools_change callback only
    on connection transitions (False→True and True→False), never on
    steady-state ticks (True→True).

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

        # Mock tool fetch to return a stable list — avoids hash-mismatch noise.
        # Using ONE stable list means on_tools_change fires only on transitions,
        # not on in-place tool-list changes.
        stable_tools = [{'name': 't1', 'description': 'd1'}]

        notify_count = [0]

        def on_tools_change():
            notify_count[0] += 1

        # Patch only the functions that actually exist on the module —
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
# Test case 5 — listChanged: true in initialize response
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
# Test case 7 — tools/list cache hit within 5s
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

        self.assertEqual(
            tools_list_forward_count[0], 1,
            f'Expected exactly 1 tools/list forward (second should hit cache), '
            f'got {tools_list_forward_count[0]}. '
            'This is bridge v2 step 6 in the plan.')


# =====================================================================
# Test case 9 — Stdout serialization under concurrent writers
# =====================================================================

class TestBridgeStdoutSerialization(EmbodyTestCase):
    """
    10 threads × 100 concurrent calls to notify_stdout must produce
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

        # Expect exactly 10 × 100 = 1000 lines (unless notify_stdout was
        # called with fewer args due to TypeError fallback — still should
        # be 1000).
        self.assertEqual(
            len(lines), 1000,
            f'Expected 1000 notification lines, got {len(lines)}')

        # The critical assertion: every line parses as valid JSON.
        bad_lines = []
        for idx, line in enumerate(lines):
            try:
                obj = json.loads(line)
                # Must be a notification — no id, has method
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
# Stubs for tests that depend on implementation steps 7-9
# (deferred to a later bridge v2 pass)
# =====================================================================

class TestBridgeV2DeferredStubs(EmbodyTestCase):
    """
    Stubs for plan test cases 4, 6, 8 — left intentionally as SkipTest
    so they show up in the runner as a visible reminder that the next
    bridge v2 phase still needs coverage.
    """

    def test_local_ping_handler_stub(self):
        """Plan case 4 — depends on bridge v2 step 4 (local ping handling)."""
        raise SkipTest('depends on bridge v2 step 4: local ping handler')

    def test_find_all_td_pids_stub(self):
        """Plan case 6 — depends on bridge v2 step 8 (find_all_td_pids)."""
        raise SkipTest('depends on bridge v2 step 8: find_all_td_pids')

    def test_initial_probe_3s_stub(self):
        """Plan case 8 — depends on bridge v2 step 7 (3s initial probe)."""
        raise SkipTest('depends on bridge v2 step 7: 3s initial probe + bridge-only fallback')
