"""Off-TD contract tests for the release .toe export (embody_admin C5b).

ExportReleaseToe cannot be tried twice (it destroys Embody, saves, quits),
so its decisions live in PURE planners pinned here -- one test per refusal,
the save-path resolution, the shell census, the inline exclusions, both tag
spellings, the Embody-owned storage keys (from the REAL constants), the
destroy order, preview purity -- plus the orchestrator on a fake TD. No
TouchDesigner import: runs on both CI legs as on a dev box.
"""

from __future__ import annotations

import ast
import copy
import importlib.util
from pathlib import Path


REPO = Path(__file__).resolve().parents[3]
EMBODY_SRC = REPO / "dev" / "embody" / "Embody"
ADMIN_PY = EMBODY_SRC / "embody_admin.py"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


admin = _load("release_toe_embody_admin", ADMIN_PY)
tdxn = _load("release_toe_tdxnext", EMBODY_SRC / "TDXNExt.py")
embodyext = _load("release_toe_embodyext", EMBODY_SRC / "EmbodyExt.py")

# What the collector hands the scrub: derived from the real constants, the
# same way _release_collect derives it, never copied.
STORAGE_KEYS = admin.release_storage_keys(
    set(tdxn.SKIP_STORAGE_KEYS) | set(embodyext.EmbodyExt._STORAGE_SKIP_EXTRA),
    embodyext.EmbodyExt._STORAGE_CONTROL_KEYS)
TDXN_TAG = "tdxn"
LEGACY_TAG = embodyext.EmbodyExt._LEGACY_TDXN_TAG
EXCLUDE_TAG = "tdxn_exclude"
LEGACY_EXCLUDE = embodyext.EmbodyExt._LEGACY_TDXN_EXCLUDE_TAG

EMBODY = "/moonshine/lib/Embody"
TABLE = "/moonshine/lib/externalizations"
HOOK = "/moonshine/pre_release_toe"
TOE = "C:/builds/Moonshine-Server-1.0.0.toe"
FOLDER = "C:/dev/moonshine/touch"
PROJECT_TOE = FOLDER + "/moonshine.42.toe"


def _ready_kwargs(**over):
    """A gate that passes, so each test can break exactly one thing."""
    kwargs = dict(
        tdxn_comps=[{"path": "/moonshine/warp", "present": True,
                     "children": 12, "file_ops": 12}],
        frame=900,
        op_errors=[],
        perform_mode=False,
        project_saved=True,
        save_path=TOE,
        project_toe=PROJECT_TOE,
        project_folder=FOLDER,
        save_dir_exists=True,
        save_path_exists=False,
        startup_done=False,
    )
    kwargs.update(over)
    return kwargs


def _state(**over):
    """The shape _release_collect reads out of TouchDesigner."""
    state = {
        "embody_path": EMBODY,
        "table_path": TABLE,
        "hook_path": HOOK,
        "tdxn_comps": [{"path": "/moonshine/warp", "present": True,
                        "children": 12, "file_ops": 12}],
        "tracked": [
            {"path": "/moonshine/network/NetworkExt", "family": "DAT",
             "file": "moonshine/network/NetworkExt.py", "syncfile": True,
             "externaltox": "", "enableexternaltox": False},
            {"path": TABLE, "family": "DAT",
             "file": "moonshine/externalizations.tsv", "syncfile": True,
             "externaltox": "", "enableexternaltox": False},
        ],
        "tracked_paths": ["/moonshine/network/NetworkExt", TABLE],
        "ops": [{"path": "/moonshine/warp", "tags": [TDXN_TAG],
                 "storage_keys": ["_tdn_rel_path"]},
                {"path": "/moonshine/network/NetworkExt", "tags": [],
                 "storage_keys": []}],
        "orphan_tables": [],
        "refs": [{"path": "/moonshine/network/NetworkExt", "par": "file",
                  "value": "moonshine/network/NetworkExt.py"},
                 {"path": "/moonshine/media/loop", "par": "file",
                  "value": "C:/dev/assets/loop.mov"}],
        "execute_dats": [],
        "op_errors": [],
        "frame": 900,
        "startup_done": False,
        "perform_mode": False,
        "project_saved": True,
        "project_toe": PROJECT_TOE,
        "project_folder": FOLDER,
        "save_dir_exists": True,
        "save_path_exists": False,
        "tdxn_mode": "full",
        "strip_on_save": True,
        "filecleanup": "ask",
        "embody_tags": [TDXN_TAG, LEGACY_TAG, "tox"],
        "exclude_tags": [EXCLUDE_TAG, LEGACY_EXCLUDE],
        "storage_keys": list(STORAGE_KEYS),
        "is_pro": True,
    }
    state.update(over)
    return state


def _refusal(plan_or_gate, needle):
    refusals = (plan_or_gate["refusals"] if "refusals" in plan_or_gate
                else plan_or_gate["readiness"]["refusals"])
    return [r for r in refusals if needle in r]


# -- the readiness gate: one test per way to be refused -------------------

def test_a_ready_project_is_not_refused():
    gate = admin.plan_release_readiness(**_ready_kwargs())
    assert gate["ready"] is True
    assert gate["refusals"] == []
    assert gate["warnings"] == []


def test_perform_mode_is_refused():
    gate = admin.plan_release_readiness(**_ready_kwargs(perform_mode=True))
    assert gate["ready"] is False
    assert _refusal(gate, "Perform Mode")


def test_an_unsaved_project_is_refused():
    gate = admin.plan_release_readiness(**_ready_kwargs(project_saved=False))
    assert _refusal(gate, "never been saved")


def test_a_missing_save_path_is_refused():
    for path in (None, "", "   "):
        gate = admin.plan_release_readiness(**_ready_kwargs(save_path=path))
        assert _refusal(gate, "save path is required"), path


def test_a_save_path_that_is_not_a_toe_is_refused():
    gate = admin.plan_release_readiness(
        **_ready_kwargs(save_path="C:/builds/Moonshine.tox"))
    assert _refusal(gate, "must end in .toe")


def test_saving_over_the_running_project_is_refused():
    """The dev .toe is the ONE file this export must never write."""
    gate = admin.plan_release_readiness(
        **_ready_kwargs(save_path=PROJECT_TOE))
    assert _refusal(gate, "the running project itself")
    # Separator and case drift must not open the hole back up.
    gate = admin.plan_release_readiness(
        **_ready_kwargs(save_path="C:\\dev\\moonshine\\touch\\Moonshine.42.TOE"))
    assert _refusal(gate, "the running project itself")


def test_relative_save_paths_resolve_against_the_project_folder():
    """TD's cwd is the project folder; a bare name lands there. Resolved
    once, up front, so the gate judges the file that would be written."""
    resolve = admin.release_resolve_save_path
    assert resolve("Release.toe", FOLDER) == FOLDER + "/Release.toe"
    assert resolve("..\\out\\Release.toe", FOLDER) == \
        "C:/dev/moonshine/out/Release.toe"
    assert resolve("D:\\builds\\Release.toe", FOLDER) == "D:/builds/Release.toe"
    assert resolve("/srv/builds/Release.toe", FOLDER) == \
        "/srv/builds/Release.toe"
    assert resolve("", FOLDER) == ""
    assert resolve(None, FOLDER) == ""


def test_a_relative_name_of_the_running_project_is_refused():
    """'moonshine.42.toe' used to pass the string compare and overwrite
    the dev .toe through project.save's cwd."""
    plan = admin.plan_release_toe(_state(), save_path="moonshine.42.toe")
    assert plan["save_path"] == PROJECT_TOE
    assert _refusal(plan, "the running project itself")


def test_a_release_in_the_projects_own_save_series_is_refused():
    """MyShow.toe beside MyShow.42.toe reads as the project's newest save
    to TD's incremental save and to _resolveProjectToe alike."""
    for path in (FOLDER + "/moonshine.toe", FOLDER + "/Moonshine.99.toe",
                 "moonshine.toe"):
        plan = admin.plan_release_toe(_state(), save_path=path)
        assert _refusal(plan, "own folder and save series"), path
    # Another base name in the project folder, or the same series
    # elsewhere, is a different file.
    for path in (FOLDER + "/MyShow.toe", "C:/builds/moonshine.toe"):
        plan = admin.plan_release_toe(_state(), save_path=path)
        assert plan["readiness"]["ready"] is True, path


def test_a_missing_save_folder_is_refused():
    gate = admin.plan_release_readiness(**_ready_kwargs(save_dir_exists=False))
    assert _refusal(gate, "save folder does not exist")
    # Unknown (None) is not a refusal -- the caller could not check.
    assert admin.plan_release_readiness(
        **_ready_kwargs(save_dir_exists=None))["ready"] is True


def test_an_existing_save_path_is_refused():
    """project.save over an existing file prompts, in a session with no
    Embody left to answer."""
    gate = admin.plan_release_readiness(**_ready_kwargs(save_path_exists=True))
    assert _refusal(gate, "already exists")


