#!/usr/bin/env bash
# Deploy the embody.tools worker to the embody Cloudflare account.
#
# Loads an account-scoped CLOUDFLARE_API_TOKEN from .cloudflare.env (gitignored)
# so this project ALWAYS deploys to its own account (6a37e9b9...), independent of
# whichever account `wrangler login` is currently using. Pairs with the pinned
# account_id in wrangler.jsonc.
#
# One-time setup:
#   1. cp .cloudflare.env.example .cloudflare.env
#   2. Create an "Edit Cloudflare Workers" API token in the EMBODY Cloudflare
#      account and paste it into .cloudflare.env
#   3. ./deploy.sh
set -euo pipefail
cd "$(dirname "$0")"

if [ -f .cloudflare.env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.cloudflare.env
  set +a
fi

if [ -z "${CLOUDFLARE_API_TOKEN:-}" ]; then
  echo "ERROR: CLOUDFLARE_API_TOKEN is not set." >&2
  echo "Copy .cloudflare.env.example to .cloudflare.env and paste a token for the" >&2
  echo "embody Cloudflare account (6a37e9b97eb8cf7ac208714658168a8c)," >&2
  echo "created via My Profile -> API Tokens -> 'Edit Cloudflare Workers'." >&2
  exit 1
fi

# Pre-deploy schema guard: refuse to ship code against an out-of-date database.
# Deploying a build that SELECTs a column the live D1 lacks fails at runtime in a
# way that is easy to miss -- e.g. a swallowed query error surfacing as a 404
# (the 0007_display_name miss that broke /u/<handle> on 2026-06-28). Applying the
# migration FIRST keeps live code from ever hitting an older schema. Set
# SKIP_MIGRATION_CHECK=1 to bypass (rare: e.g. a destructive migration meant to
# run AFTER the deploy).
if [ "${SKIP_MIGRATION_CHECK:-0}" != "1" ]; then
  echo "==> Checking for unapplied D1 migrations on 'embody' (remote)..."
  set +e
  MIGRATION_OUTPUT="$(npx wrangler d1 migrations list embody --remote 2>&1)"
  MIGRATION_RC=$?
  set -e
  if [ "$MIGRATION_RC" -ne 0 ]; then
    echo "$MIGRATION_OUTPUT" >&2
    echo "ERROR: could not check D1 migration status (wrangler exited $MIGRATION_RC)." >&2
    echo "Resolve the error above, or bypass with: SKIP_MIGRATION_CHECK=1 ./deploy.sh" >&2
    exit 1
  fi
  if echo "$MIGRATION_OUTPUT" | grep -qiE "no migrations to apply"; then
    echo "    D1 schema is up to date."
  else
    echo "$MIGRATION_OUTPUT" >&2
    echo "" >&2
    echo "ERROR: unapplied D1 migrations (above). Apply them BEFORE deploying so" >&2
    echo "live code never hits an older schema:" >&2
    echo "    npx wrangler d1 migrations apply embody --remote" >&2
    echo "" >&2
    echo "To deploy without applying (rare), re-run with: SKIP_MIGRATION_CHECK=1 ./deploy.sh" >&2
    exit 1
  fi
fi

echo "==> Deploying embody-web with a token scoped to CF account 6a37e9b9... (embody.tools)"
npm run deploy
