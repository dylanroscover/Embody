---
name: publish-specimen
description: MUST READ before publishing a Specimen from the lab to the embody.tools Collection -- the full path from a built /specimen_lab COMP through the user's approval to a verified live card: export, prompt, cover, manifest, generated site data, local checks, commit, PR to main, Platform CI deploy + sync-specimens, live verification, rollback. Authoring the network itself is /specimen-authoring.
---

# Publish a Specimen to the Collection

Embody dev only (no shipped template counterpart). `/specimen-authoring` gets a
network to the bar; this skill takes an approved one live on
https://embody.tools/collection with nothing done by hand on a laptop against
production. Every step below has been run end to end (tile-zoom-type,
2026-10-17); do them in order and do not skip the gates.

## The pipeline in one screen

```
/specimen_lab/<name>  --export-->  specimens/<category>/<slug>.tdxn (+ .prompt.md, .jpg)
                                   specimens/manifest.json  (curation source of truth)
          build-specimen-data.py -> seed.sql, first-party-sync.sql, first-party-plan.sql,
                                   fixtures/specimens.json, fixtures/specimen-graphs.ts,
                                   scripts/.seed-blobs.manifest.json  (all GENERATED)
commit on dev -> PR -> merge to main -> Platform CI:
   build-and-test (astro check, build, scanner tests) -> e2e (local D1/R2 seeded from specimens/)
   -> deploy (D1 migrations, wrangler deploy = Worker with the bundled fixtures)
   -> sync-specimens (R2 blobs by sha256, first-party-sync.sql into production D1,
                      live verification of /api/specimens/<slug>/tdn and /thumbnail)
```

The Worker carries the cards; D1 holds the rows; R2 holds the `.tdxn` and the
cover under their sha256. All three must land, and only `sync-specimens` lands
the last two. Blobs are content-addressed, the sync is gated on a difference,
so every stage is idempotent: re-running never harms production.

## 0. Gates before anything is exported

- The COMP lives at `/specimen_lab/<name>` with `name == slug.replace('-', '_')`
  (the save-time publisher `dev/specimen_publish.py` resolves it that way) and
  terminates in an Out TOP named `out1`.
- It clears the `/specimen-authoring` bar and the user has looked at it and said
  it is good. Publishing is the user's call, every time.
- `get_op_errors(recurse=True)` is clean; `get_op_performance` is acceptable.
- **Every expression is a pure value expression.** The community-paste sandbox
  (`Collection/safe_import.py`) neutralizes anything else to a constant, and a
  first-party Specimen is pasted through that same path by anyone who clicks
  "embody it". Allowed: parameter reads, `absTime`, `math.*`, `Par.eval()`,
  arithmetic, comparisons, ternaries, `min/max/int/len/...`. Not allowed:
  lambdas, comprehensions, `op()` chains with method calls, f-strings. Check
  before exporting:
  ```python
  sc = op.Embody.op('Collection/scanner').module
  bad = [(o.name, p.name) for o in [comp] + comp.findChildren()
         for p in o.pars() if p.mode == ParMode.EXPRESSION and p.expr
         and not sc.is_pure_value_expression(p.expr)]
  ```
  Rewrite with helper read-only parameters until `bad` is empty.

## 1. Export the network (snapshot, not the tracked file)

`export_network(root_path='/specimen_lab/<name>', include_dat_content=True,
output_file='<ABSOLUTE repo path>/specimens/<category>/<slug>.tdxn')`.
A relative `output_file` resolves against the PROJECT folder (`dev/`), not the
repo root; pass the absolute path. The "Failed to track" WARNING is expected.
`operators` in the result is the manifest's `operator_count` (docked key
tables count, annotations do not).

Dev bindings: lab DATs are externalized, so an export can carry
`file: specimen_lab/...` and `syncfile`. `dev/specimen_bindings.py` strips
them; `dev/embody/unit_tests/test_specimen_bindings.py` guards every committed
Specimen. Run it (step 5) rather than trusting the export.

## 2. Prompt file, cover, optional video

