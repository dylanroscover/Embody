"""Peer records and THE revocation predicate (Phase 3 slice 2).

NO NETWORK CODE LIVES HERE, deliberately. This module is the memory and
the decision -- who this host has admitted, who it refuses, and the one
function that answers "may I hear this peer at all". The TLS listener,
the peer client and the pin-checked handshake are slice 3, and they will
CALL authorize_peer rather than reimplement any part of it.

Two files, on purpose, and the split is the design:

  peers.json    -- host-private (0600 via convoy_platform._write_private).
                   The admission record: the pinned (host_id, fingerprint)
                   binding and everything a join learned about a peer.
                   MACHINE-WRITTEN, but re-read on change and never
                   clobbered: denylist.json sits right beside it and is
                   hand-editable by design, so an operator WILL eventually
                   edit this one too. Ignoring that edit and then silently
                   overwriting it is the worst of the three available
                   behaviours. The file says as much in its own `_note`.
  denylist.json -- SEPARATE, and NOT a key in host.json or in peers.json.
                   HUMAN-OWNED: incident response happens at 2am in a text
                   editor, and it must not require the daemon to restart.

WHY THE DENYLIST IS ITS OWN FILE. host.json's corruption policy is
"refuse to start" (it holds identity, and guessing at identity orphans
every peer relationship). That is exactly the WRONG response to a
fat-fingered denylist edit: a mistyped comma would take the whole host
down instead of the peer it names. So the denylist gets its own file with
its own policy -- and its policy is the opposite one:

  ############################################################
  # FAIL-CLOSED. AN UNREADABLE OR UNPARSEABLE denylist.json  #
  # REFUSES **ALL** PEERS, NOT NONE.                         #
  ############################################################

Read that twice, because the natural implementation is exactly backwards.
The obvious `try: load() except: entries = set()` yields an EMPTY
denylist, and an empty denylist blocks nobody -- so the single edit an
operator makes to stop an attack, made under pressure and typo'd, would
DISABLE the block instead of applying it. Every failure to understand the
file therefore refuses everything: unreadable, unparseable, wrong type,
an unknown key (a typo'd `host_id` for `host_ids` would silently block
nobody), a malformed entry, a version from a newer build. ABSENT is the
one exception, and it is not a failure to understand: no denylist file is
the normal state of a host that has never blocked anybody. Pinned by
test_convoy_peers.py::test_an_unparseable_denylist_refuses_every_peer
and the fail-open mutant it kills.

TWO INDEPENDENT KEYS. A denylist entry blocks by host_id OR by
fingerprint, and neither subsumes the other: blocking a FINGERPRINT
survives the peer changing its host_id, blocking a HOST_ID survives it
rotating its key. An operator who knows only one of the two can still
act.

ORDER IS THE INVARIANT (plan 1.4):

    denylist -> pin/admission -> envelope verification -> A-1 registry
             -> A-22 runtime -> A-17 lease

A blocked peer is refused BEFORE ANY SIGNATURE IS CHECKED. That is not
an optimisation; it is the property that makes revocation mean something
against a peer that still holds a valid key. The host app pins it with a
verifier spy that MUST NOT BE CALLED -- an assertion about the ORDER, not
merely about the outcome.

AND THE ORDER IS RE-ASKED ON EVERY DISPATCH, not only at submission. The
QUEUE OUTLIVES THE ADMISSION: a peer narrowed or blocked while its work
was in flight gets that work honestly requeued (a refused connection was
never delivered), and a one-shot revocation sweep cannot cover what
re-enters the queue after it has run -- by construction, not by
oversight. So authorize_peer is consulted again by the dispatcher, and
PeerDecision.may_mutate is part of the answer it must not throw away.
That is exactly the defect four independent reviewers reproduced against
the first cut of this module: one of them watched the host connect to a
node and deliver a mutation for a peer that had been narrowed to
read-only.
"""

import hashlib
import json
import os
import time

import convoy_hostkeys as hostkeys
import convoy_identity as identity
import convoy_platform as platform_mod

PEERS_FILE = "peers.json"
DENYLIST_FILE = "denylist.json"
PEERS_VERSION = 1
DENYLIST_VERSION = 1

# The four states a peer record may rest in.
#   pending       -- seen, recorded, NOT admitted. Refused like a stranger;
#                    the record exists so a join can show the operator what
#                    it is about to trust.
#   admitted      -- may send envelopes to this host.
#   observe_only  -- may send X0 (reads/liveness) and nothing else,
#                    REGARDLESS of local gate state (24.6). Containment for
#                    EXECUTABILITY, never for confidentiality: an
#                    observe-only peer can still read the project and watch
#                    the output. If you do not want them looking, block.
#   blocked       -- refused for every class including X0.
PEER_PENDING = "pending"
PEER_ADMITTED = "admitted"
PEER_OBSERVE_ONLY = "observe_only"
PEER_BLOCKED = "blocked"
PEER_STATES = (PEER_PENDING, PEER_ADMITTED, PEER_OBSERVE_ONLY, PEER_BLOCKED)

# The named reasons. Stable machine codes -- an operator message and an
# audit line key off these, so they are part of the contract.
REASON_BLOCKED = "peer_blocked"
REASON_UNKNOWN = "peer_unknown"
REASON_PIN_MISMATCH = "pin_mismatch"
REASON_OBSERVE_ONLY = "peer_observe_only"
REASON_NAMESPACE = "namespace_not_admitted"
PEER_REASONS = (REASON_BLOCKED, REASON_UNKNOWN, REASON_PIN_MISMATCH,
                REASON_OBSERVE_ONLY, REASON_NAMESPACE)

# Domain-separated digest of the PIN itself -- the (host_id, fingerprint)
# binding, not either half. Audit lines carry it so a re-admission (either
# half changing) is visible as a digest change without dumping certificate
# bytes into the trail. NUL-joined so no pair can be forged from another.
PEER_DIGEST_DOMAIN = b"convoy/1 peer\x00"
PEER_DIGEST_CHARS = 16

# A file whose mtime is younger than this may still be edited again
# within the same filesystem timestamp tick, so its cached parse is not
# trusted. Costs one re-read per call for a second after an edit.
MTIME_TRUST_S = 1.0

# ... and the backstop for the edits stat CANNOT SEE AT ALL. `rsync -t`,
# `cp -p`, a restore from backup and a backward clock step all preserve
# mtime, and at an identical size the stat signature is byte-identical
# for different content. A stat-only cache misses those FOREVER, not for
# a tick -- and the direction it misses in is FAIL-OPEN: the operator
# adds a host_id, sees no error, and believes they blocked someone they
# did not. So every cached parse is revalidated on a wall-clock window
# regardless of what stat says. One small read per window per file.
DENYLIST_REVALIDATE_S = 5.0
PEERS_REVALIDATE_S = 5.0

# A peer's controller ids are NAMESPACED BY ITS ORIGIN before they reach
# the lease registry. controller_id is self-asserted free text (see
# convoy_controllers' trust-boundary note), so without this a peer names
# `ctl-local`, the local operator's controller lands in the peer's
# attribution map, and the peer's OWN revocation releases the local
# operator's exclusive lease -- measured: a stranger's mutation went from
# refused to allowed. The prefix is not a secret and is not meant to be:
# it closes the REMOTE -> LOCAL direction, which is the trust boundary. A
# local caller that deliberately names `peer:...` is only confusing
# itself, and anything holding the IPC token already owns this machine.
CONTROLLER_NAMESPACE = "peer:"
MAX_CONTROLLER_TAIL = 64

# Bounds. A peer store is 2-30 machines by design (plan section 10); these
# are anti-DoS caps on a host-private file, not a product limit.
MAX_PEERS = 512
MAX_ENDPOINTS = 16
MAX_CONVOY_IDS = 16
MAX_TEXT_CHARS = 128
MAX_CERT_CHARS = 16384
MAX_DENYLIST_ENTRIES = 4096


