#!/usr/bin/env python3
"""Release preflight: nothing outstanding on GitHub before an Embody release.

BLOCKING: open Dependabot / code-scanning / secret-scanning alerts, draft or
triage security advisories (private vulnerability reports), open Dependabot
PRs, a red latest push-CI run per workflow on main or dev, a failed
third-party check on either tip, and real commits on main that the release
checkout (HEAD) lacks, and embody.tools serving a first-party specimen whose
/tdn bytes differ from specimens/*.tdxn. WARNING: other open PRs, open issues
(tagged NEW since the last release), merged branches left on the remote.
Fails CLOSED: a check that cannot run is itself a blocker.

It makes outstanding GitHub state visible and blocking; its one product check
is a read-only hash of the live specimens -- the platform e2e gate tests the
rest (field 2026-09-11).

Usage:  python dev/release_preflight.py [--ack KEY[,KEY...]] [--repo OWNER/NAME]
        python dev/release_preflight.py --specimens-only [--site URL]
Exit:   0 = clear, or every blocker acknowledged by the user; 1 = blocked.
Needs:  an authenticated gh CLI and git, run from the release checkout.
"""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Sequence

Runner = Callable[[Sequence[str]], str]
Reader = Callable[[str], bytes]
Fetcher = Callable[[str], bytes]

REPO_ROOT = Path(__file__).resolve().parents[1]
SITE = "https://embody.tools"

RED_CONCLUSIONS = {"failure", "cancelled", "timed_out", "action_required", "startup_failure"}
KEPT_BRANCHES = {"main", "dev", "HEAD", "gh-pages"}
DEPENDABOT_LOGINS = {"app/dependabot", "dependabot", "dependabot[bot]"}
CI_BRANCHES = ("main", "dev")
TIMEOUT_S = 120


class CheckError(Exception):
    """A check could not run. The preflight reports it as a blocker."""


@dataclass
class Finding:
    level: str  # "block" or "warn"
    key: str    # stable id for --ack, e.g. "pr:113"
    text: str


def run(cmd: Sequence[str]) -> str:
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GH_PROMPT_DISABLED="1")
    try:
        proc = subprocess.run(list(cmd), capture_output=True, text=True, encoding="utf-8", errors="replace",
                              stdin=subprocess.DEVNULL, timeout=TIMEOUT_S, env=env)
    except (OSError, subprocess.TimeoutExpired) as exc:  # missing tool, or a hung fetch
        raise CheckError(f"{cmd[0]}: {exc}") from exc
    if proc.returncode != 0:
        lines = [ln.strip() for ln in (proc.stderr or proc.stdout or "").splitlines() if ln.strip()]
        raise CheckError(" ".join(cmd[:3]) + " failed: " + (" | ".join(lines[:2])[:300] or f"exit {proc.returncode}"))
    return proc.stdout


def gh_json(runner: Runner, args: Sequence[str]):
    out = runner(["gh", *args])
    try:
        return json.loads(out) if out.strip() else None
    except json.JSONDecodeError as exc:
        raise CheckError("gh " + " ".join(args[:2]) + ": unparseable output (" + str(exc) + ")") from exc


def check_alerts(runner: Runner, repo: str) -> List[Finding]:
    out: List[Finding] = []
    feeds = [(k, f"repos/{repo}/{k}/alerts?state=open&per_page=100") for k in
             ("dependabot", "code-scanning", "secret-scanning")]
    feeds += [(f"advisory-{s}", f"repos/{repo}/security-advisories?state={s}&per_page=100") for s in ("triage", "draft")]
    for kind, path in feeds:
        try:
            items = gh_json(runner, ["api", path])
        except CheckError as exc:
            out.append(Finding("block", f"verify:{kind}", f"could not read {kind}: {exc}"))
            continue
        if not isinstance(items, list):
            out.append(Finding("block", f"verify:{kind}", f"unexpected {kind} response"))
            continue
        for a in items:
            if kind == "dependabot":
                adv = a.get("security_advisory") or {}
                pkg = ((a.get("dependency") or {}).get("package") or {}).get("name", "?")
                key, what = f"alert:dependabot:{a.get('number')}", f"{adv.get('severity', '?')} {pkg}: {adv.get('summary', '')}"
            elif kind == "code-scanning":
                rule = a.get("rule") or {}
                loc = ((a.get("most_recent_instance") or {}).get("location") or {}).get("path", "?")
                key, what = f"alert:code-scanning:{a.get('number')}", f"{rule.get('id', '?')} in {loc}"
            elif kind == "secret-scanning":
                key, what = f"alert:secret-scanning:{a.get('number')}", a.get("secret_type_display_name") or a.get("secret_type", "?")
            else:
                key = f"advisory:{a.get('ghsa_id')}"
                what = f"{a.get('state')} {a.get('severity') or '?'}: {a.get('summary', '')}"
            out.append(Finding("block", key, f"open {kind}: {what} {a.get('html_url', '')}"))
    return out


