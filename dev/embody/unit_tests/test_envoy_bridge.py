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
import shutil
import subprocess
import sys
import tempfile
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
        """Build a stub response object that urlopen would return.

        Backed by a real byte stream rather than a MagicMock: since A-46
        forward_to_http reads INCREMENTALLY, so the fixture has to hand
        lines over the way a socket does or these tests would exercise
        nothing.  Every assertion below is unchanged -- they now run
        against the streaming reader instead of a read-to-EOF buffer.
        """
        return _FakeHTTPResponse(_sse_script(body))

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

    # A-46 CHANGED THESE THREE (panel: compat-regression, timeouts,
    # runs-for-real, all "important"). They used to pin a SILENT HANG:
    # forward_to_http returned None, main() writes nothing when the
    # response is None, and the client's request id was never answered
    # or errored while state.connected stayed True. A request that got
    # no answer is connection loss and must say so, so these now assert
    # the raise. A forward with no id owed no answer and still gets None
    # -- see test_empty_body_returns_none_for_a_notification_forward.

    def _expect_severance(self, body):
        with patch('urllib.request.urlopen',
                   return_value=self._make_response(body)):
            raised = None
            try:
                bridge.forward_to_http(
                    'http://localhost:9870/mcp', {'id': 1})
            except Exception as e:  # noqa: BLE001 -- type asserted below
                raised = e
        self.assertIsInstance(
            raised, ConnectionError,
            'an unanswered request must reach the main loop, not vanish')
        self.assertIsInstance(raised, OSError)

    def test_empty_response_body(self):
        self._expect_severance('')

    def test_whitespace_only_response(self):
        self._expect_severance('   \n  ')

    def test_malformed_json_in_plain_body(self):
        """Garbled non-JSON body is severance, not silence."""
        self._expect_severance('not json at all')

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

    def test_is_process_alive_win32_uses_openprocess(self):
        """On Windows, uses OpenProcess(SYNCHRONIZE) instead of os.kill."""
        k, mock_ctypes = self._win32_ctypes(42)  # non-zero = valid handle
        with patch.dict('sys.modules', {'ctypes': mock_ctypes}):
            self.assertTrue(bridge.is_process_alive(1234, platform='win32'))
        k.OpenProcess.assert_called_once_with(0x00100000, False, 1234)
        k.CloseHandle.assert_called_once_with(42)
        # Never ctypes.windll -- that cache is shared process-wide.
        mock_ctypes.WinDLL.assert_called_once_with("kernel32")

    def test_is_process_alive_win32_dead_process(self):
        """On Windows, returns False when OpenProcess returns 0 (dead PID)."""
        k, mock_ctypes = self._win32_ctypes(0)  # zero = failed / no process
        with patch.dict('sys.modules', {'ctypes': mock_ctypes}):
            self.assertFalse(bridge.is_process_alive(9999, platform='win32'))
        k.CloseHandle.assert_not_called()
        k.WaitForSingleObject.assert_not_called()

    def test_is_process_alive_win32_zombie_handle_is_dead(self):
        """Exited-but-handle-still-open must read DEAD, not alive.

        A Windows process object (and its PID) stays allocated while ANY
        handle to it is open, so OpenProcess keeps succeeding after the
        process exits. OpenProcess alone therefore reports such a PID
        alive forever -- which stranded heartbeat files for exited
        sessions here, leaving them as phantom peers. WAIT_OBJECT_0 means
        the object is signaled, which happens exactly on exit.
        """
        k, mock_ctypes = self._win32_ctypes(42, wait_result=0x00000000)
        with patch.dict('sys.modules', {'ctypes': mock_ctypes}):
            self.assertFalse(bridge.is_process_alive(1234, platform='win32'))
        k.CloseHandle.assert_called_once_with(42)  # and no handle leak

    def test_is_process_alive_win32_wait_failed_counts_as_alive(self):
        """WAIT_FAILED is inconclusive -- err toward ALIVE.

        Callers use this to prune registry rows and reap heartbeats;
        deleting state for a process we could not verify is the dangerous
        direction, so anything that is not an explicit WAIT_OBJECT_0
        keeps the process considered alive.
        """
        k, mock_ctypes = self._win32_ctypes(42, wait_result=0xFFFFFFFF)
        with patch.dict('sys.modules', {'ctypes': mock_ctypes}):
            self.assertTrue(bridge.is_process_alive(1234, platform='win32'))
        k.CloseHandle.assert_called_once_with(42)

    # --- find_all_td_pids: pgrep filtering on macOS/Linux ---
    # The five tests that used to live here self-skipped on win32, so the
    # Windows dev box had ZERO signal on the entire POSIX pid path. They
    # are superseded by TestProcessIdentityInjected, which drives the same
    # branch through injected platform/run on every machine (D-5).

    def test_is_td_helper_process_markers(self):
        """_is_td_helper_process matches Web Render and CEF --type= cmdlines.

        cmdline is passed directly (the shipped signature now accepts it)
        rather than patching _process_cmdline.
        """
        self.assertTrue(bridge._is_td_helper_process(
            1, cmdline='.../TouchDesigner Web Render.app/.../TouchDesigner'))
        self.assertTrue(bridge._is_td_helper_process(
            2, cmdline='.../TouchDesigner --type=renderer'))
        self.assertFalse(bridge._is_td_helper_process(
            3, cmdline='/Applications/TouchDesigner.app/Contents/MacOS/TouchDesigner'))
        self.assertFalse(bridge._is_td_helper_process(4, cmdline=''))


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

    def test_get_td_status_surfaces_active_convoy_pin(self):
        # A local status report must reveal that ordinary tools are relayed to
        # a pinned remote node, so an agent never assumes a local mutation.
        pin = {'target_host_id': 'host-remote', 'convoy_id': 'studio',
               'target_node_id': 'node-remote', 'expected_runtime_id': 'rt-7'}
        state = self._make_state(convoy_target=dict(pin))
        result = bridge.handle_get_td_status(state)
        self.assertEqual(result['convoy_pin'], pin)

    def test_get_td_status_omits_convoy_pin_when_local(self):
        state = self._make_state()
        result = bridge.handle_get_td_status(state)
        self.assertNotIn('convoy_pin', result)

    def test_launch_td_no_executable(self):
        """No configured exe AND no discoverable install -> a clear error.

        find_td_installs is stubbed empty: after A-14 made
        select_td_install unconditional, this path's message depends on
        whether the HOST has TouchDesigner installed, so an unstubbed
        test passes on the dev box and fails on every CI runner.
        """
        with patch.object(bridge, 'find_td_pid', return_value=None),              patch.object(bridge, 'find_td_installs', return_value=[]):
            state = self._make_state(config={})
            result = bridge.handle_launch_td({}, state)
        self.assertEqual(result['status'], 'error')
        self.assertIn('touchdesigner', result['message'].lower())

    def test_launch_td_uses_newest_discovered_install_when_unpinned(self):
        """A-14 fresh-clone path at the launch_td level: no pin, no
        configured exe -> discovery still supplies the executable."""
        with patch.object(bridge, 'find_td_pid', return_value=None),              patch.object(bridge, 'find_td_installs',
                          return_value=[('2025.32660', '/fake/TD.app'),
                                        ('2024.30000', '/fake/TD-old.app')]),              patch.object(bridge, 'load_project_config', return_value={}):
            state = self._make_state(config={})
            result = bridge.handle_launch_td({}, state)
        # No toe_path configured, so it stops there -- past exe resolution,
        # which is the point: discovery was consulted and succeeded.
        self.assertEqual(result['status'], 'error')
        self.assertIn('toe_path', result['message'])

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
        """A configured exe that does not exist, and nothing discoverable.

        find_td_installs stubbed empty for the same host-coupling reason
        as test_launch_td_no_executable.
        """
        state = self._make_state(
            config={'td_executable': '/nonexistent/TD.app', 'toe_path': 'test.toe'},
            config_path='/tmp/.embody/envoy.json')
        with patch.object(bridge, 'find_td_pid', return_value=None),              patch.object(bridge, 'find_td_installs', return_value=[]):
            result = bridge.handle_launch_td({}, state)
        self.assertEqual(result['status'], 'error')
        self.assertIn('touchdesigner', result['message'].lower())

    def test_launch_td_reports_discovered_exe_that_vanished(self):
        """Defence in depth: select_td_install hands back a path that no
        longer exists -> launch_td's own validation refuses it."""
        state = self._make_state(
            config={'toe_path': 'test.toe'},
            config_path='/tmp/.embody/envoy.json')
        with patch.object(bridge, 'find_td_pid', return_value=None),              patch.object(bridge, 'select_td_install',
                          return_value=('/vanished/TouchDesigner.exe', None)):
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

    def test_disk_cache_is_augmented_with_current_convoy_tools(self):
        # Regression: a cache written by an older bridge (no convoy_* tools,
        # and a stale get_convoy_status description) was served verbatim,
        # hiding every convoy_* tool for a whole TD-closed session.
        stale = [
            {'name': 'create_op', 'description': 'create'},
            {'name': 'get_convoy_status', 'description': 'OLD stale routing'},
        ]
        with patch.object(bridge, 'load_tools_cache', return_value=stale):
            response = bridge.best_available_tools_list(7, '/fake/envoy.json')
        self.assertEqual(response['id'], 7)
        tools = response['result']['tools']
        names = [t['name'] for t in tools]
        self.assertIn('create_op', names)          # TD tool preserved
        self.assertIn('convoy_call', names)         # convoy_* now present
        self.assertIn('convoy_select_node', names)
        self.assertIn('get_td_status', names)
        # A stale copy of a bridge meta-tool is replaced by the current one.
        self.assertEqual(names.count('get_convoy_status'), 1)
        current = next(t for t in tools if t['name'] == 'get_convoy_status')
        self.assertNotEqual(current.get('description'), 'OLD stale routing')

    def test_disk_cache_absent_falls_back_to_bridge_only(self):
        with patch.object(bridge, 'load_tools_cache', return_value=None):
            response = bridge.best_available_tools_list(9, '/fake/envoy.json')
        names = {t['name'] for t in response['result']['tools']}
        self.assertIn('get_td_status', names)
        self.assertNotIn('create_op', names)

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

    def _pinned_state(self):
        state = bridge.BridgeState(
            url='http://127.0.0.1:9870/mcp', config=dict(self.REG),
            config_path='/fake/envoy.json', active_name='A',
            pinned_instance='A', seen_epoch=0)
        state.convoy_target = {
            'target_host_id': 'host-remote', 'convoy_id': 'studio',
            'target_node_id': 'node-remote'}
        return state

    def test_switch_instance_clears_active_convoy_pin(self):
        # Regression: switch_instance returned success while the ordinary-tool
        # relay branch still routed to the pinned REMOTE node, so a follow-up
        # delete_op / import_network executed on the remote machine.
        state = self._pinned_state()
        with patch.object(bridge, 'load_config', return_value=dict(self.REG)), \
             patch.object(bridge, 'ping_envoy_port', return_value=True), \
             patch.object(bridge, 'is_td_process_alive', return_value=True), \
             patch.object(bridge, 'notify_stdout', lambda *a, **k: None), \
             patch.object(bridge, '_publish_convoy_controller',
                          return_value={'ok': True}) as release:
            result = bridge.handle_switch_instance({'instance': 'B'}, state)
        self.assertEqual(result.get('status'), 'success')
        self.assertIsNone(state.convoy_target)
        self.assertEqual(result['cleared_convoy_pin']['target_node_id'],
                         'node-remote')
        self.assertIn('Cleared the active Convoy pin', result['message'])
        release.assert_called_once()
        self.assertEqual(release.call_args.args[0]['target_host_id'],
                         'host-remote')
        self.assertTrue(release.call_args.kwargs.get('clear_selected'))

    def test_switch_list_mode_leaves_convoy_pin_intact(self):
        state = self._pinned_state()
        with patch.object(bridge, 'load_config', return_value=dict(self.REG)), \
             patch.object(bridge, 'ping_envoy_port', return_value=True), \
             patch.object(bridge, 'is_td_process_alive', return_value=True):
            result = bridge.handle_switch_instance({}, state)
        self.assertEqual(result.get('status'), 'list')
        self.assertIsNotNone(state.convoy_target)


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


# =====================================================================
# Mac-by-construction (D-5): every platform-conditional branch runs on
# EVERY machine via parameter injection, and the macOS CI runner
# executes the POSIX ones for real. No @patch('envoy_bridge.sys') --
# that module-mock pattern already failed once in a full-suite run.
# =====================================================================

class TestPlatformInstallDiscovery(EmbodyTestCase):
    """find_td_installs on all three platforms, against fixture trees."""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix='td_installs_')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        super().tearDown()

    def _make_app(self, name, build, with_exe=True, plist_key=
                  'CFBundleShortVersionString'):
        """Build a fake macOS TouchDesigner.app bundle."""
        import plistlib
        app = os.path.join(self.tmp, name)
        contents = os.path.join(app, 'Contents')
        macos = os.path.join(contents, 'MacOS')
        os.makedirs(macos, exist_ok=True)
        if build is not None:
            with open(os.path.join(contents, 'Info.plist'), 'wb') as f:
                plistlib.dump({plist_key: build}, f)
        if with_exe:
            with open(os.path.join(macos, 'TouchDesigner'), 'w') as f:
                f.write('#!/bin/sh\n')
        return app

    def test_darwin_discovers_bundles_newest_first(self):
        self._make_app('TouchDesigner.app', '2024.30000')
        self._make_app('TouchDesigner 2025.app', '2025.32660')
        found = bridge.find_td_installs(platform='darwin', root=self.tmp)
        self.assertEqual([b for b, _e in found],
                         ['2025.32660', '2024.30000'],
                         'newest build must sort first')
        self.assertTrue(found[0][1].endswith('TouchDesigner 2025.app'),
                        'the .app BUNDLE is the launch target on macOS')

    def test_darwin_skips_bundle_without_executable(self):
        """A partially-removed .app must never be handed to `open -a`."""
        self._make_app('TouchDesigner.app', '2025.32660', with_exe=False)
        found = bridge.find_td_installs(platform='darwin', root=self.tmp)
        self.assertEqual(found, [])

    def test_darwin_skips_bundle_with_unparseable_build(self):
        self._make_app('TouchDesigner.app', '11600')      # not YYYY.NNNNN
        self._make_app('TouchDesigner 2.app', None)       # no Info.plist
        found = bridge.find_td_installs(platform='darwin', root=self.tmp)
        self.assertEqual(found, [])

    def test_read_macos_app_build_falls_back_to_bundle_version(self):
        app = self._make_app('TouchDesigner.app', '2025.32660',
                             plist_key='CFBundleVersion')
        self.assertEqual(bridge._read_macos_app_build(app), '2025.32660')

    def test_read_macos_app_build_survives_corrupt_plist(self):
        app = self._make_app('TouchDesigner.app', None)
        with open(os.path.join(app, 'Contents', 'Info.plist'), 'w') as f:
            f.write('not a plist at all')
        self.assertIsNone(bridge._read_macos_app_build(app),
                          'a corrupt Info.plist must return None, not raise')

    def test_win32_discovers_versioned_dirs(self):
        d = os.path.join(self.tmp, 'TouchDesigner.2025.32660', 'bin')
        os.makedirs(d)
        with open(os.path.join(d, 'TouchDesigner.exe'), 'w') as f:
            f.write('x')
        found = bridge.find_td_installs(platform='win32', root=self.tmp)
        self.assertEqual([b for b, _e in found], ['2025.32660'])
        self.assertTrue(found[0][1].endswith('TouchDesigner.exe'))

    def test_win32_falls_back_to_099_exe_name(self):
        d = os.path.join(self.tmp, 'TouchDesigner.2025.32660', 'bin')
        os.makedirs(d)
        with open(os.path.join(d, 'TouchDesigner099.exe'), 'w') as f:
            f.write('x')
        found = bridge.find_td_installs(platform='win32', root=self.tmp)
        self.assertTrue(found[0][1].endswith('TouchDesigner099.exe'))

    def test_linux_discovers_tarball_layout(self):
        d = os.path.join(self.tmp, 'touchdesigner-2025.32660', 'bin')
        os.makedirs(d)
        with open(os.path.join(d, 'TouchDesigner'), 'w') as f:
            f.write('x')
        found = bridge.find_td_installs(platform='linux', root=self.tmp)
        self.assertEqual([b for b, _e in found], ['2025.32660'])

    def test_default_roots_per_platform(self):
        """_td_install_roots is the path production actually takes -- every
        other test injects root=, so without this it has zero coverage."""
        win = bridge._td_install_roots('win32')
        # LITERAL, not os.path.join: this asserts a WINDOWS path, and
        # os.sep is '/' on the macOS CI runner -- which is exactly how
        # this test failed on its first real cross-platform run. A test
        # for platform-independence must not itself depend on the host.
        self.assertEqual(win, ['C:\\Program Files\\Derivative'],
                         'the win32 default must match the shipped path')
        mac = bridge._td_install_roots('darwin')
        self.assertIn('/Applications', mac)
        self.assertTrue(
            any(m.endswith('Applications') and m != '/Applications'
                for m in mac),
            'the user Applications folder must also be scanned')
        self.assertEqual(bridge._td_install_roots('linux'),
                         ['/opt/derivative'])

    def test_darwin_uses_cfbundleexecutable_not_a_hardcoded_name(self):
        """PANEL BLOCKER pin: the bundle executable name comes from
        Info.plist CFBundleExecutable, never a hardcoded basename -- a real
        .app whose executable is named differently must still be
        discovered. A fixture that hardcodes the name can never prove
        this, so this fixture deliberately uses a DIFFERENT name."""
        import plistlib
        app = os.path.join(self.tmp, 'TouchDesigner.app')
        macos = os.path.join(app, 'Contents', 'MacOS')
        os.makedirs(macos)
        with open(os.path.join(app, 'Contents', 'Info.plist'), 'wb') as f:
            plistlib.dump({'CFBundleShortVersionString': '2025.32660',
                           'CFBundleExecutable': 'TouchDesignerRenamed'}, f)
        with open(os.path.join(macos, 'TouchDesignerRenamed'), 'w') as f:
            f.write('#!/bin/sh\n')

        found = bridge.find_td_installs(platform='darwin', root=self.tmp)
        self.assertEqual([b for b, _e in found], ['2025.32660'],
                         'discovery must follow CFBundleExecutable')
        self.assertEqual(
            bridge._macos_app_executable(app),
            os.path.join(macos, 'TouchDesignerRenamed'))

    def test_darwin_falls_back_to_any_executable_when_plist_lacks_key(self):
        """No CFBundleExecutable -> fall back to whatever is in
        Contents/MacOS rather than refusing the install."""
        app = self._make_app('TouchDesigner.app', '2025.32660')
        os.rename(os.path.join(app, 'Contents', 'MacOS', 'TouchDesigner'),
                  os.path.join(app, 'Contents', 'MacOS', 'SomethingElse'))
        found = bridge.find_td_installs(platform='darwin', root=self.tmp)
        self.assertEqual([b for b, _e in found], ['2025.32660'])

    def test_empty_root_yields_no_installs_on_every_platform(self):
        for plat in ('darwin', 'win32', 'linux'):
            self.assertEqual(
                bridge.find_td_installs(platform=plat, root=self.tmp), [],
                f'{plat}: an empty root must yield no installs')


