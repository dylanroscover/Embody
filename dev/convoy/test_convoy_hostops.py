"""Focused security/contract tests for :mod:`convoy_hostops`.

No test needs a live Convoy host or TouchDesigner.  Exact command construction
uses a recording runner; resource/cancellation behavior uses the real Python
executable as a harmless child process.
"""

import json
import os
import subprocess
import sys
import threading
import time

import pytest

import convoy_hostops as hostops


def result(ok=True, stdout="", stderr="", exit_code=None, code=None, **extra):
    if exit_code is None:
        exit_code = 0 if ok else 1
    value = {
        "ok": ok,
        "code": code or ("ok" if ok else "command_failed"),
        "detail": "operation completed" if ok else "command returned a non-zero exit status",
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "truncated": False,
        "duration_ms": 1,
    }
    value.update(extra)
    return value


class RecordingRunner:
    """Minimal semantic Git/gh double which also records exact invocations."""

    def __init__(self, root, *, config_names=(), helpers=(),
                 scoped_helpers=(), remote_url=None,
                 remotes=("origin",), branches=("main",), gh_json=None):
        self.root = str(root)
        self.config_names = tuple(config_names)
        self.helpers = tuple(helpers)
        # (name, value) records for URL-scoped credential.<url>.helper keys,
        # as `git config --null --get-regexp` would emit them.
        self.scoped_helpers = tuple(scoped_helpers)
        self.remote_url = remote_url or "https://github.com/example/repo.git"
        self.remotes = tuple(remotes)
        self.branches = tuple(branches)
        self.gh_json = {} if gh_json is None else gh_json
        self.calls = []

    @staticmethod
    def _tail(argv, command):
        try:
            index = argv.index(command)
        except ValueError:
            return None
        return argv[index:]

    def run(self, **call):
        self.calls.append(call)
        argv = call["argv"]

        tail = self._tail(argv, "rev-parse")
        if tail == ["rev-parse", "--show-toplevel"]:
            return result(stdout=self.root + "\n")

        tail = self._tail(argv, "config")
        if tail == ["config", "--null", "--name-only", "--list"]:
            return result(stdout="\0".join(self.config_names) +
                           ("\0" if self.config_names else ""))
        if tail == ["config", "--null", "--get-all", "credential.helper"]:
            if not self.helpers:
                return result(False, exit_code=1)
            return result(stdout="\0".join(self.helpers) + "\0")
        if tail and tail[:3] == ["config", "--null", "--get-regexp"]:
            if not self.scoped_helpers:
                return result(False, exit_code=1)
            records = ["%s\n%s" % (name, value)
                       for name, value in self.scoped_helpers]
            return result(stdout="\0".join(records) + "\0")
        if tail and tail[:3] == ["config", "--get-all", "remote.origin.pushurl"]:
            return result(False, exit_code=1)
        if tail and tail[:3] == ["config", "--get-all", "remote.origin.url"]:
            return result(stdout=self.remote_url + "\n")

        tail = self._tail(argv, "remote")
        if tail == ["remote"]:
            return result(stdout="\n".join(self.remotes) +
                           ("\n" if self.remotes else ""))

        tail = self._tail(argv, "for-each-ref")
        if tail and tail[-1] == "refs/heads":
            return result(stdout="\n".join(self.branches) +
                           ("\n" if self.branches else ""))

        # A gh structured command is the only path without the Git prefix.
        if argv and argv[0] in {"auth", "repo", "pr", "workflow", "run"}:
            if "--json" in argv:
                return result(stdout=json.dumps(self.gh_json))
            return result(stdout="authenticated\n")
        return result(stdout="done\n")