def test_stripped_shells_are_refused_by_name():
    """THE gate. A saved dev .toe holds empty TDXN COMPs until the frame
    45-90 restores land; exporting one ships a hollow project."""
    gate = admin.plan_release_readiness(**_ready_kwargs(tdxn_comps=[
        {"path": "/moonshine/warp", "present": True, "children": 0,
         "file_ops": 12},
        {"path": "/moonshine/scene", "present": True, "children": 4,
         "file_ops": 4}]))
    assert gate["shells"] == ["/moonshine/warp"]
    assert _refusal(gate, "stripped shells")
    assert "/moonshine/warp" in gate["refusals"][0]


def test_an_empty_comp_that_is_empty_on_disk_is_not_a_shell():
    """/perform, a test sandbox: no children live AND none in the .tdxn.
    The dev project itself was refused on these (2026-09-08)."""
    gate = admin.plan_release_readiness(**_ready_kwargs(tdxn_comps=[
        {"path": "/perform", "present": True, "children": 0, "file_ops": 0}]))
    assert gate["ready"] is True
    assert gate["shells"] == [] and gate["unverifiable"] == []


def test_an_empty_comp_with_an_unreadable_file_is_refused():
    gate = admin.plan_release_readiness(**_ready_kwargs(tdxn_comps=[
        {"path": "/moonshine/warp", "present": True, "children": 0,
         "file_ops": None}]))
    assert gate["unverifiable"] == ["/moonshine/warp"]
    assert gate["shells"] == []
    assert _refusal(gate, "could not be read")


def test_a_tracked_comp_missing_from_the_network_is_refused():
    gate = admin.plan_release_readiness(**_ready_kwargs(tdxn_comps=[
        {"path": "/moonshine/warp", "present": False, "children": 0,
         "file_ops": 12}]))
    assert gate["missing"] == ["/moonshine/warp"]
    assert _refusal(gate, "not in the network")
    # A missing COMP is not ALSO reported as a shell.
    assert gate["shells"] == []


def test_a_comp_the_hook_destroyed_or_emptied_on_purpose_is_not_refused():
    """Its table row still stands after the hook; the re-run gate is told
    which COMPs the hook removed -- or emptied, which would otherwise read
    as a stripped shell."""
    comps = [{"path": "/moonshine/runtime", "present": False, "children": 0,
              "file_ops": 30},
             {"path": "/moonshine/cache", "present": True, "children": 0,
              "file_ops": 8}]
    gate = admin.plan_release_readiness(**_ready_kwargs(tdxn_comps=comps))
    assert _refusal(gate, "not in the network") and _refusal(gate, "stripped")
    gate = admin.plan_release_readiness(**_ready_kwargs(
        tdxn_comps=comps, hook_touched=["/moonshine/runtime", "/moonshine/cache"]))
    assert gate["ready"] is True and gate["missing"] == [] and gate["shells"] == []
    plan = admin.plan_release_toe(_state(tdxn_comps=comps), save_path=TOE,
                                  hook_touched=["/moonshine/runtime", "/moonshine/cache"])
    assert plan["readiness"]["ready"] is True


def test_the_startup_window_is_refused_and_the_flag_waives_it():
    gate = admin.plan_release_readiness(**_ready_kwargs(frame=90))
    assert _refusal(gate, "startup window")
    assert admin.plan_release_readiness(
        **_ready_kwargs(frame=90, startup_done=True))["ready"] is True
    # The boundary is exclusive: frame 120 is still inside.
    assert _refusal(admin.plan_release_readiness(
        **_ready_kwargs(frame=admin.RELEASE_READY_FRAME)), "startup window")
    assert admin.plan_release_readiness(
        **_ready_kwargs(frame=admin.RELEASE_READY_FRAME + 1))["ready"] is True


def test_operator_errors_are_refused_and_quoted():
    gate = admin.plan_release_readiness(
        **_ready_kwargs(op_errors=["/moonshine/x: bad wire"]))
    assert _refusal(gate, "operator error")
    assert _refusal(gate, "bad wire")


def test_operator_errors_can_be_waived_into_warnings():
    """Dormant templates carry cook errors they cannot cook away (field,
    moonshine 2026-09-08): the waiver logs them instead of refusing."""
    gate = admin.plan_release_readiness(
        **_ready_kwargs(op_errors=["/moonshine/tpl: cook error"],
                        ignore_op_errors=True))
    assert gate["ready"] is True and gate["refusals"] == []
    assert len(gate["warnings"]) == 1 and "cook error" in gate["warnings"][0]
    plan = admin.plan_release_toe(
        _state(op_errors=["/moonshine/tpl: cook error"]), save_path=TOE,
        ignore_op_errors=True)
    assert plan["readiness"]["ready"] is True


def test_every_refusal_is_reported_at_once():
    """One run, one list -- a release operator should not fix these one
    reopen at a time."""
    gate = admin.plan_release_readiness(**_ready_kwargs(
        perform_mode=True, project_saved=False, save_path="", frame=10,
        op_errors=["/x: boom"]))
    assert len(gate["refusals"]) == 5


# -- standing the disk writers down ----------------------------------------

def test_disarm_writes_both_tdxn_parameters():
    assert admin.plan_release_disarm("full", True) == [
        ("Tdxnmode", "off"), ("Tdxnstriponsave", False)]


def test_disarm_is_empty_when_already_stood_down():
    assert admin.plan_release_disarm("off", False) == []
    assert admin.plan_release_disarm("off", False, "keep") == []


def test_disarm_forces_filecleanup_to_keep_and_first():
    """A 'delete' inherited from the shared config would turn the next
    continuity sweep into a silent unlink of every inlined source -- so it
    is the first write, in case a later one fails."""
    for pref in ("delete", "ask"):
        assert admin.plan_release_disarm("full", True, pref)[0] == (
            "Filecleanup", "keep"), pref


# -- pre-save execute DATs -------------------------------------------------

def test_embodys_own_presave_hook_is_not_reported():
    """It dies with the COMP in step 4; only a PRODUCT hook blocks."""
    dats = [{"path": EMBODY + "/execute", "projectpresave": True,
             "active": True},
            {"path": "/moonshine/sources/quiesce", "projectpresave": True,
             "active": True},
            {"path": "/moonshine/other", "projectpresave": False,
             "active": True},
            {"path": "/moonshine/off", "projectpresave": True,
             "active": False}]
    assert admin.plan_release_presave_hooks(dats, EMBODY) == [
        "/moonshine/sources/quiesce"]


# -- the inline pass -------------------------------------------------------

def test_the_tracking_tables_own_row_is_never_inlined():
    """Clearing a table DAT's file keeps its TEXT -- inlining the tracker
    would bake the whole source-file manifest into the artifact."""
    plan = admin.plan_release_inline(
        _state()["tracked"], embody_path=EMBODY, table_path=TABLE,
        hook_path=HOOK)
    assert plan["dats"] == ["/moonshine/network/NetworkExt"]
    skipped = {e["path"]: e["why"] for e in plan["skipped"]}
    assert TABLE in skipped
    assert "manifest" in skipped[TABLE]


def test_the_hook_row_is_never_inlined():
    tracked = [{"path": HOOK, "family": "DAT", "file": "release/hook.py",
                "syncfile": True, "externaltox": "",
                "enableexternaltox": False}]
    plan = admin.plan_release_inline(tracked, embody_path=EMBODY,
                                     table_path=TABLE, hook_path=HOOK)
    assert plan["dats"] == []
    assert plan["skipped"][0]["path"] == HOOK
    assert "release hook" in plan["skipped"][0]["why"]


def test_everything_inside_embody_is_left_alone():
    """Clearing file/syncfile on EmbodyExt.py reinitializes the extension
    running the export -- and the COMP is deleted whole anyway."""
    tracked = [
        {"path": EMBODY, "family": "COMP", "file": "", "syncfile": False,
         "externaltox": "embody/Embody.tox", "enableexternaltox": True},
        {"path": EMBODY + "/EmbodyExt", "family": "DAT",
         "file": "embody/Embody/EmbodyExt.py", "syncfile": True,
         "externaltox": "", "enableexternaltox": False}]
    plan = admin.plan_release_inline(tracked, embody_path=EMBODY,
                                     table_path=TABLE)
    assert plan["dats"] == [] and plan["comps"] == []
    assert len(plan["skipped"]) == 2
    assert all("Embody COMP" in e["why"] for e in plan["skipped"])


def test_dats_and_comps_take_different_bindings():
    tracked = [
        {"path": "/a", "family": "DAT", "file": "a.py", "syncfile": False,
         "externaltox": "", "enableexternaltox": False},
        {"path": "/b", "family": "DAT", "file": "", "syncfile": True,
         "externaltox": "", "enableexternaltox": False},
        {"path": "/c", "family": "COMP", "file": "", "syncfile": False,
         "externaltox": "c.tox", "enableexternaltox": False},
        {"path": "/d", "family": "COMP", "file": "", "syncfile": False,
         "externaltox": "", "enableexternaltox": True},
        {"path": "/e", "family": "DAT", "file": "", "syncfile": False,
         "externaltox": "", "enableexternaltox": False},
        {"path": "/f", "family": "TOP", "file": "movie.mov",
         "syncfile": False, "externaltox": "", "enableexternaltox": False}]
    plan = admin.plan_release_inline(tracked, embody_path=EMBODY,
                                     table_path=TABLE)
    assert plan["dats"] == ["/a", "/b"]
    assert plan["comps"] == ["/c", "/d"]
    skipped = {e["path"] for e in plan["skipped"]}
    assert skipped == {"/e", "/f"}


