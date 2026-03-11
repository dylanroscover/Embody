"""
Test suite: Envoy thread communication and request processing.

Tests the main-thread side of the dual-thread architecture:
- _onRefresh: request queue polling and frame throttling
- _send_response: response queue and log piggybacking
- Request/response round-trip via queues
- Invalid payload handling
- MAX_REQUESTS_PER_FRAME throttling
- Deferred operation handling (None results)
"""

from queue import Queue

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase


class TestOnRefreshProcessing(EmbodyTestCase):
    """Test _onRefresh request processing from queue."""

    def setUp(self):
        super().setUp()
        self.envoy = self.embody.ext.Envoy

    def test_processes_valid_request(self):
        """A valid get_td_info request is processed and response queued."""
        # Drain any pre-existing responses
        while not self.envoy.response_queue.empty():
            self.envoy.response_queue.get_nowait()

        self.envoy.request_queue.put({
            'id': 9001,
            'operation': 'get_td_info',
            'params': {}
        })
        self.envoy._onRefresh()

        self.assertFalse(self.envoy.response_queue.empty(),
                         'Response should be in queue')
        resp = self.envoy.response_queue.get_nowait()
        self.assertEqual(resp['id'], 9001)
        self.assertDictHasKey(resp['result'], 'version')

    def test_processes_multiple_requests_in_one_frame(self):
        """Multiple queued requests are processed in a single _onRefresh call."""
        while not self.envoy.response_queue.empty():
            self.envoy.response_queue.get_nowait()

        for i in range(3):
            self.envoy.request_queue.put({
                'id': 8000 + i,
                'operation': 'get_td_info',
                'params': {}
            })

        self.envoy._onRefresh()

        responses = []
        while not self.envoy.response_queue.empty():
            responses.append(self.envoy.response_queue.get_nowait())
        self.assertEqual(len(responses), 3)

    def test_frame_throttle_limits_to_five(self):
        """MAX_REQUESTS_PER_FRAME=5: only 5 processed per call, rest remain queued."""
        while not self.envoy.response_queue.empty():
            self.envoy.response_queue.get_nowait()

        # Queue 8 requests
        for i in range(8):
            self.envoy.request_queue.put({
                'id': 7000 + i,
                'operation': 'get_td_info',
                'params': {}
            })

        self.envoy._onRefresh()

        # Should have processed exactly 5
        responses = []
        while not self.envoy.response_queue.empty():
            responses.append(self.envoy.response_queue.get_nowait())
        self.assertEqual(len(responses), 5)

        # 3 should remain in request queue
        remaining = 0
        while not self.envoy.request_queue.empty():
            self.envoy.request_queue.get_nowait()
            remaining += 1
        self.assertEqual(remaining, 3)

    def test_second_refresh_processes_remaining(self):
        """After throttling, a second _onRefresh picks up the rest."""
        while not self.envoy.response_queue.empty():
            self.envoy.response_queue.get_nowait()

        for i in range(7):
            self.envoy.request_queue.put({
                'id': 6000 + i,
                'operation': 'get_td_info',
                'params': {}
            })

        self.envoy._onRefresh()  # Processes 5
        self.envoy._onRefresh()  # Processes remaining 2

        responses = []
        while not self.envoy.response_queue.empty():
            responses.append(self.envoy.response_queue.get_nowait())
        self.assertEqual(len(responses), 7)

    def test_empty_queue_no_error(self):
        """_onRefresh with empty queue doesn't raise."""
        while not self.envoy.request_queue.empty():
            self.envoy.request_queue.get_nowait()
        while not self.envoy.response_queue.empty():
            self.envoy.response_queue.get_nowait()

        self.envoy._onRefresh()  # Should not raise

        self.assertTrue(self.envoy.response_queue.empty())

    def test_invalid_payload_skipped(self):
        """Non-dict or missing 'operation' key is skipped without crashing."""
        while not self.envoy.response_queue.empty():
            self.envoy.response_queue.get_nowait()

        # Invalid: not a dict
        self.envoy.request_queue.put('garbage')
        # Invalid: dict without 'operation'
        self.envoy.request_queue.put({'id': 1, 'params': {}})
        # Valid
        self.envoy.request_queue.put({
            'id': 5001,
            'operation': 'get_td_info',
            'params': {}
        })

        self.envoy._onRefresh()

        # Only the valid request should produce a response
        responses = []
        while not self.envoy.response_queue.empty():
            responses.append(self.envoy.response_queue.get_nowait())
        self.assertEqual(len(responses), 1)
        self.assertEqual(responses[0]['id'], 5001)

    def test_unknown_operation_returns_error(self):
        """Unknown operation name produces an error result (not a crash)."""
        while not self.envoy.response_queue.empty():
            self.envoy.response_queue.get_nowait()

        self.envoy.request_queue.put({
            'id': 4001,
            'operation': 'nonexistent_op_xyz',
            'params': {}
        })

        self.envoy._onRefresh()

        resp = self.envoy.response_queue.get_nowait()
        self.assertEqual(resp['id'], 4001)
        self.assertDictHasKey(resp['result'], 'error')
        self.assertIn('Unknown operation', resp['result']['error'])

    def test_handler_error_returns_error_result(self):
        """Handler that encounters an error returns error dict, not crash."""
        while not self.envoy.response_queue.empty():
            self.envoy.response_queue.get_nowait()

        self.envoy.request_queue.put({
            'id': 4002,
            'operation': 'get_op',
            'params': {'op_path': '/absolutely_nonexistent_test_op'}
        })

        self.envoy._onRefresh()

        resp = self.envoy.response_queue.get_nowait()
        self.assertEqual(resp['id'], 4002)
        self.assertDictHasKey(resp['result'], 'error')

    def test_request_id_preserved_in_response(self):
        """The request id is faithfully echoed in the response."""
        while not self.envoy.response_queue.empty():
            self.envoy.response_queue.get_nowait()

        self.envoy.request_queue.put({
            'id': 12345,
            'operation': 'get_td_info',
            'params': {}
        })

        self.envoy._onRefresh()

        resp = self.envoy.response_queue.get_nowait()
        self.assertEqual(resp['id'], 12345)

    def test_params_default_to_empty_dict(self):
        """Request without 'params' key defaults to empty dict."""
        while not self.envoy.response_queue.empty():
            self.envoy.response_queue.get_nowait()

        self.envoy.request_queue.put({
            'id': 3001,
            'operation': 'get_td_info',
            # no 'params' key
        })

        self.envoy._onRefresh()

        resp = self.envoy.response_queue.get_nowait()
        self.assertEqual(resp['id'], 3001)
        # get_td_info needs no params, so it should succeed
        self.assertNotIn('error', resp['result'])