class TestProcessIdentityInjected(EmbodyTestCase):
    """_process_is_real_td / find_all_td_pids POSIX paths, on any OS."""

    def _ps(self, mapping, returncode=0):
        """Fake subprocess.run keyed by the pid in the argv."""
        def _run(args, **kwargs):
            if args[0] == 'ps':
                pid = args[args.index('-p') + 1]
                out = mapping.get(pid, '')
                return MagicMock(
                    returncode=0 if out else 1, stdout=out, stderr='')
            if args[0] == 'pgrep':
                return MagicMock(returncode=returncode,
                                 stdout=mapping.get('pgrep', ''), stderr='')
            return MagicMock(returncode=1, stdout='', stderr='')
        return _run

    def test_real_td_accepts_bundle_and_bin_paths(self):
        run = self._ps({
            '100': 'S /Applications/TouchDesigner.app/Contents/MacOS/TouchDesigner',
            '200': 'S /opt/derivative/touchdesigner-2025.32660/bin/TouchDesigner',
        })
        self.assertTrue(bridge._process_is_real_td(100, run=run))
        self.assertTrue(bridge._process_is_real_td(200, run=run))

    def test_real_td_rejects_zombie(self):
        run = self._ps({
            '300': 'Z /Applications/TouchDesigner.app/Contents/MacOS/TouchDesigner',
        })
        self.assertFalse(
            bridge._process_is_real_td(300, run=run),
            'a defunct/zombie process must never read as a live TD')

    def test_real_td_rejects_impostor_and_empty(self):
        run = self._ps({'400': 'S /bin/zsh'})
        self.assertFalse(bridge._process_is_real_td(400, run=run))
        self.assertFalse(bridge._process_is_real_td(999, run=run),
                         'no ps output -> not a TD')

    def test_real_td_query_is_pid_scoped_and_unwrapped(self):
        seen = {}

        def _run(args, **kwargs):
            seen['argv'] = args
            return MagicMock(returncode=1, stdout='', stderr='')

        bridge._process_is_real_td(12345, run=_run)
        self.assertEqual(seen['argv'][0], 'ps')
        self.assertIn('-p', seen['argv'])
        self.assertIn('12345', seen['argv'])
        self.assertIn(
            '-ww', seen['argv'],
            'BSD/macOS ps truncates the args column to ~80 cols when piped '
            'without -ww, which silently defeats the helper and port '
            'filters on Mac')

    def test_find_all_td_pids_posix_filters_everything(self):
        """One test replacing five that used to skip on Windows."""
        me = os.getpid()
        run = self._ps({
            'pgrep': f'{me}\n100\n200\n300\n400\n',
            '100': 'S /Applications/TouchDesigner.app/Contents/MacOS/TouchDesigner',
            '200': 'S /Applications/TouchDesigner.app/Contents/MacOS/TouchDesigner',
            '300': 'S /Applications/TouchDesigner.app/Contents/MacOS/TouchDesigner',
            '400': 'S /bin/zsh',
        })
        cmdlines = {
            100: '/Applications/TouchDesigner.app/Contents/MacOS/TouchDesigner proj.toe',
            200: 'python /repo/.embody/envoy-bridge.py --port 9870',
            300: '/Applications/TouchDesigner.app/Contents/MacOS/TouchDesigner --type=gpu-process',
            400: '/bin/zsh -c "edit TouchDesigner notes"',
        }
        with patch.object(bridge, '_process_cmdline',
                          side_effect=lambda pid, **kw: cmdlines.get(pid, '')):
            pids = bridge.find_all_td_pids(platform='darwin', run=run)
        self.assertEqual(
            pids, [100],
            'must drop self, bridges (200), CEF helpers (300) and '
            'impostors (400)')

    def test_find_all_td_pids_posix_empty_on_pgrep_no_match(self):
        """pgrep exits non-zero when nothing matches -> no pids.
        (Behavior preserved from a deleted skip-guarded test.)"""
        run = self._ps({'pgrep': ''}, returncode=1)
        self.assertEqual(bridge.find_all_td_pids(platform='darwin', run=run), [])

    def test_find_all_td_pids_posix_empty_on_timeout(self):
        """A pgrep that times out must yield [], never raise.
        (Behavior preserved from a deleted skip-guarded test.)"""
        def _run(args, **kwargs):
            raise subprocess.TimeoutExpired(cmd=args, timeout=5)
        self.assertEqual(
            bridge.find_all_td_pids(platform='darwin', run=_run), [])

    def test_find_all_td_pids_posix_empty_when_pgrep_missing(self):
        """No pgrep binary (FileNotFoundError) must yield [], never raise.
        (Behavior preserved from a deleted skip-guarded test.)"""
        def _run(args, **kwargs):
            raise FileNotFoundError('pgrep')
        self.assertEqual(
            bridge.find_all_td_pids(platform='darwin', run=_run), [])

    def test_find_all_td_pids_win32_uses_tasklist(self):
        seen = {}

        def _run(args, **kwargs):
            seen['argv'] = args
            return MagicMock(
                returncode=0,
                stdout='"TouchDesigner.exe","111","Console","1","2 K"\n'
                       '"TouchDesignerWebRender.exe","222","Console","1","2 K"\n',
                stderr='')

        pids = bridge.find_all_td_pids(platform='win32', run=_run)
        self.assertEqual(seen['argv'][0], 'tasklist')
        self.assertEqual(pids, [111],
                         'the WebRender helper must never count as an instance')

    def test_process_cmdline_prefers_proc_when_present(self):
        tmp = tempfile.mkdtemp(prefix='procroot_')
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        os.makedirs(os.path.join(tmp, '4242'))
        with open(os.path.join(tmp, '4242', 'cmdline'), 'w') as f:
            f.write('td\x00--port\x009870\x00')

        def _run(args, **kwargs):
            raise AssertionError('ps must not run when /proc has the answer')

        out = bridge._process_cmdline(4242, run=_run, proc_root=tmp)
        self.assertEqual(out, 'td --port 9870 ',
                         'NUL separators become spaces')

    def test_process_cmdline_uses_ps_with_unlimited_width(self):
        seen = {}

        def _run(args, **kwargs):
            seen['argv'] = args
            return MagicMock(returncode=0, stdout='the cmdline', stderr='')

        tmp = tempfile.mkdtemp(prefix='procempty_')
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        out = bridge._process_cmdline(4242, run=_run, proc_root=tmp)
        self.assertEqual(out, 'the cmdline')
        self.assertIn('-ww', seen['argv'])


class TestWatchdogRetryIsBoundedNotRecursive(EmbodyTestCase):
    """The failure handler used to call itself, growing the stack until
    RecursionError killed the thread silently. It now returns False and a
    bounded wrapper loops -- bounded because this thread's job is to call
    os._exit(0), so a permanently broken poll must stop, not spin."""

    def test_bound_constant_is_sane(self):
        self.assertIsInstance(bridge.WATCHDOG_MAX_FAILURES, int)
        self.assertGreater(bridge.WATCHDOG_MAX_FAILURES, 1)

    def test_watchdog_source_loops_and_never_self_recurses(self):
        """Read the FILE, not inspect.getsource: this module stubs
        start_orphan_watchdog to a no-op at import (so no daemon thread
        ever spawns during tests), and getsource would happily inspect
        the stub and pass vacuously."""
        with open(_bridge_path, encoding='utf-8') as f:
            file_src = f.read()
        start = file_src.index('def start_orphan_watchdog')
        end = file_src.index('def wait_for_envoy', start)
        region = file_src[start:end]
        parts = region.split('def watchdog_forever', 1)
        self.assertEqual(len(parts), 2, 'the bounded wrapper must exist')
        self.assertNotIn(
            '\n            watchdog()', parts[0],
            'the failure handler must NOT call watchdog() recursively -- '
            'that grew the stack to RecursionError and killed the thread '
            'silently')
        self.assertIn('while failures < max_failures', parts[1],
                      'the wrapper must loop with a bound')

    def test_wrapper_gives_up_after_max_failures(self):
        """Mirror of the shipped wrapper driven by an always-failing probe:
        it must stop at the bound instead of looping forever. (The shipped
        closure needs a live stdin fd, which a test must never touch.)"""
        calls = {'n': 0}

        def probe():
            calls['n'] += 1
            return False

        def watchdog_forever(max_failures=bridge.WATCHDOG_MAX_FAILURES):
            failures = 0
            while failures < max_failures:
                if probe():
                    return
                failures += 1

        watchdog_forever()
        self.assertEqual(calls['n'], bridge.WATCHDOG_MAX_FAILURES,
                         'must stop exactly at the bound')


class TestStaleBridgePortMatching(EmbodyTestCase):
    """_cmdline_targets_port: the exact-match fix for a REAL bug.

    The old test was `"--port" in cmdline and str(port) in cmdline`, so
    cleaning up port 9870 also matched a bridge on 19870 or 98700 -- and
    stale-bridge cleanup SIGTERMs what it matches, so a peer session's
    bridge could be killed.
    """

    def test_exact_token_match_accepted(self):
        self.assertTrue(bridge._cmdline_targets_port(
            'python envoy-bridge.py --port 9870 --config x', 9870))

    def test_equals_form_rejected_because_the_parser_rejects_it(self):
        """--port=9870 must NOT match: the bridge's own parse_args accepts
        only the space form, so a bridge invoked that way is really running
        on the DEFAULT port -- matching it would kill the wrong process.
        The matcher mirrors the parser exactly."""
        self.assertFalse(bridge._cmdline_targets_port(
            'python envoy-bridge.py --port=9870', 9870))

    def test_nul_separated_proc_form_accepted(self):
        self.assertTrue(bridge._cmdline_targets_port(
            'python\x00envoy-bridge.py\x00--port\x009870\x00', 9870))

    def test_longer_port_never_matches(self):
        for other in ('19870', '98700', '9871'):
            self.assertFalse(
                bridge._cmdline_targets_port(f'bridge --port {other}', 9870),
                f'port {other} must not match a cleanup targeting 9870')

    def test_flag_without_value_never_matches(self):
        self.assertFalse(bridge._cmdline_targets_port('bridge --port', 9870))
        self.assertFalse(bridge._cmdline_targets_port('', 9870))


class TestQuitTdForceKillPosix(EmbodyTestCase):
    """The POSIX force path -- previously untestable on Windows because
    signal.SIGKILL does not exist there (AttributeError, uncaught)."""

    def test_darwin_escalates_sigterm_then_sigkill_same_pid(self):
        """SIGTERM ignored -> escalate to the force signal, same pid.

        Escalation is asserted by CALL SEQUENCE, not by comparing the two
        signal values: on Windows `_SIGKILL` falls back to SIGTERM (there
        is no SIGKILL), so the values are equal there and only the
        sequence distinguishes graceful from force. On macOS/Linux the
        extra identity assertion below does run.
        """
        import signal
        kills = []
        state = {'killed': False}

        def mock_alive(pid):
            if pid != 12345:
                return False
            return not state['killed']

        def fake_kill(pid, sig):
            kills.append((pid, sig))
            # Only the SECOND signal (the force escalation) ends it.
            if len(kills) >= 2:
                state['killed'] = True

        with patch.object(bridge, 'is_process_alive', side_effect=mock_alive), \
             patch('subprocess.run'), \
             patch('os.kill', side_effect=fake_kill):
            success, msg = bridge.quit_td(
                12345, graceful_timeout=15,
                clock=_slow_clock(100.0, step=5.0), sleep=lambda s: None,
                platform='darwin')

        self.assertTrue(success)
        self.assertIn('force-killed', msg)
        self.assertEqual(
            [p for p, _s in kills], [12345, 12345],
            'both signals must target exactly the requested pid -- never a '
            'process group, never an app-wide quit')
        self.assertEqual(kills[0][1], signal.SIGTERM,
                         'the graceful attempt is SIGTERM')
        self.assertEqual(kills[-1][1], bridge._SIGKILL,
                         'the escalation uses the force constant')
        if hasattr(signal, 'SIGKILL'):
            self.assertNotEqual(
                kills[0][1], kills[-1][1],
                'on a real POSIX host the two signals must differ')

    def test_sigkill_constant_is_defined_on_every_platform(self):
        self.assertIsNotNone(bridge._SIGKILL)


class TestLaunchTdDarwinSpawn(EmbodyTestCase):
    """The darwin spawn path: argv, fail-fast, and pid honesty."""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix='launch_darwin_')
        self.app = os.path.join(self.tmp, 'TouchDesigner.app')
        os.makedirs(self.app)
        self.toe = os.path.join(self.tmp, 'proj.toe')
        with open(self.toe, 'w') as f:
            f.write('x')
        self.embody_dir = os.path.join(self.tmp, '.embody')
        os.makedirs(self.embody_dir)
        self.config_path = os.path.join(self.embody_dir, 'envoy.json')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        super().tearDown()

    def _popen(self, returncode=0, record=None):
        def _p(argv, **kwargs):
            if record is not None:
                record.append(argv)
            proc = MagicMock()
            proc.wait.return_value = None
            proc.returncode = returncode
            proc.pid = 4242
            return proc
        return _p

    def test_spawns_open_n_a_and_resolves_pid_by_diff(self):
        argv = []
        with patch.object(bridge, 'select_td_install',
                          return_value=(self.app, None)), \
             patch.object(bridge, 'wait_for_new_td_pid', return_value=777):
            ok, msg, pid = bridge.launch_td(
                {}, self.config_path, project_path=self.toe,
                existing_pids={1, 2}, platform='darwin',
                popen=self._popen(record=argv))

        self.assertTrue(ok)
        self.assertEqual(pid, 777)
        self.assertEqual(
            argv[0], ['open', '-n', '-a', self.app, self.toe],
            "-n is load-bearing: without it LaunchServices reuses an "
            "existing window and multi-instance breaks")

    def test_nonzero_open_exit_fails_fast(self):
        called = []
        with patch.object(bridge, 'select_td_install',
                          return_value=(self.app, None)), \
             patch.object(bridge, 'wait_for_new_td_pid',
                          side_effect=lambda *a, **k: called.append(1)):
            ok, msg, pid = bridge.launch_td(
                {}, self.config_path, project_path=self.toe,
                platform='darwin', popen=self._popen(returncode=1))

        self.assertFalse(ok)
        self.assertIsNone(pid)
        self.assertIn('did not launch', msg)
        self.assertEqual(called, [],
                         'a failed `open` must not burn the pid-poll window')

    def test_unresolved_pid_never_claims_pid_none(self):
        with patch.object(bridge, 'select_td_install',
                          return_value=(self.app, None)), \
             patch.object(bridge, 'wait_for_new_td_pid', return_value=None):
            ok, msg, pid = bridge.launch_td(
                {}, self.config_path, project_path=self.toe,
                platform='darwin', popen=self._popen())

        self.assertTrue(ok, 'open succeeded -- TD is coming up')
        self.assertIsNone(pid)
        self.assertNotIn(
            'PID None', msg,
            'reporting a literal "PID None" as success is a lie; say the '
            'pid is unresolved instead')

    def test_pid_phrase_never_prints_none(self):
        """The honesty fix must hold at the layer the USER sees, not just
        inside launch_td."""
        self.assertEqual(bridge._pid_phrase(4242), 'PID 4242')
        self.assertNotIn('None', bridge._pid_phrase(None))
        self.assertIn('not yet resolved', bridge._pid_phrase(None))

    def test_win32_spawns_executable_directly(self):
        argv = []
        exe = os.path.join(self.tmp, 'TouchDesigner.exe')
        with open(exe, 'w') as f:
            f.write('x')
        with patch.object(bridge, 'select_td_install',
                          return_value=(exe, None)):
            ok, msg, pid = bridge.launch_td(
                {}, self.config_path, project_path=self.toe,
                platform='win32', popen=self._popen(record=argv))

        self.assertTrue(ok)
        self.assertEqual(pid, 4242, 'win32 gets a real pid from Popen')
        self.assertEqual(argv[0], [exe, self.toe])


class TestConvoyProbe(EmbodyTestCase):
    """The Phase 1 probe-only slice: the bridge reports whether a Convoy
    host app is present (running/absent/stale) and NEVER changes routing.
    The decision tree is isolated from the filesystem by patching the
    portfile read, pid liveness, and /health check -- mirroring the shape
    of convoy_hostprobe's own tests."""

    def _portfile(self, **kw):
        base = {'port': 59991, 'pid': 4242, 'host_id': 'abc123'}
        base.update(kw)
        return base

    def test_absent_when_no_portfile(self):
        with patch.object(bridge, '_read_convoy_portfile', return_value=None):
            r = bridge.probe_convoy_host()
        self.assertEqual(r['convoy'], 'absent')
        # the CLEAN absent path, not the outer error fallback -- so a
        # regression that routed here via an exception would be caught
        self.assertIn('no Convoy host app', r['detail'])

    def test_absent_when_data_dir_undeterminable(self):
        with patch.object(bridge, 'convoy_data_dir', return_value=None):
            r = bridge.probe_convoy_host()
        self.assertEqual(r['convoy'], 'absent')
        # must reach the clean guard, not the try/except error path
        self.assertIn('no Convoy host app', r['detail'])

    def test_stale_when_writer_is_dead(self):
        with patch.object(bridge, '_read_convoy_portfile',
                          return_value=self._portfile()), \
             patch.object(bridge, 'is_process_alive', return_value=False):
            r = bridge.probe_convoy_host()
        self.assertEqual(r['convoy'], 'stale')
        self.assertIn('writer not alive', r['detail'])

    def test_stale_when_health_unreachable(self):
        """A live pid whose port does not answer /health is not trusted --
        a recycled pid could hold it."""
        with patch.object(bridge, '_read_convoy_portfile',
                          return_value=self._portfile()), \
             patch.object(bridge, 'is_process_alive', return_value=True), \
             patch.object(bridge, '_convoy_health_host_id',
                          return_value=None):
            r = bridge.probe_convoy_host()
        self.assertEqual(r['convoy'], 'stale')
        self.assertIn('/health', r['detail'])

    def test_stale_on_recycled_port_identity_mismatch(self):
        with patch.object(bridge, '_read_convoy_portfile',
                          return_value=self._portfile(host_id='expected')), \
             patch.object(bridge, 'is_process_alive', return_value=True), \
             patch.object(bridge, '_convoy_health_host_id',
                          return_value='someone-else'):
            r = bridge.probe_convoy_host()
        self.assertEqual(r['convoy'], 'stale')
        self.assertIn('recycled', r['detail'])

    def test_running_when_identity_confirmed(self):
        with patch.object(bridge, '_read_convoy_portfile',
                          return_value=self._portfile(host_id='abc123',
                                                      port=59991)), \
             patch.object(bridge, 'is_process_alive', return_value=True), \
             patch.object(bridge, '_convoy_health_host_id',
                          return_value='abc123'):
            r = bridge.probe_convoy_host()
        self.assertEqual(r['convoy'], 'running')
        self.assertEqual(r['host_id'], 'abc123')
        self.assertEqual(r['port'], 59991)
        self.assertTrue(r['identity_confirmed'])
        self.assertIn('Explicit convoy_* tools', r['detail'])
        self.assertIn('ordinary Envoy tools retain', r['detail'])

    def test_running_when_portfile_has_no_host_id(self):
        """A portfile without a host_id cannot be identity-checked; the
        probe trusts a live /health (matching convoy_hostprobe's
        short-circuit) -- pinned so a regression that made identity the
        ONLY gate is a deliberate change, not an accident."""
        pf = self._portfile()
        pf.pop('host_id')
        with patch.object(bridge, '_read_convoy_portfile', return_value=pf), \
             patch.object(bridge, 'is_process_alive', return_value=True), \
             patch.object(bridge, '_convoy_health_host_id',
                          return_value='whatever-it-reports'):
            r = bridge.probe_convoy_host()
        self.assertEqual(r['convoy'], 'running')
        self.assertEqual(r['host_id'], 'whatever-it-reports')
        self.assertFalse(r['identity_confirmed'])
        self.assertIn('legacy portfile', r['detail'])

    def test_non_integer_pid_is_stale_not_a_probe_error(self):
        with patch.object(bridge, '_read_convoy_portfile',
                          return_value=self._portfile(pid='not-a-number')):
            r = bridge.probe_convoy_host()
        self.assertEqual(r['convoy'], 'stale')
        self.assertIn('writer not alive', r['detail'])

    def test_probe_never_raises_even_on_an_unexpected_failure(self):
        """The contract is "never raises" -- the bridge coming up must not
        be broken by a probe fault. Force an unexpected exception deep in
        the health check and prove the probe still returns a dict, not a
        traceback."""
        with patch.object(bridge, '_read_convoy_portfile',
                          return_value=self._portfile()), \
             patch.object(bridge, 'is_process_alive', return_value=True), \
             patch.object(bridge, '_convoy_health_host_id',
                          side_effect=Exception('unexpected')):
            try:
                r = bridge.probe_convoy_host()
                raised = False
            except Exception:
                raised = True
        self.assertFalse(raised, 'probe_convoy_host must never raise')
        # and it degrades to a USABLE dict (main() does _convoy.get(...))
        self.assertIsInstance(r, dict)
        self.assertEqual(r['convoy'], 'absent')

    def test_data_dir_win32_uses_localappdata_and_backslashes(self):
        with patch.object(bridge.sys, 'platform', 'win32'), \
             patch.dict(bridge.os.environ,
                        {'LOCALAPPDATA': r'C:\Users\x\AppData\Local'}):
            d = bridge.convoy_data_dir()
        # ntpath.join is used for the win32 target regardless of host OS,
        # so the separator shape is validated even on a POSIX CI runner
        self.assertEqual(d, r'C:\Users\x\AppData\Local\EmbodyConvoy')

    def test_data_dir_darwin_uses_application_support_with_slashes(self):
        with patch.object(bridge.sys, 'platform', 'darwin'), \
             patch.object(bridge.os.path, 'expanduser',
                          return_value='/Users/x'):
            d = bridge.convoy_data_dir()
        self.assertEqual(
            d, '/Users/x/Library/Application Support/EmbodyConvoy')

    def test_data_dir_linux_uses_xdg_state_home(self):
        with patch.object(bridge.sys, 'platform', 'linux'), \
             patch.dict(bridge.os.environ, {'XDG_STATE_HOME': '/home/x/.state'}):
            d = bridge.convoy_data_dir()
        self.assertEqual(d, '/home/x/.state/embody-convoy')

    def test_meta_tool_registered_and_dispatched(self):
        self.assertIn('get_convoy_status', bridge.BRIDGE_TOOL_NAMES)
        with patch.object(bridge, '_read_convoy_portfile', return_value=None):
            content = bridge.handle_bridge_tool('get_convoy_status', {}, None)
        # meta-tools return [{"type":"text","text": <json>}]
        self.assertEqual(content[0]['type'], 'text')
        payload = json.loads(content[0]['text'])
        self.assertEqual(payload['convoy'], 'absent')

    def test_meta_tool_advertised_in_tools_list(self):
        response = {'jsonrpc': '2.0', 'id': 1,
                    'result': {'tools': [{'name': 'query_network'}]}}
        bridge.augment_tools_list(response)
        names = {t['name'] for t in response['result']['tools']}
        self.assertIn('get_convoy_status', names)
        self.assertIn('get_td_status', names, 'existing meta-tools intact')