def test_the_reference_sweep_separates_absolute_from_dangling():
    sweep = admin.plan_release_reference_sweep([
        {"path": "/a", "par": "file", "value": "assets/a.txt"},
        {"path": "/b", "par": "file", "value": "C:/dev/secret/b.txt"},
        {"path": "/c", "par": "externaltox", "value": "/home/dev/c.tox"},
        {"path": "/d", "par": "file", "value": "/sys/quiet/thing"},
        {"path": "/e", "par": "file", "value": ""}])
    assert [e["path"] for e in sweep["remaining"]] == ["/a", "/b", "/c", "/d"]
    assert [e["path"] for e in sweep["absolute"]] == ["/b", "/c"]


def test_the_reference_preview_skips_what_the_inline_pass_clears():
    """The preview reads bindings BEFORE anything is cleared; what the
    inline plan names must not be reported as remaining."""
    plan = admin.plan_release_toe(_state(), save_path=TOE)
    refs = plan["references"]
    assert [e["path"] for e in refs["remaining"]] == ["/moonshine/media/loop"]
    assert [e["path"] for e in refs["absolute"]] == ["/moonshine/media/loop"]


# -- the footprint scrub ---------------------------------------------------

def test_both_tag_spellings_and_the_qualifiers_come_off():
    tags = [TDXN_TAG, LEGACY_TAG, EXCLUDE_TAG, LEGACY_EXCLUDE, "clone",
            EXCLUDE_TAG + ":play", LEGACY_EXCLUDE + ":file",
            "mycomp", "lighting:stage"]
    assert admin.release_tag_removals(
        tags, {TDXN_TAG, LEGACY_TAG, "tox"},
        {EXCLUDE_TAG, LEGACY_EXCLUDE}) == sorted([
            TDXN_TAG, LEGACY_TAG, EXCLUDE_TAG, LEGACY_EXCLUDE, "clone",
            EXCLUDE_TAG + ":play", LEGACY_EXCLUDE + ":file"])


def test_a_users_own_tags_survive_the_scrub():
    assert admin.release_tag_removals(
        ["mycomp", "lighting:stage", "tdxnish"],
        {TDXN_TAG}, {EXCLUDE_TAG}) == []


def test_the_scrub_takes_only_embody_owned_storage_keys():
    """Derived from the REAL constants. The breadcrumbs a .toe save
    persists and the two Embed-toggle control keys are Embody's; the
    Embody-UI runtime names in SKIP_STORAGE_KEYS ('hover', 'test_results')
    are names a user's own component could carry, and must never be
    unstored project-wide."""
    for key in ("_tdn_rel_path", "_tdn_external_wires",
                "_pending_tdn_restore", "_pending_tox_restore",
                "_tdn_palette_handling", "embed_dats_in_tdn",
                "embed_storage_in_tdn"):
        assert key in STORAGE_KEYS, key
    for key in ("hover", "test_results", "git_status", "visible_count",
                "expanded_paths", "cp_summary", "envoy_running"):
        assert key in tdxn.SKIP_STORAGE_KEYS, key   # the trap is real
        assert key not in STORAGE_KEYS, key
    plan = admin.plan_release_scrub(
        [{"path": "/moonshine/warp", "tags": [],
          "storage_keys": ["_tdn_rel_path", "_tdn_external_wires",
                           "embed_dats_in_tdn", "hover", "user_setting"]},
         {"path": "/moonshine/scene", "tags": [],
          "storage_keys": ["user_setting", "test_results"]}],
        embody_tags={TDXN_TAG}, exclude_tags={EXCLUDE_TAG},
        storage_keys=STORAGE_KEYS)
    assert plan["storage"] == [
        {"path": "/moonshine/warp",
         "keys": ["_tdn_external_wires", "_tdn_rel_path",
                  "embed_dats_in_tdn"]}]


def test_colour_targets_are_the_tagged_ops_plus_anything_still_tracked():
    """EmbodyExt.Disable's rule (:3606-3631), plus the operator whose tag
    was lost but whose table row was not."""
    plan = admin.plan_release_scrub(
        [{"path": "/tagged", "tags": [TDXN_TAG], "storage_keys": []},
         {"path": "/untagged_tracked", "tags": [], "storage_keys": []},
         {"path": "/users_own", "tags": ["mine"], "storage_keys": []}],
        embody_tags={TDXN_TAG}, exclude_tags={EXCLUDE_TAG},
        storage_keys=STORAGE_KEYS,
        tracked_paths=["/untagged_tracked"])
    assert plan["colours"] == ["/tagged", "/untagged_tracked"]
    assert [e["path"] for e in plan["tags"]] == ["/tagged"]


def test_the_scrub_leaves_embodys_own_subtree_and_the_tables_alone():
    """Destroyed whole in step 4; unstoring Embody's frame-chain
    generations first would only cost the frames that remain."""
    plan = admin.plan_release_scrub(
        [{"path": EMBODY, "tags": [TDXN_TAG], "storage_keys": ["_tdn_rel_path"]},
         {"path": EMBODY + "/EnvoyExt", "tags": ["py"],
          "storage_keys": ["_tdn_rel_path"]},
         {"path": TABLE, "tags": ["tsv"], "storage_keys": []},
         {"path": "/moonshine/lib/externalizations_old", "tags": ["tsv"],
          "storage_keys": []},
         {"path": "/moonshine/warp", "tags": [TDXN_TAG],
          "storage_keys": ["_tdn_rel_path"]}],
        embody_tags={TDXN_TAG, "py", "tsv"}, exclude_tags={EXCLUDE_TAG},
        storage_keys=STORAGE_KEYS,
        tracked_paths=[EMBODY, EMBODY + "/EnvoyExt", TABLE, "/moonshine/warp"],
        embody_path=EMBODY, table_path=TABLE,
        extra_tables=["/moonshine/lib/externalizations_old"])
    assert [e["path"] for e in plan["tags"]] == ["/moonshine/warp"]
    assert [e["path"] for e in plan["storage"]] == ["/moonshine/warp"]
    assert plan["colours"] == ["/moonshine/warp"]


# -- what gets destroyed, and in what order --------------------------------

def test_the_comp_is_destroyed_before_its_sibling_table():
    plan = admin.plan_release_footprint(EMBODY, TABLE)
    assert [e["path"] for e in plan] == [EMBODY, TABLE]
    assert plan[0]["what"] == "Embody COMP"


def test_a_table_inside_the_comp_is_not_destroyed_twice():
    plan = admin.plan_release_footprint(EMBODY, EMBODY + "/externalizations")
    assert [e["path"] for e in plan] == [EMBODY]


def test_a_missing_table_leaves_only_the_comp():
    assert [e["path"] for e in admin.plan_release_footprint(EMBODY, "")] == [
        EMBODY]


def test_an_unlinked_sibling_table_is_destroyed_too():
    """createExternalizationsTable re-adopts a sibling 'externalizations'
    by NAME when the par link is lost; its text is the manifest too."""
    old = "/moonshine/lib/externalizations_old"
    plan = admin.plan_release_footprint(EMBODY, TABLE, extra_tables=[old, TABLE])
    assert [e["path"] for e in plan] == [EMBODY, TABLE, old]
    assert "unlinked" in plan[2]["what"]
    inline = admin.plan_release_inline(
        [{"path": old, "family": "DAT", "file": "old.tsv", "syncfile": True,
          "externaltox": "", "enableexternaltox": False}],
        embody_path=EMBODY, table_path=TABLE, extra_tables=[old])
    assert inline["dats"] == [] and "manifest" in inline["skipped"][0]["why"]
    composed = admin.plan_release_toe(_state(orphan_tables=[old]),
                                      save_path=TOE)
    assert [e["path"] for e in composed["footprint"]] == [EMBODY, TABLE, old]


def test_the_hook_owner_is_the_product_not_embodys_parent():
    assert admin.release_product_path(EMBODY) == "/moonshine"
    assert admin.release_product_path("/embody/Embody") == "/embody"
    assert admin.release_product_path("/Embody") == "/"


def test_a_product_path_the_collector_already_resolved_wins():
    """The derived path is a rule; the collector's is a resolved operator,
    and it falls back to root when the rule names nothing that exists."""
    plan = admin.plan_release_toe(_state(product_path="/"), save_path=TOE)
    assert plan["product_path"] == "/"


# -- privacy ---------------------------------------------------------------

def test_privacy_is_refused_loudly_without_pro():
    plan = admin.plan_release_privacy("hunter2", is_pro=False)
    assert plan["apply"] is False
    assert "Pro licence" in plan["refusal"]


def test_privacy_applies_on_pro_and_is_absent_without_a_key():
    assert admin.plan_release_privacy("hunter2", is_pro=True)["apply"] is True
    none = admin.plan_release_privacy(None, is_pro=False)
    assert none["apply"] is False and none["refusal"] == ""


def test_the_privacy_refusal_reaches_the_readiness_verdict():
    """It has to refuse BEFORE step 4 -- past that point Embody is gone and
    nothing is left to report with."""
    plan = admin.plan_release_toe(_state(is_pro=False), save_path=TOE,
                                  privacy_key="hunter2")
    assert plan["readiness"]["ready"] is False
    assert _refusal(plan, "Pro licence")


