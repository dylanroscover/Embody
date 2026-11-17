# Embody starter project

`Embody-starter.toe` is an empty TouchDesigner project that installs the latest Embody into itself the first time it opens. It is the file to start a brand-new project from.

## Use it

1. Download [`Embody-starter.toe`](https://github.com/dylanroscover/Embody/raw/main/starter/Embody-starter.toe) into a new, empty folder and rename it to your project's name.
2. Open it in TouchDesigner 2025.33230 or later.
3. Embody appears in `/project1` and its setup wizard opens. Save the project to keep it.

Copy it out of this repository before opening it. Opened in place, Embody would set itself up inside the Embody source repo.

## What the first open does

- It downloads the latest release `.tox` from GitHub Releases, checks its sha256 against the release manifest, and loads it into `/project1`. The download runs on a background thread, so TouchDesigner stays responsive; Embody appears a few seconds after the window does. This is the [Quickstart](https://dylanroscover.github.io/Embody/quickstart/) Textport paste, run for you.
- With no connection, or if the download fails its checksum, a file dialog asks for an Embody release `.tox` you already have. Cancel and nothing is installed; the next open asks again.
- The install script, `/project1/embody_install`, then removes itself. Until you save, the file on disk is still the bare starter, so reopening it installs again.

## A machine with no one at it

The starter still opens the setup wizard, which waits for a person. For a render node or a media server, run the [bootstrap](https://dylanroscover.github.io/Embody/envoy/provisioning/) on a copy of the starter instead: it installs Embody offline and answers the wizard. The starter then finds Embody already in the project and installs nothing.

## What is in this folder

| File | What it is |
|---|---|
| `Embody-starter.toe` | The project: TouchDesigner's own blank project (`Samples/Setup/Base/NewProject.toe`) plus one Execute DAT, `/project1/embody_install`. |
| `embody_install.py` | That DAT's script. Edit this, never the DAT. |
| `build_starter.py` | Rebuilds the `.toe` from the two above with TouchDesigner's `toeexpand` and `toecollapse`: `python starter/build_starter.py`. |

`test_embody_bootstrap` fails while the script baked into the `.toe` differs from `embody_install.py`, so rebuild after every edit.

## The other `.toe` files in this repository

None of them is a starting point. `dev/Embody-6.toe` is Embody's own development project, and `dev/release_testing/smoke_template.toe` is a test harness that clears its own folder when it runs. See [CONTRIBUTING.md](../CONTRIBUTING.md#the-toe-files-in-this-repository).
