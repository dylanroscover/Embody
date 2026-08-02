"""Host identity keypair for the embody-convoy host app (Phase 3, slice 1).

ONE Ed25519 keypair per host, minted on first use, host-private in
convoy_platform.data_dir(). It is used for BOTH the Convoy/1 envelope
signature and (from slice 3) the TLS certificate. host_id is unchanged by
any of this; the PIN a peer records is the binding
(host_id, fingerprint), and a change to either is a re-admission event.

ONE KEY, NOT TWO. Two keys means two rotations, two revocations, and a
window in which they disagree about who this host is. The classic
objection -- cross-protocol signature confusion -- is closed here by
CONSTRUCTION plus a domain tag: Ed25519Signer prepends
ENVELOPE_DOMAIN_TAG to every payload before signing, and verifies the
same way. TLS 1.3's CertificateVerify input begins with 64 0x20 bytes
(RFC 8446 4.4.3) and an envelope payload begins with '{', so the two
message spaces could not collide even without the tag -- the tag means
we do not have to RELY on that argument holding forever. A signature
produced in a TLS handshake is therefore not a valid envelope signature,
and vice versa; both directions are pinned by a test.

SPIKE RESULT (2026-08-01, run before this module was written): CPython's
bundled OpenSSL negotiates mutual TLS 1.3 with an Ed25519 LEAF cert on
  - TD 2025.33070 Python 3.11.15 / OpenSSL 3.0.15  -> TLSv1.3, PASS
  - dev venv       Python 3.9.13  / OpenSSL 1.1.1n -> TLSv1.3, PASS
so the P-256-cert-plus-Ed25519-envelope FALLBACK is not needed and one
key stands. test_convoy_hostkeys.py keeps that spike alive as a test, so
the CI matrix (windows-latest + macos-latest) re-proves it every run
rather than trusting a one-off measurement on one machine.

FINGERPRINT: SHA-256 over the DER SubjectPublicKeyInfo -- NOT over the
certificate. A cert re-issue with the same key (expiry, a new extension,
a corrupted cert file rebuilt from the key) must NOT read as a new
identity, because reading as new identity means every peer refuses this
host until a human re-admits it. Take the first 20 bytes (160 bits,
which is exactly 32 Crockford base32 symbols -- no padding), group 8x4,
prefix 'cvfp1-':

    cvfp1-4T7K-9WQE-2M1X-BJ0R-HD5N-6PZA-C3VY-8SGF

Stored and compared LOWERCASE (one canonical form, so a case difference
can never read as a pin mismatch); DISPLAYED uppercase-grouped, because
the whole point of the format is that two humans read it aloud to each
other over the phone. Crockford's alphabet drops I, L, O and U, which
is what makes that phone call survivable.

STORAGE: identity.key (PKCS8 PEM) + identity.cert.pem, both written with
convoy_platform._write_private -- 0600, O_EXCL, O_NOFOLLOW, atomic
replace. Never in a git tree, never in .embody/, never in project.json.

THE KEY IS IDENTITY; THE CERTIFICATE IS DERIVED. That single distinction
decides every failure policy in this module:

  KEY, corrupt/unreadable/wrong-algorithm -> REFUSE TO START. This
  mirrors HostStore._load_host_file's policy for host.json, for the same
  reason and one step more sharply: silently minting a fresh identity
  would orphan every peer relationship on every peer -- each of them
  would see an unknown fingerprint for a known host_id, refuse with
  pin_mismatch, and require a human to re-admit this machine out of
  band. ONLY FileNotFoundError leads to minting; every other OSError,
  every parse failure, and a key of the wrong algorithm are refusals.

  KEY, absent -> mint, but CLAIM THE DESTINATION rather than overwrite
  it. _mint_private_key stages the new key and links it into place, so a
  racing process that got there first WINS and this one adopts the key
  that actually landed. Read-then-write with an unconditional replace
  loses an identity roughly every time two processes start together
  (measured: 11 of 12 trials) and leaves the loser signing envelopes
  with a key no peer will ever see.

  CERTIFICATE, damaged in any way -> RE-ISSUE from the key, which
  reproduces the same identity exactly because the fingerprint is over
  the SPKI. "Damaged" means unparseable, wrong SPKI, outside its
  validity window, failing its own self-signature, or missing the
  profile the transport needs -- not merely absent. Refusing instead
  would hand a denial of service to anyone able to write one file.

  CERTIFICATE, unwritable -> DEGRADE, do not die. An unwritable cert is
  the one repair that cannot be performed, and killing the daemon for it
  would BE the denial of service the repair policy exists to avoid. The
  identity is fully usable for envelopes; only the TLS artifact is
  missing, and slice 3's LAN listener refusing to start without a
  certificate is correct and is slice 3's problem.

  ROTATION -> ALL OR NOTHING. rotate() stages both files, commits both,
  and rolls the key back if the certificate commit fails, then re-reads
  from disk so what this process reports can never disagree with what
  the next boot will load. A rotation that half-succeeded while
  reporting a refusal is the same "silently changed identity" failure
  the load path refuses to permit, reached through the write path.

THE DEPENDENCY. Stdlib Python has no asymmetric primitives and no X.509
writer. THIS MODULE, AND ONLY THIS MODULE, imports `cryptography`
(A-44's stdlib rule binds the BRIDGE; the host app is a separately
packaged application). Enforced by an import-isolation test that walks
dev/convoy/ and dev/embody/ recursively. `cryptography` being ABSENT is
handled gracefully rather than fatally: this module still IMPORTS, and
the host app still starts and serves loopback, reporting a named reason
and no identity -- because a user on an older install has no such
package and a daemon that will not start is a worse failure than one
that cannot yet speak to peers.
"""

