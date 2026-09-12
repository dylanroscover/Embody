# embody.tools web app

Astro Worker app for embody.tools. The existing pages render from local fixtures. The backend API
routes are additive and use Cloudflare D1, R2, and KV bindings when run through the Cloudflare
adapter platform proxy.

## Run locally

From `platform/`, install workspace dependencies:

```sh
npm install
```

Then run the Astro app:

```sh
cd apps/web
npm run dev
```

The fixture pages remain standalone for design review. API routes require local Cloudflare bindings.

## Local API data

The Collection is the six first-party Specimens under the repo's top-level
`specimens/` (real `.tdxn` networks + `manifest.json`). `seed.sql`,
`src/fixtures/specimens.json`, `specimen-graphs.ts`, and the blob manifest are
all GENERATED from those by `scripts/build-specimen-data.py` -- never hand-edit
them. From `platform/apps/web/`:

```sh
python3 scripts/build-specimen-data.py          # regenerate seed.sql, first-party-sync/plan SQL, fixtures, blob manifest
wrangler d1 migrations apply embody --local
wrangler d1 execute embody --local --file ./src/server/seed.sql
bash scripts/upload-seed-blobs.sh               # real .tdxn blobs -> local R2, keyed by sha256
```

The blobs are content-addressed: the R2 key IS the sha256 of the `.tdxn` bytes
(it equals `tdn_r2_key` in `seed.sql`), so `/api/specimens/:slug/tdn` resolves.

`seed.sql` is **local only**. Never run it with `--remote`: it drops and rebuilds
`specimens_fts` (every community specimen falls out of search), deletes the
`STALE_SLUGS` rows (`ev`, `clean2`, `ff`, `clean-net`, `evil`), and re-creates the
six specimens with a new `created_at` and zeroed likes/copies/views.

### Updating the specimens on embody.tools

**The search mirror.** `specimens_fts` must be the migration-0005 form
(`content='', contentless_delete=1`). Production was seeded in June with a plain
`content=''` table, so the 0005 delete trigger could not remove its rows and
deleting any indexed specimen failed with "cannot DELETE from contentless fts5
table"; it was rebuilt and repopulated on 2026-09-12 (search text re-extracted
from each blob). Never run `seed.sql` against `--remote`: it re-creates the
mirror in the wrong form. The sync plan step and the e2e job both fail loudly if
the wrong form ever comes back.

Production gets `src/server/first-party-sync.sql`, generated from the same
manifest. It touches only the six first-party rows (matched by slug and the
`envoy` author), adds a version row for a new blob, and preserves ids,
`created_at`, likes/copies/views, reactions, comments, reports and every
community specimen with its search row. When prod already matches it writes 0
rows. It runs only in Platform CI, job `sync-specimens`:

- **Automatically** after every successful deploy on `main`. It asks prod what it
  holds (a read-only plan) and writes only what differs, rather than diffing the
  push, so a cancelled or failed sync heals on the next deploy instead of leaving
  prod stale until someone notices.
- **On demand:** GitHub > Actions > Platform CI > Run workflow, branch `main`.
  `dry_run` defaults to on (token probe + plan, no writes); turn it off to sync.

The job probes the deploy token (it needs **Account > D1 > Edit** and
**Account > Workers R2 Storage > Edit**), uploads missing blobs, records a D1
Time Travel bookmark, then applies the SQL. Every statement is gated on a
difference, so a re-run converges from any interrupted point (wrangler reports
the import as all-or-nothing, but Cloudflare does not document that, so the SQL
does not rely on it). It then checks that every
`https://embody.tools/api/specimens/<slug>/tdn` hashes to the repo file.
Its run summary prints the targeted rollback SQL and the
`wrangler d1 time-travel restore embody --bookmark=...` command.
`python dev/release_preflight.py` blocks a release while embody.tools serves a
stale specimen.

Locally the sync is a no-op right after `seed.sql`; to exercise it on a changed
database: `wrangler d1 execute embody --local --file ./src/server/first-party-sync.sql`
(`src/server/first-party-plan.sql` is the read-only status query).

In non-production environments, the submit endpoint accepts `turnstileToken: "dev-bypass"`. In
production the Turnstile gate fails closed: the `dev-bypass` token is never honored, an unknown
`ENVIRONMENT` is treated as production, and submissions are rejected unless a real Turnstile token
verifies. See `src/server/turnstile.ts`.

## E2E tests

