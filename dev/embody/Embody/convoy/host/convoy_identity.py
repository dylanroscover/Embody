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

  node_id     -- WHICH SAVED TD LAUNCH PROFILE ON WHICH MACHINE. Assigned
                 by the HOST on first contact and remembered host-side,
                 keyed on (project_root, node_discriminator, comp_path).
                 Different .toe files in one repository therefore route
                 independently while a restart of the same file retains
                 its address and approvals. It is the address, and it is
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
_NODE_DISCRIMINATOR = re.compile(r"^nd_[0-9a-f]{32}$")
MAX_CONVOY_ID_BYTES = 128
BINDING_STATES = ("candidate", "established")

# Descriptive facts a TD node may publish for status/discovery.  These
# fields are display data only: none participates in identity, routing, or
# authorization.  Keeping one allowlist at the host-side policy boundary
# prevents a caller from turning metadata into an unbounded arbitrary JSON
# store (or quietly smuggling authority-shaped fields into host.json).
NODE_METADATA_FIELDS = (
    "toe_path",
    "toe_name",
    "node_name",
    "hostname",
    "process_id",
    "embody_version",
    "touchdesigner_version",
)
_METADATA_TEXT_LIMITS = {
    "toe_path": 4096,
    "toe_name": 512,
    "node_name": 512,
    "hostname": 255,
    "embody_version": 128,
    "touchdesigner_version": 128,
}


def mint_id():
    """Random 128-bit lowercase-hex identifier (host_id and node_id)."""
    return secrets.token_hex(16)


def mint_runtime_id():
    """A fresh per-launch identifier. Never persisted, by design."""
    return "rt_" + secrets.token_hex(8)


def is_valid_id(value):
    return isinstance(value, str) and bool(_HEX128.match(value))


def normalize_node_discriminator(value):
    """Validated stable .toe discriminator, or ``""`` for legacy rows.

    ``None`` is accepted only so a pre-discriminator host.json can replay
    without changing its existing node IDs. New TD registrations supply the
    versioned ``nd_`` value and host routing can require it independently.
    """
    if value is None:
        return ""
    if not isinstance(value, str) or not _NODE_DISCRIMINATOR.match(value):
        raise IdentityError("malformed_node_discriminator", repr(value))
    return value


def normalize_convoy_id(value):
    """Return one canonical Convoy namespace or raise IdentityError.

    The same identifier crosses persistence, admission records, signed
    envelopes and a URL segment.  Use a UTF-8 byte bound (not Python code
    points), reject invisible controls, and refuse surrounding whitespace so
    none of those layers silently canonicalizes it differently.
    """
    if not isinstance(value, str) or not value or value != value.strip():
        raise IdentityError("malformed_convoy_id", repr(value))
    try:
        raw = value.encode("utf-8")
    except UnicodeEncodeError:
        raise IdentityError("malformed_convoy_id", repr(value))
    if (len(raw) > MAX_CONVOY_ID_BYTES
            or any(byte < 0x20 or byte == 0x7f for byte in raw)):
        raise IdentityError("malformed_convoy_id", repr(value))
    return value


def normalize_binding_state(value):
    """Return a valid durable realm-binding state.

    ``candidate`` is deliberately authority-free: it may be rebound while
    automatic LAN genesis converges.  ``established`` is sticky and may not
    be changed through ordinary registration or the candidate-rebind API.
    """
    if value not in BINDING_STATES or not isinstance(value, str):
        raise IdentityError("malformed_binding_state", repr(value))
    return value