import datetime
import hashlib
import os
import re
import secrets

import convoy_platform as platform_mod
import convoy_protocol as protocol

# The signature algorithm tag that travels INSIDE the signed payload.
# Registered here rather than in convoy_protocol so that adding an
# asymmetric scheme never touches _SIGNED_FIELDS, _signing_payload,
# verify_envelope or HmacSigner -- those stay bit-identical, and the
# HMAC tests stay green untouched.
ALG_ED25519_V1 = "ed25519-v1"

# Domain separation. Prepended to the canonical payload before signing
# and before verifying, so this key's envelope signatures live in their
# own message space, disjoint from TLS CertificateVerify and from any
# future Convoy signature over different bytes. NUL-terminated so no
# tagged message can ever be a prefix of another tag's message.
ENVELOPE_DOMAIN_TAG = b"convoy/1 envelope\x00"

# The on-disk names. PINNED BY A KNOWN-ANSWER TEST, because renaming
# either one makes an existing install's key invisible to
# _read_private_key -- which returns None on FileNotFoundError, so the
# rename would present as a silent fresh mint. Same hole class as a
# salted fingerprint: every property test still passes while the fleet
# loses its identity.
KEY_FILE = "identity.key"
CERT_FILE = "identity.cert.pem"

FINGERPRINT_PREFIX = "cvfp1-"
FINGERPRINT_BYTES = 20          # 160 bits -> exactly 32 base32 symbols
FINGERPRINT_GROUP = 4

# Crockford base32: no I, L, O or U, so a fingerprint read aloud or
# copied by hand cannot pick up the classic 1/I, 0/O confusions.
_CROCKFORD_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

# Canonical (lowercase) fingerprint form. The class is the Crockford
# alphabet minus i, l, o, u -- written as explicit ranges rather than
# [a-z] so an out-of-alphabet character cannot pass validation.
FINGERPRINT_RE = re.compile(
    r"^cvfp1-(?:[0-9a-hjkmnp-tv-z]{4}-){7}[0-9a-hjkmnp-tv-z]{4}$")

# Long validity, no expiry ceremony (the plan forbids CRL/OCSP/renewal
# machinery for a 2-30 machine LAN).
CERT_VALIDITY_DAYS = 20 * 365
# ... but HARD-CLAMPED to the UTCTime horizon rather than merely
# documented as staying inside it. RFC 5280 4.1.2.5 requires dates from
# 2050 onward to be encoded as GeneralizedTime, and a nominal 20 years
# crosses that line for real in August 2030 -- so the constant alone
# would quietly become a lie, caught only by a test that started failing
# as a mystery years later. The clamp makes the promise true at every
# mint date; a host minting in 2045 simply gets a shorter-lived
# certificate, which costs nothing because re-issue is automatic.
UTCTIME_HORIZON = datetime.datetime(2049, 12, 31, 23, 59, 59)
# Back-dated one day so a peer whose clock is a few hours behind does
# not see a freshly minted cert as not-yet-valid. Clock skew is measured
# and refused explicitly at admission (slice 4); it must not also show
# up here as an unexplained handshake failure.
CERT_BACKDATE_DAYS = 1


# ---------------------------------------------------------------------
# THE one dependency. Import failure is CAPTURED, never raised at import
# time: convoy_hostapp imports this module unconditionally, and an
# ImportError here would make the whole daemon unimportable on an
# install that simply has not got the package yet.
# ---------------------------------------------------------------------

try:
    from cryptography import x509 as _x509
    from cryptography.hazmat.primitives import serialization as _serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519 as _ed25519
    from cryptography.x509.oid import ExtendedKeyUsageOID as _EKUOID
    from cryptography.x509.oid import NameOID as _NameOID
    _IMPORT_ERROR = None
except Exception as _e:         # a broken install is not only ImportError
    _x509 = None
    _serialization = None
    _ed25519 = None
    _EKUOID = None
    _NameOID = None
    _IMPORT_ERROR = _e