class _ConvoyResponse:
    """Small urllib response double that records the requested read cap."""

    def __init__(self, payload, status=200):
        self.payload = (payload if isinstance(payload, bytes)
                        else json.dumps(payload).encode('utf-8'))
        self.status = status
        self.read_limits = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self, limit=-1):
        self.read_limits.append(limit)
        return self.payload


class _ConvoyArtifactResponse:
    def __init__(self, data, reference, status=200):
        self.status = status
        self._stream = io.BytesIO(data)
        self._headers = {
            'Content-Length': str(len(data)),
            'X-Convoy-Artifact-ID': reference['artifact_id'],
            'X-Convoy-Content-SHA256': reference['sha256'],
        }

    def getheader(self, name):
        return self._headers.get(name)

    def read(self, size=-1):
        return self._stream.read(size)


class _ConvoyArtifactConnection:
    def __init__(self, response):
        self.response = response
        self.requests = []
        self.sock = MagicMock()
        self.closed = False

    def request(self, method, path, headers=None):
        self.requests.append((method, path, dict(headers or {})))

    def getresponse(self):
        return self.response

    def close(self):
        self.closed = True


class TestConvoyBridgeHostClient(EmbodyTestCase):
    """The bridge -> local host trust boundary.

    These tests pin the order that matters: health identity first, numeric
    port + literal IPv4 loopback next, token read last. They also prove both
    unauthenticated health and authenticated host responses are bounded.
    """

    def _running(self, **overrides):
        result = {
            'convoy': 'running', 'host_id': 'host-local',
            'port': 54321, 'data_dir': 'convoy-state',
            'identity_confirmed': True,
        }
        result.update(overrides)
        return result

    def test_health_probe_is_literal_loopback_token_free_and_bounded(self):
        response = _ConvoyResponse({'ok': True, 'host_id': 'host-local'})
        seen = []

        def open_(request, timeout):
            seen.append((request, timeout))
            return response

        with patch.object(bridge.urllib.request, 'urlopen', side_effect=open_):
            host_id = bridge._convoy_health_host_id(54321)

        self.assertEqual(host_id, 'host-local')
        self.assertEqual(seen[0][0].full_url,
                         'http://127.0.0.1:54321/health')
        headers = {k.lower(): v for k, v in seen[0][0].header_items()}
        self.assertNotIn('x-convoy-host-token', headers)
        self.assertEqual(response.read_limits,
                         [bridge.CONVOY_HEALTH_MAX_BODY_BYTES + 1])

    def test_oversized_health_is_not_an_identity(self):
        response = _ConvoyResponse(
            b'x' * (bridge.CONVOY_HEALTH_MAX_BODY_BYTES + 1))
        with patch.object(bridge.urllib.request, 'urlopen',
                          return_value=response):
            self.assertIsNone(bridge._convoy_health_host_id(54321))

    def test_stale_probe_never_reads_or_sends_token(self):
        with patch.object(bridge, 'probe_convoy_host', return_value={
                'convoy': 'stale', 'detail': 'identity mismatch'}), \
             patch.object(bridge, '_read_convoy_token') as read_token, \
             patch.object(bridge.urllib.request, 'urlopen') as open_:
            result = bridge.convoy_host_call('GET', '/network/nodes')
        self.assertEqual(result['reason'], 'convoy_host_stale')
        read_token.assert_not_called()
        open_.assert_not_called()

    def test_unpinned_legacy_probe_never_reads_or_sends_token(self):
        with patch.object(bridge, 'probe_convoy_host',
                          return_value=self._running(
                              identity_confirmed=False)), \
             patch.object(bridge, '_read_convoy_token') as read_token, \
             patch.object(bridge.urllib.request, 'urlopen') as open_:
            result = bridge.convoy_host_call('GET', '/network/nodes')
        self.assertEqual(result['reason'],
                         'convoy_host_identity_unverified')
        read_token.assert_not_called()
        open_.assert_not_called()

    def test_invalid_port_never_reads_or_sends_token(self):
        with patch.object(bridge, 'probe_convoy_host',
                          return_value=self._running(port='bad')), \
             patch.object(bridge, '_read_convoy_token') as read_token, \
             patch.object(bridge.urllib.request, 'urlopen') as open_:
            result = bridge.convoy_host_call('GET', '/network/nodes')
        self.assertEqual(result['reason'], 'convoy_host_error')
        read_token.assert_not_called()
        open_.assert_not_called()

    def test_authority_shaped_path_is_rejected_before_probe_or_token(self):
        for path in ('//attacker.invalid/steal', '@attacker.invalid/steal',
                     '/safe#https://attacker.invalid'):
            with self.subTest(path=path), \
                 patch.object(bridge, 'probe_convoy_host') as probe, \
                 patch.object(bridge, '_read_convoy_token') as read_token, \
                 patch.object(bridge.urllib.request, 'urlopen') as open_:
                result = bridge.convoy_host_call('GET', path)
            self.assertEqual(result['reason'],
                             'convoy_bridge_invalid_request')
            probe.assert_not_called()
            read_token.assert_not_called()
            open_.assert_not_called()

    def test_authenticated_call_can_only_send_token_to_literal_loopback(self):
        response = _ConvoyResponse({'ok': True, 'nodes': []})
        seen = []

        def open_(request, timeout):
            seen.append((request, timeout))
            return response

        with patch.object(bridge, 'probe_convoy_host',
                          return_value=self._running()), \
             patch.object(bridge, '_read_convoy_token',
                          return_value='ab' * 32), \
             patch.object(bridge.urllib.request, 'urlopen', side_effect=open_):
            result = bridge.convoy_host_call(
                'POST', '/relay', {'operation': 'convoy_ping'}, timeout=2.5)

        self.assertTrue(result['ok'])
        request, timeout = seen[0]
        parsed = bridge.urllib.parse.urlsplit(request.full_url)
        self.assertEqual(parsed.scheme, 'http')
        self.assertEqual(parsed.hostname, '127.0.0.1')
        self.assertEqual(parsed.port, 54321)
        self.assertEqual(parsed.path, '/relay')
        self.assertEqual(timeout, 2.5)
        headers = {k.lower(): v for k, v in request.header_items()}
        self.assertEqual(headers['x-convoy-host-token'], 'ab' * 32)
        self.assertEqual(json.loads(request.data.decode('utf-8')),
                         {'operation': 'convoy_ping'})
        self.assertEqual(response.read_limits,
                         [bridge.CONVOY_HOST_MAX_BODY_BYTES + 1])

    def test_authenticated_response_is_bounded(self):
        response = _ConvoyResponse(
            b'x' * (bridge.CONVOY_HOST_MAX_BODY_BYTES + 1))
        with patch.object(bridge, 'probe_convoy_host',
                          return_value=self._running()), \
             patch.object(bridge, '_read_convoy_token',
                          return_value='ab' * 32), \
             patch.object(bridge.urllib.request, 'urlopen',
                          return_value=response):
            result = bridge.convoy_host_call('GET', '/network/nodes')
        self.assertEqual(result['reason'], 'convoy_host_response_too_large')
        self.assertEqual(response.read_limits,
                         [bridge.CONVOY_HOST_MAX_BODY_BYTES + 1])

    def test_token_reader_is_bounded_and_rejects_trailing_data(self):
        token_file = MagicMock()
        token_file.__enter__.return_value = token_file
        token_file.read.return_value = 'ab' * 32 + '\n'
        with patch('builtins.open', return_value=token_file):
            self.assertEqual(bridge._read_convoy_token('state'), 'ab' * 32)
        token_file.read.assert_called_once_with(66)
        token_file.read.reset_mock()
        token_file.read.return_value = 'ab' * 33
        with patch('builtins.open', return_value=token_file):
            self.assertIsNone(bridge._read_convoy_token('state'))
        token_file.read.assert_called_once_with(66)

    def test_non_object_or_malformed_response_is_named_host_error(self):
        for payload in (b'[]', b'not-json', b'{"value": NaN}'):
            with self.subTest(payload=payload), \
                 patch.object(bridge, 'probe_convoy_host',
                              return_value=self._running()), \
                 patch.object(bridge, '_read_convoy_token',
                              return_value='ab' * 32), \
                 patch.object(bridge.urllib.request, 'urlopen',
                              return_value=_ConvoyResponse(payload)):
                result = bridge.convoy_host_call('GET', '/network/nodes')
            self.assertFalse(result['ok'])
            self.assertEqual(result['reason'], 'convoy_host_error')

    def test_wire_status_overrides_any_status_claim_in_body(self):
        response = _ConvoyResponse(
            {'ok': False, 'http_status': 200, 'reason': 'blocked'},
            status=403)
        with patch.object(bridge, 'probe_convoy_host',
                          return_value=self._running()), \
             patch.object(bridge, '_read_convoy_token',
                          return_value='ab' * 32), \
             patch.object(bridge.urllib.request, 'urlopen',
                          return_value=response):
            result = bridge.convoy_host_call('GET', '/network/nodes')
        self.assertEqual(result['http_status'], 403)


