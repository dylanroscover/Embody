# embody.tools web scaffold

Fixture-only design review scaffold for the future embody.tools frontend.

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

This app is intentionally standalone for design review. It renders from local fixtures only and does
not require Cloudflare D1, R2, Worker bindings, auth, API routes, or secrets. Backend wiring comes in
a later batch.