class TestSendResponse(EmbodyTestCase):
    """Test _send_response queue output and log piggybacking."""

    def setUp(self):
        super().setUp()
        self.envoy = self.embody.ext.Envoy
        while not self.envoy.response_queue.empty():
            self.envoy.response_queue.get_nowait()

    def test_response_in_queue(self):
        """_send_response puts response dict in the queue."""
        self.envoy._send_response(999, {'data': 'hello'})
        resp = self.envoy.response_queue.get_nowait()
        self.assertEqual(resp['id'], 999)
        self.assertEqual(resp['result']['data'], 'hello')

    def test_response_preserves_result_contents(self):
        """Complex result dicts are preserved."""
        result = {
            'operators': [{'name': 'noise1', 'type': 'noiseTOP'}],
            'count': 1,
        }
        self.envoy._send_response(888, result)
        resp = self.envoy.response_queue.get_nowait()
        self.assertEqual(resp['result']['count'], 1)
        self.assertEqual(resp['result']['operators'][0]['name'], 'noise1')

    def test_log_piggybacking_adds_logs_key(self):
        """If log_buffer has recent entries, they're piggybacked on response."""
        # Generate a log entry
        self.embody.Log('test log entry for piggybacking', 'INFO')

        # Reset last_served_log_id to 0 so all logs are "new"
        self.envoy._last_served_log_id = 0

        result = {'data': 'test'}
        self.envoy._send_response(777, result)
        resp = self.envoy.response_queue.get_nowait()

        # If log buffer exists and has entries, _logs should be present
        log_buffer = getattr(self.embody.ext.Embody, '_log_buffer', None)
        if log_buffer and len(log_buffer) > 0:
            self.assertDictHasKey(resp['result'], '_logs')
            self.assertGreater(len(resp['result']['_logs']), 0)

    def test_log_piggybacking_updates_last_served_id(self):
        """After piggybacking, _last_served_log_id advances."""
        self.embody.Log('advance log id test', 'INFO')
        self.envoy._last_served_log_id = 0

        self.envoy._send_response(666, {'data': 'x'})

        log_buffer = getattr(self.embody.ext.Embody, '_log_buffer', None)
        if log_buffer and len(log_buffer) > 0:
            self.assertGreater(self.envoy._last_served_log_id, 0)

    def test_multiple_responses_ordered(self):
        """Multiple _send_response calls maintain FIFO order."""
        self.envoy._send_response(1, {'a': 1})
        self.envoy._send_response(2, {'b': 2})
        self.envoy._send_response(3, {'c': 3})

        ids = []
        while not self.envoy.response_queue.empty():
            ids.append(self.envoy.response_queue.get_nowait()['id'])
        self.assertListEqual(ids, [1, 2, 3])


