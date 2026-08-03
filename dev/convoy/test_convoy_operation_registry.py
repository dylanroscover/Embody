"""Convoy's explicit Envoy operation surface and dynamic permission gates.

This suite intentionally reads EnvoyExt through Python's AST.  A new MCP tool
must land with either a reviewed Convoy registry classification or a reviewed
exclusion; silently inheriting a permissive default is never allowed.
"""

import ast
from pathlib import Path

import pytest

import convoy_hostapp as hostapp
from conftest import approve_td_python


ENVOY_EXT = (Path(__file__).resolve().parents[1]
             / "embody" / "Embody" / "EnvoyExt.py")


def _is_mcp_tool(decorator):
    """Recognize @self.mcp.tool() without executing TouchDesigner source."""
    if not isinstance(decorator, ast.Call):
        return False
    tool = decorator.func
    return (isinstance(tool, ast.Attribute) and tool.attr == "tool"
            and isinstance(tool.value, ast.Attribute)
            and tool.value.attr == "mcp"
            and isinstance(tool.value.value, ast.Name)
            and tool.value.value.id == "self")


def _envoy_mcp_tools():
    tree = ast.parse(ENVOY_EXT.read_text(encoding="utf-8"))
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(_is_mcp_tool(decorator)
                for decorator in node.decorator_list)
    }


def _app_and_node(tmp_path):
    app = hostapp.HostApp(str(tmp_path / "state"))
    code, node = app.register_node({
        "project_root": str(tmp_path / "project"),
        "convoy_id": "studio",
        "comp_path": "/Embody",
        "runtime_id": "rt_registry_test",
    })
    assert code == 200, node
    return app, node


def test_envoy_ast_and_convoy_registry_cannot_drift_silently():
    envoy_tools = _envoy_mcp_tools()
    classified = (set(hostapp.PHASE1_OPERATIONS)
                  - set(hostapp.HOST_NATIVE_OPERATIONS))
    excluded = set(hostapp.ENVOY_TOOL_EXCLUSIONS)

    assert classified.isdisjoint(excluded)
    assert envoy_tools == classified | excluded
    assert all(hostapp.ENVOY_TOOL_EXCLUSIONS[name].strip()
               for name in excluded)


# run_tests and save_project are the two worker-only operations kept OFF the
# remote surface (A-1/R-2): run_tests exec_module's every test file it finds on
# disk, and save_project blocks TD's main thread 15+s before A-30/A-31 show
# protection exists.  Every OTHER registered operation is remotely addressable.
_NOT_REMOTE_EXPOSED = {"run_tests", "save_project"}


def test_every_real_registry_entry_has_an_explicit_gate_classification():
    required = {
        "schema", "mutating", "executes_arbitrary_code",
        "remote_exposed", "runtime_required", "batch_eligible",
        "side_effects",
    }
    for name, entry in hostapp.PHASE1_OPERATIONS.items():
        assert required <= set(entry), name
        assert entry["remote_exposed"] is (name not in _NOT_REMOTE_EXPOSED), \
            name
        for field in ("mutating", "executes_arbitrary_code",
                      "remote_exposed", "runtime_required",
                      "batch_eligible"):
            assert type(entry[field]) is bool, (name, field)


def test_batch_eligibility_is_capability_digest_material(tmp_path):
    app, _node = _app_and_node(tmp_path)
    before = app.build_manifest().operations["query_network"]
    app.operations["query_network"]["batch_eligible"] = False
    after = app.build_manifest().operations["query_network"]
    assert after != before


def test_static_arbitrary_code_classification_is_exact():
    arbitrary = {
        name for name, entry in hostapp.PHASE1_OPERATIONS.items()
        if entry["executes_arbitrary_code"]
    }
    assert arbitrary == {
        "execute_python", "exec_op_method", "set_dat_content",
        "edit_dat_content", "create_extension", "import_network",
        "run_tests",
    }
    # set_parameter is deliberately call-shape-sensitive, not blanket gated.
    assert hostapp.PHASE1_OPERATIONS["set_parameter"]["side_effects"][
        "arbitrary_code_when"] == "expression_or_bind"


@pytest.mark.parametrize("arguments", [
    {"op_path": "/x", "par_name": "tx", "value": 4},
    {"op_path": "/x", "par_name": "chop", "mode": "export"},
])
def test_set_parameter_constant_and_export_forms_do_not_require_python(arguments):
    gating = hostapp.effective_operation_gating(
        hostapp.PHASE1_OPERATIONS, "set_parameter", arguments)
    assert gating["mutating"] is True
    assert gating["executes_arbitrary_code"] is False


@pytest.mark.parametrize("arguments", [
    {"op_path": "/x", "par_name": "tx", "mode": "expression"},
    {"op_path": "/x", "par_name": "tx", "mode": "bind"},
    {"op_path": "/x", "par_name": "tx", "expr": "me.time.frame"},
    {"op_path": "/x", "par_name": "tx", "bind_expr": "parent().par.X"},
])
def test_set_parameter_expression_and_bind_forms_require_python(arguments):
    gating = hostapp.effective_operation_gating(
        hostapp.PHASE1_OPERATIONS, "set_parameter", arguments)
    assert gating["executes_arbitrary_code"] is True


@pytest.mark.parametrize("par_name", [
    "Convoyenable", "ConvoyRemoteWake", "Remote Wake",
    "AllowTdPython", "Allow Execute TD Python", "AllowFullShell",
    "ArtifactQuota", "convoy_node_name",
])
def test_convoy_control_parameters_are_never_relayable(par_name):
    for arguments in (
        {"op_path": "/Embody", "par_name": par_name, "value": 1},
        {"op_path": "/Embody", "par_name": par_name,
         "expr": "True", "mode": "expression"},
    ):
        with pytest.raises(hostapp.OperationRegistryError) as caught:
            hostapp.effective_operation_gating(
                hostapp.PHASE1_OPERATIONS, "set_parameter", arguments)
        assert caught.value.reason == "reserved_parameter"


