# Troubleshooting

## Verbose (Debug) Logging

For verbose path logging and troubleshooting, turn on the **Verbose** parameter on the Embody COMP (or from code, `op.Embody.par.Verbose = True`). This unblocks all `DEBUG`-level messages — including detailed externalization path resolution — which are otherwise suppressed. With Verbose on they flow to the log FIFO, to the log file (in your Log Folder, `logs` by default), and to the textport when **Print** is enabled.

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
3. Try a manual update with ++ctrl+shift+u++
4. Check the textport/logs for error messages

### Content Missing After a Save or Reopen (TDXN)

If something inside a TDXN-managed COMP comes back empty after a save or a reopen:

1. **A DAT is empty**: unexternalized, editable DAT content is always written into the `.tdxn`. A DAT that comes back empty is either generated (TouchDesigner regenerates its content on cook, so it is written as `dat_read_only`) or tagged `tdxn_exclude:dat_content`. Check the log for a **WARNING** naming it, such as a skipped duplicate companion.
2. **Storage is missing**: turn on **Embed Storage** (`Embedstorageintdxns`), or the COMP's **Embed storage in tdxn** toggle in the tagging menu. In Roundtrip mode the save logs a **WARNING** when it will lose storage.
3. **Check the Content Safety parameter**: when set to *Never Warn* (`ignore`), a save logs nothing about content the `.tdxn` cannot hold. Use *Warn Each Save* (`ask`) to see it.

See [Content Safety](externalization.md#content-safety-save-time-check) for details on how the safety check works.

### A Save Logged "Was Not Answered" or "Content Check Failed"

A save never shows a dialog: TouchDesigner has the `.toe` open for writing, and a modal then can freeze the save. A prompt that would have appeared takes its safe default instead, and the save logs what it did -- a **WARNING** in the log, also listed in `get_job_status(job_id)["warnings"]` for a `save_project` job:

- **"Palette handling was not answered for N clone(s) during this save"**: those palette components were exported as black boxes (a reference only), so any changes made inside them are not in the `.tdxn`. Set **Palette Handling** (`Tdxnpalettehandling`), or choose per COMP, to decide.
- **"Duplicate Paths Detected was not answered"**: operators sharing one externalized file path were auto-resolved -- the first stays master, the rest are tagged as clones. Review them in the Embody manager.
- **"Tdxndatsafety = 'externalize': dialogs stayed suppressed"**: the deferred filing of unexternalized DATs gave up because a save or test run kept dialogs suppressed. Nothing is lost -- the content stays in the `.tdxn` -- and the next save retries.
- **"TDXN content check failed (export continues)"** (an **ERROR**): the save-time content report hit an internal error. The export and strip still ran; please report the log line.

### Cross-Platform Issues

Embody normalizes all paths to forward slashes (`/`). If you're collaborating across Windows and macOS and encountering path issues, ensure you're using a recent version of Embody with cross-platform path handling.
