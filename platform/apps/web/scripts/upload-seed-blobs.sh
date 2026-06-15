#!/usr/bin/env bash
# Upload each first-party Specimen .tdn into the R2 bucket under key=sha256
# (content-addressed). Reads scripts/.seed-blobs.manifest.json (produced by
# build-specimen-data.py).
#
# Run from apps/web:
#   bash scripts/upload-seed-blobs.sh            # --local (miniflare; the dev server serves them)
#   bash scripts/upload-seed-blobs.sh --remote   # the deployed R2 bucket (production)
set -euo pipefail

TARGET="${1:---local}"   # --local (default) or --remote
case "$TARGET" in --local|--remote) ;; *) echo "usage: $0 [--local|--remote]" >&2; exit 2;; esac

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
  echo "Uploading embody-blobs/$SHA  <-  $PATH_  ($TARGET)"
  npx wrangler r2 object put "embody-blobs/$SHA" --file="$PATH_" "$TARGET"
done

echo "Done. R2 bucket 'embody-blobs' ($TARGET) now holds the six .tdn blobs."