class TestRequestResponseRoundTrip(EmbodyTestCase):
    """End-to-end: queue request, call _onRefresh, read response."""

    def setUp(self):
        super().setUp()
        self.envoy = self.embody.ext.Envoy
        # Drain both queues
        while not self.envoy.request_queue.empty():
            self.envoy.request_queue.get_nowait()
        while not self.envoy.response_queue.empty():
            self.envoy.response_queue.get_nowait()

    def test_round_trip_get_td_info(self):
        self.envoy.request_queue.put({
            'id': 100,
            'operation': 'get_td_info',
            'params': {}
        })
        self.envoy._onRefresh()

        resp = self.envoy.response_queue.get_nowait()
        self.assertEqual(resp['id'], 100)
        self.assertDictHasKey(resp['result'], 'version')
        self.assertDictHasKey(resp['result'], 'osName')

    def test_round_trip_get_op(self):
        comp = self.sandbox.create(baseCOMP, 'rt_test')
        self.envoy.request_queue.put({
            'id': 101,
            'operation': 'get_op',
            'params': {'op_path': comp.path}
        })
        self.envoy._onRefresh()

        resp = self.envoy.response_queue.get_nowait()
        self.assertEqual(resp['id'], 101)
        self.assertNotIn('error', resp['result'])
        self.assertEqual(resp['result']['name'], 'rt_test')

    def test_round_trip_query_network(self):
        self.envoy.request_queue.put({
            'id': 102,
            'operation': 'query_network',
            'params': {'parent_path': '/'}
        })
        self.envoy._onRefresh()

        resp = self.envoy.response_queue.get_nowait()
        self.assertEqual(resp['id'], 102)
        self.assertNotIn('error', resp['result'])

    def test_round_trip_error_propagated(self):
        """Error from handler propagates through the queue cleanly."""
        self.envoy.request_queue.put({
            'id': 103,
            'operation': 'get_op',
            'params': {'op_path': '/this_does_not_exist_rt'}
        })
        self.envoy._onRefresh()

        resp = self.envoy.response_queue.get_nowait()
        self.assertEqual(resp['id'], 103)
        self.assertDictHasKey(resp['result'], 'error')

    def test_round_trip_multiple_interleaved(self):
        """Multiple requests produce correctly-matched responses."""
        comp = self.sandbox.create(baseCOMP, 'interleave_test')

        self.envoy.request_queue.put({
            'id': 200, 'operation': 'get_td_info', 'params': {}
        })
        self.envoy.request_queue.put({
            'id': 201, 'operation': 'get_op',
            'params': {'op_path': comp.path}
        })
        self.envoy.request_queue.put({
            'id': 202, 'operation': 'get_op',
            'params': {'op_path': '/nonexistent_interleave'}
        })

        self.envoy._onRefresh()

        responses = {}
        while not self.envoy.response_queue.empty():
            r = self.envoy.response_queue.get_nowait()
            responses[r['id']] = r['result']

        # 200: td_info success
        self.assertDictHasKey(responses[200], 'version')
        # 201: get_op success
        self.assertEqual(responses[201]['name'], 'interleave_test')
        # 202: get_op error
        self.assertDictHasKey(responses[202], 'error')
