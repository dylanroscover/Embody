#!/usr/bin/env python3
"""Build the first-party Specimen seed + graph fixture from REPO/specimens.

Reads specimens/manifest.json (authoritative metadata) and each
<category>/<slug>.tdxn (YAML network blob). For each specimen it computes the
content-addressed R2 key (sha256 hex of the raw .tdxn bytes) and byte size, then
emits these artifacts under apps/web, all from the SAME rows:

  1. src/server/seed.sql       - LOCAL-ONLY destructive D1 reset + seed (dev, e2e).
  2. src/server/first-party-sync.sql - targeted, idempotent, non-destructive
     sync of the six first-party rows; the only SQL that may touch production
     (Platform CI job sync-specimens).
  3. src/server/first-party-plan.sql - read-only status report for that sync.
  4. src/fixtures/specimen-graphs.ts - parsed-and-trimmed TDXN objects per slug,
     shaped for TdxnViewer (operators + annotations; heavy DAT/shader text
     stripped) so the interactive covers render with no runtime YAML parse and
     no per-card API call.
  5. scripts/.seed-blobs.manifest.json - {slug, sha256, size, tdxn_path} list
     (tdxn_path repo-relative) for the uploaders; R2 key = sha256.
  6. src/fixtures/specimens.json - homepage featured-card fixtures.

ASCII punctuation only. Every file is written with LF line endings.
Seed ids: sp-<slug>, ver-<slug>, scan-<slug>; sync ids: ver-/scan-<slug>-<sha16>.

Run from anywhere; all paths are resolved relative to the repo root.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import yaml

# --- Paths -------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent          # apps/web/scripts
WEB_DIR = SCRIPT_DIR.parent                            # apps/web
REPO_ROOT = WEB_DIR.parents[2]                         # apps/web -> apps -> platform -> repo
SPECIMENS_DIR = REPO_ROOT / "specimens"
MANIFEST_PATH = SPECIMENS_DIR / "manifest.json"

SEED_SQL_PATH = WEB_DIR / "src" / "server" / "seed.sql"
SYNC_SQL_PATH = WEB_DIR / "src" / "server" / "first-party-sync.sql"
PLAN_SQL_PATH = WEB_DIR / "src" / "server" / "first-party-plan.sql"
GRAPHS_TS_PATH = WEB_DIR / "src" / "fixtures" / "specimen-graphs.ts"
BLOB_MANIFEST_PATH = WEB_DIR / "scripts" / ".seed-blobs.manifest.json"
FIXTURES_PATH = WEB_DIR / "src" / "fixtures" / "specimens.json"

# First-party author. The handle is 'envoy' -- the Envoy MCP server is what
# actually authored these networks -- so the public page resolves at /u/envoy and
# matches the fallback author_handle in src/lib/specimenFallback.ts. Used in
# users_profile and the FTS mirror.
AUTHOR_HANDLE = "envoy"

# Avatar for the first-party author: the Embody brand mark (public/embody-mark.svg).
AUTHOR_AVATAR_URL = "/embody-mark.svg"

# Fictional placeholder + stray test rows to purge from local D1 so the page
# shows only the six real first-party specimens.
STALE_SLUGS = [
    "layered-noise-field",
    "infinite-zoom-tunnel",
    "curl-noise-swarm",
    "spectrum-reactor",
    "signed-distance-lantern",
    "bloom-grade-stack",
    "ev",
    "clean2",
    "ff",
    "clean-net",
    "evil",
]

# Empty CapabilityJson (C2) - all surface counts zero, clean verdict.
CLEAN_CAPABILITY = {
    "scanner_version": "seed",
    "verdict": "clean",
    "counts": {
        "execute_dats": 0,
        "file_read_exprs": 0,
        "web_ops": 0,
        "extensions": 0,
        "storage_payloads": 0,
        "denylisted_types": 0,
        "traversal_paths": 0,
        "external_refs": 0,
    },
    "findings": [],
}

# TdxnViewer (parseTDXN.ts) only consumes these per-operator keys. Everything else
# (parameters, sequences, dat_content, custom_pars, flags, etc.) is dropped to
# keep the bundled covers light - the viewer draws nodes + wires, not source.
OP_KEEP_KEYS = ("name", "type", "position", "size", "color", "inputs", "comp_inputs")
ANNOTATION_KEEP_KEYS = ("name", "title", "text", "position", "size", "color")


def sql_str(value: str | None) -> str:
    """SQLite string literal with single-quote escaping. NULL for None."""
    if value is None:
        return "NULL"
    return "'" + value.replace("'", "''") + "'"


def write_lf(path: Path, text: str) -> None:
    """Write UTF-8 with LF on every OS (Path.write_text emits CRLF on Windows)."""
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


# The sync matches tags by slug, so a manifest tag must already BE its slug
# (the app's slugify is lowercase [a-z0-9] runs joined by '-').
TAG_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def family_summary(key_ops: list[str]) -> str:
    """Derive a denormalized family list (e.g. "TOP,POP,MAT") from key_ops.

    key_ops are op-type names like "glslTOP", "particlePOP". Take the trailing
    family suffix of each, dedupe preserving first-seen order.
    """
    families = ["TOP", "CHOP", "SOP", "DAT", "MAT", "POP", "COMP"]
    seen: list[str] = []
    for op in key_ops:
        upper = op.upper()
        for fam in families:
            if upper.endswith(fam) and fam not in seen:
                seen.append(fam)
                break
    return ",".join(seen)


def trim_operators(operators: list) -> list:
    """Recursively keep only the keys TdxnViewer reads; strip heavy content."""
    out = []
    for op in operators:
        if not isinstance(op, dict):
            continue
        node: dict = {}
        for key in OP_KEEP_KEYS:
            if key in op and op[key] is not None:
                node[key] = op[key]
        # Recurse into nested COMP children so sub-networks still draw.
        children = []
        for child_key in ("children", "operators"):
            kids = op.get(child_key)
            if isinstance(kids, list):
                children.extend(kids)
        if children:
            node["operators"] = trim_operators(children)
        # Carry nested annotations on a COMP if present.
        nested = op.get("annotations")
        if isinstance(nested, list) and nested:
            node["annotations"] = trim_annotations(nested)
        out.append(node)
    return out


def trim_annotations(annotations: list) -> list:
    out = []
    for ann in annotations:
        if not isinstance(ann, dict):
            continue
        node = {}
        for key in ANNOTATION_KEEP_KEYS:
            if key in ann and ann[key] is not None:
                node[key] = ann[key]
        out.append(node)
    return out


def build_graph(parsed: dict) -> dict:
    """Reduce a full parsed TDXN dict to the minimal TdxnViewer input shape."""
    graph: dict = {}
    # Keep a few harmless top-level descriptors for parity with sample-tdxn.ts.
    for key in ("format", "version", "type"):
        if key in parsed:
            graph[key] = parsed[key]
    operators = parsed.get("operators")
    graph["operators"] = trim_operators(operators) if isinstance(operators, list) else []
    annotations = parsed.get("annotations")
    if isinstance(annotations, list) and annotations:
        graph["annotations"] = trim_annotations(annotations)
    return graph


def main() -> int:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    specimens = manifest["specimens"]

    rows = []  # collected per-specimen computed data
    graphs = {}  # slug -> trimmed graph dict

    for spec in specimens:
        slug = spec["slug"]
        tdxn_path = SPECIMENS_DIR / spec["tdxn_path"]
        # Hash the bytes as git stores them (.gitattributes: eol=lf). A Windows
        # checkout can carry CRLF, which would mint a key no uploaded blob has.
        raw_bytes = tdxn_path.read_bytes().replace(b"\r\n", b"\n")
        sha256 = hashlib.sha256(raw_bytes).hexdigest()
        size = len(raw_bytes)
        parsed = yaml.safe_load(raw_bytes.decode("utf-8"))
        graphs[slug] = build_graph(parsed)
        tags = list(dict.fromkeys(spec.get("tags", [])))
        bad = [t for t in tags if not TAG_SLUG_RE.match(t)]
        if bad:
            raise SystemExit(f"{slug}: tags must be lowercase slugs (a-z, 0-9, '-'): {bad}")

        rows.append(
            {
                "slug": slug,
                "name": spec["name"],
                "description": spec["description"],
                "category": spec["category"],
                # manifest.json keeps `difficulty`; D1 + fixtures say `level` (0008).
                "level": spec["difficulty"],
                # requires is a JSON array in D1 (post-0009_requires_multi). Keep
                # it a clean list here; the legacy scalar 'none'/'' -> []. The SQL
                # emitter json.dumps it so json_each(requires) never sees invalid
                # JSON (which crashed the collection facet query).
                "requires": [
                    x
                    for x in (
                        spec.get("requires")
                        if isinstance(spec.get("requires"), list)
                        else [spec.get("requires")]
                    )
                    if x and x != "none"
                ],
                "op_count": spec["operator_count"],
                "family_summary": family_summary(spec.get("key_ops", [])),
                "license": spec.get("license", "CC-BY-4.0"),
                "tags": tags,
                "key_ops": spec.get("key_ops", []),
                "sha256": sha256,
                "size": size,
                # Repo-relative, POSIX: the committed manifest once carried
                # absolute Windows paths no other machine could use.
                "tdxn_path": tdxn_path.relative_to(REPO_ROOT).as_posix(),
            }
        )

    write_seed_sql(rows)
    write_sync_sql(rows)
    write_plan_sql(rows)
    write_graphs_ts(rows, graphs)
    write_blob_manifest(rows)
    write_fixtures(rows)

    print(f"Generated seed + first-party sync for {len(rows)} specimens:")
    for r in rows:
        print(f"  {r['slug']:<22} sha256={r['sha256'][:12]}... size={r['size']}")
    return 0


def write_seed_sql(rows: list[dict]) -> None:
    cap_json = json.dumps(CLEAN_CAPABILITY, separators=(",", ":"))
    lines: list[str] = []
    a = lines.append

    a("-- LOCAL-ONLY development seed for the first-party Specimen collection.")
    a("-- NEVER run this with --remote: it drops and rebuilds specimens_fts (every")
    a("-- community specimen falls out of search), purges slugs, and re-creates the")
    a("-- six specimens (new created_at, zeroed counters). Production is updated only")
    a("-- by first-party-sync.sql through Platform CI (job sync-specimens).")
    a("-- GENERATED by scripts/build-specimen-data.py from REPO/specimens/. Do not edit by hand.")
    a("-- Re-runnable: purges fictional/test rows, then INSERT OR REPLACE the six real specimens.")
    a("-- Apply to the local D1 (matching the astro-dev miniflare persist path):")
    a("--   npx wrangler d1 execute embody --local --file=src/server/seed.sql")
    a("-- ASCII only.")
    a("")
    a("-- Dev auth-stub user (kept; specimens reference it as author_id).")
    a("-- Handle is 'envoy' so the public user page resolves at /u/envoy and")
    a("-- matches the fallback author_handle in src/lib/specimenFallback.ts.")
    a("INSERT OR REPLACE INTO users_profile (id, handle, avatar_url, bio, trust_level)")
    a(f"VALUES ('dev-user', '{AUTHOR_HANDLE}', '{AUTHOR_AVATAR_URL}', 'First-party Embody specimen author. Curating the transparent TDXN Collection.', 'curator');")
    a("")

    # specimens_fts is a contentless FTS5 mirror. Recreate it in migration 0005's
    # contentless_delete=1 form -- a plain content='' table breaks the
    # specimens_fts_ad delete trigger (DELETE FROM specimens_fts WHERE rowid=?),
    # so every specimen delete after a seed would error. Drop -> recreate so the
    # seed stays fully re-runnable; repopulated below for the six real specimens.
    a("-- Rebuild the FTS5 mirror (contentless_delete=1, matching migration 0005).")
    a("DROP TABLE IF EXISTS specimens_fts;")
    a("CREATE VIRTUAL TABLE specimens_fts USING fts5(")
    a("  slug UNINDEXED, title, description, tags, author_handle, dat_text,")
    a("  content='', contentless_delete=1")
    a(");")
    a("")

    # Purge stale rows (join tables first to avoid orphans), then the parents.
    a("-- Purge fictional placeholders and stray test specimens by slug.")
    slug_list = ", ".join(sql_str(s) for s in STALE_SLUGS)
    a(
        "DELETE FROM scans WHERE version_id IN (SELECT v.id FROM specimen_versions v "
        f"JOIN specimens s ON s.id = v.specimen_id WHERE s.slug IN ({slug_list}));"
    )
    a(
        "DELETE FROM specimen_tags WHERE specimen_id IN "
        f"(SELECT id FROM specimens WHERE slug IN ({slug_list}));"
    )
    a(
        "DELETE FROM specimen_versions WHERE specimen_id IN "
        f"(SELECT id FROM specimens WHERE slug IN ({slug_list}));"
    )
    a(
        "DELETE FROM specimen_categories WHERE specimen_id IN "
        f"(SELECT id FROM specimens WHERE slug IN ({slug_list}));"
    )
    a(f"DELETE FROM specimens WHERE slug IN ({slug_list});")
    a("")
    # Also purge any row whose deterministic id we are about to (re)insert, so a
    # re-run is clean even if a previous version of this seed left rows behind.
    real_sp_ids = ", ".join(sql_str(f"sp-{r['slug']}") for r in rows)
    a("-- Purge any prior copy of these real specimens (clean re-run).")
    a(
        "DELETE FROM scans WHERE version_id IN (SELECT id FROM specimen_versions "
        f"WHERE specimen_id IN ({real_sp_ids}));"
    )
    a(f"DELETE FROM specimen_tags WHERE specimen_id IN ({real_sp_ids});")
    a(f"DELETE FROM specimen_versions WHERE specimen_id IN ({real_sp_ids});")
    a(f"DELETE FROM specimen_categories WHERE specimen_id IN ({real_sp_ids});")
    a(f"DELETE FROM specimens WHERE id IN ({real_sp_ids});")
    a("")

    # Tags - dedupe across all specimens.
    a("-- Tags (deduped across all specimens).")
    seen_tags: dict[str, str] = {}
    for r in rows:
        for tag in r["tags"]:
            if tag not in seen_tags:
                seen_tags[tag] = tag
    a("INSERT OR REPLACE INTO tags (id, name, slug) VALUES")
    tag_values = [
        f"  ({sql_str('tag-' + t)}, {sql_str(t)}, {sql_str(t)})" for t in seen_tags
    ]
    a(",\n".join(tag_values) + ";")
    a("")

    # specimens
    a("-- Specimens (real first-party metadata from specimens/manifest.json).")
    a(
        "INSERT OR REPLACE INTO specimens (\n"
        "  id, slug, author_id, title, description, category, level, requires, op_count,\n"
        "  family_summary, current_version_id, thumbnail_key, license, visibility, tier, scan_status,\n"
        "  capability_json, likes_count, views_count, copies_count\n"
        ") VALUES"
    )
    sp_values = []
    for r in rows:
        sp_values.append(
            "  (\n"
            f"    {sql_str('sp-' + r['slug'])},\n"
            f"    {sql_str(r['slug'])},\n"
            f"    'dev-user',\n"
            f"    {sql_str(r['name'])},\n"
            f"    {sql_str(r['description'])},\n"
            f"    {sql_str(r['category'])},\n"
            f"    {sql_str(r['level'])},\n"
            f"    {sql_str(json.dumps(r['requires']))},\n"
            f"    {r['op_count']},\n"
            f"    {sql_str(r['family_summary'])},\n"
            f"    {sql_str('ver-' + r['slug'])},\n"
            f"    '',\n"
            f"    {sql_str(r['license'])},\n"
            f"    'public',\n"
            f"    'featured',\n"
            f"    'clean',\n"
            f"    {sql_str(cap_json)},\n"
            f"    0,\n"
            f"    0,\n"
            f"    0\n"
            "  )"
        )
    a(",\n".join(sp_values) + ";")
    a("")

    # specimen_versions
    a("-- Versions (content-addressed: tdn_r2_key = tdn_sha256 = sha256 of the .tdxn bytes).")
    a(
        "INSERT OR REPLACE INTO specimen_versions (\n"
        "  id, specimen_id, version_num, tdn_r2_key, tdn_sha256, size_bytes, op_count, scan_id,\n"
        "  signature_ref, changelog\n"
        ") VALUES"
    )
    ver_values = []
    for r in rows:
        ver_values.append(
            f"  ({sql_str('ver-' + r['slug'])}, {sql_str('sp-' + r['slug'])}, 1, "
            f"{sql_str(r['sha256'])}, {sql_str(r['sha256'])}, {r['size']}, {r['op_count']}, "
            f"{sql_str('scan-' + r['slug'])}, NULL, 'First-party specimen.')"
        )
    a(",\n".join(ver_values) + ";")
    a("")

    # scans
    a("-- Scans (clean verdict, empty capability surface).")
    a(
        "INSERT OR REPLACE INTO scans (\n"
        "  id, version_id, scanner_version, verdict, capability_json, findings_json\n"
        ") VALUES"
    )
    scan_values = []
    for r in rows:
        scan_values.append(
            f"  ({sql_str('scan-' + r['slug'])}, {sql_str('ver-' + r['slug'])}, 'seed', 'clean', "
            f"{sql_str(cap_json)}, '[]')"
        )
    a(",\n".join(scan_values) + ";")
    a("")

    # specimen_tags
    a("-- Specimen <-> tag links.")
    a("INSERT OR IGNORE INTO specimen_tags (specimen_id, tag_id) VALUES")
    st_values = []
    for r in rows:
        for tag in r["tags"]:
            st_values.append(f"  ({sql_str('sp-' + r['slug'])}, {sql_str('tag-' + tag)})")
    a(",\n".join(st_values) + ";")
    a("")

    # FTS5 mirror - rowid must equal the specimen rowid. content='' FTS5 means we
    # write rows explicitly; each SELECT pulls the real rowid for its slug.
    a("-- FTS5 keyword mirror (rowid = specimen rowid; dat_text = key_ops).")
    for r in rows:
        tags_joined = " ".join(r["tags"])
        dat_text = " ".join(r["key_ops"])
        a(
            "INSERT OR REPLACE INTO specimens_fts "
            "(rowid, slug, title, description, tags, author_handle, dat_text)"
        )
        a(
            f"SELECT rowid, {sql_str(r['slug'])}, {sql_str(r['name'])}, "
            f"{sql_str(r['description'])}, {sql_str(tags_joined)}, '{AUTHOR_HANDLE}', {sql_str(dat_text)}"
        )
        a(f"FROM specimens WHERE id = {sql_str('sp-' + r['slug'])};")
        a("")

    # The level rename (0008) and this join table (0010) were hand edits to
    # seed.sql, lost when the 2026-08-30 regen rewrote it from this script;
    # the setup then died on "no column named difficulty" (field 2026-09-11).
    a("-- Category membership (multi). Seed the join table from each specimen's primary")
    a("-- category so the collection facet filter (which reads specimen_categories) and")
    a("-- the category facets list include the seeded rows. Mirrors migration 0010.")
    a("INSERT OR IGNORE INTO specimen_categories (specimen_id, category)")
    a("SELECT id, category FROM specimens WHERE category IS NOT NULL AND category <> '';")

    write_lf(SEED_SQL_PATH, "\n".join(lines).rstrip() + "\n")


# --- First-party sync (production-safe) ---------------------------------------
# Prod was seeded once (2026-06-15) and never again because the only documented
# path was seed.sql, which wipes search and purges slugs (field 2026-09-11).
# These two files touch ONLY the six first-party rows, resolved by slug + the
# 'envoy' author, and every write is gated on a difference, so re-runs are no-ops.

FTS_IS_CONTENTLESS_DELETE = (
    "EXISTS (SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'specimens_fts'"
    " AND instr(replace(lower(sql), ' ', ''), 'contentless_delete=1') > 0)"
)


def first_party_guard(slug: str, alias: str = "s") -> str:
    """Row filter matching only the slug's specimen authored by AUTHOR_HANDLE."""
    return (
        f"{alias}.slug = {sql_str(slug)} AND {alias}.author_id IN "
        f"(SELECT id FROM users_profile WHERE handle = {sql_str(AUTHOR_HANDLE)})"
    )


