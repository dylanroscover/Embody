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

### Cross-Platform Issues

Embody normalizes all paths to forward slashes (`/`). If you're collaborating across Windows and macOS and encountering path issues, ensure you're using a recent version of Embody with cross-platform path handling.
