"""Phase 1 exit criterion, driven exactly as a supervisor would."""
import json, os, subprocess, sys, tempfile, time, urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import convoy_platform as cp

STATE = tempfile.mkdtemp(prefix='convoy_exit_')
HERE = os.path.dirname(os.path.abspath(__file__))

def start():
    p = subprocess.Popen([sys.executable, os.path.join(HERE, 'convoy_hostapp.py'),
                          '--data-dir', STATE],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    for _ in range(60):
        info = cp.read_portfile(STATE)
        if info and info.get('pid') == p.pid:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{info['port']}/health", timeout=2)
                return p, info['port']
            except Exception: pass
        time.sleep(0.25)
    raise SystemExit('host app never came up: ' + p.stderr.read())

def call(port, path, body=None):
    token = open(os.path.join(STATE, cp.TOKEN_FILE)).read().strip()
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(f'http://127.0.0.1:{port}{path}', data=data,
        headers={'Content-Type': 'application/json', 'X-Convoy-Host-Token': token})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())

print('=== boot #1 (as a supervisor would) ===')
proc, port = start()
print(f'  host app pid={proc.pid} port={port}')
print('  status:', json.dumps(call(port, '/status')))

print('\n=== two fake nodes register ===')
a = call(port, '/register', {'project_root': '/Work/anchor-A', 'convoy_id': 'studio', 'comp_path': '/projA/Embody'})
b = call(port, '/register', {'project_root': '/Work/anchor-B', 'convoy_id': 'studio', 'comp_path': '/projB/Embody'})
print(f"  node A: {a['node_id'][:16]}...  approved={a['td_python_approved']}")
print(f"  node B: {b['node_id'][:16]}...  approved={b['td_python_approved']}")
print(f"  distinct: {a['node_id'] != b['node_id']} | same host: {a['host_id'] == b['host_id']}")

print('\n=== durable job ===')
j = call(port, '/jobs', {'idempotency_key': 'exit-proof', 'node_id': a['node_id'],
                         'operation': 'query_network', 'arguments': {'parent_path': '/'}})
delivery_id = j['job']['delivery_id']
print(f"  created {delivery_id} state={j['job']['state']}")

print('\n=== KILL the host app (supervisor restart) ===')
proc.terminate(); proc.wait(timeout=15)
print(f'  exited rc={proc.returncode}; portfile now: {cp.read_portfile(STATE)}')

print('\n=== boot #2 on the same data dir ===')
proc2, port2 = start()
print(f'  host app pid={proc2.pid} port={port2} (new port, found via portfile)')
st = call(port2, '/status')
print('  status:', json.dumps(st))
recovered = call(port2, f'/jobs/{delivery_id}')
print(f"  job {delivery_id} SURVIVED: state={recovered['job']['state']} key={recovered['job']['idempotency_key']}")
again = call(port2, '/register', {'project_root': '/Work/anchor-A', 'convoy_id': 'studio', 'comp_path': '/projA/Embody'})
print(f"  node A re-registered to the SAME id: {again['node_id'] == a['node_id']}")
retry = call(port2, '/jobs', {'idempotency_key': 'exit-proof', 'node_id': a['node_id'], 'operation': 'query_network'})
print(f"  idempotency held across restart: created={retry['created']} same_job={retry['job']['delivery_id'] == delivery_id}")
proc2.terminate(); proc2.wait(timeout=15)
print('\nPHASE 1 EXIT (local slice): two nodes registered, jobs survived a real process restart.')