# -- the generated tail (steps 4-6) ---------------------------------------

def test_the_finish_script_destroys_then_privates_then_saves_then_quits():
    script = admin.release_finish_script(
        admin.plan_release_footprint(EMBODY, TABLE), TOE,
        privacy_key="hunter2", quit_after=True)
    compile(script, "<release_finish>", "exec")
    assert repr([EMBODY, TABLE]) in script
    order = [script.index(s) for s in
             ("_o.destroy()", "addPrivacy", "project.save", "project.quit")]
    assert order == sorted(order)


def test_the_finish_script_skips_privacy_without_a_key_and_can_stay_open():
    script = admin.release_finish_script(
        admin.plan_release_footprint(EMBODY, ""), TOE, privacy_key="",
        quit_after=False)
    compile(script, "<release_finish>", "exec")
    assert "_key = ''" in script
    assert "_quit = False" in script


def test_the_finish_script_reaches_nothing_that_step_4_destroyed():
    """It runs with no Embody: no module, no extension, no logger."""
    script = admin.release_finish_script(
        admin.plan_release_footprint(EMBODY, TABLE), TOE)
    tree = ast.parse(script)
    called = {n.func.attr for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert called <= {"destroy", "addPrivacy", "save", "quit"}
    for banned in ("ext.", "op.Embody", "mod.", "self."):
        assert banned not in script, banned


# -- the dry run changes nothing -------------------------------------------

_TREE = ast.parse(ADMIN_PY.read_text(encoding="utf-8"))


def _fn(name):
    for node in ast.walk(_TREE):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError("{} not found in embody_admin.py".format(name))


def _calls(node):
    called = set()
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call):
            continue
        if isinstance(sub.func, ast.Name):
            called.add(sub.func.id)
        elif isinstance(sub.func, ast.Attribute):
            called.add(sub.func.attr)
    return called


def test_the_preview_and_the_collector_call_no_mutating_helper():
    """Purity proved against the SOURCE, not against a mock that could
    simply have been handed a different code path -- for the preview AND
    for every read helper it reaches through _release_collect."""
    forbidden = {"_apply_release_inline", "_apply_release_scrub",
                 "_apply_release_disarm", "_release_quiet_session",
                 "_release_retire_viz", "_release_refuse_presave",
                 "export_release_toe", "release_finish_script",
                 "_runReleaseHook", "destroy", "unstore", "store", "setattr",
                 "run", "save", "quit", "addPrivacy", "resetOpColor"}
    for name in ("preview_release_toe", "_release_collect",
                 "_release_ops_census", "_release_tdxn_file_ops",
                 "_release_save_path_state", "_release_execute_dats",
                 "_release_op_errors", "_release_remaining_refs",
                 "_release_par", "_release_frame", "_release_is_pro"):
        hit = _calls(_fn(name)) & forbidden
        assert hit == set(), (name, sorted(hit))


def test_planning_never_mutates_the_state_it_was_handed():
    state = _state()
    before = copy.deepcopy(state)
    admin.plan_release_toe(state, save_path=TOE, privacy_key="hunter2")
    assert state == before


def test_the_composed_plan_answers_every_step():
    plan = admin.plan_release_toe(_state(), save_path=TOE)
    assert plan["readiness"]["ready"] is True
    assert plan["save_path"] == TOE
    assert plan["disarm"] == [("Filecleanup", "keep"),
                              ("Tdxnmode", "off"),
                              ("Tdxnstriponsave", False)]
    assert plan["product_path"] == "/moonshine"
    assert plan["hook_path"] == HOOK
    assert plan["inline"]["dats"] == ["/moonshine/network/NetworkExt"]
    assert plan["scrub"]["colours"] == ["/moonshine/network/NetworkExt",
                                        "/moonshine/warp"]
    assert plan["scrub"]["storage"] == [{"path": "/moonshine/warp",
                                         "keys": ["_tdn_rel_path"]}]
    assert [e["path"] for e in plan["footprint"]] == [EMBODY, TABLE]
    assert plan["privacy"]["apply"] is False
    assert plan["presave_hooks"] == []
    assert [e["path"] for e in plan["references"]["remaining"]] == [
        "/moonshine/media/loop"]


# -- the orchestrator, on a fake TouchDesigner -----------------------------
# The live run (2026-09-08, worktree instance) proved the order; this pins
# it in CI: quiet -> inline -> hook -> re-plan -> scrub -> viz -> one run().
# The fakes model exactly the TD surface embody_admin touches, nothing more.

from types import SimpleNamespace


class _Par:
    def __init__(self, val):
        self.val = val
        self.readOnly = False

    def eval(self):
        return self.val


class _Pars:
    def __init__(self, **pars):
        self.__dict__["_p"] = {k: _Par(v) for k, v in pars.items()}

    def __getattr__(self, name):
        par = self.__dict__["_p"].get(name)
        if par is None:
            raise AttributeError(name)
        return par

    def __setattr__(self, name, value):
        par = self.__dict__["_p"].get(name)
        if par is None or name in self.__dict__.get("_frozen", ()):
            raise AttributeError(name)
        par.val = value
        owner = self.__dict__.get("_owner")
        if owner is not None:
            owner.world.events.append(("par", owner.path, name))


class _Op:
    def __init__(self, world, path, family, type_, tags=(), storage=None,
                 **pars):
        self.world, self.path, self.family, self.type = world, path, family, type_
        self.name = path.rsplit("/", 1)[-1] or "/"
        self.tags = set(tags)
        self.storage = dict(storage or {})
        self.par = _Pars(**pars)
        self.par.__dict__["_owner"] = self
        self.color = (1.0, 1.0, 1.0)
        self.valid = True
        self.text = ""
        self.nodeX, self.nodeY = 0, 0
        self.nodeWidth, self.nodeHeight = 100, 100

    @property
    def children(self):
        prefix = self.path.rstrip("/") + "/"
        return [o for p, o in self.world.ops.items()
                if p.startswith(prefix) and "/" not in p[len(prefix):]]

    def create(self, type_, name):
        # textDAT is monkeypatched onto the module as a marker string.
        return self.world.add(self.path.rstrip("/") + "/" + name,
                              "DAT", "text")

    def destroy(self):
        self.valid = False
        for p in [q for q in self.world.ops
                  if q == self.path or q.startswith(self.path + "/")]:
            self.world.ops.pop(p, None)

    def store(self, key, value):
        self.world.events.append(("store", self.path, key))
        self.storage[key] = value

    def unstore(self, key):
        self.storage.pop(key, None)

    def fetch(self, key, default=None, search=False):
        return self.storage.get(key, default)

    def op(self, rel):
        return self.world.ops.get(self.path.rstrip("/") + "/" + rel)

    def parent(self):
        return self.world.ops.get(self.path.rsplit("/", 1)[0] or "/")

    def findChildren(self, depth=None, includeUtility=False, type=None,
                     name=None):
        prefix = self.path.rstrip("/") + "/"
        out = []
        for p, o in list(self.world.ops.items()):
            if p == self.path or not p.startswith(prefix):
                continue
            if depth == 1 and "/" in p[len(prefix):]:
                continue
            if name is not None and o.name != name:
                continue
            if type is not None and o.family != type:
                continue
            out.append(o)
        return out

    def errors(self):
        return getattr(self, "error_text", "")

    def scriptErrors(self):
        return ""

    @property
    def numRows(self):
        return len(self.world.rows) + 1


class _Envoy:
    def __init__(self, world):
        self.world = world
        self.calls = []

    def _vizCleanup(self):
        self.calls.append("cleanup")
        self.world.events.append(("viz", "", "cleanup"))

    def _purgeVizArtifacts(self):
        self.calls.append("purge")
        return 0


class _World:
    def __init__(self):
        self.ops, self.rows, self.tdxn, self.runs = {}, [], [], []
        self.hook_body, self.seen, self.events = None, {}, []

    def add(self, path, family, type_, **kw):
        self.ops[path] = _Op(self, path, family, type_, **kw)
        return self.ops[path]

    def lookup(self, path):
        return self.ops.get(path)

    def run(self, script, *args, delayFrames=0, **kw):
        self.events.append(("run", "", str(delayFrames)))
        self.runs.append((script, delayFrames))


