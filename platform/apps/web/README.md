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

In non-production environments, the submit endpoint accepts `turnstileToken: "dev-bypass"`.
