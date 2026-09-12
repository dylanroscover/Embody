"""release_preflight: the GitHub outstanding-items gate at the top of the /release flow.

Pure stdlib, and deliberately no module-level pytest import: TestRunnerExt
imports every test_*.py under TD's Python, which has no pytest. gh and git are
replaced by a scripted runner, so every verdict (block, warn, ack, fail-closed)
is pinned without network access (field 2026-09-11).
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

_PATH = Path(__file__).resolve().parents[2] / "release_preflight.py"
_spec = importlib.util.spec_from_file_location("release_preflight", _PATH)
rp = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = rp  # @dataclass resolves its module via sys.modules
_spec.loader.exec_module(rp)

REPO = "owner/repo"


def _run(conclusion="success", status="completed", at="2026-09-11T10:00:00Z"):
    return {"status": status, "conclusion": conclusion, "url": "u", "createdAt": at}


class FakeRunner:
    """Routes each gh/git argv to a scripted response; an Exception value raises."""

    def __init__(self, **overrides):
        self.responses = {
            "dependabot": [], "code-scanning": [], "secret-scanning": [],
            "advisory-triage": [], "advisory-draft": [],
            "prs": [], "workflows": [{"id": 1, "name": "Platform CI", "state": "active"}],
            "runs:main:1": [_run()], "runs:dev:1": [_run()],
            "checks:main": {"check_runs": []}, "checks:dev": {"check_runs": []},
            "release": {"tagName": "v1.0.0", "publishedAt": "2026-09-01T00:00:00Z"},
            "issues": [], "fetch": "", "head": "dev\n", "drift": "",
            "branches": "  origin/HEAD -> origin/main\n  origin/main\n  origin/dev\n",
        }
        self.responses.update(overrides)
        self.calls = []

    def _route(self, cmd):
        joined = " ".join(cmd)
        if cmd[:2] == ["gh", "api"]:
            for kind in ("dependabot", "code-scanning", "secret-scanning"):
                if f"/{kind}/alerts" in joined:
                    return kind
            if "/security-advisories" in joined:
                return "advisory-triage" if "state=triage" in joined else "advisory-draft"
            if "/check-runs" in joined:
                return "checks:" + joined.split("/commits/")[1].split("/")[0]
        if cmd[:3] == ["gh", "pr", "list"]:
            return "prs"
        if cmd[:3] == ["gh", "workflow", "list"]:
            return "workflows"
        if cmd[:3] == ["gh", "run", "list"]:
            return f"runs:{cmd[cmd.index('--branch') + 1]}:{cmd[cmd.index('--workflow') + 1]}"
        if cmd[:3] == ["gh", "release", "view"]:
            return "release"
        if cmd[:3] == ["gh", "issue", "list"]:
            return "issues"
        if cmd[:2] == ["git", "fetch"]:
            return "fetch"
        if cmd[:2] == ["git", "rev-parse"]:
            return "head"
        if cmd[:2] == ["git", "log"]:
            return "drift"
        if cmd[:2] == ["git", "branch"]:
            return "branches"
        raise AssertionError(f"unrouted command: {joined}")

    def __call__(self, cmd):
        self.calls.append(list(cmd))
        val = self.responses.get(self._route(cmd), [])
        if isinstance(val, Exception):
            raise val
        return val if isinstance(val, str) else json.dumps(val)


def _serve_repo(url):
    """A live site that matches the repo: each slug's specimen bytes, LF as git stores them."""
    slug = url.split("/api/specimens/")[1].split("/")[0]
    specs = json.loads((rp.REPO_ROOT / "specimens" / "manifest.json").read_text(encoding="utf-8"))["specimens"]
    rel = next(s["tdxn_path"] for s in specs if s["slug"] == slug)
    return (rp.REPO_ROOT / "specimens" / rel).read_bytes().replace(b"\r\n", b"\n")


def _main(capsys, runner, *extra, fetch=_serve_repo, root=None):
    code = rp.main(["--repo", REPO, *extra], runner=runner, fetch=fetch, root=root or rp.REPO_ROOT)
    return code, capsys.readouterr().out


def _git(root, *args):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.test",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.test"}
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, env=env)