- `specimens/<category>/<slug>.prompt.md`: what it teaches, how it works,
  parameters, a one-paragraph "Recreate it" prompt, tips. Match the existing
  files; ASCII only.
- Cover `specimens/<category>/<slug>.jpg`: long edge <= 1200 px, under 500 KB,
  a frame that reads as the result (not a mid-transition blur). Render it
  deterministically: the Specimen's own `Manual` + `Phase` (or equivalent),
  `out1.cook(force=True)`, `out1.save(path, quality=0.92)`. Read the file and
  judge it against `/visual-aesthetics`.
- Cover video (the card plays it on hover; the thumbnail stays the poster):
  render a master with `/movie-export` rules (non-realtime, Add Frame per
  stepped frame, abort on a drop, `framemd5` uniqueness out of process; the lab
  keeps a `<name>_record` Movie File Out, an Info CHOP and a `<name>_render`
  Text DAT driver, output under C:/tmp/<slug>_render), then cut the hover loop
  from it:
  ```
  ffmpeg -i master.mp4 -t 8 -vf "scale=720:720:flags=lanczos,fps=30" -c:v libx264 -preset slow -crf 27 -pix_fmt yuv420p -profile:v main -movflags +faststart -an specimens/<category>/<slug>.mp4
  ```
  About 2 MB. The route cap is 10 MB and `video/mp4` with an ftyp box; the
  generator enforces both. Set `video_path` in the manifest; the row's
  `video_key` points at `videos/<sha256>` and `/api/specimens/<slug>/video`
  streams it. A slug with no `video_path` keeps whatever video its row has.

## 3. Manifest entry

Append to `specimens/manifest.json` and bump `specimen_count`. Keep the file's
inline-array formatting (`"tags": [...]` on one line) so the diff is only the
new entry; the contract is `specimens/manifest.schema.json` (it names the path
field `tdn_path`; every real entry uses `tdxn_path`). Fields: `slug`
(kebab-case), `name`, `category`, `difficulty`, `description`, `tags`,
`key_ops`, `requires: "none"`, `fallback: null`, `output_op: "out1"`,
`tdxn_path`, `thumbnail_path`, `prompt_path`, `operator_count`,
`warmup_frames`, `preview: true`, `author: "Embody"`, `license: "CC-BY-4.0"`.

## 4. Regenerate the site data

**First: `git status --short specimens/` must list only your specimen's files.**
The generator hashes the WORKING-COPY bytes of every `.tdxn` and cover, and CI
hashes the COMMITTED bytes (line endings are normalized, content is not); any
other dirty specimen file (a lab save re-exports every listed specimen) bakes a
sha into seed/sync/blob manifest that no commit matches, and both e2e and
sync-specimens fail on the drift check (field 2026-10-17, an uncommitted
thermite-and-freon re-export took the first production run down). Either commit that file
deliberately or regenerate against HEAD without touching it:
`git stash push -- specimens/<other>.tdxn`, generate, commit, `git stash pop`.
The generator prints a warning when it sees this.

```
dev/.venv-tests/Scripts/python.exe platform/apps/web/scripts/build-specimen-data.py
```
(System python has no `yaml`; the test venv does.) It rewrites six files from
`specimens/` only, deterministically; running it twice changes nothing. Never
hand-edit `seed.sql`, `first-party-sync.sql`, `first-party-plan.sql`,
`fixtures/specimens.json`, `fixtures/specimen-graphs.ts` or
`scripts/.seed-blobs.manifest.json`. The sync SQL creates a first-party row
that D1 lacks, updates a stale one, and never deletes.

## 5. Local checks (what CI will run, minus production)

1. Round trip + sandbox: import the exported file into a throwaway COMP with
   `op.Embody.ImportNetwork(path, doc, clear_first=True)`, and import
   `op.Embody.op('Collection').ext.Collection.PlanCommunityPaste(doc)['tdn']`
   into a second one; render all three at the same fixed phase and require a
   zero pixel diff (`numpyArray` on `out1`); destroy both.
