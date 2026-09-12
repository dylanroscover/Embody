#!/usr/bin/env python3
"""Checks and reports for the first-party specimen sync (Platform CI job sync-specimens).

  blobs <outdir>              write each manifest .tdxn to <outdir>/<sha256> as the
                              LF bytes git stores, print "slug<TAB>sha256<TAB>size<TAB>file",
                              and fail if the generated SQL does not carry that sha
  plan <plan.json> before     table of first-party-plan.sql output; GITHUB_OUTPUT
                              stale=<n> and fts=<upsert|skip>; writes rollback.sql
  plan <plan.json> after      fail unless every slug is first-party, clean and live

Stdlib only; plan.json is `wrangler d1 execute --json` output (field 2026-09-11).
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import List

WEB = Path(__file__).resolve().parents[1]
REPO = WEB.parents[2]
AUTHOR = "envoy"
SYNC_SQL = WEB / "src" / "server" / "first-party-sync.sql"
PLAN_SQL = WEB / "src" / "server" / "first-party-plan.sql"


def fail(msg: str) -> "None":
    print(f"::error title=Specimen sync::{msg}")
    raise SystemExit(1)


def manifest() -> List[dict]:
    specs = json.loads((REPO / "specimens" / "manifest.json").read_text(encoding="utf-8"))["specimens"]
    if not specs:
        fail("specimens/manifest.json lists no specimens")
    return specs


def output(**pairs: object) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    lines = "".join(f"{k}={v}\n" for k, v in pairs.items())
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(lines)
    print(lines, end="")


def summary(text: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(text + "\n")


def cmd_blobs(outdir: str) -> None:
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    sync, plan = SYNC_SQL.read_text(encoding="utf-8"), PLAN_SQL.read_text(encoding="utf-8")
    for spec in manifest():
        data = (REPO / "specimens" / spec["tdxn_path"]).read_bytes().replace(b"\r\n", b"\n")
        sha = hashlib.sha256(data).hexdigest()
        if f"'{sha}'" not in sync or f"'{sha}'" not in plan:
            fail(f"specimens/{spec['tdxn_path']} hashes to {sha}, which the generated sync SQL does not "
                 "carry. Run platform/apps/web/scripts/build-specimen-data.py and commit its output.")
        (out / sha).write_bytes(data)
        print(f"{spec['slug']}\t{sha}\t{len(data)}\t{out / sha}")


def load_plan(path: str):
    # Statement 1 is the FTS DDL; each later statement is one slug's row.
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        ddl = data[0]["results"]
        rows = [row for stmt in data[1:] for row in stmt["results"]]
    except (OSError, ValueError, KeyError, IndexError, TypeError) as exc:
        fail(f"unreadable plan output {path}: {exc}")
    return ddl, rows


def cmd_plan(path: str, stage: str) -> None:
    ddl, rows = load_plan(path)
    by_slug = {r.get("slug"): r for r in rows}
    print(f"{'slug':<20} {'found':>5} {'1st':>3} {'stale':>5}  {'current':<34} {'live sha':<12} {'repo sha':<12} copies")
    for r in rows:
        print(f"{r['slug']:<20} {r['found']:>5} {r['first_party']:>3} {str(r['stale']):>5}  "
              f"{str(r['current_version_id']):<34} {str(r['live_sha'])[:12]:<12} {r['repo_sha'][:12]:<12} "
              f"{r['copies_count']}")
    problems = []
    for spec in manifest():
        r = by_slug.get(spec["slug"])
        if r is None:
            problems.append(f"{spec['slug']}: missing from the plan output")
        elif not r["found"]:
            problems.append(f"{spec['slug']}: not in D1. The sync only updates existing first-party rows")
        elif not r["first_party"]:
            problems.append(f"{spec['slug']}: owned by @{r['author_handle']}, not @{AUTHOR}; refusing to touch it")
    if problems:
        fail("; ".join(problems))

    fts = next((d for d in ddl if d.get("name") == "specimens_fts"), None)
    compact = ((fts or {}).get("sql") or "").lower().replace(" ", "")
    mode = "upsert" if "contentless_delete=1" in compact else "skip"
    stale = [r for r in rows if r["stale"] == 1]

    if stale and stage == "before":
        if mode == "skip":
            # Prod runs the June seed's plain content='' mirror: REPLACE there stacks
            # stale tokens and DELETE errors, so the sync never writes it (field 2026-09-11).
            print("::warning title=Specimen search rows not updated::specimens_fts is not contentless_delete=1 "
                  "(the 2026-06-15 seed re-created it as a plain content='' table). The sync leaves the six "
                  "first-party search rows as they are; rebuilding the mirror is a separate owner decision.")
        if mode == "skip" and any(d.get("name") == "specimens_fts_ad" for d in ddl):
            print("::warning title=Specimen deletes are broken::trigger specimens_fts_ad deletes from a plain "
                  "contentless specimens_fts, which SQLite refuses, so deleting any indexed specimen fails.")
    if stage == "before":
        rollback = [
            f"UPDATE specimens SET current_version_id = '{r['current_version_id']}', updated_at = datetime('now') "
            f"WHERE slug = '{r['slug']}' AND author_id IN (SELECT id FROM users_profile WHERE handle = '{AUTHOR}');"
            for r in stale if r["current_version_id"]
        ]
        Path(path).with_name("rollback.sql").write_text("\n".join(rollback) + "\n", encoding="utf-8")
        output(stale=len(stale), fts=mode)
        return

    bad = [f"{r['slug']} (stale={r['stale']}, live {str(r['live_sha'])[:12]}, repo {r['repo_sha'][:12]})"
           for r in rows if r["stale"] != 0 or r["live_sha"] != r["repo_sha"]]
    if bad:
        fail("D1 still differs from the repo after the sync: " + ", ".join(bad))
    print(f"ok: all {len(rows)} first-party rows match the repo (search rows: {mode})")


def main(argv: List[str]) -> None:
    try:  # the TSV is read by bash: a Windows console would end lines with CRLF
        sys.stdout.reconfigure(newline="\n")
    except AttributeError:
        pass
    if len(argv) == 2 and argv[0] == "blobs":
        cmd_blobs(argv[1])
    elif len(argv) == 3 and argv[0] == "plan" and argv[2] in ("before", "after"):
        cmd_plan(argv[1], argv[2])
    else:
        print(__doc__)
        raise SystemExit(2)


if __name__ == "__main__":
    main(sys.argv[1:])