def _specimen_root(tmp_path, blobs, commit=True):
    """A repo root holding specimens/manifest.json and one .tdxn per slug.

    Committed to a real origin/main by default: release mode compares prod
    with what main holds, not with this checkout.
    """
    (tmp_path / "specimens" / "cat").mkdir(parents=True)
    specs = []
    for slug, data in blobs.items():
        (tmp_path / "specimens" / "cat" / f"{slug}.tdxn").write_bytes(data)
        specs.append({"slug": slug, "tdxn_path": f"cat/{slug}.tdxn"})
    (tmp_path / "specimens" / "manifest.json").write_text(json.dumps({"specimens": specs}), encoding="utf-8")
    if commit:
        _git(tmp_path, "init", "-q")
        _git(tmp_path, "add", "-A")
        _git(tmp_path, "commit", "-qm", "specimens")
        _git(tmp_path, "update-ref", "refs/remotes/origin/main", "HEAD")
    return tmp_path


class FakeSite:
    """Serves /api/specimens/<slug>/tdn from a dict; an Exception value raises."""

    def __init__(self, pages):
        self.pages, self.urls = pages, []

    def __call__(self, url):
        self.urls.append(url)
        val = self.pages[url.split("/api/specimens/")[1].split("/")[0]]
        if isinstance(val, Exception):
            raise val
        return val


def test_all_clear_exits_zero(capsys):
    code, out = _main(capsys, FakeRunner())
    assert code == 0
    assert "BLOCKING (0)" in out and "RESULT: CLEAR" in out


def test_open_dependabot_pr_blocks_until_user_acks(capsys):
    pr = {"number": 113, "title": "deps: bump better-auth", "author": {"login": "app/dependabot", "is_bot": True},
          "url": "u", "isDraft": False, "createdAt": "2026-09-10T00:00:00Z"}
    code, out = _main(capsys, FakeRunner(prs=[pr]))
    assert code == 1 and "[pr:113] open Dependabot PR" in out
    code, out = _main(capsys, FakeRunner(prs=[pr]), "--ack", "pr:113")
    assert code == 0 and "ACKED [pr:113]" in out


def test_human_pr_and_issues_warn_but_do_not_block(capsys):
    pr = {"number": 44, "title": "Basic git hooks", "author": {"login": "ulgens"}, "url": "u",
          "isDraft": False, "createdAt": "2026-07-09T00:00:00Z"}
    issues = [{"number": 110, "title": "new bug", "url": "u", "createdAt": "2026-09-11T09:00:00Z"},
              {"number": 36, "title": "old ask", "url": "u", "createdAt": "2026-07-09T00:00:00Z"}]
    code, out = _main(capsys, FakeRunner(prs=[pr], issues=issues))
    assert code == 0
    assert "[pr:44] open PR by ulgens" in out
    assert "#110 NEW since v1.0.0" in out and "#36:" in out and "#36 NEW" not in out


def test_every_open_security_alert_kind_blocks(capsys):
    alert = {"number": 7, "html_url": "u", "security_advisory": {"severity": "high", "summary": "s"},
             "dependency": {"package": {"name": "sharp"}}, "rule": {"id": "r"}, "secret_type_display_name": "t"}
    for kind in ("dependabot", "code-scanning", "secret-scanning"):
        code, out = _main(capsys, FakeRunner(**{kind: [alert]}))
        assert code == 1 and f"[alert:{kind}:7]" in out, kind


def test_private_vulnerability_report_blocks(capsys):
    adv = {"ghsa_id": "GHSA-abcd-efgh-ijkl", "state": "triage", "severity": "high", "summary": "rce", "html_url": "u"}
    code, out = _main(capsys, FakeRunner(**{"advisory-triage": [adv]}))
    assert code == 1 and "[advisory:GHSA-abcd-efgh-ijkl]" in out


def test_alert_and_pr_queries_ask_for_open_items_only(capsys):
    runner = FakeRunner()
    _main(capsys, runner)
    apis = [" ".join(c) for c in runner.calls if c[:2] == ["gh", "api"]]
    for kind in ("dependabot", "code-scanning", "secret-scanning"):
        assert any(f"/{kind}/alerts?state=open" in a for a in apis), kind
    assert any("security-advisories?state=triage" in a for a in apis)
    assert any("security-advisories?state=draft" in a for a in apis)
    pr_call = next(c for c in runner.calls if c[:3] == ["gh", "pr", "list"])
    assert pr_call[pr_call.index("--state") + 1] == "open"


def test_unreachable_or_malformed_alert_feed_fails_closed(capsys):
    # A 403/404 or an odd payload must never read as "zero alerts".
    code, out = _main(capsys, FakeRunner(dependabot=rp.CheckError("gh api failed: HTTP 403")))
    assert code == 1 and "[verify:dependabot]" in out
    code, out = _main(capsys, FakeRunner(**{"secret-scanning": {"message": "Not Found"}}))
    assert code == 1 and "[verify:secret-scanning]" in out


