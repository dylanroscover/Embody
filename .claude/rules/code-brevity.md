# Code Brevity: Comments and File Size

Embody dev only (no shipped template counterpart, like `multi-agent-review.md`).

## Comments: developer shorthand

Established 2026-08-19 after a third of the main extensions had become
annotation (20-70-line incident-narrative docstrings); commit 9c262fe was the
cleanup pass. Do not let it re-accrete.

1. **Shorthand, not stories.** A comment states the invariant, the mechanism,
   and the incident as a citation -- `(field 2026-08-19)` -- not a retelling.
   Target 3-6 lines.
2. **One canonical explanation per mechanism.** Explain it once, at the site
   that owns it; every other site gets a one-line pointer ("see
   EmbodyExt._installDependencies").
3. **Say what the code cannot.** Comments carry constraints, contracts, and
   the non-obvious *why* -- never what the next line does, and never why the
   change was correct (that is review talk; it dies at merge).
4. **Keep load-bearing spec blocks.** Format specs, Args/Returns, ordering
   contracts, and threading rules stay -- they are already shorthand.
5. **Never trim MCP tool docstrings** in `EnvoyExt._register_tools` for
   style: they are the public API surface, schema-pinned
   (see `embody-code-conventions.md`).
6. **Module headers describe the extension as it IS today.** Update the
   header when behavior moves -- the EmbodyExt header once sat 2+ major
   versions stale.

Same taste as the changelog: brief theme, tight facts (see the `/release`
skill).

## File size: aim under ~1k lines

Recommended, not a hard cap. Applies to NEW files and to where new code
lands, not as a mandate to shred the existing large extensions.

- A new module starts focused; when it approaches ~1k lines, that is the
  signal to extract a coherent piece (the `convoy/` package is the model:
  `convoy_install.py`, `convoy_client.py`, ... each own one concern).
- Prefer adding to a small purpose-built module over growing a monolith --
  new subsystem code does not belong in `EmbodyExt.py`/`EnvoyExt.py` just
  because the extension calls it.
- Splitting an existing large file happens as deliberate refactor work with
  tests, never as a drive-by.
- TD is not an obstacle: extension COMPs load sibling module DATs cleanly
  (the `convoy/` package ships inside the .tox already).
