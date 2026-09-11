# MCP Server Safety

## Thread Boundary

The Envoy MCP server runs in a background thread. TouchDesigner objects (OPs, COMPs, parameters) must NEVER be accessed from background threads. All TD operations go through the main thread automatically via Envoy's MCP tools.

## Localhost Only

The MCP server binds to `127.0.0.1` only. It is not accessible from the network.

## Operation Timeout

MCP operations time out at 30 seconds. If an operation needs longer, break it into smaller steps.

## Envoy's Own Host

Envoy runs inside the Embody COMP. An Envoy call must never destroy or reload the Embody COMP, an ancestor of it, `/`, or Envoy's extension DAT synchronously -- that hung TouchDesigner (issue #110). Envoy refuses it (`envoy.embody.host_destroy_refused`). Stop and ask the user; if they asked to move or remove Embody, follow the refusal text and defer the whole sequence as one `run()` string.
