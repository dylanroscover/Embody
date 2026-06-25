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

echo "==> Deploying embody-web with a token scoped to CF account 6a37e9b9... (embody.tools)"
npm run deploy