class HostKeyError(Exception):
    """Structured identity refusal. `reason` is a stable machine code."""

    def __init__(self, reason, detail=""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


class CryptographyMissing(HostKeyError):
    """The `cryptography` package is unavailable.

    A SUBCLASS so callers that do not care can treat it as any other
    identity failure, but a DISTINCT type so the host app can catch
    exactly this one and degrade -- start, serve loopback, report the
    reason -- while every other HostKeyError still refuses to start.
    """

    def __init__(self, detail=""):
        super().__init__("cryptography_missing", detail)


class RotationRefused(HostKeyError):
    """A rotation that did NOT happen, and left the disk untouched.

    Distinct from RotationIndeterminate below, and the distinction is
    the whole point: a caller may report this one to an operator as a
    plain refusal, because it is TRUE. Nothing changed.
    """


class RotationIndeterminate(HostKeyError):
    """A rotation that failed AFTER the disk had already moved, and
    could not be rolled back.

    NEVER report this as a refusal. The on-disk identity may be the new
    one or the old one; the only honest response is to re-read from
    disk, say which it is, and audit it. This class exists so that
    outcome cannot be accidentally collapsed into "it was refused" --
    which is precisely how a rotation used to change the identity for
    the NEXT boot while telling the operator nothing had happened.
    """


def cryptography_available():
    """Can this install do asymmetric identity at all?"""
    return _IMPORT_ERROR is None


def _require_cryptography():
    if _IMPORT_ERROR is not None:
        raise CryptographyMissing(
            f"the 'cryptography' package is required for Convoy host "
            f"identity and could not be imported ({_IMPORT_ERROR}). The "
            f"host app runs without it, but this machine has no LAN "
            f"identity until it is installed.")


def _utcnow():
    """Naive UTC, built without utcnow(): utcnow() is deprecated on
    Python 3.12+, and timezone-aware datetimes are only accepted by
    newer cryptography releases. This form is correct on both."""
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------------
# Fingerprint
# ---------------------------------------------------------------------

def _crockford_b32(data):
    """Crockford base32 over exactly len(data)*8 bits, MSB first.

    len(data) is a multiple of 5 by construction (20 bytes), so the bit
    count divides evenly by 5 and there is no padding to specify, argue
    about, or get wrong.
    """
    if len(data) * 8 % 5:
        raise HostKeyError("malformed_fingerprint_input",
                           "length must be a multiple of 5 bytes")
    value = int.from_bytes(data, "big")
    total_bits = len(data) * 8
    return "".join(_CROCKFORD_ALPHABET[(value >> shift) & 0x1F]
                   for shift in range(total_bits - 5, -1, -5))


def fingerprint(public_der):
    """Canonical (lowercase) fingerprint of a DER SubjectPublicKeyInfo.

    Over the SPKI, NOT over a certificate: re-issuing a cert for the
    same key must leave the identity -- and therefore every peer's pin
    -- untouched.
    """
    if not isinstance(public_der, (bytes, bytearray)) or not public_der:
        raise HostKeyError("malformed_public_key", repr(type(public_der)))
    digest = hashlib.sha256(bytes(public_der)).digest()[:FINGERPRINT_BYTES]
    body = _crockford_b32(digest)
    groups = [body[i:i + FINGERPRINT_GROUP]
              for i in range(0, len(body), FINGERPRINT_GROUP)]
    return (FINGERPRINT_PREFIX + "-".join(groups)).lower()


def format_fingerprint(value):
    """Display form: lowercase prefix, UPPERCASE grouped body.

    Two operators compare these out of band during a join; uppercase is
    what makes that readable. Never store this form -- comparison is
    always against the canonical lowercase one.
    """
    if not is_valid_fingerprint(value):
        raise HostKeyError("malformed_fingerprint", repr(value))
    return FINGERPRINT_PREFIX + value[len(FINGERPRINT_PREFIX):].upper()


def is_valid_fingerprint(value):
    return isinstance(value, str) and bool(FINGERPRINT_RE.match(value))


def _public_der(public_key):
    return public_key.public_bytes(
        _serialization.Encoding.DER,
        _serialization.PublicFormat.SubjectPublicKeyInfo)


# ---------------------------------------------------------------------
# The Signer -- plugged into convoy_protocol's EXISTING seam
# ---------------------------------------------------------------------

class Ed25519Signer(protocol.Signer):
    """Per-host asymmetric signing over the Convoy/1 canonical payload.

    Drops into the seam convoy_protocol already defines: build_envelope
    and verify_envelope are untouched, and the sig_alg tag inside the
    signed subset means a verifier keyed for one scheme cannot be fooled
    into accepting another -- verify_envelope's existing
    `algorithm_mismatch` IS the downgrade defense, in both directions.

    THE DOMAIN TAG IS NOT OPTIONAL. sign() and verify() both operate on
    ENVELOPE_DOMAIN_TAG + payload. Removing it would let one key's
    signature over some other protocol's bytes (TLS CertificateVerify
    being the one that shares this key) be replayed as an envelope
    signature the moment those two message spaces ever overlapped.

    Construct with a private key to sign and verify, or with only a
    public key (verifier_from_public_der) to verify -- a verify-only
    instance REFUSES to sign rather than silently doing nothing.
    """

    alg = ALG_ED25519_V1

    def __init__(self, private_key=None, public_key=None):
        _require_cryptography()
        if private_key is None and public_key is None:
            raise HostKeyError("malformed_signer",
                               "needs a private or a public key")
        if private_key is not None and not isinstance(
                private_key, _ed25519.Ed25519PrivateKey):
            raise HostKeyError("malformed_signer", "not an Ed25519 private key")
        if public_key is None:
            public_key = private_key.public_key()
        elif not isinstance(public_key, _ed25519.Ed25519PublicKey):
            raise HostKeyError("malformed_signer", "not an Ed25519 public key")
        self._private = private_key
        self._public = public_key

    def public_der(self):
        return _public_der(self._public)

    def fingerprint(self):
        return fingerprint(self.public_der())

    def sign(self, payload_bytes):
        if self._private is None:
            raise HostKeyError(
                "no_private_key",
                "this signer holds only a public key and can verify, "
                "not sign")
        return self._private.sign(
            ENVELOPE_DOMAIN_TAG + payload_bytes).hex()

    def verify(self, payload_bytes, signature_hex):
        """True only for a signature this key made over the TAGGED
        payload. Every malformed input is False, never an exception:
        verify_envelope turns False into a named `bad_signature`
        refusal, while a raw exception would escape as an unclassified
        500 the audit trail could not read."""
        if not isinstance(signature_hex, str):
            return False
        try:
            signature = bytes.fromhex(signature_hex)
        except ValueError:
            return False
        try:
            self._public.verify(signature,
                                ENVELOPE_DOMAIN_TAG + payload_bytes)
        except Exception:       # InvalidSignature, and anything below it
            return False
        return True


def verifier_from_public_der(public_der):
    """A verify-only Ed25519Signer for a peer's DER SubjectPublicKeyInfo.

    This is the verify side of the pin: slice 3 recomputes a connecting
    peer's fingerprint from the certificate IT presented, looks up the
    pinned public key, and builds one of these. Malformed input is a
    named refusal, not a crash.
    """
    _require_cryptography()
    if not isinstance(public_der, (bytes, bytearray)) or not public_der:
        raise HostKeyError("malformed_public_key", repr(type(public_der)))
    try:
        public_key = _serialization.load_der_public_key(bytes(public_der))
    except Exception as e:
        raise HostKeyError("malformed_public_key", str(e))
    if not isinstance(public_key, _ed25519.Ed25519PublicKey):
        raise HostKeyError(
            "unsupported_public_key",
            f"expected Ed25519, got {type(public_key).__name__}")
    return Ed25519Signer(public_key=public_key)


def public_der_from_certificate(certificate_der):
    """The DER SubjectPublicKeyInfo carried by an X.509 certificate.

    THE bridge slice 3's transport needs and the reason it lives here:
    a TLS peer is identified by `getpeercert(binary_form=True)`, which
    returns the whole CERTIFICATE DER -- but the pin, the fingerprint and
    the envelope verifier are all over the SPKI, NOT the certificate (a
    cert re-issue with the same key must read as the same identity, see
    the module header). Parsing X.509 is `cryptography`'s job, and A-44
    charters exactly one module to import it, so convoy_peerserver /
    convoy_peerclient recompute a presented peer's fingerprint through
    here rather than taking the dependency themselves:

        spki = hostkeys.public_der_from_certificate(peer_cert_der)
        fp   = hostkeys.fingerprint(spki)              # compare to the pin
        vrfy = hostkeys.verifier_from_public_der(spki) # verify its envelopes

    Malformed input (a truncated cert, a non-Ed25519 key) is a NAMED
    HostKeyError, never a raw exception escaping into a handler thread.
    """
    _require_cryptography()
    if not isinstance(certificate_der, (bytes, bytearray)) \
            or not certificate_der:
        raise HostKeyError("malformed_certificate",
                           repr(type(certificate_der)))
    try:
        certificate = _x509.load_der_x509_certificate(bytes(certificate_der))
    except Exception as e:
        raise HostKeyError("malformed_certificate",
                           f"{type(e).__name__}: {e}")
    public_key = certificate.public_key()
    if not isinstance(public_key, _ed25519.Ed25519PublicKey):
        raise HostKeyError(
            "unsupported_public_key",
            f"certificate carries a {type(public_key).__name__}, not Ed25519")
    return _public_der(public_key)


# ---------------------------------------------------------------------
# On-disk identity
# ---------------------------------------------------------------------

class HostKeys:
    """This host's loaded identity: the key, its cert, its fingerprint.

    The private key object never leaves this class except through
    signer(); nothing here serializes it, and no route returns it.

    `origin` records how this instance came to be ('loaded', 'minted',
    'adopted', 'cert_reissued', 'rotated') so the caller can audit it
    without this module needing an audit sink of its own. 'adopted'
    means this process generated a key, LOST the race to create the
    file, and is using the winner's -- worth a line precisely because it
    is the case that used to silently produce an identity nobody else
    had.

    `certificate_pem` is None when the certificate could not be written.
    The identity is still fully usable for envelopes; `cert_reason`
    names what is wrong so the host app can report it rather than
    pretending everything is fine.
    """

    alg = ALG_ED25519_V1

    def __init__(self, private_key, certificate_pem, key_path, cert_path,
                 origin, cert_reason=None):
        self._private = private_key
        self.certificate_pem = certificate_pem
        self.cert_reason = cert_reason
        self.key_path = key_path
        self.cert_path = cert_path
        self.origin = origin
        self.public_der = _public_der(private_key.public_key())
        self.fingerprint = fingerprint(self.public_der)

    def fingerprint_display(self):
        return format_fingerprint(self.fingerprint)

    def signer(self):
        """A signing Ed25519Signer for build_envelope."""
        return Ed25519Signer(private_key=self._private)

    def verifier(self):
        """A verify-only signer for this host's own public key."""
        return Ed25519Signer(public_key=self._private.public_key())

    def public_identity(self):
        """Everything that is safe to publish. NEVER the private key.

        certificate_pem IS safe to publish -- it carries only the public
        key, and a peer must obtain it to PIN this host (slice 3). It is
        the one piece of material an operator setting up admission has to
        copy from here to a peer's /peers/admit, so it is served on the
        loopback /identity route beside the fingerprint the operator
        compares out of band. None when the derived cert could not be
        written (the identity still signs envelopes; only TLS is missing).
        """
        return {
            "alg": self.alg,
            "fingerprint": self.fingerprint,
            "fingerprint_display": self.fingerprint_display(),
            "certificate": self.certificate_pem is not None,
            "certificate_reason": self.cert_reason,
            "certificate_pem": self.certificate_pem,
        }


def key_path(data_dir):
    return os.path.join(data_dir, KEY_FILE)


def cert_path(data_dir):
    return os.path.join(data_dir, CERT_FILE)


def _stage_path(path, suffix):
    """A private, unpredictable sibling of `path` used to prove a write
    can succeed before anything is committed over a live file."""
    return f"{path}.{os.getpid()}.{secrets.token_hex(8)}.{suffix}"


def _discard(*paths):
    for path in paths:
        try:
            os.unlink(path)
        except OSError:
            pass


def _read_private_key(path):
    """The stored key, or None if it is genuinely ABSENT.

    The narrowness of that None is the whole safety property: ONLY
    FileNotFoundError returns it. A permission error, a directory in the
    key's place, a truncated file, an encrypted key, a key of the wrong
    algorithm -- every one of those is a refusal, because minting on top
    of any of them would replace an identity that may still exist.
    """
    try:
        with open(path, "rb") as f:
            data = f.read()
    except FileNotFoundError:
        return None
    except OSError as e:
        raise HostKeyError(
            "identity_unreadable",
            f"the host identity key at {path} could not be read ({e}). "
            f"Refusing to start with a fresh identity -- restore the file "
            f"or delete it deliberately to mint a new one, which every "
            f"peer will then have to re-admit.")
    try:
        key = _serialization.load_pem_private_key(data, password=None)
    except Exception as e:
        raise HostKeyError(
            "identity_unreadable",
            f"the host identity key at {path} is not a readable "
            f"unencrypted PEM private key ({type(e).__name__}: {e}). "
            f"Refusing to start with a fresh identity -- silently "
            f"re-minting would orphan every peer relationship on every "
            f"peer.")
    if not isinstance(key, _ed25519.Ed25519PrivateKey):
        raise HostKeyError(
            "identity_wrong_algorithm",
            f"the host identity key at {path} is a "
            f"{type(key).__name__}, not Ed25519. Refusing to start "
            f"rather than replacing it.")
    return key


def _private_key_pem(private_key):
    return private_key.private_bytes(
        _serialization.Encoding.PEM,
        _serialization.PrivateFormat.PKCS8,
        _serialization.NoEncryption()).decode("ascii")


def _claim_file(path, data):
    """Create `path` with `data` IF AND ONLY IF it does not exist yet.

    Returns True if this call created it, False if somebody else already
    had it. Never overwrites, and never leaves a torn file behind.

    Two properties are needed at once and neither is available alone:
    ATOMIC CONTENT (a crash must not leave a half-written private key
    that then refuses to start forever) and CREATE-IF-ABSENT (a racing
    process must not clobber an identity that already landed).
    convoy_platform._write_private gives the first -- its O_EXCL is on
    its own random temp name, and the destination is always replaced --
    so this stages through it and then commits with os.link, whose
    "fails if the destination exists" is atomic on both POSIX and NTFS.

    If the filesystem has no hard links (rare here: the data dir lives
    in the per-user profile), it falls back to _write_private's plain
    atomic replace after a last-moment existence check. That fallback
    can still lose a race -- but _mint_private_key RE-READS the file
    after every claim either way, so even then every racer converges on
    the identity that is actually on disk instead of running one that is
    not.
    """
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    stage = _stage_path(path, "claim")
    platform_mod._write_private(stage, data)
    try:
        try:
            os.link(stage, path)
            return True
        except FileExistsError:
            return False
        except OSError:
            # No hard-link support on this filesystem.
            if os.path.exists(path):
                return False
            platform_mod._write_private(path, data)
            return True
    finally:
        _discard(stage)


def _mint_private_key(path):
    """Create the identity key if absent, and return what LANDED.

    Returns (private_key, created). `created` is False when another
    process won the race -- this one then adopts the winner's key rather
    than carrying on with a key that exists only in its own memory. That
    is the difference between "one of two starts is redundant" and "one
    of two hosts signs envelopes no peer can verify, and reports a
    fingerprint that is on no disk anywhere".
    """
    fresh = _ed25519.Ed25519PrivateKey.generate()
    created = _claim_file(path, _private_key_pem(fresh))
    landed = _read_private_key(path)
    if landed is None:
        # The file just created (or found) is gone again: something else
        # is actively deleting the identity underneath us. Refuse rather
        # than loop.
        raise HostKeyError(
            "identity_vanished",
            f"the host identity key at {path} disappeared immediately "
            f"after it was created -- something else is writing this "
            f"data directory.")
    return landed, created


# -- certificate -------------------------------------------------------

def _issue_certificate(private_key, now=None):
    """A long-validity self-signed cert over this key, as PEM text.

    There is no CA and there will not be one (the plan's section 1.2): a
    CA key is a promotion target and needs a home no machine in a
    leaderless topology is entitled to. Trust is anchored by PINNING
    this exact cert, so the only job the certificate does is carry the
    public key through a TLS handshake.

    THE PROFILE IS LOAD-BEARING, not decoration. `ca=False` plus
    `key_cert_sign=False` is what stops a pinned peer from being a
    certificate authority: slice 3 pins peers with
    load_verify_locations(cadata=<peer PEM>), which makes each pinned
    leaf an OpenSSL TRUST ANCHOR -- flip ca to True and that peer can
    issue certificates for any other identity and have them accepted at
    the TLS layer (demonstrated by the review panel). The app-layer SPKI
    recomputation is the second defense and still refuses it; this is
    the first. EKU carries BOTH roles because the peer leg is mutual TLS
    and one certificate serves this host as server and as client. Every
    bit here is pinned by a test.

    The subject/SAN carry the fingerprint purely so a human dumping the
    cert can see which identity it is. They are DECORATIVE and MUST
    NEVER be trusted: slice 3 recomputes the SPKI fingerprint locally
    from the presented certificate, and that -- not any name inside it
    -- identifies the peer. They are nonetheless pinned to the key by a
    test, because a certificate that self-reports a fingerprint which is
    not its own key's is an operator-deception surface the moment
    anybody dumps it to check which identity they are holding.
    """
    public_key = private_key.public_key()
    fp = fingerprint(_public_der(public_key))
    name = _x509.Name([_x509.NameAttribute(_NameOID.COMMON_NAME, fp)])
    now = _utcnow() if now is None else now
    not_after = min(now + datetime.timedelta(days=CERT_VALIDITY_DAYS),
                    UTCTIME_HORIZON)
    builder = (
        _x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(public_key)
        .serial_number(_x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=CERT_BACKDATE_DAYS))
        .not_valid_after(not_after)
        .add_extension(_x509.BasicConstraints(ca=False, path_length=None),
                       critical=True)
        .add_extension(
            _x509.KeyUsage(
                digital_signature=True, content_commitment=False,
                key_encipherment=False, data_encipherment=False,
                key_agreement=False, key_cert_sign=False, crl_sign=False,
                encipher_only=False, decipher_only=False),
            critical=True)
        .add_extension(
            _x509.ExtendedKeyUsage([_EKUOID.SERVER_AUTH,
                                    _EKUOID.CLIENT_AUTH]),
            critical=False)
        .add_extension(_x509.SubjectAlternativeName([_x509.DNSName(fp)]),
                       critical=False)
        .add_extension(
            _x509.SubjectKeyIdentifier.from_public_key(public_key),
            critical=False)
    )
    # Ed25519 signs the certificate directly; the hash algorithm
    # argument MUST be None for it (there is no prehash step).
    certificate = builder.sign(private_key, None)
    return certificate.public_bytes(
        _serialization.Encoding.PEM).decode("ascii")