def stale_predicate(r: dict, cap_json: str, alias: str = "s") -> str:
    """True while the D1 row, its tag/category sets or its current blob differ
    from the repo. Must be evaluated before the UPDATE it gates."""
    s = alias
    sha = sql_str(r["sha256"])
    tag_list = ", ".join(sql_str(t) for t in r["tags"])
    n_tags = len(r["tags"])
    clauses = [
        f"{s}.title IS NOT {sql_str(r['name'])}",
        f"{s}.description IS NOT {sql_str(r['description'])}",
        f"{s}.category IS NOT {sql_str(r['category'])}",
        f"{s}.level IS NOT {sql_str(r['level'])}",
        f"{s}.requires IS NOT {sql_str(json.dumps(r['requires']))}",
        f"{s}.op_count IS NOT {r['op_count']}",
        f"{s}.family_summary IS NOT {sql_str(r['family_summary'])}",
        f"{s}.license IS NOT {sql_str(r['license'])}",
        f"{s}.scan_status IS NOT 'clean'",
        f"{s}.capability_json IS NOT {sql_str(cap_json)}",
        f"(SELECT v.tdn_sha256 FROM specimen_versions AS v WHERE v.id = {s}.current_version_id) IS NOT {sha}",
        f"(SELECT COUNT(*) FROM specimen_tags AS st WHERE st.specimen_id = {s}.id) <> {n_tags}",
        f"(SELECT COUNT(*) FROM specimen_tags AS st JOIN tags AS t ON t.id = st.tag_id"
        f" WHERE st.specimen_id = {s}.id AND t.slug IN ({tag_list})) <> {n_tags}",
        f"(SELECT COUNT(*) FROM specimen_categories AS sc WHERE sc.specimen_id = {s}.id) <> 1",
        f"NOT EXISTS (SELECT 1 FROM specimen_categories AS sc"
        f" WHERE sc.specimen_id = {s}.id AND sc.category = {sql_str(r['category'])})",
    ]
    return "(\n      " + "\n   OR ".join(clauses) + "\n  )"


