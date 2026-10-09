"""Host-native Convoy operation composition.

The subprocess implementation has its own adversarial suite.  These tests pin
the boundaries that only HostApp can enforce: durable admission, namespace and
policy gates, no TouchDesigner wake/forward, cancellation, provenance, and
large-result artifact spill.
"""

import json
import threading

import convoy_hostapp as hostapp


def _app_and_node(tmp_path, *, perform=False):
    project = tmp_path / "project"
    project.mkdir()
    forwarded = []
    woken = []
    app = hostapp.HostApp(
        str(tmp_path / "state"),
        forwarder=lambda *args: forwarded.append(args),
        waker=lambda *args: woken.append(args))
    code, body = app.register_node({
        "project_root": str(project),
        "convoy_id": "studio",
        "comp_path": "/Embody",
        "runtime_id": "rt_hostops",
        "perform_mode": bool(perform),
        "envoy_ready": False,
    })
    assert code == 200, body
    return app, body, project, forwarded, woken


def _enable_full_shell(app):
    generation = app.policy.generation
    challenge = app.policy.begin_enable_full_shell(
        expected_generation=generation)
    app.policy.confirm_enable(
        challenge["challenge_id"], challenge["confirmation"],
        expected_generation=generation)


def _create(app, node, operation, arguments, key="hostop"):
    code, body = app.create_job({
        "idempotency_key": key,
        "node_id": node["node_id"],
        "operation": operation,
        "arguments": arguments,
    })
    assert code == 200, body
    return body["job"]


class _FakeHostOperations:
    def __init__(self, result=None):
        self.calls = []
        self.result = result or {
            "ok": True, "code": "ok", "detail": "operation completed",
            "stdout": "clean", "stderr": "", "exit_code": 0,
        }

    def run_git(self, target_id, operation, arguments, **kwargs):
        self.calls.append(("git", target_id, operation, arguments, kwargs))
        return dict(self.result)

    def run_gh(self, target_id, operation, arguments, **kwargs):
        self.calls.append(("gh", target_id, operation, arguments, kwargs))
        return dict(self.result)

    def run_shell(self, target_id, command, **kwargs):
        self.calls.append(("shell", target_id, command, kwargs))
        return dict(self.result)


def test_host_git_dispatch_is_durable_and_never_wakes_or_forwards_td(tmp_path):
    app, node, _project, forwarded, woken = _app_and_node(
        tmp_path, perform=True)
    fake = _FakeHostOperations()
    app.host_operations = fake
    job = _create(app, node, "convoy_git", {
        "operation": "status", "arguments": {}}, key="git-status")

    code, body = app.dispatch_job(job["delivery_id"])

    assert code == 200 and body["job"]["state"] == "succeeded"
    assert body["job"]["verdict_source"] == "host_operation"
    assert fake.calls[0][:4] == ("git", node["node_id"], "status", {})
    assert forwarded == []
    assert woken == []


def test_git_mutation_gate_is_action_sensitive_and_manifested():
    read = hostapp.effective_operation_gating(
        hostapp.PHASE1_OPERATIONS, "convoy_git",
        {"operation": "status", "arguments": {}})
    write = hostapp.effective_operation_gating(
        hostapp.PHASE1_OPERATIONS, "convoy_git",
        {"operation": "fetch", "arguments": {"remote": "origin"}})
    assert read["mutating"] is False
    assert write["mutating"] is True
    assert {"convoy_git", "convoy_gh", "convoy_shell"} <= set(
        hostapp.PHASE1_OPERATIONS)
    assert hostapp.HOST_SUBPROCESS_OPERATIONS.isdisjoint(
        {"execute_python", "query_network"})


def test_unknown_host_action_and_wrapper_field_refuse_before_persistence(
        tmp_path):
    app, node, _project, _forwarded, _woken = _app_and_node(tmp_path)
    for key, arguments in (
        ("unknown", {"operation": "reset_hard"}),
        ("extra", {"operation": "status", "argv": ["--porcelain"]}),
    ):
        code, body = app.create_job({
            "idempotency_key": key, "node_id": node["node_id"],
            "operation": "convoy_git", "arguments": arguments})
        assert code in (400, 403)
        assert body["reason"] in {
            "host_operation_not_exposed", "malformed"}
    assert app.db.jobs() == []


