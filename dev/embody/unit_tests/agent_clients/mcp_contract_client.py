"""Tier-1 MCP contract client for Embody's agent-test tier.

Out-of-process, stdlib-only MCP client. It spawns the SAME Envoy STDIO bridge
command Claude Code uses (read verbatim from the repo's .mcp.json), performs
the MCP handshake, checks the advertised tool inventory against an expected
manifest, and exercises a curated tool-call sequence against a sandbox COMP.
Writes a JSON report and exits 0 only when every stage succeeded.

Run by test_agent_contract.py via the AGENT tier runner; also runnable by hand:

    python mcp_contract_client.py --mcp-json <repo>/.mcp.json \
        --sandbox /embody/unit_tests/test_sandbox/sandbox_x \
        --expected-tools expected.json --report report.json

No TD imports, no third-party deps: this file runs OUTSIDE TouchDesigner in
whatever Python spawns it (normally the project venv python from .mcp.json).
"""

import argparse
import json
import os
import queue
import subprocess
import sys
import threading
import time


class BridgeClient:
    """Newline-delimited JSON-RPC 2.0 client over the Envoy STDIO bridge."""

    def __init__(self, command, args, label):
        env = dict(os.environ)
        env['EMBODY_SESSION_LABEL'] = label
        kwargs = {}
        if sys.platform == 'win32':
            kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW
        self.proc = subprocess.Popen(
            [command] + list(args),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
            **kwargs,
        )
        self._next_id = 0
        self._lines = queue.Queue()
        self.notifications = []
        # Reader thread: readline() blocks, so requests pull from a queue
        # with a timeout instead of blocking the client forever.
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self):
        try:
            for raw in self.proc.stdout:
                self._lines.put(raw)
        except Exception:
            pass
        self._lines.put(None)  # EOF sentinel

    def _send(self, payload):
        data = (json.dumps(payload) + '\n').encode('utf-8')
        self.proc.stdin.write(data)
        self.proc.stdin.flush()

    def request(self, method, params=None, timeout=40.0):
        """Send a request and wait for ITS response (id-matched).

        The bridge may interleave notifications (tools/list_changed etc.);
        those are collected, never returned."""
        self._next_id += 1
        req_id = self._next_id
        payload = {'jsonrpc': '2.0', 'id': req_id, 'method': method}
        if params is not None:
            payload['params'] = params
        self._send(payload)
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f'{method}: no response within {timeout}s')
            try:
                raw = self._lines.get(timeout=min(remaining, 1.0))
            except queue.Empty:
                continue
            if raw is None:
                raise ConnectionError(f'{method}: bridge closed stdout (EOF)')
            try:
                msg = json.loads(raw.decode('utf-8', errors='replace'))
            except Exception:
                continue  # not JSON (defensive; the bridge is line-JSON only)
            if msg.get('id') == req_id:
                if 'error' in msg:
                    raise RuntimeError(f"{method}: JSON-RPC error {msg['error']}")
                return msg.get('result')
            if 'id' not in msg:
                self.notifications.append(msg)
            # A response to a different id is dropped (we are strictly serial).

    def notify(self, method, params=None):
        payload = {'jsonrpc': '2.0', 'method': method}
        if params is not None:
            payload['params'] = params
        self._send(payload)

    def close(self):
        """Close stdin (the bridge's orphan watchdog exits on it), then reap."""
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=10)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass


def tool_text(result):
    """Extract the text payload from an MCP tools/call result."""
    for item in (result or {}).get('content', []):
        if item.get('type') == 'text':
            return item.get('text', '')
    return ''


def tool_payload(result):
    """Parse a tool result's text as JSON when possible ({} when not)."""
    text = tool_text(result)
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else {'value': parsed}
    except Exception:
        return {'raw': text}