class PeerError(Exception):
    """Structured peer refusal. `reason` is a stable machine code."""

    def __init__(self, reason, detail=""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


class PeerStoreUnreadable(PeerError):
    """peers.json is present but could not be understood.

    Every WRITE raises this rather than replacing the file. Rewriting an
    unreadable admission record would silently drop every admission and
    every block it contained -- the fail-open direction, one layer up
    from the denylist's. Reads fail closed instead: authorize_peer
    refuses every peer while this holds.
    """

    def __init__(self, detail=""):
        super().__init__("peers_unreadable", detail)


# ---------------------------------------------------------------------
# Key forms. PINNED BY KNOWN-ANSWER TESTS: a denylist entry an operator
# typed in the display (UPPERCASE) form must still block, and a changed
# normalisation would make every existing entry silently stop matching
# while every property test stayed green -- the same hole class as a
# salted fingerprint.
# ---------------------------------------------------------------------

def fold(value):
    """The MATCHING form of an identity string: stripped, lowercased.

    Deliberately does NOT validate. Denylist matching happens on this
    form so a blocked identity is refused even when the value offered is
    malformed -- a refusal must never depend on the offered value being
    well-formed.
    """
    if not isinstance(value, str):
        return ""
    return value.strip().lower()


def normalize_host_id(value):
    """A valid host_id in canonical form, or None."""
    folded = fold(value)
    return folded if identity.is_valid_id(folded) else None


def normalize_fingerprint(value):
    """A valid SPKI fingerprint in canonical form, or None.

    ONE definition of the fingerprint form in the tree: this defers to
    convoy_hostkeys, which owns it. (Importing that module is free even
    where `cryptography` is absent -- it captures its own ImportError.)
    """
    folded = fold(value)
    return folded if hostkeys.is_valid_fingerprint(folded) else None


def peer_digest(host_id, fingerprint):
    """Stable short digest of the PIN (host_id, fingerprint)."""
    material = (PEER_DIGEST_DOMAIN + fold(host_id).encode("utf-8")
                + b"\x00" + fold(fingerprint).encode("utf-8"))
    return hashlib.sha256(material).hexdigest()[:PEER_DIGEST_CHARS]


def namespaced_controller(host_id, controller_id):
    """A peer's controller id, scoped to the peer that asserted it.

    See CONTROLLER_NAMESPACE. Applied before the id reaches the lease
    registry, the heartbeat table, the attribution map, OR the delivery
    record -- all four, or the gap is the hole.
    """
    tail = str(controller_id or "")[:MAX_CONTROLLER_TAIL]
    return CONTROLLER_NAMESPACE + fold(host_id) + ":" + tail


# ---------------------------------------------------------------------
# Cached file reads, shared by both files
# ---------------------------------------------------------------------

_UNREAD = ("never-read",)


def _stat_signature(path):
    """(mtime_ns, ctime_ns, size, inode), None when ABSENT, or
    ('unstatable', err) when present-but-inaccessible.

    Absent and inaccessible are DIFFERENT answers and the whole
    fail-closed contract turns on it. ctime and inode are in the
    signature because mtime+size alone is forgeable by ordinary tools
    (see DENYLIST_REVALIDATE_S) -- they narrow the window; the
    revalidation window is what actually closes it.
    """
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return None
    except OSError as e:
        return ("unstatable", str(e))
    return (st.st_mtime_ns, getattr(st, "st_ctime_ns", 0), st.st_size,
            getattr(st, "st_ino", 0))


def _is_error_signature(signature):
    return (isinstance(signature, tuple) and len(signature) == 2
            and signature[0] == "unstatable")


def _volatility(signature, now):
    """Whether a parse of this signature may be cached at all.

    True for a file written within MTIME_TRUST_S: a second write inside
    the same timestamp tick produces an IDENTICAL signature for
    different bytes.
    """
    if signature is None or _is_error_signature(signature):
        return False
    return (now - (signature[0] / 1e9)) < MTIME_TRUST_S


class PeerDecision:
    """The answer authorize_peer gives, and the only shape callers read.

    `allowed`     -- may this peer be heard AT ALL (X0 included).
    `may_mutate`  -- may it invoke anything beyond X0. False for an
                     observe-only peer even though it is allowed, which
                     is why one boolean was never enough.
    `reason`      -- None only when fully allowed; otherwise one of
                     PEER_REASONS. An observe-only peer is ALLOWED and
                     still carries a reason, because the caller must say
                     WHY it refused the mutation it went on to refuse.
    `reversible`  -- whether this refusal can lift WITHOUT a membership
                     decision being taken back. True for the A-32
                     killswitch, a denylist entry, and a store this host
                     temporarily cannot read: all three are switches, and
                     work refused by a switch must survive it being
                     flipped back. False when a MEMBERSHIP decision was
                     taken (blocked, forgotten, narrowed, re-pinned) --
                     that work can never be served, and leaving it queued
                     makes /jobs lie. The dispatcher reads exactly this
                     to choose between skipping and terminalising.
    """

    __slots__ = ("allowed", "may_mutate", "reason", "detail", "state",
                 "host_id", "fingerprint", "digest", "reversible",
                 "admission_id")

    def __init__(self, allowed, may_mutate, reason, detail="", state=None,
                 host_id=None, fingerprint=None, reversible=False,
                 admission_id=None):
        self.allowed = bool(allowed)
        self.may_mutate = bool(may_mutate)
        self.reason = reason
        self.detail = detail
        self.state = state
        self.host_id = host_id
        self.fingerprint = fingerprint
        self.reversible = bool(reversible)
        # The record's admission LINEAGE id (see _upsert). Carried on
        # ALLOWED decisions only: the submitter stamps it onto the
        # delivery record, and the dispatch fence compares it against
        # the then-current lineage (stale_admission).
        self.admission_id = admission_id
        self.digest = peer_digest(host_id or "", fingerprint or "")

    def __bool__(self):
        return self.allowed

    def __repr__(self):
        return ("<PeerDecision allowed=%s may_mutate=%s reason=%s "
                "reversible=%s>" % (self.allowed, self.may_mutate,
                                    self.reason, self.reversible))

    def as_dict(self):
        return {"allowed": self.allowed, "may_mutate": self.may_mutate,
                "reason": self.reason, "detail": self.detail,
                "state": self.state, "peer_digest": self.digest,
                "reversible": self.reversible,
                "admission_id": self.admission_id}


# ---------------------------------------------------------------------
# The denylist
# ---------------------------------------------------------------------

class _DenylistProblem(Exception):
    """Any way of not understanding denylist.json. Every one of them
    refuses ALL peers -- see the module header."""


def _reject_duplicate_keys(pairs, problem=_DenylistProblem):
    """json.loads object hook: a DUPLICATE key refuses the file.

    Python's json keeps only the LAST value for a repeated key, so an
    operator appending a second `"host_ids": [...]` line at 2am silently
    discards the first -- the file looks right and blocks nobody. The
    2am-plausible fail-open cell.
    """
    seen = {}
    for key, value in pairs:
        if key in seen:
            raise problem(
                f"duplicate key {key!r}: JSON keeps only the last one, so "
                f"an appended line would silently discard the first")
        seen[key] = value
    return seen


PEERS_NOTE = ("Host-private Convoy peer records, written by the host app. "
              "Hand edits ARE re-read while the daemon runs and are never "
              "clobbered -- but a record this build cannot parse makes the "
              "WHOLE file unreadable, which admits nobody. To block a peer "
              "in a hurry, edit denylist.json instead: it is built for it.")


class Denylist:
    """Hand-editable blocks, re-read on mtime change, FAIL-CLOSED.

    Shape (every key optional; UNKNOWN KEYS REFUSE EVERYTHING, because a
    typo'd key name would otherwise block nobody while looking right):

        {
          "version": 1,
          "note": "free text for the human who edits this at 2am",
          "host_ids": ["<32 hex>", ...],
          "fingerprints": ["cvfp1-....", ...]
        }
    """

    KNOWN_KEYS = ("version", "note", "host_ids", "fingerprints")

    def __init__(self, path, now=None):
        self.path = path
        self._now = now or time.time
        self._host_ids = frozenset()
        self._fingerprints = frozenset()
        self._fail_closed = False
        self._fail_detail = ""
        # A sentinel no _stat_signature() result can equal, so the first
        # call always loads. None is NOT usable for that -- None means
        # "absent", a real and common state.
        self._signature = _UNREAD
        self._volatile = True
        self._loaded_at = 0.0
        self.reloads = 0

    # -- freshness ----------------------------------------------------

    def _stat(self):
        return _stat_signature(self.path)

    def _refresh(self):
        """Re-read iff the file changed -- or iff the cached parse is
        older than DENYLIST_REVALIDATE_S, whatever stat says.

        The stat is taken AFTER the read as well as before: caching a NEW
        mtime against OLD bytes would pin a stale parse until the next
        edit.
        """
        sig = self._stat()
        if (self._signature is not _UNREAD and sig == self._signature
                and not self._volatile
                and (self._now() - self._loaded_at) < DENYLIST_REVALIDATE_S):
            return
        state = None
        for _attempt in range(3):
            state = self._read(sig)
            after = self._stat()
            if after == sig:
                self._apply(state, sig)
                return
            sig = after
        # Changing under us on every attempt. The cache is not more
        # trustworthy for that -- apply the last read and stay volatile so
        # the next call reads again.
        self._apply(state, sig, volatile=True)

    def _apply(self, state, signature, volatile=None):
        self.reloads += 1
        self._signature = signature
        self._loaded_at = self._now()
        host_ids, fingerprints, fail_detail = state
        self._host_ids = host_ids
        self._fingerprints = fingerprints
        self._fail_closed = fail_detail is not None
        self._fail_detail = fail_detail or ""
        self._volatile = (volatile if volatile is not None
                          else _volatility(signature, self._loaded_at))

    # -- parsing ------------------------------------------------------

    def _read(self, signature):
        """Return (host_ids, fingerprints, fail_detail). fail_detail is
        None when the file was understood; a STRING (why) otherwise, and
        a string means EVERY peer is refused."""
        if signature is None:
            # ABSENT. Not a failure to understand -- the ordinary state of
            # a host that has never blocked anybody.
            return frozenset(), frozenset(), None
        if _is_error_signature(signature):
            return self._refuse_all(
                f"denylist at {self.path} exists but cannot be read "
                f"({signature[1]})")
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                text = f.read()
        except FileNotFoundError:
            # Deleted between the stat and the open. Absent, not broken.
            return frozenset(), frozenset(), None
        except OSError as e:
            return self._refuse_all(
                f"denylist at {self.path} could not be read ({e})")
        try:
            return self._parse(text)
        except _DenylistProblem as e:
            return self._refuse_all(
                f"denylist at {self.path} could not be understood ({e}) -- "
                f"refusing EVERY peer until it is fixed or removed")
        except ValueError as e:
            return self._refuse_all(
                f"denylist at {self.path} is not valid JSON ({e}) -- "
                f"refusing EVERY peer until it is fixed or removed")

    @staticmethod
    def _refuse_all(detail):
        return frozenset(), frozenset(), detail

    def _parse(self, text):
        if not text.strip():
            # An EMPTY file is ambiguous -- a truncated write and a
            # deliberate "block nobody" look identical -- so it refuses.
            # `{}` is the way to say "block nobody" unambiguously.
            raise _DenylistProblem(
                "the file is empty; write {} to block nobody")
        data = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
        if not isinstance(data, dict):
            raise _DenylistProblem(
                f"the top level must be a JSON object, got "
                f"{type(data).__name__}")
        unknown = sorted(set(data) - set(self.KNOWN_KEYS))
        if unknown:
            raise _DenylistProblem(
                f"unknown key(s) {unknown}; expected only "
                f"{list(self.KNOWN_KEYS)}. A mistyped key would block "
                f"NOBODY while looking correct, so it refuses everybody "
                f"instead")
        version = data.get("version", DENYLIST_VERSION)
        if isinstance(version, bool) or not isinstance(version, int):
            raise _DenylistProblem("version must be an integer")
        if version != DENYLIST_VERSION:
            # EQUALITY, not `>`. A `0` or a `-1` is not a version this
            # build understands either, and reading an unrecognised one
            # as "close enough" is a fail-open cell in a file whose whole
            # contract is that nothing is close enough.
            raise _DenylistProblem(
                f"version {version} is not v{DENYLIST_VERSION}; this build "
                f"will not guess what an unrecognised version means")
        note = data.get("note", "")
        if not isinstance(note, str):
            raise _DenylistProblem("note must be a string")
        host_ids = self._entries(data, "host_ids", normalize_host_id,
                                 "a 32-character lowercase hex host_id")
        fingerprints = self._entries(data, "fingerprints",
                                     normalize_fingerprint,
                                     "a cvfp1-... fingerprint")
        return host_ids, fingerprints, None

    @staticmethod
    def _entries(data, key, normalizer, shape):
        raw = data.get(key, [])
        # `null` is NOT an absent key: it is a value the operator wrote,
        # and coercing it to [] blocked nobody while looking deliberate.
        if not isinstance(raw, list):
            raise _DenylistProblem(
                f"{key} must be a list, got {type(raw).__name__}")
        if len(raw) > MAX_DENYLIST_ENTRIES:
            raise _DenylistProblem(
                f"{key} has {len(raw)} entries (max "
                f"{MAX_DENYLIST_ENTRIES})")
        out = set()
        for item in raw:
            if not isinstance(item, str):
                raise _DenylistProblem(
                    f"{key} entry {item!r} is not a string")
            value = normalizer(item)
            if value is None:
                # A MALFORMED ENTRY REFUSES EVERYTHING rather than being
                # skipped: skipping it means the operator believes they
                # blocked someone they did not. Use "note" for prose.
                raise _DenylistProblem(
                    f"{key} entry {item!r} is not {shape}; an entry that "
                    f"cannot match would leave its target UNBLOCKED")
            out.add(value)
        return frozenset(out)

    # -- the question -------------------------------------------------

    def blocks(self, host_id, fingerprint):
        """(blocked, detail). Both keys are consulted INDEPENDENTLY."""
        self._refresh()
        if self._fail_closed:
            return True, self._fail_detail
        folded_host = fold(host_id)
        if folded_host and folded_host in self._host_ids:
            return True, (f"host_id {folded_host} is on this host's "
                          f"denylist ({self.path})")
        folded_fp = fold(fingerprint)
        if folded_fp and folded_fp in self._fingerprints:
            return True, (f"fingerprint {folded_fp} is on this host's "
                          f"denylist ({self.path})")
        return False, ""

    def snapshot(self):
        """What is currently in force, for /peers and the audit trail."""
        self._refresh()
        return {"path": self.path,
                "fail_closed": self._fail_closed,
                "detail": self._fail_detail,
                "host_ids": sorted(self._host_ids),
                "fingerprints": sorted(self._fingerprints)}

    def add(self, host_id=None, fingerprint=None):
        """Append identities to denylist.json, creating it if absent.

        The programmatic half of the hand-editable contract, added
        because the documented realm-conflict recovery ('block the
        offending sender, then reset') was impossible through
        /peers/block for exactly the sender class that CAUSES conflicts
        -- an un-admitted host has no peer record and the state setter
        404s (field incident 2026-08-12). Values are stored in FOLDED
        form (the matching form); a fail-closed file refuses the write
        rather than replacing content the operator meant to keep.
        """
        folded_host = fold(host_id)
        folded_fp = fold(fingerprint)
        if not folded_host and not folded_fp:
            raise PeerError("denylist_entry_empty",
                            "a denylist entry needs a host_id or a "
                            "fingerprint")
        # Validate BEFORE writing: the loader refuses a file containing
        # any entry it cannot match, and a refused file fails CLOSED --
        # so appending one malformed value here would block EVERY peer
        # on this host. Refuse the entry instead.
        if folded_host and normalize_host_id(folded_host) is None:
            raise PeerError(
                "denylist_entry_malformed",
                f"{host_id!r} is not a 32-character lowercase hex host_id")
        if folded_fp and normalize_fingerprint(folded_fp) is None:
            raise PeerError(
                "denylist_entry_malformed",
                f"{fingerprint!r} is not a cvfp1-... fingerprint")
        self._refresh()
        if self._fail_closed:
            raise PeerError(
                "denylist_unreadable",
                "denylist.json is fail-closed (%s); fix the file by hand "
                "before appending to it" % (self._fail_detail,))
        host_ids = set(self._host_ids)
        fingerprints = set(self._fingerprints)
        if folded_host:
            host_ids.add(folded_host)
        if folded_fp:
            fingerprints.add(folded_fp)
        # PRESERVE the operator's note -- the field the class docstring
        # promises to the 2am hand-editor. Only fall back to a canned
        # explanation when none exists.
        note = ""
        try:
            with open(self.path, "r", encoding="utf-8") as stream:
                existing = json.load(stream)
            if isinstance(existing, dict) and isinstance(
                    existing.get("note"), str):
                note = existing["note"]
        except (OSError, ValueError):
            pass
        payload = {
            "version": 1,
            "note": note or ("Hand-editable. Blocks by host_id OR "
                             "fingerprint, matched case-insensitively."),
            "host_ids": sorted(host_ids),
            "fingerprints": sorted(fingerprints),
        }
        # Unpredictable temp name (the predictable-suffix hole
        # _write_private documents), atomic replace, then VERIFY by
        # re-reading through the normal loader -- a write that did not
        # land must be an error, not a silent no-op.
        tmp = "%s.tmp-%s" % (self.path, os.urandom(4).hex())
        try:
            with open(tmp, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, indent=1, sort_keys=True)
                stream.write("\n")
            os.replace(tmp, self.path)
        except OSError as e:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise PeerError("denylist_write_failed",
                            "%s: %s" % (type(e).__name__, e))
        self._signature = _UNREAD          # force a reload on next ask
        snapshot = self.snapshot()
        if ((folded_host and folded_host not in snapshot["host_ids"])
                or (folded_fp
                    and folded_fp not in snapshot["fingerprints"])):
            raise PeerError(
                "denylist_write_failed",
                "the entry did not survive the reload (%s)"
                % (snapshot.get("detail") or "unknown cause"))
        return snapshot


# ---------------------------------------------------------------------
# The peer store
# ---------------------------------------------------------------------

_RECORD_FIELDS = ("host_id", "fingerprint", "cert_pem", "display_name",
                  "state", "admitted_at", "admitted_via", "pin_first_seen",
                  "last_seen", "endpoints", "convoy_ids", "clock_offset_s",
                  "admission_id")


class PeerStore:
    """Admission records + the denylist, and the one decision function.

    NOT internally locked, exactly like convoy_controllers: the host app
    serializes every call under its single lock. Stated as an invariant
    because the read-modify-write in each mutator depends on it.
    """

    def __init__(self, data_dir, now=None, audit=None):
        self.data_dir = data_dir
        self.path = os.path.join(data_dir, PEERS_FILE)
        self._now = now or time.time
        # audit(event, detail) -- optional, and ALWAYS best-effort: an
        # audit failure may never alter a decision or a stored record.
        self._audit_sink = audit
        self.denylist = Denylist(os.path.join(data_dir, DENYLIST_FILE),
                                 now=self._now)
        self._peers = {}
        self._killswitch = {"engaged": False, "at": None, "reason": ""}
        # The REASON peers.json could not be read, or None. Distinct from
        # "no peers": absent is an empty store, unreadable is fail-closed.
        self.unreadable = None
        # peers.json was ABSENT at the last load. An empty store -- the
        # ordinary state of a fresh host -- but the dispatch re-check must
        # not read it as a MEMBERSHIP decision: a deleted or
        # AV-quarantined file looks exactly like this, and queued peer
        # work proves an admission was once granted (see authorize_peer).
        self.absent = False
        self._unreadable_noted = None
        self._signature = _UNREAD
        self._volatile = True
        self._loaded_at = 0.0
        self.reloads = 0
        self._ensure_current()

    # -- freshness ----------------------------------------------------
    #
    # peers.json is MACHINE-OWNED, and it is re-read on change anyway.
    # Two reasons, both learned the hard way:
    #   1. `unreadable` used to be sticky for the process lifetime, so
    #      repairing the file did nothing until the daemon restarted.
    #   2. denylist.json sits right beside it and IS hand-editable by
    #      design, which actively invites the operator to edit this one
    #      too. Ignoring that edit at runtime and then SILENTLY CLOBBERING
    #      it on the next write is the worst of the three possible
    #      behaviours. So: an external edit is honoured, and every mutator
    #      re-reads before its read-modify-write so it can never erase one.
    # The file says so itself, in `_note`.

    def _ensure_current(self):
        signature = _stat_signature(self.path)
        if (self._signature is not _UNREAD and signature == self._signature
                and not self._volatile
                and (self._now() - self._loaded_at) < PEERS_REVALIDATE_S):
            return
        for _attempt in range(3):
            self._load(signature)
            after = _stat_signature(self.path)
            if after == signature:
                self._volatile = _volatility(signature, self._loaded_at)
                return
            signature = after
        self._volatile = True

    # -- audit --------------------------------------------------------

    def _audit(self, event, detail):
        if self._audit_sink is None:
            return
        try:
            self._audit_sink(event, detail)
        except Exception:
            pass        # the trail is evidence, never a control path

    # -- disk ---------------------------------------------------------

    def _load(self, signature=None):
        self.reloads += 1
        self._signature = signature
        self._loaded_at = self._now()
        self.unreadable = None
        self.absent = False
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                text = f.read()
        except FileNotFoundError:
            self._peers = {}
            self._killswitch = {"engaged": False, "at": None, "reason": ""}
            self._unreadable_noted = None
            self.absent = True
            return                  # absent: a host that admitted nobody
        except OSError as e:
            self._unreadable(f"{self.path} could not be read ({e})")
            return
        try:
            data = json.loads(text, object_pairs_hook=(
                lambda pairs: _reject_duplicate_keys(
                    pairs, lambda m: PeerError("duplicate_key", m))))
        except PeerError as e:
            self._unreadable(f"{self.path}: {e.detail}")
            return
        except ValueError as e:
            self._unreadable(f"{self.path} is not valid JSON ({e})")
            return
        if not isinstance(data, dict):
            self._unreadable(f"{self.path} must hold a JSON object")
            return
        version = data.get("version", PEERS_VERSION)
        if isinstance(version, bool) or not isinstance(version, int) \
                or version != PEERS_VERSION:
            self._unreadable(
                f"{self.path} is v{version!r}; this build understands "
                f"v{PEERS_VERSION} and will not guess")
            return
        raw = data.get("peers", {})
        if not isinstance(raw, dict):
            self._unreadable(f"{self.path}: 'peers' must be an object")
            return
        peers = {}
        for host_id, record in raw.items():
            try:
                clean = _coerce_record(host_id, record)
            except PeerError as e:
                # ONE malformed record makes the WHOLE store unreadable.
                # Dropping it would be fail-open the moment the dropped
                # record was a BLOCKED one.
                self._unreadable(f"{self.path}: {e.detail}")
                return
            peers[clean["host_id"]] = clean
        kill = data.get("killswitch", {"engaged": False})
        if not isinstance(kill, dict) or not isinstance(
                kill.get("engaged", False), bool):
            self._unreadable(f"{self.path}: 'killswitch' is malformed")
            return
        self._peers = peers
        self._killswitch = {
            "engaged": bool(kill.get("engaged", False)),
            "at": kill.get("at"),
            "reason": str(kill.get("reason", ""))[:MAX_TEXT_CHARS]}
        # A successful load CLEARS the transition marker, so a later break
        # -- even one with the same message -- is audited afresh.
        self._unreadable_noted = None

    def _unreadable(self, detail):
        # FAIL CLOSED: no peer is admitted while this holds (authorize_peer
        # refuses every one by name), and no write may replace the file.
        # The killswitch is left ALONE rather than forced on -- a peer
        # refused because this host cannot read its own records deserves
        # to be told that, not a killswitch message that is not true.
        self.unreadable = detail
        self._peers = {}
        # ON TRANSITION ONLY. _load now re-runs on every revalidation
        # window, so auditing each one would grow audit.jsonl forever
        # while the file stays broken -- the same unbounded-trail trap
        # _note_dispatch_event exists to avoid on the drain path.
        if self._unreadable_noted != detail:
            self._unreadable_noted = detail
            self._audit("peers_unreadable", {"detail": detail[:256]})

    def _write(self):
        if self.unreadable:
            raise PeerStoreUnreadable(self.unreadable)
        payload = {"version": PEERS_VERSION,
                   "_note": PEERS_NOTE,
                   "killswitch": self._killswitch,
                   "peers": self._peers}
        platform_mod._write_private(
            self.path, json.dumps(payload, indent=1, sort_keys=True) + "\n")
        # Remember OUR OWN write, so the next read does not treat it as
        # somebody else's edit and re-parse it.
        self._signature = _stat_signature(self.path)
        self._loaded_at = self._now()
        self._volatile = _volatility(self._signature, self._loaded_at)
        self.absent = False

    def quarantine(self):
        """Move an UNREADABLE peers.json aside and start empty. Returns
        the path the damaged file was kept at.

        IN-BAND RECOVERY, and the only one that exists. An unreadable
        store refuses every write (a rewrite would drop the admissions
        AND the blocks it held), which left the operator with no route
        at all -- no admit, no block, no forget -- and a host-private
        file to hand-edit at 2am. That is strictly worse than the
        denylist, which was split out precisely so a bad edit could not
        brick the host.

        It DESTROYS MEMBERSHIP, so it refuses a store the host can still
        read, and it never deletes: the damaged file is the only record
        of what was admitted, and an operator may still want to read it.
        """
        self._ensure_current()
        if not self.unreadable:
            raise PeerError(
                "peers_readable",
                "this host can read its peer records; quarantine only ever "
                "runs against a file it has already refused to use")
        kept = f"{self.path}.{int(self._now())}.corrupt"
        try:
            os.replace(self.path, kept)
        except OSError as e:
            raise PeerError("quarantine_failed", str(e))
        self._peers = {}
        # A clean, READABLE slate. The caller (quarantine_peers) then
        # re-engages the A-32 killswitch through set_killswitch so it is
        # PERSISTED -- setting it here would be clobbered the moment the
        # store re-read (the corrupt file was renamed away, so the next
        # load sees an absent file and resets to the disengaged default).
        # Holding the emergency stop across an incident-recovery is an
        # app-level policy; the store primitive just returns to empty.
        self._killswitch = {"engaged": False, "at": None, "reason": ""}
        self.unreadable = None
        self.absent = True
        self._signature = _UNREAD
        self._audit("peers_quarantined", {"kept_at": kept})
        return kept

    # -- reads --------------------------------------------------------

    def get(self, host_id):
        self._ensure_current()
        key = normalize_host_id(host_id)
        record = self._peers.get(key) if key else None
        return dict(record) if record else None

    def peers(self):
        self._ensure_current()
        return [dict(self._peers[k]) for k in sorted(self._peers)]

    def pinned_fingerprint(self, host_id):
        """The fingerprint this host has PINNED for a host_id, or None.

        The re-check paths (dispatch, drain) know only the origin host_id;
        this is how they obtain the other half of the pin to authorize
        with. It never invents one -- an unknown host yields None, which
        authorize_peer refuses.
        """
        self._ensure_current()
        record = self._peers.get(normalize_host_id(host_id) or "")
        return record["fingerprint"] if record else None

    def find_by_fingerprint(self, fingerprint):
        value = normalize_fingerprint(fingerprint)
        if value is None:
            return None
        for record in self._peers.values():
            if record["fingerprint"] == value:
                return dict(record)
        return None

    def killswitch(self):
        """The killswitch AS IT IS IN FORCE -- not merely as stored.

        An unreadable peers.json refuses every peer, so reporting
        `engaged: false` told the operator the exact opposite of what the
        host was doing (and then set_killswitch raised, so they could not
        "fix" the thing that was not broken either). Reporting effect
        rather than storage is the only honest answer on a status route.
        """
        self._ensure_current()
        if self.unreadable:
            return {"engaged": True, "at": None, "forced": True,
                    "reason": f"peer records unreadable ({self.unreadable}) "
                              f"-- every peer is refused"}
        state = dict(self._killswitch)
        state["forced"] = False
        return state

    # -- THE decision -------------------------------------------------

    def authorize_peer(self, host_id, fingerprint, convoy_id=None):
        """May this peer be heard, and how much of it?

        ``convoy_id`` is optional only for the host-level TLS/session
        check.  Any operation that names or reveals namespace-owned state
        MUST pass it.  A host admission is not a wildcard namespace grant:
        the requested Convoy must be present in the peer record's explicit
        ``convoy_ids`` set.  Keeping the host-level and namespace-level
        checks in this one predicate prevents a new peer route from
        accidentally authorizing by certificate alone.

        THE ORDER IS THE INVARIANT (plan 1.4), and this function owns the
        first two steps of it:

          1. DENYLIST (and the A-32 killswitch, which is the same
             predicate applied to every peer at once). Consulted FIRST,
             on the RAW offered values, before anything is validated and
             LONG before any signature is verified -- a blocked peer must
             be refused while still holding a perfectly valid key.
          2. PIN + ADMISSION. The pin is the BINDING (host_id,
             fingerprint); a change to either half is a re-admission
             event, never an auto-update.

        Steps 3-6 (envelope verification, A-1 registry, A-22 runtime,
        A-17 lease) belong to the host app and run only after this
        returns allowed.

        BRANCH ORDER INSIDE STEP 2 IS ALSO LOAD-BEARING: the RECORD is
        looked up before the fingerprint is validated. The re-check paths
        pass the PINNED fingerprint, which is None for a host that is not
        on file -- so validating the fingerprint first made "has not been
        admitted" unreachable from that caller and told an operator their
        perfectly valid 32-hex host_id was malformed while they debugged
        a stuck queue.
        """
        self._ensure_current()
        # --- step 1: denylist / killswitch -------------------------------
        # REVERSIBLE, all of it: a switch that lifts without any
        # membership being taken back. The dispatcher relies on that to
        # skip rather than terminalise.
        if self._killswitch.get("engaged"):
            return self._refusal(
                REASON_BLOCKED,
                "the LAN killswitch is engaged on this host: every peer is "
                "refused until it is released"
                + (f" ({self._killswitch.get('reason')})"
                   if self._killswitch.get("reason") else ""),
                host_id, fingerprint, reversible=True)
        blocked, detail = self.denylist.blocks(host_id, fingerprint)
        if blocked:
            return self._refusal(REASON_BLOCKED, detail, host_id,
                                 fingerprint, reversible=True)

        # --- step 2: pin + admission -------------------------------------
        if self.unreadable:
            # Also reversible: repairing or quarantining the file restores
            # every admission. Work must survive that.
            return self._refusal(
                REASON_UNKNOWN,
                f"this host cannot read its own peer records "
                f"({self.unreadable}); no peer is admitted until it can",
                host_id, fingerprint, reversible=True)
        key = normalize_host_id(host_id)
        if key is None:
            return self._refusal(
                REASON_UNKNOWN,
                f"{str(host_id)[:64]!r} is not a host_id (32 lowercase hex "
                f"characters)", host_id, fingerprint)
        record = self._peers.get(key)
        if record is None:
            if self.absent:
                # The whole FILE is absent, not merely this record. For a
                # fresh host that has never admitted anybody this is the
                # ordinary state -- and then no queued peer work exists
                # to care. At the dispatch re-check, though, absence is
                # indistinguishable from a deleted or AV-quarantined
                # store, and queued peer work PROVES an admission was
                # once granted -- so the refusal is REVERSIBLE, exactly
                # like unreadable: restoring the file (or re-admitting)
                # must bring the work back, and a burn would hand a
                # file deletion the authority of a membership decision
                # nobody took. A genuine forget() REWRITES the file, so
                # that refusal stays irreversible below.
                return self._refusal(
                    REASON_UNKNOWN,
                    f"host_id {key} is not admitted: no peer records "
                    f"exist on this host ({self.path} is absent). If "
                    f"peers WERE admitted here, the store file has been "
                    f"deleted -- restore it or re-admit",
                    key, fingerprint, reversible=True)
            other = self.find_by_fingerprint(fingerprint)
            if other is not None:
                # The KEY is one we pinned -- under a DIFFERENT host_id.
                # That is a broken binding, not a stranger, and saying so
                # is what tells an operator to compare out of band.
                return self._refusal(
                    REASON_PIN_MISMATCH,
                    f"key {fold(fingerprint)} is pinned to host_id "
                    f"{other['host_id']} on this host, but it was offered "
                    f"as {key}; compare fingerprints out of band and "
                    f"re-admit", key, fingerprint, state=other["state"])
            return self._refusal(
                REASON_UNKNOWN,
                f"host_id {key} has not been admitted on this host",
                key, fingerprint)
        pin = normalize_fingerprint(fingerprint)
        if pin is None:
            return self._refusal(
                REASON_PIN_MISMATCH,
                f"host {key} presented no valid key (a fingerprint must be "
                f"a cvfp1-... SPKI fingerprint); this host pinned "
                f"{record['fingerprint']}", key, fingerprint,
                state=record["state"])
        if record["fingerprint"] != pin:
            # THE PIN IS NEVER AUTO-UPDATED -- not here, not on repeated
            # mismatch, not on a rotation hint.
            return self._refusal(
                REASON_PIN_MISMATCH,
                f"host {key} presented key {pin}; this host pinned "
                f"{record['fingerprint']}. Either that peer rotated its "
                f"Convoy identity (re-admit after comparing fingerprints "
                f"out of band) or something is impersonating it",
                key, pin, state=record["state"])
        state = record["state"]
        if state == PEER_BLOCKED:
            return self._refusal(
                REASON_BLOCKED,
                f"host {key} is blocked on this host", key, pin, state=state)

        if convoy_id is not None:
            try:
                namespace = identity.normalize_convoy_id(convoy_id)
            except identity.IdentityError:
                return self._refusal(
                    REASON_NAMESPACE,
                    "the request did not name a valid Convoy namespace",
                    key, pin, state=state)
            allowed_namespaces = record.get("convoy_ids") or ()
            if namespace not in allowed_namespaces:
                return self._refusal(
                    REASON_NAMESPACE,
                    f"host {key} is not admitted to Convoy {namespace!r}",
                    key, pin, state=state)
        if state == PEER_OBSERVE_ONLY:
            # ALLOWED, and still carrying a reason: the caller must refuse
            # anything past X0 and name WHY. The admission lineage rides
            # along -- narrowing does not break it (reads keep working,
            # which is the entire point of the state).
            return PeerDecision(
                True, False, REASON_OBSERVE_ONLY,
                f"host {key} is observe-only: reads are permitted, every "
                f"mutation is refused regardless of local gate state",
                state=state, host_id=key, fingerprint=pin,
                admission_id=record.get("admission_id"))
        if state == PEER_ADMITTED:
            return PeerDecision(True, True, None, "", state=state,
                                host_id=key, fingerprint=pin,
                                admission_id=record.get("admission_id"))
        # pending, or any state a future build wrote that this one does
        # not know: not admitted, therefore refused.
        return self._refusal(
            REASON_UNKNOWN,
            f"host {key} is recorded but not admitted (state {state!r})",
            key, pin, state=state)

    @staticmethod
    def _refusal(reason, detail, host_id, fingerprint, state=None,
                 reversible=False):
        """Shape a refusal. DELIBERATELY SILENT -- authorize_peer is a
        PURE decision and never writes an audit line of its own.

        Two reasons. It is called on the hot re-check path (every drain
        pass re-authorizes every peer-originated job), so auditing here
        would let a single revoked peer's queue grow audit.jsonl without
        bound; and an audit sink that raised would then be able to change
        a decision. The CALLERS audit, with their own dedupe.
        """
        return PeerDecision(False, False, reason, detail, state=state,
                            host_id=fold(host_id) or None,
                            fingerprint=fold(fingerprint) or None,
                            reversible=reversible)

    # -- mutations ----------------------------------------------------

    def record_peer(self, host_id, fingerprint, display_name="",
                    endpoints=None, convoy_ids=None, cert_pem=None,
                    clock_offset_s=None):
        """Remember a peer WITHOUT admitting it (state pending).

        What a join writes before the operator has confirmed anything.
        Nothing here grants any reach.
        """
        return self._upsert(host_id, fingerprint, PEER_PENDING,
                            display_name=display_name, endpoints=endpoints,
                            convoy_ids=convoy_ids, cert_pem=cert_pem,
                            clock_offset_s=clock_offset_s,
                            admitted_via=None)

    def admit(self, host_id, fingerprint, admitted_via="manual",
              display_name="", endpoints=None, convoy_ids=None,
              cert_pem=None, clock_offset_s=None):
        """Admit a peer against an EXPLICIT fingerprint.

        The fingerprint is mandatory, always, even for a peer already on
        file: admission is consent to a BINDING, and an admit that took
        the pin from whatever the peer last offered would be a pin
        auto-update wearing a different name.
        """
        record = self._upsert(host_id, fingerprint, PEER_ADMITTED,
                              display_name=display_name,
                              endpoints=endpoints, convoy_ids=convoy_ids,
                              cert_pem=cert_pem,
                              clock_offset_s=clock_offset_s,
                              admitted_via=admitted_via)
        return record

    def observe(self, host_id):
        """Narrow an existing peer to observe-only."""
        return self._set_state(host_id, PEER_OBSERVE_ONLY, "peer_observe")

    def block(self, host_id):
        """Block an existing peer: every class, including X0."""
        return self._set_state(host_id, PEER_BLOCKED, "peer_blocked")

    def forget(self, host_id):
        """Drop the identity AND the pin. Returns the dropped record.

        Distinct from block: a blocked peer is still pinned (so an
        impersonator is still detected as pin_mismatch), a forgotten one
        is a stranger again and its next join is a fresh TOFU decision.
        """
        self._ensure_current()
        if self.unreadable:
            raise PeerStoreUnreadable(self.unreadable)
        key = normalize_host_id(host_id)
        if key is None or key not in self._peers:
            raise PeerError("unknown_peer",
                            f"no peer record for {str(host_id)[:64]!r}")
        record = self._peers.pop(key)
        self._write()
        self._audit("peer_forgotten",
                    {"host_id": key,
                     "peer_digest": peer_digest(key, record["fingerprint"]),
                     "previous_state": record["state"]})
        return record

    def touch_seen(self, host_id, when=None):
        """Record contact on the peer record. Returns whether it landed.

        BEST-EFFORT BY CONTRACT: it never raises and never decides
        anything. `last_seen` is an operator convenience, and a failed
        write of it must never be able to interrupt a request.

        Its caller is slice 3's CONNECTION ACCEPT, deliberately -- once
        per connection, where contact is actually observed. Calling it
        per envelope would put an atomic rewrite of peers.json on the hot
        path of every request to buy a slightly fresher timestamp.
        """
        key = normalize_host_id(host_id)
        if key is None or key not in self._peers or self.unreadable:
            return False
        self._peers[key]["last_seen"] = when if when is not None \
            else self._now()
        try:
            self._write()
        except Exception:
            return False
        return True

    def set_killswitch(self, engaged, reason=""):
        """A-32: the same predicate applied to ALL peers at once.

        REVERSIBLE, and it unwinds NO membership: every pin, admission
        and observe-only narrowing stays exactly as it was, so releasing
        the switch restores the mesh without re-admitting anybody.

        ENGAGING works even when peers.json is unreadable, because it is
        already true in effect -- every peer is refused -- and an
        emergency stop that answers an operator with an exception during
        an incident is not an emergency stop. RELEASING still refuses:
        that would be a real change, and this host cannot say what it
        would be releasing anyone into.
        """
        self._ensure_current()
        if self.unreadable:
            if engaged:
                return self.killswitch()
            raise PeerStoreUnreadable(self.unreadable)
        self._killswitch = {"engaged": bool(engaged),
                            "at": self._now() if engaged else None,
                            "reason": str(reason or "")[:MAX_TEXT_CHARS]}
        self._write()
        self._audit("lan_killswitch",
                    {"engaged": bool(engaged),
                     "reason": self._killswitch["reason"]})
        return dict(self._killswitch)

    # -- internals ----------------------------------------------------

    def _set_state(self, host_id, state, event):
        self._ensure_current()
        if self.unreadable:
            raise PeerStoreUnreadable(self.unreadable)
        key = normalize_host_id(host_id)
        if key is None or key not in self._peers:
            raise PeerError("unknown_peer",
                            f"no peer record for {str(host_id)[:64]!r}")
        record = self._peers[key]
        previous = record["state"]
        record["state"] = state
        if state != PEER_ADMITTED:
            record["admitted_at"] = None
            record["admitted_via"] = None
        if state == PEER_BLOCKED:
            # A BLOCK IS THE LINEAGE BREAK, recorded DURABLY the instant it
            # happens -- not inferred later from the record's transient
            # state at re-admit time. Minting a fresh admission_id here is
            # a REVOCATION EPOCH: every job the old lineage stamped is
            # stale from this write onward (the dispatch fence compares
            # ids), so a blocked peer's queued work is dead even if the
            # revocation sweep never reaches it AND even if the block is
            # later laundered through observe-only before a re-admit. That
            # laundering path -- block -> observe -> admit -- was the
            # bypass: _set_state used to leave admission_id untouched, so
            # the re-admit saw an "unbroken" observe_only record and
            # PRESERVED the pre-block id, resurrecting pre-block work. The
            # break must live on the epoch, not on a state the operator
            # can move. observe-only is deliberately NOT a break: it is a
            # narrowing, and a read submitted while admitted keeps working
            # across it (24.6). forget() drops the record entirely, so its
            # break is expressed by the next admit minting fresh; a re-pin
            # mints in _upsert (pin_changed).
            record["admission_id"] = identity.mint_id()[:16]
        self._write()
        self._audit(event, {"host_id": key,
                            "peer_digest": peer_digest(
                                key, record["fingerprint"]),
                            "previous_state": previous})
        return dict(record)

    def _upsert(self, host_id, fingerprint, state, display_name="",
                endpoints=None, convoy_ids=None, cert_pem=None,
                clock_offset_s=None, admitted_via=None):
        self._ensure_current()
        if self.unreadable:
            raise PeerStoreUnreadable(self.unreadable)
        key = normalize_host_id(host_id)
        if key is None:
            raise PeerError("malformed_host_id",
                            f"{str(host_id)[:64]!r} is not a 32-character "
                            f"lowercase hex host_id")
        pin = normalize_fingerprint(fingerprint)
        if pin is None:
            raise PeerError("malformed_fingerprint",
                            f"{str(fingerprint)[:80]!r} is not a cvfp1-... "
                            f"SPKI fingerprint")
        now = self._now()
        existing = self._peers.get(key)
        # ONE KEY, ONE HOST. Admitting the same fingerprint under a second
        # host_id quietly disables the pin_mismatch detection built on
        # purpose -- "key X is pinned to host_id Y" can no longer be said
        # once two answers are true. A genuine key MOVE is expressed by
        # forgetting the old host first, which makes it a decision rather
        # than a silent loss of a check.
        clash = self.find_by_fingerprint(pin)
        if clash is not None and clash["host_id"] != key:
            raise PeerError(
                "fingerprint_already_pinned",
                f"{pin} is already pinned to host_id {clash['host_id']} on "
                f"this host; forget that peer first if the key really moved")
        if existing is None and len(self._peers) >= MAX_PEERS:
            raise PeerError("too_many_peers",
                            f"this host already holds {MAX_PEERS} peer "
                            f"records")
        record = dict(existing) if existing else _blank_record(key, pin, now)
        pin_changed = record["fingerprint"] != pin
        if pin_changed:
            # A RE-ADMISSION, audited as such -- never a silent update.
            record["pin_first_seen"] = now
            record["cert_pem"] = None
        record["fingerprint"] = pin
        record["state"] = state
        if display_name:
            record["display_name"] = str(display_name)[:MAX_TEXT_CHARS]
        if cert_pem is not None:
            record["cert_pem"] = _clean_cert(cert_pem)
        if endpoints is not None:
            record["endpoints"] = _clean_list(endpoints, MAX_ENDPOINTS)
        if convoy_ids is not None:
            record["convoy_ids"] = _clean_convoy_ids(convoy_ids)
        if clock_offset_s is not None:
            record["clock_offset_s"] = _clean_number(clock_offset_s)
        # THE ADMISSION LINEAGE. admission_id names the CURRENT unbroken
        # authorization: it is PRESERVED when this upsert merely re-affirms
        # or WIDENS a lineage that never broke (the SAME record was already
        # admitted or observe-only, SAME pin), and minted fresh -- a random
        # nonce, never a timestamp, so two writes inside one clock tick
        # cannot collide -- otherwise.
        #
        # MINTED FOR ANY BRAND-NEW IN-PROCESS RECORD (existing is None),
        # regardless of the target state -- including a PENDING one that
        # record_peer recreates after a forget. This is NOT cosmetic: a
        # forget pops the record, and forget -> record_peer -> observe ->
        # admit would otherwise re-admit a record whose admission_id was
        # never set (the mint used to live inside the PEER_ADMITTED block,
        # so a PENDING record kept the blank None), and that None COLLIDES
        # with the forgotten peer's None-lineage jobs -- laundering a full
        # revocation (confirming-panel resurrection-hunt lens). A fresh id
        # on creation makes those None jobs stale. The block's own break is
        # stamped by _set_state at BLOCK time, so block -> observe -> admit
        # cannot launder it either.
        #
        # The ONLY record that keeps admission_id == None is a LEGACY one
        # LOADED FROM DISK (a peers.json written before this field), and
        # that path is _coerce_record, never here. Such a record has NOT
        # been revoked, so a routine same-pin re-affirm must PRESERVE None
        # (its outstanding None-lineage jobs still match and dispatch) --
        # the `unbroken` branch below does exactly that. A real revocation
        # of a None-lineage peer mints a non-None epoch (block/re-pin, or
        # forget -> a fresh record here), which is what makes its None jobs
        # stale -- the break, not the mere absence of an id.
        unbroken = (existing is not None and not pin_changed
                    and existing.get("state") in (PEER_ADMITTED,
                                                  PEER_OBSERVE_ONLY))
        if not unbroken:
            record["admission_id"] = identity.mint_id()[:16]
        if state == PEER_ADMITTED:
            record["admitted_at"] = now
            record["admitted_via"] = str(admitted_via or "manual"
                                         )[:MAX_TEXT_CHARS]
        self._peers[key] = record
        self._write()
        self._audit("peer_recorded",
                    {"host_id": key, "state": state,
                     "peer_digest": peer_digest(key, pin),
                     "pin_changed": bool(existing) and pin_changed,
                     "admitted_via": record["admitted_via"]})
        return dict(record)


def _blank_record(host_id, fingerprint, now):
    """The peer record, plan 1.3, field for field."""
    return {"host_id": host_id,
            "fingerprint": fingerprint,
            "cert_pem": None,
            "display_name": "",
            "state": PEER_PENDING,
            "admitted_at": None,
            "admitted_via": None,
            "pin_first_seen": now,
            "last_seen": None,
            "endpoints": [],
            "convoy_ids": [],
            "clock_offset_s": None,
            # The unbroken-authorization lineage nonce (see _upsert).
            # None until the peer is first admitted.
            "admission_id": None}


def _clean_cert(value):
    if value is None:
        return None
    if not isinstance(value, str):
        raise PeerError("malformed_cert", "cert_pem must be a string")
    text = value.strip()
    if len(text) > MAX_CERT_CHARS:
        raise PeerError("malformed_cert",
                        f"cert_pem exceeds {MAX_CERT_CHARS} characters")
    return text or None


def _clean_list(values, limit):
    if isinstance(values, str) or not isinstance(values, (list, tuple)):
        raise PeerError("malformed_list", "expected a list of strings")
    # REJECT, never silently truncate. Slicing to the limit and clipping
    # each item lost data with no error -- an operator hand-edit to
    # peers.json (the file's own note invites one) dropped entries
    # invisibly, while every sibling validator raises. Consistency here is
    # what makes a bad edit fail closed instead of silently shrinking.
    if len(values) > limit:
        raise PeerError("malformed_list",
                        f"at most {limit} entries allowed, got "
                        f"{len(values)}")
    out = []
    for item in values:
        if not isinstance(item, str):
            raise PeerError("malformed_list",
                            f"{item!r} is not a string")
        s = item.strip()
        if len(s) > MAX_TEXT_CHARS:
            raise PeerError("malformed_list",
                            f"an entry exceeds {MAX_TEXT_CHARS} characters")
        if s and s not in out:
            out.append(s)
    return out


def _clean_convoy_ids(values):
    """Validate namespace grants with the canonical cross-layer policy."""
    if isinstance(values, str) or not isinstance(values, (list, tuple)):
        raise PeerError("malformed_list", "expected a list of strings")
    if len(values) > MAX_CONVOY_IDS:
        raise PeerError("malformed_list",
                        f"at most {MAX_CONVOY_IDS} entries allowed, got "
                        f"{len(values)}")
    out = []
    for item in values:
        try:
            namespace = identity.normalize_convoy_id(item)
        except identity.IdentityError as e:
            raise PeerError("malformed_list", e.detail or repr(item))
        if namespace not in out:
            out.append(namespace)
    return out


def _bounded_text(value, what):
    if value is None:
        return ""
    if not isinstance(value, str):
        raise PeerError("malformed_text", f"{what} must be a string")
    if len(value) > MAX_TEXT_CHARS:
        raise PeerError("malformed_text",
                        f"{what} exceeds {MAX_TEXT_CHARS} characters")
    return value


def _optional_number(value, what):
    if value is None:
        return None
    try:
        return _clean_number(value)
    except PeerError as e:
        raise PeerError("malformed_number", f"{what}: {e.detail}")


def _clean_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PeerError("malformed_number", f"{value!r} is not a number")
    value = float(value)
    if value != value or value in (float("inf"), float("-inf")):
        # NaN passes every comparison guard downstream; refuse it here.
        raise PeerError("malformed_number", "must be finite")
    return value


def _coerce_record(host_id, record):
    """Validate one record read from disk, or raise PeerError.

    Strict: an unknown state, a bad pin, a missing field -- any of them
    makes the whole store unreadable (see _load), because silently
    dropping a record can drop a BLOCK.
    """
    key = normalize_host_id(host_id)
    if key is None:
        raise PeerError("malformed_record",
                        f"peer key {str(host_id)[:64]!r} is not a host_id")
    if not isinstance(record, dict):
        raise PeerError("malformed_record",
                        f"peer {key} is not an object")
    pin = normalize_fingerprint(record.get("fingerprint"))
    if pin is None:
        raise PeerError("malformed_record",
                        f"peer {key} has no valid fingerprint")
    state = record.get("state")
    if state not in PEER_STATES:
        raise PeerError("malformed_record",
                        f"peer {key} has unknown state {state!r}")
    stored_host = record.get("host_id")
    if stored_host is not None and normalize_host_id(stored_host) != key:
        raise PeerError("malformed_record",
                        f"peer {key} carries host_id {stored_host!r}")
    clean = _blank_record(key, pin, None)
    clean["state"] = state
    # VALIDATED ON THE READ PATH, not only on the write path. These seven
    # used to be copied verbatim -- so a 200,000-character cert_pem (12x
    # the write-path cap) and a clock_offset_s of 'not-a-number' both
    # loaded clean and were written straight back out. Slice 3 reads
    # cert_pem to build a TRUST decision; a value that never passed a
    # validator is a loaded gun by then. A file is untrusted input even
    # when this process wrote it: a hand edit, a restore, or an older
    # build's record all arrive through here.
    _READ_VALIDATORS = {
        "cert_pem": _clean_cert,
        "display_name": lambda v: _bounded_text(v, "display_name"),
        "admitted_via": lambda v: _bounded_text(v, "admitted_via"),
        "admitted_at": lambda v: _optional_number(v, "admitted_at"),
        "pin_first_seen": lambda v: _optional_number(v, "pin_first_seen"),
        "last_seen": lambda v: _optional_number(v, "last_seen"),
        "clock_offset_s": lambda v: _optional_number(v, "clock_offset_s"),
        # Compared by EQUALITY only, so the shape just needs bounding; a
        # null stays None (an empty string is not a lineage).
        "admission_id": lambda v: (_bounded_text(v, "admission_id")
                                   or None),
    }
    for field, validate in _READ_VALIDATORS.items():
        if field in record:
            try:
                clean[field] = validate(record[field])
            except PeerError as e:
                raise PeerError("malformed_record",
                                f"peer {key} {field}: {e.detail}")
    for field, limit in (("endpoints", MAX_ENDPOINTS),
                         ("convoy_ids", MAX_CONVOY_IDS)):
        if field in record:
            try:
                clean[field] = (_clean_convoy_ids(record[field])
                                if field == "convoy_ids"
                                else _clean_list(record[field], limit))
            except PeerError as e:
                raise PeerError("malformed_record",
                                f"peer {key} {field}: {e.detail}")
    unknown = sorted(set(record) - set(_RECORD_FIELDS))
    if unknown:
        raise PeerError("malformed_record",
                        f"peer {key} carries unknown field(s) {unknown}")
    return clean
