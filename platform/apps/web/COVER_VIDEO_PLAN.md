# Cover Video Support - Implementation Plan

Add MP4 cover-video support to project covers on the Embody web platform, in
addition to the existing image covers. Backend: raw R2 + native `<video>` (no
Cloudflare Stream). Grid behavior: poster image + hover-play. Accepted format:
MP4 / H.264 only, <= 10 MB, magic-byte validated.

## Architecture invariant (holds in every phase)

Video is PURELY ADDITIVE. Image-only covers must behave EXACTLY as before.
Every video cover ALSO carries a poster in `thumbnail_key` (an auto-extracted
first frame), so the grid always has a fast static image and the existing
thumbnail / og:image / flip-card pipeline is reused unchanged.

Three cover states must all keep working end to end:

| Cover state    | thumbnail_key | video_key | Grid render                       | Detail render |
|----------------|---------------|-----------|-----------------------------------|---------------|
| Image-only     | set           | null      | `<img>` (unchanged)               | `<img>`       |
| Video          | set (poster)  | set       | `<img>` poster -> `<video>` hover | `<video>`     |
| No cover       | null          | null      | baked .jpg / procedural SVG       | same          |

Storage layout (same `embody-blobs` R2 bucket, binding BLOBS, no new binding):
- Poster: `thumbnails/{sha256}` (unchanged)
- Video:  `videos/{sha256}` (new prefix)

New nullable column: `specimens.video_key TEXT`.

## Phases

- Phase 0 - Schema migration (`video_key`) + db.ts plumbing (read/write/get). Invisible.
- Phase 1 - r2.ts: `putCoverVideo` (magic-byte + size validation) + `getCoverVideo` (range passthrough).
- Phase 2 - GET `/api/specimens/[slug]/video` with HTTP Range (206 / 416) support.
- Phase 3 - POST/DELETE on the same route (attach/replace/remove, author-gated) + two-step create wiring + create returns slug.
- Phase 4 - Client upload UI + poster auto-extraction (contribute.astro, edit.astro), two-step submit.
- Phase 5 - Render layer: specimenThumb.ts resolver, SpecimenCover.astro `<video>` + hover controller, the duplicated appended-card template in collection/index.astro, detail page.
- Phase 6 - Social cards (poster stays og:image), fallbacks (video error -> poster), accessibility (reduced-motion, aria).
- Phase 7 - Lifecycle (replace/remove/delete cleanup), abuse bounds, tests, deploy prep.

## Key risks

- Data-URL-in-JSON cannot carry a 10 MB video -> separate multipart endpoint, never base64.
- Safari will not play / cannot scrub without HTTP Range -> 206 + Content-Range, tested in real Safari.
- Non-MP4 / HEVC uploads will not play cross-browser -> magic-byte 'ftyp' check + MP4-only whitelist.
- Infinite-scroll cards silently lose video -> update the DUPLICATED appended-card template in collection/index.astro.
- Grid autoplay jank / bandwidth -> preload="none", load+play on hover only; poster-only on touch + reduced-motion.
- Create fails after the specimen exists -> two-step; a step-2 failure leaves an image-only cover (non-fatal, retry from Edit).

## Deploy (human-run, after the workflow; do NOT let an agent run these)

The deploy script does NOT apply D1 migrations. Run these two, in order:

1. `wrangler d1 migrations apply embody --remote`   (apply the new video_key migration)
2. `./deploy.sh`   (from platform/apps/web)

The migration MUST run before the deploy so the deployed Worker never reads a
missing column.

## Blob lifecycle (orphan policy)

Cover videos are content-addressed at videos/<sha256>, exactly like thumbnails at
thumbnails/<sha256>. deleteSpecimenById (and the per-author cascade) removes the
D1 row and its FTS mirror but does NOT delete R2 blobs. This matches the
PRE-EXISTING thumbnail policy - the image path has never deleted blobs on a
specimen delete or a cover replace. Content addressing means a replaced or
removed video simply becomes unreferenced, and identical bytes dedupe to one key.
No new cleanup was added, to stay consistent with the thumbnail path. If blob GC
is ever wanted it should sweep BOTH thumbnails/ and videos/ for keys unreferenced
by any specimen row, as a single batch job.

## Manual verification checklist (no automated harness exists)

The web app has no test runner (package.json exposes only "typecheck": "astro
check"), so verify the video path manually against a local `wrangler dev` or a
preview deploy:

1. Typecheck: `npm run typecheck` -> 0 errors.
2. Upload: contribute a specimen with an MP4 cover (<= 10 MB). The grid shows a
   poster immediately (first-frame auto-extract) and the POST to
   /api/specimens/<slug>/video returns 200 with { videoKey }.
3. Range serve (Safari-critical):
   - curl -s -D - -o /dev/null -H 'Range: bytes=0-1023' <origin>/api/specimens/<slug>/video
     -> 206, Content-Range: bytes 0-1023/<total>, Accept-Ranges: bytes,
     Content-Length: 1024.
   - curl -s -D - -o /dev/null <origin>/api/specimens/<slug>/video
     -> 200, Accept-Ranges: bytes, Content-Length: <total>.
   - A start past EOF -> 416 with Content-Range: bytes */<total>.
4. Playback: open /c/<slug> in Safari AND Chrome; the video plays and scrubs.
5. Grid hover: on /collection, hovering a video card plays it; leaving shows the
   poster; a touch device / prefers-reduced-motion shows the static poster only
   and issues NO video request until interaction.
6. Auth: POST/DELETE /api/specimens/<slug>/video as a non-author -> 403; signed
   out -> 401.
7. Remove: from Edit, "Remove video" -> DELETE returns 200, the cover reverts to
   the poster image, thumbnail_key intact.
