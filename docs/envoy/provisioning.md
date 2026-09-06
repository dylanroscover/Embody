# Provisioning a bare machine

A render node, a media server, the booth PC: a machine that has TouchDesigner installed and nothing else, no human at the keyboard, and a show file that needs Embody in it. The **bootstrap** installs Embody into that `.toe` offline, using TouchDesigner's own `toeexpand` and `toecollapse`, and leaves nothing on the machine except the Embody COMP inside the project. No resident app, no service, no second runtime.

It is the unattended cousin of the [Textport paste](../quickstart.md): same release, same checksum, no TouchDesigner window required.

## One line, run on the machine

=== "Windows (PowerShell)"

    ```powershell
    $td = (Get-ChildItem 'C:\Program Files\Derivative\TouchDesigner.*' | Sort-Object Name -Descending | Select-Object -First 1).FullName
    Invoke-WebRequest https://embody.tools/bootstrap.py -OutFile "$env:TEMP\embody_bootstrap.py"
    & "$td\bin\python.exe" "$env:TEMP\embody_bootstrap.py" "C:\shows\show.toe" --launch
    ```

=== "macOS"

    ```bash
    curl -fsSL https://embody.tools/bootstrap.py -o /tmp/embody_bootstrap.py
    python3 /tmp/embody_bootstrap.py ~/shows/show.toe --launch
    ```

    The script is stdlib-only, so any Python 3.9+ runs it; it finds TouchDesigner's tools inside the `.app` bundle itself. The macOS path is unverified at the time of writing (no Mac in the loop); please report what you see.

Over SSH, PowerShell Remoting, or an Owlette job, that is the whole provisioning step. The script lives in the repo at `dev/embody/embody_bootstrap.py`; `https://embody.tools/bootstrap.py` serves the same bytes.

## What it does

1. **Finds TouchDesigner.** The newest install under `C:\Program Files\Derivative` (or `/Applications`), unless `--td <install dir>` names one. It needs `toeexpand` and `toecollapse` from that install's `bin`, and the `TouchDesigner` executable for `--launch`.
2. **Fetches the latest release.** The same manifest the Textport paste reads, the same sha256 check. `--tox <file>` uses a local release tox instead, for air-gapped machines.
3. **Grafts Embody into the project.** The `.toe` is expanded beside a staging copy, the release tox is expanded, and the Embody COMP's files and `.toc` entries are added at the project root. An Embody already at the root is refused unless `--replace` is passed, in which case the old one is removed first.
4. **Adds first-run provisioning.** A freshly installed Embody opens its setup wizard and keeps Envoy disabled until someone clicks. The bootstrap adds a root-level Execute DAT, `embody_provision`, whose `onStart` waits for Embody, closes the wizard, applies the wizard's own backend with Auto mode and the assistant you chose (`--assistant`, default `claudecode`; `none` leaves Envoy disabled), saves the project, and destroys itself. Every later open is configured and quiet. `--no-provision` skips this and lets the wizard show.
5. **Collapses and verifies.** `toecollapse` rebuilds the `.toe`; the result is re-expanded and every grafted entry must be present, or nothing is touched. Success is judged by filesystem evidence, never by the tools' exit codes (`toeexpand` returns 1 on success).
6. **Replaces the original.** The original is kept beside it as `<name>.toe.bak-<timestamp>`; the new file takes its place atomically.
7. **Launches, if asked.** `--launch` starts TouchDesigner on the project, detached. On the first open the provisioning runs, Envoy comes up, and the instance registers itself in `.embody/envoy.json` under the project folder, so a [Convoy](../convoy/index.md) controller or an AI session can reach it.

`--json` prints a machine-readable summary on stdout (progress goes to stderr), `--keep-staging` leaves the staging directory for inspection, and every failure carries an `embody.bootstrap.*` error code.

## What it does not do

- It does not install Python packages, services, or anything outside the project folder. Envoy's own virtualenv is created by Embody on first run, exactly as it is after a Textport install.
- It does not make a git repository. First-run provisioning chooses *project folder* as the root and skips git; add a repository later if you want diffs.
- It does not talk to a running TouchDesigner. If the show file is open, close it first; the bootstrap edits the file on disk.

## Notes on the expanded format

`toeexpand` writes a `<file>.dir` tree and a `<file>.toc` index of root-relative entries (LF-only, no BOM, forward slashes). A COMP is `<name>.n`, `<name>.parm`, `<name>.cparm`, `<name>.panel` plus a `<name>/` subtree; a DAT's body is a `.text` file framed as a version line, a `*`, five big-endian integers, a byte length, and the text. The bootstrap only ever appends entries and copies files into that tree, then lets `toecollapse` do the packing. Idea adopted from td-mcp-rs's `project_install_bridge`, credited in the README; the mechanics were re-derived against 2025.33070.
