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
`specimens/` (real `.tdn` networks + `manifest.json`). `seed.sql`,
`src/fixtures/specimens.json`, `specimen-graphs.ts`, and the blob manifest are
all GENERATED from those by `scripts/build-specimen-data.py` -- never hand-edit
them. From `platform/apps/web/`:

```sh
python3 scripts/build-specimen-data.py          # regenerate seed.sql + fixtures + blob manifest
wrangler d1 migrations apply embody --local
wrangler d1 execute embody --local --file ./src/server/seed.sql
bash scripts/upload-seed-blobs.sh               # real .tdn blobs -> local R2, keyed by sha256
```

The blobs are content-addressed: the R2 key IS the sha256 of the `.tdn` bytes
(it equals `tdn_r2_key` in `seed.sql`), so `/api/specimens/:slug/tdn` resolves.

To seed **production** (deployed D1 + R2), run the generator, then target the
remote resources:

```sh
python3 scripts/build-specimen-data.py
bash scripts/upload-seed-blobs.sh --remote
wrangler d1 execute embody --remote --file ./src/server/seed.sql
```

In non-production environments, the submit endpoint accepts `turnstileToken: "dev-bypass"`. In
production the Turnstile gate fails closed: the `dev-bypass` token is never honored, an unknown
`ENVIRONMENT` is treated as production, and submissions are rejected unless a real Turnstile token
verifies. See `src/server/turnstile.ts`.

## Production environment / secrets

Set these on the Worker before going live (locally they live in `.dev.vars`):

| Variable | Required | Purpose |
|---|---|---|
| `BETTER_AUTH_SECRET` | yes | Session-signing secret for Better Auth. Auth cannot function without it. |
| `TURNSTILE_SECRET` | yes (prod) | Cloudflare Turnstile **secret** key (server-side siteverify). |
| `PUBLIC_TURNSTILE_SITE_KEY` | yes (prod) | Cloudflare Turnstile **site** key. Build-time/browser var (`import.meta.env`). When unset, the real widget is not mounted and the form uses the dev-bypass path; in production the server gate then rejects submissions, so set it. |
| `RESEND_API_KEY` | optional | Resend API key. When set, email verification is required and password-reset emails are sent. When unset, all email is skipped and signup/sign-in/reset still work (no provider). |
| `EMAIL_FROM` | optional | Sender address for transactional email, e.g. `embody.tools <noreply@embody.tools>` (Resend-verified domain). Defaults to that value. |
| `ENVIRONMENT` | yes (prod) | Set to `production` to enable the real Turnstile gate. Any other/unset value is treated as production by the server gate (fail-closed). |

Apply migrations (including `0005_fts_triggers.sql`, which adds the FTS delete-orphan trigger) before
serving traffic: `wrangler d1 migrations apply embody`.
