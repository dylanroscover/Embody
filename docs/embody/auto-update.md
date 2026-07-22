# Auto-Update

Embody can check GitHub for new releases and update itself in place — no
manual download, no drag-and-drop, and no loss of your settings or
externalizations.

!!! note "Availability"
    Self-update ships with Embody v6.0.143+. Older installs update manually
    one last time (drag in the new release `.tox` as before); from then on
    the updater is available.

## Quick start

- **Check now**: pulse **Check for Update** (Advanced page). If a newer
  release exists you'll see the version pair and a link to its release
  notes, and can install with one click.
- **Automatic**: set **Auto-Update** (Advanced page) to `Check and Notify`
  or `Check and Install`. It defaults to `Off` — nothing is checked and
  nothing leaves your machine until you opt in.

## Parameters (Advanced page)

| Parameter | Name | Meaning |
|---|---|---|
| Auto-Update | `Autoupdate` | `Off` (default): never checks. `Check and Notify`: checks once at startup and shows availability in Update Status — nothing is installed. `Check and Install`: checks at startup and installs a verified update automatically. A check that can't complete (no network, no manifest, TD too old) is logged quietly; a failure *during* an install (backup, reload, verify, rollback) always shows a dialog. |
| Check for Update | `Checkforupdate` | Checks GitHub now and prompts if an update is available (`up to date` and network errors are reported in a dialog). |
| Update Status | `Updatestatus` | Read-only status line: `Disabled` (Auto-Update is Off -- the fresh-install resting state), `Up to date (v6.0.150)`, `v6.0.151 available`, `Downloading...`, `Updated to v6.0.151`, or an error summary. |

The `Autoupdate` choice is a persisted setting (`.embody/config.json`), so it
survives updates, project moves, and re-installs like every other Embody
preference.

## What an update does

1. **Check** — a background request to
   `api.github.com/repos/dylanroscover/Embody/releases/latest`. The frame
   loop is never blocked. On a manual check every outcome is shown in a
   dialog; on the automatic startup path a failed check is logged and shown
   in Update Status without interrupting you.
2. **Gate** — the release's `embody-release.json` manifest is fetched and
   validated. If the release requires a newer TouchDesigner build than the
   one running (`min_td_build`), the update is refused with a clear message
   *before* anything is downloaded — a `.tox` saved in a newer TD build
   fails to load in older ones.
3. **Download** — the release `.tox` is downloaded to `.embody/updates/` and
   verified against the manifest's byte size and SHA-256 digest. A download
   that fails verification is discarded.
4. **Back up** — the currently installed Embody is exported to
   `.embody/updates/backup-v<version>.tox` and sanity-checked. On the manual
   path you are offered a project save first — the saved `.toe` is the
   recovery point of last resort.
5. **Install (in place)** — Embody's *contents* are reloaded from the new
   `.tox` using TouchDesigner's external-tox mechanism. The Embody COMP
   itself is never destroyed: its network location, wires, global `op.Embody`
   shortcut, and your parameter values all survive. New parameters introduced
   by the update appear with their defaults. Envoy is stopped cleanly first
   and restarts through its normal startup path afterward (if you had it
   enabled).
6. **Verify** — after the new version boots, the updater confirms the
   component is intact, stamps the new version metadata, detaches from the
   downloaded file, and reports success. If verification fails, the
   pre-update backup is restored automatically and the failure is reported.

Your externalized files and the `externalizations` table are untouched by
design — the table lives beside Embody precisely so it survives component
replacement, and the new version revalidates every tracked operator on boot
(the same path a manual drag-in upgrade uses).

## Safety model

- **Nothing silent is destructive.** `Off` is the default; `Check and
  Notify` never installs. On `Check and Install`, failures *during an
  install* — anything that touches the live component (backup, reload,
  verify, rollback) — always raise a dialog; only a fully successful install
  is quiet. Failures *before* an install starts (a check that can't reach
  GitHub, an unverifiable release, a too-old TouchDesigner) are logged to the
  Embody log and shown in Update Status, without a startup dialog on every
  launch. On the **manual** Check for Update button, every outcome —
  up-to-date, unavailable, or error — is shown in a dialog.