def _naive_utc(value):
    """A datetime from cryptography as naive UTC, on any version.

    The *_utc properties (aware) arrived in cryptography 42 and the
    naive ones are deprecated there; supporting both is one line and
    avoids a version-dependent comparison bug inside a validity check.
    """
    if value.tzinfo is not None:
        return value.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return value


def _certificate_validity(certificate):
    """(not_before, not_after) as naive UTC, on any cryptography."""
    before = getattr(certificate, "not_valid_before_utc", None)
    after = getattr(certificate, "not_valid_after_utc", None)
    if before is None:
        before = certificate.not_valid_before
    if after is None:
        after = certificate.not_valid_after
    return _naive_utc(before), _naive_utc(after)


def _certificate_problem(certificate, private_key, now):
    """Why this certificate cannot be used, or None if it is fine.

    "Matches the key" is NOT sufficient, and treating it as sufficient
    was a real denial of service: an expired certificate whose SPKI was
    correct got kept forever and then failed the TLS handshake on both
    legs with no self-heal, on a path designed to be self-healing. The
    non-adversarial triggers are ordinary -- a data dir restored from
    backup, or a certificate minted while the machine's clock was wrong,
    which is exactly the show-machine condition the plan spends a whole
    paragraph refusing at admission.
    """
    expected = _public_der(private_key.public_key())
    try:
        actual = _public_der(certificate.public_key())
    except Exception:
        return "unreadable_public_key"
    if actual != expected:
        # A cert for somebody else's key is not this host's certificate,
        # however well-formed it is.
        return "spki_mismatch"

    try:
        not_before, not_after = _certificate_validity(certificate)
    except Exception:
        return "unreadable_validity"
    if now < not_before:
        return "not_yet_valid"
    if now > not_after:
        return "expired"

    if certificate.issuer != certificate.subject:
        return "not_self_issued"
    try:
        certificate.public_key().verify(certificate.signature,
                                        certificate.tbs_certificate_bytes)
    except Exception:
        # Self-SIGNED, not merely self-issued: a matching issuer and
        # subject prove nothing on their own.
        return "bad_self_signature"

    try:
        basic = certificate.extensions.get_extension_for_class(
            _x509.BasicConstraints).value
        usage = certificate.extensions.get_extension_for_class(
            _x509.KeyUsage).value
        extended = certificate.extensions.get_extension_for_class(
            _x509.ExtendedKeyUsage).value
        certificate.extensions.get_extension_for_class(
            _x509.SubjectAlternativeName)
        certificate.extensions.get_extension_for_class(
            _x509.SubjectKeyIdentifier)
    except _x509.ExtensionNotFound:
        return "profile_incomplete"
    if basic.ca or usage.key_cert_sign or usage.crl_sign:
        # A pinned leaf is an OpenSSL TRUST ANCHOR in slice 3; a
        # CA-flagged one could issue certificates for other identities.
        return "profile_ca"
    if not usage.digital_signature:
        return "profile_no_digital_signature"
    roles = set(extended)
    if _EKUOID.SERVER_AUTH not in roles or _EKUOID.CLIENT_AUTH not in roles:
        # Mutual TLS needs both roles out of one certificate.
        return "profile_incomplete_eku"
    return None