class TestConvoyBridgePublicTools(EmbodyTestCase):
    """Public Convoy MCP semantics, with the host transport mocked."""

    def _call_args(self, **overrides):
        result = {
            'target_host_id': 'host-remote',
            'convoy_id': 'studio',
            'target_node_id': 'node-remote',
            'operation': 'convoy_ping',
            'arguments': {},
            'timeout_s': 2.0,
        }
        result.update(overrides)
        return result

    def _accepted(self, state='queued', updated=10.0):
        return {'ok': True, 'created': True, 'job': {
            'delivery_id': 'cj_123', 'state': state, 'updated': updated}}

    def test_all_public_tools_are_registered_advertised_and_dispatched(self):
        names = {'get_convoy_status', 'convoy_list_nodes',
                 'convoy_select_node', 'convoy_owlette',
                 'convoy_list_controllers',
                 'convoy_ping', 'convoy_start_node',
                 'convoy_restart_node', 'convoy_call', 'convoy_batch',
                 'convoy_get_job', 'convoy_ack_job', 'convoy_get_artifact',
                 'convoy_save_artifact',
                 'convoy_cancel_job', 'convoy_forget_node',
                 'convoy_update_embody'}
        self.assertTrue(names.issubset(bridge.BRIDGE_TOOL_NAMES))
        response = {'result': {'tools': []}}
        bridge.augment_tools_list(response)
        advertised = {t['name'] for t in response['result']['tools']}
        self.assertTrue(names.issubset(advertised))
        with patch.object(bridge, 'handle_convoy_list_nodes',
                          return_value={'ok': True, 'sentinel': 1}):
            content = bridge.handle_bridge_tool(
                'convoy_list_nodes', {'convoy_id': 'studio'}, None)
        self.assertEqual(json.loads(content[0]['text'])['sentinel'], 1)

        handlers = (
            ('convoy_select_node', 'handle_convoy_select_node'),
            ('convoy_owlette', 'handle_convoy_owlette'),
            ('convoy_list_controllers', 'handle_convoy_list_controllers'),
            ('convoy_ping', 'handle_convoy_ping'),
            ('convoy_start_node', 'handle_convoy_start_node'),
            ('convoy_restart_node', 'handle_convoy_restart_node'),
            ('convoy_batch', 'handle_convoy_batch'),
            ('convoy_ack_job', 'handle_convoy_ack_job'),
            ('convoy_get_artifact', 'handle_convoy_get_artifact'),
            ('convoy_save_artifact', 'handle_convoy_save_artifact'),
            ('convoy_cancel_job', 'handle_convoy_cancel_job'),
            ('convoy_forget_node', 'handle_convoy_forget_node'),
            ('convoy_update_embody', 'handle_convoy_update_embody'),
        )
        for tool_name, handler_name in handlers:
            with self.subTest(tool_name=tool_name), \
                 patch.object(bridge, handler_name,
                              return_value={'ok': True, 'sentinel': tool_name}):
                content = bridge.handle_bridge_tool(tool_name, {}, None)
            self.assertEqual(
                json.loads(content[0]['text'])['sentinel'], tool_name)

    def test_surface_does_not_duplicate_structured_host_operations(self):
        for redundant in ('convoy_git', 'convoy_gh', 'convoy_shell'):
            self.assertNotIn(redundant, bridge.BRIDGE_TOOL_NAMES)

    def _state(self):
        return bridge.BridgeState(
            url='http://127.0.0.1:9870/mcp', active_name='local-show')

    def test_select_node_queries_sets_reports_and_clears_session_pin(self):
        state = self._state()
        local = bridge.handle_convoy_select_node({}, state)
        self.assertEqual(local['routing'], 'direct_local')
        self.assertEqual(local['local_instance'], 'local-show')

        row = {
            'host_id': 'host-remote', 'convoy_id': 'studio',
            'node_id': 'node-remote', 'runtime_id': 'rt-7',
            'node_name': 'render', 'hostname': 'machine',
            'status': 'online', 'online': True,
        }
        with patch.object(bridge, 'handle_convoy_list_nodes', return_value={
                'ok': True, 'nodes': [row]}) as list_:
            selected = bridge.handle_convoy_select_node({
                'target_host_id': 'host-remote', 'convoy_id': 'studio',
                'target_node_id': 'node-remote'}, state)
        list_.assert_called_once_with({'convoy_id': 'studio'})
        self.assertEqual(selected['routing'], 'convoy')
        self.assertEqual(selected['selected_node']['expected_runtime_id'],
                         'rt-7')
        self.assertEqual(state.convoy_target['target_node_id'], 'node-remote')

        cleared = bridge.handle_convoy_select_node({'clear': True}, state)
        self.assertEqual(cleared['routing'], 'direct_local')
        self.assertEqual(cleared['previous_node']['target_node_id'],
                         'node-remote')
        self.assertIsNone(state.convoy_target)

    def test_select_node_refusal_never_changes_existing_pin(self):
        state = self._state()
        state.convoy_target = {
            'target_host_id': 'old-host', 'convoy_id': 'old-convoy',
            'target_node_id': 'old-node', 'expected_runtime_id': 'old-rt'}
        with patch.object(bridge, 'handle_convoy_list_nodes', return_value={
                'ok': True, 'nodes': []}):
            missing = bridge.handle_convoy_select_node({
                'target_host_id': 'new-host', 'convoy_id': 'studio',
                'target_node_id': 'new-node'}, state)
        self.assertEqual(missing['reason'], 'target_not_found')
        self.assertTrue(missing['selection_unchanged'])
        self.assertEqual(state.convoy_target['target_host_id'], 'old-host')

        with patch.object(bridge, 'handle_convoy_list_nodes', return_value={
                'ok': True, 'nodes': [{
                    'host_id': 'new-host', 'convoy_id': 'studio',
                    'node_id': 'new-node', 'runtime_id': 'rt-new'}]}):
            stale = bridge.handle_convoy_select_node({
                'target_host_id': 'new-host', 'convoy_id': 'studio',
                'target_node_id': 'new-node',
                'expected_runtime_id': 'rt-old'}, state)
        self.assertEqual(stale['reason'], 'runtime_changed')
        self.assertEqual(state.convoy_target['target_host_id'], 'old-host')

    def test_select_node_rejects_partial_and_ambiguous_clear(self):
        state = self._state()
        for params in (
                {'target_host_id': 'host'},
                {'clear': 'yes'},
                {'clear': True, 'convoy_id': 'studio'}):
            with self.subTest(params=params), \
                 patch.object(bridge, 'handle_convoy_list_nodes') as list_:
                result = bridge.handle_convoy_select_node(params, state)
            self.assertEqual(result['reason'], 'invalid_arguments')
            list_.assert_not_called()

    def test_selected_tool_returns_exact_provenance_and_runtime_guard(self):
        state = self._state()
        state.convoy_target = {
            'target_host_id': 'remote-host', 'convoy_id': 'studio',
            'target_node_id': 'remote-node',
            'expected_runtime_id': 'rt-9'}
        relay = {'ok': True, 'delivery_id': 'cj-9', 'job': {
            'state': 'succeeded', 'result': {'ops': ['/a']}}}
        with patch.object(bridge, 'handle_convoy_call',
                          return_value=relay) as call_:
            result = bridge.handle_convoy_selected_tool(
                'query_network', {'parent_path': '/'}, state)
        sent = call_.call_args.args[0]
        self.assertEqual(sent['target_host_id'], 'remote-host')
        self.assertEqual(sent['target_node_id'], 'remote-node')
        self.assertEqual(sent['expected_runtime_id'], 'rt-9')
        self.assertEqual(sent['operation'], 'query_network')
        self.assertFalse(result['isError'])
        payload = json.loads(result['content'][0]['text'])
        self.assertEqual(payload['convoy_target']['delivery_id'], 'cj-9')
        self.assertEqual(payload['convoy_target']['target_node_id'],
                         'remote-node')
        self.assertEqual(payload['result']['ops'], ['/a'])

    def test_selected_tool_preserves_multiblock_images_and_never_falls_back(self):
        state = self._state()
        state.convoy_target = {
            'target_host_id': 'remote-host', 'convoy_id': 'studio',
            'target_node_id': 'remote-node'}
        blocks = [
            {'type': 'text', 'text': 'capture'},
            {'type': 'image', 'data': 'AAAA', 'mimeType': 'image/png'},
        ]
        with patch.object(bridge, 'handle_convoy_call', return_value={
                'ok': True, 'delivery_id': 'cj-image', 'job': {
                    'state': 'succeeded',
                    'result': {'content': blocks}}}):
            result = bridge.handle_convoy_selected_tool(
                'capture_top', {'op_path': '/out1'}, state)
        self.assertFalse(result['isError'])
        self.assertEqual(result['content'][1:], blocks)
        self.assertEqual(json.loads(result['content'][0]['text'])[
            'convoy_target']['target_host_id'], 'remote-host')

        with patch.object(bridge, 'handle_convoy_call', return_value={
                'ok': False, 'reason': 'peer_unreachable'}):
            failed = bridge.handle_convoy_selected_tool(
                'query_network', {}, state)
        self.assertTrue(failed['isError'])
        failure = json.loads(failed['content'][0]['text'])
        self.assertEqual(failure['relay']['reason'], 'peer_unreachable')
        self.assertEqual(state.convoy_target['target_host_id'], 'remote-host')

    def test_batch_fanout_preserves_target_order_and_partial_results(self):
        params = {
            'targets': [
                {'target_host_id': 'host-a', 'convoy_id': 'studio',
                 'target_node_id': 'node-a', 'expected_runtime_id': 'rt-a'},
                {'target_host_id': 'host-b', 'convoy_id': 'studio',
                 'target_node_id': 'node-b'},
            ],
            'operations': [
                {'tool': 'query_network', 'params': {'parent_path': '/'}},
                {'tool': 'get_op', 'params': {'op_path': '/a'}},
            ],
            'override': False, 'timeout_s': 2, 'wait': True,
        }

        def relay(call_params):
            # A real terminal success carries the native batch_operations
            # payload; the fail-closed classifier refuses to count a bare
            # {'state': 'succeeded'} shell as a native batch success.
            if call_params['target_host_id'] == 'host-a':
                return {'ok': True, 'job': {'state': 'succeeded', 'result': {
                    'success': True, 'count': 2,
                    'results': [{'tool': 'query_network'},
                                {'tool': 'get_op'}]}}}
            return {'ok': False, 'reason': 'peer_unreachable'}

        with patch.object(bridge, 'handle_convoy_call', side_effect=relay) as call_:
            result = bridge.handle_convoy_batch(params)
        self.assertFalse(result['ok'])
        self.assertEqual(result['accepted_count'], 1)
        self.assertEqual(result['terminal_success_count'], 1)
        self.assertFalse(result['atomic'])
        self.assertEqual([row['target']['target_host_id']
                          for row in result['results']], ['host-a', 'host-b'])
        calls = [item.args[0] for item in call_.call_args_list]
        self.assertEqual({item['operation'] for item in calls},
                         {'batch_operations'})
        self.assertTrue(all(item['arguments']['operations'] ==
                            params['operations'] for item in calls))
        self.assertEqual(next(item for item in calls
                              if item['target_host_id'] == 'host-a')[
                                  'expected_runtime_id'], 'rt-a')

    def test_batch_queueing_spends_one_cumulative_deadline(self):
        params = {
            'targets': [
                {'target_host_id': 'host-%02d' % index,
                 'convoy_id': 'studio',
                 'target_node_id': 'node-%02d' % index}
                for index in range(9)
            ],
            'operations': [{'tool': 'query_network', 'params': {}}],
            'timeout_s': 0.15, 'wait': True,
        }
        release = threading.Event()
        visited = []
        guard = threading.Lock()

        def relay(call_params):
            with guard:
                visited.append(call_params['target_host_id'])
            release.wait(1.0)
            return {'ok': True, 'job': {'state': 'succeeded'}}

        timer = threading.Timer(0.11, release.set)
        timer.start()
        try:
            with patch.object(
                    bridge, 'handle_convoy_call', side_effect=relay):
                result = bridge.handle_convoy_batch(params)
        finally:
            release.set()
            timer.join(1.0)

        # Eight targets occupied the hard fanout cap.  The ninth did not
        # receive a fresh 150 ms after that queue wait; it expired under the
        # original batch deadline and never opened another relay request.
        self.assertEqual(len(visited), 8)
        self.assertEqual(result['results'][8]['relay']['reason'],
                         'convoy_batch_timeout')
        self.assertTrue(result['results'][8]['relay']['wait_timed_out'])
        self.assertEqual([row['target']['target_host_id']
                          for row in result['results']],
                         ['host-%02d' % index for index in range(9)])

    def test_batch_rejects_malformed_or_duplicate_work_before_relay(self):
        base = {
            'targets': [{'target_host_id': 'h', 'convoy_id': 'c',
                         'target_node_id': 'n'}],
            'operations': [{'tool': 'query_network', 'params': {}}],
        }
        cases = (
            {**base, 'targets': []},
            {**base, 'operations': []},
            {**base, 'operations': [{'tool': 'batch_operations'}]},
            {**base, 'operations': [{'tool': 'x', 'params': []}]},
            {**base, 'targets': base['targets'] * 2},
            {**base, 'timeout_s': float('nan')},
        )
        for params in cases:
            with self.subTest(params=params), \
                 patch.object(bridge, 'handle_convoy_call') as relay:
                result = bridge.handle_convoy_batch(params)
            self.assertEqual(result['reason'], 'invalid_arguments')
            relay.assert_not_called()

    def test_status_enriches_running_probe_without_waking_touchdesigner(self):
        probe = {'convoy': 'running', 'host_id': 'host-local',
                 'port': 1234, 'identity_confirmed': True}
        host = {'ok': True, 'realm': {'state': 'established'},
                'jobs_queued': 2}
        with patch.object(bridge, 'probe_convoy_host', return_value=probe), \
             patch.object(bridge, 'convoy_host_call', return_value=host) as call_:
            result = bridge.handle_convoy_status({})
        call_.assert_called_once_with('GET', '/status')
        self.assertEqual(result['convoy'], 'running')
        self.assertEqual(result['host_status'], host)
        self.assertEqual(result['scope'], 'local_host')
        self.assertFalse(result['wakes_touchdesigner'])

    def test_status_absent_is_a_non_waking_probe_without_host_call(self):
        probe = {'convoy': 'absent', 'detail': 'not installed'}
        with patch.object(bridge, 'probe_convoy_host', return_value=probe), \
             patch.object(bridge, 'convoy_host_call') as call_:
            result = bridge.handle_convoy_status({})
        call_.assert_not_called()
        self.assertEqual(result['convoy'], 'absent')
        self.assertFalse(result['wakes_touchdesigner'])

    def test_list_nodes_percent_encodes_namespace_and_never_falls_back(self):
        with patch.object(bridge, 'convoy_host_call', return_value={
                'ok': False, 'reason': 'not_found', 'http_status': 404}) as call_:
            result = bridge.handle_convoy_list_nodes({'convoy_id': 'A/B + C'})
        self.assertEqual(result['reason'], 'not_found')
        call_.assert_called_once_with(
            'GET', '/network/nodes?convoy_id=A%2FB%20%2B%20C')

    def test_list_nodes_rejects_bad_namespace_without_network(self):
        for value in ('', 1, True):
            with self.subTest(value=value), \
                 patch.object(bridge, 'convoy_host_call') as call_:
                result = bridge.handle_convoy_list_nodes(
                    {'convoy_id': value})
            self.assertEqual(result['reason'], 'invalid_arguments')
            call_.assert_not_called()

    def test_list_controllers_uses_the_federated_non_waking_view(self):
        network_view = {'ok': True, 'convoy_id': 'studio',
                        'controller_count': 2, 'controllers': [
            {'controller_id': 'ctl-a', 'selected_node_id': 'node-1'},
            {'controller_id': 'ctl-b', 'selected_node_id': 'node-2'},
        ]}
        with patch.object(bridge, 'convoy_host_call',
                          return_value=network_view) as call_:
            result = bridge.handle_convoy_list_controllers({})
        call_.assert_called_once_with('GET', '/network/controllers')
        self.assertTrue(result['ok'])
        self.assertEqual(result['convoy_id'], 'studio')
        self.assertFalse(result['wakes_touchdesigner'])
        self.assertEqual(result['controller_count'], 2)
        self.assertEqual([row['controller_id'] for row in
                          result['controllers']], ['ctl-a', 'ctl-b'])

    def test_list_controllers_encodes_network_filters(self):
        response = {'ok': True, 'controllers': []}
        with patch.object(bridge, 'convoy_host_call',
                          return_value=response) as call_:
            result = bridge.handle_convoy_list_controllers({
                'convoy_id': 'A/B', 'host_id': 'host-remote',
                'node_id': 'node-2'})
        call_.assert_called_once_with(
            'GET',
            '/network/controllers?convoy_id=A%2FB&host_id=host-remote&node_id=node-2')
        self.assertTrue(result['ok'])
        self.assertFalse(result['wakes_touchdesigner'])

    def test_list_controllers_rejects_bad_filters_before_probe(self):
        for params in ({'convoy_id': ''}, {'host_id': ''}, {'node_id': 1}, []):
            with self.subTest(params=params), \
                 patch.object(bridge, 'probe_convoy_host') as probe:
                result = bridge.handle_convoy_list_controllers(params)
            self.assertEqual(result['reason'], 'invalid_arguments')
            probe.assert_not_called()

    def test_owlette_capabilities_use_only_the_authenticated_local_host(self):
        response = {'ok': True, 'action': 'capabilities',
                    'capabilities': {'machine_inventory': True}}
        with patch.object(bridge, 'convoy_host_call',
                          return_value=response) as call_:
            result = bridge.handle_convoy_owlette({})
        call_.assert_called_once_with(
            'POST', '/owlette', {'action': 'capabilities'}, timeout=35.0)
        self.assertTrue(result['ok'])
        self.assertFalse(result['wakes_touchdesigner'])

    def test_owlette_submit_forwards_only_bounded_published_fields(self):
        params = {
            'action': 'submit_command', 'site_id': 'site',
            'machine_id': 'machine', 'command_type': 'capture_screenshot',
            'idempotency_key': 'stable', 'params': {'display': 1},
            'timeout_seconds': 45, 'ignored': 'never-forwarded',
        }
        with patch.object(bridge, 'convoy_host_call', return_value={
                'ok': True, 'command': {'commandId': 'cmd'}}) as call_:
            result = bridge.handle_convoy_owlette(params)
        call_.assert_called_once_with('POST', '/owlette', {
            'action': 'submit_command', 'site_id': 'site',
            'machine_id': 'machine', 'command_type': 'capture_screenshot',
            'idempotency_key': 'stable', 'params': {'display': 1},
            'timeout_seconds': 45,
        }, timeout=35.0)
        self.assertTrue(result['ok'])

    def test_owlette_refuses_malformed_and_tunnel_calls_before_host(self):
        cases = (
            {'action': 'get_machine', 'site_id': 'site'},
            {'action': 'command_status', 'site_id': 'site',
             'machine_id': 'machine'},
            {'action': 'submit_command', 'site_id': 'site',
             'machine_id': 'machine', 'command_type': 'capture_screenshot'},
            {'action': 'submit_command', 'site_id': 'site',
             'machine_id': 'machine', 'command_type': 'mcp_tool_call',
             'idempotency_key': 'stable'},
            {'action': 'submit_command', 'site_id': 'site',
             'machine_id': 'machine', 'command_type': 'capture_screenshot',
             'idempotency_key': 'stable', 'params': []},
            {'action': 'not-public'},
        )
        for params in cases:
            with self.subTest(params=params), \
                 patch.object(bridge, 'convoy_host_call') as call_:
                result = bridge.handle_convoy_owlette(params)
            self.assertFalse(result['ok'])
            call_.assert_not_called()

    def test_call_requires_an_explicit_complete_target_without_network(self):
        for missing in ('target_host_id', 'convoy_id', 'target_node_id',
                        'operation'):
            args = self._call_args()
            del args[missing]
            with self.subTest(missing=missing), \
                 patch.object(bridge, 'convoy_host_call') as call_:
                result = bridge.handle_convoy_call(args)
            self.assertEqual(result['reason'], 'invalid_arguments')
            call_.assert_not_called()

    def test_ping_is_a_fixed_host_native_non_waking_call(self):
        captured = []

        def relay(params):
            captured.append(dict(params))
            return {'ok': True, 'job': {'state': 'succeeded',
                                        'result': {'pong': True}}}

        fake_time = MagicMock()
        fake_time.monotonic.side_effect = (10.0, 10.025)
        with patch.object(bridge, 'handle_convoy_call', side_effect=relay), \
             patch.object(bridge, 'time', fake_time):
            result = bridge.handle_convoy_ping({
                'target_host_id': 'host-remote', 'convoy_id': 'studio',
                'target_node_id': 'node-remote',
                'idempotency_key': 'stable', 'timeout_s': 2})
        self.assertEqual(captured, [{
            'target_host_id': 'host-remote', 'convoy_id': 'studio',
            'target_node_id': 'node-remote',
            'idempotency_key': 'stable', 'timeout_s': 2,
            'operation': 'convoy_ping', 'arguments': {}, 'wait': True,
        }])
        self.assertEqual(result['round_trip_ms'], 25.0)
        self.assertFalse(result['wakes_touchdesigner'])

    def test_ping_rejects_incomplete_target_without_relay(self):
        with patch.object(bridge, 'handle_convoy_call') as relay:
            result = bridge.handle_convoy_ping({
                'target_host_id': 'host', 'convoy_id': 'studio'})
        self.assertEqual(result['reason'], 'invalid_arguments')
        relay.assert_not_called()

    def test_lifecycle_wrappers_pin_operations_and_never_accept_paths(self):
        captured = []

        def relay(params):
            captured.append(dict(params))
            return {'ok': True, 'job': {'state': 'succeeded'}}

        common = {'target_host_id': 'host-remote', 'convoy_id': 'studio',
                  'target_node_id': 'node-remote',
                  'idempotency_key': 'stable', 'timeout_s': 30,
                  'wait': False}
        with patch.object(bridge, 'handle_convoy_call', side_effect=relay):
            started = bridge.handle_convoy_start_node(dict(common))
            restarted = bridge.handle_convoy_restart_node(dict(
                common, expected_runtime_id='rt-1',
                policy='save_then_restart'))
        self.assertTrue(started['starts_touchdesigner'])
        self.assertTrue(restarted['restarts_touchdesigner'])
        self.assertEqual(captured[0]['operation'], 'convoy_start_node')
        self.assertEqual(captured[0]['arguments'], {'timeout_s': 30.0})
        self.assertNotIn('expected_runtime_id', captured[0])
        self.assertEqual(captured[1]['operation'], 'convoy_restart_node')
        self.assertEqual(captured[1]['expected_runtime_id'], 'rt-1')
        self.assertEqual(captured[1]['arguments'], {
            'timeout_s': 30.0, 'policy': 'save_then_restart'})
        for sent in captured:
            self.assertNotIn('executable', sent)
            self.assertNotIn('toe_path', sent)
            self.assertNotIn('operation_id', sent['arguments'])

    def test_lifecycle_wrappers_reject_unsafe_shapes_before_relay(self):
        cases = (
            ('start', {'target_host_id': 'h', 'convoy_id': 'c',
                       'target_node_id': 'n', 'timeout_s': 901}),
            ('start', {'target_host_id': 'h', 'convoy_id': 'c',
                       'target_node_id': 'n', 'wait': 'yes'}),
            ('restart', {'target_host_id': 'h', 'convoy_id': 'c',
                         'target_node_id': 'n'}),
            ('restart', {'target_host_id': 'h', 'convoy_id': 'c',
                         'target_node_id': 'n',
                         'expected_runtime_id': 'rt', 'policy': 'guess'}),
            ('restart', {'target_host_id': 'h', 'convoy_id': 'c',
                         'target_node_id': 'n',
                         'expected_runtime_id': 'rt',
                         'policy': 'discard_and_restart'}),
            ('restart', {'target_host_id': 'h', 'convoy_id': 'c',
                         'target_node_id': 'n',
                         'expected_runtime_id': 'rt', 'policy': 'force'}),
        )
        for kind, params in cases:
            handler = (bridge.handle_convoy_start_node if kind == 'start'
                       else bridge.handle_convoy_restart_node)
            with self.subTest(kind=kind, params=params), \
                 patch.object(bridge, 'handle_convoy_call') as relay:
                result = handler(params)
            self.assertEqual(result['reason'], 'invalid_arguments')
            relay.assert_not_called()

    def test_call_rejects_unsafe_shapes_before_network(self):
        bad = (
            {'arguments': []}, {'wait': 'yes'}, {'timeout_s': True},
            {'timeout_s': float('nan')}, {'timeout_s': 0.01},
            {'idempotency_key': ''}, {'expected_runtime_id': ''},
        )
        for override in bad:
            with self.subTest(override=override), \
                 patch.object(bridge, 'convoy_host_call') as call_:
                result = bridge.handle_convoy_call(
                    self._call_args(**override))
            self.assertEqual(result['reason'], 'invalid_arguments')
            call_.assert_not_called()

    def test_generated_idempotency_key_survives_indeterminate_submit(self):
        captured = []

        def call_(method, path, payload, timeout=None):
            captured.append((method, path, dict(payload), timeout))
            return {'ok': False, 'reason': 'convoy_host_error'}

        with patch.object(bridge, 'convoy_host_call', side_effect=call_):
            result = bridge.handle_convoy_call(self._call_args())
        key = captured[0][2]['idempotency_key']
        self.assertRegex(key, r'^[0-9a-f]{32}$')
        self.assertEqual(result['idempotency_key'], key)
        self.assertEqual(result['reconcile_with_idempotency_key'], key)
        self.assertEqual(result['target_host_id'], 'host-remote')
        self.assertEqual(result['target_node_id'], 'node-remote')
        self.assertEqual(result['operation'], 'convoy_ping')

    def test_provided_idempotency_key_and_controller_are_sent_unchanged(self):
        seen = []

        def call_(method, path, payload, timeout=None):
            seen.append(dict(payload))
            return self._accepted(state='succeeded')

        with patch.object(bridge, 'convoy_host_call', side_effect=call_):
            result = bridge.handle_convoy_call(self._call_args(
                idempotency_key='stable-key', expected_runtime_id='rt_1'))
        self.assertEqual(seen[0]['idempotency_key'], 'stable-key')
        self.assertEqual(seen[0]['controller_id'],
                         bridge._CONVOY_CONTROLLER_ID)
        self.assertEqual(seen[0]['expected_runtime_id'], 'rt_1')
        self.assertEqual(result['idempotency_key'], 'stable-key')
        self.assertEqual(result['controller_id'],
                         bridge._CONVOY_CONTROLLER_ID)
        self.assertEqual(result['delivery_id'], 'cj_123')

    def test_wait_false_returns_durable_ack_without_polling(self):
        with patch.object(bridge, 'convoy_host_call',
                          return_value=self._accepted()) as call_:
            result = bridge.handle_convoy_call(
                self._call_args(wait=False, idempotency_key='stable'))
        self.assertEqual(call_.call_count, 1)
        self.assertEqual(result['job']['state'], 'queued')
        self.assertEqual(result['delivery_id'], 'cj_123')

    def test_accepted_response_without_delivery_id_is_not_success(self):
        with patch.object(bridge, 'convoy_host_call', return_value={
                'ok': True, 'job': {'state': 'queued'}}):
            result = bridge.handle_convoy_call(
                self._call_args(idempotency_key='stable'))
        self.assertFalse(result['ok'])
        self.assertEqual(result['reason'], 'convoy_host_bad_response')
        self.assertEqual(result['reconcile_with_idempotency_key'], 'stable')

    def test_poll_uses_cursor_and_stops_only_on_terminal_job(self):
        responses = [
            self._accepted(),
            {'ok': True, 'changed': False, 'cursor': 10.0,
             'state': 'queued'},
            {'ok': True, 'changed': True, 'cursor': 11.0, 'job': {
                'delivery_id': 'cj_123', 'state': 'succeeded',
                'updated': 11.0, 'result': {'pong': True}}},
        ]
        with patch.object(bridge, 'convoy_host_call',
                          side_effect=responses) as call_, \
             patch.object(bridge.time, 'sleep'):
            result = bridge.handle_convoy_call(
                self._call_args(idempotency_key='stable'))
        self.assertEqual(result['job']['state'], 'succeeded')
        self.assertTrue(result['job']['result']['pong'])
        first_poll = call_.call_args_list[1].args[2]
        second_poll = call_.call_args_list[2].args[2]
        self.assertEqual(first_poll['since'], 10.0)
        self.assertEqual(second_poll['since'], 10.0)
        self.assertEqual(call_.call_count, 3)

    def test_transient_poll_dropout_reconciles_without_resubmission(self):
        responses = [
            self._accepted(),
            {'ok': False, 'reason': 'peer_unreachable'},
            {'ok': True, 'changed': True, 'cursor': 12.0, 'job': {
                'delivery_id': 'cj_123', 'state': 'failed',
                'updated': 12.0, 'result': {'message': 'remote failure'}}},
        ]
        with patch.object(bridge, 'convoy_host_call',
                          side_effect=responses) as call_, \
             patch.object(bridge.time, 'sleep'):
            result = bridge.handle_convoy_call(
                self._call_args(idempotency_key='stable'))
        self.assertEqual(result['job']['state'], 'failed')
        self.assertEqual(call_.call_args_list[0].args[1], '/relay')
        self.assertEqual(call_.call_args_list[1].args[1], '/relay/job')
        self.assertEqual(call_.call_args_list[2].args[1], '/relay/job')

    def test_permanent_poll_refusal_preserves_accepted_delivery(self):
        responses = [self._accepted(),
                     {'ok': False, 'reason': 'namespace_not_admitted'}]
        with patch.object(bridge, 'convoy_host_call', side_effect=responses), \
             patch.object(bridge.time, 'sleep'):
            result = bridge.handle_convoy_call(
                self._call_args(idempotency_key='stable'))
        self.assertTrue(result['ok'], 'the durable ACK remains true')
        self.assertTrue(result['wait_interrupted'])
        self.assertEqual(result['poll_error']['reason'],
                         'namespace_not_admitted')
        self.assertEqual(result['delivery_id'], 'cj_123')

    def test_wait_timeout_does_not_claim_non_execution(self):
        def call_(method, path, payload, timeout=None):
            if path == '/relay':
                return self._accepted()
            return {'ok': True, 'changed': False, 'cursor': 10.0,
                    'state': 'queued'}

        with patch.object(bridge, 'convoy_host_call', side_effect=call_), \
             patch.object(bridge, 'CONVOY_POLL_INITIAL_S', 0.02), \
             patch.object(bridge, 'CONVOY_POLL_MAX_S', 0.02):
            result = bridge.handle_convoy_call(self._call_args(
                timeout_s=0.1, idempotency_key='stable'))
        self.assertTrue(result['ok'])
        self.assertTrue(result['wait_timed_out'])
        self.assertEqual(result['delivery_id'], 'cj_123')
        self.assertIn('remains durable', result['detail'])
        self.assertNotIn('did not execute', result['detail'])

    def test_get_job_requires_explicit_owner_and_preserves_provenance(self):
        params = {'target_host_id': 'host-remote', 'convoy_id': 'studio',
                  'delivery_id': 'cj_123', 'since': 12.5}
        with patch.object(bridge, 'convoy_host_call',
                          return_value={'ok': True, 'changed': False}) as call_:
            result = bridge.handle_convoy_get_job(params)
        call_.assert_called_once_with('POST', '/relay/job', params)
        self.assertEqual(result['target_host_id'], 'host-remote')
        self.assertEqual(result['convoy_id'], 'studio')
        self.assertEqual(result['delivery_id'], 'cj_123')
        self.assertFalse(result['wakes_touchdesigner'])

    def test_get_job_rejects_missing_target_and_non_finite_cursor(self):
        cases = (
            {'convoy_id': 'studio', 'delivery_id': 'cj_123'},
            {'target_host_id': 'h', 'convoy_id': 'studio',
             'delivery_id': 'cj_123', 'since': float('inf')},
        )
        for params in cases:
            with self.subTest(params=params), \
                 patch.object(bridge, 'convoy_host_call') as call_:
                result = bridge.handle_convoy_get_job(params)
            self.assertEqual(result['reason'], 'invalid_arguments')
            call_.assert_not_called()

    @staticmethod
    def _artifact_reference(data=b'image-bytes', mime_type='image/png'):
        digest = bridge.hashlib.sha256(data).hexdigest()
        return {
            'kind': 'convoy_artifact', 'convoy_id': 'studio',
            'artifact_id': 'art_' + digest, 'sha256': digest,
            'size': len(data), 'mime_type': mime_type,
            'filename_hint': 'capture.png',
            'host_id': 'host-remote', 'node_id': 'node-remote',
            'controller_id': 'controller', 'job_id': 'cj_123',
        }

    def test_get_artifact_uses_one_deadline_and_fixed_controller(self):
        reference = self._artifact_reference()
        local_reference = dict(reference)
        materialized = {'ok': True, 'artifact': local_reference,
                        'transfer': {'attempts': 1}}
        downloaded = {'ok': True, 'artifact': local_reference,
                      'local_path': 'capture.png', 'verified': True}
        params = {'target_host_id': 'host-remote', 'convoy_id': 'studio',
                  'target_node_id': 'node-remote', 'artifact': reference,
                  'timeout_s': 10.0}
        with patch.object(bridge, 'convoy_host_call', side_effect=(
                materialized, {'ok': True, 'already_acknowledged': False}
        )) as host_call, \
             patch.object(bridge, 'convoy_host_download_artifact',
                          return_value=downloaded) as download, \
             patch.object(bridge.time, 'monotonic',
                          side_effect=(100.0, 102.5)):
            result = bridge.handle_convoy_get_artifact(params)
        sent = host_call.call_args_list[0].args[2]
        self.assertEqual(host_call.call_args_list[0].args[:2],
                         ('POST', '/relay/artifact'))
        self.assertEqual(host_call.call_args_list[0].kwargs['timeout'], 10.0)
        self.assertEqual(sent['controller_id'],
                         bridge._CONVOY_CONTROLLER_ID)
        self.assertEqual(sent['artifact'], reference)
        download.assert_called_once_with(local_reference, 'studio', 7.5)
        self.assertTrue(result['verified'])
        self.assertEqual(result['transfer'], {'attempts': 1})
        self.assertTrue(result['acknowledgement']['ok'])
        self.assertEqual(host_call.call_args_list[1].args, (
            'POST', '/relay/ack', {
                'target_host_id': 'host-remote', 'convoy_id': 'studio',
                'delivery_id': 'cj_123'}))
        self.assertFalse(result['wakes_touchdesigner'])

    def test_get_artifact_releases_materialization_after_verified_copy(self):
        reference = self._artifact_reference(b'protected', 'text/plain')
        materialized = {
            'ok': True, 'artifact': reference,
            'relay_protection_id': 'relay:' + ('a' * 32),
            'transfer': {'attempts': 1},
        }
        downloaded = {
            'ok': True, 'artifact': reference,
            'local_path': 'verified.bin', 'verified': True,
        }
        params = {
            'target_host_id': 'host-remote', 'convoy_id': 'studio',
            'target_node_id': 'node-remote', 'artifact': reference,
            'timeout_s': 10.0,
        }
        with patch.object(bridge, 'convoy_host_call', side_effect=(
                materialized,
                {'ok': True, 'already_acknowledged': False},
                {'ok': True, 'released': True},
        )) as host_call, patch.object(
                bridge, 'convoy_host_download_artifact',
                return_value=downloaded), patch.object(
                bridge.time, 'monotonic', side_effect=(100.0, 101.0)):
            result = bridge.handle_convoy_get_artifact(params)
        self.assertTrue(result['ok'])
        self.assertEqual(host_call.call_args_list[2].args, (
            'POST', '/relay/artifact/release', {
                'convoy_id': 'studio',
                'artifact_id': reference['artifact_id'],
                'relay_protection_id': 'relay:' + ('a' * 32),
            }))
        self.assertNotIn('relay_protection_id', result)

    def test_get_artifact_releases_materialization_after_copy_failure(self):
        reference = self._artifact_reference(b'protected', 'text/plain')
        materialized = {
            'ok': True, 'artifact': reference,
            'relay_protection_id': 'relay:' + ('b' * 32),
        }
        params = {
            'target_host_id': 'host-remote', 'convoy_id': 'studio',
            'target_node_id': 'node-remote', 'artifact': reference,
        }
        with patch.object(bridge, 'convoy_host_call', side_effect=(
                materialized, {'ok': True, 'released': True},
        )) as host_call, patch.object(
                bridge, 'convoy_host_download_artifact', return_value={
                    'ok': False, 'reason': 'artifact_corrupt'}):
            result = bridge.handle_convoy_get_artifact(params)
        self.assertEqual(result['reason'], 'artifact_corrupt')
        self.assertEqual(host_call.call_args_list[-1].args[:2],
                         ('POST', '/relay/artifact/release'))
        self.assertFalse(any(
            call.args[1] == '/relay/ack'
            for call in host_call.call_args_list))

    def test_ack_job_routes_to_exact_owner_and_is_safe_to_retry(self):
        params = {'target_host_id': 'host-remote', 'convoy_id': 'studio',
                  'delivery_id': 'cj_123'}
        with patch.object(bridge, 'convoy_host_call', return_value={
                'ok': True, 'already_acknowledged': True}) as call_:
            result = bridge.handle_convoy_ack_job(params)
        call_.assert_called_once_with('POST', '/relay/ack', params)
        self.assertTrue(result['already_acknowledged'])
        self.assertFalse(result['wakes_touchdesigner'])

    def test_get_artifact_rejects_forged_metadata_before_network(self):
        valid = self._artifact_reference()
        cases = (
            dict(valid, convoy_id='other'),
            dict(valid, artifact_id='art_' + ('0' * 64)),
            dict(valid, size=bridge.CONVOY_ARTIFACT_MAX_BYTES + 1),
            dict(valid, mime_type='image/png\r\nInjected: yes'),
        )
        for reference in cases:
            with self.subTest(reference=reference), \
                 patch.object(bridge, 'convoy_host_call') as host_call:
                result = bridge.handle_convoy_get_artifact({
                    'target_host_id': 'host-remote', 'convoy_id': 'studio',
                    'target_node_id': 'node-remote', 'artifact': reference})
            self.assertEqual(result['reason'], 'invalid_arguments')
            host_call.assert_not_called()

    def _artifact_project_state(self):
        root = tempfile.mkdtemp(prefix='convoy-save-project-')
        self.addCleanup(shutil.rmtree, root, True)
        embody_dir = os.path.join(root, '.embody')
        os.makedirs(embody_dir)
        config_path = os.path.join(embody_dir, 'envoy.json')
        with open(config_path, 'w', encoding='utf-8') as output:
            json.dump({}, output)
        return root, bridge.BridgeState(
            url='http://127.0.0.1:9870/mcp', config_path=config_path)

    def test_save_artifact_verifies_then_exports_to_own_project_and_cleans(self):
        reference = self._artifact_reference(b'saved', 'text/plain')
        root, state = self._artifact_project_state()
        descriptor, local_path = tempfile.mkstemp(
            prefix='convoy-save-verified-', suffix='.txt')
        os.close(descriptor)
        with open(local_path, 'wb') as output:
            output.write(b'saved')
        bridge._convoy_track_temp(local_path)
        events = []

        def materialize(_params, acknowledge=True,
                        release_protection=True):
            self.assertFalse(acknowledge)
            self.assertFalse(release_protection)
            events.append('verify')
            return {'ok': True, 'artifact': reference,
                    'local_path': local_path, 'verified': True}

        def export(method, path, body, timeout=None):
            if path == '/artifact/export':
                events.append('export')
                self.assertEqual(method, 'POST')
                self.assertEqual(body['project_root'], os.path.realpath(root))
                self.assertEqual(body['target_host_id'], 'host-remote')
                self.assertEqual(body['target_node_id'], 'node-remote')
                self.assertEqual(body['artifact'], reference)
                self.assertEqual(body['filename'], 'kept.txt')
                self.assertTrue(body['overwrite'])
                self.assertAlmostEqual(timeout, 57.5)
                return {'ok': True,
                        'saved_path': os.path.join(root, 'kept.txt')}
            self.assertEqual((method, path), ('POST', '/relay/ack'))
            events.append('ack')
            self.assertEqual(body['delivery_id'], 'cj_123')
            return {'ok': True, 'already_acknowledged': False}

        params = {'target_host_id': 'host-remote', 'convoy_id': 'studio',
                  'target_node_id': 'node-remote', 'artifact': reference,
                  'filename': 'kept.txt', 'overwrite': True,
                  'timeout_s': 60.0}
        with patch.object(bridge, 'handle_convoy_get_artifact',
                          side_effect=materialize), \
             patch.object(bridge, 'convoy_host_call',
                          side_effect=export), \
             patch.object(bridge.time, 'monotonic',
                          side_effect=(100.0, 102.5)):
            result = bridge.handle_convoy_save_artifact(params, state)
        self.assertTrue(result['ok'])
        self.assertTrue(result['acknowledgement']['ok'])
        self.assertEqual(events, ['verify', 'export', 'ack'])
        self.assertFalse(os.path.exists(local_path))
        self.assertNotIn(local_path, bridge._CONVOY_ARTIFACT_TEMP_PATHS)

    def test_save_artifact_cleans_verified_temp_when_export_is_refused(self):
        reference = self._artifact_reference(b'saved', 'text/plain')
        _root, state = self._artifact_project_state()
        descriptor, local_path = tempfile.mkstemp(
            prefix='convoy-save-refused-', suffix='.txt')
        os.close(descriptor)
        bridge._convoy_track_temp(local_path)
        materialized = {'ok': True, 'artifact': reference,
                        'local_path': local_path, 'verified': True}
        with patch.object(bridge, 'handle_convoy_get_artifact',
                          return_value=materialized), \
             patch.object(bridge, 'convoy_host_call', return_value={
                 'ok': False, 'reason': 'artifact_exists'}), \
             patch.object(bridge.time, 'monotonic',
                          side_effect=(20.0, 20.1)):
            result = bridge.handle_convoy_save_artifact({
                'target_host_id': 'host-remote', 'convoy_id': 'studio',
                'target_node_id': 'node-remote', 'artifact': reference}, state)
        self.assertEqual(result['reason'], 'artifact_exists')
        self.assertFalse(os.path.exists(local_path))

    def test_save_artifact_fails_closed_when_config_is_not_under_embody(self):
        reference = self._artifact_reference(b'saved', 'text/plain')
        root = tempfile.mkdtemp(prefix='convoy-save-no-embody-')
        self.addCleanup(shutil.rmtree, root, True)
        config_path = os.path.join(root, 'envoy.json')
        with open(config_path, 'w', encoding='utf-8') as output:
            json.dump({}, output)
        state = bridge.BridgeState(
            url='http://127.0.0.1:9870/mcp', config_path=config_path)
        with patch.object(bridge, 'handle_convoy_get_artifact') as materialize, \
             patch.object(bridge, 'convoy_host_call') as export:
            result = bridge.handle_convoy_save_artifact({
                'target_host_id': 'host-remote', 'convoy_id': 'studio',
                'target_node_id': 'node-remote', 'artifact': reference}, state)
        self.assertEqual(result['reason'], 'convoy_project_unavailable')
        materialize.assert_not_called()
        export.assert_not_called()

    def test_artifact_renderer_returns_native_verified_image_content(self):
        data = b'\x89PNG\r\n\x1a\nsmall-image'
        reference = self._artifact_reference(data)
        descriptor, path = tempfile.mkstemp(suffix='.png')
        os.close(descriptor)
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        with open(path, 'wb') as output:
            output.write(data)
        content = bridge.convoy_artifact_mcp_content({
            'ok': True, 'convoy_id': 'studio', 'artifact': reference,
            'local_path': path, 'verified': True})
        self.assertEqual([block['type'] for block in content],
                         ['text', 'image'])
        self.assertEqual(content[1]['mimeType'], 'image/png')
        self.assertEqual(bridge.base64.b64decode(content[1]['data']), data)
        payload = json.loads(content[0]['text'])
        self.assertNotIn('data', payload)
        self.assertNotIn('local_path', payload)
        self.assertTrue(payload['image_inline'])
        self.assertFalse(os.path.exists(path))

    def test_artifact_renderer_keeps_non_image_lazy_and_path_based(self):
        data = b'large-ish command output'
        reference = self._artifact_reference(data, 'text/plain')
        content = bridge.convoy_artifact_mcp_content({
            'ok': True, 'convoy_id': 'studio', 'artifact': reference,
            'local_path': 'verified-output.txt', 'verified': True})
        self.assertEqual(len(content), 1)
        payload = json.loads(content[0]['text'])
        self.assertEqual(payload['local_path'], 'verified-output.txt')

    def test_artifact_renderer_does_not_inline_mime_spoofed_image(self):
        data = b'not actually a png'
        reference = self._artifact_reference(data, 'image/png')
        descriptor, path = tempfile.mkstemp(suffix='.png')
        os.close(descriptor)
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        with open(path, 'wb') as output:
            output.write(data)
        content = bridge.convoy_artifact_mcp_content({
            'ok': True, 'convoy_id': 'studio', 'artifact': reference,
            'local_path': path, 'verified': True})
        self.assertEqual([block['type'] for block in content], ['text'])
        payload = json.loads(content[0]['text'])
        self.assertEqual(payload['inline_image_error'], 'ValueError')
        self.assertEqual(payload['local_path'], path)

    def test_bridge_exit_cleanup_removes_tracked_lazy_artifacts(self):
        descriptor, path = tempfile.mkstemp(suffix='.json')
        os.close(descriptor)
        bridge._convoy_track_temp(path)
        bridge._cleanup_convoy_artifact_temps()
        self.assertFalse(os.path.exists(path))
        self.assertNotIn(path, bridge._CONVOY_ARTIFACT_TEMP_PATHS)

    def test_handle_bridge_tool_renders_artifact_image_blocks(self):
        data = b'\x89PNG\r\n\x1a\nimage'
        reference = self._artifact_reference(data)
        descriptor, path = tempfile.mkstemp(suffix='.png')
        os.close(descriptor)
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        with open(path, 'wb') as output:
            output.write(data)
        with patch.object(bridge, 'handle_convoy_get_artifact', return_value={
                'ok': True, 'convoy_id': 'studio', 'artifact': reference,
                'local_path': path, 'verified': True}):
            content = bridge.handle_bridge_tool(
                'convoy_get_artifact', {}, None)
        self.assertEqual([block['type'] for block in content],
                         ['text', 'image'])

    def test_convoy_call_transparently_materializes_terminal_image(self):
        data = b'\x89PNG\r\n\x1a\ncaptured-image'
        reference = self._artifact_reference(data)
        descriptor, path = tempfile.mkstemp(suffix='.png')
        os.close(descriptor)
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        with open(path, 'wb') as output:
            output.write(data)
        relay = {'ok': True, 'job': {'state': 'succeeded', 'result': {
            'capture': True, 'artifact': reference}}}
        local = {'ok': True, 'target_host_id': 'host-remote',
                 'convoy_id': 'studio', 'target_node_id': 'node-remote',
                 'artifact': reference, 'local_path': path,
                 'verified': True}
        params = self._call_args(operation='capture_top')
        with patch.object(bridge, 'handle_convoy_call',
                          return_value=relay), \
             patch.object(bridge, 'handle_convoy_get_artifact',
                          return_value=local) as get_artifact:
            content = bridge.handle_bridge_tool('convoy_call', params, None)
        self.assertEqual([block['type'] for block in content],
                         ['text', 'image'])
        requested = get_artifact.call_args.args[0]
        self.assertEqual(requested['target_host_id'], 'host-remote')
        self.assertEqual(requested['target_node_id'], 'node-remote')
        self.assertEqual(requested['artifact'], reference)

    def test_selected_remote_capture_returns_native_image_content(self):
        data = b'\x89PNG\r\n\x1a\nselected-capture'
        reference = self._artifact_reference(data)
        descriptor, path = tempfile.mkstemp(suffix='.png')
        os.close(descriptor)
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        with open(path, 'wb') as output:
            output.write(data)
        state = self._state()
        state.convoy_target = {
            'target_host_id': 'host-remote', 'convoy_id': 'studio',
            'target_node_id': 'node-remote'}
        relay = {'ok': True, 'delivery_id': 'cj_123', 'job': {
            'state': 'succeeded', 'result': {
                'capture': True, 'artifact': reference}}}
        local = {'ok': True, 'target_host_id': 'host-remote',
                 'convoy_id': 'studio', 'target_node_id': 'node-remote',
                 'artifact': reference, 'local_path': path,
                 'verified': True}
        with patch.object(bridge, 'handle_convoy_call',
                          return_value=relay), \
             patch.object(bridge, 'handle_convoy_get_artifact',
                          return_value=local):
            result = bridge.handle_convoy_selected_tool(
                'capture_top', {'op_path': '/out'}, state)
        self.assertFalse(result['isError'])
        self.assertEqual([block['type'] for block in result['content']],
                         ['text', 'image'])
        self.assertEqual(bridge.base64.b64decode(
            result['content'][1]['data']), data)

    def test_non_image_artifact_remains_lazy_until_explicit_get(self):
        reference = self._artifact_reference(b'json', 'application/json')
        relay = {'ok': True, 'job': {'state': 'succeeded', 'result': {
            'spilled': True, 'artifact': reference}}}
        with patch.object(bridge, 'handle_convoy_call',
                          return_value=relay), \
             patch.object(bridge, 'handle_convoy_get_artifact') as get_artifact:
            content = bridge.handle_bridge_tool(
                'convoy_call', self._call_args(), None)
        self.assertEqual([block['type'] for block in content], ['text'])
        get_artifact.assert_not_called()

    def test_convoy_call_summary_never_reports_deleted_local_path(self):
        # Regression: the composite convoy_call summary used to embed the
        # pre-unlink materialization dict, advertising a verified local_path
        # for a tempfile the bridge had just deleted.
        data = b'\x89PNG\r\n\x1a\ncaptured-image'
        reference = self._artifact_reference(data)
        descriptor, path = tempfile.mkstemp(suffix='.png')
        os.close(descriptor)
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        with open(path, 'wb') as output:
            output.write(data)
        relay = {'ok': True, 'job': {'state': 'succeeded', 'result': {
            'capture': True, 'artifact': reference}}}
        local = {'ok': True, 'target_host_id': 'host-remote',
                 'convoy_id': 'studio', 'target_node_id': 'node-remote',
                 'artifact': reference, 'local_path': path, 'verified': True}
        with patch.object(bridge, 'handle_convoy_call', return_value=relay), \
             patch.object(bridge, 'handle_convoy_get_artifact',
                          return_value=local):
            content = bridge.handle_bridge_tool(
                'convoy_call', self._call_args(operation='capture_top'), None)
        summary = json.loads(content[0]['text'])
        materialization = summary['artifact_materialization']
        self.assertNotIn('local_path', materialization)
        self.assertTrue(materialization['image_inline'])
        self.assertFalse(os.path.exists(path))

    def test_selected_capture_summary_never_reports_deleted_local_path(self):
        data = b'\x89PNG\r\n\x1a\nselected-capture'
        reference = self._artifact_reference(data)
        descriptor, path = tempfile.mkstemp(suffix='.png')
        os.close(descriptor)
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        with open(path, 'wb') as output:
            output.write(data)
        state = self._state()
        state.convoy_target = {
            'target_host_id': 'host-remote', 'convoy_id': 'studio',
            'target_node_id': 'node-remote'}
        relay = {'ok': True, 'delivery_id': 'cj_123', 'job': {
            'state': 'succeeded', 'result': {
                'capture': True, 'artifact': reference}}}
        local = {'ok': True, 'target_host_id': 'host-remote',
                 'convoy_id': 'studio', 'target_node_id': 'node-remote',
                 'artifact': reference, 'local_path': path, 'verified': True}
        with patch.object(bridge, 'handle_convoy_call', return_value=relay), \
             patch.object(bridge, 'handle_convoy_get_artifact',
                          return_value=local):
            result = bridge.handle_convoy_selected_tool(
                'capture_top', {'op_path': '/out'}, state)
        summary = json.loads(result['content'][0]['text'])
        materialization = summary['artifact_materialization']
        self.assertNotIn('local_path', materialization)
        self.assertTrue(materialization['image_inline'])
        self.assertFalse(os.path.exists(path))

    def test_selected_tool_uses_deterministic_idempotency_key(self):
        # Regression: an ordinary pinned tool used to mint a fresh uuid4 per
        # call, so a retried mutating relay double-executed.  The key must be a
        # pure function of (selection, tool name, canonical arguments).
        state = self._state()
        state.convoy_target = {
            'target_host_id': 'host-remote', 'convoy_id': 'studio',
            'target_node_id': 'node-remote', 'expected_runtime_id': 'rt-7'}
        relay = {'ok': True, 'delivery_id': 'cj-1', 'job': {
            'state': 'succeeded', 'result': {'ok': True}}}
        keys = []

        def call_(call_params):
            keys.append(call_params.get('idempotency_key'))
            return relay

        with patch.object(bridge, 'handle_convoy_call', side_effect=call_):
            bridge.handle_convoy_selected_tool(
                'import_network', {'clear_first': True, 'network': {}}, state)
            bridge.handle_convoy_selected_tool(
                'import_network', {'clear_first': True, 'network': {}}, state)
            bridge.handle_convoy_selected_tool(
                'import_network', {'clear_first': False, 'network': {}}, state)
        self.assertIsNotNone(keys[0])
        self.assertEqual(keys[0], keys[1], 'identical retry reuses the key')
        self.assertNotEqual(keys[0], keys[2], 'different work digests apart')
        expected = bridge._convoy_selected_call_key(
            state.convoy_target, 'import_network',
            {'clear_first': True, 'network': {}})
        self.assertEqual(keys[0], expected)

    def test_inline_wait_is_capped_and_returns_durable_delivery_id(self):
        # Regression: the poll loop parked the stdio thread for the whole
        # caller budget (up to 3600s), so MCP ping / convoy_cancel_job / every
        # local tool were unreachable.  The in-loop wait is now short-capped
        # and the durable delivery_id handed back for convoy_get_job.
        def call_(method, path, payload, timeout=None):
            if path == '/relay':
                self.assertEqual(timeout, 10.0)  # submit keeps caller budget
                return self._accepted()
            return {'ok': True, 'changed': False, 'cursor': 10.0,
                    'state': 'queued'}

        with patch.object(bridge, 'convoy_host_call', side_effect=call_), \
             patch.object(bridge, 'CONVOY_INLINE_WAIT_MAX_S', 0.05), \
             patch.object(bridge, 'CONVOY_POLL_INITIAL_S', 0.02), \
             patch.object(bridge, 'CONVOY_POLL_MAX_S', 0.02):
            result = bridge.handle_convoy_call(self._call_args(
                timeout_s=10.0, idempotency_key='stable'))
        self.assertTrue(result['ok'])
        self.assertTrue(result['wait_timed_out'])
        self.assertTrue(result['wait_capped'])
        self.assertEqual(result['delivery_id'], 'cj_123')
        self.assertIn('convoy_get_job', result['detail'])

    def test_loopback_artifact_download_streams_and_verifies_exact_bytes(self):
        data = b'verified artifact bytes'
        reference = self._artifact_reference(data, 'text/plain')
        response = _ConvoyArtifactResponse(data, reference)
        connection = _ConvoyArtifactConnection(response)
        running = {'convoy': 'running', 'identity_confirmed': True,
                   'port': 54321, 'data_dir': 'state'}
        with patch.object(bridge, 'probe_convoy_host',
                          return_value=running), \
             patch.object(bridge, '_read_convoy_token',
                          return_value='ab' * 32), \
             patch.object(bridge.http.client, 'HTTPConnection',
                          return_value=connection):
            result = bridge.convoy_host_download_artifact(
                reference, 'studio', 5.0)
        self.addCleanup(lambda: os.path.exists(result.get('local_path', ''))
                        and os.unlink(result['local_path']))
        self.assertTrue(result['verified'])
        with open(result['local_path'], 'rb') as source:
            self.assertEqual(source.read(), data)
        method, path, headers = connection.requests[0]
        self.assertEqual(method, 'GET')
        self.assertEqual(path, '/artifacts/c3R1ZGlv/'
                         + reference['artifact_id'])
        self.assertEqual(headers['X-Convoy-Host-Token'], 'ab' * 32)
        self.assertTrue(connection.closed)

    def test_loopback_artifact_download_deletes_hash_mismatch(self):
        expected = b'expected bytes'
        reference = self._artifact_reference(expected, 'text/plain')
        response = _ConvoyArtifactResponse(b'tampered bytes', reference)
        # Keep the declared length coherent with the signed metadata so the
        # content hash, rather than a cheap size mismatch, catches this case.
        response._headers['Content-Length'] = str(len(expected))
        response._stream = io.BytesIO(b'x' * len(expected))
        connection = _ConvoyArtifactConnection(response)
        descriptor, path = tempfile.mkstemp(suffix='.txt')
        os.close(descriptor)
        descriptor = os.open(path, os.O_WRONLY | os.O_TRUNC)
        running = {'convoy': 'running', 'identity_confirmed': True,
                   'port': 54321, 'data_dir': 'state'}
        with patch.object(bridge, 'probe_convoy_host',
                          return_value=running), \
             patch.object(bridge, '_read_convoy_token',
                          return_value='ab' * 32), \
             patch.object(bridge.http.client, 'HTTPConnection',
                          return_value=connection), \
             patch.object(bridge.tempfile, 'mkstemp',
                          return_value=(descriptor, path)):
            result = bridge.convoy_host_download_artifact(
                reference, 'studio', 5.0)
        self.assertFalse(result['ok'])
        self.assertEqual(result['reason'], 'artifact_download_failed')
        self.assertFalse(os.path.exists(path))

    def test_cancel_job_routes_to_the_remote_delivery_owner(self):
        params = {'target_host_id': 'host-remote', 'convoy_id': 'studio',
                  'delivery_id': 'cj_123'}
        response = {'ok': True, 'cancel_requested': True,
                    'definitive': False}
        with patch.object(bridge, 'convoy_host_call',
                          return_value=response) as call_:
            result = bridge.handle_convoy_cancel_job(params)
        call_.assert_called_once_with('POST', '/relay/cancel', params)
        self.assertTrue(result['cancel_requested'])
        self.assertEqual(result['target_host_id'], 'host-remote')
        self.assertEqual(result['convoy_id'], 'studio')
        self.assertEqual(result['delivery_id'], 'cj_123')
        self.assertFalse(result['wakes_touchdesigner'])

    def test_cancel_job_routes_local_ownership_through_the_same_endpoint(self):
        params = {'target_host_id': 'host-local', 'convoy_id': 'studio',
                  'delivery_id': 'cj_123'}
        with patch.object(bridge, 'convoy_host_call', return_value={
                'ok': True, 'cancel_requested': True,
                'definitive': False}) as call_:
            result = bridge.handle_convoy_cancel_job(params)
        call_.assert_called_once_with('POST', '/relay/cancel', params)
        self.assertTrue(result['cancel_requested'])
        self.assertEqual(result['target_host_id'], 'host-local')
        self.assertEqual(result['delivery_id'], 'cj_123')

    def test_cancel_job_preserves_a_host_namespace_failure(self):
        params = {'target_host_id': 'host-local', 'convoy_id': 'studio',
                  'delivery_id': 'cj_123'}
        with patch.object(bridge, 'convoy_host_call', return_value={
                'ok': False, 'reason': 'job_namespace_mismatch',
                'http_status': 404}) as call_:
            result = bridge.handle_convoy_cancel_job(params)
        call_.assert_called_once_with('POST', '/relay/cancel', params)
        self.assertEqual(result['reason'], 'job_namespace_mismatch')
        self.assertEqual(result['target_host_id'], 'host-local')
        self.assertEqual(result['convoy_id'], 'studio')
        self.assertFalse(result['wakes_touchdesigner'])


