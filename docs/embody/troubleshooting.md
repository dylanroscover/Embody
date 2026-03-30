# Troubleshooting

## Debug Mode

For verbose path logging and troubleshooting, enable debug mode by setting `debug_mode = True` in the EmbodyExt extension. This will log detailed path information to the textport.

## Common Issues

### Timeline Paused

Embody requires the timeline to be running. A warning will appear in the textport if the timeline is paused. Resume the timeline to restore normal operation.

### Clone/Replicant Operators

Clones and replicants cannot be externalized. Embody will show a warning if you try to tag them. This is by design — these operators are managed by TouchDesigner's clone system.

### Engine COMPs

Engine, time, and annotate COMPs are not supported for externalization.

### File Path Conflicts

If you see warnings about duplicate file paths, this means two operators are pointing to the same external file. See [Duplicate Path Handling](externalization.md#duplicate-path-handling) for resolution options.

### Externalization Not Updating

If externalized files aren't updating on save:

1. Check that the operator is tagged (look for the tag indicator)
2. Verify the operator is marked as dirty
3. Try a manual save with ++ctrl+shift+u++
4. Check the textport/logs for error messages

### DAT Content Lost After Save (TDN)

If DATs inside TDN-managed COMPs lose their content after saving:

1. **Enable Embed DATs in TDNs**: This stores DAT content directly in the `.tdn` file, preserving it through the strip/restore cycle.
2. **Externalize the DATs**: Tag them with an Embody DAT tag so their content is saved to files on disk.
3. **Check the DAT Safety parameter**: If set to *Never Ask*, Embody won't warn you about at-risk DATs. Change it to *Ask Each Save* or *Always Externalize* to catch unprotected DATs before they lose content.

See [DAT Content Safety](externalization.md#dat-content-safety) for details on how the safety check works.

### Cross-Platform Issues

Embody normalizes all paths to forward slashes (`/`). If you're collaborating across Windows and macOS and encountering path issues, ensure you're using a recent version of Embody with cross-platform path handling.
