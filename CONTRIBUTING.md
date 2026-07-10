# Contributing to Embody

Thanks for your interest in Embody. Contributions are welcome -- but this is
not a typical Python repository, and knowing how it actually works will save
you (and the maintainer) a lot of friction. Please read this page before
opening a PR.

## The one thing to understand first

Embody is a TouchDesigner project. The ground truth is a **binary `.toe`
file** (`dev/Embody-6.toe`), and a large share of the text files in this repo
are **written by TouchDesigner itself**: when the project is saved, Embody
exports tagged operators to disk (`.py` for DATs, `.tdn` for COMPs, `.glsl`,
`.tsv`, `.json`, ...). Those files are the source of truth on project load,
and TD rewrites them byte-for-byte from live operator content on save.

Practical consequences:

- **Do not reformat, re-encode, rename, or "fix" TD-written files** (trailing
  whitespace, missing final newlines, tabs, CRLF endings). TouchDesigner will
  write those bytes right back on the next save, and your change becomes
  permanent churn. Formatting and linting tools are welcome only in the
  hand-edited areas listed below.
- **Never edit `dev/embody/externalizations.tsv`.** It is Embody's tracking
  table, managed exclusively by the extension.
- A change to extension code (e.g. `EmbodyExt.py`) works as a normal PR --
  the files on disk are read into TD on load -- but it only becomes part of a
  release after the maintainer round-trips it through a live TD session and
  saves. Expect that step; it is not a stall.

## Contribution zones

| Zone | Paths | How to contribute |
|---|---|---|
| **Open** -- normal PRs, no TD required | `platform/` (embody.tools web app), `docs/` (MkDocs site), `.github/` (CI), `README.md` and other root docs | Standard workflow. CI covers these. |
| **TD-mediated** -- PRs welcome, maintainer verifies in TD | `dev/embody/Embody/*.py` (extension source), `dev/embody/unit_tests/`, `.claude/rules/` and `.claude/skills/` (paired with `dev/embody/Embody/templates/` -- keep both in sync) | Edit the Python/Markdown as text. Do not touch file names, encodings, or whitespace conventions. Your PR is merged after a TD round-trip. |
| **Discuss first** -- open an issue instead of a PR | `dev/*.toe`, `release/*.tox` (binaries), `dev/specimen_lab/`, `specimens/` (TD-generated networks and shaders), `dev/embody/externalizations.tsv` | These are produced by a live TD session and cannot be meaningfully reviewed or merged as diffs. |

If you are unsure which zone a file is in: if it sits under `dev/embody/` or
`dev/specimen_lab/`, assume TouchDesigner writes it.

## Requirements

- **TouchDesigner 2025.32820 or later** (Windows / macOS). Pre-2025 builds
  are not supported and not a goal.
- **Python**: TouchDesigner's bundled Python. There is no `pyproject.toml`
  or pip package -- Embody ships as a `.tox`, not a Python distribution.
- **Dependencies**: none to install by hand. Embody's core has zero external
  dependencies; enabling Envoy auto-provisions a virtual environment
  (`dev/.venv`) with the MCP server dependencies in a background thread.

## Running the tests

The test suite runs **inside TouchDesigner** -- there is no headless runner.

1. Open `dev/Embody-6.toe` in TouchDesigner.
2. **Enable Envoy** (the toolbar toggle turns green, status reads
   `Running on port ...`). Many suites assert against live Envoy server
   state (sessions, tool guards, bridge) and will error in `setUp` with
   messages like `shared lock missing (server never started?)` if the
   server was never started in the session.
3. **Save the project first.** The suite is a stress test; always have a
   saved `.toe` as a recovery point.
4. In the Textport: `op.unit_tests.RunTests()`. Results land in the log
   files under `dev/logs/` and in `dev/embody/unit_tests/results.tsv`.

A normal run automatically excludes suites marked `DESTRUCTIVE = True`
(they mutate the whole live project and run only through a separate,
save-gated entry point). A handful of Envoy bridge retry-timing tests are
known to be flaky; a single failure there is usually noise. See
`docs/testing.md` for the full framework documentation.

## Why the commit history looks the way it does

Release commits bundle everything a TouchDesigner save produces at once: the
`.toe`, the exported release `.tox`, the tracking table, and every
externalized file the save touched. That is inherent to the save cycle and
makes version-bump commits large. Feature work is reviewable in the commits
and PRs leading up to a release; the release commit itself is best read via
its changelog entry (`docs/changelog.md`).

## Pull request guidelines

- Keep PRs small and single-purpose. A focused fix is easy to verify in TD;
  a broad sweep is not.
- Do not mix formatting changes with functional changes.
- If you change extension behavior, check whether a test in
  `dev/embody/unit_tests/` asserts the old behavior, and update it.
- If you change a rule or skill under `.claude/` that has a counterpart in
  `dev/embody/Embody/templates/`, update both (the template is what ships
  to user projects).
- Text files are UTF-8, LF (enforced by `.gitattributes`), no BOM, and use
  ASCII punctuation in code and generated text -- see
  `.claude/rules/ascii-punctuation.md` for why.

## Questions, bugs, ideas

Open a [GitHub issue](https://github.com/dylanroscover/Embody/issues) --
including for anything in the discuss-first zone, questions about the
architecture, or "why is this file like this?" confusion. Bug reports that
include your TD build number, OS, and the relevant lines from `dev/logs/`
are dramatically easier to act on.

Docs live at <https://dylanroscover.github.io/Embody/> -- the
[architecture page](https://dylanroscover.github.io/Embody/envoy/architecture/)
and the [TDN specification](https://dylanroscover.github.io/Embody/tdn/specification/)
are the fastest way to build a mental model of the codebase.
