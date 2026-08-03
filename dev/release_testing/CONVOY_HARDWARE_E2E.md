# Convoy hardware E2E harness

`convoy_hardware_e2e.py` is the repeatable acceptance harness for a real
Convoy spanning two computers. The first target is a local Windows workstation
relaying to TEC-B4A over the LAN. The same probe works with a macOS OpenSSH
target when one is reachable.

The default `smoke` run is nondestructive. It discovers both HostApps, verifies
the realm, Embody versions, mutual peer admission, persistent peer session, and
node directory, then sends a real Convoy ping and a read-only Envoy operation.
It does not change a parameter, write a project file, cancel work, or restart a
process.

## Prerequisites

- The intended Embody build is installed on both computers.
- TouchDesigner is running with Convoy enabled on both target nodes.
- Both nodes have converged on the same Convoy and both hosts have admitted one
  another.
- The local computer has an OpenSSH client. The remote computer has an
  OpenSSH server and Python 3 available as `python`, `python3`, or `py -3`.
- Non-interactive SSH authentication is already configured with an SSH agent,
  normal OpenSSH config, or an optional identity-file path. Do not put key
  contents, passwords, passphrases, HostApp tokens, or other credentials in the
  harness JSON.
- The remote SSH host key has been verified from a trusted, out-of-band source
  and is already present in the selected `known_hosts` file.

The harness discovers each HostApp from its per-user portfile and authenticates
to its loopback API. On the remote computer, that discovery and all HostApp
requests execute inside the SSH session. The remote HostApp token never leaves
that computer. The local token stays in memory and is recursively redacted from
results.

## SSH trust is a hard gate

Every SSH invocation uses:

- `BatchMode=yes`
- `StrictHostKeyChecking=yes`
- the configured `UserKnownHostsFile`, when supplied

There is no `accept-new`, `StrictHostKeyChecking=no`, temporary permissive
known-hosts file, or retry that bypasses an unknown/changed key. An SSH host-key
failure is a test failure. Compare the fingerprint at the remote machine's
trusted console or against trusted inventory, investigate any change, and
update `known_hosts` yourself only after that verification.

For TEC-B4A, the current development machine has not yet trusted its presented
keys. A hardware run must remain blocked until the operator confirms the chosen
fingerprint out of band. The harness will not answer the trust prompt for you.

## Configure a run

Copy `convoy_hardware_e2e.example.json` to an operator-controlled location and
fill in the real non-secret values. Do not commit the completed configuration.

Important fields:

| Field | Purpose |
| --- | --- |
| `schema_version` | Must be `1`. |
| `convoy_id` | Exact shared Convoy ID expected on both hosts. |
| `expected_embody_version` | Optional exact release expected on both selected nodes. Nodes must match each other even when this is omitted. |
| `local.data_dir` | Optional HostApp data-directory override. `null` uses the platform default. |
| `local.node_id` | Preferred local node for version comparison. If omitted, the first local node in the Convoy is used. |
| `remote.ssh_host`, `ssh_port`, `ssh_user` | OpenSSH destination. |
| `remote.known_hosts_file` | Existing verified known-hosts file. A relative path is resolved beside the JSON config. |
| `remote.identity_file` | Optional path to an existing private key. The key itself never belongs in JSON. `null` uses SSH agent/config behavior. |
| `remote.python_command` | Safe command name such as `python`, `python3`, or `py -3`. |
| `remote.target_node_id` | Exact stable node ID that receives relay calls. |
| `remote.data_dir` | Optional remote HostApp data-directory override. |
| `timeouts` | Bounded HTTP, SSH, durable-job, reconnect, and poll intervals. |
| `scenarios` | Optional test actions and their project-specific arguments. Disabled scenarios report a skip when requested. |

`known_hosts_file` and `identity_file`, when set, must already exist. The loader
rejects fields whose names look like embedded credentials, unsafe SSH host
shapes, control characters, non-finite numbers, and unknown scenarios.

## Run the safe smoke test

From the repository root:

```powershell
python dev/release_testing/convoy_hardware_e2e.py `
  --config C:/secure/convoy-hardware-e2e.json
