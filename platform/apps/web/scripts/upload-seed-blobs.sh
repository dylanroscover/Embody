#!/usr/bin/env bash
# Upload every R2 object the first-party Specimens need into LOCAL R2: each
# .tdxn under key=sha256 (content-addressed, = tdn_r2_key in seed.sql) and each
# cover image under thumbnails/<sha256> (= thumbnail_key in seed.sql, the same
# namespace the site's upload route mints). Reads
# scripts/.seed-blobs.manifest.json (produced by build-specimen-data.py; path is
# repo-relative).
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

# Parse the manifest with python (no jq dependency) into "key<TAB>content_type<TAB>path" lines.
# On Windows `python3` can be a Store shortcut that only prints an install
# nag, so pick the first name that actually runs.
PY_BIN=""
for cand in python3 python; do
  if "$cand" -c "import sys" > /dev/null 2>&1; then PY_BIN="$cand"; break; fi
done
[[ -n "$PY_BIN" ]] || { echo "python3/python not found" >&2; exit 1; }
# tr: Windows python ends lines with CRLF, which would glue a CR onto the path.
"$PY_BIN" - "$MANIFEST" <<'PY' | tr -d '\r' | while IFS=$'\t' read -r KEY CTYPE PATH_; do
import json, sys
for e in json.load(open(sys.argv[1])):
    print(f"{e['key']}\t{e['content_type']}\t{e['path']}")
PY
  echo "Uploading embody-blobs/$KEY  <-  $PATH_  ($CTYPE, local)"
  npx wrangler r2 object put "embody-blobs/$KEY" --file="$REPO_ROOT/$PATH_" \
    --content-type "$CTYPE" --local < /dev/null
done

echo "Done. Local R2 bucket 'embody-blobs' now holds the first-party .tdxn blobs and cover thumbnails."