def test_shell_requires_host_private_policy_and_rechecks_withdrawal(tmp_path):
    app, node, _project, forwarded, woken = _app_and_node(tmp_path)
    fake = _FakeHostOperations()
    app.host_operations = fake
    request = {
        "idempotency_key": "shell-off", "node_id": node["node_id"],
        "operation": "convoy_shell", "arguments": {"command": "pwd"}}
    code, body = app.create_job(request)
    assert code == 403 and body["reason"] == "full_shell_not_approved"

    _enable_full_shell(app)
    job = _create(app, node, "convoy_shell", {"command": "pwd"},
                  key="shell-on")
    app.policy.disable_full_shell()
    code, body = app.dispatch_job(job["delivery_id"])
    assert code == 403 and body["reason"] == "full_shell_not_approved"
    assert body["job"]["state"] == "refused"
    assert fake.calls == [] and forwarded == [] and woken == []


def test_namespace_change_refuses_before_host_invocation(tmp_path):
    app, node, _project, _forwarded, _woken = _app_and_node(tmp_path)
    fake = _FakeHostOperations()
    app.host_operations = fake
    job = _create(app, node, "convoy_gh", {
        "operation": "auth_status"}, key="namespace")
    app.directory.lookup(node["node_id"])["convoy_id"] = "other-studio"

    code, body = app.dispatch_job(job["delivery_id"])

    assert code == 403 and body["reason"] == "namespace_mismatch"
    assert fake.calls == []


class _CancellableHostOperations(_FakeHostOperations):
    def __init__(self):
        super().__init__()
        self.started = threading.Event()

    def run_shell(self, target_id, command, **kwargs):
        self.calls.append(("shell", target_id, command, kwargs))
        cancel_event = kwargs["cancel_event"]
        self.started.set()
        assert cancel_event.wait(5), "test cancellation did not arrive"
        return {"ok": False, "code": "cancelled",
                "detail": "operation was cancelled", "stdout": "",
                "stderr": "", "exit_code": -1}


def test_inflight_host_job_cancellation_reaches_process_event(tmp_path):
    app, node, _project, forwarded, woken = _app_and_node(
        tmp_path, perform=True)
    _enable_full_shell(app)
    fake = _CancellableHostOperations()
    app.host_operations = fake
    job = _create(app, node, "convoy_shell", {"command": "long"},
                  key="cancel")
    answer = []
    thread = threading.Thread(
        target=lambda: answer.append(app.dispatch_job(job["delivery_id"])))
    thread.start()
    assert fake.started.wait(5)

    code, body = app.cancel_host_job({"delivery_id": job["delivery_id"]})
    assert code == 202 and body["cancel_requested"] is True
    thread.join(5)

    assert not thread.is_alive()
    assert answer[0][1]["job"]["state"] == "failed"
    assert answer[0][1]["job"]["result"]["code"] == "cancelled"
    assert job["delivery_id"] not in app._hostop_cancel_events
    assert forwarded == [] and woken == []


def test_queued_host_job_cancellation_is_definitive(tmp_path):
    app, node, _project, _forwarded, _woken = _app_and_node(tmp_path)
    job = _create(app, node, "convoy_git", {"operation": "status"},
                  key="cancel-queued")
    code, body = app.cancel_host_job({"delivery_id": job["delivery_id"]})
    assert code == 200 and body["cancelled"] is True
    assert body["definitive"] is True
    assert body["job"]["state"] == "refused"


def test_large_host_result_spills_to_private_artifact(tmp_path):
    app, node, _project, forwarded, woken = _app_and_node(tmp_path)
    fake = _FakeHostOperations({
        "ok": True, "code": "ok", "detail": "operation completed",
        "stdout": "x" * (80 * 1024), "stderr": "", "exit_code": 0})
    app.host_operations = fake
    job = _create(app, node, "convoy_gh", {"operation": "auth_status"},
                  key="spill")

    code, body = app.dispatch_job(job["delivery_id"])

    result = body["job"]["result"]
    assert code == 200 and result["spilled"] is True
    assert len(json.dumps(result).encode("utf-8")) < 64 * 1024
    reference = result["artifact"]
    described = app.artifacts.describe(
        "studio", reference["artifact_id"], verify=True)
    assert described["size"] > 80 * 1024
    assert described["mime_type"] == "application/json"
    assert forwarded == [] and woken == []