```

Smoke always runs the preflight plus:

1. `convoy_ping` through local `POST /relay`, durable remote polling, and
   explicit remote outcome acknowledgement.
2. The configured read operation (default: non-recursive `query_network` at
   `/`) through the same real relay/poll/ack path.

Selecting a scenario and authorizing it are separate decisions. Merely passing
an allow flag does not add scenarios:

```powershell
# Mutation + idempotent retry only.
python dev/release_testing/convoy_hardware_e2e.py `
  --config C:/secure/convoy-hardware-e2e.json `
  --scenario mutation --allow-mutation

# Verified screenshot/file transfer and explicit project export.
python dev/release_testing/convoy_hardware_e2e.py `
  --config C:/secure/convoy-hardware-e2e.json `
  --scenario artifact --allow-mutation

# Every configured scenario during a maintenance window.
python dev/release_testing/convoy_hardware_e2e.py `
  --config C:/secure/convoy-hardware-e2e.json `
  --scenario all --allow-mutation --allow-disruption
```

Use repeated `--scenario` arguments to choose a smaller combination.

## Scenario safety and prerequisites

| Scenario | Gate | What it proves |
| --- | --- | --- |
| `ping` | none | Real signed relay, durable completion, poll, and ACK. |
| `read` | none | A real read-only Envoy call reaches the selected remote TD node. |
| `mutation` | `--allow-mutation` | A configured mutation succeeds; retrying the same idempotency key resolves to the same delivery. Configure a cleanup operation when the mutation needs restoration. |
| `artifact` | `--allow-mutation` | A configured artifact-producing read succeeds, the local HostApp materializes it, raw bytes match size/SHA-256, and the verified artifact exports into a registered local project. |
| `cancel` | `--allow-mutation` | A configured reliably cancellable job accepts cancellation, reaches the configured terminal state, and is acknowledged. |
| `dropout` | both allow flags | A preinstalled, self-reverting remote command interrupts the peer session, and both hosts reconnect. |
| `host_restart` | both allow flags | A preinstalled command restarts the remote HostApp while stable host identity survives and the peer session reconnects. |
| `td_restart` | both allow flags | Convoy performs an exact-runtime `require_clean` restart; the node returns with a new runtime ID and the peer session remains healthy. |

Do not enable a mutation until its operation and arguments point at dedicated
test content. `mutation.cleanup`, when supplied, is itself a real remote
mutation. If cleanup fails it is reported; the harness never guesses how to
restore project state.

The artifact scenario writes one uniquely named file under the configured
registered project's `.embody/convoy/artifacts/`. `overwrite` remains false.
The harness reports the saved path and leaves the verified test artifact for
the operator to inspect or remove.

Cancellation needs a job that will remain cancellable long enough for the
request to arrive. Configure a purpose-built test operation; do not point it at
production work.

Dropout's `self_reverting_command` must restore connectivity without a second
SSH call, because the deliberate outage may also cut off SSH. Keep disruptive
commands in reviewed scripts on the target and place only the script invocation
in JSON. `host_restart.command` follows the same principle. Command stdout and
stderr are bounded and redacted in reports, but commands must not print secrets.

## Results and skips

Each run atomically writes JSON and JUnit XML. By default the files go to:

```text
dev/release_testing/results/convoy-hardware-<run-id>.json
dev/release_testing/results/convoy-hardware-<run-id>.junit.xml
```

Use `--json-out` and `--junit-out` to choose CI artifact paths. Reports contain
case names, duration, pass/fail/skip/error state, sanitized diagnostics, selected
flags, and a summary. They never contain raw artifact bytes or HostApp tokens.

A scenario is **skipped**, not passed, when it was requested but its explicit
allow flag or optional configuration is absent. Missing/mismatched nodes,
realms, versions, peer admission, sessions, unexpected job outcomes, and
unknown/changed SSH keys are failures. macOS remains untested until real
hardware is reachable; Windows success must not be presented as macOS proof.

## macOS target

Use a separate config with the Mac's verified SSH host, user, Python command,
node ID, and paths. The remote probe chooses the macOS HostApp data directory
automatically and uses only Python's standard library. Disruptive command
strings are deployment-specific, so supply reviewed macOS commands/scripts
instead of copying Windows service commands.
