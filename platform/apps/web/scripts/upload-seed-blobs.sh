#!/usr/bin/env bash
# Upload each first-party Specimen .tdn into the LOCAL R2 bucket under key=sha256
# (content-addressed). Targets the same miniflare persist path the astro dev
# server uses (.wrangler/state/v3/r2), so the running dev server serves them.
#
# Run from apps/web:  bash scripts/upload-seed-blobs.sh
#
# Reads scripts/.seed-blobs.manifest.json (produced by build-specimen-data.py).
# For the DEPLOYED (remote) bucket, re-run each line with --remote instead of
# --local once R2 is provisioned:
#   npx wrangler r2 object put embody-blobs/<sha256> --file=<path> --remote
set -euo pipefail

cd "$(dirname "$0")/.."   # -> apps/web
MANIFEST="scripts/.seed-blobs.manifest.json"

if [[ ! -f "$MANIFEST" ]]; then
  echo "Missing $MANIFEST -- run: python3 scripts/build-specimen-data.py" >&2
  exit 1
fi

# Parse the manifest with python (no jq dependency) into "sha256<TAB>path" lines.
python3 - "$MANIFEST" <<'PY' | while IFS=$'\t' read -r SHA PATH_; do
import json, sys
for e in json.load(open(sys.argv[1])):
    print(f"{e['sha256']}\t{e['tdn_path']}")
PY
  echo "Uploading embody-blobs/$SHA  <-  $PATH_"
  npx wrangler r2 object put "embody-blobs/$SHA" --file="$PATH_" --local
done

echo "Done. Local R2 bucket 'embody-blobs' now holds the six .tdn blobs."