# =====================================================================
# A-46 streaming SSE transport -- BRIDGE leg
# =====================================================================
# Before A-46, forward_to_http did resp.read(): the ENTIRE response was
# buffered to EOF before a single "data:" line was parsed, so nothing a
# server pushed mid-stream -- progress notifications, tools/list_changed
# -- could ever reach the client, and the old line scanner returned the
# first notification AS the answer to the request.
#
# These tests pin the incremental reader: per-frame delivery, server-
# pushed notification pass-through, the idle-window/absolute-cap split
# and its per-frame renewal, the severance rows of the plan's failure
# matrix (nothing may EVER leave a request unanswered), the buffering
# bounds, the read-to-EOF escape hatch, and -- the compatibility floor
# -- an unchanged plain-JSON path for an Envoy that does not stream.

import types


def _sse_script(body):
    """Split a body into the byte lines a socket would hand over."""
    return io.BytesIO(body.encode('utf-8')).readlines()


def _torn_script(body, size):
    """Split a body into fixed-size byte chunks, tearing lines anywhere."""
    raw = body.encode('utf-8')
    return [raw[i:i + size] for i in range(0, len(raw), size)]


class _RecordingSock:
    """Records every settimeout() the read budget asks the socket for."""

    def __init__(self, log):
        self._log = log

    def settimeout(self, seconds):
        self._log.append(seconds)