@pytest.fixture
def root(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    return path


def make_ops(root, runner=None, resolver=None, **kwargs):
    runner = runner or RecordingRunner(root)
    resolver = resolver or (lambda target: str(root) if target == "node-1" else None)
    return hostops.HostOperations(
        resolver,
        git_executable=sys.executable,
        gh_executable=sys.executable,
        shell_executable=sys.executable,
        process_runner=runner,
        safe_state_dir=str(root.parent / "state"),
        **kwargs,
    ), runner


def final_call(runner):
    return runner.calls[-1]


def test_catalog_is_json_safe_and_has_no_destructive_pass_through():
    catalog = hostops.operation_catalog()
    json.dumps(catalog, allow_nan=False)
    assert catalog["git"]["capability"] == "host.git/v1"
    assert catalog["gh"]["capability"] == "host.gh/v1"
    assert not ({"reset", "clean", "rebase", "force_push", "api", "alias"} &
                set(catalog["git"]["operations"]))
    assert not ({"api", "extension", "auth_login", "pr_merge"} &
                set(catalog["gh"]["operations"]))


def test_unknown_git_operation_never_starts_a_process(root):
    audit = []
    ops, runner = make_ops(root, audit_callback=audit.append)
    value = ops.run_git("node-1", "git", {"argv": ["reset", "--hard"]})
    assert value["code"] == "unknown_operation"
    assert value["operation"] is None
    assert runner.calls == []
    assert audit == [{
        "event": "host_operation_refused", "capability": "host.git/v1",
        "operation": None, "target_id": "node-1", "code": "unknown_operation"}]


def test_git_status_has_exact_reviewed_argv_and_registry_cwd(root):
    ops, runner = make_ops(root)
    value = ops.run_git("node-1", "status")
    assert value["ok"]
    call = final_call(runner)
    assert call["cwd"] == os.path.realpath(str(root))
    assert call["argv"][-4:] == [
        "status", "--porcelain=v2", "--branch", "--untracked-files=normal"]
    assert "--no-pager" in call["argv"]
    assert any(arg.startswith("core.hooksPath=") for arg in call["argv"])
    assert "shell" not in call                 # the runner API has no shell field


def test_fetch_builds_flags_from_typed_fields_only(root):
    ops, runner = make_ops(root)
    value = ops.run_git("node-1", "fetch", {
        "remote": "origin", "prune": True, "tags": False})
    assert value["ok"]
    assert final_call(runner)["argv"][-6:] == [
        "fetch", "--no-recurse-submodules", "--prune", "--no-tags",
        "--", "origin"]


def test_push_is_same_branch_without_force_or_delete(root):
    ops, runner = make_ops(root)
    value = ops.run_git("node-1", "push_branch", {
        "remote": "origin", "branch": "main"})
    assert value["ok"]
    tail = final_call(runner)["argv"][-5:]
    assert tail == ["push", "--porcelain", "--", "origin",
                    "refs/heads/main:refs/heads/main"]
    assert all("force" not in arg for arg in tail)
    assert not tail[-1].startswith(":")


@pytest.mark.parametrize("operation,arguments", [
    ("fetch", {"remote": "origin", "force": True}),
    ("push_branch", {"remote": "origin", "branch": "main", "force": True}),
    ("pull_ff_only", {"remote": "origin", "branch": "main", "rebase": True}),
    ("status", {"porcelain": False}),
])
def test_unknown_git_fields_are_fail_closed(root, operation, arguments):
    ops, runner = make_ops(root)
    value = ops.run_git("node-1", operation, arguments)
    assert value["code"] == "invalid_arguments"
    # Probes may have run, but the requested operation was never dispatched.
    assert not any(call["argv"] and call["argv"][-1] in {"origin", "main"}
                   for call in runner.calls)


@pytest.mark.parametrize("url", [
    "ext::sh -c pwn", "ssh://git@example.com/repo", "git@example.com:repo",
    "file:///tmp/repo", "http://example.com/repo", "git://example.com/repo",
    "https://user:password@example.com/repo",
    "https://example.com/repo?access_token=topsecret",
])
def test_network_git_rejects_unreviewed_or_credential_urls(root, url):
    runner = RecordingRunner(root, remote_url=url)
    ops, _ = make_ops(root, runner=runner)
    value = ops.run_git("node-1", "fetch", {"remote": "origin"})
    assert value["code"] == "unsafe_remote"
    assert url not in json.dumps(value)


def test_missing_remote_and_branch_have_stable_codes(root):
    runner = RecordingRunner(root, remotes=())
    ops, _ = make_ops(root, runner=runner)
    assert ops.run_git("node-1", "fetch", {"remote": "origin"})["code"] == "remote_missing"

    runner = RecordingRunner(root, branches=("dev",))
    ops, _ = make_ops(root, runner=runner)
    assert ops.run_git("node-1", "push_branch", {
        "remote": "origin", "branch": "main"})["code"] == "ref_missing"


@pytest.mark.parametrize("helper", ["!sh -c pwn", "/tmp/helper", "custom", "manager --arg"])
def test_network_git_rejects_arbitrary_credential_helpers(root, helper):
    runner = RecordingRunner(root, helpers=(helper,))
    ops, _ = make_ops(root, runner=runner)
    value = ops.run_git("node-1", "fetch", {"remote": "origin"})
    assert value["code"] == "unsafe_repository_config"
    assert helper not in json.dumps(value)


@pytest.mark.parametrize("helper", ["manager", "manager-core", "osxkeychain", "libsecret"])
def test_reviewed_os_credential_helpers_are_allowed(root, helper):
    runner = RecordingRunner(root, helpers=(helper,))
    ops, _ = make_ops(root, runner=runner)
    assert ops.run_git("node-1", "fetch", {"remote": "origin"})["ok"]


@pytest.mark.parametrize("helper", ["!sh -c pwn", "/tmp/helper", "custom", "manager --arg"])
def test_network_git_rejects_url_scoped_credential_helpers(root, helper):
    # A URL-scoped credential.<url>.helper is the SAME shell-injection vector
    # as the bare key; the preflight must validate it too (review 2026-08-02).
    runner = RecordingRunner(root, scoped_helpers=(
        ("credential.https://github.com.helper", helper),))
    ops, _ = make_ops(root, runner=runner)
    value = ops.run_git("node-1", "fetch", {"remote": "origin"})
    assert value["code"] == "unsafe_repository_config"
    assert helper not in json.dumps(value)


def test_url_scoped_reviewed_helper_is_allowed(root):
    runner = RecordingRunner(root, scoped_helpers=(
        ("credential.https://github.com.helper", "manager"),))
    ops, _ = make_ops(root, runner=runner)
    assert ops.run_git("node-1", "fetch", {"remote": "origin"})["ok"]


@pytest.mark.parametrize("key", [
    "core.sshCommand", "core.gitProxy", "remote.origin.uploadPack",
    "remote.origin.receivePack", "remote.origin.proxy", "remote.origin.vcs",
    "url.ext::.insteadOf", "url.ssh://.pushInsteadOf", "submodule.x.update",
    "submodule.x.url",
])
def test_network_git_rejects_executable_config_escape_surfaces(root, key):
    runner = RecordingRunner(root, config_names=(key,))
    ops, _ = make_ops(root, runner=runner)
    value = ops.run_git("node-1", "fetch", {"remote": "origin"})
    assert value["code"] == "unsafe_repository_config"


@pytest.mark.parametrize("key", [
    "filter.media.clean", "filter.media.smudge", "filter.media.process",
    "filter.media.required",
])
def test_worktree_touching_git_refuses_executable_filters(root, key):
    runner = RecordingRunner(root, config_names=(key,))
    ops, _ = make_ops(root, runner=runner)
    assert ops.run_git("node-1", "pull_ff_only", {
        "remote": "origin", "branch": "main"})["code"] == "unsafe_repository_config"
    assert ops.run_git("node-1", "status")["code"] == "unsafe_repository_config"
    # Operations which cannot touch worktree content remain available.
    assert ops.run_git("node-1", "revision")["ok"]


def test_local_policy_can_only_narrow_reviewed_git_helpers_and_protocols(root):
    with pytest.raises(ValueError):
        make_ops(root, allowed_remote_schemes={"https", "ext"})
    with pytest.raises(ValueError):
        make_ops(root, allowed_credential_helpers={"manager", "custom"})
    ops, _ = make_ops(root, allowed_remote_schemes=set(),
                      allowed_credential_helpers=set())
    assert ops.run_git("node-1", "fetch", {"remote": "origin"})["code"] == "unsafe_remote"


def test_repository_must_equal_registered_root(root):
    actual = root.parent
    runner = RecordingRunner(actual)
    ops, _ = make_ops(root, runner=runner)
    assert ops.run_git("node-1", "status")["code"] == "repository_required"


def test_real_git_status_and_config_preflight_work_end_to_end(tmp_path):
    git = __import__("shutil").which("git")
    if not git:
        pytest.skip("git is not installed")
    repo = tmp_path / "actual-repo"
    repo.mkdir()
    subprocess.run([git, "init", "--quiet", str(repo)], check=True,
                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                   stderr=subprocess.PIPE, shell=False)
    isolated_home = tmp_path / "home"
    isolated_home.mkdir()
    environment = {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": str(isolated_home),
        "USERPROFILE": str(isolated_home),
    }
    for key in ("SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP"):
        if key in os.environ:
            environment[key] = os.environ[key]
    ops = hostops.HostOperations(
        lambda target: str(repo) if target == "node-1" else None,
        git_executable=git, environment=environment,
        safe_state_dir=str(tmp_path / "state"))
    value = ops.run_git("node-1", "status")
    assert value["ok"], value
    assert value["stdout"].startswith("# branch.oid")

    subprocess.run([git, "-C", str(repo), "config", "filter.attack.process",
                    "not-a-real-filter --serve"], check=True, shell=False)
    assert ops.run_git("node-1", "status")["code"] == "unsafe_repository_config"
    assert ops.run_git("node-1", "revision")["code"] == "command_failed"


def test_target_remap_is_detected_before_dispatch(root, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    values = iter((str(root), str(other)))
    ops, runner = make_ops(root, resolver=lambda _target: next(values))
    value = ops.run_git("node-1", "status")
    assert value["code"] == "target_changed"
    assert final_call(runner)["argv"][-2:] != ["--branch", "--untracked-files=normal"]


def test_structured_path_drops_empty_relative_and_worktree_entries(root):
    executable_dir = os.path.dirname(sys.executable)
    hostile = os.pathsep.join(("", ".", str(root), executable_dir, ""))
    runner = RecordingRunner(root)
    ops, _ = make_ops(root, runner=runner, environment={"PATH": hostile})
    assert ops.run_git("node-1", "status")["ok"]
    passed = final_call(runner)["environment"]["PATH"].split(os.pathsep)
    assert "" not in passed and "." not in passed
    assert os.path.realpath(str(root)) not in passed
    assert os.path.realpath(executable_dir) in passed


def test_structured_executable_may_not_live_in_the_worktree_or_be_a_batch_file(root):
    inside = root / "fake-git.exe"
    inside.write_bytes(b"not executable")
    runner = RecordingRunner(root)
    ops = hostops.HostOperations(
        lambda _target: str(root), git_executable=str(inside),
        process_runner=runner, safe_state_dir=str(root.parent / "state-a"))
    assert ops.run_git("node-1", "status")["code"] == "command_refused"
    assert runner.calls == []

    batch = root.parent / "fake-git.cmd"
    batch.write_text("@exit /b 0", encoding="utf-8")
    ops = hostops.HostOperations(
        lambda _target: str(root), git_executable=str(batch), platform="win32",
        process_runner=runner, safe_state_dir=str(root.parent / "state-b"))
    assert ops.run_git("node-1", "status")["code"] == "command_refused"
    assert runner.calls == []


def test_mutating_git_operations_serialize_per_canonical_worktree(root):
    entered = threading.Event()
    release = threading.Event()

    class BlockingRunner(RecordingRunner):
        def run(self, **call):
            if "fetch" in call["argv"]:
                self.calls.append(call)
                entered.set()
                assert release.wait(5)
                return result(stdout="done\n")
            return super().run(**call)

    runner = BlockingRunner(root)
    ops, _ = make_ops(root, runner=runner)
    outcomes = []

    def invoke():
        outcomes.append(ops.run_git("node-1", "fetch", {"remote": "origin"}))

    first = threading.Thread(target=invoke)
    second = threading.Thread(target=invoke)
    first.start()
    assert entered.wait(5)
    second.start()
    time.sleep(0.1)
    assert sum("fetch" in call["argv"] for call in runner.calls) == 1
    release.set()
    first.join(5)
    second.join(5)
    assert len(outcomes) == 2 and all(value["ok"] for value in outcomes)
    assert sum("fetch" in call["argv"] for call in runner.calls) == 2


def test_full_shell_is_disabled_without_a_local_policy_callback(root):
    ops, runner = make_ops(root)
    value = ops.run_shell("node-1", "echo should-not-run")
    assert value["code"] == "shell_disabled"
    assert runner.calls == []


@pytest.mark.parametrize("truthy", [1, "yes", object()])
def test_full_shell_policy_requires_literal_true(root, truthy):
    ops, runner = make_ops(root, full_shell_policy=lambda _target: truthy)
    assert ops.run_shell("node-1", "echo nope")["code"] == "shell_disabled"
    assert runner.calls == []


def test_full_shell_policy_is_checked_twice_and_can_narrow(root):
    answers = iter((True, False))
    ops, runner = make_ops(root, full_shell_policy=lambda _target: next(answers))
    value = ops.run_shell("node-1", "echo nope")
    assert value["code"] == "shell_disabled"
    assert runner.calls == []


def test_full_shell_uses_registered_relative_cwd_and_does_not_audit_command(root):
    child = root / "sub"
    child.mkdir()
    audit = []
    ops, runner = make_ops(
        root, full_shell_policy=lambda _target: True, audit_callback=audit.append,
        platform="linux")
    command = "echo SUPER_PRIVATE_COMMAND_8675309"
    value = ops.run_shell("node-1", command, cwd="sub")
    assert value["ok"] and value["cwd"] == "sub"
    call = final_call(runner)
    assert call["cwd"] == os.path.realpath(str(child))
    assert call["argv"] == ["-c", command]
    assert command not in json.dumps(value)
    assert command not in json.dumps(audit)


@pytest.mark.parametrize("cwd", ["..", "../outside", "/tmp", "C:\\Windows", "\\\\server\\share"])
def test_full_shell_cwd_cannot_escape_registered_worktree(root, cwd):
    ops, runner = make_ops(root, full_shell_policy=lambda _target: True)
    value = ops.run_shell("node-1", "echo nope", cwd=cwd)
    assert value["code"] == "worktree_escape"
    assert runner.calls == []


def test_full_shell_symlink_escape_is_refused(root, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    link = root / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation unavailable on this host")
    ops, runner = make_ops(root, full_shell_policy=lambda _target: True)
    assert ops.run_shell("node-1", "echo nope", cwd="link")["code"] == "worktree_escape"
    assert runner.calls == []


@pytest.mark.parametrize("platform,prefix", [
    ("win32", ["-NoLogo", "-NoProfile", "-NonInteractive", "-Command"]),
    ("darwin", ["-f", "-c"]),
    ("linux", ["-c"]),
])
def test_full_shell_platform_argv_is_explicit(root, platform, prefix):
    ops, runner = make_ops(root, full_shell_policy=lambda _target: True,
                           platform=platform)
    assert ops.run_shell("node-1", "echo hello")["ok"]
    assert final_call(runner)["argv"] == prefix + ["echo hello"]


@pytest.mark.parametrize("key", [
    "PATH", "COMSPEC", "SHELL", "PYTHONPATH", "LD_PRELOAD", "DYLD_INSERT_LIBRARIES",
    "GIT_CONFIG_GLOBAL", "GH_TOKEN", "CONVOY_HOST_TOKEN",
])
def test_full_shell_refuses_process_control_environment_additions(root, key):
    ops, runner = make_ops(root, full_shell_policy=lambda _target: True)
    value = ops.run_shell("node-1", "echo nope", env_additions={key: "value"})
    assert value["code"] == "invalid_environment"
    assert runner.calls == []


def test_full_shell_secret_environment_is_marked_for_output_redaction(root):
    ops, runner = make_ops(root, full_shell_policy=lambda _target: True)
    secret = "local-secret-8675309"
    assert ops.run_shell("node-1", "echo ok", env_additions={
        "SERVICE_TOKEN": secret})["ok"]
    call = final_call(runner)
    assert call["environment"]["SERVICE_TOKEN"] == secret
    assert secret in call["secret_values"]


def test_every_explicit_environment_value_is_secret_and_redaction_is_bounded(root):
    ops, runner = make_ops(root, full_shell_policy=lambda _target: True)
    cookie = "opaque-cookie-8675309"
    assert ops.run_shell("node-1", "echo ok", env_additions={
        "COOKIE": cookie})["ok"]
    assert cookie in final_call(runner)["secret_values"]

    too_many = ["secret-%03d" % index for index in range(hostops.MAX_REDACT_VALUES + 1)]
    runner.calls.clear()
    value = ops.run_shell("node-1", "echo nope", redact_values=too_many)
    assert value["code"] == "invalid_arguments"
    assert runner.calls == []


def test_gh_uses_fixed_json_fields_and_parses_result(root):
    payload = [{"number": 7, "title": "safe"}]
    runner = RecordingRunner(root, gh_json=payload)
    ops, _ = make_ops(root, runner=runner)
    value = ops.run_gh("node-1", "pr_list", {"limit": 10, "state": "open"})
    assert value["ok"] and value["data"] == payload
    assert final_call(runner)["argv"] == [
        "pr", "list", "--limit", "10", "--state", "open", "--json",
        "number,title,state,headRefName,baseRefName,url"]


def test_repo_bound_gh_refuses_a_registered_subdirectory_of_parent_repo(root):
    nested = root / "project"
    nested.mkdir()
    runner = RecordingRunner(root)       # Git reports the parent as toplevel
    ops, _ = make_ops(nested, runner=runner)
    value = ops.run_gh("node-1", "repo_view")
    assert value["code"] == "repository_required"
    assert not any(call["argv"] and call["argv"][0] == "repo"
                   for call in runner.calls)
    # Authentication status is intentionally host-scoped and repo-independent.
    assert ops.run_gh("node-1", "auth_status")["ok"]


@pytest.mark.parametrize("operation,arguments", [
    ("pr_list", {"limit": 1000}),
    ("pr_list", {"state": "--web"}),
    ("pr_view", {"number": "1"}),
    ("run_view", {"run_id": -1}),
    ("run_list", {"status": "--repo"}),
    ("repo_view", {"repo": "owner/other"}),
    ("api", {"path": "/user"}),
])
def test_gh_rejects_untyped_repo_flags_and_pass_through(root, operation, arguments):
    ops, runner = make_ops(root)
    value = ops.run_gh("node-1", operation, arguments)
    assert value["code"] in {"unknown_operation", "invalid_arguments"}
    assert runner.calls == []


def test_gh_token_is_inherited_only_for_gh_and_registered_as_secret(root):
    runner = RecordingRunner(root)
    ops = hostops.HostOperations(
        lambda _target: str(root), gh_executable=sys.executable,
        git_executable=sys.executable, process_runner=runner,
        environment={"PATH": os.environ.get("PATH", ""), "GH_TOKEN": "ghp_abcdefghijklmnop"},
        safe_state_dir=str(root.parent / "state"))
    assert ops.run_gh("node-1", "auth_status")["ok"]
    call = final_call(runner)
    assert call["environment"]["GH_TOKEN"] == "ghp_abcdefghijklmnop"
    assert "ghp_abcdefghijklmnop" in call["secret_values"]

    runner.calls.clear()
    assert ops.run_git("node-1", "status")["ok"]
    assert "GH_TOKEN" not in final_call(runner)["environment"]


def test_gh_success_with_non_json_output_fails_closed(root):
    runner = RecordingRunner(root)
    runner.gh_json = object()             # json.dumps in the double will raise

    def broken(**call):
        runner.calls.append(call)
        if "rev-parse" in call["argv"] and call["argv"][-1] == "--show-toplevel":
            return result(stdout=str(root) + "\n")
        return result(stdout="not-json")

    runner.run = broken
    ops, _ = make_ops(root, runner=runner)
    value = ops.run_gh("node-1", "repo_view")
    assert value["code"] == "invalid_command_output"


def real_runner(**kwargs):
    runner = hostops.ProcessRunner(max_concurrent=2)
    base = {
        "executable": sys.executable,
        "cwd": os.getcwd(),
        "environment": hostops._safe_environment(os.environ),
        "timeout_s": 5,
        "output_limit": 4096,
    }
    base.update(kwargs)
    return runner.run(**base)


def test_process_runner_exact_argv_does_not_interpret_shell_metacharacters():
    marker = "literal;echo SHOULD_NOT_RUN && dir"
    value = real_runner(argv=["-c", "import sys; print(sys.argv[1])", marker])
    assert value["ok"]
    assert value["stdout"].strip() == marker
    assert "SHOULD_NOT_RUN\n" not in value["stdout"]


def test_process_runner_bounds_combined_output_while_draining_child():
    value = real_runner(
        argv=["-c", "import sys; sys.stdout.write('a'*20000); sys.stderr.write('b'*20000)"],
        output_limit=4096,
    )
    assert value["ok"] and value["truncated"]
    assert len(value["stdout"].encode()) + len(value["stderr"].encode()) <= 4096
    assert value["observed_bytes"] == {"stdout": 20000, "stderr": 20000}
    assert "output_sha256" not in value


def test_process_runner_timeout_is_bounded_and_kills_the_process():
    started = time.monotonic()
    value = real_runner(argv=["-c", "import time; time.sleep(30)"], timeout_s=0.15)
    assert value["code"] == "timeout"
    assert time.monotonic() - started < 5


def test_process_runner_honors_preexisting_cancellation_without_spawn():
    event = threading.Event()
    event.set()
    value = real_runner(argv=["-c", "raise SystemExit('must not run')"],
                        cancel_event=event)
    assert value["code"] == "cancelled"


def test_process_runner_redacts_known_and_pattern_secrets_from_both_streams():
    secret = "s3cr3t-8675309"
    code = (
        "import sys; print(%r); "
        "sys.stderr.write('https://user:password@example.com/x ghp_abcdefghijklmnop')"
    ) % secret
    value = real_runner(argv=["-c", code], secret_values=[secret])
    serialized = json.dumps(value)
    assert secret not in serialized
    assert "password" not in serialized
    assert "ghp_abcdefghijklmnop" not in serialized
    assert "REDACTED" in serialized


def test_process_start_errors_never_echo_exception_or_executable():
    secret_path = os.path.join(str(os.getcwd()), "SUPER_SECRET_EXECUTABLE")

    def fail(_argv, **_kwargs):
        raise OSError("failed to launch " + secret_path + " token=abc123")

    runner = hostops.ProcessRunner(popen_factory=fail)
    value = runner.run(
        executable=secret_path, argv=[], cwd=os.getcwd(),
        environment={"PATH": os.defpath}, timeout_s=1, output_limit=4096)
    assert value["code"] == "command_refused"
    assert secret_path not in json.dumps(value)
    assert "abc123" not in json.dumps(value)


def test_process_runner_rejects_nan_deadline_and_oversized_environment():
    runner = hostops.ProcessRunner()
    value = runner.run(
        executable=sys.executable, argv=["-c", "print('x')"], cwd=os.getcwd(),
        environment={"PATH": os.defpath}, timeout_s=float("nan"), output_limit=4096)
    assert value["code"] == "invalid_arguments"
    value = runner.run(
        executable=sys.executable, argv=["-c", "print('x')"], cwd=os.getcwd(),
        environment={"BAD": "x\0y"}, timeout_s=1, output_limit=4096)
    assert value["code"] == "invalid_environment"


def test_platform_process_group_contract_is_testable_on_any_os():
    win = hostops._popen_platform_kwargs("win32")
    assert win["creationflags"] & 0x00000200       # CREATE_NEW_PROCESS_GROUP
    assert win["creationflags"] & 0x08000000       # CREATE_NO_WINDOW
    assert hostops._popen_platform_kwargs("darwin") == {"start_new_session": True}
    assert hostops._popen_platform_kwargs("linux") == {"start_new_session": True}


def test_windows_job_failure_falls_back_to_absolute_system32_taskkill(monkeypatch):
    class Job:
        def terminate(self):
            raise OSError("job termination failed")

    class Process:
        pid = 42

        def kill(self):
            self.killed = True

    process = Process()
    invocations = []
    absolute = r"C:\Windows\System32\taskkill.exe"
    monkeypatch.setattr(hostops.sys, "platform", "win32")
    monkeypatch.setattr(hostops, "_windows_taskkill_path", lambda: absolute)
    monkeypatch.setattr(hostops.subprocess, "run",
                        lambda argv, **kwargs: invocations.append((argv, kwargs)))
    hostops.ProcessRunner(platform="win32")._terminate_tree(process, Job())
    assert invocations[0][0] == [absolute, "/PID", "42", "/T", "/F"]
    assert invocations[0][1]["shell"] is False
    assert process.killed is True


def test_windows_job_wrapper_treats_false_termination_as_failure():
    class Kernel:
        @staticmethod
        def TerminateJobObject(_handle, _status):
            return 0

    job = object.__new__(hostops._WindowsJob)
    job._handle = 1
    job._kernel32 = Kernel()
    with pytest.raises(OSError):
        job.terminate()


def test_taskkill_path_never_uses_path_search_or_relative_system_root():
    expected = r"C:\Windows\System32\taskkill.exe"
    assert hostops._windows_taskkill_path(
        {"SystemRoot": r"C:\Windows"}, isfile=lambda path: path == expected) == expected
    assert hostops._windows_taskkill_path(
        {"SystemRoot": "Windows"}, isfile=lambda _path: True) is None


def test_posix_termination_escalates_for_children_after_leader_exits(monkeypatch):
    calls = []

    class Process:
        pid = 321

        def wait(self, timeout=None):
            return 0

        def terminate(self):
            calls.append(("terminate", None))

        def kill(self):
            calls.append(("kill", None))

    def killpg(pid, sig):
        calls.append((pid, sig))

    moments = iter((0.0, 1.0))
    monkeypatch.setattr(hostops.os, "killpg", killpg, raising=False)
    monkeypatch.setattr(hostops.time, "monotonic", lambda: next(moments))
    hostops.ProcessRunner(platform="darwin")._terminate_tree(Process(), None)
    assert calls == [(321, hostops._SIGTERM), (321, hostops._SIGKILL)]


def test_all_public_results_are_json_safe(root):
    ops, _ = make_ops(root)
    values = [
        ops.run_git("bad target!", "status"),
        ops.run_gh("node-1", "api"),
        ops.run_shell("node-1", "echo nope"),
    ]
    for value in values:
        json.dumps(value, ensure_ascii=False, allow_nan=False)
        assert isinstance(value["code"], str)
        assert isinstance(value["detail"], str)
    assert values[0]["target_id"] is None       # invalid input is not reflected
    assert values[1]["operation"] is None


def test_huge_secret_invalid_identifiers_are_never_reflected(root):
    secret = "ghp_" + "x" * 100000
    ops, runner = make_ops(root)
    value = ops.run_git(secret, secret)
    encoded = json.dumps(value)
    assert secret not in encoded
    assert len(encoded) < 1024
    assert value["target_id"] is None and value["operation"] is None
    assert runner.calls == []


def test_samefile_identity_handles_platform_aliases(monkeypatch):
    monkeypatch.setattr(hostops.os.path, "samefile",
                        lambda left, right: {str(left), str(right)} == {
                            "/Volumes/Data/Project", "/volumes/data/project"})
    assert hostops._paths_same("/Volumes/Data/Project", "/volumes/data/project")
