#!/usr/bin/env bash
# Upload each first-party Specimen .tdxn into LOCAL R2 under key=sha256
# (content-addressed). Reads scripts/.seed-blobs.manifest.json (produced by
# build-specimen-data.py; tdxn_path is repo-relative).
#
# Run from apps/web:
#   bash scripts/upload-seed-blobs.sh            # local miniflare R2 (the dev server serves it)
#
# Production blobs are uploaded by Platform CI (job sync-specimens), together
# with the D1 rows that point at them -- never from a laptop (field 2026-09-11).
set -euo pipefail

TARGET="${1:---local}"
case "$TARGET" in
  --local) ;;
  --remote)
    echo "Production is synced by Platform CI (job sync-specimens), not this script." >&2
    echo "Run it from GitHub: Actions > Platform CI > Run workflow (dry_run first)." >&2
    exit 2;;
  *) echo "usage: $0 [--local]" >&2; exit 2;;
esac

cd "$(dirname "$0")/.."   # -> apps/web
REPO_ROOT="../../.."
MANIFEST="scripts/.seed-blobs.manifest.json"

if [[ ! -f "$MANIFEST" ]]; then
  echo "Missing $MANIFEST -- run: python3 scripts/build-specimen-data.py" >&2
  exit 1
fi

# Parse the manifest with python (no jq dependency) into "sha256<TAB>path" lines.
# tr: Windows python ends lines with CRLF, which would glue a CR onto the path.
python3 - "$MANIFEST" <<'PY' | tr -d '\r' | while IFS=$'\t' read -r SHA PATH_; do
import json, sys
for e in json.load(open(sys.argv[1])):
    print(f"{e['sha256']}\t{e['tdxn_path']}")
PY
  echo "Uploading embody-blobs/$SHA  <-  $PATH_  (local)"
  npx wrangler r2 object put "embody-blobs/$SHA" --file="$REPO_ROOT/$PATH_" \
    --content-type application/json --local < /dev/null
done

echo "Done. Local R2 bucket 'embody-blobs' now holds the first-party .tdxn blobs."