class _FakeHTTPResponse:
    """Stand-in for the http.client response urlopen returns.

    Implements what forward_to_http touches: ``read1`` (the incremental
    reader), ``read`` (the escape hatch's read-to-EOF), ``close``, and
    the ``fp.raw._sock`` chain the read budget reaches through.  The
    script holds BYTE CHUNKS at arbitrary boundaries -- not lines -- so
    a test can tear a frame anywhere or dribble bytes.  An entry may be
    bytes, an exception instance (raised when the reader reaches it), or
    a callable (invoked for its side effect; its return value is used).
    """

    def __init__(self, script, on_read=None):
        self._script = list(script)
        self._on_read = on_read
        self.timeouts = []
        self.closed = False
        self.reads = 0
        self.fp = types.SimpleNamespace(
            raw=types.SimpleNamespace(_sock=_RecordingSock(self.timeouts)))

    def read1(self, size=-1):
        if self._on_read is not None:
            self._on_read(self.reads)
        if not self._script:
            return b''
        item = self._script.pop(0)
        if isinstance(item, BaseException):
            raise item
        if callable(item):
            item = item()
        self.reads += 1
        if size is not None and size >= 0 and len(item) > size:
            self._script.insert(0, item[size:])
            item = item[:size]
        return item

    def read(self, size=-1):
        out = []
        while True:
            chunk = self.read1(-1)
            if not chunk:
                break
            out.append(chunk)
        return b''.join(out)

    def close(self):
        self.closed = True


_PROGRESS = ('data: {"jsonrpc":"2.0","method":"notifications/progress",'
             '"params":{"pct":%d}}\n\n')
_ANSWER = 'data: {"jsonrpc":"2.0","id":1,"result":"done"}\n\n'


def _stdout_lines(capture):
    return [json.loads(l) for l in capture.getvalue().split('\n') if l.strip()]


class TestBridgeSseFrameParser(EmbodyTestCase):
    """The pure frame splitter -- no sockets, no responses, no clock."""

    def _frames(self, lines):
        return list(bridge._sse_frames(lines))

    def test_single_frame(self):
        self.assertEqual(
            self._frames(['event: message', 'data: {"a":1}', '']),
            ['{"a":1}'])

    def test_blank_line_closes_each_frame(self):
        self.assertEqual(
            self._frames(['data: one', '', 'data: two', '']),
            ['one', 'two'])

    def test_multi_line_data_joins_with_newlines(self):
        self.assertEqual(
            self._frames(['data: {"id":1,', 'data: "r":2}', '']),
            ['{"id":1,\n"r":2}'])

    def test_comment_lines_are_keepalives_not_frames(self):
        self.assertEqual(
            self._frames([': keep-alive', '', 'data: real', '']),
            ['real'])

    def test_non_data_fields_are_dropped(self):
        self.assertEqual(
            self._frames(['event: message', 'id: 7', 'retry: 100',
                          'data: payload', '']),
            ['payload'])

    def test_trailing_frame_without_a_blank_line_is_still_yielded(self):
        self.assertEqual(self._frames(['data: last']), ['last'])

    def test_data_without_a_space_after_the_colon(self):
        self.assertEqual(self._frames(['data:{"a":1}', '']), ['{"a":1}'])

    def test_line_with_no_colon_carries_nothing(self):
        self.assertEqual(self._frames(['garbage', 'data: ok', '']), ['ok'])

    def test_empty_input_yields_nothing(self):
        self.assertEqual(self._frames([]), [])

    def test_a_frame_past_the_buffer_cap_is_severance_not_a_silent_drop(self):
        """A never-terminated data frame cannot grow without limit."""
        lines = ('data: ' + 'x' * 500 for _ in range(20))
        raised = None
        with patch.object(bridge, '_MAX_BODY_BYTES', 2000):
            try:
                list(bridge._sse_frames(lines))
            except Exception as e:  # noqa: BLE001 -- type asserted below
                raised = e
        self.assertIsInstance(raised, ConnectionError)

    def test_sse_line_recognition_never_fires_on_json(self):
        """The mirror stops on SSE evidence, so the test must be exact:
        SSE field names are bare, every JSON key is quoted."""
        for line in ('data: x', 'event: message', 'id: 3', 'retry: 50',
                     ': ping', ':'):
            self.assertTrue(bridge._is_sse_line(line), line)
        for line in ('{"data": "x"}', '  "id": 3,', '{', '}', 'not json',
                     '"retry": 1', 'datax: y', ''):
            self.assertFalse(bridge._is_sse_line(line), line)


class TestBridgeBodyMirror(EmbodyTestCase):
    """The plain-JSON fallback buffer, and its stated bound."""

    def test_records_until_stopped(self):
        m = bridge._BodyMirror()
        m.add(b'abc')
        m.add(b'def')
        self.assertEqual(m.body(), b'abcdef')

    def test_stop_drops_what_was_held_and_stops_recording(self):
        m = bridge._BodyMirror()
        m.add(b'abc')
        m.stop()
        m.add(b'def')
        self.assertEqual(m.body(), b'')
        self.assertFalse(m.recording)

    def test_recording_stops_at_the_stated_cap(self):
        m = bridge._BodyMirror()
        with patch.object(bridge, '_MAX_BODY_BYTES', 100):
            m.add(b'x' * 99)
            self.assertTrue(m.recording)
            m.add(b'xx')
        self.assertFalse(
            m.recording,
            'past the cap the fallback is abandoned, not grown forever')
        self.assertEqual(m.body(), b'')