def write_sync_sql(rows: list[dict]) -> None:
    cap_json = json.dumps(CLEAN_CAPABILITY, separators=(",", ":"))
    lines: list[str] = []
    a = lines.append

    a("-- First-party Specimen sync. GENERATED by scripts/build-specimen-data.py from")
    a("-- REPO/specimens/. Do not edit by hand. ASCII only.")
    a("-- Production-safe: touches only the six first-party specimens (slug + author")
    a(f"-- '{AUTHOR_HANDLE}'), never deletes a specimen, never changes ids, created_at,")
    a("-- likes/views/copies, reactions, thumbnail/video, visibility or tier, and every")
    a("-- statement is gated on a difference, so a second run writes 0 rows.")
    a("-- Production runs it ONLY via Platform CI (job sync-specimens), after the blobs")
    a("-- are in R2 and a D1 Time Travel bookmark is recorded. Local:")
    a("--   npx wrangler d1 execute embody --local --file=src/server/first-party-sync.sql")
    a("")
    a("-- 1. FTS mirror rows, FIRST: the gate reads the pre-update row. Only on the")
    a("--    contentless_delete=1 table (migration 0005); on a plain content='' table")
    a("--    INSERT OR REPLACE keeps the old tokens, so it is skipped there.")
    for r in rows:
        guard = first_party_guard(r["slug"])
        a(
            "INSERT OR REPLACE INTO specimens_fts "
            "(rowid, slug, title, description, tags, author_handle, dat_text)"
        )
        a(
            f"SELECT s.rowid, s.slug, {sql_str(r['name'])}, {sql_str(r['description'])}, "
            f"{sql_str(' '.join(r['tags']))}, u.handle, {sql_str(' '.join(r['key_ops']))}"
        )
        a("FROM specimens AS s JOIN users_profile AS u ON u.id = s.author_id")
        a(f"WHERE {guard}")
        a(f"  AND {FTS_IS_CONTENTLESS_DELETE}")
        a(f"  AND {stale_predicate(r, cap_json)};")
        a("")

    a("-- 2. Tags the six need (an existing tag row is never rewritten).")
    seen_tags: list[str] = []
    for r in rows:
        for tag in r["tags"]:
            if tag not in seen_tags:
                seen_tags.append(tag)
    a("INSERT OR IGNORE INTO tags (id, name, slug) VALUES")
    a(",\n".join(f"  ({sql_str('tag-' + t)}, {sql_str(t)}, {sql_str(t)})" for t in seen_tags) + ";")
    a("")

    for r in rows:
        slug = r["slug"]
        sha = sql_str(r["sha256"])
        ver_id = sql_str(f"ver-{slug}-{r['sha256'][:16]}")
        scan_id = sql_str(f"scan-{slug}-{r['sha256'][:16]}")
        guard = first_party_guard(slug)
        tag_list = ", ".join(sql_str(t) for t in r["tags"])
        cat = sql_str(r["category"])
        a(f"-- {slug}: {r['tdxn_path']} sha256={r['sha256']} size={r['size']}")
        a("-- Version row for the repo blob, unless this specimen already has one.")
        a(
            "INSERT INTO specimen_versions (id, specimen_id, version_num, tdn_r2_key, tdn_sha256,"
            " size_bytes, op_count, scan_id, signature_ref, changelog)"
        )
        a(
            f"SELECT {ver_id}, s.id,"
            " (SELECT COALESCE(MAX(v.version_num), 0) + 1 FROM specimen_versions AS v WHERE v.specimen_id = s.id),"
            f" {sha}, {sha}, {r['size']}, {r['op_count']}, {scan_id}, NULL,"
            f" {sql_str('First-party sync of ' + r['tdxn_path'])}"
        )
        a("FROM specimens AS s")
        a(f"WHERE {guard}")
        a(
            "  AND NOT EXISTS (SELECT 1 FROM specimen_versions AS v"
            f" WHERE v.specimen_id = s.id AND v.tdn_sha256 = {sha});"
        )
        a("INSERT INTO scans (id, version_id, scanner_version, verdict, capability_json, findings_json)")
        a(f"SELECT {scan_id}, {ver_id}, 'seed', 'clean', {sql_str(cap_json)}, '[]'")
        a(
            f"WHERE EXISTS (SELECT 1 FROM specimen_versions WHERE id = {ver_id})"
            f" AND NOT EXISTS (SELECT 1 FROM scans WHERE id = {scan_id});"
        )
        a("-- Metadata + current version. Engagement, visibility, tier, media untouched.")
        a("UPDATE specimens AS s")
        a(
            f"SET title = {sql_str(r['name'])}, description = {sql_str(r['description'])},"
            f" category = {cat}, level = {sql_str(r['level'])},"
            f" requires = {sql_str(json.dumps(r['requires']))}, op_count = {r['op_count']},"
            f" family_summary = {sql_str(r['family_summary'])}, license = {sql_str(r['license'])},"
            f" scan_status = 'clean', capability_json = {sql_str(cap_json)},"
        )
        a(
            "    current_version_id = (SELECT v.id FROM specimen_versions AS v"
            f" WHERE v.specimen_id = s.id AND v.tdn_sha256 = {sha} ORDER BY v.version_num DESC LIMIT 1),"
        )
        a("    updated_at = datetime('now')")
        a(f"WHERE {guard}")
        a(
            "  AND EXISTS (SELECT 1 FROM specimen_versions AS v"
            f" WHERE v.specimen_id = s.id AND v.tdn_sha256 = {sha})"
        )
        a(f"  AND {stale_predicate(r, cap_json)};")
        a("-- Tag and category sets: drop links the repo no longer lists, add missing ones.")
        a(
            f"DELETE FROM specimen_tags WHERE specimen_id IN (SELECT s.id FROM specimens AS s WHERE {guard})"
            f" AND tag_id NOT IN (SELECT t.id FROM tags AS t WHERE t.slug IN ({tag_list}));"
        )
        a(
            "INSERT OR IGNORE INTO specimen_tags (specimen_id, tag_id)"
            f" SELECT s.id, t.id FROM specimens AS s JOIN tags AS t ON t.slug IN ({tag_list}) WHERE {guard};"
        )
        a(
            f"DELETE FROM specimen_categories WHERE specimen_id IN (SELECT s.id FROM specimens AS s WHERE {guard})"
            f" AND category NOT IN ({cat});"
        )
        a(
            "INSERT OR IGNORE INTO specimen_categories (specimen_id, category)"
            f" SELECT s.id, {cat} FROM specimens AS s WHERE {guard};"
        )
        a("")

    write_lf(SYNC_SQL_PATH, "\n".join(lines).rstrip() + "\n")


