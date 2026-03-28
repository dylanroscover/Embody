# Configuration

## Embody Parameters

Embody is configured through parameters on the Embody COMP itself. Key parameters include:

### Embody

- **Externalizations Folder** — The externalization folder path (static or expression mode)
- **Disable** — Disable Embody (stops all externalization tracking)
- **Enable/Update** — Initialize or update all externalizations
- **Externalize Full Project** — Pulse to externalize every eligible operator in the project
- **Detect Duplicate Paths** — Enable/disable duplicate path detection prompts
- **Open Manager** / **Close Manager** — Toggle the Manager UI

### Envoy

- **Envoy Enable** — Toggle the MCP server on/off
- **Envoy Port** — Port number for the MCP server (default: 9870)

### Restoration

- **TOX Restore on Start** — Restore missing TOX-strategy COMPs from `.tox` files on project open (ON by default)
- **TDN Create on Start** — Reconstruct TDN-strategy COMPs from `.tdn` files on project open

### TDN

- **Embed DATs in TDNs** — Include DAT content in TDN exports
- **DAT Safety** — What to do when TDN COMPs contain DATs with unprotected content: *Ask Each Save* (default) prompts before each save, *Always Externalize* auto-externalizes without asking, *Never Ask* suppresses the check
- **Export Project TDN** — Pulse to export the entire project network

### Logs

- **Verbose (Debug)** — Enable debug-level logging
- **Print to Textport** — Echo logs to the textport
- **Log to File** — Enabled by default, writes to `logs/<project_name>_YYMMDD.log`

## Logging System

Embody provides a multi-destination logging system:

- **File logging** (default): Logs are written to `logs/<project_name>_YYMMDD.log`. Files auto-rotate at 10 MB with numbered suffixes (`_001`, `_002`, etc.).
- **FIFO DAT**: Recent log entries are visible in TouchDesigner's network editor.
- **Textport**: Enable the **Print to Textport** parameter to echo logs to the textport.
- **Ring buffer**: The most recent 200 entries are accessible via the Envoy `get_logs` MCP tool.

### Log Levels

`DEBUG`, `INFO`, `WARNING`, `ERROR`, `SUCCESS`

### Using the Logger

From anywhere in your project:

```python
op.Embody.Log('Something happened', 'INFO')
op.Embody.Debug('Debug message')
op.Embody.Info('Informational message')
op.Embody.Warn('Warning message')
op.Embody.Error('Error message')
```