class TestBridgeStreamingForward(EmbodyTestCase):
    """forward_to_http delivers each frame as it lands, not at EOF."""

    def _forward(self, body_or_script, message=None, capture=None, **kwargs):
        script = (body_or_script if isinstance(body_or_script, list)
                  else _sse_script(body_or_script))
        on_read = kwargs.pop('on_read', None)
        resp = _FakeHTTPResponse(script, on_read=on_read)
        capture = capture if capture is not None else _StdoutCapture()
        with patch('urllib.request.urlopen', return_value=resp), \
             patch.object(sys, 'stdout', capture):
            result = bridge.forward_to_http(
                'http://127.0.0.1:9870/mcp',
                message if message is not None else {'jsonrpc': '2.0', 'id': 1},
                **kwargs)
        return result, resp, capture

    def _expect_raise(self, body_or_script, message=None, **kwargs):
        script = (body_or_script if isinstance(body_or_script, list)
                  else _sse_script(body_or_script))
        resp = _FakeHTTPResponse(script)
        capture = _StdoutCapture()
        with patch('urllib.request.urlopen', return_value=resp), \
             patch.object(sys, 'stdout', capture):
            raised = None
            try:
                bridge.forward_to_http(
                    'http://127.0.0.1:9870/mcp',
                    message if message is not None
                    else {'jsonrpc': '2.0', 'id': 1},
                    **kwargs)
            except Exception as e:  # noqa: BLE001 -- type asserted by caller
                raised = e
        return raised, resp, capture

    # --- per-frame delivery -----------------------------------------

    def test_each_notification_reaches_stdout_before_the_next_read(self):
        """The defect this whole rework exists to kill."""
        body = (_PROGRESS % 10) + (_PROGRESS % 50) + _ANSWER
        seen = []
        capture = _StdoutCapture()

        def observe(_n):
            seen.append(len(_stdout_lines(capture)))

        result, _resp, capture = self._forward(
            body, capture=capture, on_read=observe)

        self.assertEqual(result['result'], 'done')
        self.assertEqual(
            max(seen), 2,
            'both notifications must be on stdout while the stream is '
            f'still being read (observed line counts: {seen})')
        self.assertEqual(
            len(_stdout_lines(capture)), 2,
            'exactly the two notifications, never the response frame')

    def test_a_frame_torn_across_reads_is_still_routed(self):
        """read1 hands over arbitrary byte boundaries, not lines."""
        body = (_PROGRESS % 7) + _ANSWER
        result, _resp, capture = self._forward(_torn_script(body, 5))
        self.assertEqual(result['result'], 'done')
        self.assertLen(_stdout_lines(capture), 1)

    def test_multibyte_character_split_across_reads_survives(self):
        # e-acute, written as an escape so this file stays pure ASCII.
        marker = '\u00e9'
        body = 'data: {"jsonrpc":"2.0","id":1,"result":"caf%s"}\n\n' % marker
        raw = body.encode('utf-8')
        cut = raw.index(b'\xc3\xa9') + 1  # between the two bytes of e-acute
        result, _resp, _capture = self._forward([raw[:cut], raw[cut:]])
        self.assertEqual(result['result'], 'caf' + marker)

    def test_notification_payload_is_passed_through_verbatim(self):
        result, _resp, capture = self._forward((_PROGRESS % 42) + _ANSWER)
        lines = _stdout_lines(capture)
        self.assertLen(lines, 1)
        self.assertEqual(lines[0]['method'], 'notifications/progress')
        self.assertEqual(lines[0]['params'], {'pct': 42})
        self.assertEqual(lines[0]['jsonrpc'], '2.0')
        self.assertEqual(result['result'], 'done')

    def test_notification_sink_can_be_injected(self):
        got = []
        result, _resp, capture = self._forward(
            (_PROGRESS % 1) + _ANSWER, on_notification=got.append)
        self.assertLen(got, 1)
        self.assertEqual(got[0]['method'], 'notifications/progress')
        self.assertEqual(
            capture.getvalue(), '',
            'an injected sink replaces the stdout write, never doubles it')
        self.assertEqual(result['result'], 'done')

    def test_response_frame_stops_the_read(self):
        body = _ANSWER + (_PROGRESS % 99)
        result, resp, capture = self._forward(body)
        self.assertEqual(result['result'], 'done')
        self.assertEqual(
            _stdout_lines(capture), [],
            'frames after the answer belong to nobody -- do not read on')
        self.assertTrue(resp.closed, 'the response must be closed')

    def test_server_initiated_request_is_passed_through_not_returned(self):
        body = ('data: {"jsonrpc":"2.0","id":"srv-1",'
                '"method":"sampling/createMessage"}\n\n') + _ANSWER
        result, _resp, capture = self._forward(body)
        self.assertEqual(result['result'], 'done')
        lines = _stdout_lines(capture)
        self.assertLen(lines, 1)
        self.assertEqual(lines[0]['method'], 'sampling/createMessage')

    def test_frame_answering_our_id_terminates(self):
        body = 'data: {"jsonrpc":"2.0","id":7,"result":"seven"}\n\n'
        result, _resp, _capture = self._forward(
            body, message={'jsonrpc': '2.0', 'id': 7})
        self.assertEqual(result['result'], 'seven')

    def test_unparseable_frame_is_skipped_not_fatal(self):
        body = 'data: {not json\n\n' + _ANSWER
        result, _resp, _capture = self._forward(body)
        self.assertEqual(result['result'], 'done')

    def test_crlf_framing(self):
        body = ('event: message\r\ndata: {"jsonrpc":"2.0","id":1,'
                '"result":"crlf"}\r\n\r\n')
        result, _resp, _capture = self._forward(body)
        self.assertEqual(result['result'], 'crlf')

    def test_keepalive_comment_does_not_end_the_wait(self):
        body = ': ping\n\n' + (_PROGRESS % 5) + _ANSWER
        result, _resp, capture = self._forward(body)
        self.assertEqual(result['result'], 'done')
        self.assertLen(_stdout_lines(capture), 1)

    # --- tools/list_changed ------------------------------------------

    def test_list_changed_frame_invalidates_the_bridge_tool_cache(self):
        fired = []
        bridge.set_tools_cache_invalidator(lambda: fired.append(True))
        try:
            body = ('data: {"jsonrpc":"2.0",'
                    '"method":"notifications/tools/list_changed"}\n\n'
                    + _ANSWER)
            result, _resp, capture = self._forward(body)
        finally:
            bridge.set_tools_cache_invalidator(None)
        self.assertEqual(fired, [True])
        self.assertEqual(result['result'], 'done')
        lines = _stdout_lines(capture)
        self.assertLen(lines, 1)
        self.assertEqual(lines[0]['method'],
                         'notifications/tools/list_changed')

    def test_a_failing_invalidator_never_breaks_a_forward(self):
        def boom():
            raise RuntimeError('cache blew up')

        bridge.set_tools_cache_invalidator(boom)
        try:
            body = ('data: {"jsonrpc":"2.0",'
                    '"method":"notifications/tools/list_changed"}\n\n'
                    + _ANSWER)
            result, _resp, _capture = self._forward(body)
        finally:
            bridge.set_tools_cache_invalidator(None)
        self.assertEqual(result['result'], 'done')

    def test_progress_notification_does_not_invalidate_the_cache(self):
        fired = []
        bridge.set_tools_cache_invalidator(lambda: fired.append(True))
        try:
            self._forward((_PROGRESS % 3) + _ANSWER)
        finally:
            bridge.set_tools_cache_invalidator(None)
        self.assertEqual(fired, [])

    # --- timeout split: idle window renewed per frame -----------------

    def test_one_frame_is_not_a_cadence_and_never_shortens_the_cap(self):
        """PANEL (frame-parser, important): a single early frame used to
        clamp the whole rest of the forward to the idle window, so an
        operation that announces itself and then works silently died at
        60s under a documented 300s ceiling -- and the client was told
        'Lost connection' for work the server actually completed."""
        body = (_PROGRESS % 1) + _ANSWER
        _result, resp, _capture = self._forward(body)
        self.assertEqual(
            resp.timeouts, [],
            'one frame is not a cadence: the socket must keep the cap')

    def test_single_shot_answer_keeps_the_full_cap(self):
        _result, resp, _capture = self._forward(_ANSWER)
        self.assertEqual(resp.timeouts, [])

    def test_float_sliver_in_the_deadline_never_rearms_the_cap(self):
        """REGRESSION (CI 2026-08-04): ``deadline = clock() + timeout``
        rounds UP at some clock magnitudes -- at monotonic()==256.2,
        (256.2 + 300) - 256.2 == 300.00000000000006 -- so the first
        arm()'s remaining budget lands a hair ABOVE what urlopen already
        armed.  The one-sided skip window read that as "needs re-arm" and
        recorded a spurious settimeout on fresh-boot machines (small
        monotonic values) while long-uptime dev boxes passed."""
        sliver_clock = 256.2
        self.assertGreater((sliver_clock + 300) - sliver_clock, 300,
                           'fixture magnitude must reproduce the sliver')
        body = (_PROGRESS % 1) + _ANSWER
        _result, resp, _capture = self._forward(
            body, clock=lambda: sliver_clock)
        self.assertEqual(
            resp.timeouts, [],
            'a 1e-14 rounding sliver is not a reason to touch the socket')

    def test_every_frame_after_the_first_renews_the_allowance(self):
        """A stream that keeps talking keeps its full inter-frame
        allowance -- the budget is renewed per frame, never clamped once."""
        body = ''.join(_PROGRESS % i for i in range(4)) + _ANSWER
        _result, resp, _capture = self._forward(body)
        self.assertEqual(
            resp.timeouts, [bridge.SSE_IDLE_WINDOW_S] * 3,
            'frames 2..4 each renew the gap allowance')

    def test_idle_window_is_overridable(self):
        body = (_PROGRESS % 1) + (_PROGRESS % 2) + _ANSWER
        _result, resp, _capture = self._forward(body, idle_timeout=7)
        self.assertEqual(resp.timeouts, [7])

    def test_idle_window_never_exceeds_the_caller_timeout(self):
        body = (_PROGRESS % 1) + (_PROGRESS % 2) + _ANSWER
        _result, resp, _capture = self._forward(body, timeout=3)
        self.assertLen(resp.timeouts, 1)
        self.assertTrue(
            2.5 <= resp.timeouts[0] <= 3.0,
            'a 3s reconciler probe must not sit 60s waiting for a frame, '
            f'got {resp.timeouts}')

    def test_absolute_cap_severs_a_stream_that_never_answers(self):
        script = _sse_script((_PROGRESS % 1) * 200)
        raised, resp, _capture = self._expect_raise(
            script, timeout=30, clock=_slow_clock(0.0, step=10.0))
        self.assertIsInstance(
            raised, TimeoutError,
            'a cap breach must look like today socket timeout (an OSError) '
            'so the main loop classifies it as connection loss')
        self.assertIsInstance(raised, OSError)
        self.assertTrue(resp.closed)

    def test_the_socket_is_clamped_to_what_is_left_of_the_cap(self):
        """PANEL (timeouts/runs-for-real): the cap was only checked
        BETWEEN reads while the socket kept its original timeout, so a
        forward could run to ~2x the documented ceiling."""
        script = _sse_script((_PROGRESS % 1) * 40)
        _raised, resp, _capture = self._expect_raise(
            script, timeout=30, clock=_slow_clock(0.0, step=5.0))
        self.assertTrue(resp.timeouts, 'the socket budget must be re-armed')
        self.assertTrue(
            all(t <= 30 for t in resp.timeouts),
            f'no read may be given more than the cap: {resp.timeouts}')
        self.assertTrue(
            resp.timeouts[-1] < resp.timeouts[0],
            f'the budget must shrink as the cap is spent: {resp.timeouts}')

    def test_timeout_none_means_no_cap_instead_of_a_type_error(self):
        """PANEL (timeouts): min(None, 60) raised before urlopen."""
        result, _resp, _capture = self._forward(
            (_PROGRESS % 1) + _ANSWER, timeout=None)
        self.assertEqual(result['result'], 'done')

    def test_socket_timeout_mid_stream_propagates_as_oserror(self):
        import socket as _socket
        script = _sse_script(_PROGRESS % 1) + [_socket.timeout('timed out')]
        raised, _resp, capture = self._expect_raise(script)
        self.assertIsInstance(raised, OSError)
        self.assertLen(
            _stdout_lines(capture), 1,
            'frames delivered before the stall stay delivered')

    # --- failure matrix: nothing may leave a request unanswered -------

    def test_severed_stream_raises_instead_of_hanging_the_client(self):
        raised, _resp, capture = self._expect_raise(
            (_PROGRESS % 1) + (_PROGRESS % 2))
        self.assertIsInstance(raised, ConnectionError)
        self.assertIsInstance(raised, OSError)
        self.assertLen(
            _stdout_lines(capture), 2,
            'everything that arrived before the cut is already delivered')

    def test_clean_eof_before_any_frame_raises(self):
        """PANEL (timeouts, important): headers then FIN -- the ordinary
        shape of a TD crash mid-operation -- used to return None, so the
        client was never answered and connectivity never flipped."""
        raised, _resp, _capture = self._expect_raise('')
        self.assertIsInstance(raised, ConnectionError)

    def test_keepalive_only_then_eof_raises(self):
        raised, _resp, _capture = self._expect_raise(
            ': ping - 1\n\n: ping - 2\n\n')
        self.assertIsInstance(raised, ConnectionError)

    def test_whitespace_only_body_raises_for_a_request(self):
        raised, _resp, _capture = self._expect_raise('   \n  ')
        self.assertIsInstance(raised, ConnectionError)

    def test_garbage_plain_body_raises_for_a_request(self):
        """PANEL (runs-for-real, important): HTTP 200 with an error page
        on the Envoy port returned None and hung the client forever."""
        raised, _resp, _capture = self._expect_raise(
            '<html>502 Bad Gateway</html>')
        self.assertIsInstance(raised, ConnectionError)

    def test_truncated_plain_json_raises_for_a_request(self):
        raised, _resp, _capture = self._expect_raise(
            '{"jsonrpc":"2.0","id":1,"resu')
        self.assertIsInstance(raised, ConnectionError)

    def test_severed_stream_after_a_notification_forward_returns_none(self):
        """We posted a notification -- no answer was ever owed."""
        result, _resp, capture = self._forward(
            _PROGRESS % 1,
            message={'jsonrpc': '2.0', 'method': 'notifications/initialized'})
        self.assertIsNone(result)
        self.assertLen(_stdout_lines(capture), 1)

    def test_empty_body_returns_none_for_a_notification_forward(self):
        result, _resp, _capture = self._forward(
            '', message={'jsonrpc': '2.0', 'method': 'notifications/x'})
        self.assertIsNone(result)

    def test_truncated_response_frame_still_answers(self):
        import http.client as _http_client
        script = _sse_script(_ANSWER) + [
            _http_client.IncompleteRead(b'partial')]
        result, _resp, _capture = self._forward(script)
        self.assertEqual(result['result'], 'done')

    def test_incomplete_read_before_the_answer_is_connection_loss(self):
        """http.client raises IncompleteRead -- an HTTPException, not an
        OSError -- on a graceful FIN mid-body, so untranslated it reached
        main()'s catch-all and reported "Unexpected error" with no
        reconnect hint.  A cut transport gets one verdict."""
        import http.client as _http_client
        script = _sse_script(_PROGRESS % 1) + [
            _http_client.IncompleteRead(b'partial')]
        raised, _resp, _capture = self._expect_raise(script)
        self.assertIsInstance(raised, ConnectionError)
        self.assertIsInstance(raised, OSError)
        self.assertIn('mid-body', str(raised))

    # --- buffering bounds ---------------------------------------------

    def test_a_keepalive_stream_stops_mirroring_the_body(self):
        """PANEL (frame-parser): the mirror only stopped on a complete
        DATA frame, so a keepalive-only stream recorded forever.  Proof
        it now stops at the first SSE line: a plain-JSON payload placed
        AFTER a keepalive is no longer reachable by the fallback."""
        raised, _resp, _capture = self._expect_raise(
            ': ping\n{"jsonrpc":"2.0","id":1,"result":"unreachable"}')
        self.assertIsInstance(
            raised, ConnectionError,
            'the mirror kept recording past SSE evidence')

    def test_an_unterminated_line_past_the_cap_is_severance(self):
        script = [b'x' * 500] * 20
        with patch.object(bridge, '_MAX_BODY_BYTES', 2000):
            raised, _resp, _capture = self._expect_raise(script)
        self.assertIsInstance(raised, ConnectionError)

    def test_a_line_that_terminates_at_the_cap_cannot_skip_the_guard(self):
        """PANEL round 2: the cap check used to be gated on "this chunk
        has no newline", so a line that finally ENDED at the cap slipped
        past it -- and the bytes() copy, split, decode, strip and
        partitions then all ran over a ~128 MiB line, measured at 4x the
        nominal ceiling in transient allocation."""
        # Four chunks sit exactly AT the cap without tripping it; the cap
        # is crossed by the chunk that carries the newline, which is the
        # only shape the old gating let through.
        script = [b'x' * 500] * 4 + [b'x' * 500 + b'\n']
        with patch.object(bridge, '_MAX_BODY_BYTES', 2000):
            raised, _resp, _capture = self._expect_raise(script)
        self.assertIsInstance(
            raised, ConnectionError,
            'a newline arriving past the cap must not launder the line')
        self.assertIn('unterminated line', str(raised))

    def test_the_cap_is_measured_per_line_not_per_body(self):
        """Guard hoisting must not sever an ordinary long body: after each
        split the buffer holds only the trailing partial line, so a body
        far bigger than the cap still streams as long as its LINES fit."""
        body = ''.join(_PROGRESS % i for i in range(40)) + _ANSWER
        with patch.object(bridge, '_MAX_BODY_BYTES', 2000):
            result, _resp, capture = self._forward(body)
        self.assertEqual(result['result'], 'done')
        self.assertLen(_stdout_lines(capture), 40)

    # --- non-streaming Envoy: the compatibility floor -----------------

    def test_plain_json_body_is_returned_unchanged(self):
        body = '{"jsonrpc":"2.0","id":1,"result":"legacy"}'
        result, resp, capture = self._forward(body)
        self.assertEqual(result['result'], 'legacy')
        self.assertEqual(
            resp.timeouts, [],
            'a plain JSON answer is not a stream -- never touch the socket')
        self.assertEqual(_stdout_lines(capture), [])

    def test_plain_json_spanning_several_lines(self):
        body = '{\n  "jsonrpc": "2.0",\n  "id": 1,\n  "result": "pretty"\n}\n'
        result, _resp, _capture = self._forward(body)
        self.assertEqual(result['result'], 'pretty')

    def test_response_is_closed_on_every_path(self):
        _result, resp, _capture = self._forward('{"id":1,"result":"x"}')
        self.assertTrue(resp.closed)
        _raised, resp2, _capture2 = self._expect_raise('')
        self.assertTrue(resp2.closed)


class TestBridgeStreamEscapeHatch(EmbodyTestCase):
    """EMBODY_BRIDGE_NO_STREAM: read to EOF, then the SAME parser.

    The hatch exists because a transport regression severs the one
    channel a user has to report it.  It is NOT a revert to the
    pre-A-46 line scanner: that scanner returns the first notification
    AS the answer, so a naive hatch would trade a timeout bug for
    silently wrong tool results.  In hatch mode pushed frames are still
    delivered, but BATCHED at EOF -- progress stops being live, which
    is the documented cost of pulling the lever.
    """

    def _forward(self, body, message=None, env='1', **kwargs):
        resp = _FakeHTTPResponse(_sse_script(body))
        capture = _StdoutCapture()
        with patch('urllib.request.urlopen', return_value=resp), \
             patch.dict(bridge.os.environ, {}), \
             patch.object(sys, 'stdout', capture):
            if env is None:
                bridge.os.environ.pop(bridge._STREAM_FALLBACK_ENV, None)
            else:
                bridge.os.environ[bridge._STREAM_FALLBACK_ENV] = env
            result = bridge.forward_to_http(
                'http://127.0.0.1:9870/mcp',
                message if message is not None else {'jsonrpc': '2.0', 'id': 1},
                **kwargs)
        return result, resp, capture

    def test_hatch_still_answers_an_sse_response(self):
        result, _resp, _capture = self._forward((_PROGRESS % 1) + _ANSWER)
        self.assertEqual(result['result'], 'done')

    def test_hatch_never_returns_a_notification_as_the_response(self):
        """The single most important property of the hatch: the old line
        scanner returned this notification as the tool-call answer."""
        result, _resp, capture = self._forward((_PROGRESS % 5) + _ANSWER)
        self.assertEqual(result['result'], 'done')
        self.assertEqual(result.get('method'), None)
        lines = _stdout_lines(capture)
        self.assertLen(lines, 1)
        self.assertEqual(lines[0]['method'], 'notifications/progress')

    def test_hatch_delivers_every_pushed_frame_batched_at_eof(self):
        body = ''.join(_PROGRESS % i for i in range(3)) + _ANSWER
        result, _resp, capture = self._forward(body)
        self.assertEqual(result['result'], 'done')
        self.assertLen(_stdout_lines(capture), 3)

    def test_hatch_reads_to_eof_and_never_touches_the_socket_budget(self):
        body = ''.join(_PROGRESS % i for i in range(3)) + _ANSWER
        _result, resp, _capture = self._forward(body)
        self.assertEqual(
            resp.timeouts, [],
            'the hatch has no incremental reader, so no budget to arm')

    def test_hatch_still_handles_plain_json(self):
        result, _resp, _capture = self._forward(
            '{"jsonrpc":"2.0","id":1,"result":"plainhatch"}')
        self.assertEqual(result['result'], 'plainhatch')

    def test_hatch_still_refuses_to_leave_a_request_unanswered(self):
        raised = None
        try:
            self._forward('')
        except Exception as e:  # noqa: BLE001 -- type asserted below
            raised = e
        self.assertIsInstance(raised, ConnectionError)

    def test_hatch_is_off_by_default(self):
        """Unset, and every falsy spelling, keeps the streaming reader."""
        for env in (None, '', '0', 'false', 'NO'):
            body = (_PROGRESS % 1) + (_PROGRESS % 2) + _ANSWER
            _result, resp, _capture = self._forward(body, env=env)
            self.assertEqual(
                resp.timeouts, [bridge.SSE_IDLE_WINDOW_S],
                f'{env!r} must not disable streaming')


class TestBridgeStreamingStdoutSerialization(EmbodyTestCase):
    """Frames land on stdout from whichever thread is forwarding."""

    def test_concurrent_streams_and_responses_produce_valid_jsonl(self):
        """PANEL (concurrency, important): this test used to call
        `patch('urllib.request.urlopen')` INSIDE each worker thread.
        mock.patch is not thread-safe -- overlapping patches clobber each
        other's saved original, so workers consumed each other's scripts
        and, worse, an out-of-order __exit__ left a MagicMock installed
        on urllib.request.urlopen for the rest of the process (inside TD
        that is every later urllib user in the session).  ONE mock is
        installed around the whole thread block, dispatching by request
        id, so nothing is patched concurrently.
        """
        frames_per_stream = 20
        stream_threads = 8
        response_threads = 4
        responses_each = 50

        body = ''.join(_PROGRESS % i for i in range(frames_per_stream))
        responses = {
            worker: _FakeHTTPResponse(_sse_script(body + _ANSWER))
            for worker in range(stream_threads)
        }

        def dispatch(req, **_kw):
            return responses[json.loads(req.data.decode('utf-8'))['id']]

        capture = _StdoutCapture()
        errors = []

        def stream(worker):
            try:
                out = bridge.forward_to_http(
                    'http://127.0.0.1:9870/mcp',
                    {'jsonrpc': '2.0', 'id': worker})
                if out is None:
                    errors.append(RuntimeError('stream returned no answer'))
            except Exception as e:  # noqa: BLE001 -- surfaced below
                errors.append(e)

        def responder(worker):
            try:
                for i in range(responses_each):
                    bridge.send_response(
                        {'jsonrpc': '2.0', 'id': f'{worker}-{i}',
                         'result': 'ok'})
            except Exception as e:  # noqa: BLE001 -- surfaced below
                errors.append(e)

        with patch('urllib.request.urlopen', side_effect=dispatch), \
             patch.object(sys, 'stdout', capture):
            threads = (
                [threading.Thread(target=stream, args=(w,), name=f'stream-{w}')
                 for w in range(stream_threads)]
                + [threading.Thread(target=responder, args=(w,),
                                    name=f'resp-{w}')
                   for w in range(response_threads)])
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)
                self.assertFalse(t.is_alive(), f'{t.name} did not finish')

        self.assertEqual(
            errors, [],
            f'workers raised: {[type(e).__name__ + ": " + str(e) for e in errors]}')

        lines = [l for l in capture.getvalue().split('\n') if l.strip()]
        expected = (stream_threads * frames_per_stream
                    + response_threads * responses_each)
        self.assertEqual(
            len(lines), expected,
            f'expected {expected} lines, got {len(lines)}')

        bad = []
        for idx, line in enumerate(lines):
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as e:
                bad.append((idx, f'invalid JSON: {e}: {line[:80]}'))
                continue
            if not isinstance(obj, dict) or obj.get('jsonrpc') != '2.0':
                bad.append((idx, f'not a JSON-RPC object: {line[:80]}'))
        self.assertEqual(
            bad, [],
            f'{len(bad)} corrupt lines (stdout lock is not covering the '
            f'frame writer): {bad[:5]}')

    def test_the_suite_never_patches_urlopen_inside_a_thread(self):
        """Tripwire for the leak above: a patch that unwinds out of order
        reinstalls a MagicMock process-wide, and inside TouchDesigner
        that poisons every later urllib user in the session."""
        import urllib.request as _urlreq
        self.assertNotEqual(
            type(_urlreq.urlopen).__name__, 'MagicMock',
            'urllib.request.urlopen was left patched by an earlier test')
        source = open(os.path.abspath(__file__), 'r', encoding='utf-8').read()
        marker = 'def stream(worker):'
        body = source[source.index(marker):source.index(marker) + 600]
        self.assertNotIn(
            "patch('urllib.request.urlopen'", body,
            'the concurrent worker must not install its own patch')

    def test_notification_write_survives_a_closed_stdout(self):
        class _Broken:
            def write(self, _s):
                raise BrokenPipeError('client went away')

            def flush(self):
                pass

        with patch.object(sys, 'stdout', _Broken()):
            bridge.deliver_server_message(
                {'jsonrpc': '2.0', 'method': 'notifications/progress'})