def write_plan_sql(rows: list[dict]) -> None:
    cap_json = json.dumps(CLEAN_CAPABILITY, separators=(",", ":"))
    lines: list[str] = []
    a = lines.append
    a("-- First-party Specimen sync status. GENERATED by scripts/build-specimen-data.py.")
    a("-- READ-ONLY. Result 1: the specimens_fts DDL and its delete trigger. Then one")
    a("-- result per first-party slug: found, first_party, live vs repo sha, stale")
    a("-- (1 = first-party-sync.sql would write). One statement per slug: D1 refuses a")
    a("-- 6-term UNION ALL (too many terms in compound SELECT). CI runs it via")
    a("-- --command with these comment lines stripped, never --file (the import path).")
    a("SELECT type, name, sql FROM sqlite_master")
    a("WHERE name IN ('specimens_fts', 'specimens_fts_ad') ORDER BY name;")
    parts = []
    for r in rows:
        slug = sql_str(r["slug"])
        sha = sql_str(r["sha256"])
        parts.append(
            f"SELECT {slug} AS slug, {sha} AS repo_sha, {r['size']} AS repo_size,\n"
            "  s.id AS specimen_id, u.handle AS author_handle,\n"
            "  CASE WHEN s.id IS NULL THEN 0 ELSE 1 END AS found,\n"
            f"  CASE WHEN u.handle = {sql_str(AUTHOR_HANDLE)} THEN 1 ELSE 0 END AS first_party,\n"
            "  s.created_at, s.updated_at, s.visibility, s.likes_count, s.copies_count,\n"
            "  s.current_version_id, cv.version_num AS current_version, cv.tdn_sha256 AS live_sha,\n"
            "  (SELECT v.id FROM specimen_versions AS v WHERE v.specimen_id = s.id\n"
            f"     AND v.tdn_sha256 = {sha} ORDER BY v.version_num DESC LIMIT 1) AS repo_version_id,\n"
            f"  CASE WHEN s.id IS NULL THEN NULL WHEN {stale_predicate(r, cap_json)} THEN 1 ELSE 0 END AS stale\n"
            "FROM (SELECT 1) AS one\n"
            f"LEFT JOIN specimens AS s ON s.slug = {slug}\n"
            "LEFT JOIN users_profile AS u ON u.id = s.author_id\n"
            "LEFT JOIN specimen_versions AS cv ON cv.id = s.current_version_id;"
        )
    a("\n".join(parts))
    write_lf(PLAN_SQL_PATH, "\n".join(lines).rstrip() + "\n")