def test_batch_inherits_every_child_gate_and_cannot_hide_python():
    safe = {"operations": [
        {"tool": "query_network", "params": {"parent_path": "/"}},
        {"tool": "set_parameter",
         "params": {"op_path": "/x", "par_name": "tx", "value": 2}},
    ]}
    gating = hostapp.effective_operation_gating(
        hostapp.PHASE1_OPERATIONS, "batch_operations", safe)
    assert gating == {
        "mutating": True,
        "executes_arbitrary_code": False,
        "runtime_required": False,
        "remote_exposed": True,
    }

    expression = {"operations": [
        {"tool": "set_parameter",
         "params": {"op_path": "/x", "par_name": "tx",
                    "mode": "expression", "expr": "absTime.frame"}},
    ]}
    assert hostapp.effective_operation_gating(
        hostapp.PHASE1_OPERATIONS, "batch_operations", expression
    )["executes_arbitrary_code"] is True

    python = {"operations": [
        {"tool": "query_network", "params": {}},
        {"tool": "execute_python", "params": {"code": "result = 1"}},
    ]}
    gating = hostapp.effective_operation_gating(
        hostapp.PHASE1_OPERATIONS, "batch_operations", python)
    assert gating["executes_arbitrary_code"] is True
    assert gating["runtime_required"] is True


@pytest.mark.parametrize(("children", "reason"), [
    ([{"tool": "not_a_tool", "params": {}}], "operation_not_exposed"),
    ([{"tool": "get_job_status", "params": {}}],
     "operation_not_batchable"),
    ([{"tool": "batch_operations", "params": {"operations": []}}],
     "nested_batch_not_allowed"),
    ([{"tool": "query_network", "params": []}], "malformed"),
])
def test_batch_rejects_unknown_nonbatchable_nested_and_malformed(children,
                                                                  reason):
    with pytest.raises(hostapp.OperationRegistryError) as caught:
        hostapp.effective_operation_gating(
            hostapp.PHASE1_OPERATIONS, "batch_operations",
            {"operations": children})
    assert caught.value.reason == reason


def test_td_python_approval_gates_code_but_not_constant_parameters(tmp_path):
    app, node = _app_and_node(tmp_path)

    code, body = app.create_job({
        "idempotency_key": "py-gate",
        "node_id": node["node_id"],
        "operation": "execute_python",
        "arguments": {"code": "result = 1"},
        "expected_runtime_id": node["runtime_id"],
    })
    assert code == 403
    assert body["reason"] == "td_python_not_approved"

    code, body = app.create_job({
        "idempotency_key": "constant-is-not-code",
        "node_id": node["node_id"],
        "operation": "set_parameter",
        "arguments": {"op_path": "/x", "par_name": "tx", "value": 1},
    })
    assert code == 200, body

    approve_td_python(app, node["node_id"])
    code, body = app.create_job({
        # A refusal must not burn the idempotency key.
        "idempotency_key": "py-gate",
        "node_id": node["node_id"],
        "operation": "execute_python",
        "arguments": {"code": "result = 1"},
        "expected_runtime_id": node["runtime_id"],
    })
    assert code == 200, body
    assert body["created"] is True


def test_batch_applies_td_python_and_reserved_parameter_gates(tmp_path):
    app, node = _app_and_node(tmp_path)
    batch = {"operations": [{
        "tool": "set_parameter",
        "params": {"op_path": "/x", "par_name": "tx",
                   "expr": "absTime.frame"},
    }]}
    code, body = app.create_job({
        "idempotency_key": "batch-code", "node_id": node["node_id"],
        "operation": "batch_operations", "arguments": batch,
    })
    assert code == 403 and body["reason"] == "td_python_not_approved"

    approve_td_python(app, node["node_id"])
    code, body = app.create_job({
        "idempotency_key": "batch-reserved", "node_id": node["node_id"],
        "operation": "batch_operations", "arguments": {"operations": [{
            "tool": "set_parameter",
            "params": {"op_path": "/Embody",
                       "par_name": "AllowFullShell", "value": 1},
        }]},
    })
    assert code == 403 and body["reason"] == "reserved_parameter"


def test_dispatch_rechecks_python_approval_and_terminalizes_withdrawal(
        tmp_path):
    app, node = _app_and_node(tmp_path)
    approve_td_python(app, node["node_id"])
    code, body = app.create_job({
        "idempotency_key": "withdraw-before-dispatch",
        "node_id": node["node_id"],
        "operation": "execute_python",
        "arguments": {"code": "result = 1"},
        "expected_runtime_id": node["runtime_id"],
    })
    assert code == 200, body
    delivery_id = body["job"]["delivery_id"]

    # Model the user switching the permission off after admission.
    app.policy.disable_td_python(node["node_id"])
    code, body = app.dispatch_job(delivery_id)
    assert code == 403
    assert body["reason"] == "td_python_not_approved"
    assert app.db.get_job(delivery_id)["state"] == "refused"


def test_get_job_status_is_an_explicit_relayable_poll_operation(tmp_path):
    app, node = _app_and_node(tmp_path)
    entry = hostapp.PHASE1_OPERATIONS["get_job_status"]
    assert hostapp.gating_of(entry) == {
        "mutating": False,
        "executes_arbitrary_code": False,
        "runtime_required": False,
        "remote_exposed": True,
    }
    code, body = app.create_job({
        "idempotency_key": "node-job-poll",
        "node_id": node["node_id"],
        "operation": "get_job_status",
        "arguments": {"job_id": "job_12345678"},
    })
    assert code == 200, body
