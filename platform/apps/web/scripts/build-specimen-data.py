#!/usr/bin/env python3
"""Build the first-party Specimen seed + graph fixture from REPO/specimens.

Reads specimens/manifest.json (authoritative metadata) and each
<category>/<slug>.tdn (YAML network blob). For each specimen it computes the
content-addressed R2 key (sha256 hex of the raw .tdn bytes) and byte size, then
emits three artifacts under apps/web:

  1. src/server/seed.sql       - re-runnable local D1 seed with REAL metadata.
  2. src/fixtures/specimen-graphs.ts - parsed-and-trimmed TDN objects per slug,
     shaped for TdnViewer (operators + annotations; heavy DAT/shader text
     stripped) so the interactive covers render with no runtime YAML parse and
     no per-card API call.
  3. .seed-blobs.manifest.json - {slug, sha256, size, tdn_path} list used by the
     companion uploader to push each .tdn into local R2 under key=sha256.

ASCII punctuation only. Deterministic ids: sp-<slug>, ver-<slug>, scan-<slug>.

Run from anywhere; all paths are resolved relative to the repo root.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import yaml

# --- Paths -------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent          # apps/web/scripts
WEB_DIR = SCRIPT_DIR.parent                            # apps/web
REPO_ROOT = WEB_DIR.parents[2]                         # apps/web -> apps -> platform -> repo
SPECIMENS_DIR = REPO_ROOT / "specimens"
MANIFEST_PATH = SPECIMENS_DIR / "manifest.json"

SEED_SQL_PATH = WEB_DIR / "src" / "server" / "seed.sql"
GRAPHS_TS_PATH = WEB_DIR / "src" / "fixtures" / "specimen-graphs.ts"
BLOB_MANIFEST_PATH = WEB_DIR / "scripts" / ".seed-blobs.manifest.json"
FIXTURES_PATH = WEB_DIR / "src" / "fixtures" / "specimens.json"

# First-party author. The handle is 'embody.tools' (NOT 'embody') so the public
# page resolves at /u/embody.tools and matches the fallback author_handle in
# src/lib/specimenFallback.ts. Used in users_profile and the FTS mirror.
AUTHOR_HANDLE = "embody.tools"

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

# TdnViewer (parseTDN.ts) only consumes these per-operator keys. Everything else
# (parameters, sequences, dat_content, custom_pars, flags, etc.) is dropped to
# keep the bundled covers light - the viewer draws nodes + wires, not source.
OP_KEEP_KEYS = ("name", "type", "position", "size", "color", "inputs", "comp_inputs")
ANNOTATION_KEEP_KEYS = ("name", "title", "text", "position", "size", "color")


def sql_str(value: str | None) -> str:
    """SQLite string literal with single-quote escaping. NULL for None."""
    if value is None:
        return "NULL"
    return "'" + value.replace("'", "''") + "'"


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
    """Recursively keep only the keys TdnViewer reads; strip heavy content."""
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
    """Reduce a full parsed TDN dict to the minimal TdnViewer input shape."""
    graph: dict = {}
    # Keep a few harmless top-level descriptors for parity with sample-tdn.ts.
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
        tdn_path = SPECIMENS_DIR / spec["tdn_path"]
        raw_bytes = tdn_path.read_bytes()
        sha256 = hashlib.sha256(raw_bytes).hexdigest()
        size = len(raw_bytes)
        parsed = yaml.safe_load(raw_bytes.decode("utf-8"))
        graphs[slug] = build_graph(parsed)

        rows.append(
            {
                "slug": slug,
                "name": spec["name"],
                "description": spec["description"],
                "category": spec["category"],
                "difficulty": spec["difficulty"],
                "requires": spec.get("requires", "none"),
                "op_count": spec["operator_count"],
                "family_summary": family_summary(spec.get("key_ops", [])),
                "license": spec.get("license", "CC-BY-4.0"),
                "tags": spec.get("tags", []),
                "key_ops": spec.get("key_ops", []),
                "sha256": sha256,
                "size": size,
                "tdn_path": str(tdn_path),
            }
        )

    write_seed_sql(rows)
    write_graphs_ts(rows, graphs)
    write_blob_manifest(rows)
    write_fixtures(rows)

    print(f"Generated seed for {len(rows)} specimens:")
    for r in rows:
        print(f"  {r['slug']:<22} sha256={r['sha256'][:12]}... size={r['size']}")
    return 0


def write_seed_sql(rows: list[dict]) -> None:
    cap_json = json.dumps(CLEAN_CAPABILITY, separators=(",", ":"))
    lines: list[str] = []
    a = lines.append

    a("-- Local development seed for the first-party Specimen collection.")
    a("-- GENERATED by scripts/build-specimen-data.py from REPO/specimens/. Do not edit by hand.")
    a("-- Re-runnable: purges fictional/test rows, then INSERT OR REPLACE the six real specimens.")
    a("-- Apply to the local D1 (matching the astro-dev miniflare persist path):")
    a("--   npx wrangler d1 execute embody --local --file=src/server/seed.sql")
    a("-- ASCII only.")
    a("")
    a("-- Dev auth-stub user (kept; specimens reference it as author_id).")
    a("-- Handle is 'embody.tools' so the public user page resolves at /u/embody.tools and")
    a("-- matches the fallback author_handle in src/lib/specimenFallback.ts.")
    a("INSERT OR REPLACE INTO users_profile (id, handle, avatar_url, bio, trust_level)")
    a(f"VALUES ('dev-user', '{AUTHOR_HANDLE}', NULL, 'First-party Embody specimen author. Curating the transparent TDN Collection.', 'curator');")
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
        "  id, slug, author_id, title, description, category, difficulty, requires, op_count,\n"
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
            f"    {sql_str(r['difficulty'])},\n"
            f"    {sql_str(r['requires'])},\n"
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
    a("-- Versions (content-addressed: tdn_r2_key = tdn_sha256 = sha256 of the .tdn bytes).")
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

    SEED_SQL_PATH.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def write_graphs_ts(rows: list[dict], graphs: dict) -> None:
    header = (
        "// GENERATED by scripts/build-specimen-data.py from REPO/specimens/*.tdn.\n"
        "// Do not edit by hand. Re-run the generator to refresh.\n"
        "//\n"
        "// Each entry is the parsed TDN network trimmed to what TdnViewer renders\n"
        "// (operators + annotations: name, type, position, size, color, inputs,\n"
        "// comp_inputs). Heavy embedded DAT/shader text and parameters are stripped\n"
        "// so the interactive covers stay light and need no runtime YAML parse and\n"
        "// no per-card API call. Shape matches src/fixtures/sample-tdn.ts and what\n"
        "// the TdnViewer `tdn` prop expects.\n"
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
    GRAPHS_TS_PATH.write_text("\n".join(body_parts), encoding="utf-8")


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
            "tdn_path": r["tdn_path"],
        }
        for r in rows
    ]
    BLOB_MANIFEST_PATH.write_text(
        json.dumps(payload, indent=2, ensure_ascii=True) + "\n", encoding="utf-8"
    )


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
            "difficulty": r["difficulty"],
            "description": r["description"],
            "tags": r["tags"],
            "requires": r["requires"],
            "operator_count": r["op_count"],
            "key_ops": r["key_ops"],
        }
        for r in rows
    ]
    FIXTURES_PATH.write_text(
        json.dumps(payload, indent=2, ensure_ascii=True) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    raise SystemExit(main())