def write_graphs_ts(rows: list[dict], graphs: dict) -> None:
    header = (
        "// GENERATED by scripts/build-specimen-data.py from REPO/specimens/*.tdxn.\n"
        "// Do not edit by hand. Re-run the generator to refresh.\n"
        "//\n"
        "// Each entry is the parsed TDXN network trimmed to what TdxnViewer renders\n"
        "// (operators + annotations: name, type, position, size, color, inputs,\n"
        "// comp_inputs). Heavy embedded DAT/shader text and parameters are stripped\n"
        "// so the interactive covers stay light and need no runtime YAML parse and\n"
        "// no per-card API call. Shape matches src/fixtures/sample-tdxn.ts and what\n"
        "// the TdxnViewer `tdxn` prop expects.\n"
        "//\n"
        "// SCALING ROLE (post server-side collection): this bundle is now ONLY the\n"
        "// fast-path for the SSR-rendered FIRST page of the collection -- those cards\n"
        "// paint their cover with no fetch. Every card appended client-side (infinite\n"
        "// scroll) instead LAZY-FETCHES its graph from\n"
        "// /api/specimens/<slug>/tdn?format=graph (parsed + trimmed server-side), so the\n"
        "// page NEVER bundles thousands of graphs at build time. Because the generator\n"
        "// only emits the handful of first-party specimens, this file stays small even\n"
        "// as the live Collection grows to thousands. It is intentionally retained, not\n"
        "// retired; the per-slug endpoint is the path that scales.\n"
    )
    body_parts = [header, ""]
    body_parts.append(
        "export const specimenGraphs: Record<string, Record<string, unknown>> = "
        + to_ts_literal({slug: graphs[slug] for slug in (r["slug"] for r in rows)}, 0)
        + ";"
    )
    body_parts.append("")
    body_parts.append(
        "export function specimenGraph(slug: string): Record<string, unknown> | undefined {"
    )
    body_parts.append("  return specimenGraphs[slug];")
    body_parts.append("}")
    body_parts.append("")
    write_lf(GRAPHS_TS_PATH, "\n".join(body_parts))