def test_secret_safe_host_audit_drops_command_environment_and_output(tmp_path):
    app, _node, _project, _forwarded, _woken = _app_and_node(tmp_path)
    secret = "owk_live_SUPER_SECRET_VALUE"
    app._audit_host_operation({
        "event": "host_operation_started",
        "capability": "host.shell/v1",
        "operation": "execute",
        "target_id": "node",
        "command": "echo " + secret,
        "environment": {"TOKEN": secret},
        "stdout": secret,
        "arguments": {"also": secret},
    })
    audit = (tmp_path / "state" / "audit.jsonl").read_text(
        encoding="utf-8")
    assert secret not in audit
    assert "echo " not in audit



def test_put_file_gating_makes_only_live_writes_code_equivalent():
    def gate(**arguments):
        return hostapp.effective_operation_gating(
            hostapp.PHASE1_OPERATIONS, "convoy_put_file",
            dict({"artifact": {}}, **arguments))

    assert gate(dest="a.py")["executes_arbitrary_code"] is False
    assert gate(dest="scripts/a.py", worktree="review")[
        "executes_arbitrary_code"] is False
    assert gate(dest="scripts/a.py", live=True)["executes_arbitrary_code"] is True
    assert gate(dest="a.py")["mutating"] is True
    assert "convoy_put_file" in hostapp.HOST_NATIVE_OPERATIONS
    for bad in ({"dest": "nested/a.py"}, {"dest": "../a.py", "live": True},
                {"dest": "a.py", "live": True, "worktree": "review"},
                {"dest": "a.py", "path": "C:/x"}, {"dest": ".git/hooks/x",
                                                    "live": True}):
        try:
            gate(**bad)
        except hostapp.OperationRegistryError as exc:
            assert exc.reason == "malformed"
        else:
            raise AssertionError(bad)


def _send(app, node, source, controller="ctl-send"):
    return app.relay_send_file({
        "target_host_id": app.host_id, "convoy_id": node["convoy_id"],
        "target_node_id": node["node_id"], "controller_id": controller,
        "source_path": str(source)})


def _put(app, node, arguments, key, controller="ctl-send"):
    code, body = app.create_job({
        "idempotency_key": key, "node_id": node["node_id"],
        "operation": "convoy_put_file", "arguments": arguments,
        "controller_id": controller})
    return code, body


def test_send_file_lands_in_the_inbox_without_waking_td(tmp_path):
    app, node, project, forwarded, woken = _app_and_node(tmp_path, perform=True)
    source = project / "scripts" / "tool.py"
    source.parent.mkdir()
    source.write_bytes(b"print('tool')\n")
    code, staged = _send(app, node, source)
    assert code == 200 and staged["ok"], staged

    code, body = _put(app, node, {"artifact": staged["artifact"],
                                  "dest": "tool.py"}, "put-inbox")
    assert code == 200, body
    code, body = app.dispatch_job(body["job"]["delivery_id"])
    assert code == 200 and body["job"]["state"] == "succeeded", body
    landed = project / ".embody" / "convoy" / "inbox" / "tool.py"
    assert landed.read_bytes() == b"print('tool')\n"
    assert forwarded == [] and woken == []


def test_send_file_refuses_a_source_outside_registered_projects(tmp_path):
    app, node, _project, _f, _w = _app_and_node(tmp_path)
    outside = tmp_path / "secret.txt"
    outside.write_text("not for the fleet")
    code, body = _send(app, node, outside)
    assert code == 403 and body["reason"] == "source_outside_project"


def test_live_put_file_needs_allow_execute_td_python(tmp_path):
    app, node, project, _f, _w = _app_and_node(tmp_path)
    source = project / "a.py"
    source.write_text("x = 1\n")
    _code, staged = _send(app, node, source)
    code, body = _put(app, node, {"artifact": staged["artifact"],
                                  "dest": "b.py", "live": True}, "put-live")
    assert code == 403 and body["reason"] == "td_python_not_approved", body
    assert not (project / "b.py").exists()