def sanitize_node_metadata(value):
    """Return a detached, bounded node-description dict.

    This is deliberately strict rather than coercive.  Registration crosses
    a process boundary, so ``str(object)`` and ``int(True)`` are surprising
    acceptance rules at the host.  Blank optional text is omitted and ASCII
    control characters are refused because this data is rendered in status
    tables and logs.  The returned dict contains only immutable scalars and
    never aliases the caller's object.
    """
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise IdentityError("malformed_metadata", "metadata must be an object")
    unknown = set(value) - set(NODE_METADATA_FIELDS)
    if unknown:
        names = ", ".join(sorted(repr(name) for name in unknown))
        raise IdentityError("malformed_metadata",
                            "unknown metadata field(s): " + names)

    clean = {}
    for name in NODE_METADATA_FIELDS:
        if name not in value or value[name] is None:
            continue
        item = value[name]
        if name == "process_id":
            if (isinstance(item, bool) or not isinstance(item, int)
                    or not (1 <= item <= 2 ** 63 - 1)):
                raise IdentityError(
                    "malformed_metadata",
                    "process_id must be a positive 64-bit integer")
            clean[name] = item
            continue
        if not isinstance(item, str):
            raise IdentityError("malformed_metadata",
                                f"{name} must be text")
        item = item.strip()
        if not item:
            continue
        if len(item) > _METADATA_TEXT_LIMITS[name]:
            raise IdentityError(
                "malformed_metadata",
                f"{name} exceeds {_METADATA_TEXT_LIMITS[name]} characters")
        if any(ord(char) < 32 or ord(char) == 127 for char in item):
            raise IdentityError("malformed_metadata",
                                f"{name} contains control characters")
        clean[name] = item
    return clean


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
    """(project_root, node_discriminator, comp_path) -> record policy.

    Records: {node_id, project_root, node_discriminator, comp_path,
    host_id, convoy_id, binding_state, runtime_id, envoy_port, wake_port, wake_token,
    remote_wake, perform_mode, wake_active, wake_grace_s,
    td_python_approved, enabled, metadata}.
    """

    def __init__(self, host_id):
        if not is_valid_id(host_id):
            raise IdentityError("malformed_host_id", repr(host_id))
        self.host_id = host_id
        self._by_location = {}      # (project_root, comp_path) -> record
        self._by_node = {}          # node_id -> record

    def register(self, project_root, comp_path, convoy_id, runtime_id=None,
                 minted_id=None, platform=None, envoy_port=None,
                 node_discriminator=None, replay=False,
                 binding_state="established"):
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
        discriminator = normalize_node_discriminator(node_discriminator)
        if not comp_path or not isinstance(comp_path, str):
            raise IdentityError("malformed_comp_path", repr(comp_path))
        convoy_id = normalize_convoy_id(convoy_id)
        binding_state = normalize_binding_state(binding_state)

        location = (root, discriminator, comp_path)
        existing = self._by_location.get(location)
        if existing is not None:
            if existing["convoy_id"] != convoy_id:
                # One project, one convoy. Switching is an explicit
                # operator act (remint), never a drive-by re-register.
                raise IdentityError(
                    "node_identity_conflict",
                    f"already registered to convoy "
                    f"{existing['convoy_id']!r}")
            if existing["binding_state"] != binding_state:
                # Promotion, candidate convergence, and any realm-id change
                # require the explicit CAS-like API below.  A routine TD
                # heartbeat must never gain authority merely by changing a
                # field in its registration payload.
                raise IdentityError(
                    "node_binding_conflict",
                    f"already registered as {existing['binding_state']!r}")
            # A NEW runtime_id here means TD restarted: same node, new
            # run. Recording it is what lets a stale request be refused.
            existing["runtime_id"] = (None if replay else
                                      (runtime_id or mint_runtime_id()))
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
            "node_discriminator": discriminator,
            "comp_path": comp_path,
            "host_id": self.host_id,
            "convoy_id": convoy_id,
            "binding_state": binding_state,
            # A replayed durable row is OFFLINE and owns no launch. Minting
            # a fake runtime here made the first real post-restart process
            # look like a competing claimant forever.
            "runtime_id": (None if replay else
                           (runtime_id or mint_runtime_id())),
            # Where the node's Envoy listens (loopback), for dispatch-back.
            # None until the node registers live with its port.
            "envoy_port": envoy_port,
            # Process-local Perform wake endpoint. These are live launch
            # facts and HostStore deliberately never persists them.
            "wake_port": None,
            "wake_token": None,
            "remote_wake": False,
            "perform_mode": False,
            "wake_active": False,
            "wake_grace_s": 60,
            # Fail-closed: a new identity has NO TD-Python approval.
            "td_python_approved": False,
            # A live registration means the project's Convoy membership
            # gate is on.  Disabling is an explicit state transition and is
            # durable even after the per-launch runtime disappears.
            "enabled": True,
            # Descriptive status data only; never identity or authority.
            "metadata": {},
        }
        self._by_location[location] = record
        self._by_node[node_id] = record
        return record

    def set_live_state(self, node_id, *, envoy_ready=None, wake_port=None,
                       wake_token=None, remote_wake=False,
                       perform_mode=False, wake_active=False,
                       wake_grace_s=60):
        """Replace one runtime's transient routing/wake advertisement.

        Validation belongs to HostApp's authenticated register route.  This
        method owns the state transition so a future replay/persistence edit
        cannot accidentally make bearer tokens durable.
        """
        record = self._by_node.get(node_id)
        if record is None:
            raise IdentityError("unknown_node", node_id)
        if envoy_ready is False:
            record["envoy_port"] = None
        record["wake_port"] = wake_port
        record["wake_token"] = wake_token
        record["remote_wake"] = bool(remote_wake)
        record["perform_mode"] = bool(perform_mode)
        record["wake_active"] = bool(wake_active)
        record["wake_grace_s"] = int(wake_grace_s)
        return self._snapshot(record)

    def clear_envoy_port(self, node_id):
        """Forget a node's live presence -- that launch has gone away.

        Its own method precisely BECAUSE register() must never clear a
        known port: a store replay (or any re-register that omits the
        port) passes envoy_port=None, and treating that as "clear it"
        would wipe a live port on every restart replay. So clearing is an
        explicit act with an explicit caller -- the node's clean exit --
        and the replay rule above stays untouched.

        The record itself SURVIVES: node_id is the address a TD-Python
        approval attaches to and must outlive a restart (the two-identifier
        split at the top of this module). Only per-launch presence (port and
        runtime id) goes.
        """
        record = self._by_node.get(node_id)
        if record is None:
            raise IdentityError("unknown_node", node_id)
        record["envoy_port"] = None
        record["runtime_id"] = None
        record["wake_port"] = None
        record["wake_token"] = None
        record["remote_wake"] = False
        record["perform_mode"] = False
        record["wake_active"] = False
        record["wake_grace_s"] = 60
        return record

    def approve_td_python(self, node_id):
        record = self._by_node.get(node_id)
        if record is None:
            raise IdentityError("unknown_node", node_id)
        record["td_python_approved"] = True
        return record

    def set_enabled(self, node_id, enabled):
        """Set durable membership intent and return a detached snapshot."""
        record = self._by_node.get(node_id)
        if record is None:
            raise IdentityError("unknown_node", node_id)
        if not isinstance(enabled, bool):
            raise IdentityError("malformed_enabled", repr(enabled))
        record["enabled"] = enabled
        return self._snapshot(record)

    def set_metadata(self, node_id, metadata):
        """Replace descriptive metadata with a validated detached copy."""
        record = self._by_node.get(node_id)
        if record is None:
            raise IdentityError("unknown_node", node_id)
        record["metadata"] = sanitize_node_metadata(metadata)
        return self._snapshot(record)

    def candidate_nodes(self):
        """Detached snapshots of every provisional local realm binding."""
        return [self._snapshot(record) for record in self._by_node.values()
                if record["binding_state"] == "candidate"]

    def rebind_candidate(self, node_id, expected_convoy_id, convoy_id,
                         binding_state="established"):
        """Compare-and-swap one candidate binding.

        The source must still be ``candidate`` and carry exactly
        ``expected_convoy_id``.  The target may remain a candidate (while a
        deterministic genesis choice settles) or become established.  An
        established row can never be switched or demoted through this API.
        """
        self.rebind_candidates(
            convoy_id, binding_state=binding_state,
            expected={node_id: expected_convoy_id})
        return self._snapshot(self._by_node[node_id])

    def rebind_candidates(self, convoy_id, binding_state="established",
                          expected=None):
        """Atomically CAS candidate rows to one authoritative binding.

        With ``expected=None`` every current candidate participates.  A
        mapping of ``{node_id: expected_convoy_id}`` narrows the transaction
        and supplies an exact compare value for each row.  All comparisons
        are validated before any record changes, so a stale caller cannot
        produce a partial in-memory convergence.  Established rows are never
        selected implicitly and explicitly naming one is a conflict.

        Returns detached snapshots for records whose id or state changed.
        """
        convoy_id = normalize_convoy_id(convoy_id)
        binding_state = normalize_binding_state(binding_state)
        if expected is None:
            expected = {
                record["node_id"]: record["convoy_id"]
                for record in self._by_node.values()
                if record["binding_state"] == "candidate"
            }
        elif not isinstance(expected, dict):
            raise IdentityError(
                "malformed_candidate_expectation",
                "expected must be a node_id-to-convoy_id mapping")
        else:
            expected = dict(expected)

        selected = []
        for node_id, expected_convoy_id in expected.items():
            expected_convoy_id = normalize_convoy_id(expected_convoy_id)
            record = self._by_node.get(node_id)
            if record is None:
                raise IdentityError("unknown_node", node_id)
            if (record["binding_state"] != "candidate"
                    or record["convoy_id"] != expected_convoy_id):
                raise IdentityError(
                    "candidate_rebind_conflict",
                    f"node {node_id!r} is {record['binding_state']!r} at "
                    f"{record['convoy_id']!r}, expected candidate at "
                    f"{expected_convoy_id!r}")
            selected.append(record)

        changed = []
        for record in selected:
            if (record["convoy_id"] == convoy_id
                    and record["binding_state"] == binding_state):
                continue
            record["convoy_id"] = convoy_id
            record["binding_state"] = binding_state
            changed.append(self._snapshot(record))
        return changed

    def abandon_established(self, adopted_convoy_id, expected=None):
        """Operator-sanctioned realm abandonment: move every established
        row NOT bound to ``adopted_convoy_id`` to CANDIDATE **at the
        adopted id**.

        This is the one deliberate exception to "an established row can
        never be demoted": it exists solely for the Join Other Realm
        recovery, where the OPERATOR has confirmed that this machine's
        own realm is the wrong one (field, 2026-08-12: a MacBook that
        self-crowned in isolation had no path onto the house realm --
        its reset always re-derived its own realm from these very rows).
        The rows take the ADOPTED id, not their old one: a crash between
        this write and the realm commit must never leave rows that a
        restart turns back into a commitment to the abandoned realm.
        (Concretely: with the previous realm still committed on disk, a
        restart re-derives THAT realm and pulls these rows back to it --
        the safe, retryable outcome; from an unbound or conflicted
        record, candidate rows at the adopted id settle onto the
        adoption. Either way the escaped realm never regains rows that
        outvote the operator; state-machine review, 2026-08-12.) The id
        is not invented -- it is the operator-confirmed argument.
        Routine paths (register, heartbeats, rebind_candidates) remain
        unable to demote anything.

        ``expected`` mirrors ``rebind_candidates``: a mapping of
        ``{node_id: current_convoy_id}`` validated in full before any
        record changes. With ``expected=None`` every established row not
        already on the adopted id participates.

        Returns detached snapshots of the moved rows.
        """
        adopted_convoy_id = normalize_convoy_id(adopted_convoy_id)
        if expected is None:
            expected = {
                record["node_id"]: record["convoy_id"]
                for record in self._by_node.values()
                if record["binding_state"] == "established"
                and record["convoy_id"] != adopted_convoy_id
            }
        elif not isinstance(expected, dict):
            raise IdentityError(
                "malformed_abandon_expectation",
                "expected must be a node_id-to-convoy_id mapping")
        else:
            expected = dict(expected)

        selected = []
        for node_id, expected_convoy_id in expected.items():
            expected_convoy_id = normalize_convoy_id(expected_convoy_id)
            record = self._by_node.get(node_id)
            if record is None:
                raise IdentityError("unknown_node", node_id)
            if (record["binding_state"] != "established"
                    or record["convoy_id"] != expected_convoy_id):
                raise IdentityError(
                    "abandon_conflict",
                    f"node {node_id!r} is {record['binding_state']!r} at "
                    f"{record['convoy_id']!r}, expected established at "
                    f"{expected_convoy_id!r}")
            selected.append(record)

        changed = []
        for record in selected:
            record["convoy_id"] = adopted_convoy_id
            record["binding_state"] = "candidate"
            changed.append(self._snapshot(record))
        return changed

    def remint(self, node_id):
        """Operator-initiated identity reset for one node.

        The old node_id is retired and a fresh one assigned to the same
        location, with TD-Python approval reset to OFF regardless of what
        the old identity held -- a new identity inherits no privileges.
        """
        old = self._by_node.pop(node_id, None)
        if old is None:
            raise IdentityError("unknown_node", node_id)
        del self._by_location[(old["project_root"],
                               old.get("node_discriminator", ""),
                               old["comp_path"])]
        fresh = self.register(old["project_root"], old["comp_path"],
                              old["convoy_id"],
                              node_discriminator=(
                                  old.get("node_discriminator") or None),
                              binding_state=old["binding_state"])
        assert fresh["td_python_approved"] is False
        return fresh

    def forget(self, node_id):
        """Drop a record -- used to roll back an in-memory registration
        whose persistence failed. The directory must never outrun the
        store."""
        record = self._by_node.pop(node_id, None)
        if record is not None:
            self._by_location.pop(
                (record["project_root"],
                 record.get("node_discriminator", ""),
                 record["comp_path"]), None)
        return record

    def lookup(self, node_id):
        return self._by_node.get(node_id)

    def lookup_location(self, project_root, comp_path,
                        node_discriminator=None, platform=None):
        """Return a detached snapshot for one canonical stable location.

        ``None`` means no record.  The returned metadata dict is copied so
        status/discovery callers cannot mutate directory policy by editing a
        response object.
        """
        root = normalize_project_root(project_root, platform=platform)
        discriminator = normalize_node_discriminator(node_discriminator)
        if not comp_path or not isinstance(comp_path, str):
            raise IdentityError("malformed_comp_path", repr(comp_path))
        record = self._by_location.get((root, discriminator, comp_path))
        return self._snapshot(record) if record is not None else None

    @staticmethod
    def _snapshot(record):
        snapshot = dict(record)
        snapshot["metadata"] = dict(record.get("metadata") or {})
        return snapshot

    def nodes(self):
        return list(self._by_node.values())