def to_ts_literal(value, indent: int) -> str:
    """Emit a JSON-compatible value as a TS object/array literal (2-space indent).

    All specimen-graph values are plain JSON (strings, numbers, bools, null,
    lists, dicts) after trimming - no expressions survive the strip - so a
    JSON-flavored emitter is faithful and ASCII-safe.
    """
    pad = "  " * indent
    child_pad = "  " * (indent + 1)
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value) if isinstance(value, float) else str(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=True)
    if isinstance(value, list):
        if not value:
            return "[]"
        items = [child_pad + to_ts_literal(v, indent + 1) for v in value]
        return "[\n" + ",\n".join(items) + "\n" + pad + "]"
    if isinstance(value, dict):
        if not value:
            return "{}"
        items = []
        for k, v in value.items():
            key = json.dumps(k, ensure_ascii=True)
            items.append(f"{child_pad}{key}: {to_ts_literal(v, indent + 1)}")
        return "{\n" + ",\n".join(items) + "\n" + pad + "}"
    raise TypeError(f"Unsupported value type: {type(value)!r}")


def write_blob_manifest(rows: list[dict]) -> None:
    payload = [
        {
            "slug": r["slug"],
            "sha256": r["sha256"],
            "size": r["size"],
            "tdxn_path": r["tdxn_path"],
        }
        for r in rows
    ]
    write_lf(BLOB_MANIFEST_PATH, json.dumps(payload, indent=2, ensure_ascii=True) + "\n")


def write_fixtures(rows: list[dict]) -> None:
    """Homepage featured-card fixtures (src/fixtures/specimens.json).

    Consumed only by index.astro for the static landing-page cards, which render
    slug/name/category/requires/description (+ a procedural thumbnail keyed off
    slug+category). Generated from the SAME manifest as the seed so the landing
    page can never drift from the real Collection again (the prior stale set was
    abandoned placeholder data).
    """
    payload = [
        {
            "slug": r["slug"],
            "name": r["name"],
            "category": r["category"],
            "level": r["level"],
            "description": r["description"],
            "tags": r["tags"],
            "requires": r["requires"],
            "operator_count": r["op_count"],
            "key_ops": r["key_ops"],
        }
        for r in rows
    ]
    write_lf(FIXTURES_PATH, json.dumps(payload, indent=2, ensure_ascii=True) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
