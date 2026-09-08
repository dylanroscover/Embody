# GitHub Release (Post-Push)

**When:** After a successful `git push`, check whether a GitHub release should be created. This step is OPTIONAL - only proceed if release artifacts exist. Do NOT prompt the user about releases if there is nothing to release.

## 1. Detect Release Artifacts

- Glob for a `release/` or `releases/` directory at the repo root.
- If neither exists, skip this entire procedure silently.
- The assets are EXACTLY TWO files: `release/Embody-vX.Y.Z.tox` for the
  version being released and `release/embody-release.json`. Never glob
  `release/*`: the directory also holds a dozen archived `.tox` files from
  older versions, and narrowing that glob by version string is how the
  unversioned manifest got dropped from v6.2.40 and v6.2.41 (both releases
  were created with no assets, the `.tox` uploaded ~20 minutes later, the
  manifest never -- found by a consumer whose updater could not see the
  release).
- If the directory is empty, skip silently.
- **The self-updater depends on `release/embody-release.json`** (written by
  the dev save hook alongside the `.tox`). Verify it is present, that its
  `version`/`asset`/`sha256`/`size` match the `.tox` being released, and
  attach BOTH files. A release without its manifest is invisible to
  Embody's auto-updater (users must update manually).

## 2. Determine Version

Detect the version using the **first strategy that succeeds**, in order:

1. **Changelog heading**: Scan `CHANGELOG.md`, `changelog.md`, or `docs/changelog.md` for the most recent `## vX.Y.Z` or `## X.Y.Z` or `## Build NN` heading. Extract the version string.
2. **Package metadata**: Check `package.json` (`version` field), `pyproject.toml` (`[project] version`), `Cargo.toml` (`[package] version`), `setup.cfg`, or similar.
3. **Latest git tag**: Run `git describe --tags --abbrev=0`. Increment if it matches the previous release.
4. **Ask the user**: If none of the above yield a version, ask before proceeding.

## 3. Check for Existing Release

- Run `gh release list --limit 10` and check if a release with this version tag already exists.
- If it does, skip silently - do not create a duplicate.

## 4. Extract Release Notes

- If a changelog file exists, extract the section for the current version - everything under its heading until the next version heading or EOF.
- If no changelog exists, generate notes from the commit log since the last tag: `git log $(git describe --tags --abbrev=0 2>/dev/null || git rev-list --max-parents=0 HEAD)..HEAD --oneline`.
- **Prepend a project intro paragraph** before the changelog section. This serves as a landing page for visitors arriving from MCP registries or awesome lists who have no context. Use 2-3 sentences covering both Embody (externalization) and Envoy (MCP server). Include links to the docs site and changelog.
- Present the release notes to the user for approval before creating the release.

**The body is the changelog entry verbatim -- the same hard cap applies** (see
`SKILL.md` step 2: one line of theme, 3-5 bullets, under ~160 words). Never
expand it for the release page: a GitHub release is skimmed even harder than a
changelog, and an exhaustive one reads as AI slop. The intro paragraph is 2
sentences, plain, and never grows.

Whole body target: the reader takes it in without scrolling.

## 5. Create the Release

```
gh release create TAG ASSET_FILES... \
  --title "RELEASE_TITLE" \
  --notes "RELEASE_NOTES" \
  --target BRANCH
```

- **TAG**: Use the version string, prefixed with `v` if not already (e.g., `v1.2.3`). Match existing tag conventions if tags already exist in the repo.
- **ASSET_FILES**: exactly `release/Embody-vX.Y.Z.tox release/embody-release.json`,
  passed on the CREATE command so the release is never published without
  them. Not `release/*` (archived toxes), and not a create-then-upload
  split that filters by version (the manifest's name carries none).
- **TITLE**: Derive from the project name + version. Use the repo name or `package.json` name if available.
- **NOTES**: The extracted changelog section or generated commit log, passed via HEREDOC for safe formatting.
- **BRANCH**: The branch that was just pushed.

## 6. Verify and Report

- Run `gh release view TAG` to confirm creation.
- **Verify the assets, not just the release** -- this is the step that was
  missing when v6.2.40/v6.2.41 shipped without a manifest:
  ```
  gh release view TAG --json assets --jq '.assets[].name'
  curl -sIL https://github.com/dylanroscover/Embody/releases/latest/download/embody-release.json | grep -i '^location' | head -1
  ```
  The first must list BOTH `Embody-vX.Y.Z.tox` and `embody-release.json`;
  the second (the exact URL the self-updater fetches) must redirect into
  `releases/download/TAG/`. Anything else: `gh release upload TAG <file>`
  now, before reporting. A missing manifest makes the release invisible to
  every installed Embody, and nothing local ever fails.
- **The tag push triggers its own CI runs** (`gh run list --limit 5` will show
  runs on the tag ref alongside the branch runs). Watch those to completion
  too -- the v6.0.252 tag run went red on a windows-latest stall AFTER dev,
  the PR ref, and main had all passed the same code, and it went unnoticed
  until the user asked. A red tag run needs the same immediate triage as a
  red branch run.
- Report the release URL to the user.
