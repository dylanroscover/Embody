# Security Considerations

Envoy is designed as a **local development tool** — it runs on `localhost` and is intended for use in trusted environments. Understanding its security model is important before deploying it in any shared or networked context.

## Local-Only by Design

Envoy binds exclusively to `127.0.0.1` (localhost). It does **not** listen on `0.0.0.0` or any external interface. This means:

- Only processes running on the same machine can connect
- It is not accessible from the local network or internet
- DNS rebinding attacks from malicious websites cannot reach it

!!! danger "Do not expose Envoy to the network"
    Never use port forwarding, SSH tunnels, reverse proxies, or firewall rules to make Envoy's port accessible from other machines. Envoy has **no authentication** — any process that can reach the port has full control over your TouchDesigner session.

## No Authentication

Envoy does not use API keys, tokens, or any form of authentication. Any local process that can connect to the configured port (default `9870`) can:

- Read and modify any operator, parameter, or DAT content
- Execute arbitrary Python code via `execute_python`
- Call any method on any operator via `exec_op_method`
- Create, delete, rename, and connect operators
- Export and import entire networks

This is acceptable for single-user development workstations. On **shared machines** (multi-user servers, CI/CD runners, cloud desktops), other users' processes could potentially connect to your Envoy instance.

## Arbitrary Code Execution

The `execute_python` tool runs arbitrary Python code on TouchDesigner's main thread with full access to the TD environment and Python standard library. This includes:

- File system access (`open()`, `os`, `pathlib`, `shutil`)
- Network access (`requests`, `urllib`, `socket`)
- Process execution (`subprocess`, `os.system()`)
- All TouchDesigner APIs (`op()`, `ui`, `project`)

The `exec_op_method` tool can call any callable attribute on any operator, which also provides broad access.

!!! warning "MCP clients have full system access"
    Any MCP client connected to Envoy (Claude Code, Cursor, etc.) can execute arbitrary code through these tools. Only connect MCP clients you trust, and review operations in their logs if you have concerns.

## Recommendations

| Environment | Risk Level | Guidance |
|-------------|-----------|----------|
| Personal workstation | Low | Default configuration is appropriate |
| Shared workstation | Medium | Ensure other users cannot access your port; consider using a non-default port |
| Cloud/remote desktop | Medium–High | Verify no port forwarding exposes the Envoy port; avoid running on shared instances |
| Production/public servers | Not supported | Envoy is a development tool — do not run it in production environments |

## Logging and Auditing

All MCP operations are logged to Embody's ring buffer (200 entries) and to `dev/logs/` on disk. The `execute_python` tool logs a preview of each code snippet before execution. Use `get_logs` or review the log files to audit what operations have been performed.