Playwright specs in `e2e/` run against `astro dev` on local miniflare D1/R2. Each
run's `e2e/global.setup.ts` resets and reseeds local D1 and registers the e2e
admin; the specimen blobs must already be in local R2 (see "Local API data").
From `platform/apps/web/`, with a `.dev.vars` holding `ENVIRONMENT=development`
and any `BETTER_AUTH_SECRET`:

```sh
npx playwright install chromium                     # once per Playwright version
npx wrangler d1 migrations apply embody --local
npx playwright test                                 # starts astro dev on 127.0.0.1:4321
E2E_PORT=4461 npx playwright test e2e/auth.spec.ts  # another port, one spec
```

- `E2E_PORT` picks the port (default 4321); the server binds `127.0.0.1`. Outside
  CI, a server already listening on that port is reused.
- AI agents: Astro 7.3 backgrounds `astro dev` when it detects one, so the config
  sets `ASTRO_DEV_BACKGROUND=1` on the server Playwright starts. An agent can
  also start its own (`../../node_modules/.bin/astro dev --port P --host 127.0.0.1`
  daemonizes), run with `E2E_PORT=P`, and stop it with `astro dev stop`.
- A boot on a cold `node_modules/.vite` can die in Vite's dep optimizer ("The
  file does not exist at .../deps_ssr/..."); so can the first boot after
  switching between an agent-started and a Playwright-started server, since
  their Vite config hashes differ. Start it again.

**CI.** The `e2e` job in `.github/workflows/platform-ci.yml` runs the whole suite
on every push and PR that touches `platform/**` or `specimens/**`, and `deploy`
needs it, so a red e2e run never ships. Failures upload the HTML report and
traces as the `playwright-results` artifact.

**Specimen re-exports.** Every Embody re-export changes a specimen's bytes (the
header carries `exported_at`), so its sha256 no longer matches `seed.sql` and
the `Seed local R2` step fails. Regenerate and commit the seed data in the same
commit: `python3 scripts/build-specimen-data.py`.

**Production smoke.** Right after `wrangler deploy`, the deploy job checks
`https://embody.tools`: `GET /api/auth/get-session` must return 200 with `null`,
and a sign-in for an unknown address must return 401 `INVALID_EMAIL_OR_PASSWORD`.
It waits for the new version to reach the edge, must pass twice 30 s apart, and
never signs up or sends mail. If it fails, roll back
with `npx wrangler rollback` (deploy token) or Cloudflare dashboard > Workers >
embody-web > Deployments.

## Production environment / secrets

Set these on the Worker before going live (locally they live in `.dev.vars`):

| Variable | Required | Purpose |
|---|---|---|
| `BETTER_AUTH_SECRET` | yes | Session-signing secret for Better Auth. Auth cannot function without it. |
| `TURNSTILE_SECRET` | yes (prod) | Cloudflare Turnstile **secret** key (server-side siteverify). |
| `PUBLIC_TURNSTILE_SITE_KEY` | yes (prod) | Cloudflare Turnstile **site** key. Build-time/browser var (`import.meta.env`). When unset, the real widget is not mounted and the form uses the dev-bypass path; in production the server gate then rejects submissions, so set it. |
| `RESEND_API_KEY` | optional | Resend API key. When set, email verification is required and password-reset emails are sent. When unset, all email is skipped and signup/sign-in/reset still work (no provider). |
| `EMAIL_FROM` | optional | Sender address for transactional email, e.g. `embody.tools <noreply@embody.tools>` (Resend-verified domain). Defaults to that value. |
| `OWNER_NOTIFY_EMAIL` | recommended | Inbox for owner operational notifications (new signup, new specimen published, abuse report). **No address is hardcoded** -- unset means these notices are skipped. Delivery also requires `RESEND_API_KEY`. See `src/server/notifications.ts`. |
| `ADMIN_EMAILS` | recommended | Comma-separated allowlist of admin emails that may reach `/admin` (the moderation panel). **The only admin allowlist source -- no email is hardcoded.** Set it to bootstrap the first admin (e.g. `you@example.com,teammate@example.com`); from there promote others via `trust_level='admin'` in the panel. Unset -> admin access depends solely on a user's `trust_level`. See `src/server/admin.ts`. |
| `ENVIRONMENT` | yes (prod) | Set to `production` to enable the real Turnstile gate. Any other/unset value is treated as production by the server gate (fail-closed). |

Apply migrations (including `0005_fts_triggers.sql`, which adds the FTS delete-orphan trigger) before
serving traffic: `wrangler d1 migrations apply embody`.