def _read_certificate(path, private_key, now):
    """(pem, None) if the stored cert is usable FOR THIS KEY, else
    (None, reason).

    Unlike the key, an unusable certificate is not a refusal. It is a
    DERIVED artifact -- regenerating it from the private key reproduces
    the same identity exactly, because the fingerprint is over the SPKI
    -- so refusing would only convert "one file got damaged" into "this
    host is down until a human notices", and would hand a denial of
    service to anyone able to write a single file in the data dir.
    """
    try:
        with open(path, "rb") as f:
            data = f.read()
    except FileNotFoundError:
        return None, "absent"
    except OSError:
        return None, "unreadable"
    try:
        certificate = _x509.load_pem_x509_certificate(data)
    except Exception:
        return None, "unparseable"
    problem = _certificate_problem(certificate, private_key, now)
    if problem:
        return None, problem
    try:
        return data.decode("ascii"), None
    except UnicodeDecodeError:
        return None, "not_ascii"


# -- load / rotate -----------------------------------------------------

def load_or_create(data_dir, now=None):
    """This host's identity, minting it ONLY on genuine first run.

    Raises CryptographyMissing when the dependency is absent (the caller
    may degrade), and HostKeyError for a corrupt/unreadable/wrong-type
    KEY (the caller must NOT degrade -- refuse to start). A certificate
    that cannot be read, repaired OR WRITTEN never raises: it degrades,
    because the key is the identity and the cert is derived from it.
    """
    _require_cryptography()
    now = _utcnow() if now is None else now
    kpath = key_path(data_dir)
    cpath = cert_path(data_dir)

    private_key = _read_private_key(kpath)
    if private_key is None:
        private_key, created = _mint_private_key(kpath)
        origin = "minted" if created else "adopted"
    else:
        origin = "loaded"

    certificate_pem, cert_reason = _read_certificate(cpath, private_key, now)
    if certificate_pem is None:
        try:
            certificate_pem = _issue_certificate(private_key, now=now)
            platform_mod._write_private(cpath, certificate_pem)
            cert_reason = None
            if origin == "loaded":
                origin = "cert_reissued"
        except OSError as e:
            # DEGRADE. Killing the daemon because one DERIVED file
            # cannot be rewritten IS the denial of service the repair
            # policy exists to prevent -- and under the A-36 supervisor
            # it becomes a crash-restart loop. The identity is intact
            # and usable for envelopes; only the TLS artifact is
            # missing, which is slice 3's problem to refuse on.
            certificate_pem = None
            # The OS message, NOT str(e): str(e) on Windows carries the
            # full source and destination paths of the atomic replace,
            # including the random temp name -- unstable noise in a
            # field a client compares, and needless path disclosure on a
            # route. The reason CODE is the stable part.
            cert_reason = f"cert_unwritable: {getattr(e, 'strerror', None) or e}"

    return HostKeys(private_key, certificate_pem, kpath, cpath, origin,
                    cert_reason)


