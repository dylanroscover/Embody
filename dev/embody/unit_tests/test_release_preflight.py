"""release_preflight: the GitHub outstanding-items gate at the top of the /release flow.

Pure stdlib, and deliberately no module-level pytest import: TestRunnerExt
imports every test_*.py under TD's Python, which has no pytest. gh and git are
replaced by a scripted runner, so every verdict (block, warn, ack, fail-closed)
is pinned without network access (field 2026-09-11).
"""
from __future__ import annotations

import importlib.util
import json
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


def _main(capsys, runner, *extra):
    code = rp.main(["--repo", REPO, *extra], runner=runner)
    return code, capsys.readouterr().out


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