def check_prs(runner: Runner, repo: str) -> List[Finding]:
    prs = gh_json(runner, ["pr", "list", "--repo", repo, "--state", "open", "--limit", "100",
                           "--json", "number,title,author,url,isDraft,createdAt"]) or []
    out: List[Finding] = []
    for pr in prs:
        login = (pr.get("author") or {}).get("login", "")
        label = f"#{pr['number']} {pr.get('title', '')} (opened {pr.get('createdAt', '')[:10]}) {pr.get('url', '')}"
        if login in DEPENDABOT_LOGINS:
            out.append(Finding("block", f"pr:{pr['number']}", f"open Dependabot PR {label}"))
        else:
            draft = " [draft]" if pr.get("isDraft") else ""
            out.append(Finding("warn", f"pr:{pr['number']}", f"open PR by {login}{draft} {label}"))
    return out


def check_ci(runner: Runner, repo: str) -> List[Finding]:
    # Per workflow and push-only: a shared run window can drop a quiet red
    # workflow, and PR/fork runs file under arbitrary branch names.
    wfs = [w for w in (gh_json(runner, ["workflow", "list", "--repo", repo, "--all", "--json", "id,name,state"]) or [])
           if w.get("state") == "active"]
    if not wfs:
        raise CheckError("no active workflows listed")
    out: List[Finding] = []
    for br in CI_BRANCHES:
        seen = 0
        for w in sorted(wfs, key=lambda w: w.get("name", "")):
            runs = gh_json(runner, ["run", "list", "--repo", repo, "--workflow", str(w["id"]), "--branch", br,
                                    "--event", "push", "--limit", "2", "--json", "status,conclusion,url,createdAt"]) or []
            if not runs:
                continue
            seen += 1
            runs = sorted(runs, key=lambda r: r.get("createdAt", ""), reverse=True)
            last, key, name = runs[0], f"ci:{br}:{w['name']}", w["name"]
            if last.get("status") != "completed":
                prev = next((r for r in runs[1:] if r.get("status") == "completed"), None)
                if prev and prev.get("conclusion") in RED_CONCLUSIONS:
                    out.append(Finding("block", key, f"{name} on {br}: last finished run is {prev.get('conclusion')} "
                                                     f"and the rerun is still {last.get('status')} {last.get('url', '')}"))
                else:
                    out.append(Finding("warn", key, f"{name} on {br} is still {last.get('status')}; wait for it {last.get('url', '')}"))
            elif last.get("conclusion") in RED_CONCLUSIONS:
                out.append(Finding("block", key, f"latest {name} push run on {br} is {last.get('conclusion')} {last.get('url', '')}"))
        if not seen:
            out.append(Finding("block", f"verify:ci:{br}", f"no push-triggered CI runs found on {br}; renamed branch or CI off?"))
        # Third-party checks on the tip (e.g. Cloudflare Workers Builds) never appear in gh run list.
        tip = gh_json(runner, ["api", f"repos/{repo}/commits/{br}/check-runs?per_page=100"]) or {}
        for c in tip.get("check_runs", []):
            if (c.get("app") or {}).get("slug") != "github-actions" and c.get("conclusion") in RED_CONCLUSIONS:
                out.append(Finding("block", f"check:{br}:{c.get('name')}",
                                   f"third-party check {c.get('name')} on the {br} tip is {c.get('conclusion')} {c.get('html_url', '')}"))
    return out