2. `dev/.venv-tests/Scripts/python.exe -m pytest dev/embody/unit_tests/test_specimen_bindings.py -q`
3. `cd platform && npm run typecheck --workspace=@embody/web` (0 errors).
4. Optional dress rehearsal of the production sync on a throwaway local D1:
   `--persist-to C:/tmp/d1test`, `d1 migrations apply --local`, seed with the
   PREVIOUS commit's `seed.sql`, run `first-party-sync.sql`, confirm the new
   row exists, run it again and expect 0 changes.

## 6. Commit, then the user's go for main

Stage by name (the specimen trio, `manifest.json`, the six generated files,
`dev/specimen_lab/<name>.tdxn`, `dev/specimen_lab.tdxn`,
`dev/embody/externalizations.tsv`); never the unrelated dirty lab files.
Message: `feat: <slug> specimen, <one line on what it is>`. Pulse
`op.Embody.par.Refresh` after the commit (`refresh-after-commit.md`).

Pushing to `main` is the production deploy: do it only when the user has said
so. Then:
```
git push origin dev
gh pr create --base main --head dev --title "<slug> specimen" --body "..."
gh pr merge <n> --merge
```
(`dev` must hold only what is meant to ship; if it carries other unreleased
work, branch from `origin/main`, cherry-pick the specimen commit, PR that.)

## 7. Watch CI to green and verify live

```
gh run list --workflow=platform-ci.yml --branch main -L 1
gh run watch <id>
```
Jobs: build-and-test, e2e, check-deploy-secrets, deploy, sync-specimens.
Cloudflare's own Workers Build also deploys the Worker on every push to
`main`, independently of this workflow, so a UI change (viewer, page) can be
live while the rows and blobs are not: a specimen is published only when
`sync-specimens` is green, never because the site already looks updated.
Platform CI runs only on pushes to `main` (the PR checks are the bridge
tests), so a workflow edit is parsed locally before it is committed
(`commit-push-checklist.md`); a file that does not parse fails main with
zero jobs and deploys nothing. The
sync job's summary shows the D1 Time Travel bookmark taken before the write and
the per-slug plan (found / first_party / author / stale). A slug the user
published through the site under their own account (thermite-and-freon is held
by @rosco since 2026-10-08, with a video the repo path cannot carry) shows as
`held`: the sync never touches it and the live check skips it with a warning.
That row is the live one until it is published from the envoy account or the
slug is handed over; do not "fix" it from CI. Its last step is the
acceptance test: `/api/specimens/<slug>/tdn` must hash to the repo sha256 and
`/thumbnail` must return 200. Confirm yourself anyway:
```
curl -s https://embody.tools/api/specimens/<slug>/tdn | sha256sum
curl -sI https://embody.tools/api/specimens/<slug>/thumbnail | head -1
```
then open https://embody.tools/c/<slug> and look at the card and the viewer.
A red run is remediated in the same session (`commit-push-checklist.md`):
`gh run view <id> --log-failed`, fix, re-push, watch again.

To re-run the sync without a push (a token fix, a manual retry): Actions >
Platform CI > Run workflow on main; `dry_run` on prints the plan and uploads
blobs only, off applies the SQL.

## Rollback

- Worker: `npx wrangler rollback` from `platform/apps/web`, or Cloudflare
  dashboard > Workers > embody-web > Deployments.
- D1 rows: the bookmark in the sync job's summary, then
  `npx wrangler d1 time-travel restore embody --bookmark=<id>` (30-day window;
  the nightly `D1 Backup` workflow keeps 90-day SQL dumps as artifacts beyond it).
- A wrong `.tdxn` or cover: fix it in the repo and publish again; the old
  blobs are unreferenced, not harmful.

## Why it is shaped this way

- `seed.sql` is the LOCAL seed only: on production it rebuilds search with the
  first-party rows alone and purges slugs. The targeted `first-party-sync.sql`
  exists because that bit once (field 2026-09-11).
- Production is written only from CI, never from a laptop, so every write has a
  log, a bookmark and a verification attached (`upload-seed-blobs.sh --remote`
  refuses for this reason).
- The sync is gated on a difference and creates-if-missing, so it runs after
  every deploy without a human deciding whether it is needed.