class _Ext:
    """The slice of EmbodyExt the orchestrator reaches."""

    def __init__(self, world, hook_ok=True):
        self.world, self.hook_ok = world, hook_ok
        self.my = world.ops[EMBODY]
        self.root = world.ops["/"]
        self.Externalizations = world.ops.get(TABLE)
        self.log, self.hook_calls = [], []
        # The pulse handler's two dialog seams.
        self.dialogs, self.answers = [], []
        self.test_runner_active = False
        self._performMode = False
        self._STORAGE_CONTROL_KEYS = embodyext.EmbodyExt._STORAGE_CONTROL_KEYS

    def Log(self, message, level="INFO", details=None):
        self.log.append((level, message))

    def _testRunnerActive(self):
        return self.test_runner_active

    def _messageBox(self, title, message, buttons):
        self.dialogs.append((title, message, list(buttons)))
        return self.answers.pop(0) if self.answers else 0

    def _cellVal(self, row, col, default="", table=None):
        return self.world.rows[row - 1].get(col, default)

    def resolveOpIncludingUtility(self, path):
        return self.world.ops.get(path)

    def _getTDXNStrategyComps(self):
        return list(self.world.tdxn)

    def _findReleaseHook(self, target, name):
        hook = target.op(name)
        if hook is None or hook.family != "DAT" or hook.type != "text":
            return None
        return hook

    def _runReleaseHook(self, target, name, args):
        if self._findReleaseHook(target, name) is None:
            return (False, True)
        self.hook_calls.append(tuple(args))
        self.world.events.append(("hook", target.path, name))
        if self.world.hook_body is not None:
            self.world.hook_body(self.world)
        return (True, self.hook_ok)

    def _projectSavedOnDisk(self):
        return True

    def _resolveProjectToe(self):
        return PROJECT_TOE

    def _tdxnMode(self):
        return str(self.my.par.Tdxnmode.eval())

    def getTags(self):
        return ["tox", "py"]

    def _tdxnTags(self):
        return frozenset({TDXN_TAG, LEGACY_TAG})

    def _tdxnExcludeTags(self):
        return frozenset({EXCLUDE_TAG, LEGACY_EXCLUDE})

    def _storageSkipKeys(self):
        return (set(tdxn.SKIP_STORAGE_KEYS)
                | set(embodyext.EmbodyExt._STORAGE_SKIP_EXTRA))

    def resetOpColor(self, oper):
        self.world.events.append(("colour", oper.path, ""))
        oper.color = (0.55, 0.55, 0.55)

    def buildAbsolutePath(self, rel):
        return Path(FOLDER) / rel


NETWORK, WARP, UI = "/moonshine/network", "/moonshine/warp", "/moonshine/ui"
NETWORK_EXT = NETWORK + "/NetworkExt"
QUIESCE = "/moonshine/sources/quiesce"


def _world(hook_body=None, hook_ok=True, with_hook=True, presave_armed=False):
    w = _World()
    w.add("/", "COMP", "root")
    w.add("/moonshine", "COMP", "base")
    w.add("/moonshine/lib", "COMP", "base")
    w.add("/moonshine/sources", "COMP", "base")
    emb = w.add(EMBODY, "COMP", "base",
                storage={"_init_complete": True, "_git_root": "C:/dev"},
                Tdxnmode="full", Tdxnstriponsave=True, Filecleanup="ask",
                Version="6.2.41", Status="Enabled")
    w.add(EMBODY + "/execute", "DAT", "execute", active=True,
          projectpresave=True)
    tdxn_fake = SimpleNamespace(
        _read_existing_tdxn=lambda p: {"operators": [1, 2, 3]}, cancelled=0)
    tdxn_fake.cancelExport = lambda: setattr(tdxn_fake, "cancelled",
                                             tdxn_fake.cancelled + 1)
    emb.ext = SimpleNamespace(TDXN=tdxn_fake, Envoy=_Envoy(w))
    w.add(EMBODY + "/EmbodyExt", "DAT", "text", tags=["py"],
          file="embody/Embody/EmbodyExt.py", syncfile=True)
    w.add(TABLE, "DAT", "table", tags=["tsv"],
          file="moonshine/externalizations.tsv", syncfile=True)
    w.add(NETWORK, "COMP", "base", tags=[TDXN_TAG],
          storage={"_tdn_rel_path": "moonshine/network.tdxn",
                   "embed_dats_in_tdn": True, "user_setting": 1})
    w.add(NETWORK_EXT, "DAT", "text", tags=["py"],
          file="moonshine/network/NetworkExt.py", syncfile=True)
    w.add(WARP, "COMP", "base", tags=[TDXN_TAG],
          storage={"_tdn_rel_path": "moonshine/warp.tdxn"})
    w.add(WARP + "/glsl", "TOP", "glsl")
    w.add(UI, "COMP", "base", tags=["tox"], externaltox="moonshine/ui.tox",
          enableexternaltox=True)
    w.add("/moonshine/media", "TOP", "moviefilein",
          file="C:/dev/assets/loop.mov")
    if presave_armed:
        w.add(QUIESCE, "DAT", "execute", projectpresave=True, active=True)
    if with_hook:
        w.add(HOOK, "DAT", "text", tags=["py"],
              file="moonshine/pre_release_toe.py", syncfile=True)
    w.rows = [
        {"path": EMBODY + "/EmbodyExt", "strategy": "", "rel_file_path": "embody/Embody/EmbodyExt.py"},
        {"path": TABLE, "strategy": "", "rel_file_path": "moonshine/externalizations.tsv"},
        {"path": NETWORK, "strategy": "tdn", "rel_file_path": "moonshine/network.tdxn"},
        {"path": NETWORK_EXT, "strategy": "", "rel_file_path": "moonshine/network/NetworkExt.py"},
        {"path": WARP, "strategy": "tdn", "rel_file_path": "moonshine/warp.tdxn"},
        {"path": UI, "strategy": "tox", "rel_file_path": "moonshine/ui.tox"},
    ] + ([{"path": HOOK, "strategy": "", "rel_file_path": "moonshine/pre_release_toe.py"}] if with_hook else [])
    w.tdxn = [(NETWORK, "moonshine/network.tdxn"), (WARP, "moonshine/warp.tdxn")]
    w.hook_body = hook_body
    admin.op = w.lookup
    admin.run = w.run
    admin.project = SimpleNamespace(folder=FOLDER)
    admin.absTime = SimpleNamespace(frame=900)
    admin.licenses = SimpleNamespace(isPro=False)
    admin.DAT = "DAT"
    return w, _Ext(w, hook_ok=hook_ok)


def _snapshot(w):
    return {p: (dict(o.par.__dict__["_p"].items()) and
                {k: v.val for k, v in o.par.__dict__["_p"].items()},
                sorted(o.tags), dict(o.storage), o.color)
            for p, o in w.ops.items()}


def test_export_without_confirm_changes_nothing(tmp_path):
    w, ext = _world()
    before = _snapshot(w)
    res = admin.export_release_toe(ext, str(tmp_path / "Release.toe"))
    assert res == {"ran": False, "reason": "confirm required"}
    assert _snapshot(w) == before and w.runs == []


def test_the_preview_leaves_the_world_untouched(tmp_path):
    w, ext = _world()
    before = _snapshot(w)
    plan = admin.preview_release_toe(ext, save_path=str(tmp_path / "Release.toe"))
    assert plan["readiness"]["ready"] is True
    assert _snapshot(w) == before and w.runs == [] and ext.hook_calls == []


def test_a_refusal_touches_nothing(tmp_path):
    w, ext = _world()
    admin.absTime = SimpleNamespace(frame=10)
    before = _snapshot(w)
    res = admin.export_release_toe(ext, str(tmp_path / "Release.toe"), confirm=True)
    assert res["reason"] == "not ready" and _refusal(res, "startup window")
    assert _snapshot(w) == before and w.runs == []


def test_an_armed_presave_hook_with_no_release_hook_is_refused_up_front(tmp_path):
    """Reported with the other refusals, in the gate, before anything moves."""
    w, ext = _world(with_hook=False, presave_armed=True)
    before = _snapshot(w)
    res = admin.export_release_toe(ext, str(tmp_path / "Release.toe"), confirm=True)
    assert res["reason"] == "not ready"
    assert _refusal(res, "no pre_release_toe hook to disarm them")
    assert _refusal(res, QUIESCE)
    assert _snapshot(w) == before and w.runs == []
    # With a hook present the gate passes and the check waits for the hook.
    w2, ext2 = _world(presave_armed=True)
    plan = admin.preview_release_toe(ext2, save_path=str(tmp_path / "R2.toe"))
    assert plan["readiness"]["ready"] is True and plan["presave_hooks"] == [QUIESCE]


