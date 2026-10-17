#!/usr/bin/env node
// Read-only logical dump of the embody D1 database as one restorable .sql file.
//
// `wrangler d1 export` refuses any database that holds a virtual table
// ("cannot export databases with virtual tables (like FTS5)"), and specimens_fts
// is FTS5, so the nightly backup reads the schema and every real table through
// `d1 execute --json` instead and writes plain CREATE + INSERT statements.
//
// What the file holds, in order:
//   1. the schema from sqlite_master (tables, indexes, triggers, the CREATE
//      VIRTUAL TABLE itself), minus sqlite_* / _cf_* internals and the FTS
//      shadow tables (<fts>_data/_idx/_docsize/_config), which FTS5 recreates;
//   2. INSERTs for every real table, paged, in sqlite_master order;
//   3. a rebuild of the contentless specimens_fts mirror from the restored rows.
//      dat_text (key ops, read from the TDXN blob at submit time) is not in D1,
//      so it is restored empty; the sync-specimens job refills first-party rows.
//
// Restore into an EMPTY database:
//   npx wrangler d1 execute <db> --remote --file=<dump.sql>
//
// Usage: node scripts/d1-dump.mjs (--remote | --local [--persist-to DIR]) --output FILE
//        [--database embody] [--page 500]
import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const args = parseArgs(process.argv.slice(2));
const DATABASE = args.database || "embody";
const PAGE = Number(args.page || 500);
if (!args.output) fail("--output FILE is required");
if (!args.remote && !args.local) fail("pass --remote or --local");

const where = args.remote ? ["--remote"] : ["--local", ...(args["persist-to"] ? ["--persist-to", args["persist-to"]] : [])];

function fail(message) {
  console.error(`d1-dump: ${message}`);
  process.exit(1);
}

function parseArgs(argv) {
  const out = {};
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (!a.startsWith("--")) fail(`unexpected argument ${a}`);
    const key = a.slice(2);
    const next = argv[i + 1];
    if (next !== undefined && !next.startsWith("--")) {
      out[key] = next;
      i++;
    } else {
      out[key] = true;
    }
  }
  return out;
}

// wrangler's bin, found by walking up from this file: the same invocation on
// Linux CI and on a Windows laptop (npx.cmd needs a shell there, which would
// re-split the SQL argument).
function wranglerBin() {
  let dir = dirname(fileURLToPath(import.meta.url));
  for (let i = 0; i < 6; i++) {
    const candidate = join(dir, "node_modules", "wrangler", "bin", "wrangler.js");
    if (existsSync(candidate)) return candidate;
    dir = dirname(dir);
  }
  fail("wrangler not found in any parent node_modules");
}
const WRANGLER = wranglerBin();

function query(sql) {
  const out = execFileSync(
    process.execPath,
    [WRANGLER, "d1", "execute", DATABASE, ...where, "--json", "--command", sql],
    { encoding: "utf8", stdio: ["ignore", "pipe", "inherit"], maxBuffer: 256 * 1024 * 1024 }
  );
  const match = /^\[\s*[{\]]/m.exec(out);
  if (!match) fail(`wrangler returned no JSON for: ${sql.slice(0, 80)}\n${out.slice(0, 1000)}`);
  const results = JSON.parse(out.slice(match.index));
  return results.flatMap((r) => r.results || []);
}

function quoteIdent(name) {
  return `"${String(name).replace(/"/g, '""')}"`;
}

function literal(value) {
  if (value === null || value === undefined) return "NULL";
  if (typeof value === "number") return Number.isFinite(value) ? String(value) : "NULL";
  if (typeof value === "boolean") return value ? "1" : "0";
  if (Array.isArray(value)) return `X'${Buffer.from(value).toString("hex")}'`;
  if (typeof value === "object") return `'${JSON.stringify(value).replace(/'/g, "''")}'`;
  return `'${String(value).replace(/'/g, "''")}'`;
}

const master = query(
  "SELECT type, name, tbl_name, sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY rowid"
);
const virtualTables = new Set(
  master.filter((r) => r.type === "table" && /^\s*CREATE\s+VIRTUAL\s+TABLE/i.test(r.sql)).map((r) => r.name)
);
const shadowSuffixes = ["_data", "_idx", "_docsize", "_config", "_content"];
const isInternal = (name) =>
  name.startsWith("sqlite_") ||
  name.startsWith("_cf_") ||
  [...virtualTables].some((v) => shadowSuffixes.some((s) => name === v + s));

const schema = master.filter((r) => !isInternal(r.name) && !isInternal(r.tbl_name));
const dataTables = schema.filter((r) => r.type === "table" && !virtualTables.has(r.name));

const stamp = new Date().toISOString();
const lines = [];
const counts = [];
lines.push(`-- embody D1 logical dump ${stamp} (scripts/d1-dump.mjs, ${args.remote ? "remote" : "local"})`);
lines.push("-- Restore into an EMPTY database: wrangler d1 execute <db> --remote --file=<this file>");
lines.push("-- FTS shadow tables are omitted (FTS5 recreates them); specimens_fts is rebuilt at the end.");
lines.push("PRAGMA defer_foreign_keys = true;");
lines.push("");
lines.push("-- schema");
// tables and indexes first, triggers last, each in creation order
for (const kind of ["table", "index", "view", "trigger"]) {
  for (const r of schema.filter((x) => x.type === kind)) lines.push(`${r.sql.trim().replace(/;\s*$/, "")};`);
}
lines.push("");
lines.push("-- data");
for (const t of dataTables) {
  const withoutRowid = /WITHOUT\s+ROWID/i.test(t.sql);
  const ident = quoteIdent(t.name);
  let offset = 0;
  let total = 0;
  for (;;) {
    const order = withoutRowid ? "" : " ORDER BY rowid";
    const rows = query(`SELECT * FROM ${ident}${order} LIMIT ${PAGE} OFFSET ${offset}`);
    if (!rows.length) break;
    const cols = Object.keys(rows[0]);
    const colList = cols.map(quoteIdent).join(", ");
    for (const row of rows) {
      lines.push(`INSERT OR REPLACE INTO ${ident} (${colList}) VALUES (${cols.map((c) => literal(row[c])).join(", ")});`);
    }
    total += rows.length;
    offset += rows.length;
    if (rows.length < PAGE) break;
  }
  counts.push({ table: t.name, rows: total });
}
if (virtualTables.has("specimens_fts")) {
  lines.push("");
  lines.push("-- rebuild the contentless FTS mirror (dat_text is not stored in D1: empty until the next sync)");
  lines.push(
    "INSERT OR REPLACE INTO specimens_fts (rowid, slug, title, description, tags, author_handle, dat_text)\n" +
      "SELECT s.rowid, s.slug, s.title, s.description,\n" +
      "  COALESCE((SELECT group_concat(t.slug, ' ') FROM specimen_tags AS st JOIN tags AS t ON t.id = st.tag_id WHERE st.specimen_id = s.id), ''),\n" +
      "  COALESCE(u.handle, ''), ''\n" +
      "FROM specimens AS s LEFT JOIN users_profile AS u ON u.id = s.author_id;"
  );
}
lines.push("");

const output = resolve(args.output);
mkdirSync(dirname(output), { recursive: true });
writeFileSync(output, lines.join("\n"), "utf8");

console.log(`| table | rows |\n| --- | ---: |`);
for (const c of counts) console.log(`| ${c.table} | ${c.rows} |`);
console.log(`\nwrote ${output} (${schema.length} schema objects, ${dataTables.length} tables, skipped virtual: ${[...virtualTables].join(", ") || "none"})`);