class ContractRun:
    def __init__(self, args):
        self.args = args
        self.report = {
            'handshake': None,
            'server_info': None,
            'tool_count': 0,
            'missing_tools': [],
            'unexpected_tools': [],
            'calls': [],
            'roundtrip_ok': None,
            'ok': False,
            'failures': [],
        }

    def fail(self, msg):
        self.report['failures'].append(msg)

    def call(self, client, tool, arguments, required=True, gate_ok=False):
        """tools/call wrapper recording {tool, ok, ms, error, gated}."""
        entry = {'tool': tool, 'ok': False, 'ms': None, 'error': None,
                 'gated': False}
        t0 = time.monotonic()
        try:
            result = client.request(
                'tools/call', {'name': tool, 'arguments': arguments},
                timeout=self.args.call_timeout)
            entry['ms'] = round((time.monotonic() - t0) * 1000, 1)
            payload = tool_payload(result)
            err = payload.get('error')
            # batch_operations reports inner failures as success:False with
            # NO top-level error key - treat that as failure too.
            batch_failed = payload.get('success') is False
            if result.get('isError') or err or batch_failed:
                err_text = str(err) if err else (
                    json.dumps(payload.get('results', payload))[:400]
                    if batch_failed else str(tool_text(result)))
                if gate_ok and 'MULTI-SESSION GATE' in err_text:
                    # A live peer wrote this scope within the last minute: the
                    # tool AND the gate machinery both work. Operational.
                    entry['ok'] = True
                    entry['gated'] = True
                else:
                    entry['error'] = err_text[:500]
                    if required:
                        self.fail(f'{tool}: {err_text[:200]}')
            else:
                entry['ok'] = True
            self.report['calls'].append(entry)
            return payload if entry['ok'] else None
        except Exception as e:
            entry['ms'] = round((time.monotonic() - t0) * 1000, 1)
            entry['error'] = f'{type(e).__name__}: {e}'[:500]
            self.report['calls'].append(entry)
            if required:
                self.fail(f'{tool}: {entry["error"][:200]}')
            return None

    def run(self):
        args = self.args
        with open(args.mcp_json, encoding='utf-8') as f:
            entry = json.load(f)['mcpServers'][args.server]
        if entry.get('type') != 'stdio':
            self.fail(f'.mcp.json {args.server} entry is not stdio')
            return
        expected = None
        if args.expected_tools:
            with open(args.expected_tools, encoding='utf-8') as f:
                expected = set(json.load(f))

        client = BridgeClient(entry['command'], entry.get('args', []),
                              args.label)
        try:
            init = client.request('initialize', {
                'protocolVersion': '2024-11-05',
                'clientInfo': {'name': 'embody-agent-test', 'version': '1.0'},
                'capabilities': {},
            }, timeout=30)
            self.report['handshake'] = True
            self.report['server_info'] = (init or {}).get('serverInfo')
            client.notify('notifications/initialized')

            tools = client.request('tools/list', {}, timeout=30)
            names = {t['name'] for t in (tools or {}).get('tools', [])}
            self.report['tool_count'] = len(names)
            if expected is not None:
                self.report['missing_tools'] = sorted(expected - names)
                self.report['unexpected_tools'] = sorted(names - expected)
                if self.report['missing_tools']:
                    self.fail(f"missing tools: {self.report['missing_tools']}")
                if self.report['unexpected_tools']:
                    self.fail(
                        f"unexpected tools (update EXPECTED_* in "
                        f"test_agent_contract.py deliberately): "
                        f"{self.report['unexpected_tools']}")

            # ---- curated call sequence -----------------------------------
            status = self.call(client, 'get_td_status', {})
            if status is not None and not status.get('connected'):
                self.fail('get_td_status reports Envoy not connected')
            self.call(client, 'get_td_info', {})
            self.call(client, 'query_network', {'parent_path': '/'})

            sandbox = args.sandbox
            probe = f'{sandbox}/tier1_probe'
            token = args.token
            self.call(client, 'create_op', {
                'parent_path': sandbox, 'op_type': 'textDAT',
                'name': 'tier1_probe'})
            self.call(client, 'set_dat_content', {
                'op_path': probe, 'text': token})
            got = self.call(client, 'get_dat_content', {'op_path': probe})
            # Key names vary by tool version - search the whole payload.
            content = json.dumps(got) if got is not None else ''
            self.report['roundtrip_ok'] = token in content
            if not self.report['roundtrip_ok']:
                self.fail(f'roundtrip: token not in read-back content '
                          f'({content[:120]!r})')
            self.call(client, 'get_op_errors', {
                'op_path': sandbox, 'recurse': True})
            self.call(client, 'batch_operations', {'operations': [
                {'tool': 'set_op_position',
                 'params': {'op_path': probe, 'x': 400, 'y': -200}},
                {'tool': 'set_op_position',
                 'params': {'op_path': probe, 'x': 800, 'y': -200}},
            ]})
            self.call(client, 'get_logs', {'count': 5})
            # delete_op LAST: a MULTI-SESSION GATE refusal still counts as
            # operational (gate_ok) - the suite's sandbox teardown cleans up.
            self.call(client, 'delete_op', {'op_path': probe}, gate_ok=True)
        except Exception as e:
            self.report['handshake'] = bool(self.report['handshake'])
            self.fail(f'{type(e).__name__}: {e}')
        finally:
            client.close()

        self.report['ok'] = not self.report['failures']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mcp-json', required=True)
    parser.add_argument('--server', default='envoy')
    parser.add_argument('--sandbox', required=True,
                        help='op path of the sandbox COMP to build probes in')
    parser.add_argument('--expected-tools', default=None,
                        help='JSON file: list of expected tool names')
    parser.add_argument('--report', required=True)
    parser.add_argument('--token', default=f'tier1-{os.getpid()}-{int(time.time())}')
    parser.add_argument('--label', default='agent-test-tier1')
    parser.add_argument('--call-timeout', type=float, default=40.0,
                        help='per-call timeout; > Envoy 30s op timeout')
    args = parser.parse_args()

    run = ContractRun(args)
    run.run()
    with open(args.report, 'w', encoding='utf-8') as f:
        json.dump(run.report, f, indent=2)
    # Human-readable one-liner for the runner's stdout capture.
    print(json.dumps({'ok': run.report['ok'],
                      'failures': run.report['failures'][:5]}))
    sys.exit(0 if run.report['ok'] else 1)


if __name__ == '__main__':
    main()
