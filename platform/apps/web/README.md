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

From `platform/apps/web/`, apply the D1 schema and seed the local database:

```sh
wrangler d1 migrations apply embody --local
wrangler d1 execute embody --local --file ./src/server/seed.sql
```

The seed references small placeholder TDN blobs under `seed/`. To make
`/api/specimens/:slug/tdn` return those placeholders locally, upload the files to the local R2 bucket:

```sh
for file in ./src/server/seed-blobs/*.tdn; do
  wrangler r2 object put "embody-blobs/seed/$(basename "$file")" --file "$file" --local
done
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