def test_the_export_quiets_inlines_hooks_replans_scrubs_and_schedules_the_tail(tmp_path):
    def body(w):
        dat = w.ops[NETWORK_EXT]
        w.seen["synced_during_hook"] = dat.par.syncfile.eval()
        w.seen["file_during_hook"] = dat.par.file.eval()
        w.seen["tdxnmode_during_hook"] = w.ops[EMBODY].par.Tdxnmode.eval()
        w.ops[WARP].destroy()          # a runtime chain, gone on purpose
        w.ops[NETWORK_EXT].destroy()   # and one emptied, shell kept

    w, ext = _world(hook_body=body)
    emb = w.ops[EMBODY]
    save = str(tmp_path / "Release.toe")
    res = admin.export_release_toe(ext, save, confirm=True)
    assert res["ran"] is True, res
    resolved = save.replace("\\", "/")
    assert res["save_path"] == resolved
    # step 0: the session is quiet before anything else moves
    assert emb.fetch("_suppress_dialogs") is True
    assert emb.fetch("_init_complete") is None
    assert ext._restoring_settings is True
    assert w.ops[TABLE].par.syncfile.eval() is False
    assert (emb.par.Tdxnmode.eval(), emb.par.Tdxnstriponsave.eval(),
            emb.par.Filecleanup.eval()) == ("off", False, "keep")
    assert emb.par.Status.eval() == "Disabled"
    assert w.ops[EMBODY + "/execute"].par.active.eval() is False
    # step 1 before step 2: the hook saw its DAT detached and the strip down
    assert w.seen == {"synced_during_hook": False, "file_during_hook": "",
                      "tdxnmode_during_hook": "off"}
    assert ext.hook_calls == [(resolved, "6.2.41")]
    assert HOOK not in w.ops
    # re-plan: the COMP the hook destroyed is not 'missing', the one it
    # emptied is not a shell
    assert res["touched_by_hook"] == [NETWORK, WARP]
    assert emb.ext.TDXN.cancelled == 1
    # inline: every tracked binding, and only those
    assert w.seen["file_during_hook"] == ""      # inlined before the hook
    assert w.ops[UI].par.externaltox.eval() == ""
    assert w.ops[UI].par.enableexternaltox.eval() is False
    assert w.ops[EMBODY + "/EmbodyExt"].par.syncfile.eval() is True
    assert w.ops[TABLE].par.file.eval() == "moonshine/externalizations.tsv"
    assert res["inlined"] == {"dats": 1, "comps": 1, "errors": 0}
    # scrub: Embody's tags, colours and keys off the product, its own left alone
    assert w.ops[NETWORK].tags == set() and w.ops[UI].tags == set()
    assert w.ops[NETWORK].storage == {"user_setting": 1}
    assert w.ops[NETWORK].color == (0.55, 0.55, 0.55)
    assert w.ops[EMBODY + "/EmbodyExt"].tags == {"py"}
    assert w.ops[TABLE].tags == {"tsv"}
    assert res["scrubbed"]["errors"] == 0
    assert emb.ext.Envoy.calls == ["cleanup", "purge"]
    # the untracked movie binding is reported, never touched
    assert [e["path"] for e in res["absolute_refs"]] == ["/moonshine/media"]
    assert w.ops["/moonshine/media"].par.file.eval() == "C:/dev/assets/loop.mov"
    # one deferred tail, with the footprint and the resolved path
    assert len(w.runs) == 1
    script, delay = w.runs[0]
    assert delay == admin.RELEASE_FINISH_DELAY_FRAMES
    compile(script, "<release_finish>", "exec")
    assert repr([EMBODY, TABLE]) in script and repr(resolved) in script
    # and the steps happened in THIS order: quiet, inline, hook, scrub,
    # viz, tail -- first occurrence of each marker in the event log
    ev = w.events
    first = lambda pred: next(i for i, e in enumerate(ev) if pred(e))
    quiet = first(lambda e: e == ("store", EMBODY, "_suppress_dialogs"))
    disarm = first(lambda e: e == ("par", EMBODY, "Filecleanup"))
    assert disarm < first(lambda e: e == ("par", EMBODY, "Tdxnmode"))
    inline = first(lambda e: e == ("par", NETWORK_EXT, "syncfile"))
    hook = first(lambda e: e[0] == "hook")
    colour = first(lambda e: e[0] == "colour")
    viz = first(lambda e: e[0] == "viz")
    tail = first(lambda e: e[0] == "run")
    assert quiet < disarm < inline < hook < colour < viz < tail, ev


def test_a_raising_hook_aborts_after_the_inline_pass_and_keeps_the_hook(tmp_path):
    w, ext = _world(hook_ok=False)
    res = admin.export_release_toe(ext, str(tmp_path / "Release.toe"), confirm=True)
    assert res == {"ran": False, "reason": "hook failed"}
    assert HOOK in w.ops
    assert w.ops[NETWORK_EXT].par.syncfile.eval() is False   # already inlined
    assert w.ops[NETWORK].tags == {TDXN_TAG}                    # never scrubbed
    assert w.runs == []


def test_the_post_hook_gate_refuses_an_armed_presave_hook(tmp_path):
    w, ext = _world(presave_armed=True)
    res = admin.export_release_toe(ext, str(tmp_path / "Release.toe"), confirm=True)
    assert res["reason"] == "pre-save hooks armed"
    assert res["presave_hooks"] == [QUIESCE]
    assert HOOK not in w.ops and w.runs == []


def test_an_inline_failure_aborts_before_anything_is_destroyed(tmp_path):
    """A tracked DAT that keeps its binding would ship bound: fail closed."""
    w, ext = _world()
    w.rows.append({"path": "/moonshine/odd", "strategy": "",
                   "rel_file_path": "moonshine/odd.py"})
    w.add("/moonshine/odd", "DAT", "text", syncfile=True)   # no `file` par
    w.ops["/moonshine/odd"].par.__dict__["_p"]["file"] = _Par("moonshine/odd.py")
    del w.ops["/moonshine/odd"].par.__dict__["_p"]["file"]
    # `file` reads through _release_par as '' but syncfile marks it tracked;
    # clearing raises AttributeError inside _apply_release_inline
    res = admin.export_release_toe(ext, str(tmp_path / "Release.toe"), confirm=True)
    assert res["reason"] == "inline failed" and res["inlined"]["errors"] == 1
    assert HOOK in w.ops and ext.hook_calls == [] and w.runs == []
    assert w.ops[NETWORK].tags == {TDXN_TAG}


def test_a_scrub_failure_aborts_before_anything_is_destroyed(tmp_path):
    w, ext = _world()

    def refuse(oper):
        raise RuntimeError("colour locked")
    ext.resetOpColor = refuse
    res = admin.export_release_toe(ext, str(tmp_path / "Release.toe"), confirm=True)
    assert res["reason"] == "scrub failed" and res["scrubbed"]["errors"] > 0
    assert w.runs == [] and w.ops[EMBODY].valid


def test_operator_errors_are_waived_through_the_export(tmp_path):
    w, ext = _world()
    w.ops[WARP + "/glsl"].error_text = "Shader compile failed"
    save = str(tmp_path / "Release.toe")
    refused = admin.export_release_toe(ext, save, confirm=True)
    assert refused["reason"] == "not ready" and _refusal(refused, "Shader compile")
    w, ext = _world()
    w.ops[WARP + "/glsl"].error_text = "Shader compile failed"
    res = admin.export_release_toe(ext, save, confirm=True, ignore_op_errors=True)
    assert res["ran"] is True
    assert len(res["warnings"]) == 1 and "Shader compile" in res["warnings"][0]
    assert any(lvl == "WARNING" and "Shader compile" in msg for lvl, msg in ext.log)


def test_a_privacy_key_reaches_the_tail_only_under_pro(tmp_path):
    w, ext = _world()
    admin.licenses = SimpleNamespace(isPro=True)
    res = admin.export_release_toe(ext, str(tmp_path / "Release.toe"),
                                   privacy_key="hunter2", confirm=True)
    assert res["ran"] is True and res["privacy"] is True
    script = w.runs[0][0]
    assert "_key = 'hunter2'" in script and "addPrivacy" in script
    w, ext = _world()                       # isPro False again
    res = admin.export_release_toe(ext, str(tmp_path / "R2.toe"),
                                   privacy_key="hunter2", confirm=True)
    assert res["reason"] == "not ready" and _refusal(res, "Pro licence")


def test_an_existing_save_file_is_refused_by_the_real_check(tmp_path):
    w, ext = _world()
    target = tmp_path / "Release.toe"
    target.write_bytes(b"old")
    res = admin.export_release_toe(ext, str(target), confirm=True)
    assert res["reason"] == "not ready" and _refusal(res, "already exists")
    assert admin.export_release_toe(
        ext, str(tmp_path / "missing" / "Release.toe"), confirm=True)["reason"] == "not ready"


def test_the_shell_census_reads_real_tdxn_files(tmp_path):
    """The on-disk half of the shell rule, through TDXNExt's real reader:
    `operators: []` means legitimately empty, a populated list means shell."""
    (tmp_path / "moonshine").mkdir()
    (tmp_path / "moonshine" / "network.tdxn").write_text(
        "version: '2.1'\nroot: network\noperators:\n  - name: a\n    type: baseCOMP\n",
        encoding="utf-8")
    (tmp_path / "moonshine" / "warp.tdxn").write_text(
        "version: '2.1'\nroot: warp\noperators: []\n", encoding="utf-8")
    w, ext = _world()
    ext.buildAbsolutePath = lambda rel: tmp_path / rel
    w.ops[EMBODY].ext.TDXN = SimpleNamespace(
        _read_existing_tdxn=tdxn.TDXNExt._read_existing_tdxn,
        cancelExport=lambda: None)
    for p in (WARP + "/glsl", NETWORK_EXT):
        w.ops.pop(p)                          # both COMPs now childless live
    plan = admin.preview_release_toe(ext, save_path=str(tmp_path / "R.toe"))
    gate = plan["readiness"]
    assert gate["shells"] == [NETWORK] and gate["unverifiable"] == []
    assert _refusal(gate, "stripped shells") and WARP not in gate["refusals"][0]
    assert admin._release_tdxn_file_ops(ext, "moonshine/nothere.tdxn") is None