def test_red_latest_ci_blocks_but_a_superseded_failure_does_not(capsys):
    for conclusion in ("failure", "cancelled", "timed_out"):
        code, out = _main(capsys, FakeRunner(**{"runs:main:1": [_run(conclusion)]}))
        assert code == 1 and "[ci:main:Platform CI]" in out, conclusion
    healed = FakeRunner(**{"runs:main:1": [_run("failure", at="2026-09-11T09:00:00Z"),
                                           _run("success", at="2026-09-11T10:00:00Z")]})
    code, _ = _main(capsys, healed)
    assert code == 0


def test_ci_is_queried_per_workflow_and_push_only(capsys):
    runner = FakeRunner()
    _main(capsys, runner)
    run_calls = [c for c in runner.calls if c[:3] == ["gh", "run", "list"]]
    assert {c[c.index("--branch") + 1] for c in run_calls} == {"main", "dev"}
    for c in run_calls:
        assert c[c.index("--event") + 1] == "push" and "--workflow" in c


def test_in_progress_ci_warns_unless_the_last_finished_run_was_red(capsys):
    busy = [_run(conclusion="", status="in_progress", at="2026-09-11T11:00:00Z")]
    code, out = _main(capsys, FakeRunner(**{"runs:dev:1": busy}))
    assert code == 0 and "still in_progress" in out
    code, out = _main(capsys, FakeRunner(**{"runs:dev:1": busy + [_run("failure")]}))
    assert code == 1 and "last finished run is failure" in out


def test_a_branch_with_no_push_runs_fails_closed(capsys):
    code, out = _main(capsys, FakeRunner(**{"runs:dev:1": []}))
    assert code == 1 and "[verify:ci:dev]" in out


def test_failed_third_party_check_on_a_tip_blocks(capsys):
    checks = {"check_runs": [
        {"name": "Workers Builds: x", "conclusion": "failure", "app": {"slug": "cloudflare-workers-and-pages"}, "html_url": "u"},
        {"name": "e2e", "conclusion": "failure", "app": {"slug": "github-actions"}, "html_url": "u"},
    ]}
    code, out = _main(capsys, FakeRunner(**{"checks:main": checks}))
    assert code == 1 and "[check:main:Workers Builds: x]" in out and "[check:main:e2e]" not in out


def test_main_commits_missing_from_the_release_checkout_block(capsys):
    runner = FakeRunner(drift="b20bf5e0 Fix login\n5b12ca0f Fix submit\n")
    code, out = _main(capsys, runner)
    assert code == 1 and "2 commit(s) on main are missing from dev" in out
    log_cmd = next(c for c in runner.calls if c[:2] == ["git", "log"])
    # Right side = main, left = the release checkout; merge commits never count.
    assert log_cmd[-1] == "HEAD...origin/main"
    for flag in ("--no-merges", "--cherry-pick", "--right-only"):
        assert flag in log_cmd, flag


def test_merged_remote_branch_warns(capsys):
    code, out = _main(capsys, FakeRunner(branches="  origin/main\n  origin/dev\n  origin/hotfix/x\n"))
    assert code == 0 and "[branch:hotfix/x]" in out and "[branch:dev]" not in out


def test_failed_git_fetch_fails_closed(capsys):
    code, out = _main(capsys, FakeRunner(fetch=rp.CheckError("git fetch failed")))
    assert code == 1 and "[verify:git]" in out


def test_missing_tool_is_a_check_error_not_a_traceback():
    try:
        rp.run(["embody-no-such-tool-xyz", "--version"])
    except rp.CheckError as exc:
        assert "embody-no-such-tool-xyz" in str(exc)
    else:
        raise AssertionError("expected CheckError")


def test_no_release_yet_only_drops_the_new_tags(capsys):
    issues = [{"number": 5, "title": "t", "url": "u", "createdAt": "2026-09-11T00:00:00Z"}]
    code, out = _main(capsys, FakeRunner(release=rp.CheckError("release not found"), issues=issues))
    assert code == 0 and "[issue:5]" in out and "NEW" not in out.split("[issue:5]")[1].split("\n")[0]


def test_ack_that_matches_nothing_is_reported(capsys):
    code, out = _main(capsys, FakeRunner(), "--ack", "pr:999")
    assert code == 0 and "--ack pr:999 matched nothing" in out