class TestBridgeStreamingLoopback(EmbodyTestCase):
    """One real socket, one real chunked SSE server.

    Everything above scripts the response object; this proves the
    assumptions the design rests on -- read1 returns the instant bytes
    land, the budget really bounds a stalled peer, and a severance on
    the wire really reaches the main loop -- on THIS platform.
    """

    def _serve(self, handler_body):
        import http.server

        outer_body = handler_body

        class _Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def do_POST(self):
                length = int(self.headers.get('Content-Length', 0))
                self.rfile.read(length)
                try:
                    outer_body(self)
                except (BrokenPipeError, ConnectionError, OSError, ValueError):
                    pass  # The test cut this connection on purpose

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), _Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread

    @staticmethod
    def _sse_headers(handler):
        handler.send_response(200)
        handler.send_header('Content-Type', 'text/event-stream')
        handler.send_header('Transfer-Encoding', 'chunked')
        handler.end_headers()

    @staticmethod
    def _chunk(wfile, text):
        raw = text.encode('utf-8')
        wfile.write(b'%X\r\n' % len(raw) + raw + b'\r\n')
        wfile.flush()

    def _forward(self, body, message=None, **kwargs):
        server, _thread = self._serve(body)
        try:
            port = server.server_address[1]
            started = time.monotonic()
            raised = None
            result = None
            try:
                result = bridge.forward_to_http(
                    f'http://127.0.0.1:{port}/mcp',
                    message if message is not None
                    else {'jsonrpc': '2.0', 'id': 1},
                    **kwargs)
            except Exception as e:  # noqa: BLE001 -- classified by callers
                raised = e
            elapsed = time.monotonic() - started
        finally:
            server.shutdown()
            server.server_close()
        return result, raised, elapsed

    # --- per-frame delivery on a real socket --------------------------

    def test_a_frame_is_delivered_while_the_response_is_still_open(self):
        """The server refuses to send the answer until the client has
        already ACTED on the first notification.  Under a read-to-EOF
        forwarder that is a deadlock; passing it is proof of per-frame
        delivery."""
        delivered = threading.Event()
        acted_before_answer = []

        def body(handler):
            self._sse_headers(handler)
            self._chunk(handler.wfile,
                        'event: message\r\n'
                        'data: {"jsonrpc":"2.0",'
                        '"method":"notifications/progress",'
                        '"params":{"pct":10}}\r\n\r\n')
            acted_before_answer.append(delivered.wait(timeout=10))
            self._chunk(handler.wfile,
                        'data: {"jsonrpc":"2.0","id":1,'
                        '"result":"finished"}\n\n')
            handler.wfile.write(b'0\r\n\r\n')
            handler.wfile.flush()

        got = []

        def sink(msg):
            got.append(msg)
            delivered.set()

        result, raised, _elapsed = self._forward(
            body, timeout=20, on_notification=sink)

        self.assertIsNone(raised)
        self.assertEqual(
            acted_before_answer, [True],
            'the server waited for the client to act on frame 1 and timed '
            'out -- the body is still being buffered to EOF')
        self.assertLen(got, 1)
        self.assertEqual(result['result'], 'finished')

    def test_the_default_sink_writes_stdout_mid_stream(self):
        written = threading.Event()

        class _GatedCapture(_StdoutCapture):
            def write(self, s):
                _StdoutCapture.write(self, s)
                written.set()

        capture = _GatedCapture()
        acted_before_answer = []

        def body(handler):
            self._sse_headers(handler)
            self._chunk(handler.wfile,
                        'data: {"jsonrpc":"2.0",'
                        '"method":"notifications/progress",'
                        '"params":{"pct":25}}\n\n')
            acted_before_answer.append(written.wait(timeout=10))
            self._chunk(handler.wfile,
                        'data: {"jsonrpc":"2.0","id":1,"result":"live"}\n\n')
            handler.wfile.write(b'0\r\n\r\n')
            handler.wfile.flush()

        with patch.object(sys, 'stdout', capture):
            result, raised, _elapsed = self._forward(body, timeout=20)

        self.assertIsNone(raised)
        self.assertEqual(acted_before_answer, [True])
        self.assertEqual(result['result'], 'live')
        lines = _stdout_lines(capture)
        self.assertLen(lines, 1)
        self.assertEqual(lines[0]['params'], {'pct': 25})

    def test_three_notifications_stream_ahead_of_the_answer(self):
        seen = []
        gate = threading.Event()

        def body(handler):
            self._sse_headers(handler)
            for i in range(3):
                self._chunk(
                    handler.wfile,
                    'data: {"jsonrpc":"2.0","method":"notifications/progress",'
                    '"params":{"step":%d}}\n\n' % i)
            gate.wait(timeout=10)
            self._chunk(handler.wfile,
                        'data: {"jsonrpc":"2.0","id":1,"result":"ok"}\n\n')
            handler.wfile.write(b'0\r\n\r\n')
            handler.wfile.flush()

        def sink(msg):
            seen.append(msg['params']['step'])
            if len(seen) == 3:
                gate.set()

        result, raised, _elapsed = self._forward(
            body, timeout=20, on_notification=sink)
        self.assertIsNone(raised)
        self.assertEqual(seen, [0, 1, 2])
        self.assertEqual(result['result'], 'ok')

    # --- the idle window, on real time --------------------------------

    def test_one_early_frame_then_long_silence_still_answers(self):
        """PANEL (frame-parser, important) -- A/B reproduction.

        One progress frame, then the server works SILENTLY for far
        longer than the idle window, then answers.  Before the per-frame
        renewal fix this died at the idle window and the client was told
        the link dropped for work that completed.
        """
        def body(handler):
            self._sse_headers(handler)
            self._chunk(handler.wfile,
                        'data: {"jsonrpc":"2.0",'
                        '"method":"notifications/progress"}\n\n')
            time.sleep(1.2)
            self._chunk(handler.wfile,
                        'data: {"jsonrpc":"2.0","id":1,"result":"slow"}\n\n')
            handler.wfile.write(b'0\r\n\r\n')
            handler.wfile.flush()

        got = []
        result, raised, _elapsed = self._forward(
            body, timeout=20, idle_timeout=0.4, on_notification=got.append)
        self.assertIsNone(
            raised, f'a lone frame must not shorten the cap: {raised!r}')
        self.assertEqual(result['result'], 'slow')
        self.assertLen(got, 1)

    def test_a_cadence_of_frames_renews_its_allowance(self):
        """Gaps under the idle window, for longer in total than the idle
        window -- every frame renews the budget, so the stream lives."""
        def body(handler):
            self._sse_headers(handler)
            for i in range(6):
                self._chunk(
                    handler.wfile,
                    'data: {"jsonrpc":"2.0","method":"notifications/progress",'
                    '"params":{"step":%d}}\n\n' % i)
                time.sleep(0.2)
            self._chunk(handler.wfile,
                        'data: {"jsonrpc":"2.0","id":1,"result":"cadence"}\n\n')
            handler.wfile.write(b'0\r\n\r\n')
            handler.wfile.flush()

        got = []
        result, raised, elapsed = self._forward(
            body, timeout=20, idle_timeout=0.6, on_notification=got.append)
        self.assertIsNone(raised, f'{raised!r}')
        self.assertEqual(result['result'], 'cadence')
        self.assertLen(got, 6)
        self.assertGreater(
            elapsed, 0.6,
            'the stream outlived a single idle window, as intended')

    def test_silence_past_the_idle_window_after_a_cadence_fails_fast(self):
        """Once a cadence is established, prolonged silence IS severance
        -- and it must be bound by the idle window, not the cap."""
        def body(handler):
            self._sse_headers(handler)
            for i in range(3):
                self._chunk(
                    handler.wfile,
                    'data: {"jsonrpc":"2.0","method":"notifications/progress",'
                    '"params":{"step":%d}}\n\n' % i)
                time.sleep(0.1)
            time.sleep(6)

        got = []
        _result, raised, elapsed = self._forward(
            body, timeout=20, idle_timeout=0.5, on_notification=got.append)
        self.assertIsInstance(raised, OSError)
        self.assertLen(got, 3)
        self.assertLess(
            elapsed, 5.0,
            f'severance must be idle-bound, not cap-bound: {elapsed:.2f}s')

    def test_a_byte_dribbler_cannot_outlive_the_cap(self):
        """PANEL (timeouts): a peer that trickles bytes without ever
        completing a line reset the per-recv socket timeout forever, so
        a blocking readline pinned the stdin dispatch loop with neither
        bound applying.  read1 + a per-read budget check closes it."""
        def body(handler):
            self._sse_headers(handler)
            self._chunk(handler.wfile, 'data: {"jsonrpc":"2.0",')
            for _ in range(60):
                time.sleep(0.1)
                self._chunk(handler.wfile, ' ')

        _result, raised, elapsed = self._forward(body, timeout=2.0)
        self.assertIsInstance(
            raised, OSError,
            f'a dribbling peer must be severed, got {raised!r}')
        self.assertLess(
            elapsed, 8.0,
            f'the cap must hold against a dribbler: {elapsed:.2f}s')

    # --- severance on the wire ----------------------------------------

    def test_server_closing_mid_stream_is_connection_loss(self):
        got = []

        def body(handler):
            self._sse_headers(handler)
            self._chunk(handler.wfile,
                        'data: {"jsonrpc":"2.0",'
                        '"method":"notifications/progress"}\n\n')
            handler.close_connection = True
            handler.wfile.close()

        _result, raised, _elapsed = self._forward(
            body, timeout=20, on_notification=got.append)
        self.assertLen(got, 1, 'the frame that did arrive was delivered')
        self.assertIsInstance(raised, OSError, f'{raised!r}')

    def test_headers_then_graceful_close_is_connection_loss(self):
        """PANEL (compat, important): a graceful FIN with no body used to
        raise IncompleteRead out of resp.read(); read1 returns b'' there,
        so without the guard the client was silently never answered."""
        def body(handler):
            self._sse_headers(handler)
            handler.close_connection = True
            handler.wfile.close()

        _result, raised, _elapsed = self._forward(body, timeout=20)
        self.assertIsInstance(raised, OSError, f'{raised!r}')

    def test_content_length_truncated_body_is_connection_loss(self):
        """The Content-Length variant of the same severance."""
        def body(handler):
            payload = ('event: message\r\ndata: {"jsonrpc":"2.0","id":1,'
                       '"result":"never-arrives"}\r\n\r\n')
            handler.send_response(200)
            handler.send_header('Content-Type', 'text/event-stream')
            handler.send_header('Content-Length', str(len(payload) + 500))
            handler.end_headers()
            handler.wfile.write(payload[:15].encode('utf-8'))
            handler.wfile.flush()
            handler.close_connection = True
            handler.wfile.close()

        _result, raised, _elapsed = self._forward(body, timeout=20)
        self.assertIsInstance(raised, OSError, f'{raised!r}')

    def test_chunked_eof_mid_body_is_connection_loss(self):
        """The chunked variant: a chunk header promising bytes that never
        arrive, then FIN."""
        def body(handler):
            self._sse_headers(handler)
            handler.wfile.write(b'40\r\ndata: {"jsonrpc":')
            handler.wfile.flush()
            handler.close_connection = True
            handler.wfile.close()

        _result, raised, _elapsed = self._forward(body, timeout=20)
        self.assertIsInstance(raised, OSError, f'{raised!r}')

    # --- what the cap does NOT cover ----------------------------------

    def _raw_header_server(self, gap, pieces, payload):
        """A peer that dribbles its status line and headers.

        Written raw rather than through send_response so the header phase
        can be paced -- that phase belongs to urlopen, and no budget of
        ours runs during it.
        """
        head = ('HTTP/1.1 200 OK\r\n'
                'Content-Type: text/event-stream\r\n'
                'Content-Length: %d\r\n'
                'Connection: close\r\n\r\n' % len(payload)).encode('utf-8')
        step = max(1, len(head) // pieces)

        def body(handler):
            for i in range(0, len(head), step):
                handler.wfile.write(head[i:i + step])
                handler.wfile.flush()
                time.sleep(gap)
            handler.wfile.write(payload.encode('utf-8'))
            handler.wfile.flush()
            handler.close_connection = True

        return body

    def test_slow_headers_within_the_cap_still_complete(self):
        """Paced headers are not an error -- they just are not budgeted."""
        answer = 'data: {"jsonrpc":"2.0","id":1,"result":"slowhdr"}\n\n'
        result, raised, _elapsed = self._forward(
            self._raw_header_server(0.05, 4, answer), timeout=5)
        self.assertIsNone(raised, f'{raised!r}')
        self.assertEqual(result['result'], 'slowhdr')

    def test_the_header_phase_is_not_covered_by_the_cap(self):
        """PANEL round 2 (important), pinned as CURRENT behavior, not as
        a property we want.

        urlopen reads the status line and headers itself, bounded by the
        per-recv socket timeout that every arriving byte resets, so a
        peer dribbling headers under that timeout runs past the cap; the
        budget only bites once the body phase starts.  This is
        byte-identical to shipped behavior and fixing it means rewriting
        connection setup, so the code documents it instead of claiming
        an absolute cap it does not hold.  If a future change DOES bound
        the header phase, this test is the one to delete.
        """
        answer = 'data: {"jsonrpc":"2.0","id":1,"result":"late"}\n\n'
        cap = 0.5
        _result, raised, elapsed = self._forward(
            self._raw_header_server(0.25, 4, answer), timeout=cap)
        self.assertGreater(
            elapsed, cap,
            'the header phase is expected to be able to exceed the cap; '
            'if this now holds, the cap became absolute and the comments '
            'in envoy_bridge.py should say so')
        self.assertIsInstance(
            raised, OSError,
            'once the body phase starts the budget must sever it, so the '
            f'overrun is bounded by the peer, not unbounded: {raised!r}')

    def test_plain_json_over_a_real_socket_still_works(self):
        def body(handler):
            payload = b'{"jsonrpc":"2.0","id":1,"result":"plain"}'
            handler.send_response(200)
            handler.send_header('Content-Type', 'application/json')
            handler.send_header('Content-Length', str(len(payload)))
            handler.end_headers()
            handler.wfile.write(payload)

        result, raised, _elapsed = self._forward(body, timeout=20)
        self.assertIsNone(raised)
        self.assertEqual(result['result'], 'plain')

    def test_a_large_single_line_response_survives_chunked_reads(self):
        """The live Envoy puts a whole tools/list on ONE data: line."""
        big = 'y' * 400000

        def body(handler):
            self._sse_headers(handler)
            self._chunk(
                handler.wfile,
                'data: {"jsonrpc":"2.0","id":1,"result":"%s"}\n\n' % big)
            handler.wfile.write(b'0\r\n\r\n')
            handler.wfile.flush()

        result, raised, _elapsed = self._forward(body, timeout=20)
        self.assertIsNone(raised)
        self.assertEqual(len(result['result']), len(big))


class TestBridgeMainLoopStreamingWiring(EmbodyTestCase):
    """main() has to hand the frame router something to invalidate."""

    def test_main_registers_a_tools_cache_invalidator(self):
        bridge.set_tools_cache_invalidator(None)
        stdin = io.StringIO(
            json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'test'}) + '\n')
        with patch.object(sys, 'stdin', stdin), \
             patch.object(sys, 'stdout', io.StringIO()), \
             patch.object(sys, 'stderr', io.StringIO()), \
             patch.object(sys, 'argv', ['envoy_bridge.py']), \
             patch.object(bridge, 'wait_for_envoy', return_value=True), \
             patch.object(bridge, 'forward_to_http', return_value=None), \
             patch.object(bridge, 'find_td_pid', return_value=None), \
             patch.object(bridge, 'kill_stale_bridges'), \
             patch('time.sleep'):
            bridge.main()
        self.assertIsNotNone(
            bridge._tools_cache_invalidator,
            'a server-pushed tools/list_changed has nothing to clear')
        bridge._tools_cache_invalidator()  # must not raise

    def test_list_changed_drops_the_cached_tool_list(self):
        """A tools/call streams a list_changed back mid-operation; the
        tools/list that follows inside the 5s cache window must REFETCH.
        The sibling test test_second_tools_list_within_window_uses_cache
        pins the no-invalidation case at exactly one forward."""
        forwards = [0]

        def forward(url, msg, **kwargs):
            method = msg.get('method')
            if method == 'tools/list':
                forwards[0] += 1
            elif method == 'tools/call':
                bridge._tools_cache_invalidator()
            return {'jsonrpc': '2.0', 'id': msg.get('id'),
                    'result': {'tools': [{'name': 'create_op',
                                          'description': 'create'}]}}

        msgs = [
            {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize'},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
            {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call',
             'params': {'name': 'query_network'}},
            {'jsonrpc': '2.0', 'id': 4, 'method': 'tools/list'},
        ]
        stdin = io.StringIO('\n'.join(json.dumps(m) for m in msgs) + '\n')
        with patch.object(sys, 'stdin', stdin), \
             patch.object(sys, 'stdout', io.StringIO()), \
             patch.object(sys, 'stderr', io.StringIO()), \
             patch.object(sys, 'argv', ['envoy_bridge.py']), \
             patch.object(bridge, 'wait_for_envoy', return_value=True), \
             patch.object(bridge, 'forward_to_http', side_effect=forward), \
             patch.object(bridge, 'find_td_pid', return_value=None), \
             patch.object(bridge, 'kill_stale_bridges'), \
             patch('time.sleep'):
            bridge.main()

        self.assertEqual(
            forwards[0], 2,
            'the second tools/list was served from a surface the server '
            'already invalidated')


class TestBridgeStreamingDocumentedLimits(EmbodyTestCase):
    """Comments in this file have twice asserted safety the code lacked.

    Both were caught by the panel, both are pinned here so the prose and
    the behavior cannot drift apart again.
    """

    def test_the_docstring_does_not_claim_the_deadlock_is_gone(self):
        """PANEL (runs-for-real): the reader made server-pushed
        NOTIFICATIONS deliverable; a server-to-client REQUEST still
        cannot be answered until the forward returns, because the stdin
        dispatch loop is parked inside it."""
        doc = ' '.join((bridge.forward_to_http.__doc__ or '').split())
        self.assertNotIn('must answer without deadlocking', doc)
        self.assertIn('cannot be answered until the forward returns', doc)

    def test_the_cap_comment_names_what_it_actually_bounds(self):
        source = open(_bridge_path, 'r', encoding='utf-8').read()
        head = source[:source.index('# Reconciler tick intervals')]
        self.assertIn('REQUEST_TIMEOUT_S', head)
        self.assertIn('re-armed', head,
                      'the cap only holds because every read is re-armed '
                      'against what is left of it -- say so')
        self.assertIn(
            'FIRST BODY', head,
            'the cap starts at the first body byte; the header phase is '
            'per-recv only (see test_the_header_phase_is_not_covered_by_'
            'the_cap) and the comment must not claim otherwise')

    def test_the_hatch_docstring_names_every_bound_it_drops(self):
        """Batching is not the hatch's only cost -- it drops the cap, the
        idle window and the accumulation bound too, and an operator
        flipping it during an incident needs to know that."""
        doc = ' '.join((bridge.forward_to_http.__doc__ or '').split())
        self.assertIn('NO cap, NO idle window, and NO accumulation', doc)
        self.assertIn('per-recv socket timeout alone', doc)

    def test_the_max_body_comment_admits_the_accumulators_are_separate(self):
        source = open(_bridge_path, 'r', encoding='utf-8').read()
        head = source[:source.index('# Reconciler tick intervals')]
        self.assertIn(
            'INDEPENDENT', head,
            'mirror, line buffer and frame data are separate accumulators, '
            'so peak memory is a MULTIPLE of _MAX_BODY_BYTES -- say so')


class TestConvoyUpdateEmbody(EmbodyTestCase):
    """convoy_update_embody: fleet self-update without the TD Python grant."""

    ROWS = [
        {'node_id': 'n-1', 'host_id': 'h-1', 'convoy_id': 'studio',
         'node_name': 'TEC-A / Render', 'hostname': 'TEC-A',
         'embody_version': '6.0.246', 'status': 'online', 'online': True,
         'enabled': True, 'perform_mode': False},
        {'node_id': 'n-2', 'host_id': 'h-2', 'convoy_id': 'studio',
         'node_name': 'TEC-B / Show', 'hostname': 'TEC-B',
         'embody_version': '6.0.253', 'status': 'offline', 'online': False,
         'enabled': True, 'perform_mode': False},
        {'node_id': 'n-3', 'host_id': 'h-1', 'convoy_id': 'studio',
         'node_name': 'TEC-C / Stage', 'hostname': 'TEC-C',
         'embody_version': '6.0.253', 'status': 'online', 'online': True,
         'enabled': True, 'perform_mode': True},
    ]

    def _listing(self):
        return {'ok': True, 'nodes': [dict(r) for r in self.ROWS]}

    def test_requires_exactly_one_of_node_or_all(self):
        for params in ({}, {'node': 'x', 'all': True}):
            out = bridge.handle_convoy_update_embody(params)
            self.assertFalse(out.get('ok'))
            self.assertEqual(out.get('reason'), 'invalid_arguments')

    def test_all_dispatches_online_awake_nodes_and_names_the_skips(self):
        calls = []

        def fake_call(call):
            calls.append(dict(call))
            return {'ok': True, 'delivery_id': 'd-%d' % len(calls)}

        with patch.object(bridge, 'handle_convoy_list_nodes',
                          return_value=self._listing()), \
             patch.object(bridge, 'handle_convoy_call',
                          side_effect=fake_call):
            out = bridge.handle_convoy_update_embody({'all': True})
        self.assertTrue(out['ok'])
        # Only n-1 dispatches: n-2 is offline, n-3 is mid-show.
        self.assertEqual([d['node_id'] for d in out['dispatched']], ['n-1'])
        self.assertEqual(
            sorted(row['reason'] for row in out['skipped']),
            ['offline', 'perform_mode'])
        call = calls[0]
        self.assertEqual(call['operation'], 'update_embody')
        self.assertIs(call['wait'], False)
        self.assertEqual(call['target_host_id'], 'h-1')
        self.assertTrue(call['idempotency_key'].startswith('update-embody-'))
        self.assertEqual(out['dispatched'][0]['delivery_id'], 'd-1')

    def test_node_matches_by_name_id_or_hostname_and_refuses_ambiguity(self):
        with patch.object(bridge, 'handle_convoy_list_nodes',
                          return_value=self._listing()), \
             patch.object(bridge, 'handle_convoy_call',
                          return_value={'ok': True, 'delivery_id': 'd'}):
            by_host = bridge.handle_convoy_update_embody({'node': 'TEC-A'})
            missing = bridge.handle_convoy_update_embody({'node': 'nope'})
        self.assertTrue(by_host['ok'])
        self.assertEqual(by_host['dispatched'][0]['node_id'], 'n-1')
        self.assertEqual(missing['reason'], 'node_not_found')
        self.assertIn('TEC-A / Render', missing['known'])
        twins = {'ok': True, 'nodes': [
            dict(self.ROWS[0]), dict(self.ROWS[0], node_id='n-9')]}
        with patch.object(bridge, 'handle_convoy_list_nodes',
                          return_value=twins):
            ambiguous = bridge.handle_convoy_update_embody({'node': 'TEC-A'})
        self.assertEqual(ambiguous['reason'], 'node_ambiguous')

    def test_directory_failure_is_returned_not_swallowed(self):
        with patch.object(bridge, 'handle_convoy_list_nodes', return_value={
                'ok': False, 'reason': 'convoy_host_unreachable'}):
            out = bridge.handle_convoy_update_embody({'all': True})
        self.assertEqual(out['reason'], 'convoy_host_unreachable')

    def test_a_refused_dispatch_is_reported_per_node(self):
        with patch.object(bridge, 'handle_convoy_list_nodes',
                          return_value=self._listing()), \
             patch.object(bridge, 'handle_convoy_call', return_value={
                 'ok': False, 'reason': 'unknown_operation',
                 'detail': 'node predates update_embody'}):
            out = bridge.handle_convoy_update_embody({'node': 'n-1'})
        self.assertTrue(out['ok'])
        entry = out['dispatched'][0]
        self.assertFalse(entry['ok'])
        self.assertEqual(entry['reason'], 'unknown_operation')


class TestConvoyCallPayloadDedup(EmbodyTestCase):
    """The terminal job payload rides ONCE: in job, never again in remote."""

    def test_remote_envelope_carries_no_duplicate_job(self):
        submit = {'ok': True, 'job': {'delivery_id': 'd-1',
                                      'state': 'queued', 'updated': 1.0}}
        terminal = {'ok': True, 'cursor': 2.0,
                    'job': {'delivery_id': 'd-1', 'state': 'succeeded',
                            'updated': 2.0,
                            'result': {'payload': 'X' * 512}}}
        responses = [submit, terminal]

        def fake_host_call(method, path, body=None, timeout=None):
            return dict(responses.pop(0)) if responses else dict(terminal)

        with patch.object(bridge, 'convoy_host_call',
                          side_effect=fake_host_call):
            out = bridge.handle_convoy_call({
                'target_host_id': 'h-1', 'convoy_id': 'studio',
                'target_node_id': 'n-1', 'operation': 'query_network'})
        self.assertEqual(out['job']['state'], 'succeeded')
        self.assertEqual(out['job']['result']['payload'], 'X' * 512)
        self.assertIn('remote', out)
        self.assertNotIn('job', out['remote'],
                         'the job payload must not ship twice')