def test_a_quiet_step_failure_aborts_before_the_inline_pass(tmp_path):
    w, ext = _world()
    w.ops[TABLE].par.__dict__["_p"].pop("syncfile")     # detach raises
    res = admin.export_release_toe(ext, str(tmp_path / "Release.toe"), confirm=True)
    assert res["ran"] is False and res["reason"].startswith("could not quiet")
    assert w.ops[NETWORK_EXT].par.syncfile.eval() is True and w.runs == []


def test_every_disarm_write_is_attempted_after_a_failure(tmp_path):
    w, ext = _world()
    w.ops[EMBODY].par.__dict__["_frozen"] = {"Tdxnmode"}   # setattr raises
    res = admin.export_release_toe(ext, str(tmp_path / "Release.toe"), confirm=True)
    assert res["reason"] == "could not quiet the session: Tdxnmode"
    emb = w.ops[EMBODY]
    assert emb.par.Filecleanup.eval() == "keep"
    assert emb.par.Tdxnstriponsave.eval() is False
    assert w.runs == []


def test_a_hook_that_saved_the_project_aborts(tmp_path):
    toe = tmp_path / "moonshine.42.toe"
    toe.write_bytes(b"x")
    w, ext = _world()
    ext._resolveProjectToe = lambda: str(toe)

    def body(w):
        toe.write_bytes(b"xx")                         # a project.save()
        import os
        os.utime(toe, (2000000000, 2000000000))
    w.hook_body = body
    res = admin.export_release_toe(ext, str(tmp_path / "Release.toe"), confirm=True)
    assert res == {"ran": False, "reason": "hook saved the project"}
    assert w.runs == [] and w.ops[NETWORK].tags == {TDXN_TAG}


def test_a_hook_that_destroys_embody_returns_instead_of_raising(tmp_path):
    w, ext = _world()
    w.hook_body = lambda w: w.ops[EMBODY].destroy()
    res = admin.export_release_toe(ext, str(tmp_path / "Release.toe"), confirm=True)
    assert res == {"ran": False, "reason": "hook destroyed Embody"}
    assert w.runs == []


def test_second_gate_warnings_are_logged(tmp_path):
    w, ext = _world()
    w.hook_body = lambda w: setattr(w.ops[UI], "error_text", "cook failed")
    res = admin.export_release_toe(ext, str(tmp_path / "Release.toe"),
                                   confirm=True, ignore_op_errors=True)
    assert res["ran"] is True and "cook failed" in res["warnings"][0]
    assert any("after the hook" in msg and "cook failed" in msg
               for lvl, msg in ext.log)


def test_td_owned_networks_are_outside_the_scans():
    w, ext = _world()
    w.add("/sys", "COMP", "base")
    w.add("/sys/quiet", "DAT", "execute", projectpresave=True, active=True)
    w.ops["/sys/quiet"].error_text = "TD's own"
    w.add("/ui", "COMP", "base")
    w.add("/ui/dialogs", "DAT", "execute", projectpresave=True, active=True)
    assert [d["path"] for d in admin._release_execute_dats(ext)] == [
        EMBODY + "/execute"]                  # Embody's own, dropped later
    assert admin._release_op_errors(ext) == []


# ==========================================================================
# The Export Release Toe PULSE handler (one parameter, two dialogs)
# ==========================================================================
# Everything the button does before it is allowed to reach export_release_toe:
# refuse a file dialog nothing can answer, preview, and stop on Cancel.


def _chooser(w, answer):
    """Stand in for ui.chooseFile and record that it was reached."""
    def chooseFile(**kwargs):
        w.events.append(("chooseFile", kwargs.get("title", "")))
        return answer
    admin.ui = SimpleNamespace(chooseFile=chooseFile)


def test_the_pulse_refuses_the_file_dialog_during_a_save_or_test(tmp_path):
    """ui.chooseFile is a native modal no test or save can answer -- refuse
    BEFORE it opens, where _messageBox would have returned its -1 default."""
    for arm in ("suppress", "testrunner"):
        w, ext = _world()
        _chooser(w, str(tmp_path / "Release.toe"))
        if arm == "suppress":
            ext.my.store("_suppress_dialogs", True)
        else:
            ext.test_runner_active = True
        before = _snapshot(w)
        res = admin.export_release_toe_handler(ext)
        assert res == {"ran": False, "reason": "dialogs suppressed"}
        assert [e for e in w.events if e[0] == "chooseFile"] == []
        assert ext.dialogs == [] and w.runs == [] and _snapshot(w) == before


def test_an_explicit_save_path_is_not_gated_by_the_dialog_guard(tmp_path):
    """The guard exists for chooseFile; a caller that passes a path opens no
    dialog, so a save/test context still reaches the preview."""
    w, ext = _world()
    ext.test_runner_active = True
    ext.answers = [0]
    res = admin.export_release_toe_handler(
        ext, save_path=str(tmp_path / "Release.toe"))
    assert res["reason"] == "cancelled" and ext.dialogs


# --- Stage 1: the prompt that comes BEFORE the file dialog ----------------


def test_the_prompt_comes_before_the_file_dialog(tmp_path):
    """Nobody should have to name a file to find out what the button does."""
    w, ext = _world()
    _chooser(w, str(tmp_path / "Release.toe"))
    ext.answers = [0]                       # Cancel the very first prompt
    before = _snapshot(w)
    res = admin.export_release_toe_handler(ext)
    assert res["reason"] == "cancelled" and res["stage"] == "pre"
    assert [e for e in w.events if e[0] == "chooseFile"] == []
    _, message, buttons = ext.dialogs[0]
    assert buttons == ["Cancel", "Choose Location..."]
    assert "What gets written:" in message
    assert "DOES NOT SURVIVE" in message
    assert "WITHOUT Project Privacy" in message
    assert "INLINE" in message and "DESTROY" in message
    assert w.runs == [] and _snapshot(w) == before


def test_a_blocker_dead_ends_before_anyone_picks_a_path(tmp_path):
    """A project that cannot be exported says so FIRST -- not after the user
    has gone hunting for a save location."""
    w, ext = _world(with_hook=False, presave_armed=True)
    _chooser(w, str(tmp_path / "Release.toe"))
    before = _snapshot(w)
    res = admin.export_release_toe_handler(ext)
    assert res["reason"] == "not ready" and res["stage"] == "pre"
    assert _refusal(res, QUIESCE)
    assert [e for e in w.events if e[0] == "chooseFile"] == []
    assert len(ext.dialogs) == 1
    _, message, buttons = ext.dialogs[0]
    # A missing hook is the fix for this blocker, so the dialog offers it.
    assert buttons == ["Cancel", "Create the hook for me"]
    assert QUIESCE in message
    assert w.runs == [] and _snapshot(w) == before


def test_a_missing_path_is_not_a_stage_one_blocker(tmp_path):
    """The pre-flight runs with NO path, so the gate's own 'a save path is
    required' must not read as a reason the project cannot be exported."""
    w, ext = _world()
    _chooser(w, str(tmp_path / "Release.toe"))
    ext.answers = [0]
    res = admin.export_release_toe_handler(ext)
    gate = admin.preview_release_toe(ext, save_path=None)["readiness"]
    assert gate["ready"] is False                       # pathless: refused
    assert gate["path_refusals"] == ["a save path is required"]
    assert res["stage"] == "pre" and res["reason"] == "cancelled"
    _, message, buttons = ext.dialogs[0]
    assert buttons == ["Cancel", "Choose Location..."]  # prompted, not blocked
    assert "a save path is required" not in message


def test_path_refusals_stay_inside_the_full_refusal_list(tmp_path):
    """Splitting them out must not remove them: every existing caller still
    reads the whole list from refusals."""
    w, ext = _world()
    gate = admin.preview_release_toe(
        ext, save_path=str(PROJECT_TOE))["readiness"]
    assert gate["path_refusals"] and gate["path_refusals"][0] in gate["refusals"]
    assert _refusal({"refusals": gate["refusals"]}, "running project itself")


# --- Stage 2: where -------------------------------------------------------


def test_a_cancelled_file_dialog_changes_nothing(tmp_path):
    w, ext = _world()
    _chooser(w, None)
    ext.answers = [1]                       # accept stage 1, then cancel
    before = _snapshot(w)
    res = admin.export_release_toe_handler(ext)
    assert res == {"ran": False, "reason": "cancelled", "stage": "choose"}
    assert len(ext.dialogs) == 1 and w.runs == [] and _snapshot(w) == before


def test_a_bare_name_from_the_save_dialog_gets_the_toe_suffix(tmp_path):
    """The gate refuses anything that is not a .toe; a Save dialog filtered
    to .toe still hands back a bare name, so add it rather than bounce."""
    w, ext = _world()
    _chooser(w, str(tmp_path / "Release"))
    ext.answers = [1, 0]
    res = admin.export_release_toe_handler(ext)
    assert res["plan"]["save_path"].endswith("Release.toe")


# --- Stage 3: the confirm that names the real file ------------------------


def test_the_destination_verdict_stops_at_its_own_dialog(tmp_path):
    """A path-specific refusal (an existing file) surfaces AFTER the choice,
    because nothing could have known it before."""
    target = tmp_path / "Release.toe"
    target.write_text("already here")
    w, ext = _world()
    _chooser(w, str(target))
    ext.answers = [1]
    before = _snapshot(w)
    res = admin.export_release_toe_handler(ext)
    assert res["reason"] == "not ready" and res["stage"] == "confirm"
    assert _refusal(res, "already exists")
    _, message, buttons = ext.dialogs[-1]
    assert buttons == ["OK"] and "that location" in message
    assert w.runs == [] and _snapshot(w) == before