def check_git(runner: Runner, repo: str) -> List[Finding]:
    runner(["git", "fetch", "--quiet", "--prune", "origin"])
    out: List[Finding] = []
    head = runner(["git", "rev-parse", "--abbrev-ref", "HEAD"]).strip() or "HEAD"
    # Real changes only: a release merge (dev -> main) leaves a merge commit the
    # release branch lacks, and a cherry-picked hotfix has a different sha.
    missing = [ln for ln in runner(["git", "log", "--oneline", "--no-merges", "--cherry-pick", "--right-only",
                                    "HEAD...origin/main"]).splitlines() if ln.strip()]
    if missing:
        shown = "; ".join(missing[:5]) + (" ..." if len(missing) > 5 else "")
        out.append(Finding("block", "drift:main-not-in-release",
                           f"{len(missing)} commit(s) on main are missing from {head}; merge origin/main into {head} "
                           f"(and push it) first: {shown}"))
    for line in runner(["git", "branch", "-r", "--merged", "origin/main"]).splitlines():
        name = line.strip()
        if not name or "->" in name:
            continue
        short = name.split("/", 1)[1] if name.startswith("origin/") else name
        if short not in KEPT_BRANCHES:
            out.append(Finding("warn", f"branch:{short}", f"remote branch {short} is merged into main; delete it"))
    return out


def check_issues(runner: Runner, repo: str) -> List[Finding]:
    out: List[Finding] = []
    try:
        rel = gh_json(runner, ["release", "view", "--repo", repo, "--json", "tagName,publishedAt"]) or {}
    except CheckError as exc:  # no release yet: issues still list, just untagged
        rel = {}
        out.append(Finding("warn", "verify:release", f"could not read the latest release, so NEW tags are off: {exc}"))
    tag, since = rel.get("tagName", "?"), rel.get("publishedAt", "")
    issues = gh_json(runner, ["issue", "list", "--repo", repo, "--state", "open", "--limit", "200",
                              "--json", "number,title,url,createdAt"]) or []
    for i in sorted(issues, key=lambda i: i.get("number", 0), reverse=True):
        new = f" NEW since {tag}" if since and i.get("createdAt", "") >= since else ""
        out.append(Finding("warn", f"issue:{i['number']}",
                           f"open issue #{i['number']}{new}: {i.get('title', '')} {i.get('url', '')}"))
    return out


def http_get(url: str) -> bytes:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "embody-release-preflight", "Cache-Control": "no-cache"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read()
    except (OSError, ValueError, http.client.HTTPException) as exc:
        # URLError/HTTPError are OSErrors; a bad URL is a ValueError; a truncated
        # body is an HTTPException, which used to escape as a traceback.
        raise CheckError(f"GET {url}: {exc}") from exc


def committed_reader(root: Path) -> Reader:
    """specimens/<rel> as committed on origin/main, LF -- what production deploys.

    .gitattributes normalizes .tdxn to LF, but core.autocrlf decides whether a
    blob written without it keeps CRLF, so trusting git config made the hash
    platform-dependent (macOS-only CI failure 2026-09-12). Normalize like
    worktree_reader: the published blob is LF either way.
    """
    def read(rel: str) -> bytes:
        proc = subprocess.run(["git", "-C", str(root), "show", f"origin/main:specimens/{rel}"],
                              capture_output=True, stdin=subprocess.DEVNULL, timeout=TIMEOUT_S)
        if proc.returncode != 0:
            detail = proc.stderr.decode("utf-8", "replace").strip().splitlines()
            raise CheckError(f"git show origin/main:specimens/{rel}: " + (detail[0][:200] if detail else "failed"))
        return proc.stdout.replace(b"\r\n", b"\n")
    return read


def worktree_reader(root: Path) -> Reader:
    """specimens/<rel> from this checkout, LF as git stores it."""
    def read(rel: str) -> bytes:
        return (root / "specimens" / rel).read_bytes().replace(b"\r\n", b"\n")
    return read


def check_specimens(fetch: Fetcher, read: Reader, site: str = SITE,
                    label: str = "origin/main") -> List[Finding]:
    # Prod served the 2026-06 specimen blobs for months after the repo moved on
    # (field 2026-09-11). Hash what the site serves against the repo bytes. The
    # release runs this against origin/main, NOT the release checkout: a release
    # re-exports specimens, and comparing prod with those unmerged bytes blocked
    # the very push that would fix it (field 2026-09-12). CI job sync-specimens
    # is the fix; this only detects.
    try:
        specs = json.loads(read("manifest.json").decode("utf-8"))["specimens"]
    except (OSError, ValueError, KeyError, TypeError, CheckError) as exc:
        raise CheckError(f"cannot read {label} specimens/manifest.json: {exc}") from exc
    if not specs:
        raise CheckError("specimens/manifest.json lists no specimens")
    out: List[Finding] = []
    for spec in specs:
        slug, rel = spec.get("slug"), spec.get("tdxn_path")
        try:
            want = hashlib.sha256(read(rel)).hexdigest()
        except (OSError, TypeError, CheckError) as exc:
            out.append(Finding("block", f"verify:specimen:{slug}", f"cannot read {label} specimens/{rel}: {exc}"))
            continue
        url = f"{site.rstrip('/')}/api/specimens/{slug}/tdn?cb={int(time.time())}"
        try:
            got = hashlib.sha256(fetch(url)).hexdigest()
        except CheckError as exc:
            out.append(Finding("block", f"verify:specimen:{slug}", f"could not fetch the live {slug}: {exc}"))
            continue
        if got != want:
            out.append(Finding("block", f"specimen:{slug}",
                               f"{site} serves a stale {slug}: live sha256 {got[:12]}, {label} specimens/{rel} {want[:12]}. "
                               "Sync it: Actions > Platform CI > Run workflow on main, dry_run off (job sync-specimens)"))
    return out