def test_put_file_writes_only_the_callers_own_upload(tmp_path):
    app, node, project, _f, _w = _app_and_node(tmp_path)
    source = project / "a.txt"
    source.write_text("mine\n")
    _code, staged = _send(app, node, source, controller="ctl-a")
    code, body = _put(app, node, {"artifact": staged["artifact"],
                                  "dest": "a.txt"}, "put-other",
                      controller="ctl-b")
    assert code == 200, body
    _code, body = app.dispatch_job(body["job"]["delivery_id"])
    assert body["job"]["state"] == "failed"
    assert body["job"]["result"]["code"] == "artifact_not_found"
    assert not (project / ".embody" / "convoy" / "inbox").exists()


def test_put_file_into_a_registered_worktree(tmp_path):
    import shutil
    import subprocess
    git = shutil.which("git")
    if not git:
        import pytest
        pytest.skip("git is not installed")
    app, node, project, _f, _w = _app_and_node(tmp_path)

    def setup(*args):
        subprocess.run([git, "-C", str(project), "-c", "user.name=t",
                        "-c", "user.email=t@example.com", *args], check=True,
                       stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE)

    setup("init", "--quiet", "-b", "main")
    (project / "keep.txt").write_text("keep\n")
    setup("add", "keep.txt")
    setup("commit", "--quiet", "-m", "init")
    setup("worktree", "add", "-b", "review", str(tmp_path / "project-wt-review"))
    source = project / "keep.txt"
    _code, staged = _send(app, node, source)
    code, body = _put(app, node, {"artifact": staged["artifact"],
                                  "dest": "docs/keep.txt",
                                  "worktree": "review"}, "put-wt")
    assert code == 200, body
    _code, body = app.dispatch_job(body["job"]["delivery_id"])
    assert body["job"]["state"] == "succeeded", body
    assert (tmp_path / "project-wt-review" / "docs" / "keep.txt").read_text() == "keep\n"

    code, body = _put(app, node, {"artifact": staged["artifact"],
                                  "dest": "keep.txt", "worktree": "absent"},
                      "put-missing")
    _code, body = app.dispatch_job(body["job"]["delivery_id"])
    assert body["job"]["result"]["code"] == "worktree_missing"


def test_put_file_never_writes_ai_client_or_embody_config():
    for dest in (".mcp.json", ".claude/settings.local.json", ".Claude/x",
                 ".codex/config.toml", ".cursor/mcp.json", ".vscode/mcp.json",
                 ".gemini/settings.json", ".embody/project.json"):
        for mode in ({"worktree": "review"}, {"live": True}):
            try:
                hostapp.effective_operation_gating(
                    hostapp.PHASE1_OPERATIONS, "convoy_put_file",
                    dict({"artifact": {}, "dest": dest}, **mode))
            except hostapp.OperationRegistryError as exc:
                assert exc.reason == "malformed"
            else:
                raise AssertionError((dest, mode))


def test_a_worktree_registered_as_its_own_node_is_never_a_side_door(tmp_path):
    import shutil
    import subprocess
    import pytest
    git = shutil.which("git")
    if not git:
        pytest.skip("git is not installed")
    app, node, project, _f, _w = _app_and_node(tmp_path)
    for args in (("init", "--quiet", "-b", "main"),
                 ("commit", "--quiet", "--allow-empty", "-m", "init"),
                 ("worktree", "add", "-b", "show",
                  str(tmp_path / "project-wt-show"))):
        subprocess.run([git, "-C", str(project), "-c", "user.name=t",
                        "-c", "user.email=t@example.com", *args], check=True,
                       stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE)
    code, _other = app.register_node({
        "project_root": str(tmp_path / "project-wt-show"),
        "convoy_id": "studio", "comp_path": "/Embody",
        "runtime_id": "rt_show"})
    assert code == 200
    source = project / "a.txt"
    source.write_text("x\n")
    _code, staged = _send(app, node, source)
    _code, body = _put(app, node, {"artifact": staged["artifact"],
                                   "dest": "a.txt", "worktree": "show"},
                       "put-node-wt")
    _code, body = app.dispatch_job(body["job"]["delivery_id"])
    assert body["job"]["result"]["code"] == "worktree_is_node"
    assert not (tmp_path / "project-wt-show" / "a.txt").exists()

    job = _create(app, node, "convoy_git", {
        "operation": "worktree_remove", "arguments": {"name": "show"}},
        key="rm-node-wt")
    _code, body = app.dispatch_job(job["delivery_id"])
    assert body["job"]["result"]["code"] == "worktree_is_node"
    assert (tmp_path / "project-wt-show").is_dir()