def test_cancelling_the_confirm_is_the_dry_run(tmp_path):
    """Cancel is why there is no separate Preview parameter: the plan is
    logged and NOTHING is touched."""
    w, ext = _world()
    _chooser(w, str(tmp_path / "Release.toe"))
    ext.answers = [1, 0]
    before = _snapshot(w)
    res = admin.export_release_toe_handler(ext)
    assert res["reason"] == "cancelled" and res["stage"] == "confirm"
    assert res["plan"]["readiness"]["ready"]
    _, message, buttons = ext.dialogs[-1]
    assert buttons == ["Cancel", "Export and Quit"]
    assert str(tmp_path / "Release.toe").replace("\\", "/") in message
    assert w.runs == [] and ext.hook_calls == [] and _snapshot(w) == before


def test_a_suppressed_confirm_defaults_to_cancel(tmp_path):
    """_messageBox returns -1 when a dialog cannot be shown. That must read
    as Cancel at BOTH stages -- never as the Export button."""
    for answers in ([-1], [1, -1]):
        w, ext = _world()
        _chooser(w, str(tmp_path / "Release.toe"))
        ext.answers = list(answers)
        before = _snapshot(w)
        res = admin.export_release_toe_handler(ext)
        assert res["reason"] == "cancelled"
        assert w.runs == [] and _snapshot(w) == before


def test_the_confirm_button_runs_the_export(tmp_path):
    w, ext = _world()
    _chooser(w, str(tmp_path / "Release.toe"))
    ext.answers = [1, 1]                    # Choose Location..., Export
    res = admin.export_release_toe_handler(ext)
    assert res["ran"] is True and res["save_path"].endswith("Release.toe")
    assert len(w.runs) == 1                 # the destroy/save/quit tail
    # Exactly two dialogs: the prompt and the confirm. No completion box --
    # the tail quits TD and a modal would block the quit it reports.
    assert len(ext.dialogs) == 2


def test_the_confirm_names_absolute_paths_that_would_ship(tmp_path):
    """An absolute binding leaks the build machine's filesystem; the preview
    logs it, and the confirm has to say so before the one-way button."""
    w, ext = _world()
    _chooser(w, str(tmp_path / "Release.toe"))
    ext.answers = [1, 0]
    admin.export_release_toe_handler(ext)
    _, message, _ = ext.dialogs[-1]
    assert "absolute path" in message and "/moonshine/media" in message


def test_one_pulse_shows_one_dialog_even_though_a_modal_pumps_frames(tmp_path):
    """A TD modal runs the frame loop, so the pending pulse is re-delivered
    and the handler re-enters while its own dialog is still up -- measured as
    two stacked dialogs from one click. The guard makes the second a no-op."""
    w, ext = _world()
    _chooser(w, str(tmp_path / "Release.toe"))
    reentered = []

    def answer(title, message, buttons):
        ext.dialogs.append((title, message, list(buttons)))
        # Re-enter exactly as TD does: from inside the blocking dialog.
        reentered.append(admin.export_release_toe_handler(ext))
        return 0

    ext._messageBox = answer
    before = _snapshot(w)
    res = admin.export_release_toe_handler(ext)
    assert reentered == [{"ran": False, "reason": "already running"}]
    assert len(ext.dialogs) == 1                 # not two
    assert res["reason"] == "cancelled" and res["stage"] == "pre"
    assert w.runs == [] and _snapshot(w) == before


def test_the_guard_clears_after_a_raise(tmp_path):
    """A guard that latches would make the button dead until a DAT reload."""
    w, ext = _world()

    def boom(*a, **k):
        raise RuntimeError("dialog exploded")

    ext._messageBox = boom
    _chooser(w, str(tmp_path / "Release.toe"))
    try:
        admin.export_release_toe_handler(ext)
    except RuntimeError:
        pass
    assert admin._RELEASE_HANDLER_ACTIVE is False


# ==========================================================================
# The generated pre_release_toe hook (the "prep it" half)
# ==========================================================================
# The gate can only say WHY it refuses. Every fix it names belongs in a hook
# the author does not have yet, so Embody offers to write that hook.


def test_the_generated_hook_disarms_exactly_what_blocks_the_export(tmp_path):
    w, ext = _world(with_hook=False, presave_armed=True)
    plan = admin.preview_release_toe(ext, save_path=None)
    script = admin.build_release_toe_hook_script(plan, version="6.2.50",
                                                 today="2026-09-12")
    # A real disarm line, addressed RELATIVE to the product COMP.
    assert "p.op('sources/quiesce').par.projectpresave = False" in script
    assert "'/moonshine/sources/quiesce'" not in script     # no absolute path
    assert "save_path, embody_version = args[0], args[1]" in script
    assert "6.2.50" in script and "2026-09-12" in script
    compile(script, "pre_release_toe", "exec")              # it must be Python


def test_the_generated_hook_lists_absolute_paths_as_a_checklist(tmp_path):
    """Embody cannot guess the right target, so these are comments -- never
    generated code that would silently repoint someone's asset."""
    w, ext = _world(with_hook=False, presave_armed=True)
    plan = admin.preview_release_toe(ext, save_path=None)
    script = admin.build_release_toe_hook_script(plan)
    line = [l for l in script.splitlines() if "/moonshine/media" in l]
    assert line and line[0].lstrip().startswith("#")
    compile(script, "pre_release_toe", "exec")


def test_the_generated_hook_is_valid_with_nothing_to_fix(tmp_path):
    w, ext = _world(with_hook=False)
    plan = admin.preview_release_toe(ext, save_path=None)
    script = admin.build_release_toe_hook_script(plan)
    assert "None right now" in script
    compile(script, "pre_release_toe", "exec")


def test_a_reference_outside_the_product_comp_is_flagged():
    """Nothing relative reaches it, so it is absolute AND called out rather
    than inherited silently."""
    ref = admin._release_hook_ref("/elsewhere/quiesce", "/moonshine")
    assert ref.startswith("op('/elsewhere/quiesce')") and "check this" in ref
    assert admin._release_hook_ref("/moonshine/a/b", "/moonshine") == \
        "p.op('a/b')"


def test_creating_the_hook_writes_a_text_dat_on_the_product_comp(tmp_path):
    w, ext = _world(with_hook=False, presave_armed=True)
    admin.textDAT = "textDAT"
    admin.datetime = __import__("datetime")
    res = admin.create_release_toe_hook(ext)
    assert res["created"] is True and res["path"] == HOOK
    dat = w.ops[HOOK]
    assert dat.family == "DAT" and dat.type == "text"
    assert "projectpresave = False" in dat.text
    assert (dat.nodeX, dat.nodeY) != (0, 0)        # network-layout.md
    # And the export is satisfied by it: the gate now waits for the hook.
    plan = admin.preview_release_toe(ext, save_path=str(tmp_path / "R.toe"))
    assert plan["readiness"]["ready"] is True
    assert plan["hook_path"] == HOOK


def test_an_existing_hook_is_never_overwritten(tmp_path):
    """It holds the author's release recipe."""
    w, ext = _world(presave_armed=True)
    admin.textDAT = "textDAT"
    w.ops[HOOK].text = "# my careful release recipe\n"
    res = admin.create_release_toe_hook(ext)
    assert res["created"] is False and res["reason"] == "already exists"
    assert w.ops[HOOK].text == "# my careful release recipe\n"


def test_the_blocker_dialog_writes_the_hook_on_request(tmp_path):
    w, ext = _world(with_hook=False, presave_armed=True)
    admin.textDAT = "textDAT"
    _chooser(w, str(tmp_path / "Release.toe"))
    ext.answers = [1, 0]            # "Create the hook for me", then OK
    res = admin.export_release_toe_handler(ext)
    assert res["reason"] == "not ready" and res["hook"]["created"] is True
    assert HOOK in w.ops
    # It only ever WRITES the hook -- it never exports off the back of it.
    assert [e for e in w.events if e[0] == "chooseFile"] == []
    assert w.runs == []
    assert "Created " + HOOK in ext.dialogs[-1][1]


def test_declining_the_offer_writes_nothing(tmp_path):
    w, ext = _world(with_hook=False, presave_armed=True)
    admin.textDAT = "textDAT"
    _chooser(w, str(tmp_path / "Release.toe"))
    ext.answers = [0]
    res = admin.export_release_toe_handler(ext)
    assert res["reason"] == "not ready" and "hook" not in res
    assert HOOK not in w.ops and w.runs == []


def test_a_blocker_no_hook_can_fix_gets_a_plain_ok(tmp_path):
    """The offer is only made when a hook is actually the remedy."""
    w, ext = _world(with_hook=False)
    admin.absTime = SimpleNamespace(frame=10)       # startup window
    _chooser(w, str(tmp_path / "Release.toe"))
    res = admin.export_release_toe_handler(ext)
    assert res["reason"] == "not ready" and _refusal(res, "startup window")
    assert ext.dialogs[0][2] == ["OK"]
    assert HOOK not in w.ops