CHECKS = (check_alerts, check_prs, check_ci, check_git, check_issues)


def collect(runner: Runner, repo: str, fetch: Fetcher = http_get, root: Path = REPO_ROOT,
            site: str = SITE) -> List[Finding]:
    findings: List[Finding] = []
    for check in CHECKS:
        name = check.__name__.replace("check_", "")
        try:
            findings.extend(check(runner, repo))
        except CheckError as exc:
            findings.append(Finding("block", f"verify:{name}", f"could not run the {name} check: {exc}"))
    findings.extend(collect_specimens(fetch, committed_reader(root), site, "origin/main"))
    return findings


def collect_specimens(fetch: Fetcher, read: Reader, site: str, label: str) -> List[Finding]:
    try:
        return check_specimens(fetch, read, site, label)
    except CheckError as exc:
        return [Finding("block", "verify:specimens", f"could not run the specimens check: {exc}")]


def main(argv: Optional[Sequence[str]] = None, runner: Runner = run, fetch: Fetcher = http_get,
         root: Path = REPO_ROOT) -> int:
    ap = argparse.ArgumentParser(description="Embody release preflight: nothing outstanding on GitHub.")
    ap.add_argument("--repo", help="OWNER/NAME for the gh checks (default: this checkout's gh repo); "
                                   "the git checks always use this checkout")
    ap.add_argument("--ack", action="append", default=[], metavar="KEY",
                    help="blocker key the USER explicitly accepted; repeatable or comma-separated; quote keys with spaces")
    ap.add_argument("--specimens-only", action="store_true",
                    help="only compare the live first-party specimens with the repo (no gh/git); CI uses it after a sync")
    ap.add_argument("--site", default=SITE, help=f"site the specimens check reads (default {SITE})")
    args = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(errors="replace")  # PR titles can carry non-ASCII
    except AttributeError:
        pass

    acks = {k.strip() for a in args.ack for k in a.split(",") if k.strip()}
    if args.specimens_only:
        repo = f"specimens on {args.site}"
        findings = collect_specimens(fetch, worktree_reader(root), args.site, "this checkout")
    else:
        repo = args.repo
        if not repo:
            try:
                repo = (gh_json(runner, ["repo", "view", "--json", "nameWithOwner"]) or {}).get("nameWithOwner")
            except CheckError as exc:
                print(f"RESULT: BLOCKED, cannot resolve the repo ({exc}); pass --repo OWNER/NAME")
                return 1
        findings = collect(runner, repo, fetch, root, args.site)

    blockers = [f for f in findings if f.level == "block"]
    open_blockers = [f for f in blockers if f.key not in acks]
    warnings = [f for f in findings if f.level == "warn"]
    print(f"Embody release preflight: {repo}")
    for title, items in (("BLOCKING", blockers), ("WARNING", warnings)):
        print(f"{title} ({len(items)})")
        for f in items:
            tag = "ACKED " if f.key in acks else ""
            print(f"  {tag}[{f.key}] {f.text}")
    for key in sorted(acks - {f.key for f in findings}):
        print(f"NOTE: --ack {key} matched nothing; check the key")
    if open_blockers:
        print(f"RESULT: BLOCKED by {len(open_blockers)} item(s). Resolve each one, or re-run with "
              "--ack \"<key>\" for items the user explicitly accepted. Report every warning too.")
        return 1
    print(f"RESULT: CLEAR ({len(blockers)} acknowledged blocker(s), {len(warnings)} warning(s) to report)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