def rotate(data_dir, expected_fingerprint, now=None):
    """Mint a FRESH identity, retiring the current one. ALL OR NOTHING.

    Every peer that had pinned this host now sees a pin_mismatch and
    must re-admit after comparing fingerprints out of band -- that is
    the intended, visible consequence, and it is why rotation is an
    explicit operator act and never automatic.

    COMPARE-AND-SWAP, not a bare command. `expected_fingerprint` must
    echo the identity CURRENTLY ON DISK (not merely the one the caller's
    process holds in memory, which may already be stale). This is the
    same shape as A-22's expected_runtime_id, which this codebase
    already uses to stop a request acting on state that moved: a blind
    call, a replayed call, or a call racing another rotation is refused
    instead of destroying an identity whose loss costs a two-human
    out-of-band re-admission on EVERY peer. It is not a defense against
    a same-user attacker -- nothing at this layer is, and the retired
    key is unrecoverable -- it is what turns an irreversible accident
    into a named refusal.

    ATOMICITY. Both files are staged first, so a full disk or an
    unwritable directory refuses before anything live is touched. The
    key is committed, then the certificate; if the certificate commit
    fails the key is rolled back, and only if the ROLLBACK also fails
    does this raise RotationIndeterminate -- the one outcome a caller
    must never report as a refusal, because the disk moved.
    """
    _require_cryptography()
    now = _utcnow() if now is None else now
    kpath = key_path(data_dir)
    cpath = cert_path(data_dir)

    # -- compare and swap, against DISK ---------------------------------
    current = _read_private_key(kpath)       # raises on a damaged key
    current_fp = (fingerprint(_public_der(current.public_key()))
                  if current is not None else None)
    if not expected_fingerprint:
        raise RotationRefused(
            "rotation_unconfirmed",
            "rotation must echo the CURRENT fingerprint to confirm it "
            "(this host is " + (current_fp or "un-minted") + "). Every "
            "peer that pinned this host must re-admit it out of band "
            "afterwards, so an unconfirmed rotation is refused.")
    if not is_valid_fingerprint(expected_fingerprint):
        raise RotationRefused(
            "rotation_unconfirmed",
            f"{expected_fingerprint!r} is not a fingerprint")
    if expected_fingerprint != current_fp:
        raise RotationRefused(
            "rotation_fingerprint_mismatch",
            f"this host's identity is {current_fp or 'un-minted'}, not "
            f"{expected_fingerprint} -- refusing to rotate an identity "
            f"the caller was not looking at.")

    previous_key_pem = _private_key_pem(current)

    # -- stage both, commit both ----------------------------------------
    fresh = _ed25519.Ed25519PrivateKey.generate()
    fresh_key_pem = _private_key_pem(fresh)
    fresh_cert_pem = _issue_certificate(fresh, now=now)
    key_stage = _stage_path(kpath, "rotate")
    cert_stage = _stage_path(cpath, "rotate")
    try:
        platform_mod._write_private(key_stage, fresh_key_pem)
        platform_mod._write_private(cert_stage, fresh_cert_pem)
    except OSError as e:
        _discard(key_stage, cert_stage)
        raise RotationRefused(
            "rotation_failed",
            f"could not stage the new identity ({e}); the identity on "
            f"disk is UNCHANGED and this host is still {current_fp}.")

    try:
        try:
            platform_mod._write_private(kpath, fresh_key_pem)
        except OSError as e:
            raise RotationRefused(
                "rotation_failed",
                f"could not write the new key ({e}); the identity on "
                f"disk is UNCHANGED and this host is still {current_fp}.")
        try:
            platform_mod._write_private(cpath, fresh_cert_pem)
        except OSError as e:
            try:
                platform_mod._write_private(kpath, previous_key_pem)
            except OSError as rollback_error:
                raise RotationIndeterminate(
                    "rotation_indeterminate",
                    f"the new key was written but its certificate was "
                    f"not ({e}), and the old key could not be restored "
                    f"({rollback_error}). The identity on disk has "
                    f"CHANGED -- re-read it, and expect every peer to "
                    f"need re-admission.")
            raise RotationRefused(
                "rotation_failed",
                f"could not write the new certificate ({e}); the key "
                f"was rolled back and this host is still {current_fp}.")
    finally:
        _discard(key_stage, cert_stage)

    return HostKeys(fresh, fresh_cert_pem, kpath, cpath, "rotated")
