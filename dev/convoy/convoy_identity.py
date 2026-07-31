"""Node identity for the embody-convoy host app.

Pure module: no I/O, no TD, no network. Persistence lives in
convoy_hoststore; policy lives here so it is testable in isolation.

TWO IDENTIFIERS, and the split is the whole design (Dylan's call,
2026-07-31; amends A-12, which put an anchor inside the project):

  runtime_id  -- WHICH RUN. Minted randomly by the TD side at every
                 startup, never stored anywhere. Two worktrees, two
                 copies, or the same project relaunched all produce
                 different runtime_ids, always. Its job is to catch
                 "TouchDesigner restarted between my request and its
                 execution" so a command cannot land on a freshly-loaded
                 session in a different state than the one the caller was
                 looking at (A-22's expected_runtime_id).

  node_id     -- WHICH PROJECT ON WHICH MACHINE. Assigned by the HOST on
                 first contact and remembered host-side, keyed on
                 (project_root, comp_path). It is the address, and it is
                 what a TD-Python approval attaches to, so it must
                 survive restarts.

NOTHING IS STORED IN THE PROJECT. The earlier design put a random
"anchor" in the Embody COMP's TDN -- a TRACKED file -- so a `git
worktree` (which this repo's own rules mandate) or a copied folder
carried the same anchor, and two live checkouts collapsed into ONE node.
Keying on the project folder instead makes every checkout distinct by
construction, because a checkout IS a different folder.

The accepted trade: renaming/moving a project folder yields a NEW
node_id, so its approvals must be granted again. That is a visible,
one-time annoyance. The alternative failure -- two live instances
sharing an identity while work silently lands on the wrong one -- is
invisible and much worse. Fail toward the annoying one.
"""

import ntpath
import posixpath
import re
import secrets
import sys

_HEX128 = re.compile(r"^[0-9a-f]{32}$")


def mint_id():
    """Random 128-bit lowercase-hex identifier (host_id and node_id)."""
    return secrets.token_hex(16)


def mint_runtime_id():
    """A fresh per-launch identifier. Never persisted, by design."""
    return "rt_" + secrets.token_hex(8)


def is_valid_id(value):
    return isinstance(value, str) and bool(_HEX128.match(value))


class IdentityError(Exception):
    """Structured identity refusal. `reason` is a stable machine code."""

    def __init__(self, reason, detail=""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


def normalize_project_root(path, platform=None):
    """Canonical form of a project folder, so one folder is one node.

    Without this, `C:\\Work\\Show`, `C:/Work/Show` and `c:\\work\\show`
    would register as three different nodes for one folder. Case is
    folded only where the filesystem is case-insensitive: on POSIX
    `/Work` and `/work` are genuinely different directories and must
    stay different nodes.
    """
    if not path or not isinstance(path, str):
        raise IdentityError("malformed_project_root", repr(path))
    platform = platform or sys.platform
    if platform == "win32":
        text = ntpath.normpath(path.replace("/", "\\"))
        return text.rstrip("\\").lower() or "\\"
    text = posixpath.normpath(path.replace("\\", "/"))
    return text.rstrip("/") or "/"


class NodeDirectory:
    """(project_root, comp_path) -> node record, as pure policy.

    Records: {node_id, project_root, comp_path, host_id, convoy_id,
    runtime_id, td_python_approved}.
    """

    def __init__(self, host_id):
        if not is_valid_id(host_id):
            raise IdentityError("malformed_host_id", repr(host_id))
        self.host_id = host_id
        self._by_location = {}      # (project_root, comp_path) -> record
        self._by_node = {}          # node_id -> record

    def register(self, project_root, comp_path, convoy_id, runtime_id=None,
                 minted_id=None, platform=None, envoy_port=None):
        """Return this location's node record, assigning an id on first
        sight. Re-registering the same location returns the SAME record
        and refreshes its runtime_id -- that is a restart, not a new node.

        minted_id exists so the store can replay persisted rows; live
        callers leave it None. envoy_port is where the node's local Envoy
        listens (loopback) so the host can dispatch a job back to it; it
        is PER-LAUNCH like runtime_id (never persisted), so a store replay
        passes None and it fills in on the node's next live registration.
        """
        root = normalize_project_root(project_root, platform=platform)
        if not comp_path or not isinstance(comp_path, str):
            raise IdentityError("malformed_comp_path", repr(comp_path))
        if not convoy_id or not isinstance(convoy_id, str):
            raise IdentityError("malformed_convoy_id", repr(convoy_id))

        location = (root, comp_path)
        existing = self._by_location.get(location)
        if existing is not None:
            if existing["convoy_id"] != convoy_id:
                # One project, one convoy. Switching is an explicit
                # operator act (remint), never a drive-by re-register.
                raise IdentityError(
                    "node_identity_conflict",
                    f"already registered to convoy "
                    f"{existing['convoy_id']!r}")
            # A NEW runtime_id here means TD restarted: same node, new
            # run. Recording it is what lets a stale request be refused.
            existing["runtime_id"] = runtime_id or mint_runtime_id()
            # Refresh the live Envoy port, but never CLEAR a known one on a
            # replay/re-register that omits it.
            if envoy_port is not None:
                existing["envoy_port"] = envoy_port
            return existing

        node_id = minted_id or mint_id()
        if node_id in self._by_node:
            raise IdentityError(
                "node_identity_conflict",
                f"node_id {node_id!r} already present in this directory")
        record = {
            "node_id": node_id,
            "project_root": root,
            "comp_path": comp_path,
            "host_id": self.host_id,
            "convoy_id": convoy_id,
            "runtime_id": runtime_id or mint_runtime_id(),
            # Where the node's Envoy listens (loopback), for dispatch-back.
            # None until the node registers live with its port.
            "envoy_port": envoy_port,
            # Fail-closed: a new identity has NO TD-Python approval.
            "td_python_approved": False,
        }
        self._by_location[location] = record
        self._by_node[node_id] = record
        return record

    def approve_td_python(self, node_id):
        record = self._by_node.get(node_id)
        if record is None:
            raise IdentityError("unknown_node", node_id)
        record["td_python_approved"] = True
        return record

    def remint(self, node_id):
        """Operator-initiated identity reset for one node.

        The old node_id is retired and a fresh one assigned to the same
        location, with TD-Python approval reset to OFF regardless of what
        the old identity held -- a new identity inherits no privileges.
        """
        old = self._by_node.pop(node_id, None)
        if old is None:
            raise IdentityError("unknown_node", node_id)
        del self._by_location[(old["project_root"], old["comp_path"])]
        fresh = self.register(old["project_root"], old["comp_path"],
                              old["convoy_id"])
        assert fresh["td_python_approved"] is False
        return fresh

    def forget(self, node_id):
        """Drop a record -- used to roll back an in-memory registration
        whose persistence failed. The directory must never outrun the
        store."""
        record = self._by_node.pop(node_id, None)
        if record is not None:
            self._by_location.pop(
                (record["project_root"], record["comp_path"]), None)
        return record

    def lookup(self, node_id):
        return self._by_node.get(node_id)

    def nodes(self):
        return list(self._by_node.values())