# embody.tools served the June specimens for months (field 2026-09-11).
def test_stale_live_specimen_blocks_until_user_acks(capsys, tmp_path):
    root = _specimen_root(tmp_path, {"a": b"new: 1\n", "b": b"same: 1\n"})
    site = FakeSite({"a": b"old: 1\n", "b": b"same: 1\n"})
    code, out = _main(capsys, FakeRunner(), fetch=site, root=root)
    assert code == 1 and "[specimen:a]" in out and "[specimen:b]" not in out
    assert "sync-specimens" in out
    code, out = _main(capsys, FakeRunner(), "--ack", "specimen:a", fetch=site, root=root)
    assert code == 0 and "ACKED [specimen:a]" in out


def test_live_specimens_matching_the_repo_pass(capsys, tmp_path):
    root = _specimen_root(tmp_path, {"a": b"x: 1\n", "b": b"y: 2\n"})
    code, out = _main(capsys, FakeRunner(), fetch=FakeSite({"a": b"x: 1\n", "b": b"y: 2\n"}), root=root)
    assert code == 0 and "specimen" not in out


def test_unreachable_site_fails_closed(capsys, tmp_path):
    root = _specimen_root(tmp_path, {"a": b"x: 1\n"})
    site = FakeSite({"a": rp.CheckError("GET u: HTTP Error 503: Service Unavailable")})
    code, out = _main(capsys, FakeRunner(), fetch=site, root=root)
    assert code == 1 and "[verify:specimen:a]" in out and "503" in out


def test_crlf_checkout_hashes_as_git_stores_it(capsys, tmp_path):
    root = _specimen_root(tmp_path, {"a": b"x: 1\r\ny: 2\r\n"})
    code, _ = _main(capsys, FakeRunner(), fetch=FakeSite({"a": b"x: 1\ny: 2\n"}), root=root)
    assert code == 0


def test_unreadable_specimen_manifest_fails_closed(capsys, tmp_path):
    code, out = _main(capsys, FakeRunner(), fetch=FakeSite({}), root=tmp_path)
    assert code == 1 and "[verify:specimens]" in out


def test_release_compares_prod_with_main_not_the_release_checkout(capsys, tmp_path):
    # A release re-exports the specimens before it merges, so the checkout holds
    # bytes prod cannot serve yet. Comparing against them blocked the very push
    # that would fix it (field 2026-09-12); compare against origin/main instead.
    root = _specimen_root(tmp_path, {"a": b"main: 1\n"})
    (root / "specimens" / "cat" / "a.tdxn").write_bytes(b"release: 2\n")  # uncommitted
    code, out = _main(capsys, FakeRunner(), fetch=FakeSite({"a": b"main: 1\n"}), root=root)
    assert code == 0 and "[specimen:a]" not in out


def test_specimens_only_uses_the_checkout_not_origin_main(capsys, tmp_path):
    # CI runs it ON the deployed commit, often with no origin/main ref at all.
    root = _specimen_root(tmp_path, {"a": b"main: 1\n"})
    (root / "specimens" / "cat" / "a.tdxn").write_bytes(b"deployed: 2\n")
    code = rp.main(["--specimens-only"], runner=FakeRunner(), fetch=FakeSite({"a": b"deployed: 2\n"}), root=root)
    assert code == 0, capsys.readouterr().out
    code = rp.main(["--specimens-only"], runner=FakeRunner(), fetch=FakeSite({"a": b"main: 1\n"}), root=root)
    assert code == 1 and "[specimen:a]" in capsys.readouterr().out


def test_specimens_only_skips_github_and_reads_the_given_site(capsys, tmp_path):
    def no_gh(cmd):
        raise AssertionError(f"--specimens-only ran {cmd}")

    root = _specimen_root(tmp_path, {"a": b"x: 1\n"})
    site = FakeSite({"a": b"x: 1\n"})
    code = rp.main(["--specimens-only", "--site", "http://127.0.0.1:4999"], runner=no_gh, fetch=site, root=root)
    out = capsys.readouterr().out
    assert code == 0 and "RESULT: CLEAR" in out
    assert site.urls[0].startswith("http://127.0.0.1:4999/api/specimens/a/tdn?cb=")
    code = rp.main(["--specimens-only"], runner=no_gh, fetch=FakeSite({"a": b"x: 2\n"}), root=root)
    assert code == 1 and "[specimen:a]" in capsys.readouterr().out


def test_http_get_turns_a_bad_url_into_a_check_error():
    try:
        rp.http_get("not-a-url")
    except rp.CheckError as exc:
        assert "not-a-url" in str(exc)
    else:
        raise AssertionError("expected CheckError")