- **Integrity**: the manifest's SHA-256 must match the downloaded bytes, and
  the manifest names a bare `.tox` asset (no path escapes). Releases without
  a manifest (all releases before v6.0.143) are refused with a pointer to
  manual update. Note this is *tamper-detection within GitHub's trust
  domain*: the manifest and the `.tox` are published together, so the SHA-256
  protects against a corrupted or post-publish-mutated asset, not against a
  compromise of the release itself. Because there is no independent
  signature, `Check and Install` is an explicit opt-in — if you are cautious,
  prefer `Check and Notify` and review each release before installing. (A
  signed-manifest scheme with a pinned public key is planned.)
- **Compatibility**: `min_td_build` is enforced before download. Without
  this gate, loading a newer-build `.tox` on an older TouchDesigner fails
  silently and could leave a broken component.
- **Downgrade protection**: version tags are compared numerically
  (`MAJOR.MINOR.PATCH`); a release whose tag is not strictly newer than the
  installed version is never installed. Tags that don't parse are refused.
- **Crash recovery**: before the reload, a sentinel file
  (`.embody/updates/pending.json`) records the update in flight. If
  TouchDesigner crashes or is closed mid-update, the next project open
  detects it and offers to restore the backup.
- **Rollback**: a failed verification automatically reloads the pre-update
  backup. If even the rollback fails, the updater tells you to close
  *without saving* and reopen — the saved `.toe` still holds the previous
  working state.
- **Undo**: the swap is excluded from the undo stack. A stray Ctrl+Z cannot
  half-resurrect the old version.

## The dev checkout refuses self-update

If Embody's own source DATs are file-synced (you are working in the git
repository, where the repo — not a downloaded `.tox` — is the source of
truth), every update path refuses with a message to update via `git pull`
instead. This protects the working tree from being clobbered.

## Privacy

A version check is a single HTTPS request to GitHub's public API. It carries
no account data or project information — GitHub sees what any web request
shows it (your IP address and a user-agent naming the updater). With
Auto-Update `Off` (the default), Embody never phones home at all. Note
GitHub's anonymous API allows 60 requests per hour per IP address — many
machines behind one NAT (a classroom) may see checks fail temporarily;
checks fail soft and never block startup.

## For maintainers: the release manifest

Every `project.save()` in the dev project exports the release `.tox` and
writes `release/embody-release.json` beside it:

```json
{
 "schema": 1,
 "name": "Embody",
 "version": "6.0.143",
 "tag": "v6.0.143",
 "asset": "Embody-v6.0.143.tox",
 "size": 699178,
 "sha256": "…64 hex chars…",
 "td_build": "2025.33070",
 "min_td_build": "2025.33070",
 "build": 842,
 "date": "2026-07-21",
 "exported_at": "2026-07-21T12:00:00+00:00"
}
```

`min_td_build` is the build the `.tox` was saved with — the support floor by
construction. The GitHub release step attaches every file in `release/`, so
the manifest rides along automatically. **A release without its manifest is
invisible to the updater** — users on auto-update simply won't receive it.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| "cannot be verified: release has no embody-release.json" | The release predates the manifest system. Update manually from GitHub. |
| "requires TouchDesigner build X+" | Your TD build is older than the release's floor. Update TouchDesigner, or stay on the current Embody. |
| "Update check failed (network error)" | No internet, GitHub down, or the 60/hour anonymous rate limit (shared per IP). Try again later. |
| "Update Recovery" dialog at startup | A previous update was interrupted. `Restore Backup` returns to the pre-update version; `Keep Current State` leaves things as they are. |
| Status shows an old version after update | The verify step stamps version metadata a few seconds after the reload; if it still shows stale info, check the Embody log for a rollback report. |
| Updater refuses in the dev repo | Intended — see above. Use `git pull`. |
